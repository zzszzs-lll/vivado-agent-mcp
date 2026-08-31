from __future__ import annotations

import json
import hashlib
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_actions import next_action
from .env import resolve_runtime_dir


MAX_SUMMARY_STRING = 240
MAX_SUMMARY_ITEMS = 12
OMITTED_KEYS = {"raw", "raw_excerpt", "command", "log_excerpt", "properties_raw"}
RESOLUTION_IDENTITY_KEYS = (
    "manifest_path",
    "bundle_dir",
    "project_path",
    "project_dir",
    "run_name",
    "fileset",
    "device",
    "target",
    "hw_target",
    "board_fingerprint",
    "ip_name",
    "bd_name",
    "cell_name",
    "port_name",
    "interface_pin",
    "net_name",
    "artifact_manifest_path",
    "report_manifest_path",
    "output_dir",
    "project_name",
    "top",
    "testbench_top",
)
RESOLUTION_NEUTRAL_ARGS = {
    "timeout_s",
    "poll_interval_s",
    "max_wait_s",
    "probe_timeout_s",
}


def _synchronized(method):
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


class WorkflowTracer:
    def __init__(self, trace_id: str | None = None, trace_dir: str | Path | None = None) -> None:
        self.trace_id = trace_id or os.environ.get("VIVADO_AGENT_MCP_TRACE_ID") or _new_trace_id()
        configured_dir = trace_dir or os.environ.get("VIVADO_AGENT_MCP_TRACE_DIR")
        self.trace_dir = Path(configured_dir).expanduser().resolve() if configured_dir else resolve_runtime_dir().parent / "traces"
        self.trace_path = self.trace_dir / f"{self.trace_id}.jsonl"
        self.sequence = 0
        self.project_dir: Path | None = None
        self.project_trace_path: Path | None = None
        self._lock = threading.RLock()

    @_synchronized
    def record(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        result: dict[str, Any],
        started_at: datetime,
        ended_at: datetime,
        policy_shadow: dict[str, Any] | None = None,
    ) -> None:
        project_dir = infer_project_dir(result)
        if project_dir is not None:
            self.ensure_project_dir(project_dir)
        self.sequence += 1
        entry = {
            "trace_id": self.trace_id,
            "sequence": self.sequence,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_ms": int((ended_at - started_at).total_seconds() * 1000),
            "tool": tool,
            "args_summary": summarize(args),
            "result_summary": summarize_result(result),
            "operation_identity": _operation_identity(
                project_dir=self.project_dir,
                tool=tool,
                args=args,
            ),
        }
        if policy_shadow is not None:
            entry["policy_shadow"] = policy_shadow
        if result.get("ok") is False:
            entry["failure_id"] = f"{self.trace_id}:{self.sequence}"
        resolution_path = self.project_trace_path or self.trace_path
        existing, integrity = self._read_entries(resolution_path)
        if integrity["status"] == "READY" and result.get("ok") is True:
            tool_entries = [item for item in existing if item.get("entry_type") != "session_boundary"]
            unresolved, _, _ = _last_unresolved_failure(tool_entries)
            if unresolved and _can_resolve_failure(unresolved, entry):
                entry["resolves_failure_id"] = str(unresolved.get("failure_id", ""))
                entry["resolved_across_session_boundary"] = (
                    str(unresolved.get("trace_id", "")) != self.trace_id
                )
        self._append_jsonl(self.trace_path, entry)
        if self.project_trace_path is not None:
            self._append_jsonl(self.project_trace_path, entry, allow_session_boundary=True)

    @_synchronized
    def ensure_project_dir(self, project_dir: str | Path) -> Path:
        resolved = Path(project_dir).resolve()
        if self.project_dir == resolved and self.project_trace_path is not None:
            return self.project_trace_path
        self.project_dir = resolved
        self.project_trace_path = resolved / "vmcp_diagnostics" / "workflow_trace.jsonl"
        self.project_trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.project_trace_path.touch(exist_ok=True)
        return self.project_trace_path

    @_synchronized
    def status(self) -> dict[str, Any]:
        status_path = self.project_trace_path if self.project_trace_path is not None else self.trace_path
        entries, trace_integrity = self._read_entries(status_path)
        tool_entries = [entry for entry in entries if entry.get("entry_type") != "session_boundary"]
        successful = [entry for entry in tool_entries if entry.get("result_summary", {}).get("ok") is True]
        failed = [entry for entry in tool_entries if entry.get("result_summary", {}).get("ok") is False]
        last = tool_entries[-1] if tool_entries else {}
        unresolved_failure, resolved_by_tool, resolved_across_session_boundary = _last_unresolved_failure(tool_entries)
        data = {
            "trace_id": self.trace_id,
            "trace_path": str(self.trace_path),
            "trace_exists": self.trace_path.exists(),
            "status_trace_path": str(status_path),
            "project_dir": str(self.project_dir) if self.project_dir is not None else "",
            "project_trace_path": str(self.project_trace_path) if self.project_trace_path is not None else "",
            "workflow_trace_storage": {
                "global_trace_path": str(self.trace_path),
                "global_trace_scope": ".vivado_agent_mcp/traces append-only MCP transcript",
                "project_trace_path": str(self.project_trace_path) if self.project_trace_path is not None else "",
                "project_trace_scope": "<project_dir>/vmcp_diagnostics/workflow_trace.jsonl handoff copy",
                "project_trace_is_handoff_copy": self.project_trace_path is not None,
                "note": (
                    "The MCP global trace is runtime evidence. Once a project directory is known, "
                    "the same append-only entries are mirrored into the project diagnostic bundle area for handoff."
                ),
            },
            "tool_call_count": len(tool_entries),
            "trace_integrity": trace_integrity,
            "handoff_usable": trace_integrity["status"] == "READY",
            "last_tool": str(last.get("tool", "")) if last else "",
            "last_successful_tool": str(successful[-1].get("tool", "")) if successful else "",
            "last_failed_tool": str(failed[-1].get("tool", "")) if failed else "",
            "last_error_code": str(failed[-1].get("result_summary", {}).get("error_code", "")) if failed else "",
            "last_unresolved_failed_tool": str(unresolved_failure.get("tool", "")) if unresolved_failure else "",
            "last_unresolved_error_code": str(unresolved_failure.get("result_summary", {}).get("error_code", "")) if unresolved_failure else "",
            "last_unresolved_failure_id": str(unresolved_failure.get("failure_id", "")) if unresolved_failure else "",
            "failure_resolved_by_tool": resolved_by_tool,
            "resolved_across_session_boundary": resolved_across_session_boundary,
            "last_message": str(last.get("result_summary", {}).get("message", "")) if last else "",
            "last_next_actions": last.get("result_summary", {}).get("next_actions", []) if last else [],
            "next_actions": (
                _trace_integrity_next_actions(trace_integrity)
                if trace_integrity["status"] != "READY"
                else _status_next_actions(unresolved_failure)
            ),
        }
        return data

    def _append_jsonl(self, path: Path, entry: dict[str, Any], *, allow_session_boundary: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _trace_file_lock(path):
            entries, integrity = _read_trace_entries(
                path,
                expected_trace_id=None if allow_session_boundary else self.trace_id,
                legacy_status="BLOCK",
                missing_status="READY",
                allow_session_boundaries=allow_session_boundary,
            )
            if integrity["status"] != "READY" and path.exists() and path.stat().st_size:
                raise RuntimeError(f"Refusing to append to corrupt workflow trace: {integrity['issues']}")
            previous_hash = str(entries[-1].get("entry_hash", "")) if entries else ""
            with path.open("a", encoding="utf-8") as handle:
                if allow_session_boundary and entries and str(entries[-1].get("trace_id", "")) != self.trace_id:
                    boundary = {
                        "entry_type": "session_boundary",
                        "trace_id": self.trace_id,
                        "previous_trace_id": str(entries[-1].get("trace_id", "")),
                        "new_trace_id": self.trace_id,
                        "started_at": entry.get("started_at", ""),
                        "ended_at": entry.get("started_at", ""),
                    }
                    boundary = _ledger_entry(boundary, sequence=len(entries) + 1, previous_hash=previous_hash)
                    handle.write(json.dumps(boundary, ensure_ascii=False, separators=(",", ":")) + "\n")
                    previous_hash = boundary["entry_hash"]
                    entries.append(boundary)
                ledger_entry = _ledger_entry(entry, sequence=len(entries) + 1, previous_hash=previous_hash)
                handle.write(json.dumps(ledger_entry, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _read_entries(self, path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        project_trace = self.project_trace_path is not None and path == self.project_trace_path
        return _read_trace_entries(
            path,
            expected_trace_id=None if project_trace else self.trace_id,
            legacy_status="BLOCK",
            missing_status="READY",
            allow_session_boundaries=project_trace,
        )


def validate_workflow_trace_file(path: str | Path) -> dict[str, Any]:
    """Validate a handoff trace without trusting the manifest that references it."""

    _, integrity = _read_trace_entries(
        Path(path),
        expected_trace_id=None,
        legacy_status="WARN",
        missing_status="BLOCK",
        allow_session_boundaries=True,
    )
    return integrity


def infer_project_dir(result: dict[str, Any]) -> Path | None:
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    health_summary = data.get("health_summary") if isinstance(data.get("health_summary"), dict) else {}
    inputs = data.get("inputs") if isinstance(data.get("inputs"), dict) else {}
    input_project_state = inputs.get("project_state") if isinstance(inputs.get("project_state"), dict) else {}
    input_project = input_project_state.get("project") if isinstance(input_project_state.get("project"), dict) else {}
    candidates = [
        data.get("project_dir"),
        Path(str(data["project_path"])).parent if data.get("project_path") else None,
        data.get("bundle_dir"),
        data.get("partial_output_dir"),
        data.get("context", {}).get("project_dir") if isinstance(data.get("context"), dict) else None,
        data.get("project", {}).get("directory") if isinstance(data.get("project"), dict) else None,
        data.get("health", {}).get("bundle_dir") if isinstance(data.get("health"), dict) else None,
        health_summary.get("project_dir"),
        input_project.get("directory"),
    ]
    for value in candidates:
        if not value:
            continue
        path = Path(str(value))
        for parent in path.parents:
            if parent.name == "vmcp_diagnostics":
                path = parent.parent
                break
        if str(value).endswith("vmcp_diagnostics") or path.name.startswith("vmcp_"):
            path = path.parent
        if path.name and path.name != ".":
            return path.resolve()
    manifest_path = data.get("manifest_path")
    if manifest_path:
        path = Path(str(manifest_path)).resolve()
        for parent in path.parents:
            if parent.name == "vmcp_diagnostics":
                return parent.parent
    return None


def _last_unresolved_failure(entries: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str, bool]:
    failed_indices = [
        index
        for index, entry in enumerate(entries)
        if entry.get("result_summary", {}).get("ok") is False
    ]
    if not failed_indices:
        return None, "", False
    latest_resolver = ""
    latest_resolution_crossed_session = False
    for failed_index in reversed(failed_indices):
        failure_entry = entries[failed_index]
        resolver, crossed_session = _failure_resolver_after(entries[failed_index + 1 :], failure_entry)
        if resolver:
            if not latest_resolver:
                latest_resolver = resolver
                latest_resolution_crossed_session = crossed_session
            continue
        return failure_entry, latest_resolver, latest_resolution_crossed_session
    return None, latest_resolver, latest_resolution_crossed_session


def _failure_resolver_after(entries: list[dict[str, Any]], failure_entry: dict[str, Any]) -> tuple[str, bool]:
    failure_id = str(failure_entry.get("failure_id", ""))
    for entry in entries:
        tool = str(entry.get("tool", ""))
        if failure_id and str(entry.get("resolves_failure_id", "")) == failure_id and _can_resolve_failure(failure_entry, entry):
            return tool, _resolution_crossed_session(failure_entry, entry)
    return "", False


def _can_resolve_failure(failure_entry: dict[str, Any], success_entry: dict[str, Any]) -> bool:
    summary = success_entry.get("result_summary", {})
    args = success_entry.get("args_summary", {})
    return bool(
        str(failure_entry.get("tool", "")) == str(success_entry.get("tool", ""))
        and summary.get("ok") is True
        and str(summary.get("assessment_status", "")).upper() == "READY"
        and summary.get("stop_required") is False
        and summary.get("dry_run") is not True
        and (not isinstance(args, dict) or args.get("dry_run") is not True)
        and str(summary.get("status", "")).upper() != "DRY_RUN"
        and _same_resolution_target(failure_entry, success_entry)
    )


def _resolution_crossed_session(failure_entry: dict[str, Any], success_entry: dict[str, Any]) -> bool:
    explicit = success_entry.get("resolved_across_session_boundary")
    if isinstance(explicit, bool):
        return explicit
    failure_trace_id = str(failure_entry.get("trace_id", ""))
    success_trace_id = str(success_entry.get("trace_id", ""))
    return bool(failure_trace_id and success_trace_id and failure_trace_id != success_trace_id)


def _same_resolution_target(failure_entry: dict[str, Any], success_entry: dict[str, Any]) -> bool:
    failure_operation = str(failure_entry.get("operation_identity", ""))
    success_operation = str(success_entry.get("operation_identity", ""))
    if failure_operation or success_operation:
        return bool(failure_operation) and failure_operation == success_operation
    failure_args = failure_entry.get("args_summary") if isinstance(failure_entry.get("args_summary"), dict) else {}
    success_args = success_entry.get("args_summary") if isinstance(success_entry.get("args_summary"), dict) else {}
    identity_keys = {
        key
        for key in RESOLUTION_IDENTITY_KEYS
        if key in failure_args or key in success_args
    }
    if not identity_keys:
        identity_keys = (set(failure_args) | set(success_args)) - RESOLUTION_NEUTRAL_ARGS
    if not identity_keys:
        return False
    return all(
        key in failure_args
        and key in success_args
        and _normalized_identity_value(key, failure_args[key]) == _normalized_identity_value(key, success_args[key])
        for key in identity_keys
    )


def _normalized_identity_value(key: str, value: Any) -> str:
    if key.endswith(("_path", "_dir")) or key in {"output_dir", "project_dir", "bundle_dir"}:
        return os.path.normcase(os.path.normpath(str(value)))
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value).strip()


def summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    assessment_status = str(result.get("assessment_status", "")).upper()
    if not assessment_status:
        status = _result_status(data).upper()
        if result.get("ok") is False or status in {"BLOCK", "FAILED", "FAIL", "ERROR", "STALE", "TAINTED", "INVALID"}:
            assessment_status = "BLOCK"
        elif status in {"READY", "PASS", "PASSED", "COMPLETED", "COMPLETE", "CLEANED"}:
            assessment_status = "READY"
        elif status in {"WARN", "WARNING", "READY_WITH_WAIVERS"}:
            assessment_status = "WARN"
    summary = {
        "ok": bool(result.get("ok")),
        "tool": str(result.get("tool", "")),
        "error_code": str(result.get("error_code", "")),
        "message": _truncate(str(result.get("message", result.get("summary", "")))),
        "assessment_status": assessment_status,
        "stop_required": bool(result.get("stop_required", False)),
        "dry_run": data.get("dry_run") if isinstance(data.get("dry_run"), bool) else None,
        "status": _result_status(data),
        "paths": _result_paths(data),
        "next_actions": _next_action_summary(result.get("next_actions") or data.get("next_actions") or []),
    }
    return summary


def summarize(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return "<max_depth>"
    if isinstance(value, (str, Path)):
        return _truncate(str(value))
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        items = [summarize(item, depth=depth + 1) for item in value[:MAX_SUMMARY_ITEMS]]
        if len(value) > MAX_SUMMARY_ITEMS:
            items.append(f"<{len(value) - MAX_SUMMARY_ITEMS} more>")
        return items
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_SUMMARY_ITEMS:
                result["<more>"] = len(value) - MAX_SUMMARY_ITEMS
                break
            key_text = str(key)
            if key_text in OMITTED_KEYS:
                result[key_text] = "<omitted>"
                continue
            result[key_text] = summarize(item, depth=depth + 1)
        return result
    return _truncate(str(value))


def _result_status(data: dict[str, Any]) -> str:
    for key in ("effective_status", "status", "state", "normalized_state"):
        value = data.get(key)
        if value:
            return str(value)
    health = data.get("health") if isinstance(data.get("health"), dict) else {}
    if health.get("status"):
        return str(health["status"])
    progress = data.get("progress") if isinstance(data.get("progress"), dict) else {}
    return str(progress.get("normalized_status", progress.get("status", "")))


def _result_paths(data: dict[str, Any]) -> dict[str, str]:
    paths: dict[str, str] = {}
    for key in ("manifest_path", "bundle_dir", "project_dir", "project_path", "sim_dir", "log_path"):
        if data.get(key):
            paths[key] = _truncate(str(data[key]))
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    if project.get("directory"):
        paths["project_directory"] = _truncate(str(project["directory"]))
    if project.get("xpr_path"):
        paths["xpr_path"] = _truncate(str(project["xpr_path"]))
    return paths


def _status_next_actions(unresolved_failure: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not unresolved_failure:
        return [
            next_action(
                "get_agent_workflows",
                "Select or resume an Agent workflow using the trace as context.",
                preconditions=["Review get_workflow_trace_status.data.last_tool and last_unresolved_failed_tool."],
                stop_condition="Agent selects the next workflow step or exports a partial handoff.",
            )
        ]
    summary = unresolved_failure.get("result_summary", {}) if isinstance(unresolved_failure.get("result_summary"), dict) else {}
    if summary.get("error_code") == "TimeoutError":
        actions = [
            next_action(
                "session_status",
                "Check whether Vivado is still running and whether the Tcl channel is connected after the timeout.",
                stop_condition="session_status reports connected/process_running state.",
            ),
            next_action(
                "stop_session",
                "Stop the managed session if the Tcl channel is disconnected before reopening or repairing the project.",
                preconditions=["session_status shows connected=false or the current channel is unusable."],
                stop_condition="stop_session returns stopped=true or confirms no session is active.",
                optional=True,
            ),
            next_action(
                "start_session",
                "Start a fresh Vivado session before reopening a partial project.",
                preconditions=["The previous managed session has been stopped or is not connected."],
                stop_condition="start_session returns connected session data.",
                optional=True,
            ),
        ]
        paths = summary.get("paths", {}) if isinstance(summary.get("paths"), dict) else {}
        project_path = str(paths.get("project_path") or paths.get("xpr_path") or "")
        if project_path:
            actions.append(
                next_action(
                    "open_project",
                    "Open the partial project recorded in the timeout result.",
                    required_args=["project_path"],
                    arg_sources={"project_path": project_path},
                    preconditions=["A Vivado session is connected and the .xpr exists."],
                    stop_condition="open_project returns ok=true.",
                )
            )
        actions.extend(
            [
                next_action(
                    "list_fileset_files",
                    "Inspect filesets after reopening the partial project.",
                    required_args=["fileset"],
                    arg_sources={"fileset": "sources_1, constrs_1, or sim_1"},
                    preconditions=["The partial project is open."],
                    stop_condition="list_fileset_files returns current file references.",
                    optional=True,
                ),
                next_action(
                    "repair_project_setup",
                    "Reconcile RTL, XDC, sim files, tops, SystemVerilog file types, and compile order.",
                    preconditions=["The partial project is open or project_path is supplied."],
                    stop_condition="repair_project_setup returns READY or structured missing input diagnostics.",
                ),
            ]
        )
        return actions
    return [
        next_action(
            "get_agent_workflows",
            "Select or resume an Agent workflow using the trace as context.",
            preconditions=["Review get_workflow_trace_status.data.last_tool and last_unresolved_failed_tool."],
            stop_condition="Agent selects the next workflow step or exports a partial handoff.",
        ),
        next_action(
            "get_tool_catalog",
            "Review available tools before retrying the last failed MCP call.",
            preconditions=["The unresolved failure is understood and required inputs have been corrected."],
            stop_condition="Agent chooses an existing MCP tool and valid arguments for the retry.",
            optional=True,
        ),
    ]


def _next_action_summary(actions: Any) -> list[dict[str, Any]]:
    if not isinstance(actions, list):
        return []
    summarized = []
    for action in actions[:6]:
        if not isinstance(action, dict):
            continue
        summarized.append(
            {
                "tool": str(action.get("tool", "")),
                "required_args": [str(item) for item in action.get("required_args", []) if str(item)][:6],
                "optional": bool(action.get("optional", False)),
            }
        )
    return summarized


def _truncate(value: str) -> str:
    text = value.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= MAX_SUMMARY_STRING:
        return text
    return text[: MAX_SUMMARY_STRING - 16] + "<truncated>"


def _new_trace_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"{timestamp}_{uuid.uuid4().hex[:8]}"


def _entry_hash(entry: dict[str, Any]) -> str:
    payload = {key: value for key, value in entry.items() if key != "entry_hash"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ledger_entry(entry: dict[str, Any], *, sequence: int, previous_hash: str) -> dict[str, Any]:
    ledger_entry = dict(entry)
    ledger_entry["ledger_sequence"] = sequence
    ledger_entry["previous_hash"] = previous_hash
    ledger_entry["entry_hash"] = _entry_hash(ledger_entry)
    return ledger_entry


def _operation_identity(*, project_dir: Path | None, tool: str, args: dict[str, Any]) -> str:
    identity_keys = [key for key in sorted(args) if key not in RESOLUTION_NEUTRAL_ARGS]
    identity_args = {
        key: _normalized_identity_value(key, args[key])
        for key in identity_keys
    }
    payload = {
        "project_dir": os.path.normcase(os.path.normpath(str(project_dir))) if project_dir is not None else "",
        "tool": tool,
        "args": identity_args,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_trace_entries(
    path: Path,
    *,
    expected_trace_id: str | None,
    legacy_status: str,
    missing_status: str,
    allow_session_boundaries: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not path.exists():
        return [], {
            "status": missing_status,
            "format": "missing",
            "issues": [f"workflow trace does not exist: {path}"],
            "verified_entries": 0,
            "last_entry_hash": "",
        }

    entries: list[dict[str, Any]] = []
    issues: list[str] = []
    expected_previous_hash = ""
    expected_ledger_sequence = 1
    resolved_trace_id = expected_trace_id or ""
    trace_ids: list[str] = []
    session_boundaries: list[dict[str, Any]] = []
    legacy_lines = 0
    ledger_lines = 0
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"line {line_number} is invalid JSON: {exc.msg}")
            continue
        if not isinstance(payload, dict):
            issues.append(f"line {line_number} is not a JSON object")
            continue
        entries.append(payload)
        ledger_fields = {"ledger_sequence", "previous_hash", "entry_hash"}
        present_ledger_fields = ledger_fields.intersection(payload)
        if not present_ledger_fields:
            legacy_lines += 1
            continue
        if present_ledger_fields != ledger_fields:
            if "ledger_sequence" in payload and payload.get("ledger_sequence") != expected_ledger_sequence:
                issues.append(f"line {line_number} ledger sequence gap")
            issues.append(f"line {line_number} has an incomplete ledger envelope")
            continue
        ledger_lines += 1
        payload_trace_id = str(payload.get("trace_id", ""))
        if not resolved_trace_id:
            resolved_trace_id = payload_trace_id
            if payload_trace_id:
                trace_ids.append(payload_trace_id)
        if not payload_trace_id:
            issues.append(f"line {line_number} trace_id is missing")
        elif payload_trace_id != resolved_trace_id:
            valid_boundary = (
                allow_session_boundaries
                and payload.get("entry_type") == "session_boundary"
                and str(payload.get("previous_trace_id", "")) == resolved_trace_id
                and str(payload.get("new_trace_id", "")) == payload_trace_id
            )
            if valid_boundary:
                session_boundaries.append(
                    {
                        "previous_trace_id": str(payload.get("previous_trace_id", "")),
                        "new_trace_id": str(payload.get("new_trace_id", "")),
                        "ledger_sequence": payload.get("ledger_sequence"),
                    }
                )
                resolved_trace_id = payload_trace_id
                trace_ids.append(payload_trace_id)
            else:
                issues.append(f"line {line_number} trace_id mismatch without a valid session boundary")
        elif payload.get("entry_type") == "session_boundary":
            issues.append(f"line {line_number} session boundary does not transition trace_id")
        if payload.get("ledger_sequence") != expected_ledger_sequence:
            issues.append(f"line {line_number} ledger sequence gap")
        if str(payload.get("previous_hash", "")) != expected_previous_hash:
            issues.append(f"line {line_number} previous hash mismatch")
        entry_hash = str(payload.get("entry_hash", ""))
        if not entry_hash or entry_hash != _entry_hash(payload):
            issues.append(f"line {line_number} entry hash mismatch")
        expected_previous_hash = entry_hash
        expected_ledger_sequence += 1

    if legacy_lines and ledger_lines:
        issues.append("workflow trace mixes legacy and hash-chain entries")
    if not entries:
        return [], {
            "status": legacy_status,
            "format": "empty",
            "issues": ["workflow trace is empty"],
            "verified_entries": 0,
            "last_entry_hash": "",
            "trace_id": resolved_trace_id,
            "trace_ids": trace_ids,
            "session_boundary_count": len(session_boundaries),
            "session_boundaries": session_boundaries,
        }
    if issues:
        status = "BLOCK"
        trace_format = "invalid"
    elif legacy_lines:
        status = legacy_status
        trace_format = "legacy_unverified"
        issues = ["workflow trace predates the hash-chain ledger and cannot be checked for ledger self-consistency"]
    else:
        status = "READY"
        trace_format = "hash_chain_v1"
    return entries, {
        "status": status,
        "format": trace_format,
        "issues": issues,
        "verified_entries": ledger_lines,
        "last_entry_hash": expected_previous_hash,
        "trace_id": resolved_trace_id,
        "trace_ids": trace_ids,
        "session_boundary_count": len(session_boundaries),
        "session_boundaries": session_boundaries,
    }


@contextmanager
def _trace_file_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + 5
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out acquiring workflow trace lock: {lock_path}")
            time.sleep(0.05)
    try:
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        yield
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _trace_integrity_next_actions(integrity: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        next_action(
            "stop_session",
            "Stop the managed Vivado session before restarting the MCP server with a new trace ledger.",
            required_args=[],
            arg_sources={},
            preconditions=[f"Workflow trace integrity is BLOCK: {integrity.get('issues', [])}"],
            stop_condition="stop_session confirms no managed Vivado process remains; do not use the corrupt trace for handoff.",
        )
    ]
