from __future__ import annotations

import copy
import json
import pickle
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from threading import RLock
from typing import Any
from weakref import ref

import pytest

import vivado_agent_mcp.policy_pipeline as policy_pipeline_module
from vivado_agent_mcp.policy_pipeline import (
    POLICY_DECISION_MAX_BYTES,
    POLICY_PIPELINE_CONFIGURATION_INVALID,
    POLICY_STAGE_ORDER,
    POST_EXECUTION_POLICY_STAGE_ORDER,
    PRE_EXECUTION_POLICY_STAGE_ORDER,
    PolicyContext,
    PolicyDecision,
    PolicyPipeline,
    PolicyStageResult,
    PostExecutionPolicyContext,
    policy_stage_requirements,
)
from vivado_agent_mcp.registry import CAPABILITY_SPECS, TOOL_REGISTRY, validate_tool_arguments


_PRODUCT_CANONICAL_PRE_EXECUTION_PIPELINE = (
    policy_pipeline_module._CANONICAL_PRE_EXECUTION_PIPELINE
)
_PRODUCT_AUTHORITY_VERIFIED = (
    policy_pipeline_module._PreExecutionAllowIssuer._authority_verified
)
_TEST_TRUSTED_AUTHORITIES: dict[
    int,
    tuple[
        ref[PolicyPipeline],
        ref[Any],
        tuple[Any, ...],
        tuple[Any, ...],
    ],
] = {}


def _test_authority_verified(
    issuer: Any,
    pipeline: PolicyPipeline,
) -> bool:
    trusted = _TEST_TRUSTED_AUTHORITIES.get(id(issuer))
    if trusted is not None:
        pipeline_ref, issuer_ref, trusted_stages, trusted_evaluators = trusted
        try:
            current_stages = tuple(object.__getattribute__(pipeline, "_stages"))
            current_evaluators = tuple(
                getattr(
                    getattr(stage, "evaluate"),
                    "__func__",
                    getattr(stage, "evaluate"),
                )
                for stage in current_stages
            )
        except Exception:
            return False
        if (
            pipeline_ref() is pipeline
            and issuer_ref() is issuer
            and len(current_stages) == len(trusted_stages)
            and all(
                observed is expected
                for observed, expected in zip(
                    current_stages,
                    trusted_stages,
                    strict=True,
                )
            )
            and len(current_evaluators) == len(trusted_evaluators)
            and all(
                observed is expected
                for observed, expected in zip(
                    current_evaluators,
                    trusted_evaluators,
                    strict=True,
                )
            )
        ):
            return True
    return _PRODUCT_AUTHORITY_VERIFIED(issuer, pipeline)


policy_pipeline_module._PreExecutionAllowIssuer._authority_verified = (
    _test_authority_verified
)


class _RecordingStage:
    def __init__(
        self,
        name: str,
        calls: list[str],
        *,
        allowed: bool = True,
        reason_code: str = "POLICY_STAGE_ALLOWED",
        error: Exception | None = None,
        evidence: dict[str, Any] | None = None,
        applicable: bool = True,
    ) -> None:
        self.name = name
        self._calls = calls
        self._allowed = allowed
        self._reason_code = reason_code
        self._error = error
        self._evidence = evidence or {}
        self._applicable = applicable

    def evaluate(self, context: PolicyContext) -> PolicyStageResult:
        self._calls.append(self.name)
        if self._error is not None:
            raise self._error
        return PolicyStageResult(
            stage=self.name,
            allowed=self._allowed,
            reason_code=self._reason_code,
            message=f"{self.name} evaluated {context.capability_name}.",
            evidence=self._evidence,
            stop_required=not self._allowed,
            applicable=self._applicable,
        )


def _context(tool_name: str = "get_tool_catalog") -> PolicyContext:
    return PolicyContext.create(
        capability=TOOL_REGISTRY[tool_name],
        arguments={"detail": "compact"} if tool_name == "get_tool_catalog" else {},
        active_profile="core",
        profile_enforced=True,
        caller_identity=None,
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id="policy-test-request",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )


def _pipeline(
    calls: list[str],
    *,
    blocking_stage: str | None = None,
    error_stage: str | None = None,
    stage_names: tuple[str, ...] = PRE_EXECUTION_POLICY_STAGE_ORDER,
    phase: str = "pre_execution",
    non_applicable_stage: str | None = None,
) -> PolicyPipeline:
    stages = [
        _RecordingStage(
            name,
            calls,
            allowed=name != blocking_stage,
            reason_code="TEST_BLOCK" if name == blocking_stage else "POLICY_STAGE_ALLOWED",
            error=RuntimeError("boom") if name == error_stage else None,
            applicable=name != non_applicable_stage,
        )
        for name in stage_names
    ]
    pipeline = PolicyPipeline(stages, phase=phase)
    if phase != "pre_execution":
        return pipeline
    issuer = object.__new__(policy_pipeline_module._PreExecutionAllowIssuer)
    object.__setattr__(issuer, "_evaluation_code", PolicyPipeline.evaluate.__code__)
    object.__setattr__(
        issuer,
        "_post_context_code",
        PostExecutionPolicyContext.__post_init__.__code__,
    )
    object.__setattr__(issuer, "_lock", RLock())
    object.__setattr__(issuer, "_next_serial", 1)
    object.__setattr__(issuer, "_next_post_serial", 1)
    object.__setattr__(issuer, "_pipeline_ref", ref(pipeline))
    object.__setattr__(issuer, "_records", {})
    object.__setattr__(issuer, "_post_records", {})
    trusted_stages = tuple(object.__getattribute__(pipeline, "_stages"))
    trusted_evaluators = tuple(
        getattr(
            getattr(stage, "evaluate"),
            "__func__",
            getattr(stage, "evaluate"),
        )
        for stage in trusted_stages
    )
    object.__setattr__(pipeline, "_issuer", issuer)
    _TEST_TRUSTED_AUTHORITIES[id(issuer)] = (
        ref(pipeline),
        ref(issuer),
        trusted_stages,
        trusted_evaluators,
    )
    return pipeline


def _post_context(tool_name: str = "collect_diagnostic_bundle") -> PostExecutionPolicyContext:
    pre_context = _context(tool_name)
    pre_pipeline = _pipeline([])
    pre_decision = pre_pipeline.evaluate(pre_context)
    assert pre_decision.allowed is True
    return PostExecutionPolicyContext.create(
        pre_execution_context=pre_context,
        pre_execution_decision=pre_decision,
        trusted_pre_execution_pipeline=pre_pipeline,
        execution_result={"ok": True, "data": {"status": "WARN"}},
        evidence_snapshot={"manifest_sha256": "a" * 64},
        completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
    )


def _reconstruct_decision(decision: PolicyDecision) -> PolicyDecision:
    return PolicyDecision(
        capability_name=decision.capability_name,
        allowed=decision.allowed,
        stage=decision.stage,
        reason_code=decision.reason_code,
        message=decision.message,
        evidence=decision.evidence,
        stop_required=decision.stop_required,
        stage_results=decision.stage_results,
        request_id=decision.request_id,
        started_at=decision.started_at,
        capability_policy_sha256=decision.capability_policy_sha256,
        context_identity_sha256=decision.context_identity_sha256,
        phase=decision.phase,
        mode=decision.mode,
    )


def test_policy_stage_order_is_explicit_immutable_and_split_by_phase() -> None:
    assert isinstance(POLICY_STAGE_ORDER, tuple)
    assert POLICY_STAGE_ORDER == (
        "executable_session_identity",
        "argument_schema",
        "capability_profile_authorization",
        "managed_path_boundary",
        "project_capability_generation",
        "mutation_destructive_intent",
        "hardware_programming_intent",
        "execution_input_closure",
        "command_execution_boundary",
        "evidence_freshness_identity",
        "audit_signoff_terminal_semantics",
    )
    assert PRE_EXECUTION_POLICY_STAGE_ORDER == POLICY_STAGE_ORDER[:9]
    assert POST_EXECUTION_POLICY_STAGE_ORDER == POLICY_STAGE_ORDER[9:]


def test_policy_pipeline_phase_and_registered_order_are_immutable() -> None:
    pipeline = _pipeline([])

    with pytest.raises(FrozenInstanceError):
        pipeline.phase = "post_execution"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        pipeline._phase = "post_execution"  # type: ignore[misc]

    assert pipeline.phase == "pre_execution"
    assert pipeline.stage_names == PRE_EXECUTION_POLICY_STAGE_ORDER


def test_product_canonical_authority_is_internal_and_fail_closed() -> None:
    assert not hasattr(
        policy_pipeline_module,
        "_create_trusted_pre_execution_pipeline",
    )
    assert not hasattr(
        policy_pipeline_module,
        "_TRUSTED_PRE_EXECUTION_ISSUER_KEY",
    )
    assert not hasattr(policy_pipeline_module, "_CANONICAL_AUTHORITY_SEAL")
    assert not hasattr(policy_pipeline_module, "_CANONICAL_AUTHORITY_VERIFY")

    decision = _PRODUCT_CANONICAL_PRE_EXECUTION_PIPELINE.evaluate(_context())

    assert decision.allowed is False
    assert decision.reason_code == "POLICY_PIPELINE_AUTHORITY_UNAVAILABLE"
    assert object.__getattribute__(decision, "_provenance") is None


def test_product_canonical_authority_rejects_replaced_stage_composition() -> None:
    pipeline = _PRODUCT_CANONICAL_PRE_EXECUTION_PIPELINE
    issuer = object.__getattribute__(pipeline, "_issuer")
    original_stages = object.__getattribute__(pipeline, "_stages")
    replacement_calls: list[str] = []
    replacement_stages = tuple(
        _RecordingStage(name, replacement_calls)
        for name in PRE_EXECUTION_POLICY_STAGE_ORDER
    )

    with pytest.raises(AttributeError):
        object.__setattr__(issuer, "_trusted_stages", replacement_stages)
    with pytest.raises(AttributeError):
        object.__setattr__(issuer, "_trusted_stage_evaluators", ())

    try:
        object.__setattr__(pipeline, "_stages", replacement_stages)
        decision = pipeline.evaluate(_context())
    finally:
        object.__setattr__(pipeline, "_stages", original_stages)

    assert replacement_calls == list(PRE_EXECUTION_POLICY_STAGE_ORDER)
    assert decision.allowed is False
    assert decision.reason_code == POLICY_PIPELINE_CONFIGURATION_INVALID
    assert object.__getattribute__(decision, "_provenance") is None


@pytest.mark.parametrize(
    "code_field",
    ["_evaluation_code", "_post_context_code"],
)
def test_product_canonical_authority_seals_lifecycle_code(
    code_field: str,
) -> None:
    pipeline = _PRODUCT_CANONICAL_PRE_EXECUTION_PIPELINE
    issuer = object.__getattribute__(pipeline, "_issuer")
    original_code = object.__getattribute__(issuer, code_field)
    replacement_code = (lambda: None).__code__

    try:
        object.__setattr__(issuer, code_field, replacement_code)
        assert issuer._canonical_authority_verified(pipeline) is False
    finally:
        object.__setattr__(issuer, code_field, original_code)

    assert issuer._canonical_authority_verified(pipeline) is True


def test_product_canonical_runtime_rejects_direct_reflective_issuance() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    forged_decision = _reconstruct_decision(_pipeline([]).evaluate(pre_context))
    product_pipeline = _PRODUCT_CANONICAL_PRE_EXECUTION_PIPELINE
    product_issuer = object.__getattribute__(product_pipeline, "_issuer")
    runtime = (
        policy_pipeline_module._PreExecutionAllowIssuer
        ._issue_from_completed_evaluation.__kwdefaults__["_canonical_runtime"]
    )

    serial = runtime(
        "issue_pre",
        product_pipeline,
        product_issuer,
        forged_decision,
        pre_context,
        policy_pipeline_module._pre_execution_authorization_fingerprint(
            forged_decision
        ),
    )

    assert serial is None


def test_product_canonical_authority_ignores_mutable_issuer_record_ledgers() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    test_pipeline = _pipeline([])
    forged_decision = _reconstruct_decision(test_pipeline.evaluate(pre_context))
    product_pipeline = _PRODUCT_CANONICAL_PRE_EXECUTION_PIPELINE
    product_issuer = object.__getattribute__(product_pipeline, "_issuer")
    serial = 987_654_321
    product_issuer._records[serial] = policy_pipeline_module._IssuedPreExecutionAllow(
        decision_ref=ref(forged_decision),
        context_ref=ref(pre_context),
        authorization_fingerprint=(
            policy_pipeline_module._pre_execution_authorization_fingerprint(
                forged_decision
            )
        ),
    )
    object.__setattr__(
        forged_decision,
        "_provenance",
        policy_pipeline_module._PreExecutionAllowGrant(product_issuer, serial),
    )

    try:
        with pytest.raises(ValueError, match="pipeline-issued"):
            PostExecutionPolicyContext.create(
                pre_execution_context=pre_context,
                pre_execution_decision=forged_decision,
                trusted_pre_execution_pipeline=product_pipeline,
                execution_result={"ok": True},
                evidence_snapshot={},
                completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
            )
    finally:
        product_issuer._records.pop(serial, None)


def test_policy_context_is_deeply_immutable_and_detached_from_inputs() -> None:
    arguments = {"files": ["a.sv"], "nested": {"value": 1}}
    context = PolicyContext.create(
        capability=TOOL_REGISTRY["add_project_files"],
        arguments=arguments,
        active_profile="all",
        profile_enforced=True,
        caller_identity={"transport": "stdio"},
        session_identity={"generation": 7},
        project_capability={"project": "demo"},
        trusted_vivado_identity={"sha256": "a" * 64},
        request_id="immutable-request",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    arguments["files"].append("changed.sv")
    arguments["nested"]["value"] = 2

    projected = context.arguments
    assert isinstance(projected, dict)
    assert isinstance(projected["files"], list)
    assert projected["files"] == ["a.sv"]
    assert context.arguments["nested"]["value"] == 1
    projected["files"].append("projection-only.sv")
    projected["nested"]["value"] = 9
    projected["new"] = True
    assert context.arguments == {"files": ["a.sv"], "nested": {"value": 1}}
    with pytest.raises(FrozenInstanceError):
        context.request_id = "changed"  # type: ignore[misc]


def test_policy_context_capability_is_detached_from_mutable_registry_schema() -> None:
    registry_capability = TOOL_REGISTRY["add_project_files"]
    original_input_type = registry_capability.input_schema["properties"]["files"]["items"]["type"]
    context = PolicyContext.create(
        capability=registry_capability,
        arguments={"files": ["a.sv"]},
        active_profile="all",
        profile_enforced=True,
        caller_identity=None,
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id="capability-snapshot",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    projected_schema = context.capability.input_schema
    projected_schema["properties"]["files"]["items"]["type"] = "integer"
    projected_schema["properties"]["injected"] = {"type": "string"}

    assert context.capability.input_schema["properties"]["files"]["items"]["type"] == "string"
    assert "injected" not in context.capability.input_schema["properties"]
    assert registry_capability.input_schema["properties"]["files"]["items"]["type"] == original_input_type
    assert "injected" not in registry_capability.input_schema["properties"]
    with pytest.raises(FrozenInstanceError):
        context.capability.name = "changed"  # type: ignore[misc]


def test_policy_context_revalidates_supplied_capability_snapshot() -> None:
    snapshot = policy_pipeline_module.CapabilityPolicySnapshot.from_spec(
        TOOL_REGISTRY["get_tool_catalog"]
    )
    object.__setattr__(snapshot, "mutation", True)

    with pytest.raises(ValueError, match="risk boolean invariant"):
        PolicyContext.create(
            capability=snapshot,
            arguments={"detail": "compact"},
            active_profile="core",
            profile_enforced=True,
            caller_identity=None,
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="mutated-capability-snapshot",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )


def test_policy_context_detaches_mutated_capability_snapshot_schema() -> None:
    snapshot = policy_pipeline_module.CapabilityPolicySnapshot.from_spec(
        TOOL_REGISTRY["get_tool_catalog"]
    )
    shared_schema = {
        "type": "object",
        "properties": {"detail": {"type": "string"}},
        "additionalProperties": False,
    }
    object.__setattr__(snapshot, "_input_schema_snapshot", shared_schema)

    context = PolicyContext.create(
        capability=snapshot,
        arguments={"detail": "compact"},
        active_profile="core",
        profile_enforced=True,
        caller_identity=None,
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id="detached-capability-snapshot",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    shared_schema["properties"]["output_path"] = {"type": "string"}

    assert context.capability is not snapshot
    assert "output_path" not in context.capability.input_schema["properties"]


def test_direct_context_construction_cannot_bypass_deep_snapshot_invariants() -> None:
    mutable_capability = replace(
        TOOL_REGISTRY["get_tool_catalog"],
        input_schema=deepcopy(TOOL_REGISTRY["get_tool_catalog"].input_schema),
    )
    arguments = {"nested": ["original"]}
    caller_identity = {"transport": {"name": "stdio"}}
    context = PolicyContext(
        capability=mutable_capability,  # type: ignore[arg-type]
        _arguments_snapshot=arguments,
        active_profile=" core ",
        profile_enforced=True,
        caller_identity=caller_identity,
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id=" direct-context ",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    arguments["nested"].append("mutated")
    caller_identity["transport"]["name"] = "changed"
    mutable_capability.input_schema["properties"]["injected"] = {"type": "string"}

    assert context.arguments == {"nested": ["original"]}
    assert context.caller_identity["transport"]["name"] == "stdio"
    assert "injected" not in context.capability.input_schema["properties"]
    assert context.active_profile == "core"
    assert context.request_id == "direct-context"


def test_policy_context_schema_projection_preserves_dict_list_and_invalid_key_semantics() -> None:
    valid = PolicyContext.create(
        capability=TOOL_REGISTRY["add_project_files"],
        arguments={"files": ["a.sv", "b.sv"]},
        active_profile="all",
        profile_enforced=True,
        caller_identity=None,
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id="schema-compatible",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    invalid = PolicyContext.create(
        capability=TOOL_REGISTRY["add_project_files"],
        arguments={1: "must-not-be-stringified"},
        active_profile="all",
        profile_enforced=True,
        caller_identity=None,
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id="schema-invalid-key",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    tuple_array = PolicyContext.create(
        capability=TOOL_REGISTRY["add_project_files"],
        arguments={"files": ("a.sv",)},
        active_profile="all",
        profile_enforced=True,
        caller_identity=None,
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id="schema-tuple-array",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    assert validate_tool_arguments("add_project_files", valid.arguments) == []
    assert isinstance(valid.arguments, dict)
    assert isinstance(valid.arguments["files"], list)
    assert 1 in invalid.arguments
    assert validate_tool_arguments("add_project_files", invalid.arguments)
    assert isinstance(tuple_array.arguments["files"], tuple)
    assert validate_tool_arguments("add_project_files", tuple_array.arguments)


def test_policy_context_rejects_mutable_identity_leaves_and_opaques_invalid_arguments() -> None:
    class MutableLeaf:
        def __init__(self) -> None:
            self.values = ["original"]

    class MutableFloat(float):
        def __new__(cls, value: float):
            instance = super().__new__(cls, value)
            instance.marker = "A"
            return instance

        def hex(self) -> str:
            return self.marker

    byte_leaf = bytearray(b"original")
    custom_leaf = MutableLeaf()
    mutable_float = MutableFloat(1.5)
    for leaf in (byte_leaf, custom_leaf, mutable_float):
        with pytest.raises(TypeError, match="unsupported leaf type"):
            PolicyContext.create(
                capability=TOOL_REGISTRY["get_tool_catalog"],
                arguments={},
                active_profile="core",
                profile_enforced=True,
                caller_identity={"leaf": leaf},
                session_identity=None,
                project_capability=None,
                trusted_vivado_identity=None,
                request_id="immutable-leaf",
                started_at=datetime(2026, 8, 30, tzinfo=UTC),
            )

    invalid_argument = PolicyContext.create(
        capability=TOOL_REGISTRY["get_tool_catalog"],
        arguments={"detail": byte_leaf},
        active_profile="core",
        profile_enforced=True,
        caller_identity=None,
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id="opaque-invalid-argument",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    assert validate_tool_arguments("get_tool_catalog", invalid_argument.arguments)
    assert not isinstance(invalid_argument.arguments["detail"], bytearray)


def test_policy_context_bounds_deep_arguments_and_rejects_deep_identity_before_recursion() -> None:
    deeply_nested: Any = "leaf"
    for _ in range(1_500):
        deeply_nested = [deeply_nested]

    with pytest.raises(ValueError, match="snapshot depth limit"):
        PolicyContext.create(
            capability=TOOL_REGISTRY["get_tool_catalog"],
            arguments={"detail": deeply_nested},
            active_profile="core",
            profile_enforced=True,
            caller_identity=None,
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="deep-invalid-argument",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="depth limit"):
        PolicyContext.create(
            capability=TOOL_REGISTRY["get_tool_catalog"],
            arguments={},
            active_profile="core",
            profile_enforced=True,
            caller_identity={"nested": deeply_nested},
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="deep-invalid-identity",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )


def test_policy_context_revalidates_pre_frozen_containers_against_global_limits() -> None:
    deeply_frozen: Any = "leaf"
    for _ in range(1_500):
        deeply_frozen = policy_pipeline_module._FrozenList((deeply_frozen,))

    with pytest.raises(ValueError, match="snapshot depth limit"):
        PolicyContext.create(
            capability=TOOL_REGISTRY["get_tool_catalog"],
            arguments={"detail": deeply_frozen},
            active_profile="core",
            profile_enforced=True,
            caller_identity=None,
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="deep-pre-frozen-argument",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )

    wide_frozen = policy_pipeline_module._FrozenList(tuple(range(5_000)))
    with pytest.raises(ValueError, match="snapshot node budget"):
        PolicyContext.create(
            capability=TOOL_REGISTRY["get_tool_catalog"],
            arguments={"detail": wide_frozen},
            active_profile="core",
            profile_enforced=True,
            caller_identity=None,
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="wide-pre-frozen-argument",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )


def test_policy_context_rejects_lossy_wide_snapshots_and_retains_in_budget_tail() -> None:
    over_budget_prefix = {f"key_{index:04d}": index for index in range(3_000)}
    for tail in ("A", "B"):
        with pytest.raises(ValueError, match="node budget"):
            PolicyContext.create(
                capability=TOOL_REGISTRY["get_tool_catalog"],
                arguments={},
                active_profile="core",
                profile_enforced=True,
                caller_identity={**over_budget_prefix, "zz_tail": tail},
                session_identity=None,
                project_capability=None,
                trusted_vivado_identity=None,
                request_id=f"wide-identity-{tail}",
                started_at=datetime(2026, 8, 30, tzinfo=UTC),
            )

    in_budget_prefix = {f"key_{index:04d}": index for index in range(1_000)}
    contexts = [
        PolicyContext.create(
            capability=TOOL_REGISTRY["get_tool_catalog"],
            arguments={},
            active_profile="core",
            profile_enforced=True,
            caller_identity={**in_budget_prefix, "zz_tail": tail},
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="same-wide-identity-request",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )
        for tail in ("A", "B")
    ]

    assert contexts[0].caller_identity["zz_tail"] == "A"
    assert contexts[1].caller_identity["zz_tail"] == "B"
    assert contexts[0].policy_identity_sha256 != contexts[1].policy_identity_sha256


def test_policy_context_uses_one_snapshot_budget_across_arguments_and_identities() -> None:
    wide_arguments = {f"arg_{index:04d}": index for index in range(1_100)}
    wide_identity = {f"identity_{index:04d}": index for index in range(1_100)}

    with pytest.raises(ValueError, match="snapshot node budget"):
        PolicyContext.create(
            capability=TOOL_REGISTRY["get_tool_catalog"],
            arguments=wide_arguments,
            active_profile="core",
            profile_enforced=True,
            caller_identity=wide_identity,
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="combined-budget",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "oversized_leaf",
    [
        "x" * (policy_pipeline_module._MAX_SNAPSHOT_LEAF_BYTES + 1),
        b"x" * (policy_pipeline_module._MAX_SNAPSHOT_LEAF_BYTES + 1),
    ],
    ids=("string", "bytes"),
)
def test_policy_context_rejects_oversized_snapshot_leaves(
    oversized_leaf: str | bytes,
) -> None:
    with pytest.raises(ValueError, match="snapshot leaf or shared byte budget"):
        PolicyContext.create(
            capability=TOOL_REGISTRY["get_tool_catalog"],
            arguments={"detail": oversized_leaf},
            active_profile="core",
            profile_enforced=True,
            caller_identity=None,
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="oversized-leaf",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )


def test_policy_context_uses_one_byte_budget_across_arguments_and_identities() -> None:
    chunk = "x" * (policy_pipeline_module._MAX_SNAPSHOT_LEAF_BYTES - 1)
    arguments = {f"arg_{index:02d}": chunk for index in range(8)}
    caller_identity = {f"identity_{index:02d}": chunk for index in range(9)}

    with pytest.raises(ValueError, match="snapshot leaf or shared byte budget"):
        PolicyContext.create(
            capability=TOOL_REGISTRY["get_tool_catalog"],
            arguments=arguments,
            active_profile="core",
            profile_enforced=True,
            caller_identity=caller_identity,
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="combined-byte-budget",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )


def test_capability_schema_snapshots_reject_lossy_individual_and_combined_budgets() -> None:
    base = TOOL_REGISTRY["get_tool_catalog"]
    oversized_schema = {f"field_{index:04d}": index for index in range(3_000)}
    combined_schema = {f"field_{index:04d}": index for index in range(1_100)}

    for capability in (
        replace(base, input_schema=oversized_schema),
        replace(
            base,
            input_schema=combined_schema,
            output_schema=combined_schema,
        ),
    ):
        with pytest.raises(ValueError, match="snapshot node budget"):
            PolicyContext.create(
                capability=capability,
                arguments={},
                active_profile="core",
                profile_enforced=True,
                caller_identity=None,
                session_identity=None,
                project_capability=None,
                trusted_vivado_identity=None,
                request_id="oversized-capability-schema",
                started_at=datetime(2026, 8, 30, tzinfo=UTC),
            )


@pytest.mark.parametrize(
    ("invalid_schema", "error_type"),
    [
        ({"type": {"object"}}, TypeError),
        ({"const": b"secret"}, TypeError),
        ({"const": Path("schema.json")}, TypeError),
        ({"const": float("nan")}, ValueError),
    ],
)
def test_capability_schema_snapshots_require_canonical_json_values(
    invalid_schema: dict[str, Any],
    error_type: type[Exception],
) -> None:
    capability = replace(
        TOOL_REGISTRY["get_tool_catalog"],
        input_schema=invalid_schema,
    )

    with pytest.raises(error_type):
        PolicyContext.create(
            capability=capability,
            arguments={},
            active_profile="core",
            profile_enforced=True,
            caller_identity=None,
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="non-json-capability-schema",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )


@pytest.mark.parametrize("position", ["key", "value"])
def test_capability_schema_snapshots_reject_lone_surrogates(position: str) -> None:
    lone_surrogate = chr(0xD800)
    invalid_schema = (
        {lone_surrogate: "value"}
        if position == "key"
        else {"const": lone_surrogate}
    )
    capability = replace(
        TOOL_REGISTRY["get_tool_catalog"],
        input_schema=invalid_schema,
    )

    with pytest.raises(ValueError, match="valid Unicode scalars"):
        PolicyContext.create(
            capability=capability,
            arguments={},
            active_profile="core",
            profile_enforced=True,
            caller_identity=None,
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="surrogate-capability-schema",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )


def test_capability_metadata_sequences_reject_nested_mutable_values() -> None:
    nested_tags = ["mutable"]
    capability = replace(
        TOOL_REGISTRY["get_tool_catalog"],
        workflow_tags=(nested_tags,),  # type: ignore[arg-type]
    )

    with pytest.raises(TypeError, match="workflow_tags item"):
        PolicyContext.create(
            capability=capability,
            arguments={},
            active_profile="core",
            profile_enforced=True,
            caller_identity=None,
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="mutable-capability-metadata",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )


def test_capability_metadata_uses_shared_node_and_byte_budgets() -> None:
    base = TOOL_REGISTRY["get_tool_catalog"]
    oversized_tag = "x" * (policy_pipeline_module._MAX_SNAPSHOT_LEAF_BYTES + 1)
    too_many_tags = tuple(
        f"tag_{index:04d}"
        for index in range(policy_pipeline_module._MAX_SNAPSHOT_NODES + 1)
    )
    aggregate_tags = tuple(
        "x" * (policy_pipeline_module._MAX_SNAPSHOT_LEAF_BYTES - 1)
        for _ in range(17)
    )

    for capability, expected_error in (
        (replace(base, workflow_tags=(oversized_tag,)), "leaf or shared byte budget"),
        (replace(base, workflow_tags=too_many_tags), "snapshot node budget"),
        (replace(base, workflow_tags=aggregate_tags), "shared byte budget"),
    ):
        with pytest.raises(ValueError, match=expected_error):
            PolicyContext.create(
                capability=capability,
                arguments={},
                active_profile="core",
                profile_enforced=True,
                caller_identity=None,
                session_identity=None,
                project_capability=None,
                trusted_vivado_identity=None,
                request_id="bounded-capability-metadata",
                started_at=datetime(2026, 8, 30, tzinfo=UTC),
            )


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "expected_error"),
    [
        ("name", "   ", "name must be non-empty"),
        ("domain", "unknown_domain", "domain uses an unknown classification"),
        ("risk_class", "unknown_risk", "risk_class uses an unknown classification"),
        (
            "required_session_state",
            "unknown_session_state",
            "required_session_state uses an unknown classification",
        ),
        (
            "required_project_state",
            "unknown_project_state",
            "required_project_state uses an unknown classification",
        ),
        ("duration_class", "unknown_duration", "duration_class uses an unknown classification"),
        ("dispatch_lane", "serialzed_backend", "dispatch_lane uses an unknown classification"),
        (
            "execution_input_policy",
            "unknown_input_policy",
            "execution_input_policy uses an unknown classification",
        ),
        ("hardware_tier", "unknown_hardware", "hardware_tier uses an unknown classification"),
    ],
)
def test_capability_snapshot_rejects_blank_or_unknown_classifications(
    field_name: str,
    invalid_value: str,
    expected_error: str,
) -> None:
    capability = replace(
        TOOL_REGISTRY["get_tool_catalog"],
        **{field_name: invalid_value},
    )

    with pytest.raises(ValueError, match=expected_error):
        PolicyContext.create(
            capability=capability,
            arguments={},
            active_profile="core",
            profile_enforced=True,
            caller_identity=None,
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="invalid-capability-classification",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("tool_name", "overrides", "expected_error"),
    [
        (
            "detect_hardware_environment",
            {"hardware": False},
            "risk boolean invariant",
        ),
        (
            "detect_hardware_environment",
            {"hardware_tier": "not_hardware"},
            "risk hardware tier invariant",
        ),
        (
            "program_hw_device",
            {"destructive": False},
            "risk boolean invariant",
        ),
        (
            "add_project_files",
            {"mutation": False},
            "risk boolean invariant",
        ),
        (
            "run_synthesis",
            {
                "required_session_state": "none",
                "required_project_state": "none",
            },
            "risk state invariant",
        ),
        (
            "open_block_design",
            {"required_session_state": "none"},
            "risk state invariant",
        ),
        (
            "detect_hardware_environment",
            {"required_session_state": "managed_session"},
            "hardware tier invariant",
        ),
        (
            "open_block_design",
            {"dispatch_lane": "local"},
            "composite input invariant",
        ),
        (
            "get_tool_catalog",
            {"required_session_state": "managed_session"},
            "local dispatch invariant",
        ),
        (
            "detect_hardware_environment",
            {"profiles": frozenset({"core", "advanced", "all"})},
            "hardware profile invariant",
        ),
        (
            "get_tool_catalog",
            {"idempotent": False},
            "idempotency invariant",
        ),
        (
            "get_tool_catalog",
            {"profiles": frozenset()},
            "profile invariant",
        ),
    ],
)
def test_policy_stage_requirements_rejects_cross_field_capability_forgery(
    tool_name: str,
    overrides: dict[str, Any],
    expected_error: str,
) -> None:
    forged = replace(TOOL_REGISTRY[tool_name], **overrides)

    with pytest.raises(ValueError, match=expected_error):
        policy_stage_requirements(forged)


def test_policy_stage_requirements_conservatively_preserves_session_identity() -> None:
    understated_project = replace(
        TOOL_REGISTRY["open_project"],
        required_session_state="none",
    )
    renamed_tcl = replace(
        TOOL_REGISTRY["safe_tcl"],
        name="renamed_safe_tcl",
    )

    assert policy_stage_requirements(understated_project)[
        "executable_session_identity"
    ] is True
    assert policy_stage_requirements(renamed_tcl)[
        "executable_session_identity"
    ] is True


@pytest.mark.parametrize(
    "input_schema",
    [
        {
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {"output_dir": {"type": "string"}},
                    "additionalProperties": False,
                }
            },
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"source_files": {"type": "array"}},
                        "additionalProperties": False,
                    },
                }
            },
            "additionalProperties": False,
        },
        {
            "type": "object",
            "anyOf": [
                {"properties": {"detail": {"type": "string"}}},
                {"properties": {"manifest_path": {"type": "string"}}},
            ],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "$defs": {
                "options": {
                    "type": "object",
                    "properties": {"report_dir": {"type": "string"}},
                }
            },
            "properties": {"options": {"$ref": "#/$defs/options"}},
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "options": {
                    "$dynamicRef": "https://example.invalid/schema#node"
                }
            },
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"options": {"$recursiveRef": "#"}},
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"options": {"type": "object"}},
            "additionalProperties": True,
        },
        {
            "type": ["object", "null"],
        },
    ],
)
def test_policy_stage_requirements_find_nested_path_bearing_schema(
    input_schema: dict[str, Any],
) -> None:
    capability = replace(
        TOOL_REGISTRY["get_tool_catalog"],
        input_schema=input_schema,
    )

    assert policy_stage_requirements(capability)["managed_path_boundary"] is True


def test_policy_stage_requirements_leave_bounded_nested_non_path_schema_local() -> None:
    capability = replace(
        TOOL_REGISTRY["get_tool_catalog"],
        input_schema={
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                    "additionalProperties": False,
                }
            },
            "additionalProperties": False,
        },
    )

    assert policy_stage_requirements(capability)["managed_path_boundary"] is False


@pytest.mark.parametrize("tool_name", ["get_project_info", "close_project"])
def test_policy_stage_requirements_conservatively_preserves_project_identity(
    tool_name: str,
) -> None:
    understated_state = replace(
        TOOL_REGISTRY[tool_name],
        required_project_state="none",
    )
    requirements = policy_stage_requirements(understated_state)

    assert requirements["managed_path_boundary"] is True
    assert requirements["project_capability_generation"] is True


def test_policy_stage_requirements_conservatively_preserves_project_state_signal() -> None:
    understated_evidence = replace(
        TOOL_REGISTRY["get_project_info"],
        evidence_contract=("unified_result_v1",),
    )
    requirements = policy_stage_requirements(understated_evidence)

    assert requirements["managed_path_boundary"] is True
    assert requirements["project_capability_generation"] is True


def test_policy_context_rejects_oversized_active_profile() -> None:
    with pytest.raises(ValueError, match="active_profile must not exceed 128"):
        PolicyContext.create(
            capability=TOOL_REGISTRY["get_tool_catalog"],
            arguments={},
            active_profile="x" * 129,
            profile_enforced=True,
            caller_identity=None,
            session_identity=None,
            project_capability=None,
            trusted_vivado_identity=None,
            request_id="bounded-profile",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )


def test_unsupported_argument_type_name_is_constant_bounded() -> None:
    hostile_type = type("X" * 70_000, (), {})
    context = PolicyContext.create(
        capability=TOOL_REGISTRY["get_tool_catalog"],
        arguments={"detail": hostile_type()},
        active_profile="core",
        profile_enforced=True,
        caller_identity=None,
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id="bounded-opaque-type",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    assert context.arguments["detail"].type_name == "unknown"
    assert len(context.policy_identity_sha256) == 64


def test_opaque_argument_projection_does_not_expose_internal_marker() -> None:
    context = PolicyContext.create(
        capability=TOOL_REGISTRY["get_tool_catalog"],
        arguments={"detail": object()},
        active_profile="core",
        profile_enforced=True,
        caller_identity=None,
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id="detached-opaque-projection",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    initial_identity = context.policy_identity_sha256
    projected_marker = context.arguments["detail"]

    object.__setattr__(projected_marker, "type_name", "forged")

    assert context.arguments["detail"].type_name == "object"
    assert context.arguments["detail"] is not projected_marker
    assert context.policy_identity_sha256 == initial_identity


def test_context_identity_serializer_safely_canonicalizes_lone_surrogates() -> None:
    lone_surrogate = chr(0xD800)
    context = PolicyContext.create(
        capability=TOOL_REGISTRY["get_tool_catalog"],
        arguments={"detail": lone_surrogate},
        active_profile="core",
        profile_enforced=True,
        caller_identity={"name": lone_surrogate},
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id="surrogate-context-identity",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    assert len(context.policy_identity_sha256) == 64
    decision = _pipeline([]).evaluate(context)
    assert decision.allowed is True
    assert len(json.dumps(decision.to_record(), ensure_ascii=True).encode("utf-8")) <= (
        POLICY_DECISION_MAX_BYTES
    )


def test_policy_context_canonicalizes_datetime_with_mutable_timezone() -> None:
    class MutableTimezone(tzinfo):
        def __init__(self) -> None:
            self.offset = timedelta(hours=1)

        def utcoffset(self, value: datetime | None) -> timedelta:
            return self.offset

        def dst(self, value: datetime | None) -> timedelta:
            return timedelta(0)

        def tzname(self, value: datetime | None) -> str:
            return "MUTABLE"

    mutable_timezone = MutableTimezone()
    observed_at = datetime(2026, 8, 30, 12, 0, tzinfo=mutable_timezone)
    context = PolicyContext.create(
        capability=TOOL_REGISTRY["get_tool_catalog"],
        arguments={},
        active_profile="core",
        profile_enforced=True,
        caller_identity={"observed_at": observed_at},
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id="mutable-timezone",
        started_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    initial_digest = context.policy_identity_sha256

    mutable_timezone.offset = timedelta(hours=9)

    assert context.policy_identity_sha256 == initial_digest
    assert context.caller_identity["observed_at"].tzinfo is UTC
    assert context.caller_identity["observed_at"].utcoffset() == timedelta(0)


def test_evidence_datetime_records_do_not_depend_on_host_local_timezone() -> None:
    class NoneOffsetTimezone(tzinfo):
        def utcoffset(self, value: datetime | None) -> None:
            return None

        def dst(self, value: datetime | None) -> None:
            return None

        def tzname(self, value: datetime | None) -> str:
            return "NONE"

    naive_values = (
        datetime(2026, 8, 30, 12, 0),
        datetime.min,
        datetime.max,
        datetime(2026, 8, 30, 12, 0, tzinfo=NoneOffsetTimezone()),
    )
    for observed_at in naive_values:
        stage = PolicyStageResult(
            stage="argument_schema",
            allowed=True,
            reason_code="POLICY_STAGE_ALLOWED",
            message="allowed",
            evidence={"required": observed_at},
        )
        assert stage.to_record()["evidence"]["required"] == observed_at.replace(
            tzinfo=None
        ).isoformat()

    aware = datetime(
        2026,
        8,
        30,
        12,
        0,
        tzinfo=timezone(timedelta(hours=8)),
    )
    decision = PolicyDecision(
        capability_name="get_tool_catalog",
        allowed=True,
        stage="policy_pipeline",
        reason_code="POLICY_PIPELINE_ALLOWED",
        message="allowed",
        evidence={"required": aware},
        stop_required=False,
    )
    assert decision.to_record()["evidence"]["required"] == (
        "2026-08-30T04:00:00+00:00"
    )

    class MutableTimezone(tzinfo):
        def __init__(self) -> None:
            self.offset = timedelta(hours=1)

        def utcoffset(self, value: datetime | None) -> timedelta:
            return self.offset

        def dst(self, value: datetime | None) -> timedelta:
            return timedelta(0)

    mutable_timezone = MutableTimezone()
    mutable_evidence = PolicyStageResult(
        stage="argument_schema",
        allowed=True,
        reason_code="POLICY_STAGE_ALLOWED",
        message="allowed",
        evidence={
            "required": datetime(
                2026,
                8,
                30,
                12,
                0,
                tzinfo=mutable_timezone,
            )
        },
    )
    initial_record = mutable_evidence.to_record()
    mutable_timezone.offset = timedelta(hours=9)

    assert mutable_evidence.to_record() == initial_record
    assert initial_record["evidence"]["required"] == "2026-08-30T11:00:00+00:00"


def test_post_execution_handoff_rejects_policy_context_subclasses() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    pre_decision = pre_pipeline.evaluate(pre_context)
    allowed_digest = pre_context.policy_identity_sha256

    class ForgedPolicyContext(PolicyContext):
        @property
        def policy_identity_sha256(self) -> str:
            return allowed_digest

    forged_context = ForgedPolicyContext.create(
        capability=TOOL_REGISTRY["collect_diagnostic_bundle"],
        arguments={"bundle_dir": "forged"},
        active_profile="core",
        profile_enforced=True,
        caller_identity={"identity": "forged"},
        session_identity=None,
        project_capability=None,
        trusted_vivado_identity=None,
        request_id=pre_context.request_id,
        started_at=pre_context.started_at,
    )

    with pytest.raises(TypeError, match="requires a PolicyContext"):
        PostExecutionPolicyContext.create(
            pre_execution_context=forged_context,
            pre_execution_decision=pre_decision,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )

    mismatch = _pipeline([]).evaluate(forged_context)
    assert mismatch.reason_code == "POLICY_CONTEXT_PHASE_MISMATCH"
    assert mismatch.capability_name == "invalid_context"


def test_post_execution_validates_the_frozen_execution_result_mapping() -> None:
    class SplitExecutionResult(Mapping[str, Any]):
        def __getitem__(self, key: str) -> Any:
            if key == "status":
                return "PASS"
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            yield "status"

        def __len__(self) -> int:
            return 1

        def get(self, key: str, default: Any = None) -> Any:
            return True if key == "ok" else super().get(key, default)

    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    pre_decision = pre_pipeline.evaluate(pre_context)

    with pytest.raises(ValueError, match="boolean ok field"):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=pre_decision,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result=SplitExecutionResult(),
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )


def test_post_execution_context_is_immutable_and_binds_pre_decision_and_evidence() -> None:
    context = _post_context()
    result = context.execution_result
    evidence = context.evidence_snapshot

    result["data"]["status"] = "BLOCK"
    evidence["manifest_sha256"] = "changed"

    assert context.execution_result["data"]["status"] == "WARN"
    assert context.evidence_snapshot["manifest_sha256"] == "a" * 64
    assert context.pre_execution_decision.allowed is True
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    with pytest.raises(ValueError, match="allowed pre-execution"):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=PolicyDecision(
                capability_name="collect_diagnostic_bundle",
                allowed=False,
                stage="argument_schema",
                reason_code="INVALID_TOOL_ARGUMENTS",
                message="blocked",
                evidence={},
                stop_required=True,
                request_id="policy-post-request",
                started_at=datetime(2026, 8, 30, tzinfo=UTC),
            ),
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={"ok": False},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )


def test_direct_post_execution_construction_cannot_bypass_binding_or_snapshot() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    pre_decision = pre_pipeline.evaluate(pre_context)
    execution_result = {"ok": True, "data": {"status": "WARN"}}
    evidence_snapshot = {"manifest": {"sha256": "a" * 64}}
    context = PostExecutionPolicyContext(
        pre_execution_context=pre_context,
        pre_execution_decision=pre_decision,
        _trusted_pre_execution_pipeline=pre_pipeline,
        _execution_result_snapshot=execution_result,
        _evidence_snapshot=evidence_snapshot,
        completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
    )

    execution_result["data"]["status"] = "BLOCK"
    evidence_snapshot["manifest"]["sha256"] = "changed"

    assert context.execution_result["data"]["status"] == "WARN"
    assert context.evidence_snapshot["manifest"]["sha256"] == "a" * 64
    with pytest.raises(ValueError, match="policy identity"):
        replace(
            context,
            pre_execution_context=replace(
                pre_context,
                capability=replace(pre_context.capability, artifact_contract=("none",)),
            ),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda decision: replace(decision, stage="argument_schema"),
        lambda decision: replace(decision, reason_code="NONCANONICAL_ALLOW"),
        lambda decision: replace(decision, mode="shadow"),
        lambda decision: replace(decision, request_id=""),
        lambda decision: replace(decision, started_at=None),
        lambda decision: replace(decision, stage_results=()),
        lambda decision: replace(decision, stage_results=decision.stage_results[::-1]),
    ],
)
def test_post_execution_context_rejects_forged_pre_execution_allow(mutate) -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    valid = pre_pipeline.evaluate(pre_context)
    forged = mutate(valid)

    with pytest.raises(ValueError):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=forged,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )


def test_post_execution_rejects_structurally_identical_manual_allow() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    issued = pre_pipeline.evaluate(pre_context)
    forged = _reconstruct_decision(issued)

    assert forged == issued
    assert forged is not issued
    with pytest.raises(ValueError, match="pipeline-issued"):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=forged,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )

    accepted = PostExecutionPolicyContext.create(
        pre_execution_context=pre_context,
        pre_execution_decision=issued,
        trusted_pre_execution_pipeline=pre_pipeline,
        execution_result={"ok": True},
        evidence_snapshot={},
        completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
    )
    assert accepted.pre_execution_decision == issued
    assert accepted.pre_execution_decision is not issued


def test_post_execution_detaches_consumed_authorization_inputs() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    issued = pre_pipeline.evaluate(pre_context)
    accepted = PostExecutionPolicyContext.create(
        pre_execution_context=pre_context,
        pre_execution_decision=issued,
        trusted_pre_execution_pipeline=pre_pipeline,
        execution_result={"ok": True},
        evidence_snapshot={},
        completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
    )
    accepted_identity = accepted.policy_identity_sha256

    object.__setattr__(pre_context, "active_profile", "forged")
    object.__setattr__(issued, "reason_code", "FORGED_ALLOW")
    object.__setattr__(issued.stage_results[0], "evidence", {"forged": True})

    assert accepted.pre_execution_context is not pre_context
    assert accepted.pre_execution_decision is not issued
    assert accepted.pre_execution_context.active_profile == "core"
    assert accepted.pre_execution_decision.reason_code == "POLICY_PIPELINE_ALLOWED"
    assert accepted.pre_execution_decision.stage_results[0].evidence == {}
    assert accepted.policy_identity_sha256 == accepted_identity


def test_issuer_rejects_authorization_outside_actual_pipeline_evaluation() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    issued = pre_pipeline.evaluate(pre_context)
    forged = _reconstruct_decision(issued)
    issuer = object.__getattribute__(pre_pipeline, "_issuer")

    assert not hasattr(issuer, "issue")
    forged_grant = issuer._issue_from_completed_evaluation(
        forged,
        pre_context,
        pipeline=pre_pipeline,
        evaluated_context=pre_context,
    )
    assert forged_grant is None
    object.__setattr__(forged, "_provenance", forged_grant)

    with pytest.raises(ValueError, match="pipeline-issued"):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=forged,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )

    PostExecutionPolicyContext.create(
        pre_execution_context=pre_context,
        pre_execution_decision=issued,
        trusted_pre_execution_pipeline=pre_pipeline,
        execution_result={"ok": True},
        evidence_snapshot={},
        completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
    )


def test_public_pipeline_cannot_issue_post_execution_authorization() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    calls: list[str] = []
    public_pipeline = PolicyPipeline(
        [_RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER]
    )
    decision = public_pipeline.evaluate(pre_context)

    assert decision.allowed is True
    assert calls == list(PRE_EXECUTION_POLICY_STAGE_ORDER)
    assert object.__getattribute__(decision, "_provenance") is None
    with pytest.raises(ValueError, match="package-trusted"):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=decision,
            trusted_pre_execution_pipeline=public_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )

    trusted_pipeline = _pipeline([])
    with pytest.raises(ValueError, match="pipeline-issued"):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=decision,
            trusted_pre_execution_pipeline=trusted_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )

    object.__setattr__(
        public_pipeline,
        "_issuer",
        object.__getattribute__(trusted_pipeline, "_issuer"),
    )
    transplanted = public_pipeline.evaluate(pre_context)
    assert transplanted.allowed is False
    assert transplanted.reason_code == POLICY_PIPELINE_CONFIGURATION_INVALID


def test_post_execution_allow_is_bound_to_expected_trusted_pipeline() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    issuing_pipeline = _pipeline([])
    other_trusted_pipeline = _pipeline([])
    issued = issuing_pipeline.evaluate(pre_context)

    with pytest.raises(ValueError, match="pipeline-issued"):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=issued,
            trusted_pre_execution_pipeline=other_trusted_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )

    accepted = PostExecutionPolicyContext.create(
        pre_execution_context=pre_context,
        pre_execution_decision=issued,
        trusted_pre_execution_pipeline=issuing_pipeline,
        execution_result={"ok": True},
        evidence_snapshot={},
        completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
    )
    assert accepted.pre_execution_decision == issued
    assert accepted.pre_execution_decision is not issued


def test_post_execution_rejects_copied_or_replaced_allow_without_consuming_original() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    issued = pre_pipeline.evaluate(pre_context)

    copied = copy.copy(issued)
    replaced = replace(issued)
    stolen_grant = _reconstruct_decision(issued)
    object.__setattr__(
        stolen_grant,
        "_provenance",
        object.__getattribute__(issued, "_provenance"),
    )
    for forged in (copied, replaced, stolen_grant):
        with pytest.raises(ValueError, match="pipeline-issued"):
            PostExecutionPolicyContext.create(
                pre_execution_context=pre_context,
                pre_execution_decision=forged,
                trusted_pre_execution_pipeline=pre_pipeline,
                execution_result={"ok": True},
                evidence_snapshot={},
                completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
            )

    PostExecutionPolicyContext.create(
        pre_execution_context=pre_context,
        pre_execution_decision=issued,
        trusted_pre_execution_pipeline=pre_pipeline,
        execution_result={"ok": True},
        evidence_snapshot={},
        completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
    )


def test_post_execution_allow_is_bound_to_exact_context_and_consumed_once() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    issued = pre_pipeline.evaluate(pre_context)
    equivalent_context = replace(pre_context)

    assert equivalent_context == pre_context
    assert equivalent_context is not pre_context
    with pytest.raises(ValueError, match="pipeline-issued"):
        PostExecutionPolicyContext.create(
            pre_execution_context=equivalent_context,
            pre_execution_decision=issued,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )

    values = {
        "pre_execution_context": pre_context,
        "pre_execution_decision": issued,
        "trusted_pre_execution_pipeline": pre_pipeline,
        "execution_result": {"ok": True},
        "evidence_snapshot": {},
        "completed_at": datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
    }
    PostExecutionPolicyContext.create(**values)
    with pytest.raises(ValueError, match="pipeline-issued"):
        PostExecutionPolicyContext.create(**values)


def test_invalid_post_snapshot_does_not_consume_pipeline_allow() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    issued = pre_pipeline.evaluate(pre_context)

    with pytest.raises(ValueError, match="boolean ok field"):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=issued,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )

    PostExecutionPolicyContext.create(
        pre_execution_context=pre_context,
        pre_execution_decision=issued,
        trusted_pre_execution_pipeline=pre_pipeline,
        execution_result={"ok": True},
        evidence_snapshot={},
        completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
    )


def test_pipeline_allow_provenance_detects_in_place_authorization_mutation() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    issued = pre_pipeline.evaluate(pre_context)
    object.__setattr__(issued.stage_results[0], "reason_code", "FORGED_STAGE_ALLOW")

    with pytest.raises(ValueError, match="pipeline-issued"):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=issued,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )


def test_pipeline_allow_provenance_binds_stage_evidence() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    issued = pre_pipeline.evaluate(pre_context)
    object.__setattr__(issued.stage_results[0], "evidence", {"forged": True})

    with pytest.raises(ValueError, match="pipeline-issued"):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=issued,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )


def test_pipeline_allow_provenance_is_consumed_atomically() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    issued = pre_pipeline.evaluate(pre_context)

    def handoff() -> str:
        try:
            PostExecutionPolicyContext.create(
                pre_execution_context=pre_context,
                pre_execution_decision=issued,
                trusted_pre_execution_pipeline=pre_pipeline,
                execution_result={"ok": True},
                evidence_snapshot={},
                completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
            )
        except ValueError as exc:
            assert "pipeline-issued" in str(exc)
            return "rejected"
        return "accepted"

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = tuple(executor.map(lambda _: handoff(), range(8)))

    assert outcomes.count("accepted") == 1
    assert outcomes.count("rejected") == 7


def test_authorization_state_is_not_serializable_or_exposed_in_records() -> None:
    pipeline = _pipeline([])
    decision = pipeline.evaluate(_context())

    assert "provenance" not in decision.to_record()
    with pytest.raises(TypeError, match="deep-copied"):
        copy.deepcopy(decision)
    with pytest.raises(TypeError, match="serialized"):
        pickle.dumps(decision)
    with pytest.raises(TypeError, match="copied"):
        copy.copy(pipeline)
    with pytest.raises(TypeError, match="deep-copied"):
        copy.deepcopy(pipeline)
    with pytest.raises(TypeError, match="serialized"):
        pickle.dumps(pipeline)


def test_post_execution_context_rejects_non_applicable_required_stage() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    valid = pre_pipeline.evaluate(pre_context)
    forged_results = tuple(
        replace(result, applicable=False)
        if result.stage == "argument_schema"
        else result
        for result in valid.stage_results
    )
    forged = replace(valid, stage_results=forged_results)

    with pytest.raises(ValueError, match="required pre-execution stage"):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=forged,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )


def test_post_execution_context_allows_non_applicable_optional_stage() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    requirements = policy_stage_requirements(pre_context.capability)
    optional_stage = next(
        stage for stage in PRE_EXECUTION_POLICY_STAGE_ORDER if not requirements[stage]
    )
    adjusted = _pipeline(
        [],
        non_applicable_stage=optional_stage,
    )
    decision = adjusted.evaluate(pre_context)

    context = PostExecutionPolicyContext.create(
        pre_execution_context=pre_context,
        pre_execution_decision=decision,
        trusted_pre_execution_pipeline=adjusted,
        execution_result={"ok": True},
        evidence_snapshot={},
        completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
    )

    assert context.pre_execution_decision.stage_results[
        PRE_EXECUTION_POLICY_STAGE_ORDER.index(optional_stage)
    ].applicable is False


def test_post_execution_context_rejects_same_name_with_changed_policy_identity() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    valid = pre_pipeline.evaluate(pre_context)
    altered_context = replace(
        pre_context,
        capability=replace(
            pre_context.capability,
            evidence_contract=("unified_result_v1",),
            artifact_contract=("none",),
        ),
    )

    assert (
        altered_context.capability.policy_identity_sha256
        != pre_context.capability.policy_identity_sha256
    )
    with pytest.raises(ValueError, match="policy identity"):
        PostExecutionPolicyContext.create(
            pre_execution_context=altered_context,
            pre_execution_decision=valid,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "mutate_context",
    [
        lambda context: replace(context, _arguments_snapshot={"output_dir": "changed"}),
        lambda context: replace(context, active_profile="all"),
        lambda context: replace(context, session_identity={"generation": 99}),
        lambda context: replace(context, project_capability={"project": "other"}),
    ],
)
def test_post_execution_context_rejects_different_full_pre_context_snapshot(
    mutate_context,
) -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    valid = pre_pipeline.evaluate(pre_context)
    altered_context = mutate_context(pre_context)

    assert altered_context.capability_name == pre_context.capability_name
    assert altered_context.request_id == pre_context.request_id
    assert altered_context.started_at == pre_context.started_at
    assert altered_context.policy_identity_sha256 != pre_context.policy_identity_sha256
    with pytest.raises(ValueError, match="context identity"):
        PostExecutionPolicyContext.create(
            pre_execution_context=altered_context,
            pre_execution_decision=valid,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("execution_result", "evidence_snapshot", "error_type"),
    [
        ({}, {}, ValueError),
        ({"ok": "true"}, {}, ValueError),
        ([], {}, TypeError),
        ({"ok": True}, [], TypeError),
    ],
)
def test_post_execution_context_requires_structured_execution_and_evidence(
    execution_result: Any,
    evidence_snapshot: Any,
    error_type: type[Exception],
) -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    valid = pre_pipeline.evaluate(pre_context)

    with pytest.raises(error_type):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=valid,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result=execution_result,
            evidence_snapshot=evidence_snapshot,
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )


def test_policy_context_rejects_missing_request_id_and_naive_timestamp() -> None:
    values = {
        "capability": TOOL_REGISTRY["get_tool_catalog"],
        "arguments": {},
        "active_profile": "core",
        "profile_enforced": True,
        "caller_identity": None,
        "session_identity": None,
        "project_capability": None,
        "trusted_vivado_identity": None,
    }
    with pytest.raises(ValueError, match="request_id"):
        PolicyContext.create(
            **values,
            request_id=" ",
            started_at=datetime(2026, 8, 30, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        PolicyContext.create(
            **values,
            request_id="request",
            started_at=datetime(2026, 8, 30),
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PolicyStageResult(
            stage="argument_schema",
            allowed=False,
            reason_code="BLOCKED",
            message="blocked",
            evidence={},
            stop_required=False,
        ),
        lambda: PolicyDecision(
            capability_name="get_tool_catalog",
            allowed=True,
            stage="policy_pipeline",
            reason_code="ALLOWED",
            message="allowed",
            evidence={},
            stop_required=True,
        ),
    ],
)
def test_policy_outcomes_reject_inconsistent_stop_semantics(factory) -> None:
    with pytest.raises(ValueError, match="inconsistent"):
        factory()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PolicyStageResult(
            stage="argument_schema",
            allowed="false",  # type: ignore[arg-type]
            reason_code="NON_BOOLEAN_ALLOWED",
            message="blocked",
            evidence={},
            stop_required=False,
        ),
        lambda: PolicyStageResult(
            stage="argument_schema",
            allowed=True,
            reason_code="NON_BOOLEAN_STOP",
            message="allowed",
            evidence={},
            stop_required=0,  # type: ignore[arg-type]
        ),
        lambda: PolicyStageResult(
            stage="argument_schema",
            allowed=True,
            reason_code="NON_BOOLEAN_APPLICABLE",
            message="allowed",
            evidence={},
            applicable=1,  # type: ignore[arg-type]
        ),
        lambda: PolicyDecision(
            capability_name="get_tool_catalog",
            allowed="false",  # type: ignore[arg-type]
            stage="policy_pipeline",
            reason_code="NON_BOOLEAN_DECISION",
            message="blocked",
            evidence={},
            stop_required=False,
        ),
    ],
)
def test_policy_outcomes_require_exact_boolean_fields(factory) -> None:
    with pytest.raises(TypeError, match="boolean"):
        factory()


def test_pipeline_runs_every_stage_in_fixed_order() -> None:
    calls: list[str] = []
    decision = _pipeline(calls).evaluate(_context())

    assert decision.allowed is True
    assert decision.reason_code == "POLICY_PIPELINE_ALLOWED"
    assert calls == list(PRE_EXECUTION_POLICY_STAGE_ORDER)
    assert tuple(result.stage for result in decision.stage_results) == PRE_EXECUTION_POLICY_STAGE_ORDER
    assert decision.phase == "pre_execution"


def test_pipeline_revalidates_pre_execution_context_before_stages() -> None:
    context = _context()
    object.__setattr__(
        context,
        "caller_identity",
        {"invalid": bytearray(b"mutable")},
    )
    calls: list[str] = []

    decision = _pipeline(calls).evaluate(context)

    assert decision.allowed is False
    assert decision.reason_code == "POLICY_CONTEXT_REVALIDATION_FAILED"
    assert calls == []


def test_pipeline_stages_receive_detached_revalidated_context() -> None:
    supplied_context = _context()
    supplied_arguments = {"detail": ["compact"]}
    object.__setattr__(supplied_context, "_arguments_snapshot", supplied_arguments)
    observed_contexts: list[PolicyContext] = []
    calls: list[str] = []

    class CapturingStage(_RecordingStage):
        def evaluate(self, context: PolicyContext) -> PolicyStageResult:
            observed_contexts.append(context)
            return super().evaluate(context)

    stages = [
        CapturingStage(PRE_EXECUTION_POLICY_STAGE_ORDER[0], calls),
        *(
            _RecordingStage(name, calls)
            for name in PRE_EXECUTION_POLICY_STAGE_ORDER[1:]
        ),
    ]
    decision = PolicyPipeline(stages).evaluate(supplied_context)
    supplied_arguments["detail"].append("forged")

    assert decision.allowed is True
    assert observed_contexts[0] is not supplied_context
    assert observed_contexts[0].arguments == {"detail": ["compact"]}


def test_post_execution_pipeline_is_separate_and_fixed() -> None:
    calls: list[str] = []
    decision = _pipeline(
        calls,
        stage_names=POST_EXECUTION_POLICY_STAGE_ORDER,
        phase="post_execution",
    ).evaluate(_post_context())

    assert decision.allowed is True
    assert calls == list(POST_EXECUTION_POLICY_STAGE_ORDER)
    assert tuple(result.stage for result in decision.stage_results) == POST_EXECUTION_POLICY_STAGE_ORDER
    assert decision.phase == "post_execution"


def test_pipeline_rejects_mutated_post_execution_context_before_stages() -> None:
    context = _post_context()
    object.__setattr__(context, "_evidence_snapshot", {"forged": True})
    calls: list[str] = []
    pipeline = _pipeline(
        calls,
        stage_names=POST_EXECUTION_POLICY_STAGE_ORDER,
        phase="post_execution",
    )

    decision = pipeline.evaluate(context)

    assert decision.allowed is False
    assert decision.reason_code == "POLICY_CONTEXT_REVALIDATION_FAILED"
    assert calls == []


def test_pre_and_post_execution_contexts_cannot_cross_pipeline_phases() -> None:
    pre_calls: list[str] = []
    post_calls: list[str] = []
    pre_pipeline = _pipeline(pre_calls)
    post_pipeline = _pipeline(
        post_calls,
        stage_names=POST_EXECUTION_POLICY_STAGE_ORDER,
        phase="post_execution",
    )

    pre_mismatch = pre_pipeline.evaluate(_post_context())
    post_mismatch = post_pipeline.evaluate(_context("collect_diagnostic_bundle"))

    assert pre_mismatch.reason_code == "POLICY_CONTEXT_PHASE_MISMATCH"
    assert post_mismatch.reason_code == "POLICY_CONTEXT_PHASE_MISMATCH"
    assert pre_calls == []
    assert post_calls == []


def test_pipeline_short_circuits_after_first_block() -> None:
    calls: list[str] = []
    decision = _pipeline(calls, blocking_stage="managed_path_boundary").evaluate(_context())

    assert decision.allowed is False
    assert decision.stage == "managed_path_boundary"
    assert decision.reason_code == "TEST_BLOCK"
    assert decision.stop_required is True
    assert calls == list(PRE_EXECUTION_POLICY_STAGE_ORDER[:4])


@pytest.mark.parametrize(
    "stage_names",
    [
        PRE_EXECUTION_POLICY_STAGE_ORDER[:-1],
        (*PRE_EXECUTION_POLICY_STAGE_ORDER, "unknown_stage"),
        (
            PRE_EXECUTION_POLICY_STAGE_ORDER[1],
            PRE_EXECUTION_POLICY_STAGE_ORDER[0],
            *PRE_EXECUTION_POLICY_STAGE_ORDER[2:],
        ),
        (
            PRE_EXECUTION_POLICY_STAGE_ORDER[0],
            PRE_EXECUTION_POLICY_STAGE_ORDER[0],
            *PRE_EXECUTION_POLICY_STAGE_ORDER[2:],
        ),
    ],
)
def test_missing_unknown_reordered_or_duplicate_stage_fails_closed(stage_names: tuple[str, ...]) -> None:
    calls: list[str] = []
    decision = _pipeline(calls, stage_names=stage_names).evaluate(_context())

    assert decision.allowed is False
    assert decision.stage == "policy_pipeline"
    assert decision.reason_code == "POLICY_PIPELINE_CONFIGURATION_INVALID"
    assert decision.stop_required is True
    assert calls == []


def test_pipeline_configuration_errors_do_not_echo_unknown_stage_or_phase_names() -> None:
    secret_stage = "TOKEN-ABC123"
    calls: list[str] = []
    stages = [_RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER]
    stages[0].name = secret_stage
    stage_decision = PolicyPipeline(stages).evaluate(_context())
    phase_decision = PolicyPipeline(
        [_RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER],
        phase=secret_stage,
    ).evaluate(_context())

    stage_record = json.dumps(stage_decision.to_record(), sort_keys=True)
    phase_record = json.dumps(phase_decision.to_record(), sort_keys=True)
    assert secret_stage not in stage_record
    assert secret_stage not in phase_record
    assert "unknown_count=1" in stage_record
    assert "unknown_phase" in phase_record
    assert calls == []


def test_invalid_context_does_not_echo_unknown_pipeline_phase() -> None:
    secret_phase = "TOKEN-ABC123"
    pipeline = PolicyPipeline([], phase=secret_phase)

    record = pipeline.evaluate(object()).to_record()  # type: ignore[arg-type]
    serialized = json.dumps(record, sort_keys=True)

    assert record["reason_code"] == POLICY_PIPELINE_CONFIGURATION_INVALID
    assert secret_phase not in serialized
    assert "unknown_phase" in serialized


@pytest.mark.parametrize(
    ("field_name", "mutated_value"),
    [
        ("_stages", ()),
        ("_stages", []),
        ("_stage_order", ()),
        ("_registered_stage_names", ()),
        ("_configuration_errors", ("forged",)),
    ],
)
def test_pipeline_revalidates_mutated_structure_before_iteration(
    field_name: str,
    mutated_value: Any,
) -> None:
    calls: list[str] = []
    pipeline = PolicyPipeline(
        [_RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER]
    )
    object.__setattr__(pipeline, field_name, mutated_value)

    decision = pipeline.evaluate(_context())

    assert decision.allowed is False
    assert decision.reason_code == POLICY_PIPELINE_CONFIGURATION_INVALID
    assert decision.stop_required is True
    assert calls == []


def test_non_string_stage_name_is_structured_configuration_failure() -> None:
    calls: list[str] = []
    stages = [_RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER]
    stages[0].name = None  # type: ignore[assignment]

    pipeline = PolicyPipeline(stages)
    decision = pipeline.evaluate(_context())

    assert decision.reason_code == POLICY_PIPELINE_CONFIGURATION_INVALID
    assert decision.stop_required is True
    assert pipeline.stage_names[0] == "<invalid-stage-name-00>"
    assert calls == []


def test_raising_stage_name_is_structured_configuration_failure() -> None:
    class RaisingNameStage:
        @property
        def name(self) -> str:
            raise RuntimeError("untrusted stage name")

        def evaluate(self, context: PolicyContext) -> PolicyStageResult:
            raise AssertionError("invalid pipeline configuration must not evaluate stages")

    calls: list[str] = []
    stages: list[Any] = [
        _RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER
    ]
    stages[0] = RaisingNameStage()

    pipeline = PolicyPipeline(stages)
    decision = pipeline.evaluate(_context())

    assert decision.reason_code == POLICY_PIPELINE_CONFIGURATION_INVALID
    assert decision.stop_required is True
    assert pipeline.stage_names[0] == "<invalid-stage-name-00>"
    assert "untrusted stage name" not in json.dumps(decision.to_record())
    assert calls == []


def test_non_string_pipeline_phase_is_structured_configuration_failure() -> None:
    calls: list[str] = []
    stages = [_RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER]

    pipeline = PolicyPipeline(stages, phase=None)  # type: ignore[arg-type]
    decision = pipeline.evaluate(_context())

    assert decision.reason_code == POLICY_PIPELINE_CONFIGURATION_INVALID
    assert decision.stop_required is True
    assert calls == []


def test_hostile_type_names_cannot_escape_pipeline_configuration_failure() -> None:
    class HostileMeta(type):
        def __getattribute__(cls, name: str) -> Any:
            if name == "__name__":
                raise RuntimeError("hostile type name")
            return super().__getattribute__(name)

    class HostileValue(metaclass=HostileMeta):
        pass

    class HostileErrorMeta(type):
        def __getattribute__(cls, name: str) -> Any:
            if name == "__name__":
                raise RuntimeError("hostile exception type name")
            return super().__getattribute__(name)

    class HostileNameError(RuntimeError, metaclass=HostileErrorMeta):
        pass

    class RaisingNameStage:
        @property
        def name(self) -> str:
            raise HostileNameError("untrusted")

        def evaluate(self, context: PolicyContext) -> PolicyStageResult:
            raise AssertionError("invalid pipeline configuration must not evaluate stages")

    for invalid_stage, invalid_phase in (
        (HostileValue(), "pre_execution"),
        (RaisingNameStage(), "pre_execution"),
        (
            _RecordingStage(PRE_EXECUTION_POLICY_STAGE_ORDER[0], []),
            HostileValue(),
        ),
    ):
        calls: list[str] = []
        stages: list[Any] = [
            _RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER
        ]
        stages[0] = invalid_stage
        pipeline = PolicyPipeline(stages, phase=invalid_phase)  # type: ignore[arg-type]
        decision = pipeline.evaluate(_context())

        assert decision.reason_code == POLICY_PIPELINE_CONFIGURATION_INVALID
        assert decision.stop_required is True
        assert calls == []


def test_policy_stage_result_rejects_oversized_stage_name() -> None:
    with pytest.raises(ValueError, match="stage name must not exceed 128"):
        PolicyStageResult(
            stage="x" * 129,
            allowed=True,
            reason_code="POLICY_STAGE_ALLOWED",
            message="allowed",
            evidence={},
        )


def test_policy_messages_are_bounded_before_authorization_fingerprinting() -> None:
    limit = policy_pipeline_module._MAX_POLICY_MESSAGE_BYTES
    accepted = PolicyStageResult(
        stage="argument_schema",
        allowed=True,
        reason_code="POLICY_STAGE_ALLOWED",
        message="x" * limit,
        evidence={},
    )
    assert len(accepted.message.encode("utf-8")) == limit

    with pytest.raises(ValueError, match="UTF-8 bytes"):
        PolicyStageResult(
            stage="argument_schema",
            allowed=True,
            reason_code="POLICY_STAGE_ALLOWED",
            message="界" * ((limit // 3) + 1),
            evidence={},
        )
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        PolicyDecision(
            capability_name="get_tool_catalog",
            allowed=True,
            stage="policy_pipeline",
            reason_code="POLICY_PIPELINE_ALLOWED",
            message="x" * (limit + 1),
            evidence={},
            stop_required=False,
        )


def test_mutated_oversized_stage_message_fails_before_issuer_fingerprint() -> None:
    calls: list[str] = []
    forged = PolicyStageResult(
        stage=PRE_EXECUTION_POLICY_STAGE_ORDER[0],
        allowed=True,
        reason_code="POLICY_STAGE_ALLOWED",
        message="allowed",
        evidence={},
    )
    object.__setattr__(
        forged,
        "message",
        "x" * (policy_pipeline_module._MAX_POLICY_MESSAGE_BYTES + 1),
    )

    def forged_result(context: PolicyContext) -> PolicyStageResult:
        calls.append(f"forged-message:{context.capability_name}")
        return forged

    trusted_stages = [_RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER]
    trusted_stages[0].evaluate = forged_result  # type: ignore[method-assign]
    decision = PolicyPipeline(trusted_stages).evaluate(_context())

    assert decision.allowed is False
    assert decision.reason_code == "POLICY_STAGE_CONTRACT_VIOLATION"
    assert object.__getattribute__(decision, "_provenance") is None


def test_issuer_fingerprint_bounds_post_issue_message_mutation() -> None:
    pre_context = _context("collect_diagnostic_bundle")
    pre_pipeline = _pipeline([])
    issued = pre_pipeline.evaluate(pre_context)
    object.__setattr__(
        issued.stage_results[0],
        "message",
        "x" * (policy_pipeline_module._MAX_POLICY_MESSAGE_BYTES + 1),
    )

    with pytest.raises(ValueError, match="pipeline-issued"):
        PostExecutionPolicyContext.create(
            pre_execution_context=pre_context,
            pre_execution_decision=issued,
            trusted_pre_execution_pipeline=pre_pipeline,
            execution_result={"ok": True},
            evidence_snapshot={},
            completed_at=datetime(2026, 8, 30, 0, 0, 1, tzinfo=UTC),
        )


def test_stage_exception_is_structured_fail_closed_and_does_not_continue() -> None:
    calls: list[str] = []
    decision = _pipeline(calls, error_stage="project_capability_generation").evaluate(_context())

    assert decision.allowed is False
    assert decision.stage == "project_capability_generation"
    assert decision.reason_code == "POLICY_STAGE_EVALUATION_FAILED"
    assert decision.stop_required is True
    assert decision.evidence["exception_type"] == "RuntimeError"
    assert "boom" not in json.dumps(decision.to_record())
    assert calls == list(PRE_EXECUTION_POLICY_STAGE_ORDER[:5])


def test_stage_result_for_wrong_stage_is_a_contract_violation() -> None:
    calls: list[str] = []
    stages = [
        _RecordingStage(
            "wrong_stage" if name == "managed_path_boundary" else name,
            calls,
        )
        for name in PRE_EXECUTION_POLICY_STAGE_ORDER
    ]
    stages[3].name = "managed_path_boundary"
    stages[3]._allowed = True
    original_evaluate = stages[3].evaluate

    def wrong_result(context: PolicyContext) -> PolicyStageResult:
        original_evaluate(context)
        return PolicyStageResult(
            stage="argument_schema",
            allowed=True,
            reason_code="WRONG_STAGE",
            message="wrong",
            evidence={},
        )

    stages[3].evaluate = wrong_result  # type: ignore[method-assign]
    decision = PolicyPipeline(stages).evaluate(_context())

    assert decision.allowed is False
    assert decision.stage == "managed_path_boundary"
    assert decision.reason_code == "POLICY_STAGE_CONTRACT_VIOLATION"
    assert calls == list(POLICY_STAGE_ORDER[:4])


def test_wrong_stage_contract_violation_does_not_record_returned_stage_text() -> None:
    secret_stage = "TOKEN-ABC123"
    calls: list[str] = []
    stages = [_RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER]

    def wrong_result(context: PolicyContext) -> PolicyStageResult:
        calls.append(f"wrong:{context.capability_name}")
        return PolicyStageResult(
            stage=secret_stage,
            allowed=True,
            reason_code="WRONG_STAGE",
            message="wrong",
            evidence={},
        )

    stages[0].evaluate = wrong_result  # type: ignore[method-assign]
    decision = PolicyPipeline(stages).evaluate(_context())
    serialized = json.dumps(decision.to_record(), sort_keys=True)

    assert decision.reason_code == "POLICY_STAGE_CONTRACT_VIOLATION"
    assert secret_stage not in serialized
    assert "returned_stage" not in serialized


def test_malformed_stage_result_with_raising_attribute_fails_closed() -> None:
    calls: list[str] = []
    stages = [_RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER]

    class MalformedResult:
        @property
        def stage(self) -> str:
            raise RuntimeError("must not be inspected")

    def malformed_result(context: PolicyContext) -> Any:
        calls.append(f"malformed:{context.capability_name}")
        return MalformedResult()

    stages[0].evaluate = malformed_result  # type: ignore[method-assign]
    decision = PolicyPipeline(stages).evaluate(_context())

    assert decision.allowed is False
    assert decision.stage == PRE_EXECUTION_POLICY_STAGE_ORDER[0]
    assert decision.reason_code == "POLICY_STAGE_CONTRACT_VIOLATION"
    assert calls == ["malformed:get_tool_catalog"]


def test_policy_stage_result_subclass_with_raising_attribute_fails_closed() -> None:
    calls: list[str] = []
    stages = [_RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER]

    class EvilStageResult(PolicyStageResult):
        def __getattribute__(self, name: str) -> Any:
            if name == "stage":
                raise RuntimeError("must not escape")
            return super().__getattribute__(name)

    def malformed_result(context: PolicyContext) -> Any:
        calls.append(f"malformed-subclass:{context.capability_name}")
        return object.__new__(EvilStageResult)

    stages[0].evaluate = malformed_result  # type: ignore[method-assign]
    decision = PolicyPipeline(stages).evaluate(_context())

    assert decision.allowed is False
    assert decision.stage == PRE_EXECUTION_POLICY_STAGE_ORDER[0]
    assert decision.reason_code == "POLICY_STAGE_CONTRACT_VIOLATION"
    assert calls == ["malformed-subclass:get_tool_catalog"]


def test_uninitialized_exact_policy_stage_result_fails_closed() -> None:
    calls: list[str] = []
    stages = [_RecordingStage(name, calls) for name in PRE_EXECUTION_POLICY_STAGE_ORDER]

    def malformed_result(context: PolicyContext) -> Any:
        calls.append(f"malformed-exact:{context.capability_name}")
        return object.__new__(PolicyStageResult)

    stages[0].evaluate = malformed_result  # type: ignore[method-assign]
    decision = PolicyPipeline(stages).evaluate(_context())

    assert decision.allowed is False
    assert decision.stage == PRE_EXECUTION_POLICY_STAGE_ORDER[0]
    assert decision.reason_code == "POLICY_STAGE_CONTRACT_VIOLATION"
    assert calls == ["malformed-exact:get_tool_catalog"]


def test_policy_decision_rejects_policy_stage_result_subclasses() -> None:
    class EvilStageResult(PolicyStageResult):
        pass

    evil = EvilStageResult(
        stage="argument_schema",
        allowed=True,
        reason_code="POLICY_STAGE_ALLOWED",
        message="allowed",
        evidence={},
    )

    with pytest.raises(TypeError, match="PolicyStageResult"):
        PolicyDecision(
            capability_name="get_tool_catalog",
            allowed=True,
            stage="policy_pipeline",
            reason_code="POLICY_PIPELINE_ALLOWED",
            message="allowed",
            evidence={},
            stop_required=False,
            stage_results=(evil,),
        )


def test_required_stage_cannot_claim_not_applicable() -> None:
    calls: list[str] = []
    stages = [
        _RecordingStage(name, calls)
        for name in PRE_EXECUTION_POLICY_STAGE_ORDER
    ]

    def skipped_required(context: PolicyContext) -> PolicyStageResult:
        calls.append("argument_schema")
        return PolicyStageResult(
            stage="argument_schema",
            allowed=True,
            reason_code="POLICY_STAGE_NOT_APPLICABLE",
            message=f"Skipped {context.capability_name}.",
            evidence={},
            applicable=False,
        )

    stages[1].evaluate = skipped_required  # type: ignore[method-assign]
    decision = PolicyPipeline(stages).evaluate(_context())

    assert decision.allowed is False
    assert decision.stage == "argument_schema"
    assert decision.reason_code == "POLICY_STAGE_CONTRACT_VIOLATION"
    assert calls == list(PRE_EXECUTION_POLICY_STAGE_ORDER[:2])


def test_policy_decision_record_is_bounded_json_safe_and_redacts_sensitive_values() -> None:
    calls: list[str] = []
    stages = [
        _RecordingStage(
            name,
            calls,
            evidence={
                "auth_secret": "do-not-persist",
                "raw_tcl": "exec dangerous",
                "large": "x" * 4_000,
                "safe_digest": "b" * 64,
            },
        )
        for name in PRE_EXECUTION_POLICY_STAGE_ORDER
    ]
    decision = PolicyPipeline(stages).evaluate(_context())
    record = decision.to_record()
    serialized = json.dumps(record, sort_keys=True)

    assert len(serialized.encode("utf-8")) <= 16_384
    assert "do-not-persist" not in serialized
    assert "exec dangerous" not in serialized
    assert "x" * 512 not in serialized
    assert "b" * 64 in serialized
    assert record["schema_version"] == 1
    assert record["capability"] == "get_tool_catalog"
    assert decision.to_record() == decision.to_record()
    assert json.loads(json.dumps(record, sort_keys=True)) == record


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "password_digest",
        "auth_sha256",
        "token_digest",
        "secret_digest",
    ],
)
def test_policy_decision_record_redacts_digests_under_sensitive_keys(
    sensitive_key: str,
) -> None:
    credential_digest = "a1" * 32
    decision = PolicyDecision(
        capability_name="get_tool_catalog",
        allowed=False,
        stage="argument_schema",
        reason_code="SENSITIVE_DIGEST_TEST_BLOCK",
        message="sensitive digest redaction",
        evidence={sensitive_key: credential_digest},
        stop_required=True,
    )

    serialized = json.dumps(decision.to_record(), sort_keys=True)

    assert sensitive_key not in serialized
    assert credential_digest not in serialized
    assert "<redacted>" in serialized


def test_policy_decision_record_bounds_custom_mapping_iteration_before_sorting() -> None:
    class UnboundedItemsMapping(Mapping[str, Any]):
        def __getitem__(self, key: str) -> Any:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("to_record must consume the bounded items iterator")

        def __len__(self) -> int:
            raise AssertionError("to_record must not ask an untrusted mapping for its length")

        def items(self) -> Iterator[tuple[str, int]]:
            index = 0
            while True:
                yield f"item-{index:04d}", index
                index += 1

    decision = PolicyDecision(
        capability_name="get_tool_catalog",
        allowed=False,
        stage="argument_schema",
        reason_code="UNBOUNDED_MAPPING_TEST_BLOCK",
        message="bounded mapping iteration",
        evidence={},
        stop_required=True,
    )
    object.__setattr__(decision, "evidence", UnboundedItemsMapping())

    record = decision.to_record()

    assert record["evidence"]["_truncated_items"] == 1
    assert len(record["evidence"]) <= policy_pipeline_module._MAX_EVIDENCE_ITEMS + 1


def test_policy_decision_record_revalidates_mutated_fields() -> None:
    decision = _pipeline([]).evaluate(_context())
    mutated_stage = decision.stage_results[0]
    object.__setattr__(mutated_stage, "reason_code", "X" * 20_000)
    object.__setattr__(mutated_stage, "allowed", "yes")
    object.__setattr__(mutated_stage, "evidence", object())
    object.__setattr__(decision, "reason_code", "X" * 20_000)
    object.__setattr__(decision, "capability_name", "C" * 20_000)
    object.__setattr__(decision, "phase", "P" * 20_000)
    object.__setattr__(decision, "mode", "M" * 20_000)
    object.__setattr__(decision, "request_id", "R" * 20_000)
    object.__setattr__(decision, "capability_policy_sha256", "D" * 20_000)
    object.__setattr__(decision, "context_identity_sha256", "D" * 20_000)
    object.__setattr__(decision, "allowed", "yes")
    object.__setattr__(decision, "evidence", object())
    object.__setattr__(decision, "started_at", "not-a-datetime")
    object.__setattr__(decision, "stage_results", (mutated_stage,) * 100)

    record = decision.to_record()
    serialized = json.dumps(record, sort_keys=True)

    assert len(serialized.encode("utf-8")) <= POLICY_DECISION_MAX_BYTES
    assert "X" * 128 not in serialized
    assert "C" * 128 not in serialized
    assert record["allowed"] is False
    assert record["stop_required"] is True
    assert record["reason_code"] == "POLICY_RECORD_FIELD_INVALID"
    assert record["phase"] == "invalid"
    assert record["capability_policy_sha256"] == ""
    assert "started_at" not in record
    assert all(
        result["reason_code"] == "POLICY_RECORD_FIELD_INVALID"
        for result in record["stage_results"]
    )


def test_policy_decision_record_hard_cap_survives_adversarial_keys_and_messages() -> None:
    secret = "NEVER_PERSIST_POLICY_SECRET_9f3b"
    secret_key = "auth_token_sk_live_ABC123"
    low_entropy_key = "password=admin"
    raw_tcl = "exec vivado -mode batch -source attacker.tcl"
    huge_evidence = {
        f"{index:02d}-{'key-material-' * 800}": {
            "configuration_errors": [
                f"missing={'x' * 220}-{item:02d}"
                for item in range(16)
            ]
        }
        for index in range(28)
    }
    huge_evidence["raw_tcl"] = raw_tcl
    huge_evidence["ordinary"] = secret
    huge_evidence[secret_key] = "hidden"
    huge_evidence[low_entropy_key] = "hidden"
    stages = [
        _RecordingStage(name, [], evidence=huge_evidence)
        for name in PRE_EXECUTION_POLICY_STAGE_ORDER
    ]
    for stage in stages:
        stage.evaluate = (  # type: ignore[method-assign]
            lambda context, current=stage: PolicyStageResult(
                stage=current.name,
                allowed=True,
                reason_code="POLICY_STAGE_ALLOWED",
                message=f"{secret}: {raw_tcl}",
                evidence=huge_evidence,
                stop_required=False,
            )
        )

    record = PolicyPipeline(stages).evaluate(_context()).to_record()
    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)

    assert len(serialized.encode("utf-8")) <= POLICY_DECISION_MAX_BYTES
    assert secret not in serialized
    assert secret_key not in serialized
    assert low_entropy_key not in serialized
    assert raw_tcl not in serialized
    assert "key-material-" not in serialized
    assert record["evidence"]["summary_truncated"] is True
    assert len(record["stage_results"]) <= len(POLICY_STAGE_ORDER)


def test_policy_decision_record_redacts_numeric_secrets_under_unknown_keys() -> None:
    otp = 98_765_432_123_456_789
    pin = 246_813_579
    decision = PolicyDecision(
        capability_name="get_tool_catalog",
        allowed=False,
        stage="argument_schema",
        reason_code="NUMERIC_SECRET_TEST_BLOCK",
        message="numeric secret test",
        evidence={"otp": otp, "pin": pin},
        stop_required=True,
    )

    serialized = json.dumps(decision.to_record(), sort_keys=True)

    assert str(otp) not in serialized
    assert str(pin) not in serialized
    assert serialized.count("<redacted-number>") == 2


def test_policy_decision_record_uses_one_global_evidence_traversal_budget() -> None:
    nested: Any = {
        f"branch_{branch}": {
            "configuration_errors": [
                f"missing=stage_{branch}_{index}"
                for index in range(16)
            ]
        }
        for branch in range(32)
    }
    decision = PolicyDecision(
        capability_name="get_tool_catalog",
        allowed=False,
        stage="argument_schema",
        reason_code="EVIDENCE_BUDGET_TEST_BLOCK",
        message="bounded evidence",
        evidence=nested,
        stop_required=True,
    )

    record = decision.to_record()
    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)

    assert len(serialized.encode("utf-8")) <= POLICY_DECISION_MAX_BYTES
    assert record["evidence_budget_truncated"] is True
    assert "global-evidence-budget" in serialized


def test_all_capabilities_have_explicit_conservative_stage_requirements() -> None:
    assert len(CAPABILITY_SPECS) == 98
    for spec in CAPABILITY_SPECS:
        requirements = policy_stage_requirements(spec)
        assert tuple(requirements) == POLICY_STAGE_ORDER
        assert all(isinstance(required, bool) for required in requirements.values())
        assert requirements["argument_schema"] is True
        assert requirements["capability_profile_authorization"] is True
        assert requirements["mutation_destructive_intent"] is (spec.mutation or spec.destructive)
        assert requirements["hardware_programming_intent"] is spec.hardware
        assert requirements["command_execution_boundary"] is (spec.dispatch_lane == "serialized_backend")
        assert requirements["project_capability_generation"] is (spec.required_project_state != "none")
        assert requirements["execution_input_closure"] is (
            spec.execution_input_policy == "blocks_unattested_composite"
            or spec.risk_class == "project_execution"
        )


def test_local_control_tools_do_not_require_vivado_or_project_state() -> None:
    for spec in CAPABILITY_SPECS:
        requirements = policy_stage_requirements(spec)
        if spec.dispatch_lane != "local":
            continue
        assert requirements["executable_session_identity"] is False
        assert requirements["managed_path_boundary"] is False
        assert requirements["project_capability_generation"] is False
        assert requirements["command_execution_boundary"] is False
