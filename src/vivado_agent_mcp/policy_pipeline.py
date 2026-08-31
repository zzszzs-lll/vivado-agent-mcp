from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from itertools import islice
from pathlib import Path
from threading import RLock
from types import CodeType, FunctionType, MappingProxyType
from typing import Any, Mapping, Protocol, Sequence
from weakref import ReferenceType, ref

from .capability_spec import CapabilitySpec


POLICY_DECISION_SCHEMA_VERSION = 1
POLICY_DECISION_MAX_BYTES = 16_384
POLICY_PIPELINE_VERSION = 1
POLICY_PIPELINE_ALLOWED = "POLICY_PIPELINE_ALLOWED"
POLICY_PIPELINE_CONFIGURATION_INVALID = "POLICY_PIPELINE_CONFIGURATION_INVALID"
POLICY_STAGE_CONTRACT_VIOLATION = "POLICY_STAGE_CONTRACT_VIOLATION"
POLICY_STAGE_EVALUATION_FAILED = "POLICY_STAGE_EVALUATION_FAILED"
POLICY_CONTEXT_PHASE_MISMATCH = "POLICY_CONTEXT_PHASE_MISMATCH"
POLICY_CONTEXT_REVALIDATION_FAILED = "POLICY_CONTEXT_REVALIDATION_FAILED"
POLICY_PIPELINE_AUTHORITY_UNAVAILABLE = "POLICY_PIPELINE_AUTHORITY_UNAVAILABLE"
_TRUSTED_PRE_EXECUTION_ISSUER_KEY = object()
_CANONICAL_PRE_EXECUTION_PIPELINE: PolicyPipeline | None = None

PRE_EXECUTION_POLICY_STAGE_ORDER: tuple[str, ...] = (
    "executable_session_identity",
    "argument_schema",
    "capability_profile_authorization",
    "managed_path_boundary",
    "project_capability_generation",
    "mutation_destructive_intent",
    "hardware_programming_intent",
    "execution_input_closure",
    "command_execution_boundary",
)

POST_EXECUTION_POLICY_STAGE_ORDER: tuple[str, ...] = (
    "evidence_freshness_identity",
    "audit_signoff_terminal_semantics",
)

POLICY_STAGE_ORDER: tuple[str, ...] = (
    *PRE_EXECUTION_POLICY_STAGE_ORDER,
    *POST_EXECUTION_POLICY_STAGE_ORDER,
)
POLICY_PHASE_STAGE_ORDERS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "pre_execution": PRE_EXECUTION_POLICY_STAGE_ORDER,
        "post_execution": POST_EXECUTION_POLICY_STAGE_ORDER,
    }
)

_AUDIT_TERMINAL_TOOLS = frozenset(
    {
        "run_pre_hw_signoff",
        "run_project_audit",
        "collect_diagnostic_bundle",
        "validate_diagnostic_bundle",
        "check_bitstream_readiness",
    }
)
_SESSION_IDENTITY_ENTRY_TOOLS = frozenset(
    {
        "detect_vivado_environment",
        "detect_hardware_environment",
        "start_session",
        "stop_session",
        "run_tcl",
        "safe_tcl",
    }
)
_CAPABILITY_ENUM_VALUES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "domain": frozenset(
            {
                "agent_guidance",
                "block_design",
                "constraints",
                "custom_tcl",
                "diagnostics",
                "hardware_boundary",
                "ip",
                "project",
                "reports",
                "runs",
                "runtime_lightweight",
                "session",
                "simulation",
            }
        ),
        "risk_class": frozenset(
            {
                "destructive_dry_run",
                "hardware",
                "hardware_destructive",
                "normal",
                "project_execution",
                "project_mutation_immediate",
                "tcl_policy_dry_run",
            }
        ),
        "required_session_state": frozenset(
            {"managed_session", "none", "session_stopped_for_mutation"}
        ),
        "required_project_state": frozenset(
            {"managed_project_open", "none", "project_open", "project_or_manifest"}
        ),
        "duration_class": frozenset({"long", "medium", "short"}),
        "dispatch_lane": frozenset({"local", "serialized_backend"}),
        "execution_input_policy": frozenset(
            {"blocks_unattested_composite", "typed_tool_policy"}
        ),
        "hardware_tier": frozenset(
            {
                "hardware_destructive",
                "hardware_disabled_by_default",
                "hardware_log_readonly",
                "hardware_safe_detector",
                "not_hardware",
            }
        ),
    }
)
_CAPABILITY_PROFILE_SETS = frozenset(
    {
        frozenset({"all"}),
        frozenset({"advanced", "all"}),
        frozenset({"core", "advanced", "all"}),
    }
)
_CAPABILITY_RISK_BOOLEAN_SIGNATURES: Mapping[
    str,
    frozenset[tuple[bool, bool, bool, bool]],
] = MappingProxyType(
    {
        "normal": frozenset(
            {
                (True, False, False, False),
                (False, True, False, False),
            }
        ),
        "tcl_policy_dry_run": frozenset({(True, False, False, False)}),
        "project_mutation_immediate": frozenset(
            {
                (False, True, False, False),
                (False, True, True, False),
            }
        ),
        "project_execution": frozenset({(False, True, False, False)}),
        "destructive_dry_run": frozenset({(False, True, True, False)}),
        "hardware": frozenset(
            {
                (True, False, False, True),
                (False, True, False, True),
            }
        ),
        "hardware_destructive": frozenset({(False, True, True, True)}),
    }
)
_CAPABILITY_RISK_STATE_SIGNATURES: Mapping[
    str,
    frozenset[tuple[str, str]],
] = MappingProxyType(
    {
        "normal": frozenset(
            {
                ("none", "none"),
                ("none", "project_or_manifest"),
                ("managed_session", "none"),
                ("managed_session", "project_open"),
            }
        ),
        "tcl_policy_dry_run": frozenset({("none", "none")}),
        "project_mutation_immediate": frozenset(
            {
                ("managed_session", "none"),
                ("managed_session", "managed_project_open"),
            }
        ),
        "project_execution": frozenset(
            {("managed_session", "managed_project_open")}
        ),
        "destructive_dry_run": frozenset(
            {
                ("session_stopped_for_mutation", "none"),
                ("managed_session", "managed_project_open"),
            }
        ),
        "hardware": frozenset(
            {
                ("none", "none"),
                ("managed_session", "none"),
            }
        ),
        "hardware_destructive": frozenset({("managed_session", "none")}),
    }
)
_CAPABILITY_RISK_HARDWARE_TIERS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "normal": frozenset({"not_hardware"}),
        "tcl_policy_dry_run": frozenset({"not_hardware"}),
        "project_mutation_immediate": frozenset({"not_hardware"}),
        "project_execution": frozenset({"not_hardware"}),
        "destructive_dry_run": frozenset({"not_hardware"}),
        "hardware": frozenset(
            {
                "hardware_safe_detector",
                "hardware_log_readonly",
                "hardware_disabled_by_default",
            }
        ),
        "hardware_destructive": frozenset({"hardware_destructive"}),
    }
)
_CAPABILITY_HARDWARE_TIER_SIGNATURES: Mapping[
    str,
    frozenset[tuple[bool, bool, bool, str, str, str]],
] = MappingProxyType(
    {
        "hardware_safe_detector": frozenset(
            {(True, False, False, "none", "none", "serialized_backend")}
        ),
        "hardware_log_readonly": frozenset(
            {
                (
                    True,
                    False,
                    False,
                    "managed_session",
                    "none",
                    "serialized_backend",
                )
            }
        ),
        "hardware_disabled_by_default": frozenset(
            {
                (
                    True,
                    False,
                    False,
                    "managed_session",
                    "none",
                    "serialized_backend",
                ),
                (
                    False,
                    True,
                    False,
                    "managed_session",
                    "none",
                    "serialized_backend",
                ),
            }
        ),
        "hardware_destructive": frozenset(
            {
                (
                    False,
                    True,
                    True,
                    "managed_session",
                    "none",
                    "serialized_backend",
                )
            }
        ),
    }
)
_COMPOSITE_INPUT_SIGNATURES = frozenset(
    {
        ("normal", "project_open"),
        ("project_mutation_immediate", "managed_project_open"),
    }
)
_PATH_ARGUMENT_NAMES = frozenset(
    {
        "artifact_manifest_path",
        "bitstream_path",
        "bundle_dir",
        "diagnostic_manifest_path",
        "manifest_path",
        "output_dir",
        "project_dir",
        "project_path",
        "report_dir",
        "report_manifest_path",
        "runtime_dir",
        "vivado_path",
        "waiver_path",
        "xdc_path",
    }
)
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "argument",
    "auth",
    "command",
    "credential",
    "password",
    "payload",
    "raw_tcl",
    "secret",
    "token",
)
_MAX_EVIDENCE_DEPTH = 4
_MAX_EVIDENCE_ITEMS = 32
_MAX_SEQUENCE_ITEMS = 16
_MAX_STRING_LENGTH = 256
_MAX_POLICY_MESSAGE_BYTES = 4_096
_REASON_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SAFE_EVIDENCE_TEXT_KEYS = frozenset(
    {
        "configuration_errors",
        "context_type",
        "exception_type",
        "pipeline_phase",
        "returned_type",
    }
)
_SAFE_EVIDENCE_KEYS = _SAFE_EVIDENCE_TEXT_KEYS | frozenset(
    {
        "required",
        "reported_applicable",
    }
)
_SAFE_EVIDENCE_TEXT_SUFFIXES = ("_digest", "_sha256")
_SAFE_EVIDENCE_TEXT_PATTERN = re.compile(r"^[A-Za-z0-9_.=,:-]{1,256}$")
_HEX_DIGEST_PATTERN = re.compile(r"^[0-9a-fA-F]{32,128}$")
_SAFE_TYPE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_MAX_RECORD_EVIDENCE_NODES = 256
_MAX_SNAPSHOT_DEPTH = 64
_MAX_SNAPSHOT_NODES = 4_096
_MAX_SNAPSHOT_BYTES = 1_048_576
_MAX_SNAPSHOT_LEAF_BYTES = 65_536
_MAX_SNAPSHOT_INTEGER_DIGITS = 4_096
_CONCRETE_PATH_TYPE = type(Path("."))
_IMMUTABLE_LEAF_TYPES = (bool, int, float, str, bytes, datetime, _CONCRETE_PATH_TYPE)


def _safe_type_name(value: Any) -> str:
    try:
        name = type.__getattribute__(type(value), "__name__")
    except BaseException:
        return "unknown"
    return name if type(name) is str and _SAFE_TYPE_NAME_PATTERN.fullmatch(name) else "unknown"


@dataclass(frozen=True)
class _FrozenList:
    items: tuple[Any, ...]


@dataclass(frozen=True)
class _FrozenTuple:
    items: tuple[Any, ...]


@dataclass(frozen=True)
class _FrozenSet:
    items: tuple[Any, ...]
    was_frozenset: bool


@dataclass(frozen=True)
class _FrozenOpaqueValue:
    type_name: str


@dataclass
class _EvidenceBudget:
    remaining_nodes: int = _MAX_RECORD_EVIDENCE_NODES
    truncated: bool = False

    def consume(self) -> bool:
        if self.remaining_nodes <= 0:
            self.truncated = True
            return False
        self.remaining_nodes -= 1
        return True


@dataclass
class _SnapshotFreezeState:
    remaining_nodes: int = _MAX_SNAPSHOT_NODES
    remaining_bytes: int = _MAX_SNAPSHOT_BYTES
    active_container_ids: set[int] = field(default_factory=set)

    def consume(self) -> bool:
        if self.remaining_nodes <= 0:
            return False
        self.remaining_nodes -= 1
        return True

    def consume_bytes(self, size: int) -> bool:
        if size < 0 or size > _MAX_SNAPSHOT_LEAF_BYTES:
            return False
        if self.remaining_bytes < size:
            return False
        self.remaining_bytes -= size
        return True


@dataclass
class _IdentityValidationState:
    remaining_nodes: int = _MAX_SNAPSHOT_NODES
    active_container_ids: set[int] = field(default_factory=set)

    def consume(self, *, field_name: str) -> None:
        if self.remaining_nodes <= 0:
            raise ValueError(f"Policy {field_name} exceeds the identity node budget.")
        self.remaining_nodes -= 1


def _snapshot_leaf_size(value: Any) -> int:
    if value is None:
        return 4
    if type(value) is bool:
        return 5
    if type(value) is int:
        bit_length = abs(value).bit_length()
        decimal_digits = 1 if bit_length == 0 else (bit_length * 30_103) // 100_000 + 1
        if value < 0:
            decimal_digits += 1
        return (
            decimal_digits
            if decimal_digits <= _MAX_SNAPSHOT_INTEGER_DIGITS
            else _MAX_SNAPSHOT_LEAF_BYTES + 1
        )
    if type(value) is float:
        return 24
    if type(value) is str:
        return len(value.encode("utf-8", errors="surrogatepass"))
    if type(value) is bytes:
        return len(value)
    if type(value) is datetime:
        return len(value.isoformat().encode("ascii"))
    if type(value) is _CONCRETE_PATH_TYPE:
        return len(str(value).encode("utf-8", errors="surrogatepass"))
    raise TypeError(f"Unsupported policy snapshot leaf type: {_safe_type_name(value)}.")


def _consume_snapshot_leaf(
    state: _SnapshotFreezeState,
    value: Any,
    *,
    strict: bool,
    field_name: str,
) -> bool:
    if state.consume_bytes(_snapshot_leaf_size(value)):
        return True
    if strict:
        raise ValueError(
            f"Policy {field_name} exceeds the snapshot leaf or shared byte budget."
        )
    return False


def _freeze(
    value: Any,
    *,
    _state: _SnapshotFreezeState | None = None,
    _depth: int = 0,
    _strict: bool = False,
    _field_name: str = "snapshot",
) -> Any:
    if _depth >= _MAX_SNAPSHOT_DEPTH:
        if _strict:
            raise ValueError(f"Policy {_field_name} exceeds the snapshot depth limit.")
        return _FrozenOpaqueValue("snapshot_depth_limit")
    state = _state or _SnapshotFreezeState()
    if not state.consume():
        if _strict:
            raise ValueError(f"Policy {_field_name} exceeds the snapshot node budget.")
        return _FrozenOpaqueValue("snapshot_budget_limit")
    is_container = isinstance(value, (Mapping, list, tuple, set, frozenset)) or type(
        value
    ) in (_FrozenList, _FrozenTuple, _FrozenSet)
    container_id = id(value) if is_container else None
    if container_id is not None:
        if container_id in state.active_container_ids:
            if _strict:
                raise ValueError(f"Policy {_field_name} must not contain cycles.")
            return _FrozenOpaqueValue("snapshot_cycle")
        state.active_container_ids.add(container_id)
    try:
        return _freeze_value(
            value,
            state=state,
            depth=_depth,
            strict=_strict,
            field_name=_field_name,
        )
    finally:
        if container_id is not None:
            state.active_container_ids.remove(container_id)


def _freeze_value(
    value: Any,
    *,
    state: _SnapshotFreezeState,
    depth: int,
    strict: bool,
    field_name: str,
) -> Any:
    def freeze_items(items: Any) -> tuple[Any, ...]:
        frozen_items: list[Any] = []
        for item in items:
            if state.remaining_nodes <= 0:
                if strict:
                    raise ValueError(f"Policy {field_name} exceeds the snapshot node budget.")
                frozen_items.append(_FrozenOpaqueValue("snapshot_budget_limit"))
                break
            frozen_items.append(
                _freeze(
                    item,
                    _state=state,
                    _depth=depth + 1,
                    _strict=strict,
                    _field_name=field_name,
                )
            )
        return tuple(frozen_items)

    if type(value) is _FrozenList:
        return _FrozenList(freeze_items(value.items))
    if type(value) is _FrozenTuple:
        return _FrozenTuple(freeze_items(value.items))
    if type(value) is _FrozenSet:
        return _FrozenSet(
            freeze_items(value.items),
            was_frozenset=value.was_frozenset is True,
        )
    if type(value) is _FrozenOpaqueValue:
        return _FrozenOpaqueValue("opaque_value")
    if isinstance(value, Mapping):
        frozen: dict[Any, Any] = {}
        for key, item in value.items():
            if state.remaining_nodes < 2:
                if strict:
                    raise ValueError(f"Policy {field_name} exceeds the snapshot node budget.")
                frozen["__policy_snapshot_truncated__"] = _FrozenOpaqueValue(
                    "snapshot_budget_limit"
                )
                break
            frozen_key = _freeze(
                key,
                _state=state,
                _depth=depth + 1,
                _strict=strict,
                _field_name=field_name,
            )
            if isinstance(frozen_key, _FrozenOpaqueValue):
                raise TypeError("Policy snapshot mapping keys must use supported immutable types.")
            try:
                hash(frozen_key)
            except TypeError as exc:
                raise TypeError("Policy snapshot mapping keys must be hashable after freezing.") from exc
            frozen[frozen_key] = _freeze(
                item,
                _state=state,
                _depth=depth + 1,
                _strict=strict,
                _field_name=field_name,
            )
        return MappingProxyType(frozen)
    if isinstance(value, list):
        return _FrozenList(freeze_items(value))
    if isinstance(value, tuple):
        return _FrozenTuple(freeze_items(value))
    if isinstance(value, (set, frozenset)):
        return _FrozenSet(
            freeze_items(value),
            was_frozenset=isinstance(value, frozenset),
        )
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            frozen_datetime = value.replace(tzinfo=None)
        else:
            frozen_datetime = value.astimezone(UTC)
        return (
            frozen_datetime
            if _consume_snapshot_leaf(
                state,
                frozen_datetime,
                strict=strict,
                field_name=field_name,
            )
            else _FrozenOpaqueValue("snapshot_byte_budget_limit")
        )
    if value is None or type(value) in _IMMUTABLE_LEAF_TYPES:
        return (
            value
            if _consume_snapshot_leaf(
                state,
                value,
                strict=strict,
                field_name=field_name,
            )
            else _FrozenOpaqueValue("snapshot_byte_budget_limit")
        )
    return _FrozenOpaqueValue(_safe_type_name(value))


def _freeze_json_snapshot(
    value: Any,
    *,
    state: _SnapshotFreezeState,
    depth: int = 0,
    field_name: str,
) -> Any:
    if depth >= _MAX_SNAPSHOT_DEPTH:
        raise ValueError(f"Policy {field_name} exceeds the snapshot depth limit.")
    if not state.consume():
        raise ValueError(f"Policy {field_name} exceeds the snapshot node budget.")

    is_mapping = isinstance(value, Mapping)
    is_list = type(value) in (list, _FrozenList)
    container_id = id(value) if is_mapping or is_list else None
    if container_id is not None:
        if container_id in state.active_container_ids:
            raise ValueError(f"Policy {field_name} must not contain cycles.")
        state.active_container_ids.add(container_id)
    try:
        if is_mapping:
            frozen: dict[str, Any] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError(f"Policy {field_name} JSON object keys must be strings.")
                try:
                    encoded_key = key.encode("utf-8", errors="strict")
                except UnicodeEncodeError as exc:
                    raise ValueError(
                        f"Policy {field_name} JSON object keys must contain valid Unicode scalars."
                    ) from exc
                if not state.consume():
                    raise ValueError(f"Policy {field_name} exceeds the snapshot node budget.")
                if not state.consume_bytes(len(encoded_key)):
                    raise ValueError(
                        f"Policy {field_name} exceeds the snapshot leaf or shared byte budget."
                    )
                frozen[key] = _freeze_json_snapshot(
                    item,
                    state=state,
                    depth=depth + 1,
                    field_name=field_name,
                )
            return MappingProxyType(frozen)
        if is_list:
            items = value if type(value) is list else value.items
            return _FrozenList(
                tuple(
                    _freeze_json_snapshot(
                        item,
                        state=state,
                        depth=depth + 1,
                        field_name=field_name,
                    )
                    for item in items
                )
            )
        if value is None or type(value) in (bool, int):
            _consume_snapshot_leaf(
                state,
                value,
                strict=True,
                field_name=field_name,
            )
            return value
        if type(value) is str:
            try:
                encoded_value = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"Policy {field_name} JSON strings must contain valid Unicode scalars."
                ) from exc
            if not state.consume_bytes(len(encoded_value)):
                raise ValueError(
                    f"Policy {field_name} exceeds the snapshot leaf or shared byte budget."
                )
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError(f"Policy {field_name} JSON numbers must be finite.")
            _consume_snapshot_leaf(
                state,
                value,
                strict=True,
                field_name=field_name,
            )
            return value
        raise TypeError(
            f"Policy {field_name} must contain only canonical JSON value types."
        )
    finally:
        if container_id is not None:
            state.active_container_ids.remove(container_id)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {_thaw(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, _FrozenList):
        return [_thaw(item) for item in value.items]
    if isinstance(value, _FrozenTuple):
        return tuple(_thaw(item) for item in value.items)
    if isinstance(value, _FrozenSet):
        items = (_thaw(item) for item in value.items)
        return frozenset(items) if value.was_frozenset else set(items)
    if isinstance(value, _FrozenOpaqueValue):
        return _FrozenOpaqueValue(value.type_name)
    if isinstance(value, frozenset):
        return {_thaw(item) for item in value}
    return deepcopy(value)


def _identity_projection(value: Any) -> Any:
    if isinstance(value, Mapping):
        entries = [
            [_identity_projection(key), _identity_projection(item)]
            for key, item in value.items()
        ]
        entries.sort(
            key=lambda entry: json.dumps(
                entry[0], ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
        )
        return {"type": "mapping", "items": entries}
    if isinstance(value, _FrozenList):
        return {"type": "list", "items": [_identity_projection(item) for item in value.items]}
    if isinstance(value, _FrozenTuple):
        return {"type": "tuple", "items": [_identity_projection(item) for item in value.items]}
    if isinstance(value, _FrozenSet):
        items = [_identity_projection(item) for item in value.items]
        items.sort(
            key=lambda item: json.dumps(
                item, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
        )
        return {
            "type": "frozenset" if value.was_frozenset else "set",
            "items": items,
        }
    if isinstance(value, _FrozenOpaqueValue):
        return {"type": "opaque", "name": value.type_name}
    if type(value) is datetime:
        return {"type": "datetime", "value": value.isoformat()}
    if type(value) is _CONCRETE_PATH_TYPE:
        return {"type": "path", "value": str(value)}
    if type(value) is bytes:
        return {"type": "bytes", "value": value.hex()}
    if value is None:
        return {"type": "none"}
    if type(value) is bool:
        return {"type": "bool", "value": value}
    if type(value) is int:
        return {"type": "int", "value": str(value)}
    if type(value) is float:
        return {"type": "float", "value": value.hex()}
    if type(value) is str:
        return {"type": "str", "value": value}
    raise TypeError(f"Unsupported frozen policy identity value: {_safe_type_name(value)}")


def _validate_identity_snapshot(
    value: Any,
    *,
    field_name: str,
    _state: _IdentityValidationState | None = None,
    _depth: int = 0,
) -> None:
    if _depth >= _MAX_SNAPSHOT_DEPTH:
        raise ValueError(f"Policy {field_name} exceeds the identity depth limit.")
    state = _state or _IdentityValidationState()
    state.consume(field_name=field_name)
    if isinstance(value, _FrozenOpaqueValue):
        raise TypeError(f"Policy {field_name} contains an unsupported leaf type.")
    if isinstance(value, (_FrozenList, _FrozenTuple, _FrozenSet)):
        for item in value.items:
            _validate_identity_snapshot(
                item,
                field_name=field_name,
                _state=state,
                _depth=_depth + 1,
            )
        return
    if value is None or type(value) in _IMMUTABLE_LEAF_TYPES:
        return
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in state.active_container_ids:
            raise ValueError(f"Policy {field_name} must not contain cycles.")
        state.active_container_ids.add(container_id)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"Policy {field_name} keys must be strings.")
                _validate_identity_snapshot(
                    key,
                    field_name=field_name,
                    _state=state,
                    _depth=_depth + 1,
                )
                _validate_identity_snapshot(
                    item,
                    field_name=field_name,
                    _state=state,
                    _depth=_depth + 1,
                )
        finally:
            state.active_container_ids.remove(container_id)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        container_id = id(value)
        if container_id in state.active_container_ids:
            raise ValueError(f"Policy {field_name} must not contain cycles.")
        state.active_container_ids.add(container_id)
        try:
            for item in value:
                _validate_identity_snapshot(
                    item,
                    field_name=field_name,
                    _state=state,
                    _depth=_depth + 1,
                )
        finally:
            state.active_container_ids.remove(container_id)
        return
    raise TypeError(
        f"Policy {field_name} contains unsupported leaf type {_safe_type_name(value)}."
    )


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    compact = normalized.replace("_", "")
    return any(
        part in normalized or part.replace("_", "") in compact
        for part in _SENSITIVE_KEY_PARTS
    )


def _bounded_key(key: str, *, index: int) -> str:
    if key in _SAFE_EVIDENCE_KEYS:
        return key
    prefix = "_redacted_key" if _is_sensitive_key(key) else "_field"
    return f"{prefix}_{index:02d}"


def _safe_evidence_text(key: str | None, value: str) -> bool:
    if key is None:
        return False
    normalized = key.lower()
    if normalized.endswith(_SAFE_EVIDENCE_TEXT_SUFFIXES):
        return bool(_HEX_DIGEST_PATTERN.fullmatch(value))
    return normalized in _SAFE_EVIDENCE_TEXT_KEYS and bool(
        _SAFE_EVIDENCE_TEXT_PATTERN.fullmatch(value)
    )


def _bounded_text(value: str, *, parent_key: str | None) -> str:
    if not _safe_evidence_text(parent_key, value):
        return "<redacted-text>"
    if len(value) <= _MAX_STRING_LENGTH:
        return value
    return f"{value[:_MAX_STRING_LENGTH]}<truncated:{len(value) - _MAX_STRING_LENGTH}>"


def _record_message(value: Any) -> str:
    return "<redacted-message>"


def _require_bounded_policy_message(value: Any, *, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"Policy {field_name} must be a string.")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"Policy {field_name} must contain valid Unicode scalars."
        ) from exc
    if len(encoded) > _MAX_POLICY_MESSAGE_BYTES:
        raise ValueError(
            f"Policy {field_name} must not exceed "
            f"{_MAX_POLICY_MESSAGE_BYTES} UTF-8 bytes."
        )
    return value


def _record_identifier(value: Any, *, limit: int = 128) -> str:
    if type(value) is not str:
        return "<invalid-identifier>"
    try:
        valid_text = bool(value) and len(value) <= limit
        if valid_text:
            value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        valid_text = False
    if valid_text:
        return value
    return "<redacted-identifier>"


def _record_reason_code(value: Any) -> str:
    if type(value) is str and _REASON_CODE_PATTERN.fullmatch(value):
        return value
    return "POLICY_RECORD_FIELD_INVALID"


def _record_digest(value: Any) -> str:
    if type(value) is str and _HEX_DIGEST_PATTERN.fullmatch(value):
        return value
    return ""


def _record_phase(value: Any) -> str:
    return value if type(value) is str and value in POLICY_PHASE_STAGE_ORDERS else "invalid"


def _record_boolean_pair(allowed: Any, stop_required: Any) -> tuple[bool, bool]:
    if (
        type(allowed) is bool
        and type(stop_required) is bool
        and allowed != stop_required
    ):
        return allowed, stop_required
    return False, True


def _bounded_value(
    value: Any,
    *,
    depth: int = 0,
    parent_key: str | None = None,
    key_path: tuple[str, ...] = (),
    budget: _EvidenceBudget | None = None,
) -> Any:
    budget = budget or _EvidenceBudget()
    if not budget.consume():
        return "<global-evidence-budget>"
    if depth >= _MAX_EVIDENCE_DEPTH:
        return "<depth-limit>"
    if isinstance(value, Mapping):
        bounded: dict[str, Any] = {}
        sampled_items = list(islice(value.items(), _MAX_EVIDENCE_ITEMS + 1))
        items = sorted(
            (
                (str(key), item)
                for key, item in sampled_items[:_MAX_EVIDENCE_ITEMS]
            ),
            key=lambda pair: pair[0],
        )
        for index, (raw_key, item) in enumerate(items, start=1):
            if budget.remaining_nodes <= 0:
                budget.truncated = True
                bounded["_global_budget_truncated"] = True
                break
            key = _bounded_key(raw_key, index=index)
            item_key_path = (*key_path, raw_key)
            if _is_sensitive_key("_".join(item_key_path)):
                budget.consume()
                bounded[key] = "<redacted>"
            else:
                bounded[key] = _bounded_value(
                    item,
                    depth=depth + 1,
                    parent_key=raw_key,
                    key_path=item_key_path,
                    budget=budget,
                )
        if len(sampled_items) > _MAX_EVIDENCE_ITEMS:
            bounded["_truncated_items"] = 1
        return bounded
    if isinstance(value, (_FrozenList, _FrozenTuple, _FrozenSet)):
        items = list(value.items)
        if isinstance(value, _FrozenSet):
            items.sort(
                key=lambda item: json.dumps(
                    _identity_projection(item),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        bounded_items: list[Any] = []
        for item in items[:_MAX_SEQUENCE_ITEMS]:
            if budget.remaining_nodes <= 0:
                budget.truncated = True
                bounded_items.append("<global-evidence-budget>")
                break
            bounded_items.append(
                _bounded_value(
                    item,
                    depth=depth + 1,
                    parent_key=parent_key,
                    key_path=key_path,
                    budget=budget,
                )
            )
        if len(items) > _MAX_SEQUENCE_ITEMS:
            bounded_items.append(f"<truncated:{len(items) - _MAX_SEQUENCE_ITEMS}>")
        return bounded_items
    if isinstance(value, _FrozenOpaqueValue):
        return "<opaque-value>"
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=None).isoformat()
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Path):
        return _bounded_text(str(value), parent_key=parent_key)
    if isinstance(value, Enum):
        return _bounded_value(
            value.value,
            depth=depth,
            parent_key=parent_key,
            key_path=key_path,
            budget=budget,
        )
    if isinstance(value, str):
        return _bounded_text(value, parent_key=parent_key)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return "<redacted-number>"
    return f"<{_safe_type_name(value)}>"


def _record_json_bytes(record: Mapping[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=True, sort_keys=True).encode("utf-8")


def _require_aware_datetime(value: datetime) -> datetime:
    if type(value) is not datetime:
        raise TypeError("Policy timestamps must use the exact datetime type.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Policy timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _require_context_identifier(value: Any, *, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"Policy {field_name} must be a string.")
    if len(value) > 128:
        raise ValueError(f"Policy {field_name} must not exceed 128 characters.")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"Policy {field_name} must contain valid Unicode scalars."
        ) from exc
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"Policy {field_name} must be non-empty.")
    return normalized


def _consume_capability_node(
    state: _SnapshotFreezeState,
    *,
    field_name: str,
) -> None:
    if not state.consume():
        raise ValueError(f"Capability policy {field_name} exceeds the snapshot node budget.")


def _consume_capability_leaf(
    state: _SnapshotFreezeState,
    value: Any,
    *,
    field_name: str,
) -> None:
    _consume_capability_node(state, field_name=field_name)
    if not state.consume_bytes(_snapshot_leaf_size(value)):
        raise ValueError(
            f"Capability policy {field_name} exceeds the snapshot leaf or shared byte budget."
        )


def _require_capability_text(
    value: Any,
    *,
    field_name: str,
    state: _SnapshotFreezeState,
) -> str:
    if type(value) is not str:
        raise TypeError(f"Capability policy {field_name} must be a string.")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"Capability policy {field_name} must contain valid Unicode scalars."
        ) from exc
    _consume_capability_leaf(state, value, field_name=field_name)
    return value


def _require_capability_text_tuple(
    value: Any,
    *,
    field_name: str,
    state: _SnapshotFreezeState,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"Capability policy {field_name} must be a tuple of strings.")
    _consume_capability_node(state, field_name=field_name)
    return tuple(
        _require_capability_text(
            item,
            field_name=f"{field_name} item",
            state=state,
        )
        for item in value
    )


def _require_capability_profile_set(
    value: Any,
    *,
    state: _SnapshotFreezeState,
) -> frozenset[str]:
    if type(value) is not frozenset:
        raise TypeError("Capability policy profiles must be a frozenset of strings.")
    _consume_capability_node(state, field_name="profiles")
    return frozenset(
        _require_capability_text(
            item,
            field_name="profiles item",
            state=state,
        )
        for item in value
    )


def _validate_capability_policy_invariants(
    capability: CapabilityPolicySnapshot,
) -> None:
    """Validate the closed CapabilitySpec v1 cross-field safety ABI.

    This rejects internally contradictory metadata. Canonical registry provenance is
    a separate service-boundary requirement for authoritative integration.
    """

    boolean_signature = (
        capability.read_only,
        capability.mutation,
        capability.destructive,
        capability.hardware,
    )
    if boolean_signature not in _CAPABILITY_RISK_BOOLEAN_SIGNATURES[
        capability.risk_class
    ]:
        raise ValueError("Capability policy risk boolean invariant is inconsistent.")
    if capability.idempotent is not capability.read_only:
        raise ValueError("Capability policy idempotency invariant is inconsistent.")
    if capability.profiles not in _CAPABILITY_PROFILE_SETS:
        raise ValueError("Capability policy profile invariant is inconsistent.")

    state_signature = (
        capability.required_session_state,
        capability.required_project_state,
    )
    if state_signature not in _CAPABILITY_RISK_STATE_SIGNATURES[
        capability.risk_class
    ]:
        raise ValueError("Capability policy risk state invariant is inconsistent.")
    if capability.hardware_tier not in _CAPABILITY_RISK_HARDWARE_TIERS[
        capability.risk_class
    ]:
        raise ValueError(
            "Capability policy risk hardware tier invariant is inconsistent."
        )
    if capability.hardware is not (capability.hardware_tier != "not_hardware"):
        raise ValueError("Capability policy hardware flag invariant is inconsistent.")

    if capability.hardware_tier != "not_hardware":
        tier_signature = (
            capability.read_only,
            capability.mutation,
            capability.destructive,
            capability.required_session_state,
            capability.required_project_state,
            capability.dispatch_lane,
        )
        if tier_signature not in _CAPABILITY_HARDWARE_TIER_SIGNATURES[
            capability.hardware_tier
        ]:
            raise ValueError("Capability policy hardware tier invariant is inconsistent.")
        if capability.profiles != frozenset({"all"}) or capability.task_eligible:
            raise ValueError("Capability policy hardware profile invariant is inconsistent.")

    if capability.execution_input_policy == "blocks_unattested_composite":
        composite_signature = (
            capability.risk_class,
            capability.required_project_state,
        )
        if (
            composite_signature not in _COMPOSITE_INPUT_SIGNATURES
            or capability.required_session_state != "managed_session"
            or capability.dispatch_lane != "serialized_backend"
            or capability.hardware
            or capability.destructive
            or not capability.mutation
        ):
            raise ValueError(
                "Capability policy composite input invariant is inconsistent."
            )

    if capability.dispatch_lane == "local":
        local_signature = (
            capability.risk_class,
            capability.hardware_tier,
            capability.read_only,
            capability.mutation,
            capability.destructive,
            capability.hardware,
            capability.required_session_state,
            capability.required_project_state,
            capability.execution_input_policy,
        )
        if local_signature != (
            "normal",
            "not_hardware",
            True,
            False,
            False,
            False,
            "none",
            "none",
            "typed_tool_policy",
        ):
            raise ValueError("Capability policy local dispatch invariant is inconsistent.")

    if capability.task_eligible and (
        capability.duration_class != "long" or capability.hardware
    ):
        raise ValueError("Capability policy task eligibility invariant is inconsistent.")


@dataclass(frozen=True)
class CapabilityPolicySnapshot:
    """Deeply immutable policy projection detached from the global registry."""

    name: str
    domain: str
    handler: str
    description: str
    _input_schema_snapshot: Any = field(repr=False)
    _output_schema_snapshot: Any = field(repr=False)
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
    hardware_tier: str

    def __post_init__(self) -> None:
        snapshot_state = _SnapshotFreezeState()
        _consume_capability_node(snapshot_state, field_name="capability")
        for field_name in (
            "name",
            "domain",
            "handler",
            "description",
            "risk_class",
            "required_session_state",
            "required_project_state",
            "duration_class",
            "dispatch_lane",
            "execution_input_policy",
            "hardware_tier",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_capability_text(
                    getattr(self, field_name),
                    field_name=field_name,
                    state=snapshot_state,
                ),
            )
        for field_name in ("name", "domain", "handler"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"Capability policy {field_name} must be non-empty.")
        for field_name, allowed_values in _CAPABILITY_ENUM_VALUES.items():
            if getattr(self, field_name) not in allowed_values:
                raise ValueError(
                    f"Capability policy {field_name} uses an unknown classification."
                )
        for field_name in (
            "read_only",
            "mutation",
            "destructive",
            "hardware",
            "idempotent",
            "task_eligible",
            "open_world",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"Capability policy {field_name} must be a boolean.")
            _consume_capability_leaf(
                snapshot_state,
                getattr(self, field_name),
                field_name=field_name,
            )
        if not isinstance(self._input_schema_snapshot, Mapping):
            raise TypeError("Capability policy input schema must be a mapping.")
        if not isinstance(self._output_schema_snapshot, Mapping):
            raise TypeError("Capability policy output schema must be a mapping.")
        input_schema_snapshot = _freeze_json_snapshot(
            self._input_schema_snapshot,
            state=snapshot_state,
            field_name="capability_input_schema",
        )
        output_schema_snapshot = _freeze_json_snapshot(
            self._output_schema_snapshot,
            state=snapshot_state,
            field_name="capability_output_schema",
        )
        object.__setattr__(self, "_input_schema_snapshot", input_schema_snapshot)
        object.__setattr__(self, "_output_schema_snapshot", output_schema_snapshot)
        object.__setattr__(
            self,
            "profiles",
            _require_capability_profile_set(
                self.profiles,
                state=snapshot_state,
            ),
        )
        object.__setattr__(
            self,
            "workflow_tags",
            _require_capability_text_tuple(
                self.workflow_tags,
                field_name="workflow_tags",
                state=snapshot_state,
            ),
        )
        object.__setattr__(
            self,
            "supported_vivado_versions",
            _require_capability_text_tuple(
                self.supported_vivado_versions,
                field_name="supported_vivado_versions",
                state=snapshot_state,
            ),
        )
        object.__setattr__(
            self,
            "qualified_vivado_versions",
            _require_capability_text_tuple(
                self.qualified_vivado_versions,
                field_name="qualified_vivado_versions",
                state=snapshot_state,
            ),
        )
        object.__setattr__(
            self,
            "evidence_contract",
            _require_capability_text_tuple(
                self.evidence_contract,
                field_name="evidence_contract",
                state=snapshot_state,
            ),
        )
        object.__setattr__(
            self,
            "artifact_contract",
            _require_capability_text_tuple(
                self.artifact_contract,
                field_name="artifact_contract",
                state=snapshot_state,
            ),
        )
        _validate_capability_policy_invariants(self)

    @classmethod
    def from_spec(cls, capability: CapabilitySpec) -> CapabilityPolicySnapshot:
        return cls(
            name=capability.name,
            domain=capability.domain,
            handler=capability.handler,
            description=capability.description,
            _input_schema_snapshot=capability.input_schema,
            _output_schema_snapshot=capability.output_schema,
            risk_class=capability.risk_class,
            profiles=capability.profiles,
            workflow_tags=capability.workflow_tags,
            required_session_state=capability.required_session_state,
            required_project_state=capability.required_project_state,
            read_only=capability.read_only,
            mutation=capability.mutation,
            destructive=capability.destructive,
            hardware=capability.hardware,
            idempotent=capability.idempotent,
            duration_class=capability.duration_class,
            supported_vivado_versions=capability.supported_vivado_versions,
            qualified_vivado_versions=capability.qualified_vivado_versions,
            task_eligible=capability.task_eligible,
            open_world=capability.open_world,
            dispatch_lane=capability.dispatch_lane,
            execution_input_policy=capability.execution_input_policy,
            evidence_contract=capability.evidence_contract,
            artifact_contract=capability.artifact_contract,
            hardware_tier=capability.hardware_tier,
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return _thaw(self._input_schema_snapshot)

    @property
    def output_schema(self) -> dict[str, Any]:
        return _thaw(self._output_schema_snapshot)

    @property
    def risk(self) -> str:
        return self.risk_class

    @property
    def policy_identity_sha256(self) -> str:
        payload = {
            "name": self.name,
            "domain": self.domain,
            "handler": self.handler,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "risk_class": self.risk_class,
            "profiles": sorted(self.profiles),
            "workflow_tags": list(self.workflow_tags),
            "required_session_state": self.required_session_state,
            "required_project_state": self.required_project_state,
            "read_only": self.read_only,
            "mutation": self.mutation,
            "destructive": self.destructive,
            "hardware": self.hardware,
            "idempotent": self.idempotent,
            "duration_class": self.duration_class,
            "supported_vivado_versions": list(self.supported_vivado_versions),
            "qualified_vivado_versions": list(self.qualified_vivado_versions),
            "task_eligible": self.task_eligible,
            "open_world": self.open_world,
            "dispatch_lane": self.dispatch_lane,
            "execution_input_policy": self.execution_input_policy,
            "evidence_contract": list(self.evidence_contract),
            "artifact_contract": list(self.artifact_contract),
            "hardware_tier": self.hardware_tier,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()


CapabilityPolicySource = CapabilitySpec | CapabilityPolicySnapshot


def _snapshot_capability(capability: CapabilityPolicySource) -> CapabilityPolicySnapshot:
    if type(capability) is CapabilityPolicySnapshot:
        return CapabilityPolicySnapshot(
            name=object.__getattribute__(capability, "name"),
            domain=object.__getattribute__(capability, "domain"),
            handler=object.__getattribute__(capability, "handler"),
            description=object.__getattribute__(capability, "description"),
            _input_schema_snapshot=object.__getattribute__(
                capability,
                "_input_schema_snapshot",
            ),
            _output_schema_snapshot=object.__getattribute__(
                capability,
                "_output_schema_snapshot",
            ),
            risk_class=object.__getattribute__(capability, "risk_class"),
            profiles=object.__getattribute__(capability, "profiles"),
            workflow_tags=object.__getattribute__(capability, "workflow_tags"),
            required_session_state=object.__getattribute__(
                capability,
                "required_session_state",
            ),
            required_project_state=object.__getattribute__(
                capability,
                "required_project_state",
            ),
            read_only=object.__getattribute__(capability, "read_only"),
            mutation=object.__getattribute__(capability, "mutation"),
            destructive=object.__getattribute__(capability, "destructive"),
            hardware=object.__getattribute__(capability, "hardware"),
            idempotent=object.__getattribute__(capability, "idempotent"),
            duration_class=object.__getattribute__(capability, "duration_class"),
            supported_vivado_versions=object.__getattribute__(
                capability,
                "supported_vivado_versions",
            ),
            qualified_vivado_versions=object.__getattribute__(
                capability,
                "qualified_vivado_versions",
            ),
            task_eligible=object.__getattribute__(capability, "task_eligible"),
            open_world=object.__getattribute__(capability, "open_world"),
            dispatch_lane=object.__getattribute__(capability, "dispatch_lane"),
            execution_input_policy=object.__getattribute__(
                capability,
                "execution_input_policy",
            ),
            evidence_contract=object.__getattribute__(capability, "evidence_contract"),
            artifact_contract=object.__getattribute__(capability, "artifact_contract"),
            hardware_tier=object.__getattribute__(capability, "hardware_tier"),
        )
    if type(capability) is CapabilitySpec:
        return CapabilityPolicySnapshot.from_spec(capability)
    raise TypeError("Policy capability must be a CapabilitySpec or CapabilityPolicySnapshot.")


@dataclass(frozen=True)
class PolicyContext:
    """Immutable request snapshot consumed by pure policy stages."""

    capability: CapabilityPolicySnapshot
    _arguments_snapshot: Any = field(repr=False)
    active_profile: str
    profile_enforced: bool
    caller_identity: Mapping[str, Any] | None
    session_identity: Mapping[str, Any] | None
    project_capability: Mapping[str, Any] | None
    trusted_vivado_identity: Mapping[str, Any] | None
    request_id: str
    started_at: datetime

    def __post_init__(self) -> None:
        normalized_profile = _require_context_identifier(
            self.active_profile,
            field_name="active_profile",
        )
        if type(self.profile_enforced) is not bool:
            raise TypeError("Policy profile_enforced must be a boolean.")
        normalized_request_id = _require_context_identifier(
            self.request_id,
            field_name="request_id",
        )
        normalized_started_at = _require_aware_datetime(self.started_at)
        snapshot_state = _SnapshotFreezeState()
        identity_validation_state = _IdentityValidationState()
        if not snapshot_state.consume():
            raise ValueError("Policy context exceeds the snapshot node budget.")
        for field_name, value in (
            ("active_profile", normalized_profile),
            ("profile_enforced", self.profile_enforced),
            ("request_id", normalized_request_id),
            ("started_at", normalized_started_at),
        ):
            if not snapshot_state.consume():
                raise ValueError("Policy context exceeds the snapshot node budget.")
            _consume_snapshot_leaf(
                snapshot_state,
                value,
                strict=True,
                field_name=field_name,
            )
        capability_snapshot = _snapshot_capability(self.capability)
        arguments_snapshot = _freeze(
            self._arguments_snapshot,
            _state=snapshot_state,
            _strict=True,
            _field_name="arguments",
        )
        for field_name in (
            "caller_identity",
            "session_identity",
            "project_capability",
            "trusted_vivado_identity",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, Mapping):
                raise TypeError(f"Policy {field_name} must be a mapping or None.")
            if value is not None:
                frozen_value = _freeze(
                    value,
                    _state=snapshot_state,
                    _strict=True,
                    _field_name=field_name,
                )
                _validate_identity_snapshot(
                    frozen_value,
                    field_name=field_name,
                    _state=identity_validation_state,
                )
            else:
                frozen_value = None
            object.__setattr__(self, field_name, frozen_value)
        object.__setattr__(self, "capability", capability_snapshot)
        object.__setattr__(self, "_arguments_snapshot", arguments_snapshot)
        object.__setattr__(self, "active_profile", normalized_profile)
        object.__setattr__(self, "request_id", normalized_request_id)
        object.__setattr__(self, "started_at", normalized_started_at)

    @classmethod
    def create(
        cls,
        *,
        capability: CapabilityPolicySource,
        arguments: Any,
        active_profile: str,
        profile_enforced: bool,
        caller_identity: Mapping[str, Any] | None,
        session_identity: Mapping[str, Any] | None,
        project_capability: Mapping[str, Any] | None,
        trusted_vivado_identity: Mapping[str, Any] | None,
        request_id: str,
        started_at: datetime,
    ) -> PolicyContext:
        return cls(
            capability=_snapshot_capability(capability),
            _arguments_snapshot=arguments,
            active_profile=active_profile,
            profile_enforced=profile_enforced,
            caller_identity=caller_identity,
            session_identity=session_identity,
            project_capability=project_capability,
            trusted_vivado_identity=trusted_vivado_identity,
            request_id=request_id,
            started_at=started_at,
        )

    @property
    def capability_name(self) -> str:
        return self.capability.name

    @property
    def policy_identity_sha256(self) -> str:
        payload = {
            "capability_policy_sha256": self.capability.policy_identity_sha256,
            "arguments": _identity_projection(self._arguments_snapshot),
            "active_profile": self.active_profile,
            "profile_enforced": self.profile_enforced,
            "caller_identity": _identity_projection(self.caller_identity),
            "session_identity": _identity_projection(self.session_identity),
            "project_capability": _identity_projection(self.project_capability),
            "trusted_vivado_identity": _identity_projection(self.trusted_vivado_identity),
            "request_id": self.request_id,
            "started_at": self.started_at.isoformat(),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    @property
    def arguments(self) -> Any:
        """Return a detached mutable projection with original JSON container types."""

        return _thaw(self._arguments_snapshot)


@dataclass(frozen=True)
class PolicyStageResult:
    """One stage outcome; ALLOW means continue, not execute."""

    stage: str
    allowed: bool
    reason_code: str
    message: str
    evidence: Mapping[str, Any]
    stop_required: bool = False
    applicable: bool = True

    def __post_init__(self) -> None:
        if type(self.stage) is not str:
            raise TypeError("Policy stage result stage must be a string.")
        if type(self.reason_code) is not str:
            raise TypeError("Policy stage result reason_code must be a string.")
        _require_bounded_policy_message(
            self.message,
            field_name="stage result message",
        )
        if not isinstance(self.evidence, Mapping):
            raise TypeError("Policy stage result evidence must be a mapping.")
        if not self.stage.strip():
            raise ValueError("Policy stage result requires a stage name.")
        if len(self.stage) > 128:
            raise ValueError("Policy stage result stage name must not exceed 128 characters.")
        try:
            self.stage.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "Policy stage result stage name must contain valid Unicode scalars."
            ) from exc
        if not _REASON_CODE_PATTERN.fullmatch(self.reason_code):
            raise ValueError("Policy stage result requires a stable uppercase reason_code.")
        if type(self.allowed) is not bool:
            raise TypeError("Policy stage allowed must be a boolean.")
        if type(self.stop_required) is not bool:
            raise TypeError("Policy stage stop_required must be a boolean.")
        if type(self.applicable) is not bool:
            raise TypeError("Policy stage applicable must be a boolean.")
        if self.allowed == self.stop_required:
            raise ValueError("Policy stage allowed and stop_required values are inconsistent.")
        object.__setattr__(self, "evidence", _freeze(self.evidence))

    def to_record(self, *, _budget: _EvidenceBudget | None = None) -> dict[str, Any]:
        budget = _budget or _EvidenceBudget()
        allowed, stop_required = _record_boolean_pair(
            getattr(self, "allowed", None),
            getattr(self, "stop_required", None),
        )
        evidence = getattr(self, "evidence", None)
        try:
            bounded_evidence = (
                _bounded_value(evidence, budget=budget)
                if isinstance(evidence, Mapping)
                else {"record_field_invalid": True}
            )
        except Exception:
            bounded_evidence = {"record_field_invalid": True}
        return {
            "stage": _record_identifier(getattr(self, "stage", None)),
            "allowed": allowed,
            "reason_code": _record_reason_code(getattr(self, "reason_code", None)),
            "message": _record_message(getattr(self, "message", None)),
            "evidence": bounded_evidence,
            "stop_required": stop_required,
            "applicable": (
                getattr(self, "applicable", False)
                if type(getattr(self, "applicable", None)) is bool
                else False
            ),
        }


def _validated_stage_result_copy(value: Any) -> PolicyStageResult:
    if type(value) is not PolicyStageResult:
        raise TypeError("Policy stage result must use the exact PolicyStageResult type.")
    try:
        return PolicyStageResult(
            stage=object.__getattribute__(value, "stage"),
            allowed=object.__getattribute__(value, "allowed"),
            reason_code=object.__getattribute__(value, "reason_code"),
            message=object.__getattribute__(value, "message"),
            evidence=object.__getattribute__(value, "evidence"),
            stop_required=object.__getattribute__(value, "stop_required"),
            applicable=object.__getattribute__(value, "applicable"),
        )
    except Exception as exc:
        raise TypeError("Policy stage returned an invalid canonical result.") from exc


@dataclass(frozen=True)
class PolicyDecision:
    """Final fail-closed decision plus its ordered stage audit record."""

    capability_name: str
    allowed: bool
    stage: str
    reason_code: str
    message: str
    evidence: Mapping[str, Any]
    stop_required: bool
    stage_results: tuple[PolicyStageResult, ...] = ()
    request_id: str = ""
    started_at: datetime | None = None
    capability_policy_sha256: str = ""
    context_identity_sha256: str = ""
    phase: str = "pre_execution"
    mode: str = "foundation"
    _provenance: _PreExecutionAllowGrant | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        for field_name in (
            "capability_name",
            "stage",
            "reason_code",
            "message",
            "request_id",
            "capability_policy_sha256",
            "context_identity_sha256",
            "phase",
            "mode",
        ):
            if type(getattr(self, field_name)) is not str:
                raise TypeError(f"Policy decision {field_name} must be a string.")
        _require_bounded_policy_message(
            self.message,
            field_name="decision message",
        )
        if not isinstance(self.evidence, Mapping):
            raise TypeError("Policy decision evidence must be a mapping.")
        if not self.capability_name.strip():
            raise ValueError("Policy decision requires a capability name.")
        if not self.stage.strip():
            raise ValueError("Policy decision requires a stage name.")
        if not _REASON_CODE_PATTERN.fullmatch(self.reason_code):
            raise ValueError("Policy decision requires a stable uppercase reason_code.")
        if type(self.allowed) is not bool:
            raise TypeError("Policy decision allowed must be a boolean.")
        if type(self.stop_required) is not bool:
            raise TypeError("Policy decision stop_required must be a boolean.")
        if self.allowed == self.stop_required:
            raise ValueError("Policy decision allowed and stop_required values are inconsistent.")
        if self.phase not in POLICY_PHASE_STAGE_ORDERS:
            raise ValueError("Policy decision requires a known pipeline phase.")
        if self.started_at is not None:
            object.__setattr__(self, "started_at", _require_aware_datetime(self.started_at))
        if self.capability_policy_sha256 and not _HEX_DIGEST_PATTERN.fullmatch(
            self.capability_policy_sha256
        ):
            raise ValueError("Policy decision capability_policy_sha256 must be a hexadecimal digest.")
        if self.context_identity_sha256 and not _HEX_DIGEST_PATTERN.fullmatch(
            self.context_identity_sha256
        ):
            raise ValueError("Policy decision context_identity_sha256 must be a hexadecimal digest.")
        object.__setattr__(self, "evidence", _freeze(self.evidence))
        stage_results = tuple(self.stage_results)
        if len(stage_results) > len(POLICY_STAGE_ORDER):
            raise ValueError("Policy decision contains too many stage results.")
        object.__setattr__(
            self,
            "stage_results",
            tuple(_validated_stage_result_copy(result) for result in stage_results),
        )

    def to_record(self) -> dict[str, Any]:
        budget = _EvidenceBudget()
        allowed, stop_required = _record_boolean_pair(
            getattr(self, "allowed", None),
            getattr(self, "stop_required", None),
        )
        observed_evidence = getattr(self, "evidence", None)
        try:
            bounded_evidence = (
                _bounded_value(observed_evidence, budget=budget)
                if isinstance(observed_evidence, Mapping)
                else {"record_field_invalid": True}
            )
        except Exception:
            bounded_evidence = {"record_field_invalid": True}
        observed_stage_results = getattr(self, "stage_results", ())
        if type(observed_stage_results) is not tuple:
            observed_stage_results = ()
        stage_records: list[dict[str, Any]] = []
        for result in observed_stage_results[: len(POLICY_STAGE_ORDER)]:
            try:
                if type(result) is not PolicyStageResult:
                    raise TypeError("invalid stage result type")
                stage_records.append(result.to_record(_budget=budget))
            except Exception:
                stage_records.append(
                    {
                        "stage": "<invalid-identifier>",
                        "allowed": False,
                        "reason_code": "POLICY_RECORD_FIELD_INVALID",
                        "message": "<redacted-message>",
                        "evidence": {"record_field_invalid": True},
                        "stop_required": True,
                        "applicable": False,
                    }
                )
        capability_policy_sha256 = _record_digest(
            getattr(self, "capability_policy_sha256", None)
        )
        context_identity_sha256 = _record_digest(
            getattr(self, "context_identity_sha256", None)
        )
        record: dict[str, Any] = {
            "schema_version": POLICY_DECISION_SCHEMA_VERSION,
            "capability": _record_identifier(getattr(self, "capability_name", None)),
            "capability_policy_sha256": capability_policy_sha256,
            "context_identity_bound": bool(context_identity_sha256),
            "mode": _record_identifier(getattr(self, "mode", None), limit=64),
            "phase": _record_phase(getattr(self, "phase", None)),
            "allowed": allowed,
            "stage": _record_identifier(getattr(self, "stage", None)),
            "reason_code": _record_reason_code(getattr(self, "reason_code", None)),
            "message": _record_message(getattr(self, "message", None)),
            "evidence": bounded_evidence,
            "stop_required": stop_required,
            "stage_results": stage_records,
        }
        if budget.truncated:
            record["evidence_budget_truncated"] = True
        request_id = getattr(self, "request_id", None)
        if type(request_id) is str and request_id:
            record["request_id"] = _record_identifier(request_id)
        started_at = getattr(self, "started_at", None)
        if type(started_at) is datetime:
            try:
                if started_at.tzinfo is not None and started_at.utcoffset() is not None:
                    record["started_at"] = started_at.astimezone(UTC).isoformat()
            except Exception:
                pass
        serialized = _record_json_bytes(record)
        if len(serialized) <= POLICY_DECISION_MAX_BYTES:
            return record

        summary = {
            "summary_truncated": True,
            "full_record_sha256": hashlib.sha256(serialized).hexdigest(),
            "full_record_size": len(serialized),
        }
        record["message"] = "<omitted-by-record-budget>"
        record["evidence"] = summary
        record["stage_results"] = [
            {
                "stage": result["stage"],
                "allowed": result["allowed"],
                "reason_code": result["reason_code"],
                "message": "<omitted-by-record-budget>",
                "evidence": {"summary_truncated": True},
                "stop_required": result["stop_required"],
                "applicable": result["applicable"],
            }
            for result in record["stage_results"][: len(POLICY_STAGE_ORDER)]
        ]
        if len(_record_json_bytes(record)) <= POLICY_DECISION_MAX_BYTES:
            return record

        record["stage_results"] = []
        if len(_record_json_bytes(record)) > POLICY_DECISION_MAX_BYTES:
            return {
                "schema_version": POLICY_DECISION_SCHEMA_VERSION,
                "capability": "<redacted-identifier>",
                "capability_policy_sha256": "",
                "context_identity_bound": False,
                "mode": "foundation",
                "phase": "invalid",
                "allowed": False,
                "stage": "policy_pipeline",
                "reason_code": "POLICY_RECORD_BUDGET_FALLBACK",
                "message": "<omitted-by-record-budget>",
                "evidence": {"summary_truncated": True},
                "stop_required": True,
                "stage_results": [],
            }
        return record

    def __copy__(self) -> PolicyDecision:
        """Return an untrusted structural copy without authorization provenance."""

        return PolicyDecision(
            capability_name=self.capability_name,
            allowed=self.allowed,
            stage=self.stage,
            reason_code=self.reason_code,
            message=self.message,
            evidence=self.evidence,
            stop_required=self.stop_required,
            stage_results=self.stage_results,
            request_id=self.request_id,
            started_at=self.started_at,
            capability_policy_sha256=self.capability_policy_sha256,
            context_identity_sha256=self.context_identity_sha256,
            phase=self.phase,
            mode=self.mode,
        )

    def __deepcopy__(self, memo: dict[int, Any]) -> None:
        raise TypeError("Policy decisions with authorization state must not be deep-copied.")

    def __reduce_ex__(self, protocol: int) -> None:
        raise TypeError("Policy decisions with authorization state must not be serialized.")


def _pre_execution_authorization_fingerprint(decision: PolicyDecision) -> str:
    def bounded_message(value: Any) -> str:
        if type(value) is not str:
            return "<invalid-message-type>"
        if len(value) > _MAX_POLICY_MESSAGE_BYTES:
            return "<oversized-message>"
        try:
            if len(value.encode("utf-8", errors="strict")) > _MAX_POLICY_MESSAGE_BYTES:
                return "<oversized-message>"
        except UnicodeEncodeError:
            return "<invalid-message-text>"
        return value

    payload = {
        "capability_name": decision.capability_name,
        "allowed": decision.allowed,
        "stage": decision.stage,
        "reason_code": decision.reason_code,
        "message": bounded_message(decision.message),
        "evidence": _identity_projection(decision.evidence),
        "stop_required": decision.stop_required,
        "request_id": decision.request_id,
        "started_at": (
            decision.started_at.isoformat()
            if decision.started_at is not None
            else None
        ),
        "capability_policy_sha256": decision.capability_policy_sha256,
        "context_identity_sha256": decision.context_identity_sha256,
        "phase": decision.phase,
        "mode": decision.mode,
        "stage_results": [
            {
                "stage": result.stage,
                "allowed": result.allowed,
                "reason_code": result.reason_code,
                "message": bounded_message(result.message),
                "evidence": _identity_projection(result.evidence),
                "stop_required": result.stop_required,
                "applicable": result.applicable,
            }
            for result in decision.stage_results
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class _IssuedPreExecutionAllow:
    decision_ref: ReferenceType[PolicyDecision]
    context_ref: ReferenceType[PolicyContext]
    authorization_fingerprint: str


@dataclass(frozen=True)
class _IssuedPostExecutionSnapshot:
    context_ref: ReferenceType[PostExecutionPolicyContext]
    identity_fingerprint: str
    pre_execution_context: PolicyContext
    pre_execution_decision: PolicyDecision
    execution_result_snapshot: Any
    evidence_snapshot: Any
    completed_at: datetime


def _create_canonical_authority_gate() -> tuple[Any, Any, Any]:
    gate_lock = RLock()
    sealed = False
    trusted_pipeline_ref: ReferenceType[Any] | None = None
    trusted_issuer_ref: ReferenceType[Any] | None = None
    trusted_stages: tuple[Any, ...] = ()
    trusted_stage_evaluators: tuple[Any, ...] = ()
    trusted_evaluation_code: CodeType | None = None
    trusted_post_context_code: CodeType | None = None
    trusted_issue_code: CodeType | None = None
    trusted_register_post_code: CodeType | None = None
    trusted_restore_post_code: CodeType | None = None
    trusted_consume_code: CodeType | None = None
    pre_execution_records: dict[int, _IssuedPreExecutionAllow] = {}
    post_execution_records: dict[int, _IssuedPostExecutionSnapshot] = {}
    next_pre_execution_serial = 1
    next_post_execution_serial = 1

    def evaluator_identity(stage: Any) -> Any:
        evaluator = getattr(stage, "evaluate")
        return getattr(evaluator, "__func__", evaluator)

    def seal(pipeline: PolicyPipeline, issuer: Any) -> None:
        nonlocal sealed
        nonlocal trusted_pipeline_ref
        nonlocal trusted_issuer_ref
        nonlocal trusted_stages
        nonlocal trusted_stage_evaluators
        nonlocal trusted_evaluation_code
        nonlocal trusted_post_context_code
        nonlocal trusted_issue_code
        nonlocal trusted_register_post_code
        nonlocal trusted_restore_post_code
        nonlocal trusted_consume_code
        with gate_lock:
            if sealed:
                raise RuntimeError("Canonical policy authority is already sealed.")
            observed_stages = tuple(object.__getattribute__(pipeline, "_stages"))
            trusted_pipeline_ref = ref(pipeline)
            trusted_issuer_ref = ref(issuer)
            trusted_stages = observed_stages
            trusted_stage_evaluators = tuple(
                evaluator_identity(stage) for stage in observed_stages
            )
            trusted_evaluation_code = object.__getattribute__(
                issuer,
                "_evaluation_code",
            )
            trusted_post_context_code = object.__getattribute__(
                issuer,
                "_post_context_code",
            )
            issuer_type = type(issuer)
            trusted_issue_code = issuer_type._issue_from_completed_evaluation.__code__
            trusted_register_post_code = (
                issuer_type._register_post_execution_context.__code__
            )
            trusted_restore_post_code = (
                issuer_type.restore_post_execution_context.__code__
            )
            trusted_consume_code = issuer_type.consume.__code__
            sealed = True

    def verify(pipeline: PolicyPipeline, issuer: Any) -> bool:
        with gate_lock:
            if (
                not sealed
                or trusted_pipeline_ref is None
                or trusted_issuer_ref is None
                or trusted_pipeline_ref() is not pipeline
                or trusted_issuer_ref() is not issuer
            ):
                return False
            try:
                if object.__getattribute__(issuer, "_pipeline_ref")() is not pipeline:
                    return False
                if (
                    trusted_evaluation_code is None
                    or trusted_post_context_code is None
                    or trusted_issue_code is None
                    or trusted_register_post_code is None
                    or trusted_restore_post_code is None
                    or trusted_consume_code is None
                    or object.__getattribute__(issuer, "_evaluation_code")
                    is not trusted_evaluation_code
                    or object.__getattribute__(issuer, "_post_context_code")
                    is not trusted_post_context_code
                    or PolicyPipeline.evaluate.__code__ is not trusted_evaluation_code
                    or PostExecutionPolicyContext.__post_init__.__code__
                    is not trusted_post_context_code
                ):
                    return False
                current_stages = tuple(object.__getattribute__(pipeline, "_stages"))
                current_evaluators = tuple(
                    evaluator_identity(stage) for stage in current_stages
                )
            except Exception:
                return False
            return (
                len(current_stages) == len(trusted_stages)
                and all(
                    observed is trusted
                    for observed, trusted in zip(
                        current_stages,
                        trusted_stages,
                        strict=True,
                    )
                )
                and len(current_evaluators) == len(trusted_stage_evaluators)
                and all(
                    observed is trusted
                    for observed, trusted in zip(
                        current_evaluators,
                        trusted_stage_evaluators,
                        strict=True,
                    )
                )
            )

    def runtime(
        operation: str,
        pipeline: PolicyPipeline,
        issuer: Any,
        *arguments: Any,
    ) -> Any:
        nonlocal next_pre_execution_serial
        nonlocal next_post_execution_serial
        with gate_lock:
            if not verify(pipeline, issuer):
                return None
            if operation == "issue_pre":
                decision, context, fingerprint = arguments
                try:
                    caller_frame = sys._getframe(1)
                except ValueError:
                    return None
                try:
                    caller_locals = caller_frame.f_locals
                    if (
                        caller_frame.f_code is not trusted_issue_code
                        or caller_locals.get("self") is not issuer
                        or caller_locals.get("pipeline") is not pipeline
                        or caller_locals.get("decision") is not decision
                        or caller_locals.get("context") is not context
                        or caller_locals.get("fingerprint") != fingerprint
                    ):
                        return None
                finally:
                    del caller_frame
                serial = next_pre_execution_serial
                next_pre_execution_serial += 1

                def cleanup(observed_ref: ReferenceType[Any]) -> None:
                    runtime(
                        "discard_pre",
                        pipeline,
                        issuer,
                        serial,
                        observed_ref,
                    )

                pre_execution_records[serial] = _IssuedPreExecutionAllow(
                    decision_ref=ref(decision, cleanup),
                    context_ref=ref(context, cleanup),
                    authorization_fingerprint=fingerprint,
                )
                return serial
            if operation == "consume_pre":
                serial, decision, context, fingerprint = arguments
                try:
                    caller_frame = sys._getframe(1)
                except ValueError:
                    return False
                try:
                    caller_locals = caller_frame.f_locals
                    if (
                        caller_frame.f_code is not trusted_consume_code
                        or caller_locals.get("self") is not issuer
                        or caller_locals.get("trusted_pipeline") is not pipeline
                        or caller_locals.get("serial") != serial
                        or caller_locals.get("decision") is not decision
                        or caller_locals.get("context") is not context
                        or caller_locals.get("authorization_fingerprint")
                        != fingerprint
                    ):
                        return False
                finally:
                    del caller_frame
                record = pre_execution_records.get(serial)
                if record is None:
                    return False
                if (
                    record.decision_ref() is not decision
                    or record.context_ref() is not context
                ):
                    return False
                if record.authorization_fingerprint != fingerprint:
                    pre_execution_records.pop(serial, None)
                    return False
                pre_execution_records.pop(serial, None)
                return True
            if operation == "discard_pre":
                serial, observed_ref = arguments
                record = pre_execution_records.get(serial)
                if record is not None and (
                    observed_ref is record.decision_ref
                    or observed_ref is record.context_ref
                ):
                    pre_execution_records.pop(serial, None)
                return None
            if operation == "register_post":
                context, captured = arguments
                try:
                    caller_frame = sys._getframe(1)
                except ValueError:
                    return None
                try:
                    caller_locals = caller_frame.f_locals
                    if (
                        caller_frame.f_code is not trusted_register_post_code
                        or caller_locals.get("self") is not issuer
                        or caller_locals.get("trusted_pipeline") is not pipeline
                        or caller_locals.get("context") is not context
                        or caller_locals.get("captured") is not captured
                    ):
                        return None
                finally:
                    del caller_frame
                serial = next_post_execution_serial
                next_post_execution_serial += 1

                def cleanup(observed_ref: ReferenceType[Any]) -> None:
                    runtime(
                        "discard_post",
                        pipeline,
                        issuer,
                        serial,
                        observed_ref,
                    )

                post_execution_records[serial] = _IssuedPostExecutionSnapshot(
                    context_ref=ref(context, cleanup),
                    identity_fingerprint=captured.identity_fingerprint,
                    pre_execution_context=captured.pre_execution_context,
                    pre_execution_decision=captured.pre_execution_decision,
                    execution_result_snapshot=captured.execution_result_snapshot,
                    evidence_snapshot=captured.evidence_snapshot,
                    completed_at=captured.completed_at,
                )
                return serial
            if operation == "restore_post":
                serial, context = arguments
                try:
                    caller_frame = sys._getframe(1)
                except ValueError:
                    return None
                try:
                    caller_locals = caller_frame.f_locals
                    if (
                        caller_frame.f_code is not trusted_restore_post_code
                        or caller_locals.get("self") is not issuer
                        or caller_locals.get("trusted_pipeline") is not pipeline
                        or caller_locals.get("serial") != serial
                        or caller_locals.get("context") is not context
                    ):
                        return None
                finally:
                    del caller_frame
                record = post_execution_records.get(serial)
                if record is None or record.context_ref() is not context:
                    return None
                if (
                    object.__getattribute__(
                        context,
                        "_trusted_pre_execution_pipeline",
                    )
                    is not pipeline
                ):
                    return None
                try:
                    observed_fingerprint = context.policy_identity_sha256
                except Exception:
                    return None
                return (
                    record
                    if observed_fingerprint == record.identity_fingerprint
                    else None
                )
            if operation == "discard_post":
                serial, observed_ref = arguments
                record = post_execution_records.get(serial)
                if record is not None and observed_ref is record.context_ref:
                    post_execution_records.pop(serial, None)
                return None
            return None

    return seal, verify, runtime


(
    _CANONICAL_AUTHORITY_SEAL,
    _CANONICAL_AUTHORITY_VERIFY,
    _CANONICAL_AUTHORITY_RUNTIME,
) = _create_canonical_authority_gate()
del _create_canonical_authority_gate


class _PreExecutionIssuerState:
    __slots__ = (
        "evaluation_code",
        "lock",
        "next_serial",
        "next_post_serial",
        "pipeline_ref",
        "post_context_code",
        "post_records",
        "records",
    )


class _PreExecutionAllowIssuer(frozenset[tuple[Any, ...]]):
    __slots__ = ()

    def __new__(
        cls,
        *,
        trusted_pipeline: PolicyPipeline,
        trusted_evaluation_code: CodeType,
        trusted_post_context_code: CodeType,
        _factory_key: object,
    ) -> _PreExecutionAllowIssuer:
        active_factory_key = globals().get("_TRUSTED_PRE_EXECUTION_ISSUER_KEY")
        if active_factory_key is None or _factory_key is not active_factory_key:
            raise TypeError(
                "Policy authorization issuers require the package-internal trusted factory."
            )
        lifecycle_entries: list[tuple[Any, ...]] = [
            ("state", _PreExecutionIssuerState())
        ]

        def seal_callable(
            lifecycle_callable: FunctionType,
            active_function_ids: frozenset[int] = frozenset(),
        ) -> tuple[Any, ...]:
            if id(lifecycle_callable) in active_function_ids:
                return (
                    "cycle",
                    lifecycle_callable,
                    lifecycle_callable.__code__,
                )
            nested_active_ids = active_function_ids | {id(lifecycle_callable)}
            sealed_cells: list[tuple[Any, ...]] = []
            for cell in lifecycle_callable.__closure__ or ():
                cell_value = cell.cell_contents
                if type(cell_value) is FunctionType:
                    sealed_cells.append(
                        (
                            "function",
                            seal_callable(cell_value, nested_active_ids),
                        )
                    )
                else:
                    sealed_cells.append(
                        (
                            "identity",
                            type(cell_value),
                            id(cell_value),
                        )
                    )
            return (
                "callable",
                lifecycle_callable,
                lifecycle_callable.__code__,
                tuple(sealed_cells),
            )

        def unavailable_issue_pre(
            _issuer: _PreExecutionAllowIssuer,
            _decision: PolicyDecision,
            _context: PolicyContext,
            *,
            pipeline: PolicyPipeline,
            evaluated_context: PolicyContext,
        ) -> None:
            return None

        def unavailable_register_post(
            _issuer: _PreExecutionAllowIssuer,
            _context: PostExecutionPolicyContext,
        ) -> None:
            return None

        def unavailable_restore_post(
            _issuer: _PreExecutionAllowIssuer,
            _serial: int,
            *,
            context: PostExecutionPolicyContext,
        ) -> None:
            return None

        def unavailable_consume_pre(
            _issuer: _PreExecutionAllowIssuer,
            _serial: int,
            *,
            decision: PolicyDecision,
            context: PolicyContext,
            authorization_fingerprint: str,
        ) -> bool:
            return False

        for name, lifecycle_callable in (
            ("issue_pre", unavailable_issue_pre),
            ("register_post", unavailable_register_post),
            ("restore_post", unavailable_restore_post),
            ("consume_pre", unavailable_consume_pre),
        ):
            if type(lifecycle_callable) is not FunctionType:
                raise TypeError(
                    "Policy authorization lifecycle callables must be exact functions."
                )
            lifecycle_entries.append((name, seal_callable(lifecycle_callable)))
        return frozenset.__new__(cls, lifecycle_entries)

    def _mutable_state(self) -> _PreExecutionIssuerState:
        matches = tuple(
            entry[1]
            for entry in self.__class__.__base__.__iter__(self)
            if type(entry) is tuple
            and len(entry) == 2
            and entry[0] == "state"
            and type(entry[1]) is _PreExecutionIssuerState
        )
        if len(matches) != 1:
            raise RuntimeError("Policy authorization issuer state is invalid.")
        return matches[0]

    @property
    def _evaluation_code(self) -> CodeType:
        return self._mutable_state().evaluation_code

    @_evaluation_code.setter
    def _evaluation_code(self, value: CodeType) -> None:
        self._mutable_state().evaluation_code = value

    @property
    def _post_context_code(self) -> CodeType:
        return self._mutable_state().post_context_code

    @_post_context_code.setter
    def _post_context_code(self, value: CodeType) -> None:
        self._mutable_state().post_context_code = value

    @property
    def _pipeline_ref(self) -> ReferenceType[PolicyPipeline]:
        return self._mutable_state().pipeline_ref

    @_pipeline_ref.setter
    def _pipeline_ref(self, value: ReferenceType[PolicyPipeline]) -> None:
        self._mutable_state().pipeline_ref = value

    @property
    def _lock(self) -> RLock:
        return self._mutable_state().lock

    @_lock.setter
    def _lock(self, value: RLock) -> None:
        self._mutable_state().lock = value

    @property
    def _next_serial(self) -> int:
        return self._mutable_state().next_serial

    @_next_serial.setter
    def _next_serial(self, value: int) -> None:
        self._mutable_state().next_serial = value

    @property
    def _next_post_serial(self) -> int:
        return self._mutable_state().next_post_serial

    @_next_post_serial.setter
    def _next_post_serial(self, value: int) -> None:
        self._mutable_state().next_post_serial = value

    @property
    def _records(self) -> dict[int, _IssuedPreExecutionAllow]:
        return self._mutable_state().records

    @_records.setter
    def _records(self, value: dict[int, _IssuedPreExecutionAllow]) -> None:
        self._mutable_state().records = value

    @property
    def _post_records(self) -> dict[int, _IssuedPostExecutionSnapshot]:
        return self._mutable_state().post_records

    @_post_records.setter
    def _post_records(self, value: dict[int, _IssuedPostExecutionSnapshot]) -> None:
        self._mutable_state().post_records = value

    def __init__(
        self,
        *,
        trusted_pipeline: PolicyPipeline,
        trusted_evaluation_code: CodeType,
        trusted_post_context_code: CodeType,
        _factory_key: object,
    ) -> None:
        active_factory_key = globals().get("_TRUSTED_PRE_EXECUTION_ISSUER_KEY")
        if active_factory_key is None or _factory_key is not active_factory_key:
            raise TypeError(
                "Policy authorization issuers require the package-internal trusted factory."
            )
        if (
            type(trusted_pipeline) is not PolicyPipeline
            or trusted_pipeline.phase != "pre_execution"
        ):
            raise TypeError(
                "Policy authorization issuers require an exact pre-execution pipeline."
            )
        if (
            type(trusted_evaluation_code) is not CodeType
            or type(trusted_post_context_code) is not CodeType
        ):
            raise TypeError(
                "Policy authorization issuers require exact lifecycle code objects."
            )
        self._evaluation_code = trusted_evaluation_code
        self._post_context_code = trusted_post_context_code
        self._lock = RLock()
        self._next_serial = 1
        self._next_post_serial = 1
        self._pipeline_ref = ref(trusted_pipeline)
        self._records: dict[int, _IssuedPreExecutionAllow] = {}
        self._post_records: dict[int, _IssuedPostExecutionSnapshot] = {}

    def _authority_verified(
        self,
        pipeline: PolicyPipeline,
        _verify: Any = _CANONICAL_AUTHORITY_VERIFY,
    ) -> bool:
        try:
            return _verify(pipeline, self) is True
        except Exception:
            return False

    def _canonical_authority_verified(
        self,
        pipeline: PolicyPipeline,
        _verify: Any = _CANONICAL_AUTHORITY_VERIFY,
    ) -> bool:
        try:
            return _verify(pipeline, self) is True
        except Exception:
            return False

    def _issue_from_completed_evaluation(
        self,
        decision: PolicyDecision,
        context: PolicyContext,
        *,
        pipeline: PolicyPipeline,
        evaluated_context: PolicyContext,
        _canonical_runtime: Any = _CANONICAL_AUTHORITY_RUNTIME,
    ) -> _PreExecutionAllowGrant | None:
        if not self._canonical_authority_verified(pipeline):
            return None
        try:
            caller_frame = sys._getframe(1)
        except ValueError:
            return None
        try:
            caller_locals = caller_frame.f_locals
            if (
                caller_frame.f_code is not self._evaluation_code
                or caller_locals.get("self") is not pipeline
                or caller_locals.get("supplied_context") is not context
                or caller_locals.get("context") is not evaluated_context
                or caller_locals.get("decision") is not decision
            ):
                return None
        finally:
            del caller_frame
        fingerprint = _pre_execution_authorization_fingerprint(decision)
        serial = _canonical_runtime(
            "issue_pre",
            pipeline,
            self,
            decision,
            context,
            fingerprint,
        )
        return (
            _PreExecutionAllowGrant(self, serial)
            if type(serial) is int
            else None
        )

    def _register_post_execution_context(
        self,
        context: PostExecutionPolicyContext,
        _canonical_runtime: Any = _CANONICAL_AUTHORITY_RUNTIME,
    ) -> _PostExecutionSnapshotGrant | None:
        trusted_pipeline = self._pipeline_ref()
        if (
            type(trusted_pipeline) is not PolicyPipeline
            or not self._canonical_authority_verified(trusted_pipeline)
        ):
            return None
        try:
            caller_frame = sys._getframe(1)
        except ValueError:
            return None
        try:
            if (
                caller_frame.f_code is not self._post_context_code
                or caller_frame.f_locals.get("self") is not context
            ):
                return None
        finally:
            del caller_frame
        captured = _capture_post_execution_snapshot(context)
        serial = _canonical_runtime(
            "register_post",
            trusted_pipeline,
            self,
            context,
            captured,
        )
        return (
            _PostExecutionSnapshotGrant(self, serial)
            if type(serial) is int
            else None
        )

    def restore_post_execution_context(
        self,
        serial: int,
        *,
        context: PostExecutionPolicyContext,
        _canonical_runtime: Any = _CANONICAL_AUTHORITY_RUNTIME,
    ) -> PostExecutionPolicyContext | None:
        trusted_pipeline = self._pipeline_ref()
        if (
            type(trusted_pipeline) is not PolicyPipeline
            or not self._canonical_authority_verified(trusted_pipeline)
        ):
            return None
        record = _canonical_runtime(
            "restore_post",
            trusted_pipeline,
            self,
            serial,
            context,
        )
        if type(record) is not _IssuedPostExecutionSnapshot:
            return None
        return _restore_post_execution_snapshot(
            record,
            trusted_pipeline,
            self,
        )

    def owned_by(self, pipeline: PolicyPipeline) -> bool:
        return self._pipeline_ref() is pipeline

    def consume(
        self,
        serial: int,
        *,
        decision: PolicyDecision,
        context: PolicyContext,
        authorization_fingerprint: str,
        _canonical_runtime: Any = _CANONICAL_AUTHORITY_RUNTIME,
    ) -> bool:
        trusted_pipeline = self._pipeline_ref()
        if (
            type(trusted_pipeline) is not PolicyPipeline
            or not self._canonical_authority_verified(trusted_pipeline)
        ):
            return False
        return _canonical_runtime(
            "consume_pre",
            trusted_pipeline,
            self,
            serial,
            decision,
            context,
            authorization_fingerprint,
        ) is True

    def _discard(
        self,
        serial: int,
        observed_ref: ReferenceType[Any],
    ) -> None:
        with self._lock:
            record = self._records.get(serial)
            if record is not None and (
                observed_ref is record.decision_ref
                or observed_ref is record.context_ref
            ):
                self._records.pop(serial, None)

    def _discard_post(
        self,
        serial: int,
        observed_ref: ReferenceType[Any],
    ) -> None:
        with self._lock:
            record = self._post_records.get(serial)
            if record is not None and observed_ref is record.context_ref:
                self._post_records.pop(serial, None)

    def __copy__(self) -> None:
        raise TypeError("Policy authorization issuers must not be copied.")

    def __deepcopy__(self, memo: dict[int, Any]) -> None:
        raise TypeError("Policy authorization issuers must not be deep-copied.")

    def __reduce_ex__(self, protocol: int) -> None:
        raise TypeError("Policy authorization issuers must not be serialized.")


def _bind_canonical_issuer_lifecycle(
    verify_authority: Any,
    authority_runtime: Any,
) -> tuple[Any, Any, Any, Any, Any, Any]:
    def authority_verified(
        issuer: _PreExecutionAllowIssuer,
        pipeline: PolicyPipeline,
    ) -> bool:
        try:
            return verify_authority(pipeline, issuer) is True
        except Exception:
            return False

    def canonical_authority_verified(
        issuer: _PreExecutionAllowIssuer,
        pipeline: PolicyPipeline,
    ) -> bool:
        return authority_verified(issuer, pipeline)

    def issue_pre_execution(
        self: _PreExecutionAllowIssuer,
        decision: PolicyDecision,
        context: PolicyContext,
        *,
        pipeline: PolicyPipeline,
        evaluated_context: PolicyContext,
    ) -> _PreExecutionAllowGrant | None:
        if not canonical_authority_verified(self, pipeline):
            return None
        try:
            caller_frame = sys._getframe(1)
        except ValueError:
            return None
        try:
            caller_locals = caller_frame.f_locals
            if (
                caller_frame.f_code is not self._evaluation_code
                or caller_locals.get("self") is not pipeline
                or caller_locals.get("supplied_context") is not context
                or caller_locals.get("context") is not evaluated_context
                or caller_locals.get("decision") is not decision
            ):
                return None
        finally:
            del caller_frame
        fingerprint = _pre_execution_authorization_fingerprint(decision)
        serial = authority_runtime(
            "issue_pre",
            pipeline,
            self,
            decision,
            context,
            fingerprint,
        )
        return (
            _PreExecutionAllowGrant(self, serial)
            if type(serial) is int
            else None
        )

    def register_post_execution(
        self: _PreExecutionAllowIssuer,
        context: PostExecutionPolicyContext,
    ) -> _PostExecutionSnapshotGrant | None:
        trusted_pipeline = self._pipeline_ref()
        if (
            type(trusted_pipeline) is not PolicyPipeline
            or not canonical_authority_verified(self, trusted_pipeline)
        ):
            return None
        try:
            caller_frame = sys._getframe(1)
        except ValueError:
            return None
        try:
            if (
                caller_frame.f_code is not self._post_context_code
                or caller_frame.f_locals.get("self") is not context
            ):
                return None
        finally:
            del caller_frame
        captured = _capture_post_execution_snapshot(context)
        serial = authority_runtime(
            "register_post",
            trusted_pipeline,
            self,
            context,
            captured,
        )
        return (
            _PostExecutionSnapshotGrant(self, serial)
            if type(serial) is int
            else None
        )

    def restore_post_execution(
        self: _PreExecutionAllowIssuer,
        serial: int,
        *,
        context: PostExecutionPolicyContext,
    ) -> PostExecutionPolicyContext | None:
        trusted_pipeline = self._pipeline_ref()
        if (
            type(trusted_pipeline) is not PolicyPipeline
            or not canonical_authority_verified(self, trusted_pipeline)
        ):
            return None
        record = authority_runtime(
            "restore_post",
            trusted_pipeline,
            self,
            serial,
            context,
        )
        if type(record) is not _IssuedPostExecutionSnapshot:
            return None
        return _restore_post_execution_snapshot(
            record,
            trusted_pipeline,
            self,
        )

    def consume_pre_execution(
        self: _PreExecutionAllowIssuer,
        serial: int,
        *,
        decision: PolicyDecision,
        context: PolicyContext,
        authorization_fingerprint: str,
    ) -> bool:
        trusted_pipeline = self._pipeline_ref()
        if (
            type(trusted_pipeline) is not PolicyPipeline
            or not canonical_authority_verified(self, trusted_pipeline)
        ):
            return False
        return authority_runtime(
            "consume_pre",
            trusted_pipeline,
            self,
            serial,
            decision,
            context,
            authorization_fingerprint,
        ) is True

    return (
        authority_verified,
        canonical_authority_verified,
        issue_pre_execution,
        register_post_execution,
        restore_post_execution,
        consume_pre_execution,
    )


(
    _PreExecutionAllowIssuer._authority_verified,
    _PreExecutionAllowIssuer._canonical_authority_verified,
    _PreExecutionAllowIssuer._issue_from_completed_evaluation,
    _PreExecutionAllowIssuer._register_post_execution_context,
    _PreExecutionAllowIssuer.restore_post_execution_context,
    _PreExecutionAllowIssuer.consume,
) = _bind_canonical_issuer_lifecycle(
    _CANONICAL_AUTHORITY_VERIFY,
    _CANONICAL_AUTHORITY_RUNTIME,
)
del _bind_canonical_issuer_lifecycle


class _PreExecutionAllowGrant:
    __slots__ = ("_issuer", "_serial")

    def __init__(self, issuer: _PreExecutionAllowIssuer, serial: int) -> None:
        self._issuer = issuer
        self._serial = serial

    def consume(
        self,
        *,
        expected_issuer: _PreExecutionAllowIssuer,
        decision: PolicyDecision,
        context: PolicyContext,
        authorization_fingerprint: str,
    ) -> bool:
        if self._issuer is not expected_issuer:
            return False
        return self._issuer.consume(
            self._serial,
            decision=decision,
            context=context,
            authorization_fingerprint=authorization_fingerprint,
        )

    def __copy__(self) -> None:
        raise TypeError("Policy authorization grants must not be copied.")

    def __deepcopy__(self, memo: dict[int, Any]) -> None:
        raise TypeError("Policy authorization grants must not be deep-copied.")

    def __reduce_ex__(self, protocol: int) -> None:
        raise TypeError("Policy authorization grants must not be serialized.")


class _PostExecutionSnapshotGrant:
    __slots__ = ("_issuer", "_serial")

    def __init__(self, issuer: _PreExecutionAllowIssuer, serial: int) -> None:
        self._issuer = issuer
        self._serial = serial

    def restore(
        self,
        *,
        expected_issuer: _PreExecutionAllowIssuer,
        context: PostExecutionPolicyContext,
    ) -> PostExecutionPolicyContext | None:
        if self._issuer is not expected_issuer:
            return None
        return self._issuer.restore_post_execution_context(
            self._serial,
            context=context,
        )

    def __copy__(self) -> None:
        raise TypeError("Post-execution snapshot grants must not be copied.")

    def __deepcopy__(self, memo: dict[int, Any]) -> None:
        raise TypeError("Post-execution snapshot grants must not be deep-copied.")

    def __reduce_ex__(self, protocol: int) -> None:
        raise TypeError("Post-execution snapshot grants must not be serialized.")


def _detached_policy_context_copy(value: PolicyContext) -> PolicyContext:
    def thaw_optional(field_name: str) -> Any:
        observed = object.__getattribute__(value, field_name)
        return None if observed is None else _thaw(observed)

    return PolicyContext.create(
        capability=object.__getattribute__(value, "capability"),
        arguments=_thaw(object.__getattribute__(value, "_arguments_snapshot")),
        active_profile=object.__getattribute__(value, "active_profile"),
        profile_enforced=object.__getattribute__(value, "profile_enforced"),
        caller_identity=thaw_optional("caller_identity"),
        session_identity=thaw_optional("session_identity"),
        project_capability=thaw_optional("project_capability"),
        trusted_vivado_identity=thaw_optional("trusted_vivado_identity"),
        request_id=object.__getattribute__(value, "request_id"),
        started_at=object.__getattribute__(value, "started_at"),
    )


def _detached_policy_decision_copy(value: PolicyDecision) -> PolicyDecision:
    return PolicyDecision(
        capability_name=object.__getattribute__(value, "capability_name"),
        allowed=object.__getattribute__(value, "allowed"),
        stage=object.__getattribute__(value, "stage"),
        reason_code=object.__getattribute__(value, "reason_code"),
        message=object.__getattribute__(value, "message"),
        evidence=object.__getattribute__(value, "evidence"),
        stop_required=object.__getattribute__(value, "stop_required"),
        stage_results=object.__getattribute__(value, "stage_results"),
        request_id=object.__getattribute__(value, "request_id"),
        started_at=object.__getattribute__(value, "started_at"),
        capability_policy_sha256=object.__getattribute__(
            value,
            "capability_policy_sha256",
        ),
        context_identity_sha256=object.__getattribute__(
            value,
            "context_identity_sha256",
        ),
        phase=object.__getattribute__(value, "phase"),
        mode=object.__getattribute__(value, "mode"),
    )


@dataclass(frozen=True)
class PostExecutionPolicyContext:
    """Immutable post-execution snapshot kept separate from authorization input."""

    pre_execution_context: PolicyContext
    pre_execution_decision: PolicyDecision
    _trusted_pre_execution_pipeline: PolicyPipeline = field(
        repr=False,
        compare=False,
    )
    _execution_result_snapshot: Any = field(repr=False)
    _evidence_snapshot: Any = field(repr=False)
    completed_at: datetime
    _evaluation_grant: _PostExecutionSnapshotGrant | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        lifecycle_tuple_type = ().__class__
        lifecycle_exact_type = lifecycle_tuple_type.__class__
        lifecycle_string_type = "".__class__
        lifecycle_function_type = (lambda: None).__class__
        lifecycle_code_type = (lambda: None).__code__.__class__

        supplied_pre_context = self.pre_execution_context
        supplied_decision = self.pre_execution_decision
        if type(supplied_pre_context) is not PolicyContext:
            raise TypeError("Post-execution policy requires a PolicyContext.")
        if type(supplied_decision) is not PolicyDecision:
            raise TypeError("Post-execution policy requires a PolicyDecision.")
        if type(self._trusted_pre_execution_pipeline) is not PolicyPipeline:
            raise TypeError("Post-execution policy requires the expected PolicyPipeline.")
        trusted_issuer = object.__getattribute__(
            self._trusted_pre_execution_pipeline,
            "_issuer",
        )
        if (
            self._trusted_pre_execution_pipeline.phase != "pre_execution"
            or type(trusted_issuer) is not _PreExecutionAllowIssuer
            or not trusted_issuer._authority_verified(
                self._trusted_pre_execution_pipeline
            )
        ):
            raise ValueError(
                "Post-execution policy requires a package-trusted pre-execution pipeline."
            )
        consume_lifecycle: tuple[FunctionType, CodeType] | None = None
        register_lifecycle: tuple[FunctionType, CodeType] | None = None
        lifecycle_entries_invalid = False
        for entry in trusted_issuer.__class__.__base__.__iter__(trusted_issuer):
            if lifecycle_exact_type(entry) is not lifecycle_tuple_type:
                continue
            if lifecycle_tuple_type.__len__(entry) != 2:
                continue
            entry_name = lifecycle_tuple_type.__getitem__(entry, 0)
            if (
                lifecycle_exact_type(entry_name) is not lifecycle_string_type
                or entry_name not in ("consume_pre", "register_post")
            ):
                continue
            seal = lifecycle_tuple_type.__getitem__(entry, 1)
            if (
                lifecycle_exact_type(seal) is not lifecycle_tuple_type
                or lifecycle_tuple_type.__len__(seal) != 4
            ):
                lifecycle_entries_invalid = True
                break
            marker = lifecycle_tuple_type.__getitem__(seal, 0)
            lifecycle_callable = lifecycle_tuple_type.__getitem__(seal, 1)
            sealed_code = lifecycle_tuple_type.__getitem__(seal, 2)
            sealed_cells = lifecycle_tuple_type.__getitem__(seal, 3)
            if (
                lifecycle_exact_type(marker) is not lifecycle_string_type
                or marker != "callable"
                or lifecycle_exact_type(lifecycle_callable)
                is not lifecycle_function_type
                or lifecycle_exact_type(sealed_code) is not lifecycle_code_type
                or lifecycle_exact_type(sealed_cells) is not lifecycle_tuple_type
                or lifecycle_tuple_type.__len__(sealed_cells) != 0
                or lifecycle_callable.__code__ is not sealed_code
                or lifecycle_callable.__closure__ is not None
                or lifecycle_callable.__defaults__ is not None
                or lifecycle_callable.__kwdefaults__ is not None
            ):
                lifecycle_entries_invalid = True
                break
            lifecycle_pair = (lifecycle_callable, sealed_code)
            if entry_name == "consume_pre":
                if consume_lifecycle is not None:
                    lifecycle_entries_invalid = True
                    break
                consume_lifecycle = lifecycle_pair
            else:
                if register_lifecycle is not None:
                    lifecycle_entries_invalid = True
                    break
                register_lifecycle = lifecycle_pair
        if lifecycle_entries_invalid:
            consume_lifecycle = None
            register_lifecycle = None
        try:
            pre_context = _detached_policy_context_copy(supplied_pre_context)
            decision = _detached_policy_decision_copy(supplied_decision)
        except Exception as exc:
            raise ValueError(
                "Post-execution policy requires one unconsumed pipeline-issued "
                "pre-execution authorization."
            ) from exc
        if not isinstance(self._execution_result_snapshot, Mapping):
            raise TypeError("Post-execution execution_result must be a mapping.")
        if not isinstance(self._evidence_snapshot, Mapping):
            raise TypeError("Post-execution evidence_snapshot must be a mapping.")
        identity_validation_state = _IdentityValidationState()
        snapshot_state = _SnapshotFreezeState()
        if not snapshot_state.consume():
            raise ValueError("Post-execution context exceeds the snapshot node budget.")
        execution_result_snapshot = _freeze(
            self._execution_result_snapshot,
            _state=snapshot_state,
            _strict=True,
            _field_name="execution_result",
        )
        evidence_snapshot = _freeze(
            self._evidence_snapshot,
            _state=snapshot_state,
            _strict=True,
            _field_name="evidence_snapshot",
        )
        _validate_identity_snapshot(
            execution_result_snapshot,
            field_name="execution_result",
            _state=identity_validation_state,
        )
        _validate_identity_snapshot(
            evidence_snapshot,
            field_name="evidence_snapshot",
            _state=identity_validation_state,
        )
        if type(execution_result_snapshot.get("ok")) is not bool:
            raise ValueError("Post-execution execution_result requires a boolean ok field.")

        capability_snapshot = pre_context.capability
        if not decision.allowed:
            raise ValueError("Post-execution policy requires an allowed pre-execution decision.")
        if decision.mode != "foundation":
            raise ValueError("Post-execution policy requires an authoritative foundation decision.")
        if decision.phase != "pre_execution":
            raise ValueError("Post-execution policy requires a pre_execution decision.")
        if decision.stage != "policy_pipeline":
            raise ValueError("Post-execution policy requires the canonical pre-execution terminal stage.")
        if decision.reason_code != POLICY_PIPELINE_ALLOWED:
            raise ValueError("Post-execution policy requires the canonical pre-execution ALLOW reason.")
        if decision.stop_required:
            raise ValueError("Post-execution policy cannot follow a stop-required decision.")
        if decision.capability_name != capability_snapshot.name:
            raise ValueError("Post-execution capability does not match the pre-execution decision.")
        if decision.capability_policy_sha256 != capability_snapshot.policy_identity_sha256:
            raise ValueError("Post-execution capability policy identity does not match pre-execution.")
        if decision.context_identity_sha256 != pre_context.policy_identity_sha256:
            raise ValueError("Post-execution context identity does not match pre-execution.")
        if not decision.request_id.strip():
            raise ValueError("Post-execution policy requires a bound pre-execution request_id.")
        if decision.request_id != pre_context.request_id:
            raise ValueError("Post-execution request_id does not match the pre-execution context.")
        if decision.started_at is None:
            raise ValueError("Post-execution policy requires a bound pre-execution started_at.")
        if decision.started_at != pre_context.started_at:
            raise ValueError("Post-execution started_at does not match the pre-execution context.")
        normalized_completed_at = _require_aware_datetime(self.completed_at)
        if normalized_completed_at < pre_context.started_at:
            raise ValueError("Post-execution completed_at must not precede started_at.")
        stage_names = tuple(result.stage for result in decision.stage_results)
        if stage_names != PRE_EXECUTION_POLICY_STAGE_ORDER:
            raise ValueError("Post-execution policy requires the complete canonical pre-execution trace.")
        if any(not result.allowed or result.stop_required for result in decision.stage_results):
            raise ValueError("Post-execution policy requires an all-ALLOW pre-execution trace.")
        requirements = policy_stage_requirements(capability_snapshot)
        if any(
            requirements[result.stage] and not result.applicable
            for result in decision.stage_results
        ):
            raise ValueError(
                "Post-execution policy requires every required pre-execution stage to apply."
            )
        provenance = object.__getattribute__(supplied_decision, "_provenance")
        authorization_fingerprint = _pre_execution_authorization_fingerprint(decision)
        if (
            type(provenance) is not _PreExecutionAllowGrant
            or object.__getattribute__(provenance, "_issuer") is not trusted_issuer
            or consume_lifecycle is None
            or not consume_lifecycle[0].__class__(
                consume_lifecycle[1],
                consume_lifecycle[0].__globals__,
                consume_lifecycle[1].co_name,
            )(
                trusted_issuer,
                object.__getattribute__(provenance, "_serial"),
                decision=supplied_decision,
                context=supplied_pre_context,
                authorization_fingerprint=authorization_fingerprint,
            )
        ):
            raise ValueError(
                "Post-execution policy requires one unconsumed pipeline-issued "
                "pre-execution authorization."
            )

        object.__setattr__(self, "pre_execution_context", pre_context)
        object.__setattr__(self, "pre_execution_decision", decision)
        object.__setattr__(
            self,
            "_execution_result_snapshot",
            execution_result_snapshot,
        )
        object.__setattr__(
            self,
            "_evidence_snapshot",
            evidence_snapshot,
        )
        object.__setattr__(self, "completed_at", normalized_completed_at)
        evaluation_grant = (
            register_lifecycle[0].__class__(
                register_lifecycle[1],
                register_lifecycle[0].__globals__,
                register_lifecycle[1].co_name,
            )(trusted_issuer, self)
            if register_lifecycle is not None
            else None
        )
        if type(evaluation_grant) is not _PostExecutionSnapshotGrant:
            raise ValueError(
                "Post-execution policy requires canonical internal snapshot ownership."
            )
        object.__setattr__(self, "_evaluation_grant", evaluation_grant)

    @classmethod
    def create(
        cls,
        *,
        pre_execution_context: PolicyContext,
        pre_execution_decision: PolicyDecision,
        trusted_pre_execution_pipeline: PolicyPipeline,
        execution_result: Mapping[str, Any],
        evidence_snapshot: Mapping[str, Any],
        completed_at: datetime,
    ) -> PostExecutionPolicyContext:
        return cls(
            pre_execution_context=pre_execution_context,
            pre_execution_decision=pre_execution_decision,
            _trusted_pre_execution_pipeline=trusted_pre_execution_pipeline,
            _execution_result_snapshot=execution_result,
            _evidence_snapshot=evidence_snapshot,
            completed_at=completed_at,
        )

    @property
    def capability(self) -> CapabilityPolicySnapshot:
        return self.pre_execution_context.capability

    @property
    def capability_name(self) -> str:
        return self.capability.name

    @property
    def policy_identity_sha256(self) -> str:
        payload = {
            "pre_execution_context_sha256": self.pre_execution_context.policy_identity_sha256,
            "pre_execution_authorization_sha256": (
                _pre_execution_authorization_fingerprint(
                    self.pre_execution_decision
                )
            ),
            "pre_execution_decision": {
                "allowed": self.pre_execution_decision.allowed,
                "stage": self.pre_execution_decision.stage,
                "reason_code": self.pre_execution_decision.reason_code,
                "stop_required": self.pre_execution_decision.stop_required,
                "mode": self.pre_execution_decision.mode,
                "capability_policy_sha256": (
                    self.pre_execution_decision.capability_policy_sha256
                ),
                "context_identity_sha256": self.pre_execution_decision.context_identity_sha256,
                "stage_results": [
                    {
                        "stage": result.stage,
                        "allowed": result.allowed,
                        "reason_code": result.reason_code,
                        "stop_required": result.stop_required,
                        "applicable": result.applicable,
                        "evidence": _identity_projection(result.evidence),
                    }
                    for result in self.pre_execution_decision.stage_results
                ],
            },
            "execution_result": _identity_projection(self._execution_result_snapshot),
            "evidence_snapshot": _identity_projection(self._evidence_snapshot),
            "completed_at": self.completed_at.isoformat(),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    @property
    def request_id(self) -> str:
        return self.pre_execution_context.request_id

    @property
    def started_at(self) -> datetime:
        return self.pre_execution_context.started_at

    @property
    def execution_result(self) -> Any:
        return _thaw(self._execution_result_snapshot)

    @property
    def evidence_snapshot(self) -> Any:
        return _thaw(self._evidence_snapshot)


def _capture_post_execution_snapshot(
    context: PostExecutionPolicyContext,
) -> _IssuedPostExecutionSnapshot:
    snapshot_state = _SnapshotFreezeState()
    if not snapshot_state.consume():
        raise ValueError("Post-execution snapshot exceeds the snapshot node budget.")
    execution_result_snapshot = _freeze(
        _thaw(object.__getattribute__(context, "_execution_result_snapshot")),
        _state=snapshot_state,
        _strict=True,
        _field_name="execution_result",
    )
    evidence_snapshot = _freeze(
        _thaw(object.__getattribute__(context, "_evidence_snapshot")),
        _state=snapshot_state,
        _strict=True,
        _field_name="evidence_snapshot",
    )
    return _IssuedPostExecutionSnapshot(
        context_ref=ref(context),
        identity_fingerprint=context.policy_identity_sha256,
        pre_execution_context=_detached_policy_context_copy(
            object.__getattribute__(context, "pre_execution_context")
        ),
        pre_execution_decision=_detached_policy_decision_copy(
            object.__getattribute__(context, "pre_execution_decision")
        ),
        execution_result_snapshot=execution_result_snapshot,
        evidence_snapshot=evidence_snapshot,
        completed_at=_require_aware_datetime(
            object.__getattribute__(context, "completed_at")
        ),
    )


def _restore_post_execution_snapshot(
    record: _IssuedPostExecutionSnapshot,
    trusted_pipeline: PolicyPipeline | None,
    trusted_issuer: _PreExecutionAllowIssuer,
) -> PostExecutionPolicyContext | None:
    if (
        type(trusted_pipeline) is not PolicyPipeline
        or type(trusted_issuer) is not _PreExecutionAllowIssuer
        or not trusted_issuer._authority_verified(trusted_pipeline)
    ):
        return None
    snapshot_state = _SnapshotFreezeState()
    if not snapshot_state.consume():
        return None
    try:
        pre_context = _detached_policy_context_copy(record.pre_execution_context)
        decision = _detached_policy_decision_copy(record.pre_execution_decision)
        execution_result_snapshot = _freeze(
            _thaw(record.execution_result_snapshot),
            _state=snapshot_state,
            _strict=True,
            _field_name="execution_result",
        )
        evidence_snapshot = _freeze(
            _thaw(record.evidence_snapshot),
            _state=snapshot_state,
            _strict=True,
            _field_name="evidence_snapshot",
        )
        completed_at = _require_aware_datetime(record.completed_at)
    except Exception:
        return None
    restored = object.__new__(PostExecutionPolicyContext)
    object.__setattr__(restored, "pre_execution_context", pre_context)
    object.__setattr__(restored, "pre_execution_decision", decision)
    object.__setattr__(restored, "_trusted_pre_execution_pipeline", trusted_pipeline)
    object.__setattr__(restored, "_execution_result_snapshot", execution_result_snapshot)
    object.__setattr__(restored, "_evidence_snapshot", evidence_snapshot)
    object.__setattr__(restored, "completed_at", completed_at)
    object.__setattr__(restored, "_evaluation_grant", None)
    if restored.policy_identity_sha256 != record.identity_fingerprint:
        return None
    return restored


PolicyEvaluationContext = PolicyContext | PostExecutionPolicyContext


class PolicyStage(Protocol):
    """Synchronous pure stage contract for a pre-collected context snapshot."""

    name: str

    def evaluate(self, context: PolicyEvaluationContext) -> PolicyStageResult:
        ...


@dataclass(frozen=True, init=False)
class PolicyPipeline:
    """Evaluate every registered stage in the one canonical order."""

    _phase: str
    _stages: tuple[PolicyStage, ...]
    _registered_stage_names: tuple[str, ...]
    _stage_order: tuple[str, ...]
    _configuration_errors: tuple[str, ...]
    _issuer: _PreExecutionAllowIssuer | None = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __init__(self, stages: Sequence[PolicyStage], *, phase: str = "pre_execution") -> None:
        registered_stages = tuple(stages)
        normalized_phase, phase_errors = self._normalize_phase(phase)
        names, name_errors = self._collect_stage_names(registered_stages)
        object.__setattr__(self, "_phase", normalized_phase)
        object.__setattr__(self, "_stages", registered_stages)
        object.__setattr__(self, "_registered_stage_names", names)
        object.__setattr__(
            self,
            "_stage_order",
            POLICY_PHASE_STAGE_ORDERS.get(normalized_phase, ()),
        )
        object.__setattr__(
            self,
            "_configuration_errors",
            (
                *phase_errors,
                *name_errors,
                *self._validate_stage_names(names, phase=normalized_phase),
            ),
        )
        object.__setattr__(self, "_issuer", None)

    @staticmethod
    def _normalize_phase(phase: Any) -> tuple[str, tuple[str, ...]]:
        if type(phase) is not str:
            return "<invalid-phase>", (f"phase_type={_safe_type_name(phase)}",)
        if not phase or len(phase) > 128:
            return "<invalid-phase>", ("phase_value_invalid",)
        try:
            phase.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return "<invalid-phase>", ("phase_value_invalid",)
        return phase, ()

    @staticmethod
    def _collect_stage_names(
        stages: tuple[PolicyStage, ...],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        names: list[str] = []
        errors: list[str] = []
        for index, stage in enumerate(stages):
            placeholder = f"<invalid-stage-name-{index:02d}>"
            try:
                name = stage.name
            except Exception as exc:
                names.append(placeholder)
                errors.append(f"stage_name_error_{index:02d}={_safe_type_name(exc)}")
                continue
            if type(name) is not str:
                names.append(placeholder)
                errors.append(f"stage_name_type_{index:02d}={_safe_type_name(name)}")
                continue
            try:
                valid_text = bool(name) and len(name) <= 128
                if valid_text:
                    name.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                valid_text = False
            if not valid_text:
                names.append(placeholder)
                errors.append(f"stage_name_value_{index:02d}=invalid")
                continue
            names.append(name)
        return tuple(names), tuple(errors)

    @staticmethod
    def _validate_stage_names(names: tuple[str, ...], *, phase: str) -> tuple[str, ...]:
        errors: list[str] = []
        expected_order = POLICY_PHASE_STAGE_ORDERS.get(phase)
        if expected_order is None:
            return ("unknown_phase",)
        if names != expected_order:
            missing = tuple(name for name in expected_order if name not in names)
            unknown = tuple(name for name in names if name not in expected_order)
            duplicates = tuple(sorted({name for name in names if names.count(name) > 1}))
            if missing:
                errors.append(f"missing={','.join(missing)}")
            if unknown:
                errors.append(f"unknown_count={len(unknown)}")
            if duplicates:
                errors.append(f"duplicate_count={len(duplicates)}")
            if not (missing or unknown or duplicates):
                errors.append("stage_order_mismatch")
        return tuple(errors)

    def _runtime_configuration(
        self,
    ) -> tuple[
        str,
        tuple[PolicyStage, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]:
        errors: list[str] = []
        try:
            observed_phase = object.__getattribute__(self, "_phase")
        except Exception as exc:
            observed_phase = None
            errors.append(f"phase_read_error={_safe_type_name(exc)}")
        normalized_phase, phase_errors = self._normalize_phase(observed_phase)
        errors.extend(phase_errors)

        try:
            observed_stages = object.__getattribute__(self, "_stages")
        except Exception as exc:
            observed_stages = None
            errors.append(f"stage_container_read_error={_safe_type_name(exc)}")
        if type(observed_stages) is not tuple:
            registered_stages: tuple[PolicyStage, ...] = ()
            errors.append("stage_container_invalid")
        elif len(observed_stages) > len(POLICY_STAGE_ORDER):
            registered_stages = ()
            errors.append("stage_count_invalid")
        else:
            registered_stages = observed_stages

        names, name_errors = self._collect_stage_names(registered_stages)
        errors.extend(name_errors)
        errors.extend(self._validate_stage_names(names, phase=normalized_phase))
        stage_order = POLICY_PHASE_STAGE_ORDERS.get(normalized_phase, ())
        derived_errors = tuple(errors)
        try:
            cached_snapshot_matches = (
                object.__getattribute__(self, "_registered_stage_names") == names
                and object.__getattribute__(self, "_stage_order") == stage_order
                and object.__getattribute__(self, "_configuration_errors")
                == derived_errors
            )
        except Exception:
            cached_snapshot_matches = False
        if not cached_snapshot_matches:
            derived_errors = (*derived_errors, "pipeline_snapshot_mismatch")
        return normalized_phase, registered_stages, stage_order, derived_errors

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def stage_names(self) -> tuple[str, ...]:
        return self._registered_stage_names

    def __copy__(self) -> None:
        raise TypeError("Policy pipelines must not be copied.")

    def __deepcopy__(self, memo: dict[int, Any]) -> None:
        raise TypeError("Policy pipelines must not be deep-copied.")

    def __reduce_ex__(self, protocol: int) -> None:
        raise TypeError("Policy pipelines must not be serialized.")

    def evaluate(self, context: PolicyEvaluationContext) -> PolicyDecision:
        lifecycle_tuple_type = ().__class__
        lifecycle_exact_type = lifecycle_tuple_type.__class__
        lifecycle_string_type = "".__class__
        lifecycle_function_type = (lambda: None).__class__
        lifecycle_code_type = (lambda: None).__code__.__class__
        issue_lifecycle: tuple[FunctionType, CodeType] | None = None
        issue_lifecycle_invalid = False
        candidate_issuer = object.__getattribute__(self, "_issuer")
        if lifecycle_exact_type(candidate_issuer) is _PreExecutionAllowIssuer:
            for entry in candidate_issuer.__class__.__base__.__iter__(
                candidate_issuer
            ):
                if lifecycle_exact_type(entry) is not lifecycle_tuple_type:
                    continue
                if lifecycle_tuple_type.__len__(entry) != 2:
                    continue
                entry_name = lifecycle_tuple_type.__getitem__(entry, 0)
                if (
                    lifecycle_exact_type(entry_name) is not lifecycle_string_type
                    or entry_name != "issue_pre"
                ):
                    continue
                if issue_lifecycle is not None:
                    issue_lifecycle_invalid = True
                    break
                seal = lifecycle_tuple_type.__getitem__(entry, 1)
                if (
                    lifecycle_exact_type(seal) is not lifecycle_tuple_type
                    or lifecycle_tuple_type.__len__(seal) != 4
                ):
                    issue_lifecycle_invalid = True
                    break
                marker = lifecycle_tuple_type.__getitem__(seal, 0)
                lifecycle_callable = lifecycle_tuple_type.__getitem__(seal, 1)
                sealed_code = lifecycle_tuple_type.__getitem__(seal, 2)
                sealed_cells = lifecycle_tuple_type.__getitem__(seal, 3)
                if (
                    lifecycle_exact_type(marker) is not lifecycle_string_type
                    or marker != "callable"
                    or lifecycle_exact_type(lifecycle_callable)
                    is not lifecycle_function_type
                    or lifecycle_exact_type(sealed_code) is not lifecycle_code_type
                    or lifecycle_exact_type(sealed_cells)
                    is not lifecycle_tuple_type
                    or lifecycle_tuple_type.__len__(sealed_cells) != 0
                    or lifecycle_callable.__code__ is not sealed_code
                    or lifecycle_callable.__closure__ is not None
                    or lifecycle_callable.__defaults__ is not None
                    or lifecycle_callable.__kwdefaults__ is not None
                ):
                    issue_lifecycle_invalid = True
                    break
                issue_lifecycle = (lifecycle_callable, sealed_code)
        if issue_lifecycle_invalid:
            issue_lifecycle = None

        supplied_context = context
        (
            pipeline_phase,
            registered_stages,
            stage_order,
            configuration_errors,
        ) = self._runtime_configuration()
        normalized_phase = (
            pipeline_phase
            if pipeline_phase in POLICY_PHASE_STAGE_ORDERS
            else "pre_execution"
        )
        evidence_phase = (
            pipeline_phase
            if pipeline_phase in POLICY_PHASE_STAGE_ORDERS
            else "invalid"
        )
        if configuration_errors:
            return PolicyDecision(
                capability_name="invalid_context",
                allowed=False,
                stage="policy_pipeline",
                reason_code=POLICY_PIPELINE_CONFIGURATION_INVALID,
                message="Policy pipeline stage registration is incomplete or out of order.",
                evidence={"configuration_errors": configuration_errors},
                stop_required=True,
                phase=normalized_phase,
                mode="foundation",
            )
        if type(context) not in (PolicyContext, PostExecutionPolicyContext):
            return PolicyDecision(
                capability_name="invalid_context",
                allowed=False,
                stage="policy_pipeline",
                reason_code=POLICY_CONTEXT_PHASE_MISMATCH,
                message="Policy context is not an exact canonical context type.",
                evidence={
                    "pipeline_phase": evidence_phase,
                    "context_type": _safe_type_name(context),
                },
                stop_required=True,
                phase=normalized_phase,
                mode="foundation",
            )
        context_matches_phase = (
            pipeline_phase == "pre_execution" and type(context) is PolicyContext
        ) or (
            pipeline_phase == "post_execution"
            and type(context) is PostExecutionPolicyContext
        )
        if not context_matches_phase:
            return PolicyDecision(
                capability_name="invalid_context",
                allowed=False,
                stage="policy_pipeline",
                reason_code=POLICY_CONTEXT_PHASE_MISMATCH,
                message="Policy context does not match the selected pipeline phase.",
                evidence={
                    "pipeline_phase": evidence_phase,
                    "context_type": _safe_type_name(context),
                },
                stop_required=True,
                phase=normalized_phase,
                mode="foundation",
            )
        try:
            if type(context) is PolicyContext:
                context = _detached_policy_context_copy(context)
            else:
                trusted_pipeline = object.__getattribute__(
                    context,
                    "_trusted_pre_execution_pipeline",
                )
                trusted_issuer = object.__getattribute__(trusted_pipeline, "_issuer")
                evaluation_grant = object.__getattribute__(context, "_evaluation_grant")
                if (
                    type(trusted_issuer) is not _PreExecutionAllowIssuer
                    or not trusted_issuer._authority_verified(trusted_pipeline)
                    or type(evaluation_grant) is not _PostExecutionSnapshotGrant
                    or object.__getattribute__(evaluation_grant, "_issuer")
                    is not trusted_issuer
                ):
                    raise ValueError("Post-execution context ownership is invalid.")
                restore_lifecycle: tuple[FunctionType, CodeType] | None = None
                restore_lifecycle_invalid = False
                for entry in trusted_issuer.__class__.__base__.__iter__(
                    trusted_issuer
                ):
                    if lifecycle_exact_type(entry) is not lifecycle_tuple_type:
                        continue
                    if lifecycle_tuple_type.__len__(entry) != 2:
                        continue
                    entry_name = lifecycle_tuple_type.__getitem__(entry, 0)
                    if (
                        lifecycle_exact_type(entry_name)
                        is not lifecycle_string_type
                        or entry_name != "restore_post"
                    ):
                        continue
                    if restore_lifecycle is not None:
                        restore_lifecycle_invalid = True
                        break
                    seal = lifecycle_tuple_type.__getitem__(entry, 1)
                    if (
                        lifecycle_exact_type(seal) is not lifecycle_tuple_type
                        or lifecycle_tuple_type.__len__(seal) != 4
                    ):
                        restore_lifecycle_invalid = True
                        break
                    marker = lifecycle_tuple_type.__getitem__(seal, 0)
                    lifecycle_callable = lifecycle_tuple_type.__getitem__(seal, 1)
                    sealed_code = lifecycle_tuple_type.__getitem__(seal, 2)
                    sealed_cells = lifecycle_tuple_type.__getitem__(seal, 3)
                    if (
                        lifecycle_exact_type(marker) is not lifecycle_string_type
                        or marker != "callable"
                        or lifecycle_exact_type(lifecycle_callable)
                        is not lifecycle_function_type
                        or lifecycle_exact_type(sealed_code)
                        is not lifecycle_code_type
                        or lifecycle_exact_type(sealed_cells)
                        is not lifecycle_tuple_type
                        or lifecycle_tuple_type.__len__(sealed_cells) != 0
                        or lifecycle_callable.__code__ is not sealed_code
                        or lifecycle_callable.__closure__ is not None
                        or lifecycle_callable.__defaults__ is not None
                        or lifecycle_callable.__kwdefaults__ is not None
                    ):
                        restore_lifecycle_invalid = True
                        break
                    restore_lifecycle = (lifecycle_callable, sealed_code)
                if restore_lifecycle_invalid:
                    restore_lifecycle = None
                restored = (
                    restore_lifecycle[0].__class__(
                        restore_lifecycle[1],
                        restore_lifecycle[0].__globals__,
                        restore_lifecycle[1].co_name,
                    )(
                        trusted_issuer,
                        object.__getattribute__(evaluation_grant, "_serial"),
                        context=context,
                    )
                    if restore_lifecycle is not None
                    else None
                )
                if type(restored) is not PostExecutionPolicyContext:
                    raise ValueError("Post-execution context snapshot is invalid.")
                context = restored
        except Exception as exc:
            return PolicyDecision(
                capability_name="invalid_context",
                allowed=False,
                stage="policy_pipeline",
                reason_code=POLICY_CONTEXT_REVALIDATION_FAILED,
                message="Policy context revalidation failed closed before stage evaluation.",
                evidence={
                    "pipeline_phase": evidence_phase,
                    "context_type": _safe_type_name(supplied_context),
                    "revalidation_error_type": _safe_type_name(exc),
                },
                stop_required=True,
                phase=normalized_phase,
                mode="foundation",
            )
        evaluated: list[PolicyStageResult] = []
        requirements = policy_stage_requirements(context.capability)
        for expected_name, stage in zip(stage_order, registered_stages, strict=True):
            try:
                result = stage.evaluate(context)
            except Exception as exc:
                failure_result = PolicyStageResult(
                    stage=expected_name,
                    allowed=False,
                    reason_code=POLICY_STAGE_EVALUATION_FAILED,
                    message="Policy stage raised an unexpected exception and failed closed.",
                    evidence={"exception_type": _safe_type_name(exc)},
                    stop_required=True,
                )
                evaluated.append(failure_result)
                return self._decision_from_result(context, failure_result, evaluated)
            returned_type = _safe_type_name(result)
            try:
                result = _validated_stage_result_copy(result)
            except Exception as exc:
                invalid_result = PolicyStageResult(
                    stage=expected_name,
                    allowed=False,
                    reason_code=POLICY_STAGE_CONTRACT_VIOLATION,
                    message="Policy stage returned an invalid result and failed closed.",
                    evidence={
                        "returned_type": returned_type,
                        "contract_error_type": _safe_type_name(exc),
                    },
                    stop_required=True,
                )
                evaluated.append(invalid_result)
                return self._decision_from_result(context, invalid_result, evaluated)
            if result.stage != expected_name:
                invalid_result = PolicyStageResult(
                    stage=expected_name,
                    allowed=False,
                    reason_code=POLICY_STAGE_CONTRACT_VIOLATION,
                    message="Policy stage returned a result for the wrong stage and failed closed.",
                    evidence={
                        "returned_type": _safe_type_name(result),
                    },
                    stop_required=True,
                )
                evaluated.append(invalid_result)
                return self._decision_from_result(context, invalid_result, evaluated)
            if requirements[expected_name] and not result.applicable:
                invalid_result = PolicyStageResult(
                    stage=expected_name,
                    allowed=False,
                    reason_code=POLICY_STAGE_CONTRACT_VIOLATION,
                    message="Required policy stage reported itself as not applicable and failed closed.",
                    evidence={"required": True, "reported_applicable": False},
                    stop_required=True,
                )
                evaluated.append(invalid_result)
                return self._decision_from_result(context, invalid_result, evaluated)
            evaluated.append(result)
            if not result.allowed:
                return self._decision_from_result(context, result, evaluated)

        decision = PolicyDecision(
            capability_name=context.capability_name,
            allowed=True,
            stage="policy_pipeline",
            reason_code=POLICY_PIPELINE_ALLOWED,
            message="All registered policy stages allowed the request.",
            evidence={},
            stop_required=False,
            stage_results=tuple(evaluated),
            request_id=context.request_id,
            started_at=context.started_at,
            capability_policy_sha256=context.capability.policy_identity_sha256,
            context_identity_sha256=context.policy_identity_sha256,
            phase=pipeline_phase,
            mode="foundation",
        )
        if (
            pipeline_phase == "pre_execution"
            and type(context) is PolicyContext
            and type(self._issuer) is _PreExecutionAllowIssuer
        ):
            provenance = (
                issue_lifecycle[0].__class__(
                    issue_lifecycle[1],
                    issue_lifecycle[0].__globals__,
                    issue_lifecycle[1].co_name,
                )(
                    self._issuer,
                    decision,
                    supplied_context,
                    pipeline=self,
                    evaluated_context=context,
                )
                if issue_lifecycle is not None
                else None
            )
            if type(provenance) is not _PreExecutionAllowGrant:
                return PolicyDecision(
                    capability_name=context.capability_name,
                    allowed=False,
                    stage="policy_pipeline",
                    reason_code=POLICY_PIPELINE_CONFIGURATION_INVALID,
                    message="Trusted policy authorization issuer ownership is invalid.",
                    evidence={"issuer_ownership_valid": False},
                    stop_required=True,
                    stage_results=tuple(evaluated),
                    request_id=context.request_id,
                    started_at=context.started_at,
                    capability_policy_sha256=context.capability.policy_identity_sha256,
                    context_identity_sha256=context.policy_identity_sha256,
                    phase=self.phase,
                    mode="foundation",
                )
            object.__setattr__(
                decision,
                "_provenance",
                provenance,
            )
        return decision
    @staticmethod
    def _decision_from_result(
        context: PolicyEvaluationContext,
        result: PolicyStageResult,
        evaluated: list[PolicyStageResult],
    ) -> PolicyDecision:
        return PolicyDecision(
            capability_name=context.capability_name,
            allowed=result.allowed,
            stage=result.stage,
            reason_code=result.reason_code,
            message=result.message,
            evidence=result.evidence,
            stop_required=result.stop_required,
            stage_results=tuple(evaluated),
            request_id=context.request_id,
            started_at=context.started_at,
            capability_policy_sha256=context.capability.policy_identity_sha256,
            context_identity_sha256=context.policy_identity_sha256,
            phase=(
                "post_execution"
                if result.stage in POST_EXECUTION_POLICY_STAGE_ORDER
                else "pre_execution"
            ),
            mode="foundation",
        )


@dataclass(frozen=True)
class _UnavailablePreExecutionAuthorityStage:
    name: str

    def evaluate(self, context: PolicyEvaluationContext) -> PolicyStageResult:
        return PolicyStageResult(
            stage=self.name,
            allowed=False,
            reason_code=POLICY_PIPELINE_AUTHORITY_UNAVAILABLE,
            message="Authoritative policy composition is not connected in the foundation phase.",
            evidence={"foundation_only": True},
            stop_required=True,
        )


def _bootstrap_canonical_pre_execution_pipeline() -> PolicyPipeline:
    stages = tuple(
        _UnavailablePreExecutionAuthorityStage(name)
        for name in PRE_EXECUTION_POLICY_STAGE_ORDER
    )
    pipeline = PolicyPipeline(stages, phase="pre_execution")
    issuer = _PreExecutionAllowIssuer(
        trusted_pipeline=pipeline,
        trusted_evaluation_code=PolicyPipeline.evaluate.__code__,
        trusted_post_context_code=PostExecutionPolicyContext.__post_init__.__code__,
        _factory_key=_TRUSTED_PRE_EXECUTION_ISSUER_KEY,
    )
    object.__setattr__(pipeline, "_issuer", issuer)
    _CANONICAL_AUTHORITY_SEAL(pipeline, issuer)
    return pipeline


_CANONICAL_PRE_EXECUTION_PIPELINE = _bootstrap_canonical_pre_execution_pipeline()
del _bootstrap_canonical_pre_execution_pipeline
del _TRUSTED_PRE_EXECUTION_ISSUER_KEY
del _CANONICAL_AUTHORITY_SEAL
del _CANONICAL_AUTHORITY_VERIFY
del _CANONICAL_AUTHORITY_RUNTIME


def _is_path_argument_name(name: str) -> bool:
    normalized = name.lower()
    return normalized in _PATH_ARGUMENT_NAMES or normalized.endswith(
        ("_path", "_paths", "_dir", "_dirs", "_files")
    )


def _input_schema_requires_path_boundary(schema: Mapping[str, Any]) -> bool:
    all_of_neutral_keywords = {"if", "then", "else", "not"}
    all_of_annotation_keywords = {
        "$anchor",
        "$comment",
        "$dynamicAnchor",
        "$id",
        "$schema",
        "default",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
    scalar_constraint_keywords = {
        "contentEncoding",
        "contentMediaType",
        "contentSchema",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "maxLength",
        "maximum",
        "minLength",
        "minimum",
        "multipleOf",
        "pattern",
    }
    general_applicator_keywords = {
        "$dynamicRef",
        "$recursiveRef",
        "$ref",
        "allOf",
        "anyOf",
        "else",
        "if",
        "not",
        "oneOf",
        "then",
    }

    def neutral_all_of_branch(candidate: Any) -> bool:
        if candidate is True:
            return True
        if not isinstance(candidate, Mapping):
            return False
        if not candidate:
            return True
        if candidate.get("not") is True:
            return False
        if (
            candidate.get("if") is True
            and candidate.get("then") is False
        ) or (
            candidate.get("if") is False
            and candidate.get("else") is False
        ):
            return False
        meaningful = set(candidate) - all_of_annotation_keywords
        if meaningful == {"propertyNames"}:
            return candidate.get("propertyNames") is True
        return bool(meaningful) and meaningful <= all_of_neutral_keywords and all(
            type(candidate.get(key)) is bool for key in meaningful
        )

    def fixed_json_value_requires_path_boundary(value: Any) -> bool:
        pending = [value]
        visited = 0
        while pending:
            visited += 1
            if visited > 4_096:
                return True
            candidate = pending.pop()
            if isinstance(candidate, Mapping):
                for key, child in candidate.items():
                    if type(key) is not str or _is_path_argument_name(key):
                        return True
                    pending.append(child)
                continue
            if isinstance(candidate, Sequence) and not isinstance(
                candidate,
                (str, bytes, bytearray),
            ):
                pending.extend(candidate)
        return False

    def subschema_may_accept_path(value: Any) -> bool:
        if value is False:
            return False
        if value is True:
            return True
        if isinstance(value, Mapping):
            return _input_schema_requires_path_boundary(value)
        return True

    def conditional_branch_may_accept_path(
        base_schema: Mapping[str, Any],
        branch_schema: Any,
    ) -> bool:
        if branch_schema is False:
            return False
        if branch_schema is True:
            return subschema_may_accept_path(base_schema)
        if not isinstance(branch_schema, Mapping):
            return True
        if not base_schema:
            return subschema_may_accept_path(branch_schema)
        return subschema_may_accept_path(
            {"allOf": [base_schema, branch_schema]}
        )

    stack: list[Any] = [schema]
    visited_nodes = 0
    while stack:
        visited_nodes += 1
        if visited_nodes > 4_096:
            return True
        node = stack.pop()
        if node is True:
            return True
        if node is False:
            continue
        if not isinstance(node, Mapping):
            continue
        if not node:
            return True
        all_of_children = node.get("allOf")
        if isinstance(all_of_children, (list, tuple)) and all(
            type(child) is bool or isinstance(child, Mapping)
            for child in all_of_children
        ):
            if any(child is False for child in all_of_children):
                continue
            substantive_children = [
                child
                for child in all_of_children
                if not neutral_all_of_branch(child)
            ]
            if not substantive_children:
                stack.append(
                    {
                        key: value
                        for key, value in node.items()
                        if key != "allOf"
                    }
                )
                continue
            if len(substantive_children) != len(all_of_children):
                node = {**node, "allOf": substantive_children}
                all_of_children = substantive_children
            combined_node = {
                key: value for key, value in node.items() if key != "allOf"
            }
            mergeable = not (
                set(combined_node) & all_of_neutral_keywords
            ) and not any(
                isinstance(child, Mapping)
                and bool(set(child) & all_of_neutral_keywords)
                for child in all_of_children
            )
            for child in all_of_children:
                if child is True or not child:
                    continue
                assert isinstance(child, Mapping)
                if set(combined_node) & set(child):
                    mergeable = False
                    break
                combined_node.update(child)
            if mergeable:
                stack.append(combined_node)
                continue
        if node.get("not") is True:
            continue
        negated_schema = node.get("not")
        if isinstance(negated_schema, Mapping):
            if not negated_schema:
                continue
            return True
        if "if" in node and ("then" in node or "else" in node):
            condition = node.get("if")
            if not (type(condition) is bool or isinstance(condition, Mapping)):
                return True
            base_schema = {
                key: value
                for key, value in node.items()
                if key not in {"if", "then", "else"}
            }
            then_schema = node.get("then", True)
            else_schema = node.get("else", True)
            if condition is True or (
                isinstance(condition, Mapping) and not condition
            ):
                if conditional_branch_may_accept_path(base_schema, then_schema):
                    return True
                continue
            if condition is False:
                if conditional_branch_may_accept_path(base_schema, else_schema):
                    return True
                continue
            if conditional_branch_may_accept_path(
                base_schema,
                then_schema,
            ) or conditional_branch_may_accept_path(base_schema, else_schema):
                return True
            continue

        neutral_boolean_keywords = {
            key
            for key in ("if", "then", "else", "not", "propertyNames")
            if type(node.get(key)) is bool
        }
        annotation_keywords = {
            "$anchor",
            "$comment",
            "$dynamicAnchor",
            "$id",
            "$schema",
            "default",
            "deprecated",
            "description",
            "examples",
            "readOnly",
            "title",
            "writeOnly",
        }
        noop_validation_keywords: set[str] = set()
        inactive_conditional_keywords: set[str] = set()
        if "if" in node and "then" not in node and "else" not in node:
            inactive_conditional_keywords.add("if")
        if "if" not in node:
            inactive_conditional_keywords.update(
                key for key in ("then", "else") if key in node
            )
        noop_validation_keywords.update(inactive_conditional_keywords)
        if node.get("required") in ([], ()):
            noop_validation_keywords.add("required")
        for key in ("minContains", "minItems", "minLength", "minProperties"):
            if node.get(key) == 0:
                noop_validation_keywords.add(key)
        if node.get("uniqueItems") is False:
            noop_validation_keywords.add("uniqueItems")
        for key in (
            "dependencies",
            "dependentRequired",
            "dependentSchemas",
            "patternProperties",
            "properties",
        ):
            if node.get(key) == {}:
                noop_validation_keywords.add(key)
        if node.get("allOf") in ([], ()):
            noop_validation_keywords.add("allOf")
        if not (
            set(node)
            - neutral_boolean_keywords
            - annotation_keywords
            - noop_validation_keywords
        ):
            return True

        if "const" in node:
            if fixed_json_value_requires_path_boundary(node.get("const")):
                return True
            continue
        enum_values = node.get("enum")
        if (
            isinstance(enum_values, Sequence)
            and not isinstance(enum_values, (str, bytes, bytearray))
        ):
            if any(
                fixed_json_value_requires_path_boundary(value)
                for value in enum_values
            ):
                return True
            continue

        declared_type = node.get("type")
        declared_object_type = declared_type == "object" or (
            isinstance(declared_type, Sequence)
            and not isinstance(declared_type, (str, bytes, bytearray))
            and "object" in declared_type
        )
        declared_array_type = declared_type == "array" or (
            isinstance(declared_type, Sequence)
            and not isinstance(declared_type, (str, bytes, bytearray))
            and "array" in declared_type
        )
        object_instances_possible = declared_type is None or declared_object_type
        array_instances_possible = declared_type is None or declared_array_type
        object_forces_empty = (
            node.get("propertyNames") is False
            or (
                type(node.get("maxProperties")) is int
                and node.get("maxProperties") <= 0
            )
        )
        if (
            object_forces_empty
            and declared_type is not None
            and not declared_array_type
        ):
            continue
        object_properties_reachable = (
            object_instances_possible and not object_forces_empty
        )
        object_schema = (
            declared_object_type
            or (
                declared_type is None
                and any(
                    key in node
                    for key in (
                        "dependencies",
                        "dependentRequired",
                        "dependentSchemas",
                        "maxProperties",
                        "minProperties",
                        "properties",
                        "patternProperties",
                        "additionalProperties",
                        "propertyNames",
                        "required",
                        "unevaluatedProperties",
                    )
                )
            )
        )
        if (
            object_schema
            and object_properties_reachable
            and "additionalProperties" not in node
            and "unevaluatedProperties" not in node
        ):
            return True

        properties = node.get("properties")
        if object_properties_reachable and isinstance(properties, Mapping):
            for name, child in properties.items():
                if type(name) is not str or _is_path_argument_name(name):
                    return True
                stack.append(child)

        pattern_properties = node.get("patternProperties")
        if (
            object_properties_reachable
            and isinstance(pattern_properties, Mapping)
            and pattern_properties
        ):
            return True

        additional_properties = node.get("additionalProperties")
        if object_properties_reachable and (
            additional_properties is True
            or isinstance(additional_properties, Mapping)
        ):
            return True
        unevaluated_properties = node.get("unevaluatedProperties")
        if (
            object_properties_reachable
            and unevaluated_properties is True
            and "additionalProperties" not in node
        ):
            return True

        reference = node.get("$ref")
        if type(reference) is str and not reference.startswith("#/"):
            return True
        if any(
            type(node.get(keyword)) is str
            for keyword in ("$dynamicRef", "$recursiveRef")
        ):
            return True

        array_keywords = {
            "contains",
            "items",
            "maxContains",
            "maxItems",
            "minContains",
            "minItems",
            "prefixItems",
            "unevaluatedItems",
            "uniqueItems",
        }
        object_constraint_keywords = {
            "additionalProperties",
            "maxProperties",
            "minProperties",
            "patternProperties",
            "properties",
            "propertyNames",
            "required",
            "unevaluatedProperties",
        }
        object_is_open = object_schema and object_properties_reachable and (
            "additionalProperties" not in node
            and "unevaluatedProperties" not in node
        )
        if (
            declared_type is None
            and not object_schema
            and any(key in node for key in scalar_constraint_keywords)
            and not any(key in node for key in general_applicator_keywords)
        ):
            return True
        if (
            declared_type is None
            and any(key in node for key in array_keywords)
            and (
                object_is_open
                or not any(key in node for key in object_constraint_keywords)
            )
        ):
            return True

        max_items = node.get("maxItems")
        contains_schema = node.get("contains")
        max_contains = node.get("maxContains")
        contains_forces_empty = (
            contains_schema is True
            and type(max_contains) is int
            and max_contains <= 0
        )
        prefix_items = node.get("prefixItems")
        prefix_item_count = (
            len(prefix_items) if isinstance(prefix_items, (list, tuple)) else 0
        )
        first_false_prefix = next(
            (
                index
                for index, child in enumerate(prefix_items)
                if child is False
            ),
            None,
        ) if isinstance(prefix_items, (list, tuple)) else None
        values_possible = not (
            (type(max_items) is int and max_items <= 0)
            or contains_forces_empty
            or first_false_prefix == 0
        )
        tail_bounded_by_prefix = (
            type(max_items) is int and max_items <= prefix_item_count
        ) or first_false_prefix is not None
        min_contains = node.get("minContains", 1)
        min_items = node.get("minItems", 0)
        array_cardinality_is_unsatisfiable = (
            type(min_items) is int
            and type(max_items) is int
            and min_items > max_items
        )
        contains_is_unsatisfiable = contains_schema is False and not (
            type(min_contains) is int and min_contains <= 0
        )
        contains_bounds_are_unsatisfiable = (
            "contains" in node
            and type(min_contains) is int
            and type(max_contains) is int
            and min_contains > max_contains
        )
        array_is_unsatisfiable = (
            contains_is_unsatisfiable
            or contains_bounds_are_unsatisfiable
            or array_cardinality_is_unsatisfiable
        )
        if array_is_unsatisfiable:
            array_instances_possible = False
            if declared_array_type and not declared_object_type:
                continue
        unevaluated_items = node.get("unevaluatedItems")
        array_tail_reachable = (
            "items" not in node
            and values_possible
            and not tail_bounded_by_prefix
        )
        open_array_tail = (
            array_tail_reachable
            and (
                unevaluated_items is None
                or unevaluated_items is True
                or contains_schema is True
            )
        )
        if (
            array_instances_possible
            and (
                declared_array_type
                or (declared_type is None and object_schema)
            )
        ) and open_array_tail:
            return True

        items_schema = node.get("items")
        if (
            array_instances_possible
            and items_schema is True
            and values_possible
            and not tail_bounded_by_prefix
        ):
            return True
        if (
            array_instances_possible
            and values_possible
            and not tail_bounded_by_prefix
            and isinstance(items_schema, Mapping)
        ):
            stack.append(items_schema)

        contains_can_match = not (
            type(max_contains) is int and max_contains <= 0
        )
        if (
            array_instances_possible
            and contains_schema is True
            and open_array_tail
            and contains_can_match
        ):
            return True
        if (
            array_instances_possible
            and values_possible
            and contains_can_match
            and isinstance(contains_schema, Mapping)
        ):
            stack.append(contains_schema)

        if array_instances_possible and unevaluated_items is True and open_array_tail:
            return True
        if (
            array_instances_possible
            and array_tail_reachable
            and isinstance(unevaluated_items, Mapping)
        ):
            stack.append(unevaluated_items)

        if (
            object_properties_reachable
            and "additionalProperties" not in node
            and isinstance(
                unevaluated_properties,
                Mapping,
            )
        ):
            return True

        for key in ("not", "if", "then", "else"):
            if key in inactive_conditional_keywords:
                continue
            child = node.get(key)
            if isinstance(child, Mapping):
                stack.append(child)

        property_names = node.get("propertyNames")
        if object_properties_reachable and isinstance(property_names, Mapping):
            stack.append(property_names)

        for key in ("allOf", "anyOf", "oneOf"):
            children = node.get(key)
            if isinstance(children, (list, tuple)):
                stack.extend(children)

        if array_instances_possible and values_possible:
            prefix_children = node.get("prefixItems")
            if isinstance(prefix_children, (list, tuple)):
                reachable_prefix_count = len(prefix_children)
                if type(max_items) is int:
                    reachable_prefix_count = max(
                        0,
                        min(reachable_prefix_count, max_items),
                    )
                if first_false_prefix is not None:
                    reachable_prefix_count = min(
                        reachable_prefix_count,
                        first_false_prefix,
                    )
                stack.extend(prefix_children[:reachable_prefix_count])

        for key in ("$defs", "definitions"):
            children = node.get(key)
            if isinstance(children, Mapping):
                stack.extend(children.values())
        if object_properties_reachable:
            for key in ("dependencies", "dependentSchemas"):
                children = node.get(key)
                if isinstance(children, Mapping):
                    stack.extend(children.values())
    return False


def policy_stage_requirements(capability: CapabilityPolicySource) -> Mapping[str, bool]:
    """Project CapabilitySpec metadata onto the fixed policy stage contract."""

    capability = _snapshot_capability(capability)
    input_schema = capability.input_schema
    project_identity_required = "project_identity" in capability.evidence_contract
    path_required = bool(
        capability.required_project_state != "none"
        or project_identity_required
        or _input_schema_requires_path_boundary(input_schema)
    )
    session_identity_required = bool(
        capability.required_session_state != "none"
        or capability.required_project_state in {"project_open", "managed_project_open"}
        or capability.mutation
        or capability.destructive
        or capability.hardware
        or capability.risk_class == "project_execution"
        or capability.risk_class == "tcl_policy_dry_run"
        or capability.execution_input_policy == "blocks_unattested_composite"
        or capability.name in _SESSION_IDENTITY_ENTRY_TOOLS
    )
    project_capability_required = bool(
        capability.required_project_state != "none"
        or project_identity_required
        or capability.risk_class == "project_execution"
        or capability.execution_input_policy == "blocks_unattested_composite"
    )
    hardware_intent_required = bool(
        capability.hardware
        or capability.hardware_tier != "not_hardware"
        or capability.risk_class in {"hardware", "hardware_destructive"}
    )
    evidence_required = bool(
        session_identity_required
        or project_capability_required
        or hardware_intent_required
        or set(capability.evidence_contract) - {"unified_result_v1"}
        or tuple(capability.artifact_contract) != ("none",)
    )
    return MappingProxyType({
        "executable_session_identity": session_identity_required,
        "argument_schema": True,
        "capability_profile_authorization": True,
        "managed_path_boundary": path_required,
        "project_capability_generation": project_capability_required,
        "mutation_destructive_intent": (
            capability.mutation
            or capability.destructive
            or capability.risk_class
            in {
                "destructive_dry_run",
                "hardware_destructive",
                "project_execution",
                "project_mutation_immediate",
            }
        ),
        "hardware_programming_intent": hardware_intent_required,
        "execution_input_closure": (
            capability.execution_input_policy == "blocks_unattested_composite"
            or capability.risk_class == "project_execution"
        ),
        "command_execution_boundary": capability.dispatch_lane == "serialized_backend",
        "evidence_freshness_identity": evidence_required,
        "audit_signoff_terminal_semantics": capability.name in _AUDIT_TERMINAL_TOOLS,
    })


def policy_stage_manifest(capabilities: Sequence[CapabilityPolicySource]) -> dict[str, Any]:
    """Return a deterministic audit manifest for capability-to-stage coverage."""

    capability_records: list[dict[str, Any]] = []
    names: set[str] = set()
    for capability_source in capabilities:
        capability = _snapshot_capability(capability_source)
        if capability.name in names:
            raise ValueError(f"Duplicate capability in policy manifest: {capability.name}")
        names.add(capability.name)
        requirements = policy_stage_requirements(capability)
        capability_records.append(
            {
                "name": capability.name,
                "required_stages": [name for name in POLICY_STAGE_ORDER if requirements[name]],
                "not_applicable_stages": [name for name in POLICY_STAGE_ORDER if not requirements[name]],
            }
        )
    payload = {
        "schema_version": POLICY_PIPELINE_VERSION,
        "stage_order": list(POLICY_STAGE_ORDER),
        "pre_execution_stage_order": list(PRE_EXECUTION_POLICY_STAGE_ORDER),
        "post_execution_stage_order": list(POST_EXECUTION_POLICY_STAGE_ORDER),
        "capability_count": len(capability_records),
        "capabilities": capability_records,
    }
    payload["manifest_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload
