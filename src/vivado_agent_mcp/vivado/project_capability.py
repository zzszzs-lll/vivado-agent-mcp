from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from .managed_path import (
    ManagedPathError,
    directory_identity,
    read_stable_file,
    validate_managed_path,
)


PROJECT_CAPABILITY_MARKER = ".vivado-agent-mcp-project.json"
PROJECT_CAPABILITY_SCHEMA_VERSION = 1
MAX_PROJECT_MARKER_BYTES = 16 * 1024
MAX_PROJECT_FILE_BYTES = 64 * 1024 * 1024


def create_project_capability(project_path: str | Path, *, generation_id: str) -> dict[str, Any]:
    project = Path(os.path.abspath(os.fspath(project_path)))
    root = project.parent
    if not generation_id:
        raise ManagedPathError("project capability requires a session generation id")
    validate_managed_path(root, root)
    validate_managed_path(root, project)
    marker = root / PROJECT_CAPABILITY_MARKER
    if os.path.lexists(marker):
        raise ManagedPathError(f"project capability marker already exists: {marker}")
    payload = {
        "schema_version": PROJECT_CAPABILITY_SCHEMA_VERSION,
        "nonce": uuid.uuid4().hex,
        "project_path": str(project),
        "generation_id": generation_id,
    }
    try:
        with marker.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(marker, 0o600)
        except OSError:
            pass
        return _capture_project_capability(project, generation_id=generation_id, expected_nonce=payload["nonce"])
    except Exception:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def verify_project_capability(
    capability: dict[str, Any],
    project_path: str | Path,
    *,
    generation_id: str,
    verify_project_content: bool = True,
) -> dict[str, Any]:
    project = Path(os.path.abspath(os.fspath(project_path)))
    if _path_key(project) != str(capability.get("project_path_key", "")):
        raise ManagedPathError("project capability path does not match the requested project")
    if not generation_id or generation_id != str(capability.get("generation_id", "")):
        raise ManagedPathError("project capability belongs to a different Vivado session generation")
    current = _capture_project_capability(
        project,
        generation_id=generation_id,
        expected_nonce=str(capability.get("nonce", "")),
        marker_generation_id=str(capability.get("marker_generation_id") or capability.get("generation_id", "")),
    )
    for key in ("root_identity", "marker_identity", "marker_sha256"):
        if current.get(key) != capability.get(key):
            raise ManagedPathError(f"project capability {key} changed")
    current_object_identity = list(current.get("project_file_object_identity", []))
    expected_object_identity = list(capability.get("project_file_object_identity", []))
    if current_object_identity != expected_object_identity:
        raise ManagedPathError("project capability project_file_object_identity changed")
    if verify_project_content and current.get("project_file_sha256") != capability.get("project_file_sha256"):
        raise ManagedPathError("project capability project_file_sha256 changed")
    return current


def refresh_project_capability(capability: dict[str, Any], *, generation_id: str) -> dict[str, Any]:
    project_path = str(capability.get("project_path", ""))
    return verify_project_capability(
        capability,
        project_path,
        generation_id=generation_id,
        verify_project_content=False,
    )


def rebind_project_capability_generation(
    capability: dict[str, Any],
    *,
    generation_id: str,
) -> dict[str, Any]:
    if not generation_id:
        raise ManagedPathError("project capability rebind requires a session generation id")
    previous_generation = str(capability.get("generation_id", ""))
    project_path = str(capability.get("project_path", ""))
    previous = verify_project_capability(
        capability,
        project_path,
        generation_id=previous_generation,
        verify_project_content=True,
    )
    rebound = _capture_project_capability(
        project_path,
        generation_id=generation_id,
        expected_nonce=str(capability.get("nonce", "")),
        marker_generation_id=str(capability.get("marker_generation_id") or previous_generation),
    )
    for key in (
        "root_identity",
        "marker_identity",
        "marker_sha256",
        "project_file_object_identity",
        "project_file_sha256",
    ):
        if rebound.get(key) != previous.get(key):
            raise ManagedPathError(f"project capability {key} changed during session-generation rebind")
    return rebound


def _capture_project_capability(
    project_path: str | Path,
    *,
    generation_id: str,
    expected_nonce: str,
    marker_generation_id: str | None = None,
) -> dict[str, Any]:
    project = Path(os.path.abspath(os.fspath(project_path)))
    root = project.parent
    marker = root / PROJECT_CAPABILITY_MARKER
    validate_managed_path(root, root)
    validate_managed_path(root, project)
    validate_managed_path(root, marker)
    project_content, project_identity = read_stable_file(project, root=root, max_bytes=MAX_PROJECT_FILE_BYTES)
    marker_content, marker_identity = read_stable_file(marker, root=root, max_bytes=MAX_PROJECT_MARKER_BYTES)
    if not stat.S_ISREG(project_identity[2]) or not stat.S_ISREG(marker_identity[2]):
        raise ManagedPathError("project capability requires regular project and marker files")
    try:
        marker_payload = json.loads(marker_content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedPathError("project capability marker is unreadable") from exc
    if not isinstance(marker_payload, dict):
        raise ManagedPathError("project capability marker must be a JSON object")
    if marker_payload.get("schema_version") != PROJECT_CAPABILITY_SCHEMA_VERSION:
        raise ManagedPathError("project capability marker schema does not match")
    nonce = str(marker_payload.get("nonce", ""))
    try:
        uuid.UUID(hex=nonce)
    except (ValueError, AttributeError) as exc:
        raise ManagedPathError("project capability marker nonce is invalid") from exc
    if not expected_nonce or nonce != expected_nonce:
        raise ManagedPathError("project capability marker nonce changed")
    if _path_key(marker_payload.get("project_path", "")) != _path_key(project):
        raise ManagedPathError("project capability marker path does not match")
    expected_marker_generation = marker_generation_id or generation_id
    if str(marker_payload.get("generation_id", "")) != expected_marker_generation:
        raise ManagedPathError("project capability marker generation does not match")
    return {
        "schema_version": PROJECT_CAPABILITY_SCHEMA_VERSION,
        "project_path": str(project),
        "project_path_key": _path_key(project),
        "project_root": str(root),
        "marker_path": str(marker),
        "generation_id": generation_id,
        "marker_generation_id": expected_marker_generation,
        "nonce": nonce,
        "root_identity": list(directory_identity(root)),
        "project_file_identity": list(project_identity),
        "project_file_object_identity": list(project_identity[:3]),
        "project_file_sha256": hashlib.sha256(project_content).hexdigest(),
        "marker_identity": list(marker_identity),
        "marker_sha256": hashlib.sha256(marker_content).hexdigest(),
    }


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))
