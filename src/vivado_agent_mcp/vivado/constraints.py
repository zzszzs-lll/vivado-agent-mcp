from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .parsers import (
    DRC_REPORT_BEGIN_MARKER,
    TIMING_SUMMARY_REPORT_BEGIN_MARKER,
    _extract_report_attestation,
    parse_messages,
    parse_timing_summary,
    parse_vivado_rule_violations,
    report_end_marker,
)
from .tcl import tcl_list_quote
from .wire import decode_wire_list, tcl_wire_prelude

XDC_MARKER = "__VMCP_XDC_BEGIN__"
CLOCK_REPORT_MARKER = "__VMCP_CLOCK_REPORT_BEGIN__"
CHECK_TIMING_REPORT_BEGIN_MARKER = "__VMCP_CHECK_TIMING_REPORT_BEGIN__"
METHODOLOGY_REPORT_BEGIN_MARKER = "__VMCP_METHODOLOGY_REPORT_BEGIN__"

CHECK_TIMING_KEYS = (
    "no_clock",
    "constant_clock",
    "pulse_width_clock",
    "unconstrained_internal_endpoints",
    "no_input_delay",
    "no_output_delay",
    "multiple_clock",
    "generated_clocks",
    "loops",
    "partial_input_delay",
    "partial_output_delay",
    "latch_loops",
)
CHECK_TIMING_SCHEMA_VERSION = "vivado_2021_2_v1"
BLOCKING_CHECKS = {
    "no_clock",
    "unconstrained_internal_endpoints",
    "multiple_clock",
    "loops",
    "latch_loops",
    "pulse_width_clock",
}
WARNING_CHECKS = {
    "no_input_delay",
    "no_output_delay",
    "partial_input_delay",
    "partial_output_delay",
    "generated_clocks",
    "constant_clock",
}
XDC_COMMANDS = (
    "create_clock",
    "create_generated_clock",
    "set_clock_groups",
    "set_input_delay",
    "set_output_delay",
    "set_false_path",
    "set_multicycle_path",
    "set_property",
)


def constraints_summary_command(*, fileset: str = "constrs_1") -> str:
    fileset_ref = tcl_list_quote(fileset)
    return "; ".join(
        [
            tcl_wire_prelude(),
            "set discovery_errors [list]",
            "set fileset_discovery_status ERROR",
            "set xdc_file_discovery_status ERROR",
            "set design_discovery_status ERROR",
            "set ports_discovery_status NOT_APPLICABLE",
            "set clocks_discovery_status NOT_APPLICABLE",
            "set generated_clocks_discovery_status NOT_APPLICABLE",
            "set clock_report_discovery_status NOT_APPLICABLE",
            "set xdc_file_discovery_reason {}",
            "set xdc_files [list]",
            f"if {{[catch {{set fs [get_filesets {fileset_ref}]}} discovery_error]}} {{"
            "lappend discovery_errors \"get_filesets failed: $discovery_error\""
            "} elseif {[llength $fs] == 0} {"
            "set fileset_discovery_status EMPTY; set xdc_file_discovery_status EMPTY"
            "} else {"
            "set fileset_discovery_status READY; "
            "if {[catch {set fileset_files [get_files -of_objects $fs]} discovery_error]} {"
            "set xdc_file_discovery_status ERROR; lappend discovery_errors \"get_files -of_objects failed: $discovery_error\""
            "} else {"
            "foreach f $fileset_files {if {[string equal -nocase [file extension $f] {.xdc}] && [lsearch -exact $xdc_files $f] < 0} {lappend xdc_files [file normalize $f]}}; "
            "set xdc_file_discovery_status [expr {[llength $xdc_files] > 0 ? {READY} : {EMPTY}}]"
            "}"
            "}",
            "if {[catch {set all_xdc_files [get_files -filter {FILE_TYPE == XDC}]} discovery_error]} {"
            "set xdc_file_discovery_status ERROR; lappend discovery_errors \"global XDC discovery failed: $discovery_error\""
            "} else {"
            "foreach f $all_xdc_files {if {[string equal -nocase [file extension $f] {.xdc}] && [lsearch -exact $xdc_files [file normalize $f]] < 0} {lappend xdc_files [file normalize $f]}}; "
            "if {$xdc_file_discovery_status ne {ERROR}} {set xdc_file_discovery_status [expr {[llength $xdc_files] > 0 ? {READY} : {EMPTY}}]}"
            "}",
            "if {$xdc_file_discovery_status eq {ERROR}} {set xdc_file_discovery_reason {A required Vivado XDC discovery query failed.}} "
            "elseif {$xdc_file_discovery_status eq {EMPTY}} {set xdc_file_discovery_reason {No XDC file paths were returned by successful Vivado discovery.}}",
            "set current_design_object {}; "
            "if {[catch {set current_design_object [current_design]} discovery_error]} {"
            "set design_discovery_status ERROR; lappend discovery_errors \"current_design failed: $discovery_error\""
            "} elseif {$current_design_object eq {}} {set design_discovery_status NOT_APPLICABLE} "
            "else {set design_discovery_status READY}",
            "set ports [list]; set clocks [list]; set generated_clocks [list]; set clock_report {}",
            "if {$design_discovery_status eq {READY}} {"
            "if {[catch {set ports [get_ports *]} discovery_error]} {set ports_discovery_status ERROR; lappend discovery_errors \"get_ports failed: $discovery_error\"} else {set ports_discovery_status [expr {[llength $ports] > 0 ? {READY} : {EMPTY}}]}; "
            "if {[catch {set clocks [get_clocks *]} discovery_error]} {set clocks_discovery_status ERROR; lappend discovery_errors \"get_clocks failed: $discovery_error\"} else {set clocks_discovery_status [expr {[llength $clocks] > 0 ? {READY} : {EMPTY}}]}; "
            "if {$clocks_discovery_status ne {ERROR}} {set generated_clocks_discovery_status READY; foreach c $clocks {if {[catch {set is_generated [get_property IS_GENERATED $c]} discovery_error]} {set generated_clocks_discovery_status ERROR; lappend discovery_errors \"generated clock property failed: $discovery_error\"; break}; if {$is_generated} {lappend generated_clocks $c}}}; "
            "if {[catch {set clock_report [report_clocks -return_string]} discovery_error]} {set clock_report_discovery_status ERROR; lappend discovery_errors \"report_clocks failed: $discovery_error\"} else {set clock_report_discovery_status READY}"
            "}",
            "set xdc_content {}; foreach f $xdc_files {"
            "if {![file isfile $f]} {set xdc_file_discovery_status ERROR; lappend discovery_errors \"XDC file is unavailable: $f\"; continue}; "
            "if {[catch {set fh [open $f r]; set file_content [read $fh]; close $fh} discovery_error]} {"
            "catch {close $fh}; set xdc_file_discovery_status ERROR; lappend discovery_errors \"XDC read failed for $f: $discovery_error\"; continue}; "
            "append xdc_content \"\\n# file=$f\\n\" $file_content"
            "}",
            "join [list "
            "\"xdc_files=[::vivado_agent_mcp_wire_list $xdc_files]\" "
            "\"xdc_file_discovery_status=$xdc_file_discovery_status\" "
            "\"xdc_file_discovery_reason=$xdc_file_discovery_reason\" "
            "\"fileset_discovery_status=$fileset_discovery_status\" "
            "\"design_discovery_status=$design_discovery_status\" "
            "\"ports_discovery_status=$ports_discovery_status\" "
            "\"clocks_discovery_status=$clocks_discovery_status\" "
            "\"generated_clocks_discovery_status=$generated_clocks_discovery_status\" "
            "\"clock_report_discovery_status=$clock_report_discovery_status\" "
            "\"discovery_errors=[::vivado_agent_mcp_wire_list $discovery_errors]\" "
            "\"ports=[::vivado_agent_mcp_wire_list $ports]\" "
            "\"clocks=[::vivado_agent_mcp_wire_list $clocks]\" "
            "\"generated_clocks=[::vivado_agent_mcp_wire_list $generated_clocks]\" "
            f"\"clock_report_begin={CLOCK_REPORT_MARKER}\" $clock_report "
            f"\"xdc_begin={XDC_MARKER}\" $xdc_content] \"\\n\"",
        ]
    )


def check_timing_constraints_command() -> str:
    return _attested_report_command(
        "check_timing -return_string",
        report_type="check_timing",
        begin_marker=CHECK_TIMING_REPORT_BEGIN_MARKER,
    )


def timing_summary_command() -> str:
    return _attested_report_command(
        "report_timing_summary -return_string",
        report_type="timing_summary",
        begin_marker=TIMING_SUMMARY_REPORT_BEGIN_MARKER,
    )


def drc_report_command() -> str:
    return _attested_report_command(
        "report_drc -return_string",
        report_type="drc",
        begin_marker=DRC_REPORT_BEGIN_MARKER,
    )


def clock_summary_command() -> str:
    return (
        "set clocks [report_clocks -return_string]; "
        "set networks \"\"; catch {set networks [report_clock_networks -return_string]}; "
        f"join [list \"clock_report_begin={CLOCK_REPORT_MARKER}\" $clocks \"clock_networks_begin=__VMCP_CLOCK_NETWORKS_BEGIN__\" $networks] \"\\n\""
    )


def timing_paths_command(*, max_paths: int = 10, delay_type: str = "max") -> str:
    return (
        "report_timing_summary -return_string "
        f"-max_paths {int(max_paths)} -delay_type {tcl_list_quote(delay_type)}"
    )


def methodology_report_command() -> str:
    return _attested_report_command(
        "report_methodology -return_string",
        report_type="methodology",
        begin_marker=METHODOLOGY_REPORT_BEGIN_MARKER,
    )


def _attested_report_command(command: str, *, report_type: str, begin_marker: str) -> str:
    end_marker = report_end_marker(begin_marker)
    return (
        f"set vmcp_report [{command}]; "
        "set vmcp_report_bytes [string bytelength $vmcp_report]; "
        "set vmcp_vivado_version_short [version -short]; "
        "set vmcp_vivado_build [string map {\\n { } \\r { }} [version]]; "
        "join [list "
        '"vmcp_report_ok=1" '
        f'"vmcp_report_type={report_type}" '
        '"vmcp_vivado_version_short=$vmcp_vivado_version_short" '
        '"vmcp_vivado_build=$vmcp_vivado_build" '
        f'"vmcp_report_command={command}" '
        '"vmcp_parser_schema_version=vivado_2021_2_v1" '
        '"vmcp_report_bytes=$vmcp_report_bytes" '
        f'"vmcp_report_begin={begin_marker}" '
        "$vmcp_report "
        f'"vmcp_report_end={end_marker}"] "\\n"'
    )


def qor_summary_command() -> str:
    return (
        "if {[llength [info commands report_qor_summary]] > 0} {"
        "report_qor_summary -return_string"
        "} else {"
        "join [list \"qor_unavailable=1\" \"message=report_qor_summary is unavailable in this Vivado version\"] \"\\n\""
        "}"
    )


def managed_xdc_payload(
    *,
    name: str,
    constraints: list[dict[str, Any]],
) -> dict[str, Any]:
    filename = _managed_xdc_filename(name)
    content = render_xdc_constraints(constraints)
    return {
        "filename": filename,
        "content": content,
        "content_bytes": content.encode("utf-8"),
        "constraint_count": len(constraints),
    }


def add_managed_xdc_command(
    *,
    xdc_path: str | Path,
    fileset: str = "constrs_1",
    constraint_count: int,
) -> str:
    return (
        f"set xdc_path [file normalize {tcl_list_quote(str(xdc_path))}]; "
        "if {![file isfile $xdc_path]} {error {Managed XDC was not prepared by the MCP filesystem broker}}; "
        "if {![string equal -nocase [file extension $xdc_path] {.xdc}]} {error {Managed constraints must use an .xdc file}}; "
        f"add_files -fileset {tcl_list_quote(fileset)} $xdc_path; "
        f"update_compile_order -fileset {tcl_list_quote(fileset)}; "
        "join [list "
        "\"path=$xdc_path\" "
        f"{tcl_list_quote(f'fileset={fileset}')} "
        f"\"constraint_count={constraint_count}\""
        "] \"\\n\""
    )


def _managed_xdc_filename(name: str) -> str:
    raw = str(name).strip()
    if not raw or raw in {".", ".."}:
        raise ValueError("Managed XDC name must be a file name, not a path")
    filename = raw if raw.lower().endswith(".xdc") else f"{raw}.xdc"
    if any(separator in filename for separator in ("/", "\\")) or ":" in filename:
        raise ValueError("Managed XDC name must be a file name, not a path")
    return filename


def render_xdc_constraints(constraints: list[dict[str, Any]]) -> str:
    lines = ["# Generated by Vivado Agent MCP managed XDC. Do not edit user XDC files in place."]
    for item in constraints:
        lines.append(_render_constraint(item))
    return "\n".join(lines) + "\n"


def parse_constraints_summary(raw: str) -> dict[str, Any]:
    required_markers_present = all(
        marker in raw
        for marker in (f"clock_report_begin={CLOCK_REPORT_MARKER}", f"xdc_begin={XDC_MARKER}")
    )
    metadata_text, xdc_text = _split_marker(raw, f"xdc_begin={XDC_MARKER}")
    metadata_text, clock_report = _split_marker(metadata_text, f"clock_report_begin={CLOCK_REPORT_MARKER}")
    metadata = _parse_key_value_lines(metadata_text)
    xdc_files = decode_wire_list(metadata.get("xdc_files", ""))
    ports = decode_wire_list(metadata.get("ports", ""))
    clocks = decode_wire_list(metadata.get("clocks", ""))
    xdc_file_discovery_status = metadata.get("xdc_file_discovery_status", "")
    xdc_file_discovery_reason = metadata.get("xdc_file_discovery_reason", "")
    probe_names = (
        "fileset_discovery_status",
        "xdc_file_discovery_status",
        "design_discovery_status",
        "ports_discovery_status",
        "clocks_discovery_status",
        "generated_clocks_discovery_status",
        "clock_report_discovery_status",
    )
    allowed_probe_statuses = {"READY", "EMPTY", "NOT_APPLICABLE", "ERROR"}
    probe_statuses = {name: metadata.get(name, "") for name in probe_names}
    discovery_errors = decode_wire_list(metadata.get("discovery_errors", ""))
    for name, value in probe_statuses.items():
        if value not in allowed_probe_statuses:
            discovery_errors.append(f"{name} is missing or invalid: {value or '<missing>'}")
    if not required_markers_present:
        discovery_errors.append("constraints summary response is missing required wire markers")
    counts = {command: 0 for command in XDC_COMMANDS}
    for line in xdc_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for command in counts:
            if stripped.startswith(command):
                counts[command] += 1
    findings = []
    required_discovery_failed = bool(discovery_errors) or any(value == "ERROR" for value in probe_statuses.values())
    if required_discovery_failed:
        status = "BLOCK"
        findings.append(
            {
                "severity": "BLOCK",
                "code": "CONSTRAINT_DISCOVERY_FAILED",
                "message": "One or more required Vivado constraint discovery queries failed.",
            }
        )
    elif xdc_file_discovery_status == "EMPTY":
        status = "WARN"
        xdc_file_discovery_reason = xdc_file_discovery_reason or "Vivado completed discovery but returned no XDC file paths."
        findings.append(
            {
                "severity": "WARN",
                "code": "XDC_FILE_DISCOVERY_WARN",
                "message": xdc_file_discovery_reason,
            }
        )
    else:
        status = "READY"
    return {
        "ok": status != "BLOCK",
        "error_code": "CONSTRAINT_DISCOVERY_FAILED" if status == "BLOCK" else "",
        "status": status,
        "findings": findings,
        "xdc_files": xdc_files,
        "xdc_file_discovery_status": xdc_file_discovery_status,
        "xdc_file_discovery_reason": xdc_file_discovery_reason,
        "probe_statuses": probe_statuses,
        "discovery_errors": list(dict.fromkeys(discovery_errors)),
        "ports": ports,
        "clocks": clocks,
        "generated_clocks": decode_wire_list(metadata.get("generated_clocks", "")),
        "counts": counts,
        "clock_summary": parse_clock_summary(clock_report),
        "raw": raw,
        "raw_excerpt": _excerpt(xdc_text or raw),
    }


def parse_check_timing_report(raw: str) -> dict[str, Any]:
    report_text, attested, attestation = _extract_report_attestation(
        raw,
        report_type="check_timing",
        begin_marker=CHECK_TIMING_REPORT_BEGIN_MARKER,
    )
    counts = {key: 0 for key in CHECK_TIMING_KEYS}
    schema_text, detail_text, toc_present = _check_timing_schema_sections(report_text)
    matches = re.findall(r"checking\s+([a-zA-Z0-9_]+)\s*\((\d+)\)", schema_text)
    occurrences: dict[str, int] = {}
    unknown_keys: list[str] = []
    for key, value in matches:
        normalized = key.strip().lower()
        if normalized in counts:
            counts[normalized] = int(value)
            occurrences[normalized] = occurrences.get(normalized, 0) + 1
        else:
            unknown_keys.append(normalized)
    missing_keys = sorted(set(CHECK_TIMING_KEYS) - set(occurrences))
    duplicate_keys = sorted(key for key, count in occurrences.items() if count != 1)
    unknown_keys = sorted(set(unknown_keys))
    detail_missing_keys: list[str] = []
    detail_duplicate_keys: list[str] = []
    detail_unknown_keys: list[str] = []
    detail_count_mismatches: list[str] = []
    if detail_text:
        detail_occurrences: dict[str, list[int]] = {}
        for key, value in re.findall(r"checking\s+([a-zA-Z0-9_]+)\s*\((\d+)\)", detail_text):
            normalized = key.strip().lower()
            if normalized not in counts:
                detail_unknown_keys.append(normalized)
                continue
            detail_occurrences.setdefault(normalized, []).append(int(value))
        detail_missing_keys = sorted(set(CHECK_TIMING_KEYS) - set(detail_occurrences))
        detail_duplicate_keys = sorted(key for key, values in detail_occurrences.items() if len(values) != 1)
        detail_unknown_keys = sorted(set(detail_unknown_keys))
        detail_count_mismatches = sorted(
            key
            for key, values in detail_occurrences.items()
            if len(values) == 1 and key in occurrences and values[0] != counts[key]
        )
    blocking_checks = [key for key in BLOCKING_CHECKS if counts.get(key, 0) > 0]
    warning_checks = [key for key in WARNING_CHECKS if counts.get(key, 0) > 0]
    structurally_recognized = bool(re.search(r"(?im)^\s*check_timing\s+report(?:\s*:\s*no\s+violations)?\s*$", report_text))
    structure_recognized = bool(matches) or structurally_recognized
    complete = bool(
        attested
        and matches
        and not missing_keys
        and not duplicate_keys
        and not unknown_keys
        and not detail_missing_keys
        and not detail_duplicate_keys
        and not detail_unknown_keys
        and not detail_count_mismatches
        and (not toc_present or bool(detail_text))
    )
    parsed = complete
    status = "BLOCK" if not parsed or blocking_checks else "WARN" if warning_checks else "READY"
    return {
        "ok": True,
        "parsed": parsed,
        "report_attested": attested,
        "transport_attested": attested,
        "report_attestation": attestation,
        "error_code": "" if attested else "REPORT_VERSION_MISMATCH",
        "structure_recognized": structure_recognized,
        "complete": complete,
        "schema_version": CHECK_TIMING_SCHEMA_VERSION,
        "expected_keys": list(CHECK_TIMING_KEYS),
        "observed_keys": sorted(occurrences),
        "missing_keys": missing_keys,
        "duplicate_keys": duplicate_keys,
        "unknown_keys": unknown_keys,
        "detail_missing_keys": detail_missing_keys,
        "detail_duplicate_keys": detail_duplicate_keys,
        "detail_unknown_keys": detail_unknown_keys,
        "detail_count_mismatches": detail_count_mismatches,
        "toc_present": toc_present,
        "detail_section_present": bool(detail_text),
        "status": status,
        "counts": counts,
        "blocking_checks": sorted(blocking_checks),
        "warning_checks": sorted(warning_checks),
        "raw": raw,
        "raw_excerpt": _excerpt(raw),
    }


def parse_methodology_report(raw: str) -> dict[str, Any]:
    report_text, attested, attestation = _extract_report_attestation(
        raw,
        report_type="methodology",
        begin_marker=METHODOLOGY_REPORT_BEGIN_MARKER,
    )
    messages = parse_messages(report_text)
    rule_summary = parse_vivado_rule_violations(report_text)
    counts = {
        severity: max(messages["counts"][severity], rule_summary["counts"][severity])
        for severity in ("ERROR", "CRITICAL WARNING", "WARNING", "INFO")
    }
    header_recognized = bool(
        re.search(
            r"(?im)^\s*(?:report\s+methodology|methodology\s+(?:report|summary)(?::\s*no\s+violations)?)\s*$",
            report_text,
        )
    )
    explicit_empty = bool(re.search(r"(?im)(?:methodology\s+(?:report|summary)\s*:\s*no\s+violations|no\s+methodology\s+violations)", report_text))
    violation_summary = rule_summary["summary_count"] is not None
    structure_recognized = header_recognized or violation_summary
    observed_count = rule_summary["parsed_count"]
    if not rule_summary["violations"]:
        observed_count = len(
            [
                message
                for message in messages["messages"]
                if str(message.get("id", "")).upper().startswith("METHODOLOGY ")
            ]
        )
    summary_consistent = rule_summary["summary_count"] is None or rule_summary["summary_count"] == observed_count
    complete = bool(
        attested
        and not rule_summary["duplicate_summary"]
        and not rule_summary["malformed_rows"]
        and summary_consistent
        and (explicit_empty or (header_recognized and violation_summary and rule_summary["table_recognized"]))
    )
    parsed = complete
    unclassified_violations = bool(
        rule_summary["summary_count"]
        and not rule_summary["violations"]
        and not observed_count
    )
    status = (
        "BLOCK"
        if not parsed or unclassified_violations or counts["ERROR"] or counts["CRITICAL WARNING"]
        else "WARN"
        if counts["WARNING"] or counts["INFO"]
        else "READY"
    )
    return {
        "ok": True,
        "parsed": parsed,
        "report_attested": attested,
        "transport_attested": attested,
        "report_attestation": attestation,
        "structure_recognized": structure_recognized,
        "complete": complete,
        "status": status,
        "counts": counts,
        "violation_summary_count": rule_summary["summary_count"],
        "parsed_violation_count": observed_count,
        "summary_consistent": summary_consistent,
        "duplicate_summary": rule_summary["duplicate_summary"],
        "table_recognized": rule_summary["table_recognized"],
        "malformed_rows": rule_summary["malformed_rows"],
        "unclassified_violations": unclassified_violations,
        "messages": messages["messages"] or rule_summary["violations"],
        "raw": raw,
        "raw_excerpt": _excerpt(raw),
    }


def _check_timing_schema_sections(report_text: str) -> tuple[str, str, bool]:
    toc = re.search(r"(?im)^\s*Table of Contents\s*$", report_text)
    if not toc:
        return report_text, "", False
    no_clock_headings = list(re.finditer(r"(?im)^\s*1\.\s+checking\s+no_clock\s*\(\d+\)\s*$", report_text[toc.end() :]))
    if len(no_clock_headings) < 2:
        return report_text[toc.end() :], "", True
    detail_start = toc.end() + no_clock_headings[1].start()
    return report_text[toc.end() : detail_start], report_text[detail_start:], True


def parse_clock_summary(raw: str) -> dict[str, Any]:
    _, clock_report = _split_marker(raw, f"clock_report_begin={CLOCK_REPORT_MARKER}")
    text = clock_report or raw
    clocks: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _is_clock_report_non_data_line(stripped):
            continue
        match = re.match(r"(?P<name>[A-Za-z_][^\s]*)\s+(?P<period>[-+]?(?:\d+(?:\.\d*)?|\.\d+))", stripped)
        if match:
            clocks.append(
                {
                    "name": match.group("name"),
                    "period_ns": float(match.group("period")),
                    "raw": stripped,
                }
            )
    return {"ok": True, "clocks": clocks, "raw": raw, "raw_excerpt": _excerpt(text)}


def _is_clock_report_non_data_line(line: str) -> bool:
    lower = line.lower()
    if lower.startswith(("clock ", "----", "name ", "period", "waveform", "source", "generated", "vivado", "date:", "host:", "command:")):
        return True
    if "copyright" in lower or "all rights reserved" in lower:
        return True
    if set(line) <= {"-", "=", " ", "\t"}:
        return True
    return False


def parse_timing_paths(raw: str) -> dict[str, Any]:
    summary = parse_timing_summary(raw)
    path: dict[str, Any] = {}
    slack = re.search(r"Slack\s*\([^)]*\)\s*:\s*(?P<slack>[-+]?(?:\d+(?:\.\d*)?|\.\d+))", raw, flags=re.IGNORECASE)
    if slack:
        path["slack_ns"] = float(slack.group("slack"))
    labels = {
        "path_group": "Path Group",
        "from_clock": "From Clock",
        "to_clock": "To Clock",
        "endpoint": "Endpoint",
    }
    for key, label in labels.items():
        match = re.search(rf"{re.escape(label)}\s*:\s*(.+)", raw, flags=re.IGNORECASE)
        if match:
            path[key] = match.group(1).strip()
    paths = [path] if path else []
    return {"ok": True, "summary": summary, "paths": paths, "raw": raw, "raw_excerpt": _excerpt(raw)}


def parse_qor_summary(raw: str) -> dict[str, Any]:
    values = _parse_key_value_lines(raw)
    available = values.get("qor_unavailable", "0") != "1"
    design_score = _first_int_after("Design Score", raw)
    wns_ns = _first_float_after("WNS", raw)
    tns_ns = _first_float_after("TNS", raw)
    messages = parse_messages(raw)
    structure_recognized = not available or any(value is not None for value in (design_score, wns_ns, tns_ns))
    complete = not available or (wns_ns is not None and tns_ns is not None)
    status = (
        "WARN"
        if not available
        else "BLOCK"
        if not complete or messages["counts"]["ERROR"] or messages["counts"]["CRITICAL WARNING"]
        else "WARN"
        if messages["counts"]["WARNING"]
        else "READY"
    )
    return {
        "ok": True,
        "available": available,
        "parsed": complete,
        "structure_recognized": structure_recognized,
        "complete": complete,
        "status": status,
        "message": values.get("message", ""),
        "design_score": design_score,
        "wns_ns": wns_ns,
        "tns_ns": tns_ns,
        "messages": messages,
        "raw": raw,
        "raw_excerpt": _excerpt(raw),
    }


def parse_timing_closure_analysis(
    *,
    timing: dict[str, Any],
    check_timing: dict[str, Any],
    methodology: dict[str, Any],
    drc: dict[str, Any],
    critical_warnings: dict[str, Any],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if timing.get("timing_met") is False:
        findings.append(
            {
                "severity": "BLOCK",
                "code": "TIMING_NOT_MET",
                "message": "Timing is not met",
                "detail": {"wns_ns": timing.get("wns_ns"), "tns_ns": timing.get("tns_ns")},
            }
        )
    for key in sorted(BLOCKING_CHECKS):
        value = check_timing.get("counts", {}).get(key, 0)
        if value:
            findings.append({"severity": "BLOCK", "code": f"CHECK_TIMING_{key.upper()}", "message": f"check_timing reports {key}={value}"})
    for key in sorted(WARNING_CHECKS):
        value = check_timing.get("counts", {}).get(key, 0)
        if value:
            findings.append({"severity": "WARN", "code": f"CHECK_TIMING_{key.upper()}", "message": f"check_timing reports {key}={value}"})
    method_counts = methodology.get("counts", {})
    if method_counts.get("ERROR", 0):
        findings.append({"severity": "BLOCK", "code": "METHODOLOGY_ERROR", "message": f"Methodology contains {method_counts['ERROR']} error(s)"})
    if method_counts.get("CRITICAL WARNING", 0):
        findings.append({"severity": "BLOCK", "code": "METHODOLOGY_CRITICAL_WARNING", "message": f"Methodology contains {method_counts['CRITICAL WARNING']} critical warning(s)"})
    if method_counts.get("WARNING", 0):
        findings.append({"severity": "WARN", "code": "METHODOLOGY_WARNING", "message": f"Methodology contains {method_counts['WARNING']} warning(s)"})
    if drc.get("error_count", 0):
        findings.append({"severity": "BLOCK", "code": "DRC_ERROR", "message": f"DRC contains {drc['error_count']} error(s)"})
    if drc.get("critical_warning_count", 0):
        findings.append({"severity": "BLOCK", "code": "DRC_CRITICAL_WARNING", "message": f"DRC contains {drc['critical_warning_count']} critical warning(s)"})
    critical_counts = critical_warnings.get("counts", {})
    if critical_counts.get("ERROR", 0):
        findings.append({"severity": "BLOCK", "code": "RUN_ERROR", "message": f"Run messages contain {critical_counts['ERROR']} error(s)"})
    if critical_counts.get("CRITICAL WARNING", 0):
        findings.append({"severity": "BLOCK", "code": "RUN_CRITICAL_WARNING", "message": f"Run messages contain {critical_counts['CRITICAL WARNING']} critical warning(s)"})
    status = "BLOCK" if any(item["severity"] == "BLOCK" for item in findings) else "WARN" if findings else "READY"
    return {"ok": True, "status": status, "findings": findings}


def parse_managed_xdc_result(raw: str) -> dict[str, Any]:
    values = _parse_key_value_lines(raw)
    return {
        "path": values.get("path", ""),
        "fileset": values.get("fileset", ""),
        "constraint_count": int(values.get("constraint_count", "0") or 0),
    }


def _render_constraint(item: dict[str, Any]) -> str:
    kind = str(item["type"])
    if kind == "create_clock":
        line = f"create_clock -period {tcl_list_quote(item['period'])}"
        if item.get("name") is not None:
            line += f" -name {tcl_list_quote(item['name'])}"
        return f"{line} {_ports_expr(_as_list(item.get('ports') or item.get('port')))}"
    if kind in {"set_input_delay", "set_output_delay"}:
        return f"{kind} -clock {tcl_list_quote(item['clock'])} {tcl_list_quote(item['delay'])} {_ports_expr(_as_list(item.get('ports') or item.get('port')))}"
    if kind == "set_false_path":
        return _timing_exception_line("set_false_path", item)
    if kind == "set_multicycle_path":
        options = [tcl_list_quote(item.get("cycles", 1))]
        if item.get("setup"):
            options.append("-setup")
        if item.get("hold"):
            options.append("-hold")
        return _timing_exception_line("set_multicycle_path " + " ".join(options), item)
    if kind == "set_property":
        target = item.get("target")
        if target == "current_design":
            target_expr = "[current_design]"
        else:
            target_expr = _ports_expr(_as_list(item.get("ports") or item.get("port")))
        return f"set_property {tcl_list_quote(item['property'])} {tcl_list_quote(item['value'])} {target_expr}"
    raise ValueError(f"Unsupported managed XDC constraint type: {kind}")


def _timing_exception_line(command: str, item: dict[str, Any]) -> str:
    parts = [command]
    if item.get("from"):
        parts.extend(["-from", _ports_expr(_as_list(item["from"]))])
    if item.get("to"):
        parts.extend(["-to", _ports_expr(_as_list(item["to"]))])
    if item.get("through"):
        parts.extend(["-through", _ports_expr(_as_list(item["through"]))])
    return " ".join(parts)


def _ports_expr(values: list[Any]) -> str:
    if len(values) == 1:
        return f"[get_ports {tcl_list_quote(values[0])}]"
    return "[get_ports [list " + " ".join(tcl_list_quote(value) for value in values) + "]]"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _parse_key_value_lines(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _split_marker(raw: str, marker_line: str) -> tuple[str, str]:
    if marker_line not in raw:
        return raw, ""
    before, after = raw.split(marker_line, 1)
    return before, after.lstrip("\r\n")


def _first_float_after(label: str, raw: str) -> float | None:
    match = re.search(rf"{re.escape(label)}(?:\([^)]*\))?\s*:?\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))", raw, flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


def _first_int_after(label: str, raw: str) -> int | None:
    match = re.search(rf"{re.escape(label)}\s*:?\s*(\d+)", raw, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _excerpt(text: str, limit: int = 4096) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]
