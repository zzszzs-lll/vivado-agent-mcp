from __future__ import annotations

from typing import Any

from .parsers import parse_messages
from .tcl import tcl_list_quote
from .wire import decode_wire_list, tcl_wire_prelude

BD_VALIDATE_MARKER = "__VMCP_BD_VALIDATE_BEGIN__"


def create_block_design_command(*, name: str, force: bool = False) -> str:
    parts: list[str] = [f"set bd_name {tcl_list_quote(name)}"]
    if force:
        parts.extend(
            [
                "set open_bds [get_bd_designs -quiet $bd_name]",
                "if {[llength $open_bds] > 0} {close_bd_design $bd_name}",
                "set old_bd_files [get_files -quiet [format {*/%s.bd} $bd_name]]",
                "if {[llength $old_bd_files] > 0} {remove_files $old_bd_files}",
            ]
        )
    parts.extend(
        [
            f"create_bd_design {tcl_list_quote(name)}",
            f"current_bd_design {tcl_list_quote(name)}",
            "save_bd_design",
            _bd_file_setup("$bd_name"),
            _bd_summary_command("$bd_name", already_variable=True),
        ]
    )
    return "; ".join(parts)


def open_block_design_command(*, name: str) -> str:
    return "; ".join(
        [
            f"set bd_name {tcl_list_quote(name)}",
            _bd_file_setup("$bd_name"),
            "open_bd_design $bd_file",
            "current_bd_design $bd_name",
            _bd_summary_command("$bd_name", already_variable=True),
        ]
    )


def add_bd_ip_cell_command(
    *,
    vlnv: str,
    cell_name: str,
    properties: dict[str, Any] | None = None,
) -> str:
    parts = [
        f"create_bd_cell -type ip -vlnv {tcl_list_quote(vlnv)} {tcl_list_quote(cell_name)}",
    ]
    if properties:
        parts.append(
            f"set_property -dict {_property_list(properties, config_keys=True)} [get_bd_cells {tcl_list_quote(cell_name)}]"
        )
    parts.extend(["save_bd_design", f"set cell_name {tcl_list_quote(cell_name)}; set cell_name"])
    return "; ".join(parts)


def create_bd_port_command(
    *,
    name: str,
    direction: str,
    port_type: str | None = None,
    from_index: int | None = None,
    to_index: int | None = None,
    properties: dict[str, Any] | None = None,
) -> str:
    args = [f"create_bd_port -dir {tcl_list_quote(direction)}"]
    if port_type:
        args.append(f"-type {tcl_list_quote(port_type)}")
    if from_index is not None:
        args.append(f"-from {tcl_list_quote(from_index)}")
    if to_index is not None:
        args.append(f"-to {tcl_list_quote(to_index)}")
    args.append(tcl_list_quote(name))
    parts = [" ".join(args)]
    if properties:
        parts.append(f"set_property -dict {_property_list(properties, config_keys=False)} [get_bd_ports {tcl_list_quote(name)}]")
    parts.extend(["save_bd_design", f"set port_name {tcl_list_quote(name)}; set port_name"])
    return "; ".join(parts)


def connect_bd_net_command(*, source: str, targets: list[str]) -> str:
    target_values = " ".join(tcl_list_quote(target) for target in targets)
    return "; ".join(
        [
            tcl_wire_prelude(),
            _bd_resolver_proc(),
            f"set source_obj [::vmcp_resolve_bd_obj {tcl_list_quote(source)}]",
            "set target_objs [list]",
            f"foreach target [list {target_values}] {{lappend target_objs [::vmcp_resolve_bd_obj $target]}}",
            "connect_bd_net $source_obj $target_objs",
            "save_bd_design",
            "join [list \"source=$source_obj\" \"targets=[::vivado_agent_mcp_wire_list $target_objs]\"] \"\\n\"",
        ]
    )


def connect_bd_intf_net_command(*, source: str, targets: list[str]) -> str:
    target_values = " ".join(tcl_list_quote(target) for target in targets)
    return "; ".join(
        [
            tcl_wire_prelude(),
            _bd_resolver_proc(),
            f"set source_obj [::vmcp_resolve_bd_obj {tcl_list_quote(source)}]",
            "set target_objs [list]",
            f"foreach target [list {target_values}] {{lappend target_objs [::vmcp_resolve_bd_obj $target]}}",
            "foreach target_obj $target_objs {connect_bd_intf_net $source_obj $target_obj}",
            "save_bd_design",
            "join [list \"source=$source_obj\" \"targets=[::vivado_agent_mcp_wire_list $target_objs]\"] \"\\n\"",
        ]
    )


def validate_block_design_command(*, bd_name: str | None = None) -> str:
    parts = []
    if bd_name:
        parts.extend(
            [
                f"set bd_name {tcl_list_quote(bd_name)}",
                "set bd_files [get_files -quiet [format {*/%s.bd} $bd_name]]",
                "if {[llength $bd_files] != 1} {error [format {Expected exactly one Block Design file for %s, found %d} $bd_name [llength $bd_files]]}",
                "set bd_file [lindex $bd_files 0]",
                "open_bd_design $bd_file",
                "current_bd_design $bd_name",
            ]
        )
    else:
        parts.extend(
            [
                "set bd_name [current_bd_design -quiet]",
                "if {$bd_name eq {}} {error {No current Block Design is open}}",
                "set bd_file [lindex [get_files -quiet [format {*/%s.bd} $bd_name]] 0]",
            ]
        )
    parts.append("set status VALID")
    parts.append("set raw \"\"")
    parts.append("if {[catch {validate_bd_design} raw]} {set status INVALID}")
    parts.append(
        f"join [list \"bd_name=$bd_name\" \"bd_file=$bd_file\" \"status=$status\" \"raw_begin={BD_VALIDATE_MARKER}\" $raw] \"\\n\""
    )
    return "; ".join(parts)


def generate_block_design_wrapper_command(
    *,
    bd_name: str,
    wrapper_top: str | None = None,
    set_top: bool = True,
) -> str:
    top = wrapper_top or f"{bd_name}_wrapper"
    parts = [
        tcl_wire_prelude(),
        f"set bd_name {tcl_list_quote(bd_name)}",
        _bd_file_setup("$bd_name"),
        "make_wrapper -files $bd_file -top",
        "set p [current_project]",
        "set project_dir [get_property DIRECTORY $p]",
        "set project_name [get_property NAME $p]",
        "set wrapper_files [glob -nocomplain [file join $project_dir ${project_name}.gen sources_1 bd $bd_name hdl *wrapper.v]]",
        "if {[llength $wrapper_files] == 0} {set wrapper_files [glob -nocomplain [file join $project_dir ${project_name}.gen sources_1 bd $bd_name hdl *wrapper.vhd]]}",
        "if {[llength $wrapper_files] == 0} {error \"Block design wrapper was not generated\"}",
        "add_files -norecurse $wrapper_files",
    ]
    if set_top:
        parts.extend(
            [
                f"set_property top {tcl_list_quote(top)} [current_fileset]",
                "update_compile_order -fileset sources_1",
            ]
        )
    parts.append("join [list \"wrapper_files=[::vivado_agent_mcp_wire_list $wrapper_files]\" \"top=[get_property TOP [current_fileset]]\"] \"\\n\"")
    return "; ".join(parts)


def parse_block_design_validation(raw: str) -> dict[str, Any]:
    metadata_text, report_text = _split_metadata_and_report(raw)
    metadata = _parse_key_value_lines(metadata_text)
    messages = parse_messages(report_text)
    status = metadata.get("status", "UNKNOWN")
    if messages["counts"].get("ERROR", 0):
        status = "INVALID"
    return {
        "ok": True,
        "bd_name": metadata.get("bd_name", ""),
        "bd_file": metadata.get("bd_file", ""),
        "status": status,
        "messages": messages,
        "raw": raw,
        "raw_excerpt": _excerpt(report_text or raw),
    }


def parse_key_value_result(raw: str) -> dict[str, Any]:
    values: dict[str, Any] = _parse_key_value_lines(raw)
    for key in ("targets", "wrapper_files"):
        if key in values:
            values[key] = decode_wire_list(str(values[key]))
    return values


def _bd_file_setup(name_expr: str) -> str:
    if name_expr.startswith("$"):
        file_pattern = f"[format {{*/%s.bd}} ${{{name_expr[1:]}}}]"
    else:
        file_pattern = tcl_list_quote(f"*/{name_expr}.bd")
    return (
        f"set bd_file [lindex [get_files -quiet {file_pattern}] 0]; "
        "if {$bd_file eq \"\"} {error \"Block design file not found\"}"
    )


def _bd_summary_command(name: str, *, already_variable: bool = False) -> str:
    if already_variable:
        return "join [list [format {name=%s} $bd_name] [format {bd_file=%s} $bd_file]] \"\\n\""
    return (
        "join [list "
        f"{tcl_list_quote(f'name={name}')} "
        f"[format {{bd_file=%s}} [lindex [get_files -quiet {tcl_list_quote(f'*/{name}.bd')}] 0]]"
        "] \"\\n\""
    )


def _bd_resolver_proc() -> str:
    return (
        "proc ::vmcp_resolve_bd_obj {name} {"
        "foreach getter {get_bd_pins get_bd_ports get_bd_intf_pins get_bd_intf_ports get_bd_cells} {"
        "set obj [$getter -quiet $name]; "
        "if {[llength $obj] > 0} {return $obj}"
        "}; "
        "error \"BD object not found: $name\""
        "}"
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


def _split_metadata_and_report(raw: str) -> tuple[str, str]:
    marker_line = f"raw_begin={BD_VALIDATE_MARKER}"
    if marker_line not in raw:
        return raw, ""
    metadata, report = raw.split(marker_line, 1)
    return metadata, report.lstrip("\r\n")


def _parse_key_value_lines(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _excerpt(text: str, limit: int = 4096) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]
