"""A text-only MCP client must receive the same bounded result as a structured client."""
import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from kp_agent_tooling._impl.tool_delivery import DeliveryStore


def test_standard_text_matches_structured_content_for_inline_and_continued(tmp_path):
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    # 60 000 bytes: above the 48 000-byte inline ceiling (step 3, 2026-09-21), so the
    # delivery is genuinely continued. 20 000 has been inline since that change.
    (evidence / 'large.json').write_text(json.dumps({'value': 'x' * 60_000}))
    subprocess.run(['git', 'init', '-q', str(evidence)], check=True)
    subprocess.run(['git', '-C', str(evidence), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(evidence), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    revision = subprocess.check_output(['git', '-C', str(evidence), 'rev-parse', 'HEAD'],
                                       text=True).strip()
    config = tmp_path / 'tooling.json'
    config.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1', 'repos': {},
                                  'evidence_repo': str(evidence), 'evidence_revision': revision,
                                  'evidence': {'large': 'large.json'},
                                  'enabled_tools': ['tooling.identity', 'verification.finding', 'lifecycle.evidence',
                                                    'delivery.read']}))
    script = Path(__file__).resolve().parents[3] / 'packages/tooling/src/kp_agent_tooling/cli.py'

    async def check():
        params = StdioServerParameters(command=sys.executable,
            args=[str(script), '--config', str(config), 'serve'])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
                await session.initialize()
                async def same(name, arguments, *, is_error=False):
                    reply = await session.call_tool(name, arguments)
                    assert reply.isError == is_error
                    assert len(reply.content) == 1 and reply.content[0].type == 'text'
                    assert json.loads(reply.content[0].text) == reply.structuredContent
                    assert reply.content[0].text == DeliveryStore.encode(reply.structuredContent).decode('ascii')
                    return reply.structuredContent

                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert tools['tooling.identity'].inputSchema == {
                    'type': 'object', 'properties': {}, 'additionalProperties': False}
                native_identity = await same('tooling.identity', {})
                assert native_identity['schema_version'] == 'ops.agent-tooling-identity.v1'
                assert native_identity['config_sha256'] == hashlib.sha256(config.read_bytes()).hexdigest()
                assert native_identity['executable_artifact_integrity'] == 'not-attested'
                assert 'config_path' not in native_identity and 'gateway_config' not in native_identity
                inline = await same('verification.finding', {'mode': 'validate', 'finding': {}})
                assert inline['status'] == 'invalid'
                invalid_arguments = await same('verification.finding', {'mode': 'unsupported'}, is_error=True)
                assert invalid_arguments == {'status': 'error', 'reason': 'ValidationError'}
                continued = await same('lifecycle.evidence', {'evidence_id': 'large'})
                assert continued['status'] == 'continued'
                page = await same('delivery.read', continued['next_call']['arguments'])
                assert page['sha256'] == continued['sha256']
                assert page['offset'] == 0 and page['bytes'] > 0

    asyncio.run(check())
