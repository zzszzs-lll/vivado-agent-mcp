from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

from vivado_agent_mcp.release_identity import (
    compare_package_member_manifests,
    materialize_immutable_git_snapshot,
    package_root_untracked_files,
    sha256_file,
    source_identity,
    source_wheel_pair_id,
    wheel_package_member_manifest,
)


def test_source_identity_tracks_clean_git_content_and_release_wheel(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = repo / "tracked.txt"
    tracked.write_text("v1\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Vivado MCP Test")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "fixture")

    clean = source_identity(repo)
    wheel = repo / "demo.whl"
    wheel.write_bytes(b"wheel")
    wheel_hash = sha256_file(wheel)
    pair_id = source_wheel_pair_id(
        source=clean,
        wheel_sha256=wheel_hash,
        package_version="0.10.0",
        source_wheel_provenance_verified=True,
    )

    assert clean["available"] is True
    assert clean["clean"] is True
    assert len(clean["commit"]) == 40
    assert len(clean["identity_id"]) == 64
    assert len(pair_id) == 64

    tracked.write_text("v2\n", encoding="utf-8")
    dirty = source_identity(repo)
    assert dirty["clean"] is False
    assert dirty["identity_id"] != clean["identity_id"]


def test_source_wheel_pair_id_is_empty_when_clean_source_provenance_is_unverified() -> None:
    pair_id = source_wheel_pair_id(
        source={"clean": True, "identity_id": "a" * 64},
        wheel_sha256="b" * 64,
        package_version="0.10.0",
        source_wheel_provenance_verified=False,
    )

    assert pair_id == ""


def test_source_wheel_pair_id_is_empty_when_source_is_dirty() -> None:
    pair_id = source_wheel_pair_id(
        source={"clean": False, "identity_id": "a" * 64},
        wheel_sha256="b" * 64,
        package_version="0.10.0",
        source_wheel_provenance_verified=True,
    )

    assert pair_id == ""


def test_source_identity_rejects_nested_workspace_in_outer_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "test_use" / "identity-probe"
    nested.mkdir(parents=True)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Vivado MCP Test")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "fixture")

    identity = source_identity(nested)

    assert identity["available"] is False
    assert identity["reason_code"] == "workspace_not_git_top_level"
    assert identity["workspace"] == str(nested.resolve())
    assert identity["git_toplevel"] == str(repo.resolve())
    assert identity["commit"] == ""


def test_immutable_snapshot_blocks_git_info_excluded_package_file(tmp_path: Path) -> None:
    repo = _package_repo(tmp_path)
    excluded = repo / "src" / "vivado_agent_mcp" / "injected.py"
    (repo / ".git" / "info" / "exclude").write_text("src/vivado_agent_mcp/injected.py\n", encoding="utf-8")
    excluded.write_text("INJECTED = True\n", encoding="utf-8")

    identity = source_identity(repo)
    contamination = package_root_untracked_files(repo)
    snapshot = materialize_immutable_git_snapshot(repo, tmp_path / "snapshot")

    assert identity["clean"] is True
    assert contamination["status"] == "BLOCK"
    assert contamination["ignored"] == ["src/vivado_agent_mcp/injected.py"]
    assert snapshot["ok"] is False
    assert snapshot["reason_code"] == "package_root_contaminated"


def test_immutable_snapshot_rejects_file_destination(tmp_path: Path) -> None:
    repo = _package_repo(tmp_path)
    destination = tmp_path / "snapshot"
    destination.write_text("not a directory", encoding="utf-8")

    snapshot = materialize_immutable_git_snapshot(repo, destination)

    assert snapshot["ok"] is False
    assert snapshot["reason_code"] == "snapshot_destination_not_directory"


def test_immutable_snapshot_ignores_only_derived_python_bytecode_cache(tmp_path: Path) -> None:
    repo = _package_repo(tmp_path)
    cache_dir = repo / "src" / "vivado_agent_mcp" / "__pycache__"
    cache_dir.mkdir()
    bytecode = cache_dir / "__init__.cpython-312.pyc"
    bytecode.write_bytes(b"derived bytecode")
    (repo / ".git" / "info" / "exclude").write_text(
        "src/vivado_agent_mcp/__pycache__/\n",
        encoding="utf-8",
    )

    contamination = package_root_untracked_files(repo)
    snapshot = materialize_immutable_git_snapshot(repo, tmp_path / "snapshot")

    assert contamination["status"] == "READY"
    assert contamination["paths"] == []
    assert contamination["ignored_derived_artifacts"] == [
        "src/vivado_agent_mcp/__pycache__/__init__.cpython-312.pyc"
    ]
    assert snapshot["ok"] is True


def test_immutable_snapshot_materializes_git_head_and_matches_wheel_members(tmp_path: Path) -> None:
    repo = _package_repo(tmp_path)
    snapshot = materialize_immutable_git_snapshot(repo, tmp_path / "snapshot")
    wheel = tmp_path / "vivado_agent_mcp-0.10.0-py3-none-any.whl"
    source_file = repo / "src" / "vivado_agent_mcp" / "__init__.py"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("vivado_agent_mcp/__init__.py", source_file.read_bytes())

    wheel_manifest = wheel_package_member_manifest(wheel)
    comparison = compare_package_member_manifests(snapshot["package_manifest"], wheel_manifest)

    assert snapshot["ok"] is True
    assert snapshot["snapshot_type"] == "git_archive_head"
    assert snapshot["source_identity"]["clean"] is True
    assert snapshot["package_manifest"]["member_count"] == 1
    assert comparison["matches"] is True


def test_package_manifest_comparison_detects_foreign_wheel_member(tmp_path: Path) -> None:
    repo = _package_repo(tmp_path)
    snapshot = materialize_immutable_git_snapshot(repo, tmp_path / "snapshot")
    wheel = tmp_path / "vivado_agent_mcp-0.10.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("vivado_agent_mcp/__init__.py", b"__version__ = '0.10.0'\n")
        archive.writestr("vivado_agent_mcp/injected.py", b"INJECTED = True\n")

    comparison = compare_package_member_manifests(
        snapshot["package_manifest"],
        wheel_package_member_manifest(wheel),
    )

    assert comparison["matches"] is False
    assert comparison["extra"] == ["vivado_agent_mcp/injected.py"]


def _package_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    package = repo / "src" / "vivado_agent_mcp"
    package.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='vivado-agent-mcp'\nversion='0.10.0'\n", encoding="utf-8")
    (package / "__init__.py").write_text("__version__ = '0.10.0'\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Vivado MCP Test")
    _git(repo, "add", "pyproject.toml", "src/vivado_agent_mcp/__init__.py")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
