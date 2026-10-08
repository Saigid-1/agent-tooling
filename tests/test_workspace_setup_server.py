"""Actual loopback requests against the independent setup transport."""

import http.client
import json
from pathlib import Path
import subprocess
import sys
from threading import Thread

import pytest

from kp_agent_tooling._impl.workspace_setup_server import MAX_BODY, WorkspaceSetupServer


@pytest.fixture
def server():
    service = WorkspaceSetupServer()
    thread = Thread(target=service.serve_forever, daemon=True)
    thread.start()
    yield service
    service.shutdown()
    service.server_close()
    thread.join(timeout=2)


@pytest.fixture
def request_data(tmp_path):
    repo = tmp_path / 'product with spaces'
    repo.mkdir()
    subprocess.check_call(['git', 'init', '-q'], cwd=repo)
    (repo / 'main.py').write_text('import pathlib\n')
    subprocess.check_call(['git', 'add', 'main.py'], cwd=repo)
    subprocess.check_call(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                           'commit', '-qm', 'source'], cwd=repo)
    return {'schema_version': 'ops.workspace-setup.v1', 'repo_key': 'product',
            'repository': str(repo), 'output_root': str(tmp_path / 'configured workspace'),
            'launcher': {'command': sys.executable, 'args': ['/operator/tooling/cli.py']},
            'capabilities': ['source-navigation']}


def request(server, method, path, *, body=None, token=True, headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    values = {'Host': f'127.0.0.1:{server.server_port}'}
    if token:
        values['Authorization'] = 'Bearer ' + server.launch_url.split('#token=', 1)[1]
    if body is not None:
        values['Content-Type'] = 'application/json'
        body = json.dumps(body).encode()
    values.update(headers or {})
    connection.request(method, path, body=body, headers=values)
    response = connection.getresponse()
    raw = response.read()
    result = (response.status, json.loads(raw) if response.getheader('Content-Type', '').startswith('application/json') else raw,
              dict(response.getheaders()))
    connection.close()
    return result


def test_catalog_auth_and_same_origin(server):
    assert server.server_address[0] == '127.0.0.1'
    status, body, headers = request(server, 'GET', '/api/catalog')
    assert status == 200 and body['schema_version'] == 'ops.workspace-catalog.v1'
    assert headers['Cache-Control'] == 'no-store' and 'Access-Control-Allow-Origin' not in headers
    assert request(server, 'GET', '/api/catalog', token=False)[:2] == (401, {'status': 'error', 'reason': 'authorization required'})
    assert request(server, 'GET', '/api/catalog', headers={'Authorization': 'Bearer é'})[0] == 401
    assert request(server, 'GET', '/api/catalog', headers={'Host': 'evil.example'})[0] == 400
    assert request(server, 'GET', '/api/catalog', headers={'Origin': 'http://evil.example'})[0] == 400
    assert request(server, 'GET', '/api/catalog', headers={'Sec-Fetch-Site': 'cross-site'})[0] == 400
    assert request(server, 'GET', '/api/catalog', headers={'Origin': server.origin})[0] == 200


def test_plan_apply_and_refresh_has_no_write(server, request_data):
    output = Path(request_data['output_root'])
    status, planned, _ = request(server, 'POST', '/api/plan', body={'request': request_data})
    assert status == 200 and planned['status'] == 'configuration-ready'
    assert not output.exists()
    status, applied, _ = request(server, 'POST', '/api/apply', body={
        'request': request_data, 'expected_plan_sha256': planned['plan_sha256']})
    assert status == 200 and applied['status'] == 'configured'
    bundle = Path(applied['bundle'])
    original = {path.name: path.read_bytes() for path in bundle.iterdir()}
    assert request(server, 'GET', '/api/catalog')[0] == 200
    assert request(server, 'POST', '/api/plan', body={'request': request_data})[0] == 200
    assert {path.name: path.read_bytes() for path in bundle.iterdir()} == original
    assert request(server, 'POST', '/api/apply', body={
        'request': request_data, 'expected_plan_sha256': '0' * 64})[0] == 409
    assert {path.name: path.read_bytes() for path in bundle.iterdir()} == original
    (Path(request_data['repository']) / 'main.py').write_text('import sys\n')
    assert request(server, 'POST', '/api/apply', body={
        'request': request_data, 'expected_plan_sha256': planned['plan_sha256']})[0] == 409
    assert {path.name: path.read_bytes() for path in bundle.iterdir()} == original


def test_rejects_unbounded_malformed_and_cross_origin_posts(server, request_data):
    valid = {'request': request_data}
    assert request(server, 'POST', '/api/plan', body=valid, token=False)[0] == 401
    assert request(server, 'POST', '/api/plan', body=valid, headers={'Origin': 'http://evil.example'})[0] == 400
    assert request(server, 'POST', '/api/plan', body={'wrong': valid})[0] == 400
    assert request(server, 'POST', '/api/plan', body=valid, headers={'Content-Type': 'text/plain'})[0] == 400
    assert request(server, 'POST', '/api/plan', body=valid, headers={'Content-Length': str(MAX_BODY + 1)})[0] == 400
    assert request(server, 'POST', '/api/plan', body=valid, headers={'Transfer-Encoding': 'chunked'})[0] == 400
    assert request(server, 'POST', '/api/plan', body=valid, headers={'Content-Type': 'application/json',
                    'Origin': server.origin})[0] == 200
    bad_repo = {**request_data, 'repository': '/no/such/repository'}
    status, error, _ = request(server, 'POST', '/api/plan', body={'request': bad_repo})
    assert status == 400 and error['status'] == 'error'
    assert not Path(request_data['output_root']).exists()


def test_static_routes_are_allowlisted(server):
    for route, content_type in (('/', 'text/html'), ('/app.js', 'text/javascript'), ('/style.css', 'text/css')):
        status, body, headers = request(server, 'GET', route, token=False)
        assert status == 200 and body
        assert headers['Content-Type'].startswith(content_type)
    assert request(server, 'GET', '/workspace_setup.py', token=False)[0] == 404
    assert request(server, 'GET', '/../workspace_setup.py', token=False)[0] == 404
    assert request(server, 'GET', '/app.js?file=../workspace_setup.py', token=False)[0] == 404
