from __future__ import annotations

from collections.abc import Mapping, Sequence
import re

HEX_LIST_PREFIX = "vmcp_hex_list_v1:"
HEX_ROW_PREFIX = "vmcp_hex_row_v1:"
WIRE_TRUST_VERSIONED = "VERSIONED"
WIRE_TRUST_LEGACY_UNATTESTED = "LEGACY_UNATTESTED"
_HEX_RE = re.compile(r"^[0-9a-fA-F]*$")


def tcl_wire_prelude() -> str:
    """Return Tcl helpers for delimiter-safe UTF-8 list and row transport."""
    return (
        "proc ::vivado_agent_mcp_wire_hex {value} {"
        "binary scan [encoding convertto utf-8 $value] H* encoded; return $encoded"
        "}; "
        "proc ::vivado_agent_mcp_wire_list {values} {"
        "set encoded_values [list]; "
        "foreach value $values {lappend encoded_values [::vivado_agent_mcp_wire_hex $value]}; "
        f'return "{HEX_LIST_PREFIX}[join $encoded_values ,]"'
        "}; "
        "proc ::vivado_agent_mcp_wire_row {pairs} {"
        "if {[llength $pairs] % 2 != 0} {error {wire row requires key/value pairs}}; "
        "set encoded_fields [list]; "
        "foreach {key value} $pairs {"
        "if {![regexp {^[A-Za-z_][A-Za-z0-9_]*$} $key]} {error {invalid wire row key}}; "
        "lappend encoded_fields \"$key=[::vivado_agent_mcp_wire_hex $value]\""
        "}; "
        f'return "{HEX_ROW_PREFIX}[join $encoded_fields |]"'
        "}"
    )


def encode_wire_list(values: Sequence[str]) -> str:
    return HEX_LIST_PREFIX + ",".join(_encode_text(value) for value in values)


def decode_wire_list(
    value: str,
    *,
    legacy_separator: str = ";",
    allow_legacy: bool = False,
) -> list[str]:
    if value == "":
        return []
    if not value.startswith(HEX_LIST_PREFIX):
        if not allow_legacy:
            raise ValueError("Unversioned wire list is not accepted on this path")
        return [item for item in value.split(legacy_separator) if item]
    payload = value[len(HEX_LIST_PREFIX) :]
    if not payload:
        return []
    return [_decode_text(item) for item in payload.split(",")]


def encode_wire_row(values: Mapping[str, str]) -> str:
    fields: list[str] = []
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid wire row key: {key!r}")
        fields.append(f"{key}={_encode_text(value)}")
    return HEX_ROW_PREFIX + "|".join(fields)


def decode_wire_row(
    line: str,
    *,
    legacy_separator: str = "|",
    allow_legacy: bool = False,
) -> dict[str, str]:
    if not line.startswith(HEX_ROW_PREFIX):
        if not allow_legacy:
            raise ValueError("Unversioned wire row is not accepted on this path")
        result: dict[str, str] = {}
        for chunk in line.split(legacy_separator):
            if "=" in chunk:
                key, value = chunk.split("=", 1)
                normalized_key = key.strip()
                if normalized_key in result:
                    raise ValueError(f"Duplicate legacy wire row key: {normalized_key!r}")
                result[normalized_key] = value.strip()
        return result
    payload = line[len(HEX_ROW_PREFIX) :]
    if not payload:
        return {}
    result = {}
    for chunk in payload.split("|"):
        if "=" not in chunk:
            raise ValueError("Malformed delimiter-safe wire row")
        key, encoded = chunk.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid wire row key: {key!r}")
        if key in result:
            raise ValueError(f"Duplicate wire row key: {key!r}")
        result[key] = _decode_text(encoded)
    return result


def wire_list_trust(value: str) -> str:
    return WIRE_TRUST_VERSIONED if value.startswith(HEX_LIST_PREFIX) else WIRE_TRUST_LEGACY_UNATTESTED


def wire_row_trust(value: str) -> str:
    return WIRE_TRUST_VERSIONED if value.startswith(HEX_ROW_PREFIX) else WIRE_TRUST_LEGACY_UNATTESTED


def _encode_text(value: str) -> str:
    return str(value).encode("utf-8").hex()


def _decode_text(value: str) -> str:
    if len(value) % 2 or not _HEX_RE.fullmatch(value):
        raise ValueError("Malformed delimiter-safe UTF-8 hex value")
    try:
        return bytes.fromhex(value).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Wire value is not valid UTF-8") from exc
