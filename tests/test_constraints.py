import tkinter
from pathlib import Path

import pytest

from vivado_agent_mcp.vivado.constraints import (
    CHECK_TIMING_KEYS,
    add_managed_xdc_command,
    check_timing_constraints_command,
    drc_report_command,
    qor_summary_command,
    constraints_summary_command,
    managed_xdc_payload,
    methodology_report_command,
    parse_check_timing_report,
    parse_clock_summary,
    parse_constraints_summary,
    parse_methodology_report,
    parse_qor_summary,
    parse_timing_closure_analysis,
    parse_timing_paths,
    render_xdc_constraints,
)
from vivado_agent_mcp.vivado.readiness import evaluate_bitstream_readiness
from vivado_agent_mcp.vivado.wire import encode_wire_list


def _attested_report(report_type: str, begin_marker: str, body: str) -> str:
    end_marker = begin_marker.replace("_BEGIN__", "_END__")
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
        f"vmcp_report_end={end_marker}"
    )


def test_parse_constraints_summary_extracts_xdc_ports_clocks_and_constraint_counts() -> None:
    raw = f"""
xdc_files={encode_wire_list(['D:/Vivado_Mcp/test_use/project/top.xdc', 'D:/Vivado_Mcp/test_use/project/vmcp_constraints/managed.xdc'])}
xdc_file_discovery_status=READY
fileset_discovery_status=READY
design_discovery_status=READY
ports_discovery_status=READY
clocks_discovery_status=READY
generated_clocks_discovery_status=READY
clock_report_discovery_status=READY
discovery_errors={encode_wire_list([])}
ports={encode_wire_list(['clk', 'rst_n', 'led[0]'])}
clocks={encode_wire_list(['sys_clk'])}
generated_clocks={encode_wire_list(['clk_div2'])}
clock_report_begin=__VMCP_CLOCK_REPORT_BEGIN__
xdc_begin=__VMCP_XDC_BEGIN__
create_clock -period 10.000 -name sys_clk [get_ports clk]
set_input_delay -clock sys_clk 2.0 [get_ports rst_n]
set_output_delay -clock sys_clk 3.0 [get_ports {{led[0]}}]
set_false_path -from [get_ports rst_n]
set_multicycle_path 2 -setup -from [get_cells a] -to [get_cells b]
set_clock_groups -asynchronous -group [get_clocks sys_clk] -group [get_clocks aux_clk]
""".strip()

    result = parse_constraints_summary(raw)

    assert result["xdc_files"] == [
        "D:/Vivado_Mcp/test_use/project/top.xdc",
        "D:/Vivado_Mcp/test_use/project/vmcp_constraints/managed.xdc",
    ]
    assert result["status"] == "READY"
    assert result["xdc_file_discovery_status"] == "READY"
    assert result["ports"] == ["clk", "rst_n", "led[0]"]
    assert result["clocks"] == ["sys_clk"]
    assert result["generated_clocks"] == ["clk_div2"]
    assert result["counts"]["create_clock"] == 1
    assert result["counts"]["set_input_delay"] == 1
    assert result["counts"]["set_output_delay"] == 1
    assert result["counts"]["set_false_path"] == 1
    assert result["counts"]["set_multicycle_path"] == 1
    assert result["counts"]["set_clock_groups"] == 1


def test_parse_constraints_summary_warns_when_clocks_exist_but_xdc_paths_missing() -> None:
    raw = f"""
xdc_files=
xdc_file_discovery_status=EMPTY
fileset_discovery_status=READY
design_discovery_status=READY
ports_discovery_status=READY
clocks_discovery_status=READY
generated_clocks_discovery_status=READY
clock_report_discovery_status=READY
discovery_errors={encode_wire_list([])}
ports={encode_wire_list(['clk', 'rst_n'])}
clocks={encode_wire_list(['sys_clk'])}
clock_report_begin=__VMCP_CLOCK_REPORT_BEGIN__
xdc_begin=__VMCP_XDC_BEGIN__
create_clock -period 10.000 -name sys_clk [get_ports clk]
""".strip()

    result = parse_constraints_summary(raw)

    assert result["status"] == "WARN"
    assert result["xdc_files"] == []
    assert result["xdc_file_discovery_status"] == "EMPTY"
    assert "no XDC file paths" in result["xdc_file_discovery_reason"]
    assert result["findings"][0]["code"] == "XDC_FILE_DISCOVERY_WARN"


def test_parse_constraints_summary_blocks_required_discovery_error() -> None:
    raw = f"""
xdc_files=
xdc_file_discovery_status=ERROR
fileset_discovery_status=ERROR
design_discovery_status=NOT_APPLICABLE
ports_discovery_status=NOT_APPLICABLE
clocks_discovery_status=NOT_APPLICABLE
generated_clocks_discovery_status=NOT_APPLICABLE
clock_report_discovery_status=NOT_APPLICABLE
discovery_errors={encode_wire_list(['get_filesets failed: invalid command state'])}
clock_report_begin=__VMCP_CLOCK_REPORT_BEGIN__
xdc_begin=__VMCP_XDC_BEGIN__
""".strip()

    result = parse_constraints_summary(raw)

    assert result["ok"] is False
    assert result["status"] == "BLOCK"
    assert result["error_code"] == "CONSTRAINT_DISCOVERY_FAILED"
    assert result["discovery_errors"] == ["get_filesets failed: invalid command state"]


def test_constraints_summary_command_reports_successful_empty_xdc_discovery_as_empty() -> None:
    interpreter = tkinter.Tcl()
    interpreter.eval("proc get_filesets {args} {return [list constrs_1]}")
    interpreter.eval("proc get_files {args} {return [list]}")
    interpreter.eval("proc current_design {} {return {}}")

    raw = interpreter.eval(constraints_summary_command())
    result = parse_constraints_summary(raw)

    assert result["status"] == "WARN"
    assert result["xdc_file_discovery_status"] == "EMPTY"
    assert result["probe_statuses"]["fileset_discovery_status"] == "READY"
    assert result["discovery_errors"] == []


def test_parse_check_timing_report_extracts_common_violation_counts() -> None:
    report = """
1. checking no_clock (2)
 There are 2 register/latch pins with no clock.
4. checking unconstrained_internal_endpoints (3)
 There are 3 pins that are not constrained for maximum delay.
5. checking no_input_delay (1)
6. checking no_output_delay (4)
7. checking multiple_clock (1)
9. checking loops (0)
""".strip()

    result = parse_check_timing_report(report)

    assert result["counts"]["no_clock"] == 2
    assert result["counts"]["unconstrained_internal_endpoints"] == 3
    assert result["counts"]["no_input_delay"] == 1
    assert result["counts"]["no_output_delay"] == 4
    assert result["counts"]["multiple_clock"] == 1
    assert result["counts"]["loops"] == 0
    assert result["status"] == "BLOCK"
    assert "no_clock" in result["blocking_checks"]


def test_attested_check_timing_requires_each_vivado_2021_2_key_exactly_once() -> None:
    body = (Path(__file__).parent / "fixtures" / "vivado_2021_2" / "check_timing_clean.txt").read_text(encoding="utf-8").strip()
    complete = parse_check_timing_report(
        _attested_report("check_timing", "__VMCP_CHECK_TIMING_REPORT_BEGIN__", body)
    )
    missing = parse_check_timing_report(
        _attested_report("check_timing", "__VMCP_CHECK_TIMING_REPORT_BEGIN__", "checking no_clock (0)")
    )
    duplicate = parse_check_timing_report(
        _attested_report(
            "check_timing",
            "__VMCP_CHECK_TIMING_REPORT_BEGIN__",
            body + "\nchecking no_clock (0)",
        )
    )
    unknown = parse_check_timing_report(
        _attested_report(
            "check_timing",
            "__VMCP_CHECK_TIMING_REPORT_BEGIN__",
            body + "\nchecking future_check (0)",
        )
    )

    assert complete["parsed"] is True
    assert complete["status"] == "READY"
    assert complete["schema_version"] == "vivado_2021_2_v1"
    assert missing["status"] == "BLOCK" and missing["missing_keys"]
    assert duplicate["status"] == "BLOCK" and duplicate["duplicate_keys"] == ["no_clock"]
    assert unknown["status"] == "BLOCK" and unknown["unknown_keys"] == ["future_check"]


def test_check_timing_rejects_report_attested_by_unsupported_vivado_version() -> None:
    body = (Path(__file__).parent / "fixtures" / "vivado_2021_2" / "check_timing_clean.txt").read_text(encoding="utf-8").strip()
    report = _attested_report("check_timing", "__VMCP_CHECK_TIMING_REPORT_BEGIN__", body).replace(
        "vmcp_vivado_version_short=2021.2",
        "vmcp_vivado_version_short=2024.2",
    )

    result = parse_check_timing_report(report)

    assert result["status"] == "BLOCK"
    assert result["transport_attested"] is False
    assert result["error_code"] == "REPORT_VERSION_MISMATCH"


def test_attested_check_timing_accepts_matching_vivado_toc_and_detail_sections() -> None:
    rows = [f"{index}. checking {key} (0)" for index, key in enumerate(CHECK_TIMING_KEYS, start=1)]
    body = "\n".join(
        ["check_timing report", "Table of Contents", "-----------------", *rows, "", *rows]
    )
    complete = parse_check_timing_report(
        _attested_report("check_timing", "__VMCP_CHECK_TIMING_REPORT_BEGIN__", body)
    )
    mismatch_rows = list(rows)
    mismatch_rows[0] = "1. checking no_clock (1)"
    mismatch = parse_check_timing_report(
        _attested_report(
            "check_timing",
            "__VMCP_CHECK_TIMING_REPORT_BEGIN__",
            "\n".join(["check_timing report", "Table of Contents", "-----------------", *rows, "", *mismatch_rows]),
        )
    )
    toc_only = parse_check_timing_report(
        _attested_report(
            "check_timing",
            "__VMCP_CHECK_TIMING_REPORT_BEGIN__",
            "\n".join(["check_timing report", "Table of Contents", "-----------------", *rows]),
        )
    )

    assert complete["parsed"] is True
    assert complete["duplicate_keys"] == []
    assert complete["detail_missing_keys"] == []
    assert mismatch["parsed"] is False
    assert mismatch["detail_count_mismatches"] == ["no_clock"]
    assert toc_only["parsed"] is False
    assert toc_only["status"] == "BLOCK"
    assert toc_only["toc_present"] is True
    assert toc_only["detail_section_present"] is False


def test_parse_methodology_report_counts_messages_and_sets_status() -> None:
    report = """
ERROR: [Methodology 1-1] Missing primary clock constraint.
CRITICAL WARNING: [Methodology 2-2] CDC structure needs review.
WARNING: [Methodology 3-3] High fanout net.
""".strip()

    result = parse_methodology_report(report)

    assert result["counts"]["ERROR"] == 1
    assert result["counts"]["CRITICAL WARNING"] == 1
    assert result["counts"]["WARNING"] == 1
    assert result["parsed"] is False
    assert result["status"] == "BLOCK"


def test_attested_methodology_info_or_warning_does_not_prove_report_complete() -> None:
    info = parse_methodology_report(_attested_report("methodology", "__VMCP_METHODOLOGY_REPORT_BEGIN__", "INFO: [Common 17-206] Exiting Vivado"))
    warning = parse_methodology_report(_attested_report("methodology", "__VMCP_METHODOLOGY_REPORT_BEGIN__", "WARNING: [Methodology 1-1] Partial output"))

    assert info["transport_attested"] is True
    assert info["complete"] is False
    assert info["status"] == "BLOCK"
    assert warning["counts"]["WARNING"] == 1
    assert warning["complete"] is False
    assert warning["status"] == "BLOCK"


def test_parse_methodology_accepts_vivado_2021_rule_table_and_preserves_warning_severity() -> None:
    body = """
Report Methodology
1. REPORT SUMMARY
Violations found: 5
+-----------+----------+-------------------------------+------------+
| Rule      | Severity | Description                   | Violations |
+-----------+----------+-------------------------------+------------+
| TIMING-18 | Warning  | Missing input or output delay | 5          |
+-----------+----------+-------------------------------+------------+
2. REPORT DETAILS
""".strip()
    result = parse_methodology_report(_attested_report("methodology", "__VMCP_METHODOLOGY_REPORT_BEGIN__", body))

    assert result["parsed"] is True
    assert result["complete"] is True
    assert result["violation_summary_count"] == 5
    assert result["counts"]["WARNING"] == 5
    assert result["status"] == "WARN"


def test_parse_methodology_ignores_vivado_metadata_pipes_outside_rule_table() -> None:
    body = """
| Tool Version : Vivado v.2021.2 (win64)
| Design       : counter_top
Report Methodology
1. REPORT SUMMARY
Violations found: 1
| Rule      | Severity | Description                   | Violations |
| TIMING-18 | Warning  | Missing input or output delay | 1          |
2. REPORT DETAILS
""".strip()

    result = parse_methodology_report(
        _attested_report("methodology", "__VMCP_METHODOLOGY_REPORT_BEGIN__", body)
    )

    assert result["parsed"] is True
    assert result["malformed_rows"] == []
    assert result["counts"]["WARNING"] == 1
    assert result["status"] == "WARN"


def test_empty_check_timing_and_methodology_reports_are_unparsed() -> None:
    assert parse_check_timing_report("")["parsed"] is False
    assert parse_methodology_report("")["parsed"] is False


def test_report_attestation_does_not_make_unknown_constraint_reports_parsed() -> None:
    check_timing = parse_check_timing_report(_attested_report("check_timing", "__VMCP_CHECK_TIMING_REPORT_BEGIN__", "opaque check timing output"))
    methodology = parse_methodology_report(_attested_report("methodology", "__VMCP_METHODOLOGY_REPORT_BEGIN__", "opaque methodology output"))

    for result in (check_timing, methodology):
        assert result["transport_attested"] is True
        assert result["structure_recognized"] is False
        assert result["complete"] is False
        assert result["parsed"] is False
        assert result["status"] == "BLOCK"


def test_failure_text_with_report_name_is_not_treated_as_successful_report() -> None:
    check_timing = parse_check_timing_report("Could not generate check_timing report")
    methodology = parse_methodology_report("Could not generate methodology report")

    assert check_timing["parsed"] is False
    assert check_timing["status"] == "BLOCK"
    assert methodology["parsed"] is False
    assert methodology["status"] == "BLOCK"


def test_report_titles_alone_are_not_complete_timing_or_methodology_evidence() -> None:
    check_timing = parse_check_timing_report("check_timing report")
    methodology = parse_methodology_report("methodology report")

    for result in (check_timing, methodology):
        assert result["structure_recognized"] is True
        assert result["complete"] is False
        assert result["status"] == "BLOCK"


def test_parse_clock_summary_extracts_clock_rows() -> None:
    report = """
Clock Report
------------
Clock           Period(ns)  Waveform(ns)
sys_clk         10.000      {0.000 5.000}
aux_clk         20.000      {0.000 10.000}
""".strip()

    result = parse_clock_summary(report)

    assert result["clocks"][0]["name"] == "sys_clk"
    assert result["clocks"][0]["period_ns"] == 10.0
    assert result["clocks"][1]["name"] == "aux_clk"


def test_parse_clock_summary_skips_vivado_header_and_copyright_lines() -> None:
    report = """
Vivado v2024.2
Copyright 1986-2024 Advanced Micro Devices, Inc. All Rights Reserved.
Command: report_clocks
Date: Sun May 31 06:00:00 2026

Clock           Period(ns)  Waveform(ns)
sys_clk         10.000      {0.000 5.000}
""".strip()

    result = parse_clock_summary(report)

    assert [clock["name"] for clock in result["clocks"]] == ["sys_clk"]


def test_parse_timing_paths_extracts_summary_and_paths() -> None:
    body = """
Design Timing Summary
WNS(ns) TNS(ns) WHS(ns) THS(ns)
-0.125 -0.500 0.025 0.000
Slack (VIOLATED) : -0.125ns (required time - arrival time)
Path Group: sys_clk
From Clock: sys_clk
To Clock: sys_clk
Endpoint: u_reg/Q
""".strip()
    report = _attested_report("timing_summary", "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__", body)

    result = parse_timing_paths(report)

    assert result["summary"]["wns_ns"] == -0.125
    assert result["summary"]["timing_met"] is False
    assert result["paths"][0]["slack_ns"] == -0.125
    assert result["paths"][0]["path_group"] == "sys_clk"
    assert result["paths"][0]["from_clock"] == "sys_clk"
    assert result["paths"][0]["to_clock"] == "sys_clk"


def test_parse_qor_summary_extracts_scores_and_messages() -> None:
    report = """
Design Score: 7
WNS(ns): -0.125
TNS(ns): -0.500
CRITICAL WARNING: [QoR 18-1] Congestion risk.
""".strip()

    result = parse_qor_summary(report)

    assert result["design_score"] == 7
    assert result["wns_ns"] == -0.125
    assert result["tns_ns"] == -0.5
    assert result["messages"]["counts"]["CRITICAL WARNING"] == 1


def test_qor_summary_command_gracefully_handles_older_vivado_without_command() -> None:
    result = parse_qor_summary(
        "qor_unavailable=1\nmessage=report_qor_summary is unavailable in this Vivado version"
    )

    assert "info commands report_qor_summary" in qor_summary_command()
    assert result["available"] is False
    assert result["message"] == "report_qor_summary is unavailable in this Vivado version"


def test_managed_xdc_payload_and_add_command_preserve_user_xdc() -> None:
    payload = managed_xdc_payload(
        name="时序 managed",
        constraints=[
            {"type": "create_clock", "name": "sys clk", "period": 10.0, "port": "clk$in"},
            {"type": "set_input_delay", "clock": "sys clk", "delay": 2.5, "ports": ["rst_n"]},
            {"type": "set_output_delay", "clock": "sys clk", "delay": 3.0, "ports": ["led[0]"]},
            {"type": "set_false_path", "from": ["rst_n"]},
            {"type": "set_multicycle_path", "cycles": 2, "setup": True, "from": ["a_reg"], "to": ["b_reg"]},
            {"type": "set_property", "property": "PACKAGE_PIN", "value": "U16", "ports": ["led[0]"]},
            {"type": "set_property", "property": "IOSTANDARD", "value": "LVCMOS33", "ports": ["led[0]"]},
            {"type": "set_property", "property": "CFGBVS", "value": "VCCO", "target": "current_design"},
            {"type": "set_property", "property": "CONFIG_VOLTAGE", "value": "3.3", "target": "current_design"},
        ],
    )
    command = add_managed_xdc_command(
        xdc_path=Path("D:/project/vmcp_constraints") / payload["filename"],
        fileset="constrs_1",
        constraint_count=payload["constraint_count"],
    )

    assert payload["filename"] == "时序 managed.xdc"
    assert payload["content"].startswith("# Generated by Vivado Agent MCP")
    assert "add_files -fileset {constrs_1} $xdc_path" in command
    assert "open $xdc_path" not in command
    assert "file delete" not in command
    assert "create_clock -period {10.0} -name {sys clk} [get_ports {clk$in}]" in payload["content"]
    assert "set_property {PACKAGE_PIN} {U16} [get_ports {led[0]}]" in payload["content"]
    assert "set_property {CFGBVS} {VCCO} [current_design]" in payload["content"]


def test_add_managed_xdc_fileset_metadata_is_tcl_quoted() -> None:
    fileset = 'evil[exec calc]"; file delete -force -- C:/'
    command = add_managed_xdc_command(
        xdc_path="D:/project/vmcp_constraints/managed.xdc",
        fileset=fileset,
        constraint_count=1,
    )

    assert f"add_files -fileset {{{fileset}}}" in command
    assert f"{{fileset={fileset}}}" in command
    assert f'"fileset={fileset}' not in command


def test_managed_xdc_payload_preserves_adversarial_content_as_exact_utf8_bytes() -> None:
    constraints = [
        {
            "type": "set_property",
            "property": "USER_NOTE",
            "value": (
                "}; set ::vmcp_pwned 1; # $value [set value] "
                + "\\\n"
                + "Unicode-约束-末尾\\"
            ),
            "target": "current_design",
        }
    ]
    payload = managed_xdc_payload(
        name="adversarial",
        constraints=constraints,
    )

    assert payload["content_bytes"] == render_xdc_constraints(constraints).encode("utf-8")
    assert b"set ::vmcp_pwned" in payload["content_bytes"]


@pytest.mark.parametrize("name", ["../outside", r"C:\temp\outside.xdc", "subdir/managed.xdc", ".", ""])
def test_managed_xdc_payload_rejects_path_like_names(name: str) -> None:
    with pytest.raises(ValueError, match="file name"):
        managed_xdc_payload(
            name=name,
            constraints=[{"type": "create_clock", "name": "sys_clk", "period": 10.0, "port": "clk"}],
        )


def test_command_builders_use_expected_vivado_reports() -> None:
    assert "check_timing -return_string" in check_timing_constraints_command()
    assert "vmcp_report_begin=__VMCP_CHECK_TIMING_REPORT_BEGIN__" in check_timing_constraints_command()
    assert "vmcp_report_begin=__VMCP_DRC_REPORT_BEGIN__" in drc_report_command()
    assert "vmcp_report_begin=__VMCP_METHODOLOGY_REPORT_BEGIN__" in methodology_report_command()
    assert "report_clocks -return_string" in constraints_summary_command()
    assert "get_filesets {constrs_1}" in constraints_summary_command()
    assert "get_filesets -quiet" not in constraints_summary_command()
    assert "FILE_TYPE == XDC" in constraints_summary_command()


def test_timing_closure_analysis_prioritizes_blocking_findings() -> None:
    result = parse_timing_closure_analysis(
        timing={"timing_met": False, "wns_ns": -0.1},
        check_timing={"status": "BLOCK", "counts": {"no_clock": 1, "unconstrained_internal_endpoints": 0}},
        methodology={"status": "WARN", "counts": {"ERROR": 0, "CRITICAL WARNING": 0, "WARNING": 2}},
        drc={"error_count": 0, "critical_warning_count": 0, "warning_count": 0},
        critical_warnings={"counts": {"ERROR": 0, "CRITICAL WARNING": 0}},
    )

    assert result["status"] == "BLOCK"
    assert result["findings"][0]["severity"] == "BLOCK"
    assert result["findings"][0]["code"] == "TIMING_NOT_MET"


def test_readiness_blocks_on_strict_constraint_failures_and_warns_on_io_delay_gaps() -> None:
    blocked = evaluate_bitstream_readiness(
        timing={"ok": True, "parsed": True, "timing_met": True},
        drc={"ok": True, "parsed": True, "error_count": 0, "critical_warning_count": 0, "warning_count": 0},
        critical_warnings={"ok": True, "counts": {"ERROR": 0, "CRITICAL WARNING": 0}},
        check_timing={"ok": True, "parsed": True, "counts": {"no_clock": 1, "unconstrained_internal_endpoints": 0}},
        methodology={"ok": True, "parsed": True, "counts": {"ERROR": 0, "CRITICAL WARNING": 0, "WARNING": 0}},
    )
    warned = evaluate_bitstream_readiness(
        timing={"ok": True, "parsed": True, "timing_met": True},
        drc={"ok": True, "parsed": True, "error_count": 0, "critical_warning_count": 0, "warning_count": 0},
        critical_warnings={"ok": True, "counts": {"ERROR": 0, "CRITICAL WARNING": 0}},
        check_timing={"ok": True, "parsed": True, "counts": {"no_input_delay": 1, "no_output_delay": 0}},
        methodology={"ok": True, "parsed": True, "counts": {"ERROR": 0, "CRITICAL WARNING": 0, "WARNING": 1}},
    )

    assert blocked["status"] == "BLOCK"
    assert "check_timing reports no_clock=1" in blocked["reasons"]
    assert warned["status"] == "WARN"
    assert "check_timing reports no_input_delay=1" in warned["warnings"]
