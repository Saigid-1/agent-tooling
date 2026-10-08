"""Isolated single-operator image trial; synthetic data only, no live registrations.

Requires a fresh physical --root on the operator's selected volume and the
package installed from this checkout (the runtime root is rendered by the
installer, board role only). Leaves the board running for inspection and prints
its receipt, never its passcode.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from kp_agent_tooling._impl.runtime_install import apply, plan

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--root', required=True, type=Path)
p.add_argument('--image', required=True, help='digest reference of the product image')
p.add_argument('--docker', default='docker')
p.add_argument('--port', type=int, default=3488)
a = p.parse_args()
repo = Path(__file__).resolve().parents[1]
root = a.root
if not root.is_absolute() or root.exists() or root.parent.resolve() != root.parent:
    raise SystemExit('Select a fresh physical root under an existing physical directory')
root.mkdir(mode=0o700)
runtime, fixture = root / 'runtime', root / 'fixture-repo'
runtime.mkdir(mode=0o700)
fixture.mkdir(mode=0o700)
# The installer requires one committed navigation target; a synthetic one suffices.
fixture_git = ['git', '-C', str(fixture), '-c', 'user.name=trial', '-c', 'user.email=trial@example.invalid']
subprocess.run(['git', 'init', '-q', str(fixture)], check=True)
(fixture / 'README.md').write_text('synthetic import trial\n')
subprocess.run(fixture_git + ['add', 'README.md'], check=True)
subprocess.run(fixture_git + ['commit', '-q', '-m', 'synthetic fixture'], check=True)
project = 'agent-import-' + str(a.port)
install = dict(runtime_root=str(runtime), image=a.image, repositories=[f'fixture={fixture}'],
    components='tooling,board', uid=os.getuid(), gid=os.getgid(), board_port=a.port,
    project_name=project)
apply(plan(**install)['plan_sha256'], **install)

def write(path, value):
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(0o600)

cfg = runtime / 'config/board'
write(cfg / 'catalog.json', (repo / 'config/desk-context/catalog.example.json').read_text())
write(cfg / 'doctrine.md', (repo / 'config/desk-context/doctrine.md').read_text())
(runtime / 'state/memory').mkdir(mode=0o700)
(runtime / 'state/memory/import-trial').mkdir(mode=0o700)
write(cfg / 'session-import.json', dict(schema_version='ops.desk-memory.local.v1',
    state_root='/state/memory/import-trial', catalog_path='/config/board/catalog.json',
    workspace_root='/config/board', provider_instance='isolated-docker-trial',
    provider_session_id='synthetic-import-operator'))
native = 'a55869b4-6ab4-4e5c-b028-8d4b31d27b71'
source = runtime / 'import-sources' / f'rollout-2026-09-25T00-00-00-{native}.jsonl'
def row(kind, payload):
    return json.dumps(dict(type=kind, timestamp='2026-09-25T00:00:00Z', payload=payload)) + '\n'
def message(text):
    return row('event_msg', dict(type='user_message', message=text))
write(source, row('session_meta', dict(id=native, cwd='/synthetic/repo')) + message('cedar import marker'))
env = dict(os.environ)
compose = [a.docker, 'compose', '--project-directory', str(runtime)]
def run(command, **kw):
    return subprocess.run(command, check=True, capture_output=True, text=True, env=env, **kw).stdout
run(compose + ['up', '-d', 'board'])
container = run(compose + ['ps', '-q', 'board']).strip()
def py(code):
    return run([a.docker, 'exec', container, 'python', '-c', code])
base = f'http://127.0.0.1:{a.port}'
cookie = ''
def authenticate():
    global cookie
    logs = subprocess.run([a.docker, 'logs', container], capture_output=True, text=True, check=True)
    code = re.findall(r'Remote access passcode: (\S+)', logs.stdout + logs.stderr)[-1]
    request = urllib.request.Request(base + '/api/passcode/verify', data=json.dumps({'passcode':code}).encode(),
        headers={'Content-Type':'application/json', 'Origin':base})
    with urllib.request.urlopen(request, timeout=10) as response:
        cookie = response.headers['Set-Cookie'].split(';')[0]
def ready():
    for attempt in range(40):
        try:
            authenticate()
            return
        except (OSError, IndexError):
            if attempt == 39:
                raise
            time.sleep(.5)
def call(name, value=None):
    mutation = value is not None
    request = urllib.request.Request(base + '/api/trpc/' + name + ('' if mutation else '?input=null'),
        data=json.dumps(value).encode() if mutation else None,
        headers={'Content-Type':'application/json', 'Origin':base, 'Cookie':cookie})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)['result']['data']
def action(name, **values):
    return call('sessionImport.run', dict(action=name, **values))
ready()
saved_cookie = cookie
cookie = ''
try:
    call('sessionImport.setup')
    raise AssertionError('Unauthenticated request was accepted')
except urllib.error.HTTPError as error:
    assert error.code == 401
cookie = saved_cookie
assert call('sessionImport.setup')['configured'] is True
assert action('desks')['code'] == 'admission_unavailable'
py("from kp_agent_tooling._impl.service.desk_memory_runtime import initialize,admit,components; from kp_agent_tooling._impl.service.session_sources import SessionSources; p='/config/board/session-import.json'; initialize(p); admit(p,desk_id='implementation-desk',provider_id='fixture',model_id='fixture'); SessionSources(components(p)[3]).upgrade()")
desk = action('desks')['desks'][0]['binding_key']
def preview(mode):
    result = action('preview', sourceFile='/import-sources/' + source.name,
        nativeSessionId=native, selectedDeskId=desk, mode=mode)
    assert result['status'] == 'preview', result
    return result
first = preview('full')
job = first['job_id']
assert action('apply', planToken=first['plan_token'], consent=True)['phase'] == 'ready'
assert action('continue', jobId=job)['counts']['imported'] == 1
assert action('continue', jobId=job)['phase'] == 'complete'
assert action('assert-owner', jobId=job, selectedDeskId=desk, assertedBy='synthetic-operator')['status'] == 'asserted'
run(compose + ['restart', 'board'])
ready()
assert action('status', jobId=job)['phase'] == 'complete'
assert action('apply', planToken=first['plan_token'], consent=True)['counts']['imported'] == 1
follow = preview('current-turn-and-forward')
job2 = follow['job_id']
action('apply', planToken=follow['plan_token'], consent=True)
action('follow', jobId=job2)
def wait_for(predicate):
    for _ in range(900):
        status = action('status', jobId=job2)
        if predicate(status):
            return status
        time.sleep(.2)
    raise AssertionError(status)
wait_for(lambda s: s['phase'] == 'following')
run(compose + ['restart', 'board'])
ready()
with source.open('a') as stream:
    stream.write(message('birch after restart'))
wait_for(lambda s: s['counts'].get('imported', 0) == 1)
assert action('stop', jobId=job2)['phase'] == 'paused'
wait_for(lambda s: s['worker_active'] is False)
# Public CLI and native stdio MCP must recover identical source-backed search.
parity = py('''import asyncio,json,shutil,subprocess
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
cmd=shutil.which('kp-agent-memory')
args=['--config','/config/board/session-import.json']
query={'query':'cedar import marker'}
cli=json.loads(subprocess.check_output([cmd,*args,'call','--tool','memory.search','--arguments',json.dumps(query)]))
async def check():
    async with stdio_client(StdioServerParameters(command=cmd,args=[*args,'serve'])) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize()
            result=await s.call_tool('memory.search',query)
            assert not result.isError
            assert result.structuredContent == cli
            assert 'cedar import marker' in json.dumps(cli)
asyncio.run(check())
print(json.dumps({'cli_mcp_parity':True}))
''')
assert json.loads(parity)['cli_mcp_parity']
inspect = json.loads(run([a.docker, 'inspect', container]))[0]
image = json.loads(run([a.docker, 'image', 'inspect', inspect['Image']]))[0]
receipt = dict(status='passed', image_id=inspect['Image'], source_revision=image['Config']['Labels']['org.opencontainers.image.revision'],
    fixture_only=True, exact_session_admission=True, unauthenticated_refused=True,
    unadmitted_refused=True, browser_preview_apply_capture=True, restart_replay=True,
    follower_restart_append_stop=True, cli_native_mcp_parity=True,
    root=str(root), url=base, container=container,
    mounts=[{'destination':m['Destination'], 'source':m['Source'], 'writable':m['RW']} for m in inspect['Mounts']],
    image_size_bytes=image['Size'])
write(root / 'receipt.json', receipt)
print(json.dumps(receipt, indent=2))
