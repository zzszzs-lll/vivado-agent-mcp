import hashlib
import socketserver
import threading
import time
import tkinter
from pathlib import Path

import pytest

import vivado_agent_mcp.vivado.session as session_module
import vivado_agent_mcp.vivado.bootstrap as bootstrap_module
from vivado_agent_mcp.vivado.bootstrap import BootstrapPermissionError, write_bootstrap
from vivado_agent_mcp.vivado.env import TrustedVivadoPathError, capture_server_vivado_identity
from vivado_agent_mcp.vivado.session import (
    PROJECT_ACTIVE_IDENTITY_MISMATCH_MARKER,
    MAX_RESPONSE_BODY_BYTES,
    TRANSPORT_AUTH,
    WIRE_PROTOCOL_VERSION,
    GuiTcpVivadoSession,
    SessionManager,
    SessionTaintedError,
    active_project_identity_guard_command,
    _request_signature,
    _response_signature,
)
from vivado_agent_mcp.vivado.runs import EXECUTABLE_CONSTRAINT_BLOCK_MARKER
from vivado_agent_mcp.vivado.wire import encode_wire_row


def _signed_response(
    *,
    secret: str,
    request_id: str,
    sequence: int,
    body: str,
    status: int = 0,
    accepted: bool = True,
    protocol: str = WIRE_PROTOCOL_VERSION,
    declared_length: int | None = None,
    declared_sha256: str | None = None,
    signature_request_id: str | None = None,
    signature_sequence: int | None = None,
    signature_status: int | None = None,
) -> bytes:
    body_bytes = body.encode("utf-8")
    encoded_body = body_bytes.hex()
    body_length = len(body_bytes) if declared_length is None else declared_length
    body_sha256 = hashlib.sha256(body_bytes).hexdigest() if declared_sha256 is None else declared_sha256
    response_hmac = _response_signature(
        secret_hex=secret,
        request_id=signature_request_id or request_id,
        sequence=sequence if signature_sequence is None else signature_sequence,
        status=status if signature_status is None else signature_status,
        request_accepted=accepted,
        body_length=body_length,
        body_sha256=body_sha256,
        encoded_body=encoded_body,
    )
    header = (
        f"{protocol} {request_id} STATUS {status} SEQUENCE {sequence} ACCEPTED {int(accepted)} "
        f"LENGTH {body_length} SHA256 {body_sha256} HMAC {response_hmac}"
    )
    return f"{header}\n{encoded_body}\n{WIRE_PROTOCOL_VERSION} {request_id} END\n".encode("ascii")


class _Handler(socketserver.StreamRequestHandler):
    seen_command = ""
    seen_sequence = 0
    expected_secret = ""

    def handle(self) -> None:
        line = self.rfile.readline().decode("utf-8")
        protocol, request, sequence, signature, encoded = line.strip().split(" ", 4)
        if protocol != WIRE_PROTOCOL_VERSION:
            return
        expected_signature = _request_signature(
            secret_hex=self.__class__.expected_secret,
            request_id=request,
            sequence=int(sequence),
            encoded_command=encoded,
        )
        if signature != expected_signature:
            self.wfile.write(
                _signed_response(
                    secret=self.__class__.expected_secret,
                    request_id=request,
                    sequence=int(sequence),
                    body="authentication failed",
                    status=1,
                    accepted=False,
                )
            )
            return
        self.__class__.seen_sequence = int(sequence)
        self.__class__.seen_command = bytes.fromhex(encoded).decode("utf-8")
        self.wfile.write(
            _signed_response(
                secret=self.__class__.expected_secret,
                request_id=request,
                sequence=int(sequence),
                body="RESULT: 2021.2",
            )
        )


class _AdversarialResponseHandler(socketserver.StreamRequestHandler):
    expected_secret = ""
    mode = "unauthenticated"

    def handle(self) -> None:
        protocol, request_id, sequence_text, _signature, _encoded = (
            self.rfile.readline().decode("ascii").strip().split(" ", 4)
        )
        assert protocol == WIRE_PROTOCOL_VERSION
        sequence = int(sequence_text)
        mode = self.__class__.mode
        if mode == "unauthenticated":
            response = _signed_response(
                secret="0" * 64,
                request_id=request_id,
                sequence=sequence,
                body="forged READY",
            )
        elif mode == "tampered_body":
            response = _signed_response(
                secret=self.__class__.expected_secret,
                request_id=request_id,
                sequence=sequence,
                body="ORIGINAL",
            ).replace(b"4f524947494e414c", b"54414d5045524544", 1)
        elif mode == "tampered_status":
            response = _signed_response(
                secret=self.__class__.expected_secret,
                request_id=request_id,
                sequence=sequence,
                body="status changed",
                status=1,
                signature_status=0,
            )
        elif mode == "wrong_sequence":
            response = _signed_response(
                secret=self.__class__.expected_secret,
                request_id=request_id,
                sequence=sequence + 1,
                signature_sequence=sequence,
                body="wrong sequence",
            )
        elif mode == "old_protocol":
            response = _signed_response(
                secret=self.__class__.expected_secret,
                request_id=request_id,
                sequence=sequence,
                body="old protocol",
                protocol="VMCP1",
            )
        elif mode == "malformed_length":
            response = _signed_response(
                secret=self.__class__.expected_secret,
                request_id=request_id,
                sequence=sequence,
                body="short",
                declared_length=99,
            )
        elif mode == "truncated":
            response = _signed_response(
                secret=self.__class__.expected_secret,
                request_id=request_id,
                sequence=sequence,
                body="partial",
            ).split(f"\n{WIRE_PROTOCOL_VERSION} ".encode("ascii"), 1)[0]
        else:
            raise AssertionError(f"unknown adversarial response mode: {mode}")
        self.wfile.write(response)


def test_bootstrap_requires_session_secret_and_monotonic_sequence(tmp_path: Path) -> None:
    bootstrap = tmp_path / "bootstrap.tcl"
    write_bootstrap(bootstrap, "127.0.0.1", 12345, "a" * 64)

    text = bootstrap.read_text(encoding="utf-8")
    assert "a" * 64 in text
    assert "presented_hmac" in text
    assert "presented_secret" not in text
    assert "vivado_agent_mcp_hmac_sha256" in text
    assert "vivado_agent_mcp_last_sequence" in text
    assert "vivado_agent_mcp_protocol_version VMCP2" in text
    assert "response_hmac" in text
    assert "body_sha256" in text
    assert "authentication failed" in text
    assert "request replay or sequence mismatch" in text
    assert "vivado_agent_mcp_max_response_body_bytes 8388608" in text
    assert "VMCP_RESPONSE_BODY_TOO_LARGE" in text


def test_authenticated_response_rejects_declared_body_over_limit() -> None:
    secret = "a" * 64
    request_id = "vmcp_0123456789abcdef0123456789abcdef"
    response = _signed_response(
        secret=secret,
        request_id=request_id,
        sequence=1,
        body="small",
        declared_length=MAX_RESPONSE_BODY_BYTES + 1,
    )

    with pytest.raises(session_module.TclResponseProtocolError) as exc_info:
        session_module._parse_authenticated_response(
            response,
            secret_hex=secret,
            request_id=request_id,
            expected_sequence=1,
        )

    assert exc_info.value.error_code == "TCL_RESPONSE_TOO_LARGE"


def test_bootstrap_hmac_matches_python_sha256(tmp_path: Path) -> None:
    secret = "a" * 64
    request_id = "vmcp_0123456789abcdef"
    sequence = 7
    encoded = "puts hello".encode("utf-8").hex()
    payload = f"{WIRE_PROTOCOL_VERSION}\n{request_id}\n{sequence}\n{encoded}"
    bootstrap = tmp_path / "bootstrap.tcl"
    write_bootstrap(bootstrap, "127.0.0.1", 12345, secret)

    helper_script = bootstrap.read_text(encoding="utf-8").split("proc ::vivado_agent_mcp_handle", 1)[0]
    try:
        interpreter = tkinter.Tcl()
    except tkinter.TclError as exc:
        pytest.skip(f"Python Tcl runtime is unavailable: {exc}")
    interpreter.eval(helper_script)
    tcl_signature = interpreter.call("::vivado_agent_mcp_hmac_sha256", secret, payload)

    assert tcl_signature == _request_signature(
        secret_hex=secret,
        request_id=request_id,
        sequence=sequence,
        encoded_command=encoded,
    )

    body_bytes = "READY\n证据".encode("utf-8")
    encoded_body = body_bytes.hex()
    body_sha256 = hashlib.sha256(body_bytes).hexdigest()
    response_payload = (
        f"{WIRE_PROTOCOL_VERSION}\n{request_id}\n{sequence}\n0\n1\n"
        f"{len(body_bytes)}\n{body_sha256}\n{encoded_body}"
    )
    tcl_response_signature = interpreter.call(
        "::vivado_agent_mcp_hmac_sha256",
        secret,
        response_payload,
    )
    assert tcl_response_signature == _response_signature(
        secret_hex=secret,
        request_id=request_id,
        sequence=sequence,
        status=0,
        request_accepted=True,
        body_length=len(body_bytes),
        body_sha256=body_sha256,
        encoded_body=encoded_body,
    )


def test_bootstrap_windows_acl_is_restricted_to_current_sid(tmp_path: Path, monkeypatch) -> None:
    bootstrap = tmp_path / "bootstrap.tcl"
    bootstrap.write_text("secret", encoding="utf-8")
    calls = []

    def fake_icacls(path: Path, sid: str) -> int:
        calls.append((path, sid))
        return 0

    monkeypatch.setattr(bootstrap_module, "_current_windows_sid", lambda: "S-1-5-21-1234")
    monkeypatch.setattr(bootstrap_module, "_run_icacls", fake_icacls)

    bootstrap_module._restrict_bootstrap_permissions(bootstrap, platform_name="nt")

    assert calls == [(bootstrap, "S-1-5-21-1234")]


def test_icacls_output_is_captured_away_from_mcp_stdio(tmp_path: Path, monkeypatch) -> None:
    bootstrap = tmp_path / "bootstrap.tcl"
    bootstrap.write_text("secret", encoding="utf-8")
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return bootstrap_module.subprocess.CompletedProcess(command, 0, stdout=b"processed", stderr=b"")

    monkeypatch.setattr(bootstrap_module.subprocess, "run", fake_run)

    assert bootstrap_module._run_icacls(bootstrap, "S-1-5-21-1234") == 0
    assert observed["kwargs"]["stdout"] is bootstrap_module.subprocess.PIPE
    assert observed["kwargs"]["stderr"] is bootstrap_module.subprocess.PIPE
    assert observed["kwargs"]["stdin"] is bootstrap_module.subprocess.DEVNULL


def test_bootstrap_permission_failure_removes_secret_file(tmp_path: Path, monkeypatch) -> None:
    bootstrap = tmp_path / "bootstrap.tcl"

    def reject_permissions(path: Path) -> None:
        raise BootstrapPermissionError("ACL failure")

    monkeypatch.setattr(bootstrap_module, "_restrict_bootstrap_permissions", reject_permissions)

    with pytest.raises(BootstrapPermissionError, match="ACL failure"):
        write_bootstrap(bootstrap, "127.0.0.1", 12345, "a" * 64)

    assert bootstrap.exists() is False


def test_gui_tcp_session_round_trips_request_id() -> None:
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        session = GuiTcpVivadoSession(host="127.0.0.1", port=server.server_address[1])
        _Handler.expected_secret = session._auth_secret
        result = session.run_tcl("puts hello\nversion -short", timeout_s=2)

        server.shutdown()

    assert result["ok"] is True
    assert result["request_id"].startswith("vmcp_")
    assert "RESULT: 2021.2" in result["raw"]
    assert _Handler.seen_command == "puts hello\nversion -short"
    assert _Handler.seen_sequence == 1
    assert result["sequence"] == 1
    assert result["committed_sequence"] == 1
    assert result["protocol_authenticated"] is True
    assert result["request_accepted"] is True
    assert result["protocol_version"] == WIRE_PROTOCOL_VERSION
    assert result["transport_auth"] == TRANSPORT_AUTH


def test_gui_tcp_session_binds_expected_project_inside_same_tcl_request(tmp_path: Path) -> None:
    expected_project = tmp_path / "project-a" / "demo.xpr"
    expected_project.parent.mkdir()
    expected_project.write_text("# project a\n", encoding="utf-8")

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = GuiTcpVivadoSession(host="127.0.0.1", port=server.server_address[1])
        _Handler.expected_secret = session._auth_secret

        with session.require_current_project(expected_project):
            result = session.run_tcl("set_property TOP {demo_top} [current_fileset]", timeout_s=2)
        server.shutdown()

    assert result["ok"] is True
    assert PROJECT_ACTIVE_IDENTITY_MISMATCH_MARKER in _Handler.seen_command
    assert str(expected_project.resolve()).replace("\\", "/") in _Handler.seen_command.replace("\\", "/")
    assert _Handler.seen_command.index(PROJECT_ACTIVE_IDENTITY_MISMATCH_MARKER) < _Handler.seen_command.index("set_property TOP")


def test_active_project_guard_blocks_before_side_effect_for_mismatch_or_empty_project(tmp_path: Path) -> None:
    project_a = tmp_path / "project-a" / "demo.xpr"
    project_b = tmp_path / "project-b" / "demo.xpr"
    project_a.parent.mkdir()
    project_b.parent.mkdir()
    project_a.write_text("# A\n", encoding="utf-8")
    project_b.write_text("# B\n", encoding="utf-8")
    try:
        interpreter = tkinter.Tcl()
    except tkinter.TclError as exc:
        pytest.skip(f"Python Tcl runtime is unavailable: {exc}")
    interpreter.eval("set ::vmcp_test_side_effect 0; set ::vmcp_test_project_name demo")
    interpreter.eval(
        "proc current_project {} {if {$::vmcp_test_project_dir eq {}} {return {}}; return project_object}"
    )
    interpreter.eval(
        "proc get_property {property object} {"
        "if {$object eq {}} {error {no current project}}; "
        "if {$property eq {DIRECTORY}} {return $::vmcp_test_project_dir}; "
        "if {$property eq {NAME}} {return $::vmcp_test_project_name}; "
        "error {unsupported property}}"
    )
    guarded_side_effect = (
        active_project_identity_guard_command(project_a)
        + "; set ::vmcp_test_side_effect 1"
    )

    interpreter.setvar("vmcp_test_project_dir", str(project_b.parent.resolve()).replace("\\", "/"))
    with pytest.raises(tkinter.TclError, match=PROJECT_ACTIVE_IDENTITY_MISMATCH_MARKER):
        interpreter.eval(guarded_side_effect)
    assert interpreter.getvar("vmcp_test_side_effect") == "0"

    interpreter.setvar("vmcp_test_project_dir", "")
    with pytest.raises(tkinter.TclError, match=PROJECT_ACTIVE_IDENTITY_MISMATCH_MARKER):
        interpreter.eval(guarded_side_effect)
    assert interpreter.getvar("vmcp_test_side_effect") == "0"

    interpreter.setvar("vmcp_test_project_dir", str(project_a.parent.resolve()).replace("\\", "/"))
    interpreter.eval(guarded_side_effect)
    assert interpreter.getvar("vmcp_test_side_effect") == "1"


def test_gui_tcp_session_does_not_consume_sequence_on_connection_failure() -> None:
    session = GuiTcpVivadoSession(host="127.0.0.1", port=1)

    with pytest.raises(OSError):
        session.run_tcl("version -short", timeout_s=1)

    assert session._sequence == 0


def test_gui_tcp_session_does_not_commit_rejected_authentication() -> None:
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = GuiTcpVivadoSession(host="127.0.0.1", port=server.server_address[1])
        _Handler.expected_secret = "f" * 64

        result = session.run_tcl("version -short", timeout_s=2)
        server.shutdown()

    assert result["ok"] is False
    assert result["error_code"] == "TCL_RESPONSE_AUTHENTICATION_FAILED"
    assert result["protocol_authenticated"] is False
    assert session._sequence == 0
    assert session.state == "TAINTED"


@pytest.mark.parametrize(
    ("mode", "error_code"),
    [
        ("unauthenticated", "TCL_RESPONSE_AUTHENTICATION_FAILED"),
        ("tampered_body", "TCL_RESPONSE_AUTHENTICATION_FAILED"),
        ("tampered_status", "TCL_RESPONSE_AUTHENTICATION_FAILED"),
        ("wrong_sequence", "TCL_RESPONSE_SEQUENCE_MISMATCH"),
        ("old_protocol", "TCL_PROTOCOL_VERSION_MISMATCH"),
        ("malformed_length", "TCL_RESPONSE_LENGTH_MISMATCH"),
        ("truncated", "TCL_RESPONSE_TRUNCATED"),
    ],
)
def test_invalid_or_unauthenticated_response_taints_and_terminates_generation(
    mode: str,
    error_code: str,
    monkeypatch,
) -> None:
    class FakeProcess:
        pid = 97531

        def poll(self) -> None:
            return None

    terminated: list[int] = []
    monkeypatch.setattr(
        session_module,
        "_terminate_process_tree",
        lambda process: terminated.append(process.pid) or {"attempted": True, "terminated": True},
    )
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _AdversarialResponseHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = GuiTcpVivadoSession(host="127.0.0.1", port=server.server_address[1])
        session.process = FakeProcess()  # type: ignore[assignment]
        _AdversarialResponseHandler.expected_secret = session._auth_secret
        _AdversarialResponseHandler.mode = mode

        result = session.run_tcl("version -short", timeout_s=2)
        server.shutdown()

    assert result["ok"] is False
    assert result["error_code"] == error_code
    assert result["protocol_authenticated"] is False
    assert result["request_accepted"] is False
    assert session._sequence == 0
    assert session.state == "TAINTED"
    assert terminated == [97531]
    with pytest.raises(SessionTaintedError):
        session.run_tcl("version -short", timeout_s=1)


def test_session_start_reports_runtime_and_bootstrap_paths(tmp_path: Path, monkeypatch) -> None:
    vivado = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    vivado.parent.mkdir(parents=True)
    vivado.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))
    runtime_dir = tmp_path / "runtime"
    popen_call = {}

    class FakeProcess:
        pid = 12345

        def poll(self) -> None:
            return None

    def fake_popen(command, **kwargs):
        popen_call["command"] = command
        popen_call["kwargs"] = kwargs
        return FakeProcess()

    tcl_commands = []

    def fake_run_tcl(self, command: str, timeout_s: int = 60):
        tcl_commands.append(command)
        return {"ok": True, "raw": "Vivado v2021.2 (64-bit)"}

    monkeypatch.setattr("vivado_agent_mcp.vivado.session._spawn_vivado_process", fake_popen)
    monkeypatch.setattr(GuiTcpVivadoSession, "run_tcl", fake_run_tcl)

    session = GuiTcpVivadoSession(port=34567, vivado_path=str(vivado), runtime_dir=str(runtime_dir))
    result = session.start(timeout_s=1)

    expected_bootstrap = runtime_dir.resolve() / "vivado_agent_mcp_34567.tcl"
    assert result["ok"] is True
    assert result["runtime_dir"] == str(runtime_dir.resolve())
    assert result["temp_dir"] == str(runtime_dir.resolve())
    assert result["bootstrap_path"] == str(expected_bootstrap)
    assert result["transport_auth"] == TRANSPORT_AUTH
    assert result["protocol_version"] == WIRE_PROTOCOL_VERSION
    assert result["bootstrap_present"] is False
    assert result["bootstrap_removed_after_handshake"] is True
    assert result["session_state"] == "READY"
    assert result["version"] == "2021.2"
    assert result["version_output"] == "Vivado v2021.2 (64-bit)"
    assert tcl_commands == ["version"]
    assert "auth_secret" not in result
    assert popen_call["kwargs"]["cwd"] == str(runtime_dir.resolve())
    assert popen_call["kwargs"]["env"]["TEMP"] == str(runtime_dir.resolve())
    assert popen_call["kwargs"]["env"]["TMP"] == str(runtime_dir.resolve())
    assert str(expected_bootstrap) in popen_call["command"]
    assert expected_bootstrap.exists() is False


def test_session_manager_rejects_unconfigured_executable_before_spawn(tmp_path: Path, monkeypatch) -> None:
    trusted = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("@echo off\necho trusted\n", encoding="utf-8")
    sentinel = tmp_path / "sentinel.txt"
    attacker = tmp_path / "attacker.cmd"
    attacker.write_text(f'@echo off\necho executed>"{sentinel}"\n', encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(trusted))
    manager = SessionManager()
    spawned = False

    def reject_spawn(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("untrusted executable must not reach process spawn")

    monkeypatch.setattr(session_module, "_spawn_vivado_process", reject_spawn)

    result = manager.start(vivado_path=str(attacker), runtime_dir=str(tmp_path / "runtime"))

    assert result["ok"] is False
    assert result["error_code"] == "VIVADO_PATH_MISMATCH"
    assert result["data"]["execution_attempted"] is False
    assert spawned is False
    assert sentinel.exists() is False


def test_session_manager_validates_path_before_touching_existing_session(tmp_path: Path, monkeypatch) -> None:
    trusted = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("@echo off\necho trusted\n", encoding="utf-8")
    attacker = tmp_path / "attacker.cmd"
    attacker.write_text("@echo off\necho attacker\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(trusted))

    class ExistingSession:
        def __init__(self) -> None:
            self.status_called = False
            self.stop_called = False

        def status(self, probe_connection: bool = False) -> dict:
            self.status_called = True
            return {"ok": True, "connected": True, "process_running": True}

        def stop(self) -> dict:
            self.stop_called = True
            return {"ok": True}

    existing = ExistingSession()
    manager = SessionManager(session=existing)  # type: ignore[arg-type]

    result = manager.start(vivado_path=str(attacker), runtime_dir=str(tmp_path / "runtime"))

    assert result["ok"] is False
    assert result["error_code"] == "VIVADO_PATH_MISMATCH"
    assert result["execution_attempted"] is False
    assert existing.status_called is False
    assert existing.stop_called is False


def test_spawn_revalidates_configured_executable_identity_immediately_before_popen(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vivado = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    vivado.parent.mkdir(parents=True)
    vivado.write_text("@echo off\necho trusted\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))
    trusted_identity = capture_server_vivado_identity()
    vivado.write_text("@echo off\necho replaced before spawn\n", encoding="utf-8")
    popen_called = False

    def reject_popen(*args, **kwargs):
        nonlocal popen_called
        popen_called = True
        raise AssertionError("changed executable must not reach subprocess.Popen")

    monkeypatch.setattr(session_module.subprocess, "Popen", reject_popen)

    with pytest.raises(TrustedVivadoPathError, match="identity changed") as exc_info:
        session_module._spawn_vivado_process(
            [str(vivado), "-mode", "gui"],
            trusted_identity=trusted_identity,
        )

    assert exc_info.value.error_code == "VIVADO_PATH_IDENTITY_CHANGED"
    assert popen_called is False


@pytest.mark.parametrize(
    ("version_output", "error_code", "expected_version", "taint_reason"),
    [
        ("Vivado v2021.2.1 (64-bit)", "UNSUPPORTED_VIVADO_VERSION", "2021.2.1", "unsupported_vivado_version"),
        ("Vivado version unavailable", "VIVADO_VERSION_UNATTESTED", None, "vivado_version_unattested"),
    ],
)
def test_session_start_fails_closed_on_unqualified_full_version(
    tmp_path: Path,
    monkeypatch,
    version_output: str,
    error_code: str,
    expected_version: str | None,
    taint_reason: str,
) -> None:
    vivado = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    vivado.parent.mkdir(parents=True)
    vivado.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))
    runtime_dir = tmp_path / "runtime"
    terminated = []

    class FakeProcess:
        pid = 12345

        def poll(self) -> None:
            return None

    monkeypatch.setattr("vivado_agent_mcp.vivado.session._spawn_vivado_process", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(
        "vivado_agent_mcp.vivado.session._terminate_process_tree",
        lambda process: terminated.append(process.pid) or {"attempted": True, "terminated": True, "pid": process.pid},
    )

    def fake_run_tcl(self, command: str, timeout_s: int = 60):
        assert command == "version"
        return {"ok": True, "raw": version_output}

    monkeypatch.setattr(GuiTcpVivadoSession, "run_tcl", fake_run_tcl)

    session = GuiTcpVivadoSession(port=34567, vivado_path=str(vivado), runtime_dir=str(runtime_dir))
    result = session.start(timeout_s=1)

    assert result["ok"] is False
    assert result["error_code"] == error_code
    assert result["version"] == expected_version
    assert result["version_output"] == version_output
    assert result["supported_version"] == "2021.2"
    assert result["bootstrap_present"] is False
    assert result["session_state"] == "TAINTED"
    assert result["taint_reason"] == taint_reason
    assert terminated == [12345]


def test_session_start_fails_closed_when_bootstrap_acl_cannot_be_restricted(tmp_path: Path, monkeypatch) -> None:
    vivado = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    vivado.parent.mkdir(parents=True)
    vivado.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))
    runtime_dir = tmp_path / "runtime"

    def reject_bootstrap(path: Path, host: str, port: int, auth_secret: str) -> None:
        raise BootstrapPermissionError("ACL failure")

    monkeypatch.setattr(session_module, "write_bootstrap", reject_bootstrap)

    session = GuiTcpVivadoSession(port=34567, vivado_path=str(vivado), runtime_dir=str(runtime_dir))
    result = session.start(timeout_s=1)

    assert result["ok"] is False
    assert result["error_code"] == "BOOTSTRAP_PERMISSION_FAILURE"
    assert result["bootstrap_present"] is False
    assert result["session_state"] == "START_FAILED"
    assert result["taint_reason"] == "bootstrap_permission_failure"


def test_session_start_timeout_reports_startup_logs_and_terminates(tmp_path: Path, monkeypatch) -> None:
    vivado = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    vivado.parent.mkdir(parents=True)
    vivado.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))
    runtime_dir = tmp_path / "runtime"
    terminated = {}

    class FakeProcess:
        pid = 12345

        def poll(self) -> None:
            return None

    def fake_popen(command, **kwargs):
        kwargs["stdout"].write(b"Vivado starting\n")
        kwargs["stderr"].write(b"startup warning\n")
        kwargs["stdout"].flush()
        kwargs["stderr"].flush()
        return FakeProcess()

    def fake_terminate(process) -> None:
        terminated["pid"] = process.pid

    monkeypatch.setattr("vivado_agent_mcp.vivado.session._spawn_vivado_process", fake_popen)
    monkeypatch.setattr("vivado_agent_mcp.vivado.session._terminate_process_tree", fake_terminate)

    session = GuiTcpVivadoSession(port=34567, vivado_path=str(vivado), runtime_dir=str(runtime_dir))
    result = session.start(timeout_s=0)

    assert result["ok"] is False
    assert result["error_code"] == "VIVADO_START_TIMEOUT"
    assert result["startup"]["vivado_path"] == str(vivado.resolve())
    assert result["startup"]["phase"] == "tcl_server_timeout"
    assert result["startup"]["vivado_process_started"] is True
    assert result["startup"]["tcl_server_connected"] is False
    assert result["startup"]["recommended_retry_timeout_s"] == 240
    assert result["startup"]["process_running"] is True
    assert result["startup"]["terminated_after_timeout"] is True
    assert result["startup"]["stdout_tail"] == "Vivado starting"
    assert result["startup"]["stderr_tail"] == "startup warning"
    assert Path(result["startup"]["stdout_path"]).exists()
    assert Path(result["startup"]["stderr_path"]).exists()
    assert terminated["pid"] == 12345


def test_session_start_reports_early_process_exit(tmp_path: Path, monkeypatch) -> None:
    vivado = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    vivado.parent.mkdir(parents=True)
    vivado.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))
    runtime_dir = tmp_path / "runtime"

    class FakeProcess:
        pid = 12345
        returncode = 42

        def poll(self) -> int:
            return 42

    def fake_popen(command, **kwargs):
        kwargs["stderr"].write(b"license checkout failed\n")
        kwargs["stderr"].flush()
        return FakeProcess()

    monkeypatch.setattr("vivado_agent_mcp.vivado.session._spawn_vivado_process", fake_popen)

    session = GuiTcpVivadoSession(port=34567, vivado_path=str(vivado), runtime_dir=str(runtime_dir))
    result = session.start(timeout_s=1)

    assert result["ok"] is False
    assert result["error_code"] == "VIVADO_PROCESS_EXITED"
    assert result["startup"]["returncode"] == 42
    assert result["startup"]["phase"] == "process_exited"
    assert result["startup"]["vivado_process_started"] is True
    assert result["startup"]["tcl_server_connected"] is False
    assert result["startup"]["process_running"] is False
    assert result["startup"]["stderr_tail"] == "license checkout failed"


def test_session_status_reports_runtime_and_bootstrap_paths(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bootstrap = runtime_dir / "vivado_agent_mcp_34567.tcl"
    session = GuiTcpVivadoSession(port=34567, runtime_dir=str(runtime_dir))
    session._runtime_path = runtime_dir
    session._bootstrap_path = bootstrap

    monkeypatch.setattr(session, "run_tcl", lambda command, timeout_s=2: {"ok": True, "raw": "2021.2"})

    result = session.status()

    assert result["ok"] is True
    assert result["connected"] is True
    assert result["runtime_dir"] == str(runtime_dir)
    assert result["temp_dir"] == str(runtime_dir)
    assert result["bootstrap_path"] == str(bootstrap)


def test_session_manager_restarts_stale_disconnected_session(tmp_path: Path, monkeypatch) -> None:
    class StaleSession:
        stopped = False

        def status(self) -> dict:
            return {"ok": True, "connected": False, "process_running": True}

        def stop(self) -> dict:
            self.stopped = True
            return {"ok": True}

    class FreshSession:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def start(self, timeout_s: int) -> dict:
            return {"ok": True, "connected": True, "process_running": True, "timeout_s": timeout_s}

    stale = StaleSession()
    vivado = tmp_path / "Vivado" / "2021.2" / "bin" / "vivado.bat"
    vivado.parent.mkdir(parents=True)
    vivado.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("VIVADO_PATH", str(vivado))
    monkeypatch.setattr(session_module, "GuiTcpVivadoSession", FreshSession)
    manager = SessionManager(session=stale)  # type: ignore[arg-type]

    result = manager.start(vivado_path=str(vivado), timeout_s=240, runtime_dir="D:/runtime")

    assert stale.stopped is True
    assert isinstance(manager.session, FreshSession)
    assert result["connected"] is True
    assert result["timeout_s"] == 240


def test_stop_failure_keeps_session_handle_and_bootstrap_for_recovery(tmp_path: Path, monkeypatch) -> None:
    class StubbornProcess:
        pid = 24680

        def poll(self) -> None:
            return None

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    bootstrap = runtime_dir / "vivado_agent_mcp_1234.tcl"
    bootstrap.write_text("server bootstrap", encoding="utf-8")
    session = GuiTcpVivadoSession(runtime_dir=str(runtime_dir))
    session.process = StubbornProcess()  # type: ignore[assignment]
    session._runtime_path = runtime_dir
    session._bootstrap_path = bootstrap
    monkeypatch.setattr(
        session_module,
        "_terminate_process_tree",
        lambda process: {"attempted": True, "terminated": False, "pid": process.pid, "error": "access denied"},
    )
    manager = SessionManager(session=session)

    result = manager.stop()

    assert result["ok"] is False
    assert result["error_code"] == "SESSION_STOP_FAILED"
    assert result["stopped"] is False
    assert result["process_running"] is True
    assert manager.session is session
    assert bootstrap.exists()
    assert session.state == "STOP_FAILED"


def test_manager_refuses_new_session_when_previous_process_cannot_stop(monkeypatch) -> None:
    class StaleSession:
        def status(self) -> dict:
            return {"ok": True, "connected": False, "process_running": True}

        def stop(self) -> dict:
            return {"ok": False, "error_code": "SESSION_STOP_FAILED", "process_running": True}

    stale = StaleSession()
    manager = SessionManager(session=stale)  # type: ignore[arg-type]

    result = manager.start(timeout_s=240)

    assert result["ok"] is False
    assert result["error_code"] == "PREVIOUS_SESSION_STOP_FAILED"
    assert manager.session is stale


def test_tcl_timeout_taints_session_terminates_process_and_blocks_reuse(monkeypatch) -> None:
    class SlowHandler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.rfile.readline()
            time.sleep(0.2)

    class FakeProcess:
        pid = 9876

        def poll(self) -> None:
            return None

    terminated: list[int] = []
    monkeypatch.setattr(
        session_module,
        "_terminate_process_tree",
        lambda process: terminated.append(process.pid),
    )

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), SlowHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = GuiTcpVivadoSession(host="127.0.0.1", port=server.server_address[1])
        session.process = FakeProcess()  # type: ignore[assignment]

        with pytest.raises(TimeoutError):
            session.run_tcl("after 1000 {set ::late_write 1}", timeout_s=0.05)
        with pytest.raises(SessionTaintedError):
            session.run_tcl("version -short", timeout_s=1)

        server.shutdown()

    assert session.state == "TAINTED"
    assert session.taint_reason == "tcl_request_timeout"
    assert terminated == [9876]


@pytest.mark.parametrize(
    ("xdc_content", "expected_ok", "expected_call_count"),
    [
        ("set_property PACKAGE_PIN U16 [get_ports led]\n", True, 2),
        ("set_property PACKAGE_PIN U16 [exec cmd.exe]\n", False, 1),
    ],
)
def test_guarded_tcl_execution_revalidates_and_locks_xdc_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    xdc_content: str,
    expected_ok: bool,
    expected_call_count: int,
) -> None:
    xdc = tmp_path / "top.xdc"
    xdc.write_text(xdc_content, encoding="utf-8")
    rtl = tmp_path / "top.sv"
    rtl.write_text("module top; endmodule\n", encoding="utf-8")
    project = tmp_path / "demo.xpr"
    project.write_text("# demo\n", encoding="utf-8")
    preflight_raw = (
        "vivado_version_short=2021.2\n"
        "vivado_version_full=Vivado v2021.2\n"
        f"project_dir={tmp_path}\n"
        "project_name=demo\n"
        f"project_path={project}\n"
        "part=xc7a35tcpg236-1\n"
        "top=top\n"
        "include_dirs=\n"
        "verilog_defines=\n"
        "sources_begin=__VMCP_SOURCE_INPUTS_BEGIN__\n"
        + encode_wire_row({"used_in": "synthesis", "order": "0", "path": str(rtl), "type": "SystemVerilog", "library": "xil_defaultlib"})
        + "\n"
        "constraints_begin=__VMCP_CONSTRAINT_INPUTS_BEGIN__\n"
        + encode_wire_row({"fileset": "constrs_1", "path": str(xdc), "type": "XDC"})
        + "\nrun_configurations_begin=__VMCP_RUN_CONFIGURATIONS_BEGIN__\n"
        "composite_inputs_begin=__VMCP_COMPOSITE_INPUTS_BEGIN__\n"
        "discovery_errors_begin=__VMCP_EXECUTION_INPUT_ERRORS_BEGIN__"
    )
    calls: list[str] = []
    session = GuiTcpVivadoSession(generation_id="guard-test")
    session.state = "READY"

    def fake_run_tcl_locked(command: str, *, timeout_s: int) -> dict:
        calls.append(command)
        if len(calls) == 1:
            return {"ok": True, "raw": preflight_raw, "generation_id": session.generation_id}
        return {"ok": True, "raw": "launched", "generation_id": session.generation_id}

    monkeypatch.setattr(session, "_run_tcl_locked", fake_run_tcl_locked)

    result = session.run_tcl(
        f"set guard_marker {{{EXECUTABLE_CONSTRAINT_BLOCK_MARKER}}}; launch_runs {{synth_1}}",
        timeout_s=60,
    )

    assert result["ok"] is expected_ok
    assert len(calls) == expected_call_count
    if not expected_ok:
        assert result["error_code"] == "EXECUTABLE_CONSTRAINT_INPUT_BLOCKED"
        assert EXECUTABLE_CONSTRAINT_BLOCK_MARKER in result["raw"]
        assert not any("launch_runs" in command for command in calls)
