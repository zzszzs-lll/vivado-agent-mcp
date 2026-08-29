from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path


def write_test_bitstream(
    path: Path,
    *,
    design: str = "top",
    part: str = "xc7a35tcpg236-1",
    payload: bytes = b"\xaa\x99\x55\x66",
) -> Path:
    header_part = _header_part(part)
    preamble = b"\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00"
    content = bytearray(struct.pack(">H", len(preamble)))
    content.extend(preamble)
    content.extend(struct.pack(">H", 1))
    for key, value in (
        (b"a", design),
        (b"b", header_part),
        (b"c", "2026/07/19"),
        (b"d", "12:00:00"),
    ):
        encoded = value.encode("ascii") + b"\x00"
        content.extend(key)
        content.extend(struct.pack(">H", len(encoded)))
        content.extend(encoded)
    content.extend(b"e")
    content.extend(struct.pack(">I", len(payload)))
    content.extend(payload)
    path.write_bytes(bytes(content))
    return path


def write_test_design_execution_identity(project_dir: Path) -> dict:
    source_dir = project_dir / "src"
    xdc_dir = project_dir / "xdc"
    source_dir.mkdir(parents=True, exist_ok=True)
    xdc_dir.mkdir(parents=True, exist_ok=True)
    source = source_dir / "fixture_top.sv"
    constraint = xdc_dir / "fixture_top.xdc"
    if not source.exists():
        source.write_text("module fixture_top; endmodule\n", encoding="utf-8")
    if not constraint.exists():
        constraint.write_text("create_clock -period 10 [get_ports clk]\n", encoding="utf-8")
    files = []
    for path, source_kind in ((source, "compile_source"), (constraint, "constraint")):
        stat_result = path.stat()
        raw = path.read_bytes()
        files.append(
            {
                "path": str(path.resolve()),
                "size": len(raw),
                "mtime_ns": stat_result.st_mtime_ns,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "object_identity": [stat_result.st_dev, stat_result.st_ino, stat_result.st_mode],
                "nlink": stat_result.st_nlink,
                "source_kind": source_kind,
            }
        )
    payload = {
        "schema_version": 1,
        "project_dir": str(project_dir.resolve()),
        "project_name": "fixture",
        "project_path": str((project_dir / "fixture.xpr").resolve()),
        "part": "xc7a35tcpg236-1",
        "top": "fixture_top",
        "vivado_version_short": "2021.2",
        "vivado_version_full": "Vivado v2021.2",
        "include_dirs": [],
        "verilog_defines": [],
        "source_compile_order": [{"used_in": "synthesis", "order": "0", "path": str(source.resolve())}],
        "constraint_compile_order": [{"used_in": "implementation", "order": "0", "path": str(constraint.resolve())}],
        "run_configurations": [{"run": "impl_1", "property": "STRATEGY", "value": "Vivado Implementation Defaults"}],
        "composite_inputs": [],
        "files": files,
        "include_files": [],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"status": "READY", "sha256": hashlib.sha256(canonical).hexdigest(), "identity": payload, "issues": []}


def _header_part(part: str) -> str:
    normalized = part.strip().lower()
    if normalized.startswith(("xc", "xa", "xq")):
        normalized = normalized[2:]
    return re.sub(r"-\d+[a-z]*$", "", normalized)
