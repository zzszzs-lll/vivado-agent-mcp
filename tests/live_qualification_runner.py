from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


_WORKSPACE = Path(__file__).resolve().parents[1]
_SOURCE_IMPORT_DISABLED = "--release-wheel" in sys.argv
if not _SOURCE_IMPORT_DISABLED:
    _SRC = str(_WORKSPACE / "src")
    if _SRC not in sys.path:
        sys.path.insert(0, _SRC)

from vivado_agent_mcp.qualification import (  # noqa: E402
    build_qualification_record,
    qualification_fixture_manifest,
    update_qualification_matrix,
    validate_published_qualification_bundle,
    validate_qualification_record,
    write_public_qualification_evidence,
)
from vivado_agent_mcp.release_identity import sha256_file, source_identity  # noqa: E402


DEFAULT_PART = "xc7a35tcpg236-1"
QUALIFICATION_SCENARIO_ID = "S01"
QUALIFICATION_POLICY_VERSION = "vivado-policy-v1"
QUALIFICATION_WORKFLOW_VERSION = "live-qualification-v1"
QUALIFICATION_SCENARIO_VERSION = "agent-scenario-v1"
_HEX_32 = re.compile(r"[0-9a-f]{32}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Produce a fail-closed commit-bound live Vivado qualification record from the exact-wheel S01 MCP stdio flow.",
    )
    parser.add_argument("--workspace", default=str(_WORKSPACE), help="Git repository top-level used to build the exact package.")
    parser.add_argument("--output-dir", default="test_use/live_qualification", help="Qualification evidence output root.")
    parser.add_argument("--python", default=sys.executable, help="Clean venv Python containing the exact release wheel.")
    parser.add_argument("--release-wheel", required=True, help="Exact wheel already verified by clean-install smoke.")
    parser.add_argument("--release-manifest", required=True, help="clean_install_smoke_result.json for the exact wheel.")
    parser.add_argument("--source-provenance-manifest", required=True, help="source-provenance.json from immutable distribution build.")
    parser.add_argument("--matrix-template", default="qualification/matrix.json", help="Tracked qualification matrix template.")
    parser.add_argument("--include-live-vivado", action="store_true", help="Authorize the existing S01 MCP stdio live Project Mode flow.")
    parser.add_argument("--require-qualified", action="store_true", help="Return non-zero unless the result is qualified.")
    parser.add_argument("--allow-external-output-dir", action="store_true")
    parser.add_argument("--part", default=DEFAULT_PART)
    parser.add_argument("--vivado-timeout-s", type=int, default=240)
    parser.add_argument("--poll-timeout-s", type=int, default=1800)
    parser.add_argument("--poll-interval-s", type=int, default=10)
    parser.add_argument("--runner-class", default="self-hosted-windows-vivado-2021.2")
    parser.add_argument("--evidence-run-id", default="", help="Shared 32-hex nonce; generated when omitted.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    output_root = _resolve_output_root(
        workspace,
        Path(args.output_dir).expanduser(),
        allow_external=bool(args.allow_external_output_dir),
    )
    run_dir = output_root / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=False)
    started_at = _now()
    evidence_run_id = str(args.evidence_run_id or uuid.uuid4().hex).lower()
    if not _HEX_32.fullmatch(evidence_run_id):
        parser.error("--evidence-run-id must be exactly 32 hexadecimal characters")

    source_provenance_path = _resolve_existing_file(workspace, args.source_provenance_manifest)
    release_manifest_path = _resolve_existing_file(workspace, args.release_manifest)
    wheel_path = _resolve_existing_file(workspace, args.release_wheel)
    matrix_path = _resolve_existing_file(workspace, args.matrix_template)
    source_provenance = _load_json(source_provenance_path)
    source_provenance["source_identity"] = source_identity(workspace)
    release_manifest = _load_json(release_manifest_path)
    matrix = _load_json(matrix_path)
    harness = verify_qualification_harness(release_manifest, workspace)

    scenario_result: dict[str, Any] = {}
    scenario_result_path = run_dir / "scenario_not_run.json"
    scenario_result_path.write_text("{}\n", encoding="utf-8")
    command: list[str] = []
    command_result: dict[str, Any] = {"returncode": None, "stdout_tail": "", "stderr_tail": ""}

    if args.include_live_vivado and not os.environ.get("VIVADO_PATH"):
        scenario_result = {"qualification_unavailable": True, "reason_code": "QUALIFICATION_VIVADO_UNAVAILABLE"}
        scenario_result_path.write_text(json.dumps(scenario_result, indent=2), encoding="utf-8")
    elif args.include_live_vivado and harness["ok"]:
        command = qualification_scenario_command(
            python_exe=Path(args.python).expanduser().resolve(),
            workspace=workspace,
            output_dir=run_dir / "scenario",
            wheel_path=wheel_path,
            release_manifest_path=release_manifest_path,
            evidence_run_id=evidence_run_id,
            part=str(args.part),
            vivado_timeout_s=max(1, int(args.vivado_timeout_s)),
            poll_timeout_s=max(1, int(args.poll_timeout_s)),
            poll_interval_s=max(1, int(args.poll_interval_s)),
        )
        try:
            completed = subprocess.run(
                command,
                cwd=str(workspace),
                env=_qualification_environment(run_dir),
                capture_output=True,
                text=True,
                timeout=max(900, int(args.poll_timeout_s) * 4),
                check=False,
            )
            command_result = {
                "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-4000:],
                "stderr_tail": completed.stderr[-4000:],
            }
            summary = _last_json_object(completed.stdout)
            candidate = Path(str(summary.get("result_path", ""))).expanduser()
            if candidate.is_file():
                scenario_result_path = candidate.resolve()
                scenario_result = _load_json(scenario_result_path)
            else:
                scenario_result = {
                    "qualification_interrupted": True,
                    "reason_code": "QUALIFICATION_SCENARIO_RESULT_MISSING",
                }
                scenario_result_path = run_dir / "scenario_result_missing.json"
                scenario_result_path.write_text(json.dumps(scenario_result, indent=2), encoding="utf-8")
        except subprocess.TimeoutExpired as exc:
            command_result = {
                "returncode": None,
                "stdout_tail": _text_tail(exc.stdout),
                "stderr_tail": _text_tail(exc.stderr),
            }
            scenario_result = {
                "qualification_interrupted": True,
                "reason_code": "QUALIFICATION_SCENARIO_TIMEOUT",
            }
            scenario_result_path = run_dir / "scenario_timeout.json"
            scenario_result_path.write_text(json.dumps(scenario_result, indent=2), encoding="utf-8")
    elif args.include_live_vivado:
        scenario_result = {
            "qualification_interrupted": True,
            "reason_code": "QUALIFICATION_VALIDATION_HARNESS_MISMATCH",
        }
        scenario_result_path = run_dir / "harness_mismatch.json"
        scenario_result_path.write_text(json.dumps(scenario_result, indent=2), encoding="utf-8")

    record = build_qualification_record_from_run(
        source_provenance=source_provenance,
        release_manifest=release_manifest,
        scenario_result=scenario_result,
        scenario_result_path=scenario_result_path,
        live_requested=bool(args.include_live_vivado),
        generated_at=_now(),
        started_at=started_at,
        runner_class=str(args.runner_class),
        public_evidence_dir=run_dir / "public-evidence",
    )
    expected = _expected_identity(source_provenance, wheel_path, workspace=workspace)
    validation = validate_qualification_record(record, expected=expected)
    matrix_update = update_qualification_matrix(matrix, record) if validation["ok"] else {
        "ok": False,
        "status": "BLOCK",
        "reason_code": "QUALIFICATION_RECORD_INVALID",
        "reason_codes": validation["reason_codes"],
        "matrix": matrix,
    }

    record_path = run_dir / "qualification-record.json"
    validation_path = run_dir / "qualification-validation.json"
    matrix_output_path = run_dir / "qualification-matrix.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    publication_validation = (
        validate_published_qualification_bundle(record_path)
        if record["qualification_status"] == "qualified"
        else {
            "ok": True,
            "status": "NOT_APPLICABLE",
            "reason_code": "PUBLIC_QUALIFICATION_BUNDLE_NOT_REQUIRED",
            "issues": [],
        }
    )
    validation["published_evidence"] = publication_validation
    validation_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    matrix_output_path.write_text(json.dumps(matrix_update["matrix"], ensure_ascii=False, indent=2), encoding="utf-8")
    report = {
        "ok": validation["ok"] and matrix_update["ok"] and publication_validation["ok"],
        "status": record["qualification_status"],
        "qualified": record["qualification_status"] == "qualified" and validation["ok"] and publication_validation["ok"],
        "record_id": record["record_id"],
        "record_path": str(record_path),
        "validation_path": str(validation_path),
        "public_evidence_dir": str(run_dir / "public-evidence") if record["qualification_status"] == "qualified" else "",
        "matrix_path": str(matrix_output_path),
        "scenario_result_path": str(scenario_result_path),
        "source_provenance_path": str(source_provenance_path),
        "release_manifest_path": str(release_manifest_path),
        "wheel_path": str(wheel_path),
        "wheel_sha256": sha256_file(wheel_path),
        "validation_harness": harness,
        "command": command,
        "command_result": command_result,
        "hardware_validation": record["hardware_validation"],
    }
    summary_path = run_dir / "qualification-summary.json"
    report["summary_path"] = str(summary_path)
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.json else None))
    if not report["ok"] or (args.require_qualified and not report["qualified"]):
        return 2
    return 0


def build_qualification_record_from_run(
    *,
    source_provenance: dict[str, Any],
    release_manifest: dict[str, Any],
    scenario_result: dict[str, Any],
    scenario_result_path: Path,
    live_requested: bool,
    generated_at: str,
    started_at: str,
    runner_class: str,
    public_evidence_dir: Path | None = None,
) -> dict[str, Any]:
    source_identity = _mapping(source_provenance.get("source_identity"))
    source_snapshot = _mapping(source_provenance.get("source_snapshot"))
    release_source = _mapping(release_manifest.get("source_identity"))
    package_data = _mapping(source_provenance.get("package"))
    wheel = _mapping(source_provenance.get("wheel"))
    sdist = _mapping(source_provenance.get("sdist"))
    scenario = _select_s01(scenario_result)
    scenario_document_matches = _scenario_document_matches(scenario_result, scenario_result_path)
    vivado = _vivado_identity(scenario)
    fixture = _fixture_identity(scenario)
    evidence, evidence_complete, evidence_publication_failure = collect_qualification_evidence(
        scenario_result_path,
        scenario,
        public_evidence_dir=public_evidence_dir or scenario_result_path.parent / "public-evidence",
        source_commit=str(source_identity.get("commit", "")),
    )
    release_provenance_ok = _release_provenance_matches(source_provenance, release_manifest)
    scenario_provenance_ok = _scenario_provenance_matches(
        scenario_result,
        source_provenance=source_provenance,
        release_manifest=release_manifest,
    )
    hardware_ok = _hardware_boundary_ok(scenario_result, scenario, source_provenance, release_manifest)
    live_pass = bool(
        live_requested
        and scenario.get("status") == "PASS"
        and scenario.get("executed") is True
        and scenario.get("full_scenario_coverage") is True
        and vivado.get("identity_status") == "VERIFIED"
        and fixture.get("id") == qualification_fixture_manifest()["id"]
        and evidence_complete
        and scenario_document_matches
        and release_provenance_ok
        and scenario_provenance_ok
        and hardware_ok
    )

    if not live_requested:
        qualification_status = "unvalidated"
        terminal_status = "SKIPPED"
        reason_code = "QUALIFICATION_LIVE_VIVADO_NOT_REQUESTED"
        message = "Live Vivado execution was not requested; no qualification claim was produced."
    elif scenario_result.get("qualification_unavailable"):
        qualification_status = "unvalidated"
        terminal_status = "UNAVAILABLE"
        reason_code = str(scenario_result.get("reason_code", "QUALIFICATION_VIVADO_UNAVAILABLE"))
        message = "Trusted live Vivado execution is unavailable in this runner environment."
    elif scenario_result.get("qualification_interrupted"):
        qualification_status = "rejected"
        terminal_status = "INTERRUPTED"
        reason_code = str(scenario_result.get("reason_code", "QUALIFICATION_INTERRUPTED"))
        message = "Live qualification was interrupted before complete evidence was produced."
    elif not scenario_document_matches:
        qualification_status = "rejected"
        terminal_status = "BLOCK"
        reason_code = "QUALIFICATION_SCENARIO_DOCUMENT_MISMATCH"
        message = "Parsed S01 evidence does not match the hashed scenario result document."
    elif not release_provenance_ok:
        qualification_status = "rejected"
        terminal_status = "BLOCK"
        reason_code = "QUALIFICATION_RELEASE_PROVENANCE_MISMATCH"
        message = "Source, exact wheel, and release evidence identities do not match."
    elif not scenario_provenance_ok:
        qualification_status = "rejected"
        terminal_status = "BLOCK"
        reason_code = "QUALIFICATION_SCENARIO_PROVENANCE_MISMATCH"
        message = "S01 evidence is not bound to the exact release wheel/source identity and evidence run nonce."
    elif not hardware_ok:
        qualification_status = "rejected"
        terminal_status = "BLOCK"
        reason_code = "QUALIFICATION_HARDWARE_BOUNDARY_VIOLATION"
        message = "Live evidence attempted to claim hardware validation outside the no-board qualification scope."
    elif not evidence_complete:
        qualification_status = "rejected"
        terminal_status = "BLOCK"
        if evidence_publication_failure:
            reason_code = str(
                evidence_publication_failure.get("reason_code", "PUBLIC_EVIDENCE_PUBLICATION_FAILED")
            )
            evidence_type = str(evidence_publication_failure.get("evidence_type", ""))
            detail = str(evidence_publication_failure.get("detail", ""))
            message = "Public evidence publication failed."
            if evidence_type:
                message += f" evidence_type={evidence_type}."
            if detail:
                message += f" detail={detail}"
        else:
            reason_code = "QUALIFICATION_LIVE_EVIDENCE_INCOMPLETE"
            message = "Live flow did not produce every required fresh artifact/report/audit/diagnostic evidence digest."
    elif live_pass:
        qualification_status = "qualified"
        terminal_status = "PASS"
        reason_code = "QUALIFICATION_COMPLETE"
        message = "Commit-bound live Vivado Project Mode software qualification completed."
    else:
        qualification_status = "rejected"
        terminal_status = "BLOCK"
        reason_code = "QUALIFICATION_LIVE_FLOW_REJECTED"
        message = "Live Project Mode flow did not satisfy every qualification gate."

    software_status = qualification_status.upper()
    return build_qualification_record(
        qualification_status=qualification_status,
        terminal_status=terminal_status,
        reason_code=reason_code,
        message=message,
        started_at=started_at,
        ended_at=generated_at,
        generated_at=generated_at,
        source={
            "commit": str(source_identity.get("commit", "")),
            "tree": str(source_identity.get("tree", "")),
            "dirty": source_identity.get("clean") is not True,
            "tracked_digest": str(source_identity.get("tracked_digest", "")),
            "source_archive_sha256": str(source_snapshot.get("archive_sha256", "")),
        },
        package={
            "name": str(package_data.get("name", "vivado-agent-mcp")),
            "version": str(package_data.get("version", "")),
            "wheel": {"name": str(wheel.get("name", "")), "sha256": str(wheel.get("sha256", ""))},
            "sdist": {"name": str(sdist.get("name", "")), "sha256": str(sdist.get("sha256", ""))},
            "source_wheel_pair_id": str(release_manifest.get("source_wheel_pair_id", "")),
            "provenance_verified": release_provenance_ok,
        },
        vivado=vivado,
        runner=_runner_identity(runner_class),
        fixture=fixture,
        execution={
            "tool_profile": "core",
            "policy_version": QUALIFICATION_POLICY_VERSION,
            "workflow_version": QUALIFICATION_WORKFLOW_VERSION,
            "scenario_id": QUALIFICATION_SCENARIO_ID,
            "scenario_version": QUALIFICATION_SCENARIO_VERSION,
        },
        evidence=evidence,
        software_validation={
            "status": software_status,
            "validated": qualification_status in {"qualified", "compatible"},
            "scope": "no_board_project_mode_software_flow",
        },
        hardware_validation={
            "status": "NOT_VALIDATED",
            "validated": False,
            "scope": "real_fpga_jtag_programming_runtime",
        },
    )


def collect_qualification_evidence(
    scenario_result_path: Path,
    scenario: dict[str, Any],
    *,
    public_evidence_dir: Path,
    source_commit: str,
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    allowed_root = scenario_result_path.resolve().parent
    partial = _mapping(scenario.get("partial_handoff"))
    artifact_path = _bounded_file(partial.get("artifact_manifest_path"), allowed_root)
    report_path = _bounded_file(partial.get("report_manifest_path"), allowed_root)
    diagnostic_path = _bounded_file(partial.get("diagnostic_manifest_path"), allowed_root)
    audit_path = _verified_audit_path(diagnostic_path, allowed_root)
    raw_paths = {
        "scenario_result": _bounded_file(scenario_result_path, allowed_root),
        "artifact_manifest": artifact_path,
        "report_manifest": report_path,
        "audit_result": audit_path,
        "diagnostic_manifest": diagnostic_path,
    }
    entries = {name: _evidence_entry(path) for name, path in raw_paths.items()}
    complete = all(_entry_complete(entry) for entry in entries.values())
    if complete:
        published = write_public_qualification_evidence(
            {name: path for name, path in raw_paths.items() if path is not None},
            public_evidence_dir,
            source_commit=source_commit,
            hardware_validation={
                "status": "NOT_VALIDATED",
                "validated": False,
                "scope": "real_fpga_jtag_programming_runtime",
            },
        )
        if published["ok"]:
            return published["evidence"], True, {}
        publication_failure = {
            key: published[key]
            for key in ("status", "reason_code", "evidence_type", "detail")
            if key in published
        }
        complete = False
    else:
        publication_failure = {}
    normalized_payload = {
        "scenario": {
            "id": scenario.get("id"),
            "status": scenario.get("status"),
            "executed": scenario.get("executed"),
            "full_scenario_coverage": scenario.get("full_scenario_coverage"),
            "handoff_status": scenario.get("handoff_status"),
            "handoff_ready": scenario.get("handoff_ready"),
            "handoff_reviewable": scenario.get("handoff_reviewable"),
            "hardware_validation_status": scenario.get("hardware_validation_status"),
            "checks": scenario.get("checks", {}),
        },
        "evidence": entries,
    }
    return {
        "freshness": "FRESH" if complete else "MISSING",
        "normalized_evidence_sha256": hashlib.sha256(_canonical_json(normalized_payload)).hexdigest() if complete else "",
        **entries,
    }, complete, publication_failure


def qualification_scenario_command(
    *,
    python_exe: Path,
    workspace: Path,
    output_dir: Path,
    wheel_path: Path,
    release_manifest_path: Path,
    evidence_run_id: str,
    part: str,
    vivado_timeout_s: int,
    poll_timeout_s: int,
    poll_interval_s: int,
) -> list[str]:
    return [
        str(python_exe.resolve()),
        str((workspace / "tests" / "agent_scenario_runner.py").resolve()),
        "--scenarios",
        QUALIFICATION_SCENARIO_ID,
        "--include-live-vivado",
        "--output-dir",
        str(output_dir.resolve()),
        "--allow-external-output-dir",
        "--python",
        str(python_exe.resolve()),
        "--release-wheel",
        str(wheel_path.resolve()),
        "--release-manifest",
        str(release_manifest_path.resolve()),
        "--evidence-run-id",
        evidence_run_id,
        "--part",
        part,
        "--vivado-timeout-s",
        str(vivado_timeout_s),
        "--poll-timeout-s",
        str(poll_timeout_s),
        "--poll-interval-s",
        str(poll_interval_s),
    ]


def verify_qualification_harness(release_manifest: dict[str, Any], workspace: Path) -> dict[str, Any]:
    harness = _mapping(release_manifest.get("validation_harness"))
    entries = harness.get("files") if isinstance(harness.get("files"), list) else []
    expected = "tests/live_qualification_runner.py"
    entry = next((item for item in entries if isinstance(item, dict) and item.get("path") == expected), None)
    path = (workspace / expected).resolve()
    ok = bool(
        harness.get("status") == "READY"
        and isinstance(entry, dict)
        and path.is_file()
        and int(entry.get("size", -1)) == path.stat().st_size
        and str(entry.get("sha256", "")) == sha256_file(path)
    )
    return {
        "ok": ok,
        "status": "READY" if ok else "BLOCK",
        "reason_code": "" if ok else "QUALIFICATION_VALIDATION_HARNESS_MISMATCH",
        "path": expected,
        "sha256": sha256_file(path) if path.is_file() else "",
    }


def _release_provenance_matches(source_provenance: dict[str, Any], release_manifest: dict[str, Any]) -> bool:
    source = _mapping(source_provenance.get("source_identity"))
    release_source = _mapping(release_manifest.get("source_identity"))
    wheel = _mapping(source_provenance.get("wheel"))
    release_wheel = _mapping(release_manifest.get("wheel"))
    source_hardware = _mapping(source_provenance.get("hardware_validation"))
    release_hardware = _mapping(release_manifest.get("hardware_validation"))
    source_snapshot = _mapping(source_provenance.get("source_snapshot"))
    sdist = _mapping(source_provenance.get("sdist"))
    source_package = _mapping(source_provenance.get("package"))
    release_package = _mapping(release_manifest.get("package"))
    return bool(
        source_provenance.get("status") == "PASS"
        and source_provenance.get("source_wheel_provenance_verified") is True
        and source.get("clean") is True
        and release_source.get("clean") is True
        and all(source.get(key) == release_source.get(key) for key in ("commit", "tree", "tracked_digest"))
        and wheel.get("sha256") == release_wheel.get("sha256")
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(source_snapshot.get("archive_sha256", ""))))
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(sdist.get("sha256", ""))))
        and source_package.get("name") == release_package.get("name") == "vivado-agent-mcp"
        and source_package.get("version") == release_package.get("version")
        and release_manifest.get("wheel_sha256_verified") is True
        and release_manifest.get("source_wheel_provenance_verified") is True
        and release_manifest.get("release_evidence_ready") is True
        and bool(release_manifest.get("source_wheel_pair_id"))
        and release_manifest.get("source_wheel_pair_id") == release_manifest.get("release_evidence_id")
        and source_hardware.get("status") == "NOT_VALIDATED"
        and source_hardware.get("validated") is False
        and release_hardware.get("status") == "NOT_VALIDATED"
        and release_hardware.get("validated") is False
    )


def _scenario_provenance_matches(
    scenario_result: dict[str, Any],
    *,
    source_provenance: dict[str, Any],
    release_manifest: dict[str, Any],
) -> bool:
    if not scenario_result or scenario_result.get("qualification_unavailable") or scenario_result.get("qualification_interrupted"):
        return False
    scenario_source = _mapping(scenario_result.get("source_identity"))
    package_provenance = _mapping(scenario_result.get("package_provenance"))
    source = _mapping(source_provenance.get("source_identity"))
    wheel = _mapping(source_provenance.get("wheel"))
    release_run_id = str(release_manifest.get("evidence_run_id", ""))
    scenario_run_id = str(scenario_result.get("evidence_run_id", ""))
    return bool(
        all(scenario_source.get(key) == source.get(key) for key in ("commit", "tree", "tracked_digest"))
        and _mapping(package_provenance.get("source_identity")).get("commit") == source.get("commit")
        and package_provenance.get("wheel_sha256") == wheel.get("sha256")
        and package_provenance.get("release_evidence_id") == release_manifest.get("release_evidence_id")
        and bool(_HEX_32.fullmatch(release_run_id))
        and scenario_run_id == release_run_id
    )


def _hardware_boundary_ok(
    scenario_result: dict[str, Any],
    scenario: dict[str, Any],
    source_provenance: dict[str, Any],
    release_manifest: dict[str, Any],
) -> bool:
    return bool(
        scenario_result.get("hardware_validation_status", "NOT_VALIDATED") == "NOT_VALIDATED"
        and scenario.get("hardware_validation_status", "NOT_VALIDATED") == "NOT_VALIDATED"
        and _mapping(scenario.get("partial_handoff")).get("hardware_validation_status", "NOT_VALIDATED")
        == "NOT_VALIDATED"
        and _mapping(source_provenance.get("hardware_validation")).get("status") == "NOT_VALIDATED"
        and _mapping(release_manifest.get("hardware_validation")).get("status") == "NOT_VALIDATED"
    )


def _vivado_identity(scenario: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(_mapping(scenario.get("vivado_environment")))
    return {
        "identity_status": str(value.get("identity_status", "UNAVAILABLE")),
        "canonical_path_sha256": str(value.get("canonical_path_sha256", "")),
        "executable_sha256": str(value.get("executable_sha256", "")),
        "file_identity": deepcopy(_mapping(value.get("file_identity"))),
        "version": str(value.get("version", "")),
        "full_version": str(value.get("full_version", "")),
        "build": str(value.get("build", "")),
        "version_attested": value.get("version_attested") is True,
    }


def _fixture_identity(scenario: dict[str, Any]) -> dict[str, Any]:
    value = _mapping(scenario.get("qualification_fixture"))
    return deepcopy(value) if value else qualification_fixture_manifest()


def _runner_identity(runner_class: str) -> dict[str, Any]:
    public = {
        "os": platform.system() or "unknown",
        "os_version": platform.version() or "unknown",
        "architecture": platform.machine() or "unknown",
        "python_version": platform.python_version(),
        "runner_class": runner_class,
    }
    private_identity = {**public, "node": platform.node(), "python_executable": str(Path(sys.executable).resolve())}
    return {**public, "identity_sha256": hashlib.sha256(_canonical_json(private_identity)).hexdigest()}


def _expected_identity(source_provenance: dict[str, Any], wheel_path: Path, *, workspace: Path) -> dict[str, str]:
    sdist = _mapping(source_provenance.get("sdist"))
    sdist_path = Path(str(sdist.get("path", ""))).expanduser()
    if not sdist_path.is_absolute():
        sdist_path = workspace / sdist_path
    actual_sdist_sha256 = sha256_file(sdist_path) if sdist_path.is_file() else ""
    return {
        "commit": _git_text(workspace, "rev-parse", "HEAD"),
        "source_archive_sha256": _git_archive_sha256(workspace),
        "wheel_sha256": sha256_file(wheel_path),
        "sdist_sha256": actual_sdist_sha256,
        "fixture_sha256": qualification_fixture_manifest()["aggregate_sha256"],
    }


def _git_archive_sha256(workspace: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), "archive", "--format=tar", "HEAD"],
        capture_output=True,
        timeout=120,
        check=False,
    )
    return hashlib.sha256(completed.stdout).hexdigest() if completed.returncode == 0 else ""


def _git_text(workspace: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _select_s01(result: dict[str, Any]) -> dict[str, Any]:
    scenarios = result.get("scenario_results") if isinstance(result.get("scenario_results"), list) else []
    return next((item for item in scenarios if isinstance(item, dict) and item.get("id") == QUALIFICATION_SCENARIO_ID), {})


def _scenario_document_matches(result: dict[str, Any], path: Path) -> bool:
    try:
        return _load_json(path.resolve()) == result
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _bounded_evidence_entry(value: Any, allowed_root: Path) -> dict[str, Any]:
    return _evidence_entry(_bounded_file(value, allowed_root))


def _bounded_file(value: Any, allowed_root: Path) -> Path | None:
    if not value:
        return None
    candidate = Path(str(value)).expanduser().resolve()
    try:
        candidate.relative_to(allowed_root.resolve())
    except ValueError:
        return None
    if not candidate.is_file() or candidate.is_symlink():
        return None
    return candidate


def _verified_audit_path(diagnostic_path: Path | None, allowed_root: Path) -> Path | None:
    if diagnostic_path is None:
        return None
    try:
        manifest = _load_json(diagnostic_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    entry = next((item for item in files if isinstance(item, dict) and item.get("category") == "audit"), None)
    if not isinstance(entry, dict):
        return None
    raw_path = Path(str(entry.get("path", "")))
    candidate_value = raw_path if raw_path.is_absolute() else diagnostic_path.parent / raw_path
    candidate = _bounded_file(candidate_value, allowed_root)
    if candidate is None:
        return None
    if candidate.stat().st_size != int(entry.get("size", -1)) or sha256_file(candidate) != str(entry.get("sha256", "")):
        return None
    return candidate


def _evidence_entry(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"size": 0, "sha256": ""}
    return {"size": path.stat().st_size, "sha256": sha256_file(path)}


def _entry_complete(entry: dict[str, Any]) -> bool:
    return int(entry.get("size", 0)) > 0 and bool(re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))))


def _resolve_output_root(workspace: Path, requested: Path, *, allow_external: bool) -> Path:
    root = requested.resolve() if requested.is_absolute() else (workspace / requested).resolve()
    if allow_external:
        return root
    allowed = (workspace / "test_use").resolve()
    try:
        relative = root.relative_to(allowed)
    except ValueError as exc:
        raise ValueError("qualification output must stay under workspace/test_use") from exc
    if not relative.parts:
        raise ValueError("qualification output must be a dedicated descendant of workspace/test_use")
    return root


def _resolve_existing_file(workspace: Path, value: str) -> Path:
    path = Path(value).expanduser()
    resolved = path.resolve() if path.is_absolute() else (workspace / path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _qualification_environment(run_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("SystemRoot", r"C:\WINDOWS")
    env.setdefault("WINDIR", r"C:\WINDOWS")
    env["VIVADO_AGENT_MCP_RUNTIME_DIR"] = str((run_dir / "runtime").resolve())
    env["VIVADO_AGENT_MCP_TRUSTED_PROJECT_ROOTS"] = str(run_dir.resolve())
    env["VIVADO_AGENT_MCP_TOOL_PROFILE"] = "core"
    env.pop("PYTHONPATH", None)
    return env


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _last_json_object(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _text_tail(value: str | bytes | None, limit: int = 4000) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[-limit:]
    return str(value or "")[-limit:]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
