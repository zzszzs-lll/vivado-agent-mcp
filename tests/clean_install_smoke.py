from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_WORKSPACE = Path(__file__).resolve().parents[1]
_SRC = str(_WORKSPACE / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from vivado_agent_mcp import __version__
from vivado_agent_mcp.release_identity import (
    compare_package_member_manifests,
    materialize_immutable_git_snapshot,
    package_member_manifest,
    sha256_file,
    source_identity,
    source_wheel_pair_id,
    wheel_package_member_manifest,
)


DEFAULT_OUTPUT_DIR = "test_use/clean_install_smoke"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a wheel or accept an exact prebuilt wheel, install it into a clean venv, and run customer-facing preflight commands.",
    )
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[1]), help="Repository root.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for run artifacts.")
    parser.add_argument("--python", default=sys.executable, help="Python executable used to build the wheel and create the venv.")
    parser.add_argument("--wheel-path", default="", help="Exact prebuilt vivado-agent-mcp wheel to install instead of rebuilding from source.")
    parser.add_argument("--expected-wheel-sha256", default="", help="Expected SHA256 for --wheel-path; a mismatch blocks before installation.")
    parser.add_argument("--runtime-lock", default="", help="Pre-resolved runtime requirements file with exact versions and SHA256 hashes.")
    parser.add_argument(
        "--source-provenance-manifest",
        default="",
        help="source-provenance.json produced with the supplied exact dist wheel.",
    )
    parser.add_argument("--include-vivado-probe", action="store_true", help="Run bounded Vivado launch probes in doctor and selftest.")
    parser.add_argument("--probe-timeout-s", type=int, default=60, help="Vivado probe timeout when --include-vivado-probe is set.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON report.")
    parser.add_argument("--evidence-run-id", default="", help="Shared 32-hex run nonce used to bind clean-install and scenario evidence.")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    output_root = Path(args.output_dir).expanduser()
    if not output_root.is_absolute():
        output_root = workspace / output_root
    run_dir = output_root.resolve() / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    evidence_run_id = str(args.evidence_run_id).lower()
    if evidence_run_id and not re.fullmatch(r"[0-9a-f]{32}", evidence_run_id):
        parser.error("--evidence-run-id must be exactly 32 hexadecimal characters")
    expected_wheel_sha256 = str(args.expected_wheel_sha256).strip().lower()
    if expected_wheel_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_wheel_sha256):
        parser.error("--expected-wheel-sha256 must be exactly 64 hexadecimal characters")
    wheel_path = Path(args.wheel_path).expanduser() if args.wheel_path else None
    if wheel_path is not None and not wheel_path.is_absolute():
        wheel_path = workspace / wheel_path
    runtime_lock_path = Path(args.runtime_lock).expanduser() if args.runtime_lock else None
    if runtime_lock_path is not None and not runtime_lock_path.is_absolute():
        runtime_lock_path = workspace / runtime_lock_path
    source_provenance_manifest_path = (
        Path(args.source_provenance_manifest).expanduser() if args.source_provenance_manifest else None
    )
    if source_provenance_manifest_path is not None and not source_provenance_manifest_path.is_absolute():
        source_provenance_manifest_path = workspace / source_provenance_manifest_path
    report = run_clean_install_smoke(
        workspace=workspace,
        run_dir=run_dir,
        python_exe=Path(args.python).resolve(),
        include_vivado_probe=bool(args.include_vivado_probe),
        probe_timeout_s=max(1, int(args.probe_timeout_s)),
        evidence_run_id=evidence_run_id,
        wheel_path=wheel_path.resolve() if wheel_path else None,
        expected_wheel_sha256=expected_wheel_sha256,
        runtime_lock_path=runtime_lock_path.resolve() if runtime_lock_path else None,
        source_provenance_manifest_path=(
            source_provenance_manifest_path.resolve() if source_provenance_manifest_path else None
        ),
    )
    result_path = run_dir / "clean_install_smoke_result.json"
    report["result_path"] = str(result_path)
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"clean-install smoke: {report['status']}")
        print(report["summary"])
        print(f"result_path={result_path}")
        print(f"wheel_path={report.get('wheel_path', '')}")
        for check in report["checks"]:
            print(f"[{check['status']}] {check['id']}: {check['message']}")
    return 0 if report["status"] in {"PASS", "WARN"} else 2


def run_clean_install_smoke(
    *,
    workspace: Path,
    run_dir: Path,
    python_exe: Path,
    include_vivado_probe: bool,
    probe_timeout_s: int,
    evidence_run_id: str = "",
    wheel_path: Path | None = None,
    expected_wheel_sha256: str = "",
    runtime_lock_path: Path | None = None,
    source_provenance_manifest_path: Path | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    source_before_build = source_identity(workspace)
    expected_wheel_sha256 = expected_wheel_sha256.strip().lower()
    wheelhouse = run_dir / "wheelhouse"
    venv_dir = run_dir / "venv"
    wheelhouse.mkdir(parents=True, exist_ok=True)

    if runtime_lock_path is not None and not runtime_lock_path.is_file():
        _add_check(
            checks,
            "dependency_resolution_lock",
            "BLOCK",
            "The requested pre-resolved runtime lock file does not exist.",
            {"runtime_lock_path": str(runtime_lock_path)},
        )
        return _report(
            workspace,
            run_dir,
            python_exe,
            include_vivado_probe,
            probe_timeout_s,
            checks,
            commands,
            wheel_path=wheel_path,
            evidence_run_id=evidence_run_id,
            expected_wheel_sha256=expected_wheel_sha256,
            wheel_source="supplied_dist" if wheel_path else "built_from_git_snapshot",
        )

    if not (workspace / "pyproject.toml").exists():
        _add_check(checks, "workspace", "BLOCK", "pyproject.toml was not found.", {"workspace": str(workspace)})
        return _report(workspace, run_dir, python_exe, include_vivado_probe, probe_timeout_s, checks, commands, evidence_run_id=evidence_run_id)
    if expected_wheel_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_wheel_sha256):
        _add_check(
            checks,
            "wheel_integrity",
            "BLOCK",
            "Expected wheel SHA256 must contain exactly 64 hexadecimal characters.",
            {"expected_sha256": expected_wheel_sha256},
        )
        return _report(
            workspace,
            run_dir,
            python_exe,
            include_vivado_probe,
            probe_timeout_s,
            checks,
            commands,
            wheel_path=wheel_path,
            evidence_run_id=evidence_run_id,
            expected_wheel_sha256=expected_wheel_sha256,
            wheel_source="supplied_dist" if wheel_path else "built_from_git_snapshot",
        )

    supplied_wheel = wheel_path.resolve() if wheel_path else None
    wheel_source = "supplied_dist" if supplied_wheel else "built_from_git_snapshot"
    wheel_package_version = ""
    wheel_version_verified = False
    if supplied_wheel:
        if not supplied_wheel.is_file() or not supplied_wheel.name.startswith("vivado_agent_mcp-") or supplied_wheel.suffix != ".whl":
            _add_check(
                checks,
                "wheel_artifact",
                "BLOCK",
                "The supplied vivado-agent-mcp wheel does not exist or has an unexpected filename.",
                {"wheel_path": str(supplied_wheel)},
            )
            return _report(
                workspace,
                run_dir,
                python_exe,
                include_vivado_probe,
                probe_timeout_s,
                checks,
                commands,
                wheel_path=supplied_wheel,
                evidence_run_id=evidence_run_id,
                expected_wheel_sha256=expected_wheel_sha256,
                wheel_source=wheel_source,
            )
        wheel_path = supplied_wheel
        _add_check(checks, "wheel_artifact", "PASS", "The supplied dist wheel exists and will be installed without rebuilding the project.", {"wheel_path": str(wheel_path)})
        actual_sha256 = sha256_file(wheel_path)
        sha_matches = not expected_wheel_sha256 or actual_sha256 == expected_wheel_sha256
        _add_check(
            checks,
            "wheel_integrity",
            "PASS" if sha_matches else "BLOCK",
            "The supplied wheel SHA256 matches the expected value." if sha_matches and expected_wheel_sha256 else (
                "The supplied wheel SHA256 was recorded; no expected value was provided."
                if sha_matches
                else "The supplied wheel SHA256 does not match the expected value."
            ),
            {"expected_sha256": expected_wheel_sha256, "actual_sha256": actual_sha256},
        )
        if not sha_matches:
            return _report(
                workspace,
                run_dir,
                python_exe,
                include_vivado_probe,
                probe_timeout_s,
                checks,
                commands,
                wheel_path=wheel_path,
                evidence_run_id=evidence_run_id,
                expected_wheel_sha256=expected_wheel_sha256,
                wheel_sha256_verified=False,
                wheel_source=wheel_source,
            )
        if source_provenance_manifest_path is not None:
            provenance_verified, provenance_data = _verify_supplied_source_provenance(
                workspace=workspace,
                snapshot_dir=run_dir / "source_provenance_snapshot",
                manifest_path=source_provenance_manifest_path,
                source=source_before_build,
                wheel_path=wheel_path,
                wheel_sha256=actual_sha256,
            )
            _add_check(
                checks,
                "source_wheel_provenance",
                "PASS" if provenance_verified else "BLOCK",
                (
                    "The supplied exact wheel is bound to the current clean Git source and immutable build manifest."
                    if provenance_verified
                    else "The supplied wheel does not match the current source provenance manifest."
                ),
                provenance_data,
            )
            if not provenance_verified:
                return _report(
                    workspace,
                    run_dir,
                    python_exe,
                    include_vivado_probe,
                    probe_timeout_s,
                    checks,
                    commands,
                    wheel_path=wheel_path,
                    evidence_run_id=evidence_run_id,
                    expected_wheel_sha256=expected_wheel_sha256,
                    wheel_sha256_verified=bool(expected_wheel_sha256 and sha_matches),
                    wheel_source=wheel_source,
                )
            wheel_source = "supplied_dist_with_provenance"
        else:
            _add_check(
                checks,
                "source_wheel_provenance",
                "NOT_VERIFIED",
                "A supplied wheel is not proven to have been built from the current workspace source.",
                {"source_identity": source_before_build},
            )
    else:
        snapshot = materialize_immutable_git_snapshot(workspace, run_dir / "source_snapshot")
        snapshot_ok = snapshot.get("ok") is True
        snapshot_check_data = {key: value for key, value in snapshot.items() if key != "package_manifest"}
        if isinstance(snapshot.get("package_manifest"), dict):
            snapshot_check_data["package_manifest"] = {
                "schema_version": snapshot["package_manifest"].get("schema_version"),
                "member_count": snapshot["package_manifest"].get("member_count"),
                "digest": snapshot["package_manifest"].get("digest"),
            }
        _add_check(
            checks,
            "immutable_source_snapshot",
            "PASS" if snapshot_ok else "BLOCK",
            "A clean immutable git archive snapshot was materialized for the wheel build."
            if snapshot_ok
            else "The immutable Git source snapshot could not be established.",
            snapshot_check_data,
        )
        if not snapshot_ok:
            return _report(
                workspace,
                run_dir,
                python_exe,
                include_vivado_probe,
                probe_timeout_s,
                checks,
                commands,
                evidence_run_id=evidence_run_id,
                expected_wheel_sha256=expected_wheel_sha256,
                wheel_source=wheel_source,
            )
        build_root = Path(str(snapshot["snapshot_root"]))
        build_command = [str(python_exe), "-m", "pip", "wheel"]
        if runtime_lock_path is not None:
            build_command.extend(["--no-deps", "--no-build-isolation"])
        build_command.extend([str(build_root), "--wheel-dir", str(wheelhouse)])
        build = _run_command(
            build_command,
            cwd=build_root,
            commands=commands,
            timeout_s=600,
        )
        _add_check(
            checks,
            "wheel_build",
            "PASS" if build["returncode"] == 0 else "BLOCK",
            "Wheel build completed." if build["returncode"] == 0 else "Wheel build failed.",
            {"returncode": build["returncode"]},
        )
        wheel_path = _find_project_wheel(wheelhouse)
        if not wheel_path:
            _add_check(checks, "wheel_artifact", "BLOCK", "vivado-agent-mcp wheel was not found in wheelhouse.", {"wheelhouse": str(wheelhouse)})
            return _report(workspace, run_dir, python_exe, include_vivado_probe, probe_timeout_s, checks, commands, evidence_run_id=evidence_run_id)
        _add_check(checks, "wheel_artifact", "PASS", "vivado-agent-mcp wheel was produced.", {"wheel_path": str(wheel_path)})
        actual_sha256 = sha256_file(wheel_path)
        sha_matches = not expected_wheel_sha256 or actual_sha256 == expected_wheel_sha256
        _add_check(
            checks,
            "wheel_integrity",
            "PASS" if sha_matches else "BLOCK",
            "Built wheel SHA256 is recorded and matches the expected value when supplied." if sha_matches else "Built wheel SHA256 does not match the expected value.",
            {"expected_sha256": expected_wheel_sha256, "actual_sha256": actual_sha256},
        )
        if not sha_matches:
            return _report(
                workspace,
                run_dir,
                python_exe,
                include_vivado_probe,
                probe_timeout_s,
                checks,
                commands,
                wheel_path=wheel_path,
                evidence_run_id=evidence_run_id,
                expected_wheel_sha256=expected_wheel_sha256,
                wheel_source=wheel_source,
            )
        source_after_build = source_identity(workspace)
        wheel_package_manifest = wheel_package_member_manifest(wheel_path)
        package_member_comparison = compare_package_member_manifests(
            snapshot["package_manifest"],
            wheel_package_manifest,
        )
        _add_check(
            checks,
            "source_wheel_package_members",
            "PASS" if package_member_comparison["matches"] else "BLOCK",
            "Wheel package members match the immutable Git snapshot byte-for-byte."
            if package_member_comparison["matches"]
            else "Wheel package members differ from the immutable Git snapshot.",
            package_member_comparison,
        )
        source_wheel_provenance_verified = (
            source_before_build.get("available") is True
            and source_before_build.get("clean") is True
            and source_after_build.get("available") is True
            and source_after_build.get("clean") is True
            and source_before_build.get("identity_id") == source_after_build.get("identity_id")
            and snapshot.get("source_identity", {}).get("identity_id") == source_before_build.get("identity_id")
            and package_member_comparison["matches"]
        )
        provenance_manifest = {
            "schema_version": 1,
            "source_identity": snapshot["source_identity"],
            "snapshot_type": snapshot["snapshot_type"],
            "snapshot_archive_sha256": snapshot["archive_sha256"],
            "source_package_manifest": snapshot["package_manifest"],
            "wheel_package_manifest": wheel_package_manifest,
            "package_member_comparison": package_member_comparison,
            "wheel_path": str(wheel_path),
            "wheel_sha256": actual_sha256,
            "source_wheel_provenance_verified": source_wheel_provenance_verified,
        }
        provenance_manifest_path = run_dir / "source_wheel_provenance_manifest.json"
        provenance_manifest_path.write_text(
            json.dumps(provenance_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _add_check(
            checks,
            "source_wheel_provenance",
            "PASS" if source_wheel_provenance_verified else "NOT_VERIFIED",
            (
                "The wheel was built from an immutable Git snapshot and package members match byte-for-byte."
                if source_wheel_provenance_verified
                else "The wheel build could not be bound to an unchanged clean source identity."
            ),
            {
                "source_identity": source_after_build,
                "source_identity_before_build": source_before_build,
                "snapshot_type": snapshot["snapshot_type"],
                "snapshot_archive_sha256": snapshot["archive_sha256"],
                "source_package_digest": snapshot["package_manifest"]["digest"],
                "wheel_package_digest": wheel_package_manifest["digest"],
                "provenance_manifest_path": str(provenance_manifest_path),
                "provenance_manifest_sha256": sha256_file(provenance_manifest_path),
            },
        )

    try:
        wheel_identity = _read_wheel_identity(wheel_path)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        _add_check(
            checks,
            "wheel_version_identity",
            "BLOCK",
            "Wheel package metadata could not be read safely.",
            {"wheel_path": str(wheel_path), "error": str(exc)},
        )
        return _report(
            workspace,
            run_dir,
            python_exe,
            include_vivado_probe,
            probe_timeout_s,
            checks,
            commands,
            wheel_path=wheel_path,
            evidence_run_id=evidence_run_id,
            expected_wheel_sha256=expected_wheel_sha256,
            wheel_source=wheel_source,
        )
    wheel_package_version = wheel_identity["version"]
    wheel_version_verified = wheel_identity["name"] == "vivado-agent-mcp" and wheel_package_version == __version__
    _add_check(
        checks,
        "wheel_version_identity",
        "PASS" if wheel_version_verified else "BLOCK",
        "Wheel name and version match the source package contract." if wheel_version_verified else "Wheel name or version does not match the source package contract.",
        {
            "wheel_name": wheel_identity["name"],
            "wheel_version": wheel_package_version,
            "workspace_version": __version__,
            "metadata_path": wheel_identity["metadata_path"],
        },
    )
    if not wheel_version_verified:
        return _report(
            workspace,
            run_dir,
            python_exe,
            include_vivado_probe,
            probe_timeout_s,
            checks,
            commands,
            wheel_path=wheel_path,
            evidence_run_id=evidence_run_id,
            expected_wheel_sha256=expected_wheel_sha256,
            wheel_source=wheel_source,
            wheel_package_version=wheel_package_version,
            wheel_version_verified=False,
        )

    if supplied_wheel:
        if runtime_lock_path is not None:
            dependencies = _run_command(
                [
                    str(python_exe),
                    "-m",
                    "pip",
                    "download",
                    "--require-hashes",
                    "-r",
                    str(runtime_lock_path),
                    "--dest",
                    str(wheelhouse),
                ],
                cwd=workspace,
                commands=commands,
                timeout_s=600,
            )
            if dependencies["returncode"] == 0:
                shutil.copy2(wheel_path, wheelhouse / wheel_path.name)
        else:
            dependencies = _run_command(
                [str(python_exe), "-m", "pip", "wheel", str(wheel_path), "--wheel-dir", str(wheelhouse)],
                cwd=workspace,
                commands=commands,
                timeout_s=600,
            )
        dependencies_ok = dependencies["returncode"] == 0
        _add_check(
            checks,
            "wheel_dependencies",
            "PASS" if dependencies_ok else "BLOCK",
            "Dependency wheels were prepared without rebuilding the project." if dependencies_ok else "Dependency wheel preparation failed.",
            {
                "returncode": dependencies["returncode"],
                "runtime_lock_path": str(runtime_lock_path) if runtime_lock_path else "",
                "pre_resolved": runtime_lock_path is not None,
            },
        )
        if not dependencies_ok:
            return _report(
                workspace,
                run_dir,
                python_exe,
                include_vivado_probe,
                probe_timeout_s,
                checks,
                commands,
                wheel_path=wheel_path,
                evidence_run_id=evidence_run_id,
                expected_wheel_sha256=expected_wheel_sha256,
                wheel_source=wheel_source,
                wheel_package_version=wheel_package_version,
                wheel_version_verified=True,
            )
    elif runtime_lock_path is not None:
        dependencies = _run_command(
            [
                str(python_exe),
                "-m",
                "pip",
                "download",
                "--require-hashes",
                "-r",
                str(runtime_lock_path),
                "--dest",
                str(wheelhouse),
            ],
            cwd=workspace,
            commands=commands,
            timeout_s=600,
        )
        dependencies_ok = dependencies["returncode"] == 0
        _add_check(
            checks,
            "wheel_dependencies",
            "PASS" if dependencies_ok else "BLOCK",
            "Pre-resolved dependency wheels were prepared from the runtime lock."
            if dependencies_ok
            else "Dependency wheel preparation from the runtime lock failed.",
            {
                "returncode": dependencies["returncode"],
                "runtime_lock_path": str(runtime_lock_path),
                "pre_resolved": True,
            },
        )
        if not dependencies_ok:
            return _report(
                workspace,
                run_dir,
                python_exe,
                include_vivado_probe,
                probe_timeout_s,
                checks,
                commands,
                wheel_path=wheel_path,
                evidence_run_id=evidence_run_id,
                expected_wheel_sha256=expected_wheel_sha256,
                wheel_source=wheel_source,
                wheel_package_version=wheel_package_version,
                wheel_version_verified=True,
            )

    dependency_lock_verified = runtime_lock_path is not None
    _add_check(
        checks,
        "dependency_resolution_lock",
        "PASS" if dependency_lock_verified else "NOT_VERIFIED",
        "Runtime dependencies were selected from a committed exact-version SHA256 lock."
        if dependency_lock_verified
        else "Runtime dependency versions were resolved during this run; installation bytes are hashed but selection is not reproducible.",
        {
            "runtime_lock_path": str(runtime_lock_path) if runtime_lock_path else "",
            "runtime_lock_sha256": sha256_file(runtime_lock_path) if runtime_lock_path else "",
        },
    )

    install_wheel_path = _find_project_wheel(wheelhouse)
    if not install_wheel_path:
        _add_check(
            checks,
            "wheel_install_snapshot",
            "BLOCK",
            "The controlled wheelhouse does not contain exactly one vivado-agent-mcp wheel.",
            {"wheelhouse": str(wheelhouse)},
        )
        return _report(
            workspace,
            run_dir,
            python_exe,
            include_vivado_probe,
            probe_timeout_s,
            checks,
            commands,
            wheel_path=wheel_path,
            evidence_run_id=evidence_run_id,
            expected_wheel_sha256=expected_wheel_sha256,
            wheel_source=wheel_source,
            wheel_package_version=wheel_package_version,
            wheel_version_verified=True,
        )
    install_wheel_sha256 = sha256_file(install_wheel_path)
    install_snapshot_matches = install_wheel_sha256 == actual_sha256
    _add_check(
        checks,
        "wheel_install_snapshot",
        "PASS" if install_snapshot_matches else "BLOCK",
        "The controlled install snapshot matches the verified source wheel." if install_snapshot_matches else "The controlled install snapshot does not match the verified source wheel.",
        {
            "source_wheel_path": str(wheel_path),
            "source_sha256": actual_sha256,
            "install_wheel_path": str(install_wheel_path),
            "install_sha256": install_wheel_sha256,
        },
    )
    if not install_snapshot_matches:
        return _report(
            workspace,
            run_dir,
            python_exe,
            include_vivado_probe,
            probe_timeout_s,
            checks,
            commands,
            wheel_path=wheel_path,
            evidence_run_id=evidence_run_id,
            expected_wheel_sha256=expected_wheel_sha256,
            wheel_sha256_verified=bool(
                expected_wheel_sha256
                and sha256_file(wheel_path) == expected_wheel_sha256
                and sha256_file(install_wheel_path) == expected_wheel_sha256
            ),
            wheel_source=wheel_source,
            wheel_package_version=wheel_package_version,
            wheel_version_verified=True,
            install_wheel_path=install_wheel_path,
            install_wheel_sha256=sha256_file(install_wheel_path),
            evidence_wheel_sha256=sha256_file(wheel_path),
        )

    try:
        requirements_path, hashed_wheel_count = _write_hashed_wheel_requirements(wheelhouse, run_dir / "wheel-requirements.txt")
    except (OSError, ValueError) as exc:
        _add_check(
            checks,
            "wheel_hash_lock",
            "BLOCK",
            "Hashed wheel requirements could not be generated.",
            {"error": str(exc), "wheelhouse": str(wheelhouse)},
        )
        return _report(
            workspace,
            run_dir,
            python_exe,
            include_vivado_probe,
            probe_timeout_s,
            checks,
            commands,
            wheel_path=wheel_path,
            evidence_run_id=evidence_run_id,
            expected_wheel_sha256=expected_wheel_sha256,
            wheel_source=wheel_source,
            wheel_package_version=wheel_package_version,
            wheel_version_verified=True,
            install_wheel_path=install_wheel_path,
            install_wheel_sha256=install_wheel_sha256,
        )
    _add_check(
        checks,
        "wheel_hash_lock",
        "PASS",
        "Every wheel installation input is pinned by a pip SHA256 requirement.",
        {"requirements_path": str(requirements_path), "wheel_count": hashed_wheel_count},
    )

    venv = _run_command([str(python_exe), "-m", "venv", str(venv_dir)], cwd=workspace, commands=commands, timeout_s=300)
    _add_check(
        checks,
        "venv_create",
        "PASS" if venv["returncode"] == 0 else "BLOCK",
        "Clean venv was created." if venv["returncode"] == 0 else "Clean venv creation failed.",
        {"venv_dir": str(venv_dir), "returncode": venv["returncode"]},
    )
    venv_python = _venv_python(venv_dir)
    if not venv_python.exists():
        _add_check(checks, "venv_python", "BLOCK", "Venv Python executable was not found.", {"python": str(venv_python)})
        failure_source_sha256 = sha256_file(wheel_path)
        failure_install_sha256 = sha256_file(install_wheel_path)
        return _report(
            workspace,
            run_dir,
            python_exe,
            include_vivado_probe,
            probe_timeout_s,
            checks,
            commands,
            wheel_path=wheel_path,
            evidence_run_id=evidence_run_id,
            expected_wheel_sha256=expected_wheel_sha256,
            wheel_sha256_verified=bool(
                expected_wheel_sha256
                and failure_source_sha256 == expected_wheel_sha256
                and failure_install_sha256 == expected_wheel_sha256
            ),
            wheel_source=wheel_source,
            wheel_package_version=wheel_package_version,
            wheel_version_verified=True,
            install_wheel_path=install_wheel_path,
            install_wheel_sha256=failure_install_sha256,
            evidence_wheel_sha256=failure_source_sha256,
        )
    _add_check(checks, "venv_python", "PASS", "Venv Python executable is available.", {"python": str(venv_python)})

    source_preinstall_sha256 = sha256_file(wheel_path)
    install_preinstall_sha256 = sha256_file(install_wheel_path)
    preinstall_integrity_ok = source_preinstall_sha256 == actual_sha256 and install_preinstall_sha256 == actual_sha256
    _add_check(
        checks,
        "wheel_pre_install_integrity",
        "PASS" if preinstall_integrity_ok else "BLOCK",
        "Source and controlled install wheel hashes still match immediately before pip." if preinstall_integrity_ok else "Wheel bytes changed before pip installation; package execution was blocked.",
        {
            "expected_sha256": actual_sha256,
            "source_preinstall_sha256": source_preinstall_sha256,
            "install_preinstall_sha256": install_preinstall_sha256,
        },
    )
    if not preinstall_integrity_ok:
        return _report(
            workspace,
            run_dir,
            python_exe,
            include_vivado_probe,
            probe_timeout_s,
            checks,
            commands,
            wheel_path=wheel_path,
            evidence_run_id=evidence_run_id,
            expected_wheel_sha256=expected_wheel_sha256,
            wheel_source=wheel_source,
            wheel_package_version=wheel_package_version,
            wheel_version_verified=True,
            install_wheel_path=install_wheel_path,
            install_wheel_sha256=install_wheel_sha256,
        )

    install = _run_command(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--require-hashes",
            "-r",
            str(requirements_path),
        ],
        cwd=workspace,
        commands=commands,
        timeout_s=600,
        clean_pythonpath=True,
    )
    _add_check(
        checks,
        "wheel_install",
        "PASS" if install["returncode"] == 0 else "BLOCK",
        "Wheel installed into clean venv without source PYTHONPATH." if install["returncode"] == 0 else "Wheel install failed.",
        {
            "returncode": install["returncode"],
            "clean_pythonpath": True,
            "require_hashes": True,
            "requirements_path": str(requirements_path),
        },
    )
    if install["returncode"] != 0:
        return _report(
            workspace,
            run_dir,
            python_exe,
            include_vivado_probe,
            probe_timeout_s,
            checks,
            commands,
            wheel_path=wheel_path,
            evidence_run_id=evidence_run_id,
            expected_wheel_sha256=expected_wheel_sha256,
            wheel_source=wheel_source,
            wheel_package_version=wheel_package_version,
            wheel_version_verified=True,
            install_wheel_path=install_wheel_path,
            install_wheel_sha256=install_wheel_sha256,
            pip_hashes_enforced=True,
        )

    version_cmd = _run_command(
        [str(venv_python), "-c", "import vivado_agent_mcp; print(vivado_agent_mcp.__version__)"],
        cwd=run_dir,
        commands=commands,
        timeout_s=60,
        clean_pythonpath=True,
    )
    _add_check(
        checks,
        "import_installed_package",
        "PASS" if version_cmd["returncode"] == 0 and version_cmd["stdout_tail"].strip() else "BLOCK",
        "Installed package imports from clean venv." if version_cmd["returncode"] == 0 else "Installed package import failed.",
        {"version": version_cmd["stdout_tail"].strip(), "returncode": version_cmd["returncode"]},
    )

    console_script = _console_script(venv_dir)
    console_exists = console_script.exists()
    _add_check(
        checks,
        "console_script",
        "PASS" if console_exists else "BLOCK",
        "vivado-agent-mcp console script exists." if console_exists else "vivado-agent-mcp console script is missing.",
        {"script": str(console_script)},
    )
    if not console_exists:
        failure_source_sha256 = sha256_file(wheel_path)
        failure_install_sha256 = sha256_file(install_wheel_path)
        return _report(
            workspace,
            run_dir,
            python_exe,
            include_vivado_probe,
            probe_timeout_s,
            checks,
            commands,
            wheel_path=wheel_path,
            evidence_run_id=evidence_run_id,
            expected_wheel_sha256=expected_wheel_sha256,
            wheel_sha256_verified=bool(
                expected_wheel_sha256
                and failure_source_sha256 == expected_wheel_sha256
                and failure_install_sha256 == expected_wheel_sha256
            ),
            wheel_source=wheel_source,
            wheel_package_version=wheel_package_version,
            wheel_version_verified=True,
            installed_version=version_cmd["stdout_tail"].strip(),
            install_wheel_path=install_wheel_path,
            install_wheel_sha256=failure_install_sha256,
            pip_hashes_enforced=True,
            evidence_wheel_sha256=failure_source_sha256,
        )

    installed_version = version_cmd["stdout_tail"].strip()
    installed_version_verified = (
        version_cmd["returncode"] == 0
        and installed_version == wheel_package_version
        and installed_version == __version__
    )
    _add_check(
        checks,
        "installed_version_identity",
        "PASS" if installed_version_verified else "BLOCK",
        "Installed package version matches wheel metadata and source version." if installed_version_verified else "Installed package version does not match wheel metadata and source version.",
        {
            "installed_version": installed_version,
            "wheel_version": wheel_package_version,
            "workspace_version": __version__,
        },
    )
    if not installed_version_verified:
        return _report(
            workspace,
            run_dir,
            python_exe,
            include_vivado_probe,
            probe_timeout_s,
            checks,
            commands,
            wheel_path=wheel_path,
            evidence_run_id=evidence_run_id,
            expected_wheel_sha256=expected_wheel_sha256,
            wheel_source=wheel_source,
            wheel_package_version=wheel_package_version,
            wheel_version_verified=True,
            installed_version=installed_version,
            installed_version_verified=False,
            install_wheel_path=install_wheel_path,
            install_wheel_sha256=install_wheel_sha256,
            pip_hashes_enforced=True,
        )
    console_help = _run_command([str(console_script), "--help"], cwd=run_dir, commands=commands, timeout_s=60, clean_pythonpath=True)
    help_text = console_help["stdout_tail"]
    help_ok = console_help["returncode"] == 0 and "vivado-agent-mcp" in help_text and "doctor" in help_text and "selftest" in help_text
    _add_check(
        checks,
        "console_help",
        "PASS" if help_ok else "BLOCK",
        "vivado-agent-mcp --help is usable." if help_ok else "vivado-agent-mcp --help failed or returned incomplete output.",
        {"returncode": console_help["returncode"]},
    )

    console_version = _run_command([str(console_script), "--version"], cwd=run_dir, commands=commands, timeout_s=60, clean_pythonpath=True)
    console_version_text = console_version["stdout_tail"].strip()
    version_ok = bool(installed_version) and console_version["returncode"] == 0 and console_version_text == installed_version
    _add_check(
        checks,
        "console_version",
        "PASS" if version_ok else "BLOCK",
        "vivado-agent-mcp --version matches the installed package." if version_ok else "vivado-agent-mcp --version does not match the installed package.",
        {"returncode": console_version["returncode"], "console_version": console_version_text, "installed_version": installed_version},
    )

    doctor_args = [str(console_script), "doctor", "--json"]
    if not include_vivado_probe:
        doctor_args.append("--no-probe-launch")
    else:
        doctor_args.extend(["--probe-timeout-s", str(probe_timeout_s)])
    doctor = _run_command(doctor_args, cwd=run_dir, commands=commands, timeout_s=max(120, probe_timeout_s + 60), clean_pythonpath=True)
    doctor_report = _parse_json(doctor["stdout"])
    doctor_status = str(doctor_report.get("status", "")) if isinstance(doctor_report, dict) else ""
    doctor_check_status, doctor_contract_ok, doctor_block_ids = _classify_doctor_report(
        doctor_report,
        returncode=int(doctor["returncode"]),
    )
    _add_check(
        checks,
        "doctor",
        doctor_check_status,
        (
            f"doctor returned a valid installed-package report with status={doctor_status}."
            if doctor_contract_ok
            else "doctor command did not return the expected installed-package report contract."
        ),
        {
            "returncode": doctor["returncode"],
            "status": doctor_status,
            "block_ids": sorted(doctor_block_ids),
            "include_vivado_probe": include_vivado_probe,
        },
    )

    selftest_dir = run_dir / "selftest"
    selftest_args = [str(console_script), "selftest", "--workspace", str(run_dir), "--output-dir", str(selftest_dir), "--json"]
    if include_vivado_probe:
        selftest_args.extend(["--include-vivado-probe", "--probe-timeout-s", str(probe_timeout_s)])
    selftest = _run_command(selftest_args, cwd=run_dir, commands=commands, timeout_s=max(180, probe_timeout_s + 120), clean_pythonpath=True)
    selftest_report = _parse_json(selftest["stdout"])
    selftest_status = str(selftest_report.get("status", "")) if isinstance(selftest_report, dict) else ""
    hardware = selftest_report.get("hardware_validation", {}) if isinstance(selftest_report, dict) else {}
    selftest_check_status = "PASS" if selftest_status == "PASS" else "WARN" if selftest_status == "WARN" else "BLOCK"
    _add_check(
        checks,
        "selftest",
        selftest_check_status if selftest["returncode"] == 0 else "BLOCK",
        f"selftest completed with status={selftest_status or 'unknown'}." if selftest["returncode"] == 0 else "selftest command failed.",
        {
            "returncode": selftest["returncode"],
            "status": selftest_status,
            "validation_scope": selftest_report.get("validation_scope") if isinstance(selftest_report, dict) else "",
            "hardware_status": hardware.get("status") if isinstance(hardware, dict) else "",
        },
    )

    final_sha256 = sha256_file(wheel_path)
    final_install_sha256 = sha256_file(install_wheel_path)
    wheel_unchanged = final_sha256 == actual_sha256 and final_install_sha256 == actual_sha256
    _add_check(
        checks,
        "wheel_post_install_integrity",
        "PASS" if wheel_unchanged else "BLOCK",
        "Wheel SHA256 remained unchanged through installation and smoke commands." if wheel_unchanged else "Wheel bytes changed during clean-install smoke.",
        {
            "before_sha256": actual_sha256,
            "source_after_sha256": final_sha256,
            "install_after_sha256": final_install_sha256,
        },
    )
    report = _report(
        workspace,
        run_dir,
        python_exe,
        include_vivado_probe,
        probe_timeout_s,
        checks,
        commands,
        wheel_path=wheel_path,
        evidence_run_id=evidence_run_id,
        expected_wheel_sha256=expected_wheel_sha256,
        wheel_sha256_verified=bool(expected_wheel_sha256 and wheel_unchanged and final_sha256 == expected_wheel_sha256),
        wheel_source=wheel_source,
        wheel_package_version=wheel_package_version,
        wheel_version_verified=True,
        installed_version=installed_version,
        installed_version_verified=installed_version_verified,
        install_wheel_path=install_wheel_path,
        install_wheel_sha256=final_install_sha256,
        pip_hashes_enforced=True,
        evidence_wheel_sha256=actual_sha256,
    )
    report["doctor_report"] = doctor_report if isinstance(doctor_report, dict) else {}
    report["selftest_report"] = selftest_report if isinstance(selftest_report, dict) else {}
    return report


def _classify_doctor_report(report: dict[str, Any], *, returncode: int) -> tuple[str, bool, set[str]]:
    status = str(report.get("status", "")) if isinstance(report, dict) else ""
    checks = report.get("checks", []) if isinstance(report, dict) else []
    block_ids = {
        str(item.get("id", ""))
        for item in checks
        if isinstance(item, dict) and str(item.get("status", "")).upper() == "BLOCK"
    }
    contract_ok = (
        returncode in {0, 2}
        and status in {"READY", "WARN", "BLOCK"}
        and isinstance(report.get("package"), dict)
        and isinstance(checks, list)
    )
    if contract_ok and status == "READY":
        return "PASS", True, block_ids
    expected_environment_blocks = {"vivado_path", "vivado_version", "vivado_launch_probe"}
    if contract_ok and (status == "WARN" or block_ids <= expected_environment_blocks):
        return "WARN", True, block_ids
    return "BLOCK", contract_ok, block_ids


def _verify_supplied_source_provenance(
    *,
    workspace: Path,
    snapshot_dir: Path,
    manifest_path: Path,
    source: dict[str, Any],
    wheel_path: Path,
    wheel_sha256: str,
) -> tuple[bool, dict[str, Any]]:
    reasons: list[str] = []
    manifest: dict[str, Any] = {}
    try:
        if not manifest_path.is_file():
            raise ValueError("manifest is not a regular file")
        if manifest_path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("manifest exceeds 20 MiB")
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("manifest root must be an object")
        manifest = loaded
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return False, {
            "manifest_path": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path) if manifest_path.is_file() else "",
            "reasons": [str(exc)],
            "source_identity": source,
        }

    manifest_source = manifest.get("source_identity") if isinstance(manifest.get("source_identity"), dict) else {}
    manifest_wheel = manifest.get("wheel") if isinstance(manifest.get("wheel"), dict) else {}
    manifest_package = manifest.get("package") if isinstance(manifest.get("package"), dict) else {}
    hardware = manifest.get("hardware_validation") if isinstance(manifest.get("hardware_validation"), dict) else {}
    source_manifest = manifest.get("source_package_manifest") if isinstance(manifest.get("source_package_manifest"), dict) else {}
    manifest_comparison = (
        manifest.get("package_member_comparison")
        if isinstance(manifest.get("package_member_comparison"), dict)
        else {}
    )
    immutable_snapshot = materialize_immutable_git_snapshot(workspace, snapshot_dir)
    immutable_source_manifest = (
        immutable_snapshot.get("package_manifest")
        if isinstance(immutable_snapshot.get("package_manifest"), dict)
        else {}
    )
    actual_wheel_manifest = wheel_package_member_manifest(wheel_path)
    source_comparison = compare_package_member_manifests(source_manifest, immutable_source_manifest)
    wheel_comparison = compare_package_member_manifests(source_manifest, actual_wheel_manifest)

    if manifest.get("status") != "PASS" or manifest.get("source_wheel_provenance_verified") is not True:
        reasons.append("manifest does not attest a successful source-to-wheel build")
    if source.get("available") is not True or source.get("clean") is not True:
        reasons.append("current workspace source identity is unavailable or dirty")
    if immutable_snapshot.get("ok") is not True:
        reasons.append("current immutable Git snapshot could not be materialized")
    elif immutable_snapshot.get("source_identity", {}).get("identity_id") != source.get("identity_id"):
        reasons.append("current immutable Git snapshot identity does not match the workspace")
    if not source.get("identity_id") or manifest_source.get("identity_id") != source.get("identity_id"):
        reasons.append("manifest source identity does not match the current workspace")
    if manifest_wheel.get("name") != wheel_path.name or manifest_wheel.get("sha256") != wheel_sha256:
        reasons.append("manifest wheel identity does not match the supplied wheel")
    if manifest_package.get("name") != "vivado-agent-mcp" or manifest_package.get("version") != __version__:
        reasons.append("manifest package identity does not match the installed source contract")
    if manifest_comparison.get("matches") is not True:
        reasons.append("manifest package member comparison is not successful")
    if not source_comparison["matches"]:
        reasons.append("manifest source package members do not match the current immutable Git snapshot")
    if not wheel_comparison["matches"]:
        reasons.append("supplied wheel package members do not match the manifest source package members")
    if hardware.get("status") != "NOT_VALIDATED" or hardware.get("validated") is not False:
        reasons.append("manifest hardware validation boundary is missing or contradictory")
    return not reasons, {
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "source_identity": manifest_source,
        "immutable_source_snapshot": {
            "ok": immutable_snapshot.get("ok") is True,
            "snapshot_type": str(immutable_snapshot.get("snapshot_type", "")),
            "archive_sha256": str(immutable_snapshot.get("archive_sha256", "")),
            "reason_code": str(immutable_snapshot.get("reason_code", "")),
            "reason": str(immutable_snapshot.get("reason", "")),
        },
        "wheel": {"name": wheel_path.name, "sha256": wheel_sha256},
        "source_package_comparison": source_comparison,
        "wheel_package_comparison": wheel_comparison,
        "hardware_validation": hardware,
        "reasons": reasons,
    }


def _validation_harness_manifest(workspace: Path) -> dict[str, Any]:
    relative_paths = [
        "tests/agent_scenario_runner.py",
        "tests/agent_stdio_regression.py",
        "tests/live_qualification_runner.py",
    ]
    files: list[dict[str, Any]] = []
    errors: list[str] = []
    for relative in relative_paths:
        path = (workspace / relative).resolve()
        try:
            path.relative_to(workspace.resolve())
        except ValueError:
            errors.append(f"validation harness path escapes workspace: {relative}")
            continue
        if not path.is_file():
            errors.append(f"validation harness file is missing: {relative}")
            continue
        payload = path.read_bytes()
        files.append(
            {
                "path": relative,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "status": "READY" if not errors and len(files) == len(relative_paths) else "BLOCK",
        "policy": "exact_source_harness_size_sha256",
        "identity_sha256": hashlib.sha256(canonical).hexdigest() if files else "",
        "files": files,
        "errors": errors,
    }


def _report(
    workspace: Path,
    run_dir: Path,
    python_exe: Path,
    include_vivado_probe: bool,
    probe_timeout_s: int,
    checks: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    *,
    wheel_path: Path | None = None,
    evidence_run_id: str = "",
    expected_wheel_sha256: str = "",
    wheel_sha256_verified: bool = False,
    wheel_source: str = "built_from_git_snapshot",
    wheel_package_version: str = "",
    wheel_version_verified: bool = False,
    installed_version: str = "",
    installed_version_verified: bool = False,
    install_wheel_path: Path | None = None,
    install_wheel_sha256: str = "",
    pip_hashes_enforced: bool = False,
    evidence_wheel_sha256: str = "",
) -> dict[str, Any]:
    status = _overall_status(checks)
    check_states = {str(check["id"]): str(check["status"]) for check in checks}
    provenance_check = next((check for check in checks if check["id"] == "source_wheel_provenance"), None)
    provenance_data = provenance_check.get("data", {}) if isinstance(provenance_check, dict) else {}
    source = provenance_data.get("source_identity") if isinstance(provenance_data.get("source_identity"), dict) else source_identity(workspace)
    wheel_sha256 = evidence_wheel_sha256 or (sha256_file(wheel_path) if wheel_path and wheel_path.is_file() else "")
    wheel_sha256_verified = bool(
        wheel_sha256_verified
        or (
            check_states.get("wheel_integrity") == "PASS"
            and bool(expected_wheel_sha256)
            and wheel_sha256 == expected_wheel_sha256
        )
    )
    wheel_version_verified = bool(
        wheel_version_verified or check_states.get("wheel_version_identity") == "PASS"
    )
    snapshot_check = next((check for check in checks if check["id"] == "wheel_install_snapshot"), None)
    snapshot_data = snapshot_check.get("data", {}) if isinstance(snapshot_check, dict) else {}
    snapshot_path = str(snapshot_data.get("install_wheel_path", ""))
    if install_wheel_path is None and snapshot_path:
        install_wheel_path = Path(snapshot_path)
    if not install_wheel_sha256:
        install_wheel_sha256 = str(snapshot_data.get("install_sha256", ""))
    pip_hashes_enforced = bool(
        pip_hashes_enforced
        or (
            check_states.get("wheel_hash_lock") == "PASS"
            and "wheel_install" in check_states
        )
    )
    source_wheel_provenance_verified = bool(
        provenance_check
        and provenance_check.get("status") == "PASS"
        and wheel_source in {"built_from_git_snapshot", "supplied_dist_with_provenance"}
    )
    dependency_lock_verified = check_states.get("dependency_resolution_lock") == "PASS"
    validation_harness = _validation_harness_manifest(workspace)
    pair_id = source_wheel_pair_id(
        source=source,
        wheel_sha256=wheel_sha256,
        package_version=wheel_package_version if wheel_version_verified else "",
        source_wheel_provenance_verified=source_wheel_provenance_verified,
    )
    release_evidence_ready = bool(
        pair_id
        and status != "BLOCK"
        and dependency_lock_verified
        and validation_harness["status"] == "READY"
    )
    release_id = pair_id if release_evidence_ready else ""
    return {
        "ok": status != "BLOCK",
        "status": status,
        "summary": _summary(status),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "name": "vivado-agent-mcp",
            "version": wheel_package_version or __version__,
            "workspace_version": __version__,
            "wheel_version": wheel_package_version,
            "installed_version": installed_version,
            "version_verified": bool(wheel_version_verified and (not installed_version or installed_version_verified)),
        },
        "execution_mode": "clean_venv_wheel_install_smoke",
        "workspace": str(workspace),
        "run_dir": str(run_dir),
        "python": str(python_exe),
        "wheel_path": str(wheel_path) if wheel_path else "",
        "wheel_sha256": wheel_sha256,
        "wheel": {
            "name": wheel_path.name if wheel_path else "",
            "sha256": wheel_sha256,
        },
        "expected_wheel_sha256": expected_wheel_sha256,
        "wheel_sha256_verified": wheel_sha256_verified,
        "wheel_source": wheel_source,
        "install_wheel_path": str(install_wheel_path) if install_wheel_path else "",
        "install_wheel_sha256": install_wheel_sha256,
        "pip_hashes_enforced": pip_hashes_enforced,
        "dependency_lock_verified": dependency_lock_verified,
        "source_identity": source,
        "source_wheel_provenance_verified": source_wheel_provenance_verified,
        "source_wheel_pair_id": pair_id,
        "release_evidence_id": release_id,
        "release_evidence_ready": release_evidence_ready,
        "validation_harness": validation_harness,
        "evidence_state": check_states,
        "evidence_run_id": evidence_run_id,
        "include_vivado_probe": include_vivado_probe,
        "probe_timeout_s": probe_timeout_s,
        "checks": checks,
        "commands": commands,
        "hardware_validation": {
            "status": "NOT_VALIDATED",
            "validated": False,
            "message": "Clean-install smoke does not validate real FPGA hardware, JTAG, programming, ILA, or VIO.",
        },
    }


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    commands: list[dict[str, Any]],
    timeout_s: int,
    clean_pythonpath: bool = False,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("SystemRoot", r"C:\WINDOWS")
    env.setdefault("WINDIR", r"C:\WINDOWS")
    if clean_pythonpath:
        env.pop("PYTHONPATH", None)
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        result = {
            "command": command,
            "cwd": str(cwd),
            "returncode": -1,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_tail": _tail(stdout),
            "stderr_tail": _tail(stderr or f"TimeoutExpired after {timeout_s}s"),
            "clean_pythonpath": clean_pythonpath,
            "timeout_s": timeout_s,
            "timed_out": True,
        }
        commands.append(
            {
                "command": command,
                "cwd": str(cwd),
                "returncode": -1,
                "stdout_tail": result["stdout_tail"],
                "stderr_tail": result["stderr_tail"],
                "clean_pythonpath": clean_pythonpath,
                "timeout_s": timeout_s,
                "timed_out": True,
            }
        )
        return result
    result = {
        "command": command,
        "cwd": str(cwd),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
        "clean_pythonpath": clean_pythonpath,
    }
    commands.append(
        {
            "command": command,
            "cwd": str(cwd),
            "returncode": completed.returncode,
            "stdout_tail": result["stdout_tail"],
            "stderr_tail": result["stderr_tail"],
            "clean_pythonpath": clean_pythonpath,
        }
    )
    return result


def _find_project_wheel(wheelhouse: Path) -> Path | None:
    wheels = sorted(wheelhouse.glob("vivado_agent_mcp-*.whl"))
    return wheels[0] if len(wheels) == 1 else None


def _read_wheel_identity(wheel_path: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel_path) as archive:
        metadata_paths = []
        for name in archive.namelist():
            parts = name.split("/")
            if len(parts) == 2 and parts[0].endswith(".dist-info") and parts[1] == "METADATA":
                metadata_paths.append(name)
        if len(metadata_paths) != 1:
            raise ValueError(f"expected exactly one wheel METADATA entry, found {len(metadata_paths)}")
        metadata_path = metadata_paths[0]
        info = archive.getinfo(metadata_path)
        if info.file_size > 1024 * 1024:
            raise ValueError("wheel METADATA exceeds 1 MiB")
        metadata = BytesParser().parsebytes(archive.read(info))
    name = re.sub(r"[-_.]+", "-", str(metadata.get("Name", "")).strip()).lower()
    version = str(metadata.get("Version", "")).strip()
    if not name or not version:
        raise ValueError("wheel METADATA is missing Name or Version")
    return {"name": name, "version": version, "metadata_path": metadata_path}


def _write_hashed_wheel_requirements(wheelhouse: Path, requirements_path: Path) -> tuple[Path, int]:
    wheels = sorted(path.resolve() for path in wheelhouse.glob("*.whl") if path.is_file())
    if not wheels:
        raise ValueError("wheelhouse contains no installable wheels")
    lines = [f"{path.as_uri()} --hash=sha256:{sha256_file(path)}" for path in wheels]
    requirements_path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return requirements_path, len(wheels)


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _console_script(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "vivado-agent-mcp.exe"
    return venv_dir / "bin" / "vivado-agent-mcp"


def _parse_json(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _add_check(checks: list[dict[str, Any]], check_id: str, status: str, message: str, data: dict[str, Any] | None = None) -> None:
    checks.append({"id": check_id, "status": status, "message": message, "data": data or {}})


def _overall_status(checks: list[dict[str, Any]]) -> str:
    statuses = {check["status"] for check in checks}
    if "BLOCK" in statuses:
        return "BLOCK"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"


def _summary(status: str) -> str:
    if status == "PASS":
        return "Wheel clean-install smoke passed; console script, help/version, doctor, and selftest work without source PYTHONPATH."
    if status == "WARN":
        return "Wheel clean-install smoke is usable with reviewable warnings."
    return "Wheel clean-install smoke is blocked; do not hand this package to beta users yet."


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:] if len(text) > limit else text


if __name__ == "__main__":
    raise SystemExit(main())
