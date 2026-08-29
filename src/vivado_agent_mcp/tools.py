from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from .result import failure, success
from .registry import (
    EXISTING_PROJECT_EXECUTION_TOOLS,
    IMMEDIATE_PROJECT_MUTATION_TOOLS,
    TOOL_PROFILE_ENV,
    UNATTESTED_COMPOSITE_EXECUTION_TOOLS,
    handler_name,
    input_schema_properties,
    profile_tool_names,
    resolve_tool_profile,
    tool_names as registry_tool_names,
    validate_tool_arguments,
)
from .vivado.artifacts import (
    artifact_run_blockers,
    collect_artifacts,
    load_artifact_manifest_with_sha256,
    resolve_artifact_manifest_for_read,
    sha256_file,
    validate_artifact_manifest,
)
from .vivado.agent_actions import next_action
from .vivado.audit import (
    apply_waivers_to_signoff,
    collect_diagnostic_bundle_files,
    create_waiver,
    evaluate_project_audit,
    load_waivers,
    remove_waiver,
    render_project_replay_script,
    waiver_path,
    write_project_replay_script,
)
from .vivado.block_design import (
    add_bd_ip_cell_command,
    connect_bd_intf_net_command,
    connect_bd_net_command,
    create_bd_port_command,
    create_block_design_command,
    generate_block_design_wrapper_command,
    open_block_design_command,
    parse_block_design_validation,
    parse_key_value_result,
    validate_block_design_command,
)
from .vivado.constraints import (
    METHODOLOGY_REPORT_BEGIN_MARKER,
    add_managed_xdc_command,
    check_timing_constraints_command,
    clock_summary_command,
    constraints_summary_command,
    drc_report_command,
    methodology_report_command,
    managed_xdc_payload,
    parse_check_timing_report,
    parse_clock_summary,
    parse_constraints_summary,
    parse_managed_xdc_result,
    parse_methodology_report,
    parse_qor_summary,
    parse_timing_closure_analysis,
    parse_timing_paths,
    qor_summary_command,
    timing_paths_command,
    timing_summary_command,
)
from .vivado.env import (
    capture_server_vivado_identity,
    find_vivado,
    resolve_runtime_dir,
    validate_trusted_vivado_executable,
)
from .vivado.evidence_store import (
    EvidenceSnapshot,
    StagedEvidenceFile,
    load_json_evidence,
    stage_verified_file,
    verify_staged_file,
)
from .vivado.hardware import (
    close_hardware_manager_command,
    close_hw_target_command,
    connect_hw_server_command,
    detect_hardware_environment,
    disconnect_hw_server_command,
    get_hw_device_status_command,
    hardware_messages_command,
    hardware_server_policy,
    list_hw_devices_command,
    list_hw_targets_command,
    open_hardware_manager_command,
    open_hw_target_command,
    parse_hardware_error_code,
    parse_hardware_messages,
    parse_hw_devices,
    parse_hw_targets,
    parse_programming_result,
    program_hw_device_command,
    select_hw_device_command,
    select_programming_artifacts,
    validate_bitstream_path,
    validate_ltx_path,
)
from .vivado.hardware_boundary import hardware_validation_boundary
from .vivado.managed_path import (
    ManagedPathError,
    atomic_write_bytes,
    delete_managed_snapshot,
    ensure_managed_directory,
    hold_managed_output_directories,
    hold_managed_paths_stable,
    read_stable_bytes,
    snapshot_managed_tree,
    validate_managed_path,
)
from .vivado.ip import (
    configure_ip_command,
    create_ip_command,
    export_ip_user_files_command,
    generate_ip_targets_command,
    ip_status_command,
    parse_ip_status,
    upgrade_ip_command,
)
from .vivado.parsers import (
    DRC_REPORT_BEGIN_MARKER,
    TIMING_SUMMARY_REPORT_BEGIN_MARKER,
    attest_report_text,
    parse_drc_report,
    parse_messages,
    parse_timing_summary,
    parse_utilization_report,
)
from .vivado.prehardware import (
    REPORT_CATEGORIES,
    analyze_sources_result,
    cdc_report_command,
    check_syntax_command,
    clock_interaction_report_command,
    collect_report_bundle_files,
    compile_order_command,
    configuration_voltage_command,
    design_hierarchy_command,
    elaboration_result_command,
    evaluate_pre_hw_signoff,
    parse_cdc_report,
    parse_clock_interaction_report,
    parse_compile_order,
    parse_configuration_voltage,
    parse_design_hierarchy,
    parse_elaboration_result,
    parse_power_report,
    parse_report_bundle_context,
    parse_syntax_report,
    power_report_command,
    report_bundle_command,
    run_elaboration_command,
)
from .vivado.project_capability import (
    create_project_capability,
    rebind_project_capability_generation,
    refresh_project_capability,
    verify_project_capability,
)
from .vivado.agent_catalog import (
    build_agent_scenarios,
    build_agent_workflows,
    build_tool_catalog,
    validate_diagnostic_bundle_manifest,
)
from .vivado.project import (
    add_project_files_command,
    compare_file_spec_inventories,
    file_spec_inventory_digest,
    list_fileset_files_command,
    normalize_project_file_specs,
    parse_fileset_files,
    parse_project_state,
    parse_run_refresh_rows,
    project_state_command,
    replay_file_specs_command,
    remove_project_files_command,
    set_project_part_command,
    set_project_top_command,
    update_compile_order_command,
)
from .vivado.readiness import evaluate_bitstream_readiness
from .vivado.runs import (
    ALLOWED_RUN_PROPERTIES,
    EXECUTABLE_CONSTRAINT_BLOCK_MARKER,
    EXECUTABLE_INPUT_DISCOVERY_BLOCK_MARKER,
    MAX_TRUSTED_XDC_BYTES,
    RUN_HOOK_BLOCK_MARKER,
    VIVADO_VERSION_BLOCK_MARKER,
    artifact_context_command,
    blocked_constraint_file_inputs,
    clean_run_outputs_command,
    configure_run_command,
    guarded_launch_run_command,
    get_run_configuration_command,
    parse_clean_outputs,
    parse_run_configuration,
    parse_run_rows,
    parse_project_execution_inputs,
    project_execution_inputs_command,
    reset_runs_command,
    run_hook_guard_command,
    validate_generated_child_name,
    validate_generated_clean_target,
    validate_run_properties,
    validate_xdc_text,
)
from .vivado.runtime_cache import (
    clean_runtime_cache as clean_runtime_cache_data,
    get_runtime_cache_status as get_runtime_cache_status_data,
)
from .vivado.session import (
    DEFAULT_START_TIMEOUT_S,
    RECOMMENDED_RETRY_TIMEOUT_S,
    GuiTcpVivadoSession,
    PROJECT_ACTIVE_IDENTITY_MISMATCH_MARKER,
    SessionManager,
    SessionTaintedError,
)
from .vivado.simulation import (
    DEFAULT_MAX_VCD_MB,
    analyze_testbench_waveform_paths,
    behavioral_simulation_command,
    build_simulation_source_identity,
    build_design_execution_identity,
    build_simulation_stable_input_identity,
    configure_simulation_command,
    managed_simulation_policy_preflight_command,
    parse_simulation_vcd_preflight,
    parse_simulation_result,
    simulation_result_read_command,
    simulation_vcd_preflight_command,
    validate_simulation_defines,
    validate_simulation_trust_closure,
)
from .vivado.tcl import safe_tcl, tcl_list_quote
from .vivado.tcl_policy import (
    raw_tcl_programming_command,
    tcl_dry_run_data,
    tcl_policy_allows,
    tcl_policy_failure_data,
)
from .vivado.wire import decode_wire_list, decode_wire_row, tcl_wire_prelude
from .vivado.workflow_trace import WorkflowTracer


REUSABLE_AUDIT_MAX_AGE_HOURS = 24
MAX_REPORT_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_REPORT_FILE_BYTES = 64 * 1024 * 1024
MAX_DIAGNOSTIC_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_REUSABLE_AUDIT_BYTES = 16 * 1024 * 1024
RUN_LAUNCH_TRANSITION_TIMEOUT_S = 60.0
TRUSTED_SIMULATION_ROOTS_ENV = "VIVADO_AGENT_MCP_TRUSTED_PROJECT_ROOTS"
TRUSTED_SIMULATION_CONFIRM = "RUN_TRUSTED_XSIM"


class VivadoToolService:
    def __init__(
        self,
        manager: SessionManager | None = None,
        session: Any | None = None,
        tracer: WorkflowTracer | None = None,
        tool_profile: str | None = None,
        enforce_tool_profile: bool = False,
    ) -> None:
        self.manager = manager or SessionManager()
        manager_identity = getattr(self.manager, "trusted_vivado_identity", None)
        self._trusted_vivado_identity = (
            dict(manager_identity) if isinstance(manager_identity, dict) else capture_server_vivado_identity()
        )
        self._session_override = session
        self.tracer = tracer or WorkflowTracer()
        self._run_launches: dict[str, dict[str, Any]] = {}
        self._project_mutation_scope = "unbound"
        self._mcp_created_project_capabilities: dict[str, dict[str, Any]] = {}
        self._active_project_capability: dict[str, Any] | None = None
        self.tool_profile = resolve_tool_profile(tool_profile)
        self.enforce_tool_profile = enforce_tool_profile
        self._profile_tool_names = set(profile_tool_names(self.tool_profile))

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments if arguments is not None else {}
        started_at = datetime.now(UTC)
        handler = self._handlers().get(name)
        if not handler:
            result = failure(name, "UNKNOWN_TOOL", f"Unknown tool: {name}")
            trace_error = self._record_trace(name, args, result, started_at)
            return _trace_write_failure(name, result, trace_error) if trace_error else result
        argument_issues = validate_tool_arguments(name, args)
        if argument_issues:
            result = failure(
                name,
                "INVALID_TOOL_ARGUMENTS",
                "Tool arguments do not satisfy the registered MCP input schema.",
                data={
                    "validation_errors": argument_issues,
                    "handler_executed": False,
                    "next_actions": [
                        next_action(
                            "get_tool_catalog",
                            "Inspect the registered input schema before retrying the tool.",
                            stop_condition="The retry contains only declared arguments with valid JSON types.",
                        )
                    ],
                },
            )
            trace_error = self._record_trace(name, args if isinstance(args, dict) else {}, result, started_at)
            return _trace_write_failure(name, result, trace_error) if trace_error else result
        if self.enforce_tool_profile and name not in self._profile_tool_names:
            result = failure(
                name,
                "TOOL_NOT_AVAILABLE_IN_PROFILE",
                f"Tool {name!r} is not available in the active {self.tool_profile!r} MCP profile.",
                data={
                    "active_tool_profile": self.tool_profile,
                    "profile_env": TOOL_PROFILE_ENV,
                    "next_actions": [
                        next_action(
                            "get_tool_catalog",
                            "Inspect tools available in the active server profile.",
                            stop_condition="The Agent selects an available core tool or the server administrator explicitly changes profile.",
                        )
                    ],
                },
            )
            trace_error = self._record_trace(name, args, result, started_at)
            return _trace_write_failure(name, result, trace_error) if trace_error else result
        if "vivado_path" in args and "vivado_path" in input_schema_properties(name):
            path_assertion = validate_trusted_vivado_executable(
                args.get("vivado_path"),
                trusted_identity=self._trusted_vivado_identity,
            )
            if not path_assertion.get("ok"):
                result = failure(
                    name,
                    str(path_assertion.get("error_code") or "VIVADO_PATH_MISMATCH"),
                    str(path_assertion.get("message") or "Vivado path assertion failed."),
                    data={
                        **path_assertion,
                        "handler_executed": False,
                        "path_assertion_checked": True,
                        "next_actions": [
                            next_action(
                                "detect_vivado_environment",
                                "Inspect the immutable server-start VIVADO_PATH identity before retrying without a path override.",
                                stop_condition=(
                                    "detect_vivado_environment reports the configured canonical path, or the MCP server "
                                    "must be restarted after correcting VIVADO_PATH."
                                ),
                            )
                        ],
                    },
                )
                trace_error = self._record_trace(name, args, result, started_at)
                return _trace_write_failure(name, result, trace_error) if trace_error else result
        guarded_project_action = name in (
            IMMEDIATE_PROJECT_MUTATION_TOOLS
            | EXISTING_PROJECT_EXECUTION_TOOLS
            | UNATTESTED_COMPOSITE_EXECUTION_TOOLS
        ) and name != "create_project"
        repair_dry_run = name == "repair_project_setup" and bool(args.get("dry_run", True))
        managed_transport = False
        project_guard = nullcontext()
        if guarded_project_action and not repair_dry_run:
            try:
                managed_transport = isinstance(self._session(), GuiTcpVivadoSession)
            except Exception:  # noqa: BLE001 - the handler will return the canonical session error.
                managed_transport = False
            if managed_transport and self._project_mutation_scope == "mcp_created_project":
                try:
                    if not self._active_project_capability:
                        raise ManagedPathError("active project capability is missing")
                    verify_project_capability(
                        self._active_project_capability,
                        self._active_project_capability["project_path"],
                        generation_id=str(getattr(self._session(), "generation_id", "")),
                        verify_project_content=False,
                    )
                    project_guard = self._session().require_current_project(
                        self._active_project_capability["project_path"]
                    )
                except (ManagedPathError, OSError, ValueError) as exc:
                    self._project_mutation_scope = "indeterminate"
                    result = failure(
                        name,
                        "PROJECT_CAPABILITY_INVALID",
                        "The MCP-created project capability no longer identifies the original project object.",
                        data={"reason": str(exc), "mutation_scope": self._project_mutation_scope, "handler_executed": False},
                    )
                    trace_error = self._record_trace(name, args, result, started_at)
                    return _trace_write_failure(name, result, trace_error) if trace_error else result
            if managed_transport and self._project_mutation_scope != "mcp_created_project":
                result = failure(
                    name,
                    "EXISTING_PROJECT_MUTATION_REQUIRES_WORKING_COPY",
                    "Immediate mutation is disabled for projects opened as existing projects; rebuild a separate MCP-managed working project first.",
                    data={
                        "mutation_scope": self._project_mutation_scope,
                        "original_project_protected": True,
                        "blocked_tool_class": (
                            "project_mutation_immediate"
                            if name in IMMEDIATE_PROJECT_MUTATION_TOOLS
                            else "project_execution"
                        ),
                        "next_actions": _existing_project_rebuild_actions(),
                    },
                )
                trace_error = self._record_trace(name, args, result, started_at)
                return _trace_write_failure(name, result, trace_error) if trace_error else result
            if managed_transport and name in UNATTESTED_COMPOSITE_EXECUTION_TOOLS:
                result = failure(
                    name,
                    "EXECUTABLE_COMPOSITE_INPUT_UNSUPPORTED",
                    "This IP/Block Design operation is disabled because its executable dependencies are not yet attested by the Vivado 2021.2 trusted-input closure.",
                    data={
                        "handler_executed": False,
                        "stop_required": True,
                        "unsupported_input_classes": [
                            "IP repository",
                            "IP definition",
                            "XCI",
                            "IP-delivered XDC",
                            "Block Design",
                            "OOC run",
                        ],
                        "next_actions": [
                            next_action(
                                "get_project_state",
                                "Inspect the project without executing or generating unattested composite inputs.",
                                stop_condition="The project contains only reviewed RTL, declarative XDC, and supported simulation sources.",
                            )
                        ],
                    },
                )
                trace_error = self._record_trace(name, args, result, started_at)
                return _trace_write_failure(name, result, trace_error) if trace_error else result
        try:
            with project_guard:
                if managed_transport and name in EXISTING_PROJECT_EXECUTION_TOOLS:
                    preflight_command = run_hook_guard_command()
                    preflight = self._session().run_tcl(
                        preflight_command,
                        timeout_s=min(int(args.get("timeout_s", 60) or 60), 60),
                    )
                    preflight["command"] = preflight_command
                    if not preflight.get("ok"):
                        result = _tcl_failure(name, preflight)
                        result.setdefault("data", {})["execution_preflight_only"] = True
                    else:
                        result = handler(args)
                else:
                    result = handler(args)
        except TimeoutError as exc:
            result = self._timeout_failure(
                name,
                exc,
                timeout_s=int(args.get("timeout_s", 60) or 60),
                request_context=dict(args),
            )
        except SessionTaintedError as exc:
            result = failure(
                name,
                "SESSION_TAINTED",
                str(exc),
                data={
                    **exc.data,
                    "next_actions": [
                        next_action(
                            "start_session",
                            "Start a new Vivado generation because the timed-out Tcl command had an indeterminate outcome.",
                            preconditions=["The tainted managed process tree has been terminated."],
                            stop_condition="start_session returns a new generation_id and session_state=READY.",
                        )
                    ],
                },
            )
        except Exception as exc:  # noqa: BLE001 - MCP tools must serialize errors.
            result = failure(name, exc.__class__.__name__, str(exc))
        if result.get("error_code") == "PROJECT_ACTIVE_IDENTITY_MISMATCH":
            self._project_mutation_scope = "indeterminate"
            self._active_project_capability = None
            result.setdefault("data", {}).update(
                {
                    "mutation_scope": self._project_mutation_scope,
                    "handler_executed": False,
                    "stop_required": True,
                }
            )
        trace_error = self._record_trace(name, args, result, started_at)
        return _trace_write_failure(name, result, trace_error) if trace_error else result

    def _record_trace(self, name: str, args: dict[str, Any], result: dict[str, Any], started_at: datetime) -> str:
        try:
            self.tracer.record(tool=name, args=args, result=result, started_at=started_at, ended_at=datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001 - trace failure is converted to a structured MCP result.
            return f"{exc.__class__.__name__}: {exc}"
        return ""

    def tool_names(self) -> list[str]:
        return sorted(self._profile_tool_names) if self.enforce_tool_profile else registry_tool_names()

    def _handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            name: getattr(self, handler_name(name))
            for name in registry_tool_names()
        }

    def _session(self) -> Any:
        if self._session_override is not None:
            return self._session_override
        return self.manager.current()

    def _find_vivado(
        self,
        requested_path: str | None = None,
        *,
        probe_launch: bool = False,
        probe_timeout_s: int = 15,
        runtime_dir: str | None = None,
    ) -> dict[str, Any]:
        return find_vivado(
            requested_path,
            probe_launch=probe_launch,
            probe_timeout_s=probe_timeout_s,
            runtime_dir=runtime_dir,
            trusted_identity=self._trusted_vivado_identity,
        )

    def cancel_active_operation(self) -> dict[str, Any]:
        """Best-effort cancellation boundary used by the async MCP dispatcher."""

        try:
            if self._session_override is not None and hasattr(self._session_override, "stop"):
                result = self._session_override.stop()
            elif self._session_override is not None:
                result = {"ok": True, "stopped": False, "backend": "override", "reason": "stop_not_supported"}
            else:
                result = self.manager.stop()
        except Exception as exc:  # noqa: BLE001 - cancellation must remain best-effort and serializable.
            result = {
                "ok": False,
                "error_code": "ACTIVE_OPERATION_CANCEL_FAILED",
                "message": f"{exc.__class__.__name__}: {exc}",
            }
        finally:
            self._project_mutation_scope = "unbound"
            self._mcp_created_project_capabilities.clear()
            self._active_project_capability = None
            self._run_launches.clear()
        return result if isinstance(result, dict) else {"ok": False, "error_code": "ACTIVE_OPERATION_CANCEL_INVALID_RESULT"}

    def _run_execution_tcl(self, command: str, *, timeout_s: int) -> dict[str, Any]:
        session = self._session()
        guarded_command = command
        if isinstance(session, GuiTcpVivadoSession) and EXECUTABLE_CONSTRAINT_BLOCK_MARKER not in command:
            guarded_command = f"{run_hook_guard_command()}; {command}"
        result = session.run_tcl(guarded_command, timeout_s=timeout_s)
        result["command"] = guarded_command
        return result

    def _capture_design_execution_identity(self, *, timeout_s: int) -> dict[str, Any]:
        session = self._session()
        guard = nullcontext()
        if isinstance(session, GuiTcpVivadoSession):
            try:
                if self._project_mutation_scope != "mcp_created_project" or not self._active_project_capability:
                    raise ManagedPathError("managed project capability is unavailable for design identity capture")
                verify_project_capability(
                    self._active_project_capability,
                    self._active_project_capability["project_path"],
                    generation_id=str(getattr(session, "generation_id", "")),
                    verify_project_content=False,
                )
                guard = session.require_current_project(self._active_project_capability["project_path"])
            except (ManagedPathError, OSError, ValueError) as exc:
                return failure(
                    "design_execution_identity",
                    "PROJECT_CAPABILITY_INVALID",
                    str(exc),
                    data={"mutation_scope": self._project_mutation_scope},
                )
        command = project_execution_inputs_command()
        with guard:
            raw = session.run_tcl(command, timeout_s=min(timeout_s, 60))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("design_execution_identity", raw)
        parsed = parse_project_execution_inputs(str(raw.get("raw", "")))
        identity = build_design_execution_identity(parsed)
        if identity.get("status") != "READY":
            return failure(
                "design_execution_identity",
                "DESIGN_EXECUTION_IDENTITY_BLOCKED",
                "Design source/constraint/include/run configuration identity is incomplete.",
                data={"identity": identity, "command": command},
            )
        return success(
            "design_execution_identity",
            "Design execution identity captured.",
            {"design_execution_identity": identity, "command": command},
        )

    def _evidence_design_execution_identity(
        self,
        *,
        run_name: str,
        timeout_s: int,
        require_terminal_launch: bool,
    ) -> dict[str, Any]:
        session = self._session()
        if isinstance(session, GuiTcpVivadoSession):
            captured = self._capture_design_execution_identity(timeout_s=timeout_s)
            if not captured.get("ok"):
                return captured
            identity = captured["data"]["design_execution_identity"]
            launch = self._run_launches.get(run_name)
            if require_terminal_launch:
                if not launch or launch.get("operation") != "bitstream":
                    return failure(
                        "design_execution_identity",
                        "RUN_LAUNCH_IDENTITY_MISSING",
                        "Artifact/report handoff requires the tracked bitstream launch identity from this MCP session.",
                        data={"run_name": run_name, "stop_required": True},
                    )
                launched = launch.get("design_execution_identity")
                terminal = launch.get("terminal_design_execution_identity")
                if not isinstance(launched, dict) or not isinstance(terminal, dict):
                    return failure(
                        "design_execution_identity",
                        "RUN_TERMINAL_IDENTITY_MISSING",
                        "Poll get_run_progress to terminal completion before collecting artifact/report handoff evidence.",
                        data={"run_name": run_name, "run_launch": launch, "stop_required": True},
                    )
                expected_sha256 = str(identity.get("sha256", ""))
                if any(str(item.get("sha256", "")) != expected_sha256 for item in (launched, terminal)):
                    return failure(
                        "design_execution_identity",
                        "SOURCE_CLOSURE_CHANGED",
                        "Current design closure no longer matches the launch and terminal run identities.",
                        data={
                            "run_name": run_name,
                            "launch_design_execution_identity": launched,
                            "terminal_design_execution_identity": terminal,
                            "current_design_execution_identity": identity,
                            "stop_required": True,
                        },
                    )
            return success(
                "design_execution_identity",
                "Design execution identity is ready for evidence collection.",
                {"design_execution_identity": identity},
            )
        identity = getattr(session, "design_execution_identity", None)
        if isinstance(identity, dict) and identity.get("status") == "READY":
            return success(
                "design_execution_identity",
                "Injected test-session design execution identity accepted.",
                {"design_execution_identity": identity},
            )
        return failure(
            "design_execution_identity",
            "DESIGN_EXECUTION_IDENTITY_BLOCKED",
            "The session did not provide a design execution identity.",
            data={"run_name": run_name, "stop_required": True},
        )

    def _safe_session_status(self) -> dict[str, Any]:
        try:
            if self._session_override is not None and hasattr(self._session_override, "status"):
                status = self._session_override.status()
            else:
                status = self.manager.status()
            return status if isinstance(status, dict) else {"ok": False, "status_error": "session status is not a dict"}
        except Exception as exc:  # noqa: BLE001 - status must remain diagnostic-only.
            return {"ok": False, "status_error": exc.__class__.__name__, "message": str(exc)}

    def _timeout_failure(
        self,
        tool: str,
        error: TimeoutError,
        *,
        timeout_s: int,
        command: str = "",
        request_context: dict[str, Any] | None = None,
        partial_context: dict[str, Any] | None = None,
        next_actions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session_status = self._safe_session_status()
        data = _tcl_timeout_data(
            tool=tool,
            error=error,
            timeout_s=timeout_s,
            command=command,
            request_context=request_context or {},
            session_status=session_status,
            partial_context=partial_context or {},
            next_actions=next_actions,
        )
        return failure(tool, "TimeoutError", str(error) or f"{tool} timed out.", data=data)

    def _get_tool_catalog(self, _: dict[str, Any]) -> dict[str, Any]:
        data = build_tool_catalog(self.tool_names())
        data["active_tool_profile"] = self.tool_profile if self.enforce_tool_profile else "internal_full_registry"
        data["profile_enforced"] = self.enforce_tool_profile
        data["profile_env"] = TOOL_PROFILE_ENV
        data["hidden_tool_count"] = len(registry_tool_names()) - len(self.tool_names())
        return success("get_tool_catalog", "Vivado Agent MCP tool catalog collected.", data)

    def _get_agent_workflows(self, _: dict[str, Any]) -> dict[str, Any]:
        data = build_agent_workflows(self.tool_names())
        return success("get_agent_workflows", "Vivado Agent MCP workflow recipes collected.", data)

    def _get_agent_scenarios(self, args: dict[str, Any]) -> dict[str, Any]:
        scenario_id = str(args.get("scenario_id", "") or "")
        data = build_agent_scenarios(self.tool_names(), scenario_id=scenario_id)
        if scenario_id and not data["scenarios"]:
            return failure("get_agent_scenarios", "AGENT_SCENARIO_NOT_FOUND", "Vivado Agent MCP scenario not found.", data=data)
        return success("get_agent_scenarios", "Vivado Agent MCP validation scenarios collected.", data)

    def _get_workflow_trace_status(self, _: dict[str, Any]) -> dict[str, Any]:
        data = self.tracer.status()
        return success("get_workflow_trace_status", "Vivado Agent MCP workflow trace status collected.", data)

    def _detect_vivado_environment(self, args: dict[str, Any]) -> dict[str, Any]:
        data = self._find_vivado(
            args.get("vivado_path"),
            probe_launch=bool(args.get("probe_launch", False)),
            probe_timeout_s=int(args.get("probe_timeout_s", 15) or 15),
            runtime_dir=args.get("runtime_dir"),
        )
        if not data.get("ok"):
            return failure(
                "detect_vivado_environment",
                str(data.get("error_code") or "VIVADO_ENVIRONMENT_UNAVAILABLE"),
                str(data.get("message") or "Vivado environment detection failed."),
                data=data,
            )
        return success("detect_vivado_environment", "Vivado environment detected.", data)

    def _detect_hardware_environment(self, args: dict[str, Any]) -> dict[str, Any]:
        data = detect_hardware_environment(
            args.get("vivado_path"),
            trusted_identity=self._trusted_vivado_identity,
        )
        if not data.get("ok"):
            return failure(
                "detect_hardware_environment",
                str(data.get("error_code") or "HARDWARE_ENVIRONMENT_UNAVAILABLE"),
                str(data.get("message") or "Vivado hardware environment detection failed."),
                data=data,
            )
        return success("detect_hardware_environment", "Vivado hardware environment detected.", data)

    def _start_session(self, args: dict[str, Any]) -> dict[str, Any]:
        self._project_mutation_scope = "unbound"
        self._active_project_capability = None
        data = self.manager.start(
            vivado_path=args.get("vivado_path"),
            port=int(args.get("port", 0) or 0),
            timeout_s=int(args.get("timeout_s", DEFAULT_START_TIMEOUT_S)),
            runtime_dir=args.get("runtime_dir"),
        )
        if not data.get("ok"):
            data["next_actions"] = _start_session_failure_actions(data)
            return failure("start_session", data.get("error_code", "START_FAILED"), data.get("message", ""), data=data)
        data["workflow_trace_storage"] = _workflow_trace_storage_data(self.tracer)
        data["recoverable_managed_project_count"] = len(self._mcp_created_project_capabilities)
        return success("start_session", "Vivado GUI session started.", data)

    def _stop_session(self, _: dict[str, Any]) -> dict[str, Any]:
        capability_checkpoint = self._checkpoint_active_project_before_session_stop()
        data = self.manager.stop()
        data["project_capability_checkpoint"] = capability_checkpoint
        if not data.get("ok"):
            data["next_actions"] = [
                next_action(
                    "session_status",
                    "Inspect the managed Vivado process before retrying stop_session.",
                    required_args=[],
                    arg_sources={},
                    preconditions=["The failed managed session handle is still retained."],
                    stop_condition="session_status confirms the process state, then stop_session is retried or the process is terminated externally.",
                ),
                next_action(
                    "stop_session",
                    "Retry managed process-tree termination after inspecting the failure details.",
                    required_args=[],
                    arg_sources={},
                    preconditions=["Review stop_session.data.termination and confirm the PID belongs to this MCP session."],
                    stop_condition="stop_session returns stopped=true and process_running=false.",
                ),
            ]
            return failure(
                "stop_session",
                str(data.get("error_code", "SESSION_STOP_FAILED")),
                str(data.get("message", "Vivado session could not be stopped.")),
                data=data,
            )
        self._project_mutation_scope = "unbound"
        self._active_project_capability = None
        data["next_actions"] = [
            next_action(
                "get_runtime_cache_status",
                "Inspect the Vivado MCP runtime directory after stopping the session.",
                required_args=[],
                arg_sources={"runtime_dir": "stop_session.data.runtime_dir if present, otherwise default runtime"},
                preconditions=["Vivado MCP managed session has been stopped."],
                stop_condition="Runtime cache status is available for dry-run cleanup planning.",
                optional=True,
            )
        ]
        return success("stop_session", "Vivado session stopped.", data)

    def _checkpoint_active_project_before_session_stop(self) -> dict[str, Any]:
        capability = self._active_project_capability
        if not isinstance(capability, dict):
            return {
                "status": "NOT_APPLICABLE",
                "attempted": False,
                "project_closed": False,
                "capability_refreshed": False,
                "capability_invalidated": False,
                "project_path": "",
                "reason": "no_active_mcp_created_project",
            }
        project_path = str(capability.get("project_path", ""))
        project_key = str(capability.get("project_path_key", ""))
        try:
            close_result = self._close_project({"timeout_s": 30})
        except Exception as exc:  # noqa: BLE001 - process termination must continue after checkpoint failure.
            self._mcp_created_project_capabilities.pop(project_key, None)
            self._active_project_capability = None
            self._project_mutation_scope = "unbound"
            return {
                "status": "BLOCK",
                "attempted": True,
                "project_closed": False,
                "capability_refreshed": False,
                "capability_invalidated": True,
                "project_path": project_path,
                "error_code": exc.__class__.__name__,
                "reason": f"{exc.__class__.__name__}: {exc}",
            }
        if close_result.get("ok"):
            refreshed = self._mcp_created_project_capabilities.get(project_key)
            if not isinstance(refreshed, dict):
                self._mcp_created_project_capabilities.pop(project_key, None)
                return {
                    "status": "BLOCK",
                    "attempted": True,
                    "project_closed": True,
                    "capability_refreshed": False,
                    "capability_invalidated": True,
                    "project_path": project_path,
                    "error_code": "PROJECT_CAPABILITY_CHECKPOINT_MISSING",
                    "reason": "Managed project closed without a refreshed capability checkpoint.",
                }
            return {
                "status": "READY",
                "attempted": True,
                "project_closed": True,
                "capability_refreshed": True,
                "capability_invalidated": False,
                "project_path": project_path,
                "project_file_sha256": str(refreshed.get("project_file_sha256", "")),
                "reason": "managed_project_closed_and_checkpointed",
            }
        self._mcp_created_project_capabilities.pop(project_key, None)
        self._active_project_capability = None
        self._project_mutation_scope = "unbound"
        return {
            "status": "BLOCK",
            "attempted": True,
            "project_closed": bool(close_result.get("data", {}).get("project_closed", False)),
            "capability_refreshed": False,
            "capability_invalidated": True,
            "project_path": project_path,
            "error_code": str(close_result.get("error_code", "PROJECT_CAPABILITY_CHECKPOINT_FAILED")),
            "reason": str(close_result.get("message", "Managed project capability checkpoint failed.")),
        }

    def _session_status(self, _: dict[str, Any]) -> dict[str, Any]:
        return success("session_status", "Vivado session status collected.", self._session_status_data())

    def _get_runtime_cache_status(self, args: dict[str, Any]) -> dict[str, Any]:
        data = get_runtime_cache_status_data(args.get("runtime_dir"))
        if data.get("exists"):
            summary = f"Runtime cache status collected: {data.get('file_count', 0)} file(s), {data.get('total_bytes', 0)} byte(s)."
        else:
            summary = "Runtime cache directory does not exist."
        return success("get_runtime_cache_status", summary, data)

    def _clean_runtime_cache(self, args: dict[str, Any]) -> dict[str, Any]:
        dry_run = bool(args.get("dry_run", True))
        if not dry_run:
            status = self.manager.status()
            if status.get("connected") or status.get("process_running"):
                runtime_dir = args.get("runtime_dir") or status.get("runtime_dir") or status.get("temp_dir") or ""
                data = {
                    "status": "BLOCK",
                    "reason": "session_active",
                    "runtime_dir": str(runtime_dir),
                    "dry_run": False,
                    "session_status": status,
                    "next_actions": [
                        next_action(
                            "stop_session",
                            "Stop the managed Vivado session before deleting runtime cache files.",
                            preconditions=["A managed Vivado session is connected or still running."],
                            stop_condition="stop_session reports the session is stopped.",
                        )
                    ],
                }
                return failure(
                    "clean_runtime_cache",
                    "RUNTIME_SESSION_ACTIVE",
                    "Runtime cleanup requires the managed Vivado session to be stopped.",
                    data=data,
                )

        data = clean_runtime_cache_data(
            args.get("runtime_dir"),
            dry_run=dry_run,
            max_age_hours=float(args.get("max_age_hours", 0) or 0),
            include_unknown=bool(args.get("include_unknown", False)),
            runtime_identity=str(args.get("runtime_identity", "")),
            plan_sha256=str(args.get("plan_sha256", "")),
            execution_intent=str(args.get("execution_intent", "")),
            confirm=str(args.get("confirm", "")),
        )
        if data.get("status") == "BLOCK":
            reason = str(data.get("reason", "runtime_cleanup_blocked"))
            error_code = {
                "runtime_dir_looks_like_project": "RUNTIME_DIR_REJECTED",
                "runtime_dir_is_filesystem_root": "RUNTIME_DIR_REJECTED",
                "runtime_dir_is_protected_root": "RUNTIME_DIR_REJECTED",
                "runtime_dir_contains_protected_root": "RUNTIME_DIR_REJECTED",
                "runtime_dir_looks_like_repository": "RUNTIME_DIR_REJECTED",
                "runtime_identity_missing": "RUNTIME_IDENTITY_REQUIRED",
                "runtime_identity_invalid": "RUNTIME_IDENTITY_INVALID",
                "runtime_identity_marker_not_regular_file": "RUNTIME_IDENTITY_INVALID",
                "runtime_identity_marker_unreadable": "RUNTIME_IDENTITY_INVALID",
                "runtime_identity_marker_not_object": "RUNTIME_IDENTITY_INVALID",
                "runtime_identity_schema_mismatch": "RUNTIME_IDENTITY_INVALID",
                "runtime_identity_id_invalid": "RUNTIME_IDENTITY_INVALID",
                "runtime_identity_path_invalid": "RUNTIME_IDENTITY_INVALID",
                "runtime_identity_path_mismatch": "RUNTIME_IDENTITY_INVALID",
                "runtime_identity_confirmation_required": "RUNTIME_IDENTITY_CONFIRMATION_REQUIRED",
                "runtime_identity_mismatch": "RUNTIME_IDENTITY_MISMATCH",
                "unknown_cleanup_requires_configured_runtime": "RUNTIME_UNKNOWN_CLEANUP_REJECTED",
                "unknown_cleanup_intent_required": "RUNTIME_UNKNOWN_CLEANUP_INTENT_REQUIRED",
                "unknown_cleanup_confirmation_required": "RUNTIME_UNKNOWN_CLEANUP_CONFIRMATION_REQUIRED",
                "vivado_process_active": "RUNTIME_PROCESS_ACTIVE",
                "process_detection_unavailable": "RUNTIME_PROCESS_DETECTION_UNAVAILABLE",
            }.get(reason, "RUNTIME_CLEANUP_BLOCKED")
            return failure("clean_runtime_cache", error_code, f"Runtime cleanup blocked: {reason}.", data=data)

        status_text = str(data.get("status", "")).lower() or "completed"
        planned = data.get("planned", {}) if isinstance(data.get("planned"), dict) else {}
        return success(
            "clean_runtime_cache",
            f"Runtime cache cleanup {status_text}: {planned.get('file_count', 0)} candidate file(s).",
            data,
        )

    def _open_hardware_manager(self, args: dict[str, Any]) -> dict[str, Any]:
        blocked = _hardware_mode_disabled("open_hardware_manager", args)
        if blocked:
            return blocked
        command = open_hardware_manager_command()
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _hardware_tcl_failure("open_hardware_manager", raw)
        return _hardware_success("open_hardware_manager", "Vivado Hardware Manager opened.", raw)

    def _close_hardware_manager(self, args: dict[str, Any]) -> dict[str, Any]:
        blocked = _hardware_mode_disabled("close_hardware_manager", args)
        if blocked:
            return blocked
        command = close_hardware_manager_command()
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _hardware_tcl_failure("close_hardware_manager", raw)
        return _hardware_success("close_hardware_manager", "Vivado Hardware Manager closed.", raw)

    def _connect_hw_server(self, args: dict[str, Any]) -> dict[str, Any]:
        blocked = _hardware_mode_disabled("connect_hw_server", args)
        if blocked:
            return blocked
        host = str(args.get("host", "localhost"))
        port = int(args.get("port", 3121))
        command = connect_hw_server_command(host=host, port=port)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        raw["connection"] = {"host": host, "port": port}
        if not raw.get("ok"):
            return _hardware_tcl_failure("connect_hw_server", raw)
        return _hardware_success("connect_hw_server", f"Connected to hw_server at {host}:{port}.", raw)

    def _disconnect_hw_server(self, args: dict[str, Any]) -> dict[str, Any]:
        blocked = _hardware_mode_disabled("disconnect_hw_server", args)
        if blocked:
            return blocked
        command = disconnect_hw_server_command()
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _hardware_tcl_failure("disconnect_hw_server", raw)
        return _hardware_success("disconnect_hw_server", "Disconnected from hw_server.", raw)

    def _list_hw_targets(self, args: dict[str, Any]) -> dict[str, Any]:
        blocked = _hardware_mode_disabled("list_hw_targets", args)
        if blocked:
            return blocked
        command = list_hw_targets_command()
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _hardware_tcl_failure("list_hw_targets", raw)
        data = parse_hw_targets(raw.get("raw", ""))
        data["command"] = command
        return _hardware_success("list_hw_targets", f"Found {data['count']} hardware target(s).", data)

    def _open_hw_target(self, args: dict[str, Any]) -> dict[str, Any]:
        blocked = _hardware_mode_disabled("open_hw_target", args)
        if blocked:
            return blocked
        command = open_hw_target_command(
            target=args.get("target"),
            index=int(args.get("index", 0)),
        )
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _hardware_tcl_failure("open_hw_target", raw)
        data = parse_hw_targets(raw.get("raw", ""))
        data["command"] = command
        return _hardware_success("open_hw_target", "Hardware target opened.", data)

    def _close_hw_target(self, args: dict[str, Any]) -> dict[str, Any]:
        blocked = _hardware_mode_disabled("close_hw_target", args)
        if blocked:
            return blocked
        command = close_hw_target_command(target=args.get("target"))
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _hardware_tcl_failure("close_hw_target", raw)
        data = parse_hw_targets(raw.get("raw", ""))
        data["command"] = command
        return _hardware_success("close_hw_target", "Hardware target closed.", data)

    def _list_hw_devices(self, args: dict[str, Any]) -> dict[str, Any]:
        blocked = _hardware_mode_disabled("list_hw_devices", args)
        if blocked:
            return blocked
        command = list_hw_devices_command()
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _hardware_tcl_failure("list_hw_devices", raw)
        data = parse_hw_devices(raw.get("raw", ""))
        data["command"] = command
        return _hardware_success("list_hw_devices", f"Found {data['count']} hardware device(s).", data)

    def _select_hw_device(self, args: dict[str, Any]) -> dict[str, Any]:
        blocked = _hardware_mode_disabled("select_hw_device", args)
        if blocked:
            return blocked
        command = select_hw_device_command(
            device=args.get("device"),
            part=args.get("part"),
            index=int(args["index"]) if "index" in args else None,
        )
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _hardware_tcl_failure("select_hw_device", raw)
        data = parse_programming_result(raw.get("raw", ""))
        data["command"] = command
        return _hardware_success("select_hw_device", "Hardware device selected.", data)

    def _program_hw_device(self, args: dict[str, Any]) -> dict[str, Any]:
        bitstream, ltx, error = _validated_programming_paths(
            tool="program_hw_device",
            bitstream_path=args.get("bitstream_path"),
            ltx_path=args.get("ltx_path"),
        )
        if error:
            return error
        gate = self._hardware_programming_gate(
            tool="program_hw_device",
            args=args,
            bitstream=bitstream,
        )
        if not gate.get("ok"):
            return gate
        staged_bitstream, staged_error = _staged_programming_bitstream("program_hw_device", gate)
        if staged_error:
            return staged_error
        command = program_hw_device_command(
            bitstream_path=str(staged_bitstream.path),
            ltx_path=str(ltx) if ltx else None,
            device=args.get("device"),
            target=args.get("target"),
        )
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 300)))
        raw["command"] = command
        if not raw.get("ok"):
            return _hardware_tcl_failure("program_hw_device", raw)
        data = parse_programming_result(raw.get("raw", ""))
        data["command"] = command
        data["artifacts"] = {"bitstream_path": str(bitstream), "ltx_path": str(ltx) if ltx else None}
        data["programming_gate"] = gate["data"]
        return _hardware_success("program_hw_device", f"Hardware device programming {data['status']}.", data)

    def _program_from_artifact_manifest(self, args: dict[str, Any]) -> dict[str, Any]:
        manifest_path = Path(str(args["manifest_path"]))
        if not manifest_path.exists():
            return failure(
                "program_from_artifact_manifest",
                "MANIFEST_NOT_FOUND",
                f"Artifact manifest not found: {manifest_path}",
                data=_with_hardware_validation({"manifest_path": str(manifest_path)}),
            )
        intent_failure = _manifest_programming_intent_failure(args, manifest_path)
        if intent_failure is not None:
            return intent_failure
        mode_failure = _hardware_mode_disabled("program_from_artifact_manifest", args)
        if mode_failure is not None:
            return mode_failure
        validated_result = self._load_validated_artifact_manifest(
            args,
            tool="program_from_artifact_manifest",
        )
        if not validated_result.get("ok"):
            rejected_data = _with_hardware_validation(dict(validated_result.get("data") or {}))
            rejected_data.setdefault(
                "next_actions",
                [
                    next_action(
                        "collect_build_artifacts",
                        "Rebuild a fresh, strictly validated artifact manifest before hardware programming.",
                        required_args=["run_name"],
                        arg_sources={"run_name": "program_from_artifact_manifest.run_name"},
                        preconditions=["The implementation run is complete and does not need refresh."],
                        stop_condition="collect_build_artifacts returns status=READY with a schema_version=4 source-closure-bound manifest.",
                    )
                ],
            )
            return failure(
                "program_from_artifact_manifest",
                str(validated_result.get("error_code") or "ARTIFACT_MANIFEST_REJECTED"),
                str(validated_result.get("message") or "Artifact manifest validation failed."),
                raw_excerpt=str(validated_result.get("raw_excerpt") or ""),
                data=rejected_data,
            )
        validated_manifest = dict(validated_result.get("data") or {})
        manifest_path = Path(str(validated_manifest["manifest_path"]))
        try:
            artifacts = select_programming_artifacts(validated_manifest, manifest_path=manifest_path)
        except ValueError as exc:
            return failure(
                "program_from_artifact_manifest",
                "MANIFEST_BITSTREAM_NOT_FOUND",
                str(exc),
                data=_with_hardware_validation({"manifest_path": str(manifest_path)}),
            )
        bitstream, ltx, error = _validated_programming_paths(
            tool="program_from_artifact_manifest",
            bitstream_path=artifacts["bitstream_path"],
            ltx_path=artifacts.get("ltx_path"),
            missing_bit_code="MANIFEST_BITSTREAM_NOT_FOUND",
        )
        if error:
            return error
        gate = self._hardware_programming_gate(
            tool="program_from_artifact_manifest",
            args=args,
            bitstream=bitstream,
            manifest_path=manifest_path,
            validated_manifest_sha256=str(validated_manifest.get("manifest_sha256", "")),
        )
        if not gate.get("ok"):
            return gate
        staged_bitstream, staged_error = _staged_programming_bitstream("program_from_artifact_manifest", gate)
        if staged_error:
            return staged_error
        command = program_hw_device_command(
            bitstream_path=str(staged_bitstream.path),
            ltx_path=str(ltx) if ltx else None,
            device=args.get("device"),
            target=args.get("target"),
        )
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 300)))
        raw["command"] = command
        if not raw.get("ok"):
            return _hardware_tcl_failure("program_from_artifact_manifest", raw)
        data = parse_programming_result(raw.get("raw", ""))
        data["command"] = command
        data["artifacts"] = {"manifest_path": str(manifest_path), "bitstream_path": str(bitstream), "ltx_path": str(ltx) if ltx else None}
        data["programming_gate"] = gate["data"]
        return _hardware_success("program_from_artifact_manifest", f"Hardware device programming {data['status']}.", data)

    def _hardware_programming_gate(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        bitstream: Path,
        manifest_path: Path | None = None,
        validated_manifest_sha256: str = "",
    ) -> dict[str, Any]:
        missing: list[str] = []
        if not str(args.get("hardware_intent", "")).strip():
            missing.append("hardware_intent")
        if str(args.get("confirm", "")) != "PROGRAM_FPGA":
            missing.append("confirm=PROGRAM_FPGA")
        board_fingerprint = str(args.get("board_fingerprint", "")).strip()
        if not board_fingerprint:
            missing.append("board_fingerprint")
        expected_bitstream_sha256 = str(args.get("expected_bitstream_sha256", "")).strip().lower()
        actual_bitstream_sha256 = sha256_file(bitstream)
        if not expected_bitstream_sha256:
            missing.append("expected_bitstream_sha256")
        elif expected_bitstream_sha256 != actual_bitstream_sha256:
            return _programming_gate_failure(
                tool,
                "BITSTREAM_HASH_MISMATCH",
                "Bitstream SHA256 does not match expected_bitstream_sha256.",
                {
                    "bitstream_path": str(bitstream),
                    "expected_bitstream_sha256": expected_bitstream_sha256,
                    "actual_bitstream_sha256": actual_bitstream_sha256,
                },
            )
        manifest_hash_data: dict[str, Any] = {}
        if manifest_path is not None:
            expected_manifest_sha256 = str(args.get("manifest_sha256", "")).strip().lower()
            actual_manifest_sha256 = sha256_file(manifest_path)
            manifest_hash_data = {
                "manifest_path": str(manifest_path),
                "expected_manifest_sha256": expected_manifest_sha256,
                "actual_manifest_sha256": actual_manifest_sha256,
            }
            if validated_manifest_sha256 and validated_manifest_sha256 != actual_manifest_sha256:
                return _programming_gate_failure(
                    tool,
                    "MANIFEST_CHANGED_AFTER_VALIDATION",
                    "Artifact manifest changed after strict validation.",
                    {
                        **manifest_hash_data,
                        "validated_manifest_sha256": validated_manifest_sha256,
                    },
                )
            if not expected_manifest_sha256:
                missing.append("manifest_sha256")
            elif expected_manifest_sha256 != actual_manifest_sha256:
                return _programming_gate_failure(
                    tool,
                    "MANIFEST_HASH_MISMATCH",
                    "Artifact manifest SHA256 does not match manifest_sha256.",
                    manifest_hash_data,
                )
        if missing:
            return _programming_gate_failure(
                tool,
                "HARDWARE_INTENT_REQUIRED",
                "FPGA programming requires explicit hardware intent, confirmation, board fingerprint, and artifact hashes.",
                {
                    "missing": missing,
                    "bitstream_path": str(bitstream),
                    "actual_bitstream_sha256": actual_bitstream_sha256,
                    **manifest_hash_data,
                },
            )
        blocked = _hardware_mode_disabled(tool, args)
        if blocked:
            return blocked

        preflight_command = get_hw_device_status_command(device=args.get("device"))
        preflight_raw = self._session().run_tcl(preflight_command, timeout_s=int(args.get("timeout_s", 60)))
        preflight_raw["command"] = preflight_command
        if not preflight_raw.get("ok"):
            return _hardware_tcl_failure(tool, preflight_raw)
        preflight = parse_programming_result(preflight_raw.get("raw", ""))
        actual_fingerprint = _hardware_fingerprint(preflight)
        if not _board_fingerprint_matches(board_fingerprint, preflight):
            return _programming_gate_failure(
                tool,
                "BOARD_FINGERPRINT_MISMATCH",
                "Selected hardware device does not match board_fingerprint.",
                {
                    "board_fingerprint": board_fingerprint,
                    "actual_fingerprint": actual_fingerprint,
                    "preflight": preflight,
                    "bitstream_path": str(bitstream),
                    "actual_bitstream_sha256": actual_bitstream_sha256,
                    **manifest_hash_data,
                },
            )
        runtime_root = Path(
            getattr(self._session(), "_runtime_path", None)
            or getattr(self._session(), "runtime_dir", None)
            or resolve_runtime_dir()
        )
        runtime_root.mkdir(parents=True, exist_ok=True)
        try:
            staged = stage_verified_file(
                bitstream,
                runtime_root=runtime_root,
                expected_sha256=expected_bitstream_sha256,
            )
        except (OSError, ValueError) as exc:
            return _programming_gate_failure(
                tool,
                "BITSTREAM_STAGING_FAILED",
                str(exc),
                {
                    "bitstream_path": str(bitstream),
                    "expected_bitstream_sha256": expected_bitstream_sha256,
                    "runtime_dir": str(runtime_root),
                },
            )
        return success(
            tool,
            "Hardware programming gate accepted.",
            _with_hardware_validation(
                {
                    "hardware_intent": str(args.get("hardware_intent", "")),
                    "board_fingerprint": board_fingerprint,
                    "actual_fingerprint": actual_fingerprint,
                    "bitstream_path": str(bitstream),
                    "actual_bitstream_sha256": actual_bitstream_sha256,
                    "staged_bitstream": {
                        "path": str(staged.path),
                        "sha256": staged.sha256,
                        "size": staged.size,
                        "file_id": staged.file_id,
                        "mtime_ns": staged.mtime_ns,
                    },
                    "preflight": preflight,
                    **manifest_hash_data,
                }
            ),
        )

    def _get_hw_device_status(self, args: dict[str, Any]) -> dict[str, Any]:
        blocked = _hardware_mode_disabled("get_hw_device_status", args)
        if blocked:
            return blocked
        command = get_hw_device_status_command(device=args.get("device"))
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _hardware_tcl_failure("get_hw_device_status", raw)
        data = parse_programming_result(raw.get("raw", ""))
        data["command"] = command
        return _hardware_success("get_hw_device_status", f"Hardware device status: {data['status']}.", data)

    def _get_hardware_messages(self, args: dict[str, Any]) -> dict[str, Any]:
        command = hardware_messages_command()
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _hardware_tcl_failure("get_hardware_messages", raw)
        data = parse_hardware_messages(raw.get("raw", ""))
        data["command"] = command
        data["hardware_tool_tier"] = "hardware_log_readonly"
        data["server_policy"] = hardware_server_policy()
        return _hardware_success("get_hardware_messages", "Hardware messages parsed.", data)

    def _run_tcl(self, args: dict[str, Any]) -> dict[str, Any]:
        command = str(args["command"])
        programming_block = _raw_tcl_programming_failure("run_tcl", command)
        if programming_block is not None:
            return programming_block
        if bool(args.get("dry_run", False)):
            data = tcl_dry_run_data(command, args)
            if not data["policy_allowed"]:
                return failure(
                    "run_tcl",
                    "TCL_POLICY_BLOCKED",
                    "Vivado Tcl dry-run was blocked by policy; command was not executed.",
                    data=tcl_policy_failure_data("run_tcl", command, args),
                )
            return success("run_tcl", "Vivado Tcl dry-run policy accepted.", data)
        if not tcl_policy_allows(command, args)[0]:
            return failure("run_tcl", "TCL_POLICY_BLOCKED", "Vivado Tcl command blocked by policy.", data=tcl_policy_failure_data("run_tcl", command, args))
        return failure(
            "run_tcl",
            "TCL_EXECUTION_DISABLED",
            "Public raw Tcl execution is disabled; use a dedicated typed MCP tool or dry_run=true.",
            data={
                **tcl_dry_run_data(command, {**args, "dry_run": True}),
                "dry_run": False,
                "policy_allowed": False,
                "execution_disabled": True,
            },
        )

    def _safe_tcl(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            command = safe_tcl(str(args["template"]), args.get("args", {}))
        except KeyError as exc:
            return failure(
                "safe_tcl",
                "SAFE_TCL_TEMPLATE_ERROR",
                "Safe Tcl template contains an unresolved placeholder. Literal Tcl braces are allowed; placeholders must have matching args.",
                data={
                    "template": str(args.get("template", "")),
                    "missing": [part.strip().strip("'\"") for part in str(exc).split(",") if part.strip()],
                    "next_actions": [
                        next_action(
                            "safe_tcl",
                            "Retry with args for every placeholder, or use literal Tcl braces that are not simple {name} placeholders.",
                            required_args=["template", "args"],
                            arg_sources={"template": "original Tcl template", "args": "mapping for each {placeholder} token"},
                            preconditions=["Review the Tcl template and decide which braced words are placeholders."],
                            stop_condition="safe_tcl returns ok=true or a policy-gated structured failure.",
                        )
                    ],
                },
            )
        programming_block = _raw_tcl_programming_failure("safe_tcl", command)
        if programming_block is not None:
            return programming_block
        if bool(args.get("dry_run", False)):
            data = tcl_dry_run_data(command, args)
            if not data["policy_allowed"]:
                return failure(
                    "safe_tcl",
                    "TCL_POLICY_BLOCKED",
                    "Safe Tcl dry-run was blocked by policy; command was not executed.",
                    data=tcl_policy_failure_data("safe_tcl", command, args),
                )
            return success("safe_tcl", "Safe Tcl dry-run policy accepted.", data)
        if not tcl_policy_allows(command, args)[0]:
            return failure("safe_tcl", "TCL_POLICY_BLOCKED", "Safe Tcl command blocked by policy.", data=tcl_policy_failure_data("safe_tcl", command, args))
        return failure(
            "safe_tcl",
            "TCL_EXECUTION_DISABLED",
            "Public Tcl template execution is disabled; use a dedicated typed MCP tool or dry_run=true.",
            data={
                **tcl_dry_run_data(command, {**args, "dry_run": True}),
                "dry_run": False,
                "policy_allowed": False,
                "execution_disabled": True,
            },
        )

    def _create_project(self, args: dict[str, Any]) -> dict[str, Any]:
        if bool(args.get("force", False)):
            return failure(
                "create_project",
                "DESTRUCTIVE_FORCE_DISABLED",
                "create_project force=true is disabled because it can replace an existing project without an attestable review plan.",
                data={"force": True, "project_dir": str(args.get("project_dir", "")), "project_name": str(args.get("project_name", ""))},
            )
        project_name = str(args["project_name"])
        project_dir = str(args["project_dir"])
        project_path = Path(project_dir) / f"{project_name}.xpr"
        if project_path.exists():
            return failure(
                "create_project",
                "PROJECT_ALREADY_EXISTS",
                "create_project refuses to adopt or replace an existing .xpr; open it inspection-only or choose a new working-project path.",
                data={
                    "project_path": str(project_path),
                    "project_dir": project_dir,
                    "project_name": project_name,
                    "original_project_protected": True,
                    "next_actions": [
                        next_action(
                            "open_project",
                            "Open the existing project in inspection-only mode.",
                            required_args=["project_path"],
                            arg_sources={"project_path": str(project_path)},
                            stop_condition="open_project returns the existing-project read-only policy.",
                        ),
                        *_existing_project_rebuild_actions(),
                    ],
                },
            )
        part = str(args["part"])
        top = str(args["top"])
        rtl_files = [str(path) for path in args.get("rtl_files", [])]
        xdc_files = [str(path) for path in args.get("xdc_files", [])]
        sim_files = [str(path) for path in args.get("sim_files", [])]
        file_specs_supplied = "file_specs" in args
        file_specs: list[dict[str, Any]] = []
        if file_specs_supplied:
            file_specs, file_spec_errors = normalize_project_file_specs(
                list(args.get("file_specs", [])),
                rtl_files=rtl_files,
                xdc_files=xdc_files,
                sim_files=sim_files,
            )
            if file_spec_errors:
                return failure(
                    "create_project",
                    "PROJECT_FILE_SEMANTICS_INVALID",
                    "Per-file Vivado semantics are incomplete or unsafe; the project was not created.",
                    data={
                        "file_specs_supplied": True,
                        "file_spec_errors": file_spec_errors,
                        "semantic_inventory_digest": "",
                        "original_project_protected": True,
                    },
                )
        composite_failure = _composite_project_input_failure(
            "create_project",
            rtl_files + xdc_files + sim_files,
        )
        if composite_failure:
            return composite_failure
        constraint_failure = _constraint_input_failure("create_project", xdc_files)
        if constraint_failure:
            return constraint_failure
        testbench_top = args.get("testbench_top")
        source_include_dirs = [str(path) for path in args.get("source_include_dirs", [])]
        source_defines, source_defines_error = _validated_source_defines("create_project", args.get("source_defines", {}))
        if source_defines_error:
            return source_defines_error
        include_dirs = [str(path) for path in args.get("include_dirs", [])]
        defines, defines_error = _validated_simulation_defines("create_project", args.get("defines", {}))
        if defines_error:
            return defines_error
        simulator = str(args.get("simulator", "Vivado Simulator"))
        target_language = args.get("target_language")
        vivado_target_language, sv_file_type = _normalize_target_language(target_language)
        command_parts = [
            f"create_project {tcl_list_quote(project_name)} {tcl_list_quote(project_dir)} -part {tcl_list_quote(part)}",
        ]
        if rtl_files:
            command_parts.append(f"add_files {_tcl_list(rtl_files)}")
        if xdc_files:
            command_parts.append(f"add_files -fileset constrs_1 {_tcl_list(xdc_files)}")
        if vivado_target_language:
            command_parts.append(f"set_property target_language {tcl_list_quote(vivado_target_language)} [current_project]")
        if source_include_dirs:
            command_parts.append(f"set_property include_dirs {_tcl_list(source_include_dirs)} [get_filesets {{sources_1}}]")
        if source_defines:
            command_parts.append(f"set_property verilog_define {_tcl_define_list(source_defines)} [get_filesets {{sources_1}}]")
        command_parts.extend(
            [
                f"set_property target_simulator {tcl_list_quote(simulator)} [current_project]",
                f"set_property top {tcl_list_quote(top)} [current_fileset]",
                "update_compile_order -fileset sources_1",
            ]
        )
        if sim_files or testbench_top or include_dirs or defines:
            command_parts.append(
                configure_simulation_command(
                    sim_files=sim_files,
                    testbench_top=str(testbench_top) if testbench_top else None,
                    include_dirs=include_dirs,
                    defines=defines,
                    simulator=simulator,
                )
            )
        if file_specs_supplied:
            replay_command = replay_file_specs_command(file_specs)
            if replay_command:
                command_parts.append(replay_command)
        elif sv_file_type:
            sv_files = _systemverilog_files(rtl_files + sim_files)
            if sv_files:
                command_parts.append(
                    f"foreach f {_tcl_list(sv_files)} "
                    f"{{set matches [get_files -quiet $f]; if {{[llength $matches] > 0}} {{set_property file_type {tcl_list_quote(sv_file_type)} $matches}}}}"
                )
        command_parts.extend(["set d [get_property DIRECTORY [current_project]]", "set d"])
        command = "; ".join(command_parts)
        timeout_s = int(args.get("timeout_s", 300))
        planned_language_policy = _planned_language_policy(target_language, vivado_target_language, sv_file_type)
        partial_context = _create_project_partial_context(
            project_name=project_name,
            project_dir=project_dir,
            part=part,
            top=top,
            testbench_top=str(testbench_top) if testbench_top else "",
            rtl_files=rtl_files,
            xdc_files=xdc_files,
            sim_files=sim_files,
            target_language=target_language,
            vivado_target_language=vivado_target_language,
            sv_file_type=sv_file_type,
        )
        self._project_mutation_scope = "indeterminate"
        try:
            data = self._session().run_tcl(command, timeout_s=timeout_s)
        except TimeoutError as exc:
            partial_context = _create_project_partial_context(
                project_name=project_name,
                project_dir=project_dir,
                part=part,
                top=top,
                testbench_top=str(testbench_top) if testbench_top else "",
                rtl_files=rtl_files,
                xdc_files=xdc_files,
                sim_files=sim_files,
                target_language=target_language,
                vivado_target_language=vivado_target_language,
                sv_file_type=sv_file_type,
            )
            return self._timeout_failure(
                "create_project",
                exc,
                timeout_s=timeout_s,
                command=command,
                request_context={
                    "project_name": project_name,
                    "project_dir": project_dir,
                    "part": part,
                    "top": top,
                    "rtl_files": rtl_files,
                    "xdc_files": xdc_files,
                    "sim_files": sim_files,
                    "testbench_top": str(testbench_top) if testbench_top else "",
                    "source_include_dirs": source_include_dirs,
                    "source_defines": source_defines,
                    "include_dirs": include_dirs,
                    "defines": defines,
                    "target_language": str(target_language or ""),
                    "file_specs_supplied": file_specs_supplied,
                    "semantic_inventory_digest": file_spec_inventory_digest(file_specs) if file_specs_supplied else "",
                },
                partial_context=partial_context,
                next_actions=_create_project_timeout_actions(partial_context),
            )
        data["command"] = command
        data["project"] = {
            "name": project_name,
            "directory": project_dir,
            "part": part,
            "top": top,
            "sim_top": str(testbench_top) if testbench_top else None,
            "simulator": simulator,
            "target_language": target_language,
            "vivado_target_language": vivado_target_language,
            "systemverilog_file_type": sv_file_type,
            "requested_target_language": target_language,
            "vivado_project_target_language": vivado_target_language,
            "source_file_type_policy": sv_file_type,
            "xpr_path": str(Path(project_dir) / f"{project_name}.xpr"),
        }
        if sv_file_type == "SystemVerilog":
            language_policy_note = (
                "SystemVerilog is enabled through per-file file_type=SystemVerilog for .sv sources; "
                "Vivado project target_language remains Verilog as a compatibility setting."
            )
            data["project"]["language_policy_note"] = language_policy_note
            data["language_policy_note"] = language_policy_note
        data["files"] = {"rtl": rtl_files, "xdc": xdc_files, "sim": sim_files}
        data["file_specs"] = file_specs
        data["semantic_inventory_digest"] = file_spec_inventory_digest(file_specs) if file_specs_supplied else ""
        data["planned_files"] = partial_context["planned_files"]
        data["planned_language_policy"] = planned_language_policy
        data["setup_status"] = _planned_setup_status(
            partial_success=True,
            project_path=data["project"]["xpr_path"],
            rtl_files=rtl_files,
            xdc_files=xdc_files,
            sim_files=sim_files,
            top=top,
            testbench_top=str(testbench_top) if testbench_top else "",
        )
        if not data["setup_status"]["missing_expected_files"]:
            data["setup_status"]["status"] = "READY"
            data["setup_status"]["status_scope"] = "post_create_project"
            data["setup_status"]["actual_state_known"] = True
            data["setup_status"]["needs_open_project"] = False
            data["setup_status"]["needs_fileset_repair"] = False
            for fileset_status in data["setup_status"]["filesets"].values():
                fileset_status["needs_repair"] = False
        if not data.get("ok"):
            return _tcl_failure("create_project", data)
        session = self._session()
        if file_specs_supplied:
            semantic_verification = _verify_project_file_semantics(
                session,
                file_specs,
                timeout_s=min(timeout_s, 120),
            )
            data["file_semantics"] = semantic_verification
            if not semantic_verification["matches"]:
                self._project_mutation_scope = "indeterminate"
                self._active_project_capability = None
                return failure(
                    "create_project",
                    "PROJECT_FILE_SEMANTICS_MISMATCH",
                    "Vivado created the working project, but its allowlisted per-file semantics do not match the requested inventory.",
                    data={
                        **data,
                        "partial_success": True,
                        "project_path": data["project"]["xpr_path"],
                        "project_capability": {"bound": False},
                        "next_actions": [
                            next_action(
                                "close_project",
                                "Close the unbound working project before reviewing the semantic mismatch.",
                                stop_condition="close_project returns ok=true or confirms no project is open.",
                            ),
                            next_action(
                                "get_agent_workflows",
                                "Review the existing-project handoff workflow before rebuilding into a new path.",
                                optional=True,
                                stop_condition="A new project path and corrected complete file_specs inventory are available.",
                            ),
                        ],
                    },
                )
        project_key = _project_path_key(data["project"]["xpr_path"])
        if isinstance(session, GuiTcpVivadoSession):
            try:
                capability = create_project_capability(
                    data["project"]["xpr_path"],
                    generation_id=str(data.get("generation_id") or getattr(session, "generation_id", "")),
                )
            except (ManagedPathError, OSError, ValueError) as exc:
                self._project_mutation_scope = "indeterminate"
                self._active_project_capability = None
                return failure(
                    "create_project",
                    "PROJECT_CAPABILITY_ESTABLISH_FAILED",
                    "Vivado created the project, but MCP could not bind it to a stable project capability.",
                    data={
                        **data,
                        "partial_success": True,
                        "project_path": data["project"]["xpr_path"],
                        "reason": str(exc),
                        "mutation_scope": self._project_mutation_scope,
                    },
                )
        else:
            capability = {
                "project_path": data["project"]["xpr_path"],
                "project_path_key": project_key,
                "generation_id": str(data.get("generation_id") or getattr(session, "generation_id", "test-session")),
                "unmanaged_test_session": True,
            }
        self._project_mutation_scope = "mcp_created_project"
        self._mcp_created_project_capabilities[project_key] = capability
        self._active_project_capability = capability
        data["project_capability"] = {
            "schema_version": capability.get("schema_version", 0),
            "bound": True,
            "project_path": capability.get("project_path", ""),
            "generation_id": capability.get("generation_id", ""),
            "marker_path": capability.get("marker_path", ""),
        }
        message = "Vivado project created."
        if sv_file_type == "SystemVerilog":
            message = f"{message} SystemVerilog .sv files use file_type=SystemVerilog while project target_language remains Verilog."
        return success("create_project", message, data)

    def _configure_simulation(self, args: dict[str, Any]) -> dict[str, Any]:
        sim_files = [str(path) for path in args.get("sim_files", [])]
        composite_failure = _composite_project_input_failure("configure_simulation", sim_files)
        if composite_failure:
            return composite_failure
        defines, defines_error = _validated_simulation_defines("configure_simulation", args.get("defines", {}))
        if defines_error:
            return defines_error
        command = configure_simulation_command(
            sim_files=sim_files,
            testbench_top=args.get("testbench_top"),
            include_dirs=[str(path) for path in args.get("include_dirs", [])],
            defines=defines,
            simulator=str(args.get("simulator", "Vivado Simulator")),
            simset=str(args.get("simset", "sim_1")),
        )
        timeout_s = int(args.get("timeout_s", 60))
        try:
            data = self._session().run_tcl(command, timeout_s=timeout_s)
        except TimeoutError as exc:
            context = {
                "simset": str(args.get("simset", "sim_1")),
                "sim_files": sim_files,
                "testbench_top": str(args.get("testbench_top", "") or ""),
                "include_dirs": [str(path) for path in args.get("include_dirs", [])],
                "defines": defines,
                "simulator": str(args.get("simulator", "Vivado Simulator")),
            }
            return self._timeout_failure(
                "configure_simulation",
                exc,
                timeout_s=timeout_s,
                command=command,
                request_context=context,
                partial_context={
                    "partial_success": False,
                    "project_state_hint": {"recommended_probe": "list_fileset_files", "fileset": context["simset"]},
                },
                next_actions=_setup_repair_actions(context | {"tool": "configure_simulation"}),
            )
        data["command"] = command
        if not data.get("ok"):
            return _tcl_failure("configure_simulation", data)
        data["simulation"] = {
            "simset": str(args.get("simset", "sim_1")),
            "top": args.get("testbench_top"),
            "simulator": str(args.get("simulator", "Vivado Simulator")),
        }
        data["files"] = {"sim": [str(path) for path in args.get("sim_files", [])]}
        return success("configure_simulation", "Vivado simulation fileset configured.", data)

    def _repair_project_setup(self, args: dict[str, Any]) -> dict[str, Any]:
        timeout_s = int(args.get("timeout_s", 120))
        rtl_files = [str(path) for path in args.get("rtl_files", [])]
        xdc_files = [str(path) for path in args.get("xdc_files", [])]
        constraint_failure = _constraint_input_failure("repair_project_setup", xdc_files)
        if constraint_failure:
            return constraint_failure
        sim_files = [str(path) for path in args.get("sim_files", [])]
        composite_failure = _composite_project_input_failure(
            "repair_project_setup",
            rtl_files + xdc_files + sim_files,
        )
        if composite_failure:
            return composite_failure
        top = str(args.get("top", "") or "")
        testbench_top = str(args.get("testbench_top", "") or "")
        include_dirs = [str(path) for path in args.get("include_dirs", [])]
        defines, defines_error = _validated_simulation_defines("repair_project_setup", args.get("defines", {}))
        if defines_error:
            return defines_error
        simulator = str(args.get("simulator", "Vivado Simulator"))
        target_language = args.get("target_language")
        vivado_target_language, sv_file_type = _normalize_target_language(target_language)
        missing_files = _missing_input_files(rtl_files + xdc_files + sim_files + include_dirs)
        context = {
            "project_path": str(args.get("project_path", "") or ""),
            "rtl_files": rtl_files,
            "xdc_files": xdc_files,
            "sim_files": sim_files,
            "top": top,
            "testbench_top": testbench_top,
            "target_language": str(target_language or ""),
            "include_dirs": include_dirs,
            "defines": defines,
            "simulator": simulator,
        }
        planned_operations = _repair_project_setup_operations(
            project_path=context["project_path"],
            rtl_files=rtl_files,
            xdc_files=xdc_files,
            sim_files=sim_files,
            top=top,
            testbench_top=testbench_top,
            include_dirs=include_dirs,
            defines=defines,
            simulator=simulator,
            vivado_target_language=vivado_target_language,
            sv_file_type=sv_file_type,
        )
        setup_status = _planned_setup_status(
            partial_success=not missing_files,
            project_path=context["project_path"],
            rtl_files=rtl_files,
            xdc_files=xdc_files,
            sim_files=sim_files,
            top=top,
            testbench_top=testbench_top,
            missing_files=missing_files,
        )
        base_data = {
            "status": "DRY_RUN" if bool(args.get("dry_run", True)) else "PLANNED",
            "dry_run": bool(args.get("dry_run", True)),
            "request_context": context,
            "planned_operations": planned_operations,
            "planned_operation_semantics": {
                "mode": "idempotent_reconcile",
                "note": "planned_operations describe reconcile steps; they do not imply duplicate file insertion or destructive reset.",
            },
            "planned_files": {"rtl": rtl_files, "xdc": xdc_files, "sim": sim_files},
            "planned_language_policy": _planned_language_policy(target_language, vivado_target_language, sv_file_type),
            "setup_status": setup_status,
            "fileset_summary": setup_status["filesets"],
            "missing_after_repair": missing_files,
            "next_actions": _post_setup_repair_actions(),
        }
        if missing_files:
            missing_data = base_data | {
                "status": "BLOCK",
                "next_actions": [
                    next_action(
                        "repair_project_setup",
                        "Retry setup repair after providing existing RTL/XDC/simulation files.",
                        preconditions=["All paths in repair_project_setup.data.missing_after_repair exist on disk."],
                        stop_condition="repair_project_setup returns READY or REPAIRED.",
                    )
                ],
            }
            return failure(
                "repair_project_setup",
                "PROJECT_SETUP_INPUT_MISSING",
                "Project setup repair input contains missing files.",
                data=missing_data,
            )
        if bool(args.get("dry_run", True)):
            return success("repair_project_setup", "Project setup repair dry-run planned.", base_data)

        command = _repair_project_setup_command(
            project_path=context["project_path"],
            rtl_files=rtl_files,
            xdc_files=xdc_files,
            sim_files=sim_files,
            top=top,
            testbench_top=testbench_top,
            include_dirs=include_dirs,
            defines=defines,
            simulator=simulator,
            vivado_target_language=vivado_target_language,
            sv_file_type=sv_file_type,
        )
        try:
            raw = self._session().run_tcl(command, timeout_s=timeout_s)
        except TimeoutError as exc:
            return self._timeout_failure(
                "repair_project_setup",
                exc,
                timeout_s=timeout_s,
                command=command,
                request_context=context,
                partial_context=base_data | {"partial_success": False},
                next_actions=_setup_repair_actions(context | {"tool": "repair_project_setup"}),
            )
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("repair_project_setup", raw)
        parsed = _parse_key_value_lines(raw.get("raw", ""))
        status = str(parsed.get("setup_status", "") or "").upper()
        discovery_status = str(parsed.get("postcondition_discovery_status", "") or "").upper()
        missing_after = decode_wire_list(str(parsed.get("missing_after_repair", "")))
        discovery_errors = decode_wire_list(str(parsed.get("discovery_errors", "")))
        if discovery_status != "READY" or status == "ERROR" or discovery_errors:
            return failure(
                "repair_project_setup",
                "PROJECT_SETUP_POSTCONDITION_DISCOVERY_FAILED",
                "Project setup mutation completed, but required Vivado postcondition discovery failed or was incomplete.",
                data=base_data
                | {
                    "status": "BLOCK",
                    "dry_run": False,
                    "command": command,
                    "result": parsed,
                    "postcondition_discovery_status": discovery_status or "ERROR",
                    "discovery_errors": discovery_errors
                    or ["postcondition_discovery_status is missing or invalid"],
                    "missing_after_repair": missing_after,
                },
            )
        if status != "READY" or missing_after:
            return failure(
                "repair_project_setup",
                "PROJECT_SETUP_INCOMPLETE",
                "Project setup repair postconditions are not READY.",
                data=base_data
                | {
                    "status": "BLOCK",
                    "dry_run": False,
                    "command": command,
                    "result": parsed,
                    "postcondition_discovery_status": discovery_status,
                    "discovery_errors": discovery_errors,
                    "missing_after_repair": missing_after,
                },
            )
        setup_status_after = _post_repair_setup_status(
            base_data["setup_status"],
            status=status,
            missing_after=missing_after,
        )
        data = base_data | {
            "status": "REPAIRED",
            "dry_run": False,
            "command": command,
            "result": parsed,
            "setup_status": setup_status_after,
            "fileset_summary": setup_status_after["filesets"],
            "missing_after_repair": missing_after,
            "postcondition_discovery_status": discovery_status,
            "discovery_errors": discovery_errors,
        }
        return success("repair_project_setup", f"Project setup repair {status}.", data)

    def _run_behavioral_simulation(self, args: dict[str, Any]) -> dict[str, Any]:
        simset = str(args.get("simset", "sim_1"))
        requested_vcd_name = str(args.get("vcd_name", "vmcp_behav.vcd"))
        try:
            validate_generated_child_name(simset)
            validate_generated_child_name(requested_vcd_name)
            if Path(requested_vcd_name).suffix.lower() != ".vcd":
                raise ValueError("VCD output name must end with .vcd")
        except ValueError as exc:
            return failure(
                "run_behavioral_simulation",
                "SIMULATION_OUTPUT_NAME_INVALID",
                str(exc),
                data={"simset": simset, "vcd_name": requested_vcd_name, "policy_allowed": False},
            )
        session = self._session()
        managed_transport = isinstance(session, GuiTcpVivadoSession)
        if managed_transport and args.get("incremental") is True:
            return failure(
                "run_behavioral_simulation",
                "SIMULATION_INCREMENTAL_DISABLED",
                "Trusted XSIM execution requires a non-incremental rebuild; incremental=true is not allowed.",
                data={"simset": simset, "incremental": True, "policy_allowed": False},
            )
        run_all = bool(args.get("run_all", False))
        export_vcd = bool(args.get("export_vcd", False))
        timeout_s = int(args.get("timeout_s", 300))
        max_vcd_mb = args.get("max_vcd_mb", DEFAULT_MAX_VCD_MB)
        preflight = self._simulation_vcd_preflight(simset=simset, timeout_s=min(timeout_s, 60))
        preflight_state = _simulation_preflight_state(preflight)
        preflight = preflight_state["preflight"]
        testbench_vcd_usage = preflight_state["testbench_vcd_usage"]
        vcd_risk = export_vcd or testbench_vcd_usage
        waveform_paths = preflight_state["waveform_paths"]
        execution_effects_before = preflight_state["execution_effects"]
        if waveform_paths.get("uncontrolled_reasons"):
            return _simulation_uncontrolled_waveform_failure(
                simset=simset,
                export_vcd=export_vcd,
                preflight=preflight,
            )
        if run_all and float(max_vcd_mb) > 0:
            return _simulation_run_all_vcd_blocked(
                simset=simset,
                export_vcd=export_vcd,
                preflight=preflight,
                vcd_risk=vcd_risk,
            )
        skip_mcp_vcd = export_vcd and testbench_vcd_usage
        source_identity_before = preflight_state["source_identity"]
        stable_input_identity_before = preflight_state["stable_input_identity"]
        if managed_transport:
            trust_failure = _trusted_simulation_failure(
                args,
                preflight=preflight,
                source_identity=source_identity_before,
            )
            if trust_failure:
                return trust_failure
            policy_command = managed_simulation_policy_preflight_command(simset=simset)
            policy_raw = session.run_tcl(policy_command, timeout_s=min(timeout_s, 60))
            policy_raw["command"] = policy_command
            if not policy_raw.get("ok"):
                return _tcl_failure("run_behavioral_simulation", policy_raw)
            policy_preflight = parse_simulation_vcd_preflight(policy_raw.get("raw", ""))
            policy_preflight["command"] = policy_command
            policy_preflight["simset"] = simset
            preflight_state = _simulation_preflight_state(policy_preflight)
            preflight = preflight_state["preflight"]
            testbench_vcd_usage = preflight_state["testbench_vcd_usage"]
            vcd_risk = export_vcd or testbench_vcd_usage
            waveform_paths = preflight_state["waveform_paths"]
            execution_effects_before = preflight_state["execution_effects"]
            if waveform_paths.get("uncontrolled_reasons"):
                return _simulation_uncontrolled_waveform_failure(
                    simset=simset,
                    export_vcd=export_vcd,
                    preflight=preflight,
                )
            skip_mcp_vcd = export_vcd and testbench_vcd_usage
            source_identity_before = preflight_state["source_identity"]
            stable_input_identity_before = preflight_state["stable_input_identity"]
            trust_failure = _trusted_simulation_failure(
                args,
                preflight=preflight,
                source_identity=source_identity_before,
            )
            if trust_failure:
                return trust_failure
        if managed_transport and source_identity_before.get("status") != "READY":
            return failure(
                "run_behavioral_simulation",
                "SIMULATION_SOURCE_IDENTITY_UNAVAILABLE",
                "Simulation compile-order source identity could not be established before launch.",
                data={
                    "simset": simset,
                    "preflight": preflight,
                    "simulation_source_identity": source_identity_before,
                    "next_actions": _simulation_failure_repair_actions(simset=simset),
                },
            )
        simulation_started_at = datetime.now(UTC).isoformat()
        simulation_invocation_id = f"sim_{uuid.uuid4().hex}"
        requested_vcd_path = Path(requested_vcd_name)
        effective_vcd_name = f"{requested_vcd_path.stem}_{simulation_invocation_id}{requested_vcd_path.suffix}"
        command = behavioral_simulation_command(
            simset=simset,
            run_time=str(args.get("run_time", "1 us")),
            run_all=run_all,
            export_vcd=export_vcd,
            vcd_name=effective_vcd_name,
            max_vcd_mb=max_vcd_mb,
            skip_mcp_vcd=skip_mcp_vcd,
            testbench_vcd_usage=testbench_vcd_usage,
            simulation_invocation_id=simulation_invocation_id,
            started_at=simulation_started_at,
            monitored_waveform_paths=[str(path) for path in waveform_paths.get("monitored_paths", [])],
            source_identity_sha256=str(source_identity_before.get("sha256", "")) if source_identity_before.get("status") == "READY" else "",
            stable_input_identity_sha256=str(stable_input_identity_before.get("sha256", ""))
            if stable_input_identity_before.get("status") == "READY"
            else "",
            session_generation_id=str(getattr(session, "generation_id", "")),
            incremental=None if managed_transport else args.get("incremental"),
        )
        if managed_transport and EXECUTABLE_CONSTRAINT_BLOCK_MARKER not in command:
            command = f"{run_hook_guard_command()}; {command}"
        source_lock: dict[str, int] = {}
        output_lock: dict[str, int] = {}
        source_identity_after_locked: dict[str, Any] = {}
        stable_input_identity_after_locked: dict[str, Any] = {}
        try:
            if managed_transport:
                trust_closure = preflight.get("trusted_execution_closure", {})
                project_dir = Path(str(preflight.get("project_dir", ""))).resolve()
                project_path = Path(str(preflight.get("project_path", ""))).resolve()
                sim_dir = Path(str(preflight.get("sim_dir", ""))).resolve()
                validate_managed_path(project_dir, project_dir)
                validate_managed_path(project_dir, project_path)
                ensure_managed_directory(project_dir, sim_dir)
                project_path_key = os.path.normcase(str(project_path))
                immutable_closure_files = [
                    str(path)
                    for path in trust_closure.get("closure_files", [])
                    if os.path.normcase(str(Path(str(path)).resolve())) != project_path_key
                ]
                with hold_managed_paths_stable(
                    str(trust_closure.get("accepted_root", "")),
                    files=immutable_closure_files,
                    directories=[str(path) for path in trust_closure.get("closure_directories", [])],
                    writable_files=[project_path],
                ) as source_lock:
                    if _directory_has_entries(sim_dir):
                        return failure(
                            "run_behavioral_simulation",
                            "SIMULATION_GENERATED_STATE_NOT_CLEAN",
                            "Trusted XSIM execution requires an empty generated simulation directory; clean the simset outputs first.",
                            data={
                                "sim_dir": str(sim_dir),
                                "simset": simset,
                                "next_actions": _simulation_failure_repair_actions(simset=simset),
                            },
                        )
                    with hold_managed_output_directories(
                        project_dir,
                        directories=[sim_dir],
                    ) as output_lock:
                        if _directory_has_entries(sim_dir):
                            return failure(
                                "run_behavioral_simulation",
                                "SIMULATION_GENERATED_STATE_CHANGED_BEFORE_LAUNCH",
                                "Generated XSIM state appeared before launch while the output directory identity was being pinned.",
                                data={"sim_dir": str(sim_dir), "simset": simset},
                            )
                        locked_preflight = self._simulation_vcd_preflight(
                            simset=simset,
                            timeout_s=min(timeout_s, 60),
                        )
                        if not locked_preflight.get("ok"):
                            return failure(
                                "run_behavioral_simulation",
                                "SIMULATION_PREFLIGHT_RECHECK_FAILED",
                                "Simulation project/fileset metadata could not be re-attested while the source closure was locked.",
                                data={
                                    "preflight": preflight,
                                    "locked_preflight": locked_preflight,
                                    "next_actions": _simulation_failure_repair_actions(simset=simset),
                                },
                            )
                        locked_waveform_paths = analyze_testbench_waveform_paths(locked_preflight)
                        execution_effects_locked = _simulation_execution_effects_snapshot(locked_waveform_paths)
                        if (
                            locked_waveform_paths.get("status") != "READY"
                            or execution_effects_locked != execution_effects_before
                        ):
                            execution_effects_delta = _simulation_execution_effects_delta(
                                execution_effects_before,
                                execution_effects_locked,
                            )
                            return failure(
                                "run_behavioral_simulation",
                                "SIMULATION_EXECUTION_EFFECTS_CHANGED_BEFORE_LAUNCH",
                                "Simulation host inputs, outputs, or source closure changed before the execution lock was acquired.",
                                data={
                                    "preflight": preflight,
                                    "execution_effects_before": execution_effects_before,
                                    "execution_effects_locked": execution_effects_locked,
                                    "execution_effects_delta": execution_effects_delta,
                                    "locked_analysis": locked_waveform_paths,
                                    "next_actions": _simulation_failure_repair_actions(simset=simset),
                                },
                            )
                        locked_preflight = dict(locked_preflight)
                        locked_preflight["host_input_files"] = [
                            *list(locked_waveform_paths.get("host_input_files", [])),
                            *([str(locked_preflight.get("project_path"))] if locked_preflight.get("project_path") else []),
                        ]
                        locked_identity = build_simulation_source_identity(locked_preflight)
                        locked_stable_identity = build_simulation_stable_input_identity(locked_preflight, locked_identity)
                        if (
                            locked_identity.get("status") != "READY"
                            or locked_identity.get("sha256") != source_identity_before.get("sha256")
                            or locked_stable_identity.get("status") != "READY"
                            or locked_stable_identity.get("sha256") != stable_input_identity_before.get("sha256")
                        ):
                            return failure(
                                "run_behavioral_simulation",
                                "SIMULATION_SOURCE_CHANGED_BEFORE_LAUNCH",
                                "Simulation source/include closure changed before the stability lock was acquired.",
                                data={
                                    "preflight": preflight,
                                    "simulation_source_identity_before": source_identity_before,
                                    "simulation_source_identity_locked": locked_identity,
                                    "simulation_stable_input_identity_before": stable_input_identity_before,
                                    "simulation_stable_input_identity_locked": locked_stable_identity,
                                    "next_actions": _simulation_failure_repair_actions(simset=simset),
                                },
                            )
                        raw = session.run_tcl(command, timeout_s=timeout_s)
                        if raw.get("ok"):
                            post_preflight = self._simulation_vcd_preflight(
                                simset=simset,
                                timeout_s=min(timeout_s, 60),
                            )
                            if post_preflight.get("ok"):
                                post_state = _simulation_preflight_state(post_preflight)
                                source_identity_after_locked = post_state["source_identity"]
                                stable_input_identity_after_locked = post_state["stable_input_identity"]
                            else:
                                issue = "simulation project/fileset metadata could not be re-queried after XSIM execution"
                                source_identity_after_locked = {"status": "BLOCK", "sha256": "", "identity": {}, "issues": [issue]}
                                stable_input_identity_after_locked = {"status": "BLOCK", "sha256": "", "identity": {}, "issues": [issue]}
            else:
                raw = session.run_tcl(command, timeout_s=timeout_s)
        except TimeoutError as exc:
            return self._simulation_timeout_failure(simset=simset, timeout_s=timeout_s, export_vcd=export_vcd, preflight=preflight, error=exc)
        except ManagedPathError as exc:
            return failure(
                "run_behavioral_simulation",
                "SIMULATION_SOURCE_LOCK_FAILED",
                "Simulation source/include/host-input closure could not be held stable for XSIM host execution.",
                data={
                    "preflight": preflight,
                    "simulation_source_identity": source_identity_before,
                    "reason": str(exc),
                    "next_actions": _simulation_failure_repair_actions(simset=simset),
                },
            )
        raw["command"] = command
        if not raw.get("ok"):
            transient_failure = _simulation_xsim_launch_transient_failure(
                raw,
                command=command,
                simset=simset,
                run_time=str(args.get("run_time", "1 us")),
                export_vcd=export_vcd,
                max_vcd_mb=max_vcd_mb,
                preflight=preflight,
            )
            if transient_failure is not None:
                if self._session_override is None and hasattr(self.manager, "stop"):
                    runtime_dir = str(getattr(session, "_runtime_path", "") or getattr(session, "runtime_dir", "") or "")
                    project_dir = str(preflight.get("project_dir", ""))
                    project_name = str(preflight.get("project_name", ""))
                    project_path = str(Path(project_dir) / f"{project_name}.xpr") if project_dir and project_name else ""
                    try:
                        stop_result = self.manager.stop()
                    except Exception as stop_exc:  # noqa: BLE001 - recovery evidence must remain serializable.
                        stop_result = {"ok": False, "error_code": stop_exc.__class__.__name__, "message": str(stop_exc)}
                    managed_session_stopped = bool(stop_result.get("stopped") or stop_result.get("ok"))
                    recovery_actions = _simulation_managed_restart_actions(
                        simset=simset,
                        run_time=str(args.get("run_time", "1 us")),
                        export_vcd=export_vcd,
                        max_vcd_mb=max_vcd_mb,
                        runtime_dir=runtime_dir,
                        project_path=project_path,
                        stop_succeeded=managed_session_stopped,
                    )
                    transient_data = transient_failure.get("data") if isinstance(transient_failure.get("data"), dict) else {}
                    transient_data.update(
                        {
                            "abort_attempted": True,
                            "managed_session_stopped": managed_session_stopped,
                            "stop_result": stop_result,
                            "runtime_dir": runtime_dir,
                            "project_path": project_path,
                            "next_actions": recovery_actions,
                        }
                    )
                    transient_failure["data"] = transient_data
                    transient_failure["next_actions"] = recovery_actions
                return transient_failure
            if export_vcd and not skip_mcp_vcd:
                return _simulation_vcd_export_failure(raw, command=command, simset=simset, preflight=preflight)
            return _tcl_failure("run_behavioral_simulation", raw)
        data = parse_simulation_result(raw.get("raw", ""))
        data["expected_simulation_invocation_id"] = simulation_invocation_id
        data["requested_vcd_name"] = requested_vcd_name
        data["effective_vcd_name"] = effective_vcd_name
        data["transport_session_generation_id"] = str(raw.get("generation_id", ""))
        source_identity_after = source_identity_after_locked or build_simulation_source_identity(preflight)
        stable_input_identity_after = stable_input_identity_after_locked or build_simulation_stable_input_identity(
            preflight,
            source_identity_after,
        )
        source_identity_matches = (
            source_identity_before.get("status") == "READY"
            and source_identity_after.get("status") == "READY"
            and source_identity_before.get("sha256") == source_identity_after.get("sha256")
        )
        data["simulation_source_identity"] = {
            "recorded_sha256": data.get("simulation_source_identity_sha256", ""),
            "before": source_identity_before,
            "after": source_identity_after,
            "matches": source_identity_matches,
            "comparison_scope": "prelaunch_project_attestation",
            "expected_project_file_update": not source_identity_matches,
        }
        stable_input_identity_matches = (
            stable_input_identity_before.get("status") == "READY"
            and stable_input_identity_after.get("status") == "READY"
            and stable_input_identity_before.get("sha256") == stable_input_identity_after.get("sha256")
        )
        data["simulation_stable_input_identity"] = {
            "recorded_sha256": data.get("simulation_stable_input_identity_sha256", ""),
            "before": stable_input_identity_before,
            "after": stable_input_identity_after,
            "matches": stable_input_identity_matches,
        }
        data["command"] = command
        data["simset"] = simset
        data["preflight"] = preflight
        data["simulation_isolation"] = "trusted_project_host_execution_not_os_sandboxed"
        data["simulation_source_lock"] = source_lock
        data["simulation_output_lock"] = output_lock
        data["managed_simulation_policy_command"] = policy_command if managed_transport else ""
        data["incremental_control"] = "managed_preflight_set_false" if managed_transport else "caller_controlled"
        data["simulation_execution_binding_sha256"] = str(preflight.get("simulation_execution_binding_sha256", ""))
        data["preflight_testbench_vcd_usage"] = testbench_vcd_usage
        data["preflight_testbench_vcd_sources"] = preflight.get("testbench_vcd_sources", [])
        data["export_vcd_requested"] = export_vcd
        data["mcp_vcd_export_mode"] = str(data.get("mcp_vcd_export_mode") or data.get("vcd_export_mode") or ("mcp_open_vcd" if export_vcd else "disabled"))
        data["vcd_export_mode"] = data["mcp_vcd_export_mode"]
        data["testbench_vcd_usage"] = bool(data.get("testbench_vcd_usage")) or testbench_vcd_usage
        data["testbench_vcd_detected"] = bool(data.get("testbench_vcd_detected")) or testbench_vcd_usage
        if data.get("simulation_invocation_id") and data.get("simulation_invocation_id") != simulation_invocation_id:
            data["next_actions"] = [_simulation_retry_action()]
            return failure(
                "run_behavioral_simulation",
                "SIMULATION_INVOCATION_MISMATCH",
                "Simulation result does not belong to the invocation that launched it.",
                data=data,
            )
        if managed_transport and (
            not stable_input_identity_matches
            or data.get("simulation_source_identity_sha256") != source_identity_before.get("sha256")
            or data.get("simulation_stable_input_identity_sha256") != stable_input_identity_before.get("sha256")
            or data.get("session_generation_id") != str(raw.get("generation_id", ""))
        ):
            freshness = data.setdefault("evidence_freshness", {})
            freshness["status"] = "STALE"
            freshness.setdefault("reasons", []).append(
                "simulation source identity or session generation does not match the current managed invocation"
            )
        if self._session_override is None and data.get("evidence_freshness", {}).get("status") != "FRESH":
            data["next_actions"] = [_simulation_retry_action()]
            return failure(
                "run_behavioral_simulation",
                "SIMULATION_EVIDENCE_STALE",
                "Simulation project, simset, source, or invocation evidence is missing or changed.",
                data=data,
            )
        if data["mcp_vcd_export_mode"] == "testbench_existing" and data["testbench_vcd_detected"] and not data.get("vcd_conflict_severity"):
            data["vcd_conflict_severity"] = "info"
        if data.get("vcd_limit_exceeded"):
            data["next_actions"] = [_simulation_retry_action()]
            return failure(
                "run_behavioral_simulation",
                "SIMULATION_VCD_LIMIT_EXCEEDED",
                "Behavioral simulation exceeded the configured VCD size limit.",
                data.get("log_excerpt", ""),
                data,
            )
        if str(data.get("status", "")).lower() != "completed":
            data["next_actions"] = _simulation_failure_repair_actions(simset=simset)
            return failure(
                "run_behavioral_simulation",
                "SIMULATION_FAILED",
                f"Behavioral simulation {data.get('status', 'failed')}.",
                data.get("log_excerpt", ""),
                data,
            )
        return success("run_behavioral_simulation", f"Behavioral simulation {data['status']}.", data)

    def _get_simulation_result(self, args: dict[str, Any]) -> dict[str, Any]:
        simset = str(args.get("simset", "sim_1"))
        try:
            validate_generated_child_name(simset)
        except ValueError as exc:
            return failure(
                "get_simulation_result",
                "SIMULATION_OUTPUT_NAME_INVALID",
                str(exc),
                data={"simset": simset, "policy_allowed": False},
            )
        command = simulation_result_read_command(
            simset=simset,
            status_source=str(args.get("_status_source", "latest_log_tail")),
        )
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("get_simulation_result", raw)
        data = parse_simulation_result(raw.get("raw", ""))
        data["command"] = command
        data["simset"] = simset
        if isinstance(self._session(), GuiTcpVivadoSession):
            current_preflight = self._simulation_vcd_preflight(simset=simset, timeout_s=min(int(args.get("timeout_s", 60)), 60))
            current_state = _simulation_preflight_state(current_preflight)
            current_identity = current_state["source_identity"]
            current_stable_identity = current_state["stable_input_identity"]
            recorded_identity = str(data.get("simulation_source_identity_sha256", ""))
            recorded_stable_identity = str(data.get("simulation_stable_input_identity_sha256", ""))
            current_generation = str(raw.get("generation_id", ""))
            identity_matches = (
                current_stable_identity.get("status") == "READY"
                and bool(recorded_stable_identity)
                and recorded_stable_identity == current_stable_identity.get("sha256")
                and bool(data.get("session_generation_id"))
                and data.get("session_generation_id") == current_generation
            )
            data["simulation_source_identity"] = {
                "recorded_sha256": recorded_identity,
                "current": current_identity,
                "matches": recorded_identity == current_identity.get("sha256"),
                "comparison_scope": "prelaunch_project_attestation",
            }
            data["simulation_stable_input_identity"] = {
                "recorded_sha256": recorded_stable_identity,
                "current": current_stable_identity,
                "matches": identity_matches,
                "comparison_scope": "postlaunch_freshness",
            }
            if not identity_matches:
                freshness = data.setdefault("evidence_freshness", {})
                freshness["status"] = "STALE"
                freshness.setdefault("reasons", []).append(
                    "current simulation compile-order source identity or session generation differs from the recorded invocation"
                )
                return failure(
                    "get_simulation_result",
                    "SIMULATION_EVIDENCE_STALE",
                    "Simulation result does not match the current source identity or Vivado session generation.",
                    data=data,
                )
        return success("get_simulation_result", f"Simulation result: {data['status']}.", data)

    def _simulation_vcd_preflight(self, *, simset: str, timeout_s: int = 60) -> dict[str, Any]:
        command = simulation_vcd_preflight_command(simset=simset)
        raw = self._session().run_tcl(command, timeout_s=timeout_s)
        if not raw.get("ok"):
            return {"ok": False, "simset": simset, "raw": raw.get("raw", ""), "command": command}
        data = parse_simulation_vcd_preflight(raw.get("raw", ""))
        data["command"] = command
        data["simset"] = simset
        return data

    def _simulation_timeout_failure(self, *, simset: str, timeout_s: int, export_vcd: bool, preflight: dict[str, Any], error: TimeoutError) -> dict[str, Any]:
        sim_dir = str(preflight.get("sim_dir", ""))
        artifacts = _scan_simulation_artifacts(sim_dir)
        stop_result: dict[str, Any] = {}
        abort_attempted = self._session_override is None and hasattr(self.manager, "stop")
        if abort_attempted:
            try:
                stop_result = self.manager.stop()
            except Exception as stop_exc:  # noqa: BLE001 - timeout recovery must remain serializable.
                stop_result = {"ok": False, "message": str(stop_exc)}
        data = {
            "status": "failed",
            "simset": simset,
            "sim_dir": sim_dir,
            "log_path": artifacts.get("log_path", ""),
            "vcd_size_bytes": artifacts.get("vcd_total_bytes", 0),
            "artifacts": artifacts,
            "timeout_s_used": timeout_s,
            "abort_attempted": abort_attempted,
            "managed_session_stopped": bool(stop_result.get("stopped") or stop_result.get("ok")) if abort_attempted else False,
            "stop_result": stop_result,
            "preflight": preflight,
            "preflight_testbench_vcd_usage": bool(preflight.get("testbench_vcd_usage", False)),
            "preflight_testbench_vcd_sources": preflight.get("testbench_vcd_sources", []),
            "export_vcd_requested": export_vcd,
            "mcp_vcd_export_mode": "testbench_existing" if preflight.get("testbench_vcd_usage") else "mcp_open_vcd" if export_vcd else "disabled",
            "testbench_vcd_usage": bool(preflight.get("testbench_vcd_usage", False)),
            "simulation_diagnosis": {"primary_cause": "timeout", "causes": ["timeout"], "status": "failed"},
            "diagnosis": {"primary_cause": "timeout", "causes": ["timeout"], "status": "failed"},
            "next_actions": [
                _simulation_retry_action(),
                next_action(
                    "start_session",
                    "Restart the managed Vivado session after timeout recovery stopped it.",
                    preconditions=["The previous run_behavioral_simulation timed out and abort_attempted=true."],
                    stop_condition="start_session returns ok=true.",
                    optional=True,
                ),
                next_action(
                    "open_project",
                    "Reopen the project before retrying simulation after a managed session stop.",
                    required_args=["project_path"],
                    arg_sources={"project_path": "original workflow project .xpr path"},
                    preconditions=["start_session returned ok=true."],
                    stop_condition="open_project returns ok=true.",
                    optional=True,
                ),
            ],
        }
        return failure("run_behavioral_simulation", "TimeoutError", str(error) or "Behavioral simulation timed out.", data=data)

    def _create_ip(self, args: dict[str, Any]) -> dict[str, Any]:
        vlnv = str(args["vlnv"])
        module_name = str(args["module_name"])
        properties = args.get("properties", {})
        command = create_ip_command(
            vlnv=vlnv,
            module_name=module_name,
            ip_dir=args.get("ip_dir"),
            properties=properties,
        )
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 180)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("create_ip", raw)
        status = parse_ip_status(raw.get("raw", ""))
        data = {
            "command": command,
            "ip": status | {"name": module_name, "vlnv": vlnv, "properties": properties},
        }
        return success("create_ip", "Vivado IP created.", data)

    def _configure_ip(self, args: dict[str, Any]) -> dict[str, Any]:
        ip_name = str(args["ip_name"])
        properties = args.get("properties", {})
        command = configure_ip_command(ip_name=ip_name, properties=properties)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("configure_ip", raw)
        status = parse_ip_status(raw.get("raw", ""))
        data = {"command": command, "ip": status | {"name": ip_name, "properties": properties}}
        return success("configure_ip", "Vivado IP configured.", data)

    def _generate_ip_targets(self, args: dict[str, Any]) -> dict[str, Any]:
        ip_name = str(args["ip_name"])
        targets = [str(target) for target in args.get("targets", [])] or None
        command = generate_ip_targets_command(ip_name=ip_name, targets=targets)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 300)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("generate_ip_targets", raw)
        status = parse_ip_status(raw.get("raw", ""))
        data = {"command": command, "targets": targets or ["all"], "ip": status | {"name": ip_name}}
        return success("generate_ip_targets", "Vivado IP targets generated.", data)

    def _get_ip_status(self, args: dict[str, Any]) -> dict[str, Any]:
        ip_name = str(args["ip_name"])
        command = ip_status_command(ip_name=ip_name)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("get_ip_status", raw)
        status = parse_ip_status(raw.get("raw", ""))
        return success("get_ip_status", "Vivado IP status parsed.", {"command": command, "ip": status | {"name": ip_name}})

    def _upgrade_ip(self, args: dict[str, Any]) -> dict[str, Any]:
        ip_name = str(args["ip_name"])
        command = upgrade_ip_command(ip_name=ip_name)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 300)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("upgrade_ip", raw)
        status = parse_ip_status(raw.get("raw", ""))
        return success("upgrade_ip", "Vivado IP upgraded.", {"command": command, "ip": status | {"name": ip_name}})

    def _export_ip_user_files(self, args: dict[str, Any]) -> dict[str, Any]:
        ip_name = str(args["ip_name"])
        command = export_ip_user_files_command(ip_name=ip_name)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 300)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("export_ip_user_files", raw)
        status = parse_ip_status(raw.get("raw", ""))
        return success("export_ip_user_files", "Vivado IP user files exported.", {"command": command, "ip": status | {"name": ip_name}})

    def _create_block_design(self, args: dict[str, Any]) -> dict[str, Any]:
        if bool(args.get("force", False)):
            return failure(
                "create_block_design",
                "DESTRUCTIVE_FORCE_DISABLED",
                "create_block_design force=true is disabled because it can remove an existing block design without an attestable review plan.",
                data={"force": True, "name": str(args.get("name", ""))},
            )
        name = str(args["name"])
        command = create_block_design_command(name=name, force=False)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("create_block_design", raw)
        data = {"command": command, "block_design": parse_key_value_result(raw.get("raw", "")) | {"name": name}}
        return success("create_block_design", "Block design created.", data)

    def _open_block_design(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args["name"])
        command = open_block_design_command(name=name)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("open_block_design", raw)
        data = {"command": command, "block_design": parse_key_value_result(raw.get("raw", "")) | {"name": name}}
        return success("open_block_design", "Block design opened.", data)

    def _add_bd_ip_cell(self, args: dict[str, Any]) -> dict[str, Any]:
        vlnv = str(args["vlnv"])
        cell_name = str(args["cell_name"])
        properties = args.get("properties", {})
        command = add_bd_ip_cell_command(vlnv=vlnv, cell_name=cell_name, properties=properties)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("add_bd_ip_cell", raw)
        data = {"command": command, "cell": {"name": cell_name, "vlnv": vlnv, "properties": properties}}
        return success("add_bd_ip_cell", "Block design IP cell added.", data)

    def _create_bd_port(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args["name"])
        direction = str(args["direction"])
        command = create_bd_port_command(
            name=name,
            direction=direction,
            port_type=args.get("type"),
            from_index=args.get("from"),
            to_index=args.get("to"),
            properties=args.get("properties", {}),
        )
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("create_bd_port", raw)
        data = {"command": command, "port": {"name": name, "direction": direction}}
        return success("create_bd_port", "Block design port created.", data)

    def _connect_bd_net(self, args: dict[str, Any]) -> dict[str, Any]:
        source = str(args["source"])
        targets = [str(target) for target in args.get("targets", [])]
        command = connect_bd_net_command(source=source, targets=targets)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("connect_bd_net", raw)
        data = {"command": command, "connection": {"source": source, "targets": targets}}
        return success("connect_bd_net", "Block design net connected.", data)

    def _connect_bd_intf_net(self, args: dict[str, Any]) -> dict[str, Any]:
        source = str(args["source"])
        targets = [str(target) for target in args.get("targets", [])]
        command = connect_bd_intf_net_command(source=source, targets=targets)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("connect_bd_intf_net", raw)
        data = {"command": command, "connection": {"source": source, "targets": targets}}
        return success("connect_bd_intf_net", "Block design interface net connected.", data)

    def _validate_block_design(self, args: dict[str, Any]) -> dict[str, Any]:
        command = validate_block_design_command(bd_name=args.get("bd_name"))
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("validate_block_design", raw)
        data = parse_block_design_validation(raw.get("raw", ""))
        data["command"] = command
        return success("validate_block_design", f"Block design validation: {data['status']}.", data)

    def _generate_block_design_wrapper(self, args: dict[str, Any]) -> dict[str, Any]:
        bd_name = str(args["bd_name"])
        wrapper_top = args.get("wrapper_top")
        command = generate_block_design_wrapper_command(
            bd_name=bd_name,
            wrapper_top=wrapper_top,
            set_top=bool(args.get("set_top", True)),
        )
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 180)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("generate_block_design_wrapper", raw)
        parsed = parse_key_value_result(raw.get("raw", ""))
        data = {"command": command, "wrapper": parsed | {"bd_name": bd_name, "top": parsed.get("top", wrapper_top or f"{bd_name}_wrapper")}}
        return success("generate_block_design_wrapper", "Block design wrapper generated.", data)

    def _open_project(self, args: dict[str, Any]) -> dict[str, Any]:
        self._project_mutation_scope = "indeterminate"
        project_path = str(args["project_path"])
        project_key = _project_path_key(project_path)
        capability = self._mcp_created_project_capabilities.get(project_key)
        session = self._session()
        managed_transport = isinstance(session, GuiTcpVivadoSession)
        capability_rebound = False
        if managed_transport and capability is not None:
            try:
                current_generation = str(getattr(session, "generation_id", ""))
                if str(capability.get("generation_id", "")) != current_generation:
                    capability = rebind_project_capability_generation(
                        capability,
                        generation_id=current_generation,
                    )
                    self._mcp_created_project_capabilities[project_key] = capability
                    capability_rebound = True
                verify_project_capability(
                    capability,
                    project_path,
                    generation_id=current_generation,
                )
            except (ManagedPathError, OSError, ValueError) as exc:
                self._project_mutation_scope = "unbound"
                self._active_project_capability = None
                self._mcp_created_project_capabilities.pop(project_key, None)
                return failure(
                    "open_project",
                    "PROJECT_CAPABILITY_INVALID",
                    "The requested path no longer identifies the MCP-created project object; the project was not opened.",
                    data={
                        "project_path": project_path,
                        "reason": str(exc),
                        "project_opened": False,
                        "mutation_scope": self._project_mutation_scope,
                    },
                )
        created_by_this_service = capability is not None
        timeout_s = int(args.get("timeout_s", 60))
        open_template = "open_project {project}" if created_by_this_service else "open_project -read_only {project}"
        open_command = safe_tcl(open_template, {"project": project_path})
        opened = session.run_tcl(open_command, timeout_s=timeout_s)
        opened["command"] = open_command
        if not opened.get("ok"):
            self._project_mutation_scope = "unbound"
            return _tcl_failure("open_project", opened)
        guard_command = run_hook_guard_command(close_project_on_block=True)
        guarded = session.run_tcl(guard_command, timeout_s=timeout_s)
        guarded["command"] = f"{open_command}; {guard_command}"
        guarded["open_command"] = open_command
        guarded["guard_command"] = guard_command
        if not guarded.get("ok"):
            self._project_mutation_scope = "unbound"
            close_command = "catch {close_project}"
            try:
                close_result = session.run_tcl(close_command, timeout_s=min(timeout_s, 30))
            except Exception as exc:  # pragma: no cover - defensive cleanup metadata
                close_result = {"ok": False, "message": f"{exc.__class__.__name__}: {exc}"}
            guarded["project_close_attempted"] = True
            guarded["project_close_ok"] = bool(close_result.get("ok"))
            guarded["project_close_command"] = close_command
            guarded["project_close_message"] = str(close_result.get("message", ""))
            return _tcl_failure("open_project", guarded)
        self._project_mutation_scope = "mcp_created_project" if created_by_this_service else "existing_project_read_only"
        self._active_project_capability = capability if created_by_this_service else None
        guarded["mutation_policy"] = {
            "scope": self._project_mutation_scope,
            "origin": "mcp_created_in_current_server_process" if created_by_this_service else "external_existing_project",
            "generation_rebound": capability_rebound,
            "original_project_protected": not created_by_this_service,
            "vivado_read_only": not created_by_this_service,
            "mcp_policy_read_only": not created_by_this_service,
            "immediate_mutations_allowed": created_by_this_service,
            "design_execution_allowed": created_by_this_service,
            "working_copy_required": not created_by_this_service,
        }
        message = (
            "MCP-created project reopened with managed mutation and design execution enabled."
            if created_by_this_service
            else "Project opened with Vivado -read_only and MCP inspection-only policy; mutation and design execution require a separate MCP-managed project."
        )
        return success("open_project", message, guarded)

    def _close_project(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self._session()
        active_capability = self._active_project_capability
        timeout_s = int(args.get("timeout_s", 30))
        data: dict[str, Any] = {}
        refreshed: dict[str, Any] | None = None
        try:
            if isinstance(session, GuiTcpVivadoSession) and active_capability is not None:
                verify_project_capability(
                    active_capability,
                    str(active_capability.get("project_path", "")),
                    generation_id=str(getattr(session, "generation_id", "")),
                    verify_project_content=False,
                )
                with hold_managed_paths_stable(
                    str(active_capability.get("project_root", "")),
                    files=[],
                    directories=[],
                    writable_files=[str(active_capability.get("project_path", ""))],
                ):
                    with session.require_current_project(str(active_capability.get("project_path", ""))):
                        data = session.run_tcl("close_project", timeout_s=timeout_s)
                    if data.get("ok"):
                        refreshed = refresh_project_capability(
                            active_capability,
                            generation_id=str(getattr(session, "generation_id", "")),
                        )
            else:
                data = session.run_tcl("close_project", timeout_s=timeout_s)
        except (ManagedPathError, OSError, ValueError) as exc:
            self._project_mutation_scope = "indeterminate"
            self._active_project_capability = None
            if active_capability is not None:
                self._mcp_created_project_capabilities.pop(str(active_capability.get("project_path_key", "")), None)
            return failure(
                "close_project",
                "PROJECT_CAPABILITY_CLOSE_GUARD_FAILED",
                "The project capability could not remain bound to the same project object while closing.",
                data={
                    **data,
                    "project_closed": bool(data.get("ok")),
                    "reason": str(exc),
                    "mutation_scope": self._project_mutation_scope,
                },
            )
        if not data.get("ok"):
            return _tcl_failure("close_project", data)
        self._project_mutation_scope = "unbound"
        self._active_project_capability = None
        if refreshed is not None:
            self._mcp_created_project_capabilities[refreshed["project_path_key"]] = refreshed
        return success("close_project", "Project close command completed.", data)

    def _get_project_info(self, args: dict[str, Any]) -> dict[str, Any]:
        command = (
            "set p [current_project]; "
            "join [list "
            "\"name=[get_property NAME $p]\" "
            "\"part=[get_property PART $p]\" "
            "\"directory=[get_property DIRECTORY $p]\" "
            "\"top=[get_property TOP [current_fileset]]\""
            "] \"\\n\""
        )
        data = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 30)))
        if not data.get("ok"):
            return _tcl_failure("get_project_info", data)
        data["project"] = _parse_key_value_lines(data.get("raw", ""))
        return success("get_project_info", "Project information collected.", data)

    def _get_project_state(self, args: dict[str, Any]) -> dict[str, Any]:
        command = project_state_command()
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("get_project_state", raw)
        data = parse_project_state(raw.get("raw", ""))
        data["command"] = command
        return success("get_project_state", "Project state collected.", data)

    def _resolve_project_dir(self, args: dict[str, Any], *, timeout_s: int = 60) -> Path | None:
        if args.get("project_dir"):
            return Path(str(args["project_dir"]))
        command = project_state_command()
        raw = self._session().run_tcl(command, timeout_s=timeout_s)
        if not raw.get("ok"):
            return None
        directory = parse_project_state(raw.get("raw", "")).get("project", {}).get("directory", "")
        return Path(directory) if directory else None

    def _list_fileset_files(self, args: dict[str, Any]) -> dict[str, Any]:
        fileset = str(args.get("fileset", "sources_1"))
        command = list_fileset_files_command(fileset=fileset)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("list_fileset_files", raw)
        data = parse_fileset_files(raw.get("raw", ""), fileset=fileset)
        data["command"] = command
        data["fileset"] = fileset
        return success("list_fileset_files", f"Fileset {fileset} files listed.", data)

    def _add_project_files(self, args: dict[str, Any]) -> dict[str, Any]:
        fileset = str(args.get("fileset", "sources_1"))
        files = [str(path) for path in args.get("files", [])]
        composite_failure = _composite_project_input_failure("add_project_files", files)
        if composite_failure:
            return composite_failure
        if "constr" in fileset.lower():
            constraint_failure = _constraint_input_failure("add_project_files", files)
            if constraint_failure:
                return constraint_failure
        command = add_project_files_command(
            fileset=fileset,
            files=files,
            copy_to_project=bool(args.get("copy_to_project", False)),
        )
        timeout_s = int(args.get("timeout_s", 120))
        try:
            raw = self._session().run_tcl(command, timeout_s=timeout_s)
        except TimeoutError as exc:
            context = {
                "fileset": fileset,
                "files": files,
                "copy_to_project": bool(args.get("copy_to_project", False)),
            }
            return self._timeout_failure(
                "add_project_files",
                exc,
                timeout_s=timeout_s,
                command=command,
                request_context=context,
                partial_context={
                    "partial_success": False,
                    "fileset": fileset,
                    "files": files,
                    "copy_to_project": bool(args.get("copy_to_project", False)),
                    "project_state_hint": {"recommended_probe": "list_fileset_files", "fileset": fileset},
                },
                next_actions=_setup_repair_actions(context | {"tool": "add_project_files"}),
            )
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("add_project_files", raw)
        data = {
            "command": command,
            "fileset": fileset,
            "files": files,
            "copy_to_project": bool(args.get("copy_to_project", False)),
            "result": _parse_key_value_lines(raw.get("raw", "")),
        }
        return success("add_project_files", f"Added {len(files)} file(s) to {fileset}.", data)

    def _remove_project_files(self, args: dict[str, Any]) -> dict[str, Any]:
        fileset = str(args.get("fileset", "sources_1"))
        files = [str(path) for path in args.get("files", [])]
        command = remove_project_files_command(fileset=fileset, files=files)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("remove_project_files", raw)
        data = {
            "command": command,
            "fileset": fileset,
            "files": files,
            "result": _parse_key_value_lines(raw.get("raw", "")),
        }
        return success("remove_project_files", f"Removed {len(files)} file reference(s) from {fileset}.", data)

    def _set_project_top(self, args: dict[str, Any]) -> dict[str, Any]:
        top = str(args["top"])
        fileset = str(args.get("fileset", "sources_1"))
        command = set_project_top_command(top=top, fileset=fileset)
        timeout_s = int(args.get("timeout_s", 60))
        try:
            raw = self._session().run_tcl(command, timeout_s=timeout_s)
        except TimeoutError as exc:
            context = {"top": top, "fileset": fileset}
            return self._timeout_failure(
                "set_project_top",
                exc,
                timeout_s=timeout_s,
                command=command,
                request_context=context,
                partial_context={
                    "partial_success": False,
                    "project_state_hint": {"recommended_probe": "get_project_state", "fileset": fileset},
                },
                next_actions=_setup_repair_actions(context | {"tool": "set_project_top"}),
            )
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("set_project_top", raw)
        data = {"command": command, "top": top, "fileset": fileset, "result": _parse_key_value_lines(raw.get("raw", ""))}
        return success("set_project_top", f"Project top set for {fileset}.", data)

    def _set_project_part(self, args: dict[str, Any]) -> dict[str, Any]:
        part = str(args["part"])
        command = set_project_part_command(part=part)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("set_project_part", raw)
        data = parse_run_refresh_rows(raw.get("raw", ""))
        data["command"] = command
        data["part"] = part
        return success("set_project_part", "Project part updated.", data)

    def _update_project_compile_order(self, args: dict[str, Any]) -> dict[str, Any]:
        filesets = [str(fileset) for fileset in args.get("filesets", [])] or None
        command = update_compile_order_command(filesets=filesets)
        timeout_s = int(args.get("timeout_s", 60))
        try:
            raw = self._session().run_tcl(command, timeout_s=timeout_s)
        except TimeoutError as exc:
            context = {"filesets": filesets or ["sources_1", "sim_1"]}
            return self._timeout_failure(
                "update_project_compile_order",
                exc,
                timeout_s=timeout_s,
                command=command,
                request_context=context,
                partial_context={
                    "partial_success": False,
                    "project_state_hint": {"recommended_probe": "get_compile_order", "filesets": context["filesets"]},
                },
                next_actions=_setup_repair_actions(context | {"tool": "update_project_compile_order"}),
            )
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("update_project_compile_order", raw)
        data = {"command": command, "filesets": filesets or ["sources_1", "sim_1"], "result": _parse_key_value_lines(raw.get("raw", ""))}
        return success("update_project_compile_order", "Project compile order updated.", data)

    def _check_syntax(self, args: dict[str, Any]) -> dict[str, Any]:
        fileset = str(args.get("fileset", "sources_1"))
        command = check_syntax_command(fileset=fileset)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("check_syntax", raw)
        data = parse_syntax_report(raw.get("raw", ""))
        data["command"] = command
        data["fileset"] = fileset
        return success("check_syntax", f"Syntax check status: {data['status']}.", data)

    def _get_compile_order(self, args: dict[str, Any]) -> dict[str, Any]:
        fileset = str(args.get("fileset", "sources_1"))
        command = compile_order_command(fileset=fileset)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("get_compile_order", raw)
        data = parse_compile_order(raw.get("raw", ""))
        data["command"] = command
        data["fileset"] = fileset
        return success("get_compile_order", f"Compile order status: {data['status']}.", data)

    def _analyze_sources(self, args: dict[str, Any]) -> dict[str, Any]:
        syntax_result = self._check_syntax(args)
        if not syntax_result.get("ok"):
            return _analysis_input_failure("check_syntax", syntax_result, tool="analyze_sources")
        order_result = self._get_compile_order(args)
        if not order_result.get("ok"):
            return _analysis_input_failure("get_compile_order", order_result, tool="analyze_sources")
        data = analyze_sources_result(syntax=syntax_result["data"], compile_order=order_result["data"])
        return success("analyze_sources", f"Source analysis status: {data['status']}.", data)

    def _run_elaboration(self, args: dict[str, Any]) -> dict[str, Any]:
        command = run_elaboration_command(top=args.get("top"), part=args.get("part"))
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 180)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("run_elaboration", raw)
        data = parse_elaboration_result(raw.get("raw", ""))
        data["command"] = command
        return success("run_elaboration", f"Elaboration status: {data['status']}.", data)

    def _get_elaboration_result(self, args: dict[str, Any]) -> dict[str, Any]:
        command = elaboration_result_command()
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("get_elaboration_result", raw)
        data = parse_elaboration_result(raw.get("raw", ""))
        data["command"] = command
        return success("get_elaboration_result", f"Elaboration result status: {data['status']}.", data)

    def _get_design_hierarchy(self, args: dict[str, Any]) -> dict[str, Any]:
        command = design_hierarchy_command()
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("get_design_hierarchy", raw)
        data = parse_design_hierarchy(raw.get("raw", ""))
        data["command"] = command
        return success("get_design_hierarchy", "Design hierarchy collected.", data)

    def _get_run_configuration(self, args: dict[str, Any]) -> dict[str, Any]:
        run_name = str(args.get("run_name", "impl_1"))
        command = get_run_configuration_command(run_name=run_name)
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 60)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("get_run_configuration", raw)
        data = parse_run_configuration(raw.get("raw", ""))
        if isinstance(data.get("run"), dict):
            data["run"]["session_generation_id"] = str(
                raw.get("generation_id") or data["run"].get("session_generation_id", "")
            )
        data["command"] = command
        data["run_name"] = run_name
        return success("get_run_configuration", f"Run {run_name} configuration collected.", data)

    def _configure_run(self, args: dict[str, Any]) -> dict[str, Any]:
        run_name = str(args["run_name"])
        properties = args.get("properties", {})
        if not isinstance(properties, dict):
            return failure(
                "configure_run",
                "RUN_PROPERTIES_INVALID",
                "configure_run.properties must be a JSON object.",
                data={"run_name": run_name, "allowed_properties": sorted(ALLOWED_RUN_PROPERTIES)},
            )
        try:
            validate_run_properties(properties)
        except ValueError as exc:
            return failure(
                "configure_run",
                "RUN_PROPERTY_NOT_ALLOWED",
                str(exc),
                data={
                    "run_name": run_name,
                    "rejected_properties": sorted(str(key) for key in properties),
                    "allowed_properties": sorted(ALLOWED_RUN_PROPERTIES),
                    "allowed_patterns": ["STEPS.<STEP>.ARGS.<ARG>", "STEPS.<STEP>.IS_ENABLED"],
                    "policy": "Tcl hook and script properties are permanently blocked; other properties require the explicit allowlist.",
                },
            )
        command = configure_run_command(
            run_name=run_name,
            strategy=args.get("strategy"),
            properties=properties,
        )
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("configure_run", raw)
        data = {"command": command, "run_name": run_name, "result": _parse_key_value_lines(raw.get("raw", ""))}
        return success("configure_run", f"Run {run_name} configured.", data)

    def _reset_runs(self, args: dict[str, Any]) -> dict[str, Any]:
        run_names = [str(run_name) for run_name in args.get("run_names", [])] or None
        command = reset_runs_command(run_names=run_names)
        data = {
            "status": "DRY_RUN" if bool(args.get("dry_run", True)) else "PENDING_EXECUTION",
            "dry_run": bool(args.get("dry_run", True)),
            "command": command,
            "run_names": run_names or ["synth_1", "impl_1"],
            "intent": str(args.get("intent", "")),
        }
        if bool(args.get("dry_run", True)):
            data["next_actions"] = [_destructive_confirm_action("reset_runs", "RESET_RUNS")]
            return success("reset_runs", "Vivado run reset dry-run prepared.", data)
        if str(args.get("confirm", "")) != "RESET_RUNS" or not str(args.get("intent", "")).strip():
            return failure(
                "reset_runs",
                "DESTRUCTIVE_CONFIRMATION_REQUIRED",
                "reset_runs requires dry_run=false, intent, and confirm=RESET_RUNS before resetting Vivado runs.",
                data={**data, "next_actions": [_destructive_confirm_action("reset_runs", "RESET_RUNS")]},
            )
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("reset_runs", raw)
        result = parse_run_rows(raw.get("raw", ""))
        result["command"] = command
        result["run_names"] = run_names or ["synth_1", "impl_1"]
        result["dry_run"] = False
        result["intent"] = str(args.get("intent", ""))
        return success("reset_runs", "Vivado runs reset.", result)

    def _clean_run_outputs(self, args: dict[str, Any]) -> dict[str, Any]:
        simsets = [str(simset) for simset in args.get("simsets", [])]
        run_names = [str(run_name) for run_name in args.get("run_names", [])] or ([] if simsets else ["synth_1", "impl_1"])
        include_cache = bool(args.get("include_cache", False))
        include_gen = bool(args.get("include_gen", False))
        command = clean_run_outputs_command(
            run_names=run_names,
            simsets=simsets or None,
            include_cache=include_cache,
            include_gen=include_gen,
        )
        dry_run = bool(args.get("dry_run", True))
        data = {
            "status": "DRY_RUN" if dry_run else "PENDING_EXECUTION",
            "dry_run": dry_run,
            "executed": False,
            "run_names": run_names,
            "simsets": simsets,
            "include_cache": include_cache,
            "include_gen": include_gen,
            "planned_targets": _planned_clean_run_output_targets(run_names, simsets, include_cache=include_cache, include_gen=include_gen),
            "intent": str(args.get("intent", "")),
        }
        if dry_run:
            data["next_actions"] = [_destructive_confirm_action("clean_run_outputs", "CLEAN_RUN_OUTPUTS")]
            return success("clean_run_outputs", "Vivado generated output cleanup dry-run prepared.", data)
        data["command"] = command
        if str(args.get("confirm", "")) != "CLEAN_RUN_OUTPUTS" or not str(args.get("intent", "")).strip():
            return failure(
                "clean_run_outputs",
                "DESTRUCTIVE_CONFIRMATION_REQUIRED",
                "clean_run_outputs requires dry_run=false, intent, and confirm=CLEAN_RUN_OUTPUTS before deleting generated outputs.",
                data={**data, "next_actions": [_destructive_confirm_action("clean_run_outputs", "CLEAN_RUN_OUTPUTS")]},
            )
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("clean_run_outputs", raw)
        context = parse_clean_outputs(raw.get("raw", ""))
        project_dir = Path(str(context.get("project_dir", ""))).resolve()
        if not context.get("project_name") or not project_dir.is_dir():
            return failure(
                "clean_run_outputs",
                "CLEAN_OUTPUT_CONTEXT_INVALID",
                "Vivado did not return a usable current project identity for generated-output cleanup.",
                data={**data, "context": context},
            )
        snapshots: list[tuple[Path, list[dict[str, Any]]]] = []
        try:
            for raw_target in context.get("targets", []):
                target = validate_generated_clean_target(project_dir, raw_target)
                if os.path.lexists(target):
                    snapshots.append((target, snapshot_managed_tree(project_dir, target)))
            deleted = {"file_count": 0, "dir_count": 0, "bytes": 0}
            for target, snapshot in snapshots:
                counts = delete_managed_snapshot(project_dir, target, snapshot)
                for key in deleted:
                    deleted[key] += int(counts.get(key, 0))
        except (ManagedPathError, OSError, ValueError) as exc:
            return failure(
                "clean_run_outputs",
                "CLEAN_OUTPUT_IDENTITY_CHANGED",
                f"Generated-output cleanup was blocked by the managed filesystem broker: {exc}",
                data={**data, "context": context, "broker_error": str(exc)},
            )
        result = {
            **context,
            "command": command,
            "dry_run": False,
            "executed": True,
            "intent": str(args.get("intent", "")),
            "run_names": run_names,
            "simsets": simsets,
            "include_cache": include_cache,
            "include_gen": include_gen,
            "deleted": [str(target) for target, _ in snapshots],
            "deleted_file_count": deleted["file_count"],
            "deleted_directory_count": deleted["dir_count"],
            "released_bytes": deleted["bytes"],
            "deletion_backend": "python_managed_snapshot_broker",
        }
        return success("clean_run_outputs", "Vivado generated run outputs cleaned.", result)

    def _collect_build_artifacts(self, args: dict[str, Any]) -> dict[str, Any]:
        run_name = str(args.get("run_name", "impl_1"))
        timeout_s = int(args.get("timeout_s", 60))
        identity_result = self._evidence_design_execution_identity(
            run_name=run_name,
            timeout_s=timeout_s,
            require_terminal_launch=True,
        )
        if not identity_result.get("ok"):
            return failure(
                "collect_build_artifacts",
                str(identity_result.get("error_code") or "DESIGN_EXECUTION_IDENTITY_BLOCKED"),
                str(identity_result.get("message") or "Design execution identity is unavailable."),
                data=dict(identity_result.get("data") or {}),
            )
        design_identity = identity_result["data"]["design_execution_identity"]
        command = artifact_context_command(run_name=run_name)
        raw = self._session().run_tcl(command, timeout_s=timeout_s)
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("collect_build_artifacts", raw)
        values = _parse_key_value_lines(raw.get("raw", ""))
        values["run_bitstream_files"] = decode_wire_list(str(values.get("run_bitstream_files", "")))
        values["session_generation_id"] = str(raw.get("generation_id") or values.get("session_generation_id", ""))
        blockers = artifact_run_blockers(values)
        if blockers:
            return failure(
                "collect_build_artifacts",
                "ARTIFACT_RUN_NOT_READY",
                "Build artifacts require a completed, current write_bitstream run.",
                data={
                    "command": command,
                    "context": values,
                    "blockers": blockers,
                    "next_actions": [
                        next_action(
                            "get_run_progress",
                            "Verify or complete the implementation/bitstream run before collecting artifacts.",
                            required_args=["run_name"],
                            arg_sources={"run_name": run_name},
                            preconditions=["The requested Vivado run exists."],
                            stop_condition="get_run_progress reports terminal completion, 100% progress, a bitstream, and no refresh requirement.",
                        )
                    ],
                },
            )
        try:
            data = collect_artifacts(
                project_dir=values["project_dir"],
                run_dir=values["run_dir"],
                run_name=run_name,
                output_dir=args.get("output_dir"),
                run_context=values,
                design_execution_identity=design_identity,
                collection_id=f"artifact_{uuid.uuid4().hex}",
            )
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError) and "write outside project directory" in str(exc):
                return _artifact_output_dir_outside_project_failure(
                    command=command,
                    context=values,
                    run_name=run_name,
                    requested_output_dir=args.get("output_dir"),
                    message=str(exc),
                )
            return failure("collect_build_artifacts", exc.__class__.__name__, str(exc), data={"command": command, "context": values})
        data["command"] = command
        data["context"] = values
        if data.get("status") != "READY":
            data["next_actions"] = [
                next_action(
                    "get_run_progress",
                    "Re-read the target run state and canonical bitstream evidence before retrying artifact collection.",
                    required_args=["run_name"],
                    arg_sources={"run_name": run_name},
                    preconditions=["Review collect_build_artifacts.data.evidence_freshness.reasons."],
                    stop_condition="get_run_progress reports terminal completion, 100% progress, a canonical bitstream, and no refresh requirement.",
                ),
                next_action(
                    "collect_build_artifacts",
                    "Retry artifact collection only after every stale-evidence reason has been resolved.",
                    required_args=["run_name"],
                    arg_sources={"run_name": run_name},
                    preconditions=["The completed run and write_bitstream execution markers are current and internally consistent."],
                    stop_condition="collect_build_artifacts returns ok=true and evidence_freshness.status=FRESH.",
                ),
            ]
            return failure(
                "collect_build_artifacts",
                "ARTIFACT_EVIDENCE_STALE",
                "Build artifacts could not be bound to the current completed Vivado run.",
                data=data,
            )
        data["next_actions"] = _post_artifact_handoff_actions(run_name, manifest_path=str(data.get("manifest_path", "")))
        return success("collect_build_artifacts", f"Collected {len(data['artifacts'])} build artifact(s).", data)

    def _load_validated_artifact_manifest(
        self,
        args: dict[str, Any],
        *,
        tool: str,
    ) -> dict[str, Any]:
        run_name = str(args.get("run_name", "impl_1"))
        manifest_path = args.get("manifest_path")
        timeout_s = int(args.get("timeout_s", 60))
        identity_result = self._evidence_design_execution_identity(
            run_name=run_name,
            timeout_s=timeout_s,
            require_terminal_launch=False,
        )
        if not identity_result.get("ok"):
            return failure(
                tool,
                str(identity_result.get("error_code") or "DESIGN_EXECUTION_IDENTITY_BLOCKED"),
                str(identity_result.get("message") or "Design execution identity is unavailable."),
                data=dict(identity_result.get("data") or {}),
            )
        design_identity = identity_result["data"]["design_execution_identity"]
        command = artifact_context_command(run_name=run_name)
        raw = self._session().run_tcl(command, timeout_s=timeout_s)
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure(tool, raw)
        context = _parse_key_value_lines(raw.get("raw", ""))
        context["run_bitstream_files"] = decode_wire_list(str(context.get("run_bitstream_files", "")))
        context["session_generation_id"] = str(raw.get("generation_id") or context.get("session_generation_id", ""))
        if manifest_path is None:
            manifest_path = _resolve_artifact_manifest_path(
                context.get("project_dir", ""),
                run_name,
                "",
            )
        else:
            manifest_path = _resolve_manifest_path_argument(manifest_path)
        try:
            manifest_path = resolve_artifact_manifest_for_read(
                manifest_path,
                project_dir=context.get("project_dir", ""),
            )
            loaded, manifest_sha256 = load_artifact_manifest_with_sha256(
                manifest_path,
                project_dir=context.get("project_dir", ""),
            )
            data = validate_artifact_manifest(
                loaded,
                manifest_path=str(manifest_path),
                project_dir=context.get("project_dir", ""),
                run_name=run_name,
                current_run_context=context,
                current_design_execution_identity=design_identity,
            )
            data["manifest_sha256"] = manifest_sha256
        except OSError as exc:
            return failure(tool, "MANIFEST_NOT_FOUND", str(exc), data={"command": command, "manifest_path": manifest_path})
        except (ValueError, json.JSONDecodeError) as exc:
            allowed_root = Path(str(context.get("project_dir", ""))) / "vmcp_artifacts"
            return failure(
                tool,
                "ARTIFACT_MANIFEST_REJECTED",
                f"Artifact manifest must remain under {allowed_root}: {exc}",
                data={
                    "command": command,
                    "manifest_path": str(manifest_path),
                    "allowed_manifest_root": str(allowed_root),
                    "context": context,
                },
            )
        data["command"] = command
        return success(tool, "Artifact manifest loaded and strictly validated.", data)

    def _get_artifact_manifest(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._load_validated_artifact_manifest(args, tool="get_artifact_manifest")

    def _collect_report_bundle(self, args: dict[str, Any]) -> dict[str, Any]:
        run_name = str(args.get("run_name", "impl_1"))
        timeout_s = int(args.get("timeout_s", 180))
        try:
            validate_generated_child_name(run_name)
        except ValueError as exc:
            return failure(
                "collect_report_bundle",
                "REPORT_RUN_NAME_INVALID",
                str(exc),
                data={"run_name": run_name, "policy_allowed": False},
            )
        identity_result = self._evidence_design_execution_identity(
            run_name=run_name,
            timeout_s=timeout_s,
            require_terminal_launch=True,
        )
        if not identity_result.get("ok"):
            return failure(
                "collect_report_bundle",
                str(identity_result.get("error_code") or "DESIGN_EXECUTION_IDENTITY_BLOCKED"),
                str(identity_result.get("message") or "Design execution identity is unavailable."),
                data=dict(identity_result.get("data") or {}),
            )
        design_identity = identity_result["data"]["design_execution_identity"]
        collection_id = f"report_{uuid.uuid4().hex}"
        command = report_bundle_command(run_name=run_name, collection_id=collection_id)
        session = self._session()
        if isinstance(session, GuiTcpVivadoSession):
            project_dir = self._resolve_project_dir(args, timeout_s=min(int(args.get("timeout_s", 180)), 60))
            if project_dir is None:
                return failure(
                    "collect_report_bundle",
                    "REPORT_PROJECT_CONTEXT_UNAVAILABLE",
                    "Could not resolve the current project directory before preparing report output.",
                )
            report_dir = project_dir / "vmcp_reports" / run_name / "invocations" / collection_id
            ensure_managed_directory(project_dir, report_dir)
            output_directories = [
                project_dir / "vmcp_reports",
                project_dir / "vmcp_reports" / run_name,
                project_dir / "vmcp_reports" / run_name / "invocations",
                report_dir,
            ]
            try:
                with hold_managed_output_directories(project_dir, directories=[str(path) for path in output_directories]):
                    if any(report_dir.iterdir()):
                        raise ManagedPathError("report invocation directory was not empty before broker preparation")
                    report_paths = [report_dir / name for name in REPORT_CATEGORIES]
                    for report_path in report_paths:
                        atomic_write_bytes(project_dir, report_path, b"")
                    with hold_managed_paths_stable(
                        project_dir,
                        files=[],
                        directories=[],
                        writable_files=report_paths,
                    ):
                        raw = session.run_tcl(command, timeout_s=int(args.get("timeout_s", 180)))
            except ManagedPathError as exc:
                return failure(
                    "collect_report_bundle",
                    "REPORT_OUTPUT_IDENTITY_UNSTABLE",
                    str(exc),
                    data={"project_dir": str(project_dir), "report_dir": str(report_dir)},
                )
        else:
            raw = session.run_tcl(command, timeout_s=int(args.get("timeout_s", 180)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("collect_report_bundle", raw)
        context = parse_report_bundle_context(raw.get("raw", ""))
        context["session_generation_id"] = str(raw.get("generation_id") or context.get("session_generation_id", ""))
        try:
            report_dir = _resolve_report_dir_argument(args.get("report_dir"), context.get("report_dir"), run_name)
        except ValueError as exc:
            return failure(
                "collect_report_bundle",
                "REPORT_DIR_CONTEXT_MISMATCH",
                str(exc),
                data={
                    "command": command,
                    "context": context,
                    "requested_report_dir": str(args.get("report_dir") or ""),
                    "current_invocation_report_dir": str(context.get("report_dir") or ""),
                },
            )
        if str(context.get("collection_id", "")) and str(context.get("collection_id")) != collection_id:
            return failure(
                "collect_report_bundle",
                "REPORT_COLLECTION_ID_MISMATCH",
                "Vivado report collection context did not match the requested invocation.",
                data={"command": command, "context": context, "expected_collection_id": collection_id},
            )
        try:
            data = collect_report_bundle_files(
                report_dir=str(report_dir),
                run_name=run_name,
                project_dir=context.get("project_dir") or None,
                report_context=context,
                design_execution_identity=design_identity,
            )
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError) and "outside project directory" in str(exc):
                return _report_output_dir_outside_project_failure(
                    command=command,
                    context=context,
                    run_name=run_name,
                    requested_report_dir=args.get("report_dir"),
                    resolved_report_dir=report_dir,
                    message=str(exc),
                )
            return failure("collect_report_bundle", exc.__class__.__name__, str(exc), data={"command": command, "context": context})
        data["command"] = command
        data["context"] = context
        return success("collect_report_bundle", f"Collected {len(data['reports'])} report file(s).", data)

    def _list_signoff_waivers(self, args: dict[str, Any]) -> dict[str, Any]:
        project_dir = self._resolve_project_dir(args, timeout_s=int(args.get("timeout_s", 60)))
        if project_dir is None:
            return failure("list_signoff_waivers", "PROJECT_DIR_UNAVAILABLE", "Could not resolve current Vivado project directory.")
        try:
            waivers = load_waivers(project_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return failure("list_signoff_waivers", "WAIVER_READ_FAILED", str(exc), data={"project_dir": str(project_dir)})
        data = {
            "project_dir": str(project_dir),
            "waiver_path": str(waiver_path(project_dir)),
            "waiver_count": len(waivers),
            "waivers": waivers,
        }
        return success("list_signoff_waivers", f"Loaded {len(waivers)} signoff waiver(s).", data)

    def _create_signoff_waiver(self, args: dict[str, Any]) -> dict[str, Any]:
        project_dir = self._resolve_project_dir(args, timeout_s=int(args.get("timeout_s", 60)))
        if project_dir is None:
            return failure("create_signoff_waiver", "PROJECT_DIR_UNAVAILABLE", "Could not resolve current Vivado project directory.")
        waiver_id = str(args["id"])
        try:
            waiver = create_waiver(
                project_dir=project_dir,
                waiver_id=waiver_id,
                finding_fingerprint=str(args["finding_fingerprint"]),
                evidence_identity_sha256=str(args["evidence_identity_sha256"]),
                code=args.get("code"),
                message_contains=args.get("message_contains"),
                source_tool=args.get("source_tool"),
                reason=str(args.get("reason", "")),
                owner=str(args.get("owner", "")),
                expires_on=args.get("expires_on"),
                enabled=bool(args.get("enabled", True)),
            )
        except ValueError as exc:
            error_code = "WAIVER_ALREADY_EXISTS" if "already exists" in str(exc) else "WAIVER_INVALID"
            return failure("create_signoff_waiver", error_code, str(exc), data={"project_dir": str(project_dir), "id": waiver_id})
        except OSError as exc:
            return failure("create_signoff_waiver", exc.__class__.__name__, str(exc), data={"project_dir": str(project_dir), "id": waiver_id})
        return success(
            "create_signoff_waiver",
            f"Signoff waiver created: {waiver_id}.",
            {"project_dir": str(project_dir), "waiver_path": str(waiver_path(project_dir)), "waiver": waiver},
        )

    def _remove_signoff_waiver(self, args: dict[str, Any]) -> dict[str, Any]:
        project_dir = self._resolve_project_dir(args, timeout_s=int(args.get("timeout_s", 60)))
        if project_dir is None:
            return failure("remove_signoff_waiver", "PROJECT_DIR_UNAVAILABLE", "Could not resolve current Vivado project directory.")
        waiver_id = str(args["id"])
        try:
            removed = remove_waiver(project_dir=project_dir, waiver_id=waiver_id)
            waivers = load_waivers(project_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return failure("remove_signoff_waiver", "WAIVER_WRITE_FAILED", str(exc), data={"project_dir": str(project_dir), "id": waiver_id})
        return success(
            "remove_signoff_waiver",
            f"Signoff waiver {'removed' if removed else 'not found'}: {waiver_id}.",
            {"project_dir": str(project_dir), "waiver_path": str(waiver_path(project_dir)), "id": waiver_id, "removed": removed, "waiver_count": len(waivers)},
        )

    def _run_project_audit(self, args: dict[str, Any]) -> dict[str, Any]:
        timeout_s = int(args.get("timeout_s", 180))
        environment = self._find_vivado(args.get("vivado_path"))
        completed_steps: list[str] = []
        current_step = "get_project_state"
        project_state: dict[str, Any] = {}
        project_dir_text = ""
        partial_evidence: dict[str, Any] = {
            "environment": {
                "ok": environment.get("ok"),
                "path": environment.get("path"),
                "version": environment.get("version"),
            }
        }
        try:
            project_state_result = self._get_project_state(args)
            if not project_state_result.get("ok"):
                return _analysis_input_failure("get_project_state", project_state_result, tool="run_project_audit")
            project_state = project_state_result["data"]
            partial_evidence["project_state"] = project_state
            completed_steps.append(current_step)
            project_dir_text = project_state.get("project", {}).get("directory", "")
            waivers = _load_waivers_safe(project_dir_text)
            partial_evidence["waivers"] = {"count": len(waivers)}

            current_step = "analyze_sources"
            sources = _data_or_finding("analyze_sources", self._analyze_sources(args))
            partial_evidence["sources"] = sources
            completed_steps.append(current_step)

            current_step = "get_constraints_summary"
            constraints = _data_or_finding("get_constraints_summary", self._get_constraints_summary(args))
            partial_evidence["constraints"] = constraints
            completed_steps.append(current_step)

            current_step = "collect_ip_status"
            ip_status = self._collect_ip_audit(timeout_s=timeout_s)
            partial_evidence["ip_status"] = ip_status
            completed_steps.append(current_step)

            current_step = "collect_bd_validation"
            bd_validation = self._collect_bd_audit(timeout_s=timeout_s)
            partial_evidence["bd_validation"] = bd_validation
            completed_steps.append(current_step)

            current_step = "collect_run_configurations"
            run_configurations = self._collect_run_configuration_audit(timeout_s=timeout_s)
            partial_evidence["run_configurations"] = run_configurations
            completed_steps.append(current_step)

            current_step = "load_manifests"
            artifact_result = self._get_artifact_manifest(
                {
                    "run_name": str(args.get("run_name", "impl_1")),
                    "timeout_s": timeout_s,
                }
            )
            artifact_manifest = artifact_result.get("data") if artifact_result.get("ok") else None
            artifact_validation = {
                "status": "READY" if artifact_result.get("ok") else "BLOCK",
                "error_code": str(artifact_result.get("error_code", "")),
                "message": str(artifact_result.get("message", "")),
            }
            try:
                report_manifest = self._load_existing_report_manifest(
                    args,
                    run_name=str(args.get("run_name", "impl_1")),
                    project_dir=project_dir_text,
                    timeout_s=timeout_s,
                )
            except ReportManifestValidationError as exc:
                return _report_manifest_validation_failure("run_project_audit", exc)
            partial_evidence["artifact_manifest_present"] = artifact_manifest is not None
            partial_evidence["artifact_manifest_validation"] = artifact_validation
            partial_evidence["report_manifest_present"] = report_manifest is not None
            completed_steps.append(current_step)

            current_step = "run_pre_hw_signoff"
            signoff_args = args | {"apply_waivers": bool(args.get("apply_waivers", True)), "project_dir": project_dir_text}
            if report_manifest is not None:
                signoff_args["report_manifest_path"] = str(report_manifest.get("manifest_path", ""))
            signoff = _data_or_finding("run_pre_hw_signoff", self._run_pre_hw_signoff(signoff_args))
            if report_manifest is None and isinstance(signoff.get("report_bundle"), dict) and signoff["report_bundle"].get("reports"):
                report_manifest = signoff["report_bundle"]
            partial_evidence["signoff"] = signoff
            completed_steps.append(current_step)
        except TimeoutError as exc:
            project_path = _project_path_from_state(project_state)
            return self._timeout_failure(
                "run_project_audit",
                exc,
                timeout_s=timeout_s,
                command=f"run_project_audit stage={current_step}",
                request_context={"run_name": str(args.get("run_name", "impl_1")), "apply_waivers": bool(args.get("apply_waivers", True))},
                partial_context={
                    "status": "BLOCK",
                    "partial_success": bool(completed_steps),
                    "current_step": current_step,
                    "completed_steps": completed_steps,
                    "partial_evidence": partial_evidence,
                    "project_dir": project_dir_text,
                    "project_path": project_path,
                },
                next_actions=_project_audit_timeout_actions(project_path=project_path),
            )

        data = evaluate_project_audit(
            environment=environment,
            project_state=project_state,
            sources=sources,
            constraints=constraints,
            ip_status=ip_status,
            bd_validation=bd_validation,
            run_configurations=run_configurations,
            artifact_manifest=artifact_manifest,
            artifact_validation=artifact_validation,
            report_manifest=report_manifest,
            signoff=signoff,
            waivers=waivers,
        )
        return success("run_project_audit", f"Project audit status: {data.get('effective_status', data['status'])}.", data)

    def _collect_diagnostic_bundle(self, args: dict[str, Any]) -> dict[str, Any]:
        timeout_s = int(args.get("timeout_s", 180))
        run_name = str(args.get("run_name", "impl_1"))
        reuse_audit_from_manifest = args.get("reuse_audit_from_manifest")
        progress_context = _diagnostic_progress_context(args, run_name=run_name, timeout_s=timeout_s)

        def mark(step: str) -> None:
            progress_context["current_step"] = step

        def done(artifact: str) -> None:
            progress_context["last_successful_artifact"] = artifact

        run_configurations: dict[str, Any] | None = None
        try:
            mark("get_project_state")
            project_state_result = self._get_project_state(args)
            if not project_state_result.get("ok"):
                progress_context["failed_result"] = project_state_result
                return _diagnostic_collection_failure("ANALYSIS_INPUT_FAILED", "Could not collect get_project_state.", progress_context, project_state_result.get("raw_excerpt", ""))
            project_state = project_state_result["data"]
            project_dir = project_state.get("project", {}).get("directory", "")
            if not project_dir:
                return failure("collect_diagnostic_bundle", "PROJECT_DIR_UNAVAILABLE", "Could not resolve current Vivado project directory.", data=progress_context)
            progress_context["project_dir"] = str(project_dir)
            progress_context["partial_output_dir"] = _planned_diagnostic_bundle_dir(project_dir, args)
            done("project_state")

            mark("detect_vivado_environment")
            environment = self._find_vivado(args.get("vivado_path"))
            done("vivado_environment")

            if reuse_audit_from_manifest:
                mark("capture_design_execution_identity")
                identity_result = self._evidence_design_execution_identity(
                    run_name=run_name,
                    timeout_s=timeout_s,
                    require_terminal_launch=False,
                )
                if not identity_result.get("ok"):
                    progress_context["failed_result"] = identity_result
                    return _diagnostic_collection_failure(
                        str(identity_result.get("error_code") or "DESIGN_EXECUTION_IDENTITY_BLOCKED"),
                        str(identity_result.get("message") or "Could not capture design execution identity."),
                        progress_context,
                        str(identity_result.get("raw_excerpt", "")),
                    )
                design_execution_identity = identity_result["data"]["design_execution_identity"]
                done("design_execution_identity")
                mark("collect_current_run_configuration")
                run_configurations = self._collect_run_configuration_audit(timeout_s=timeout_s)
                done("current_run_configuration")
                mark("reuse_audit_from_manifest")
                audit_data = _load_reusable_audit_from_diagnostic_manifest(
                    reuse_audit_from_manifest,
                    project_dir,
                    run_name,
                    current_run_configurations=run_configurations,
                    current_design_execution_identity=design_execution_identity,
                )
                progress_context["reuse_audit_from_manifest"] = str(Path(str(reuse_audit_from_manifest)).resolve())
                done("audit_result_reused")
            else:
                mark("run_project_audit")
                audit_result = self._run_project_audit(args)
                if not audit_result.get("ok") and audit_result.get("error_code") == "TimeoutError":
                    progress_context["failed_result"] = audit_result
                    progress_context["audit_timeout_context"] = audit_result.get("data", {})
                    partial_manifest = self._collect_partial_diagnostic_bundle(
                        args=args,
                        progress_context=progress_context,
                        project_dir=project_dir,
                        project_state=project_state,
                        environment=environment,
                        audit_result=audit_result,
                        run_name=run_name,
                    )
                    if partial_manifest:
                        progress_context["manifest_path"] = str(partial_manifest.get("manifest_path", ""))
                        progress_context["partial_manifest_path"] = str(partial_manifest.get("manifest_path", ""))
                        progress_context["bundle_dir"] = str(partial_manifest.get("bundle_dir", ""))
                        done("partial_diagnostic_manifest")
                    return _diagnostic_collection_failure(
                        "TimeoutError",
                        "Project audit timed out; partial diagnostic bundle was collected when possible.",
                        progress_context,
                        audit_result.get("raw_excerpt", ""),
                    )
                audit_data = audit_result["data"] if audit_result.get("ok") else _data_or_finding("run_project_audit", audit_result)
                done("audit_result")

            mark("collect_filesets")
            filesets = self._collect_filesets_for_bundle(timeout_s=timeout_s)
            done("filesets")

            mark("collect_run_configurations")
            if run_configurations is None:
                run_configurations = self._collect_run_configuration_audit(timeout_s=timeout_s)
            done("run_configurations")

            mark("load_waivers")
            waivers = _load_waivers_safe(project_dir)
            session_status = self._session_status_data()
            replay_script = render_project_replay_script(
                vivado_version=str(environment.get("version", "")),
                project=project_state.get("project", {}),
                filesets=filesets,
                run_configurations=run_configurations.get("runs", run_configurations),
            )
            done("replay_script")

            audit_report_manifest = (
                audit_data.get("inputs", {}).get("report_manifest", {})
                if isinstance(audit_data, dict)
                else {}
            )
            audit_report_manifest_path = (
                str(audit_report_manifest.get("manifest_path", ""))
                if isinstance(audit_report_manifest, dict)
                else ""
            )
            report_manifest_candidate = (
                Path(audit_report_manifest_path)
                if audit_report_manifest_path
                else _latest_report_manifest_path(Path(project_dir), run_name)
            )
            report_manifest: Path | None = None
            report_manifest_snapshot: EvidenceSnapshot | None = None
            if report_manifest_candidate.exists():
                mark("revalidate_report_manifest")
                validated_report = self._load_existing_report_manifest(
                    {"report_manifest_path": str(report_manifest_candidate), "project_dir": str(project_dir)},
                    run_name=run_name,
                    project_dir=project_dir,
                    timeout_s=timeout_s,
                )
                if validated_report is None:
                    raise ValueError("report manifest exists but strict validation did not accept it")
                _, report_manifest_snapshot = load_json_evidence(
                    report_manifest_candidate,
                    root=Path(project_dir) / "vmcp_reports",
                    max_bytes=MAX_REPORT_MANIFEST_BYTES,
                )
                if report_manifest_snapshot.sha256 != str(validated_report.get("manifest_sha256", "")):
                    raise ValueError("report manifest changed after strict validation")
                report_manifest = report_manifest_candidate
                done("report_manifest_revalidated")
            artifact_manifest_candidate = _resolve_artifact_manifest_path(project_dir, run_name)
            artifact_manifest: Path | None = None
            artifact_manifest_snapshot: EvidenceSnapshot | None = None
            if artifact_manifest_candidate.exists():
                mark("revalidate_artifact_manifest")
                artifact_result = self._get_artifact_manifest(
                    {"run_name": run_name, "timeout_s": timeout_s}
                )
                if artifact_result.get("ok"):
                    artifact_manifest = Path(str(artifact_result["data"]["manifest_path"])).resolve()
                    _, artifact_manifest_snapshot = load_json_evidence(
                        artifact_manifest,
                        root=Path(project_dir) / "vmcp_artifacts",
                        max_bytes=4 * 1024 * 1024,
                    )
                    if artifact_manifest_snapshot.sha256 != str(artifact_result["data"].get("manifest_sha256", "")):
                        raise ValueError("artifact manifest changed after strict validation")
                    done("artifact_manifest_revalidated")
                else:
                    progress_context["artifact_manifest_rejected"] = {
                        "manifest_path": str(artifact_manifest_candidate),
                        "error_code": str(artifact_result.get("error_code", "")),
                        "message": str(artifact_result.get("message", "")),
                    }
            workflow_trace_path = self.tracer.ensure_project_dir(project_dir)

            mark("collect_log_tail")
            logs = self._collect_log_tail(timeout_s=timeout_s)
            done("logs_tail")

            mark("write_diagnostic_bundle")
            data = collect_diagnostic_bundle_files(
                project_dir=project_dir,
                audit_result=audit_data,
                environment=environment,
                project_state=project_state,
                filesets=filesets,
                run_configurations=run_configurations,
                waivers=waivers,
                session_status=session_status,
                replay_script=replay_script,
                report_manifest_path=report_manifest,
                artifact_manifest_path=artifact_manifest,
                report_manifest_snapshot=report_manifest_snapshot,
                artifact_manifest_snapshot=artifact_manifest_snapshot,
                workflow_trace_path=workflow_trace_path if workflow_trace_path.exists() else None,
                logs=logs,
                output_dir=args.get("output_dir"),
                timestamp=args.get("timestamp"),
            )
            done("diagnostic_manifest")
        except ReportManifestValidationError as exc:
            return _diagnostic_collection_failure(
                exc.error_code,
                str(exc),
                {**progress_context, **exc.data},
            )
        except (OSError, ValueError) as exc:
            return _diagnostic_collection_failure(exc.__class__.__name__, str(exc), progress_context)
        except Exception as exc:  # noqa: BLE001 - preserve recoverable diagnostic context for MCP clients.
            return _diagnostic_collection_failure(exc.__class__.__name__, str(exc) or exc.__class__.__name__, progress_context)
        return success("collect_diagnostic_bundle", f"Diagnostic bundle collected: {data['bundle_dir']}.", data)

    def _collect_partial_diagnostic_bundle(
        self,
        *,
        args: dict[str, Any],
        progress_context: dict[str, Any],
        project_dir: str,
        project_state: dict[str, Any],
        environment: dict[str, Any],
        audit_result: dict[str, Any],
        run_name: str,
    ) -> dict[str, Any] | None:
        audit_data = _partial_audit_result_from_failure(audit_result, project_state=project_state)
        report_manifest = Path(project_dir) / "vmcp_reports" / run_name / "report_manifest.json"
        # A timeout can leave the managed Tcl generation indeterminate. Partial
        # handoff therefore omits manifests that cannot be revalidated safely.
        artifact_manifest: Path | None = None
        workflow_trace_path = self.tracer.ensure_project_dir(project_dir)
        try:
            return collect_diagnostic_bundle_files(
                project_dir=project_dir,
                audit_result=audit_data,
                environment=environment,
                project_state=project_state,
                filesets={},
                run_configurations={},
                waivers=_load_waivers_safe(project_dir),
                session_status=self._session_status_data(),
                replay_script="",
                report_manifest_path=report_manifest if report_manifest.exists() else None,
                artifact_manifest_path=artifact_manifest,
                workflow_trace_path=workflow_trace_path if workflow_trace_path.exists() else None,
                logs={
                    "partial_collection": (
                        "collect_diagnostic_bundle wrote this partial bundle after run_project_audit failed.\n"
                        f"failed_error_code={audit_result.get('error_code', '')}\n"
                        f"failed_message={audit_result.get('message', '')}\n"
                        f"progress_context={json.dumps(progress_context, ensure_ascii=False, default=str)[:4000]}"
                    )
                },
                output_dir=args.get("output_dir"),
                timestamp=args.get("timestamp"),
            )
        except (OSError, ValueError) as exc:
            progress_context["partial_bundle_error"] = f"{exc.__class__.__name__}: {exc}"
            return None

    def _validate_diagnostic_bundle(self, args: dict[str, Any]) -> dict[str, Any]:
        manifest_path = args.get("manifest_path")
        if not manifest_path and args.get("bundle_dir"):
            manifest_path = str(Path(str(args["bundle_dir"])) / "diagnostic_manifest.json")
        if not manifest_path:
            return failure(
                "validate_diagnostic_bundle",
                "DIAGNOSTIC_MANIFEST_REQUIRED",
                "manifest_path or bundle_dir is required.",
                data={
                    "next_steps": ["Pass collect_diagnostic_bundle.data.manifest_path or the diagnostic bundle directory."],
                    "next_actions": [
                        next_action(
                            "collect_diagnostic_bundle",
                            "Create a diagnostic bundle before validating handoff health.",
                            required_args=["run_name"],
                            arg_sources={"run_name": "workflow.run_name or current implementation run"},
                            preconditions=["Original Vivado project is open."],
                            stop_condition="collect_diagnostic_bundle returns manifest_path.",
                        )
                    ],
                },
            )
        try:
            data = validate_diagnostic_bundle_manifest(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return failure(
                "validate_diagnostic_bundle",
                exc.__class__.__name__,
                str(exc),
                data={
                    "manifest_path": str(manifest_path),
                    "next_steps": ["Rebuild collect_diagnostic_bundle and validate the new manifest."],
                    "next_actions": [
                        next_action(
                            "collect_diagnostic_bundle",
                            "Regenerate diagnostic bundle because the supplied manifest cannot be read.",
                            required_args=["run_name"],
                            arg_sources={"run_name": "workflow.run_name or current implementation run"},
                            preconditions=["Original Vivado project is open."],
                            stop_condition="collect_diagnostic_bundle returns a readable manifest_path.",
                        )
                    ],
                },
            )
        return success("validate_diagnostic_bundle", f"Diagnostic bundle health: {data['health']['status']}.", data)

    def _export_project_replay_script(self, args: dict[str, Any]) -> dict[str, Any]:
        timeout_s = int(args.get("timeout_s", 60))
        state_result = self._get_project_state(args)
        if not state_result.get("ok"):
            return _analysis_input_failure("get_project_state", state_result, tool="export_project_replay_script")
        project_state = state_result["data"]
        project = project_state.get("project", {})
        project_dir = project.get("directory", "")
        if not project_dir:
            return failure("export_project_replay_script", "PROJECT_DIR_UNAVAILABLE", "Could not resolve current Vivado project directory.")
        filesets = self._collect_filesets_for_bundle(timeout_s=timeout_s)
        run_configurations = {
            run_name: _data_or_finding("get_run_configuration", self._get_run_configuration({"run_name": run_name, "timeout_s": timeout_s}))
            for run_name in ("synth_1", "impl_1")
        }
        environment = self._find_vivado(args.get("vivado_path"))
        script = render_project_replay_script(
            vivado_version=str(environment.get("version", "")),
            project=project,
            filesets=filesets,
            run_configurations=run_configurations,
        )
        try:
            data = write_project_replay_script(project_dir=project_dir, script=script, output_path=args.get("output_path"))
        except (OSError, ValueError) as exc:
            return failure("export_project_replay_script", exc.__class__.__name__, str(exc), data={"project_dir": project_dir})
        data["project"] = project
        data["filesets"] = filesets
        return success("export_project_replay_script", f"Project replay script exported: {data['script_path']}.", data)

    def _session_status_data(self) -> dict[str, Any]:
        if self._session_override is not None and hasattr(self._session_override, "status"):
            try:
                status = self._session_override.status()
            except Exception as exc:  # noqa: BLE001 - status must remain serializable.
                return {"ok": False, "connected": False, "backend": "override", "message": str(exc)}
            if isinstance(status, dict):
                return status
        return self.manager.status()

    def _collect_filesets_for_bundle(self, *, timeout_s: int = 60) -> dict[str, Any]:
        filesets: dict[str, Any] = {}
        for fileset in ("sources_1", "constrs_1", "sim_1"):
            result = self._list_fileset_files({"fileset": fileset, "timeout_s": timeout_s})
            filesets[fileset] = result["data"]["files"] if result.get("ok") else []
        return filesets

    def _collect_run_configuration_audit(self, *, timeout_s: int = 60) -> dict[str, Any]:
        runs: dict[str, Any] = {}
        findings: list[dict[str, Any]] = []
        for run_name in ("synth_1", "impl_1"):
            result = self._get_run_configuration({"run_name": run_name, "timeout_s": timeout_s})
            if result.get("ok"):
                runs[run_name] = result["data"]
            else:
                findings.append({"severity": "WARN", "code": "RUN_CONFIGURATION_UNAVAILABLE", "message": f"{run_name} configuration unavailable", "source_tool": "get_run_configuration"})
        status = "WARN" if findings else "READY"
        return {"status": status, "runs": runs, "findings": findings}

    def _collect_ip_audit(self, *, timeout_s: int = 60) -> dict[str, Any]:
        command = (
            f"{tcl_wire_prelude()}; "
            "set rows [list]; "
            "foreach ip [get_ips -quiet *] {"
            "set locked \"\"; catch {set locked [get_property IS_LOCKED $ip]}; "
            "set upgrade \"\"; catch {set upgrade [get_property UPGRADE_VERSIONS $ip]}; "
            "lappend rows [::vivado_agent_mcp_wire_row [list ip $ip locked $locked upgrade_available [expr {$upgrade ne \"\"}]]]"
            "}; "
            "::vivado_agent_mcp_wire_list $rows"
        )
        raw = self._session().run_tcl(command, timeout_s=timeout_s)
        if not raw.get("ok"):
            return {"status": "WARN", "findings": [{"severity": "WARN", "code": "IP_STATUS_UNAVAILABLE", "message": "IP status unavailable", "source_tool": "get_ip_status"}], "raw": raw.get("raw", "")}
        try:
            encoded_rows = decode_wire_list(raw.get("raw", ""), allow_legacy=False)
            ips = [decode_wire_row(line, allow_legacy=False) for line in encoded_rows]
        except ValueError as exc:
            return {
                "status": "BLOCK",
                "wire_trust": "INVALID",
                "findings": [
                    {
                        "severity": "BLOCK",
                        "code": "IP_WIRE_PROTOCOL_INVALID",
                        "message": str(exc),
                        "source_tool": "get_ip_status",
                    }
                ],
            }
        findings = [
            {"severity": "WARN", "code": "IP_LOCKED", "message": f"IP is locked: {item.get('ip', '')}", "source_tool": "get_ip_status"}
            for item in ips
            if str(item.get("locked", "")).lower() in {"1", "true", "yes"}
        ]
        return {
            "status": "WARN" if findings else "READY",
            "ips": ips,
            "findings": findings,
            "command": command,
            "wire_trust": "VERSIONED",
        }

    def _collect_bd_audit(self, *, timeout_s: int = 60) -> dict[str, Any]:
        command = f"{tcl_wire_prelude()}; ::vivado_agent_mcp_wire_list [get_files -quiet *.bd]"
        raw = self._session().run_tcl(command, timeout_s=timeout_s)
        if not raw.get("ok"):
            return {"status": "WARN", "findings": [{"severity": "WARN", "code": "BD_STATUS_UNAVAILABLE", "message": "Block Design status unavailable", "source_tool": "validate_block_design"}], "raw": raw.get("raw", "")}
        try:
            bd_files = decode_wire_list(raw.get("raw", ""), allow_legacy=False)
        except ValueError as exc:
            return {
                "status": "BLOCK",
                "wire_trust": "INVALID",
                "bd_files": [],
                "designs": [],
                "findings": [
                    {
                        "severity": "BLOCK",
                        "code": "BD_WIRE_PROTOCOL_INVALID",
                        "message": str(exc),
                        "source_tool": "validate_block_design",
                    }
                ],
            }
        if not bd_files:
            return {"status": "READY", "wire_trust": "VERSIONED", "bd_files": [], "designs": [], "findings": []}

        designs: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        for bd_file in bd_files:
            bd_name = Path(bd_file).stem
            file_path = Path(bd_file)
            file_identity = {
                "path": str(file_path),
                "size": file_path.stat().st_size if file_path.is_file() else None,
                "sha256": sha256_file(file_path) if file_path.is_file() else "",
            }
            validation = self._validate_block_design({"bd_name": bd_name, "timeout_s": timeout_s})
            if not validation.get("ok"):
                design = {
                    "bd_name": bd_name,
                    "bd_file": bd_file,
                    "file_identity": file_identity,
                    "status": "BLOCK",
                    "message": validation.get("message", "Block Design validation failed"),
                }
                designs.append(design)
                findings.append(
                    {
                        "severity": "BLOCK",
                        "code": "BD_VALIDATION_FAILED",
                        "message": f"Block Design {bd_name} could not be opened or validated: {design['message']}",
                        "source_tool": "validate_block_design",
                        "bd_name": bd_name,
                        "bd_file": bd_file,
                    }
                )
                continue
            validation_data = validation["data"]
            validation_status = "BLOCK" if validation_data.get("status") != "VALID" else "READY"
            designs.append(
                {
                    "bd_name": bd_name,
                    "bd_file": bd_file,
                    "file_identity": file_identity,
                    "status": validation_status,
                    "validation": validation_data,
                }
            )
            if validation_status == "BLOCK":
                findings.append(
                    {
                        "severity": "BLOCK",
                        "code": "BD_INVALID",
                        "message": f"Block Design {bd_name} validation is {validation_data.get('status', 'UNKNOWN')}",
                        "source_tool": "validate_block_design",
                        "bd_name": bd_name,
                        "bd_file": bd_file,
                    }
                )
        return {
            "status": "BLOCK" if findings else "READY",
            "wire_trust": "VERSIONED",
            "bd_files": bd_files,
            "designs": designs,
            "findings": findings,
        }

    def _collect_log_tail(self, *, timeout_s: int = 60) -> dict[str, str]:
        raw = self._session().run_tcl(_read_vivado_messages_command(), timeout_s=timeout_s)
        if not raw.get("ok"):
            return {"vivado": raw.get("raw", "")}
        return {"vivado": raw.get("raw", "")[-20000:]}

    def _run_synthesis(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._launch_run(
            tool="run_synthesis",
            operation="synthesis",
            run_name=str(args.get("run_name", "synth_1")),
            timeout_s=int(args.get("timeout_s", 30)),
        )

    def _run_implementation(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._launch_run(
            tool="run_implementation",
            operation="implementation",
            run_name=str(args.get("run_name", "impl_1")),
            timeout_s=int(args.get("timeout_s", 30)),
        )

    def _generate_bitstream(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._launch_run(
            tool="generate_bitstream",
            operation="bitstream",
            run_name=str(args.get("run_name", "impl_1")),
            timeout_s=int(args.get("timeout_s", 30)),
            to_step="write_bitstream",
        )

    def _get_run_progress(self, args: dict[str, Any]) -> dict[str, Any]:
        run_name = str(args.get("run_name", "impl_1"))
        expect_bitstream = bool(args.get("expect_bitstream", False))
        run_ref = tcl_list_quote(run_name)
        command = (
            f"{tcl_wire_prelude()}; set runs [get_runs -quiet {run_ref}]; "
            "if {[llength $runs] == 0} {error \"Vivado run not found\"}; "
            "set r [lindex $runs 0]; "
            "set run_dir [get_property DIRECTORY $r]; "
            "set bit_files [glob -nocomplain -directory $run_dir *.bit]; "
            "join [list "
            "\"status=[get_property STATUS $r]\" "
            "\"progress=[get_property PROGRESS $r]\" "
            "\"needs_refresh=[get_property NEEDS_REFRESH $r]\" "
            "\"bitstream_exists=[expr {[llength $bit_files] > 0}]\" "
            "\"bitstream_files=[::vivado_agent_mcp_wire_list $bit_files]\""
            "] \"\\n\""
        )
        data = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 30)))
        if not data.get("ok"):
            return _tcl_failure("get_run_progress", data)
        progress = _parse_key_value_lines(data.get("raw", ""))
        bitstream_wire = progress.get("bitstream_files")
        if bitstream_wire is None:
            bitstream_files: list[str] = []
            wire_trust = "MISSING"
        else:
            try:
                bitstream_files = decode_wire_list(bitstream_wire, allow_legacy=False)
                wire_trust = "VERSIONED"
            except ValueError as exc:
                return failure(
                    "get_run_progress",
                    "RUN_PROGRESS_WIRE_PROTOCOL_INVALID",
                    str(exc),
                    data={"run_name": run_name, "wire_trust": "INVALID"},
                )
        progress["bitstream_files"] = bitstream_files
        progress["wire_trust"] = wire_trust
        data["run_name"] = run_name
        launch = self._run_launches.get(run_name)
        if launch:
            launch_generation = str(launch.get("generation_id", ""))
            result_generation = str(data.get("generation_id", ""))
            if launch_generation and result_generation and launch_generation != result_generation:
                self._run_launches.pop(run_name, None)
                launch = None
        bitstream_exists = progress.get("bitstream_exists") == "1"
        if bitstream_exists and wire_trust != "VERSIONED":
            return failure(
                "get_run_progress",
                "RUN_PROGRESS_WIRE_PROTOCOL_INVALID",
                "A reported bitstream requires a versioned bitstream_files wire list.",
                data={"run_name": run_name, "wire_trust": wire_trust},
            )
        state = _classify_run_state(
            progress.get("status", ""),
            expect_bitstream=expect_bitstream,
            bitstream_exists=bitstream_exists,
        )
        launch_transition_pending = False
        launch_transition_timed_out = False
        if expect_bitstream and not bitstream_exists and launch and launch.get("operation") == "bitstream":
            launch_age_s = max(0.0, monotonic() - float(launch.get("monotonic_started_at", 0.0)))
            status_lower = progress.get("status", "").lower()
            if state == "not_started" and launch_age_s <= RUN_LAUNCH_TRANSITION_TIMEOUT_S:
                state = "launching"
                launch_transition_pending = True
            elif state == "not_started":
                state = "failed"
                launch_transition_timed_out = True
            elif state == "failed":
                self._run_launches.pop(run_name, None)
        elif state == "failed":
            self._run_launches.pop(run_name, None)
        normalized = _normalize_run_progress(progress, state=state, expect_bitstream=expect_bitstream)
        progress.update(
            {
                "normalized_status": normalized["status"],
                "normalized_progress": normalized["progress"],
            }
        )
        data["progress"] = progress
        data["state"] = state
        data["normalized_state"] = state
        data["terminal"] = normalized["terminal"]
        data["phase"] = normalized["phase"]
        data["percent"] = normalized["percent"]
        data["launch_transition_pending"] = launch_transition_pending
        data["launch_transition_timed_out"] = launch_transition_timed_out
        if state == "complete" and launch and isinstance(self._session(), GuiTcpVivadoSession):
            current_identity_result = self._capture_design_execution_identity(
                timeout_s=int(args.get("timeout_s", 30))
            )
            if not current_identity_result.get("ok"):
                return failure(
                    "get_run_progress",
                    str(current_identity_result.get("error_code") or "DESIGN_EXECUTION_IDENTITY_BLOCKED"),
                    str(current_identity_result.get("message") or "Could not capture terminal design execution identity."),
                    data={
                        "run_name": run_name,
                        "progress": progress,
                        "run_launch": launch,
                        "identity_failure": current_identity_result,
                        "stop_required": True,
                    },
                )
            current_identity = current_identity_result["data"]["design_execution_identity"]
            launched_identity = launch.get("design_execution_identity")
            if not isinstance(launched_identity, dict) or str(launched_identity.get("sha256", "")) != str(current_identity.get("sha256", "")):
                return failure(
                    "get_run_progress",
                    "SOURCE_CLOSURE_CHANGED",
                    "RTL/XDC/include/run configuration identity changed between run launch and terminal completion.",
                    data={
                        "run_name": run_name,
                        "progress": progress,
                        "launch_design_execution_identity": launched_identity or {},
                        "terminal_design_execution_identity": current_identity,
                        "stop_required": True,
                    },
                )
            launch["terminal_design_execution_identity"] = current_identity
            data["design_execution_identity"] = current_identity
            data["design_execution_identity_sha256"] = str(current_identity.get("sha256", ""))
        if launch:
            data["run_launch"] = {
                "launch_id": launch.get("launch_id", ""),
                "operation": launch.get("operation", ""),
                "started_at": launch.get("started_at", ""),
                "generation_id": launch.get("generation_id", ""),
                "design_execution_identity_sha256": str(
                    (launch.get("design_execution_identity") or {}).get("sha256", "")
                    if isinstance(launch.get("design_execution_identity"), dict)
                    else ""
                ),
            }
        if state == "complete" and expect_bitstream and progress.get("bitstream_exists") == "1":
            data["next_actions"] = _post_bitstream_handoff_actions(run_name)
        elif state == "not_started" and expect_bitstream:
            data["next_actions"] = [
                next_action(
                    "generate_bitstream",
                    "The implementation run is complete, but write_bitstream has not been launched in this MCP session.",
                    arg_sources={"run_name": run_name},
                    preconditions=["Implementation completed without a bitstream artifact."],
                    stop_condition="generate_bitstream returns a launch_id, then get_run_progress observes write_bitstream or the bitstream file.",
                )
            ]
        elif state == "failed":
            data["next_actions"] = [
                next_action(
                    "diagnose_run_failure",
                    "Collect run log tail and critical Vivado messages before attempting a repair or relaunch.",
                    required_args=["run_name"],
                    arg_sources={"run_name": run_name, "expect_bitstream": "same value used for get_run_progress when relevant"},
                    preconditions=["Vivado run reached a failed terminal state."],
                    stop_condition="diagnose_run_failure returns a primary_cause and repair next_actions.",
                )
            ]
        return success("get_run_progress", "Run progress collected.", data)

    def _diagnose_run_failure(self, args: dict[str, Any]) -> dict[str, Any]:
        run_name = str(args.get("run_name", "impl_1"))
        timeout_s = int(args.get("timeout_s", 60))
        expect_bitstream = bool(args.get("expect_bitstream", run_name == "impl_1"))
        progress_result = self._get_run_progress(
            {"run_name": run_name, "timeout_s": min(timeout_s, 60), "expect_bitstream": expect_bitstream}
        )
        context_command = _run_failure_context_command(run_name)
        context_raw = self._session().run_tcl(context_command, timeout_s=timeout_s)
        context_raw["command"] = context_command
        critical_result = self._get_critical_warnings({"timeout_s": min(timeout_s, 60)})

        data = _run_failure_diagnosis_data(
            run_name=run_name,
            progress_result=progress_result,
            context_raw=context_raw,
            critical_result=critical_result,
        )
        if not progress_result.get("ok") and not context_raw.get("ok"):
            return failure(
                "diagnose_run_failure",
                "RUN_DIAGNOSIS_UNAVAILABLE",
                "Run failure diagnosis could not read run progress or run log context.",
                data=data,
            )
        return success(
            "diagnose_run_failure",
            f"Run failure diagnosis collected: {data['diagnosis']['primary_cause']}.",
            data,
        )

    def _get_timing_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        command = timing_summary_command()
        raw = self._run_execution_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        command = str(raw["command"])
        if not raw.get("ok"):
            return _tcl_failure("get_timing_summary", raw)
        data = parse_timing_summary(raw.get("raw", ""))
        data["raw"] = raw.get("raw", "")
        return success("get_timing_summary", "Timing summary parsed.", data)

    def _get_utilization_report(self, args: dict[str, Any]) -> dict[str, Any]:
        raw = self._run_execution_tcl("report_utilization -return_string", timeout_s=int(args.get("timeout_s", 120)))
        if not raw.get("ok"):
            return _tcl_failure("get_utilization_report", raw)
        data = parse_utilization_report(raw.get("raw", ""))
        data["raw"] = raw.get("raw", "")
        return success("get_utilization_report", "Utilization report parsed.", data)

    def _get_drc_report(self, args: dict[str, Any]) -> dict[str, Any]:
        command = drc_report_command()
        raw = self._run_execution_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        command = str(raw["command"])
        if not raw.get("ok"):
            return _tcl_failure("get_drc_report", raw)
        data = parse_drc_report(raw.get("raw", ""))
        data["raw"] = raw.get("raw", "")
        data["command"] = command
        return success("get_drc_report", "DRC report parsed.", data)

    def _get_constraints_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        command = constraints_summary_command(fileset=str(args.get("fileset", "constrs_1")))
        raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("get_constraints_summary", raw)
        data = parse_constraints_summary(raw.get("raw", ""))
        data["command"] = command
        if data.get("status") == "BLOCK":
            return failure(
                "get_constraints_summary",
                str(data.get("error_code") or "CONSTRAINT_DISCOVERY_FAILED"),
                "Vivado constraint discovery is incomplete or failed; the summary cannot be treated as READY.",
                data=data,
            )
        return success("get_constraints_summary", "Vivado constraints summary parsed.", data)

    def _check_timing_constraints(self, args: dict[str, Any]) -> dict[str, Any]:
        command = check_timing_constraints_command()
        raw = self._run_execution_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        command = str(raw["command"])
        if not raw.get("ok"):
            return _tcl_failure("check_timing_constraints", raw)
        data = parse_check_timing_report(raw.get("raw", ""))
        data["command"] = command
        return success("check_timing_constraints", f"check_timing status: {data['status']}.", data)

    def _get_clock_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        command = clock_summary_command()
        raw = self._run_execution_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        command = str(raw["command"])
        if not raw.get("ok"):
            return _tcl_failure("get_clock_summary", raw)
        data = parse_clock_summary(raw.get("raw", ""))
        data["command"] = command
        return success("get_clock_summary", "Clock summary parsed.", data)

    def _get_timing_paths(self, args: dict[str, Any]) -> dict[str, Any]:
        command = timing_paths_command(
            max_paths=int(args.get("max_paths", 10)),
            delay_type=str(args.get("delay_type", "max")),
        )
        raw = self._run_execution_tcl(command, timeout_s=int(args.get("timeout_s", 180)))
        command = str(raw["command"])
        if not raw.get("ok"):
            return _tcl_failure("get_timing_paths", raw)
        data = parse_timing_paths(raw.get("raw", ""))
        data["command"] = command
        return success("get_timing_paths", "Timing paths parsed.", data)

    def _get_methodology_report(self, args: dict[str, Any]) -> dict[str, Any]:
        command = methodology_report_command()
        raw = self._run_execution_tcl(command, timeout_s=int(args.get("timeout_s", 180)))
        command = str(raw["command"])
        if not raw.get("ok"):
            return _tcl_failure("get_methodology_report", raw)
        data = parse_methodology_report(raw.get("raw", ""))
        data["command"] = command
        return success("get_methodology_report", f"Methodology status: {data['status']}.", data)

    def _get_qor_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        command = qor_summary_command()
        raw = self._run_execution_tcl(command, timeout_s=int(args.get("timeout_s", 180)))
        command = str(raw["command"])
        if not raw.get("ok"):
            return _tcl_failure("get_qor_summary", raw)
        data = parse_qor_summary(raw.get("raw", ""))
        data["command"] = command
        return success("get_qor_summary", "QoR summary parsed.", data)

    def _get_cdc_report(self, args: dict[str, Any]) -> dict[str, Any]:
        command = cdc_report_command()
        raw = self._run_execution_tcl(command, timeout_s=int(args.get("timeout_s", 180)))
        command = str(raw["command"])
        if not raw.get("ok"):
            return _tcl_failure("get_cdc_report", raw)
        data = parse_cdc_report(raw.get("raw", ""))
        data["command"] = command
        return success("get_cdc_report", f"CDC report status: {data['status']}.", data)

    def _get_clock_interaction_report(self, args: dict[str, Any]) -> dict[str, Any]:
        command = clock_interaction_report_command()
        raw = self._run_execution_tcl(command, timeout_s=int(args.get("timeout_s", 180)))
        command = str(raw["command"])
        if not raw.get("ok"):
            return _tcl_failure("get_clock_interaction_report", raw)
        data = parse_clock_interaction_report(raw.get("raw", ""))
        data["command"] = command
        return success("get_clock_interaction_report", f"Clock interaction status: {data['status']}.", data)

    def _get_power_report(self, args: dict[str, Any]) -> dict[str, Any]:
        command = power_report_command()
        raw = self._run_execution_tcl(command, timeout_s=int(args.get("timeout_s", 180)))
        command = str(raw["command"])
        if not raw.get("ok"):
            return _tcl_failure("get_power_report", raw)
        data = parse_power_report(raw.get("raw", ""))
        data["command"] = command
        return success("get_power_report", f"Power report status: {data['status']}.", data)

    def _analyze_timing_closure(self, args: dict[str, Any]) -> dict[str, Any]:
        timing_result = self._get_timing_summary(args)
        if not timing_result.get("ok"):
            return _analysis_input_failure("get_timing_summary", timing_result)
        check_result = self._check_timing_constraints(args)
        if not check_result.get("ok"):
            return _analysis_input_failure("check_timing_constraints", check_result)
        methodology_result = self._get_methodology_report(args)
        if not methodology_result.get("ok"):
            return _analysis_input_failure("get_methodology_report", methodology_result)
        drc_result = self._get_drc_report(args)
        if not drc_result.get("ok"):
            return _analysis_input_failure("get_drc_report", drc_result)
        critical_result = self._get_critical_warnings(args)
        if not critical_result.get("ok"):
            return _analysis_input_failure("get_critical_warnings", critical_result)
        data = parse_timing_closure_analysis(
            timing=timing_result["data"],
            check_timing=check_result["data"],
            methodology=methodology_result["data"],
            drc=drc_result["data"],
            critical_warnings=critical_result["data"],
        )
        return success("analyze_timing_closure", f"Timing closure analysis: {data['status']}.", data)

    def _run_pre_hw_signoff(self, args: dict[str, Any]) -> dict[str, Any]:
        timeout_s = int(args.get("timeout_s", 180))
        run_name = str(args.get("run_name", "impl_1"))
        project_dir_text = str(args.get("project_dir") or "")
        report_manifest_path = str(args.get("report_manifest_path") or "")
        completed_steps: list[str] = []
        current_step = "analyze_sources"
        partial_evidence: dict[str, Any] = {"run_name": run_name}
        if project_dir_text:
            partial_evidence["project_dir"] = project_dir_text
        if report_manifest_path:
            partial_evidence["report_manifest_path"] = report_manifest_path

        try:
            sources_result = self._analyze_sources(args)
            if not sources_result.get("ok"):
                return _analysis_input_failure("analyze_sources", sources_result, tool="run_pre_hw_signoff")
            partial_evidence["sources"] = sources_result["data"]
            completed_steps.append(current_step)

            current_step = "run_elaboration"
            elaboration_result = self._run_elaboration(args)
            if not elaboration_result.get("ok"):
                return _analysis_input_failure("run_elaboration", elaboration_result, tool="run_pre_hw_signoff")
            partial_evidence["elaboration"] = elaboration_result["data"]
            completed_steps.append(current_step)

            current_step = "open_run"
            open_run_command = f"{run_hook_guard_command()}; open_run {tcl_list_quote(run_name)}"
            open_run_raw = self._session().run_tcl(open_run_command, timeout_s=min(timeout_s, 120))
            open_run_raw["command"] = open_run_command
            if not open_run_raw.get("ok"):
                return _analysis_input_failure(
                    "open_run",
                    _tcl_failure("open_run", open_run_raw),
                    tool="run_pre_hw_signoff",
                    action_tool="get_run_configuration",
                    action_required_args=["run_name"],
                    action_arg_sources={"run_name": "run_pre_hw_signoff.args.run_name or impl_1"},
                )
            partial_evidence["open_run"] = {"ok": True, "run_name": run_name}
            completed_steps.append(current_step)

            current_step = "load_report_manifest"
            try:
                report_manifest = self._load_existing_report_manifest(args, run_name=run_name, timeout_s=min(timeout_s, 60))
            except ReportManifestValidationError as exc:
                return _report_manifest_validation_failure("run_pre_hw_signoff", exc)
            report_inputs = _signoff_inputs_from_report_manifest(report_manifest) if report_manifest else {}
            partial_evidence["report_manifest_present"] = report_manifest is not None
            if report_manifest:
                partial_evidence["report_manifest_path"] = str(report_manifest.get("manifest_path", "") or report_manifest_path)
                partial_evidence["report_manifest_status"] = report_manifest.get("status", "")
            completed_steps.append(current_step)

            current_step = "configuration_voltage"
            configuration_voltage_raw = self._session().run_tcl(configuration_voltage_command(), timeout_s=min(timeout_s, 120))
            configuration_voltage = (
                parse_configuration_voltage(configuration_voltage_raw.get("raw", ""))
                if configuration_voltage_raw.get("ok")
                else {
                    "ok": False,
                    "status": "WARN",
                    "cfgbvs": "",
                    "config_voltage": "",
                    "warnings": ["Could not inspect CFGBVS/CONFIG_VOLTAGE on current_design before handoff."],
                    "raw": configuration_voltage_raw.get("raw", ""),
                }
            )
            partial_evidence["configuration_voltage"] = configuration_voltage
            completed_steps.append(current_step)

            if report_inputs:
                current_step = "refresh_check_timing"
                check_timing = self._refresh_check_timing_for_signoff(args, timeout_s=min(timeout_s, 60))
                partial_evidence["check_timing"] = check_timing
                completed_steps.append(current_step)

                current_step = "evaluate_report_bundle_readiness"
                readiness_data = evaluate_bitstream_readiness(
                    report_inputs["timing"],
                    report_inputs["drc"],
                    report_inputs["critical_warnings"],
                    check_timing,
                    report_inputs["methodology"],
                )
                if check_timing.get("status") == "WARN" and check_timing.get("warning_message"):
                    readiness_data.setdefault("warnings", []).append(str(check_timing["warning_message"]))
                readiness_data["source"] = "report_bundle"
                readiness_data["report_manifest_path"] = str(report_manifest.get("manifest_path", ""))
                readiness_result = success("check_bitstream_readiness", f"Bitstream readiness: {readiness_data['status']}.", readiness_data)
                cdc_result = success("get_cdc_report", f"CDC report status: {report_inputs['cdc']['status']}.", report_inputs["cdc"])
                clock_result = success(
                    "get_clock_interaction_report",
                    f"Clock interaction status: {report_inputs['clock_interaction']['status']}.",
                    report_inputs["clock_interaction"],
                )
                power_result = success("get_power_report", f"Power report status: {report_inputs['power']['status']}.", report_inputs["power"])
                partial_evidence["readiness"] = readiness_result["data"]
                partial_evidence["cdc"] = cdc_result["data"]
                partial_evidence["clock_interaction"] = clock_result["data"]
                partial_evidence["power"] = power_result["data"]
                completed_steps.append(current_step)
            else:
                current_step = "check_bitstream_readiness"
                readiness_result = self._check_bitstream_readiness(args)
                if not readiness_result.get("ok"):
                    return _analysis_input_failure("check_bitstream_readiness", readiness_result, tool="run_pre_hw_signoff")
                partial_evidence["readiness"] = readiness_result["data"]
                completed_steps.append(current_step)

                current_step = "get_cdc_report"
                cdc_result = self._get_cdc_report(args)
                if not cdc_result.get("ok"):
                    return _analysis_input_failure("get_cdc_report", cdc_result, tool="run_pre_hw_signoff")
                partial_evidence["cdc"] = cdc_result["data"]
                completed_steps.append(current_step)

                current_step = "get_clock_interaction_report"
                clock_result = self._get_clock_interaction_report(args)
                if not clock_result.get("ok"):
                    return _analysis_input_failure("get_clock_interaction_report", clock_result, tool="run_pre_hw_signoff")
                partial_evidence["clock_interaction"] = clock_result["data"]
                completed_steps.append(current_step)

                current_step = "get_power_report"
                power_result = self._get_power_report(args)
                if not power_result.get("ok"):
                    return _analysis_input_failure("get_power_report", power_result, tool="run_pre_hw_signoff")
                partial_evidence["power"] = power_result["data"]
                completed_steps.append(current_step)

                current_step = "collect_report_bundle"
                report_result = self._collect_report_bundle(args)
                report_manifest = report_result["data"] if report_result.get("ok") else {"status": "WARN", "message": report_result.get("message", "")}
                partial_evidence["report_bundle"] = report_manifest
                completed_steps.append(current_step)

            current_step = "get_simulation_result"
            simulation_args = dict(args)
            simulation_args["_status_source"] = "simulation_invocation_log_span"
            simulation_result = self._get_simulation_result(simulation_args)
            simulation = (
                {**simulation_result["data"], "source_tool": "get_simulation_result"}
                if simulation_result.get("ok")
                else {
                    "status": "BLOCK",
                    "message": simulation_result.get("message", "simulation result unavailable"),
                    "error_code": simulation_result.get("error_code", "SIMULATION_RESULT_UNAVAILABLE"),
                    "source_tool": "get_simulation_result",
                }
            )
            partial_evidence["simulation"] = simulation
            completed_steps.append(current_step)

            current_step = "evaluate_pre_hw_signoff"
            data = evaluate_pre_hw_signoff(
                sources=sources_result["data"],
                elaboration=elaboration_result["data"],
                simulation=simulation,
                readiness=readiness_result["data"],
                cdc=cdc_result["data"],
                clock_interaction=clock_result["data"],
                power=power_result["data"],
                configuration_voltage=configuration_voltage,
                report_manifest=report_manifest,
            )
            data["report_bundle"] = report_manifest
            completed_steps.append(current_step)

            if bool(args.get("apply_waivers", True)):
                current_step = "apply_waivers"
                project_dir = self._resolve_project_dir(args, timeout_s=int(args.get("timeout_s", 60)))
                if project_dir is not None:
                    data = apply_waivers_to_signoff(data, _load_waivers_safe(project_dir))
                completed_steps.append(current_step)
            return success("run_pre_hw_signoff", f"Pre-hardware signoff status: {data.get('effective_status', data['status'])}.", data)
        except TimeoutError as exc:
            return self._timeout_failure(
                "run_pre_hw_signoff",
                exc,
                timeout_s=timeout_s,
                command=f"run_pre_hw_signoff stage={current_step}",
                request_context={
                    "run_name": run_name,
                    "project_dir": project_dir_text,
                    "report_manifest_path": report_manifest_path,
                    "apply_waivers": bool(args.get("apply_waivers", True)),
                },
                partial_context={
                    "status": "BLOCK",
                    "partial_success": bool(completed_steps),
                    "current_step": current_step,
                    "completed_steps": completed_steps,
                    "partial_evidence": partial_evidence,
                    "project_dir": project_dir_text,
                    "project_path": str(args.get("project_path", "") or ""),
                    "run_name": run_name,
                    "report_manifest_path": report_manifest_path,
                    "handoff_stage": "pre_hw_signoff",
                    "hardware_validation": hardware_validation_boundary(),
                },
                next_actions=_pre_hw_signoff_timeout_actions(
                    run_name=run_name,
                    project_path=str(args.get("project_path", "") or ""),
                    report_manifest_path=report_manifest_path,
                ),
            )

    def _load_existing_report_manifest(
        self,
        args: dict[str, Any],
        *,
        run_name: str,
        project_dir: str | Path | None = None,
        timeout_s: int = 60,
    ) -> dict[str, Any] | None:
        manifest_arg = args.get("report_manifest_path")
        explicit = bool(str(manifest_arg or "").strip())
        project_path: Path | None = None
        if project_dir:
            project_path = Path(project_dir).resolve()
        elif args.get("project_dir"):
            project_path = Path(str(args["project_dir"])).resolve()
        elif explicit:
            resolved_project = self._resolve_project_dir(args, timeout_s=timeout_s)
            project_path = resolved_project.resolve() if resolved_project else None

        if project_path is None:
            if explicit:
                raise ReportManifestValidationError(
                    "REPORT_MANIFEST_PROJECT_UNAVAILABLE",
                    "Could not resolve the current project directory for report_manifest_path validation.",
                    data={"manifest_path": str(manifest_arg), "run_name": run_name},
                )
            return None

        manifest_path = Path(os.path.abspath(os.fspath(manifest_arg))) if explicit else _latest_report_manifest_path(project_path, run_name)
        if not manifest_path.exists():
            if explicit:
                raise ReportManifestValidationError(
                    "REPORT_MANIFEST_NOT_FOUND",
                    f"Report manifest not found: {manifest_path}",
                    data={"manifest_path": str(manifest_path), "project_dir": str(project_path), "run_name": run_name},
                )
            return None
        try:
            data, snapshot = load_json_evidence(
                manifest_path,
                root=project_path / "vmcp_reports",
                max_bytes=MAX_REPORT_MANIFEST_BYTES,
            )
        except ManagedPathError as exc:
            if explicit:
                raise ReportManifestValidationError(
                    "REPORT_MANIFEST_UNTRUSTED",
                    f"Report manifest must remain inside the current project vmcp_reports root: {exc}",
                    data={"manifest_path": str(manifest_path), "project_dir": str(project_path), "run_name": run_name},
                ) from exc
            return None
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if explicit:
                raise ReportManifestValidationError(
                    "REPORT_MANIFEST_INVALID",
                    f"Report manifest is not readable JSON: {exc}",
                    data={"manifest_path": str(manifest_path), "project_dir": str(project_path), "run_name": run_name},
                ) from exc
            return None
        try:
            identity_result = self._evidence_design_execution_identity(
                run_name=run_name,
                timeout_s=timeout_s,
                require_terminal_launch=False,
            )
            if not identity_result.get("ok"):
                raise ReportManifestValidationError(
                    str(identity_result.get("error_code") or "DESIGN_EXECUTION_IDENTITY_BLOCKED"),
                    str(identity_result.get("message") or "Could not capture the current design execution identity."),
                    data={
                        "manifest_path": str(manifest_path),
                        "project_dir": str(project_path),
                        "run_name": run_name,
                        "identity_failure": identity_result,
                    },
                )
            validated = _validated_report_manifest(
                data,
                manifest_path=manifest_path,
                project_dir=project_path,
                run_name=run_name,
                current_design_execution_identity=identity_result["data"]["design_execution_identity"],
            )
            current_configuration = self._get_run_configuration(
                {"run_name": run_name, "timeout_s": timeout_s}
            )
            if not current_configuration.get("ok"):
                raise ReportManifestValidationError(
                    "REPORT_MANIFEST_CURRENT_RUN_UNAVAILABLE",
                    "Could not verify the report manifest against the current Vivado run.",
                    data={
                        "manifest_path": str(manifest_path),
                        "project_dir": str(project_path),
                        "run_name": run_name,
                        "run_configuration_error": current_configuration,
                    },
                )
            _validate_report_manifest_current_run(
                validated,
                current_configuration.get("data", {}).get("run", {}),
            )
            validated["manifest_sha256"] = snapshot.sha256
            return validated
        except ReportManifestValidationError:
            if explicit:
                raise
            return None

    def _refresh_check_timing_for_signoff(self, args: dict[str, Any], *, timeout_s: int) -> dict[str, Any]:
        try:
            result = self._check_timing_constraints(args | {"timeout_s": timeout_s})
        except TimeoutError as exc:
            return {
                "ok": False,
                "status": "BLOCK",
                "parsed": False,
                "counts": {},
                "warning_message": f"check_timing refresh timed out after {timeout_s}s while using report bundle evidence: {exc}",
            }
        if result.get("ok"):
            data = result.get("data", {})
            data["source"] = "live_check_timing_refresh"
            return data
        return {
            "ok": False,
            "status": "BLOCK",
            "parsed": False,
            "counts": {},
            "warning_message": f"check_timing refresh failed while using report bundle evidence: {result.get('message', result.get('error_code', 'unknown'))}",
        }

    def _create_managed_xdc(self, args: dict[str, Any]) -> dict[str, Any]:
        if bool(args.get("force", False)):
            return failure(
                "create_managed_xdc",
                "DESTRUCTIVE_FORCE_DISABLED",
                "Managed XDC overwrite is disabled; use a new name so prior constraint evidence remains immutable.",
                data={"name": str(args.get("name", "")), "force": True},
            )
        payload = managed_xdc_payload(
            name=str(args["name"]),
            constraints=list(args.get("constraints", [])),
        )
        policy_issues = validate_xdc_text(str(payload["content"]))
        if policy_issues:
            return failure(
                "create_managed_xdc",
                "MANAGED_XDC_POLICY_MISMATCH",
                "Managed XDC generation produced content outside the trusted executable-constraint policy.",
                data={
                    "name": str(args["name"]),
                    "policy_issues": policy_issues,
                    "policy_allowed": False,
                },
            )
        fileset = str(args.get("fileset", "constrs_1"))
        context_command = (
            "set p [current_project]; join [list "
            '"project_name=[get_property NAME $p]" '
            '"project_dir=[file normalize [get_property DIRECTORY $p]]"'
            "] \"\\n\""
        )
        context_raw = self._session().run_tcl(
            context_command,
            timeout_s=min(int(args.get("timeout_s", 120)), 60),
        )
        if not context_raw.get("ok"):
            return _tcl_failure("create_managed_xdc", context_raw)
        context = _parse_key_value_lines(context_raw.get("raw", ""))
        project_dir = Path(str(context.get("project_dir", ""))).resolve()
        if not context.get("project_name") or not project_dir.is_dir():
            return failure(
                "create_managed_xdc",
                "MANAGED_XDC_PROJECT_CONTEXT_INVALID",
                "Vivado did not return a usable current project identity for managed XDC creation.",
                data={"project_context": context},
            )
        xdc_path = project_dir / "vmcp_constraints" / str(payload["filename"])
        if os.path.lexists(xdc_path):
            return failure(
                "create_managed_xdc",
                "MANAGED_XDC_ALREADY_EXISTS",
                "Managed XDC already exists; choose a new name instead of overwriting prior constraint evidence.",
                data={"path": str(xdc_path), "project_dir": str(project_dir)},
            )
        try:
            atomic_write_bytes(project_dir, xdc_path, payload["content_bytes"])
            command = add_managed_xdc_command(
                xdc_path=xdc_path,
                fileset=fileset,
                constraint_count=int(payload["constraint_count"]),
            )
            with hold_managed_paths_stable(
                project_dir,
                files=[xdc_path],
                directories=[xdc_path.parent],
            ):
                raw = self._session().run_tcl(command, timeout_s=int(args.get("timeout_s", 120)))
        except (ManagedPathError, OSError) as exc:
            return failure(
                "create_managed_xdc",
                "MANAGED_XDC_FILESYSTEM_IDENTITY_CHANGED",
                f"Managed XDC filesystem identity could not be held stable: {exc}",
                data={"path": str(xdc_path), "project_dir": str(project_dir)},
            )
        raw["command"] = command
        if not raw.get("ok"):
            return _tcl_failure("create_managed_xdc", raw)
        data = {
            "command": command,
            "filesystem_backend": "python_atomic_managed_write",
            "managed_xdc": parse_managed_xdc_result(raw.get("raw", "")),
        }
        return success("create_managed_xdc", "Managed XDC created and added to constraints fileset.", data)

    def _get_messages(self, args: dict[str, Any]) -> dict[str, Any]:
        raw = self._session().run_tcl(_read_vivado_messages_command(), timeout_s=int(args.get("timeout_s", 60)))
        if not raw.get("ok"):
            return _tcl_failure("get_messages", raw)
        data = parse_messages(raw.get("raw", ""))
        data["raw"] = raw.get("raw", "")
        return success("get_messages", "Vivado messages parsed.", data)

    def _get_critical_warnings(self, args: dict[str, Any]) -> dict[str, Any]:
        raw = self._session().run_tcl(_read_vivado_messages_command(), timeout_s=int(args.get("timeout_s", 60)))
        if not raw.get("ok"):
            return _tcl_failure("get_critical_warnings", raw)
        data = _filter_messages(
            parse_messages(raw.get("raw", "")),
            severities={"ERROR", "CRITICAL WARNING"},
        )
        data["raw"] = raw.get("raw", "")
        return success("get_critical_warnings", "Critical Vivado messages parsed.", data)

    def _check_bitstream_readiness(self, args: dict[str, Any]) -> dict[str, Any]:
        timing_result = self._get_timing_summary(args)
        if not timing_result.get("ok"):
            return _readiness_input_failure("get_timing_summary", timing_result)
        drc_result = self._get_drc_report(args)
        if not drc_result.get("ok"):
            return _readiness_input_failure("get_drc_report", drc_result)
        critical_result = self._get_critical_warnings(args)
        if not critical_result.get("ok"):
            return _readiness_input_failure("get_critical_warnings", critical_result)
        check_result = self._check_timing_constraints(args)
        if not check_result.get("ok"):
            return _readiness_input_failure("check_timing_constraints", check_result)
        methodology_result = self._get_methodology_report(args)
        if not methodology_result.get("ok"):
            return _readiness_input_failure("get_methodology_report", methodology_result)
        timing = timing_result["data"]
        drc = drc_result["data"]
        critical = critical_result["data"]
        check_timing = check_result["data"]
        methodology = methodology_result["data"]
        data = evaluate_bitstream_readiness(timing, drc, critical, check_timing, methodology)
        return success("check_bitstream_readiness", f"Bitstream readiness: {data['status']}.", data)

    def _launch_run(
        self,
        tool: str,
        operation: str,
        run_name: str,
        timeout_s: int,
        to_step: str | None = None,
    ) -> dict[str, Any]:
        command = guarded_launch_run_command(run_name=run_name, to_step=to_step)
        data = self._session().run_tcl(command, timeout_s=timeout_s)
        if not data.get("ok"):
            data.update({"run_name": run_name, "operation": operation, "state": "launch_failed"})
            return _tcl_failure(tool, data)
        design_identity = data.get("design_execution_identity")
        if isinstance(self._session(), GuiTcpVivadoSession) and (
            not isinstance(design_identity, dict) or design_identity.get("status") != "READY"
        ):
            return failure(
                tool,
                "DESIGN_EXECUTION_IDENTITY_BLOCKED",
                "Managed Vivado run launch did not return a complete design execution identity.",
                data={"run_name": run_name, "operation": operation, "handler_executed": False},
            )
        launch_id = f"run_{uuid.uuid4().hex}"
        started_at = datetime.now(UTC).isoformat()
        self._run_launches[run_name] = {
            "launch_id": launch_id,
            "operation": operation,
            "to_step": to_step or "",
            "started_at": started_at,
            "monotonic_started_at": monotonic(),
            "generation_id": str(data.get("generation_id", "")),
            "design_execution_identity": design_identity if isinstance(design_identity, dict) else {},
        }
        data.update(
            {
                "run_name": run_name,
                "operation": operation,
                "state": "launched",
                "launch_id": launch_id,
                "launch_started_at": started_at,
                "design_execution_identity_sha256": str(design_identity.get("sha256", "")) if isinstance(design_identity, dict) else "",
                "next_tool": "get_run_progress",
                "next_actions": [_run_progress_next_action(run_name, expect_bitstream=operation == "bitstream")],
            }
        )
        return success(tool, f"{operation.title()} run launched.", data)


def _run_progress_next_action(run_name: str, *, expect_bitstream: bool = False) -> dict[str, Any]:
    return next_action(
        "get_run_progress",
        "Poll the Vivado run until it reaches a terminal state.",
        required_args=["run_name"],
        arg_sources={
            "run_name": f"current launched run {run_name}",
            "expect_bitstream": "set true when polling generate_bitstream or final implementation bitstream output",
        },
        preconditions=["launch_runs returned ok=true."],
        stop_condition="get_run_progress.data.terminal is true or workflow max_wait_s is exceeded.",
    )


def _start_session_failure_actions(data: dict[str, Any]) -> list[dict[str, Any]]:
    runtime_dir = str(data.get("runtime_dir") or data.get("temp_dir") or "")
    return [
        next_action(
            "detect_vivado_environment",
            "Probe Vivado launchability before retrying the GUI Tcl session.",
            arg_sources={
                "probe_launch": "true",
                "probe_timeout_s": "60",
                "runtime_dir": runtime_dir or "start_session.args.runtime_dir or default runtime",
            },
            preconditions=["start_session failed before Tcl server became connected."],
            stop_condition="detect_vivado_environment returns launch_probe.diagnosis and log tails.",
        ),
        next_action(
            "start_session",
            f"Retry start_session with a longer timeout_s={RECOMMENDED_RETRY_TIMEOUT_S} if launch probe is acceptable.",
            arg_sources={
                "timeout_s": str(RECOMMENDED_RETRY_TIMEOUT_S),
                "runtime_dir": runtime_dir or "same runtime_dir as the failed start_session call",
            },
            preconditions=["detect_vivado_environment(probe_launch=true) does not show a hard Vivado launch failure."],
            stop_condition="start_session returns ok=true or a more specific startup phase/error_code.",
            optional=True,
        ),
    ]


def _post_bitstream_handoff_actions(run_name: str) -> list[dict[str, Any]]:
    return [
        next_action(
            "collect_build_artifacts",
            "Collect bitstream and related generated artifacts into vmcp_artifacts.",
            required_args=["run_name"],
            arg_sources={"run_name": f"completed run {run_name}"},
            preconditions=["get_run_progress returned complete and bitstream_exists=true."],
            stop_condition="collect_build_artifacts returns manifest_path with bitstream evidence.",
        ),
        next_action(
            "collect_report_bundle",
            "Collect timing, utilization, DRC, methodology, QoR, CDC, clock interaction, and power reports.",
            required_args=["run_name"],
            arg_sources={"run_name": f"completed run {run_name}"},
            preconditions=["Implementation run is complete."],
            stop_condition="collect_report_bundle returns report_manifest.json.",
        ),
        next_action(
            "run_pre_hw_signoff",
            "Evaluate no-board pre-hardware software signoff before handoff.",
            required_args=["run_name"],
            arg_sources={"run_name": f"completed run {run_name}"},
            preconditions=["Reports can be generated from the completed implementation run."],
            stop_condition="run_pre_hw_signoff returns READY, WARN, or BLOCK with findings.",
        ),
        next_action(
            "run_project_audit",
            "Aggregate project readiness, manifests, waivers, and signoff into audit evidence.",
            required_args=["run_name"],
            arg_sources={"run_name": f"completed run {run_name}"},
            preconditions=["Artifact and report collection have been attempted."],
            stop_condition="run_project_audit returns READY, WARN, or BLOCK with active findings.",
        ),
        next_action(
            "collect_diagnostic_bundle",
            "Create a project-local diagnostic reference index, including workflow trace.",
            required_args=["run_name"],
            arg_sources={"run_name": f"completed run {run_name}"},
            preconditions=["Project audit and signoff evidence are available."],
            stop_condition="collect_diagnostic_bundle returns manifest_path.",
        ),
        next_action(
            "validate_diagnostic_bundle",
            "Validate diagnostic reference integrity and reviewability before project-local handoff review.",
            required_args=["manifest_path"],
            arg_sources={"manifest_path": "collect_diagnostic_bundle.data.manifest_path"},
            preconditions=["Diagnostic bundle manifest exists."],
            stop_condition="validate_diagnostic_bundle returns reviewable WARN without claiming portable handoff readiness.",
        ),
    ]


def _artifact_output_dir_outside_project_failure(
    *,
    command: str,
    context: dict[str, str],
    run_name: str,
    requested_output_dir: Any,
    message: str,
) -> dict[str, Any]:
    project_dir = Path(str(context.get("project_dir", ""))).resolve() if context.get("project_dir") else Path()
    allowed_output_root = (project_dir / "vmcp_artifacts" / run_name).resolve() if context.get("project_dir") else Path("vmcp_artifacts") / run_name
    requested = Path(str(requested_output_dir)).resolve() if requested_output_dir else Path()
    data = {
        "command": command,
        "context": context,
        "project_dir": str(project_dir) if context.get("project_dir") else "",
        "requested_output_dir": str(requested) if requested_output_dir else "",
        "allowed_output_root": str(allowed_output_root),
        "run_name": run_name,
        "reason": message,
        "next_actions": [
            next_action(
                "collect_build_artifacts",
                "Retry without output_dir so artifacts are written to the project-local vmcp_artifacts tree.",
                required_args=["run_name"],
                arg_sources={"run_name": f"same run_name {run_name}", "output_dir": "omit this argument"},
                preconditions=["Bitstream/run artifacts still exist inside the current Vivado project directory."],
                stop_condition="collect_build_artifacts returns ok=true and manifest_path under <project_dir>/vmcp_artifacts.",
            ),
            next_action(
                "collect_build_artifacts",
                "Retry with an explicit project-local vmcp_artifacts output_dir if a custom path is required.",
                required_args=["run_name", "output_dir"],
                arg_sources={"run_name": f"same run_name {run_name}", "output_dir": str(allowed_output_root)},
                preconditions=["The requested output_dir is inside the current project_dir."],
                stop_condition="collect_build_artifacts returns ok=true and manifest_path under the requested project-local output_dir.",
                optional=True,
            ),
        ],
    }
    return failure(
        "collect_build_artifacts",
        "ARTIFACT_OUTPUT_DIR_OUTSIDE_PROJECT",
        "collect_build_artifacts output_dir must stay inside the current Vivado project directory.",
        data=data,
    )


def _report_output_dir_outside_project_failure(
    *,
    command: str,
    context: dict[str, str],
    run_name: str,
    requested_report_dir: Any,
    resolved_report_dir: str,
    message: str,
) -> dict[str, Any]:
    project_dir = Path(str(context.get("project_dir", ""))).resolve() if context.get("project_dir") else Path()
    allowed_report_dir = (project_dir / "vmcp_reports" / run_name).resolve() if context.get("project_dir") else Path("vmcp_reports") / run_name
    requested = Path(str(requested_report_dir)).resolve() if requested_report_dir else Path()
    data = {
        "command": command,
        "context": context,
        "project_dir": str(project_dir) if context.get("project_dir") else "",
        "requested_report_dir": str(requested) if requested_report_dir else "",
        "resolved_report_dir": str(resolved_report_dir),
        "allowed_report_dir": str(allowed_report_dir),
        "run_name": run_name,
        "reason": message,
        "next_actions": [
            next_action(
                "collect_report_bundle",
                "Retry without report_dir so reports are written to the project-local vmcp_reports tree.",
                required_args=["run_name"],
                arg_sources={"run_name": f"same run_name {run_name}", "report_dir": "omit this argument"},
                preconditions=["The implementation run is complete and current project_dir is correct."],
                stop_condition="collect_report_bundle returns report_manifest.json under <project_dir>/vmcp_reports/<run_name>.",
            ),
            next_action(
                "collect_report_bundle",
                "Retry with an explicit project-local report_dir if a custom report path is required.",
                required_args=["run_name", "report_dir"],
                arg_sources={"run_name": f"same run_name {run_name}", "report_dir": str(allowed_report_dir)},
                preconditions=["The requested report_dir is inside the current project_dir."],
                stop_condition="collect_report_bundle returns report_manifest.json under the requested project-local report_dir.",
                optional=True,
            ),
        ],
    }
    return failure(
        "collect_report_bundle",
        "REPORT_OUTPUT_DIR_OUTSIDE_PROJECT",
        "collect_report_bundle report_dir must stay inside the current Vivado project directory; refusing to write outside project directory.",
        data=data,
    )


def _post_artifact_handoff_actions(run_name: str, *, manifest_path: str = "") -> list[dict[str, Any]]:
    actions = _post_bitstream_handoff_actions(run_name)[1:]
    actions[0] = next_action(
        "collect_report_bundle",
        "Collect reports after artifact collection; a bitstream alone is not a complete Agent handoff.",
        required_args=["run_name"],
        arg_sources={"run_name": f"artifact manifest run {run_name}", "manifest_path": manifest_path or "collect_build_artifacts.data.manifest_path"},
        preconditions=["collect_build_artifacts returned ok=true."],
        stop_condition="collect_report_bundle returns report_manifest.json.",
    )
    return actions


def _tcl_timeout_data(
    *,
    tool: str,
    error: TimeoutError,
    timeout_s: int,
    command: str,
    request_context: dict[str, Any],
    session_status: dict[str, Any],
    partial_context: dict[str, Any],
    next_actions: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    runtime_dir = str(session_status.get("runtime_dir") or session_status.get("temp_dir") or "")
    stdout_path = str(session_status.get("stdout_path", ""))
    stderr_path = str(session_status.get("stderr_path", ""))
    payload = {
        "status": "BLOCK",
        "timeout_s_used": timeout_s,
        "command_excerpt": _command_excerpt(command),
        "request_context": request_context,
        "session_status": session_status,
        "runtime_dir": runtime_dir,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdout_tail": _read_text_tail(stdout_path),
        "stderr_tail": _read_text_tail(stderr_path),
        "partial_success": bool(partial_context.get("partial_success", False)),
        "timeout_error": str(error),
        **partial_context,
    }
    payload["next_actions"] = next_actions or _generic_timeout_actions(tool, payload)
    return payload


def _command_excerpt(command: str, limit: int = 1200) -> str:
    text = command.replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 15] + "...<truncated>"


def _read_text_tail(path: str | Path | None, *, max_bytes: int = 8192) -> str:
    if not path:
        return ""
    file_path = Path(str(path))
    if not file_path.exists() or not file_path.is_file():
        return ""
    try:
        size = file_path.stat().st_size
        with file_path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read(max_bytes)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()


def _create_project_partial_context(
    *,
    project_name: str,
    project_dir: str,
    part: str,
    top: str,
    testbench_top: str,
    rtl_files: list[str],
    xdc_files: list[str],
    sim_files: list[str],
    target_language: Any,
    vivado_target_language: str | None,
    sv_file_type: str | None,
) -> dict[str, Any]:
    project_path = Path(project_dir) / f"{project_name}.xpr"
    partial_success = project_path.exists()
    return {
        "partial_success": partial_success,
        "project_capability_bound": False,
        "mutation_scope": "unbound",
        "recovery_policy": "inspection_then_rebuild" if partial_success else "retry_create_project",
        "project_path": str(project_path),
        "project_dir": project_dir,
        "project_name": project_name,
        "part": part,
        "top": top,
        "testbench_top": testbench_top,
        "planned_files": {"rtl": rtl_files, "xdc": xdc_files, "sim": sim_files},
        "planned_language_policy": _planned_language_policy(target_language, vivado_target_language, sv_file_type),
        "setup_status": _planned_setup_status(
            partial_success=partial_success,
            project_path=str(project_path),
            rtl_files=rtl_files,
            xdc_files=xdc_files,
            sim_files=sim_files,
            top=top,
            testbench_top=testbench_top,
        ),
    }


def _planned_language_policy(target_language: Any, vivado_target_language: str | None, sv_file_type: str | None) -> dict[str, Any]:
    note = ""
    if sv_file_type == "SystemVerilog":
        note = (
            "SystemVerilog is enabled through per-file file_type=SystemVerilog for .sv sources; "
            "Vivado project target_language remains Verilog as a compatibility setting."
        )
    return {
        "requested_target_language": target_language,
        "vivado_project_target_language": vivado_target_language,
        "source_file_type_policy": sv_file_type,
        "language_policy_note": note,
    }


def _planned_setup_status(
    *,
    partial_success: bool,
    project_path: str,
    rtl_files: list[str],
    xdc_files: list[str],
    sim_files: list[str],
    top: str,
    testbench_top: str,
    missing_files: list[str] | None = None,
) -> dict[str, Any]:
    missing = list(missing_files if missing_files is not None else _missing_input_files(rtl_files + xdc_files + sim_files))
    missing_set = set(missing)
    filesets = {
        "sources_1": {
            "expected_files": rtl_files,
            "expected_count": len(rtl_files),
            "planned_repair": bool(rtl_files),
            "needs_repair": any(item in missing_set for item in rtl_files),
        },
        "constrs_1": {
            "expected_files": xdc_files,
            "expected_count": len(xdc_files),
            "planned_repair": bool(xdc_files),
            "needs_repair": any(item in missing_set for item in xdc_files),
        },
        "sim_1": {
            "expected_files": sim_files,
            "expected_count": len(sim_files),
            "planned_repair": bool(sim_files or testbench_top),
            "needs_repair": any(item in missing_set for item in sim_files),
        },
    }
    project_exists = bool(project_path and Path(project_path).exists())
    if missing:
        status = "BLOCK"
    elif partial_success:
        status = "WARN"
    else:
        status = "BLOCK"
    return {
        "status": status,
        "status_scope": "planned_preflight",
        "actual_state_known": False,
        "project_path": project_path,
        "project_exists": project_exists,
        "planned_open_project": bool(project_path),
        "needs_open_project": False,
        "planned_fileset_repair": any(item["planned_repair"] for item in filesets.values()),
        "needs_fileset_repair": any(item["needs_repair"] for item in filesets.values()),
        "planned_preflight_note": "In planned_preflight, planned_* fields describe actions the tool would take; needs_* fields only describe known missing inputs because current Vivado fileset state has not been observed.",
        "top": top,
        "testbench_top": testbench_top,
        "filesets": filesets,
        "missing_expected_files": missing,
    }


def _post_repair_setup_status(setup_status: dict[str, Any], *, status: str, missing_after: list[str]) -> dict[str, Any]:
    missing = set(missing_after)
    filesets: dict[str, Any] = {}
    for name, fileset in dict(setup_status.get("filesets", {})).items():
        expected_files = [str(item) for item in fileset.get("expected_files", [])]
        filesets[name] = dict(fileset) | {"needs_repair": any(item in missing for item in expected_files)}
    return dict(setup_status) | {
        "status": "BLOCK" if missing_after else status,
        "status_scope": "post_repair",
        "actual_state_known": True,
        "needs_open_project": False,
        "needs_fileset_repair": bool(missing_after),
        "filesets": filesets,
        "missing_expected_files": missing_after,
    }


def _missing_input_files(paths: list[str]) -> list[str]:
    missing: list[str] = []
    for item in paths:
        if item and not Path(item).exists():
            missing.append(item)
    return missing


def _create_project_timeout_actions(context: dict[str, Any]) -> list[dict[str, Any]]:
    actions = [
        next_action(
            "session_status",
            "Check whether Vivado is still running and whether the Tcl channel is connected after the timeout.",
            stop_condition="session_status reports connected/process_running state.",
        ),
        next_action(
            "stop_session",
            "Stop the managed Vivado session before reconnecting to a partial project if the Tcl channel is disconnected.",
            preconditions=["session_status reports connected=false or the current Tcl channel is unusable."],
            stop_condition="stop_session returns stopped=true or confirms no session is active.",
            optional=True,
        ),
        next_action(
            "start_session",
            "Start a fresh Vivado generation after stopping a timed-out or tainted session.",
            preconditions=["stop_session completed, or session_status confirms no reusable session is active."],
            stop_condition="start_session returns ok=true before open_project or create_project is retried.",
            optional=True,
        ),
    ]
    if context.get("partial_success") and context.get("project_path"):
        actions.extend(
            [
                next_action(
                    "open_project",
                    "Open the unbound partial project for inspection only; timeout did not establish an MCP project capability.",
                    required_args=["project_path"],
                    arg_sources={"project_path": str(context["project_path"])},
                    preconditions=["A new Vivado session is started and the partial .xpr exists."],
                    stop_condition="open_project returns mutation_policy.scope=existing_project_read_only; do not execute or repair this project in place.",
                ),
                next_action(
                    "create_project",
                    "Rebuild the reviewed inputs as a new MCP-managed project in a distinct empty directory.",
                    required_args=["project_name", "project_dir", "part", "top", "rtl_files"],
                    arg_sources={
                        "project_name": f"{context.get('project_name', 'project')}_recovered",
                        "project_dir": "a new empty directory distinct from the timed-out partial project",
                        "part": str(context.get("part", "")),
                        "rtl_files": "create_project.data.planned_files.rtl",
                        "xdc_files": "create_project.data.planned_files.xdc",
                        "sim_files": "create_project.data.planned_files.sim",
                        "top": str(context.get("top", "")),
                        "testbench_top": str(context.get("testbench_top", "")),
                    },
                    preconditions=["The partial project was inspected without mutation and the original RTL/XDC/simulation inputs remain trusted."],
                    stop_condition="create_project returns ok=true with project_capability.bound=true for the new project path.",
                ),
            ]
        )
    else:
        actions.append(
            next_action(
                "create_project",
                "Retry project creation after verifying Vivado session health; no partial .xpr was found.",
                required_args=["project_name", "project_dir", "part", "top", "rtl_files"],
                arg_sources={"project_name": "same request", "project_dir": "same request", "part": "same request", "top": "same request", "rtl_files": "same request"},
                preconditions=["session_status/start_session confirms a healthy Tcl channel."],
                stop_condition="create_project returns ok=true or partial_success=true with project_path.",
            )
        )
    return actions


def _existing_project_rebuild_actions() -> list[dict[str, Any]]:
    actions = [
        next_action(
            "get_project_state",
            "Read the existing project identity, part, design top, and simulation top without mutation.",
            stop_condition="get_project_state returns the part, design top, and simulation top used by the working-project plan.",
        )
    ]
    for fileset in ("sources_1", "constrs_1", "sim_1"):
        actions.append(
            next_action(
                "list_fileset_files",
                f"Read the existing {fileset} file inventory without executing project inputs.",
                required_args=["fileset"],
                arg_sources={"fileset": fileset},
                preconditions=["The existing project is open in inspection-only mode."],
                stop_condition=f"list_fileset_files returns the reviewed {fileset} file inventory.",
            )
        )
    actions.append(
        next_action(
            "close_project",
            "Close the inspection-only existing project before creating the separate MCP-managed working project.",
            preconditions=[
                "All required existing-project state and fileset inventories were captured.",
                "get_project_state.data.fileset_properties.discovery_status is READY.",
            ],
            stop_condition="close_project returns ok=true and no project remains open in Vivado.",
        )
    )
    actions.append(
        next_action(
            "create_project",
            "Create a separate MCP-managed Project Mode working project from the reviewed fileset inventories.",
            required_args=[
                "project_name",
                "project_dir",
                "part",
                "top",
                "rtl_files",
                "xdc_files",
                "sim_files",
                "file_specs",
                "testbench_top",
                "target_language",
                "simulator",
                "source_include_dirs",
                "source_defines",
                "include_dirs",
                "defines",
            ],
            arg_sources={
                "project_name": "get_project_state.data.project.name + '_working'",
                "project_dir": "get_project_state.data.project.directory + '_working'",
                "part": "get_project_state.data.project.part",
                "top": "get_project_state.data.project.top",
                "rtl_files": "list_fileset_files(fileset='sources_1').data.files[*].path where exists=true",
                "xdc_files": "list_fileset_files(fileset='constrs_1').data.files[*].path where exists=true",
                "sim_files": "list_fileset_files(fileset='sim_1').data.files[*].path where exists=true",
                "file_specs": "concatenate list_fileset_files(fileset='sources_1|constrs_1|sim_1').data.file_specs without modification",
                "testbench_top": "get_project_state.data.project.sim_top",
                "target_language": "SystemVerilog when any reviewed sources_1/sim_1 file_type is SystemVerilog; otherwise get_project_state.data.project.target_language",
                "simulator": "get_project_state.data.project.target_simulator",
                "source_include_dirs": "get_project_state.data.fileset_properties.sources_1.include_dirs",
                "source_defines": "get_project_state.data.fileset_properties.sources_1.defines",
                "include_dirs": "get_project_state.data.fileset_properties.sim_1.include_dirs",
                "defines": "get_project_state.data.fileset_properties.sim_1.defines",
            },
            preconditions=[
                "The existing project state and all three fileset inventories were inspected.",
                "get_project_state.data.fileset_properties.discovery_status is READY; otherwise stop before close_project.",
                "Each list_fileset_files result has reconstruction_status=READY and a non-empty semantic_inventory_digest; otherwise stop before close_project.",
                "All selected RTL, XDC, and simulation inputs passed executable-input policy review.",
                "project_dir is a new path distinct from the existing project directory.",
            ],
            stop_condition="create_project returns ok=true, file_semantics.reconstruction_equivalent=true, and project_capability.bound=true for a distinct project_path.",
        )
    )
    return actions


def _setup_repair_actions(context: dict[str, Any]) -> list[dict[str, Any]]:
    fileset = str(context.get("fileset") or context.get("simset") or "")
    actions = [
        next_action(
            "session_status",
            "Check whether the Vivado Tcl channel survived the setup operation timeout.",
            stop_condition="session_status reports connected/process_running state.",
        )
    ]
    if fileset:
        actions.append(
            next_action(
                "list_fileset_files",
                "Inspect the affected fileset before deciding whether setup repair is needed.",
                required_args=["fileset"],
                arg_sources={"fileset": fileset},
                preconditions=["A Vivado project is open and the Tcl channel is connected."],
                stop_condition="list_fileset_files returns the current fileset references.",
            )
        )
    actions.append(
        next_action(
            "repair_project_setup",
            "Reconcile the Project Mode setup using the known source, constraint, simulation, and top-level inputs.",
            preconditions=["Project is open or project_path is supplied; missing source files have been resolved."],
            stop_condition="repair_project_setup returns READY or a structured missing input failure.",
        )
    )
    return actions


def _post_setup_repair_actions() -> list[dict[str, Any]]:
    return [
        next_action(
            "check_syntax",
            "Verify source files after Project Mode setup repair.",
            required_args=["fileset"],
            arg_sources={"fileset": "sources_1"},
            preconditions=["repair_project_setup returned READY or REPAIRED."],
            stop_condition="check_syntax returns READY or structured syntax findings.",
        ),
        next_action(
            "get_compile_order",
            "Inspect compile order after setup repair.",
            required_args=["fileset"],
            arg_sources={"fileset": "sources_1 or sim_1"},
            preconditions=["repair_project_setup returned READY or REPAIRED."],
            stop_condition="get_compile_order returns missing/duplicate/type diagnostics.",
            optional=True,
        ),
        next_action(
            "run_behavioral_simulation",
            "Run finite behavioral simulation after sim_1 has been repaired.",
            required_args=["simset"],
            arg_sources={"simset": "sim_1"},
            preconditions=["sim_1 contains the testbench and testbench_top is set."],
            stop_condition="run_behavioral_simulation returns completed or structured simulation findings.",
            optional=True,
        ),
    ]


def _generic_timeout_actions(tool: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        next_action(
            "session_status",
            f"Check Vivado session health after {tool} timed out.",
            stop_condition="session_status reports connected/process_running state.",
        ),
        next_action(
            "get_workflow_trace_status",
            "Capture workflow trace state before retrying or handing off.",
            preconditions=["The timeout result has been recorded in workflow trace."],
            stop_condition="get_workflow_trace_status returns trace and unresolved failure context.",
            optional=True,
        ),
    ]


def _project_audit_timeout_actions(*, project_path: str = "") -> list[dict[str, Any]]:
    actions = [
        next_action(
            "session_status",
            "Check whether the Vivado Tcl channel survived the project audit timeout.",
            stop_condition="session_status reports connected/process_running state.",
        ),
        next_action(
            "stop_session",
            "Stop the managed session if run_project_audit disconnected or left Vivado unresponsive.",
            preconditions=["run_project_audit.data.session_status.connected is false or Tcl channel is unusable."],
            stop_condition="stop_session returns stopped=true or confirms no session is active.",
            optional=True,
        ),
        next_action(
            "start_session",
            "Restart Vivado before reopening the project and retrying the audit.",
            arg_sources={"timeout_s": str(RECOMMENDED_RETRY_TIMEOUT_S)},
            preconditions=["stop_session completed or no managed session is active."],
            stop_condition="start_session returns ok=true.",
            optional=True,
        ),
    ]
    if project_path:
        actions.append(
            next_action(
                "open_project",
                "Reopen the project that was being audited before the timeout.",
                required_args=["project_path"],
                arg_sources={"project_path": project_path},
                preconditions=["start_session returned ok=true and the .xpr exists."],
                stop_condition="open_project returns ok=true.",
            )
        )
    actions.extend(
        [
            next_action(
                "run_project_audit",
                "Retry the audit after session recovery; increase timeout_s if Vivado reports remain slow.",
                arg_sources={"timeout_s": str(RECOMMENDED_RETRY_TIMEOUT_S)},
                preconditions=["The project is open and artifact/report/signoff evidence is available."],
                stop_condition="run_project_audit returns READY, WARN, or BLOCK with findings.",
            ),
            next_action(
                "collect_diagnostic_bundle",
                "Package partial evidence if audit cannot be completed within the available time window.",
                preconditions=["Repeated run_project_audit timeout prevents fresh audit completion."],
                stop_condition="collect_diagnostic_bundle returns a manifest or structured partial failure.",
                optional=True,
            ),
        ]
    )
    return actions


def _pre_hw_signoff_timeout_actions(*, run_name: str, project_path: str = "", report_manifest_path: str = "") -> list[dict[str, Any]]:
    actions = [
        next_action(
            "session_status",
            "Check whether the Vivado Tcl channel survived the pre-hardware signoff timeout.",
            stop_condition="session_status reports connected/process_running state.",
        ),
        next_action(
            "stop_session",
            "Stop the managed session if run_pre_hw_signoff disconnected or left Vivado unresponsive.",
            preconditions=["run_pre_hw_signoff.data.session_status.connected is false or Tcl channel is unusable."],
            stop_condition="stop_session returns stopped=true or confirms no session is active.",
            optional=True,
        ),
        next_action(
            "start_session",
            "Restart Vivado before reopening the project and retrying pre-hardware signoff.",
            arg_sources={"timeout_s": str(RECOMMENDED_RETRY_TIMEOUT_S)},
            preconditions=["stop_session completed or no managed session is active."],
            stop_condition="start_session returns ok=true.",
            optional=True,
        ),
    ]
    if project_path:
        actions.append(
            next_action(
                "open_project",
                "Reopen the project that was in pre-hardware signoff before the timeout.",
                required_args=["project_path"],
                arg_sources={"project_path": project_path},
                preconditions=["start_session returned ok=true and the .xpr exists."],
                stop_condition="open_project returns ok=true.",
            )
        )
    if not report_manifest_path:
        actions.append(
            next_action(
                "collect_report_bundle",
                "Regenerate trusted report evidence before retrying signoff; this avoids rerunning heavy report Tcl inside run_pre_hw_signoff.",
                required_args=["run_name"],
                arg_sources={"run_name": run_name},
                preconditions=["The project is open and implementation reports are available."],
                stop_condition="collect_report_bundle returns a project-local report_manifest.json.",
            )
        )
    signoff_arg_sources = {"run_name": run_name, "timeout_s": str(RECOMMENDED_RETRY_TIMEOUT_S)}
    required_args = ["run_name"]
    if report_manifest_path:
        signoff_arg_sources["report_manifest_path"] = report_manifest_path
        required_args.append("report_manifest_path")
    actions.extend(
        [
            next_action(
                "run_pre_hw_signoff",
                "Retry pre-hardware signoff after session recovery, preferably with trusted report manifest evidence.",
                required_args=required_args,
                arg_sources=signoff_arg_sources,
                preconditions=["The project is open and a fresh finite behavioral simulation result exists."],
                stop_condition="run_pre_hw_signoff returns READY, WARN, or BLOCK with findings.",
            ),
            next_action(
                "collect_diagnostic_bundle",
                "Package partial evidence if pre-hardware signoff cannot be completed within the available time window.",
                required_args=["run_name"],
                arg_sources={"run_name": run_name},
                preconditions=["Repeated run_pre_hw_signoff timeout prevents fresh signoff completion."],
                stop_condition="collect_diagnostic_bundle returns a manifest or structured partial failure.",
                optional=True,
            ),
        ]
    )
    return actions


def _project_path_from_state(project_state: dict[str, Any]) -> str:
    project = project_state.get("project", {}) if isinstance(project_state, dict) else {}
    directory = str(project.get("directory", "") or "")
    name = str(project.get("name", "") or "")
    if not directory or not name:
        return ""
    return str(Path(directory) / f"{name}.xpr")


def _repair_project_setup_operations(
    *,
    project_path: str,
    rtl_files: list[str],
    xdc_files: list[str],
    sim_files: list[str],
    top: str,
    testbench_top: str,
    include_dirs: list[str],
    defines: dict[str, str | None],
    simulator: str,
    vivado_target_language: str | None,
    sv_file_type: str | None,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    if project_path:
        operations.append({"operation": "open_project_if_needed", "project_path": project_path})
    if rtl_files:
        operations.append({"operation": "add_files", "fileset": "sources_1", "files": rtl_files})
    if xdc_files:
        operations.append({"operation": "add_files", "fileset": "constrs_1", "files": xdc_files})
    if top:
        operations.append({"operation": "set_top", "fileset": "sources_1", "top": top})
    if vivado_target_language:
        operations.append({"operation": "set_project_target_language", "language": vivado_target_language})
    operations.append({"operation": "set_target_simulator", "simulator": simulator})
    if sim_files:
        operations.append({"operation": "add_files", "fileset": "sim_1", "files": sim_files})
    if testbench_top:
        operations.append({"operation": "set_top", "fileset": "sim_1", "top": testbench_top})
    if include_dirs:
        operations.append({"operation": "set_include_dirs", "fileset": "sim_1", "include_dirs": include_dirs})
    if defines:
        operations.append({"operation": "set_verilog_define", "fileset": "sim_1", "defines": defines})
    if sv_file_type:
        operations.append({"operation": "set_file_type", "file_type": sv_file_type})
    operations.append({"operation": "update_compile_order", "filesets": ["sources_1", "sim_1"]})
    return [_as_reconcile_operation(item) for item in operations]


def _as_reconcile_operation(operation: dict[str, Any]) -> dict[str, Any]:
    return operation | {
        "operation_semantics": "reconcile",
        "idempotent": True,
        "duplicates_expected": False,
    }


def _repair_project_setup_command(
    *,
    project_path: str,
    rtl_files: list[str],
    xdc_files: list[str],
    sim_files: list[str],
    top: str,
    testbench_top: str,
    include_dirs: list[str],
    defines: dict[str, str | None],
    simulator: str,
    vivado_target_language: str | None,
    sv_file_type: str | None,
) -> str:
    parts: list[str] = [tcl_wire_prelude()]
    if project_path:
        parts.append(
            f"set vmcp_target_project_path [file normalize {tcl_list_quote(project_path)}]; "
            "set vmcp_project_already_open 0; "
            "catch {"
            "set vmcp_current_project_path [file normalize [file join [get_property DIRECTORY [current_project]] "
            "\"[get_property NAME [current_project]].xpr\"]]; "
            "if {$vmcp_current_project_path eq $vmcp_target_project_path} {set vmcp_project_already_open 1}"
            "}; "
            f"if {{$vmcp_project_already_open == 0}} {{open_project {tcl_list_quote(project_path)}}}"
        )
    parts.append(run_hook_guard_command(close_project_on_block=True))
    if rtl_files:
        parts.append(f"add_files {_tcl_list(rtl_files)}")
    if xdc_files:
        parts.append(f"add_files -fileset {{constrs_1}} {_tcl_list(xdc_files)}")
    if vivado_target_language:
        parts.append(f"set_property target_language {tcl_list_quote(vivado_target_language)} [current_project]")
    parts.append(f"set_property target_simulator {tcl_list_quote(simulator)} [current_project]")
    if top:
        parts.append(f"set_property top {tcl_list_quote(top)} [get_filesets {{sources_1}}]")
    if sim_files or testbench_top or include_dirs or defines:
        parts.append(
            configure_simulation_command(
                sim_files=sim_files,
                testbench_top=testbench_top or None,
                include_dirs=include_dirs,
                defines=defines,
                simulator=simulator,
            )
        )
    if sv_file_type:
        sv_files = _systemverilog_files(rtl_files + sim_files)
        if sv_files:
            parts.append(
                f"foreach f {_tcl_list(sv_files)} "
                f"{{set matches [get_files -quiet $f]; if {{[llength $matches] > 0}} {{set_property file_type {tcl_list_quote(sv_file_type)} $matches}}}}"
            )
    parts.extend(
        [
            "set vmcp_postcondition_errors [list]",
            "set vmcp_missing_after_repair [list]",
            "if {[catch {update_compile_order -fileset sources_1} vmcp_postcondition_error]} {lappend vmcp_postcondition_errors \"sources_1 compile-order update failed: $vmcp_postcondition_error\"}",
            "if {[catch {update_compile_order -fileset sim_1} vmcp_postcondition_error]} {lappend vmcp_postcondition_errors \"sim_1 compile-order update failed: $vmcp_postcondition_error\"}",
        ]
    )
    for fileset_name, expected_files in (
        ("sources_1", rtl_files),
        ("constrs_1", xdc_files),
        ("sim_1", sim_files),
    ):
        expected_expr = _tcl_list(expected_files)
        fileset_ref = tcl_list_quote(fileset_name)
        parts.append(
            f"set vmcp_expected_files {expected_expr}; "
            f"if {{[catch {{set vmcp_post_fileset [get_filesets {fileset_ref}]}} vmcp_postcondition_error]}} {{"
            f"lappend vmcp_postcondition_errors \"{fileset_name} discovery failed: $vmcp_postcondition_error\""
            "} elseif {[llength $vmcp_post_fileset] == 0} {"
            f"lappend vmcp_missing_after_repair \"fileset:{fileset_name}\""
            "} elseif {[catch {set vmcp_actual_files [get_files -of_objects $vmcp_post_fileset]} vmcp_postcondition_error]} {"
            f"lappend vmcp_postcondition_errors \"{fileset_name} file discovery failed: $vmcp_postcondition_error\""
            "} else {foreach vmcp_expected_file $vmcp_expected_files {"
            "set vmcp_expected_normalized [file normalize $vmcp_expected_file]; set vmcp_found 0; "
            "foreach vmcp_actual_file $vmcp_actual_files {if {[string equal -nocase $vmcp_expected_normalized [file normalize $vmcp_actual_file]]} {set vmcp_found 1; break}}; "
            f"if {{$vmcp_found == 0}} {{lappend vmcp_missing_after_repair \"{fileset_name}:$vmcp_expected_normalized\"}}"
            "}}"
        )
    if top:
        parts.append(
            "if {[catch {set vmcp_sources_top [get_property TOP [get_filesets {sources_1}]]} vmcp_postcondition_error]} {"
            "lappend vmcp_postcondition_errors \"sources_1 TOP discovery failed: $vmcp_postcondition_error\""
            f"}} elseif {{![string equal $vmcp_sources_top {tcl_list_quote(top)}]}} {{lappend vmcp_missing_after_repair \"sources_1:top:{top}\"}}"
        )
    if testbench_top:
        parts.append(
            "if {[catch {set vmcp_sim_top [get_property TOP [get_filesets {sim_1}]]} vmcp_postcondition_error]} {"
            "lappend vmcp_postcondition_errors \"sim_1 TOP discovery failed: $vmcp_postcondition_error\""
            f"}} elseif {{![string equal $vmcp_sim_top {tcl_list_quote(testbench_top)}]}} {{lappend vmcp_missing_after_repair \"sim_1:top:{testbench_top}\"}}"
        )
    parts.extend(
        [
            "set vmcp_postcondition_discovery_status [expr {[llength $vmcp_postcondition_errors] > 0 ? {ERROR} : {READY}}]",
            "set vmcp_setup_status [expr {[llength $vmcp_postcondition_errors] > 0 ? {ERROR} : ([llength $vmcp_missing_after_repair] > 0 ? {BLOCK} : {READY})}]",
            "join [list \"setup_status=$vmcp_setup_status\" \"postcondition_discovery_status=$vmcp_postcondition_discovery_status\" \"missing_after_repair=[::vivado_agent_mcp_wire_list $vmcp_missing_after_repair]\" \"discovery_errors=[::vivado_agent_mcp_wire_list $vmcp_postcondition_errors]\"] \"\\n\"",
        ]
    )
    return "; ".join(parts)


def _validated_simulation_defines(
    tool: str,
    raw_defines: Any,
) -> tuple[dict[str, str | None], dict[str, Any] | None]:
    try:
        return validate_simulation_defines(raw_defines), None
    except ValueError as exc:
        return {}, failure(
            tool,
            "SIMULATION_DEFINE_INVALID",
            str(exc),
            data={"defines": raw_defines, "simulation_isolation": "trusted_project_host_execution_not_os_sandboxed"},
        )


def _validated_source_defines(
    tool: str,
    raw_defines: Any,
) -> tuple[dict[str, str | None], dict[str, Any] | None]:
    try:
        return validate_simulation_defines(raw_defines), None
    except ValueError as exc:
        return {}, failure(
            tool,
            "SOURCE_DEFINE_INVALID",
            str(exc),
            data={"source_defines": raw_defines},
        )


def _trusted_simulation_failure(
    args: dict[str, Any],
    *,
    preflight: dict[str, Any],
    source_identity: dict[str, Any],
) -> dict[str, Any] | None:
    project_dir = str(preflight.get("project_dir", "")).strip()
    execution_intent = str(args.get("execution_intent", "")).strip()
    confirm = str(args.get("confirm", "")).strip()
    configured = [item.strip() for item in os.environ.get(TRUSTED_SIMULATION_ROOTS_ENV, "").split(os.pathsep) if item.strip()]
    reasons: list[str] = []
    if not execution_intent:
        reasons.append("execution_intent is required")
    if confirm != TRUSTED_SIMULATION_CONFIRM:
        reasons.append(f"confirm must equal {TRUSTED_SIMULATION_CONFIRM}")
    if not configured:
        reasons.append(f"server environment {TRUSTED_SIMULATION_ROOTS_ENV} is not configured")
    if not project_dir:
        reasons.append("simulation preflight did not return project_dir")

    trust_closure: dict[str, Any] = {"status": "BLOCK", "issues": []}
    if not reasons:
        trust_closure = validate_simulation_trust_closure(
            preflight,
            source_identity,
            trusted_roots=configured,
        )
        reasons.extend(str(item) for item in trust_closure.get("issues", []) if str(item))
    if not reasons:
        preflight["trusted_project_root"] = str(trust_closure.get("accepted_root", ""))
        preflight["trusted_execution_closure"] = trust_closure
        binding_payload = {
            "execution_intent": execution_intent,
            "confirm": confirm,
            "source_identity_sha256": str(source_identity.get("sha256", "")),
            "trusted_project_root": str(trust_closure.get("accepted_root", "")),
        }
        preflight["simulation_execution_binding_sha256"] = hashlib.sha256(
            json.dumps(binding_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        preflight["simulation_isolation"] = "trusted_project_host_execution_not_os_sandboxed"
        return None
    return failure(
        "run_behavioral_simulation",
        "SIMULATION_SOURCE_UNTRUSTED",
        "XSIM host execution is restricted to server-configured trusted project roots.",
        data={
            "project_dir": project_dir,
            "trusted_project_roots_configured": len(configured),
            "reasons": reasons,
            "trusted_execution_closure": trust_closure,
            "simulation_source_identity": source_identity,
            "simulation_isolation": "trusted_project_host_execution_not_os_sandboxed",
            "next_actions": [
                next_action(
                    "run_behavioral_simulation",
                    "Retry only after the server administrator trusts this project root and the caller records explicit XSIM intent.",
                    required_args=["execution_intent", "confirm"],
                    arg_sources={
                        "execution_intent": "human-reviewed reason for executing trusted HDL/testbench code",
                        "confirm": TRUSTED_SIMULATION_CONFIRM,
                    },
                    preconditions=[f"The project is inside a root configured by {TRUSTED_SIMULATION_ROOTS_ENV}."],
                    stop_condition="run_behavioral_simulation passes trusted-project preflight or remains BLOCK.",
                )
            ],
        },
    )


def _parse_key_value_lines(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _load_manifest_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    data.setdefault("manifest_path", str(path))
    return data


class ReportManifestValidationError(ValueError):
    def __init__(self, error_code: str, message: str, *, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.data = data or {}


def _validated_report_manifest(
    data: Any,
    *,
    manifest_path: Path,
    project_dir: Path,
    run_name: str,
    current_design_execution_identity: dict[str, Any],
) -> dict[str, Any]:
    project_path = project_dir.resolve()
    manifest = manifest_path.resolve()
    if not isinstance(data, dict):
        raise _report_manifest_error(
            "REPORT_MANIFEST_INVALID",
            "Report manifest must be a JSON object.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
        )
    _assert_path_inside(manifest, project_path, "report_manifest_path must resolve inside the current project directory", run_name=run_name)
    if manifest.name != "report_manifest.json":
        raise _report_manifest_error(
            "REPORT_MANIFEST_UNTRUSTED",
            "report_manifest_path must point to a report_manifest.json file.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
        )
    manifest_run_name = str(data.get("run_name", "")).strip()
    if manifest_run_name and manifest_run_name != run_name:
        raise _report_manifest_error(
            "REPORT_MANIFEST_RUN_MISMATCH",
            f"Report manifest run_name {manifest_run_name!r} does not match requested run {run_name!r}.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
            extra={"manifest_run_name": manifest_run_name},
        )

    report_dir = _resolve_report_manifest_dir(data.get("report_dir"), manifest)
    _assert_path_inside(report_dir, project_path, "Report manifest report_dir must resolve inside the current project directory", run_name=run_name)
    if manifest.parent != report_dir:
        raise _report_manifest_error(
            "REPORT_MANIFEST_UNTRUSTED",
            "report_manifest_path must be located in the manifest report_dir.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
            extra={"report_dir": str(report_dir)},
        )

    reports = data.get("reports")
    if not isinstance(reports, list) or not reports:
        raise _report_manifest_error(
            "REPORT_MANIFEST_INVALID",
            "Report manifest must contain a non-empty reports list.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
            extra={"report_dir": str(report_dir)},
        )

    validated_reports: list[dict[str, Any]] = []
    report_categories: set[str] = set()
    for index, item in enumerate(reports):
        if not isinstance(item, dict):
            raise _report_manifest_error(
                "REPORT_MANIFEST_INVALID",
                "Report manifest reports entries must be JSON objects.",
                manifest_path=manifest,
                project_dir=project_path,
                run_name=run_name,
                extra={"report_dir": str(report_dir), "report_index": index},
            )
        validated = _validated_report_manifest_entry(
            item,
            index=index,
            manifest_path=manifest,
            report_dir=report_dir,
            project_dir=project_path,
            run_name=run_name,
        )
        category = str(validated.get("category", "")).strip()
        if not category or category in report_categories:
            raise _report_manifest_error(
                "REPORT_MANIFEST_INVALID",
                f"Report manifest category must be non-empty and unique: {category!r}.",
                manifest_path=manifest,
                project_dir=project_path,
                run_name=run_name,
                extra={"report_dir": str(report_dir), "report_index": index, "category": category},
            )
        report_categories.add(category)
        validated_reports.append(validated)

    if data.get("schema_version") != 4:
        raise _report_manifest_error(
            "REPORT_MANIFEST_STALE",
            "Report manifest schema_version=4 is required for source-closure-bound evidence.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
        )
    recorded_design_identity = data.get("design_execution_identity")
    current_design_identity = (
        current_design_execution_identity
        if isinstance(current_design_execution_identity, dict)
        else {}
    )
    recorded_sha256 = str(data.get("design_execution_identity_sha256", ""))
    if (
        not isinstance(recorded_design_identity, dict)
        or recorded_design_identity.get("status") != "READY"
        or current_design_identity.get("status") != "READY"
        or not re.fullmatch(r"[0-9a-f]{64}", recorded_sha256)
        or recorded_sha256 != str(recorded_design_identity.get("sha256", ""))
    ):
        raise _report_manifest_error(
            "REPORT_MANIFEST_STALE",
            "Report manifest design execution identity is missing or invalid.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
        )
    if recorded_design_identity != current_design_identity:
        raise _report_manifest_error(
            "SOURCE_CLOSURE_CHANGED",
            "Current RTL/XDC/include/run configuration closure does not match the report manifest.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
            extra={
                "recorded_design_execution_identity_sha256": recorded_sha256,
                "current_design_execution_identity_sha256": str(current_design_identity.get("sha256", "")),
            },
        )
    if (
        str(data.get("vivado_version_short", "")) != "2021.2"
        or not str(data.get("vivado_build", "")).startswith("Vivado v2021.2")
        or str(data.get("report_command_schema", "")) != "vivado_2021_2_v1"
    ):
        raise _report_manifest_error(
            "REPORT_VERSION_MISMATCH",
            "Report manifest must be generated and attested by Vivado 2021.2 with the vivado_2021_2_v1 command schema.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
        )
    collection_id = str(data.get("collection_id", "")).strip()
    if not collection_id:
        raise _report_manifest_error(
            "REPORT_MANIFEST_STALE",
            "Report manifest collection_id is required.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
        )
    freshness = data.get("evidence_freshness")
    if not isinstance(freshness, dict) or str(freshness.get("status", "")).upper() != "FRESH":
        raise _report_manifest_error(
            "REPORT_MANIFEST_STALE",
            "Report manifest evidence_freshness.status must be FRESH.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
        )
    if _truthy_text(freshness.get("needs_refresh")):
        raise _report_manifest_error(
            "REPORT_MANIFEST_STALE",
            "Report manifest indicates that the Vivado run needs refresh.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
        )

    validated_categories = {
        str(item.get("category", ""))
        for item in validated_reports
    }
    missing_categories = sorted(
        {"timing", "utilization", "drc", "methodology", "messages"} - validated_categories
    )
    if missing_categories:
        raise _report_manifest_error(
            "REPORT_MANIFEST_INCOMPLETE",
            f"Report manifest is missing required current report categories: {', '.join(missing_categories)}.",
            manifest_path=manifest,
            project_dir=project_path,
            run_name=run_name,
        )

    result = dict(data)
    result["manifest_path"] = str(manifest)
    result["project_dir"] = str(project_path)
    result["report_dir"] = str(report_dir)
    result["run_name"] = run_name
    result["reports"] = validated_reports
    return result


def _latest_report_manifest_path(project_dir: Path, run_name: str) -> Path:
    run_root = project_dir / "vmcp_reports" / run_name
    legacy = run_root / "report_manifest.json"
    candidates = list((run_root / "invocations").glob("*/report_manifest.json"))
    return max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else legacy


def _validate_report_manifest_current_run(
    manifest: dict[str, Any],
    current_run: Any,
) -> None:
    if not isinstance(current_run, dict):
        raise ReportManifestValidationError(
            "REPORT_MANIFEST_CURRENT_RUN_UNAVAILABLE",
            "Current Vivado run state is not a structured object.",
            data={"manifest_path": manifest.get("manifest_path", "")},
        )
    current_status = str(current_run.get("status", "")).strip()
    current_progress = str(current_run.get("progress", "")).strip()
    current_needs_refresh = _truthy_text(current_run.get("needs_refresh"))
    if _classify_run_state(current_status) != "complete" or not _progress_is_complete(current_progress) or current_needs_refresh:
        raise ReportManifestValidationError(
            "REPORT_MANIFEST_STALE",
            "Current Vivado run is incomplete or needs refresh; report evidence is stale.",
            data={
                "manifest_path": manifest.get("manifest_path", ""),
                "current_run": current_run,
            },
        )
    snapshot = manifest.get("run_snapshot") if isinstance(manifest.get("run_snapshot"), dict) else {}
    if _classify_run_state(str(snapshot.get("status", ""))) != "complete":
        raise ReportManifestValidationError(
            "REPORT_MANIFEST_STALE",
            "Recorded report run snapshot is not complete.",
            data={"manifest_path": manifest.get("manifest_path", ""), "run_snapshot": snapshot},
        )
    if not _progress_is_complete(snapshot.get("progress")) or _truthy_text(snapshot.get("needs_refresh")):
        raise ReportManifestValidationError(
            "REPORT_MANIFEST_STALE",
            "Recorded report run snapshot is incomplete or needs refresh.",
            data={"manifest_path": manifest.get("manifest_path", ""), "run_snapshot": snapshot},
        )
    recorded_directory = _normalized_path_text(snapshot.get("directory"))
    current_directory = _normalized_path_text(current_run.get("directory"))
    if not recorded_directory or recorded_directory != current_directory:
        raise ReportManifestValidationError(
            "REPORT_MANIFEST_RUN_MISMATCH",
            "Current Vivado run directory does not match the report collection snapshot.",
            data={
                "manifest_path": manifest.get("manifest_path", ""),
                "recorded_run_directory": recorded_directory,
                "current_run_directory": current_directory,
            },
        )
    recorded_generation = str(snapshot.get("session_generation_id", ""))
    current_generation = str(current_run.get("session_generation_id", ""))
    if recorded_generation or current_generation:
        if not recorded_generation or recorded_generation != current_generation:
            raise ReportManifestValidationError(
                "REPORT_MANIFEST_SESSION_MISMATCH",
                "Current Vivado session generation does not match the report collection snapshot.",
                data={
                    "manifest_path": manifest.get("manifest_path", ""),
                    "recorded_session_generation_id": recorded_generation,
                    "current_session_generation_id": current_generation,
                },
            )


def _progress_is_complete(value: Any) -> bool:
    text = str(value or "").strip().rstrip("%")
    try:
        return float(text) >= 100.0
    except ValueError:
        return False


def _resolve_report_manifest_dir(report_dir_value: Any, manifest_path: Path) -> Path:
    if report_dir_value:
        raw = Path(str(report_dir_value))
        return raw.resolve() if raw.is_absolute() else (manifest_path.parent / raw).resolve()
    return manifest_path.parent.resolve()


def _validated_report_manifest_entry(
    item: dict[str, Any],
    *,
    index: int,
    manifest_path: Path,
    report_dir: Path,
    project_dir: Path,
    run_name: str,
) -> dict[str, Any]:
    path_text = str(item.get("path", "")).strip()
    if not path_text:
        raise _report_manifest_error(
            "REPORT_MANIFEST_INVALID",
            "Report manifest entry is missing path.",
            manifest_path=manifest_path,
            project_dir=project_dir,
            run_name=run_name,
            extra={"report_dir": str(report_dir), "report_index": index},
        )
    raw_path = Path(path_text)
    report_path = raw_path.resolve() if raw_path.is_absolute() else (report_dir / raw_path).resolve()
    _assert_path_inside(report_path, report_dir, "Report manifest entry path must resolve inside the manifest report_dir", run_name=run_name)
    if not report_path.is_file():
        raise _report_manifest_error(
            "REPORT_FILE_NOT_FOUND",
            f"Report file listed in manifest was not found: {report_path}",
            manifest_path=manifest_path,
            project_dir=project_dir,
            run_name=run_name,
            extra={"report_dir": str(report_dir), "report_path": str(report_path), "report_index": index},
        )

    try:
        report_content = read_stable_bytes(report_path, root=report_dir, max_bytes=MAX_REPORT_FILE_BYTES)
    except (OSError, ManagedPathError) as exc:
        raise _report_manifest_error(
            "REPORT_FILE_INTEGRITY_MISMATCH",
            f"Report file could not be read as a bounded stable snapshot: {exc}",
            manifest_path=manifest_path,
            project_dir=project_dir,
            run_name=run_name,
            extra={"report_dir": str(report_dir), "report_path": str(report_path), "report_index": index},
        ) from exc
    expected_size = _int_or_none(item.get("size"))
    actual_size = len(report_content)
    if expected_size is None or expected_size != actual_size:
        raise _report_manifest_error(
            "REPORT_FILE_INTEGRITY_MISMATCH",
            "Report file size does not match report manifest.",
            manifest_path=manifest_path,
            project_dir=project_dir,
            run_name=run_name,
            extra={"report_dir": str(report_dir), "report_path": str(report_path), "expected_size": expected_size, "actual_size": actual_size},
        )

    expected_hash = str(item.get("sha256", "")).strip().lower()
    if not _is_sha256_text(expected_hash):
        raise _report_manifest_error(
            "REPORT_FILE_INTEGRITY_MISSING",
            "Report manifest entry is missing a valid SHA256 hash.",
            manifest_path=manifest_path,
            project_dir=project_dir,
            run_name=run_name,
            extra={"report_dir": str(report_dir), "report_path": str(report_path), "report_index": index},
        )
    actual_hash = hashlib.sha256(report_content).hexdigest()
    if expected_hash != actual_hash:
        raise _report_manifest_error(
            "REPORT_FILE_INTEGRITY_MISMATCH",
            "Report file SHA256 does not match report manifest.",
            manifest_path=manifest_path,
            project_dir=project_dir,
            run_name=run_name,
            extra={"report_dir": str(report_dir), "report_path": str(report_path), "expected_sha256": expected_hash, "actual_sha256": actual_hash},
        )

    validated = dict(item)
    validated["path"] = str(report_path)
    validated["size"] = actual_size
    validated["sha256"] = actual_hash
    return validated


def _assert_path_inside(path: Path, root: Path, message: str, *, run_name: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ReportManifestValidationError(
            "REPORT_MANIFEST_UNTRUSTED",
            message,
            data={"path": str(path.resolve()), "allowed_root": str(root.resolve()), "run_name": run_name},
        ) from exc


def _report_manifest_error(
    error_code: str,
    message: str,
    *,
    manifest_path: Path,
    project_dir: Path,
    run_name: str,
    extra: dict[str, Any] | None = None,
) -> ReportManifestValidationError:
    data = {"manifest_path": str(manifest_path), "project_dir": str(project_dir), "run_name": run_name}
    if extra:
        data.update(extra)
    return ReportManifestValidationError(error_code, message, data=data)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_sha256_text(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value.strip().lower()))


def _load_reusable_audit_from_diagnostic_manifest(
    manifest_path: Any,
    project_dir: str | Path,
    run_name: str,
    *,
    current_run_configurations: dict[str, Any],
    current_design_execution_identity: dict[str, Any],
) -> dict[str, Any]:
    project_path = Path(project_dir).resolve()
    diagnostics_root = project_path / "vmcp_diagnostics"
    path = Path(str(manifest_path)).resolve()
    if path.is_dir():
        path = path / "diagnostic_manifest.json"
    try:
        path.relative_to(diagnostics_root)
    except ValueError as exc:
        raise ValueError("reuse_audit_from_manifest must point inside the current project's vmcp_diagnostics directory") from exc
    try:
        manifest, _ = load_json_evidence(
            path,
            root=diagnostics_root,
            max_bytes=MAX_DIAGNOSTIC_MANIFEST_BYTES,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"diagnostic manifest not found: {path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("diagnostic manifest must be a JSON object")
    manifest.setdefault("manifest_path", str(path))
    files = manifest.get("files", [])
    if not isinstance(files, list):
        raise ValueError("diagnostic manifest files must be a list")
    manifest_hardware = manifest.get("hardware_validation") if isinstance(manifest.get("hardware_validation"), dict) else {}
    if manifest_hardware.get("status") != "NOT_VALIDATED" or manifest_hardware.get("validated") is not False:
        raise ValueError("diagnostic manifest must preserve hardware_validation.status=NOT_VALIDATED and validated=false")
    audit_entries = [entry for entry in files if isinstance(entry, dict) and entry.get("category") == "audit"]
    if len(audit_entries) != 1:
        raise ValueError(f"diagnostic manifest must contain exactly one audit entry, found {len(audit_entries)}")
    audit_entry = audit_entries[0]
    audit_path = _resolve_diagnostic_entry_path(audit_entry, path.parent, diagnostics_root)
    if not audit_path.exists():
        raise FileNotFoundError(f"reusable audit result not found: {audit_path}")
    expected_size = audit_entry.get("size")
    expected_hash = str(audit_entry.get("sha256", "")).strip().lower()
    if not isinstance(expected_size, int) or expected_size < 0:
        raise ValueError("diagnostic manifest audit entry is missing a valid size")
    if not _is_sha256_text(expected_hash):
        raise ValueError("diagnostic manifest audit entry is missing a valid sha256")
    audit, audit_snapshot = load_json_evidence(
        audit_path,
        root=diagnostics_root,
        max_bytes=MAX_REUSABLE_AUDIT_BYTES,
    )
    if audit_snapshot.size != expected_size or audit_snapshot.sha256 != expected_hash:
        raise ValueError("reusable audit result does not match diagnostic manifest size/sha256")
    if not isinstance(audit, dict):
        raise ValueError("reusable audit result must be a JSON object")
    hardware_validation = audit.get("hardware_validation") if isinstance(audit.get("hardware_validation"), dict) else {}
    if hardware_validation.get("status") != "NOT_VALIDATED" or hardware_validation.get("validated") is not False:
        raise ValueError("reusable audit must preserve hardware_validation.status=NOT_VALIDATED and validated=false")
    freshness = audit.get("evidence_freshness") if isinstance(audit.get("evidence_freshness"), dict) else {}
    if str(freshness.get("status", "")).upper() != "FRESH":
        raise ValueError("reusable audit evidence_freshness.status must be FRESH")
    checked_at_text = str(freshness.get("checked_at", "")).strip()
    if not checked_at_text:
        raise ValueError("reusable audit evidence_freshness.checked_at is required")
    checked_at = _parse_reusable_audit_checked_at(checked_at_text)
    age_seconds = (datetime.now(UTC) - checked_at).total_seconds()
    if age_seconds < -300:
        raise ValueError("reusable audit evidence_freshness.checked_at is unexpectedly in the future")
    if age_seconds > REUSABLE_AUDIT_MAX_AGE_HOURS * 3600:
        raise ValueError(
            f"reusable audit is older than {REUSABLE_AUDIT_MAX_AGE_HOURS} hours and must be refreshed"
        )
    project_summary = audit.get("health_summary") if isinstance(audit.get("health_summary"), dict) else {}
    audit_project_dir = str(project_summary.get("project_dir", "")).strip()
    if not audit_project_dir or Path(audit_project_dir).resolve() != project_path:
        raise ValueError("reusable audit project identity does not match the current project")
    run_states = freshness.get("run_states") if isinstance(freshness.get("run_states"), list) else []
    target_states = [
        item
        for item in run_states
        if isinstance(item, dict) and str(item.get("run_name", "")) == run_name
    ]
    if len(target_states) != 1:
        raise ValueError(f"reusable audit freshness must contain exactly one run state for {run_name}, found {len(target_states)}")
    target_state = target_states[0]
    if _truthy_text(target_state.get("needs_refresh")):
        raise ValueError(f"reusable audit run {run_name} still needs refresh")
    if _classify_run_state(str(target_state.get("status", ""))) != "complete":
        raise ValueError(f"reusable audit run {run_name} is not complete")
    current_state = _current_run_state_for_reuse(current_run_configurations, run_name)
    if _truthy_text(current_state.get("needs_refresh")):
        raise ValueError(f"current Vivado run {run_name} needs refresh; reusable audit is stale")
    if _classify_run_state(str(current_state.get("status", ""))) != "complete":
        raise ValueError(f"current Vivado run {run_name} is not complete; reusable audit is stale")
    if _classify_run_state(str(target_state.get("status", ""))) != _classify_run_state(str(current_state.get("status", ""))):
        raise ValueError(f"current Vivado run {run_name} state does not match reusable audit")
    audit_inputs = audit.get("inputs") if isinstance(audit.get("inputs"), dict) else {}
    recorded_run_configurations = audit_inputs.get("run_configurations")
    if not isinstance(recorded_run_configurations, dict):
        raise ValueError("reusable audit is missing recorded run configuration identity")
    recorded_current_state = _current_run_state_for_reuse(recorded_run_configurations, run_name)
    if _normalized_path_text(recorded_current_state.get("directory")) != _normalized_path_text(current_state.get("directory")):
        raise ValueError(f"current Vivado run {run_name} directory does not match reusable audit")
    _validate_reusable_audit_manifest_identities(
        audit,
        freshness=freshness,
        project_path=project_path,
        run_name=run_name,
        current_design_execution_identity=current_design_execution_identity,
    )
    audit["reused_from_manifest"] = str(path)
    audit["reused_current_run_state"] = current_state
    return audit


def _parse_reusable_audit_checked_at(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("reusable audit evidence_freshness.checked_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("reusable audit evidence_freshness.checked_at must include a timezone")
    return parsed.astimezone(UTC)


def _current_run_state_for_reuse(run_configurations: dict[str, Any], run_name: str) -> dict[str, Any]:
    raw_runs = run_configurations.get("runs", run_configurations) if isinstance(run_configurations, dict) else {}
    entry = raw_runs.get(run_name) if isinstance(raw_runs, dict) else None
    run = entry.get("run", entry) if isinstance(entry, dict) else None
    if not isinstance(run, dict):
        raise ValueError(f"current Vivado run configuration for {run_name} is unavailable")
    return {
        "run_name": run_name,
        "status": str(run.get("status", run.get("STATUS", ""))),
        "needs_refresh": _truthy_text(run.get("needs_refresh", run.get("NEEDS_REFRESH", ""))),
        "directory": str(run.get("directory", run.get("DIRECTORY", ""))),
    }


def _validate_reusable_audit_manifest_identities(
    audit: dict[str, Any],
    *,
    freshness: dict[str, Any],
    project_path: Path,
    run_name: str,
    current_design_execution_identity: dict[str, Any],
) -> None:
    inputs = audit.get("inputs") if isinstance(audit.get("inputs"), dict) else {}
    for label, root_name in (("artifact", "vmcp_artifacts"), ("report", "vmcp_reports")):
        recorded = inputs.get(f"{label}_manifest")
        if not isinstance(recorded, dict) or not recorded:
            raise ValueError(f"reusable audit is missing recorded {label} manifest identity")
        manifest_text = str(freshness.get(f"{label}_manifest_path", "")).strip()
        if not manifest_text:
            raise ValueError(f"reusable audit is missing {label}_manifest_path freshness identity")
        manifest_path = Path(manifest_text).resolve()
        allowed_root = (project_path / root_name).resolve()
        try:
            manifest_path.relative_to(allowed_root)
        except ValueError as exc:
            raise ValueError(f"reusable audit {label} manifest must remain inside {allowed_root}") from exc
        try:
            current, _ = load_json_evidence(
                manifest_path,
                root=allowed_root,
                max_bytes=MAX_REPORT_MANIFEST_BYTES,
            )
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"reusable audit {label} manifest no longer exists or is invalid: {manifest_path}") from exc
        if not isinstance(current, dict):
            raise ValueError(f"reusable audit {label} manifest must be a JSON object: {manifest_path}")
        current.setdefault("manifest_path", str(manifest_path))
        if label == "report":
            current = _validated_report_manifest(
                current,
                manifest_path=manifest_path,
                project_dir=project_path,
                run_name=run_name,
                current_design_execution_identity=current_design_execution_identity,
            )
        else:
            current = _validated_artifact_manifest_for_audit_reuse(
                current,
                manifest_path=manifest_path,
                project_path=project_path,
                run_name=run_name,
                current_design_execution_identity=current_design_execution_identity,
            )
        _require_reuse_manifest_fresh(current, label=label, run_name=run_name)
        if _reuse_manifest_identity(recorded, label=label) != _reuse_manifest_identity(current, label=label):
            raise ValueError(f"current {label} manifest identity no longer matches the reusable audit")


def _validated_artifact_manifest_for_audit_reuse(
    manifest: dict[str, Any],
    *,
    manifest_path: Path,
    project_path: Path,
    run_name: str,
    current_design_execution_identity: dict[str, Any],
) -> dict[str, Any]:
    if manifest.get("schema_version") != 4:
        raise ValueError("reusable audit artifact manifest schema_version=4 is required")
    if str(manifest.get("run_name", "")).strip() != run_name:
        raise ValueError(f"artifact manifest run_name does not match requested run {run_name}")
    recorded_design_identity = manifest.get("design_execution_identity")
    if (
        not isinstance(recorded_design_identity, dict)
        or recorded_design_identity.get("status") != "READY"
        or str(manifest.get("design_execution_identity_sha256", ""))
        != str(recorded_design_identity.get("sha256", ""))
    ):
        raise ValueError("reusable audit artifact manifest design execution identity is missing or invalid")
    if recorded_design_identity != current_design_execution_identity:
        raise ValueError("SOURCE_CLOSURE_CHANGED: reusable audit artifact source closure no longer matches the current project")
    declared_path = Path(str(manifest.get("manifest_path", ""))).resolve()
    if declared_path != manifest_path:
        raise ValueError("artifact manifest path identity does not match the current manifest file")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("reusable audit artifact manifest must contain at least one artifact")
    allowed_root = (project_path / "vmcp_artifacts").resolve()
    for index, item in enumerate(artifacts):
        if not isinstance(item, dict):
            raise ValueError(f"artifact manifest entry {index} must be an object")
        artifact_path = Path(str(item.get("export_path", item.get("path", "")))).resolve()
        try:
            artifact_path.relative_to(allowed_root)
        except ValueError as exc:
            raise ValueError(f"artifact manifest entry {index} resolves outside {allowed_root}") from exc
        if not artifact_path.is_file():
            raise ValueError(f"artifact manifest entry {index} no longer exists: {artifact_path}")
        expected_size = _int_or_none(item.get("size"))
        expected_hash = str(item.get("sha256", "")).strip().lower()
        if expected_size != artifact_path.stat().st_size or not _is_sha256_text(expected_hash):
            raise ValueError(f"artifact manifest entry {index} has invalid size or SHA256 evidence")
        if sha256_file(artifact_path).lower() != expected_hash:
            raise ValueError(f"artifact manifest entry {index} SHA256 no longer matches")
        source_text = str(item.get("source_path", "")).strip()
        if not source_text:
            raise ValueError(f"artifact manifest entry {index} is missing source_path")
        source_path = Path(source_text).resolve()
        try:
            source_path.relative_to(project_path.resolve())
        except ValueError as exc:
            raise ValueError(f"artifact manifest source entry {index} resolves outside the current project") from exc
        if not source_path.is_file():
            raise ValueError(f"artifact manifest source entry {index} no longer exists: {source_path}")
        if source_path.stat().st_size != expected_size or sha256_file(source_path).lower() != expected_hash:
            raise ValueError(f"artifact manifest source entry {index} no longer matches the captured artifact")
    return manifest


def _require_reuse_manifest_fresh(manifest: dict[str, Any], *, label: str, run_name: str) -> None:
    if str(manifest.get("run_name", "")).strip() != run_name:
        raise ValueError(f"{label} manifest run_name does not match requested run {run_name}")
    freshness = manifest.get("evidence_freshness") if isinstance(manifest.get("evidence_freshness"), dict) else {}
    if str(freshness.get("status", "")).upper() != "FRESH" or _truthy_text(freshness.get("needs_refresh")):
        raise ValueError(f"current {label} manifest evidence is not FRESH")
    if str(freshness.get("run_name", "")).strip() != run_name:
        raise ValueError(f"current {label} manifest freshness run_name does not match {run_name}")
    if not str(freshness.get("collected_at", "")).strip():
        raise ValueError(f"current {label} manifest freshness collected_at is required")


def _reuse_manifest_identity(manifest: dict[str, Any], *, label: str) -> dict[str, Any]:
    freshness = manifest.get("evidence_freshness") if isinstance(manifest.get("evidence_freshness"), dict) else {}
    entries_key = "artifacts" if label == "artifact" else "reports"
    entries = manifest.get(entries_key) if isinstance(manifest.get(entries_key), list) else []
    normalized_entries = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        normalized_entries.append(
            {
                "category": str(item.get("category", "")),
                "path": str(Path(str(item.get("export_path", item.get("path", "")))).resolve()),
                "size": _int_or_none(item.get("size")),
                "sha256": str(item.get("sha256", "")).strip().lower(),
            }
        )
    return {
        "run_name": str(manifest.get("run_name", "")).strip(),
        "manifest_path": str(Path(str(manifest.get("manifest_path", ""))).resolve()),
        "freshness": {
            "status": str(freshness.get("status", "")).upper(),
            "run_name": str(freshness.get("run_name", "")).strip(),
            "collected_at": str(freshness.get("collected_at", "")).strip(),
            "needs_refresh": _truthy_text(freshness.get("needs_refresh")),
        },
        "entries": sorted(normalized_entries, key=lambda item: (item["category"], item["path"])),
    }


def _normalized_path_text(value: Any) -> str:
    text = str(value or "").strip()
    return os.path.normcase(os.path.normpath(text)) if text else ""


def _resolve_diagnostic_entry_path(entry: dict[str, Any], bundle_root: Path, diagnostics_root: Path) -> Path:
    raw_text = str(entry.get("path", "")).strip()
    if not raw_text:
        raise ValueError("diagnostic manifest audit entry is missing path")
    raw = Path(raw_text)
    candidate = raw.resolve() if raw.is_absolute() else (bundle_root / raw).resolve()
    try:
        candidate.relative_to(diagnostics_root)
    except ValueError as exc:
        raise ValueError("diagnostic manifest audit entry must resolve inside vmcp_diagnostics") from exc
    return candidate


def _resolve_artifact_manifest_path(project_dir: str | Path, run_name: str, preferred_path: str | Path | None = None) -> Path:
    preferred = Path(preferred_path).resolve() if preferred_path else None
    if preferred is not None and preferred.exists():
        if preferred.is_dir():
            directory_manifest = preferred / "manifest.json"
            if directory_manifest.exists():
                return directory_manifest
        return preferred
    project_path = Path(project_dir).resolve() if str(project_dir) else Path()
    candidates = [
        project_path / "vmcp_artifacts" / run_name / "manifest.json",
        project_path / "vmcp_artifacts" / "manifest.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return preferred or candidates[0]


def _signoff_inputs_from_report_manifest(report_manifest: dict[str, Any]) -> dict[str, Any]:
    reports = report_manifest.get("reports", []) if isinstance(report_manifest.get("reports"), list) else []
    by_category: dict[str, dict[str, Any]] = {}
    for item in reports:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip()
        if category and item.get("path"):
            by_category[category] = item

    report_root = Path(str(report_manifest.get("report_dir", "")))

    timing = _parse_report_file(
        by_category,
        report_root,
        "timing",
        parse_timing_summary,
        _missing_report_input("timing"),
        report_envelope=("timing_summary", TIMING_SUMMARY_REPORT_BEGIN_MARKER),
        report_attestation=report_manifest,
    )
    drc = _parse_report_file(
        by_category,
        report_root,
        "drc",
        parse_drc_report,
        _missing_drc_report_input(),
        report_envelope=("drc", DRC_REPORT_BEGIN_MARKER),
        report_attestation=report_manifest,
    )
    methodology = _parse_report_file(
        by_category,
        report_root,
        "methodology",
        parse_methodology_report,
        _missing_report_input("methodology"),
        report_envelope=("methodology", METHODOLOGY_REPORT_BEGIN_MARKER),
        report_attestation=report_manifest,
    )
    cdc = _parse_report_file(by_category, report_root, "cdc", parse_cdc_report, _missing_crossing_report_input("cdc"))
    clock_interaction = _parse_report_file(
        by_category,
        report_root,
        "clock_interaction",
        parse_clock_interaction_report,
        _missing_crossing_report_input("clock_interaction"),
    )
    power = _parse_report_file(by_category, report_root, "power", parse_power_report, _missing_report_input("power"))
    messages = _parse_report_file(by_category, report_root, "messages", parse_messages, {"ok": True, "counts": {}, "messages": []})
    return {
        "timing": _with_report_source(timing, report_manifest, "timing"),
        "drc": _with_report_source(drc, report_manifest, "drc"),
        "methodology": _with_report_source(methodology, report_manifest, "methodology"),
        "cdc": _with_report_source(cdc, report_manifest, "cdc"),
        "clock_interaction": _with_report_source(clock_interaction, report_manifest, "clock_interaction"),
        "power": _with_report_source(power, report_manifest, "power"),
        "critical_warnings": _with_report_source(_critical_messages(messages), report_manifest, "messages"),
    }


def _parse_report_file(
    by_category: dict[str, dict[str, Any]],
    report_root: Path,
    category: str,
    parser: Callable[[str], dict[str, Any]],
    missing_value: dict[str, Any],
    report_envelope: tuple[str, str] | None = None,
    report_attestation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = by_category.get(category)
    if entry is None:
        return dict(missing_value)
    path = Path(str(entry.get("path", "")))
    try:
        content = read_stable_bytes(path, root=report_root, max_bytes=MAX_REPORT_FILE_BYTES)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if len(content) != _int_or_none(entry.get("size")) or actual_sha256 != str(entry.get("sha256", "")).lower():
            raise ManagedPathError(f"{category} report changed after manifest validation")
        raw = content.decode("utf-8", errors="replace")
    except (OSError, ManagedPathError) as exc:
        data = dict(missing_value)
        data.update({"status": "BLOCK", "available": False, "message": f"Could not validate {category} report bytes: {exc}"})
        return data
    if report_envelope:
        attestation = report_attestation or {}
        report_type = report_envelope[0]
        report_commands = {
            "timing_summary": "report_timing_summary",
            "drc": "report_drc",
            "methodology": "report_methodology",
        }
        parser_input = attest_report_text(
            *report_envelope,
            raw,
            vivado_version_short=str(attestation.get("vivado_version_short", "")),
            vivado_build=str(attestation.get("vivado_build", "")),
            report_command=report_commands.get(report_type, report_type),
        )
    else:
        parser_input = raw
    data = parser(parser_input)
    data["raw"] = raw
    data["raw_excerpt"] = raw[-4096:]
    data["report_path"] = str(path)
    return data


def _missing_report_input(category: str) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "WARN",
        "available": False,
        "message": f"{category} report is missing from existing report bundle.",
        "counts": {},
        "messages": {"ok": True, "counts": {}, "messages": []},
    }


def _missing_drc_report_input() -> dict[str, Any]:
    return {
        "ok": True,
        "status": "WARN",
        "available": False,
        "error_count": 0,
        "critical_warning_count": 0,
        "warning_count": 0,
        "violations": [],
        "message": "DRC report is missing from existing report bundle.",
    }


def _missing_crossing_report_input(category: str) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "WARN",
        "available": False,
        "report": category,
        "counts": {"unsafe": 0, "unknown": 0, "safe": 0},
        "messages": {"ok": True, "counts": {}, "messages": []},
        "message": f"{category} report is missing from existing report bundle.",
    }


def _critical_messages(messages: dict[str, Any]) -> dict[str, Any]:
    parsed_messages = messages if isinstance(messages.get("counts"), dict) else parse_messages(str(messages.get("raw", "")))
    return _filter_messages(parsed_messages, severities={"ERROR", "CRITICAL WARNING"})


def _with_report_source(data: dict[str, Any], report_manifest: dict[str, Any], category: str) -> dict[str, Any]:
    result = dict(data)
    result["source"] = "report_bundle"
    result["report_category"] = category
    result["report_manifest_path"] = str(report_manifest.get("manifest_path", ""))
    return result


def _resolve_manifest_path_argument(manifest_path: Any) -> Path:
    resolved = Path(str(manifest_path)).resolve()
    if resolved.exists() and resolved.is_dir():
        return resolved / "manifest.json"
    return resolved


def _diagnostic_progress_context(args: dict[str, Any], *, run_name: str, timeout_s: int) -> dict[str, Any]:
    return {
        "status": "BLOCK",
        "current_step": "not_started",
        "last_successful_artifact": "",
        "project_dir": "",
        "partial_output_dir": "",
        "run_name": run_name,
        "timeout_s_used": timeout_s,
        "retry_suggestion": "Retry collect_diagnostic_bundle after fixing the failed step, or increase timeout_s if the failed step was still making progress.",
        "next_actions": [
            next_action(
                "collect_diagnostic_bundle",
                "Retry diagnostic bundle collection with the same run_name after the failed step is fixed or with a larger timeout_s.",
                required_args=["run_name"],
                arg_sources={"run_name": "failed collect_diagnostic_bundle.data.run_name"},
                preconditions=["The Vivado project is open and the failed step has been addressed."],
                stop_condition="collect_diagnostic_bundle returns ok=true with manifest_path.",
            ),
            next_action(
                "collect_diagnostic_bundle",
                "Rebuild the diagnostic bundle by reusing a previously fresh audit manifest when only handoff packaging needs recovery.",
                required_args=["run_name", "reuse_audit_from_manifest"],
                arg_sources={
                    "run_name": "failed collect_diagnostic_bundle.data.run_name",
                    "reuse_audit_from_manifest": "previous collect_diagnostic_bundle.data.manifest_path",
                },
                preconditions=["A previous diagnostic_manifest.json exists for the same open project and its audit evidence is FRESH."],
                stop_condition="collect_diagnostic_bundle returns ok=true with a new manifest_path.",
                optional=True,
            ),
        ],
    }


def _workflow_trace_storage_data(tracer: WorkflowTracer) -> dict[str, Any]:
    project_trace_path = str(tracer.project_trace_path) if tracer.project_trace_path is not None else ""
    return {
        "global_trace_path": str(tracer.trace_path),
        "global_trace_scope": ".vivado_agent_mcp/traces append-only MCP transcript",
        "project_trace_path": project_trace_path,
        "project_trace_scope": "<project_dir>/vmcp_diagnostics/workflow_trace.jsonl handoff copy",
        "project_trace_is_handoff_copy": bool(project_trace_path),
        "note": (
            "The MCP global trace is runtime evidence. Once a project directory is known, "
            "the same append-only entries are mirrored into the project diagnostic bundle area for handoff."
        ),
    }


def _partial_audit_result_from_failure(audit_result: dict[str, Any], *, project_state: dict[str, Any]) -> dict[str, Any]:
    data = audit_result.get("data") if isinstance(audit_result.get("data"), dict) else {}
    project = project_state.get("project", {}) if isinstance(project_state, dict) else {}
    message = str(audit_result.get("message") or audit_result.get("summary") or "Project audit did not complete.")
    finding = {
        "severity": "BLOCK",
        "code": str(audit_result.get("error_code") or "AUDIT_INCOMPLETE"),
        "source_tool": "run_project_audit",
        "message": message,
        "detail": {
            "current_step": data.get("current_step", ""),
            "completed_steps": data.get("completed_steps", []),
            "partial_success": bool(data.get("partial_success", False)),
        },
    }
    return {
        "ok": False,
        "status": "BLOCK",
        "effective_status": "BLOCK",
        "validation_scope": "pre_hardware_software",
        "ready_meaning": "This partial diagnostic bundle is for recovery only; it is not a complete handoff and does not represent real FPGA board validation.",
        "hardware_validation": hardware_validation_boundary(),
        "evidence_freshness": {
            "status": "STALE",
            "run_name": str(data.get("request_context", {}).get("run_name", "")),
            "needs_refresh": True,
            "collected_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "source": "partial_collect_diagnostic_bundle",
            "reason": message,
        },
        "health_summary": {
            "project_name": project.get("name", ""),
            "project_dir": project.get("directory", ""),
            "part": project.get("part", ""),
            "top": project.get("top", ""),
            "active_block_count": 1,
            "active_warning_count": 0,
            "waived_count": 0,
        },
        "active_findings": [finding],
        "blocking_items": [finding],
        "warnings": [],
        "raw_findings": [finding],
        "waived_findings": [],
        "waiver_summary": {"waived_finding_count": 0, "active_finding_count": 1, "requires_handoff_archive": False},
        "next_steps": ["Resume the project, rerun run_project_audit, then rebuild collect_diagnostic_bundle for a complete handoff."],
        "next_actions": [
            next_action(
                "run_project_audit",
                "Refresh the incomplete audit after recovering the Vivado session.",
                required_args=["run_name"],
                arg_sources={"run_name": "partial audit run_name or workflow run_name"},
                preconditions=["Vivado session is running and the project is open."],
                stop_condition="run_project_audit returns READY, WARN, or BLOCK with complete findings.",
            ),
            next_action(
                "collect_diagnostic_bundle",
                "Rebuild the diagnostic bundle after audit recovery.",
                required_args=["run_name"],
                arg_sources={"run_name": "workflow run_name"},
                preconditions=["run_project_audit completed."],
                stop_condition="collect_diagnostic_bundle returns a complete diagnostic_manifest.json.",
            ),
        ],
        "inputs": {"failed_result": audit_result},
    }


def _planned_diagnostic_bundle_dir(project_dir: str | Path, args: dict[str, Any]) -> str:
    if args.get("output_dir"):
        return str(Path(str(args["output_dir"])).resolve())
    timestamp = str(args.get("timestamp") or "<timestamp>")
    return str(Path(project_dir).resolve() / "vmcp_diagnostics" / timestamp)


def _diagnostic_collection_failure(error_code: str, message: str, context: dict[str, Any], raw_excerpt: str = "") -> dict[str, Any]:
    payload = dict(context)
    payload["error_code"] = error_code
    payload["failed_step"] = payload.get("current_step", "")
    return failure("collect_diagnostic_bundle", error_code, message, raw_excerpt, data=payload)


def _report_manifest_validation_failure(tool: str, exc: ReportManifestValidationError) -> dict[str, Any]:
    data = dict(exc.data)
    run_name = str(data.get("run_name") or "impl_1")
    data["next_steps"] = [
        "Run collect_report_bundle for the current project/run to regenerate a trusted project-local report manifest.",
        "Only pass a report_manifest.json whose run_name, report_dir, report file size, and SHA256 match the current project evidence.",
    ]
    data["next_actions"] = [
        next_action(
            "collect_report_bundle",
            "Regenerate trusted report evidence before using the signoff/audit report manifest fast path.",
            required_args=["run_name"],
            arg_sources={"run_name": run_name},
            preconditions=["Vivado session is running and implementation report evidence is available."],
            stop_condition="collect_report_bundle returns ok=true with data.manifest_path inside the current project.",
        )
    ]
    return failure(tool, exc.error_code, str(exc), data=data)


def _simulation_run_all_vcd_blocked(
    *,
    simset: str,
    export_vcd: bool,
    preflight: dict[str, Any],
    vcd_risk: bool,
) -> dict[str, Any]:
    data = {
        "status": "BLOCK",
        "simset": simset,
        "vcd_risk": vcd_risk,
        "waveform_risk": True,
        "export_vcd": export_vcd,
        "export_vcd_requested": export_vcd,
        "mcp_vcd_export_mode": "testbench_existing" if preflight.get("testbench_vcd_usage") else "mcp_open_vcd" if export_vcd else "disabled",
        "testbench_vcd_usage": bool(preflight.get("testbench_vcd_usage", False)),
        "testbench_vcd_sources": preflight.get("testbench_vcd_sources", []),
        "preflight_testbench_vcd_usage": bool(preflight.get("testbench_vcd_usage", False)),
        "preflight_testbench_vcd_sources": preflight.get("testbench_vcd_sources", []),
        "preflight": preflight,
        "next_actions": [_simulation_retry_action()],
    }
    return failure(
        "run_behavioral_simulation",
        "SIMULATION_RUN_ALL_VCD_BLOCKED",
        "run_all with bounded waveform storage is blocked because VCD or WDB growth cannot be controlled; use a finite run_time or explicitly disable the limit with max_vcd_mb=0.",
        data=data,
    )


def _simulation_uncontrolled_waveform_failure(
    *,
    simset: str,
    export_vcd: bool,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    analysis = preflight.get("waveform_path_analysis", {})
    return failure(
        "run_behavioral_simulation",
        "SIMULATION_WAVEFORM_PATH_UNCONTROLLED",
        "Simulation host filesystem effects cannot be bounded safely from the reviewed source closure.",
        data={
            "status": "BLOCK",
            "simset": simset,
            "export_vcd_requested": export_vcd,
            "preflight": preflight,
            "uncontrolled_reasons": analysis.get("uncontrolled_reasons", []),
            "next_actions": [
                next_action(
                    "run_behavioral_simulation",
                    "Use literal controlled output paths, keep literal host inputs inside a configured trusted root, and remove unsupported macro or language constructs.",
                    required_args=["simset", "max_vcd_mb"],
                    arg_sources={
                        "simset": simset,
                        "max_vcd_mb": "retain a positive limit after moving the dump path inside the project; use 0 only for an explicitly accepted unbounded run",
                    },
                    preconditions=["Every simulation host input/output path and source construct is deterministic and reviewed."],
                    stop_condition="run_behavioral_simulation preflight reports no uncontrolled host filesystem effects.",
                )
            ],
        },
    )


def _simulation_preflight_state(preflight: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(preflight)
    waveform_paths = analyze_testbench_waveform_paths(prepared)
    prepared["waveform_path_analysis"] = waveform_paths
    prepared["host_input_files"] = [
        *list(waveform_paths.get("host_input_files", [])),
        *([str(prepared.get("project_path"))] if prepared.get("project_path") else []),
    ]
    source_identity = build_simulation_source_identity(prepared)
    return {
        "preflight": prepared,
        "testbench_vcd_usage": bool(prepared.get("testbench_vcd_usage", False)),
        "waveform_paths": waveform_paths,
        "execution_effects": _simulation_execution_effects_snapshot(waveform_paths),
        "source_identity": source_identity,
        "stable_input_identity": build_simulation_stable_input_identity(prepared, source_identity),
    }


def _simulation_execution_effects_snapshot(analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(analysis.get("status", "")),
        "monitored_paths": sorted({str(item) for item in analysis.get("monitored_paths", []) if str(item)}),
        "host_input_files": sorted({str(item) for item in analysis.get("host_input_files", []) if str(item)}),
        "scanned_source_paths": sorted({str(item) for item in analysis.get("scanned_source_paths", []) if str(item)}),
        "include_files": sorted({str(item) for item in analysis.get("include_files", []) if str(item)}),
        "uncontrolled_reasons": sorted({str(item) for item in analysis.get("uncontrolled_reasons", []) if str(item)}),
    }


def _simulation_execution_effects_delta(before: dict[str, Any], locked: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for key in ("monitored_paths", "host_input_files", "scanned_source_paths", "include_files", "uncontrolled_reasons"):
        before_values = {str(item) for item in before.get(key, []) if str(item)}
        locked_values = {str(item) for item in locked.get(key, []) if str(item)}
        if before_values != locked_values:
            delta[key] = {
                "added": sorted(locked_values - before_values),
                "removed": sorted(before_values - locked_values),
                "before_count": len(before_values),
                "locked_count": len(locked_values),
            }
    if str(before.get("status", "")) != str(locked.get("status", "")):
        delta["status"] = {
            "before": str(before.get("status", "")),
            "locked": str(locked.get("status", "")),
        }
    return {"changed_fields": sorted(delta), "changes": delta}


def _simulation_retry_action() -> dict[str, Any]:
    return next_action(
        "run_behavioral_simulation",
        "Retry simulation with a finite run_time, smaller VCD scope, disabled export_vcd, or a reviewed max_vcd_mb.",
        required_args=["simset"],
        arg_sources={"simset": "failed run_behavioral_simulation.data.simset"},
        preconditions=["The testbench has a finite timeout and VCD generation risk has been reviewed."],
        stop_condition="run_behavioral_simulation returns completed without vcd_limit_exceeded.",
    )


def _simulation_failure_repair_actions(*, simset: str) -> list[dict[str, Any]]:
    return [
        next_action(
            "get_simulation_result",
            "Read the latest simulation log and waveform artifact summary before editing RTL or testbench code.",
            required_args=["simset"],
            arg_sources={"simset": simset},
            preconditions=["A behavioral simulation invocation returned failed or unknown status."],
            stop_condition="get_simulation_result returns status, log paths, artifact summary, and simulation_diagnosis.",
        ),
        next_action(
            "analyze_sources",
            "Check simulation fileset diagnostics first because the failing condition may live in the testbench or sim-only sources.",
            required_args=["fileset"],
            arg_sources={"fileset": simset},
            preconditions=["Vivado project is open and simulation files are loaded."],
            stop_condition="analyze_sources returns actionable simulation fileset diagnostics or confirms the simset is clean.",
            optional=True,
        ),
        next_action(
            "check_syntax",
            "Run Vivado native syntax checks on the simulation fileset before rerunning the failing testbench.",
            required_args=["fileset"],
            arg_sources={"fileset": simset},
            preconditions=["Vivado project is open and simulation fileset exists."],
            stop_condition="check_syntax returns ok=true or structured simset syntax findings.",
            optional=True,
        ),
        next_action(
            "analyze_sources",
            "Check RTL source diagnostics when simulation failure may be caused by DUT HDL or stale compile order.",
            required_args=["fileset"],
            arg_sources={"fileset": "sources_1"},
            preconditions=["Vivado project is open and source files are loaded."],
            stop_condition="analyze_sources returns actionable source diagnostics or confirms source analysis is clean.",
            optional=True,
        ),
        next_action(
            "check_syntax",
            "Run Vivado native syntax checks before rerunning simulation after source edits.",
            required_args=["fileset"],
            arg_sources={"fileset": "sources_1"},
            preconditions=["Vivado project is open and RTL files are loaded."],
            stop_condition="check_syntax returns ok=true or structured syntax findings.",
            optional=True,
        ),
        next_action(
            "get_compile_order",
            "Inspect simulation compile order and missing files before rerunning the failing testbench.",
            required_args=["fileset"],
            arg_sources={"fileset": simset},
            preconditions=["Simulation fileset exists."],
            stop_condition="get_compile_order explains missing files, duplicate files, unknown file types, or confirms order is clean.",
            optional=True,
        ),
        next_action(
            "run_behavioral_simulation",
            "Rerun the same simulation with a finite run_time after fixing the DUT or testbench issue.",
            required_args=["simset", "run_time"],
            arg_sources={"simset": simset, "run_time": "Use the previous bounded run_time or a reviewed shorter debug interval."},
            preconditions=["A code or testbench fix has been applied, or the previous diagnosis was reviewed."],
            stop_condition="run_behavioral_simulation returns completed with current invocation log span.",
        ),
    ]


def _simulation_vcd_export_failure(raw: dict[str, Any], *, command: str, simset: str, preflight: dict[str, Any]) -> dict[str, Any]:
    data = {
        **raw,
        "command": command,
        "status": "failed",
        "simset": simset,
        "preflight": preflight,
        "preflight_testbench_vcd_usage": bool(preflight.get("testbench_vcd_usage", False)),
        "preflight_testbench_vcd_sources": preflight.get("testbench_vcd_sources", []),
        "export_vcd_requested": True,
        "mcp_vcd_export_mode": "mcp_open_vcd",
        "vcd_export_mode": "mcp_open_vcd",
        "testbench_vcd_usage": bool(preflight.get("testbench_vcd_usage", False)),
        "testbench_vcd_detected": bool(preflight.get("testbench_vcd_usage", False)),
        "simulation_diagnosis": {
            "primary_cause": "vcd_export_failed",
            "causes": ["vcd_export_failed", "vivado_tcl_failed"],
            "status": "failed",
            "confidence": "medium",
            "message": "Vivado Tcl failed while MCP VCD export was requested; retry without export_vcd to isolate design/testbench behavior.",
        },
        "diagnosis": {
            "primary_cause": "vcd_export_failed",
            "causes": ["vcd_export_failed", "vivado_tcl_failed"],
            "status": "failed",
            "confidence": "medium",
        },
        "next_actions": [
            next_action(
                "run_behavioral_simulation",
                "Retry the same simulation with export_vcd=false to distinguish design/testbench failure from MCP VCD export failure.",
                required_args=["simset", "export_vcd"],
                arg_sources={"simset": simset, "export_vcd": "false"},
                preconditions=["The testbench has a finite timeout and simulation setup is otherwise unchanged."],
                stop_condition="run_behavioral_simulation returns completed or a non-VCD simulation diagnosis.",
            ),
            next_action(
                "get_simulation_result",
                "Read current simulation logs and artifacts after the failed VCD attempt.",
                required_args=["simset"],
                arg_sources={"simset": simset},
                preconditions=["Vivado simulation generated any log files."],
                stop_condition="get_simulation_result returns log_path/log_paths and diagnosis.",
                optional=True,
            ),
        ],
    }
    return failure(
        "run_behavioral_simulation",
        "SIMULATION_VCD_EXPORT_FAILED",
        "Behavioral simulation failed while MCP VCD export was requested; retry with export_vcd=false to isolate the failure.",
        raw.get("raw", ""),
        data,
    )


def _simulation_xsim_launch_transient_failure(
    raw: dict[str, Any],
    *,
    command: str,
    simset: str,
    run_time: str,
    export_vcd: bool,
    max_vcd_mb: int | float,
    preflight: dict[str, Any],
) -> dict[str, Any] | None:
    sim_dir = str(preflight.get("sim_dir", ""))
    artifacts = _scan_simulation_artifacts(sim_dir)
    empty_scripts = [
        item
        for item in artifacts.get("launch_scripts", [])
        if int(item.get("size_bytes") or 0) == 0
    ]
    if not empty_scripts:
        return None
    data = {
        **raw,
        "command": command,
        "status": "failed",
        "simset": simset,
        "sim_dir": sim_dir,
        "preflight": preflight,
        "artifacts": artifacts,
        "empty_launch_scripts": empty_scripts,
        "retry_scope": "once",
        "simulation_diagnosis": {
            "primary_cause": "xsim_generated_script_empty",
            "causes": ["xsim_generated_script_empty", "vivado_tcl_failed"],
            "status": "failed",
            "confidence": "high",
            "message": "Vivado created an empty XSIM launch script before launch_simulation failed; retry this bounded invocation once.",
        },
        "diagnosis": {
            "primary_cause": "xsim_generated_script_empty",
            "causes": ["xsim_generated_script_empty", "vivado_tcl_failed"],
            "status": "failed",
            "confidence": "high",
        },
        "next_actions": [
            next_action(
                "run_behavioral_simulation",
                "Retry the unchanged bounded simulation once because Vivado generated a zero-byte XSIM launch script.",
                required_args=["simset", "run_time", "export_vcd", "max_vcd_mb"],
                arg_sources={
                    "simset": simset,
                    "run_time": run_time,
                    "export_vcd": export_vcd,
                    "max_vcd_mb": max_vcd_mb,
                },
                preconditions=["The prior error_code is SIMULATION_XSIM_LAUNCH_TRANSIENT and this invocation has not already been retried."],
                stop_condition="The retry completes, or any second failure is returned without another automatic retry.",
            ),
            next_action(
                "get_simulation_result",
                "Inspect XSIM logs if the single retry also fails.",
                required_args=["simset"],
                arg_sources={"simset": simset},
                preconditions=["The single transient retry failed."],
                stop_condition="get_simulation_result returns the latest XSIM log and diagnosis.",
                optional=True,
            ),
        ],
    }
    return failure(
        "run_behavioral_simulation",
        "SIMULATION_XSIM_LAUNCH_TRANSIENT",
        "Vivado generated an empty XSIM launch script; the bounded simulation may be retried once.",
        raw.get("raw", ""),
        data,
    )


def _simulation_managed_restart_actions(
    *,
    simset: str,
    run_time: str,
    export_vcd: bool,
    max_vcd_mb: int | float,
    runtime_dir: str,
    project_path: str,
    stop_succeeded: bool,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if not stop_succeeded:
        actions.append(
            next_action(
                "stop_session",
                "Stop the managed Vivado process tree before recovering from the failed XSIM launch.",
                preconditions=["The transient launch failure reported managed_session_stopped=false."],
                stop_condition="stop_session confirms process_running=false.",
            )
        )
    actions.extend(
        [
            next_action(
                "start_session",
                "Start a fresh Vivado session generation after the failed XSIM launch.",
                required_args=["timeout_s"],
                arg_sources={"timeout_s": 240, "runtime_dir": runtime_dir or "reuse the prior managed runtime_dir"},
                preconditions=["The previous managed Vivado process tree is stopped."],
                stop_condition="start_session returns connected=true with a new generation_id.",
            ),
            next_action(
                "open_project",
                "Reopen the same Project Mode project in the fresh session.",
                required_args=["project_path"],
                arg_sources={"project_path": project_path or "failed run_behavioral_simulation.data.project_path"},
                preconditions=["start_session returned connected=true."],
                stop_condition="open_project returns ok=true for the original .xpr.",
            ),
            next_action(
                "run_behavioral_simulation",
                "Retry the unchanged bounded simulation once in the fresh session generation.",
                required_args=["simset", "run_time", "export_vcd", "max_vcd_mb"],
                arg_sources={
                    "simset": simset,
                    "run_time": run_time,
                    "export_vcd": export_vcd,
                    "max_vcd_mb": max_vcd_mb,
                },
                preconditions=["The original project is open in a new Vivado session generation and this invocation has not already been retried."],
                stop_condition="The retry completes, or any second failure is returned without another automatic retry.",
            ),
            next_action(
                "get_simulation_result",
                "Inspect XSIM logs if the fresh-session retry also fails.",
                required_args=["simset"],
                arg_sources={"simset": simset},
                preconditions=["The single fresh-session retry failed."],
                stop_condition="get_simulation_result returns the latest XSIM log and diagnosis.",
                optional=True,
            ),
        ]
    )
    return actions


def _directory_has_entries(directory: str | Path) -> bool:
    with os.scandir(directory) as entries:
        return next(entries, None) is not None


def _scan_simulation_artifacts(sim_dir: str) -> dict[str, Any]:
    if not sim_dir:
        return {"sim_dir": "", "log_path": "", "vcd_files": [], "wdb_files": [], "launch_scripts": [], "vcd_total_bytes": 0, "wdb_total_bytes": 0}
    root = Path(sim_dir)
    if not root.exists():
        return {"sim_dir": sim_dir, "log_path": "", "vcd_files": [], "wdb_files": [], "launch_scripts": [], "vcd_total_bytes": 0, "wdb_total_bytes": 0}
    vcd_files = _local_file_entries(root.glob("*.vcd"))
    wdb_files = _local_file_entries(root.glob("*.wdb"))
    launch_scripts = _local_file_entries(
        path
        for name in ("compile.bat", "compile.sh", "elaborate.bat", "elaborate.sh", "simulate.bat", "simulate.sh")
        if (path := root / name).is_file()
    )
    log_path = ""
    for name in ("xsim.log", "simulate.log"):
        candidate = root / name
        if candidate.exists():
            log_path = str(candidate)
            break
    return {
        "sim_dir": str(root),
        "log_path": log_path,
        "vcd_files": vcd_files,
        "wdb_files": wdb_files,
        "launch_scripts": launch_scripts,
        "vcd_total_bytes": sum(int(item.get("size_bytes") or 0) for item in vcd_files),
        "wdb_total_bytes": sum(int(item.get("size_bytes") or 0) for item in wdb_files),
    }


def _local_file_entries(paths: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in paths:
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = None
        entries.append({"path": str(path), "size_bytes": size})
    return entries


def _trace_write_failure(tool: str, operation_result: dict[str, Any], trace_error: str) -> dict[str, Any]:
    data = operation_result.get("data") if isinstance(operation_result.get("data"), dict) else {}
    return failure(
        tool,
        "WORKFLOW_TRACE_WRITE_FAILED",
        "The tool returned, but its workflow trace could not be recorded; treat the operation outcome as requiring review.",
        data={
            "trace_error": trace_error,
            "operation_result_summary": {
                "ok": bool(operation_result.get("ok")),
                "error_code": str(operation_result.get("error_code", "")),
                "message": str(operation_result.get("message", ""))[:240],
                "status": str(data.get("effective_status") or data.get("status") or ""),
            },
            "next_actions": [
                next_action(
                    "get_workflow_trace_status",
                    "Inspect trace integrity before continuing or handing off the project.",
                    preconditions=["A tool call returned WORKFLOW_TRACE_WRITE_FAILED."],
                    stop_condition="Trace integrity is READY, or the project is explicitly handed off with the trace failure recorded.",
                )
            ],
        },
    )


def _resolve_report_dir_argument(report_dir_arg: Any, context_report_dir: Any, run_name: str) -> str:
    actual = Path(str(context_report_dir)).resolve()
    if not str(context_report_dir).strip():
        raise ValueError("Vivado did not return the current invocation report directory")
    if not report_dir_arg:
        return str(actual)
    requested = Path(str(report_dir_arg)).resolve()
    if requested.name == "vmcp_reports":
        requested = requested / run_name
    try:
        actual.relative_to(requested)
    except ValueError as exc:
        raise ValueError(
            "Requested report_dir does not contain the current invocation report directory; stale or redirected report evidence is refused"
        ) from exc
    return str(actual)


def _normalize_target_language(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    language = str(value).strip()
    if not language:
        return None, None
    normalized = language.replace("_", "").replace("-", "").replace(" ", "").lower()
    if normalized in {"systemverilog", "sv"}:
        return "Verilog", "SystemVerilog"
    return language, None


def _verify_project_file_semantics(
    session: Any,
    expected_specs: list[dict[str, Any]],
    *,
    timeout_s: int,
) -> dict[str, Any]:
    fileset_results: dict[str, Any] = {}
    actual_specs: list[dict[str, Any]] = []
    verification_errors: list[str] = []
    for fileset in sorted({str(spec["fileset"]) for spec in expected_specs}):
        command = list_fileset_files_command(fileset=fileset)
        command_sha256 = hashlib.sha256(command.encode("utf-8")).hexdigest()
        try:
            raw = session.run_tcl(command, timeout_s=timeout_s)
        except Exception as exc:
            fileset_results[fileset] = {
                "ok": False,
                "command_sha256": command_sha256,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
            verification_errors.append(f"{fileset} post-create inventory failed: {exc.__class__.__name__}: {exc}")
            continue
        if not raw.get("ok"):
            fileset_results[fileset] = {
                "ok": False,
                "command_sha256": command_sha256,
                "message": str(raw.get("message", "")),
                "error_code": str(raw.get("error_code", "")),
            }
            verification_errors.append(
                f"{fileset} post-create inventory returned Tcl failure: {raw.get('message', '')}"
            )
            continue
        parsed = parse_fileset_files(str(raw.get("raw", "")), fileset=fileset)
        fileset_results[fileset] = {
            "ok": parsed["reconstruction_status"] == "READY",
            "command_sha256": command_sha256,
            "reconstruction_status": parsed["reconstruction_status"],
            "semantic_inventory_digest": parsed["semantic_inventory_digest"],
            "discovery_errors": parsed["discovery_errors"],
            "file_count": len(parsed["file_specs"]),
        }
        if parsed["reconstruction_status"] != "READY":
            verification_errors.extend(parsed["discovery_errors"])
        actual_specs.extend(parsed["file_specs"])
    comparison = compare_file_spec_inventories(expected_specs, actual_specs)
    comparison["matches"] = bool(comparison["matches"] and not verification_errors)
    return {
        **comparison,
        "verification_status": "READY" if comparison["matches"] else "BLOCK",
        "reconstruction_equivalent": bool(comparison["matches"]),
        "verification_errors": verification_errors,
        "filesets": fileset_results,
    }


def _systemverilog_files(files: list[str]) -> list[str]:
    return [path for path in files if Path(path).suffix.lower() in {".sv", ".svh"}]


def _load_waivers_safe(project_dir: str | Path) -> list[dict[str, Any]]:
    if not str(project_dir):
        return []
    try:
        return load_waivers(project_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def _data_or_finding(source_tool: str, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok"):
        data = result.get("data", {})
        if isinstance(data, dict):
            return data
        return {"status": "READY", "value": data, "findings": []}
    return {
        "status": "BLOCK",
        "findings": [
            {
                "severity": "BLOCK",
                "code": result.get("error_code", "TOOL_FAILED"),
                "message": result.get("message", f"{source_tool} failed"),
                "source_tool": source_tool,
            }
        ],
        "failed_result": result,
    }


def _tcl_list(values: list[str]) -> str:
    return "[list " + " ".join(tcl_list_quote(value) for value in values) + "]"


def _tcl_define_list(defines: dict[str, str | None]) -> str:
    return _tcl_list([name if value is None else f"{name}={value}" for name, value in defines.items()])


def _project_path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _tcl_failure(tool: str, data: dict[str, Any]) -> dict[str, Any]:
    raw = str(data.get("raw", ""))
    if data.get("error_code") == "PROJECT_ACTIVE_IDENTITY_MISMATCH" or PROJECT_ACTIVE_IDENTITY_MISMATCH_MARKER in raw:
        return failure(
            tool,
            "PROJECT_ACTIVE_IDENTITY_MISMATCH",
            "Vivado current_project does not match the managed project capability; the operation was blocked.",
            raw,
            {**data, "policy_allowed": False, "handler_executed": False, "stop_required": True},
        )
    if VIVADO_VERSION_BLOCK_MARKER in raw:
        return failure(
            tool,
            "UNSUPPORTED_VIVADO_VERSION",
            "Trusted Project Mode execution is currently restricted to Vivado 2021.2.",
            raw,
            {**data, "policy_allowed": False, "required_vivado_version": "2021.2"},
        )
    if EXECUTABLE_CONSTRAINT_BLOCK_MARKER in raw:
        return failure(
            tool,
            "EXECUTABLE_CONSTRAINT_INPUT_BLOCKED",
            "Vivado project contains a script-type, unknown, or non-XDC constraint input; execution was blocked before the design was opened or launched.",
            raw,
            {
                **data,
                "policy_allowed": False,
                "next_actions": [
                    next_action(
                        "list_fileset_files",
                        "Inspect every active constraints fileset and remove Tcl, script-type, unknown, or non-XDC entries in a reviewed project copy.",
                        required_args=["fileset"],
                        arg_sources={"fileset": "the constraints fileset named in the Vivado guard error"},
                        preconditions=["Do not bypass this guard through raw Tcl or a different run."],
                        stop_condition="Every constraint entry has a .xdc suffix and Vivado FILE_TYPE XDC before retrying.",
                    )
                ],
            },
        )
    if EXECUTABLE_INPUT_DISCOVERY_BLOCK_MARKER in raw:
        return failure(
            tool,
            "EXECUTABLE_INPUT_DISCOVERY_FAILED",
            "A required Vivado project-input query failed; execution was blocked because the input closure is unknown.",
            raw,
            {**data, "policy_allowed": False, "handler_executed": False, "stop_required": True},
        )
    if RUN_HOOK_BLOCK_MARKER in raw:
        return failure(
            tool,
            "RUN_HOOK_BLOCKED",
            "Vivado project contains a non-empty Tcl hook or script property; execution was blocked before launch.",
            raw,
            {
                **data,
                "policy_allowed": False,
                "next_actions": [
                    next_action(
                        "get_run_configuration",
                        "Inspect the affected run properties and remove executable hooks in a reviewed Vivado GUI session before retrying.",
                        required_args=["run_name"],
                        arg_sources={"run_name": "the run named in the Vivado Tcl error"},
                        preconditions=["Do not bypass the hook policy through run_tcl or safe_tcl."],
                        stop_condition="All Tcl PRE/POST, hook, and script properties are empty before open or launch.",
                    )
                ],
            },
        )
    return failure(tool, "TCL_FAILED", "Vivado Tcl command failed.", raw, data)


def _constraint_input_failure(tool: str, files: list[str]) -> dict[str, Any] | None:
    blocked = blocked_constraint_file_inputs(files)
    issues: dict[str, list[str]] = {}
    unreadable: dict[str, str] = {}
    if not blocked:
        for raw_path in files:
            path = Path(raw_path).expanduser().resolve()
            try:
                content = read_stable_bytes(path, root=path.parent, max_bytes=MAX_TRUSTED_XDC_BYTES)
                text = content.decode("utf-8-sig")
            except (ManagedPathError, OSError, UnicodeDecodeError) as exc:
                unreadable[raw_path] = f"{exc.__class__.__name__}: {exc}"
                continue
            policy_issues = validate_xdc_text(text)
            if policy_issues:
                issues[raw_path] = policy_issues
    if not blocked and not issues and not unreadable:
        return None
    error_code = "CONSTRAINT_INPUT_UNREADABLE" if unreadable and not blocked and not issues else "EXECUTABLE_CONSTRAINT_INPUT_BLOCKED"
    message = (
        "Constraint inputs could not be read as stable UTF-8 XDC evidence."
        if error_code == "CONSTRAINT_INPUT_UNREADABLE"
        else "Constraint inputs must be declarative .xdc files whose top-level and command-substitution commands are in the trusted XDC allowlist."
    )
    return failure(
        tool,
        error_code,
        message,
        data={
            "policy_allowed": False,
            "blocked_files": blocked or sorted(issues),
            "policy_issues": issues,
            "unreadable_files": unreadable,
            "allowed_extensions": [".xdc"],
            "max_xdc_bytes": MAX_TRUSTED_XDC_BYTES,
            "next_actions": [
                next_action(
                    "create_managed_xdc",
                    "Create a reviewed MCP-managed XDC instead of adding executable or dynamically composed Tcl constraints.",
                    required_args=["name", "constraints"],
                    arg_sources={"name": "a project-local XDC name", "constraints": "reviewed declarative XDC constraints"},
                    preconditions=["The project is open and every required constraint can be expressed by the supported structured XDC schema."],
                    stop_condition="The constraints fileset contains only stable UTF-8 XDC accepted by the trusted command allowlist.",
                    optional=True,
                )
            ],
        },
    )


def _composite_project_input_failure(tool: str, files: list[str]) -> dict[str, Any] | None:
    blocked = sorted(
        raw_path
        for raw_path in files
        if Path(raw_path).suffix.lower() in {".xci", ".bd", ".dcp"}
        or Path(raw_path).name.lower() == "component.xml"
    )
    if not blocked:
        return None
    return failure(
        tool,
        "EXECUTABLE_COMPOSITE_INPUT_UNSUPPORTED",
        "IP, Block Design, checkpoint, and component metadata files are not accepted by the current trusted project setup closure.",
        data={
            "handler_executed": False,
            "stop_required": True,
            "blocked_files": blocked,
            "blocked_extensions": [".xci", ".bd", ".dcp", "component.xml"],
            "next_actions": [
                next_action(
                    "get_agent_workflows",
                    "Use the reviewed RTL/XDC Project Mode workflow until composite-input attestation is available.",
                    stop_condition="Project setup contains no IP/BD/XCI/DCP/component.xml inputs.",
                )
            ],
        },
    )


def _hardware_tcl_failure(tool: str, data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("raw", "")
    error_code = parse_hardware_error_code(raw)
    message = "Vivado hardware Tcl command failed."
    if error_code == "NO_HW_TARGET":
        message = "No Vivado hardware target is available."
    elif error_code == "NO_HW_DEVICE":
        message = "No Vivado hardware device is available."
    elif error_code == "HW_SERVER_CONNECT_FAILED":
        message = "Could not connect to Vivado hw_server."
    elif error_code == "HW_PROGRAM_FAILED":
        message = "Vivado hardware device programming failed."
    return failure(tool, error_code, message, raw, _with_hardware_validation(data))


def _hardware_success(tool: str, summary: str, data: dict[str, Any]) -> dict[str, Any]:
    return success(tool, summary, _with_hardware_validation(data))


def _hardware_mode_disabled(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    requested_mode = str(args.get("hardware_mode") or "no_board").strip().lower()
    server_policy = hardware_server_policy()
    server_mode = str(server_policy["server_hardware_mode"])
    if requested_mode == "enabled" and server_mode == "enabled":
        return None
    return failure(
        tool,
        "HARDWARE_MODE_DISABLED",
        "Hardware Manager operations are disabled in the no-board software profile.",
        data=_with_hardware_validation(
            {
                "status": "BLOCK",
                "hardware_mode": requested_mode,
                "server_hardware_mode": server_mode,
                "required_hardware_mode": "enabled",
                "required_server_hardware_mode": "enabled",
                "server_policy": server_policy,
                "next_actions": [
                    next_action(
                        "detect_hardware_environment",
                        "Use the safe environment detector only; defer Hardware Manager actions until a real board/JTAG profile is enabled.",
                        required_args=[],
                        arg_sources={},
                        preconditions=["Current project phase is no-board Project Mode software validation."],
                        stop_condition="Hardware work is deferred or a future hardware profile is explicitly enabled.",
                    )
                ],
            }
        ),
    )


def _raw_tcl_programming_failure(tool: str, command: str) -> dict[str, Any] | None:
    if not raw_tcl_programming_command(command):
        return None
    return failure(
        tool,
        "RAW_TCL_HARDWARE_PROGRAMMING_FORBIDDEN",
        "Hardware programming commands cannot be executed through raw Tcl; use the dedicated programming tool.",
        data=_with_hardware_validation(
            {
                "status": "BLOCK",
                "command_excerpt": command[:500],
                "dedicated_tool_required": True,
                "next_actions": [
                    next_action(
                        "program_hw_device",
                        "Use the dedicated programming tool so hardware mode, intent, confirmation, board fingerprint, and hashes are enforced.",
                        required_args=[
                            "bitstream_path",
                            "hardware_intent",
                            "confirm",
                            "board_fingerprint",
                            "expected_bitstream_sha256",
                            "hardware_mode",
                        ],
                        arg_sources={
                            "bitstream_path": "validated collect_build_artifacts manifest",
                            "hardware_intent": "explicit human-approved hardware operation",
                            "confirm": "literal PROGRAM_FPGA",
                            "board_fingerprint": "selected physical target identity",
                            "expected_bitstream_sha256": "validated artifact manifest",
                            "hardware_mode": "literal enabled only in a future board-enabled server profile",
                        },
                        preconditions=["A real board/JTAG environment is intentionally enabled."],
                        stop_condition="program_hw_device passes every hardware gate or programming remains deferred.",
                    )
                ],
            }
        ),
    )


def _with_hardware_validation(data: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(data or {})
    payload.setdefault("hardware_validation", hardware_validation_boundary())
    return payload


def _manifest_programming_intent_failure(args: dict[str, Any], manifest_path: Path) -> dict[str, Any] | None:
    missing: list[str] = []
    if not str(args.get("hardware_intent", "")).strip():
        missing.append("hardware_intent")
    if str(args.get("confirm", "")) != "PROGRAM_FPGA":
        missing.append("confirm=PROGRAM_FPGA")
    if not str(args.get("board_fingerprint", "")).strip():
        missing.append("board_fingerprint")
    if not str(args.get("expected_bitstream_sha256", "")).strip():
        missing.append("expected_bitstream_sha256")
    if not str(args.get("manifest_sha256", "")).strip():
        missing.append("manifest_sha256")
    if not missing:
        return None
    return _programming_gate_failure(
        "program_from_artifact_manifest",
        "HARDWARE_INTENT_REQUIRED",
        "FPGA programming requires explicit hardware intent, confirmation, board fingerprint, and artifact hashes.",
        {"missing": missing, "manifest_path": str(manifest_path)},
    )


def _staged_programming_bitstream(
    tool: str,
    gate: dict[str, Any],
) -> tuple[StagedEvidenceFile, dict[str, Any] | None]:
    staged_data = gate.get("data", {}).get("staged_bitstream", {})
    try:
        staged = StagedEvidenceFile(
            path=Path(str(staged_data["path"])),
            sha256=str(staged_data["sha256"]),
            size=int(staged_data["size"]),
            file_id=str(staged_data["file_id"]),
            mtime_ns=int(staged_data["mtime_ns"]),
        )
        verify_staged_file(staged)
        return staged, None
    except (KeyError, TypeError, ValueError, OSError) as exc:
        empty = StagedEvidenceFile(Path(), "", 0, "", 0)
        return empty, _programming_gate_failure(
            tool,
            "STAGED_BITSTREAM_CHANGED",
            str(exc),
            {"staged_bitstream": staged_data},
        )


def _programming_gate_failure(tool: str, error_code: str, message: str, data: dict[str, Any]) -> dict[str, Any]:
    required_args = ["hardware_intent", "confirm", "board_fingerprint", "expected_bitstream_sha256"]
    arg_sources = {
        "hardware_intent": "human-approved programming reason",
        "confirm": "literal PROGRAM_FPGA",
        "board_fingerprint": "get_hw_device_status.data actual device/part fingerprint",
        "expected_bitstream_sha256": "collect_build_artifacts manifest bitstream sha256",
    }
    if tool == "program_from_artifact_manifest":
        required_args.append("manifest_sha256")
        arg_sources["manifest_sha256"] = "SHA256 of the strictly validated artifact manifest"
    payload = _with_hardware_validation(
        {
            "status": "BLOCK",
            "reason": error_code,
            **data,
            "next_actions": [
                next_action(
                    tool,
                    "Retry FPGA programming only after explicit hardware intent, confirmation, board fingerprint, and artifact hashes are correct.",
                    required_args=required_args,
                    arg_sources=arg_sources,
                    preconditions=["A real FPGA board is connected and selected intentionally."],
                    stop_condition=f"{tool} passes gate checks or hardware programming is deferred.",
                )
            ],
        }
    )
    return failure(tool, error_code, message, data=payload)


def _destructive_confirm_action(tool: str, confirm: str) -> dict[str, Any]:
    return next_action(
        tool,
        "Review the dry-run output, then retry with explicit intent and confirmation if deletion/reset is still required.",
        required_args=["dry_run", "intent", "confirm"],
        arg_sources={
            "dry_run": "set false after reviewing dry-run output",
            "intent": "human-approved reason for this destructive maintenance action",
            "confirm": f"literal {confirm}",
        },
        preconditions=["Dry-run output has been reviewed and targets are generated Vivado outputs only."],
        stop_condition=f"{tool} completes or remains dry-run.",
    )


def _planned_clean_run_output_targets(
    run_names: list[str],
    simsets: list[str],
    *,
    include_cache: bool,
    include_gen: bool,
) -> list[dict[str, Any]]:
    targets = [{"kind": "run_output", "name": run_name, "pattern": "<project_dir>/<project_name>.runs/<run_name>"} for run_name in run_names]
    targets.extend({"kind": "simulation_output", "name": simset, "pattern": "<project_dir>/<project_name>.sim/<simset>"} for simset in simsets)
    if include_cache:
        targets.append({"kind": "project_cache", "name": ".cache", "pattern": "<project_dir>/<project_name>.cache"})
    if include_gen:
        targets.append({"kind": "generated_ip_output", "name": ".gen", "pattern": "<project_dir>/<project_name>.gen"})
    return targets


def _hardware_fingerprint(preflight: dict[str, Any]) -> str:
    device = str(preflight.get("device", "")).strip()
    part = str(preflight.get("part", "")).strip()
    if device and part:
        return f"device={device}|part={part}"
    return device or part


def _board_fingerprint_matches(expected: str, preflight: dict[str, Any]) -> bool:
    expected_text = expected.strip()
    device = str(preflight.get("device", "")).strip()
    part = str(preflight.get("part", "")).strip()
    if not device or not part:
        return False
    return expected_text == f"device={device}|part={part}"


def _validated_programming_paths(
    *,
    tool: str,
    bitstream_path: Any,
    ltx_path: Any | None = None,
    missing_bit_code: str = "BITSTREAM_NOT_FOUND",
) -> tuple[Path | None, Path | None, dict[str, Any] | None]:
    if not bitstream_path:
        return None, None, failure(tool, "BITSTREAM_NOT_FOUND", "bitstream_path is required.", data=_with_hardware_validation())
    try:
        bitstream = validate_bitstream_path(str(bitstream_path))
    except FileNotFoundError as exc:
        return None, None, failure(tool, missing_bit_code, f"Bitstream not found: {exc}", data=_with_hardware_validation({"bitstream_path": str(bitstream_path)}))
    except ValueError as exc:
        return None, None, failure(tool, "INVALID_BITSTREAM", str(exc), data=_with_hardware_validation({"bitstream_path": str(bitstream_path)}))

    probes = None
    if ltx_path:
        try:
            probes = validate_ltx_path(str(ltx_path))
        except FileNotFoundError as exc:
            return None, None, failure(tool, "LTX_NOT_FOUND", f"Probes file not found: {exc}", data=_with_hardware_validation({"ltx_path": str(ltx_path)}))
        except ValueError as exc:
            return None, None, failure(tool, "INVALID_LTX", str(exc), data=_with_hardware_validation({"ltx_path": str(ltx_path)}))
    return bitstream, probes, None


def _readiness_input_failure(failed_tool: str, failed_result: dict[str, Any]) -> dict[str, Any]:
    return failure(
        "check_bitstream_readiness",
        "READINESS_INPUT_FAILED",
        f"Could not collect {failed_tool}.",
        failed_result.get("raw_excerpt", ""),
        {
            "failed_tool": failed_tool,
            "failed_result": failed_result,
            "next_actions": [
                next_action(
                    failed_tool,
                    f"Collect {failed_tool} successfully before rerunning check_bitstream_readiness.",
                    preconditions=["Project is open and the required design/run context is available."],
                    stop_condition=f"{failed_tool} returns ok=true.",
                )
            ],
        },
    )


def _analysis_input_failure(
    failed_tool: str,
    failed_result: dict[str, Any],
    *,
    tool: str = "analyze_timing_closure",
    action_tool: str | None = None,
    action_required_args: list[str] | None = None,
    action_arg_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    routed_tool = action_tool or failed_tool
    failed_error_code = str(failed_result.get("error_code", ""))
    if failed_error_code in {
        "CONSTRAINT_INPUT_UNREADABLE",
        "EXECUTABLE_CONSTRAINT_INPUT_BLOCKED",
        "RUN_HOOK_BLOCKED",
        "UNSUPPORTED_VIVADO_VERSION",
    }:
        return failure(
            tool,
            failed_error_code,
            str(failed_result.get("message") or f"{failed_tool} was blocked by trusted-execution policy."),
            failed_result.get("raw_excerpt", ""),
            {
                "failed_tool": failed_tool,
                "failed_result": failed_result,
                "policy_allowed": False,
                "next_actions": list(failed_result.get("next_actions", [])),
            },
        )
    return failure(
        tool,
        "ANALYSIS_INPUT_FAILED",
        f"Could not collect {failed_tool}.",
        failed_result.get("raw_excerpt", ""),
        {
            "failed_tool": failed_tool,
            "failed_result": failed_result,
            "next_actions": [
                next_action(
                    routed_tool,
                    f"Collect evidence for {failed_tool} successfully before rerunning {tool}.",
                    required_args=action_required_args,
                    arg_sources=action_arg_sources,
                    preconditions=["Project is open and the required design/run context is available."],
                    stop_condition=f"{failed_tool} dependency is available and {tool} can be rerun.",
                )
            ],
        },
    )


def _filter_messages(data: dict[str, Any], *, severities: set[str]) -> dict[str, Any]:
    counts = {"ERROR": 0, "CRITICAL WARNING": 0, "WARNING": 0, "INFO": 0}
    messages = [msg for msg in data.get("messages", []) if msg.get("severity") in severities]
    for msg in messages:
        severity = msg.get("severity", "")
        if severity in counts:
            counts[severity] += 1
    return {"ok": True, "counts": counts, "messages": messages}


def _read_vivado_messages_command() -> str:
    return (
        "set message_files [list]; "
        "if {![catch {set p [current_project]}] && $p ne \"\"} {"
        "set project_dir [get_property DIRECTORY $p]; "
        "foreach pattern [list "
        "[file join $project_dir *.runs * runme.log] "
        "[file join $project_dir *.sim * * xsim xsim.log]"
        "] {foreach f [glob -nocomplain $pattern] {lappend message_files $f}}"
        "}; "
        "if {[llength $message_files] == 0} {"
        "set candidates [glob -nocomplain [file join [pwd] *.log]]; "
        "set log_path \"\"; "
        "foreach p $candidates {if {[file tail $p] eq \"vivado.log\"} {set log_path $p}}; "
        "if {$log_path eq \"\" && [llength $candidates] > 0} {set log_path [lindex $candidates end]}; "
        "if {$log_path ne \"\"} {lappend message_files $log_path}"
        "}; "
        "set content \"\"; "
        "foreach log_path $message_files {"
        "if {[file exists $log_path]} {"
        "set fh [open $log_path r]; "
        "seek $fh 0 end; "
        "set size [tell $fh]; "
        "set start [expr {$size > 1048576 ? $size - 1048576 : 0}]; "
        "seek $fh $start start; "
        "append content \"\\n__VMCP_LOG_FILE__=$log_path\\n\"; "
        "append content [read $fh]; "
        "close $fh"
        "}"
        "}; "
        "set content"
    )


def _run_failure_context_command(run_name: str) -> str:
    run_ref = tcl_list_quote(run_name)
    return (
        f"{tcl_wire_prelude()}; "
        f"set runs [get_runs -quiet {run_ref}]; "
        "if {[llength $runs] == 0} {error \"Vivado run not found\"}; "
        "set r [lindex $runs 0]; "
        "set run_dir [get_property DIRECTORY $r]; "
        "set status [get_property STATUS $r]; "
        "set progress [get_property PROGRESS $r]; "
        "set needs_refresh [get_property NEEDS_REFRESH $r]; "
        "set log_files [list]; "
        "foreach pattern [list [file join $run_dir runme.log] [file join $run_dir *.log] [file join $run_dir *.jou]] {"
        "foreach f [glob -nocomplain $pattern] {if {[lsearch -exact $log_files $f] < 0} {lappend log_files $f}}"
        "}; "
        "set content [join [list "
        "\"run_dir=$run_dir\" "
        "\"status=$status\" "
        "\"progress=$progress\" "
        "\"needs_refresh=$needs_refresh\" "
        "\"log_files=[::vivado_agent_mcp_wire_list $log_files]\" "
        "] \"\\n\"]; "
        "append content \"\\n__VMCP_RUN_LOG_TAIL_START__\\n\"; "
        "foreach log_path $log_files {"
        "if {[file exists $log_path]} {"
        "set fh [open $log_path r]; "
        "seek $fh 0 end; "
        "set size [tell $fh]; "
        "set start [expr {$size > 65536 ? $size - 65536 : 0}]; "
        "seek $fh $start start; "
        "append content \"\\n__VMCP_RUN_LOG_FILE__=$log_path\\n\"; "
        "append content [read $fh]; "
        "close $fh"
        "}"
        "}; "
        "set content"
    )


def _run_failure_diagnosis_data(
    *,
    run_name: str,
    progress_result: dict[str, Any],
    context_raw: dict[str, Any],
    critical_result: dict[str, Any],
) -> dict[str, Any]:
    progress_data = progress_result.get("data", {}) if progress_result.get("ok") else {}
    context = _parse_run_failure_context(context_raw.get("raw", "")) if context_raw.get("ok") else {}
    critical = critical_result.get("data", {}) if critical_result.get("ok") else {}
    findings = _run_failure_findings(
        progress_result=progress_result,
        progress_data=progress_data,
        context_raw=context_raw,
        context=context,
        critical_result=critical_result,
        critical=critical,
    )
    primary_cause = _primary_run_failure_cause(findings)
    severity = _max_finding_severity(findings)
    next_actions = _run_failure_next_actions(run_name=run_name, primary_cause=primary_cause, findings=findings)
    return {
        "status": severity,
        "run_name": run_name,
        "run_progress": progress_data,
        "run_context": context,
        "critical_warnings": critical,
        "diagnosis": {
            "primary_cause": primary_cause,
            "severity": severity,
            "finding_count": len(findings),
        },
        "findings": findings,
        "next_actions": next_actions,
        "inputs": {
            "progress_ok": bool(progress_result.get("ok")),
            "context_ok": bool(context_raw.get("ok")),
            "critical_warnings_ok": bool(critical_result.get("ok")),
        },
    }


def _parse_run_failure_context(raw: str) -> dict[str, Any]:
    head, marker, tail = raw.partition("__VMCP_RUN_LOG_TAIL_START__")
    values = _parse_key_value_lines(head)
    log_tail = tail.strip() if marker else ""
    log_files = decode_wire_list(values.get("log_files", ""))
    marker_files = [
        line.split("=", 1)[1].strip()
        for line in log_tail.splitlines()
        if line.startswith("__VMCP_RUN_LOG_FILE__=")
    ]
    return {
        "run_dir": values.get("run_dir", ""),
        "status": values.get("status", ""),
        "progress": values.get("progress", ""),
        "needs_refresh": _truthy_text(values.get("needs_refresh", "")),
        "log_files": log_files or marker_files,
        "log_tail": _truncate_text(log_tail, 20000),
        "log_tail_bytes": len(log_tail.encode("utf-8")),
    }


def _run_failure_findings(
    *,
    progress_result: dict[str, Any],
    progress_data: dict[str, Any],
    context_raw: dict[str, Any],
    context: dict[str, Any],
    critical_result: dict[str, Any],
    critical: dict[str, Any],
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    state = str(progress_data.get("state", ""))
    if not progress_result.get("ok"):
        findings.append({"severity": "WARN", "code": "RUN_PROGRESS_UNAVAILABLE", "message": "get_run_progress did not return run state."})
    if not context_raw.get("ok"):
        findings.append({"severity": "WARN", "code": "RUN_LOG_CONTEXT_UNAVAILABLE", "message": "Run directory or run log tail could not be read."})
    if state == "failed":
        findings.append({"severity": "BLOCK", "code": "RUN_FAILED", "message": "Vivado run status is failed."})
    progress = progress_data.get("progress", {}) if isinstance(progress_data.get("progress"), dict) else {}
    if _truthy_text(progress.get("needs_refresh", "")) or bool(context.get("needs_refresh")):
        findings.append({"severity": "WARN", "code": "RUN_NEEDS_REFRESH", "message": "Vivado reports the run needs refresh."})
    counts = critical.get("counts", {}) if isinstance(critical, dict) else {}
    if counts.get("ERROR", 0):
        findings.append({"severity": "BLOCK", "code": "RUN_ERROR", "message": f"Vivado messages contain {counts['ERROR']} error(s)."})
    if counts.get("CRITICAL WARNING", 0):
        findings.append({"severity": "BLOCK", "code": "RUN_CRITICAL_WARNING", "message": f"Vivado messages contain {counts['CRITICAL WARNING']} critical warning(s)."})
    if not critical_result.get("ok"):
        findings.append({"severity": "WARN", "code": "CRITICAL_MESSAGES_UNAVAILABLE", "message": "Critical Vivado messages could not be parsed."})
    log_tail = str(context.get("log_tail", ""))
    if "No clocks" in log_tail or "no clock" in log_tail.lower() or "Timing 38-313" in log_tail:
        findings.append({"severity": "WARN", "code": "CONSTRAINT_CLOCK_RISK", "message": "Run log suggests missing or incomplete clock constraints."})
    if "ERROR:" in log_tail and not counts.get("ERROR", 0):
        findings.append({"severity": "BLOCK", "code": "RUN_LOG_ERROR", "message": "Run log tail contains ERROR lines not present in get_critical_warnings output."})
    if context_raw.get("ok") and not context.get("log_files"):
        findings.append({"severity": "WARN", "code": "RUN_LOG_MISSING", "message": "Run exists but no run log files were found."})
    if not findings:
        findings.append({"severity": "INFO", "code": "NO_RUN_BLOCKER_FOUND", "message": "No obvious run blocker was found in progress, run log tail, or critical messages."})
    return findings


def _primary_run_failure_cause(findings: list[dict[str, str]]) -> str:
    priority = [
        "RUN_ERROR",
        "RUN_LOG_ERROR",
        "RUN_FAILED",
        "RUN_CRITICAL_WARNING",
        "CONSTRAINT_CLOCK_RISK",
        "RUN_NEEDS_REFRESH",
        "RUN_PROGRESS_UNAVAILABLE",
        "RUN_LOG_CONTEXT_UNAVAILABLE",
        "CRITICAL_MESSAGES_UNAVAILABLE",
        "RUN_LOG_MISSING",
        "NO_RUN_BLOCKER_FOUND",
    ]
    by_code = {finding["code"]: finding for finding in findings}
    for code in priority:
        if code in by_code:
            return code.lower()
    return findings[0]["code"].lower() if findings else "unknown"


def _max_finding_severity(findings: list[dict[str, str]]) -> str:
    order = {"INFO": 0, "WARN": 1, "BLOCK": 2}
    severity = max((order.get(finding.get("severity", "INFO"), 0) for finding in findings), default=0)
    return {value: key for key, value in order.items()}[severity]


def _run_failure_next_actions(*, run_name: str, primary_cause: str, findings: list[dict[str, str]]) -> list[dict[str, Any]]:
    actions = [
        next_action(
            "get_critical_warnings",
            "Review the exact Vivado ERROR and CRITICAL WARNING messages before changing the design.",
            required_args=[],
            arg_sources={},
            preconditions=["Project is open and run logs are still present."],
            stop_condition="Critical message list is available or the log source is missing.",
        ),
        next_action(
            "get_run_configuration",
            "Inspect run strategy, part, top, and launch configuration for the failed run.",
            required_args=["run_name"],
            arg_sources={"run_name": run_name},
            preconditions=["The failed Vivado run still exists in the project."],
            stop_condition="Run configuration has been collected or the run is missing.",
        ),
    ]
    codes = {finding["code"] for finding in findings}
    if "CONSTRAINT_CLOCK_RISK" in codes or "RUN_CRITICAL_WARNING" in codes:
        actions.append(
            next_action(
                "analyze_timing_closure",
                "Aggregate timing, constraints, DRC, methodology, and run messages for closure blockers.",
                required_args=[],
                arg_sources={},
                preconditions=["Implementation reports or run messages are available."],
                stop_condition="Timing closure findings identify repair inputs or no timing blocker remains.",
            )
        )
    if primary_cause in {"run_error", "run_log_error", "run_failed"}:
        actions.append(
            next_action(
                "check_syntax",
                "Check source syntax before relaunching synthesis or implementation.",
                required_args=[],
                arg_sources={"fileset": "default sources_1 unless the workflow uses another fileset"},
                preconditions=["Project is open and source files are available."],
                stop_condition="Syntax status is READY or concrete source errors are reported.",
            )
        )
    actions.append(
        next_action(
            "get_run_progress",
            "Poll the run again only after addressing the diagnosed blocker or relaunching the run.",
            required_args=["run_name"],
            arg_sources={"run_name": run_name},
            preconditions=["A repair or relaunch step has completed."],
            stop_condition="Run reaches terminal state or workflow max_wait_s is exceeded.",
            optional=True,
        )
    )
    return actions


def _truthy_text(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def _classify_run_state(status: str, expect_bitstream: bool = False, bitstream_exists: bool = False) -> str:
    lower = status.lower()
    if "error" in lower or "fail" in lower:
        return "failed"
    if "incomplete" in lower or re.search(r"\bnot\s+complete(?:d)?\b", lower):
        return "unknown"
    if re.search(r"\bcomplete(?:d)?\b", lower):
        if expect_bitstream and not bitstream_exists:
            return "failed" if "write_bitstream" in lower else "not_started"
        return "complete"
    if "running" in lower or "launch" in lower:
        return "running"
    if "queued" in lower:
        return "queued"
    if "not started" in lower:
        return "not_started"
    return "unknown"


def _normalize_run_progress(progress: dict[str, str], *, state: str, expect_bitstream: bool) -> dict[str, Any]:
    raw_status = progress.get("status", "")
    bitstream_exists = progress.get("bitstream_exists") == "1"
    terminal = state in {"complete", "failed"}
    percent = _parse_progress_percent(progress.get("progress", ""))
    phase = _run_phase(raw_status)

    if state == "complete":
        normalized_status = "complete"
        normalized_progress = "100%"
        percent = 100.0
        phase = "bitstream" if expect_bitstream and bitstream_exists else phase
    elif state == "launching":
        normalized_status = "launching"
        normalized_progress = progress.get("progress", "")
        phase = "bitstream" if expect_bitstream else phase
    elif state == "failed":
        normalized_status = "failed"
        normalized_progress = progress.get("progress", "")
    else:
        normalized_status = raw_status
        normalized_progress = progress.get("progress", "")

    return {
        "status": normalized_status,
        "progress": normalized_progress,
        "terminal": terminal,
        "phase": phase,
        "percent": percent,
    }


def _parse_progress_percent(value: str) -> float | None:
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value)
    if not match:
        return None
    return max(0.0, min(100.0, float(match.group(0))))


def _run_phase(status: str) -> str:
    lower = status.lower()
    if "write_bitstream" in lower or "bitstream" in lower:
        return "bitstream"
    if "route" in lower:
        return "route"
    if "place" in lower:
        return "place"
    if "opt" in lower:
        return "opt_design"
    if "synth" in lower:
        return "synthesis"
    if "complete" in lower:
        return "complete"
    if "not started" in lower:
        return "not_started"
    if "queued" in lower:
        return "queued"
    if "running" in lower:
        return "running"
    return "unknown"
