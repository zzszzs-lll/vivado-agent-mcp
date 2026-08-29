from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .tcl import tcl_list_quote
from .wire import decode_wire_list, decode_wire_row, tcl_wire_prelude

GENERATED_OUTPUT_MARKERS = (".runs", ".sim", ".cache", ".gen")
RUN_HOOK_BLOCK_MARKER = "VMCP_RUN_HOOK_BLOCKED"
EXECUTABLE_CONSTRAINT_BLOCK_MARKER = "VMCP_EXECUTABLE_CONSTRAINT_INPUT_BLOCKED"
EXECUTABLE_COMPOSITE_INPUT_BLOCK_MARKER = "VMCP_EXECUTABLE_COMPOSITE_INPUT_BLOCKED"
EXECUTABLE_INPUT_DISCOVERY_BLOCK_MARKER = "VMCP_EXECUTABLE_INPUT_DISCOVERY_FAILED"
VIVADO_VERSION_BLOCK_MARKER = "VMCP_UNSUPPORTED_VIVADO_VERSION"
SUPPORTED_VIVADO_VERSION = "2021.2"
MAX_TRUSTED_XDC_BYTES = 2 * 1024 * 1024
ALLOWED_XDC_COMMANDS = frozenset(
    {
        "create_clock",
        "create_generated_clock",
        "current_instance",
        "group_path",
        "set_bus_skew",
        "set_case_analysis",
        "set_clock_gating_check",
        "set_clock_groups",
        "set_clock_latency",
        "set_clock_sense",
        "set_clock_transition",
        "set_clock_uncertainty",
        "set_data_check",
        "set_disable_timing",
        "set_drive",
        "set_driving_cell",
        "set_false_path",
        "set_input_delay",
        "set_input_jitter",
        "set_load",
        "set_logic_dc",
        "set_logic_one",
        "set_logic_zero",
        "set_max_capacitance",
        "set_max_delay",
        "set_max_fanout",
        "set_max_time_borrow",
        "set_max_transition",
        "set_min_delay",
        "set_multicycle_path",
        "set_operating_conditions",
        "set_output_delay",
        "set_propagated_clock",
        "set_property",
        "set_system_jitter",
        "set_units",
    }
)
ALLOWED_XDC_QUERY_COMMANDS = frozenset(
    {
        "all_clocks",
        "all_inputs",
        "all_outputs",
        "all_registers",
        "current_design",
        "get_bels",
        "get_cells",
        "get_clocks",
        "get_generated_clocks",
        "get_nets",
        "get_package_pins",
        "get_pins",
        "get_ports",
        "get_sites",
        "get_tiles",
        "list",
    }
)
BASE_ALLOWED_RUN_PROPERTIES = {
    "AUTO_INCREMENTAL_CHECKPOINT",
    "CONSTRSET",
    "INCREMENTAL_CHECKPOINT",
    "IS_ENABLED",
    "PART",
}
VIVADO_2021_2_ALLOWED_RUN_STEP_PROPERTIES = frozenset(
    {
        "STEPS.SYNTH_DESIGN.ARGS.DIRECTIVE",
        "STEPS.SYNTH_DESIGN.ARGS.FLATTEN_HIERARCHY",
        "STEPS.SYNTH_DESIGN.ARGS.FSM_EXTRACTION",
        "STEPS.SYNTH_DESIGN.ARGS.RESOURCE_SHARING",
        "STEPS.SYNTH_DESIGN.ARGS.RETIMING",
        "STEPS.SYNTH_DESIGN.ARGS.KEEP_EQUIVALENT_REGISTERS",
        "STEPS.SYNTH_DESIGN.ARGS.NO_LC",
        "STEPS.SYNTH_DESIGN.ARGS.SHREG_MIN_SIZE",
        "STEPS.SYNTH_DESIGN.ARGS.CONTROL_SET_OPT_THRESHOLD",
        "STEPS.SYNTH_DESIGN.IS_ENABLED",
        "STEPS.OPT_DESIGN.ARGS.DIRECTIVE",
        "STEPS.OPT_DESIGN.IS_ENABLED",
        "STEPS.POWER_OPT_DESIGN.ARGS.DIRECTIVE",
        "STEPS.POWER_OPT_DESIGN.IS_ENABLED",
        "STEPS.PLACE_DESIGN.ARGS.DIRECTIVE",
        "STEPS.PLACE_DESIGN.IS_ENABLED",
        "STEPS.PHYS_OPT_DESIGN.ARGS.DIRECTIVE",
        "STEPS.PHYS_OPT_DESIGN.IS_ENABLED",
        "STEPS.ROUTE_DESIGN.ARGS.DIRECTIVE",
        "STEPS.ROUTE_DESIGN.IS_ENABLED",
        "STEPS.POST_ROUTE_PHYS_OPT_DESIGN.ARGS.DIRECTIVE",
        "STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED",
        "STEPS.WRITE_BITSTREAM.IS_ENABLED",
    }
)
ALLOWED_RUN_PROPERTIES = frozenset(BASE_ALLOWED_RUN_PROPERTIES) | VIVADO_2021_2_ALLOWED_RUN_STEP_PROPERTIES
_FORBIDDEN_RUN_PROPERTY = re.compile(r"(?:^|[._])(?:TCL|HOOK|SCRIPT)(?:$|[._])")


def get_run_configuration_command(*, run_name: str) -> str:
    run_ref = tcl_list_quote(run_name)
    return (
        f"set runs [get_runs -quiet {run_ref}]; "
        f"if {{[llength $runs] == 0}} {{error {tcl_list_quote(f'Vivado run not found: {run_name}')}}}; "
        "set r [lindex $runs 0]; "
        "set properties \"\"; catch {set properties [report_property -return_string $r]}; "
        "join [list "
        "\"name=[get_property NAME $r]\" "
        "\"flow=[get_property FLOW $r]\" "
        "\"strategy=[get_property STRATEGY $r]\" "
        "\"status=[get_property STATUS $r]\" "
        "\"progress=[get_property PROGRESS $r]\" "
        "\"needs_refresh=[get_property NEEDS_REFRESH $r]\" "
        "\"directory=[get_property DIRECTORY $r]\" "
        "\"properties_begin=__VMCP_RUN_PROPERTIES_BEGIN__\" "
        "$properties"
        "] \"\\n\""
    )


def configure_run_command(
    *,
    run_name: str,
    strategy: str | None = None,
    properties: dict[str, Any] | None = None,
) -> str:
    validate_run_properties(properties or {})
    run_ref = tcl_list_quote(run_name)
    parts = [
        f"set runs [get_runs -quiet {run_ref}]",
        f"if {{[llength $runs] == 0}} {{error {tcl_list_quote(f'Vivado run not found: {run_name}')}}}",
        "set r [lindex $runs 0]",
    ]
    if strategy:
        parts.append(f"set_property strategy {tcl_list_quote(strategy)} $r")
    for key, value in (properties or {}).items():
        parts.append(f"set_property {tcl_list_quote(key)} {tcl_list_quote(value)} $r")
    parts.append(
        "join [list "
        f"{tcl_list_quote(f'name={run_name}')} "
        "\"strategy=[get_property STRATEGY $r]\" "
        "\"needs_refresh=[get_property NEEDS_REFRESH $r]\" "
        "\"status=[get_property STATUS $r]\""
        "] \"\\n\""
    )
    return "; ".join(parts)


def validate_run_properties(properties: dict[str, Any]) -> None:
    for raw_key, value in properties.items():
        raw_text = str(raw_key)
        key = raw_text.strip().upper()
        if raw_text != key:
            raise ValueError(f"Vivado run property names must use canonical uppercase spelling without surrounding whitespace: {raw_key!r}")
        if not key or _FORBIDDEN_RUN_PROPERTY.search(key):
            raise ValueError(f"Vivado run property is permanently blocked by policy: {raw_key!r}")
        if key not in ALLOWED_RUN_PROPERTIES:
            raise ValueError(f"Vivado run property is not in the configure_run allowlist: {raw_key!r}")
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise ValueError(f"Vivado run property values must be scalar JSON values: {raw_key!r}")


def run_hook_guard_command(*, run_name: str | None = None, close_project_on_block: bool = False) -> str:
    if run_name:
        run_ref = tcl_list_quote(run_name)
        select_runs = (
            f"if {{[catch {{set vmcp_guard_runs [get_runs {run_ref}]}} vmcp_guard_error]}} {{"
            "lappend vmcp_guard_discovery_errors [list get_runs $vmcp_guard_error]; set vmcp_guard_runs [list]}; "
            f"if {{[llength $vmcp_guard_runs] == 0}} {{error {tcl_list_quote(f'Vivado run not found: {run_name}')}}}"
        )
    else:
        select_runs = (
            "if {[catch {set vmcp_guard_runs [get_runs]} vmcp_guard_error]} {"
            "lappend vmcp_guard_discovery_errors [list get_runs $vmcp_guard_error]; set vmcp_guard_runs [list]}"
        )
    return "; ".join(
        [
            "set vmcp_guard_vivado_version [version -short]",
            (
                f"if {{![string equal {{{SUPPORTED_VIVADO_VERSION}}} $vmcp_guard_vivado_version]}} {{"
                + ("catch {close_project}; " if close_project_on_block else "")
                + f"error \"{VIVADO_VERSION_BLOCK_MARKER}: expected {SUPPORTED_VIVADO_VERSION}, got $vmcp_guard_vivado_version\"}}"
            ),
            "set vmcp_guard_discovery_errors [list]",
            select_runs,
            "set vmcp_guard_hook_properties [list]",
            "foreach vmcp_guard_run $vmcp_guard_runs {"
            "set vmcp_guard_properties [list]; "
            "if {[catch {set vmcp_guard_properties [list_property $vmcp_guard_run]} vmcp_guard_error]} {"
            "lappend vmcp_guard_discovery_errors [list list_property $vmcp_guard_error]; continue}; "
            "foreach vmcp_guard_property $vmcp_guard_properties {"
            "set vmcp_guard_name [string toupper $vmcp_guard_property]; "
            "if {[regexp {(^|[._])(TCL|HOOK|SCRIPT)([._]|$)} $vmcp_guard_name]} {"
            "set vmcp_guard_value {}; "
            "if {[catch {set vmcp_guard_value [get_property $vmcp_guard_property $vmcp_guard_run]} vmcp_guard_error]} {"
            "lappend vmcp_guard_discovery_errors [list get_property $vmcp_guard_property $vmcp_guard_error]"
            "} elseif {[string trim $vmcp_guard_value] ne {}} {"
            "lappend vmcp_guard_hook_properties [list [get_property NAME $vmcp_guard_run] $vmcp_guard_property]"
            "}"
            "}"
            "}"
            "}",
            (
                f"if {{[llength $vmcp_guard_hook_properties] > 0}} {{"
                + ("catch {close_project}; " if close_project_on_block else "")
                + f"error \"{RUN_HOOK_BLOCK_MARKER}: non-empty run hook properties: $vmcp_guard_hook_properties\"}}"
            ),
            "set vmcp_guard_constraint_issues [list]",
            "set vmcp_guard_filesets [list]; "
            "if {[catch {set vmcp_guard_filesets [get_filesets]} vmcp_guard_error]} {"
            "lappend vmcp_guard_discovery_errors [list get_filesets $vmcp_guard_error]}; "
            "foreach vmcp_guard_fileset $vmcp_guard_filesets {"
            "set vmcp_guard_fileset_name {}; "
            "if {[catch {set vmcp_guard_fileset_name [get_property NAME $vmcp_guard_fileset]} vmcp_guard_error]} {"
            "lappend vmcp_guard_discovery_errors [list fileset_name $vmcp_guard_error]; continue}; "
            "set vmcp_guard_fileset_type {}; "
            "if {[catch {set vmcp_guard_fileset_type [get_property FILESET_TYPE $vmcp_guard_fileset]} vmcp_guard_error]} {"
            "lappend vmcp_guard_discovery_errors [list fileset_type $vmcp_guard_error]; continue}; "
            "if {[string match -nocase {*constr*} $vmcp_guard_fileset_type] || "
            "[string match -nocase {constrs*} $vmcp_guard_fileset_name]} {"
            "set vmcp_guard_constraints [list]; "
            "if {[catch {set vmcp_guard_constraints [get_files -of_objects $vmcp_guard_fileset]} vmcp_guard_error]} {"
            "lappend vmcp_guard_discovery_errors [list constraint_files $vmcp_guard_fileset_name $vmcp_guard_error]; continue}; "
            "foreach vmcp_guard_constraint $vmcp_guard_constraints {"
            "set vmcp_guard_constraint_path [file normalize $vmcp_guard_constraint]; "
            "set vmcp_guard_constraint_type {}; "
            "if {[catch {set vmcp_guard_constraint_type [get_property FILE_TYPE $vmcp_guard_constraint]} vmcp_guard_error]} {"
            "lappend vmcp_guard_discovery_errors [list constraint_type $vmcp_guard_constraint_path $vmcp_guard_error]; continue}; "
            "set vmcp_guard_constraint_extension [string tolower [file extension $vmcp_guard_constraint_path]]; "
            "if {$vmcp_guard_constraint_extension ne {.xdc} || "
            "![string equal -nocase [string trim $vmcp_guard_constraint_type] {XDC}]} {"
            "lappend vmcp_guard_constraint_issues [list $vmcp_guard_fileset_name $vmcp_guard_constraint_path $vmcp_guard_constraint_type]"
            "}"
            "}"
            "}"
            "}",
            (
                f"if {{[llength $vmcp_guard_constraint_issues] > 0}} {{"
                + ("catch {close_project}; " if close_project_on_block else "")
                + f"error \"{EXECUTABLE_CONSTRAINT_BLOCK_MARKER}: constraints must use .xdc files with FILE_TYPE XDC: $vmcp_guard_constraint_issues\"}}"
            ),
            "set vmcp_guard_composite_inputs [list]",
            "set vmcp_guard_project {}; "
            "if {[catch {set vmcp_guard_project [current_project]} vmcp_guard_error]} {lappend vmcp_guard_discovery_errors [list current_project $vmcp_guard_error]}; "
            "set vmcp_guard_ip_repositories {}; "
            "if {$vmcp_guard_project ne {} && [catch {set vmcp_guard_ip_repositories [get_property IP_REPO_PATHS $vmcp_guard_project]} vmcp_guard_error]} {"
            "lappend vmcp_guard_discovery_errors [list ip_repositories $vmcp_guard_error]}; "
            "if {[llength $vmcp_guard_ip_repositories] > 0} {lappend vmcp_guard_composite_inputs [list ip_repository $vmcp_guard_ip_repositories]}",
            "set vmcp_guard_ips [list]; if {[catch {set vmcp_guard_ips [get_ips *]} vmcp_guard_error]} {lappend vmcp_guard_discovery_errors [list get_ips $vmcp_guard_error]}; "
            "foreach vmcp_guard_ip $vmcp_guard_ips {lappend vmcp_guard_composite_inputs [list ip $vmcp_guard_ip]}",
            "set vmcp_guard_files [list]; if {[catch {set vmcp_guard_files [get_files *]} vmcp_guard_error]} {lappend vmcp_guard_discovery_errors [list get_files $vmcp_guard_error]}; "
            "foreach vmcp_guard_file $vmcp_guard_files {"
            "set vmcp_guard_path [file normalize $vmcp_guard_file]; "
            "set vmcp_guard_extension [string tolower [file extension $vmcp_guard_path]]; "
            "if {$vmcp_guard_extension in {.xci .bd .dcp} || [string equal -nocase [file tail $vmcp_guard_path] {component.xml}]} {"
            "lappend vmcp_guard_composite_inputs [list composite_file $vmcp_guard_path]"
            "}"
            "}",
            (
                f"if {{[llength $vmcp_guard_discovery_errors] > 0}} {{"
                + ("catch {close_project}; " if close_project_on_block else "")
                + f"error \"{EXECUTABLE_INPUT_DISCOVERY_BLOCK_MARKER}: required Vivado discovery failed: $vmcp_guard_discovery_errors\"}}"
            ),
            (
                f"if {{[llength $vmcp_guard_composite_inputs] > 0}} {{"
                + ("catch {close_project}; " if close_project_on_block else "")
                + f"error \"{EXECUTABLE_COMPOSITE_INPUT_BLOCK_MARKER}: IP/BD/XCI/DCP inputs are not yet accepted by the trusted execution closure: $vmcp_guard_composite_inputs\"}}"
            ),
        ]
    )


def blocked_constraint_file_inputs(paths: list[str]) -> list[str]:
    return [path for path in paths if Path(path).suffix.lower() != ".xdc"]


def project_execution_inputs_command() -> str:
    run_property_list = "[list " + " ".join(
        tcl_list_quote(item)
        for item in sorted(ALLOWED_RUN_PROPERTIES | {"FLOW", "STRATEGY", "SRCSET"})
    ) + "]"
    return "; ".join(
        [
            tcl_wire_prelude(),
            "set vmcp_discovery_errors [list]",
            "set vmcp_project {}; set vmcp_project_dir {}; set vmcp_project_name {}; set vmcp_project_path {}; set vmcp_part {}; set vmcp_top {}; set vmcp_include_dirs [list]; set vmcp_verilog_defines [list]",
            "set vmcp_vivado_version_short {}; if {[catch {set vmcp_vivado_version_short [version -short]} vmcp_error]} {lappend vmcp_discovery_errors \"version -short failed: $vmcp_error\"}",
            "set vmcp_vivado_version_full {}; if {[catch {set vmcp_vivado_version_full [string map {\\n { } \\r { }} [version]]} vmcp_error]} {lappend vmcp_discovery_errors \"version failed: $vmcp_error\"}",
            "if {[catch {set vmcp_project [current_project]} vmcp_error]} {lappend vmcp_discovery_errors \"current_project failed: $vmcp_error\"} elseif {$vmcp_project eq {}} {lappend vmcp_discovery_errors {current_project returned no project}}",
            "if {$vmcp_project ne {}} {"
            "if {[catch {set vmcp_project_dir [file normalize [get_property DIRECTORY $vmcp_project]]} vmcp_error]} {lappend vmcp_discovery_errors \"project DIRECTORY failed: $vmcp_error\"}; "
            "if {[catch {set vmcp_project_name [get_property NAME $vmcp_project]} vmcp_error]} {lappend vmcp_discovery_errors \"project NAME failed: $vmcp_error\"}; "
            "if {$vmcp_project_dir ne {} && $vmcp_project_name ne {}} {set vmcp_project_path [file normalize [file join $vmcp_project_dir ${vmcp_project_name}.xpr]]}; "
            "if {[catch {set vmcp_part [get_property PART $vmcp_project]} vmcp_error]} {lappend vmcp_discovery_errors \"project PART failed: $vmcp_error\"}"
            "}",
            "set vmcp_sources_fileset {}; if {[catch {set vmcp_sources_fileset [get_filesets {sources_1}]} vmcp_error]} {lappend vmcp_discovery_errors \"sources_1 fileset failed: $vmcp_error\"} elseif {[llength $vmcp_sources_fileset] == 0} {lappend vmcp_discovery_errors {sources_1 fileset is missing}}",
            "if {[llength $vmcp_sources_fileset] > 0} {"
            "if {[catch {set vmcp_top [get_property TOP $vmcp_sources_fileset]} vmcp_error]} {lappend vmcp_discovery_errors \"sources_1 TOP failed: $vmcp_error\"}; "
            "if {[catch {set vmcp_include_dirs [get_property INCLUDE_DIRS $vmcp_sources_fileset]} vmcp_error]} {lappend vmcp_discovery_errors \"sources_1 INCLUDE_DIRS failed: $vmcp_error\"}; "
            "if {[catch {set vmcp_verilog_defines [get_property VERILOG_DEFINE $vmcp_sources_fileset]} vmcp_error]} {lappend vmcp_discovery_errors \"sources_1 VERILOG_DEFINE failed: $vmcp_error\"}"
            "}",
            "set vmcp_source_rows [list]",
            "set vmcp_seen_source_paths [dict create]",
            "set vmcp_usage synthesis; "
            "set vmcp_effective_sources [list]; "
            "if {[catch {set vmcp_effective_sources [get_files -compile_order sources -used_in $vmcp_usage]} vmcp_error]} {"
            "lappend vmcp_discovery_errors \"source compile order $vmcp_usage failed: $vmcp_error\""
            "} else {set vmcp_order 0; foreach vmcp_source $vmcp_effective_sources {"
            "set vmcp_source_path [file normalize $vmcp_source]; set vmcp_source_type {}; set vmcp_source_library {}; "
            "dict set vmcp_seen_source_paths $vmcp_source_path 1; "
            "if {[catch {set vmcp_source_type [get_property FILE_TYPE $vmcp_source]} vmcp_error]} {lappend vmcp_discovery_errors \"source FILE_TYPE $vmcp_source_path failed: $vmcp_error\"}; "
            "if {[catch {set vmcp_source_library [get_property LIBRARY $vmcp_source]} vmcp_error]} {lappend vmcp_discovery_errors \"source LIBRARY $vmcp_source_path failed: $vmcp_error\"}; "
            "lappend vmcp_source_rows [::vivado_agent_mcp_wire_row [list used_in $vmcp_usage order $vmcp_order path $vmcp_source_path type $vmcp_source_type library $vmcp_source_library]]; incr vmcp_order"
            "}}",
            "set vmcp_fileset_sources [list]; "
            "if {[catch {set vmcp_fileset_sources [get_files -quiet -of_objects $vmcp_sources_fileset]} vmcp_error]} {"
            "lappend vmcp_discovery_errors \"sources_1 complete inventory failed: $vmcp_error\""
            "} else {set vmcp_inventory_order 0; foreach vmcp_source $vmcp_fileset_sources {"
            "set vmcp_source_path [file normalize $vmcp_source]; "
            "if {[dict exists $vmcp_seen_source_paths $vmcp_source_path]} {incr vmcp_inventory_order; continue}; "
            "set vmcp_source_used_in [list]; set vmcp_source_type {}; set vmcp_source_library {}; "
            "if {[catch {set vmcp_source_used_in [get_property USED_IN $vmcp_source]} vmcp_error]} {"
            "lappend vmcp_discovery_errors \"source USED_IN $vmcp_source_path failed: $vmcp_error\"; incr vmcp_inventory_order; continue}; "
            "if {[lsearch -nocase -exact $vmcp_source_used_in synthesis] < 0 && "
            "[lsearch -nocase -exact $vmcp_source_used_in implementation] < 0} {incr vmcp_inventory_order; continue}; "
            "if {[catch {set vmcp_source_type [get_property FILE_TYPE $vmcp_source]} vmcp_error]} {lappend vmcp_discovery_errors \"source FILE_TYPE $vmcp_source_path failed: $vmcp_error\"}; "
            "if {[catch {set vmcp_source_library [get_property LIBRARY $vmcp_source]} vmcp_error]} {lappend vmcp_discovery_errors \"source LIBRARY $vmcp_source_path failed: $vmcp_error\"}; "
            "lappend vmcp_source_rows [::vivado_agent_mcp_wire_row [list used_in [join $vmcp_source_used_in ,] order $vmcp_inventory_order path $vmcp_source_path type $vmcp_source_type library $vmcp_source_library inventory_scope fileset]]; "
            "dict set vmcp_seen_source_paths $vmcp_source_path 1; incr vmcp_inventory_order"
            "}}",
            "set vmcp_constraint_rows [list]",
            "set vmcp_seen_constraints [dict create]",
            "foreach vmcp_usage {synthesis implementation} {"
            "set vmcp_effective_constraints [list]; "
            "if {[catch {set vmcp_effective_constraints [get_files -compile_order constraints -used_in $vmcp_usage]} vmcp_error]} {"
            "lappend vmcp_discovery_errors \"constraint compile order $vmcp_usage failed: $vmcp_error\""
            "} else {set vmcp_order 0; foreach vmcp_constraint $vmcp_effective_constraints {"
            "set vmcp_constraint_path [file normalize $vmcp_constraint]; "
            "set vmcp_constraint_type {}; if {[catch {set vmcp_constraint_type [get_property FILE_TYPE $vmcp_constraint]} vmcp_error]} {lappend vmcp_discovery_errors \"constraint FILE_TYPE $vmcp_constraint_path failed: $vmcp_error\"}; "
            "lappend vmcp_constraint_rows [::vivado_agent_mcp_wire_row [list used_in $vmcp_usage order $vmcp_order path $vmcp_constraint_path type $vmcp_constraint_type]]; incr vmcp_order"
            "}}"
            "}",
            "set vmcp_composite_rows [list]",
            "set vmcp_ip_repositories {}; if {$vmcp_project ne {} && [catch {set vmcp_ip_repositories [get_property IP_REPO_PATHS $vmcp_project]} vmcp_error]} {lappend vmcp_discovery_errors \"IP_REPO_PATHS failed: $vmcp_error\"}; "
            "foreach vmcp_repo $vmcp_ip_repositories {lappend vmcp_composite_rows [::vivado_agent_mcp_wire_row [list kind ip_repository value [file normalize $vmcp_repo]]]}",
            "set vmcp_ips [list]; if {[catch {set vmcp_ips [get_ips *]} vmcp_error]} {lappend vmcp_discovery_errors \"get_ips failed: $vmcp_error\"}; "
            "foreach vmcp_ip $vmcp_ips {lappend vmcp_composite_rows [::vivado_agent_mcp_wire_row [list kind ip value $vmcp_ip]]}",
            "set vmcp_all_files [list]; if {[catch {set vmcp_all_files [get_files *]} vmcp_error]} {lappend vmcp_discovery_errors \"get_files failed: $vmcp_error\"}; foreach vmcp_file $vmcp_all_files {"
            "set vmcp_path [file normalize $vmcp_file]; "
            "set vmcp_extension [string tolower [file extension $vmcp_path]]; "
            "if {$vmcp_extension in {.xci .bd .dcp} || [string equal -nocase [file tail $vmcp_path] {component.xml}]} {"
            "set vmcp_type {}; if {[catch {set vmcp_type [get_property FILE_TYPE $vmcp_file]} vmcp_error]} {lappend vmcp_discovery_errors \"composite FILE_TYPE $vmcp_path failed: $vmcp_error\"}; "
            "lappend vmcp_composite_rows [::vivado_agent_mcp_wire_row [list kind composite_file value $vmcp_path type $vmcp_type]]"
            "}"
            "}",
            "set vmcp_run_rows [list]; set vmcp_runs [list]; if {[catch {set vmcp_runs [get_runs]} vmcp_error]} {lappend vmcp_discovery_errors \"get_runs failed: $vmcp_error\"}; foreach vmcp_run $vmcp_runs {"
            "set vmcp_run_name {}; if {[catch {set vmcp_run_name [get_property NAME $vmcp_run]} vmcp_error]} {lappend vmcp_discovery_errors \"run NAME failed: $vmcp_error\"; continue}; "
            f"foreach vmcp_run_property {run_property_list} {{set vmcp_run_value {{}}; if {{[catch {{set vmcp_run_value [get_property $vmcp_run_property $vmcp_run]}} vmcp_error]}} {{lappend vmcp_discovery_errors \"run $vmcp_run_name property $vmcp_run_property failed: $vmcp_error\"}} else {{lappend vmcp_run_rows [::vivado_agent_mcp_wire_row [list run $vmcp_run_name property $vmcp_run_property value $vmcp_run_value]]}}}}; "
            "set vmcp_srcset {}; if {[catch {set vmcp_srcset [get_property SRCSET $vmcp_run]} vmcp_error]} {lappend vmcp_discovery_errors \"run SRCSET failed: $vmcp_error\"}; "
            "if {$vmcp_srcset ne {} && $vmcp_srcset ni {sources_1 sim_1}} {"
            "lappend vmcp_composite_rows [::vivado_agent_mcp_wire_row [list kind ooc_run value [get_property NAME $vmcp_run] srcset $vmcp_srcset]]"
            "}"
            "}",
            "set vmcp_error_rows [list]; foreach vmcp_error $vmcp_discovery_errors {lappend vmcp_error_rows [::vivado_agent_mcp_wire_row [list error $vmcp_error]]}",
            "join [concat [list "
            "\"vivado_version_short=$vmcp_vivado_version_short\" "
            "\"vivado_version_full=$vmcp_vivado_version_full\" "
            "\"project_dir=$vmcp_project_dir\" "
            "\"project_name=$vmcp_project_name\" "
            "\"project_path=$vmcp_project_path\" "
            "\"part=$vmcp_part\" "
            "\"top=$vmcp_top\" "
            "\"include_dirs=[::vivado_agent_mcp_wire_list $vmcp_include_dirs]\" "
            "\"verilog_defines=[::vivado_agent_mcp_wire_list $vmcp_verilog_defines]\" "
            "\"sources_begin=__VMCP_SOURCE_INPUTS_BEGIN__\""
            "] $vmcp_source_rows [list "
            "\"constraints_begin=__VMCP_CONSTRAINT_INPUTS_BEGIN__\""
            "] $vmcp_constraint_rows [list \"run_configurations_begin=__VMCP_RUN_CONFIGURATIONS_BEGIN__\"] $vmcp_run_rows "
            "[list \"composite_inputs_begin=__VMCP_COMPOSITE_INPUTS_BEGIN__\"] $vmcp_composite_rows "
            "[list \"discovery_errors_begin=__VMCP_EXECUTION_INPUT_ERRORS_BEGIN__\"] $vmcp_error_rows] \"\\n\"",
        ]
    )


def parse_project_execution_inputs(raw: str) -> dict[str, Any]:
    source_marker = "sources_begin=__VMCP_SOURCE_INPUTS_BEGIN__"
    constraint_marker = "constraints_begin=__VMCP_CONSTRAINT_INPUTS_BEGIN__"
    run_marker = "run_configurations_begin=__VMCP_RUN_CONFIGURATIONS_BEGIN__"
    composite_marker = "composite_inputs_begin=__VMCP_COMPOSITE_INPUTS_BEGIN__"
    error_marker = "discovery_errors_begin=__VMCP_EXECUTION_INPUT_ERRORS_BEGIN__"
    wire_format_complete = all(marker in raw for marker in (source_marker, constraint_marker, run_marker, composite_marker, error_marker))
    metadata, _, remainder = raw.partition(source_marker)
    source_text, _, remainder = remainder.partition(constraint_marker)
    constraint_text, _, remainder = remainder.partition(run_marker)
    run_text, _, remainder = remainder.partition(composite_marker)
    composite_text, _, error_text = remainder.partition(error_marker)
    values = _parse_key_value_lines(metadata)
    sources = [decode_wire_row(line) for line in source_text.splitlines() if line.strip()]
    constraints = [decode_wire_row(line) for line in constraint_text.splitlines() if line.strip()]
    run_configurations = [decode_wire_row(line) for line in run_text.splitlines() if line.strip()]
    composites = [decode_wire_row(line) for line in composite_text.splitlines() if line.strip()]
    errors = [decode_wire_row(line) for line in error_text.splitlines() if line.strip()]
    if not wire_format_complete:
        errors.append({"error": "execution input response is missing required wire sections"})
    parsed = {
        "vivado_version_short": values.get("vivado_version_short", ""),
        "vivado_version_full": values.get("vivado_version_full", ""),
        "project_dir": values.get("project_dir", ""),
        "project_name": values.get("project_name", ""),
        "project_path": values.get("project_path", ""),
        "part": values.get("part", ""),
        "top": values.get("top", ""),
        "include_dirs": decode_wire_list(values.get("include_dirs", "")),
        "verilog_defines": decode_wire_list(values.get("verilog_defines", "")),
        "sources": sources,
        "constraints": constraints,
        "run_configurations": run_configurations,
        "composite_inputs": composites,
        "discovery_errors": errors,
        "wire_format_complete": wire_format_complete,
    }
    for required in ("vivado_version_short", "project_dir", "project_name", "project_path", "part", "top"):
        if not parsed[required]:
            parsed["discovery_errors"].append({"error": f"required execution input metadata is missing: {required}"})
    if not sources:
        parsed["discovery_errors"].append({"error": "source compile-order discovery returned no executable sources"})
    return parsed


def validate_xdc_text(text: str) -> list[str]:
    try:
        commands, position = _scan_tcl_script(text, 0, terminator=None, depth=0)
        if position != len(text):
            return ["XDC parser did not consume the complete file"]
    except ValueError as exc:
        return [str(exc)]
    issues: list[str] = []
    for depth, command, line in commands:
        allowed = ALLOWED_XDC_COMMANDS if depth == 0 else ALLOWED_XDC_QUERY_COMMANDS
        if command.lower() not in allowed:
            scope = "top-level" if depth == 0 else "command substitution"
            issues.append(f"line {line}: {scope} command {command!r} is not in the trusted XDC allowlist")
    return issues


def _scan_tcl_script(
    text: str,
    start: int,
    *,
    terminator: str | None,
    depth: int,
) -> tuple[list[tuple[int, str, int]], int]:
    commands: list[tuple[int, str, int]] = []
    index = start
    command_start = True
    while index < len(text):
        char = text[index]
        if terminator and char == terminator:
            return commands, index + 1
        if char in " \t\r":
            index += 1
            continue
        if char in "\n;":
            command_start = True
            index += 1
            continue
        if command_start and char == "#":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
            command_start = True
            continue
        line = text.count("\n", 0, index) + 1
        command, nested, index = _scan_tcl_word(text, index, terminator=terminator, require_literal=True, depth=depth)
        if not command:
            raise ValueError(f"line {line}: dynamic or empty Tcl command names are not allowed in trusted XDC")
        commands.append((depth, command, line))
        commands.extend(nested)
        command_start = False
        while index < len(text):
            char = text[index]
            if terminator and char == terminator:
                return commands, index + 1
            if char in "\n;":
                command_start = True
                index += 1
                break
            if char in " \t\r":
                index += 1
                continue
            _, nested, index = _scan_tcl_word(text, index, terminator=terminator, require_literal=False, depth=depth)
            commands.extend(nested)
    if terminator:
        raise ValueError("unterminated Tcl command substitution in XDC")
    return commands, index


def _scan_tcl_word(
    text: str,
    start: int,
    *,
    terminator: str | None,
    require_literal: bool,
    depth: int,
) -> tuple[str, list[tuple[int, str, int]], int]:
    index = start
    literal: list[str] = []
    nested: list[tuple[int, str, int]] = []
    dynamic = False
    while index < len(text):
        char = text[index]
        if char in " \t\r\n;" or (terminator and char == terminator):
            break
        if char == "{":
            dynamic = dynamic or require_literal
            index = _skip_braced_word(text, index + 1)
            continue
        if char == '"':
            dynamic = dynamic or require_literal
            quoted_nested, index = _scan_quoted_word(text, index + 1, depth=depth)
            nested.extend(quoted_nested)
            continue
        if char == "[":
            dynamic = dynamic or require_literal
            bracket_commands, index = _scan_tcl_script(text, index + 1, terminator="]", depth=depth + 1)
            nested.extend(bracket_commands)
            continue
        if char == "$":
            dynamic = dynamic or require_literal
            index += 1
            continue
        if char == "\\":
            if index + 1 >= len(text):
                raise ValueError("trailing Tcl escape in XDC")
            if text[index + 1] == "\n":
                index += 2
                while index < len(text) and text[index] in " \t":
                    index += 1
                continue
            literal.append(text[index + 1])
            index += 2
            continue
        if char == "]" and not terminator:
            raise ValueError("unexpected closing bracket in XDC")
        literal.append(char)
        index += 1
    return ("" if dynamic else "".join(literal)), nested, index


def _skip_braced_word(text: str, start: int) -> int:
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise ValueError("unterminated braced Tcl word in XDC")


def _scan_quoted_word(text: str, start: int, *, depth: int) -> tuple[list[tuple[int, str, int]], int]:
    nested: list[tuple[int, str, int]] = []
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            return nested, index + 1
        if char == "[":
            commands, index = _scan_tcl_script(text, index + 1, terminator="]", depth=depth + 1)
            nested.extend(commands)
            continue
        if char == "\\" and index + 1 < len(text):
            index += 2
            continue
        index += 1
    raise ValueError("unterminated quoted Tcl word in XDC")


def guarded_launch_run_command(*, run_name: str, to_step: str | None = None) -> str:
    launch = f"launch_runs {tcl_list_quote(run_name)}"
    if to_step:
        launch += f" -to_step {to_step}"
    # A hook on any project run can execute as a dependency or through a later
    # workflow step, so the launch boundary must attest the whole run set.
    return f"{run_hook_guard_command()}; {launch}"


def reset_runs_command(*, run_names: list[str] | None = None) -> str:
    selected = run_names or ["synth_1", "impl_1"]
    parts = [tcl_wire_prelude(), "set rows [list]"]
    for run_name in selected:
        run_ref = tcl_list_quote(run_name)
        parts.extend(
            [
                f"set runs [get_runs -quiet {run_ref}]",
                f"if {{[llength $runs] == 0}} {{error {tcl_list_quote(f'Vivado run not found: {run_name}')}}}",
                "set r [lindex $runs 0]",
                "reset_run [list $r]",
                "lappend rows [::vivado_agent_mcp_wire_row [list run [get_property NAME $r] status [get_property STATUS $r] needs_refresh [get_property NEEDS_REFRESH $r]]]",
            ]
        )
    parts.append("join $rows \"\\n\"")
    return "; ".join(parts)


def clean_run_outputs_command(
    *,
    run_names: list[str] | None = None,
    simsets: list[str] | None = None,
    include_cache: bool = False,
    include_gen: bool = False,
) -> str:
    selected_runs = ["synth_1", "impl_1"] if run_names is None else run_names
    selected_simsets = simsets or []
    for name in [*selected_runs, *selected_simsets]:
        validate_generated_child_name(name)
    parts = [
        tcl_wire_prelude(),
        "catch {close_sim}",
        "set p [current_project]",
        "set project_name [get_property NAME $p]",
        "set project_dir [file normalize [get_property DIRECTORY $p]]",
        "set runs_root [file normalize [file join $project_dir ${project_name}.runs]]",
        "set sim_root [file normalize [file join $project_dir ${project_name}.sim]]",
        "set cache_root [file normalize [file join $project_dir ${project_name}.cache]]",
        "set gen_root [file normalize [file join $project_dir ${project_name}.gen]]",
        "set targets [list]",
    ]
    for run_name in selected_runs:
        parts.append(f"lappend targets [file join $runs_root {tcl_list_quote(run_name)}]")
    for simset in selected_simsets:
        parts.append(f"lappend targets [file join $sim_root {tcl_list_quote(simset)}]")
    if include_cache:
        parts.append("lappend targets $cache_root")
    if include_gen:
        parts.append("lappend targets $gen_root")
    parts.extend(
        [
            "join [list "
            "\"project_name=$project_name\" "
            "\"project_dir=$project_dir\" "
            "\"targets=[::vivado_agent_mcp_wire_list $targets]\""
            "] \"\\n\"",
        ]
    )
    return "; ".join(parts)


def artifact_context_command(*, run_name: str) -> str:
    run_ref = tcl_list_quote(run_name)
    return (
        f"{tcl_wire_prelude()}; "
        "set p [current_project]; "
        "set project_dir [file normalize [get_property DIRECTORY $p]]; "
        "set project_name [get_property NAME $p]; "
        "set project_part [get_property PART $p]; "
        f"set runs [get_runs -quiet {run_ref}]; "
        f"if {{[llength $runs] != 1}} {{error {tcl_list_quote(f'Vivado run must resolve exactly once: {run_name}')}}}; "
        "set r [lindex $runs 0]; "
        "set run_dir [file normalize [get_property DIRECTORY $r]]; "
        "set run_srcset {}; catch {set run_srcset [get_property SRCSET $r]}; "
        "set run_top {}; if {$run_srcset ne {}} {catch {set run_top [get_property TOP [get_filesets $run_srcset]]}}; "
        "set expected_bitstream_path {}; if {$run_top ne {}} {set expected_bitstream_path [file normalize [file join $run_dir ${run_top}.bit]]}; "
        "set run_bitstream_files [list]; catch {set run_bitstream_files [glob -nocomplain -types f -directory $run_dir *.bit]}; "
        "set write_bitstream_step_enabled {}; catch {set write_bitstream_step_enabled [get_property STEPS.WRITE_BITSTREAM.IS_ENABLED $r]}; "
        "set write_bitstream_step_status {}; catch {set write_bitstream_step_status [get_property STEPS.WRITE_BITSTREAM.STATUS $r]}; "
        "join [list "
        "\"project_name=$project_name\" "
        "\"project_dir=$project_dir\" "
        "\"project_part=$project_part\" "
        "\"run_dir=$run_dir\" "
        "\"run_srcset=$run_srcset\" "
        "\"run_top=$run_top\" "
        "\"expected_bitstream_path=$expected_bitstream_path\" "
        "\"run_bitstream_files=[::vivado_agent_mcp_wire_list $run_bitstream_files]\" "
        "\"write_bitstream_step_enabled=$write_bitstream_step_enabled\" "
        "\"write_bitstream_step_status=$write_bitstream_step_status\" "
        "\"run_status=[get_property STATUS $r]\" "
        "\"run_progress=[get_property PROGRESS $r]\" "
        "\"run_needs_refresh=[get_property NEEDS_REFRESH $r]\""
        "] \"\\n\""
    )


def artifact_manifest_context_command(*, run_name: str) -> str:
    run_ref = tcl_list_quote(run_name)
    return (
        "set p [current_project]; "
        "set project_dir [get_property DIRECTORY $p]; "
        f"set run_name {run_ref}; "
        "set manifest_path [file join $project_dir vmcp_artifacts $run_name manifest.json]; "
        "set root_manifest_path [file join $project_dir vmcp_artifacts manifest.json]; "
        "if {![file exists $manifest_path] && [file exists $root_manifest_path]} {set manifest_path $root_manifest_path}; "
        "join [list "
        "\"manifest_path=$manifest_path\" "
        "\"project_dir=$project_dir\" "
        "\"run_name=$run_name\""
        "] \"\\n\""
    )


def parse_run_configuration(raw: str) -> dict[str, Any]:
    metadata, properties = _split_marker(raw, "properties_begin=__VMCP_RUN_PROPERTIES_BEGIN__")
    values = _parse_key_value_lines(metadata)
    return {"ok": True, "run": values, "properties_raw": properties.strip(), "raw": raw}


def parse_run_rows(raw: str) -> dict[str, Any]:
    return {"ok": True, "runs": [decode_wire_row(line) for line in raw.splitlines() if line.strip()], "raw": raw}


def parse_clean_outputs(raw: str) -> dict[str, Any]:
    values = _parse_key_value_lines(raw)
    return {
        "ok": True,
        "project_dir": values.get("project_dir", ""),
        "project_name": values.get("project_name", ""),
        "targets": decode_wire_list(values.get("targets", "")),
        "raw": raw,
    }


def validate_generated_clean_target(project_dir: str | Path, target: str | Path) -> Path:
    project = Path(project_dir).resolve()
    resolved = Path(target).resolve()
    try:
        relative = resolved.relative_to(project)
    except ValueError as exc:
        raise ValueError("Refusing to clean outside project directory") from exc
    if not relative.parts or not any(relative.parts[0].endswith(marker) for marker in GENERATED_OUTPUT_MARKERS):
        raise ValueError("Refusing to clean path that is not a Vivado generated output")
    root_name = relative.parts[0]
    if root_name.endswith((".runs", ".sim")) and len(relative.parts) != 2:
        raise ValueError("Refusing to clean anything other than a direct generated run or simulation child")
    if root_name.endswith((".cache", ".gen")) and len(relative.parts) != 1:
        raise ValueError("Refusing to clean a nested path below an exact generated cache root")
    return resolved


def validate_generated_child_name(value: str) -> None:
    if not value or value in {".", ".."} or "/" in value or "\\" in value or ":" in value:
        raise ValueError(f"Generated output name must be a single path component: {value!r}")


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
