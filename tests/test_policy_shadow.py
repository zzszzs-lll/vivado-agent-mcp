import json
from datetime import UTC, datetime
from pathlib import Path

import vivado_agent_mcp.tools as tools_module
from vivado_agent_mcp.policy_pipeline import PRE_EXECUTION_POLICY_STAGE_ORDER
from vivado_agent_mcp.policy_shadow import (
    POLICY_SHADOW_MAX_BYTES,
    LegacyPreHandlerDecision,
    PolicyShadowFacts,
    evaluate_policy_shadow,
)
from vivado_agent_mcp.registry import TOOL_REGISTRY
from vivado_agent_mcp.tools import VivadoToolService
from vivado_agent_mcp.vivado.project_capability import create_project_capability
from vivado_agent_mcp.vivado.session import GuiTcpVivadoSession
from vivado_agent_mcp.vivado.workflow_trace import WorkflowTracer


def _trace_entry(tracer: WorkflowTracer) -> dict:
    entries = [
        json.loads(line)
        for line in tracer.trace_path.read_text(encoding="utf-8").splitlines()
    ]
    return entries[-1]


def _service(tmp_path: Path, **kwargs) -> tuple[VivadoToolService, WorkflowTracer]:
    tracer = WorkflowTracer(trace_id="policy-shadow", trace_dir=tmp_path / "trace")
    return VivadoToolService(tracer=tracer, **kwargs), tracer


def test_shadow_pipeline_evaluates_every_registered_capability_with_bounded_records() -> None:
    started_at = datetime(2026, 8, 31, tzinfo=UTC)

    for name in TOOL_REGISTRY:
        report = evaluate_policy_shadow(
            capability_name=name,
            arguments={},
            active_profile="all",
            profile_enforced=False,
            trusted_vivado_identity=None,
            project_capability=None,
            facts=PolicyShadowFacts(),
            legacy_decision=LegacyPreHandlerDecision.allow(),
            request_id=f"shadow-{name}",
            started_at=started_at,
        )

        assert report["mode"] == "shadow"
        assert report["authoritative_source"] == "legacy_vivado_tool_service"
        assert report["legacy_authority_retained"] is True
        assert report["pipeline"]["allowed"] in {True, False}
        assert len(json.dumps(report, sort_keys=True).encode("utf-8")) <= POLICY_SHADOW_MAX_BYTES


def test_local_tool_shadow_does_not_start_or_read_a_vivado_session(tmp_path: Path) -> None:
    class NoSessionManager:
        trusted_vivado_identity = {"ok": False, "error_code": "VIVADO_PATH_NOT_CONFIGURED"}

        def __init__(self) -> None:
            self.current_calls = 0

        def current(self):
            self.current_calls += 1
            raise AssertionError("local shadow evaluation must not read a Vivado session")

    manager = NoSessionManager()
    service, tracer = _service(tmp_path, manager=manager)

    result = service.call("get_tool_catalog", {})
    shadow = _trace_entry(tracer)["policy_shadow"]

    assert result["ok"] is True
    assert "policy_shadow" not in result
    assert manager.current_calls == 0
    assert shadow["comparison"]["equivalent"] is True
    assert shadow["legacy"]["allowed"] is True
    assert shadow["pipeline"]["reason_code"] == "POLICY_PIPELINE_ALLOWED"
    assert [item["stage"] for item in shadow["pipeline"]["stage_results"]] == list(
        PRE_EXECUTION_POLICY_STAGE_ORDER
    )


def test_shadow_schema_and_profile_blocks_match_legacy_reason_codes(tmp_path: Path) -> None:
    schema_service, schema_tracer = _service(tmp_path / "schema")
    schema_result = schema_service.call(
        "get_tool_catalog",
        {"unexpected": True},
    )
    schema_shadow = _trace_entry(schema_tracer)["policy_shadow"]

    profile_service, profile_tracer = _service(
        tmp_path / "profile",
        tool_profile="core",
        enforce_tool_profile=True,
    )
    profile_result = profile_service.call("run_tcl", {"command": "get_projects"})
    profile_shadow = _trace_entry(profile_tracer)["policy_shadow"]

    assert schema_result["error_code"] == "INVALID_TOOL_ARGUMENTS"
    assert schema_shadow["legacy"]["reason_code"] == "INVALID_TOOL_ARGUMENTS"
    assert schema_shadow["pipeline"]["reason_code"] == "INVALID_TOOL_ARGUMENTS"
    assert schema_shadow["comparison"]["equivalent"] is True
    assert profile_result["error_code"] == "TOOL_NOT_AVAILABLE_IN_PROFILE"
    assert profile_shadow["legacy"]["reason_code"] == "TOOL_NOT_AVAILABLE_IN_PROFILE"
    assert profile_shadow["pipeline"]["reason_code"] == "TOOL_NOT_AVAILABLE_IN_PROFILE"
    assert profile_shadow["comparison"]["equivalent"] is True


def test_shadow_vivado_path_block_matches_legacy_without_exposing_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    trusted = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("@echo off\necho trusted\n", encoding="utf-8")
    attacker = tmp_path / "attacker.cmd"
    attacker.write_text("@echo off\necho attacker\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(trusted))
    service, tracer = _service(tmp_path)

    result = service.call(
        "start_session",
        {"vivado_path": str(attacker), "runtime_dir": str(tmp_path / "runtime")},
    )
    shadow = _trace_entry(tracer)["policy_shadow"]
    serialized_shadow = json.dumps(shadow, sort_keys=True)

    assert result["error_code"] == "VIVADO_PATH_MISMATCH"
    assert shadow["pipeline"]["reason_code"] == "VIVADO_PATH_MISMATCH"
    assert shadow["comparison"]["equivalent"] is True
    assert str(trusted) not in serialized_shadow
    assert str(attacker) not in serialized_shadow


def test_shadow_existing_project_and_composite_blocks_match_legacy(
    tmp_path: Path,
) -> None:
    class ManagedSession(GuiTcpVivadoSession):
        def __init__(self) -> None:
            super().__init__(generation_id="shadow-generation")

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            raise AssertionError("legacy facade must block before Tcl execution")

    existing_service, existing_tracer = _service(
        tmp_path / "existing",
        session=ManagedSession(),
    )
    existing_service._project_mutation_scope = "existing_project_read_only"

    existing_result = existing_service.call("set_project_top", {"top": "new_top"})
    existing_shadow = _trace_entry(existing_tracer)["policy_shadow"]

    composite_dir = tmp_path / "composite" / "project"
    composite_dir.mkdir(parents=True)
    project_path = composite_dir / "demo.xpr"
    project_path.write_text("# managed project\n", encoding="utf-8")
    composite_session = ManagedSession()
    composite_service, composite_tracer = _service(
        tmp_path / "composite",
        session=composite_session,
    )
    capability = create_project_capability(
        project_path,
        generation_id=composite_session.generation_id,
    )
    composite_service._active_project_capability = capability
    composite_service._project_mutation_scope = "mcp_created_project"

    composite_result = composite_service.call(
        "create_ip",
        {"vlnv": "xilinx.com:ip:blk_mem_gen:8.4", "module_name": "memory"},
    )
    composite_shadow = _trace_entry(composite_tracer)["policy_shadow"]

    assert existing_result["error_code"] == (
        "EXISTING_PROJECT_MUTATION_REQUIRES_WORKING_COPY"
    )
    assert existing_shadow["pipeline"]["reason_code"] == (
        "EXISTING_PROJECT_MUTATION_REQUIRES_WORKING_COPY"
    )
    assert existing_shadow["comparison"]["equivalent"] is True
    assert composite_result["error_code"] == "EXECUTABLE_COMPOSITE_INPUT_UNSUPPORTED"
    assert composite_shadow["pipeline"]["reason_code"] == (
        "EXECUTABLE_COMPOSITE_INPUT_UNSUPPORTED"
    )
    assert composite_shadow["comparison"]["equivalent"] is True


def test_handler_policy_remains_authoritative_after_shadow_allow(tmp_path: Path) -> None:
    service, tracer = _service(tmp_path)

    result = service.call("run_tcl", {"command": "get_projects"})
    shadow = _trace_entry(tracer)["policy_shadow"]

    assert result["error_code"] == "TCL_EXECUTION_DISABLED"
    assert shadow["legacy"]["allowed"] is True
    assert shadow["pipeline"]["allowed"] is True
    assert shadow["comparison"]["equivalent"] is True
    assert shadow["legacy_authority_retained"] is True


def test_shadow_exception_fails_closed_only_in_trace_and_does_not_change_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, tracer = _service(tmp_path)

    def fail_shadow(**_kwargs):
        raise RuntimeError("shadow boom")

    monkeypatch.setattr(tools_module, "evaluate_policy_shadow", fail_shadow)

    result = service.call("get_tool_catalog", {})
    shadow = _trace_entry(tracer)["policy_shadow"]

    assert result["ok"] is True
    assert "policy_shadow" not in result
    assert shadow["authoritative_source"] == "legacy_vivado_tool_service"
    assert shadow["pipeline_evaluated"] is False
    assert shadow["pipeline"]["allowed"] is False
    assert shadow["pipeline"]["reason_code"] == "POLICY_SHADOW_EVALUATION_FAILED"
    assert shadow["comparison"]["false_block"] is True


def test_unknown_tool_records_fail_closed_shadow_without_changing_legacy_result(
    tmp_path: Path,
) -> None:
    service, tracer = _service(tmp_path)

    result = service.call("not_a_tool", {})
    shadow = _trace_entry(tracer)["policy_shadow"]

    assert result["error_code"] == "UNKNOWN_TOOL"
    assert shadow["pipeline_evaluated"] is False
    assert shadow["pipeline"]["allowed"] is False
    assert shadow["pipeline"]["reason_code"] == "UNKNOWN_TOOL"
    assert shadow["comparison"]["equivalent"] is True
