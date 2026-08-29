from __future__ import annotations

import asyncio
import sys

from . import __version__
from .doctor import main as doctor_main
from .server import run_stdio_server
from .selftest import main as selftest_main


def _print_help() -> None:
    print(
        "\n".join(
            [
                "vivado-agent-mcp: stdio MCP server for no-board Vivado Project Mode workflows.",
                "",
                "usage:",
                "  vivado-agent-mcp                 start stdio MCP server",
                "  vivado-agent-mcp doctor [...]    check local environment",
                "  vivado-agent-mcp selftest [...]  run Agent-facing stdio selftest",
                "  vivado-agent-mcp --version       print package version",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int | None:
    """Console entry point for the stdio MCP server."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-h", "--help"}:
        _print_help()
        return 0
    if args and args[0] in {"-V", "--version"}:
        print(__version__)
        return 0
    if args and args[0] == "doctor":
        return doctor_main(args[1:])
    if args and args[0] == "selftest":
        return selftest_main(args[1:])
    try:
        asyncio.run(run_stdio_server())
    except KeyboardInterrupt:
        return 130
    return None


if __name__ == "__main__":
    code = main()
    if isinstance(code, int):
        sys.exit(code)
