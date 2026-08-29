from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_actions import dedupe_next_actions, next_action
from .hardware_boundary import hardware_validation_boundary
from .managed_path import atomic_write_bytes
from .parsers import parse_messages
from .runs import run_hook_guard_command
from .tcl import tcl_list_quote
from .wire import HEX_ROW_PREFIX, decode_wire_row, tcl_wire_prelude

SYNTAX_MARKER = "__VMCP_SYNTAX_REPORT_BEGIN__"
SYNTAX_END_MARKER = "__VMCP_SYNTAX_REPORT_END__"
ELABORATION_MARKER = "__VMCP_ELABORATION_REPORT_BEGIN__"
ELABORATION_END_MARKER = "__VMCP_ELABORATION_REPORT_END__"
COMPILE_ORDER_MARKER = "__VMCP_COMPILE_ORDER_BEGIN__"
COMPILE_ORDER_END_MARKER = "__VMCP_COMPILE_ORDER_END__"

REPORT_CATEGORIES = {
    "timing_summary.rpt": "timing",
    "utilization.rpt": "utilization",
    "drc.rpt": "drc",
    "methodology.rpt": "methodology",
    "qor_summary.rpt": "qor",
    "cdc.rpt": "cdc",
    "clock_interaction.rpt": "clock_interaction",
    "power.rpt": "power",
    "messages.log": "messages",
}
REQUIRED_REPORT_CATEGORIES = {"timing", "utilization", "drc", "methodology", "messages"}


def check_syntax_command(*, fileset: str = "sources_1") -> str:
    fileset_ref = tcl_list_quote(fileset)
    return (
        f"set fs [get_filesets {fileset_ref}]; "
        "set syntax_status READY; "
        "set syntax_raw \"\"; "
        f"if {{[catch {{check_syntax -fileset {fileset_ref}}} syntax_raw]}} {{set syntax_status BLOCK}}; "
        "join [list "
        "\"status=$syntax_status\" "
        "\"syntax_status=$syntax_status\" "
        f"\"fileset={tcl_list_quote(fileset)}\" "
        f"\"raw_begin={SYNTAX_MARKER}\" "
        "$syntax_raw "
        f"\"raw_end={SYNTAX_END_MARKER}\""
        "] \"\\n\""
    )


def compile_order_command(*, fileset: str = "sources_1") -> str:
    fileset_ref = tcl_list_quote(fileset)
    used_in = "simulation" if fileset == "sim_1" else "synthesis"
    return (
        f"{tcl_wire_prelude()}; "
        f"set fs [get_filesets {fileset_ref}]; "
        "set top \"\"; catch {set top [get_property TOP $fs]}; "
        f"set ordered [get_files -compile_order sources -quiet -used_in {used_in} -of_objects $fs]; "
        "set rows [list]; "
        "set seen [dict create]; "
        "set duplicates [list]; "
        "set index 0; "
        "foreach f $ordered {"
        "set path [file normalize $f]; "
        "set file_type \"\"; catch {set file_type [get_property FILE_TYPE $f]}; "
        "set managed 0; catch {set managed [get_property IS_MANAGED $f]}; "
        "set exists [expr {[file exists $path] ? 1 : 0}]; "
        "if {[dict exists $seen $path]} {lappend duplicates $path} else {dict set seen $path 1}; "
        "lappend rows [::vivado_agent_mcp_wire_row [list file $path type $file_type exists $exists managed $managed used_in "
        f"{used_in}"
        " order $index]]; "
        "incr index"
        "}; "
        "set duplicate_rows [list]; "
        "foreach d $duplicates {lappend duplicate_rows [::vivado_agent_mcp_wire_row [list duplicate $d]]}; "
        "set compile_order_status [expr {$index > 0 ? {READY} : {BLOCK}}]; "
        "join [concat [list "
        "\"status=$compile_order_status\" "
        "\"compile_order_schema=vivado_2021_2_v1\" "
        "\"compile_order_complete=1\" "
        "\"compile_order_count=$index\" "
        f"\"fileset={tcl_list_quote(fileset)}\" "
        "\"top=$top\" "
        f"\"raw_begin={COMPILE_ORDER_MARKER}\" "
        "] $rows $duplicate_rows [list "
        f"\"raw_end={COMPILE_ORDER_END_MARKER}\""
        "]] \"\\n\""
    )


def run_elaboration_command(*, top: str | None = None, part: str | None = None) -> str:
    top_arg = tcl_list_quote(top) if top else "$elab_top"
    part_arg = tcl_list_quote(part) if part else "$elab_part"
    parts = [
        run_hook_guard_command(),
        "set p [current_project]",
        "set elab_top \"\"",
        "catch {set elab_top [get_property TOP [get_filesets sources_1]]}",
        "set elab_part \"\"",
        "catch {set elab_part [get_property PART $p]}",
    ]
    if top:
        parts.append(f"set elab_top {tcl_list_quote(top)}")
    if part:
        parts.append(f"set elab_part {tcl_list_quote(part)}")
    parts.extend(
        [
            "if {$elab_top eq \"\"} {error \"Design top is not set\"}",
            "if {$elab_part eq \"\"} {error \"Project part is not set\"}",
            "set elab_status READY",
            "set elab_raw \"\"",
            "catch {close_design}",
            f"set elab_code [catch {{synth_design -rtl -name vmcp_elaborated_design -top {top_arg} -part {part_arg}}} elab_result]",
            "if {$elab_code} {set elab_status BLOCK; set elab_raw [string range $elab_result 0 65535]} else {set elab_raw \"\"}",
            "join [list "
            "\"status=$elab_status\" "
            "\"top=$elab_top\" "
            "\"part=$elab_part\" "
            f"\"raw_begin={ELABORATION_MARKER}\" "
            "$elab_raw "
            f"\"raw_end={ELABORATION_END_MARKER}\""
            "] \"\\n\"",
        ]
    )
    return "; ".join(parts)


def elaboration_result_command() -> str:
    return (
        "set status READY; "
        "set raw \"\"; "
        "if {[catch {report_design_analysis -return_string} raw]} {set raw \"\"}; "
        "join [list "
        "\"status=$status\" "
        f"\"raw_begin={ELABORATION_MARKER}\" "
        "$raw "
        f"\"raw_end={ELABORATION_END_MARKER}\""
        "] \"\\n\""
    )


def design_hierarchy_command() -> str:
    return (
        f"{tcl_wire_prelude()}; "
        "set rows [list]; "
        "foreach p [get_ports -quiet *] {"
        "set direction \"\"; catch {set direction [get_property DIRECTION $p]}; "
        "set left \"\"; catch {set left [get_property LEFT $p]}; "
        "set right \"\"; catch {set right [get_property RIGHT $p]}; "
        "set width 1; if {$left ne \"\" && $right ne \"\"} {set width [expr {abs($left - $right) + 1}]}; "
        "lappend rows [::vivado_agent_mcp_wire_row [list port $p direction $direction width $width]]"
        "}; "
        "foreach c [get_cells -hierarchical -quiet *] {"
        "set ref \"\"; catch {set ref [get_property REF_NAME $c]}; "
        "set primitive 0; catch {set primitive [get_property IS_PRIMITIVE $c]}; "
        "lappend rows [::vivado_agent_mcp_wire_row [list cell $c ref $ref primitive $primitive]]"
        "}; "
        "join $rows \"\\n\""
    )


def cdc_report_command() -> str:
    return _optional_report_command(command="report_cdc", unavailable_key="cdc_unavailable", message="report_cdc is unavailable in this Vivado version")


def clock_interaction_report_command() -> str:
    return _optional_report_command(
        command="report_clock_interaction",
        unavailable_key="clock_interaction_unavailable",
        message="report_clock_interaction is unavailable in this Vivado version",
    )


def power_report_command() -> str:
    return _optional_report_command(command="report_power", unavailable_key="power_unavailable", message="report_power is unavailable in this Vivado version")


def configuration_voltage_command() -> str:
    return (
        "set vmcp_cfgbvs \"\"; "
        "set vmcp_config_voltage \"\"; "
        "catch {set vmcp_cfgbvs [get_property CFGBVS [current_design]]}; "
        "catch {set vmcp_config_voltage [get_property CONFIG_VOLTAGE [current_design]]}; "
        "join [list \"cfgbvs=$vmcp_cfgbvs\" \"config_voltage=$vmcp_config_voltage\"] \"\\n\""
    )


def report_bundle_command(*, run_name: str = "impl_1", collection_id: str | None = None) -> str:
    run_ref = tcl_list_quote(run_name)
    collection_ref = tcl_list_quote(collection_id or f"report_{uuid.uuid4().hex}")
    expected_report_names = " ".join(tcl_list_quote(name) for name in REPORT_CATEGORIES)
    parts = [
        run_hook_guard_command(),
        "set p [current_project]",
        "set project_dir [file normalize [get_property DIRECTORY $p]]",
        f"set run_name {run_ref}",
        f"set collection_id {collection_ref}",
        "set collection_started_ms [clock milliseconds]",
        "set vivado_version_short [version -short]",
        "set vivado_build [string map {\\n { } \\r { }} [version]]",
        "set runs [get_runs -quiet $run_name]",
        "if {[llength $runs] != 1} {error {Requested report run must resolve to exactly one Vivado run}}",
        "set report_dir [file join $project_dir vmcp_reports $run_name invocations $collection_id]",
        "if {![file isdirectory $report_dir]} {error {Report collection directory was not prepared by the MCP filesystem broker}}",
        f"set vmcp_expected_report_names [list {expected_report_names}]",
        "set vmcp_prepared_report_names [list]",
        "foreach vmcp_prepared_report [glob -nocomplain -types f -directory $report_dir *] {"
        "if {[file size $vmcp_prepared_report] != 0} {error {Prepared report output is not empty}}; "
        "lappend vmcp_prepared_report_names [file tail $vmcp_prepared_report]"
        "}",
        "if {[lsort $vmcp_prepared_report_names] ne [lsort $vmcp_expected_report_names]} {error {Report output placeholders do not match the broker contract}}",
        "set run_status {}",
        "set run_progress {}",
        "set run_needs_refresh {}",
        "set run_directory {}",
        "set open_run_status missing",
        "set open_run_message {Vivado run was not found}",
        "set runs [get_runs -quiet $run_name]",
        "if {[llength $runs] > 0} {"
        "set r [lindex $runs 0]; "
        "catch {set run_status [get_property STATUS $r]}; "
        "catch {set run_progress [get_property PROGRESS $r]}; "
        "catch {set run_needs_refresh [get_property NEEDS_REFRESH $r]}; "
        "catch {set run_directory [file normalize [get_property DIRECTORY $r]]}; "
        "if {[catch {open_run $run_name} open_run_error]} {"
        "set open_run_status failed; set open_run_message $open_run_error"
        "} else {"
        "set open_run_status generated; set open_run_message {open_run completed}"
        "}"
        "}",
    ]
    for category, filename, command in (
        ("timing", "timing_summary.rpt", "report_timing_summary"),
        ("utilization", "utilization.rpt", "report_utilization"),
        ("drc", "drc.rpt", "report_drc"),
        ("methodology", "methodology.rpt", "report_methodology"),
    ):
        parts.append(_report_generation_command(category, filename, command))
    for category, filename, command in (
        ("qor", "qor_summary.rpt", "report_qor_summary"),
        ("cdc", "cdc.rpt", "report_cdc"),
        ("clock_interaction", "clock_interaction.rpt", "report_clock_interaction"),
        ("power", "power.rpt", "report_power"),
    ):
        parts.append(_optional_report_generation_command(category, filename, command))
    parts.extend(
        [
            "set messages_report_command_status skipped",
            "set messages_report_command_message {open_run did not complete}",
            "set messages_complete_scan 0",
            "set messages_source_stable 0",
            "set messages_extracted_count 0",
            "set run_log {}",
            "set run_log_size_before -1",
            "set run_log_size_after -1",
            "set run_log_mtime_before -1",
            "set run_log_mtime_after -1",
            "if {$open_run_status eq {generated}} {"
            "set run_log [file join $run_directory runme.log]; "
            "if {![file exists $run_log]} {"
            "set messages_report_command_status failed; "
            "set messages_report_command_message {current runme.log was not found}"
            "} elseif {[catch {"
            "set run_log_size_before [file size $run_log]; "
            "set run_log_mtime_before [file mtime $run_log]; "
            "set input_fh [open $run_log r]; "
            "set output_fh [open [file join $report_dir messages.log] wb]; "
            "while {[gets $input_fh vmcp_log_line] >= 0} {"
            "if {[regexp -nocase {(^|[^[:alpha:]])(CRITICAL WARNING|ERROR|WARNING|FATAL):} $vmcp_log_line]} {"
            "puts $output_fh $vmcp_log_line; incr messages_extracted_count"
            "}"
            "}; "
            "close $input_fh; close $output_fh; "
            "set run_log_size_after [file size $run_log]; "
            "set run_log_mtime_after [file mtime $run_log]; "
            "set messages_source_stable [expr {$run_log_size_before == $run_log_size_after && $run_log_mtime_before == $run_log_mtime_after}]; "
            "if {!$messages_source_stable} {error {runme.log changed while messages were collected}}; "
            "set messages_complete_scan 1"
            "} messages_error]} {"
            "catch {close $input_fh}; catch {close $output_fh}; "
            "set messages_report_command_status failed; set messages_report_command_message $messages_error"
            "} else {"
            "set messages_report_command_status generated; "
            "set messages_report_command_message {scanned complete current runme.log for diagnostic messages}"
            "}"
            "}",
            "set context_rows [list "
            "\"run_name=$run_name\" "
            "\"project_dir=$project_dir\" "
            "\"report_dir=$report_dir\" "
            "\"collection_id=$collection_id\" "
            "\"collection_started_ms=$collection_started_ms\" "
            "\"vivado_version_short=$vivado_version_short\" "
            "\"vivado_build=$vivado_build\" "
            "\"report_command_schema=vivado_2021_2_v1\" "
            "\"open_run_status=$open_run_status\" "
            "\"open_run_message=[string map {\\n { } \\r { }} $open_run_message]\" "
            "\"run_status=$run_status\" "
            "\"run_progress=$run_progress\" "
            "\"run_needs_refresh=$run_needs_refresh\" "
            "\"run_directory=$run_directory\" "
            "\"run_log_path=$run_log\" "
            "\"run_log_size_before=$run_log_size_before\" "
            "\"run_log_size_after=$run_log_size_after\" "
            "\"run_log_mtime_before=$run_log_mtime_before\" "
            "\"run_log_mtime_after=$run_log_mtime_after\" "
            "\"messages_complete_scan=$messages_complete_scan\" "
            "\"messages_source_stable=$messages_source_stable\" "
            "\"messages_extracted_count=$messages_extracted_count\""
            "]",
        ]
    )
    for category in REPORT_CATEGORIES.values():
        parts.append(
            f"lappend context_rows \"{category}_report_command_status=$"
            f"{category}_report_command_status\""
        )
        parts.append(
            f"lappend context_rows \"{category}_report_command_message="
            f"[string map {{\\n {{ }} \\r {{ }}}} $"
            f"{category}_report_command_message]\""
        )
    parts.append("join $context_rows \"\\n\"")
    return "; ".join(parts)


def _report_generation_command(category: str, filename: str, command: str) -> str:
    return (
        f"set {category}_report_command_status skipped; "
        f"set {category}_report_command_message {{open_run did not complete}}; "
        f"if {{$open_run_status eq {{generated}}}} {{"
        f"if {{[catch {{{command} -file [file join $report_dir {filename}] -quiet}} report_error]}} {{"
        f"set {category}_report_command_status failed; set {category}_report_command_message $report_error"
        "} else {"
        f"set {category}_report_command_status generated; "
        f"set {category}_report_command_message {{{command} completed}}"
        "}"
        "}"
    )


def _optional_report_generation_command(category: str, filename: str, command: str) -> str:
    return (
        f"set {category}_report_command_status skipped; "
        f"set {category}_report_command_message {{open_run did not complete}}; "
        f"if {{$open_run_status eq {{generated}}}} {{"
        f"if {{[llength [info commands {command}]] == 0}} {{"
        f"set {category}_report_command_status unavailable; "
        f"set {category}_report_command_message {{{command} command is unavailable in this Vivado version}}"
        f"}} elseif {{[catch {{{command} -file [file join $report_dir {filename}] -quiet}} report_error]}} {{"
        f"set {category}_report_command_status failed; set {category}_report_command_message $report_error"
        "} else {"
        f"set {category}_report_command_status generated; "
        f"set {category}_report_command_message {{{command} completed}}"
        "}"
        "}"
    )


def parse_syntax_report(raw: str) -> dict[str, Any]:
    metadata, report, envelope_complete = _split_marker_envelope(
        raw,
        f"raw_begin={SYNTAX_MARKER}",
        f"raw_end={SYNTAX_END_MARKER}",
    )
    values = _parse_key_value_lines(metadata)
    messages = parse_messages(report)
    counts = messages["counts"]
    parsed = envelope_complete and values.get("status", "").upper() in {"READY", "WARN", "BLOCK"} and bool(values.get("fileset"))
    status = _status_from_messages(counts, default=values.get("status", "BLOCK")) if parsed else "BLOCK"
    return {
        "ok": True,
        "status": status,
        "available": bool(raw),
        "parsed": parsed,
        "complete": parsed,
        "attestation_valid": parsed,
        "error_code": "" if parsed else "SOURCE_EVIDENCE_INCOMPLETE",
        "fileset": values.get("fileset", ""),
        "counts": counts,
        "messages": messages["messages"],
        "raw": raw,
        "raw_excerpt": _excerpt(report or raw),
    }


def parse_compile_order(raw: str) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    duplicates: list[str] = []
    metadata, rows, envelope_complete = _split_marker_envelope(
        raw,
        f"raw_begin={COMPILE_ORDER_MARKER}",
        f"raw_end={COMPILE_ORDER_END_MARKER}",
    )
    values = _parse_key_value_lines(metadata)
    for line in rows.splitlines():
        if not line.startswith(HEX_ROW_PREFIX):
            continue
        fields = decode_wire_row(line)
        if "file" in fields:
            file_type = fields.get("type", "")
            files.append(
                {
                    "path": fields.get("file", ""),
                    "file_type": file_type,
                    "exists": fields.get("exists", "0") == "1",
                    "managed": fields.get("managed", "0") == "1",
                    "used_in": fields.get("used_in", ""),
                    "order": int(fields.get("order", "0") or 0),
                }
            )
        elif "duplicate" in fields:
            duplicates.append(fields["duplicate"])
    missing = [item["path"] for item in files if not item["exists"]]
    unknown = [item["path"] for item in files if item["file_type"].strip().lower() in {"", "unknown"}]
    declared_count = _int_or_none(values.get("compile_order_count", ""))
    parsed = (
        envelope_complete
        and values.get("compile_order_schema") == "vivado_2021_2_v1"
        and values.get("compile_order_complete") == "1"
        and values.get("status", "").upper() in {"READY", "WARN", "BLOCK"}
        and bool(values.get("fileset"))
        and bool(values.get("top"))
        and declared_count == len(files)
        and bool(files)
    )
    status = "BLOCK" if not parsed or missing else "WARN" if duplicates or unknown else "READY"
    return {
        "ok": True,
        "status": status,
        "available": bool(raw),
        "parsed": parsed,
        "complete": parsed,
        "attestation_valid": parsed,
        "error_code": "" if parsed else "SOURCE_EVIDENCE_INCOMPLETE",
        "fileset": values.get("fileset", ""),
        "top": values.get("top", ""),
        "files": files,
        "missing_files": missing,
        "duplicates": duplicates,
        "unknown_file_types": unknown,
        "raw": raw,
        "raw_excerpt": _excerpt(raw),
    }


def parse_elaboration_result(raw: str) -> dict[str, Any]:
    metadata, report, envelope_complete = _split_marker_envelope(
        raw,
        f"raw_begin={ELABORATION_MARKER}",
        f"raw_end={ELABORATION_END_MARKER}",
    )
    values = _parse_key_value_lines(metadata)
    messages = parse_messages(report)
    unresolved = sorted(set(re.findall(r"module\s+'([^']+)'\s+not found|module\s+\"([^\"]+)\"\s+not found", report, flags=re.IGNORECASE)))
    unresolved_names = sorted({name for match in unresolved for name in match if name})
    black_box_count = len(re.findall(r"black\s+box", report, flags=re.IGNORECASE))
    width_mismatch_count = len(re.findall(r"width\s+mismatch", report, flags=re.IGNORECASE))
    counts = messages["counts"]
    parsed = envelope_complete and values.get("status", "").upper() in {"READY", "WARN", "BLOCK"}
    status = _status_from_messages(counts, default=values.get("status", "BLOCK")) if parsed else "BLOCK"
    if unresolved_names or black_box_count or status == "BLOCK":
        status = "BLOCK"
    elif width_mismatch_count or counts["WARNING"]:
        status = "WARN"
    return {
        "ok": True,
        "status": status,
        "available": bool(raw),
        "parsed": parsed,
        "complete": parsed,
        "attestation_valid": parsed,
        "error_code": "" if parsed else "SOURCE_EVIDENCE_INCOMPLETE",
        "top": values.get("top", ""),
        "part": values.get("part", ""),
        "counts": counts,
        "messages": messages["messages"],
        "unresolved_modules": unresolved_names,
        "black_box_count": black_box_count,
        "width_mismatch_count": width_mismatch_count,
        "raw": raw,
        "raw_excerpt": _excerpt(report or raw),
    }


def parse_design_hierarchy(raw: str) -> dict[str, Any]:
    ports: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    for line in raw.splitlines():
        fields = decode_wire_row(line)
        if "port" in fields:
            ports.append(
                {
                    "name": fields.get("port", ""),
                    "direction": fields.get("direction", ""),
                    "width": int(fields.get("width", "1") or 1),
                }
            )
        elif "cell" in fields:
            cells.append(
                {
                    "name": fields.get("cell", ""),
                    "ref": fields.get("ref", ""),
                    "primitive": _truthy(fields.get("primitive", "")),
                }
            )
    primitive_count = sum(1 for cell in cells if cell["primitive"])
    return {
        "ok": True,
        "ports": ports,
        "cells": cells,
        "cell_counts": {"total": len(cells), "primitive": primitive_count, "hierarchical": len(cells) - primitive_count},
        "raw": raw,
        "raw_excerpt": _excerpt(raw),
    }


def parse_cdc_report(raw: str) -> dict[str, Any]:
    if _is_unavailable(raw, "cdc_unavailable"):
        return _unavailable_report(raw, "cdc")
    counts = _crossing_counts(raw)
    messages = parse_messages(raw)
    structure_recognized = _crossing_structure_recognized(raw) or _empty_cdc_report_recognized(raw)
    status = "BLOCK" if not structure_recognized or counts["unsafe"] or messages["counts"]["ERROR"] or messages["counts"]["CRITICAL WARNING"] else "WARN" if counts["unknown"] or messages["counts"]["WARNING"] else "READY"
    return {"ok": True, "status": status, "available": True, "parsed": structure_recognized, "structure_recognized": structure_recognized, "complete": structure_recognized, "counts": counts, "messages": messages, "raw": raw, "raw_excerpt": _excerpt(raw)}


def parse_clock_interaction_report(raw: str) -> dict[str, Any]:
    if _is_unavailable(raw, "clock_interaction_unavailable"):
        return _unavailable_report(raw, "clock_interaction")
    table_recognized = _clock_interaction_table_recognized(raw)
    counts = _crossing_counts(raw) if _crossing_structure_recognized(raw) else _clock_interaction_table_counts(raw)
    messages = parse_messages(raw)
    structure_recognized = _crossing_structure_recognized(raw) or table_recognized
    status = "BLOCK" if not structure_recognized or counts["unsafe"] or messages["counts"]["ERROR"] or messages["counts"]["CRITICAL WARNING"] else "WARN" if counts["unknown"] or messages["counts"]["WARNING"] else "READY"
    return {"ok": True, "status": status, "available": True, "parsed": structure_recognized, "structure_recognized": structure_recognized, "complete": structure_recognized, "counts": counts, "messages": messages, "raw": raw, "raw_excerpt": _excerpt(raw)}


def parse_power_report(raw: str) -> dict[str, Any]:
    if _is_unavailable(raw, "power_unavailable"):
        return _unavailable_report(raw, "power")
    messages = parse_messages(raw)
    total_on_chip_w = _first_float_after_any(["Total On-Chip Power (W)", "Total On-Chip Power"], raw)
    dynamic_w = _first_float_after_any(["Dynamic (W)", "Dynamic"], raw)
    static_w = _first_float_after_any(["Static Power (W)", "Device Static (W)", "Static"], raw)
    structure_recognized = total_on_chip_w is not None
    status = _status_from_messages(messages["counts"], default="READY" if structure_recognized else "BLOCK")
    return {
        "ok": True,
        "status": status,
        "available": True,
        "parsed": structure_recognized,
        "structure_recognized": structure_recognized,
        "complete": structure_recognized,
        "total_on_chip_w": total_on_chip_w,
        "dynamic_w": dynamic_w,
        "static_w": static_w,
        "messages": messages,
        "raw": raw,
        "raw_excerpt": _excerpt(raw),
    }


def parse_configuration_voltage(raw: str) -> dict[str, Any]:
    values = _parse_key_value_lines(raw)
    cfgbvs = values.get("cfgbvs", "").strip()
    config_voltage = values.get("config_voltage", "").strip()
    warnings: list[str] = []
    if not cfgbvs or not config_voltage:
        warnings.append(
            "CFGBVS/CONFIG_VOLTAGE are not fully set on current_design; document board voltage basis or set both properties before real board handoff."
        )
    return {
        "ok": True,
        "status": "WARN" if warnings else "READY",
        "cfgbvs": cfgbvs,
        "config_voltage": config_voltage,
        "warnings": warnings,
        "raw": raw,
        "raw_excerpt": _excerpt(raw),
    }


def analyze_sources_result(*, syntax: dict[str, Any], compile_order: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []
    error_codes: list[str] = []
    if any(syntax.get(field) is False for field in ("parsed", "complete", "attestation_valid")):
        reasons.append("syntax evidence is incomplete or unattested")
        error_codes.append("SOURCE_EVIDENCE_INCOMPLETE")
    if any(compile_order.get(field) is False for field in ("parsed", "complete", "attestation_valid")):
        reasons.append("compile-order evidence is incomplete or unattested")
        error_codes.append("SOURCE_EVIDENCE_INCOMPLETE")
    syntax_counts = syntax.get("counts", {})
    if syntax.get("status") == "BLOCK" or syntax_counts.get("ERROR", 0):
        reasons.append(f"syntax has {syntax_counts.get('ERROR', 0)} error(s)")
    if syntax_counts.get("CRITICAL WARNING", 0):
        reasons.append(f"syntax has {syntax_counts['CRITICAL WARNING']} critical warning(s)")
    if syntax_counts.get("WARNING", 0):
        warnings.append(f"syntax has {syntax_counts['WARNING']} warning(s)")
    for path in compile_order.get("missing_files", []):
        reasons.append(f"missing source file: {path}")
    for path in compile_order.get("duplicates", []):
        warnings.append(f"duplicate source file in compile order: {path}")
    for path in compile_order.get("unknown_file_types", []):
        warnings.append(f"unknown source file type: {path}")
    status = "BLOCK" if reasons else "WARN" if warnings else "READY"
    return {
        "ok": True,
        "status": status,
        "error_codes": _dedupe(error_codes),
        "reasons": _dedupe(reasons),
        "warnings": _dedupe(warnings),
        "syntax": syntax,
        "compile_order": compile_order,
    }


def evaluate_pre_hw_signoff(
    *,
    sources: dict[str, Any],
    elaboration: dict[str, Any],
    simulation: dict[str, Any],
    readiness: dict[str, Any],
    cdc: dict[str, Any],
    clock_interaction: dict[str, Any],
    power: dict[str, Any],
    configuration_voltage: dict[str, Any] | None = None,
    report_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons = list(sources.get("reasons", []))
    warnings = list(sources.get("warnings", []))
    _merge_status("sources", sources, reasons, warnings)
    _merge_elaboration(elaboration, reasons, warnings)
    _merge_simulation(simulation, reasons, warnings)
    if readiness.get("status") == "BLOCK":
        reasons.extend(readiness.get("reasons", ["bitstream readiness is blocked"]))
    elif readiness.get("status") == "WARN":
        warnings.extend(readiness.get("warnings", ["bitstream readiness has warnings"]))
    if cdc.get("counts", {}).get("unsafe", 0):
        reasons.append(f"CDC reports unsafe crossings={cdc['counts']['unsafe']}")
    _merge_status("CDC", cdc, reasons, warnings)
    if clock_interaction.get("counts", {}).get("unsafe", 0):
        reasons.append(f"clock interaction reports unsafe crossings={clock_interaction['counts']['unsafe']}")
    if clock_interaction.get("counts", {}).get("unknown", 0):
        warnings.append(f"clock interaction has unknown crossings={clock_interaction['counts']['unknown']}")
    _merge_status("clock interaction", clock_interaction, reasons, warnings)
    _merge_status("power", power, reasons, warnings)
    if configuration_voltage:
        warnings.extend(configuration_voltage.get("warnings", []))
    status = "BLOCK" if reasons else "WARN" if warnings else "READY"
    report_manifest_path = ""
    if report_manifest:
        report_manifest_path = str(report_manifest.get("manifest_path", ""))
    return {
        "ok": True,
        "status": status,
        "effective_status": status,
        "validation_scope": "pre_hardware_software",
        "ready_meaning": "READY means the no-board Vivado software evidence is clean enough for handoff; it is not real FPGA board validation.",
        "reasons": _dedupe(reasons),
        "warnings": _dedupe(warnings),
        "warning_review_guidance": _signoff_warning_review_guidance(reasons, warnings),
        "next_steps": signoff_next_steps(status, reasons, warnings),
        "next_actions": signoff_next_actions(status, reasons, warnings),
        "report_manifest_path": report_manifest_path,
        "configuration_voltage": configuration_voltage or {},
        "hardware_validation": hardware_validation_boundary(),
        "inputs": {
            "sources": sources,
            "elaboration": elaboration,
            "simulation": simulation,
            "readiness": readiness,
            "cdc": cdc,
            "clock_interaction": clock_interaction,
            "power": power,
            "configuration_voltage": configuration_voltage or {},
        },
    }


def _signoff_warning_review_guidance(reasons: list[str], warnings: list[str]) -> dict[str, Any]:
    findings = [*reasons, *warnings]
    io_delay_counts = _io_delay_counts(findings)
    guidance: dict[str, Any] = {}
    if io_delay_counts:
        summary_parts = [f"{name}={count}" for name, count in io_delay_counts.items()]
        guidance["io_delay"] = {
            "reviewable_without_board": True,
            "summary": ", ".join(summary_parts),
            "agent_instruction": (
                "In the no-board phase, missing input/output delay warnings may be accepted only as a reviewable WARN handoff; "
                "do not report the bundle as READY or hardware-validated."
            ),
            "required_review_note": (
                "Record that board-level IO timing requirements are not yet available, or add input/output delay constraints before handoff."
            ),
            "rerun_condition": "When board-level IO timing requirements are provided, add the XDC constraints and rerun timing, signoff, audit, and diagnostic bundle validation.",
        }
    return guidance


def _io_delay_counts(findings: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in findings:
        text = str(item)
        for key in ("no_input_delay", "no_output_delay", "partial_input_delay", "partial_output_delay"):
            match = re.search(rf"{key}=([0-9]+)", text)
            if match:
                counts[key] = max(counts.get(key, 0), int(match.group(1)))
    return counts


def collect_report_bundle_files(
    *,
    report_dir: str | Path,
    run_name: str,
    project_dir: str | Path | None = None,
    report_context: dict[str, Any] | None = None,
    design_execution_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    directory = Path(report_dir).resolve()
    context = report_context or {}
    if project_dir is not None:
        _assert_inside_project(Path(project_dir).resolve(), directory, operation="write")
    report_status = _report_status(directory, context)
    status_by_category = {str(item["category"]): item for item in report_status}
    reports: list[dict[str, Any]] = []
    for path in sorted(item for item in directory.iterdir() if item.is_file()):
        category = REPORT_CATEGORIES.get(path.name)
        if category is None or not status_by_category.get(category, {}).get("current"):
            continue
        reports.append(
            {
                "path": str(path),
                "category": category,
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    missing_reports = [item for item in report_status if not item["current"]]
    required_missing = [
        item for item in missing_reports if str(item.get("category", "")) in REQUIRED_REPORT_CATEGORIES
    ]
    freshness = _report_collection_freshness(
        run_name=run_name,
        context=context,
        report_status=report_status,
    )
    if str(context.get("vivado_version_short", "")) != "2021.2":
        freshness["status"] = "STALE"
        freshness["reasons"].append("report evidence is not attested to Vivado 2021.2")
    if not str(context.get("vivado_build", "")).startswith("Vivado v2021.2"):
        freshness["status"] = "STALE"
        freshness["reasons"].append("report evidence Vivado build attestation is missing or does not match Vivado 2021.2")
    if str(context.get("report_command_schema", "")) != "vivado_2021_2_v1":
        freshness["status"] = "STALE"
        freshness["reasons"].append("report command schema attestation is missing or unsupported")
    source_log_evidence = _source_log_evidence(context, project_dir=project_dir)
    if source_log_evidence["status"] != "FRESH":
        freshness["status"] = "STALE"
        freshness["reasons"].extend(source_log_evidence["reasons"])
    design_identity = (
        design_execution_identity
        if isinstance(design_execution_identity, dict)
        else context.get("design_execution_identity")
        if isinstance(context.get("design_execution_identity"), dict)
        else {}
    )
    design_identity_sha256 = str(design_identity.get("sha256", ""))
    if design_identity.get("status") != "READY" or not re.fullmatch(r"[0-9a-f]{64}", design_identity_sha256):
        freshness["status"] = "STALE"
        freshness["reasons"].append("design execution identity is missing, incomplete, or invalid")
    manifest_status = (
        "BLOCK"
        if freshness["status"] != "FRESH" or required_missing
        else "WARN"
        if missing_reports
        else "READY"
    )
    command_outcomes = {
        category: {
            "status": str(context.get(f"{category}_report_command_status", "")),
            "message": str(context.get(f"{category}_report_command_message", "")),
        }
        for category in REPORT_CATEGORIES.values()
    }
    manifest = {
        "schema_version": 4,
        "status": manifest_status,
        "run_name": run_name,
        "vivado_version_short": str(context.get("vivado_version_short", "")),
        "vivado_build": str(context.get("vivado_build", "")),
        "report_command_schema": str(context.get("report_command_schema", "")),
        "collection_id": str(context.get("collection_id", "")),
        "report_dir": str(directory),
        "manifest_path": str(directory / "report_manifest.json"),
        "expected_report_count": len(REPORT_CATEGORIES),
        "generated_report_count": len(reports),
        "report_status": report_status,
        "missing_reports": missing_reports,
        "required_missing_reports": required_missing,
        "command_outcomes": command_outcomes,
        "run_snapshot": {
            "run_name": run_name,
            "status": str(context.get("run_status", "")),
            "progress": str(context.get("run_progress", "")),
            "needs_refresh": str(context.get("run_needs_refresh", "")),
            "directory": str(context.get("run_directory", "")),
            "session_generation_id": str(context.get("session_generation_id", "")),
        },
        "evidence_freshness": freshness,
        "source_log_evidence": source_log_evidence,
        "design_execution_identity": design_identity,
        "design_execution_identity_sha256": design_identity_sha256,
        "reports": reports,
    }
    manifest_path = Path(manifest["manifest_path"])
    atomic_write_bytes(
        directory,
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    manifest["manifest_sha256"] = _sha256_file(manifest_path)
    return manifest


def _assert_inside_project(project_dir: Path, target: Path, *, operation: str) -> None:
    try:
        target.resolve().relative_to(project_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"Refusing to {operation} outside project directory") from exc


def parse_report_bundle_context(raw: str) -> dict[str, str]:
    return _parse_key_value_lines(raw)


def _optional_report_command(*, command: str, unavailable_key: str, message: str) -> str:
    return (
        "set report_raw \"\"; "
        f"if {{[llength [info commands {command}]] > 0}} {{"
        f"if {{[catch {{{command} -return_string}} report_raw]}} {{set report_raw \"ERROR: $report_raw\"}}; "
        "set report_raw"
        "} else {"
        f"join [list \"{unavailable_key}=1\" \"message={message}\"] \"\\n\""
        "}"
    )


def _crossing_counts(raw: str) -> dict[str, int]:
    return {
        "unsafe": _first_int_after("Unsafe", raw) or 0,
        "unknown": _first_int_after("Unknown", raw) or 0,
        "safe": _first_int_after("Safe", raw) or 0,
    }


def _crossing_structure_recognized(raw: str) -> bool:
    return all(_first_int_after(label, raw) is not None for label in ("Unsafe", "Unknown", "Safe"))


def _empty_cdc_report_recognized(raw: str) -> bool:
    return bool(
        re.search(r"(?im)^\s*CDC\s+Report\s*$", raw)
        and re.search(r"(?im)^\s*\|?\s*Command\s*:\s*report_cdc(?:\s|$)", raw)
    )


def _clock_interaction_table_recognized(raw: str) -> bool:
    return bool(
        re.search(r"(?im)^\s*Clock\s+Interaction\s+Report\s*$", raw)
        and re.search(r"(?im)^\s*Clock\s+Interaction\s+Table\s*$", raw)
        and re.search(r"(?im)From\s+Clock\s+To\s+Clock.*Classification", raw, flags=re.DOTALL)
    )


def _clock_interaction_table_counts(raw: str) -> dict[str, int]:
    counts = {"unsafe": 0, "unknown": 0, "safe": 0}
    pattern = re.compile(
        r"\s{2,}(Clean|Unsafe|Unknown|No\s+Common\s+Clock|User\s+Ignored|Ignored|Partial)\s{2,}",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(raw):
        classification = re.sub(r"\s+", " ", match.group(1).strip()).lower()
        if classification == "clean":
            counts["safe"] += 1
        elif classification == "unsafe":
            counts["unsafe"] += 1
        else:
            counts["unknown"] += 1
    return counts


def _unavailable_report(raw: str, name: str) -> dict[str, Any]:
    values = _parse_key_value_lines(raw)
    return {
        "ok": True,
        "status": "WARN",
        "available": False,
        "parsed": False,
        "structure_recognized": True,
        "complete": False,
        "report": name,
        "message": values.get("message", ""),
        "counts": {"unsafe": 0, "unknown": 0, "safe": 0},
        "messages": parse_messages(raw),
        "raw": raw,
        "raw_excerpt": _excerpt(raw),
    }


def _is_unavailable(raw: str, key: str) -> bool:
    return _parse_key_value_lines(raw).get(key, "0") == "1"


def _status_from_messages(counts: dict[str, int], default: str = "READY") -> str:
    if default == "BLOCK" or counts.get("ERROR", 0) or counts.get("CRITICAL WARNING", 0):
        return "BLOCK"
    if default == "WARN" or counts.get("WARNING", 0):
        return "WARN"
    return "READY"


def _merge_status(label: str, item: dict[str, Any], reasons: list[str], warnings: list[str]) -> None:
    if item.get("status") == "BLOCK":
        reasons.append(f"{label} status is BLOCK")
    elif item.get("status") == "WARN":
        message = item.get("message")
        warnings.append(f"{label} status is WARN" + (f": {message}" if message else ""))
    elif item.get("status") == "READY" and any(
        field in item and item.get(field) is not True
        for field in ("available", "parsed", "complete", "structure_recognized", "attestation_valid")
    ):
        reasons.append(f"{label} evidence is incomplete or unattested")


def _merge_elaboration(elaboration: dict[str, Any], reasons: list[str], warnings: list[str]) -> None:
    if elaboration.get("status") == "BLOCK":
        reasons.append("elaboration status is BLOCK")
    for module in elaboration.get("unresolved_modules", []):
        reasons.append(f"unresolved module: {module}")
    if elaboration.get("black_box_count", 0):
        reasons.append(f"elaboration has black boxes={elaboration['black_box_count']}")
    if elaboration.get("width_mismatch_count", 0):
        warnings.append(f"elaboration has width mismatches={elaboration['width_mismatch_count']}")


def _merge_simulation(simulation: dict[str, Any], reasons: list[str], warnings: list[str]) -> None:
    status = str(simulation.get("status", "")).strip()
    normalized = status.upper()
    if simulation.get("source_tool") == "get_simulation_result" and simulation.get("error_code"):
        message = str(simulation.get("message", "")).strip()
        detail = f": {message}" if message else ""
        reasons.append(f"behavioral simulation result is unavailable{detail}")
        return
    if simulation.get("source_tool") in {"get_simulation_result", "run_behavioral_simulation"} and not _has_fresh_simulation_invocation(simulation):
        reasons.append("behavioral simulation result is not from a current invocation log span")
        return
    if normalized in {"PASSED", "PASS", "COMPLETED", "COMPLETE"}:
        return
    if normalized in {"FAILED", "FAIL", "BLOCK", "ERROR"}:
        reasons.append("behavioral simulation failed")
        return
    message = str(simulation.get("message", "")).strip()
    detail = f": {message}" if message else ""
    reasons.append(f"behavioral simulation result is not conclusive: {status or 'missing'}{detail}")


def _has_fresh_simulation_invocation(simulation: dict[str, Any]) -> bool:
    freshness = simulation.get("evidence_freshness") if isinstance(simulation.get("evidence_freshness"), dict) else {}
    if str(freshness.get("status", "")).strip().upper() != "FRESH":
        return False
    if not all(bool(freshness.get(key)) for key in ("same_project", "same_simset", "same_sources")):
        return False
    if str(simulation.get("status_source", "")).strip() != "simulation_invocation_log_span":
        return False
    if not str(simulation.get("simulation_invocation_id", "")).strip():
        return False
    if not str(simulation.get("ended_at", "")).strip():
        return False
    log_span = simulation.get("log_span") if isinstance(simulation.get("log_span"), dict) else {}
    try:
        start = int(log_span.get("start", 0))
        end = int(log_span.get("end", 0))
    except (TypeError, ValueError):
        return False
    return end > start


def signoff_next_steps(status: str, reasons: list[str], warnings: list[str]) -> list[str]:
    findings = [*reasons, *warnings]
    if status == "BLOCK":
        steps = ["Fix blocking signoff findings before treating the bitstream as hardware-ready."]
        steps.extend(_next_step_for_signoff_finding(item) for item in findings)
        steps.append("After fixes, rerun run_pre_hw_signoff and collect_report_bundle to refresh handoff evidence.")
        return _dedupe(steps)
    if status == "WARN":
        steps = ["Review warning findings and decide whether project-specific waivers are justified."]
        steps.extend(_next_step_for_signoff_finding(item) for item in findings)
        steps.append("Keep collect_report_bundle output with the diagnostic bundle for handoff.")
        return _dedupe(steps)
    return ["Proceed to explicit hardware programming only when a real FPGA board is connected."]


def signoff_next_actions(status: str, reasons: list[str], warnings: list[str]) -> list[dict[str, Any]]:
    if status == "READY":
        return [
            next_action(
                "collect_diagnostic_bundle",
                "Archive validated pre-hardware signoff evidence before handoff.",
                required_args=["run_name"],
                arg_sources={"run_name": "workflow.run_name or current implementation run"},
                preconditions=["run_pre_hw_signoff returned READY."],
                stop_condition="diagnostic bundle manifest is written.",
            )
        ]
    actions: list[dict[str, Any]] = []
    for finding in [*reasons, *warnings]:
        actions.extend(_next_actions_for_signoff_finding(str(finding)))
    actions.append(
        next_action(
            "collect_report_bundle",
            "Refresh report evidence after signoff findings are addressed.",
            required_args=["run_name"],
            arg_sources={"run_name": "workflow.run_name or current implementation run"},
            preconditions=["Implementation results are available."],
            stop_condition="report_manifest.json is written.",
            optional=status == "WARN",
        )
    )
    return dedupe_next_actions(actions)


def _next_step_for_signoff_finding(message: str) -> str:
    text = message.lower()
    if any(token in text for token in ("syntax", "missing source file", "duplicate source file", "unknown source file type", "sources status")):
        return "Run analyze_sources, check_syntax, and get_compile_order; fix RTL/fileset issues first."
    if any(token in text for token in ("elaboration", "unresolved module", "black box", "width mismatch")):
        return "Run run_elaboration and get_elaboration_result; resolve missing modules, black boxes, or width mismatches."
    if "behavioral simulation failed" in text or "simulation" in text:
        return "Run run_behavioral_simulation and get_simulation_result; fix the failing testbench or DUT behavior."
    if "cfgbvs" in text or "config_voltage" in text:
        return "Set or document board-specific CFGBVS and CONFIG_VOLTAGE before real FPGA board handoff."
    if any(token in text for token in ("timing", "no_clock", "unconstrained", "constraint", "readiness")):
        return "Run check_timing_constraints and analyze_timing_closure; fix XDC/timing readiness blockers."
    if "drc" in text:
        return "Run get_drc_report and resolve DRC errors before bitstream handoff."
    if "methodology" in text:
        return "Run get_methodology_report and resolve methodology errors before bitstream handoff."
    if "cdc" in text:
        return "Run get_cdc_report and resolve unsafe crossings or document reviewed waivers."
    if "clock interaction" in text:
        return "Run get_clock_interaction_report and review unsafe or unknown clock interactions."
    if "power" in text or "report_power" in text:
        return "Run get_power_report when implementation results are open; retain unavailable power report warnings if Vivado cannot produce it."
    return ""


def _next_actions_for_signoff_finding(message: str) -> list[dict[str, Any]]:
    text = message.lower()
    if any(token in text for token in ("syntax", "missing source file", "duplicate source file", "unknown source file type", "sources status")):
        return [
            next_action(
                "analyze_sources",
                "Diagnose source, syntax, and compile-order blockers.",
                required_args=["fileset"],
                arg_sources={"fileset": "workflow.fileset or sources_1"},
                preconditions=["Project is open."],
                stop_condition="analyze_sources status is READY.",
            )
        ]
    if any(token in text for token in ("elaboration", "unresolved module", "black box", "width mismatch")):
        return [
            next_action(
                "run_elaboration",
                "Re-run RTL elaboration to isolate missing modules, black boxes, or width mismatches.",
                required_args=["top", "part"],
                arg_sources={"top": "project top", "part": "project part"},
                preconditions=["Project is open and source blockers are addressed."],
                stop_condition="run_elaboration succeeds.",
            )
        ]
    if "behavioral simulation failed" in text or "simulation" in text:
        return [
            next_action(
                "run_behavioral_simulation",
                "Re-run behavioral simulation after DUT or testbench fixes.",
                required_args=["simset"],
                arg_sources={"simset": "workflow.simset or sim_1"},
                preconditions=["Simulation fileset is configured."],
                stop_condition="simulation result is PASSED.",
            )
        ]
    if "cfgbvs" in text or "config_voltage" in text:
        return [
            next_action(
                "create_managed_xdc",
                "Add reviewed board-specific CFGBVS and CONFIG_VOLTAGE properties to an MCP-managed XDC.",
                required_args=["name", "constraints"],
                arg_sources={
                    "name": "for example board_configuration_voltage",
                    "constraints": "board documentation: CFGBVS and CONFIG_VOLTAGE values",
                },
                preconditions=["Board voltage requirements are known from the target board or carrier design."],
                stop_condition="Managed XDC containing CFGBVS and CONFIG_VOLTAGE is added to constrs_1.",
                optional=True,
            )
        ]
    if any(token in text for token in ("timing", "no_clock", "unconstrained", "constraint", "readiness")):
        return [
            next_action(
                "check_timing_constraints",
                "Check unconstrained clocks and endpoints before timing closure.",
                preconditions=["Implementation result is open."],
                stop_condition="check_timing_constraints status is READY.",
            ),
            next_action(
                "analyze_timing_closure",
                "Aggregate timing, DRC, methodology, and critical-message blockers.",
                preconditions=["Implementation result is open."],
                stop_condition="analyze_timing_closure status is READY.",
            ),
        ]
    if "drc" in text:
        return [
            next_action(
                "get_drc_report",
                "Inspect DRC errors before bitstream handoff.",
                preconditions=["Implementation result is open."],
                stop_condition="DRC report has no ERROR or CRITICAL WARNING.",
            )
        ]
    if "methodology" in text:
        return [
            next_action(
                "get_methodology_report",
                "Inspect methodology errors before bitstream handoff.",
                preconditions=["Implementation result is open."],
                stop_condition="methodology report has no ERROR or CRITICAL WARNING.",
            )
        ]
    if "cdc" in text:
        return [
            next_action(
                "get_cdc_report",
                "Inspect CDC crossings and resolve unsafe paths.",
                preconditions=["Implementation result is open."],
                stop_condition="CDC report has no unsafe crossings.",
            )
        ]
    if "clock interaction" in text:
        return [
            next_action(
                "get_clock_interaction_report",
                "Inspect unsafe or unknown clock interactions.",
                preconditions=["Implementation result is open."],
                stop_condition="clock interaction report has no unsafe crossings.",
            )
        ]
    if "power" in text or "report_power" in text:
        return [
            next_action(
                "get_power_report",
                "Refresh power report when Vivado can produce it.",
                preconditions=["Implementation result is open."],
                stop_condition="power report is available or warning is documented.",
                optional=True,
            )
        ]
    return []


def _parse_key_value_lines(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line and "|" not in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _split_marker(raw: str, marker_line: str) -> tuple[str, str]:
    if marker_line not in raw:
        return raw, ""
    before, after = raw.split(marker_line, 1)
    return before, after.lstrip("\r\n")


def _split_marker_envelope(raw: str, begin_line: str, end_line: str) -> tuple[str, str, bool]:
    if begin_line not in raw or end_line not in raw:
        return raw, "", False
    before, after = raw.split(begin_line, 1)
    report, _ = after.split(end_line, 1)
    return before, report.strip("\r\n"), True


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_float_after(label: str, raw: str) -> float | None:
    match = re.search(rf"{re.escape(label)}[^\d+\-.]{{0,120}}([-+]?(?:\d+(?:\.\d*)?|\.\d+))", raw, flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


def _first_float_after_any(labels: list[str], raw: str) -> float | None:
    for label in labels:
        value = _first_float_after(label, raw)
        if value is not None:
            return value
    return None


def _first_int_after(label: str, raw: str) -> int | None:
    match = re.search(rf"{re.escape(label)}\s*:?\s*(\d+)", raw, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _truthy(value: str) -> bool:
    text = str(value).strip().lower()
    return text not in {"", "0", "false", "no", "{}"}


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_status(directory: Path, report_context: dict[str, str] | None = None) -> list[dict[str, Any]]:
    context = report_context or {}
    try:
        collection_started_ms = int(str(context.get("collection_started_ms", "")))
    except ValueError:
        collection_started_ms = 0
    rows: list[dict[str, Any]] = []
    for filename, category in REPORT_CATEGORIES.items():
        path = directory / filename
        present = path.is_file()
        command_status = str(context.get(f"{category}_report_command_status", "")).strip().lower()
        mtime_ms = int(path.stat().st_mtime * 1000) if present else 0
        generated_this_invocation = (
            present
            and command_status == "generated"
            and collection_started_ms > 0
            and mtime_ms >= collection_started_ms - 2000
        )
        missing_details = _missing_report_details(category, context) if not generated_this_invocation else {}
        if present and not generated_this_invocation and not missing_details.get("reason"):
            missing_details = {
                "reason": "Report file exists but was not proven to be generated by the current collection invocation.",
                "optional": category not in REQUIRED_REPORT_CATEGORIES,
                "optional_due_to_vivado_version": False,
                "severity": "warn",
                "user_message": "Regenerate the report bundle; existing files are not accepted as current evidence.",
            }
        rows.append(
            {
                "category": category,
                "filename": filename,
                "path": str(path),
                "present": present,
                "current": generated_this_invocation,
                "command_status": command_status,
                "mtime_ms": mtime_ms,
                "collection_started_ms": collection_started_ms,
                "status": "current" if generated_this_invocation else "stale" if present else command_status or "missing",
                "reason": "" if generated_this_invocation else str(missing_details.get("reason", "")),
                **missing_details,
            }
        )
    return rows


def _missing_report_reason(category: str, report_context: dict[str, str] | None = None) -> str:
    return str(_missing_report_details(category, report_context or {}).get("reason", ""))


def _missing_report_details(category: str, report_context: dict[str, str] | None = None) -> dict[str, Any]:
    context = report_context or {}
    command_status = str(context.get(f"{category}_report_command_status", "")).strip()
    command_message = str(context.get(f"{category}_report_command_message", "")).strip()
    if category == "qor":
        if command_status:
            detail = f"; message={command_message}" if command_message else ""
            version_unavailable = command_status.lower() == "unavailable" or "unavailable in this vivado version" in command_message.lower()
            return {
                "reason": f"qor_summary.rpt was not generated; report_qor_summary status={command_status}{detail}.",
                "optional": True,
                "optional_due_to_vivado_version": version_unavailable,
                "severity": "info" if version_unavailable else "warn",
                "user_message": (
                    "Vivado does not support report_qor_summary in this version; this is not a design failure."
                    if version_unavailable
                    else "QoR summary was not generated; review the command message before treating this as a design issue."
                ),
            }
        return {
            "reason": "qor_summary.rpt was not generated; report_qor_summary may be unavailable in this Vivado version or failed for the current design.",
            "optional": True,
            "optional_due_to_vivado_version": False,
            "severity": "warn",
            "user_message": "QoR summary is optional, but this manifest could not prove whether the report is version-unavailable or failed.",
        }
    if category in {"cdc", "clock_interaction", "power"}:
        return {
            "reason": (
                f"{category} report was not generated; command status={command_status or 'unknown'}"
                + (f"; message={command_message}" if command_message else "")
                + "."
            ),
            "optional": True,
            "optional_due_to_vivado_version": False,
            "severity": "warn",
            "user_message": f"{category} report is optional in some Vivado versions; keep the manifest reason in handoff evidence.",
        }
    return {
        "reason": (
            f"Expected report file was not generated by the current collection; command status={command_status or 'unknown'}"
            + (f"; message={command_message}" if command_message else "")
            + "."
        ),
        "optional": False,
        "optional_due_to_vivado_version": False,
        "severity": "warn",
        "user_message": "Expected handoff report is missing and should be regenerated or investigated.",
    }


def _report_collection_freshness(
    *,
    run_name: str,
    context: dict[str, str],
    report_status: list[dict[str, Any]],
) -> dict[str, Any]:
    reasons: list[str] = []
    collection_id = str(context.get("collection_id", "")).strip()
    if not collection_id:
        reasons.append("collection_id is missing")
    if str(context.get("open_run_status", "")).strip().lower() != "generated":
        reasons.append("open_run did not complete for the requested run")
    run_status = str(context.get("run_status", "")).strip()
    if "complete" not in run_status.lower():
        reasons.append(f"run status is not complete: {run_status or 'missing'}")
    progress = str(context.get("run_progress", "")).strip().rstrip("%")
    try:
        progress_complete = float(progress) >= 100.0
    except ValueError:
        progress_complete = False
    if not progress_complete:
        reasons.append(f"run progress is not 100%: {context.get('run_progress', '') or 'missing'}")
    needs_refresh_text = str(context.get("run_needs_refresh", "")).strip().lower()
    if needs_refresh_text not in {"0", "false", "no"}:
        reasons.append(f"run needs_refresh is not false: {needs_refresh_text or 'missing'}")
    if str(context.get("messages_complete_scan", "")).strip().lower() not in {"1", "true", "yes"}:
        reasons.append("messages.log was not produced from a complete runme.log scan")
    if str(context.get("messages_source_stable", "")).strip().lower() not in {"1", "true", "yes"}:
        reasons.append("runme.log changed or was not attested stable while messages were collected")
    for item in report_status:
        if str(item.get("category", "")) in REQUIRED_REPORT_CATEGORIES and not item.get("current"):
            reasons.append(f"required report is not current: {item.get('category')}")
    return {
        "status": "FRESH" if not reasons else "STALE",
        "run_name": run_name,
        "collection_id": collection_id,
        "collected_at": _utc_now(),
        "collection_started_ms": str(context.get("collection_started_ms", "")),
        "needs_refresh": needs_refresh_text not in {"0", "false", "no"},
        "source": "collect_report_bundle",
        "reasons": reasons,
    }


def _source_log_evidence(context: dict[str, str], *, project_dir: str | Path | None) -> dict[str, Any]:
    reasons: list[str] = []
    path_text = str(context.get("run_log_path", "")).strip()
    if not path_text:
        return {"status": "STALE", "path": "", "reasons": ["run_log_path is missing"]}
    path = Path(path_text).resolve()
    if project_dir is not None:
        try:
            _assert_inside_project(Path(project_dir).resolve(), path, operation="read")
        except ValueError as exc:
            return {"status": "STALE", "path": str(path), "reasons": [str(exc)]}
    if not path.is_file():
        return {"status": "STALE", "path": str(path), "reasons": ["runme.log is missing after report collection"]}
    stat = path.stat()
    expected_size = _int_context(context, "run_log_size_after")
    expected_mtime = _int_context(context, "run_log_mtime_after")
    if expected_size is None or expected_size != stat.st_size:
        reasons.append("runme.log size changed after report collection")
    if expected_mtime is None or expected_mtime != int(stat.st_mtime):
        reasons.append("runme.log mtime changed after report collection")
    return {
        "status": "FRESH" if not reasons else "STALE",
        "path": str(path),
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "sha256": _sha256_file(path),
        "complete_scan": str(context.get("messages_complete_scan", "")).strip().lower() in {"1", "true", "yes"},
        "source_stable": str(context.get("messages_source_stable", "")).strip().lower() in {"1", "true", "yes"},
        "messages_extracted_count": _int_context(context, "messages_extracted_count") or 0,
        "reasons": reasons,
    }


def _int_context(context: dict[str, str], key: str) -> int | None:
    try:
        return int(str(context.get(key, "")).strip())
    except (TypeError, ValueError):
        return None


def _excerpt(text: str, limit: int = 4096) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
