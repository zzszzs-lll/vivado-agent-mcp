from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import mcp.types as types


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    domain: str
    handler: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_class: str
    profiles: frozenset[str]
    workflow_tags: tuple[str, ...]
    required_session_state: str
    required_project_state: str
    read_only: bool
    mutation: bool
    destructive: bool
    hardware: bool
    idempotent: bool
    duration_class: str
    supported_vivado_versions: tuple[str, ...]
    qualified_vivado_versions: tuple[str, ...]
    task_eligible: bool
    open_world: bool
    dispatch_lane: str
    execution_input_policy: str
    evidence_contract: tuple[str, ...]
    artifact_contract: tuple[str, ...]
    hardware_tier: str = "not_hardware"

    @property
    def risk(self) -> str:
        """Compatibility alias retained for existing catalog and policy callers."""

        return self.risk_class

    def to_mcp_tool(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            description=self.description,
            inputSchema=deepcopy(self.input_schema),
            outputSchema=deepcopy(self.output_schema),
            annotations=types.ToolAnnotations(
                readOnlyHint=self.read_only,
                destructiveHint=self.destructive,
                idempotentHint=self.idempotent,
                openWorldHint=self.open_world,
            ),
        )

    def to_manifest_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": self.domain,
            "handler": self.handler,
            "description": self.description,
            "input_schema": deepcopy(self.input_schema),
            "output_schema": deepcopy(self.output_schema),
            "risk_class": self.risk_class,
            "profiles": sorted(self.profiles),
            "workflow_tags": list(self.workflow_tags),
            "mcp_annotations": {
                "read_only": self.read_only,
                "destructive": self.destructive,
                "idempotent": self.idempotent,
                "open_world": self.open_world,
            },
            "required_session_state": self.required_session_state,
            "required_project_state": self.required_project_state,
            "mutation": self.mutation,
            "hardware": self.hardware,
            "hardware_tier": self.hardware_tier,
            "hardware_validation_status": "NOT_VALIDATED" if self.hardware else "NOT_APPLICABLE",
            "duration_class": self.duration_class,
            "supported_vivado_versions": list(self.supported_vivado_versions),
            "qualified_vivado_versions": list(self.qualified_vivado_versions),
            "task_eligible": self.task_eligible,
            "dispatch_lane": self.dispatch_lane,
            "execution_input_policy": self.execution_input_policy,
            "evidence_contract": list(self.evidence_contract),
            "artifact_contract": list(self.artifact_contract),
        }
