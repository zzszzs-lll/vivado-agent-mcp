from __future__ import annotations

import hashlib
import hmac
import socket
import secrets
import signal
import subprocess
import threading
import time
import uuid
import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bootstrap import BootstrapPermissionError, write_bootstrap
from .env import (
    TrustedVivadoPathError,
    _parse_vivado_version,
    capture_server_vivado_identity,
    find_vivado,
    resolve_runtime_dir,
    validate_trusted_vivado_executable,
    vivado_environment,
)
from .managed_path import ManagedPathError, hold_managed_paths_stable, read_stable_bytes
from .runs import (
    EXECUTABLE_CONSTRAINT_BLOCK_MARKER,
    MAX_TRUSTED_XDC_BYTES,
    SUPPORTED_VIVADO_VERSION,
    parse_project_execution_inputs,
    project_execution_inputs_command,
    validate_xdc_text,
)
from .runtime_identity import RuntimeIdentityError, ensure_runtime_identity
from .simulation import build_design_execution_identity, verify_design_execution_identity_files
from .tcl import tcl_list_quote


DEFAULT_START_TIMEOUT_S = 180
RECOMMENDED_RETRY_TIMEOUT_S = 240
WIRE_PROTOCOL_VERSION = "VMCP2"
TRANSPORT_AUTH = "mutual_hmac_sha256_sequence_v2"
PROJECT_ACTIVE_IDENTITY_MISMATCH_MARKER = "VMCP_PROJECT_ACTIVE_IDENTITY_MISMATCH"
MAX_RESPONSE_BODY_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_ENVELOPE_BYTES = (MAX_RESPONSE_BODY_BYTES * 2) + 4096


class TclResponseProtocolError(RuntimeError):
    def __init__(self, error_code: str, message: str, *, taint_reason: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.taint_reason = taint_reason


@dataclass
class GuiTcpVivadoSession:
    host: str = "127.0.0.1"
    port: int = 0
    vivado_path: str | None = None
    runtime_dir: str | None = None
    trusted_vivado_identity: dict[str, Any] = field(default_factory=capture_server_vivado_identity, repr=False)
    process: subprocess.Popen | None = None
    backend: str = "gui_spawn_tcp"
    _bootstrap_path: Path | None = None
    _runtime_path: Path | None = None
    _stdout_path: Path | None = None
    _stderr_path: Path | None = None
    _bootstrap_removed_after_handshake: bool = False
    _auth_secret: str = field(default_factory=lambda: secrets.token_hex(32), repr=False)
    _sequence: int = 0
    _request_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _project_guard_state: threading.local = field(default_factory=threading.local, repr=False)
    generation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = "NEW"
    taint_reason: str = ""

    def start(self, timeout_s: int = DEFAULT_START_TIMEOUT_S) -> dict[str, Any]:
        self.state = "STARTING"
        if self.port == 0:
            self.port = _free_port(self.host)
        vivado = find_vivado(self.vivado_path, trusted_identity=self.trusted_vivado_identity)
        if not vivado["ok"]:
            return {
                "ok": False,
                "error_code": str(vivado.get("error_code") or "VIVADO_NOT_FOUND"),
                "message": str(vivado.get("message") or "Trusted Vivado executable is unavailable."),
                "execution_attempted": False,
                "data": vivado,
            }

        runtime_dir = resolve_runtime_dir(self.runtime_dir)
        try:
            runtime_identity = ensure_runtime_identity(runtime_dir)
        except RuntimeIdentityError as exc:
            return {
                "ok": False,
                "error_code": "RUNTIME_DIR_REJECTED",
                "message": str(exc),
                "runtime_dir": str(runtime_dir),
                "runtime_identity": {"status": "BLOCK", "reason": exc.reason},
            }
        self._runtime_path = runtime_dir
        bootstrap = runtime_dir / f"vivado_agent_mcp_{self.port}.tcl"
        try:
            write_bootstrap(bootstrap, self.host, self.port, self._auth_secret)
        except (BootstrapPermissionError, OSError) as exc:
            self.state = "START_FAILED"
            self.taint_reason = "bootstrap_permission_failure"
            return {
                "ok": False,
                "error_code": "BOOTSTRAP_PERMISSION_FAILURE",
                "message": str(exc),
                "runtime_dir": str(runtime_dir),
                "bootstrap_path": str(bootstrap),
                "bootstrap_present": bootstrap.exists(),
                "generation_id": self.generation_id,
                "session_state": self.state,
                "taint_reason": self.taint_reason,
            }
        self._bootstrap_path = bootstrap
        self._stdout_path = runtime_dir / f"vivado_agent_mcp_{self.port}.stdout.log"
        self._stderr_path = runtime_dir / f"vivado_agent_mcp_{self.port}.stderr.log"
        command = [
            str(vivado["path"]),
            "-mode",
            "gui",
            "-source",
            str(bootstrap),
        ]
        try:
            with self._stdout_path.open("ab") as stdout_file, self._stderr_path.open("ab") as stderr_file:
                self.process = _spawn_vivado_process(
                    command,
                    trusted_identity=self.trusted_vivado_identity,
                    cwd=str(runtime_dir),
                    env=vivado_environment(temp_dir=runtime_dir),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    stdin=subprocess.DEVNULL,
                    creationflags=_creation_flags(),
                    start_new_session=os.name != "nt",
                )
        except TrustedVivadoPathError as exc:
            bootstrap_cleanup_error = ""
            try:
                bootstrap.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                bootstrap_cleanup_error = str(cleanup_exc)
            self.state = "START_FAILED"
            self.taint_reason = "vivado_path_identity_rejected"
            return {
                "ok": False,
                "error_code": exc.error_code,
                "message": str(exc),
                "data": {
                    **exc.data,
                    "runtime_dir": str(runtime_dir),
                    "bootstrap_path": str(bootstrap),
                    "bootstrap_present": bootstrap.exists(),
                    "bootstrap_cleanup_error": bootstrap_cleanup_error,
                    "generation_id": self.generation_id,
                    "session_state": self.state,
                    "taint_reason": self.taint_reason,
                    "execution_attempted": False,
                },
            }
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return {
                    "ok": False,
                    "error_code": "VIVADO_PROCESS_EXITED",
                    "message": f"Vivado process exited before Tcl server became available (returncode={self.process.poll()}).",
                    "runtime_dir": str(runtime_dir),
                    "temp_dir": str(runtime_dir),
                    "bootstrap_path": str(bootstrap),
                    "runtime_identity": runtime_identity,
                    "startup": self._startup_diagnostics(
                        command,
                        vivado_path=str(Path(str(vivado["path"])).resolve()),
                        phase="process_exited",
                    ),
                }
            try:
                result = self.run_tcl("version", timeout_s=5)
                if result["ok"]:
                    version_output = str(result.get("raw", "")).strip()
                    attested_version = _parse_vivado_version(version_output)
                    if attested_version != SUPPORTED_VIVADO_VERSION:
                        bootstrap_cleanup_error = ""
                        try:
                            bootstrap.unlink(missing_ok=True)
                        except OSError as exc:
                            bootstrap_cleanup_error = str(exc)
                        termination = _terminate_process_tree(self.process) if self.process is not None else {}
                        self.state = "TAINTED"
                        self.taint_reason = "unsupported_vivado_version" if attested_version else "vivado_version_unattested"
                        return {
                            "ok": False,
                            "error_code": "UNSUPPORTED_VIVADO_VERSION" if attested_version else "VIVADO_VERSION_UNATTESTED",
                            "message": (
                                f"Vivado {SUPPORTED_VIVADO_VERSION} is required; the connected process reported {attested_version}."
                                if attested_version
                                else "The connected Vivado process did not return an attested full version."
                            ),
                            "vivado_path": vivado["path"],
                            "version": attested_version,
                            "version_output": version_output,
                            "supported_version": SUPPORTED_VIVADO_VERSION,
                            "runtime_dir": str(runtime_dir),
                            "bootstrap_path": str(bootstrap),
                            "bootstrap_present": bootstrap.exists(),
                            "bootstrap_cleanup_error": bootstrap_cleanup_error,
                            "generation_id": self.generation_id,
                            "session_state": self.state,
                            "taint_reason": self.taint_reason,
                            "termination": termination,
                        }
                    try:
                        bootstrap.unlink(missing_ok=True)
                    except OSError as exc:
                        termination = _terminate_process_tree(self.process) if self.process is not None else {}
                        self.state = "TAINTED"
                        self.taint_reason = "bootstrap_secret_cleanup_failed"
                        return {
                            "ok": False,
                            "error_code": "BOOTSTRAP_SECRET_CLEANUP_FAILED",
                            "message": "Vivado connected, but the secret-bearing bootstrap file could not be removed.",
                            "runtime_dir": str(runtime_dir),
                            "bootstrap_path": str(bootstrap),
                            "bootstrap_cleanup_error": str(exc),
                            "generation_id": self.generation_id,
                            "session_state": self.state,
                            "taint_reason": self.taint_reason,
                            "termination": termination,
                        }
                    self._bootstrap_removed_after_handshake = True
                    self.state = "READY"
                    return {
                        "ok": True,
                        "backend": self.backend,
                        "host": self.host,
                        "port": self.port,
                        "vivado_path": vivado["path"],
                        "version": attested_version,
                        "version_output": version_output,
                        "runtime_dir": str(runtime_dir),
                        "temp_dir": str(runtime_dir),
                        "bootstrap_path": str(bootstrap),
                        "bootstrap_present": False,
                        "bootstrap_removed_after_handshake": True,
                        "runtime_identity": runtime_identity,
                        "generation_id": self.generation_id,
                        "session_state": self.state,
                        "transport_auth": TRANSPORT_AUTH,
                        "protocol_version": WIRE_PROTOCOL_VERSION,
                        "startup": self._startup_diagnostics(
                            command,
                            vivado_path=str(Path(str(vivado["path"])).resolve()),
                            phase="connected",
                            tcl_server_connected=True,
                        ),
                    }
                if result.get("protocol_authenticated") is False or result.get("request_accepted") is False:
                    termination = _terminate_process_tree(self.process) if self.process is not None else {}
                    self.state = "TAINTED"
                    self.taint_reason = result.get("taint_reason") or "tcl_authentication_failed"
                    return {
                        "ok": False,
                        "error_code": result.get("error_code") or "TCL_RESPONSE_AUTHENTICATION_FAILED",
                        "message": result.get("message") or "Vivado Tcl server response authentication failed.",
                        "runtime_dir": str(runtime_dir),
                        "bootstrap_path": str(bootstrap),
                        "generation_id": self.generation_id,
                        "session_state": self.state,
                        "taint_reason": self.taint_reason,
                        "termination": termination,
                        "request_status_line": result.get("status_line", ""),
                    }
            except OSError:
                time.sleep(1)
        terminated_after_timeout = False
        termination: dict[str, Any] = {}
        if self.process and self.process.poll() is None:
            termination = _terminate_process_tree(self.process)
            terminated_after_timeout = True
        self.state = "TAINTED"
        self.taint_reason = "startup_timeout"
        return {
            "ok": False,
            "error_code": "VIVADO_START_TIMEOUT",
            "message": f"Vivado Tcl server did not accept connections within {timeout_s}s.",
            "runtime_dir": str(runtime_dir),
            "temp_dir": str(runtime_dir),
            "bootstrap_path": str(bootstrap),
            "runtime_identity": runtime_identity,
            "generation_id": self.generation_id,
            "session_state": self.state,
            "taint_reason": self.taint_reason,
            "termination": termination,
            "startup": self._startup_diagnostics(
                command,
                vivado_path=str(Path(str(vivado["path"])).resolve()),
                terminated_after_timeout=terminated_after_timeout,
                phase="tcl_server_timeout",
            ),
        }

    def stop(self) -> dict[str, Any]:
        process = self.process
        termination: dict[str, Any] = {
            "attempted": False,
            "terminated": process is None or process.poll() is not None,
            "pid": process.pid if process is not None else None,
        }
        if process and process.poll() is None:
            termination = _terminate_process_tree(process)
        process_running = process is not None and process.poll() is None
        if process_running:
            self.state = "STOP_FAILED"
            return {
                "ok": False,
                "error_code": "SESSION_STOP_FAILED",
                "message": "Vivado process tree is still running after the stop attempt.",
                "backend": self.backend,
                "generation_id": self.generation_id,
                "session_state": self.state,
                "process_running": True,
                "termination": termination,
                **self._path_diagnostics(),
            }
        if self._bootstrap_path:
            try:
                self._bootstrap_path.unlink(missing_ok=True)
            except OSError as exc:
                termination["bootstrap_cleanup_error"] = str(exc)
        self.state = "STOPPED"
        data = {
            "ok": True,
            "backend": self.backend,
            "generation_id": self.generation_id,
            "session_state": self.state,
            "process_running": False,
            "termination": termination,
        }
        data.update(self._path_diagnostics())
        return data

    def status(self, *, probe_connection: bool = True) -> dict[str, Any]:
        running = self.process is not None and self.process.poll() is None
        connected = running and self.state == "READY"
        if probe_connection and self.state != "TAINTED":
            try:
                connected = self.run_tcl("version -short", timeout_s=2)["ok"]
            except OSError:
                connected = False
        return {
            "ok": True,
            "backend": self.backend,
            "host": self.host,
            "port": self.port,
            "process_running": running,
            "connected": connected,
            "generation_id": self.generation_id,
            "session_state": self.state,
            "tainted": self.state == "TAINTED",
            "taint_reason": self.taint_reason,
            "runtime_dir": str(self._runtime_path) if self._runtime_path is not None else "",
            "temp_dir": str(self._runtime_path) if self._runtime_path is not None else "",
            "bootstrap_path": str(self._bootstrap_path) if self._bootstrap_path is not None else "",
            "bootstrap_present": bool(self._bootstrap_path and self._bootstrap_path.exists()),
            "bootstrap_removed_after_handshake": self._bootstrap_removed_after_handshake,
            "stdout_path": str(self._stdout_path) if self._stdout_path is not None else "",
            "stderr_path": str(self._stderr_path) if self._stderr_path is not None else "",
        }

    def run_tcl(self, command: str, timeout_s: int = 60) -> dict[str, Any]:
        if self.state == "TAINTED":
            raise SessionTaintedError(self.generation_id, self.taint_reason)
        with self._request_lock:
            if EXECUTABLE_CONSTRAINT_BLOCK_MARKER in command:
                result = self._run_with_trusted_project_inputs(command, timeout_s=timeout_s)
            else:
                result = self._run_tcl_locked(command, timeout_s=timeout_s)
        return _normalize_project_guard_failure(result)

    @contextmanager
    def require_current_project(self, project_path: str | Path):
        expected = str(Path(project_path).expanduser().resolve())
        previous = getattr(self._project_guard_state, "expected_project_path", "")
        self._project_guard_state.expected_project_path = expected
        try:
            yield self
        finally:
            self._project_guard_state.expected_project_path = previous

    def _run_with_trusted_project_inputs(self, command: str, *, timeout_s: int) -> dict[str, Any]:
        preflight = self._run_tcl_locked(project_execution_inputs_command(), timeout_s=min(timeout_s, 60))
        if not preflight.get("ok"):
            return preflight
        inputs = parse_project_execution_inputs(str(preflight.get("raw", "")))
        version = str(inputs.get("vivado_version_short", ""))
        if version != SUPPORTED_VIVADO_VERSION:
            return self._constraint_policy_failure(
                f"trusted project execution requires Vivado {SUPPORTED_VIVADO_VERSION}; got {version or 'unknown'}"
            )
        constraints = list(inputs.get("constraints", []))
        discovery_errors = list(inputs.get("discovery_errors", []))
        if discovery_errors:
            return self._constraint_policy_failure(
                f"effective executable-input discovery failed: {discovery_errors[:8]}",
                error_code="EXECUTABLE_INPUT_DISCOVERY_FAILED",
            )
        composite_inputs = list(inputs.get("composite_inputs", []))
        if composite_inputs:
            return self._constraint_policy_failure(
                "IP/BD/XCI/DCP/custom-repository/OOC inputs are not yet accepted by the trusted execution closure: "
                f"{composite_inputs[:8]}",
                error_code="EXECUTABLE_COMPOSITE_INPUT_BLOCKED",
            )
        design_identity = build_design_execution_identity(inputs)
        if design_identity.get("status") != "READY":
            return self._constraint_policy_failure(
                f"design execution identity is incomplete: {design_identity.get('issues', [])[:8]}",
                error_code="DESIGN_EXECUTION_IDENTITY_BLOCKED",
            )
        grouped_files: dict[Path, list[Path]] = {}
        identity_files = design_identity.get("identity", {}).get("files", [])
        for item in identity_files:
            path = Path(str(item.get("path", ""))).expanduser().resolve()
            grouped_files.setdefault(path.parent, []).append(path)
        for item in constraints:
            path = Path(str(item.get("path", ""))).expanduser().resolve()
            file_type = str(item.get("type", "")).strip()
            if path.suffix.lower() != ".xdc" or file_type.lower() != "xdc":
                return self._constraint_policy_failure(
                    f"constraint input must be a .xdc regular file with FILE_TYPE XDC: {path} ({file_type or 'unknown'})"
                )
        try:
            with ExitStack() as stack:
                for parent, files in grouped_files.items():
                    stack.enter_context(
                        hold_managed_paths_stable(
                            parent,
                            files=files,
                            directories=[parent],
                        )
                    )
                for files in grouped_files.values():
                    for path in files:
                        if path.suffix.lower() != ".xdc":
                            continue
                        content = read_stable_bytes(path, root=path.parent, max_bytes=MAX_TRUSTED_XDC_BYTES)
                        try:
                            text = content.decode("utf-8-sig")
                        except UnicodeDecodeError as exc:
                            raise ManagedPathError(f"constraint input is not valid UTF-8: {path}") from exc
                        issues = validate_xdc_text(text)
                        if issues:
                            raise ManagedPathError(f"untrusted XDC content in {path}: {'; '.join(issues[:8])}")
                identity_issues = verify_design_execution_identity_files(design_identity)
                if identity_issues:
                    return self._constraint_policy_failure(
                        f"design execution source closure changed before launch: {identity_issues[:8]}",
                        error_code="SOURCE_CLOSURE_CHANGED",
                    )
                result = self._run_tcl_locked(command, timeout_s=timeout_s)
                result["design_execution_identity"] = design_identity
                result["design_execution_identity_sha256"] = str(design_identity.get("sha256", ""))
                return result
        except (ManagedPathError, OSError, ValueError) as exc:
            return self._constraint_policy_failure(str(exc))

    def _constraint_policy_failure(
        self,
        message: str,
        *,
        error_code: str = "EXECUTABLE_CONSTRAINT_INPUT_BLOCKED",
    ) -> dict[str, Any]:
        raw = f"{EXECUTABLE_CONSTRAINT_BLOCK_MARKER}: {message}"
        return {
            "ok": False,
            "request_id": "",
            "raw": raw,
            "status_line": "",
            "error_code": error_code,
            "message": message,
            "protocol_authenticated": True,
            "request_accepted": True,
            "protocol_version": WIRE_PROTOCOL_VERSION,
            "transport_auth": TRANSPORT_AUTH,
            "generation_id": self.generation_id,
            "sequence": self._sequence,
            "committed_sequence": self._sequence,
        }

    def _run_tcl_locked(self, command: str, *, timeout_s: int) -> dict[str, Any]:
        expected_project_path = str(getattr(self._project_guard_state, "expected_project_path", ""))
        if expected_project_path:
            command = f"{active_project_identity_guard_command(expected_project_path)}; {command}"
        request_id = f"vmcp_{uuid.uuid4().hex}"
        encoded = command.encode("utf-8").hex()
        candidate_sequence = self._sequence + 1
        signature = _request_signature(
            secret_hex=self._auth_secret,
            request_id=request_id,
            sequence=candidate_sequence,
            encoded_command=encoded,
        )
        payload = (
            f"{WIRE_PROTOCOL_VERSION} {request_id} {candidate_sequence} {signature} {encoded}\n"
        ).encode("ascii")
        request_sent = False
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout_s) as sock:
                sock.settimeout(timeout_s)
                sock.sendall(payload)
                request_sent = True
                raw = _read_until_end(sock, request_id)
        except TimeoutError:
            if request_sent:
                self._mark_tainted("tcl_request_timeout")
            raise
        except TclResponseProtocolError as exc:
            self._mark_tainted(exc.taint_reason)
            return self._protocol_failure(
                request_id=request_id,
                sequence=candidate_sequence,
                error_code=exc.error_code,
                message=str(exc),
                taint_reason=exc.taint_reason,
            )
        except OSError as exc:
            if not request_sent:
                raise
            self._mark_tainted("tcl_response_transport_failure")
            return self._protocol_failure(
                request_id=request_id,
                sequence=candidate_sequence,
                error_code="TCL_RESPONSE_TRANSPORT_FAILURE",
                message=f"Vivado Tcl response transport failed after the request was sent: {exc}",
                taint_reason=self.taint_reason,
            )

        try:
            response = _parse_authenticated_response(
                raw,
                secret_hex=self._auth_secret,
                request_id=request_id,
                expected_sequence=candidate_sequence,
            )
        except TclResponseProtocolError as exc:
            self._mark_tainted(exc.taint_reason)
            return self._protocol_failure(
                request_id=request_id,
                sequence=candidate_sequence,
                error_code=exc.error_code,
                message=str(exc),
                taint_reason=exc.taint_reason,
            )

        if not response["request_accepted"]:
            self._mark_tainted("tcl_request_rejected")
            return self._protocol_failure(
                request_id=request_id,
                sequence=candidate_sequence,
                error_code="TCL_REQUEST_REJECTED",
                message="Vivado Tcl server rejected the authenticated request or sequence.",
                taint_reason=self.taint_reason,
                protocol_authenticated=True,
                status_line=response["status_line"],
            )

        self._sequence = candidate_sequence
        ok = response["status"] == 0
        return {
            "ok": ok,
            "request_id": request_id,
            "raw": response["body"].strip(),
            "status_line": response["status_line"],
            "error_code": "" if ok else "TCL_COMMAND_FAILED",
            "protocol_authenticated": True,
            "request_accepted": True,
            "protocol_version": WIRE_PROTOCOL_VERSION,
            "transport_auth": TRANSPORT_AUTH,
            "generation_id": self.generation_id,
            "sequence": candidate_sequence,
            "committed_sequence": self._sequence,
        }

    def _protocol_failure(
        self,
        *,
        request_id: str,
        sequence: int,
        error_code: str,
        message: str,
        taint_reason: str,
        protocol_authenticated: bool = False,
        status_line: str = "",
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "request_id": request_id,
            "raw": "",
            "status_line": status_line,
            "error_code": error_code,
            "message": message,
            "protocol_authenticated": protocol_authenticated,
            "request_accepted": False,
            "protocol_version": WIRE_PROTOCOL_VERSION,
            "transport_auth": TRANSPORT_AUTH,
            "generation_id": self.generation_id,
            "sequence": sequence,
            "committed_sequence": self._sequence,
            "session_state": self.state,
            "taint_reason": taint_reason,
        }

    def _mark_tainted(self, reason: str) -> None:
        self.state = "TAINTED"
        self.taint_reason = reason
        if self.process is not None and self.process.poll() is None:
            _terminate_process_tree(self.process)

    def _path_diagnostics(self) -> dict[str, str]:
        return {
            "runtime_dir": str(self._runtime_path) if self._runtime_path is not None else "",
            "temp_dir": str(self._runtime_path) if self._runtime_path is not None else "",
            "bootstrap_path": str(self._bootstrap_path) if self._bootstrap_path is not None else "",
            "bootstrap_present": bool(self._bootstrap_path and self._bootstrap_path.exists()),
            "bootstrap_removed_after_handshake": self._bootstrap_removed_after_handshake,
            "stdout_path": str(self._stdout_path) if self._stdout_path is not None else "",
            "stderr_path": str(self._stderr_path) if self._stderr_path is not None else "",
        }

    def _startup_diagnostics(
        self,
        command: list[str],
        *,
        vivado_path: str,
        terminated_after_timeout: bool = False,
        phase: str = "",
        tcl_server_connected: bool = False,
    ) -> dict[str, Any]:
        returncode = self.process.poll() if self.process else None
        resolved_phase = phase
        if not resolved_phase:
            if self.process is None:
                resolved_phase = "not_started"
            elif tcl_server_connected:
                resolved_phase = "connected"
            elif returncode is not None:
                resolved_phase = "process_exited"
            elif terminated_after_timeout:
                resolved_phase = "tcl_server_timeout"
            else:
                resolved_phase = "waiting_for_tcl_server"
        return {
            "phase": resolved_phase,
            "command": command,
            "vivado_path": vivado_path,
            "vivado_process_started": self.process is not None,
            "tcl_server_connected": tcl_server_connected,
            "recommended_retry_timeout_s": RECOMMENDED_RETRY_TIMEOUT_S,
            "pid": self.process.pid if self.process else None,
            "returncode": returncode,
            "process_running": self.process is not None and returncode is None,
            "terminated_after_timeout": terminated_after_timeout,
            "stdout_path": str(self._stdout_path) if self._stdout_path else "",
            "stderr_path": str(self._stderr_path) if self._stderr_path else "",
            "stdout_tail": _read_tail(self._stdout_path),
            "stderr_tail": _read_tail(self._stderr_path),
        }


@dataclass
class SessionManager:
    session: GuiTcpVivadoSession | None = field(default=None)
    trusted_vivado_identity: dict[str, Any] = field(default_factory=capture_server_vivado_identity, repr=False)

    def start(
        self,
        vivado_path: str | None = None,
        port: int = 0,
        timeout_s: int = DEFAULT_START_TIMEOUT_S,
        runtime_dir: str | None = None,
    ) -> dict[str, Any]:
        if vivado_path is not None:
            path_assertion = validate_trusted_vivado_executable(
                vivado_path,
                trusted_identity=self.trusted_vivado_identity,
            )
            if not path_assertion.get("ok"):
                return _session_path_assertion_failure(path_assertion)
        if self.session:
            try:
                status = self.session.status(probe_connection=True)
            except TypeError:
                status = self.session.status()
            if status.get("connected") is True and status.get("process_running") is True:
                return status
            stop_result = self.session.stop()
            if not stop_result.get("ok"):
                return {
                    **stop_result,
                    "error_code": "PREVIOUS_SESSION_STOP_FAILED",
                    "message": "Could not stop the previous Vivado session; refusing to start a second managed process.",
                }
            self.session = None
        path_assertion = validate_trusted_vivado_executable(
            vivado_path,
            trusted_identity=self.trusted_vivado_identity,
        )
        if not path_assertion.get("ok"):
            return _session_path_assertion_failure(path_assertion)
        self.session = GuiTcpVivadoSession(
            port=port,
            vivado_path=vivado_path,
            runtime_dir=runtime_dir,
            trusted_vivado_identity=self.trusted_vivado_identity,
        )
        result = self.session.start(timeout_s=timeout_s)
        if not result["ok"] and not (
            self.session.process is not None and self.session.process.poll() is None
        ):
            self.session = None
        return result

    def stop(self) -> dict[str, Any]:
        if not self.session:
            return {"ok": True, "backend": "none", "stopped": False}
        result = self.session.stop()
        if result.get("ok"):
            self.session = None
            return result | {"stopped": True}
        return result | {"stopped": False}

    def current(self) -> GuiTcpVivadoSession:
        if not self.session:
            raise RuntimeError("Vivado session is not started")
        return self.session

    def status(self) -> dict[str, Any]:
        if not self.session:
            return {"ok": True, "connected": False, "backend": "none"}
        try:
            return self.session.status(probe_connection=False)
        except TypeError:
            return self.session.status()


def active_project_identity_guard_command(expected_project_path: str | Path) -> str:
    expected = str(Path(expected_project_path).expanduser().resolve())
    marker = PROJECT_ACTIVE_IDENTITY_MISMATCH_MARKER
    return "; ".join(
        [
            f"set vmcp_expected_project_path [file normalize {tcl_list_quote(expected)}]",
            (
                "if {[catch {set vmcp_active_project [current_project]} vmcp_active_project_error]} {"
                f"error \"{marker}: current_project query failed: $vmcp_active_project_error\""
                "}"
            ),
            (
                "if {[catch {set vmcp_active_project_dir [file normalize [get_property DIRECTORY $vmcp_active_project]]} "
                "vmcp_active_project_error]} {"
                f"error \"{marker}: project directory query failed: $vmcp_active_project_error\""
                "}"
            ),
            (
                "if {[catch {set vmcp_active_project_name [get_property NAME $vmcp_active_project]} "
                "vmcp_active_project_error]} {"
                f"error \"{marker}: project name query failed: $vmcp_active_project_error\""
                "}"
            ),
            "set vmcp_active_project_path [file normalize [file join $vmcp_active_project_dir ${vmcp_active_project_name}.xpr]]",
            (
                "if {![string equal -nocase $vmcp_expected_project_path $vmcp_active_project_path]} {"
                f"error \"{marker}: expected=$vmcp_expected_project_path actual=$vmcp_active_project_path\""
                "}"
            ),
        ]
    )


def _normalize_project_guard_failure(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok") is not False:
        return result
    text = "\n".join(str(result.get(key, "")) for key in ("raw", "message", "status_line"))
    if PROJECT_ACTIVE_IDENTITY_MISMATCH_MARKER not in text:
        return result
    return {
        **result,
        "error_code": "PROJECT_ACTIVE_IDENTITY_MISMATCH",
        "message": "Vivado current_project does not match the managed project capability.",
    }


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _read_until_end(sock: socket.socket, request_id: str) -> bytes:
    response = bytearray()
    end_marker = f"\n{WIRE_PROTOCOL_VERSION} {request_id} END\n".encode("ascii")
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise TclResponseProtocolError(
                "TCL_RESPONSE_TRUNCATED",
                "Vivado Tcl response closed before the authenticated end marker.",
                taint_reason="tcl_response_truncated",
            )
        response.extend(chunk)
        if len(response) > MAX_RESPONSE_ENVELOPE_BYTES:
            raise TclResponseProtocolError(
                "TCL_RESPONSE_TOO_LARGE",
                "Vivado Tcl response exceeded the authenticated envelope limit.",
                taint_reason="tcl_response_too_large",
            )
        header_end = response.find(b"\n")
        if header_end >= 0:
            try:
                header_fields = bytes(response[:header_end]).decode("ascii").split()
                if len(header_fields) == 14 and header_fields[8] == "LENGTH":
                    if int(header_fields[9]) > MAX_RESPONSE_BODY_BYTES:
                        raise TclResponseProtocolError(
                            "TCL_RESPONSE_TOO_LARGE",
                            "Vivado Tcl response declared a body larger than the allowed limit.",
                            taint_reason="tcl_response_too_large",
                        )
            except (UnicodeDecodeError, ValueError):
                pass
        if response.endswith(end_marker):
            return bytes(response)


def _request_signature(*, secret_hex: str, request_id: str, sequence: int, encoded_command: str) -> str:
    signed_payload = (
        f"{WIRE_PROTOCOL_VERSION}\n{request_id}\n{sequence}\n{encoded_command}"
    ).encode("ascii")
    return hmac.new(bytes.fromhex(secret_hex), signed_payload, hashlib.sha256).hexdigest()


def _response_signature(
    *,
    secret_hex: str,
    request_id: str,
    sequence: int,
    status: int,
    request_accepted: bool,
    body_length: int,
    body_sha256: str,
    encoded_body: str,
) -> str:
    signed_payload = (
        f"{WIRE_PROTOCOL_VERSION}\n{request_id}\n{sequence}\n{status}\n"
        f"{int(request_accepted)}\n{body_length}\n{body_sha256}\n{encoded_body}"
    ).encode("ascii")
    return hmac.new(bytes.fromhex(secret_hex), signed_payload, hashlib.sha256).hexdigest()


def _parse_authenticated_response(
    raw: bytes,
    *,
    secret_hex: str,
    request_id: str,
    expected_sequence: int,
) -> dict[str, Any]:
    end_marker = f"\n{WIRE_PROTOCOL_VERSION} {request_id} END\n".encode("ascii")
    if not raw.endswith(end_marker):
        raise TclResponseProtocolError(
            "TCL_RESPONSE_TRUNCATED",
            "Vivado Tcl response is missing its authenticated end marker.",
            taint_reason="tcl_response_truncated",
        )
    envelope = raw[: -len(end_marker)]
    header_bytes, separator, encoded_body_bytes = envelope.partition(b"\n")
    if not separator:
        raise TclResponseProtocolError(
            "TCL_RESPONSE_MALFORMED",
            "Vivado Tcl response is missing its body envelope.",
            taint_reason="tcl_response_malformed",
        )
    try:
        status_line = header_bytes.decode("ascii")
        fields = status_line.split()
        if len(fields) != 14:
            raise ValueError("unexpected response header field count")
        protocol, returned_request_id = fields[0], fields[1]
        labels = fields[2::2]
        if labels != ["STATUS", "SEQUENCE", "ACCEPTED", "LENGTH", "SHA256", "HMAC"]:
            raise ValueError("unexpected response header labels")
        status = int(fields[3])
        sequence = int(fields[5])
        accepted_value = int(fields[7])
        body_length = int(fields[9])
        body_sha256 = fields[11].lower()
        response_hmac = fields[13].lower()
        encoded_body = encoded_body_bytes.decode("ascii")
    except (UnicodeDecodeError, ValueError, IndexError) as exc:
        raise TclResponseProtocolError(
            "TCL_RESPONSE_MALFORMED",
            f"Vivado Tcl response envelope is malformed: {exc}",
            taint_reason="tcl_response_malformed",
        ) from exc

    if protocol != WIRE_PROTOCOL_VERSION:
        raise TclResponseProtocolError(
            "TCL_PROTOCOL_VERSION_MISMATCH",
            f"Vivado Tcl response protocol {protocol!r} does not match {WIRE_PROTOCOL_VERSION}.",
            taint_reason="tcl_protocol_version_mismatch",
        )
    if returned_request_id != request_id:
        raise TclResponseProtocolError(
            "TCL_RESPONSE_REQUEST_MISMATCH",
            "Vivado Tcl response request ID does not match the current request.",
            taint_reason="tcl_response_request_mismatch",
        )
    if sequence != expected_sequence:
        raise TclResponseProtocolError(
            "TCL_RESPONSE_SEQUENCE_MISMATCH",
            "Vivado Tcl response sequence does not match the current request.",
            taint_reason="tcl_response_sequence_mismatch",
        )
    if accepted_value not in {0, 1} or status < 0 or body_length < 0:
        raise TclResponseProtocolError(
            "TCL_RESPONSE_MALFORMED",
            "Vivado Tcl response status or accepted flag is invalid.",
            taint_reason="tcl_response_malformed",
        )
    if body_length > MAX_RESPONSE_BODY_BYTES or len(encoded_body_bytes) > MAX_RESPONSE_BODY_BYTES * 2:
        raise TclResponseProtocolError(
            "TCL_RESPONSE_TOO_LARGE",
            "Vivado Tcl response body exceeded the allowed limit.",
            taint_reason="tcl_response_too_large",
        )
    if len(body_sha256) != 64 or any(character not in "0123456789abcdef" for character in body_sha256):
        raise TclResponseProtocolError(
            "TCL_RESPONSE_MALFORMED",
            "Vivado Tcl response body digest is invalid.",
            taint_reason="tcl_response_malformed",
        )
    if len(response_hmac) != 64 or any(character not in "0123456789abcdef" for character in response_hmac):
        raise TclResponseProtocolError(
            "TCL_RESPONSE_AUTHENTICATION_FAILED",
            "Vivado Tcl response HMAC is missing or invalid.",
            taint_reason="tcl_response_authentication_failed",
        )
    if len(encoded_body) % 2 or any(character not in "0123456789abcdefABCDEF" for character in encoded_body):
        raise TclResponseProtocolError(
            "TCL_RESPONSE_MALFORMED",
            "Vivado Tcl response body encoding is invalid.",
            taint_reason="tcl_response_malformed",
        )
    if len(encoded_body) != body_length * 2:
        raise TclResponseProtocolError(
            "TCL_RESPONSE_LENGTH_MISMATCH",
            "Vivado Tcl response encoded body length does not match the authenticated header.",
            taint_reason="tcl_response_length_mismatch",
        )

    expected_hmac = _response_signature(
        secret_hex=secret_hex,
        request_id=request_id,
        sequence=sequence,
        status=status,
        request_accepted=bool(accepted_value),
        body_length=body_length,
        body_sha256=body_sha256,
        encoded_body=encoded_body.lower(),
    )
    if not hmac.compare_digest(response_hmac, expected_hmac):
        raise TclResponseProtocolError(
            "TCL_RESPONSE_AUTHENTICATION_FAILED",
            "Vivado Tcl response HMAC verification failed.",
            taint_reason="tcl_response_authentication_failed",
        )
    body_bytes = bytes.fromhex(encoded_body)
    if len(body_bytes) != body_length:
        raise TclResponseProtocolError(
            "TCL_RESPONSE_LENGTH_MISMATCH",
            "Vivado Tcl response body length does not match the authenticated header.",
            taint_reason="tcl_response_length_mismatch",
        )
    if not hmac.compare_digest(hashlib.sha256(body_bytes).hexdigest(), body_sha256):
        raise TclResponseProtocolError(
            "TCL_RESPONSE_DIGEST_MISMATCH",
            "Vivado Tcl response body digest does not match the authenticated header.",
            taint_reason="tcl_response_digest_mismatch",
        )
    try:
        body = body_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TclResponseProtocolError(
            "TCL_RESPONSE_ENCODING_INVALID",
            "Vivado Tcl response body is not valid UTF-8.",
            taint_reason="tcl_response_encoding_invalid",
        ) from exc
    return {
        "status": status,
        "body": body,
        "request_accepted": bool(accepted_value),
        "status_line": status_line,
    }


def _read_tail(path: Path | None, *, max_bytes: int = 8192) -> str:
    if path is None or not path.exists():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read(max_bytes)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()


def _creation_flags() -> int:
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        return subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def _session_path_assertion_failure(path_assertion: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": str(path_assertion.get("error_code") or "VIVADO_PATH_MISMATCH"),
        "message": str(path_assertion.get("message") or "Vivado path assertion failed."),
        "execution_attempted": False,
        "data": {
            **path_assertion,
            "handler_executed": False,
            "path_assertion_checked": True,
        },
    }


def _spawn_vivado_process(
    command: list[str],
    *,
    trusted_identity: dict[str, Any],
    **kwargs: Any,
) -> subprocess.Popen:
    if not command:
        raise TrustedVivadoPathError(
            "VIVADO_PATH_TRUST_STATE_INVALID",
            "Vivado process command is empty.",
        )
    validated = validate_trusted_vivado_executable(command[0], trusted_identity=trusted_identity)
    if not validated.get("ok"):
        raise TrustedVivadoPathError(
            str(validated.get("error_code") or "VIVADO_PATH_IDENTITY_CHANGED"),
            str(validated.get("message") or "Vivado executable identity validation failed before process spawn."),
            data=validated,
        )
    trusted_command = [str(validated["canonical_path"]), *command[1:]]
    return subprocess.Popen(trusted_command, **kwargs)


def _terminate_process_tree(process: subprocess.Popen) -> dict[str, Any]:
    result: dict[str, Any] = {
        "attempted": True,
        "terminated": False,
        "pid": process.pid,
        "method": "taskkill" if os.name == "nt" else "process_group",
        "error": "",
    }
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            result["taskkill_returncode"] = completed.returncode
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            result["terminated"] = process.poll() is not None
        except OSError as exc:
            result["error"] = str(exc)
        return result
    try:
        process_group = os.getpgid(process.pid)
        os.killpg(process_group, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process_group, signal.SIGKILL)
            process.wait(timeout=5)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired) as exc:
        result["error"] = str(exc)
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as fallback_exc:
            result["error"] = f"{result['error']}; fallback: {fallback_exc}".strip("; ")
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as kill_exc:
                result["error"] = f"{result['error']}; kill: {kill_exc}".strip("; ")
    result["terminated"] = process.poll() is not None
    return result


class SessionTaintedError(RuntimeError):
    def __init__(self, generation_id: str, reason: str) -> None:
        super().__init__("Vivado session is tainted after an indeterminate Tcl timeout; start a new session.")
        self.data = {
            "generation_id": generation_id,
            "session_state": "TAINTED",
            "taint_reason": reason,
        }
