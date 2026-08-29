import subprocess
from pathlib import Path

import pytest

from vivado_agent_mcp.tools import VivadoToolService
import vivado_agent_mcp.vivado.runtime_cache as runtime_cache_module
from vivado_agent_mcp.vivado.runtime_cache import (
    _is_mount_point,
    _parse_posix_server_processes,
    _parse_windows_server_processes,
    clean_runtime_cache,
    detect_active_vivado_processes,
    get_runtime_cache_status,
)
from vivado_agent_mcp.vivado.runtime_identity import ensure_runtime_identity, inspect_runtime_identity


def test_mount_point_check_falls_back_when_pathlib_is_unsupported(tmp_path: Path, monkeypatch) -> None:
    def unsupported_is_mount(_path: Path) -> bool:
        raise NotImplementedError("unsupported on this Python/Windows combination")

    monkeypatch.setattr(Path, "is_mount", unsupported_is_mount)
    monkeypatch.setattr(runtime_cache_module.os.path, "ismount", lambda _path: False)

    assert _is_mount_point(tmp_path) is False


def test_runtime_cache_status_classifies_temporary_files(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))

    status = get_runtime_cache_status(runtime_dir)

    assert status["runtime_dir"] == str(runtime_dir.resolve())
    assert status["exists"] is True
    assert status["file_count"] == 8
    assert status["runtime_identity"]["status"] == "READY"
    assert status["categories"]["runtime_identity"]["file_count"] == 1
    assert status["categories"]["vivado_xil"]["file_count"] == 1
    assert status["categories"]["bootstrap_tcl"]["file_count"] == 1
    assert status["categories"]["java_jni_tmp"]["file_count"] == 1
    assert status["categories"]["java_perfdata"]["file_count"] == 1
    assert status["categories"]["xsim_wave_tmp"]["file_count"] == 1
    assert status["categories"]["workflow_trace"]["file_count"] == 1
    assert status["categories"]["unknown"]["file_count"] == 1
    assert status["cleanup_candidates"]["file_count"] == 5
    assert status["cleanup_candidates"]["dir_count"] >= 1
    assert status["largest_files"][0]["size"] >= status["largest_files"][-1]["size"]


def test_runtime_cache_status_for_missing_dir_still_returns_next_action(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "missing_runtime"
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))
    status = get_runtime_cache_status(runtime_dir)

    assert status["exists"] is False
    assert status["cleanup_candidates"]["file_count"] == 0
    assert status["next_actions"][0]["tool"] == "start_session"


def test_runtime_cache_status_explains_server_process_probe_timeout(tmp_path: Path, monkeypatch) -> None:
    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 2))

    monkeypatch.setattr(subprocess, "run", timeout_run)
    runtime_dir = tmp_path / "missing_runtime"
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))

    status = get_runtime_cache_status(runtime_dir)

    assert status["server_processes"][0]["reason"] == "server_process_probe_timeout"
    assert status["process_detection_status"]["status"] == "UNAVAILABLE"
    assert status["process_detection_status"]["cleanup_impact"] == "Real cleanup should stay dry-run or be retried after process detection succeeds."
    assert any(action["tool"] == "get_runtime_cache_status" for action in status["next_actions"])


def test_runtime_cache_clean_dry_run_does_not_delete_files(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))

    result = clean_runtime_cache(runtime_dir, dry_run=True)

    assert result["status"] == "DRY_RUN"
    assert result["planned"]["file_count"] == 5
    assert result["deleted"]["file_count"] == 0
    assert (runtime_dir / "vivado_agent_mcp_12345.tcl").exists()
    assert (runtime_dir / "notes.keep").exists()


def test_runtime_cache_clean_removes_known_temporary_files_only(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))

    dry_run = clean_runtime_cache(runtime_dir, dry_run=True, active_processes=[])
    runtime_id = dry_run["runtime_identity"]["runtime_id"]
    result = clean_runtime_cache(
        runtime_dir,
        dry_run=False,
        runtime_identity=runtime_id,
        plan_sha256=dry_run["plan_sha256"],
        active_processes=[],
    )

    assert result["status"] == "CLEANED"
    assert result["deleted"]["file_count"] == 5
    assert not (runtime_dir / "vivado_agent_mcp_12345.tcl").exists()
    assert not (runtime_dir / ".Xil").exists()
    assert not (runtime_dir / "libzstd-jni123.dll").exists()
    assert not (runtime_dir / "hsperfdata_testuser" / "pid").exists()
    assert not (runtime_dir / "tb_top_behav_1.xilwvdat").exists()
    assert (runtime_dir / "notes.keep").exists()


def test_runtime_cache_clean_can_include_unknown_files_when_explicit(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))
    dry_run = clean_runtime_cache(runtime_dir, dry_run=True, include_unknown=True, active_processes=[])
    runtime_id = dry_run["runtime_identity"]["runtime_id"]

    result = clean_runtime_cache(
        runtime_dir,
        dry_run=False,
        include_unknown=True,
        runtime_identity=runtime_id,
        plan_sha256=dry_run["plan_sha256"],
        execution_intent="clean_runtime_unknown",
        confirm="CLEAN_RUNTIME_UNKNOWN",
        active_processes=[],
    )

    assert result["status"] == "CLEANED"
    assert result["deleted"]["file_count"] == 6
    assert not (runtime_dir / "notes.keep").exists()


def test_runtime_cache_real_cleanup_requires_identity_from_dry_run(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))

    missing = clean_runtime_cache(runtime_dir, dry_run=False, active_processes=[])
    mismatched = clean_runtime_cache(
        runtime_dir,
        dry_run=False,
        runtime_identity="not-the-reviewed-runtime",
        active_processes=[],
    )

    assert missing["status"] == "BLOCK"
    assert missing["reason"] == "runtime_identity_confirmation_required"
    assert mismatched["status"] == "BLOCK"
    assert mismatched["reason"] == "runtime_identity_mismatch"
    assert (runtime_dir / "vivado_agent_mcp_12345.tcl").exists()


def test_runtime_cache_rejects_cleanup_when_dry_run_plan_drifted(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))
    dry_run = clean_runtime_cache(runtime_dir, dry_run=True, active_processes=[])
    (runtime_dir / "new.tmp").write_text("changed after review", encoding="utf-8")

    result = clean_runtime_cache(
        runtime_dir,
        dry_run=False,
        runtime_identity=dry_run["runtime_identity"]["runtime_id"],
        plan_sha256=dry_run["plan_sha256"],
        active_processes=[],
    )

    assert result["status"] == "BLOCK"
    assert result["reason"] == "cleanup_plan_mismatch"
    assert (runtime_dir / "vivado_agent_mcp_12345.tcl").exists()
    assert (runtime_dir / "new.tmp").exists()


def test_runtime_cleanup_revalidates_target_immediately_before_first_delete(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))
    dry_run = clean_runtime_cache(runtime_dir, dry_run=True, active_processes=[])
    original_delete = runtime_cache_module.delete_managed_snapshot
    raced_paths: list[Path] = []

    def inject_drift(root, target, expected):
        target_path = Path(target)
        raced_paths.append(target_path)
        if target_path.is_dir():
            (target_path / "injected-after-prevalidation.tmp").write_text("drift", encoding="utf-8")
        else:
            target_path.write_bytes(target_path.read_bytes() + b"drift")
        return original_delete(root, target, expected)

    monkeypatch.setattr(runtime_cache_module, "delete_managed_snapshot", inject_drift)
    result = clean_runtime_cache(
        runtime_dir,
        dry_run=False,
        runtime_identity=dry_run["runtime_identity"]["runtime_id"],
        plan_sha256=dry_run["plan_sha256"],
        active_processes=[],
    )

    assert result["status"] == "BLOCK"
    assert result["reason"] == "cleanup_plan_drift"
    assert raced_paths
    assert raced_paths[0].exists()


def test_runtime_cache_rejects_unconfigured_root_before_recursive_scan(tmp_path: Path, monkeypatch) -> None:
    configured = tmp_path / "configured" / "runtime"
    victim = tmp_path / "victim"
    victim.mkdir()
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(configured))
    monkeypatch.setattr(
        "vivado_agent_mcp.vivado.runtime_cache._scan_runtime",
        lambda root: (_ for _ in ()).throw(AssertionError("arbitrary root must not be scanned")),
    )
    monkeypatch.setattr(
        "vivado_agent_mcp.vivado.runtime_cache.inspect_runtime_identity",
        lambda root: (_ for _ in ()).throw(AssertionError("arbitrary root marker must not be inspected")),
    )

    status = get_runtime_cache_status(victim)

    assert status["status"] == "BLOCK"
    assert status["reason"] == "runtime_dir_not_configured_root"
    assert status["scan"]["performed"] is False
    assert status["runtime_identity"]["status"] == "SKIPPED"


def test_runtime_cache_rejects_unmarked_arbitrary_directory_even_for_dry_run(tmp_path: Path, monkeypatch) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    unknown = victim / "important.txt"
    unknown.write_text("do not delete", encoding="utf-8")
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(victim))

    result = clean_runtime_cache(victim, dry_run=True, include_unknown=True, active_processes=[])

    assert result["status"] == "BLOCK"
    assert result["reason"] == "runtime_identity_missing"
    assert unknown.read_text(encoding="utf-8") == "do not delete"


def test_runtime_cache_unknown_cleanup_requires_configured_root_and_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    runtime_id = inspect_runtime_identity(runtime_dir)["runtime_id"]

    not_configured = clean_runtime_cache(
        runtime_dir,
        dry_run=False,
        include_unknown=True,
        runtime_identity=runtime_id,
        execution_intent="clean_runtime_unknown",
        confirm="CLEAN_RUNTIME_UNKNOWN",
        active_processes=[],
    )
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))
    missing_intent = clean_runtime_cache(
        runtime_dir,
        dry_run=False,
        include_unknown=True,
        runtime_identity=runtime_id,
        active_processes=[],
    )
    missing_confirm = clean_runtime_cache(
        runtime_dir,
        dry_run=False,
        include_unknown=True,
        runtime_identity=runtime_id,
        execution_intent="clean_runtime_unknown",
        active_processes=[],
    )

    assert not_configured["reason"] == "runtime_dir_not_configured_root"
    assert missing_intent["reason"] == "unknown_cleanup_intent_required"
    assert missing_confirm["reason"] == "unknown_cleanup_confirmation_required"
    assert (runtime_dir / "notes.keep").exists()


def test_runtime_cache_real_cleanup_rejects_forged_marker_outside_configured_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configured_runtime = _write_runtime_fixture(tmp_path / "configured")
    victim = _write_runtime_fixture(tmp_path / "victim")
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(configured_runtime))
    victim_id = inspect_runtime_identity(victim)["runtime_id"]

    result = clean_runtime_cache(
        victim,
        dry_run=False,
        runtime_identity=victim_id,
        active_processes=[],
    )

    assert result["status"] == "BLOCK"
    assert result["reason"] == "runtime_dir_not_configured_root"
    assert result["configured_runtime_dir"] == str(configured_runtime.resolve())
    assert (victim / "vivado_agent_mcp_12345.tcl").exists()


def test_runtime_cache_scan_does_not_follow_junction_like_directories(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    linked = runtime_dir / "linked_cache"
    linked.mkdir()
    (linked / "do-not-scan.tmp").write_text("outside-like", encoding="utf-8")
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(
        "vivado_agent_mcp.vivado.runtime_cache._is_junction",
        lambda path: Path(path).name == "linked_cache",
    )

    status = get_runtime_cache_status(runtime_dir)

    assert status["scan"]["complete"] is False
    assert any(item["reason"] == "junction_not_followed" for item in status["skipped"])
    assert all("do-not-scan.tmp" not in item["path"] for item in status["largest_files"])


def test_runtime_cleanup_plan_binds_file_identity_not_only_size_and_mtime(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr("vivado_agent_mcp.vivado.runtime_cache._stat_file_id", lambda stat: "identity-a")
    first = clean_runtime_cache(runtime_dir, dry_run=True, active_processes=[])
    monkeypatch.setattr("vivado_agent_mcp.vivado.runtime_cache._stat_file_id", lambda stat: "identity-b")
    second = clean_runtime_cache(runtime_dir, dry_run=True, active_processes=[])

    assert first["plan_sha256"] != second["plan_sha256"]


def test_runtime_cache_rejects_repository_root_even_with_forged_temp_files(tmp_path: Path, monkeypatch) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / "vivado_agent_mcp_12345.tcl").write_text("important", encoding="utf-8")
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(repository))

    result = clean_runtime_cache(repository, dry_run=True, active_processes=[])

    assert result["status"] == "BLOCK"
    assert result["reason"] == "runtime_dir_looks_like_repository"
    assert (repository / "vivado_agent_mcp_12345.tcl").exists()


def test_runtime_cache_clean_blocks_project_like_runtime_dir(tmp_path: Path, monkeypatch) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    ensure_runtime_identity(project_dir, workspace_root=tmp_path)
    (project_dir / "demo.xpr").write_text("", encoding="utf-8")
    (project_dir / "vmcp_artifacts").mkdir()
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(project_dir))

    result = clean_runtime_cache(project_dir, dry_run=False, active_processes=[])

    assert result["status"] == "BLOCK"
    assert result["reason"] == "runtime_dir_looks_like_project"
    assert (project_dir / "demo.xpr").exists()
    assert (project_dir / "vmcp_artifacts").exists()


def test_runtime_cache_clean_blocks_when_processes_are_active(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))
    runtime_id = get_runtime_cache_status(runtime_dir)["runtime_identity"]["runtime_id"]

    result = clean_runtime_cache(
        runtime_dir,
        dry_run=False,
        runtime_identity=runtime_id,
        active_processes=[{"name": "vivado.exe", "pid": 1234}],
    )

    assert result["status"] == "BLOCK"
    assert result["reason"] == "vivado_process_active"
    assert result["active_processes"][0]["name"] == "vivado.exe"
    assert (runtime_dir / "vivado_agent_mcp_12345.tcl").exists()


def test_runtime_cache_clean_dry_run_marks_active_process_reason(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))

    result = clean_runtime_cache(
        runtime_dir,
        dry_run=True,
        active_processes=[{"name": "xsim.exe", "pid": "123"}],
    )

    assert result["status"] == "DRY_RUN"
    assert result["will_not_clean_because_active_process"] is True
    assert result["active_processes"] == [{"name": "xsim.exe", "pid": "123"}]


def test_runtime_cache_process_detection_timeout_returns_unavailable_marker(monkeypatch) -> None:
    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 5))

    monkeypatch.setattr(subprocess, "run", timeout_run)

    assert detect_active_vivado_processes() == [
        {"name": "process_detection_unavailable", "pid": "", "reason": "process_probe_timeout"}
    ]


def test_runtime_cache_clean_blocks_real_delete_when_process_detection_times_out(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))
    runtime_id = get_runtime_cache_status(runtime_dir)["runtime_identity"]["runtime_id"]

    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 2))

    monkeypatch.setattr(subprocess, "run", timeout_run)

    result = clean_runtime_cache(runtime_dir, dry_run=False, runtime_identity=runtime_id)

    assert result["status"] == "BLOCK"
    assert result["reason"] == "process_detection_unavailable"
    assert result["active_processes"][0]["reason"] == "process_probe_timeout"
    assert (runtime_dir / "vivado_agent_mcp_12345.tcl").exists()


def test_runtime_cache_tools_return_structured_next_actions(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))
    service = VivadoToolService()

    status = service.call("get_runtime_cache_status", {"runtime_dir": str(runtime_dir)})
    dry_run = service.call("clean_runtime_cache", {"runtime_dir": str(runtime_dir)})

    assert status["ok"] is True
    assert status["tool"] == "get_runtime_cache_status"
    assert status["data"]["cleanup_candidates"]["file_count"] == 5
    assert status["next_actions"][0]["tool"] == "clean_runtime_cache"
    assert dry_run["ok"] is True
    assert dry_run["data"]["status"] == "DRY_RUN"
    assert dry_run["next_actions"][0]["tool"] == "clean_runtime_cache"


def test_runtime_cache_tool_blocks_when_session_is_active(tmp_path: Path) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)

    class ActiveManager:
        def status(self) -> dict:
            return {"ok": True, "connected": True, "process_running": True}

    service = VivadoToolService(manager=ActiveManager())  # type: ignore[arg-type]

    result = service.call("clean_runtime_cache", {"runtime_dir": str(runtime_dir), "dry_run": False})

    assert result["ok"] is False
    assert result["error_code"] == "RUNTIME_SESSION_ACTIVE"
    assert result["next_actions"][0]["tool"] == "stop_session"
    assert (runtime_dir / "vivado_agent_mcp_12345.tcl").exists()


def test_runtime_cache_status_reports_mcp_server_processes(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = _write_runtime_fixture(tmp_path)
    monkeypatch.setenv("VIVADO_AGENT_MCP_RUNTIME_DIR", str(runtime_dir))

    def fake_server_processes(workspace_root=None):
        return [
            {
                "pid": "123",
                "ppid": "1",
                "created_at": "2026-06-01T00:00:00+08:00",
                "command_line": "C:/Python/python.exe -m vivado_agent_mcp",
                "command_line_excerpt": "C:/Python/python.exe -m vivado_agent_mcp",
                "workspace_match": True,
            }
        ]

    monkeypatch.setattr("vivado_agent_mcp.vivado.runtime_cache.detect_vivado_agent_server_processes", fake_server_processes)

    status = get_runtime_cache_status(runtime_dir)

    assert status["server_processes"][0]["pid"] == "123"
    assert status["server_processes"][0]["workspace_match"] is True


def test_runtime_cache_server_process_command_summary_redacts_sensitive_args() -> None:
    workspace = Path("D:/Vivado_Mcp")
    windows = _parse_windows_server_processes(
        (
            '{"ProcessId":123,"ParentProcessId":1,"CreationDate":"20260601000000.000000+480",'
            '"CommandLine":"C:/Python/python.exe -m vivado_agent_mcp --token abc123 '
            '--password=passw0rd api_key=plain D:/Vivado_Mcp"}'
        ),
        workspace,
    )
    posix = _parse_posix_server_processes(
        "123 1 Mon Jun 01 00:00:00 2026 /usr/bin/python -m vivado_agent_mcp --secret swordfish --credential=plain D:/Vivado_Mcp",
        workspace,
    )

    for process in windows + posix:
        summary = process["command_summary"]
        assert "<redacted>" in summary
        assert "abc123" not in summary
        assert "passw0rd" not in summary
        assert "swordfish" not in summary
        assert "credential=plain" not in summary


def _write_runtime_fixture(tmp_path: Path) -> Path:
    runtime_dir = tmp_path / ".vivado_agent_mcp" / "runtime"
    xil_dir = runtime_dir / ".Xil" / "Vivado-1234-PC"
    hsperf_dir = runtime_dir / "hsperfdata_testuser"
    empty_dir = runtime_dir / "empty_dir"
    xil_dir.mkdir(parents=True)
    hsperf_dir.mkdir(parents=True)
    empty_dir.mkdir(parents=True)
    ensure_runtime_identity(runtime_dir, workspace_root=tmp_path)
    (xil_dir / "elab.rtd").write_bytes(b"xil-cache")
    (runtime_dir / "vivado_agent_mcp_12345.tcl").write_text("socket -server test\n", encoding="utf-8")
    (runtime_dir / "libzstd-jni123.dll").write_bytes(b"jni")
    (hsperf_dir / "pid").write_text("123", encoding="utf-8")
    (runtime_dir / "tb_top_behav_1.xilwvdat").write_bytes(b"wave")
    (runtime_dir / "traces").mkdir()
    (runtime_dir / "traces" / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    (runtime_dir / "notes.keep").write_text("keep me", encoding="utf-8")
    return runtime_dir
