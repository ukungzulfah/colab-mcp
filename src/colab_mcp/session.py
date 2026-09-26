# Copyright 2026 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
from collections.abc import AsyncIterator
import contextlib
from contextlib import AsyncExitStack
import json
from fastmcp import FastMCP, Client
from fastmcp.client.transports import ClientTransport
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.tool_injection import ToolInjectionMiddleware
from fastmcp.server.proxy import FastMCPProxy
from fastmcp.tools.tool import Tool, ToolResult
from mcp.client.session import ClientSession
from mcp.types import TextContent
import webbrowser

from colab_mcp.websocket_server import ColabWebSocketServer, COLAB, SCRATCH_PATH

UI_CONNECTION_TIMEOUT = 60.0  # secs

FE_CONNECTED_KEY = "fe_connected"
PROXY_TOKEN_KEY = "proxy_token"
PROXY_PORT_KEY = "proxy_port"
INJECTED_TOOL_NAME = "open_colab_browser_connection"

GET_OUTPUT_CELL_TOOL_NAME = "get_output_cell"
NOT_CONNECTED_MSG = (
    "Not connected to a Colab session. Call 'open_colab_browser_connection' first."
)
NO_CELL_MSG = "Provide either cellId (non-empty) or cellIndex (>= 0)."


class ColabTransport(ClientTransport):
    def __init__(self, wss: ColabWebSocketServer):
        self.wss = wss

    @contextlib.asynccontextmanager
    async def connect_session(self, **session_kwargs) -> AsyncIterator[ClientSession]:
        async with ClientSession(
            self.wss.read_stream, self.wss.write_stream, **session_kwargs
        ) as session:
            yield session

    def __repr__(self) -> str:
        return "<ColabSessionProxyTransport>"


class ColabProxyClient:
    def __init__(self, wss: ColabWebSocketServer):
        self.wss = wss
        self.stubbed_mcp_client = Client(FastMCP())
        self.proxy_mcp_client: Client | None = None
        self._exit_stack = AsyncExitStack()
        self._start_task = None

    def is_connected(self):
        return self.wss.connection_live.is_set() and self.proxy_mcp_client is not None

    async def await_proxy_connection(self):
        with contextlib.suppress(asyncio.TimeoutError):
            # wait for the connection to be live and for the proxy client to fully initialize
            connection_tasks = asyncio.gather(
                self.wss.connection_live.wait(), self._start_task
            )
            await asyncio.wait_for(
                connection_tasks,
                timeout=UI_CONNECTION_TIMEOUT,
            )

    def client_factory(self):
        if self.is_connected():
            return self.proxy_mcp_client
        # return a client mapped to a stubbed mcp server if there is no session proxy
        return self.stubbed_mcp_client

    async def _start_proxy_client(self):
        # blocks until a websocket connection is made successfully
        self.proxy_mcp_client = await self._exit_stack.enter_async_context(
            Client(ColabTransport(self.wss))
        )

    async def __aenter__(self):
        self._start_task = asyncio.create_task(self._start_proxy_client())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._start_task:
            self._start_task.cancel()
        await self._exit_stack.aclose()


class ColabProxyMiddleware(Middleware):
    def __init__(self, proxy_client: ColabProxyClient):
        self.proxy_client = proxy_client
        self.last_message_connected = self.proxy_client.is_connected()

    async def on_message(self, context: MiddlewareContext, call_next):
        """
        Check for a change to Colab session connectivity on any communication with this MCP server and
        notify the client when the connectivity status has changed.
        """
        context.fastmcp_context.set_state(
            FE_CONNECTED_KEY, self.proxy_client.is_connected()
        )
        context.fastmcp_context.set_state(PROXY_TOKEN_KEY, self.proxy_client.wss.token)
        context.fastmcp_context.set_state(PROXY_PORT_KEY, self.proxy_client.wss.port)

        result = await call_next(context)

        connected = self.proxy_client.is_connected()
        connection_state_changed = connected != self.last_message_connected
        self.last_message_connected = connected
        if connection_state_changed:
            await context.fastmcp_context.send_tool_list_changed()

        return result

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        if context.message.name != INJECTED_TOOL_NAME:
            return result
        if self.proxy_client.is_connected():
            return result
        # if the tool call was for open_colab_browser_connection and there is no existing connection, try to await full connection
        await context.fastmcp_context.report_progress(
            progress=1, total=3, message="The user is not connected to the Colab UI"
        )
        await context.fastmcp_context.report_progress(
            progress=2,
            total=3,
            message="Waiting for user to connect in Colab - will wait for 60s",
        )
        await self.proxy_client.await_proxy_connection()
        if self.proxy_client.is_connected():
            await context.fastmcp_context.report_progress(
                progress=3, total=3, message="The Colab UI is successfully connected!"
            )
            return ToolResult(
                content=[TextContent(type="text", text="true")],
                structured_content={"result": True},
            )
        else:
            await context.fastmcp_context.report_progress(
                progress=3,
                total=3,
                message="Timeout while waiting for the user to connect.",
            )
            return ToolResult(
                content=[TextContent(type="text", text="false")],
                structured_content={"result": False},
            )


async def check_session_proxy_tool_fn(ctx: Context = CurrentContext()) -> bool:
    fe_connected = ctx.get_state(FE_CONNECTED_KEY)
    token = ctx.get_state(PROXY_TOKEN_KEY)
    port = ctx.get_state(PROXY_PORT_KEY)
    if fe_connected:
        return True
    webbrowser.open_new(
        f"{COLAB}{SCRATCH_PATH}#mcpProxyToken={token}&mcpProxyPort={port}"
    )
    return False


check_session_proxy_tool = Tool.from_function(
    fn=check_session_proxy_tool_fn,
    name=INJECTED_TOOL_NAME,
    description="Opens a connection to a Google Colab browser session and unlocks notebook editing tools. Returns a boolean representing whether the connection attempt succeeded",
)


def _as_dict(value) -> dict | None:
    """Coerce a value that may be a dict or a JSON string into a dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _extract_result_json(result: ToolResult) -> dict:
    """Extract the JSON payload returned by a proxied Colab frontend tool.

    The Colab frontend returns its result as a JSON string; depending on the
    transport it surfaces as ``data`` (a JSON string), wrapped in
    ``structured_content`` (``{"result": "<json>"}``), or as the first text
    content block. Any of these shapes is handled.
    """
    for candidate in (
        getattr(result, "data", None),
        (getattr(result, "structured_content", None) or {}).get("result")
        if isinstance(getattr(result, "structured_content", None), dict)
        else None,
        getattr(result, "structured_content", None),
    ):
        parsed = _as_dict(candidate)
        if parsed is not None and parsed:
            return parsed

    for item in getattr(result, "content", None) or []:
        parsed = _as_dict(getattr(item, "text", None))
        if parsed is not None and parsed:
            return parsed
    return {}


def make_get_output_cell_fn(proxy_client: ColabProxyClient):
    """Build the ``get_output_cell`` tool fn, closing over the proxy client.

    The tool reads execution outputs of an existing cell WITHOUT re-running it,
    by delegating to the Colab frontend's ``get_cells`` tool with
    ``includeOutputs=True`` and filtering the result locally. This is needed
    because the frontend ``get_cells`` schema is index-range based and does not
    accept a ``cellId``.
    """

    async def get_output_cell_fn(cellId: str = "", cellIndex: int = -1) -> str:
        if not proxy_client.is_connected() or proxy_client.proxy_mcp_client is None:
            return NOT_CONNECTED_MSG
        if not cellId and cellIndex < 0:
            return NO_CELL_MSG

        result = await proxy_client.proxy_mcp_client.call_tool(
            "get_cells", {"includeOutputs": True}
        )
        payload = _extract_result_json(result)
        cells = payload.get("cells", [])
        if not isinstance(cells, list):
            cells = []

        target = None
        if cellId:
            target = next((c for c in cells if c.get("id") == cellId), None)
            if target is None:
                return f"Cell with id '{cellId}' was not found in the notebook."
        else:
            if cellIndex >= len(cells):
                return (
                    f"Cell index {cellIndex} is out of range: the notebook has "
                    f"{len(cells)} cell(s)."
                )
            target = cells[cellIndex]

        outputs = target.get("outputs") or []
        return json.dumps(
            {
                "cellId": target.get("id"),
                "cell_type": target.get("cell_type"),
                "outputs": outputs,
            },
            indent=2,
        )

    return get_output_cell_fn


def make_get_output_cell_tool(proxy_client: ColabProxyClient) -> Tool:
    return Tool.from_function(
        fn=make_get_output_cell_fn(proxy_client),
        name=GET_OUTPUT_CELL_TOOL_NAME,
        description=(
            "Returns the execution outputs (stdout, results, errors) of an "
            "existing Colab notebook cell WITHOUT re-running it. "
            "Identify the cell by 'cellId' (preferred) or by 'cellIndex' "
            "(0-based). Use this to read results after run_code_cell returns, "
            "including output produced after a long-running cell was cut off."
        ),
    )


class ColabSessionProxy:
    def __init__(self):
        self._exit_stack = AsyncExitStack()
        self.proxy_server: FastMCPProxy | None = None
        # list order matters, see: https://gofastmcp.com/servers/middleware#multiple-middleware
        self.middleware: list[Middleware] = []
        self.wss: ColabWebSocketServer | None = None

    async def start_proxy_server(self):
        self.wss = await self._exit_stack.enter_async_context(ColabWebSocketServer())
        proxy_client = await self._exit_stack.enter_async_context(
            ColabProxyClient(self.wss)
        )
        self.proxy_server = FastMCPProxy(
            client_factory=proxy_client.client_factory,
            instructions="Connects to a user's Google Colab session in a browser and allows for interactions with their Google Colab notebook",
        )
        # ColabProxyMiddleware must be first because it sets the fe_connected state
        self.middleware.append(ColabProxyMiddleware(proxy_client))
        self.middleware.append(
            ToolInjectionMiddleware(
                tools=[
                    check_session_proxy_tool,
                    make_get_output_cell_tool(proxy_client),
                ]
            )
        )

    async def cleanup(self):
        await self._exit_stack.aclose()
