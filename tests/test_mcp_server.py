from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from douyin_knowledge.agent_gateway import AgentGateway
from douyin_knowledge.mcp_server import create_server

EXPECTED_TOOLS = {
    "douyin_capabilities",
    "douyin_doctor",
    "douyin_status",
    "douyin_plan",
    "douyin_prepare_handoff",
    "douyin_get_manifest",
    "douyin_read_text",
    "douyin_open_visual",
    "douyin_assignment_status",
    "douyin_submit_candidate",
    "douyin_cleanup_assignment",
}


def test_mcp_server_registers_host_neutral_tools(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = create_server(
            AgentGateway(tmp_path / "private-instance", tmp_path / "agent-gateway")
        )
        tools = await server.list_tools()
        _content, capabilities = await server.call_tool("douyin_capabilities", {})
        _error_content, invalid_plan = await server.call_tool(
            "douyin_plan", {"limit": 6}
        )

        assert {tool.name for tool in tools} == EXPECTED_TOOLS
        assert capabilities["data"]["mode"] == "candidate-only"
        assert invalid_plan["error"]["code"] == "invalid_limit"

    anyio.run(exercise)


def test_mcp_stdio_negotiates_and_returns_structured_capabilities(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository = Path(__file__).resolve().parents[1]
        source_path = os.pathsep.join([str(repository / "src"), str(repository)])
        environment = {**os.environ, "PYTHONPATH": source_path}
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "douyin_knowledge.mcp_server",
                "--root",
                str(tmp_path / "private-instance"),
                "--workspace",
                str(tmp_path / "agent-gateway"),
            ],
            env=environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("douyin_capabilities", {})

        assert initialized.serverInfo.name == "douyin-knowledge"
        assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["ok"] is True
        assert result.structuredContent["data"]["mode"] == "candidate-only"
        serialized = str(result.structuredContent).casefold()
        assert str(tmp_path).casefold() not in serialized

    anyio.run(exercise)
