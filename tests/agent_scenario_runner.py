from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import subprocess
import sys
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters, stdio_client

_WORKSPACE = Path(__file__).resolve().parents[1]
_SRC = str(_WORKSPACE / "src")
_SOURCE_IMPORT_DISABLED = "--release-wheel" in sys.argv
_RUNNER_RELEASE_MODE = _SOURCE_IMPORT_DISABLED
_RUNNER_HARNESS_IDENTITY: dict[str, Any] = {}
if not _SOURCE_IMPORT_DISABLED and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import vivado_agent_mcp as _installed_package  # noqa: E402
from vivado_agent_mcp.vivado.agent_catalog import (  # noqa: E402
    DEFAULT_LIVE_SCENARIOS,
    DEFAULT_NO_LIVE_SCENARIOS,
    SCENARIO_VALIDATION_POLICY,
    SCENARIO_RUNNER_COVERAGE,
)
from vivado_agent_mcp.release_identity import source_identity  # noqa: E402
from vivado_agent_mcp.vivado.evidence_attestation import attest_diagnostic_manifest  # noqa: E402
from vivado_agent_mcp.vivado.evidence_store import verify_evidence_reference  # noqa: E402
from vivado_agent_mcp.vivado.runtime_identity import ensure_runtime_identity  # noqa: E402
from vivado_agent_mcp.vivado.workflow_trace import WorkflowTracer  # noqa: E402


SUPPORTED_SCENARIOS = ("S00", "S01", "S02", "S03", "S04", "S05", "S06", "S07")
DEFAULT_SCENARIOS = ("S00", "S05")
DEFAULT_PYTHON = sys.executable
DEFAULT_PART = "xc7a35tcpg236-1"
LIVE_PROJECT_BLOCKING_TOOLS = {
    "start_session",
    "create_project",
    "repair_project_setup",
    "check_syntax",
    "run_behavioral_simulation",
    "run_synthesis",
    "run_implementation",
    "generate_bitstream",
    "collect_build_artifacts",
    "collect_report_bundle",
    "run_pre_hw_signoff",
    "run_project_audit",
    "collect_diagnostic_bundle",
    "validate_diagnostic_bundle",
    "stop_session",
}
S03_LIVE_LIMITATION = (
    "Live XSIM simulation-repair evidence only; does not run synthesis, implementation, bitstream, "
    "hardware manager, programming, or real-board validation."
)
S07_LIVE_LIMITATION = (
    "Live existing-project inspection and evidence handoff only; seed project creation is fixture setup, and the "
    "receiver stage must not execute, mutate, or recreate the original project or claim real-board validation."
)

SCENARIO_EVIDENCE: dict[str, dict[str, Any]] = {
    "runner": {
        "execution_mode": "runner_guard",
        "evidence_class": "runner_contract",
        "full_scenario_coverage": False,
    },
    **{key: dict(value) for key, value in SCENARIO_RUNNER_COVERAGE.items()},
}

_SIMULATION_REPAIR_FAKE_SERVER = r"""
import asyncio
import json
import sys
from pathlib import Path

from vivado_agent_mcp import server as srv
from vivado_agent_mcp.tools import VivadoToolService


class SimulationRepairSession:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.state_path = base_dir / "simulation_repair_state.json"
        self.commands = []
        self.launch_count = 0
        self.latest_raw = ""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._write_state()

    def run_tcl(self, command: str, timeout_s: int = 60):
        self.commands.append(command)
        if "$dumpfile" in command or "$dumpvars" in command:
            return self._return(
                {
                    "ok": True,
                    "raw": (
                        f"sim_dir={self._sim_dir()}\n"
                        "testbench_vcd_usage=0\n"
                        "testbench_vcd_sources="
                    ),
                }
            )
        if "launch_simulation" in command:
            invocation_id = self._invocation_id(command)
            self.launch_count += 1
            if self.launch_count == 1:
                self.latest_raw = self._failure_raw(invocation_id)
                return self._return({"ok": True, "raw": self.latest_raw})
            self.latest_raw = self._pass_raw(invocation_id)
            return self._return({"ok": True, "raw": self.latest_raw})
        return self._return({"ok": True, "raw": self.latest_raw.replace("status_source=simulation_invocation_log_span", "status_source=latest_log_tail")})

    def status(self):
        return {
            "connected": True,
            "running": True,
            "backend": "fake-simulation-repair",
            "runtime_dir": str(self.base_dir / "runtime"),
        }

    def _return(self, payload):
        self._write_state()
        return payload

    def _write_state(self) -> None:
        self.state_path.write_text(
            json.dumps({"commands": self.commands, "launch_count": self.launch_count}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _sim_dir(self) -> str:
        return f"{self.base_dir.as_posix()}/project/demo.sim/sim_1/behav/xsim"

    def _invocation_id(self, command: str) -> str:
        marker = "set vmcp_simulation_invocation_id {"
        if marker not in command:
            return ""
        return command.split(marker, 1)[1].split("}", 1)[0]

    def _failure_raw(self, invocation_id: str) -> str:
        sim_dir = self._sim_dir()
        return (
            f"sim_dir={sim_dir}\n"
            f"log_path={sim_dir}/xsim.log\n"
            "status_source=simulation_invocation_log_span\n"
            f"simulation_invocation_id={invocation_id}\n"
            "log_span_start=100\n"
            "log_span_end=210\n"
            "log_span_reset_detected=0\n"
            "mcp_vcd_export_mode=disabled\n"
            "export_vcd_requested=0\n"
            "vcd_total_bytes=0\n"
            "log_begin=__VMCP_LOG_BEGIN__\n"
            "TEST FAIL expected_count=4 observed=1\n"
            "INFO: [XSIM 43-3496] Simulation finished\n"
        )

    def _pass_raw(self, invocation_id: str) -> str:
        sim_dir = self._sim_dir()
        return (
            f"sim_dir={sim_dir}\n"
            f"log_path={sim_dir}/xsim.log\n"
            "status_source=simulation_invocation_log_span\n"
            f"simulation_invocation_id={invocation_id}\n"
            "log_span_start=210\n"
            "log_span_end=320\n"
            "log_span_reset_detected=0\n"
            "mcp_vcd_export_mode=disabled\n"
            "export_vcd_requested=0\n"
            "vcd_total_bytes=0\n"
            "log_begin=__VMCP_LOG_BEGIN__\n"
            "TB_PASS repaired_count=4\n"
            "INFO: [XSIM 43-3496] Simulation finished\n"
        )


base_dir = Path(sys.argv[1])
srv.service = VivadoToolService(session=SimulationRepairSession(base_dir))
asyncio.run(srv.run_stdio_server())
"""


def main() -> int:
    global _RUNNER_HARNESS_IDENTITY, _RUNNER_RELEASE_MODE
    parser = argparse.ArgumentParser(description="Run reusable Agent scenario validations through MCP stdio.")
    parser.add_argument("--scenarios", default=",".join(DEFAULT_SCENARIOS), help="Comma-separated scenario IDs. Supported: S00,S01,S02,S03,S04,S05,S06,S07.")
    parser.add_argument("--output-dir", default="test_use/agent_scenario_runner", help="Directory for runner evidence.")
    parser.add_argument("--python", default=DEFAULT_PYTHON, help="Python executable used to launch the MCP server and helper scripts.")
    parser.add_argument("--include-live-vivado", action="store_true", help="Allow live Project Mode scenarios to start a real Vivado GUI session and run Project Mode flow.")
    parser.add_argument("--part", default=DEFAULT_PART, help="Vivado part for live Project Mode scenarios.")
    parser.add_argument("--vivado-timeout-s", type=int, default=240)
    parser.add_argument("--poll-timeout-s", type=int, default=1800)
    parser.add_argument("--poll-interval-s", type=int, default=15)
    parser.add_argument("--fail-on-watch", action="store_true", help="Return non-zero when any requested scenario is WATCH.")
    parser.add_argument("--allow-external-output-dir", action="store_true", help="Allow evidence output outside workspace test_use.")
    parser.add_argument("--release-wheel", help="Run in exact-wheel evidence mode; the runner process and MCP subprocesses must import this installed wheel.")
    parser.add_argument("--release-manifest", help="Release manifest that binds --release-wheel to source identity and release_evidence_id.")
    parser.add_argument("--evidence-run-id", help="Shared 32-hex run nonce used to bind clean-install and scenario evidence.")
    args = parser.parse_args()
    _RUNNER_RELEASE_MODE = bool(args.release_wheel)

    workspace = Path(__file__).resolve().parents[1]
    try:
        output_root = resolve_runner_output_root(workspace, args.output_dir, allow_external=bool(args.allow_external_output_dir))
    except ValueError as exc:
        safe_root = workspace / "test_use" / "agent_scenario_runner"
        run_dir = safe_root / f"run_{_timestamp()}"
        run_dir.mkdir(parents=True, exist_ok=True)
        result_path = run_dir / "agent_scenario_runner_result.json"
        result = output_dir_rejected_result(
            requested_output_dir=str(Path(args.output_dir).expanduser()),
            allowed_output_root=str((workspace / "test_use").resolve()),
            run_dir=run_dir,
            message=str(exc),
        )
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": False, "passed": False, "blocked": True, "watch": True, "result_path": str(result_path)}, ensure_ascii=False))
        return 1

    run_dir = output_root / f"run_{_timestamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "agent_scenario_runner_result.json"

    package_provenance: dict[str, Any] = {}
    try:
        package_provenance = collect_package_provenance(
            workspace=workspace,
            python_exe=Path(args.python),
            release_wheel=Path(args.release_wheel) if args.release_wheel else None,
            release_manifest=Path(args.release_manifest) if args.release_manifest else None,
            evidence_run_id=str(args.evidence_run_id or ""),
        )
        _RUNNER_HARNESS_IDENTITY = dict(package_provenance.get("validation_harness") or {})
        requested = parse_scenarios(args.scenarios)
        result = asyncio.run(
            run_scenarios(
                workspace=workspace,
                run_dir=run_dir,
                scenario_ids=requested,
                python_exe=str(args.python),
                include_live_vivado=bool(args.include_live_vivado),
                part=str(args.part),
                vivado_timeout_s=int(args.vivado_timeout_s),
                poll_timeout_s=int(args.poll_timeout_s),
                poll_interval_s=int(args.poll_interval_s),
            )
        )
    except RunnerProvenanceError as exc:
        result = {
            "ok": False,
            "passed": False,
            "blocked": True,
            "watch": False,
            "error_code": "RUNNER_PACKAGE_PROVENANCE_BLOCKED",
            "message": str(exc),
            "run_dir": str(run_dir),
            "scenario_results": [],
            "package_provenance": package_provenance,
        }
    except ValueError as exc:
        result = unsupported_scenario_result(raw_scenarios=str(args.scenarios), run_dir=run_dir, message=str(exc))
    except Exception as exc:  # noqa: BLE001 - runner output must stay machine-readable for Agents.
        result = {
            "ok": False,
            "passed": False,
            "blocked": True,
            "watch": True,
            "error": exc.__class__.__name__,
            "message": str(exc),
            "run_dir": str(run_dir),
            "scenario_results": [],
        }

    result.setdefault(
        "validation_matrix",
        build_validation_matrix(
            result.get("scenario_results", []),
            include_live_vivado=bool(args.include_live_vivado),
        ),
    )
    result.setdefault("generated_at", _now())
    result["source_identity"] = package_provenance.get("source_identity") or source_identity(workspace)
    result["package_provenance"] = package_provenance
    result["release_evidence_id"] = str(package_provenance.get("release_evidence_id", ""))
    result["evidence_run_id"] = str(package_provenance.get("evidence_run_id", ""))
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": result.get("ok", False),
                "passed": result.get("passed", False),
                "blocked": result.get("blocked", False),
                "watch": result.get("watch", False),
                "skipped_live": result.get("skipped_live", False),
                "result_path": str(result_path),
            },
            ensure_ascii=False,
        )
    )
    if result.get("blocked"):
        return 1
    if args.fail_on_watch and result.get("watch"):
        return 1
    return 0


def parse_scenarios(raw: str) -> list[str]:
    selected: list[str] = []
    for item in str(raw or "").replace(";", ",").split(","):
        scenario_id = item.strip().upper()
        if not scenario_id:
            continue
        if scenario_id not in SUPPORTED_SCENARIOS:
            raise ValueError(f"Unsupported scenario ID: {scenario_id}")
        if scenario_id not in selected:
            selected.append(scenario_id)
    return selected or list(DEFAULT_SCENARIOS)


class RunnerProvenanceError(ValueError):
    pass


def collect_package_provenance(
    *,
    workspace: Path,
    python_exe: Path,
    release_wheel: Path | None,
    release_manifest: Path | None,
    evidence_run_id: str,
) -> dict[str, Any]:
    import_path = Path(str(_installed_package.__file__)).resolve()
    if release_wheel is None:
        return {
            "mode": "workspace_source",
            "workspace_import_disabled": False,
            "wheel_payload_verified": False,
            "import_path": str(import_path),
            "python_executable": str(Path(sys.executable).resolve()),
            "source_identity": source_identity(workspace),
            "release_evidence_id": "",
            "evidence_run_id": "",
        }
    if not _SOURCE_IMPORT_DISABLED:
        raise RunnerProvenanceError("exact-wheel mode must be selected on the process command line before package imports")
    wheel = release_wheel.expanduser().resolve()
    manifest_path = release_manifest.expanduser().resolve() if release_manifest is not None else None
    if not wheel.is_file() or manifest_path is None or not manifest_path.is_file():
        raise RunnerProvenanceError("--release-wheel and --release-manifest must identify existing files")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", evidence_run_id):
        raise RunnerProvenanceError("exact-wheel mode requires --evidence-run-id as an unpredictable 32-hex nonce")
    if Path(python_exe).resolve() != Path(sys.executable).resolve():
        raise RunnerProvenanceError("--python must be the same clean-environment interpreter running the exact-wheel runner")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunnerProvenanceError(f"release manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise RunnerProvenanceError("release manifest must be a JSON object")
    wheel_sha256 = _sha256(wheel)
    manifest_wheel = manifest.get("wheel") if isinstance(manifest.get("wheel"), dict) else {}
    if wheel_sha256 != str(manifest_wheel.get("sha256", "")):
        raise RunnerProvenanceError("release wheel SHA256 does not match the release manifest")
    validation_harness = _verify_validation_harness(manifest, workspace)
    if _imports_from_workspace_source(import_path, workspace):
        raise RunnerProvenanceError("exact-wheel runner imported vivado_agent_mcp from the workspace")
    distribution = importlib.metadata.distribution("vivado-agent-mcp")
    record_entry = next((item for item in distribution.files or [] if str(item).replace("\\", "/").endswith(".dist-info/RECORD")), None)
    if record_entry is None:
        raise RunnerProvenanceError("installed distribution RECORD was not found")
    record_path = Path(distribution.locate_file(record_entry)).resolve()
    if not record_path.is_file():
        raise RunnerProvenanceError("installed distribution RECORD path is missing")
    release_id = str(manifest.get("release_evidence_id", ""))
    source = manifest.get("source_identity") if isinstance(manifest.get("source_identity"), dict) else {}
    if len(release_id) != 64 or source.get("clean") is not True:
        raise RunnerProvenanceError("release manifest provenance is incomplete or not clean")
    manifest_package = manifest.get("package") if isinstance(manifest.get("package"), dict) else {}
    expected_version = str(manifest_package.get("version", ""))
    if not expected_version or distribution.version != expected_version or _installed_package.__version__ != expected_version:
        raise RunnerProvenanceError("installed package version does not match the release manifest")
    payload = _verify_installed_wheel_payload(
        wheel,
        distribution=distribution,
        import_path=import_path,
    )
    if not payload["valid"]:
        raise RunnerProvenanceError(
            "installed package payload does not match the exact release wheel: " + "; ".join(payload["errors"][:5])
        )
    python_sha256 = _sha256(Path(sys.executable))
    environment_payload = {
        "python_executable": str(Path(sys.executable).resolve()),
        "python_sha256": python_sha256,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "sys_prefix": str(Path(sys.prefix).resolve()),
        "import_path": str(import_path),
        "wheel_sha256": wheel_sha256,
        "installed_payload_sha256": payload["installed_payload_sha256"],
        "installed_record_sha256": _sha256(record_path),
    }
    environment_identity_sha256 = hashlib.sha256(
        json.dumps(environment_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "mode": "installed_exact_wheel",
        "workspace_import_disabled": True,
        "wheel_payload_verified": True,
        "wheel_path": str(wheel),
        "wheel_sha256": wheel_sha256,
        "wheel_record_sha256": payload["wheel_record_sha256"],
        "wheel_member_count": payload["wheel_member_count"],
        "verified_payload_member_count": payload["verified_payload_member_count"],
        "installed_payload_sha256": payload["installed_payload_sha256"],
        "installed_version": distribution.version,
        "installed_record_path": str(record_path),
        "installed_record_sha256": _sha256(record_path),
        "import_path": str(import_path),
        "python_executable": str(Path(sys.executable).resolve()),
        "python_sha256": python_sha256,
        "environment_identity_sha256": environment_identity_sha256,
        "environment_identity": environment_payload,
        "validation_harness": validation_harness,
        "source_identity": source,
        "release_evidence_id": release_id,
        "evidence_run_id": evidence_run_id.lower(),
    }


def _imports_from_workspace_source(import_path: Path, workspace: Path) -> bool:
    source_root = (workspace.resolve() / "src").resolve()
    try:
        import_path.resolve().relative_to(source_root)
    except ValueError:
        return False
    return True


def _verify_validation_harness(manifest: dict[str, Any], workspace: Path) -> dict[str, Any]:
    harness = manifest.get("validation_harness") if isinstance(manifest.get("validation_harness"), dict) else {}
    entries = harness.get("files") if isinstance(harness.get("files"), list) else []
    expected_paths = [
        "tests/agent_scenario_runner.py",
        "tests/agent_stdio_regression.py",
    ]
    if harness.get("status") != "READY" or [str(item.get("path", "")) for item in entries if isinstance(item, dict)] != expected_paths:
        raise RunnerProvenanceError("release manifest validation_harness is missing or incomplete")
    verified: list[dict[str, Any]] = []
    workspace_root = workspace.resolve()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RunnerProvenanceError("release manifest validation_harness entry is not an object")
        relative = str(entry.get("path", ""))
        candidate = (workspace_root / relative).resolve()
        try:
            candidate.relative_to(workspace_root)
        except ValueError as exc:
            raise RunnerProvenanceError(f"validation harness path escapes workspace: {relative}") from exc
        if not candidate.is_file():
            raise RunnerProvenanceError(f"validation harness file is missing: {relative}")
        try:
            content = candidate.read_bytes()
        except OSError as exc:
            raise RunnerProvenanceError(f"validation harness file is unreadable: {relative}: {exc}") from exc
        size = len(content)
        digest = hashlib.sha256(content).hexdigest()
        if size != entry.get("size") or digest != str(entry.get("sha256", "")):
            raise RunnerProvenanceError(f"validation harness bytes do not match release manifest: {relative}")
        verified.append({"path": relative, "size": size, "sha256": digest})
    canonical = json.dumps(verified, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    identity_sha256 = hashlib.sha256(canonical).hexdigest()
    if identity_sha256 != str(harness.get("identity_sha256", "")):
        raise RunnerProvenanceError("validation harness aggregate identity does not match release manifest")
    return {
        "status": "VERIFIED",
        "policy": "exact_source_harness_size_sha256",
        "identity_sha256": identity_sha256,
        "files": verified,
    }


def _verify_installed_wheel_payload(
    wheel: Path,
    *,
    distribution: importlib.metadata.Distribution,
    import_path: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    verified: list[tuple[str, int, str]] = []
    wheel_names: set[str] = set()
    wheel_package_names: set[str] = set()
    wheel_record_sha256 = ""
    expected_import_path = Path(
        distribution.locate_file(PurePosixPath("vivado_agent_mcp/__init__.py"))
    ).resolve()
    if import_path != expected_import_path:
        errors.append("imported package path does not match the wheel distribution path")
    try:
        with zipfile.ZipFile(wheel) as archive:
            infos = [item for item in archive.infolist() if not item.is_dir()]
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                errors.append("wheel contains duplicate members")
            for info in infos:
                name = info.filename
                if not _safe_archive_member_name(name):
                    errors.append(f"wheel contains unsafe member path: {name}")
                    continue
                mode = info.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    errors.append(f"wheel contains a symbolic-link member: {name}")
                    continue
                wheel_names.add(name)
                with archive.open(info) as source:
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                source_sha256 = digest.hexdigest()
                if name.endswith(".dist-info/RECORD"):
                    wheel_record_sha256 = source_sha256
                    continue
                installed_path = Path(distribution.locate_file(PurePosixPath(name))).resolve()
                if not installed_path.is_file():
                    errors.append(f"installed wheel member is missing: {name}")
                    continue
                details = os.lstat(installed_path)
                if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                    errors.append(f"installed wheel member is not a single-link regular file: {name}")
                    continue
                installed_sha256 = _sha256(installed_path)
                if details.st_size != size or installed_sha256 != source_sha256:
                    errors.append(f"installed wheel member bytes differ: {name}")
                    continue
                verified.append((name, size, source_sha256))
                if name.startswith("vivado_agent_mcp/"):
                    wheel_package_names.add(name)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        errors.append(f"could not verify wheel payload: {exc.__class__.__name__}: {exc}")

    package_root = import_path.parent
    installed_package_names = {
        f"vivado_agent_mcp/{path.relative_to(package_root).as_posix()}"
        for path in package_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix.lower() != ".pyc"
    }
    if installed_package_names != wheel_package_names:
        missing = sorted(wheel_package_names - installed_package_names)
        unexpected = sorted(installed_package_names - wheel_package_names)
        if missing:
            errors.append(f"installed package is missing wheel members: {', '.join(missing[:5])}")
        if unexpected:
            errors.append(f"installed package contains unlisted files: {', '.join(unexpected[:5])}")
    if "vivado_agent_mcp/__init__.py" not in wheel_names:
        errors.append("wheel does not contain vivado_agent_mcp/__init__.py")
    if not wheel_record_sha256:
        errors.append("wheel RECORD member is missing")

    payload_digest = hashlib.sha256()
    for name, size, digest in sorted(verified):
        payload_digest.update(f"{name}\0{size}\0{digest}\n".encode("utf-8"))
    return {
        "valid": not errors,
        "wheel_member_count": len(wheel_names),
        "verified_payload_member_count": len(verified),
        "wheel_record_sha256": wheel_record_sha256,
        "installed_payload_sha256": payload_digest.hexdigest() if verified else "",
        "errors": errors,
    }


def _safe_archive_member_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return (
        bool(normalized)
        and normalized == name
        and not path.is_absolute()
        and ".." not in path.parts
        and not re.match(r"^[A-Za-z]:", normalized)
    )


def resolve_runner_output_root(workspace: Path, output_dir: str, *, allow_external: bool = False) -> Path:
    root = Path(output_dir).expanduser()
    if not root.is_absolute():
        root = workspace / root
    resolved = root.resolve()
    allowed_root = (workspace / "test_use").resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        if allow_external:
            return resolved
        raise ValueError(f"Refusing to write runner evidence outside workspace test_use: {resolved}") from exc
    return resolved


def scenario_evidence_fields(
    scenario_id: str,
    *,
    execution_mode: str = "",
    evidence_class: str = "",
    full_scenario_coverage: bool | None = None,
    limitation: str = "",
) -> dict[str, Any]:
    fields = dict(SCENARIO_EVIDENCE.get(scenario_id, SCENARIO_EVIDENCE["runner"]))
    if execution_mode:
        fields["execution_mode"] = execution_mode
    if evidence_class:
        fields["evidence_class"] = evidence_class
    if full_scenario_coverage is not None:
        fields["full_scenario_coverage"] = bool(full_scenario_coverage)
    if limitation:
        fields["limitation"] = limitation
    return fields


def s03_live_evidence_fields() -> dict[str, Any]:
    return scenario_evidence_fields(
        "S03",
        execution_mode="mcp_stdio_live_xsim_repair",
        evidence_class="live_xsim_repair_flow",
        full_scenario_coverage=True,
        limitation=S03_LIVE_LIMITATION,
    )


def s07_live_evidence_fields() -> dict[str, Any]:
    return scenario_evidence_fields(
        "S07",
        execution_mode="mcp_stdio_live_existing_project_handoff",
        evidence_class="live_existing_project_handoff_flow",
        full_scenario_coverage=True,
        limitation=S07_LIVE_LIMITATION,
    )


def output_dir_rejected_result(*, requested_output_dir: str, allowed_output_root: str, run_dir: Path, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "passed": False,
        "blocked": True,
        "watch": True,
        "error_code": "RUNNER_OUTPUT_DIR_OUTSIDE_WORKSPACE",
        "message": message,
        "requested_output_dir": requested_output_dir,
        "allowed_output_root": allowed_output_root,
        "run_dir": str(run_dir),
        "scenario_results": [
            {
                "id": "runner",
                "status": "BLOCK",
                **scenario_evidence_fields("runner"),
                "error_code": "RUNNER_OUTPUT_DIR_OUTSIDE_WORKSPACE",
                "summary": message,
                "next_actions": [
                    {
                        "tool": "agent_scenario_runner.py",
                        "reason": "Runner evidence must stay under workspace test_use unless explicitly allowed.",
                        "required_args": ["--output-dir"],
                        "arg_sources": {"--output-dir": "Use D:/Vivado_Mcp/test_use/<scenario-runner-dir> or pass --allow-external-output-dir."},
                        "preconditions": ["External output directory is explicitly reviewed."],
                        "stop_condition": "Runner result JSON is written under an approved evidence directory.",
                        "optional": False,
                    }
                ],
            }
        ],
    }


def unsupported_scenario_result(*, raw_scenarios: str, run_dir: Path, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "passed": False,
        "watch": False,
        "blocked": True,
        "started_at": _now(),
        "ended_at": _now(),
        "run_dir": str(run_dir),
        "error_code": "UNSUPPORTED_SCENARIO",
        "message": message,
        "requested_scenarios": raw_scenarios,
        "supported_scenarios": list(SUPPORTED_SCENARIOS),
        "scenario_results": [
            {
                "id": "",
                "status": "BLOCK",
                **scenario_evidence_fields("runner"),
                "error_code": "UNSUPPORTED_SCENARIO",
                "message": message,
                "requested_scenarios": raw_scenarios,
                "supported_scenarios": list(SUPPORTED_SCENARIOS),
                "next_actions": [
                    {
                        "tool": "tests/agent_scenario_runner.py",
                        "reason": "Retry the runner with one or more supported scenario IDs.",
                        "required_args": ["--scenarios"],
                        "arg_sources": {"--scenarios": ",".join(SUPPORTED_SCENARIOS)},
                        "preconditions": ["Choose scenario IDs from supported_scenarios."],
                        "stop_condition": "Runner writes agent_scenario_runner_result.json with no UNSUPPORTED_SCENARIO error.",
                        "optional": False,
                    }
                ],
            }
        ],
    }


async def run_scenarios(
    *,
    workspace: Path,
    run_dir: Path,
    scenario_ids: list[str],
    python_exe: str,
    include_live_vivado: bool,
    part: str,
    vivado_timeout_s: int,
    poll_timeout_s: int,
    poll_interval_s: int,
) -> dict[str, Any]:
    started_at = _now()
    results: list[dict[str, Any]] = []
    for scenario_id in scenario_ids:
        scenario_dir = run_dir / scenario_id
        scenario_dir.mkdir(parents=True, exist_ok=True)
        if scenario_id == "S00":
            results.append(run_s00_stdio_baseline(workspace=workspace, scenario_dir=scenario_dir, python_exe=python_exe))
        elif scenario_id == "S01":
            results.append(
                await run_live_project_flow(
                    workspace=workspace,
                    scenario_dir=scenario_dir,
                    python_exe=python_exe,
                    include_live_vivado=include_live_vivado,
                    part=part,
                    vivado_timeout_s=vivado_timeout_s,
                    poll_timeout_s=poll_timeout_s,
                    poll_interval_s=poll_interval_s,
                    scenario_id="S01",
                    label="Minimal counter Project Mode closure",
                    project_name="s01_counter",
                    source_writer=write_s01_project_sources,
                    sim_run_time="5 us",
                )
            )
        elif scenario_id == "S05":
            results.append(await run_s05_warn_handoff(workspace=workspace, scenario_dir=scenario_dir, python_exe=python_exe))
        elif scenario_id == "S06":
            results.append(await run_s06_safety_gates(workspace=workspace, scenario_dir=scenario_dir, python_exe=python_exe))
        elif scenario_id == "S03":
            if include_live_vivado:
                results.append(
                    await run_s03_live_simulation_failure_repair(
                        workspace=workspace,
                        scenario_dir=scenario_dir,
                        python_exe=python_exe,
                        part=part,
                        vivado_timeout_s=vivado_timeout_s,
                    )
                )
            else:
                results.append(await run_s03_simulation_failure_repair(workspace=workspace, scenario_dir=scenario_dir, python_exe=python_exe))
        elif scenario_id == "S04":
            results.append(run_s04_partial_setup_recovery(workspace=workspace, scenario_dir=scenario_dir, python_exe=python_exe))
        elif scenario_id == "S07":
            if include_live_vivado:
                results.append(
                    await run_s07_live_existing_project_handoff(
                        workspace=workspace,
                        scenario_dir=scenario_dir,
                        python_exe=python_exe,
                        part=part,
                        vivado_timeout_s=vivado_timeout_s,
                        poll_timeout_s=poll_timeout_s,
                        poll_interval_s=poll_interval_s,
                    )
                )
            else:
                results.append(await run_s07_existing_project_handoff(workspace=workspace, scenario_dir=scenario_dir, python_exe=python_exe))
        elif scenario_id == "S02":
            results.append(
                await run_live_project_flow(
                    workspace=workspace,
                    scenario_dir=scenario_dir,
                    python_exe=python_exe,
                    include_live_vivado=include_live_vivado,
                    part=part,
                    vivado_timeout_s=vivado_timeout_s,
                    poll_timeout_s=poll_timeout_s,
                    poll_interval_s=poll_interval_s,
                    scenario_id="S02",
                    label="Multi-file SystemVerilog PWM",
                    project_name="s02_pwm",
                    source_writer=write_s02_project_sources,
                    sim_run_time="20 us",
                )
            )
    blocked = any(item["status"] == "BLOCK" for item in results)
    watch = any(item["status"] == "WATCH" for item in results)
    passed = bool(results) and all(item["status"] == "PASS" for item in results)
    skipped_live = any(bool(item.get("skipped_live")) for item in results)
    return {
        "ok": not blocked,
        "passed": passed,
        "watch": watch,
        "blocked": blocked,
        "skipped_live": skipped_live,
        "started_at": started_at,
        "ended_at": _now(),
        "workspace": str(workspace),
        "run_dir": str(run_dir),
        "scenario_count": len(results),
        "scenario_results": results,
        "hardware_validation_status": _combined_hardware_status(results),
        "validation_matrix": build_validation_matrix(results, include_live_vivado=include_live_vivado),
    }


def build_validation_matrix(results: list[dict[str, Any]], *, include_live_vivado: bool) -> dict[str, Any]:
    result_by_id = {str(item.get("id", "")).upper(): item for item in results if item.get("id")}
    rows = [_validation_matrix_row(scenario_id, result_by_id.get(scenario_id)) for scenario_id in SUPPORTED_SCENARIOS]
    no_live_rows = [row for row in rows if row["no_live_required"]]
    live_rows = [row for row in rows if row["live_required"]]
    no_live_complete = all(row["requested"] and row["status"] == "PASS" for row in no_live_rows)
    live_complete = all(row["live_evidence_observed"] and row["status"] == "PASS" for row in live_rows)
    no_blocks = all(row["status"] != "BLOCK" for row in rows if row["requested"])
    hardware_not_validated = _combined_hardware_status(results) in {"", "NOT_VALIDATED"}
    all_requested_passed = bool(results) and all(str(item.get("status", "")).upper() == "PASS" for item in results)
    scenario_matrix_complete = no_live_complete and live_complete and no_blocks and hardware_not_validated
    scenario_matrix_status = "PASS" if scenario_matrix_complete else "WATCH"
    if not no_blocks or not hardware_not_validated:
        scenario_matrix_status = "BLOCK"
    return {
        "version": 1,
        "policy_id": SCENARIO_VALIDATION_POLICY["id"],
        "scope": SCENARIO_VALIDATION_POLICY["scope"],
        "include_live_vivado": bool(include_live_vivado),
        "required_no_live_scenarios": list(DEFAULT_NO_LIVE_SCENARIOS),
        "required_live_scenarios": list(DEFAULT_LIVE_SCENARIOS),
        "rows": rows,
        "checks": {
            "all_requested_passed": all_requested_passed,
            "no_live_matrix_complete": no_live_complete,
            "live_matrix_complete": live_complete,
            "no_block_results": no_blocks,
            "hardware_boundary_not_validated": hardware_not_validated,
        },
        "status": scenario_matrix_status,
        "complete": scenario_matrix_complete,
        "claim_boundary": "Scenario evidence only; not a release attestation or hardware validation.",
        "next_required_runs": _next_required_runs(rows),
    }


def _validation_matrix_row(scenario_id: str, result: dict[str, Any] | None) -> dict[str, Any]:
    mode = str((result or {}).get("execution_mode", ""))
    evidence_class = str((result or {}).get("evidence_class", ""))
    full_coverage = bool((result or {}).get("full_scenario_coverage", False))
    hardware_status = str((result or {}).get("hardware_validation_status", ""))
    live_mode = mode in {
        "mcp_stdio_live_project",
        "mcp_stdio_live_xsim_repair",
        "mcp_stdio_live_existing_project_handoff",
    }
    live_evidence = (
        live_mode
        and full_coverage
        and hardware_status == "NOT_VALIDATED"
        and evidence_class not in {"", "synthetic_bundle_contract", "stdio_fake_session_contract"}
    )
    return {
        "id": scenario_id,
        "requested": result is not None,
        "status": str((result or {}).get("status", "NOT_RUN")),
        "execution_mode": mode,
        "evidence_class": evidence_class,
        "full_scenario_coverage": full_coverage,
        "hardware_validation_status": hardware_status,
        "no_live_required": scenario_id in DEFAULT_NO_LIVE_SCENARIOS,
        "live_required": scenario_id in DEFAULT_LIVE_SCENARIOS,
        "live_evidence_observed": live_evidence,
    }


def _next_required_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    missing_no_live = [row["id"] for row in rows if row["no_live_required"] and row["status"] != "PASS"]
    missing_live = [row["id"] for row in rows if row["live_required"] and not row["live_evidence_observed"]]
    actions = []
    if missing_no_live:
        actions.append(
            {
                "command": f"python tests/agent_scenario_runner.py --scenarios {','.join(missing_no_live)} --output-dir test_use/agent_scenario_runner_no_live",
                "reason": "Complete the no-live scenario matrix without starting Vivado.",
            }
        )
    if missing_live:
        actions.append(
            {
                "command": f"python tests/agent_scenario_runner.py --scenarios {','.join(missing_live)} --include-live-vivado --output-dir test_use/agent_scenario_runner_live --poll-timeout-s 1800 --poll-interval-s 10",
                "reason": "Collect live/live-lite no-board Project Mode evidence for the scenario matrix.",
            }
        )
    return actions


def run_s00_stdio_baseline(*, workspace: Path, scenario_dir: Path, python_exe: str) -> dict[str, Any]:
    output_dir = scenario_dir / "agent_stdio_regression"
    command = _nested_stdio_regression_command(workspace, output_dir=output_dir, python_exe=python_exe)
    env = _stdio_env(workspace)
    started = _now()
    completed = subprocess.run(command, cwd=str(workspace), env=env, capture_output=True, text=True, timeout=180, check=False)
    result_path = output_dir / "agent_stdio_regression_result.json"
    data: dict[str, Any] = {}
    if result_path.exists():
        data = json.loads(result_path.read_text(encoding="utf-8"))
    nested_package_ok = _nested_stdio_package_evidence_ok(data, workspace=workspace, python_exe=python_exe)
    status = "PASS" if completed.returncode == 0 and data.get("ok") is True and nested_package_ok else "BLOCK"
    tool_count = int(data.get("tool_count", 0) or 0)
    tool_call_count = int(data.get("tool_call_count", 0) or len(data.get("tool_calls", []) or []))
    checks = data.get("checks", {})
    if isinstance(checks, dict):
        checks["nested_package_provenance"] = nested_package_ok
    failed_checks = [name for name, passed in checks.items() if passed is False] if isinstance(checks, dict) else []
    return {
        "id": "S00",
        "label": "Agent-only stdio baseline",
        "status": status,
        "started_at": started,
        "ended_at": _now(),
        "evidence_dir": str(scenario_dir),
        "result_path": str(result_path),
        "command": command,
        "returncode": completed.returncode,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
        "tool_count": tool_count,
        "tool_definition_count": tool_count,
        "tool_call_count": tool_call_count,
        **scenario_evidence_fields("S00"),
        "tool_count_note": "tool_definition_count is MCP server tool definitions; tool_call_count is runner-visible MCP calls. Nested stdio regression details are in result_path.",
        "nested_result_path": str(result_path),
        "nested_failed_checks": failed_checks,
        "nested_package_execution": data.get("package_execution", {}),
        "checks": checks,
        "recovery_checks": {
            "create_project_timeout_partial_success": checks.get("stdio_create_project_timeout_partial_success"),
            "add_project_files_timeout_repair_actions": checks.get("stdio_add_project_files_timeout_repair_actions"),
            "workflow_trace_unresolved_failure_cleared": checks.get("workflow_trace_unresolved_failure_cleared"),
        },
        "negative_path_summary": {
            "tcl_policy_blocked": checks.get("tcl_policy_blocked"),
            "hardware_gate_blocked": checks.get("hardware_gate_blocked"),
            "safe_tcl_literal_braces_dry_run": checks.get("safe_tcl_literal_braces_dry_run"),
        },
        "hardware_validation_status": "NOT_VALIDATED" if checks.get("scenario_feedback_template_not_validated") else "",
        "summary": "Agent-only stdio baseline passed." if status == "PASS" else "Agent-only stdio baseline failed.",
        "recommendations": [] if status == "PASS" else ["Open agent_stdio_regression_result.json and fix failed checks before running live scenarios."],
    }


async def run_s03_simulation_failure_repair(*, workspace: Path, scenario_dir: Path, python_exe: str) -> dict[str, Any]:
    fake_dir = scenario_dir / "fake_stdio_server"
    state_path = fake_dir / "simulation_repair_state.json"
    transcript: list[dict[str, Any]] = []
    started = _now()

    env = _stdio_env(workspace)
    params = StdioServerParameters(
        command=python_exe,
        args=["-c", _SIMULATION_REPAIR_FAKE_SERVER, str(fake_dir)],
        env=env,
        cwd=str(workspace),
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = await session.list_tools()
            scenario = await call_tool(session, "get_agent_scenarios", {"scenario_id": "S03"}, transcript)
            workflows = await call_tool(session, "get_agent_workflows", {}, transcript)
            guard = await call_tool(session, "run_behavioral_simulation", {"simset": "sim_1", "run_all": True, "export_vcd": True}, transcript)
            state_after_guard = read_json_file(state_path)
            launch_count_after_guard = int(state_after_guard.get("launch_count", -1))
            failed = await call_tool(session, "run_behavioral_simulation", {"simset": "sim_1", "run_time": "200 ns", "export_vcd": False, "max_vcd_mb": 64}, transcript)
            latest = await call_tool(session, "get_simulation_result", {"simset": "sim_1"}, transcript)
            rerun = await call_tool(session, "run_behavioral_simulation", {"simset": "sim_1", "run_time": "200 ns", "export_vcd": False, "max_vcd_mb": 64}, transcript)

    failed_data = failed.get("data") if isinstance(failed.get("data"), dict) else {}
    latest_data = latest.get("data") if isinstance(latest.get("data"), dict) else {}
    rerun_data = rerun.get("data") if isinstance(rerun.get("data"), dict) else {}
    failed_diagnosis = failed_data.get("simulation_diagnosis") if isinstance(failed_data.get("simulation_diagnosis"), dict) else {}
    rerun_diagnosis = rerun_data.get("simulation_diagnosis") if isinstance(rerun_data.get("simulation_diagnosis"), dict) else {}
    failed_actions = failed.get("next_actions", [])
    failed_action_tools = {item.get("tool") for item in failed_actions if isinstance(item, dict)}
    final_state = read_json_file(state_path)

    checks = {
        "scenario_available": _structured_ok(scenario)
        and (scenario.get("data") or {}).get("selected_count") == 1
        and (scenario.get("data") or {}).get("scenarios", [{}])[0].get("id") == "S03",
        "workflow_available": _structured_ok(workflows)
        and any(item.get("id") == "simulation_failure_repair" for item in ((workflows.get("data") or {}).get("workflows") or []) if isinstance(item, dict)),
        "vcd_guard_blocked": guard.get("ok") is False and guard.get("error_code") == "SIMULATION_RUN_ALL_VCD_BLOCKED",
        "vcd_guard_no_launch": launch_count_after_guard == 0,
        "initial_failure_has_primary_cause": failed_data.get("status") == "failed"
        and failed_diagnosis.get("primary_cause") == "testbench_failure",
        "initial_failure_uses_invocation_span": failed_data.get("status_source") == "simulation_invocation_log_span"
        and (failed_data.get("log_span") or {}).get("start") == 100
        and (failed_data.get("log_span") or {}).get("end") == 210,
        "failure_next_actions_route_repair": {"get_simulation_result", "analyze_sources", "check_syntax", "get_compile_order", "run_behavioral_simulation"} <= failed_action_tools,
        "latest_result_marked_latest_log_tail": latest_data.get("status_source") == "latest_log_tail"
        and latest_data.get("status") == "failed",
        "rerun_pass_uses_current_span": rerun_data.get("status") == "completed"
        and rerun_diagnosis.get("primary_cause") == "testbench_pass"
        and (rerun_data.get("log_span") or {}).get("start") == 210
        and (rerun_data.get("log_span") or {}).get("end") == 320,
    }
    result_path = scenario_dir / "simulation_repair_contract.json"
    result_path.write_text(
        json.dumps(
            {
                "checks": checks,
                "vcd_guard": summarize_structured(guard),
                "initial_failure": summarize_simulation_structured(failed),
                "latest_result": summarize_simulation_structured(latest),
                "rerun": summarize_simulation_structured(rerun),
                "command_count": len(final_state.get("commands", [])),
                "launch_count": int(final_state.get("launch_count", 0)),
                "tool_call_count": len(transcript),
                **scenario_evidence_fields("S03"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    status = "PASS" if all(checks.values()) else "BLOCK"
    return {
        "id": "S03",
        "label": "Simulation failure repair",
        "status": status,
        "started_at": started,
        "ended_at": _now(),
        "evidence_dir": str(scenario_dir),
        "result_path": str(result_path),
        "tool_count": len(tools.tools),
        "tool_definition_count": len(tools.tools),
        "tool_call_count": len(transcript),
        **scenario_evidence_fields("S03"),
        "tool_count_note": "tool_definition_count is MCP server tool definitions; tool_call_count is runner-visible MCP calls against a no-live fake Vivado simulation session.",
        "tool_calls": transcript,
        "checks": checks,
        "vcd_guard": summarize_structured(guard),
        "initial_failure": summarize_simulation_structured(failed),
        "latest_result": summarize_simulation_structured(latest),
        "rerun": summarize_simulation_structured(rerun),
        "next_actions": _summarize_next_actions(failed_actions),
        "fake_session_state_path": str(state_path),
        "fake_session_command_count": len(final_state.get("commands", [])),
        "fake_session_launch_count": int(final_state.get("launch_count", 0)),
        "hardware_validation_status": "NOT_VALIDATED",
        "summary": "Simulation failure repair contract passed." if status == "PASS" else "Simulation failure repair contract failed.",
        "recommendations": [] if status == "PASS" else ["Inspect simulation_repair_contract.json and harden simulation diagnosis or next_actions."],
    }


async def run_s03_live_simulation_failure_repair(
    *,
    workspace: Path,
    scenario_dir: Path,
    python_exe: str,
    part: str,
    vivado_timeout_s: int,
) -> dict[str, Any]:
    trusted_xsim = {
        "execution_intent": "execute the runner-generated S03 testbench inside the configured trusted test workspace",
        "confirm": "RUN_TRUSTED_XSIM",
        "incremental": False,
    }
    project_inputs = write_s03_live_project_sources(scenario_dir / "source_tree")
    project_dir = scenario_dir / "project"
    project_name = "s03_counter_repair"
    project_path = project_dir / f"{project_name}.xpr"
    result_path = scenario_dir / "simulation_repair_live_lite.json"
    transcript: list[dict[str, Any]] = []
    started = _now()
    stop_summary: dict[str, Any] = {}
    tools_count = 0
    failure_result: dict[str, Any] | None = None
    initial_retry: dict[str, Any] = {}

    async with mcp_stdio_session(workspace=workspace, python_exe=python_exe) as session:
        tools = await session.list_tools()
        tools_count = len(tools.tools)
        try:
            await call_tool(session, "get_tool_catalog", {}, transcript)
            await call_tool(session, "get_agent_workflows", {}, transcript)
            scenario = await call_tool(session, "get_agent_scenarios", {"scenario_id": "S03"}, transcript)
            start = await call_tool(
                session,
                "start_session",
                {"timeout_s": vivado_timeout_s, "runtime_dir": str(scenario_dir / "runtime")},
                transcript,
            )
            if not _structured_ok(start):
                failure_result = _s03_live_failure(
                    scenario_dir=scenario_dir,
                    result_path=result_path,
                    transcript=transcript,
                    tools=tools_count,
                    summary="S03 live-lite start_session failed.",
                    project_path=project_path,
                    started=started,
                )
                return failure_result
            create = await call_tool(session, "create_project", _create_project_args(project_inputs, project_dir, part, project_name=project_name), transcript)
            if not _structured_ok(create):
                failure_result = _s03_live_failure(
                    scenario_dir=scenario_dir,
                    result_path=result_path,
                    transcript=transcript,
                    tools=tools_count,
                    summary="S03 live-lite create_project failed.",
                    project_path=project_path,
                    started=started,
                )
                return failure_result
            repair_args = _repair_project_args(project_inputs, project_path)
            await call_tool(session, "repair_project_setup", repair_args | {"dry_run": True, "timeout_s": 120}, transcript)
            repair = await call_tool(session, "repair_project_setup", repair_args | {"dry_run": False, "timeout_s": 180}, transcript)
            if not _structured_ok(repair):
                failure_result = _s03_live_failure(
                    scenario_dir=scenario_dir,
                    result_path=result_path,
                    transcript=transcript,
                    tools=tools_count,
                    summary="S03 live-lite repair_project_setup failed.",
                    project_path=project_path,
                    started=started,
                )
                return failure_result
            await call_tool(session, "list_fileset_files", {"fileset": "sources_1", "timeout_s": 60}, transcript)
            await call_tool(session, "list_fileset_files", {"fileset": "sim_1", "timeout_s": 60}, transcript)
            await call_tool(session, "get_compile_order", {"fileset": "sources_1", "timeout_s": 120}, transcript)
            await call_tool(session, "get_compile_order", {"fileset": "sim_1", "timeout_s": 120}, transcript)
            syntax_before = await call_tool(session, "check_syntax", {"fileset": "sources_1", "timeout_s": 180}, transcript)
            guard = await call_tool(
                session,
                "run_behavioral_simulation",
                {"simset": "sim_1", "run_all": True, "export_vcd": True, "max_vcd_mb": 64, "timeout_s": 120},
                transcript,
            )
            first_run = await call_tool(
                session,
                "run_behavioral_simulation",
                {"simset": "sim_1", "run_time": "2 us", "export_vcd": False, "max_vcd_mb": 64, "timeout_s": 300, **trusted_xsim},
                transcript,
            )
            latest_after_failure = await call_tool(session, "get_simulation_result", {"simset": "sim_1", "timeout_s": 90}, transcript)
            if not _s03_initial_failure_detected(first_run):
                retry_stop = await call_tool(session, "stop_session", {}, transcript)
                retry_start = await call_tool(
                    session,
                    "start_session",
                    {"timeout_s": vivado_timeout_s, "runtime_dir": str(scenario_dir / "runtime_initial_retry")},
                    transcript,
                )
                retry_open = await call_tool(session, "open_project", {"project_path": str(project_path), "timeout_s": 180}, transcript)
                retry_clean = await call_tool(
                    session,
                    "clean_run_outputs",
                    {
                        "simsets": ["sim_1"],
                        "dry_run": False,
                        "intent": "clean generated XSIM state before retrying the expected S03 failure invocation",
                        "confirm": "CLEAN_RUN_OUTPUTS",
                        "timeout_s": 120,
                    },
                    transcript,
                )
                await call_tool(session, "update_project_compile_order", {"filesets": ["sources_1", "sim_1"], "timeout_s": 120}, transcript)
                first_run = await call_tool(
                    session,
                    "run_behavioral_simulation",
                    {"simset": "sim_1", "run_time": "2 us", "export_vcd": False, "max_vcd_mb": 64, "timeout_s": 300, **trusted_xsim},
                    transcript,
                )
                latest_after_failure = await call_tool(session, "get_simulation_result", {"simset": "sim_1", "timeout_s": 90}, transcript)
                initial_retry = {
                    "stop_session": summarize_structured(retry_stop),
                    "start_session": summarize_structured(retry_start),
                    "open_project": summarize_structured(retry_open),
                    "clean_run_outputs": summarize_structured(retry_clean),
                    "incremental_control": "run_behavioral_simulation incremental=false",
                }
            Path(project_inputs["faulty_rtl_file"]).write_text(project_inputs["fixed_rtl_text"], encoding="utf-8")
            await call_tool(session, "update_project_compile_order", {"filesets": ["sources_1", "sim_1"], "timeout_s": 120}, transcript)
            syntax_after = await call_tool(session, "check_syntax", {"fileset": "sources_1", "timeout_s": 180}, transcript)
            clean_sim_dry_run = await call_tool(session, "clean_run_outputs", {"simsets": ["sim_1"], "dry_run": True, "timeout_s": 120}, transcript)
            stop_after_failure = await call_tool(session, "stop_session", {}, transcript)
            restart_after_fix = await call_tool(
                session,
                "start_session",
                {"timeout_s": vivado_timeout_s, "runtime_dir": str(scenario_dir / "runtime_after_fix")},
                transcript,
            )
            open_after_fix = await call_tool(session, "open_project", {"project_path": str(project_path), "timeout_s": 180}, transcript)
            clean_sim_execute = await call_tool(
                session,
                "clean_run_outputs",
                {
                    "simsets": ["sim_1"],
                    "dry_run": False,
                    "intent": "clean stale XSIM outputs after applying the S03 DUT repair",
                    "confirm": "CLEAN_RUN_OUTPUTS",
                    "timeout_s": 120,
                },
                transcript,
            )
            await call_tool(session, "update_project_compile_order", {"filesets": ["sources_1", "sim_1"], "timeout_s": 120}, transcript)
            syntax_after_reopen = await call_tool(session, "check_syntax", {"fileset": "sources_1", "timeout_s": 180}, transcript)
            rerun = await call_tool(
                session,
                "run_behavioral_simulation",
                {"simset": "sim_1", "run_time": "2 us", "export_vcd": False, "max_vcd_mb": 64, "timeout_s": 300, **trusted_xsim},
                transcript,
            )
        finally:
            if not transcript or transcript[-1].get("tool") != "stop_session":
                try:
                    stop_summary = await call_tool(session, "stop_session", {}, transcript)
                except Exception as exc:  # noqa: BLE001 - preserve live-lite evidence.
                    stop_summary = {"ok": False, "error_code": exc.__class__.__name__, "message": str(exc)}
            if failure_result is not None:
                _refresh_s03_live_failure_result(result_path, transcript, stop_summary)
                failure_result["tool_call_count"] = len(transcript)
                failure_result["tool_calls"] = transcript
                failure_result["stop_session"] = summarize_structured(stop_summary)

    first_data = first_run.get("data") if isinstance(first_run.get("data"), dict) else {}
    first_diagnosis = first_data.get("simulation_diagnosis") if isinstance(first_data.get("simulation_diagnosis"), dict) else {}
    latest_data = latest_after_failure.get("data") if isinstance(latest_after_failure.get("data"), dict) else {}
    rerun_data = rerun.get("data") if isinstance(rerun.get("data"), dict) else {}
    rerun_diagnosis = rerun_data.get("simulation_diagnosis") if isinstance(rerun_data.get("simulation_diagnosis"), dict) else {}
    first_actions = first_run.get("next_actions", [])
    first_action_tools = {item.get("tool") for item in first_actions if isinstance(item, dict)}
    first_span = first_data.get("log_span") if isinstance(first_data.get("log_span"), dict) else {}
    rerun_span = rerun_data.get("log_span") if isinstance(rerun_data.get("log_span"), dict) else {}
    checks = {
        "scenario_available": _structured_ok(scenario)
        and (scenario.get("data") or {}).get("selected_count") == 1
        and (scenario.get("data") or {}).get("scenarios", [{}])[0].get("id") == "S03",
        "xsim_incremental_disabled": first_data.get("incremental_control") == "managed_preflight_set_false"
        and rerun_data.get("incremental_control") == "managed_preflight_set_false"
        and "set_property INCREMENTAL 0" in str(first_data.get("managed_simulation_policy_command", ""))
        and "set_property INCREMENTAL 0" in str(rerun_data.get("managed_simulation_policy_command", "")),
        "syntax_before_fault_is_valid": _structured_ok(syntax_before),
        "vcd_guard_blocked": guard.get("ok") is False and guard.get("error_code") == "SIMULATION_RUN_ALL_VCD_BLOCKED",
        "initial_failure_detected": first_data.get("status") == "failed" and first_diagnosis.get("primary_cause") == "testbench_failure",
        "initial_failure_uses_invocation_span": first_data.get("status_source") == "simulation_invocation_log_span"
        and int(first_span.get("end", 0) or 0) > int(first_span.get("start", 0) or 0),
        "failure_next_actions_route_repair": {"get_simulation_result", "check_syntax", "get_compile_order", "run_behavioral_simulation"} <= first_action_tools,
        "latest_result_reads_failure_tail": latest_data.get("status_source") == "latest_log_tail" and latest_data.get("status") == "failed",
        "syntax_after_fix_is_valid": _structured_ok(syntax_after) and _structured_ok(syntax_after_reopen),
        "simulation_cleanup_dry_run_is_narrow": _structured_ok(clean_sim_dry_run)
        and (clean_sim_dry_run.get("data") or {}).get("run_names") == []
        and (clean_sim_dry_run.get("data") or {}).get("simsets") == ["sim_1"],
        "simulation_cleanup_executed": _structured_ok(clean_sim_execute)
        and bool((clean_sim_execute.get("data") or {}).get("executed"))
        and (clean_sim_execute.get("data") or {}).get("simsets") == ["sim_1"],
        "session_restarted_after_fix": _structured_ok(stop_after_failure) and _structured_ok(restart_after_fix) and _structured_ok(open_after_fix),
        "rerun_passes": rerun_data.get("status") == "completed" and rerun_diagnosis.get("primary_cause") in {"testbench_pass", "completed", "completed_with_testbench_vcd"},
        "rerun_uses_current_invocation_span": rerun_data.get("status_source") == "simulation_invocation_log_span"
        and int(rerun_span.get("end", 0) or 0) > int(rerun_span.get("start", 0) or 0)
        and rerun_data.get("simulation_invocation_id") != first_data.get("simulation_invocation_id"),
        "stop_session_returned": bool(stop_summary.get("ok")),
    }
    status = "PASS" if all(checks.values()) else "BLOCK"
    payload = {
        "checks": checks,
        "project_path": str(project_path),
        "source_fix": {
            "file": project_inputs["faulty_rtl_file"],
            "description": "Initial DUT increments by two; fixed DUT increments by one to satisfy the self-checking testbench.",
        },
        "initial_retry": initial_retry,
        "vcd_guard": summarize_structured(guard),
        "incremental_control": "run_behavioral_simulation incremental=false",
        "initial_failure": summarize_simulation_structured(first_run),
        "latest_after_failure": summarize_simulation_structured(latest_after_failure),
        "clean_sim_dry_run": summarize_structured(clean_sim_dry_run),
        "clean_sim_execute": summarize_structured(clean_sim_execute),
        "stop_after_failure": summarize_structured(stop_after_failure),
        "restart_after_fix": summarize_structured(restart_after_fix),
        "open_after_fix": summarize_structured(open_after_fix),
        "rerun": summarize_simulation_structured(rerun),
        "stop_session": summarize_structured(stop_summary),
        "tool_call_count": len(transcript),
        **s03_live_evidence_fields(),
    }
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "id": "S03",
        "label": "Simulation failure repair live-lite",
        "status": status,
        "started_at": started,
        "ended_at": _now(),
        "executed": True,
        "evidence_dir": str(scenario_dir),
        "project_path": str(project_path),
        "result_path": str(result_path),
        "tool_count": tools_count,
        "tool_definition_count": tools_count,
        "tool_call_count": len(transcript),
        **s03_live_evidence_fields(),
        "tool_count_note": "tool_definition_count is MCP server tool definitions; tool_call_count is runner-visible MCP calls against a real Vivado XSIM repair flow.",
        "tool_calls": transcript,
        "checks": checks,
        "vcd_guard": summarize_structured(guard),
        "incremental_control": "run_behavioral_simulation incremental=false",
        "initial_failure": summarize_simulation_structured(first_run),
        "latest_after_failure": summarize_simulation_structured(latest_after_failure),
        "clean_sim_dry_run": summarize_structured(clean_sim_dry_run),
        "stop_after_failure": summarize_structured(stop_after_failure),
        "restart_after_fix": summarize_structured(restart_after_fix),
        "open_after_fix": summarize_structured(open_after_fix),
        "rerun": summarize_simulation_structured(rerun),
        "stop_session": summarize_structured(stop_summary),
        "next_actions": [] if status == "PASS" else _summarize_next_actions(first_actions),
        "hardware_validation_status": "NOT_VALIDATED",
        "summary": "S03 live-lite repaired a real XSIM testbench failure." if status == "PASS" else "S03 live-lite did not complete the real XSIM repair loop.",
        "recommendations": [] if status == "PASS" else ["Inspect simulation_repair_live_lite.json and the final failed tool call before rerunning S03 live-lite."],
    }


def _s03_live_failure(
    *,
    scenario_dir: Path,
    result_path: Path,
    transcript: list[dict[str, Any]],
    tools: int,
    summary: str,
    project_path: Path,
    started: str,
) -> dict[str, Any]:
    payload = {
        "summary": summary,
        "project_path": str(project_path),
        "tool_call_count": len(transcript),
        "tool_calls": transcript,
        **s03_live_evidence_fields(),
    }
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "id": "S03",
        "label": "Simulation failure repair live-lite",
        "status": "BLOCK",
        "started_at": started,
        "ended_at": _now(),
        "executed": True,
        "evidence_dir": str(scenario_dir),
        "project_path": str(project_path),
        "result_path": str(result_path),
        "tool_count": tools,
        "tool_definition_count": tools,
        "tool_call_count": len(transcript),
        **s03_live_evidence_fields(),
        "tool_count_note": "tool_definition_count is MCP server tool definitions; tool_call_count is runner-visible MCP calls against a real Vivado XSIM repair flow.",
        "tool_calls": transcript,
        "hardware_validation_status": "NOT_VALIDATED",
        "summary": summary,
        "recommendations": ["Inspect the last failed tool call and follow its next_actions before rerunning S03 live-lite."],
    }


def _refresh_s03_live_failure_result(result_path: Path, transcript: list[dict[str, Any]], stop_summary: dict[str, Any]) -> None:
    if not result_path.exists():
        return
    payload = read_json_file(result_path)
    if payload.get("execution_mode") != "mcp_stdio_live_xsim_repair":
        return
    payload["tool_call_count"] = len(transcript)
    payload["tool_calls"] = transcript
    payload["stop_session"] = summarize_structured(stop_summary)
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_s05_warn_handoff(*, workspace: Path, scenario_dir: Path, python_exe: str) -> dict[str, Any]:
    manifest_path = write_reviewable_warn_bundle(scenario_dir / "warn_handoff_bundle")
    transcript: list[dict[str, Any]] = []
    async with mcp_stdio_session(workspace=workspace, python_exe=python_exe) as session:
        tools = await session.list_tools()
        await call_tool(session, "get_tool_catalog", {}, transcript)
        await call_tool(session, "get_agent_scenarios", {"scenario_id": "S05"}, transcript)
        validate = await call_tool(session, "validate_diagnostic_bundle", {"manifest_path": str(manifest_path)}, transcript)
    outcome = classify_s05_validation(validate)
    return {
        "id": "S05",
        "label": "Reviewable WARN diagnostic handoff",
        "status": outcome["status"],
        "started_at": transcript[0]["started_at"] if transcript else _now(),
        "ended_at": _now(),
        "evidence_dir": str(scenario_dir),
        "manifest_path": str(manifest_path),
        "tool_count": len(tools.tools),
        "tool_definition_count": len(tools.tools),
        "tool_call_count": len(transcript),
        **scenario_evidence_fields("S05"),
        "tool_count_note": "tool_definition_count is MCP server tool definitions; tool_call_count is runner-visible MCP calls.",
        "tool_calls": transcript,
        "validation": summarize_structured(validate),
        "hardware_validation_status": outcome["hardware_validation_status"],
        "handoff_status": outcome["handoff_status"],
        "handoff_ready": outcome["handoff_ready"],
        "handoff_reviewable": outcome["handoff_reviewable"],
        "review_required_reasons": outcome["review_required_reasons"],
        "review_guidance": outcome["review_guidance"],
        "recommended_entrypoint": outcome["recommended_entrypoint"],
        "next_steps": outcome["next_steps"],
        "summary": outcome["summary"],
        "recommendations": outcome["recommendations"],
    }


async def run_s06_safety_gates(*, workspace: Path, scenario_dir: Path, python_exe: str) -> dict[str, Any]:
    bitstream_path = scenario_dir / "dummy.bit"
    bitstream_path.write_bytes(b"vmcp dummy bitstream for safety gate checks\n")
    manifest_path = scenario_dir / "artifact_manifest.json"
    manifest_path.write_text(
        json.dumps({"artifacts": [{"category": "bitstream", "export_path": str(bitstream_path)}]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    runtime_dir = scenario_dir / "runtime"
    ensure_runtime_identity(runtime_dir, workspace_root=workspace)
    (runtime_dir / ".Xil").mkdir(parents=True, exist_ok=True)
    (runtime_dir / "vivado_agent_mcp_probe.tcl").write_text("puts probe\n", encoding="utf-8")
    transcript: list[dict[str, Any]] = []
    async with mcp_stdio_session(
        workspace=workspace,
        python_exe=python_exe,
        runtime_dir=runtime_dir,
        tool_profile="all",
    ) as session:
        tools = await session.list_tools()
        await call_tool(session, "get_tool_catalog", {}, transcript)
        await call_tool(session, "get_agent_scenarios", {"scenario_id": "S06"}, transcript)
        dangerous_tcl = await call_tool(session, "run_tcl", {"command": "file delete -force D:/Vivado_Mcp/test_use/runner_should_not_delete.tmp"}, transcript)
        safe_tcl = await call_tool(session, "safe_tcl", {"template": "exec {program}", "args": {"program": "calc"}}, transcript)
        encoded_programming = [
            await call_tool(session, "run_tcl", {"command": command}, transcript)
            for command in (
                r"program_hw_d\145vices [current_hw_device]",
                r"program_hw_d\x65vices [current_hw_device]",
                r"program_hw_d\u0065vices [current_hw_device]",
            )
        ]
        hardware_manager = await call_tool(session, "list_hw_targets", {}, transcript)
        program_hw = await call_tool(session, "program_hw_device", {"bitstream_path": str(bitstream_path)}, transcript)
        program_manifest = await call_tool(session, "program_from_artifact_manifest", {"manifest_path": str(manifest_path)}, transcript)
        reset_dry_run = await call_tool(session, "reset_runs", {}, transcript)
        clean_outputs_dry_run = await call_tool(session, "clean_run_outputs", {}, transcript)
        runtime_status = await call_tool(session, "get_runtime_cache_status", {"runtime_dir": str(runtime_dir)}, transcript)
        runtime_clean_dry_run = await call_tool(session, "clean_runtime_cache", {"runtime_dir": str(runtime_dir), "dry_run": True}, transcript)
        cleanup_data = runtime_clean_dry_run.get("data") if isinstance(runtime_clean_dry_run.get("data"), dict) else {}
        cleanup_identity = cleanup_data.get("runtime_identity") if isinstance(cleanup_data.get("runtime_identity"), dict) else {}
        (runtime_dir / "runtime_plan_drift.tmp").write_text("created after dry-run review\n", encoding="utf-8")
        runtime_clean_drift = await call_tool(
            session,
            "clean_runtime_cache",
            {
                "runtime_dir": str(runtime_dir),
                "dry_run": False,
                "runtime_identity": str(cleanup_identity.get("runtime_id", "")),
                "plan_sha256": str(cleanup_data.get("plan_sha256", "")),
            },
            transcript,
        )

    program_hw_status = _hardware_status_from_structured(program_hw)
    program_manifest_status = _hardware_status_from_structured(program_manifest)
    observed_hardware_status = _first_hardware_status(transcript)
    checks = {
        "dangerous_tcl_blocked": _error_code(dangerous_tcl) == "TCL_POLICY_BLOCKED",
        "safe_tcl_external_blocked": _error_code(safe_tcl) == "TCL_POLICY_BLOCKED",
        "encoded_hardware_programming_blocked": all(
            _error_code(result) == "RAW_TCL_HARDWARE_PROGRAMMING_FORBIDDEN"
            for result in encoded_programming
        ),
        "hardware_manager_default_blocked": _error_code(hardware_manager) == "HARDWARE_MODE_DISABLED",
        "program_hw_intent_gate": _error_code(program_hw) == "HARDWARE_INTENT_REQUIRED",
        "program_hw_hardware_not_validated": program_hw_status == "NOT_VALIDATED",
        "program_manifest_intent_gate": _error_code(program_manifest) == "HARDWARE_INTENT_REQUIRED",
        "program_manifest_hardware_not_validated": program_manifest_status == "NOT_VALIDATED",
        "reset_runs_default_dry_run": _structured_ok(reset_dry_run) and bool((reset_dry_run.get("data") or {}).get("dry_run")),
        "clean_run_outputs_default_dry_run": _structured_ok(clean_outputs_dry_run) and bool((clean_outputs_dry_run.get("data") or {}).get("dry_run")),
        "runtime_status_collected": _structured_ok(runtime_status),
        "runtime_cleanup_dry_run": _structured_ok(runtime_clean_dry_run)
        and bool((runtime_clean_dry_run.get("data") or {}).get("dry_run"))
        and bool(cleanup_identity.get("runtime_id"))
        and bool(cleanup_data.get("plan_sha256")),
        "runtime_cleanup_plan_drift_blocked": _structured_ok(runtime_clean_drift) is False
        and (runtime_clean_drift.get("data") or {}).get("reason") == "cleanup_plan_mismatch",
    }
    status = "PASS" if all(checks.values()) else "BLOCK"
    return {
        "id": "S06",
        "label": "Safety gate negative paths",
        "status": status,
        "started_at": transcript[0]["started_at"] if transcript else _now(),
        "ended_at": _now(),
        "evidence_dir": str(scenario_dir),
        "tool_count": len(tools.tools),
        "tool_definition_count": len(tools.tools),
        "tool_profile": "all",
        "tool_call_count": len(transcript),
        **scenario_evidence_fields("S06"),
        "tool_count_note": "tool_definition_count is MCP server tool definitions; tool_call_count is runner-visible MCP calls.",
        "tool_calls": transcript,
        "checks": checks,
        "hardware_validation_status": observed_hardware_status or "MISSING",
        "summary": "Safety gate negative paths passed." if status == "PASS" else "Safety gate negative paths need review.",
        "recommendations": [] if status == "PASS" else ["Inspect failed S06 checks and harden safety gate responses before release."],
    }


async def run_s07_existing_project_handoff(*, workspace: Path, scenario_dir: Path, python_exe: str) -> dict[str, Any]:
    manifest_path = write_existing_project_handoff_bundle(scenario_dir / "existing_project_bundle")
    replay_path = manifest_path.parent / "replay_project.tcl"
    trace_path = manifest_path.parent / "workflow_trace.jsonl"
    transcript: list[dict[str, Any]] = []
    async with mcp_stdio_session(workspace=workspace, python_exe=python_exe) as session:
        tools = await session.list_tools()
        await call_tool(session, "get_tool_catalog", {}, transcript)
        await call_tool(session, "get_agent_workflows", {}, transcript)
        scenario = await call_tool(session, "get_agent_scenarios", {"scenario_id": "S07"}, transcript)
        validate_initial = await call_tool(session, "validate_diagnostic_bundle", {"manifest_path": str(manifest_path)}, transcript)
        now = datetime.now(timezone.utc)
        WorkflowTracer(trace_id=trace_path.stem, trace_dir=trace_path.parent).record(
            tool="validate_diagnostic_bundle",
            args={"manifest_path": str(manifest_path)},
            result={"ok": True, "tool": "validate_diagnostic_bundle", "message": "Synthetic append-only handoff validation."},
            started_at=now,
            ended_at=now,
        )
        validate_append = await call_tool(session, "validate_diagnostic_bundle", {"manifest_path": str(manifest_path)}, transcript)
        trace_status = await call_tool(session, "get_workflow_trace_status", {}, transcript)

    append_health = ((validate_append.get("data") or {}).get("health") or {}) if isinstance(validate_append.get("data"), dict) else {}
    initial_health = ((validate_initial.get("data") or {}).get("health") or {}) if isinstance(validate_initial.get("data"), dict) else {}
    resume = ((validate_append.get("data") or {}).get("resume_context") or {}) if isinstance(validate_append.get("data"), dict) else {}
    primary_refs = resume.get("primary_file_refs") if isinstance(resume.get("primary_file_refs"), dict) else {}
    workflow_trace_ref = resume.get("workflow_trace_ref") if isinstance(resume.get("workflow_trace_ref"), dict) else {}
    replay_script_ref = primary_refs.get("replay_script") if isinstance(primary_refs.get("replay_script"), dict) else {}
    trace_snapshot = _verified_evidence_snapshot(workflow_trace_ref, root=manifest_path.parent)
    replay_snapshot = _verified_evidence_snapshot(replay_script_ref, root=manifest_path.parent)
    replay_text = replay_snapshot.content.decode("utf-8") if replay_snapshot is not None else ""
    checks = {
        "scenario_available": _structured_ok(scenario)
        and (scenario.get("data") or {}).get("selected_count") == 1
        and (scenario.get("data") or {}).get("scenarios", [{}])[0].get("id") == "S07",
        "initial_reference_index_reviewable": _structured_ok(validate_initial)
        and (validate_initial.get("data") or {}).get("status") == "WARN"
        and (validate_initial.get("data") or {}).get("handoff_ready") is False
        and (validate_initial.get("data") or {}).get("handoff_reviewable") is True
        and initial_health.get("bundle_mode") == "reference"
        and initial_health.get("portable") is False,
        "append_only_trace_growth_reviewed": _structured_ok(validate_append)
        and (validate_append.get("data") or {}).get("status") == "WARN"
        and bool(append_health.get("workflow_trace_append_only_growth"))
        and append_health.get("bundle_mode") == "reference"
        and append_health.get("portable") is False
        and append_health.get("handoff_ready") is False,
        "resume_context_has_trace": trace_snapshot is not None
        and resume.get("handoff_ready") is False
        and resume.get("handoff_reviewable") is True,
        "replay_script_existing_project_oriented": "open_project" in replay_text and "create_project" not in replay_text,
        "workflow_trace_status_structured": _structured_ok(trace_status)
        and bool((trace_status.get("data") or {}).get("trace_path")),
        "hardware_boundary_not_validated": _hardware_status_from_structured(validate_append) == "NOT_VALIDATED",
    }
    status = "PASS" if all(checks.values()) else "BLOCK"
    return {
        "id": "S07",
        "label": "Existing project audit and replay handoff",
        "status": status,
        "started_at": transcript[0]["started_at"] if transcript else _now(),
        "ended_at": _now(),
        "evidence_dir": str(scenario_dir),
        "manifest_path": str(manifest_path),
        "replay_script_path": str(replay_snapshot.path) if replay_snapshot is not None else "",
        "workflow_trace_path": str(trace_snapshot.path) if trace_snapshot is not None else "",
        "tool_count": len(tools.tools),
        "tool_definition_count": len(tools.tools),
        "tool_call_count": len(transcript),
        **scenario_evidence_fields("S07"),
        "tool_count_note": "tool_definition_count is MCP server tool definitions; tool_call_count is runner-visible MCP calls against a synthetic existing-project diagnostic bundle fixture.",
        "tool_calls": transcript,
        "checks": checks,
        "initial_validation": summarize_structured(validate_initial),
        "append_validation": summarize_structured(validate_append),
        "resume_context": resume,
        "workflow_trace_status": summarize_structured(trace_status),
        "hardware_validation_status": _hardware_status_from_structured(validate_append),
        "handoff_status": (validate_append.get("data") or {}).get("status", ""),
        "handoff_ready": (validate_append.get("data") or {}).get("handoff_ready"),
        "handoff_reviewable": (validate_append.get("data") or {}).get("handoff_reviewable"),
        "summary": "Existing project handoff contract passed." if status == "PASS" else "Existing project handoff contract failed.",
        "recommendations": [] if status == "PASS" else ["Inspect S07 checks and harden existing-project handoff, replay, or trace validation."],
    }


async def run_s07_live_existing_project_handoff(
    *,
    workspace: Path,
    scenario_dir: Path,
    python_exe: str,
    part: str,
    vivado_timeout_s: int,
    poll_timeout_s: int,
    poll_interval_s: int,
) -> dict[str, Any]:
    started = _now()
    scenario_dir.mkdir(parents=True, exist_ok=True)
    result_path = scenario_dir / "existing_project_handoff_live_lite.json"
    seed_dir = scenario_dir / "seed_project"
    receiver_runtime = scenario_dir / "receiver_runtime"
    seed_result = await run_live_project_flow(
        workspace=workspace,
        scenario_dir=seed_dir,
        python_exe=python_exe,
        include_live_vivado=True,
        part=part,
        vivado_timeout_s=vivado_timeout_s,
        poll_timeout_s=poll_timeout_s,
        poll_interval_s=poll_interval_s,
        scenario_id="S01",
        label="S07 seed minimal counter Project Mode closure",
        project_name="s07_seed_counter",
        source_writer=write_s01_project_sources,
        sim_run_time="20 us",
    )

    seed_project_path = str(seed_result.get("project_path", "") or "")
    seed_project = Path(seed_project_path) if seed_project_path else Path()
    if seed_result.get("status") != "PASS" or not seed_project_path or not seed_project.exists():
        return _s07_live_seed_failure(
            scenario_dir=scenario_dir,
            result_path=result_path,
            seed_result=seed_result,
            started=started,
            summary="S07 live-lite seed project did not reach a handoff-ready Project Mode checkpoint.",
        )

    transcript: list[dict[str, Any]] = []
    tools_count = 0
    blocker = ""
    stop_summary: dict[str, Any] = {}
    scenario: dict[str, Any] = {}
    project_state: dict[str, Any] = {}
    audit: dict[str, Any] = {}
    bundle: dict[str, Any] = {}
    validation: dict[str, Any] = {}
    trace_status: dict[str, Any] = {}
    simulation_refresh: dict[str, Any] = {}
    receiver_artifacts: dict[str, Any] = {}
    receiver_reports: dict[str, Any] = {}
    partial_handoff = seed_result.get("partial_handoff") if isinstance(seed_result.get("partial_handoff"), dict) else {}
    artifact_manifest_path = str(partial_handoff.get("artifact_manifest_path", "") or "")
    manifest_path = str(seed_result.get("manifest_path", "") or partial_handoff.get("diagnostic_manifest_path", ""))
    report_manifest_path = str(partial_handoff.get("report_manifest_path", "") or "")
    if manifest_path:
        bundle = {
            "ok": True,
            "tool": "producer_diagnostic_bundle",
            "message": "Receiver is validating producer diagnostic evidence without recollecting it from the original project.",
            "error_code": "",
            "data": {"status": "READY", "manifest_path": manifest_path},
        }

    async with mcp_stdio_session(workspace=workspace, python_exe=python_exe) as session:
        tools = await session.list_tools()
        tools_count = len(tools.tools)
        try:
            await call_tool(session, "get_tool_catalog", {}, transcript)
            await call_tool(session, "get_agent_workflows", {}, transcript)
            scenario = await call_tool(session, "get_agent_scenarios", {"scenario_id": "S07"}, transcript)
            start = await call_tool(
                session,
                "start_session",
                {"timeout_s": vivado_timeout_s, "runtime_dir": str(receiver_runtime)},
                transcript,
            )
            if not _structured_ok(start):
                blocker = "receiver start_session failed."
            if not blocker:
                opened = await call_tool(session, "open_project", {"project_path": seed_project_path, "timeout_s": 180}, transcript)
                if not _structured_ok(opened):
                    blocker = "receiver open_project failed."
            if not blocker:
                project_state = await call_tool(session, "get_project_state", {"timeout_s": 120}, transcript)
                if not _structured_ok(project_state):
                    blocker = "receiver get_project_state failed."
            if not blocker:
                for fileset in ("sources_1", "constrs_1", "sim_1"):
                    fileset_result = await call_tool(session, "list_fileset_files", {"fileset": fileset, "timeout_s": 120}, transcript)
                    if not _structured_ok(fileset_result):
                        blocker = f"receiver list_fileset_files failed for {fileset}."
                        break
            if not blocker and not manifest_path:
                blocker = "producer diagnostic manifest is unavailable for inspection-only receiver validation."
            if manifest_path and not blocker:
                validation = await call_tool(session, "validate_diagnostic_bundle", {"manifest_path": manifest_path}, transcript)
                if not _tool_status_ok(validation):
                    blocker = "receiver validate_diagnostic_bundle failed or returned BLOCK."
            if not blocker:
                trace_status = await call_tool(session, "get_workflow_trace_status", {}, transcript)
                if not _structured_ok(trace_status):
                    blocker = "receiver get_workflow_trace_status failed."
        finally:
            if not transcript or transcript[-1].get("tool") != "stop_session":
                try:
                    stop_summary = await call_tool(session, "stop_session", {}, transcript)
                except Exception as exc:  # noqa: BLE001 - preserve receiver failure evidence.
                    stop_summary = {"ok": False, "error_code": exc.__class__.__name__, "message": str(exc)}

    outcome = classify_live_project_status(transcript, validation, scenario_id="S07", handoff_blocker=blocker)
    payload = _s07_live_result_payload(
        scenario_dir=scenario_dir,
        result_path=result_path,
        started=started,
        seed_result=seed_result,
        seed_project_path=seed_project_path,
        tools_count=tools_count,
        transcript=transcript,
        scenario=scenario,
        project_state=project_state,
        receiver_artifacts=receiver_artifacts,
        receiver_reports=receiver_reports,
        audit=audit,
        bundle=bundle,
        validation=validation,
        trace_status=trace_status,
        stop_summary=stop_summary,
        blocker=blocker,
        outcome=outcome,
        artifact_manifest_path=artifact_manifest_path,
        report_manifest_path=report_manifest_path,
    )
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _s07_live_seed_failure(
    *,
    scenario_dir: Path,
    result_path: Path,
    seed_result: dict[str, Any],
    started: str,
    summary: str,
) -> dict[str, Any]:
    payload = {
        "id": "S07",
        "label": "Existing project audit and replay handoff live-lite",
        "status": "BLOCK",
        "started_at": started,
        "ended_at": _now(),
        "executed": True,
        "evidence_dir": str(scenario_dir),
        "result_path": str(result_path),
        **s07_live_evidence_fields(),
        "receiver_created_project": False,
        "seed_project_path": str(seed_result.get("project_path", "") or ""),
        "receiver_project_path": "",
        "seed_status": seed_result.get("status", ""),
        "seed_checkpoint_path": seed_result.get("checkpoint_path", ""),
        "seed_progress_path": seed_result.get("progress_path", ""),
        "partial_handoff": seed_result.get("partial_handoff", {}),
        "tool_count": int(seed_result.get("tool_count", 0) or 0),
        "tool_definition_count": int(seed_result.get("tool_definition_count", 0) or 0),
        "tool_call_count": int(seed_result.get("tool_call_count", 0) or 0),
        "tool_count_note": "tool_calls are from the failed producer seed stage because the receiver stage never started.",
        "tool_calls": seed_result.get("tool_calls", []),
        "seed_result": _summarize_seed_result(seed_result),
        "result_kind": "seed_failure_checkpoint",
        "hardware_validation_status": "NOT_VALIDATED",
        "handoff_ready": False,
        "handoff_reviewable": False,
        "summary": summary,
        "recommendations": ["Inspect the seed project checkpoint, partial_handoff, and last failed tool before rerunning S07 live-lite."],
    }
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _s07_live_result_payload(
    *,
    scenario_dir: Path,
    result_path: Path,
    started: str,
    seed_result: dict[str, Any],
    seed_project_path: str,
    tools_count: int,
    transcript: list[dict[str, Any]],
    scenario: dict[str, Any],
    project_state: dict[str, Any],
    receiver_artifacts: dict[str, Any],
    receiver_reports: dict[str, Any],
    audit: dict[str, Any],
    bundle: dict[str, Any],
    validation: dict[str, Any],
    trace_status: dict[str, Any],
    stop_summary: dict[str, Any],
    blocker: str,
    outcome: dict[str, Any],
    artifact_manifest_path: str,
    report_manifest_path: str,
) -> dict[str, Any]:
    manifest_path = _data_string(bundle, "manifest_path") or _data_string(bundle, "partial_manifest_path")
    resume = ((validation.get("data") or {}).get("resume_context") or {}) if isinstance(validation.get("data"), dict) else {}
    workflow_trace_ref = resume.get("workflow_trace_ref") if isinstance(resume.get("workflow_trace_ref"), dict) else {}
    primary_refs = resume.get("primary_file_refs") if isinstance(resume.get("primary_file_refs"), dict) else {}
    replay_script_ref = primary_refs.get("replay_script") if isinstance(primary_refs.get("replay_script"), dict) else {}
    bundle_root = Path(manifest_path).resolve().parent if manifest_path else None
    workflow_trace_snapshot = (
        _verified_evidence_snapshot(workflow_trace_ref, root=bundle_root)
        if bundle_root is not None
        else None
    )
    replay_script_snapshot = (
        _verified_evidence_snapshot(replay_script_ref, root=bundle_root)
        if bundle_root is not None
        else None
    )
    workflow_trace_path = str(workflow_trace_snapshot.path) if workflow_trace_snapshot is not None else ""
    replay_script_path = str(replay_script_snapshot.path) if replay_script_snapshot is not None else ""
    receiver_tools = [str(item.get("tool", "")) for item in transcript]
    required_receiver_tool_chain = (
        "get_tool_catalog",
        "get_agent_workflows",
        "get_agent_scenarios",
        "start_session",
        "open_project",
        "get_project_state",
        "list_fileset_files",
        "validate_diagnostic_bundle",
        "get_workflow_trace_status",
        "stop_session",
    )
    validation_data = validation.get("data") if isinstance(validation.get("data"), dict) else {}
    handoff_ready = bool(validation_data.get("handoff_ready"))
    handoff_reviewable = bool(validation_data.get("handoff_reviewable"))
    hardware_status = _hardware_status_from_structured(validation) or "NOT_VALIDATED"
    checks = {
        "seed_project_ready": seed_result.get("status") == "PASS" and bool(seed_project_path) and Path(seed_project_path).exists(),
        "scenario_available": _structured_ok(scenario)
        and (scenario.get("data") or {}).get("selected_count") == 1
        and (scenario.get("data") or {}).get("scenarios", [{}])[0].get("id") == "S07",
        "receiver_no_create_project": "create_project" not in receiver_tools,
        "receiver_inspection_only": not ({
            "run_behavioral_simulation",
            "run_synthesis",
            "run_implementation",
            "generate_bitstream",
            "collect_build_artifacts",
            "collect_report_bundle",
            "run_pre_hw_signoff",
            "run_project_audit",
            "collect_diagnostic_bundle",
            "repair_project_setup",
        } & set(receiver_tools)),
        "receiver_opened_existing_project": "open_project" in receiver_tools,
        "receiver_required_tools_present": set(required_receiver_tool_chain) <= set(receiver_tools),
        "receiver_required_tool_chain_order": _tools_in_order(receiver_tools, required_receiver_tool_chain),
        "diagnostic_bundle_validate_handoff": _tool_status_ok(validation)
        and hardware_status == "NOT_VALIDATED"
        and (validation_data.get("status") == "READY" or handoff_reviewable),
        "workflow_trace_status_structured": _structured_ok(trace_status) and bool(workflow_trace_path),
        "workflow_trace_reference_bound": workflow_trace_snapshot is not None,
        "replay_script_present": replay_script_snapshot is not None,
        "hardware_boundary_not_validated": hardware_status == "NOT_VALIDATED",
    }
    final_status = "PASS" if not blocker and outcome.get("status") == "PASS" and all(checks.values()) else "BLOCK"
    return {
        "id": "S07",
        "label": "Existing project audit and replay handoff live-lite",
        "status": final_status,
        "started_at": started,
        "ended_at": _now(),
        "executed": True,
        "evidence_dir": str(scenario_dir),
        "result_path": str(result_path),
        "seed_project_path": seed_project_path,
        "receiver_project_path": seed_project_path,
        "receiver_created_project": False,
        "manifest_path": manifest_path,
        "workflow_trace_path": workflow_trace_path,
        "replay_script_path": replay_script_path,
        "tool_count": tools_count,
        "tool_definition_count": tools_count,
        "tool_call_count": len(transcript),
        **s07_live_evidence_fields(),
        "tool_count_note": "tool_definition_count is MCP server tool definitions; tool_call_count is receiver-stage MCP calls only. Seed producer evidence is nested under seed_result.",
        "tool_calls": transcript,
        "seed_result": _summarize_seed_result(seed_result),
        "receiver_checks": checks,
        "checks": checks,
        "project_state": summarize_structured(project_state),
        "artifact_bundle": summarize_structured(receiver_artifacts),
        "report_bundle": summarize_structured(receiver_reports),
        "audit": summarize_structured(audit),
        "bundle": summarize_structured(bundle),
        "final_validation": summarize_structured(validation),
        "workflow_trace_status": summarize_structured(trace_status),
        "stop_session": summarize_structured(stop_summary),
        "artifact_manifest_path": artifact_manifest_path,
        "report_manifest_path": report_manifest_path,
        "handoff_blocker": blocker,
        "hardware_validation_status": hardware_status,
        "handoff_status": outcome.get("handoff_status", ""),
        "handoff_ready": handoff_ready,
        "handoff_reviewable": handoff_reviewable,
        "review_required_reasons": outcome.get("review_required_reasons", []),
        "review_guidance": outcome.get("review_guidance", {}),
        "recommended_entrypoint": outcome.get("recommended_entrypoint", ""),
        "next_steps": outcome.get("next_steps", []),
        "summary": "S07 receiver inspected an existing Project Mode project and validated producer handoff evidence without executing or mutating the original project."
        if final_status == "PASS"
        else f"S07 live-lite receiver handoff needs review: {blocker or 'one or more receiver checks failed.'}",
        "recommendations": []
        if final_status == "PASS"
        else ["Inspect receiver tool_calls, workflow_trace_path, replay_script_path, and final_validation before rerunning S07 live-lite."],
    }


def _summarize_seed_result(seed_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": seed_result.get("id", ""),
        "status": seed_result.get("status", ""),
        "project_path": seed_result.get("project_path", ""),
        "manifest_path": seed_result.get("manifest_path", ""),
        "checkpoint_path": seed_result.get("checkpoint_path", ""),
        "progress_path": seed_result.get("progress_path", ""),
        "handoff_status": seed_result.get("handoff_status", ""),
        "handoff_ready": seed_result.get("handoff_ready", False),
        "handoff_reviewable": seed_result.get("handoff_reviewable", False),
        "hardware_validation_status": seed_result.get("hardware_validation_status", ""),
        "handoff_blocker": seed_result.get("handoff_blocker", ""),
        "partial_handoff": seed_result.get("partial_handoff", {}),
        "tool_call_count": seed_result.get("tool_call_count", 0),
    }


def _verified_evidence_snapshot(reference: dict[str, Any], *, root: Path):
    try:
        return verify_evidence_reference(reference, root=root, max_bytes=64 * 1024 * 1024)
    except (OSError, ValueError):
        return None


def _tools_in_order(actual_tools: list[str], required_tools: tuple[str, ...]) -> bool:
    cursor = 0
    for tool in actual_tools:
        if cursor < len(required_tools) and tool == required_tools[cursor]:
            cursor += 1
    return cursor == len(required_tools)


def run_s04_partial_setup_recovery(*, workspace: Path, scenario_dir: Path, python_exe: str) -> dict[str, Any]:
    output_dir = scenario_dir / "agent_stdio_regression"
    command = _nested_stdio_regression_command(workspace, output_dir=output_dir, python_exe=python_exe)
    env = _stdio_env(workspace)
    started = _now()
    completed = subprocess.run(command, cwd=str(workspace), env=env, capture_output=True, text=True, timeout=180, check=False)
    result_path = output_dir / "agent_stdio_regression_result.json"
    data: dict[str, Any] = {}
    if result_path.exists():
        data = json.loads(result_path.read_text(encoding="utf-8"))
    checks = data.get("checks", {}) if isinstance(data.get("checks"), dict) else {}
    failed_checks = [name for name, passed in checks.items() if passed is False]
    s04_checks = {
        "create_project_timeout_partial_success": bool(checks.get("stdio_create_project_timeout_partial_success")),
        "add_project_files_timeout_repair_actions": bool(checks.get("stdio_add_project_files_timeout_repair_actions")),
        "repair_project_setup_dry_run_structured": bool(checks.get("repair_project_setup_dry_run_structured")),
        "nested_package_provenance": _nested_stdio_package_evidence_ok(data, workspace=workspace, python_exe=python_exe),
    }
    status = "PASS" if completed.returncode == 0 and all(s04_checks.values()) else "BLOCK"
    tool_count = int(data.get("tool_count", 0) or 0)
    tool_call_count = int(data.get("tool_call_count", 0) or len(data.get("tool_calls", []) or []))
    return {
        "id": "S04",
        "label": "Partial project setup recovery",
        "status": status,
        "started_at": started,
        "ended_at": _now(),
        "evidence_dir": str(scenario_dir),
        "result_path": str(result_path),
        "command": command,
        "returncode": completed.returncode,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
        "checks": s04_checks,
        "tool_count": tool_count,
        "tool_definition_count": tool_count,
        "tool_call_count": tool_call_count,
        **scenario_evidence_fields("S04"),
        "tool_count_note": "tool_definition_count is MCP server tool definitions; tool_call_count is runner-visible MCP calls. Nested stdio regression details are in result_path.",
        "nested_result_path": str(result_path),
        "nested_failed_checks": failed_checks,
        "nested_package_execution": data.get("package_execution", {}),
        "hardware_validation_status": "NOT_VALIDATED",
        "next_actions": [
            {
                "tool": "open_project",
                "reason": "If create_project timed out after writing an .xpr, open it for inspection only; pathname existence did not establish a project capability.",
                "required_args": ["project_path"],
                "arg_sources": {"project_path": "create_project partial_success data.project_path"},
                "preconditions": ["create_project returned TimeoutError with partial_success=true."],
                "stop_condition": "open_project reports mutation_policy.scope=existing_project_read_only.",
                "optional": False,
            },
            {
                "tool": "create_project",
                "reason": "Rebuild reviewed inputs in a distinct empty directory to establish a new object-bound project capability.",
                "required_args": ["project_name", "project_dir", "part", "top", "rtl_files"],
                "arg_sources": {"project_name/project_dir": "new recovery project identity", "part/top/rtl_files/xdc_files/sim_files/testbench_top": "original project setup request"},
                "preconditions": ["The partial project was inspected without mutation and source inputs remain trusted."],
                "stop_condition": "create_project returns project_capability.bound=true for the distinct path.",
                "optional": False,
            },
            {
                "tool": "repair_project_setup",
                "reason": "Repair a later add/configure setup timeout only when the successfully created project capability remains valid.",
                "required_args": [],
                "arg_sources": {"rtl_files/xdc_files/sim_files/top/testbench_top": "original project setup request"},
                "preconditions": ["The active project was created successfully by this MCP server and remains capability-bound."],
                "stop_condition": "repair_project_setup returns setup_status READY or actionable missing_after_repair.",
                "optional": True,
            },
        ],
        "summary": "Partial project setup recovery checks passed." if status == "PASS" else "Partial project setup recovery checks failed.",
        "recommendations": [] if status == "PASS" else ["Open agent_stdio_regression_result.json and fix timeout recovery checks before release."],
    }


async def run_live_project_flow(
    *,
    workspace: Path,
    scenario_dir: Path,
    python_exe: str,
    include_live_vivado: bool,
    part: str,
    vivado_timeout_s: int,
    poll_timeout_s: int,
    poll_interval_s: int,
    scenario_id: str,
    label: str,
    project_name: str,
    source_writer: Any,
    sim_run_time: str,
) -> dict[str, Any]:
    if not include_live_vivado:
        return {
            "id": scenario_id,
            "label": label,
            "status": "BLOCK",
            "executed": False,
            "skipped_live": True,
            "requires_live_vivado": True,
            "required_flag": "--include-live-vivado",
            "error_code": "LIVE_VIVADO_NOT_ENABLED",
            "evidence_dir": str(scenario_dir),
            "tool_count": 0,
            "tool_definition_count": 0,
            "tool_call_count": 0,
            **scenario_evidence_fields(
                scenario_id,
                execution_mode="live_project_not_executed",
                evidence_class="live_project_required",
                full_scenario_coverage=False,
            ),
            "hardware_validation_status": "NOT_VALIDATED",
            "summary": f"{scenario_id} live Vivado flow was requested but --include-live-vivado was not set.",
            "recommendations": [f"Rerun with --scenarios {scenario_id} --include-live-vivado when a local Vivado GUI session is allowed."],
            "next_actions": [
                {
                    "tool": "tests/agent_scenario_runner.py",
                    "reason": f"Authorize live Vivado execution before running {scenario_id}.",
                    "required_args": ["--scenarios", "--include-live-vivado"],
                    "arg_sources": {"--scenarios": scenario_id, "--include-live-vivado": "explicit user or CI authorization"},
                    "preconditions": ["A visible local Vivado GUI session may be started and long Project Mode runs are allowed."],
                    "stop_condition": f"{scenario_id} returns PASS/WATCH/BLOCK with executed=true.",
                    "optional": False,
                }
            ],
        }

    project_inputs = source_writer(scenario_dir / "source_tree")
    project_dir = scenario_dir / "project"
    project_path = project_dir / f"{project_name}.xpr"
    transcript: list[dict[str, Any]] = []
    manifest_path = ""
    final_validation: dict[str, Any] = {}
    stop_summary: dict[str, Any] = {}
    artifacts: dict[str, Any] = {}
    reports: dict[str, Any] = {}
    signoff: dict[str, Any] = {}
    audit: dict[str, Any] = {}
    bundle: dict[str, Any] = {}
    handoff_blocker = ""
    checkpoint_path = scenario_dir / "agent_scenario_runner_checkpoint.json"
    progress_path = scenario_dir / "scenario_progress.jsonl"

    def write_checkpoint(phase: str, summary: str = "") -> None:
        partial = _live_partial_handoff(
            project_path=project_path,
            project_dir=project_dir,
            artifacts=artifacts,
            reports=reports,
            signoff=signoff,
            audit=audit,
            bundle=bundle,
            final_validation=final_validation,
            bitstream_completed=True,
            handoff_blocker=handoff_blocker,
        )
        payload = {
            "ok": False,
            "passed": False,
            "blocked": False,
            "watch": True,
            "phase": phase,
            "summary": summary or f"{scenario_id} live flow checkpoint after {phase}.",
            "scenario_results": [
                {
                    "id": scenario_id,
                    "label": label,
                    "status": "RUNNING",
                    "executed": True,
                    "evidence_dir": str(scenario_dir),
                    "project_path": str(project_path),
                    "tool_call_count": len(transcript),
                    **scenario_evidence_fields(scenario_id),
                    "tool_calls": transcript,
                    "partial_handoff": partial,
                    "hardware_validation_status": "NOT_VALIDATED",
                }
            ],
            "hardware_validation_status": "NOT_VALIDATED",
        }
        checkpoint_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if not progress_path.exists():
            progress_path.write_text("", encoding="utf-8")
        with progress_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": _now(), "phase": phase, "summary": payload["summary"], "tool_call_count": len(transcript)}, ensure_ascii=False) + "\n")

    async with mcp_stdio_session(workspace=workspace, python_exe=python_exe) as session:
        tools = await session.list_tools()
        try:
            await call_tool(session, "get_tool_catalog", {}, transcript)
            await call_tool(session, "get_agent_workflows", {}, transcript)
            await call_tool(session, "get_agent_scenarios", {"scenario_id": scenario_id}, transcript)
            start = await call_tool(
                session,
                "start_session",
                {"timeout_s": vivado_timeout_s, "runtime_dir": str(scenario_dir / "runtime")},
                transcript,
            )
            if not _structured_ok(start):
                return _live_project_failure(scenario_id, label, scenario_dir, transcript, tools=len(tools.tools), summary="start_session failed.")
            create = await call_tool(session, "create_project", _create_project_args(project_inputs, project_dir, part, project_name=project_name), transcript)
            if not _structured_ok(create):
                return _live_project_failure(scenario_id, label, scenario_dir, transcript, tools=len(tools.tools), summary="create_project failed.")
            repair_args = _repair_project_args(project_inputs, project_path)
            await call_tool(session, "repair_project_setup", repair_args | {"dry_run": True, "timeout_s": 120}, transcript)
            repair = await call_tool(session, "repair_project_setup", repair_args | {"dry_run": False, "timeout_s": 180}, transcript)
            if not _structured_ok(repair):
                return _live_project_failure(scenario_id, label, scenario_dir, transcript, tools=len(tools.tools), summary="repair_project_setup failed.")
            await call_tool(session, "list_fileset_files", {"fileset": "sources_1", "timeout_s": 60}, transcript)
            await call_tool(session, "list_fileset_files", {"fileset": "sim_1", "timeout_s": 60}, transcript)
            await call_tool(session, "get_compile_order", {"fileset": "sources_1", "timeout_s": 120}, transcript)
            await call_tool(session, "get_compile_order", {"fileset": "sim_1", "timeout_s": 120}, transcript)
            await call_tool(session, "check_syntax", {"fileset": "sources_1", "timeout_s": 180}, transcript)
            sim = await run_behavioral_simulation_with_transient_retry(
                session,
                {"simset": "sim_1", "run_time": sim_run_time, "export_vcd": False, "max_vcd_mb": 64, "timeout_s": 300},
                transcript,
                recovery={"runtime_dir": str(scenario_dir / "runtime"), "project_path": str(project_path), "start_timeout_s": vivado_timeout_s},
            )
            if not _simulation_passed(sim):
                return _live_project_failure(scenario_id, label, scenario_dir, transcript, tools=len(tools.tools), summary="run_behavioral_simulation did not pass.")
            synth = await call_tool(session, "run_synthesis", {"run_name": "synth_1", "timeout_s": 60}, transcript)
            if not _structured_ok(synth):
                return _live_project_failure(scenario_id, label, scenario_dir, transcript, tools=len(tools.tools), summary="run_synthesis failed to launch.")
            synth_progress = await poll_run(session, "synth_1", False, poll_timeout_s, poll_interval_s, transcript)
            if not _run_complete(synth_progress):
                return _live_project_failure(scenario_id, label, scenario_dir, transcript, tools=len(tools.tools), summary="synth_1 did not reach complete.")
            impl = await call_tool(session, "run_implementation", {"run_name": "impl_1", "timeout_s": 60}, transcript)
            if not _structured_ok(impl):
                return _live_project_failure(scenario_id, label, scenario_dir, transcript, tools=len(tools.tools), summary="run_implementation failed to launch.")
            impl_progress = await poll_run(session, "impl_1", False, poll_timeout_s, poll_interval_s, transcript)
            if not _run_complete(impl_progress):
                return _live_project_failure(scenario_id, label, scenario_dir, transcript, tools=len(tools.tools), summary="impl_1 did not reach complete before bitstream.")
            bit = await call_tool(session, "generate_bitstream", {"run_name": "impl_1", "timeout_s": 60}, transcript)
            if not _structured_ok(bit):
                return _live_project_failure(scenario_id, label, scenario_dir, transcript, tools=len(tools.tools), summary="generate_bitstream failed to launch.")
            bit_progress = await poll_run(session, "impl_1", True, poll_timeout_s, poll_interval_s, transcript)
            if not _run_complete(bit_progress):
                return _live_project_failure(scenario_id, label, scenario_dir, transcript, tools=len(tools.tools), summary="impl_1 bitstream did not complete.")
            write_checkpoint("bitstream_complete", "Bitstream completed; handoff collection has not finished yet.")
            artifacts = await call_tool(session, "collect_build_artifacts", {"run_name": "impl_1", "timeout_s": 120}, transcript)
            write_checkpoint("collect_build_artifacts", "Build artifact collection completed or returned a structured result.")
            if not _tool_status_ok(artifacts):
                handoff_blocker = "collect_build_artifacts failed or returned BLOCK."
            if not handoff_blocker:
                reports = await call_tool(session, "collect_report_bundle", {"run_name": "impl_1", "timeout_s": 240}, transcript)
                write_checkpoint("collect_report_bundle", "Report bundle collection completed or returned a structured result.")
                if not _tool_status_ok(reports):
                    handoff_blocker = "collect_report_bundle failed or returned BLOCK."
            reports_data = reports.get("data") if isinstance(reports.get("data"), dict) else {}
            report_manifest_path = str(reports_data.get("manifest_path", ""))
            handoff_timeout_s = 600
            signoff_args = {"run_name": "impl_1", "project_dir": str(project_dir), "timeout_s": handoff_timeout_s}
            audit_args = {"run_name": "impl_1", "project_dir": str(project_dir), "timeout_s": handoff_timeout_s}
            if report_manifest_path:
                signoff_args["report_manifest_path"] = report_manifest_path
                audit_args["report_manifest_path"] = report_manifest_path
            if not handoff_blocker:
                signoff = await call_tool(session, "run_pre_hw_signoff", signoff_args, transcript)
                write_checkpoint("run_pre_hw_signoff", "Pre-hardware signoff completed or returned a structured result.")
                if not _tool_status_ok(signoff):
                    handoff_blocker = "run_pre_hw_signoff failed or returned BLOCK."
            if not handoff_blocker:
                audit = await call_tool(session, "run_project_audit", audit_args, transcript)
                write_checkpoint("run_project_audit", "Project audit completed or returned a structured result.")
                if not _tool_status_ok(audit):
                    handoff_blocker = "run_project_audit failed or returned BLOCK."
            if not handoff_blocker:
                bundle = await call_tool(
                    session,
                    "collect_diagnostic_bundle",
                    {
                        "run_name": "impl_1",
                        "timestamp": f"{scenario_id.lower()}_runner_{_timestamp()}",
                        "timeout_s": handoff_timeout_s,
                    },
                    transcript,
                )
                write_checkpoint("collect_diagnostic_bundle", "Diagnostic bundle collection completed or returned a structured result.")
                if not _tool_status_ok(bundle):
                    handoff_blocker = "collect_diagnostic_bundle failed or returned BLOCK."
            bundle_data = bundle.get("data") if isinstance(bundle.get("data"), dict) else {}
            manifest_path = str(bundle_data.get("manifest_path", "") or bundle_data.get("partial_manifest_path", ""))
            if not handoff_blocker and not manifest_path:
                handoff_blocker = "collect_diagnostic_bundle did not return manifest_path or partial_manifest_path."
            if manifest_path and (not handoff_blocker or handoff_blocker.startswith("collect_diagnostic_bundle")):
                final_validation = await call_tool(session, "validate_diagnostic_bundle", {"manifest_path": manifest_path}, transcript)
                write_checkpoint("validate_diagnostic_bundle", "Diagnostic bundle validation completed or returned a structured result.")
                if not handoff_blocker and not _tool_status_ok(final_validation):
                    handoff_blocker = "validate_diagnostic_bundle failed or returned BLOCK."
            stop_summary = await call_tool(session, "stop_session", {}, transcript)
        finally:
            if not transcript or transcript[-1].get("tool") != "stop_session":
                try:
                    stop_summary = await call_tool(session, "stop_session", {}, transcript)
                except Exception as exc:  # noqa: BLE001 - preserve original scenario failure.
                    stop_summary = {"ok": False, "error_code": exc.__class__.__name__, "message": str(exc)}

    partial_handoff = _live_partial_handoff(
        project_path=project_path,
        project_dir=project_dir,
        artifacts=artifacts,
        reports=reports,
        signoff=signoff,
        audit=audit,
        bundle=bundle,
        final_validation=final_validation,
        bitstream_completed=True,
        handoff_blocker=handoff_blocker,
    )
    status = classify_live_project_status(transcript, final_validation, scenario_id=scenario_id, handoff_blocker=handoff_blocker)
    return {
        "id": scenario_id,
        "label": label,
        "status": status["status"],
        "executed": True,
        "evidence_dir": str(scenario_dir),
        "project_path": str(project_path),
        "manifest_path": manifest_path,
        "tool_count": len(tools.tools),
        "tool_definition_count": len(tools.tools),
        "tool_call_count": len(transcript),
        **scenario_evidence_fields(scenario_id),
        "tool_count_note": "tool_definition_count is MCP server tool definitions; tool_call_count is runner-visible MCP calls.",
        "tool_calls": transcript,
        "stop_session": summarize_structured(stop_summary),
        "final_validation": summarize_structured(final_validation),
        "checkpoint_path": str(checkpoint_path),
        "progress_path": str(progress_path),
        "partial_handoff": partial_handoff,
        "handoff_blocker": handoff_blocker,
        "hardware_validation_status": status["hardware_validation_status"],
        "handoff_status": status["handoff_status"],
        "handoff_ready": status["handoff_ready"],
        "handoff_reviewable": status["handoff_reviewable"],
        "review_required_reasons": status["review_required_reasons"],
        "review_guidance": status["review_guidance"],
        "recommended_entrypoint": status["recommended_entrypoint"],
        "next_actions": partial_handoff["next_actions"],
        "next_steps": status["next_steps"],
        "summary": status["summary"],
        "recommendations": status["recommendations"],
        "handoff_tools_ok": all(_tool_status_ok(item) for item in (artifacts, reports, signoff, audit, bundle) if item),
    }


async def poll_run(
    session: ClientSession,
    run_name: str,
    expect_bitstream: bool,
    timeout_s: int,
    interval_s: int,
    transcript: list[dict[str, Any]],
) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_s, 1)
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await call_tool(
            session,
            "get_run_progress",
            {"run_name": run_name, "expect_bitstream": expect_bitstream, "timeout_s": 60},
            transcript,
        )
        if not _structured_ok(last):
            return last
        data = last.get("data") if isinstance(last.get("data"), dict) else {}
        if data.get("terminal") is True:
            return last
        await asyncio.sleep(max(interval_s, 1))
    return {
        "ok": False,
        "tool": "get_run_progress",
        "error_code": "SCENARIO_POLL_TIMEOUT",
        "message": f"{run_name} did not reach terminal state within {timeout_s}s.",
        "data": {"run_name": run_name, "expect_bitstream": expect_bitstream, "last_progress": last.get("data", {})},
    }


def classify_s05_validation(validate: dict[str, Any]) -> dict[str, Any]:
    data = validate.get("data") if isinstance(validate.get("data"), dict) else {}
    health = data.get("health") if isinstance(data.get("health"), dict) else {}
    hardware = data.get("hardware_validation") if isinstance(data.get("hardware_validation"), dict) else {}
    handoff_ready = bool(data.get("handoff_ready"))
    handoff_reviewable = bool(data.get("handoff_reviewable"))
    hardware_status = str(hardware.get("status", ""))
    bundle_mode = str(data.get("bundle_mode") or health.get("bundle_mode") or "")
    portable = data.get("portable", health.get("portable"))
    handoff = _handoff_summary(validate)
    if not validate.get("ok"):
        result = {
            "status": "BLOCK",
            "hardware_validation_status": hardware_status,
            "handoff_ready": handoff_ready,
            "handoff_reviewable": handoff_reviewable,
            "review_guidance": health.get("review_guidance", {}),
            "summary": "S05 validate_diagnostic_bundle failed.",
            "recommendations": ["Fix validate_diagnostic_bundle failure before treating WARN handoff as reviewable."],
        }
        result.update(handoff)
        return result
    if bundle_mode not in {"reference", "legacy_reference"} or portable is not False or handoff_ready:
        result = {
            "status": "BLOCK",
            "hardware_validation_status": hardware_status,
            "handoff_ready": handoff_ready,
            "handoff_reviewable": handoff_reviewable,
            "review_guidance": health.get("review_guidance", {}),
            "summary": "S05 violated the current reference-bundle contract.",
            "recommendations": [
                "Require bundle_mode=reference, portable=false, and handoff_ready=false until portable closure is implemented."
            ],
        }
        result.update(handoff)
        return result
    if data.get("status") == "WARN" and handoff_ready is False and handoff_reviewable and hardware_status == "NOT_VALIDATED":
        result = {
            "status": "PASS",
            "hardware_validation_status": hardware_status,
            "handoff_ready": handoff_ready,
            "handoff_reviewable": handoff_reviewable,
            "review_guidance": health.get("review_guidance", {}),
            "summary": "S05 WARN handoff is reviewable and preserves NOT_VALIDATED hardware boundary.",
            "recommendations": ["Review WARN findings before archiving this as a no-board handoff."],
        }
        result.update(handoff)
        return result
    result = {
        "status": "WATCH",
        "hardware_validation_status": hardware_status,
        "handoff_ready": handoff_ready,
        "handoff_reviewable": handoff_reviewable,
        "review_guidance": health.get("review_guidance", {}),
        "summary": "S05 returned a structured result, but WARN handoff semantics need review.",
        "recommendations": ["Inspect review_guidance, handoff_ready, handoff_reviewable, and hardware_validation fields."],
    }
    result.update(handoff)
    return result


def classify_live_s02_status(transcript: list[dict[str, Any]], validation: dict[str, Any]) -> dict[str, Any]:
    return classify_live_project_status(transcript, validation, scenario_id="S02")


def unresolved_live_project_failures(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return blocking tool failures that were not superseded by a later success."""

    unresolved_by_tool: dict[str, list[dict[str, Any]]] = {}
    for item in transcript:
        tool = str(item.get("tool") or "")
        if tool not in LIVE_PROJECT_BLOCKING_TOOLS:
            continue
        if item.get("ok") is False:
            unresolved_by_tool.setdefault(tool, []).append(item)
        elif item.get("ok") is True:
            unresolved_by_tool.pop(tool, None)
    return sorted(
        (item for failures in unresolved_by_tool.values() for item in failures),
        key=lambda item: int(item.get("seq") or 0),
    )


def classify_live_project_status(transcript: list[dict[str, Any]], validation: dict[str, Any], *, scenario_id: str, handoff_blocker: str = "") -> dict[str, Any]:
    failed = unresolved_live_project_failures(transcript)
    data = validation.get("data") if isinstance(validation.get("data"), dict) else {}
    health = data.get("health") if isinstance(data.get("health"), dict) else {}
    hardware = data.get("hardware_validation") if isinstance(data.get("hardware_validation"), dict) else {}
    hardware_status = str(hardware.get("status", ""))
    handoff_ready = bool(data.get("handoff_ready"))
    handoff_reviewable = bool(data.get("handoff_reviewable"))
    bundle_mode = str(data.get("bundle_mode") or health.get("bundle_mode") or "")
    portable = data.get("portable", health.get("portable"))
    handoff = _handoff_summary(validation)
    if failed:
        result = {
            "status": "BLOCK",
            "hardware_validation_status": hardware_status or "NOT_VALIDATED",
            "handoff_ready": handoff_ready,
            "handoff_reviewable": handoff_reviewable,
            "summary": f"{scenario_id} blocked at {failed[-1].get('tool')} with {failed[-1].get('error_code')}.",
            "recommendations": ["Use tool_calls and get_workflow_trace_status to resume from the last failed MCP action."],
        }
        result.update(handoff)
        return result
    if handoff_blocker:
        result = {
            "status": "BLOCK",
            "hardware_validation_status": hardware_status or "NOT_VALIDATED",
            "handoff_ready": handoff_ready,
            "handoff_reviewable": handoff_reviewable,
            "summary": f"{scenario_id} blocked during handoff: {handoff_blocker}",
            "recommendations": ["Use partial_handoff.failed_tools and partial_handoff.next_actions to resume from the blocked handoff step."],
        }
        result.update(handoff)
        return result
    if bundle_mode not in {"reference", "legacy_reference"} or portable is not False or handoff_ready:
        result = {
            "status": "BLOCK",
            "hardware_validation_status": hardware_status or "NOT_VALIDATED",
            "handoff_ready": handoff_ready,
            "handoff_reviewable": handoff_reviewable,
            "summary": f"{scenario_id} violated the current reference-bundle contract.",
            "recommendations": [
                "Require bundle_mode=reference, portable=false, and handoff_ready=false until portable closure is implemented."
            ],
        }
        result.update(handoff)
        return result
    if validation.get("ok") and data.get("status") == "WARN" and handoff_reviewable and hardware_status == "NOT_VALIDATED":
        result = {
            "status": "PASS",
            "hardware_validation_status": hardware_status,
            "handoff_ready": handoff_ready,
            "handoff_reviewable": handoff_reviewable,
            "summary": f"{scenario_id} completed live Project Mode flow with reviewable WARN handoff.",
            "recommendations": ["Review WARN findings before archiving this as a no-board handoff."],
        }
        result.update(handoff)
        return result
    result = {
        "status": "WATCH",
        "hardware_validation_status": hardware_status,
        "handoff_ready": handoff_ready,
        "handoff_reviewable": handoff_reviewable,
        "summary": f"{scenario_id} finished without a tool failure, but final handoff state needs review.",
        "recommendations": ["Inspect final_validation.health and review_guidance."],
    }
    result.update(handoff)
    return result


def _handoff_summary(validate: dict[str, Any]) -> dict[str, Any]:
    data = validate.get("data") if isinstance(validate.get("data"), dict) else {}
    health = data.get("health") if isinstance(data.get("health"), dict) else {}
    resume = data.get("resume_context") if isinstance(data.get("resume_context"), dict) else {}
    return {
        "handoff_status": str(data.get("status") or health.get("status") or ""),
        "review_required_reasons": list(health.get("review_required_reasons", []) or resume.get("review_required_reasons", [])),
        "review_guidance": dict(health.get("review_guidance", {}) or resume.get("review_guidance", {})),
        "recommended_entrypoint": str(resume.get("recommended_entrypoint", "")),
        "next_steps": list(data.get("next_steps", [])),
    }


def _post_stop_resume_actions(
    *,
    project_path: str,
    manifest_path: str,
    run_name: str,
    failed_tool: str = "",
    project_dir: str = "",
    report_manifest_path: str = "",
) -> list[dict[str, Any]]:
    actions = [
        {
            "tool": "start_session",
            "reason": "The live scenario stops its Vivado session before writing final runner output; start a fresh managed session before running project-bound follow-up tools.",
            "required_args": [],
            "arg_sources": {},
            "preconditions": ["Follow-up audit, report, or Tcl inspection is needed after scenario completion."],
            "stop_condition": "start_session returns ok=true and session_status.connected=true.",
            "optional": False,
        },
        {
            "tool": "open_project",
            "reason": "Reopen the scenario project before running project-bound analysis after stop_session.",
            "required_args": ["project_path"],
            "arg_sources": {"project_path": project_path},
            "preconditions": ["start_session succeeded."],
            "stop_condition": "open_project returns ok=true for the scenario .xpr.",
            "optional": False,
        },
    ]
    if failed_tool:
        retry_action = _failed_handoff_retry_action(
            failed_tool=failed_tool,
            run_name=run_name,
            project_dir=project_dir,
            report_manifest_path=report_manifest_path,
            manifest_path=manifest_path,
        )
        if retry_action:
            actions.append(retry_action)
    if manifest_path:
        actions.append(
            {
                "tool": "validate_diagnostic_bundle",
                "reason": "Re-check the saved diagnostic bundle before handoff review or archive.",
                "required_args": ["manifest_path"],
                "arg_sources": {"manifest_path": manifest_path},
                "preconditions": ["Diagnostic manifest path exists."],
                "stop_condition": "validate_diagnostic_bundle returns READY or reviewable WARN.",
                "optional": True,
            }
        )
    actions.append(
        {
            "tool": "run_project_audit",
            "reason": "Refresh audit only after the project is reopened; do not run project-bound audit against a stopped session.",
            "required_args": ["run_name"],
            "arg_sources": {"run_name": run_name},
            "preconditions": ["start_session and open_project succeeded."],
            "stop_condition": "run_project_audit returns READY, WARN, or BLOCK with active findings.",
            "optional": True,
        }
    )
    return actions


def _failed_handoff_retry_action(
    *,
    failed_tool: str,
    run_name: str,
    project_dir: str,
    report_manifest_path: str,
    manifest_path: str,
) -> dict[str, Any]:
    if failed_tool == "collect_report_bundle":
        return {
            "tool": "collect_report_bundle",
            "reason": "Retry report bundle collection before signoff/audit because report evidence was the first blocked handoff step.",
            "required_args": ["run_name"],
            "arg_sources": {"run_name": run_name},
            "preconditions": ["start_session and open_project succeeded."],
            "stop_condition": "collect_report_bundle returns a project-local report_manifest_path.",
            "optional": False,
        }
    if failed_tool == "run_pre_hw_signoff":
        arg_sources = {"run_name": run_name}
        required_args = ["run_name"]
        if project_dir:
            arg_sources["project_dir"] = project_dir
        if report_manifest_path:
            required_args.append("report_manifest_path")
            arg_sources["report_manifest_path"] = report_manifest_path
        return {
            "tool": "run_pre_hw_signoff",
            "reason": "Retry the blocked pre-hardware signoff step using the existing report manifest before running audit.",
            "required_args": required_args,
            "arg_sources": arg_sources,
            "preconditions": ["start_session and open_project succeeded.", "Report bundle evidence exists or collect_report_bundle has been rerun."],
            "stop_condition": "run_pre_hw_signoff returns READY, WARN, or a structured BLOCK finding.",
            "optional": False,
        }
    if failed_tool == "run_project_audit":
        arg_sources = {"run_name": run_name}
        required_args = ["run_name"]
        if project_dir:
            arg_sources["project_dir"] = project_dir
        if report_manifest_path:
            required_args.append("report_manifest_path")
            arg_sources["report_manifest_path"] = report_manifest_path
        return {
            "tool": "run_project_audit",
            "reason": "Retry the blocked project audit step after reopening the project.",
            "required_args": required_args,
            "arg_sources": arg_sources,
            "preconditions": ["start_session and open_project succeeded.", "Pre-hardware signoff findings have been reviewed."],
            "stop_condition": "run_project_audit returns READY, WARN, or BLOCK with active findings.",
            "optional": False,
        }
    if failed_tool == "collect_diagnostic_bundle":
        return {
            "tool": "collect_diagnostic_bundle",
            "reason": "Retry diagnostic bundle collection after reopening the project and reviewing any audit timeout context.",
            "required_args": ["run_name"],
            "arg_sources": {"run_name": run_name},
            "preconditions": ["start_session and open_project succeeded.", "Project audit is available or can be rerun."],
            "stop_condition": "collect_diagnostic_bundle returns manifest_path or partial_manifest_path.",
            "optional": False,
        }
    if failed_tool == "validate_diagnostic_bundle" and manifest_path:
        return {
            "tool": "validate_diagnostic_bundle",
            "reason": "Retry diagnostic bundle validation for the saved manifest.",
            "required_args": ["manifest_path"],
            "arg_sources": {"manifest_path": manifest_path},
            "preconditions": ["Diagnostic manifest path exists."],
            "stop_condition": "validate_diagnostic_bundle returns READY, reviewable WARN, or BLOCK with repair actions.",
            "optional": False,
        }
    return {}


def _s03_counter_source(*, fixed: bool) -> str:
    increment = "4'd1" if fixed else "4'd2"
    comment = "Correct increment for S03 live-lite rerun." if fixed else "Intentional S03 live-lite bug: increments too fast."
    return f"""module counter_repair_top (
    input  logic clk,
    input  logic rst_n,
    output logic [3:0] count
);
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            count <= '0;
        end else begin
            // {comment}
            count <= count + {increment};
        end
    end
endmodule
"""


def write_s03_live_project_sources(root: Path) -> dict[str, Any]:
    src_dir = root / "src"
    sim_dir = root / "sim"
    xdc_dir = root / "xdc"
    for directory in (src_dir, sim_dir, xdc_dir):
        directory.mkdir(parents=True, exist_ok=True)

    top = src_dir / "counter_repair_top.sv"
    fixed_text = _s03_counter_source(fixed=True)
    top.write_text(_s03_counter_source(fixed=False), encoding="utf-8")

    tb = sim_dir / "tb_counter_repair_top.sv"
    tb.write_text(
        """`timescale 1ns/1ps

module tb_counter_repair_top;
    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic [3:0] count;
    logic done = 1'b0;
    int expected;

    counter_repair_top dut (
        .clk(clk),
        .rst_n(rst_n),
        .count(count)
    );

    always #5 clk = ~clk;

    initial begin
        repeat (3) @(posedge clk);
        @(negedge clk);
        rst_n = 1'b1;
        expected = 0;
        repeat (8) begin
            @(posedge clk);
            #1;
            expected = (expected + 1) & 4'hf;
            if (count !== expected[3:0]) begin
                done = 1'b1;
                $display("TB_FAIL expected=%0d got=%0d", expected, count);
                $finish;
            end
        end
        done = 1'b1;
        $display("TB_PASS count=%0d", count);
        $finish;
    end

    initial begin
        #2000;
        if (!done) begin
            $display("TB_FAIL timeout");
        end
        $finish;
    end
endmodule
""",
        encoding="utf-8",
    )

    xdc = xdc_dir / "counter_repair.xdc"
    xdc.write_text(
        """set_property PACKAGE_PIN W5 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]
create_clock -period 10.000 -name sys_clk [get_ports clk]

set_property PACKAGE_PIN U18 [get_ports rst_n]
set_property IOSTANDARD LVCMOS33 [get_ports rst_n]

set_property PACKAGE_PIN U16 [get_ports {count[0]}]
set_property PACKAGE_PIN E19 [get_ports {count[1]}]
set_property PACKAGE_PIN U19 [get_ports {count[2]}]
set_property PACKAGE_PIN V19 [get_ports {count[3]}]
set_property IOSTANDARD LVCMOS33 [get_ports {count[*]}]

set_property CFGBVS VCCO [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
""",
        encoding="utf-8",
    )

    return {
        "rtl_files": [str(top)],
        "xdc_files": [str(xdc)],
        "sim_files": [str(tb)],
        "top": "counter_repair_top",
        "testbench_top": "tb_counter_repair_top",
        "target_language": "SystemVerilog",
        "faulty_rtl_file": str(top),
        "fixed_rtl_text": fixed_text,
    }


def write_s01_project_sources(root: Path) -> dict[str, Any]:
    src_dir = root / "src"
    sim_dir = root / "sim"
    xdc_dir = root / "xdc"
    src_dir.mkdir(parents=True, exist_ok=True)
    sim_dir.mkdir(parents=True, exist_ok=True)
    xdc_dir.mkdir(parents=True, exist_ok=True)

    top = src_dir / "counter_top.v"
    top.write_text(
        """module counter_top (
    input wire clk,
    input wire rst_n,
    output wire [3:0] led
);
    reg [7:0] counter;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            counter <= 8'd0;
        end else begin
            counter <= counter + 8'd1;
        end
    end

    assign led = counter[7:4];
endmodule
""",
        encoding="utf-8",
    )

    tb = sim_dir / "tb_counter_top.v"
    tb.write_text(
        """`timescale 1ns/1ps
module tb_counter_top;
    reg clk;
    reg rst_n;
    wire [3:0] led;
    reg [3:0] last_led;
    reg done;
    integer transitions;

    counter_top dut (
        .clk(clk),
        .rst_n(rst_n),
        .led(led)
    );

    initial begin
        clk = 1'b0;
        forever #5 clk = ~clk;
    end

    initial begin
        transitions = 0;
        done = 1'b0;
        rst_n = 1'b0;
        repeat (4) @(posedge clk);
        rst_n = 1'b1;
        last_led = led;
        repeat (512) begin
            @(posedge clk);
            if (led !== last_led) begin
                transitions = transitions + 1;
                last_led = led;
            end
        end
        if (transitions < 4) begin
            done = 1'b1;
            $display("TB_FAIL transitions=%0d", transitions);
            $finish;
        end
        done = 1'b1;
        $display("TB_PASS transitions=%0d", transitions);
        $finish;
    end

    initial begin
        #10000;
        if (!done) begin
            $display("TB_FAIL timeout");
        end
        $finish;
    end
endmodule
""",
        encoding="utf-8",
    )

    xdc = xdc_dir / "counter.xdc"
    xdc.write_text(
        """set_property PACKAGE_PIN W5 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]
create_clock -period 10.000 -name sys_clk [get_ports clk]

set_property PACKAGE_PIN U18 [get_ports rst_n]
set_property IOSTANDARD LVCMOS33 [get_ports rst_n]

set_property PACKAGE_PIN U16 [get_ports {led[0]}]
set_property PACKAGE_PIN E19 [get_ports {led[1]}]
set_property PACKAGE_PIN U19 [get_ports {led[2]}]
set_property PACKAGE_PIN V19 [get_ports {led[3]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[*]}]

set_property CFGBVS VCCO [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
""",
        encoding="utf-8",
    )

    return {
        "rtl_files": [str(top)],
        "xdc_files": [str(xdc)],
        "sim_files": [str(tb)],
        "top": "counter_top",
        "testbench_top": "tb_counter_top",
        "target_language": "Verilog",
    }


def write_s02_project_sources(root: Path) -> dict[str, Any]:
    src_dir = root / "src"
    sim_dir = root / "sim"
    xdc_dir = root / "xdc"
    for directory in (src_dir, sim_dir, xdc_dir):
        directory.mkdir(parents=True, exist_ok=True)

    clock_enable = src_dir / "clock_enable.sv"
    clock_enable.write_text(
        """module clock_enable #(
    parameter int DIVISOR = 16
) (
    input  logic clk,
    input  logic rst_n,
    output logic tick
);
    localparam int COUNT_WIDTH = (DIVISOR <= 2) ? 1 : $clog2(DIVISOR);
    logic [COUNT_WIDTH-1:0] count;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            count <= '0;
            tick <= 1'b0;
        end else if (count == DIVISOR - 1) begin
            count <= '0;
            tick <= 1'b1;
        end else begin
            count <= count + 1'b1;
            tick <= 1'b0;
        end
    end
endmodule
""",
        encoding="utf-8",
    )

    pwm_core = src_dir / "pwm_core.sv"
    pwm_core.write_text(
        """module pwm_core #(
    parameter int PWM_BITS = 8
) (
    input  logic clk,
    input  logic rst_n,
    input  logic [PWM_BITS-1:0] duty,
    output logic pwm
);
    logic [PWM_BITS-1:0] counter;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            counter <= '0;
        end else begin
            counter <= counter + 1'b1;
        end
    end

    always_comb begin
        pwm = counter < duty;
    end
endmodule
""",
        encoding="utf-8",
    )

    top = src_dir / "breath_led_top.sv"
    top.write_text(
        """module breath_led_top #(
    parameter int PWM_BITS = 8,
    parameter int TICK_DIVISOR = 128
) (
    input  logic clk,
    input  logic rst_n,
    output logic [3:0] led
);
    logic tick;
    logic direction_up;
    logic [PWM_BITS-1:0] duty;
    logic pwm;

    clock_enable #(.DIVISOR(TICK_DIVISOR)) u_tick (
        .clk(clk),
        .rst_n(rst_n),
        .tick(tick)
    );

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            duty <= '0;
            direction_up <= 1'b1;
        end else if (tick) begin
            if (direction_up) begin
                duty <= duty + 1'b1;
                if (duty == {PWM_BITS{1'b1}} - 1'b1) begin
                    direction_up <= 1'b0;
                end
            end else begin
                duty <= duty - 1'b1;
                if (duty == {{(PWM_BITS-1){1'b0}}, 1'b1}) begin
                    direction_up <= 1'b1;
                end
            end
        end
    end

    pwm_core #(.PWM_BITS(PWM_BITS)) u_pwm (
        .clk(clk),
        .rst_n(rst_n),
        .duty(duty),
        .pwm(pwm)
    );

    assign led = {4{pwm}};
endmodule
""",
        encoding="utf-8",
    )

    tb = sim_dir / "tb_breath_led_top.sv"
    tb.write_text(
        """`timescale 1ns/1ps

module tb_breath_led_top;
    logic clk = 1'b0;
    logic rst_n = 1'b0;
    wire [3:0] led;
    logic done = 1'b0;

    breath_led_top #(
        .PWM_BITS(4),
        .TICK_DIVISOR(4)
    ) dut (
        .clk(clk),
        .rst_n(rst_n),
        .led(led)
    );

    always #5 clk = ~clk;

    initial begin
        int transitions;
        logic last_led;
        transitions = 0;
        repeat (5) @(posedge clk);
        rst_n = 1'b1;
        last_led = led[0];
        repeat (512) begin
            @(posedge clk);
            if (led[0] !== last_led) begin
                transitions++;
                last_led = led[0];
            end
        end
        if (transitions < 2) begin
            done = 1'b1;
            $display("TB_FAIL transitions=%0d", transitions);
            $finish;
        end
        done = 1'b1;
        $display("TB_PASS transitions=%0d", transitions);
        $finish;
    end

    initial begin
        #20000;
        if (!done) begin
            $display("TB_FAIL timeout");
        end
        $finish;
    end
endmodule
""",
        encoding="utf-8",
    )

    xdc = xdc_dir / "breath_led.xdc"
    xdc.write_text(
        """set_property PACKAGE_PIN W5 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]
create_clock -period 10.000 -name sys_clk [get_ports clk]

set_property PACKAGE_PIN U18 [get_ports rst_n]
set_property IOSTANDARD LVCMOS33 [get_ports rst_n]

set_property PACKAGE_PIN U16 [get_ports {led[0]}]
set_property PACKAGE_PIN E19 [get_ports {led[1]}]
set_property PACKAGE_PIN U19 [get_ports {led[2]}]
set_property PACKAGE_PIN V19 [get_ports {led[3]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[*]}]

set_property CFGBVS VCCO [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
""",
        encoding="utf-8",
    )

    return {
        "rtl_files": [str(clock_enable), str(pwm_core), str(top)],
        "xdc_files": [str(xdc)],
        "sim_files": [str(tb)],
        "top": "breath_led_top",
        "testbench_top": "tb_breath_led_top",
        "target_language": "SystemVerilog",
    }


def write_reviewable_warn_bundle(bundle_dir: Path) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    audit = {
        "status": "WARN",
        "effective_status": "WARN",
        "active_findings": [
            {
                "severity": "WARN",
                "code": "CONFIG_VOLTAGE_REVIEW",
                "source_tool": "run_pre_hw_signoff",
                "message": "CFGBVS and CONFIG_VOLTAGE are board-handoff review items in no-board validation.",
            }
        ],
        "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
        "evidence_freshness": _freshness("agent_scenario_runner_s05"),
    }
    required = [
        ("audit", "audit_result.json", json.dumps(audit, ensure_ascii=False, indent=2)),
        ("environment", "vivado_environment.json", "{}"),
        ("project_state", "project_state.json", "{}"),
        ("filesets", "filesets.json", "{}"),
        ("run_configurations", "run_configurations.json", "{}"),
        ("waivers", "waivers.json", "{}"),
        ("session_status", "session_status.json", "{}"),
        ("replay_script", "replay_project.tcl", "create_project {s05} {.} -part {xc7a35tcpg236-1}\n"),
        ("logs", "logs_tail.txt", "WARNING: reviewable no-board handoff warning\n"),
    ]
    files: list[dict[str, Any]] = []
    for category, name, content in required:
        path = bundle_dir / name
        path.write_text(content, encoding="utf-8")
        files.append({"path": str(path), "category": category, "size": path.stat().st_size, "sha256": _sha256(path)})
    files.append(_write_synthetic_trace(bundle_dir, [("run_project_audit", {"status": "WARN"})]))
    manifest = {
        "schema_version": 2,
        "bundle_mode": "reference",
        "portable": False,
        "portability": {
            "status": "PROJECT_LOCAL_REFERENCE_ONLY",
            "reason": "Referenced payloads remain in project-local vmcp_* directories.",
        },
        "project_dir": str(bundle_dir.parent),
        "bundle_dir": str(bundle_dir),
        "manifest_path": str(bundle_dir / "diagnostic_manifest.json"),
        "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
        "summary": {
            "audit_status": "WARN",
            "missing_required_categories": [],
            "complete": True,
            "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
            "validation_scope": "pre_hardware_software",
            "ready_meaning": "READY means no-board Vivado software handoff evidence is ready, not real FPGA board validation.",
            "evidence_freshness": _freshness("agent_scenario_runner_s05"),
        },
        "files": files,
    }
    manifest["integrity_model"] = {"status": "SELF_CONSISTENCY_VERIFIED_BY_FILE_HASHES", "scope": "bundle_files"}
    manifest["authenticity"] = attest_diagnostic_manifest(manifest)
    manifest_path = bundle_dir / "diagnostic_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def write_existing_project_handoff_bundle(bundle_dir: Path) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    project_dir = bundle_dir.parent / "existing_project"
    project_dir.mkdir(parents=True, exist_ok=True)
    project_path = project_dir / "existing_demo.xpr"
    project_path.write_text("# fake existing Project Mode handoff fixture\n", encoding="utf-8")
    audit = {
        "status": "READY",
        "effective_status": "READY",
        "active_findings": [],
        "project_path": str(project_path),
        "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
        "evidence_freshness": _freshness("agent_scenario_runner_s07"),
    }
    required = [
        ("audit", "audit_result.json", json.dumps(audit, ensure_ascii=False, indent=2)),
        ("environment", "vivado_environment.json", "{}"),
        ("project_state", "project_state.json", json.dumps({"project": {"name": "existing_demo", "directory": str(project_dir), "path": str(project_path)}}, ensure_ascii=False, indent=2)),
        ("filesets", "filesets.json", "{}"),
        ("run_configurations", "run_configurations.json", "{}"),
        ("waivers", "waivers.json", "{}"),
        ("session_status", "session_status.json", json.dumps({"connected": True, "project_path": str(project_path)}, ensure_ascii=False, indent=2)),
        ("replay_script", "replay_project.tcl", f"open_project {{{project_path}}}\nget_project_state\n"),
        ("logs", "logs_tail.txt", "INFO: existing project audit handoff fixture\n"),
    ]
    files: list[dict[str, Any]] = []
    for category, name, content in required:
        path = bundle_dir / name
        path.write_text(content, encoding="utf-8")
        files.append({"path": str(path), "category": category, "size": path.stat().st_size, "sha256": _sha256(path)})
    files.append(
        _write_synthetic_trace(
            bundle_dir,
            [("open_project", {"status": "READY"}), ("run_project_audit", {"status": "READY"})],
        )
    )
    manifest = {
        "schema_version": 2,
        "bundle_mode": "reference",
        "portable": False,
        "portability": {
            "status": "PROJECT_LOCAL_REFERENCE_ONLY",
            "reason": "Referenced payloads remain in project-local vmcp_* directories.",
        },
        "project_dir": str(project_dir),
        "bundle_dir": str(bundle_dir),
        "manifest_path": str(bundle_dir / "diagnostic_manifest.json"),
        "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
        "summary": {
            "audit_status": "READY",
            "missing_required_categories": [],
            "complete": True,
            "primary_files": {
                "workflow_trace": str(bundle_dir / "workflow_trace.jsonl"),
                "replay_script": str(bundle_dir / "replay_project.tcl"),
            },
            "hardware_validation": {"status": "NOT_VALIDATED", "validated": False},
            "validation_scope": "pre_hardware_software",
            "ready_meaning": "READY means no-board Vivado software handoff evidence is ready, not real FPGA board validation.",
            "evidence_freshness": _freshness("agent_scenario_runner_s07"),
        },
        "files": files,
    }
    manifest["integrity_model"] = {"status": "SELF_CONSISTENCY_VERIFIED_BY_FILE_HASHES", "scope": "bundle_files"}
    manifest["authenticity"] = attest_diagnostic_manifest(manifest)
    manifest_path = bundle_dir / "diagnostic_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def _write_synthetic_trace(bundle_dir: Path, records: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    tracer = WorkflowTracer(trace_id="workflow_trace", trace_dir=bundle_dir)
    now = datetime.now(timezone.utc)
    for tool, data in records:
        tracer.record(
            tool=tool,
            args={},
            result={"ok": True, "tool": tool, "data": data},
            started_at=now,
            ended_at=now,
        )
    path = tracer.trace_path
    return {"path": str(path), "category": "workflow_trace", "size": path.stat().st_size, "sha256": _sha256(path)}


@asynccontextmanager
async def mcp_stdio_session(
    *,
    workspace: Path,
    python_exe: str,
    runtime_dir: Path | None = None,
    tool_profile: str | None = None,
) -> AsyncIterator[ClientSession]:
    params = StdioServerParameters(
        command=python_exe,
        args=["-m", "vivado_agent_mcp"],
        env=_stdio_env(workspace, runtime_dir=runtime_dir, tool_profile=tool_profile),
        cwd=str(workspace),
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


async def call_tool(session: ClientSession, tool: str, args: dict[str, Any], transcript: list[dict[str, Any]]) -> dict[str, Any]:
    started = _now()
    t0 = time.monotonic()
    timeout_s = _mcp_call_timeout_s(args)
    try:
        result = await asyncio.wait_for(session.call_tool(tool, args), timeout=timeout_s)
    except asyncio.TimeoutError:
        structured = {
            "ok": False,
            "tool": tool,
            "error_code": "RUNNER_MCP_CALL_TIMEOUT",
            "message": f"{tool} did not return through MCP stdio within {timeout_s}s.",
            "data": {
                "tool": tool,
                "args_summary": _runner_args_summary(args),
                "runner_timeout_s": timeout_s,
                "next_actions": [
                    {
                        "tool": "get_workflow_trace_status",
                        "reason": "Inspect the last completed MCP workflow trace after a runner-side call timeout.",
                        "required_args": [],
                        "arg_sources": {},
                        "preconditions": ["The MCP server process is still reachable, or the trace file can be inspected from disk."],
                        "stop_condition": "The trace identifies the last completed or unresolved tool call.",
                        "optional": False,
                    }
                ],
            },
        }
        transcript.append(
            {
                "seq": len(transcript) + 1,
                "tool": tool,
                "started_at": started,
                "ended_at": _now(),
                "duration_s": round(time.monotonic() - t0, 3),
                "ok": False,
                "error_code": "RUNNER_MCP_CALL_TIMEOUT",
                "message": structured["message"],
                "status": "BLOCK",
                "next_actions": _summarize_next_actions(structured["data"]["next_actions"]),
                "hardware_validation_status": "",
                "data_summary": structured["data"],
            }
        )
        return structured
    structured = result.structuredContent or {
        "ok": not bool(result.isError),
        "tool": tool,
        "message": _content_text(result.content) or "Tool returned without structuredContent.",
        "data": {},
    }
    transcript.append(
        {
            "seq": len(transcript) + 1,
            "tool": tool,
            "started_at": started,
            "ended_at": _now(),
            "duration_s": round(time.monotonic() - t0, 3),
            "ok": bool(structured.get("ok")),
            "error_code": str(structured.get("error_code", "")),
            "message": str(structured.get("message") or structured.get("summary") or "")[:500],
            "status": _status_from_structured(structured),
            "next_actions": _summarize_next_actions(structured.get("next_actions", [])),
            "hardware_validation_status": _hardware_status_from_structured(structured),
            "data_summary": _summarize_data(tool, structured),
        }
    )
    return structured


def _mcp_call_timeout_s(args: dict[str, Any]) -> int:
    try:
        requested = int(args.get("timeout_s", 0) or 0)
    except (TypeError, ValueError):
        requested = 0
    return max(60, requested + 90) if requested else 360


def _runner_args_summary(args: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in args.items():
        if key in {"command", "template"}:
            summary[key] = str(value)[:200]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
        elif isinstance(value, list):
            summary[key] = [str(item)[:200] for item in value[:5]]
        else:
            summary[key] = str(value)[:200]
    return summary


def _summarize_data(tool: str, structured: dict[str, Any]) -> dict[str, Any]:
    data = structured.get("data") if isinstance(structured.get("data"), dict) else {}
    keys = {
        "manifest_path",
        "partial_manifest_path",
        "bundle_dir",
        "status",
        "handoff_ready",
        "handoff_reviewable",
        "simulation_invocation_id",
        "status_source",
        "testbench_vcd_usage",
        "testbench_vcd_detected",
        "export_vcd_requested",
        "mcp_vcd_export_mode",
        "vcd_conflict",
        "vcd_conflict_severity",
        "vcd_total_bytes",
        "report_manifest_path",
    }
    summary = {key: data.get(key) for key in sorted(keys) if key in data}
    if not structured.get("ok"):
        raw_excerpt = str(structured.get("raw_excerpt", ""))
        if raw_excerpt:
            summary["raw_excerpt_tail"] = _tail(raw_excerpt, 2000)
        command = str(data.get("command", ""))
        if command:
            summary["command_excerpt"] = command[:500]
        failed_result = data.get("failed_result") if isinstance(data.get("failed_result"), dict) else {}
        if failed_result:
            summary["failed_tool"] = str(data.get("failed_tool", ""))
            summary["failed_result"] = {
                key: failed_result.get(key)
                for key in ("ok", "error_code", "message")
                if key in failed_result
            }
    if tool == "run_behavioral_simulation":
        diagnosis = data.get("simulation_diagnosis") if isinstance(data.get("simulation_diagnosis"), dict) else {}
        artifacts = data.get("artifacts") if isinstance(data.get("artifacts"), dict) else {}
        summary["simulation_diagnosis"] = {
            key: diagnosis.get(key)
            for key in ("primary_cause", "warnings", "vcd_risk", "vcd_limit_exceeded")
            if key in diagnosis
        }
        summary["artifacts"] = {
            key: artifacts.get(key)
            for key in ("vcd_total_bytes", "largest_vcd_file", "wdb_files", "vcd_files")
            if key in artifacts
        }
        effects_delta = data.get("execution_effects_delta") if isinstance(data.get("execution_effects_delta"), dict) else {}
        if effects_delta:
            summary["execution_effects_delta"] = effects_delta
        uncontrolled_reasons = [str(item) for item in data.get("uncontrolled_reasons", []) if str(item)]
        if uncontrolled_reasons:
            summary["uncontrolled_reasons"] = uncontrolled_reasons[:16]
        preflight = data.get("preflight") if isinstance(data.get("preflight"), dict) else {}
        preflight_errors = [str(item) for item in preflight.get("preflight_errors", []) if str(item)]
        if preflight_errors:
            summary["preflight_errors"] = preflight_errors[:16]
    resume = data.get("resume_context") if isinstance(data.get("resume_context"), dict) else {}
    if resume:
        summary["resume_context"] = {
            key: resume.get(key)
            for key in ("status", "handoff_ready", "recommended_entrypoint", "workflow_trace_ref")
            if key in resume
        }
    return summary


def summarize_structured(structured: dict[str, Any]) -> dict[str, Any]:
    data = structured.get("data") if isinstance(structured.get("data"), dict) else {}
    health = data.get("health") if isinstance(data.get("health"), dict) else {}
    return {
        "ok": bool(structured.get("ok")),
        "tool": structured.get("tool", ""),
        "error_code": structured.get("error_code", ""),
        "message": structured.get("message", structured.get("summary", "")),
        "status": data.get("status", health.get("status", "")),
        "handoff_ready": data.get("handoff_ready", health.get("handoff_ready", None)),
        "handoff_reviewable": data.get("handoff_reviewable", health.get("handoff_reviewable", None)),
        "hardware_validation": data.get("hardware_validation", structured.get("hardware_validation", {})),
        "resume_context": data.get("resume_context", structured.get("resume_context", {})),
    }


def summarize_simulation_structured(structured: dict[str, Any]) -> dict[str, Any]:
    summary = summarize_structured(structured)
    data = structured.get("data") if isinstance(structured.get("data"), dict) else {}
    diagnosis = data.get("simulation_diagnosis") if isinstance(data.get("simulation_diagnosis"), dict) else {}
    summary.update(
        {
            "status_source": data.get("status_source", ""),
            "simulation_status": data.get("status", ""),
            "simulation_diagnosis": {
                "primary_cause": diagnosis.get("primary_cause", ""),
                "causes": diagnosis.get("causes", []),
                "status": diagnosis.get("status", ""),
            },
            "log_span": data.get("log_span", {}),
            "next_actions": _summarize_next_actions(structured.get("next_actions", [])),
        }
    )
    return summary


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _create_project_args(project_inputs: dict[str, Any], project_dir: Path, part: str, *, project_name: str = "s02_pwm") -> dict[str, Any]:
    return {
        "project_name": project_name,
        "project_dir": str(project_dir),
        "part": part,
        "top": project_inputs["top"],
        "rtl_files": project_inputs["rtl_files"],
        "xdc_files": project_inputs["xdc_files"],
        "sim_files": project_inputs["sim_files"],
        "testbench_top": project_inputs["testbench_top"],
        "target_language": project_inputs["target_language"],
        "force": False,
        "timeout_s": 300,
    }


def _repair_project_args(project_inputs: dict[str, Any], project_path: Path) -> dict[str, Any]:
    return {
        "project_path": str(project_path),
        "rtl_files": project_inputs["rtl_files"],
        "xdc_files": project_inputs["xdc_files"],
        "sim_files": project_inputs["sim_files"],
        "top": project_inputs["top"],
        "testbench_top": project_inputs["testbench_top"],
        "target_language": project_inputs["target_language"],
    }


def _live_partial_handoff(
    *,
    project_path: Path,
    project_dir: Path,
    artifacts: dict[str, Any],
    reports: dict[str, Any],
    signoff: dict[str, Any],
    audit: dict[str, Any],
    bundle: dict[str, Any],
    final_validation: dict[str, Any],
    bitstream_completed: bool = False,
    handoff_blocker: str = "",
    run_name: str = "impl_1",
) -> dict[str, Any]:
    artifact_manifest = _data_string(artifacts, "manifest_path")
    report_manifest = _data_string(reports, "manifest_path")
    bundle_manifest = _data_string(bundle, "manifest_path") or _data_string(bundle, "partial_manifest_path")
    completed_tools = [
        tool_name
        for tool_name, structured in (
            ("collect_build_artifacts", artifacts),
            ("collect_report_bundle", reports),
            ("run_pre_hw_signoff", signoff),
            ("run_project_audit", audit),
            ("collect_diagnostic_bundle", bundle),
            ("validate_diagnostic_bundle", final_validation),
        )
        if _tool_status_ok(structured)
    ]
    failed_tools = [
        {
            "tool": tool_name,
            "error_code": str(structured.get("error_code", "")),
            "status": _status_from_structured(structured),
            "message": str(structured.get("message") or structured.get("summary") or "")[:500],
        }
        for tool_name, structured in (
            ("collect_build_artifacts", artifacts),
            ("collect_report_bundle", reports),
            ("run_pre_hw_signoff", signoff),
            ("run_project_audit", audit),
            ("collect_diagnostic_bundle", bundle),
            ("validate_diagnostic_bundle", final_validation),
        )
        if structured and not _tool_status_ok(structured)
    ]
    failed_tool = failed_tools[0]["tool"] if failed_tools else ""
    return {
        "handoff_partial": bool(bitstream_completed) and not _tool_status_ok(final_validation),
        "handoff_blocker": handoff_blocker,
        "project_path": str(project_path),
        "project_dir": str(project_dir),
        "artifact_manifest_path": artifact_manifest,
        "report_manifest_path": report_manifest,
        "diagnostic_manifest_path": bundle_manifest,
        "workflow_trace_path": str(project_dir / "vmcp_diagnostics" / "workflow_trace.jsonl"),
        "completed_tools": completed_tools,
        "failed_tools": failed_tools,
        "hardware_validation_status": "NOT_VALIDATED",
        "next_actions": _post_stop_resume_actions(
            project_path=str(project_path),
            manifest_path=bundle_manifest,
            run_name=run_name,
            failed_tool=failed_tool,
            project_dir=str(project_dir),
            report_manifest_path=report_manifest,
        ),
    }


def _data_string(structured: dict[str, Any], key: str) -> str:
    data = structured.get("data") if isinstance(structured.get("data"), dict) else {}
    return str(data.get(key, "") or "")


def _live_project_failure(scenario_id: str, label: str, scenario_dir: Path, transcript: list[dict[str, Any]], *, tools: int, summary: str) -> dict[str, Any]:
    return {
        "id": scenario_id,
        "label": label,
        "status": "BLOCK",
        "executed": True,
        "evidence_dir": str(scenario_dir),
        "tool_count": tools,
        "tool_definition_count": tools,
        "tool_call_count": len(transcript),
        **scenario_evidence_fields(scenario_id),
        "tool_count_note": "tool_definition_count is MCP server tool definitions; tool_call_count is runner-visible MCP calls.",
        "tool_calls": transcript,
        "hardware_validation_status": "NOT_VALIDATED",
        "summary": summary,
        "recommendations": [f"Inspect the last failed tool call and follow its next_actions before rerunning {scenario_id}."],
    }


def _simulation_passed(structured: dict[str, Any]) -> bool:
    if not _structured_ok(structured):
        return False
    data = structured.get("data") if isinstance(structured.get("data"), dict) else {}
    diagnosis = data.get("simulation_diagnosis") if isinstance(data.get("simulation_diagnosis"), dict) else {}
    return data.get("status") == "completed" and diagnosis.get("primary_cause") in {"testbench_pass", "completed", "completed_with_testbench_vcd"}


async def run_behavioral_simulation_with_transient_retry(
    session: Any,
    args: dict[str, Any],
    transcript: list[dict[str, Any]],
    *,
    recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    simulation_args = {
        "execution_intent": "execute runner-controlled HDL/testbench code inside the configured trusted test workspace",
        "confirm": "RUN_TRUSTED_XSIM",
        "incremental": False,
        **args,
    }
    result = await call_tool(session, "run_behavioral_simulation", simulation_args, transcript)
    if result.get("error_code") != "SIMULATION_XSIM_LAUNCH_TRANSIENT":
        return result
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if data.get("abort_attempted"):
        if not data.get("managed_session_stopped"):
            return result
        recovery = recovery or {}
        runtime_dir = str(recovery.get("runtime_dir") or data.get("runtime_dir") or "")
        project_path = str(recovery.get("project_path") or data.get("project_path") or "")
        if not project_path:
            return result
        start_args: dict[str, Any] = {"timeout_s": int(recovery.get("start_timeout_s") or 240)}
        if runtime_dir:
            start_args["runtime_dir"] = runtime_dir
        restarted = await call_tool(session, "start_session", start_args, transcript)
        if not _structured_ok(restarted):
            return restarted
        reopened = await call_tool(session, "open_project", {"project_path": project_path, "timeout_s": 180}, transcript)
        if not _structured_ok(reopened):
            return reopened
    return await call_tool(session, "run_behavioral_simulation", simulation_args, transcript)


def _s03_initial_failure_detected(structured: dict[str, Any]) -> bool:
    if structured.get("error_code") != "SIMULATION_FAILED":
        return False
    data = structured.get("data") if isinstance(structured.get("data"), dict) else {}
    diagnosis = data.get("simulation_diagnosis") if isinstance(data.get("simulation_diagnosis"), dict) else {}
    span = data.get("log_span") if isinstance(data.get("log_span"), dict) else {}
    return (
        data.get("status") == "failed"
        and diagnosis.get("primary_cause") == "testbench_failure"
        and data.get("status_source") == "simulation_invocation_log_span"
        and int(span.get("end", 0) or 0) > int(span.get("start", 0) or 0)
    )


def _run_complete(structured: dict[str, Any]) -> bool:
    if not _structured_ok(structured):
        return False
    data = structured.get("data") if isinstance(structured.get("data"), dict) else {}
    return data.get("terminal") is True and str(data.get("state", "")).lower() == "complete"


def _structured_ok(structured: dict[str, Any]) -> bool:
    return isinstance(structured, dict) and structured.get("ok") is True


def _tool_status_ok(structured: dict[str, Any]) -> bool:
    if not _structured_ok(structured):
        return False
    data = structured.get("data") if isinstance(structured.get("data"), dict) else {}
    return str(data.get("status", "READY")).upper() != "BLOCK"


def _status_from_structured(structured: dict[str, Any]) -> str:
    data = structured.get("data") if isinstance(structured.get("data"), dict) else {}
    health = data.get("health") if isinstance(data.get("health"), dict) else {}
    return str(data.get("status") or health.get("status") or structured.get("error_code") or "")


def _summarize_next_actions(actions: Any) -> list[dict[str, Any]]:
    if not isinstance(actions, list):
        return []
    return [dict(item) for item in actions[:8] if isinstance(item, dict)]


def _error_code(structured: dict[str, Any]) -> str:
    return str(structured.get("error_code", ""))


def _first_hardware_status(transcript: list[dict[str, Any]]) -> str:
    for item in transcript:
        status = str(item.get("hardware_validation_status", ""))
        if status:
            return status
    return ""


def _hardware_status_from_structured(structured: dict[str, Any]) -> str:
    data = structured.get("data") if isinstance(structured.get("data"), dict) else {}
    hardware = structured.get("hardware_validation")
    if not isinstance(hardware, dict):
        hardware = data.get("hardware_validation") if isinstance(data.get("hardware_validation"), dict) else {}
    return str(hardware.get("status", ""))


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", "")
        if text:
            parts.append(str(text))
    return "\n".join(parts)[:1000]


def _combined_hardware_status(results: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("hardware_validation_status", "")) for item in results if item.get("hardware_validation_status")}
    if not statuses:
        return "NOT_VALIDATED"
    if statuses == {"NOT_VALIDATED"}:
        return "NOT_VALIDATED"
    return ",".join(sorted(statuses))


def _stdio_env(
    workspace: Path,
    *,
    runtime_dir: Path | None = None,
    tool_profile: str | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    if _RUNNER_RELEASE_MODE:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = str(workspace / "src")
    env.setdefault("SystemRoot", r"C:\WINDOWS")
    env.setdefault("WINDIR", r"C:\WINDOWS")
    env.setdefault("VIVADO_AGENT_MCP_TRUSTED_PROJECT_ROOTS", str((workspace / "test_use").resolve()))
    if runtime_dir is not None:
        env["VIVADO_AGENT_MCP_RUNTIME_DIR"] = str(runtime_dir.resolve())
    env["VIVADO_AGENT_MCP_TOOL_PROFILE"] = tool_profile or "core"
    return env


def _nested_stdio_regression_command(
    workspace: Path,
    *,
    output_dir: Path,
    python_exe: str,
) -> list[str]:
    command = [
        python_exe,
        str(workspace / "tests" / "agent_stdio_regression.py"),
        "--output-dir",
        str(output_dir),
        "--python",
        python_exe,
    ]
    if _RUNNER_RELEASE_MODE:
        stdio_entry = next(
            (
                item
                for item in _RUNNER_HARNESS_IDENTITY.get("files", [])
                if isinstance(item, dict) and item.get("path") == "tests/agent_stdio_regression.py"
            ),
            {},
        )
        command.extend(
            [
                "--installed-package",
                "--expected-package-import-path",
                str(Path(str(_installed_package.__file__)).resolve()),
                "--expected-harness-sha256",
                str(stdio_entry.get("sha256", "")),
            ]
        )
    return command


def _nested_stdio_package_evidence_ok(
    data: dict[str, Any],
    *,
    workspace: Path,
    python_exe: str,
) -> bool:
    if not _RUNNER_RELEASE_MODE:
        return True
    package = data.get("package_execution")
    if not isinstance(package, dict):
        return False
    try:
        expected_import = Path(str(_installed_package.__file__)).resolve()
        regression_import = Path(str(package.get("regression_import_path", ""))).resolve()
        server_import = Path(str(package.get("expected_mcp_import_path", ""))).resolve()
        reported_python = Path(str(package.get("python_executable", ""))).resolve()
        requested_python = Path(python_exe).resolve()
        workspace_root = workspace.resolve()
    except (OSError, ValueError, TypeError):
        return False
    if not all(
        [
            str(package.get("mode", "")) == "installed_package",
            package.get("workspace_source_enabled") is False,
            package.get("mcp_server_import_guard") is True,
            package.get("timeout_server_import_guard") is True,
            package.get("harness_self_verified") is True,
            str(package.get("harness_sha256", ""))
            == str(
                next(
                    (
                        item.get("sha256", "")
                        for item in _RUNNER_HARNESS_IDENTITY.get("files", [])
                        if isinstance(item, dict) and item.get("path") == "tests/agent_stdio_regression.py"
                    ),
                    "",
                )
            ),
            regression_import == expected_import,
            server_import == expected_import,
            reported_python == requested_python,
        ]
    ):
        return False
    try:
        regression_import.relative_to(workspace_root)
    except ValueError:
        return True
    return False


def _ensure_src_on_path(workspace: Path) -> None:
    src = str((workspace / "src").resolve())
    if src not in sys.path:
        sys.path.insert(0, src)


def _freshness(source: str) -> dict[str, Any]:
    return {
        "status": "FRESH",
        "run_name": "impl_1",
        "needs_refresh": False,
        "collected_at": _now(),
        "source": source,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


if __name__ == "__main__":
    raise SystemExit(main())
