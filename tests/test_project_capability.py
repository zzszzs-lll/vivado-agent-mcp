from pathlib import Path

import pytest

from vivado_agent_mcp.vivado.managed_path import ManagedPathError
from vivado_agent_mcp.vivado.project_capability import (
    create_project_capability,
    rebind_project_capability_generation,
    refresh_project_capability,
    verify_project_capability,
)


def _create_project(root: Path) -> Path:
    root.mkdir(parents=True)
    project = root / "demo.xpr"
    project.write_text("# project identity v1\n", encoding="utf-8")
    return project


def test_project_capability_binds_root_project_marker_and_session_generation(tmp_path: Path) -> None:
    project = _create_project(tmp_path / "project")

    capability = create_project_capability(project, generation_id="generation-a")
    verified = verify_project_capability(capability, project, generation_id="generation-a")

    assert verified["project_path"] == str(project)
    assert len(verified["project_file_sha256"]) == 64
    assert len(verified["marker_sha256"]) == 64
    with pytest.raises(ManagedPathError, match="different Vivado session generation"):
        verify_project_capability(capability, project, generation_id="generation-b")


def test_project_capability_rejects_same_path_project_file_replacement(tmp_path: Path) -> None:
    project = _create_project(tmp_path / "project")
    capability = create_project_capability(project, generation_id="generation-a")

    project.unlink()
    project.write_text("# replacement project\n", encoding="utf-8")

    with pytest.raises(ManagedPathError, match="project_file_object_identity changed|project_file_sha256 changed"):
        verify_project_capability(capability, project, generation_id="generation-a")


def test_project_capability_allows_content_refresh_but_never_file_object_replacement(tmp_path: Path) -> None:
    project = _create_project(tmp_path / "project")
    capability = create_project_capability(project, generation_id="generation-a")

    project.write_text("# Vivado updated project content\n", encoding="utf-8")
    verified = verify_project_capability(
        capability,
        project,
        generation_id="generation-a",
        verify_project_content=False,
    )
    refreshed = refresh_project_capability(capability, generation_id="generation-a")

    assert verified["project_file_object_identity"] == capability["project_file_object_identity"]
    assert refreshed["project_file_sha256"] != capability["project_file_sha256"]

    project.unlink()
    project.write_text("# same path, different object\n", encoding="utf-8")
    with pytest.raises(ManagedPathError, match="project_file_object_identity changed"):
        verify_project_capability(
            refreshed,
            project,
            generation_id="generation-a",
            verify_project_content=False,
        )


def test_project_capability_rebinds_same_object_to_new_session_generation(tmp_path: Path) -> None:
    project = _create_project(tmp_path / "project")
    capability = create_project_capability(project, generation_id="generation-a")
    original_hash = capability["project_file_sha256"]
    project.write_text("# Vivado updated project before restart\n", encoding="utf-8")
    capability = refresh_project_capability(capability, generation_id="generation-a")

    rebound = rebind_project_capability_generation(capability, generation_id="generation-b")
    verified = verify_project_capability(rebound, project, generation_id="generation-b")

    assert rebound["generation_id"] == "generation-b"
    assert rebound["marker_generation_id"] == "generation-a"
    assert rebound["project_file_object_identity"] == capability["project_file_object_identity"]
    assert capability["project_file_sha256"] != original_hash
    assert rebound["project_file_sha256"] == capability["project_file_sha256"]
    assert verified["project_file_sha256"] == rebound["project_file_sha256"]


def test_project_capability_rebind_rejects_unrecorded_in_place_content_change(tmp_path: Path) -> None:
    project = _create_project(tmp_path / "project")
    capability = create_project_capability(project, generation_id="generation-a")
    project.write_text("# externally changed while session stopped\n", encoding="utf-8")

    with pytest.raises(ManagedPathError, match="project_file_sha256 changed"):
        rebind_project_capability_generation(capability, generation_id="generation-b")


def test_project_capability_rejects_same_path_root_replacement(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = _create_project(root)
    capability = create_project_capability(project, generation_id="generation-a")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "demo.xpr").write_text("# replacement project\n", encoding="utf-8")
    (replacement / ".vivado-agent-mcp-project.json").write_bytes(
        (root / ".vivado-agent-mcp-project.json").read_bytes()
    )
    moved = tmp_path / "original-root"
    root.rename(moved)
    replacement.rename(root)

    with pytest.raises(ManagedPathError, match="root_identity changed"):
        verify_project_capability(capability, root / "demo.xpr", generation_id="generation-a")
