from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client

from . import __version__
from .registry import TOOL_PROFILE_ENV, profile_tool_names, resolve_tool_profile
from .release_identity import source_identity
from .vivado.evidence_attestation import attest_diagnostic_manifest
from .vivado.workflow_trace import WorkflowTracer

DEFAULT_OUTPUT_DIR = ".vivado_agent_mcp/selftest"
DEFAULT_PROBE_TIMEOUT_S = 60


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vivado-agent-mcp selftest",
        description="Run an Agent-facing stdio selftest for vivado-agent-mcp.",
    )
    parser.add_argument("--workspace", default=".", help="Workspace root used to run the MCP server.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for the selftest report and temporary artifacts.")
    parser.add_argument("--include-vivado-probe", action="store_true", help="Also run detect_vivado_environment(probe_launch=true).")
    parser.add_argument("--probe-timeout-s", type=int, default=DEFAULT_PROBE_TIMEOUT_S, help="Timeout for the optional Vivado batch launch probe.")
    parser.add_argument("--json", action="store_true", help="Print the full machine-readable selftest report as JSON.")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = workspace / output_dir
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    include_vivado_probe = bool(args.include_vivado_probe)
    probe_timeout_s = max(1, int(args.probe_timeout_s))

    try:
        report = asyncio.run(
            run_selftest(
                workspace=workspace,
                output_dir=output_dir,
                include_vivado_probe=include_vivado_probe,
                probe_timeout_s=probe_timeout_s,
            )
        )
    except Exception as exc:  # noqa: BLE001 - selftest must always produce a report.
        report = _exception_report(
            workspace=workspace,
            output_dir=output_dir,
            include_vivado_probe=include_vivado_probe,
            probe_timeout_s=probe_timeout_s,
            exc=exc,
        )

    result_path = output_dir / "selftest_report.json"
    report["result_path"] = str(result_path)
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human_report(report)
    return 0 if report["status"] in {"PASS", "WARN"} else 2


async def run_selftest(
    *,
    workspace: Path,
    output_dir: Path,
    include_vivado_probe: bool = False,
    probe_timeout_s: int = DEFAULT_PROBE_TIMEOUT_S,
) -> dict[str, Any]:
    env = os.environ.copy()
    src_path = workspace / "src"
    if src_path.exists():
        current_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(src_path) if not current_pythonpath else f"{src_path}{os.pathsep}{current_pythonpath}"
    env.setdefault("SystemRoot", r"C:\WINDOWS")
    env.setdefault("WINDIR", r"C:\WINDOWS")
    active_profile = resolve_tool_profile(env.get(TOOL_PROFILE_ENV))
    expected_tool_names = set(profile_tool_names(active_profile))

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "vivado_agent_mcp"],
        env=env,
        cwd=str(workspace),
    )
    checks: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {}

    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_by_name = {tool.name: tool for tool in tools.tools}
            actual_tool_names = set(tool_by_name)
            missing_profile_tools = sorted(expected_tool_names - actual_tool_names)
            unexpected_profile_tools = sorted(actual_tool_names - expected_tool_names)
            _add_check(
                checks,
                "list_tools",
                "PASS" if not missing_profile_tools and not unexpected_profile_tools else "BLOCK",
                f"Server exposed the expected {active_profile} profile with {len(tool_by_name)} tools.",
                {
                    "tool_profile": active_profile,
                    "tool_count": len(tool_by_name),
                    "expected_tool_count": len(expected_tool_names),
                    "missing": missing_profile_tools,
                    "unexpected": unexpected_profile_tools,
                },
            )
            _check_required_tools(checks, tool_by_name, active_profile=active_profile)
            _check_schema_fields(checks, tool_by_name)

            catalog = await session.call_tool("get_tool_catalog", {})
            workflows = await session.call_tool("get_agent_workflows", {})
            scenarios = await session.call_tool("get_agent_scenarios", {})
            runtime_status = await session.call_tool("get_runtime_cache_status", {})
            _check_result_ok(checks, "get_tool_catalog", catalog)
            _check_result_ok(checks, "get_agent_workflows", workflows)
            _check_result_ok(checks, "get_agent_scenarios", scenarios)
            _check_result_ok(checks, "get_runtime_cache_status", runtime_status)
            _check_agent_payloads(checks, workflows, scenarios)

            manifest_path = _write_synthetic_diagnostic_bundle(output_dir / "synthetic_diagnostic_bundle")
            artifacts["synthetic_diagnostic_manifest"] = str(manifest_path)
            diagnostic_validate = await session.call_tool("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})
            _check_diagnostic_validate(checks, diagnostic_validate)

            if {"run_tcl", "safe_tcl"} <= actual_tool_names:
                blocked_tcl = await session.call_tool("run_tcl", {"command": "file delete -force -- {demo.runs}"})
                dry_tcl = await session.call_tool("run_tcl", {"command": "file delete -force -- {demo.runs}", "dry_run": True})
                safe_tcl = await session.call_tool(
                    "safe_tcl",
                    {
                        "template": "get_ports -quiet *",
                        "args": {},
                        "dry_run": True,
                    },
                )
                _check_tcl_gates(checks, blocked_tcl, dry_tcl, safe_tcl)
            else:
                _check_profile_hidden_gate(
                    checks,
                    check_id="tcl_safety_gates",
                    tool_names={"run_tcl", "safe_tcl"},
                    actual_tool_names=actual_tool_names,
                    active_profile=active_profile,
                )

            if "program_hw_device" in actual_tool_names:
                bitstream = output_dir / "hardware_gate.bit"
                bitstream.write_text("fake bitstream\n", encoding="utf-8")
                artifacts["hardware_gate_bitstream"] = str(bitstream)
                hardware_gate = await session.call_tool("program_hw_device", {"bitstream_path": str(bitstream)})
                _check_hardware_gate(checks, hardware_gate)
            else:
                _check_profile_hidden_gate(
                    checks,
                    check_id="hardware_programming_gate",
                    tool_names={"program_hw_device", "program_from_artifact_manifest"},
                    actual_tool_names=actual_tool_names,
                    active_profile=active_profile,
                )
            if "list_hw_targets" in actual_tool_names:
                hardware_manager_gate = await session.call_tool("list_hw_targets", {})
                _check_hardware_manager_gate(checks, hardware_manager_gate)
            else:
                _check_profile_hidden_gate(
                    checks,
                    check_id="hardware_manager_gate",
                    tool_names={"list_hw_targets"},
                    actual_tool_names=actual_tool_names,
                    active_profile=active_profile,
                )

            detect_args: dict[str, Any] = {}
            if include_vivado_probe:
                detect_args = {
                    "probe_launch": True,
                    "probe_timeout_s": probe_timeout_s,
                    "runtime_dir": str(output_dir / "runtime"),
                }
            environment = await session.call_tool("detect_vivado_environment", detect_args)
            _check_environment(checks, environment, include_vivado_probe=include_vivado_probe)

            trace_status = await session.call_tool("get_workflow_trace_status", {})
            _check_result_ok(checks, "get_workflow_trace_status", trace_status)

    status = _overall_status(checks)
    validation_scope = _validation_scope(include_vivado_probe)
    return {
        "ok": status != "BLOCK",
        "status": status,
        "summary": _summary_for_status(status, include_vivado_probe=include_vivado_probe),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package": {"name": "vivado-agent-mcp", "version": __version__},
        "source_identity": source_identity(workspace),
        "workspace": str(workspace),
        "output_dir": str(output_dir),
        "execution_mode": "mcp_stdio_selftest",
        "tool_profile": active_profile,
        "validation_scope": validation_scope,
        "include_vivado_probe": include_vivado_probe,
        "probe_timeout_s": probe_timeout_s,
        "checks": checks,
        "artifacts": artifacts,
        "next_steps": _next_steps(checks),
        "hardware_validation": {
            "status": "NOT_VALIDATED",
            "validated": False,
            "message": "Selftest does not validate real FPGA hardware, JTAG, programming, ILA, or VIO.",
        },
    }


def _exception_report(
    *,
    workspace: Path,
    output_dir: Path,
    include_vivado_probe: bool,
    probe_timeout_s: int,
    exc: Exception,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "BLOCK",
        "summary": "Selftest could not complete; MCP stdio startup or client calls failed.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package": {"name": "vivado-agent-mcp", "version": __version__},
        "source_identity": source_identity(workspace),
        "workspace": str(workspace),
        "output_dir": str(output_dir),
        "execution_mode": "mcp_stdio_selftest",
        "validation_scope": _validation_scope(include_vivado_probe),
        "include_vivado_probe": include_vivado_probe,
        "probe_timeout_s": probe_timeout_s,
        "checks": [
            {
                "id": "selftest_exception",
                "status": "BLOCK",
                "message": f"{exc.__class__.__name__}: {exc}",
                "data": {"exception_type": exc.__class__.__name__, "error": str(exc)},
            }
        ],
        "artifacts": {},
        "next_steps": [
            "Run vivado-agent-mcp doctor first to check Python, runtime, and Vivado path.",
            "Confirm the MCP client command starts python -m vivado_agent_mcp without extra subcommands.",
        ],
        "hardware_validation": {
            "status": "NOT_VALIDATED",
            "validated": False,
            "message": "Selftest failed before any hardware validation; real FPGA hardware is still not validated.",
        },
    }


def _check_required_tools(
    checks: list[dict[str, Any]],
    tool_by_name: dict[str, Any],
    *,
    active_profile: str,
) -> None:
    required = {
        "get_tool_catalog",
        "get_agent_workflows",
        "get_agent_scenarios",
        "validate_diagnostic_bundle",
        "detect_vivado_environment",
        "get_runtime_cache_status",
        "get_workflow_trace_status",
        "repair_project_setup",
        "run_behavioral_simulation",
    }
    profile_names = set(profile_tool_names(active_profile))
    required.update({"run_tcl", "safe_tcl", "program_hw_device"} & profile_names)
    missing = sorted(required - set(tool_by_name))
    _add_check(
        checks,
        "required_tools",
        "PASS" if not missing else "BLOCK",
        "Required Agent-facing tools are present." if not missing else f"Missing required tools: {', '.join(missing)}",
        {"tool_profile": active_profile, "missing": missing, "required_count": len(required)},
    )


def _check_schema_fields(checks: list[dict[str, Any]], tool_by_name: dict[str, Any]) -> None:
    expectations = {
        "run_behavioral_simulation": {"max_vcd_mb"},
        "collect_diagnostic_bundle": {"reuse_audit_from_manifest"},
        "repair_project_setup": {"dry_run", "sim_files", "testbench_top"},
        "run_tcl": {"dry_run", "execution_intent", "allow_destructive"},
        "program_hw_device": {"hardware_intent", "confirm", "board_fingerprint", "expected_bitstream_sha256", "hardware_mode"},
    }
    missing: dict[str, list[str]] = {}
    not_exposed: list[str] = []
    for tool_name, fields in expectations.items():
        if tool_name not in tool_by_name:
            not_exposed.append(tool_name)
            continue
        properties = _tool_properties(tool_by_name.get(tool_name))
        absent = sorted(fields - set(properties))
        if absent:
            missing[tool_name] = absent
    _add_check(
        checks,
        "input_schema_contract",
        "PASS" if not missing else "BLOCK",
        "Key input schema fields are present." if not missing else "One or more key schema fields are missing.",
        {"missing": missing, "not_exposed": sorted(not_exposed)},
    )
    required_output_fields = {
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
    }
    missing_output: dict[str, list[str]] = {}
    for tool_name, tool in tool_by_name.items():
        schema = getattr(tool, "outputSchema", None) or {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        absent = sorted(required_output_fields - set(required if isinstance(required, list) else []))
        if absent:
            missing_output[tool_name] = absent
    _add_check(
        checks,
        "output_schema_contract",
        "PASS" if not missing_output else "BLOCK",
        "All tools require the unified Agent routing fields in outputSchema."
        if not missing_output
        else "One or more tool output schemas omit required Agent routing fields.",
        {"missing": missing_output},
    )


def _check_profile_hidden_gate(
    checks: list[dict[str, Any]],
    *,
    check_id: str,
    tool_names: set[str],
    actual_tool_names: set[str],
    active_profile: str,
) -> None:
    unexpectedly_exposed = sorted(tool_names & actual_tool_names)
    ok = not unexpectedly_exposed
    _add_check(
        checks,
        check_id,
        "PASS" if ok else "BLOCK",
        f"The {active_profile} profile does not expose {', '.join(sorted(tool_names))}."
        if ok
        else f"The {active_profile} profile unexpectedly exposes {', '.join(unexpectedly_exposed)}.",
        {
            "tool_profile": active_profile,
            "profile_isolation": True,
            "hidden_tools": sorted(tool_names - actual_tool_names),
            "unexpectedly_exposed": unexpectedly_exposed,
        },
    )


def _check_agent_payloads(checks: list[dict[str, Any]], workflows: Any, scenarios: Any) -> None:
    workflows_data = _structured(workflows).get("data", {})
    scenarios_data = _structured(scenarios).get("data", {})
    workflow_items = workflows_data.get("workflows") or []
    scenario_items = scenarios_data.get("scenarios") or []
    feedback_template = scenarios_data.get("feedback_template", {})
    validation_policy = scenarios_data.get("validation_policy", {})
    scenario_ids = {str(item.get("id", "")) for item in scenario_items if isinstance(item, dict)}
    required_no_live = set(validation_policy.get("required_no_live_scenarios") or []) if isinstance(validation_policy, dict) else set()
    required_live = set(validation_policy.get("required_live_scenarios") or []) if isinstance(validation_policy, dict) else set()
    missing_gate_scenarios = sorted((required_no_live | required_live) - scenario_ids)
    missing_runner_coverage = sorted(
        str(item.get("id", ""))
        for item in scenario_items
        if isinstance(item, dict)
        and not (
            isinstance(item.get("runner_coverage"), dict)
            and item["runner_coverage"].get("execution_mode")
            and item["runner_coverage"].get("evidence_class")
            and isinstance(item["runner_coverage"].get("full_scenario_coverage"), bool)
        )
    )
    checks_ok = (
        bool(workflow_items)
        and len(scenario_items) >= 8
        and feedback_template.get("hardware_validation_status") == "NOT_VALIDATED"
        and bool(required_no_live)
        and bool(required_live)
        and not missing_gate_scenarios
        and not missing_runner_coverage
    )
    _add_check(
        checks,
        "agent_workflow_payloads",
        "PASS" if checks_ok else "BLOCK",
        "Workflow and scenario payloads are structured for Agent routing." if checks_ok else "Workflow or scenario payloads are incomplete.",
        {
            "workflow_count": len(workflow_items),
            "scenario_count": len(scenario_items),
            "feedback_hardware_status": feedback_template.get("hardware_validation_status"),
            "validation_policy_present": bool(validation_policy),
            "required_no_live_runner_scenarios": sorted(required_no_live),
            "required_live_runner_scenarios": sorted(required_live),
            "missing_gate_scenarios": missing_gate_scenarios,
            "missing_runner_coverage": missing_runner_coverage,
        },
    )


def _check_result_ok(checks: list[dict[str, Any]], check_id: str, result: Any) -> None:
    structured = _structured(result)
    ok = structured.get("ok") is True and structured.get("tool")
    _add_check(
        checks,
        check_id,
        "PASS" if ok else "BLOCK",
        f"{check_id} returned structuredContent." if ok else f"{check_id} did not return ok structuredContent.",
        {"tool": structured.get("tool"), "error_code": structured.get("error_code")},
    )


def _check_diagnostic_validate(checks: list[dict[str, Any]], result: Any) -> None:
    structured = _structured(result)
    data = structured.get("data", {})
    health = data.get("health", {})
    resume_context = data.get("resume_context", {})
    hardware = structured.get("hardware_validation") or data.get("hardware_validation") or {}
    ok = (
        structured.get("ok") is True
        and health.get("status") == "WARN"
        and health.get("bundle_mode") == "reference"
        and health.get("portable") is False
        and resume_context.get("handoff_ready") is False
        and resume_context.get("handoff_reviewable") is True
        and hardware.get("status") == "NOT_VALIDATED"
        and hardware.get("validated") is False
    )
    _add_check(
        checks,
        "diagnostic_bundle_contract",
        "PASS" if ok else "BLOCK",
        "Synthetic diagnostic reference bundle validates with explicit review-only no-board semantics."
        if ok
        else "Synthetic diagnostic bundle did not validate as expected.",
        {
            "health_status": health.get("status"),
            "handoff_ready": resume_context.get("handoff_ready"),
            "handoff_reviewable": resume_context.get("handoff_reviewable"),
            "bundle_mode": health.get("bundle_mode"),
            "portable": health.get("portable"),
            "hardware_status": hardware.get("status"),
        },
    )


def _check_tcl_gates(checks: list[dict[str, Any]], blocked_tcl: Any, dry_tcl: Any, safe_tcl: Any) -> None:
    blocked = _structured(blocked_tcl)
    dry = _structured(dry_tcl)
    safe = _structured(safe_tcl)
    ok = (
        getattr(blocked_tcl, "isError", False) is True
        and blocked.get("error_code") == "TCL_POLICY_BLOCKED"
        and bool(blocked.get("next_actions"))
        and getattr(dry_tcl, "isError", False) is True
        and dry.get("error_code") == "TCL_POLICY_BLOCKED"
        and dry.get("data", {}).get("dry_run") is True
        and dry.get("data", {}).get("policy_allowed") is False
        and safe.get("ok") is True
        and safe.get("data", {}).get("dry_run") is True
        and safe.get("policy_allowed") is True
        and safe.get("data", {}).get("policy", {}).get("risk") == "LOW"
    )
    _add_check(
        checks,
        "tcl_safety_gates",
        "PASS" if ok else "BLOCK",
        "Tcl safety gates reject destructive scripts even in dry-run and allow low-risk query templates."
        if ok
        else "Tcl safety gate behavior is not as expected.",
        {
            "blocked_error_code": blocked.get("error_code"),
            "dry_run_ok": dry.get("ok"),
            "safe_tcl_ok": safe.get("ok"),
            "safe_tcl_policy_allowed": safe.get("policy_allowed"),
            "safe_tcl_policy_risk": safe.get("data", {}).get("policy", {}).get("risk"),
        },
    )


def _check_hardware_gate(checks: list[dict[str, Any]], result: Any) -> None:
    structured = _structured(result)
    hardware = structured.get("hardware_validation") or {}
    ok = (
        getattr(result, "isError", False) is True
        and structured.get("error_code") == "HARDWARE_INTENT_REQUIRED"
        and hardware.get("status") == "NOT_VALIDATED"
    )
    _add_check(
        checks,
        "hardware_programming_gate",
        "PASS" if ok else "BLOCK",
        "Hardware programming is blocked without explicit intent and keeps NOT_VALIDATED boundary."
        if ok
        else "Hardware programming gate did not block as expected.",
        {"error_code": structured.get("error_code"), "hardware_status": hardware.get("status")},
    )


def _check_hardware_manager_gate(checks: list[dict[str, Any]], result: Any) -> None:
    structured = _structured(result)
    hardware = structured.get("hardware_validation") or {}
    ok = (
        getattr(result, "isError", False) is True
        and structured.get("error_code") == "HARDWARE_MODE_DISABLED"
        and hardware.get("status") == "NOT_VALIDATED"
    )
    _add_check(
        checks,
        "hardware_manager_gate",
        "PASS" if ok else "BLOCK",
        "Hardware Manager tools are blocked by default in no-board beta."
        if ok
        else "Hardware Manager tool did not block under the no-board beta profile.",
        {"error_code": structured.get("error_code"), "hardware_status": hardware.get("status")},
    )


def _check_environment(checks: list[dict[str, Any]], result: Any, *, include_vivado_probe: bool) -> None:
    structured = _structured(result)
    data = structured.get("data", {})
    launch_probe = data.get("launch_probe", {})
    if structured.get("ok") is not True:
        _add_check(
            checks,
            "vivado_environment_query",
            "WARN",
            "Vivado environment query returned a structured non-ok result; run doctor before live workflows.",
            {"error_code": structured.get("error_code"), "vivado_ok": data.get("ok")},
        )
        return
    if include_vivado_probe and launch_probe.get("ok") is not True:
        _add_check(
            checks,
            "vivado_environment_query",
            "BLOCK",
            "Vivado launch probe was requested but did not pass.",
            {"vivado_ok": data.get("ok"), "launch_probe": launch_probe},
        )
        return
    status = "PASS" if data.get("ok") else "WARN"
    message = (
        "Vivado environment query returned structured data."
        if status == "PASS"
        else "Vivado was not found by selftest; MCP contract is testable, but live workflows need doctor/environment repair."
    )
    _add_check(
        checks,
        "vivado_environment_query",
        status,
        message,
        {"vivado_ok": data.get("ok"), "vivado_path": data.get("path"), "probe_requested": include_vivado_probe},
    )


def _tool_properties(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None) or {}
    properties = schema.get("properties", {})
    return properties if isinstance(properties, dict) else {}


def _structured(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    return structured if isinstance(structured, dict) else {}


def _write_synthetic_diagnostic_bundle(bundle_dir: Path) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    required = [
        ("audit", "audit_result.json", "{}"),
        ("environment", "vivado_environment.json", "{}"),
        ("project_state", "project_state.json", "{}"),
        ("filesets", "filesets.json", "{}"),
        ("run_configurations", "run_configurations.json", "{}"),
        ("waivers", "waivers.json", "{}"),
        ("session_status", "session_status.json", "{}"),
        ("replay_script", "replay_project.tcl", "create_project {demo} {.} -part {xc7a35tcpg236-1}\n"),
        ("logs", "logs_tail.txt", "INFO: selftest handoff log\n"),
    ]
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
    now = datetime.now(timezone.utc)
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
            "reason": "Selftest validates reference-bundle routing, not portable nested evidence closure.",
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
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "source": "vivado_agent_mcp_selftest",
            },
        },
        "files": files,
    }
    manifest["integrity_model"] = {"status": "SELF_CONSISTENCY_VERIFIED_BY_FILE_HASHES", "scope": "bundle_files"}
    manifest["authenticity"] = attest_diagnostic_manifest(manifest)
    manifest_path = bundle_dir / "diagnostic_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def _add_check(checks: list[dict[str, Any]], check_id: str, status: str, message: str, data: dict[str, Any] | None = None) -> None:
    checks.append({"id": check_id, "status": status, "message": message, "data": data or {}})


def _overall_status(checks: list[dict[str, Any]]) -> str:
    statuses = {check["status"] for check in checks}
    if "BLOCK" in statuses:
        return "BLOCK"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"


def _validation_scope(include_vivado_probe: bool) -> str:
    return "mcp_stdio_contract_plus_vivado_probe" if include_vivado_probe else "mcp_stdio_contract"


def _summary_for_status(status: str, *, include_vivado_probe: bool = False) -> str:
    if status == "PASS":
        if include_vivado_probe:
            return "MCP stdio selftest passed with the optional Vivado launch probe; Agent-facing contracts and bounded Vivado discovery are usable."
        return "MCP stdio selftest passed; this proves Agent-facing contracts and no-board hardware boundaries, but not live Vivado project execution."
    if status == "WARN":
        return "MCP stdio contract is usable, but at least one environment or readiness item should be reviewed before live Vivado workflows."
    return "MCP stdio selftest is blocked; fix BLOCK items before giving this MCP to an Agent."


def _next_steps(checks: list[dict[str, Any]]) -> list[str]:
    steps: list[str] = []
    by_id = {check["id"]: check for check in checks}
    block_ids = [check["id"] for check in checks if check["status"] == "BLOCK"]
    warn_ids = [check["id"] for check in checks if check["status"] == "WARN"]
    if block_ids:
        steps.append(f"Fix BLOCK checks first: {', '.join(block_ids)}.")
    if by_id.get("vivado_environment_query", {}).get("status") in {"WARN", "BLOCK"}:
        steps.append("Run vivado-agent-mcp doctor and repair VIVADO_PATH/runtime before live Vivado workflows.")
    if by_id.get("agent_workflow_payloads", {}).get("status") == "BLOCK":
        steps.append("Inspect get_agent_workflows/get_agent_scenarios structuredContent; Agent routing contract is incomplete.")
    if by_id.get("tcl_safety_gates", {}).get("status") == "BLOCK":
        steps.append("Do not allow Agent use until Tcl safety gates are fixed.")
    if by_id.get("hardware_programming_gate", {}).get("status") == "BLOCK":
        steps.append("Do not expose hardware programming tools until hardware intent gates keep NOT_VALIDATED boundary.")
    if warn_ids and not block_ids:
        steps.append(f"Review WARN checks before live Project Mode use: {', '.join(warn_ids)}.")
    if not steps:
        steps.append("Configure the MCP client, then ask the Agent to call get_tool_catalog and get_agent_workflows.")
    return steps


def _print_human_report(report: dict[str, Any]) -> None:
    print(f"vivado-agent-mcp selftest: {report['status']}")
    print(report["summary"])
    print(f"package={report['package']['name']} {report['package']['version']}")
    print(f"workspace={report['workspace']}")
    print(f"output_dir={report['output_dir']}")
    print(f"validation_scope={report.get('validation_scope', 'mcp_stdio_contract')}")
    print(f"include_vivado_probe={report.get('include_vivado_probe', False)}")
    print(f"result_path={report['result_path']}")
    print("")
    for check in report["checks"]:
        print(f"[{check['status']}] {check['id']}: {check['message']}")
    print("")
    print("next_steps:")
    for step in report["next_steps"]:
        print(f"- {step}")
    print("")
    print("hardware_validation=NOT_VALIDATED")


if __name__ == "__main__":
    raise SystemExit(main())
