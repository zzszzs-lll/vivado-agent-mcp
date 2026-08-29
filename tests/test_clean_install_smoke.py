from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import clean_install_smoke
import pytest


@pytest.fixture(autouse=True)
def _stub_immutable_snapshot_for_orchestration_tests(monkeypatch) -> None:
    default_source = {
        "available": True,
        "clean": True,
        "identity_id": "a" * 64,
        "commit": "b" * 40,
        "tree": "c" * 40,
        "tracked_digest": "d" * 64,
    }
    package_manifest = {
        "schema_version": 1,
        "member_count": 1,
        "digest": "e" * 64,
        "members": [{"path": "vivado_agent_mcp/__init__.py", "size": 1, "sha256": "f" * 64}],
    }
    monkeypatch.setattr(clean_install_smoke, "source_identity", lambda workspace: dict(default_source))

    def fake_snapshot(workspace: Path, destination: Path) -> dict:
        source = clean_install_smoke.source_identity(workspace)
        if source.get("available") is not True or source.get("clean") is not True:
            return {
                "ok": False,
                "reason_code": "source_identity_not_clean",
                "reason": "test source is not clean",
                "source_identity": source,
                "snapshot_root": str(destination),
            }
        destination.mkdir(parents=True, exist_ok=True)
        return {
            "ok": True,
            "snapshot_type": "git_archive_head",
            "snapshot_root": str(destination),
            "archive_sha256": "1" * 64,
            "source_identity": source,
            "package_root_contamination": {"status": "READY", "paths": []},
            "package_manifest": package_manifest,
        }

    monkeypatch.setattr(clean_install_smoke, "materialize_immutable_git_snapshot", fake_snapshot)
    monkeypatch.setattr(clean_install_smoke, "package_member_manifest", lambda *args, **kwargs: package_manifest)
    monkeypatch.setattr(clean_install_smoke, "wheel_package_member_manifest", lambda wheel: package_manifest)
    monkeypatch.setattr(
        clean_install_smoke,
        "_validation_harness_manifest",
        lambda workspace: {
            "status": "READY",
            "policy": "exact_source_harness_size_sha256",
            "identity_sha256": "2" * 64,
            "files": [],
            "errors": [],
        },
    )


def test_clean_install_smoke_help_bootstraps_source_package_in_isolated_mode(tmp_path: Path) -> None:
    script = Path(__file__).with_name("clean_install_smoke.py")
    completed = subprocess.run(
        [sys.executable, "-I", str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Build a wheel" in completed.stdout


def test_clean_install_smoke_report_happy_path(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    runtime_lock = tmp_path / "runtime-lock.txt"
    runtime_lock.write_text("mcp==1.29.1 --hash=sha256:" + "1" * 64 + "\n", encoding="ascii")
    clean_source = {"available": True, "clean": True, "identity_id": "a" * 64}

    def fake_run_command(command, *, cwd, commands: list, timeout_s, clean_pythonpath=False):
        command_text = " ".join(command)
        commands.append(
            {
                "command": command,
                "cwd": str(cwd),
                "returncode": 0,
                "stdout_tail": "",
                "stderr_tail": "",
                "clean_pythonpath": clean_pythonpath,
            }
        )
        if "pip wheel" in command_text:
            wheelhouse = Path(command[-1])
            wheelhouse.mkdir(parents=True, exist_ok=True)
            _write_fake_wheel(wheelhouse / "vivado_agent_mcp-0.10.0-py3-none-any.whl")
        if " venv " in command_text:
            scripts = run_dir / "venv" / "Scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "python.exe").write_text("python", encoding="utf-8")
            (scripts / "vivado-agent-mcp.exe").write_text("script", encoding="utf-8")
        stdout = ""
        if "import vivado_agent_mcp" in command_text:
            stdout = "0.10.0\n"
        if command[-1] == "--help":
            stdout = "vivado-agent-mcp doctor selftest\n"
        if command[-1] == "--version":
            stdout = "0.10.0\n"
        if " doctor " in command_text:
            stdout = '{"status":"READY","package":{"name":"vivado-agent-mcp"},"checks":[]}'
        if " selftest " in command_text:
            stdout = '{"status":"PASS","validation_scope":"mcp_stdio_contract","hardware_validation":{"status":"NOT_VALIDATED"}}'
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "stdout_tail": stdout,
            "stderr_tail": "",
            "clean_pythonpath": clean_pythonpath,
        }

    monkeypatch.setattr(clean_install_smoke, "_run_command", fake_run_command)
    monkeypatch.setattr(clean_install_smoke, "source_identity", lambda workspace: clean_source)

    report = clean_install_smoke.run_clean_install_smoke(
        workspace=workspace,
        run_dir=run_dir,
        python_exe=Path(sys.executable),
        include_vivado_probe=False,
        probe_timeout_s=60,
        runtime_lock_path=runtime_lock,
    )

    assert report["status"] == "PASS"
    assert report["execution_mode"] == "clean_venv_wheel_install_smoke"
    assert report["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert report["doctor_report"]["status"] == "READY"
    assert report["selftest_report"]["validation_scope"] == "mcp_stdio_contract"
    assert report["source_wheel_provenance_verified"] is True
    assert len(report["source_wheel_pair_id"]) == 64
    assert report["release_evidence_id"] == report["source_wheel_pair_id"]
    assert report["release_evidence_ready"] is True
    assert report["dependency_lock_verified"] is True
    assert report["evidence_state"]["source_wheel_provenance"] == "PASS"
    wheel_build_command = next(
        item["command"]
        for item in report["commands"]
        if item["command"][1:4] == ["-m", "pip", "wheel"]
    )
    assert "--no-deps" in wheel_build_command
    assert "--no-build-isolation" in wheel_build_command
    assert any(check["id"] == "console_script" and check["status"] == "PASS" for check in report["checks"])
    assert any(check["id"] == "console_help" and check["status"] == "PASS" for check in report["checks"])
    assert any(check["id"] == "console_version" and check["status"] == "PASS" for check in report["checks"])


def test_clean_install_smoke_blocks_on_console_version_mismatch(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def fake_run_command(command, *, cwd, commands: list, timeout_s, clean_pythonpath=False):
        command_text = " ".join(command)
        commands.append(
            {
                "command": command,
                "cwd": str(cwd),
                "returncode": 0,
                "stdout_tail": "",
                "stderr_tail": "",
                "clean_pythonpath": clean_pythonpath,
            }
        )
        if "pip wheel" in command_text:
            wheelhouse = Path(command[-1])
            wheelhouse.mkdir(parents=True, exist_ok=True)
            _write_fake_wheel(wheelhouse / "vivado_agent_mcp-0.10.0-py3-none-any.whl")
        if " venv " in command_text:
            scripts = run_dir / "venv" / "Scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "python.exe").write_text("python", encoding="utf-8")
            (scripts / "vivado-agent-mcp.exe").write_text("script", encoding="utf-8")
        stdout = ""
        if "import vivado_agent_mcp" in command_text:
            stdout = "0.10.0\n"
        if command[-1] == "--help":
            stdout = "vivado-agent-mcp doctor selftest\n"
        if command[-1] == "--version":
            stdout = "0.9.0\n"
        if " doctor " in command_text:
            stdout = '{"status":"READY","package":{"name":"vivado-agent-mcp"},"checks":[]}'
        if " selftest " in command_text:
            stdout = '{"status":"PASS","validation_scope":"mcp_stdio_contract","hardware_validation":{"status":"NOT_VALIDATED"}}'
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "stdout_tail": stdout,
            "stderr_tail": "",
            "clean_pythonpath": clean_pythonpath,
        }

    monkeypatch.setattr(clean_install_smoke, "_run_command", fake_run_command)

    report = clean_install_smoke.run_clean_install_smoke(
        workspace=workspace,
        run_dir=run_dir,
        python_exe=Path(sys.executable),
        include_vivado_probe=False,
        probe_timeout_s=60,
    )

    version_check = next(check for check in report["checks"] if check["id"] == "console_version")
    assert report["status"] == "BLOCK"
    assert version_check["status"] == "BLOCK"
    assert version_check["data"]["console_version"] == "0.9.0"
    assert version_check["data"]["installed_version"] == "0.10.0"


def test_clean_install_smoke_forwards_vivado_probe_flags(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    observed: list[list[str]] = []

    def fake_run_command(command, *, cwd, commands: list, timeout_s, clean_pythonpath=False):
        observed.append(list(command))
        command_text = " ".join(command)
        commands.append(
            {
                "command": command,
                "cwd": str(cwd),
                "returncode": 0,
                "stdout_tail": "",
                "stderr_tail": "",
                "clean_pythonpath": clean_pythonpath,
            }
        )
        if "pip wheel" in command_text:
            wheelhouse = Path(command[-1])
            wheelhouse.mkdir(parents=True, exist_ok=True)
            _write_fake_wheel(wheelhouse / "vivado_agent_mcp-0.10.0-py3-none-any.whl")
        if " venv " in command_text:
            scripts = run_dir / "venv" / "Scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "python.exe").write_text("python", encoding="utf-8")
            (scripts / "vivado-agent-mcp.exe").write_text("script", encoding="utf-8")
        stdout = ""
        if "import vivado_agent_mcp" in command_text:
            stdout = "0.10.0\n"
        if command[-1] == "--help":
            stdout = "vivado-agent-mcp doctor selftest\n"
        if command[-1] == "--version":
            stdout = "0.10.0\n"
        if " doctor " in command_text:
            stdout = '{"status":"WARN","package":{"name":"vivado-agent-mcp"},"checks":[{"id":"xsim_tools","status":"WARN"}]}'
        if " selftest " in command_text:
            stdout = '{"status":"WARN","validation_scope":"mcp_stdio_contract_plus_vivado_probe","hardware_validation":{"status":"NOT_VALIDATED","validated":false}}'
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "stdout_tail": stdout,
            "stderr_tail": "",
            "clean_pythonpath": clean_pythonpath,
        }

    monkeypatch.setattr(clean_install_smoke, "_run_command", fake_run_command)

    report = clean_install_smoke.run_clean_install_smoke(
        workspace=workspace,
        run_dir=run_dir,
        python_exe=Path(sys.executable),
        include_vivado_probe=True,
        probe_timeout_s=123,
    )

    doctor_cmd = next(command for command in observed if "doctor" in command)
    selftest_cmd = next(command for command in observed if "selftest" in command)
    assert report["status"] == "WARN"
    assert "--no-probe-launch" not in doctor_cmd
    assert doctor_cmd[-2:] == ["--probe-timeout-s", "123"]
    assert "--include-vivado-probe" in selftest_cmd
    assert selftest_cmd[-2:] == ["--probe-timeout-s", "123"]
    assert report["selftest_report"]["validation_scope"] == "mcp_stdio_contract_plus_vivado_probe"
    assert next(check for check in report["checks"] if check["id"] == "doctor")["status"] == "WARN"
    assert next(check for check in report["checks"] if check["id"] == "selftest")["status"] == "WARN"


def test_clean_install_smoke_accepts_structured_missing_vivado_as_environment_warning() -> None:
    status, contract_ok, block_ids = clean_install_smoke._classify_doctor_report(
        {
            "status": "BLOCK",
            "package": {"name": "vivado-agent-mcp", "version": "0.10.0"},
            "checks": [
                {"id": "python", "status": "PASS"},
                {"id": "runtime_dir", "status": "PASS"},
                {"id": "vivado_path", "status": "BLOCK"},
            ],
        },
        returncode=2,
    )

    assert status == "WARN"
    assert contract_ok is True
    assert block_ids == {"vivado_path"}


def test_clean_install_smoke_blocks_unexpected_doctor_failure() -> None:
    status, contract_ok, block_ids = clean_install_smoke._classify_doctor_report(
        {
            "status": "BLOCK",
            "package": {"name": "vivado-agent-mcp", "version": "0.10.0"},
            "checks": [{"id": "mcp_tool_registry", "status": "BLOCK"}],
        },
        returncode=2,
    )

    assert status == "BLOCK"
    assert contract_ok is True
    assert block_ids == {"mcp_tool_registry"}


def test_clean_install_smoke_blocks_when_exact_wheel_hash_mismatches(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    wheel_path = tmp_path / "dist" / "vivado_agent_mcp-0.10.0-py3-none-any.whl"
    wheel_path.parent.mkdir()
    wheel_path.write_bytes(b"exact wheel bytes")

    monkeypatch.setattr(
        clean_install_smoke,
        "_run_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("hash mismatch must block before command execution")),
    )

    report = clean_install_smoke.run_clean_install_smoke(
        workspace=workspace,
        run_dir=run_dir,
        python_exe=Path(sys.executable),
        include_vivado_probe=False,
        probe_timeout_s=60,
        wheel_path=wheel_path,
        expected_wheel_sha256="0" * 64,
    )

    integrity = next(check for check in report["checks"] if check["id"] == "wheel_integrity")
    assert report["status"] == "BLOCK"
    assert integrity["status"] == "BLOCK"
    assert report["wheel_path"] == str(wheel_path.resolve())
    assert report["wheel_sha256"] != "0" * 64


def test_clean_install_smoke_installs_supplied_wheel_without_rebuilding_project(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    wheel_path = (tmp_path / "dist" / "vivado_agent_mcp-0.10.0-py3-none-any.whl").resolve()
    wheel_path.parent.mkdir()
    _write_fake_wheel(wheel_path)
    expected_sha256 = clean_install_smoke.sha256_file(wheel_path)
    runtime_lock = tmp_path / "runtime-lock.txt"
    runtime_lock.write_text("mcp==1.29.1 --hash=sha256:" + "1" * 64 + "\n", encoding="ascii")
    source_identity = {"available": True, "clean": True, "identity_id": "a" * 64}
    source_manifest = clean_install_smoke.package_member_manifest(workspace, relative_to=workspace)
    provenance_manifest = tmp_path / "dist" / "source-provenance.json"
    provenance_manifest.write_text(
        json.dumps(
            {
                "status": "PASS",
                "package": {"name": "vivado-agent-mcp", "version": "0.10.0"},
                "source_identity": source_identity,
                "source_package_manifest": source_manifest,
                "package_member_comparison": {"matches": True},
                "wheel": {"name": wheel_path.name, "sha256": expected_sha256},
                "source_wheel_provenance_verified": True,
                "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
            }
        ),
        encoding="utf-8",
    )
    observed: list[list[str]] = []

    def fake_run_command(command, *, cwd, commands: list, timeout_s, clean_pythonpath=False):
        observed.append(list(command))
        command_text = " ".join(command)
        commands.append(
            {
                "command": command,
                "cwd": str(cwd),
                "returncode": 0,
                "stdout_tail": "",
                "stderr_tail": "",
                "clean_pythonpath": clean_pythonpath,
            }
        )
        if command[1:4] == ["-m", "pip", "wheel"]:
            wheelhouse = Path(command[-1])
            wheelhouse.mkdir(parents=True, exist_ok=True)
            (wheelhouse / wheel_path.name).write_bytes(wheel_path.read_bytes())
        if " venv " in command_text:
            scripts = run_dir / "venv" / "Scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "python.exe").write_text("python", encoding="utf-8")
            (scripts / "vivado-agent-mcp.exe").write_text("script", encoding="utf-8")
        stdout = ""
        if "import vivado_agent_mcp" in command_text:
            stdout = "0.10.0\n"
        if command[-1] == "--help":
            stdout = "vivado-agent-mcp doctor selftest\n"
        if command[-1] == "--version":
            stdout = "0.10.0\n"
        if " doctor " in command_text:
            stdout = '{"status":"READY","package":{"name":"vivado-agent-mcp"},"checks":[]}'
        if " selftest " in command_text:
            stdout = '{"status":"PASS","validation_scope":"mcp_stdio_contract","hardware_validation":{"status":"NOT_VALIDATED"}}'
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "stdout_tail": stdout,
            "stderr_tail": "",
            "clean_pythonpath": clean_pythonpath,
        }

    monkeypatch.setattr(clean_install_smoke, "_run_command", fake_run_command)
    monkeypatch.setattr(clean_install_smoke, "source_identity", lambda workspace: dict(source_identity))

    report = clean_install_smoke.run_clean_install_smoke(
        workspace=workspace,
        run_dir=run_dir,
        python_exe=Path(sys.executable),
        include_vivado_probe=False,
        probe_timeout_s=60,
        wheel_path=wheel_path,
        expected_wheel_sha256=expected_sha256,
        runtime_lock_path=runtime_lock,
        source_provenance_manifest_path=provenance_manifest,
    )

    pip_wheel_commands = [command for command in observed if command[1:4] == ["-m", "pip", "wheel"]]
    install_command = next(command for command in observed if command[1:4] == ["-m", "pip", "install"])
    assert report["status"] == "PASS"
    assert report["wheel_sha256"] == expected_sha256
    assert report["wheel_sha256_verified"] is True
    assert report["pip_hashes_enforced"] is True
    assert report["package"]["version_verified"] is True
    assert report["wheel_source"] == "supplied_dist_with_provenance"
    assert report["source_wheel_provenance_verified"] is True
    assert report["source_wheel_pair_id"]
    assert report["release_evidence_id"] == report["source_wheel_pair_id"]
    assert report["release_evidence_ready"] is True
    assert report["evidence_state"]["source_wheel_provenance"] == "PASS"
    assert pip_wheel_commands == []
    assert "--require-hashes" in install_command
    assert str(run_dir / "wheel-requirements.txt") in install_command
    dependency_download = next(command for command in observed if command[1:4] == ["-m", "pip", "download"])
    assert str(runtime_lock) in dependency_download


def test_supplied_source_provenance_blocks_foreign_wheel_hash(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    wheel_path = tmp_path / "vivado_agent_mcp-0.10.0-py3-none-any.whl"
    _write_fake_wheel(wheel_path)
    source = {"available": True, "clean": True, "identity_id": "a" * 64}
    source_manifest = clean_install_smoke.package_member_manifest(workspace, relative_to=workspace)
    manifest_path = tmp_path / "source-provenance.json"
    manifest_path.write_text(
        json.dumps(
            {
                "status": "PASS",
                "package": {"name": "vivado-agent-mcp", "version": "0.10.0"},
                "source_identity": source,
                "source_package_manifest": source_manifest,
                "package_member_comparison": {"matches": True},
                "wheel": {"name": wheel_path.name, "sha256": "0" * 64},
                "source_wheel_provenance_verified": True,
                "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
            }
        ),
        encoding="utf-8",
    )

    verified, data = clean_install_smoke._verify_supplied_source_provenance(
        workspace=workspace,
        manifest_path=manifest_path,
        source=source,
        wheel_path=wheel_path,
        wheel_sha256=clean_install_smoke.sha256_file(wheel_path),
    )

    assert verified is False
    assert "manifest wheel identity does not match the supplied wheel" in data["reasons"]


@pytest.mark.parametrize("missing_artifact", ["venv_python", "console_script"])
def test_clean_install_smoke_preserves_supplied_wheel_provenance_on_post_validation_failure(
    tmp_path: Path,
    monkeypatch,
    missing_artifact: str,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    wheel_path = (tmp_path / "dist" / "vivado_agent_mcp-0.10.0-py3-none-any.whl").resolve()
    _write_fake_wheel(wheel_path)
    expected_sha256 = clean_install_smoke.sha256_file(wheel_path)

    def fake_run_command(command, *, cwd, commands: list, timeout_s, clean_pythonpath=False):
        command_text = " ".join(command)
        commands.append(
            {
                "command": command,
                "cwd": str(cwd),
                "returncode": 0,
                "stdout_tail": "",
                "stderr_tail": "",
                "clean_pythonpath": clean_pythonpath,
            }
        )
        if command[1:4] == ["-m", "pip", "wheel"]:
            wheelhouse = Path(command[-1])
            wheelhouse.mkdir(parents=True, exist_ok=True)
            (wheelhouse / wheel_path.name).write_bytes(wheel_path.read_bytes())
        if " venv " in command_text and missing_artifact == "console_script":
            scripts = run_dir / "venv" / "Scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "python.exe").write_text("python", encoding="utf-8")
        stdout = "0.10.0\n" if "import vivado_agent_mcp" in command_text else ""
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "stdout_tail": stdout,
            "stderr_tail": "",
            "clean_pythonpath": clean_pythonpath,
        }

    monkeypatch.setattr(clean_install_smoke, "_run_command", fake_run_command)

    report = clean_install_smoke.run_clean_install_smoke(
        workspace=workspace,
        run_dir=run_dir,
        python_exe=Path(sys.executable),
        include_vivado_probe=False,
        probe_timeout_s=60,
        wheel_path=wheel_path,
        expected_wheel_sha256=expected_sha256,
    )

    assert report["status"] == "BLOCK"
    assert report["wheel_source"] == "supplied_dist"
    assert report["wheel_path"] == str(wheel_path)
    assert report["expected_wheel_sha256"] == expected_sha256
    assert report["wheel_sha256"] == expected_sha256
    assert report["wheel_sha256_verified"] is True
    assert report["package"]["wheel_version"] == "0.10.0"
    assert report["install_wheel_path"] == str(run_dir / "wheelhouse" / wheel_path.name)
    assert report["install_wheel_sha256"] == expected_sha256
    assert report["evidence_state"]["wheel_artifact"] == "PASS"
    assert report["evidence_state"]["wheel_integrity"] == "PASS"
    assert report["evidence_state"]["wheel_version_identity"] == "PASS"
    assert report["evidence_state"][missing_artifact] == "BLOCK"
    assert next(check for check in report["checks"] if check["id"] == missing_artifact)["status"] == "BLOCK"


def test_clean_install_smoke_blocks_wheel_version_mismatch_before_execution(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    wheel_path = tmp_path / "dist" / "vivado_agent_mcp-0.10.0-py3-none-any.whl"
    _write_fake_wheel(wheel_path, version="9.9.9")

    monkeypatch.setattr(
        clean_install_smoke,
        "_run_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("version mismatch must block before command execution")),
    )

    report = clean_install_smoke.run_clean_install_smoke(
        workspace=workspace,
        run_dir=run_dir,
        python_exe=Path(sys.executable),
        include_vivado_probe=False,
        probe_timeout_s=60,
        wheel_path=wheel_path,
        expected_wheel_sha256=clean_install_smoke.sha256_file(wheel_path),
    )

    version_check = next(check for check in report["checks"] if check["id"] == "wheel_version_identity")
    assert report["status"] == "BLOCK"
    assert version_check["status"] == "BLOCK"
    assert report["package"]["wheel_version"] == "9.9.9"
    assert report["package"]["version_verified"] is False
    assert report["source_wheel_pair_id"] == ""
    assert report["commands"] == []


def test_clean_install_smoke_blocks_snapshot_mutation_before_pip_install(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    wheel_path = tmp_path / "dist" / "vivado_agent_mcp-0.10.0-py3-none-any.whl"
    _write_fake_wheel(wheel_path)
    observed: list[list[str]] = []

    def fake_run_command(command, *, cwd, commands: list, timeout_s, clean_pythonpath=False):
        observed.append(list(command))
        command_text = " ".join(command)
        commands.append({"command": command, "cwd": str(cwd), "returncode": 0, "stdout_tail": "", "stderr_tail": ""})
        if command[1:4] == ["-m", "pip", "wheel"]:
            wheelhouse = Path(command[-1])
            wheelhouse.mkdir(parents=True, exist_ok=True)
            (wheelhouse / wheel_path.name).write_bytes(wheel_path.read_bytes())
        if " venv " in command_text:
            scripts = run_dir / "venv" / "Scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "python.exe").write_text("python", encoding="utf-8")
            (scripts / "vivado-agent-mcp.exe").write_text("script", encoding="utf-8")
            with (run_dir / "wheelhouse" / wheel_path.name).open("ab") as handle:
                handle.write(b"tampered-before-install")
        return {"returncode": 0, "stdout": "", "stderr": "", "stdout_tail": "", "stderr_tail": "", "clean_pythonpath": clean_pythonpath}

    monkeypatch.setattr(clean_install_smoke, "_run_command", fake_run_command)

    report = clean_install_smoke.run_clean_install_smoke(
        workspace=workspace,
        run_dir=run_dir,
        python_exe=Path(sys.executable),
        include_vivado_probe=False,
        probe_timeout_s=60,
        wheel_path=wheel_path,
        expected_wheel_sha256=clean_install_smoke.sha256_file(wheel_path),
    )

    preinstall = next(check for check in report["checks"] if check["id"] == "wheel_pre_install_integrity")
    assert report["status"] == "BLOCK"
    assert preinstall["status"] == "BLOCK"
    assert not any(command[1:4] == ["-m", "pip", "install"] for command in observed)


def test_clean_install_smoke_stops_after_built_wheel_hash_mismatch(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    observed: list[list[str]] = []

    def fake_run_command(command, *, cwd, commands: list, timeout_s, clean_pythonpath=False):
        observed.append(list(command))
        wheelhouse = Path(command[-1])
        wheelhouse.mkdir(parents=True, exist_ok=True)
        _write_fake_wheel(wheelhouse / "vivado_agent_mcp-0.10.0-py3-none-any.whl")
        commands.append({"command": command, "cwd": str(cwd), "returncode": 0, "stdout_tail": "", "stderr_tail": ""})
        return {"returncode": 0, "stdout": "", "stderr": "", "stdout_tail": "", "stderr_tail": "", "clean_pythonpath": clean_pythonpath}

    monkeypatch.setattr(clean_install_smoke, "_run_command", fake_run_command)

    report = clean_install_smoke.run_clean_install_smoke(
        workspace=workspace,
        run_dir=run_dir,
        python_exe=Path(sys.executable),
        include_vivado_probe=False,
        probe_timeout_s=60,
        expected_wheel_sha256="0" * 64,
    )

    integrity = next(check for check in report["checks"] if check["id"] == "wheel_integrity")
    assert report["status"] == "BLOCK"
    assert integrity["status"] == "BLOCK"
    assert len(observed) == 1


def test_clean_install_smoke_blocks_dirty_source_before_snapshot_build(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    monkeypatch.setattr(
        clean_install_smoke,
        "source_identity",
        lambda workspace: {"available": True, "clean": False, "identity_id": "a" * 64},
    )

    def fake_run_command(command, *, cwd, commands: list, timeout_s, clean_pythonpath=False):
        command_text = " ".join(command)
        commands.append({"command": command, "cwd": str(cwd), "returncode": 0, "stdout_tail": "", "stderr_tail": ""})
        if command[1:4] == ["-m", "pip", "wheel"]:
            wheelhouse = Path(command[-1])
            wheelhouse.mkdir(parents=True, exist_ok=True)
            _write_fake_wheel(wheelhouse / "vivado_agent_mcp-0.10.0-py3-none-any.whl")
        if " venv " in command_text:
            scripts = run_dir / "venv" / "Scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "python.exe").write_text("python", encoding="utf-8")
            (scripts / "vivado-agent-mcp.exe").write_text("script", encoding="utf-8")
        stdout = ""
        if "import vivado_agent_mcp" in command_text:
            stdout = "0.10.0\n"
        if command[-1] == "--help":
            stdout = "vivado-agent-mcp doctor selftest\n"
        if command[-1] == "--version":
            stdout = "0.10.0\n"
        if " doctor " in command_text:
            stdout = '{"status":"READY","package":{"name":"vivado-agent-mcp"},"checks":[]}'
        if " selftest " in command_text:
            stdout = '{"status":"PASS","validation_scope":"mcp_stdio_contract","hardware_validation":{"status":"NOT_VALIDATED"}}'
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "stdout_tail": stdout,
            "stderr_tail": "",
            "clean_pythonpath": clean_pythonpath,
        }

    monkeypatch.setattr(clean_install_smoke, "_run_command", fake_run_command)

    report = clean_install_smoke.run_clean_install_smoke(
        workspace=workspace,
        run_dir=run_dir,
        python_exe=Path(sys.executable),
        include_vivado_probe=False,
        probe_timeout_s=60,
    )

    assert report["status"] == "BLOCK"
    assert report["source_wheel_provenance_verified"] is False
    assert report["source_wheel_pair_id"] == ""
    assert next(check for check in report["checks"] if check["id"] == "immutable_source_snapshot")["status"] == "BLOCK"
    assert report["commands"] == []


def test_clean_install_report_derives_completed_wheel_evidence_on_early_failure(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    wheel_path = tmp_path / "dist" / "vivado_agent_mcp-0.10.0-py3-none-any.whl"
    _write_fake_wheel(wheel_path)
    wheel_sha256 = clean_install_smoke.sha256_file(wheel_path)
    monkeypatch.setattr(
        clean_install_smoke,
        "source_identity",
        lambda workspace: {"available": True, "clean": True, "identity_id": "a" * 64},
    )

    report = clean_install_smoke._report(
        workspace,
        tmp_path / "run",
        Path(sys.executable),
        False,
        60,
        [
            {"id": "wheel_integrity", "status": "PASS", "message": "", "data": {}},
            {"id": "source_wheel_provenance", "status": "NOT_VERIFIED", "message": "", "data": {}},
            {"id": "wheel_version_identity", "status": "PASS", "message": "", "data": {}},
            {"id": "wheel_dependencies", "status": "BLOCK", "message": "", "data": {}},
        ],
        [],
        wheel_path=wheel_path,
        expected_wheel_sha256=wheel_sha256,
        wheel_source="supplied_dist",
        wheel_package_version="0.10.0",
    )

    assert report["status"] == "BLOCK"
    assert report["wheel_sha256_verified"] is True
    assert report["package"]["version_verified"] is True
    assert report["source_wheel_provenance_verified"] is False
    assert report["release_evidence_ready"] is False
    assert report["evidence_state"]["wheel_dependencies"] == "BLOCK"


def test_clean_install_report_derives_snapshot_and_hash_lock_state_on_install_failure(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    wheel_path = tmp_path / "dist" / "vivado_agent_mcp-0.10.0-py3-none-any.whl"
    install_path = tmp_path / "wheelhouse" / wheel_path.name
    _write_fake_wheel(wheel_path)
    install_path.parent.mkdir()
    install_path.write_bytes(wheel_path.read_bytes())
    wheel_sha256 = clean_install_smoke.sha256_file(wheel_path)
    monkeypatch.setattr(clean_install_smoke, "source_identity", lambda workspace: {"available": False, "clean": False})

    report = clean_install_smoke._report(
        workspace,
        tmp_path / "run",
        Path(sys.executable),
        False,
        60,
        [
            {"id": "wheel_integrity", "status": "PASS", "message": "", "data": {}},
            {"id": "source_wheel_provenance", "status": "NOT_VERIFIED", "message": "", "data": {}},
            {"id": "wheel_version_identity", "status": "PASS", "message": "", "data": {}},
            {
                "id": "wheel_install_snapshot",
                "status": "PASS",
                "message": "",
                "data": {"install_wheel_path": str(install_path), "install_sha256": wheel_sha256},
            },
            {"id": "wheel_hash_lock", "status": "PASS", "message": "", "data": {}},
            {"id": "wheel_install", "status": "BLOCK", "message": "", "data": {}},
        ],
        [],
        wheel_path=wheel_path,
        expected_wheel_sha256=wheel_sha256,
        wheel_source="supplied_dist",
        wheel_package_version="0.10.0",
    )

    assert report["install_wheel_path"] == str(install_path)
    assert report["install_wheel_sha256"] == wheel_sha256
    assert report["pip_hashes_enforced"] is True


def test_clean_install_report_keeps_pair_but_withholds_release_evidence_after_block(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    wheel_path = tmp_path / "dist" / "vivado_agent_mcp-0.10.0-py3-none-any.whl"
    _write_fake_wheel(wheel_path)
    wheel_sha256 = clean_install_smoke.sha256_file(wheel_path)
    clean_source = {"available": True, "clean": True, "identity_id": "a" * 64}
    monkeypatch.setattr(clean_install_smoke, "source_identity", lambda workspace: clean_source)

    report = clean_install_smoke._report(
        workspace,
        tmp_path / "run",
        Path(sys.executable),
        False,
        60,
        [
            {"id": "wheel_integrity", "status": "PASS", "message": "", "data": {}},
            {
                "id": "source_wheel_provenance",
                "status": "PASS",
                "message": "",
                "data": {"source_identity": clean_source},
            },
            {"id": "wheel_version_identity", "status": "PASS", "message": "", "data": {}},
            {"id": "wheel_install", "status": "BLOCK", "message": "", "data": {}},
        ],
        [],
        wheel_path=wheel_path,
        expected_wheel_sha256=wheel_sha256,
        wheel_source="built_from_git_snapshot",
        wheel_package_version="0.10.0",
    )

    assert report["status"] == "BLOCK"
    assert len(report["source_wheel_pair_id"]) == 64
    assert report["release_evidence_id"] == ""
    assert report["release_evidence_ready"] is False


def test_mcp_config_examples_are_valid_json() -> None:
    examples = Path(__file__).resolve().parents[1] / "examples"
    for path in [examples / "mcp-config-full.json", examples / "mcp-config-minimal.json"]:
        data = json.loads(path.read_text(encoding="utf-8"))
        server = data["mcpServers"]["vivado-agent"]
        assert server["command"].endswith("/Scripts/vivado-agent-mcp.exe")
        assert server["args"] == []
        payload = json.dumps(server, ensure_ascii=False)
        assert "doctor" not in payload
        assert "selftest" not in payload
        assert "agent_scenario_runner" not in payload
        assert "test_use" not in payload


def _write_fake_wheel(path: Path, *, version: str = "0.10.0") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = f"Metadata-Version: 2.4\nName: vivado-agent-mcp\nVersion: {version}\n\n"
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"vivado_agent_mcp-{version}.dist-info/METADATA", metadata)
