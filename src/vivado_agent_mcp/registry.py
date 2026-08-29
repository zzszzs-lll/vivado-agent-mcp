from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any

import mcp.types as types

from .capability_spec import CapabilitySpec
from .result import RESPONSE_SCHEMA_VERSION
from .vivado.runs import ALLOWED_RUN_PROPERTIES, SUPPORTED_VIVADO_VERSION


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _vivado_path_assertion_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "description": (
            "Optional identity assertion. It must match the canonical executable identity "
            "captured from VIVADO_PATH when the MCP server started and cannot override it."
        ),
    }


def _output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer"},
            "ok": {"type": "boolean"},
            "tool": {"type": "string"},
            "summary": {"type": "string"},
            "data": {"type": "object"},
            "error_code": {"type": "string"},
            "message": {"type": "string"},
            "raw_excerpt": {"type": "string"},
            "policy_allowed": {"type": "boolean"},
            "handoff_reviewable": {"type": "boolean"},
            "assessment_status": {"type": "string", "enum": ["READY", "WARN", "BLOCK", "NOT_APPLICABLE"]},
            "stop_required": {"type": "boolean"},
            "handoff_ready": {"type": "boolean"},
            "next_steps": {"type": "array", "items": {"type": "string"}},
            "next_actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "reason": {"type": "string"},
                        "required_args": {"type": "array", "items": {"type": "string"}},
                        "arg_sources": {"type": "object"},
                        "preconditions": {"type": "array", "items": {"type": "string"}},
                        "stop_condition": {"type": "string"},
                        "optional": {"type": "boolean"},
                    },
                    "required": ["tool", "reason", "required_args", "arg_sources", "preconditions", "stop_condition", "optional"],
                    "additionalProperties": False,
                },
            },
            "hardware_validation": {"type": "object"},
            "resume_context": {"type": "object"},
        },
        "required": [
            "schema_version",
            "ok",
            "tool",
            "summary",
            "message",
            "error_code",
            "data",
            "assessment_status",
            "stop_required",
            "handoff_ready",
        ],
        "additionalProperties": True,
    }


# Private schema seeds are normalized once; every runtime consumer uses CAPABILITY_SPECS.
_CAPABILITY_SCHEMA_SEEDS = [
    types.Tool(
        name="get_tool_catalog",
        description="Return the Agent-facing Vivado MCP capability matrix and tool groups, with optional full CapabilitySpec metadata.",
        inputSchema=_schema({"detail": {"type": "string", "enum": ["compact", "full"]}}),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="get_agent_workflows", description="Return standard Agent workflow recipes for no-board Project Mode PL development.", inputSchema=_schema({}), outputSchema=_output_schema()),
    types.Tool(name="get_agent_scenarios", description="Return reusable Subagent validation scenarios for Agent-facing Vivado MCP acceptance.", inputSchema=_schema({"scenario_id": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(name="get_workflow_trace_status", description="Return the current Agent workflow transcript status and recoverable handoff pointers.", inputSchema=_schema({}), outputSchema=_output_schema()),
    types.Tool(
        name="detect_vivado_environment",
        description="Detect the server-start VIVADO_PATH environment and optionally run a bounded batch probe; vivado_path can only assert the same canonical executable identity.",
        inputSchema=_schema(
            {
                "vivado_path": _vivado_path_assertion_schema(),
                "probe_launch": {"type": "boolean"},
                "probe_timeout_s": {"type": "integer"},
                "runtime_dir": {"type": "string"},
            }
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="detect_hardware_environment", description="Detect Hardware Manager companion tools under the server-start VIVADO_PATH; vivado_path cannot override the configured executable.", inputSchema=_schema({"vivado_path": _vivado_path_assertion_schema()}), outputSchema=_output_schema()),
    types.Tool(name="start_session", description="Start a visible Vivado GUI session from the server-start VIVADO_PATH identity and open a local TCP Tcl channel.", inputSchema=_schema({"vivado_path": _vivado_path_assertion_schema(), "port": {"type": "integer"}, "timeout_s": {"type": "integer"}, "runtime_dir": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(name="stop_session", description="Stop the managed Vivado session.", inputSchema=_schema({}), outputSchema=_output_schema()),
    types.Tool(name="session_status", description="Get managed Vivado session status.", inputSchema=_schema({}), outputSchema=_output_schema()),
    types.Tool(name="get_runtime_cache_status", description="Inspect the MCP runtime directory and summarize temporary Vivado cache candidates.", inputSchema=_schema({"runtime_dir": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(
        name="clean_runtime_cache",
        description="Dry-run or safely clean temporary files from the MCP runtime directory without touching project vmcp_* artifacts.",
        inputSchema=_schema(
            {
                "runtime_dir": {"type": "string"},
                "dry_run": {"type": "boolean"},
                "max_age_hours": {"type": "number"},
                "include_unknown": {"type": "boolean"},
                "runtime_identity": {"type": "string"},
                "plan_sha256": {"type": "string"},
                "execution_intent": {"type": "string"},
                "confirm": {"type": "string"},
            }
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="open_hardware_manager", description="Open Vivado Hardware Manager in the current Vivado session.", inputSchema=_schema({"timeout_s": {"type": "integer"}, "hardware_mode": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(name="close_hardware_manager", description="Close Vivado Hardware Manager.", inputSchema=_schema({"timeout_s": {"type": "integer"}, "hardware_mode": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(name="connect_hw_server", description="Connect to a local or explicitly specified Vivado hw_server.", inputSchema=_schema({"host": {"type": "string"}, "port": {"type": "integer"}, "timeout_s": {"type": "integer"}, "hardware_mode": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(name="disconnect_hw_server", description="Disconnect from Vivado hw_server.", inputSchema=_schema({"timeout_s": {"type": "integer"}, "hardware_mode": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(name="list_hw_targets", description="List Vivado Hardware Manager targets.", inputSchema=_schema({"timeout_s": {"type": "integer"}, "hardware_mode": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(name="open_hw_target", description="Open a Vivado hardware target by name or index.", inputSchema=_schema({"target": {"type": "string"}, "index": {"type": "integer"}, "timeout_s": {"type": "integer"}, "hardware_mode": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(name="close_hw_target", description="Close a Vivado hardware target by name or current target.", inputSchema=_schema({"target": {"type": "string"}, "timeout_s": {"type": "integer"}, "hardware_mode": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(name="list_hw_devices", description="List FPGA devices visible to Vivado Hardware Manager.", inputSchema=_schema({"timeout_s": {"type": "integer"}, "hardware_mode": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(name="select_hw_device", description="Select the current FPGA device by device name, part, or index.", inputSchema=_schema({"device": {"type": "string"}, "part": {"type": "string"}, "index": {"type": "integer"}, "timeout_s": {"type": "integer"}, "hardware_mode": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(
        name="program_hw_device",
        description="Program an FPGA device with an explicit .bit file and optional .ltx probes file.",
        inputSchema=_schema(
            {
                "bitstream_path": {"type": "string"},
                "ltx_path": {"type": "string"},
                "device": {"type": "string"},
                "target": {"type": "string"},
                "timeout_s": {"type": "integer"},
                "hardware_intent": {"type": "string"},
                "confirm": {"type": "string"},
                "board_fingerprint": {"type": "string"},
                "expected_bitstream_sha256": {"type": "string"},
                "hardware_mode": {"type": "string"},
            },
            ["bitstream_path"],
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="program_from_artifact_manifest",
        description="Program an FPGA device using .bit and optional .ltx paths from a collected artifact manifest.",
        inputSchema=_schema(
            {
                "manifest_path": {"type": "string"},
                "run_name": {"type": "string"},
                "device": {"type": "string"},
                "target": {"type": "string"},
                "timeout_s": {"type": "integer"},
                "hardware_intent": {"type": "string"},
                "confirm": {"type": "string"},
                "board_fingerprint": {"type": "string"},
                "expected_bitstream_sha256": {"type": "string"},
                "manifest_sha256": {"type": "string"},
                "hardware_mode": {"type": "string"},
            },
            ["manifest_path"],
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="get_hw_device_status", description="Read current or selected hardware device programming status.", inputSchema=_schema({"device": {"type": "string"}, "timeout_s": {"type": "integer"}, "hardware_mode": {"type": "string"}}), outputSchema=_output_schema()),
    types.Tool(name="get_hardware_messages", description="Read-only parse Hardware Manager and Vivado hardware-related messages from logs; does not connect to hardware.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(
        name="run_tcl",
        description="Classify a raw Vivado Tcl command in dry-run mode; public raw Tcl execution is disabled.",
        inputSchema=_schema(
            {
                "command": {"type": "string"},
                "timeout_s": {"type": "integer"},
                "dry_run": {"type": "boolean"},
                "execution_intent": {"type": "string"},
                "allow_project_write": {"type": "boolean"},
                "allow_destructive": {"type": "boolean"},
                "allow_hardware": {"type": "boolean"},
                "hardware_mode": {"type": "string"},
                "allow_external": {"type": "boolean"},
                "allow_unrestricted": {"type": "boolean"},
                "confirm": {"type": "string"},
            },
            ["command"],
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="safe_tcl",
        description="Render and classify a Tcl-list-escaped template in dry-run mode; public Tcl execution is disabled.",
        inputSchema=_schema(
            {
                "template": {"type": "string"},
                "args": {"type": "object"},
                "timeout_s": {"type": "integer"},
                "dry_run": {"type": "boolean"},
                "execution_intent": {"type": "string"},
                "allow_project_write": {"type": "boolean"},
                "allow_destructive": {"type": "boolean"},
                "allow_hardware": {"type": "boolean"},
                "hardware_mode": {"type": "string"},
                "allow_external": {"type": "boolean"},
                "allow_unrestricted": {"type": "boolean"},
                "confirm": {"type": "string"},
            },
            ["template", "args"],
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="create_project",
        description="Create a Vivado project from existing design, constraint, and simulation files.",
        inputSchema=_schema(
            {
                "project_name": {"type": "string"},
                "project_dir": {"type": "string"},
                "part": {"type": "string"},
                "top": {"type": "string"},
                "rtl_files": {"type": "array", "items": {"type": "string"}},
                "xdc_files": {"type": "array", "items": {"type": "string"}},
                "sim_files": {"type": "array", "items": {"type": "string"}},
                "file_specs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "fileset": {"type": "string", "enum": ["sources_1", "constrs_1", "sim_1"]},
                            "file_type": {"type": "string"},
                            "library": {"type": "string"},
                            "compile_order": {"type": "integer", "minimum": 0},
                            "is_global_include": {"type": ["boolean", "null"]},
                            "used_in_synthesis": {"type": ["boolean", "null"]},
                            "used_in_implementation": {"type": ["boolean", "null"]},
                            "used_in_simulation": {"type": ["boolean", "null"]},
                            "processing_order": {"type": "string"},
                            "scoped_to_ref": {"type": "string"},
                            "scoped_to_cells": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": [
                            "path",
                            "fileset",
                            "file_type",
                            "library",
                            "compile_order",
                            "is_global_include",
                            "used_in_synthesis",
                            "used_in_implementation",
                            "used_in_simulation",
                            "processing_order",
                            "scoped_to_ref",
                            "scoped_to_cells",
                        ],
                        "additionalProperties": False,
                    },
                },
                "testbench_top": {"type": "string"},
                "source_include_dirs": {"type": "array", "items": {"type": "string"}},
                "source_defines": {"type": "object"},
                "include_dirs": {"type": "array", "items": {"type": "string"}},
                "defines": {"type": "object"},
                "target_language": {"type": "string"},
                "simulator": {"type": "string"},
                "force": {"type": "boolean"},
                "timeout_s": {"type": "integer"},
            },
            ["project_name", "project_dir", "part", "top", "rtl_files"],
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="configure_simulation",
        description="Configure a Vivado sim_1 fileset in the currently open project.",
        inputSchema=_schema(
            {
                "sim_files": {"type": "array", "items": {"type": "string"}},
                "testbench_top": {"type": "string"},
                "include_dirs": {"type": "array", "items": {"type": "string"}},
                "defines": {"type": "object"},
                "simulator": {"type": "string"},
                "simset": {"type": "string"},
                "timeout_s": {"type": "integer"},
            },
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="repair_project_setup",
        description="Dry-run or repair Project Mode setup by reconciling RTL, XDC, sim files, tops, SystemVerilog file types, and compile order.",
        inputSchema=_schema(
            {
                "project_path": {"type": "string"},
                "rtl_files": {"type": "array", "items": {"type": "string"}},
                "xdc_files": {"type": "array", "items": {"type": "string"}},
                "sim_files": {"type": "array", "items": {"type": "string"}},
                "top": {"type": "string"},
                "testbench_top": {"type": "string"},
                "target_language": {"type": "string"},
                "include_dirs": {"type": "array", "items": {"type": "string"}},
                "defines": {"type": "object"},
                "simulator": {"type": "string"},
                "dry_run": {"type": "boolean"},
                "timeout_s": {"type": "integer"},
            },
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="run_behavioral_simulation",
        description="Run Vivado XSIM behavioral simulation for a configured simulation fileset.",
        inputSchema=_schema(
            {
                "simset": {"type": "string"},
                "run_time": {"type": "string"},
                "run_all": {"type": "boolean"},
                "export_vcd": {"type": "boolean"},
                "vcd_name": {"type": "string"},
                "max_vcd_mb": {"type": "number"},
                "incremental": {"type": "boolean"},
                "execution_intent": {"type": "string"},
                "confirm": {"type": "string"},
                "timeout_s": {"type": "integer"},
            },
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="get_simulation_result",
        description="Read and parse Vivado XSIM logs and generated waveform artifacts.",
        inputSchema=_schema({"simset": {"type": "string"}, "timeout_s": {"type": "integer"}}),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="create_ip",
        description="Create a Vivado IP customization from a VLNV.",
        inputSchema=_schema(
            {
                "vlnv": {"type": "string"},
                "module_name": {"type": "string"},
                "ip_dir": {"type": "string"},
                "properties": {"type": "object"},
                "timeout_s": {"type": "integer"},
            },
            ["vlnv", "module_name"],
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="configure_ip",
        description="Set Vivado IP CONFIG properties.",
        inputSchema=_schema({"ip_name": {"type": "string"}, "properties": {"type": "object"}, "timeout_s": {"type": "integer"}}, ["ip_name", "properties"]),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="generate_ip_targets",
        description="Generate Vivado IP output products.",
        inputSchema=_schema({"ip_name": {"type": "string"}, "targets": {"type": "array", "items": {"type": "string"}}, "timeout_s": {"type": "integer"}}, ["ip_name"]),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="get_ip_status", description="Read Vivado IP status.", inputSchema=_schema({"ip_name": {"type": "string"}, "timeout_s": {"type": "integer"}}, ["ip_name"]), outputSchema=_output_schema()),
    types.Tool(name="upgrade_ip", description="Upgrade a Vivado IP customization.", inputSchema=_schema({"ip_name": {"type": "string"}, "timeout_s": {"type": "integer"}}, ["ip_name"]), outputSchema=_output_schema()),
    types.Tool(name="export_ip_user_files", description="Export Vivado IP user files.", inputSchema=_schema({"ip_name": {"type": "string"}, "timeout_s": {"type": "integer"}}, ["ip_name"]), outputSchema=_output_schema()),
    types.Tool(
        name="create_block_design",
        description="Create a Vivado Block Design.",
        inputSchema=_schema({"name": {"type": "string"}, "force": {"type": "boolean"}, "timeout_s": {"type": "integer"}}, ["name"]),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="open_block_design", description="Open a Vivado Block Design.", inputSchema=_schema({"name": {"type": "string"}, "timeout_s": {"type": "integer"}}, ["name"]), outputSchema=_output_schema()),
    types.Tool(
        name="add_bd_ip_cell",
        description="Add an IP cell to the current Vivado Block Design.",
        inputSchema=_schema({"vlnv": {"type": "string"}, "cell_name": {"type": "string"}, "properties": {"type": "object"}, "timeout_s": {"type": "integer"}}, ["vlnv", "cell_name"]),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="create_bd_port",
        description="Create a port in the current Vivado Block Design.",
        inputSchema=_schema(
            {
                "name": {"type": "string"},
                "direction": {"type": "string"},
                "type": {"type": "string"},
                "from": {"type": "integer"},
                "to": {"type": "integer"},
                "properties": {"type": "object"},
                "timeout_s": {"type": "integer"},
            },
            ["name", "direction"],
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="connect_bd_net",
        description="Connect scalar Block Design pins and ports.",
        inputSchema=_schema({"source": {"type": "string"}, "targets": {"type": "array", "items": {"type": "string"}}, "timeout_s": {"type": "integer"}}, ["source", "targets"]),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="connect_bd_intf_net",
        description="Connect Block Design interface pins and ports.",
        inputSchema=_schema({"source": {"type": "string"}, "targets": {"type": "array", "items": {"type": "string"}}, "timeout_s": {"type": "integer"}}, ["source", "targets"]),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="validate_block_design", description="Validate the current Vivado Block Design.", inputSchema=_schema({"bd_name": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(
        name="generate_block_design_wrapper",
        description="Generate and add the HDL wrapper for a Vivado Block Design.",
        inputSchema=_schema({"bd_name": {"type": "string"}, "wrapper_top": {"type": "string"}, "set_top": {"type": "boolean"}, "timeout_s": {"type": "integer"}}, ["bd_name"]),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="open_project", description="Open a Vivado .xpr project.", inputSchema=_schema({"project_path": {"type": "string"}, "timeout_s": {"type": "integer"}}, ["project_path"]), outputSchema=_output_schema()),
    types.Tool(name="close_project", description="Close the current Vivado project.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_project_info", description="Get current Vivado project information.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_project_state", description="Get current Vivado project, fileset, run, and artifact state.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="list_fileset_files", description="List files referenced by a Vivado fileset.", inputSchema=_schema({"fileset": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(
        name="add_project_files",
        description="Add existing RTL, XDC, or simulation files to a Vivado fileset.",
        inputSchema=_schema(
            {
                "fileset": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "copy_to_project": {"type": "boolean"},
                "timeout_s": {"type": "integer"},
            },
            ["files"],
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="remove_project_files",
        description="Remove file references from a Vivado fileset without deleting source files from disk.",
        inputSchema=_schema({"fileset": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}, "timeout_s": {"type": "integer"}}, ["files"]),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="set_project_top", description="Set design or simulation top for a Vivado fileset.", inputSchema=_schema({"top": {"type": "string"}, "fileset": {"type": "string"}, "timeout_s": {"type": "integer"}}, ["top"]), outputSchema=_output_schema()),
    types.Tool(name="set_project_part", description="Set current Vivado project part and report run refresh state.", inputSchema=_schema({"part": {"type": "string"}, "timeout_s": {"type": "integer"}}, ["part"]), outputSchema=_output_schema()),
    types.Tool(name="update_project_compile_order", description="Update Vivado compile order for sources_1 and/or sim_1.", inputSchema=_schema({"filesets": {"type": "array", "items": {"type": "string"}}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="check_syntax", description="Run Vivado native syntax check for a fileset.", inputSchema=_schema({"fileset": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_compile_order", description="Inspect Vivado compile order, missing files, duplicates, and unknown file types.", inputSchema=_schema({"fileset": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="analyze_sources", description="Aggregate syntax and compile-order diagnostics for a fileset.", inputSchema=_schema({"fileset": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="run_elaboration", description="Run Vivado RTL elaboration without launching synthesis runs.", inputSchema=_schema({"top": {"type": "string"}, "part": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_elaboration_result", description="Read and parse current Vivado elaboration diagnostics.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_design_hierarchy", description="Summarize ports and cells from the currently open elaborated or synthesized design.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_run_configuration", description="Get Vivado run strategy, status, progress, directory, and raw properties.", inputSchema=_schema({"run_name": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(
        name="configure_run",
        description="Configure a Vivado run strategy and explicit run properties.",
        inputSchema=_schema(
            {
                "run_name": {"type": "string"},
                "strategy": {"type": "string"},
                "properties": {
                    "type": "object",
                    "propertyNames": {"enum": sorted(ALLOWED_RUN_PROPERTIES)},
                    "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
                },
                "timeout_s": {"type": "integer"},
            },
            ["run_name"],
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="reset_runs",
        description="Dry-run or explicitly reset selected Vivado runs; defaults to synth_1 and impl_1.",
        inputSchema=_schema({"run_names": {"type": "array", "items": {"type": "string"}}, "timeout_s": {"type": "integer"}, "dry_run": {"type": "boolean"}, "intent": {"type": "string"}, "confirm": {"type": "string"}}),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="clean_run_outputs",
        description="Delete selected Vivado generated run/simulation/cache outputs inside the current project directory only.",
        inputSchema=_schema(
            {
                "run_names": {"type": "array", "items": {"type": "string"}},
                "simsets": {"type": "array", "items": {"type": "string"}},
                "include_cache": {"type": "boolean"},
                "include_gen": {"type": "boolean"},
                "timeout_s": {"type": "integer"},
                "dry_run": {"type": "boolean"},
                "intent": {"type": "string"},
                "confirm": {"type": "string"},
            }
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="collect_build_artifacts", description="Copy bitstream, probes, checkpoints, reports, and Vivado metadata into vmcp_artifacts and write manifest.json.", inputSchema=_schema({"run_name": {"type": "string"}, "output_dir": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_artifact_manifest", description="Read a collected build artifact manifest.", inputSchema=_schema({"run_name": {"type": "string"}, "manifest_path": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="collect_report_bundle", description="Generate and collect pre-hardware Vivado reports into vmcp_reports with report_manifest.json.", inputSchema=_schema({"run_name": {"type": "string"}, "report_dir": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(
        name="run_project_audit",
        description="Aggregate Project Mode health, signoff, manifests, waivers, and next-step diagnostics without launching build runs.",
        inputSchema=_schema(
            {
                "vivado_path": _vivado_path_assertion_schema(),
                "fileset": {"type": "string"},
                "top": {"type": "string"},
                "part": {"type": "string"},
                "simset": {"type": "string"},
                "run_name": {"type": "string"},
                "project_dir": {"type": "string"},
                "report_manifest_path": {"type": "string"},
                "apply_waivers": {"type": "boolean"},
                "timeout_s": {"type": "integer"},
            }
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="list_signoff_waivers", description="List MCP-managed signoff waivers from vmcp_signoff/waivers.json.", inputSchema=_schema({"project_dir": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(
        name="create_signoff_waiver",
        description="Create an MCP-managed signoff waiver bound to one exact reviewed finding fingerprint.",
        inputSchema=_schema(
            {
                "project_dir": {"type": "string"},
                "id": {"type": "string"},
                "finding_fingerprint": {"type": "string"},
                "evidence_identity_sha256": {"type": "string"},
                "code": {"type": "string"},
                "message_contains": {"type": "string"},
                "source_tool": {"type": "string"},
                "reason": {"type": "string"},
                "owner": {"type": "string"},
                "expires_on": {"type": "string"},
                "enabled": {"type": "boolean"},
                "timeout_s": {"type": "integer"},
            },
            ["id", "finding_fingerprint", "evidence_identity_sha256"],
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="remove_signoff_waiver", description="Remove an MCP-managed signoff waiver by id.", inputSchema=_schema({"project_dir": {"type": "string"}, "id": {"type": "string"}, "timeout_s": {"type": "integer"}}, ["id"]), outputSchema=_output_schema()),
    types.Tool(
        name="collect_diagnostic_bundle",
        description="Collect audit inputs, manifests, waivers, and log tails into vmcp_diagnostics/<timestamp>/diagnostic_manifest.json.",
        inputSchema=_schema(
            {
                "vivado_path": _vivado_path_assertion_schema(),
                "run_name": {"type": "string"},
                "output_dir": {"type": "string"},
                "timestamp": {"type": "string"},
                "reuse_audit_from_manifest": {"type": "string"},
                "timeout_s": {"type": "integer"},
            }
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="validate_diagnostic_bundle",
        description="Validate a project-local diagnostic reference manifest for integrity and reviewability; it is not a portable reproduction bundle.",
        inputSchema=_schema({"manifest_path": {"type": "string"}, "bundle_dir": {"type": "string"}}),
        outputSchema=_output_schema(),
    ),
    types.Tool(
        name="export_project_replay_script",
        description="Export a readable Project Mode Tcl replay script for current project references and report commands.",
        inputSchema=_schema({"vivado_path": _vivado_path_assertion_schema(), "output_path": {"type": "string"}, "timeout_s": {"type": "integer"}}),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="run_synthesis", description="Launch synthesis asynchronously; poll with get_run_progress.", inputSchema=_schema({"run_name": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="run_implementation", description="Launch implementation asynchronously; poll with get_run_progress.", inputSchema=_schema({"run_name": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="generate_bitstream", description="Launch bitstream generation asynchronously for an implementation run; poll with get_run_progress.", inputSchema=_schema({"run_name": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_run_progress", description="Get Vivado run status and progress.", inputSchema=_schema({"run_name": {"type": "string"}, "timeout_s": {"type": "integer"}, "expect_bitstream": {"type": "boolean"}}), outputSchema=_output_schema()),
    types.Tool(name="diagnose_run_failure", description="Aggregate run status, run log tail, and critical messages into a structured run failure diagnosis.", inputSchema=_schema({"run_name": {"type": "string"}, "timeout_s": {"type": "integer"}, "expect_bitstream": {"type": "boolean"}}), outputSchema=_output_schema()),
    types.Tool(name="get_timing_summary", description="Parse report_timing_summary output.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_utilization_report", description="Parse report_utilization output.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_drc_report", description="Parse report_drc output.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_constraints_summary", description="Summarize XDC files, ports, clocks, and common constraint commands.", inputSchema=_schema({"fileset": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="check_timing_constraints", description="Run and parse Vivado check_timing.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_clock_summary", description="Parse Vivado clock reports.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_timing_paths", description="Parse worst setup or hold timing paths.", inputSchema=_schema({"max_paths": {"type": "integer"}, "delay_type": {"type": "string"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_methodology_report", description="Run and parse Vivado report_methodology.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_qor_summary", description="Run and parse Vivado report_qor_summary.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_cdc_report", description="Run and parse Vivado CDC report.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_clock_interaction_report", description="Run and parse Vivado clock interaction report.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_power_report", description="Run and parse Vivado power report.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="analyze_timing_closure", description="Aggregate timing, constraints, methodology, DRC, and run messages into prioritized findings.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="run_pre_hw_signoff", description="Aggregate source, elaboration, simulation, timing, CDC, power, reports, readiness, and signoff waivers into pre-hardware signoff.", inputSchema=_schema({"fileset": {"type": "string"}, "top": {"type": "string"}, "part": {"type": "string"}, "simset": {"type": "string"}, "run_name": {"type": "string"}, "project_dir": {"type": "string"}, "project_path": {"type": "string"}, "report_manifest_path": {"type": "string"}, "apply_waivers": {"type": "boolean"}, "timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(
        name="create_managed_xdc",
        description="Create a separate MCP-managed XDC file and add it to constrs_1.",
        inputSchema=_schema(
            {
                "name": {"type": "string"},
                "constraints": {"type": "array", "items": {"type": "object"}},
                "fileset": {"type": "string"},
                "force": {"type": "boolean"},
                "timeout_s": {"type": "integer"},
            },
            ["name", "constraints"],
        ),
        outputSchema=_output_schema(),
    ),
    types.Tool(name="get_messages", description="Parse Vivado messages.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="get_critical_warnings", description="Return ERROR and CRITICAL WARNING messages.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
    types.Tool(name="check_bitstream_readiness", description="Aggregate timing, DRC, and critical messages into READY/WARN/BLOCK.", inputSchema=_schema({"timeout_s": {"type": "integer"}}), outputSchema=_output_schema()),
]

CAPABILITY_DOMAIN_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "agent_guidance": (
        "get_tool_catalog",
        "get_agent_workflows",
        "get_agent_scenarios",
        "get_workflow_trace_status",
    ),
    "session": (
        "detect_vivado_environment",
        "start_session",
        "session_status",
        "stop_session",
    ),
    "runtime_lightweight": ("get_runtime_cache_status", "clean_runtime_cache"),
    "project": (
        "create_project",
        "repair_project_setup",
        "open_project",
        "close_project",
        "get_project_info",
        "get_project_state",
        "add_project_files",
        "remove_project_files",
        "list_fileset_files",
        "set_project_top",
        "set_project_part",
        "update_project_compile_order",
    ),
    "simulation": (
        "configure_simulation",
        "run_behavioral_simulation",
        "get_simulation_result",
    ),
    "ip": (
        "create_ip",
        "configure_ip",
        "generate_ip_targets",
        "export_ip_user_files",
        "get_ip_status",
        "upgrade_ip",
    ),
    "block_design": (
        "create_block_design",
        "open_block_design",
        "add_bd_ip_cell",
        "create_bd_port",
        "connect_bd_net",
        "connect_bd_intf_net",
        "validate_block_design",
        "generate_block_design_wrapper",
    ),
    "runs": (
        "run_synthesis",
        "run_implementation",
        "generate_bitstream",
        "get_run_progress",
        "diagnose_run_failure",
        "get_run_configuration",
        "configure_run",
        "reset_runs",
        "clean_run_outputs",
        "collect_build_artifacts",
        "get_artifact_manifest",
    ),
    "constraints": (
        "create_managed_xdc",
        "get_constraints_summary",
        "check_timing_constraints",
        "get_clock_summary",
        "get_timing_paths",
        "analyze_timing_closure",
        "check_bitstream_readiness",
    ),
    "reports": (
        "get_timing_summary",
        "get_utilization_report",
        "get_drc_report",
        "get_methodology_report",
        "get_qor_summary",
        "get_cdc_report",
        "get_clock_interaction_report",
        "get_power_report",
        "get_messages",
        "get_critical_warnings",
        "collect_report_bundle",
    ),
    "diagnostics": (
        "analyze_sources",
        "check_syntax",
        "get_compile_order",
        "run_elaboration",
        "get_elaboration_result",
        "get_design_hierarchy",
        "run_pre_hw_signoff",
        "run_project_audit",
        "list_signoff_waivers",
        "create_signoff_waiver",
        "remove_signoff_waiver",
        "collect_diagnostic_bundle",
        "export_project_replay_script",
        "validate_diagnostic_bundle",
    ),
    "hardware_boundary": (
        "detect_hardware_environment",
        "open_hardware_manager",
        "close_hardware_manager",
        "connect_hw_server",
        "disconnect_hw_server",
        "list_hw_targets",
        "open_hw_target",
        "close_hw_target",
        "list_hw_devices",
        "select_hw_device",
        "program_hw_device",
        "program_from_artifact_manifest",
        "get_hw_device_status",
        "get_hardware_messages",
    ),
    "custom_tcl": ("run_tcl", "safe_tcl"),
}

CAPABILITY_WORKFLOW_SEQUENCES: dict[str, tuple[str, ...]] = {
    "new_project_to_bitstream": (
        "start_session",
        "create_project",
        "repair_project_setup",
        "configure_simulation",
        "check_syntax",
        "get_compile_order",
        "run_behavioral_simulation",
        "run_synthesis",
        "get_run_progress",
        "run_implementation",
        "get_run_progress",
        "generate_bitstream",
        "get_run_progress",
        "collect_build_artifacts",
        "collect_report_bundle",
        "run_pre_hw_signoff",
        "run_project_audit",
        "collect_diagnostic_bundle",
        "validate_diagnostic_bundle",
    ),
    "existing_project_audit": (
        "start_session",
        "open_project",
        "get_project_state",
        "list_fileset_files",
        "validate_diagnostic_bundle",
        "get_workflow_trace_status",
        "stop_session",
    ),
    "simulation_failure_repair": (
        "get_simulation_result",
        "analyze_sources",
        "check_syntax",
        "get_compile_order",
        "run_behavioral_simulation",
    ),
    "timing_failure_repair": (
        "diagnose_run_failure",
        "get_constraints_summary",
        "check_timing_constraints",
        "get_timing_summary",
        "get_timing_paths",
        "analyze_timing_closure",
        "check_bitstream_readiness",
    ),
    "diagnostic_bundle_handoff": (
        "run_project_audit",
        "collect_diagnostic_bundle",
        "validate_diagnostic_bundle",
        "stop_session",
        "get_runtime_cache_status",
        "clean_runtime_cache",
    ),
    "project_closeout_cleanup": (
        "stop_session",
        "get_runtime_cache_status",
        "clean_runtime_cache",
    ),
}

_HARDWARE_SAFE_DETECTOR_TOOLS = {
    "detect_hardware_environment",
}

_HARDWARE_LOG_READONLY_TOOLS = {
    "get_hardware_messages",
}

_HARDWARE_DISABLED_BY_DEFAULT_TOOLS = {
    "open_hardware_manager",
    "close_hardware_manager",
    "connect_hw_server",
    "disconnect_hw_server",
    "list_hw_targets",
    "open_hw_target",
    "close_hw_target",
    "list_hw_devices",
    "select_hw_device",
    "get_hw_device_status",
}

_HARDWARE_TOOLS = (
    _HARDWARE_SAFE_DETECTOR_TOOLS
    | _HARDWARE_LOG_READONLY_TOOLS
    | _HARDWARE_DISABLED_BY_DEFAULT_TOOLS
)

_HARDWARE_DESTRUCTIVE_TOOLS = {"program_hw_device", "program_from_artifact_manifest"}
_IMMEDIATE_PROJECT_MUTATION_TOOLS = {
    "add_bd_ip_cell",
    "add_project_files",
    "configure_ip",
    "configure_run",
    "configure_simulation",
    "connect_bd_intf_net",
    "connect_bd_net",
    "create_bd_port",
    "create_block_design",
    "create_ip",
    "create_managed_xdc",
    "create_project",
    "create_signoff_waiver",
    "export_ip_user_files",
    "generate_block_design_wrapper",
    "generate_ip_targets",
    "remove_project_files",
    "remove_signoff_waiver",
    "repair_project_setup",
    "set_project_part",
    "set_project_top",
    "update_project_compile_order",
    "upgrade_ip",
}
_UNATTESTED_COMPOSITE_EXECUTION_TOOLS = {
    "add_bd_ip_cell",
    "configure_ip",
    "create_ip",
    "export_ip_user_files",
    "generate_block_design_wrapper",
    "generate_ip_targets",
    "open_block_design",
    "upgrade_ip",
    "validate_block_design",
}
_EXISTING_PROJECT_EXECUTION_TOOLS = {
    "analyze_timing_closure",
    "check_bitstream_readiness",
    "check_timing_constraints",
    "clean_run_outputs",
    "collect_build_artifacts",
    "collect_diagnostic_bundle",
    "collect_report_bundle",
    "export_project_replay_script",
    "generate_bitstream",
    "get_cdc_report",
    "get_clock_interaction_report",
    "get_clock_summary",
    "get_drc_report",
    "get_methodology_report",
    "get_power_report",
    "get_qor_summary",
    "get_timing_paths",
    "get_timing_summary",
    "get_utilization_report",
    "reset_runs",
    "run_behavioral_simulation",
    "run_elaboration",
    "run_implementation",
    "run_pre_hw_signoff",
    "run_project_audit",
    "run_synthesis",
}
TOOL_PROFILE_ENV = "VIVADO_AGENT_MCP_TOOL_PROFILE"
DEFAULT_TOOL_PROFILE = "core"
_CORE_TOOL_NAMES = frozenset(
    {
        "analyze_sources",
        "analyze_timing_closure",
        "check_bitstream_readiness",
        "check_syntax",
        "check_timing_constraints",
        "close_project",
        "clean_run_outputs",
        "clean_runtime_cache",
        "collect_build_artifacts",
        "collect_diagnostic_bundle",
        "collect_report_bundle",
        "configure_simulation",
        "create_project",
        "detect_vivado_environment",
        "diagnose_run_failure",
        "generate_bitstream",
        "get_agent_scenarios",
        "get_agent_workflows",
        "get_compile_order",
        "get_constraints_summary",
        "get_project_state",
        "get_run_progress",
        "get_runtime_cache_status",
        "get_simulation_result",
        "get_timing_summary",
        "get_timing_paths",
        "get_tool_catalog",
        "get_workflow_trace_status",
        "list_fileset_files",
        "open_project",
        "repair_project_setup",
        "run_behavioral_simulation",
        "run_implementation",
        "run_pre_hw_signoff",
        "run_project_audit",
        "run_synthesis",
        "session_status",
        "start_session",
        "stop_session",
        "update_project_compile_order",
        "validate_diagnostic_bundle",
    }
)


_ADVANCED_ONLY_TOOL_NAMES = {
    "run_tcl",
    "safe_tcl",
    "create_ip",
    "configure_ip",
    "generate_ip_targets",
    "get_ip_status",
    "upgrade_ip",
    "export_ip_user_files",
    "create_block_design",
    "open_block_design",
    "add_bd_ip_cell",
    "create_bd_port",
    "connect_bd_net",
    "connect_bd_intf_net",
    "validate_block_design",
    "generate_block_design_wrapper",
    "get_project_info",
    "add_project_files",
    "remove_project_files",
    "set_project_top",
    "set_project_part",
    "run_elaboration",
    "get_elaboration_result",
    "get_design_hierarchy",
    "get_run_configuration",
    "configure_run",
    "reset_runs",
    "get_artifact_manifest",
    "list_signoff_waivers",
    "create_signoff_waiver",
    "remove_signoff_waiver",
    "export_project_replay_script",
    "get_utilization_report",
    "get_drc_report",
    "get_clock_summary",
    "get_methodology_report",
    "get_qor_summary",
    "get_cdc_report",
    "get_clock_interaction_report",
    "get_power_report",
    "create_managed_xdc",
    "get_messages",
    "get_critical_warnings",
}
_NORMAL_TOOL_NAMES = {
    "get_tool_catalog",
    "get_agent_workflows",
    "get_agent_scenarios",
    "get_workflow_trace_status",
    "detect_vivado_environment",
    "start_session",
    "stop_session",
    "session_status",
    "get_runtime_cache_status",
    "get_simulation_result",
    "get_ip_status",
    "open_block_design",
    "validate_block_design",
    "open_project",
    "close_project",
    "get_project_info",
    "get_project_state",
    "list_fileset_files",
    "check_syntax",
    "get_compile_order",
    "analyze_sources",
    "get_elaboration_result",
    "get_design_hierarchy",
    "get_run_configuration",
    "get_artifact_manifest",
    "list_signoff_waivers",
    "validate_diagnostic_bundle",
    "get_run_progress",
    "diagnose_run_failure",
    "get_constraints_summary",
    "get_messages",
    "get_critical_warnings",
}
_TCL_POLICY_DRY_RUN_TOOLS = {"run_tcl", "safe_tcl"}
_DESTRUCTIVE_DRY_RUN_TOOLS = {"clean_runtime_cache", "reset_runs", "clean_run_outputs"}
_DESTRUCTIVE_TOOL_NAMES = (
    _DESTRUCTIVE_DRY_RUN_TOOLS
    | _HARDWARE_DESTRUCTIVE_TOOLS
    | {"remove_project_files", "remove_signoff_waiver"}
)
_RISK_TOOL_NAMES: dict[str, set[str]] = {
    "normal": _NORMAL_TOOL_NAMES,
    "tcl_policy_dry_run": _TCL_POLICY_DRY_RUN_TOOLS,
    "hardware": _HARDWARE_TOOLS,
    "hardware_destructive": _HARDWARE_DESTRUCTIVE_TOOLS,
    "destructive_dry_run": _DESTRUCTIVE_DRY_RUN_TOOLS,
    "project_mutation_immediate": _IMMEDIATE_PROJECT_MUTATION_TOOLS,
    "project_execution": _EXISTING_PROJECT_EXECUTION_TOOLS - _DESTRUCTIVE_DRY_RUN_TOOLS,
}
_EXPOSURE_TOOL_NAMES: dict[str, set[str] | frozenset[str]] = {
    "core": _CORE_TOOL_NAMES,
    "advanced": _ADVANCED_ONLY_TOOL_NAMES,
    "hardware": _HARDWARE_TOOLS | _HARDWARE_DESTRUCTIVE_TOOLS,
}


CAPABILITY_SPEC_VERSION = 1

_LOCAL_CONTROL_TOOL_NAMES = {
    "get_tool_catalog",
    "get_agent_workflows",
    "get_agent_scenarios",
    "get_workflow_trace_status",
    "session_status",
}
_READ_ONLY_TOOL_NAMES = {
    "get_tool_catalog",
    "get_agent_workflows",
    "get_agent_scenarios",
    "get_workflow_trace_status",
    "session_status",
    "get_runtime_cache_status",
    "detect_hardware_environment",
    "list_hw_targets",
    "list_hw_devices",
    "get_hw_device_status",
    "get_hardware_messages",
    "run_tcl",
    "safe_tcl",
    "get_simulation_result",
    "get_ip_status",
    "get_project_info",
    "get_project_state",
    "list_fileset_files",
    "check_syntax",
    "get_compile_order",
    "analyze_sources",
    "get_elaboration_result",
    "get_design_hierarchy",
    "get_run_configuration",
    "get_run_progress",
    "diagnose_run_failure",
    "get_artifact_manifest",
    "list_signoff_waivers",
    "validate_diagnostic_bundle",
    "get_constraints_summary",
    "get_messages",
    "get_critical_warnings",
}
_READ_ONLY_HARDWARE_TOOLS = (
    _HARDWARE_SAFE_DETECTOR_TOOLS
    | _HARDWARE_LOG_READONLY_TOOLS
    | {
        "list_hw_targets",
        "list_hw_devices",
        "get_hw_device_status",
    }
)
_LONG_DURATION_TOOLS = {
    "run_behavioral_simulation",
    "run_elaboration",
    "run_synthesis",
    "run_implementation",
    "generate_bitstream",
    "collect_report_bundle",
    "run_pre_hw_signoff",
    "run_project_audit",
    "collect_diagnostic_bundle",
}
_MEDIUM_DURATION_DOMAINS = {
    "session",
    "project",
    "simulation",
    "ip",
    "block_design",
    "runs",
    "constraints",
    "reports",
    "diagnostics",
    "hardware_boundary",
}
_SESSION_FREE_TOOLS = {
    "get_tool_catalog",
    "get_agent_workflows",
    "get_agent_scenarios",
    "get_workflow_trace_status",
    "detect_vivado_environment",
    "detect_hardware_environment",
    "start_session",
    "stop_session",
    "session_status",
    "get_runtime_cache_status",
    "clean_runtime_cache",
    "get_artifact_manifest",
    "validate_diagnostic_bundle",
    "run_tcl",
    "safe_tcl",
}
_VIVADO_INDEPENDENT_TOOLS = (
    set(CAPABILITY_DOMAIN_TOOL_NAMES["agent_guidance"])
    | set(CAPABILITY_DOMAIN_TOOL_NAMES["runtime_lightweight"])
    | {"get_artifact_manifest", "validate_diagnostic_bundle"}
)
_PROJECT_FREE_TOOLS = {
    name
    for domain in ("agent_guidance", "session", "runtime_lightweight", "hardware_boundary", "custom_tcl")
    for name in CAPABILITY_DOMAIN_TOOL_NAMES[domain]
} | {"create_project", "open_project"}
_PROJECT_OR_MANIFEST_TOOLS = {
    "get_artifact_manifest",
    "validate_diagnostic_bundle",
}
_ARTIFACT_CONTRACTS: dict[str, tuple[str, ...]] = {
    "generate_bitstream": ("bitstream",),
    "collect_build_artifacts": ("artifact_manifest", "artifact_sha256"),
    "get_artifact_manifest": ("artifact_manifest",),
    "collect_report_bundle": ("report_manifest", "report_sha256"),
    "collect_diagnostic_bundle": ("diagnostic_manifest", "evidence_sha256"),
    "validate_diagnostic_bundle": ("diagnostic_manifest", "evidence_sha256"),
    "export_project_replay_script": ("replay_tcl",),
}


def _domain_for_tool(name: str) -> str:
    matches = [domain for domain, names in CAPABILITY_DOMAIN_TOOL_NAMES.items() if name in names]
    if len(matches) != 1:
        raise ValueError(f"Capability {name!r} must belong to exactly one domain; found {matches!r}")
    return matches[0]


def _workflow_tags_for_tool(name: str) -> tuple[str, ...]:
    return tuple(workflow_id for workflow_id, names in CAPABILITY_WORKFLOW_SEQUENCES.items() if name in names)


def _risk_for_tool(name: str) -> str:
    matches = [risk for risk, names in _RISK_TOOL_NAMES.items() if name in names]
    if len(matches) != 1:
        raise ValueError(f"Capability {name!r} must have exactly one explicit risk class; found {matches!r}")
    return matches[0]


def _hardware_tier_for_tool(name: str) -> str:
    if name in _HARDWARE_SAFE_DETECTOR_TOOLS:
        return "hardware_safe_detector"
    if name in _HARDWARE_LOG_READONLY_TOOLS:
        return "hardware_log_readonly"
    if name in _HARDWARE_DISABLED_BY_DEFAULT_TOOLS:
        return "hardware_disabled_by_default"
    if name in _HARDWARE_DESTRUCTIVE_TOOLS:
        return "hardware_destructive"
    if name in _CORE_TOOL_NAMES or name in _ADVANCED_ONLY_TOOL_NAMES:
        return "not_hardware"
    raise ValueError(f"Capability {name!r} has no explicit hardware classification")


def _profiles_for_tool(name: str, *, hardware: bool) -> frozenset[str]:
    matches = [exposure for exposure, names in _EXPOSURE_TOOL_NAMES.items() if name in names]
    if len(matches) != 1:
        raise ValueError(f"Capability {name!r} must have exactly one explicit exposure class; found {matches!r}")
    exposure = matches[0]
    if hardware != (exposure == "hardware"):
        raise ValueError(f"Capability {name!r} hardware and exposure metadata disagree")
    if exposure == "core":
        return frozenset({"core", "advanced", "all"})
    if exposure == "advanced":
        return frozenset({"advanced", "all"})
    return frozenset({"all"})


def _required_session_state(name: str) -> str:
    if name == "clean_runtime_cache":
        return "session_stopped_for_mutation"
    return "none" if name in _SESSION_FREE_TOOLS else "managed_session"


def _required_project_state(name: str, risk: str) -> str:
    if name in _PROJECT_OR_MANIFEST_TOOLS:
        return "project_or_manifest"
    if name in _PROJECT_FREE_TOOLS:
        return "none"
    if risk in {"project_mutation_immediate", "project_execution", "destructive_dry_run"}:
        return "managed_project_open"
    return "project_open"


def _build_capability_spec(tool: types.Tool) -> CapabilitySpec:
    name = tool.name
    domain = _domain_for_tool(name)
    risk = _risk_for_tool(name)
    hardware_tier = _hardware_tier_for_tool(name)
    hardware = hardware_tier != "not_hardware"
    read_only = name in _READ_ONLY_TOOL_NAMES or name in _READ_ONLY_HARDWARE_TOOLS
    destructive = name in _DESTRUCTIVE_TOOL_NAMES
    required_session_state = _required_session_state(name)
    required_project_state = _required_project_state(name, risk)
    if name in _LONG_DURATION_TOOLS:
        duration_class = "long"
    elif domain in _MEDIUM_DURATION_DOMAINS:
        duration_class = "medium"
    else:
        duration_class = "short"
    evidence_contract = [f"unified_result_v{RESPONSE_SCHEMA_VERSION}"]
    if required_session_state == "managed_session":
        evidence_contract.append("managed_session_generation")
    if required_project_state in {"project_open", "managed_project_open"}:
        evidence_contract.append("project_identity")
    if hardware:
        evidence_contract.append("hardware_not_validated")
    return CapabilitySpec(
        name=name,
        domain=domain,
        handler=f"_{name}",
        description=tool.description or "",
        input_schema=dict(tool.inputSchema),
        output_schema=dict(tool.outputSchema or _output_schema()),
        risk_class=risk,
        profiles=_profiles_for_tool(name, hardware=hardware),
        workflow_tags=_workflow_tags_for_tool(name),
        required_session_state=required_session_state,
        required_project_state=required_project_state,
        read_only=read_only,
        mutation=not read_only,
        destructive=destructive,
        hardware=hardware,
        idempotent=read_only,
        duration_class=duration_class,
        supported_vivado_versions=() if name in _VIVADO_INDEPENDENT_TOOLS else (SUPPORTED_VIVADO_VERSION,),
        qualified_vivado_versions=(),
        task_eligible=duration_class == "long" and not hardware,
        open_world=domain != "agent_guidance",
        dispatch_lane="local" if name in _LOCAL_CONTROL_TOOL_NAMES else "serialized_backend",
        execution_input_policy=(
            "blocks_unattested_composite"
            if name in _UNATTESTED_COMPOSITE_EXECUTION_TOOLS
            else "typed_tool_policy"
        ),
        evidence_contract=tuple(evidence_contract),
        artifact_contract=_ARTIFACT_CONTRACTS.get(name, ("none",)),
        hardware_tier=hardware_tier,
    )


def _validate_capability_metadata() -> None:
    seed_names = [tool.name for tool in _CAPABILITY_SCHEMA_SEEDS]
    if len(seed_names) != len(set(seed_names)):
        raise ValueError("Capability schema seed names must be unique")

    domain_names = [name for names in CAPABILITY_DOMAIN_TOOL_NAMES.values() for name in names]
    if len(domain_names) != len(set(domain_names)):
        raise ValueError("Each capability must belong to exactly one domain")
    if set(domain_names) != set(seed_names):
        missing = sorted(set(seed_names) - set(domain_names))
        unknown = sorted(set(domain_names) - set(seed_names))
        raise ValueError(f"Capability domain metadata drifted; missing={missing!r}, unknown={unknown!r}")

    risk_names = [name for names in _RISK_TOOL_NAMES.values() for name in names]
    if len(risk_names) != len(set(risk_names)) or set(risk_names) != set(seed_names):
        raise ValueError("Every capability must have exactly one explicit risk class")

    exposure_names = [name for names in _EXPOSURE_TOOL_NAMES.values() for name in names]
    if len(exposure_names) != len(set(exposure_names)) or set(exposure_names) != set(seed_names):
        raise ValueError("Every capability must have exactly one explicit exposure class")

    if not _DESTRUCTIVE_TOOL_NAMES <= set(seed_names):
        raise ValueError("Destructive capability metadata references an unknown tool")
    destructive_prefixes = (
        "clean_",
        "delete_",
        "erase_",
        "overwrite_",
        "program_",
        "remove_",
        "reset_",
    )
    missing_destructive = sorted(
        name
        for name in seed_names
        if name.startswith(destructive_prefixes) and name not in _DESTRUCTIVE_TOOL_NAMES
    )
    if missing_destructive:
        raise ValueError(f"Destructive capability classification is missing: {missing_destructive!r}")

    referenced_names = (
        set(_CORE_TOOL_NAMES)
        | set(_HARDWARE_TOOLS)
        | set(_HARDWARE_DESTRUCTIVE_TOOLS)
        | set(_IMMEDIATE_PROJECT_MUTATION_TOOLS)
        | set(_UNATTESTED_COMPOSITE_EXECUTION_TOOLS)
        | set(_EXISTING_PROJECT_EXECUTION_TOOLS)
        | {
            name
            for sequence in CAPABILITY_WORKFLOW_SEQUENCES.values()
            for name in sequence
        }
    )
    unknown_references = sorted(referenced_names - set(seed_names))
    if unknown_references:
        raise ValueError(f"Capability metadata references unknown tools: {unknown_references!r}")


_validate_capability_metadata()
CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = tuple(
    _build_capability_spec(tool) for tool in _CAPABILITY_SCHEMA_SEEDS
)
TOOL_REGISTRY: dict[str, CapabilitySpec] = {spec.name: spec for spec in CAPABILITY_SPECS}
if len(TOOL_REGISTRY) != len(CAPABILITY_SPECS):
    raise ValueError("Capability names must be unique")

TOOL_DEFS = [spec.to_mcp_tool() for spec in CAPABILITY_SPECS]

HARDWARE_SAFE_DETECTOR_TOOLS = {
    spec.name for spec in CAPABILITY_SPECS if spec.hardware_tier == "hardware_safe_detector"
}
HARDWARE_LOG_READONLY_TOOLS = {
    spec.name for spec in CAPABILITY_SPECS if spec.hardware_tier == "hardware_log_readonly"
}
HARDWARE_DISABLED_BY_DEFAULT_TOOLS = {
    spec.name for spec in CAPABILITY_SPECS if spec.hardware_tier == "hardware_disabled_by_default"
}
HARDWARE_DESTRUCTIVE_TOOLS = {
    spec.name for spec in CAPABILITY_SPECS if spec.hardware_tier == "hardware_destructive"
}
HARDWARE_TOOLS = HARDWARE_SAFE_DETECTOR_TOOLS | HARDWARE_LOG_READONLY_TOOLS | HARDWARE_DISABLED_BY_DEFAULT_TOOLS
IMMEDIATE_PROJECT_MUTATION_TOOLS = {
    spec.name for spec in CAPABILITY_SPECS if spec.risk == "project_mutation_immediate"
}
EXISTING_PROJECT_EXECUTION_TOOLS = {
    spec.name
    for spec in CAPABILITY_SPECS
    if spec.risk in {"project_execution", "destructive_dry_run"}
    and spec.required_project_state == "managed_project_open"
}
UNATTESTED_COMPOSITE_EXECUTION_TOOLS = {
    spec.name for spec in CAPABILITY_SPECS if spec.execution_input_policy == "blocks_unattested_composite"
}
CORE_TOOL_NAMES = frozenset(spec.name for spec in CAPABILITY_SPECS if "core" in spec.profiles)


def hardware_tool_tiers() -> dict[str, list[str]]:
    return {
        "hardware_safe_detector": sorted(HARDWARE_SAFE_DETECTOR_TOOLS),
        "hardware_log_readonly": sorted(HARDWARE_LOG_READONLY_TOOLS),
        "hardware_disabled_by_default": sorted(HARDWARE_DISABLED_BY_DEFAULT_TOOLS),
        "hardware_destructive": sorted(HARDWARE_DESTRUCTIVE_TOOLS),
    }


def capability_domain_tool_names(domain: str) -> tuple[str, ...]:
    return CAPABILITY_DOMAIN_TOOL_NAMES[domain]


def capability_workflow_sequence(workflow_id: str) -> tuple[str, ...]:
    return CAPABILITY_WORKFLOW_SEQUENCES[workflow_id]


def local_control_tool_names() -> frozenset[str]:
    return frozenset(spec.name for spec in CAPABILITY_SPECS if spec.dispatch_lane == "local")


def capability_manifest(names: list[str] | set[str] | None = None) -> dict[str, Any]:
    selected = set(names) if names is not None else set(TOOL_REGISTRY)
    unknown = sorted(selected - set(TOOL_REGISTRY))
    if unknown:
        raise ValueError(f"Unknown capability name(s): {unknown!r}")
    capabilities = [
        spec.to_manifest_record()
        for spec in CAPABILITY_SPECS
        if spec.name in selected
    ]
    payload = {
        "schema_version": CAPABILITY_SPEC_VERSION,
        "capability_count": len(capabilities),
        "capabilities": capabilities,
        "hardware_validation": {
            "status": "NOT_VALIDATED",
            "validated": False,
        },
    }
    payload["capability_digest"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def tool_names() -> list[str]:
    return sorted(TOOL_REGISTRY)


def resolve_tool_profile(profile: str | None = None) -> str:
    selected = str(profile if profile is not None else os.environ.get(TOOL_PROFILE_ENV, DEFAULT_TOOL_PROFILE)).strip().lower()
    return selected if selected in {"core", "advanced", "all"} else DEFAULT_TOOL_PROFILE


def profile_tool_names(profile: str | None = None) -> list[str]:
    selected = resolve_tool_profile(profile)
    return sorted(spec.name for spec in CAPABILITY_SPECS if selected in spec.profiles)


def tool_definitions(names: list[str] | set[str] | None = None) -> list[types.Tool]:
    selected = set(names) if names is not None else set(profile_tool_names())
    return [tool for tool in TOOL_DEFS if tool.name in selected]


def handler_name(tool_name: str) -> str:
    return TOOL_REGISTRY[tool_name].handler


def input_schema_properties(tool_name: str) -> set[str]:
    schema = TOOL_REGISTRY[tool_name].input_schema
    return set(schema.get("properties", {}))


def validate_tool_arguments(tool_name: str, arguments: Any) -> list[str]:
    if tool_name not in TOOL_REGISTRY:
        return []
    return _validate_schema_value(arguments, TOOL_REGISTRY[tool_name].input_schema, path="arguments")


def _validate_schema_value(value: Any, schema: dict[str, Any], *, path: str) -> list[str]:
    issues: list[str] = []
    expected = schema.get("type")
    allowed_types = expected if isinstance(expected, list) else [expected] if isinstance(expected, str) else []
    if allowed_types and not any(_matches_json_type(value, item) for item in allowed_types):
        return [f"{path} must have JSON type {' or '.join(str(item) for item in allowed_types)}"]
    if "enum" in schema and value not in schema["enum"]:
        issues.append(f"{path} must be one of {schema['enum']!r}")
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        property_names = schema.get("propertyNames")
        additional_properties = schema.get("additionalProperties")
        non_string_keys = [key for key in value if not isinstance(key, str)]
        if non_string_keys:
            issues.append(f"{path} contains non-string object key(s)")
        if isinstance(schema.get("minProperties"), int) and len(value) < schema["minProperties"]:
            issues.append(f"{path} must contain at least {schema['minProperties']} propertie(s)")
        if isinstance(schema.get("maxProperties"), int) and len(value) > schema["maxProperties"]:
            issues.append(f"{path} must contain at most {schema['maxProperties']} propertie(s)")
        for name in required:
            if name not in value:
                issues.append(f"{path}.{name} is required")
        if isinstance(property_names, dict):
            for name in (name for name in value if isinstance(name, str)):
                issues.extend(
                    _validate_schema_value(
                        name,
                        property_names,
                        path=f"{path}.{name} property name",
                    )
                )
        if additional_properties is False:
            for name in sorted((name for name in value if isinstance(name, str) and name not in properties)):
                issues.append(f"{path}.{name} is not allowed")
        for name, item in value.items():
            child_schema = properties.get(name)
            if not isinstance(child_schema, dict) and isinstance(additional_properties, dict):
                child_schema = additional_properties
            if isinstance(child_schema, dict):
                issues.extend(_validate_schema_value(item, child_schema, path=f"{path}.{name}"))
    elif isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            issues.append(f"{path} must contain at least {schema['minItems']} item(s)")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            issues.append(f"{path} must contain at most {schema['maxItems']} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                issues.extend(_validate_schema_value(item, item_schema, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            issues.append(f"{path} is shorter than {schema['minLength']} character(s)")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            issues.append(f"{path} is longer than {schema['maxLength']} character(s)")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            issues.append(f"{path} must be a finite JSON number")
            return issues
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            issues.append(f"{path} must be >= {schema['minimum']}")
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            issues.append(f"{path} must be <= {schema['maximum']}")
    return issues


def _matches_json_type(value: Any, expected: str) -> bool:
    return {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(expected, False)
