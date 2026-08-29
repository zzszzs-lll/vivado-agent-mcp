from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from . import __version__
from .registry import TOOL_DEFS, _output_schema, tool_definitions
from .tools import VivadoToolService


server = Server("vivado-agent-mcp")
service = VivadoToolService(enforce_tool_profile=True)
_LOCAL_CONTROL_TOOLS = {
    "get_tool_catalog",
    "get_agent_workflows",
    "get_agent_scenarios",
    "get_workflow_trace_status",
    "session_status",
}


class _DispatchRequest:
    def __init__(self) -> None:
        self.cancelled = Event()


def _consume_completed_future(future: asyncio.Future[Any]) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except (asyncio.CancelledError, Exception):
        return


class _SerializedToolDispatcher:
    def __init__(self) -> None:
        self._lock = Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._active_request: _DispatchRequest | None = None
        self._cancellation_done = Event()
        self._cancellation_done.set()
        self._shutdown = False

    def _get_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("Vivado tool dispatcher is shut down")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vivado-mcp-backend")
            return self._executor

    def _call_owned(
        self,
        request: _DispatchRequest,
        tool_service: VivadoToolService,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self._cancellation_done.wait()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("Vivado tool dispatcher is shut down")
            if request.cancelled.is_set():
                return {
                    "ok": False,
                    "tool": name,
                    "message": "Request was cancelled before backend execution.",
                    "error_code": "REQUEST_CANCELLED_BEFORE_EXECUTION",
                    "data": {},
                }
            self._active_request = request
        try:
            return tool_service.call(name, arguments)
        finally:
            with self._lock:
                if self._active_request is request:
                    self._active_request = None

    def _cancel_if_active(self, request: _DispatchRequest, tool_service: VivadoToolService) -> None:
        with self._lock:
            owns_cancellation = self._active_request is request and self._cancellation_done.is_set()
            if owns_cancellation:
                self._cancellation_done.clear()
        if not owns_cancellation:
            return
        try:
            tool_service.cancel_active_operation()
        finally:
            self._cancellation_done.set()

    def _cancel_for_shutdown(self, tool_service: VivadoToolService) -> None:
        while True:
            self._cancellation_done.wait()
            with self._lock:
                if not self._cancellation_done.is_set():
                    continue
                self._cancellation_done.clear()
                break
        try:
            tool_service.cancel_active_operation()
        finally:
            self._cancellation_done.set()

    async def call(self, tool_service: VivadoToolService, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name in _LOCAL_CONTROL_TOOLS:
            return tool_service.call(name, arguments)
        loop = asyncio.get_running_loop()
        request = _DispatchRequest()
        future = loop.run_in_executor(self._get_executor(), self._call_owned, request, tool_service, name, arguments)
        try:
            return await future
        except asyncio.CancelledError:
            request.cancelled.set()
            future.cancel()
            future.add_done_callback(_consume_completed_future)
            with self._lock:
                owns_active_request = self._active_request is request
            if owns_active_request:
                await asyncio.to_thread(self._cancel_if_active, request, tool_service)
            raise

    async def shutdown(self, tool_service: VivadoToolService) -> None:
        with self._lock:
            self._shutdown = True
            executor = self._executor
            self._executor = None
        if executor is not None:
            await asyncio.to_thread(executor.shutdown, False, cancel_futures=True)
        await asyncio.to_thread(self._cancel_for_shutdown, tool_service)
        if executor is not None:
            await asyncio.to_thread(executor.shutdown, True, cancel_futures=True)


_dispatcher = _SerializedToolDispatcher()


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return tool_definitions(service.tool_names())


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
    result = await _dispatcher.call(service, name, arguments or {})
    return _to_call_tool_result(result)


def _to_call_tool_result(result: dict[str, Any]) -> types.CallToolResult:
    ok = bool(result.get("ok"))
    text = result.get("summary") if ok else result.get("message")
    if not text:
        text = f"{result.get('tool', 'tool')} {'completed' if ok else 'failed'}."
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=str(text))],
        structuredContent=result,
        isError=not ok,
    )


async def run_stdio_server() -> None:
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="vivado-agent-mcp",
                    server_version=__version__,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        await _dispatcher.shutdown(service)


def run() -> None:
    asyncio.run(run_stdio_server())
