from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .env import find_vivado
from .hardware_boundary import hardware_validation_boundary
from .parsers import parse_messages
from .tcl import tcl_list_quote
from .wire import decode_wire_row, tcl_wire_prelude


HARDWARE_MODE_ENV = "VIVADO_AGENT_MCP_HARDWARE_MODE"


def hardware_server_policy() -> dict[str, Any]:
    server_mode = os.environ.get(HARDWARE_MODE_ENV, "no_board").strip().lower()
    return {
        "server_policy_env": HARDWARE_MODE_ENV,
        "server_hardware_mode": server_mode,
        "default_hardware_mode": "no_board",
        "hardware_tools_disabled_by_default": server_mode != "enabled",
        "requires_tool_hardware_mode": "enabled",
        "requires_server_hardware_mode": "enabled",
    }


def detect_hardware_environment(
    vivado_path: str | None = None,
    *,
    trusted_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    vivado = find_vivado(vivado_path, trusted_identity=trusted_identity)
    server_policy = hardware_server_policy()
    if not vivado.get("ok"):
        return {
            "ok": False,
            "error_code": str(vivado.get("error_code") or "VIVADO_ENVIRONMENT_UNAVAILABLE"),
            "message": str(vivado.get("message") or "Trusted Vivado executable is unavailable."),
            "path": None,
            "install_bin": None,
            "version": None,
            "hardware_tool_tier": "hardware_safe_detector",
            "server_policy": server_policy,
            "hardware_validation": hardware_validation_boundary(),
            "hardware_manager_tcl": False,
            "tools": _missing_hardware_tools(),
            "searched": vivado.get("searched", []),
            "execution_attempted": bool(vivado.get("execution_attempted")),
        }

    bin_dir = Path(str(vivado["install_bin"]))
    version = vivado.get("version")
    tools = {
        "vivado": vivado["tools"]["vivado"],
        "hw_server": _tool_info(_find_companion_tool(bin_dir, "hw_server")),
        "xsdb": _tool_info(_find_companion_tool(bin_dir, "xsdb")),
    }
    return {
        "ok": True,
        "path": vivado.get("path"),
        "install_bin": str(bin_dir),
        "version": version,
        "hardware_tool_tier": "hardware_safe_detector",
        "server_policy": server_policy,
        "hardware_validation": hardware_validation_boundary(),
        "hardware_manager_tcl": True,
        "tools": tools,
        "hw_server_available": tools["hw_server"]["available"],
        "xsdb_available": tools["xsdb"]["available"],
        "searched": vivado.get("searched", []),
    }


def open_hardware_manager_command() -> str:
    return "open_hw_manager"


def close_hardware_manager_command() -> str:
    return "close_hw_manager"


def connect_hw_server_command(*, host: str = "localhost", port: int = 3121) -> str:
    return f"connect_hw_server -url {tcl_list_quote(f'{host}:{port}')}"


def disconnect_hw_server_command() -> str:
    return "disconnect_hw_server"


def list_hw_targets_command() -> str:
    return (
        f"{tcl_wire_prelude()}; "
        "set rows [list]; "
        "foreach target [get_hw_targets -quiet *] {"
        "set is_open 0; catch {set is_open [get_property IS_OPEN $target]}; "
        "set state \"\"; catch {set state [get_property STATUS $target]}; "
        "lappend rows [::vivado_agent_mcp_wire_row [list target $target is_open $is_open state $state]]"
        "}; "
        "if {[llength $rows] == 0} {error \"No hardware targets exist\"}; "
        "join $rows \"\\n\""
    )


def open_hw_target_command(*, target: str | None = None, index: int = 0) -> str:
    target_ref = tcl_list_quote(target) if target else "*"
    return (
        f"{tcl_wire_prelude()}; "
        f"set targets [get_hw_targets -quiet {target_ref}]; "
        "if {[llength $targets] == 0} {error \"No hardware targets exist\"}; "
        f"open_hw_target [lindex $targets {index}]; "
        f"set opened_target [lindex $targets {index}]; "
        "::vivado_agent_mcp_wire_row [list target $opened_target is_open 1 state open]"
    )


def close_hw_target_command(*, target: str | None = None) -> str:
    target_ref = tcl_list_quote(target) if target else "*"
    return (
        f"{tcl_wire_prelude()}; "
        f"set targets [get_hw_targets -quiet {target_ref}]; "
        "if {[llength $targets] == 0} {error \"No hardware targets exist\"}; "
        "set target_obj [lindex $targets 0]; "
        "close_hw_target $target_obj; "
        "::vivado_agent_mcp_wire_row [list target $target_obj is_open 0 state closed]"
    )


def list_hw_devices_command() -> str:
    return (
        f"{tcl_wire_prelude()}; "
        "set rows [list]; "
        "foreach device [get_hw_devices -quiet *] {"
        "set part \"\"; catch {set part [get_property PART $device]}; "
        "set probes_file \"\"; catch {set probes_file [get_property PROBES.FILE $device]}; "
        "lappend rows [::vivado_agent_mcp_wire_row [list device $device part $part programmed [get_property IS_PROGRAMMED $device] probes_file $probes_file]]"
        "}; "
        "if {[llength $rows] == 0} {error \"No hardware devices found\"}; "
        "join $rows \"\\n\""
    )


def select_hw_device_command(
    *,
    device: str | None = None,
    part: str | None = None,
    index: int | None = None,
) -> str:
    parts = [_device_setup_command(device=device, part=part, index=index)]
    parts.extend(["current_hw_device $device_obj", _device_status_lines_command()])
    return "; ".join(parts)


def get_hw_device_status_command(*, device: str | None = None) -> str:
    return "; ".join([_device_setup_command(device=device), _device_status_lines_command()])


def program_hw_device_command(
    *,
    bitstream_path: str,
    ltx_path: str | None = None,
    device: str | None = None,
    target: str | None = None,
) -> str:
    parts: list[str] = []
    if target:
        parts.append(open_hw_target_command(target=target))
    parts.append(_device_setup_command(device=device))
    parts.append(f"set_property PROGRAM.FILE {tcl_list_quote(bitstream_path)} $device_obj")
    if ltx_path:
        parts.append(f"set_property PROBES.FILE {tcl_list_quote(ltx_path)} $device_obj")
    parts.extend(
        [
            "program_hw_devices $device_obj",
            "refresh_hw_device $device_obj",
            _device_status_lines_command(),
        ]
    )
    return "; ".join(parts)


def hardware_messages_command() -> str:
    return (
        "set content \"\"; "
        "set log_files [list]; "
        "foreach pattern [list [file join [pwd] vivado.log] [file join [pwd] *.jou] [file join [pwd] *.log]] {"
        "foreach f [glob -nocomplain $pattern] {if {[lsearch -exact $log_files $f] < 0} {lappend log_files $f}}"
        "}; "
        "foreach log_path $log_files {"
        "if {[file exists $log_path]} {"
        "set fh [open $log_path r]; "
        "seek $fh 0 end; "
        "set size [tell $fh]; "
        "set start [expr {$size > 1048576 ? $size - 1048576 : 0}]; "
        "seek $fh $start start; "
        "append content \"\\n__VMCP_HW_LOG_FILE__=$log_path\\n\"; "
        "append content [read $fh]; "
        "close $fh"
        "}"
        "}; "
        "set content"
    )


def validate_bitstream_path(path: str | Path) -> Path:
    bitstream = Path(path)
    if bitstream.suffix.lower() != ".bit":
        raise ValueError("bitstream_path must point to a .bit file")
    if not bitstream.exists():
        raise FileNotFoundError(str(bitstream))
    return bitstream


def validate_ltx_path(path: str | Path) -> Path:
    probes = Path(path)
    if probes.suffix.lower() != ".ltx":
        raise ValueError("ltx_path must point to a .ltx file")
    if not probes.exists():
        raise FileNotFoundError(str(probes))
    return probes


def select_programming_artifacts(manifest: dict[str, Any], *, manifest_path: str | Path = "") -> dict[str, Any]:
    artifacts = list(manifest.get("artifacts", []))
    bitstream = _select_artifact_path(artifacts, category="bitstream", suffix=".bit")
    if bitstream is None:
        raise ValueError("artifact manifest does not contain a bitstream artifact")
    ltx = _select_artifact_path(artifacts, category="debug_probes", suffix=".ltx")
    return {
        "manifest_path": str(manifest_path),
        "bitstream_path": str(bitstream),
        "ltx_path": str(ltx) if ltx is not None else None,
    }


def select_programming_artifacts_from_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return select_programming_artifacts(manifest, manifest_path=manifest_path)


def parse_hw_targets(raw: str) -> dict[str, Any]:
    targets = []
    for line in raw.splitlines():
        values = _parse_delimited_fields(line)
        if not values.get("target"):
            continue
        targets.append(
            {
                "name": values.get("target", ""),
                "target": values.get("target", ""),
                "is_open": _truthy(values.get("is_open", "")),
                "state": values.get("state", ""),
            }
        )
    return {"ok": True, "count": len(targets), "targets": targets, "raw": raw}


def parse_hw_devices(raw: str) -> dict[str, Any]:
    devices = []
    for line in raw.splitlines():
        values = _parse_delimited_fields(line)
        if not values.get("device"):
            continue
        devices.append(
            {
                "name": values.get("device", ""),
                "device": values.get("device", ""),
                "part": values.get("part", ""),
                "programmed": _truthy(values.get("programmed", "")),
                "program_file": values.get("program_file", ""),
                "probes_file": values.get("probes_file", ""),
            }
        )
    return {"ok": True, "count": len(devices), "devices": devices, "raw": raw}


def parse_programming_result(raw: str) -> dict[str, Any]:
    rows = [line.strip() for line in raw.splitlines() if line.strip().startswith("vmcp_hex_row_v1:")]
    if len(rows) != 1:
        raise ValueError("Programming status requires exactly one versioned wire row")
    values = decode_wire_row(rows[0], allow_legacy=False)
    messages = parse_hardware_messages(raw)
    programmed = _truthy(values.get("programmed", values.get("is_programmed", "")))
    status = "PROGRAMMED" if programmed else "NOT_PROGRAMMED"
    if messages["counts"]["ERROR"]:
        status = "FAILED"
    return {
        "ok": True,
        "status": status,
        "device": values.get("device", ""),
        "part": values.get("part", ""),
        "programmed": programmed,
        "program_file": values.get("program_file", ""),
        "probes_file": values.get("probes_file", ""),
        "hw_cfgmem": values.get("hw_cfgmem", ""),
        "messages": messages,
        "raw": raw,
        "raw_excerpt": _excerpt(raw),
    }


def parse_hardware_messages(raw: str) -> dict[str, Any]:
    parsed = parse_messages(raw)
    parsed["raw"] = raw
    parsed["raw_excerpt"] = _excerpt(raw)
    return parsed


def parse_hardware_error_code(raw: str) -> str:
    text = raw.lower()
    if "no hardware targets" in text or "no hw target" in text or "no targets" in text:
        return "NO_HW_TARGET"
    if "no hardware devices" in text or "no devices" in text or "device not found" in text:
        return "NO_HW_DEVICE"
    if "connect_hw_server" in text or "hw_server" in text and ("failed" in text or "refused" in text):
        return "HW_SERVER_CONNECT_FAILED"
    if "open_hw_target" in text or "failed to open target" in text:
        return "HW_TARGET_OPEN_FAILED"
    if "program_hw_devices" in text or "programming" in text or "startup status" in text:
        return "HW_PROGRAM_FAILED"
    return "TCL_FAILED"


def _device_setup_command(
    *,
    device: str | None = None,
    part: str | None = None,
    index: int | None = None,
) -> str:
    if device:
        return (
            f"set devices [get_hw_devices -quiet {tcl_list_quote(device)}]; "
            "if {[llength $devices] == 0} {error \"No hardware devices found\"}; "
            "set device_obj [lindex $devices 0]"
        )
    if part:
        return (
            "set device_obj \"\"; "
            "foreach candidate [get_hw_devices -quiet *] {"
            f"if {{[string equal [get_property PART $candidate] {tcl_list_quote(part)}]}} {{set device_obj $candidate; break}}"
            "}; "
            "if {$device_obj eq \"\"} {error \"No hardware devices found\"}"
        )
    if index is not None:
        return (
            f"set device_obj [lindex [get_hw_devices -quiet *] {index}]; "
            "if {$device_obj eq \"\"} {error \"No hardware devices found\"}"
        )
    return (
        "set device_obj \"\"; "
        "catch {set device_obj [current_hw_device]}; "
        "if {$device_obj eq \"\"} {set device_obj [lindex [get_hw_devices -quiet *] 0]}; "
        "if {$device_obj eq \"\"} {error \"No hardware devices found\"}"
    )


def _device_status_lines_command() -> str:
    return (
        f"{tcl_wire_prelude()}; "
        "set part \"\"; catch {set part [get_property PART $device_obj]}; "
        "set program_file \"\"; catch {set program_file [get_property PROGRAM.FILE $device_obj]}; "
        "set probes_file \"\"; catch {set probes_file [get_property PROBES.FILE $device_obj]}; "
        "set hw_cfgmem \"\"; catch {set hw_cfgmem [get_property PROGRAM.HW_CFGMEM $device_obj]}; "
        "set programmed 0; catch {set programmed [get_property IS_PROGRAMMED $device_obj]}; "
        "::vivado_agent_mcp_wire_row [list "
        "device $device_obj "
        "part $part "
        "programmed $programmed "
        "program_file $program_file "
        "probes_file $probes_file "
        "hw_cfgmem $hw_cfgmem]"
    )


def _find_companion_tool(bin_dir: Path, name: str) -> Path | None:
    suffixes = [".bat", ".exe", ""] if os.name == "nt" else ["", ".sh"]
    for suffix in suffixes:
        candidate = bin_dir / f"{name}{suffix}"
        if candidate.exists():
            return candidate.resolve()
    which = shutil.which(name) or shutil.which(f"{name}.bat")
    return Path(which).resolve() if which else None


def _tool_info(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"available": False, "path": None, "version": None}
    return {"available": True, "path": str(path), "version": None}


def _missing_hardware_tools() -> dict[str, dict[str, Any]]:
    return {
        name: {"available": False, "path": None, "version": None}
        for name in ("vivado", "hw_server", "xsdb")
    }


def _select_artifact_path(artifacts: list[dict[str, Any]], *, category: str, suffix: str) -> Path | None:
    for artifact in artifacts:
        candidate_category = str(artifact.get("category", ""))
        path = artifact.get("export_path") or artifact.get("source_path")
        if not path:
            continue
        candidate = Path(str(path))
        if candidate_category == category or candidate.suffix.lower() == suffix:
            return candidate
    return None


def _parse_delimited_fields(line: str) -> dict[str, str]:
    return decode_wire_row(line, allow_legacy=False)


def _truthy(value: str) -> bool:
    text = str(value).strip().lower()
    return text not in {"", "0", "false", "no", "{}"}


def _excerpt(text: str, limit: int = 4096) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]
