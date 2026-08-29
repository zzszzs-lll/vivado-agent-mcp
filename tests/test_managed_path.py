import os
from pathlib import Path

import pytest

import vivado_agent_mcp.vivado.managed_path as managed_path
from vivado_agent_mcp.vivado.managed_path import (
    ManagedPathError,
    atomic_write_bytes,
    delete_managed_snapshot,
    hold_managed_paths_stable,
    snapshot_managed_tree,
)


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound mutation contract")


def test_windows_managed_write_and_delete_use_handle_bound_mutations(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "managed" / "evidence.json"
    atomic_write_bytes(tmp_path, target, b"first")
    atomic_write_bytes(tmp_path, target, b"second")
    snapshot = snapshot_managed_tree(tmp_path, target.parent)

    monkeypatch.setattr(os, "replace", lambda *_args, **_kwargs: pytest.fail("os.replace must not be used on Windows"))
    monkeypatch.setattr(os, "unlink", lambda *_args, **_kwargs: pytest.fail("os.unlink must not be used on Windows"))

    assert target.read_bytes() == b"second"
    deleted = delete_managed_snapshot(tmp_path, target.parent, snapshot)

    assert deleted == {"file_count": 1, "dir_count": 1, "bytes": 6}
    assert not target.parent.exists()


def test_windows_handle_bound_delete_rejects_identity_drift(tmp_path: Path) -> None:
    target = tmp_path / "managed" / "evidence.json"
    target.parent.mkdir()
    target.write_bytes(b"first")
    snapshot = snapshot_managed_tree(tmp_path, target.parent)
    target.write_bytes(b"changed")

    with pytest.raises(ManagedPathError, match="changed after planning"):
        delete_managed_snapshot(tmp_path, target.parent, snapshot)

    assert target.exists()


def test_windows_managed_write_rejects_hard_link_without_modifying_external_content(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    managed = tmp_path / "managed" / "evidence.json"
    outside.write_bytes(b"outside-original")
    managed.parent.mkdir()
    os.link(outside, managed)

    with pytest.raises(ManagedPathError, match="multiple hard links"):
        atomic_write_bytes(tmp_path / "managed", managed, b"replacement")

    assert outside.read_bytes() == b"outside-original"
    assert managed.read_bytes() == b"outside-original"


def test_windows_stability_lock_pins_authorized_sources_without_mutating_directory_acl(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "sim" / "tb.sv"
    late_include = source.parent / "late_include.svh"
    source.parent.mkdir(parents=True)
    source.write_text("module tb; endmodule\n", encoding="utf-8")

    with hold_managed_paths_stable(tmp_path, files=[source], directories=[project, source.parent]) as evidence:
        assert evidence == {"file_count": 1, "writable_file_count": 0, "directory_count": 2}
        with pytest.raises(PermissionError):
            source.write_text("module changed; endmodule\n", encoding="utf-8")
        with pytest.raises(PermissionError):
            source.unlink()
        with pytest.raises(PermissionError):
            source.parent.rename(project / "renamed_sim")
        late_include.write_text("`define LATE 1\n", encoding="utf-8")

    source.write_text("module changed; endmodule\n", encoding="utf-8")
    assert "changed" in source.read_text(encoding="utf-8")
    assert late_include.exists()


def test_windows_writable_stability_lock_allows_in_place_tool_update_but_pins_object(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project_file = project / "demo.xpr"
    project_file.write_text("before", encoding="utf-8")

    with hold_managed_paths_stable(
        tmp_path,
        files=[],
        directories=[project],
        writable_files=[project_file],
    ) as evidence:
        assert evidence == {"file_count": 0, "writable_file_count": 1, "directory_count": 1}
        project_file.write_text("after", encoding="utf-8")
        with pytest.raises(PermissionError):
            project_file.unlink()
        with pytest.raises(PermissionError):
            project_file.rename(project / "replacement.xpr")

    assert project_file.read_text(encoding="utf-8") == "after"


def test_windows_atomic_write_does_not_overwrite_intruder_after_quarantine(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "managed" / "evidence.json"
    target.parent.mkdir()
    target.write_bytes(b"old")
    original_rename = managed_path._windows_rename_handle

    def rename_and_inject(handle: int, parent_handle: int, source_path: Path, leaf: str) -> None:
        original_rename(handle, parent_handle, source_path, leaf)
        if source_path == target:
            target.write_bytes(b"intruder")

    monkeypatch.setattr(managed_path, "_windows_rename_handle", rename_and_inject)

    with pytest.raises(ManagedPathError, match="handle-bound rename failed"):
        atomic_write_bytes(tmp_path, target, b"new")

    assert target.read_bytes() == b"intruder"
    quarantines = list(target.parent.glob(".vmcp-*.quarantine"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == b"old"


def test_windows_atomic_write_temp_cannot_be_modified_before_install(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "managed" / "evidence.json"
    original_open = managed_path._windows_open
    original_rename = managed_path._windows_rename_handle
    attempted = False
    temp_open_policy: dict[str, bool] = {}

    def capture_open(path: Path, **kwargs):
        if kwargs.get("creation") == managed_path._CREATE_NEW:
            temp_open_policy["share_write"] = bool(kwargs.get("share_write", True))
            temp_open_policy["share_delete"] = bool(kwargs.get("share_delete", False))
        return original_open(path, **kwargs)

    def rename_after_tamper_attempt(handle: int, parent_handle: int, source_path: Path, leaf: str) -> None:
        nonlocal attempted
        if source_path.suffix == ".tmp":
            attempted = True
            with pytest.raises(PermissionError):
                source_path.write_bytes(b"intruder")
        original_rename(handle, parent_handle, source_path, leaf)

    monkeypatch.setattr(managed_path, "_windows_open", capture_open)
    monkeypatch.setattr(managed_path, "_windows_rename_handle", rename_after_tamper_attempt)

    atomic_write_bytes(tmp_path, target, b"trusted")

    assert attempted is True
    assert temp_open_policy == {"share_write": False, "share_delete": False}
    assert target.read_bytes() == b"trusted"
