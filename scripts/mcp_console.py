"""JSON-lines client for interactive testing of a fresh stdio MCP server.

Each line is {"tool": "observe", "arguments": {}}. No automatic input retries.
EOF closes the client and server, releasing the device lease.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters


async def main():
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "mobile_use.server"], cwd=root,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    # A declared navigation + timing + return navigation may exceed one 30s budget.
    async with Client(params, read_timeout_seconds=120) as client:
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                request = json.loads(line)
                result = await client.call_tool(request["tool"], request.get("arguments", {}))
                print(result.model_dump_json(by_alias=True), flush=True)
            except Exception as exc:
                print(json.dumps({"isError": True, "error": str(exc)}), flush=True)


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
