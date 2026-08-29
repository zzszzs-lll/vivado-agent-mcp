from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mcp.types as types
import vivado_agent_mcp
from mcp import ClientSession, StdioServerParameters, stdio_client

from vivado_agent_mcp.vivado.evidence_attestation import attest_diagnostic_manifest
from vivado_agent_mcp.vivado.runtime_identity import ensure_runtime_identity
from vivado_agent_mcp.vivado.workflow_trace import WorkflowTracer


_PACKAGE_GUARD_SERVER = r"""
import sys
from pathlib import Path

import vivado_agent_mcp
from vivado_agent_mcp.__main__ import main

expected = Path(sys.argv[1]).resolve()
actual = Path(vivado_agent_mcp.__file__).resolve()
if actual != expected:
    raise RuntimeError(f"MCP package import mismatch: expected={expected}, actual={actual}")
raise SystemExit(main([]) or 0)
"""

_TIMEOUT_FAKE_SERVER = r"""
import asyncio
import sys
from pathlib import Path

import vivado_agent_mcp
from vivado_agent_mcp import server as srv
from vivado_agent_mcp.tools import VivadoToolService


class TimeoutSession:
    def __init__(self, base_dir: Path, partial_project_path: Path) -> None:
        self.base_dir = base_dir
        self.partial_project_path = partial_project_path
        self.runtime_dir = base_dir / "runtime"
        self.stdout_path = self.runtime_dir / "vivado_stdout.log"
        self.stderr_path = self.runtime_dir / "vivado_stderr.log"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.stdout_path.write_text("INFO: fake Vivado stdout tail\n", encoding="utf-8")
        self.stderr_path.write_text("ERROR: fake Vivado stderr tail\n", encoding="utf-8")

    def run_tcl(self, command: str, timeout_s: int = 60):
        if command.lstrip().startswith("create_project "):
            self.partial_project_path.parent.mkdir(parents=True, exist_ok=True)
            self.partial_project_path.write_text("# fake partial xpr\n", encoding="utf-8")
        raise TimeoutError("fake stdio Tcl timeout")

    def status(self):
        return {
            "connected": False,
            "running": True,
            "runtime_dir": str(self.runtime_dir),
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
        }


expected = Path(sys.argv[1]).resolve()
actual = Path(vivado_agent_mcp.__file__).resolve()
if actual != expected:
    raise RuntimeError(f"timeout MCP package import mismatch: expected={expected}, actual={actual}")
base_dir = Path(sys.argv[2])
partial_project_path = Path(sys.argv[3])
srv.service = VivadoToolService(session=TimeoutSession(base_dir, partial_project_path))
asyncio.run(srv.run_stdio_server())
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an Agent-facing MCP stdio black-box regression from current source.")
    parser.add_argument("--output-dir", default="test_use/agent_stdio_regression", help="Directory for regression artifacts.")
    parser.add_argument("--python", default=sys.executable, help="Python executable used for every nested MCP stdio process.")
    parser.add_argument("--installed-package", action="store_true", help="Do not inject workspace/src; use the package installed for --python.")
    parser.add_argument("--expected-package-import-path", help="Exact vivado_agent_mcp.__file__ expected in installed-package mode.")
    parser.add_argument("--expected-harness-sha256", help="Release-manifest SHA256 for this exact stdio regression script.")
    parser.add_argument("--probe-vivado", action="store_true", help="Also run detect_vivado_environment with probe_launch=true.")
    parser.add_argument("--probe-timeout-s", type=int, default=15)
    args = parser.parse_args()

    workspace = Path(__file__).resolve().parents[1]
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "agent_stdio_regression_result.json"
    package_import_path = Path(vivado_agent_mcp.__file__).resolve()
    harness_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    expected_import_path = (
        Path(args.expected_package_import_path).expanduser().resolve()
        if args.expected_package_import_path
        else package_import_path
    )

    try:
        if args.installed_package and package_import_path != expected_import_path:
            raise RuntimeError(
                f"installed-package regression imported unexpected package: expected={expected_import_path}, actual={package_import_path}"
            )
        if args.installed_package and (
            not args.expected_harness_sha256
            or harness_sha256 != str(args.expected_harness_sha256).lower()
        ):
            raise RuntimeError(
                "installed-package regression harness does not match the release manifest: "
                f"expected={args.expected_harness_sha256 or '<missing>'}, actual={harness_sha256}"
            )
        result = asyncio.run(
            _run_regression(
                workspace,
                output_dir,
                args.probe_vivado,
                args.probe_timeout_s,
                python_exe=Path(args.python).expanduser().resolve(),
                use_workspace_source=not args.installed_package,
                expected_package_import_path=expected_import_path,
                harness_sha256=harness_sha256,
                harness_self_verified=bool(args.installed_package),
            )
        )
    except Exception as exc:  # noqa: BLE001 - script result must be machine-readable for agents.
        result = {"ok": False, "error": exc.__class__.__name__, "message": str(exc)}

    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": result.get("ok", False), "result_path": str(result_path)}, ensure_ascii=False))
    return 0 if result.get("ok") else 1


async def _run_regression(
    workspace: Path,
    output_dir: Path,
    probe_vivado: bool,
    probe_timeout_s: int,
    *,
    python_exe: Path,
    use_workspace_source: bool,
    expected_package_import_path: Path,
    harness_sha256: str,
    harness_self_verified: bool,
) -> dict[str, Any]:
    env = os.environ.copy()
    if use_workspace_source:
        env["PYTHONPATH"] = str(workspace / "src")
    else:
        env.pop("PYTHONPATH", None)
    env["VIVADO_AGENT_MCP_TOOL_PROFILE"] = "all"
    runtime_dir = output_dir / "runtime"
    ensure_runtime_identity(runtime_dir, workspace_root=workspace)
    env["VIVADO_AGENT_MCP_RUNTIME_DIR"] = str(runtime_dir)
    env.setdefault("SystemRoot", r"C:\WINDOWS")
    env.setdefault("WINDIR", r"C:\WINDOWS")
    params = StdioServerParameters(
        command=str(python_exe),
        args=["-c", _PACKAGE_GUARD_SERVER, str(expected_package_import_path)],
        env=env,
        cwd=str(workspace),
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_by_name = {tool.name: tool for tool in tools.tools}
            sim_schema = _tool_properties(tool_by_name["run_behavioral_simulation"])
            diagnostic_schema = _tool_properties(tool_by_name["collect_diagnostic_bundle"])
            repair_schema = _tool_properties(tool_by_name["repair_project_setup"])

            catalog = await session.call_tool("get_tool_catalog", {})
            workflows = await session.call_tool("get_agent_workflows", {})
            scenarios = await session.call_tool("get_agent_scenarios", {})
            manifest_path = _write_stdio_diagnostic_bundle(output_dir / "append_only_diagnostic_bundle")
            workflow_trace_path = manifest_path.parent / "workflow_trace.jsonl"
            _append_stdio_trace_entry(workflow_trace_path, tool="validate_diagnostic_bundle")
            diagnostic_validate = await session.call_tool("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})
            blocked_tcl = await session.call_tool("run_tcl", {"command": "file delete -force -- {demo.runs}"})
            dry_tcl = await session.call_tool("run_tcl", {"command": "file delete -force -- {demo.runs}", "dry_run": True})
            repair_dry_run = await session.call_tool("repair_project_setup", {"dry_run": True})
            literal_safe_tcl = await session.call_tool(
                "safe_tcl",
                {
                    "template": "get_ports -quiet -filter {DIRECTION == IN}",
                    "args": {},
                    "dry_run": True,
                },
            )
            bitstream = output_dir / "hardware_gate.bit"
            bitstream.write_text("fake bitstream\n", encoding="utf-8")
            hardware_gate = await session.call_tool("program_hw_device", {"bitstream_path": str(bitstream)})
            diagnostic_revalidate = await session.call_tool("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})
            detect_args: dict[str, Any] = {}
            if probe_vivado:
                detect_args = {
                    "probe_launch": True,
                    "probe_timeout_s": probe_timeout_s,
                    "runtime_dir": str(output_dir / "runtime"),
                }
            environment = await session.call_tool("detect_vivado_environment", detect_args)
            trace_status = await session.call_tool("get_workflow_trace_status", {})
            timeout_regression = await _run_timeout_stdio_regression(
                workspace,
                output_dir / "timeout_stdio",
                python_exe=python_exe,
                use_workspace_source=use_workspace_source,
                expected_package_import_path=expected_package_import_path,
            )
            environment_ok = _ok(environment)
            expected_latest_failure_tool = "program_hw_device" if environment_ok else "detect_vivado_environment"
            expected_latest_error_code = (
                "HARDWARE_INTENT_REQUIRED"
                if environment_ok
                else str((environment.structuredContent or {}).get("error_code", ""))
            )
            trace_data = (trace_status.structuredContent or {}).get("data", {})

            checks = {
                "run_behavioral_simulation_has_max_vcd_mb": "max_vcd_mb" in sim_schema,
                "collect_diagnostic_bundle_has_reuse_audit_from_manifest": "reuse_audit_from_manifest" in diagnostic_schema,
                "repair_project_setup_schema_visible": "dry_run" in repair_schema and "sim_files" in repair_schema,
                "catalog_ok": _ok(catalog),
                "workflows_ok": _ok(workflows) and bool(workflows.structuredContent["data"]["workflows"]),
                "scenarios_ok": _ok(scenarios)
                and scenarios.structuredContent is not None
                and scenarios.structuredContent["data"]["scenario_count"] >= 8
                and any(item["id"] == "S00" for item in scenarios.structuredContent["data"]["scenarios"])
                and all(item["available"] for item in scenarios.structuredContent["data"]["scenarios"]),
                "scenario_feedback_template_not_validated": _ok(scenarios)
                and scenarios.structuredContent["data"]["feedback_template"]["hardware_validation_status"] == "NOT_VALIDATED",
                "diagnostic_append_only_trace_reviewable": _ok(diagnostic_validate)
                and diagnostic_validate.structuredContent["data"]["health"]["status"] == "WARN"
                and bool(diagnostic_validate.structuredContent["data"]["health"]["workflow_trace_append_only_growth"]),
                "diagnostic_resume_context_structured": _ok(diagnostic_validate)
                and diagnostic_validate.structuredContent["data"]["resume_context"]["handoff_ready"] is False
                and diagnostic_validate.structuredContent["data"]["resume_context"]["handoff_reviewable"] is True
                and bool(diagnostic_validate.structuredContent["data"]["resume_context"]["workflow_trace_append_only_growth"]),
                "diagnostic_handoff_reviewable": _ok(diagnostic_validate)
                and diagnostic_validate.structuredContent["data"]["handoff_reviewable"] is True,
                "tcl_policy_blocked": blocked_tcl.isError is True
                and blocked_tcl.structuredContent is not None
                and blocked_tcl.structuredContent.get("error_code") == "TCL_POLICY_BLOCKED"
                and bool(blocked_tcl.structuredContent.get("next_actions")),
                "tcl_dry_run_structured": dry_tcl.isError is True
                and dry_tcl.structuredContent is not None
                and dry_tcl.structuredContent.get("error_code") == "TCL_POLICY_BLOCKED"
                and dry_tcl.structuredContent["data"]["dry_run"] is True
                and dry_tcl.structuredContent["data"]["policy_allowed"] is False,
                "repair_project_setup_dry_run_structured": _ok(repair_dry_run)
                and repair_dry_run.structuredContent["tool"] == "repair_project_setup"
                and repair_dry_run.structuredContent["data"]["dry_run"] is True,
                "safe_tcl_literal_braces_dry_run": _ok(literal_safe_tcl)
                and literal_safe_tcl.structuredContent["data"]["dry_run"] is True
                and literal_safe_tcl.structuredContent["data"]["policy_allowed"] is True,
                "hardware_gate_blocked": hardware_gate.isError is True
                and hardware_gate.structuredContent is not None
                and hardware_gate.structuredContent.get("error_code") == "HARDWARE_INTENT_REQUIRED",
                "environment_structured": environment.structuredContent is not None
                and environment.structuredContent.get("tool") == "detect_vivado_environment",
                "workflow_trace_status_structured": _ok(trace_status)
                and trace_status.structuredContent is not None
                and bool(trace_status.structuredContent["data"].get("trace_path")),
                "workflow_trace_recorded_tools": trace_status.structuredContent is not None
                and trace_status.structuredContent["data"].get("tool_call_count", 0) >= 5,
                "workflow_trace_unrelated_failure_preserved": _ok(diagnostic_revalidate)
                and trace_status.structuredContent is not None
                and trace_data.get("last_failed_tool") == expected_latest_failure_tool
                and trace_data.get("last_unresolved_failed_tool") == expected_latest_failure_tool
                and trace_data.get("last_unresolved_error_code") == expected_latest_error_code
                and trace_data.get("failure_resolved_by_tool") == "",
                "stdio_create_project_timeout_partial_success": timeout_regression["checks"]["create_project_timeout_partial_success"],
                "stdio_add_project_files_timeout_repair_actions": timeout_regression["checks"]["add_project_files_timeout_repair_actions"],
            }
            if probe_vivado:
                probe = environment.structuredContent["data"].get("launch_probe", {}) if environment.structuredContent else {}
                checks["environment_probe_requested"] = probe.get("requested") is True
                checks["environment_probe_has_diagnosis"] = bool(probe.get("diagnosis", {}).get("primary_cause"))

            return {
                "ok": all(checks.values()),
                "workspace": str(workspace),
                "output_dir": str(output_dir),
                "tool_count": len(tools.tools),
                "checks": checks,
                "diagnostic_validate": diagnostic_validate.structuredContent,
                "agent_scenarios": scenarios.structuredContent,
                "environment": environment.structuredContent,
                "workflow_trace_status": trace_status.structuredContent,
                "timeout_regression": timeout_regression,
                "package_execution": {
                    "mode": "workspace_source" if use_workspace_source else "installed_package",
                    "workspace_source_enabled": use_workspace_source,
                    "python_executable": str(python_exe),
                    "regression_import_path": str(Path(vivado_agent_mcp.__file__).resolve()),
                    "expected_mcp_import_path": str(expected_package_import_path),
                    "mcp_server_import_guard": True,
                    "timeout_server_import_guard": True,
                    "harness_path": str(Path(__file__).resolve()),
                    "harness_sha256": harness_sha256,
                    "harness_self_verified": harness_self_verified,
                },
            }


async def _run_timeout_stdio_regression(
    workspace: Path,
    output_dir: Path,
    *,
    python_exe: Path,
    use_workspace_source: bool,
    expected_package_import_path: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    project_dir = output_dir / "partial_project"
    project_dir.mkdir(parents=True, exist_ok=True)
    project_path = project_dir / "timeout_demo.xpr"
    rtl_file = output_dir / "src" / "timeout_top.sv"
    xdc_file = output_dir / "xdc" / "timeout.xdc"
    sim_file = output_dir / "sim" / "tb_timeout_top.sv"
    for path in (rtl_file, xdc_file, sim_file):
        path.parent.mkdir(parents=True, exist_ok=True)
    rtl_file.write_text("// fake\n", encoding="utf-8")
    xdc_file.write_text("# fake constraint\n", encoding="utf-8")
    sim_file.write_text("// fake\n", encoding="utf-8")

    env = os.environ.copy()
    if use_workspace_source:
        env["PYTHONPATH"] = str(workspace / "src")
    else:
        env.pop("PYTHONPATH", None)
    env["VIVADO_AGENT_MCP_TOOL_PROFILE"] = "all"
    runtime_dir = output_dir / "mcp_runtime"
    ensure_runtime_identity(runtime_dir, workspace_root=workspace)
    env["VIVADO_AGENT_MCP_RUNTIME_DIR"] = str(runtime_dir)
    env.setdefault("SystemRoot", r"C:\WINDOWS")
    env.setdefault("WINDIR", r"C:\WINDOWS")
    params = StdioServerParameters(
        command=str(python_exe),
        args=[
            "-c",
            _TIMEOUT_FAKE_SERVER,
            str(expected_package_import_path),
            str(output_dir / "fake_server"),
            str(project_path),
        ],
        env=env,
        cwd=str(workspace),
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            create_timeout = await session.call_tool(
                "create_project",
                {
                    "project_name": "timeout_demo",
                    "project_dir": str(project_dir),
                    "part": "xc7a35tcpg236-1",
                    "rtl_files": [str(rtl_file)],
                    "xdc_files": [str(xdc_file)],
                    "sim_files": [str(sim_file)],
                    "top": "timeout_top",
                    "testbench_top": "tb_timeout_top",
                    "target_language": "SystemVerilog",
                    "timeout_s": 1,
                },
            )
            add_timeout = await session.call_tool(
                "add_project_files",
                {
                    "fileset": "sources_1",
                    "files": [str(rtl_file)],
                    "timeout_s": 1,
                },
            )

    create_data = create_timeout.structuredContent or {}
    add_data = add_timeout.structuredContent or {}
    create_payload = create_data.get("data", {})
    add_payload = add_data.get("data", {})
    create_action_tools = {action.get("tool") for action in create_data.get("next_actions", [])}
    add_action_tools = {action.get("tool") for action in add_data.get("next_actions", [])}
    checks = {
        "create_project_timeout_partial_success": create_timeout.isError is True
        and create_data.get("error_code") == "TimeoutError"
        and create_payload.get("partial_success") is True
        and create_payload.get("project_path") == str(project_path)
        and create_payload.get("planned_files", {}).get("rtl") == [str(rtl_file)]
        and bool(create_payload.get("planned_language_policy", {}).get("language_policy_note"))
        and create_payload.get("project_capability_bound") is False
        and create_payload.get("recovery_policy") == "inspection_then_rebuild"
        and create_payload.get("session_status", {}).get("runtime_dir")
        and "fake Vivado stdout tail" in str(create_payload.get("stdout_tail", ""))
        and {"session_status", "stop_session", "open_project", "create_project"} <= create_action_tools
        and "repair_project_setup" not in create_action_tools,
        "add_project_files_timeout_repair_actions": add_timeout.isError is True
        and add_data.get("error_code") == "TimeoutError"
        and add_payload.get("fileset") == "sources_1"
        and add_payload.get("files") == [str(rtl_file)]
        and {"list_fileset_files", "repair_project_setup"} <= add_action_tools,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "create_project": create_data,
        "add_project_files": add_data,
    }


def _tool_properties(tool: types.Tool) -> dict[str, Any]:
    schema = tool.inputSchema or {}
    properties = schema.get("properties", {})
    return properties if isinstance(properties, dict) else {}


def _ok(result: Any) -> bool:
    return result.structuredContent is not None and result.structuredContent.get("ok") is True


def _write_stdio_diagnostic_bundle(bundle_dir: Path) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
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
    _append_stdio_trace_entry(bundle_dir / "workflow_trace.jsonl", tool="run_project_audit")
    trace_path = bundle_dir / "workflow_trace.jsonl"
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
                "collected_at": "2026-06-01T00:00:00Z",
                "source": "agent_stdio_regression",
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


def _append_stdio_trace_entry(trace_path: Path, *, tool: str) -> None:
    tracer = WorkflowTracer(trace_id=trace_path.stem, trace_dir=trace_path.parent)
    now = datetime.now(UTC)
    tracer.record(
        tool=tool,
        args={},
        result={"ok": True, "tool": tool, "message": f"Synthetic {tool} trace entry."},
        started_at=now,
        ended_at=now,
    )


if __name__ == "__main__":
    raise SystemExit(main())
