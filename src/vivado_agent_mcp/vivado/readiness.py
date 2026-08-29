from __future__ import annotations

from typing import Any


def evaluate_bitstream_readiness(
    timing: dict[str, Any],
    drc: dict[str, Any],
    critical_warnings: dict[str, Any],
    check_timing: dict[str, Any] | None = None,
    methodology: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []

    if timing.get("parsed") is not True or timing.get("timing_met") is None:
        reasons.append("Timing summary is unavailable or could not be parsed")
    elif timing.get("timing_met") is False:
        reasons.append("Timing is not met")
    if drc.get("parsed") is not True:
        reasons.append("DRC report is unavailable or could not be parsed")
    if drc.get("error_count", 0):
        reasons.append(f"DRC contains {drc.get('error_count')} error(s)")
    if drc.get("critical_warning_count", 0):
        reasons.append(f"DRC contains {drc.get('critical_warning_count')} critical warning(s)")

    counts = critical_warnings.get("counts", {})
    if counts.get("ERROR", 0):
        reasons.append(f"Run messages contain {counts.get('ERROR')} error(s)")
    if counts.get("CRITICAL WARNING", 0):
        reasons.append(
            f"Run messages contain {counts.get('CRITICAL WARNING')} critical warning(s)"
        )
    check_counts = (check_timing or {}).get("counts", {})
    if check_timing is not None and check_timing.get("parsed") is not True:
        reasons.append("check_timing report is unavailable or could not be parsed")
    for key in (
        "no_clock",
        "unconstrained_internal_endpoints",
        "multiple_clock",
        "loops",
        "latch_loops",
        "pulse_width_clock",
    ):
        if check_counts.get(key, 0):
            reasons.append(f"check_timing reports {key}={check_counts.get(key)}")
    for key in (
        "no_input_delay",
        "no_output_delay",
        "partial_input_delay",
        "partial_output_delay",
        "generated_clocks",
        "constant_clock",
    ):
        if check_counts.get(key, 0):
            warnings.append(f"check_timing reports {key}={check_counts.get(key)}")

    methodology_counts = (methodology or {}).get("counts", {})
    if methodology is not None and methodology.get("parsed") is not True:
        reasons.append("Methodology report is unavailable or could not be parsed")
    if methodology_counts.get("ERROR", 0):
        reasons.append(f"Methodology contains {methodology_counts.get('ERROR')} error(s)")
    if methodology_counts.get("CRITICAL WARNING", 0):
        reasons.append(
            f"Methodology contains {methodology_counts.get('CRITICAL WARNING')} critical warning(s)"
        )
    if methodology_counts.get("WARNING", 0):
        warnings.append(f"Methodology contains {methodology_counts.get('WARNING')} warning(s)")

    if drc.get("warning_count", 0):
        warnings.append(f"DRC contains {drc.get('warning_count')} warning(s)")
    for violation in drc.get("violations", []):
        if not isinstance(violation, dict):
            continue
        text = " ".join(str(violation.get(key, "")) for key in ("id", "message"))
        if "CFGBVS" in text or "CONFIG_VOLTAGE" in text:
            warnings.append(
                "DRC configuration voltage warning requires review before board handoff: "
                f"{text.strip()}"
            )

    status = "BLOCK" if reasons else "WARN" if warnings else "READY"
    return {
        "ok": True,
        "status": status,
        "reasons": reasons,
        "warnings": warnings,
    }
