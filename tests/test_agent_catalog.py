import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vivado_agent_mcp.result import failure, success
from vivado_agent_mcp.server import TOOL_DEFS, _output_schema
from vivado_agent_mcp.tools import VivadoToolService
from vivado_agent_mcp.vivado.evidence_store import verify_evidence_reference
from vivado_agent_mcp.vivado.evidence_attestation import (
    attest_diagnostic_manifest,
    resolve_attestation_trust_dir,
    verify_diagnostic_manifest_attestation,
)
from vivado_agent_mcp.vivado.workflow_trace import WorkflowTracer


def test_result_contract_promotes_message_next_steps_next_actions_and_hardware_boundary() -> None:
    hardware_validation = {"status": "NOT_VALIDATED", "validated": False}
    resume_context = {"status": "READY", "handoff_ready": True, "recommended_entrypoint": "get_agent_workflows"}
    next_actions = [
        {
            "tool": "run_project_audit",
            "reason": "Refresh project audit evidence.",
            "required_args": ["run_name"],
            "arg_sources": {"run_name": "workflow.run_name"},
            "preconditions": ["Project is open."],
            "stop_condition": "audit status is READY",
            "optional": False,
        }
    ]

    ok_result = success(
        "run_project_audit",
        "Project audit status: READY.",
        {
            "next_steps": ["Archive the diagnostic bundle."],
            "next_actions": next_actions,
            "hardware_validation": hardware_validation,
            "resume_context": resume_context,
            "handoff_reviewable": True,
        },
    )
    failed_result = failure(
        "run_project_audit",
        "AUDIT_INPUT_FAILED",
        "Project audit could not run.",
        data={
            "next_steps": ["Run get_project_state first."],
            "next_actions": next_actions,
            "hardware_validation": hardware_validation,
            "resume_context": resume_context,
            "handoff_reviewable": True,
        },
    )

    assert ok_result["message"] == ok_result["summary"]
    assert ok_result["schema_version"] == 1
    assert ok_result["error_code"] == ""
    assert ok_result["next_steps"] == ["Archive the diagnostic bundle."]
    assert ok_result["next_actions"] == next_actions
    assert ok_result["hardware_validation"] == hardware_validation
    assert ok_result["resume_context"] == resume_context
    assert ok_result["handoff_reviewable"] is True
    assert ok_result["assessment_status"] == "READY"
    assert ok_result["stop_required"] is False
    assert ok_result["handoff_ready"] is True
    assert failed_result["summary"] == failed_result["message"]
    assert failed_result["schema_version"] == 1
    assert failed_result["next_steps"] == ["Run get_project_state first."]
    assert failed_result["next_actions"] == next_actions
    assert failed_result["hardware_validation"] == hardware_validation
    assert failed_result["resume_context"] == resume_context
    assert failed_result["handoff_reviewable"] is False
    assert failed_result["assessment_status"] == "BLOCK"
    assert failed_result["stop_required"] is True
    assert failed_result["handoff_ready"] is False


def test_result_contract_normalizes_domain_assessment_status() -> None:
    blocked = success("validate_diagnostic_bundle", "Bundle blocked.", {"status": "BLOCK", "handoff_ready": True})
    warned = success("run_project_audit", "Audit review required.", {"effective_status": "READY_WITH_WAIVERS"})
    ready = success("collect_build_artifacts", "Artifacts ready.", {"status": "READY", "handoff_ready": True})

    assert (blocked["assessment_status"], blocked["stop_required"], blocked["handoff_ready"]) == ("BLOCK", True, False)
    assert (warned["assessment_status"], warned["stop_required"]) == ("WARN", True)
    assert (ready["assessment_status"], ready["stop_required"], ready["handoff_ready"]) == ("READY", False, True)


def test_result_contract_reduces_nested_statuses_and_never_handoffs_warn_or_stale() -> None:
    warned = success(
        "run_project_audit",
        "Nested review required.",
        {"status": "READY", "health": {"status": "WARN", "handoff_ready": True}, "handoff_ready": True},
    )
    stale = success(
        "validate_diagnostic_bundle",
        "Nested evidence is stale.",
        {"status": "READY", "validation": {"evidence_freshness": {"status": "STALE"}}, "handoff_ready": True},
    )

    assert (warned["assessment_status"], warned["stop_required"], warned["handoff_ready"]) == ("WARN", True, False)
    assert (stale["assessment_status"], stale["stop_required"], stale["handoff_ready"]) == ("BLOCK", True, False)


def test_result_contract_reduces_statuses_under_unknown_nested_containers() -> None:
    result = success(
        "validate_diagnostic_bundle",
        "Unknown evidence containers must not bypass outcome reduction.",
        {
            "status": "READY",
            "evidence": {"custom": [{"producer": {"status": "BLOCK"}}]},
            "handoff_ready": True,
        },
    )

    assert result["assessment_status"] == "BLOCK"
    assert result["stop_required"] is True
    assert result["handoff_ready"] is False


@pytest.mark.parametrize("child_key", ["inputs", "partial_evidence"])
def test_result_contract_reduces_additional_assessment_children(child_key: str) -> None:
    result = success(
        "collect_diagnostic_bundle",
        "Nested evidence must block handoff.",
        {"status": "READY", child_key: [{"status": "BLOCK"}], "handoff_ready": True},
    )

    assert result["assessment_status"] == "BLOCK"
    assert result["stop_required"] is True
    assert result["handoff_ready"] is False


def test_service_enforces_selected_tool_profile_before_dispatch() -> None:
    service = VivadoToolService(tool_profile="core", enforce_tool_profile=True)

    blocked = service.call("run_tcl", {"command": "get_projects"})
    catalog = service.call("get_tool_catalog", {})

    assert blocked["error_code"] == "TOOL_NOT_AVAILABLE_IN_PROFILE"
    assert blocked["data"]["active_tool_profile"] == "core"
    assert catalog["data"]["active_tool_profile"] == "core"
    assert catalog["data"]["hidden_tool_count"] > 0


def test_local_attestation_uses_user_trust_dir_not_runtime(tmp_path: Path, monkeypatch) -> None:
    trust_dir = tmp_path / "user-trust"
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("VIVADO_AGENT_MCP_TRUST_DIR", str(trust_dir))
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))
    manifest = {"schema_version": 2, "project_dir": str(tmp_path / "project"), "files": []}

    manifest["authenticity"] = attest_diagnostic_manifest(manifest)
    verification = verify_diagnostic_manifest_attestation(manifest)

    assert resolve_attestation_trust_dir() == trust_dir.resolve()
    assert verification["status"] == "READY"
    assert manifest["authenticity"]["trust_scope"] == "local_os_user"
    assert manifest["authenticity"]["portable"] is False
    assert Path(manifest["authenticity"]["ledger_path"]).is_relative_to(trust_dir.resolve())
    assert not runtime_dir.exists()


def test_local_attestation_is_review_only_under_different_user_anchor(tmp_path: Path, monkeypatch) -> None:
    first_trust = tmp_path / "trust-a"
    second_trust = tmp_path / "trust-b"
    monkeypatch.setenv("VIVADO_AGENT_MCP_TRUST_DIR", str(first_trust))
    manifest = {"schema_version": 2, "project_dir": str(tmp_path / "project"), "files": []}
    manifest["authenticity"] = attest_diagnostic_manifest(manifest)
    monkeypatch.setenv("VIVADO_AGENT_MCP_TRUST_DIR", str(second_trust))
    attest_diagnostic_manifest({"schema_version": 2, "project_dir": "other", "files": []})

    verification = verify_diagnostic_manifest_attestation(manifest)

    assert verification["status"] == "WARN"
    assert verification["code"] == "ATTESTATION_TRUST_ANCHOR_MISMATCH"


def test_tool_catalog_exposes_agent_capability_matrix() -> None:
    service = VivadoToolService()

    result = service.call("get_tool_catalog", {})

    assert result["ok"] is True
    assert result["data"]["version"] == 1
    assert result["data"]["tool_count"] >= 89
    groups = {group["id"]: group for group in result["data"]["groups"]}
    assert {"project", "simulation", "runs", "reports", "diagnostics", "runtime_lightweight", "hardware_boundary"} <= set(groups)
    assert "create_project" in groups["project"]["tools"]
    assert "run_behavioral_simulation" in groups["simulation"]["tools"]
    assert "run_project_audit" in groups["diagnostics"]["tools"]
    assert "get_runtime_cache_status" in groups["runtime_lightweight"]["tools"]
    assert "clean_runtime_cache" in groups["runtime_lightweight"]["tools"]
    assert groups["ip"]["execution_status"] == "BLOCKED_UNSUPPORTED_COMPOSITE_INPUT"
    assert groups["block_design"]["execution_status"] == "BLOCKED_UNSUPPORTED_COMPOSITE_INPUT"
    assert "reject" in groups["ip"]["purpose"]
    assert "reject" in groups["block_design"]["purpose"]
    assert result["data"]["support_status"]["maturity"] == "ALPHA"
    assert result["data"]["support_status"]["hardware_validation_status"] == "NOT_VALIDATED"
    assert result["data"]["managed_execution_boundary"]["supported_design_shape"] == "trusted_pure_rtl_xdc_project_mode"
    assert result["data"]["managed_execution_boundary"]["composite_inputs"] == "BLOCKED_UNSUPPORTED_COMPOSITE_INPUT"
    assert result["data"]["hardware_boundary"]["status"] == "NOT_VALIDATED"
    assert result["data"]["hardware_boundary"]["default_hardware_mode"] == "no_board"
    assert result["data"]["hardware_boundary"]["hardware_tools_disabled_by_default"] is True
    assert result["data"]["hardware_boundary"]["server_policy_env"] == "VIVADO_AGENT_MCP_HARDWARE_MODE"
    assert result["data"]["hardware_boundary"]["tool_tiers"]["hardware_safe_detector"] == ["detect_hardware_environment"]
    assert result["data"]["hardware_boundary"]["tool_tiers"]["hardware_log_readonly"] == ["get_hardware_messages"]
    assert "list_hw_targets" in result["data"]["hardware_boundary"]["tool_tiers"]["hardware_disabled_by_default"]
    assert "program_hw_device" in result["data"]["hardware_boundary"]["tool_tiers"]["hardware_destructive"]
    assert result["data"]["tool_metadata"]["diagnose_run_failure"]["risk"] == "normal"
    assert result["data"]["recommended_entrypoints"]["new_project_to_bitstream"] == "get_agent_workflows"


def test_tool_catalog_groups_cover_all_service_tools() -> None:
    service = VivadoToolService()

    result = service.call("get_tool_catalog", {})

    tool_names = set(service.tool_names())
    grouped_tools = {
        tool
        for group in result["data"]["groups"]
        for tool in group["tools"]
    }
    assert grouped_tools == tool_names


def test_agent_workflows_explain_inputs_tool_order_and_stop_conditions() -> None:
    service = VivadoToolService()

    result = service.call("get_agent_workflows", {})

    assert result["ok"] is True
    workflows = {workflow["id"]: workflow for workflow in result["data"]["workflows"]}
    assert {
        "new_project_to_bitstream",
        "existing_project_audit",
        "simulation_failure_repair",
        "timing_failure_repair",
        "diagnostic_bundle_handoff",
        "project_closeout_cleanup",
    } <= set(workflows)
    new_project = workflows["new_project_to_bitstream"]
    assert "part" in new_project["required_inputs"]
    assert "fpga_part" not in new_project["required_inputs"]
    assert "create_project" in new_project["tool_sequence"]
    assert "repair_project_setup" in new_project["tool_sequence"]
    assert "collect_report_bundle" in new_project["tool_sequence"]
    assert "collect_diagnostic_bundle" in new_project["tool_sequence"]
    assert "validate_diagnostic_bundle" in new_project["tool_sequence"]
    assert "export_project_replay_script" not in workflows["diagnostic_bundle_handoff"]["tool_sequence"]
    assert "get_runtime_cache_status" in workflows["project_closeout_cleanup"]["tool_sequence"]
    assert "clean_runtime_cache" in workflows["project_closeout_cleanup"]["tool_sequence"]
    service_tools = set(service.tool_names())
    schema_properties = _tool_schema_properties()
    for workflow in workflows.values():
        assert set(workflow["tool_sequence"]) <= service_tools
        assert workflow["steps"]
        assert [step["tool"] for step in workflow["steps"]] == workflow["tool_sequence"]
        for step in workflow["steps"]:
            assert set(step["required_args"]) <= schema_properties[step["tool"]]
        assert set(step) >= {
            "tool",
            "required_args",
            "arg_sources",
            "preconditions",
            "failure_stop_conditions",
            "success_artifacts",
            "tier",
            "max_wait_s",
            "poll_interval_s",
            "completion_gate",
            "partial_handoff_condition",
        }
        assert step["tier"] in {"core_bitstream", "handoff_required", "cleanup", "diagnostic", "repair"}
        assert isinstance(step["max_wait_s"], int)
        assert isinstance(step["poll_interval_s"], int)
        assert isinstance(step["completion_gate"], str)
        assert isinstance(step["partial_handoff_condition"], str)
    tiers = {step["tool"]: step["tier"] for step in new_project["steps"]}
    waits = {step["tool"]: step["max_wait_s"] for step in new_project["steps"]}
    assert tiers["generate_bitstream"] == "core_bitstream"
    assert tiers["collect_build_artifacts"] == "handoff_required"
    assert tiers["run_project_audit"] == "handoff_required"
    assert tiers["validate_diagnostic_bundle"] == "handoff_required"
    assert waits["start_session"] == 240
    start_step = next(step for step in new_project["steps"] if step["tool"] == "start_session")
    assert any("VIVADO_PATH" in item for item in start_step["preconditions"])
    assert all("vivado_path is supplied" not in item for item in start_step["preconditions"])
    assert "diagnostic bundle" in new_project["completion_gate"].lower()
    assert any("NOT_VALIDATED" in item for item in result["data"]["global_boundaries"])
    assert any("Alpha" in item for item in result["data"]["global_boundaries"])
    assert any("IP/XCI/Block Design" in item for item in result["data"]["global_boundaries"])


def test_server_output_schema_requires_unified_response_contract() -> None:
    schema = _output_schema()

    assert {
        "schema_version",
        "ok",
        "tool",
        "summary",
        "message",
        "error_code",
        "data",
        "assessment_status",
        "stop_required",
        "handoff_ready",
    } <= set(schema["required"])
    assert schema["properties"]["schema_version"]["type"] == "integer"
    assert "next_actions" in schema["properties"]
    assert "resume_context" in schema["properties"]
    assert "handoff_reviewable" in schema["properties"]


def test_tool_catalog_exposes_workflow_trace_entrypoint() -> None:
    service = VivadoToolService()

    result = service.call("get_tool_catalog", {})

    assert "get_agent_scenarios" in result["data"]["groups"][0]["tools"]
    assert result["data"]["recommended_entrypoints"]["subagent_validation_scenarios"] == "get_agent_scenarios"
    assert "get_workflow_trace_status" in result["data"]["groups"][0]["tools"]
    assert "get_workflow_trace_status" in service.tool_names()


def test_agent_scenarios_expose_reusable_subagent_benchmarks() -> None:
    service = VivadoToolService()

    result = service.call("get_agent_scenarios", {})

    assert result["ok"] is True
    data = result["data"]
    assert data["version"] == 1
    assert data["scenario_count"] == 8
    assert data["selected_count"] == 8
    scenarios = {scenario["id"]: scenario for scenario in data["scenarios"]}
    assert set(scenarios) == {"S00", "S01", "S02", "S03", "S04", "S05", "S06", "S07"}
    assert scenarios["S00"]["available"] is True
    assert "get_agent_scenarios" in scenarios["S00"]["required_tools"]
    assert scenarios["S02"]["workflow_id"] == "new_project_to_bitstream"
    assert "repair_project_setup" in scenarios["S02"]["required_tools"]
    assert scenarios["S02"]["example_project"]["rtl_files"] == ["clock_enable.sv", "pwm_core.sv", "pwm_breathing_top.sv"]
    assert scenarios["S02"]["example_project"]["testbench_top"] == "tb_pwm_breathing_top"
    assert scenarios["S02"]["example_project"]["simulation"]["run_time"]
    assert scenarios["S03"]["runner_coverage"]["execution_mode"] == "mcp_stdio_fake_session"
    assert scenarios["S03"]["runner_coverage"]["evidence_class"] == "stdio_fake_session_contract"
    assert scenarios["S03"]["runner_coverage"]["full_scenario_coverage"] is False
    assert scenarios["S03"]["runner_coverage"]["live_execution_mode"] == "mcp_stdio_live_xsim_repair"
    assert scenarios["S03"]["runner_coverage"]["live_requires_flag"] == "--include-live-vivado"
    assert "does not run synthesis" in scenarios["S03"]["runner_coverage"]["live_limitation"]
    assert scenarios["S07"]["runner_coverage"]["execution_mode"] == "mcp_stdio_synthetic_bundle"
    assert scenarios["S07"]["runner_coverage"]["full_scenario_coverage"] is False
    assert scenarios["S07"]["runner_coverage"]["live_execution_mode"] == "mcp_stdio_live_existing_project_handoff"
    assert scenarios["S07"]["runner_coverage"]["live_requires_flag"] == "--include-live-vivado"
    assert "hardware manager" in scenarios["S07"]["runner_coverage"]["live_limitation"]
    assert data["feedback_template"]["hardware_validation_status"] == "NOT_VALIDATED"
    policy = data["validation_policy"]
    assert policy["id"] == "default_no_board_scenario_matrix"
    assert policy["required_no_live_scenarios"] == ["S00", "S03", "S04", "S05", "S06", "S07"]
    assert policy["required_live_scenarios"] == ["S01", "S02", "S03", "S07"]
    payload_text = json.dumps(data, ensure_ascii=False)
    assert "D:\\Vivado_Mcp" not in payload_text
    assert "D:/Vivado_Mcp" not in payload_text
    assert "D:\\Python312" not in payload_text
    assert "D:/Python312" not in payload_text
    assert data["recommended_artifact_root"] == "<workspace>/test_use"
    assert "NOT_VALIDATED" in policy["hardware_boundary"]
    assert any("NOT_VALIDATED" in item for item in data["global_boundaries"])
    assert any("does not attest distribution readiness" in item for item in data["global_boundaries"])
    service_tools = set(service.tool_names())
    for scenario in data["scenarios"]:
        assert scenario["available"] is True
        assert scenario["missing_tools"] == []
        assert set(scenario["required_tools"]) <= service_tools
        assert scenario["acceptance"]
        assert scenario["stop_conditions"]
        assert scenario["runner_coverage"]["execution_mode"]
        assert scenario["runner_coverage"]["evidence_class"]
        assert isinstance(scenario["runner_coverage"]["full_scenario_coverage"], bool)
    _assert_next_actions_are_valid(result["next_actions"])


def test_agent_scenarios_can_filter_or_report_unknown_id() -> None:
    service = VivadoToolService()

    filtered = service.call("get_agent_scenarios", {"scenario_id": "s04"})
    missing = service.call("get_agent_scenarios", {"scenario_id": "s99"})

    assert filtered["ok"] is True
    assert filtered["data"]["scenario_id"] == "S04"
    assert [scenario["id"] for scenario in filtered["data"]["scenarios"]] == ["S04"]
    assert missing["ok"] is False
    assert missing["error_code"] == "AGENT_SCENARIO_NOT_FOUND"
    assert missing["data"]["scenario_id"] == "S99"
    assert missing["data"]["scenarios"] == []
    assert missing["next_actions"][0]["tool"] == "get_agent_scenarios"
    _assert_next_actions_are_valid(missing["next_actions"])


def test_validate_diagnostic_bundle_marks_reference_manifest_reviewable_not_ready(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    assert result["ok"] is True
    health = result["data"]["health"]
    assert health["status"] == "WARN"
    assert health["handoff_ready"] is False
    assert health["handoff_reviewable"] is True
    assert health["bundle_mode"] == "reference"
    assert health["portable"] is False
    assert health["bundle_mode_supported"] is True
    assert health["portable_mode_supported"] is False
    assert health["missing_required_categories"] == []
    assert health["missing_files"] == []
    assert health["hash_mismatches"] == []
    assert health["path_escapes"] == []
    assert health["invalid_entries"] == []
    assert health["hardware_validation_missing"] is False
    assert result["data"]["status"] == "WARN"
    assert result["data"]["handoff_ready"] is False
    assert result["data"]["resume_context"]["handoff_ready"] is False
    assert result["data"]["resume_context"]["handoff_reviewable"] is True
    assert result["data"]["resume_context"]["recommended_entrypoint"] == "get_agent_workflows"
    assert result["resume_context"] == result["data"]["resume_context"]
    assert result["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert result["next_actions"][0]["tool"] == "get_agent_workflows"
    _assert_next_actions_are_valid(result["next_actions"])
    assert "not self-contained" in "\n".join(result["data"]["next_steps"])


def test_validate_diagnostic_bundle_rejects_self_declared_portable_mode(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bundle_mode"] = "portable"
    manifest["portable"] = True
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    result = VivadoToolService().call(
        "validate_diagnostic_bundle",
        {"manifest_path": str(manifest_path)},
    )

    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert health["handoff_ready"] is False
    assert health["portable"] is False
    assert health["declared_portable"] is True
    assert health["bundle_mode_supported"] is False
    assert health["portable_mode_supported"] is False
    assert "portable_bundle_mode_not_supported" in {
        item["reason"] for item in health["invalid_entries"]
    }


def test_validate_diagnostic_bundle_accepts_append_only_workflow_trace_growth(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    trace_entry = next(entry for entry in data["files"] if entry["category"] == "workflow_trace")
    trace_path = Path(trace_entry["path"])
    captured_size = trace_path.stat().st_size
    tracer = WorkflowTracer(trace_id="handoff", trace_dir=trace_path.parent)
    tracer.sequence = 1
    now = datetime.now(UTC)
    tracer.record(
        tool="validate_diagnostic_bundle",
        args={"manifest_path": str(manifest_path)},
        result={"ok": True, "tool": "validate_diagnostic_bundle", "data": {"status": "READY"}},
        started_at=now,
        ended_at=now,
    )
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "WARN"
    assert health["handoff_ready"] is False
    assert health["handoff_reviewable"] is True
    assert health["hash_mismatches"] == []
    assert health["size_mismatches"] == []
    assert health["workflow_trace_append_only_growth"][0]["captured_size"] == str(captured_size)
    assert result["data"]["resume_context"]["workflow_trace_captured_size"] == captured_size
    assert result["data"]["resume_context"]["workflow_trace_append_only_growth"]
    assert health["workflow_trace_integrity"]["status"] == "READY"


def test_validate_diagnostic_bundle_blocks_invalid_appended_workflow_trace_record(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    trace_entry = next(entry for entry in data["files"] if entry["category"] == "workflow_trace")
    trace_path = Path(trace_entry["path"])
    with trace_path.open("a", encoding="utf-8") as handle:
        handle.write('{"trace_id":"handoff","ledger_sequence":2}\n')

    result = VivadoToolService().call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    assert result["data"]["health"]["status"] == "BLOCK"
    assert result["data"]["health"]["workflow_trace_integrity"]["status"] == "BLOCK"
    assert result["data"]["handoff_ready"] is False


def test_validate_diagnostic_bundle_blocks_mutated_workflow_trace_prefix(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    trace_entry = next(entry for entry in data["files"] if entry["category"] == "workflow_trace")
    trace_path = Path(trace_entry["path"])
    trace_path.write_text(trace_path.read_text(encoding="utf-8").replace("run_project_audit", "tampered_audit", 1), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert health["hash_mismatches"]
    assert health["workflow_trace_append_only_growth"] == []


def test_validate_diagnostic_bundle_blocks_missing_files_and_bad_hash(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["files"][0]["sha256"] = "0" * 64
    Path(data["files"][1]["path"]).unlink()
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    assert result["ok"] is True
    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert health["handoff_ready"] is False
    assert health["missing_files"]
    assert health["hash_mismatches"]
    assert {action["tool"] for action in result["next_actions"]} >= {"collect_diagnostic_bundle", "validate_diagnostic_bundle"}
    assert result["data"]["resume_context"]["handoff_ready"] is False
    _assert_next_actions_are_valid(result["next_actions"])
    assert "Rebuild collect_diagnostic_bundle" in "\n".join(result["data"]["next_steps"])


def test_validate_diagnostic_bundle_blocks_coherent_project_bundle_replacement_without_runtime_anchor(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    audit_entry = next(entry for entry in data["files"] if entry["category"] == "audit")
    audit_path = Path(audit_entry["path"])
    audit_path.write_text('{"status":"READY","forged":true}', encoding="utf-8")
    audit_entry["size"] = audit_path.stat().st_size
    audit_entry["sha256"] = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = VivadoToolService().call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["hash_mismatches"] == []
    assert health["size_mismatches"] == []
    assert health["authenticity"]["status"] == "BLOCK"
    assert health["authenticity"]["code"] == "ATTESTATION_PAYLOAD_MISMATCH"
    assert health["status"] == "BLOCK"
    assert health["handoff_ready"] is False


def test_validate_diagnostic_bundle_blocks_re_attested_nested_design_identity_mismatch(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    bundle_dir = manifest_path.parent
    artifact_dir = tmp_path / "vmcp_artifacts" / "impl_1"
    report_dir = tmp_path / "vmcp_reports" / "impl_1"
    artifact_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    bitstream_path = artifact_dir / "demo.bit"
    report_path = report_dir / "timing_summary.rpt"
    bitstream_path.write_bytes(b"test-bitstream")
    report_path.write_text("timing summary\n", encoding="utf-8")

    artifact_identity = {"status": "READY", "sha256": "d" * 64, "identity": {"top": "demo"}}
    report_identity = {"status": "READY", "sha256": "e" * 64, "identity": {"top": "demo_changed"}}
    common = {
        "schema_version": 4,
        "status": "READY",
        "run_name": "impl_1",
        "run_snapshot": {"session_generation_id": "test-generation"},
        "evidence_freshness": {"status": "FRESH", "needs_refresh": False},
    }
    artifact_manifest = {
        **common,
        "design_execution_identity": artifact_identity,
        "design_execution_identity_sha256": artifact_identity["sha256"],
        "artifacts": [
            {
                "category": "bitstream",
                "export_path": str(bitstream_path),
                "size": bitstream_path.stat().st_size,
                "sha256": hashlib.sha256(bitstream_path.read_bytes()).hexdigest(),
            }
        ],
    }
    report_manifest = {
        **common,
        "design_execution_identity": report_identity,
        "design_execution_identity_sha256": report_identity["sha256"],
        "reports": [
            {
                "category": "timing_summary",
                "path": str(report_path),
                "size": report_path.stat().st_size,
                "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            }
        ],
    }
    nested_paths = {
        "artifact_manifest": bundle_dir / "artifact_manifest.json",
        "report_manifest": bundle_dir / "report_manifest.json",
    }
    nested_paths["artifact_manifest"].write_text(
        json.dumps(artifact_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    nested_paths["report_manifest"].write_text(
        json.dumps(report_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    diagnostic_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    diagnostic_manifest["summary"]["design_execution_identity_sha256"] = artifact_identity["sha256"]
    for category, nested_path in nested_paths.items():
        diagnostic_manifest["files"].append(
            {
                "path": str(nested_path),
                "category": category,
                "size": nested_path.stat().st_size,
                "sha256": hashlib.sha256(nested_path.read_bytes()).hexdigest(),
            }
        )
    diagnostic_manifest.pop("authenticity", None)
    diagnostic_manifest["authenticity"] = attest_diagnostic_manifest(diagnostic_manifest)
    manifest_path.write_text(json.dumps(diagnostic_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    result = VivadoToolService().call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert health["handoff_ready"] is False
    assert health["authenticity"]["status"] == "READY"
    assert "nested_manifest_design_identity_mismatch" in {
        item["reason"] for item in health["invalid_entries"]
    }


def test_validate_diagnostic_bundle_blocks_path_escape_entries(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    external_path = tmp_path / "outside_bundle.log"
    external_path.write_text("INFO: outside file\n", encoding="utf-8")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["files"][0]["path"] = str(external_path)
    data["files"][0]["size"] = external_path.stat().st_size
    data["files"][0]["sha256"] = hashlib.sha256(external_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert health["handoff_ready"] is False
    assert health["path_escapes"]
    assert str(external_path) in health["path_escapes"][0]["path"]


def test_validate_diagnostic_bundle_blocks_missing_integrity_fields(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["files"][0].pop("sha256")
    data["files"][1]["size"] = "not-an-int"
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert health["handoff_ready"] is False
    assert len(health["invalid_entries"]) == 2
    assert {item["reason"] for item in health["invalid_entries"]} == {"missing_or_invalid_sha256", "missing_or_invalid_size"}


@pytest.mark.parametrize("category", ["audit", "artifact_manifest", "report_manifest", "workflow_trace"])
def test_validate_diagnostic_bundle_rejects_duplicate_singleton_identity(tmp_path: Path, category: str) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    matching = [entry for entry in data["files"] if entry["category"] == category]
    if matching:
        entry = matching[0]
    else:
        source = next(entry for entry in data["files"] if entry["category"] == "audit")
        entry = {**source, "category": category}
        data["files"].append(entry)
    data["files"].append(dict(entry))
    data.pop("authenticity", None)
    data["authenticity"] = attest_diagnostic_manifest(data)
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = VivadoToolService().call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert any(
        issue.get("reason") == "duplicate_singleton_category" and issue.get("category") == category
        for issue in health["invalid_entries"]
    )
    assert category not in result["data"]["resume_context"]["primary_file_refs"]


def test_resume_context_binds_primary_file_content_and_object_identity(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)

    result = VivadoToolService().call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    reference = result["data"]["resume_context"]["primary_file_refs"]["audit"]
    assert "primary_files" not in result["data"]["resume_context"]
    assert len(reference["sha256"]) == 64
    assert reference["size"] > 0
    assert reference["file_id"]
    Path(reference["path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="DIAGNOSTIC_OBJECT_IDENTITY_CHANGED"):
        verify_evidence_reference(reference, root=manifest_path.parent, max_bytes=8 * 1024 * 1024)


def test_validate_diagnostic_bundle_warns_when_audit_status_is_not_ready(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["summary"]["audit_status"] = "WARN"
    data.pop("authenticity", None)
    data["authenticity"] = attest_diagnostic_manifest(data)
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "WARN"
    assert health["handoff_ready"] is False
    assert health["handoff_reviewable"] is True
    assert health["review_guidance"]["classification"] == "reviewable_warn"
    assert "not a portable READY bundle" in health["review_guidance"]["agent_instruction"]
    assert any("CFGBVS" in item for item in health["review_guidance"]["acceptable_warn_categories"])
    assert any("no_input_delay/no_output_delay" in item for item in health["review_guidance"]["acceptable_warn_categories"])
    assert any("missing input/output delays" in item for item in health["review_guidance"]["review_required_evidence"])
    assert result["data"]["resume_context"]["handoff_reviewable"] is True
    assert result["data"]["resume_context"]["review_guidance"]["classification"] == "reviewable_warn"
    assert result["handoff_reviewable"] is True
    assert result["next_actions"][0]["tool"] == "get_agent_workflows"
    assert result["next_actions"][1]["tool"] == "run_project_audit"
    assert result["next_actions"][1]["optional"] is True
    _assert_next_actions_are_valid(result["next_actions"])
    assert "reference bundles are not self-contained" in "\n".join(result["data"]["next_steps"])


def test_validate_diagnostic_bundle_never_promotes_waived_audit_to_handoff_ready(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["summary"]["audit_status"] = "READY"
    data["summary"]["effective_status"] = "READY_WITH_WAIVERS"
    data["summary"]["waived_finding_count"] = 1
    data["summary"]["waiver_summary"] = {"waived_finding_count": 1, "requires_handoff_archive": True}
    data.pop("authenticity", None)
    data["authenticity"] = attest_diagnostic_manifest(data)
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = VivadoToolService().call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "WARN"
    assert health["audit_effective_status"] == "READY_WITH_WAIVERS"
    assert health["waived_finding_count"] == 1
    assert health["handoff_ready"] is False
    assert health["handoff_reviewable"] is True
    assert "waived_findings_require_review" in health["review_required_reasons"]


def test_validate_diagnostic_bundle_warns_when_evidence_freshness_is_missing(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["summary"].pop("evidence_freshness")
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "WARN"
    assert health["handoff_ready"] is False
    assert health["handoff_reviewable"] is False
    assert health["evidence_freshness_missing"] is True
    assert {action["tool"] for action in result["next_actions"]} >= {"run_project_audit", "collect_diagnostic_bundle", "validate_diagnostic_bundle"}
    assert "evidence_freshness.status is FRESH" in "\n".join(result["data"]["next_steps"])


def test_validate_diagnostic_bundle_warns_when_workflow_trace_is_missing(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["files"] = [entry for entry in data["files"] if entry["category"] != "workflow_trace"]
    data["summary"]["categories"] = [entry["category"] for entry in data["files"]]
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "WARN"
    assert health["handoff_ready"] is False
    assert health["handoff_reviewable"] is False
    assert health["workflow_trace_missing"] is True
    assert result["data"]["resume_context"]["workflow_trace_ref"] == {}
    assert {action["tool"] for action in result["next_actions"]} >= {"collect_diagnostic_bundle", "validate_diagnostic_bundle"}


def test_validate_diagnostic_bundle_blocks_missing_hardware_validation_boundary(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["summary"].pop("hardware_validation")
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert health["handoff_ready"] is False
    assert health["hardware_validation_missing"] is True
    assert result["data"]["hardware_validation"] == {}
    assert {action["tool"] for action in result["next_actions"]} >= {"run_project_audit", "collect_diagnostic_bundle"}
    _assert_next_actions_are_valid(result["next_actions"])
    assert "Restore hardware_validation.status=NOT_VALIDATED" in "\n".join(result["data"]["next_steps"])


def test_validate_diagnostic_bundle_blocks_missing_top_level_hardware_boundary(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data.pop("hardware_validation")
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert health["handoff_ready"] is False
    assert health["hardware_validation_missing"] is True
    assert health["manifest_hardware_validation_missing"] is True
    assert health["summary_hardware_validation_missing"] is False


def test_validate_diagnostic_bundle_blocks_non_not_validated_hardware_boundary(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["summary"]["hardware_validation"] = {"status": "VALIDATED", "validated": True}
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert health["handoff_ready"] is False
    assert health["hardware_validation_missing"] is False
    assert health["hardware_validation_status"] == "VALIDATED"
    assert {action["tool"] for action in result["next_actions"]} >= {"run_project_audit", "collect_diagnostic_bundle", "validate_diagnostic_bundle"}
    _assert_next_actions_are_valid(result["next_actions"])
    assert "Restore hardware_validation.status=NOT_VALIDATED" in "\n".join(result["data"]["next_steps"])


def test_validate_diagnostic_bundle_blocks_contradictory_validated_flag(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["summary"]["hardware_validation"] = {"status": "NOT_VALIDATED", "validated": True}
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert health["handoff_ready"] is False
    assert health["hardware_validation_missing"] is False
    assert health["hardware_validation_status"] == "NOT_VALIDATED"
    assert health["hardware_validation_validated"] is True
    assert health["hardware_boundary_invalid"] is True
    assert {action["tool"] for action in result["next_actions"]} >= {"run_project_audit", "collect_diagnostic_bundle", "validate_diagnostic_bundle"}
    _assert_next_actions_are_valid(result["next_actions"])
    assert "validated=false" in "\n".join(result["data"]["next_steps"])


def test_validate_diagnostic_bundle_blocks_missing_validated_flag(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["summary"]["hardware_validation"] = {"status": "NOT_VALIDATED"}
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert health["hardware_validation_status"] == "NOT_VALIDATED"
    assert health["hardware_validation_validated"] is None
    assert health["hardware_boundary_invalid"] is True
    _assert_next_actions_are_valid(result["next_actions"])


def test_validate_diagnostic_bundle_blocks_resource_limit_violations(tmp_path: Path) -> None:
    manifest_path = _write_diagnostic_bundle(tmp_path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["files"][0]["size"] = 101 * 1024 * 1024
    data["files"].extend(dict(data["files"][1]) for _ in range(300))
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    health = result["data"]["health"]
    assert health["status"] == "BLOCK"
    assert health["resource_limits"]
    reasons = {item["reason"] for item in health["resource_limits"]}
    assert {"too_many_files", "file_size_exceeds_limit"} <= reasons
    assert {action["tool"] for action in result["next_actions"]} >= {"collect_diagnostic_bundle", "validate_diagnostic_bundle"}
    _assert_next_actions_are_valid(result["next_actions"])


def test_validate_diagnostic_bundle_missing_manifest_args_returns_next_action() -> None:
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {})

    assert result["ok"] is False
    assert result["error_code"] == "DIAGNOSTIC_MANIFEST_REQUIRED"
    assert result["next_actions"][0]["tool"] == "collect_diagnostic_bundle"
    _assert_next_actions_are_valid(result["next_actions"])


def _tool_schema_properties() -> dict[str, set[str]]:
    return {
        tool.name: set(tool.inputSchema.get("properties", {}))
        for tool in TOOL_DEFS
    }


def _assert_next_actions_are_valid(actions: list[dict]) -> None:
    service_tools = set(VivadoToolService().tool_names())
    schema_properties = _tool_schema_properties()
    required_fields = {"tool", "reason", "required_args", "arg_sources", "preconditions", "stop_condition", "optional"}
    assert actions
    for action in actions:
        assert set(action) == required_fields
        assert action["tool"] in service_tools
        assert set(action["required_args"]) <= schema_properties[action["tool"]]


def _write_diagnostic_bundle(tmp_path: Path) -> Path:
    bundle_dir = tmp_path / "vmcp_diagnostics" / "handoff"
    bundle_dir.mkdir(parents=True)
    required = [
        ("audit", "audit_result.json", "{}"),
        ("environment", "vivado_environment.json", "{}"),
        ("project_state", "project_state.json", "{}"),
        ("filesets", "filesets.json", "{}"),
        ("run_configurations", "run_configurations.json", "{}"),
        ("waivers", "waivers.json", "{}"),
        ("session_status", "session_status.json", "{}"),
        ("replay_script", "replay_project.tcl", "create_project {demo} {.} -part {xc7a35tcpg236-1}\n"),
        ("logs", "logs_tail.txt", "INFO: handoff log\n"),
    ]
    files = []
    for category, name, content in required:
        path = bundle_dir / name
        path.write_text(content, encoding="utf-8")
        files.append(
            {
                "path": str(path),
                "category": category,
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    tracer = WorkflowTracer(trace_id="handoff", trace_dir=bundle_dir)
    now = datetime.now(UTC)
    tracer.record(
        tool="run_project_audit",
        args={"run_name": "impl_1"},
        result={"ok": True, "tool": "run_project_audit", "data": {"status": "READY"}},
        started_at=now,
        ended_at=now,
    )
    trace_path = tracer.trace_path
    files.append(
        {
            "path": str(trace_path),
            "category": "workflow_trace",
            "size": trace_path.stat().st_size,
            "sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
        }
    )
    manifest = {
        "schema_version": 2,
        "bundle_mode": "reference",
        "portable": False,
        "portability": {
            "status": "PROJECT_LOCAL_REFERENCE_ONLY",
            "reason": "Referenced payloads remain in project-local vmcp_* directories.",
        },
        "project_dir": str(tmp_path),
        "bundle_dir": str(bundle_dir),
        "manifest_path": str(bundle_dir / "diagnostic_manifest.json"),
        "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
        "summary": {
            "audit_status": "READY",
            "missing_required_categories": [],
            "complete": True,
            "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
            "validation_scope": "pre_hardware_software",
            "ready_meaning": "READY means no-board Vivado software handoff evidence is ready, not real FPGA board validation.",
            "evidence_freshness": {
                "status": "FRESH",
                "run_name": "impl_1",
                "needs_refresh": False,
                "collected_at": "2026-05-30T00:00:00Z",
                "source": "test_diagnostic_bundle",
            },
        },
        "files": files,
    }
    manifest["integrity_model"] = {
        "status": "SELF_CONSISTENCY_VERIFIED_BY_FILE_HASHES",
        "scope": "bundle_files",
    }
    manifest["authenticity"] = attest_diagnostic_manifest(manifest)
    manifest_path = bundle_dir / "diagnostic_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path
