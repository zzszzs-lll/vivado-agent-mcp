from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .agent_actions import next_action
from .env import resolve_runtime_dir
from .runtime_identity import (
    RUNTIME_IDENTITY_FILENAME,
    inspect_runtime_identity,
    runtime_root_rejection_reason,
)
from .evidence_attestation import ATTESTATION_KEY_FILENAME, ATTESTATION_LEDGER_DIRECTORY
from .managed_path import (
    ManagedPathError,
    delete_managed_snapshot,
    snapshot_managed_tree,
    validate_managed_snapshot,
)


PROCESS_NAMES = {"vivado", "vivado.exe", "xsim", "xsim.exe", "hw_server", "hw_server.exe"}
PROJECT_MARKERS = {"vmcp_artifacts", "vmcp_reports", "vmcp_diagnostics", "vmcp_signoff", "vmcp_constraints"}
PROCESS_DETECTION_TIMEOUT_S = 2
ACTIVE_PROCESS_DETECTION_TIMEOUT_S = 5
RUNTIME_SCAN_MAX_FILES = 100_000
RUNTIME_SCAN_MAX_DEPTH = 32
RUNTIME_SCAN_TIMEOUT_S = 5.0
_INCOMPLETE_SCAN_REASONS = {
    "scan_file_limit_exceeded",
    "scan_depth_limit_exceeded",
    "scan_time_limit_exceeded",
    "symlink_not_followed",
    "junction_not_followed",
    "mount_point_not_followed",
    "scandir_failed",
    "stat_failed",
}


def get_runtime_cache_status(runtime_dir: str | Path | None = None, *, max_largest_files: int = 10) -> dict[str, Any]:
    root = _runtime_root(runtime_dir)
    configured_runtime = resolve_runtime_dir().resolve()
    root_rejection = _runtime_preflight_rejection(root, configured_runtime)
    if root_rejection:
        return _blocked_status(
            root,
            configured_runtime,
            _uninspected_runtime_identity(root, root_rejection),
            root_rejection,
        )
    runtime_identity = inspect_runtime_identity(root)
    server_processes = detect_vivado_agent_server_processes(_workspace_root_from_runtime(root))
    process_detection_status = _process_detection_status(server_processes)
    if not root.exists():
        next_actions = [
            next_action(
                "start_session",
                "Create a Vivado MCP runtime directory only when a new Vivado session is needed.",
                preconditions=["A Vivado session is required for the current workflow."],
                stop_condition="start_session returns a runtime_dir or the workflow proceeds without a runtime cache.",
                optional=True,
            )
        ]
        if process_detection_status["status"] == "UNAVAILABLE":
            next_actions.append(_retry_runtime_status_action())
        return {
            "runtime_dir": str(root),
            "exists": False,
            "file_count": 0,
            "dir_count": 0,
            "total_bytes": 0,
            "categories": {},
            "largest_files": [],
            "cleanup_candidates": _empty_cleanup_summary(),
            "runtime_identity": runtime_identity,
            "scan": _scan_summary([], [], [], performed=False),
            "server_processes": server_processes,
            "process_detection_status": process_detection_status,
            "next_actions": next_actions,
        }

    if runtime_identity.get("status") != "READY":
        return _blocked_status(
            root,
            configured_runtime,
            runtime_identity,
            str(runtime_identity.get("reason") or "runtime_identity_invalid"),
        )
    if _looks_like_project_dir(root):
        return _blocked_status(root, configured_runtime, runtime_identity, "runtime_dir_looks_like_project")

    files, dirs, skipped = _scan_runtime(root)
    categories = _category_summary(files, dirs)
    cleanup = _cleanup_plan(root, include_unknown=False, max_age_hours=0, scan=(files, dirs, skipped))
    largest_files = sorted(
        (
            {"path": item["path"], "size": item["size"], "category": item["category"]}
            for item in files
        ),
        key=lambda item: int(item["size"]),
        reverse=True,
    )[:max_largest_files]
    next_actions = [
        next_action(
            "clean_runtime_cache",
            "Review or remove temporary Vivado runtime cache after the session is stopped.",
            required_args=["runtime_dir"],
            arg_sources={"runtime_dir": "get_runtime_cache_status.data.runtime_dir"},
            preconditions=["No Vivado session or Vivado/XSIM/hw_server process is actively using the runtime directory."],
            stop_condition="clean_runtime_cache returns DRY_RUN or CLEANED.",
            optional=True,
        )
    ]
    if process_detection_status["status"] == "UNAVAILABLE":
        next_actions.append(_retry_runtime_status_action())
    return {
        "runtime_dir": str(root),
        "exists": True,
        "file_count": len(files),
        "dir_count": len(dirs),
        "total_bytes": sum(int(item["size"]) for item in files),
        "categories": categories,
        "largest_files": largest_files,
        "cleanup_candidates": cleanup["planned"],
        "runtime_identity": runtime_identity,
        "scan": _scan_summary(files, dirs, skipped),
        "server_processes": server_processes,
        "process_detection_status": process_detection_status,
        "skipped": skipped,
        "next_actions": next_actions,
    }


def clean_runtime_cache(
    runtime_dir: str | Path | None = None,
    *,
    dry_run: bool = True,
    max_age_hours: float = 0,
    include_unknown: bool = False,
    runtime_identity: str = "",
    plan_sha256: str = "",
    execution_intent: str = "",
    confirm: str = "",
    active_processes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    root = _runtime_root(runtime_dir)
    status = get_runtime_cache_status(root)
    if status.get("status") == "BLOCK":
        blocked = _blocked(root, str(status.get("reason") or "runtime_preflight_failed"), status)
        if status.get("configured_runtime_dir"):
            blocked["configured_runtime_dir"] = status["configured_runtime_dir"]
        return blocked
    identity = status.get("runtime_identity", {})
    if not dry_run and not runtime_identity:
        return _blocked(root, "runtime_identity_confirmation_required", status)
    if not dry_run and runtime_identity != identity.get("runtime_id"):
        return _blocked(root, "runtime_identity_mismatch", status)
    if include_unknown and not dry_run:
        if execution_intent != "clean_runtime_unknown":
            return _blocked(root, "unknown_cleanup_intent_required", status)
        if confirm != "CLEAN_RUNTIME_UNKNOWN":
            return _blocked(root, "unknown_cleanup_confirmation_required", status)
    active = active_processes if active_processes is not None else detect_active_vivado_processes()
    if active and not dry_run:
        block_reason = "process_detection_unavailable" if _process_detection_unavailable_present(active) else "vivado_process_active"
        data = _blocked(root, block_reason, status)
        data["active_processes"] = _normalize_processes(active)
        data["will_not_clean_because_active_process"] = True
        return data

    plan = _cleanup_plan(root, include_unknown=include_unknown, max_age_hours=max_age_hours)
    if not plan["scan_complete"]:
        blocked = _blocked(root, "runtime_scan_incomplete", status)
        blocked["scan"] = plan["scan"]
        blocked["skipped"] = plan["skipped"]
        return blocked
    normalized_active = _normalize_processes(active)
    if dry_run:
        return {
            "status": "DRY_RUN",
            "runtime_dir": str(root),
            "dry_run": True,
            "include_unknown": include_unknown,
            "max_age_hours": max_age_hours,
            "runtime_identity": identity,
            "plan_sha256": plan["plan_sha256"],
            "planned": plan["planned"],
            "deleted": _empty_deleted_summary(),
            "skipped": plan["skipped"],
            "active_processes": normalized_active,
            "will_not_clean_because_active_process": bool(normalized_active),
            "next_actions": [
                next_action(
                    "clean_runtime_cache",
                    "Apply the reviewed runtime cleanup plan.",
                    required_args=(
                        ["runtime_dir", "dry_run", "runtime_identity", "plan_sha256", "execution_intent", "confirm"]
                        if include_unknown
                        else ["runtime_dir", "dry_run", "runtime_identity", "plan_sha256"]
                    ),
                    arg_sources={
                        "runtime_dir": "clean_runtime_cache.data.runtime_dir",
                        "runtime_identity": "clean_runtime_cache.data.runtime_identity.runtime_id",
                        "plan_sha256": "clean_runtime_cache.data.plan_sha256",
                        "dry_run": "set false after reviewing dry-run output",
                        **(
                            {
                                "execution_intent": "set to clean_runtime_unknown",
                                "confirm": "set to CLEAN_RUNTIME_UNKNOWN",
                            }
                            if include_unknown
                            else {}
                        ),
                    },
                    preconditions=["stop_session has completed and no Vivado/XSIM/hw_server process is active."],
                    stop_condition="clean_runtime_cache returns CLEANED.",
                )
            ],
        }

    if not plan_sha256:
        blocked = _blocked(root, "cleanup_plan_confirmation_required", status)
        blocked["actual_plan_sha256"] = plan["plan_sha256"]
        return blocked
    if plan_sha256 != plan["plan_sha256"]:
        blocked = _blocked(root, "cleanup_plan_mismatch", status)
        blocked["expected_plan_sha256"] = plan_sha256
        blocked["actual_plan_sha256"] = plan["plan_sha256"]
        return blocked

    try:
        deleted = _apply_cleanup_plan(root, plan)
    except (ManagedPathError, OSError) as exc:
        blocked = _blocked(root, "cleanup_plan_drift", status)
        blocked["plan_sha256"] = plan["plan_sha256"]
        blocked["drift_error"] = f"{exc.__class__.__name__}: {exc}"
        return blocked
    return {
        "status": "CLEANED",
        "runtime_dir": str(root),
        "dry_run": False,
        "include_unknown": include_unknown,
        "max_age_hours": max_age_hours,
        "runtime_identity": identity,
        "plan_sha256": plan["plan_sha256"],
        "planned": plan["planned"],
        "deleted": deleted,
        "skipped": plan["skipped"],
        "active_processes": _normalize_processes(active),
        "next_actions": [
            next_action(
                "get_runtime_cache_status",
                "Verify runtime cache size after cleanup.",
                required_args=["runtime_dir"],
                arg_sources={"runtime_dir": "clean_runtime_cache.data.runtime_dir"},
                preconditions=["clean_runtime_cache returned CLEANED."],
                stop_condition="runtime cache status is collected.",
                optional=True,
            )
        ],
    }


def detect_active_vivado_processes() -> list[dict[str, Any]]:
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                timeout=ACTIVE_PROCESS_DETECTION_TIMEOUT_S,
            )
            rows = csv.reader(completed.stdout.splitlines())
            processes = []
            for row in rows:
                if len(row) < 2:
                    continue
                name = row[0]
                if name.lower() in PROCESS_NAMES:
                    processes.append({"name": name, "pid": row[1]})
            return processes
        completed = subprocess.run(
            ["ps", "-A", "-o", "pid=", "-o", "comm="],
            capture_output=True,
            text=True,
            check=False,
            timeout=ACTIVE_PROCESS_DETECTION_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return [_process_detection_unavailable("process_probe_timeout")]
    except OSError:
        return [_process_detection_unavailable("process_probe_failed")]
    processes = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        pid, name = parts
        if Path(name).name.lower() in PROCESS_NAMES:
            processes.append({"name": Path(name).name, "pid": pid})
    return processes


def detect_vivado_agent_server_processes(workspace_root: str | Path | None = None) -> list[dict[str, Any]]:
    workspace = Path(workspace_root).resolve() if workspace_root else Path.cwd().resolve()
    try:
        if os.name == "nt":
            command = (
                "$ErrorActionPreference='SilentlyContinue'; "
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -match '^(python|python.exe|py|py.exe)$' -and $_.CommandLine -match 'vivado_agent_mcp' } | "
                "Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine | ConvertTo-Json -Compress"
            )
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                check=False,
                timeout=PROCESS_DETECTION_TIMEOUT_S,
            )
            return _parse_windows_server_processes(completed.stdout, workspace)

        completed = subprocess.run(
            ["ps", "-eo", "pid=", "-o", "ppid=", "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=PROCESS_DETECTION_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return [_process_detection_unavailable("server_process_probe_timeout")]
    except OSError:
        return [_process_detection_unavailable("server_process_probe_failed")]
    return _parse_posix_server_processes(completed.stdout, workspace)


def _runtime_root(runtime_dir: str | Path | None) -> Path:
    if runtime_dir is None or str(runtime_dir).strip() == "":
        return resolve_runtime_dir().resolve()
    return resolve_runtime_dir(str(runtime_dir)).resolve()


def _runtime_preflight_rejection(root: Path, configured_runtime: Path) -> str:
    if root != configured_runtime:
        return "runtime_dir_not_configured_root"
    if _is_network_path(root):
        return "runtime_dir_network_path_not_allowed"
    rejection = runtime_root_rejection_reason(root)
    if rejection:
        return rejection
    if root.exists() and (not root.is_dir() or root.is_symlink() or _is_junction(root) or _is_mount_point(root)):
        return "runtime_dir_not_regular_directory"
    return ""


def _blocked_status(
    root: Path,
    configured_runtime: Path,
    runtime_identity: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "status": "BLOCK",
        "reason": reason,
        "runtime_dir": str(root),
        "configured_runtime_dir": str(configured_runtime),
        "exists": root.exists(),
        "file_count": 0,
        "dir_count": 0,
        "total_bytes": 0,
        "categories": {},
        "largest_files": [],
        "cleanup_candidates": _empty_cleanup_summary(),
        "runtime_identity": runtime_identity,
        "scan": _scan_summary([], [], [], performed=False),
        "server_processes": [],
        "process_detection_status": {
            "status": "SKIPPED",
            "reason": "runtime_preflight_failed",
            "cleanup_impact": "Runtime contents were not scanned.",
            "dry_run_impact": "Dry-run is blocked until the configured runtime identity is valid.",
        },
        "skipped": [],
        "next_actions": [],
    }


def _scan_runtime(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    files: list[dict[str, Any]] = []
    dirs: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    if not root.exists():
        return files, dirs, skipped
    started_at = time.monotonic()
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        if time.monotonic() - started_at > RUNTIME_SCAN_TIMEOUT_S:
            skipped.append({"path": str(directory), "reason": "scan_time_limit_exceeded"})
            break
        try:
            entries = os.scandir(directory)
        except OSError:
            skipped.append({"path": str(directory), "reason": "scandir_failed"})
            continue
        with entries:
            for entry in entries:
                path = Path(entry.path)
                if time.monotonic() - started_at > RUNTIME_SCAN_TIMEOUT_S:
                    skipped.append({"path": str(path), "reason": "scan_time_limit_exceeded"})
                    pending.clear()
                    break
                if entry.is_symlink():
                    skipped.append({"path": str(path), "reason": "symlink_not_followed"})
                    continue
                if _is_junction(path):
                    skipped.append({"path": str(path), "reason": "junction_not_followed"})
                    continue
                if _is_mount_point(path):
                    skipped.append({"path": str(path), "reason": "mount_point_not_followed"})
                    continue
                try:
                    stat = entry.stat(follow_symlinks=False)
                    if entry.is_dir(follow_symlinks=False):
                        child_depth = depth + 1
                        if child_depth > RUNTIME_SCAN_MAX_DEPTH:
                            skipped.append({"path": str(path), "reason": "scan_depth_limit_exceeded"})
                            continue
                        dirs.append(
                            {
                                "path": str(path),
                                "category": _category_for_dir(root, path),
                                "empty": _is_empty_dir(path),
                                "mtime_ns": stat.st_mtime_ns,
                                "file_id": _stat_file_id(stat),
                            }
                        )
                        pending.append((path, child_depth))
                    elif entry.is_file(follow_symlinks=False):
                        if len(files) >= RUNTIME_SCAN_MAX_FILES:
                            skipped.append({"path": str(path), "reason": "scan_file_limit_exceeded"})
                            pending.clear()
                            break
                        files.append(
                            {
                                "path": str(path),
                                "category": _category_for_file(root, path),
                                "size": stat.st_size,
                                "mtime": stat.st_mtime,
                                "mtime_ns": stat.st_mtime_ns,
                                "file_id": _stat_file_id(stat),
                            }
                        )
                except OSError:
                    skipped.append({"path": str(path), "reason": "stat_failed"})
    return files, dirs, skipped


def _scan_complete(skipped: list[dict[str, str]]) -> bool:
    return not any(item.get("reason") in _INCOMPLETE_SCAN_REASONS for item in skipped)


def _scan_summary(
    files: list[dict[str, Any]],
    dirs: list[dict[str, Any]],
    skipped: list[dict[str, str]],
    *,
    performed: bool = True,
) -> dict[str, Any]:
    return {
        "performed": performed,
        "complete": performed and _scan_complete(skipped),
        "file_count": len(files),
        "dir_count": len(dirs),
        "limits": {
            "max_files": RUNTIME_SCAN_MAX_FILES,
            "max_depth": RUNTIME_SCAN_MAX_DEPTH,
            "timeout_s": RUNTIME_SCAN_TIMEOUT_S,
        },
        "block_reasons": sorted(
            {str(item.get("reason")) for item in skipped if item.get("reason") in _INCOMPLETE_SCAN_REASONS}
        ),
    }


def _category_summary(files: list[dict[str, Any]], dirs: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for item in files:
        bucket = summary.setdefault(str(item["category"]), {"file_count": 0, "dir_count": 0, "bytes": 0})
        bucket["file_count"] += 1
        bucket["bytes"] += int(item["size"])
    for item in dirs:
        category = str(item["category"])
        if item.get("empty"):
            category = "empty_dir"
        bucket = summary.setdefault(category, {"file_count": 0, "dir_count": 0, "bytes": 0})
        bucket["dir_count"] += 1
    return summary


def _cleanup_plan(
    root: Path,
    *,
    include_unknown: bool,
    max_age_hours: float,
    scan: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    files, dirs, skipped = scan if scan is not None else _scan_runtime(root)
    cutoff = time.time() - max_age_hours * 3600 if max_age_hours > 0 else None
    cleanable_categories = {
        "bootstrap_tcl",
        "vivado_xil",
        "java_jni_tmp",
        "java_perfdata",
        "xsim_wave_tmp",
        "programming_staging",
    }
    if include_unknown:
        cleanable_categories.add("unknown")

    directory_candidates: list[Path] = []
    for item in dirs:
        path = Path(str(item["path"]))
        category = str(item["category"])
        if item.get("empty") or category in {"vivado_xil", "java_perfdata", "programming_staging"}:
            if _old_enough(path, cutoff):
                directory_candidates.append(path)
    directory_candidates = _collapse_directories(root, directory_candidates)

    standalone_files: list[dict[str, Any]] = []
    planned_bytes = 0
    planned_file_count = 0
    planned_categories: dict[str, int] = {}
    for item in files:
        path = Path(str(item["path"]))
        category = str(item["category"])
        if category not in cleanable_categories or not _old_enough(path, cutoff):
            continue
        planned_file_count += 1
        planned_bytes += int(item["size"])
        planned_categories[category] = planned_categories.get(category, 0) + 1
        if not any(_inside(directory, path) for directory in directory_candidates):
            standalone_files.append(item)

    directories_by_path = {str(item["path"]): item for item in dirs}
    directory_entries = []
    for path in directory_candidates:
        contained = [item for item in files if _inside(path, Path(str(item["path"])))]
        directory_metadata = directories_by_path.get(str(path), {})
        directory_entries.append(
            {
                "path": str(path),
                "category": _category_for_dir(root, path),
                "file_count": len(contained),
                "bytes": sum(int(item["size"]) for item in contained),
                "mtime_ns": int(directory_metadata.get("mtime_ns", 0)),
                "file_id": str(directory_metadata.get("file_id", "")),
            }
        )
    deletion_targets = [
        {"path": str(item["path"]), "snapshot": snapshot_managed_tree(root, item["path"])}
        for item in standalone_files
    ]
    deletion_targets.extend(
        {"path": str(path), "snapshot": snapshot_managed_tree(root, path)}
        for path in directory_candidates
    )
    plan = {
        "files": standalone_files,
        "directories": directory_entries,
        "deletion_targets": sorted(deletion_targets, key=lambda item: str(item["path"]).lower()),
        "planned": {
            "file_count": planned_file_count,
            "dir_count": len(directory_candidates),
            "bytes": planned_bytes,
            "categories": planned_categories,
        },
        "skipped": skipped,
        "scan_complete": _scan_complete(skipped),
        "scan": _scan_summary(files, dirs, skipped),
    }
    plan["plan_sha256"] = _cleanup_plan_sha256(
        root,
        files=files,
        dirs=dirs,
        plan=plan,
        include_unknown=include_unknown,
        max_age_hours=max_age_hours,
    )
    return plan


def _cleanup_plan_sha256(
    root: Path,
    *,
    files: list[dict[str, Any]],
    dirs: list[dict[str, Any]],
    plan: dict[str, Any],
    include_unknown: bool,
    max_age_hours: float,
) -> str:
    identity = inspect_runtime_identity(root)
    payload = {
        "schema_version": 1,
        "runtime_dir": str(root),
        "runtime_id": str(identity.get("runtime_id", "")),
        "include_unknown": bool(include_unknown),
        "max_age_hours": float(max_age_hours),
        "inventory": {
            "files": sorted(
                (
                    {
                        "path": _relative_key(root, Path(str(item["path"]))),
                        "category": str(item["category"]),
                        "size": int(item["size"]),
                        "mtime_ns": int(item.get("mtime_ns", 0)),
                        "file_id": str(item.get("file_id", "")),
                    }
                    for item in files
                ),
                key=lambda item: item["path"],
            ),
            "directories": sorted(
                (
                    {
                        "path": _relative_key(root, Path(str(item["path"]))),
                        "category": str(item["category"]),
                        "empty": bool(item.get("empty")),
                        "mtime_ns": int(item.get("mtime_ns", 0)),
                        "file_id": str(item.get("file_id", "")),
                    }
                    for item in dirs
                ),
                key=lambda item: item["path"],
            ),
        },
        "targets": {
            "files": sorted(
                (
                    {
                        "path": _relative_key(root, Path(str(item["path"]))),
                        "category": str(item["category"]),
                        "size": int(item["size"]),
                        "mtime_ns": int(item.get("mtime_ns", 0)),
                        "file_id": str(item.get("file_id", "")),
                    }
                    for item in plan["files"]
                ),
                key=lambda item: item["path"],
            ),
            "directories": sorted(
                (
                    {
                        "path": _relative_key(root, Path(str(item["path"]))),
                        "category": str(item["category"]),
                        "file_count": int(item.get("file_count", 0)),
                        "bytes": int(item.get("bytes", 0)),
                        "mtime_ns": int(item.get("mtime_ns", 0)),
                        "file_id": str(item.get("file_id", "")),
                    }
                    for item in plan["directories"]
                ),
                key=lambda item: item["path"],
            ),
            "snapshots": [
                {
                    "path": _relative_key(root, Path(str(target["path"]))),
                    "entries": [
                        {
                            **entry,
                            "path": _relative_key(root, Path(str(entry["path"]))),
                        }
                        for entry in target["snapshot"]
                    ],
                }
                for target in plan["deletion_targets"]
            ],
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _relative_key(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _uninspected_runtime_identity(root: Path, reason: str) -> dict[str, Any]:
    return {
        "status": "SKIPPED",
        "runtime_id": "",
        "runtime_dir": str(root),
        "marker_path": "",
        "schema_version": 1,
        "created_at": "",
        "workspace_root": "",
        "reason": f"runtime_preflight_failed:{reason}",
    }


def _is_network_path(path: Path) -> bool:
    text = str(path)
    return text.startswith("\\\\") or text.startswith("//")


def _is_junction(path: Path) -> bool:
    predicate = getattr(path, "is_junction", None)
    if predicate is None:
        return False
    try:
        return bool(predicate())
    except OSError:
        return True


def _is_mount_point(path: Path) -> bool:
    try:
        return path.is_mount()
    except NotImplementedError:
        return os.path.ismount(path)
    except OSError:
        return True


def _stat_file_id(stat: os.stat_result) -> str:
    return f"{int(getattr(stat, 'st_dev', 0))}:{int(getattr(stat, 'st_ino', 0))}"


def _path_crosses_link_boundary(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or _is_junction(current) or _is_mount_point(current):
            return True
    return False


def _apply_cleanup_plan(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    targets = list(plan.get("deletion_targets", []))
    for target in targets:
        validate_managed_snapshot(root, target["path"], target["snapshot"])
    deleted = {"file_count": 0, "dir_count": 0, "bytes": 0, "skipped": []}
    for target in targets:
        result = delete_managed_snapshot(root, target["path"], target["snapshot"])
        for key in ("file_count", "dir_count", "bytes"):
            deleted[key] += int(result[key])
    return deleted


def _blocked(root: Path, reason: str, status: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "BLOCK",
        "reason": reason,
        "runtime_dir": str(root),
        "dry_run": False,
        "planned": status.get("cleanup_candidates", _empty_cleanup_summary()),
        "deleted": _empty_deleted_summary(),
        "skipped": status.get("skipped", []),
        "next_actions": [
            next_action(
                "get_runtime_cache_status",
                "Inspect runtime cache state before retrying cleanup.",
                required_args=["runtime_dir"],
                arg_sources={"runtime_dir": "clean_runtime_cache.data.runtime_dir"},
                preconditions=["Cleanup blocker has been resolved."],
                stop_condition="runtime cache status is collected.",
            )
        ],
    }


def _category_for_file(root: Path, path: Path) -> str:
    name = path.name
    parts = set(_relative_parts(root, path))
    if name == RUNTIME_IDENTITY_FILENAME:
        return "runtime_identity"
    if name == ATTESTATION_KEY_FILENAME or ATTESTATION_LEDGER_DIRECTORY in parts:
        return "evidence_attestation"
    if "traces" in parts and path.suffix.lower() == ".jsonl":
        return "workflow_trace"
    if "programming_staging" in parts:
        return "programming_staging"
    if name == "workflow_trace.jsonl":
        return "workflow_trace"
    if ".Xil" in parts:
        return "vivado_xil"
    if name.startswith("vivado_agent_mcp_") and name.endswith(".tcl"):
        return "bootstrap_tcl"
    if name.startswith("libzstd-jni") and name.endswith(".dll"):
        return "java_jni_tmp"
    if any(part.startswith("hsperfdata_") for part in parts):
        return "java_perfdata"
    if path.suffix.lower() == ".xilwvdat":
        return "xsim_wave_tmp"
    return "unknown"


def _category_for_dir(root: Path, path: Path) -> str:
    parts = _relative_parts(root, path)
    if ATTESTATION_LEDGER_DIRECTORY in parts:
        return "evidence_attestation"
    if "programming_staging" in parts:
        return "programming_staging"
    if ".Xil" in parts:
        return "vivado_xil"
    if any(part.startswith("hsperfdata_") for part in parts):
        return "java_perfdata"
    return "unknown"


def _relative_parts(root: Path, path: Path) -> tuple[str, ...]:
    try:
        return path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        return ()


def _collapse_directories(root: Path, candidates: list[Path]) -> list[Path]:
    collapsed: list[Path] = []
    for candidate in sorted((path.resolve() for path in candidates), key=lambda item: len(item.parts)):
        if candidate == root.resolve():
            continue
        if any(_inside(parent, candidate) for parent in collapsed):
            continue
        collapsed.append(candidate)
    return collapsed


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _is_empty_dir(path: Path) -> bool:
    try:
        next(path.iterdir())
    except StopIteration:
        return True
    except OSError:
        return False
    return False


def _old_enough(path: Path, cutoff: float | None) -> bool:
    if cutoff is None:
        return True
    try:
        return path.stat().st_mtime <= cutoff
    except OSError:
        return False


def _looks_like_project_dir(root: Path) -> bool:
    if not root.exists() or not root.is_dir():
        return False
    try:
        children = list(root.iterdir())
    except OSError:
        return False
    if any(child.suffix.lower() == ".xpr" for child in children if child.is_file()):
        return True
    return any(child.is_dir() and child.name in PROJECT_MARKERS for child in children)


def _workspace_root_from_runtime(root: Path) -> Path:
    if root.name == "runtime" and root.parent.name == ".vivado_agent_mcp":
        return root.parent.parent.resolve()
    return Path.cwd().resolve()


def _parse_windows_server_processes(stdout: str, workspace: Path) -> list[dict[str, Any]]:
    text = stdout.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = payload if isinstance(payload, list) else [payload]
    processes: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        command_line = str(row.get("CommandLine", ""))
        if "vivado_agent_mcp" not in command_line:
            continue
        processes.append(
            {
                "pid": str(row.get("ProcessId", "")),
                "ppid": str(row.get("ParentProcessId", "")),
                "started_at": str(row.get("CreationDate", "")),
                "command_summary": _summarize_command(command_line),
                "workspace_match": str(workspace).lower() in command_line.lower(),
            }
        )
    return processes


def _parse_posix_server_processes(stdout: str, workspace: Path) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if "vivado_agent_mcp" not in line:
            continue
        parts = line.strip().split(maxsplit=7)
        if len(parts) < 8:
            continue
        pid, ppid = parts[0], parts[1]
        started_at = " ".join(parts[2:7])
        command_line = parts[7]
        processes.append(
            {
                "pid": pid,
                "ppid": ppid,
                "started_at": started_at,
                "command_summary": _summarize_command(command_line),
                "workspace_match": str(workspace) in command_line,
            }
        )
    return processes


def _summarize_command(command_line: str, limit: int = 240) -> str:
    return _truncate_command(_redact_sensitive_command_args(command_line), limit=limit)


def _redact_sensitive_command_args(command_line: str) -> str:
    text = command_line.replace("\r", " ").replace("\n", " ").strip()
    sensitive_name = r"(?:token|access[-_]?token|password|passwd|secret|credential|api[-_]?key|apikey)"
    text = re.sub(
        rf"(?i)(\b{sensitive_name}\s*=\s*)(\"[^\"]*\"|'[^']*'|\S+)",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        rf"(?i)((?:--?|/){sensitive_name}(?:=|:))(\"[^\"]*\"|'[^']*'|\S+)",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        rf"(?i)((?:--?|/){sensitive_name}\s+)(\"[^\"]*\"|'[^']*'|\S+)",
        r"\1<redacted>",
        text,
    )
    return text


def _truncate_command(command_line: str, limit: int = 240) -> str:
    text = command_line.replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 13] + "...<truncated>"


def _process_detection_unavailable(reason: str) -> dict[str, str]:
    return {"name": "process_detection_unavailable", "pid": "", "reason": reason}


def _process_detection_unavailable_present(processes: list[dict[str, Any]]) -> bool:
    return any(str(item.get("name", "")) == "process_detection_unavailable" for item in processes)


def _process_detection_status(processes: list[dict[str, Any]]) -> dict[str, str]:
    unavailable = next((item for item in processes if str(item.get("name", "")) == "process_detection_unavailable"), None)
    if unavailable:
        return {
            "status": "UNAVAILABLE",
            "reason": str(unavailable.get("reason", "")),
            "cleanup_impact": "Real cleanup should stay dry-run or be retried after process detection succeeds.",
            "dry_run_impact": "Runtime status statistics remain usable, but active MCP server process detection is incomplete.",
        }
    return {
        "status": "AVAILABLE",
        "reason": "",
        "cleanup_impact": "Process detection completed; cleanup gates can use the observed process list.",
        "dry_run_impact": "Runtime status statistics are usable.",
    }


def _retry_runtime_status_action() -> dict[str, Any]:
    return next_action(
        "get_runtime_cache_status",
        "Retry runtime status when server process probing timed out or failed; keep real cleanup as dry-run until process detection is available.",
        preconditions=["No urgent real cleanup is required, or process detection failure needs confirmation."],
        stop_condition="get_runtime_cache_status returns process_detection_status.status=AVAILABLE or the user keeps cleanup dry-run.",
        optional=True,
    )


def _normalize_processes(processes: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in processes:
        entry = {"name": str(item.get("name", "")), "pid": str(item.get("pid", ""))}
        if item.get("reason"):
            entry["reason"] = str(item["reason"])
        normalized.append(entry)
    return normalized


def _empty_cleanup_summary() -> dict[str, Any]:
    return {"file_count": 0, "dir_count": 0, "bytes": 0, "categories": {}}


def _empty_deleted_summary() -> dict[str, Any]:
    return {"file_count": 0, "dir_count": 0, "bytes": 0, "skipped": []}
