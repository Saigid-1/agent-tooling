#!/usr/bin/env python3
"""Model-gateway CLI and stdio MCP server (``agent-tooling.model-gateway.v1``).

    kp-agent-models --config <path> [--memory-config <path>] serve
    kp-agent-models --config <path> doctor [--live]
    kp-agent-models --config <path> tools
    kp-agent-models --config <path> call --tool model.<capability> --arguments '<json>'

``serve`` exposes one ``model.<capability>`` tool per configured route. The
configuration is re-read for every listing and call. ``doctor`` never sends a
request without ``--live``; with ``--live`` it sends only unpaid model-metadata
GET requests. ``--memory-config`` (or the configuration's ``memory_config``)
names the bound session's desk-memory configuration; successful calls are then
recorded against that desk.
"""

import argparse
import asyncio
import json
import sys

from kp_agent_tooling._impl.service.model_gateway import GatewayConfigError, ModelGateway


def _emit(value):
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))


async def serve(gateway):
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import CallToolResult, TextContent, Tool

    server = Server('kp-agent-models')

    @server.list_tools()
    async def list_tools():
        try:
            tools = await asyncio.to_thread(gateway.tools)
        except GatewayConfigError:
            # A configuration broken after start lists nothing rather than stale routes.
            tools = []
        return [Tool(**tool) for tool in tools]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        result = await asyncio.to_thread(gateway.call, name, arguments)
        return CallToolResult(
            content=[TextContent(type='text', text=json.dumps(result, ensure_ascii=True))],
            structuredContent=result, isError=result.get('status') != 'ok')

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv=None):
    parser = argparse.ArgumentParser(prog='kp-agent-models', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', required=True, help='agent-tooling.model-gateway.v1 configuration file')
    parser.add_argument('--memory-config', help='bound session desk-memory configuration (optional)')
    parser.add_argument('action', choices=('serve', 'doctor', 'tools', 'call'))
    parser.add_argument('--live', action='store_true',
                        help='doctor only: also send unpaid model-metadata GET requests')
    parser.add_argument('--tool')
    parser.add_argument('--arguments', default='{}')
    args = parser.parse_args(argv)
    gateway = ModelGateway(args.config, memory_config=args.memory_config)

    if args.action == 'doctor':
        report = gateway.doctor(live=args.live)
        _emit(report)
        raise SystemExit(0 if report.get('status') == 'ready' else 1)
    try:
        gateway.load()
    except GatewayConfigError as error:
        _emit({'status': 'error', 'category': 'configuration_invalid', 'detail': str(error)})
        raise SystemExit(2)
    if args.action == 'serve':
        asyncio.run(serve(gateway))
        return
    if args.action == 'tools':
        _emit(gateway.tools())
        return
    if not args.tool:
        parser.error('--tool is required for call')
    try:
        arguments = json.loads(args.arguments)
    except ValueError:
        parser.error('--arguments must be JSON')
    result = gateway.call(args.tool, arguments)
    _emit(result)
    raise SystemExit(0 if result.get('status') == 'ok' else 1)


if __name__ == '__main__':
    main(sys.argv[1:])
