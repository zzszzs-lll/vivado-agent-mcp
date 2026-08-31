from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .policy_pipeline import (
    PRE_EXECUTION_POLICY_STAGE_ORDER,
    PolicyContext,
    PolicyDecision,
    PolicyPipeline,
    PolicyStageResult,
    policy_stage_requirements,
)
from .registry import TOOL_REGISTRY, validate_tool_arguments
from .vivado.env import validate_trusted_vivado_executable


POLICY_SHADOW_SCHEMA_VERSION = 1
POLICY_SHADOW_MAX_BYTES = 24_576
POLICY_SHADOW_STAGE_ALLOWED = "POLICY_SHADOW_STAGE_ALLOWED"
POLICY_SHADOW_LEGACY_AUTHORITY_RETAINED = (
    "POLICY_SHADOW_LEGACY_AUTHORITY_RETAINED"
)
POLICY_SHADOW_EVALUATION_FAILED = "POLICY_SHADOW_EVALUATION_FAILED"
POLICY_STAGE_NOT_APPLICABLE = "POLICY_STAGE_NOT_APPLICABLE"
POLICY_LEGACY_PRE_HANDLER_ALLOWED = "POLICY_LEGACY_PRE_HANDLER_ALLOWED"
_REASON_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


@dataclass(frozen=True)
class LegacyPreHandlerDecision:
    """The official legacy facade outcome compared by the shadow pipeline."""

    allowed: bool
    stage: str
    reason_code: str

    @classmethod
    def allow(cls) -> LegacyPreHandlerDecision:
        return cls(
            allowed=True,
            stage="legacy_pre_handler",
            reason_code=POLICY_LEGACY_PRE_HANDLER_ALLOWED,
        )

    @classmethod
    def block(cls, reason_code: str) -> LegacyPreHandlerDecision:
        normalized = _safe_reason_code(reason_code, fallback="LEGACY_POLICY_BLOCKED")
        return cls(
            allowed=False,
            stage=_legacy_stage_for_reason(normalized),
            reason_code=normalized,
        )


@dataclass(frozen=True)
class PolicyShadowFacts:
    """Bounded facade facts already observed by the legacy call path."""

    managed_transport_observed: bool = False
    managed_transport: bool = False
    guarded_project_action: bool = False
    repair_dry_run: bool = False
    project_mutation_scope: str = "unbound"
    project_capability_check: str = "not_checked"

    def to_session_identity(self) -> dict[str, Any]:
        return {
            "projection_schema": "vivado_tool_service_shadow_v1",
            "managed_transport_observed": self.managed_transport_observed,
            "managed_transport": self.managed_transport,
            "guarded_project_action": self.guarded_project_action,
            "repair_dry_run": self.repair_dry_run,
            "project_mutation_scope": self.project_mutation_scope,
            "project_capability_check": self.project_capability_check,
        }


@dataclass(frozen=True)
class _ShadowPolicyStage:
    name: str

    def evaluate(self, context: PolicyContext) -> PolicyStageResult:
        required = bool(policy_stage_requirements(context.capability)[self.name])
        if not required:
            return PolicyStageResult(
                stage=self.name,
                allowed=True,
                reason_code=POLICY_STAGE_NOT_APPLICABLE,
                message="The capability metadata marks this policy stage as not applicable.",
                evidence={"required": False},
                stop_required=False,
                applicable=False,
            )

        if self.name == "executable_session_identity":
            return _evaluate_executable_session_identity(context)
        if self.name == "argument_schema":
            return _evaluate_argument_schema(context)
        if self.name == "capability_profile_authorization":
            return _evaluate_capability_profile(context)
        if self.name == "project_capability_generation":
            return _evaluate_project_capability(context)
        if self.name == "mutation_destructive_intent":
            return _evaluate_mutation_intent(context)
        if self.name == "execution_input_closure":
            return _evaluate_execution_input_closure(context)
        return _legacy_authority_result(self.name)


_SHADOW_PIPELINE = PolicyPipeline(
    tuple(_ShadowPolicyStage(name) for name in PRE_EXECUTION_POLICY_STAGE_ORDER),
    phase="pre_execution",
)


def evaluate_policy_shadow(
    *,
    capability_name: str,
    arguments: Any,
    active_profile: str,
    profile_enforced: bool,
    trusted_vivado_identity: Mapping[str, Any] | None,
    project_capability: Mapping[str, Any] | None,
    facts: PolicyShadowFacts,
    legacy_decision: LegacyPreHandlerDecision,
    request_id: str,
    started_at: datetime,
) -> dict[str, Any]:
    """Evaluate the additive shadow pipeline without granting execution authority."""

    capability = TOOL_REGISTRY.get(capability_name)
    if capability is None:
        decision = _fallback_decision(
            capability_name=capability_name,
            reason_code="UNKNOWN_TOOL",
            request_id=request_id,
            started_at=started_at,
            evidence={"capability_registered": False},
        )
        return _comparison_record(
            legacy_decision=legacy_decision,
            pipeline_decision=decision,
            pipeline_evaluated=False,
        )

    context = PolicyContext.create(
        capability=capability,
        arguments=arguments,
        active_profile=active_profile,
        profile_enforced=profile_enforced,
        caller_identity=None,
        session_identity=facts.to_session_identity(),
        project_capability=project_capability,
        trusted_vivado_identity=trusted_vivado_identity,
        request_id=request_id,
        started_at=started_at,
    )
    decision = _SHADOW_PIPELINE.evaluate(context)
    return _comparison_record(
        legacy_decision=legacy_decision,
        pipeline_decision=decision,
        pipeline_evaluated=True,
    )


def policy_shadow_failure_record(
    *,
    capability_name: str,
    legacy_decision: LegacyPreHandlerDecision,
    request_id: str,
    started_at: datetime,
    exception_type: str,
) -> dict[str, Any]:
    """Return a fail-closed shadow record while leaving legacy authority unchanged."""

    decision = _fallback_decision(
        capability_name=capability_name,
        reason_code=POLICY_SHADOW_EVALUATION_FAILED,
        request_id=request_id,
        started_at=started_at,
        evidence={"exception_type": _safe_identifier(exception_type)},
    )
    return _comparison_record(
        legacy_decision=legacy_decision,
        pipeline_decision=decision,
        pipeline_evaluated=False,
    )


def _evaluate_executable_session_identity(
    context: PolicyContext,
) -> PolicyStageResult:
    arguments = context.arguments
    properties = context.capability.input_schema.get("properties", {})
    if (
        isinstance(arguments, dict)
        and "vivado_path" in arguments
        and isinstance(properties, dict)
        and "vivado_path" in properties
    ):
        assertion = validate_trusted_vivado_executable(
            arguments.get("vivado_path"),
            trusted_identity=(
                dict(context.trusted_vivado_identity)
                if context.trusted_vivado_identity is not None
                else None
            ),
        )
        if not assertion.get("ok"):
            reason_code = _safe_reason_code(
                assertion.get("error_code"),
                fallback="VIVADO_PATH_MISMATCH",
            )
            return PolicyStageResult(
                stage="executable_session_identity",
                allowed=False,
                reason_code=reason_code,
                message="The requested Vivado executable identity did not match the trusted server identity.",
                evidence={"path_assertion_checked": True},
                stop_required=True,
            )
        return PolicyStageResult(
            stage="executable_session_identity",
            allowed=True,
            reason_code=POLICY_SHADOW_STAGE_ALLOWED,
            message="The requested Vivado executable identity matched the trusted server identity.",
            evidence={"path_assertion_checked": True},
            stop_required=False,
        )
    return _legacy_authority_result("executable_session_identity")


def _evaluate_argument_schema(context: PolicyContext) -> PolicyStageResult:
    issues = validate_tool_arguments(context.capability_name, context.arguments)
    if issues:
        return PolicyStageResult(
            stage="argument_schema",
            allowed=False,
            reason_code="INVALID_TOOL_ARGUMENTS",
            message="Tool arguments do not satisfy the registered MCP input schema.",
            evidence={"validation_failed": True, "validation_error_count": len(issues)},
            stop_required=True,
        )
    return PolicyStageResult(
        stage="argument_schema",
        allowed=True,
        reason_code=POLICY_SHADOW_STAGE_ALLOWED,
        message="Tool arguments satisfy the registered MCP input schema.",
        evidence={"validation_failed": False},
        stop_required=False,
    )


def _evaluate_capability_profile(context: PolicyContext) -> PolicyStageResult:
    if context.profile_enforced and context.active_profile not in context.capability.profiles:
        return PolicyStageResult(
            stage="capability_profile_authorization",
            allowed=False,
            reason_code="TOOL_NOT_AVAILABLE_IN_PROFILE",
            message="The capability is not available in the active MCP profile.",
            evidence={"profile_enforced": True, "active_profile": context.active_profile},
            stop_required=True,
        )
    return PolicyStageResult(
        stage="capability_profile_authorization",
        allowed=True,
        reason_code=POLICY_SHADOW_STAGE_ALLOWED,
        message="The capability is available under the active MCP profile policy.",
        evidence={"profile_enforced": context.profile_enforced},
        stop_required=False,
    )


def _evaluate_project_capability(context: PolicyContext) -> PolicyStageResult:
    facts = _session_facts(context)
    if facts.get("project_capability_check") == "failed":
        return PolicyStageResult(
            stage="project_capability_generation",
            allowed=False,
            reason_code="PROJECT_CAPABILITY_INVALID",
            message="The legacy facade rejected the active project capability snapshot.",
            evidence={"legacy_capability_check": "failed"},
            stop_required=True,
        )
    return _legacy_authority_result("project_capability_generation")


def _evaluate_mutation_intent(context: PolicyContext) -> PolicyStageResult:
    facts = _session_facts(context)
    if (
        facts.get("managed_transport") is True
        and facts.get("guarded_project_action") is True
        and facts.get("repair_dry_run") is False
        and facts.get("project_mutation_scope") != "mcp_created_project"
    ):
        return PolicyStageResult(
            stage="mutation_destructive_intent",
            allowed=False,
            reason_code="EXISTING_PROJECT_MUTATION_REQUIRES_WORKING_COPY",
            message="Existing-project mutation or execution requires a separate MCP-managed working copy.",
            evidence={"existing_project_protected": True},
            stop_required=True,
        )
    return _legacy_authority_result("mutation_destructive_intent")


def _evaluate_execution_input_closure(context: PolicyContext) -> PolicyStageResult:
    facts = _session_facts(context)
    if (
        facts.get("managed_transport") is True
        and facts.get("guarded_project_action") is True
        and facts.get("repair_dry_run") is False
        and context.capability.execution_input_policy
        == "blocks_unattested_composite"
    ):
        return PolicyStageResult(
            stage="execution_input_closure",
            allowed=False,
            reason_code="EXECUTABLE_COMPOSITE_INPUT_UNSUPPORTED",
            message="Executable composite inputs are not covered by the trusted input closure.",
            evidence={"unattested_composite_inputs_blocked": True},
            stop_required=True,
        )
    return _legacy_authority_result("execution_input_closure")


def _legacy_authority_result(stage: str) -> PolicyStageResult:
    return PolicyStageResult(
        stage=stage,
        allowed=True,
        reason_code=POLICY_SHADOW_LEGACY_AUTHORITY_RETAINED,
        message="This stage remains enforced by the existing legacy gate or handler in shadow mode.",
        evidence={"legacy_authority_retained": True},
        stop_required=False,
    )


def _session_facts(context: PolicyContext) -> Mapping[str, Any]:
    return context.session_identity or {}


def _fallback_decision(
    *,
    capability_name: str,
    reason_code: str,
    request_id: str,
    started_at: datetime,
    evidence: Mapping[str, Any],
) -> PolicyDecision:
    return PolicyDecision(
        capability_name=_safe_identifier(capability_name) or "unknown_capability",
        allowed=False,
        stage="policy_pipeline",
        reason_code=_safe_reason_code(
            reason_code,
            fallback=POLICY_SHADOW_EVALUATION_FAILED,
        ),
        message="The shadow policy evaluation failed closed without changing the legacy decision.",
        evidence=evidence,
        stop_required=True,
        request_id=_safe_identifier(request_id) or "shadow-request",
        started_at=started_at,
        phase="pre_execution",
        mode="foundation",
    )


def _comparison_record(
    *,
    legacy_decision: LegacyPreHandlerDecision,
    pipeline_decision: PolicyDecision,
    pipeline_evaluated: bool,
) -> dict[str, Any]:
    outcome_equivalent = legacy_decision.allowed == pipeline_decision.allowed
    reason_equivalent = bool(
        outcome_equivalent
        and (
            legacy_decision.allowed
            or legacy_decision.reason_code == pipeline_decision.reason_code
        )
    )
    stage_equivalent = bool(
        outcome_equivalent
        and (
            legacy_decision.allowed
            or legacy_decision.stage == pipeline_decision.stage
        )
    )
    record: dict[str, Any] = {
        "schema_version": POLICY_SHADOW_SCHEMA_VERSION,
        "mode": "shadow",
        "evaluation_scope": "pre_handler_facade",
        "authoritative_source": "legacy_vivado_tool_service",
        "legacy_authority_retained": True,
        "pipeline_evaluated": pipeline_evaluated,
        "legacy": {
            "allowed": legacy_decision.allowed,
            "stage": legacy_decision.stage,
            "reason_code": legacy_decision.reason_code,
        },
        "pipeline": pipeline_decision.to_record(),
        "comparison": {
            "outcome_equivalent": outcome_equivalent,
            "reason_equivalent": reason_equivalent,
            "stage_equivalent": stage_equivalent,
            "equivalent": outcome_equivalent and reason_equivalent and stage_equivalent,
            "false_allow": pipeline_decision.allowed and not legacy_decision.allowed,
            "false_block": legacy_decision.allowed and not pipeline_decision.allowed,
        },
    }
    serialized = _json_bytes(record)
    if len(serialized) <= POLICY_SHADOW_MAX_BYTES:
        return record

    pipeline_record = record["pipeline"]
    record["pipeline"] = {
        "summary_truncated": True,
        "full_record_sha256": hashlib.sha256(_json_bytes(pipeline_record)).hexdigest(),
        "allowed": bool(pipeline_record.get("allowed")),
        "stage": _safe_identifier(pipeline_record.get("stage")),
        "reason_code": _safe_reason_code(
            pipeline_record.get("reason_code"),
            fallback=POLICY_SHADOW_EVALUATION_FAILED,
        ),
    }
    record["record_truncated"] = True
    return record


def _legacy_stage_for_reason(reason_code: str) -> str:
    if reason_code == "INVALID_TOOL_ARGUMENTS":
        return "argument_schema"
    if reason_code == "TOOL_NOT_AVAILABLE_IN_PROFILE":
        return "capability_profile_authorization"
    if reason_code.startswith("VIVADO_PATH_"):
        return "executable_session_identity"
    if reason_code == "PROJECT_CAPABILITY_INVALID":
        return "project_capability_generation"
    if reason_code == "EXISTING_PROJECT_MUTATION_REQUIRES_WORKING_COPY":
        return "mutation_destructive_intent"
    if reason_code == "EXECUTABLE_COMPOSITE_INPUT_UNSUPPORTED":
        return "execution_input_closure"
    return "policy_pipeline"


def _safe_identifier(value: Any) -> str:
    if type(value) is not str:
        return ""
    normalized = value.strip()
    if not normalized:
        return ""
    return normalized[:128]


def _safe_reason_code(value: Any, *, fallback: str) -> str:
    if type(value) is str and _REASON_CODE_PATTERN.fullmatch(value):
        return value
    return fallback


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
