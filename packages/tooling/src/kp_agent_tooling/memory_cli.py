#!/usr/bin/env python3
"""Session-bound episodic memory CLI and native MCP stdio server."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.episodic_memory_tools import from_config, tool_failure


class AdmissionAwareMemory:
    """Stable discovery; recreate the session adapter per call so repairs take effect."""
    def __init__(self, config):
        self.config = config

    def tools(self):
        from kp_agent_tooling._impl.service.episodic_memory_tools import TOOLS
        return TOOLS

    def call(self, name, arguments):
        import jsonschema
        schema = next((t['inputSchema'] for t in self.tools() if t['name'] == name), None)
        if schema is None:
            raise ValueError('unknown memory operation')
        jsonschema.validate(arguments, schema)
        try:
            adapter = from_config(self.config)
        except Exception:
            if name == 'memory.connection_status':
                return {'status':'not_ready', 'category':'portable_memory_setup_required',
                        'authorization_changed':False, 'guidance':'Operator must repair this exact session admission; tool discovery does not grant access.'}
            raise
        try:
            return adapter.call(name, arguments)
        finally:
            adapter.close()

    def close(self):
        pass


async def serve(adapter):
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import CallToolResult, TextContent, Tool

    server = Server("ops-episodic-memory")

    @server.list_tools()
    async def list_tools():
        return [Tool(**tool) for tool in adapter.tools()]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            result = await asyncio.to_thread(adapter.call, name, arguments)
            failed = False
        except Exception as error:
            result = tool_failure(name, error)
            failed = True
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=True))],
            structuredContent=result, isError=failed,
        )

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("action", choices=("doctor", "tools", "call", "serve"))
    parser.add_argument("--tool")
    parser.add_argument("--arguments", default="{}")
    args = parser.parse_args()
    try:
        adapter = AdmissionAwareMemory(args.config) if args.action in ("serve", "tools") else from_config(args.config)
    except Exception as error:
        print(json.dumps({"status": "error", "category": (leaf.STORE_REFUSAL if isinstance(error, leaf.StoreOutsideVolume)
                                                         else "memory_unavailable"),
                          "guidance": "Ask the operator to check memory tool configuration and service health."}))
        raise SystemExit(1)
    try:
        if args.action == "serve":
            asyncio.run(serve(adapter))
            return
        if args.action == "doctor":
            result = {"status": "ready", "tool_count": len(adapter.tools()),
                      "admission": "operator-configured session resolved against the current desk authority"}
        elif args.action == "tools":
            result = adapter.tools()
        else:
            if not args.tool:
                parser.error("--tool required for call")
            try:
                result = adapter.call(args.tool, json.loads(args.arguments))
            except Exception as error:
                result = tool_failure(args.tool, error)
        print(json.dumps(result, ensure_ascii=True))
        if isinstance(result, dict) and result.get("status") == "error":
            raise SystemExit(1)
    finally:
        adapter.close()


if __name__ == "__main__":
    main()
