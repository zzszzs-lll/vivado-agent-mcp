from __future__ import annotations

from typing import Any

from .parsers import parse_messages
from .tcl import tcl_list_quote
from .wire import (
    HEX_ROW_PREFIX,
    WIRE_TRUST_VERSIONED,
    decode_wire_row,
    tcl_wire_prelude,
)

IP_STATUS_MARKER = "__VMCP_IP_STATUS_BEGIN__"


def create_ip_command(
    *,
    vlnv: str,
    module_name: str,
    ip_dir: str | None = None,
    properties: dict[str, Any] | None = None,
) -> str:
    parts = [f"create_ip -vlnv {tcl_list_quote(vlnv)} -module_name {tcl_list_quote(module_name)}"]
    if ip_dir:
        parts[0] = f"{parts[0]} -dir {tcl_list_quote(ip_dir)}"
    if properties:
        parts.append(
            f"set_property -dict {_property_list(properties, config_keys=True)} [get_ips {tcl_list_quote(module_name)}]"
        )
    parts.append(ip_status_command(ip_name=module_name, include_report=False))
    return "; ".join(parts)


def configure_ip_command(*, ip_name: str, properties: dict[str, Any]) -> str:
    parts = [
        _ip_setup(ip_name),
        f"set_property -dict {_property_list(properties, config_keys=True)} $ip_obj",
        ip_status_command(ip_name=ip_name),
    ]
    return "; ".join(parts)


def generate_ip_targets_command(*, ip_name: str, targets: list[str] | None = None) -> str:
    target_expr = _target_expr(targets or ["all"])
    return "; ".join(
        [
            _ip_setup(ip_name),
            _ip_file_setup(),
            f"generate_target {target_expr} $ip_files",
            ip_status_command(ip_name=ip_name),
        ]
    )


def ip_status_command(*, ip_name: str, include_report: bool = True) -> str:
    report = (
        "if {[catch {report_ip_status -return_string} report]} {set report \"\"}"
        if include_report
        else "set report \"\""
    )
    return (
        f"{tcl_wire_prelude()}; {_ip_setup(ip_name)}; "
        "set locked \"\"; catch {set locked [get_property IS_LOCKED $ip_obj]}; "
        "set upgrade_versions \"\"; catch {set upgrade_versions [get_property UPGRADE_VERSIONS $ip_obj]}; "
        f"{report}; "
        "join [list "
        "[::vivado_agent_mcp_wire_row [list name $ip_name xci_path $xci_path locked $locked upgrade_available [expr {$upgrade_versions ne \"\"}]]] "
        f"\"report_begin={IP_STATUS_MARKER}\" "
        "$report"
        "] \"\\n\""
    )


def upgrade_ip_command(*, ip_name: str) -> str:
    return "; ".join([_ip_setup(ip_name), "upgrade_ip $ip_obj", ip_status_command(ip_name=ip_name)])


def export_ip_user_files_command(*, ip_name: str) -> str:
    return "; ".join(
        [
            _ip_setup(ip_name),
            _ip_file_setup(),
            "export_ip_user_files -of_objects $ip_files -no_script -sync -force -quiet",
            ip_status_command(ip_name=ip_name),
        ]
    )


def parse_ip_status(raw: str) -> dict[str, Any]:
    metadata_text, report_text = _split_metadata_and_report(raw)
    metadata_lines = [line.strip() for line in metadata_text.splitlines() if line.strip()]
    if len(metadata_lines) == 1 and metadata_lines[0].startswith(HEX_ROW_PREFIX):
        metadata = decode_wire_row(metadata_lines[0], allow_legacy=False)
        wire_trust = WIRE_TRUST_VERSIONED
    else:
        raise ValueError("IP status requires exactly one versioned wire row")
    messages = parse_messages(report_text)
    return {
        "ok": True,
        "name": metadata.get("name", ""),
        "xci_path": metadata.get("xci_path", ""),
        "locked": _truthy(metadata.get("locked", "")),
        "upgrade_available": _truthy(metadata.get("upgrade_available", "")),
        "wire_trust": wire_trust,
        "messages": messages,
        "raw": raw,
        "raw_excerpt": _excerpt(report_text or raw),
    }


def _ip_setup(ip_name: str) -> str:
    ip_ref = tcl_list_quote(ip_name)
    return (
        f"set ip_obj [get_ips -quiet {ip_ref}]; "
        f"set ip_name {ip_ref}; "
        "if {[llength $ip_obj] == 0} {error \"Vivado IP not found\"}; "
        "set ip_obj [lindex $ip_obj 0]; "
        "set xci_path \"\"; "
        "catch {set xci_path [get_property IP_FILE $ip_obj]}; "
        "if {$xci_path eq \"\"} {"
        "set candidates [get_files -quiet *.xci]; "
        "foreach f $candidates {if {[string match \"*$ip_name.xci\" $f]} {set xci_path $f}}"
        "}"
    )


def _ip_file_setup() -> str:
    return (
        "set ip_files [get_files -quiet $xci_path]; "
        "if {[llength $ip_files] == 0 && $xci_path ne \"\"} {set ip_files [list $xci_path]}; "
        "if {[llength $ip_files] == 0} {error \"Vivado IP XCI file not found\"}"
    )


def _property_list(properties: dict[str, Any], *, config_keys: bool) -> str:
    values: list[str] = []
    for key, value in properties.items():
        prop = _config_key(key) if config_keys else str(key)
        values.extend([prop, _property_value(value)])
    return "[list " + " ".join(tcl_list_quote(value) for value in values) + "]"


def _config_key(key: str) -> str:
    text = str(key)
    return text if text.startswith("CONFIG.") else f"CONFIG.{text}"


def _property_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _target_expr(targets: list[str]) -> str:
    if len(targets) == 1:
        return tcl_list_quote(targets[0])
    return "[list " + " ".join(tcl_list_quote(target) for target in targets) + "]"


def _split_metadata_and_report(raw: str) -> tuple[str, str]:
    marker_line = f"report_begin={IP_STATUS_MARKER}"
    if marker_line not in raw:
        return raw, ""
    metadata, report = raw.split(marker_line, 1)
    return metadata, report.lstrip("\r\n")


def _truthy(value: str) -> bool:
    text = value.strip().lower()
    return text not in {"", "0", "false", "no", "{}"}


def _excerpt(text: str, limit: int = 4096) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]
