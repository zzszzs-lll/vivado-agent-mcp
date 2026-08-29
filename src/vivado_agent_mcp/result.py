from __future__ import annotations

from typing import Any


RESPONSE_SCHEMA_VERSION = 1
_BLOCK_STATUSES = {"BLOCK", "FAILED", "FAIL", "ERROR", "STALE", "TAINTED", "INVALID"}
_WARN_STATUSES = {"WARN", "WARNING", "READY_WITH_WAIVERS"}
_READY_STATUSES = {"READY", "PASS", "PASSED", "COMPLETED", "COMPLETE", "CLEANED"}
def success(tool: str, summary: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    payload_data = data or {}
    result = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "ok": True,
        "tool": tool,
        "summary": summary,
        "message": summary,
        "error_code": "",
        "data": payload_data,
    }
    _promote_common_fields(result, payload_data)
    return result


def failure(
    tool: str,
    error_code: str,
    message: str,
    raw_excerpt: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload_data = data or {}
    result = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "ok": False,
        "tool": tool,
        "error_code": error_code,
        "summary": message,
        "message": message,
        "raw_excerpt": raw_excerpt[:4000],
        "data": payload_data,
    }
    _promote_common_fields(result, payload_data)
    return result


def _promote_common_fields(result: dict[str, Any], data: dict[str, Any]) -> None:
    for key in ("next_steps", "next_actions", "hardware_validation", "resume_context", "handoff_reviewable", "policy_allowed"):
        value = data.get(key)
        if value is not None:
            result[key] = value
    assessment_status = _assessment_status(data, ok=bool(result.get("ok")))
    result["assessment_status"] = assessment_status
    result["stop_required"] = assessment_status in {"WARN", "BLOCK"}
    result["handoff_ready"] = bool(result.get("ok")) and assessment_status == "READY" and _handoff_ready(data)
    if "handoff_reviewable" in result:
        result["handoff_reviewable"] = bool(result["handoff_reviewable"]) and assessment_status != "BLOCK"


def _assessment_status(data: dict[str, Any], *, ok: bool) -> str:
    if not ok:
        return "BLOCK"
    observed = list(_assessment_nodes(data))
    if any(status == "BLOCK" for status in observed):
        return "BLOCK"
    if any(status == "WARN" for status in observed):
        return "WARN"
    if any(status == "READY" for status in observed):
        return "READY"
    return "NOT_APPLICABLE"


def _assessment_nodes(node: Any, seen: set[int] | None = None) -> list[str]:
    if not isinstance(node, (dict, list)):
        return []
    visited = seen if seen is not None else set()
    identity = id(node)
    if identity in visited:
        return []
    visited.add(identity)
    if isinstance(node, list):
        statuses: list[str] = []
        for item in node:
            statuses.extend(_assessment_nodes(item, visited))
        return statuses

    statuses: list[str] = []
    if node.get("ok") is False or node.get("policy_allowed") is False:
        statuses.append("BLOCK")
    hardware = node.get("hardware_validation")
    if isinstance(hardware, dict) and hardware:
        if str(hardware.get("status", "")).strip().upper() != "NOT_VALIDATED" or hardware.get("validated") is not False:
            statuses.append("BLOCK")
    for key in ("assessment_status", "effective_status", "status"):
        normalized = str(node.get(key) or "").strip().upper()
        if normalized in _BLOCK_STATUSES:
            statuses.append("BLOCK")
        elif normalized in _WARN_STATUSES:
            statuses.append("WARN")
        elif normalized in _READY_STATUSES:
            statuses.append("READY")
    for child in node.values():
        if isinstance(child, (dict, list)):
            statuses.extend(_assessment_nodes(child, visited))
    return statuses


def _handoff_ready(data: dict[str, Any]) -> bool:
    if isinstance(data.get("handoff_ready"), bool):
        return bool(data["handoff_ready"])
    for key in ("resume_context", "health"):
        nested = data.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("handoff_ready"), bool):
            return bool(nested["handoff_ready"])
    return False
