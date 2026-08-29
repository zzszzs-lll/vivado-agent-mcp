import os
from pathlib import Path

import pytest

import vivado_agent_mcp.vivado.simulation as simulation
from vivado_agent_mcp.vivado.wire import encode_wire_list, encode_wire_row


def _attest_xsim_preflight(preflight: dict) -> None:
    project_dir = Path(preflight["project_dir"])
    project_path = project_dir / f"{preflight['project_name']}.xpr"
    project_path.parent.mkdir(parents=True, exist_ok=True)
    project_path.write_text("# trusted test project\n", encoding="utf-8")
    preflight.update(
        {
            "project_path": str(project_path),
            "vivado_version_short": "2021.2",
            "vivado_version_full": "Vivado v2021.2",
            "project_properties": ["PART=xc7a35tcpg236-1"],
            "simset_properties": ["TOP=tb", "TARGET_SIMULATOR=Vivado Simulator"],
            "sim_file_metadata": [
                {"path": str(path), "file_type": Path(path).suffix.lower(), "used_in": "simulation"}
                for path in preflight.get("sim_files", [])
            ],
        }
    )


def test_behavioral_simulation_command_closes_previous_sim_before_launch() -> None:
    command = simulation.behavioral_simulation_command(simset="sim_1", run_time="100 ns")

    assert command.index("catch {close_sim}") < command.index("launch_simulation")
    assert command.index("launch_simulation") < command.index("remove_bps -all -quiet")
    assert "catch {run {" in command
    assert "set vmcp_run_tcl_failed 1" in command


def test_simulation_preflight_uses_compile_order_and_caps_source_scan() -> None:
    command = simulation.simulation_vcd_preflight_command(simset="sim_1")

    assert "-compile_order sources -used_in simulation" in command
    assert str(simulation.MAX_PREFLIGHT_SOURCE_BYTES) in command
    assert "preflight_errors=" in command
    assert "include_dirs=" in command
    assert "verilog_defines=" in command
    assert "target_simulator=" in command
    assert f"set simulator_property_schema_version {{{simulation.XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION}}}" in command
    assert "simulator_options=" in command
    assert "list_property $fs" in command
    for property_name in (
        "xsim.compile.tcl.pre",
        "xsim.compile.xvlog.more_options",
        "xsim.compile.xvhdl.more_options",
        "xsim.compile.xsc.more_options",
        "xsim.elaborate.xelab.more_options",
        "xsim.simulate.tcl.post",
        "xsim.simulate.custom_tcl",
        "xsim.simulate.xsim.more_options",
    ):
        assert property_name in command


def test_managed_simulation_policy_disables_incremental_before_attested_preflight() -> None:
    command = simulation.managed_simulation_policy_preflight_command(simset="sim_1")

    assert "set_property INCREMENTAL 0" in command
    assert command.index("set_property INCREMENTAL 0") < command.index("project_path=")
    assert "VMCP_SIMULATION_INCREMENTAL_POLICY_FAILED" in command
    assert "simulator_property_schema_version=" in command


def test_simulation_source_identity_changes_after_rtl_mutation(tmp_path) -> None:
    project_dir = tmp_path / "project"
    source = project_dir / "rtl" / "top.sv"
    source.parent.mkdir(parents=True)
    source.write_text("module top; endmodule\n", encoding="utf-8")
    preflight = {
        "project_dir": str(project_dir),
        "project_name": "demo",
        "simset": "sim_1",
        "sim_top": "tb_top",
        "sim_files": [str(source)],
        "include_dirs": [str(project_dir / "include")],
        "verilog_defines": ["TEST_MODE=1"],
        "target_simulator": "Vivado Simulator",
        "preflight_errors": [],
    }

    before = simulation.build_simulation_source_identity(preflight)
    source.write_text("module top; logic changed; endmodule\n", encoding="utf-8")
    after = simulation.build_simulation_source_identity(preflight)

    assert before["status"] == "READY"
    assert after["status"] == "READY"
    assert before["sha256"] != after["sha256"]
    assert before["identity"]["files"][0]["sha256"] != after["identity"]["files"][0]["sha256"]


def test_stable_simulation_identity_ignores_expected_xpr_update_but_tracks_sources(tmp_path) -> None:
    project_dir = tmp_path / "project"
    source = project_dir / "rtl" / "top.sv"
    source.parent.mkdir(parents=True)
    source.write_text("module top; endmodule\n", encoding="utf-8")
    preflight = {
        "project_dir": str(project_dir),
        "project_name": "demo",
        "simset": "sim_1",
        "sim_top": "tb_top",
        "sim_files": [str(source)],
        "include_dirs": [],
        "verilog_defines": [],
        "target_simulator": "Vivado Simulator",
        "simulator_property_schema_version": simulation.XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION,
        "simulator_options": [],
    }
    _attest_xsim_preflight(preflight)
    preflight["host_input_files"] = [preflight["project_path"]]

    full_before = simulation.build_simulation_source_identity(preflight)
    stable_before = simulation.build_simulation_stable_input_identity(preflight, full_before)
    Path(preflight["project_path"]).write_text("# Vivado updated project XML\n", encoding="utf-8")
    full_after_project_write = simulation.build_simulation_source_identity(preflight)
    stable_after_project_write = simulation.build_simulation_stable_input_identity(preflight, full_after_project_write)

    assert full_before["sha256"] != full_after_project_write["sha256"]
    assert stable_before["sha256"] == stable_after_project_write["sha256"]

    preflight["project_properties"] = ["PART=xc7a35tcpg236-1", "DEFAULT_LIB=work_changed"]
    full_after_property_change = simulation.build_simulation_source_identity(preflight)
    stable_after_property_change = simulation.build_simulation_stable_input_identity(
        preflight,
        full_after_property_change,
    )

    assert stable_before["sha256"] != stable_after_property_change["sha256"]

    source.write_text("module top; wire changed; endmodule\n", encoding="utf-8")
    full_after_source_write = simulation.build_simulation_source_identity(preflight)
    stable_after_source_write = simulation.build_simulation_stable_input_identity(preflight, full_after_source_write)

    assert stable_before["sha256"] != stable_after_source_write["sha256"]


def test_simulation_source_identity_includes_recursive_literal_includes(tmp_path) -> None:
    project_dir = tmp_path / "project"
    source = project_dir / "sim" / "tb.sv"
    include_dir = project_dir / "include"
    nested = include_dir / "nested.svh"
    behavior = include_dir / "behavior.svh"
    source.parent.mkdir(parents=True)
    include_dir.mkdir(parents=True)
    source.write_text('`include "../include/behavior.svh"\nmodule tb; endmodule\n', encoding="utf-8")
    behavior.write_text('`include "nested.svh"\nlocalparam int VALUE = 1;\n', encoding="utf-8")
    nested.write_text("localparam int NESTED = 1;\n", encoding="utf-8")
    preflight = {
        "project_dir": str(project_dir),
        "project_name": "demo",
        "simset": "sim_1",
        "sim_top": "tb",
        "sim_files": [str(source)],
        "include_dirs": [str(include_dir)],
        "verilog_defines": [],
        "target_simulator": "Vivado Simulator",
        "preflight_errors": [],
    }

    before = simulation.build_simulation_source_identity(preflight)
    nested.write_text("localparam int NESTED = 2;\n", encoding="utf-8")
    after = simulation.build_simulation_source_identity(preflight)

    assert before["status"] == "READY"
    assert set(before["identity"]["include_files"]) == {str(behavior.resolve()), str(nested.resolve())}
    assert before["sha256"] != after["sha256"]


def test_simulation_source_identity_blocks_include_dirs_only_resolution_to_prevent_late_shadowing(tmp_path) -> None:
    project_dir = tmp_path / "project"
    source = project_dir / "sim" / "tb.sv"
    include_dir = project_dir / "include"
    source.parent.mkdir(parents=True)
    include_dir.mkdir(parents=True)
    source.write_text('`include "defs.svh"\nmodule tb; endmodule\n', encoding="utf-8")
    (include_dir / "defs.svh").write_text("localparam int VALUE = 1;\n", encoding="utf-8")

    identity = simulation.build_simulation_source_identity(
        {
            "project_dir": str(project_dir),
            "project_name": "demo",
            "simset": "sim_1",
            "sim_top": "tb",
            "sim_files": [str(source)],
            "include_dirs": [str(include_dir)],
            "preflight_errors": [],
        }
    )

    assert identity["status"] == "BLOCK"
    assert any("late-file shadowing" in issue for issue in identity["issues"])


def test_simulation_trust_closure_requires_every_executable_input_inside_one_trusted_root(tmp_path) -> None:
    trusted = tmp_path / "trusted"
    project = trusted / "project"
    source = project / "sim" / "tb.sv"
    include_dir = project / "include"
    header = include_dir / "defs.svh"
    source.parent.mkdir(parents=True)
    include_dir.mkdir(parents=True)
    source.write_text('`include "../include/defs.svh"\nmodule tb; endmodule\n', encoding="utf-8")
    header.write_text("localparam int VALUE = 1;\n", encoding="utf-8")
    preflight = {
        "project_dir": str(project),
        "project_name": "demo",
        "simset": "sim_1",
        "sim_top": "tb",
        "sim_files": [str(source)],
        "include_dirs": [str(include_dir)],
        "verilog_defines": ["TEST_MODE=1"],
        "target_simulator": "Vivado Simulator",
        "simulator_property_schema_version": simulation.XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION,
        "simulator_options": [],
        "preflight_errors": [],
    }

    _attest_xsim_preflight(preflight)
    identity = simulation.build_simulation_source_identity(preflight)
    result = simulation.validate_simulation_trust_closure(
        preflight,
        identity,
        trusted_roots=[str(trusted)],
    )

    assert result["status"] == "READY"
    assert result["source_count"] == 2
    assert result["accepted_root"] == str(trusted.resolve())
    assert set(result["closure_directories"]) == {
        str(source.parent.resolve()),
        str(header.parent.resolve()),
        str(include_dir.resolve()),
    }


def test_simulation_trust_closure_blocks_external_include_and_xsim_more_options(tmp_path) -> None:
    trusted = tmp_path / "trusted"
    project = trusted / "project"
    outside = tmp_path / "outside"
    source = project / "sim" / "tb.sv"
    header = outside / "defs.svh"
    source.parent.mkdir(parents=True)
    outside.mkdir()
    source.write_text(f'`include "{header.as_posix()}"\nmodule tb; endmodule\n', encoding="utf-8")
    header.write_text("localparam int VALUE = 1;\n", encoding="utf-8")
    preflight = {
        "project_dir": str(project),
        "project_name": "demo",
        "simset": "sim_1",
        "sim_top": "tb",
        "sim_files": [str(source)],
        "include_dirs": [],
        "verilog_defines": [],
        "target_simulator": "Vivado Simulator",
        "simulator_property_schema_version": simulation.XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION,
        "simulator_options": ["xsim.elaborate.xelab.more_options --sv_lib evil"],
        "preflight_errors": [],
    }

    _attest_xsim_preflight(preflight)
    identity = simulation.build_simulation_source_identity(preflight)
    result = simulation.validate_simulation_trust_closure(
        preflight,
        identity,
        trusted_roots=[str(trusted)],
    )

    assert result["status"] == "BLOCK"
    assert any("more_options" in issue for issue in result["issues"])
    assert any("escape every trusted root" in issue for issue in result["issues"])


def test_simulation_trust_closure_requires_attested_executable_property_schema(tmp_path) -> None:
    trusted = tmp_path / "trusted"
    source = trusted / "project" / "sim" / "tb.sv"
    source.parent.mkdir(parents=True)
    source.write_text("module tb; endmodule\n", encoding="utf-8")
    preflight = {
        "project_dir": str(source.parents[1]),
        "project_name": "demo",
        "simset": "sim_1",
        "sim_top": "tb",
        "sim_files": [str(source)],
        "include_dirs": [],
        "verilog_defines": [],
        "target_simulator": "Vivado Simulator",
        "simulator_options": [],
        "preflight_errors": [],
    }

    _attest_xsim_preflight(preflight)
    identity = simulation.build_simulation_source_identity(preflight)
    result = simulation.validate_simulation_trust_closure(preflight, identity, trusted_roots=[str(trusted)])

    assert result["status"] == "BLOCK"
    assert any("executable-property schema" in issue for issue in result["issues"])


def test_simulation_trust_closure_blocks_non_rtl_model_selection(tmp_path) -> None:
    trusted = tmp_path / "trusted"
    source = trusted / "project" / "sim" / "tb.sv"
    source.parent.mkdir(parents=True)
    source.write_text("module tb; endmodule\n", encoding="utf-8")
    preflight = {
        "project_dir": str(source.parents[1]),
        "project_name": "demo",
        "simset": "sim_1",
        "sim_top": "tb",
        "sim_files": [str(source)],
        "include_dirs": [],
        "verilog_defines": [],
        "target_simulator": "Vivado Simulator",
        "simulator_property_schema_version": simulation.XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION,
        "simulator_options": [],
        "preflight_errors": [],
    }
    _attest_xsim_preflight(preflight)
    preflight["project_properties"].append("PREFERRED_SIM_MODEL string false tlm_dpi")

    identity = simulation.build_simulation_source_identity(preflight)
    result = simulation.validate_simulation_trust_closure(preflight, identity, trusted_roots=[str(trusted)])

    assert result["status"] == "BLOCK"
    assert result["simulation_model_policy"] == "rtl_only"
    assert result["untrusted_simulation_models_detected"] is True
    assert any("only accepts RTL" in issue for issue in result["issues"])


def test_simulation_include_closure_fails_closed_for_unresolved_dynamic_ambiguous_and_cycle(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source_dir = project_dir / "sim"
    include_dir = project_dir / "include"
    source_dir.mkdir(parents=True)
    include_dir.mkdir(parents=True)
    source = source_dir / "tb.sv"

    cases = {
        "unresolved": '`include "missing.svh"\n',
        "dynamic": "`include HEADER_FILE\n",
        "ambiguous": '`include "shared.svh"\n',
        "cycle": '`include "cycle.svh"\n',
    }
    (source_dir / "shared.svh").write_text("localparam int A = 1;\n", encoding="utf-8")
    (include_dir / "shared.svh").write_text("localparam int A = 2;\n", encoding="utf-8")
    (include_dir / "cycle.svh").write_text('`include "../sim/tb.sv"\n', encoding="utf-8")

    for label, content in cases.items():
        source.write_text(content, encoding="utf-8")
        result = simulation.analyze_testbench_waveform_paths(
            {
                "project_dir": str(project_dir),
                "sim_dir": str(sim_dir),
                "sim_files": [str(source)],
                "include_dirs": [str(include_dir)],
                "preflight_errors": [],
            }
        )
        assert result["status"] == "BLOCK", label
        assert result["uncontrolled_reasons"], label


def test_simulation_waveform_scan_covers_include_file_outputs(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.sv"
    include_dir = project_dir / "include"
    include_file = include_dir / "tb_behavior.svh"
    source.parent.mkdir(parents=True)
    include_dir.mkdir(parents=True)
    source.write_text('`include "../include/tb_behavior.svh"\nmodule tb; endmodule\n', encoding="utf-8")
    include_file.write_text(
        'integer fd; initial begin $dumpfile("D:/outside/large.vcd"); fd = $fopen("D:/outside/results.log", "w"); end\n',
        encoding="utf-8",
    )

    result = simulation.analyze_testbench_waveform_paths(
        {
            "project_dir": str(project_dir),
            "sim_dir": str(sim_dir),
            "sim_files": [str(source)],
            "include_dirs": [str(include_dir)],
            "preflight_errors": [],
        }
    )

    assert result["status"] == "BLOCK"
    assert str(include_file.resolve()) in result["scanned_source_paths"]
    assert any("testbench VCD path escapes" in reason for reason in result["uncontrolled_reasons"])
    assert any("$fopen path escapes" in reason for reason in result["uncontrolled_reasons"])


def test_simulation_waveform_scan_blocks_token_pasting_and_dpi_host_execution(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.sv"
    source.parent.mkdir(parents=True)
    source.write_text(
        '`define CAT(a,b) a``b\nimport "DPI-C" function int host_call();\nmodule tb; endmodule\n',
        encoding="utf-8",
    )

    result = simulation.analyze_testbench_waveform_paths(
        {
            "project_dir": str(project_dir),
            "sim_dir": str(sim_dir),
            "sim_files": [str(source)],
            "preflight_errors": [],
        }
    )

    assert result["status"] == "BLOCK"
    assert any("token-pasting" in reason for reason in result["uncontrolled_reasons"])
    assert any("DPI code" in reason for reason in result["uncontrolled_reasons"])


def test_simulation_waveform_scan_blocks_user_macro_expansion_that_can_hide_host_execution(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.sv"
    source.parent.mkdir(parents=True)
    source.write_text(
        "`define DOLLAR $\n`define HOST system\nmodule tb; initial `DOLLAR`HOST(\"whoami\"); endmodule\n",
        encoding="utf-8",
    )

    result = simulation.analyze_testbench_waveform_paths(
        {
            "project_dir": str(project_dir),
            "sim_dir": str(sim_dir),
            "sim_files": [str(source)],
            "preflight_errors": [],
        }
    )

    assert result["status"] == "BLOCK"
    assert any("user-defined preprocessor macros" in reason for reason in result["uncontrolled_reasons"])


def test_simulation_waveform_scan_blocks_unanalyzed_non_verilog_source(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.vhd"
    source.parent.mkdir(parents=True)
    source.write_text("entity tb is end entity; architecture sim of tb is begin end architecture;\n", encoding="utf-8")

    result = simulation.analyze_testbench_waveform_paths(
        {
            "project_dir": str(project_dir),
            "sim_dir": str(sim_dir),
            "sim_files": [str(source)],
            "preflight_errors": [],
        }
    )

    assert result["status"] == "BLOCK"
    assert any("cannot be statically attested" in reason for reason in result["uncontrolled_reasons"])


def test_simulation_host_inputs_are_hashed_and_bound_to_trusted_root(tmp_path) -> None:
    trusted = tmp_path / "trusted"
    project_dir = trusted / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.sv"
    memory = project_dir / "data" / "memory.hex"
    source.parent.mkdir(parents=True)
    memory.parent.mkdir(parents=True)
    source.write_text(f'module tb; reg [7:0] mem [0:1]; initial $readmemh("{memory.as_posix()}", mem); endmodule\n', encoding="utf-8")
    memory.write_text("01\n02\n", encoding="utf-8")
    preflight = {
        "project_dir": str(project_dir),
        "project_name": "demo",
        "simset": "sim_1",
        "sim_top": "tb",
        "sim_files": [str(source)],
        "include_dirs": [],
        "verilog_defines": [],
        "target_simulator": "Vivado Simulator",
        "simulator_property_schema_version": simulation.XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION,
        "simulator_options": [],
        "sim_dir": str(sim_dir),
        "preflight_errors": [],
    }

    analysis = simulation.analyze_testbench_waveform_paths(preflight)
    preflight["host_input_files"] = analysis["host_input_files"]
    _attest_xsim_preflight(preflight)
    before = simulation.build_simulation_source_identity(preflight)
    trust = simulation.validate_simulation_trust_closure(preflight, before, trusted_roots=[str(trusted)])
    memory.write_text("03\n04\n", encoding="utf-8")
    after = simulation.build_simulation_source_identity(preflight)

    assert analysis["status"] == "READY"
    assert analysis["host_input_files"] == [str(memory.resolve())]
    assert any(item["source_kind"] == "host_input" for item in before["identity"]["files"])
    assert trust["status"] == "READY"
    assert trust["host_input_count"] == 1
    assert before["sha256"] != after["sha256"]


def test_simulation_host_input_dynamic_path_and_trusted_root_escape_are_blocked(tmp_path) -> None:
    trusted = tmp_path / "trusted"
    project_dir = trusted / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.sv"
    outside = tmp_path / "outside" / "secret.hex"
    source.parent.mkdir(parents=True)
    outside.parent.mkdir()
    outside.write_text("ff\n", encoding="utf-8")
    source.write_text(
        f'module tb; reg [7:0] mem [0:1]; string p; initial begin $readmemb(p, mem); $sdf_annotate("{outside.as_posix()}"); end endmodule\n',
        encoding="utf-8",
    )
    preflight = {
        "project_dir": str(project_dir),
        "project_name": "demo",
        "simset": "sim_1",
        "sim_top": "tb",
        "sim_files": [str(source)],
        "include_dirs": [],
        "verilog_defines": [],
        "target_simulator": "Vivado Simulator",
        "simulator_property_schema_version": simulation.XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION,
        "simulator_options": [],
        "sim_dir": str(sim_dir),
        "preflight_errors": [],
    }

    analysis = simulation.analyze_testbench_waveform_paths(preflight)
    assert analysis["status"] == "BLOCK"
    assert any("dynamic or non-literal $readmemb" in reason for reason in analysis["uncontrolled_reasons"])

    source.write_text(
        f'module tb; initial $sdf_annotate("{outside.as_posix()}"); endmodule\n',
        encoding="utf-8",
    )
    analysis = simulation.analyze_testbench_waveform_paths(preflight)
    preflight["host_input_files"] = analysis["host_input_files"]
    _attest_xsim_preflight(preflight)
    identity = simulation.build_simulation_source_identity(preflight)
    trust = simulation.validate_simulation_trust_closure(preflight, identity, trusted_roots=[str(trusted)])

    assert analysis["status"] == "READY"
    assert trust["status"] == "BLOCK"
    assert any("escape every trusted root" in issue for issue in trust["issues"])


def test_simulation_fopen_read_mode_is_attested_and_dynamic_or_read_write_modes_block(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.sv"
    data_file = project_dir / "data" / "input.txt"
    source.parent.mkdir(parents=True)
    data_file.parent.mkdir(parents=True)
    data_file.write_text("input\n", encoding="utf-8")
    source.write_text(
        f'integer fd; initial fd = $fopen("{data_file.as_posix()}", "r");\n',
        encoding="utf-8",
    )
    preflight = {
        "project_dir": str(project_dir),
        "sim_dir": str(sim_dir),
        "sim_files": [str(source)],
        "preflight_errors": [],
    }

    analysis = simulation.analyze_testbench_waveform_paths(preflight)
    assert analysis["status"] == "READY"
    assert analysis["host_input_files"] == [str(data_file.resolve())]

    source.write_text(
        f'integer fd; string mode; initial fd = $fopen("{data_file.as_posix()}", mode);\n',
        encoding="utf-8",
    )
    dynamic = simulation.analyze_testbench_waveform_paths(preflight)
    assert dynamic["status"] == "BLOCK"
    assert any("dynamic, unassigned, or non-literal $fopen" in reason for reason in dynamic["uncontrolled_reasons"])

    source.write_text(
        f'integer fd; initial fd = $fopen("{data_file.as_posix()}", "r+");\n',
        encoding="utf-8",
    )
    read_write = simulation.analyze_testbench_waveform_paths(preflight)
    assert read_write["status"] == "BLOCK"
    assert any("read/write $fopen mode" in reason for reason in read_write["uncontrolled_reasons"])


def test_simulation_system_identifier_policy_fails_closed_without_scanning_strings(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.sv"
    source.parent.mkdir(parents=True)
    source.write_text(
        'module tb; localparam int W = $clog2(8); initial begin $display("literal $system( is data"); $finish; end endmodule\n',
        encoding="utf-8",
    )
    preflight = {
        "project_dir": str(project_dir),
        "sim_dir": str(sim_dir),
        "sim_files": [str(source)],
        "preflight_errors": [],
    }

    allowed = simulation.analyze_testbench_waveform_paths(preflight)
    assert allowed["status"] == "READY"

    source.write_text('module tb; initial $coverage_save("outside.ucdb"); endmodule\n', encoding="utf-8")
    unknown = simulation.analyze_testbench_waveform_paths(preflight)
    assert unknown["status"] == "BLOCK"
    assert any("unsupported system task/function" in reason for reason in unknown["uncontrolled_reasons"])


def test_simulation_host_input_reparse_path_is_rejected(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.sv"
    outside = tmp_path / "outside" / "secret.hex"
    linked = project_dir / "data" / "linked.hex"
    source.parent.mkdir(parents=True)
    outside.parent.mkdir()
    linked.parent.mkdir(parents=True)
    outside.write_text("ff\n", encoding="utf-8")
    try:
        os.symlink(outside, linked)
    except OSError as exc:
        pytest.skip(f"Windows symlink creation is unavailable: {exc}")
    source.write_text(
        f'module tb; reg [7:0] mem [0:1]; initial $readmemh("{linked.as_posix()}", mem); endmodule\n',
        encoding="utf-8",
    )

    analysis = simulation.analyze_testbench_waveform_paths(
        {
            "project_dir": str(project_dir),
            "sim_dir": str(sim_dir),
            "sim_files": [str(source)],
            "preflight_errors": [],
        }
    )

    assert analysis["status"] == "BLOCK"
    assert any("reparse point" in reason for reason in analysis["uncontrolled_reasons"])


def test_simulation_host_input_hard_link_is_rejected(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.sv"
    outside = tmp_path / "outside" / "secret.hex"
    linked = project_dir / "data" / "inside.hex"
    source.parent.mkdir(parents=True)
    outside.parent.mkdir()
    linked.parent.mkdir(parents=True)
    outside.write_text("ff\n", encoding="utf-8")
    os.link(outside, linked)
    source.write_text(
        f'module tb; reg [7:0] mem [0:1]; initial $readmemh("{linked.as_posix()}", mem); endmodule\n',
        encoding="utf-8",
    )

    analysis = simulation.analyze_testbench_waveform_paths(
        {
            "project_dir": str(project_dir),
            "sim_dir": str(sim_dir),
            "sim_files": [str(source)],
            "preflight_errors": [],
        }
    )

    assert analysis["status"] == "BLOCK"
    assert any("multiple hard links" in reason for reason in analysis["uncontrolled_reasons"])


def test_simulation_trust_closure_blocks_external_source_without_other_blockers(tmp_path) -> None:
    trusted = tmp_path / "trusted"
    project = trusted / "project"
    outside = tmp_path / "outside" / "tb.sv"
    project.mkdir(parents=True)
    outside.parent.mkdir()
    outside.write_text("module tb; endmodule\n", encoding="utf-8")
    preflight = {
        "project_dir": str(project),
        "project_name": "demo",
        "simset": "sim_1",
        "sim_top": "tb",
        "sim_files": [str(outside)],
        "include_dirs": [],
        "verilog_defines": [],
        "target_simulator": "Vivado Simulator",
        "simulator_options": [],
        "preflight_errors": [],
    }

    _attest_xsim_preflight(preflight)
    identity = simulation.build_simulation_source_identity(preflight)
    result = simulation.validate_simulation_trust_closure(preflight, identity, trusted_roots=[str(trusted)])

    assert result["status"] == "BLOCK"
    assert any("escape every trusted root" in issue for issue in result["issues"])


def test_simulation_trust_closure_blocks_xsim_options_without_path_escape(tmp_path) -> None:
    trusted = tmp_path / "trusted"
    project = trusted / "project"
    source = project / "sim" / "tb.sv"
    source.parent.mkdir(parents=True)
    source.write_text("module tb; endmodule\n", encoding="utf-8")
    preflight = {
        "project_dir": str(project),
        "project_name": "demo",
        "simset": "sim_1",
        "sim_top": "tb",
        "sim_files": [str(source)],
        "include_dirs": [],
        "verilog_defines": [],
        "target_simulator": "Vivado Simulator",
        "simulator_options": ["xsim.elaborate.xelab.more_options --sv_lib untrusted"],
        "preflight_errors": [],
    }

    _attest_xsim_preflight(preflight)
    identity = simulation.build_simulation_source_identity(preflight)
    result = simulation.validate_simulation_trust_closure(preflight, identity, trusted_roots=[str(trusted)])

    assert result["status"] == "BLOCK"
    assert any("more_options" in issue for issue in result["issues"])


def test_testbench_vcd_path_inside_project_but_outside_sim_root_is_blocked(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.sv"
    source.parent.mkdir(parents=True)
    source.write_text('$dumpfile("../shared.vcd");\n$dumpvars;', encoding="utf-8")

    result = simulation.analyze_testbench_waveform_paths(
        {
            "project_dir": str(project_dir),
            "sim_dir": str(sim_dir),
            "testbench_vcd_sources": [str(source)],
            "preflight_errors": [],
        }
    )

    assert result["status"] == "BLOCK"
    assert "controlled simulation directory" in result["uncontrolled_reasons"][0]


def test_simulation_preflight_blocks_external_fopen_and_dynamic_outputs(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.sv"
    source.parent.mkdir(parents=True)
    source.write_text(
        'integer fd; initial begin fd = $fopen("D:/outside/results.log", "w"); $fwrite(fd, "x"); end\n',
        encoding="utf-8",
    )

    result = simulation.analyze_testbench_waveform_paths(
        {
            "project_dir": str(project_dir),
            "sim_dir": str(sim_dir),
            "sim_files": [str(source)],
            "preflight_errors": [],
        }
    )

    assert result["status"] == "BLOCK"
    assert any("$fopen path escapes" in reason for reason in result["uncontrolled_reasons"])

    source.write_text('initial $writememh(output_path, memory);\n', encoding="utf-8")
    dynamic = simulation.analyze_testbench_waveform_paths(
        {
            "project_dir": str(project_dir),
            "sim_dir": str(sim_dir),
            "sim_files": [str(source)],
            "preflight_errors": [],
        }
    )
    assert dynamic["status"] == "BLOCK"
    assert any("dynamic or non-literal $writememh" in reason for reason in dynamic["uncontrolled_reasons"])


def test_vcd_limited_simulation_checks_size_between_bounded_run_chunks() -> None:
    command = simulation.behavioral_simulation_command(
        simset="sim_1",
        run_time="1 us",
        export_vcd=True,
        max_vcd_mb=1,
    )

    loop_start = command.index("for {set vmcp_vcd_step 0}")
    run_index = command.index("run {3906250fs}", loop_start)
    size_check_index = command.index("file size $f", run_index)
    break_index = command.index("set vmcp_vcd_limit_stopped 1; break", size_check_index)
    assert loop_start < run_index < size_check_index < break_index
    assert "run all" not in command
    assert "catch {run {3906250fs}} vmcp_run_error" in command
    assert "*.wdb" in command
    assert "vmcp_current_waveform_total" in command


def test_behavioral_simulation_monitors_declared_project_waveform_path() -> None:
    command = simulation.behavioral_simulation_command(
        simset="sim_1",
        run_time="1 us",
        testbench_vcd_usage=True,
        monitored_waveform_paths=["D:/project/waves/tb.vcd"],
    )

    assert "set vmcp_monitored_waveform_paths" in command
    assert "D:/project/waves/tb.vcd" in command
    assert "foreach f $vmcp_monitored_waveform_paths" in command


def test_parse_simulation_result_extracts_status_and_artifacts() -> None:
    raw = f"""
sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim
log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xsim.log
wdb_files={encode_wire_list(['D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/top.wdb'])}
vcd_files={encode_wire_list(['D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/vmcp_behav.vcd'])}
log_begin=__VMCP_LOG_BEGIN__
INFO: [XSIM 43-3496] Simulation finished
CRITICAL WARNING: [Vivado 12-123] example warning
""".strip()

    result = simulation.parse_simulation_result(raw)

    assert result["status"] == "completed"
    assert result["counts"]["ERROR"] == 0
    assert result["counts"]["CRITICAL WARNING"] == 1
    assert result["artifacts"]["wdb_files"][0]["path"] == "D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/top.wdb"
    assert result["artifacts"]["vcd_files"][0]["path"] == "D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/vmcp_behav.vcd"


def test_parse_simulation_result_marks_error_logs_failed() -> None:
    raw = """
sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim
log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xsim.log
wdb_files=
vcd_files=
log_begin=__VMCP_LOG_BEGIN__
ERROR: [XSIM 43-3225] Cannot find design unit
""".strip()

    result = simulation.parse_simulation_result(raw)

    assert result["status"] == "failed"
    assert result["counts"]["ERROR"] == 1
    assert "Cannot find design unit" in result["log_excerpt"]


def test_parse_simulation_result_exposes_all_log_paths() -> None:
    raw = f"""
sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim
log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xelab.log
log_paths={encode_wire_list(['D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xvlog.log', 'D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xelab.log'])}
wdb_files=
vcd_files=
log_begin=__VMCP_LOG_BEGIN__
ERROR: [XSIM 43-3225] Cannot find design unit
""".strip()

    result = simulation.parse_simulation_result(raw)

    assert result["status"] == "failed"
    assert result["artifacts"]["log_paths"] == [
        "D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xvlog.log",
        "D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xelab.log",
    ]


def test_parse_simulation_result_marks_testbench_fail_failed() -> None:
    raw = f"""
sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim
log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/simulate.log
wdb_files={encode_wire_list(['D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/top.wdb'])}
vcd_files=
log_begin=__VMCP_LOG_BEGIN__
FAIL timeout
$finish called at time : 205 ns
""".strip()

    result = simulation.parse_simulation_result(raw)

    assert result["status"] == "failed"
    assert result["simulation_diagnosis"]["primary_cause"] == "testbench_failure"


def test_parse_simulation_result_marks_tb_fail_token_failed() -> None:
    raw = """
sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim
log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/simulate.log
wdb_files=
vcd_files=
log_begin=__VMCP_LOG_BEGIN__
Time resolution is 1 ps
TB_FAIL expected=1 got=2
$finish called at time : 36 ns
""".strip()

    result = simulation.parse_simulation_result(raw)

    assert result["status"] == "failed"
    assert result["simulation_diagnosis"]["primary_cause"] == "testbench_failure"


def test_parse_simulation_result_keeps_incomplete_logs_unknown() -> None:
    raw = """
sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim
log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/simulate.log
wdb_files=
vcd_files=
log_begin=__VMCP_LOG_BEGIN__
Time resolution is 1 ps
""".strip()

    result = simulation.parse_simulation_result(raw)

    assert result["status"] == "unknown"


def test_simulation_result_read_command_targets_xsim_artifacts() -> None:
    command = simulation.simulation_result_read_command(simset="sim_1")

    assert "{sim_1}" in command
    assert ".sim" in command
    assert "xsim" in command
    assert "xsim.log" in command
    assert "simulate.log" in command
    assert "xelab.log" in command
    assert "xvlog.log" in command
    assert "log_paths=" in command
    assert "__VMCP_LOG_BEGIN__" in command
    assert "vmcp_log_previous_size" in command
    assert "vmcp_log_previous_mtime" in command
    assert "vmcp_log_current_mtime" in command
    assert "vmcp_log_current_mtime != $vmcp_log_previous_mtime" in command
    assert "vmcp_log_span_reset_detected" in command
    assert "status_source=latest_log_tail" in command
    assert "vmcp_log_settle_deadline" not in command


def test_invocation_result_waits_for_xsim_log_flush_without_weakening_latest_log_reads() -> None:
    command = simulation.simulation_result_read_command(
        simset="sim_1",
        status_source="simulation_invocation_log_span",
    )

    assert "vmcp_log_settle_deadline" in command
    assert "simulate.log" in command
    assert "finish called" in command
    assert "tb_pass" in command
    assert "after 100" in command
    assert "log_settle_wait_ms=" in command
    assert "log_settle_terminal_detected=" in command


def test_behavioral_simulation_command_tracks_preferred_log_mtime() -> None:
    command = simulation.behavioral_simulation_command(simset="sim_1", run_time="20 us")

    assert "set vmcp_log_previous_mtime 0" in command
    assert "if {$vmcp_log_previous_path eq \"\" && [file exists $vmcp_log_candidate]}" in command
    assert "catch {set vmcp_log_previous_mtime [file mtime $vmcp_log_candidate]}" in command


def test_simulation_result_read_command_catches_missing_or_locked_logs() -> None:
    command = simulation.simulation_result_read_command(simset="sim_1")

    assert "catch {set log_candidates" in command
    assert "catch {set wdb_files" in command
    assert "catch {set vcd_files" in command
    assert "catch {" in command and "set content" in command


def test_parse_simulation_result_extracts_waveform_sizes_and_diagnosis() -> None:
    wdb_row = encode_wire_row({'path': 'D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/top.wdb', 'size_bytes': '1024'})
    vcd_row = encode_wire_row({'path': 'D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/vmcp_behav.vcd', 'size_bytes': '268435457'})
    raw = f"""
sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim
log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xsim.log
wdb_files={encode_wire_list([wdb_row])}
vcd_files={encode_wire_list([vcd_row])}
wdb_total_bytes=1024
vcd_total_bytes=268435457
vcd_largest_file=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/vmcp_behav.vcd
vcd_largest_bytes=268435457
vcd_limit_bytes=268435456
vcd_limit_exceeded=1
vcd_limit_stopped=1
vcd_conflict=0
vcd_export_mode=mcp_open_vcd
log_begin=__VMCP_LOG_BEGIN__
INFO: [XSIM 43-3496] Simulation finished
""".strip()

    result = simulation.parse_simulation_result(raw)

    assert result["artifacts"]["vcd_total_bytes"] == 268435457
    assert result["artifacts"]["vcd_files"][0]["size_bytes"] == 268435457
    assert result["artifacts"]["largest_vcd_file"]["size_bytes"] == 268435457
    assert result["diagnosis"]["primary_cause"] == "vcd_limit_exceeded"
    assert result["simulation_diagnosis"]["primary_cause"] == "vcd_limit_exceeded"
    assert result["vcd_limit_stopped"] is True


def test_parse_simulation_result_uses_invocation_span_without_old_failures() -> None:
    raw = """
sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim
log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xsim.log
status_source=simulation_invocation_log_span
simulation_invocation_id=sim-123
started_at=2026-06-01T00:00:00Z
ended_at=2026-06-01T00:00:02Z
log_previous_size=4096
log_previous_mtime=100
log_current_size=4200
log_current_mtime=101
log_span_start=4096
log_span_end=4200
log_span_reset_detected=0
log_settle_wait_ms=700
log_settle_terminal_detected=1
wdb_files=
vcd_files=
log_begin=__VMCP_LOG_BEGIN__
TB_PASS current invocation passed
$finish called at time : 100 ns
""".strip()

    result = simulation.parse_simulation_result(raw)

    assert result["status"] == "completed"
    assert result["status_source"] == "simulation_invocation_log_span"
    assert result["simulation_invocation_id"] == "sim-123"
    assert result["log_span"]["start"] == 4096
    assert result["log_span"]["end"] == 4200
    assert result["log_span"]["previous_mtime"] == 100
    assert result["log_span"]["current_mtime"] == 101
    assert result["log_span"]["reset_detected"] is False
    assert result["log_span"]["settle_wait_ms"] == 700
    assert result["log_span"]["settle_terminal_detected"] is True
    assert result["diagnosis"]["primary_cause"] == "testbench_pass"


def test_parse_simulation_result_attests_fresh_context_and_detects_source_change() -> None:
    before = encode_wire_list(['D:/project/tb.sv|100|200'])
    same_after = encode_wire_list(['D:/project/tb.sv|100|200'])
    changed_after = encode_wire_list(['D:/project/tb.sv|101|201'])
    common = f"""
status_source=simulation_invocation_log_span
simulation_invocation_id=sim-ctx
project_dir_before=D:/project
project_dir_after=D:/project
project_name_before=demo
project_name_after=demo
simset_before=sim_1
simset_after=sim_1
sim_top_before=tb
sim_top_after=tb
source_snapshot_before={before}
""".strip()
    fresh = simulation.parse_simulation_result(
        common
        + f"\nsource_snapshot_after={same_after}\nlog_begin=__VMCP_LOG_BEGIN__\nTB_PASS"
    )
    stale = simulation.parse_simulation_result(
        common
        + f"\nsource_snapshot_after={changed_after}\nlog_begin=__VMCP_LOG_BEGIN__\nTB_PASS"
    )

    assert fresh["evidence_freshness"]["status"] == "FRESH"
    assert fresh["evidence_freshness"]["same_sources"] is True
    assert stale["evidence_freshness"]["status"] == "STALE"
    assert stale["evidence_freshness"]["same_sources"] is False


def test_parse_simulation_result_reports_timescale_warning_as_low_priority_diagnosis() -> None:
    raw = """
sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim
log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xsim.log
status_source=simulation_invocation_log_span
log_begin=__VMCP_LOG_BEGIN__
WARNING: [XSIM 43-4099] "top.sv" Line 1. Module top doesn't have a timescale but at least one module in design has a timescale.
TB_PASS current invocation passed
$finish called at time : 100 ns
""".strip()

    result = simulation.parse_simulation_result(raw)

    assert result["status"] == "completed"
    assert result["diagnosis"]["warnings"][0]["code"] == "MISSING_TIMESCALE"


def test_parse_simulation_result_uses_current_pass_log_after_gui_run_tcl_error() -> None:
    raw = f"""
sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim
log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xsim.log
status_source=simulation_invocation_log_span
run_tcl_failed=1
run_error={encode_wire_list(['ERROR: [Common 17-190] Invalid Tcl eval of add_bp during event processing.'])}
breakpoints_cleared=1
log_begin=__VMCP_LOG_BEGIN__
TB_PASS transitions=63
$finish called at time : 100 ns
""".strip()

    result = simulation.parse_simulation_result(raw)

    assert result["status"] == "completed"
    assert result["run_tcl_failed"] is True
    assert result["breakpoints_cleared"] is True
    assert "add_bp" in result["run_error"]
    assert result["diagnosis"]["primary_cause"] == "testbench_pass"
    assert any(item["code"] == "SIMULATION_RUN_TCL_ERROR" for item in result["diagnosis"]["warnings"])


def test_simulation_vcd_preflight_detects_testbench_dump_usage() -> None:
    raw = f"""
sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim
testbench_vcd_usage=1
testbench_vcd_sources={encode_wire_list(['D:/Vivado_Mcp/test_use/demo/sim/tb_top.sv'])}
""".strip()

    command = simulation.simulation_vcd_preflight_command(simset="sim_1")
    result = simulation.parse_simulation_vcd_preflight(raw)

    assert "$dumpfile" in command
    assert result["testbench_vcd_usage"] is True
    assert result["testbench_vcd_sources"] == ["D:/Vivado_Mcp/test_use/demo/sim/tb_top.sv"]


def test_parse_simulation_result_treats_testbench_vcd_as_info_when_deduped() -> None:
    vcd_row = encode_wire_row({'path': 'D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/tb.vcd', 'size_bytes': '128'})
    raw = f"""
sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim
log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xsim.log
vcd_conflict=1
vcd_export_mode=testbench_existing
mcp_vcd_export_mode=testbench_existing
testbench_vcd_usage=1
testbench_vcd_detected=1
vcd_files={encode_wire_list([vcd_row])}
log_begin=__VMCP_LOG_BEGIN__
INFO: [XSIM 43-3496] Simulation finished
""".strip()

    result = simulation.parse_simulation_result(raw)

    assert result["status"] == "completed"
    assert result["vcd_conflict"] is True
    assert result["vcd_conflict_severity"] == "info"
    assert result["testbench_vcd_detected"] is True
    assert result["diagnosis"]["primary_cause"] == "completed_with_testbench_vcd"
    assert result["diagnosis"]["info"][0]["code"] == "TESTBENCH_EXISTING_VCD"
    assert "vcd_conflict" not in result["diagnosis"]["causes"]
