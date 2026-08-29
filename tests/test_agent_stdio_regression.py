import json
import os
import subprocess
import sys
from pathlib import Path


def test_agent_stdio_regression_script_runs_from_current_source(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    output_dir = tmp_path / "agent_stdio_regression"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(workspace / "src")
    env.setdefault("SystemRoot", r"C:\WINDOWS")
    env.setdefault("WINDIR", r"C:\WINDOWS")

    completed = subprocess.run(
        [
            sys.executable,
            str(workspace / "tests" / "agent_stdio_regression.py"),
            "--output-dir",
            str(output_dir),
        ],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    result = json.loads((output_dir / "agent_stdio_regression_result.json").read_text(encoding="utf-8"))
    assert completed.returncode == 0, completed.stdout + completed.stderr + json.dumps(result, ensure_ascii=False, indent=2)
    assert result["ok"] is True
    assert result["tool_count"] >= 90
    assert result["checks"]["run_behavioral_simulation_has_max_vcd_mb"] is True
    assert result["checks"]["catalog_ok"] is True
    assert result["checks"]["workflows_ok"] is True
    assert result["checks"]["tcl_policy_blocked"] is True
    assert result["checks"]["hardware_gate_blocked"] is True
    assert result["package_execution"]["mode"] == "workspace_source"
    assert result["package_execution"]["workspace_source_enabled"] is True
    assert result["package_execution"]["mcp_server_import_guard"] is True
    assert result["package_execution"]["timeout_server_import_guard"] is True
