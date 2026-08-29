from __future__ import annotations

import hashlib
import inspect
import json

import pytest

import vivado_agent_mcp.registry as registry_module
from vivado_agent_mcp.registry import (
    CAPABILITY_SPEC_VERSION,
    CAPABILITY_SPECS,
    CAPABILITY_DOMAIN_TOOL_NAMES,
    EXISTING_PROJECT_EXECUTION_TOOLS,
    IMMEDIATE_PROJECT_MUTATION_TOOLS,
    TOOL_DEFS,
    TOOL_REGISTRY,
    UNATTESTED_COMPOSITE_EXECUTION_TOOLS,
    capability_manifest,
    local_control_tool_names,
    profile_tool_names,
    tool_definitions,
)
from vivado_agent_mcp.tools import VivadoToolService
from vivado_agent_mcp import server as server_module
from vivado_agent_mcp.vivado.agent_catalog import TOOL_GROUPS, WORKFLOWS


POLICY_PROFILE_BASELINE_SHA256 = "bedfbbb85bfa62b792e93c06c58340c5b99daba418d9373eacd2afa61b24a7ad"
CATALOG_WORKFLOW_BASELINE_SHA256 = "121c430fa6d413bc9fb4e247ec812ee49a3f11555df4b50219074476c5a280ec"


def test_capability_specs_generate_registry_and_mcp_tool_definitions() -> None:
    assert len(CAPABILITY_SPECS) == 98
    assert [spec.name for spec in CAPABILITY_SPECS] == [tool.name for tool in TOOL_DEFS]
    assert set(TOOL_REGISTRY) == {spec.name for spec in CAPABILITY_SPECS}

    for spec, tool in zip(CAPABILITY_SPECS, TOOL_DEFS, strict=True):
        assert TOOL_REGISTRY[spec.name] is spec
        assert tool == spec.to_mcp_tool()
        assert tool.description == spec.description
        assert tool.inputSchema == spec.input_schema
        assert tool.outputSchema == spec.output_schema
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is spec.read_only
        assert tool.annotations.destructiveHint is spec.destructive
        assert tool.annotations.idempotentHint is spec.idempotent
        assert tool.annotations.openWorldHint is spec.open_world


def test_capability_specs_are_complete_and_conservative() -> None:
    service = VivadoToolService()
    allowed_profiles = {"core", "advanced", "all"}
    allowed_durations = {"short", "medium", "long"}
    allowed_dispatch_lanes = {"local", "serialized_backend"}
    allowed_session_states = {"none", "managed_session", "session_stopped_for_mutation"}
    allowed_project_states = {"none", "project_open", "managed_project_open", "project_or_manifest"}

    for spec in CAPABILITY_SPECS:
        assert spec.domain and spec.domain != "unclassified"
        assert spec.handler == f"_{spec.name}"
        assert hasattr(service, spec.handler)
        assert len(inspect.signature(getattr(service, spec.handler)).parameters) == 1
        assert spec.profiles and spec.profiles <= allowed_profiles
        assert "all" in spec.profiles
        assert spec.duration_class in allowed_durations
        assert spec.dispatch_lane in allowed_dispatch_lanes
        assert spec.required_session_state in allowed_session_states
        assert spec.required_project_state in allowed_project_states
        assert spec.mutation is (not spec.read_only)
        assert not (spec.hardware and "core" in spec.profiles)
        assert not (spec.hardware and "advanced" in spec.profiles)
        assert not (spec.task_eligible and spec.hardware)
        assert spec.supported_vivado_versions in {(), ("2021.2",)}
        assert spec.qualified_vivado_versions == ()
        assert spec.evidence_contract
        assert spec.evidence_contract[0] == "unified_result_v1"

    with pytest.raises(ValueError, match="explicit risk class"):
        registry_module._risk_for_tool("unclassified_tool")
    with pytest.raises(ValueError, match="explicit exposure class"):
        registry_module._profiles_for_tool("unclassified_tool", hardware=False)
    with pytest.raises(ValueError, match="hardware classification"):
        registry_module._hardware_tier_for_tool("unclassified_tool")


def test_policy_and_profile_projection_matches_pre_capability_baseline() -> None:
    payload = {
        "tools": {
            name: {
                "risk": spec.risk,
                "profiles": [
                    profile
                    for profile in ("core", "advanced", "all")
                    if name in profile_tool_names(profile)
                ],
            }
            for name, spec in sorted(TOOL_REGISTRY.items())
        }
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert digest == POLICY_PROFILE_BASELINE_SHA256
    assert IMMEDIATE_PROJECT_MUTATION_TOOLS == {
        spec.name for spec in CAPABILITY_SPECS if spec.risk == "project_mutation_immediate"
    }
    assert EXISTING_PROJECT_EXECUTION_TOOLS == {
        spec.name
        for spec in CAPABILITY_SPECS
        if spec.risk in {"project_execution", "destructive_dry_run"}
        and spec.required_project_state == "managed_project_open"
    }
    assert UNATTESTED_COMPOSITE_EXECUTION_TOOLS == {
        spec.name for spec in CAPABILITY_SPECS if spec.execution_input_policy == "blocks_unattested_composite"
    }

    for spec in CAPABILITY_SPECS:
        if spec.risk in {
            "project_mutation_immediate",
            "project_execution",
            "destructive_dry_run",
            "hardware_destructive",
        }:
            assert spec.read_only is False
        if spec.destructive:
            assert spec.read_only is False
            assert spec.mutation is True
    assert TOOL_REGISTRY["run_tcl"].risk == "tcl_policy_dry_run"
    assert TOOL_REGISTRY["safe_tcl"].risk == "tcl_policy_dry_run"
    assert TOOL_REGISTRY["run_tcl"].read_only is True
    assert TOOL_REGISTRY["safe_tcl"].read_only is True
    assert TOOL_REGISTRY["remove_project_files"].destructive is True
    assert TOOL_REGISTRY["remove_signoff_waiver"].destructive is True


def test_agent_catalog_groups_and_workflow_tags_are_capability_projections() -> None:
    grouped_names: list[str] = []
    for group in TOOL_GROUPS:
        names = list(group["tools"])
        grouped_names.extend(names)
        assert names == list(CAPABILITY_DOMAIN_TOOL_NAMES[group["id"]])
        assert all(TOOL_REGISTRY[name].domain == group["id"] for name in names)

    assert len(grouped_names) == len(set(grouped_names))
    assert set(grouped_names) == {spec.name for spec in CAPABILITY_SPECS}

    expected_tags = {
        spec.name: {
            workflow["id"]
            for workflow in WORKFLOWS
            if spec.name in workflow["tool_sequence"]
        }
        for spec in CAPABILITY_SPECS
    }
    assert {spec.name: set(spec.workflow_tags) for spec in CAPABILITY_SPECS} == expected_tags
    payload = {
        "groups": [(group["id"], group["tools"]) for group in TOOL_GROUPS],
        "workflows": [(workflow["id"], workflow["tool_sequence"]) for workflow in WORKFLOWS],
    }
    assert hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest() == CATALOG_WORKFLOW_BASELINE_SHA256


def test_capability_manifest_is_deterministic_and_json_serializable() -> None:
    first = capability_manifest()
    second = capability_manifest()

    assert first == second
    assert first["schema_version"] == CAPABILITY_SPEC_VERSION
    assert first["capability_count"] == len(CAPABILITY_SPECS)
    assert json.dumps(first, sort_keys=True, separators=(",", ":"))
    digest_payload = dict(first)
    capability_digest = digest_payload.pop("capability_digest")
    assert capability_digest == hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert [item["name"] for item in first["capabilities"]] == [spec.name for spec in CAPABILITY_SPECS]
    assert all(
        item["hardware_validation_status"] == "NOT_VALIDATED"
        for item in first["capabilities"]
        if item["hardware"]
    )
    with pytest.raises(ValueError, match="Unknown capability"):
        capability_manifest({"not_a_registered_tool"})


def test_catalog_and_dispatch_lanes_are_generated_from_capability_specs() -> None:
    service = VivadoToolService()
    result = service.call("get_tool_catalog", {})
    full_result = service.call("get_tool_catalog", {"detail": "full"})

    assert result["ok"] is True
    assert result["data"]["capability_spec_version"] == CAPABILITY_SPEC_VERSION
    assert result["data"]["support_status"]["task_extension_status"] == (
        "NOT_AVAILABLE_ON_CURRENT_MCP_SDK_V1"
    )
    assert result["data"]["capability_digest"] == capability_manifest(set(TOOL_REGISTRY))[
        "capability_digest"
    ]
    assert result["data"]["capability_detail"] == "compact"
    assert full_result["data"]["capability_detail"] == "full"
    metadata = result["data"]["tool_metadata"]
    for name, item in metadata.items():
        spec = TOOL_REGISTRY[name]
        assert item["risk"] == spec.risk
        compact_annotations = {
            key: value
            for key, value in {
                "read_only": spec.read_only,
                "destructive": spec.destructive,
            }.items()
            if value
        }
        if compact_annotations:
            assert item["annotations"] == compact_annotations
        else:
            assert "annotations" not in item
        if spec.hardware:
            assert item["hardware_validation_status"] == "NOT_VALIDATED"
        else:
            assert "hardware_validation_status" not in item
        full_item = full_result["data"]["tool_metadata"][name]
        assert full_item["domain"] == spec.domain
        assert full_item["annotations"] == {
            "read_only": spec.read_only,
            "destructive": spec.destructive,
            "idempotent": spec.idempotent,
            "open_world": spec.open_world,
        }
        assert full_item["profiles"] == sorted(spec.profiles)
        assert full_item["workflow_tags"] == list(spec.workflow_tags)
        assert full_item["required_session_state"] == spec.required_session_state
        assert full_item["required_project_state"] == spec.required_project_state
        assert full_item["dispatch_lane"] == spec.dispatch_lane

    expected_local = {
        spec.name for spec in CAPABILITY_SPECS if spec.dispatch_lane == "local"
    }
    assert local_control_tool_names() == expected_local
    assert server_module._LOCAL_CONTROL_TOOLS == expected_local


def test_core_capability_projection_stays_within_agent_context_budget() -> None:
    core_names = profile_tool_names("core")
    serialized_tools = json.dumps(
        [
            tool.model_dump(by_alias=True, exclude_none=True)
            for tool in tool_definitions(core_names)
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    service = VivadoToolService(tool_profile="core", enforce_tool_profile=True)
    compact_catalog = json.dumps(
        service.call("get_tool_catalog", {}),
        separators=(",", ":"),
    ).encode("utf-8")
    full_catalog = json.dumps(
        service.call("get_tool_catalog", {"detail": "full"}),
        separators=(",", ":"),
    ).encode("utf-8")

    assert len(serialized_tools) <= 73_000
    assert len(compact_catalog) <= 11_000
    assert len(full_catalog) <= 36_000
