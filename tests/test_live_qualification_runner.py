from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import live_qualification_runner as runner

from vivado_agent_mcp.qualification import qualification_fixture_manifest, validate_qualification_record


_COMMIT = "1" * 40
_TREE = "2" * 40
_TRACKED = "a" * 64
_ARCHIVE = "b" * 64
_WHEEL = "c" * 64
_SDIST = "d" * 64
_PAIR = "e" * 64
_VIVADO = "f" * 64


def _source_provenance() -> dict:
    return {
        "status": "PASS",
        "source_snapshot": {"archive_sha256": _ARCHIVE},
        "source_identity": {
            "available": True,
            "clean": True,
            "commit": _COMMIT,
            "tree": _TREE,
            "tracked_digest": _TRACKED,
        },
        "package": {"name": "vivado-agent-mcp", "version": "0.10.0"},
        "wheel": {"name": "vivado_agent_mcp-0.10.0-py3-none-any.whl", "sha256": _WHEEL},
        "sdist": {"name": "vivado_agent_mcp-0.10.0.tar.gz", "sha256": _SDIST},
        "source_wheel_provenance_verified": True,
        "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
    }


def _release_manifest() -> dict:
    return {
        "status": "PASS",
        "source_identity": {
            "available": True,
            "clean": True,
            "commit": _COMMIT,
            "tree": _TREE,
            "tracked_digest": _TRACKED,
        },
        "package": {"name": "vivado-agent-mcp", "version": "0.10.0"},
        "wheel": {"name": "vivado_agent_mcp-0.10.0-py3-none-any.whl", "sha256": _WHEEL},
        "wheel_sha256_verified": True,
        "source_wheel_provenance_verified": True,
        "source_wheel_pair_id": _PAIR,
        "release_evidence_id": _PAIR,
        "release_evidence_ready": True,
        "evidence_run_id": "7" * 32,
        "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
        "validation_harness": {"status": "READY", "files": []},
    }


def _write_live_scenario_result(tmp_path: Path) -> tuple[dict, Path]:
    artifact = tmp_path / "artifact_manifest.json"
    report = tmp_path / "report_manifest.json"
    diagnostic = tmp_path / "diagnostic_manifest.json"
    audit = tmp_path / "audit_result.json"
    for path, payload in (
        (artifact, {"schema_version": 4, "status": "READY"}),
        (report, {"schema_version": 2, "status": "READY"}),
        (audit, {"status": "READY", "hardware_validation": {"status": "NOT_VALIDATED", "validated": False}}),
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
    diagnostic.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
                "files": [
                    {
                        "category": "audit",
                        "path": str(audit),
                        "size": audit.stat().st_size,
                        "sha256": runner.sha256_file(audit),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    scenario = {
        "id": "S01",
        "status": "PASS",
        "executed": True,
        "full_scenario_coverage": True,
        "hardware_validation_status": "NOT_VALIDATED",
        "vivado_environment": {
            "identity_status": "VERIFIED",
            "canonical_path_sha256": "9" * 64,
            "executable_sha256": _VIVADO,
            "file_identity": {"object_identity": [1, 2], "file_identity": [3, 4]},
            "version": "2021.2",
            "full_version": "Vivado v2021.2 (64-bit)",
            "build": "SW Build 3367213 | IP Build 3369179",
            "version_attested": True,
        },
        "qualification_fixture": qualification_fixture_manifest(),
        "partial_handoff": {
            "artifact_manifest_path": str(artifact),
            "report_manifest_path": str(report),
            "diagnostic_manifest_path": str(diagnostic),
            "hardware_validation_status": "NOT_VALIDATED",
        },
        "handoff_ready": False,
        "handoff_reviewable": True,
        "handoff_status": "WARN",
        "checks": {"simulation": True, "synthesis": True, "bitstream": True, "diagnostic": True},
    }
    result = {
        "ok": True,
        "passed": True,
        "blocked": False,
        "source_identity": _release_manifest()["source_identity"],
        "package_provenance": {
            "wheel_sha256": _WHEEL,
            "source_identity": _release_manifest()["source_identity"],
            "release_evidence_id": _PAIR,
        },
        "evidence_run_id": "7" * 32,
        "scenario_results": [scenario],
        "hardware_validation_status": "NOT_VALIDATED",
    }
    result_path = tmp_path / "agent_scenario_runner_result.json"
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result, result_path


def test_live_pass_builds_valid_commit_bound_qualification_record(tmp_path: Path) -> None:
    scenario_result, result_path = _write_live_scenario_result(tmp_path)
    record = runner.build_qualification_record_from_run(
        source_provenance=_source_provenance(),
        release_manifest=_release_manifest(),
        scenario_result=scenario_result,
        scenario_result_path=result_path,
        live_requested=True,
        generated_at="2026-08-30T03:05:00+00:00",
        started_at="2026-08-30T03:00:00+00:00",
        runner_class="test-self-hosted",
    )

    validation = validate_qualification_record(
        record,
        expected={
            "commit": _COMMIT,
            "source_archive_sha256": _ARCHIVE,
            "wheel_sha256": _WHEEL,
            "sdist_sha256": _SDIST,
            "vivado_executable_sha256": _VIVADO,
            "fixture_sha256": qualification_fixture_manifest()["aggregate_sha256"],
        },
    )
    assert validation["ok"] is True
    assert record["qualification_status"] == "qualified"
    assert record["terminal"]["status"] == "PASS"
    assert record["software_validation"]["status"] == "QUALIFIED"
    assert record["hardware_validation"] == {
        "status": "NOT_VALIDATED",
        "validated": False,
        "scope": "real_fpga_jtag_programming_runtime",
    }


def test_missing_live_evidence_rejects_claim_instead_of_emitting_qualified(tmp_path: Path) -> None:
    scenario_result, result_path = _write_live_scenario_result(tmp_path)
    Path(scenario_result["scenario_results"][0]["partial_handoff"]["artifact_manifest_path"]).unlink()

    record = runner.build_qualification_record_from_run(
        source_provenance=_source_provenance(),
        release_manifest=_release_manifest(),
        scenario_result=scenario_result,
        scenario_result_path=result_path,
        live_requested=True,
        generated_at="2026-08-30T03:05:00+00:00",
        started_at="2026-08-30T03:00:00+00:00",
        runner_class="test-self-hosted",
    )
    assert record["qualification_status"] == "rejected"
    assert record["terminal"]["status"] == "BLOCK"
    assert record["terminal"]["reason_code"] == "QUALIFICATION_LIVE_EVIDENCE_INCOMPLETE"
    assert validate_qualification_record(record)["ok"] is True


def test_no_live_request_is_unvalidated_and_never_qualified(tmp_path: Path) -> None:
    result_path = tmp_path / "not-run.json"
    result_path.write_text("{}", encoding="utf-8")
    record = runner.build_qualification_record_from_run(
        source_provenance=_source_provenance(),
        release_manifest=_release_manifest(),
        scenario_result={},
        scenario_result_path=result_path,
        live_requested=False,
        generated_at="2026-08-30T03:05:00+00:00",
        started_at="2026-08-30T03:00:00+00:00",
        runner_class="test-no-live",
    )
    assert record["qualification_status"] == "unvalidated"
    assert record["terminal"]["status"] == "SKIPPED"
    assert record["software_validation"]["validated"] is False
    assert validate_qualification_record(record)["ok"] is True


def test_release_source_commit_mismatch_is_rejected(tmp_path: Path) -> None:
    scenario_result, result_path = _write_live_scenario_result(tmp_path)
    release = _release_manifest()
    release["source_identity"]["commit"] = "8" * 40
    record = runner.build_qualification_record_from_run(
        source_provenance=_source_provenance(),
        release_manifest=release,
        scenario_result=scenario_result,
        scenario_result_path=result_path,
        live_requested=True,
        generated_at="2026-08-30T03:05:00+00:00",
        started_at="2026-08-30T03:00:00+00:00",
        runner_class="test-self-hosted",
    )
    assert record["qualification_status"] == "rejected"
    assert record["terminal"]["reason_code"] == "QUALIFICATION_RELEASE_PROVENANCE_MISMATCH"


def test_scenario_from_another_wheel_is_rejected(tmp_path: Path) -> None:
    scenario_result, result_path = _write_live_scenario_result(tmp_path)
    scenario_result["package_provenance"]["wheel_sha256"] = "0" * 64
    result_path.write_text(json.dumps(scenario_result, indent=2), encoding="utf-8")
    record = runner.build_qualification_record_from_run(
        source_provenance=_source_provenance(),
        release_manifest=_release_manifest(),
        scenario_result=scenario_result,
        scenario_result_path=result_path,
        live_requested=True,
        generated_at="2026-08-30T03:05:00+00:00",
        started_at="2026-08-30T03:00:00+00:00",
        runner_class="test-self-hosted",
    )
    assert record["qualification_status"] == "rejected"
    assert record["terminal"]["reason_code"] == "QUALIFICATION_SCENARIO_PROVENANCE_MISMATCH"


def test_qualification_command_uses_exact_wheel_and_existing_stdio_runner(tmp_path: Path) -> None:
    command = runner.qualification_scenario_command(
        python_exe=tmp_path / "venv" / "Scripts" / "python.exe",
        workspace=tmp_path,
        output_dir=tmp_path / "evidence",
        wheel_path=tmp_path / "dist" / "vivado_agent_mcp-0.10.0-py3-none-any.whl",
        release_manifest_path=tmp_path / "clean_install_smoke_result.json",
        evidence_run_id="a" * 32,
        part="xc7a35tcpg236-1",
        vivado_timeout_s=240,
        poll_timeout_s=1800,
        poll_interval_s=10,
    )
    assert command[1].endswith("tests\\agent_scenario_runner.py")
    assert command[command.index("--scenarios") + 1] == "S01"
    assert "--include-live-vivado" in command
    assert command[command.index("--release-wheel") + 1].endswith(".whl")
    assert command[command.index("--release-manifest") + 1].endswith("clean_install_smoke_result.json")


def test_forged_scenario_hardware_validation_is_rejected(tmp_path: Path) -> None:
    scenario_result, result_path = _write_live_scenario_result(tmp_path)
    forged = deepcopy(scenario_result)
    forged["scenario_results"][0]["hardware_validation_status"] = "VALIDATED"
    result_path.write_text(json.dumps(forged, indent=2), encoding="utf-8")
    record = runner.build_qualification_record_from_run(
        source_provenance=_source_provenance(),
        release_manifest=_release_manifest(),
        scenario_result=forged,
        scenario_result_path=result_path,
        live_requested=True,
        generated_at="2026-08-30T03:05:00+00:00",
        started_at="2026-08-30T03:00:00+00:00",
        runner_class="test-self-hosted",
    )
    assert record["qualification_status"] == "rejected"
    assert record["terminal"]["reason_code"] == "QUALIFICATION_HARDWARE_BOUNDARY_VIOLATION"
