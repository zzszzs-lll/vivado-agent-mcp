import hashlib
from pathlib import Path

from fakes import RecordingSession
from vivado_agent_mcp.tools import VivadoToolService
from vivado_agent_mcp.vivado.tcl_policy import classify_tcl
from vivado_agent_mcp.vivado.wire import encode_wire_list, encode_wire_row


def test_tcl_policy_classifies_low_risk_and_gated_operations() -> None:
    assert classify_tcl("version -short")["risk"] == "LOW"
    assert classify_tcl("get_ports -quiet *")["risk"] == "LOW"
    assert classify_tcl("report_timing_summary -return_string")["risk"] == "LOW"
    read_only_query = """
set rows [list]
foreach p [get_ports -quiet *] {
    lappend rows $p
}
puts [join $rows {;}]
""".strip()
    expected = [
        "allow_destructive",
        "allow_external",
        "allow_hardware",
        "allow_project_write",
        "allow_unrestricted",
    ]
    assert classify_tcl(read_only_query)["risk"] == "UNRESTRICTED"
    assert classify_tcl("open_project {demo.xpr}")["required_flags"] == expected
    assert classify_tcl("set_property top top [current_fileset]")["required_flags"] == expected
    assert classify_tcl("file delete -force -- {demo.runs}")["required_flags"] == expected
    assert classify_tcl("program_hw_devices [current_hw_device]")["required_flags"] == expected
    assert classify_tcl("exec cmd /c dir")["required_flags"] == expected


def test_tcl_policy_classifies_dangerous_tcl_with_tabs_and_nested_commands() -> None:
    tabbed_delete = classify_tcl("file\tdelete -force -- {demo.runs}")
    nested_exec = classify_tcl("catch {exec\tcmd /c dir} msg")
    guarded_delete = classify_tcl("if {[file exists {demo.runs}]} { file\tdelete -force -- {demo.runs} }")

    for classification in (tabbed_delete, nested_exec, guarded_delete):
        assert classification["risk"] == "UNRESTRICTED"
        assert classification["required_flags"] == [
            "allow_destructive",
            "allow_external",
            "allow_hardware",
            "allow_project_write",
            "allow_unrestricted",
        ]


def test_tcl_policy_backslash_substitutions_never_receive_low_risk() -> None:
    commands = [
        r"set x [e\170ec cmd /c whoami]",
        r"set x [e\x78ec cmd /c whoami]",
        r"if {1} {e\u0078ec cmd /c whoami}",
        r"catch {fi\154e delete -force -- {demo.runs}}",
        r"set x $::vivado_agent_mcp_auth_secret",
        r"set c e\170ec; $c cmd /c whoami",
    ]

    for command in commands:
        assert classify_tcl(command)["risk"] == "UNRESTRICTED"


def test_tcl_policy_fails_closed_for_dynamic_and_unknown_commands() -> None:
    dynamic = classify_tcl("set c ex; append c ec; $c cmd /c whoami")
    nested_dynamic = classify_tcl("if {1} {$c cmd /c whoami}")
    eval_command = classify_tcl("eval $script")
    unknown = classify_tcl("custom_query_command -quiet")

    for classification in (dynamic, nested_dynamic, eval_command, unknown):
        assert classification["risk"] == "UNRESTRICTED"
        assert classification["required_flags"] == [
            "allow_destructive",
            "allow_external",
            "allow_hardware",
            "allow_project_write",
            "allow_unrestricted",
        ]


def test_unrestricted_tcl_requires_all_gates_and_fixed_confirmation() -> None:
    session = RecordingSession(raw="should-not-run")
    service = VivadoToolService(session=session)
    command = "set c ex; append c ec; $c cmd /c whoami"
    common = {
        "command": command,
        "execution_intent": "execute reviewed unrestricted Tcl",
        "allow_project_write": True,
        "allow_destructive": True,
        "allow_hardware": True,
        "allow_external": True,
        "allow_unrestricted": True,
    }

    missing_confirm = service.call("run_tcl", common)
    disabled = service.call("run_tcl", {**common, "confirm": "EXECUTE_UNRESTRICTED_TCL"})

    assert missing_confirm["ok"] is False
    assert missing_confirm["error_code"] == "TCL_POLICY_BLOCKED"
    assert "confirm" in missing_confirm["data"]["missing"]
    assert disabled["ok"] is False
    assert disabled["error_code"] == "TCL_EXECUTION_DISABLED"
    assert session.commands == []


def test_dynamic_file_subcommand_is_unrestricted_and_not_covered_by_destructive_gate() -> None:
    session = RecordingSession(raw="should-not-run")
    service = VivadoToolService(session=session)
    command = "set sub delete; file $sub -force -- {demo.runs}"

    result = service.call(
        "run_tcl",
        {
            "command": command,
            "execution_intent": "delete generated output",
            "allow_destructive": True,
        },
    )

    assert classify_tcl(command)["risk"] == "UNRESTRICTED"
    assert result["ok"] is False
    assert result["error_code"] == "TCL_POLICY_BLOCKED"
    assert "allow_unrestricted" in result["data"]["missing"]
    assert session.commands == []


def test_raw_tcl_cannot_bypass_hardware_mode_or_dedicated_programming_tool(monkeypatch) -> None:
    session = RecordingSession(raw="device=demo")
    service = VivadoToolService(session=session)
    query_args = {
        "command": "get_hw_devices -quiet",
        "execution_intent": "inspect explicitly enabled hardware",
        "allow_project_write": True,
        "allow_destructive": True,
        "allow_hardware": True,
        "allow_external": True,
        "allow_unrestricted": True,
        "confirm": "EXECUTE_UNRESTRICTED_TCL",
        "hardware_mode": "enabled",
    }

    mode_blocked = service.call("run_tcl", query_args)
    monkeypatch.setenv("VIVADO_AGENT_MCP_HARDWARE_MODE", "enabled")
    query_allowed = service.call("run_tcl", query_args)
    programming_results = []
    for programming_command in (
        "program_hw_devices [current_hw_device]",
        r"program_hw_d\145vices [current_hw_device]",
        r"program_hw_d\x65vices [current_hw_device]",
        r"program_hw_d\u0065vices [current_hw_device]",
    ):
        programming_results.append(service.call(
            "run_tcl",
            {
            "command": programming_command,
            "execution_intent": "program an explicitly selected board",
            "allow_project_write": True,
            "allow_destructive": True,
            "allow_hardware": True,
            "allow_external": True,
            "allow_unrestricted": True,
            "confirm": "EXECUTE_UNRESTRICTED_TCL",
            "hardware_mode": "enabled",
            },
        ))

    assert mode_blocked["ok"] is False
    assert mode_blocked["error_code"] == "TCL_EXECUTION_DISABLED"
    assert query_allowed["ok"] is False
    assert query_allowed["error_code"] == "TCL_EXECUTION_DISABLED"
    for programming_blocked in programming_results:
        assert programming_blocked["ok"] is False
        assert programming_blocked["error_code"] == "RAW_TCL_HARDWARE_PROGRAMMING_FORBIDDEN"
        assert programming_blocked["next_actions"][0]["tool"] == "program_hw_device"
    assert session.commands == []


def test_raw_tcl_dynamic_dispatch_and_aliases_never_reach_session() -> None:
    session = RecordingSession(raw="must-not-run")
    service = VivadoToolService(session=session)
    commands = [
        "set p [join {program hw devices} _]; set d [join {current hw device} _]; $p [$d]",
        "proc get_vmcp_exec {} {exec cmd /c whoami}; get_vmcp_exec",
        "rename program_hw_devices get_vmcp_status; get_vmcp_status [current_hw_device]",
        "interp alias {} get_vmcp_status {} program_hw_devices; get_vmcp_status [current_hw_device]",
        "namespace eval ::vmcp {namespace import ::program_hw_devices}",
        "uplevel #0 $dynamic_script",
    ]
    gate = {
        "execution_intent": "execute reviewed unrestricted Tcl",
        "allow_project_write": True,
        "allow_destructive": True,
        "allow_hardware": True,
        "allow_external": True,
        "allow_unrestricted": True,
        "confirm": "EXECUTE_UNRESTRICTED_TCL",
        "hardware_mode": "enabled",
    }

    results = [service.call("run_tcl", {**gate, "command": command}) for command in commands]

    assert all(result["ok"] is False for result in results)
    assert all(result["error_code"] in {"TCL_EXECUTION_DISABLED", "RAW_TCL_HARDWARE_PROGRAMMING_FORBIDDEN"} for result in results)
    assert session.commands == []


def test_run_tcl_dry_run_and_policy_gate_do_not_execute_risky_tcl() -> None:
    session = RecordingSession(raw="deleted=never")
    service = VivadoToolService(session=session)

    rejected_dry_run = service.call("run_tcl", {"command": "file delete -force -- {demo.runs}", "dry_run": True})
    accepted_dry_run = service.call("run_tcl", {"command": "get_ports -quiet *", "dry_run": True})
    blocked = service.call("run_tcl", {"command": "file delete -force -- {demo.runs}"})
    wrong_gate = service.call(
        "run_tcl",
        {
            "command": "file\tdelete -force -- {demo.runs}",
            "execution_intent": "delete generated Vivado output selected by a higher-level tool",
            "allow_project_write": True,
        },
    )
    allowed = service.call(
        "run_tcl",
        {
            "command": "file delete -force -- {demo.runs}",
            "execution_intent": "delete generated Vivado output selected by a higher-level tool",
            "allow_project_write": True,
            "allow_destructive": True,
            "allow_hardware": True,
            "allow_external": True,
            "allow_unrestricted": True,
            "confirm": "EXECUTE_UNRESTRICTED_TCL",
        },
    )

    assert rejected_dry_run["ok"] is False
    assert rejected_dry_run["error_code"] == "TCL_POLICY_BLOCKED"
    assert rejected_dry_run["policy_allowed"] is False
    assert accepted_dry_run["ok"] is True
    assert accepted_dry_run["data"]["status"] == "DRY_RUN"
    assert blocked["ok"] is False
    assert blocked["error_code"] == "TCL_POLICY_BLOCKED"
    assert blocked["next_actions"][0]["tool"] == "run_tcl"
    assert wrong_gate["ok"] is False
    assert wrong_gate["error_code"] == "TCL_POLICY_BLOCKED"
    assert "allow_destructive" in wrong_gate["data"]["missing"]
    assert allowed["ok"] is False
    assert allowed["error_code"] == "TCL_EXECUTION_DISABLED"
    assert session.commands == []


def test_hardware_programming_gate_blocks_missing_hash_and_fingerprint_mismatch(tmp_path: Path, monkeypatch) -> None:
    bitstream = tmp_path / "top.bit"
    bitstream.write_text("bitstream", encoding="utf-8")
    session = RecordingSession(raw=encode_wire_row({'device': 'xc7a35t_0', 'part': 'xc7a35tcpg236-1', 'programmed': '0'}))
    service = VivadoToolService(session=session)

    missing_gate = service.call("program_hw_device", {"bitstream_path": str(bitstream), "device": "xc7a35t_0"})
    hash_mismatch = service.call(
        "program_hw_device",
        {
            "bitstream_path": str(bitstream),
            "device": "xc7a35t_0",
            "hardware_intent": "program explicit attached board",
            "confirm": "PROGRAM_FPGA",
            "board_fingerprint": "device=xc7a35t_0|part=xc7a35tcpg236-1",
            "expected_bitstream_sha256": "0" * 64,
        },
    )
    mode_disabled = service.call(
        "program_hw_device",
        {
            "bitstream_path": str(bitstream),
            "device": "xc7a35t_0",
            "hardware_intent": "program explicit attached board",
            "confirm": "PROGRAM_FPGA",
            "board_fingerprint": "device=xc7a35t_0|part=xc7a35tcpg236-1",
            "expected_bitstream_sha256": hashlib.sha256(bitstream.read_bytes()).hexdigest(),
        },
    )
    monkeypatch.setenv("VIVADO_AGENT_MCP_HARDWARE_MODE", "enabled")
    fingerprint_mismatch = service.call(
        "program_hw_device",
        {
            "bitstream_path": str(bitstream),
            "device": "xc7a35t_0",
            "hardware_intent": "program explicit attached board",
            "confirm": "PROGRAM_FPGA",
            "board_fingerprint": "device=other|part=xc7a35tcpg236-1",
            "expected_bitstream_sha256": hashlib.sha256(bitstream.read_bytes()).hexdigest(),
            "hardware_mode": "enabled",
        },
    )
    part_only_fingerprint = service.call(
        "program_hw_device",
        {
            "bitstream_path": str(bitstream),
            "device": "xc7a35t_0",
            "hardware_intent": "program explicit attached board",
            "confirm": "PROGRAM_FPGA",
            "board_fingerprint": "xc7a35tcpg236-1",
            "expected_bitstream_sha256": hashlib.sha256(bitstream.read_bytes()).hexdigest(),
            "hardware_mode": "enabled",
        },
    )

    assert missing_gate["ok"] is False
    assert missing_gate["error_code"] == "HARDWARE_INTENT_REQUIRED"
    assert hash_mismatch["ok"] is False
    assert hash_mismatch["error_code"] == "BITSTREAM_HASH_MISMATCH"
    assert mode_disabled["ok"] is False
    assert mode_disabled["error_code"] == "HARDWARE_MODE_DISABLED"
    assert fingerprint_mismatch["ok"] is False
    assert fingerprint_mismatch["error_code"] == "BOARD_FINGERPRINT_MISMATCH"
    assert part_only_fingerprint["ok"] is False
    assert part_only_fingerprint["error_code"] == "BOARD_FINGERPRINT_MISMATCH"
    assert not any("program_hw_devices" in command for command in session.commands)
    assert len(session.commands) == 2


def test_reset_and_clean_outputs_default_to_dry_run_and_require_confirmation(tmp_path: Path) -> None:
    reset_session = RecordingSession(raw=encode_wire_row({'run': 'synth_1', 'status': 'Not started', 'progress': '0%'}))
    reset_service = VivadoToolService(session=reset_session)

    reset_dry_run = reset_service.call("reset_runs", {"run_names": ["synth_1"]})
    reset_blocked = reset_service.call("reset_runs", {"run_names": ["synth_1"], "dry_run": False})
    reset_real = reset_service.call(
        "reset_runs",
        {
            "run_names": ["synth_1"],
            "dry_run": False,
            "intent": "reset generated synthesis run before rerun",
            "confirm": "RESET_RUNS",
        },
    )

    assert reset_dry_run["ok"] is True
    assert reset_dry_run["data"]["status"] == "DRY_RUN"
    assert reset_blocked["ok"] is False
    assert reset_blocked["error_code"] == "DESTRUCTIVE_CONFIRMATION_REQUIRED"
    assert reset_real["ok"] is True
    assert len(reset_session.commands) == 1
    assert "reset_run" in reset_session.commands[0]

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    clean_target = project_dir / "demo.runs" / "synth_1"
    clean_session = RecordingSession(
        raw=(
            "project_name=demo\n"
            f"project_dir={project_dir}\n"
            f"targets={encode_wire_list([str(clean_target)])}"
        )
    )
    clean_service = VivadoToolService(session=clean_session)

    clean_dry_run = clean_service.call("clean_run_outputs", {"run_names": ["synth_1"]})
    clean_blocked = clean_service.call("clean_run_outputs", {"run_names": ["synth_1"], "dry_run": False})
    clean_real = clean_service.call(
        "clean_run_outputs",
        {
            "run_names": ["synth_1"],
            "dry_run": False,
            "intent": "clean generated run outputs after review",
            "confirm": "CLEAN_RUN_OUTPUTS",
        },
    )

    assert clean_dry_run["ok"] is True
    assert clean_dry_run["data"]["status"] == "DRY_RUN"
    assert clean_dry_run["data"]["executed"] is False
    assert "command" not in clean_dry_run["data"]
    assert clean_dry_run["data"]["planned_targets"] == [
        {"kind": "run_output", "name": "synth_1", "pattern": "<project_dir>/<project_name>.runs/<run_name>"}
    ]
    assert clean_blocked["ok"] is False
    assert clean_blocked["error_code"] == "DESTRUCTIVE_CONFIRMATION_REQUIRED"
    assert clean_real["ok"] is True
    assert clean_real["data"]["run_names"] == ["synth_1"]
    assert clean_real["data"]["simsets"] == []
    assert clean_real["data"]["executed"] is True
    assert clean_real["data"]["deletion_backend"] == "python_managed_snapshot_broker"
    assert len(clean_session.commands) == 1
    assert "file delete -force" not in clean_session.commands[0]

    sim_clean_dry_run = clean_service.call("clean_run_outputs", {"simsets": ["sim_1"]})
    assert sim_clean_dry_run["data"]["run_names"] == []
    assert sim_clean_dry_run["data"]["planned_targets"] == [
        {"kind": "simulation_output", "name": "sim_1", "pattern": "<project_dir>/<project_name>.sim/<simset>"}
    ]
