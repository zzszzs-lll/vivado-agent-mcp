from __future__ import annotations

import re
from typing import Any


DRC_REPORT_BEGIN_MARKER = "__VMCP_DRC_REPORT_BEGIN__"
DRC_REPORT_END_MARKER = "__VMCP_DRC_REPORT_END__"
TIMING_SUMMARY_REPORT_BEGIN_MARKER = "__VMCP_TIMING_SUMMARY_REPORT_BEGIN__"
_REPORT_COMMANDS = {
    "check_timing": "check_timing",
    "drc": "report_drc",
    "methodology": "report_methodology",
    "timing_summary": "report_timing_summary",
}


_FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"


def _first_float_after(label: str, text: str) -> float | None:
    pattern = rf"{re.escape(label)}(?:\s*\([^)]*\))?\s*:?\s*({_FLOAT})"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


def _timing_header_columns(line: str) -> list[str]:
    patterns = (
        (r"\bTNS\s+Failing\s+Endpoints\b", "tns_failing_endpoints"),
        (r"\bTNS\s+Total\s+Endpoints\b", "tns_total_endpoints"),
        (r"\bTHS\s+Failing\s+Endpoints\b", "ths_failing_endpoints"),
        (r"\bTHS\s+Total\s+Endpoints\b", "ths_total_endpoints"),
        (r"\bTPWS\s+Failing\s+Endpoints\b", "tpws_failing_endpoints"),
        (r"\bTPWS\s+Total\s+Endpoints\b", "tpws_total_endpoints"),
        (r"\bWNS(?:\s*\(ns\))?\b", "wns"),
        (r"\bTNS(?:\s*\(ns\))?\b", "tns"),
        (r"\bWHS(?:\s*\(ns\))?\b", "whs"),
        (r"\bTHS(?:\s*\(ns\))?\b", "ths"),
        (r"\bWPWS(?:\s*\(ns\))?\b", "wpws"),
        (r"\bTPWS(?:\s*\(ns\))?\b", "tpws"),
    )
    matches: list[tuple[int, str]] = []
    for pattern, label in patterns:
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if match:
            matches.append((match.start(), label))
    return [label for _, label in sorted(matches)]


def _design_timing_summary_scope(text: str) -> tuple[str, int]:
    lines = text.splitlines()
    heading_indexes = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"\s*\|?\s*Design Timing Summary\s*", line, flags=re.IGNORECASE)
    ]
    if not heading_indexes:
        return text, 0
    if len(heading_indexes) != 1:
        return "", len(heading_indexes)

    start = heading_indexes[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        if re.fullmatch(r"\s*\|\s*[A-Za-z][A-Za-z0-9 /_-]*\s*", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end]), 1


def _timing_row_values(text: str) -> tuple[dict[str, float], int, list[str], int]:
    scoped_text, summary_section_count = _design_timing_summary_scope(text)
    if summary_section_count > 1:
        return {}, 0, [], summary_section_count
    lines = [line.strip() for line in scoped_text.splitlines() if line.strip()]
    header_indexes = [
        index
        for index, line in enumerate(lines)
        if all(token in line.upper() for token in ("WNS", "TNS", "WHS", "THS"))
    ]
    if len(header_indexes) != 1:
        return {}, len(header_indexes), [], summary_section_count
    columns = _timing_header_columns(lines[header_indexes[0]])
    if any(columns.count(name) != 1 for name in ("wns", "tns", "whs", "ths")):
        return {}, 1, columns, summary_section_count
    for candidate in lines[header_indexes[0] + 1 : header_indexes[0] + 4]:
        values = [float(value) for value in re.findall(_FLOAT, candidate)]
        if len(values) >= len(columns):
            return dict(zip(columns, values[: len(columns)], strict=True)), 1, columns, summary_section_count
    return {}, 1, columns, summary_section_count


def parse_timing_summary(text: str) -> dict[str, Any]:
    report, attestation_complete, attestation = _extract_report_attestation(
        text,
        report_type="timing_summary",
        begin_marker=TIMING_SUMMARY_REPORT_BEGIN_MARKER,
    )
    row_values, header_count, columns, summary_section_count = _timing_row_values(report)
    wns = _first_float_after("WNS", report)
    tns = _first_float_after("TNS", report)
    whs = _first_float_after("WHS", report)
    ths = _first_float_after("THS", report)

    if row_values:
        wns = row_values["wns"]
        tns = row_values["tns"]
        whs = row_values["whs"]
        ths = row_values["ths"]

    complete_metrics = all(value is not None for value in (wns, tns, whs, ths))
    complete_structure = bool(row_values) and header_count == 1 and summary_section_count <= 1
    parsed = complete_metrics and complete_structure and attestation_complete
    timing_met = None
    if parsed:
        timing_met = wns >= 0 and whs >= 0

    return {
        "ok": True,
        "status": "READY" if parsed and timing_met else "BLOCK",
        "wns_ns": wns,
        "tns_ns": tns,
        "whs_ns": whs,
        "ths_ns": ths,
        "timing_met": timing_met,
        "parsed": parsed,
        "parse_status": "PARSED" if parsed else "UNKNOWN",
        "complete_metrics": complete_metrics,
        "complete_structure": complete_structure,
        "summary_header_count": header_count,
        "summary_section_count": summary_section_count,
        "summary_columns": columns,
        "attestation_required": True,
        "attestation_complete": attestation_complete,
        "attestation": attestation,
    }


def _parse_resource_line(line: str) -> tuple[str, int, int, float] | None:
    parts = [part.strip() for part in line.strip().strip("|").split("|")]
    if len(parts) < 4:
        return None
    name = parts[0].lower()
    nums = re.findall(_FLOAT, " | ".join(parts[1:]))
    if len(nums) < 3:
        return None
    return name, int(float(nums[0])), int(float(nums[-2])), float(nums[-1])


def parse_utilization_report(text: str) -> dict[str, Any]:
    aliases = {
        "lut": ("slice luts", "clb luts", "lut as logic"),
        "ff": ("slice registers", "register as flip flop", "clb registers"),
        "bram": ("block ram tile", "ramb36", "ramb18"),
        "dsp": ("dsps", "dsp48"),
        "iob": ("bonded iob", "iob"),
    }
    resources: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        parsed = _parse_resource_line(line)
        if not parsed:
            continue
        name, used, available, percent = parsed
        for key, needles in aliases.items():
            if key in resources:
                continue
            if any(needle in name for needle in needles):
                resources[key] = {
                    "used": used,
                    "available": available,
                    "utilization_percent": percent,
                }
    return {"ok": True, "resources": resources}


_MESSAGE_RE = re.compile(
    r"(?P<severity>CRITICAL WARNING|FATAL|ERROR|WARNING|INFO):\s*"
    r"(?:\[(?P<id>[^\]]+)\])?\s*(?P<message>.*)",
    flags=re.IGNORECASE,
)


def parse_messages(text: str) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    counts = {"ERROR": 0, "CRITICAL WARNING": 0, "WARNING": 0, "INFO": 0}
    for line_no, line in enumerate(text.splitlines(), start=1):
        match = _MESSAGE_RE.search(line.strip())
        if not match:
            continue
        original_severity = match.group("severity").upper()
        severity = "ERROR" if original_severity == "FATAL" else original_severity
        msg = {
            "severity": severity,
            "id": (match.group("id") or "").strip(),
            "message": ("FATAL: " if original_severity == "FATAL" else "") + match.group("message").strip(),
            "line": line_no,
        }
        counts[severity] += 1
        messages.append(msg)
    return {"ok": True, "counts": counts, "messages": messages}


def parse_drc_report(text: str) -> dict[str, Any]:
    report_text, attested, attestation = _extract_report_attestation(
        text,
        report_type="drc",
        begin_marker=DRC_REPORT_BEGIN_MARKER,
    )
    parsed = parse_messages(report_text)
    message_violations = [
        msg
        for msg in parsed["messages"]
        if msg["severity"] in {"ERROR", "CRITICAL WARNING", "WARNING"}
    ]
    rule_summary = parse_vivado_rule_violations(report_text)
    counts = {
        severity: max(parsed["counts"][severity], rule_summary["counts"][severity])
        for severity in ("ERROR", "CRITICAL WARNING", "WARNING", "INFO")
    }
    violations = message_violations or rule_summary["violations"]
    header_recognized = bool(
        re.search(
            r"(?im)^\s*(?:report\s+drc|drc\s+(?:report|summary)(?::\s*no\s+violations)?|no\s+drc\s+violations)\s*$",
            report_text,
        )
    )
    explicit_empty = bool(re.search(r"(?im)(?:drc\s+(?:report|summary)\s*:\s*no\s+violations|no\s+drc\s+violations)", report_text))
    violation_summary = rule_summary["summary_count"] is not None
    structure_recognized = header_recognized or violation_summary
    observed_count = rule_summary["parsed_count"]
    if not rule_summary["violations"]:
        observed_count = len(
            [
                message
                for message in parsed["messages"]
                if str(message.get("id", "")).upper().startswith("DRC ")
            ]
        )
    summary_consistent = rule_summary["summary_count"] is None or rule_summary["summary_count"] == observed_count
    complete = bool(
        attested
        and not rule_summary["duplicate_summary"]
        and not rule_summary["malformed_rows"]
        and summary_consistent
        and (explicit_empty or (header_recognized and violation_summary and rule_summary["table_recognized"]))
    )
    unclassified_violations = bool(
        rule_summary["summary_count"]
        and not rule_summary["violations"]
        and not observed_count
    )
    status = (
        "BLOCK"
        if not complete or unclassified_violations or counts["ERROR"] or counts["CRITICAL WARNING"]
        else "WARN"
        if counts["WARNING"] or counts["INFO"]
        else "READY"
    )
    return {
        "ok": True,
        "parsed": complete,
        "report_attested": attested,
        "transport_attested": attested,
        "report_attestation": attestation,
        "structure_recognized": structure_recognized,
        "complete": complete,
        "status": status,
        "error_count": counts["ERROR"],
        "critical_warning_count": counts["CRITICAL WARNING"],
        "warning_count": counts["WARNING"],
        "violation_summary_count": rule_summary["summary_count"],
        "parsed_violation_count": observed_count,
        "summary_consistent": summary_consistent,
        "duplicate_summary": rule_summary["duplicate_summary"],
        "table_recognized": rule_summary["table_recognized"],
        "malformed_rows": rule_summary["malformed_rows"],
        "unclassified_violations": unclassified_violations,
        "violations": violations,
    }


def parse_vivado_rule_violations(text: str) -> dict[str, Any]:
    counts = {"ERROR": 0, "CRITICAL WARNING": 0, "WARNING": 0, "INFO": 0}
    violations: list[dict[str, Any]] = []
    summary_matches = re.findall(
        r"(?im)(?:violations\s+found|(?:total|number\s+of)\s+(?:drc\s+)?violations?)\s*:\s*(\d+)",
        text,
    )
    malformed_rows: list[dict[str, Any]] = []
    table_recognized = False
    in_rule_table = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_rule_table and stripped and not stripped.startswith("+"):
                in_rule_table = False
            continue
        if not stripped.endswith("|"):
            if in_rule_table:
                malformed_rows.append({"line": line_no, "reason": "unterminated table row", "raw": stripped[:500]})
            # Vivado's report metadata header uses leading pipes without a
            # trailing pipe. It is outside the rule table and is not a row.
            continue
        fields = [field.strip() for field in stripped.strip("|").split("|")]
        if len(fields) >= 2 and fields[0].lower() in {"rule", "rule id", "id", "check"} and fields[1].lower() == "severity":
            table_recognized = True
            in_rule_table = True
            continue
        if not in_rule_table:
            continue
        if all(not field.strip(" -:=") for field in fields):
            continue
        if len(fields) < 4:
            malformed_rows.append({"line": line_no, "reason": "table row has fewer than four fields", "raw": stripped[:500]})
            continue
        severity = _normalize_rule_severity(fields[1])
        count_text = fields[-1]
        if severity is None:
            malformed_rows.append({"line": line_no, "reason": "unknown severity", "raw": stripped[:500]})
            continue
        if not count_text.isdigit():
            malformed_rows.append({"line": line_no, "reason": "invalid violation count", "raw": stripped[:500]})
            continue
        count = int(count_text)
        counts[severity] += count
        violations.append(
            {
                "severity": severity,
                "id": fields[0],
                "message": fields[2],
                "count": count,
            }
        )
    return {
        "summary_count": int(summary_matches[0]) if len(summary_matches) == 1 else None,
        "duplicate_summary": len(summary_matches) > 1,
        "table_recognized": table_recognized,
        "parsed_count": sum(item["count"] for item in violations),
        "malformed_rows": malformed_rows,
        "counts": counts,
        "violations": violations,
    }


def _normalize_rule_severity(value: str) -> str | None:
    normalized = re.sub(r"\s+", " ", value.strip()).upper()
    aliases = {
        "ERROR": "ERROR",
        "CRITICAL WARNING": "CRITICAL WARNING",
        "WARNING": "WARNING",
        "INFO": "INFO",
        "INFORMATION": "INFO",
        "ADVISORY": "INFO",
    }
    return aliases.get(normalized)


def report_end_marker(begin_marker: str) -> str:
    return begin_marker.replace("_BEGIN__", "_END__")


def attest_report_text(
    report_type: str,
    begin_marker: str,
    body: str,
    *,
    vivado_version_short: str = "2021.2",
    vivado_build: str = "Vivado v2021.2",
    report_command: str | None = None,
) -> str:
    """Wrap already verified report bytes in the same strict parser envelope as Tcl transport."""
    return (
        "vmcp_report_ok=1\n"
        f"vmcp_report_type={report_type}\n"
        f"vmcp_vivado_version_short={vivado_version_short}\n"
        f"vmcp_vivado_build={vivado_build}\n"
        f"vmcp_report_command={report_command or _REPORT_COMMANDS.get(report_type, report_type)}\n"
        "vmcp_parser_schema_version=vivado_2021_2_v1\n"
        f"vmcp_report_bytes={len(body.encode('utf-8'))}\n"
        f"vmcp_report_begin={begin_marker}\n"
        f"{body}\n"
        f"vmcp_report_end={report_end_marker(begin_marker)}"
    )


def _extract_report_attestation(
    text: str,
    *,
    report_type: str,
    begin_marker: str,
) -> tuple[str, bool, dict[str, str]]:
    parts = text.split("\n", 8)
    metadata = {
        "vivado_version_short": parts[2].partition("=")[2] if len(parts) > 2 else "",
        "vivado_build": parts[3].partition("=")[2] if len(parts) > 3 else "",
        "report_command": parts[4].partition("=")[2] if len(parts) > 4 else "",
        "parser_schema_version": parts[5].partition("=")[2] if len(parts) > 5 else "",
    }
    expected_command = _REPORT_COMMANDS.get(report_type, report_type)
    observed_command = metadata["report_command"].split(maxsplit=1)[0] if metadata["report_command"] else ""
    if len(parts) != 9:
        return text, False, metadata
    expected_headers = (
        parts[0] == "vmcp_report_ok=1",
        parts[1] == f"vmcp_report_type={report_type}",
        parts[2] == "vmcp_vivado_version_short=2021.2",
        parts[3].startswith("vmcp_vivado_build=Vivado v2021.2"),
        parts[4].startswith("vmcp_report_command=") and observed_command == expected_command,
        parts[5] == "vmcp_parser_schema_version=vivado_2021_2_v1",
        parts[6].startswith("vmcp_report_bytes="),
        parts[7] == f"vmcp_report_begin={begin_marker}",
    )
    if not all(expected_headers):
        return text, False, metadata
    body_and_end = parts[8]
    if "\n" not in body_and_end:
        return text, False, metadata
    body, end_line = body_and_end.rsplit("\n", 1)
    if end_line != f"vmcp_report_end={report_end_marker(begin_marker)}":
        return body, False, metadata
    try:
        expected_bytes = int(parts[6].split("=", 1)[1])
    except ValueError:
        return body, False, metadata
    return body, expected_bytes == len(body.encode("utf-8")), metadata
