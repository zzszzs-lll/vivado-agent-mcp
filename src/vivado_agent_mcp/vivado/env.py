from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from .managed_path import file_identity, is_reparse_point


DEFAULT_RUNTIME_DIR_NAME = ".vivado_agent_mcp"
PROBE_TAIL_BYTES = 8192
TRUSTED_VIVADO_PATH_ENV = "VIVADO_PATH"
VIVADO_EXECUTABLE_NAMES = frozenset({"vivado", "vivado.bat", "vivado.exe"})
EXECUTABLE_HASH_CHUNK_BYTES = 1024 * 1024
VIVADO_VERSION_PATTERN = re.compile(
    r"\bVivado(?:\s+\w+){0,3}\s+v(?P<version>\d{4}\.\d+(?:\.\d+)*)\b",
    re.IGNORECASE,
)
VIVADO_PATH_VERSION_PATTERN = re.compile(r"\d{4}\.\d+(?:\.\d+)*")


def vivado_environment(base: dict[str, str] | None = None, temp_dir: Path | None = None) -> dict[str, str]:
    env = dict(base or os.environ)
    if os.name == "nt":
        env.setdefault("SystemRoot", r"C:\WINDOWS")
        env.setdefault("WINDIR", r"C:\WINDOWS")
        env.setdefault("PROCESSOR_ARCHITECTURE", "AMD64")
    if temp_dir is not None:
        env["TEMP"] = str(temp_dir)
        env["TMP"] = str(temp_dir)
    return env


class TrustedVivadoPathError(ValueError):
    def __init__(self, error_code: str, message: str, *, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.data = dict(data or {})


def capture_server_vivado_identity() -> dict[str, Any]:
    configured_path = str(os.environ.get(TRUSTED_VIVADO_PATH_ENV, "") or "").strip()
    if not configured_path:
        return _trusted_path_failure(
            "VIVADO_PATH_NOT_CONFIGURED",
            f"{TRUSTED_VIVADO_PATH_ENV} must be configured before the MCP server starts.",
            configured_path="",
        )
    try:
        identity = _capture_executable_identity(configured_path)
    except TrustedVivadoPathError as exc:
        return _trusted_path_failure(
            exc.error_code,
            str(exc),
            configured_path=configured_path,
            **exc.data,
        )
    return {
        "ok": True,
        "source": "environment",
        "environment_variable": TRUSTED_VIVADO_PATH_ENV,
        "configured_path": configured_path,
        "canonical_path": identity["canonical_path"],
        "object_identity": identity["object_identity"],
        "file_identity": identity["file_identity"],
        "sha256": identity["sha256"],
        "execution_attempted": False,
    }


def validate_trusted_vivado_executable(
    requested_path: str | Path | None = None,
    *,
    trusted_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trusted = dict(trusted_identity) if trusted_identity is not None else capture_server_vivado_identity()
    requested_text = str(requested_path or "").strip()
    if not trusted.get("ok"):
        return {
            **trusted,
            "requested_path": requested_text,
            "request_path_verified": False,
            "execution_attempted": False,
        }

    trusted_path = Path(str(trusted.get("canonical_path", "")))
    if not trusted_path.is_absolute():
        return _trusted_path_failure(
            "VIVADO_PATH_TRUST_STATE_INVALID",
            "Captured VIVADO_PATH trust state does not contain an absolute canonical path.",
            configured_path=str(trusted.get("configured_path", "")),
            requested_path=requested_text,
        )

    requested = Path(requested_text).expanduser() if requested_text else trusted_path
    if not requested.is_absolute():
        return _trusted_path_failure(
            "VIVADO_PATH_NOT_ABSOLUTE",
            "Vivado executable paths must be absolute.",
            configured_path=str(trusted.get("configured_path", "")),
            trusted_canonical_path=str(trusted_path),
            requested_path=requested_text,
        )
    requested_canonical = requested.resolve(strict=False)
    if os.path.normcase(str(requested_canonical)) != os.path.normcase(str(trusted_path)):
        return _trusted_path_failure(
            "VIVADO_PATH_MISMATCH",
            "The requested Vivado executable does not match the server-start VIVADO_PATH canonical path.",
            configured_path=str(trusted.get("configured_path", "")),
            trusted_canonical_path=str(trusted_path),
            requested_path=requested_text,
            requested_canonical_path=str(requested_canonical),
        )

    try:
        current = _capture_executable_identity(requested_canonical)
    except TrustedVivadoPathError as exc:
        error_code = "VIVADO_PATH_IDENTITY_CHANGED" if exc.error_code != "VIVADO_PATH_NOT_CONFIGURED" else exc.error_code
        return _trusted_path_failure(
            error_code,
            f"The configured Vivado executable can no longer be verified: {exc}",
            configured_path=str(trusted.get("configured_path", "")),
            trusted_canonical_path=str(trusted_path),
            requested_path=requested_text,
            **exc.data,
        )

    identity_matches = (
        os.path.normcase(str(current["canonical_path"])) == os.path.normcase(str(trusted_path))
        and list(current["object_identity"]) == list(trusted.get("object_identity", []))
        and list(current["file_identity"]) == list(trusted.get("file_identity", []))
        and str(current["sha256"]) == str(trusted.get("sha256", ""))
    )
    if not identity_matches:
        return _trusted_path_failure(
            "VIVADO_PATH_IDENTITY_CHANGED",
            "The configured Vivado executable identity changed after server startup.",
            configured_path=str(trusted.get("configured_path", "")),
            trusted_canonical_path=str(trusted_path),
            requested_path=requested_text,
            requested_canonical_path=str(current["canonical_path"]),
            trusted_object_identity=list(trusted.get("object_identity", [])),
            current_object_identity=list(current["object_identity"]),
            trusted_sha256=str(trusted.get("sha256", "")),
            current_sha256=str(current["sha256"]),
        )
    return {
        **trusted,
        "canonical_path": str(current["canonical_path"]),
        "requested_path": requested_text,
        "requested_canonical_path": str(current["canonical_path"]),
        "request_path_verified": True,
        "execution_attempted": False,
    }


def find_vivado(
    explicit_path: str | None = None,
    *,
    probe_launch: bool = False,
    probe_timeout_s: int = 15,
    runtime_dir: str | None = None,
    trusted_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trusted = dict(trusted_identity) if trusted_identity is not None else capture_server_vivado_identity()
    validated = validate_trusted_vivado_executable(explicit_path, trusted_identity=trusted)
    searched = [str(trusted.get("configured_path", ""))] if trusted.get("configured_path") else []
    if explicit_path and explicit_path not in searched:
        searched.append(explicit_path)
    if not validated.get("ok"):
        return {
            **_missing_vivado_info(searched=searched),
            **validated,
        }

    path = Path(str(validated["canonical_path"]))
    data = _vivado_info(path, source="environment", searched=searched)
    data["trusted_executable_identity"] = {
        "environment_variable": TRUSTED_VIVADO_PATH_ENV,
        "configured_path": str(trusted.get("configured_path", "")),
        "canonical_path": str(validated["canonical_path"]),
        "object_identity": list(validated.get("object_identity", [])),
        "file_identity": list(validated.get("file_identity", [])),
        "sha256": str(validated.get("sha256", "")),
        "request_path_verified": bool(validated.get("request_path_verified")),
    }
    data["execution_attempted"] = False
    if probe_launch:
        probe = probe_vivado_launch(
            path,
            timeout_s=probe_timeout_s,
            runtime_dir=runtime_dir,
            trusted_identity=trusted,
        )
        data["launch_probe"] = probe
        data["process_launch_ok"] = bool(probe.get("process_launch_ok"))
        data["launch_ready"] = bool(probe.get("launch_ready"))
        data["probed_version"] = probe.get("probed_version")
        data["version_attested"] = bool(probe.get("version_attested"))
        data["version"] = probe.get("probed_version") if probe.get("version_attested") else None
        data["tools"]["vivado"]["version"] = data["version"]
        data["execution_attempted"] = bool(probe.get("execution_attempted"))
        if not probe.get("ok") and probe.get("error_code") in {
            "VIVADO_PATH_MISMATCH",
            "VIVADO_PATH_IDENTITY_CHANGED",
            "VIVADO_PATH_NOT_CONFIGURED",
            "VIVADO_PATH_NOT_ABSOLUTE",
            "VIVADO_PATH_TRUST_STATE_INVALID",
        }:
            data["ok"] = False
            data["error_code"] = probe.get("error_code")
            data["message"] = probe.get("message")
    else:
        data["launch_probe"] = _skipped_launch_probe()
        data["process_launch_ok"] = None
        data["launch_ready"] = None
    return data


def probe_vivado_launch(
    path: str | Path,
    *,
    timeout_s: int = 15,
    runtime_dir: str | None = None,
    trusted_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validated = validate_trusted_vivado_executable(path, trusted_identity=trusted_identity)
    if not validated.get("ok"):
        return {
            **validated,
            "requested": True,
            "status": "BLOCK",
            "process_launch_ok": False,
            "launch_ready": False,
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "probed_version": None,
            "version_attested": False,
        }
    vivado_path = Path(str(validated["canonical_path"]))
    runtime = resolve_runtime_dir(runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    command = [str(vivado_path), "-mode", "batch", "-version"]
    try:
        completed = subprocess.run(
            command,
            cwd=str(runtime),
            env=vivado_environment(temp_dir=runtime),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "requested": True,
            "ok": False,
            "status": "TIMEOUT",
            "execution_attempted": True,
            "command": command,
            "vivado_path": str(vivado_path),
            "runtime_dir": str(runtime),
            "timeout_s": timeout_s,
            "returncode": None,
            "process_launch_ok": False,
            "launch_ready": False,
            "stdout_tail": _tail_text(exc.output),
            "stderr_tail": _tail_text(exc.stderr),
            "probed_version": None,
            "version_attested": False,
            "diagnosis": {
                "primary_cause": "timeout",
                "message": "Vivado batch version probe timed out before returning.",
            },
        }
    except OSError as exc:
        return {
            "requested": True,
            "ok": False,
            "status": "PROBE_ERROR",
            "execution_attempted": True,
            "command": command,
            "vivado_path": str(vivado_path),
            "runtime_dir": str(runtime),
            "timeout_s": timeout_s,
            "returncode": None,
            "process_launch_ok": False,
            "launch_ready": False,
            "stdout_tail": "",
            "stderr_tail": str(exc),
            "probed_version": None,
            "version_attested": False,
            "diagnosis": {
                "primary_cause": "probe_error",
                "message": str(exc),
            },
        }
    process_launch_ok = completed.returncode == 0
    stdout_tail = _tail_text(completed.stdout)
    stderr_tail = _tail_text(completed.stderr)
    probed_version = _parse_vivado_version("\n".join(part for part in (stdout_tail, stderr_tail) if part))
    version_attested = bool(process_launch_ok and probed_version)
    launch_ready = version_attested
    return {
        "requested": True,
        "ok": launch_ready,
        "status": "PASS" if launch_ready else ("UNATTESTED" if process_launch_ok else "FAIL"),
        "execution_attempted": True,
        "command": command,
        "vivado_path": str(vivado_path),
        "runtime_dir": str(runtime),
        "timeout_s": timeout_s,
        "returncode": completed.returncode,
        "process_launch_ok": process_launch_ok,
        "launch_ready": launch_ready,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "probed_version": probed_version,
        "version_attested": version_attested,
        "diagnosis": {
            "primary_cause": "launch_ok" if version_attested else ("version_unattested" if process_launch_ok else "process_exit"),
            "message": (
                f"Vivado batch version probe attested version {probed_version}."
                if version_attested
                else (
                    "Vivado batch probe completed, but its output did not contain an attested Vivado version."
                    if process_launch_ok
                    else "Vivado batch version probe exited with a non-zero return code."
                )
            ),
        },
    }


def resolve_runtime_dir(explicit_dir: str | None = None, cwd: Path | None = None) -> Path:
    """Resolve runtime files inside a user-controlled workspace by default."""

    if explicit_dir:
        return Path(explicit_dir).expanduser().resolve()
    if os.environ.get("VIVADO_AGENT_MCP_RUNTIME_DIR"):
        return Path(os.environ["VIVADO_AGENT_MCP_RUNTIME_DIR"]).expanduser().resolve()
    root = (cwd or Path.cwd()).resolve()
    return root / DEFAULT_RUNTIME_DIR_NAME / "runtime"


def _skipped_launch_probe() -> dict[str, Any]:
    return {
        "requested": False,
        "ok": None,
        "status": "SKIPPED",
        "process_launch_ok": None,
        "launch_ready": None,
        "probed_version": None,
        "version_attested": False,
        "execution_attempted": False,
        "diagnosis": {
            "primary_cause": "not_requested",
            "message": "Set probe_launch=true to run a bounded Vivado batch startup probe.",
        },
    }


def _capture_executable_identity(path_text: str | Path) -> dict[str, Any]:
    candidate = Path(path_text).expanduser()
    if not candidate.is_absolute():
        raise TrustedVivadoPathError(
            "VIVADO_PATH_NOT_ABSOLUTE",
            "VIVADO_PATH must be an absolute path.",
            data={"candidate_path": str(candidate)},
        )
    if not os.path.lexists(candidate):
        raise TrustedVivadoPathError(
            "VIVADO_PATH_NOT_FOUND",
            "Configured VIVADO_PATH does not exist.",
            data={"candidate_path": str(candidate)},
        )
    original = os.lstat(candidate)
    if is_reparse_point(candidate, original):
        raise TrustedVivadoPathError(
            "VIVADO_PATH_REPARSE_POINT_BLOCKED",
            "Configured VIVADO_PATH must not be a symlink, junction, or reparse point.",
            data={"candidate_path": str(candidate)},
        )
    canonical = candidate.resolve(strict=True)
    if canonical.name.lower() not in VIVADO_EXECUTABLE_NAMES:
        raise TrustedVivadoPathError(
            "VIVADO_PATH_EXECUTABLE_NAME_INVALID",
            "Configured VIVADO_PATH must name vivado, vivado.exe, or vivado.bat.",
            data={"candidate_path": str(candidate), "canonical_path": str(canonical)},
        )
    before = file_identity(canonical)
    if not stat.S_ISREG(before[2]):
        raise TrustedVivadoPathError(
            "VIVADO_PATH_NOT_REGULAR_FILE",
            "Configured VIVADO_PATH must be a regular file.",
            data={"canonical_path": str(canonical)},
        )
    digest = hashlib.sha256()
    try:
        with canonical.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            opened_identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            expected_opened_identity = (before[0], before[1], before[3], before[4])
            if opened_identity != expected_opened_identity:
                raise TrustedVivadoPathError(
                    "VIVADO_PATH_IDENTITY_CHANGED",
                    "Configured VIVADO_PATH changed while its identity was captured.",
                    data={"canonical_path": str(canonical)},
                )
            while True:
                chunk = handle.read(EXECUTABLE_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise TrustedVivadoPathError(
            "VIVADO_PATH_IDENTITY_UNAVAILABLE",
            f"Could not read configured VIVADO_PATH for identity capture: {exc}",
            data={"canonical_path": str(canonical)},
        ) from exc
    after = file_identity(canonical)
    if after != before:
        raise TrustedVivadoPathError(
            "VIVADO_PATH_IDENTITY_CHANGED",
            "Configured VIVADO_PATH changed while its identity was captured.",
            data={"canonical_path": str(canonical)},
        )
    return {
        "canonical_path": str(canonical),
        "object_identity": list(before[:3]),
        "file_identity": list(before),
        "sha256": digest.hexdigest(),
    }


def _trusted_path_failure(error_code: str, message: str, **data: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "source": "environment",
        "environment_variable": TRUSTED_VIVADO_PATH_ENV,
        "execution_attempted": False,
        **data,
    }


def _missing_vivado_info(*, searched: list[str]) -> dict[str, Any]:
    return {
        "ok": False,
        "path": None,
        "source": "environment",
        "searched": searched,
        "version": None,
        "path_hint_version": None,
        "probed_version": None,
        "version_attested": False,
        "tools": _missing_native_tools(),
        "xsim_available": False,
        "launch_probe": _skipped_launch_probe(),
        "process_launch_ok": False,
        "launch_ready": False,
        "execution_attempted": False,
    }


def _vivado_info(path: Path, source: str, searched: list[str]) -> dict[str, Any]:
    bin_dir = path.parent
    path_hint_version = _infer_vivado_version(path)
    tools = {
        "vivado": _tool_info(path),
        "xvlog": _tool_info(_find_companion_tool(bin_dir, "xvlog")),
        "xelab": _tool_info(_find_companion_tool(bin_dir, "xelab")),
        "xsim": _tool_info(_find_companion_tool(bin_dir, "xsim")),
    }
    return {
        "ok": True,
        "path": str(path.resolve()),
        "source": source,
        "install_bin": str(bin_dir.resolve()),
        "version": None,
        "path_hint_version": path_hint_version,
        "probed_version": None,
        "version_attested": False,
        "tools": tools,
        "xsim_available": all(tools[name]["available"] for name in ("xvlog", "xelab", "xsim")),
        "searched": searched,
    }


def _find_companion_tool(bin_dir: Path, name: str) -> Path | None:
    suffixes = [".bat", ".exe", ""] if os.name == "nt" else ["", ".sh"]
    for suffix in suffixes:
        candidate = bin_dir / f"{name}{suffix}"
        if candidate.exists():
            return candidate.resolve()
    which = shutil.which(name) or shutil.which(f"{name}.bat")
    return Path(which).resolve() if which else None


def _missing_native_tools() -> dict[str, dict[str, Any]]:
    return {
        name: {"available": False, "path": None, "version": None}
        for name in ("vivado", "xvlog", "xelab", "xsim")
    }


def _tool_info(path: Path | None, version: str | None = None) -> dict[str, Any]:
    if path is None:
        return {"available": False, "path": None, "version": None}
    return {"available": True, "path": str(path), "version": version}


def _tail_text(value: str | bytes | None, *, max_bytes: int = PROBE_TAIL_BYTES) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        data = value[-max_bytes:]
        return data.decode("utf-8", errors="replace").strip()
    encoded = value.encode("utf-8", errors="replace")
    return encoded[-max_bytes:].decode("utf-8", errors="replace").strip()


def _infer_vivado_version(path: Path) -> str | None:
    for part in path.parts:
        if VIVADO_PATH_VERSION_PATTERN.fullmatch(part):
            return part
    return None


def _parse_vivado_version(output: str) -> str | None:
    match = VIVADO_VERSION_PATTERN.search(output)
    return match.group("version") if match else None
