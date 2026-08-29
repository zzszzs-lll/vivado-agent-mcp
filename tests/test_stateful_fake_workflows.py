import json
import os
import time
from pathlib import Path

from bitstream_fixture import write_test_bitstream, write_test_design_execution_identity
from fakes import StatefulFakeVivadoSession
from vivado_agent_mcp.tools import VivadoToolService
from vivado_agent_mcp.vivado.artifacts import collect_artifacts
from vivado_agent_mcp.vivado.prehardware import collect_report_bundle_files


def test_stateful_fake_workflow_reaches_ready_audit_with_fresh_evidence(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    _write_fresh_manifests(project_dir)
    service = VivadoToolService(session=StatefulFakeVivadoSession(project_dir))

    result = service.call("run_project_audit", {"run_name": "impl_1", "timeout_s": 1})

    assert result["ok"] is True
    assert result["data"]["status"] == "READY"
    assert result["data"]["effective_status"] == "READY"
    assert result["data"]["validation_scope"] == "pre_hardware_software"
    assert "without real FPGA board validation" in result["data"]["ready_meaning"]
    assert result["data"]["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert result["data"]["evidence_freshness"]["status"] == "FRESH"


def test_stateful_fake_workflow_accepts_root_artifact_manifest_from_agent_output_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    _write_fresh_manifests(project_dir)
    run_manifest = project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json"
    root_manifest = project_dir / "vmcp_artifacts" / "manifest.json"
    manifest_data = json.loads(run_manifest.read_text(encoding="utf-8"))
    manifest_data["manifest_path"] = str(root_manifest)
    manifest_data["output_dir"] = str(root_manifest.parent)
    root_manifest.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
    run_manifest.unlink()
    service = VivadoToolService(session=StatefulFakeVivadoSession(project_dir))

    audit = service.call("run_project_audit", {"run_name": "impl_1", "timeout_s": 1})
    bundle = service.call("collect_diagnostic_bundle", {"run_name": "impl_1", "timestamp": "20260530_121500", "timeout_s": 1})

    assert audit["ok"] is True
    assert audit["data"]["evidence_freshness"]["status"] == "FRESH"
    assert audit["data"]["inputs"]["artifact_manifest"]["manifest_path"] == str(root_manifest.resolve())
    assert bundle["ok"] is True
    manifest = json.loads(Path(bundle["data"]["manifest_path"]).read_text(encoding="utf-8"))
    assert "artifact_manifest" in {item["category"] for item in manifest["files"]}


def test_stateful_fake_workflow_blocks_empty_artifact_manifest_during_audit(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    _write_fresh_manifests(project_dir)
    manifest_path = project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    service = VivadoToolService(session=StatefulFakeVivadoSession(project_dir))

    result = service.call("run_project_audit", {"run_name": "impl_1", "timeout_s": 1})

    assert result["ok"] is True
    assert result["data"]["status"] == "BLOCK"
    artifact_findings = [
        item for item in result["data"]["active_findings"] if item.get("source_tool") == "get_artifact_manifest"
    ]
    assert artifact_findings
    assert artifact_findings[0]["code"] == "ARTIFACT_MANIFEST_REJECTED"


def test_stateful_fake_workflow_exposes_timing_blocker_next_actions(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    _write_fresh_manifests(project_dir)
    service = VivadoToolService(session=StatefulFakeVivadoSession(project_dir, timing_met=False))

    result = service.call("run_pre_hw_signoff", {"run_name": "impl_1", "timeout_s": 1})

    assert result["ok"] is True
    assert result["data"]["status"] == "BLOCK"
    assert "Timing is not met" in result["data"]["reasons"]
    assert {action["tool"] for action in result["next_actions"]} >= {"check_timing_constraints", "analyze_timing_closure"}


def test_stateful_fake_workflow_blocks_artifact_evidence_when_run_needs_refresh(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    _write_fresh_manifests(project_dir)
    service = VivadoToolService(session=StatefulFakeVivadoSession(project_dir, needs_refresh="1"))

    result = service.call("run_project_audit", {"run_name": "impl_1", "timeout_s": 1})

    assert result["ok"] is True
    assert result["data"]["status"] == "BLOCK"
    assert result["data"]["evidence_freshness"]["status"] == "STALE"
    assert any(item["source_tool"] == "get_artifact_manifest" for item in result["data"]["active_findings"])
    assert any(item["code"] == "EVIDENCE_STALE" for item in result["data"]["active_findings"])


def test_stateful_fake_workflow_preserves_ready_with_waivers_semantics(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    _write_fresh_manifests(project_dir, include_power=False)
    service = VivadoToolService(session=StatefulFakeVivadoSession(project_dir))

    initial = service.call("run_project_audit", {"run_name": "impl_1", "timeout_s": 1})
    waiver_results = [
        service.call(
            "create_signoff_waiver",
            {
                "id": f"reviewed-{index}",
                "finding_fingerprint": finding["finding_fingerprint"],
                "evidence_identity_sha256": initial["data"]["waiver_evidence_identity_sha256"],
                "code": finding["code"],
                "reason": "Test waiver for one exact reviewed stale-evidence finding.",
                "owner": "test",
            },
        )
        for index, finding in enumerate(initial["data"]["active_findings"])
    ]
    result = service.call("run_project_audit", {"run_name": "impl_1", "timeout_s": 1})

    assert all(item["ok"] is True for item in waiver_results)
    assert result["ok"] is True
    assert result["data"]["status"] == "READY"
    assert result["data"]["effective_status"] == "READY_WITH_WAIVERS"
    assert result["data"]["waiver_summary"]["waived_finding_count"] >= 1
    assert result["next_actions"][0]["tool"] == "list_signoff_waivers"


def test_stateful_fake_workflow_collects_and_validates_diagnostic_bundle(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    _write_fresh_manifests(project_dir)
    service = VivadoToolService(session=StatefulFakeVivadoSession(project_dir))

    audit = service.call("run_project_audit", {"run_name": "impl_1", "timeout_s": 1})
    bundle = service.call("collect_diagnostic_bundle", {"run_name": "impl_1", "timestamp": "20260530_120000", "timeout_s": 1})
    validated = service.call("validate_diagnostic_bundle", {"manifest_path": bundle["data"]["manifest_path"]})

    assert audit["data"]["status"] == "READY"
    assert bundle["ok"] is True
    assert validated["ok"] is True
    assert validated["data"]["status"] == "WARN"
    assert validated["data"]["resume_context"]["handoff_ready"] is False
    assert validated["data"]["resume_context"]["handoff_reviewable"] is True
    assert validated["data"]["resume_context"]["evidence_freshness_status"] == "FRESH"


def _write_fresh_manifests(project_dir: Path, *, include_power: bool = True) -> None:
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    started_marker = run_dir / ".vivado.begin.rst"
    ended_marker = run_dir / ".vivado.end.rst"
    started_marker.write_text("start", encoding="utf-8")
    bitstream = run_dir / "top.bit"
    write_test_bitstream(bitstream)
    ended_marker.write_text("end", encoding="utf-8")
    base_ns = time.time_ns() - 10_000_000
    os.utime(started_marker, ns=(base_ns, base_ns))
    os.utime(bitstream, ns=(base_ns + 1_000_000, base_ns + 1_000_000))
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
            "expected_bitstream_path": str(bitstream),
            "run_bitstream_files": [str(bitstream)],
            "write_bitstream_step_enabled": "1",
            "write_bitstream_step_status": "Complete!",
            "session_generation_id": "test-generation",
        },
        collection_id="artifact_stateful_fake",
    )

    report_dir = project_dir / "vmcp_reports" / "impl_1"
    report_dir.mkdir(parents=True)
    report_names = {
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
    report_contents = {
        "timing_summary.rpt": "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.100 0.000\n",
        "utilization.rpt": "Slice LUTs | 1 | 100 | 1.00\n",
        "drc.rpt": "DRC report: no violations\n",
        "methodology.rpt": "Methodology report: no violations\n",
        "qor_summary.rpt": "QoR summary: timing met\n",
        "cdc.rpt": "Safe 1\nUnsafe 0\nUnknown 0\n",
        "clock_interaction.rpt": "Safe 1\nUnsafe 0\nUnknown 0\n",
        "power.rpt": "Total On-Chip Power (W): 0.1\n",
        "messages.log": "Vivado Agent MCP report bundle generated.\n",
    }
    for filename, content in report_contents.items():
        if filename == "power.rpt" and not include_power:
            continue
        (report_dir / filename).write_text(content, encoding="utf-8")
    present_reports = [report_dir / filename for filename in report_names.values() if (report_dir / filename).is_file()]
    started_ms = min(int(path.stat().st_mtime * 1000) for path in present_reports) - 100
    report_context = {
        "vivado_version_short": "2021.2",
        "vivado_build": "Vivado v2021.2",
        "report_command_schema": "vivado_2021_2_v1",
        "collection_id": "report_stateful_fake",
        "collection_started_ms": str(started_ms),
        "open_run_status": "generated",
        "run_status": "write_bitstream Complete!",
        "run_progress": "100%",
        "run_needs_refresh": "0",
        "run_directory": str(run_dir),
        "session_generation_id": "test-generation",
        "design_execution_identity": write_test_design_execution_identity(project_dir),
    }
    run_log = run_dir / "runme.log"
    run_log.write_text("INFO: stateful fake run complete\n", encoding="utf-8")
    report_context.update(
        {
            "run_log_path": str(run_log),
            "run_log_size_before": str(run_log.stat().st_size),
            "run_log_size_after": str(run_log.stat().st_size),
            "run_log_mtime_before": str(int(run_log.stat().st_mtime)),
            "run_log_mtime_after": str(int(run_log.stat().st_mtime)),
            "messages_complete_scan": "1",
            "messages_source_stable": "1",
            "messages_extracted_count": "0",
        }
    )
    for category in report_names:
        report_context[f"{category}_report_command_status"] = "generated"
        report_context[f"{category}_report_command_message"] = "stateful fake report collection"
    collect_report_bundle_files(
        report_dir=report_dir,
        run_name="impl_1",
        project_dir=project_dir,
        report_context=report_context,
    )
