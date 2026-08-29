from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import build_distributions
from vivado_agent_mcp.release_identity import package_member_manifest


def test_distribution_builder_uses_snapshot_and_verifies_exact_wheel_members(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    package = tmp_path / "snapshot" / "src" / "vivado_agent_mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = '0.10.0'\n", encoding="utf-8")
    workspace.mkdir()
    output_dir = workspace / "dist"
    work_dir = workspace / "work"
    source_identity = {
        "available": True,
        "clean": True,
        "identity_id": "a" * 64,
        "commit": "b" * 40,
        "tree": "c" * 40,
        "tracked_digest": "d" * 64,
    }
    snapshot_manifest = package_member_manifest(package, relative_to=package.parent)

    monkeypatch.setattr(
        build_distributions,
        "materialize_immutable_git_snapshot",
        lambda workspace, destination: {
            "ok": True,
            "snapshot_type": "git_archive_head",
            "snapshot_root": str(package.parents[1]),
            "archive_sha256": "e" * 64,
            "source_identity": source_identity,
            "package_manifest": snapshot_manifest,
        },
    )

    def fake_run(command, *, cwd, capture_output, text, timeout, check):
        output_dir.mkdir(parents=True, exist_ok=True)
        wheel = output_dir / "vivado_agent_mcp-0.10.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("vivado_agent_mcp/__init__.py", (package / "__init__.py").read_bytes())
        (output_dir / "vivado_agent_mcp-0.10.0.tar.gz").write_bytes(b"sdist")
        return subprocess.CompletedProcess(command, 0, stdout="built", stderr="")

    monkeypatch.setattr(build_distributions.subprocess, "run", fake_run)

    report = build_distributions.build_distributions(
        workspace=workspace,
        output_dir=output_dir,
        work_dir=work_dir,
        python_exe=Path("python"),
    )

    assert report["status"] == "PASS"
    assert report["source_wheel_provenance_verified"] is True
    assert report["package_member_comparison"]["matches"] is True
    assert report["hardware_validation"]["status"] == "NOT_VALIDATED"
    assert command_uses_snapshot(report["build"]["command"], package.parents[1])


def test_distribution_builder_blocks_wheel_member_drift(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "repo"
    package = tmp_path / "snapshot" / "src" / "vivado_agent_mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("expected\n", encoding="utf-8")
    workspace.mkdir()
    output_dir = workspace / "dist"
    snapshot_manifest = package_member_manifest(package, relative_to=package.parent)
    monkeypatch.setattr(
        build_distributions,
        "materialize_immutable_git_snapshot",
        lambda workspace, destination: {
            "ok": True,
            "snapshot_type": "git_archive_head",
            "snapshot_root": str(package.parents[1]),
            "archive_sha256": "e" * 64,
            "source_identity": {"available": True, "clean": True, "identity_id": "a" * 64},
            "package_manifest": snapshot_manifest,
        },
    )

    def fake_run(command, *, cwd, capture_output, text, timeout, check):
        output_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_dir / "vivado_agent_mcp-0.10.0-py3-none-any.whl", "w") as archive:
            archive.writestr("vivado_agent_mcp/__init__.py", b"changed\n")
        (output_dir / "vivado_agent_mcp-0.10.0.tar.gz").write_bytes(b"sdist")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(build_distributions.subprocess, "run", fake_run)

    report = build_distributions.build_distributions(
        workspace=workspace,
        output_dir=output_dir,
        work_dir=workspace / "work",
        python_exe=Path("python"),
    )

    assert report["status"] == "BLOCK"
    assert report["source_wheel_provenance_verified"] is False
    assert report["package_member_comparison"]["changed"]


def command_uses_snapshot(command: list[str], snapshot_root: Path) -> bool:
    return str(snapshot_root) in command and "--outdir" in command


def test_distribution_builder_blocks_cleanup_paths_outside_or_overlapping_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    outside_report = build_distributions.build_distributions(
        workspace=workspace,
        output_dir=outside,
        work_dir=workspace / "work",
        python_exe=Path("python"),
    )
    overlap_report = build_distributions.build_distributions(
        workspace=workspace,
        output_dir=workspace / "build",
        work_dir=workspace / "build" / "work",
        python_exe=Path("python"),
    )

    assert outside_report["status"] == "BLOCK"
    assert "strict descendants" in outside_report["summary"]
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert overlap_report["status"] == "BLOCK"
    assert "must not overlap" in overlap_report["summary"]
