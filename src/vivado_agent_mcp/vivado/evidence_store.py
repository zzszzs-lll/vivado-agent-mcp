from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .managed_path import ensure_managed_directory, file_identity, is_reparse_point, read_stable_file


@dataclass(frozen=True)
class EvidenceSnapshot:
    path: Path
    content: bytes
    sha256: str
    size: int
    file_id: str
    mtime_ns: int


@dataclass(frozen=True)
class StagedEvidenceFile:
    path: Path
    sha256: str
    size: int
    file_id: str
    mtime_ns: int


def load_json_evidence(
    path: str | Path,
    *,
    root: str | Path,
    max_bytes: int,
) -> tuple[Any, EvidenceSnapshot]:
    snapshot = load_evidence_snapshot(path, root=root, max_bytes=max_bytes)
    return json.loads(snapshot.content.decode("utf-8")), snapshot


def load_evidence_snapshot(
    path: str | Path,
    *,
    root: str | Path,
    max_bytes: int,
) -> EvidenceSnapshot:
    candidate = Path(path)
    content, identity = read_stable_file(candidate, root=root, max_bytes=max_bytes)
    return EvidenceSnapshot(
        path=candidate,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        file_id=f"{identity[0]}:{identity[1]}",
        mtime_ns=identity[4],
    )


def verify_evidence_reference(
    reference: dict[str, Any],
    *,
    root: str | Path,
    max_bytes: int,
) -> EvidenceSnapshot:
    snapshot = load_evidence_snapshot(str(reference.get("path", "")), root=root, max_bytes=max_bytes)
    expected = (
        str(reference.get("sha256", "")).lower(),
        reference.get("size"),
        str(reference.get("file_id", "")),
        reference.get("mtime_ns"),
    )
    actual = (snapshot.sha256, snapshot.size, snapshot.file_id, snapshot.mtime_ns)
    if actual != expected:
        raise ValueError("DIAGNOSTIC_OBJECT_IDENTITY_CHANGED: evidence bytes or file identity changed after validation")
    return snapshot


def stage_verified_file(
    source: str | Path,
    *,
    runtime_root: str | Path,
    expected_sha256: str,
) -> StagedEvidenceFile:
    source_path = Path(source)
    source_before = os.lstat(source_path)
    if not stat.S_ISREG(source_before.st_mode) or is_reparse_point(source_path, source_before):
        raise ValueError(f"evidence source is not a regular non-reparse file: {source_path}")
    stage_dir = ensure_managed_directory(runtime_root, Path(runtime_root) / "programming_staging")
    temp_name = ""
    digest = hashlib.sha256()
    size = 0
    try:
        with source_path.open("rb") as source_handle, tempfile.NamedTemporaryFile(
            prefix="bitstream-",
            suffix=source_path.suffix,
            dir=stage_dir,
            delete=False,
        ) as staged_handle:
            temp_name = staged_handle.name
            opened = os.fstat(source_handle.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size, opened.st_mtime_ns) != (
                source_before.st_dev,
                source_before.st_ino,
                source_before.st_mode,
                source_before.st_size,
                source_before.st_mtime_ns,
            ):
                raise ValueError(f"evidence source changed before staging: {source_path}")
            while chunk := source_handle.read(1024 * 1024):
                staged_handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            staged_handle.flush()
            os.fsync(staged_handle.fileno())
        source_after = os.lstat(source_path)
        if (
            source_after.st_dev != source_before.st_dev
            or source_after.st_ino != source_before.st_ino
            or source_after.st_size != source_before.st_size
            or source_after.st_mtime_ns != source_before.st_mtime_ns
        ):
            raise ValueError(f"evidence source changed during staging: {source_path}")
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256.lower():
            raise ValueError("staged evidence SHA256 does not match expected_sha256")
        staged_path = Path(temp_name)
        temp_name = ""
        identity = file_identity(staged_path)
        return StagedEvidenceFile(
            path=staged_path,
            sha256=actual_sha256,
            size=size,
            file_id=f"{identity[0]}:{identity[1]}",
            mtime_ns=identity[4],
        )
    finally:
        if temp_name and os.path.lexists(temp_name):
            os.unlink(temp_name)


def verify_staged_file(staged: StagedEvidenceFile) -> None:
    details = os.lstat(staged.path)
    if is_reparse_point(staged.path, details) or not stat.S_ISREG(details.st_mode):
        raise ValueError("staged evidence is no longer a regular file")
    if (
        f"{details.st_dev}:{details.st_ino}" != staged.file_id
        or details.st_size != staged.size
        or details.st_mtime_ns != staged.mtime_ns
    ):
        raise ValueError("staged evidence identity changed before use")
    digest = hashlib.sha256()
    with staged.path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != staged.sha256:
        raise ValueError("staged evidence SHA256 changed before use")
