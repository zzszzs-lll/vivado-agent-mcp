import json
import os
from pathlib import Path
from typing import Any

import pytest

import vivado_agent_mcp.tools as tools_module
from bitstream_fixture import write_test_bitstream, write_test_design_execution_identity
from vivado_agent_mcp.tools import VivadoToolService
from vivado_agent_mcp.vivado.artifacts import collect_artifacts, load_artifact_manifest, parse_bitstream_header
from vivado_agent_mcp.vivado.project import (
    add_project_files_command,
    compare_file_spec_inventories,
    file_spec_inventory_digest,
    list_fileset_files_command,
    normalize_project_file_specs,
    parse_fileset_files,
    parse_project_state,
    project_state_command,
    replay_file_specs_command,
    remove_project_files_command,
    set_project_part_command,
    set_project_top_command,
    update_compile_order_command,
)
from vivado_agent_mcp.vivado.runs import (
    clean_run_outputs_command,
    configure_run_command,
    guarded_launch_run_command,
    get_run_configuration_command,
    parse_project_execution_inputs,
    project_execution_inputs_command,
    reset_runs_command,
    run_hook_guard_command,
    validate_generated_clean_target,
    validate_xdc_text,
)
from vivado_agent_mcp.vivado.simulation import build_design_execution_identity, verify_design_execution_identity_files
from vivado_agent_mcp.vivado.wire import encode_wire_list, encode_wire_row


class FakeSession:
    def __init__(self, raw: str = "", ok: bool = True) -> None:
        self.commands: list[str] = []
        self.raw = raw
        self.ok = ok

    def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
        self.commands.append(command)
        return {"ok": self.ok, "raw": self.raw}


def test_project_state_parser_extracts_project_filesets_runs_and_artifacts() -> None:
    raw = f"""
project_name=demo
project_dir=D:/Vivado_Mcp/test_use/v060/project
part=xc7a35tcpg236-1
top=top
sim_top=tb_top
filesets={encode_wire_list(['sources_1', 'constrs_1', 'sim_1'])}
runs={encode_wire_list(['synth_1', 'impl_1'])}
bitstream_files={encode_wire_list(['D:/Vivado_Mcp/test_use/v060/project/demo.runs/impl_1/top.bit'])}
""".strip()

    result = parse_project_state(raw)

    assert result["project"]["name"] == "demo"
    assert result["project"]["top"] == "top"
    assert result["project"]["sim_top"] == "tb_top"
    assert result["filesets"] == ["sources_1", "constrs_1", "sim_1"]
    assert result["runs"] == ["synth_1", "impl_1"]
    assert result["artifacts"]["bitstream_files"] == [
        "D:/Vivado_Mcp/test_use/v060/project/demo.runs/impl_1/top.bit"
    ]


def test_project_state_parser_deduplicates_bitstream_files() -> None:
    bitstream = 'D:/Vivado_Mcp/test_use/demo/demo.runs/impl_1/top.bit'
    raw = f"""
project_name=demo
project_dir=D:/Vivado_Mcp/test_use/demo
part=xc7a35tcpg236-1
top=top
sim_top=tb_top
filesets={encode_wire_list(['sources_1', 'sim_1'])}
runs={encode_wire_list(['synth_1', 'impl_1'])}
bitstream_files={encode_wire_list([bitstream, bitstream])}
""".strip()

    result = parse_project_state(raw)

    assert result["artifacts"]["bitstream_files"] == [
        "D:/Vivado_Mcp/test_use/demo/demo.runs/impl_1/top.bit"
    ]


def test_project_state_parser_preserves_rebuild_fileset_properties() -> None:
    raw = "\n".join(
        [
            "project_name=demo",
            "project_dir=D:/demo",
            "part=xc7a35tcpg236-1",
            "top=top",
            "sim_top=tb_top",
            "target_language=Verilog",
            "target_simulator=Vivado Simulator",
            f"source_include_dirs={encode_wire_list(['D:/demo/rtl/include'])}",
            f"source_verilog_defines={encode_wire_list(['SYNTHESIS=1', 'FLAG_ONLY'])}",
            f"sim_include_dirs={encode_wire_list(['D:/demo/sim/include'])}",
            f"sim_verilog_defines={encode_wire_list(['SIMULATION=1'])}",
            f"fileset_property_errors={encode_wire_list([])}",
        ]
    )

    result = parse_project_state(raw)

    assert result["project"]["target_language"] == "Verilog"
    assert result["project"]["target_simulator"] == "Vivado Simulator"
    assert result["fileset_properties"] == {
        "discovery_status": "READY",
        "errors": [],
        "sources_1": {
            "include_dirs": ["D:/demo/rtl/include"],
            "defines": {"SYNTHESIS": "1", "FLAG_ONLY": None},
        },
        "sim_1": {
            "include_dirs": ["D:/demo/sim/include"],
            "defines": {"SIMULATION": "1"},
        },
    }


def test_project_state_parser_blocks_unreproducible_fileset_defines() -> None:
    raw = "\n".join(
        [
            f"source_verilog_defines={encode_wire_list(['BAD NAME=1'])}",
            f"sim_verilog_defines={encode_wire_list([])}",
            f"fileset_property_errors={encode_wire_list([])}",
        ]
    )

    result = parse_project_state(raw)

    assert result["fileset_properties"]["discovery_status"] == "BLOCK"
    assert "cannot be reproduced safely" in result["fileset_properties"]["errors"][0]


def test_fileset_commands_quote_paths_and_do_not_delete_source_files() -> None:
    source = r"D:\Vivado_Mcp\test_use\rtl dir\top$模块.v"

    add_command = add_project_files_command(fileset="sources_1", files=[source], copy_to_project=False)
    remove_command = remove_project_files_command(fileset="sources_1", files=[source])
    list_command = list_fileset_files_command(fileset="sources_1")

    assert "add_files -fileset {sources_1} [list {D:\\Vivado_Mcp\\test_use\\rtl dir\\top$模块.v}]" in add_command
    assert "import_files" not in add_command
    assert "remove_files [list {D:\\Vivado_Mcp\\test_use\\rtl dir\\top$模块.v}]" in remove_command
    assert "file delete" not in remove_command.lower()
    assert "get_files -quiet -of_objects $fs" in list_command
    assert "list_property $f" in list_command
    assert "IS_MANAGED" in list_command
    assert "IS_GLOBAL_INCLUDE" in list_command
    assert "USED_IN_SYNTHESIS" in list_command
    assert "compile_order $vmcp_compile_order" in list_command
    assert "PROCESSING_ORDER" in list_command
    assert "SCOPED_TO_REF" in list_command
    assert "lsearch -exact $bit_files" in project_state_command()
    assert "get_property INCLUDE_DIRS $source_fs" in project_state_command()
    assert "get_property VERILOG_DEFINE $sim_fs" in project_state_command()
    assert "fileset_property_errors=" in project_state_command()


def test_parse_fileset_files_returns_file_type_existence_and_managed_state() -> None:
    raw = "\n".join(
        [
            encode_wire_row(
                {
                    'path': 'D:/Vivado_Mcp/test_use/project/top.v',
                    'fileset': 'sources_1',
                    'type': 'Verilog',
                    'compile_order': '0',
                    'exists': '1',
                    'managed': '0',
                }
            ),
            encode_wire_row(
                {
                    'path': 'D:/Vivado_Mcp/test_use/project/top.xdc',
                    'fileset': 'sources_1',
                    'type': 'XDC',
                    'compile_order': '1',
                    'exists': '0',
                    'managed': '1',
                }
            ),
        ]
    )

    result = parse_fileset_files(raw)

    assert result["files"][0] == {
        "path": "D:/Vivado_Mcp/test_use/project/top.v",
        "file_type": "Verilog",
        "exists": True,
        "managed": False,
    }
    assert result["files"][1]["exists"] is False
    assert result["files"][1]["managed"] is True
    assert result["reconstruction_status"] == "READY"


def test_fileset_semantic_inventory_preserves_allowlisted_vivado_properties() -> None:
    raw = "\n".join(
        [
            encode_wire_row(
                {
                    "path": "D:/workspace/rtl/top.v",
                    "fileset": "sources_1",
                    "type": "SystemVerilog",
                    "library": "work_lib",
                    "compile_order": "0",
                    "exists": "1",
                    "managed": "0",
                    "is_global_include": "0",
                    "used_in_synthesis": "1",
                    "used_in_implementation": "1",
                    "used_in_simulation": "1",
                    "processing_order": "",
                    "scoped_to_ref": "",
                    "scoped_to_cells": encode_wire_list([]),
                }
            ),
            encode_wire_row({"vmcp_meta": "1", "discovery_errors": encode_wire_list([])}),
        ]
    )

    result = parse_fileset_files(raw, fileset="sources_1")

    assert result["reconstruction_status"] == "READY"
    assert len(result["semantic_inventory_digest"]) == 64
    assert result["file_specs"] == [
        {
            "path": "D:/workspace/rtl/top.v",
            "fileset": "sources_1",
            "file_type": "SystemVerilog",
            "library": "work_lib",
            "compile_order": 0,
            "is_global_include": False,
            "used_in_synthesis": True,
            "used_in_implementation": True,
            "used_in_simulation": True,
            "processing_order": "",
            "scoped_to_ref": "",
            "scoped_to_cells": [],
        }
    ]


def test_fileset_semantic_inventory_blocks_invalid_optional_boolean_values() -> None:
    raw = "\n".join(
        [
            encode_wire_row(
                {
                    "path": "D:/workspace/rtl/top.sv",
                    "fileset": "sources_1",
                    "type": "SystemVerilog",
                    "library": "xil_defaultlib",
                    "compile_order": "0",
                    "exists": "1",
                    "managed": "0",
                    "is_global_include": "maybe",
                    "used_in_synthesis": "1",
                    "used_in_implementation": "1",
                    "used_in_simulation": "1",
                    "processing_order": "",
                    "scoped_to_ref": "",
                    "scoped_to_cells": encode_wire_list([]),
                }
            ),
            encode_wire_row({"vmcp_meta": "1", "discovery_errors": encode_wire_list([])}),
        ]
    )

    result = parse_fileset_files(raw, fileset="sources_1")

    assert result["reconstruction_status"] == "BLOCK"
    assert result["semantic_inventory_digest"] == ""
    assert "invalid Vivado boolean value" in result["discovery_errors"][0]


def test_file_semantics_replay_covers_vhdl_xdc_and_global_include() -> None:
    specs = [
        {
            "path": "D:/workspace/rtl/pkg.vhd",
            "fileset": "sources_1",
            "file_type": "VHDL 2008",
            "library": "design_lib",
            "compile_order": 0,
            "is_global_include": False,
            "used_in_synthesis": True,
            "used_in_implementation": True,
            "used_in_simulation": True,
            "processing_order": "",
            "scoped_to_ref": "",
            "scoped_to_cells": [],
        },
        {
            "path": "D:/workspace/rtl/defs.svh",
            "fileset": "sources_1",
            "file_type": "Verilog Header",
            "library": "xil_defaultlib",
            "compile_order": 1,
            "is_global_include": True,
            "used_in_synthesis": True,
            "used_in_implementation": True,
            "used_in_simulation": True,
            "processing_order": "",
            "scoped_to_ref": "",
            "scoped_to_cells": [],
        },
        {
            "path": "D:/workspace/xdc/late.xdc",
            "fileset": "constrs_1",
            "file_type": "XDC",
            "library": "",
            "compile_order": 0,
            "is_global_include": None,
            "used_in_synthesis": True,
            "used_in_implementation": True,
            "used_in_simulation": False,
            "processing_order": "LATE",
            "scoped_to_ref": "core",
            "scoped_to_cells": ["u_core"],
        },
    ]

    normalized, errors = normalize_project_file_specs(specs)
    command = replay_file_specs_command(normalized)

    assert errors == []
    assert "set_property FILE_TYPE {VHDL 2008}" in command
    assert "set_property LIBRARY {design_lib}" in command
    assert "set_property FILE_TYPE {Verilog Header}" in command
    assert "set_property IS_GLOBAL_INCLUDE {1}" in command
    assert "set_property PROCESSING_ORDER {LATE}" in command
    assert "set_property SCOPED_TO_REF {core}" in command
    assert "set_property SCOPED_TO_CELLS [list {u_core}]" in command
    assert "update_compile_order -fileset {sources_1}" in command
    assert "reorder_files -fileset {sources_1} -front [list {D:/workspace/rtl/pkg.vhd} {D:/workspace/rtl/defs.svh}]" in command


def test_file_semantics_comparison_detects_post_create_drift() -> None:
    expected = [
        {
            "path": "D:/workspace/rtl/top.v",
            "fileset": "sources_1",
            "file_type": "SystemVerilog",
            "library": "xil_defaultlib",
            "compile_order": 0,
            "is_global_include": False,
            "used_in_synthesis": True,
            "used_in_implementation": True,
            "used_in_simulation": True,
            "processing_order": "",
            "scoped_to_ref": "",
            "scoped_to_cells": [],
        }
    ]
    actual = [{**expected[0], "file_type": "Verilog"}]

    comparison = compare_file_spec_inventories(expected, actual)

    assert comparison["matches"] is False
    assert comparison["changed"][0]["differences"]["file_type"] == {
        "expected": "SystemVerilog",
        "actual": "Verilog",
    }
    assert file_spec_inventory_digest(expected) != file_spec_inventory_digest(actual)


def test_file_semantics_comparison_detects_compile_order_drift() -> None:
    first = {
        "path": "D:/workspace/rtl/pkg.sv",
        "fileset": "sources_1",
        "file_type": "SystemVerilog",
        "library": "xil_defaultlib",
        "compile_order": 0,
        "is_global_include": False,
        "used_in_synthesis": True,
        "used_in_implementation": True,
        "used_in_simulation": True,
        "processing_order": "",
        "scoped_to_ref": "",
        "scoped_to_cells": [],
    }
    second = {**first, "path": "D:/workspace/rtl/top.sv", "compile_order": 1}
    actual = [{**first, "compile_order": 1}, {**second, "compile_order": 0}]

    comparison = compare_file_spec_inventories([first, second], actual)

    assert comparison["matches"] is False
    assert all("compile_order" in item["differences"] for item in comparison["changed"])
    assert file_spec_inventory_digest([first, second]) != file_spec_inventory_digest(actual)


def test_file_semantics_rejects_duplicate_or_gapped_compile_order() -> None:
    base = {
        "path": "D:/workspace/rtl/pkg.sv",
        "fileset": "sources_1",
        "file_type": "SystemVerilog",
        "library": "xil_defaultlib",
        "compile_order": 0,
        "is_global_include": False,
        "used_in_synthesis": True,
        "used_in_implementation": True,
        "used_in_simulation": True,
        "processing_order": "",
        "scoped_to_ref": "",
        "scoped_to_cells": [],
    }

    _, duplicate_errors = normalize_project_file_specs(
        [base, {**base, "path": "D:/workspace/rtl/top.sv"}]
    )
    _, gapped_errors = normalize_project_file_specs(
        [base, {**base, "path": "D:/workspace/rtl/top.sv", "compile_order": 2}]
    )

    assert any("unique and contiguous" in error for error in duplicate_errors)
    assert any("unique and contiguous" in error for error in gapped_errors)


def test_top_part_and_compile_order_commands_cover_design_and_sim_filesets() -> None:
    design_top = set_project_top_command(top="top", fileset="sources_1")
    sim_top = set_project_top_command(top="tb_top", fileset="sim_1")
    part = set_project_part_command(part="xc7a35tcpg236-1")
    compile_order = update_compile_order_command(filesets=["sources_1", "sim_1"])

    assert "set_property top {top} [get_filesets {sources_1}]" in design_top
    assert "update_compile_order -fileset {sources_1}" in design_top
    assert "set_property top {tb_top} [get_filesets {sim_1}]" in sim_top
    assert "update_compile_order -fileset {sim_1}" in sim_top
    assert "set_property part {xc7a35tcpg236-1} [current_project]" in part
    assert "::vivado_agent_mcp_wire_row" in part
    assert "needs_refresh [get_property NEEDS_REFRESH $r]" in part
    assert "linsert $rows 0" in part
    assert "update_compile_order -fileset {sources_1}" in compile_order
    assert "update_compile_order -fileset {sim_1}" in compile_order


def test_run_configuration_commands_validate_run_and_set_strategy_properties() -> None:
    get_command = get_run_configuration_command(run_name="impl_1")
    configure_command = configure_run_command(
        run_name="impl_1",
        strategy="Performance_Explore",
        properties={"STEPS.OPT_DESIGN.ARGS.DIRECTIVE": "Explore", "IS_ENABLED": True},
    )
    reset_command = reset_runs_command(run_names=["synth_1", "impl_1"])

    assert "get_runs -quiet {impl_1}" in get_command
    assert "error {Vivado run not found: impl_1}" in get_command
    assert "set_property strategy {Performance_Explore} $r" in configure_command
    assert "set_property {STEPS.OPT_DESIGN.ARGS.DIRECTIVE} {Explore} $r" in configure_command
    assert "set_property {IS_ENABLED} {True} $r" in configure_command
    assert "reset_run [list $r]" in reset_command
    assert "get_runs -quiet {synth_1}" in reset_command


def test_configure_run_command_rejects_executable_hook_and_unknown_properties() -> None:
    for properties in (
        {"STEPS.SYNTH_DESIGN.TCL.PRE": "hook.tcl"},
        {"STEPS.OPT_DESIGN.TCL.POST": "hook.tcl"},
        {"STEPS.ROUTE_DESIGN.TCL.PRE": "hook.tcl"},
        {"USER_SCRIPT": "hook.tcl"},
        {"UNREVIEWED_PROPERTY": "value"},
        {"STEPS.UNKNOWN_STEP.ARGS.UNKNOWN_ARGUMENT": "value"},
        {"STEPS.SYNTH_DESIGN.ARGS.MORE_OPTIONS": "-some-future-option"},
        {"STEPS.UNKNOWN_STEP.IS_ENABLED": True},
        {" steps.synth_design.args.flatten_hierarchy ": "none"},
    ):
        with pytest.raises(ValueError):
            configure_run_command(run_name="synth_1", properties=properties)


def test_run_hook_guard_checks_all_runs_without_misclassifying_normal_source_properties() -> None:
    guard = run_hook_guard_command(close_project_on_block=True)
    launch = guarded_launch_run_command(run_name="impl_1", to_step="write_bitstream")

    assert "get_runs]" in guard
    assert "string equal {2021.2}" in guard
    assert "(TCL|HOOK|SCRIPT)" in guard
    assert "*SCRIPT*" not in guard
    assert "SOURCE_FILE" not in guard and "*.SOURCE" not in guard
    assert "catch {close_project}" in guard
    assert "VMCP_EXECUTABLE_CONSTRAINT_INPUT_BLOCKED" in guard
    assert "FILE_TYPE" in guard
    assert "file extension" in guard
    assert "get_runs]" in launch
    assert launch.index("VMCP_RUN_HOOK_BLOCKED") < launch.index("launch_runs {impl_1}")
    assert launch.index("VMCP_EXECUTABLE_CONSTRAINT_INPUT_BLOCKED") < launch.index("launch_runs {impl_1}")


def test_run_hook_guard_treats_critical_discovery_errors_as_blocking() -> None:
    command = run_hook_guard_command()

    assert "VMCP_EXECUTABLE_INPUT_DISCOVERY_FAILED" in command
    assert "vmcp_guard_discovery_errors" in command
    assert "get_ips" in command
    assert ".xci .bd .dcp" in command
    assert "get_bd_designs" not in command
    assert command.index("VMCP_EXECUTABLE_INPUT_DISCOVERY_FAILED") < command.index("VMCP_EXECUTABLE_COMPOSITE_INPUT_BLOCKED")


def test_execution_input_parser_blocks_incomplete_wire_sections() -> None:
    parsed = parse_project_execution_inputs("vivado_version_short=2021.2\nproject_dir=D:/demo")

    assert parsed["wire_format_complete"] is False
    assert parsed["discovery_errors"]


def test_project_execution_input_preflight_enumerates_constraint_identity() -> None:
    command = project_execution_inputs_command()
    raw = (
        "vivado_version_short=2021.2\n"
        "vivado_version_full=Vivado v2021.2\n"
        "project_dir=D:/project\n"
        "project_name=demo\n"
        "project_path=D:/project/demo.xpr\n"
        "part=xc7a35tcpg236-1\n"
        "top=top\n"
        "include_dirs=\n"
        "verilog_defines=\n"
        "sources_begin=__VMCP_SOURCE_INPUTS_BEGIN__\n"
        + encode_wire_row({"used_in": "implementation", "order": "0", "path": "D:/project/top.sv", "type": "SystemVerilog", "library": "xil_defaultlib"})
        + "\n"
        "constraints_begin=__VMCP_CONSTRAINT_INPUTS_BEGIN__\n"
        + encode_wire_row({"used_in": "implementation", "path": "D:/project/top.xdc", "type": "XDC"})
        + "\nrun_configurations_begin=__VMCP_RUN_CONFIGURATIONS_BEGIN__\n"
        + "\ncomposite_inputs_begin=__VMCP_COMPOSITE_INPUTS_BEGIN__\n"
        + "discovery_errors_begin=__VMCP_EXECUTION_INPUT_ERRORS_BEGIN__"
    )

    parsed = parse_project_execution_inputs(raw)

    assert "version -short" in command
    assert "get_files -compile_order sources -used_in" in command
    assert "get_files -quiet -of_objects $vmcp_sources_fileset" in command
    assert "get_property USED_IN $vmcp_source" in command
    assert "inventory_scope fileset" in command
    assert "get_files -compile_order constraints -used_in" in command
    assert "get_ips *" in command
    assert ".xci .bd .dcp" in command
    assert "get_bd_designs *" not in command
    assert parsed["vivado_version_short"] == "2021.2"
    assert parsed["constraints"] == [
        {"used_in": "implementation", "path": "D:/project/top.xdc", "type": "XDC"}
    ]
    assert parsed["composite_inputs"] == []
    assert parsed["discovery_errors"] == []


def test_project_execution_input_parser_exposes_composite_inputs_and_discovery_failures() -> None:
    raw = (
        "vivado_version_short=2021.2\n"
        "vivado_version_full=Vivado v2021.2\n"
        "project_dir=D:/project\n"
        "project_name=demo\n"
        "project_path=D:/project/demo.xpr\n"
        "part=xc7a35tcpg236-1\n"
        "top=top\n"
        "include_dirs=\n"
        "verilog_defines=\n"
        "sources_begin=__VMCP_SOURCE_INPUTS_BEGIN__\n"
        + encode_wire_row({"used_in": "implementation", "order": "0", "path": "D:/project/top.sv", "type": "SystemVerilog", "library": "xil_defaultlib"})
        + "\n"
        "constraints_begin=__VMCP_CONSTRAINT_INPUTS_BEGIN__\n"
        "run_configurations_begin=__VMCP_RUN_CONFIGURATIONS_BEGIN__\n"
        "composite_inputs_begin=__VMCP_COMPOSITE_INPUTS_BEGIN__\n"
        + encode_wire_row({"kind": "ip", "value": "clk_wiz_0"})
        + "\ndiscovery_errors_begin=__VMCP_EXECUTION_INPUT_ERRORS_BEGIN__\n"
        + encode_wire_row({"error": "constraints implementation query failed"})
    )

    parsed = parse_project_execution_inputs(raw)

    assert parsed["composite_inputs"] == [{"kind": "ip", "value": "clk_wiz_0"}]
    assert parsed["discovery_errors"] == [{"error": "constraints implementation query failed"}]


def test_design_execution_identity_binds_rtl_xdc_include_and_run_configuration(tmp_path: Path) -> None:
    project = tmp_path / "demo.xpr"
    rtl = tmp_path / "top.sv"
    header = tmp_path / "defs.svh"
    xdc = tmp_path / "top.xdc"
    project.write_text("# demo\n", encoding="utf-8")
    header.write_text("localparam int WIDTH = 8;\n", encoding="utf-8")
    rtl.write_text('`include "defs.svh"\nmodule top; endmodule\n', encoding="utf-8")
    xdc.write_text("create_clock -period 10 [get_ports clk]\n", encoding="utf-8")
    preflight = {
        "wire_format_complete": True,
        "vivado_version_short": "2021.2",
        "vivado_version_full": "Vivado v2021.2",
        "project_dir": str(tmp_path),
        "project_name": "demo",
        "project_path": str(project),
        "part": "xc7a35tcpg236-1",
        "top": "top",
        "include_dirs": [],
        "verilog_defines": ["FEATURE=1"],
        "sources": [{"used_in": "synthesis", "order": "0", "path": str(rtl), "type": "SystemVerilog", "library": "xil_defaultlib"}],
        "constraints": [{"used_in": "implementation", "order": "0", "path": str(xdc), "type": "XDC"}],
        "run_configurations": [{"run": "impl_1", "property": "STRATEGY", "value": "Vivado Implementation Defaults"}],
        "composite_inputs": [],
        "discovery_errors": [],
    }

    identity = build_design_execution_identity(preflight)

    assert identity["status"] == "READY"
    assert len(identity["sha256"]) == 64
    assert {Path(item["path"]).name for item in identity["identity"]["files"]} == {"top.sv", "defs.svh", "top.xdc"}
    assert identity["identity"]["run_configurations"][0]["property"] == "STRATEGY"
    assert verify_design_execution_identity_files(identity) == []

    rtl.write_text('`include "defs.svh"\nmodule top; wire changed; endmodule\n', encoding="utf-8")
    assert verify_design_execution_identity_files(identity)
    assert build_design_execution_identity(preflight)["sha256"] != identity["sha256"]
    rtl.write_text('`include "defs.svh"\nmodule top; endmodule\n', encoding="utf-8")

    header.write_text("localparam int WIDTH = 16;\n", encoding="utf-8")
    assert verify_design_execution_identity_files(identity)
    header.write_text("localparam int WIDTH = 8;\n", encoding="utf-8")

    xdc.write_text("create_clock -period 8 [get_ports clk]\n", encoding="utf-8")
    assert verify_design_execution_identity_files(identity)
    xdc.write_text("create_clock -period 10 [get_ports clk]\n", encoding="utf-8")

    assert build_design_execution_identity({**preflight, "verilog_defines": ["FEATURE=2"]})["sha256"] != identity["sha256"]
    assert build_design_execution_identity({**preflight, "include_dirs": [str(tmp_path)]})["sha256"] != identity["sha256"]
    reordered = {**preflight, "sources": [{**preflight["sources"][0], "order": "1"}]}
    assert build_design_execution_identity(reordered)["sha256"] != identity["sha256"]
    reconfigured = {
        **preflight,
        "run_configurations": [{"run": "impl_1", "property": "STRATEGY", "value": "Performance_Explore"}],
    }
    assert build_design_execution_identity(reconfigured)["sha256"] != identity["sha256"]


def test_design_execution_identity_accepts_and_hashes_unambiguous_include_dir_literal(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    include_dir = tmp_path / "include"
    source_dir.mkdir()
    include_dir.mkdir()
    project = tmp_path / "demo.xpr"
    rtl = source_dir / "top.sv"
    header = include_dir / "shared_defs.svh"
    xdc = tmp_path / "top.xdc"
    project.write_text("# demo\n", encoding="utf-8")
    rtl.write_text('`include "shared_defs.svh"\nmodule top; endmodule\n', encoding="utf-8")
    header.write_text("localparam int WIDTH = 8;\n", encoding="utf-8")
    xdc.write_text("create_clock -period 10 [get_ports clk]\n", encoding="utf-8")
    preflight = {
        "wire_format_complete": True,
        "vivado_version_short": "2021.2",
        "vivado_version_full": "Vivado v2021.2",
        "project_dir": str(tmp_path),
        "project_name": "demo",
        "project_path": str(project),
        "part": "xc7a35tcpg236-1",
        "top": "top",
        "include_dirs": [str(include_dir)],
        "verilog_defines": [],
        "sources": [{"used_in": "synthesis", "order": "0", "path": str(rtl), "type": "SystemVerilog", "library": "xil_defaultlib"}],
        "constraints": [{"used_in": "implementation", "order": "0", "path": str(xdc), "type": "XDC"}],
        "run_configurations": [],
        "composite_inputs": [],
        "discovery_errors": [],
    }

    identity = build_design_execution_identity(preflight)

    assert identity["status"] == "READY"
    assert str(header.resolve()) in identity["identity"]["include_files"]
    assert any(item["path"] == str(header.resolve()) and item["source_kind"] == "include" for item in identity["identity"]["files"])


def test_design_execution_identity_accepts_vhdl_compile_order_sources(tmp_path: Path) -> None:
    project = tmp_path / "demo.xpr"
    rtl = tmp_path / "top.vhd"
    xdc = tmp_path / "top.xdc"
    project.write_text("# demo\n", encoding="utf-8")
    rtl.write_text(
        "library ieee; use ieee.std_logic_1164.all; entity top is end; architecture rtl of top is begin end;\n",
        encoding="utf-8",
    )
    xdc.write_text("create_clock -period 10 [get_ports clk]\n", encoding="utf-8")
    preflight = {
        "wire_format_complete": True,
        "vivado_version_short": "2021.2",
        "vivado_version_full": "Vivado v2021.2",
        "project_dir": str(tmp_path),
        "project_name": "demo",
        "project_path": str(project),
        "part": "xc7a35tcpg236-1",
        "top": "top",
        "include_dirs": [],
        "verilog_defines": [],
        "sources": [{"used_in": "synthesis", "order": "0", "path": str(rtl), "type": "VHDL", "library": "xil_defaultlib"}],
        "constraints": [{"used_in": "implementation", "order": "0", "path": str(xdc), "type": "XDC"}],
        "run_configurations": [],
        "composite_inputs": [],
        "discovery_errors": [],
    }

    identity = build_design_execution_identity(preflight)

    assert identity["status"] == "READY"
    assert any(item["path"] == str(rtl.resolve()) and item["source_kind"] == "compile_source" for item in identity["identity"]["files"])
    rtl.write_text(rtl.read_text(encoding="utf-8") + "-- changed\n", encoding="utf-8")
    assert verify_design_execution_identity_files(identity)


def test_trusted_xdc_parser_accepts_declarative_constraints_and_braced_data() -> None:
    content = (
        "# reviewed constraints\n"
        "set_property {PACKAGE_PIN} {U16} [get_ports {led[0]}]\n"
        "set_property {USER_NOTE} {text; exec is data here} [current_design]\n"
        "create_clock -period {10.0} [get_ports {clk}]\n"
    )

    assert validate_xdc_text(content) == []


@pytest.mark.parametrize(
    "content",
    [
        "exec cmd.exe /c echo pwned\n",
        "source malicious.tcl\n",
        "set_property PACKAGE_PIN U16 [exec cmd.exe]\n",
        "set_property PACKAGE_PIN U16 [get_ports clk]; exec cmd.exe\n",
        "$dynamic_command cmd.exe\n",
        "set_property PACKAGE_PIN U16 [get_ports clk; open victim.txt w]\n",
    ],
)
def test_trusted_xdc_parser_rejects_host_effect_and_dynamic_commands(content: str) -> None:
    assert validate_xdc_text(content)


def test_project_and_run_commands_do_not_reinsert_untrusted_values_into_tcl_quotes() -> None:
    payload = 'evil[exec calc]"; file delete -force -- C:/'
    commands = [
        add_project_files_command(fileset=payload, files=[]),
        remove_project_files_command(fileset=payload, files=[]),
        set_project_top_command(top=payload, fileset=payload),
        set_project_part_command(part=payload),
        update_compile_order_command(filesets=[payload]),
        get_run_configuration_command(run_name=payload),
        configure_run_command(run_name=payload, strategy=payload),
    ]

    for command in commands:
        assert f'"fileset={payload}' not in command
        assert f'"top={payload}' not in command
        assert f'"part={payload}' not in command
        assert f'"Vivado run not found: {payload}' not in command
        assert f"{{{payload}}}" in command


def test_clean_run_outputs_command_only_reports_project_generated_targets() -> None:
    command = clean_run_outputs_command(
        run_names=["synth_1"],
        simsets=["sim_1"],
        include_cache=True,
        include_gen=True,
    )

    assert "set project_dir [file normalize [get_property DIRECTORY $p]]" in command
    assert "set runs_root [file normalize [file join $project_dir ${project_name}.runs]]" in command
    assert "set sim_root [file normalize [file join $project_dir ${project_name}.sim]]" in command
    assert "set cache_root [file normalize [file join $project_dir ${project_name}.cache]]" in command
    assert "set gen_root [file normalize [file join $project_dir ${project_name}.gen]]" in command
    assert "targets=[::vivado_agent_mcp_wire_list $targets]" in command
    assert "file delete" not in command
    assert "open " not in command
    assert "remove_files" not in command


def test_clean_run_outputs_command_can_target_only_simsets() -> None:
    command = clean_run_outputs_command(run_names=[], simsets=["sim_1"])

    assert "lappend targets [file join $sim_root {sim_1}]" in command
    assert "lappend targets [file join $runs_root" not in command


def test_clean_run_outputs_apply_uses_managed_snapshot_broker_not_tcl_delete(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_target = project_dir / "demo.runs" / "synth_1"
    sim_target = project_dir / "demo.sim" / "sim_1"
    run_target.mkdir(parents=True)
    sim_target.mkdir(parents=True)
    (run_target / "runme.log").write_text("generated", encoding="utf-8")
    (sim_target / "xsim.log").write_text("generated", encoding="utf-8")
    raw = (
        "project_name=demo\n"
        f"project_dir={project_dir}\n"
        f"targets={encode_wire_list([str(run_target), str(sim_target)])}"
    )
    fake = FakeSession(raw=raw)

    result = VivadoToolService(session=fake).call(
        "clean_run_outputs",
        {
            "run_names": ["synth_1"],
            "simsets": ["sim_1"],
            "dry_run": False,
            "intent": "remove reviewed generated outputs",
            "confirm": "CLEAN_RUN_OUTPUTS",
        },
    )

    assert result["ok"] is True
    assert result["data"]["deletion_backend"] == "python_managed_snapshot_broker"
    assert result["data"]["deleted_file_count"] == 2
    assert not run_target.exists()
    assert not sim_target.exists()
    assert "file delete" not in fake.commands[0]


def test_validate_generated_clean_target_rejects_outside_paths_and_user_sources(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    allowed = project_dir / "demo.runs" / "synth_1"
    source_file = project_dir / "rtl" / "top.v"
    outside = tmp_path / "outside" / "demo.runs" / "synth_1"

    allowed.mkdir(parents=True)
    source_file.parent.mkdir(parents=True)
    source_file.write_text("module top; endmodule", encoding="utf-8")
    outside.mkdir(parents=True)

    assert validate_generated_clean_target(project_dir, allowed) == allowed.resolve()
    with pytest.raises(ValueError, match="outside project directory"):
        validate_generated_clean_target(project_dir, outside)
    with pytest.raises(ValueError, match="not a Vivado generated output"):
        validate_generated_clean_target(project_dir, source_file)


def test_clean_run_outputs_rejects_parent_traversal_and_user_named_generated_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="single path component"):
        clean_run_outputs_command(run_names=["../rtl"])

    project_dir = tmp_path / "project"
    user_dir = project_dir / "rtl" / "user.runs"
    user_dir.mkdir(parents=True)
    with pytest.raises(ValueError, match="not a Vivado generated output"):
        validate_generated_clean_target(project_dir, user_dir)

    nested_run_path = project_dir / "demo.runs" / "synth_1" / "user_data"
    nested_run_path.mkdir(parents=True)
    with pytest.raises(ValueError, match="direct generated run or simulation child"):
        validate_generated_clean_target(project_dir, nested_run_path)

    nested_cache_path = project_dir / "demo.cache" / "user_data"
    nested_cache_path.mkdir(parents=True)
    with pytest.raises(ValueError, match="nested path below an exact generated cache root"):
        validate_generated_clean_target(project_dir, nested_cache_path)


def test_collect_artifacts_copies_expected_outputs_and_writes_manifest(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    expected = {
        "top.bit": "bitstream",
        "top.ltx": "debug_probes",
        "top.dcp": "checkpoint",
        "timing.rpt": "report",
        "timing.rpx": "report",
        "vivado.pb": "vivado_metadata",
    }
    for name in expected:
        if name.endswith(".bit"):
            write_test_bitstream(run_dir / name)
        else:
            (run_dir / name).write_text(f"content for {name}", encoding="utf-8")
    (run_dir / "ignored.txt").write_text("ignore me", encoding="utf-8")

    manifest = collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        run_context=_fresh_artifact_context(project_dir, run_dir),
    )

    manifest_path = Path(manifest["manifest_path"])
    assert manifest_path == project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json"
    assert manifest_path.exists()
    assert len(manifest["artifacts"]) == len(expected)
    by_name = {Path(item["source_path"]).name: item for item in manifest["artifacts"]}
    assert set(by_name) == set(expected)
    for name, category in expected.items():
        assert by_name[name]["category"] == category
        assert Path(by_name[name]["export_path"]).exists()
        assert len(by_name[name]["sha256"]) == 64
    assert not (project_dir / "vmcp_artifacts" / "impl_1" / "ignored.txt").exists()
    assert load_artifact_manifest(manifest_path)["artifacts"][0]["sha256"]
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["run_name"] == "impl_1"
    bitstream = next(item for item in manifest["artifacts"] if item["category"] == "bitstream")
    assert bitstream["bitstream_header"]["design"] == "top"
    assert bitstream["bitstream_header"]["part"] == "7a35tcpg236"
    assert manifest["bitstream_origin"]["status"] == "ATTESTED"


def test_parse_bitstream_header_rejects_truncation_and_reports_identity(tmp_path: Path) -> None:
    valid = write_test_bitstream(tmp_path / "top.bit")
    header = parse_bitstream_header(valid)

    assert header["format"] == "xilinx_bit_v1"
    assert header["payload_offset"] + header["payload_size"] == header["file_size"]

    truncated = tmp_path / "truncated.bit"
    truncated.write_bytes(valid.read_bytes()[:-1])
    with pytest.raises(ValueError, match="payload length"):
        parse_bitstream_header(truncated)


@pytest.mark.parametrize(
    ("design", "part", "reason"),
    [
        ("other_top", "xc7a35tcpg236-1", "design does not match"),
        ("top", "xc7z020clg400-1", "part does not match"),
    ],
)
def test_collect_artifacts_rejects_bitstream_header_mismatch(
    tmp_path: Path,
    design: str,
    part: str,
    reason: str,
) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    write_test_bitstream(run_dir / "top.bit", design=design, part=part)

    manifest = collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        run_context=_fresh_artifact_context(project_dir, run_dir),
    )

    assert manifest["status"] == "BLOCK"
    assert any(reason in item for item in manifest["evidence_freshness"]["reasons"])


def test_collect_artifacts_rejects_extra_noncanonical_bitstream(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    write_test_bitstream(run_dir / "top.bit")
    (run_dir / "forged.bit").write_text("forged bitstream", encoding="utf-8")

    manifest = collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        run_context=_fresh_artifact_context(project_dir, run_dir),
    )

    assert manifest["status"] == "BLOCK"
    assert "target run must contain exactly one canonical top-named bitstream" in manifest["evidence_freshness"]["reasons"]
    bitstreams = [item for item in manifest["artifacts"] if item["category"] == "bitstream"]
    assert [Path(item["source_path"]).name for item in bitstreams] == ["top.bit"]
    assert not (project_dir / "vmcp_artifacts" / "impl_1" / "forged.bit").exists()


def test_collect_artifacts_rejects_run_or_output_dirs_outside_project(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    outside_run = tmp_path / "outside.runs" / "impl_1"
    outside_output = tmp_path / "outside_artifacts"
    run_dir.mkdir(parents=True)
    outside_run.mkdir(parents=True)
    (run_dir / "top.bit").write_text("bitstream", encoding="utf-8")
    (outside_run / "top.bit").write_text("bitstream", encoding="utf-8")

    with pytest.raises(ValueError, match="write outside project directory"):
        collect_artifacts(project_dir=project_dir, run_dir=run_dir, run_name="impl_1", output_dir=outside_output)
    with pytest.raises(ValueError, match="read outside project directory"):
        collect_artifacts(project_dir=project_dir, run_dir=outside_run, run_name="impl_1")
    assert not outside_output.exists()


def test_collect_artifacts_does_not_reissue_old_bitstream_for_new_run_markers(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    bitstream = run_dir / "top.bit"
    bitstream.write_text("old bitstream", encoding="utf-8")
    begin_marker = run_dir / ".vivado.begin.rst"
    end_marker = run_dir / ".vivado.end.rst"
    begin_marker.write_text("new run started", encoding="utf-8")
    end_marker.write_text("new run completed", encoding="utf-8")
    new_run_time = bitstream.stat().st_mtime_ns + 2_000_000_000
    os.utime(begin_marker, ns=(new_run_time, new_run_time))
    os.utime(end_marker, ns=(new_run_time + 1_000_000_000, new_run_time + 1_000_000_000))

    manifest = collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        run_context={
            **_fresh_artifact_context(project_dir, run_dir),
            "write_bitstream_step_enabled": "",
            "run_status": "write_bitstream Complete!",
            "run_progress": "100%",
            "run_needs_refresh": "0",
            "session_generation_id": "generation-a",
        },
    )

    assert manifest["status"] == "BLOCK"
    assert manifest["evidence_freshness"]["status"] == "STALE"
    assert any("predates current run launch marker" in reason for reason in manifest["evidence_freshness"]["reasons"])


def test_collect_artifacts_uses_step_markers_for_vivado_bitstream_continuation(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    route_report = run_dir / "top_timing_summary_routed.rpt"
    bitstream = run_dir / "top.bit"
    metadata = run_dir / "write_bitstream.pb"
    route_report.write_text(route_report.name, encoding="utf-8")
    write_test_bitstream(bitstream)
    metadata.write_text(metadata.name, encoding="utf-8")

    base_ns = max(path.stat().st_mtime_ns for path in (route_report, bitstream, metadata)) + 10_000_000_000
    marker_times = {
        ".init_design.begin.rst": base_ns,
        ".init_design.end.rst": base_ns + 1_000_000_000,
        ".route_design.begin.rst": base_ns + 2_000_000_000,
        ".route_design.end.rst": base_ns + 3_000_000_000,
        ".write_bitstream.begin.rst": base_ns + 4_000_000_000,
        ".write_bitstream.end.rst": base_ns + 6_000_000_000,
        # Vivado 2021.2 updates the broad begin marker for a continuation but can leave the broad end marker stale.
        ".vivado.begin.rst": base_ns + 7_000_000_000,
        ".vivado.end.rst": base_ns + 3_000_000_000,
    }
    for name, timestamp_ns in marker_times.items():
        marker = run_dir / name
        marker.write_text(name, encoding="utf-8")
        os.utime(marker, ns=(timestamp_ns, timestamp_ns))
    os.utime(route_report, ns=(base_ns + 3_000_000_000, base_ns + 3_000_000_000))
    os.utime(bitstream, ns=(base_ns + 5_000_000_000, base_ns + 5_000_000_000))
    os.utime(metadata, ns=(base_ns + 6_500_000_000, base_ns + 6_500_000_000))

    manifest = collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        run_context={
            **_fresh_artifact_context(project_dir, run_dir),
            "run_status": "write_bitstream Complete!",
            "run_progress": "100%",
            "run_needs_refresh": "0",
            "session_generation_id": "generation-a",
        },
    )

    assert manifest["status"] == "READY"
    assert manifest["evidence_freshness"]["status"] == "FRESH"
    assert manifest["run_execution_identity"]["marker_source"] == "step_markers"
    assert manifest["run_execution_identity"]["write_bitstream_started_mtime_ns"] == base_ns + 4_000_000_000
    assert manifest["run_execution_identity"]["write_bitstream_ended_mtime_ns"] == base_ns + 6_000_000_000


def test_collect_artifacts_rejects_explicitly_disabled_write_bitstream_step(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    bitstream = run_dir / "top.bit"
    write_test_bitstream(bitstream)
    for name in (".write_bitstream.begin.rst", ".write_bitstream.end.rst"):
        (run_dir / name).write_text(name, encoding="utf-8")

    manifest = collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        run_context={
            **_fresh_artifact_context(project_dir, run_dir),
            "write_bitstream_step_enabled": "0",
        },
    )

    assert manifest["status"] == "BLOCK"
    assert any("explicitly disabled" in reason for reason in manifest["evidence_freshness"]["reasons"])


def test_collect_build_artifacts_tool_uses_vivado_run_directory_and_manifest(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    write_test_bitstream(run_dir / "top.bit")
    fake = _artifact_fake_session(project_dir, run_dir)
    service = VivadoToolService(session=fake)

    result = service.call("collect_build_artifacts", {"run_name": "impl_1"})

    assert result["ok"] is True
    assert result["tool"] == "collect_build_artifacts"
    assert result["data"]["run_name"] == "impl_1"
    assert result["data"]["artifacts"][0]["category"] == "bitstream"
    assert Path(result["data"]["manifest_path"]).exists()
    assert "get_runs -quiet {impl_1}" in fake.commands[0]
    assert [action["tool"] for action in result["next_actions"][:3]] == [
        "collect_report_bundle",
        "run_pre_hw_signoff",
        "run_project_audit",
    ]


def test_collect_build_artifacts_stale_evidence_routes_to_run_recheck(tmp_path: Path, monkeypatch) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    write_test_bitstream(run_dir / "top.bit")
    fake = _artifact_fake_session(project_dir, run_dir)
    service = VivadoToolService(session=fake)
    monkeypatch.setattr(
        tools_module,
        "collect_artifacts",
        lambda **kwargs: {
            "status": "BLOCK",
            "manifest_path": str(project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json"),
            "evidence_freshness": {"status": "STALE", "reasons": ["write_bitstream marker missing"]},
            "artifacts": [],
        },
    )

    result = service.call("collect_build_artifacts", {"run_name": "impl_1"})

    assert result["ok"] is False
    assert result["error_code"] == "ARTIFACT_EVIDENCE_STALE"
    assert [action["tool"] for action in result["next_actions"]] == [
        "get_run_progress",
        "collect_build_artifacts",
    ]
    assert all(action["tool"] != "collect_report_bundle" for action in result["next_actions"])


def test_collect_build_artifacts_tool_rejects_output_dir_outside_project(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    outside_output = tmp_path / "outside_artifacts"
    run_dir.mkdir(parents=True)
    write_test_bitstream(run_dir / "top.bit")
    fake = _artifact_fake_session(project_dir, run_dir)
    service = VivadoToolService(session=fake)

    result = service.call("collect_build_artifacts", {"run_name": "impl_1", "output_dir": str(outside_output)})

    assert result["ok"] is False
    assert result["tool"] == "collect_build_artifacts"
    assert result["error_code"] == "ARTIFACT_OUTPUT_DIR_OUTSIDE_PROJECT"
    assert result["data"]["project_dir"] == str(project_dir.resolve())
    assert result["data"]["requested_output_dir"] == str(outside_output.resolve())
    assert result["data"]["allowed_output_root"] == str((project_dir / "vmcp_artifacts" / "impl_1").resolve())
    assert result["next_actions"][0]["tool"] == "collect_build_artifacts"
    assert "omit" in result["next_actions"][0]["arg_sources"]["output_dir"]
    assert not outside_output.exists()


def test_get_artifact_manifest_tool_reads_existing_manifest(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    write_test_bitstream(run_dir / "top.bit")
    manifest = collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        run_context=_fresh_artifact_context(project_dir, run_dir),
    )
    manifest_path = Path(manifest["manifest_path"])
    fake = _artifact_fake_session(project_dir, run_dir)
    service = VivadoToolService(session=fake)

    result = service.call("get_artifact_manifest", {"run_name": "impl_1"})

    assert result["ok"] is True
    assert result["data"]["run_name"] == "impl_1"
    assert result["data"]["manifest_path"] == str(manifest_path)
    assert len(result["data"]["manifest_sha256"]) == 64


def test_get_artifact_manifest_rejects_non_object_json_with_stable_error(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    manifest_path = project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("[]", encoding="utf-8")
    service = VivadoToolService(session=_artifact_fake_session(project_dir, run_dir))

    result = service.call(
        "get_artifact_manifest",
        {"run_name": "impl_1", "manifest_path": str(manifest_path)},
    )

    assert result["ok"] is False
    assert result["error_code"] == "ARTIFACT_MANIFEST_REJECTED"
    assert "JSON object" in result["message"]


def test_get_artifact_manifest_rejects_external_path_before_json_read(tmp_path: Path, monkeypatch) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    outside_manifest = tmp_path / "outside" / "manifest.json"
    outside_manifest.parent.mkdir()
    outside_manifest.write_text('{"schema_version": 3}', encoding="utf-8")
    fake = _artifact_fake_session(project_dir, run_dir)
    service = VivadoToolService(session=fake)

    monkeypatch.setattr(
        "vivado_agent_mcp.tools.load_artifact_manifest_with_sha256",
        lambda path: (_ for _ in ()).throw(AssertionError("external manifest must not be read")),
    )
    result = service.call(
        "get_artifact_manifest",
        {"run_name": "impl_1", "manifest_path": str(outside_manifest)},
    )

    assert result["ok"] is False
    assert result["error_code"] == "ARTIFACT_MANIFEST_REJECTED"
    assert "vmcp_artifacts" in result["message"]


def test_get_artifact_manifest_rejects_oversized_json_before_parse(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    manifest_path = project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    service = VivadoToolService(session=_artifact_fake_session(project_dir, run_dir))

    result = service.call(
        "get_artifact_manifest",
        {"run_name": "impl_1", "manifest_path": str(manifest_path)},
    )

    assert result["ok"] is False
    assert result["error_code"] == "ARTIFACT_MANIFEST_REJECTED"
    assert "read limit" in result["message"]


def test_get_artifact_manifest_tool_falls_back_to_project_artifacts_root_manifest(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    write_test_bitstream(run_dir / "top.bit")
    manifest = collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        output_dir=project_dir / "vmcp_artifacts",
        run_context=_fresh_artifact_context(project_dir, run_dir),
    )
    root_manifest = Path(manifest["manifest_path"])
    fake = _artifact_fake_session(project_dir, run_dir)
    service = VivadoToolService(session=fake)

    result = service.call("get_artifact_manifest", {"run_name": "impl_1"})

    assert result["ok"] is True
    assert result["data"]["run_name"] == "impl_1"
    assert result["data"]["manifest_path"] == str(root_manifest.resolve())


def test_get_artifact_manifest_accepts_manifest_directory_argument(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    write_test_bitstream(run_dir / "top.bit")
    manifest = collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        output_dir=project_dir / "vmcp_artifacts",
        run_context=_fresh_artifact_context(project_dir, run_dir),
    )
    manifest_path = Path(manifest["manifest_path"])
    manifest_dir = manifest_path.parent
    service = VivadoToolService(session=_artifact_fake_session(project_dir, run_dir))

    result = service.call("get_artifact_manifest", {"manifest_path": str(manifest_dir)})

    assert result["ok"] is True
    assert result["data"]["manifest_path"] == str(manifest_path.resolve())


@pytest.mark.parametrize("tamper_target", ["export", "source"])
def test_get_artifact_manifest_rejects_tampered_artifact_evidence(
    tmp_path: Path,
    tamper_target: str,
) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    source = run_dir / "top.bit"
    write_test_bitstream(source)
    manifest = collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        run_context=_fresh_artifact_context(project_dir, run_dir),
    )
    artifact = manifest["artifacts"][0]
    target = Path(artifact["export_path"] if tamper_target == "export" else artifact["source_path"])
    target.write_text("tampered bitstream", encoding="utf-8")
    service = VivadoToolService(session=_artifact_fake_session(project_dir, run_dir))

    result = service.call("get_artifact_manifest", {"manifest_path": manifest["manifest_path"]})

    assert result["ok"] is False
    assert result["error_code"] == "ARTIFACT_MANIFEST_REJECTED"
    assert "mismatch" in result["message"].lower()


def test_get_artifact_manifest_rejects_source_closure_changed_after_collection(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    write_test_bitstream(run_dir / "top.bit")
    manifest = collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        run_context=_fresh_artifact_context(project_dir, run_dir),
    )
    (project_dir / "src" / "fixture_top.sv").write_text(
        "module fixture_top; wire source_changed_after_run; endmodule\n",
        encoding="utf-8",
    )
    service = VivadoToolService(session=_artifact_fake_session(project_dir, run_dir))

    result = service.call("get_artifact_manifest", {"manifest_path": manifest["manifest_path"]})

    assert result["ok"] is False
    assert result["error_code"] == "ARTIFACT_MANIFEST_REJECTED"
    assert "SOURCE_CLOSURE_CHANGED" in result["message"]


def test_get_artifact_manifest_rejects_arbitrary_json_document(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    manifest_path = project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps({"run_name": "impl_1", "artifacts": []}), encoding="utf-8")
    service = VivadoToolService(session=_artifact_fake_session(project_dir, run_dir))

    result = service.call("get_artifact_manifest", {"manifest_path": str(manifest_path)})

    assert result["ok"] is False
    assert result["error_code"] == "ARTIFACT_MANIFEST_REJECTED"
    assert "schema_version=4" in result["message"]


def _fresh_artifact_context(project_dir: Path, run_dir: Path) -> dict[str, Any]:
    begin_marker = run_dir / ".vivado.begin.rst"
    end_marker = run_dir / ".vivado.end.rst"
    if not begin_marker.exists() or not end_marker.exists():
        artifact_mtimes = [path.stat().st_mtime_ns for path in run_dir.iterdir() if path.is_file()]
        earliest = min(artifact_mtimes, default=1_000_000_000)
        latest = max(artifact_mtimes, default=earliest)
        begin_marker.write_text("run started\n", encoding="utf-8")
        end_marker.write_text("run completed\n", encoding="utf-8")
        os.utime(begin_marker, ns=(max(1, earliest - 2_000_000_000), max(1, earliest - 2_000_000_000)))
        os.utime(end_marker, ns=(latest + 2_000_000_000, latest + 2_000_000_000))
    return {
        "project_name": "demo",
        "project_dir": str(project_dir),
        "project_part": "xc7a35tcpg236-1",
        "run_dir": str(run_dir),
        "run_srcset": "sources_1",
        "run_top": "top",
        "run_status": "write_bitstream Complete!",
        "run_progress": "100%",
        "run_needs_refresh": "0",
        "expected_bitstream_path": str(run_dir / "top.bit"),
        "run_bitstream_files": [str(run_dir / "top.bit")],
        "write_bitstream_step_enabled": "1",
        "write_bitstream_step_status": "Complete!",
        "session_generation_id": "test-generation",
        "design_execution_identity": write_test_design_execution_identity(project_dir),
    }


def _artifact_context_raw(project_dir: Path, run_dir: Path) -> str:
    context = _fresh_artifact_context(project_dir, run_dir)
    context["run_bitstream_files"] = encode_wire_list(context["run_bitstream_files"])
    return "\n".join(
        f"{key}={value}"
        for key, value in context.items()
        if key != "design_execution_identity"
    )


def _artifact_fake_session(project_dir: Path, run_dir: Path) -> FakeSession:
    session = FakeSession(raw=_artifact_context_raw(project_dir, run_dir))
    session.design_execution_identity = write_test_design_execution_identity(project_dir)
    return session
