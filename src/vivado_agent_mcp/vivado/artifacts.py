from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence_store import load_json_evidence
from .managed_path import (
    ManagedPathError,
    atomic_copy_file,
    atomic_write_bytes,
    ensure_managed_directory,
    file_identity,
    is_reparse_point,
    validate_managed_path,
)

ARTIFACT_CATEGORIES = {
    ".bit": "bitstream",
    ".ltx": "debug_probes",
    ".dcp": "checkpoint",
    ".rpt": "report",
    ".rpx": "report",
    ".pb": "vivado_metadata",
}
MAX_ARTIFACT_MANIFEST_BYTES = 4 * 1024 * 1024
ARTIFACT_COMPLETION_MTIME_TOLERANCE_NS = 2_000_000_000
MAX_BITSTREAM_HEADER_FIELD_BYTES = 64 * 1024


def collect_artifacts(
    *,
    project_dir: str | Path,
    run_dir: str | Path,
    run_name: str,
    output_dir: str | Path | None = None,
    run_context: dict[str, Any] | None = None,
    design_execution_identity: dict[str, Any] | None = None,
    collection_id: str | None = None,
) -> dict[str, Any]:
    project_path = Path(project_dir).resolve()
    run_path = Path(run_dir).resolve()
    export_dir = Path(output_dir).resolve() if output_dir else (project_path / "vmcp_artifacts" / run_name).resolve()
    _assert_inside_project(project_path, run_path, operation="read")
    _assert_inside_project(project_path, export_dir, operation="write")
    ensure_managed_directory(project_path, export_dir)
    context = run_context or {}
    blockers = artifact_run_blockers(context)
    design_identity = (
        design_execution_identity
        if isinstance(design_execution_identity, dict)
        else context.get("design_execution_identity")
        if isinstance(context.get("design_execution_identity"), dict)
        else {}
    )
    if design_identity.get("status") != "READY" or not re.fullmatch(r"[0-9a-f]{64}", str(design_identity.get("sha256", ""))):
        blockers.append("design execution identity is missing, incomplete, or invalid")
    run_execution_identity, run_identity_blockers = _run_execution_identity(run_path)
    blockers.extend(run_identity_blockers)
    expected_bitstream = Path(str(context.get("expected_bitstream_path", ""))).resolve() if context.get("expected_bitstream_path") else None
    observed_bitstreams = sorted(path.resolve() for path in run_path.rglob("*.bit") if path.is_file())
    raw_context_bitstreams = context.get("run_bitstream_files", [])
    if not isinstance(raw_context_bitstreams, list):
        raw_context_bitstreams = []
    context_bitstreams = sorted(
        Path(str(path)).resolve()
        for path in raw_context_bitstreams
        if str(path)
    )
    run_top = str(context.get("run_top", "")).strip()
    project_part = str(context.get("project_part", "")).strip()
    if not run_top or not project_part:
        blockers.append("Vivado artifact context is missing the target run top or project part")
    if expected_bitstream is None:
        blockers.append("Vivado artifact context did not identify the target run bitstream path")
    else:
        try:
            expected_bitstream.relative_to(run_path)
        except ValueError:
            blockers.append("Vivado target bitstream path is outside the target run directory")
        if run_top and expected_bitstream.name != f"{run_top}.bit":
            blockers.append("Vivado target bitstream filename does not match the target run top")
        if observed_bitstreams != [expected_bitstream]:
            blockers.append("target run must contain exactly one canonical top-named bitstream")
        if context_bitstreams != [expected_bitstream]:
            blockers.append("Vivado run context did not report exactly the canonical bitstream")
    if not _write_bitstream_step_attested(context, run_execution_identity):
        blockers.append("Vivado write_bitstream step is explicitly disabled or lacks completed step markers")

    artifacts: list[dict[str, Any]] = []
    for source in sorted(path for path in run_path.rglob("*") if path.is_file()):
        category = classify_artifact(source)
        if category is None:
            continue
        if category == "bitstream" and (expected_bitstream is None or source.resolve() != expected_bitstream):
            continue
        destination = _unique_destination(export_dir, source.name)
        atomic_copy_file(project_path, source, project_path, destination)
        export_sha256 = sha256_file(destination)
        source_sha256 = sha256_file(source)
        if source_sha256 != export_sha256:
            blockers.append(f"artifact source changed while it was copied: {source}")
        artifact = {
            "source_path": str(source),
            "export_path": str(destination),
            "size": destination.stat().st_size,
            "sha256": export_sha256,
            "source_sha256": source_sha256,
            "source_mtime_ns": source.stat().st_mtime_ns,
            "category": category,
        }
        if category == "bitstream":
            try:
                source_header = parse_bitstream_header(source)
                export_header = parse_bitstream_header(destination)
            except (OSError, ValueError) as exc:
                blockers.append(f"bitstream header is invalid: {exc}")
            else:
                if source_header != export_header:
                    blockers.append("bitstream source and exported copy have different header identity")
                blockers.extend(_bitstream_header_blockers(export_header, top=run_top, part=project_part))
                artifact["bitstream_header"] = export_header
        artifacts.append(artifact)

    for artifact in artifacts:
        blockers.extend(_artifact_timing_blockers(artifact, run_execution_identity))

    if not any(item["category"] == "bitstream" for item in artifacts):
        blockers.append("no bitstream artifact was found in the implementation run directory")
    manifest = {
        "schema_version": 4,
        "status": "READY" if not blockers else "BLOCK",
        "collection_id": collection_id or f"artifact_{uuid.uuid4().hex}",
        "run_name": run_name,
        "project_dir": str(project_path),
        "run_dir": str(run_path),
        "output_dir": str(export_dir),
        "manifest_path": str(export_dir / "manifest.json"),
        "evidence_freshness": {
            "status": "FRESH" if not blockers else "STALE",
            "run_name": run_name,
            "collected_at": _utc_now(),
            "needs_refresh": _truthy(context.get("run_needs_refresh")),
            "source": "collect_build_artifacts",
            "reasons": blockers,
        },
        "run_snapshot": {
            "run_name": run_name,
            "status": str(context.get("run_status", "")),
            "progress": str(context.get("run_progress", "")),
            "needs_refresh": str(context.get("run_needs_refresh", "")),
            "directory": str(run_path),
            "session_generation_id": str(context.get("session_generation_id", "")),
            "top": run_top,
            "part": project_part,
            "expected_bitstream_path": str(expected_bitstream) if expected_bitstream else "",
            "write_bitstream_step_enabled": str(context.get("write_bitstream_step_enabled", "")),
            "write_bitstream_step_status": str(context.get("write_bitstream_step_status", "")),
        },
        "bitstream_origin": {
            "status": "ATTESTED" if not any("bitstream" in reason.lower() for reason in blockers) else "BLOCK",
            "policy": "vivado_canonical_path_plus_unique_output_plus_xilinx_header",
            "canonical_path": str(expected_bitstream) if expected_bitstream else "",
            "top": run_top,
            "part": project_part,
        },
        "run_execution_identity": run_execution_identity,
        "design_execution_identity": design_identity,
        "design_execution_identity_sha256": str(design_identity.get("sha256", "")),
        "artifacts": artifacts,
    }
    manifest_path = Path(manifest["manifest_path"])
    atomic_write_bytes(
        project_path,
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return manifest


def artifact_run_blockers(context: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    status = str(context.get("run_status", "")).strip()
    if "complete" not in status.lower():
        blockers.append(f"run status is not complete: {status or 'missing'}")
    if "write_bitstream" not in status.lower():
        blockers.append(f"run status does not attest write_bitstream completion: {status or 'missing'}")
    progress = str(context.get("run_progress", "")).strip().rstrip("%")
    try:
        complete = float(progress) >= 100.0
    except ValueError:
        complete = False
    if not complete:
        blockers.append(f"run progress is not 100%: {context.get('run_progress', '') or 'missing'}")
    needs_refresh = str(context.get("run_needs_refresh", "")).strip().lower()
    if needs_refresh not in {"0", "false", "no"}:
        blockers.append(f"run needs_refresh is not false: {needs_refresh or 'missing'}")
    return blockers


def _assert_inside_project(project_dir: Path, target: Path, *, operation: str) -> None:
    try:
        target.resolve().relative_to(project_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"Refusing to {operation} outside project directory") from exc


def load_artifact_manifest_with_sha256(
    path: str | Path,
    *,
    project_dir: str | Path | None = None,
) -> tuple[Any, str]:
    manifest_path = Path(path)
    root = Path(project_dir) / "vmcp_artifacts" if project_dir is not None else manifest_path.parent
    data, snapshot = load_json_evidence(
        manifest_path,
        root=root,
        max_bytes=MAX_ARTIFACT_MANIFEST_BYTES,
    )
    if isinstance(data, dict):
        data.setdefault("manifest_path", str(manifest_path))
    return data, snapshot.sha256


def load_artifact_manifest(path: str | Path) -> Any:
    data, _ = load_artifact_manifest_with_sha256(path)
    return data


def resolve_artifact_manifest_for_read(
    path: str | Path,
    *,
    project_dir: str | Path,
    max_bytes: int = MAX_ARTIFACT_MANIFEST_BYTES,
) -> Path:
    manifest = Path(os.path.abspath(os.fspath(path)))
    root = Path(os.path.abspath(os.fspath(Path(project_dir) / "vmcp_artifacts")))
    if manifest.name != "manifest.json":
        raise ValueError("Artifact manifest path must end with manifest.json")
    try:
        validated = validate_managed_path(root, manifest)
    except ManagedPathError as exc:
        if not os.path.lexists(manifest):
            raise FileNotFoundError(f"Artifact manifest was not found: {manifest}") from exc
        raise ValueError(str(exc)) from exc
    if not validated.is_file():
        raise FileNotFoundError(f"Artifact manifest was not found: {validated}")
    size = validated.stat().st_size
    if size > max_bytes:
        raise ValueError(f"Artifact manifest exceeds the {max_bytes}-byte read limit")
    return validated


def validate_artifact_manifest(
    data: Any,
    *,
    manifest_path: str | Path,
    project_dir: str | Path,
    run_name: str,
    current_run_context: dict[str, Any],
    current_design_execution_identity: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Artifact manifest must be a JSON object")
    manifest = Path(manifest_path).resolve()
    project = Path(project_dir).resolve()
    artifacts_root = (project / "vmcp_artifacts").resolve()
    _assert_inside(artifacts_root, manifest, "Artifact manifest must stay under the current project vmcp_artifacts directory")
    if manifest.name != "manifest.json":
        raise ValueError("Artifact manifest path must end with manifest.json")
    if data.get("schema_version") != 4:
        raise ValueError("Artifact manifest schema_version=4 is required")
    if str(data.get("run_name", "")) != run_name:
        raise ValueError("Artifact manifest run_name does not match the requested run")
    if Path(str(data.get("project_dir", ""))).resolve() != project:
        raise ValueError("Artifact manifest project identity does not match the current project")
    output_dir = Path(str(data.get("output_dir", ""))).resolve()
    _assert_inside(artifacts_root, output_dir, "Artifact output directory must stay under vmcp_artifacts")
    if manifest.parent != output_dir:
        raise ValueError("Artifact manifest must be located in its output_dir")
    freshness = data.get("evidence_freshness") if isinstance(data.get("evidence_freshness"), dict) else {}
    if str(freshness.get("status", "")).upper() != "FRESH" or _truthy(freshness.get("needs_refresh")):
        raise ValueError("Artifact manifest evidence is stale")
    if artifact_run_blockers(current_run_context):
        raise ValueError("Current Vivado run is incomplete or needs refresh")
    snapshot = data.get("run_snapshot") if isinstance(data.get("run_snapshot"), dict) else {}
    if artifact_run_blockers(
        {
            "run_status": snapshot.get("status"),
            "run_progress": snapshot.get("progress"),
            "run_needs_refresh": snapshot.get("needs_refresh"),
        }
    ):
        raise ValueError("Artifact manifest run snapshot is incomplete or stale")
    current_run_dir_text = str(current_run_context.get("run_dir", "")).strip()
    if not current_run_dir_text:
        raise ValueError("Current Vivado run directory is unavailable")
    current_run_dir = Path(current_run_dir_text).resolve()
    recorded_run_dir = Path(str(snapshot.get("directory", ""))).resolve()
    if current_run_dir != recorded_run_dir:
        raise ValueError("Artifact manifest run directory does not match the current Vivado run")
    current_top = str(current_run_context.get("run_top", "")).strip()
    current_part = str(current_run_context.get("project_part", "")).strip()
    if not current_top or not current_part:
        raise ValueError("Current Vivado run top or project part is unavailable")
    if str(snapshot.get("top", "")).strip() != current_top or str(snapshot.get("part", "")).strip() != current_part:
        raise ValueError("Artifact manifest top or part does not match the current Vivado run")
    current_expected_text = str(current_run_context.get("expected_bitstream_path", "")).strip()
    if not current_expected_text:
        raise ValueError("Current Vivado run did not report a canonical bitstream path")
    current_expected_bitstream = Path(current_expected_text).resolve()
    _assert_inside(current_run_dir, current_expected_bitstream, "Current canonical bitstream path escapes the run directory")
    if current_expected_bitstream.name != f"{current_top}.bit":
        raise ValueError("Current canonical bitstream filename does not match the run top")
    context_bitstreams = current_run_context.get("run_bitstream_files", [])
    if not isinstance(context_bitstreams, list) or [Path(str(item)).resolve() for item in context_bitstreams] != [current_expected_bitstream]:
        raise ValueError("Current Vivado run did not report exactly one canonical bitstream")
    if Path(str(snapshot.get("expected_bitstream_path", ""))).resolve() != current_expected_bitstream:
        raise ValueError("Artifact manifest canonical bitstream path does not match the current Vivado run")
    current_execution, execution_blockers = _run_execution_identity(current_run_dir)
    if execution_blockers:
        raise ValueError("Artifact manifest run execution markers are missing or incomplete")
    if not _write_bitstream_step_attested(current_run_context, current_execution):
        raise ValueError("Current Vivado write_bitstream step is explicitly disabled or lacks completed step markers")
    recorded_generation = str(snapshot.get("session_generation_id", ""))
    current_generation = str(current_run_context.get("session_generation_id", ""))
    if recorded_generation or current_generation:
        if not recorded_generation or recorded_generation != current_generation:
            raise ValueError("Artifact manifest session generation does not match the current Vivado session")
    recorded_execution = data.get("run_execution_identity")
    if not isinstance(recorded_execution, dict):
        raise ValueError("Artifact manifest run execution identity is missing")
    if current_execution != recorded_execution:
        raise ValueError("Artifact manifest run execution markers are missing, stale, or changed")
    recorded_design_identity = data.get("design_execution_identity")
    current_design_identity = current_design_execution_identity if isinstance(current_design_execution_identity, dict) else {}
    if (
        not isinstance(recorded_design_identity, dict)
        or recorded_design_identity.get("status") != "READY"
        or current_design_identity.get("status") != "READY"
        or str(recorded_design_identity.get("sha256", "")) != str(current_design_identity.get("sha256", ""))
        or recorded_design_identity != current_design_identity
    ):
        raise ValueError("SOURCE_CLOSURE_CHANGED: artifact manifest design execution identity does not match current RTL/XDC/include/run configuration closure")
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("Artifact manifest must contain artifacts")
    validated: list[dict[str, Any]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError("Artifact manifest entries must be JSON objects")
        export_path = Path(str(item.get("export_path", ""))).resolve()
        source_path = Path(str(item.get("source_path", ""))).resolve()
        _assert_inside(output_dir, export_path, "Artifact export path escapes output_dir")
        _assert_inside(current_run_dir, source_path, "Artifact source path escapes current run directory")
        if not export_path.is_file():
            raise ValueError(f"Artifact file is missing: {export_path}")
        if not source_path.is_file():
            raise ValueError(f"Artifact source file is missing: {source_path}")
        if int(item.get("size", -1)) != export_path.stat().st_size:
            raise ValueError(f"Artifact size mismatch: {export_path}")
        export_sha256 = sha256_file(export_path)
        source_sha256 = sha256_file(source_path)
        if str(item.get("sha256", "")).lower() != export_sha256:
            raise ValueError(f"Artifact SHA256 mismatch: {export_path}")
        if int(item.get("source_mtime_ns", -1)) != source_path.stat().st_mtime_ns:
            raise ValueError(f"Artifact source mtime mismatch: {source_path}")
        if str(item.get("source_sha256", "")).lower() != source_sha256:
            raise ValueError(f"Artifact source SHA256 mismatch: {source_path}")
        if source_sha256 != export_sha256:
            raise ValueError(f"Artifact source and export content differ: {source_path}")
        timing_blockers = _artifact_timing_blockers(
            {
                "source_path": str(source_path),
                "source_mtime_ns": source_path.stat().st_mtime_ns,
                "category": str(item.get("category", "")),
            },
            current_execution,
        )
        if timing_blockers:
            raise ValueError(timing_blockers[0])
        if str(item.get("category", "")) == "bitstream":
            if source_path != current_expected_bitstream:
                raise ValueError("Artifact manifest bitstream is not the current canonical run output")
            recorded_header = item.get("bitstream_header") if isinstance(item.get("bitstream_header"), dict) else None
            if recorded_header is None:
                raise ValueError("Artifact manifest bitstream header attestation is missing")
            source_header = parse_bitstream_header(source_path)
            export_header = parse_bitstream_header(export_path)
            if source_header != export_header or source_header != recorded_header:
                raise ValueError("Artifact bitstream header identity changed")
            header_blockers = _bitstream_header_blockers(source_header, top=current_top, part=current_part)
            if header_blockers:
                raise ValueError(header_blockers[0])
        validated.append(dict(item))
    bitstreams = [item for item in validated if str(item.get("category", "")) == "bitstream"]
    if len(bitstreams) != 1:
        raise ValueError("Artifact manifest must contain exactly one canonical bitstream")
    origin = data.get("bitstream_origin") if isinstance(data.get("bitstream_origin"), dict) else {}
    if origin.get("status") != "ATTESTED" or origin.get("policy") != "vivado_canonical_path_plus_unique_output_plus_xilinx_header":
        raise ValueError("Artifact manifest bitstream origin attestation is incomplete")
    result = dict(data)
    result["manifest_path"] = str(manifest)
    result["artifacts"] = validated
    return result


def classify_artifact(path: str | Path) -> str | None:
    return ARTIFACT_CATEGORIES.get(Path(path).suffix.lower())


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_bitstream_header(path: str | Path) -> dict[str, Any]:
    candidate = Path(path)
    before = file_identity(candidate)
    details = os.lstat(candidate)
    if not stat.S_ISREG(details.st_mode) or is_reparse_point(candidate, details) or details.st_nlink != 1:
        raise ValueError("bitstream must be a single-link regular non-reparse file")
    with candidate.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        opened_identity = (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size, opened.st_mtime_ns)
        if opened_identity != before:
            raise ValueError("bitstream identity changed before header read")
        preamble_length = _read_uint(handle, 2)
        if not 1 <= preamble_length <= MAX_BITSTREAM_HEADER_FIELD_BYTES:
            raise ValueError("bitstream preamble length is invalid")
        _read_exact(handle, preamble_length)
        if _read_uint(handle, 2) != 1:
            raise ValueError("bitstream header separator is invalid")
        fields: dict[str, str] = {}
        for expected_key in ("a", "b", "c", "d"):
            key = _read_exact(handle, 1).decode("ascii", errors="strict")
            if key != expected_key:
                raise ValueError(f"bitstream header field order is invalid: expected {expected_key}, got {key}")
            field_length = _read_uint(handle, 2)
            if not 1 <= field_length <= MAX_BITSTREAM_HEADER_FIELD_BYTES:
                raise ValueError(f"bitstream header field {key} length is invalid")
            fields[key] = _read_exact(handle, field_length).rstrip(b"\x00").decode("ascii", errors="strict")
        if _read_exact(handle, 1) != b"e":
            raise ValueError("bitstream payload field is missing")
        payload_length = _read_uint(handle, 4)
        payload_offset = handle.tell()
        if payload_length <= 0 or payload_offset + payload_length != opened.st_size:
            raise ValueError("bitstream payload length does not match the file size")
        after_open = os.fstat(handle.fileno())
    after_identity = (after_open.st_dev, after_open.st_ino, after_open.st_mode, after_open.st_size, after_open.st_mtime_ns)
    if after_identity != before or file_identity(candidate) != before:
        raise ValueError("bitstream identity changed during header read")
    return {
        "format": "xilinx_bit_v1",
        "design": fields["a"],
        "part": fields["b"],
        "date": fields["c"],
        "time": fields["d"],
        "payload_offset": payload_offset,
        "payload_size": payload_length,
        "file_size": opened.st_size,
    }


def _read_exact(handle, size: int) -> bytes:
    content = handle.read(size)
    if len(content) != size:
        raise ValueError("bitstream header is truncated")
    return content


def _read_uint(handle, size: int) -> int:
    formats = {2: ">H", 4: ">I"}
    return int(struct.unpack(formats[size], _read_exact(handle, size))[0])


def _bitstream_header_blockers(header: dict[str, Any], *, top: str, part: str) -> list[str]:
    blockers: list[str] = []
    design = str(header.get("design", "")).split(";", 1)[0].strip()
    design = re.sub(r"\.(?:ncd|dcp)$", "", design, flags=re.IGNORECASE)
    if not top or design.casefold() != top.casefold():
        blockers.append(f"bitstream header design does not match run top: {design!r} != {top!r}")
    header_part = _normalize_xilinx_part(str(header.get("part", "")))
    expected_part = _normalize_xilinx_part(part)
    if not expected_part or header_part != expected_part:
        blockers.append(f"bitstream header part does not match project part: {header.get('part', '')!r} != {part!r}")
    return blockers


def _normalize_xilinx_part(value: str) -> str:
    normalized = value.strip().lower().split(";", 1)[0]
    if normalized.startswith(("xc", "xa", "xq")):
        normalized = normalized[2:]
    return re.sub(r"-\d+[a-z]*$", "", normalized)


def _unique_destination(directory: Path, filename: str) -> Path:
    destination = directory / filename
    if not destination.exists():
        return destination
    stem = destination.stem
    suffix = destination.suffix
    index = 2
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _assert_inside(root: Path, target: Path, message: str) -> None:
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(message) from exc


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() not in {"", "0", "false", "no"}


def _write_bitstream_step_attested(context: dict[str, Any], execution: dict[str, Any]) -> bool:
    value = str(context.get("write_bitstream_step_enabled", "")).strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value:
        return False
    return (
        execution.get("marker_source") == "step_markers"
        and isinstance(execution.get("write_bitstream_started_mtime_ns"), int)
        and isinstance(execution.get("write_bitstream_ended_mtime_ns"), int)
    )


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_execution_identity(run_dir: Path) -> tuple[dict[str, Any], list[str]]:
    identity: dict[str, Any] = {"run_dir": str(run_dir), "marker_source": "", "markers": {}}
    blockers: list[str] = []
    step_markers: dict[str, dict[str, Any]] = {}
    for begin_marker in sorted(run_dir.glob(".*.begin.rst")):
        step = begin_marker.name[1 : -len(".begin.rst")]
        if step.lower() == "vivado":
            continue
        end_marker = run_dir / f".{step}.end.rst"
        pair = {"started": _marker_identity(begin_marker)}
        if end_marker.is_file():
            pair["ended"] = _marker_identity(end_marker)
        else:
            blockers.append(f"Vivado run step completion marker is missing: {end_marker}")
        step_markers[step] = pair

    complete_steps = [pair for pair in step_markers.values() if "ended" in pair]
    if complete_steps:
        identity["marker_source"] = "step_markers"
        identity["markers"] = step_markers
        identity["started_mtime_ns"] = min(int(pair["started"]["mtime_ns"]) for pair in complete_steps)
        identity["ended_mtime_ns"] = max(int(pair["ended"]["mtime_ns"]) for pair in complete_steps)
        write_pair = step_markers.get("write_bitstream", {})
        if "started" in write_pair:
            identity["write_bitstream_started_mtime_ns"] = int(write_pair["started"]["mtime_ns"])
        if "ended" in write_pair:
            identity["write_bitstream_ended_mtime_ns"] = int(write_pair["ended"]["mtime_ns"])
            identity["ended_mtime_ns"] = int(write_pair["ended"]["mtime_ns"])
        elif "started" in write_pair:
            blockers.append("Vivado write_bitstream completion marker is missing")
    else:
        identity["marker_source"] = "vivado_fallback"
        for label, name in (("started", ".vivado.begin.rst"), ("ended", ".vivado.end.rst")):
            marker = run_dir / name
            if not marker.is_file():
                blockers.append(f"Vivado run {label} marker is missing: {marker}")
                continue
            marker_data = _marker_identity(marker)
            identity["markers"][label] = marker_data
            identity[f"{label}_mtime_ns"] = marker_data["mtime_ns"]
    started = identity.get("started_mtime_ns")
    ended = identity.get("ended_mtime_ns")
    if isinstance(started, int) and isinstance(ended, int) and ended < started:
        blockers.append("Vivado run completion marker predates the launch marker")
    return identity, blockers


def _marker_identity(marker: Path) -> dict[str, Any]:
    return {
        "path": str(marker),
        "size": marker.stat().st_size,
        "mtime_ns": marker.stat().st_mtime_ns,
        "sha256": sha256_file(marker),
    }


def _artifact_timing_blockers(artifact: dict[str, Any], execution: dict[str, Any]) -> list[str]:
    source_path = str(artifact.get("source_path", ""))
    source_mtime_ns = int(artifact.get("source_mtime_ns", -1))
    started_ns = int(execution.get("started_mtime_ns", -1))
    ended_ns = int(execution.get("ended_mtime_ns", -1))
    blockers: list[str] = []
    if started_ns >= 0 and source_mtime_ns < started_ns:
        blockers.append(f"artifact predates current run launch marker: {source_path}")
    if ended_ns >= 0 and source_mtime_ns > ended_ns + ARTIFACT_COMPLETION_MTIME_TOLERANCE_NS:
        blockers.append(f"artifact is newer than current run completion marker: {source_path}")

    if str(artifact.get("category", "")) == "bitstream":
        write_started_ns = int(execution.get("write_bitstream_started_mtime_ns", -1))
        write_ended_ns = int(execution.get("write_bitstream_ended_mtime_ns", -1))
        if write_started_ns >= 0 and source_mtime_ns < write_started_ns:
            blockers.append(f"bitstream predates current write_bitstream launch marker: {source_path}")
        if write_ended_ns >= 0 and source_mtime_ns > write_ended_ns + ARTIFACT_COMPLETION_MTIME_TOLERANCE_NS:
            blockers.append(f"bitstream is newer than current write_bitstream completion marker: {source_path}")
    return blockers
