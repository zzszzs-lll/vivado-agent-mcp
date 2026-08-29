import asyncio
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import agent_scenario_runner as runner
from agent_scenario_runner import (
    _summarize_next_actions,
    _post_stop_resume_actions,
    _live_partial_handoff,
    build_validation_matrix,
    classify_live_s02_status,
    classify_s05_validation,
    parse_scenarios,
    resolve_runner_output_root,
    write_existing_project_handoff_bundle,
    write_reviewable_warn_bundle,
    write_s01_project_sources,
    write_s02_project_sources,
    write_s03_live_project_sources,
)
from vivado_agent_mcp.tools import VivadoToolService
from vivado_agent_mcp.vivado.agent_catalog import SCENARIO_VALIDATION_POLICY, SCENARIOS


def _evidence_ref(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": str(path),
        "size": stat.st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "file_id": f"{stat.st_dev}:{stat.st_ino}",
        "mtime_ns": stat.st_mtime_ns,
    }


class _FixtureDistribution:
    version = "0.10.0"

    def __init__(self, root: Path) -> None:
        self.root = root

    def locate_file(self, relative) -> Path:
        return self.root / Path(str(relative))


def test_exact_wheel_payload_verifier_matches_installed_bytes(tmp_path: Path) -> None:
    install_root = tmp_path / "site-packages"
    entries = {
        "vivado_agent_mcp/__init__.py": b'__version__ = "0.10.0"\n',
        "vivado_agent_mcp/module.py": b"VALUE = 1\n",
        "vivado_agent_mcp-0.10.0.dist-info/METADATA": b"Name: vivado-agent-mcp\nVersion: 0.10.0\n",
        "vivado_agent_mcp-0.10.0.dist-info/RECORD": b"fixture record\n",
    }
    for relative, content in entries.items():
        path = install_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    wheel = tmp_path / "vivado_agent_mcp-0.10.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for relative, content in entries.items():
            archive.writestr(relative, content)

    result = runner._verify_installed_wheel_payload(
        wheel,
        distribution=_FixtureDistribution(install_root),
        import_path=install_root / "vivado_agent_mcp" / "__init__.py",
    )

    assert result["valid"] is True
    assert result["wheel_member_count"] == 4
    assert result["verified_payload_member_count"] == 3
    assert len(result["installed_payload_sha256"]) == 64

    (install_root / "vivado_agent_mcp" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    tampered = runner._verify_installed_wheel_payload(
        wheel,
        distribution=_FixtureDistribution(install_root),
        import_path=install_root / "vivado_agent_mcp" / "__init__.py",
    )
    assert tampered["valid"] is False
    assert any("bytes differ" in error for error in tampered["errors"])


def test_exact_wheel_import_guard_allows_workspace_local_clean_venv_but_rejects_src(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    clean_venv_import = workspace / "test_use" / "clean" / "venv" / "Lib" / "site-packages" / "vivado_agent_mcp" / "__init__.py"
    source_import = workspace / "src" / "vivado_agent_mcp" / "__init__.py"

    assert runner._imports_from_workspace_source(clean_venv_import, workspace) is False
    assert runner._imports_from_workspace_source(source_import, workspace) is True


def test_evidence_reference_consumer_rejects_post_validation_replacement(tmp_path: Path) -> None:
    evidence = tmp_path / "replay_project.tcl"
    evidence.write_text("open_project {demo.xpr}\n", encoding="utf-8")
    reference = _evidence_ref(evidence)
    evidence.write_text("create_project {forged} {.}\n", encoding="utf-8")

    assert runner._verified_evidence_snapshot(reference, root=tmp_path) is None


def test_nested_stdio_regression_uses_exact_installed_package_in_release_mode(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    installed_init = tmp_path / "venv" / "Lib" / "site-packages" / "vivado_agent_mcp" / "__init__.py"
    python_exe = tmp_path / "venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(runner, "_RUNNER_RELEASE_MODE", True)
    monkeypatch.setattr(runner, "_installed_package", SimpleNamespace(__file__=str(installed_init)))
    monkeypatch.setattr(
        runner,
        "_RUNNER_HARNESS_IDENTITY",
        {
            "files": [
                {"path": "tests/agent_stdio_regression.py", "sha256": "a" * 64},
            ]
        },
    )

    command = runner._nested_stdio_regression_command(
        workspace,
        output_dir=tmp_path / "evidence",
        python_exe=str(python_exe),
    )
    evidence = {
        "package_execution": {
            "mode": "installed_package",
            "workspace_source_enabled": False,
            "python_executable": str(python_exe),
            "regression_import_path": str(installed_init),
            "expected_mcp_import_path": str(installed_init),
            "mcp_server_import_guard": True,
            "timeout_server_import_guard": True,
            "harness_self_verified": True,
            "harness_sha256": "a" * 64,
        }
    }

    assert "--installed-package" in command
    assert command[command.index("--expected-package-import-path") + 1] == str(installed_init.resolve())
    assert command[command.index("--expected-harness-sha256") + 1] == "a" * 64
    assert runner._nested_stdio_package_evidence_ok(
        evidence,
        workspace=workspace,
        python_exe=str(python_exe),
    ) is True


def test_validation_harness_verifier_blocks_one_byte_runner_change(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    paths = (
        "tests/agent_scenario_runner.py",
        "tests/agent_stdio_regression.py",
    )
    entries = []
    for relative in paths:
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    identity_sha256 = hashlib.sha256(
        json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = {
        "validation_harness": {
            "status": "READY",
            "files": entries,
            "identity_sha256": identity_sha256,
        }
    }

    verified = runner._verify_validation_harness(manifest, workspace)
    assert verified["status"] == "VERIFIED"

    (workspace / paths[0]).write_text("# changed by one byte!\n", encoding="utf-8")
    try:
        runner._verify_validation_harness(manifest, workspace)
    except runner.RunnerProvenanceError as exc:
        assert "bytes do not match" in str(exc)
    else:
        raise AssertionError("changed runner bytes must be rejected")


def test_nested_stdio_release_evidence_rejects_workspace_import(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace_init = workspace / "src" / "vivado_agent_mcp" / "__init__.py"
    python_exe = tmp_path / "venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(runner, "_RUNNER_RELEASE_MODE", True)
    monkeypatch.setattr(runner, "_installed_package", SimpleNamespace(__file__=str(workspace_init)))
    evidence = {
        "package_execution": {
            "mode": "installed_package",
            "workspace_source_enabled": False,
            "python_executable": str(python_exe),
            "regression_import_path": str(workspace_init),
            "expected_mcp_import_path": str(workspace_init),
            "mcp_server_import_guard": True,
            "timeout_server_import_guard": True,
        }
    }

    assert runner._nested_stdio_package_evidence_ok(
        evidence,
        workspace=workspace,
        python_exe=str(python_exe),
    ) is False


def test_parse_scenarios_normalizes_and_dedupes() -> None:
    assert parse_scenarios("s00, S05; s02, s00") == ["S00", "S05", "S02"]
    assert parse_scenarios("s01,s04") == ["S01", "S04"]
    assert parse_scenarios("s06") == ["S06"]
    assert parse_scenarios("s03,s07") == ["S03", "S07"]
    assert parse_scenarios("") == ["S00", "S05"]


def test_parse_scenarios_rejects_unknown_id() -> None:
    try:
        parse_scenarios("S99")
    except ValueError as exc:
        assert "S99" in str(exc)
    else:
        raise AssertionError("S99 should be rejected")


def test_runner_failure_summary_preserves_bounded_tcl_diagnostics() -> None:
    summary = runner._summarize_data(
        "run_behavioral_simulation",
        {
            "ok": False,
            "raw_excerpt": "x" * 2500 + "TCL ROOT CAUSE",
            "data": {"command": "launch_simulation\n" + "y" * 1000},
        },
    )

    assert len(summary["raw_excerpt_tail"]) <= 2000
    assert summary["raw_excerpt_tail"].endswith("TCL ROOT CAUSE")
    assert len(summary["command_excerpt"]) == 500


def test_s03_expected_simulation_failure_is_a_valid_repair_trigger() -> None:
    structured = {
        "ok": False,
        "error_code": "SIMULATION_FAILED",
        "data": {
            "status": "failed",
            "status_source": "simulation_invocation_log_span",
            "simulation_diagnosis": {"primary_cause": "testbench_failure"},
            "log_span": {"start": 10, "end": 200},
        },
    }

    assert runner._s03_initial_failure_detected(structured) is True
    assert runner._s03_initial_failure_detected({**structured, "error_code": "SIMULATION_GENERATED_STATE_NOT_CLEAN"}) is False


def test_s03_live_evidence_fields_do_not_reuse_no_live_limitation() -> None:
    fields = runner.s03_live_evidence_fields()

    assert fields["execution_mode"] == "mcp_stdio_live_xsim_repair"
    assert fields["evidence_class"] == "live_xsim_repair_flow"
    assert fields["full_scenario_coverage"] is True
    assert "No-live fake" not in fields["limitation"]
    assert "does not run synthesis" in fields["limitation"]


def test_s07_live_evidence_fields_do_not_reuse_synthetic_limitation() -> None:
    fields = runner.s07_live_evidence_fields()

    assert fields["execution_mode"] == "mcp_stdio_live_existing_project_handoff"
    assert fields["evidence_class"] == "live_existing_project_handoff_flow"
    assert fields["full_scenario_coverage"] is True
    assert "Synthetic diagnostic bundle" not in fields["limitation"]
    assert "receiver stage must not execute, mutate, or recreate" in fields["limitation"]


def test_runner_output_dir_defaults_to_workspace_test_use_boundary() -> None:
    workspace = Path(__file__).resolve().parents[1]
    outside = workspace.parent / "__external_runner_output_for_test__"

    try:
        resolve_runner_output_root(workspace, str(outside))
    except ValueError as exc:
        assert "outside workspace test_use" in str(exc)
    else:
        raise AssertionError("external runner output should require an explicit allow flag")

    assert resolve_runner_output_root(workspace, str(outside), allow_external=True) == outside.resolve()
    assert resolve_runner_output_root(workspace, "test_use/agent_scenario_runner").is_relative_to((workspace / "test_use").resolve())


def test_live_runner_never_forces_project_replacement() -> None:
    args = runner._create_project_args(
        {
            "top": "top",
            "rtl_files": ["top.sv"],
            "xdc_files": ["top.xdc"],
            "sim_files": ["tb_top.sv"],
            "testbench_top": "tb_top",
            "target_language": "SystemVerilog",
        },
        Path("project"),
        "xc7a35tcpg236-1",
    )

    assert args["force"] is False


def test_call_tool_returns_structured_timeout_and_transcript(monkeypatch) -> None:
    class HangingSession:
        async def call_tool(self, _tool: str, _args: dict):
            await asyncio.sleep(60)

    monkeypatch.setattr(runner, "_mcp_call_timeout_s", lambda _args: 0.01)
    transcript: list[dict] = []

    result = asyncio.run(runner.call_tool(HangingSession(), "run_project_audit", {"timeout_s": 600}, transcript))

    assert result["ok"] is False
    assert result["error_code"] == "RUNNER_MCP_CALL_TIMEOUT"
    assert result["data"]["runner_timeout_s"] == 0.01
    assert result["data"]["next_actions"][0]["tool"] == "get_workflow_trace_status"
    assert transcript[0]["tool"] == "run_project_audit"
    assert transcript[0]["error_code"] == "RUNNER_MCP_CALL_TIMEOUT"
    assert transcript[0]["next_actions"][0]["tool"] == "get_workflow_trace_status"


def test_live_runner_retries_only_structured_empty_xsim_script_failure_once(monkeypatch) -> None:
    responses = [
        {"ok": False, "error_code": "SIMULATION_XSIM_LAUNCH_TRANSIENT", "data": {}},
        {"ok": True, "error_code": "", "data": {"status": "completed"}},
    ]
    calls: list[tuple[str, dict]] = []

    async def fake_call_tool(_session, tool: str, args: dict, _transcript: list[dict]) -> dict:
        calls.append((tool, args))
        return responses.pop(0)

    monkeypatch.setattr(runner, "call_tool", fake_call_tool)
    args = {"simset": "sim_1", "run_time": "20 us", "export_vcd": False, "max_vcd_mb": 64}

    result = asyncio.run(runner.run_behavioral_simulation_with_transient_retry(object(), args, []))

    assert result["data"]["status"] == "completed"
    expected_args = {
        "execution_intent": "execute runner-controlled HDL/testbench code inside the configured trusted test workspace",
        "confirm": "RUN_TRUSTED_XSIM",
        "incremental": False,
        **args,
    }
    assert calls == [("run_behavioral_simulation", expected_args), ("run_behavioral_simulation", expected_args)]


def test_live_runner_restarts_managed_session_before_single_xsim_retry(monkeypatch) -> None:
    responses = [
        {
            "ok": False,
            "error_code": "SIMULATION_XSIM_LAUNCH_TRANSIENT",
            "data": {
                "abort_attempted": True,
                "managed_session_stopped": True,
                "runtime_dir": "D:/runtime",
                "project_path": "D:/project/demo.xpr",
            },
        },
        {"ok": True, "error_code": "", "data": {"connected": True}},
        {"ok": True, "error_code": "", "data": {"project_path": "D:/project/demo.xpr"}},
        {"ok": True, "error_code": "", "data": {"status": "completed"}},
    ]
    calls: list[tuple[str, dict]] = []

    async def fake_call_tool(_session, tool: str, args: dict, _transcript: list[dict]) -> dict:
        calls.append((tool, args))
        return responses.pop(0)

    monkeypatch.setattr(runner, "call_tool", fake_call_tool)
    args = {"simset": "sim_1", "run_time": "20 us", "export_vcd": False, "max_vcd_mb": 64}

    result = asyncio.run(
        runner.run_behavioral_simulation_with_transient_retry(
            object(),
            args,
            [],
            recovery={"runtime_dir": "D:/runtime", "project_path": "D:/project/demo.xpr", "start_timeout_s": 240},
        )
    )

    assert result["data"]["status"] == "completed"
    assert [tool for tool, _ in calls] == ["run_behavioral_simulation", "start_session", "open_project", "run_behavioral_simulation"]
    assert calls[1][1] == {"timeout_s": 240, "runtime_dir": "D:/runtime"}
    assert calls[2][1]["project_path"] == "D:/project/demo.xpr"


def test_validation_matrix_marks_partial_runner_as_watch() -> None:
    matrix = build_validation_matrix(
        [
            {
                "id": "S00",
                "status": "PASS",
                "execution_mode": "nested_mcp_stdio_regression",
                "evidence_class": "stdio_integration",
                "full_scenario_coverage": True,
                "hardware_validation_status": "NOT_VALIDATED",
            },
            {
                "id": "S05",
                "status": "PASS",
                "execution_mode": "mcp_stdio_synthetic_bundle",
                "evidence_class": "synthetic_bundle_contract",
                "full_scenario_coverage": False,
                "hardware_validation_status": "NOT_VALIDATED",
            },
        ],
        include_live_vivado=False,
    )

    assert matrix["policy_id"] == "default_no_board_scenario_matrix"
    assert matrix["status"] == "WATCH"
    assert matrix["complete"] is False
    assert matrix["checks"]["all_requested_passed"] is True
    assert matrix["checks"]["no_live_matrix_complete"] is False
    assert matrix["checks"]["live_matrix_complete"] is False
    assert matrix["checks"]["hardware_boundary_not_validated"] is True
    assert {row["id"] for row in matrix["rows"] if row["status"] == "NOT_RUN"} >= {"S03", "S04", "S06", "S07"}
    assert matrix["next_required_runs"]


def test_runner_matrix_matches_agent_scenario_policy() -> None:
    matrix = build_validation_matrix([], include_live_vivado=False)

    assert set(runner.SUPPORTED_SCENARIOS) == {item["id"] for item in SCENARIOS}
    assert matrix["policy_id"] == SCENARIO_VALIDATION_POLICY["id"]
    assert matrix["scope"] == SCENARIO_VALIDATION_POLICY["scope"]
    assert matrix["required_no_live_scenarios"] == SCENARIO_VALIDATION_POLICY["required_no_live_scenarios"]
    assert matrix["required_live_scenarios"] == SCENARIO_VALIDATION_POLICY["required_live_scenarios"]


def test_validation_matrix_passes_only_with_no_live_and_live_evidence() -> None:
    results = []
    for scenario_id in ("S00", "S04", "S05", "S06"):
        results.append(
            {
                "id": scenario_id,
                "status": "PASS",
                "execution_mode": "nested_mcp_stdio_regression",
                "evidence_class": "stdio_contract",
                "full_scenario_coverage": True,
                "hardware_validation_status": "NOT_VALIDATED",
            }
        )
    for scenario_id, mode in {
        "S01": "mcp_stdio_live_project",
        "S02": "mcp_stdio_live_project",
        "S03": "mcp_stdio_live_xsim_repair",
        "S07": "mcp_stdio_live_existing_project_handoff",
    }.items():
        results.append(
            {
                "id": scenario_id,
                "status": "PASS",
                "execution_mode": mode,
                "evidence_class": "live_project_software_flow",
                "full_scenario_coverage": True,
                "hardware_validation_status": "NOT_VALIDATED",
            }
        )

    matrix = build_validation_matrix(results, include_live_vivado=True)

    assert matrix["status"] == "PASS"
    assert matrix["complete"] is True
    assert matrix["checks"]["no_live_matrix_complete"] is True
    assert matrix["checks"]["live_matrix_complete"] is True
    assert matrix["next_required_runs"] == []


def test_validation_matrix_rejects_thin_live_evidence() -> None:
    results = []
    for scenario_id in ("S00", "S04", "S05", "S06"):
        results.append(
            {
                "id": scenario_id,
                "status": "PASS",
                "execution_mode": "nested_mcp_stdio_regression",
                "evidence_class": "stdio_contract",
                "full_scenario_coverage": True,
                "hardware_validation_status": "NOT_VALIDATED",
            }
        )
    for scenario_id in ("S01", "S02", "S03", "S07"):
        results.append(
            {
                "id": scenario_id,
                "status": "PASS",
                "execution_mode": "mcp_stdio_live_project",
                "evidence_class": "synthetic_bundle_contract",
                "full_scenario_coverage": False,
                "hardware_validation_status": "",
            }
        )

    matrix = build_validation_matrix(results, include_live_vivado=True)

    assert matrix["status"] == "WATCH"
    assert matrix["complete"] is False
    assert matrix["checks"]["live_matrix_complete"] is False
    missing_live_evidence = {
        row["id"] for row in matrix["rows"] if row["live_required"] and not row["live_evidence_observed"]
    }
    assert missing_live_evidence == {"S01", "S02", "S03", "S07"}


def test_write_s01_project_sources_matches_minimal_counter_shape(tmp_path: Path) -> None:
    inputs = write_s01_project_sources(tmp_path / "s01")

    assert inputs["target_language"] == "Verilog"
    assert inputs["top"] == "counter_top"
    assert inputs["testbench_top"] == "tb_counter_top"
    assert len(inputs["rtl_files"]) == 1
    tb_text = Path(inputs["sim_files"][0]).read_text(encoding="utf-8")
    xdc_text = Path(inputs["xdc_files"][0]).read_text(encoding="utf-8")
    assert "$finish" in tb_text
    assert "TB_PASS" in tb_text
    assert "reg done;" in tb_text
    assert "done = 1'b1;" in tb_text
    assert "if (!done)" in tb_text
    assert "create_clock" in xdc_text
    assert "CFGBVS" in xdc_text


def test_write_s02_project_sources_matches_scenario_shape(tmp_path: Path) -> None:
    inputs = write_s02_project_sources(tmp_path / "s02")

    assert inputs["target_language"] == "SystemVerilog"
    assert inputs["top"] == "breath_led_top"
    assert inputs["testbench_top"] == "tb_breath_led_top"
    assert len(inputs["rtl_files"]) == 3
    assert all(Path(path).suffix == ".sv" for path in inputs["rtl_files"])
    assert len(inputs["sim_files"]) == 1
    tb_text = Path(inputs["sim_files"][0]).read_text(encoding="utf-8")
    xdc_text = Path(inputs["xdc_files"][0]).read_text(encoding="utf-8")
    assert "$finish" in tb_text
    assert "TB_PASS" in tb_text
    assert "logic done = 1'b0;" in tb_text
    assert "done = 1'b1;" in tb_text
    assert "if (!done)" in tb_text
    assert "create_clock" in xdc_text
    assert "CFGBVS" in xdc_text


def test_write_s03_live_project_sources_matches_repair_shape(tmp_path: Path) -> None:
    inputs = write_s03_live_project_sources(tmp_path / "s03")

    assert inputs["target_language"] == "SystemVerilog"
    assert inputs["top"] == "counter_repair_top"
    assert inputs["testbench_top"] == "tb_counter_repair_top"
    assert len(inputs["rtl_files"]) == 1
    faulty_text = Path(inputs["rtl_files"][0]).read_text(encoding="utf-8")
    fixed_text = inputs["fixed_rtl_text"]
    tb_text = Path(inputs["sim_files"][0]).read_text(encoding="utf-8")
    xdc_text = Path(inputs["xdc_files"][0]).read_text(encoding="utf-8")
    assert "4'd2" in faulty_text
    assert "4'd1" in fixed_text
    assert "TB_FAIL" in tb_text
    assert "TB_PASS" in tb_text
    assert "$finish" in tb_text
    assert "@(negedge clk)" in tb_text
    assert "logic done" in tb_text
    assert "if (!done)" in tb_text
    assert "create_clock" in xdc_text
    assert "CFGBVS" in xdc_text


def test_reviewable_warn_bundle_validates_as_handoff_reviewable(tmp_path: Path) -> None:
    manifest_path = write_reviewable_warn_bundle(tmp_path / "warn_bundle")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})
    outcome = classify_s05_validation(result)

    assert result["ok"] is True
    assert result["data"]["status"] == "WARN"
    assert result["data"]["handoff_ready"] is False
    assert result["data"]["handoff_reviewable"] is True
    assert result["data"]["bundle_mode"] == "reference"
    assert result["data"]["portable"] is False
    assert result["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert outcome["status"] == "PASS"
    assert outcome["hardware_validation_status"] == "NOT_VALIDATED"
    assert outcome["handoff_status"] == "WARN"
    assert set(outcome["review_required_reasons"]) == {"audit_status=WARN", "bundle_not_portable"}
    assert outcome["recommended_entrypoint"] == "get_agent_workflows"
    assert outcome["next_steps"]


def test_existing_project_handoff_reference_bundle_is_reviewable(tmp_path: Path) -> None:
    manifest_path = write_existing_project_handoff_bundle(tmp_path / "existing_bundle")
    service = VivadoToolService()

    result = service.call("validate_diagnostic_bundle", {"manifest_path": str(manifest_path)})

    assert result["ok"] is True
    assert result["data"]["status"] == "WARN"
    assert result["data"]["handoff_ready"] is False
    assert result["data"]["handoff_reviewable"] is True
    assert result["data"]["resume_context"]["workflow_trace_ref"]["path"].endswith("workflow_trace.jsonl")
    assert "open_project" in (manifest_path.parent / "replay_project.tcl").read_text(encoding="utf-8")


def test_live_s02_classification_ignores_nonblocking_discovery_failure() -> None:
    transcript = [{"tool": "get_agent_workflows", "ok": False, "error_code": "", "message": "invalid args"}]
    validation = {
        "ok": True,
        "data": {
            "status": "WARN",
            "handoff_ready": False,
            "handoff_reviewable": True,
            "bundle_mode": "reference",
            "portable": False,
            "hardware_validation": {"status": "NOT_VALIDATED"},
        },
    }

    outcome = classify_live_s02_status(transcript, validation)

    assert outcome["status"] == "PASS"
    assert outcome["hardware_validation_status"] == "NOT_VALIDATED"
    assert outcome["handoff_status"] == "WARN"


def test_live_s02_classification_ignores_resolved_blocking_failure() -> None:
    transcript = [
        {
            "seq": 1,
            "tool": "run_behavioral_simulation",
            "ok": False,
            "error_code": "SIMULATION_XSIM_LAUNCH_TRANSIENT",
        },
        {"seq": 2, "tool": "start_session", "ok": True, "error_code": ""},
        {"seq": 3, "tool": "run_behavioral_simulation", "ok": True, "error_code": ""},
    ]
    validation = {
        "ok": True,
        "data": {
            "status": "WARN",
            "handoff_ready": False,
            "handoff_reviewable": True,
            "bundle_mode": "reference",
            "portable": False,
            "hardware_validation": {"status": "NOT_VALIDATED"},
        },
    }

    outcome = classify_live_s02_status(transcript, validation)

    assert outcome["status"] == "PASS"
    assert outcome["summary"] == "S02 completed live Project Mode flow with reviewable WARN handoff."


def test_live_s02_classification_blocks_unresolved_blocking_failure() -> None:
    transcript = [
        {"seq": 1, "tool": "run_behavioral_simulation", "ok": True, "error_code": ""},
        {"seq": 2, "tool": "run_project_audit", "ok": False, "error_code": "AUDIT_FAILED"},
    ]
    validation = {
        "ok": True,
        "data": {
            "status": "WARN",
            "handoff_ready": False,
            "handoff_reviewable": True,
            "bundle_mode": "reference",
            "portable": False,
            "hardware_validation": {"status": "NOT_VALIDATED"},
        },
    }

    outcome = classify_live_s02_status(transcript, validation)

    assert outcome["status"] == "BLOCK"
    assert "run_project_audit" in outcome["summary"]


def test_live_partial_handoff_preserves_artifact_report_after_signoff_timeout(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_path = project_dir / "demo.xpr"
    artifacts = {"ok": True, "data": {"manifest_path": str(project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json")}}
    reports = {"ok": True, "data": {"status": "WARN", "manifest_path": str(project_dir / "vmcp_reports" / "impl_1" / "report_manifest.json")}}
    signoff = {"ok": False, "tool": "run_pre_hw_signoff", "error_code": "TimeoutError", "message": "timed out", "data": {"status": "BLOCK"}}

    handoff = _live_partial_handoff(
        project_path=project_path,
        project_dir=project_dir,
        artifacts=artifacts,
        reports=reports,
        signoff=signoff,
        audit={},
        bundle={},
        final_validation={},
        bitstream_completed=True,
        handoff_blocker="run_pre_hw_signoff failed or returned BLOCK.",
    )

    assert handoff["handoff_partial"] is True
    assert handoff["handoff_blocker"] == "run_pre_hw_signoff failed or returned BLOCK."
    assert handoff["artifact_manifest_path"].endswith("manifest.json")
    assert handoff["report_manifest_path"].endswith("report_manifest.json")
    assert handoff["hardware_validation_status"] == "NOT_VALIDATED"
    assert handoff["failed_tools"] == [
        {
            "tool": "run_pre_hw_signoff",
            "error_code": "TimeoutError",
            "status": "BLOCK",
            "message": "timed out",
        }
    ]
    assert [action["tool"] for action in handoff["next_actions"][:2]] == ["start_session", "open_project"]
    assert handoff["next_actions"][2]["tool"] == "run_pre_hw_signoff"
    assert handoff["next_actions"][2]["arg_sources"]["report_manifest_path"].endswith("report_manifest.json")


def test_live_runner_stops_after_signoff_blocker_and_writes_partial_checkpoint(tmp_path: Path, monkeypatch) -> None:
    called_tools: list[str] = []

    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[object()] * 97)

    @asynccontextmanager
    async def fake_stdio_session(**_kwargs):
        yield FakeSession()

    async def fake_call_tool(_session, tool: str, args: dict, transcript: list[dict]) -> dict:
        called_tools.append(tool)
        data: dict = {}
        ok = True
        message = f"{tool} ok"
        if tool == "run_behavioral_simulation":
            data = {"status": "completed", "simulation_diagnosis": {"primary_cause": "testbench_pass"}}
        elif tool == "collect_build_artifacts":
            data = {"manifest_path": str(tmp_path / "S02" / "project" / "vmcp_artifacts" / "impl_1" / "manifest.json")}
        elif tool == "collect_report_bundle":
            data = {"status": "WARN", "manifest_path": str(tmp_path / "S02" / "project" / "vmcp_reports" / "impl_1" / "report_manifest.json")}
        elif tool == "run_pre_hw_signoff":
            data = {"status": "BLOCK"}
            message = "signoff timed out"
        elif tool == "stop_session":
            data = {"status": "stopped"}

        structured = {"ok": ok, "tool": tool, "message": message, "error_code": "", "data": data}
        transcript.append(
            {
                "seq": len(transcript) + 1,
                "tool": tool,
                "ok": ok,
                "error_code": "",
                "message": message,
                "status": data.get("status", ""),
                "next_actions": [],
                "hardware_validation_status": "NOT_VALIDATED" if tool == "run_pre_hw_signoff" else "",
                "data_summary": data,
            }
        )
        return structured

    async def fake_poll_run(_session, _run_name, _expect_bitstream, _timeout_s, _interval_s, _transcript):
        return {"ok": True, "data": {"terminal": True, "state": "complete"}}

    monkeypatch.setattr(runner, "mcp_stdio_session", fake_stdio_session)
    monkeypatch.setattr(runner, "call_tool", fake_call_tool)
    monkeypatch.setattr(runner, "poll_run", fake_poll_run)

    result = asyncio.run(
        runner.run_live_project_flow(
            workspace=Path(__file__).resolve().parents[1],
            scenario_dir=tmp_path / "S02",
            python_exe=sys.executable,
            include_live_vivado=True,
            part="xc7a35tcpg236-1",
            vivado_timeout_s=1,
            poll_timeout_s=1,
            poll_interval_s=1,
            scenario_id="S02",
            label="Multi-file SystemVerilog PWM",
            project_name="s02_pwm",
            source_writer=write_s02_project_sources,
            sim_run_time="20 us",
        )
    )

    assert result["status"] == "BLOCK"
    assert result["handoff_blocker"] == "run_pre_hw_signoff failed or returned BLOCK."
    assert result["partial_handoff"]["handoff_partial"] is True
    assert result["partial_handoff"]["failed_tools"][0]["tool"] == "run_pre_hw_signoff"
    assert result["partial_handoff"]["next_actions"][2]["tool"] == "run_pre_hw_signoff"
    assert "run_project_audit" not in called_tools
    assert "collect_diagnostic_bundle" not in called_tools
    assert "validate_diagnostic_bundle" not in called_tools
    assert Path(result["checkpoint_path"]).exists()
    assert Path(result["progress_path"]).exists()
    progress = Path(result["progress_path"]).read_text(encoding="utf-8")
    assert "bitstream_complete" in progress
    assert "run_pre_hw_signoff" in progress


def test_live_runner_validates_partial_bundle_after_bundle_blocker(tmp_path: Path, monkeypatch) -> None:
    called_tools: list[str] = []
    called_args: dict[str, dict] = {}

    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[object()] * 97)

    @asynccontextmanager
    async def fake_stdio_session(**_kwargs):
        yield FakeSession()

    async def fake_call_tool(_session, tool: str, args: dict, transcript: list[dict]) -> dict:
        called_tools.append(tool)
        called_args[tool] = dict(args)
        data: dict = {}
        message = f"{tool} ok"
        if tool == "run_behavioral_simulation":
            data = {
                "status": "completed",
                "status_source": "simulation_invocation_log_span",
                "simulation_invocation_id": "sim-live",
                "ended_at": "2026-06-16T00:00:00Z",
                "log_span": {"start": 0, "end": 128, "reset_detected": False},
                "command": "launch_simulation -simset sim_1",
                "simulation_diagnosis": {"primary_cause": "testbench_pass"},
            }
        elif tool == "collect_build_artifacts":
            data = {"manifest_path": str(tmp_path / "S02" / "project" / "vmcp_artifacts" / "impl_1" / "manifest.json")}
        elif tool == "collect_report_bundle":
            data = {"status": "WARN", "manifest_path": str(tmp_path / "S02" / "project" / "vmcp_reports" / "impl_1" / "report_manifest.json")}
        elif tool == "run_pre_hw_signoff":
            data = {"status": "WARN"}
        elif tool == "run_project_audit":
            data = {"status": "WARN", "hardware_validation": {"status": "NOT_VALIDATED", "validated": False}}
        elif tool == "collect_diagnostic_bundle":
            data = {"status": "BLOCK", "partial_manifest_path": str(tmp_path / "S02" / "project" / "vmcp_diagnostics" / "partial_manifest.json")}
            message = "bundle partial"
        elif tool == "validate_diagnostic_bundle":
            data = {"status": "BLOCK", "handoff_ready": False, "handoff_reviewable": False}
            message = "partial bundle blocked"

        structured = {"ok": True, "tool": tool, "message": message, "error_code": "", "data": data}
        transcript.append(
            {
                "seq": len(transcript) + 1,
                "tool": tool,
                "ok": True,
                "error_code": "",
                "message": message,
                "status": data.get("status", ""),
                "next_actions": [],
                "hardware_validation_status": "NOT_VALIDATED" if tool in {"run_pre_hw_signoff", "run_project_audit", "validate_diagnostic_bundle"} else "",
                "data_summary": data,
            }
        )
        return structured

    async def fake_poll_run(_session, _run_name, _expect_bitstream, _timeout_s, _interval_s, _transcript):
        return {"ok": True, "data": {"terminal": True, "state": "complete"}}

    monkeypatch.setattr(runner, "mcp_stdio_session", fake_stdio_session)
    monkeypatch.setattr(runner, "call_tool", fake_call_tool)
    monkeypatch.setattr(runner, "poll_run", fake_poll_run)

    result = asyncio.run(
        runner.run_live_project_flow(
            workspace=Path(__file__).resolve().parents[1],
            scenario_dir=tmp_path / "S02",
            python_exe=sys.executable,
            include_live_vivado=True,
            part="xc7a35tcpg236-1",
            vivado_timeout_s=1,
            poll_timeout_s=1,
            poll_interval_s=1,
            scenario_id="S02",
            label="Multi-file SystemVerilog PWM",
            project_name="s02_pwm",
            source_writer=write_s02_project_sources,
            sim_run_time="20 us",
        )
    )

    assert result["status"] == "BLOCK"
    assert result["handoff_blocker"] == "collect_diagnostic_bundle failed or returned BLOCK."
    assert "validate_diagnostic_bundle" in called_tools
    assert result["partial_handoff"]["diagnostic_manifest_path"].endswith("partial_manifest.json")
    assert result["partial_handoff"]["failed_tools"][0]["tool"] == "collect_diagnostic_bundle"
    assert result["partial_handoff"]["failed_tools"][1]["tool"] == "validate_diagnostic_bundle"
    for tool in ("run_pre_hw_signoff", "run_project_audit", "collect_diagnostic_bundle"):
        assert "simulation_result" not in called_args[tool]
    assert "audit_result" not in called_args["collect_diagnostic_bundle"]


def test_next_actions_summary_preserves_action_contract() -> None:
    actions = [
        {
            "tool": "repair_project_setup",
            "reason": "Recover partial setup.",
            "required_args": ["project_path"],
            "arg_sources": {"project_path": "create_project.data.project_path"},
            "preconditions": ["Project exists."],
            "stop_condition": "repair_project_setup returns READY.",
            "optional": False,
        }
    ]

    assert _summarize_next_actions(actions) == actions


def test_post_stop_resume_actions_start_and_open_before_project_bound_tools() -> None:
    actions = _post_stop_resume_actions(project_path="D:/p/demo.xpr", manifest_path="D:/p/vmcp_diagnostics/manifest.json", run_name="impl_1")

    assert [action["tool"] for action in actions[:2]] == ["start_session", "open_project"]
    assert any(action["tool"] == "validate_diagnostic_bundle" and action["optional"] is True for action in actions)
    assert actions[-1]["tool"] == "run_project_audit"
    assert "stopped session" in actions[-1]["reason"]


def test_agent_scenario_runner_unknown_scenario_returns_structured_block(tmp_path: Path) -> None:
    completed = _run_runner(tmp_path, "S99")

    assert completed.returncode == 1
    assert "Traceback" not in completed.stderr
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    result = json.loads(Path(summary["result_path"]).read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert result["blocked"] is True
    assert result["error_code"] == "UNSUPPORTED_SCENARIO"
    assert result["supported_scenarios"] == ["S00", "S01", "S02", "S03", "S04", "S05", "S06", "S07"]
    assert result["scenario_results"][0]["execution_mode"] == "runner_guard"
    assert result["scenario_results"][0]["evidence_class"] == "runner_contract"
    assert result["scenario_results"][0]["next_actions"][0]["required_args"] == ["--scenarios"]


def test_agent_scenario_runner_s02_requires_explicit_live_flag(tmp_path: Path) -> None:
    completed = _run_runner(tmp_path, "S02")

    assert completed.returncode == 1
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    result = json.loads(Path(summary["result_path"]).read_text(encoding="utf-8"))
    scenario = result["scenario_results"][0]
    assert result["ok"] is False
    assert result["blocked"] is True
    assert result["skipped_live"] is True
    assert scenario["status"] == "BLOCK"
    assert scenario["error_code"] == "LIVE_VIVADO_NOT_ENABLED"
    assert scenario["execution_mode"] == "live_project_not_executed"
    assert scenario["evidence_class"] == "live_project_required"
    assert scenario["full_scenario_coverage"] is False
    assert scenario["next_actions"][0]["required_args"] == ["--scenarios", "--include-live-vivado"]


def test_agent_scenario_runner_s01_requires_explicit_live_flag(tmp_path: Path) -> None:
    completed = _run_runner(tmp_path, "S01")

    assert completed.returncode == 1
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    result = json.loads(Path(summary["result_path"]).read_text(encoding="utf-8"))
    scenario = result["scenario_results"][0]
    assert result["ok"] is False
    assert result["blocked"] is True
    assert scenario["id"] == "S01"
    assert scenario["error_code"] == "LIVE_VIVADO_NOT_ENABLED"
    assert scenario["execution_mode"] == "live_project_not_executed"
    assert scenario["evidence_class"] == "live_project_required"
    assert scenario["tool_definition_count"] == 0
    assert scenario["tool_call_count"] == 0
    assert scenario["hardware_validation_status"] == "NOT_VALIDATED"


def test_agent_scenario_runner_s03_uses_live_lite_when_flag_enabled(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_s03_live(**kwargs):
        calls.append(kwargs)
        return {
            "id": "S03",
            "label": "Simulation failure repair live-lite",
            "status": "PASS",
            "executed": True,
            "execution_mode": "mcp_stdio_live_xsim_repair",
            "evidence_class": "live_xsim_repair_flow",
            "full_scenario_coverage": True,
            "hardware_validation_status": "NOT_VALIDATED",
        }

    monkeypatch.setattr(runner, "run_s03_live_simulation_failure_repair", fake_s03_live)

    result = asyncio.run(
        runner.run_scenarios(
            workspace=Path(__file__).resolve().parents[1],
            run_dir=tmp_path / "runner_live_s03",
            scenario_ids=["S03"],
            python_exe=sys.executable,
            include_live_vivado=True,
            part="xc7a35tcpg236-1",
            vivado_timeout_s=240,
            poll_timeout_s=1,
            poll_interval_s=1,
        )
    )

    assert calls
    assert calls[0]["part"] == "xc7a35tcpg236-1"
    scenario = result["scenario_results"][0]
    assert result["ok"] is True
    assert scenario["execution_mode"] == "mcp_stdio_live_xsim_repair"
    assert scenario["evidence_class"] == "live_xsim_repair_flow"
    assert scenario["full_scenario_coverage"] is True


def test_agent_scenario_runner_s07_uses_live_lite_when_flag_enabled(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_s07_live(**kwargs):
        calls.append(kwargs)
        return {
            "id": "S07",
            "label": "Existing project audit and replay handoff live-lite",
            "status": "PASS",
            "executed": True,
            "execution_mode": "mcp_stdio_live_existing_project_handoff",
            "evidence_class": "live_existing_project_handoff_flow",
            "full_scenario_coverage": True,
            "hardware_validation_status": "NOT_VALIDATED",
        }

    monkeypatch.setattr(runner, "run_s07_live_existing_project_handoff", fake_s07_live)

    result = asyncio.run(
        runner.run_scenarios(
            workspace=Path(__file__).resolve().parents[1],
            run_dir=tmp_path / "runner_live_s07",
            scenario_ids=["S07"],
            python_exe=sys.executable,
            include_live_vivado=True,
            part="xc7a35tcpg236-1",
            vivado_timeout_s=240,
            poll_timeout_s=1800,
            poll_interval_s=10,
        )
    )

    assert calls
    assert calls[0]["poll_timeout_s"] == 1800
    scenario = result["scenario_results"][0]
    assert result["ok"] is True
    assert scenario["execution_mode"] == "mcp_stdio_live_existing_project_handoff"
    assert scenario["evidence_class"] == "live_existing_project_handoff_flow"
    assert scenario["full_scenario_coverage"] is True


def test_s07_live_receiver_opens_existing_project_without_create(tmp_path: Path, monkeypatch) -> None:
    project_dir = tmp_path / "S07" / "seed_project" / "project"
    project_dir.mkdir(parents=True)
    project_path = project_dir / "s07_seed_counter.xpr"
    project_path.write_text("# seed project\n", encoding="utf-8")
    trace_path = project_dir / "vmcp_diagnostics" / "workflow_trace.jsonl"
    replay_path = project_dir / "vmcp_diagnostics" / "replay_project.tcl"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text('{"tool":"open_project","ok":true}\n', encoding="utf-8")
    replay_path.write_text(f"open_project {{{project_path}}}\n", encoding="utf-8")
    report_manifest = project_dir / "vmcp_reports" / "impl_1" / "report_manifest.json"
    report_manifest.parent.mkdir(parents=True)
    report_manifest.write_text("{}", encoding="utf-8")
    diagnostic_manifest = project_dir / "vmcp_diagnostics" / "diagnostic_manifest.json"
    diagnostic_manifest.write_text(
        json.dumps(
            {
                "summary": {"primary_files": {"workflow_trace": str(trace_path), "replay_script": str(replay_path)}},
                "files": [
                    {"category": "workflow_trace", "path": str(trace_path)},
                    {"category": "replay_script", "path": str(replay_path)},
                ],
            }
        ),
        encoding="utf-8",
    )

    async def fake_seed_flow(**_kwargs):
        return {
            "id": "S01",
            "status": "PASS",
            "project_path": str(project_path),
            "manifest_path": str(diagnostic_manifest),
            "checkpoint_path": str(tmp_path / "checkpoint.json"),
            "progress_path": str(tmp_path / "progress.jsonl"),
            "handoff_status": "READY",
            "handoff_ready": True,
            "handoff_reviewable": True,
            "hardware_validation_status": "NOT_VALIDATED",
            "partial_handoff": {
                "report_manifest_path": str(report_manifest),
                "diagnostic_manifest_path": str(diagnostic_manifest),
                "workflow_trace_path": str(trace_path),
            },
            "tool_calls": [{"tool": "create_project", "ok": True}],
            "tool_call_count": 9,
        }

    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[object()] * 101)

    @asynccontextmanager
    async def fake_stdio_session(**_kwargs):
        yield FakeSession()

    async def fake_call_tool(_session, tool: str, args: dict, transcript: list[dict]) -> dict:
        data: dict = {}
        if tool == "get_agent_scenarios":
            data = {"selected_count": 1, "scenarios": [{"id": "S07"}]}
        elif tool == "get_project_state":
            data = {"project": {"path": str(project_path), "directory": str(project_dir)}}
        elif tool == "run_behavioral_simulation":
            data = {"status": "completed", "simulation_diagnosis": {"primary_cause": "testbench_pass"}}
        elif tool == "collect_build_artifacts":
            data = {
                "status": "READY",
                "manifest_path": str(project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json"),
            }
        elif tool == "collect_report_bundle":
            data = {"status": "READY", "manifest_path": str(report_manifest)}
        elif tool == "run_project_audit":
            data = {"status": "WARN", "hardware_validation": {"status": "NOT_VALIDATED"}}
        elif tool == "collect_diagnostic_bundle":
            data = {"status": "READY", "manifest_path": str(diagnostic_manifest)}
        elif tool == "validate_diagnostic_bundle":
            data = {
                "status": "WARN",
                "handoff_ready": False,
                "handoff_reviewable": True,
                "bundle_mode": "reference",
                "portable": False,
                "hardware_validation": {"status": "NOT_VALIDATED"},
                "health": {
                    "bundle_mode": "reference",
                    "portable": False,
                    "handoff_ready": False,
                    "review_required_reasons": ["audit_status=WARN"],
                },
                "resume_context": {
                    "workflow_trace_ref": _evidence_ref(trace_path),
                    "primary_file_refs": {"replay_script": _evidence_ref(replay_path)},
                    "recommended_entrypoint": "open_project",
                },
            }
        elif tool == "get_workflow_trace_status":
            data = {"trace_path": str(trace_path), "last_successful_tool": "validate_diagnostic_bundle"}
        elif tool == "stop_session":
            data = {"status": "stopped"}
        structured = {"ok": True, "tool": tool, "message": f"{tool} ok", "error_code": "", "data": data}
        transcript.append(
            {
                "seq": len(transcript) + 1,
                "tool": tool,
                "ok": True,
                "error_code": "",
                "message": structured["message"],
                "status": data.get("status", ""),
                "next_actions": [],
                "hardware_validation_status": "NOT_VALIDATED" if tool in {"run_project_audit", "validate_diagnostic_bundle"} else "",
                "data_summary": data,
            }
        )
        return structured

    monkeypatch.setattr(runner, "run_live_project_flow", fake_seed_flow)
    monkeypatch.setattr(runner, "mcp_stdio_session", fake_stdio_session)
    monkeypatch.setattr(runner, "call_tool", fake_call_tool)

    result = asyncio.run(
        runner.run_s07_live_existing_project_handoff(
            workspace=Path(__file__).resolve().parents[1],
            scenario_dir=tmp_path / "S07",
            python_exe=sys.executable,
            part="xc7a35tcpg236-1",
            vivado_timeout_s=240,
            poll_timeout_s=1800,
            poll_interval_s=10,
        )
    )

    receiver_tools = [item["tool"] for item in result["tool_calls"]]
    assert result["status"] == "PASS"
    assert result["execution_mode"] == "mcp_stdio_live_existing_project_handoff"
    assert result["receiver_created_project"] is False
    assert result["seed_project_path"] == str(project_path)
    assert result["receiver_project_path"] == str(project_path)
    assert "create_project" not in receiver_tools
    assert {
        "get_tool_catalog",
        "get_agent_workflows",
        "get_agent_scenarios",
        "start_session",
        "open_project",
        "get_project_state",
        "list_fileset_files",
        "validate_diagnostic_bundle",
        "get_workflow_trace_status",
        "stop_session",
    } <= set(receiver_tools)
    assert result["receiver_checks"]["receiver_required_tools_present"] is True
    assert result["receiver_checks"]["receiver_required_tool_chain_order"] is True
    assert result["receiver_checks"]["receiver_inspection_only"] is True
    assert result["handoff_reviewable"] is True
    assert result["hardware_validation_status"] == "NOT_VALIDATED"
    assert result["workflow_trace_path"] == str(trace_path)
    assert result["replay_script_path"] == str(replay_path)
    assert Path(result["result_path"]).exists()


def test_s07_live_result_blocks_when_receiver_executes_original_project(tmp_path: Path) -> None:
    project_dir = tmp_path / "seed_project" / "project"
    project_dir.mkdir(parents=True)
    project_path = project_dir / "s07_seed_counter.xpr"
    project_path.write_text("# seed project\n", encoding="utf-8")
    trace_path = project_dir / "vmcp_diagnostics" / "workflow_trace.jsonl"
    replay_path = project_dir / "vmcp_diagnostics" / "replay_project.tcl"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text('{"tool":"open_project","ok":true}\n', encoding="utf-8")
    replay_path.write_text(f"open_project {{{project_path}}}\n", encoding="utf-8")
    validation = {
        "ok": True,
        "tool": "validate_diagnostic_bundle",
        "data": {
            "status": "READY",
            "handoff_ready": True,
            "handoff_reviewable": True,
            "hardware_validation": {"status": "NOT_VALIDATED"},
            "resume_context": {
                "workflow_trace_ref": _evidence_ref(trace_path),
                "primary_file_refs": {"replay_script": _evidence_ref(replay_path)},
            },
        },
    }
    transcript = [
        {"tool": "get_tool_catalog", "ok": True},
        {"tool": "get_agent_workflows", "ok": True},
        {"tool": "get_agent_scenarios", "ok": True},
        {"tool": "start_session", "ok": True},
        {"tool": "open_project", "ok": True},
        {"tool": "get_project_state", "ok": True},
        {"tool": "list_fileset_files", "ok": True},
        {"tool": "run_behavioral_simulation", "ok": True},
        {"tool": "validate_diagnostic_bundle", "ok": True},
        {"tool": "get_workflow_trace_status", "ok": True},
        {"tool": "stop_session", "ok": True},
    ]

    result = runner._s07_live_result_payload(
        scenario_dir=tmp_path / "S07",
        result_path=tmp_path / "S07" / "existing_project_handoff_live_lite.json",
        started="2026-06-16T00:00:00Z",
        seed_result={"status": "PASS", "project_path": str(project_path)},
        seed_project_path=str(project_path),
        tools_count=101,
        transcript=transcript,
        scenario={"ok": True, "data": {"selected_count": 1, "scenarios": [{"id": "S07"}]}},
        project_state={"ok": True, "data": {"project": {"path": str(project_path)}}},
        receiver_artifacts={"ok": True, "data": {"status": "READY"}},
        receiver_reports={"ok": True, "data": {"status": "READY"}},
        audit={"ok": True, "data": {"status": "READY"}},
        bundle={"ok": True, "data": {"status": "READY", "manifest_path": str(project_dir / "vmcp_diagnostics" / "diagnostic_manifest.json")}},
        validation=validation,
        trace_status={"ok": True, "data": {"trace_path": str(trace_path)}},
        stop_summary={"ok": True, "data": {"status": "stopped"}},
        blocker="",
        outcome={"status": "PASS", "handoff_status": "READY", "hardware_validation_status": "NOT_VALIDATED"},
        artifact_manifest_path="",
        report_manifest_path="",
    )

    assert result["status"] == "BLOCK"
    assert result["receiver_checks"]["receiver_required_tools_present"] is True
    assert result["receiver_checks"]["receiver_required_tool_chain_order"] is True
    assert result["receiver_checks"]["receiver_inspection_only"] is False


def test_s07_live_seed_failure_writes_reusable_result(tmp_path: Path, monkeypatch) -> None:
    async def fake_seed_flow(**_kwargs):
        return {
            "id": "S01",
            "status": "BLOCK",
            "project_path": str(tmp_path / "missing.xpr"),
            "checkpoint_path": str(tmp_path / "checkpoint.json"),
            "progress_path": str(tmp_path / "progress.jsonl"),
            "partial_handoff": {"artifact_manifest_path": str(tmp_path / "manifest.json")},
            "tool_calls": [{"tool": "start_session", "ok": False, "error_code": "TimeoutError"}],
            "tool_call_count": 1,
            "hardware_validation_status": "NOT_VALIDATED",
        }

    monkeypatch.setattr(runner, "run_live_project_flow", fake_seed_flow)

    result = asyncio.run(
        runner.run_s07_live_existing_project_handoff(
            workspace=Path(__file__).resolve().parents[1],
            scenario_dir=tmp_path / "S07_seed_fail",
            python_exe=sys.executable,
            part="xc7a35tcpg236-1",
            vivado_timeout_s=240,
            poll_timeout_s=1800,
            poll_interval_s=10,
        )
    )
    detail = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))

    assert result["status"] == "BLOCK"
    assert result["result_kind"] == "seed_failure_checkpoint"
    assert result["receiver_created_project"] is False
    assert result["partial_handoff"]["artifact_manifest_path"].endswith("manifest.json")
    assert detail["tool_calls"][0]["tool"] == "start_session"
    assert detail["hardware_validation_status"] == "NOT_VALIDATED"


def test_s07_live_receiver_failure_writes_stop_session_and_transcript(tmp_path: Path, monkeypatch) -> None:
    project_dir = tmp_path / "S07_receiver_fail" / "seed_project" / "project"
    project_dir.mkdir(parents=True)
    project_path = project_dir / "s07_seed_counter.xpr"
    project_path.write_text("# seed project\n", encoding="utf-8")
    report_manifest = project_dir / "vmcp_reports" / "impl_1" / "report_manifest.json"
    report_manifest.parent.mkdir(parents=True)
    report_manifest.write_text("{}", encoding="utf-8")

    async def fake_seed_flow(**_kwargs):
        return {
            "id": "S01",
            "status": "PASS",
            "project_path": str(project_path),
            "handoff_status": "READY",
            "handoff_ready": True,
            "handoff_reviewable": True,
            "hardware_validation_status": "NOT_VALIDATED",
            "partial_handoff": {},
            "tool_calls": [{"tool": "create_project", "ok": True}],
            "tool_call_count": 9,
        }

    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[object()] * 101)

    @asynccontextmanager
    async def fake_stdio_session(**_kwargs):
        yield FakeSession()

    captured_audit_args: dict = {}

    async def fake_call_tool(_session, tool: str, _args: dict, transcript: list[dict]) -> dict:
        if tool == "run_project_audit":
            captured_audit_args.update(_args)
        ok = tool != "run_project_audit"
        data: dict = {}
        error_code = ""
        if tool == "get_agent_scenarios":
            data = {"selected_count": 1, "scenarios": [{"id": "S07"}]}
        elif tool == "run_behavioral_simulation":
            data = {
                "status": "completed",
                "status_source": "simulation_invocation_log_span",
                "simulation_invocation_id": "sim-s07",
                "ended_at": "2026-06-16T00:00:00Z",
                "log_span": {"start": 12, "end": 128, "reset_detected": False},
                "simset": "sim_1",
                "command": "launch_simulation -simset sim_1",
                "simulation_diagnosis": {"primary_cause": "testbench_pass"},
            }
        elif tool == "collect_build_artifacts":
            data = {
                "status": "READY",
                "manifest_path": str(project_dir / "vmcp_artifacts" / "impl_1" / "manifest.json"),
            }
        elif tool == "collect_report_bundle":
            data = {"status": "READY", "manifest_path": str(report_manifest)}
        elif tool == "run_project_audit":
            data = {"status": "BLOCK", "hardware_validation": {"status": "NOT_VALIDATED"}}
            error_code = "AUDIT_BLOCKED"
        elif tool == "stop_session":
            data = {"status": "stopped"}
        structured = {"ok": ok, "tool": tool, "message": f"{tool} {'ok' if ok else 'blocked'}", "error_code": error_code, "data": data}
        transcript.append(
            {
                "seq": len(transcript) + 1,
                "tool": tool,
                "ok": ok,
                "error_code": error_code,
                "message": structured["message"],
                "status": data.get("status", "BLOCK" if not ok else ""),
                "next_actions": [],
                "hardware_validation_status": "NOT_VALIDATED" if tool == "run_project_audit" else "",
                "data_summary": data,
            }
        )
        return structured

    monkeypatch.setattr(runner, "run_live_project_flow", fake_seed_flow)
    monkeypatch.setattr(runner, "mcp_stdio_session", fake_stdio_session)
    monkeypatch.setattr(runner, "call_tool", fake_call_tool)

    result = asyncio.run(
        runner.run_s07_live_existing_project_handoff(
            workspace=Path(__file__).resolve().parents[1],
            scenario_dir=tmp_path / "S07_receiver_fail",
            python_exe=sys.executable,
            part="xc7a35tcpg236-1",
            vivado_timeout_s=240,
            poll_timeout_s=1800,
            poll_interval_s=10,
        )
    )
    detail = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
    receiver_tools = [item["tool"] for item in result["tool_calls"]]

    assert result["status"] == "BLOCK"
    assert result["handoff_blocker"] == "producer diagnostic manifest is unavailable for inspection-only receiver validation."
    assert receiver_tools[-1] == "stop_session"
    assert "create_project" not in receiver_tools
    assert "run_project_audit" not in receiver_tools
    assert captured_audit_args == {}
    assert detail["tool_calls"][-1]["tool"] == "stop_session"
    assert detail["stop_session"]["ok"] is True


def test_s03_live_failure_result_includes_stop_session_after_early_block(tmp_path: Path, monkeypatch) -> None:
    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="get_tool_catalog")])

    @asynccontextmanager
    async def fake_mcp_stdio_session(**_kwargs):
        yield FakeSession()

    async def fake_call_tool(_session, tool: str, _args: dict, transcript: list[dict]):
        ok = tool != "start_session"
        structured = {
            "ok": ok,
            "tool": tool,
            "message": f"{tool} {'ok' if ok else 'failed'}",
            "error_code": "" if ok else "TimeoutError",
            "data": {},
            "next_actions": [],
        }
        transcript.append(
            {
                "seq": len(transcript) + 1,
                "tool": tool,
                "ok": ok,
                "error_code": structured["error_code"],
                "message": structured["message"],
                "status": "PASS" if ok else "BLOCK",
                "next_actions": [],
                "hardware_validation_status": "",
                "data_summary": {},
            }
        )
        return structured

    monkeypatch.setattr(runner, "mcp_stdio_session", fake_mcp_stdio_session)
    monkeypatch.setattr(runner, "call_tool", fake_call_tool)

    scenario_dir = tmp_path / "S03"
    result = asyncio.run(
        runner.run_s03_live_simulation_failure_repair(
            workspace=Path(__file__).resolve().parents[1],
            scenario_dir=scenario_dir,
            python_exe=sys.executable,
            part="xc7a35tcpg236-1",
            vivado_timeout_s=1,
        )
    )
    detail = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))

    assert result["status"] == "BLOCK"
    assert result["tool_call_count"] == 5
    assert result["tool_calls"][-1]["tool"] == "stop_session"
    assert detail["tool_call_count"] == 5
    assert detail["tool_calls"][-1]["tool"] == "stop_session"
    assert detail["stop_session"]["ok"] is True


def test_s03_live_retry_cleans_generated_state_before_second_failure_invocation(tmp_path: Path, monkeypatch) -> None:
    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="get_tool_catalog")])

    @asynccontextmanager
    async def fake_mcp_stdio_session(**_kwargs):
        yield FakeSession()

    observed: list[tuple[str, dict]] = []
    bounded_simulation_count = 0
    latest_result_count = 0
    repair_actions = [
        {
            "tool": tool,
            "reason": "repair",
            "required_args": [],
            "arg_sources": {},
            "preconditions": [],
            "stop_condition": "done",
            "optional": False,
        }
        for tool in ("get_simulation_result", "check_syntax", "get_compile_order", "run_behavioral_simulation")
    ]

    async def fake_call_tool(_session, tool: str, args: dict, transcript: list[dict]):
        nonlocal bounded_simulation_count, latest_result_count
        observed.append((tool, dict(args)))
        ok = True
        error_code = ""
        data: dict = {}
        next_actions: list[dict] = []
        status = ""
        if tool == "get_agent_scenarios":
            data = {"selected_count": 1, "scenarios": [{"id": "S03"}]}
        elif tool == "run_behavioral_simulation" and args.get("run_all"):
            ok = False
            error_code = "SIMULATION_RUN_ALL_VCD_BLOCKED"
        elif tool == "run_behavioral_simulation":
            bounded_simulation_count += 1
            if bounded_simulation_count == 1:
                ok = False
                error_code = "TCL_FAILED"
            elif bounded_simulation_count == 2:
                ok = False
                error_code = "SIMULATION_FAILED"
                data = {
                    "status": "failed",
                    "status_source": "simulation_invocation_log_span",
                    "simulation_invocation_id": "initial-failure",
                    "incremental_control": "managed_preflight_set_false",
                    "managed_simulation_policy_command": "set_property INCREMENTAL 0",
                    "simulation_diagnosis": {"primary_cause": "testbench_failure"},
                    "log_span": {"start": 0, "end": 100},
                }
                next_actions = repair_actions
            else:
                data = {
                    "status": "completed",
                    "status_source": "simulation_invocation_log_span",
                    "simulation_invocation_id": "repaired-pass",
                    "incremental_control": "managed_preflight_set_false",
                    "managed_simulation_policy_command": "set_property INCREMENTAL 0",
                    "simulation_diagnosis": {"primary_cause": "testbench_pass"},
                    "log_span": {"start": 0, "end": 120},
                }
                status = "completed"
        elif tool == "get_simulation_result":
            latest_result_count += 1
            if latest_result_count == 2:
                data = {"status": "failed", "status_source": "latest_log_tail"}
            else:
                ok = False
                error_code = "SIMULATION_EVIDENCE_STALE"
        elif tool == "clean_run_outputs":
            data = {
                "run_names": [],
                "simsets": list(args.get("simsets", [])),
                "executed": not bool(args.get("dry_run", True)),
            }
        elif tool == "check_syntax":
            data = {"status": "READY"}

        structured = {
            "ok": ok,
            "tool": tool,
            "message": f"{tool} {'ok' if ok else 'failed'}",
            "error_code": error_code,
            "data": data,
            "next_actions": next_actions,
        }
        transcript.append(
            {
                "seq": len(transcript) + 1,
                "tool": tool,
                "ok": ok,
                "error_code": error_code,
                "message": structured["message"],
                "status": status,
                "next_actions": next_actions,
                "hardware_validation_status": "",
                "data_summary": data,
            }
        )
        return structured

    monkeypatch.setattr(runner, "mcp_stdio_session", fake_mcp_stdio_session)
    monkeypatch.setattr(runner, "call_tool", fake_call_tool)

    result = asyncio.run(
        runner.run_s03_live_simulation_failure_repair(
            workspace=Path(__file__).resolve().parents[1],
            scenario_dir=tmp_path / "S03",
            python_exe=sys.executable,
            part="xc7a35tcpg236-1",
            vivado_timeout_s=1,
        )
    )

    retry_clean_indexes = [
        index
        for index, (tool, args) in enumerate(observed)
        if tool == "clean_run_outputs"
        and args.get("dry_run") is False
        and "before retrying the expected S03 failure invocation" in str(args.get("intent", ""))
    ]
    bounded_simulation_indexes = [
        index
        for index, (tool, args) in enumerate(observed)
        if tool == "run_behavioral_simulation" and not args.get("run_all")
    ]

    assert result["status"] == "PASS"
    assert len(retry_clean_indexes) == 1
    assert bounded_simulation_indexes[0] < retry_clean_indexes[0] < bounded_simulation_indexes[1]
    detail = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
    assert detail["initial_retry"]["clean_run_outputs"]["ok"] is True


def test_agent_scenario_runner_s05_runs_from_current_source(tmp_path: Path) -> None:
    completed = _run_runner(tmp_path, "S05")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    result = json.loads(Path(summary["result_path"]).read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["passed"] is True
    assert result["scenario_results"][0]["id"] == "S05"
    assert result["scenario_results"][0]["status"] == "PASS"
    assert result["scenario_results"][0]["execution_mode"] == "mcp_stdio_synthetic_bundle"
    assert result["scenario_results"][0]["evidence_class"] == "synthetic_bundle_contract"
    assert result["scenario_results"][0]["handoff_reviewable"] is True
    assert result["scenario_results"][0]["handoff_status"] == "WARN"
    assert set(result["scenario_results"][0]["review_required_reasons"]) == {
        "audit_status=WARN",
        "bundle_not_portable",
    }
    assert result["scenario_results"][0]["tool_calls"][-1]["next_actions"][0]["reason"]


def test_agent_scenario_runner_s03_runs_simulation_repair_contract(tmp_path: Path) -> None:
    completed = _run_runner(tmp_path, "S03")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    result = json.loads(Path(summary["result_path"]).read_text(encoding="utf-8"))
    scenario = result["scenario_results"][0]
    assert result["ok"] is True
    assert scenario["id"] == "S03"
    assert scenario["status"] == "PASS"
    assert scenario["execution_mode"] == "mcp_stdio_fake_session"
    assert scenario["evidence_class"] == "stdio_fake_session_contract"
    assert scenario["full_scenario_coverage"] is False
    assert scenario["tool_call_count"] == 6
    assert scenario["fake_session_launch_count"] == 2
    assert scenario["tool_calls"][0]["tool"] == "get_agent_scenarios"
    assert all(scenario["checks"].values())
    assert scenario["vcd_guard"]["error_code"] == "SIMULATION_RUN_ALL_VCD_BLOCKED"
    assert scenario["initial_failure"]["simulation_diagnosis"]["primary_cause"] == "testbench_failure"
    assert scenario["rerun"]["simulation_diagnosis"]["primary_cause"] == "testbench_pass"
    assert {action["tool"] for action in scenario["next_actions"]} >= {"get_simulation_result", "run_behavioral_simulation"}


def test_agent_scenario_runner_s06_runs_safety_negative_paths(tmp_path: Path) -> None:
    completed = _run_runner(tmp_path, "S06")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    result = json.loads(Path(summary["result_path"]).read_text(encoding="utf-8"))
    scenario = result["scenario_results"][0]
    assert result["ok"] is True
    assert scenario["id"] == "S06"
    assert scenario["status"] == "PASS"
    assert scenario["execution_mode"] == "mcp_stdio_negative_paths"
    assert scenario["evidence_class"] == "stdio_safety_contract"
    assert scenario["full_scenario_coverage"] is True
    assert scenario["tool_profile"] == "all"
    assert all(scenario["checks"].values())
    assert scenario["hardware_validation_status"] == "NOT_VALIDATED"


def test_agent_scenario_runner_s07_runs_existing_project_handoff_contract(tmp_path: Path) -> None:
    completed = _run_runner(tmp_path, "S07")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    result = json.loads(Path(summary["result_path"]).read_text(encoding="utf-8"))
    scenario = result["scenario_results"][0]
    assert result["ok"] is True
    assert scenario["id"] == "S07"
    assert scenario["status"] == "PASS"
    assert scenario["execution_mode"] == "mcp_stdio_synthetic_bundle"
    assert scenario["evidence_class"] == "synthetic_bundle_contract"
    assert scenario["full_scenario_coverage"] is False
    assert all(scenario["checks"].values())
    assert scenario["handoff_status"] == "WARN"
    assert scenario["handoff_ready"] is False
    assert scenario["handoff_reviewable"] is True
    assert scenario["hardware_validation_status"] == "NOT_VALIDATED"


def test_agent_scenario_runner_s04_runs_partial_setup_recovery(tmp_path: Path) -> None:
    completed = _run_runner(tmp_path, "S04")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    result = json.loads(Path(summary["result_path"]).read_text(encoding="utf-8"))
    scenario = result["scenario_results"][0]
    assert result["ok"] is True
    assert scenario["id"] == "S04"
    assert scenario["status"] == "PASS"
    assert scenario["execution_mode"] == "nested_mcp_stdio_timeout_regression"
    assert scenario["evidence_class"] == "stdio_fake_timeout_contract"
    assert scenario["full_scenario_coverage"] is False
    assert all(scenario["checks"].values())
    assert scenario["tool_definition_count"] == scenario["tool_count"]
    assert "tool_call_count" in scenario
    assert "tool_definition_count" in scenario["tool_count_note"]
    assert scenario["next_actions"][0]["tool"] == "open_project"
    assert scenario["hardware_validation_status"] == "NOT_VALIDATED"


def _run_runner(tmp_path: Path, scenarios: str) -> subprocess.CompletedProcess[str]:
    workspace = Path(__file__).resolve().parents[1]
    output_dir = tmp_path / f"runner_{scenarios.replace(',', '_')}"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(workspace / "src")
    env.setdefault("SystemRoot", r"C:\WINDOWS")
    env.setdefault("WINDIR", r"C:\WINDOWS")

    completed = subprocess.run(
        [
            sys.executable,
            str(workspace / "tests" / "agent_scenario_runner.py"),
            "--scenarios",
            scenarios,
            "--output-dir",
            str(output_dir),
            "--allow-external-output-dir",
        ],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return completed
