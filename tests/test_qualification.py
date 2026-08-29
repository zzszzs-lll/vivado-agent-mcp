from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from vivado_agent_mcp.qualification import (
    QUALIFICATION_SCHEMA_VERSION,
    build_qualification_record,
    compare_qualification_records,
    materialize_qualification_fixture,
    qualification_fixture_manifest,
    qualification_record_schema,
    seal_qualification_record,
    update_qualification_matrix,
    validate_qualification_record,
)


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SHA_E = "e" * 64
_COMMIT = "1" * 40
_TREE = "2" * 40
_STARTED = "2026-08-30T01:00:00+00:00"
_ENDED = "2026-08-30T01:05:00+00:00"


def _qualified_record() -> dict:
    return build_qualification_record(
        qualification_status="qualified",
        terminal_status="PASS",
        reason_code="QUALIFICATION_COMPLETE",
        message="Commit-bound live Vivado software qualification completed.",
        started_at=_STARTED,
        ended_at=_ENDED,
        generated_at=_ENDED,
        source={
            "commit": _COMMIT,
            "tree": _TREE,
            "dirty": False,
            "tracked_digest": _SHA_A,
            "source_archive_sha256": _SHA_B,
        },
        package={
            "name": "vivado-agent-mcp",
            "version": "0.10.0",
            "wheel": {"name": "vivado_agent_mcp-0.10.0-py3-none-any.whl", "sha256": _SHA_C},
            "sdist": {"name": "vivado_agent_mcp-0.10.0.tar.gz", "sha256": _SHA_D},
            "source_wheel_pair_id": _SHA_E,
            "provenance_verified": True,
        },
        vivado={
            "identity_status": "VERIFIED",
            "canonical_path_sha256": _SHA_A,
            "executable_sha256": _SHA_B,
            "file_identity": {"volume_serial": 7, "file_index": 11, "size": 1234, "mtime_ns": 99},
            "version": "2021.2",
            "full_version": "Vivado v2021.2 (64-bit)",
            "build": "Build 3367213 on Tue Oct 19 02:48:09 MDT 2021",
            "version_attested": True,
        },
        runner={
            "os": "Windows",
            "os_version": "11",
            "architecture": "AMD64",
            "python_version": "3.12.7",
            "runner_class": "self-hosted-windows-vivado-2021.2",
            "identity_sha256": _SHA_C,
        },
        fixture={
            "id": "minimal-counter-v1",
            "aggregate_sha256": _SHA_D,
            "files": [
                {"path": "qualification_counter.sv", "size": 10, "sha256": _SHA_A},
                {"path": "tb_qualification_counter.sv", "size": 20, "sha256": _SHA_B},
                {"path": "qualification_counter.xdc", "size": 30, "sha256": _SHA_C},
            ],
        },
        execution={
            "tool_profile": "core",
            "policy_version": "vivado-policy-v1",
            "workflow_version": "live-qualification-v1",
            "scenario_id": "S01",
            "scenario_version": "agent-scenario-v1",
        },
        evidence={
            "freshness": "FRESH",
            "normalized_evidence_sha256": _SHA_E,
            "scenario_result": {"size": 100, "sha256": _SHA_A},
            "artifact_manifest": {"size": 101, "sha256": _SHA_B},
            "report_manifest": {"size": 102, "sha256": _SHA_C},
            "audit_result": {"size": 103, "sha256": _SHA_D},
            "diagnostic_manifest": {"size": 104, "sha256": _SHA_E},
        },
        software_validation={
            "status": "QUALIFIED",
            "validated": True,
            "scope": "no_board_project_mode_software_flow",
        },
        hardware_validation={
            "status": "NOT_VALIDATED",
            "validated": False,
            "scope": "real_fpga_jtag_programming_runtime",
        },
    )


def _expected_identity() -> dict[str, str]:
    return {
        "commit": _COMMIT,
        "source_archive_sha256": _SHA_B,
        "wheel_sha256": _SHA_C,
        "sdist_sha256": _SHA_D,
        "vivado_executable_sha256": _SHA_B,
        "fixture_sha256": _SHA_D,
    }


def test_qualification_schema_is_public_and_matches_generated_contract() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "qualification" / "qualification-record.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema == qualification_record_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["schema_version"]["const"] == QUALIFICATION_SCHEMA_VERSION
    assert set(schema["properties"]["qualification_status"]["enum"]) == {
        "trusted",
        "qualified",
        "compatible",
        "unvalidated",
        "rejected",
    }


def test_qualified_record_validates_against_expected_identities() -> None:
    record = _qualified_record()
    result = validate_qualification_record(record, expected=_expected_identity())
    assert result["ok"] is True
    assert result["status"] == "READY"
    assert result["reason_codes"] == []
    assert result["record_id"] == record["record_id"]


def test_missing_required_field_blocks_record() -> None:
    record = _qualified_record()
    record.pop("vivado")
    result = validate_qualification_record(record)
    assert result["ok"] is False
    assert "QUALIFICATION_SCHEMA_REQUIRED_FIELD_MISSING" in result["reason_codes"]


def test_unexpected_schema_field_blocks_record() -> None:
    record = _qualified_record()
    record["unexpected"] = "not part of the qualification contract"
    record = seal_qualification_record(record)
    result = validate_qualification_record(record)
    assert result["ok"] is False
    assert "QUALIFICATION_SCHEMA_ADDITIONAL_PROPERTY" in result["reason_codes"]


def test_wrong_commit_package_or_vivado_identity_blocks_record() -> None:
    expectations = _expected_identity()
    for key in ("commit", "wheel_sha256", "sdist_sha256", "vivado_executable_sha256"):
        wrong = dict(expectations)
        wrong[key] = "f" * len(wrong[key])
        result = validate_qualification_record(_qualified_record(), expected=wrong)
        assert result["ok"] is False
        assert "QUALIFICATION_EXPECTED_IDENTITY_MISMATCH" in result["reason_codes"]


def test_stale_evidence_cannot_be_qualified() -> None:
    record = _qualified_record()
    record["evidence"]["freshness"] = "STALE"
    record = seal_qualification_record(record)
    result = validate_qualification_record(record)
    assert result["ok"] is False
    assert "QUALIFICATION_EVIDENCE_NOT_FRESH" in result["reason_codes"]


def test_forged_pass_without_complete_evidence_is_rejected() -> None:
    record = _qualified_record()
    record["evidence"]["audit_result"] = {"size": 0, "sha256": ""}
    record = seal_qualification_record(record)
    result = validate_qualification_record(record)
    assert result["ok"] is False
    assert "QUALIFICATION_PASS_EVIDENCE_INCOMPLETE" in result["reason_codes"]


def test_interrupted_or_skipped_run_cannot_claim_qualified() -> None:
    for terminal_status in ("INTERRUPTED", "SKIPPED", "UNAVAILABLE"):
        record = _qualified_record()
        record["terminal"]["status"] = terminal_status
        record = seal_qualification_record(record)
        result = validate_qualification_record(record)
        assert result["ok"] is False
        assert "QUALIFICATION_TERMINAL_STATUS_CONTRADICTS_QUALIFIED" in result["reason_codes"]


def test_software_qualification_preserves_hardware_not_validated() -> None:
    record = _qualified_record()
    result = validate_qualification_record(record)
    assert result["ok"] is True
    assert record["software_validation"] == {
        "status": "QUALIFIED",
        "validated": True,
        "scope": "no_board_project_mode_software_flow",
    }
    assert record["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert record["hardware_validation"]["validated"] is False

    forged = deepcopy(record)
    forged["hardware_validation"] = {"status": "VALIDATED", "validated": True, "scope": "board"}
    forged = seal_qualification_record(forged)
    blocked = validate_qualification_record(forged)
    assert blocked["ok"] is False
    assert "QUALIFICATION_HARDWARE_BOUNDARY_VIOLATION" in blocked["reason_codes"]


def test_repeat_records_have_stable_comparison_identity() -> None:
    first = _qualified_record()
    second = deepcopy(first)
    second["generated_at"] = "2026-08-30T02:05:00+00:00"
    second["terminal"]["started_at"] = "2026-08-30T02:00:00+00:00"
    second["terminal"]["ended_at"] = "2026-08-30T02:05:00+00:00"
    second = seal_qualification_record(second)

    comparison = compare_qualification_records(first, second)
    assert comparison["comparable"] is True
    assert comparison["comparison_identity_matches"] is True
    assert comparison["normalized_evidence_matches"] is True
    assert comparison["terminal_status_matches"] is True


def test_matrix_accepts_valid_transition_and_rejects_forged_record() -> None:
    matrix = {
        "schema_version": 1,
        "entries": [
            {
                "platform": "windows",
                "vivado_version": "2021.2",
                "trust_status": "trusted",
                "qualification_status": "unvalidated",
                "software_validation_status": "UNVALIDATED",
                "hardware_validation_status": "NOT_VALIDATED",
                "record_id": "",
                "record_sha256": "",
            }
        ],
    }
    updated = update_qualification_matrix(matrix, _qualified_record())
    assert updated["ok"] is True
    entry = updated["matrix"]["entries"][0]
    assert entry["qualification_status"] == "qualified"
    assert entry["software_validation_status"] == "QUALIFIED"
    assert entry["hardware_validation_status"] == "NOT_VALIDATED"
    assert entry["record_id"] == _qualified_record()["record_id"]

    forged = _qualified_record()
    forged["source"]["dirty"] = True
    forged = seal_qualification_record(forged)
    rejected = update_qualification_matrix(updated["matrix"], forged)
    assert rejected["ok"] is False
    assert rejected["matrix"] == updated["matrix"]


def test_packaged_qualification_fixture_is_deterministic_and_non_overwriting(tmp_path: Path) -> None:
    manifest = qualification_fixture_manifest()
    assert manifest["id"] == "minimal-counter-v1"
    assert len(manifest["files"]) == 3
    assert len(manifest["aggregate_sha256"]) == 64
    assert {item["path"] for item in manifest["files"]} == {
        "qualification_counter.sv",
        "tb_qualification_counter.sv",
        "qualification_counter.xdc",
    }

    materialized = materialize_qualification_fixture(tmp_path / "fixture")
    assert materialized["manifest"] == manifest
    assert all(Path(path).is_file() for path in materialized["paths"].values())

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "sentinel.txt").write_text("do not replace", encoding="utf-8")
    blocked = materialize_qualification_fixture(occupied)
    assert blocked["ok"] is False
    assert blocked["reason_code"] == "QUALIFICATION_FIXTURE_DESTINATION_NOT_EMPTY"
    assert (occupied / "sentinel.txt").read_text(encoding="utf-8") == "do not replace"
