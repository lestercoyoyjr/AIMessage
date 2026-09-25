"""Agent A: write a federated memory through Centralaizer's MCP server (streamable-http).

This is the real agent path — a genuine MCP `call_tool("memory_write", ...)` over the same
transport Claude Desktop / Cursor use — not a shortcut through the HTTP API. Run it with a Python
that has fastmcp (Centralaizer's venv); AIMessage's own venv doesn't ship fastmcp.

    python demo/_agent_write.py http://127.0.0.1:3020/mcp "the memory content"

Prints the tool's JSON result ({"status": "stored", "id": ...}).
"""
import asyncio
import json
import sys

from fastmcp import Client


async def main() -> None:
    url, content = sys.argv[1], sys.argv[2]
    async with Client(url) as c:
        r = await c.call_tool("memory_write",
                              {"agent_id": "agent-A", "content": content, "owner": "shared"})
    # CallToolResult shape varies by fastmcp version — try the structured paths, then text.
    data = getattr(r, "data", None)
    if data is None:
        data = getattr(r, "structured_content", None)
    if data is None:
        blocks = getattr(r, "content", None) or []
        data = getattr(blocks[0], "text", "{}") if blocks else "{}"
    if isinstance(data, str):
        data = json.loads(data)
    print(json.dumps(data))


if __name__ == "__main__":
    asyncio.run(main())
