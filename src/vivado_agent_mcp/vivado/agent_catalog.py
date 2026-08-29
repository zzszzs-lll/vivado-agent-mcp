from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..registry import TOOL_REGISTRY, hardware_tool_tiers
from .audit import REQUIRED_DIAGNOSTIC_CATEGORIES, SINGLETON_DIAGNOSTIC_CATEGORIES
from .agent_actions import dedupe_next_actions, next_action
from .evidence_attestation import verify_diagnostic_manifest_attestation
from .evidence_store import EvidenceSnapshot, load_evidence_snapshot, load_json_evidence
from .hardware_boundary import hardware_validation_boundary
from .managed_path import ManagedPathError, validate_managed_path
from .workflow_trace import validate_workflow_trace_file


MAX_DIAGNOSTIC_FILES = 256
MAX_DIAGNOSTIC_FILE_BYTES = 100 * 1024 * 1024
MAX_DIAGNOSTIC_TOTAL_BYTES = 512 * 1024 * 1024
MAX_DIAGNOSTIC_MANIFEST_BYTES = 8 * 1024 * 1024


TOOL_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "id": "agent_guidance",
        "label": "Agent guidance entrypoints",
        "purpose": "Expose the tool catalog, standard recipes, scenario benchmarks, and handoff validation entrypoints.",
        "tools": ["get_tool_catalog", "get_agent_workflows", "get_agent_scenarios", "get_workflow_trace_status"],
    },
    {
        "id": "session",
        "label": "Vivado session",
        "purpose": "Start, stop, and inspect the visible Vivado GUI/TCP Tcl session.",
        "tools": ["detect_vivado_environment", "start_session", "session_status", "stop_session"],
    },
    {
        "id": "runtime_lightweight",
        "label": "Runtime lightweight cleanup",
        "purpose": "Inspect and explicitly clean temporary MCP/Vivado runtime cache without touching project-local vmcp_* deliverables.",
        "tools": ["get_runtime_cache_status", "clean_runtime_cache"],
    },
    {
        "id": "project",
        "label": "Project and filesets",
        "purpose": "Create/open projects and manage sources_1, constrs_1, and sim_1 references.",
        "tools": [
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
        ],
    },
    {
        "id": "simulation",
        "label": "Behavioral simulation",
        "purpose": "Configure sim_1, run Vivado XSIM, and parse logs plus waveform artifacts.",
        "tools": ["configure_simulation", "run_behavioral_simulation", "get_simulation_result"],
    },
    {
        "id": "ip",
        "label": "Vivado IP",
        "purpose": "Retain IP maintenance primitives for future compatibility work; managed simulation, run, report, signoff, audit, and diagnostic execution reject IP/XCI design inputs.",
        "execution_status": "BLOCKED_UNSUPPORTED_COMPOSITE_INPUT",
        "execution_boundary": "Do not route an Agent into managed design execution when IP/XCI inputs are present.",
        "tools": ["create_ip", "configure_ip", "generate_ip_targets", "export_ip_user_files", "get_ip_status", "upgrade_ip"],
    },
    {
        "id": "block_design",
        "label": "Block Design",
        "purpose": "Retain Block Design maintenance primitives for future compatibility work; managed simulation, run, report, signoff, audit, and diagnostic execution reject BD design inputs.",
        "execution_status": "BLOCKED_UNSUPPORTED_COMPOSITE_INPUT",
        "execution_boundary": "Do not route an Agent into managed design execution when Block Design inputs are present.",
        "tools": [
            "create_block_design",
            "open_block_design",
            "add_bd_ip_cell",
            "create_bd_port",
            "connect_bd_net",
            "connect_bd_intf_net",
            "validate_block_design",
            "generate_block_design_wrapper",
        ],
    },
    {
        "id": "runs",
        "label": "Synthesis, implementation, and bitstream",
        "purpose": "Launch runs without blocking, poll progress, configure/reset runs, and collect build outputs.",
        "tools": [
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
        ],
    },
    {
        "id": "constraints",
        "label": "Constraints and timing readiness",
        "purpose": "Inspect and diagnose XDC, timing constraints, clocks, and closure blockers.",
        "tools": [
            "create_managed_xdc",
            "get_constraints_summary",
            "check_timing_constraints",
            "get_clock_summary",
            "get_timing_paths",
            "analyze_timing_closure",
            "check_bitstream_readiness",
        ],
    },
    {
        "id": "reports",
        "label": "Reports",
        "purpose": "Parse implementation, timing, quality, CDC, clock interaction, power, and message reports.",
        "tools": [
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
        ],
    },
    {
        "id": "diagnostics",
        "label": "Signoff, audit, diagnostics, and replay",
        "purpose": "Create handoff-grade project health evidence without claiming real-board validation.",
        "tools": [
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
        ],
    },
    {
        "id": "hardware_boundary",
        "label": "Hardware boundary",
        "purpose": "Expose explicit hardware interfaces, path checks, and no-board negative results only.",
        "tools": [
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
        ],
    },
    {
        "id": "custom_tcl",
        "label": "Tcl policy dry-run",
        "purpose": "Render and classify raw or template Tcl without executing it; uncovered operations require a dedicated typed tool.",
        "tools": ["run_tcl", "safe_tcl"],
    },
)


WORKFLOWS: tuple[dict[str, Any], ...] = (
    {
        "id": "new_project_to_bitstream",
        "label": "New Project Mode project to bitstream",
        "required_inputs": ["project_name", "project_dir", "part", "rtl_files", "top", "clock_and_pin_constraints", "testbench_or_sim_plan", "authorized_source_editing_capability"],
        "tool_sequence": [
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
        ],
        "completion_gate": "Agent handoff is complete only after bitstream artifacts, report bundle, pre-hardware signoff, project audit, diagnostic bundle, and validate_diagnostic_bundle are all available.",
        "stop_conditions": ["run_pre_hw_signoff returns BLOCK", "run_project_audit returns BLOCK", "timing/DRC/methodology readiness blocks bitstream handoff"],
    },
    {
        "id": "existing_project_audit",
        "label": "Existing project inspection and evidence handoff",
        "required_inputs": ["project_path", "diagnostic_manifest_path"],
        "tool_sequence": [
            "start_session",
            "open_project",
            "get_project_state",
            "list_fileset_files",
            "validate_diagnostic_bundle",
            "get_workflow_trace_status",
            "stop_session",
        ],
        "completion_gate": "Existing project inspection is complete only after the receiver records project/fileset state and validates producer evidence without executing or mutating the original project.",
        "stop_conditions": [
            "diagnostic bundle health is BLOCK",
            "any design execution or mutation is requested against the original project",
            "further audit requires a separately reconstructed MCP-created working project",
        ],
    },
    {
        "id": "simulation_failure_repair",
        "label": "Simulation failure diagnosis loop",
        "required_inputs": ["open_project", "simset", "testbench_top"],
        "tool_sequence": ["get_simulation_result", "analyze_sources", "check_syntax", "get_compile_order", "run_behavioral_simulation"],
        "completion_gate": "Simulation repair is complete when the latest bounded run_behavioral_simulation invocation reports completed.",
        "stop_conditions": ["self-checking testbench still reports FAIL", "XSIM log still contains ERROR"],
    },
    {
        "id": "timing_failure_repair",
        "label": "Timing and constraint repair loop",
        "required_inputs": ["open_project", "clock_requirements", "pin_constraints", "run_name"],
        "tool_sequence": ["diagnose_run_failure", "get_constraints_summary", "check_timing_constraints", "get_timing_summary", "get_timing_paths", "analyze_timing_closure", "check_bitstream_readiness"],
        "completion_gate": "Timing repair is complete when check_bitstream_readiness is READY or only documented warnings remain.",
        "stop_conditions": ["WNS/TNS remains failing", "check_timing reports unconstrained clocks/endpoints", "DRC or methodology errors remain"],
    },
    {
        "id": "diagnostic_bundle_handoff",
        "label": "Diagnostic bundle handoff",
        "required_inputs": ["open_project", "run_name"],
        "tool_sequence": [
            "run_project_audit",
            "collect_diagnostic_bundle",
            "validate_diagnostic_bundle",
            "stop_session",
            "get_runtime_cache_status",
            "clean_runtime_cache",
        ],
        "completion_gate": "Project-local diagnostic review handoff is complete when validate_diagnostic_bundle returns READY or an explicitly reviewed WARN; portable cross-machine handoff is not supported.",
        "stop_conditions": ["validate_diagnostic_bundle returns BLOCK", "hardware_validation is missing or not NOT_VALIDATED"],
    },
    {
        "id": "project_closeout_cleanup",
        "label": "Project closeout runtime cleanup",
        "required_inputs": [],
        "tool_sequence": ["stop_session", "get_runtime_cache_status", "clean_runtime_cache"],
        "completion_gate": "Cleanup is complete after stop_session and a reviewed clean_runtime_cache dry-run, with real deletion only on explicit confirmation.",
        "stop_conditions": [
            "clean_runtime_cache dry-run returns BLOCK",
            "Vivado/XSIM/hw_server process is still active",
            "runtime_dir resolves to a project directory instead of the MCP runtime directory",
        ],
    },
)


SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "id": "S00",
        "label": "Agent-only stdio baseline",
        "purpose": "Validate tool discovery, schema, workflow recipes, safety gates, and fake timeout recovery without launching Vivado.",
        "recommended_frequency": "After MCP schema, registry, server, or response contract changes.",
        "required_tools": ["get_tool_catalog", "get_agent_workflows", "get_agent_scenarios", "get_workflow_trace_status"],
        "workflow_id": "",
        "project_shape": "No Vivado project; use tests/agent_stdio_regression.py as a black-box stdio client.",
        "acceptance": [
            "agent_stdio_regression result ok=true.",
            "Tool discovery, catalog, workflow, scenario, Tcl policy, hardware gate, and fake timeout checks pass.",
            "No real Vivado session or hardware path is required.",
        ],
        "stop_conditions": ["stdio server cannot list tools", "structuredContent is missing", "safety gate negative paths are not blocked"],
    },
    {
        "id": "S01",
        "label": "Minimal counter Project Mode closure",
        "purpose": "Validate a minimal RTL/XDC/testbench flow through bitstream and diagnostic handoff.",
        "recommended_frequency": "Every version candidate.",
        "required_tools": [
            "get_tool_catalog",
            "get_agent_workflows",
            "start_session",
            "create_project",
            "run_behavioral_simulation",
            "run_synthesis",
            "get_run_progress",
            "run_implementation",
            "generate_bitstream",
            "collect_build_artifacts",
            "collect_report_bundle",
            "run_pre_hw_signoff",
            "run_project_audit",
            "collect_diagnostic_bundle",
            "validate_diagnostic_bundle",
            "stop_session",
        ],
        "workflow_id": "new_project_to_bitstream",
        "project_shape": "Single counter top, one XDC with clock/reset/output placeholders, and a finite self-checking testbench.",
        "acceptance": [
            "Bitstream file exists.",
            "Artifact and report manifests exist inside the project directory.",
            "Diagnostic bundle validates as READY or explicit reviewable WARN.",
            "hardware_validation.status remains NOT_VALIDATED.",
        ],
        "stop_conditions": ["simulation fails without actionable diagnosis", "bitstream is missing after terminal run", "diagnostic bundle returns BLOCK"],
    },
    {
        "id": "S02",
        "label": "Multi-file SystemVerilog PWM",
        "purpose": "Validate SystemVerilog file-type policy, compile order, sim top, bounded simulation, and GUI responsiveness checkpoints.",
        "recommended_frequency": "Every version candidate.",
        "required_tools": ["get_tool_catalog", "get_agent_workflows", "start_session", "create_project", "repair_project_setup", "get_compile_order", "list_fileset_files", "run_behavioral_simulation", "generate_bitstream", "validate_diagnostic_bundle"],
        "workflow_id": "new_project_to_bitstream",
        "project_shape": "At least three .sv RTL files, one XDC, and a finite self-checking testbench.",
        "example_project": {
            "rtl_files": ["clock_enable.sv", "pwm_core.sv", "pwm_breathing_top.sv"],
            "xdc_files": ["pwm_breathing.xdc"],
            "sim_files": ["tb_pwm_breathing_top.sv"],
            "top": "pwm_breathing_top",
            "testbench_top": "tb_pwm_breathing_top",
            "simulation": {"run_time": "200 us", "run_all": False, "max_vcd_mb": 64},
            "expected_behavior": "Self-checking testbench observes PWM ramp up, peak, and ramp down before $finish.",
        },
        "acceptance": [
            "Vivado project target_language compatibility note is understood.",
            ".sv files are represented as SystemVerilog file_type.",
            "repair_project_setup post-repair status does not retain resolved needs_* flags.",
            "Vivado GUI is responsive after MCP tools return when process status can be observed.",
        ],
        "stop_conditions": ["SV files are treated as plain Verilog incorrectly", "compile order cannot be explained", "GUI remains unresponsive after tool return"],
    },
    {
        "id": "S03",
        "label": "Simulation failure repair",
        "purpose": "Validate simulation_diagnosis, invocation log span, VCD guard semantics, and fix-after-failure routing.",
        "recommended_frequency": "After simulation or VCD changes.",
        "required_tools": ["run_behavioral_simulation", "get_simulation_result", "analyze_sources", "check_syntax", "get_compile_order"],
        "workflow_id": "simulation_failure_repair",
        "project_shape": "A small project with an intentional DUT or testbench failure followed by a fix and rerun.",
        "acceptance": [
            "Initial failure includes simulation_diagnosis.primary_cause.",
            "Rerun PASS is based on the current invocation log span.",
            "VCD run_all risk is blocked or explicitly bounded.",
            "testbench_existing VCD mode is informational when no duplicate MCP open_vcd occurs.",
        ],
        "stop_conditions": ["old logs contaminate PASS/FAIL", "VCD exceeds configured guard", "next_actions cannot route a rerun"],
    },
    {
        "id": "S04",
        "label": "Partial project setup recovery",
        "purpose": "Validate fail-closed create timeout recovery and capability-bound add/configure setup repair.",
        "recommended_frequency": "After project, fileset, session, or timeout changes.",
        "required_tools": ["session_status", "open_project", "create_project", "list_fileset_files", "repair_project_setup", "get_workflow_trace_status"],
        "workflow_id": "new_project_to_bitstream",
        "project_shape": "Fake timeout through stdio regression or a reviewed partial .xpr project; avoid forcing real long hangs.",
        "acceptance": [
            "TimeoutError includes timeout_s_used, command excerpt, request_context, session_status, runtime/log tails, and next_actions.",
            "create_project partial_success=true never grants trust from pathname existence and returns project_capability_bound=false.",
            "The timed-out partial .xpr is inspection-only; reviewed inputs are rebuilt in a distinct MCP-created working project.",
            "repair_project_setup remains available for later setup timeouts only while a successfully established project capability remains valid.",
        ],
        "stop_conditions": ["TimeoutError returns empty data", "an external or unremembered .xpr is mutated in place", "repair status remains contradictory after execution"],
    },
    {
        "id": "S05",
        "label": "Reviewable WARN diagnostic handoff",
        "purpose": "Validate warning handoff semantics without claiming hardware validation.",
        "recommended_frequency": "After signoff, audit, diagnostic, or evidence freshness changes.",
        "required_tools": ["run_pre_hw_signoff", "run_project_audit", "collect_diagnostic_bundle", "validate_diagnostic_bundle"],
        "workflow_id": "diagnostic_bundle_handoff",
        "project_shape": "A bitstream-ready project with non-blocking warnings such as CFGBVS/CONFIG_VOLTAGE or IO delay review items.",
        "acceptance": [
            "Signoff and audit expose active warnings.",
            "validate_diagnostic_bundle distinguishes handoff_ready from handoff_reviewable.",
            "Subagent reports WARN as review-required handoff, not READY.",
            "hardware_validation.status remains NOT_VALIDATED.",
        ],
        "stop_conditions": ["WARN is silently washed into READY", "hardware validation boundary is missing", "freshness is stale but handoff_ready=true"],
    },
    {
        "id": "S06",
        "label": "Safety gate negative paths",
        "purpose": "Validate disabled public Tcl execution plus programming, deletion/reset, and runtime cleanup gates.",
        "recommended_frequency": "After safety, hardware, run cleanup, or runtime cache changes.",
        "required_tools": ["run_tcl", "safe_tcl", "list_hw_targets", "program_hw_device", "program_from_artifact_manifest", "reset_runs", "clean_run_outputs", "get_runtime_cache_status", "clean_runtime_cache"],
        "workflow_id": "project_closeout_cleanup",
        "project_shape": "No real hardware; use dry-run and negative calls only.",
        "acceptance": [
            "Public Tcl never reaches the managed session; dangerous dry-run classification without intent/allow flags is blocked.",
            "Hardware programming without explicit intent, confirm, fingerprint, and hashes is blocked.",
            "Reset and cleanup tools default to dry-run.",
            "Runtime real cleanup is blocked while sessions or Vivado/XSIM processes are active.",
        ],
        "stop_conditions": ["any destructive or hardware operation executes without gates", "cleanup targets project vmcp_* deliverables", "failure lacks actionable next_actions"],
    },
    {
        "id": "S07",
        "label": "Existing project audit and replay handoff",
        "purpose": "Validate inspection-only handoff when another Agent opens an existing project and verifies producer evidence without executing it.",
        "recommended_frequency": "After audit, workflow trace, replay, or diagnostic bundle changes.",
        "required_tools": ["start_session", "open_project", "get_project_state", "list_fileset_files", "validate_diagnostic_bundle", "get_workflow_trace_status", "stop_session"],
        "workflow_id": "existing_project_audit",
        "project_shape": "Reuse an S01 or S02 project plus its diagnostic manifest as an inspection-only Project Mode handoff target.",
        "acceptance": [
            "Existing project opens without recreate.",
            "Receiver records project and fileset state but does not execute simulation, runs, reports, audit collection, or mutation against the original project.",
            "Producer diagnostic evidence validates as READY or explicitly reviewable WARN.",
            "Diagnostic bundle exposes identity-bound primary-file references, workflow_trace_ref, and replay_project.tcl evidence.",
            "append-only workflow trace growth does not become BLOCK.",
            "get_workflow_trace_status identifies last success, historical failure, and unresolved failure correctly.",
        ],
        "stop_conditions": [
            "original project would be mutated or executed",
            "trace cannot support handoff",
            "bundle validation blocks append-only trace growth",
            "deeper audit is requested before a distinct MCP-created working project exists",
        ],
    },
)

DEFAULT_NO_LIVE_SCENARIOS = ("S00", "S03", "S04", "S05", "S06", "S07")
DEFAULT_LIVE_SCENARIOS = ("S01", "S02", "S03", "S07")


SCENARIO_RUNNER_COVERAGE: dict[str, dict[str, Any]] = {
    "S00": {
        "execution_mode": "nested_mcp_stdio_regression",
        "evidence_class": "stdio_integration",
        "full_scenario_coverage": True,
    },
    "S01": {
        "execution_mode": "mcp_stdio_live_project",
        "evidence_class": "live_project_software_flow",
        "full_scenario_coverage": True,
        "requires_flag": "--include-live-vivado",
    },
    "S02": {
        "execution_mode": "mcp_stdio_live_project",
        "evidence_class": "live_project_software_flow",
        "full_scenario_coverage": True,
        "requires_flag": "--include-live-vivado",
    },
    "S03": {
        "execution_mode": "mcp_stdio_fake_session",
        "evidence_class": "stdio_fake_session_contract",
        "full_scenario_coverage": False,
        "limitation": "No-live fake Vivado session; validates MCP stdio serialization and simulation repair contract, not real XSIM execution.",
        "live_execution_mode": "mcp_stdio_live_xsim_repair",
        "live_evidence_class": "live_xsim_repair_flow",
        "live_requires_flag": "--include-live-vivado",
        "live_note": "With --include-live-vivado, S03 runs a small real XSIM project that fails once, patches the DUT source, and reruns to PASS.",
        "live_limitation": "Live S03 covers real XSIM simulation repair only; it does not run synthesis, implementation, bitstream, hardware manager, programming, or real-board validation.",
    },
    "S04": {
        "execution_mode": "nested_mcp_stdio_timeout_regression",
        "evidence_class": "stdio_fake_timeout_contract",
        "full_scenario_coverage": False,
    },
    "S05": {
        "execution_mode": "mcp_stdio_synthetic_bundle",
        "evidence_class": "synthetic_bundle_contract",
        "full_scenario_coverage": False,
    },
    "S06": {
        "execution_mode": "mcp_stdio_negative_paths",
        "evidence_class": "stdio_safety_contract",
        "full_scenario_coverage": True,
    },
    "S07": {
        "execution_mode": "mcp_stdio_synthetic_bundle",
        "evidence_class": "synthetic_bundle_contract",
        "full_scenario_coverage": False,
        "limitation": "Synthetic diagnostic bundle; validates validate/handoff/replay sub-contracts, not full existing-project audit collection.",
        "live_execution_mode": "mcp_stdio_live_existing_project_handoff",
        "live_evidence_class": "live_existing_project_handoff_flow",
        "live_requires_flag": "--include-live-vivado",
        "live_note": "With --include-live-vivado, S07 first creates a small seed Project Mode build, then a fresh receiver session opens the existing .xpr in inspection-only mode and validates producer diagnostic, trace, and replay evidence.",
        "live_limitation": "Live S07 does not execute or mutate the original project. Deeper audit requires reconstruction into a distinct MCP-created working project; hardware manager, JTAG, programming, and board validation remain NOT_VALIDATED.",
    },
}


SCENARIO_VALIDATION_POLICY: dict[str, Any] = {
    "id": "default_no_board_scenario_matrix",
    "scope": "Agent-facing software workflow validation without real FPGA hardware",
    "required_no_live_scenarios": list(DEFAULT_NO_LIVE_SCENARIOS),
    "required_live_scenarios": list(DEFAULT_LIVE_SCENARIOS),
    "hardware_boundary": "All hardware-related results must keep hardware_validation.status=NOT_VALIDATED and validated=false.",
    "non_goals": ["real FPGA board validation", "JTAG", "programming", "ILA/VIO runtime debug", "Non-Project Mode"],
}


WORKFLOW_STEP_DETAILS: dict[str, dict[str, Any]] = {
    "start_session": {
        "required_args": [],
        "arg_sources": {},
        "preconditions": [
            "VIVADO_PATH was configured before the MCP server started and still matches the captured canonical executable identity."
        ],
        "failure_stop_conditions": ["Vivado session cannot start."],
        "success_artifacts": ["session_status.connected=true"],
    },
    "stop_session": {
        "required_args": [],
        "arg_sources": {},
        "preconditions": ["Runtime cleanup or project closeout is requested, or the Agent has finished Vivado work."],
        "failure_stop_conditions": ["Managed session cannot be stopped."],
        "success_artifacts": ["session stopped", "runtime_dir if the managed session had one"],
    },
    "get_runtime_cache_status": {
        "required_args": [],
        "arg_sources": {"runtime_dir": "optional user override or stop_session.data.runtime_dir"},
        "preconditions": ["Use the MCP runtime directory, not the Vivado project directory."],
        "failure_stop_conditions": ["Runtime path cannot be resolved or inspected."],
        "success_artifacts": ["runtime cache file counts, size summary, cleanup candidate summary"],
    },
    "clean_runtime_cache": {
        "required_args": [],
        "arg_sources": {
            "runtime_dir": "get_runtime_cache_status.data.runtime_dir or explicit runtime override",
            "dry_run": "default true; set false only after reviewing candidates",
            "max_age_hours": "optional age gate, default 0",
            "include_unknown": "default false; set true only for explicitly approved unknown runtime files",
        },
        "preconditions": [
            "Managed Vivado session is stopped.",
            "No local Vivado, XSIM, or hw_server process is active for real deletion.",
            "runtime_dir is inside the resolved MCP runtime directory and is not a project directory.",
        ],
        "failure_stop_conditions": ["Cleanup returns BLOCK or only dry-run was requested."],
        "success_artifacts": ["deleted or planned file counts", "released or expected bytes", "skipped/refused item list"],
    },
    "create_project": {
        "required_args": ["project_name", "project_dir", "part", "top", "rtl_files"],
        "arg_sources": {
            "project_name": "user requirement",
            "project_dir": "user requirement",
            "part": "target FPGA part",
            "top": "design top",
            "rtl_files": "source inventory",
        },
        "preconditions": [
            "Vivado session is running.",
            "RTL and testbench files already exist and were created or reviewed through an authorized source-editing capability; this MCP does not edit HDL source bytes.",
        ],
        "failure_stop_conditions": ["Project cannot be created or required files are missing."],
        "success_artifacts": ["open Vivado project", "sources_1/constrs_1/sim_1 references"],
    },
    "repair_project_setup": {
        "required_args": [],
        "arg_sources": {
            "project_path": "a project created successfully by this MCP server with a currently valid project capability",
            "rtl_files": "source inventory",
            "xdc_files": "constraint inventory",
            "sim_files": "testbench inventory",
            "top": "design top",
            "testbench_top": "simulation top",
        },
        "preconditions": ["A Vivado session is running and the active project has a valid MCP-created capability; use dry_run=true first after later setup-tool timeouts.", "A create_project TimeoutError does not establish a capability; external or timeout-partial projects remain inspection-only and cannot use dry_run=false repair in place."],
        "failure_stop_conditions": ["Missing files remain or project setup cannot be repaired."],
        "success_artifacts": ["sources_1/constrs_1/sim_1 reconciled", "top and sim_top set", "compile order updated"],
        "tier": "repair",
        "completion_gate": "Project setup is READY or a structured repair failure identifies remaining missing inputs.",
        "partial_handoff_condition": "Repair returns BLOCK or session connection is unavailable; include get_workflow_trace_status.",
    },
    "configure_simulation": {
        "required_args": ["sim_files", "testbench_top"],
        "arg_sources": {"sim_files": "testbench inventory", "testbench_top": "testbench_or_sim_plan"},
        "preconditions": ["Project is open."],
        "failure_stop_conditions": ["sim_1 cannot be configured."],
        "success_artifacts": ["configured sim_1 fileset"],
    },
    "check_syntax": {
        "required_args": ["fileset"],
        "arg_sources": {"fileset": "workflow fileset, default sources_1"},
        "preconditions": ["Project is open."],
        "failure_stop_conditions": ["Syntax errors remain."],
        "success_artifacts": ["syntax status"],
    },
    "get_compile_order": {
        "required_args": ["fileset"],
        "arg_sources": {"fileset": "workflow fileset, default sources_1 or sim_1"},
        "preconditions": ["Project is open."],
        "failure_stop_conditions": ["Missing, duplicate, or unknown source files remain."],
        "success_artifacts": ["compile order diagnostics"],
    },
    "run_behavioral_simulation": {
        "required_args": ["simset"],
        "arg_sources": {"simset": "workflow simset, default sim_1"},
        "preconditions": ["sim_1 is configured.", "testbench has a timeout and $finish."],
        "failure_stop_conditions": ["XSIM reports ERROR or self-checking testbench FAIL."],
        "success_artifacts": ["XSIM logs and waveform artifacts"],
    },
    "get_simulation_result": {
        "required_args": ["simset"],
        "arg_sources": {"simset": "workflow simset, default sim_1"},
        "preconditions": ["Simulation has been run or logs exist."],
        "failure_stop_conditions": ["Simulation result remains FAILED."],
        "success_artifacts": ["parsed XSIM result"],
    },
    "run_synthesis": {
        "required_args": ["run_name"],
        "arg_sources": {"run_name": "workflow run_name, default synth_1"},
        "preconditions": ["Project source state is valid."],
        "failure_stop_conditions": ["Synthesis launch fails."],
        "success_artifacts": ["launched synthesis run"],
    },
    "run_implementation": {
        "required_args": ["run_name"],
        "arg_sources": {"run_name": "workflow run_name, default impl_1"},
        "preconditions": ["Synthesis run completed."],
        "failure_stop_conditions": ["Implementation launch fails."],
        "success_artifacts": ["launched implementation run"],
    },
    "generate_bitstream": {
        "required_args": ["run_name"],
        "arg_sources": {"run_name": "workflow run_name, default impl_1"},
        "preconditions": ["Implementation run completed and readiness is acceptable."],
        "failure_stop_conditions": ["Bitstream launch fails."],
        "success_artifacts": ["launched bitstream run"],
    },
    "get_run_progress": {
        "required_args": ["run_name"],
        "arg_sources": {"run_name": "last launched run"},
        "preconditions": ["A Vivado run exists."],
        "failure_stop_conditions": ["Run status is failed or bitstream is missing after completion."],
        "success_artifacts": ["run status and progress"],
    },
    "diagnose_run_failure": {
        "required_args": ["run_name"],
        "arg_sources": {"run_name": "failed or stalled run, default impl_1"},
        "preconditions": ["A Vivado project is open and the run exists or run logs are still present."],
        "failure_stop_conditions": ["Diagnosis cannot read run progress or run log context."],
        "success_artifacts": ["run status, run log tail, critical-message summary, and repair next_actions"],
        "tier": "repair",
        "completion_gate": "A primary_cause and executable next_actions are available for the failed run.",
    },
    "collect_build_artifacts": {
        "required_args": ["run_name"],
        "arg_sources": {"run_name": "implementation run, default impl_1"},
        "preconditions": ["Bitstream generation has completed or artifacts exist."],
        "failure_stop_conditions": ["Required artifacts cannot be found."],
        "success_artifacts": ["artifact manifest with SHA256"],
    },
    "run_pre_hw_signoff": {
        "required_args": ["run_name"],
        "arg_sources": {"run_name": "implementation run, default impl_1"},
        "preconditions": ["Reports and implementation result are available."],
        "failure_stop_conditions": ["Signoff status is BLOCK."],
        "success_artifacts": ["pre-hardware signoff result"],
    },
    "run_project_audit": {
        "required_args": ["run_name"],
        "arg_sources": {"run_name": "implementation run, default impl_1"},
        "preconditions": ["Project is open."],
        "failure_stop_conditions": ["Audit status is BLOCK."],
        "success_artifacts": ["project audit result with active findings"],
    },
    "collect_diagnostic_bundle": {
        "required_args": ["run_name"],
        "arg_sources": {"run_name": "implementation run, default impl_1"},
        "preconditions": ["Project audit has been run or can be run."],
        "failure_stop_conditions": ["Diagnostic manifest cannot be written."],
        "success_artifacts": ["diagnostic_manifest.json"],
    },
    "validate_diagnostic_bundle": {
        "required_args": ["manifest_path"],
        "arg_sources": {"manifest_path": "collect_diagnostic_bundle.data.manifest_path"},
        "preconditions": ["diagnostic_manifest.json exists."],
        "failure_stop_conditions": ["Diagnostic bundle health is BLOCK."],
        "success_artifacts": ["handoff health and resume_context"],
    },
    "open_project": {
        "required_args": ["project_path"],
        "arg_sources": {"project_path": "user supplied .xpr path"},
        "preconditions": ["Vivado session is running."],
        "failure_stop_conditions": ["Project cannot be opened."],
        "success_artifacts": ["open Vivado project"],
    },
    "get_project_state": {
        "required_args": [],
        "arg_sources": {},
        "preconditions": ["Project is open."],
        "failure_stop_conditions": ["Project state cannot be read."],
        "success_artifacts": ["project/fileset/run state"],
    },
    "analyze_sources": {
        "required_args": ["fileset"],
        "arg_sources": {"fileset": "workflow fileset, default sources_1"},
        "preconditions": ["Project is open."],
        "failure_stop_conditions": ["Source blockers remain."],
        "success_artifacts": ["source diagnostics"],
    },
    "get_constraints_summary": {
        "required_args": ["fileset"],
        "arg_sources": {"fileset": "workflow fileset, default constrs_1"},
        "preconditions": ["Project constraints are loaded."],
        "failure_stop_conditions": ["Constraint inventory cannot be read."],
        "success_artifacts": ["constraint summary"],
    },
    "check_timing_constraints": {
        "required_args": [],
        "arg_sources": {},
        "preconditions": ["Implementation or timing context is available."],
        "failure_stop_conditions": ["Unconstrained clocks/endpoints remain."],
        "success_artifacts": ["check_timing result"],
    },
    "get_timing_summary": {
        "required_args": [],
        "arg_sources": {},
        "preconditions": ["Implementation result is open."],
        "failure_stop_conditions": ["Timing summary cannot be read."],
        "success_artifacts": ["timing summary"],
    },
    "get_timing_paths": {
        "required_args": [],
        "arg_sources": {},
        "preconditions": ["Implementation result is open."],
        "failure_stop_conditions": ["Timing paths cannot be read."],
        "success_artifacts": ["worst timing paths"],
    },
    "analyze_timing_closure": {
        "required_args": [],
        "arg_sources": {},
        "preconditions": ["Timing, DRC, methodology, and messages are available."],
        "failure_stop_conditions": ["Timing closure status is BLOCK."],
        "success_artifacts": ["prioritized timing findings"],
    },
    "check_bitstream_readiness": {
        "required_args": [],
        "arg_sources": {},
        "preconditions": ["Implementation result is open."],
        "failure_stop_conditions": ["Readiness status is BLOCK."],
        "success_artifacts": ["bitstream readiness result"],
    },
}


def build_tool_catalog(tool_names: list[str]) -> dict[str, Any]:
    available = set(tool_names)
    groups = [_available_group(group, available) for group in TOOL_GROUPS]
    return {
        "version": 1,
        "tool_count": len(available),
        "groups": groups,
        "recommended_entrypoints": {
            "new_project_to_bitstream": "get_agent_workflows",
            "existing_project_audit": "get_agent_workflows",
            "subagent_validation_scenarios": "get_agent_scenarios",
            "diagnostic_handoff": "validate_diagnostic_bundle",
            "tcl_policy_dry_run": "safe_tcl",
        },
        "support_status": {
            "maturity": "ALPHA",
            "validated_scope": "Windows, Python 3.11/3.12, Vivado 2021.2, trusted pure RTL/XDC Project Mode",
            "hardware_validation_status": "NOT_VALIDATED",
        },
        "managed_execution_boundary": {
            "supported_design_shape": "trusted_pure_rtl_xdc_project_mode",
            "composite_inputs": "BLOCKED_UNSUPPORTED_COMPOSITE_INPUT",
            "blocked_input_classes": ["Vivado IP", "XCI", "Block Design"],
            "existing_project_policy": "inspection_only_unless_rebuilt_as_a_distinct_mcp_created_project",
            "same_windows_principal_isolation": "NOT_PROVIDED",
        },
        "failure_classes": [
            "SESSION_UNAVAILABLE",
            "PROJECT_DIR_UNAVAILABLE",
            "TCL_FAILED",
            "READINESS_INPUT_FAILED",
            "ANALYSIS_INPUT_FAILED",
            "MANIFEST_NOT_FOUND",
            "DIAGNOSTIC_BUNDLE_BLOCKED",
            "HARDWARE_NOT_VALIDATED",
        ],
        "hardware_boundary": hardware_validation_boundary()
        | {
            "default_hardware_mode": "no_board",
            "hardware_tools_disabled_by_default": True,
            "enable_requires_future_hardware_profile": True,
            "server_policy_env": "VIVADO_AGENT_MCP_HARDWARE_MODE",
            "tool_tiers": hardware_tool_tiers(),
        },
        "tool_metadata": {
            name: {"risk": TOOL_REGISTRY[name].risk}
            for name in sorted(available)
        },
    }


def build_agent_workflows(tool_names: list[str]) -> dict[str, Any]:
    available = set(tool_names)
    workflows = []
    for workflow in WORKFLOWS:
        item = dict(workflow)
        item["available"] = all(tool in available for tool in item["tool_sequence"])
        item["missing_tools"] = [tool for tool in item["tool_sequence"] if tool not in available]
        item["steps"] = [_workflow_step(tool) for tool in item["tool_sequence"]]
        workflows.append(item)
    return {
        "version": 1,
        "workflows": workflows,
        "global_boundaries": [
            "The package maturity is Alpha; workflow results describe software evidence only.",
            "Managed design execution supports trusted pure RTL/XDC Project Mode only and blocks IP/XCI/Block Design inputs.",
            "Existing or timeout-partial projects are inspection-only until reviewed inputs are rebuilt in a distinct MCP-created project.",
            "Do not claim real FPGA board validation; hardware results remain NOT_VALIDATED until board evidence exists.",
            "Do not auto-scan LAN hw_server targets or modify Vivado global configuration.",
            "Keep test and diagnostic outputs under <workspace>/test_use or <workspace>/.vivado_agent_mcp unless the user explicitly supplies a project-local path.",
        ],
    }


def build_agent_scenarios(tool_names: list[str], scenario_id: str = "") -> dict[str, Any]:
    available = set(tool_names)
    requested_id = scenario_id.strip().upper()
    selected: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        if requested_id and scenario["id"].upper() != requested_id:
            continue
        item = dict(scenario)
        required_tools = list(item["required_tools"])
        item["available"] = all(tool in available for tool in required_tools)
        item["missing_tools"] = [tool for tool in required_tools if tool not in available]
        item["required_tool_count"] = len(required_tools)
        item["runner_coverage"] = dict(SCENARIO_RUNNER_COVERAGE.get(item["id"], {}))
        selected.append(item)

    next_actions: list[dict[str, Any]]
    if requested_id and not selected:
        next_actions = [
            next_action(
                "get_agent_scenarios",
                "List available scenario IDs before selecting a scenario.",
                required_args=[],
                arg_sources={},
                preconditions=[],
                stop_condition="Use one of the returned scenarios[].id values.",
            )
        ]
    else:
        next_actions = [
            next_action(
                "get_tool_catalog",
                "Confirm required scenario tools are still registered before running a Subagent validation.",
                required_args=[],
                arg_sources={},
                preconditions=[],
                stop_condition="Tool catalog is available and hardware boundary remains NOT_VALIDATED.",
                optional=True,
            ),
            next_action(
                "get_agent_workflows",
                "Read the workflow recipe referenced by the selected scenario before executing project steps.",
                required_args=[],
                arg_sources={},
                preconditions=["Selected scenario has a non-empty workflow_id."],
                stop_condition="Workflow steps, parameter sources, and stop conditions are understood.",
                optional=True,
            ),
        ]

    return {
        "version": 1,
        "scenario_count": len(SCENARIOS),
        "selected_count": len(selected),
        "scenario_id": requested_id,
        "scenarios": selected,
        "recommended_artifact_root": "<workspace>/test_use",
        "validation_policy": dict(SCENARIO_VALIDATION_POLICY),
        "feedback_template": {
            "scenario_id": "",
            "result": "PASS|WATCH|BLOCK",
            "tool_count": 0,
            "mcp_tools_used": [],
            "artifacts": [],
            "issues": [],
            "improvement_suggestions": [],
            "hardware_validation_status": "NOT_VALIDATED",
        },
        "global_boundaries": [
            "Scenario PASS only describes the stated software evidence class and does not attest distribution readiness or real hardware behavior.",
            "Managed live scenarios support trusted pure RTL/XDC Project Mode only and must stop on IP/XCI/Block Design inputs.",
            "Scenario execution must not claim real FPGA board validation; hardware results remain NOT_VALIDATED until board evidence exists.",
            "Place temporary Subagent projects under <workspace>/test_use and project deliverables under project-local vmcp_* directories.",
            "Do not auto-program hardware, auto-scan LAN hw_server targets, or modify Vivado global configuration.",
        ],
        "next_actions": dedupe_next_actions(next_actions),
    }


def validate_diagnostic_bundle_manifest(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(os.path.abspath(os.fspath(manifest_path)))
    if not path.exists():
        raise FileNotFoundError(f"diagnostic manifest not found: {path}")
    bundle_root = path.parent
    manifest, _ = load_json_evidence(
        path,
        root=bundle_root,
        max_bytes=MAX_DIAGNOSTIC_MANIFEST_BYTES,
    )
    if not isinstance(manifest, dict):
        raise ValueError("diagnostic manifest must be a JSON object")
    files = manifest.get("files", [])
    if not isinstance(files, list):
        raise ValueError("diagnostic manifest files must be a list")
    authenticity = verify_diagnostic_manifest_attestation(manifest)

    summary = manifest.get("summary", {}) if isinstance(manifest.get("summary"), dict) else {}
    bundle_mode = str(manifest.get("bundle_mode", "legacy_reference"))
    declared_portable = manifest.get("portable")
    bundle_mode_supported = (
        bundle_mode in {"reference", "legacy_reference"}
        and (declared_portable is None or declared_portable is False)
    )
    portable = False
    categories = _categories(files)
    category_counts = _category_counts(files)
    duplicate_singletons = sorted(
        category
        for category, count in category_counts.items()
        if category in SINGLETON_DIAGNOSTIC_CATEGORIES and count != 1
    )
    missing_categories = [category for category in REQUIRED_DIAGNOSTIC_CATEGORIES if category not in categories]
    workflow_trace_missing = "workflow_trace" not in categories
    missing_files: list[str] = []
    path_escapes: list[dict[str, str]] = []
    invalid_entries: list[dict[str, str]] = []
    invalid_entries.extend(
        {"path": str(path), "reason": "duplicate_singleton_category", "category": category}
        for category in duplicate_singletons
    )
    if not bundle_mode_supported:
        invalid_entries.append(
            {
                "path": str(path),
                "reason": "portable_bundle_mode_not_supported",
            }
        )
    hash_mismatches: list[dict[str, str]] = []
    size_mismatches: list[dict[str, str]] = []
    workflow_trace_append_only_growth: list[dict[str, str]] = []
    workflow_trace_integrity: dict[str, Any] = {
        "status": "WARN" if workflow_trace_missing else "UNKNOWN",
        "format": "missing" if workflow_trace_missing else "unknown",
        "issues": ["workflow trace is missing"] if workflow_trace_missing else [],
        "verified_entries": 0,
    }
    resource_limits: list[dict[str, str]] = []
    if len(files) > MAX_DIAGNOSTIC_FILES:
        resource_limits.append({"reason": "too_many_files", "file_count": str(len(files)), "limit": str(MAX_DIAGNOSTIC_FILES)})
    declared_total_size = 0
    category_entries: dict[str, tuple[dict[str, Any], Path, EvidenceSnapshot]] = {}
    validated_primary_files: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(files[:MAX_DIAGNOSTIC_FILES]):
        if not isinstance(entry, dict):
            invalid_entries.append({"index": str(index), "reason": "entry_not_object"})
            continue
        file_path, path_error = _entry_path(entry, bundle_root)
        if path_error is not None:
            if path_error["reason"] == "missing_path":
                invalid_entries.append(path_error)
            else:
                path_escapes.append(path_error)
            continue
        assert file_path is not None
        expected_hash = entry.get("sha256")
        expected_size = entry.get("size")
        has_valid_integrity = True
        if not _is_sha256(expected_hash):
            invalid_entries.append({"path": str(file_path), "reason": "missing_or_invalid_sha256"})
            has_valid_integrity = False
        if not isinstance(expected_size, int) or expected_size < 0:
            invalid_entries.append({"path": str(file_path), "reason": "missing_or_invalid_size"})
            has_valid_integrity = False
        if isinstance(expected_size, int) and expected_size >= 0:
            declared_total_size += expected_size
            if expected_size > MAX_DIAGNOSTIC_FILE_BYTES:
                resource_limits.append(
                    {
                        "path": str(file_path),
                        "reason": "file_size_exceeds_limit",
                        "size": str(expected_size),
                        "limit": str(MAX_DIAGNOSTIC_FILE_BYTES),
                    }
                )
                has_valid_integrity = False
        if not file_path.exists():
            missing_files.append(str(file_path))
            continue
        if not has_valid_integrity:
            continue
        try:
            snapshot = load_evidence_snapshot(
                file_path,
                root=bundle_root,
                max_bytes=MAX_DIAGNOSTIC_FILE_BYTES,
            )
        except (OSError, ValueError, ManagedPathError) as exc:
            invalid_entries.append(
                {"path": str(file_path), "reason": f"evidence_snapshot_unavailable:{exc.__class__.__name__}"}
            )
            continue
        actual_size = snapshot.size
        if str(entry.get("category", "")) == "workflow_trace":
            workflow_trace_integrity = validate_workflow_trace_file(file_path)
            trace_growth = _workflow_trace_append_only_growth(
                file_path=file_path,
                expected_hash=str(expected_hash),
                expected_size=expected_size,
                actual_size=actual_size,
                content=snapshot.content,
            )
            if trace_growth is not None:
                if trace_growth:
                    workflow_trace_append_only_growth.append(trace_growth)
                if "workflow_trace" not in duplicate_singletons:
                    validated_primary_files["workflow_trace"] = _validated_evidence_reference(entry, file_path, snapshot)
                continue
        if actual_size > MAX_DIAGNOSTIC_FILE_BYTES:
            resource_limits.append(
                {
                    "path": str(file_path),
                    "reason": "actual_file_size_exceeds_limit",
                    "size": str(actual_size),
                    "limit": str(MAX_DIAGNOSTIC_FILE_BYTES),
                }
            )
            continue
        actual_hash = snapshot.sha256
        if str(expected_hash) != actual_hash:
            hash_mismatches.append({"path": str(file_path), "expected": str(expected_hash), "actual": actual_hash})
        if expected_size != actual_size:
            size_mismatches.append({"path": str(file_path), "expected": str(expected_size), "actual": str(actual_size)})
        category = str(entry.get("category", ""))
        integrity_matches = str(expected_hash) == actual_hash and expected_size == actual_size
        if integrity_matches and category not in duplicate_singletons:
            validated_primary_files[category] = _validated_evidence_reference(entry, file_path, snapshot)
        if integrity_matches and category in {"artifact_manifest", "report_manifest"} and category not in duplicate_singletons:
            category_entries[category] = (entry, file_path, snapshot)
    invalid_entries.extend(
        _validate_nested_manifests(
            category_entries,
            bundle_root=bundle_root,
            project_dir=str(manifest.get("project_dir", "")),
            declared_design_execution_identity_sha256=str(
                summary.get("design_execution_identity_sha256", "")
            ),
        )
    )
    if declared_total_size > MAX_DIAGNOSTIC_TOTAL_BYTES:
        resource_limits.append(
            {
                "reason": "total_size_exceeds_limit",
                "size": str(declared_total_size),
                "limit": str(MAX_DIAGNOSTIC_TOTAL_BYTES),
            }
        )

    summary_hardware_present = isinstance(summary.get("hardware_validation"), dict)
    manifest_hardware_present = isinstance(manifest.get("hardware_validation"), dict)
    hardware_validation = summary.get("hardware_validation") if summary_hardware_present else {}
    manifest_hardware_validation = manifest.get("hardware_validation") if manifest_hardware_present else {}
    hardware_validation_present = summary_hardware_present and manifest_hardware_present
    hardware_boundary_invalid = (
        _hardware_boundary_invalid(
            hardware_validation=hardware_validation,
            hardware_validation_missing=not summary_hardware_present,
        )
        or _hardware_boundary_invalid(
            hardware_validation=manifest_hardware_validation,
            hardware_validation_missing=not manifest_hardware_present,
        )
        or hardware_validation != manifest_hardware_validation
    )
    evidence_freshness = summary.get("evidence_freshness") if isinstance(summary.get("evidence_freshness"), dict) else {}
    design_execution_identity_sha256 = str(summary.get("design_execution_identity_sha256", ""))
    freshness_missing = not bool(evidence_freshness)
    freshness_status = str(evidence_freshness.get("status", "")).upper() if evidence_freshness else ""
    audit_status = str(summary.get("audit_status", ""))
    audit_effective_status = str(summary.get("effective_status", audit_status))
    waived_finding_count = int(summary.get("waived_finding_count", summary.get("waived_count", 0)) or 0)
    complete = bool(summary.get("complete")) and not summary.get("missing_required_categories")
    status = _bundle_status(
        missing_categories=missing_categories,
        missing_files=missing_files,
        path_escapes=path_escapes,
        invalid_entries=invalid_entries,
        hash_mismatches=hash_mismatches,
        size_mismatches=size_mismatches,
        resource_limits=resource_limits,
        audit_status=audit_status,
        audit_effective_status=audit_effective_status,
        waived_finding_count=waived_finding_count,
        complete=complete,
        hardware_boundary_invalid=hardware_boundary_invalid,
        freshness_missing=freshness_missing,
        freshness_status=freshness_status,
        workflow_trace_missing=workflow_trace_missing,
        workflow_trace_integrity_status=str(workflow_trace_integrity.get("status", "UNKNOWN")),
        authenticity_status=str(authenticity.get("status", "UNKNOWN")),
        portable=portable,
    )
    handoff_ready = status == "READY" and portable
    health = {
        "status": status,
        "handoff_ready": handoff_ready,
        "manifest_path": str(path),
        "bundle_dir": str(bundle_root),
        "declared_bundle_dir": str(manifest.get("bundle_dir", "")),
        "bundle_mode": bundle_mode,
        "portable": portable,
        "declared_portable": declared_portable,
        "bundle_mode_supported": bundle_mode_supported,
        "portable_mode_supported": False,
        "portability": manifest.get("portability", {}),
        "audit_status": audit_status,
        "audit_effective_status": audit_effective_status,
        "waived_finding_count": waived_finding_count,
        "complete": complete,
        "categories": sorted(categories),
        "missing_required_categories": missing_categories,
        "workflow_trace_missing": workflow_trace_missing,
        "workflow_trace_append_only_growth": workflow_trace_append_only_growth,
        "workflow_trace_integrity": workflow_trace_integrity,
        "integrity_model": manifest.get("integrity_model", {}),
        "authenticity": authenticity,
        "missing_files": missing_files,
        "path_escapes": path_escapes,
        "invalid_entries": invalid_entries,
        "hash_mismatches": hash_mismatches,
        "size_mismatches": size_mismatches,
        "resource_limits": resource_limits,
        "limits": {
            "max_files": MAX_DIAGNOSTIC_FILES,
            "max_file_bytes": MAX_DIAGNOSTIC_FILE_BYTES,
            "max_total_bytes": MAX_DIAGNOSTIC_TOTAL_BYTES,
        },
        "hardware_validation": hardware_validation,
        "manifest_hardware_validation": manifest_hardware_validation,
        "hardware_validation_missing": not hardware_validation_present,
        "summary_hardware_validation_missing": not summary_hardware_present,
        "manifest_hardware_validation_missing": not manifest_hardware_present,
        "hardware_validation_status": str(hardware_validation.get("status", "")) if hardware_validation else "",
        "hardware_validation_validated": hardware_validation.get("validated") if hardware_validation else None,
        "hardware_boundary_invalid": hardware_boundary_invalid,
        "evidence_freshness": evidence_freshness,
        "evidence_freshness_missing": freshness_missing,
        "evidence_freshness_status": freshness_status,
        "design_execution_identity_sha256": design_execution_identity_sha256,
    }
    health["handoff_reviewable"] = _bundle_handoff_reviewable(health)
    health["review_required_reasons"] = _bundle_review_required_reasons(health)
    health["review_guidance"] = _bundle_review_guidance(health)
    return {
        "status": status,
        "handoff_ready": handoff_ready,
        "handoff_reviewable": health["handoff_reviewable"],
        "health": health,
        "resume_context": _resume_context(manifest, files, health, validated_primary_files=validated_primary_files),
        "hardware_validation": hardware_validation,
        "bundle_mode": bundle_mode,
        "portable": portable,
        "validation_scope": str(summary.get("validation_scope", "pre_hardware_software")),
        "ready_meaning": str(
            summary.get(
                "ready_meaning",
                "READY means the diagnostic bundle is no-board software handoff evidence, not real FPGA board validation.",
            )
        ),
        "next_actions": _bundle_next_actions(health),
        "next_steps": _bundle_next_steps(health),
    }


def _available_group(group: dict[str, Any], available: set[str]) -> dict[str, Any]:
    item = dict(group)
    tools = [tool for tool in group["tools"] if tool in available]
    item["tools"] = tools
    item["missing_tools"] = [tool for tool in group["tools"] if tool not in available]
    item["tool_count"] = len(tools)
    return item


def _workflow_step(tool: str) -> dict[str, Any]:
    details = WORKFLOW_STEP_DETAILS.get(tool, {})
    return {
        "tool": tool,
        "required_args": list(details.get("required_args", [])),
        "arg_sources": dict(details.get("arg_sources", {})),
        "preconditions": list(details.get("preconditions", [])),
        "failure_stop_conditions": list(details.get("failure_stop_conditions", [])),
        "success_artifacts": list(details.get("success_artifacts", [])),
        "tier": str(details.get("tier", _default_step_tier(tool))),
        "max_wait_s": int(details.get("max_wait_s", _default_step_max_wait(tool))),
        "poll_interval_s": int(details.get("poll_interval_s", 15 if tool == "get_run_progress" else 0)),
        "completion_gate": str(details.get("completion_gate", _default_completion_gate(tool))),
        "partial_handoff_condition": str(details.get("partial_handoff_condition", _default_partial_handoff_condition(tool))),
    }


def _default_step_tier(tool: str) -> str:
    if tool in {"stop_session", "get_runtime_cache_status", "clean_runtime_cache"}:
        return "cleanup"
    if tool in {
        "collect_build_artifacts",
        "collect_report_bundle",
        "run_pre_hw_signoff",
        "run_project_audit",
        "collect_diagnostic_bundle",
        "validate_diagnostic_bundle",
    }:
        return "handoff_required"
    if tool in {"get_simulation_result", "analyze_sources", "analyze_timing_closure", "get_timing_paths", "check_timing_constraints"}:
        return "repair"
    if tool in {
        "start_session",
        "create_project",
        "open_project",
        "get_project_state",
        "configure_simulation",
        "check_syntax",
        "get_compile_order",
        "run_behavioral_simulation",
        "run_synthesis",
        "run_implementation",
        "generate_bitstream",
        "get_run_progress",
    }:
        return "core_bitstream"
    return "diagnostic"


def _default_step_max_wait(tool: str) -> int:
    if tool == "start_session":
        return 240
    if tool == "run_behavioral_simulation":
        return 300
    if tool in {"run_synthesis", "run_implementation", "generate_bitstream"}:
        return 60
    if tool == "get_run_progress":
        return 1800
    if tool in {"collect_diagnostic_bundle", "run_project_audit", "run_pre_hw_signoff"}:
        return 300
    return 120


def _default_completion_gate(tool: str) -> str:
    if tool == "get_run_progress":
        return "Run reaches terminal state or the Agent emits a partial handoff after max_wait_s."
    if tool == "generate_bitstream":
        return "Bitstream run is launched; completion must be confirmed by get_run_progress(expect_bitstream=true)."
    if tool == "collect_build_artifacts":
        return "Artifact manifest with bitstream SHA256 exists."
    if tool == "validate_diagnostic_bundle":
        return "Diagnostic bundle health is READY or explicitly reviewed WARN."
    return f"{tool} returns ok=true or a structured failure with next_actions."


def _default_partial_handoff_condition(tool: str) -> str:
    if tool in {"get_run_progress", "run_behavioral_simulation", "collect_diagnostic_bundle"}:
        return "max_wait_s exceeded, timeout, or BLOCK result; include get_workflow_trace_status in handoff."
    return "Tool returns failure or required preconditions cannot be satisfied; include get_workflow_trace_status in handoff."


def _categories(files: list[Any]) -> set[str]:
    return {str(entry.get("category", "")) for entry in files if isinstance(entry, dict) and entry.get("category")}


def _category_counts(files: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in files:
        if isinstance(entry, dict) and entry.get("category"):
            category = str(entry["category"])
            counts[category] = counts.get(category, 0) + 1
    return counts


def _resume_context(
    manifest: dict[str, Any],
    files: list[Any],
    health: dict[str, Any],
    *,
    validated_primary_files: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    category_counts = _category_counts(files)
    primary_file_refs = {
        category: {
            "path": str(entry.get("path", "")),
            "size": entry.get("validated_size"),
            "sha256": str(entry.get("validated_sha256", "")),
            "file_id": str(entry.get("validated_file_id", "")),
            "mtime_ns": entry.get("validated_mtime_ns"),
            "consumption_contract": "revalidate_size_sha256_file_id_mtime_before_read",
        }
        for category, entry in validated_primary_files.items()
    }
    return {
        "manifest_path": health["manifest_path"],
        "bundle_dir": health["bundle_dir"],
        "status": health["status"],
        "audit_status": health["audit_status"],
        "handoff_ready": health["handoff_ready"],
        "handoff_reviewable": bool(health.get("handoff_reviewable")),
        "review_required_reasons": list(health.get("review_required_reasons", [])),
        "review_guidance": dict(health.get("review_guidance", {})),
        "categories": health["categories"],
        "category_counts": category_counts,
        "primary_file_refs": primary_file_refs,
        "workflow_trace_ref": primary_file_refs.get("workflow_trace", {}),
        "workflow_trace_append_only_growth": health.get("workflow_trace_append_only_growth", []),
        "workflow_trace_captured_size": _workflow_trace_captured_size(validated_primary_files.get("workflow_trace")),
        "workflow_trace_integrity": dict(health.get("workflow_trace_integrity", {})),
        "authenticity": dict(health.get("authenticity", {})),
        "hardware_validation_status": health["hardware_validation_status"],
        "evidence_freshness_status": health["evidence_freshness_status"],
        "design_execution_identity_sha256": health.get("design_execution_identity_sha256", ""),
        "recommended_entrypoint": "get_agent_workflows",
        "bundle_mode": health.get("bundle_mode", "legacy_reference"),
        "portable": bool(health.get("portable")),
    }


def _entry_path(entry: dict[str, Any], bundle_root: Path) -> tuple[Path | None, dict[str, str] | None]:
    raw_text = str(entry.get("path", "")).strip()
    if not raw_text:
        return None, {"path": "", "reason": "missing_path"}
    raw = Path(raw_text)
    candidate = Path(os.path.abspath(os.fspath(raw if raw.is_absolute() else bundle_root / raw)))
    try:
        validate_managed_path(bundle_root, candidate, allow_missing_leaf=True)
    except ManagedPathError as exc:
        reason = "path_outside_bundle" if "outside managed root" in str(exc) else "path_reparse_or_invalid"
        return candidate, {"path": str(candidate), "reason": reason}
    except ValueError:
        return candidate, {"path": str(candidate), "reason": "path_outside_bundle"}
    return candidate, None


def _validate_nested_manifests(
    entries: dict[str, tuple[dict[str, Any], Path, EvidenceSnapshot]],
    *,
    bundle_root: Path,
    project_dir: str,
    declared_design_execution_identity_sha256: str,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    parsed: dict[str, dict[str, Any]] = {}
    project = Path(os.path.abspath(project_dir)) if project_dir else None
    for category, (entry, path, snapshot) in entries.items():
        try:
            data = json.loads(snapshot.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            issues.append({"path": str(path), "reason": f"nested_manifest_unreadable:{exc.__class__.__name__}"})
            continue
        if not isinstance(data, dict):
            issues.append({"path": str(path), "reason": "nested_manifest_not_object"})
            continue
        parsed[category] = data
        if entry.get("evidence_consumption") == "validated_bytes_snapshot":
            if str(entry.get("source_sha256", "")).lower() != snapshot.sha256:
                issues.append({"path": str(path), "reason": "validated_snapshot_digest_mismatch"})
        if data.get("schema_version") != 4:
            issues.append({"path": str(path), "reason": "nested_manifest_schema_version"})
        design_identity = data.get("design_execution_identity")
        design_identity_sha256 = str(data.get("design_execution_identity_sha256", ""))
        if (
            not isinstance(design_identity, dict)
            or design_identity.get("status") != "READY"
            or not _is_sha256(design_identity_sha256)
            or design_identity_sha256 != str(design_identity.get("sha256", ""))
        ):
            issues.append({"path": str(path), "reason": "nested_manifest_design_identity_invalid"})
        allowed = {"READY"} if category == "artifact_manifest" else {"READY", "WARN"}
        if str(data.get("status", "")).upper() not in allowed:
            issues.append({"path": str(path), "reason": "nested_manifest_status"})
        freshness = data.get("evidence_freshness") if isinstance(data.get("evidence_freshness"), dict) else {}
        if str(freshness.get("status", "")).upper() != "FRESH" or _truthy(freshness.get("needs_refresh")):
            issues.append({"path": str(path), "reason": "nested_manifest_stale"})
        if project is None:
            issues.append({"path": str(path), "reason": "nested_manifest_project_missing"})
            continue
        items_key = "artifacts" if category == "artifact_manifest" else "reports"
        items = data.get(items_key)
        if not isinstance(items, list):
            issues.append({"path": str(path), "reason": f"nested_manifest_{items_key}_invalid"})
            continue
        if category == "artifact_manifest" and not any(
            isinstance(item, dict) and str(item.get("category", "")) == "bitstream"
            for item in items
        ):
            issues.append({"path": str(path), "reason": "nested_artifact_bitstream_missing"})
        expected_root = project / ("vmcp_artifacts" if category == "artifact_manifest" else "vmcp_reports")
        for item in items:
            if not isinstance(item, dict):
                issues.append({"path": str(path), "reason": "nested_manifest_entry_not_object"})
                continue
            referenced = item.get("export_path") if category == "artifact_manifest" else item.get("path")
            reference = Path(os.path.abspath(os.fspath(referenced or "")))
            try:
                reference_snapshot = load_evidence_snapshot(
                    reference,
                    root=expected_root,
                    max_bytes=MAX_DIAGNOSTIC_FILE_BYTES,
                )
            except (ManagedPathError, OSError, ValueError) as exc:
                issues.append({"path": str(reference), "reason": f"nested_reference_invalid:{exc.__class__.__name__}"})
                continue
            expected_size = item.get("size")
            expected_hash = str(item.get("sha256", "")).lower()
            if not isinstance(expected_size, int) or reference_snapshot.size != expected_size:
                issues.append({"path": str(reference), "reason": "nested_reference_size_mismatch"})
            if not _is_sha256(expected_hash) or reference_snapshot.sha256 != expected_hash:
                issues.append({"path": str(reference), "reason": "nested_reference_hash_mismatch"})
    artifact = parsed.get("artifact_manifest", {})
    report = parsed.get("report_manifest", {})
    if artifact and report:
        if str(artifact.get("run_name", "")) != str(report.get("run_name", "")):
            issues.append({"path": str(bundle_root), "reason": "nested_manifest_run_mismatch"})
        artifact_snapshot = artifact.get("run_snapshot", {}) if isinstance(artifact.get("run_snapshot"), dict) else {}
        report_snapshot = report.get("run_snapshot", {}) if isinstance(report.get("run_snapshot"), dict) else {}
        if not str(artifact_snapshot.get("session_generation_id", "")) or (
            str(artifact_snapshot.get("session_generation_id", ""))
            != str(report_snapshot.get("session_generation_id", ""))
        ):
            issues.append({"path": str(bundle_root), "reason": "nested_manifest_generation_mismatch"})
        artifact_design_identity = artifact.get("design_execution_identity")
        report_design_identity = report.get("design_execution_identity")
        if artifact_design_identity != report_design_identity:
            issues.append({"path": str(bundle_root), "reason": "nested_manifest_design_identity_mismatch"})
        nested_identity_sha256 = str(artifact.get("design_execution_identity_sha256", ""))
        if (
            not _is_sha256(declared_design_execution_identity_sha256)
            or nested_identity_sha256 != declared_design_execution_identity_sha256
        ):
            issues.append({"path": str(bundle_root), "reason": "diagnostic_design_identity_mismatch"})
    return issues


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in value)


def _workflow_trace_append_only_growth(
    *,
    file_path: Path,
    expected_hash: str,
    expected_size: int,
    actual_size: int,
    content: bytes,
) -> dict[str, str] | None:
    if actual_size < expected_size:
        return None
    prefix_hash = hashlib.sha256(content[:expected_size]).hexdigest()
    if prefix_hash != expected_hash:
        return None
    if actual_size == expected_size:
        return {}
    return {
        "path": str(file_path),
        "captured_size": str(expected_size),
        "actual_size": str(actual_size),
        "captured_sha256": expected_hash,
        "status": "append_only_growth",
    }


def _workflow_trace_captured_size(entry: dict[str, Any] | None) -> int:
    return int(entry["size"]) if isinstance(entry, dict) and isinstance(entry.get("size"), int) else 0


def _validated_evidence_reference(
    entry: dict[str, Any],
    path: Path,
    snapshot: EvidenceSnapshot,
) -> dict[str, Any]:
    return dict(entry) | {
        "path": str(path),
        "validated_size": snapshot.size,
        "validated_sha256": snapshot.sha256,
        "validated_file_id": snapshot.file_id,
        "validated_mtime_ns": snapshot.mtime_ns,
    }


def _hardware_boundary_invalid(*, hardware_validation: dict[str, Any], hardware_validation_missing: bool) -> bool:
    if hardware_validation_missing:
        return True
    if str(hardware_validation.get("status", "")).upper() != "NOT_VALIDATED":
        return True
    return hardware_validation.get("validated") is not False


def _bundle_handoff_reviewable(health: dict[str, Any]) -> bool:
    if health["status"] == "READY":
        return True
    if health["status"] != "WARN":
        return False
    integrity_or_boundary_issue = any(
        [
            health["missing_required_categories"],
            health["missing_files"],
            health["path_escapes"],
            health["invalid_entries"],
            health["hash_mismatches"],
            health["size_mismatches"],
            health["resource_limits"],
            health.get("hardware_boundary_invalid"),
            health["evidence_freshness_missing"],
            str(health["evidence_freshness_status"]).upper() != "FRESH",
            health.get("workflow_trace_missing"),
            not health["complete"],
            str(health["audit_status"]).upper() == "BLOCK",
        ]
    )
    return not integrity_or_boundary_issue


def _bundle_review_required_reasons(health: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    audit_status = str(health.get("audit_status", "")).upper()
    if audit_status and audit_status != "READY":
        reasons.append(f"audit_status={audit_status}")
    if int(health.get("waived_finding_count", 0)) > 0:
        reasons.append("waived_findings_require_review")
    if not health.get("complete"):
        reasons.append("manifest_incomplete")
    if health.get("workflow_trace_missing"):
        reasons.append("workflow_trace_missing")
    trace_integrity_status = str(health.get("workflow_trace_integrity", {}).get("status", "")).upper()
    if trace_integrity_status and trace_integrity_status != "READY":
        reasons.append(f"workflow_trace_integrity={trace_integrity_status}")
    authenticity_status = str(health.get("authenticity", {}).get("status", "")).upper()
    if authenticity_status and authenticity_status != "READY":
        reasons.append(f"authenticity={authenticity_status}")
    if health.get("evidence_freshness_missing") or str(health.get("evidence_freshness_status", "")).upper() != "FRESH":
        reasons.append("evidence_freshness_not_fresh")
    if health.get("hardware_boundary_invalid"):
        reasons.append("hardware_boundary_invalid")
    if not health.get("portable"):
        reasons.append("bundle_not_portable")
    return reasons


def _bundle_review_guidance(health: dict[str, Any]) -> dict[str, Any]:
    if health["status"] == "READY":
        return {
            "classification": "ready",
            "agent_instruction": "Archive or resume from this bundle as no-board software handoff evidence.",
            "acceptable_warn_categories": [],
            "must_fix_before_handoff": [],
        }
    if health.get("handoff_reviewable"):
        return {
            "classification": "reviewable_warn",
            "agent_instruction": "Treat this as a project-local review index, not a portable READY bundle and not real hardware validation.",
            "acceptable_warn_categories": [
                "audit_status=WARN with no active BLOCK findings",
                "READY_WITH_WAIVERS with explicit waiver evidence retained for human review",
                "documented board-handoff warnings such as missing CFGBVS/CONFIG_VOLTAGE when no real board is available",
                "documented IO timing review warnings such as no_input_delay/no_output_delay when board-level requirements are not yet supplied",
            ],
            "must_fix_before_handoff": [
                "missing files, hash or size mismatches, path escapes, invalid manifest entries, resource-limit failures",
                "missing or non-NOT_VALIDATED hardware boundary",
                "missing or stale evidence_freshness",
                "audit_status=BLOCK",
            ],
            "review_required_evidence": [
                "Review every waived finding and its matched_finding_sha256; waivers do not produce handoff_ready without an external approval anchor.",
                "Record why missing input/output delays are acceptable for this no-board software handoff, or provide board-level IO timing requirements and rerun timing/audit.",
                "Keep hardware_validation.status=NOT_VALIDATED until real FPGA board/JTAG evidence exists.",
            ],
        }
    return {
        "classification": "not_reviewable",
        "agent_instruction": "Regenerate or repair the bundle before handoff.",
        "acceptable_warn_categories": [],
        "must_fix_before_handoff": _bundle_review_required_reasons(health) or ["bundle health is not reviewable"],
    }


def _bundle_status(
    *,
    missing_categories: list[str],
    missing_files: list[str],
    path_escapes: list[dict[str, str]],
    invalid_entries: list[dict[str, str]],
    hash_mismatches: list[dict[str, str]],
    size_mismatches: list[dict[str, str]],
    resource_limits: list[dict[str, str]],
    audit_status: str,
    audit_effective_status: str,
    waived_finding_count: int,
    complete: bool,
    hardware_boundary_invalid: bool,
    freshness_missing: bool,
    freshness_status: str,
    workflow_trace_missing: bool,
    workflow_trace_integrity_status: str,
    authenticity_status: str,
    portable: bool,
) -> str:
    if missing_categories or missing_files or path_escapes or invalid_entries or hash_mismatches or size_mismatches or resource_limits:
        return "BLOCK"
    if audit_status.upper() == "BLOCK":
        return "BLOCK"
    if hardware_boundary_invalid:
        return "BLOCK"
    if freshness_missing or freshness_status != "FRESH":
        return "WARN"
    if workflow_trace_missing:
        return "WARN"
    if workflow_trace_integrity_status.upper() == "BLOCK":
        return "BLOCK"
    if workflow_trace_integrity_status.upper() != "READY":
        return "WARN"
    if authenticity_status.upper() == "BLOCK":
        return "BLOCK"
    if authenticity_status.upper() != "READY":
        return "WARN"
    if audit_effective_status.upper() == "READY_WITH_WAIVERS" or waived_finding_count > 0:
        return "WARN"
    if audit_status.upper() != "READY":
        return "WARN"
    if not complete:
        return "WARN"
    if not portable:
        return "WARN"
    return "READY"


def _bundle_next_steps(health: dict[str, Any]) -> list[str]:
    if health["status"] == "READY":
        return ["Archive this diagnostic bundle with the project handoff and keep real hardware validation deferred."]
    if health.get("handoff_reviewable"):
        return [
            "Review audit findings in audit_result.json and keep the bundle with its originating project; reference bundles are not self-contained.",
            "Do not claim real FPGA board validation; hardware_validation.status remains NOT_VALIDATED.",
        ]
    steps = []
    if (
        health["missing_required_categories"]
        or health["missing_files"]
        or health["hash_mismatches"]
        or health["size_mismatches"]
        or health["invalid_entries"]
        or health["resource_limits"]
    ):
        steps.append("Rebuild collect_diagnostic_bundle after fixing missing categories, missing files, hash mismatches, or bundle resource limits.")
    if health["path_escapes"]:
        steps.append("Regenerate the diagnostic bundle so every manifest file entry resolves inside the bundle directory.")
    if health.get("hardware_boundary_invalid"):
        steps.append("Restore hardware_validation.status=NOT_VALIDATED and validated=false until real FPGA board evidence exists.")
    if health["evidence_freshness_missing"] or health["evidence_freshness_status"] != "FRESH":
        steps.append("Regenerate audit and diagnostic bundle evidence so evidence_freshness.status is FRESH before handoff.")
    if health.get("workflow_trace_missing"):
        steps.append("Regenerate collect_diagnostic_bundle so workflow_trace.jsonl is included for Agent handoff replay.")
    trace_integrity_status = str(health.get("workflow_trace_integrity", {}).get("status", "")).upper()
    if trace_integrity_status and trace_integrity_status != "READY":
        steps.append("Regenerate the diagnostic bundle from a self-consistent hash-chain workflow trace; legacy traces remain review-only.")
    authenticity_status = str(health.get("authenticity", {}).get("status", "")).upper()
    if authenticity_status and authenticity_status != "READY":
        steps.append(
            "Validate on the originating managed runtime or regenerate the bundle there; local HMAC attestation is not a portable public-key signature."
        )
    if str(health["audit_status"]).upper() == "BLOCK":
        steps.append("Resolve active project audit blockers before treating the handoff as ready.")
    elif str(health["audit_status"]).upper() != "READY":
        steps.append("Run run_project_audit and regenerate the diagnostic bundle so audit_status is READY before handoff.")
    if not steps:
        steps.append("Re-run validate_diagnostic_bundle after regenerating the diagnostic bundle.")
    return steps


def _bundle_next_actions(health: dict[str, Any]) -> list[dict[str, Any]]:
    if health["status"] == "READY":
        return [
            next_action(
                "get_agent_workflows",
                "Select the next Agent workflow using this validated diagnostic bundle as handoff evidence.",
                preconditions=["validate_diagnostic_bundle returned READY."],
                stop_condition="Agent selects a workflow recipe or archives the bundle.",
            )
        ]
    if health.get("handoff_reviewable"):
        actions = [
            next_action(
                "get_agent_workflows",
                "Resume from this project-local reference index after explicitly reviewing WARN findings and portability limits.",
                preconditions=[
                    "validate_diagnostic_bundle returned WARN with handoff_reviewable=true.",
                    "Audit findings and bundle_not_portable have been reviewed and accepted or assigned.",
                ],
                stop_condition="Agent resumes from the originating project or records why the reference index is sufficient.",
            )
        ]
        if str(health.get("audit_status", "")).upper() != "READY" or int(health.get("waived_finding_count", 0)) > 0:
            actions.append(next_action(
                "run_project_audit",
                "Optionally rerun audit after addressing warning findings; this does not make a reference bundle portable.",
                required_args=["run_name"],
                arg_sources={"run_name": "workflow.run_name or current implementation run"},
                preconditions=["Original Vivado project is open and warning fixes or waivers were applied."],
                stop_condition="run_project_audit data.status is READY or remaining findings are documented.",
                optional=True,
            ))
        return actions

    actions: list[dict[str, Any]] = []
    hardware_boundary_invalid = bool(health.get("hardware_boundary_invalid"))
    freshness_stale = health["evidence_freshness_missing"] or str(health["evidence_freshness_status"]).upper() != "FRESH"
    workflow_trace_missing = bool(health.get("workflow_trace_missing"))
    workflow_trace_integrity_stale = str(health.get("workflow_trace_integrity", {}).get("status", "")).upper() != "READY"
    authenticity_stale = str(health.get("authenticity", {}).get("status", "")).upper() != "READY"
    if str(health["audit_status"]).upper() != "READY" or hardware_boundary_invalid or freshness_stale:
        actions.append(
            next_action(
                "run_project_audit",
                "Refresh audit evidence, freshness, and the expected no-board hardware validation boundary.",
                required_args=["run_name"],
                arg_sources={"run_name": "workflow.run_name or current implementation run"},
                preconditions=["Original Vivado project is open."],
                stop_condition="run_project_audit data.status is READY.",
            )
        )
    if (
        health["missing_required_categories"]
        or health["missing_files"]
        or health["hash_mismatches"]
        or health["size_mismatches"]
        or health["invalid_entries"]
        or health["resource_limits"]
        or health["path_escapes"]
        or str(health["audit_status"]).upper() != "READY"
        or hardware_boundary_invalid
        or freshness_stale
        or workflow_trace_missing
        or workflow_trace_integrity_stale
        or authenticity_stale
    ):
        actions.append(
            next_action(
                "collect_diagnostic_bundle",
                "Regenerate diagnostic evidence after fixing manifest, audit, or hardware-boundary issues.",
                required_args=["run_name"],
                arg_sources={"run_name": "workflow.run_name or current implementation run"},
                preconditions=["Original Vivado project is open."],
                stop_condition="collect_diagnostic_bundle writes a new diagnostic_manifest.json.",
            )
        )
        actions.append(
            next_action(
                "validate_diagnostic_bundle",
                "Validate the regenerated diagnostic manifest before handoff.",
                required_args=["manifest_path"],
                arg_sources={"manifest_path": "collect_diagnostic_bundle.data.manifest_path"},
                preconditions=["New diagnostic_manifest.json exists."],
                stop_condition="validate_diagnostic_bundle health is READY.",
            )
        )
    return dedupe_next_actions(actions)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
