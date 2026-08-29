import hashlib
import json
import tkinter
from pathlib import Path

import pytest

import vivado_agent_mcp.tools as tools_module
from vivado_agent_mcp.tools import VivadoToolService, _signoff_inputs_from_report_manifest
from vivado_agent_mcp.vivado.constraints import CHECK_TIMING_KEYS
from vivado_agent_mcp.vivado.prehardware import (
    REPORT_CATEGORIES,
    analyze_sources_result,
    cdc_report_command,
    check_syntax_command,
    clock_interaction_report_command,
    collect_report_bundle_files,
    compile_order_command,
    configuration_voltage_command,
    design_hierarchy_command,
    evaluate_pre_hw_signoff,
    parse_cdc_report,
    parse_clock_interaction_report,
    parse_compile_order,
    parse_configuration_voltage,
    parse_design_hierarchy,
    parse_elaboration_result,
    parse_power_report,
    parse_report_bundle_context,
    parse_syntax_report,
    power_report_command,
    report_bundle_command,
    run_elaboration_command,
)
from vivado_agent_mcp.vivado.wire import encode_wire_list, encode_wire_row


_TEST_DESIGN_IDENTITY = {
    "status": "READY",
    "schema_version": 1,
    "sha256": "d" * 64,
    "files": [],
}


def _attested_report(report_type: str, begin_marker: str, body: str) -> str:
    report_command = {
        "check_timing": "check_timing",
        "drc": "report_drc",
        "methodology": "report_methodology",
        "timing_summary": "report_timing_summary",
    }.get(report_type, report_type)
    return (
        "vmcp_report_ok=1\n"
        f"vmcp_report_type={report_type}\n"
        "vmcp_vivado_version_short=2021.2\n"
        "vmcp_vivado_build=Vivado v2021.2\n"
        f"vmcp_report_command={report_command}\n"
        "vmcp_parser_schema_version=vivado_2021_2_v1\n"
        f"vmcp_report_bytes={len(body.encode('utf-8'))}\n"
        f"vmcp_report_begin={begin_marker}\n"
        f"{body}\n"
        f"vmcp_report_end={begin_marker.replace('_BEGIN__', '_END__')}"
    )


def _complete_check_timing_body(**overrides: int) -> str:
    counts = {key: 0 for key in CHECK_TIMING_KEYS} | overrides
    return "check_timing report\n" + "\n".join(
        f"checking {key} ({counts[key]})" for key in CHECK_TIMING_KEYS
    )


def _syntax_raw(*, status: str = "READY", body: str = "") -> str:
    return (
        f"status={status}\n"
        "fileset=sources_1\n"
        "raw_begin=__VMCP_SYNTAX_REPORT_BEGIN__\n"
        f"{body}\n"
        "raw_end=__VMCP_SYNTAX_REPORT_END__"
    )


def _elaboration_raw(*, status: str = "READY", body: str = "") -> str:
    return (
        f"status={status}\n"
        "top=top\n"
        "part=xc7a35tcpg236-1\n"
        "raw_begin=__VMCP_ELABORATION_REPORT_BEGIN__\n"
        f"{body}\n"
        "raw_end=__VMCP_ELABORATION_REPORT_END__"
    )


def _compile_order_raw(*, missing: bool = False, duplicate: bool = False) -> str:
    rows = [encode_wire_row({'file': 'D:/rtl/top.v', 'type': 'Verilog', 'exists': '1', 'managed': '0', 'used_in': 'synthesis', 'order': '0'})]
    if missing:
        rows.append(encode_wire_row({'file': 'D:/rtl/missing.v', 'type': 'Verilog', 'exists': '0', 'managed': '0', 'used_in': 'synthesis', 'order': '1'}))
    if duplicate:
        rows.append(encode_wire_row({'duplicate': 'D:/rtl/top.v'}))
    file_count = 1 + int(missing)
    return (
        "status=READY\n"
        "compile_order_schema=vivado_2021_2_v1\n"
        "compile_order_complete=1\n"
        f"compile_order_count={file_count}\n"
        "fileset=sources_1\n"
        "top=top\n"
        "raw_begin=__VMCP_COMPILE_ORDER_BEGIN__\n"
        + "\n".join(rows)
        + "\nraw_end=__VMCP_COMPILE_ORDER_END__"
    )


class FakeSession:
    def __init__(self, responses: list[dict] | None = None, raw: str = "", ok: bool = True) -> None:
        self.commands: list[str] = []
        self.responses = list(responses or [])
        self.raw = raw
        self.ok = ok
        self.design_execution_identity = dict(_TEST_DESIGN_IDENTITY)

    def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
        self.commands.append(command)
        if self.responses:
            return self.responses.pop(0)
        return {"ok": self.ok, "raw": self.raw}


def _fresh_report_context(report_dir: Path, *, collection_id: str = "report_test") -> dict[str, object]:
    filenames = {
        "timing": "timing_summary.rpt",
        "utilization": "utilization.rpt",
        "drc": "drc.rpt",
        "methodology": "methodology.rpt",
        "qor": "qor_summary.rpt",
        "cdc": "cdc.rpt",
        "clock_interaction": "clock_interaction.rpt",
        "power": "power.rpt",
        "messages": "messages.log",
    }
    present = [report_dir / filename for filename in filenames.values() if (report_dir / filename).is_file()]
    started_ms = min((int(path.stat().st_mtime * 1000) for path in present), default=1000) - 100
    reports_root = next(parent for parent in (report_dir, *report_dir.parents) if parent.name == "vmcp_reports")
    project_dir = reports_root.parent
    run_directory = project_dir / "demo.runs" / "impl_1"
    run_directory.mkdir(parents=True, exist_ok=True)
    run_log = run_directory / "runme.log"
    run_log.write_text("INFO: test run complete\n", encoding="utf-8")
    context = {
        "vivado_version_short": "2021.2",
        "vivado_build": "Vivado v2021.2",
        "report_command_schema": "vivado_2021_2_v1",
        "collection_id": collection_id,
        "collection_started_ms": str(started_ms),
        "open_run_status": "generated",
        "run_status": "write_bitstream Complete!",
        "run_progress": "100%",
        "run_needs_refresh": "0",
        "run_directory": str(run_directory),
        "session_generation_id": "test-generation",
        "run_log_path": str(run_log),
        "run_log_size_before": str(run_log.stat().st_size),
        "run_log_size_after": str(run_log.stat().st_size),
        "run_log_mtime_before": str(int(run_log.stat().st_mtime)),
        "run_log_mtime_after": str(int(run_log.stat().st_mtime)),
        "messages_complete_scan": "1",
        "messages_source_stable": "1",
        "messages_extracted_count": "0",
        "design_execution_identity": dict(_TEST_DESIGN_IDENTITY),
    }
    for category, filename in filenames.items():
        exists = (report_dir / filename).is_file()
        context[f"{category}_report_command_status"] = (
            "generated" if exists else "unavailable" if category in {"qor", "cdc", "clock_interaction", "power"} else "failed"
        )
        context[f"{category}_report_command_message"] = "test report collection"
    return context


def _run_configuration_raw(context: dict[str, str]) -> str:
    return (
        "name=impl_1\n"
        "status=write_bitstream Complete!\n"
        "progress=100%\n"
        "needs_refresh=0\n"
        f"directory={context['run_directory']}\n"
        f"session_generation_id={context.get('session_generation_id', '')}\n"
        "properties_begin=__VMCP_RUN_PROPERTIES_BEGIN__\n"
    )


def _fresh_simulation_raw(*, invocation_id: str = "sim-123", status: str = "Simulation finished") -> str:
    return (
        "sim_dir=D:/sim\n"
        "project_dir_before=D:/project\n"
        "project_dir_after=D:/project\n"
        "project_name_before=demo\n"
        "project_name_after=demo\n"
        "simset_before=sim_1\n"
        "simset_after=sim_1\n"
        "sim_top_before=tb_top\n"
        "sim_top_after=tb_top\n"
        f"source_snapshot_before={encode_wire_list(['D:/project/tb.sv|100|1'])}\n"
        f"source_snapshot_after={encode_wire_list(['D:/project/tb.sv|100|1'])}\n"
        "status_source=simulation_invocation_log_span\n"
        f"simulation_invocation_id={invocation_id}\n"
        "ended_at=2026-06-11T00:00:00Z\n"
        "log_span_start=0\n"
        "log_span_end=128\n"
        "log_span_reset_detected=0\n"
        "log_begin=__VMCP_LOG_BEGIN__\n"
        f"{status}"
    )


def _signoff_setup_responses() -> list[dict]:
    return [
        {"ok": True, "raw": _syntax_raw()},
        {"ok": True, "raw": _compile_order_raw()},
        {"ok": True, "raw": _elaboration_raw()},
        {"ok": True, "raw": ""},
    ]


def test_prehardware_command_builders_use_vivado_native_commands() -> None:
    syntax = check_syntax_command(fileset="sources_1")
    order = compile_order_command(fileset="sim_1")
    elaboration = run_elaboration_command(top="top$设计", part="xc7a35tcpg236-1")
    hierarchy = design_hierarchy_command()
    cdc = cdc_report_command()
    clock = clock_interaction_report_command()
    power = power_report_command()
    config_voltage = configuration_voltage_command()
    bundle = report_bundle_command(run_name="impl_1")

    assert "check_syntax -fileset {sources_1}" in syntax
    assert "syntax_status=" in syntax
    assert "get_files -compile_order sources" in order
    assert "fileset={sim_1}" in order
    assert "synth_design -rtl" in elaboration
    assert "set elab_code [catch" in elaboration
    assert "set elab_raw [string range $elab_result 0 65535]" in elaboration
    assert 'else {set elab_raw ""}' in elaboration
    assert "-top {top$设计}" in elaboration
    assert "-part {xc7a35tcpg236-1}" in elaboration
    assert "get_cells -hierarchical" in hierarchy
    assert "get_ports -quiet *" in hierarchy
    assert "info commands report_cdc" in cdc
    assert "info commands report_clock_interaction" in clock
    assert "info commands report_power" in power
    assert "get_property CFGBVS" in config_voltage
    assert "get_property CONFIG_VOLTAGE" in config_voltage
    assert "vmcp_reports" in bundle
    assert "report_timing_summary" in bundle


def test_prehardware_parsers_extract_blocking_and_warning_statuses() -> None:
    syntax = parse_syntax_report(_syntax_raw(status="BLOCK", body="ERROR: [VRFC 10-2063] syntax error"))
    order = parse_compile_order(_compile_order_raw(missing=True, duplicate=True))
    elaboration = parse_elaboration_result(
        _elaboration_raw(
            status="BLOCK",
            body=(
                "ERROR: [Synth 8-439] module 'missing_child' not found\n"
                "WARNING: black box cell u_ip\nwidth mismatch on port data"
            ),
        )
    )
    hierarchy = parse_design_hierarchy(
        "\n".join(
            [
                encode_wire_row({'port': 'clk', 'direction': 'IN', 'width': '1'}),
                encode_wire_row({'cell': 'top/u0', 'ref': 'counter', 'primitive': '0'}),
                encode_wire_row({'cell': 'top/LUT0', 'ref': 'LUT6', 'primitive': '1'}),
            ]
        )
    )
    cdc = parse_cdc_report("Unsafe: 1\nUnknown: 2\nSafe: 5\nCRITICAL WARNING: CDC path")
    clock = parse_clock_interaction_report("Unsafe: 0\nUnknown: 2\nSafe: 3")
    power = parse_power_report("Total On-Chip Power (W): 0.125\nDynamic (W): 0.100\nStatic Power (W): 0.025")
    vivado_power = parse_power_report(
        "| Total On-Chip Power (W) | 0.073 |\n"
        "| Dynamic (W)            | 0.001 |\n"
        "| Device Static (W)      | 0.072 |\n"
    )
    power_unavailable = parse_power_report("power_unavailable=1\nmessage=report_power is unavailable")
    config_voltage = parse_configuration_voltage("cfgbvs=VCCO\nconfig_voltage=3.3")
    missing_config_voltage = parse_configuration_voltage("cfgbvs=\nconfig_voltage=")

    assert syntax["status"] == "BLOCK"
    assert syntax["counts"]["ERROR"] == 1
    assert order["status"] == "BLOCK"
    assert order["missing_files"] == ["D:/rtl/missing.v"]
    assert order["duplicates"] == ["D:/rtl/top.v"]
    assert elaboration["status"] == "BLOCK"
    assert elaboration["unresolved_modules"] == ["missing_child"]
    assert elaboration["black_box_count"] == 1
    assert elaboration["width_mismatch_count"] == 1
    assert hierarchy["ports"][0]["name"] == "clk"
    assert hierarchy["cell_counts"]["primitive"] == 1
    assert cdc["status"] == "BLOCK"
    assert cdc["counts"]["unsafe"] == 1
    assert clock["status"] == "WARN"
    assert clock["counts"]["unknown"] == 2
    assert power["available"] is True
    assert power["total_on_chip_w"] == 0.125
    assert vivado_power["total_on_chip_w"] == 0.073
    assert vivado_power["dynamic_w"] == 0.001
    assert vivado_power["static_w"] == 0.072
    assert power_unavailable["available"] is False
    assert power_unavailable["status"] == "WARN"
    assert config_voltage["status"] == "READY"
    assert missing_config_voltage["status"] == "WARN"
    assert "CFGBVS/CONFIG_VOLTAGE" in missing_config_voltage["warnings"][0]


def test_unknown_cdc_clock_interaction_and_power_reports_fail_closed() -> None:
    cdc = parse_cdc_report("opaque CDC output")
    clock = parse_clock_interaction_report("opaque clock interaction output")
    power = parse_power_report("opaque power output")

    for result in (cdc, clock, power):
        assert result["parsed"] is False
        assert result["complete"] is False
        assert result["status"] == "BLOCK"


def test_empty_source_evidence_fails_closed() -> None:
    syntax = parse_syntax_report("")
    order = parse_compile_order("")
    elaboration = parse_elaboration_result("")
    sources = analyze_sources_result(syntax=syntax, compile_order=order)

    for result in (syntax, order, elaboration):
        assert result["parsed"] is False
        assert result["complete"] is False
        assert result["status"] == "BLOCK"
    assert sources["status"] == "BLOCK"
    assert "SOURCE_EVIDENCE_INCOMPLETE" in sources["error_codes"]


def test_signoff_preserves_opaque_cdc_and_clock_parser_blocks() -> None:
    signoff = evaluate_pre_hw_signoff(
        sources={"status": "READY", "reasons": [], "warnings": []},
        elaboration={"status": "READY", "unresolved_modules": [], "black_box_count": 0, "width_mismatch_count": 0},
        simulation={
            "status": "PASSED",
            "status_source": "simulation_invocation_log_span",
            "simulation_invocation_id": "sim-123",
            "ended_at": "2026-07-17T00:00:00Z",
            "log_span": {"start": 0, "end": 1, "reset_detected": False},
            "source_tool": "run_behavioral_simulation",
            "evidence_freshness": {"status": "FRESH", "same_project": True, "same_simset": True, "same_sources": True},
        },
        readiness={"status": "READY", "reasons": [], "warnings": []},
        cdc=parse_cdc_report("opaque CDC output"),
        clock_interaction=parse_clock_interaction_report("opaque clock interaction output"),
        power={"status": "READY", "available": True},
    )

    assert signoff["status"] == "BLOCK"
    assert "CDC status is BLOCK" in signoff["reasons"]
    assert "clock interaction status is BLOCK" in signoff["reasons"]


def test_vivado_2021_empty_cdc_and_clean_clock_interaction_reports_are_complete() -> None:
    cdc = parse_cdc_report(
        """
| Tool Version : Vivado v.2021.2
| Command      : report_cdc -file D:/project/cdc.rpt -quiet
| Design       : top
CDC Report
""".strip()
    )
    clock = parse_clock_interaction_report(
        """
Clock Interaction Report
Clock Interaction Table
From Clock    To Clock      Clock Edges  WNS(ns)  TNS(ns)  Endpoints  Classification       Constraints
------------  ------------  -----------  -------  -------  ---------  -------------------  -----------
sys_clk       sys_clk       rise - rise     7.41     0.00         33  Clean                Timed
""".strip()
    )

    assert cdc["parsed"] is True
    assert cdc["complete"] is True
    assert cdc["counts"] == {"unsafe": 0, "unknown": 0, "safe": 0}
    assert cdc["status"] == "READY"
    assert clock["parsed"] is True
    assert clock["complete"] is True
    assert clock["counts"] == {"unsafe": 0, "unknown": 0, "safe": 1}
    assert clock["status"] == "READY"


def test_analyze_sources_and_pre_hw_signoff_classify_ready_warn_block() -> None:
    blocked_sources = analyze_sources_result(
        syntax={"status": "BLOCK", "counts": {"ERROR": 1, "CRITICAL WARNING": 0, "WARNING": 0}},
        compile_order={"status": "READY", "missing_files": [], "duplicates": [], "unknown_file_types": []},
    )
    warned_sources = analyze_sources_result(
        syntax={"status": "READY", "counts": {"ERROR": 0, "CRITICAL WARNING": 0, "WARNING": 1}},
        compile_order={"status": "READY", "missing_files": [], "duplicates": [], "unknown_file_types": []},
    )
    signoff = evaluate_pre_hw_signoff(
        sources=warned_sources,
        elaboration={"status": "READY", "unresolved_modules": [], "black_box_count": 0, "width_mismatch_count": 0},
        simulation={"status": "PASSED"},
        readiness={"status": "READY", "reasons": [], "warnings": []},
        cdc={"status": "BLOCK", "counts": {"unsafe": 2, "unknown": 0}},
        clock_interaction={"status": "WARN", "counts": {"unsafe": 0, "unknown": 1}},
        power={"status": "WARN", "available": False, "message": "unavailable"},
        report_manifest={"manifest_path": "D:/project/vmcp_reports/impl_1/report_manifest.json"},
    )

    assert blocked_sources["status"] == "BLOCK"
    assert "syntax has 1 error(s)" in blocked_sources["reasons"]
    assert warned_sources["status"] == "WARN"
    assert "syntax has 1 warning(s)" in warned_sources["warnings"]
    assert signoff["status"] == "BLOCK"
    assert "CDC reports unsafe crossings=2" in signoff["reasons"]
    assert "clock interaction has unknown crossings=1" in signoff["warnings"]
    assert signoff["report_manifest_path"].endswith("report_manifest.json")
    assert signoff["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert signoff["hardware_validation"]["real_board_required"] is True
    assert "no real FPGA board" in signoff["hardware_validation"]["message"]


def test_pre_hw_signoff_next_steps_route_findings_to_mcp_tools() -> None:
    signoff = evaluate_pre_hw_signoff(
        sources={
            "status": "BLOCK",
            "reasons": ["syntax has 1 error(s)", "missing source file: D:/rtl/missing.v"],
            "warnings": ["unknown source file type: D:/rtl/generated.sv"],
        },
        elaboration={
            "status": "BLOCK",
            "unresolved_modules": ["missing_child"],
            "black_box_count": 1,
            "width_mismatch_count": 0,
        },
        simulation={"status": "FAILED"},
        readiness={"status": "BLOCK", "reasons": ["timing is not met", "DRC report has ERROR"], "warnings": []},
        cdc={"status": "BLOCK", "counts": {"unsafe": 2, "unknown": 0}},
        clock_interaction={"status": "WARN", "counts": {"unsafe": 0, "unknown": 1}},
        power={"status": "WARN", "available": False, "message": "report_power is unavailable"},
        report_manifest={"manifest_path": "D:/project/vmcp_reports/impl_1/report_manifest.json"},
    )

    steps = "\n".join(signoff["next_steps"])

    assert signoff["status"] == "BLOCK"
    assert "analyze_sources" in steps
    assert "check_syntax" in steps
    assert "get_compile_order" in steps
    assert "run_elaboration" in steps
    assert "get_elaboration_result" in steps
    assert "run_behavioral_simulation" in steps
    assert "get_simulation_result" in steps
    assert "check_timing_constraints" in steps
    assert "analyze_timing_closure" in steps
    assert "get_drc_report" in steps
    assert "get_cdc_report" in steps
    assert "get_clock_interaction_report" in steps
    assert "get_power_report" in steps
    assert "collect_report_bundle" in steps
    action_tools = {action["tool"] for action in signoff["next_actions"]}
    assert {"analyze_sources", "check_timing_constraints", "run_behavioral_simulation"} <= action_tools
    assert all(set(action) == {"tool", "reason", "required_args", "arg_sources", "preconditions", "stop_condition", "optional"} for action in signoff["next_actions"])


def test_pre_hw_signoff_blocks_inconclusive_simulation_result() -> None:
    signoff = evaluate_pre_hw_signoff(
        sources={"status": "READY", "reasons": [], "warnings": []},
        elaboration={"status": "READY", "unresolved_modules": [], "black_box_count": 0, "width_mismatch_count": 0},
        simulation={"status": "unknown", "message": "no current XSIM log span"},
        readiness={"status": "READY", "reasons": [], "warnings": []},
        cdc={"status": "READY", "counts": {"unsafe": 0, "unknown": 0}},
        clock_interaction={"status": "READY", "counts": {"unsafe": 0, "unknown": 0}},
        power={"status": "READY", "available": True},
        report_manifest={"manifest_path": "D:/project/vmcp_reports/impl_1/report_manifest.json"},
    )

    assert signoff["status"] == "BLOCK"
    assert any("behavioral simulation result is not conclusive" in reason for reason in signoff["reasons"])
    assert any(action["tool"] == "run_behavioral_simulation" for action in signoff["next_actions"])


def test_pre_hw_signoff_blocks_completed_latest_log_tail_simulation_result() -> None:
    signoff = evaluate_pre_hw_signoff(
        sources={"status": "READY", "reasons": [], "warnings": []},
        elaboration={"status": "READY", "unresolved_modules": [], "black_box_count": 0, "width_mismatch_count": 0},
        simulation={
            "status": "completed",
            "status_source": "latest_log_tail",
            "simulation_invocation_id": "sim-old",
            "ended_at": "2026-06-11T00:00:00Z",
            "log_span": {"start": 0, "end": 1024, "reset_detected": False},
            "source_tool": "get_simulation_result",
        },
        readiness={"status": "READY", "reasons": [], "warnings": []},
        cdc={"status": "READY", "counts": {"unsafe": 0, "unknown": 0}},
        clock_interaction={"status": "READY", "counts": {"unsafe": 0, "unknown": 0}},
        power={"status": "READY", "available": True},
        report_manifest={"manifest_path": "D:/project/vmcp_reports/impl_1/report_manifest.json"},
    )

    assert signoff["status"] == "BLOCK"
    assert any("current invocation log span" in reason for reason in signoff["reasons"])
    assert any(action["tool"] == "run_behavioral_simulation" for action in signoff["next_actions"])


def test_pre_hw_signoff_blocks_run_behavioral_simulation_without_invocation_span() -> None:
    signoff = evaluate_pre_hw_signoff(
        sources={"status": "READY", "reasons": [], "warnings": []},
        elaboration={"status": "READY", "unresolved_modules": [], "black_box_count": 0, "width_mismatch_count": 0},
        simulation={"status": "completed", "source_tool": "run_behavioral_simulation"},
        readiness={"status": "READY", "reasons": [], "warnings": []},
        cdc={"status": "READY", "counts": {"unsafe": 0, "unknown": 0}},
        clock_interaction={"status": "READY", "counts": {"unsafe": 0, "unknown": 0}},
        power={"status": "READY", "available": True},
        report_manifest={"manifest_path": "D:/project/vmcp_reports/impl_1/report_manifest.json"},
    )

    assert signoff["status"] == "BLOCK"
    assert any("current invocation log span" in reason for reason in signoff["reasons"])


def test_pre_hw_signoff_does_not_accept_ready_or_ok_as_simulation_pass() -> None:
    for status in ("READY", "OK"):
        signoff = evaluate_pre_hw_signoff(
            sources={"status": "READY", "reasons": [], "warnings": []},
            elaboration={"status": "READY", "unresolved_modules": [], "black_box_count": 0, "width_mismatch_count": 0},
            simulation={
                "status": status,
                "status_source": "simulation_invocation_log_span",
                "simulation_invocation_id": "sim-123",
                "ended_at": "2026-06-11T00:00:00Z",
                "log_span": {"start": 0, "end": 1024, "reset_detected": False},
                "source_tool": "get_simulation_result",
                "evidence_freshness": {
                    "status": "FRESH",
                    "same_project": True,
                    "same_simset": True,
                    "same_sources": True,
                },
            },
            readiness={"status": "READY", "reasons": [], "warnings": []},
            cdc={"status": "READY", "counts": {"unsafe": 0, "unknown": 0}},
            clock_interaction={"status": "READY", "counts": {"unsafe": 0, "unknown": 0}},
            power={"status": "READY", "available": True},
            report_manifest={"manifest_path": "D:/project/vmcp_reports/impl_1/report_manifest.json"},
        )

        assert signoff["status"] == "BLOCK"
        assert any("not conclusive" in reason for reason in signoff["reasons"])


def test_pre_hw_signoff_blocks_stale_simulation_even_with_completed_log_span() -> None:
    signoff = evaluate_pre_hw_signoff(
        sources={"status": "READY", "reasons": [], "warnings": []},
        elaboration={"status": "READY", "unresolved_modules": [], "black_box_count": 0, "width_mismatch_count": 0},
        simulation={
            "status": "completed",
            "status_source": "simulation_invocation_log_span",
            "simulation_invocation_id": "sim-stale",
            "ended_at": "2026-06-11T00:00:00Z",
            "log_span": {"start": 0, "end": 128},
            "source_tool": "run_behavioral_simulation",
            "evidence_freshness": {
                "status": "STALE",
                "same_project": True,
                "same_simset": True,
                "same_sources": False,
            },
        },
        readiness={"status": "READY", "reasons": [], "warnings": []},
        cdc={"status": "READY", "counts": {"unsafe": 0, "unknown": 0}},
        clock_interaction={"status": "READY", "counts": {"unsafe": 0, "unknown": 0}},
        power={"status": "READY", "available": True},
        report_manifest={"manifest_path": "D:/project/vmcp_reports/impl_1/report_manifest.json"},
    )

    assert signoff["status"] == "BLOCK"
    assert any("current invocation log span" in reason for reason in signoff["reasons"])


def test_pre_hw_signoff_surfaces_configuration_voltage_warning() -> None:
    signoff = evaluate_pre_hw_signoff(
        sources={"status": "READY", "reasons": [], "warnings": []},
        elaboration={"status": "READY", "unresolved_modules": [], "black_box_count": 0, "width_mismatch_count": 0},
        simulation={"status": "PASSED"},
        readiness={"status": "READY", "reasons": [], "warnings": []},
        cdc={"status": "READY", "counts": {"unsafe": 0, "unknown": 0}},
        clock_interaction={"status": "READY", "counts": {"unsafe": 0, "unknown": 0}},
        power={"status": "READY", "available": True},
        configuration_voltage=parse_configuration_voltage("cfgbvs=\nconfig_voltage="),
        report_manifest={"manifest_path": "D:/project/vmcp_reports/impl_1/report_manifest.json"},
    )

    assert signoff["status"] == "WARN"
    assert any("CFGBVS/CONFIG_VOLTAGE" in warning for warning in signoff["warnings"])
    assert signoff["configuration_voltage"]["status"] == "WARN"
    assert any(action["tool"] == "create_managed_xdc" for action in signoff["next_actions"])


def test_pre_hw_signoff_adds_io_delay_review_guidance() -> None:
    signoff = evaluate_pre_hw_signoff(
        sources={"status": "READY", "warnings": [], "reasons": []},
        elaboration={"status": "READY"},
        simulation={
            "status": "PASSED",
            "simulation_invocation_id": "sim-1",
            "ended_at": "2026-06-15T00:00:00Z",
            "log_span": {"start": 0, "end": 10},
        },
        readiness={
            "status": "WARN",
            "warnings": [
                "check_timing reports no_input_delay=1",
                "check_timing reports no_output_delay=4",
            ],
            "reasons": [],
        },
        cdc={"status": "READY", "counts": {}},
        clock_interaction={"status": "READY", "counts": {}},
        power={"status": "READY"},
    )

    guidance = signoff["warning_review_guidance"]
    assert guidance["io_delay"]["reviewable_without_board"] is True
    assert "no_input_delay=1" in guidance["io_delay"]["summary"]
    assert "no_output_delay=4" in guidance["io_delay"]["summary"]
    assert "board-level IO timing requirements" in guidance["io_delay"]["rerun_condition"]


def test_collect_report_bundle_files_writes_manifest_with_hashes(tmp_path: Path) -> None:
    report_dir = tmp_path / "project" / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    (report_dir / "timing_summary.rpt").write_text("timing", encoding="utf-8")
    (report_dir / "cdc.rpt").write_text("cdc", encoding="utf-8")
    (report_dir / "notes.tmp").write_text("ignore", encoding="utf-8")

    manifest = collect_report_bundle_files(
        report_dir=report_dir,
        run_name="impl_1",
        report_context=_fresh_report_context(report_dir),
    )

    assert manifest["run_name"] == "impl_1"
    assert manifest["status"] == "BLOCK"
    assert manifest["manifest_path"].endswith("report_manifest.json")
    assert {item["category"] for item in manifest["reports"]} == {"timing", "cdc"}
    assert manifest["expected_report_count"] == 9
    assert manifest["generated_report_count"] == 2
    assert any(item["category"] == "qor" and item["status"] == "unavailable" for item in manifest["report_status"])
    assert any(item["category"] == "qor" and "report_qor_summary" in item["reason"] for item in manifest["missing_reports"])
    assert all(item["sha256"] for item in manifest["reports"])
    assert (report_dir / "report_manifest.json").exists()


def test_strict_report_manifest_validation_rejects_self_declared_wrong_build(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    report_dir = project_dir / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    (report_dir / "timing_summary.rpt").write_text("timing", encoding="utf-8")
    manifest = collect_report_bundle_files(
        report_dir=report_dir,
        run_name="impl_1",
        project_dir=project_dir,
        report_context=_fresh_report_context(report_dir),
    )
    manifest["vivado_build"] = "Vivado v2022.1"

    with pytest.raises(tools_module.ReportManifestValidationError) as raised:
        tools_module._validated_report_manifest(
            manifest,
            manifest_path=Path(manifest["manifest_path"]),
            project_dir=project_dir,
            run_name="impl_1",
            current_design_execution_identity=manifest["design_execution_identity"],
        )

    assert raised.value.error_code == "REPORT_VERSION_MISMATCH"


def test_report_bundle_command_uses_unique_invocation_and_real_run_log(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    report_dir = project_dir / "vmcp_reports" / "impl_1" / "invocations" / "report_exact_test"
    run_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    for filename in REPORT_CATEGORIES:
        (report_dir / filename).touch()
    (run_dir / "runme.log").write_text(
        "CRITICAL WARNING: [Test 1-1] current run message\n" + ("ordinary run output without severity\n" * 40_000),
        encoding="utf-8",
    )
    interpreter = tkinter.Tcl()
    interpreter.call("set", "::project_dir", str(project_dir))
    interpreter.call("set", "::run_dir", str(run_dir))
    interpreter.eval("proc current_project {} {return project_1}")
    interpreter.eval("proc get_runs {args} {return [list run_1]}")
    interpreter.eval("proc get_filesets {args} {return [list]}")
    interpreter.eval("proc get_files {args} {return [list]}")
    interpreter.eval("proc get_ips {args} {return [list]}")
    interpreter.eval("proc get_bd_designs {args} {return [list]}")
    interpreter.eval("proc list_property {object} {return [list]}")
    interpreter.eval("proc version {args} {if {[llength $args] && [lindex $args 0] eq {-short}} {return {2021.2}}; return {Vivado v2021.2}}")
    interpreter.eval("proc open_run {name} {return {}}")
    interpreter.eval(
        """
        proc get_property {property object} {
            switch -- $property {
                DIRECTORY {
                    if {$object eq "project_1"} {return $::project_dir}
                    return $::run_dir
                }
                STATUS {return {write_bitstream Complete!}}
                PROGRESS {return {100%}}
                NEEDS_REFRESH {return 0}
                default {return {}}
            }
        }
        proc vmcp_write_report {label args} {
            set index [lsearch -exact $args -file]
            set path [lindex $args [expr {$index + 1}]]
            set fh [open $path w]
            puts $fh "$label current invocation"
            close $fh
        }
        """
    )
    for command in (
        "report_timing_summary",
        "report_utilization",
        "report_drc",
        "report_methodology",
        "report_qor_summary",
        "report_cdc",
        "report_clock_interaction",
        "report_power",
    ):
        interpreter.eval(f"interp alias {{}} {command} {{}} vmcp_write_report {command}")

    raw = interpreter.eval(
        report_bundle_command(run_name="impl_1", collection_id="report_exact_test")
    )
    context = parse_report_bundle_context(raw)
    manifest = collect_report_bundle_files(
        report_dir=context["report_dir"],
        run_name="impl_1",
        project_dir=project_dir,
        report_context=context,
        design_execution_identity=dict(_TEST_DESIGN_IDENTITY),
    )

    assert Path(context["report_dir"]).parent.name == "invocations"
    assert context["collection_id"] == "report_exact_test"
    assert context["messages_report_command_status"] == "generated"
    assert (Path(context["report_dir"]) / "messages.log").read_text(encoding="utf-8") == (
        "CRITICAL WARNING: [Test 1-1] current run message\n"
    )
    assert manifest["status"] == "READY"
    assert manifest["evidence_freshness"]["status"] == "FRESH"
    assert manifest["source_log_evidence"]["size"] > 1024 * 1024
    assert manifest["source_log_evidence"]["complete_scan"] is True
    assert manifest["source_log_evidence"]["messages_extracted_count"] == 1
    assert manifest["generated_report_count"] == 9


def test_report_bundle_rejects_old_files_when_current_commands_fail(tmp_path: Path) -> None:
    report_dir = tmp_path / "project" / "vmcp_reports" / "impl_1" / "invocations" / "report_failed"
    report_dir.mkdir(parents=True)
    old_report = report_dir / "timing_summary.rpt"
    old_report.write_text("old timing report", encoding="utf-8")
    context = _fresh_report_context(report_dir, collection_id="report_failed")
    context["collection_started_ms"] = str(int(old_report.stat().st_mtime * 1000) + 60_000)
    context["timing_report_command_status"] = "failed"
    context["timing_report_command_message"] = "report_timing_summary failed"

    manifest = collect_report_bundle_files(
        report_dir=report_dir,
        run_name="impl_1",
        project_dir=report_dir.parents[3],
        report_context=context,
    )

    assert manifest["status"] == "BLOCK"
    assert manifest["reports"] == []
    timing = next(item for item in manifest["report_status"] if item["category"] == "timing")
    assert timing["present"] is True
    assert timing["current"] is False
    assert timing["status"] == "stale"
    assert manifest["evidence_freshness"]["status"] == "STALE"


def test_report_bundle_manifest_is_stale_for_unsupported_vivado_version(tmp_path: Path) -> None:
    report_dir = tmp_path / "project" / "vmcp_reports" / "impl_1" / "invocations" / "unsupported_version"
    report_dir.mkdir(parents=True)
    for filename in ("timing_summary.rpt", "utilization.rpt", "drc.rpt", "methodology.rpt", "messages.log"):
        (report_dir / filename).write_text("current report\n", encoding="utf-8")
    context = _fresh_report_context(report_dir, collection_id="unsupported_version")
    context["vivado_version_short"] = "2024.2"

    manifest = collect_report_bundle_files(
        report_dir=report_dir,
        run_name="impl_1",
        project_dir=report_dir.parents[3],
        report_context=context,
    )

    assert manifest["status"] == "BLOCK"
    assert manifest["evidence_freshness"]["status"] == "STALE"
    assert any("Vivado 2021.2" in reason for reason in manifest["evidence_freshness"]["reasons"])


def test_report_bundle_manifest_is_stale_for_mismatched_vivado_build(tmp_path: Path) -> None:
    report_dir = tmp_path / "project" / "vmcp_reports" / "impl_1" / "invocations" / "unsupported_build"
    report_dir.mkdir(parents=True)
    for filename in ("timing_summary.rpt", "utilization.rpt", "drc.rpt", "methodology.rpt", "messages.log"):
        (report_dir / filename).write_text("current report\n", encoding="utf-8")
    context = _fresh_report_context(report_dir, collection_id="unsupported_build")
    context["vivado_build"] = "Vivado v2022.1"

    manifest = collect_report_bundle_files(
        report_dir=report_dir,
        run_name="impl_1",
        project_dir=report_dir.parents[3],
        report_context=context,
    )

    assert manifest["status"] == "BLOCK"
    assert manifest["evidence_freshness"]["status"] == "STALE"
    assert any("build attestation" in reason for reason in manifest["evidence_freshness"]["reasons"])


def test_collect_report_bundle_files_includes_qor_command_status_in_missing_reason(tmp_path: Path) -> None:
    report_dir = tmp_path / "project" / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    (report_dir / "timing_summary.rpt").write_text("timing", encoding="utf-8")

    manifest = collect_report_bundle_files(
        report_dir=report_dir,
        run_name="impl_1",
        report_context={
            **_fresh_report_context(report_dir),
            "qor_report_command_status": "unavailable",
            "qor_report_command_message": "report_qor_summary command is unavailable in this Vivado version",
        },
    )

    qor_missing = next(item for item in manifest["missing_reports"] if item["category"] == "qor")
    assert "status=unavailable" in qor_missing["reason"]
    assert "report_qor_summary command is unavailable" in qor_missing["reason"]
    assert qor_missing["optional_due_to_vivado_version"] is True
    assert qor_missing["severity"] == "info"
    assert qor_missing["user_message"] == "Vivado does not support report_qor_summary in this version; this is not a design failure."


def test_collect_report_bundle_files_marks_unknown_qor_missing_as_warn(tmp_path: Path) -> None:
    report_dir = tmp_path / "project" / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)

    manifest = collect_report_bundle_files(report_dir=report_dir, run_name="impl_1")

    qor_missing = next(item for item in manifest["missing_reports"] if item["category"] == "qor")
    assert qor_missing["optional"] is True
    assert qor_missing["optional_due_to_vivado_version"] is False
    assert qor_missing["severity"] == "warn"


def test_collect_report_bundle_files_rejects_report_dir_outside_project(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    report_dir = tmp_path / "outside_reports"
    report_dir.mkdir(parents=True)
    (report_dir / "timing_summary.rpt").write_text("timing", encoding="utf-8")

    with pytest.raises(ValueError, match="outside project directory"):
        collect_report_bundle_files(report_dir=report_dir, run_name="impl_1", project_dir=project_dir)

    assert not (report_dir / "report_manifest.json").exists()


def test_prehardware_tools_return_structured_content(tmp_path: Path) -> None:
    report_dir = tmp_path / "project" / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    (report_dir / "timing_summary.rpt").write_text("timing", encoding="utf-8")
    fake = FakeSession(
        responses=[
            {"ok": True, "raw": _syntax_raw()},
            {
                "ok": True,
                "raw": (
                    f"run_name=impl_1\nproject_dir={report_dir.parent.parent}\nreport_dir={report_dir}\n"
                    + "\n".join(
                        f"{key}={value}"
                        for key, value in _fresh_report_context(report_dir).items()
                        if key != "collection_id"
                    )
                ),
            },
        ]
    )
    service = VivadoToolService(session=fake)

    syntax = service.call("check_syntax", {"fileset": "sources_1"})
    bundle = service.call("collect_report_bundle", {"run_name": "impl_1"})

    assert syntax["ok"] is True
    assert syntax["data"]["status"] == "READY"
    assert "check_syntax -fileset {sources_1}" in fake.commands[0]
    assert bundle["ok"] is True
    assert bundle["data"]["manifest_path"].endswith("report_manifest.json")
    assert "vmcp_reports" in fake.commands[1]


def test_collect_report_bundle_treats_vmcp_reports_argument_as_run_scoped_base_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    report_root = project_dir / "vmcp_reports"
    report_dir = report_root / "impl_1"
    report_dir.mkdir(parents=True)
    (report_dir / "timing_summary.rpt").write_text("timing", encoding="utf-8")
    fake = FakeSession(
        raw=(
            f"run_name=impl_1\nproject_dir={project_dir}\nreport_dir={report_dir}\n"
            + "\n".join(
                f"{key}={value}"
                for key, value in _fresh_report_context(report_dir).items()
                if key != "collection_id"
            )
        )
    )
    service = VivadoToolService(session=fake)

    result = service.call("collect_report_bundle", {"run_name": "impl_1", "report_dir": str(report_root)})

    assert result["ok"] is True
    assert result["data"]["report_dir"] == str(report_dir.resolve())
    assert len(result["data"]["reports"]) == 1
    assert Path(result["data"]["manifest_path"]) == report_dir.resolve() / "report_manifest.json"


def test_collect_report_bundle_tool_rejects_report_dir_outside_project(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    report_dir = project_dir / "vmcp_reports" / "impl_1"
    outside_report_dir = tmp_path / "outside_reports"
    report_dir.mkdir(parents=True)
    outside_report_dir.mkdir(parents=True)
    (outside_report_dir / "timing_summary.rpt").write_text("timing", encoding="utf-8")
    fake = FakeSession(raw=f"run_name=impl_1\nproject_dir={project_dir}\nreport_dir={report_dir}")
    service = VivadoToolService(session=fake)

    result = service.call("collect_report_bundle", {"run_name": "impl_1", "report_dir": str(outside_report_dir)})

    assert result["ok"] is False
    assert result["tool"] == "collect_report_bundle"
    assert result["error_code"] == "REPORT_DIR_CONTEXT_MISMATCH"
    assert "current invocation" in result["message"]
    assert result["data"]["current_invocation_report_dir"] == str(report_dir)
    assert not (outside_report_dir / "report_manifest.json").exists()


def test_collect_report_bundle_never_redirects_to_stale_project_local_directory(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    current_dir = project_dir / "vmcp_reports" / "impl_1" / "invocations" / "current"
    stale_dir = project_dir / "vmcp_reports" / "impl_1" / "stale"
    current_dir.mkdir(parents=True)
    stale_dir.mkdir(parents=True)
    (stale_dir / "timing_summary.rpt").write_text("old report", encoding="utf-8")
    fake = FakeSession(raw=f"run_name=impl_1\nproject_dir={project_dir}\nreport_dir={current_dir}")

    result = VivadoToolService(session=fake).call(
        "collect_report_bundle",
        {"run_name": "impl_1", "report_dir": str(stale_dir)},
    )

    assert result["ok"] is False
    assert result["error_code"] == "REPORT_DIR_CONTEXT_MISMATCH"
    assert not (stale_dir / "report_manifest.json").exists()


def test_run_pre_hw_signoff_opens_impl_run_before_readiness(tmp_path: Path) -> None:
    report_dir = tmp_path / "project" / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    (report_dir / "timing_summary.rpt").write_text("timing", encoding="utf-8")
    fake = FakeSession(
        responses=[
            {"ok": True, "raw": _syntax_raw()},
            {"ok": True, "raw": _compile_order_raw()},
            {"ok": True, "raw": _elaboration_raw()},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": "cfgbvs=VCCO\nconfig_voltage=3.3"},
            {"ok": True, "raw": _attested_report("timing_summary", "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__", "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.100 0.000")},
            {"ok": True, "raw": _attested_report('drc', '__VMCP_DRC_REPORT_BEGIN__', 'DRC report: no violations')},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": _attested_report('check_timing', '__VMCP_CHECK_TIMING_REPORT_BEGIN__', _complete_check_timing_body())},
            {"ok": True, "raw": _attested_report('methodology', '__VMCP_METHODOLOGY_REPORT_BEGIN__', 'Methodology report: no violations')},
            {"ok": True, "raw": "Unsafe: 0\nUnknown: 0\nSafe: 1"},
            {"ok": True, "raw": "Unsafe: 0\nUnknown: 0\nSafe: 1"},
            {"ok": True, "raw": "Total On-Chip Power (W): 0.125"},
            {"ok": True, "raw": f"run_name=impl_1\nreport_dir={report_dir}"},
            {
                "ok": True,
                "raw": _fresh_simulation_raw(),
            },
        ]
    )
    service = VivadoToolService(session=fake)

    result = service.call("run_pre_hw_signoff", {"run_name": "impl_1"})

    assert result["ok"] is True
    assert result["data"]["status"] == "READY"
    assert result["data"]["next_actions"][0]["tool"] == "collect_diagnostic_bundle"
    assert any("open_run {impl_1}" in command for command in fake.commands)
    open_run_index = next(index for index, command in enumerate(fake.commands) if "open_run {impl_1}" in command)
    assert open_run_index < next(
        index for index, command in enumerate(fake.commands) if "report_timing_summary" in command
    )


def test_run_pre_hw_signoff_timeout_returns_partial_handoff_context(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project_path = project_dir / "demo.xpr"
    project_path.write_text("# project\n", encoding="utf-8")
    report_manifest = project_dir / "vmcp_reports" / "impl_1" / "report_manifest.json"
    report_manifest.parent.mkdir(parents=True)
    report_manifest.write_text("{}", encoding="utf-8")

    class SignoffTimeoutSession(FakeSession):
        def status(self) -> dict:
            return {
                "connected": False,
                "running": True,
                "runtime_dir": str(tmp_path / "runtime"),
                "stdout_path": "",
                "stderr_path": "",
            }

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "open_run {impl_1}" in command:
                raise TimeoutError("open_run timed out")
            if self.responses:
                return self.responses.pop(0)
            return {"ok": True, "raw": ""}

    fake = SignoffTimeoutSession(
        responses=[
            {"ok": True, "raw": _syntax_raw()},
            {"ok": True, "raw": _compile_order_raw()},
            {"ok": True, "raw": _elaboration_raw()},
        ]
    )
    service = VivadoToolService(session=fake)

    result = service.call(
        "run_pre_hw_signoff",
        {
            "run_name": "impl_1",
            "project_dir": str(project_dir),
            "project_path": str(project_path),
            "report_manifest_path": str(report_manifest),
            "timeout_s": 180,
        },
    )

    assert result["ok"] is False
    assert result["error_code"] == "TimeoutError"
    data = result["data"]
    assert data["current_step"] == "open_run"
    assert data["partial_success"] is True
    assert data["completed_steps"] == ["analyze_sources", "run_elaboration"]
    assert data["partial_evidence"]["sources"]["status"] == "READY"
    assert data["partial_evidence"]["elaboration"]["status"] == "READY"
    assert data["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert data["project_path"] == str(project_path)
    action_tools = [action["tool"] for action in result["next_actions"]]
    assert action_tools[:3] == ["session_status", "stop_session", "start_session"]
    assert "open_project" in action_tools
    assert "collect_report_bundle" not in action_tools
    retry = next(action for action in result["next_actions"] if action["tool"] == "run_pre_hw_signoff")
    assert "report_manifest_path" in retry["required_args"]
    assert retry["arg_sources"]["report_manifest_path"] == str(report_manifest)
    assert result["hardware_validation"]["status"] == "NOT_VALIDATED"


def test_run_pre_hw_signoff_timeout_without_manifest_recommends_report_bundle(tmp_path: Path) -> None:
    class SignoffTimeoutSession(FakeSession):
        def status(self) -> dict:
            return {"connected": True, "running": True, "runtime_dir": str(tmp_path / "runtime")}

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "open_run {impl_1}" in command:
                raise TimeoutError("open_run timed out")
            if self.responses:
                return self.responses.pop(0)
            return {"ok": True, "raw": ""}

    fake = SignoffTimeoutSession(
        responses=[
            {"ok": True, "raw": _syntax_raw()},
            {"ok": True, "raw": _compile_order_raw()},
            {"ok": True, "raw": _elaboration_raw()},
        ]
    )
    service = VivadoToolService(session=fake)

    result = service.call("run_pre_hw_signoff", {"run_name": "impl_1", "timeout_s": 180})

    assert result["ok"] is False
    action_tools = [action["tool"] for action in result["next_actions"]]
    assert "collect_report_bundle" in action_tools
    retry = next(action for action in result["next_actions"] if action["tool"] == "run_pre_hw_signoff")
    assert retry["required_args"] == ["run_name"]


def test_run_pre_hw_signoff_blocks_latest_log_tail_simulation_result(tmp_path: Path) -> None:
    report_dir = tmp_path / "project" / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    (report_dir / "timing_summary.rpt").write_text("timing", encoding="utf-8")
    fake = FakeSession(
        responses=[
            {"ok": True, "raw": _syntax_raw()},
            {"ok": True, "raw": _compile_order_raw()},
            {"ok": True, "raw": _elaboration_raw()},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": "cfgbvs=VCCO\nconfig_voltage=3.3"},
            {"ok": True, "raw": _attested_report("timing_summary", "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__", "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.100 0.000")},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": "Unsafe: 0\nUnknown: 0\nSafe: 1"},
            {"ok": True, "raw": "Unsafe: 0\nUnknown: 0\nSafe: 1"},
            {"ok": True, "raw": "Total On-Chip Power (W): 0.125"},
            {"ok": True, "raw": f"run_name=impl_1\nreport_dir={report_dir}"},
            {
                "ok": True,
                "raw": (
                    "sim_dir=D:/sim\n"
                    "status_source=latest_log_tail\n"
                    "simulation_invocation_id=sim-old\n"
                    "ended_at=2026-06-11T00:00:00Z\n"
                    "log_span_start=0\n"
                    "log_span_end=128\n"
                    "log_begin=__VMCP_LOG_BEGIN__\n"
                    "Simulation finished"
                ),
            },
        ]
    )
    service = VivadoToolService(session=fake)

    result = service.call("run_pre_hw_signoff", {"run_name": "impl_1"})

    assert result["ok"] is True
    assert result["data"]["status"] == "BLOCK"
    assert any("current invocation log span" in reason for reason in result["data"]["reasons"])


def test_run_pre_hw_signoff_blocks_unavailable_simulation_result(tmp_path: Path) -> None:
    report_dir = tmp_path / "project" / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    (report_dir / "timing_summary.rpt").write_text("timing", encoding="utf-8")
    fake = FakeSession(
        responses=[
            {"ok": True, "raw": _syntax_raw()},
            {"ok": True, "raw": _compile_order_raw()},
            {"ok": True, "raw": _elaboration_raw()},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": "cfgbvs=VCCO\nconfig_voltage=3.3"},
            {"ok": True, "raw": _attested_report("timing_summary", "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__", "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.100 0.000")},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": "Unsafe: 0\nUnknown: 0\nSafe: 1"},
            {"ok": True, "raw": "Unsafe: 0\nUnknown: 0\nSafe: 1"},
            {"ok": True, "raw": "Total On-Chip Power (W): 0.125"},
            {"ok": True, "raw": f"run_name=impl_1\nreport_dir={report_dir}"},
            {"ok": False, "raw": "ERROR: cannot read xsim.log"},
        ]
    )
    service = VivadoToolService(session=fake)

    result = service.call("run_pre_hw_signoff", {"run_name": "impl_1"})

    assert result["ok"] is True
    assert result["data"]["status"] == "BLOCK"
    assert result["data"]["inputs"]["simulation"]["error_code"] == "TCL_FAILED"
    assert any("behavioral simulation result is unavailable" in reason for reason in result["data"]["reasons"])
    assert any(action["tool"] == "run_behavioral_simulation" for action in result["next_actions"])


def test_run_pre_hw_signoff_uses_existing_report_manifest_without_rerunning_heavy_reports(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    report_dir = project_dir / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    (report_dir / "timing_summary.rpt").write_text("WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.100 0.000", encoding="utf-8")
    (report_dir / "utilization.rpt").write_text("utilization", encoding="utf-8")
    (report_dir / "drc.rpt").write_text("DRC report: no violations", encoding="utf-8")
    (report_dir / "methodology.rpt").write_text("Methodology report: no violations", encoding="utf-8")
    (report_dir / "qor_summary.rpt").write_text("Design Score: 1\nWNS: 0.100\nTNS: 0.000", encoding="utf-8")
    (report_dir / "cdc.rpt").write_text("Safe: 1\nUnsafe: 0\nUnknown: 0", encoding="utf-8")
    (report_dir / "clock_interaction.rpt").write_text("Safe: 1\nUnsafe: 0\nUnknown: 0", encoding="utf-8")
    (report_dir / "power.rpt").write_text("Total On-Chip Power (W): 0.125", encoding="utf-8")
    (report_dir / "messages.log").write_text("Vivado Agent MCP report bundle generated.", encoding="utf-8")
    report_context = _fresh_report_context(report_dir)
    manifest = collect_report_bundle_files(
        report_dir=report_dir,
        run_name="impl_1",
        project_dir=project_dir,
        report_context=report_context,
    )
    manifest_path = Path(manifest["manifest_path"])
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["status"] == "READY"
    fake = FakeSession(
        responses=[
            {"ok": True, "raw": _syntax_raw()},
            {"ok": True, "raw": _compile_order_raw()},
            {"ok": True, "raw": _elaboration_raw()},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": _run_configuration_raw(report_context)},
            {"ok": True, "raw": "cfgbvs=VCCO\nconfig_voltage=3.3"},
            {"ok": True, "raw": _attested_report('check_timing', '__VMCP_CHECK_TIMING_REPORT_BEGIN__', _complete_check_timing_body())},
                {
                    "ok": True,
                    "raw": _fresh_simulation_raw(),
                },
        ]
    )
    service = VivadoToolService(session=fake)

    result = service.call("run_pre_hw_signoff", {"run_name": "impl_1", "project_dir": str(project_dir), "report_manifest_path": str(manifest_path)})

    assert result["ok"] is True
    assert result["data"]["status"] == "READY"
    assert result["data"]["inputs"]["readiness"]["source"] == "report_bundle"
    assert result["data"]["report_bundle"]["manifest_path"] == str(manifest_path)
    assert not any("file mkdir $report_dir" in command for command in fake.commands)
    assert not any("report_cdc" in command and "-return_string" in command for command in fake.commands)
    assert not any("report_power" in command and "-return_string" in command for command in fake.commands)


def test_run_pre_hw_signoff_ignores_provided_simulation_result_and_rereads_log(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    report_dir = project_dir / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    (report_dir / "timing_summary.rpt").write_text("WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.100 0.000", encoding="utf-8")
    (report_dir / "utilization.rpt").write_text("utilization", encoding="utf-8")
    (report_dir / "drc.rpt").write_text("DRC report: no violations", encoding="utf-8")
    (report_dir / "methodology.rpt").write_text("Methodology report: no violations", encoding="utf-8")
    (report_dir / "qor_summary.rpt").write_text("Design Score: 1\nWNS: 0.100\nTNS: 0.000", encoding="utf-8")
    (report_dir / "cdc.rpt").write_text("Safe: 1\nUnsafe: 0\nUnknown: 0", encoding="utf-8")
    (report_dir / "clock_interaction.rpt").write_text("Safe: 1\nUnsafe: 0\nUnknown: 0", encoding="utf-8")
    (report_dir / "power.rpt").write_text("Total On-Chip Power (W): 0.125", encoding="utf-8")
    (report_dir / "messages.log").write_text("Vivado Agent MCP report bundle generated.", encoding="utf-8")
    report_context = _fresh_report_context(report_dir)
    manifest = collect_report_bundle_files(
        report_dir=report_dir,
        run_name="impl_1",
        project_dir=project_dir,
        report_context=report_context,
    )
    manifest_path = Path(manifest["manifest_path"])
    fake = FakeSession(
        responses=[
            {"ok": True, "raw": _syntax_raw()},
            {"ok": True, "raw": _compile_order_raw()},
            {"ok": True, "raw": _elaboration_raw()},
            {"ok": True, "raw": ""},
            {"ok": True, "raw": _run_configuration_raw(report_context)},
            {"ok": True, "raw": "cfgbvs=VCCO\nconfig_voltage=3.3"},
            {"ok": True, "raw": _attested_report('check_timing', '__VMCP_CHECK_TIMING_REPORT_BEGIN__', _complete_check_timing_body())},
            {
                "ok": True,
                "raw": _fresh_simulation_raw(invocation_id="sim-observed"),
            },
        ]
    )
    service = VivadoToolService(session=fake)
    simulation_result = {
        "status": "completed",
        "status_source": "simulation_invocation_log_span",
        "simulation_invocation_id": "sim-provided",
        "ended_at": "2026-06-11T00:00:00Z",
        "log_span": {"start": 0, "end": 128, "reset_detected": False},
        "source_tool": "run_behavioral_simulation",
    }

    rejected_provided_result = service.call(
        "run_pre_hw_signoff",
        {
            "run_name": "impl_1",
            "project_dir": str(project_dir),
            "report_manifest_path": str(manifest_path),
            "simulation_result": simulation_result,
        },
    )
    assert rejected_provided_result["ok"] is False
    assert rejected_provided_result["error_code"] == "INVALID_TOOL_ARGUMENTS"

    result = service.call(
        "run_pre_hw_signoff",
        {
            "run_name": "impl_1",
            "project_dir": str(project_dir),
            "report_manifest_path": str(manifest_path),
        },
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "READY"
    assert result["data"]["inputs"]["simulation"]["simulation_invocation_id"] == "sim-observed"
    assert result["data"]["inputs"]["simulation"].get("evidence_source") != "provided_simulation_result"
    assert any("log_begin=__VMCP_LOG_BEGIN__" in command for command in fake.commands)


def test_signoff_report_parser_validates_and_consumes_the_same_bounded_bytes(tmp_path: Path) -> None:
    report_dir = tmp_path / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    timing_path = report_dir / "timing_summary.rpt"
    original = b"WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.100 0.000"
    timing_path.write_bytes(original)
    manifest = {
        "report_dir": str(report_dir),
        "vivado_version_short": "2021.2",
        "vivado_build": "Vivado v2021.2 (64-bit)",
        "reports": [
            {
                "category": "timing",
                "path": str(timing_path),
                "size": len(original),
                "sha256": hashlib.sha256(original).hexdigest(),
            }
        ],
    }
    timing_path.write_bytes(original.replace(b"0.100", b"9.999", 1))

    parsed = _signoff_inputs_from_report_manifest(manifest)

    assert parsed["timing"]["status"] == "BLOCK"
    assert parsed["timing"]["available"] is False
    assert "changed after manifest validation" in parsed["timing"]["message"]


def test_signoff_report_parser_uses_manifest_vivado_attestation(tmp_path: Path) -> None:
    report_dir = tmp_path / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    drc_path = report_dir / "drc.rpt"
    content = b"DRC report: no violations"
    drc_path.write_bytes(content)
    manifest = {
        "report_dir": str(report_dir),
        "vivado_version_short": "2021.2",
        "vivado_build": "Vivado v2021.2 (64-bit)",
        "reports": [
            {
                "category": "drc",
                "path": str(drc_path),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }

    parsed = _signoff_inputs_from_report_manifest(manifest)

    assert parsed["drc"]["status"] == "READY"
    assert parsed["drc"]["report_attestation"]["vivado_build"] == "Vivado v2021.2 (64-bit)"
    assert parsed["drc"]["report_attestation"]["report_command"] == "report_drc"


def test_signoff_report_parser_does_not_parse_replacement_written_after_stable_read(tmp_path: Path, monkeypatch) -> None:
    report_dir = tmp_path / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    timing_path = report_dir / "timing_summary.rpt"
    original = b"WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.100 0.000"
    replacement = b"WNS(ns) TNS(ns) WHS(ns) THS(ns)\n-9.999 -99.000 0.100 0.000"
    timing_path.write_bytes(original)
    manifest = {
        "report_dir": str(report_dir),
        "vivado_version_short": "2021.2",
        "vivado_build": "Vivado v2021.2 (64-bit)",
        "reports": [
            {
                "category": "timing",
                "path": str(timing_path),
                "size": len(original),
                "sha256": hashlib.sha256(original).hexdigest(),
            }
        ],
    }
    stable_read = tools_module.read_stable_bytes

    def replace_after_read(path, *, root, max_bytes):
        content = stable_read(path, root=root, max_bytes=max_bytes)
        Path(path).write_bytes(replacement)
        return content

    monkeypatch.setattr(tools_module, "read_stable_bytes", replace_after_read)

    parsed = _signoff_inputs_from_report_manifest(manifest)

    assert parsed["timing"]["status"] == "READY"
    assert parsed["timing"]["wns_ns"] == 0.1


def test_run_pre_hw_signoff_rejects_report_manifest_outside_project(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    outside_report_dir = tmp_path / "outside_reports" / "impl_1"
    outside_report_dir.mkdir(parents=True)
    (outside_report_dir / "timing_summary.rpt").write_text("WNS: 0.100\nTNS: 0.000", encoding="utf-8")
    manifest = collect_report_bundle_files(report_dir=outside_report_dir, run_name="impl_1")
    fake = FakeSession(responses=_signoff_setup_responses())
    service = VivadoToolService(session=fake)

    result = service.call(
        "run_pre_hw_signoff",
        {"run_name": "impl_1", "project_dir": str(project_dir), "report_manifest_path": manifest["manifest_path"]},
    )

    assert result["ok"] is False
    assert result["error_code"] == "REPORT_MANIFEST_UNTRUSTED"
    assert "current project" in result["message"]
    assert result["next_actions"][0]["tool"] == "collect_report_bundle"
    assert not any("get_property CFGBVS" in command for command in fake.commands)


def test_run_pre_hw_signoff_rejects_report_manifest_entry_outside_report_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    report_dir = project_dir / "vmcp_reports" / "impl_1"
    outside_report = project_dir / "other" / "timing_summary.rpt"
    report_dir.mkdir(parents=True)
    outside_report.parent.mkdir(parents=True)
    outside_report.write_text("WNS: 0.100\nTNS: 0.000", encoding="utf-8")
    manifest_path = report_dir / "report_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_name": "impl_1",
                "report_dir": str(report_dir),
                "manifest_path": str(manifest_path),
                "reports": [
                    {
                        "path": str(outside_report),
                        "category": "timing",
                        "size": outside_report.stat().st_size,
                        "sha256": hashlib.sha256(outside_report.read_bytes()).hexdigest(),
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    fake = FakeSession(responses=_signoff_setup_responses())
    service = VivadoToolService(session=fake)

    result = service.call(
        "run_pre_hw_signoff",
        {"run_name": "impl_1", "project_dir": str(project_dir), "report_manifest_path": str(manifest_path)},
    )

    assert result["ok"] is False
    assert result["error_code"] == "REPORT_MANIFEST_UNTRUSTED"
    assert "report_dir" in result["message"]


def test_run_pre_hw_signoff_rejects_report_manifest_hash_mismatch(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    report_dir = project_dir / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    timing_report = report_dir / "timing_summary.rpt"
    timing_report.write_text("WNS: 0.100\nTNS: 0.000", encoding="utf-8")
    manifest_path = report_dir / "report_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_name": "impl_1",
                "report_dir": str(report_dir),
                "manifest_path": str(manifest_path),
                "reports": [
                    {
                        "path": str(timing_report),
                        "category": "timing",
                        "size": timing_report.stat().st_size,
                        "sha256": "0" * 64,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    fake = FakeSession(responses=_signoff_setup_responses())
    service = VivadoToolService(session=fake)

    result = service.call(
        "run_pre_hw_signoff",
        {"run_name": "impl_1", "project_dir": str(project_dir), "report_manifest_path": str(manifest_path)},
    )

    assert result["ok"] is False
    assert result["error_code"] == "REPORT_FILE_INTEGRITY_MISMATCH"
    assert "SHA256" in result["message"]


def test_validated_report_entry_parses_the_same_bounded_snapshot_it_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    report_dir = project_dir / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    report_path = report_dir / "timing_summary.rpt"
    original = b"WNS: 0.100\nTNS: 0.000\n"
    replacement = b"WNS: -9.999\nTNS: -9.999\n"
    report_path.write_bytes(original)
    manifest_path = report_dir / "report_manifest.json"
    calls: list[int] = []
    real_read = tools_module.read_stable_bytes

    def snapshot_then_replace(path, *, root, max_bytes):
        calls.append(max_bytes)
        content = real_read(path, root=root, max_bytes=max_bytes)
        Path(path).write_bytes(replacement)
        return content

    monkeypatch.setattr(tools_module, "read_stable_bytes", snapshot_then_replace)
    validated = tools_module._validated_report_manifest_entry(
        {
            "path": str(report_path),
            "category": "timing",
            "size": len(original),
            "sha256": hashlib.sha256(original).hexdigest(),
        },
        index=0,
        manifest_path=manifest_path,
        report_dir=report_dir,
        project_dir=project_dir,
        run_name="impl_1",
    )

    assert calls == [tools_module.MAX_REPORT_FILE_BYTES]
    assert validated["size"] == len(original)
    assert validated["sha256"] == hashlib.sha256(original).hexdigest()
    assert report_path.read_bytes() == replacement
