from pathlib import Path

from vivado_agent_mcp.result import failure, success
from vivado_agent_mcp.tools import VivadoToolService
from vivado_agent_mcp.vivado.constraints import CHECK_TIMING_KEYS
from vivado_agent_mcp.vivado.project_capability import create_project_capability
from vivado_agent_mcp.vivado.simulation import XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION
from vivado_agent_mcp.vivado.session import (
    GuiTcpVivadoSession,
    PROJECT_ACTIVE_IDENTITY_MISMATCH_MARKER,
    SessionTaintedError,
)
from vivado_agent_mcp.vivado.wire import encode_wire_list, encode_wire_row


def _attested_report(report_type: str, begin_marker: str, body: str) -> str:
    report_command = {
        "check_timing": "check_timing",
        "drc": "report_drc",
        "methodology": "report_methodology",
        "timing_summary": "report_timing_summary",
    }.get(report_type, report_type)
    return (
        "vmcp_report_ok=1\n"
        f"vmcp_report_type={report_type}\n"
        "vmcp_vivado_version_short=2021.2\n"
        "vmcp_vivado_build=Vivado v2021.2\n"
        f"vmcp_report_command={report_command}\n"
        "vmcp_parser_schema_version=vivado_2021_2_v1\n"
        f"vmcp_report_bytes={len(body.encode('utf-8'))}\n"
        f"vmcp_report_begin={begin_marker}\n"
        f"{body}\n"
        f"vmcp_report_end={begin_marker.replace('_BEGIN__', '_END__')}"
    )


def _complete_check_timing_body(**overrides: int) -> str:
    counts = {key: 0 for key in CHECK_TIMING_KEYS} | overrides
    return "check_timing report\n" + "\n".join(
        f"checking {key} ({counts[key]})" for key in CHECK_TIMING_KEYS
    )


class FakeSession:
    def __init__(self, raw: str | None = None, ok: bool = True) -> None:
        self.commands: list[str] = []
        self.raw = raw if raw is not None else "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.010 0.000 0.020 0.000"
        self.ok = ok

    def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
        self.commands.append(command)
        return {"ok": self.ok, "raw": self.raw}

    def status(self) -> dict:
        return {"ok": True, "connected": True, "backend": "fake"}


def _unrestricted_tcl_gate() -> dict[str, object]:
    return {
        "execution_intent": "exercise reviewed unrestricted Tcl in a unit test",
        "allow_project_write": True,
        "allow_destructive": True,
        "allow_hardware": True,
        "allow_external": True,
        "allow_unrestricted": True,
        "confirm": "EXECUTE_UNRESTRICTED_TCL",
    }


def _bind_managed_project(service: VivadoToolService, project_dir, *, project_name: str = "demo") -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    project_path = project_dir / f"{project_name}.xpr"
    if not project_path.exists():
        project_path.write_text("# managed test project\n", encoding="utf-8")
    generation_id = str(service._session().generation_id)
    capability = create_project_capability(project_path, generation_id=generation_id)
    service._mcp_created_project_capabilities[capability["project_path_key"]] = capability
    service._active_project_capability = capability
    service._project_mutation_scope = "mcp_created_project"


class ProjectSwitchingManagedSession(GuiTcpVivadoSession):
    def __init__(self, actual_project) -> None:
        super().__init__(generation_id="project-switch-generation")
        self.actual_project = str(actual_project)
        self.expected_project = ""
        self.commands: list[str] = []

    def require_current_project(self, project_path):
        session = self

        class _Guard:
            def __enter__(self):
                session.expected_project = str(project_path)
                return session

            def __exit__(self, *_exc):
                session.expected_project = ""
                return False

        return _Guard()

    def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
        if self.expected_project and Path(self.expected_project).resolve() != Path(self.actual_project).resolve():
            return {
                "ok": False,
                "raw": f"{PROJECT_ACTIVE_IDENTITY_MISMATCH_MARKER}: expected={self.expected_project} actual={self.actual_project}",
                "error_code": "PROJECT_ACTIVE_IDENTITY_MISMATCH",
                "message": "Vivado current_project does not match the managed project capability.",
            }
        self.commands.append(command)
        return {"ok": True, "raw": "top=demo_top"}

    def status(self) -> dict:
        return {"ok": True, "connected": True, "process_running": True, "backend": "fake-managed"}


def test_managed_project_action_blocks_when_gui_switched_to_same_named_project(tmp_path: Path) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_b.mkdir()
    switched_xpr = project_b / "demo.xpr"
    switched_xpr.write_text("# project b\n", encoding="utf-8")
    session = ProjectSwitchingManagedSession(switched_xpr)
    service = VivadoToolService(session=session)
    _bind_managed_project(service, project_a)

    result = service.call("set_project_top", {"top": "demo_top"})

    assert result["ok"] is False
    assert result["error_code"] == "PROJECT_ACTIVE_IDENTITY_MISMATCH"
    assert result["data"]["mutation_scope"] == "indeterminate"
    assert result["data"]["handler_executed"] is False
    assert service._active_project_capability is None
    assert session.commands == []


def test_managed_project_action_blocks_when_vivado_has_no_current_project(tmp_path: Path) -> None:
    session = ProjectSwitchingManagedSession("")
    service = VivadoToolService(session=session)
    _bind_managed_project(service, tmp_path / "project-a")

    result = service.call("set_project_top", {"top": "demo_top"})

    assert result["ok"] is False
    assert result["error_code"] == "PROJECT_ACTIVE_IDENTITY_MISMATCH"
    assert result["data"]["mutation_scope"] == "indeterminate"
    assert result["data"]["handler_executed"] is False
    assert service._active_project_capability is None
    assert session.commands == []


def test_start_session_timeout_failure_suggests_probe_and_longer_retry() -> None:
    class TimeoutManager:
        def start(self, **kwargs) -> dict:
            self.kwargs = kwargs
            return {
                "ok": False,
                "error_code": "VIVADO_START_TIMEOUT",
                "message": "Vivado Tcl server did not accept connections within 180s.",
                "runtime_dir": r"D:\Vivado_Mcp\.vivado_agent_mcp\runtime",
                "startup": {
                    "phase": "tcl_server_timeout",
                    "vivado_process_started": True,
                    "tcl_server_connected": False,
                    "recommended_retry_timeout_s": 240,
                },
            }

        def current(self):
            raise RuntimeError("not used")

    manager = TimeoutManager()
    service = VivadoToolService(manager=manager)

    result = service.call("start_session", {})

    assert result["ok"] is False
    assert manager.kwargs["timeout_s"] == 180
    assert result["data"]["startup"]["phase"] == "tcl_server_timeout"
    assert result["data"]["startup"]["recommended_retry_timeout_s"] == 240
    assert [action["tool"] for action in result["next_actions"][:2]] == ["detect_vivado_environment", "start_session"]
    assert result["next_actions"][1]["arg_sources"]["timeout_s"] == "240"


def test_service_freezes_vivado_path_at_startup_and_rejects_later_override(tmp_path: Path, monkeypatch) -> None:
    trusted = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("@echo off\necho trusted\n", encoding="utf-8")
    sentinel = tmp_path / "sentinel.txt"
    attacker = tmp_path / "attacker.cmd"
    attacker.write_text(f'@echo off\necho executed>"{sentinel}"\n', encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(trusted))
    service = VivadoToolService()
    monkeypatch.setenv("VIVADO_PATH", str(attacker))

    configured = service.call("detect_vivado_environment", {})

    detect = service.call(
        "detect_vivado_environment",
        {"vivado_path": str(attacker), "probe_launch": True, "runtime_dir": str(tmp_path / "runtime")},
    )
    start = service.call("start_session", {"vivado_path": str(attacker), "runtime_dir": str(tmp_path / "runtime")})

    assert configured["ok"] is True
    assert configured["data"]["path"] == str(trusted.resolve())
    assert detect["ok"] is False
    assert detect["error_code"] == "VIVADO_PATH_MISMATCH"
    assert detect["data"]["execution_attempted"] is False
    assert start["ok"] is False
    assert start["error_code"] == "VIVADO_PATH_MISMATCH"
    assert start["data"]["execution_attempted"] is False
    assert sentinel.exists() is False


def test_service_rejects_vivado_path_before_diagnostic_handler_runs(tmp_path: Path, monkeypatch) -> None:
    trusted = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("@echo off\necho trusted\n", encoding="utf-8")
    attacker = tmp_path / "attacker.cmd"
    attacker.write_text("@echo off\necho attacker\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(trusted))
    service = VivadoToolService()
    handler_called = False

    def reject_handler(args: dict) -> dict:
        nonlocal handler_called
        handler_called = True
        raise AssertionError("diagnostic handler must not run before path assertion validation")

    monkeypatch.setattr(service, "_collect_diagnostic_bundle", reject_handler)

    result = service.call("collect_diagnostic_bundle", {"vivado_path": str(attacker)})

    assert result["ok"] is False
    assert result["error_code"] == "VIVADO_PATH_MISMATCH"
    assert result["data"]["handler_executed"] is False
    assert result["data"]["execution_attempted"] is False
    assert handler_called is False


def test_tool_service_returns_structured_timing_summary() -> None:
    body = "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.010 0.000 0.020 0.000"
    service = VivadoToolService(
        session=FakeSession(raw=_attested_report("timing_summary", "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__", body))
    )

    result = service.call("get_timing_summary", {})

    assert result["ok"] is True
    assert result["tool"] == "get_timing_summary"
    assert result["data"]["timing_met"] is True


def test_get_timing_summary_rejects_legacy_arbitrary_command_input() -> None:
    fake = FakeSession()
    service = VivadoToolService(session=fake)

    result = service.call("get_timing_summary", {"command": "exec calc"})

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_TOOL_ARGUMENTS"
    assert result["data"]["handler_executed"] is False
    assert fake.commands == []


def test_runtime_schema_validation_rejects_missing_required_and_wrong_typed_arguments() -> None:
    fake = FakeSession()
    service = VivadoToolService(session=fake)

    missing = service.call("create_project", {"project_name": "demo"})
    wrong_type = service.call("run_behavioral_simulation", {"max_vcd_mb": "256"})

    assert missing["error_code"] == "INVALID_TOOL_ARGUMENTS"
    assert wrong_type["error_code"] == "INVALID_TOOL_ARGUMENTS"
    assert missing["data"]["handler_executed"] is False
    assert wrong_type["data"]["handler_executed"] is False
    assert fake.commands == []


def test_create_project_rejects_xci_before_calling_vivado(tmp_path) -> None:
    xci = tmp_path / "third_party.xci"
    xci.write_text("<spirit:component/>", encoding="utf-8")
    fake = FakeSession()
    service = VivadoToolService(session=fake)

    result = service.call(
        "create_project",
        {
            "project_name": "demo",
            "project_dir": str(tmp_path / "project"),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [str(xci)],
        },
    )

    assert result["ok"] is False
    assert result["error_code"] == "EXECUTABLE_COMPOSITE_INPUT_UNSUPPORTED"
    assert result["data"]["handler_executed"] is False
    assert fake.commands == []


def test_managed_composite_handlers_are_blocked_before_calling_vivado(tmp_path) -> None:
    class ManagedSession(GuiTcpVivadoSession):
        def __init__(self) -> None:
            super().__init__(generation_id="generation-test")
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return {"ok": True, "raw": ""}

    session = ManagedSession()
    service = VivadoToolService(session=session)
    _bind_managed_project(service, tmp_path / "project")

    create_result = service.call(
        "create_ip",
        {"vlnv": "xilinx.com:ip:xlconstant:1.1", "module_name": "const_0"},
    )
    open_result = service.call("open_block_design", {"name": "design_1"})
    validate_result = service.call("validate_block_design", {"bd_name": "design_1"})

    for result in (create_result, open_result, validate_result):
        assert result["ok"] is False
        assert result["error_code"] == "EXECUTABLE_COMPOSITE_INPUT_UNSUPPORTED"
        assert result["data"]["handler_executed"] is False
    assert session.commands == []


def test_tool_service_safe_tcl_uses_escaped_template_arguments() -> None:
    fake = FakeSession()
    service = VivadoToolService(session=fake)

    result = service.call(
        "safe_tcl",
        {
            "template": "open_project {project}",
            "args": {"project": r"D:\fpga demo\a$b.xpr"},
            "execution_intent": "Open an explicitly supplied project for inspection.",
            "allow_project_write": True,
            "allow_destructive": True,
            "allow_hardware": True,
            "allow_external": True,
            "allow_unrestricted": True,
            "confirm": "EXECUTE_UNRESTRICTED_TCL",
            "dry_run": True,
        },
    )

    assert result["ok"] is True
    assert result["data"]["command"] == r"open_project {D:\fpga demo\a$b.xpr}"
    assert fake.commands == []


def test_tool_service_safe_tcl_blocks_project_write_without_policy_gate() -> None:
    fake = FakeSession()
    service = VivadoToolService(session=fake)

    result = service.call(
        "safe_tcl",
        {"template": "open_project {project}", "args": {"project": r"D:\fpga demo\a$b.xpr"}},
    )

    assert result["ok"] is False
    assert result["error_code"] == "TCL_POLICY_BLOCKED"
    assert result["data"]["policy"]["risk"] == "UNRESTRICTED"
    assert "allow_project_write" in result["data"]["missing"]
    assert fake.commands == []


def test_tool_service_safe_tcl_reports_missing_placeholder_without_executing() -> None:
    fake = FakeSession()
    service = VivadoToolService(session=fake)

    result = service.call("safe_tcl", {"template": "open_project {project}", "args": {}})

    assert result["ok"] is False
    assert result["error_code"] == "SAFE_TCL_TEMPLATE_ERROR"
    assert result["next_actions"][0]["tool"] == "safe_tcl"
    assert fake.commands == []


def test_tool_service_safe_tcl_allows_read_only_literal_braces() -> None:
    fake = FakeSession(raw="clk")
    service = VivadoToolService(session=fake)

    result = service.call(
        "safe_tcl",
        {
            "template": "get_ports -quiet -filter {DIRECTION == IN}",
            "args": {},
            "dry_run": True,
        },
    )

    assert result["ok"] is True
    assert result["data"]["command"] == "get_ports -quiet -filter {DIRECTION == IN}"
    assert fake.commands == []


def test_tool_service_safe_tcl_normalizes_top_level_return_result() -> None:
    class ReturnSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return {"ok": False, "raw": "2021.2", "status_line": "vmcp_test STATUS 2"}

    fake = ReturnSession()
    service = VivadoToolService(session=fake)

    result = service.call("safe_tcl", {"template": "return [version -short]", "args": {}, **_unrestricted_tcl_gate()})

    assert result["ok"] is False
    assert result["error_code"] == "TCL_EXECUTION_DISABLED"
    assert fake.commands == []


def test_tool_service_run_tcl_normalizes_top_level_return_result() -> None:
    class ReturnSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return {"ok": False, "raw": "2021.2", "status_line": "vmcp_test STATUS 2"}

    fake = ReturnSession()
    service = VivadoToolService(session=fake)

    result = service.call("run_tcl", {"command": "return [version -short]", **_unrestricted_tcl_gate()})

    assert result["ok"] is False
    assert result["error_code"] == "TCL_EXECUTION_DISABLED"
    assert fake.commands == []


def test_tool_service_safe_tcl_does_not_normalize_return_code_error() -> None:
    class ReturnErrorSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return {"ok": False, "raw": "bad", "status_line": "vmcp_test STATUS 2"}

    fake = ReturnErrorSession()
    service = VivadoToolService(session=fake)

    result = service.call("safe_tcl", {"template": "return -code error bad", "args": {}, **_unrestricted_tcl_gate()})

    assert result["ok"] is False
    assert result["error_code"] == "TCL_EXECUTION_DISABLED"
    assert "tcl_return_normalized" not in result["data"]
    assert fake.commands == []


def test_icarus_public_tools_are_removed() -> None:
    service = VivadoToolService(session=FakeSession())

    assert service.call("detect_simulation_environment", {})["error_code"] == "UNKNOWN_TOOL"
    assert service.call("run_rtl_simulation", {})["error_code"] == "UNKNOWN_TOOL"


def test_create_project_builds_vivado_project_with_simset(tmp_path) -> None:
    rtl = tmp_path / "rtl dir" / "top.v"
    xdc = tmp_path / "constraints" / "top.xdc"
    sim = tmp_path / "sim sources" / "tb_top.v"
    project_dir = tmp_path / "vivado project"
    rtl.parent.mkdir()
    xdc.parent.mkdir()
    sim.parent.mkdir()
    rtl.write_text("module top; endmodule", encoding="utf-8")
    xdc.write_text("", encoding="utf-8")
    sim.write_text("module tb_top; endmodule", encoding="utf-8")
    fake = FakeSession(raw=str(project_dir).replace("\\", "/"))
    service = VivadoToolService(session=fake)

    result = service.call(
        "create_project",
        {
            "project_name": "demo project",
            "project_dir": str(project_dir),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [str(rtl)],
            "xdc_files": [str(xdc)],
            "sim_files": [str(sim)],
            "testbench_top": "tb_top",
            "source_include_dirs": [str(tmp_path / "rtl include")],
            "source_defines": {"SYNTHESIS": "1", "SOURCE_FLAG": None},
            "include_dirs": [str(tmp_path / "rtl dir")],
            "defines": {"VMCP_TEST": "1", "FLAG_ONLY": None},
            "target_language": "Verilog",
            "simulator": "Vivado Simulator",
            "force": False,
        },
    )

    assert result["ok"] is True
    command = fake.commands[0]
    assert "create_project {demo project}" in command
    assert " -part {xc7a35tcpg236-1}" in command
    assert " -force" not in command
    assert "add_files [list" in command
    assert str(rtl) in command
    assert "add_files -fileset constrs_1 [list" in command
    assert str(xdc) in command
    assert "add_files -fileset {sim_1} [list" in command
    assert str(sim) in command
    assert "set_property top {top} [current_fileset]" in command
    assert "set_property top {tb_top} [get_filesets {sim_1}]" in command
    assert f"set_property include_dirs [list {{{tmp_path / 'rtl include'}}}] [get_filesets {{sources_1}}]" in command
    assert "set_property verilog_define [list {SYNTHESIS=1} {SOURCE_FLAG}] [get_filesets {sources_1}]" in command
    assert "set_property include_dirs [list" in command
    assert "set_property verilog_define [list {VMCP_TEST=1} {FLAG_ONLY}]" in command
    assert "set_property target_language {Verilog} [current_project]" in command
    assert "set_property target_simulator {Vivado Simulator} [current_project]" in command
    assert "update_compile_order -fileset sources_1" in command
    assert "update_compile_order -fileset {sim_1}" in command
    assert result["data"]["project"]["name"] == "demo project"
    assert result["data"]["project"]["sim_top"] == "tb_top"
    assert result["data"]["project"]["xpr_path"].endswith("demo project.xpr")
    assert result["data"]["files"]["rtl"] == [str(rtl)]
    assert result["data"]["files"]["xdc"] == [str(xdc)]
    assert result["data"]["files"]["sim"] == [str(sim)]
    assert result["data"]["setup_status"]["status"] == "READY"
    assert result["data"]["setup_status"]["status_scope"] == "post_create_project"
    assert result["data"]["setup_status"]["actual_state_known"] is True
    assert result["data"]["setup_status"]["needs_open_project"] is False
    assert result["data"]["setup_status"]["needs_fileset_repair"] is False


def test_create_project_maps_systemverilog_target_language_to_vivado_project_language(tmp_path) -> None:
    rtl = tmp_path / "rtl" / "top.sv"
    sim = tmp_path / "sim" / "tb_top.sv"
    project_dir = tmp_path / "vivado_project"
    rtl.parent.mkdir()
    sim.parent.mkdir()
    rtl.write_text("module top; endmodule", encoding="utf-8")
    sim.write_text("module tb_top; endmodule", encoding="utf-8")
    fake = FakeSession(raw=str(project_dir).replace("\\", "/"))
    service = VivadoToolService(session=fake)

    result = service.call(
        "create_project",
        {
            "project_name": "sv_demo",
            "project_dir": str(project_dir),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [str(rtl)],
            "sim_files": [str(sim)],
            "testbench_top": "tb_top",
            "target_language": "SystemVerilog",
        },
    )

    assert result["ok"] is True
    command = fake.commands[0]
    assert "set_property target_language {Verilog} [current_project]" in command
    assert "set_property target_language {SystemVerilog}" not in command
    assert "set_property file_type {SystemVerilog}" in command
    assert str(rtl) in command
    assert str(sim) in command
    assert result["data"]["project"]["target_language"] == "SystemVerilog"
    assert result["data"]["project"]["vivado_target_language"] == "Verilog"
    assert result["data"]["project"]["systemverilog_file_type"] == "SystemVerilog"
    assert result["data"]["project"]["requested_target_language"] == "SystemVerilog"
    assert result["data"]["project"]["vivado_project_target_language"] == "Verilog"
    assert result["data"]["project"]["source_file_type_policy"] == "SystemVerilog"
    assert "project target_language remains Verilog" in result["message"]
    assert "file_type=SystemVerilog" in result["data"]["language_policy_note"]


def test_create_project_replays_and_verifies_complete_file_semantics(tmp_path) -> None:
    rtl = tmp_path / "rtl" / "top.v"
    project_dir = tmp_path / "working_project"
    rtl.parent.mkdir()
    rtl.write_text("module top; logic value; endmodule", encoding="utf-8")
    spec = {
        "path": str(rtl),
        "fileset": "sources_1",
        "file_type": "SystemVerilog",
        "library": "design_lib",
        "compile_order": 0,
        "is_global_include": False,
        "used_in_synthesis": True,
        "used_in_implementation": True,
        "used_in_simulation": True,
        "processing_order": "",
        "scoped_to_ref": "",
        "scoped_to_cells": [],
    }
    inventory_raw = encode_wire_row(
        {
            "path": str(rtl),
            "fileset": "sources_1",
            "type": "SystemVerilog",
            "library": "design_lib",
            "compile_order": "0",
            "exists": "1",
            "managed": "0",
            "is_global_include": "0",
            "used_in_synthesis": "1",
            "used_in_implementation": "1",
            "used_in_simulation": "1",
            "processing_order": "",
            "scoped_to_ref": "",
            "scoped_to_cells": encode_wire_list([]),
        }
    ) + "\n" + encode_wire_row({"vmcp_meta": "1", "discovery_errors": encode_wire_list([])})

    class SemanticSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "get_files -quiet -of_objects $fs" in command:
                return {"ok": True, "raw": inventory_raw}
            return {"ok": True, "raw": str(project_dir).replace("\\", "/")}

    fake = SemanticSession()
    service = VivadoToolService(session=fake)

    result = service.call(
        "create_project",
        {
            "project_name": "semantic_demo",
            "project_dir": str(project_dir),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [str(rtl)],
            "file_specs": [spec],
        },
    )

    assert result["ok"] is True
    assert "set_property FILE_TYPE {SystemVerilog}" in fake.commands[0]
    assert "set_property LIBRARY {design_lib}" in fake.commands[0]
    assert "reorder_files -fileset {sources_1} -front [list" in fake.commands[0]
    assert result["data"]["file_semantics"]["reconstruction_equivalent"] is True
    assert result["data"]["file_semantics"]["expected_digest"] == result["data"]["file_semantics"]["actual_digest"]
    assert all(
        "command" not in fileset_result and len(fileset_result["command_sha256"]) == 64
        for fileset_result in result["data"]["file_semantics"]["filesets"].values()
    )


def test_create_project_blocks_when_post_create_file_semantics_drift(tmp_path) -> None:
    rtl = tmp_path / "rtl" / "top.v"
    project_dir = tmp_path / "working_project"
    rtl.parent.mkdir()
    rtl.write_text("module top; logic value; endmodule", encoding="utf-8")
    spec = {
        "path": str(rtl),
        "fileset": "sources_1",
        "file_type": "SystemVerilog",
        "library": "xil_defaultlib",
        "compile_order": 0,
        "is_global_include": False,
        "used_in_synthesis": True,
        "used_in_implementation": True,
        "used_in_simulation": True,
        "processing_order": "",
        "scoped_to_ref": "",
        "scoped_to_cells": [],
    }
    drifted_raw = encode_wire_row(
        {
            "path": str(rtl),
            "fileset": "sources_1",
            "type": "Verilog",
            "library": "xil_defaultlib",
            "compile_order": "0",
            "exists": "1",
            "managed": "0",
            "is_global_include": "0",
            "used_in_synthesis": "1",
            "used_in_implementation": "1",
            "used_in_simulation": "1",
            "processing_order": "",
            "scoped_to_ref": "",
            "scoped_to_cells": encode_wire_list([]),
        }
    ) + "\n" + encode_wire_row({"vmcp_meta": "1", "discovery_errors": encode_wire_list([])})

    class DriftSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "get_files -quiet -of_objects $fs" in command:
                return {"ok": True, "raw": drifted_raw}
            return {"ok": True, "raw": str(project_dir).replace("\\", "/")}

    result = VivadoToolService(session=DriftSession()).call(
        "create_project",
        {
            "project_name": "semantic_demo",
            "project_dir": str(project_dir),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [str(rtl)],
            "file_specs": [spec],
        },
    )

    assert result["ok"] is False
    assert result["error_code"] == "PROJECT_FILE_SEMANTICS_MISMATCH"
    assert result["data"]["partial_success"] is True
    assert result["data"]["file_semantics"]["changed"][0]["differences"]["file_type"]["actual"] == "Verilog"
    assert result["data"]["project_capability"]["bound"] is False


def test_create_project_rejects_incomplete_file_semantics_inventory(tmp_path) -> None:
    rtl = tmp_path / "top.v"
    rtl.write_text("module top; endmodule", encoding="utf-8")
    service = VivadoToolService(session=FakeSession())

    result = service.call(
        "create_project",
        {
            "project_name": "semantic_demo",
            "project_dir": str(tmp_path / "working"),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [str(rtl)],
            "file_specs": [],
        },
    )

    assert result["ok"] is False
    assert result["error_code"] == "PROJECT_FILE_SEMANTICS_INVALID"
    assert service._session().commands == []


def test_create_project_timeout_reports_partial_project_context(tmp_path) -> None:
    rtl = tmp_path / "rtl" / "top.sv"
    sim = tmp_path / "sim" / "tb_top.sv"
    xdc = tmp_path / "xdc" / "top.xdc"
    project_dir = tmp_path / "vivado_project"
    project_dir.mkdir()
    for path in (rtl, sim, xdc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    class TimeoutSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            (project_dir / "demo.xpr").write_text("# partial project\n", encoding="utf-8")
            raise TimeoutError("timed out")

        def status(self) -> dict:
            return {
                "ok": True,
                "connected": False,
                "process_running": True,
                "runtime_dir": str(tmp_path / "runtime"),
                "stdout_path": str(tmp_path / "runtime" / "stdout.log"),
                "stderr_path": str(tmp_path / "runtime" / "stderr.log"),
            }

    service = VivadoToolService(session=TimeoutSession())

    result = service.call(
        "create_project",
        {
            "project_name": "demo",
            "project_dir": str(project_dir),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [str(rtl)],
            "xdc_files": [str(xdc)],
            "sim_files": [str(sim)],
            "testbench_top": "tb_top",
            "target_language": "SystemVerilog",
            "timeout_s": 1,
        },
    )

    assert result["ok"] is False
    assert result["error_code"] == "TimeoutError"
    assert result["data"]["timeout_s_used"] == 1
    assert result["data"]["partial_success"] is True
    assert result["data"]["project_capability_bound"] is False
    assert result["data"]["recovery_policy"] == "inspection_then_rebuild"
    assert result["data"]["project_path"] == str(project_dir / "demo.xpr")
    assert result["data"]["planned_files"]["rtl"] == [str(rtl)]
    assert result["data"]["planned_language_policy"]["source_file_type_policy"] == "SystemVerilog"
    assert result["data"]["setup_status"]["status"] == "WARN"
    assert {"session_status", "stop_session", "open_project", "create_project"} <= {
        action["tool"] for action in result["next_actions"]
    }
    assert "repair_project_setup" not in {action["tool"] for action in result["next_actions"]}


def test_create_project_refuses_to_adopt_existing_xpr(tmp_path) -> None:
    project_dir = tmp_path / "existing"
    project_dir.mkdir()
    project_path = project_dir / "demo.xpr"
    project_path.write_text("# existing\n", encoding="utf-8")
    fake = FakeSession()
    service = VivadoToolService(session=fake)

    result = service.call(
        "create_project",
        {
            "project_name": "demo",
            "project_dir": str(project_dir),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [],
        },
    )

    assert result["error_code"] == "PROJECT_ALREADY_EXISTS"
    assert result["data"]["original_project_protected"] is True
    actions = result["next_actions"]
    assert [action["tool"] for action in actions] == [
        "open_project",
        "get_project_state",
        "list_fileset_files",
        "list_fileset_files",
        "list_fileset_files",
        "close_project",
        "create_project",
    ]
    assert [action["arg_sources"]["fileset"] for action in actions[2:5]] == ["sources_1", "constrs_1", "sim_1"]
    rebuild = actions[-1]
    assert rebuild["required_args"] == [
        "project_name",
        "project_dir",
        "part",
        "top",
        "rtl_files",
        "xdc_files",
        "sim_files",
        "file_specs",
        "testbench_top",
        "target_language",
        "simulator",
        "source_include_dirs",
        "source_defines",
        "include_dirs",
        "defines",
    ]
    assert set(rebuild["required_args"]) <= set(rebuild["arg_sources"])
    assert {
        "source_include_dirs",
        "source_defines",
        "include_dirs",
        "defines",
        "target_language",
        "simulator",
    } <= set(rebuild["arg_sources"])
    assert "file_type is SystemVerilog" in rebuild["arg_sources"]["target_language"]
    assert "fileset_properties.discovery_status is READY" in " ".join(actions[-2]["preconditions"])
    assert all("shell" not in str(source).lower() for source in rebuild["arg_sources"].values())
    assert fake.commands == []


def test_create_project_timeout_without_xpr_does_not_suggest_open_project(tmp_path) -> None:
    class TimeoutSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            raise TimeoutError("timed out")

    project_dir = tmp_path / "missing_project"
    service = VivadoToolService(session=TimeoutSession())

    result = service.call(
        "create_project",
        {
            "project_name": "demo",
            "project_dir": str(project_dir),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [],
            "timeout_s": 1,
        },
    )

    assert result["ok"] is False
    assert result["data"]["partial_success"] is False
    assert "open_project" not in {action["tool"] for action in result["next_actions"]}
    assert "create_project" in {action["tool"] for action in result["next_actions"]}


def test_configure_simulation_updates_open_project_simset(tmp_path) -> None:
    sim = tmp_path / "tb_top.v"
    sim.write_text("module tb_top; endmodule", encoding="utf-8")
    fake = FakeSession(raw="")
    service = VivadoToolService(session=fake)

    result = service.call(
        "configure_simulation",
        {
            "sim_files": [str(sim)],
            "testbench_top": "tb_top",
            "include_dirs": [str(tmp_path)],
            "defines": {"VMCP_TEST": "1"},
            "simulator": "Vivado Simulator",
        },
    )

    assert result["ok"] is True
    assert result["tool"] == "configure_simulation"
    command = fake.commands[0]
    assert "add_files -fileset {sim_1} [list" in command
    assert "set_property top {tb_top} [get_filesets {sim_1}]" in command
    assert "set_property target_simulator {Vivado Simulator} [current_project]" in command
    assert "update_compile_order -fileset {sim_1}" in command


def test_add_project_files_timeout_reports_repair_actions(tmp_path) -> None:
    xdc = tmp_path / "top.xdc"
    xdc.write_text("", encoding="utf-8")

    class TimeoutSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            raise TimeoutError("timed out")

        def status(self) -> dict:
            return {"ok": True, "connected": False, "process_running": True, "runtime_dir": str(tmp_path / "runtime")}

    service = VivadoToolService(session=TimeoutSession())

    result = service.call("add_project_files", {"fileset": "constrs_1", "files": [str(xdc)], "timeout_s": 2})

    assert result["ok"] is False
    assert result["error_code"] == "TimeoutError"
    assert result["data"]["fileset"] == "constrs_1"
    assert result["data"]["files"] == [str(xdc)]
    assert result["data"]["project_state_hint"]["recommended_probe"] == "list_fileset_files"
    assert {"list_fileset_files", "repair_project_setup"} <= {action["tool"] for action in result["next_actions"]}


def test_constraint_tcl_inputs_are_blocked_before_vivado_execution(tmp_path) -> None:
    rtl = tmp_path / "top.sv"
    rtl.write_text("module top; endmodule\n", encoding="utf-8")
    constraint_tcl = tmp_path / "constraints.tcl"
    constraint_tcl.write_text("puts blocked\n", encoding="utf-8")

    for tool, arguments in (
        (
            "create_project",
            {
                "project_name": "demo",
                "project_dir": str(tmp_path / "project"),
                "part": "xc7a35tcpg236-1",
                "top": "top",
                "rtl_files": [str(rtl)],
                "xdc_files": [str(constraint_tcl)],
            },
        ),
        (
            "add_project_files",
            {"fileset": "constrs_1", "files": [str(constraint_tcl)]},
        ),
        (
            "repair_project_setup",
            {"rtl_files": [str(rtl)], "xdc_files": [str(constraint_tcl)], "top": "top", "dry_run": False},
        ),
    ):
        fake = FakeSession()
        result = VivadoToolService(session=fake).call(tool, arguments)

        assert result["ok"] is False
        assert result["error_code"] == "EXECUTABLE_CONSTRAINT_INPUT_BLOCKED"
        assert result["data"]["blocked_files"] == [str(constraint_tcl)]
        assert fake.commands == []


def test_open_project_maps_executable_constraint_guard_to_stable_error() -> None:
    class ConstraintGuardSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if command.startswith("open_project") or command == "catch {close_project}":
                return {"ok": True, "raw": ""}
            return {"ok": False, "raw": "VMCP_EXECUTABLE_CONSTRAINT_INPUT_BLOCKED: constraints.tcl FILE_TYPE Tcl"}

    fake = ConstraintGuardSession()

    result = VivadoToolService(session=fake).call("open_project", {"project_path": "D:/project/demo.xpr"})

    assert result["ok"] is False
    assert result["error_code"] == "EXECUTABLE_CONSTRAINT_INPUT_BLOCKED"
    assert result["data"]["policy_allowed"] is False
    assert result["data"]["project_close_attempted"] is True
    assert result["data"]["project_close_ok"] is True


def test_typed_project_tools_reject_executable_commands_disguised_as_xdc(tmp_path) -> None:
    rtl = tmp_path / "top.sv"
    malicious_xdc = tmp_path / "malicious.xdc"
    rtl.write_text("module top; endmodule\n", encoding="utf-8")
    malicious_xdc.write_text("exec cmd.exe /c echo pwned\n", encoding="utf-8")

    for tool, arguments in (
        (
            "create_project",
            {
                "project_name": "demo",
                "project_dir": str(tmp_path / "project"),
                "part": "xc7a35tcpg236-1",
                "top": "top",
                "rtl_files": [str(rtl)],
                "xdc_files": [str(malicious_xdc)],
            },
        ),
        ("add_project_files", {"fileset": "constrs_1", "files": [str(malicious_xdc)]}),
        (
            "repair_project_setup",
            {"rtl_files": [str(rtl)], "xdc_files": [str(malicious_xdc)], "top": "top", "dry_run": False},
        ),
    ):
        fake = FakeSession()
        result = VivadoToolService(session=fake).call(tool, arguments)

        assert result["ok"] is False
        assert result["error_code"] == "EXECUTABLE_CONSTRAINT_INPUT_BLOCKED"
        assert str(malicious_xdc) in result["data"]["policy_issues"]
        assert fake.commands == []


def test_repair_project_setup_dry_run_plans_setup_without_tcl(tmp_path) -> None:
    rtl = tmp_path / "top.sv"
    xdc = tmp_path / "top.xdc"
    sim = tmp_path / "tb_top.sv"
    for path in (rtl, xdc, sim):
        path.write_text("", encoding="utf-8")
    fake = FakeSession()
    service = VivadoToolService(session=fake)

    result = service.call(
        "repair_project_setup",
        {
            "rtl_files": [str(rtl)],
            "xdc_files": [str(xdc)],
            "sim_files": [str(sim)],
            "top": "top",
            "testbench_top": "tb_top",
            "target_language": "SystemVerilog",
        },
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "DRY_RUN"
    assert result["data"]["setup_status"]["status"] == "WARN"
    assert result["data"]["setup_status"]["status_scope"] == "planned_preflight"
    assert result["data"]["setup_status"]["actual_state_known"] is False
    assert result["data"]["setup_status"]["planned_fileset_repair"] is True
    assert result["data"]["setup_status"]["needs_fileset_repair"] is False
    assert all(not item["needs_repair"] for item in result["data"]["fileset_summary"].values())
    assert all(item["planned_repair"] for item in result["data"]["fileset_summary"].values())
    assert "planned_*" in result["data"]["setup_status"]["planned_preflight_note"]
    assert result["data"]["planned_operation_semantics"]["mode"] == "idempotent_reconcile"
    assert "do not imply duplicate file insertion" in result["data"]["planned_operation_semantics"]["note"]
    assert result["data"]["planned_operations"]
    assert all(operation["idempotent"] is True for operation in result["data"]["planned_operations"])
    assert all(operation["operation_semantics"] == "reconcile" for operation in result["data"]["planned_operations"])
    assert fake.commands == []


def test_repair_project_setup_executes_setup_commands(tmp_path) -> None:
    rtl = tmp_path / "top.sv"
    xdc = tmp_path / "top.xdc"
    sim = tmp_path / "tb_top.sv"
    for path in (rtl, xdc, sim):
        path.write_text("", encoding="utf-8")
    fake = FakeSession(raw="setup_status=READY\npostcondition_discovery_status=READY\nmissing_after_repair=\ndiscovery_errors=")
    service = VivadoToolService(session=fake)

    result = service.call(
        "repair_project_setup",
        {
            "rtl_files": [str(rtl)],
            "xdc_files": [str(xdc)],
            "sim_files": [str(sim)],
            "top": "top",
            "testbench_top": "tb_top",
            "target_language": "SystemVerilog",
            "dry_run": False,
        },
    )

    assert result["ok"] is True
    assert result["data"]["setup_status"]["status"] == "READY"
    assert result["data"]["setup_status"]["status_scope"] == "post_repair"
    assert result["data"]["setup_status"]["actual_state_known"] is True
    assert result["data"]["setup_status"]["needs_open_project"] is False
    assert result["data"]["setup_status"]["needs_fileset_repair"] is False
    assert all(not item["needs_repair"] for item in result["data"]["fileset_summary"].values())
    assert "add_files [list" in fake.commands[0]
    assert "add_files -fileset {constrs_1}" in fake.commands[0]
    assert "add_files -fileset {sim_1}" in fake.commands[0]
    assert "set_property top {tb_top} [get_filesets {sim_1}]" in fake.commands[0]
    assert "set_property file_type {SystemVerilog}" in fake.commands[0]
    assert "VMCP_RUN_HOOK_BLOCKED" in fake.commands[0]
    assert fake.commands[0].index("VMCP_RUN_HOOK_BLOCKED") < fake.commands[0].index("add_files [list")


def test_repair_project_setup_open_project_is_conditional_when_project_path_supplied(tmp_path) -> None:
    project_dir = tmp_path / "vivado_project"
    project_dir.mkdir()
    project_path = project_dir / "demo.xpr"
    project_path.write_text("", encoding="utf-8")
    rtl = tmp_path / "top.sv"
    rtl.write_text("", encoding="utf-8")
    fake = FakeSession(raw="setup_status=READY\npostcondition_discovery_status=READY\nmissing_after_repair=\ndiscovery_errors=")
    service = VivadoToolService(session=fake)

    result = service.call(
        "repair_project_setup",
        {
            "project_path": str(project_path),
            "rtl_files": [str(rtl)],
            "top": "top",
            "target_language": "SystemVerilog",
            "dry_run": False,
        },
    )

    assert result["ok"] is True
    assert result["data"]["setup_status"]["status_scope"] == "post_repair"
    assert result["data"]["setup_status"]["needs_open_project"] is False
    assert result["data"]["setup_status"]["needs_fileset_repair"] is False
    command = fake.commands[0]
    assert "vmcp_project_already_open" in command
    assert "if {$vmcp_project_already_open == 0} {open_project" in command
    assert command.count(f"open_project {{{project_path}}}") == 1
    assert "VMCP_RUN_HOOK_BLOCKED" in command
    assert command.index("VMCP_RUN_HOOK_BLOCKED") < command.index("add_files [list")


def test_repair_project_setup_blocks_when_postcondition_discovery_failed(tmp_path) -> None:
    rtl = tmp_path / "top.sv"
    rtl.write_text("module top; endmodule\n", encoding="utf-8")
    fake = FakeSession(
        raw=(
            "setup_status=ERROR\n"
            "postcondition_discovery_status=ERROR\n"
            "missing_after_repair=\n"
            f"discovery_errors={encode_wire_list(['get_files failed'])}"
        )
    )

    result = VivadoToolService(session=fake).call(
        "repair_project_setup",
        {"rtl_files": [str(rtl)], "top": "top", "dry_run": False},
    )

    assert result["ok"] is False
    assert result["error_code"] == "PROJECT_SETUP_POSTCONDITION_DISCOVERY_FAILED"
    assert result["data"]["discovery_errors"] == ["get_files failed"]


def test_repair_project_setup_stops_before_mutation_when_run_hook_guard_blocks(tmp_path) -> None:
    rtl = tmp_path / "top.sv"
    rtl.write_text("module top; endmodule\n", encoding="utf-8")
    fake = FakeSession(raw="VMCP_RUN_HOOK_BLOCKED run=impl_1 property=STEPS.ROUTE_DESIGN.TCL.PRE", ok=False)

    result = VivadoToolService(session=fake).call(
        "repair_project_setup",
        {"rtl_files": [str(rtl)], "top": "top", "dry_run": False},
    )

    assert result["ok"] is False
    assert len(fake.commands) == 1
    command = fake.commands[0]
    assert command.index("VMCP_RUN_HOOK_BLOCKED") < command.index("add_files [list")


def test_repair_project_setup_rejects_missing_files(tmp_path) -> None:
    service = VivadoToolService(session=FakeSession())

    result = service.call(
        "repair_project_setup",
        {
            "rtl_files": [str(tmp_path / "missing.sv")],
            "top": "top",
            "dry_run": False,
        },
    )

    assert result["ok"] is False
    assert result["error_code"] == "PROJECT_SETUP_INPUT_MISSING"
    assert result["data"]["setup_status"]["status"] == "BLOCK"
    assert result["data"]["setup_status"]["needs_fileset_repair"] is True
    assert result["next_actions"][0]["tool"] == "repair_project_setup"


def test_run_project_audit_timeout_returns_recovery_actions(tmp_path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    class AuditTimeoutSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if len(self.commands) == 1:
                return {
                    "ok": True,
                    "raw": (
                        "project_name=demo\n"
                        f"project_dir={project_dir}\n"
                        "part=xc7a35tcpg236-1\n"
                        "top=top\n"
                        "sim_top=tb_top\n"
                        f"filesets={encode_wire_list(['sources_1', 'sim_1'])}\n"
                        f"runs={encode_wire_list(['synth_1', 'impl_1'])}\n"
                        "bitstream_files="
                    ),
                }
            raise TimeoutError("audit stage timed out")

        def status(self) -> dict:
            return {"ok": True, "connected": False, "process_running": True, "runtime_dir": str(tmp_path / "runtime")}

    service = VivadoToolService(session=AuditTimeoutSession())

    result = service.call("run_project_audit", {"run_name": "impl_1", "timeout_s": 3})

    assert result["ok"] is False
    assert result["error_code"] == "TimeoutError"
    assert result["data"]["current_step"] == "analyze_sources"
    assert result["data"]["completed_steps"] == ["get_project_state"]
    assert result["data"]["partial_success"] is True
    assert result["data"]["project_path"] == str(project_dir / "demo.xpr")
    assert {"session_status", "stop_session", "start_session", "open_project", "run_project_audit"} <= {
        action["tool"] for action in result["next_actions"]
    }


def test_run_behavioral_simulation_uses_finite_runtime_by_default() -> None:
    fake = FakeSession(raw="status=completed\nlog_begin=__VMCP_LOG_BEGIN__\nINFO: [XSIM 43-3496] Simulation finished")
    service = VivadoToolService(session=fake)

    result = service.call("run_behavioral_simulation", {"export_vcd": True})

    assert result["ok"] is True
    assert result["tool"] == "run_behavioral_simulation"
    command = fake.commands[-1]
    assert "launch_simulation -simset {sim_1} -mode behavioral" in command
    assert "for {set vmcp_vcd_step 0}" in command
    assert "run {3906250fs}" in command
    assert "set vmcp_vcd_limit_stopped 1; break" in command
    assert "run all" not in command
    assert "open_vcd" in command
    assert "catch {log_vcd" in command
    assert "catch {close_sim}" in command
    assert result["data"]["status"] == "completed"


def test_run_behavioral_simulation_vcd_tcl_failure_returns_vcd_diagnosis() -> None:
    class VcdFailureSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.responses = [
                {"ok": True, "raw": "sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim\ntestbench_vcd_usage=0\ntestbench_vcd_sources="},
                {"ok": False, "raw": "ERROR: [Common 17-39] launch_simulation failed due to earlier errors"},
            ]

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return self.responses.pop(0)

    service = VivadoToolService(session=VcdFailureSession())

    result = service.call("run_behavioral_simulation", {"export_vcd": True, "run_time": "200 ns"})

    assert result["ok"] is False
    assert result["error_code"] == "SIMULATION_VCD_EXPORT_FAILED"
    assert result["data"]["simulation_diagnosis"]["primary_cause"] == "vcd_export_failed"
    assert result["next_actions"][0]["tool"] == "run_behavioral_simulation"
    assert result["next_actions"][0]["arg_sources"]["export_vcd"] == "false"


def test_run_behavioral_simulation_classifies_empty_xsim_script_as_single_retry_transient(tmp_path) -> None:
    sim_dir = tmp_path / "demo.sim" / "sim_1" / "behav" / "xsim"
    sim_dir.mkdir(parents=True)
    (sim_dir / "compile.bat").write_bytes(b"")

    class EmptyCompileScriptSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.responses = [
                {"ok": True, "raw": f"sim_dir={sim_dir}\ntestbench_vcd_usage=0\ntestbench_vcd_sources="},
                {"ok": False, "raw": "ERROR: [Vivado 12-4473] Detected error while running simulation."},
            ]

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return self.responses.pop(0)

    result = VivadoToolService(session=EmptyCompileScriptSession()).call(
        "run_behavioral_simulation",
        {"run_time": "200 ns", "export_vcd": False, "max_vcd_mb": 64},
    )

    assert result["ok"] is False
    assert result["error_code"] == "SIMULATION_XSIM_LAUNCH_TRANSIENT"
    assert result["data"]["retry_scope"] == "once"
    assert result["data"]["simulation_diagnosis"]["primary_cause"] == "xsim_generated_script_empty"
    assert result["data"]["empty_launch_scripts"][0]["path"].endswith("compile.bat")
    action = result["next_actions"][0]
    assert action["tool"] == "run_behavioral_simulation"
    assert action["arg_sources"]["run_time"] == "200 ns"
    assert action["arg_sources"]["max_vcd_mb"] == 64


def test_managed_empty_xsim_script_stops_session_and_routes_fresh_generation_recovery(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    runtime_dir = tmp_path / "runtime"
    sim_dir.mkdir(parents=True)
    runtime_dir.mkdir()
    (sim_dir / "compile.bat").write_bytes(b"")

    class EmptyCompileScriptSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self._runtime_path = runtime_dir
            self.responses = [
                {
                    "ok": True,
                    "raw": (
                        f"project_dir={project_dir}\nproject_name=demo\nsim_dir={sim_dir}\n"
                        "testbench_vcd_usage=0\ntestbench_vcd_sources="
                    ),
                },
                {"ok": False, "raw": "ERROR: [Vivado 12-4473] Detected error while running simulation."},
            ]

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return self.responses.pop(0)

    class ManagedSessionManager:
        def __init__(self) -> None:
            self.session = EmptyCompileScriptSession()
            self.stopped = False

        def current(self):
            return self.session

        def stop(self) -> dict:
            self.stopped = True
            return {"ok": True, "stopped": True, "process_running": False}

    manager = ManagedSessionManager()
    result = VivadoToolService(manager=manager).call(
        "run_behavioral_simulation",
        {"run_time": "200 ns", "export_vcd": False, "max_vcd_mb": 64},
    )

    assert result["error_code"] == "SIMULATION_XSIM_LAUNCH_TRANSIENT"
    assert result["data"]["abort_attempted"] is True
    assert result["data"]["managed_session_stopped"] is True
    assert result["data"]["runtime_dir"] == str(runtime_dir)
    assert result["data"]["project_path"] == str(project_dir / "demo.xpr")
    assert [action["tool"] for action in result["next_actions"][:3]] == ["start_session", "open_project", "run_behavioral_simulation"]
    assert manager.stopped is True


def test_run_behavioral_simulation_can_run_all_only_when_waveform_limit_is_explicitly_disabled() -> None:
    fake = FakeSession(
        raw=(
            "status=completed\n"
            "status_source=simulation_invocation_log_span\n"
            "log_begin=__VMCP_LOG_BEGIN__\n"
            "INFO: [XSIM 43-3496] Simulation finished"
        )
    )
    service = VivadoToolService(session=fake)

    result = service.call("run_behavioral_simulation", {"run_all": True, "max_vcd_mb": 0})

    assert result["ok"] is True
    assert "run all" in fake.commands[-1]


def test_run_behavioral_simulation_blocks_project_external_testbench_vcd(tmp_path) -> None:
    project_dir = tmp_path / "project"
    sim_dir = project_dir / "demo.sim" / "sim_1" / "behav" / "xsim"
    source = project_dir / "sim" / "tb.sv"
    source.parent.mkdir(parents=True)
    source.write_text('$dumpfile("../../../../../outside.vcd");\n$dumpvars;', encoding="utf-8")

    class ExternalDumpSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return {
                "ok": True,
                "raw": (
                    f"project_dir={project_dir}\nproject_name=demo\nsimset=sim_1\nsim_top=tb\n"
                    f"sim_files={encode_wire_list([str(source)])}\nsim_dir={sim_dir}\n"
                    f"testbench_vcd_usage=1\ntestbench_vcd_sources={encode_wire_list([str(source)])}"
                ),
            }

    fake = ExternalDumpSession()
    result = VivadoToolService(session=fake).call("run_behavioral_simulation", {})

    assert result["ok"] is False
    assert result["error_code"] == "SIMULATION_WAVEFORM_PATH_UNCONTROLLED"
    assert "escapes controlled simulation directory" in result["data"]["uncontrolled_reasons"][0]
    assert not any("launch_simulation" in command for command in fake.commands)


def test_managed_simulation_rejects_external_compile_source_before_launch(tmp_path, monkeypatch) -> None:
    trusted = tmp_path / "trusted"
    project = trusted / "project"
    outside = tmp_path / "outside" / "tb.sv"
    project.mkdir(parents=True)
    outside.parent.mkdir()
    outside.write_text("module tb; initial $finish; endmodule\n", encoding="utf-8")
    sim_dir = project / "demo.sim" / "sim_1" / "behav" / "xsim"

    class ManagedExternalSourceSession(GuiTcpVivadoSession):
        def __init__(self) -> None:
            super().__init__(generation_id="generation-test")
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return {
                "ok": True,
                "generation_id": self.generation_id,
                "raw": (
                    f"project_dir={project}\nproject_name=demo\nsimset=sim_1\nsim_top=tb\n"
                    f"sim_files={encode_wire_list([str(outside)])}\ninclude_dirs={encode_wire_list([])}\n"
                    f"verilog_defines={encode_wire_list([])}\ntarget_simulator=Vivado Simulator\n"
                    f"simulator_property_schema_version={XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION}\n"
                    f"simulator_options={encode_wire_list([])}\nsim_dir={sim_dir}\n"
                    "testbench_vcd_usage=0\ntestbench_vcd_sources=\npreflight_errors="
                ),
            }

    monkeypatch.setenv("VIVADO_AGENT_MCP_TRUSTED_PROJECT_ROOTS", str(trusted))
    session = ManagedExternalSourceSession()
    service = VivadoToolService(session=session)
    _bind_managed_project(service, project)
    result = service.call(
        "run_behavioral_simulation",
        {"execution_intent": "run reviewed project simulation", "confirm": "RUN_TRUSTED_XSIM"},
    )

    assert result["error_code"] == "SIMULATION_SOURCE_UNTRUSTED"
    assert len(session.commands) == 2
    assert "VMCP_EXECUTABLE_COMPOSITE_INPUT_BLOCKED" in session.commands[0]
    assert not any("launch_simulation" in command for command in session.commands)


def test_managed_simulation_rejects_external_host_input_before_launch(tmp_path, monkeypatch) -> None:
    trusted = tmp_path / "trusted"
    project = trusted / "project"
    outside = tmp_path / "outside" / "secret.hex"
    source = project / "sim" / "tb.sv"
    source.parent.mkdir(parents=True)
    outside.parent.mkdir()
    outside.write_text("ff\n", encoding="utf-8")
    source.write_text(
        f'module tb; reg [7:0] mem [0:1]; initial $readmemh("{outside.as_posix()}", mem); endmodule\n',
        encoding="utf-8",
    )
    sim_dir = project / "demo.sim" / "sim_1" / "behav" / "xsim"

    class ManagedExternalHostInputSession(GuiTcpVivadoSession):
        def __init__(self) -> None:
            super().__init__(generation_id="generation-test")
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return {
                "ok": True,
                "generation_id": self.generation_id,
                "raw": (
                    f"project_dir={project}\nproject_name=demo\nsimset=sim_1\nsim_top=tb\n"
                    f"sim_files={encode_wire_list([str(source)])}\ninclude_dirs={encode_wire_list([])}\n"
                    f"verilog_defines={encode_wire_list([])}\ntarget_simulator=Vivado Simulator\n"
                    f"simulator_property_schema_version={XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION}\n"
                    f"simulator_options={encode_wire_list([])}\nsim_dir={sim_dir}\n"
                    "testbench_vcd_usage=0\ntestbench_vcd_sources=\npreflight_errors="
                ),
            }

    monkeypatch.setenv("VIVADO_AGENT_MCP_TRUSTED_PROJECT_ROOTS", str(trusted))
    session = ManagedExternalHostInputSession()
    service = VivadoToolService(session=session)
    _bind_managed_project(service, project)
    result = service.call(
        "run_behavioral_simulation",
        {"execution_intent": "run reviewed project simulation", "confirm": "RUN_TRUSTED_XSIM"},
    )

    assert result["error_code"] == "SIMULATION_SOURCE_UNTRUSTED"
    assert result["data"]["trusted_execution_closure"]["host_input_count"] == 1
    assert len(session.commands) == 2
    assert "VMCP_EXECUTABLE_COMPOSITE_INPUT_BLOCKED" in session.commands[0]
    assert not any("launch_simulation" in command for command in session.commands)


def test_managed_simulation_rescans_host_effects_under_lock_before_launch(tmp_path, monkeypatch) -> None:
    trusted = tmp_path / "trusted"
    project = trusted / "project"
    source = project / "sim" / "tb.sv"
    trusted_input = project / "data" / "trusted.hex"
    outside_input = tmp_path / "outside" / "secret.hex"
    source.parent.mkdir(parents=True)
    project_path = project / "demo.xpr"
    project_path.write_text("# trusted test project\n", encoding="utf-8")
    trusted_input.parent.mkdir(parents=True)
    outside_input.parent.mkdir()
    trusted_input.write_text("01\n", encoding="utf-8")
    outside_input.write_text("ff\n", encoding="utf-8")
    source.write_text(
        f'module tb; reg [7:0] mem [0:1]; initial $readmemh("{trusted_input.as_posix()}", mem); endmodule\n',
        encoding="utf-8",
    )
    sim_dir = project / "demo.sim" / "sim_1" / "behav" / "xsim"

    class ManagedScanRaceSession(GuiTcpVivadoSession):
        def __init__(self) -> None:
            super().__init__(generation_id="generation-test")
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return {
                "ok": True,
                "generation_id": self.generation_id,
                "raw": (
                    f"project_dir={project}\nproject_name=demo\nsimset=sim_1\nsim_top=tb\n"
                    f"project_path={project_path}\nvivado_version_short=2021.2\nvivado_version_full=Vivado v2021.2\n"
                    f"project_properties={encode_wire_list(['PART=xc7a35tcpg236-1'])}\n"
                    f"simset_properties={encode_wire_list(['TOP=tb'])}\n"
                    f"sim_file_metadata={encode_wire_list([encode_wire_row({'path': str(source), 'file_type': 'SystemVerilog', 'used_in': 'simulation'})])}\n"
                    f"sim_files={encode_wire_list([str(source)])}\ninclude_dirs={encode_wire_list([])}\n"
                    f"verilog_defines={encode_wire_list([])}\ntarget_simulator=Vivado Simulator\n"
                    f"simulator_property_schema_version={XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION}\n"
                    f"simulator_options={encode_wire_list([])}\nsim_dir={sim_dir}\n"
                    "testbench_vcd_usage=0\ntestbench_vcd_sources=\npreflight_errors="
                ),
            }

    from vivado_agent_mcp.vivado.simulation import analyze_testbench_waveform_paths as real_analyze

    scan_count = 0

    def mutate_after_second_scan(preflight: dict) -> dict:
        nonlocal scan_count
        scan_count += 1
        result = real_analyze(preflight)
        if scan_count == 2:
            source.write_text(
                f'module tb; reg [7:0] mem [0:1]; initial $readmemh("{outside_input.as_posix()}", mem); endmodule\n',
                encoding="utf-8",
            )
        return result

    monkeypatch.setenv("VIVADO_AGENT_MCP_TRUSTED_PROJECT_ROOTS", str(trusted))
    monkeypatch.setattr("vivado_agent_mcp.tools.analyze_testbench_waveform_paths", mutate_after_second_scan)
    session = ManagedScanRaceSession()
    service = VivadoToolService(session=session)
    _bind_managed_project(service, project)
    result = service.call(
        "run_behavioral_simulation",
        {"execution_intent": "run reviewed project simulation", "confirm": "RUN_TRUSTED_XSIM"},
    )

    assert result["error_code"] == "SIMULATION_EXECUTION_EFFECTS_CHANGED_BEFORE_LAUNCH"
    assert result["data"]["execution_effects_delta"]["changed_fields"] == ["host_input_files"]
    assert result["data"]["execution_effects_delta"]["changes"]["host_input_files"]["before_count"] == 1
    assert result["data"]["execution_effects_delta"]["changes"]["host_input_files"]["locked_count"] == 1
    assert scan_count == 3
    assert len(session.commands) == 4
    assert not any("launch_simulation" in command for command in session.commands)


def test_managed_simulation_blocks_nonempty_generated_xsim_state(tmp_path, monkeypatch) -> None:
    trusted = tmp_path / "trusted"
    project = trusted / "project"
    source = project / "sim" / "tb.sv"
    project_path = project / "demo.xpr"
    sim_dir = project / "demo.sim" / "sim_1" / "behav" / "xsim"
    source.parent.mkdir(parents=True)
    sim_dir.mkdir(parents=True)
    source.write_text("module tb; initial $finish; endmodule\n", encoding="utf-8")
    project_path.write_text("# trusted test project\n", encoding="utf-8")
    (sim_dir / "stale_generated.tcl").write_text("puts stale\n", encoding="utf-8")

    class ManagedStaleSimulationSession(GuiTcpVivadoSession):
        def __init__(self) -> None:
            super().__init__(generation_id="generation-test")
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return {
                "ok": True,
                "generation_id": self.generation_id,
                "raw": (
                    f"project_dir={project}\nproject_name=demo\nsimset=sim_1\nsim_top=tb\n"
                    f"project_path={project_path}\nvivado_version_short=2021.2\nvivado_version_full=Vivado v2021.2\n"
                    f"project_properties={encode_wire_list(['PART=xc7a35tcpg236-1'])}\n"
                    f"simset_properties={encode_wire_list(['TOP=tb'])}\n"
                    f"sim_file_metadata={encode_wire_list([encode_wire_row({'path': str(source), 'file_type': 'SystemVerilog', 'used_in': 'simulation'})])}\n"
                    f"sim_files={encode_wire_list([str(source)])}\ninclude_dirs={encode_wire_list([])}\n"
                    f"verilog_defines={encode_wire_list([])}\ntarget_simulator=Vivado Simulator\n"
                    f"simulator_property_schema_version={XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION}\n"
                    f"simulator_options={encode_wire_list([])}\nsim_dir={sim_dir}\n"
                    "testbench_vcd_usage=0\ntestbench_vcd_sources=\npreflight_errors="
                ),
            }

    monkeypatch.setenv("VIVADO_AGENT_MCP_TRUSTED_PROJECT_ROOTS", str(trusted))
    session = ManagedStaleSimulationSession()
    service = VivadoToolService(session=session)
    _bind_managed_project(service, project)
    result = service.call(
        "run_behavioral_simulation",
        {"execution_intent": "run reviewed project simulation", "confirm": "RUN_TRUSTED_XSIM"},
    )

    assert result["error_code"] == "SIMULATION_GENERATED_STATE_NOT_CLEAN"
    assert len(session.commands) == 3
    assert not any("launch_simulation" in command for command in session.commands)


def test_managed_simulation_rejects_every_executable_xsim_property_before_launch(tmp_path, monkeypatch) -> None:
    trusted = tmp_path / "trusted"
    property_names = (
        "xsim.compile.tcl.pre",
        "xsim.compile.xvlog.more_options",
        "xsim.compile.xvhdl.more_options",
        "xsim.compile.xsc.more_options",
        "xsim.elaborate.xelab.more_options",
        "xsim.simulate.tcl.post",
        "xsim.simulate.custom_tcl",
        "xsim.simulate.xsim.more_options",
    )

    class ManagedExecutableOptionSession(GuiTcpVivadoSession):
        def __init__(self, property_name: str, project, source, sim_dir) -> None:
            super().__init__(generation_id="generation-test")
            self.property_name = property_name
            self.project = project
            self.source = source
            self.sim_dir = sim_dir
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            option_row = encode_wire_row({"property": self.property_name, "value": "evil.tcl"})
            return {
                "ok": True,
                "generation_id": self.generation_id,
                "raw": (
                    f"project_dir={self.project}\nproject_name=demo\nsimset=sim_1\nsim_top=tb\n"
                    f"sim_files={encode_wire_list([str(self.source)])}\ninclude_dirs={encode_wire_list([])}\n"
                    f"verilog_defines={encode_wire_list([])}\ntarget_simulator=Vivado Simulator\n"
                    f"simulator_property_schema_version={XSIM_EXECUTABLE_PROPERTY_SCHEMA_VERSION}\n"
                    f"simulator_options={encode_wire_list([option_row])}\nsim_dir={self.sim_dir}\n"
                    "testbench_vcd_usage=0\ntestbench_vcd_sources=\npreflight_errors="
                ),
            }

    monkeypatch.setenv("VIVADO_AGENT_MCP_TRUSTED_PROJECT_ROOTS", str(trusted))
    for index, property_name in enumerate(property_names):
        project = trusted / f"project_{index}"
        source = project / "sim" / "tb.sv"
        source.parent.mkdir(parents=True)
        source.write_text("module tb; initial $finish; endmodule\n", encoding="utf-8")
        sim_dir = project / "demo.sim" / "sim_1" / "behav" / "xsim"
        session = ManagedExecutableOptionSession(property_name, project, source, sim_dir)
        service = VivadoToolService(session=session)
        _bind_managed_project(service, project)
        result = service.call(
            "run_behavioral_simulation",
            {"execution_intent": "run reviewed project simulation", "confirm": "RUN_TRUSTED_XSIM"},
        )
        assert result["error_code"] == "SIMULATION_SOURCE_UNTRUSTED", property_name
        assert len(session.commands) == 2, property_name
        assert not any("launch_simulation" in command for command in session.commands), property_name


def test_run_behavioral_simulation_failed_testbench_returns_repair_actions() -> None:
    fake = FakeSession(
        raw=(
            "sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim\n"
            "log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xsim.log\n"
            "status_source=simulation_invocation_log_span\n"
            "log_span_start=128\n"
            "log_span_end=256\n"
            "log_begin=__VMCP_LOG_BEGIN__\n"
            "TEST FAIL expected=4 observed=1\n"
            "INFO: [XSIM 43-3496] Simulation finished"
        )
    )
    service = VivadoToolService(session=fake)

    result = service.call("run_behavioral_simulation", {"run_time": "200 ns", "export_vcd": False})

    assert result["ok"] is False
    assert result["error_code"] == "SIMULATION_FAILED"
    assert result["data"]["status"] == "failed"
    assert result["data"]["simulation_diagnosis"]["primary_cause"] == "testbench_failure"
    assert result["data"]["status_source"] == "simulation_invocation_log_span"
    assert result["data"]["log_span"]["start"] == 128
    fileset_actions = {
        (action["tool"], action.get("arg_sources", {}).get("fileset"))
        for action in result["next_actions"]
        if isinstance(action.get("arg_sources"), dict)
    }
    assert {action["tool"] for action in result["next_actions"]} >= {
        "get_simulation_result",
        "analyze_sources",
        "check_syntax",
        "get_compile_order",
        "run_behavioral_simulation",
    }
    assert ("analyze_sources", "sim_1") in fileset_actions
    assert ("check_syntax", "sim_1") in fileset_actions
    assert ("analyze_sources", "sources_1") in fileset_actions
    assert ("check_syntax", "sources_1") in fileset_actions


def test_run_behavioral_simulation_blocks_run_all_with_vcd_risk() -> None:
    fake = FakeSession(raw="sim_dir=D:/demo/demo.sim/sim_1/behav/xsim\ntestbench_vcd_usage=0\ntestbench_vcd_sources=")
    service = VivadoToolService(session=fake)

    result = service.call("run_behavioral_simulation", {"run_all": True, "export_vcd": True})

    assert result["ok"] is False
    assert result["error_code"] == "SIMULATION_RUN_ALL_VCD_BLOCKED"
    assert result["data"]["vcd_risk"] is True
    assert result["next_actions"][0]["tool"] == "run_behavioral_simulation"
    assert not any("launch_simulation" in command for command in fake.commands)


def test_run_behavioral_simulation_avoids_extra_open_vcd_when_testbench_dumps() -> None:
    class VcdDumpSession:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "$dumpfile" in command:
                    return {"ok": True, "raw": f"sim_dir=D:/demo/demo.sim/sim_1/behav/xsim\ntestbench_vcd_usage=1\ntestbench_vcd_sources={encode_wire_list(['D:/demo/sim/tb.sv'])}"}
            return {
                "ok": True,
                "raw": (
                    "sim_dir=D:/demo/demo.sim/sim_1/behav/xsim\n"
                    "log_path=D:/demo/demo.sim/sim_1/behav/xsim/xsim.log\n"
                    "vcd_conflict=1\n"
                    "vcd_export_mode=testbench_existing\n"
                    "vcd_total_bytes=128\n"
                        f"vcd_files={encode_wire_list([encode_wire_row({'path': 'D:/demo/demo.sim/sim_1/behav/xsim/tb.vcd', 'size_bytes': '128'})])}\n"
                    "log_begin=__VMCP_LOG_BEGIN__\n"
                    "INFO: [XSIM 43-3496] Simulation finished"
                ),
            }

    fake = VcdDumpSession()
    service = VivadoToolService(session=fake)

    result = service.call("run_behavioral_simulation", {"export_vcd": True})

    assert result["ok"] is True
    assert result["data"]["vcd_conflict"] is True
    assert result["data"]["vcd_conflict_severity"] == "info"
    assert result["data"]["vcd_export_mode"] == "testbench_existing"
    assert result["data"]["mcp_vcd_export_mode"] == "testbench_existing"
    assert result["data"]["export_vcd_requested"] is True
    assert result["data"]["preflight_testbench_vcd_usage"] is True
    assert result["data"]["testbench_vcd_usage"] is True
    assert result["data"]["testbench_vcd_detected"] is True
    assert result["data"]["simulation_diagnosis"]["primary_cause"] == "completed_with_testbench_vcd"
    assert "open_vcd" not in fake.commands[-1]


def test_run_behavioral_simulation_reports_testbench_vcd_without_mcp_export() -> None:
    class TestbenchOnlyVcdSession:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "$dumpfile" in command:
                    return {"ok": True, "raw": f"sim_dir=D:/demo/demo.sim/sim_1/behav/xsim\ntestbench_vcd_usage=1\ntestbench_vcd_sources={encode_wire_list(['D:/demo/sim/tb.sv'])}"}
            return {
                "ok": True,
                "raw": (
                    "sim_dir=D:/demo/demo.sim/sim_1/behav/xsim\n"
                    "log_path=D:/demo/demo.sim/sim_1/behav/xsim/xsim.log\n"
                    "vcd_export_mode=testbench_existing\n"
                    "mcp_vcd_export_mode=testbench_existing\n"
                    "export_vcd_requested=0\n"
                    "testbench_vcd_usage=1\n"
                        f"vcd_files={encode_wire_list([encode_wire_row({'path': 'D:/demo/demo.sim/sim_1/behav/xsim/tb.vcd', 'size_bytes': '128'})])}\n"
                    "log_begin=__VMCP_LOG_BEGIN__\n"
                    "INFO: [XSIM 43-3496] Simulation finished"
                ),
            }

    fake = TestbenchOnlyVcdSession()
    service = VivadoToolService(session=fake)

    result = service.call("run_behavioral_simulation", {"export_vcd": False})

    assert result["ok"] is True
    assert result["data"]["testbench_vcd_usage"] is True
    assert result["data"]["testbench_vcd_detected"] is True
    assert result["data"]["vcd_conflict_severity"] == "info"
    assert result["data"]["preflight_testbench_vcd_usage"] is True
    assert result["data"]["export_vcd_requested"] is False
    assert result["data"]["mcp_vcd_export_mode"] == "testbench_existing"
    assert result["data"]["simulation_diagnosis"]["primary_cause"] == "completed_with_testbench_vcd"
    assert "open_vcd" not in fake.commands[-1]


def test_run_behavioral_simulation_accepts_current_tb_pass_after_gui_run_tcl_error() -> None:
    class GuiBreakpointSession:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "$dumpfile" in command:
                return {
                    "ok": True,
                    "raw": "sim_dir=D:/demo/demo.sim/sim_1/behav/xsim\ntestbench_vcd_usage=0\ntestbench_vcd_sources=",
                }
            return {
                "ok": True,
                "raw": (
                    "sim_dir=D:/demo/demo.sim/sim_1/behav/xsim\n"
                    "log_path=D:/demo/demo.sim/sim_1/behav/xsim/xsim.log\n"
                    "status_source=simulation_invocation_log_span\n"
                    "run_tcl_failed=1\n"
                    f"run_error={encode_wire_list(['ERROR: [Common 17-190] Invalid Tcl eval of add_bp during event processing.'])}\n"
                    "breakpoints_cleared=1\n"
                    "log_begin=__VMCP_LOG_BEGIN__\n"
                    "TB_PASS transitions=63\n"
                    "$finish called at time : 100 ns"
                ),
            }

    fake = GuiBreakpointSession()
    service = VivadoToolService(session=fake)

    result = service.call("run_behavioral_simulation", {"run_time": "200 ns", "export_vcd": False})

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["run_tcl_failed"] is True
    assert result["data"]["breakpoints_cleared"] is True
    assert any(
        item["code"] == "SIMULATION_RUN_TCL_ERROR"
        for item in result["data"]["simulation_diagnosis"]["warnings"]
    )
    assert "remove_bps -all -quiet" in fake.commands[-1]


def test_run_behavioral_simulation_fails_when_vcd_limit_is_exceeded() -> None:
    class LargeVcdSession:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "$dumpfile" in command:
                return {"ok": True, "raw": "sim_dir=D:/demo/demo.sim/sim_1/behav/xsim\ntestbench_vcd_usage=0\ntestbench_vcd_sources="}
            return {
                "ok": True,
                "raw": (
                    "sim_dir=D:/demo/demo.sim/sim_1/behav/xsim\n"
                    "log_path=D:/demo/demo.sim/sim_1/behav/xsim/xsim.log\n"
                    "vcd_limit_bytes=268435456\n"
                    "vcd_limit_exceeded=1\n"
                    "vcd_total_bytes=268435457\n"
                    "vcd_largest_file=D:/demo/demo.sim/sim_1/behav/xsim/vmcp_behav.vcd\n"
                    "vcd_largest_bytes=268435457\n"
                        f"vcd_files={encode_wire_list([encode_wire_row({'path': 'D:/demo/demo.sim/sim_1/behav/xsim/vmcp_behav.vcd', 'size_bytes': '268435457'})])}\n"
                    "log_begin=__VMCP_LOG_BEGIN__\n"
                    "INFO: [XSIM 43-3496] Simulation finished"
                ),
            }

    service = VivadoToolService(session=LargeVcdSession())

    result = service.call("run_behavioral_simulation", {"export_vcd": True, "max_vcd_mb": 256})

    assert result["ok"] is False
    assert result["error_code"] == "SIMULATION_VCD_LIMIT_EXCEEDED"
    assert result["data"]["artifacts"]["vcd_total_bytes"] == 268435457
    assert result["data"]["diagnosis"]["primary_cause"] == "vcd_limit_exceeded"
    assert result["next_actions"][0]["tool"] == "run_behavioral_simulation"


def test_run_behavioral_simulation_timeout_returns_recoverable_context() -> None:
    class TimeoutSession:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "$dumpfile" in command:
                return {"ok": True, "raw": "sim_dir=D:/demo/demo.sim/sim_1/behav/xsim\ntestbench_vcd_usage=0\ntestbench_vcd_sources="}
            raise TimeoutError("simulation timed out")

    class TimeoutManager:
        def __init__(self) -> None:
            self.session = TimeoutSession()
            self.stopped = False

        def current(self) -> TimeoutSession:
            return self.session

        def stop(self) -> dict:
            self.stopped = True
            return {"ok": True, "stopped": True}

    manager = TimeoutManager()
    service = VivadoToolService(manager=manager)

    result = service.call("run_behavioral_simulation", {"export_vcd": True, "timeout_s": 1})

    assert result["ok"] is False
    assert result["error_code"] == "TimeoutError"
    assert result["data"]["sim_dir"] == "D:/demo/demo.sim/sim_1/behav/xsim"
    assert result["data"]["timeout_s_used"] == 1
    assert result["data"]["abort_attempted"] is True
    assert result["data"]["managed_session_stopped"] is True
    assert result["data"]["simulation_diagnosis"]["primary_cause"] == "timeout"
    assert manager.stopped is True


def test_get_simulation_result_parses_xsim_log() -> None:
    fake = FakeSession(
        raw=(
            "sim_dir=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim\n"
            "log_path=D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/xsim.log\n"
            f"wdb_files={encode_wire_list(['D:/Vivado_Mcp/test_use/demo/demo.sim/sim_1/behav/xsim/top.wdb'])}\n"
            "vcd_files=\n"
            "log_begin=__VMCP_LOG_BEGIN__\n"
            "ERROR: [XSIM 43-3225] Cannot find design unit"
        )
    )
    service = VivadoToolService(session=fake)

    result = service.call("get_simulation_result", {})

    assert result["ok"] is True
    assert result["data"]["status"] == "failed"
    assert result["data"]["counts"]["ERROR"] == 1
    assert "xsim.log" in fake.commands[0]


def test_get_simulation_result_blocks_stale_managed_session_evidence(tmp_path) -> None:
    source = tmp_path / "tb.sv"
    source.write_text("module tb; initial $finish; endmodule\n", encoding="utf-8")

    class ManagedSimulationSession(GuiTcpVivadoSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            return {
                "ok": True,
                "generation_id": self.generation_id,
                "raw": (
                    "simulation_invocation_id=sim-old\n"
                    f"session_generation_id={self.generation_id}\n"
                    f"simulation_source_identity_sha256={'0' * 64}\n"
                    "log_begin=__VMCP_LOG_BEGIN__\nTB_PASS\n"
                ),
            }

    session = ManagedSimulationSession(generation_id="generation-current")
    service = VivadoToolService(session=session)
    service._simulation_vcd_preflight = lambda **_: {
        "status": "READY",
        "project_dir": str(tmp_path),
        "project_name": "demo",
        "simset": "sim_1",
        "sim_top": "tb",
        "sim_files": [str(source)],
        "include_dirs": [],
        "verilog_defines": [],
        "target_simulator": "XSim",
        "preflight_errors": [],
    }

    result = service.call("get_simulation_result", {})

    assert result["ok"] is False
    assert result["error_code"] == "SIMULATION_EVIDENCE_STALE"
    assert result["assessment_status"] == "BLOCK"
    assert result["stop_required"] is True
    assert result["data"]["evidence_freshness"]["status"] == "STALE"


def test_create_ip_tool_returns_structured_ip_data(tmp_path) -> None:
    fake = FakeSession(raw=encode_wire_row({'name': 'const_0', 'xci_path': 'D:/Vivado_Mcp/test_use/project/const_0.xci', 'locked': '0', 'upgrade_available': '0'}))
    service = VivadoToolService(session=fake)

    result = service.call(
        "create_ip",
        {
            "vlnv": "xilinx.com:ip:xlconstant:1.1",
            "module_name": "const_0",
            "ip_dir": str(tmp_path / "ip dir"),
            "properties": {"CONST_WIDTH": "1", "CONFIG.CONST_VAL": "1"},
        },
    )

    assert result["ok"] is True
    assert result["tool"] == "create_ip"
    assert result["data"]["ip"]["name"] == "const_0"
    assert result["data"]["ip"]["vlnv"] == "xilinx.com:ip:xlconstant:1.1"
    assert "create_ip -vlnv {xilinx.com:ip:xlconstant:1.1}" in fake.commands[0]
    assert "set_property -dict [list {CONFIG.CONST_WIDTH} {1} {CONFIG.CONST_VAL} {1}]" in fake.commands[0]


def test_ip_tools_return_tcl_failure() -> None:
    fake = FakeSession(raw="ERROR: IP failed", ok=False)
    service = VivadoToolService(session=fake)

    result = service.call(
        "configure_ip",
        {"ip_name": "const_0", "properties": {"CONST_VAL": "1"}},
    )

    assert result["ok"] is False
    assert result["error_code"] == "TCL_FAILED"
    assert "IP failed" in result["raw_excerpt"]


def test_block_design_tools_build_tcl_and_parse_validation() -> None:
    fake = FakeSession(raw="status=VALID\nraw_begin=__VMCP_BD_VALIDATE_BEGIN__\nINFO: [BD 5-1] OK")
    service = VivadoToolService(session=fake)

    create_result = service.call("create_block_design", {"name": "design_1", "force": False})
    add_result = service.call(
        "add_bd_ip_cell",
        {
            "vlnv": "xilinx.com:ip:xlconstant:1.1",
            "cell_name": "const_0",
            "properties": {"CONST_WIDTH": "1"},
        },
    )
    port_result = service.call("create_bd_port", {"name": "led", "direction": "O", "from": 0, "to": 0})
    conn_result = service.call("connect_bd_net", {"source": "const_0/dout", "targets": ["led"]})
    validate_result = service.call("validate_block_design", {"bd_name": "design_1"})

    assert create_result["ok"] is True
    assert add_result["ok"] is True
    assert port_result["ok"] is True
    assert conn_result["ok"] is True
    assert validate_result["data"]["status"] == "VALID"
    assert "create_bd_design {design_1}" in fake.commands[0]
    assert "create_bd_cell -type ip -vlnv {xilinx.com:ip:xlconstant:1.1} {const_0}" in fake.commands[1]
    assert "create_bd_port -dir {O} -from {0} -to {0} {led}" in fake.commands[2]
    assert "connect_bd_net $source_obj $target_objs" in fake.commands[3]


def test_generate_block_design_wrapper_tool_sets_top() -> None:
    fake = FakeSession(raw=f"wrapper_files={encode_wire_list(['D:/Vivado_Mcp/test_use/project/project.gen/sources_1/bd/design_1/hdl/design_1_wrapper.v'])}\ntop=design_1_wrapper")
    service = VivadoToolService(session=fake)

    result = service.call(
        "generate_block_design_wrapper",
        {"bd_name": "design_1", "wrapper_top": "design_1_wrapper", "set_top": True},
    )

    assert result["ok"] is True
    assert result["data"]["wrapper"]["top"] == "design_1_wrapper"
    assert "make_wrapper -files $bd_file -top" in fake.commands[0]
    assert "set_property top {design_1_wrapper} [current_fileset]" in fake.commands[0]


def test_constraint_diagnostic_tools_return_structured_data() -> None:
    fake = FakeSession(raw="1. checking no_clock (1)\n5. checking no_input_delay (2)")
    service = VivadoToolService(session=fake)

    result = service.call("check_timing_constraints", {})

    assert result["ok"] is True
    assert result["tool"] == "check_timing_constraints"
    assert result["data"]["counts"]["no_clock"] == 1
    assert result["data"]["counts"]["no_input_delay"] == 2
    assert result["data"]["status"] == "BLOCK"
    assert "check_timing -return_string" in fake.commands[0]
    assert "vmcp_report_begin=__VMCP_CHECK_TIMING_REPORT_BEGIN__" in fake.commands[0]


def test_create_managed_xdc_tool_uses_python_managed_write_and_does_not_edit_user_xdc(tmp_path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    class ManagedXdcSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "project_name=" in command and "project_dir=" in command:
                return {"ok": True, "raw": f"project_name=demo\nproject_dir={project_dir}"}
            return {
                "ok": True,
                "raw": f"path={project_dir / 'vmcp_constraints' / 'managed.xdc'}\nfileset=constrs_1\nconstraint_count=1",
            }

    fake = ManagedXdcSession()
    service = VivadoToolService(session=fake)

    result = service.call(
        "create_managed_xdc",
        {
            "name": "managed",
            "constraints": [{"type": "create_clock", "name": "sys_clk", "period": 10.0, "port": "clk"}],
        },
    )

    assert result["ok"] is True
    assert result["data"]["managed_xdc"]["path"].endswith("managed.xdc")
    assert result["data"]["filesystem_backend"] == "python_atomic_managed_write"
    assert (project_dir / "vmcp_constraints" / "managed.xdc").is_file()
    assert "open $xdc_path" not in "\n".join(fake.commands)
    assert "add_files -fileset {constrs_1} $xdc_path" in fake.commands[1]


def test_create_managed_xdc_tool_rejects_path_like_name_before_tcl() -> None:
    fake = FakeSession(raw="")
    service = VivadoToolService(session=fake)

    result = service.call(
        "create_managed_xdc",
        {
            "name": "../outside",
            "constraints": [{"type": "create_clock", "name": "sys_clk", "period": 10.0, "port": "clk"}],
        },
    )

    assert result["ok"] is False
    assert result["tool"] == "create_managed_xdc"
    assert result["error_code"] == "ValueError"
    assert "file name" in result["message"]
    assert fake.commands == []


def test_create_managed_xdc_blocks_if_generator_drifts_outside_trusted_policy(monkeypatch) -> None:
    fake = FakeSession(raw="")
    service = VivadoToolService(session=fake)

    monkeypatch.setattr(
        "vivado_agent_mcp.tools.managed_xdc_payload",
        lambda **_kwargs: {
            "filename": "managed.xdc",
            "constraint_count": 1,
            "content": "exec cmd.exe /c echo pwned\n",
            "content_bytes": b"exec cmd.exe /c echo pwned\n",
        },
    )

    result = service.call(
        "create_managed_xdc",
        {
            "name": "managed",
            "constraints": [{"type": "create_clock", "name": "sys_clk", "period": 10.0, "port": "clk"}],
        },
    )

    assert result["ok"] is False
    assert result["error_code"] == "MANAGED_XDC_POLICY_MISMATCH"
    assert result["data"]["policy_allowed"] is False
    assert fake.commands == []


def test_analyze_timing_closure_aggregates_report_findings() -> None:
    class RoutingSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "report_timing_summary" in command:
                return {
                    "ok": True,
                    "raw": _attested_report(
                        "timing_summary",
                        "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__",
                        "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n-0.100 -0.200 0.010 0.000",
                    ),
                }
            if "check_timing" in command:
                return {"ok": True, "raw": "1. checking no_clock (1)"}
            if "report_methodology" in command:
                return {"ok": True, "raw": "WARNING: [Methodology 1-1] Review constraints"}
            if "report_drc" in command:
                return {"ok": True, "raw": ""}
            if "check_timing" in command:
                return {"ok": True, "raw": ""}
            if "report_methodology" in command:
                return {"ok": True, "raw": ""}
            if "runme.log" in command:
                return {"ok": True, "raw": ""}
            return {"ok": True, "raw": ""}

    fake = RoutingSession()
    service = VivadoToolService(session=fake)

    result = service.call("analyze_timing_closure", {})

    assert result["ok"] is True
    assert result["data"]["status"] == "BLOCK"
    assert result["data"]["findings"][0]["code"] == "TIMING_NOT_MET"


def test_strict_readiness_collects_check_timing_and_methodology() -> None:
    class RoutingSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "report_timing_summary" in command:
                return {
                    "ok": True,
                    "raw": _attested_report(
                        "timing_summary",
                        "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__",
                        "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.010 0.000",
                    ),
                }
            if "report_drc" in command:
                return {"ok": True, "raw": ""}
            if "check_timing" in command:
                return {"ok": True, "raw": "1. checking no_clock (1)"}
            if "report_methodology" in command:
                return {"ok": True, "raw": ""}
            if "runme.log" in command:
                return {"ok": True, "raw": ""}
            return {"ok": True, "raw": ""}

    fake = RoutingSession()
    service = VivadoToolService(session=fake)

    result = service.call("check_bitstream_readiness", {})

    assert result["ok"] is True
    assert result["data"]["status"] == "BLOCK"
    assert "check_timing reports no_clock=1" in result["data"]["reasons"]
    assert any("check_timing" in command for command in fake.commands)
    assert any("report_methodology" in command for command in fake.commands)


def test_create_project_returns_tcl_failure(tmp_path) -> None:
    fake = FakeSession(raw="ERROR: create_project failed", ok=False)
    service = VivadoToolService(session=fake)

    result = service.call(
        "create_project",
        {
            "project_name": "demo",
            "project_dir": str(tmp_path / "project"),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [],
        },
    )

    assert result["ok"] is False
    assert result["error_code"] == "TCL_FAILED"
    assert "create_project failed" in result["raw_excerpt"]


def test_run_synthesis_launches_async_without_waiting_by_default() -> None:
    fake = FakeSession(raw="")
    service = VivadoToolService(session=fake)

    result = service.call("run_synthesis", {})

    assert result["ok"] is True
    assert len(fake.commands) == 1
    assert "list_property $vmcp_guard_run" in fake.commands[0]
    assert "VMCP_RUN_HOOK_BLOCKED" in fake.commands[0]
    assert fake.commands[0].endswith("launch_runs {synth_1}")
    assert "wait_on_run" not in fake.commands[0]
    assert result["data"]["run_name"] == "synth_1"
    assert result["data"]["state"] == "launched"


def test_generate_bitstream_launches_to_write_bitstream_without_waiting_by_default() -> None:
    fake = FakeSession(raw="")
    service = VivadoToolService(session=fake)

    result = service.call("generate_bitstream", {})

    assert result["ok"] is True
    assert len(fake.commands) == 1
    assert "VMCP_RUN_HOOK_BLOCKED" in fake.commands[0]
    assert fake.commands[0].endswith("launch_runs {impl_1} -to_step write_bitstream")
    assert "wait_on_run" not in fake.commands[0]
    assert result["data"]["operation"] == "bitstream"
    assert result["data"]["launch_id"].startswith("run_")
    assert result["data"]["launch_started_at"]
    assert result["next_actions"][0]["tool"] == "get_run_progress"
    assert result["next_actions"][0]["required_args"] == ["run_name"]


def test_get_run_progress_returns_key_values_from_tcl_result_not_puts() -> None:
    fake = FakeSession(raw="status=Running synth_design\nprogress=37%\nneeds_refresh=0\nbitstream_exists=0\nbitstream_files=vmcp_hex_list_v1:")
    service = VivadoToolService(session=fake)

    result = service.call("get_run_progress", {"run_name": "synth_1"})

    assert result["ok"] is True
    assert "puts" not in fake.commands[0]
    assert "get_runs -quiet {synth_1}" in fake.commands[0]
    assert result["data"]["progress"]["status"] == "Running synth_design"
    assert result["data"]["progress"]["progress"] == "37%"
    assert result["data"]["progress"]["needs_refresh"] == "0"
    assert result["data"]["progress"]["bitstream_exists"] == "0"
    assert result["data"]["progress"]["bitstream_files"] == []
    assert result["data"]["progress"]["wire_trust"] == "VERSIONED"
    assert "::vivado_agent_mcp_wire_list $bit_files" in fake.commands[0]
    assert result["data"]["terminal"] is False
    assert result["data"]["phase"] == "synthesis"
    assert result["data"]["percent"] == 37.0


def test_get_run_progress_rejects_legacy_bitstream_file_transport() -> None:
    fake = FakeSession(
        raw=(
            "status=write_bitstream Complete!\nprogress=100%\nneeds_refresh=0\n"
            "bitstream_exists=1\nbitstream_files=D:/demo/top.bit"
        )
    )
    service = VivadoToolService(session=fake)

    result = service.call("get_run_progress", {"run_name": "impl_1", "expect_bitstream": True})

    assert result["ok"] is False
    assert result["error_code"] == "RUN_PROGRESS_WIRE_PROTOCOL_INVALID"
    assert result["data"]["wire_trust"] == "INVALID"


def test_get_run_progress_can_wait_for_expected_bitstream_file() -> None:
    fake = FakeSession(raw="status=route_design Complete!\nprogress=100%\nneeds_refresh=0\nbitstream_exists=0\nbitstream_files=vmcp_hex_list_v1:")
    service = VivadoToolService(session=fake)

    result = service.call("get_run_progress", {"run_name": "impl_1", "expect_bitstream": True})

    assert result["ok"] is True
    assert "bitstream_exists" in fake.commands[0]
    assert result["data"]["state"] == "not_started"
    assert result["data"]["terminal"] is False
    assert result["next_actions"][0]["tool"] == "generate_bitstream"


def test_get_run_progress_treats_prelaunch_route_status_as_bitstream_launch_transition() -> None:
    class LaunchTransitionSession(FakeSession):
        def __init__(self) -> None:
            super().__init__(raw="")
            self.responses = [
                "",
                "status=route_design Complete!\nprogress=100%\nneeds_refresh=0\nbitstream_exists=0\nbitstream_files=vmcp_hex_list_v1:",
                "status=Running write_bitstream...\nprogress=92%\nneeds_refresh=0\nbitstream_exists=0\nbitstream_files=vmcp_hex_list_v1:",
                "status=write_bitstream Complete!\nprogress=100%\nneeds_refresh=0\nbitstream_exists=1\nbitstream_files=vmcp_hex_list_v1:443a2f64656d6f2f746f702e626974",
            ]

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return {"ok": True, "raw": self.responses.pop(0), "generation_id": "generation-1"}

    fake = LaunchTransitionSession()
    service = VivadoToolService(session=fake)

    launched = service.call("generate_bitstream", {"run_name": "impl_1"})
    transition = service.call("get_run_progress", {"run_name": "impl_1", "expect_bitstream": True})
    running = service.call("get_run_progress", {"run_name": "impl_1", "expect_bitstream": True})
    complete = service.call("get_run_progress", {"run_name": "impl_1", "expect_bitstream": True})

    assert launched["ok"] is True
    assert transition["data"]["state"] == "launching"
    assert transition["data"]["terminal"] is False
    assert transition["data"]["phase"] == "bitstream"
    assert transition["data"]["launch_transition_pending"] is True
    assert transition["data"]["run_launch"]["launch_id"] == launched["data"]["launch_id"]
    assert running["data"]["state"] == "running"
    assert running["data"]["terminal"] is False
    assert complete["data"]["state"] == "complete"
    assert complete["data"]["terminal"] is True
    assert complete["data"]["progress"]["bitstream_files"] == ["D:/demo/top.bit"]
    assert complete["next_actions"][0]["tool"] == "collect_build_artifacts"


def test_get_run_progress_blocks_source_closure_change_between_launch_and_terminal(tmp_path: Path) -> None:
    class TerminalManagedSession(GuiTcpVivadoSession):
        def __init__(self) -> None:
            super().__init__(generation_id="terminal-identity-generation")

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            return {
                "ok": True,
                "raw": (
                    "status=synth_design Complete!\nprogress=100%\nneeds_refresh=0\n"
                    "bitstream_exists=0\nbitstream_files=vmcp_hex_list_v1:"
                ),
                "generation_id": self.generation_id,
            }

        def status(self, *, probe_connection: bool = True) -> dict:
            return {"ok": True, "connected": True, "process_running": True, "generation_id": self.generation_id}

    session = TerminalManagedSession()
    service = VivadoToolService(session=session)
    _bind_managed_project(service, tmp_path / "project-a")
    launch_identity = {"status": "READY", "sha256": "d" * 64, "identity": {"top": "demo_top"}}
    terminal_identity = {"status": "READY", "sha256": "e" * 64, "identity": {"top": "demo_top"}}
    service._run_launches["synth_1"] = {
        "launch_id": "run-source-closure-test",
        "operation": "synthesis",
        "started_at": "2026-07-20T00:00:00Z",
        "monotonic_started_at": 0.0,
        "generation_id": session.generation_id,
        "design_execution_identity": launch_identity,
    }
    service._capture_design_execution_identity = lambda **_kwargs: success(
        "design_execution_identity",
        "Captured terminal design identity.",
        {"design_execution_identity": terminal_identity},
    )

    result = service.call("get_run_progress", {"run_name": "synth_1"})

    assert result["ok"] is False
    assert result["error_code"] == "SOURCE_CLOSURE_CHANGED"
    assert result["data"]["launch_design_execution_identity"] == launch_identity
    assert result["data"]["terminal_design_execution_identity"] == terminal_identity
    assert result["stop_required"] is True


def test_get_run_progress_normalizes_terminal_bitstream_status() -> None:
    fake = FakeSession(raw="status=Running write_bitstream...\nprogress=83.333333%\nneeds_refresh=0\nbitstream_exists=1\nbitstream_files=vmcp_hex_list_v1:443a2f64656d6f2f746f702e626974")
    service = VivadoToolService(session=fake)

    result = service.call("get_run_progress", {"run_name": "impl_1", "expect_bitstream": True})

    assert result["ok"] is True
    assert result["data"]["state"] == "running"
    assert result["data"]["normalized_state"] == "running"
    assert result["data"]["terminal"] is False
    assert result["data"]["phase"] == "bitstream"
    assert result["data"]["percent"] == 83.333333
    assert result.get("next_actions", []) == []
    assert result["data"]["progress"]["status"] == "Running write_bitstream..."
    assert result["data"]["progress"]["normalized_status"] == "Running write_bitstream..."
    assert result["data"]["progress"]["normalized_progress"] == "83.333333%"


def test_get_run_progress_failed_run_recommends_run_failure_diagnosis() -> None:
    fake = FakeSession(raw="status=ERROR\nprogress=100%\nneeds_refresh=0\nbitstream_exists=0\nbitstream_files=vmcp_hex_list_v1:")
    service = VivadoToolService(session=fake)

    result = service.call("get_run_progress", {"run_name": "impl_1"})

    assert result["ok"] is True
    assert result["data"]["state"] == "failed"
    assert result["next_actions"][0]["tool"] == "diagnose_run_failure"
    assert result["next_actions"][0]["required_args"] == ["run_name"]


def test_get_run_progress_does_not_treat_incomplete_as_complete() -> None:
    fake = FakeSession(raw="status=incomplete\nprogress=99%\nneeds_refresh=0\nbitstream_exists=1\nbitstream_files=vmcp_hex_list_v1:443a2f64656d6f2f746f702e626974")
    service = VivadoToolService(session=fake)

    result = service.call("get_run_progress", {"run_name": "impl_1", "expect_bitstream": True})

    assert result["ok"] is True
    assert result["data"]["state"] == "unknown"
    assert result["data"]["terminal"] is False
    assert result.get("next_actions", []) == []


def test_diagnose_run_failure_aggregates_progress_log_tail_and_messages() -> None:
    class RoutingSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "bitstream_exists" in command:
                return {"ok": True, "raw": "status=ERROR\nprogress=100%\nneeds_refresh=0\nbitstream_exists=0\nbitstream_files=vmcp_hex_list_v1:"}
            if "__VMCP_RUN_LOG_TAIL_START__" in command:
                return {
                    "ok": True,
                    "raw": (
                        "run_dir=D:/proj/top.runs/impl_1\n"
                        "status=ERROR\n"
                        "progress=100%\n"
                        "needs_refresh=0\n"
                        f"log_files={encode_wire_list(['D:/proj/top.runs/impl_1/runme.log'])}\n"
                        "__VMCP_RUN_LOG_TAIL_START__\n"
                        "__VMCP_RUN_LOG_FILE__=D:/proj/top.runs/impl_1/runme.log\n"
                        "ERROR: [Common 17-69] Command failed\n"
                    ),
                }
            if "__VMCP_LOG_FILE__" in command:
                return {"ok": True, "raw": "ERROR: [Common 17-69] Command failed\n"}
            return {"ok": False, "raw": "unexpected command"}

    fake = RoutingSession()
    service = VivadoToolService(session=fake)

    result = service.call("diagnose_run_failure", {"run_name": "impl_1"})

    assert result["ok"] is True
    assert result["data"]["status"] == "BLOCK"
    assert result["data"]["diagnosis"]["primary_cause"] == "run_error"
    assert result["data"]["run_context"]["run_dir"] == "D:/proj/top.runs/impl_1"
    assert result["data"]["critical_warnings"]["counts"]["ERROR"] == 1
    assert [action["tool"] for action in result["next_actions"][:2]] == [
        "get_critical_warnings",
        "get_run_configuration",
    ]


def test_get_messages_reads_vivado_log_instead_of_calling_missing_tcl_command() -> None:
    fake = FakeSession(raw="CRITICAL WARNING: [Timing 38-282] example")
    service = VivadoToolService(session=fake)

    result = service.call("get_messages", {})

    assert result["ok"] is True
    assert fake.commands[0] != "get_messages"
    assert "vivado.log" in fake.commands[0]
    assert result["data"]["counts"]["CRITICAL WARNING"] == 1


def test_get_messages_prefers_current_project_run_logs() -> None:
    fake = FakeSession(raw="WARNING: [Timing 38-313] no constraints")
    service = VivadoToolService(session=fake)

    result = service.call("get_messages", {})

    assert result["ok"] is True
    assert "runme.log" in fake.commands[0]
    assert "*.runs" in fake.commands[0]
    assert "vivado.log" in fake.commands[0]


def test_get_critical_warnings_recounts_filtered_messages() -> None:
    fake = FakeSession(
        raw=(
            "WARNING: [Timing 38-313] no constraints\n"
            "CRITICAL WARNING: [Timing 38-282] failed timing\n"
        )
    )
    service = VivadoToolService(session=fake)

    result = service.call("get_critical_warnings", {})

    assert result["ok"] is True
    assert result["data"]["counts"]["WARNING"] == 0
    assert result["data"]["counts"]["CRITICAL WARNING"] == 1
    assert [msg["severity"] for msg in result["data"]["messages"]] == ["CRITICAL WARNING"]


def test_readiness_uses_project_run_logs_for_critical_messages() -> None:
    class RoutingSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "report_timing_summary" in command:
                return {
                    "ok": True,
                    "raw": _attested_report(
                        "timing_summary",
                        "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__",
                        "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.010 0.000 0.020 0.000",
                    ),
                }
            if "report_drc" in command:
                return {"ok": True, "raw": _attested_report('drc', '__VMCP_DRC_REPORT_BEGIN__', 'DRC report: no violations')}
            if "check_timing" in command:
                return {"ok": True, "raw": _attested_report('check_timing', '__VMCP_CHECK_TIMING_REPORT_BEGIN__', _complete_check_timing_body())}
            if "report_methodology" in command:
                return {"ok": True, "raw": _attested_report('methodology', '__VMCP_METHODOLOGY_REPORT_BEGIN__', 'Methodology report: no violations')}
            if "runme.log" in command:
                return {"ok": True, "raw": ""}
            return {"ok": True, "raw": "ERROR: [BD 5-102] stale session error"}

    fake = RoutingSession()
    service = VivadoToolService(session=fake)

    result = service.call("check_bitstream_readiness", {})

    assert result["ok"] is True
    assert result["data"]["status"] == "READY"
    assert any("runme.log" in command for command in fake.commands)


def test_get_project_info_returns_key_values_from_tcl_result_not_puts() -> None:
    fake = FakeSession(raw="name=demo\npart=xc7a35tcpg236-1\ndirectory=D:/Vivado_Mcp/test_use/demo\ntop=top")
    service = VivadoToolService(session=fake)

    result = service.call("get_project_info", {})

    assert result["ok"] is True
    assert "puts" not in fake.commands[0]
    assert result["data"]["project"]["name"] == "demo"
    assert result["data"]["project"]["top"] == "top"


def test_open_project_returns_failure_when_tcl_fails() -> None:
    fake = FakeSession(raw="ERROR: cannot open project", ok=False)
    service = VivadoToolService(session=fake)

    result = service.call("open_project", {"project_path": r"D:\Vivado_Mcp\test_use\missing.xpr"})

    assert result["ok"] is False
    assert result["error_code"] == "TCL_FAILED"
    assert "cannot open project" in result["raw_excerpt"]


def test_open_project_and_run_launch_block_existing_vivado_hooks() -> None:
    raw = "ERROR: VMCP_RUN_HOOK_BLOCKED: non-empty run hook properties: synth_1 STEPS.SYNTH_DESIGN.TCL.PRE"
    class HookSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if command.startswith("open_project"):
                return {"ok": True, "raw": ""}
            return {"ok": False, "raw": raw}

    fake = HookSession()
    service = VivadoToolService(session=fake)

    opened = service.call("open_project", {"project_path": r"D:\Vivado_Mcp\test_use\demo.xpr"})
    launched = service.call("run_synthesis", {})

    assert opened["error_code"] == "RUN_HOOK_BLOCKED"
    assert launched["error_code"] == "RUN_HOOK_BLOCKED"
    assert opened["data"]["policy_allowed"] is False
    assert launched["next_actions"][0]["tool"] == "get_run_configuration"
    assert "open_project" in fake.commands[0]
    assert "catch {close_project}" in fake.commands[1]
    assert fake.commands[2] == "catch {close_project}"
    assert "launch_runs {synth_1}" in fake.commands[3]


def test_configure_run_rejects_hook_property_before_tcl_execution() -> None:
    fake = FakeSession(raw="")
    service = VivadoToolService(session=fake)

    result = service.call(
        "configure_run",
        {"run_name": "synth_1", "properties": {"STEPS.SYNTH_DESIGN.TCL.POST": "hook.tcl"}},
    )

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_TOOL_ARGUMENTS"
    assert result["data"]["handler_executed"] is False
    assert any("property name must be one of" in issue for issue in result["data"]["validation_errors"])
    assert fake.commands == []


def test_report_tool_returns_failure_when_tcl_fails() -> None:
    fake = FakeSession(raw="ERROR: no open synthesized design", ok=False)
    service = VivadoToolService(session=fake)

    result = service.call("get_timing_summary", {})

    assert result["ok"] is False
    assert result["error_code"] == "TCL_FAILED"
    assert "no open synthesized design" in result["raw_excerpt"]


def test_readiness_returns_failure_when_dependency_report_fails() -> None:
    fake = FakeSession(raw="ERROR: design is not open", ok=False)
    service = VivadoToolService(session=fake)

    result = service.call("check_bitstream_readiness", {})

    assert result["ok"] is False
    assert result["error_code"] == "READINESS_INPUT_FAILED"
    assert result["data"]["failed_tool"] == "get_timing_summary"
    assert result["next_actions"][0]["tool"] == "get_timing_summary"
    assert result["next_actions"][0]["required_args"] == []


def test_analysis_returns_failure_next_action_when_dependency_report_fails() -> None:
    fake = FakeSession(raw="ERROR: design is not open", ok=False)
    service = VivadoToolService(session=fake)

    result = service.call("analyze_timing_closure", {})

    assert result["ok"] is False
    assert result["error_code"] == "ANALYSIS_INPUT_FAILED"
    assert result["data"]["failed_tool"] == "get_timing_summary"
    assert result["next_actions"][0]["tool"] == "get_timing_summary"


def test_pre_hw_signoff_open_run_failure_routes_to_mcp_tool_action() -> None:
    class RoutingSession(FakeSession):
        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if "check_syntax" in command:
                return {"ok": True, "raw": "status=READY\nraw_begin=__VMCP_SYNTAX_REPORT_BEGIN__\n"}
            if "compile_order sources" in command:
                return {"ok": True, "raw": "fileset=sources_1\ntop=top\n" + encode_wire_row({'file': 'D:/rtl/top.v', 'type': 'Verilog', 'exists': '1', 'managed': '0', 'used_in': 'synthesis', 'order': '0'})}
            if "synth_design -rtl" in command:
                return {"ok": True, "raw": "status=READY\ntop=top\npart=xc7a35tcpg236-1\nraw_begin=__VMCP_ELABORATION_REPORT_BEGIN__\n"}
            if "open_run {impl_1}" in command:
                return {"ok": False, "raw": "ERROR: [Common 17-69] Run impl_1 not found"}
            return {"ok": True, "raw": ""}

    fake = RoutingSession()
    service = VivadoToolService(session=fake)

    result = service.call("run_pre_hw_signoff", {"run_name": "impl_1"})

    assert result["ok"] is False
    assert result["error_code"] == "ANALYSIS_INPUT_FAILED"
    assert result["data"]["failed_tool"] == "open_run"
    assert any("open_run {impl_1}" in command for command in fake.commands)
    action_tools = {action["tool"] for action in result["next_actions"]}
    assert "open_run" not in action_tools
    assert "get_run_configuration" in action_tools
    assert action_tools <= set(VivadoToolService().tool_names())


def test_managed_existing_project_is_read_only_until_separate_project_is_created(tmp_path) -> None:
    working_project = tmp_path / "working" / "working.xpr"

    class ManagedProjectSession(GuiTcpVivadoSession):
        def __init__(self) -> None:
            super().__init__(generation_id="generation-test")
            self.commands: list[str] = []
            self.project_open = False

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if command.startswith("open_project"):
                self.project_open = True
            if command.startswith("close_project"):
                self.project_open = False
            if command.startswith("create_project"):
                if self.project_open:
                    return {"ok": False, "generation_id": self.generation_id, "raw": "project already open"}
                working_project.parent.mkdir(parents=True, exist_ok=True)
                working_project.write_text("# managed working project\n", encoding="utf-8")
                self.project_open = True
            return {"ok": True, "generation_id": self.generation_id, "raw": ""}

    existing = tmp_path / "existing" / "demo.xpr"
    existing.parent.mkdir()
    existing.write_text("# existing project\n", encoding="utf-8")
    session = ManagedProjectSession()
    service = VivadoToolService(session=session)

    opened = service.call("open_project", {"project_path": str(existing)})
    blocked = service.call("set_project_top", {"top": "new_top"})
    execution_blocked = service.call("run_synthesis", {})
    artifact_write_blocked = service.call("collect_build_artifacts", {"run_name": "impl_1"})
    replay_write_blocked = service.call("export_project_replay_script", {})

    assert opened["ok"] is True
    assert opened["data"]["mutation_policy"]["scope"] == "existing_project_read_only"
    assert opened["data"]["mutation_policy"]["vivado_read_only"] is True
    assert opened["data"]["mutation_policy"]["mcp_policy_read_only"] is True
    assert session.commands[0].startswith("open_project -read_only ")
    assert blocked["error_code"] == "EXISTING_PROJECT_MUTATION_REQUIRES_WORKING_COPY"
    assert blocked["data"]["original_project_protected"] is True
    assert execution_blocked["error_code"] == "EXISTING_PROJECT_MUTATION_REQUIRES_WORKING_COPY"
    assert execution_blocked["data"]["blocked_tool_class"] == "project_execution"
    assert artifact_write_blocked["error_code"] == "EXISTING_PROJECT_MUTATION_REQUIRES_WORKING_COPY"
    assert replay_write_blocked["error_code"] == "EXISTING_PROJECT_MUTATION_REQUIRES_WORKING_COPY"
    assert [action["tool"] for action in blocked["next_actions"]] == [
        "get_project_state",
        "list_fileset_files",
        "list_fileset_files",
        "list_fileset_files",
        "close_project",
        "create_project",
    ]
    assert [action["arg_sources"]["fileset"] for action in blocked["next_actions"][1:4]] == [
        "sources_1",
        "constrs_1",
        "sim_1",
    ]
    rebuild = blocked["next_actions"][-1]
    assert rebuild["required_args"] == [
        "project_name",
        "project_dir",
        "part",
        "top",
        "rtl_files",
        "xdc_files",
        "sim_files",
        "file_specs",
        "testbench_top",
        "target_language",
        "simulator",
        "source_include_dirs",
        "source_defines",
        "include_dirs",
        "defines",
    ]
    assert set(rebuild["required_args"]) <= set(rebuild["arg_sources"])
    assert "source_defines" in rebuild["arg_sources"]
    assert "file_specs" in rebuild["arg_sources"]
    assert len(session.commands) == 2

    closed = service.call("close_project", {})
    created = service.call(
        "create_project",
        {
            "project_name": "working",
            "project_dir": str(tmp_path / "working"),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [],
        },
    )
    allowed = service.call("set_project_top", {"top": "new_top"})

    assert closed["ok"] is True
    assert created["ok"] is True
    assert allowed["ok"] is True
    assert len(session.commands) == 5


def test_mcp_created_project_capability_rebinds_after_managed_session_restart(tmp_path) -> None:
    project = tmp_path / "managed" / "demo.xpr"
    project.parent.mkdir()
    project.write_text("# managed project\n", encoding="utf-8")
    capability = create_project_capability(project, generation_id="generation-a")

    class RestartedSession(GuiTcpVivadoSession):
        def __init__(self) -> None:
            super().__init__(generation_id="generation-b")
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return {"ok": True, "generation_id": self.generation_id, "raw": ""}

    session = RestartedSession()
    service = VivadoToolService(session=session)
    service._mcp_created_project_capabilities[capability["project_path_key"]] = capability

    opened = service.call("open_project", {"project_path": str(project)})

    assert opened["ok"] is True
    assert session.commands[0].startswith("open_project ")
    assert "-read_only" not in session.commands[0]
    assert opened["data"]["mutation_policy"]["scope"] == "mcp_created_project"
    assert opened["data"]["mutation_policy"]["generation_rebound"] is True
    rebound = service._mcp_created_project_capabilities[capability["project_path_key"]]
    assert rebound["generation_id"] == "generation-b"


def test_stop_session_checkpoints_managed_project_before_generation_rebind(tmp_path) -> None:
    project = tmp_path / "project" / "demo.xpr"
    project.parent.mkdir(parents=True)
    project.write_text("# initial managed project\n", encoding="utf-8")

    class ManagedSession(GuiTcpVivadoSession):
        def __init__(self, generation_id: str) -> None:
            super().__init__(generation_id=generation_id)
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            return {"ok": True, "generation_id": self.generation_id, "raw": ""}

    class ManagedSessionManager:
        def __init__(self) -> None:
            self.session = ManagedSession("generation-a")
            self.stopped = False

        def current(self) -> ManagedSession:
            return self.session

        def stop(self) -> dict:
            self.stopped = True
            return {"ok": True, "stopped": True, "process_running": False}

    manager = ManagedSessionManager()
    service = VivadoToolService(manager=manager)
    capability = create_project_capability(project, generation_id="generation-a")
    original_hash = capability["project_file_sha256"]
    service._mcp_created_project_capabilities[capability["project_path_key"]] = capability
    service._active_project_capability = capability
    service._project_mutation_scope = "mcp_created_project"
    project.write_text("# trusted Vivado update before stop\n", encoding="utf-8")

    stopped = service.call("stop_session", {})

    assert stopped["ok"] is True
    assert stopped["data"]["project_capability_checkpoint"]["status"] == "READY"
    assert stopped["data"]["project_capability_checkpoint"]["capability_refreshed"] is True
    refreshed = service._mcp_created_project_capabilities[capability["project_path_key"]]
    assert refreshed["project_file_sha256"] != original_hash
    assert manager.stopped is True

    manager.session = ManagedSession("generation-b")
    reopened = service.call("open_project", {"project_path": str(project)})

    assert reopened["ok"] is True
    assert reopened["data"]["mutation_policy"]["generation_rebound"] is True
    assert reopened["data"]["mutation_policy"]["scope"] == "mcp_created_project"


def test_stop_session_invalidates_capability_when_project_checkpoint_fails(tmp_path) -> None:
    project = tmp_path / "project" / "demo.xpr"
    project.parent.mkdir(parents=True)
    project.write_text("# managed project\n", encoding="utf-8")

    class FailingCloseSession(GuiTcpVivadoSession):
        def __init__(self) -> None:
            super().__init__(generation_id="generation-a")

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            return {"ok": False, "generation_id": self.generation_id, "raw": "close failed"}

    class Manager:
        def __init__(self) -> None:
            self.session = FailingCloseSession()

        def current(self) -> FailingCloseSession:
            return self.session

        def stop(self) -> dict:
            return {"ok": True, "stopped": True, "process_running": False}

    service = VivadoToolService(manager=Manager())
    capability = create_project_capability(project, generation_id="generation-a")
    service._mcp_created_project_capabilities[capability["project_path_key"]] = capability
    service._active_project_capability = capability
    service._project_mutation_scope = "mcp_created_project"

    stopped = service.call("stop_session", {})

    checkpoint = stopped["data"]["project_capability_checkpoint"]
    assert stopped["ok"] is True
    assert checkpoint["status"] == "BLOCK"
    assert checkpoint["capability_invalidated"] is True
    assert capability["project_path_key"] not in service._mcp_created_project_capabilities


def test_stop_session_preserves_project_closed_state_when_checkpoint_refresh_fails(tmp_path) -> None:
    project = tmp_path / "project" / "demo.xpr"
    project.parent.mkdir(parents=True)
    project.write_text("# managed project\n", encoding="utf-8")

    class Manager:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> dict:
            self.stopped = True
            return {"ok": True, "stopped": True, "process_running": False}

    class ClosedButUncheckpointedService(VivadoToolService):
        def _close_project(self, args: dict) -> dict:
            return failure(
                "close_project",
                "PROJECT_CAPABILITY_CLOSE_GUARD_FAILED",
                "The project closed, but its capability checkpoint could not be refreshed.",
                data={"project_closed": True},
            )

    manager = Manager()
    service = ClosedButUncheckpointedService(manager=manager)
    capability = create_project_capability(project, generation_id="generation-a")
    service._mcp_created_project_capabilities[capability["project_path_key"]] = capability
    service._active_project_capability = capability
    service._project_mutation_scope = "mcp_created_project"

    stopped = service.call("stop_session", {})

    checkpoint = stopped["data"]["project_capability_checkpoint"]
    assert stopped["ok"] is True
    assert manager.stopped is True
    assert checkpoint["status"] == "BLOCK"
    assert checkpoint["project_closed"] is True
    assert checkpoint["capability_invalidated"] is True


def test_stop_session_still_terminates_process_when_project_checkpoint_raises(tmp_path) -> None:
    project = tmp_path / "project" / "demo.xpr"
    project.parent.mkdir(parents=True)
    project.write_text("# managed project\n", encoding="utf-8")

    class TaintedSession(GuiTcpVivadoSession):
        def __init__(self) -> None:
            super().__init__(generation_id="generation-a")

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            return {"ok": True, "generation_id": self.generation_id, "raw": ""}

    class Manager:
        def __init__(self) -> None:
            self.session = TaintedSession()
            self.stopped = False

        def current(self) -> TaintedSession:
            return self.session

        def stop(self) -> dict:
            self.stopped = True
            return {"ok": True, "stopped": True, "process_running": False}

    class RaisingCheckpointService(VivadoToolService):
        def _close_project(self, args: dict) -> dict:
            raise SessionTaintedError("generation-a", "indeterminate test timeout")

    manager = Manager()
    service = RaisingCheckpointService(manager=manager)
    capability = create_project_capability(project, generation_id="generation-a")
    service._mcp_created_project_capabilities[capability["project_path_key"]] = capability
    service._active_project_capability = capability
    service._project_mutation_scope = "mcp_created_project"

    stopped = service.call("stop_session", {})

    checkpoint = stopped["data"]["project_capability_checkpoint"]
    assert stopped["ok"] is True
    assert manager.stopped is True
    assert checkpoint["status"] == "BLOCK"
    assert checkpoint["error_code"] == "SessionTaintedError"
    assert checkpoint["capability_invalidated"] is True
    assert capability["project_path_key"] not in service._mcp_created_project_capabilities


def test_managed_project_created_by_current_service_can_be_reopened_for_recovery(tmp_path) -> None:
    class ManagedProjectSession(GuiTcpVivadoSession):
        def __init__(self) -> None:
            super().__init__(generation_id="generation-test")
            self.commands: list[str] = []

        def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
            self.commands.append(command)
            if command.startswith("create_project"):
                project_dir.mkdir(parents=True, exist_ok=True)
                project_path.write_text("# managed project\n", encoding="utf-8")
            if "postcondition_discovery_status" in command:
                return {
                    "ok": True,
                    "generation_id": self.generation_id,
                    "raw": (
                        "setup_status=READY\n"
                        "postcondition_discovery_status=READY\n"
                        f"missing_after_repair={encode_wire_list([])}\n"
                        f"discovery_errors={encode_wire_list([])}"
                    ),
                }
            return {"ok": True, "generation_id": self.generation_id, "raw": ""}

    project_dir = tmp_path / "managed"
    project_path = project_dir / "demo.xpr"
    session = ManagedProjectSession()
    service = VivadoToolService(session=session)
    created = service.call(
        "create_project",
        {
            "project_name": "demo",
            "project_dir": str(project_dir),
            "part": "xc7a35tcpg236-1",
            "top": "top",
            "rtl_files": [],
        },
    )
    service._project_mutation_scope = "unbound"

    reopened = service.call("open_project", {"project_path": str(project_path)})
    repair = service.call("repair_project_setup", {"top": "top", "dry_run": False})

    assert created["ok"] is True
    assert reopened["data"]["mutation_policy"]["scope"] == "mcp_created_project"
    assert reopened["data"]["mutation_policy"]["origin"] == "mcp_created_in_current_server_process"
    assert reopened["data"]["mutation_policy"]["vivado_read_only"] is False
    assert any(command.startswith("open_project {") for command in session.commands)
    assert not any(command.startswith("open_project -read_only") for command in session.commands)
    assert repair["ok"] is True


def test_signoff_preserves_trusted_execution_policy_failure(monkeypatch) -> None:
    service = VivadoToolService(session=FakeSession())
    monkeypatch.setattr(
        service,
        "_analyze_sources",
        lambda _args: {"ok": True, "data": {"status": "READY"}},
    )
    blocked_action = {
        "tool": "list_fileset_files",
        "reason": "Inspect blocked constraints.",
        "required_args": ["fileset"],
        "arg_sources": {"fileset": "blocked constraint fileset"},
        "preconditions": ["Project is open."],
        "stop_condition": "Only trusted XDC remains.",
        "optional": False,
    }
    monkeypatch.setattr(
        service,
        "_run_elaboration",
        lambda _args: {
            "ok": False,
            "error_code": "EXECUTABLE_CONSTRAINT_INPUT_BLOCKED",
            "message": "Executable constraint input blocked.",
            "raw_excerpt": "VMCP_EXECUTABLE_CONSTRAINT_INPUT_BLOCKED",
            "next_actions": [blocked_action],
        },
    )

    result = service.call("run_pre_hw_signoff", {"run_name": "impl_1"})

    assert result["error_code"] == "EXECUTABLE_CONSTRAINT_INPUT_BLOCKED"
    assert result["data"]["failed_tool"] == "run_elaboration"
    assert result["next_actions"] == [blocked_action]
