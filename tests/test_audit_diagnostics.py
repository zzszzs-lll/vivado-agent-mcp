import json
import hashlib
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bitstream_fixture import write_test_bitstream, write_test_design_execution_identity
import vivado_agent_mcp.tools as tools_module
from vivado_agent_mcp.tools import VivadoToolService
from vivado_agent_mcp.vivado import managed_path
from vivado_agent_mcp.vivado.artifacts import collect_artifacts
from vivado_agent_mcp.vivado.audit import (
    apply_signoff_waivers,
    apply_waivers_to_signoff,
    collect_diagnostic_bundle_files,
    create_waiver,
    evaluate_project_audit,
    finding_fingerprint,
    load_waivers,
    remove_waiver,
    render_project_replay_script,
    waiver_path,
)
from vivado_agent_mcp.vivado.constraints import (
    CHECK_TIMING_KEYS,
    CHECK_TIMING_REPORT_BEGIN_MARKER,
    METHODOLOGY_REPORT_BEGIN_MARKER,
)
from vivado_agent_mcp.vivado.parsers import DRC_REPORT_BEGIN_MARKER, attest_report_text
from vivado_agent_mcp.vivado.prehardware import collect_report_bundle_files
from vivado_agent_mcp.vivado.wire import encode_wire_list, encode_wire_row


def _complete_check_timing_body(**overrides: int) -> str:
    counts = {key: 0 for key in CHECK_TIMING_KEYS} | overrides
    return "check_timing report\n" + "\n".join(
        f"checking {key} ({counts[key]})" for key in CHECK_TIMING_KEYS
    )


def _fresh_artifact_manifest(project_dir: str = "D:/Vivado_Mcp/test_use/demo") -> dict:
    design_identity = {"status": "READY", "schema_version": 1, "sha256": "d" * 64, "files": []}
    return {
        "schema_version": 4,
        "status": "READY",
        "manifest_path": f"{project_dir}/vmcp_artifacts/impl_1/manifest.json",
        "evidence_freshness": {"status": "FRESH", "needs_refresh": False},
        "run_name": "impl_1",
        "manifest_sha256": "a" * 64,
        "run_snapshot": {"session_generation_id": "test-generation"},
        "artifacts": [{"category": "bitstream", "sha256": "b" * 64}],
        "design_execution_identity": design_identity,
        "design_execution_identity_sha256": design_identity["sha256"],
    }


def _fresh_report_manifest(project_dir: str = "D:/Vivado_Mcp/test_use/demo") -> dict:
    design_identity = {"status": "READY", "schema_version": 1, "sha256": "d" * 64, "files": []}
    return {
        "schema_version": 4,
        "status": "READY",
        "manifest_path": f"{project_dir}/vmcp_reports/impl_1/report_manifest.json",
        "run_name": "impl_1",
        "manifest_sha256": "c" * 64,
        "run_snapshot": {"session_generation_id": "test-generation"},
        "evidence_freshness": {"status": "FRESH", "needs_refresh": False},
        "design_execution_identity": design_identity,
        "design_execution_identity_sha256": design_identity["sha256"],
    }


def _attested_report(report_type: str, marker: str, body: str) -> str:
    return attest_report_text(report_type, marker, body)


def _project_state_raw(
    project_dir: Path,
    *,
    bitstream_files: list[str] | None = None,
) -> str:
    return "\n".join(
        [
            "project_name=demo",
            f"project_dir={project_dir}",
            "part=xc7a35tcpg236-1",
            "top=top",
            "sim_top=tb_top",
            f"filesets={encode_wire_list(['sources_1', 'constrs_1', 'sim_1'])}",
            f"runs={encode_wire_list(['synth_1', 'impl_1'])}",
            f"bitstream_files={encode_wire_list(bitstream_files or [])}",
        ]
    )


def _fileset_file_row(path: Path | str, file_type: str) -> str:
    return encode_wire_row(
        {
            "path": str(path),
            "type": file_type,
            "exists": "1",
            "managed": "0",
        }
    )


def _compile_order_raw(path: Path | str) -> str:
    row = encode_wire_row(
        {
            "file": str(path),
            "type": "Verilog",
            "exists": "1",
            "managed": "0",
            "used_in": "synthesis",
            "order": "0",
        }
    )
    return (
        "status=READY\n"
        "compile_order_schema=vivado_2021_2_v1\n"
        "compile_order_complete=1\n"
        "compile_order_count=1\n"
        "fileset=sources_1\n"
        "top=top\n"
        "raw_begin=__VMCP_COMPILE_ORDER_BEGIN__\n"
        f"{row}\n"
        "raw_end=__VMCP_COMPILE_ORDER_END__"
    )


def _syntax_raw() -> str:
    return (
        "status=READY\n"
        "fileset=sources_1\n"
        "raw_begin=__VMCP_SYNTAX_REPORT_BEGIN__\n"
        "raw_end=__VMCP_SYNTAX_REPORT_END__"
    )


def _elaboration_raw() -> str:
    return (
        "status=READY\n"
        "top=top\n"
        "part=xc7a35tcpg236-1\n"
        "raw_begin=__VMCP_ELABORATION_REPORT_BEGIN__\n"
        "raw_end=__VMCP_ELABORATION_REPORT_END__"
    )


def _constraints_raw(xdc_path: Path | str) -> str:
    return "\n".join(
        [
            f"xdc_files={encode_wire_list([str(xdc_path)])}",
            "xdc_file_discovery_status=READY",
            "fileset_discovery_status=READY",
            "design_discovery_status=READY",
            "ports_discovery_status=READY",
            "clocks_discovery_status=READY",
            "generated_clocks_discovery_status=READY",
            "clock_report_discovery_status=READY",
            f"discovery_errors={encode_wire_list([])}",
            f"ports={encode_wire_list(['clk'])}",
            f"clocks={encode_wire_list(['clk'])}",
            f"generated_clocks={encode_wire_list([])}",
            "clock_report_begin=__VMCP_CLOCK_REPORT_BEGIN__",
            "Clock clk",
            "xdc_begin=__VMCP_XDC_BEGIN__",
            "create_clock -period 10 [get_ports clk]",
        ]
    )


def _fresh_report_context(report_dir: Path, *, collection_id: str = "audit_report_test") -> dict[str, object]:
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
    project_dir = next(
        (parent.parent for parent in report_dir.parents if parent.name == "vmcp_reports"),
        report_dir.parent.parent,
    )
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
        "design_execution_identity": write_test_design_execution_identity(project_dir),
    }
    for category, filename in filenames.items():
        exists = (report_dir / filename).is_file()
        context[f"{category}_report_command_status"] = (
            "generated" if exists else "unavailable" if category in {"qor", "cdc", "clock_interaction", "power"} else "failed"
        )
        context[f"{category}_report_command_message"] = "test report collection"
    return context


class FakeSession:
    def __init__(self, responses: list[dict] | None = None) -> None:
        self.commands: list[str] = []
        self.responses = list(responses or [])

    def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
        self.commands.append(command)
        if self.responses:
            return self.responses.pop(0)
        return {"ok": True, "raw": ""}


def test_waiver_store_matches_findings_and_preserves_original_findings(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    findings = [
        {
            "id": "f1",
            "severity": "BLOCK",
            "code": "READINESS_BLOCK",
            "message": "check_timing reports no_clock=1",
            "source_tool": "check_bitstream_readiness",
        },
        {
            "id": "f2",
            "severity": "WARN",
            "code": "POWER_UNAVAILABLE",
            "message": "power report unavailable",
            "source_tool": "get_power_report",
        },
    ]

    evidence_identity = "d" * 64
    bound_findings = apply_signoff_waivers(
        findings,
        [],
        evidence_identity_sha256=evidence_identity,
    )["findings"]
    created = create_waiver(
        project_dir=project_dir,
        waiver_id="known_no_clock",
        finding_fingerprint=bound_findings[0]["finding_fingerprint"],
        evidence_identity_sha256=evidence_identity,
        code="READINESS_BLOCK",
        message_contains="no_clock",
        source_tool="check_bitstream_readiness",
        reason="temporary board-independent test fixture",
    )
    applied = apply_signoff_waivers(
        findings,
        load_waivers(project_dir),
        evidence_identity_sha256=evidence_identity,
    )

    assert created["id"] == "known_no_clock"
    assert len(applied["findings"]) == 2
    assert [item["id"] for item in applied["active_findings"]] == ["f2"]
    assert applied["waived_findings"][0]["finding"]["id"] == "f1"
    assert applied["waived_findings"][0]["waiver"]["id"] == "known_no_clock"
    assert len(applied["waived_findings"][0]["waiver"]["matched_finding_sha256"]) == 64
    with pytest.raises(ValueError, match="already exists"):
        create_waiver(
            project_dir=project_dir,
            waiver_id="known_no_clock",
            finding_fingerprint=bound_findings[0]["finding_fingerprint"],
            evidence_identity_sha256=evidence_identity,
            code="READINESS_BLOCK",
            message_contains="no_clock",
            source_tool="check_bitstream_readiness",
            reason="duplicate",
        )
    assert remove_waiver(project_dir=project_dir, waiver_id="known_no_clock") is True
    assert load_waivers(project_dir) == []


def test_waiver_expiry_is_strictly_validated(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    with pytest.raises(ValueError, match="ISO date"):
        create_waiver(
            project_dir=project_dir,
            waiver_id="invalid_expiry",
            finding_fingerprint="0" * 64,
            evidence_identity_sha256="d" * 64,
            code="READINESS_BLOCK",
            reason="reviewed",
            expires_on="2026-99-99",
        )

    waiver_file = waiver_path(project_dir)
    waiver_file.parent.mkdir(parents=True)
    waiver_file.write_text(
        json.dumps({"waivers": [{"id": "invalid_loaded", "expires_on": "not-a-date"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="ISO date"):
        load_waivers(project_dir)

    for noncanonical in ("20260715", "2026-W29-3"):
        with pytest.raises(ValueError, match="ISO date"):
            create_waiver(
                project_dir=project_dir,
                waiver_id=f"invalid-{noncanonical}",
                finding_fingerprint="0" * 64,
                evidence_identity_sha256="d" * 64,
                reason="reviewed",
                expires_on=noncanonical,
            )


def test_waiver_fingerprint_prevents_broad_rule_from_matching_new_finding(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    original = {
        "severity": "WARN",
        "code": "EVIDENCE_STALE",
        "message": "report manifest is stale for impl_1",
        "source_tool": "run_project_audit",
    }
    changed = {
        **original,
        "message": "artifact manifest is stale for a later impl_1 run",
    }
    evidence_identity = "d" * 64
    bound_findings = apply_signoff_waivers(
        [original, changed],
        [],
        evidence_identity_sha256=evidence_identity,
    )["findings"]
    waiver = create_waiver(
        project_dir=project_dir,
        waiver_id="one-reviewed-finding",
        finding_fingerprint=bound_findings[0]["finding_fingerprint"],
        evidence_identity_sha256=evidence_identity,
        code="EVIDENCE_STALE",
        message_contains="stale",
        source_tool="run_project_audit",
        reason="reviewed original evidence only",
    )

    applied = apply_signoff_waivers(
        [original, changed],
        [waiver],
        evidence_identity_sha256=evidence_identity,
    )

    assert len(applied["waived_findings"]) == 1
    assert applied["waived_findings"][0]["finding"]["message"] == original["message"]
    assert [item["message"] for item in applied["active_findings"]] == [changed["message"]]

    changed_evidence = apply_signoff_waivers(
        [original],
        [waiver],
        evidence_identity_sha256="e" * 64,
    )
    assert changed_evidence["waived_findings"] == []
    assert changed_evidence["active_findings"][0]["message"] == original["message"]


def test_waiver_write_rejects_symlinked_signoff_directory(tmp_path: Path, monkeypatch) -> None:
    project_dir = tmp_path / "project"
    outside_dir = tmp_path / "outside"
    project_dir.mkdir()
    outside_dir.mkdir()
    signoff_dir = project_dir / "vmcp_signoff"
    try:
        signoff_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        signoff_dir.mkdir()
        original_is_reparse_point = managed_path.is_reparse_point
        monkeypatch.setattr(
            managed_path,
            "is_reparse_point",
            lambda path, info=None: Path(path) == signoff_dir or original_is_reparse_point(path, info),
        )

    with pytest.raises(ValueError, match="symlink|junction|reparse"):
        create_waiver(
            project_dir=project_dir,
            waiver_id="outside-write",
            finding_fingerprint="a" * 64,
            evidence_identity_sha256="b" * 64,
            reason="must remain project-local",
        )

    assert not (outside_dir / "waivers.json").exists()


def test_waiver_fingerprint_includes_stable_finding_details() -> None:
    first = {
        "id": "drc-1",
        "severity": "BLOCK",
        "code": "DRC",
        "message": "DRC contains 1 error(s)",
        "source_tool": "run_pre_hw_signoff",
        "detail": {"rule": "NSTD-1", "affected_object": "led"},
    }
    second = {
        **first,
        "id": "drc-2",
        "finding_fingerprint": "ignored volatile value",
        "detail": {"rule": "UCIO-1", "affected_object": "button"},
    }

    assert finding_fingerprint(first) != finding_fingerprint(second)
    assert finding_fingerprint(first) == finding_fingerprint({**first, "id": "renumbered"})


def test_project_audit_status_uses_active_findings_after_waivers() -> None:
    signoff = {
        "status": "BLOCK",
        "findings": [
            {
                "id": "signoff-0",
                "severity": "BLOCK",
                "code": "READINESS_BLOCK",
                "message": "check_timing reports no_clock=1",
                "source_tool": "check_bitstream_readiness",
            }
        ],
    }
    audit_inputs = {
        "environment": {"vivado": {"available": True, "version": "2021.2"}},
        "project_state": {"project": {"name": "demo", "directory": "D:/Vivado_Mcp/test_use/demo"}},
        "sources": {"status": "READY", "findings": []},
        "constraints": {"status": "READY", "findings": []},
        "ip_status": {"status": "READY", "findings": []},
        "bd_validation": {"status": "READY", "findings": []},
        "run_configurations": {"status": "READY", "findings": []},
        "artifact_manifest": _fresh_artifact_manifest(),
        "report_manifest": _fresh_report_manifest(),
        "signoff": signoff,
    }
    unwaived = evaluate_project_audit(**audit_inputs, waivers=[])
    bound_finding = next(item for item in unwaived["active_findings"] if item["code"] == "READINESS_BLOCK")
    waiver = {
        "id": "known_no_clock",
        "finding_fingerprint": bound_finding["finding_fingerprint"],
        "evidence_identity_sha256": unwaived["waiver_evidence_identity_sha256"],
        "code": "READINESS_BLOCK",
        "message_contains": "no_clock",
        "source_tool": "check_bitstream_readiness",
        "enabled": True,
    }

    audit = evaluate_project_audit(**audit_inputs, waivers=[waiver])

    assert audit["status"] == "READY"
    assert audit["waiver_count"] == 1
    assert audit["active_findings"] == []
    assert audit["waived_findings"][0]["finding"]["id"] == "signoff-0"
    assert audit["raw_findings"][0]["message"] == "check_timing reports no_clock=1"
    assert audit["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert audit["hardware_validation"]["real_board_required"] is True
    assert "no real FPGA board" in audit["hardware_validation"]["message"]


def test_project_audit_inherits_configuration_voltage_warning_from_signoff() -> None:
    audit = evaluate_project_audit(
        environment={"vivado": {"available": True, "version": "2021.2"}},
        project_state={"project": {"name": "demo", "directory": "D:/Vivado_Mcp/test_use/demo"}},
        sources={"status": "READY", "findings": []},
        constraints={"status": "READY", "findings": []},
        ip_status={"status": "READY", "findings": []},
        bd_validation={"status": "READY", "findings": []},
        run_configurations={"status": "READY", "findings": []},
        artifact_manifest=_fresh_artifact_manifest(),
        report_manifest=_fresh_report_manifest(),
        signoff={
            "status": "WARN",
            "warnings": ["CFGBVS/CONFIG_VOLTAGE are not fully set on current_design; document board voltage basis before handoff."],
        },
        waivers=[],
    )

    messages = [finding["message"] for finding in audit["active_findings"]]
    assert audit["status"] == "WARN"
    assert any("CFGBVS/CONFIG_VOLTAGE" in message for message in messages)
    assert any(action["tool"] == "create_managed_xdc" for action in audit["next_actions"])


def test_project_audit_blocks_mismatched_artifact_and_report_design_closures() -> None:
    artifact_manifest = _fresh_artifact_manifest()
    report_manifest = _fresh_report_manifest()
    report_identity = {
        "status": "READY",
        "schema_version": 1,
        "sha256": "e" * 64,
        "files": [],
    }
    report_manifest["design_execution_identity"] = report_identity
    report_manifest["design_execution_identity_sha256"] = report_identity["sha256"]

    audit = evaluate_project_audit(
        environment={"vivado": {"available": True, "version": "2021.2"}},
        project_state={"project": {"name": "demo", "directory": "D:/Vivado_Mcp/test_use/demo"}},
        sources={"status": "READY", "findings": []},
        constraints={"status": "READY", "findings": []},
        ip_status={"status": "READY", "findings": []},
        bd_validation={"status": "READY", "findings": []},
        run_configurations={"status": "READY", "findings": []},
        artifact_manifest=artifact_manifest,
        report_manifest=report_manifest,
        signoff={"status": "READY", "warnings": [], "findings": []},
        waivers=[],
    )

    assert audit["status"] == "BLOCK"
    assert audit["evidence_freshness"]["status"] == "STALE"
    assert audit["evidence_freshness"]["design_execution_identity_sha256"] == ""
    assert any(
        "different RTL/XDC/include/run configuration closures" in finding["message"]
        for finding in audit["active_findings"]
    )


def test_project_audit_surfaces_missing_report_files_from_manifest() -> None:
    audit = evaluate_project_audit(
        environment={"vivado": {"available": True, "version": "2021.2"}},
        project_state={"project": {"name": "demo", "directory": "D:/Vivado_Mcp/test_use/demo"}},
        sources={"status": "READY", "findings": []},
        constraints={"status": "READY", "findings": []},
        ip_status={"status": "READY", "findings": []},
        bd_validation={"status": "READY", "findings": []},
        run_configurations={"status": "READY", "findings": []},
        artifact_manifest=_fresh_artifact_manifest(),
        report_manifest={
            **_fresh_report_manifest(),
            "manifest_path": "D:/Vivado_Mcp/test_use/demo/vmcp_reports/impl_1/report_manifest.json",
            "missing_reports": [
                {
                    "category": "qor",
                    "filename": "qor_summary.rpt",
                    "status": "missing",
                    "reason": "qor_summary.rpt was not generated; report_qor_summary may be unavailable.",
                }
            ],
        },
        signoff={"status": "READY", "warnings": [], "findings": []},
        waivers=[],
    )

    assert audit["status"] == "WARN"
    finding = next(item for item in audit["active_findings"] if item["code"] == "REPORT_FILE_MISSING")
    assert "qor report missing" in finding["message"]
    assert any(action["tool"] == "collect_report_bundle" for action in audit["next_actions"])


def test_signoff_waivers_recompute_next_actions_from_active_findings() -> None:
    signoff = {
        "status": "BLOCK",
        "reasons": ["check_timing reports no_clock=1"],
        "warnings": [],
        "findings": [
            {
                "id": "signoff-0",
                "severity": "BLOCK",
                "code": "READINESS_BLOCK",
                "message": "check_timing reports no_clock=1",
                "source_tool": "check_bitstream_readiness",
            }
        ],
        "next_steps": ["Run analyze_timing_closure before signoff."],
        "next_actions": [
            {
                "tool": "analyze_timing_closure",
                "reason": "Diagnose timing blocker.",
                "required_args": [],
                "arg_sources": {},
                "preconditions": ["Implementation result is open."],
                "stop_condition": "timing closure status is READY",
                "optional": False,
            }
        ],
    }
    evidence_identity = "d" * 64
    unwaived = apply_waivers_to_signoff(
        signoff,
        [],
        evidence_identity_sha256=evidence_identity,
    )
    waiver = {
        "id": "known_no_clock",
        "finding_fingerprint": unwaived["active_findings"][0]["finding_fingerprint"],
        "evidence_identity_sha256": evidence_identity,
        "code": "READINESS_BLOCK",
        "message_contains": "no_clock",
        "source_tool": "check_bitstream_readiness",
        "enabled": True,
    }

    waived = apply_waivers_to_signoff(
        signoff,
        [waiver],
        evidence_identity_sha256=evidence_identity,
    )

    assert waived["status"] == "READY"
    assert waived["reasons"] == []
    assert waived["warnings"] == []
    assert waived["active_findings"] == []
    assert waived["raw_reasons"] == ["check_timing reports no_clock=1"]
    assert waived["effective_status"] == "READY_WITH_WAIVERS"
    assert waived["waiver_summary"]["requires_handoff_archive"] is True
    assert waived["next_actions"][0]["tool"] == "list_signoff_waivers"
    assert "collect_diagnostic_bundle" in {action["tool"] for action in waived["next_actions"]}
    assert "analyze_timing_closure" not in {action["tool"] for action in waived["next_actions"]}
    assert "real FPGA board" in "\n".join(waived["next_steps"])


def test_project_audit_next_steps_route_active_findings_to_mcp_tools() -> None:
    audit = evaluate_project_audit(
        environment={"vivado": {"available": True, "version": "2021.2"}},
        project_state={"project": {"name": "demo", "directory": "D:/Vivado_Mcp/test_use/demo"}},
        sources={
            "status": "BLOCK",
            "findings": [
                {
                    "id": "source-0",
                    "severity": "BLOCK",
                    "code": "SYNTAX",
                    "message": "syntax error near module declaration",
                    "source_tool": "analyze_sources",
                }
            ],
        },
        constraints={"status": "READY", "findings": []},
        ip_status={
            "status": "WARN",
            "findings": [
                {
                    "id": "ip-0",
                    "severity": "WARN",
                    "code": "IP_LOCKED",
                    "message": "IP core is locked and output products are stale",
                    "source_tool": "get_ip_status",
                }
            ],
        },
        bd_validation={"status": "READY", "findings": []},
        run_configurations={"status": "READY", "findings": []},
        artifact_manifest=None,
        report_manifest=None,
        signoff={
            "status": "BLOCK",
            "findings": [
                {
                    "id": "signoff-0",
                    "severity": "BLOCK",
                    "code": "READINESS_BLOCK",
                    "message": "check_timing reports no_clock=1",
                    "source_tool": "check_bitstream_readiness",
                }
            ],
        },
        waivers=[],
    )

    steps = "\n".join(audit["next_steps"])

    assert audit["status"] == "BLOCK"
    assert "analyze_sources" in steps
    assert "check_syntax" in steps
    assert "analyze_timing_closure" in steps
    assert "collect_build_artifacts" in steps
    assert "collect_report_bundle" in steps
    assert "get_ip_status" in steps
    assert "generate_ip_targets" in steps
    assert "explicit signoff waiver" in steps
    action_tools = {action["tool"] for action in audit["next_actions"]}
    assert {"analyze_sources", "analyze_timing_closure", "collect_build_artifacts", "collect_report_bundle", "get_ip_status", "create_signoff_waiver"} <= action_tools
    assert all(set(action) == {"tool", "reason", "required_args", "arg_sources", "preconditions", "stop_condition", "optional"} for action in audit["next_actions"])


def test_collect_diagnostic_bundle_writes_manifest_and_copies_only_managed_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    report_dir = project_dir / "vmcp_reports" / "impl_1"
    artifact_dir = project_dir / "vmcp_artifacts" / "impl_1"
    source_dir = project_dir / "rtl"
    report_dir.mkdir(parents=True)
    artifact_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    report_manifest = report_dir / "report_manifest.json"
    artifact_manifest = artifact_dir / "manifest.json"
    source_file = source_dir / "top.v"
    report_manifest.write_text(json.dumps({"reports": []}), encoding="utf-8")
    artifact_manifest.write_text(json.dumps({"artifacts": []}), encoding="utf-8")
    source_file.write_text("module top; endmodule", encoding="utf-8")

    bundle = collect_diagnostic_bundle_files(
        project_dir=project_dir,
        audit_result={"status": "READY"},
        environment={"vivado": {"version": "2021.2"}},
        project_state={"project": {"name": "demo"}},
        filesets={"sources_1": [{"path": str(source_file)}]},
        run_configurations={"impl_1": {"run": {"strategy": "Default"}}},
        waivers=[],
        session_status={
            "connected": True,
            "runtime_dir": "D:/Vivado_Mcp/.vivado_agent_mcp/runtime",
            "temp_dir": "D:/Vivado_Mcp/.vivado_agent_mcp/runtime",
        },
        replay_script="create_project {demo} {D:/Vivado_Mcp/test_use/demo} -part {xc7a35tcpg236-1}\n",
        report_manifest_path=report_manifest,
        artifact_manifest_path=artifact_manifest,
        logs={"vivado": "tail"},
        timestamp="20260524_120000",
    )

    manifest_path = Path(bundle["manifest_path"])
    assert manifest_path == project_dir / "vmcp_diagnostics" / "20260524_120000" / "diagnostic_manifest.json"
    assert manifest_path.exists()
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    categories = {item["category"] for item in bundle["files"]}
    assert {"audit", "environment", "project_state", "filesets", "run_configurations", "waivers", "session_status", "replay_script", "logs", "report_manifest", "artifact_manifest"} <= categories
    assert manifest_data["summary"]["file_count"] == len(bundle["files"])
    assert manifest_data["summary"]["audit_status"] == "READY"
    assert manifest_data["summary"]["complete"] is True
    assert manifest_data["summary"]["missing_required_categories"] == []
    assert manifest_data["bundle_mode"] == "reference"
    assert manifest_data["portable"] is False
    assert manifest_data["portability"]["status"] == "PROJECT_LOCAL_REFERENCE_ONLY"
    assert "session_status" in manifest_data["summary"]["required_categories"]
    assert "replay_script" in manifest_data["summary"]["required_categories"]
    assert manifest_data["summary"]["category_counts"]["replay_script"] == 1
    assert manifest_data["summary"]["primary_files"]["session_status"].endswith("session_status.json")
    assert manifest_data["summary"]["primary_files"]["replay_script"].endswith("replay_project.tcl")
    session_status = json.loads((manifest_path.parent / "session_status.json").read_text(encoding="utf-8"))
    assert session_status["connected"] is True
    assert session_status["runtime_dir"] == "D:/Vivado_Mcp/.vivado_agent_mcp/runtime"
    assert "create_project {demo}" in (manifest_path.parent / "replay_project.tcl").read_text(encoding="utf-8")
    assert not (manifest_path.parent / "top.v").exists()
    assert all(item["sha256"] for item in bundle["files"])
    with pytest.raises(ValueError, match="outside project directory"):
        collect_diagnostic_bundle_files(
            project_dir=project_dir,
            audit_result={},
            environment={},
            project_state={},
            filesets={},
            run_configurations={},
            waivers=[],
            output_dir=tmp_path / "outside_bundle",
        )


def test_run_project_audit_uses_explicit_project_local_report_manifest(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    report_dir = project_dir / "vmcp_reports" / "custom" / "impl_1"
    artifact_dir = project_dir / "vmcp_artifacts" / "impl_1"
    source_dir = project_dir / "rtl"
    constr_dir = project_dir / "xdc"
    report_dir.mkdir(parents=True)
    artifact_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    constr_dir.mkdir(parents=True)
    (source_dir / "top.v").write_text("module top; endmodule", encoding="utf-8")
    (constr_dir / "top.xdc").write_text("create_clock -period 10 [get_ports clk]", encoding="utf-8")
    (artifact_dir / "manifest.json").write_text(json.dumps({"artifacts": []}), encoding="utf-8")
    (report_dir / "timing_summary.rpt").write_text("WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.100 0.000", encoding="utf-8")
    (report_dir / "utilization.rpt").write_text("Utilization summary", encoding="utf-8")
    (report_dir / "drc.rpt").write_text("", encoding="utf-8")
    (report_dir / "methodology.rpt").write_text("", encoding="utf-8")
    (report_dir / "cdc.rpt").write_text("Safe: 1\nUnsafe: 0\nUnknown: 0", encoding="utf-8")
    (report_dir / "clock_interaction.rpt").write_text("Safe: 1\nUnsafe: 0\nUnknown: 0", encoding="utf-8")
    (report_dir / "power.rpt").write_text("Total On-Chip Power (W): 0.100", encoding="utf-8")
    (report_dir / "messages.log").write_text("Vivado Agent MCP report bundle generated.", encoding="utf-8")
    manifest = collect_report_bundle_files(
        report_dir=report_dir,
        run_name="impl_1",
        project_dir=project_dir,
        report_context=_fresh_report_context(report_dir),
    )
    manifest_path = Path(manifest["manifest_path"])

    class ExplicitManifestAuditSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            project_text = str(project_dir)
            if "project_name=$project_name" in command and "filesets=" in command:
                return {"ok": True, "raw": _project_state_raw(project_dir)}
            if "check_syntax" in command:
                return {"ok": True, "raw": _syntax_raw()}
            if "compile_order sources" in command:
                return {"ok": True, "raw": _compile_order_raw(source_dir / "top.v")}
            if "xdc_begin=__VMCP_XDC_BEGIN__" in command:
                return {"ok": True, "raw": _constraints_raw(constr_dir / "top.xdc")}
            if "foreach ip [get_ips" in command or "get_files -quiet *.bd" in command:
                return {"ok": True, "raw": encode_wire_list([])}
            if "report_property -return_string $r" in command:
                run_name = "synth_1" if "{synth_1}" in command else "impl_1"
                return {"ok": True, "raw": f"name={run_name}\nstrategy=Default\nstatus=complete\nprogress=100%\nneeds_refresh=0\ndirectory={project_text}/demo.runs/{run_name}\nsession_generation_id=test-generation\nproperties_begin=__VMCP_RUN_PROPERTIES_BEGIN__\n"}
            if "synth_design -rtl" in command:
                return {"ok": True, "raw": _elaboration_raw()}
            if command.startswith("open_run"):
                return {"ok": True, "raw": ""}
            if "get_property CFGBVS" in command or "get_property CONFIG_VOLTAGE" in command:
                return {"ok": True, "raw": "cfgbvs=VCCO\nconfig_voltage=3.3"}
            if "check_timing -return_string" in command:
                return {
                    "ok": True,
                    "raw": _attested_report(
                        "check_timing",
                        CHECK_TIMING_REPORT_BEGIN_MARKER,
                        _complete_check_timing_body(),
                    ),
                }
            if "log_begin=__VMCP_LOG_BEGIN__" in command:
                return {
                    "ok": True,
                    "raw": (
                        "sim_dir=D:/sim\n"
                        f"project_dir_before={project_text}\n"
                        f"project_dir_after={project_text}\n"
                        "project_name_before=demo\nproject_name_after=demo\n"
                        "simset_before=sim_1\nsimset_after=sim_1\n"
                        "sim_top_before=tb_top\nsim_top_after=tb_top\n"
                        f"source_snapshot_before={encode_wire_list([f'{project_text}/sim/tb_top.v|100|1'])}\n"
                        f"source_snapshot_after={encode_wire_list([f'{project_text}/sim/tb_top.v|100|1'])}\n"
                        "status_source=simulation_invocation_log_span\n"
                        "simulation_invocation_id=sim-123\n"
                        "ended_at=2026-06-11T00:00:00Z\n"
                        "log_span_start=0\n"
                        "log_span_end=128\n"
                        "log_span_reset_detected=0\n"
                        "log_begin=__VMCP_LOG_BEGIN__\n"
                        "Simulation finished"
                    ),
                }
            return {"ok": True, "raw": ""}

    fake = ExplicitManifestAuditSession()
    fake.design_execution_identity = manifest["design_execution_identity"]
    service = VivadoToolService(session=fake)

    result = service.call(
        "run_project_audit",
        {"run_name": "impl_1", "project_dir": str(project_dir), "report_manifest_path": str(manifest_path), "timeout_s": 1},
    )

    assert result["ok"] is True, result
    assert result["data"]["evidence_freshness"]["report_manifest_path"] == str(manifest_path.resolve())
    assert result["data"]["inputs"]["report_manifest"]["manifest_path"] == str(manifest_path.resolve())
    assert result["data"]["inputs"]["signoff"]["report_manifest_path"] == str(manifest_path.resolve())


def test_project_bd_audit_validates_every_block_design_and_blocks_on_one_invalid(tmp_path: Path) -> None:
    design_a = tmp_path / "design_a.bd"
    design_b = tmp_path / "design_b.bd"
    design_a.write_text("design a", encoding="utf-8")
    design_b.write_text("design b", encoding="utf-8")

    class MultiBdSession:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "get_files -quiet *.bd" in command:
                return {"ok": True, "raw": encode_wire_list([str(design_a), str(design_b)])}
            if "set bd_name {design_a}" in command:
                return {
                    "ok": True,
                    "raw": f"bd_name=design_a\nbd_file={design_a}\nstatus=VALID\nraw_begin=__VMCP_BD_VALIDATE_BEGIN__\nINFO: design_a valid",
                }
            if "set bd_name {design_b}" in command:
                return {
                    "ok": True,
                    "raw": f"bd_name=design_b\nbd_file={design_b}\nstatus=INVALID\nraw_begin=__VMCP_BD_VALIDATE_BEGIN__\nERROR: design_b invalid",
                }
            raise AssertionError(f"unexpected Tcl: {command}")

    fake = MultiBdSession()
    result = VivadoToolService(session=fake)._collect_bd_audit()

    assert result["status"] == "BLOCK"
    assert [item["bd_name"] for item in result["designs"]] == ["design_a", "design_b"]
    assert result["designs"][0]["status"] == "READY"
    assert result["designs"][1]["status"] == "BLOCK"
    assert result["designs"][0]["file_identity"]["sha256"] == hashlib.sha256(b"design a").hexdigest()
    assert result["findings"][0]["code"] == "BD_INVALID"
    assert all("open_bd_design $bd_file" in command for command in fake.commands[1:])
    assert not any("file mkdir $report_dir" in command for command in fake.commands)


def test_project_ip_audit_requires_versioned_rows() -> None:
    class IpSession:
        def __init__(self, raw: str) -> None:
            self.raw = raw

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            return {"ok": True, "raw": self.raw}

    legacy = VivadoToolService(session=IpSession("ip=axi|locked=0|upgrade_available=0"))._collect_ip_audit()
    versioned_row = encode_wire_row({"ip": "axi|safe", "locked": "0", "upgrade_available": "0"})
    versioned = VivadoToolService(session=IpSession(encode_wire_list([versioned_row])))._collect_ip_audit()

    assert legacy["status"] == "BLOCK"
    assert legacy["wire_trust"] == "INVALID"
    assert legacy["findings"][0]["code"] == "IP_WIRE_PROTOCOL_INVALID"
    assert versioned["status"] == "READY"
    assert versioned["wire_trust"] == "VERSIONED"
    assert versioned["ips"][0]["ip"] == "axi|safe"


def test_diagnostic_manifest_summary_exposes_audit_health_and_next_steps(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    audit_result = {
        "status": "BLOCK",
        "health_summary": {
            "active_block_count": 1,
            "active_warning_count": 2,
            "waived_count": 1,
        },
        "active_findings": [
            {"id": "f0", "severity": "BLOCK", "code": "READINESS_BLOCK", "message": "timing is not met"},
            {"id": "f1", "severity": "WARN", "code": "POWER_UNAVAILABLE", "message": "power report unavailable"},
            {"id": "f2", "severity": "WARN", "code": "REPORT_MANIFEST_MISSING", "message": "report manifest unavailable"},
        ],
        "waived_findings": [
            {"finding": {"id": "wf0", "severity": "BLOCK", "code": "READINESS_BLOCK"}, "waiver": {"id": "known_no_clock"}},
        ],
        "next_steps": [
            "Run analyze_timing_closure before signoff.",
            "Run collect_report_bundle after implementation results are available.",
        ],
        "next_actions": [
            {
                "tool": "analyze_timing_closure",
                "reason": "Investigate timing blocker.",
                "required_args": ["run_name"],
                "arg_sources": {"run_name": "workflow.run_name"},
                "preconditions": ["Implementation result is open."],
                "stop_condition": "timing closure status is READY",
                "optional": False,
            }
        ],
        "hardware_validation": {
            "status": "NOT_VALIDATED",
            "real_board_required": True,
            "message": "Generated without no real FPGA board validation.",
        },
    }

    bundle = collect_diagnostic_bundle_files(
        project_dir=project_dir,
        audit_result=audit_result,
        environment={"vivado": {"version": "2021.2"}},
        project_state={"project": {"name": "demo"}},
        filesets={},
        run_configurations={},
        waivers=[],
        session_status={"connected": True},
        replay_script="create_project {demo} {.} -part {xc7a35tcpg236-1}\n",
        timestamp="20260524_130000",
    )

    summary = json.loads(Path(bundle["manifest_path"]).read_text(encoding="utf-8"))["summary"]

    assert summary["audit_status"] == "BLOCK"
    assert summary["active_block_count"] == 1
    assert summary["active_warning_count"] == 2
    assert summary["waived_count"] == 1
    assert summary["active_finding_count"] == 3
    assert summary["waived_finding_count"] == 1
    assert summary["next_steps"] == audit_result["next_steps"]
    assert summary["next_step_count"] == 2
    assert summary["next_actions"] == audit_result["next_actions"]
    assert summary["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert summary["hardware_validation"]["real_board_required"] is True
    assert "no real FPGA board" in summary["hardware_validation"]["message"]


def test_replay_script_is_project_mode_and_records_filesets_runs_and_reports() -> None:
    script = render_project_replay_script(
        vivado_version="2021.2",
        project={"name": "demo", "directory": "D:/Vivado_Mcp/test_use/demo", "part": "xc7a35tcpg236-1", "top": "top", "sim_top": "tb_top"},
        filesets={
            "sources_1": [{"path": "D:/Vivado_Mcp/test_use/demo/rtl/top.v", "file_type": "Verilog"}],
            "constrs_1": [{"path": "D:/Vivado_Mcp/test_use/demo/xdc/top.xdc", "file_type": "XDC"}],
            "sim_1": [{"path": "D:/Vivado_Mcp/test_use/demo/sim/tb_top.v", "file_type": "Verilog"}],
        },
        run_configurations={
            "synth_1": {"run": {"strategy": "Vivado Synthesis Defaults"}},
            "impl_1": {"run": {"strategy": "Vivado Implementation Defaults"}},
        },
    )

    assert "create_project {demo} {D:/Vivado_Mcp/test_use/demo} -part {xc7a35tcpg236-1}" in script
    assert "add_files -fileset {sources_1} [list {D:/Vivado_Mcp/test_use/demo/rtl/top.v}]" in script
    assert "add_files -fileset {constrs_1} [list {D:/Vivado_Mcp/test_use/demo/xdc/top.xdc}]" in script
    assert "add_files -fileset {sim_1} [list {D:/Vivado_Mcp/test_use/demo/sim/tb_top.v}]" in script
    assert "set_property top {top} [get_filesets {sources_1}]" in script
    assert "set_property strategy {Vivado Implementation Defaults} [get_runs {impl_1}]" in script
    assert "report_timing_summary" in script
    assert "read_verilog" not in script
    assert "synth_design" not in script


def test_waiver_tools_return_structured_content(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    service = VivadoToolService(session=FakeSession())

    evidence_identity = "d" * 64
    finding = {
        "severity": "WARN",
        "code": "POWER_UNAVAILABLE",
        "message": "power report unavailable",
        "source_tool": "get_power_report",
    }
    bound_finding = apply_signoff_waivers(
        [finding],
        [],
        evidence_identity_sha256=evidence_identity,
    )["findings"][0]
    created = service.call(
        "create_signoff_waiver",
        {
            "project_dir": str(project_dir),
            "id": "known_warning",
            "finding_fingerprint": bound_finding["finding_fingerprint"],
            "evidence_identity_sha256": evidence_identity,
            "code": "POWER_UNAVAILABLE",
            "message_contains": "power",
            "source_tool": "get_power_report",
            "reason": "tool unavailable on Vivado 2021.2",
        },
    )
    listed = service.call("list_signoff_waivers", {"project_dir": str(project_dir)})
    removed = service.call("remove_signoff_waiver", {"project_dir": str(project_dir), "id": "known_warning"})

    assert created["ok"] is True
    assert created["data"]["waiver"]["id"] == "known_warning"
    assert listed["ok"] is True
    assert listed["data"]["waiver_count"] == 1
    assert removed["ok"] is True
    assert removed["data"]["removed"] is True


def test_export_project_replay_script_tool_writes_replay_tcl(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    fake = FakeSession(
        responses=[
            {
                "ok": True,
                "raw": _project_state_raw(project_dir),
            },
            {"ok": True, "raw": _fileset_file_row("D:/Vivado_Mcp/test_use/demo/rtl/top.v", "Verilog")},
            {"ok": True, "raw": _fileset_file_row("D:/Vivado_Mcp/test_use/demo/xdc/top.xdc", "XDC")},
            {"ok": True, "raw": _fileset_file_row("D:/Vivado_Mcp/test_use/demo/sim/tb_top.v", "Verilog")},
            {"ok": True, "raw": "name=synth_1\nstrategy=Vivado Synthesis Defaults\nstatus=complete"},
            {"ok": True, "raw": "name=impl_1\nstrategy=Vivado Implementation Defaults\nstatus=complete"},
        ]
    )
    service = VivadoToolService(session=fake)

    result = service.call("export_project_replay_script", {})

    assert result["ok"] is True, result
    script_path = Path(result["data"]["script_path"])
    assert script_path == project_dir / "vmcp_diagnostics" / "replay_project.tcl"
    assert script_path.exists()
    assert "create_project {demo}" in script_path.read_text(encoding="utf-8")


def test_collect_diagnostic_bundle_tool_runs_audit_signoff_and_writes_replay_with_session_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    report_dir = project_dir / "vmcp_reports" / "impl_1"
    artifact_dir = project_dir / "vmcp_artifacts" / "impl_1"
    source_dir = project_dir / "rtl"
    constr_dir = project_dir / "xdc"
    sim_dir = project_dir / "sim"
    report_dir.mkdir(parents=True)
    artifact_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    constr_dir.mkdir(parents=True)
    sim_dir.mkdir(parents=True)
    (source_dir / "top.v").write_text("module top; endmodule", encoding="utf-8")
    (constr_dir / "top.xdc").write_text("create_clock -period 10 [get_ports clk]", encoding="utf-8")
    (sim_dir / "tb_top.v").write_text("module tb_top; endmodule", encoding="utf-8")
    (report_dir / "timing_summary.rpt").write_text("timing", encoding="utf-8")
    (report_dir / "utilization.rpt").write_text("utilization", encoding="utf-8")
    (report_dir / "drc.rpt").write_text("", encoding="utf-8")
    (report_dir / "methodology.rpt").write_text("", encoding="utf-8")
    (report_dir / "qor_summary.rpt").write_text("Design Score: 1\nWNS: 0.100\nTNS: 0.000", encoding="utf-8")
    (report_dir / "cdc.rpt").write_text("Safe: 1\nUnsafe: 0\nUnknown: 0", encoding="utf-8")
    (report_dir / "clock_interaction.rpt").write_text("Safe: 1\nUnsafe: 0\nUnknown: 0", encoding="utf-8")
    (report_dir / "power.rpt").write_text("Total On-Chip Power (W): 0.100", encoding="utf-8")
    (report_dir / "messages.log").write_text("Vivado Agent MCP report bundle generated.", encoding="utf-8")
    (report_dir / "report_manifest.json").write_text(json.dumps({"reports": []}), encoding="utf-8")
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    started_marker = run_dir / ".vivado.begin.rst"
    ended_marker = run_dir / ".vivado.end.rst"
    started_marker.write_text("start", encoding="utf-8")
    source_bitstream_path = run_dir / "top.bit"
    original_bitstream = write_test_bitstream(source_bitstream_path).read_bytes()
    ended_marker.write_text("end", encoding="utf-8")
    base_ns = time.time_ns() - 10_000_000
    os.utime(started_marker, ns=(base_ns, base_ns))
    os.utime(source_bitstream_path, ns=(base_ns + 1_000_000, base_ns + 1_000_000))
    os.utime(ended_marker, ns=(base_ns + 2_000_000, base_ns + 2_000_000))
    collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        design_execution_identity=write_test_design_execution_identity(project_dir),
        run_context={
            "project_part": "xc7a35tcpg236-1",
            "run_top": "top",
            "run_status": "write_bitstream Complete!",
            "run_progress": "100%",
            "run_needs_refresh": "0",
            "expected_bitstream_path": str(source_bitstream_path),
            "run_bitstream_files": [str(source_bitstream_path)],
            "write_bitstream_step_enabled": "1",
            "write_bitstream_step_status": "Complete!",
            "session_generation_id": "test-generation",
        },
        collection_id="artifact_diagnostic_test",
    )

    class DiagnosticFakeSession:
        def __init__(self, project: Path) -> None:
            self.project = project
            self.design_execution_identity = write_test_design_execution_identity(project)
            self.commands: list[str] = []
            self.run_status = "write_bitstream Complete!"
            self.run_needs_refresh = "0"

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            project_text = str(self.project)
            if "project_name=$project_name" in command and "filesets=" in command:
                return {
                    "ok": True,
                    "raw": _project_state_raw(
                        self.project,
                        bitstream_files=[f"{project_text}/demo.runs/impl_1/top.bit"],
                    ),
                }
            if "check_syntax" in command:
                return {"ok": True, "raw": _syntax_raw()}
            if "compile_order sources" in command:
                return {"ok": True, "raw": _compile_order_raw(source_dir / "top.v")}
            if "xdc_begin=__VMCP_XDC_BEGIN__" in command:
                return {"ok": True, "raw": _constraints_raw(constr_dir / "top.xdc")}
            if "foreach ip [get_ips" in command:
                return {"ok": True, "raw": encode_wire_list([])}
            if "get_files -quiet *.bd" in command:
                return {"ok": True, "raw": encode_wire_list([])}
            if "report_property -return_string $r" in command:
                run_name = "synth_1" if "{synth_1}" in command else "impl_1"
                return {"ok": True, "raw": f"name={run_name}\nstrategy=Default\nstatus={self.run_status}\nprogress=100%\nneeds_refresh={self.run_needs_refresh}\ndirectory={project_text}/demo.runs/{run_name}\nsession_generation_id=test-generation\nproperties_begin=__VMCP_RUN_PROPERTIES_BEGIN__\n"}
            if "run_needs_refresh=[get_property NEEDS_REFRESH $r]" in command:
                return {
                    "ok": True,
                    "generation_id": "test-generation",
                    "raw": (
                        "project_name=demo\n"
                        f"project_dir={project_text}\n"
                        "project_part=xc7a35tcpg236-1\n"
                        f"run_dir={project_text}/demo.runs/impl_1\n"
                        "run_srcset=sources_1\n"
                        "run_top=top\n"
                        f"run_status={self.run_status}\n"
                        "run_progress=100%\n"
                        f"run_needs_refresh={self.run_needs_refresh}\n"
                        f"expected_bitstream_path={source_bitstream_path}\n"
                        f"run_bitstream_files={encode_wire_list([str(source_bitstream_path)])}\n"
                        "write_bitstream_step_enabled=1\n"
                        "write_bitstream_step_status=Complete!"
                    ),
                }
            if "synth_design -rtl" in command:
                return {"ok": True, "raw": _elaboration_raw()}
            if command.startswith("open_run"):
                return {"ok": True, "raw": ""}
            if "get_property CFGBVS" in command or "get_property CONFIG_VOLTAGE" in command:
                return {"ok": True, "raw": "cfgbvs=VCCO\nconfig_voltage=3.3"}
            if "report_timing_summary -return_string" in command:
                return {
                    "ok": True,
                    "raw": _attested_report(
                        "timing_summary",
                        "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__",
                        "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.100 0.000",
                    ),
                }
            if "report_drc -return_string" in command:
                return {
                    "ok": True,
                    "raw": _attested_report("drc", DRC_REPORT_BEGIN_MARKER, "DRC report: no violations"),
                }
            if "check_timing -return_string" in command:
                return {
                    "ok": True,
                    "raw": _attested_report(
                        "check_timing",
                        CHECK_TIMING_REPORT_BEGIN_MARKER,
                        _complete_check_timing_body(),
                    ),
                }
            if "report_methodology -return_string" in command:
                return {
                    "ok": True,
                    "raw": _attested_report(
                        "methodology",
                        METHODOLOGY_REPORT_BEGIN_MARKER,
                        "Methodology report: no violations",
                    ),
                }
            if "report_cdc" in command and "-return_string" in command:
                return {"ok": True, "raw": "Safe: 1\nUnsafe: 0\nUnknown: 0"}
            if "report_clock_interaction" in command and "-return_string" in command:
                return {"ok": True, "raw": "Safe: 1\nUnsafe: 0\nUnknown: 0"}
            if "report_power" in command and "-return_string" in command:
                return {"ok": True, "raw": "Total On-Chip Power (W): 0.100"}
            if "vmcp_reports" in command:
                match = re.search(r"set collection_id \{([^}]+)\}", command)
                collection_id = match.group(1) if match else "diagnostic_report"
                invocation_dir = report_dir / "invocations" / collection_id
                invocation_dir.mkdir(parents=True, exist_ok=True)
                report_contents = {
                    "timing_summary.rpt": "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.100 0.000",
                    "utilization.rpt": "Utilization summary",
                    "drc.rpt": "DRC report: no violations",
                    "methodology.rpt": "Methodology report: no violations",
                    "qor_summary.rpt": "Design Score: 1\nWNS: 0.100\nTNS: 0.000",
                    "cdc.rpt": "Safe: 1\nUnsafe: 0\nUnknown: 0",
                    "clock_interaction.rpt": "Safe: 1\nUnsafe: 0\nUnknown: 0",
                    "power.rpt": "Total On-Chip Power (W): 0.100",
                    "messages.log": "INFO: current implementation run completed",
                }
                for name, content in report_contents.items():
                    (invocation_dir / name).write_text(content, encoding="utf-8")
                context = _fresh_report_context(invocation_dir, collection_id=collection_id)
                return {"ok": True, "raw": "\n".join(f"{key}={value}" for key, value in context.items()) + f"\nreport_dir={invocation_dir}\nproject_dir={project_text}\nrun_name=impl_1"}
            if "log_begin=__VMCP_LOG_BEGIN__" in command:
                return {
                    "ok": True,
                    "raw": (
                        f"sim_dir={project_text}/demo.sim/sim_1/behav/xsim\n"
                        f"log_path={project_text}/demo.sim/sim_1/behav/xsim/xsim.log\n"
                        f"project_dir_before={project_text}\n"
                        f"project_dir_after={project_text}\n"
                        "project_name_before=demo\nproject_name_after=demo\n"
                        "simset_before=sim_1\nsimset_after=sim_1\n"
                        "sim_top_before=tb_top\nsim_top_after=tb_top\n"
                        f"source_snapshot_before={encode_wire_list([f'{project_text}/sim/tb_top.v|100|1'])}\n"
                        f"source_snapshot_after={encode_wire_list([f'{project_text}/sim/tb_top.v|100|1'])}\n"
                        f"wdb_files={encode_wire_list([])}\n"
                        f"vcd_files={encode_wire_list([])}\n"
                        "status_source=simulation_invocation_log_span\n"
                        "simulation_invocation_id=fake-sim-1\n"
                        "ended_at=2026-06-11T00:00:00Z\n"
                        "log_span_start=0\n"
                        "log_span_end=128\n"
                        "log_span_reset_detected=0\n"
                        "log_begin=__VMCP_LOG_BEGIN__\n"
                        "Simulation finished"
                    ),
                }
            if "sources_1" in command and "get_files -quiet -of_objects" in command:
                return {"ok": True, "raw": _fileset_file_row(source_dir / "top.v", "Verilog")}
            if "constrs_1" in command and "get_files -quiet -of_objects" in command:
                return {"ok": True, "raw": _fileset_file_row(constr_dir / "top.xdc", "XDC")}
            if "sim_1" in command and "get_files -quiet -of_objects" in command:
                return {"ok": True, "raw": _fileset_file_row(sim_dir / "tb_top.v", "Verilog")}
            return {"ok": True, "raw": ""}

        def status(self) -> dict:
            return {"ok": True, "connected": True, "backend": "fake-vivado", "runtime_dir": "D:/Vivado_Mcp/.vivado_agent_mcp/runtime"}

    fake = DiagnosticFakeSession(project_dir)
    service = VivadoToolService(session=fake)

    result = service.call("collect_diagnostic_bundle", {"run_name": "impl_1", "timestamp": "20260524_140000"})

    assert result["ok"] is True, result
    manifest_path = Path(result["data"]["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["summary"]["audit_status"] == "READY"
    assert manifest["summary"]["complete"] is True
    assert manifest["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert manifest["hardware_validation"]["validated"] is False
    assert manifest["summary"]["hardware_validation"]["status"] == "NOT_VALIDATED"
    session_status = json.loads((manifest_path.parent / "session_status.json").read_text(encoding="utf-8"))
    assert session_status["connected"] is True
    assert session_status["backend"] == "fake-vivado"
    assert "create_project {demo}" in (manifest_path.parent / "replay_project.tcl").read_text(encoding="utf-8")
    assert any("check_syntax" in command for command in fake.commands)
    assert any("report_cdc" in command for command in fake.commands)

    audit_path = manifest_path.parent / "audit_result.json"
    source_audit_bytes = audit_path.read_bytes()
    real_load_json_evidence = tools_module.load_json_evidence
    reusable_audit_reads: list[int] = []
    audit_mutated_after_snapshot = False

    def load_snapshot_then_mutate(path, *, root, max_bytes):
        nonlocal audit_mutated_after_snapshot
        data, snapshot = real_load_json_evidence(path, root=root, max_bytes=max_bytes)
        if Path(path).resolve() == audit_path.resolve() and not audit_mutated_after_snapshot:
            reusable_audit_reads.append(max_bytes)
            audit_path.write_bytes(b'{"mutated_after_snapshot": true}')
            audit_mutated_after_snapshot = True
        return data, snapshot

    monkeypatch.setattr(tools_module, "load_json_evidence", load_snapshot_then_mutate)
    fake.commands.clear()
    reused = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140001",
            "reuse_audit_from_manifest": str(manifest_path),
        },
    )
    audit_path.write_bytes(source_audit_bytes)

    assert reused["ok"] is True, (reused["message"], reused["data"])
    assert audit_mutated_after_snapshot is True
    assert reusable_audit_reads == [tools_module.MAX_REUSABLE_AUDIT_BYTES]
    reused_manifest_path = Path(reused["data"]["manifest_path"])
    reused_audit = json.loads((reused_manifest_path.parent / "audit_result.json").read_text(encoding="utf-8"))
    assert reused_audit["reused_from_manifest"] == str(manifest_path.resolve())

    original_audit_bytes = audit_path.read_bytes()
    original_manifest_bytes = manifest_path.read_bytes()

    audit_path.write_bytes(original_audit_bytes + b"\n")
    tampered = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140010",
            "reuse_audit_from_manifest": str(manifest_path),
        },
    )
    assert tampered["ok"] is False
    assert "size/sha256" in tampered["message"]
    audit_path.write_bytes(original_audit_bytes)

    def write_semantically_modified_audit(payload: dict) -> None:
        audit_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        audit_path.write_bytes(audit_bytes)
        manifest_payload = json.loads(original_manifest_bytes.decode("utf-8"))
        audit_entry = next(item for item in manifest_payload["files"] if item.get("category") == "audit")
        audit_entry["size"] = len(audit_bytes)
        audit_entry["sha256"] = hashlib.sha256(audit_bytes).hexdigest()
        manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    forged_hardware = json.loads(original_audit_bytes.decode("utf-8"))
    forged_hardware["hardware_validation"]["validated"] = True
    write_semantically_modified_audit(forged_hardware)
    rejected_hardware = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140011",
            "reuse_audit_from_manifest": str(manifest_path),
        },
    )
    assert rejected_hardware["ok"] is False
    assert "validated=false" in rejected_hardware["message"]

    wrong_project = json.loads(original_audit_bytes.decode("utf-8"))
    wrong_project["health_summary"]["project_dir"] = str(tmp_path / "another_project")
    write_semantically_modified_audit(wrong_project)
    rejected_project = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140012",
            "reuse_audit_from_manifest": str(manifest_path),
        },
    )
    assert rejected_project["ok"] is False
    assert "project identity" in rejected_project["message"]

    wrong_run = json.loads(original_audit_bytes.decode("utf-8"))
    wrong_run["evidence_freshness"]["run_states"] = []
    write_semantically_modified_audit(wrong_run)
    rejected_run = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140013",
            "reuse_audit_from_manifest": str(manifest_path),
        },
    )
    assert rejected_run["ok"] is False
    assert "exactly one run state" in rejected_run["message"]

    incomplete_run = json.loads(original_audit_bytes.decode("utf-8"))
    target_run = next(
        item
        for item in incomplete_run["evidence_freshness"]["run_states"]
        if item["run_name"] == "impl_1"
    )
    target_run["status"] = "incomplete"
    write_semantically_modified_audit(incomplete_run)
    rejected_incomplete_run = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140014",
            "reuse_audit_from_manifest": str(manifest_path),
        },
    )
    assert rejected_incomplete_run["ok"] is False
    assert "is not complete" in rejected_incomplete_run["message"]

    audit_path.write_bytes(original_audit_bytes)
    manifest_path.write_bytes(original_manifest_bytes)

    fake.run_needs_refresh = "1"
    rejected_current_refresh = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140015",
            "reuse_audit_from_manifest": str(manifest_path),
        },
    )
    assert rejected_current_refresh["ok"] is False
    assert "current Vivado run impl_1 needs refresh" in rejected_current_refresh["message"]
    fake.run_needs_refresh = "0"

    fake.run_status = "incomplete"
    rejected_current_status = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140016",
            "reuse_audit_from_manifest": str(manifest_path),
        },
    )
    assert rejected_current_status["ok"] is False
    assert "current Vivado run impl_1 is not complete" in rejected_current_status["message"]
    fake.run_status = "write_bitstream Complete!"

    stale_timestamp_audit = json.loads(original_audit_bytes.decode("utf-8"))
    stale_timestamp_audit["evidence_freshness"]["checked_at"] = "2020-01-01T00:00:00Z"
    write_semantically_modified_audit(stale_timestamp_audit)
    rejected_old_audit = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140017",
            "reuse_audit_from_manifest": str(manifest_path),
        },
    )
    assert rejected_old_audit["ok"] is False
    assert "older than" in rejected_old_audit["message"]
    audit_path.write_bytes(original_audit_bytes)
    manifest_path.write_bytes(original_manifest_bytes)

    original_audit = json.loads(original_audit_bytes.decode("utf-8"))
    report_manifest_path = Path(original_audit["evidence_freshness"]["report_manifest_path"])
    original_report_manifest = report_manifest_path.read_bytes()
    changed_report_manifest = json.loads(original_report_manifest.decode("utf-8"))
    changed_report_manifest["evidence_freshness"]["collected_at"] = "2099-01-01T00:00:00Z"
    report_manifest_path.write_text(json.dumps(changed_report_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    rejected_report_identity = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140018",
            "reuse_audit_from_manifest": str(manifest_path),
        },
    )
    assert rejected_report_identity["ok"] is False
    assert "report manifest identity" in rejected_report_identity["message"]
    report_manifest_path.write_bytes(original_report_manifest)

    source_bitstream_path.write_bytes(b"new-run-bitstream")
    rejected_current_artifact = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140019",
            "reuse_audit_from_manifest": str(manifest_path),
        },
    )
    assert rejected_current_artifact["ok"] is False
    assert "artifact manifest source entry" in rejected_current_artifact["message"]
    source_bitstream_path.write_bytes(original_bitstream)

    audit_data = json.loads((manifest_path.parent / "audit_result.json").read_text(encoding="utf-8"))
    fake.commands.clear()
    rejected_unregistered_audit = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140002",
            "audit_result": audit_data,
        },
    )
    assert rejected_unregistered_audit["ok"] is False
    assert rejected_unregistered_audit["error_code"] == "INVALID_TOOL_ARGUMENTS"

    provided = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140002",
        },
    )

    assert provided["ok"] is True
    provided_manifest_path = Path(provided["data"]["manifest_path"])
    provided_audit = json.loads((provided_manifest_path.parent / "audit_result.json").read_text(encoding="utf-8"))
    assert provided_audit["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert provided_audit["hardware_validation"]["validated"] is False
    assert any("check_syntax" in command for command in fake.commands)
    assert "reused_from_manifest" not in provided_audit

    stale_audit = json.loads(audit_path.read_text(encoding="utf-8"))
    stale_audit["evidence_freshness"]["status"] = "STALE"
    audit_path.write_text(json.dumps(stale_audit, ensure_ascii=False, indent=2), encoding="utf-8")
    stale = service.call(
        "collect_diagnostic_bundle",
        {
            "run_name": "impl_1",
            "timestamp": "20260524_140002",
            "reuse_audit_from_manifest": str(manifest_path),
        },
    )

    assert stale["ok"] is False
    assert stale["error_code"] == "ValueError"
    assert stale["data"]["failed_step"] == "reuse_audit_from_manifest"


def test_collect_diagnostic_bundle_preserves_report_version_error_code(tmp_path: Path, monkeypatch) -> None:
    project_dir = tmp_path / "project"
    report_manifest = project_dir / "vmcp_reports" / "impl_1" / "report_manifest.json"
    report_manifest.parent.mkdir(parents=True)
    report_manifest.write_text("{}", encoding="utf-8")

    class MinimalSession:
        def status(self) -> dict:
            return {"ok": True, "connected": True, "generation_id": "generation-1"}

    service = VivadoToolService(session=MinimalSession())
    monkeypatch.setattr(
        service,
        "_get_project_state",
        lambda _args: {"ok": True, "data": {"project": {"directory": str(project_dir), "name": "demo"}}},
    )
    monkeypatch.setattr(
        service,
        "_run_project_audit",
        lambda _args: {
            "ok": True,
            "data": {
                "status": "READY",
                "inputs": {"report_manifest": {"manifest_path": str(report_manifest)}},
                "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
            },
        },
    )
    monkeypatch.setattr(service, "_collect_filesets_for_bundle", lambda **_kwargs: {})
    monkeypatch.setattr(service, "_collect_run_configuration_audit", lambda **_kwargs: {"runs": {}})
    monkeypatch.setattr(
        service,
        "_load_existing_report_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            tools_module.ReportManifestValidationError(
                "REPORT_VERSION_MISMATCH",
                "Report evidence was not produced by Vivado 2021.2.",
                data={"manifest_path": str(report_manifest)},
            )
        ),
    )

    result = service.call("collect_diagnostic_bundle", {"run_name": "impl_1"})

    assert result["ok"] is False
    assert result["error_code"] == "REPORT_VERSION_MISMATCH"
    assert result["data"]["failed_step"] == "revalidate_report_manifest"
    assert result["data"]["manifest_path"] == str(report_manifest)


def test_collect_diagnostic_bundle_does_not_attest_rejected_artifact_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / "project"
    artifact_manifest = project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json"
    artifact_manifest.parent.mkdir(parents=True)
    artifact_manifest.write_text("{}", encoding="utf-8")
    captured: dict = {}

    class MinimalSession:
        def status(self) -> dict:
            return {"ok": True, "connected": True, "generation_id": "generation-1"}

    service = VivadoToolService(session=MinimalSession())
    monkeypatch.setattr(
        service,
        "_get_project_state",
        lambda args: {
            "ok": True,
            "data": {
                "project": {
                    "name": "demo",
                    "directory": str(project_dir),
                    "part": "xc7a35tcpg236-1",
                    "top": "top",
                }
            },
        },
    )
    monkeypatch.setattr(
        service,
        "_run_project_audit",
        lambda args: {
            "ok": True,
            "data": {
                "status": "BLOCK",
                "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
            },
        },
    )
    monkeypatch.setattr(service, "_collect_filesets_for_bundle", lambda timeout_s=60: {})
    monkeypatch.setattr(
        service,
        "_collect_run_configuration_audit",
        lambda timeout_s=60: {"status": "READY", "runs": {}, "findings": []},
    )
    monkeypatch.setattr(service, "_collect_log_tail", lambda timeout_s=60: {"vivado": ""})
    monkeypatch.setattr(
        service,
        "_get_artifact_manifest",
        lambda args: {
            "ok": False,
            "error_code": "ARTIFACT_MANIFEST_SCHEMA_UNSUPPORTED",
            "message": "artifact manifest schema_version=4 is required",
            "data": {"manifest_path": str(artifact_manifest)},
        },
    )
    monkeypatch.setattr(
        service,
        "_find_vivado",
        lambda path=None, **kwargs: {"ok": True, "path": "D:/Vivado/vivado.bat", "version": "2021.2"},
    )

    def fake_collect(**kwargs):
        captured.update(kwargs)
        bundle_dir = project_dir / "vmcp_diagnostics" / "bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        return {
            "bundle_dir": str(bundle_dir),
            "manifest_path": str(bundle_dir / "diagnostic_manifest.json"),
            "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
        }

    monkeypatch.setattr("vivado_agent_mcp.tools.collect_diagnostic_bundle_files", fake_collect)

    result = service.call(
        "collect_diagnostic_bundle",
        {"run_name": "impl_1", "timestamp": "20260716_000000"},
    )

    assert result["ok"] is True
    assert captured["artifact_manifest_path"] is None


def test_collect_diagnostic_bundle_timeout_returns_recoverable_progress_context(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    class TimeoutDiagnosticSession:
        def __init__(self, project: Path) -> None:
            self.project = project
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "project_name=$project_name" in command:
                return {
                    "ok": True,
                    "raw": _project_state_raw(self.project),
                }
            if "check_syntax" in command:
                raise TimeoutError("timed out while collecting syntax evidence")
            return {"ok": True, "raw": ""}

        def status(self) -> dict:
            return {"ok": True, "connected": True, "backend": "timeout-fake"}

    service = VivadoToolService(session=TimeoutDiagnosticSession(project_dir))

    result = service.call("collect_diagnostic_bundle", {"run_name": "impl_1", "timestamp": "20260531_060000", "timeout_s": 1})

    assert result["ok"] is False
    assert result["error_code"] == "TimeoutError"
    assert result["data"]["current_step"] == "run_project_audit"
    assert result["data"]["failed_step"] == "run_project_audit"
    assert result["data"]["project_dir"] == str(project_dir)
    partial_output_dir = result["data"]["partial_output_dir"].replace("\\", "/")
    assert partial_output_dir.endswith("vmcp_diagnostics/20260531_060000")
    assert result["data"]["last_successful_artifact"] == "partial_diagnostic_manifest"
    partial_manifest_path = Path(result["data"]["partial_manifest_path"])
    assert partial_manifest_path.exists()
    partial_manifest = json.loads(partial_manifest_path.read_text(encoding="utf-8"))
    assert partial_manifest["summary"]["audit_status"] == "BLOCK"
    audit_result = json.loads((partial_manifest_path.parent / "audit_result.json").read_text(encoding="utf-8"))
    assert audit_result["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert audit_result["evidence_freshness"]["needs_refresh"] is True
    validation = service.call("validate_diagnostic_bundle", {"manifest_path": str(partial_manifest_path)})
    assert validation["ok"] is True
    assert validation["data"]["status"] == "BLOCK"
    assert validation["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert result["data"]["timeout_s_used"] == 1
    assert result["next_actions"][0]["tool"] == "collect_diagnostic_bundle"
    assert any(action["required_args"] == ["run_name", "reuse_audit_from_manifest"] for action in result["next_actions"])
