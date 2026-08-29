from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

_PLACEHOLDER_PATTERN = re.compile(r"\{([a-z_][A-Za-z0-9_]*)\}")


def tcl_list_quote(value: Any) -> str:
    """Quote a value as a single Tcl list element.

    Use the readable braced form only when a caller value cannot affect brace
    parsing. Otherwise use a double-quoted word with every Tcl substitution
    introducer and physical control character escaped.
    """

    text = str(value)
    if "\x00" in text:
        raise ValueError("Tcl values cannot contain NUL bytes")
    if "{" not in text and "}" not in text and "\\\n" not in text and "\\\r\n" not in text and not text.endswith("\\"):
        return "{" + text + "}"
    escaped = text.translate(
        str.maketrans(
            {
                "\\": "\\\\",
                '"': '\\"',
                "$": "\\$",
                "[": "\\[",
                "]": "\\]",
                "\n": "\\n",
                "\r": "\\r",
                "\t": "\\t",
                "\v": "\\v",
                "\f": "\\f",
            }
        )
    )
    return '"' + escaped + '"'


def safe_tcl(template: str, args: Mapping[str, Any]) -> str:
    """Render a Tcl template with lowercase placeholders quoted as Tcl list elements."""

    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in args:
            missing.append(name)
            return match.group(0)
        return tcl_list_quote(args[name])

    rendered: list[str] = []
    offset = 0
    for match in _PLACEHOLDER_PATTERN.finditer(template):
        rendered.append(template[offset : match.start()].replace("{{", "{").replace("}}", "}"))
        rendered.append(replace(match))
        offset = match.end()
    rendered.append(template[offset:].replace("{{", "{").replace("}}", "}"))
    if missing:
        raise KeyError(", ".join(missing))
    return "".join(rendered)
