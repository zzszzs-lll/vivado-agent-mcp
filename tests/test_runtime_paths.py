import os
from pathlib import Path
import subprocess
import tomllib

import pytest

from vivado_agent_mcp.vivado.env import (
    _parse_vivado_version,
    capture_server_vivado_identity,
    find_vivado,
    resolve_runtime_dir,
    vivado_environment,
)


def test_pytest_cache_dir_stays_under_vivado_agent_workspace() -> None:
    workspace = Path(__file__).resolve().parents[1]
    config = tomllib.loads((workspace / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_options = config["tool"]["pytest"]["ini_options"]

    assert pytest_options["cache_dir"] == ".vivado_agent_mcp/pytest_cache"
    assert not (workspace / ".pytest_cache").exists()
    assert not (workspace / ".pip_tmp").exists()


def test_pytest_basetemp_does_not_own_the_entire_test_use_evidence_root() -> None:
    workspace = Path(__file__).resolve().parents[1]
    config = tomllib.loads((workspace / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"].split()

    assert "--basetemp=test_use/pytest_tmp" in addopts
    assert "--basetemp=test_use" not in addopts


def test_pytest_tmp_path_stays_under_test_use(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    test_use = workspace / "test_use"

    assert tmp_path.resolve().is_relative_to(test_use.resolve())


def test_default_runtime_dir_stays_under_current_workspace(tmp_path: Path) -> None:
    runtime_dir = resolve_runtime_dir(cwd=tmp_path)

    assert runtime_dir == tmp_path / ".vivado_agent_mcp" / "runtime"


def test_vivado_environment_forces_temp_to_runtime_dir(tmp_path: Path) -> None:
    runtime_dir = tmp_path / ".vivado_agent_mcp" / "runtime"
    env = vivado_environment(base={"TEMP": "outside", "TMP": "outside"}, temp_dir=runtime_dir)

    assert env["TEMP"] == str(runtime_dir)
    assert env["TMP"] == str(runtime_dir)


def test_explicit_runtime_dir_overrides_default(tmp_path: Path) -> None:
    explicit = tmp_path / "test_use" / "runtime"
    ignored_cwd = tmp_path / "ignored_cwd"

    assert resolve_runtime_dir(str(explicit), cwd=ignored_cwd) == explicit.resolve()


def test_find_vivado_reports_native_simulator_tools(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "Vivado" / "2021.2" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("vivado.bat", "xvlog.bat", "xelab.bat", "xsim.bat"):
        (bin_dir / name).write_text("@echo off\necho fake\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(bin_dir / "vivado.bat"))

    result = find_vivado(str(bin_dir / "vivado.bat"))

    assert result["ok"] is True
    assert result["tools"]["vivado"]["available"] is True
    assert result["tools"]["xvlog"]["available"] is True
    assert result["tools"]["xelab"]["available"] is True
    assert result["tools"]["xsim"]["available"] is True
    assert result["xsim_available"] is True
    assert result["path_hint_version"] == "2021.2"
    assert result["probed_version"] is None
    assert result["version_attested"] is False
    assert result["version"] is None


def test_find_vivado_rejects_explicit_path_that_does_not_match_server_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    trusted = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("@echo off\necho trusted\n", encoding="utf-8")
    attacker = tmp_path / "attacker.cmd"
    attacker.write_text("@echo off\necho executed\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(trusted))
    called = False

    def reject_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("untrusted executable must not reach subprocess.run")

    monkeypatch.setattr(subprocess, "run", reject_run)

    result = find_vivado(str(attacker), probe_launch=True, runtime_dir=str(tmp_path / "runtime"))

    assert result["ok"] is False
    assert result["error_code"] == "VIVADO_PATH_MISMATCH"
    assert result["execution_attempted"] is False
    assert called is False


@pytest.mark.skipif(os.name != "nt", reason="Windows .cmd execution sentinel regression")
def test_fake_cmd_is_blocked_before_execution_and_cannot_create_sentinel(tmp_path: Path, monkeypatch) -> None:
    trusted = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("@echo off\necho Vivado v2021.2 ^(64-bit^)\n", encoding="utf-8")
    sentinel = tmp_path / "sentinel.txt"
    attacker = tmp_path / "fake-vivado.cmd"
    attacker.write_text(
        f'@echo off\necho executed>"{sentinel}"\necho Vivado v2021.2 ^(64-bit^)\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("VIVADO_PATH", str(trusted))

    result = find_vivado(str(attacker), probe_launch=True, runtime_dir=str(tmp_path / "runtime"))

    assert result["ok"] is False
    assert result["error_code"] == "VIVADO_PATH_MISMATCH"
    assert result["execution_attempted"] is False
    assert sentinel.exists() is False


def test_server_vivado_identity_blocks_file_replacement_before_probe(tmp_path: Path, monkeypatch) -> None:
    vivado = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    vivado.parent.mkdir(parents=True)
    vivado.write_text("@echo off\necho trusted\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))
    trusted_identity = capture_server_vivado_identity()
    vivado.write_text("@echo off\necho replaced\n", encoding="utf-8")
    called = False

    def reject_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("replaced executable must not reach subprocess.run")

    monkeypatch.setattr(subprocess, "run", reject_run)

    result = find_vivado(
        probe_launch=True,
        runtime_dir=str(tmp_path / "runtime"),
        trusted_identity=trusted_identity,
    )

    assert result["ok"] is False
    assert result["error_code"] == "VIVADO_PATH_IDENTITY_CHANGED"
    assert result["execution_attempted"] is False
    assert called is False


def test_vivado_version_parser_preserves_patch_versions() -> None:
    assert _parse_vivado_version("Vivado v2021.2.1 (64-bit)") == "2021.2.1"
    assert _parse_vivado_version("Vivado ML Edition v2021.2.1 (64-bit)") == "2021.2.1"


def test_find_vivado_launch_probe_reports_success(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "Vivado" / "2021.2" / "bin"
    bin_dir.mkdir(parents=True)
    vivado = bin_dir / "vivado.bat"
    vivado.write_text("@echo off\necho fake\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="Vivado v2021.2\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = find_vivado(str(vivado), probe_launch=True, probe_timeout_s=7, runtime_dir=str(tmp_path / "runtime"))

    assert result["ok"] is True
    assert result["launch_ready"] is True
    assert result["launch_probe"]["requested"] is True
    assert result["launch_probe"]["status"] == "PASS"
    assert result["launch_probe"]["returncode"] == 0
    assert result["launch_probe"]["stdout_tail"] == "Vivado v2021.2"
    assert result["path_hint_version"] == "2021.2"
    assert result["probed_version"] == "2021.2"
    assert result["version_attested"] is True
    assert result["version"] == "2021.2"
    assert captured["command"] == [str(vivado.resolve()), "-mode", "batch", "-version"]
    assert captured["kwargs"]["timeout"] == 7
    assert captured["kwargs"]["env"]["TEMP"] == str((tmp_path / "runtime").resolve())


def test_find_vivado_launch_probe_reports_process_exit(tmp_path: Path, monkeypatch) -> None:
    vivado = tmp_path / "vivado.bat"
    vivado.write_text("@echo off\nexit /b 1\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="license checkout failed\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = find_vivado(str(vivado), probe_launch=True, probe_timeout_s=3, runtime_dir=str(tmp_path / "runtime"))

    assert result["ok"] is True
    assert result["launch_ready"] is False
    assert result["launch_probe"]["status"] == "FAIL"
    assert result["launch_probe"]["returncode"] == 1
    assert result["launch_probe"]["diagnosis"]["primary_cause"] == "process_exit"
    assert result["launch_probe"]["stderr_tail"] == "license checkout failed"


def test_find_vivado_launch_probe_requires_attested_version_for_readiness(tmp_path: Path, monkeypatch) -> None:
    vivado = tmp_path / "vivado.bat"
    vivado.write_text("@echo off\necho completed\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="probe completed\n", stderr=""),
    )

    result = find_vivado(str(vivado), probe_launch=True, runtime_dir=str(tmp_path / "runtime"))

    assert result["launch_probe"]["process_launch_ok"] is True
    assert result["launch_probe"]["ok"] is False
    assert result["launch_probe"]["version_attested"] is False
    assert result["launch_probe"]["status"] == "UNATTESTED"
    assert result["launch_ready"] is False
    assert result["version"] is None


def test_find_vivado_attests_version_from_stderr(tmp_path: Path, monkeypatch) -> None:
    vivado = tmp_path / "vivado.bat"
    vivado.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="Vivado ML Edition v2021.2.1 (64-bit)\n",
        ),
    )

    result = find_vivado(str(vivado), probe_launch=True, runtime_dir=str(tmp_path / "runtime"))

    assert result["launch_ready"] is True
    assert result["probed_version"] == "2021.2.1"
    assert result["version_attested"] is True


def test_find_vivado_launch_probe_reports_timeout(tmp_path: Path, monkeypatch) -> None:
    vivado = tmp_path / "vivado.bat"
    vivado.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="starting", stderr="still waiting")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = find_vivado(str(vivado), probe_launch=True, probe_timeout_s=2, runtime_dir=str(tmp_path / "runtime"))

    assert result["launch_ready"] is False
    assert result["launch_probe"]["status"] == "TIMEOUT"
    assert result["launch_probe"]["timeout_s"] == 2
    assert result["launch_probe"]["diagnosis"]["primary_cause"] == "timeout"
    assert result["launch_probe"]["stdout_tail"] == "starting"
    assert result["version_attested"] is False
    assert result["probed_version"] is None


def test_find_vivado_probe_output_overrides_misleading_path_hint(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "Vivado" / "2021.2" / "bin"
    bin_dir.mkdir(parents=True)
    vivado = bin_dir / "vivado.bat"
    vivado.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="Vivado v2024.2 (64-bit)\n", stderr=""),
    )

    result = find_vivado(str(vivado), probe_launch=True, runtime_dir=str(tmp_path / "runtime"))

    assert result["path_hint_version"] == "2021.2"
    assert result["probed_version"] == "2024.2"
    assert result["version_attested"] is True
    assert result["version"] == "2024.2"


def test_find_vivado_preserves_patch_version_when_path_hint_is_base_release(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "Vivado" / "2021.2" / "bin"
    bin_dir.mkdir(parents=True)
    vivado = bin_dir / "vivado.bat"
    vivado.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="Vivado v2021.2.1 (64-bit)\n", stderr=""),
    )

    result = find_vivado(str(vivado), probe_launch=True, runtime_dir=str(tmp_path / "runtime"))

    assert result["path_hint_version"] == "2021.2"
    assert result["probed_version"] == "2021.2.1"
    assert result["version"] == "2021.2.1"


def test_find_vivado_attests_version_when_install_path_has_no_version(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "toolchain" / "bin"
    bin_dir.mkdir(parents=True)
    vivado = bin_dir / "vivado.bat"
    vivado.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="Vivado v2021.2 (64-bit)\n", stderr=""),
    )

    result = find_vivado(str(vivado), probe_launch=True, runtime_dir=str(tmp_path / "runtime"))

    assert result["path_hint_version"] is None
    assert result["probed_version"] == "2021.2"
    assert result["version_attested"] is True


def test_find_vivado_has_no_machine_specific_fallback(monkeypatch) -> None:
    monkeypatch.delenv("VIVADO_PATH", raising=False)
    monkeypatch.setattr("vivado_agent_mcp.vivado.env.shutil.which", lambda name: None)

    result = find_vivado()

    assert result["ok"] is False
    assert result["searched"] == []
