from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
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
    sha256_file,
    wheel_package_member_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build wheel and sdist from an immutable Git HEAD snapshot and emit source provenance.",
    )
    parser.add_argument("--workspace", default=str(_WORKSPACE), help="Git repository top-level.")
    parser.add_argument("--output-dir", default="dist", help="Distribution output directory.")
    parser.add_argument("--work-dir", default="test_use/distribution_build", help="Ignored temporary build directory.")
    parser.add_argument("--python", default=sys.executable, help="Python executable used for python -m build.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON report.")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    output_dir = _resolve_under(workspace, args.output_dir)
    work_dir = _resolve_under(workspace, args.work_dir)
    report = build_distributions(
        workspace=workspace,
        output_dir=output_dir,
        work_dir=work_dir,
        python_exe=Path(args.python).expanduser().resolve(),
    )
    report_path = output_dir / "source-provenance.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    report["manifest_path"] = str(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"distribution build: {report['status']}")
        print(report["summary"])
        print(f"manifest_path={report_path}")
    return 0 if report["status"] == "PASS" else 2


def build_distributions(
    *,
    workspace: Path,
    output_dir: Path,
    work_dir: Path,
    python_exe: Path,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    output_dir = output_dir.resolve()
    work_dir = work_dir.resolve()
    if not _strict_descendant(workspace, output_dir) or not _strict_descendant(workspace, work_dir):
        return _blocked(
            "output_dir and work_dir must be dedicated strict descendants of workspace",
            workspace,
            output_dir,
            work_dir,
        )
    if output_dir == work_dir or _strict_descendant(output_dir, work_dir) or _strict_descendant(work_dir, output_dir):
        return _blocked(
            "output_dir and work_dir must not overlap",
            workspace,
            output_dir,
            work_dir,
        )
    if output_dir.exists():
        for path in output_dir.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    snapshot = materialize_immutable_git_snapshot(workspace, work_dir / "source_snapshot")
    if snapshot.get("ok") is not True:
        return {
            **_base_report(workspace, output_dir, work_dir),
            "status": "BLOCK",
            "summary": "Immutable Git source snapshot could not be established.",
            "source_snapshot": snapshot,
        }
    snapshot_root = Path(str(snapshot["snapshot_root"]))
    command = [str(python_exe), "-m", "build", "--no-isolation", "--outdir", str(output_dir), str(snapshot_root)]
    completed = subprocess.run(
        command,
        cwd=str(snapshot_root),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if completed.returncode != 0:
        return {
            **_base_report(workspace, output_dir, work_dir),
            "status": "BLOCK",
            "summary": "Distribution build failed.",
            "source_snapshot": _snapshot_summary(snapshot),
            "build": {
                "command": command,
                "returncode": completed.returncode,
                "stdout_tail": _tail(completed.stdout),
                "stderr_tail": _tail(completed.stderr),
            },
        }
    wheels = sorted(output_dir.glob("vivado_agent_mcp-*.whl"))
    sdists = sorted(output_dir.glob("vivado_agent_mcp-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        return {
            **_base_report(workspace, output_dir, work_dir),
            "status": "BLOCK",
            "summary": "Expected exactly one vivado-agent-mcp wheel and one sdist.",
            "source_snapshot": _snapshot_summary(snapshot),
            "wheel_paths": [str(path) for path in wheels],
            "sdist_paths": [str(path) for path in sdists],
        }
    wheel = wheels[0]
    sdist = sdists[0]
    wheel_manifest = wheel_package_member_manifest(wheel)
    comparison = compare_package_member_manifests(snapshot["package_manifest"], wheel_manifest)
    status = "PASS" if comparison["matches"] else "BLOCK"
    return {
        **_base_report(workspace, output_dir, work_dir),
        "status": status,
        "summary": (
            "Wheel and sdist were built from immutable Git HEAD; wheel package bytes match the snapshot."
            if status == "PASS"
            else "Wheel package bytes differ from the immutable Git snapshot."
        ),
        "package": {"name": "vivado-agent-mcp", "version": __version__},
        "source_snapshot": _snapshot_summary(snapshot),
        "source_identity": snapshot["source_identity"],
        "source_package_manifest": snapshot["package_manifest"],
        "wheel_package_manifest": wheel_manifest,
        "package_member_comparison": comparison,
        "wheel": {"path": str(wheel), "name": wheel.name, "sha256": sha256_file(wheel)},
        "sdist": {"path": str(sdist), "name": sdist.name, "sha256": sha256_file(sdist)},
        "source_wheel_provenance_verified": status == "PASS",
        "hardware_validation": {
            "status": "NOT_VALIDATED",
            "validated": False,
            "message": "Distribution construction does not validate FPGA hardware.",
        },
        "build": {
            "command": command,
            "returncode": completed.returncode,
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
        },
    }


def _base_report(workspace: Path, output_dir: Path, work_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution_mode": "immutable_git_snapshot_distribution_build",
        "workspace": str(workspace),
        "output_dir": str(output_dir),
        "work_dir": str(work_dir),
    }


def _snapshot_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    package_manifest = snapshot.get("package_manifest", {})
    return {
        "ok": snapshot.get("ok") is True,
        "snapshot_type": str(snapshot.get("snapshot_type", "")),
        "archive_sha256": str(snapshot.get("archive_sha256", "")),
        "source_identity": snapshot.get("source_identity", {}),
        "package_manifest": {
            "schema_version": package_manifest.get("schema_version"),
            "member_count": package_manifest.get("member_count"),
            "digest": package_manifest.get("digest"),
        },
    }


def _blocked(reason: str, workspace: Path, output_dir: Path, work_dir: Path) -> dict[str, Any]:
    return {
        **_base_report(workspace, output_dir, work_dir),
        "status": "BLOCK",
        "summary": reason,
    }


def _resolve_under(workspace: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def _strict_descendant(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return bool(relative.parts)


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:]


if __name__ == "__main__":
    raise SystemExit(main())
