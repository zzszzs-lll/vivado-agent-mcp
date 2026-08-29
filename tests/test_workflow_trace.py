import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import vivado_agent_mcp.vivado.workflow_trace as workflow_trace_module
from vivado_agent_mcp.tools import VivadoToolService
from vivado_agent_mcp.vivado.parsers import TIMING_SUMMARY_REPORT_BEGIN_MARKER, attest_report_text
from vivado_agent_mcp.vivado.workflow_trace import WorkflowTracer, validate_workflow_trace_file


class TraceSession:
    def __init__(self, raw: str = "", ok: bool = True) -> None:
        self.raw = raw
        self.ok = ok
        self.commands: list[str] = []

    def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
        self.commands.append(command)
        return {"ok": self.ok, "raw": self.raw}


def test_workflow_trace_records_success_failure_and_project_mirror(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_dir = tmp_path / "project"
    long_raw = "RAW_SHOULD_NOT_BE_IN_TRACE" * 200
    service = VivadoToolService(session=TraceSession(raw=long_raw))

    created = service.call(
        "create_project",
        {
            "project_name": "demo",
            "project_dir": str(project_dir),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [],
        },
    )
    failed = service.call("not_a_tool", {})

    assert created["ok"] is True
    assert failed["ok"] is False
    status = service.call("get_workflow_trace_status", {})

    assert status["ok"] is True
    data = status["data"]
    assert data["tool_call_count"] >= 2
    assert data["last_successful_tool"] == "create_project"
    assert data["last_failed_tool"] == "not_a_tool"
    assert data["last_unresolved_failed_tool"] == "not_a_tool"
    assert data["last_unresolved_error_code"] == "UNKNOWN_TOOL"
    assert data["failure_resolved_by_tool"] == ""
    assert data["project_dir"] == str(project_dir)
    assert Path(data["trace_path"]).exists()
    assert Path(data["project_trace_path"]).exists()
    assert data["workflow_trace_storage"]["global_trace_path"] == data["trace_path"]
    assert data["workflow_trace_storage"]["project_trace_path"] == data["project_trace_path"]
    assert data["workflow_trace_storage"]["project_trace_is_handoff_copy"] is True
    assert ".vivado_agent_mcp" in data["workflow_trace_storage"]["global_trace_scope"]

    entries = [json.loads(line) for line in Path(data["project_trace_path"]).read_text(encoding="utf-8").splitlines()]
    assert [entry["tool"] for entry in entries[:2]] == ["create_project", "not_a_tool"]
    assert entries[0]["args_summary"]["project_name"] == "demo"
    assert entries[0]["result_summary"]["ok"] is True
    assert entries[1]["result_summary"]["error_code"] == "UNKNOWN_TOOL"
    trace_text = Path(data["project_trace_path"]).read_text(encoding="utf-8")
    assert "RAW_SHOULD_NOT_BE_IN_TRACE" not in trace_text
    assert "create_project {" not in trace_text
    assert status["next_actions"][0]["tool"] == "get_agent_workflows"


def test_workflow_trace_status_clears_unresolved_failure_after_recovery(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    session = TraceSession(raw="ERROR: first failure", ok=False)
    service = VivadoToolService(session=session)

    failed = service.call("get_timing_summary", {})
    session.ok = True
    session.raw = attest_report_text(
        "timing_summary",
        TIMING_SUMMARY_REPORT_BEGIN_MARKER,
        "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.010 0.000 0.020 0.000",
    )
    recovered = service.call("get_timing_summary", {})
    status = service.call("get_workflow_trace_status", {})

    assert failed["ok"] is False
    assert recovered["ok"] is True
    assert status["data"]["last_failed_tool"] == "get_timing_summary"
    assert status["data"]["last_error_code"] == "TCL_FAILED"
    assert status["data"]["last_unresolved_failed_tool"] == ""
    assert status["data"]["last_unresolved_error_code"] == ""
    assert status["data"]["failure_resolved_by_tool"] == "get_timing_summary"
    assert status["data"]["resolved_across_session_boundary"] is False
    assert status["data"]["trace_integrity"]["status"] == "READY"


def test_workflow_trace_timeout_failure_suggests_project_recovery(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    class TimeoutSession(TraceSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            (project_dir / "demo.xpr").write_text("", encoding="utf-8")
            raise TimeoutError("timed out")

        def status(self) -> dict:
            return {"ok": True, "connected": False, "process_running": True}

    service = VivadoToolService(session=TimeoutSession())

    failed = service.call(
        "create_project",
        {
            "project_name": "demo",
            "project_dir": str(project_dir),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [],
            "timeout_s": 1,
        },
    )
    status = service.call("get_workflow_trace_status", {})

    assert failed["ok"] is False
    assert failed["error_code"] == "TimeoutError"
    action_tools = [action["tool"] for action in status["next_actions"]]
    assert "get_agent_workflows" not in action_tools[:1]
    assert {"session_status", "stop_session", "open_project", "repair_project_setup"} <= set(action_tools)


def test_workflow_trace_status_reports_missing_trace_without_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    service = VivadoToolService(session=TraceSession())

    result = service.call("get_workflow_trace_status", {})

    assert result["ok"] is True
    assert result["data"]["tool_call_count"] == 0
    assert result["data"]["trace_exists"] is False
    assert result["data"]["last_successful_tool"] == ""
    assert result["next_actions"][0]["tool"] == "get_agent_workflows"


def test_workflow_trace_corruption_and_sequence_gap_are_blocking(tmp_path: Path) -> None:
    tracer = WorkflowTracer(trace_id="trace", trace_dir=tmp_path / "global")
    now = datetime.now(UTC)
    tracer.record(
        tool="get_tool_catalog",
        args={},
        result={"ok": True, "tool": "get_tool_catalog", "data": {}},
        started_at=now,
        ended_at=now,
    )
    with tracer.trace_path.open("a", encoding="utf-8") as handle:
        handle.write('{"trace_id":"trace","ledger_sequence":3}\n')
        handle.write("not-json\n")

    status = tracer.status()

    assert status["trace_integrity"]["status"] == "BLOCK"
    assert status["handoff_usable"] is False
    assert any("sequence gap" in issue for issue in status["trace_integrity"]["issues"])
    assert any("invalid JSON" in issue for issue in status["trace_integrity"]["issues"])
    assert status["next_actions"][0]["tool"] == "stop_session"


def test_workflow_trace_does_not_copy_prior_project_entries_into_new_project(tmp_path: Path) -> None:
    tracer = WorkflowTracer(trace_id="trace", trace_dir=tmp_path / "global")
    now = datetime.now(UTC)
    project_a = tmp_path / "project_a"
    project_b = tmp_path / "project_b"

    tracer.record(
        tool="get_tool_catalog",
        args={},
        result={"ok": True, "tool": "get_tool_catalog", "data": {}},
        started_at=now,
        ended_at=now,
    )
    tracer.record(
        tool="create_project",
        args={"project_dir": str(project_a)},
        result={"ok": True, "tool": "create_project", "data": {"project_dir": str(project_a)}},
        started_at=now,
        ended_at=now,
    )
    tracer.record(
        tool="open_project",
        args={"project_path": str(project_b / "demo.xpr")},
        result={"ok": True, "tool": "open_project", "data": {"project_dir": str(project_b)}},
        started_at=now,
        ended_at=now,
    )

    trace_a = project_a / "vmcp_diagnostics" / "workflow_trace.jsonl"
    trace_b = project_b / "vmcp_diagnostics" / "workflow_trace.jsonl"
    assert [json.loads(line)["tool"] for line in trace_a.read_text(encoding="utf-8").splitlines()] == ["create_project"]
    assert [json.loads(line)["tool"] for line in trace_b.read_text(encoding="utf-8").splitlines()] == ["open_project"]
    status = tracer.status()
    assert status["status_trace_path"] == str(trace_b)
    assert status["tool_call_count"] == 1


def test_project_workflow_trace_accepts_authenticated_session_boundaries(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    now = datetime.now(UTC)
    first = WorkflowTracer(trace_id="session-a", trace_dir=tmp_path / "global-a")
    first.record(
        tool="create_project",
        args={"project_dir": str(project_dir)},
        result={"ok": True, "tool": "create_project", "data": {"project_dir": str(project_dir)}},
        started_at=now,
        ended_at=now,
    )
    second = WorkflowTracer(trace_id="session-b", trace_dir=tmp_path / "global-b")
    second.record(
        tool="open_project",
        args={"project_path": str(project_dir / "demo.xpr")},
        result={"ok": True, "tool": "open_project", "data": {"project_dir": str(project_dir)}},
        started_at=now,
        ended_at=now,
    )

    trace_path = project_dir / "vmcp_diagnostics" / "workflow_trace.jsonl"
    entries = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    integrity = validate_workflow_trace_file(trace_path)

    assert [entry.get("entry_type", "tool") for entry in entries] == ["tool", "session_boundary", "tool"]
    assert entries[1]["previous_trace_id"] == "session-a"
    assert entries[1]["new_trace_id"] == "session-b"
    assert integrity["status"] == "READY"
    assert integrity["trace_ids"] == ["session-a", "session-b"]
    assert integrity["session_boundary_count"] == 1
    assert integrity["session_boundaries"] == [
        {
            "previous_trace_id": "session-a",
            "new_trace_id": "session-b",
            "ledger_sequence": 2,
        }
    ]
    assert entries[1]["previous_hash"] == entries[0]["entry_hash"]
    assert entries[2]["previous_hash"] == entries[1]["entry_hash"]
    assert second.status()["tool_call_count"] == 2


def test_project_workflow_trace_resolves_same_target_across_sessions(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    now = datetime.now(UTC)
    failed = WorkflowTracer(trace_id="session-a", trace_dir=tmp_path / "global-a")
    failed.record(
        tool="get_timing_summary",
        args={
            "project_dir": str(project_dir),
            "run_name": "impl_1",
            "target": "timing",
            "timeout_s": 60,
        },
        result={
            "ok": False,
            "tool": "get_timing_summary",
            "error_code": "TimeoutError",
            "data": {"project_dir": str(project_dir)},
        },
        started_at=now,
        ended_at=now,
    )

    recovered = WorkflowTracer(trace_id="session-b", trace_dir=tmp_path / "global-b")
    recovered.record(
        tool="get_timing_summary",
        args={
            "project_dir": str(project_dir),
            "run_name": "impl_1",
            "target": "timing",
            "timeout_s": 240,
        },
        result={
            "ok": True,
            "tool": "get_timing_summary",
            "data": {"project_dir": str(project_dir), "status": "READY"},
        },
        started_at=now,
        ended_at=now,
    )

    trace_path = project_dir / "vmcp_diagnostics" / "workflow_trace.jsonl"
    entries = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    failure_entry, boundary_entry, success_entry = entries
    status = recovered.status()

    assert failure_entry["trace_id"] == "session-a"
    assert boundary_entry["entry_type"] == "session_boundary"
    assert success_entry["trace_id"] == "session-b"
    assert failure_entry["operation_identity"] == success_entry["operation_identity"]
    assert success_entry["resolves_failure_id"] == failure_entry["failure_id"]
    assert success_entry["resolved_across_session_boundary"] is True
    assert status["last_unresolved_failed_tool"] == ""
    assert status["failure_resolved_by_tool"] == "get_timing_summary"
    assert status["resolved_across_session_boundary"] is True
    assert status["trace_integrity"]["status"] == "READY"
    assert status["trace_integrity"]["session_boundary_count"] == 1


def test_project_workflow_trace_does_not_resolve_different_targets_across_sessions(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    project_dir = tmp_path / "project"
    target_cases = (
        (
            "get_timing_summary",
            {"project_dir": str(project_dir), "run_name": "impl_1"},
            {"project_dir": str(project_dir), "run_name": "impl_2"},
        ),
        (
            "validate_diagnostic_bundle",
            {"project_dir": str(project_dir), "manifest_path": str(project_dir / "vmcp_diagnostics" / "a" / "diagnostic_manifest.json")},
            {"project_dir": str(project_dir), "manifest_path": str(project_dir / "vmcp_diagnostics" / "b" / "diagnostic_manifest.json")},
        ),
    )

    for index, (tool, failed_args, recovered_args) in enumerate(target_cases):
        failed = WorkflowTracer(trace_id=f"session-a-{index}", trace_dir=tmp_path / f"global-a-{index}")
        failed.record(
            tool=tool,
            args=failed_args,
            result={"ok": False, "tool": tool, "error_code": "TimeoutError", "data": {"project_dir": str(project_dir)}},
            started_at=now,
            ended_at=now,
        )
        recovered = WorkflowTracer(trace_id=f"session-b-{index}", trace_dir=tmp_path / f"global-b-{index}")
        recovered.record(
            tool=tool,
            args=recovered_args,
            result={"ok": True, "tool": tool, "data": {"project_dir": str(project_dir), "status": "READY"}},
            started_at=now,
            ended_at=now,
        )

        status = recovered.status()
        assert status["last_unresolved_failed_tool"] == tool
        assert status["failure_resolved_by_tool"] == ""


def test_project_workflow_trace_does_not_resolve_failure_from_another_project(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    failed = WorkflowTracer(trace_id="session-a", trace_dir=tmp_path / "global-a")
    failed.record(
        tool="run_pre_hw_signoff",
        args={"project_dir": str(project_a), "run_name": "impl_1"},
        result={"ok": False, "tool": "run_pre_hw_signoff", "error_code": "TimeoutError", "data": {"project_dir": str(project_a)}},
        started_at=now,
        ended_at=now,
    )
    recovered = WorkflowTracer(trace_id="session-b", trace_dir=tmp_path / "global-b")
    recovered.record(
        tool="run_pre_hw_signoff",
        args={"project_dir": str(project_b), "run_name": "impl_1"},
        result={"ok": True, "tool": "run_pre_hw_signoff", "data": {"project_dir": str(project_b), "status": "READY"}},
        started_at=now,
        ended_at=now,
    )

    assert failed.status()["last_unresolved_failed_tool"] == "run_pre_hw_signoff"
    assert recovered.status()["last_unresolved_failed_tool"] == ""


def test_project_workflow_trace_blocks_rehashed_invalid_session_transition(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    now = datetime.now(UTC)
    for trace_id, tool in (("session-a", "create_project"), ("session-b", "open_project")):
        tracer = WorkflowTracer(trace_id=trace_id, trace_dir=tmp_path / f"global-{trace_id}")
        tracer.record(
            tool=tool,
            args={"project_dir": str(project_dir)},
            result={"ok": True, "tool": tool, "data": {"project_dir": str(project_dir)}},
            started_at=now,
            ended_at=now,
        )
    trace_path = project_dir / "vmcp_diagnostics" / "workflow_trace.jsonl"
    entries = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    entries[1]["previous_trace_id"] = "unrelated-session"
    entries[1]["entry_hash"] = workflow_trace_module._entry_hash(entries[1])
    entries[2]["previous_hash"] = entries[1]["entry_hash"]
    entries[2]["entry_hash"] = workflow_trace_module._entry_hash(entries[2])
    trace_path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")

    integrity = validate_workflow_trace_file(trace_path)

    assert integrity["status"] == "BLOCK"
    assert any("session boundary" in issue for issue in integrity["issues"])


def test_service_surfaces_project_trace_write_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    tracer = WorkflowTracer(trace_id="trace", trace_dir=tmp_path / "global")
    service = VivadoToolService(session=TraceSession(), tracer=tracer)
    assert service.call(
        "create_project",
        {
            "project_name": "demo",
            "project_dir": str(project_dir),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [],
        },
    )["ok"] is True
    assert tracer.project_trace_path is not None
    with tracer.project_trace_path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")

    result = service.call("get_tool_catalog", {})

    assert result["ok"] is False
    assert result["error_code"] == "WORKFLOW_TRACE_WRITE_FAILED"
    assert result["data"]["operation_result_summary"]["ok"] is True


def test_workflow_trace_validate_does_not_resolve_unrelated_failure(tmp_path: Path) -> None:
    tracer = WorkflowTracer(trace_id="trace", trace_dir=tmp_path / "global")
    now = datetime.now(UTC)
    project_dir = tmp_path / "project"
    tracer.record(
        tool="create_project",
        args={},
        result={"ok": True, "tool": "create_project", "data": {"project_dir": str(project_dir)}},
        started_at=now,
        ended_at=now,
    )
    tracer.record(
        tool="program_hw_device",
        args={},
        result={"ok": False, "tool": "program_hw_device", "error_code": "HARDWARE_INTENT_REQUIRED", "data": {}},
        started_at=now,
        ended_at=now,
    )
    tracer.record(
        tool="validate_diagnostic_bundle",
        args={},
        result={"ok": True, "tool": "validate_diagnostic_bundle", "data": {"status": "READY"}},
        started_at=now,
        ended_at=now,
    )

    status = tracer.status()
    assert status["last_unresolved_failed_tool"] == "program_hw_device"
    assert status["failure_resolved_by_tool"] == ""


def test_workflow_trace_validate_does_not_resolve_prior_handoff_failure(tmp_path: Path) -> None:
    tracer = WorkflowTracer(trace_id="trace", trace_dir=tmp_path / "global")
    now = datetime.now(UTC)
    project_dir = tmp_path / "project"
    tracer.record(
        tool="create_project",
        args={},
        result={"ok": True, "tool": "create_project", "data": {"project_dir": str(project_dir)}},
        started_at=now,
        ended_at=now,
    )
    tracer.record(
        tool="collect_build_artifacts",
        args={},
        result={"ok": False, "tool": "collect_build_artifacts", "error_code": "ARTIFACT_MISSING", "data": {}},
        started_at=now,
        ended_at=now,
    )
    tracer.record(
        tool="validate_diagnostic_bundle",
        args={},
        result={"ok": True, "tool": "validate_diagnostic_bundle", "data": {"status": "READY"}},
        started_at=now,
        ended_at=now,
    )

    status = tracer.status()
    assert status["last_unresolved_failed_tool"] == "collect_build_artifacts"
    assert status["last_unresolved_error_code"] == "ARTIFACT_MISSING"
    assert status["failure_resolved_by_tool"] == ""


def test_workflow_trace_same_tool_success_must_match_manifest_target(tmp_path: Path) -> None:
    tracer = WorkflowTracer(trace_id="trace", trace_dir=tmp_path / "global")
    now = datetime.now(UTC)
    project_dir = tmp_path / "project"
    manifest_a = project_dir / "vmcp_diagnostics" / "a" / "diagnostic_manifest.json"
    manifest_b = project_dir / "vmcp_diagnostics" / "b" / "diagnostic_manifest.json"
    tracer.record(
        tool="create_project",
        args={},
        result={"ok": True, "tool": "create_project", "data": {"project_dir": str(project_dir)}},
        started_at=now,
        ended_at=now,
    )
    tracer.record(
        tool="validate_diagnostic_bundle",
        args={"manifest_path": str(manifest_a)},
        result={"ok": False, "tool": "validate_diagnostic_bundle", "error_code": "BUNDLE_INVALID", "data": {}},
        started_at=now,
        ended_at=now,
    )
    tracer.record(
        tool="validate_diagnostic_bundle",
        args={"manifest_path": str(manifest_b)},
        result={"ok": True, "tool": "validate_diagnostic_bundle", "data": {"status": "READY"}},
        started_at=now,
        ended_at=now,
    )

    unresolved = tracer.status()
    assert unresolved["last_unresolved_failed_tool"] == "validate_diagnostic_bundle"
    assert unresolved["failure_resolved_by_tool"] == ""

    tracer.record(
        tool="validate_diagnostic_bundle",
        args={"manifest_path": str(manifest_a)},
        result={"ok": True, "tool": "validate_diagnostic_bundle", "data": {"status": "READY"}},
        started_at=now,
        ended_at=now,
    )

    resolved = tracer.status()
    assert resolved["last_unresolved_failed_tool"] == ""
    assert resolved["failure_resolved_by_tool"] == "validate_diagnostic_bundle"


def test_workflow_trace_same_tool_success_does_not_resolve_different_semantic_target(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    target_cases = (
        ("program_hw_device", "device", "xc7a-a", "xc7a-b"),
        ("open_hw_target", "target", "localhost:3121/a", "localhost:3121/b"),
        ("get_ip_status", "ip_name", "axi_gpio_0", "axi_gpio_1"),
        ("validate_block_design", "bd_name", "design_a", "design_b"),
    )

    for index, (tool, key, failed_target, successful_target) in enumerate(target_cases):
        tracer = WorkflowTracer(trace_id=f"trace-{index}", trace_dir=tmp_path / f"global-{index}")
        tracer.record(
            tool=tool,
            args={key: failed_target},
            result={"ok": False, "tool": tool, "error_code": "TEST_FAILURE", "data": {}},
            started_at=now,
            ended_at=now,
        )
        tracer.record(
            tool=tool,
            args={key: successful_target},
            result={"ok": True, "tool": tool, "data": {}},
            started_at=now,
            ended_at=now,
        )

        status = tracer.status()
        assert status["last_unresolved_failed_tool"] == tool
        assert status["failure_resolved_by_tool"] == ""


def test_workflow_trace_serializes_concurrent_record_and_status_calls(tmp_path: Path) -> None:
    tracer = WorkflowTracer(trace_id="concurrent", trace_dir=tmp_path / "global")
    now = datetime.now(UTC)

    def record(index: int) -> None:
        tracer.record(
            tool="session_status",
            args={"index": index},
            result={"ok": True, "tool": "session_status", "data": {"status": "READY"}},
            started_at=now,
            ended_at=now,
        )
        tracer.status()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record, range(40)))

    status = tracer.status()
    assert status["tool_call_count"] == 40
    assert status["trace_integrity"]["status"] == "READY"
