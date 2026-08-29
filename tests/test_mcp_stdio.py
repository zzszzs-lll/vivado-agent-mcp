import asyncio
import hashlib
import json
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import mcp.types as types
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

from vivado_agent_mcp.server import TOOL_DEFS, call_tool
import vivado_agent_mcp.server as server_module
from vivado_agent_mcp.registry import profile_tool_names
from vivado_agent_mcp.tools import VivadoToolService
from vivado_agent_mcp.vivado.evidence_attestation import attest_diagnostic_manifest
from vivado_agent_mcp.vivado.runtime_identity import ensure_runtime_identity
from vivado_agent_mcp.vivado.workflow_trace import WorkflowTracer


def test_call_tool_handler_returns_text_and_structured_content() -> None:
    result = asyncio.run(call_tool("detect_vivado_environment", {}))

    assert isinstance(result, types.CallToolResult)
    assert result.content
    assert isinstance(result.content[0], types.TextContent)
    assert result.structuredContent is not None
    assert result.structuredContent["tool"] == "detect_vivado_environment"
    assert "ok" in result.structuredContent


def test_call_tool_handler_marks_failures_as_mcp_errors() -> None:
    result = asyncio.run(call_tool("unknown_tool", {}))

    assert isinstance(result, types.CallToolResult)
    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["ok"] is False
    assert result.structuredContent["error_code"] == "UNKNOWN_TOOL"


def test_async_dispatcher_keeps_local_status_responsive_and_serializes_backend(monkeypatch) -> None:
    class BlockingService:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def call(self, name: str, arguments: dict) -> dict:
            if name == "session_status":
                return {"ok": True, "tool": name, "message": "local status", "data": {"connected": True}}
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            self.started.set()
            self.release.wait(timeout=5)
            with self.lock:
                self.active -= 1
            return {"ok": True, "tool": name, "message": "backend complete", "data": {}}

        def cancel_active_operation(self) -> dict:
            self.release.set()
            return {"ok": True}

    async def exercise() -> None:
        fake = BlockingService()
        monkeypatch.setattr(server_module, "service", fake)
        first = asyncio.create_task(server_module.call_tool("backend_one", {}))
        assert await asyncio.to_thread(fake.started.wait, 1)
        second = asyncio.create_task(server_module.call_tool("backend_two", {}))
        status = await asyncio.wait_for(server_module.call_tool("session_status", {}), timeout=0.25)
        assert status.structuredContent["data"]["connected"] is True
        await asyncio.sleep(0.05)
        assert fake.max_active == 1
        fake.release.set()
        await asyncio.gather(first, second)
        assert fake.max_active == 1

    asyncio.run(exercise())


def test_async_dispatcher_cancellation_aborts_owned_active_operation() -> None:
    class CancellableService:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.cancelled = threading.Event()

        def call(self, name: str, arguments: dict) -> dict:
            self.started.set()
            self.release.wait(timeout=5)
            return {"ok": False, "tool": name, "message": "cancelled", "data": {}}

        def cancel_active_operation(self) -> dict:
            self.cancelled.set()
            self.release.set()
            return {"ok": True, "stopped": True}

    async def exercise() -> None:
        fake = CancellableService()
        dispatcher = server_module._SerializedToolDispatcher()
        task = asyncio.create_task(dispatcher.call(fake, "long_vivado_call", {}))
        assert await asyncio.to_thread(fake.started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.cancelled.is_set()
        await dispatcher.shutdown(fake)

    asyncio.run(exercise())


def test_async_dispatcher_queued_cancellation_does_not_abort_active_operation() -> None:
    class QueuedService:
        def __init__(self) -> None:
            self.first_started = threading.Event()
            self.first_release = threading.Event()
            self.lock = threading.Lock()
            self.active_name: str | None = None
            self.calls: list[str] = []
            self.cancelled_names: list[str | None] = []

        def call(self, name: str, arguments: dict) -> dict:
            with self.lock:
                self.active_name = name
                self.calls.append(name)
            if name == "first":
                self.first_started.set()
                self.first_release.wait(timeout=5)
            with self.lock:
                self.active_name = None
            return {"ok": True, "tool": name, "message": "complete", "data": {}}

        def cancel_active_operation(self) -> dict:
            with self.lock:
                self.cancelled_names.append(self.active_name)
            self.first_release.set()
            return {"ok": True, "stopped": True}

    async def exercise() -> None:
        fake = QueuedService()
        dispatcher = server_module._SerializedToolDispatcher()
        first = asyncio.create_task(dispatcher.call(fake, "first", {}))
        assert await asyncio.to_thread(fake.first_started.wait, 1)
        second = asyncio.create_task(dispatcher.call(fake, "second", {}))
        third = asyncio.create_task(dispatcher.call(fake, "third", {}))
        await asyncio.sleep(0.05)

        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        await asyncio.sleep(0.05)
        first_was_still_running = not first.done()
        cancellations_before_shutdown = list(fake.cancelled_names)

        fake.first_release.set()
        await asyncio.gather(first, third)
        calls_before_shutdown = list(fake.calls)
        await dispatcher.shutdown(fake)

        assert first_was_still_running is True
        assert cancellations_before_shutdown == []
        assert calls_before_shutdown == ["first", "third"]

    asyncio.run(exercise())


def test_async_dispatcher_cancelled_request_waiting_for_prior_cancel_never_executes() -> None:
    class BarrierService:
        def __init__(self) -> None:
            self.first_started = threading.Event()
            self.first_release = threading.Event()
            self.cancel_started = threading.Event()
            self.allow_cancel_return = threading.Event()
            self.lock = threading.Lock()
            self.active_name: str | None = None
            self.calls: list[str] = []

        def call(self, name: str, arguments: dict) -> dict:
            with self.lock:
                self.active_name = name
                self.calls.append(name)
            if name == "first":
                self.first_started.set()
                self.first_release.wait(timeout=5)
            with self.lock:
                self.active_name = None
            return {"ok": True, "tool": name, "message": "complete", "data": {}}

        def cancel_active_operation(self) -> dict:
            self.cancel_started.set()
            self.first_release.set()
            self.allow_cancel_return.wait(timeout=5)
            return {"ok": True, "stopped": True}

    async def exercise() -> None:
        fake = BarrierService()
        dispatcher = server_module._SerializedToolDispatcher()
        loop = asyncio.get_running_loop()
        background_errors: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: background_errors.append(context))
        first = asyncio.create_task(dispatcher.call(fake, "first", {}))
        assert await asyncio.to_thread(fake.first_started.wait, 1)
        second = asyncio.create_task(dispatcher.call(fake, "second", {}))

        first.cancel()
        assert await asyncio.to_thread(fake.cancel_started.wait, 1)
        await asyncio.sleep(0.05)
        second.cancel()
        fake.allow_cancel_return.set()

        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(asyncio.CancelledError):
            await second
        third = await dispatcher.call(fake, "third", {})
        calls_before_shutdown = list(fake.calls)
        await dispatcher.shutdown(fake)
        await asyncio.sleep(0)

        assert third["ok"] is True
        assert calls_before_shutdown == ["first", "third"]
        assert background_errors == []

    asyncio.run(exercise())


def test_async_dispatcher_shutdown_cancels_queue_before_active_operation() -> None:
    class ShutdownService:
        def __init__(self) -> None:
            self.first_started = threading.Event()
            self.first_release = threading.Event()
            self.cancel_started = threading.Event()
            self.allow_cancel_return = threading.Event()
            self.lock = threading.Lock()
            self.active_name: str | None = None
            self.calls: list[str] = []
            self.cancelled_names: list[str | None] = []

        def call(self, name: str, arguments: dict) -> dict:
            with self.lock:
                self.active_name = name
                self.calls.append(name)
            if name == "first":
                self.first_started.set()
                self.first_release.wait(timeout=5)
            with self.lock:
                self.active_name = None
            return {"ok": True, "tool": name, "message": "complete", "data": {}}

        def cancel_active_operation(self) -> dict:
            with self.lock:
                self.cancelled_names.append(self.active_name)
            self.cancel_started.set()
            self.allow_cancel_return.wait(timeout=5)
            self.first_release.set()
            return {"ok": True, "stopped": True}

    async def exercise() -> None:
        fake = ShutdownService()
        dispatcher = server_module._SerializedToolDispatcher()
        first = asyncio.create_task(dispatcher.call(fake, "first", {}))
        assert await asyncio.to_thread(fake.first_started.wait, 1)
        queued = asyncio.create_task(dispatcher.call(fake, "queued", {}))
        await asyncio.sleep(0.05)

        shutdown = asyncio.create_task(dispatcher.shutdown(fake))
        assert await asyncio.to_thread(fake.cancel_started.wait, 1)
        for _ in range(100):
            if queued.done():
                break
            await asyncio.sleep(0.01)
        queue_cancelled_before_active_stop_returned = queued.cancelled()

        fake.allow_cancel_return.set()
        await shutdown
        await first
        with pytest.raises(asyncio.CancelledError):
            await queued

        assert queue_cancelled_before_active_stop_returned is True
        assert fake.calls == ["first"]
        assert fake.cancelled_names == ["first"]

    asyncio.run(exercise())


def test_server_tool_definitions_match_service_handlers() -> None:
    server_tool_names = {tool.name for tool in TOOL_DEFS}
    service_tool_names = set(VivadoToolService().tool_names())

    assert server_tool_names == service_tool_names


def test_stdio_client_receives_structured_content(tmp_path: Path) -> None:
    asyncio.run(_run_stdio_client_check(tmp_path))


def test_stdio_default_core_profile_exposes_exact_reduced_surface(tmp_path: Path) -> None:
    asyncio.run(_run_stdio_core_profile_check(tmp_path))


async def _run_stdio_core_profile_check(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(workspace / "src")
    env["VIVADO_AGENT_MCP_RUNTIME_DIR"] = str(tmp_path / "runtime")
    env["VIVADO_AGENT_MCP_TOOL_PROFILE"] = "core"
    env.setdefault("SystemRoot", r"C:\WINDOWS")
    env.setdefault("WINDIR", r"C:\WINDOWS")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "vivado_agent_mcp"],
        env=env,
        cwd=str(workspace),
    )

    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == set(profile_tool_names("core"))
            assert {"run_tcl", "safe_tcl", "configure_run", "program_hw_device"}.isdisjoint(names)
            catalog = await session.call_tool("get_tool_catalog", {})
            assert catalog.structuredContent["data"]["active_tool_profile"] == "core"


async def _run_stdio_client_check(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    runtime_dir = tmp_path / ".vivado_agent_mcp" / "runtime"
    runtime_dir.mkdir(parents=True)
    ensure_runtime_identity(runtime_dir, workspace_root=tmp_path)
    (runtime_dir / "vivado_agent_mcp_stdio.tcl").write_text("socket -server test\n", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(workspace / "src")
    env["VIVADO_AGENT_MCP_RUNTIME_DIR"] = str(runtime_dir)
    env["VIVADO_AGENT_MCP_TOOL_PROFILE"] = "all"
    env.setdefault("SystemRoot", r"C:\WINDOWS")
    env.setdefault("WINDIR", r"C:\WINDOWS")

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "vivado_agent_mcp"],
        env=env,
        cwd=str(workspace),
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            assert any(tool.name == "detect_vivado_environment" for tool in tools.tools)
            assert "detect_simulation_environment" not in tool_names
            assert "run_rtl_simulation" not in tool_names
            assert "get_tool_catalog" in tool_names
            assert "get_agent_workflows" in tool_names
            assert "get_agent_scenarios" in tool_names
            assert "create_project" in tool_names
            assert "configure_simulation" in tool_names
            assert "run_behavioral_simulation" in tool_names
            assert "get_simulation_result" in tool_names
            assert "create_ip" in tool_names
            assert "configure_ip" in tool_names
            assert "generate_ip_targets" in tool_names
            assert "get_ip_status" in tool_names
            assert "upgrade_ip" in tool_names
            assert "export_ip_user_files" in tool_names
            assert "create_block_design" in tool_names
            assert "open_block_design" in tool_names
            assert "add_bd_ip_cell" in tool_names
            assert "create_bd_port" in tool_names
            assert "connect_bd_net" in tool_names
            assert "connect_bd_intf_net" in tool_names
            assert "validate_block_design" in tool_names
            assert "generate_block_design_wrapper" in tool_names
            assert "get_constraints_summary" in tool_names
            assert "check_timing_constraints" in tool_names
            assert "get_clock_summary" in tool_names
            assert "get_timing_paths" in tool_names
            assert "get_methodology_report" in tool_names
            assert "get_qor_summary" in tool_names
            assert "analyze_timing_closure" in tool_names
            assert "create_managed_xdc" in tool_names
            assert "get_project_state" in tool_names
            assert "list_fileset_files" in tool_names
            assert "add_project_files" in tool_names
            assert "remove_project_files" in tool_names
            assert "repair_project_setup" in tool_names
            assert "set_project_top" in tool_names
            assert "set_project_part" in tool_names
            assert "update_project_compile_order" in tool_names
            assert "get_run_configuration" in tool_names
            assert "configure_run" in tool_names
            assert "reset_runs" in tool_names
            assert "clean_run_outputs" in tool_names
            assert "get_runtime_cache_status" in tool_names
            assert "clean_runtime_cache" in tool_names
            assert "collect_build_artifacts" in tool_names
            assert "get_artifact_manifest" in tool_names
            assert "detect_hardware_environment" in tool_names
            assert "open_hardware_manager" in tool_names
            assert "close_hardware_manager" in tool_names
            assert "connect_hw_server" in tool_names
            assert "disconnect_hw_server" in tool_names
            assert "list_hw_targets" in tool_names
            assert "open_hw_target" in tool_names
            assert "close_hw_target" in tool_names
            assert "list_hw_devices" in tool_names
            assert "select_hw_device" in tool_names
            assert "program_hw_device" in tool_names
            assert "program_from_artifact_manifest" in tool_names
            assert "get_hw_device_status" in tool_names
            assert "get_hardware_messages" in tool_names
            assert "check_syntax" in tool_names
            assert "get_compile_order" in tool_names
            assert "analyze_sources" in tool_names
            assert "run_elaboration" in tool_names
            assert "get_elaboration_result" in tool_names
            assert "get_design_hierarchy" in tool_names
            assert "get_cdc_report" in tool_names
            assert "get_clock_interaction_report" in tool_names
            assert "get_power_report" in tool_names
            assert "collect_report_bundle" in tool_names
            assert "run_pre_hw_signoff" in tool_names
            assert "run_project_audit" in tool_names
            assert "list_signoff_waivers" in tool_names
            assert "create_signoff_waiver" in tool_names
            assert "remove_signoff_waiver" in tool_names
            assert "collect_diagnostic_bundle" in tool_names
            assert "validate_diagnostic_bundle" in tool_names
            assert "export_project_replay_script" in tool_names

            result = await session.call_tool("detect_vivado_environment", {})
            assert result.content
            assert isinstance(result.content[0], types.TextContent)
            assert result.structuredContent is not None
            assert result.structuredContent["tool"] == "detect_vivado_environment"
            assert "ok" in result.structuredContent

            catalog = await session.call_tool("get_tool_catalog", {})
            assert catalog.structuredContent is not None
            assert catalog.structuredContent["ok"] is True
            assert catalog.structuredContent["data"]["recommended_entrypoints"]["new_project_to_bitstream"] == "get_agent_workflows"

            workflows = await session.call_tool("get_agent_workflows", {})
            assert workflows.structuredContent is not None
            assert workflows.structuredContent["ok"] is True
            workflow_items = workflows.structuredContent["data"]["workflows"]
            assert any(item["id"] == "new_project_to_bitstream" and item["steps"] for item in workflow_items)

            scenarios = await session.call_tool("get_agent_scenarios", {"scenario_id": "S00"})
            assert scenarios.structuredContent is not None
            assert scenarios.structuredContent["ok"] is True
            assert scenarios.structuredContent["data"]["scenarios"][0]["id"] == "S00"
            assert "get_agent_scenarios" in scenarios.structuredContent["data"]["scenarios"][0]["required_tools"]

            blocked_tcl = await session.call_tool("run_tcl", {"command": "file delete -force -- {demo.runs}"})
            assert blocked_tcl.isError is True
            assert blocked_tcl.structuredContent is not None
            assert blocked_tcl.structuredContent["error_code"] == "TCL_POLICY_BLOCKED"
            assert blocked_tcl.structuredContent["next_actions"][0]["tool"] == "run_tcl"

            dry_tcl = await session.call_tool("run_tcl", {"command": "file delete -force -- {demo.runs}", "dry_run": True})
            assert dry_tcl.isError is True
            assert dry_tcl.structuredContent is not None
            assert dry_tcl.structuredContent["error_code"] == "TCL_POLICY_BLOCKED"
            assert dry_tcl.structuredContent["data"]["dry_run"] is True
            assert dry_tcl.structuredContent["data"]["policy_allowed"] is False

            reset_dry_run = await session.call_tool("reset_runs", {})
            assert reset_dry_run.structuredContent is not None
            assert reset_dry_run.structuredContent["ok"] is True
            assert reset_dry_run.structuredContent["data"]["status"] == "DRY_RUN"
            assert reset_dry_run.structuredContent["next_actions"][0]["tool"] == "reset_runs"

            bitstream = tmp_path / "stdio_gate.bit"
            bitstream.write_text("bitstream", encoding="utf-8")
            hardware_gate = await session.call_tool("program_hw_device", {"bitstream_path": str(bitstream)})
            assert hardware_gate.isError is True
            assert hardware_gate.structuredContent is not None
            assert hardware_gate.structuredContent["error_code"] == "HARDWARE_INTENT_REQUIRED"
            assert hardware_gate.structuredContent["next_actions"][0]["tool"] == "program_hw_device"

            failure_result = await session.call_tool("program_hw_device", {"bitstream_path": str(workspace / "test_use" / "missing.bit")})
            assert failure_result.isError is True
            assert failure_result.content
            assert isinstance(failure_result.content[0], types.TextContent)
            assert failure_result.structuredContent is not None
            assert failure_result.structuredContent["ok"] is False
            assert failure_result.structuredContent["tool"] == "program_hw_device"
            assert failure_result.structuredContent["error_code"] == "BITSTREAM_NOT_FOUND"
            assert failure_result.structuredContent["data"]["hardware_validation"]["status"] == "NOT_VALIDATED"

            action_result = await session.call_tool("validate_diagnostic_bundle", {})
            assert action_result.isError is True
            assert action_result.structuredContent is not None
            assert action_result.structuredContent["tool"] == "validate_diagnostic_bundle"
            assert action_result.structuredContent["next_actions"][0]["tool"] == "collect_diagnostic_bundle"

            ready_manifest = _write_stdio_diagnostic_bundle(tmp_path / "ready_bundle")
            warn_manifest = _write_stdio_diagnostic_bundle(tmp_path / "warn_bundle", audit_status="WARN")
            block_manifest = _write_stdio_diagnostic_bundle(tmp_path / "block_bundle")
            block_data = json.loads(block_manifest.read_text(encoding="utf-8"))
            Path(block_data["files"][0]["path"]).unlink()
            block_manifest.write_text(json.dumps(block_data, ensure_ascii=False, indent=2), encoding="utf-8")

            ready_result = await session.call_tool("validate_diagnostic_bundle", {"manifest_path": str(ready_manifest)})
            warn_result = await session.call_tool("validate_diagnostic_bundle", {"manifest_path": str(warn_manifest)})
            block_result = await session.call_tool("validate_diagnostic_bundle", {"manifest_path": str(block_manifest)})
            assert ready_result.structuredContent is not None
            assert warn_result.structuredContent is not None
            assert block_result.structuredContent is not None
            assert ready_result.structuredContent["data"]["health"]["status"] == "WARN"
            assert ready_result.structuredContent["data"]["resume_context"]["handoff_ready"] is False
            assert ready_result.structuredContent["data"]["resume_context"]["handoff_reviewable"] is True
            assert ready_result.structuredContent["data"]["resume_context"]["bundle_mode"] == "reference"
            assert ready_result.isError is False
            assert ready_result.structuredContent["assessment_status"] == "WARN"
            assert ready_result.structuredContent["stop_required"] is True
            assert ready_result.structuredContent["handoff_ready"] is False
            assert warn_result.structuredContent["data"]["health"]["status"] == "WARN"
            assert warn_result.structuredContent["assessment_status"] == "WARN"
            assert warn_result.structuredContent["stop_required"] is True
            assert warn_result.structuredContent["data"]["resume_context"]["handoff_reviewable"] is True
            assert warn_result.structuredContent["next_actions"][0]["tool"] == "get_agent_workflows"
            assert warn_result.structuredContent["next_actions"][1]["tool"] == "run_project_audit"
            assert warn_result.structuredContent["next_actions"][1]["optional"] is True
            assert block_result.structuredContent["data"]["health"]["status"] == "BLOCK"
            assert block_result.isError is False
            assert block_result.structuredContent["assessment_status"] == "BLOCK"
            assert block_result.structuredContent["stop_required"] is True
            assert block_result.structuredContent["handoff_ready"] is False
            assert block_result.structuredContent["next_actions"][0]["tool"] == "collect_diagnostic_bundle"

            runtime_result = await session.call_tool("get_runtime_cache_status", {"runtime_dir": str(runtime_dir)})
            assert runtime_result.isError is False
            assert runtime_result.structuredContent is not None
            assert runtime_result.structuredContent["tool"] == "get_runtime_cache_status"

            cleanup = await session.call_tool("clean_runtime_cache", {"runtime_dir": str(runtime_dir), "dry_run": True})
            assert cleanup.structuredContent is not None
            assert cleanup.structuredContent["ok"] is True
            assert cleanup.structuredContent["data"]["status"] == "DRY_RUN"
            runtime_identity = cleanup.structuredContent["data"]["runtime_identity"]
            plan_sha256 = cleanup.structuredContent["data"]["plan_sha256"]
            assert runtime_identity["runtime_id"]
            assert len(plan_sha256) == 64

            (runtime_dir / "stdio_plan_drift.tmp").write_text("created after dry-run\n", encoding="utf-8")
            drifted_cleanup = await session.call_tool(
                "clean_runtime_cache",
                {
                    "runtime_dir": str(runtime_dir),
                    "dry_run": False,
                    "runtime_identity": runtime_identity["runtime_id"],
                    "plan_sha256": plan_sha256,
                },
            )
            assert drifted_cleanup.isError is True
            assert drifted_cleanup.structuredContent is not None
            assert drifted_cleanup.structuredContent["error_code"] == "RUNTIME_CLEANUP_BLOCKED"
            assert drifted_cleanup.structuredContent["data"]["reason"] == "cleanup_plan_mismatch"
            assert (runtime_dir / "vivado_agent_mcp_stdio.tcl").exists()


def _write_stdio_diagnostic_bundle(bundle_dir: Path, *, audit_status: str = "READY") -> Path:
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
    tracer = WorkflowTracer(trace_id="workflow_trace", trace_dir=bundle_dir)
    now = datetime.now(UTC)
    tracer.record(
        tool="run_project_audit",
        args={},
        result={"ok": True, "tool": "run_project_audit", "message": "Synthetic audit trace entry."},
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
        "project_dir": str(bundle_dir.parent),
        "bundle_dir": str(bundle_dir),
        "manifest_path": str(bundle_dir / "diagnostic_manifest.json"),
        "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
        "summary": {
            "audit_status": audit_status,
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
                "source": "stdio_regression",
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
