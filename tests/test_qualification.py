from __future__ import annotations

import hashlib
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
    validate_published_qualification_bundle,
    validate_qualification_record,
    write_public_qualification_evidence,
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
                "notes": ["A commit-bound public qualification record has not yet been attached."],
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
    assert entry["record_ref"] == (
        "qualification/records/" + _qualified_record()["source"]["commit"] + "/qualification-record.json"
    )
    assert entry["notes"] == [
        (
            "Qualified by reviewed commit-bound record "
            + _qualified_record()["record_id"]
            + " for source commit "
            + _qualified_record()["source"]["commit"]
            + "."
        ),
        "Execution policy is represented independently by trust_status=trusted.",
        "Qualification is limited to the no-board Project Mode software flow; FPGA/JTAG hardware remains NOT_VALIDATED.",
    ]
    assert all("not yet been attached" not in note for note in entry["notes"])

    forged = _qualified_record()
    forged["source"]["dirty"] = True
    forged = seal_qualification_record(forged)
    rejected = update_qualification_matrix(updated["matrix"], forged)
    assert rejected["ok"] is False
    assert rejected["matrix"] == updated["matrix"]


def test_matrix_notes_keep_execution_trust_independent_from_qualification_status() -> None:
    trusted = _qualified_record()
    trusted["qualification_status"] = "trusted"
    trusted["terminal"] = {
        **trusted["terminal"],
        "status": "BLOCK",
        "reason_code": "QUALIFICATION_NOT_RUN",
    }
    trusted["software_validation"] = {
        "status": "TRUSTED",
        "validated": False,
        "scope": "no_board_project_mode_software_flow",
    }
    trusted = seal_qualification_record(trusted)

    updated = update_qualification_matrix({"schema_version": 1, "entries": []}, trusted)

    assert updated["ok"] is True
    entry = updated["matrix"]["entries"][0]
    assert entry["qualification_status"] == "trusted"
    assert entry["trust_status"] == "unvalidated"
    assert "trust_status=unvalidated" in entry["notes"][1]
    assert all("Execution policy trusts" not in note for note in entry["notes"])


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


def test_tracked_public_qualification_records_validate_and_match_matrix() -> None:
    workspace = Path(__file__).resolve().parents[1]
    matrix = json.loads((workspace / "qualification" / "matrix.json").read_text(encoding="utf-8"))
    qualified_entries = [
        entry
        for entry in matrix["entries"]
        if entry.get("qualification_status") == "qualified"
    ]

    assert qualified_entries
    for entry in qualified_entries:
        record_ref = entry["record_ref"]
        record_path = (workspace / record_ref).resolve()
        assert record_path.is_relative_to((workspace / "qualification" / "records").resolve())
        assert record_path.is_file()

        record = json.loads(record_path.read_text(encoding="utf-8"))
        validation = validate_qualification_record(record)
        publication_validation = validate_published_qualification_bundle(record_path)
        stored_validation = json.loads(
            record_path.with_name("qualification-validation.json").read_text(encoding="utf-8")
        )

        assert validation["ok"] is True
        assert publication_validation["ok"] is True
        assert stored_validation["ok"] is True
        assert stored_validation["published_evidence"]["ok"] is True
        assert stored_validation["record_id"] == record["record_id"]
        assert entry["record_id"] == record["record_id"]
        assert entry["record_sha256"] == record["record_id"]
        assert entry["source_commit"] == record["source"]["commit"]
        assert entry["full_version"] == record["vivado"]["full_version"]
        assert entry["hardware_validation_status"] == "NOT_VALIDATED"
        assert record["hardware_validation"] == {
            "status": "NOT_VALIDATED",
            "validated": False,
            "scope": "real_fpga_jtag_programming_runtime",
        }


def test_public_qualification_evidence_is_redacted_and_digest_verified(tmp_path: Path) -> None:
    raw_paths: dict[str, Path] = {}
    for name in (
        "scenario_result",
        "artifact_manifest",
        "report_manifest",
        "audit_result",
        "diagnostic_manifest",
    ):
        path = tmp_path / "raw" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "project_path": r"D:\private\project.xpr",
                    "message": "owner@example.invalid",
                    "nested": {
                        "safe": name,
                        "command": "open D:/private/project.xpr",
                        "raw_excerpt": "# file=D:/private/project.xdc\ncreate_clock -period 10",
                        "unc_excerpt": r"source=\\private-host\share\project.xdc",
                        "hostname": "private-build-host",
                        "identity_note": "username=LOCAL_USER_SENTINEL",
                        "encoded_path": "vmcp_hex_row_v1:path=443a2f70726976617465",
                        "key_id": "LOCAL_KEY_ID_SENTINEL",
                        "trust_anchor_id": "LOCAL_TRUST_ANCHOR_SENTINEL",
                        "file_id": "LOCAL_FILE_ID_SENTINEL",
                        "source_file_id": "LOCAL_SOURCE_FILE_ID_SENTINEL",
                        "hardware_validation": {
                            "status": "NOT_VALIDATED",
                            "validated": False,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        raw_paths[name] = path

    published = write_public_qualification_evidence(
        raw_paths,
        tmp_path / "record" / "public-evidence",
        source_commit=_COMMIT,
        hardware_validation={
            "status": "NOT_VALIDATED",
            "validated": False,
            "scope": "real_fpga_jtag_programming_runtime",
        },
    )
    record = _qualified_record()
    record["evidence"] = published["evidence"]
    record = seal_qualification_record(record)
    record_path = tmp_path / "record" / "qualification-record.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    validation = validate_published_qualification_bundle(record_path)
    public_text = "\n".join(Path(path).read_text(encoding="utf-8") for path in published["paths"].values())

    assert published["ok"] is True
    assert validation["ok"] is True
    assert "D:\\private" not in public_text
    assert "D:/private" not in public_text
    assert "private-host" not in public_text
    assert "private-build-host" not in public_text
    assert "LOCAL_USER_SENTINEL" not in public_text
    assert "443a2f70726976617465" not in public_text
    assert "LOCAL_KEY_ID_SENTINEL" not in public_text
    assert "LOCAL_TRUST_ANCHOR_SENTINEL" not in public_text
    assert "LOCAL_FILE_ID_SENTINEL" not in public_text
    assert "LOCAL_SOURCE_FILE_ID_SENTINEL" not in public_text
    assert "owner@example.invalid" not in public_text
    assert "raw_excerpt" not in public_text
    assert "<redacted-local-identity>" in public_text

    sha256_commit = write_public_qualification_evidence(
        raw_paths,
        tmp_path / "sha256-record" / "public-evidence",
        source_commit="2" * 64,
        hardware_validation={
            "status": "NOT_VALIDATED",
            "validated": False,
            "scope": "real_fpga_jtag_programming_runtime",
        },
    )
    assert sha256_commit["ok"] is True

    extra_dir = record_path.parent / "public-evidence" / "private"
    extra_dir.mkdir()
    (extra_dir / "raw.json").write_text('{"path":"D:/private/raw.json"}', encoding="utf-8")
    extra = validate_published_qualification_bundle(record_path)
    assert extra["ok"] is False
    assert "PUBLIC_EVIDENCE_FILE_SET_INVALID" in extra["issues"]
    (extra_dir / "raw.json").unlink()
    extra_dir.rmdir()

    evidence_path = Path(published["paths"]["scenario_result"])
    evidence_path.write_text(evidence_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    tampered = validate_published_qualification_bundle(record_path)
    assert tampered["ok"] is False
    assert "PUBLIC_EVIDENCE_DIGEST_MISMATCH:scenario_result" in tampered["issues"]

    original_scenario = json.dumps(
        json.loads(evidence_path.read_text(encoding="utf-8").rstrip()),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    evidence_path.write_text(original_scenario, encoding="utf-8", newline="\n")
    missing_path = Path(published["paths"]["diagnostic_manifest"])
    missing_payload = missing_path.read_bytes()
    missing_path.unlink()
    missing = validate_published_qualification_bundle(record_path)
    assert missing["ok"] is False
    assert any(issue.startswith("PUBLIC_EVIDENCE_FILE_INVALID:diagnostic_manifest") for issue in missing["issues"])
    missing_path.write_bytes(missing_payload)

    base_scenario = json.loads(evidence_path.read_text(encoding="utf-8"))

    def validate_resealed_scenario(scenario_document: dict) -> dict:
        evidence_path.write_text(
            json.dumps(scenario_document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        entries = {}
        for name, public_path in published["paths"].items():
            payload = Path(public_path).read_bytes()
            entries[name] = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        updated_record = deepcopy(record)
        updated_record["evidence"] = {
            "freshness": "FRESH",
            "normalized_evidence_sha256": hashlib.sha256(
                json.dumps({"evidence": entries}, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            **entries,
        }
        updated_record = seal_qualification_record(updated_record)
        record_path.write_text(
            json.dumps(updated_record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return validate_published_qualification_bundle(record_path)

    for claim_key, claim_value in (
        ("hardware_validation", {"status": "VALIDATED", "validated": True}),
        ("hardware_validation", "VALIDATED"),
        ("hardwareValidation", {"status": "VALIDATED", "validated": True}),
        ("hardwareValidation", {"Status": "VALIDATED", "Validated": True}),
        ("hardwareValidation", ["VALIDATED"]),
    ):
        scenario_document = deepcopy(base_scenario)
        scenario_document["summary"]["nested"].pop("hardware_validation", None)
        scenario_document["summary"]["nested"][claim_key] = claim_value
        contradictory = validate_resealed_scenario(scenario_document)
        assert contradictory["ok"] is False
        assert "PUBLIC_EVIDENCE_NESTED_HARDWARE_BOUNDARY_INVALID:scenario_result" in contradictory["issues"]

    local_identity = deepcopy(base_scenario)
    local_identity["summary"]["nested"]["file_id"] = "LOCAL_FILE_ID_SENTINEL"
    identity_validation = validate_resealed_scenario(local_identity)
    assert identity_validation["ok"] is False
    assert "PUBLIC_EVIDENCE_LOCAL_IDENTITY_FIELD:scenario_result:file_id" in identity_validation["issues"]


def test_public_qualification_evidence_rejects_symlink_destination(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw_paths = {}
    for name in (
        "scenario_result",
        "artifact_manifest",
        "report_manifest",
        "audit_result",
        "diagnostic_manifest",
    ):
        path = tmp_path / "raw" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status":"PASS"}', encoding="utf-8")
        raw_paths[name] = path

    destination = (tmp_path / "public-evidence").absolute()
    original_is_symlink = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        if path.absolute() == destination:
            return True
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    blocked = write_public_qualification_evidence(
        raw_paths,
        destination,
        source_commit=_COMMIT,
        hardware_validation={
            "status": "NOT_VALIDATED",
            "validated": False,
            "scope": "real_fpga_jtag_programming_runtime",
        },
    )

    assert blocked["ok"] is False
    assert blocked["reason_code"] == "PUBLIC_EVIDENCE_DESTINATION_SYMLINK"
