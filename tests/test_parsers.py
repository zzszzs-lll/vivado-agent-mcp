from vivado_agent_mcp.vivado.parsers import (
    parse_drc_report,
    parse_messages,
    parse_timing_summary,
    parse_utilization_report,
)
from vivado_agent_mcp.vivado.readiness import evaluate_bitstream_readiness


def _attested_report(report_type: str, begin_marker: str, body: str) -> str:
    end_marker = begin_marker.replace("_BEGIN__", "_END__")
    report_command = {"drc": "report_drc", "timing_summary": "report_timing_summary"}.get(report_type, report_type)
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


def test_drc_report_rejects_mismatched_vivado_build_and_report_command() -> None:
    report = _attested_report("drc", "__VMCP_DRC_REPORT_BEGIN__", "DRC report: no violations")

    wrong_build = parse_drc_report(report.replace("Vivado v2021.2", "Vivado v2022.1"))
    wrong_command = parse_drc_report(report.replace("vmcp_report_command=report_drc", "vmcp_report_command=exec"))

    assert wrong_build["status"] == "BLOCK"
    assert wrong_build["report_attested"] is False
    assert wrong_command["status"] == "BLOCK"
    assert wrong_command["report_attested"] is False


def test_parse_timing_summary_extracts_slack_values_and_status() -> None:
    body = """
    Design Timing Summary
    ---------------------
    WNS(ns)      TNS(ns)  TNS Failing Endpoints  WHS(ns) THS(ns)
    -0.125       -1.500   3                      0.033   0.000
    """.strip()
    report = _attested_report("timing_summary", "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__", body)

    result = parse_timing_summary(report)

    assert result["wns_ns"] == -0.125
    assert result["tns_ns"] == -1.5
    assert result["whs_ns"] == 0.033
    assert result["ths_ns"] == 0.0
    assert result["timing_met"] is False
    assert result["parsed"] is True


def test_parse_timing_summary_maps_full_vivado_endpoint_columns() -> None:
    body = """
    WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns) THS Failing Endpoints THS Total Endpoints WPWS(ns) TPWS(ns) TPWS Failing Endpoints TPWS Total Endpoints
    0.125 0.000 0 42 -0.050 -0.250 2 42 4.500 0.000 0 18
    """.strip()
    report = _attested_report("timing_summary", "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__", body)

    result = parse_timing_summary(report)

    assert result["wns_ns"] == 0.125
    assert result["tns_ns"] == 0.0
    assert result["whs_ns"] == -0.05
    assert result["ths_ns"] == -0.25
    assert result["timing_met"] is False
    assert result["complete_structure"] is True


def test_parse_timing_summary_scopes_main_summary_when_full_report_has_detail_tables() -> None:
    body = """
    | Design Timing Summary
    | ---------------------
    WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns) THS Failing Endpoints THS Total Endpoints WPWS(ns) TPWS(ns) TPWS Failing Endpoints TPWS Total Endpoints
    ------- ------- --------------------- ------------------- ------- ------- --------------------- ------------------- -------- -------- ---------------------- --------------------
    7.777 0.000 0 8 0.324 0.000 0 8 4.500 0.000 0 9

    | Clock Summary
    | -------------
    Clock WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns) THS Failing Endpoints THS Total Endpoints
    sys_clk 7.777 0.000 0 8 0.324 0.000 0 8

    | Inter Clock Table
    | -----------------
    From Clock To Clock WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns) THS Failing Endpoints THS Total Endpoints
    sys_clk sys_clk 7.777 0.000 0 8 0.324 0.000 0 8
    """.strip()
    report = _attested_report("timing_summary", "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__", body)

    result = parse_timing_summary(report)

    assert result["status"] == "READY"
    assert result["timing_met"] is True
    assert result["wns_ns"] == 7.777
    assert result["whs_ns"] == 0.324
    assert result["summary_section_count"] == 1
    assert result["summary_header_count"] == 1


def test_parse_timing_summary_rejects_truncated_fragment_and_incomplete_row() -> None:
    fragment = parse_timing_summary("WNS(ns): 0.125\nWHS(ns): 0.000")
    incomplete = parse_timing_summary(
        "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.125 0.000 0.050"
    )

    assert fragment["status"] == "BLOCK"
    assert fragment["parsed"] is False
    assert incomplete["status"] == "BLOCK"
    assert incomplete["complete_structure"] is False


def test_parse_timing_summary_rejects_complete_but_unattested_or_duplicate_table() -> None:
    body = "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.125 0.000 0.050 0.000"
    duplicate = _attested_report(
        "timing_summary",
        "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__",
        f"{body}\n{body}",
    )

    unattested = parse_timing_summary(body)
    duplicated = parse_timing_summary(duplicate)

    assert unattested["status"] == "BLOCK"
    assert unattested["attestation_complete"] is False
    assert duplicated["status"] == "BLOCK"
    assert duplicated["summary_header_count"] == 2


def test_parse_timing_summary_rejects_duplicate_design_summary_sections() -> None:
    section = """
    | Design Timing Summary
    WNS(ns) TNS(ns) WHS(ns) THS(ns)
    0.125 0.000 0.050 0.000
    """.strip()
    report = _attested_report(
        "timing_summary",
        "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__",
        f"{section}\n{section}",
    )

    result = parse_timing_summary(report)

    assert result["status"] == "BLOCK"
    assert result["parsed"] is False
    assert result["summary_section_count"] == 2


def test_parse_timing_summary_requires_complete_attested_envelope_when_present() -> None:
    body = "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.125 0.000 0.050 0.000"
    valid = parse_timing_summary(_attested_report("timing_summary", "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__", body))
    truncated = parse_timing_summary(
        _attested_report("timing_summary", "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__", body).rsplit("\n", 1)[0]
    )

    assert valid["status"] == "READY"
    assert valid["attestation_complete"] is True
    assert truncated["status"] == "BLOCK"
    assert truncated["attestation_complete"] is False


def test_parse_utilization_report_extracts_common_resources() -> None:
    report = """
    | Slice LUTs*            |  1200 | 20800 | 5.77 |
    | Slice Registers        |  2400 | 41600 | 5.77 |
    | Block RAM Tile         |    10 |    50 | 20.00 |
    | DSPs                   |     4 |    90 | 4.44 |
    | Bonded IOB             |    30 |   106 | 28.30 |
    """

    result = parse_utilization_report(report)

    assert result["resources"]["lut"]["used"] == 1200
    assert result["resources"]["ff"]["available"] == 41600
    assert result["resources"]["bram"]["utilization_percent"] == 20.0
    assert result["resources"]["dsp"]["used"] == 4
    assert result["resources"]["iob"]["available"] == 106


def test_parse_utilization_report_extracts_vivado_used_fixed_prohibited_available_table() -> None:
    report = """
    | Site Type              | Used | Fixed | Prohibited | Available | Util% |
    | Slice LUTs*            |    0 |     0 |          0 |     20800 |  0.00 |
    | Slice Registers        |    1 |     0 |          0 |     41600 | <0.01 |
    | Block RAM Tile         |    2 |     0 |          0 |        50 |  4.00 |
    | DSPs                   |    3 |     0 |          0 |        90 |  3.33 |
    | Bonded IOB             |    4 |     0 |          0 |       106 |  3.77 |
    """

    result = parse_utilization_report(report)

    assert result["resources"]["lut"] == {
        "used": 0,
        "available": 20800,
        "utilization_percent": 0.0,
    }
    assert result["resources"]["ff"]["available"] == 41600
    assert result["resources"]["ff"]["utilization_percent"] == 0.01
    assert result["resources"]["bram"]["available"] == 50
    assert result["resources"]["dsp"]["available"] == 90
    assert result["resources"]["iob"]["available"] == 106


def test_parse_drc_and_messages_classify_errors_and_warnings() -> None:
    drc = """
    ERROR: [DRC UCIO-1] Unconstrained Logical Port: 2 out of 10 logical ports have no user assigned specific location constraint.
    CRITICAL WARNING: [Timing 38-282] The design failed to meet timing requirements.
    WARNING: [Vivado 12-123] Example warning.
    """

    drc_result = parse_drc_report(drc)
    msg_result = parse_messages(drc)

    assert drc_result["error_count"] == 1
    assert drc_result["critical_warning_count"] == 1
    assert drc_result["violations"][0]["id"] == "DRC UCIO-1"
    assert msg_result["counts"]["ERROR"] == 1
    assert msg_result["counts"]["CRITICAL WARNING"] == 1
    assert msg_result["messages"][2]["severity"] == "WARNING"
    assert drc_result["parsed"] is False
    assert drc_result["status"] == "BLOCK"


def test_attested_drc_info_or_warning_does_not_prove_report_complete() -> None:
    info = parse_drc_report(_attested_report("drc", "__VMCP_DRC_REPORT_BEGIN__", "INFO: [Common 17-206] Exiting Vivado"))
    warning = parse_drc_report(_attested_report("drc", "__VMCP_DRC_REPORT_BEGIN__", "WARNING: [Common 17-206] Partial output"))

    assert info["transport_attested"] is True
    assert info["complete"] is False
    assert info["status"] == "BLOCK"
    assert warning["warning_count"] == 1
    assert warning["complete"] is False
    assert warning["status"] == "BLOCK"


def test_empty_reports_are_unparsed_and_readiness_fails_closed() -> None:
    timing = parse_timing_summary("")
    drc = parse_drc_report("")

    result = evaluate_bitstream_readiness(
        timing=timing,
        drc=drc,
        critical_warnings={"ok": True, "counts": {"ERROR": 0, "CRITICAL WARNING": 0}},
        check_timing={"ok": True, "parsed": False, "counts": {}},
        methodology={"ok": True, "parsed": False, "counts": {}},
    )

    assert timing["parsed"] is False
    assert timing["timing_met"] is None
    assert drc["parsed"] is False
    assert result["status"] == "BLOCK"
    assert any("Timing summary" in reason for reason in result["reasons"])
    assert any("DRC report" in reason for reason in result["reasons"])
    assert any("check_timing" in reason for reason in result["reasons"])
    assert any("Methodology" in reason for reason in result["reasons"])


def test_report_generation_failure_text_does_not_attest_drc_success() -> None:
    drc = parse_drc_report("Could not generate DRC report")

    result = evaluate_bitstream_readiness(
        timing={"ok": True, "parsed": True, "timing_met": True},
        drc=drc,
        critical_warnings={"ok": True, "counts": {"ERROR": 0, "CRITICAL WARNING": 0}},
    )

    assert drc["parsed"] is False
    assert drc["report_attested"] is False
    assert result["status"] == "BLOCK"
    assert "DRC report is unavailable or could not be parsed" in result["reasons"]


def test_drc_attestation_does_not_make_unknown_body_parsed() -> None:
    drc = parse_drc_report(_attested_report("drc", "__VMCP_DRC_REPORT_BEGIN__", "opaque text from an unsupported report format"))

    assert drc["transport_attested"] is True
    assert drc["structure_recognized"] is False
    assert drc["complete"] is False
    assert drc["parsed"] is False
    assert drc["status"] == "BLOCK"


def test_drc_title_alone_is_not_complete_evidence() -> None:
    result = parse_drc_report("DRC report")

    assert result["structure_recognized"] is True
    assert result["complete"] is False
    assert result["status"] == "BLOCK"


def test_parse_drc_accepts_vivado_2021_report_summary_with_zero_violations() -> None:
    body = """
Report DRC
1. REPORT SUMMARY
Violations found: 0
+------+----------+-------------+------------+
| Rule | Severity | Description | Violations |
+------+----------+-------------+------------+
2. REPORT DETAILS
""".strip()
    result = parse_drc_report(_attested_report("drc", "__VMCP_DRC_REPORT_BEGIN__", body))

    assert result["parsed"] is True
    assert result["complete"] is True
    assert result["violation_summary_count"] == 0
    assert result["status"] == "READY"


def test_parse_drc_ignores_vivado_metadata_pipes_outside_rule_table() -> None:
    body = """
| Tool Version : Vivado v.2021.2 (win64)
| Date         : Fri Jul 17 10:53:29 2026
| Design       : counter_top
Report DRC
1. REPORT SUMMARY
Violations found: 0
+------+----------+-------------+------------+
| Rule | Severity | Description | Violations |
+------+----------+-------------+------------+
+------+----------+-------------+------------+
2. REPORT DETAILS
""".strip()

    result = parse_drc_report(_attested_report("drc", "__VMCP_DRC_REPORT_BEGIN__", body))

    assert result["parsed"] is True
    assert result["table_recognized"] is True
    assert result["malformed_rows"] == []
    assert result["status"] == "READY"


def test_parse_drc_still_blocks_unterminated_rows_inside_rule_table() -> None:
    body = """
Report DRC
Violations found: 1
| Rule | Severity | Description | Violations |
| NSTD-1 | Warning | Missing IOSTANDARD | 1
2. REPORT DETAILS
""".strip()

    result = parse_drc_report(_attested_report("drc", "__VMCP_DRC_REPORT_BEGIN__", body))

    assert result["parsed"] is False
    assert result["malformed_rows"][0]["reason"] == "unterminated table row"
    assert result["status"] == "BLOCK"


def test_readiness_blocks_when_timing_or_drc_fails() -> None:
    result = evaluate_bitstream_readiness(
        timing={"ok": True, "parsed": True, "timing_met": False, "wns_ns": -0.1},
        drc={"ok": True, "parsed": True, "error_count": 1, "critical_warning_count": 0},
        critical_warnings={"ok": True, "counts": {"ERROR": 0, "CRITICAL WARNING": 0}},
    )

    assert result["status"] == "BLOCK"
    assert "Timing is not met" in result["reasons"]
    assert "DRC contains 1 error(s)" in result["reasons"]


def test_readiness_surfaces_configuration_voltage_drc_warning() -> None:
    body = """
Report DRC
1. REPORT SUMMARY
Violations found: 1
| Rule | Severity | Description | Violations |
| CFGBVS-1 | Warning | Missing CFGBVS and CONFIG_VOLTAGE properties may cause board configuration voltage issues. | 1 |
2. REPORT DETAILS
""".strip()
    drc = parse_drc_report(_attested_report("drc", "__VMCP_DRC_REPORT_BEGIN__", body))

    result = evaluate_bitstream_readiness(
        timing={"ok": True, "parsed": True, "timing_met": True},
        drc=drc,
        critical_warnings={"ok": True, "counts": {"ERROR": 0, "CRITICAL WARNING": 0}},
    )

    assert result["status"] == "WARN"
    assert any("CFGBVS" in warning and "CONFIG_VOLTAGE" in warning for warning in result["warnings"])


def test_readiness_blocks_when_parser_attestation_is_missing() -> None:
    result = evaluate_bitstream_readiness(
        timing={"ok": True, "timing_met": True, "wns_ns": 0.1},
        drc={"ok": True, "error_count": 0, "critical_warning_count": 0, "warning_count": 0},
        critical_warnings={"ok": True, "counts": {"ERROR": 0, "CRITICAL WARNING": 0}},
        check_timing={"ok": True, "counts": {}},
        methodology={"ok": True, "counts": {}},
    )

    assert result["status"] == "BLOCK"
    assert "Timing summary is unavailable or could not be parsed" in result["reasons"]
    assert "DRC report is unavailable or could not be parsed" in result["reasons"]
    assert "check_timing report is unavailable or could not be parsed" in result["reasons"]
    assert "Methodology report is unavailable or could not be parsed" in result["reasons"]
