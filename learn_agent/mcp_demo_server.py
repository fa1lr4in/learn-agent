"""A separate MCP process exposing one arithmetic tool over stdio."""

import os
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field


server = MCPServer("learn-agent-demo")


@server.tool()
def sum_numbers(values: Annotated[list[int], Field(max_length=100)]) -> dict[str, int]:
    """对非空整数列表求和，返回 count、total 和实际服务进程号 server_pid。"""
    if not values:
        raise ToolError("values 不能为空，请提供至少一个整数。")
    return {"count": len(values), "total": sum(values), "server_pid": os.getpid()}


if __name__ == "__main__":
    server.run(transport="stdio")
