"""Gateway catalog advertisement is not backend readiness or semantic absence."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import sys

import anyio
from mcp import ClientSession
import pytest

from kp_agent_tooling_ops._impl.service import gateway_transport
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
from kp_agent_tooling import cli as agent_tooling_cli


def _adapter(tmp_path):
    gateway = tmp_path / 'gateway.toml'
    gateway.write_text('[mcp_servers.ops-gateway]\nurl="http://127.0.0.1:8400/api/mcp"\nhttp_headers={}\n')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1',
                                  'gateway_config': str(gateway), 'repos': {}}))
    adapter = AgentTooling(config)
    adapter.catalog = {'knowledge.context': {'name': 'knowledge.context',
        'description': 'Maintained guidance.', 'inputSchema': {'type': 'object',
        'properties': {'query': {'type': 'string'}}, 'required': ['query'],
        'additionalProperties': False}}}
    return adapter


class _Response:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def read(self, limit):
        return self.body[:limit]


class _Connection:
    response = None
    error = None
    requests = 0

    def __init__(self, *args, **kwargs):
        pass

    def request(self, method, path, body, headers):
        type(self).requests += 1
        if type(self).error:
            raise type(self).error('secret transport detail')

    def getresponse(self):
        return type(self).response

    def close(self):
        pass


@pytest.mark.parametrize(('status', 'reason', 'category', 'retryable'), [
    (400, 'gateway_request_rejected', 'request', False),
    (401, 'gateway_auth_failed', 'auth', False),
    (503, 'gateway_service_unavailable', 'unavailable', True),
    (504, 'gateway_timeout', 'timeout', True),
])
def test_http_failure_has_fixed_code_and_does_not_echo_remote_body(
        monkeypatch, status, reason, category, retryable):
    monkeypatch.setattr(gateway_transport.http.client, 'HTTPConnection', _Connection)
    _Connection.response = _Response(status, b'{"error":"secret remote detail"}')
    _Connection.error = None
    with pytest.raises(gateway_transport.GatewayFailure) as caught:
        gateway_transport.rpc('tools/call', {'name': 'knowledge.context'}, {})
    result = caught.value.result()
    assert (result['reason'], result['category'], result['retryable']) == (
        reason, category, retryable)
    assert result['evidence_status'] == 'not_obtained'
    assert 'secret' not in json.dumps(result)


@pytest.mark.parametrize(('exception', 'reason'), [
    (TimeoutError, 'gateway_timeout'),
    (PermissionError, 'gateway_unavailable'),
])
def test_transport_exception_is_bounded(monkeypatch, exception, reason):
    monkeypatch.setattr(gateway_transport.http.client, 'HTTPConnection', _Connection)
    _Connection.error = exception
    with pytest.raises(gateway_transport.GatewayFailure) as caught:
        gateway_transport.rpc('tools/list', {}, {})
    assert caught.value.reason == reason
    assert 'secret' not in json.dumps(caught.value.result())
    _Connection.error = None


def test_valid_gateway_response_is_unchanged(monkeypatch):
    monkeypatch.setattr(gateway_transport.http.client, 'HTTPConnection', _Connection)
    _Connection.error = None
    result = {'structuredContent': {'status': 'ok', 'data': {'answer': 'kept'}}}
    _Connection.response = _Response(200, json.dumps({'jsonrpc': '2.0', 'id': 1,
                                                        'result': result}).encode())
    assert gateway_transport.rpc('tools/call', {}, {}) == result


def test_application_error_does_not_expose_remote_text(monkeypatch):
    monkeypatch.setattr(gateway_transport.http.client, 'HTTPConnection', _Connection)
    _Connection.error = None
    _Connection.response = _Response(200, json.dumps({'jsonrpc': '2.0', 'id': 1,
        'result': {'isError': True, 'content': [{'type': 'text',
                                                 'text': 'secret provider exception'}]}}).encode())
    with pytest.raises(gateway_transport.GatewayFailure) as caught:
        gateway_transport.rpc('tools/call', {}, {})
    assert caught.value.result()['reason'] == 'gateway_tool_failed'
    assert 'secret' not in json.dumps(caught.value.result())


@pytest.mark.parametrize('rows', [None, {}, ['invalid'], [{'name': 'knowledge.context'}],
    [{'name': 'knowledge.context', 'description': 'x', 'inputSchema': {'type': 'bogus'}}],
    [{'name': 'knowledge.context', 'description': 'x', 'inputSchema': {}},
     {'name': 'knowledge.context', 'description': 'x', 'inputSchema': {}}]])
def test_malformed_catalog_fails_closed(rows):
    with pytest.raises(gateway_transport.GatewayFailure) as caught:
        gateway_transport.catalog_tools({'tools': rows}, {'knowledge.context'})
    assert caught.value.reason == 'gateway_catalog_invalid'


def test_discovery_failure_is_cached_and_backend_not_fabricated(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    adapter.catalog = None
    attempts = []
    def offline(*args):
        attempts.append(args)
        raise gateway_transport.GatewayFailure('gateway_timeout', 'timeout', retryable=True)
    monkeypatch.setattr(adapter, 'rpc', offline)
    assert 'knowledge.context' not in {t['name'] for t in adapter.tools()}
    assert 'knowledge.context' not in {t['name'] for t in adapter.tools()}
    assert len(attempts) == 1
    outcome = adapter.call('knowledge.context', {'query': 'q'})
    assert outcome['status'] == 'unavailable'
    assert outcome['reason'] == 'gateway_timeout'
    assert outcome['stage'] == 'discovery' and outcome['retryable'] is True
    assert outcome['evidence_status'] == 'not_obtained'
    assert adapter.gateway_discovery_failure['reason'] == 'gateway_timeout'


def test_advertised_tool_failure_and_success_are_distinct(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    descriptor = next(t for t in adapter.tools() if t['name'] == 'knowledge.context')
    assert 'advertisement only' in descriptor['description']
    monkeypatch.setattr(adapter, 'rpc', lambda *args: (_ for _ in ()).throw(
        gateway_transport.GatewayFailure('gateway_request_rejected', 'request')))
    failed = adapter.call('knowledge.context', {'query': 'q'})
    assert failed['status'] == 'error' and failed['reason'] == 'gateway_request_rejected'
    assert failed['stage'] == 'call' and failed['retryable'] is False
    assert failed['evidence_status'] == 'not_obtained'
    assert adapter.gateway_operation_status['knowledge.context']['status'] == 'failed'
    adapter.enabled_tools = ['knowledge.context']
    doctor = adapter.doctor()
    assert doctor['status'] == 'partial' and doctor['gateway_catalog_status'] == 'advertised'
    assert doctor['gateway_backend_status'] == 'operation_failed'
    monkeypatch.setattr(adapter, 'rpc', lambda *args: {
        'structuredContent': {'status': 'ok', 'data': {'answer': 'kept'}}})
    response = adapter.call('knowledge.context', {'query': 'q'})
    assert response['status'] == 'ok' and response['data'] == {'answer': 'kept'}
    assert response['gateway_transport']['elapsed_ms'] >= 0
    assert adapter.gateway_operation_status['knowledge.context']['status'] == 'response_received'


def test_catalog_pair_validation_fails_before_transport(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    adapter.catalog['knowledge.context']['inputSchema'] = {'type': 'object',
        'properties': {'repo_key': {'type': 'string', 'enum': ['ats', 'ops']},
            'capability_id': {'type': 'string', 'enum': ['code-navigation', 'planning-sequencing']}},
        'required': ['repo_key', 'capability_id'], 'additionalProperties': False,
        'oneOf': [
            {'properties': {'repo_key': {'const': 'ats'}, 'capability_id': {'enum': ['planning-sequencing']}}},
            {'properties': {'repo_key': {'const': 'ops'}, 'capability_id': {'enum': ['code-navigation']}}},
        ]}
    monkeypatch.setattr(adapter, 'rpc', lambda *args: pytest.fail('invalid pair contacted gateway'))
    result = adapter.call('knowledge.context', {'repo_key': 'ats', 'capability_id': 'code-navigation'})
    assert result['reason'] == 'gateway_request_invalid_for_catalog'
    assert result['stage'] == 'catalog_validation'


def test_doctor_catalog_not_attempted_when_only_local_tools_enabled(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    adapter.catalog = None
    adapter.enabled_tools = ['tooling.identity']
    monkeypatch.setattr(adapter, 'rpc', lambda *args: pytest.fail('unexpected gateway discovery'))
    doctor = adapter.doctor()
    assert doctor['gateway_catalog_status'] == 'not_attempted'
    assert doctor['gateway_backend_status'] == 'not_probed'


def test_cli_emits_bounded_failure_and_fails_exit(tmp_path, monkeypatch, capsys):
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(adapter, 'rpc', lambda *args: (_ for _ in ()).throw(
        gateway_transport.GatewayFailure('gateway_auth_failed', 'auth')))
    monkeypatch.setattr(agent_tooling_cli, 'AgentTooling', lambda config: adapter)
    monkeypatch.setattr(sys, 'argv', ['agent_tooling_cli.py', '--config', 'unused',
        'call', '--tool', 'knowledge.context', '--arguments', '{"query":"q"}'])
    with pytest.raises(SystemExit) as caught:
        agent_tooling_cli.main()
    assert caught.value.code == 1
    outcome = json.loads(capsys.readouterr().out)
    assert outcome['reason'] == 'gateway_auth_failed'
    assert outcome['evidence_status'] == 'not_obtained'


def test_mcp_emits_same_bounded_failure(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(adapter, 'rpc', lambda *args: (_ for _ in ()).throw(
        gateway_transport.GatewayFailure('gateway_service_unavailable', 'unavailable',
                                         retryable=True)))
    streams = {}

    @asynccontextmanager
    async def memory_stdio():
        client_send, server_read = anyio.create_memory_object_stream(100)
        server_send, client_read = anyio.create_memory_object_stream(100)
        streams['client'] = (client_read, client_send)
        async with client_send, server_read, server_send, client_read:
            yield server_read, server_send

    import mcp.server.stdio
    monkeypatch.setattr(mcp.server.stdio, 'stdio_server', memory_stdio)

    async def check():
        async with anyio.create_task_group() as group:
            group.start_soon(agent_tooling_cli.serve, adapter)
            while 'client' not in streams:
                await anyio.sleep(0)
            read, write = streams['client']
            async with ClientSession(read, write,
                    read_timeout_seconds=timedelta(seconds=5)) as session:
                await session.initialize()
                reply = await session.call_tool('knowledge.context', {'query': 'q'})
                assert not reply.isError
                assert reply.structuredContent['reason'] == 'gateway_service_unavailable'
                assert json.loads(reply.content[0].text) == reply.structuredContent
            group.cancel_scope.cancel()

    asyncio.run(check())


def test_catalog_validation_names_the_offending_arguments(tmp_path, monkeypatch):
    """2026-09-22 trial: an arm passed snapshot_id and a misspelled key to every
    knowledge call and only ever read "review arguments". The envelope now names them."""
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(adapter, 'rpc', lambda *args: pytest.fail('invalid request contacted gateway'))
    result = adapter.call('knowledge.context', {'question': 'q', 'snapshot_id': 'navigation-snapshot:x'})
    assert result['reason'] == 'gateway_request_invalid_for_catalog' and result['stage'] == 'catalog_validation'
    assert result['required_arguments'] == ['query'] and result['accepted_arguments'] == ['query']
    assert result['unknown_arguments'] == ['question', 'snapshot_id']
    messages = ' '.join(error['message'] for error in result['schema_errors'])
    assert "'query' is a required property" in messages and 'snapshot_id' in messages
    assert all(error['path'] == '<root>' for error in result['schema_errors'])
    assert result['evidence_status'] == 'not_obtained'


def test_context_waits_for_composed_backend_but_other_calls_keep_short_bound(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    import time
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            time.sleep(0.08)
            payload = b'{"jsonrpc":"2.0","id":1,"result":{"status":"ok"}}'
            try:
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(gateway_transport, 'DEFAULT_TIMEOUT_SECONDS', 0.02)
    monkeypatch.setattr(gateway_transport, 'CONTEXT_TIMEOUT_SECONDS', 1)
    endpoint = f'http://127.0.0.1:{server.server_port}/api/mcp'
    try:
        assert gateway_transport.rpc('tools/call', {'name': 'knowledge.context'}, {}, endpoint=endpoint) == {'status': 'ok'}
        with pytest.raises(gateway_transport.GatewayFailure) as caught:
            gateway_transport.rpc('tools/list', {}, {}, endpoint=endpoint)
        failure = caught.value.result()
        assert failure['reason'] == 'gateway_timeout'
        assert failure['timeout_seconds'] == 0.02
        assert failure['elapsed_seconds'] >= 0.02
        assert failure['timeout_scope'] == 'gateway_http_socket_wait'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_rpc_rejection_has_safe_diagnostic_without_remote_text(monkeypatch):
    monkeypatch.setattr(gateway_transport.http.client, 'HTTPConnection', _Connection)
    _Connection.error = None
    _Connection.response = _Response(200, json.dumps({'jsonrpc':'2.0','id':1,
        'error': {'code': -32602, 'message': 'private path and token'}}).encode())
    with pytest.raises(gateway_transport.GatewayFailure) as caught:
        gateway_transport.rpc('tools/call', {}, {})
    result = caught.value.result()
    assert result['request_diagnostic'] == 'arguments_rejected_by_gateway'
    assert result['rpc_error_code'] == -32602
    assert 'private' not in json.dumps(result)


def test_reference_rejection_directs_to_parent_without_inventing_expiry(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    adapter.catalog['knowledge.reference_recovery'] = {'name':'knowledge.reference_recovery',
        'description':'Recover references', 'inputSchema':{'type':'object'}}
    monkeypatch.setattr(adapter, 'rpc', lambda *args: (_ for _ in ()).throw(
        gateway_transport.GatewayFailure('gateway_request_rejected', 'request')))
    result = adapter.call('knowledge.reference_recovery', {})
    assert result['operation'] == 'knowledge.reference_recovery'
    assert 'generation_digest' in result['next_step']
    assert result['evidence_status'] == 'not_obtained'
