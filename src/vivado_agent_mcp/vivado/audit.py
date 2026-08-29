from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .agent_actions import dedupe_next_actions, next_action
from .artifacts import sha256_file
from .evidence_attestation import attest_diagnostic_manifest
from .evidence_store import EvidenceSnapshot
from .hardware_boundary import hardware_validation_boundary
from .managed_path import atomic_write_bytes, read_stable_bytes
from .prehardware import signoff_next_actions, signoff_next_steps
from .tcl import tcl_list_quote

WAIVER_DIRECTORY = "vmcp_signoff"
WAIVER_FILENAME = "waivers.json"
MAX_WAIVER_BYTES = 1024 * 1024
DIAGNOSTIC_DIRECTORY = "vmcp_diagnostics"
REPLAY_FILENAME = "replay_project.tcl"
REQUIRED_DIAGNOSTIC_CATEGORIES = (
    "audit",
    "environment",
    "project_state",
    "filesets",
    "run_configurations",
    "waivers",
    "session_status",
    "replay_script",
    "logs",
)
SINGLETON_DIAGNOSTIC_CATEGORIES = frozenset(
    (*REQUIRED_DIAGNOSTIC_CATEGORIES, "artifact_manifest", "report_manifest", "workflow_trace")
)


def waiver_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / WAIVER_DIRECTORY / WAIVER_FILENAME


def load_waivers(project_dir: str | Path) -> list[dict[str, Any]]:
    path = waiver_path(project_dir)
    if not os.path.lexists(path):
        return []
    data = json.loads(read_stable_bytes(path, root=project_dir, max_bytes=MAX_WAIVER_BYTES).decode("utf-8"))
    if isinstance(data, dict):
        waivers = data.get("waivers", [])
    else:
        waivers = data
    if not isinstance(waivers, list):
        raise ValueError("waivers.json must contain a waiver list")
    normalized = [dict(item) for item in waivers if isinstance(item, dict)]
    for waiver in normalized:
        _validate_waiver_expiry(waiver)
    return normalized


def save_waivers(project_dir: str | Path, waivers: list[dict[str, Any]]) -> Path:
    path = waiver_path(project_dir)
    payload = {
        "schema_version": 1,
        "updated_at": _utc_now(),
        "waivers": waivers,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    if len(content) > MAX_WAIVER_BYTES:
        raise ValueError(f"waivers.json exceeds {MAX_WAIVER_BYTES} bytes")
    return atomic_write_bytes(project_dir, path, content)


def create_waiver(
    *,
    project_dir: str | Path,
    waiver_id: str,
    finding_fingerprint: str,
    evidence_identity_sha256: str,
    code: str | None = None,
    message_contains: str | None = None,
    source_tool: str | None = None,
    reason: str = "",
    owner: str = "",
    expires_on: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    waivers = load_waivers(project_dir)
    if any(item.get("id") == waiver_id for item in waivers):
        raise ValueError(f"Signoff waiver already exists: {waiver_id}")
    expiry = (expires_on or "").strip()
    if expiry:
        _validate_waiver_expiry({"id": waiver_id, "expires_on": expiry})
    fingerprint = finding_fingerprint.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("Signoff waiver finding_fingerprint must be a 64-character lowercase SHA256 value")
    evidence_identity = evidence_identity_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_identity):
        raise ValueError("Signoff waiver evidence_identity_sha256 must be a 64-character lowercase SHA256 value")
    waiver = {
        "id": waiver_id,
        "finding_fingerprint": fingerprint,
        "evidence_identity_sha256": evidence_identity,
        "code": code or "",
        "message_contains": message_contains or "",
        "source_tool": source_tool or "",
        "reason": reason,
        "owner": owner,
        "expires_on": expiry,
        "enabled": enabled,
        "created_at": _utc_now(),
    }
    waivers.append(waiver)
    save_waivers(project_dir, waivers)
    return waiver


def remove_waiver(*, project_dir: str | Path, waiver_id: str) -> bool:
    waivers = load_waivers(project_dir)
    remaining = [item for item in waivers if item.get("id") != waiver_id]
    removed = len(remaining) != len(waivers)
    save_waivers(project_dir, remaining)
    return removed


def apply_signoff_waivers(
    findings: list[dict[str, Any]],
    waivers: list[dict[str, Any]],
    *,
    evidence_identity_sha256: str = "",
) -> dict[str, Any]:
    active: list[dict[str, Any]] = []
    waived: list[dict[str, Any]] = []
    normalized = [
        _bind_finding_evidence(_normalize_finding(item, index), evidence_identity_sha256)
        for index, item in enumerate(findings)
    ]
    for finding in normalized:
        waiver = _matching_waiver(finding, waivers)
        if waiver:
            waived.append({"finding": finding, "waiver": waiver})
        else:
            active.append(finding)
    return {
        "findings": normalized,
        "active_findings": active,
        "waived_findings": waived,
        "waiver_count": len([item for item in waivers if _waiver_enabled(item)]),
    }


def apply_waivers_to_signoff(
    signoff: dict[str, Any],
    waivers: list[dict[str, Any]],
    *,
    evidence_identity_sha256: str = "",
) -> dict[str, Any]:
    data = dict(signoff)
    findings = findings_from_result("run_pre_hw_signoff", data)
    applied = apply_signoff_waivers(findings, waivers, evidence_identity_sha256=evidence_identity_sha256)
    status = status_from_findings(applied["active_findings"])
    data["raw_findings"] = applied["findings"]
    data["findings"] = applied["findings"]
    data["active_findings"] = applied["active_findings"]
    data["waived_findings"] = applied["waived_findings"]
    data["waiver_count"] = applied["waiver_count"]
    data["raw_reasons"] = list(data.get("reasons", []))
    data["raw_warnings"] = list(data.get("warnings", []))
    data["reasons"] = [item["message"] for item in applied["active_findings"] if item["severity"] == "BLOCK"]
    data["warnings"] = [item["message"] for item in applied["active_findings"] if item["severity"] == "WARN"]
    data["status"] = status
    data["effective_status"] = "READY_WITH_WAIVERS" if status == "READY" and applied["waived_findings"] else status
    data["waiver_summary"] = {
        "waived_finding_count": len(applied["waived_findings"]),
        "active_finding_count": len(applied["active_findings"]),
        "requires_handoff_archive": bool(applied["waived_findings"]),
    }
    data["next_steps"] = signoff_next_steps(status, data["reasons"], data["warnings"])
    data["next_actions"] = signoff_next_actions(status, data["reasons"], data["warnings"])
    if data["effective_status"] == "READY_WITH_WAIVERS":
        data["next_steps"] = [
            "Archive waiver evidence with the diagnostic bundle; READY is based on active findings after explicit waivers.",
            *data["next_steps"],
        ]
        data["next_actions"] = dedupe_next_actions(
            [
                next_action(
                    "list_signoff_waivers",
                    "Include explicit waiver evidence in the project handoff.",
                    required_args=["project_dir"],
                    arg_sources={"project_dir": "current Vivado project directory"},
                    preconditions=["Waived findings exist and active findings are clear."],
                    stop_condition="waiver list is available for diagnostic bundle handoff.",
                ),
                *data["next_actions"],
            ]
        )
    return data


def findings_from_result(source_tool: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    existing = result.get("findings")
    if isinstance(existing, list) and existing:
        return [_normalize_finding(item, index, default_source=source_tool) for index, item in enumerate(existing)]
    findings: list[dict[str, Any]] = []
    for message in result.get("reasons", []) or []:
        findings.append(_finding(source_tool, "BLOCK", str(message), len(findings)))
    for message in result.get("warnings", []) or []:
        findings.append(_finding(source_tool, "WARN", str(message), len(findings)))
    status = str(result.get("status", "")).upper()
    if status in {"BLOCK", "WARN"} and not findings:
        message = str(result.get("message", f"{source_tool} status is {status}"))
        findings.append(_finding(source_tool, status, message, 0))
    return findings


def status_from_findings(findings: list[dict[str, Any]]) -> str:
    severities = {str(item.get("severity", "")).upper() for item in findings}
    if "BLOCK" in severities:
        return "BLOCK"
    if "WARN" in severities or "WARNING" in severities:
        return "WARN"
    return "READY"


def evaluate_project_audit(
    *,
    environment: dict[str, Any],
    project_state: dict[str, Any],
    sources: dict[str, Any],
    constraints: dict[str, Any],
    ip_status: dict[str, Any],
    bd_validation: dict[str, Any],
    run_configurations: dict[str, Any],
    artifact_manifest: dict[str, Any] | None,
    report_manifest: dict[str, Any] | None,
    signoff: dict[str, Any],
    artifact_validation: dict[str, Any] | None = None,
    waivers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw_findings: list[dict[str, Any]] = []
    for source_tool, result in (
        ("analyze_sources", sources),
        ("get_constraints_summary", constraints),
        ("get_ip_status", ip_status),
        ("validate_block_design", bd_validation),
        ("get_run_configuration", run_configurations),
        ("run_pre_hw_signoff", signoff),
    ):
        raw_findings.extend(findings_from_result(source_tool, result or {}))
    if not artifact_manifest:
        validation_message = str((artifact_validation or {}).get("message", "")).strip()
        validation_code = str((artifact_validation or {}).get("error_code", "")).strip()
        message = "artifact manifest is unavailable"
        if validation_message:
            message = f"artifact manifest is unavailable or rejected: {validation_message}"
        raw_findings.append(
            _finding(
                "get_artifact_manifest",
                "BLOCK",
                message,
                len(raw_findings),
                code=validation_code or "ARTIFACT_MANIFEST_MISSING",
            )
        )
    if not report_manifest:
        raw_findings.append(_finding("collect_report_bundle", "WARN", "report manifest is unavailable", len(raw_findings), code="REPORT_MANIFEST_MISSING"))
    else:
        for missing_report in report_manifest.get("missing_reports", []):
            if not isinstance(missing_report, dict):
                continue
            category = str(missing_report.get("category", "") or "unknown")
            reason = str(missing_report.get("reason", "") or "report file was not generated")
            raw_findings.append(
                _finding(
                    "collect_report_bundle",
                    "WARN",
                    f"{category} report missing: {reason}",
                    len(raw_findings),
                    code="REPORT_FILE_MISSING",
                )
            )
    evidence_freshness = _audit_evidence_freshness(
        run_configurations=run_configurations,
        artifact_manifest=artifact_manifest,
        report_manifest=report_manifest,
    )
    for issue in evidence_freshness["issues"]:
        raw_findings.append(_finding("run_project_audit", "BLOCK", issue, len(raw_findings), code="EVIDENCE_STALE"))
    evidence_identity_sha256 = build_waiver_evidence_identity(
        project_state=project_state,
        run_configurations=run_configurations,
        artifact_manifest=artifact_manifest,
        report_manifest=report_manifest,
    )
    applied = apply_signoff_waivers(
        raw_findings,
        waivers or [],
        evidence_identity_sha256=evidence_identity_sha256,
    )
    status = status_from_findings(applied["active_findings"])
    effective_status = "READY_WITH_WAIVERS" if status == "READY" and applied["waived_findings"] else status
    project = project_state.get("project", {}) if isinstance(project_state, dict) else {}
    return {
        "ok": True,
        "status": status,
        "effective_status": effective_status,
        "validation_scope": "pre_hardware_software",
        "ready_meaning": "READY means Project Mode software evidence is handoff-ready without real FPGA board validation.",
        "hardware_validation": hardware_validation_boundary(),
        "waiver_summary": {
            "waived_finding_count": len(applied["waived_findings"]),
            "active_finding_count": len(applied["active_findings"]),
            "requires_handoff_archive": bool(applied["waived_findings"]),
        },
        "evidence_freshness": evidence_freshness,
        "waiver_evidence_identity_sha256": evidence_identity_sha256,
        "health_summary": {
            "project_name": project.get("name", ""),
            "project_dir": project.get("directory", ""),
            "part": project.get("part", ""),
            "top": project.get("top", ""),
            "active_block_count": sum(1 for item in applied["active_findings"] if item["severity"] == "BLOCK"),
            "active_warning_count": sum(1 for item in applied["active_findings"] if item["severity"] == "WARN"),
            "waived_count": len(applied["waived_findings"]),
        },
        "raw_findings": applied["findings"],
        "active_findings": applied["active_findings"],
        "waived_findings": applied["waived_findings"],
        "waiver_count": applied["waiver_count"],
        "blocking_items": [item for item in applied["active_findings"] if item["severity"] == "BLOCK"],
        "warnings": [item for item in applied["active_findings"] if item["severity"] == "WARN"],
        "next_steps": _with_waiver_ready_step(effective_status, _audit_next_steps(status, applied["active_findings"])),
        "next_actions": _with_waiver_ready_action(effective_status, _audit_next_actions(status, applied["active_findings"]), project),
        "inputs": {
            "environment": environment,
            "project_state": project_state,
            "sources": sources,
            "constraints": constraints,
            "ip_status": ip_status,
            "bd_validation": bd_validation,
            "run_configurations": run_configurations,
            "artifact_manifest": artifact_manifest or {},
            "artifact_validation": artifact_validation or {},
            "report_manifest": report_manifest or {},
            "signoff": signoff,
        },
    }


def collect_diagnostic_bundle_files(
    *,
    project_dir: str | Path,
    audit_result: dict[str, Any],
    environment: dict[str, Any],
    project_state: dict[str, Any],
    filesets: dict[str, Any],
    run_configurations: dict[str, Any],
    waivers: list[dict[str, Any]],
    session_status: dict[str, Any] | None = None,
    replay_script: str | None = None,
    report_manifest_path: str | Path | None = None,
    artifact_manifest_path: str | Path | None = None,
    report_manifest_snapshot: EvidenceSnapshot | None = None,
    artifact_manifest_snapshot: EvidenceSnapshot | None = None,
    workflow_trace_path: str | Path | None = None,
    logs: dict[str, str] | None = None,
    output_dir: str | Path | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    project_path = Path(project_dir).resolve()
    bundle_dir = Path(output_dir).resolve() if output_dir else project_path / DIAGNOSTIC_DIRECTORY / (timestamp or _timestamp())
    _assert_inside_project(project_path, bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    files: list[dict[str, Any]] = []
    files.append(_write_json(bundle_dir / "audit_result.json", "audit", audit_result))
    files.append(_write_json(bundle_dir / "vivado_environment.json", "environment", environment))
    files.append(_write_json(bundle_dir / "project_state.json", "project_state", project_state))
    files.append(_write_json(bundle_dir / "filesets.json", "filesets", filesets))
    files.append(_write_json(bundle_dir / "run_configurations.json", "run_configurations", run_configurations))
    files.append(_write_json(bundle_dir / "waivers.json", "waivers", {"waivers": waivers}))
    if session_status is not None:
        files.append(_write_json(bundle_dir / "session_status.json", "session_status", session_status))
    if replay_script:
        files.append(_write_text(bundle_dir / REPLAY_FILENAME, "replay_script", replay_script))
    if logs:
        files.append(_write_text(bundle_dir / "logs_tail.txt", "logs", "\n\n".join(f"## {key}\n{value}" for key, value in logs.items())))
    else:
        files.append(_write_text(bundle_dir / "logs_tail.txt", "logs", ""))
    for category, manifest_path, snapshot in (
        ("report_manifest", report_manifest_path, report_manifest_snapshot),
        ("artifact_manifest", artifact_manifest_path, artifact_manifest_snapshot),
    ):
        if snapshot is not None:
            files.append(_write_evidence_snapshot(bundle_dir, snapshot, category))
        elif manifest_path:
            copied = _copy_managed_manifest(project_path, bundle_dir, Path(manifest_path), category)
            if copied:
                files.append(copied)
    if workflow_trace_path:
        copied = _copy_workflow_trace(project_path, bundle_dir, Path(workflow_trace_path))
        if copied:
            files.append(copied)

    manifest = {
        "schema_version": 2,
        "bundle_mode": "reference",
        "portable": False,
        "portability": {
            "status": "PROJECT_LOCAL_REFERENCE_ONLY",
            "reason": "Referenced report and artifact payloads remain in project-local vmcp_reports and vmcp_artifacts directories.",
        },
        "created_at": _utc_now(),
        "project_dir": str(project_path),
        "bundle_dir": str(bundle_dir),
        "manifest_path": str(bundle_dir / "diagnostic_manifest.json"),
        "hardware_validation": audit_result.get("hardware_validation") or hardware_validation_boundary(),
        "integrity_model": {
            "status": "SELF_CONSISTENCY_VERIFIED_BY_FILE_HASHES",
            "scope": "bundle_files",
            "authenticity_distinction": (
                "Bundle file hashes detect accidental or partial mutation. Local runtime attestation is evaluated separately."
            ),
        },
        "summary": _diagnostic_manifest_summary(files, audit_result),
        "files": files,
    }
    try:
        manifest["authenticity"] = attest_diagnostic_manifest(manifest)
    except (OSError, ValueError) as exc:
        manifest["authenticity"] = {
            "status": "NOT_ATTESTED",
            "scheme": "none",
            "trust_scope": "local_managed_runtime",
            "portable": False,
            "external_signature": False,
            "reason": f"{exc.__class__.__name__}: {exc}",
        }
    Path(manifest["manifest_path"]).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def render_project_replay_script(
    *,
    vivado_version: str = "",
    project: dict[str, Any],
    filesets: dict[str, list[dict[str, Any]]],
    run_configurations: dict[str, dict[str, Any]],
) -> str:
    name = str(project.get("name", "replay_project"))
    directory = str(project.get("directory", "."))
    part = str(project.get("part", ""))
    lines = [
        "# Vivado Agent MCP Project Mode replay script.",
        "# This script recreates Project Mode references for audit/reproduction.",
        f"# Vivado version: {vivado_version or 'unknown'}",
        "",
        f"create_project {tcl_list_quote(name)} {tcl_list_quote(directory)} -part {tcl_list_quote(part)}",
    ]
    for fileset in ("sources_1", "constrs_1", "sim_1"):
        files = [str(item.get("path", "")) for item in filesets.get(fileset, []) if item.get("path")]
        if files:
            lines.append(f"add_files -fileset {tcl_list_quote(fileset)} {_tcl_list(files)}")
    top = str(project.get("top", ""))
    sim_top = str(project.get("sim_top", ""))
    if top:
        lines.append(f"set_property top {tcl_list_quote(top)} [get_filesets {{sources_1}}]")
    if sim_top:
        lines.append(f"set_property top {tcl_list_quote(sim_top)} [get_filesets {{sim_1}}]")
    lines.extend(
        [
            "update_compile_order -fileset sources_1",
            "update_compile_order -fileset sim_1",
        ]
    )
    for run_name, config in run_configurations.items():
        run = config.get("run", config) if isinstance(config, dict) else {}
        strategy = run.get("strategy") or run.get("STRATEGY")
        if strategy:
            lines.append(f"set_property strategy {tcl_list_quote(strategy)} [get_runs {tcl_list_quote(run_name)}]")
    lines.extend(
        [
            "",
            "# Audit reports. These commands assume implementation results already exist.",
            "catch {open_run impl_1}",
            "catch {report_timing_summary -file timing_summary.rpt}",
            "catch {report_utilization -file utilization.rpt}",
            "catch {report_drc -file drc.rpt}",
            "catch {report_methodology -file methodology.rpt}",
        ]
    )
    return "\n".join(lines) + "\n"


def write_project_replay_script(
    *,
    project_dir: str | Path,
    script: str,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    project_path = Path(project_dir).resolve()
    path = Path(output_path).resolve() if output_path else project_path / DIAGNOSTIC_DIRECTORY / REPLAY_FILENAME
    _assert_inside_project(project_path, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script, encoding="utf-8")
    return {
        "script_path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _audit_evidence_freshness(
    *,
    run_configurations: dict[str, Any],
    artifact_manifest: dict[str, Any] | None,
    report_manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    issues: list[str] = []
    run_states: list[dict[str, Any]] = []
    raw_runs = run_configurations.get("runs", run_configurations) if isinstance(run_configurations, dict) else {}
    if isinstance(raw_runs, dict):
        iterable = raw_runs.items()
    elif isinstance(raw_runs, list):
        iterable = ((str(index), item) for index, item in enumerate(raw_runs))
    else:
        iterable = ()
    for run_name, item in iterable:
        run = item.get("run", item) if isinstance(item, dict) else {}
        if not isinstance(run, dict):
            continue
        needs_refresh = _truthy(run.get("needs_refresh", run.get("NEEDS_REFRESH", "")))
        status = str(run.get("status", run.get("STATUS", "")))
        state = {"run_name": str(run_name), "status": status, "needs_refresh": needs_refresh}
        run_states.append(state)
        if needs_refresh:
            issues.append(f"run {run_name} needs refresh before evidence is fresh")
    design_identities: dict[str, dict[str, Any]] = {}
    for label, manifest in (("artifact", artifact_manifest), ("report", report_manifest)):
        if not manifest:
            issues.append(f"{label} manifest is missing")
            continue
        if manifest.get("schema_version") != 4:
            issues.append(f"{label} manifest schema_version=4 is required")
        manifest_status = str(manifest.get("status", "")).upper()
        allowed_statuses = {"READY"} if label == "artifact" else {"READY", "WARN"}
        if manifest_status not in allowed_statuses:
            issues.append(f"{label} manifest status is {manifest_status or 'missing'}")
        freshness = manifest.get("evidence_freshness") if isinstance(manifest, dict) else None
        if not isinstance(freshness, dict):
            issues.append(f"{label} manifest evidence freshness is missing")
        else:
            freshness_status = str(freshness.get("status", "")).upper()
            if freshness_status != "FRESH":
                issues.append(f"{label} manifest evidence freshness is {freshness_status or 'missing'}")
            if _truthy(freshness.get("needs_refresh")):
                issues.append(f"{label} manifest needs refresh")
        if label == "artifact":
            artifacts = manifest.get("artifacts")
            if not isinstance(artifacts, list) or not any(
                isinstance(item, dict) and str(item.get("category", "")) == "bitstream"
                for item in artifacts
            ):
                issues.append("artifact manifest does not contain a validated bitstream")
        design_identity = manifest.get("design_execution_identity")
        design_identity_sha256 = str(manifest.get("design_execution_identity_sha256", ""))
        if (
            not isinstance(design_identity, dict)
            or design_identity.get("status") != "READY"
            or not re.fullmatch(r"[0-9a-f]{64}", design_identity_sha256)
            or design_identity_sha256 != str(design_identity.get("sha256", ""))
        ):
            issues.append(f"{label} manifest design execution identity is missing or invalid")
        else:
            design_identities[label] = design_identity
    if len(design_identities) == 2 and design_identities["artifact"] != design_identities["report"]:
        issues.append("artifact and report manifests describe different RTL/XDC/include/run configuration closures")
    shared_design_identity = (
        design_identities.get("artifact", {})
        if design_identities.get("artifact") == design_identities.get("report")
        else {}
    )
    return {
        "status": "STALE" if issues else "FRESH",
        "issues": _dedupe_steps(issues),
        "run_states": run_states,
        "artifact_manifest_path": str((artifact_manifest or {}).get("manifest_path", "")),
        "report_manifest_path": str((report_manifest or {}).get("manifest_path", "")),
        "design_execution_identity_sha256": str(shared_design_identity.get("sha256", "")),
        "checked_at": _utc_now(),
    }


def _matching_waiver(finding: dict[str, Any], waivers: list[dict[str, Any]]) -> dict[str, Any] | None:
    for waiver in waivers:
        if _waiver_matches(finding, waiver):
            matched = dict(waiver)
            matched["matched_finding_sha256"] = finding_fingerprint(finding)
            return matched
    return None


def _waiver_matches(finding: dict[str, Any], waiver: dict[str, Any]) -> bool:
    if not _waiver_enabled(waiver):
        return False
    if finding.get("waiver_eligible") is not True:
        return False
    evidence_identity = str(finding.get("evidence_identity_sha256", "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_identity):
        return False
    if str(waiver.get("evidence_identity_sha256", "")).strip().lower() != evidence_identity:
        return False
    expected_fingerprint = str(waiver.get("finding_fingerprint", "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint):
        return False
    if expected_fingerprint != finding_fingerprint(finding):
        return False
    code = str(waiver.get("code", "")).strip()
    source_tool = str(waiver.get("source_tool", "")).strip()
    message_contains = str(waiver.get("message_contains", "")).strip()
    if code and code != str(finding.get("code", "")):
        return False
    if source_tool and source_tool != str(finding.get("source_tool", "")):
        return False
    if message_contains and message_contains.lower() not in str(finding.get("message", "")).lower():
        return False
    return True


def build_waiver_evidence_identity(
    *,
    project_state: dict[str, Any],
    run_configurations: dict[str, Any],
    artifact_manifest: dict[str, Any] | None,
    report_manifest: dict[str, Any] | None,
) -> str:
    artifact = artifact_manifest or {}
    report = report_manifest or {}
    project = project_state.get("project", {}) if isinstance(project_state, dict) else {}
    artifact_snapshot = artifact.get("run_snapshot", {}) if isinstance(artifact.get("run_snapshot"), dict) else {}
    report_snapshot = report.get("run_snapshot", {}) if isinstance(report.get("run_snapshot"), dict) else {}
    artifact_run = str(artifact.get("run_name", ""))
    report_run = str(report.get("run_name", ""))
    artifact_generation = str(artifact_snapshot.get("session_generation_id", ""))
    report_generation = str(report_snapshot.get("session_generation_id", ""))
    artifact_sha256 = str(artifact.get("manifest_sha256", "")).lower()
    report_sha256 = str(report.get("manifest_sha256", "")).lower()
    artifact_design_identity = artifact.get("design_execution_identity")
    report_design_identity = report.get("design_execution_identity")
    design_identity_sha256 = str(artifact.get("design_execution_identity_sha256", "")).lower()
    bitstream_sha256 = next(
        (
            str(item.get("sha256", "")).lower()
            for item in artifact.get("artifacts", [])
            if isinstance(item, dict) and str(item.get("category", "")) == "bitstream"
        ),
        "",
    )
    required_hashes = (artifact_sha256, report_sha256, bitstream_sha256)
    if (
        not str(project.get("directory", ""))
        or not artifact_run
        or artifact_run != report_run
        or not artifact_generation
        or artifact_generation != report_generation
        or not isinstance(artifact_design_identity, dict)
        or artifact_design_identity != report_design_identity
        or design_identity_sha256 != str(artifact_design_identity.get("sha256", "")).lower()
        or design_identity_sha256 != str(report.get("design_execution_identity_sha256", "")).lower()
        or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in required_hashes)
        or not re.fullmatch(r"[0-9a-f]{64}", design_identity_sha256)
    ):
        return ""
    payload = {
        "schema_version": 2,
        "project": {
            key: str(project.get(key, ""))
            for key in ("directory", "name", "part", "top")
        },
        "run_name": artifact_run,
        "session_generation_id": artifact_generation,
        "artifact_manifest_sha256": artifact_sha256,
        "report_manifest_sha256": report_sha256,
        "bitstream_sha256": bitstream_sha256,
        "design_execution_identity_sha256": design_identity_sha256,
        "run_configurations_sha256": hashlib.sha256(
            json.dumps(run_configurations, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _bind_finding_evidence(finding: dict[str, Any], evidence_identity_sha256: str) -> dict[str, Any]:
    identity = evidence_identity_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        identity = str(finding.get("evidence_identity_sha256", "")).strip().lower()
    eligible = bool(re.fullmatch(r"[0-9a-f]{64}", identity))
    bound = {
        **finding,
        "evidence_identity_sha256": identity if eligible else "",
        "waiver_eligible": eligible,
    }
    bound["finding_fingerprint"] = finding_fingerprint(bound)
    return bound


def _waiver_enabled(waiver: dict[str, Any]) -> bool:
    if waiver.get("enabled", True) is False:
        return False
    expires_on = str(waiver.get("expires_on", "")).strip()
    if not expires_on:
        return True
    try:
        return date.fromisoformat(expires_on) >= datetime.now(UTC).date()
    except ValueError:
        return False


def _validate_waiver_expiry(waiver: dict[str, Any]) -> None:
    expires_on = str(waiver.get("expires_on", "")).strip()
    if not expires_on:
        return
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expires_on):
            raise ValueError("date must use canonical calendar form")
        parsed = date.fromisoformat(expires_on)
        if parsed.isoformat() != expires_on:
            raise ValueError("date must use canonical calendar form")
    except ValueError as exc:
        waiver_id = str(waiver.get("id", "<unknown>"))
        raise ValueError(f"Signoff waiver {waiver_id} expires_on must be an ISO date YYYY-MM-DD") from exc


def finding_fingerprint(finding: dict[str, Any]) -> str:
    identity = {
        key: value
        for key, value in finding.items()
        if key not in {"id", "finding_fingerprint"}
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_finding(item: dict[str, Any], index: int, default_source: str = "") -> dict[str, Any]:
    message = str(item.get("message", ""))
    source_tool = str(item.get("source_tool", default_source))
    severity = str(item.get("severity", "WARN")).upper()
    if severity == "WARNING":
        severity = "WARN"
    normalized = {
        "id": str(item.get("id", f"{source_tool or 'finding'}-{index}")),
        "severity": "BLOCK" if severity == "BLOCK" else "WARN",
        "code": str(item.get("code", _code_for_message(message, severity))),
        "message": message,
        "source_tool": source_tool,
        **{key: value for key, value in item.items() if key not in {"id", "severity", "code", "message", "source_tool"}},
    }
    normalized["finding_fingerprint"] = finding_fingerprint(normalized)
    return normalized


def _finding(source_tool: str, severity: str, message: str, index: int, *, code: str | None = None) -> dict[str, Any]:
    finding = {
        "id": f"{source_tool}-{index}",
        "severity": "BLOCK" if severity == "BLOCK" else "WARN",
        "code": code or _code_for_message(message, severity),
        "message": message,
        "source_tool": source_tool,
    }
    finding["finding_fingerprint"] = finding_fingerprint(finding)
    return finding


def _code_for_message(message: str, severity: str) -> str:
    text = message.lower()
    if "check_timing" in text or "timing is not met" in text or "no_clock" in text:
        return "READINESS_BLOCK" if severity == "BLOCK" else "READINESS_WARN"
    if "syntax" in text:
        return "SYNTAX"
    if "elaboration" in text or "unresolved module" in text or "black box" in text:
        return "ELABORATION"
    if "cdc" in text:
        return "CDC"
    if "clock interaction" in text:
        return "CLOCK_INTERACTION"
    if "methodology" in text:
        return "METHODOLOGY"
    if "drc" in text:
        return "DRC"
    if "power" in text:
        return "POWER_UNAVAILABLE" if "unavailable" in text else "POWER"
    return "SIGNOFF_BLOCK" if severity == "BLOCK" else "SIGNOFF_WARN"


def _audit_next_steps(status: str, active_findings: list[dict[str, Any]] | None = None) -> list[str]:
    findings = active_findings or []
    steps: list[str] = []
    if status == "BLOCK":
        steps.append("Fix active blocking findings before treating the project as ready.")
    elif status == "WARN":
        steps.append("Review active warnings and keep diagnostic bundle/replay script with the project handoff.")
    else:
        return ["Archive the diagnostic bundle and defer real hardware validation until an FPGA board is connected."]
    for finding in findings:
        step = _audit_next_step_for_finding(finding)
        if step:
            steps.append(step)
    steps.append("For known acceptable findings, create an explicit signoff waiver; do not hide raw findings.")
    return _dedupe_steps(steps)


def _audit_next_step_for_finding(finding: dict[str, Any]) -> str:
    source_tool = str(finding.get("source_tool", ""))
    code = str(finding.get("code", "")).upper()
    message = str(finding.get("message", "")).lower()
    text = f"{source_tool.lower()} {code.lower()} {message}"
    if source_tool == "analyze_sources" or code in {"SYNTAX", "ELABORATION"} or "syntax" in text or "compile order" in text:
        return "Run analyze_sources, check_syntax, and get_compile_order; fix RTL/fileset issues before rerunning signoff."
    if "cfgbvs" in text or "config_voltage" in text:
        return "Set or document board-specific CFGBVS and CONFIG_VOLTAGE before real FPGA board handoff."
    if source_tool in {"check_bitstream_readiness", "get_constraints_summary"} or code.startswith("READINESS") or "timing" in text or "constraint" in text or "no_clock" in text:
        return "Run get_constraints_summary, check_timing_constraints, and analyze_timing_closure; fix XDC/timing readiness blockers."
    if source_tool == "get_artifact_manifest" or code == "ARTIFACT_MANIFEST_MISSING":
        return "Run collect_build_artifacts after bitstream generation, then reload get_artifact_manifest."
    if source_tool == "collect_report_bundle" or code in {"REPORT_MANIFEST_MISSING", "REPORT_FILE_MISSING"}:
        return "Run collect_report_bundle after implementation results are available; if an optional report remains missing, keep the manifest reason in the handoff."
    if source_tool == "get_ip_status" or code.startswith("IP_") or "ip " in text:
        return "Run get_ip_status, generate_ip_targets, and upgrade_ip for locked or stale IP outputs."
    if source_tool == "validate_block_design" or code.startswith("BD_") or "block design" in text:
        return "Run validate_block_design and resolve invalid BD connections before signoff."
    if source_tool == "get_run_configuration" or code.startswith("RUN_") or "run configuration" in text:
        return "Run get_run_configuration and ensure synth_1/impl_1 are configured before launch_run."
    if code == "POWER_UNAVAILABLE" or source_tool == "get_power_report":
        return "Run get_power_report when implementation results are open; keep the unavailable report finding if the tool cannot produce power data."
    if code == "CDC" or source_tool == "get_cdc_report":
        return "Run get_cdc_report and resolve unsafe crossings or document reviewed waivers."
    if code == "CLOCK_INTERACTION" or source_tool == "get_clock_interaction_report":
        return "Run get_clock_interaction_report and review unconstrained or unsafe clock interactions."
    if code in {"DRC", "METHODOLOGY"} or source_tool in {"get_drc_report", "get_methodology_report"}:
        return "Run get_drc_report and get_methodology_report; resolve implementation quality findings."
    return ""


def _audit_next_actions(status: str, active_findings: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    findings = active_findings or []
    if status == "READY":
        return [
            next_action(
                "collect_diagnostic_bundle",
                "Archive audit-ready project evidence for Agent handoff.",
                required_args=["run_name"],
                arg_sources={"run_name": "workflow.run_name or current implementation run"},
                preconditions=["run_project_audit returned READY."],
                stop_condition="diagnostic bundle manifest is written.",
            )
        ]
    actions: list[dict[str, Any]] = []
    for finding in findings:
        actions.extend(_audit_next_actions_for_finding(finding))
    actions.append(
        next_action(
            "create_signoff_waiver",
            "Create an explicit waiver only for reviewed, acceptable findings; raw findings stay preserved.",
            required_args=["id", "finding_fingerprint", "project_dir"],
            arg_sources={
                "id": "user-chosen waiver id",
                "finding_fingerprint": "run_project_audit.data.active_findings[].finding_fingerprint",
                "project_dir": "project_state.project.directory",
            },
            preconditions=["Finding has been reviewed and accepted for this project context."],
            stop_condition="waiver is stored in vmcp_signoff/waivers.json.",
            optional=True,
        )
    )
    return dedupe_next_actions(actions)


def _with_waiver_ready_step(effective_status: str, steps: list[str]) -> list[str]:
    if effective_status != "READY_WITH_WAIVERS":
        return steps
    return _dedupe_steps(
        [
            "Archive waiver evidence with the diagnostic bundle; READY is based on active findings after explicit waivers.",
            *steps,
        ]
    )


def _with_waiver_ready_action(effective_status: str, actions: list[dict[str, Any]], project: dict[str, Any]) -> list[dict[str, Any]]:
    if effective_status != "READY_WITH_WAIVERS":
        return actions
    return dedupe_next_actions(
        [
            next_action(
                "list_signoff_waivers",
                "Include explicit waiver evidence in the project handoff.",
                required_args=["project_dir"],
                arg_sources={"project_dir": str(project.get("directory", "")) or "current Vivado project directory"},
                preconditions=["Waived findings exist and active findings are clear."],
                stop_condition="waiver list is available for diagnostic bundle handoff.",
            ),
            *actions,
        ]
    )


def _audit_next_actions_for_finding(finding: dict[str, Any]) -> list[dict[str, Any]]:
    source_tool = str(finding.get("source_tool", ""))
    code = str(finding.get("code", "")).upper()
    message = str(finding.get("message", "")).lower()
    text = f"{source_tool.lower()} {code.lower()} {message}"
    if source_tool == "analyze_sources" or code in {"SYNTAX", "ELABORATION"} or "syntax" in text or "compile order" in text:
        return [
            next_action(
                "analyze_sources",
                "Diagnose RTL/fileset blockers reported by audit.",
                required_args=["fileset"],
                arg_sources={"fileset": "sources_1 unless audit context says otherwise"},
                preconditions=["Project is open."],
                stop_condition="analyze_sources status is READY.",
            )
        ]
    if "cfgbvs" in text or "config_voltage" in text:
        return [
            next_action(
                "create_managed_xdc",
                "Add reviewed board-specific CFGBVS and CONFIG_VOLTAGE properties to an MCP-managed XDC.",
                required_args=["name", "constraints"],
                arg_sources={
                    "name": "for example board_configuration_voltage",
                    "constraints": "board documentation: CFGBVS and CONFIG_VOLTAGE values",
                },
                preconditions=["Board voltage requirements are known from the target board or carrier design."],
                stop_condition="Managed XDC containing CFGBVS and CONFIG_VOLTAGE is added to constrs_1.",
                optional=True,
            )
        ]
    if source_tool in {"check_bitstream_readiness", "get_constraints_summary"} or code.startswith("READINESS") or "timing" in text or "constraint" in text or "no_clock" in text:
        return [
            next_action(
                "analyze_timing_closure",
                "Diagnose timing, DRC, methodology, and readiness blockers.",
                preconditions=["Implementation result is open."],
                stop_condition="analyze_timing_closure status is READY.",
            )
        ]
    if source_tool == "get_artifact_manifest" or code == "ARTIFACT_MANIFEST_MISSING":
        return [
            next_action(
                "collect_build_artifacts",
                "Regenerate build artifact manifest after bitstream generation.",
                required_args=["run_name"],
                arg_sources={"run_name": "workflow.run_name or impl_1"},
                preconditions=["Bitstream generation has completed or artifacts exist."],
                stop_condition="artifact manifest is written.",
            )
        ]
    if source_tool == "collect_report_bundle" or code in {"REPORT_MANIFEST_MISSING", "REPORT_FILE_MISSING"}:
        return [
            next_action(
                "collect_report_bundle",
                "Regenerate implementation report bundle or preserve explicit missing-report reasons.",
                required_args=["run_name"],
                arg_sources={"run_name": "workflow.run_name or impl_1"},
                preconditions=["Implementation results are available."],
                stop_condition="report manifest is written with present/missing status for expected reports.",
            )
        ]
    if source_tool == "get_ip_status" or code.startswith("IP_") or "ip " in text:
        return [
            next_action(
                "get_ip_status",
                "Inspect locked or stale IP outputs.",
                preconditions=["Project is open."],
                stop_condition="IP status is current.",
            ),
            next_action(
                "generate_ip_targets",
                "Regenerate IP output products when stale or missing.",
                required_args=["ip_name"],
                arg_sources={"ip_name": "get_ip_status result"},
                preconditions=["Affected IP is identified."],
                stop_condition="IP output products are generated.",
            ),
        ]
    if source_tool == "validate_block_design" or code.startswith("BD_") or "block design" in text:
        return [
            next_action(
                "validate_block_design",
                "Re-validate Block Design after connection or IP fixes.",
                required_args=["bd_name"],
                arg_sources={"bd_name": "project BD file name"},
                preconditions=["Block Design exists in the open project."],
                stop_condition="Block Design validation is clean.",
            )
        ]
    if source_tool == "get_run_configuration" or code.startswith("RUN_") or "run configuration" in text:
        return [
            next_action(
                "get_run_configuration",
                "Inspect Vivado run configuration and refresh state.",
                required_args=["run_name"],
                arg_sources={"run_name": "workflow.run_name or synth_1/impl_1"},
                preconditions=["Project is open."],
                stop_condition="run configuration is available.",
            )
        ]
    if code == "POWER_UNAVAILABLE" or source_tool == "get_power_report":
        return [
            next_action(
                "get_power_report",
                "Refresh power report when Vivado can produce it.",
                preconditions=["Implementation result is open."],
                stop_condition="power report is available or warning is documented.",
                optional=True,
            )
        ]
    if code == "CDC" or source_tool == "get_cdc_report":
        return [
            next_action(
                "get_cdc_report",
                "Inspect CDC warnings or unsafe crossings.",
                preconditions=["Implementation result is open."],
                stop_condition="CDC report has no unsafe crossings.",
            )
        ]
    if code == "CLOCK_INTERACTION" or source_tool == "get_clock_interaction_report":
        return [
            next_action(
                "get_clock_interaction_report",
                "Inspect clock interaction warnings.",
                preconditions=["Implementation result is open."],
                stop_condition="clock interaction report has no unsafe crossings.",
            )
        ]
    if code in {"DRC", "METHODOLOGY"} or source_tool in {"get_drc_report", "get_methodology_report"}:
        return [
            next_action(
                "get_drc_report",
                "Inspect DRC quality findings.",
                preconditions=["Implementation result is open."],
                stop_condition="DRC report has no ERROR or CRITICAL WARNING.",
            ),
            next_action(
                "get_methodology_report",
                "Inspect methodology quality findings.",
                preconditions=["Implementation result is open."],
                stop_condition="methodology report has no ERROR or CRITICAL WARNING.",
            ),
        ]
    return []


def _dedupe_steps(steps: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for step in steps:
        if step and step not in seen:
            unique.append(step)
            seen.add(step)
    return unique


def _write_json(path: Path, category: str, payload: dict[str, Any]) -> dict[str, Any]:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return _file_entry(path, category)


def _write_text(path: Path, category: str, text: str) -> dict[str, Any]:
    path.write_text(text, encoding="utf-8")
    return _file_entry(path, category)


def _copy_managed_manifest(project_dir: Path, bundle_dir: Path, source: Path, category: str) -> dict[str, Any] | None:
    if not source.exists():
        return None
    source = source.resolve()
    _assert_inside_project(project_dir, source)
    if not any(part in {"vmcp_artifacts", "vmcp_reports", "vmcp_signoff"} for part in source.parts):
        raise ValueError("Refusing to copy non-MCP-managed diagnostic input")
    target_dir = bundle_dir / "managed" / category
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / source.name
    shutil.copy2(source, destination)
    entry = _file_entry(destination, category)
    entry["source_path"] = str(source)
    return entry


def _write_evidence_snapshot(bundle_dir: Path, snapshot: EvidenceSnapshot, category: str) -> dict[str, Any]:
    target_dir = bundle_dir / "managed" / category
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / snapshot.path.name
    atomic_write_bytes(bundle_dir, destination, snapshot.content)
    entry = _file_entry(destination, category)
    entry.update(
        {
            "source_path": str(snapshot.path),
            "source_sha256": snapshot.sha256,
            "source_size": snapshot.size,
            "source_file_id": snapshot.file_id,
            "source_mtime_ns": snapshot.mtime_ns,
            "evidence_consumption": "validated_bytes_snapshot",
        }
    )
    return entry


def _copy_workflow_trace(project_dir: Path, bundle_dir: Path, source: Path) -> dict[str, Any] | None:
    if not source.exists():
        return None
    source = source.resolve()
    _assert_inside_project(project_dir, source)
    if not any(part == "vmcp_diagnostics" for part in source.parts):
        raise ValueError("Refusing to copy workflow trace outside vmcp_diagnostics")
    destination = bundle_dir / "workflow_trace.jsonl"
    if source != destination.resolve():
        shutil.copy2(source, destination)
    entry = _file_entry(destination, "workflow_trace")
    entry["source_path"] = str(source)
    return entry


def _diagnostic_manifest_summary(files: list[dict[str, Any]], audit_result: dict[str, Any]) -> dict[str, Any]:
    category_counts: dict[str, int] = {}
    primary_files: dict[str, str] = {}
    for item in files:
        category = str(item.get("category", ""))
        if not category:
            continue
        category_counts[category] = category_counts.get(category, 0) + 1
        primary_files.setdefault(category, str(item.get("path", "")))
    missing = [category for category in REQUIRED_DIAGNOSTIC_CATEGORIES if category not in category_counts]
    health = audit_result.get("health_summary", {}) if isinstance(audit_result.get("health_summary"), dict) else {}
    active_findings = audit_result.get("active_findings", [])
    waived_findings = audit_result.get("waived_findings", [])
    next_steps = [str(item) for item in audit_result.get("next_steps", []) if str(item)]
    next_actions = audit_result.get("next_actions", [])
    if not isinstance(next_actions, list):
        next_actions = []
    if not isinstance(active_findings, list):
        active_findings = []
    if not isinstance(waived_findings, list):
        waived_findings = []
    return {
        "file_count": len(files),
        "audit_status": str(audit_result.get("status", "")),
        "effective_status": str(audit_result.get("effective_status", audit_result.get("status", ""))),
        "validation_scope": str(audit_result.get("validation_scope", "pre_hardware_software")),
        "ready_meaning": str(
            audit_result.get(
                "ready_meaning",
                "READY means no-board Vivado software evidence is handoff-ready; it is not real FPGA board validation.",
            )
        ),
        "waiver_summary": audit_result.get("waiver_summary", {}),
        "evidence_freshness": audit_result.get("evidence_freshness", {"status": "UNKNOWN", "issues": ["audit evidence freshness unavailable"]}),
        "design_execution_identity_sha256": str(
            (audit_result.get("evidence_freshness") or {}).get("design_execution_identity_sha256", "")
            if isinstance(audit_result.get("evidence_freshness"), dict)
            else ""
        ),
        "active_block_count": int(health.get("active_block_count", _count_findings(active_findings, "BLOCK"))),
        "active_warning_count": int(health.get("active_warning_count", _count_findings(active_findings, "WARN"))),
        "waived_count": int(health.get("waived_count", len(waived_findings))),
        "active_finding_count": len(active_findings),
        "waived_finding_count": len(waived_findings),
        "next_step_count": len(next_steps),
        "next_steps": next_steps,
        "next_actions": next_actions,
        "hardware_validation": audit_result.get("hardware_validation") or hardware_validation_boundary(),
        "categories": sorted(category_counts),
        "category_counts": category_counts,
        "primary_files": primary_files,
        "required_categories": list(REQUIRED_DIAGNOSTIC_CATEGORIES),
        "missing_required_categories": missing,
        "complete": not missing,
    }


def _count_findings(findings: list[Any], severity: str) -> int:
    return sum(1 for item in findings if isinstance(item, dict) and str(item.get("severity", "")).upper() == severity)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() not in {"", "0", "false", "no", "{}"}


def _file_entry(path: Path, category: str) -> dict[str, Any]:
    return {
        "path": str(path),
        "category": category,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _assert_inside_project(project_dir: Path, target: Path) -> None:
    project = project_dir.resolve()
    resolved = target.resolve()
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise ValueError("Refusing to write outside project directory") from exc


def _tcl_list(values: list[str]) -> str:
    return "[list " + " ".join(tcl_list_quote(value) for value in values) + "]"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")
