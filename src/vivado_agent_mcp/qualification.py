from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any


QUALIFICATION_SCHEMA_VERSION = "1.0"
QUALIFICATION_MATRIX_SCHEMA_VERSION = 1
PUBLIC_EVIDENCE_SCHEMA_VERSION = 1
QUALIFICATION_FIXTURE_ID = "minimal-counter-v1"

QUALIFICATION_STATUSES = {"trusted", "qualified", "compatible", "unvalidated", "rejected"}
TERMINAL_STATUSES = {"PASS", "BLOCK", "SKIPPED", "UNAVAILABLE", "INTERRUPTED"}
SOFTWARE_STATUSES = {"TRUSTED", "QUALIFIED", "COMPATIBLE", "UNVALIDATED", "REJECTED"}
VIVADO_IDENTITY_STATUSES = {"VERIFIED", "UNAVAILABLE", "UNATTESTED", "MISMATCH"}
EVIDENCE_FRESHNESS_STATUSES = {"FRESH", "STALE", "MISSING"}

_FIXTURE_FILES = (
    "qualification_counter.sv",
    "tb_qualification_counter.sv",
    "qualification_counter.xdc",
)
_REQUIRED_EVIDENCE = (
    "scenario_result",
    "artifact_manifest",
    "report_manifest",
    "audit_result",
    "diagnostic_manifest",
)
_PUBLIC_EVIDENCE_FILENAMES = {name: f"{name}.json" for name in _REQUIRED_EVIDENCE}
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_GIT_OBJECT_PATTERN = re.compile(r"[0-9a-f]{40,64}")
_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(r"(?i)[a-z]:[\\/]")
_WINDOWS_UNC_PATH_PATTERN = re.compile(r"\\\\[^\\/\s]+[\\/]")
_POSIX_USER_PATH_PATTERN = re.compile(r"(?i)/(?:home|users)/")
_EMAIL_PATTERN = re.compile(r"(?i)\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_LOCAL_IDENTITY_LABEL_PATTERN = re.compile(
    r"(?i)\b(?:computer(?:_name)?|host(?:name)?|local_user|login|machine(?:_name)?|os_user|user(?:name)?)"
    r"\s*[:=]\s*\S+"
)
_VMCP_ENCODED_PATH_PATTERN = re.compile(r"(?i)\bvmcp_hex_row_v1:(?:file|path)=")
_LOCAL_IDENTITY_FIELD_NAMES = frozenset(
    {
        "computername",
        "fileid",
        "fileidentity",
        "fileindex",
        "host",
        "hostname",
        "inode",
        "keyid",
        "localuser",
        "login",
        "machine",
        "machinename",
        "osuser",
        "owner",
        "ownername",
        "sourcefileid",
        "trustanchorid",
        "user",
        "username",
        "volumeid",
        "volumeserial",
    }
)


def qualification_record_schema() -> dict[str, Any]:
    digest = {"type": "string", "pattern": "^(?:|[0-9a-f]{64})$"}
    required_digest = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/zzszzs-lll/vivado-agent-mcp/qualification/qualification-record.schema.json",
        "title": "Vivado Agent MCP commit-bound qualification record",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "record_id",
            "generated_at",
            "qualification_status",
            "terminal",
            "source",
            "package",
            "vivado",
            "runner",
            "fixture",
            "execution",
            "evidence",
            "software_validation",
            "hardware_validation",
        ],
        "properties": {
            "schema_version": {"const": QUALIFICATION_SCHEMA_VERSION},
            "record_id": required_digest,
            "generated_at": {"type": "string", "format": "date-time"},
            "qualification_status": {"type": "string", "enum": sorted(QUALIFICATION_STATUSES)},
            "terminal": {
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "reason_code", "message", "started_at", "ended_at"],
                "properties": {
                    "status": {"type": "string", "enum": sorted(TERMINAL_STATUSES)},
                    "reason_code": {"type": "string", "minLength": 1},
                    "message": {"type": "string"},
                    "started_at": {"type": "string", "format": "date-time"},
                    "ended_at": {"type": "string", "format": "date-time"},
                },
            },
            "source": {
                "type": "object",
                "additionalProperties": False,
                "required": ["commit", "tree", "dirty", "tracked_digest", "source_archive_sha256"],
                "properties": {
                    "commit": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
                    "tree": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
                    "dirty": {"type": "boolean"},
                    "tracked_digest": required_digest,
                    "source_archive_sha256": digest,
                },
            },
            "package": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "version", "wheel", "sdist", "source_wheel_pair_id", "provenance_verified"],
                "properties": {
                    "name": {"const": "vivado-agent-mcp"},
                    "version": {"type": "string", "minLength": 1},
                    "wheel": {"$ref": "#/$defs/packageArtifact"},
                    "sdist": {"$ref": "#/$defs/packageArtifact"},
                    "source_wheel_pair_id": digest,
                    "provenance_verified": {"type": "boolean"},
                },
            },
            "vivado": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "identity_status",
                    "canonical_path_sha256",
                    "executable_sha256",
                    "file_identity",
                    "version",
                    "full_version",
                    "build",
                    "version_attested",
                ],
                "properties": {
                    "identity_status": {"type": "string", "enum": sorted(VIVADO_IDENTITY_STATUSES)},
                    "canonical_path_sha256": digest,
                    "executable_sha256": digest,
                    "file_identity": {"type": "object"},
                    "version": {"type": "string"},
                    "full_version": {"type": "string"},
                    "build": {"type": "string"},
                    "version_attested": {"type": "boolean"},
                },
            },
            "runner": {
                "type": "object",
                "additionalProperties": False,
                "required": ["os", "os_version", "architecture", "python_version", "runner_class", "identity_sha256"],
                "properties": {
                    "os": {"type": "string", "minLength": 1},
                    "os_version": {"type": "string", "minLength": 1},
                    "architecture": {"type": "string", "minLength": 1},
                    "python_version": {"type": "string", "minLength": 1},
                    "runner_class": {"type": "string", "minLength": 1},
                    "identity_sha256": required_digest,
                },
            },
            "fixture": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "aggregate_sha256", "files"],
                "properties": {
                    "id": {"const": QUALIFICATION_FIXTURE_ID},
                    "aggregate_sha256": required_digest,
                    "files": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["path", "size", "sha256"],
                            "properties": {
                                "path": {"type": "string", "minLength": 1},
                                "size": {"type": "integer", "minimum": 1},
                                "sha256": required_digest,
                            },
                        },
                    },
                },
            },
            "execution": {
                "type": "object",
                "additionalProperties": False,
                "required": ["tool_profile", "policy_version", "workflow_version", "scenario_id", "scenario_version"],
                "properties": {
                    "tool_profile": {"type": "string", "minLength": 1},
                    "policy_version": {"type": "string", "minLength": 1},
                    "workflow_version": {"type": "string", "minLength": 1},
                    "scenario_id": {"type": "string", "minLength": 1},
                    "scenario_version": {"type": "string", "minLength": 1},
                },
            },
            "evidence": {
                "type": "object",
                "additionalProperties": False,
                "required": ["freshness", "normalized_evidence_sha256", *_REQUIRED_EVIDENCE],
                "properties": {
                    "freshness": {"type": "string", "enum": sorted(EVIDENCE_FRESHNESS_STATUSES)},
                    "normalized_evidence_sha256": digest,
                    **{name: {"$ref": "#/$defs/evidenceEntry"} for name in _REQUIRED_EVIDENCE},
                },
            },
            "software_validation": {"$ref": "#/$defs/validationBoundary"},
            "hardware_validation": {"$ref": "#/$defs/validationBoundary"},
        },
        "$defs": {
            "packageArtifact": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "sha256"],
                "properties": {
                    "name": {"type": "string"},
                    "sha256": digest,
                },
            },
            "evidenceEntry": {
                "type": "object",
                "additionalProperties": False,
                "required": ["size", "sha256"],
                "properties": {
                    "size": {"type": "integer", "minimum": 0},
                    "sha256": digest,
                },
            },
            "validationBoundary": {
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "validated", "scope"],
                "properties": {
                    "status": {"type": "string", "minLength": 1},
                    "validated": {"type": "boolean"},
                    "scope": {"type": "string", "minLength": 1},
                },
            },
        },
    }


def build_qualification_record(
    *,
    qualification_status: str,
    terminal_status: str,
    reason_code: str,
    message: str,
    started_at: str,
    ended_at: str,
    generated_at: str,
    source: dict[str, Any],
    package: dict[str, Any],
    vivado: dict[str, Any],
    runner: dict[str, Any],
    fixture: dict[str, Any],
    execution: dict[str, Any],
    evidence: dict[str, Any],
    software_validation: dict[str, Any],
    hardware_validation: dict[str, Any],
) -> dict[str, Any]:
    record = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "record_id": "",
        "generated_at": generated_at,
        "qualification_status": qualification_status,
        "terminal": {
            "status": terminal_status,
            "reason_code": reason_code,
            "message": message,
            "started_at": started_at,
            "ended_at": ended_at,
        },
        "source": deepcopy(source),
        "package": deepcopy(package),
        "vivado": deepcopy(vivado),
        "runner": deepcopy(runner),
        "fixture": deepcopy(fixture),
        "execution": deepcopy(execution),
        "evidence": deepcopy(evidence),
        "software_validation": deepcopy(software_validation),
        "hardware_validation": deepcopy(hardware_validation),
    }
    return seal_qualification_record(record)


def seal_qualification_record(record: dict[str, Any]) -> dict[str, Any]:
    sealed = deepcopy(record)
    sealed["schema_version"] = QUALIFICATION_SCHEMA_VERSION
    sealed["record_id"] = _record_digest(sealed)
    return sealed


def validate_qualification_record(
    record: Any,
    *,
    expected: dict[str, str] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    details: list[dict[str, str]] = []

    def add(reason_code: str, path: str, message: str) -> None:
        if reason_code not in reasons:
            reasons.append(reason_code)
        details.append({"reason_code": reason_code, "path": path, "message": message})

    if not isinstance(record, dict):
        add("QUALIFICATION_SCHEMA_TYPE_MISMATCH", "$", "qualification record must be an object")
        return _validation_result(record, reasons, details)

    required_top = set(qualification_record_schema()["required"])
    for field in sorted(required_top - set(record)):
        add("QUALIFICATION_SCHEMA_REQUIRED_FIELD_MISSING", field, "required top-level field is missing")
    for field in sorted(set(record) - required_top):
        add("QUALIFICATION_SCHEMA_ADDITIONAL_PROPERTY", field, "unexpected top-level field is not allowed")
    if reasons:
        return _validation_result(record, reasons, details)

    if record.get("schema_version") != QUALIFICATION_SCHEMA_VERSION:
        add("QUALIFICATION_SCHEMA_VERSION_UNSUPPORTED", "schema_version", "schema version is not supported")
    qualification_status = str(record.get("qualification_status", ""))
    if qualification_status not in QUALIFICATION_STATUSES:
        add("QUALIFICATION_STATUS_INVALID", "qualification_status", "qualification status is not recognized")

    terminal = _mapping(record.get("terminal"))
    source = _mapping(record.get("source"))
    package = _mapping(record.get("package"))
    vivado = _mapping(record.get("vivado"))
    runner = _mapping(record.get("runner"))
    fixture = _mapping(record.get("fixture"))
    execution = _mapping(record.get("execution"))
    evidence = _mapping(record.get("evidence"))
    software = _mapping(record.get("software_validation"))
    hardware = _mapping(record.get("hardware_validation"))

    required_nested = {
        "terminal": {"status", "reason_code", "message", "started_at", "ended_at"},
        "source": {"commit", "tree", "dirty", "tracked_digest", "source_archive_sha256"},
        "package": {"name", "version", "wheel", "sdist", "source_wheel_pair_id", "provenance_verified"},
        "vivado": {
            "identity_status",
            "canonical_path_sha256",
            "executable_sha256",
            "file_identity",
            "version",
            "full_version",
            "build",
            "version_attested",
        },
        "runner": {"os", "os_version", "architecture", "python_version", "runner_class", "identity_sha256"},
        "fixture": {"id", "aggregate_sha256", "files"},
        "execution": {"tool_profile", "policy_version", "workflow_version", "scenario_id", "scenario_version"},
        "evidence": {"freshness", "normalized_evidence_sha256", *_REQUIRED_EVIDENCE},
        "software_validation": {"status", "validated", "scope"},
        "hardware_validation": {"status", "validated", "scope"},
    }
    nested_values = {
        "terminal": terminal,
        "source": source,
        "package": package,
        "vivado": vivado,
        "runner": runner,
        "fixture": fixture,
        "execution": execution,
        "evidence": evidence,
        "software_validation": software,
        "hardware_validation": hardware,
    }
    for name, fields in required_nested.items():
        value = nested_values[name]
        if not value and not isinstance(record.get(name), dict):
            add("QUALIFICATION_SCHEMA_TYPE_MISMATCH", name, "field must be an object")
            continue
        for field in sorted(fields - set(value)):
            add("QUALIFICATION_SCHEMA_REQUIRED_FIELD_MISSING", f"{name}.{field}", "required field is missing")
        for field in sorted(set(value) - fields):
            add("QUALIFICATION_SCHEMA_ADDITIONAL_PROPERTY", f"{name}.{field}", "unexpected field is not allowed")

    for name in ("wheel", "sdist"):
        _validate_exact_keys(_mapping(package.get(name)), {"name", "sha256"}, f"package.{name}", add)
    for name in _REQUIRED_EVIDENCE:
        _validate_exact_keys(_mapping(evidence.get(name)), {"size", "sha256"}, f"evidence.{name}", add)
    for index, item in enumerate(fixture.get("files", []) if isinstance(fixture.get("files"), list) else []):
        _validate_exact_keys(_mapping(item), {"path", "size", "sha256"}, f"fixture.files[{index}]", add)

    if terminal.get("status") not in TERMINAL_STATUSES:
        add("QUALIFICATION_TERMINAL_STATUS_INVALID", "terminal.status", "terminal status is not recognized")
    if not str(terminal.get("reason_code", "")):
        add("QUALIFICATION_REASON_CODE_MISSING", "terminal.reason_code", "stable terminal reason code is required")
    _validate_time_order(terminal, record, add)

    if not _GIT_OBJECT_PATTERN.fullmatch(str(source.get("commit", ""))):
        add("QUALIFICATION_SOURCE_IDENTITY_INVALID", "source.commit", "Git commit identity is invalid")
    if not _GIT_OBJECT_PATTERN.fullmatch(str(source.get("tree", ""))):
        add("QUALIFICATION_SOURCE_IDENTITY_INVALID", "source.tree", "Git tree identity is invalid")
    if not _is_sha256(source.get("tracked_digest")):
        add("QUALIFICATION_SOURCE_IDENTITY_INVALID", "source.tracked_digest", "tracked source digest is invalid")
    if source.get("dirty") is not False and qualification_status in {"trusted", "qualified", "compatible"}:
        add("QUALIFICATION_SOURCE_NOT_CLEAN", "source.dirty", "trusted or validated qualification requires a clean source tree")

    if package.get("name") != "vivado-agent-mcp" or not str(package.get("version", "")):
        add("QUALIFICATION_PACKAGE_IDENTITY_INVALID", "package", "package name/version identity is invalid")
    for label in ("wheel", "sdist"):
        artifact = _mapping(package.get(label))
        if not artifact or set(artifact) < {"name", "sha256"}:
            add("QUALIFICATION_SCHEMA_REQUIRED_FIELD_MISSING", f"package.{label}", "package artifact identity is incomplete")
    if vivado.get("identity_status") not in VIVADO_IDENTITY_STATUSES:
        add("QUALIFICATION_VIVADO_IDENTITY_INVALID", "vivado.identity_status", "Vivado identity status is invalid")
    if evidence.get("freshness") not in EVIDENCE_FRESHNESS_STATUSES:
        add("QUALIFICATION_EVIDENCE_FRESHNESS_INVALID", "evidence.freshness", "evidence freshness status is invalid")
    if fixture.get("id") != QUALIFICATION_FIXTURE_ID:
        add("QUALIFICATION_FIXTURE_ID_MISMATCH", "fixture.id", "qualification fixture ID is not supported")
    _validate_fixture(fixture, add)
    _validate_runner(runner, add)

    software_status = str(software.get("status", ""))
    if software_status not in SOFTWARE_STATUSES:
        add("QUALIFICATION_SOFTWARE_STATUS_INVALID", "software_validation.status", "software status is invalid")
    if hardware.get("status") != "NOT_VALIDATED" or hardware.get("validated") is not False:
        add(
            "QUALIFICATION_HARDWARE_BOUNDARY_VIOLATION",
            "hardware_validation",
            "real FPGA/JTAG validation must remain NOT_VALIDATED in this qualification schema",
        )
    _validate_status_consistency(qualification_status, terminal, software, add)

    if qualification_status == "qualified":
        _validate_qualified_record(source, package, vivado, evidence, add)

    expected_values = expected or {}
    observed = {
        "commit": str(source.get("commit", "")),
        "source_archive_sha256": str(source.get("source_archive_sha256", "")),
        "wheel_sha256": str(_mapping(package.get("wheel")).get("sha256", "")),
        "sdist_sha256": str(_mapping(package.get("sdist")).get("sha256", "")),
        "vivado_executable_sha256": str(vivado.get("executable_sha256", "")),
        "fixture_sha256": str(fixture.get("aggregate_sha256", "")),
    }
    for key, expected_value in sorted(expected_values.items()):
        if key in observed and str(expected_value).lower() != observed[key].lower():
            add(
                "QUALIFICATION_EXPECTED_IDENTITY_MISMATCH",
                key,
                f"observed identity does not match expected {key}",
            )

    record_id = str(record.get("record_id", ""))
    calculated_record_id = _record_digest(record)
    if not _is_sha256(record_id) or record_id != calculated_record_id:
        add("QUALIFICATION_RECORD_DIGEST_MISMATCH", "record_id", "record digest does not match canonical record content")
    return _validation_result(record, reasons, details, calculated_record_id=calculated_record_id)


def compare_qualification_records(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    first_validation = validate_qualification_record(first)
    second_validation = validate_qualification_record(second)
    if not first_validation["ok"] or not second_validation["ok"]:
        return {
            "comparable": False,
            "comparison_identity_matches": False,
            "normalized_evidence_matches": False,
            "terminal_status_matches": False,
            "first_reason_codes": first_validation["reason_codes"],
            "second_reason_codes": second_validation["reason_codes"],
        }
    first_identity = _comparison_identity(first)
    second_identity = _comparison_identity(second)
    return {
        "comparable": first_identity == second_identity,
        "comparison_identity_matches": first_identity == second_identity,
        "comparison_identity_sha256": first_identity if first_identity == second_identity else "",
        "first_comparison_identity_sha256": first_identity,
        "second_comparison_identity_sha256": second_identity,
        "normalized_evidence_matches": _mapping(first.get("evidence")).get("normalized_evidence_sha256")
        == _mapping(second.get("evidence")).get("normalized_evidence_sha256"),
        "terminal_status_matches": _mapping(first.get("terminal")).get("status")
        == _mapping(second.get("terminal")).get("status"),
        "qualification_status_matches": first.get("qualification_status") == second.get("qualification_status"),
    }


def update_qualification_matrix(matrix: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    original = deepcopy(matrix)
    validation = validate_qualification_record(record)
    if not validation["ok"]:
        return {
            "ok": False,
            "status": "BLOCK",
            "reason_code": "QUALIFICATION_MATRIX_RECORD_INVALID",
            "reason_codes": validation["reason_codes"],
            "matrix": original,
        }
    if matrix.get("schema_version") != QUALIFICATION_MATRIX_SCHEMA_VERSION or not isinstance(matrix.get("entries"), list):
        return {
            "ok": False,
            "status": "BLOCK",
            "reason_code": "QUALIFICATION_MATRIX_SCHEMA_INVALID",
            "reason_codes": ["QUALIFICATION_MATRIX_SCHEMA_INVALID"],
            "matrix": original,
        }
    updated = deepcopy(matrix)
    vivado = _mapping(record.get("vivado"))
    runner = _mapping(record.get("runner"))
    software = _mapping(record.get("software_validation"))
    hardware = _mapping(record.get("hardware_validation"))
    terminal = _mapping(record.get("terminal"))
    target_version = str(vivado.get("version", ""))
    target_platform = str(runner.get("os", "")).lower()
    target = next(
        (
            entry
            for entry in updated["entries"]
            if isinstance(entry, dict)
            and str(entry.get("vivado_version", "")) == target_version
            and str(entry.get("platform", "")).lower() == target_platform
        ),
        None,
    )
    if target is None:
        target = {
            "platform": target_platform,
            "vivado_version": target_version,
            "trust_status": "unvalidated",
            "qualification_status": "unvalidated",
            "software_validation_status": "UNVALIDATED",
            "hardware_validation_status": "NOT_VALIDATED",
            "record_id": "",
            "record_sha256": "",
        }
        updated["entries"].append(target)
    current_status = str(target.get("qualification_status", "unvalidated"))
    next_status = str(record.get("qualification_status", ""))
    if not _matrix_transition_allowed(current_status, next_status):
        return {
            "ok": False,
            "status": "BLOCK",
            "reason_code": "QUALIFICATION_MATRIX_TRANSITION_REJECTED",
            "reason_codes": ["QUALIFICATION_MATRIX_TRANSITION_REJECTED"],
            "matrix": original,
        }
    source_commit = str(_mapping(record.get("source")).get("commit", ""))
    record_id = str(record.get("record_id", ""))
    target.update(
        {
            "qualification_status": next_status,
            "software_validation_status": str(software.get("status", "")),
            "hardware_validation_status": str(hardware.get("status", "")),
            "record_id": record_id,
            "record_sha256": record_id,
            "record_ref": (
                f"qualification/records/{source_commit}/qualification-record.json"
                if source_commit and record_id
                else ""
            ),
            "source_commit": source_commit,
            "vivado_executable_sha256": str(vivado.get("executable_sha256", "")),
            "full_version": str(vivado.get("full_version", "")),
            "terminal_status": str(terminal.get("status", "")),
            "reason_code": str(terminal.get("reason_code", "")),
            "updated_at": str(record.get("generated_at", "")),
            "notes": _matrix_status_notes(
                next_status,
                trust_status=str(target.get("trust_status", "unvalidated")),
                record_id=record_id,
                source_commit=source_commit,
            ),
        }
    )
    updated["entries"] = sorted(
        updated["entries"],
        key=lambda entry: (str(entry.get("platform", "")), str(entry.get("vivado_version", ""))),
    )
    updated["updated_at"] = str(record.get("generated_at", ""))
    return {
        "ok": True,
        "status": "READY",
        "reason_code": "QUALIFICATION_MATRIX_UPDATED",
        "reason_codes": [],
        "matrix": updated,
    }


def write_public_qualification_evidence(
    raw_evidence_paths: Mapping[str, str | Path],
    output_dir: str | Path,
    *,
    source_commit: str,
    hardware_validation: dict[str, Any],
) -> dict[str, Any]:
    if set(raw_evidence_paths) != set(_REQUIRED_EVIDENCE):
        return _public_evidence_failure("PUBLIC_EVIDENCE_SET_INVALID")
    if not _GIT_OBJECT_PATTERN.fullmatch(source_commit):
        return _public_evidence_failure("PUBLIC_EVIDENCE_SOURCE_COMMIT_INVALID")
    if not _hardware_not_validated(hardware_validation):
        return _public_evidence_failure("PUBLIC_EVIDENCE_HARDWARE_BOUNDARY_INVALID")

    requested_destination = Path(output_dir).expanduser().absolute()
    if _path_contains_link_like_component(requested_destination):
        return _public_evidence_failure("PUBLIC_EVIDENCE_DESTINATION_SYMLINK")
    destination = requested_destination.resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        return _public_evidence_failure("PUBLIC_EVIDENCE_DESTINATION_NOT_EMPTY")

    prepared: dict[str, bytes] = {}
    for name in _REQUIRED_EVIDENCE:
        path = Path(raw_evidence_paths[name]).expanduser().resolve()
        try:
            if not path.is_file() or path.is_symlink():
                raise ValueError("evidence is not a regular file")
            raw = path.read_bytes()
            if not raw or len(raw) > 16 * 1024 * 1024:
                raise ValueError("evidence size is outside the public snapshot limit")
            document = json.loads(raw.decode("utf-8"))
            if not isinstance(document, dict):
                raise ValueError("evidence JSON root must be an object")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            return _public_evidence_failure(
                "PUBLIC_EVIDENCE_SOURCE_INVALID",
                evidence_type=name,
                detail=str(exc),
            )
        public_document = {
            "schema_version": PUBLIC_EVIDENCE_SCHEMA_VERSION,
            "evidence_type": name,
            "source_commit": source_commit,
            "raw_evidence": {
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
            "summary": _redact_public_evidence(document),
            "hardware_validation": deepcopy(hardware_validation),
        }
        issues = _public_evidence_document_issues(
            public_document,
            expected_type=name,
            expected_commit=source_commit,
            expected_hardware=hardware_validation,
        )
        if issues:
            return _public_evidence_failure(
                "PUBLIC_EVIDENCE_REDACTION_FAILED",
                evidence_type=name,
                detail=", ".join(issues),
            )
        prepared[name] = json.dumps(public_document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"

    destination.mkdir(parents=True, exist_ok=True)
    if (
        _path_contains_link_like_component(requested_destination)
        or requested_destination.resolve() != destination
    ):
        return _public_evidence_failure("PUBLIC_EVIDENCE_DESTINATION_CHANGED")
    entries: dict[str, dict[str, Any]] = {}
    paths: dict[str, str] = {}
    for name in _REQUIRED_EVIDENCE:
        payload = prepared[name]
        path = destination / _PUBLIC_EVIDENCE_FILENAMES[name]
        path.write_bytes(payload)
        entries[name] = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        paths[name] = str(path)
    normalized = hashlib.sha256(_canonical_json({"evidence": entries})).hexdigest()
    return {
        "ok": True,
        "status": "READY",
        "reason_code": "PUBLIC_EVIDENCE_READY",
        "evidence": {
            "freshness": "FRESH",
            "normalized_evidence_sha256": normalized,
            **entries,
        },
        "paths": paths,
    }


def validate_published_qualification_bundle(record_path: str | Path) -> dict[str, Any]:
    path = Path(record_path).expanduser().resolve()
    issues: list[str] = []
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "status": "BLOCK",
            "reason_code": "PUBLIC_QUALIFICATION_RECORD_UNREADABLE",
            "issues": [str(exc)],
        }
    if not isinstance(record, dict):
        return {
            "ok": False,
            "status": "BLOCK",
            "reason_code": "PUBLIC_QUALIFICATION_RECORD_INVALID",
            "issues": ["record root must be an object"],
        }
    record_validation = validate_qualification_record(record)
    if not record_validation["ok"]:
        issues.extend(record_validation["reason_codes"])

    evidence_dir = path.parent / "public-evidence"
    expected_names = set(_PUBLIC_EVIDENCE_FILENAMES.values())
    actual_items = list(evidence_dir.iterdir()) if evidence_dir.is_dir() and not evidence_dir.is_symlink() else []
    actual_names = {item.name for item in actual_items}
    if (
        evidence_dir.is_symlink()
        or actual_names != expected_names
        or any(not item.is_file() or item.is_symlink() for item in actual_items)
    ):
        issues.append("PUBLIC_EVIDENCE_FILE_SET_INVALID")

    actual_entries: dict[str, dict[str, Any]] = {}
    source_commit = str(_mapping(record.get("source")).get("commit", ""))
    hardware = _mapping(record.get("hardware_validation"))
    record_evidence = _mapping(record.get("evidence"))
    for name in _REQUIRED_EVIDENCE:
        evidence_path = evidence_dir / _PUBLIC_EVIDENCE_FILENAMES[name]
        try:
            if not evidence_path.is_file() or evidence_path.is_symlink():
                raise ValueError("public evidence file is missing or not regular")
            payload = evidence_path.read_bytes()
            document = json.loads(payload.decode("utf-8"))
            if not isinstance(document, dict):
                raise ValueError("public evidence root must be an object")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            issues.append(f"PUBLIC_EVIDENCE_FILE_INVALID:{name}:{exc}")
            continue
        issues.extend(
            _public_evidence_document_issues(
                document,
                expected_type=name,
                expected_commit=source_commit,
                expected_hardware=hardware,
            )
        )
        entry = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        actual_entries[name] = entry
        if _mapping(record_evidence.get(name)) != entry:
            issues.append(f"PUBLIC_EVIDENCE_DIGEST_MISMATCH:{name}")

    normalized = hashlib.sha256(_canonical_json({"evidence": actual_entries})).hexdigest()
    if record_evidence.get("normalized_evidence_sha256") != normalized:
        issues.append("PUBLIC_EVIDENCE_NORMALIZED_DIGEST_MISMATCH")
    return {
        "ok": not issues,
        "status": "READY" if not issues else "BLOCK",
        "reason_code": "PUBLIC_QUALIFICATION_BUNDLE_READY" if not issues else "PUBLIC_QUALIFICATION_BUNDLE_INVALID",
        "record_id": str(record.get("record_id", "")),
        "source_commit": source_commit,
        "evidence_dir": "public-evidence",
        "evidence": actual_entries,
        "issues": issues,
        "hardware_validation": hardware,
    }


def qualification_fixture_manifest() -> dict[str, Any]:
    fixture_root = resources.files("vivado_agent_mcp").joinpath("qualification_fixture")
    files: list[dict[str, Any]] = []
    for name in _FIXTURE_FILES:
        payload = fixture_root.joinpath(name).read_bytes()
        files.append({"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    aggregate = hashlib.sha256(_canonical_json(files)).hexdigest()
    return {
        "id": QUALIFICATION_FIXTURE_ID,
        "aggregate_sha256": aggregate,
        "files": files,
    }


def materialize_qualification_fixture(destination: str | Path) -> dict[str, Any]:
    target = Path(destination).expanduser()
    if target.is_symlink():
        return _fixture_block("QUALIFICATION_FIXTURE_DESTINATION_SYMLINK", target)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        return _fixture_block("QUALIFICATION_FIXTURE_DESTINATION_NOT_EMPTY", target)
    target.mkdir(parents=True, exist_ok=True)
    target = target.resolve()
    fixture_root = resources.files("vivado_agent_mcp").joinpath("qualification_fixture")
    paths: dict[str, str] = {}
    labels = {
        "qualification_counter.sv": "rtl",
        "tb_qualification_counter.sv": "sim",
        "qualification_counter.xdc": "xdc",
    }
    for name in _FIXTURE_FILES:
        output = target / name
        output.write_bytes(fixture_root.joinpath(name).read_bytes())
        paths[labels[name]] = str(output)
    return {
        "ok": True,
        "status": "READY",
        "reason_code": "",
        "fixture_dir": str(target),
        "manifest": qualification_fixture_manifest(),
        "paths": paths,
        "project_inputs": {
            "rtl_files": [paths["rtl"]],
            "xdc_files": [paths["xdc"]],
            "sim_files": [paths["sim"]],
            "top": "qualification_counter",
            "testbench_top": "tb_qualification_counter",
            "target_language": "SystemVerilog",
        },
    }


def _fixture_block(reason_code: str, target: Path) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "BLOCK",
        "reason_code": reason_code,
        "fixture_dir": str(target),
        "manifest": {},
        "paths": {},
        "project_inputs": {},
    }


def _validate_qualified_record(
    source: dict[str, Any],
    package: dict[str, Any],
    vivado: dict[str, Any],
    evidence: dict[str, Any],
    add: Any,
) -> None:
    if source.get("dirty") is not False:
        add("QUALIFICATION_SOURCE_NOT_CLEAN", "source.dirty", "qualified record requires a clean source tree")
    required_digests = {
        "source.source_archive_sha256": source.get("source_archive_sha256"),
        "package.wheel.sha256": _mapping(package.get("wheel")).get("sha256"),
        "package.sdist.sha256": _mapping(package.get("sdist")).get("sha256"),
        "package.source_wheel_pair_id": package.get("source_wheel_pair_id"),
    }
    for path, value in required_digests.items():
        if not _is_sha256(value):
            add("QUALIFICATION_PACKAGE_PROVENANCE_INCOMPLETE", path, "qualified record requires a valid SHA256")
    if package.get("provenance_verified") is not True:
        add(
            "QUALIFICATION_PACKAGE_PROVENANCE_INCOMPLETE",
            "package.provenance_verified",
            "qualified record requires immutable source/wheel provenance",
        )
    if (
        vivado.get("identity_status") != "VERIFIED"
        or vivado.get("version_attested") is not True
        or not _is_sha256(vivado.get("canonical_path_sha256"))
        or not _is_sha256(vivado.get("executable_sha256"))
        or not str(vivado.get("version", ""))
        or not str(vivado.get("full_version", ""))
        or not str(vivado.get("build", ""))
        or not _mapping(vivado.get("file_identity"))
    ):
        add(
            "QUALIFICATION_VIVADO_IDENTITY_UNATTESTED",
            "vivado",
            "qualified record requires canonical executable identity and process-attested full version/build",
        )
    if evidence.get("freshness") != "FRESH":
        add("QUALIFICATION_EVIDENCE_NOT_FRESH", "evidence.freshness", "qualified record requires fresh evidence")
    incomplete = [
        name
        for name in _REQUIRED_EVIDENCE
        if not _valid_evidence_entry(_mapping(evidence.get(name)))
    ]
    if incomplete or not _is_sha256(evidence.get("normalized_evidence_sha256")):
        add(
            "QUALIFICATION_PASS_EVIDENCE_INCOMPLETE",
            "evidence",
            "qualified PASS is missing complete evidence: " + ", ".join(incomplete),
        )


def _validate_status_consistency(
    qualification_status: str,
    terminal: dict[str, Any],
    software: dict[str, Any],
    add: Any,
) -> None:
    expected_software = qualification_status.upper()
    if qualification_status in QUALIFICATION_STATUSES and software.get("status") != expected_software:
        add(
            "QUALIFICATION_SOFTWARE_STATUS_CONTRADICTION",
            "software_validation.status",
            "software status must match qualification status",
        )
    terminal_status = terminal.get("status")
    if qualification_status == "qualified":
        if terminal_status != "PASS":
            add(
                "QUALIFICATION_TERMINAL_STATUS_CONTRADICTS_QUALIFIED",
                "terminal.status",
                "qualified record requires terminal PASS",
            )
        if software.get("validated") is not True:
            add(
                "QUALIFICATION_SOFTWARE_STATUS_CONTRADICTION",
                "software_validation.validated",
                "qualified software status must be validated=true",
            )
    elif qualification_status in {"trusted", "unvalidated", "rejected"} and software.get("validated") is not False:
        add(
            "QUALIFICATION_SOFTWARE_STATUS_CONTRADICTION",
            "software_validation.validated",
            f"{qualification_status} software status must not claim validated=true",
        )
    if qualification_status == "unvalidated" and terminal_status == "PASS":
        add(
            "QUALIFICATION_UNVALIDATED_PASS_CONTRADICTION",
            "terminal.status",
            "unvalidated record cannot claim terminal PASS",
        )


def _validate_fixture(fixture: dict[str, Any], add: Any) -> None:
    if not _is_sha256(fixture.get("aggregate_sha256")):
        add("QUALIFICATION_FIXTURE_IDENTITY_INVALID", "fixture.aggregate_sha256", "fixture digest is invalid")
    files = fixture.get("files")
    if not isinstance(files, list) or not files:
        add("QUALIFICATION_FIXTURE_IDENTITY_INVALID", "fixture.files", "fixture file inventory is missing")
        return
    for index, item in enumerate(files):
        entry = _mapping(item)
        if not str(entry.get("path", "")) or not isinstance(entry.get("size"), int) or int(entry.get("size", 0)) <= 0:
            add("QUALIFICATION_FIXTURE_IDENTITY_INVALID", f"fixture.files[{index}]", "fixture entry path/size is invalid")
        if not _is_sha256(entry.get("sha256")):
            add("QUALIFICATION_FIXTURE_IDENTITY_INVALID", f"fixture.files[{index}].sha256", "fixture entry digest is invalid")


def _validate_runner(runner: dict[str, Any], add: Any) -> None:
    for field in ("os", "os_version", "architecture", "python_version", "runner_class"):
        if not str(runner.get(field, "")):
            add("QUALIFICATION_RUNNER_IDENTITY_INVALID", f"runner.{field}", "runner identity field is missing")
    if not _is_sha256(runner.get("identity_sha256")):
        add("QUALIFICATION_RUNNER_IDENTITY_INVALID", "runner.identity_sha256", "runner identity digest is invalid")


def _validate_exact_keys(value: dict[str, Any], allowed: set[str], path: str, add: Any) -> None:
    for field in sorted(set(value) - allowed):
        add("QUALIFICATION_SCHEMA_ADDITIONAL_PROPERTY", f"{path}.{field}", "unexpected field is not allowed")


def _validate_time_order(terminal: dict[str, Any], record: dict[str, Any], add: Any) -> None:
    try:
        started = _parse_datetime(terminal.get("started_at"))
        ended = _parse_datetime(terminal.get("ended_at"))
        generated = _parse_datetime(record.get("generated_at"))
    except ValueError:
        add("QUALIFICATION_TIMESTAMP_INVALID", "generated_at", "qualification timestamps must be timezone-aware ISO-8601")
        return
    if started > ended or ended > generated:
        add("QUALIFICATION_TIMESTAMP_ORDER_INVALID", "terminal", "timestamps must satisfy started_at <= ended_at <= generated_at")


def _validation_result(
    record: Any,
    reasons: list[str],
    details: list[dict[str, str]],
    *,
    calculated_record_id: str = "",
) -> dict[str, Any]:
    record_id = str(record.get("record_id", "")) if isinstance(record, dict) else ""
    return {
        "ok": not reasons,
        "status": "READY" if not reasons else "BLOCK",
        "qualification_status": str(record.get("qualification_status", "")) if isinstance(record, dict) else "",
        "record_id": record_id,
        "calculated_record_id": calculated_record_id,
        "reason_codes": reasons,
        "issues": details,
        "hardware_validation": deepcopy(_mapping(record.get("hardware_validation"))) if isinstance(record, dict) else {},
    }


def _comparison_identity(record: dict[str, Any]) -> str:
    source = _mapping(record.get("source"))
    package = _mapping(record.get("package"))
    vivado = _mapping(record.get("vivado"))
    fixture = _mapping(record.get("fixture"))
    payload = {
        "source": {
            "commit": source.get("commit"),
            "tree": source.get("tree"),
            "tracked_digest": source.get("tracked_digest"),
            "source_archive_sha256": source.get("source_archive_sha256"),
        },
        "package": {
            "version": package.get("version"),
            "wheel_sha256": _mapping(package.get("wheel")).get("sha256"),
            "sdist_sha256": _mapping(package.get("sdist")).get("sha256"),
            "source_wheel_pair_id": package.get("source_wheel_pair_id"),
        },
        "vivado": {
            "canonical_path_sha256": vivado.get("canonical_path_sha256"),
            "executable_sha256": vivado.get("executable_sha256"),
            "file_identity": vivado.get("file_identity"),
            "version": vivado.get("version"),
            "full_version": vivado.get("full_version"),
            "build": vivado.get("build"),
        },
        "fixture": {
            "id": fixture.get("id"),
            "aggregate_sha256": fixture.get("aggregate_sha256"),
        },
        "execution": record.get("execution"),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _matrix_transition_allowed(current: str, next_status: str) -> bool:
    transitions = {
        "unvalidated": QUALIFICATION_STATUSES,
        "trusted": {"trusted", "qualified", "compatible", "rejected"},
        "compatible": {"compatible", "qualified", "rejected"},
        "qualified": {"qualified", "rejected"},
        "rejected": {"rejected", "trusted", "compatible", "qualified"},
    }
    return next_status in transitions.get(current, set())


def _matrix_status_notes(
    status: str,
    *,
    trust_status: str,
    record_id: str,
    source_commit: str,
) -> list[str]:
    identity = f"record {record_id} for source commit {source_commit}"
    hardware_note = "Qualification is limited to the no-board Project Mode software flow; FPGA/JTAG hardware remains NOT_VALIDATED."
    trust_note = f"Execution policy is represented independently by trust_status={trust_status}."
    if status == "qualified":
        return [f"Qualified by reviewed commit-bound {identity}.", trust_note, hardware_note]
    if status == "compatible":
        return [f"Compatibility is supported by reviewed {identity}; it does not change execution trust.", trust_note, hardware_note]
    if status == "trusted":
        return [f"The reviewed {identity} has qualification_status=trusted; it is not a completed live qualification.", trust_note, hardware_note]
    if status == "rejected":
        return [f"Rejected by reviewed {identity}; qualification was not granted.", trust_note, hardware_note]
    return [
        f"Unvalidated record {record_id} is attached for source commit {source_commit}; it does not establish qualification.",
        trust_note,
        hardware_note,
    ]


def _public_evidence_failure(reason_code: str, **data: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "BLOCK",
        "reason_code": reason_code,
        **data,
    }


def _path_contains_link_like_component(path: Path) -> bool:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            return True
        if not candidate.exists():
            continue
        is_junction = getattr(candidate, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
    return False


def _hardware_not_validated(value: dict[str, Any]) -> bool:
    return value.get("status") == "NOT_VALIDATED" and value.get("validated") is False


def _drop_public_evidence_key(key: str) -> bool:
    normalized = key.lower()
    return (
        _is_local_identity_field(key)
        or normalized
        in {
            "command",
            "commands",
            "cwd",
            "raw",
            "stderr",
            "stdout",
            "workspace",
        }
        or normalized.startswith("raw_")
        or normalized.endswith("_raw")
        or normalized.endswith(("_path", "_paths", "_dir", "_directory", "_root"))
        or normalized in {"path", "paths", "directory", "directories"}
    )


def _is_local_identity_field(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    return compact in _LOCAL_IDENTITY_FIELD_NAMES


def _redact_public_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_public_evidence(item)
            for key, item in value.items()
            if not _drop_public_evidence_key(str(key))
        }
    if isinstance(value, list):
        return [_redact_public_evidence(item) for item in value]
    if isinstance(value, str):
        if (
            _WINDOWS_ABSOLUTE_PATH_PATTERN.search(value)
            or _WINDOWS_UNC_PATH_PATTERN.search(value)
            or _POSIX_USER_PATH_PATTERN.search(value)
            or _EMAIL_PATTERN.search(value)
            or _LOCAL_IDENTITY_LABEL_PATTERN.search(value)
            or _VMCP_ENCODED_PATH_PATTERN.search(value)
        ):
            return "<redacted-local-identity>"
    return value


def _public_evidence_document_issues(
    document: dict[str, Any],
    *,
    expected_type: str,
    expected_commit: str,
    expected_hardware: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    expected_keys = {
        "schema_version",
        "evidence_type",
        "source_commit",
        "raw_evidence",
        "summary",
        "hardware_validation",
    }
    if set(document) != expected_keys:
        issues.append(f"PUBLIC_EVIDENCE_SCHEMA_KEYS_INVALID:{expected_type}")
    if document.get("schema_version") != PUBLIC_EVIDENCE_SCHEMA_VERSION:
        issues.append(f"PUBLIC_EVIDENCE_SCHEMA_VERSION_INVALID:{expected_type}")
    if document.get("evidence_type") != expected_type:
        issues.append(f"PUBLIC_EVIDENCE_TYPE_MISMATCH:{expected_type}")
    if document.get("source_commit") != expected_commit:
        issues.append(f"PUBLIC_EVIDENCE_COMMIT_MISMATCH:{expected_type}")
    raw = _mapping(document.get("raw_evidence"))
    if set(raw) != {"size", "sha256"} or not _valid_evidence_entry(raw):
        issues.append(f"PUBLIC_EVIDENCE_RAW_IDENTITY_INVALID:{expected_type}")
    if not isinstance(document.get("summary"), dict):
        issues.append(f"PUBLIC_EVIDENCE_SUMMARY_INVALID:{expected_type}")
    if _mapping(document.get("hardware_validation")) != expected_hardware or not _hardware_not_validated(expected_hardware):
        issues.append(f"PUBLIC_EVIDENCE_HARDWARE_BOUNDARY_INVALID:{expected_type}")
    issues.extend(_public_hardware_boundary_issues(document.get("summary"), evidence_type=expected_type))
    issues.extend(_public_local_identity_issues(document.get("summary"), evidence_type=expected_type))
    serialized = json.dumps(document, ensure_ascii=False, sort_keys=True)
    if (
        _WINDOWS_ABSOLUTE_PATH_PATTERN.search(serialized)
        or _WINDOWS_UNC_PATH_PATTERN.search(serialized)
        or _POSIX_USER_PATH_PATTERN.search(serialized)
        or _EMAIL_PATTERN.search(serialized)
        or _LOCAL_IDENTITY_LABEL_PATTERN.search(serialized)
        or _VMCP_ENCODED_PATH_PATTERN.search(serialized)
    ):
        issues.append(f"PUBLIC_EVIDENCE_LOCAL_IDENTITY_LEAK:{expected_type}")
    return issues


def _public_hardware_boundary_issues(value: Any, *, evidence_type: str) -> list[str]:
    issues: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized == "hardwarevalidation":
                if isinstance(item, dict):
                    boundary = {
                        re.sub(r"[^a-z0-9]", "", str(boundary_key).lower()): boundary_value
                        for boundary_key, boundary_value in item.items()
                    }
                    if boundary and ({"status", "validated"} & set(boundary)) and not _hardware_not_validated(boundary):
                        issues.append(f"PUBLIC_EVIDENCE_NESTED_HARDWARE_BOUNDARY_INVALID:{evidence_type}")
                elif item is not None and item != "" and item != "NOT_VALIDATED":
                    issues.append(f"PUBLIC_EVIDENCE_NESTED_HARDWARE_BOUNDARY_INVALID:{evidence_type}")
            elif (
                normalized in {"hardwarestatus", "hardwarevalidationstatus"}
                and item is not None
                and item != ""
                and item != "NOT_VALIDATED"
            ):
                issues.append(f"PUBLIC_EVIDENCE_NESTED_HARDWARE_STATUS_INVALID:{evidence_type}")
            elif (
                normalized in {"hardwarevalidated", "hardwarevalidationvalidated"}
                and item is not None
                and item != ""
                and item is not False
            ):
                issues.append(f"PUBLIC_EVIDENCE_NESTED_HARDWARE_VALIDATED_INVALID:{evidence_type}")
            issues.extend(_public_hardware_boundary_issues(item, evidence_type=evidence_type))
    elif isinstance(value, list):
        for item in value:
            issues.extend(_public_hardware_boundary_issues(item, evidence_type=evidence_type))
    return sorted(set(issues))


def _public_local_identity_issues(value: Any, *, evidence_type: str) -> list[str]:
    issues: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_local_identity_field(str(key)):
                issues.append(f"PUBLIC_EVIDENCE_LOCAL_IDENTITY_FIELD:{evidence_type}:{key}")
            issues.extend(_public_local_identity_issues(item, evidence_type=evidence_type))
    elif isinstance(value, list):
        for item in value:
            issues.extend(_public_local_identity_issues(item, evidence_type=evidence_type))
    return sorted(set(issues))


def _record_digest(record: dict[str, Any]) -> str:
    payload = deepcopy(record)
    payload.pop("record_id", None)
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _is_sha256(value: Any) -> bool:
    return bool(_SHA256_PATTERN.fullmatch(str(value or "").lower()))


def _valid_evidence_entry(entry: dict[str, Any]) -> bool:
    return isinstance(entry.get("size"), int) and int(entry.get("size", 0)) > 0 and _is_sha256(entry.get("sha256"))


def _parse_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone is required")
    return parsed
