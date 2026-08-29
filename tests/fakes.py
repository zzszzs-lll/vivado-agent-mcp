from __future__ import annotations

import re
from pathlib import Path

from bitstream_fixture import write_test_design_execution_identity
from vivado_agent_mcp.vivado.constraints import (
    CHECK_TIMING_KEYS,
    CHECK_TIMING_REPORT_BEGIN_MARKER,
    METHODOLOGY_REPORT_BEGIN_MARKER,
)
from vivado_agent_mcp.vivado.parsers import (
    DRC_REPORT_BEGIN_MARKER,
    TIMING_SUMMARY_REPORT_BEGIN_MARKER,
    attest_report_text,
)
from vivado_agent_mcp.vivado.wire import encode_wire_list, encode_wire_row


def complete_check_timing_body(**overrides: int) -> str:
    counts = {key: 0 for key in CHECK_TIMING_KEYS} | overrides
    return "check_timing report\n" + "\n".join(
        f"checking {key} ({counts[key]})" for key in CHECK_TIMING_KEYS
    )


class RecordingSession:
    def __init__(self, raw: str = "", ok: bool = True) -> None:
        self.commands: list[str] = []
        self.raw = raw
        self.ok = ok

    def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
        self.commands.append(command)
        return {"ok": self.ok, "raw": self.raw}


class StatefulFakeVivadoSession:
    def __init__(
        self,
        project_dir: Path,
        *,
        simulation_status: str = "completed",
        needs_refresh: str = "0",
        source_ok: bool = True,
        timing_met: bool = True,
        drc_clean: bool = True,
        artifact_present: bool = True,
        report_bundle_ok: bool = True,
    ) -> None:
        self.project_dir = project_dir
        self.project_name = "demo"
        self.top = "top"
        self.part = "xc7a35tcpg236-1"
        self.simulation_status = simulation_status
        self.needs_refresh = needs_refresh
        self.source_ok = source_ok
        self.timing_met = timing_met
        self.drc_clean = drc_clean
        self.artifact_present = artifact_present
        self.report_bundle_ok = report_bundle_ok
        self.generation_id = "test-generation"
        self.design_execution_identity = write_test_design_execution_identity(project_dir)
        self.commands: list[str] = []

    def run_tcl(self, command: str, timeout_s: int = 60) -> dict:
        self.commands.append(command)
        text = command.lower()
        project_text = str(self.project_dir).replace("\\", "/")
        if "set collection_id" in text and "report_command_schema=vivado_2021_2_v1" in text:
            if not self.report_bundle_ok:
                return {"ok": False, "raw": "ERROR: report bundle generation failed"}
            match = re.search(r"set collection_id \{([^}]+)\}", command)
            collection_id = match.group(1) if match else "report_stateful_fake"
            report_dir = self.project_dir / "vmcp_reports" / "impl_1" / "invocations" / collection_id
            report_dir.mkdir(parents=True, exist_ok=True)
            for name in (
                "timing_summary.rpt",
                "utilization.rpt",
                "drc.rpt",
                "methodology.rpt",
                "qor_summary.rpt",
                "cdc.rpt",
                "clock_interaction.rpt",
                "power.rpt",
                "messages.log",
            ):
                (report_dir / name).write_text(f"Fake report for {name}\n", encoding="utf-8")
            run_dir = self.project_dir / f"{self.project_name}.runs" / "impl_1"
            run_dir.mkdir(parents=True, exist_ok=True)
            run_log = run_dir / "runme.log"
            run_log.write_text("INFO: stateful fake run complete\n", encoding="utf-8")
            started_ms = min(int(path.stat().st_mtime * 1000) for path in report_dir.iterdir()) - 100
            context = {
                "run_name": "impl_1",
                "project_dir": project_text,
                "report_dir": str(report_dir),
                "collection_id": collection_id,
                "collection_started_ms": str(started_ms),
                "vivado_version_short": "2021.2",
                "vivado_build": "Vivado v2021.2",
                "report_command_schema": "vivado_2021_2_v1",
                "open_run_status": "generated",
                "run_status": "write_bitstream Complete!",
                "run_progress": "100%",
                "run_needs_refresh": self.needs_refresh,
                "run_directory": str(run_dir),
                "run_log_path": str(run_log),
                "run_log_size_before": str(run_log.stat().st_size),
                "run_log_size_after": str(run_log.stat().st_size),
                "run_log_mtime_before": str(int(run_log.stat().st_mtime)),
                "run_log_mtime_after": str(int(run_log.stat().st_mtime)),
                "messages_complete_scan": "1",
                "messages_source_stable": "1",
                "messages_extracted_count": "0",
            }
            for category in (
                "timing",
                "utilization",
                "drc",
                "methodology",
                "qor",
                "cdc",
                "clock_interaction",
                "power",
                "messages",
            ):
                context[f"{category}_report_command_status"] = "generated"
                context[f"{category}_report_command_message"] = "stateful fake report collection"
            return {
                "ok": True,
                "generation_id": self.generation_id,
                "raw": "\n".join(f"{key}={value}" for key, value in context.items()),
            }
        if "run_bitstream_files=" in command and "expected_bitstream_path=" in command:
            bitstream_path = f"{project_text}/{self.project_name}.runs/impl_1/top.bit" if self.artifact_present else ""
            return {
                "ok": True,
                "generation_id": self.generation_id,
                "raw": (
                    f"project_name={self.project_name}\n"
                    f"project_dir={project_text}\n"
                    f"project_part={self.part}\n"
                    f"run_dir={project_text}/{self.project_name}.runs/impl_1\n"
                    "run_srcset=sources_1\n"
                    f"run_top={self.top}\n"
                    "run_status=write_bitstream Complete!\n"
                    "run_progress=100%\n"
                    f"run_needs_refresh={self.needs_refresh}\n"
                    f"expected_bitstream_path={bitstream_path}\n"
                    f"run_bitstream_files={encode_wire_list([bitstream_path] if bitstream_path else [])}\n"
                    "write_bitstream_step_enabled=1\n"
                    "write_bitstream_step_status=Complete!"
                ),
            }
        if ("bitstream_files=" in command and "run_bitstream_files=" not in command) or "get_property name $p" in text and "filesets=" in text:
            bitstream_path = f"{project_text}/{self.project_name}.runs/impl_1/top.bit" if self.artifact_present else ""
            return {
                "ok": True,
                "generation_id": self.generation_id,
                "raw": (
                    f"project_name={self.project_name}\n"
                    f"project_dir={project_text}\n"
                    f"name={self.project_name}\n"
                    f"directory={project_text}\n"
                    f"part={self.part}\n"
                    f"top={self.top}\n"
                    "sim_top=tb_top\n"
                    f"filesets={encode_wire_list(['sources_1', 'constrs_1', 'sim_1'])}\n"
                    f"runs={encode_wire_list(['synth_1', 'impl_1'])}\n"
                    f"bitstream_files={encode_wire_list([bitstream_path] if bitstream_path else [])}"
                ),
            }
        if (
            "set project_dir [get_property directory $p]" in text
            and "run_needs_refresh=[get_property needs_refresh $r]" in text
        ):
            return {
                "ok": True,
                "generation_id": self.generation_id,
                "raw": (
                    f"project_name={self.project_name}\n"
                    f"project_dir={project_text}\n"
                    f"run_dir={project_text}/{self.project_name}.runs/impl_1\n"
                    "run_status=write_bitstream Complete!\n"
                    "run_progress=100%\n"
                    f"run_needs_refresh={self.needs_refresh}"
                ),
            }
        if "get_runs -quiet" in text and "properties_begin=__vmcp_run_properties_begin__" in text:
            run_name = "synth_1" if "synth_1" in command else "impl_1"
            run_status = "synth_design Complete!" if run_name == "synth_1" else "write_bitstream Complete!"
            return {
                "ok": True,
                "generation_id": self.generation_id,
                "raw": (
                    f"name={run_name}\n"
                    "flow=Vivado Synthesis\n"
                    "strategy=Default\n"
                    f"status={run_status}\n"
                    "progress=100%\n"
                    f"needs_refresh={self.needs_refresh}\n"
                    f"directory={project_text}/{self.project_name}.runs/{run_name}\n"
                    "properties_begin=__VMCP_RUN_PROPERTIES_BEGIN__\n"
                ),
            }
        if "__vmcp_syntax_report_begin__" in text:
            status = "READY" if self.source_ok else "BLOCK"
            raw = "" if self.source_ok else "ERROR: [Synth 8-439] syntax failure"
            return {
                "ok": True,
                "raw": (
                    f"status={status}\n"
                    "fileset=sources_1\n"
                    "raw_begin=__VMCP_SYNTAX_REPORT_BEGIN__\n"
                    f"{raw}\n"
                    "raw_end=__VMCP_SYNTAX_REPORT_END__"
                ),
            }
        if "get_files -compile_order" in text:
            row = encode_wire_row(
                {
                    "file": f"{project_text}/rtl/top.v",
                    "type": "Verilog",
                    "exists": "1",
                    "managed": "0",
                    "used_in": "synthesis",
                    "order": "0",
                }
            )
            return {
                "ok": True,
                "raw": (
                    "status=READY\n"
                    "compile_order_schema=vivado_2021_2_v1\n"
                    "compile_order_complete=1\n"
                    "compile_order_count=1\n"
                    "fileset=sources_1\n"
                    "top=top\n"
                    "raw_begin=__VMCP_COMPILE_ORDER_BEGIN__\n"
                    f"{row}\n"
                    "raw_end=__VMCP_COMPILE_ORDER_END__"
                ),
            }
        if "xdc_begin=__vmcp_xdc_begin__" in text:
            xdc_path = f"{project_text}/xdc/top.xdc"
            return {
                "ok": True,
                "raw": (
                    f"xdc_files={encode_wire_list([xdc_path])}\n"
                    "xdc_file_discovery_status=READY\n"
                    "fileset_discovery_status=READY\n"
                    "design_discovery_status=READY\n"
                    "ports_discovery_status=READY\n"
                    "clocks_discovery_status=READY\n"
                    "generated_clocks_discovery_status=READY\n"
                    "clock_report_discovery_status=READY\n"
                    f"discovery_errors={encode_wire_list([])}\n"
                    f"ports={encode_wire_list(['clk'])}\n"
                    f"clocks={encode_wire_list(['clk'])}\n"
                    f"generated_clocks={encode_wire_list([])}\n"
                    "clock_report_begin=__VMCP_CLOCK_REPORT_BEGIN__\n"
                    "Clock clk\n"
                    "xdc_begin=__VMCP_XDC_BEGIN__\n"
                    "create_clock -period 10 [get_ports clk]"
                ),
            }
        if "foreach ip [get_ips -quiet *]" in text:
            return {"ok": True, "raw": encode_wire_list([])}
        if "get_files -quiet *.bd" in text:
            return {"ok": True, "raw": encode_wire_list([])}
        if "__vmcp_elaboration_report_begin__" in text:
            return {
                "ok": True,
                "raw": (
                    "status=READY\n"
                    "top=top\n"
                    "part=xc7a35tcpg236-1\n"
                    "raw_begin=__VMCP_ELABORATION_REPORT_BEGIN__\n"
                    "raw_end=__VMCP_ELABORATION_REPORT_END__"
                ),
            }
        if "open_run" in text:
            return {"ok": True, "raw": ""}
        if "get_property cfgbvs" in text or "get_property config_voltage" in text:
            return {"ok": True, "raw": "cfgbvs=VCCO\nconfig_voltage=3.3"}
        if "report_timing_summary" in text:
            body = (
                "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n0.100 0.000 0.100 0.000\n"
                if self.timing_met
                else "WNS(ns) TNS(ns) WHS(ns) THS(ns)\n-0.200 -1.000 0.100 0.000\n"
            )
            return {
                "ok": True,
                "raw": attest_report_text(
                    "timing_summary",
                    TIMING_SUMMARY_REPORT_BEGIN_MARKER,
                    body,
                ),
            }
        if "report_drc" in text:
            body = "DRC report: no violations\n" if self.drc_clean else "ERROR: [DRC NSTD-1] DRC violation\n"
            return {"ok": True, "raw": attest_report_text("drc", DRC_REPORT_BEGIN_MARKER, body)}
        if "report_methodology" in text:
            return {
                "ok": True,
                "raw": attest_report_text(
                    "methodology",
                    METHODOLOGY_REPORT_BEGIN_MARKER,
                    "Methodology report: no violations\n",
                ),
            }
        if "report_utilization" in text:
            return {"ok": True, "raw": ""}
        if "report_qor_summary" in text:
            return {"ok": True, "raw": ""}
        if "check_timing" in text:
            return {
                "ok": True,
                "raw": attest_report_text(
                    "check_timing",
                    CHECK_TIMING_REPORT_BEGIN_MARKER,
                    complete_check_timing_body(),
                ),
            }
        if "report_cdc" in text:
            return {"ok": True, "raw": "Safe 1\nUnsafe 0\nUnknown 0\n"}
        if "report_clock_interaction" in text:
            return {"ok": True, "raw": "Safe 1\nUnsafe 0\nUnknown 0\n"}
        if "report_power" in text:
            return {"ok": True, "raw": "Total On-Chip Power (W): 0.1\n"}
        if "vmcp_reports" in text and "report_dir" in text:
            return {
                "ok": True,
                "generation_id": self.generation_id,
                "raw": f"run_name=impl_1\nproject_dir={project_text}\nreport_dir={project_text}/vmcp_reports/impl_1",
            }
        if "xsim.log" in text or "sim_1" in text and "status=" in text:
            log = "INFO: simulation finished" if self.simulation_status == "completed" else f"ERROR: simulation {self.simulation_status}"
            return {
                "ok": True,
                "raw": (
                    f"status={self.simulation_status}\n"
                    f"project_dir_before={project_text}\n"
                    f"project_dir_after={project_text}\n"
                    "project_name_before=demo\n"
                    "project_name_after=demo\n"
                    "simset_before=sim_1\n"
                    "simset_after=sim_1\n"
                    "sim_top_before=tb_top\n"
                    "sim_top_after=tb_top\n"
                    f"source_snapshot_before={encode_wire_list([f'{project_text}/sim/tb_top.sv|100|1'])}\n"
                    f"source_snapshot_after={encode_wire_list([f'{project_text}/sim/tb_top.sv|100|1'])}\n"
                    "status_source=simulation_invocation_log_span\n"
                    "simulation_invocation_id=fake-sim-1\n"
                    "ended_at=2026-06-11T00:00:00Z\n"
                    "log_span_start=0\n"
                    "log_span_end=128\n"
                    "log_span_reset_detected=0\n"
                    "log_begin=__VMCP_LOG_BEGIN__\n"
                    f"{log}"
                ),
            }
        return {"ok": True, "raw": ""}
