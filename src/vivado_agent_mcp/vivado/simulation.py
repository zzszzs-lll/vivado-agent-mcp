from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Any

from .managed_path import ManagedPathError, is_reparse_point, validate_managed_path
from .parsers import parse_messages
from .runs import run_hook_guard_command
from .tcl import tcl_list_quote
from .wire import HEX_ROW_PREFIX, decode_wire_list, decode_wire_row, tcl_wire_prelude

LOG_MARKER = "__VMCP_LOG_BEGIN__"
DEFAULT_MAX_VCD_MB = 256
MAX_VCD_CHECKPOINTS = 256
MAX_PREFLIGHT_SOURCE_BYTES = 4 * 1024 * 1024
MAX_IDENTITY_SOURCE_BYTES = 64 * 1024 * 1024
MAX_INCLUDE_CLOSURE_FILES = 4096
MAX_INCLUDE_CLOSURE_DEPTH = 128
_INCLUDE_DIRECTIVE_RE = re.compile(r"(?im)^[ \t]*`include\b(?P<operand>[^\r\n]*)")
_LITERAL_INCLUDE_RE = re.compile(r'^"(?P<path>[^"\r\n]+)"\s*$')
_DEFINE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DEFINE_VALUE_RE = re.compile(r"[A-Za-z0-9_+./:'?=-]+\Z")
_FOREIGN_EXECUTABLE_SUFFIXES = {".a", ".c", ".cc", ".cpp", ".cxx", ".dll", ".lib", ".o", ".obj", ".so"}
_UNTRUSTED_SIMULATION_MODEL_RE = re.compile(
    r"(?im)\b(?:PREFERRED_SIM_MODEL|SELECTED_SIM_MODEL)\b[^\r\n]*(?:tlm_dpi|tlm|dpi|systemc)\b"
)
_TRUSTED_VERILOG_SUFFIXES = {".v", ".sv", ".vh", ".svh"}
_TRUSTED_DESIGN_SOURCE_SUFFIXES = _TRUSTED_VERILOG_SUFFIXES | {".vhd", ".vhdl"}
_STATICALLY_ALLOWED_PREPROCESSOR_DIRECTIVES = {
    "begin_keywords",
    "celldefine",
    "default_nettype",
    "else",
    "elsif",
    "end_keywords",
    "endcelldefine",
    "endif",
    "ifdef",
    "ifndef",
    "include",
    "line",
    "nounconnected_drive",
    "resetall",
    "timescale",
    "unconnected_drive",
}
_TRUSTED_SYSTEM_IDENTIFIERS = {
    "acos",
    "acosh",
    "asin",
    "asinh",
    "assertcontrol",
    "assertkill",
    "assertoff",
    "asserton",
    "atan",
    "atan2",
    "atanh",
    "bitstoreal",
    "bits",
    "cast",
    "ceil",
    "changed",
    "clog2",
    "cos",
    "cosh",
    "countbits",
    "countones",
    "dimensions",
    "display",
    "displayb",
    "displayh",
    "displayo",
    "dumpall",
    "dumpfile",
    "dumpflush",
    "dumplimit",
    "dumpoff",
    "dumpon",
    "dumpvars",
    "error",
    "exp",
    "fatal",
    "fclose",
    "fdisplay",
    "feof",
    "ferror",
    "fflush",
    "fgetc",
    "fgets",
    "finish",
    "floor",
    "fmonitor",
    "fopen",
    "fread",
    "fscanf",
    "fseek",
    "fstrobe",
    "ftell",
    "future_gclk",
    "fwrite",
    "global_clock",
    "high",
    "hypot",
    "increment",
    "info",
    "isunknown",
    "itor",
    "left",
    "ln",
    "log10",
    "low",
    "monitor",
    "monitorb",
    "monitorh",
    "monitoro",
    "onehot",
    "onehot0",
    "past",
    "pow",
    "printtimescale",
    "random",
    "readmemb",
    "readmemh",
    "realtime",
    "realtobits",
    "rewind",
    "right",
    "root",
    "rose",
    "rtoi",
    "sampled",
    "sdf_annotate",
    "sformat",
    "sformatf",
    "signed",
    "sin",
    "sinh",
    "size",
    "sqrt",
    "sscanf",
    "stable",
    "stime",
    "strobe",
    "strobeb",
    "strobeh",
    "strobeo",
    "tan",
    "tanh",
    "test$plusargs",
    "time",
    "timeformat",
    "typename",
    "ungetc",
    "unit",
    "unpacked_dimensions",
    "unsigned",
    "urandom",
    "urandom_range",
    "value$plusargs",
    "warning",
    "write",
    "writeb",
    "writeh",
    "writememb",
    "writememh",
    "writeo",
}
XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION = "vivado_2021_2_v1"
_XSIM_EXECUTABLE_OPTION_PROPERTIES = (
    "xsim.compile.tcl.pre",
    "xsim.compile.tcl.post",
    "xsim.compile.xvlog.more_options",
    "xsim.compile.xvlog.more_option",
    "xsim.compile.xvhdl.more_options",
    "xsim.compile.xvhdl.more_option",
    "xsim.compile.xsc.more_options",
    "xsim.compile.xsc.more_option",
    "xsim.elaborate.tcl.pre",
    "xsim.elaborate.tcl.post",
    "xsim.elaborate.xelab.more_options",
    "xsim.elaborate.xelab.more_option",
    "xsim.simulate.tcl.pre",
    "xsim.simulate.tcl.post",
    "xsim.simulate.custom_tcl",
    "xsim.simulate.xsim.more_options",
    "xsim.simulate.xsim.more_option",
)
_TIME_UNIT_FS = {
    "fs": Decimal(1),
    "ps": Decimal(1_000),
    "ns": Decimal(1_000_000),
    "us": Decimal(1_000_000_000),
    "ms": Decimal(1_000_000_000_000),
    "s": Decimal(1_000_000_000_000_000),
}


def configure_simulation_command(
    *,
    sim_files: list[str] | None = None,
    testbench_top: str | None = None,
    include_dirs: list[str] | None = None,
    defines: dict[str, str | None] | None = None,
    simulator: str = "Vivado Simulator",
    simset: str = "sim_1",
) -> str:
    simset_ref = tcl_list_quote(simset)
    parts = [
        f"set_property target_simulator {tcl_list_quote(simulator)} [current_project]",
    ]
    if sim_files:
        parts.append(f"add_files -fileset {simset_ref} {_tcl_list(sim_files)}")
    if testbench_top:
        parts.append(f"set_property top {tcl_list_quote(testbench_top)} [get_filesets {simset_ref}]")
    if include_dirs:
        parts.append(f"set_property include_dirs {_tcl_list(include_dirs)} [get_filesets {simset_ref}]")
    if defines:
        parts.append(f"set_property verilog_define {_define_list(defines)} [get_filesets {simset_ref}]")
    parts.append(f"update_compile_order -fileset {simset_ref}")
    return "; ".join(parts)


def validate_simulation_defines(defines: Any) -> dict[str, str | None]:
    if defines is None:
        return {}
    if not isinstance(defines, dict):
        raise ValueError("simulation defines must be an object")
    validated: dict[str, str | None] = {}
    for raw_name, raw_value in defines.items():
        name = str(raw_name)
        if not _DEFINE_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid simulation define name: {name!r}")
        if raw_value is None:
            validated[name] = None
            continue
        value = str(raw_value)
        if not value or not _DEFINE_VALUE_RE.fullmatch(value):
            raise ValueError(f"unsafe simulation define value for {name}: {value!r}")
        validated[name] = value
    return validated


def behavioral_simulation_command(
    *,
    simset: str = "sim_1",
    run_time: str = "1 us",
    run_all: bool = False,
    export_vcd: bool = False,
    vcd_name: str = "vmcp_behav.vcd",
    max_vcd_mb: int | float = DEFAULT_MAX_VCD_MB,
    skip_mcp_vcd: bool = False,
    testbench_vcd_usage: bool = False,
    simulation_invocation_id: str | None = None,
    started_at: str = "",
    monitored_waveform_paths: list[str] | None = None,
    source_identity_sha256: str = "",
    stable_input_identity_sha256: str = "",
    session_generation_id: str = "",
    incremental: bool | None = None,
) -> str:
    simset_ref = tcl_list_quote(simset)
    max_vcd_bytes = _max_vcd_bytes(max_vcd_mb)
    testbench_existing = bool(testbench_vcd_usage or skip_mcp_vcd)
    vcd_risk = bool(export_vcd or testbench_existing)
    if export_vcd and not skip_mcp_vcd:
        vcd_export_mode = "mcp_open_vcd"
    elif testbench_existing:
        vcd_export_mode = "testbench_existing"
    else:
        vcd_export_mode = "disabled"
    parts = [
        run_hook_guard_command(),
        _sim_artifact_setup_command(simset),
        f"set vmcp_simulation_invocation_id {tcl_list_quote(simulation_invocation_id or _new_invocation_id())}",
        f"set vmcp_simulation_started_at {tcl_list_quote(started_at)}",
        f"set vmcp_monitored_waveform_paths {_tcl_list(monitored_waveform_paths or [])}",
        f"set vmcp_simulation_source_identity_sha256 {tcl_list_quote(source_identity_sha256)}",
        f"set vmcp_simulation_stable_input_identity_sha256 {tcl_list_quote(stable_input_identity_sha256)}",
        f"set vmcp_session_generation_id {tcl_list_quote(session_generation_id)}",
        _simulation_context_snapshot_command("before"),
        "set vmcp_log_previous_path \"\"",
        "set vmcp_log_previous_size 0",
        "set vmcp_log_previous_mtime 0",
        "foreach vmcp_log_candidate [list [file join $sim_dir xsim.log] [file join $sim_dir simulate.log]] {"
        "if {$vmcp_log_previous_path eq \"\" && [file exists $vmcp_log_candidate]} {"
        "set vmcp_log_previous_path $vmcp_log_candidate; "
        "catch {set vmcp_log_previous_size [file size $vmcp_log_candidate]}; "
        "catch {set vmcp_log_previous_mtime [file mtime $vmcp_log_candidate]}"
        "}"
        "}",
        "set vmcp_vcd_conflict 0",
        "set vmcp_vcd_conflict_severity \"\"",
        f"set vmcp_vcd_export_mode {tcl_list_quote(vcd_export_mode)}",
        f"set vmcp_mcp_vcd_export_mode {tcl_list_quote(vcd_export_mode)}",
        f"set vmcp_export_vcd_requested {1 if export_vcd else 0}",
        f"set vmcp_vcd_limit_bytes {max_vcd_bytes}",
        "set vmcp_vcd_limit_exceeded 0",
        "set vmcp_vcd_limit_stopped 0",
        "set vmcp_run_tcl_failed 0",
        "set vmcp_run_error \"\"",
        "set vmcp_breakpoints_cleared 0",
        f"set vmcp_testbench_vcd_usage {1 if testbench_existing else 0}",
        f"set vmcp_testbench_vcd_detected {1 if testbench_existing else 0}",
        "catch {close_sim}",
    ]
    if incremental is not None:
        parts.append(f"set_property INCREMENTAL {1 if incremental else 0} [get_filesets {simset_ref}]")
    parts.append(f"launch_simulation -simset {simset_ref} -mode behavioral")
    parts.append("if {![catch {remove_bps -all -quiet}]} {set vmcp_breakpoints_cleared 1}")
    if export_vcd and skip_mcp_vcd:
        parts.append("set vmcp_vcd_conflict 1")
        parts.append("set vmcp_vcd_conflict_severity info")
    elif testbench_existing:
        parts.append("set vmcp_vcd_conflict_severity info")
    if export_vcd and not skip_mcp_vcd:
        vcd_path = "[file join $sim_dir " + tcl_list_quote(vcd_name) + "]"
        parts.extend(
            [
                f"set vcd_path {vcd_path}",
                "catch {open_vcd $vcd_path}",
                "catch {log_vcd [get_objects -r /*]}",
                "catch {log_vcd [get_objects -r *]}",
            ]
        )
    if not run_all and max_vcd_bytes > 0:
        parts.append(_bounded_vcd_run_command(run_time))
    else:
        run_command = "run all" if run_all else f"run {tcl_list_quote(_xsim_time(run_time))}"
        parts.append(f"if {{[catch {{{run_command}}} vmcp_run_error]}} {{set vmcp_run_tcl_failed 1}}")
    parts.append(
        "if {$vmcp_run_tcl_failed} {"
        "set vmcp_run_error [string range [string map [list \"\\r\" \" \" \"\\n\" \" \"] $vmcp_run_error] 0 4095]"
        "}"
    )
    parts.append("set vmcp_simulation_ended_at [clock format [clock seconds] -gmt true -format {%Y-%m-%dT%H:%M:%SZ}]")
    if run_all or max_vcd_bytes <= 0 or not vcd_risk:
        parts.append(_vcd_limit_check_command())
    if export_vcd:
        parts.append("catch {close_vcd}")
    parts.append("catch {close_sim}")
    parts.append(_simulation_context_snapshot_command("after"))
    parts.append(
        simulation_result_read_command(
            simset=simset,
            include_log=True,
            status_source="simulation_invocation_log_span",
        )
    )
    return "; ".join(parts)


def managed_simulation_policy_preflight_command(*, simset: str = "sim_1") -> str:
    simset_ref = tcl_list_quote(simset)
    policy = (
        f"set vmcp_policy_simset [get_filesets -quiet {simset_ref}]; "
        "if {[llength $vmcp_policy_simset] != 1} {error {VMCP_SIMSET_NOT_FOUND: trusted XSIM requires one simulation fileset}}; "
        "set vmcp_incremental_before [get_property INCREMENTAL $vmcp_policy_simset]; "
        "if {[expr {bool($vmcp_incremental_before)}]} {set_property INCREMENTAL 0 $vmcp_policy_simset}; "
        "set vmcp_incremental_after [get_property INCREMENTAL $vmcp_policy_simset]; "
        "if {[expr {bool($vmcp_incremental_after)}]} {error {VMCP_SIMULATION_INCREMENTAL_POLICY_FAILED: INCREMENTAL remained enabled}}"
    )
    return f"{policy}; {simulation_vcd_preflight_command(simset=simset)}"


def simulation_vcd_preflight_command(*, simset: str = "sim_1") -> str:
    setup = _sim_artifact_setup_command(simset)
    return (
        f"{setup}; "
        "set testbench_vcd_usage 0; "
        "set testbench_vcd_sources [list]; "
        "set preflight_errors [list]; "
        "set fs [get_filesets $simset]; "
        "set project_path [file normalize [file join $project_dir ${project_name}.xpr]]; "
        "set vivado_version_short [version -short]; "
        "set vivado_version_full [version]; "
        "set project_properties {}; catch {set project_properties [report_property -return_string [current_project]]}; "
        "set simset_properties_report {}; catch {set simset_properties_report [report_property -return_string $fs]}; "
        "set sim_files [list]; "
        "if {[catch {set sim_files [get_files -quiet -compile_order sources -used_in simulation -of_objects $fs]}]} {set sim_files [get_files -quiet -of_objects $fs]}; "
        "set include_dirs [list]; catch {set include_dirs [get_property include_dirs $fs]}; "
        "set verilog_defines [list]; catch {set verilog_defines [get_property verilog_define $fs]}; "
        "set target_simulator {}; catch {set target_simulator [get_property target_simulator [current_project]]}; "
        f"set simulator_property_schema_version {tcl_list_quote(XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION)}; "
        f"set vmcp_executable_property_names {_tcl_list(list(_XSIM_EXECUTABLE_OPTION_PROPERTIES))}; "
        "set vmcp_simset_properties [list]; catch {set vmcp_simset_properties [list_property $fs]}; "
        "foreach vmcp_property $vmcp_simset_properties {"
        "set vmcp_property_lower [string tolower $vmcp_property]; "
        "if {[string match {xsim.*tcl*} $vmcp_property_lower] || "
        "[string match {xsim.*script*} $vmcp_property_lower] || "
        "[string match {xsim.*custom_tcl*} $vmcp_property_lower] || "
        "[string match {xsim.*more_option*} $vmcp_property_lower]} {"
        "if {[lsearch -exact -nocase $vmcp_executable_property_names $vmcp_property] < 0} {"
        "lappend vmcp_executable_property_names $vmcp_property"
        "}"
        "}"
        "}; "
        "set simulator_options [list]; "
        "set sim_file_metadata [list]; "
        "foreach vmcp_option_property $vmcp_executable_property_names {"
        "set vmcp_option_value {}; catch {set vmcp_option_value [get_property $vmcp_option_property $fs]}; "
        "if {[string trim $vmcp_option_value] ne {}} {lappend simulator_options [list $vmcp_option_property $vmcp_option_value]}"
        "}; "
        "foreach f [get_files -quiet -of_objects $fs] {"
        "set path [file normalize $f]; "
        "set file_type {}; catch {set file_type [get_property FILE_TYPE $f]}; "
        "set library {}; catch {set library [get_property LIBRARY $f]}; "
        "set used_in_simulation {}; catch {set used_in_simulation [get_property USED_IN_SIMULATION $f]}; "
        "set is_global_include {}; catch {set is_global_include [get_property IS_GLOBAL_INCLUDE $f]}; "
        "lappend sim_file_metadata [::vivado_agent_mcp_wire_row [list path $path file_type $file_type library $library used_in_simulation $used_in_simulation is_global_include $is_global_include]]; "
        "if {[file exists $path] && [file isfile $path]} {"
        f"if {{[file size $path] > {MAX_PREFLIGHT_SOURCE_BYTES}}} {{lappend preflight_errors \"source exceeds preflight scan limit: $path\"; continue}}; "
        "if {[catch {set fh [open $path r]; set content [read $fh]; close $fh} read_error]} {"
        "catch {close $fh}; lappend preflight_errors \"could not inspect source $path: $read_error\""
        "} else {"
        "if {[string first {$dumpfile} $content] >= 0 || [string first {$dumpvars} $content] >= 0} {"
        "set testbench_vcd_usage 1; lappend testbench_vcd_sources $path"
        "}"
        "}"
        "}"
        "}; "
        "join [list "
        "\"project_dir=$project_dir\" "
        "\"project_name=$project_name\" "
        "\"project_path=$project_path\" "
        "\"vivado_version_short=$vivado_version_short\" "
        "\"vivado_version_full=[string map {\\n { } \\r { }} $vivado_version_full]\" "
        "\"project_properties=[::vivado_agent_mcp_wire_list [list $project_properties]]\" "
        "\"simset_properties=[::vivado_agent_mcp_wire_list [list $simset_properties_report]]\" "
        "\"simset=$simset\" "
        "\"sim_top=[get_property TOP $fs]\" "
        "\"sim_files=[::vivado_agent_mcp_wire_list $sim_files]\" "
        "\"sim_file_metadata=[::vivado_agent_mcp_wire_list $sim_file_metadata]\" "
        "\"include_dirs=[::vivado_agent_mcp_wire_list $include_dirs]\" "
        "\"verilog_defines=[::vivado_agent_mcp_wire_list $verilog_defines]\" "
        "\"target_simulator=$target_simulator\" "
        "\"simulator_property_schema_version=$simulator_property_schema_version\" "
        "\"simulator_options=[::vivado_agent_mcp_wire_list $simulator_options]\" "
        "\"sim_dir=$sim_dir\" "
        "\"testbench_vcd_usage=$testbench_vcd_usage\" "
        "\"testbench_vcd_sources=[::vivado_agent_mcp_wire_list $testbench_vcd_sources]\" "
        "\"preflight_errors=[::vivado_agent_mcp_wire_list $preflight_errors]\""
        "] \"\\n\""
    )


def simulation_result_read_command(*, simset: str = "sim_1", include_log: bool = True, status_source: str = "latest_log_tail") -> str:
    setup = _sim_artifact_setup_command(simset)
    log_settle = ""
    if status_source == "simulation_invocation_log_span":
        # XSIM can flush simulate.log shortly after `run`/`close_sim` returns.
        # Wait only for the current invocation log to become terminal or stable;
        # latest-log reads remain immediate and retain their stale-evidence rules.
        log_settle = (
            "set vmcp_log_settle_started [clock milliseconds]; "
            "set vmcp_log_settle_deadline [expr {$vmcp_log_settle_started + 3000}]; "
            "set vmcp_log_settle_previous_size -1; "
            "set vmcp_log_settle_stable_samples 0; "
            "while {[clock milliseconds] < $vmcp_log_settle_deadline} {"
            "set vmcp_log_settle_candidates [list]; catch {set vmcp_log_settle_candidates [glob -nocomplain -directory $sim_dir *.log]}; "
            "set vmcp_log_settle_path \"\"; "
            "foreach vmcp_log_settle_preferred [list xsim.log simulate.log] {"
            "if {$vmcp_log_settle_path eq \"\"} {foreach vmcp_log_settle_candidate $vmcp_log_settle_candidates {"
            "if {[file tail $vmcp_log_settle_candidate] eq $vmcp_log_settle_preferred} {set vmcp_log_settle_path $vmcp_log_settle_candidate}"
            "}}"
            "}; "
            "if {$vmcp_log_settle_path ne \"\" && [file exists $vmcp_log_settle_path]} {"
            "set vmcp_log_settle_size 0; catch {set vmcp_log_settle_size [file size $vmcp_log_settle_path]}; "
            "if {$vmcp_log_settle_size > 0 && $vmcp_log_settle_size == $vmcp_log_settle_previous_size} {"
            "incr vmcp_log_settle_stable_samples"
            "} else {set vmcp_log_settle_stable_samples 0}; "
            "set vmcp_log_settle_previous_size $vmcp_log_settle_size; "
            "set vmcp_log_settle_content \"\"; "
            "if {$vmcp_log_settle_size > 0 && ![catch {"
            "set vmcp_log_settle_fh [open $vmcp_log_settle_path r]; "
            "seek $vmcp_log_settle_fh [expr {$vmcp_log_settle_size > 65536 ? $vmcp_log_settle_size - 65536 : 0}] start; "
            "set vmcp_log_settle_content [string tolower [read $vmcp_log_settle_fh]]; "
            "close $vmcp_log_settle_fh"
            "}]} {"
            "if {[string first {finish called} $vmcp_log_settle_content] >= 0 || "
            "[string first {simulation finished} $vmcp_log_settle_content] >= 0 || "
            "[string first {tb_pass} $vmcp_log_settle_content] >= 0 || "
            "[string first {tb_fail} $vmcp_log_settle_content] >= 0} {"
            "set vmcp_log_settle_terminal_detected 1; break"
            "}"
            "}; "
            "if {[expr {[clock milliseconds] - $vmcp_log_settle_started}] >= 1000 && $vmcp_log_settle_stable_samples >= 3} {break}"
            "}; "
            "after 100"
            "}; "
            "set vmcp_log_settle_wait_ms [expr {[clock milliseconds] - $vmcp_log_settle_started}]; "
        )
    log_read = (
        "set content \"\"; "
        "if {$log_path ne \"\" && [file exists $log_path]} {"
        "if {[catch {"
        "set fh [open $log_path r]; "
        "seek $fh 0 end; "
        "set size [tell $fh]; "
        "set vmcp_log_current_size $size; "
        "set vmcp_log_current_mtime 0; catch {set vmcp_log_current_mtime [file mtime $log_path]}; "
        "set vmcp_log_span_reset_detected 0; "
        "set start [expr {$size > 1048576 ? $size - 1048576 : 0}]; "
        "if {$log_path eq $vmcp_log_previous_path && $size >= $vmcp_log_previous_size} {"
        "if {$size == $vmcp_log_previous_size && $vmcp_log_current_mtime != $vmcp_log_previous_mtime} {set start 0; set vmcp_log_span_reset_detected 1} else {set start $vmcp_log_previous_size}"
        "} elseif {$vmcp_log_previous_path ne \"\"} {set vmcp_log_span_reset_detected 1}; "
        "set vmcp_log_span_start $start; "
        "set vmcp_log_span_end $size; "
        "seek $fh $start start; "
        "set content [read $fh]; "
        "close $fh"
        "} read_error]} {"
        "catch {close $fh}; "
        "set content \"WARNING: Vivado Agent MCP could not read simulation log: $read_error\""
        "}}"
        if include_log
        else "set content \"\""
    )
    return (
        f"{setup}; "
        "set vmcp_log_settle_wait_ms 0; set vmcp_log_settle_terminal_detected 0; "
        f"{log_settle}"
        "set log_candidates [list]; catch {set log_candidates [glob -nocomplain -directory $sim_dir *.log]}; "
        "set log_path \"\"; "
        "foreach preferred_log [list xsim.log simulate.log xelab.log xvlog.log] {"
        "if {$log_path eq \"\"} {foreach log_candidate $log_candidates {if {[file tail $log_candidate] eq $preferred_log} {set log_path $log_candidate}}}"
        "}; "
        "if {$log_path eq \"\" && [llength $log_candidates] > 0} {set log_path [lindex $log_candidates end]}; "
        "set wdb_files [list]; catch {set wdb_files [glob -nocomplain -directory $sim_dir *.wdb]}; "
        "set vcd_files [list]; catch {set vcd_files [glob -nocomplain -directory $sim_dir *.vcd]}; "
        "if {[info exists vmcp_monitored_waveform_paths]} {foreach f $vmcp_monitored_waveform_paths {"
        "if {[file exists $f]} {set ext [string tolower [file extension $f]]; "
        "if {$ext eq {.vcd} && [lsearch -exact $vcd_files $f] < 0} {lappend vcd_files $f}; "
        "if {$ext eq {.wdb} && [lsearch -exact $wdb_files $f] < 0} {lappend wdb_files $f}}"
        "}}; "
        "set wdb_rows [list]; set wdb_total_bytes 0; "
        "foreach f $wdb_files {set sz 0; catch {set sz [file size $f]}; incr wdb_total_bytes $sz; lappend wdb_rows [::vivado_agent_mcp_wire_row [list path $f size_bytes $sz]]}; "
        "set vcd_rows [list]; set vcd_total_bytes 0; set vcd_largest_file \"\"; set vcd_largest_bytes 0; "
        "foreach f $vcd_files {set sz 0; catch {set sz [file size $f]}; incr vcd_total_bytes $sz; if {$sz > $vcd_largest_bytes} {set vcd_largest_bytes $sz; set vcd_largest_file $f}; lappend vcd_rows [::vivado_agent_mcp_wire_row [list path $f size_bytes $sz]]}; "
        "if {![info exists vmcp_vcd_conflict]} {set vmcp_vcd_conflict 0}; "
        "if {![info exists vmcp_vcd_conflict_severity]} {set vmcp_vcd_conflict_severity \"\"}; "
        "if {![info exists vmcp_vcd_export_mode]} {set vmcp_vcd_export_mode disabled}; "
        "if {![info exists vmcp_mcp_vcd_export_mode]} {set vmcp_mcp_vcd_export_mode $vmcp_vcd_export_mode}; "
        "if {![info exists vmcp_export_vcd_requested]} {set vmcp_export_vcd_requested 0}; "
        "if {![info exists vmcp_vcd_limit_bytes]} {set vmcp_vcd_limit_bytes 0}; "
        "if {![info exists vmcp_vcd_limit_exceeded]} {set vmcp_vcd_limit_exceeded 0}; "
        "if {![info exists vmcp_vcd_limit_stopped]} {set vmcp_vcd_limit_stopped 0}; "
        "if {![info exists vmcp_run_tcl_failed]} {set vmcp_run_tcl_failed 0}; "
        "if {![info exists vmcp_run_error]} {set vmcp_run_error \"\"}; "
        "if {![info exists vmcp_breakpoints_cleared]} {set vmcp_breakpoints_cleared 0}; "
        "if {![info exists vmcp_testbench_vcd_usage]} {set vmcp_testbench_vcd_usage 0}; "
        "if {![info exists vmcp_testbench_vcd_detected]} {set vmcp_testbench_vcd_detected $vmcp_testbench_vcd_usage}; "
        "if {![info exists vmcp_simulation_source_identity_sha256]} {set vmcp_simulation_source_identity_sha256 \"\"}; "
        "if {![info exists vmcp_simulation_stable_input_identity_sha256]} {set vmcp_simulation_stable_input_identity_sha256 \"\"}; "
        "if {![info exists vmcp_session_generation_id]} {set vmcp_session_generation_id \"\"}; "
        "if {![info exists vmcp_simulation_invocation_id]} {set vmcp_simulation_invocation_id \"\"}; "
        "if {![info exists vmcp_simulation_started_at]} {set vmcp_simulation_started_at \"\"}; "
        "if {![info exists vmcp_simulation_ended_at]} {set vmcp_simulation_ended_at \"\"}; "
        "if {![info exists vmcp_log_previous_path]} {set vmcp_log_previous_path \"\"}; "
        "if {![info exists vmcp_log_previous_size]} {set vmcp_log_previous_size 0}; "
        "if {![info exists vmcp_log_previous_mtime]} {set vmcp_log_previous_mtime 0}; "
        "if {![info exists vmcp_log_current_size]} {set vmcp_log_current_size 0}; "
        "if {![info exists vmcp_log_current_mtime]} {set vmcp_log_current_mtime 0}; "
        "if {![info exists vmcp_log_span_start]} {set vmcp_log_span_start 0}; "
        "if {![info exists vmcp_log_span_end]} {set vmcp_log_span_end 0}; "
        "if {![info exists vmcp_log_span_reset_detected]} {set vmcp_log_span_reset_detected 0}; "
        "if {![info exists vmcp_project_dir_before]} {set vmcp_project_dir_before \"\"}; "
        "if {![info exists vmcp_project_dir_after]} {set vmcp_project_dir_after \"\"}; "
        "if {![info exists vmcp_project_name_before]} {set vmcp_project_name_before \"\"}; "
        "if {![info exists vmcp_project_name_after]} {set vmcp_project_name_after \"\"}; "
        "if {![info exists vmcp_simset_before]} {set vmcp_simset_before \"\"}; "
        "if {![info exists vmcp_simset_after]} {set vmcp_simset_after \"\"}; "
        "if {![info exists vmcp_sim_top_before]} {set vmcp_sim_top_before \"\"}; "
        "if {![info exists vmcp_sim_top_after]} {set vmcp_sim_top_after \"\"}; "
        "if {![info exists vmcp_source_snapshot_before]} {set vmcp_source_snapshot_before [list]}; "
        "if {![info exists vmcp_source_snapshot_after]} {set vmcp_source_snapshot_after [list]}; "
        "if {![info exists vmcp_include_dirs_before]} {set vmcp_include_dirs_before [list]}; "
        "if {![info exists vmcp_include_dirs_after]} {set vmcp_include_dirs_after [list]}; "
        "if {![info exists vmcp_verilog_defines_before]} {set vmcp_verilog_defines_before [list]}; "
        "if {![info exists vmcp_verilog_defines_after]} {set vmcp_verilog_defines_after [list]}; "
        "if {![info exists vmcp_target_simulator_before]} {set vmcp_target_simulator_before \"\"}; "
        "if {![info exists vmcp_target_simulator_after]} {set vmcp_target_simulator_after \"\"}; "
        f"{log_read}; "
        "join [list "
        "\"sim_dir=$sim_dir\" "
        "\"log_path=$log_path\" "
        "\"log_paths=[::vivado_agent_mcp_wire_list $log_candidates]\" "
        f"\"status_source={status_source}\" "
        "\"simulation_invocation_id=$vmcp_simulation_invocation_id\" "
        "\"started_at=$vmcp_simulation_started_at\" "
        "\"ended_at=$vmcp_simulation_ended_at\" "
        "\"log_previous_path=$vmcp_log_previous_path\" "
        "\"log_previous_size=$vmcp_log_previous_size\" "
        "\"log_previous_mtime=$vmcp_log_previous_mtime\" "
        "\"log_current_size=$vmcp_log_current_size\" "
        "\"log_current_mtime=$vmcp_log_current_mtime\" "
        "\"log_span_start=$vmcp_log_span_start\" "
        "\"log_span_end=$vmcp_log_span_end\" "
        "\"log_span_reset_detected=$vmcp_log_span_reset_detected\" "
        "\"log_settle_wait_ms=$vmcp_log_settle_wait_ms\" "
        "\"log_settle_terminal_detected=$vmcp_log_settle_terminal_detected\" "
        "\"project_dir_before=$vmcp_project_dir_before\" "
        "\"project_dir_after=$vmcp_project_dir_after\" "
        "\"project_name_before=$vmcp_project_name_before\" "
        "\"project_name_after=$vmcp_project_name_after\" "
        "\"simset_before=$vmcp_simset_before\" "
        "\"simset_after=$vmcp_simset_after\" "
        "\"sim_top_before=$vmcp_sim_top_before\" "
        "\"sim_top_after=$vmcp_sim_top_after\" "
        "\"source_snapshot_before=[::vivado_agent_mcp_wire_list $vmcp_source_snapshot_before]\" "
        "\"source_snapshot_after=[::vivado_agent_mcp_wire_list $vmcp_source_snapshot_after]\" "
        "\"include_dirs_before=[::vivado_agent_mcp_wire_list $vmcp_include_dirs_before]\" "
        "\"include_dirs_after=[::vivado_agent_mcp_wire_list $vmcp_include_dirs_after]\" "
        "\"verilog_defines_before=[::vivado_agent_mcp_wire_list $vmcp_verilog_defines_before]\" "
        "\"verilog_defines_after=[::vivado_agent_mcp_wire_list $vmcp_verilog_defines_after]\" "
        "\"target_simulator_before=$vmcp_target_simulator_before\" "
        "\"target_simulator_after=$vmcp_target_simulator_after\" "
        "\"simulation_source_identity_sha256=$vmcp_simulation_source_identity_sha256\" "
        "\"simulation_stable_input_identity_sha256=$vmcp_simulation_stable_input_identity_sha256\" "
        "\"session_generation_id=$vmcp_session_generation_id\" "
        "\"wdb_files=[::vivado_agent_mcp_wire_list $wdb_rows]\" "
        "\"vcd_files=[::vivado_agent_mcp_wire_list $vcd_rows]\" "
        "\"wdb_paths=[::vivado_agent_mcp_wire_list $wdb_files]\" "
        "\"vcd_paths=[::vivado_agent_mcp_wire_list $vcd_files]\" "
        "\"wdb_total_bytes=$wdb_total_bytes\" "
        "\"vcd_total_bytes=$vcd_total_bytes\" "
        "\"waveform_total_bytes=[expr {$vcd_total_bytes + $wdb_total_bytes}]\" "
        "\"vcd_largest_file=$vcd_largest_file\" "
        "\"vcd_largest_bytes=$vcd_largest_bytes\" "
        "\"vcd_conflict=$vmcp_vcd_conflict\" "
        "\"vcd_conflict_severity=$vmcp_vcd_conflict_severity\" "
        "\"vcd_export_mode=$vmcp_vcd_export_mode\" "
        "\"mcp_vcd_export_mode=$vmcp_mcp_vcd_export_mode\" "
        "\"export_vcd_requested=$vmcp_export_vcd_requested\" "
        "\"vcd_limit_bytes=$vmcp_vcd_limit_bytes\" "
        "\"vcd_limit_exceeded=$vmcp_vcd_limit_exceeded\" "
        "\"vcd_limit_stopped=$vmcp_vcd_limit_stopped\" "
        "\"run_tcl_failed=$vmcp_run_tcl_failed\" "
        "\"run_error=[::vivado_agent_mcp_wire_list [list $vmcp_run_error]]\" "
        "\"breakpoints_cleared=$vmcp_breakpoints_cleared\" "
        "\"testbench_vcd_usage=$vmcp_testbench_vcd_usage\" "
        "\"testbench_vcd_detected=$vmcp_testbench_vcd_detected\" "
        f"\"log_begin={LOG_MARKER}\" "
        "$content"
        "] \"\\n\""
    )


def parse_simulation_vcd_preflight(raw: str) -> dict[str, Any]:
    metadata = _parse_key_value_lines(raw)
    return {
        "ok": True,
        "project_dir": metadata.get("project_dir", ""),
        "project_name": metadata.get("project_name", ""),
        "project_path": metadata.get("project_path", ""),
        "vivado_version_short": metadata.get("vivado_version_short", ""),
        "vivado_version_full": metadata.get("vivado_version_full", ""),
        "project_properties": _split_path_list(metadata.get("project_properties", "")),
        "simset_properties": _split_path_list(metadata.get("simset_properties", "")),
        "simset": metadata.get("simset", ""),
        "sim_top": metadata.get("sim_top", ""),
        "sim_files": _split_path_list(metadata.get("sim_files", "")),
        "sim_file_metadata": [
            decode_wire_row(item)
            for item in _split_path_list(metadata.get("sim_file_metadata", ""))
        ],
        "include_dirs": _split_path_list(metadata.get("include_dirs", "")),
        "verilog_defines": _split_path_list(metadata.get("verilog_defines", "")),
        "target_simulator": metadata.get("target_simulator", ""),
        "simulator_property_schema_version": metadata.get("simulator_property_schema_version", ""),
        "simulator_options": _split_path_list(metadata.get("simulator_options", "")),
        "sim_dir": metadata.get("sim_dir", ""),
        "testbench_vcd_usage": _truthy(metadata.get("testbench_vcd_usage", "")),
        "testbench_vcd_sources": _split_path_list(metadata.get("testbench_vcd_sources", "")),
        "preflight_errors": _split_path_list(metadata.get("preflight_errors", "")),
        "raw": raw,
    }


def build_simulation_source_identity(preflight: dict[str, Any]) -> dict[str, Any]:
    issues = [str(item) for item in preflight.get("preflight_errors", []) if str(item)]
    closure = _resolve_simulation_include_closure(
        preflight.get("sim_files", []),
        include_dirs=preflight.get("include_dirs", []),
        project_dir=str(preflight.get("project_dir", "")),
    )
    issues.extend(closure["issues"])
    host_inputs = _snapshot_simulation_host_inputs(preflight.get("host_input_files", []))
    issues.extend(host_inputs["issues"])
    files_by_path = {
        str(item["path"]): item
        for item in [*closure["files"], *host_inputs["files"]]
    }
    files = [files_by_path[path] for path in sorted(files_by_path)]
    identity_payload = {
        "project_dir": str(preflight.get("project_dir", "")),
        "project_name": str(preflight.get("project_name", "")),
        "project_path": str(preflight.get("project_path", "")),
        "vivado_version_short": str(preflight.get("vivado_version_short", "")),
        "vivado_version_full": str(preflight.get("vivado_version_full", "")),
        "project_properties": list(preflight.get("project_properties", [])),
        "simset_properties": list(preflight.get("simset_properties", [])),
        "sim_file_metadata": sorted(
            [dict(item) for item in preflight.get("sim_file_metadata", []) if isinstance(item, dict)],
            key=lambda item: str(item.get("path", "")),
        ),
        "simset": str(preflight.get("simset", "")),
        "sim_top": str(preflight.get("sim_top", "")),
        "include_dirs": sorted(str(item) for item in preflight.get("include_dirs", []) if str(item)),
        "verilog_defines": sorted(str(item) for item in preflight.get("verilog_defines", []) if str(item)),
        "target_simulator": str(preflight.get("target_simulator", "")),
        "simulator_property_schema_version": str(preflight.get("simulator_property_schema_version", "")),
        "simulator_options": sorted(str(item) for item in preflight.get("simulator_options", []) if str(item)),
        "files": files,
        "include_files": closure["include_files"],
        "host_input_files": host_inputs["paths"],
    }
    required_identity = all(
        identity_payload[key]
        for key in ("project_dir", "project_name", "simset", "sim_top")
    ) and bool(files)
    if not required_identity:
        issues.append("simulation project/simset/top or compile-order source identity is incomplete")
    canonical = json.dumps(identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "status": "READY" if not issues else "BLOCK",
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "identity": identity_payload,
        "issues": _unique_strings(issues),
    }


def build_design_execution_identity(preflight: dict[str, Any]) -> dict[str, Any]:
    issues = [
        str(item.get("error", item)) if isinstance(item, dict) else str(item)
        for item in preflight.get("discovery_errors", [])
        if item
    ]
    if preflight.get("wire_format_complete") is not True:
        issues.append("design execution input discovery response is structurally incomplete")
    composite_inputs = [dict(item) for item in preflight.get("composite_inputs", []) if isinstance(item, dict)]
    if composite_inputs:
        issues.append("design execution identity does not accept IP/BD/XCI/DCP/custom-repository/OOC inputs")
    source_rows = sorted(
        [dict(item) for item in preflight.get("sources", []) if isinstance(item, dict)],
        key=lambda item: (str(item.get("used_in", "")), _identity_order(item.get("order")), str(item.get("path", ""))),
    )
    source_paths = sorted({str(item.get("path", "")) for item in source_rows if str(item.get("path", ""))})
    verilog_source_paths = [
        path
        for path in source_paths
        if Path(path).suffix.lower() in _TRUSTED_VERILOG_SUFFIXES
    ]
    closure = _resolve_simulation_include_closure(
        verilog_source_paths,
        include_dirs=preflight.get("include_dirs", []),
        project_dir=str(preflight.get("project_dir", "")),
        strict_static_attestation=False,
    )
    issues.extend(closure["issues"])
    source_files = list(closure["files"])
    captured_source_paths = {str(item.get("path", "")) for item in source_files}
    for path_text in source_paths:
        suffix = Path(path_text).suffix.lower()
        if suffix not in _TRUSTED_DESIGN_SOURCE_SUFFIXES:
            issues.append(f"design source language is not accepted by the pure RTL execution closure: {path_text}")
            continue
        resolved = str(_absolute_path(Path(path_text)))
        if resolved in captured_source_paths:
            continue
        snapshot, error = _snapshot_design_identity_file(
            path_text,
            source_kind="compile_source",
            required_suffix="",
        )
        if error:
            issues.append(error)
        elif snapshot:
            source_files.append(snapshot)
    constraint_rows = sorted(
        [dict(item) for item in preflight.get("constraints", []) if isinstance(item, dict)],
        key=lambda item: (str(item.get("used_in", "")), _identity_order(item.get("order")), str(item.get("path", ""))),
    )
    constraint_files: list[dict[str, Any]] = []
    for path_text in sorted({str(item.get("path", "")) for item in constraint_rows if str(item.get("path", ""))}):
        snapshot, error = _snapshot_design_identity_file(path_text, source_kind="constraint", required_suffix=".xdc")
        if error:
            issues.append(error)
        elif snapshot:
            constraint_files.append(snapshot)
    files_by_path = {
        str(item["path"]): item
        for item in [*source_files, *constraint_files]
    }
    files = [files_by_path[path] for path in sorted(files_by_path)]
    identity_payload = {
        "schema_version": 1,
        "project_dir": str(preflight.get("project_dir", "")),
        "project_name": str(preflight.get("project_name", "")),
        "project_path": str(preflight.get("project_path", "")),
        "part": str(preflight.get("part", "")),
        "top": str(preflight.get("top", "")),
        "vivado_version_short": str(preflight.get("vivado_version_short", "")),
        "vivado_version_full": str(preflight.get("vivado_version_full", "")),
        "include_dirs": sorted(str(item) for item in preflight.get("include_dirs", []) if str(item)),
        "verilog_defines": sorted(str(item) for item in preflight.get("verilog_defines", []) if str(item)),
        "source_compile_order": source_rows,
        "constraint_compile_order": constraint_rows,
        "run_configurations": sorted(
            [dict(item) for item in preflight.get("run_configurations", []) if isinstance(item, dict)],
            key=lambda item: (str(item.get("run", "")), str(item.get("property", ""))),
        ),
        "composite_inputs": composite_inputs,
        "files": files,
        "include_files": closure["include_files"],
    }
    if not all(identity_payload[key] for key in ("project_dir", "project_name", "project_path", "part", "top")):
        issues.append("design project path/name/part/top identity is incomplete")
    if not source_rows or not source_paths or not files:
        issues.append("design source compile-order identity is empty")
    canonical = json.dumps(identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "status": "READY" if not issues else "BLOCK",
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "identity": identity_payload,
        "issues": _unique_strings(issues),
    }


def verify_design_execution_identity_files(design_identity: dict[str, Any]) -> list[str]:
    payload = design_identity.get("identity") if isinstance(design_identity.get("identity"), dict) else {}
    issues: list[str] = []
    for expected in payload.get("files", []):
        if not isinstance(expected, dict):
            issues.append("design execution identity contains a non-object file entry")
            continue
        current, error = _snapshot_design_identity_file(
            str(expected.get("path", "")),
            source_kind=str(expected.get("source_kind", "")),
            required_suffix=".xdc" if str(expected.get("source_kind", "")) == "constraint" else "",
        )
        if error:
            issues.append(error)
        elif current != expected:
            issues.append(f"design execution input changed after identity capture: {expected.get('path', '')}")
    return _unique_strings(issues)


def build_simulation_stable_input_identity(
    preflight: dict[str, Any],
    source_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    full = source_identity or build_simulation_source_identity(preflight)
    identity = full.get("identity") if isinstance(full.get("identity"), dict) else {}
    project_path = str(identity.get("project_path", ""))
    project_key = os.path.normcase(str(Path(project_path).resolve())) if project_path else ""

    def is_project_file(item: dict[str, Any]) -> bool:
        raw_path = str(item.get("path", ""))
        return bool(project_key and raw_path and os.path.normcase(str(Path(raw_path).resolve())) == project_key)

    stable_files = [dict(item) for item in identity.get("files", []) if isinstance(item, dict) and not is_project_file(item)]
    stable_host_inputs = [
        str(path)
        for path in identity.get("host_input_files", [])
        if not project_key or os.path.normcase(str(Path(str(path)).resolve())) != project_key
    ]
    payload = {
        "project_dir": str(identity.get("project_dir", "")),
        "project_name": str(identity.get("project_name", "")),
        "project_path": project_path,
        "vivado_version_short": str(identity.get("vivado_version_short", "")),
        "vivado_version_full": str(identity.get("vivado_version_full", "")),
        "project_properties": list(identity.get("project_properties", [])),
        "simset_properties": list(identity.get("simset_properties", [])),
        "sim_file_metadata": list(identity.get("sim_file_metadata", [])),
        "simset": str(identity.get("simset", "")),
        "sim_top": str(identity.get("sim_top", "")),
        "include_dirs": list(identity.get("include_dirs", [])),
        "verilog_defines": list(identity.get("verilog_defines", [])),
        "target_simulator": str(identity.get("target_simulator", "")),
        "simulator_property_schema_version": str(identity.get("simulator_property_schema_version", "")),
        "simulator_options": list(identity.get("simulator_options", [])),
        "files": stable_files,
        "include_files": list(identity.get("include_files", [])),
        "host_input_files": stable_host_inputs,
    }
    issues = [str(item) for item in full.get("issues", []) if str(item)]
    if not stable_files:
        issues.append("simulation stable input identity contains no executable source or host-input files")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "status": "READY" if full.get("status") == "READY" and not issues else "BLOCK",
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "identity": payload,
        "issues": _unique_strings(issues),
        "project_file_excluded_from_freshness": bool(project_key),
    }


def validate_simulation_trust_closure(
    preflight: dict[str, Any],
    source_identity: dict[str, Any],
    *,
    trusted_roots: list[str],
) -> dict[str, Any]:
    issues = [str(item) for item in source_identity.get("issues", []) if str(item)]
    if source_identity.get("status") != "READY":
        issues.append("simulation source/include closure is incomplete")
    target_simulator = str(preflight.get("target_simulator", "")).strip().lower()
    if target_simulator not in {"vivado simulator", "xsim"}:
        issues.append(f"target_simulator must be Vivado Simulator/XSim, got {preflight.get('target_simulator', '')!r}")
    if str(preflight.get("vivado_version_short", "")).strip() != "2021.2":
        issues.append(
            f"trusted XSIM execution requires Vivado 2021.2, got {preflight.get('vivado_version_short', '')!r}"
        )
    project_path = str(preflight.get("project_path", "")).strip()
    if not project_path or Path(project_path).suffix.lower() != ".xpr":
        issues.append("trusted XSIM execution requires the current .xpr path in the attested source identity")
    if not preflight.get("project_properties"):
        issues.append("trusted XSIM execution requires attested project property metadata")
    if not preflight.get("simset_properties"):
        issues.append("trusted XSIM execution requires attested simulation fileset property metadata")
    if not preflight.get("sim_file_metadata"):
        issues.append("trusted XSIM execution requires per-file compile metadata")
    simulator_options = [str(item) for item in preflight.get("simulator_options", []) if str(item).strip()]
    property_schema = str(preflight.get("simulator_property_schema_version", "")).strip()
    if property_schema != XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION:
        issues.append(f"unsupported XSIM executable-property schema: {property_schema!r}")
    if simulator_options:
        issues.append("non-empty executable XSIM Tcl/custom/more_options properties are not allowed in trusted simulation mode")
    semantic_properties = "\n".join(
        str(item)
        for item in [
            *list(preflight.get("project_properties", [])),
            *list(preflight.get("simset_properties", [])),
        ]
    )
    selected_models = sorted(set(_UNTRUSTED_SIMULATION_MODEL_RE.findall(semantic_properties)))
    if selected_models:
        issues.append("trusted XSIM execution only accepts RTL simulation models; TLM/DPI/SystemC model selection is blocked")
    try:
        _validated_existing_defines(preflight.get("verilog_defines", []))
    except ValueError as exc:
        issues.append(str(exc))

    closure_files = [
        str(item.get("path", ""))
        for item in source_identity.get("identity", {}).get("files", [])
        if isinstance(item, dict) and item.get("path")
    ]
    include_dirs = [str(item) for item in preflight.get("include_dirs", []) if str(item)]
    foreign_inputs = sorted(
        path for path in closure_files if Path(path).suffix.lower() in _FOREIGN_EXECUTABLE_SUFFIXES
    )
    if foreign_inputs:
        issues.append(f"foreign/DPI executable inputs are not allowed: {', '.join(foreign_inputs)}")

    accepted_root = ""
    for root in trusted_roots:
        try:
            for path in [str(preflight.get("project_dir", "")), *closure_files, *include_dirs]:
                if not path:
                    raise ManagedPathError("trusted simulation closure contains an empty path")
                validate_managed_path(root, path)
        except (ManagedPathError, OSError):
            continue
        accepted_root = str(Path(root).resolve())
        break
    if not accepted_root:
        issues.append("project, compile sources, recursive includes, or include directories escape every trusted root")
    return {
        "status": "READY" if not issues else "BLOCK",
        "accepted_root": accepted_root,
        "issues": _unique_strings(issues),
        "closure_files": sorted(closure_files),
        "closure_directories": sorted(
            {
                *(str(Path(path).resolve().parent) for path in closure_files),
                *(str(Path(path).resolve()) for path in include_dirs),
            }
        ),
        "source_count": len(closure_files),
        "include_dir_count": len(include_dirs),
        "simulator_options": simulator_options,
        "simulation_model_policy": "rtl_only",
        "untrusted_simulation_models_detected": bool(selected_models),
        "simulator_property_schema_version": property_schema or XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION,
        "foreign_inputs": foreign_inputs,
        "host_input_count": sum(
            1
            for item in source_identity.get("identity", {}).get("files", [])
            if isinstance(item, dict) and item.get("source_kind") == "host_input"
        ),
    }


def _validated_existing_defines(raw_defines: Any) -> dict[str, str | None]:
    parsed: dict[str, str | None] = {}
    for raw in raw_defines or []:
        text = str(raw)
        name, separator, value = text.partition("=")
        parsed[name] = value if separator else None
    return validate_simulation_defines(parsed)


def parse_simulation_result(raw: str) -> dict[str, Any]:
    metadata_text, log_text = _split_metadata_and_log(raw)
    metadata = _parse_key_value_lines(metadata_text)
    messages = parse_messages(log_text)
    vcd_limit_exceeded = _truthy(metadata.get("vcd_limit_exceeded", ""))
    status = "failed" if vcd_limit_exceeded else _classify_simulation_status(log_text, messages["counts"])
    vcd_files = _parse_file_rows(metadata.get("vcd_files", ""), fallback_paths=metadata.get("vcd_paths", ""))
    wdb_files = _parse_file_rows(metadata.get("wdb_files", ""), fallback_paths=metadata.get("wdb_paths", ""))
    mcp_vcd_export_mode = metadata.get("mcp_vcd_export_mode") or metadata.get("vcd_export_mode", "disabled")
    testbench_vcd_usage = _truthy(metadata.get("testbench_vcd_usage", "")) or (mcp_vcd_export_mode == "testbench_existing" and bool(vcd_files))
    testbench_vcd_detected = _truthy(metadata.get("testbench_vcd_detected", "")) or testbench_vcd_usage
    vcd_conflict = _truthy(metadata.get("vcd_conflict", ""))
    vcd_conflict_severity = _vcd_conflict_severity(
        metadata.get("vcd_conflict_severity", ""),
        log_text=log_text,
        status=status,
        vcd_conflict=vcd_conflict,
        mcp_vcd_export_mode=mcp_vcd_export_mode,
        testbench_vcd_detected=testbench_vcd_detected,
    )
    largest_vcd = {
        "path": metadata.get("vcd_largest_file", ""),
        "size_bytes": _int_or_none(metadata.get("vcd_largest_bytes", "")) or 0,
    }
    diagnosis = _diagnose_simulation(
        status=status,
        log_text=log_text,
        counts=messages["counts"],
        vcd_limit_exceeded=vcd_limit_exceeded,
        vcd_conflict=vcd_conflict,
        vcd_conflict_severity=vcd_conflict_severity,
        mcp_vcd_export_mode=mcp_vcd_export_mode,
        testbench_vcd_detected=testbench_vcd_detected,
    )
    run_tcl_failed = _truthy(metadata.get("run_tcl_failed", ""))
    encoded_run_errors = _split_path_list(metadata.get("run_error", ""))
    run_error = _excerpt(encoded_run_errors[0] if encoded_run_errors else "", limit=4096)
    breakpoints_cleared = _truthy(metadata.get("breakpoints_cleared", ""))
    if run_tcl_failed:
        diagnosis["warnings"].append(
            {
                "code": "SIMULATION_RUN_TCL_ERROR",
                "severity": "WARNING",
                "message": (
                    "Vivado reported a Tcl run error; final simulation status was derived from the current invocation log."
                ),
            }
        )
    evidence_freshness = _simulation_evidence_freshness(metadata)
    return {
        "ok": True,
        "status": status,
        "status_source": metadata.get("status_source", "latest_log_tail"),
        "simulation_invocation_id": metadata.get("simulation_invocation_id", ""),
        "simulation_source_identity_sha256": metadata.get("simulation_source_identity_sha256", ""),
        "simulation_stable_input_identity_sha256": metadata.get("simulation_stable_input_identity_sha256", ""),
        "session_generation_id": metadata.get("session_generation_id", ""),
        "started_at": metadata.get("started_at", ""),
        "ended_at": metadata.get("ended_at", ""),
        "simulation_context": {
            "project_dir": metadata.get("project_dir_before", ""),
            "project_name": metadata.get("project_name_before", ""),
            "simset": metadata.get("simset_before", ""),
            "sim_top": metadata.get("sim_top_before", ""),
            "source_snapshot_before": _split_path_list(metadata.get("source_snapshot_before", "")),
            "source_snapshot_after": _split_path_list(metadata.get("source_snapshot_after", "")),
            "include_dirs_before": _split_path_list(metadata.get("include_dirs_before", "")),
            "include_dirs_after": _split_path_list(metadata.get("include_dirs_after", "")),
            "verilog_defines_before": _split_path_list(metadata.get("verilog_defines_before", "")),
            "verilog_defines_after": _split_path_list(metadata.get("verilog_defines_after", "")),
            "target_simulator_before": metadata.get("target_simulator_before", ""),
            "target_simulator_after": metadata.get("target_simulator_after", ""),
        },
        "evidence_freshness": evidence_freshness,
        "log_span": {
            "previous_path": metadata.get("log_previous_path", ""),
            "previous_size": _int_or_none(metadata.get("log_previous_size", "")) or 0,
            "previous_mtime": _int_or_none(metadata.get("log_previous_mtime", "")) or 0,
            "current_size": _int_or_none(metadata.get("log_current_size", "")) or 0,
            "current_mtime": _int_or_none(metadata.get("log_current_mtime", "")) or 0,
            "start": _int_or_none(metadata.get("log_span_start", "")) or 0,
            "end": _int_or_none(metadata.get("log_span_end", "")) or 0,
            "reset_detected": _truthy(metadata.get("log_span_reset_detected", "")),
            "settle_wait_ms": _int_or_none(metadata.get("log_settle_wait_ms", "")) or 0,
            "settle_terminal_detected": _truthy(metadata.get("log_settle_terminal_detected", "")),
        },
        "counts": messages["counts"],
        "messages": messages["messages"],
        "diagnosis": diagnosis,
        "simulation_diagnosis": diagnosis,
        "vcd_conflict": vcd_conflict,
        "vcd_conflict_severity": vcd_conflict_severity,
        "vcd_export_mode": mcp_vcd_export_mode,
        "mcp_vcd_export_mode": mcp_vcd_export_mode,
        "export_vcd_requested": _truthy(metadata.get("export_vcd_requested", "")),
        "vcd_limit_bytes": _int_or_none(metadata.get("vcd_limit_bytes", "")) or 0,
        "vcd_limit_exceeded": vcd_limit_exceeded,
        "vcd_limit_stopped": _truthy(metadata.get("vcd_limit_stopped", "")),
        "run_tcl_failed": run_tcl_failed,
        "run_error": run_error,
        "breakpoints_cleared": breakpoints_cleared,
        "testbench_vcd_usage": testbench_vcd_usage,
        "testbench_vcd_detected": testbench_vcd_detected,
        "artifacts": {
            "sim_dir": metadata.get("sim_dir", ""),
            "log_path": metadata.get("log_path", ""),
            "log_paths": _split_path_list(metadata.get("log_paths", "")),
            "wdb_files": wdb_files,
            "vcd_files": vcd_files,
            "wdb_paths": [item["path"] for item in wdb_files],
            "vcd_paths": [item["path"] for item in vcd_files],
            "wdb_total_bytes": _int_or_none(metadata.get("wdb_total_bytes", "")) or _sum_file_sizes(wdb_files),
            "vcd_total_bytes": _int_or_none(metadata.get("vcd_total_bytes", "")) or _sum_file_sizes(vcd_files),
            "waveform_total_bytes": _int_or_none(metadata.get("waveform_total_bytes", ""))
            or (_sum_file_sizes(vcd_files) + _sum_file_sizes(wdb_files)),
            "largest_vcd_file": largest_vcd,
        },
        "log_excerpt": _excerpt(log_text),
        "raw": raw,
    }


def _sim_artifact_setup_command(simset: str) -> str:
    simset_ref = tcl_list_quote(simset)
    return (
        f"{tcl_wire_prelude()}; "
        "set p [current_project]; "
        "set project_dir [get_property DIRECTORY $p]; "
        "set project_name [get_property NAME $p]; "
        f"set simset {simset_ref}; "
        "set sim_dir [file normalize [file join $project_dir ${project_name}.sim $simset behav xsim]]"
    )


def analyze_testbench_waveform_paths(preflight: dict[str, Any]) -> dict[str, Any]:
    project_text = str(preflight.get("project_dir", "")).strip()
    sim_dir_text = str(preflight.get("sim_dir", "")).strip()
    source_paths = {
        str(path)
        for key in ("sim_files", "testbench_vcd_sources")
        for path in preflight.get(key, [])
        if str(path)
    }
    preflight_errors = [str(item) for item in preflight.get("preflight_errors", []) if str(item)]
    if not project_text or not sim_dir_text:
        return {
            "status": "UNATTESTED",
            "monitored_paths": [],
            "uncontrolled_reasons": preflight_errors,
            "reason": "preflight did not expose project_dir and sim_dir",
        }
    project_dir = Path(project_text).resolve()
    sim_dir = Path(sim_dir_text).resolve()
    closure = _resolve_simulation_include_closure(
        source_paths,
        include_dirs=preflight.get("include_dirs", []),
        project_dir=project_text,
    )
    monitored: list[str] = []
    host_input_files: list[str] = []
    uncontrolled: list[str] = [*preflight_errors, *closure["issues"]]
    try:
        sim_dir.relative_to(project_dir)
    except ValueError:
        uncontrolled.append(f"simulation directory escapes current project: {sim_dir}")
    for source_text, content in closure["contents"].items():
        source = Path(source_text)
        scan_text = _strip_verilog_comments(content)
        token_text = _mask_verilog_strings(scan_text)
        if re.search(r'(?i)\b(?:import|export)\s+"DPI(?:-C)?"', scan_text):
            uncontrolled.append(f"DPI code can create unmonitored outputs: {source}")
        if re.search(r"(?i)\$system\s*\(", token_text):
            uncontrolled.append(f"$system can create unmonitored outputs: {source}")
        if "``" in token_text:
            uncontrolled.append(f"preprocessor token-pasting can hide host side effects: {source}")
        system_identifiers = {
            match.group(1).lower()
            for match in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_$]*)", token_text)
        }
        unsupported_system_identifiers = sorted(system_identifiers - _TRUSTED_SYSTEM_IDENTIFIERS)
        if unsupported_system_identifiers:
            uncontrolled.append(
                f"unsupported system task/function cannot be statically attested in trusted mode: {source}: "
                + ", ".join(f"${name}" for name in unsupported_system_identifiers)
            )
        occurrences = len(re.findall(r"\$dumpfile\s*\(", scan_text, flags=re.IGNORECASE))
        literals = re.findall(r"\$dumpfile\s*\(\s*[\"']([^\"']+)[\"']", scan_text, flags=re.IGNORECASE)
        if occurrences > len(literals):
            uncontrolled.append(f"dynamic or non-literal $dumpfile path in {source}")
        for declared in literals:
            _add_controlled_simulation_output(
                declared,
                sim_dir=sim_dir,
                source=source,
                output_kind="testbench VCD",
                monitored=monitored,
                uncontrolled=uncontrolled,
            )
        if "$dumpvars" in scan_text and not literals and occurrences == 0:
            monitored.append(str((sim_dir / "dump.vcd").resolve()))
        fopen_occurrences = len(re.findall(r"(?i)\$fopen\s*\(", scan_text))
        fopen_matches = re.findall(
            r"(?i)([A-Za-z_][A-Za-z0-9_$]*)\s*=\s*\$fopen\s*\(\s*[\"']([^\"']+)[\"']\s*(?:,\s*[\"']([^\"']+)[\"']\s*)?\)",
            scan_text,
        )
        if fopen_occurrences > len(fopen_matches):
            uncontrolled.append(f"dynamic, unassigned, or non-literal $fopen path in {source}")
        controlled_output_handles: set[str] = set()
        for handle, declared, raw_mode in fopen_matches:
            mode = (raw_mode or "w").strip().lower()
            if "+" in mode:
                uncontrolled.append(f"read/write $fopen mode cannot be held immutable in {source}: {mode}")
            elif mode in {"r", "rb"}:
                _add_attested_simulation_input(
                    declared,
                    sim_dir=sim_dir,
                    source=source,
                    input_kind="$fopen",
                    host_inputs=host_input_files,
                    uncontrolled=uncontrolled,
                )
            elif mode in {"w", "wb", "a", "ab"}:
                controlled_output_handles.add(handle)
                _add_controlled_simulation_output(
                    declared,
                    sim_dir=sim_dir,
                    source=source,
                    output_kind="$fopen",
                    monitored=monitored,
                    uncontrolled=uncontrolled,
                )
            else:
                uncontrolled.append(f"unsupported $fopen mode in {source}: {mode or '<empty>'}")
        for handle in re.findall(r"(?i)\$(?:fwrite|fdisplay|fstrobe|fmonitor)\s*\(\s*([A-Za-z_][A-Za-z0-9_$]*)", scan_text):
            if handle not in controlled_output_handles:
                uncontrolled.append(f"file output uses an unattested handle {handle} in {source}")
        for system_task in ("writememh", "writememb"):
            task_occurrences = len(re.findall(rf"(?i)\${system_task}\s*\(", scan_text))
            task_literals = re.findall(rf"(?i)\${system_task}\s*\(\s*[\"']([^\"']+)[\"']", scan_text)
            if task_occurrences > len(task_literals):
                uncontrolled.append(f"dynamic or non-literal ${system_task} path in {source}")
            for declared in task_literals:
                _add_controlled_simulation_output(
                    declared,
                    sim_dir=sim_dir,
                    source=source,
                    output_kind=f"${system_task}",
                    monitored=monitored,
                    uncontrolled=uncontrolled,
                )
        for system_task in ("readmemh", "readmemb", "sdf_annotate"):
            task_occurrences = len(re.findall(rf"(?i)\${system_task}\s*\(", scan_text))
            task_literals = re.findall(rf"(?i)\${system_task}\s*\(\s*[\"']([^\"']+)[\"']", scan_text)
            if task_occurrences > len(task_literals):
                uncontrolled.append(f"dynamic or non-literal ${system_task} path in {source}")
            for declared in task_literals:
                _add_attested_simulation_input(
                    declared,
                    sim_dir=sim_dir,
                    source=source,
                    input_kind=f"${system_task}",
                    host_inputs=host_input_files,
                    uncontrolled=uncontrolled,
                )
    return {
        "status": "BLOCK" if uncontrolled else "READY",
        "monitored_paths": sorted(set(monitored)),
        "scanned_source_paths": sorted(closure["contents"]),
        "include_files": closure["include_files"],
        "host_input_files": sorted(set(host_input_files)),
        "uncontrolled_reasons": _unique_strings(uncontrolled),
    }


def _snapshot_simulation_host_inputs(raw_paths: Any) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    issues: list[str] = []
    paths: list[str] = []
    for raw_path in sorted({str(item) for item in raw_paths or [] if str(item)}):
        path = _absolute_path(Path(raw_path))
        path_text = str(path)
        paths.append(path_text)
        try:
            if _path_contains_reparse_component(path):
                issues.append(f"simulation host input contains a symlink, junction, or reparse point: {path}")
                continue
            stat_result = path.stat()
            if not path.is_file():
                issues.append(f"simulation host input is not a regular file: {path}")
                continue
            if stat_result.st_nlink != 1:
                issues.append(f"simulation host input must not have multiple hard links: {path}")
                continue
            if stat_result.st_size > MAX_IDENTITY_SOURCE_BYTES:
                issues.append(f"simulation host input exceeds identity hash limit: {path}")
                continue
            raw = path.read_bytes()
        except OSError as exc:
            issues.append(f"could not read simulation host input {path}: {exc}")
            continue
        files.append(
            {
                "path": path_text,
                "size": len(raw),
                "mtime_ns": stat_result.st_mtime_ns,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "object_identity": [stat_result.st_dev, stat_result.st_ino, stat_result.st_mode],
                "nlink": stat_result.st_nlink,
                "source_kind": "host_input",
            }
        )
    return {"files": files, "paths": paths, "issues": issues}


def _resolve_simulation_include_closure(
    raw_roots: Any,
    *,
    include_dirs: Any,
    project_dir: str,
    strict_static_attestation: bool = True,
) -> dict[str, Any]:
    base_dir = _absolute_path(Path(project_dir)) if project_dir else None
    roots = {
        _resolve_source_path(str(raw_path), base_dir)
        for raw_path in raw_roots or []
        if str(raw_path)
    }
    resolved_include_dirs = _resolve_include_dirs(include_dirs, base_dir)
    files: dict[str, dict[str, Any]] = {}
    contents: dict[str, str] = {}
    include_files: set[str] = set()
    issues: list[str] = []
    visited: set[Path] = set()
    active: list[Path] = []
    attempted: set[Path] = set()

    def visit(path: Path, *, included: bool) -> None:
        path = _absolute_path(path)
        if path in active:
            start = active.index(path)
            chain = " -> ".join(str(item) for item in [*active[start:], path])
            issues.append(f"cyclic simulation `include detected: {chain}")
            return
        if path in visited:
            if included and path not in roots:
                include_files.add(str(path))
            return
        attempted.add(path)
        if len(attempted) > MAX_INCLUDE_CLOSURE_FILES:
            issues.append(f"simulation `include closure exceeds {MAX_INCLUDE_CLOSURE_FILES} files")
            return
        if len(active) >= MAX_INCLUDE_CLOSURE_DEPTH:
            issues.append(f"simulation `include closure exceeds depth {MAX_INCLUDE_CLOSURE_DEPTH} at {path}")
            return
        if not path.is_file():
            issues.append(f"simulation source is missing: {path}")
            return
        try:
            if _path_contains_reparse_component(path):
                issues.append(f"simulation source contains a symlink, junction, or reparse point: {path}")
                return
            stat = path.stat()
            if stat.st_nlink != 1:
                issues.append(f"simulation source must not have multiple hard links: {path}")
                return
            if stat.st_size > MAX_IDENTITY_SOURCE_BYTES:
                issues.append(f"simulation source exceeds identity hash limit: {path}")
                return
            raw = path.read_bytes()
        except OSError as exc:
            issues.append(f"could not read simulation source {path}: {exc}")
            return
        path_text = str(path)
        files[path_text] = {
            "path": path_text,
            "size": len(raw),
            "mtime_ns": stat.st_mtime_ns,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "object_identity": [stat.st_dev, stat.st_ino, stat.st_mode],
            "nlink": stat.st_nlink,
            "source_kind": "include" if included and path not in roots else "compile_source",
        }
        if path.suffix.lower() not in _TRUSTED_VERILOG_SUFFIXES:
            issues.append(
                f"simulation source language cannot be statically attested in trusted mode: {path}"
            )
            visited.add(path)
            return
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            issues.append(f"simulation source is not valid UTF-8 and cannot be statically attested: {path}")
            visited.add(path)
            return
        contents[path_text] = content
        if included and path not in roots:
            include_files.add(path_text)

        active.append(path)
        stripped = _strip_verilog_comments(content)
        macro_scan = _mask_verilog_strings(stripped)
        macro_names = [match.group(1) for match in re.finditer(r"`([A-Za-z_][A-Za-z0-9_]*)", macro_scan)]
        if strict_static_attestation and any(name.lower() in {"define", "undef"} for name in macro_names):
            issues.append(f"user-defined preprocessor macros cannot be statically attested in trusted mode: {path}")
        unsupported_macros = sorted(
            {
                name
                for name in macro_names
                if name.lower() not in _STATICALLY_ALLOWED_PREPROCESSOR_DIRECTIVES
                and name.lower() not in {"define", "undef"}
            }
        )
        if strict_static_attestation and unsupported_macros:
            issues.append(
                f"preprocessor macro expansion cannot be statically attested in trusted mode: {path}: "
                + ", ".join(unsupported_macros)
            )
        for match in _INCLUDE_DIRECTIVE_RE.finditer(stripped):
            operand = match.group("operand").strip()
            literal = _LITERAL_INCLUDE_RE.fullmatch(operand)
            if literal is None:
                issues.append(f"dynamic or non-literal `include in {path}: {operand or '<empty>'}")
                continue
            declared = literal.group("path")
            candidates = _resolve_include_candidates(declared, source=path, include_dirs=resolved_include_dirs)
            if not candidates:
                issues.append(f"unresolved simulation `include {declared!r} in {path}")
                continue
            if len(candidates) > 1:
                joined = ", ".join(str(candidate) for candidate in candidates)
                issues.append(f"ambiguous simulation `include {declared!r} in {path}: {joined}")
                continue
            if not Path(declared).is_absolute():
                source_relative = _absolute_path(path.parent / declared)
                if strict_static_attestation and candidates[0] != source_relative:
                    issues.append(
                        f"trusted simulation `include {declared!r} in {path} depends on include_dirs search; "
                        "use an explicit source-relative literal path to prevent late-file shadowing"
                    )
                    continue
            visit(candidates[0], included=True)
        active.pop()
        visited.add(path)

    for root in sorted(roots, key=str):
        visit(root, included=False)
    return {
        "files": [files[path] for path in sorted(files)],
        "contents": {path: contents[path] for path in sorted(contents)},
        "include_files": sorted(include_files),
        "issues": _unique_strings(issues),
    }


def _snapshot_design_identity_file(
    raw_path: str,
    *,
    source_kind: str,
    required_suffix: str,
) -> tuple[dict[str, Any] | None, str]:
    if not raw_path:
        return None, "design execution identity contains an empty file path"
    path = _absolute_path(Path(raw_path))
    if required_suffix and path.suffix.lower() != required_suffix:
        return None, f"design execution input has unexpected suffix: {path}"
    try:
        if _path_contains_reparse_component(path):
            return None, f"design execution input contains a symlink, junction, or reparse point: {path}"
        stat_result = path.stat()
        if not path.is_file():
            return None, f"design execution input is not a regular file: {path}"
        if stat_result.st_nlink != 1:
            return None, f"design execution input must not have multiple hard links: {path}"
        if stat_result.st_size > MAX_IDENTITY_SOURCE_BYTES:
            return None, f"design execution input exceeds identity hash limit: {path}"
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"could not read design execution input {path}: {exc}"
    return {
        "path": str(path),
        "size": len(raw),
        "mtime_ns": stat_result.st_mtime_ns,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "object_identity": [stat_result.st_dev, stat_result.st_ino, stat_result.st_mode],
        "nlink": stat_result.st_nlink,
        "source_kind": source_kind,
    }, ""


def _identity_order(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 2**31 - 1


def _resolve_source_path(raw_path: str, base_dir: Path | None) -> Path:
    path = Path(raw_path)
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return _absolute_path(path)


def _resolve_include_dirs(raw_dirs: Any, base_dir: Path | None) -> list[Path]:
    result: list[Path] = []
    for raw_dir in raw_dirs or []:
        if not str(raw_dir):
            continue
        path = Path(str(raw_dir))
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        resolved = _absolute_path(path)
        if resolved not in result:
            result.append(resolved)
    return result


def _resolve_include_candidates(declared: str, *, source: Path, include_dirs: list[Path]) -> list[Path]:
    include_path = Path(declared)
    candidates = [include_path] if include_path.is_absolute() else [source.parent / include_path, *(root / include_path for root in include_dirs)]
    resolved: list[Path] = []
    for candidate in candidates:
        path = _absolute_path(candidate)
        if path.is_file() and path not in resolved:
            resolved.append(path)
    return resolved


def _strip_verilog_comments(content: str) -> str:
    result: list[str] = []
    index = 0
    state = "code"
    while index < len(content):
        char = content[index]
        next_char = content[index + 1] if index + 1 < len(content) else ""
        if state == "code":
            if char == '"':
                state = "string"
                result.append(char)
            elif char == "/" and next_char == "/":
                state = "line_comment"
                result.extend((" ", " "))
                index += 1
            elif char == "/" and next_char == "*":
                state = "block_comment"
                result.extend((" ", " "))
                index += 1
            else:
                result.append(char)
        elif state == "string":
            result.append(char)
            if char == "\\" and next_char:
                result.append(next_char)
                index += 1
            elif char == '"':
                state = "code"
        elif state == "line_comment":
            result.append(char if char in "\r\n" else " ")
            if char in "\r\n":
                state = "code"
        else:
            if char == "*" and next_char == "/":
                result.extend((" ", " "))
                index += 1
                state = "code"
            else:
                result.append(char if char in "\r\n" else " ")
        index += 1
    return "".join(result)


def _mask_verilog_strings(content: str) -> str:
    result: list[str] = []
    in_string = False
    index = 0
    while index < len(content):
        char = content[index]
        if not in_string:
            if char == '"':
                in_string = True
                result.append(" ")
            else:
                result.append(char)
        else:
            if char == "\\" and index + 1 < len(content):
                result.append(" ")
                index += 1
                result.append("\n" if content[index] == "\n" else " ")
            elif char == '"':
                in_string = False
                result.append(" ")
            else:
                result.append(char if char in "\r\n" else " ")
        index += 1
    return "".join(result)


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _add_controlled_simulation_output(
    declared: str,
    *,
    sim_dir: Path,
    source: Path,
    output_kind: str,
    monitored: list[str],
    uncontrolled: list[str],
) -> None:
    candidate = Path(declared)
    if not candidate.is_absolute():
        candidate = sim_dir / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(sim_dir)
    except ValueError:
        uncontrolled.append(f"{output_kind} path escapes controlled simulation directory in {source}: {resolved}")
        return
    monitored.append(str(resolved))


def _add_attested_simulation_input(
    declared: str,
    *,
    sim_dir: Path,
    source: Path,
    input_kind: str,
    host_inputs: list[str],
    uncontrolled: list[str],
) -> None:
    candidate = Path(declared)
    if not candidate.is_absolute():
        candidate = sim_dir / candidate
    resolved = _absolute_path(candidate)
    if _path_contains_reparse_component(resolved):
        uncontrolled.append(f"{input_kind} input contains a symlink, junction, or reparse point in {source}: {resolved}")
        return
    if not resolved.is_file():
        uncontrolled.append(f"{input_kind} input is missing or not a regular file in {source}: {resolved}")
        return
    if resolved.stat().st_nlink != 1:
        uncontrolled.append(f"{input_kind} input must not have multiple hard links in {source}: {resolved}")
        return
    host_inputs.append(str(resolved))


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_contains_reparse_component(path: Path) -> bool:
    absolute = _absolute_path(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if os.path.lexists(current) and is_reparse_point(current, os.lstat(current)):
            return True
    return False


def _simulation_context_snapshot_command(suffix: str) -> str:
    return (
        f"set vmcp_project_dir_{suffix} [get_property DIRECTORY [current_project]]; "
        f"set vmcp_project_name_{suffix} [get_property NAME [current_project]]; "
        f"set vmcp_simset_{suffix} $simset; "
        f"set vmcp_sim_top_{suffix} [get_property TOP [get_filesets $simset]]; "
        f"set vmcp_include_dirs_{suffix} [list]; catch {{set vmcp_include_dirs_{suffix} [get_property include_dirs [get_filesets $simset]]}}; "
        f"set vmcp_verilog_defines_{suffix} [list]; catch {{set vmcp_verilog_defines_{suffix} [get_property verilog_define [get_filesets $simset]]}}; "
        f"set vmcp_target_simulator_{suffix} \"\"; catch {{set vmcp_target_simulator_{suffix} [get_property target_simulator [current_project]]}}; "
        f"set vmcp_source_snapshot_{suffix} [list]; "
        "set vmcp_compile_files [list]; if {[catch {set vmcp_compile_files [get_files -quiet -compile_order sources -used_in simulation -of_objects [get_filesets $simset]]}]} {set vmcp_compile_files [get_files -quiet -of_objects [get_filesets $simset]]}; "
        "foreach vmcp_source [lsort $vmcp_compile_files] {"
        "set vmcp_source_path [file normalize $vmcp_source]; set vmcp_source_size -1; set vmcp_source_mtime -1; "
        "catch {set vmcp_source_size [file size $vmcp_source_path]}; "
        "catch {set vmcp_source_mtime [file mtime $vmcp_source_path]}; "
        f"lappend vmcp_source_snapshot_{suffix} [::vivado_agent_mcp_wire_row [list path $vmcp_source_path size_bytes $vmcp_source_size mtime $vmcp_source_mtime]]"
        "}"
    )


def _vcd_limit_check_command() -> str:
    return (
        "if {$vmcp_vcd_limit_bytes > 0} {"
        "set vmcp_current_waveform_total 0; set vmcp_current_waveform_files [list]; "
        "foreach vmcp_pattern [list *.vcd *.wdb] {set vmcp_found [list]; catch {set vmcp_found [glob -nocomplain -directory $sim_dir $vmcp_pattern]}; "
        "foreach f $vmcp_found {if {[lsearch -exact $vmcp_current_waveform_files $f] < 0} {lappend vmcp_current_waveform_files $f}}}; "
        "foreach f $vmcp_monitored_waveform_paths {if {[file exists $f] && [lsearch -exact $vmcp_current_waveform_files $f] < 0} {lappend vmcp_current_waveform_files $f}}; "
        "foreach f $vmcp_current_waveform_files {set sz 0; catch {set sz [file size $f]}; incr vmcp_current_waveform_total $sz}; "
        "if {$vmcp_current_waveform_total > $vmcp_vcd_limit_bytes} {set vmcp_vcd_limit_exceeded 1}"
        "}"
    )


def _simulation_evidence_freshness(metadata: dict[str, str]) -> dict[str, Any]:
    reasons: list[str] = []
    pairs = (
        ("project_dir_before", "project_dir_after"),
        ("project_name_before", "project_name_after"),
        ("simset_before", "simset_after"),
        ("sim_top_before", "sim_top_after"),
        ("source_snapshot_before", "source_snapshot_after"),
    )
    missing_context = False
    for before_key, after_key in pairs:
        before = metadata.get(before_key, "")
        after = metadata.get(after_key, "")
        if not before or not after:
            missing_context = True
            reasons.append(f"missing {before_key}/{after_key}")
        elif before != after:
            reasons.append(f"{before_key} changed during simulation")
    for before_key, after_key in (
        ("include_dirs_before", "include_dirs_after"),
        ("verilog_defines_before", "verilog_defines_after"),
        ("target_simulator_before", "target_simulator_after"),
    ):
        before = metadata.get(before_key, "")
        after = metadata.get(after_key, "")
        if not before and not after:
            continue
        if not before or not after:
            reasons.append(f"missing {before_key}/{after_key}")
        elif before != after:
            reasons.append(f"{before_key} changed during simulation")
    if metadata.get("status_source", "") != "simulation_invocation_log_span":
        reasons.append("status is not based on the current invocation log span")
    if not metadata.get("simulation_invocation_id", ""):
        reasons.append("simulation_invocation_id is missing")
    return {
        "status": "FRESH" if not reasons else "UNKNOWN" if missing_context else "STALE",
        "reasons": reasons,
        "same_project": metadata.get("project_dir_before", "") == metadata.get("project_dir_after", "") != "",
        "same_simset": metadata.get("simset_before", "") == metadata.get("simset_after", "") != "",
        "same_sources": metadata.get("source_snapshot_before", "") == metadata.get("source_snapshot_after", "") != "",
    }


def _bounded_vcd_run_command(run_time: str) -> str:
    total_fs = _duration_fs(run_time)
    if total_fs is None or total_fs <= 0:
        raise ValueError("VCD-limited simulation requires run_time in fs, ps, ns, us, ms, or s")
    chunk_fs = max(1, (total_fs + MAX_VCD_CHECKPOINTS - 1) // MAX_VCD_CHECKPOINTS)
    chunk_count, remainder_fs = divmod(total_fs, chunk_fs)
    check = _vcd_limit_check_command()
    command = (
        f"for {{set vmcp_vcd_step 0}} {{$vmcp_vcd_step < {chunk_count}}} {{incr vmcp_vcd_step}} {{"
        f"if {{[catch {{run {tcl_list_quote(f'{chunk_fs}fs')}}} vmcp_run_error]}} {{set vmcp_run_tcl_failed 1; break}}; {check}; "
        "if {$vmcp_vcd_limit_exceeded} {set vmcp_vcd_limit_stopped 1; break}"
        "}"
    )
    if remainder_fs:
        command += (
            "; if {!$vmcp_vcd_limit_stopped && !$vmcp_run_tcl_failed} {"
            f"if {{[catch {{run {tcl_list_quote(f'{remainder_fs}fs')}}} vmcp_run_error]}} {{set vmcp_run_tcl_failed 1}}; {check}; "
            "if {$vmcp_vcd_limit_exceeded} {set vmcp_vcd_limit_stopped 1}"
            "}"
        )
    return command


def _duration_fs(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*", str(value))
    if not match:
        return None
    unit = match.group(2).lower()
    multiplier = _TIME_UNIT_FS.get(unit)
    if multiplier is None:
        return None
    try:
        duration = Decimal(match.group(1)) * multiplier
    except InvalidOperation:
        return None
    return int(duration.to_integral_value(rounding=ROUND_CEILING))


def _tcl_list(values: list[str]) -> str:
    return "[list " + " ".join(tcl_list_quote(value) for value in values) + "]"


def _define_list(defines: dict[str, str | None]) -> str:
    values = [name if value is None else f"{name}={value}" for name, value in validate_simulation_defines(defines).items()]
    return _tcl_list(values)


def _xsim_time(value: str) -> str:
    text = value.strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s+([A-Za-z]+)", text)
    if match:
        return f"{match.group(1)}{match.group(2)}"
    return text


def _max_vcd_bytes(max_vcd_mb: int | float) -> int:
    try:
        value = float(max_vcd_mb)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_VCD_MB
    if value <= 0:
        return 0
    return int(value * 1024 * 1024)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split_metadata_and_log(raw: str) -> tuple[str, str]:
    marker_line = f"log_begin={LOG_MARKER}"
    if marker_line not in raw:
        return raw, ""
    metadata, log = raw.split(marker_line, 1)
    return metadata, log.lstrip("\r\n")


def _parse_key_value_lines(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _split_path_list(value: str) -> list[str]:
    return decode_wire_list(value)


def _parse_file_rows(value: str, *, fallback_paths: str = "") -> list[dict[str, Any]]:
    rows = _split_path_list(value)
    if not rows and fallback_paths:
        rows = _split_path_list(fallback_paths)
    result: list[dict[str, Any]] = []
    for row in rows:
        if row.startswith(HEX_ROW_PREFIX):
            fields = decode_wire_row(row)
            result.append({"path": fields.get("path", ""), "size_bytes": _int_or_none(fields.get("size_bytes", ""))})
            continue
        path, size_text = row, ""
        if "|" in row:
            path, size_text = row.rsplit("|", 1)
        result.append({"path": path, "size_bytes": _int_or_none(size_text)})
    return result


def _sum_file_sizes(files: list[dict[str, Any]]) -> int:
    total = 0
    for item in files:
        size = item.get("size_bytes")
        if isinstance(size, int):
            total += size
    return total


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _classify_simulation_status(log_text: str, counts: dict[str, int]) -> str:
    lower = log_text.lower()
    if counts.get("ERROR", 0) > 0 or _has_failure_token(lower):
        return "failed"
    if "simulation finished" in lower or "finish called" in lower or "exit" in lower:
        return "completed"
    return "unknown"


def _diagnose_simulation(
    *,
    status: str,
    log_text: str,
    counts: dict[str, int],
    vcd_limit_exceeded: bool,
    vcd_conflict: bool,
    vcd_conflict_severity: str,
    mcp_vcd_export_mode: str,
    testbench_vcd_detected: bool,
) -> dict[str, Any]:
    lower = log_text.lower()
    causes: list[str] = []
    warnings = _simulation_warnings(log_text)
    info = []
    if vcd_limit_exceeded:
        causes.append("vcd_limit_exceeded")
    if re.search(r"\btest\s+fail", lower) or _has_failure_token(lower):
        causes.append("testbench_failure")
    if counts.get("ERROR", 0) > 0:
        causes.append("vivado_error")
    if (vcd_conflict and vcd_conflict_severity in {"warning", "error"}) or "only one vcd file" in lower:
        causes.append("vcd_conflict")
    if status == "unknown":
        causes.append("unknown_incomplete")
    if not causes and status == "completed" and re.search(r"\b(tb_)?pass\b", lower):
        causes.append("testbench_pass")
    if not causes and status == "completed" and mcp_vcd_export_mode == "testbench_existing" and testbench_vcd_detected:
        causes.append("completed_with_testbench_vcd")
    if mcp_vcd_export_mode == "testbench_existing" and testbench_vcd_detected:
        info.append(
            {
                "code": "TESTBENCH_EXISTING_VCD",
                "severity": "INFO",
                "message": "Testbench already generates VCD; MCP did not call open_vcd again.",
            }
        )
    if not causes:
        causes.append("completed" if status == "completed" else status)
    return {
        "primary_cause": causes[0],
        "causes": causes,
        "status": status,
        "warnings": warnings,
        "info": info,
    }


def _has_failure_token(lower_log_text: str) -> bool:
    return bool(re.search(r"(^|[^a-z0-9])(?:tb_)?fail(?:ed|ure)?([^a-z0-9]|$)", lower_log_text))


def _simulation_warnings(log_text: str) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    lower = log_text.lower()
    if "doesn't have a timescale" in lower or "does not have a timescale" in lower:
        warnings.append(
            {
                "code": "MISSING_TIMESCALE",
                "severity": "LOW",
                "message": "One or more modules do not declare a timescale while another module does.",
            }
        )
    return warnings


def _vcd_conflict_severity(
    value: str,
    *,
    log_text: str,
    status: str,
    vcd_conflict: bool,
    mcp_vcd_export_mode: str,
    testbench_vcd_detected: bool,
) -> str:
    explicit = str(value).strip().lower()
    if explicit:
        return explicit
    if "only one vcd file" in log_text.lower():
        return "error" if status == "failed" else "warning"
    if mcp_vcd_export_mode == "testbench_existing" and testbench_vcd_detected:
        return "info"
    if vcd_conflict:
        return "error" if status == "failed" else "warning"
    return ""


def _new_invocation_id() -> str:
    return f"sim-{uuid.uuid4().hex[:12]}"


def _excerpt(text: str, limit: int = 4096) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]
