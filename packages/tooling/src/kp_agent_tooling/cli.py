#!/usr/bin/env python3
"""Configured agent tools: CLI JSON and MCP stdio use the same read-only adapter."""
import argparse
import asyncio
import json
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling

async def serve(adapter):
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent, CallToolResult
    server=Server('ops-agent-tooling')
    lock=asyncio.Lock()
    @server.list_tools()
    async def tools(): return [Tool(**t) for t in await asyncio.to_thread(adapter.tools)]
    @server.call_tool(validate_input=False)
    async def call(name,arguments):
        async with lock:
            try: result=await asyncio.to_thread(adapter.call,name,arguments)
            except Exception as error:result={'status':'error','reason':type(error).__name__}
            if name != 'delivery.read':result=adapter.delivery.deliver(result)
        # Some MCP hosts expose only standard text content. DeliveryStore has
        # already bounded the payload; use its exact JSON encoding in both fields.
        return CallToolResult(content=[TextContent(type='text',text=adapter.delivery.encode(result).decode('ascii'))],
                              structuredContent=result, isError=result.get('status') == 'error')
    async with stdio_server() as (read,write):
        await server.run(read,write,server.create_initialization_options())

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('action',choices=['doctor','tools','call','serve'])
    parser.add_argument('--tool');parser.add_argument('--arguments',default='{}')
    args=parser.parse_args();adapter=AgentTooling(args.config)
    if args.action=='serve':asyncio.run(serve(adapter));return
    if args.action=='doctor':result=adapter.doctor()
    elif args.action=='tools':result=adapter.tools()
    else:result=adapter.call(args.tool,json.loads(args.arguments))
    failed=isinstance(result,dict) and result.get('status') in {'error','unavailable','invalid'}
    if not (args.action == 'call' and args.tool == 'delivery.read'):
        result=adapter.delivery.deliver(result)
    print(json.dumps(result))
    if failed or (isinstance(result,dict) and result.get('status')=='error'):raise SystemExit(1)
if __name__=='__main__':main()
