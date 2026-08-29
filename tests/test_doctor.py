import json
from pathlib import Path
from types import SimpleNamespace

from vivado_agent_mcp import __version__
from vivado_agent_mcp import doctor
from vivado_agent_mcp import __main__ as entrypoint
from vivado_agent_mcp.registry import profile_tool_names, tool_names


def test_doctor_ready_with_validated_vivado(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "find_vivado", lambda *args, **kwargs: _vivado_result(version="2021.2", launch_ok=True))

    report = doctor.build_doctor_report(workspace=str(tmp_path))

    assert report["status"] == "READY"
    assert report["ok"] is True
    assert report["version_policy"]["requires_exact_vivado_version"] is True
    assert report["version_policy"]["validated_versions"] == ["2021.2"]
    assert report["version_policy"]["probed_version"] == "2021.2"
    assert report["version_policy"]["version_attested"] is True
    assert report["probe_timeout_s"] == doctor.DEFAULT_PROBE_TIMEOUT_S
    assert report["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert report["mcp"]["tool_profile"] == "core"
    assert report["mcp"]["tool_count"] == len(profile_tool_names("core"))
    assert report["mcp"]["registry_tool_count"] == len(tool_names())
    assert any(check["id"] == "mcp_tool_registry" and check["status"] == "PASS" for check in report["checks"])


def test_doctor_reports_environment_selected_tool_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIVADO_AGENT_MCP_TOOL_PROFILE", "advanced")
    monkeypatch.setattr(doctor, "find_vivado", lambda *args, **kwargs: _vivado_result(version="2021.2", launch_ok=True))

    report = doctor.build_doctor_report(workspace=str(tmp_path))

    assert report["mcp"]["tool_profile"] == "advanced"
    assert report["mcp"]["exposed_tool_count"] == len(profile_tool_names("advanced"))
    assert report["mcp"]["registry_tool_count"] == len(tool_names())


def test_doctor_blocks_unsupported_vivado_version(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "find_vivado", lambda *args, **kwargs: _vivado_result(version="2024.2", launch_ok=True))

    report = doctor.build_doctor_report(workspace=str(tmp_path))

    assert report["status"] == "BLOCK"
    version_check = _check(report, "vivado_version")
    assert version_check["status"] == "BLOCK"
    assert "Vivado 2021.2 is required" in version_check["message"]
    assert any("2021.2" in step for step in report["next_steps"])


def test_doctor_blocks_unqualified_vivado_patch_release(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "find_vivado", lambda *args, **kwargs: _vivado_result(version="2021.2.1", launch_ok=True))

    report = doctor.build_doctor_report(workspace=str(tmp_path))

    assert report["status"] == "BLOCK"
    assert report["version_policy"]["probed_version"] == "2021.2.1"
    version_check = _check(report, "vivado_version")
    assert version_check["status"] == "BLOCK"
    assert "2021.2.1" in version_check["message"]


def test_doctor_blocks_when_vivado_launch_probe_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "find_vivado", lambda *args, **kwargs: _vivado_result(version="2021.2", launch_ok=False))

    report = doctor.build_doctor_report(workspace=str(tmp_path))

    assert report["status"] == "BLOCK"
    launch_check = _check(report, "vivado_launch_probe")
    assert launch_check["status"] == "BLOCK"
    assert any("vivado-agent-mcp doctor --probe-timeout-s 120" in step for step in report["next_steps"])
    assert any("probe-timeout" in step or "detect_vivado_environment" in step for step in report["next_steps"])


def test_doctor_does_not_trust_path_hint_without_attested_probe(tmp_path: Path, monkeypatch) -> None:
    result = _vivado_result(version="2021.2", launch_ok=True)
    result.update({"probed_version": None, "version_attested": False, "version": None, "launch_ready": False})
    result["launch_probe"].update(
        {
            "ok": False,
            "status": "UNATTESTED",
            "process_launch_ok": True,
            "probed_version": None,
            "version_attested": False,
            "launch_ready": False,
            "diagnosis": {
                "primary_cause": "version_unattested",
                "message": "Vivado batch probe completed, but its output did not contain an attested Vivado version.",
            },
        }
    )
    monkeypatch.setattr(doctor, "find_vivado", lambda *args, **kwargs: result)

    report = doctor.build_doctor_report(workspace=str(tmp_path))

    assert report["status"] == "BLOCK"
    version_check = _check(report, "vivado_version")
    assert version_check["data"]["path_hint_version"] == "2021.2"
    assert version_check["data"]["probed_version"] is None
    assert version_check["data"]["version_attested"] is False
    launch_check = _check(report, "vivado_launch_probe")
    assert launch_check["status"] == "BLOCK"
    assert "did not contain an attested Vivado version" in launch_check["message"]


def test_python_check_matches_requires_python_minor_range(monkeypatch) -> None:
    for minor, expected in ((10, "BLOCK"), (11, "PASS"), (12, "PASS"), (13, "BLOCK")):
        checks = []
        monkeypatch.setattr(doctor.sys, "version_info", SimpleNamespace(major=3, minor=minor, micro=0))
        doctor._add_python_check(checks)
        assert checks[0]["status"] == expected
        assert checks[0]["data"]["requires"] == ">=3.11,<3.13"


def test_doctor_json_output(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(doctor, "find_vivado", lambda *args, **kwargs: _vivado_result(version="2021.2", launch_ok=True))

    assert doctor.main(["--workspace", str(tmp_path), "--json"]) == 0

    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["status"] == "READY"
    assert data["mcp"]["transport"] == "stdio"


def test_entrypoint_doctor_subcommand_does_not_start_stdio(monkeypatch) -> None:
    calls = {}

    def fake_doctor_main(args):
        calls["doctor_args"] = args
        return 0

    async def fake_run_stdio_server():
        calls["stdio_started"] = True

    monkeypatch.setattr(entrypoint, "doctor_main", fake_doctor_main)
    monkeypatch.setattr(entrypoint, "run_stdio_server", fake_run_stdio_server)

    assert entrypoint.main(["doctor", "--json"]) == 0
    assert calls["doctor_args"] == ["--json"]
    assert "stdio_started" not in calls


def test_entrypoint_selftest_subcommand_does_not_start_stdio(monkeypatch) -> None:
    calls = {}

    def fake_selftest_main(args):
        calls["selftest_args"] = args
        return 0

    async def fake_run_stdio_server():
        calls["stdio_started"] = True

    monkeypatch.setattr(entrypoint, "selftest_main", fake_selftest_main)
    monkeypatch.setattr(entrypoint, "run_stdio_server", fake_run_stdio_server)

    assert entrypoint.main(["selftest", "--json"]) == 0
    assert calls["selftest_args"] == ["--json"]
    assert "stdio_started" not in calls


def test_entrypoint_help_does_not_start_stdio(monkeypatch, capsys) -> None:
    calls = {}

    async def fake_run_stdio_server():
        calls["stdio_started"] = True

    monkeypatch.setattr(entrypoint, "run_stdio_server", fake_run_stdio_server)

    assert entrypoint.main(["--help"]) == 0

    output = capsys.readouterr().out
    assert "vivado-agent-mcp" in output
    assert "doctor" in output
    assert "selftest" in output
    assert "stdio MCP server" in output
    assert "stdio_started" not in calls


def test_entrypoint_version_does_not_start_stdio(monkeypatch, capsys) -> None:
    calls = {}

    async def fake_run_stdio_server():
        calls["stdio_started"] = True

    monkeypatch.setattr(entrypoint, "run_stdio_server", fake_run_stdio_server)

    assert entrypoint.main(["--version"]) == 0

    assert capsys.readouterr().out.strip() == __version__
    assert "stdio_started" not in calls


def test_entrypoint_without_subcommand_starts_stdio(monkeypatch) -> None:
    calls = {}

    def fake_doctor_main(args):
        calls["doctor_args"] = args
        return 0

    async def fake_run_stdio_server():
        calls["stdio_started"] = True

    monkeypatch.setattr(entrypoint, "doctor_main", fake_doctor_main)
    monkeypatch.setattr(entrypoint, "run_stdio_server", fake_run_stdio_server)

    assert entrypoint.main([]) is None
    assert calls["stdio_started"] is True
    assert "doctor_args" not in calls


def _check(report: dict, check_id: str) -> dict:
    return next(check for check in report["checks"] if check["id"] == check_id)


def _vivado_result(*, version: str, launch_ok: bool) -> dict:
    return {
        "ok": True,
        "path": rf"D:\Xilinx\Vivado\{version}\bin\vivado.bat",
        "source": "candidate",
        "install_bin": rf"D:\Xilinx\Vivado\{version}\bin",
        "version": version,
        "path_hint_version": version,
        "probed_version": version,
        "version_attested": launch_ok,
        "tools": {
            "vivado": {"available": True, "path": rf"D:\Xilinx\Vivado\{version}\bin\vivado.bat", "version": version},
            "xvlog": {"available": True, "path": rf"D:\Xilinx\Vivado\{version}\bin\xvlog.bat", "version": None},
            "xelab": {"available": True, "path": rf"D:\Xilinx\Vivado\{version}\bin\xelab.bat", "version": None},
            "xsim": {"available": True, "path": rf"D:\Xilinx\Vivado\{version}\bin\xsim.bat", "version": None},
        },
        "xsim_available": True,
        "searched": [],
        "launch_ready": launch_ok,
        "launch_probe": {
            "requested": True,
            "ok": launch_ok,
            "status": "PASS" if launch_ok else "FAIL",
            "probed_version": version if launch_ok else None,
            "version_attested": launch_ok,
            "diagnosis": {"primary_cause": "launch_ok" if launch_ok else "process_exit"},
        },
    }
