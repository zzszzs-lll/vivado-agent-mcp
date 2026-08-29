from __future__ import annotations

from typing import Any


def next_action(
    tool: str,
    reason: str,
    *,
    required_args: list[str] | None = None,
    arg_sources: dict[str, str] | None = None,
    preconditions: list[str] | None = None,
    stop_condition: str = "",
    optional: bool = False,
) -> dict[str, Any]:
    return {
        "tool": tool,
        "reason": reason,
        "required_args": list(required_args or []),
        "arg_sources": dict(arg_sources or {}),
        "preconditions": list(preconditions or []),
        "stop_condition": stop_condition,
        "optional": optional,
    }


def dedupe_next_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for action in actions:
        key = (
            str(action.get("tool", "")),
            tuple(str(item) for item in action.get("required_args", [])),
            str(action.get("reason", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(action)
    return unique
