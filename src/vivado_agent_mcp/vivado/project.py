from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from .simulation import validate_simulation_defines
from .tcl import tcl_list_quote
from .wire import decode_wire_list, decode_wire_row, tcl_wire_prelude


SUPPORTED_FILESETS = {"sources_1", "constrs_1", "sim_1"}
SUPPORTED_FILE_TYPES = {
    "Verilog",
    "SystemVerilog",
    "Verilog Header",
    "VHDL",
    "VHDL 2008",
    "XDC",
}
SUPPORTED_PROCESSING_ORDERS = {"", "EARLY", "NORMAL", "LATE"}
FILE_SPEC_BOOLEAN_FIELDS = (
    "is_global_include",
    "used_in_synthesis",
    "used_in_implementation",
    "used_in_simulation",
)
FILE_SPEC_FIELDS = (
    "path",
    "fileset",
    "file_type",
    "library",
    "compile_order",
    *FILE_SPEC_BOOLEAN_FIELDS,
    "processing_order",
    "scoped_to_ref",
    "scoped_to_cells",
)
_LIBRARY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def project_state_command() -> str:
    return (
        f"{tcl_wire_prelude()}; "
        "set p [current_project]; "
        "set project_name [get_property NAME $p]; "
        "set project_dir [get_property DIRECTORY $p]; "
        "set top \"\"; catch {set top [get_property TOP [get_filesets sources_1]]}; "
        "set sim_top \"\"; catch {set sim_top [get_property TOP [get_filesets sim_1]]}; "
        "set target_language \"\"; set target_simulator \"\"; set fileset_property_errors [list]; "
        "if {[catch {set target_language [get_property TARGET_LANGUAGE $p]} vmcp_error]} {lappend fileset_property_errors \"project TARGET_LANGUAGE discovery failed: $vmcp_error\"}; "
        "if {[catch {set target_simulator [get_property TARGET_SIMULATOR $p]} vmcp_error]} {lappend fileset_property_errors \"project TARGET_SIMULATOR discovery failed: $vmcp_error\"}; "
        "set source_include_dirs [list]; set source_verilog_defines [list]; "
        "set source_fs [get_filesets -quiet {sources_1}]; "
        "if {[llength $source_fs] != 1} {lappend fileset_property_errors {sources_1 fileset is missing or ambiguous}} else {"
        "if {[catch {set source_include_dirs [get_property INCLUDE_DIRS $source_fs]} vmcp_error]} {lappend fileset_property_errors \"sources_1 INCLUDE_DIRS discovery failed: $vmcp_error\"}; "
        "if {[catch {set source_verilog_defines [get_property VERILOG_DEFINE $source_fs]} vmcp_error]} {lappend fileset_property_errors \"sources_1 VERILOG_DEFINE discovery failed: $vmcp_error\"}"
        "}; "
        "set sim_include_dirs [list]; set sim_verilog_defines [list]; "
        "set sim_fs [get_filesets -quiet {sim_1}]; "
        "if {[llength $sim_fs] > 1} {lappend fileset_property_errors {sim_1 fileset is ambiguous}} elseif {[llength $sim_fs] == 1} {"
        "if {[catch {set sim_include_dirs [get_property INCLUDE_DIRS $sim_fs]} vmcp_error]} {lappend fileset_property_errors \"sim_1 INCLUDE_DIRS discovery failed: $vmcp_error\"}; "
        "if {[catch {set sim_verilog_defines [get_property VERILOG_DEFINE $sim_fs]} vmcp_error]} {lappend fileset_property_errors \"sim_1 VERILOG_DEFINE discovery failed: $vmcp_error\"}"
        "}; "
        "set bit_files [list]; "
        "foreach pattern [list "
        "[file join $project_dir ${project_name}.runs impl_1 *.bit] "
        "[file join $project_dir ${project_name}.runs * *.bit]"
        "] {foreach f [glob -nocomplain $pattern] {if {[lsearch -exact $bit_files $f] < 0} {lappend bit_files $f}}}; "
        "join [list "
        "\"project_name=$project_name\" "
        "\"project_dir=$project_dir\" "
        "\"part=[get_property PART $p]\" "
        "\"top=$top\" "
        "\"sim_top=$sim_top\" "
        "\"target_language=$target_language\" "
        "\"target_simulator=$target_simulator\" "
        "\"source_include_dirs=[::vivado_agent_mcp_wire_list $source_include_dirs]\" "
        "\"source_verilog_defines=[::vivado_agent_mcp_wire_list $source_verilog_defines]\" "
        "\"sim_include_dirs=[::vivado_agent_mcp_wire_list $sim_include_dirs]\" "
        "\"sim_verilog_defines=[::vivado_agent_mcp_wire_list $sim_verilog_defines]\" "
        "\"fileset_property_errors=[::vivado_agent_mcp_wire_list $fileset_property_errors]\" "
        "\"filesets=[::vivado_agent_mcp_wire_list [get_filesets -quiet *]]\" "
        "\"runs=[::vivado_agent_mcp_wire_list [get_runs -quiet *]]\" "
        "\"bitstream_files=[::vivado_agent_mcp_wire_list $bit_files]\""
        "] \"\\n\""
    )


def list_fileset_files_command(*, fileset: str = "sources_1") -> str:
    fileset_ref = tcl_list_quote(fileset)
    return (
        f"{tcl_wire_prelude()}; "
        f"set fs [get_filesets {fileset_ref}]; "
        "set rows [list]; set discovery_errors [list]; set vmcp_compile_order 0; "
        "foreach f [get_files -quiet -of_objects $fs] {"
        "set path [file normalize $f]; "
        "set props [list]; "
        "if {[catch {set props [list_property $f]} vmcp_error]} {lappend discovery_errors \"$path property inventory failed: $vmcp_error\"}; "
        "set file_type \"\"; set library \"\"; set managed 0; set is_global_include \"\"; "
        "set used_in_synthesis \"\"; set used_in_implementation \"\"; set used_in_simulation \"\"; "
        "set processing_order \"\"; set scoped_to_ref \"\"; set scoped_to_cells [list]; "
        "foreach property_name [list FILE_TYPE LIBRARY IS_MANAGED IS_GLOBAL_INCLUDE USED_IN_SYNTHESIS USED_IN_IMPLEMENTATION USED_IN_SIMULATION PROCESSING_ORDER SCOPED_TO_REF SCOPED_TO_CELLS] {"
        "if {[lsearch -exact $props $property_name] < 0} {continue}; "
        "if {[catch {set property_value [get_property $property_name $f]} vmcp_error]} {lappend discovery_errors \"$path $property_name discovery failed: $vmcp_error\"; continue}; "
        "switch -- $property_name {"
        "FILE_TYPE {set file_type $property_value} LIBRARY {set library $property_value} IS_MANAGED {set managed $property_value} "
        "IS_GLOBAL_INCLUDE {set is_global_include $property_value} USED_IN_SYNTHESIS {set used_in_synthesis $property_value} "
        "USED_IN_IMPLEMENTATION {set used_in_implementation $property_value} USED_IN_SIMULATION {set used_in_simulation $property_value} "
        "PROCESSING_ORDER {set processing_order $property_value} SCOPED_TO_REF {set scoped_to_ref $property_value} "
        "SCOPED_TO_CELLS {set scoped_to_cells $property_value}"
        "}"
        "}; "
        "set exists [expr {[file exists $path] ? 1 : 0}]; "
        "if {!$exists} {lappend discovery_errors \"$path does not exist\"}; "
        f"lappend rows [::vivado_agent_mcp_wire_row [list path $path fileset {fileset_ref} type $file_type library $library compile_order $vmcp_compile_order exists $exists managed $managed "
        "is_global_include $is_global_include used_in_synthesis $used_in_synthesis used_in_implementation $used_in_implementation "
        "used_in_simulation $used_in_simulation processing_order $processing_order scoped_to_ref $scoped_to_ref "
        "scoped_to_cells [::vivado_agent_mcp_wire_list $scoped_to_cells]]]; incr vmcp_compile_order"
        "}; "
        "lappend rows [::vivado_agent_mcp_wire_row [list vmcp_meta 1 discovery_errors [::vivado_agent_mcp_wire_list $discovery_errors]]]; "
        "join $rows \"\\n\""
    )


def replay_file_specs_command(file_specs: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for index, spec in enumerate(file_specs):
        target_var = f"vmcp_file_{index}"
        matches_var = f"vmcp_matches_{index}"
        fileset_ref = tcl_list_quote(str(spec["fileset"]))
        path_ref = tcl_list_quote(str(spec["path"]))
        mismatch_message = tcl_list_quote(
            f"VMCP_FILE_SPEC_TARGET_MISMATCH: {spec['fileset']} {spec['path']}"
        )
        parts.extend(
            [
                f"set {matches_var} [list]",
                f"foreach vmcp_candidate [get_files -quiet -of_objects [get_filesets {fileset_ref}]] "
                f"{{if {{[file normalize $vmcp_candidate] eq [file normalize {path_ref}]}} {{lappend {matches_var} $vmcp_candidate}}}}",
                f"if {{[llength ${matches_var}] != 1}} {{error {mismatch_message}}}",
                f"set {target_var} [lindex ${matches_var} 0]",
            ]
        )
        property_values = {
            "FILE_TYPE": spec["file_type"],
            "LIBRARY": spec["library"],
            "IS_GLOBAL_INCLUDE": _tcl_bool(spec["is_global_include"]),
            "USED_IN_SYNTHESIS": _tcl_bool(spec["used_in_synthesis"]),
            "USED_IN_IMPLEMENTATION": _tcl_bool(spec["used_in_implementation"]),
            "USED_IN_SIMULATION": _tcl_bool(spec["used_in_simulation"]),
            "PROCESSING_ORDER": spec["processing_order"],
            "SCOPED_TO_REF": spec["scoped_to_ref"],
        }
        for property_name, value in property_values.items():
            if value is None or value == "":
                continue
            parts.append(f"set_property {property_name} {tcl_list_quote(str(value))} ${target_var}")
        scoped_to_cells = spec["scoped_to_cells"]
        if scoped_to_cells:
            parts.append(f"set_property SCOPED_TO_CELLS {_tcl_list(scoped_to_cells)} ${target_var}")
    for fileset in sorted(SUPPORTED_FILESETS):
        ordered_specs = sorted(
            (spec for spec in file_specs if spec["fileset"] == fileset),
            key=lambda item: int(item["compile_order"]),
        )
        if not ordered_specs:
            continue
        fileset_ref = tcl_list_quote(fileset)
        if fileset in {"sources_1", "sim_1"}:
            parts.append(f"update_compile_order -fileset {fileset_ref}")
        parts.append(
            f"reorder_files -fileset {fileset_ref} -front "
            f"{_tcl_list([str(spec['path']) for spec in ordered_specs])}"
        )
    return "; ".join(parts)


def add_project_files_command(
    *,
    fileset: str = "sources_1",
    files: list[str],
    copy_to_project: bool = False,
) -> str:
    fileset_ref = tcl_list_quote(fileset)
    files_ref = _tcl_list(files)
    parts = [
        f"add_files -fileset {fileset_ref} {files_ref}",
    ]
    if copy_to_project:
        parts.append(f"import_files -fileset {fileset_ref} -force -norecurse {files_ref}")
    parts.extend(
        [
            f"update_compile_order -fileset {fileset_ref}",
            "join [list "
            f"{tcl_list_quote(f'fileset={fileset}')} "
            f"\"file_count={len(files)}\" "
            f"\"copy_to_project={1 if copy_to_project else 0}\""
            "] \"\\n\"",
        ]
    )
    return "; ".join(parts)


def remove_project_files_command(*, fileset: str = "sources_1", files: list[str]) -> str:
    fileset_ref = tcl_list_quote(fileset)
    files_ref = _tcl_list(files)
    return (
        f"remove_files {files_ref}; "
        f"update_compile_order -fileset {fileset_ref}; "
        "join [list "
        f"{tcl_list_quote(f'fileset={fileset}')} "
        f"\"removed_count={len(files)}\""
        "] \"\\n\""
    )


def set_project_top_command(*, top: str, fileset: str = "sources_1") -> str:
    fileset_ref = tcl_list_quote(fileset)
    top_ref = tcl_list_quote(top)
    return (
        f"set_property top {top_ref} [get_filesets {fileset_ref}]; "
        f"update_compile_order -fileset {fileset_ref}; "
        "join [list "
        f"{tcl_list_quote(f'fileset={fileset}')} "
        f"{tcl_list_quote(f'top={top}')}"
        "] \"\\n\""
    )


def set_project_part_command(*, part: str) -> str:
    part_ref = tcl_list_quote(part)
    return (
        f"{tcl_wire_prelude()}; "
        f"set_property part {part_ref} [current_project]; "
        "set rows [list]; "
        "foreach r [get_runs -quiet *] {"
        "lappend rows [::vivado_agent_mcp_wire_row [list run [get_property NAME $r] needs_refresh [get_property NEEDS_REFRESH $r] status [get_property STATUS $r]]]"
        "}; "
        f"set rows [linsert $rows 0 {tcl_list_quote(f'part={part}')}]; "
        "join $rows \"\\n\""
    )


def update_compile_order_command(*, filesets: list[str] | None = None) -> str:
    selected = filesets or ["sources_1", "sim_1"]
    commands = [f"update_compile_order -fileset {tcl_list_quote(fileset)}" for fileset in selected]
    commands.append("join [list " + " ".join(tcl_list_quote(f"fileset={fileset}") for fileset in selected) + "] \"\\n\"")
    return "; ".join(commands)


def parse_project_state(raw: str) -> dict[str, Any]:
    values = _parse_key_value_lines(raw)
    source_defines, source_define_errors = _parse_define_property(
        decode_wire_list(values.get("source_verilog_defines", "")),
        fileset="sources_1",
    )
    sim_defines, sim_define_errors = _parse_define_property(
        decode_wire_list(values.get("sim_verilog_defines", "")),
        fileset="sim_1",
    )
    property_errors = decode_wire_list(values.get("fileset_property_errors", ""))
    property_errors.extend(source_define_errors)
    property_errors.extend(sim_define_errors)
    return {
        "ok": True,
        "project": {
            "name": values.get("project_name", ""),
            "directory": values.get("project_dir", ""),
            "part": values.get("part", ""),
            "top": values.get("top", ""),
            "sim_top": values.get("sim_top", ""),
            "target_language": values.get("target_language", ""),
            "target_simulator": values.get("target_simulator", ""),
        },
        "fileset_properties": {
            "discovery_status": "READY" if not property_errors else "BLOCK",
            "errors": property_errors,
            "sources_1": {
                "include_dirs": decode_wire_list(values.get("source_include_dirs", "")),
                "defines": source_defines,
            },
            "sim_1": {
                "include_dirs": decode_wire_list(values.get("sim_include_dirs", "")),
                "defines": sim_defines,
            },
        },
        "filesets": decode_wire_list(values.get("filesets", "")),
        "runs": decode_wire_list(values.get("runs", "")),
        "artifacts": {
            "bitstream_files": _unique_preserving_order(decode_wire_list(values.get("bitstream_files", ""))),
        },
        "raw": raw,
    }


def parse_fileset_files(raw: str, *, fileset: str = "") -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    file_specs: list[dict[str, Any]] = []
    discovery_errors: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        values = _parse_delimited_fields(stripped)
        if values.get("vmcp_meta") == "1":
            discovery_errors.extend(decode_wire_list(values.get("discovery_errors", "")))
            continue
        files.append(
            {
                "path": values.get("path", ""),
                "file_type": values.get("type", ""),
                "exists": values.get("exists", "0") == "1",
                "managed": values.get("managed", "0") == "1",
            }
        )
        optional_boole: dict[str, bool | None] = {}
        for field in FILE_SPEC_BOOLEAN_FIELDS:
            wire_value = values.get(field, "")
            try:
                optional_boole[field] = _parse_optional_bool(wire_value)
            except ValueError:
                optional_boole[field] = None
                discovery_errors.append(
                    f"{values.get('path', '')} {field} contains an invalid Vivado boolean value: {wire_value!r}"
                )
        spec = {
            "path": values.get("path", ""),
            "fileset": values.get("fileset", fileset),
            "file_type": values.get("type", ""),
            "library": values.get("library", ""),
            "compile_order": _int_or_invalid(values.get("compile_order", "")),
            **optional_boole,
            "processing_order": values.get("processing_order", ""),
            "scoped_to_ref": values.get("scoped_to_ref", ""),
            "scoped_to_cells": decode_wire_list(values.get("scoped_to_cells", "")),
        }
        file_specs.append(spec)
    normalized_specs, spec_errors = normalize_project_file_specs(file_specs)
    discovery_errors.extend(spec_errors)
    file_specs = normalized_specs
    status = "READY" if not discovery_errors else "BLOCK"
    return {
        "ok": True,
        "files": files,
        "file_specs": file_specs,
        "semantic_inventory_digest": file_spec_inventory_digest(file_specs) if status == "READY" else "",
        "reconstruction_status": status,
        "discovery_errors": discovery_errors,
        "raw": raw,
    }


def normalize_project_file_specs(
    file_specs: list[dict[str, Any]],
    *,
    rtl_files: list[str] | None = None,
    xdc_files: list[str] | None = None,
    sim_files: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, raw_spec in enumerate(file_specs):
        if not isinstance(raw_spec, dict):
            errors.append(f"file_specs[{index}] must be an object")
            continue
        missing = [field for field in FILE_SPEC_FIELDS if field not in raw_spec]
        extra = sorted(set(raw_spec) - set(FILE_SPEC_FIELDS))
        if missing:
            errors.append(f"file_specs[{index}] is missing fields: {', '.join(missing)}")
        if extra:
            errors.append(f"file_specs[{index}] contains unsupported fields: {', '.join(extra)}")
        if missing or extra:
            continue
        spec = dict(raw_spec)
        path = str(spec["path"])
        fileset = str(spec["fileset"])
        file_type = str(spec["file_type"])
        library = str(spec["library"])
        compile_order = spec["compile_order"]
        processing_order = str(spec["processing_order"]).upper()
        scoped_to_ref = str(spec["scoped_to_ref"])
        scoped_to_cells = spec["scoped_to_cells"]
        if not path or "\x00" in path:
            errors.append(f"file_specs[{index}].path must be a non-empty path without NUL")
        if fileset not in SUPPORTED_FILESETS:
            errors.append(f"file_specs[{index}].fileset is not supported: {fileset!r}")
        if file_type not in SUPPORTED_FILE_TYPES:
            errors.append(f"file_specs[{index}].file_type is not supported: {file_type!r}")
        if library and not _LIBRARY_RE.fullmatch(library):
            errors.append(f"file_specs[{index}].library is not a safe Vivado library name: {library!r}")
        if isinstance(compile_order, bool) or not isinstance(compile_order, int) or compile_order < 0:
            errors.append(f"file_specs[{index}].compile_order must be a non-negative integer")
            compile_order = -1
        if processing_order not in SUPPORTED_PROCESSING_ORDERS:
            errors.append(f"file_specs[{index}].processing_order is not supported: {processing_order!r}")
        if fileset != "constrs_1" and (processing_order or scoped_to_ref or scoped_to_cells):
            errors.append(f"file_specs[{index}] uses XDC-only scope/order properties outside constrs_1")
        if not isinstance(scoped_to_cells, list) or not all(isinstance(item, str) and "\x00" not in item for item in scoped_to_cells):
            errors.append(f"file_specs[{index}].scoped_to_cells must be an array of strings without NUL")
            scoped_to_cells = []
        bool_values: dict[str, bool | None] = {}
        for field in FILE_SPEC_BOOLEAN_FIELDS:
            value = spec[field]
            if value is not None and not isinstance(value, bool):
                errors.append(f"file_specs[{index}].{field} must be boolean or null")
                value = None
            bool_values[field] = value
        key = (fileset, _file_spec_path_key(path))
        if key in seen:
            errors.append(f"file_specs contains duplicate path in {fileset}: {path}")
        seen.add(key)
        normalized.append(
            {
                "path": path,
                "fileset": fileset,
                "file_type": file_type,
                "library": library,
                "compile_order": compile_order,
                **bool_values,
                "processing_order": processing_order,
                "scoped_to_ref": scoped_to_ref,
                "scoped_to_cells": sorted(scoped_to_cells),
            }
        )
    for fileset in SUPPORTED_FILESETS:
        orders = sorted(
            spec["compile_order"]
            for spec in normalized
            if spec["fileset"] == fileset and spec["compile_order"] >= 0
        )
        if orders and orders != list(range(len(orders))):
            errors.append(f"file_specs compile_order for {fileset} must be unique and contiguous from zero")
    if rtl_files is not None or xdc_files is not None or sim_files is not None:
        expected = {
            ("sources_1", _file_spec_path_key(path)) for path in (rtl_files or [])
        } | {
            ("constrs_1", _file_spec_path_key(path)) for path in (xdc_files or [])
        } | {
            ("sim_1", _file_spec_path_key(path)) for path in (sim_files or [])
        }
        actual = {(spec["fileset"], _file_spec_path_key(spec["path"])) for spec in normalized}
        if expected != actual:
            missing = sorted(f"{fileset}:{path}" for fileset, path in expected - actual)
            extra = sorted(f"{fileset}:{path}" for fileset, path in actual - expected)
            if missing:
                errors.append(f"file_specs is missing source entries: {missing}")
            if extra:
                errors.append(f"file_specs contains entries outside the project file arrays: {extra}")
    normalized.sort(key=lambda item: (item["fileset"], item["compile_order"], _file_spec_path_key(item["path"])))
    return normalized, errors


def file_spec_inventory_digest(file_specs: list[dict[str, Any]]) -> str:
    normalized, errors = normalize_project_file_specs(file_specs)
    if errors:
        return ""
    payload = [
        {
            **spec,
            "path": _file_spec_path_key(spec["path"]),
        }
        for spec in normalized
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def compare_file_spec_inventories(
    expected_specs: list[dict[str, Any]],
    actual_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    expected, expected_errors = normalize_project_file_specs(expected_specs)
    actual, actual_errors = normalize_project_file_specs(actual_specs)
    expected_by_key = {(item["fileset"], _file_spec_path_key(item["path"])): item for item in expected}
    actual_by_key = {(item["fileset"], _file_spec_path_key(item["path"])): item for item in actual}
    missing = [expected_by_key[key] for key in sorted(expected_by_key.keys() - actual_by_key.keys())]
    extra = [actual_by_key[key] for key in sorted(actual_by_key.keys() - expected_by_key.keys())]
    changed: list[dict[str, Any]] = []
    for key in sorted(expected_by_key.keys() & actual_by_key.keys()):
        expected_item = expected_by_key[key]
        actual_item = actual_by_key[key]
        differences = {
            field: {"expected": expected_item[field], "actual": actual_item[field]}
            for field in FILE_SPEC_FIELDS
            if field != "path" and expected_item[field] != actual_item[field]
        }
        if differences:
            changed.append({"path": expected_item["path"], "fileset": expected_item["fileset"], "differences": differences})
    matches = not expected_errors and not actual_errors and not missing and not extra and not changed
    return {
        "matches": matches,
        "expected_digest": file_spec_inventory_digest(expected) if not expected_errors else "",
        "actual_digest": file_spec_inventory_digest(actual) if not actual_errors else "",
        "expected_errors": expected_errors,
        "actual_errors": actual_errors,
        "missing": missing,
        "extra": extra,
        "changed": changed,
    }


def parse_run_refresh_rows(raw: str) -> dict[str, Any]:
    values = _parse_key_value_lines(raw)
    runs: list[dict[str, str]] = []
    for line in raw.splitlines():
        if line.startswith("run="):
            runs.append(_parse_delimited_fields(line))
    return {"ok": True, "part": values.get("part", ""), "runs": runs, "raw": raw}


def _tcl_list(values: list[str]) -> str:
    return "[list " + " ".join(tcl_list_quote(value) for value in values) + "]"


def _parse_define_property(values: list[str], *, fileset: str) -> tuple[dict[str, str | None], list[str]]:
    defines: dict[str, str | None] = {}
    errors: list[str] = []
    for item in values:
        name, separator, value = item.partition("=")
        if not name:
            errors.append(f"{fileset} contains an empty VERILOG_DEFINE name")
            continue
        if name in defines:
            errors.append(f"{fileset} contains duplicate VERILOG_DEFINE name: {name}")
            continue
        defines[name] = value if separator else None
    try:
        return validate_simulation_defines(defines), errors
    except ValueError as exc:
        errors.append(f"{fileset} VERILOG_DEFINE cannot be reproduced safely: {exc}")
        return {}, errors


def _parse_key_value_lines(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line and "|" not in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _parse_delimited_fields(line: str) -> dict[str, str]:
    return decode_wire_row(line)


def _unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _parse_optional_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"invalid optional boolean value: {value!r}")


def _int_or_invalid(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _file_spec_path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).replace("\\", "/")


def _tcl_bool(value: bool | None) -> str | None:
    if value is None:
        return None
    return "1" if value else "0"
