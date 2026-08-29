from __future__ import annotations

import re
from typing import Any

from .agent_actions import next_action


PROJECT_WRITE_PATTERNS = (
    r"\bcreate_project\b",
    r"\bopen_project\b",
    r"\bclose_project\b",
    r"\badd_files\b",
    r"\bremove_files\b",
    r"\bset_property\b",
    r"\bcreate_bd_",
    r"\bconnect_bd_",
    r"\bmake_wrapper\b",
    r"\bgenerate_target\b",
    r"\blaunch_runs\b",
    r"\bopen_run\b",
    r"\bsynth_design\b",
    r"\bwrite_",
    r"\bfile\s+mkdir\b",
    r"\bset_param\b",
)

DESTRUCTIVE_PATTERNS = (
    r"\bfile\s+delete\b",
    r"\breset_run\b",
    r"\bdelete_project\b",
    r"\bdelete_files\b",
    r"\bremove_files\b",
    r"\bdelete_bd_",
)

HARDWARE_PATTERNS = (
    r"\bopen_hw\b",
    r"\bclose_hw\b",
    r"\bconnect_hw_server\b",
    r"\bdisconnect_hw_server\b",
    r"\bget_hw_",
    r"\bcurrent_hw_",
    r"\bset_property\s+program\.",
    r"\bprogram_hw_devices\b",
    r"\brefresh_hw_device\b",
)

EXTERNAL_PATTERNS = (
    r"\bexec\s+",
    r"\bsource\s+",
    r"\bopen\s+\|",
    r"\bsocket\s+",
    r"\bstart_gui\b",
    r"\bexit\b",
)

UNRESTRICTED_FLAGS = (
    "allow_project_write",
    "allow_destructive",
    "allow_hardware",
    "allow_external",
    "allow_unrestricted",
)
UNRESTRICTED_CONFIRM = "EXECUTE_UNRESTRICTED_TCL"

RAW_PROGRAMMING_PATTERNS = (
    r"\bprogram_hw_devices\b",
    r"\bprogram_hw_cfgmem\b",
    r"\bboot_hw_device\b",
)


def classify_tcl(command: str) -> dict[str, Any]:
    text = command.strip()
    lowered = text.lower()
    categories: list[str] = []
    required_flags: list[str] = []

    if _is_low_risk(text):
        return {
            "risk": "LOW",
            "categories": [],
            "required_flags": [],
            "command_excerpt": text[:500],
        }

    if _contains_any(lowered, EXTERNAL_PATTERNS):
        categories.append("external")
    if _contains_any(lowered, HARDWARE_PATTERNS):
        categories.append("hardware")
    if _contains_any(lowered, DESTRUCTIVE_PATTERNS):
        categories.append("destructive")
    if _contains_any(lowered, PROJECT_WRITE_PATTERNS):
        categories.append("project_write")
    categories.append("unrestricted")
    required_flags.extend(UNRESTRICTED_FLAGS)

    return {
        "risk": "UNRESTRICTED",
        "categories": sorted(set(categories)),
        "required_flags": sorted(set(required_flags)),
        "command_excerpt": text[:500],
    }


def tcl_policy_allows(command: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    classification = classify_tcl(command)
    missing_flags = [flag for flag in classification["required_flags"] if not bool(args.get(flag, False))]
    intent = str(args.get("execution_intent", "")).strip()
    confirm = str(args.get("confirm", "")).strip()
    if classification["risk"] != "LOW" and not intent:
        missing_flags.append("execution_intent")
    if classification["risk"] == "UNRESTRICTED" and confirm != UNRESTRICTED_CONFIRM:
        missing_flags.append("confirm")
    decision = {
        "policy": classification,
        "execution_intent": intent,
        "confirm": confirm,
        "missing": sorted(set(missing_flags)),
    }
    return not missing_flags, decision


def raw_tcl_programming_command(command: str) -> bool:
    """Return true for hardware programming commands reserved for dedicated tools."""

    normalized = _decode_backslash_character_escapes(command.lower())
    return _contains_any(normalized, RAW_PROGRAMMING_PATTERNS)


def tcl_policy_failure_data(tool: str, command: str, args: dict[str, Any]) -> dict[str, Any]:
    _, decision = tcl_policy_allows(command, args)
    required_args = ["execution_intent", *decision["policy"]["required_flags"]]
    arg_sources = {
        "execution_intent": "human-approved reason for this Tcl operation",
        **{
            flag: "set true only after reviewing the matching risk category"
            for flag in decision["policy"]["required_flags"]
        },
    }
    if decision["policy"]["risk"] == "UNRESTRICTED":
        required_args.append("confirm")
        arg_sources["confirm"] = f"set to {UNRESTRICTED_CONFIRM}"
    return {
        **decision,
        "dry_run": bool(args.get("dry_run", False)),
        "policy_allowed": False,
        "command": command,
        "next_actions": [
            next_action(
                tool,
                "Retry only after explicitly documenting intent and enabling the required Tcl policy flags.",
                required_args=required_args,
                arg_sources=arg_sources,
                preconditions=["The requested Tcl command has been reviewed against project and hardware safety boundaries."],
                stop_condition=f"{tool} returns ok=true or dry_run=true with an accepted policy.",
            )
        ],
    }


def tcl_dry_run_data(command: str, args: dict[str, Any]) -> dict[str, Any]:
    allowed, decision = tcl_policy_allows(command, args)
    return {
        **decision,
        "status": "DRY_RUN",
        "allowed": allowed,
        "policy_allowed": allowed,
        "dry_run": True,
        "command": command,
    }


def _is_low_risk(command: str) -> bool:
    text = command.strip()
    if not text:
        return True
    if any(token in text for token in (";", "\n", "\r", "\\", "[", "]", "$", "\x00")):
        return False
    lowered = text.lower()
    if "::vivado_agent_mcp_" in lowered or not _balanced_tcl_grouping(text):
        return False
    if _contains_any(lowered, HARDWARE_PATTERNS):
        return False
    command_name = text.split(maxsplit=1)[0].lower()
    if command_name == "version":
        return text.lower() in {"version", "version -short"}
    if command_name in {"current_project", "current_run"}:
        return len(text.split()) == 1
    if command_name == "list_property" or command_name.startswith("get_"):
        return True
    if command_name.startswith("report_"):
        lowered = text.lower()
        return "-return_string" in lowered and "-file" not in lowered and "-append" not in lowered
    return False


def _balanced_tcl_grouping(text: str) -> bool:
    brace_depth = 0
    quoted = False
    for character in text:
        if character == '"' and brace_depth == 0:
            quoted = not quoted
        elif not quoted and character == "{":
            brace_depth += 1
        elif not quoted and character == "}":
            brace_depth -= 1
            if brace_depth < 0:
                return False
    return brace_depth == 0 and not quoted


def _decode_backslash_character_escapes(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        try:
            if token.startswith(("x", "u", "U")):
                return chr(int(token[1:], 16))
            return chr(int(token, 8))
        except (ValueError, OverflowError):
            return match.group(0)

    return re.sub(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[0-7]{1,3})", replace, text)


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)
