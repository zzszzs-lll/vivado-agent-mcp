from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


RUNTIME_IDENTITY_FILENAME = ".vivado-agent-mcp-runtime.json"
RUNTIME_IDENTITY_SCHEMA_VERSION = 1


class RuntimeIdentityError(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def ensure_runtime_identity(
    runtime_dir: str | Path,
    *,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(runtime_dir).expanduser().resolve()
    rejection = runtime_root_rejection_reason(root, workspace_root=workspace_root)
    if rejection:
        raise RuntimeIdentityError(rejection, f"Unsafe runtime directory: {root}")

    root.mkdir(parents=True, exist_ok=True)
    existing = inspect_runtime_identity(root)
    if existing["status"] == "READY":
        return existing
    if existing["status"] != "MISSING":
        raise RuntimeIdentityError(
            "runtime_identity_invalid",
            f"Runtime identity marker is invalid: {existing.get('reason', existing['status'])}",
        )

    marker = root / RUNTIME_IDENTITY_FILENAME
    payload = {
        "schema_version": RUNTIME_IDENTITY_SCHEMA_VERSION,
        "runtime_id": uuid.uuid4().hex,
        "runtime_dir": str(root),
        "workspace_root": str(Path(workspace_root or Path.cwd()).resolve()),
        "created_at": datetime.now(UTC).isoformat(),
    }
    try:
        with marker.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        try:
            os.chmod(marker, 0o600)
        except OSError:
            pass
    except FileExistsError:
        existing = inspect_runtime_identity(root)
        if existing["status"] == "READY":
            return existing
        raise RuntimeIdentityError(
            "runtime_identity_invalid",
            f"Runtime identity marker was created concurrently but is invalid: {existing.get('reason', existing['status'])}",
        )
    return inspect_runtime_identity(root)


def inspect_runtime_identity(runtime_dir: str | Path) -> dict[str, Any]:
    root = Path(runtime_dir).expanduser().resolve()
    marker = root / RUNTIME_IDENTITY_FILENAME
    base = {
        "status": "MISSING",
        "runtime_id": "",
        "runtime_dir": str(root),
        "marker_path": str(marker),
        "schema_version": RUNTIME_IDENTITY_SCHEMA_VERSION,
        "created_at": "",
        "workspace_root": "",
        "reason": "runtime_identity_missing",
    }
    if not marker.exists():
        return base
    if marker.is_symlink() or not marker.is_file():
        return {**base, "status": "INVALID", "reason": "runtime_identity_marker_not_regular_file"}
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {**base, "status": "INVALID", "reason": "runtime_identity_marker_unreadable"}
    if not isinstance(payload, dict):
        return {**base, "status": "INVALID", "reason": "runtime_identity_marker_not_object"}
    if payload.get("schema_version") != RUNTIME_IDENTITY_SCHEMA_VERSION:
        return {**base, "status": "INVALID", "reason": "runtime_identity_schema_mismatch"}
    runtime_id = str(payload.get("runtime_id", ""))
    try:
        uuid.UUID(hex=runtime_id)
    except (ValueError, AttributeError):
        return {**base, "status": "INVALID", "reason": "runtime_identity_id_invalid"}
    try:
        recorded_root = Path(str(payload.get("runtime_dir", ""))).expanduser().resolve()
    except (OSError, ValueError):
        return {**base, "status": "INVALID", "reason": "runtime_identity_path_invalid"}
    if recorded_root != root:
        return {**base, "status": "INVALID", "reason": "runtime_identity_path_mismatch"}
    return {
        **base,
        "status": "READY",
        "runtime_id": runtime_id,
        "created_at": str(payload.get("created_at", "")),
        "workspace_root": str(payload.get("workspace_root", "")),
        "reason": "",
    }


def runtime_root_rejection_reason(
    runtime_dir: str | Path,
    *,
    workspace_root: str | Path | None = None,
) -> str:
    root = Path(runtime_dir).expanduser().resolve()
    if root == Path(root.anchor):
        return "runtime_dir_is_filesystem_root"

    protected = {
        Path.cwd().resolve(),
        Path.home().resolve(),
    }
    if workspace_root is not None:
        protected.add(Path(workspace_root).expanduser().resolve())
    for candidate in protected:
        if root == candidate:
            return "runtime_dir_is_protected_root"
        if _is_ancestor(root, candidate):
            return "runtime_dir_contains_protected_root"

    if (root / ".git").exists():
        return "runtime_dir_looks_like_repository"
    if (root / "pyproject.toml").is_file() and (root / "src").is_dir():
        return "runtime_dir_looks_like_repository"
    return ""


def _is_ancestor(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return parent != child
