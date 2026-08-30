from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

from vivado_agent_mcp.policy_pipeline import (
    POLICY_STAGE_ORDER,
    CapabilityPolicySnapshot,
    policy_stage_manifest,
    policy_stage_requirements,
)
from vivado_agent_mcp.registry import CAPABILITY_SPECS, TOOL_REGISTRY


POLICY_STAGE_MANIFEST_BASELINE_SHA256 = "4f7d6931715f7088f1f3675142d6a6d9a60295665b9ac516dc8be57f938ef948"
POLICY_STAGE_REQUIRED_COUNTS = {
    "executable_session_identity": 90,
    "argument_schema": 98,
    "capability_profile_authorization": 98,
    "managed_path_boundary": 80,
    "project_capability_generation": 70,
    "mutation_destructive_intent": 66,
    "hardware_programming_intent": 14,
    "execution_input_closure": 33,
    "command_execution_boundary": 93,
    "evidence_freshness_identity": 92,
    "audit_signoff_terminal_semantics": 5,
}


def test_registry_capabilities_satisfy_independent_cross_field_safety_invariants() -> None:
    violations: list[tuple[str, str]] = []
    hardware_risks = {"hardware", "hardware_destructive"}
    mutating_risks = {
        "destructive_dry_run",
        "hardware_destructive",
        "project_execution",
        "project_mutation_immediate",
    }
    composite_signatures = {
        ("normal", "project_open"),
        ("project_mutation_immediate", "managed_project_open"),
    }

    for capability in CAPABILITY_SPECS:
        def require(condition: bool, invariant: str) -> None:
            if not condition:
                violations.append((capability.name, invariant))

        require(capability.mutation is (not capability.read_only), "mutation/read_only")
        require(capability.idempotent is capability.read_only, "idempotent/read_only")
        require(
            not capability.destructive
            or (capability.mutation and not capability.read_only),
            "destructive/mutation",
        )
        require(
            capability.hardware is (capability.risk_class in hardware_risks),
            "hardware/risk",
        )
        require(
            capability.hardware is (capability.hardware_tier != "not_hardware"),
            "hardware/tier",
        )
        if capability.hardware:
            require(capability.profiles == frozenset({"all"}), "hardware/profiles")
            require(capability.task_eligible is False, "hardware/task")
        if capability.required_project_state in {
            "project_open",
            "managed_project_open",
        }:
            require(
                capability.required_session_state == "managed_session",
                "project/session",
            )
        if capability.required_project_state == "managed_project_open":
            require(capability.mutation, "managed_project/mutation")
        if capability.risk_class == "project_execution":
            require(
                capability.required_session_state == "managed_session"
                and capability.required_project_state == "managed_project_open"
                and capability.mutation
                and not capability.destructive,
                "project_execution",
            )
        if capability.risk_class == "project_mutation_immediate":
            require(
                capability.required_session_state == "managed_session"
                and capability.mutation,
                "project_mutation",
            )
        if capability.risk_class in {"destructive_dry_run", "hardware_destructive"}:
            require(
                capability.destructive and capability.mutation,
                "destructive_risk",
            )
        if capability.risk_class in mutating_risks:
            require(not capability.read_only, "mutating_risk/read_only")
        if capability.execution_input_policy == "blocks_unattested_composite":
            require(
                (
                    capability.risk_class,
                    capability.required_project_state,
                )
                in composite_signatures
                and capability.required_session_state == "managed_session"
                and capability.dispatch_lane == "serialized_backend"
                and capability.mutation
                and not capability.destructive
                and not capability.hardware,
                "composite_input",
            )
        if capability.dispatch_lane == "local":
            require(
                capability.risk_class == "normal"
                and capability.hardware_tier == "not_hardware"
                and capability.read_only
                and not capability.mutation
                and not capability.destructive
                and not capability.hardware
                and capability.required_session_state == "none"
                and capability.required_project_state == "none"
                and capability.execution_input_policy == "typed_tool_policy",
                "local_dispatch",
            )

    assert violations == []


def test_all_registry_capabilities_construct_valid_policy_snapshots() -> None:
    snapshots = tuple(
        CapabilityPolicySnapshot.from_spec(capability)
        for capability in CAPABILITY_SPECS
    )

    assert tuple(snapshot.name for snapshot in snapshots) == tuple(
        capability.name for capability in CAPABILITY_SPECS
    )


def test_every_capability_has_a_deterministic_complete_policy_stage_plan() -> None:
    manifest = policy_stage_manifest(CAPABILITY_SPECS)

    assert manifest["schema_version"] == 1
    assert manifest["capability_count"] == 98
    assert manifest["stage_order"] == list(POLICY_STAGE_ORDER)
    assert manifest["manifest_sha256"] == POLICY_STAGE_MANIFEST_BASELINE_SHA256
    assert [item["name"] for item in manifest["capabilities"]] == [
        capability.name for capability in CAPABILITY_SPECS
    ]
    for item in manifest["capabilities"]:
        required = item["required_stages"]
        not_applicable = item["not_applicable_stages"]
        assert required == [name for name in POLICY_STAGE_ORDER if name in required]
        assert not_applicable == [name for name in POLICY_STAGE_ORDER if name in not_applicable]
        assert set(required).isdisjoint(not_applicable)
        assert set(required) | set(not_applicable) == set(POLICY_STAGE_ORDER)


def test_policy_stage_classification_counts_and_strong_invariants_are_locked() -> None:
    counts: Counter[str] = Counter()
    for capability in CAPABILITY_SPECS:
        requirements = policy_stage_requirements(capability)
        counts.update(name for name, required in requirements.items() if required)

        assert tuple(requirements) == POLICY_STAGE_ORDER
        assert requirements["mutation_destructive_intent"] is (
            capability.mutation or capability.destructive
        )
        assert requirements["hardware_programming_intent"] is capability.hardware
        assert requirements["project_capability_generation"] is (
            capability.required_project_state != "none"
        )
        assert requirements["execution_input_closure"] is (
            capability.execution_input_policy == "blocks_unattested_composite"
            or capability.risk_class == "project_execution"
        )
        assert requirements["command_execution_boundary"] is (
            capability.dispatch_lane == "serialized_backend"
        )
        if capability.destructive:
            assert requirements["mutation_destructive_intent"] is True
        if capability.hardware:
            assert requirements["hardware_programming_intent"] is True
        if (
            requirements["executable_session_identity"]
            or requirements["project_capability_generation"]
            or requirements["hardware_programming_intent"]
        ):
            assert requirements["evidence_freshness_identity"] is True

    assert counts == Counter(POLICY_STAGE_REQUIRED_COUNTS)


@pytest.mark.parametrize(
    "tool_name",
    ["program_hw_device", "create_project", "get_project_info"],
)
def test_safety_classification_requires_evidence_despite_weak_contracts(
    tool_name: str,
) -> None:
    understated = replace(
        TOOL_REGISTRY[tool_name],
        evidence_contract=("unified_result_v1",),
        artifact_contract=("none",),
    )

    assert policy_stage_requirements(understated)["evidence_freshness_identity"] is True


def test_policy_stage_plan_sentinels_preserve_current_boundaries() -> None:
    catalog = policy_stage_requirements(TOOL_REGISTRY["get_tool_catalog"])
    safe_tcl = policy_stage_requirements(TOOL_REGISTRY["safe_tcl"])
    session_status = policy_stage_requirements(TOOL_REGISTRY["session_status"])
    create_project = policy_stage_requirements(TOOL_REGISTRY["create_project"])
    clean_runtime = policy_stage_requirements(TOOL_REGISTRY["clean_runtime_cache"])
    program = policy_stage_requirements(TOOL_REGISTRY["program_hw_device"])
    create_ip = policy_stage_requirements(TOOL_REGISTRY["create_ip"])
    synthesis = policy_stage_requirements(TOOL_REGISTRY["run_synthesis"])
    validate_bundle = policy_stage_requirements(TOOL_REGISTRY["validate_diagnostic_bundle"])

    assert {name for name, required in catalog.items() if required} == {
        "argument_schema",
        "capability_profile_authorization",
    }
    assert safe_tcl["managed_path_boundary"] is True
    assert {name for name, required in session_status.items() if required} == {
        "argument_schema",
        "capability_profile_authorization",
    }
    assert create_project["executable_session_identity"] is True
    assert create_project["project_capability_generation"] is False
    assert create_project["mutation_destructive_intent"] is True
    assert create_project["managed_path_boundary"] is True
    assert clean_runtime["executable_session_identity"] is True
    assert clean_runtime["mutation_destructive_intent"] is True
    assert program["hardware_programming_intent"] is True
    assert program["mutation_destructive_intent"] is True
    assert create_ip["project_capability_generation"] is True
    assert create_ip["execution_input_closure"] is True
    assert synthesis["project_capability_generation"] is True
    assert synthesis["execution_input_closure"] is True
    assert validate_bundle["executable_session_identity"] is False
    assert validate_bundle["project_capability_generation"] is True
    assert validate_bundle["evidence_freshness_identity"] is True
    assert validate_bundle["audit_signoff_terminal_semantics"] is True


def test_policy_stage_manifest_rejects_duplicate_capabilities() -> None:
    with pytest.raises(ValueError, match="Duplicate capability"):
        policy_stage_manifest((CAPABILITY_SPECS[0], CAPABILITY_SPECS[0]))
