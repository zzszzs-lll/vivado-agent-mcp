import hashlib
import json
import os
from pathlib import Path

import pytest

from bitstream_fixture import write_test_bitstream, write_test_design_execution_identity
from vivado_agent_mcp.tools import VivadoToolService
from vivado_agent_mcp.vivado.artifacts import collect_artifacts
from vivado_agent_mcp.vivado.wire import encode_wire_list
from vivado_agent_mcp.vivado.hardware import (
    close_hardware_manager_command,
    connect_hw_server_command,
    detect_hardware_environment,
    disconnect_hw_server_command,
    get_hw_device_status_command,
    list_hw_devices_command,
    list_hw_targets_command,
    open_hardware_manager_command,
    open_hw_target_command,
    parse_hw_devices,
    parse_hw_targets,
    parse_hardware_error_code,
    parse_hardware_messages,
    parse_programming_result,
    program_hw_device_command,
    select_hw_device_command,
    select_programming_artifacts_from_manifest,
    validate_bitstream_path,
)
from vivado_agent_mcp.vivado.wire import encode_wire_row


class FakeSession:
    def __init__(self, raw: str = "", ok: bool = True, raws: list[str] | None = None) -> None:
        self.commands: list[str] = []
        self.raw = raw
        self.ok = ok
        self.raws = list(raws or [])

    def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
        self.commands.append(command)
        raw = self.raws.pop(0) if self.raws else self.raw
        return {"ok": self.ok, "raw": raw}


def test_detect_hardware_environment_reports_hw_server_and_xsdb(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "Vivado" / "2021.2" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("vivado.bat", "hw_server.bat", "xsdb.bat"):
        (bin_dir / name).write_text("@echo off\necho fake\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(bin_dir / "vivado.bat"))

    result = detect_hardware_environment(str(bin_dir / "vivado.bat"))

    assert result["ok"] is True
    assert result["tools"]["vivado"]["available"] is True
    assert result["tools"]["hw_server"]["available"] is True
    assert result["tools"]["xsdb"]["available"] is True
    assert result["hardware_manager_tcl"] is True
    assert result["hardware_tool_tier"] == "hardware_safe_detector"
    assert result["server_policy"]["server_policy_env"] == "VIVADO_AGENT_MCP_HARDWARE_MODE"
    assert result["server_policy"]["server_hardware_mode"] == "no_board"


def test_hardware_manager_commands_are_local_and_do_not_scan_network() -> None:
    assert open_hardware_manager_command() == "open_hw_manager"
    assert close_hardware_manager_command() == "close_hw_manager"
    assert disconnect_hw_server_command() == "disconnect_hw_server"

    connect = connect_hw_server_command(host="localhost", port=3121)
    targets = list_hw_targets_command()
    devices = list_hw_devices_command()

    assert connect == "connect_hw_server -url {localhost:3121}"
    assert "scan" not in connect.lower()
    assert "get_hw_targets -quiet *" in targets
    assert "::vivado_agent_mcp_wire_row" in targets
    assert "target $target is_open $is_open" in targets
    assert "get_hw_devices -quiet *" in devices
    assert "programmed [get_property IS_PROGRAMMED $device]" in devices


def test_target_device_select_and_status_commands() -> None:
    open_target = open_hw_target_command(target="localhost:3121/xilinx_tcf/Digilent/210308ABC")
    select_by_name = select_hw_device_command(device="xc7a35t_0")
    select_by_part = select_hw_device_command(part="xc7a35tcpg236-1")
    select_by_index = select_hw_device_command(index=0)
    status = get_hw_device_status_command(device="xc7a35t_0")

    assert "open_hw_target [lindex $targets 0]" in open_target
    assert "get_hw_targets -quiet {localhost:3121/xilinx_tcf/Digilent/210308ABC}" in open_target
    assert "get_hw_devices -quiet {xc7a35t_0}" in select_by_name
    assert "PART $candidate" in select_by_part
    assert "lindex [get_hw_devices -quiet *] 0" in select_by_index
    assert "PROGRAM.FILE" in status
    assert "PROBES.FILE" in status
    assert "IS_PROGRAMMED" in status


def test_program_hw_device_command_sets_bitstream_and_optional_ltx(tmp_path: Path) -> None:
    bit = tmp_path / "build out" / "top$设计.bit"
    ltx = tmp_path / "build out" / "top.ltx"
    bit.parent.mkdir()
    write_test_bitstream(bit)
    ltx.write_text("ltx", encoding="utf-8")

    command = program_hw_device_command(bitstream_path=str(bit), ltx_path=str(ltx), device="xc7a35t_0")

    assert f"set_property PROGRAM.FILE {{{bit}}} $device_obj" in command
    assert f"set_property PROBES.FILE {{{ltx}}} $device_obj" in command
    assert "program_hw_devices $device_obj" in command
    assert "refresh_hw_device $device_obj" in command


def test_validate_bitstream_path_rejects_missing_or_wrong_extension(tmp_path: Path) -> None:
    txt = tmp_path / "top.txt"
    txt.write_text("not bit", encoding="utf-8")

    with pytest.raises(ValueError, match=".bit"):
        validate_bitstream_path(txt)
    with pytest.raises(FileNotFoundError):
        validate_bitstream_path(tmp_path / "missing.bit")


def test_manifest_selection_prefers_export_paths_and_optional_ltx(tmp_path: Path) -> None:
    bit = tmp_path / "artifacts" / "top.bit"
    ltx = tmp_path / "artifacts" / "top.ltx"
    bit.parent.mkdir()
    bit.write_text("bit", encoding="utf-8")
    ltx.write_text("ltx", encoding="utf-8")
    manifest = {
        "artifacts": [
            {"category": "report", "export_path": str(tmp_path / "timing.rpt")},
            {"category": "bitstream", "export_path": str(bit)},
            {"category": "debug_probes", "export_path": str(ltx)},
        ]
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    selected = select_programming_artifacts_from_manifest(manifest_path)

    assert selected["bitstream_path"] == str(bit)
    assert selected["ltx_path"] == str(ltx)


def test_manifest_selection_fails_when_bitstream_missing(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"artifacts": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="bitstream"):
        select_programming_artifacts_from_manifest(manifest_path)


def test_hardware_parsers_extract_targets_devices_and_errors() -> None:
    targets = parse_hw_targets(encode_wire_row({'target': 'localhost:3121/xilinx_tcf/Digilent/abc', 'is_open': '1', 'state': 'open'}))
    devices = parse_hw_devices(encode_wire_row({'device': 'xc7a35t_0', 'part': 'xc7a35tcpg236-1', 'programmed': '0', 'probes_file': 'top.ltx'}))
    program = parse_programming_result(encode_wire_row({'device': 'xc7a35t_0', 'part': 'xc7a35tcpg236-1', 'programmed': '1', 'program_file': 'top.bit'}))
    messages = parse_hardware_messages("ERROR: [Labtools 27-3164] End of startup status: LOW\nWARNING: example")

    assert targets["targets"][0]["is_open"] is True
    assert devices["devices"][0]["part"] == "xc7a35tcpg236-1"
    assert devices["devices"][0]["programmed"] is False
    assert program["status"] == "PROGRAMMED"
    assert messages["counts"]["ERROR"] == 1
    assert parse_hardware_error_code("ERROR: [Labtools 27-3415] No hardware targets exist") == "NO_HW_TARGET"
    assert parse_hardware_error_code("ERROR: no devices found") == "NO_HW_DEVICE"
    assert parse_hardware_error_code("ERROR: connect_hw_server failed") == "HW_SERVER_CONNECT_FAILED"
    assert parse_hardware_error_code("ERROR: program_hw_devices failed") == "HW_PROGRAM_FAILED"


def test_hardware_tools_return_structured_success_and_specific_failures(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIVADO_AGENT_MCP_HARDWARE_MODE", "enabled")
    bit = tmp_path / "top.bit"
    bit.write_text("bit", encoding="utf-8")
    fake = FakeSession(raw=encode_wire_row({'device': 'xc7a35t_0', 'part': 'xc7a35tcpg236-1', 'programmed': '1', 'program_file': 'top.bit'}))
    service = VivadoToolService(session=fake)

    blocked = service.call("program_hw_device", {"bitstream_path": str(bit), "device": "xc7a35t_0"})
    result = service.call(
        "program_hw_device",
        {
            "bitstream_path": str(bit),
            "device": "xc7a35t_0",
            "hardware_intent": "exercise explicit programming gate in a fake session",
            "confirm": "PROGRAM_FPGA",
            "board_fingerprint": "device=xc7a35t_0|part=xc7a35tcpg236-1",
            "expected_bitstream_sha256": hashlib.sha256(bit.read_bytes()).hexdigest(),
            "hardware_mode": "enabled",
        },
    )

    assert blocked["ok"] is False
    assert blocked["error_code"] == "HARDWARE_INTENT_REQUIRED"
    assert fake.commands
    assert result["ok"] is True
    assert result["tool"] == "program_hw_device"
    assert result["data"]["status"] == "PROGRAMMED"
    assert "program_hw_devices" in fake.commands[-1]

    missing = service.call("program_hw_device", {"bitstream_path": str(tmp_path / "missing.bit")})
    assert missing["ok"] is False
    assert missing["error_code"] == "BITSTREAM_NOT_FOUND"


def test_no_board_mode_blocks_hardware_manager_tools_by_default() -> None:
    fake = FakeSession(raw="target=localhost|is_open=1|state=open")
    service = VivadoToolService(session=fake)

    for tool in [
        "open_hardware_manager",
        "close_hardware_manager",
        "connect_hw_server",
        "disconnect_hw_server",
        "list_hw_targets",
        "open_hw_target",
        "close_hw_target",
        "list_hw_devices",
        "select_hw_device",
        "get_hw_device_status",
    ]:
        result = service.call(tool, {})
        assert result["ok"] is False
        assert result["error_code"] == "HARDWARE_MODE_DISABLED"
        assert result["data"]["hardware_mode"] == "no_board"
        assert result["data"]["server_policy"]["server_hardware_mode"] == "no_board"
        assert result["data"]["hardware_validation"]["status"] == "NOT_VALIDATED"
        assert result["next_actions"][0]["tool"] == "detect_hardware_environment"

    assert fake.commands == []


def test_server_policy_blocks_agent_supplied_hardware_mode() -> None:
    fake = FakeSession(raw="target=localhost|is_open=1|state=open")
    service = VivadoToolService(session=fake)

    result = service.call("list_hw_targets", {"hardware_mode": "enabled"})

    assert result["ok"] is False
    assert result["error_code"] == "HARDWARE_MODE_DISABLED"
    assert result["data"]["hardware_mode"] == "enabled"
    assert result["data"]["server_hardware_mode"] == "no_board"
    assert fake.commands == []


def test_get_hardware_messages_is_log_readonly() -> None:
    fake = FakeSession(raw="ERROR: [Labtools 27-3164] End of startup status: LOW")
    service = VivadoToolService(session=fake)

    result = service.call("get_hardware_messages", {})

    assert result["ok"] is True
    assert result["data"]["hardware_tool_tier"] == "hardware_log_readonly"
    assert result["data"]["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert result["data"]["server_policy"]["server_hardware_mode"] == "no_board"
    assert "connect_hw_server" not in fake.commands[0]
    assert "program_hw_devices" not in fake.commands[0]


def test_hardware_tool_results_mark_real_board_validation_as_deferred(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "Vivado" / "2021.2" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("vivado.bat", "hw_server.bat", "xsdb.bat"):
        (bin_dir / name).write_text("@echo off\necho fake\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(bin_dir / "vivado.bat"))

    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    service = VivadoToolService(session=_artifact_fake_session(project_dir, run_dir))
    environment = service.call("detect_hardware_environment", {"vivado_path": str(bin_dir / "vivado.bat")})
    no_target = service.call("list_hw_targets", {})
    missing_bit = service.call("program_hw_device", {"bitstream_path": str(tmp_path / "missing.bit")})
    missing_manifest = service.call("program_from_artifact_manifest", {"manifest_path": str(tmp_path / "missing_manifest.json")})
    empty_manifest_path = project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json"
    empty_manifest_path.parent.mkdir(parents=True)
    empty_manifest_path.write_text(json.dumps({"artifacts": []}), encoding="utf-8")
    manifest_without_bitstream = service.call("program_from_artifact_manifest", {"manifest_path": str(empty_manifest_path)})

    assert environment["ok"] is True
    assert environment["data"]["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert environment["data"]["hardware_validation"]["real_board_required"] is True
    assert environment["data"]["server_policy"]["hardware_tools_disabled_by_default"] is True
    assert no_target["data"]["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert no_target["error_code"] == "HARDWARE_MODE_DISABLED"
    assert missing_bit["data"]["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert missing_bit["error_code"] == "BITSTREAM_NOT_FOUND"
    assert missing_manifest["data"]["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert missing_manifest["error_code"] == "MANIFEST_NOT_FOUND"
    assert manifest_without_bitstream["data"]["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert manifest_without_bitstream["error_code"] == "HARDWARE_INTENT_REQUIRED"
    assert "manifest_sha256" in manifest_without_bitstream["data"]["missing"]


def test_program_from_artifact_manifest_uses_manifest_bit_and_ltx(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIVADO_AGENT_MCP_HARDWARE_MODE", "enabled")
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    bit = run_dir / "top.bit"
    ltx = run_dir / "top.ltx"
    write_test_bitstream(bit)
    ltx.write_text("ltx", encoding="utf-8")
    _write_run_markers(run_dir, [bit, ltx])
    manifest = collect_artifacts(
        project_dir=project_dir,
        run_dir=run_dir,
        run_name="impl_1",
        run_context=_artifact_context(project_dir, run_dir),
    )
    manifest_path = Path(manifest["manifest_path"])
    exported_bit = next(Path(item["export_path"]) for item in manifest["artifacts"] if item["category"] == "bitstream")
    exported_ltx = next(Path(item["export_path"]) for item in manifest["artifacts"] if item["category"] == "debug_probes")
    fake = FakeSession(
        raws=[
            _artifact_context_raw(project_dir, run_dir),
            encode_wire_row({'device': 'xc7a35t_0', 'part': 'xc7a35tcpg236-1', 'programmed': '0'}),
            encode_wire_row({'device': 'xc7a35t_0', 'part': 'xc7a35tcpg236-1', 'programmed': '1', 'program_file': 'top.bit'}),
        ]
    )
    fake.design_execution_identity = manifest["design_execution_identity"]
    service = VivadoToolService(session=fake)

    result = service.call(
        "program_from_artifact_manifest",
        {
            "manifest_path": str(manifest_path),
            "hardware_intent": "exercise explicit manifest programming gate in a fake session",
            "confirm": "PROGRAM_FPGA",
            "board_fingerprint": "device=xc7a35t_0|part=xc7a35tcpg236-1",
            "expected_bitstream_sha256": hashlib.sha256(exported_bit.read_bytes()).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "hardware_mode": "enabled",
        },
    )

    assert result["ok"] is True
    assert result["data"]["artifacts"]["bitstream_path"] == str(exported_bit)
    assert f"set_property PROBES.FILE {{{exported_ltx}}} $device_obj" in fake.commands[-1]
    assert len(fake.commands) == 3


def test_program_from_artifact_manifest_rejects_unvalidated_manifest_before_hardware_preflight(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VIVADO_AGENT_MCP_HARDWARE_MODE", "enabled")
    project_dir = tmp_path / "project"
    run_dir = project_dir / "demo.runs" / "impl_1"
    manifest_path = project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json"
    run_dir.mkdir(parents=True)
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps({"schema_version": 3, "artifacts": []}), encoding="utf-8")
    fake = FakeSession(raw=_artifact_context_raw(project_dir, run_dir))
    fake.design_execution_identity = write_test_design_execution_identity(project_dir)
    service = VivadoToolService(session=fake)

    result = service.call(
        "program_from_artifact_manifest",
        {
            "manifest_path": str(manifest_path),
            "hardware_intent": "program a reviewed artifact",
            "confirm": "PROGRAM_FPGA",
            "board_fingerprint": "device=xc7a35t_0|part=xc7a35tcpg236-1",
            "expected_bitstream_sha256": "0" * 64,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "hardware_mode": "enabled",
        },
    )

    assert result["ok"] is False
    assert result["error_code"] == "ARTIFACT_MANIFEST_REJECTED"
    assert result["data"]["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert len(fake.commands) == 1
    assert "get_hw_devices" not in fake.commands[0]
    assert "program_hw_devices" not in fake.commands[0]


def test_programming_gate_blocks_manifest_changed_after_strict_validation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VIVADO_AGENT_MCP_HARDWARE_MODE", "enabled")
    bitstream = tmp_path / "top.bit"
    manifest_path = tmp_path / "manifest.json"
    bitstream.write_bytes(b"bitstream")
    manifest_path.write_text('{"version": 1}\n', encoding="utf-8")
    validated_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest_path.write_text('{"version": 2}\n', encoding="utf-8")
    current_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    fake = FakeSession(raw="must not reach hardware preflight")
    service = VivadoToolService(session=fake)

    result = service._hardware_programming_gate(
        tool="program_from_artifact_manifest",
        args={
            "hardware_intent": "program a reviewed artifact",
            "confirm": "PROGRAM_FPGA",
            "board_fingerprint": "device=xc7a35t_0|part=xc7a35tcpg236-1",
            "expected_bitstream_sha256": hashlib.sha256(bitstream.read_bytes()).hexdigest(),
            "manifest_sha256": current_manifest_sha256,
            "hardware_mode": "enabled",
        },
        bitstream=bitstream,
        manifest_path=manifest_path,
        validated_manifest_sha256=validated_manifest_sha256,
    )

    assert result["ok"] is False
    assert result["error_code"] == "MANIFEST_CHANGED_AFTER_VALIDATION"
    assert fake.commands == []


def _artifact_context(project_dir: Path, run_dir: Path) -> dict[str, object]:
    return {
        "project_name": "demo",
        "project_dir": str(project_dir),
        "project_part": "xc7a35tcpg236-1",
        "run_dir": str(run_dir),
        "run_srcset": "sources_1",
        "run_top": "top",
        "run_status": "write_bitstream Complete!",
        "run_progress": "100%",
        "run_needs_refresh": "0",
        "expected_bitstream_path": str(run_dir / "top.bit"),
        "run_bitstream_files": [str(run_dir / "top.bit")],
        "write_bitstream_step_enabled": "1",
        "write_bitstream_step_status": "Complete!",
        "session_generation_id": "hardware-test-generation",
        "design_execution_identity": write_test_design_execution_identity(project_dir),
    }


def _artifact_context_raw(project_dir: Path, run_dir: Path) -> str:
    context = _artifact_context(project_dir, run_dir)
    context["run_bitstream_files"] = encode_wire_list(context["run_bitstream_files"])
    return "\n".join(
        f"{key}={value}"
        for key, value in context.items()
        if key != "design_execution_identity"
    )


def _artifact_fake_session(project_dir: Path, run_dir: Path) -> FakeSession:
    session = FakeSession(raw=_artifact_context_raw(project_dir, run_dir))
    session.design_execution_identity = write_test_design_execution_identity(project_dir)
    return session


def _write_run_markers(run_dir: Path, artifacts: list[Path]) -> None:
    earliest = min(path.stat().st_mtime_ns for path in artifacts)
    latest = max(path.stat().st_mtime_ns for path in artifacts)
    begin_marker = run_dir / ".vivado.begin.rst"
    end_marker = run_dir / ".vivado.end.rst"
    begin_marker.write_text("run started\n", encoding="utf-8")
    end_marker.write_text("run completed\n", encoding="utf-8")
    os.utime(begin_marker, ns=(max(1, earliest - 2_000_000_000), max(1, earliest - 2_000_000_000)))
    os.utime(end_marker, ns=(latest + 2_000_000_000, latest + 2_000_000_000))
