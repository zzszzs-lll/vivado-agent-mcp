from __future__ import annotations

import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

if os.name == "nt":
    import ctypes
    from ctypes import wintypes


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class ManagedPathError(ValueError):
    pass


def file_identity(path: str | Path) -> tuple[int, int, int, int, int]:
    info = os.lstat(path)
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def directory_identity(path: str | Path) -> tuple[int, int, int]:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or is_reparse_point(path, info):
        raise ManagedPathError(f"managed path is not a regular directory: {path}")
    return (info.st_dev, info.st_ino, info.st_mode)


def is_reparse_point(path: str | Path, info: os.stat_result | None = None) -> bool:
    candidate = Path(path)
    details = info or os.lstat(candidate)
    is_junction = getattr(candidate, "is_junction", None)
    return bool(
        stat.S_ISLNK(details.st_mode)
        or (getattr(details, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)
        or (callable(is_junction) and is_junction())
    )


def validate_managed_path(
    root: str | Path,
    target: str | Path,
    *,
    allow_missing_leaf: bool = False,
) -> Path:
    root_path = Path(os.path.abspath(os.fspath(root)))
    target_path = Path(os.path.abspath(os.fspath(target)))
    try:
        common = os.path.commonpath((root_path, target_path))
    except ValueError as exc:
        raise ManagedPathError(f"path is outside managed root: {target_path}") from exc
    if os.path.normcase(common) != os.path.normcase(str(root_path)):
        raise ManagedPathError(f"path is outside managed root: {target_path}")
    if not root_path.exists() or not root_path.is_dir():
        raise ManagedPathError(f"managed root is not an existing directory: {root_path}")
    root_info = os.lstat(root_path)
    if is_reparse_point(root_path, root_info):
        raise ManagedPathError(f"managed root is a symlink, junction, or reparse point: {root_path}")

    relative = target_path.relative_to(root_path)
    current = root_path
    for index, part in enumerate(relative.parts):
        current /= part
        if not os.path.lexists(current):
            if allow_missing_leaf and index == len(relative.parts) - 1:
                return target_path
            raise ManagedPathError(f"managed path component does not exist: {current}")
        details = os.lstat(current)
        if is_reparse_point(current, details):
            raise ManagedPathError(f"managed path contains a symlink, junction, or reparse point: {current}")
    return target_path


def ensure_managed_directory(root: str | Path, directory: str | Path) -> Path:
    root_path = validate_managed_path(root, root)
    directory_path = Path(os.path.abspath(os.fspath(directory)))
    try:
        relative = directory_path.relative_to(root_path)
    except ValueError as exc:
        raise ManagedPathError(f"directory is outside managed root: {directory_path}") from exc
    current = root_path
    for part in relative.parts:
        current /= part
        if os.path.lexists(current):
            validate_managed_path(root_path, current)
            if not current.is_dir():
                raise ManagedPathError(f"managed directory component is not a directory: {current}")
        else:
            current.mkdir()
            validate_managed_path(root_path, current)
    return directory_path


def read_stable_bytes(path: str | Path, *, root: str | Path, max_bytes: int) -> bytes:
    content, _ = read_stable_file(path, root=root, max_bytes=max_bytes)
    return content


def read_stable_file(
    path: str | Path,
    *,
    root: str | Path,
    max_bytes: int,
) -> tuple[bytes, tuple[int, int, int, int, int]]:
    candidate = validate_managed_path(root, path)
    before = file_identity(candidate)
    if not stat.S_ISREG(before[2]):
        raise ManagedPathError(f"managed path is not a regular file: {candidate}")
    if before[3] > max_bytes:
        raise ManagedPathError(f"managed file exceeds {max_bytes} bytes: {candidate}")
    with candidate.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        opened_identity = (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size, opened.st_mtime_ns)
        if opened_identity != before:
            raise ManagedPathError(f"managed file changed before read: {candidate}")
        content = handle.read(max_bytes + 1)
        after_open = os.fstat(handle.fileno())
    if len(content) > max_bytes:
        raise ManagedPathError(f"managed file exceeds {max_bytes} bytes: {candidate}")
    after = file_identity(candidate)
    after_open_identity = (
        after_open.st_dev,
        after_open.st_ino,
        after_open.st_mode,
        after_open.st_size,
        after_open.st_mtime_ns,
    )
    if before != after or before != after_open_identity:
        raise ManagedPathError(f"managed file changed during read: {candidate}")
    return content, before


@contextmanager
def hold_managed_paths_stable(
    root: str | Path,
    *,
    files: list[str | Path],
    directories: list[str | Path],
    writable_files: list[str | Path] | None = None,
) -> Iterator[dict[str, int]]:
    """Pin executable inputs; writable files keep object identity but permit tool-owned updates."""

    if os.name != "nt":
        raise ManagedPathError("managed source stability locks are currently supported only on Windows")
    root_path = validate_managed_path(root, root)
    file_paths = sorted({validate_managed_path(root_path, path) for path in files}, key=str)
    writable_paths = sorted({validate_managed_path(root_path, path) for path in (writable_files or [])}, key=str)
    if set(file_paths) & set(writable_paths):
        raise ManagedPathError("managed stability-lock targets cannot be both immutable and writable")
    directory_paths = sorted({validate_managed_path(root_path, path) for path in directories}, key=str)
    file_handles: list[tuple[int, Path, tuple[int, int, int, int, int]]] = []
    writable_handles: list[tuple[int, Path, tuple[int, int, int, int, int]]] = []
    directory_handles: list[tuple[int, Path, tuple[int, int, int]]] = []
    member_handles: list[int] = []
    try:
        for path in directory_paths:
            expected = directory_identity(path)
            handle = _windows_open(
                path,
                access=_FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES,
                share_delete=False,
                directory=True,
            )
            _windows_verify_handle(handle, path, (expected[0], expected[1], expected[2], 0, 0))
            directory_handles.append((handle, path, expected))
            with os.scandir(path) as entries:
                for entry in entries:
                    member_path = Path(entry.path)
                    member_info = os.lstat(member_path)
                    if is_reparse_point(member_path, member_info):
                        raise ManagedPathError(f"managed directory contains a reparse point: {member_path}")
                    member_handle = _windows_open(
                        member_path,
                        access=_FILE_READ_ATTRIBUTES,
                        share_delete=False,
                        directory=stat.S_ISDIR(member_info.st_mode),
                    )
                    _windows_verify_handle(
                        member_handle,
                        member_path,
                        (
                            member_info.st_dev,
                            member_info.st_ino,
                            member_info.st_mode,
                            member_info.st_size,
                            member_info.st_mtime_ns,
                        ),
                    )
                    member_handles.append(member_handle)
        for path in file_paths:
            expected = file_identity(path)
            if not stat.S_ISREG(expected[2]):
                raise ManagedPathError(f"managed stability-lock target is not a regular file: {path}")
            handle = _windows_open(
                path,
                access=_GENERIC_READ | _FILE_READ_ATTRIBUTES,
                share_delete=False,
                share_write=False,
                directory=False,
            )
            _windows_verify_handle(handle, path, expected, reject_multiple_links=True)
            file_handles.append((handle, path, expected))
        for path in writable_paths:
            expected = file_identity(path)
            if not stat.S_ISREG(expected[2]):
                raise ManagedPathError(f"managed writable stability-lock target is not a regular file: {path}")
            handle = _windows_open(
                path,
                access=_GENERIC_READ | _FILE_READ_ATTRIBUTES,
                share_delete=False,
                share_write=True,
                directory=False,
            )
            _windows_verify_handle(handle, path, expected, reject_multiple_links=True)
            writable_handles.append((handle, path, expected))
        yield {
            "file_count": len(file_handles),
            "writable_file_count": len(writable_handles),
            "directory_count": len(directory_handles),
        }
        for handle, path, expected in file_handles:
            _windows_verify_handle(handle, path, expected, reject_multiple_links=True)
        for handle, path, expected in writable_handles:
            _windows_verify_handle(handle, path, expected, reject_multiple_links=True, verify_content=False)
        for handle, path, expected in directory_handles:
            _windows_verify_handle(handle, path, (expected[0], expected[1], expected[2], 0, 0))
    finally:
        for handle, _, _ in reversed(writable_handles):
            _KERNEL32.CloseHandle(handle)
        for handle, _, _ in reversed(file_handles):
            _KERNEL32.CloseHandle(handle)
        for handle in reversed(member_handles):
            _KERNEL32.CloseHandle(handle)
        for handle, _, _ in reversed(directory_handles):
            _KERNEL32.CloseHandle(handle)


@contextmanager
def hold_managed_output_directories(
    root: str | Path,
    *,
    directories: list[str | Path],
) -> Iterator[dict[str, int]]:
    """Pin writable output directories while another process creates members inside them."""

    if os.name != "nt":
        raise ManagedPathError("managed output-directory locks are currently supported only on Windows")
    root_path = validate_managed_path(root, root)
    paths = sorted({validate_managed_path(root_path, path) for path in directories}, key=str)
    handles: list[tuple[int, Path, tuple[int, int, int]]] = []
    try:
        for path in paths:
            expected = directory_identity(path)
            handle = _windows_open(
                path,
                access=_FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES,
                share_delete=False,
                directory=True,
            )
            _windows_verify_handle(handle, path, (expected[0], expected[1], expected[2], 0, 0))
            handles.append((handle, path, expected))
        yield {"directory_count": len(handles)}
        for handle, path, expected in handles:
            _windows_verify_handle(handle, path, (expected[0], expected[1], expected[2], 0, 0))
    finally:
        for handle, _, _ in reversed(handles):
            _KERNEL32.CloseHandle(handle)


def atomic_write_bytes(root: str | Path, target: str | Path, content: bytes) -> Path:
    root_path = validate_managed_path(root, root)
    target_path = Path(os.path.abspath(os.fspath(target)))
    parent = ensure_managed_directory(root_path, target_path.parent)
    parent_identity = directory_identity(parent)
    expected_target: tuple[int, int, int, int, int] | None = None
    if os.path.lexists(target_path):
        validate_managed_path(root_path, target_path)
        target_info = os.lstat(target_path)
        if not stat.S_ISREG(target_info.st_mode):
            raise ManagedPathError(f"managed write target is not a regular file: {target_path}")
        if target_info.st_nlink != 1:
            raise ManagedPathError(f"managed write target must not have multiple hard links: {target_path}")
        expected_target = file_identity(target_path)

    if os.name == "nt":
        _windows_create_and_rename(
            parent,
            parent_identity,
            target_path.name,
            content,
            expected_target=expected_target,
        )
        return validate_managed_path(root_path, target_path)

    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(prefix=".vmcp-", suffix=".tmp", dir=parent, delete=False) as handle:
            temp_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if directory_identity(parent) != parent_identity:
            raise ManagedPathError(f"managed write parent changed during write: {parent}")
        if os.path.lexists(target_path):
            validate_managed_path(root_path, target_path)
        os.replace(temp_name, target_path)
        temp_name = ""
        return validate_managed_path(root_path, target_path)
    finally:
        if temp_name and os.path.lexists(temp_name):
            os.unlink(temp_name)


def atomic_copy_file(
    source_root: str | Path,
    source: str | Path,
    target_root: str | Path,
    target: str | Path,
    *,
    replace: bool = False,
) -> Path:
    source_root_path = validate_managed_path(source_root, source_root)
    source_path = validate_managed_path(source_root_path, source)
    source_identity = file_identity(source_path)
    source_info = os.lstat(source_path)
    if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
        raise ManagedPathError(f"managed copy source must be a single-link regular file: {source_path}")

    target_root_path = validate_managed_path(target_root, target_root)
    target_path = Path(os.path.abspath(os.fspath(target)))
    parent = ensure_managed_directory(target_root_path, target_path.parent)
    parent_identity = directory_identity(parent)
    expected_target: tuple[int, int, int, int, int] | None = None
    if os.path.lexists(target_path):
        if not replace:
            raise ManagedPathError(f"managed copy target already exists: {target_path}")
        validate_managed_path(target_root_path, target_path)
        target_info = os.lstat(target_path)
        if not stat.S_ISREG(target_info.st_mode) or target_info.st_nlink != 1:
            raise ManagedPathError(f"managed copy target must be a single-link regular file: {target_path}")
        expected_target = file_identity(target_path)

    if os.name == "nt":
        _windows_copy_and_rename(
            source_path,
            source_identity,
            parent,
            parent_identity,
            target_path.name,
            expected_target=expected_target,
        )
        return validate_managed_path(target_root_path, target_path)

    temp_path = parent / f".vmcp-{uuid.uuid4().hex}.tmp"
    try:
        with source_path.open("rb") as source_handle, temp_path.open("xb") as target_handle:
            opened_source = os.fstat(source_handle.fileno())
            opened_identity = (
                opened_source.st_dev,
                opened_source.st_ino,
                opened_source.st_mode,
                opened_source.st_size,
                opened_source.st_mtime_ns,
            )
            if opened_identity != source_identity:
                raise ManagedPathError(f"managed copy source changed before read: {source_path}")
            while chunk := source_handle.read(1024 * 1024):
                target_handle.write(chunk)
            target_handle.flush()
            os.fsync(target_handle.fileno())
            after_source = os.fstat(source_handle.fileno())
        if source_identity != (
            after_source.st_dev,
            after_source.st_ino,
            after_source.st_mode,
            after_source.st_size,
            after_source.st_mtime_ns,
        ) or source_identity != file_identity(source_path):
            raise ManagedPathError(f"managed copy source changed during read: {source_path}")
        if directory_identity(parent) != parent_identity:
            raise ManagedPathError(f"managed copy target parent changed during write: {parent}")
        if expected_target is not None and file_identity(target_path) != expected_target:
            raise ManagedPathError(f"managed copy target changed during write: {target_path}")
        os.replace(temp_path, target_path)
        return validate_managed_path(target_root_path, target_path)
    finally:
        if os.path.lexists(temp_path):
            os.unlink(temp_path)


def snapshot_managed_tree(root: str | Path, target: str | Path) -> list[dict[str, Any]]:
    root_path = validate_managed_path(root, root)
    target_path = validate_managed_path(root_path, target)
    pending = [target_path]
    snapshot: list[dict[str, Any]] = []
    while pending:
        current = pending.pop()
        details = os.lstat(current)
        if is_reparse_point(current, details):
            raise ManagedPathError(f"managed tree contains a reparse point: {current}")
        if stat.S_ISDIR(details.st_mode):
            kind = "dir"
            with os.scandir(current) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        elif stat.S_ISREG(details.st_mode):
            kind = "file"
        else:
            raise ManagedPathError(f"managed tree contains an unsupported object: {current}")
        snapshot.append(
            {
                "path": str(current),
                "relative_path": current.relative_to(root_path).as_posix(),
                "kind": kind,
                "file_id": f"{details.st_dev}:{details.st_ino}",
                "mode": details.st_mode,
                "size": details.st_size,
                "mtime_ns": details.st_mtime_ns,
            }
        )
    return sorted(snapshot, key=lambda item: str(item["relative_path"]))


def validate_managed_snapshot(root: str | Path, target: str | Path, expected: list[dict[str, Any]]) -> None:
    current = snapshot_managed_tree(root, target)
    if current != expected:
        raise ManagedPathError(f"managed tree changed after planning: {target}")


def delete_managed_snapshot(root: str | Path, target: str | Path, expected: list[dict[str, Any]]) -> dict[str, int]:
    validate_managed_snapshot(root, target, expected)
    deleted_files = 0
    deleted_dirs = 0
    deleted_bytes = 0
    for item in sorted(expected, key=lambda entry: len(Path(str(entry["path"])).parts), reverse=True):
        path = Path(str(item["path"]))
        details = os.lstat(path)
        identity = f"{details.st_dev}:{details.st_ino}"
        changed = identity != item["file_id"] or details.st_mode != item["mode"] or is_reparse_point(path, details)
        if item["kind"] == "file":
            changed = changed or details.st_size != item["size"] or details.st_mtime_ns != item["mtime_ns"]
        if changed:
            raise ManagedPathError(f"managed object changed immediately before deletion: {path}")
        if item["kind"] == "file":
            if os.name == "nt":
                _windows_delete_handle(path, item)
            else:
                path.unlink()
            deleted_files += 1
            deleted_bytes += int(item["size"])
        else:
            if os.name == "nt":
                _windows_delete_handle(path, item)
            else:
                path.rmdir()
            deleted_dirs += 1
    return {"file_count": deleted_files, "dir_count": deleted_dirs, "bytes": deleted_bytes}


if os.name == "nt":
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _NTDLL = ctypes.WinDLL("ntdll")
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _DELETE = 0x00010000
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_TRAVERSE = 0x0020
    _FILE_READ_ATTRIBUTES = 0x0080
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_RENAME_INFORMATION_CLASS = 10
    _FILE_DISPOSITION_INFO_CLASS = 4
    _FILE_DISPOSITION_INFO_EX_CLASS = 21
    _FILE_DISPOSITION_FLAG_DELETE = 0x00000001
    _FILE_DISPOSITION_FLAG_POSIX_SEMANTICS = 0x00000002
    _FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE = 0x00000010

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _FILE_RENAME_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("Reserved", ctypes.c_ubyte * (ctypes.sizeof(ctypes.c_void_p) - 1)),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    class _IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class _FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    class _FILE_DISPOSITION_INFO_EX(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD)]

    _KERNEL32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _KERNEL32.CreateFileW.restype = wintypes.HANDLE
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    _KERNEL32.GetFileInformationByHandle.restype = wintypes.BOOL
    _KERNEL32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _KERNEL32.SetFileInformationByHandle.restype = wintypes.BOOL
    _KERNEL32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    _KERNEL32.SetFilePointerEx.restype = wintypes.BOOL
    _KERNEL32.SetEndOfFile.argtypes = [wintypes.HANDLE]
    _KERNEL32.SetEndOfFile.restype = wintypes.BOOL
    _KERNEL32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _KERNEL32.ReadFile.restype = wintypes.BOOL
    _KERNEL32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _KERNEL32.WriteFile.restype = wintypes.BOOL
    _KERNEL32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _KERNEL32.FlushFileBuffers.restype = wintypes.BOOL
    _NTDLL.NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IO_STATUS_BLOCK),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    _NTDLL.NtSetInformationFile.restype = ctypes.c_long
    _NTDLL.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    _NTDLL.RtlNtStatusToDosError.restype = wintypes.ULONG


def _windows_open(
    path: Path,
    *,
    access: int,
    share_delete: bool,
    directory: bool,
    creation: int = 3,
    share_write: bool = True,
) -> int:
    flags = _FILE_FLAG_OPEN_REPARSE_POINT | (_FILE_FLAG_BACKUP_SEMANTICS if directory else _FILE_ATTRIBUTE_NORMAL)
    share = _FILE_SHARE_READ | (_FILE_SHARE_WRITE if share_write else 0) | (_FILE_SHARE_DELETE if share_delete else 0)
    handle = _KERNEL32.CreateFileW(str(path), access, share, None, creation, flags, None)
    if handle == _INVALID_HANDLE_VALUE:
        raise ManagedPathError(f"Windows handle open failed for {path}: {ctypes.WinError(ctypes.get_last_error())}")
    return int(handle)


def _windows_handle_info(handle: int, *, path: Path) -> _BY_HANDLE_FILE_INFORMATION:
    info = _BY_HANDLE_FILE_INFORMATION()
    if not _KERNEL32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise ManagedPathError(f"Windows handle identity failed for {path}: {ctypes.WinError(ctypes.get_last_error())}")
    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise ManagedPathError(f"managed object became a reparse point: {path}")
    return info


def _windows_verify_handle(
    handle: int,
    path: Path,
    expected: dict[str, Any] | tuple[int, int, int, int, int],
    *,
    reject_multiple_links: bool = False,
    verify_content: bool = True,
) -> None:
    info = _windows_handle_info(handle, path=path)
    file_index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
    is_directory = bool(info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY)
    if isinstance(expected, tuple):
        expected_inode = int(expected[1])
        expected_mode = int(expected[2])
        expected_size = int(expected[3])
        expected_mtime_ns = int(expected[4])
    else:
        expected_inode = int(str(expected["file_id"]).split(":", 1)[1])
        expected_mode = int(expected["mode"])
        expected_size = int(expected["size"])
        expected_mtime_ns = int(expected["mtime_ns"])
    size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
    filetime = (int(info.ftLastWriteTime.dwHighDateTime) << 32) | int(info.ftLastWriteTime.dwLowDateTime)
    mtime_ns = (filetime - 116444736000000000) * 100
    if file_index != expected_inode or is_directory != stat.S_ISDIR(expected_mode):
        raise ManagedPathError(f"managed object identity changed before handle-bound mutation: {path}")
    if reject_multiple_links and int(info.nNumberOfLinks) != 1:
        raise ManagedPathError(f"managed write target must not have multiple hard links: {path}")
    if verify_content and not is_directory and (size != expected_size or mtime_ns != expected_mtime_ns):
        raise ManagedPathError(f"managed file content identity changed before handle-bound mutation: {path}")


def _windows_write_handle(handle: int, path: Path, content: bytes) -> None:
    if not _KERNEL32.SetFilePointerEx(handle, 0, None, 0) or not _KERNEL32.SetEndOfFile(handle):
        raise ManagedPathError(f"Windows handle truncate failed for {path}: {ctypes.WinError(ctypes.get_last_error())}")
    offset = 0
    while offset < len(content):
        chunk = content[offset : offset + 1024 * 1024]
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(chunk)
        if not _KERNEL32.WriteFile(handle, buffer, len(chunk), ctypes.byref(written), None) or written.value != len(chunk):
            raise ManagedPathError(f"Windows handle write failed for {path}: {ctypes.WinError(ctypes.get_last_error())}")
        offset += written.value
    if not _KERNEL32.FlushFileBuffers(handle):
        raise ManagedPathError(f"Windows handle flush failed for {path}: {ctypes.WinError(ctypes.get_last_error())}")


def _windows_rename_handle(handle: int, parent_handle: int, source_path: Path, leaf: str) -> None:
    encoded_name = leaf.encode("utf-16-le")
    name_offset = _FILE_RENAME_INFORMATION.FileName.offset
    buffer = ctypes.create_string_buffer(name_offset + len(encoded_name))
    rename = ctypes.cast(buffer, ctypes.POINTER(_FILE_RENAME_INFORMATION)).contents
    rename.ReplaceIfExists = 0
    rename.RootDirectory = parent_handle
    rename.FileNameLength = len(encoded_name)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded_name, len(encoded_name))
    io_status = _IO_STATUS_BLOCK()
    ntstatus = _NTDLL.NtSetInformationFile(
        handle,
        ctypes.byref(io_status),
        buffer,
        len(buffer),
        _FILE_RENAME_INFORMATION_CLASS,
    )
    if ntstatus < 0:
        winerror = int(_NTDLL.RtlNtStatusToDosError(ntstatus))
        raise ManagedPathError(f"Windows handle-bound rename failed for {source_path}: {ctypes.WinError(winerror)}")


def _windows_create_and_rename(
    parent: Path,
    expected_parent: tuple[int, int, int],
    leaf: str,
    content: bytes,
    *,
    expected_target: tuple[int, int, int, int, int] | None,
) -> None:
    parent_handle = _windows_open(
        parent,
        access=_FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES,
        share_delete=False,
        directory=True,
    )
    temp_path = parent / f".vmcp-{uuid.uuid4().hex}.tmp"
    temp_handle = 0
    target_handle = 0
    temp_installed = False
    quarantine_path: Path | None = None
    try:
        _windows_verify_handle(parent_handle, parent, (expected_parent[0], expected_parent[1], expected_parent[2], 0, 0))
        if expected_target is not None:
            target_path = parent / leaf
            target_handle = _windows_open(
                target_path,
                access=_DELETE | _FILE_READ_ATTRIBUTES,
                share_delete=False,
                share_write=False,
                directory=False,
            )
            _windows_verify_handle(
                target_handle,
                target_path,
                expected_target,
                reject_multiple_links=True,
            )
        temp_handle = _windows_open(
            temp_path,
            access=_GENERIC_WRITE | _FILE_READ_ATTRIBUTES | _DELETE,
            share_delete=False,
            share_write=False,
            directory=False,
            creation=_CREATE_NEW,
        )
        _windows_write_handle(temp_handle, temp_path, content)
        if target_handle:
            quarantine_path = parent / f".vmcp-{uuid.uuid4().hex}.quarantine"
            _windows_rename_handle(target_handle, parent_handle, target_path, quarantine_path.name)
        _windows_rename_handle(temp_handle, parent_handle, temp_path, leaf)
        temp_installed = True
        if target_handle and quarantine_path is not None:
            _windows_mark_delete(target_handle, quarantine_path)
    except Exception:
        if target_handle and quarantine_path is not None and not temp_installed:
            try:
                _windows_rename_handle(target_handle, parent_handle, quarantine_path, leaf)
                quarantine_path = None
            except ManagedPathError:
                pass
        raise
    finally:
        if temp_handle:
            if not temp_installed:
                _windows_mark_delete(temp_handle, temp_path)
            _KERNEL32.CloseHandle(temp_handle)
        if target_handle:
            _KERNEL32.CloseHandle(target_handle)
        _KERNEL32.CloseHandle(parent_handle)


def _windows_copy_and_rename(
    source_path: Path,
    expected_source: tuple[int, int, int, int, int],
    parent: Path,
    expected_parent: tuple[int, int, int],
    leaf: str,
    *,
    expected_target: tuple[int, int, int, int, int] | None,
) -> None:
    source_handle = _windows_open(
        source_path,
        access=_GENERIC_READ | _FILE_READ_ATTRIBUTES,
        share_delete=False,
        share_write=False,
        directory=False,
    )
    parent_handle = _windows_open(
        parent,
        access=_FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES,
        share_delete=False,
        directory=True,
    )
    temp_path = parent / f".vmcp-{uuid.uuid4().hex}.tmp"
    temp_handle = 0
    target_handle = 0
    temp_installed = False
    quarantine_path: Path | None = None
    try:
        _windows_verify_handle(source_handle, source_path, expected_source, reject_multiple_links=True)
        _windows_verify_handle(parent_handle, parent, (expected_parent[0], expected_parent[1], expected_parent[2], 0, 0))
        if expected_target is not None:
            target_path = parent / leaf
            target_handle = _windows_open(
                target_path,
                access=_DELETE | _FILE_READ_ATTRIBUTES,
                share_delete=False,
                share_write=False,
                directory=False,
            )
            _windows_verify_handle(target_handle, target_path, expected_target, reject_multiple_links=True)
        temp_handle = _windows_open(
            temp_path,
            access=_GENERIC_WRITE | _FILE_READ_ATTRIBUTES | _DELETE,
            share_delete=False,
            share_write=False,
            directory=False,
            creation=_CREATE_NEW,
        )
        _windows_copy_handles(source_handle, temp_handle, source_path, temp_path)
        _windows_verify_handle(source_handle, source_path, expected_source, reject_multiple_links=True)
        if target_handle:
            quarantine_path = parent / f".vmcp-{uuid.uuid4().hex}.quarantine"
            _windows_rename_handle(target_handle, parent_handle, target_path, quarantine_path.name)
        _windows_rename_handle(temp_handle, parent_handle, temp_path, leaf)
        temp_installed = True
        if target_handle and quarantine_path is not None:
            _windows_mark_delete(target_handle, quarantine_path)
    except Exception:
        if target_handle and quarantine_path is not None and not temp_installed:
            try:
                _windows_rename_handle(target_handle, parent_handle, quarantine_path, leaf)
                quarantine_path = None
            except ManagedPathError:
                pass
        raise
    finally:
        if temp_handle:
            if not temp_installed:
                _windows_mark_delete(temp_handle, temp_path)
            _KERNEL32.CloseHandle(temp_handle)
        if target_handle:
            _KERNEL32.CloseHandle(target_handle)
        _KERNEL32.CloseHandle(parent_handle)
        _KERNEL32.CloseHandle(source_handle)


def _windows_copy_handles(source_handle: int, target_handle: int, source_path: Path, target_path: Path) -> None:
    if not _KERNEL32.SetFilePointerEx(target_handle, 0, None, 0) or not _KERNEL32.SetEndOfFile(target_handle):
        raise ManagedPathError(f"Windows handle truncate failed for {target_path}: {ctypes.WinError(ctypes.get_last_error())}")
    while True:
        buffer = ctypes.create_string_buffer(1024 * 1024)
        read = wintypes.DWORD()
        if not _KERNEL32.ReadFile(source_handle, buffer, len(buffer), ctypes.byref(read), None):
            raise ManagedPathError(f"Windows handle read failed for {source_path}: {ctypes.WinError(ctypes.get_last_error())}")
        if read.value == 0:
            break
        written = wintypes.DWORD()
        if not _KERNEL32.WriteFile(target_handle, buffer, read.value, ctypes.byref(written), None) or written.value != read.value:
            raise ManagedPathError(f"Windows handle write failed for {target_path}: {ctypes.WinError(ctypes.get_last_error())}")
    if not _KERNEL32.FlushFileBuffers(target_handle):
        raise ManagedPathError(f"Windows handle flush failed for {target_path}: {ctypes.WinError(ctypes.get_last_error())}")


def _windows_mark_delete(handle: int, path: Path) -> None:
    extended = _FILE_DISPOSITION_INFO_EX(
        _FILE_DISPOSITION_FLAG_DELETE
        | _FILE_DISPOSITION_FLAG_POSIX_SEMANTICS
        | _FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE
    )
    if _KERNEL32.SetFileInformationByHandle(
        handle,
        _FILE_DISPOSITION_INFO_EX_CLASS,
        ctypes.byref(extended),
        ctypes.sizeof(extended),
    ):
        return
    basic = _FILE_DISPOSITION_INFO(True)
    if not _KERNEL32.SetFileInformationByHandle(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(basic),
        ctypes.sizeof(basic),
    ):
        raise ManagedPathError(f"Windows handle-bound delete failed for {path}: {ctypes.WinError(ctypes.get_last_error())}")


def _windows_delete_handle(path: Path, expected: dict[str, Any]) -> None:
    is_directory = expected["kind"] == "dir"
    handle = _windows_open(
        path,
        access=_DELETE | _FILE_READ_ATTRIBUTES,
        share_delete=True,
        directory=is_directory,
    )
    try:
        _windows_verify_handle(handle, path, expected)
        _windows_mark_delete(handle, path)
    finally:
        _KERNEL32.CloseHandle(handle)
