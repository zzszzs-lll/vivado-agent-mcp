from __future__ import annotations

from typing import Any


def hardware_validation_boundary() -> dict[str, Any]:
    return {
        "status": "NOT_VALIDATED",
        "validated": False,
        "real_board_required": True,
        "scope": "pre-hardware software flow only",
        "message": "Generated with no real FPGA board validation; real hardware bring-up is deferred.",
    }
