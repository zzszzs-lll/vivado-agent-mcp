from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Any


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize_immutable_git_snapshot(
    workspace: str | Path,
    destination: str | Path,
    *,
    package_root: str = "src/vivado_agent_mcp",
) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    snapshot_root = Path(destination).expanduser().resolve()
    source = source_identity(root)
    if source.get("available") is not True or source.get("clean") is not True:
        return {
            "ok": False,
            "reason_code": "source_identity_not_clean",
            "reason": "immutable build snapshots require a clean Git top-level source identity",
            "source_identity": source,
            "snapshot_root": str(snapshot_root),
        }
    contamination = package_root_untracked_files(root, package_root=package_root)
    if contamination["paths"]:
        return {
            "ok": False,
            "reason_code": "package_root_contaminated",
            "reason": "package root contains ignored or untracked files outside the declared Git tree",
            "source_identity": source,
            "snapshot_root": str(snapshot_root),
            "package_root_contamination": contamination,
        }
    if snapshot_root.exists() and not snapshot_root.is_dir():
        return {
            "ok": False,
            "reason_code": "snapshot_destination_not_directory",
            "reason": "immutable snapshot destination must be a directory path",
            "source_identity": source,
            "snapshot_root": str(snapshot_root),
        }
    if snapshot_root.exists() and any(snapshot_root.iterdir()):
        return {
            "ok": False,
            "reason_code": "snapshot_destination_not_empty",
            "reason": "immutable snapshot destination must be absent or empty",
            "source_identity": source,
            "snapshot_root": str(snapshot_root),
        }
    snapshot_root.mkdir(parents=True, exist_ok=True)
    archive = _git(root, "archive", "--format=tar", "HEAD")
    if archive is None:
        return {
            "ok": False,
            "reason_code": "git_archive_failed",
            "reason": "git archive could not materialize HEAD",
            "source_identity": source,
            "snapshot_root": str(snapshot_root),
        }
    try:
        _extract_safe_tar_bytes(archive, snapshot_root)
    except (OSError, tarfile.TarError, ValueError) as exc:
        shutil.rmtree(snapshot_root, ignore_errors=True)
        return {
            "ok": False,
            "reason_code": "git_archive_extract_failed",
            "reason": str(exc),
            "source_identity": source,
            "snapshot_root": str(snapshot_root),
        }
    package_manifest = package_member_manifest(snapshot_root / package_root, relative_to=snapshot_root / "src")
    if not package_manifest["members"]:
        shutil.rmtree(snapshot_root, ignore_errors=True)
        return {
            "ok": False,
            "reason_code": "snapshot_package_empty",
            "reason": "immutable Git snapshot does not contain the Python package root",
            "source_identity": source,
            "snapshot_root": str(snapshot_root),
        }
    archive_sha256 = hashlib.sha256(archive).hexdigest()
    return {
        "ok": True,
        "snapshot_type": "git_archive_head",
        "snapshot_root": str(snapshot_root),
        "archive_sha256": archive_sha256,
        "source_identity": source,
        "package_root_contamination": contamination,
        "package_manifest": package_manifest,
    }


def package_root_untracked_files(
    workspace: str | Path,
    *,
    package_root: str = "src/vivado_agent_mcp",
) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    ordinary = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z", "--", package_root)
    ignored = _git_bytes(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--", package_root)
    if ordinary is None or ignored is None:
        return {"status": "BLOCK", "paths": [], "ordinary": [], "ignored": [], "reason": "Git package-root inventory failed"}
    ordinary_paths = _decode_git_paths(ordinary)
    ignored_candidates = _decode_git_paths(ignored)
    ignored_derived_artifacts = [path for path in ignored_candidates if _is_derived_python_cache(path)]
    ignored_paths = [path for path in ignored_candidates if path not in ignored_derived_artifacts]
    return {
        "status": "READY" if not ordinary_paths and not ignored_paths else "BLOCK",
        "paths": sorted(set(ordinary_paths + ignored_paths)),
        "ordinary": ordinary_paths,
        "ignored": ignored_paths,
        "ignored_derived_artifacts": ignored_derived_artifacts,
        "reason": "" if not ordinary_paths and not ignored_paths else "package root contains files outside the Git tree",
    }


def package_member_manifest(package_root: str | Path, *, relative_to: str | Path) -> dict[str, Any]:
    root = Path(package_root).resolve()
    base = Path(relative_to).resolve()
    members: list[dict[str, Any]] = []
    if root.is_dir():
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(base).as_posix()
            if _is_derived_python_cache(relative):
                continue
            members.append({"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)})
    return _manifest_payload(members)


def wheel_package_member_manifest(wheel_path: str | Path, *, package_prefix: str = "vivado_agent_mcp/") -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    with zipfile.ZipFile(Path(wheel_path)) as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            if info.is_dir() or not info.filename.startswith(package_prefix):
                continue
            payload = archive.read(info)
            members.append(
                {
                    "path": info.filename,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
    return _manifest_payload(members)


def compare_package_member_manifests(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    expected_by_path = {str(item["path"]): item for item in expected.get("members", []) if isinstance(item, dict)}
    actual_by_path = {str(item["path"]): item for item in actual.get("members", []) if isinstance(item, dict)}
    missing = sorted(expected_by_path.keys() - actual_by_path.keys())
    extra = sorted(actual_by_path.keys() - expected_by_path.keys())
    changed = [
        {
            "path": path,
            "expected": expected_by_path[path],
            "actual": actual_by_path[path],
        }
        for path in sorted(expected_by_path.keys() & actual_by_path.keys())
        if expected_by_path[path].get("size") != actual_by_path[path].get("size")
        or expected_by_path[path].get("sha256") != actual_by_path[path].get("sha256")
    ]
    matches = not missing and not extra and not changed and bool(expected_by_path)
    return {
        "matches": matches,
        "expected_digest": str(expected.get("digest", "")),
        "actual_digest": str(actual.get("digest", "")),
        "missing": missing,
        "extra": extra,
        "changed": changed,
    }


def _is_derived_python_cache(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return "/__pycache__/" in f"/{normalized}" and Path(normalized).suffix.lower() in {".pyc", ".pyo"}


def source_identity(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    git_toplevel_text = _git_text(root, "rev-parse", "--show-toplevel")
    if git_toplevel_text is None:
        return _unavailable_identity(
            root,
            reason_code="not_git_worktree",
            reason="workspace is not a readable Git worktree",
        )
    git_toplevel = Path(git_toplevel_text).expanduser().resolve()
    if root != git_toplevel:
        return _unavailable_identity(
            root,
            git_toplevel=git_toplevel,
            reason_code="workspace_not_git_top_level",
            reason="source identity requires the requested workspace to be the Git worktree top-level",
        )

    commit = _git_text(root, "rev-parse", "HEAD")
    tree = _git_text(root, "rev-parse", "HEAD^{tree}")
    status = _git_text(root, "status", "--porcelain=v1", "--untracked-files=all")
    tracked = _git_bytes(root, "ls-files", "-z")
    if commit is None or tree is None or status is None or tracked is None:
        return _unavailable_identity(
            root,
            git_toplevel=git_toplevel,
            reason_code="git_identity_unreadable",
            reason="workspace Git commit, tree, status, or tracked files could not be read",
        )

    tracked_paths = [item for item in tracked.split(b"\0") if item]
    if not tracked_paths:
        return _unavailable_identity(
            root,
            git_toplevel=git_toplevel,
            reason_code="no_tracked_files",
            reason="Git worktree has no tracked files to bind to source identity",
        )
    digest = hashlib.sha256()
    for raw_path in sorted(tracked_paths):
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        path = root / relative
        digest.update(raw_path)
        digest.update(b"\0")
        if path.is_file():
            digest.update(sha256_file(path).encode("ascii"))
        else:
            digest.update(b"<missing>")
        digest.update(b"\0")
    tracked_digest = digest.hexdigest()
    clean = not status.strip()
    identity_id = source_identity_id(commit=commit, tree=tree, tracked_digest=tracked_digest)
    return {
        "available": True,
        "clean": clean,
        "workspace": str(root),
        "git_toplevel": str(git_toplevel),
        "commit": commit,
        "tree": tree,
        "tracked_digest": tracked_digest,
        "identity_id": identity_id,
        "tracked_file_count": len(tracked_paths),
        "dirty_entry_count": len([line for line in status.splitlines() if line.strip()]),
        "reason_code": "" if clean else "git_worktree_dirty",
        "reason": "" if clean else "Git worktree has staged, unstaged, or untracked changes",
    }


def _unavailable_identity(
    root: Path,
    *,
    reason_code: str,
    reason: str,
    git_toplevel: Path | None = None,
) -> dict[str, Any]:
    return {
        "available": False,
        "clean": False,
        "workspace": str(root),
        "git_toplevel": str(git_toplevel) if git_toplevel else "",
        "commit": "",
        "tree": "",
        "tracked_digest": "",
        "identity_id": "",
        "tracked_file_count": 0,
        "dirty_entry_count": 0,
        "reason_code": reason_code,
        "reason": reason,
    }


def source_wheel_pair_id(
    *,
    source: dict[str, Any],
    wheel_sha256: str,
    package_version: str,
    source_wheel_provenance_verified: bool,
) -> str:
    identity_id = str(source.get("identity_id", ""))
    if (
        not source_wheel_provenance_verified
        or source.get("clean") is not True
        or not identity_id
        or not wheel_sha256
        or not package_version
    ):
        return ""
    payload = {
        "source_identity_id": identity_id,
        "wheel_sha256": wheel_sha256,
        "package_version": package_version,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def release_evidence_id(
    *,
    source: dict[str, Any],
    wheel_sha256: str,
    package_version: str,
    source_wheel_provenance_verified: bool = False,
) -> str:
    return source_wheel_pair_id(
        source=source,
        wheel_sha256=wheel_sha256,
        package_version=package_version,
        source_wheel_provenance_verified=source_wheel_provenance_verified,
    )


def source_identity_id(*, commit: str, tree: str, tracked_digest: str) -> str:
    if not commit or not tree or not tracked_digest:
        return ""
    payload = {
        "commit": commit,
        "tree": tree,
        "tracked_digest": tracked_digest,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _git_text(root: Path, *args: str) -> str | None:
    output = _git(root, *args)
    if output is None:
        return None
    return output.decode("utf-8", errors="replace").strip()


def _git_bytes(root: Path, *args: str) -> bytes | None:
    return _git(root, *args)


def _git(root: Path, *args: str) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _extract_safe_tar_bytes(payload: bytes, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = archive.getmembers()
        for member in members:
            if not (member.isdir() or member.isreg()):
                raise ValueError(f"git archive contains unsupported member type: {member.name}")
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"git archive member escapes snapshot root: {member.name}")
            target = (destination_resolved / member_path).resolve()
            try:
                target.relative_to(destination_resolved)
            except ValueError as exc:
                raise ValueError(f"git archive member escapes snapshot root: {member.name}") from exc
        for member in members:
            target = destination_resolved / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"git archive member could not be read: {member.name}")
            with source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)


def _decode_git_paths(payload: bytes) -> list[str]:
    return sorted(
        item.decode("utf-8", errors="surrogateescape")
        for item in payload.split(b"\0")
        if item
    )


def _manifest_payload(members: list[dict[str, Any]]) -> dict[str, Any]:
    canonical_members = sorted(members, key=lambda item: str(item["path"]))
    digest = hashlib.sha256(
        json.dumps(canonical_members, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()
    return {
        "schema_version": 1,
        "member_count": len(canonical_members),
        "digest": digest,
        "members": canonical_members,
    }
