from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .runtime_identity import RuntimeIdentityError


ATTESTATION_SCHEMA_VERSION = 2
ATTESTATION_KEY_FILENAME = "attestation.key"
TRUST_ANCHOR_FILENAME = "trust_anchor.json"
ATTESTATION_LEDGER_DIRECTORY = "evidence_attestations"


def attest_diagnostic_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    trust_dir = resolve_attestation_trust_dir()
    anchor = _load_or_create_trust_anchor(trust_dir)
    key = _load_or_create_key(trust_dir)
    payload = _canonical_manifest_payload(manifest)
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
    key_id = hashlib.sha256(key).hexdigest()[:24]
    ledger_dir = trust_dir / ATTESTATION_LEDGER_DIRECTORY
    ledger_dir.mkdir(parents=True, exist_ok=True)
    _restrict_private_path(ledger_dir)
    ledger_path = ledger_dir / f"{payload_sha256}.json"
    ledger = {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "trust_anchor_id": anchor["trust_anchor_id"],
        "key_id": key_id,
        "manifest_payload_sha256": payload_sha256,
        "manifest_hmac_sha256": signature,
        "manifest_path": str(manifest.get("manifest_path", "")),
        "project_dir": str(manifest.get("project_dir", "")),
    }
    _write_private_json(ledger_path, ledger)
    return {
        "status": "ATTESTED_LOCAL_USER",
        "scheme": "HMAC-SHA256",
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "trust_anchor_id": anchor["trust_anchor_id"],
        "key_id": key_id,
        "manifest_payload_sha256": payload_sha256,
        "manifest_hmac_sha256": signature,
        "ledger_path": str(ledger_path),
        "trust_scope": "local_os_user",
        "portable": False,
        "external_signature": False,
        "note": (
            "This OS-user-local HMAC detects bundle replacement only while the private user trust anchor remains uncompromised. "
            "It does not defend against compromise of the same OS user and is not an Ed25519/Sigstore or portable supply-chain signature."
        ),
    }


def verify_diagnostic_manifest_attestation(manifest: dict[str, Any]) -> dict[str, Any]:
    attestation = manifest.get("authenticity")
    if not isinstance(attestation, dict):
        return _verification(
            "WARN",
            "NOT_ATTESTED",
            ["diagnostic manifest has no independent runtime attestation"],
        )
    if str(attestation.get("status", "")).upper() == "NOT_ATTESTED":
        return _verification(
            "WARN",
            "NOT_ATTESTED",
            [str(attestation.get("reason", "diagnostic manifest local runtime attestation was not created"))],
        )
    required = {
        "trust_anchor_id",
        "key_id",
        "manifest_payload_sha256",
        "manifest_hmac_sha256",
    }
    if (
        attestation.get("schema_version") != ATTESTATION_SCHEMA_VERSION
        or attestation.get("scheme") != "HMAC-SHA256"
        or not required.issubset(attestation)
    ):
        return _verification(
            "BLOCK",
            "ATTESTATION_ENVELOPE_INVALID",
            ["diagnostic manifest authenticity envelope is malformed or unsupported"],
        )

    trust_dir = resolve_attestation_trust_dir()
    try:
        anchor = _load_trust_anchor(trust_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        return _verification(
            "WARN",
            "ATTESTATION_TRUST_ANCHOR_UNAVAILABLE",
            ["current OS-user trust anchor is unavailable; local attestation cannot be verified"],
        )
    if str(attestation.get("trust_anchor_id", "")) != str(anchor.get("trust_anchor_id", "")):
        return _verification(
            "WARN",
            "ATTESTATION_TRUST_ANCHOR_MISMATCH",
            ["bundle was attested by a different OS-user trust anchor; treat it as review-only"],
        )
    key_path = trust_dir / ATTESTATION_KEY_FILENAME
    try:
        key = key_path.read_bytes()
    except OSError:
        return _verification(
            "WARN",
            "ATTESTATION_KEY_UNAVAILABLE",
            ["local runtime attestation key is unavailable"],
        )
    if len(key) != 32:
        return _verification("BLOCK", "ATTESTATION_KEY_INVALID", ["local runtime attestation key has an invalid length"])
    key_id = hashlib.sha256(key).hexdigest()[:24]
    if not hmac.compare_digest(str(attestation.get("key_id", "")), key_id):
        return _verification("BLOCK", "ATTESTATION_KEY_ID_MISMATCH", ["attestation key identity does not match"])

    payload = _canonical_manifest_payload(manifest)
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
    expected_payload_sha256 = str(attestation.get("manifest_payload_sha256", ""))
    expected_signature = str(attestation.get("manifest_hmac_sha256", ""))
    if not hmac.compare_digest(expected_payload_sha256, payload_sha256):
        return _verification("BLOCK", "ATTESTATION_PAYLOAD_MISMATCH", ["manifest payload digest does not match the attestation"])
    if not hmac.compare_digest(expected_signature, signature):
        return _verification("BLOCK", "ATTESTATION_HMAC_MISMATCH", ["manifest HMAC verification failed"])

    ledger_path = trust_dir / ATTESTATION_LEDGER_DIRECTORY / f"{payload_sha256}.json"
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _verification(
            "WARN",
            "ATTESTATION_LEDGER_UNAVAILABLE",
            ["independent runtime attestation ledger entry is unavailable"],
            ledger_path=ledger_path,
        )
    if not isinstance(ledger, dict):
        return _verification("BLOCK", "ATTESTATION_LEDGER_INVALID", ["runtime attestation ledger entry is not an object"])
    ledger_values = (
        str(ledger.get("trust_anchor_id", "")),
        str(ledger.get("key_id", "")),
        str(ledger.get("manifest_payload_sha256", "")),
        str(ledger.get("manifest_hmac_sha256", "")),
    )
    expected_values = (
        str(anchor.get("trust_anchor_id", "")),
        key_id,
        payload_sha256,
        signature,
    )
    if not all(hmac.compare_digest(actual, expected) for actual, expected in zip(ledger_values, expected_values, strict=True)):
        return _verification("BLOCK", "ATTESTATION_LEDGER_MISMATCH", ["runtime attestation ledger does not match the manifest"])
    return _verification(
        "READY",
        "ATTESTED_LOCAL_USER",
        [],
        ledger_path=ledger_path,
        payload_sha256=payload_sha256,
        key_id=key_id,
    )


def _canonical_manifest_payload(manifest: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in manifest.items() if key != "authenticity"}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def resolve_attestation_trust_dir() -> Path:
    configured = os.environ.get("VIVADO_AGENT_MCP_TRUST_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return (Path(local_app_data) / "vivado-agent-mcp" / "trust").resolve()
        return (Path.home() / "AppData" / "Local" / "vivado-agent-mcp" / "trust").resolve()
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return (base / "vivado-agent-mcp" / "trust").resolve()


def _ensure_trust_dir(trust_dir: Path) -> None:
    trust_dir.mkdir(parents=True, exist_ok=True)
    _restrict_private_path(trust_dir)


def _load_or_create_trust_anchor(trust_dir: Path) -> dict[str, Any]:
    _ensure_trust_dir(trust_dir)
    path = trust_dir / TRUST_ANCHOR_FILENAME
    try:
        return _load_trust_anchor(trust_dir)
    except FileNotFoundError:
        payload = {
            "schema_version": 1,
            "trust_anchor_id": f"trust_{uuid.uuid4().hex}",
            "created_at": datetime.now(UTC).isoformat(),
        }
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return _load_trust_anchor(trust_dir)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_private_path(path)
        return payload


def _load_trust_anchor(trust_dir: Path) -> dict[str, Any]:
    path = trust_dir / TRUST_ANCHOR_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Local attestation trust anchor is invalid")
    anchor_id = str(data.get("trust_anchor_id", ""))
    if not _valid_trust_anchor_id(anchor_id):
        raise ValueError("Local attestation trust anchor id is invalid")
    _restrict_private_path(path)
    return data


def _valid_trust_anchor_id(value: str) -> bool:
    return value.startswith("trust_") and len(value) == 38 and all(character in "0123456789abcdef" for character in value[6:])


def _load_or_create_key(trust_dir: Path) -> bytes:
    _ensure_trust_dir(trust_dir)
    key_path = trust_dir / ATTESTATION_KEY_FILENAME
    try:
        key = key_path.read_bytes()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        try:
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = key_path.read_bytes()
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
    if len(key) != 32:
        raise RuntimeIdentityError("attestation_key_invalid", f"Invalid attestation key: {key_path}")
    try:
        _restrict_private_path(key_path)
    except OSError as exc:
        raise RuntimeIdentityError("attestation_key_permissions", f"Could not protect attestation key: {key_path}") from exc
    return key


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _restrict_private_path(temporary)
    temporary.replace(path)
    _restrict_private_path(path)


def _restrict_private_path(path: Path) -> None:
    if os.name == "nt":
        from .bootstrap import _current_windows_sid, _run_icacls

        if _run_icacls(path, _current_windows_sid()) != 0:
            raise OSError(f"Failed to restrict Windows ACL: {path}")
        return
    expected_mode = 0o700 if path.is_dir() else 0o600
    os.chmod(path, expected_mode)
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    if actual_mode != expected_mode:
        raise OSError(f"Private path mode is {actual_mode:o}; expected {expected_mode:o}: {path}")


def _verification(
    status: str,
    code: str,
    issues: list[str],
    *,
    ledger_path: Path | None = None,
    payload_sha256: str = "",
    key_id: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "code": code,
        "issues": issues,
        "scheme": "HMAC-SHA256" if code != "NOT_ATTESTED" else "none",
        "trust_scope": "local_os_user",
        "portable": False,
        "external_signature": False,
        "ledger_path": str(ledger_path) if ledger_path is not None else "",
        "manifest_payload_sha256": payload_sha256,
        "key_id": key_id,
    }
