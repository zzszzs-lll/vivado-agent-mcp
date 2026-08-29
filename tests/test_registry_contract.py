from vivado_agent_mcp.registry import (
    EXISTING_PROJECT_EXECUTION_TOOLS,
    IMMEDIATE_PROJECT_MUTATION_TOOLS,
    TOOL_DEFS,
    TOOL_REGISTRY,
    hardware_tool_tiers,
    input_schema_properties,
    profile_tool_names,
    validate_tool_arguments,
)
from vivado_agent_mcp.tools import VivadoToolService


def test_registry_is_single_source_for_server_tools_and_service_handlers() -> None:
    service = VivadoToolService()
    server_names = {tool.name for tool in TOOL_DEFS}
    registry_names = set(TOOL_REGISTRY)

    assert server_names == registry_names
    assert set(service.tool_names()) == registry_names
    for name, spec in TOOL_REGISTRY.items():
        assert spec.handler == f"_{name}"
        assert hasattr(service, spec.handler), f"missing handler for {name}"
        assert spec.input_schema["type"] == "object"
        assert spec.risk in {
            "normal",
            "tcl_policy_dry_run",
            "hardware",
            "hardware_destructive",
            "destructive_dry_run",
            "project_mutation_immediate",
            "project_execution",
        }
    for name in {"list_hw_targets", "list_hw_devices", "disconnect_hw_server", "close_hardware_manager", "get_hw_device_status", "get_hardware_messages"}:
        assert TOOL_REGISTRY[name].risk == "hardware"


def test_registry_exposes_hardware_tool_tiers() -> None:
    tiers = hardware_tool_tiers()

    assert tiers["hardware_safe_detector"] == ["detect_hardware_environment"]
    assert tiers["hardware_log_readonly"] == ["get_hardware_messages"]
    assert "list_hw_targets" in tiers["hardware_disabled_by_default"]
    assert "program_hw_device" in tiers["hardware_destructive"]


def test_default_core_profile_hides_raw_hardware_and_advanced_mutations() -> None:
    core = set(profile_tool_names("core"))
    advanced = set(profile_tool_names("advanced"))
    all_tools = set(profile_tool_names("all"))

    assert 25 <= len(core) <= 45
    assert {"get_tool_catalog", "create_project", "run_behavioral_simulation", "generate_bitstream", "validate_diagnostic_bundle"} <= core
    assert "close_project" in core
    assert "clean_run_outputs" in core
    assert {
        "run_tcl",
        "safe_tcl",
        "remove_project_files",
        "set_project_part",
        "create_block_design",
        "configure_run",
        "get_run_configuration",
        "get_drc_report",
    }.isdisjoint(core)
    assert {"open_hardware_manager", "program_hw_device"}.isdisjoint(core)
    assert "run_tcl" in advanced and "open_hardware_manager" not in advanced
    assert all_tools == set(TOOL_REGISTRY)


def test_immediate_project_mutations_are_not_misclassified_as_normal() -> None:
    assert {"configure_run", "set_project_part", "connect_bd_net", "create_managed_xdc"} <= IMMEDIATE_PROJECT_MUTATION_TOOLS
    assert all(TOOL_REGISTRY[name].risk == "project_mutation_immediate" for name in IMMEDIATE_PROJECT_MUTATION_TOOLS)


def test_existing_project_execution_tools_are_explicitly_classified() -> None:
    assert {
        "run_synthesis",
        "run_behavioral_simulation",
        "collect_build_artifacts",
        "collect_report_bundle",
        "export_project_replay_script",
        "run_project_audit",
    } <= EXISTING_PROJECT_EXECUTION_TOOLS
    non_destructive = EXISTING_PROJECT_EXECUTION_TOOLS - {"clean_run_outputs", "reset_runs"}
    assert all(TOOL_REGISTRY[name].risk == "project_execution" for name in non_destructive)
    assert all(TOOL_REGISTRY[name].risk == "destructive_dry_run" for name in {"clean_run_outputs", "reset_runs"})


def test_default_workflow_steps_are_available_in_core_profile() -> None:
    service = VivadoToolService()
    result = service.call("get_agent_workflows", {})
    workflow_tools = {
        step["tool"]
        for workflow in result["data"]["workflows"]
        for step in workflow.get("steps", [])
    }

    assert workflow_tools <= set(profile_tool_names("core"))


def test_run_behavioral_simulation_schema_exposes_vcd_limit() -> None:
    assert "max_vcd_mb" in input_schema_properties("run_behavioral_simulation")


def test_collect_diagnostic_bundle_schema_exposes_audit_reuse() -> None:
    assert "reuse_audit_from_manifest" in input_schema_properties("collect_diagnostic_bundle")
    assert "audit_result" not in input_schema_properties("collect_diagnostic_bundle")
    assert "simulation_result" not in input_schema_properties("collect_diagnostic_bundle")


def test_handoff_tools_schema_exposes_report_manifest_path() -> None:
    assert "report_manifest_path" in input_schema_properties("run_pre_hw_signoff")
    assert "simulation_result" not in input_schema_properties("run_pre_hw_signoff")
    assert "report_manifest_path" in input_schema_properties("run_project_audit")
    assert "simulation_result" not in input_schema_properties("run_project_audit")


def test_timing_summary_schema_does_not_expose_arbitrary_tcl_command() -> None:
    assert "command" not in input_schema_properties("get_timing_summary")
    assert "project_dir" in input_schema_properties("run_project_audit")


def test_detect_vivado_environment_schema_exposes_launch_probe() -> None:
    properties = input_schema_properties("detect_vivado_environment")

    assert {"probe_launch", "probe_timeout_s", "runtime_dir"} <= properties


def test_vivado_path_parameters_are_documented_as_identity_assertions() -> None:
    tools_with_vivado_path = {
        name: spec
        for name, spec in TOOL_REGISTRY.items()
        if "vivado_path" in spec.input_schema.get("properties", {})
    }

    assert tools_with_vivado_path
    for spec in tools_with_vivado_path.values():
        description = spec.input_schema["properties"]["vivado_path"]["description"]
        assert "VIVADO_PATH" in description
        assert "cannot override" in description


def test_repair_project_setup_schema_is_registered() -> None:
    properties = input_schema_properties("repair_project_setup")

    assert {
        "project_path",
        "rtl_files",
        "xdc_files",
        "sim_files",
        "top",
        "testbench_top",
        "target_language",
        "include_dirs",
        "defines",
        "simulator",
        "dry_run",
        "timeout_s",
    } <= properties


def test_get_agent_scenarios_schema_is_registered() -> None:
    assert "scenario_id" in input_schema_properties("get_agent_scenarios")


def test_diagnose_run_failure_schema_is_registered() -> None:
    assert {"run_name", "timeout_s", "expect_bitstream"} <= input_schema_properties("diagnose_run_failure")


def test_next_actions_refer_to_existing_tools_and_schema_arguments(tmp_path) -> None:
    bitstream = tmp_path / "top.bit"
    bitstream.write_text("bitstream", encoding="utf-8")
    service = VivadoToolService()
    samples = [
        service.call("run_tcl", {"command": "file delete -force -- {demo.runs}"}),
        service.call("reset_runs", {}),
        service.call("clean_run_outputs", {}),
        service.call("validate_diagnostic_bundle", {}),
        service.call("program_hw_device", {"bitstream_path": str(bitstream)}),
    ]

    for result in samples:
        assert result.get("next_actions"), result
        for action in result["next_actions"]:
            assert action["tool"] in TOOL_REGISTRY
            assert set(action["required_args"]) <= input_schema_properties(action["tool"])


def test_existing_project_rebuild_actions_cover_required_schema_arguments(tmp_path) -> None:
    project_dir = tmp_path / "existing"
    project_dir.mkdir()
    (project_dir / "demo.xpr").write_text("# existing\n", encoding="utf-8")

    result = VivadoToolService().call(
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
    for action in result["next_actions"]:
        schema = TOOL_REGISTRY[action["tool"]].input_schema
        schema_required = set(schema.get("required", []))
        action_required = set(action["required_args"])
        assert schema_required <= action_required
        assert action_required <= input_schema_properties(action["tool"])
        assert action_required <= set(action["arg_sources"])
        assert all(
            forbidden not in str(source).lower()
            for source in action["arg_sources"].values()
            for forbidden in ("shell", "powershell", "terminal", "command line")
        )
    rebuild = next(action for action in result["next_actions"] if action["tool"] == "create_project")
    assert {
        "rtl_files",
        "xdc_files",
        "sim_files",
        "file_specs",
        "source_include_dirs",
        "source_defines",
        "include_dirs",
        "defines",
    } <= set(rebuild["required_args"])


def test_every_registry_schema_rejects_extra_missing_and_wrong_typed_arguments() -> None:
    wrong_values = {
        "boolean": "not-a-boolean",
        "integer": "not-an-integer",
        "number": "not-a-number",
        "string": 1,
        "array": {},
        "object": [],
    }
    for name, spec in TOOL_REGISTRY.items():
        assert any("is not allowed" in issue for issue in validate_tool_arguments(name, {"__unexpected__": True})), name
        required = spec.input_schema.get("required", [])
        missing_issues = validate_tool_arguments(name, {})
        for argument in required:
            assert any(f"arguments.{argument} is required" == issue for issue in missing_issues), (name, argument)
        properties = spec.input_schema.get("properties", {})
        for argument, schema in properties.items():
            expected_type = schema.get("type") if isinstance(schema, dict) else None
            if expected_type not in wrong_values:
                continue
            issues = validate_tool_arguments(name, {argument: wrong_values[expected_type]})
            assert any(issue.startswith(f"arguments.{argument} must have JSON type") for issue in issues), (name, argument)


def test_configure_run_schema_enforces_property_names_and_value_types_before_handler() -> None:
    service = VivadoToolService()

    unknown_property = service.call(
        "configure_run",
        {"run_name": "synth_1", "properties": {"STEPS.SYNTH_DESIGN.ARGS.FORGED": True}},
    )
    invalid_value = service.call(
        "configure_run",
        {"run_name": "synth_1", "properties": {"STEPS.SYNTH_DESIGN.ARGS.FLATTEN_HIERARCHY": []}},
    )

    assert unknown_property["ok"] is False
    assert unknown_property["error_code"] == "INVALID_TOOL_ARGUMENTS"
    assert unknown_property["data"]["handler_executed"] is False
    assert any("property name must be one of" in issue for issue in unknown_property["data"]["validation_errors"])
    assert invalid_value["ok"] is False
    assert invalid_value["error_code"] == "INVALID_TOOL_ARGUMENTS"
    assert invalid_value["data"]["handler_executed"] is False
    assert any("must have JSON type" in issue for issue in invalid_value["data"]["validation_errors"])
