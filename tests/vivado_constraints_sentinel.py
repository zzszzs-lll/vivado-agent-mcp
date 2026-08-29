from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_scenario_runner import call_tool, mcp_stdio_session, write_s01_project_sources

_WORKSPACE = Path(__file__).resolve().parents[1]
_SRC = str(_WORKSPACE / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from vivado_agent_mcp.release_identity import source_identity  # noqa: E402


PROTECTED_TOOLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("run_synthesis", {"run_name": "synth_1", "timeout_s": 60}),
    ("run_implementation", {"run_name": "impl_1", "timeout_s": 60}),
    ("generate_bitstream", {"run_name": "impl_1", "timeout_s": 60}),
    ("collect_report_bundle", {"run_name": "impl_1", "timeout_s": 60}),
    ("run_pre_hw_signoff", {"run_name": "impl_1", "timeout_s": 60}),
    (
        "run_behavioral_simulation",
        {
            "simset": "sim_1",
            "run_time": "1 us",
            "export_vcd": False,
            "incremental": False,
            "execution_intent": "verify executable constraint sentinel guard",
            "confirm": "RUN_TRUSTED_XSIM",
            "timeout_s": 60,
        },
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Vivado 2021.2 blocks executable XDC before typed design execution.")
    parser.add_argument("--workspace", default=str(_WORKSPACE))
    parser.add_argument("--output-dir", default="test_use/vivado_constraints_sentinel")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--part", default="xc7a35tcpg236-1")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    output_root = Path(args.output_dir)
    if not output_root.is_absolute():
        output_root = workspace / output_root
    run_dir = output_root / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=False)
    result = asyncio.run(
        run_sentinel(
            workspace=workspace,
            run_dir=run_dir,
            python_exe=str(args.python),
            part=str(args.part),
        )
    )
    result_path = run_dir / "vivado_constraints_sentinel_result.json"
    result["result_path"] = str(result_path)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": result["ok"], "result_path": str(result_path)}, ensure_ascii=False))
    return 0 if result["ok"] else 1


async def run_sentinel(*, workspace: Path, run_dir: Path, python_exe: str, part: str) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    runtime_dir = run_dir / "runtime"
    source_inputs = write_s01_project_sources(run_dir / "source")
    project_dir = run_dir / "project"
    sentinel_path = run_dir / "EXECUTABLE_CONSTRAINT_SENTINEL.txt"
    transcript: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    stop_result: dict[str, Any] = {}

    async with mcp_stdio_session(
        workspace=workspace,
        python_exe=python_exe,
        runtime_dir=runtime_dir,
        tool_profile="core",
    ) as session:
        try:
            results["environment"] = await call_tool(
                session,
                "detect_vivado_environment",
                {"probe_launch": True, "probe_timeout_s": 60, "runtime_dir": str(runtime_dir)},
                transcript,
            )
            results["start_session"] = await call_tool(
                session,
                "start_session",
                {"timeout_s": 240, "runtime_dir": str(runtime_dir)},
                transcript,
            )
            if results["start_session"].get("ok"):
                results["create_project"] = await call_tool(
                    session,
                    "create_project",
                    {
                        "project_name": "constraint_sentinel",
                        "project_dir": str(project_dir),
                        "part": part,
                        "top": source_inputs["top"],
                        "rtl_files": source_inputs["rtl_files"],
                        "xdc_files": source_inputs["xdc_files"],
                        "sim_files": source_inputs["sim_files"],
                        "testbench_top": source_inputs["testbench_top"],
                        "target_language": source_inputs["target_language"],
                        "timeout_s": 180,
                    },
                    transcript,
                )
            if results.get("create_project", {}).get("ok"):
                Path(source_inputs["xdc_files"][0]).write_text(
                    f"set vmcp_fp [open {{{sentinel_path.as_posix()}}} w]\n"
                    "puts $vmcp_fp {PWNED}\n"
                    "close $vmcp_fp\n",
                    encoding="utf-8",
                )
                for tool, tool_args in PROTECTED_TOOLS:
                    results[tool] = await call_tool(session, tool, dict(tool_args), transcript)
        finally:
            stop_result = await call_tool(session, "stop_session", {}, transcript)

    environment_data = results.get("environment", {}).get("data", {})
    tool_checks = {
        tool: result.get("error_code") == "EXECUTABLE_CONSTRAINT_INPUT_BLOCKED"
        for tool, _ in PROTECTED_TOOLS
        if (result := results.get(tool)) is not None
    }
    checks = {
        "vivado_2021_2": str(environment_data.get("version", "")) == "2021.2",
        "session_started": bool(results.get("start_session", {}).get("ok")),
        "project_created": bool(results.get("create_project", {}).get("ok")),
        "all_typed_paths_blocked": len(tool_checks) == len(PROTECTED_TOOLS) and all(tool_checks.values()),
        "sentinel_absent": not sentinel_path.exists(),
        "session_stopped": bool(stop_result.get("ok")),
    }
    return {
        "ok": all(checks.values()),
        "status": "PASS" if all(checks.values()) else "BLOCK",
        "started_at": started_at,
        "ended_at": datetime.now(UTC).isoformat(),
        "source_identity": source_identity(workspace),
        "execution_mode": "mcp_stdio_live_vivado_2021_2_constraint_sentinel",
        "evidence_class": "live_executable_constraint_negative_path",
        "hardware_validation_status": "NOT_VALIDATED",
        "sentinel_path": str(sentinel_path),
        "checks": checks,
        "tool_checks": tool_checks,
        "tool_results": {name: _summary(result) for name, result in results.items()},
        "stop_session": _summary(stop_result),
        "tool_calls": transcript,
    }


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(result.get("ok")),
        "error_code": str(result.get("error_code", "")),
        "message": str(result.get("message") or result.get("summary") or "")[:500],
    }


if __name__ == "__main__":
    raise SystemExit(main())
