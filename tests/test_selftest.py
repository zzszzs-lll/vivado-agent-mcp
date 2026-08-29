import json
import os
import subprocess
import sys
from pathlib import Path

from vivado_agent_mcp import selftest


def test_selftest_cli_runs_from_current_source(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    output_dir = tmp_path / "selftest"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(workspace / "src")
    env.setdefault("SystemRoot", r"C:\WINDOWS")
    env.setdefault("WINDIR", r"C:\WINDOWS")
    env["VIVADO_AGENT_MCP_TOOL_PROFILE"] = "core"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "vivado_agent_mcp",
            "selftest",
            "--workspace",
            str(workspace),
            "--output-dir",
            str(output_dir),
            "--json",
        ],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] in {"PASS", "WARN"}
    assert report["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert report["execution_mode"] == "mcp_stdio_selftest"
    assert report["tool_profile"] == "core"
    assert report["validation_scope"] == "mcp_stdio_contract"
    assert report["include_vivado_probe"] is False
    if report["status"] == "PASS":
        assert "not live Vivado project execution" in report["summary"]
    else:
        assert "before live Vivado workflows" in report["summary"]
    assert Path(report["result_path"]).exists()

    checks = {check["id"]: check for check in report["checks"]}
    assert checks["list_tools"]["status"] == "PASS"
    assert checks["list_tools"]["data"]["tool_profile"] == "core"
    assert checks["required_tools"]["status"] == "PASS"
    assert checks["input_schema_contract"]["status"] == "PASS"
    assert checks["get_tool_catalog"]["status"] == "PASS"
    assert checks["get_agent_workflows"]["status"] == "PASS"
    assert checks["get_agent_scenarios"]["status"] == "PASS"
    assert checks["agent_workflow_payloads"]["status"] == "PASS"
    assert checks["diagnostic_bundle_contract"]["status"] == "PASS"
    diagnostic_data = checks["diagnostic_bundle_contract"]["data"]
    assert diagnostic_data["health_status"] == "WARN"
    assert diagnostic_data["handoff_ready"] is False
    assert diagnostic_data["handoff_reviewable"] is True
    assert diagnostic_data["bundle_mode"] == "reference"
    assert diagnostic_data["portable"] is False
    assert checks["tcl_safety_gates"]["status"] == "PASS"
    assert checks["hardware_programming_gate"]["status"] == "PASS"
    assert checks["hardware_manager_gate"]["status"] == "PASS"


def test_selftest_exception_report_preserves_probe_context(tmp_path: Path, monkeypatch, capsys) -> None:
    async def fail_selftest(**kwargs):
        raise RuntimeError("stdio failed")

    monkeypatch.setattr(selftest, "run_selftest", fail_selftest)

    rc = selftest.main(
        [
            "--workspace",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "selftest"),
            "--include-vivado-probe",
            "--probe-timeout-s",
            "77",
            "--json",
        ]
    )

    assert rc == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "BLOCK"
    assert report["include_vivado_probe"] is True
    assert report["probe_timeout_s"] == 77
    assert report["validation_scope"] == "mcp_stdio_contract_plus_vivado_probe"
    assert Path(report["result_path"]).exists()
