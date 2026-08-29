from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .registry import profile_tool_names, resolve_tool_profile, tool_names
from .vivado.env import capture_server_vivado_identity, find_vivado, resolve_runtime_dir


SUPPORTED_PYTHON_MINORS = ((3, 11), (3, 12))
VALIDATED_VIVADO_VERSIONS = ("2021.2",)
DEFAULT_PROBE_TIMEOUT_S = 60


def build_doctor_report(
    *,
    vivado_path: str | None = None,
    runtime_dir: str | None = None,
    workspace: str | None = None,
    probe_launch: bool = True,
    probe_timeout_s: int = DEFAULT_PROBE_TIMEOUT_S,
) -> dict[str, Any]:
    workspace_path = Path(workspace or Path.cwd()).resolve()
    runtime_path = resolve_runtime_dir(runtime_dir, cwd=workspace_path)
    checks: list[dict[str, Any]] = []
    tool_profile = resolve_tool_profile()
    exposed_tools = profile_tool_names(tool_profile)
    registry_tools = tool_names()

    _add_python_check(checks)
    _add_runtime_check(checks, runtime_path)

    trusted_vivado_identity = capture_server_vivado_identity()
    vivado = find_vivado(
        vivado_path,
        probe_launch=probe_launch,
        probe_timeout_s=probe_timeout_s,
        runtime_dir=str(runtime_path),
        trusted_identity=trusted_vivado_identity,
    )
    _add_vivado_checks(checks, vivado, probe_launch=probe_launch)
    _add_mcp_check(checks, tool_profile=tool_profile, exposed_tools=exposed_tools, registry_tools=registry_tools)

    status = _overall_status(checks)
    return {
        "ok": status != "BLOCK",
        "status": status,
        "summary": _summary_for_status(status),
        "package": {
            "name": "vivado-agent-mcp",
            "version": __version__,
        },
        "workspace": str(workspace_path),
        "runtime_dir": str(runtime_path),
        "probe_timeout_s": probe_timeout_s,
        "version_policy": {
            "requires_exact_vivado_version": True,
            "validated_versions": list(VALIDATED_VIVADO_VERSIONS),
            "detected_version": vivado.get("probed_version"),
            "path_hint_version": vivado.get("path_hint_version"),
            "probed_version": vivado.get("probed_version"),
            "version_attested": bool(vivado.get("version_attested")),
            "message": (
                "Trusted Project Mode execution currently requires Vivado 2021.2; other versions are blocked until separately qualified."
            ),
        },
        "vivado": vivado,
        "mcp": {
            "transport": "stdio",
            "tool_profile": tool_profile,
            "tool_count": len(exposed_tools),
            "exposed_tool_count": len(exposed_tools),
            "registry_tool_count": len(registry_tools),
            "entrypoint": "vivado-agent-mcp",
        },
        "checks": checks,
        "next_steps": _next_steps(checks),
        "hardware_validation": {
            "status": "NOT_VALIDATED",
            "validated": False,
            "message": "Doctor does not validate real FPGA hardware, JTAG, programming, ILA, or VIO.",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vivado-agent-mcp doctor",
        description="Check whether this machine is ready to run vivado-agent-mcp.",
    )
    parser.add_argument(
        "--vivado-path",
        help="Optional redundant path assertion; it must match the server-start VIVADO_PATH canonical identity.",
    )
    parser.add_argument("--runtime-dir", help="Runtime directory used for Vivado/MCP temporary files.")
    parser.add_argument("--workspace", default=".", help="Workspace root used to resolve the default runtime directory.")
    parser.add_argument("--probe-timeout-s", type=int, default=DEFAULT_PROBE_TIMEOUT_S, help="Timeout for vivado -mode batch -version.")
    parser.add_argument("--no-probe-launch", action="store_true", help="Skip the bounded Vivado batch startup probe.")
    parser.add_argument("--json", action="store_true", help="Print the full machine-readable report as JSON.")
    args = parser.parse_args(argv)

    report = build_doctor_report(
        vivado_path=args.vivado_path,
        runtime_dir=args.runtime_dir,
        workspace=args.workspace,
        probe_launch=not args.no_probe_launch,
        probe_timeout_s=max(1, int(args.probe_timeout_s)),
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human_report(report)
    return 0 if report["status"] != "BLOCK" else 2


def _add_python_check(checks: list[dict[str, Any]]) -> None:
    current = sys.version_info
    current_minor = (current.major, current.minor)
    supported = current_minor in SUPPORTED_PYTHON_MINORS
    supported_text = "3.11 or 3.12"
    checks.append(
        {
            "id": "python",
            "status": "PASS" if supported else "BLOCK",
            "message": (
                f"Python {current.major}.{current.minor}.{current.micro} is supported."
                if supported
                else f"Python {current.major}.{current.minor}.{current.micro} is unsupported; Python {supported_text} is required."
            ),
            "data": {
                "executable": sys.executable,
                "version": f"{current.major}.{current.minor}.{current.micro}",
                "requires": ">=3.11,<3.13",
                "supported_minors": [f"{major}.{minor}" for major, minor in SUPPORTED_PYTHON_MINORS],
            },
        }
    )


def _add_runtime_check(checks: list[dict[str, Any]], runtime_path: Path) -> None:
    try:
        runtime_path.mkdir(parents=True, exist_ok=True)
        marker = runtime_path / ".doctor_write_test"
        marker.write_text("ok", encoding="utf-8")
        marker.unlink(missing_ok=True)
    except OSError as exc:
        checks.append(
            {
                "id": "runtime_dir",
                "status": "BLOCK",
                "message": f"Runtime directory is not writable: {exc}",
                "data": {"runtime_dir": str(runtime_path), "error": str(exc)},
            }
        )
        return
    checks.append(
        {
            "id": "runtime_dir",
            "status": "PASS",
            "message": "Runtime directory is writable.",
            "data": {"runtime_dir": str(runtime_path)},
        }
    )


def _add_vivado_checks(checks: list[dict[str, Any]], vivado: dict[str, Any], *, probe_launch: bool) -> None:
    if not vivado.get("ok"):
        checks.append(
            {
                "id": "vivado_path",
                "status": "BLOCK",
                "message": "Trusted Vivado executable is unavailable. Set VIVADO_PATH before starting the command.",
                "data": {"searched": vivado.get("searched", [])},
            }
        )
        return

    checks.append(
        {
            "id": "vivado_path",
            "status": "PASS",
            "message": f"Vivado executable found: {vivado.get('path')}",
            "data": {"path": vivado.get("path"), "source": vivado.get("source")},
        }
    )

    path_hint_version = vivado.get("path_hint_version")
    probed_version = vivado.get("probed_version")
    version_attested = bool(vivado.get("version_attested"))
    if version_attested and probed_version in VALIDATED_VIVADO_VERSIONS:
        version_status = "PASS"
        version_message = f"Vivado {probed_version} was attested by the launch probe and matches the validated target."
    elif version_attested and probed_version:
        version_status = "BLOCK"
        version_message = f"Vivado {probed_version} is not supported by the current trusted execution contract; Vivado 2021.2 is required."
    else:
        version_status = "BLOCK"
        version_message = "Vivado version was not attested from actual probe output; trusted execution requires a successful Vivado 2021.2 probe."
    checks.append(
        {
            "id": "vivado_version",
            "status": version_status,
            "message": version_message,
            "data": {
                "detected_version": probed_version,
                "path_hint_version": path_hint_version,
                "probed_version": probed_version,
                "version_attested": version_attested,
                "validated_versions": list(VALIDATED_VIVADO_VERSIONS),
            },
        }
    )

    if vivado.get("xsim_available"):
        checks.append(
            {
                "id": "xsim_tools",
                "status": "PASS",
                "message": "Vivado XSIM companion tools are available.",
                "data": {"tools": vivado.get("tools", {})},
            }
        )
    else:
        checks.append(
            {
                "id": "xsim_tools",
                "status": "WARN",
                "message": "One or more XSIM companion tools were not found; behavioral simulation may fail.",
                "data": {"tools": vivado.get("tools", {})},
            }
        )

    launch_probe = vivado.get("launch_probe", {})
    if not probe_launch:
        checks.append(
            {
                "id": "vivado_launch_probe",
                "status": "WARN",
                "message": "Vivado launch probe was skipped; path exists but startup was not verified.",
                "data": launch_probe,
            }
        )
    elif vivado.get("launch_ready"):
        checks.append(
            {
                "id": "vivado_launch_probe",
                "status": "PASS",
                "message": "Vivado batch startup probe completed with an attested version.",
                "data": launch_probe,
            }
        )
    else:
        diagnosis = launch_probe.get("diagnosis", {})
        checks.append(
            {
                "id": "vivado_launch_probe",
                "status": "BLOCK",
                "message": str(diagnosis.get("message") or "Vivado batch startup probe did not establish a trusted environment."),
                "data": launch_probe,
            }
        )


def _add_mcp_check(
    checks: list[dict[str, Any]],
    *,
    tool_profile: str,
    exposed_tools: list[str],
    registry_tools: list[str],
) -> None:
    checks.append(
        {
            "id": "mcp_tool_registry",
            "status": "PASS" if exposed_tools and registry_tools else "BLOCK",
            "message": (
                f"MCP {tool_profile} profile exposes {len(exposed_tools)} of {len(registry_tools)} registered tools."
            ),
            "data": {
                "tool_profile": tool_profile,
                "tool_count": len(exposed_tools),
                "exposed_tool_count": len(exposed_tools),
                "registry_tool_count": len(registry_tools),
                "transport": "stdio",
            },
        }
    )


def _overall_status(checks: list[dict[str, Any]]) -> str:
    statuses = {check["status"] for check in checks}
    if "BLOCK" in statuses:
        return "BLOCK"
    if "WARN" in statuses:
        return "WARN"
    return "READY"


def _summary_for_status(status: str) -> str:
    if status == "READY":
        return "Environment preflight passed for vivado-agent-mcp; run stdio regression or a small Project Mode smoke before treating workflows as validated."
    if status == "WARN":
        return "Machine can probably run vivado-agent-mcp, but at least one item should be reviewed."
    return "Machine is not ready for vivado-agent-mcp until BLOCK items are fixed."


def _next_steps(checks: list[dict[str, Any]]) -> list[str]:
    steps: list[str] = []
    by_id = {check["id"]: check for check in checks}
    if by_id.get("vivado_path", {}).get("status") == "BLOCK":
        steps.append("Install Vivado or set VIVADO_PATH / --vivado-path to the Vivado executable.")
    if by_id.get("python", {}).get("status") == "BLOCK":
        steps.append("Run vivado-agent-mcp with Python 3.11 or 3.12, matching the package requires-python contract.")
    if by_id.get("vivado_launch_probe", {}).get("status") == "BLOCK":
        steps.append(
            "Retry vivado-agent-mcp doctor --probe-timeout-s 120 --json, or call detect_vivado_environment(probe_launch=true); inspect stdout_tail/stderr_tail."
        )
    if by_id.get("runtime_dir", {}).get("status") == "BLOCK":
        steps.append("Choose a writable --runtime-dir under the workspace.")
    if by_id.get("vivado_version", {}).get("status") == "BLOCK":
        steps.append("Install or select Vivado 2021.2 before using trusted Project Mode execution tools.")
    if by_id.get("xsim_tools", {}).get("status") == "WARN":
        steps.append("Check the Vivado bin directory for xvlog/xelab/xsim before running behavioral simulation workflows.")
    if not steps:
        steps.append("Configure the MCP client to run vivado-agent-mcp, then ask the Agent to call get_tool_catalog and get_agent_workflows.")
    return steps


def _print_human_report(report: dict[str, Any]) -> None:
    print(f"vivado-agent-mcp doctor: {report['status']}")
    print(report["summary"])
    print(f"package={report['package']['name']} {report['package']['version']}")
    print(f"runtime_dir={report['runtime_dir']}")
    print(f"probe_timeout_s={report['probe_timeout_s']}")
    print(f"vivado_path={report['vivado'].get('path')}")
    print(f"vivado_path_hint_version={report['vivado'].get('path_hint_version') or 'unknown'}")
    print(f"vivado_probed_version={report['vivado'].get('probed_version') or 'unknown'}")
    print(f"vivado_version_attested={str(bool(report['vivado'].get('version_attested'))).lower()}")
    print("version_policy=Trusted Project Mode execution requires Vivado 2021.2.")
    print("")
    for check in report["checks"]:
        print(f"[{check['status']}] {check['id']}: {check['message']}")
    print("")
    print("next_steps:")
    for step in report["next_steps"]:
        print(f"- {step}")
    print("")
    print("hardware_validation=NOT_VALIDATED")


if __name__ == "__main__":
    raise SystemExit(main())
