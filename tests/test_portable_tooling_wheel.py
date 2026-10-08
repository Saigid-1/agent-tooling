"""A built wheel must work outside this checkout without the OPS app installed."""
import pytest
import json
from pathlib import Path
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=None, env=None):
    return subprocess.check_output(args, cwd=cwd, env=env, text=True)


@pytest.mark.skip(reason='S5 excluded: builds the OPS-only portable_tooling/ extraction tree (pip wheel --no-build-isolation)')
def test_wheel_clean_install_and_packaged_assets(tmp_path):
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    run(sys.executable, '-m', 'pip', 'wheel', '--no-build-isolation', '--no-deps',
        '-w', str(wheels), str(ROOT / 'portable_tooling'), cwd=tmp_path)
    wheel, = wheels.glob('kp_agent_tooling-*.whl')
    with zipfile.ZipFile(wheel) as archive:
        paths = archive.namelist()
    assert not any(path.startswith('kp_ops/') for path in paths)
    assert not any('/dispatch' in path or '/binding_registry' in path or '/opencode_desk_launch' in path or '/app.py' in path for path in paths)
    assert 'kp_agent_tooling/_impl/service/workspace_context.py' in paths
    assert 'kp_agent_tooling/_impl/service/episodic_memory.py' in paths
    assert 'kp_agent_tooling/_impl/service/summary_evidence.py' in paths
    assert 'kp_agent_tooling/_impl/service/episodic_queue.py' in paths
    assert 'kp_agent_tooling/_impl/semantic_index.py' in paths
    assert 'kp_agent_tooling/_impl/git_batch.py' in paths
    assert 'kp_agent_tooling/_impl/commit_rationale.py' in paths
    assert 'kp_agent_tooling/_impl/tool_discovery.py' in paths

    assert 'kp_agent_tooling/_impl/service/agent_tooling.py' in paths
    assert 'kp_agent_tooling/_impl/repository_manifest.py' in paths
    assert 'kp_agent_tooling/_impl/navigation_search_pages.py' in paths
    assert 'kp_agent_tooling/_impl/host_transcript_capture.py' in paths
    assert 'kp_agent_tooling/capture_cli.py' in paths
    assert 'kp_agent_tooling/transcript_cli.py' in paths
    venv = tmp_path / 'venv'
    run(sys.executable, '-m', 'venv', str(venv), cwd=tmp_path)
    python = venv / 'bin/python'
    run(str(python), '-m', 'pip', 'install', '--no-index', '--no-deps', str(wheel), cwd=tmp_path)
    output = run(str(python), '-I', '-c', '\n'.join([
        'import importlib.resources as r, importlib.util as u, json',
        'import kp_agent_tooling._impl.service.agent_tooling, kp_agent_tooling.cli',
        'import kp_agent_tooling._impl.service.repository_coverage',
        'import kp_agent_tooling._impl.host_transcript_capture',
        'import kp_agent_tooling._impl.service.workspace_context',
        'import kp_agent_tooling._impl.service.desk_memory_runtime',
        'import kp_agent_tooling._impl.service.episodic_queue',
        'import kp_agent_tooling._impl.service.episodic_summarizer',
        'import kp_agent_tooling._impl.semantic_index',
        'from kp_agent_tooling._impl.commit_rationale import RationaleCatalog',

        'import kp_agent_tooling.capture_cli, kp_agent_tooling.transcript_cli',
        'from kp_agent_tooling._impl.server_identity import identity',
        'assert identity({"repos":{}}, "a"*64)["status"] == "unknown"',
        'from kp_agent_tooling._impl.package_lineage import inspect_wheel',
        'assert inspect_wheel(None, name="example", version="1", sha256="a"*64, size=1)["reason"] == "wheel_not_available"',
        'p = r.files("kp_agent_tooling").joinpath("assets")',
        'assert json.loads(p.joinpath("finding.schema.json").read_text())',
        'assert json.loads(p.joinpath("journey-registry.schema.json").read_text())',
        'assert p.joinpath("verify-behavior.md").read_text()',
        'assert u.find_spec("kp_core") is None',
        'assert u.find_spec("fastapi") is None',
        'print(kp_agent_tooling._impl.service.agent_tooling.__file__)',
    ]), cwd=tmp_path)
    assert str(venv) in output and str(ROOT) not in output
    config = tmp_path / 'tooling.json'
    config.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1', 'repos': {},
        'serena': {'command': '/bin/true', 'python': str(python), 'runtime_home': str(tmp_path)},
        'enabled_tools': ['tooling.identity', 'verification.guide', 'verification.finding', 'delivery.read']}))
    catalog = json.loads(run(str(venv / 'bin/kp-agent-tooling'), '--config', str(config), 'tools', cwd=tmp_path))
    assert {row['name'] for row in catalog} == {'tooling.identity', 'verification.guide',
                                                'verification.finding', 'delivery.read'}
    setup_catalog = json.loads(run(str(venv / 'bin/kp-agent-setup'), 'catalog', cwd=tmp_path))
    assert setup_catalog['schema_version'] == 'ops.workspace-catalog.v1'
    assert setup_catalog['capabilities'][0]['id'] == 'source-navigation'


def test_local_scip_provider_without_gateway(tmp_path):
    from kp_agent_tooling._impl.scip_navigation import digest
    from kp_agent_tooling._impl.service.agent_tooling import AgentTooling

    repo = tmp_path / 'source'
    repo.mkdir()
    run('git', 'init', '-q', str(repo))
    (repo / 'module.py').write_text('def alpha():\n    return 1\n')
    run('git', 'add', 'module.py', cwd=repo)
    run('git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-q', '-m', 'pin', cwd=repo)
    revision = run('git', 'rev-parse', 'HEAD', cwd=repo).strip()
    blob = run('git', 'rev-parse', 'HEAD:module.py', cwd=repo).strip()
    data = {'schema': 'ops.scip-navigation.v1', 'repo_key': 'product', 'revision': revision,
        'producer': {'name': 'scip-python', 'version': '0.6.6'}, 'provenance': {},
        'blobs': {'module.py': blob},
        'occurrences': [{'symbol': 'scip-python python example alpha().', 'path': 'module.py',
                         'blob_sha': blob, 'byte_offset': 4, 'byte_length': 5,
                         'definition': True, 'local': False}],
        'gaps': [], 'limitations': ['Static only']}
    index = {'sha256': digest(data), 'data': data}
    index_path = tmp_path / 'index.json'
    index_path.write_text(json.dumps(index))
    source_catalog = {'repositories': {'product': {'path': str(repo)}},
        'platforms': {'product': {'schema': 'ops.platform-request.v1', 'owner': 'test',
            'profile': 'source', 'sources': {'product': {'revision': revision,
                'artifacts': [{'path': 'module.py', 'role': 'configuration'}]}}}},
        'scip_indexes': {'product': [{'path': str(index_path), 'revision': revision,
                                     'sha256': index['sha256']}]}}
    catalog_path = tmp_path / 'sources.json'
    catalog_path.write_text(json.dumps(source_catalog))
    config_path = tmp_path / 'tooling.json'
    config_path.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1',
        'repos': {'product': {'path': str(repo), 'revision': revision}},
        'serena': {'command': '/bin/true', 'python': sys.executable, 'runtime_home': str(tmp_path)},
        'local_knowledge_config': str(catalog_path),
        'enabled_tools': ['knowledge.platform', 'knowledge.symbol']}))
    adapter = AgentTooling(config_path)
    assert [t['name'] for t in adapter.tools()] == ['knowledge.platform', 'knowledge.symbol']
    report = adapter.call('knowledge.symbol', {'repo_key': 'product', 'target_revision': revision,
                                              'path': 'module.py', 'line': 1})
    assert report['status'] == 'ok'
    assert report['data']['results'][0]['resolution']['definitions'][0]['revision'] == revision
    assert adapter.call('knowledge.platform', {'repo_key': 'product'})['status'] == 'ok'


@pytest.mark.skip(reason='S5 excluded: builds the OPS-only portable_tooling/ extraction tree (pip wheel --no-build-isolation)')
def test_installed_memory_mcp_without_core_or_dispatch(tmp_path):
    wheels=tmp_path/'wheels';wheels.mkdir()
    run(sys.executable,'-m','pip','wheel','--no-build-isolation','--no-deps','-w',str(wheels),str(ROOT/'portable_tooling'),cwd=tmp_path)
    wheel,=wheels.glob('*.whl'); installed=tmp_path/'installed'
    with zipfile.ZipFile(wheel) as archive:archive.extractall(installed)
    # -I excludes checkout imports; a finder rejects legacy imports even though
    # the test interpreter may have Core available for unrelated repository tests.
    program=r'''
import sys, json, pathlib, asyncio, importlib.abc
class DenyLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'kp_ops','kp_core','fastapi'}:
            raise ImportError('retired runtime import attempted: '+fullname)
sys.meta_path.insert(0,DenyLegacy())
sys.path.insert(0,sys.argv[1])
from kp_agent_tooling._impl.service.desk_memory_runtime import initialize,admit,components
from kp_agent_tooling._impl.service.episodic_memory_tools import from_config
root=pathlib.Path(sys.argv[2]); root.mkdir(mode=0o700)
source=pathlib.Path(sys.argv[3])
catalog=root/'catalog.json';catalog.write_text((source/'catalog.example.json').read_text());catalog.chmod(0o600)
(root/'doctrine.md').write_text((source/'doctrine.md').read_text())
config=root/'session.json';config.write_text(json.dumps({'schema_version':'ops.desk-memory.local.v1','state_root':str(root),'workspace_root':str(root),'catalog_path':str(catalog),'provider_instance':'codex-local','provider_session_id':'existing-session'}));config.chmod(0o600)
initialize(config);receipt=admit(config,desk_id='implementation-desk',provider_id='openai',model_id='test')
assert receipt['context']['source_revision_status']=='declared_unverified'
assert receipt['dispatch'] is False
_,_,_,store=components(config)
episode=store.capture('existing-session',source_ref='fixture-transcript:1',events=[{'event_id':'one','role':'user','text':'Preserve evidence.'}])
assert from_config(config).call('memory.read_event',{'episode_id':episode['episode_id'],'event_id':'one'})['text']=='Preserve evidence.'
import kp_agent_tooling.queue_cli, kp_agent_tooling.claude_hook_cli
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
async def check():
    code='import sys;sys.path.insert(0,'+repr(sys.argv[1])+');from kp_agent_tooling.memory_cli import main;main()'
    args=['-I','-c',code,'--config',str(config),'serve']
    async with stdio_client(StdioServerParameters(command=sys.executable,args=args)) as (r,w):
        async with ClientSession(r,w) as client:
            await client.initialize()
            tools=await client.list_tools()
            assert len(tools.tools)==12
            assert "memory.session" in {tool.name for tool in tools.tools}
            choices=await client.call_tool('memory.bindings',{})
            assert not choices.isError and choices.structuredContent['bindings']
            assert any(row['own'] for row in choices.structuredContent['bindings'])
            result=await client.call_tool('memory.read_event',{'episode_id':episode['episode_id'],'event_id':'one'})
            assert not result.isError and result.structuredContent['text']=='Preserve evidence.'
            proposal=await client.call_tool('memory.propose',{'episode_ids':[episode['episode_id']],
                'items':[{'kind':'observation','text':'Evidence is preserved.',
                    'citations':[{'episode_id':episode['episode_id'],'event_id':'one',
                                  'start':0,'end':18,'quote':'Preserve evidence.'}]}],
                'unresolved_questions':[]})
            assert not proposal.isError, proposal
            capsule=proposal.structuredContent['capsule_id']
            for name in ('memory.resume','memory.handoff','memory.evidence_directory','memory.handoff_page'):
                answer=await client.call_tool(name,{'capsule_id':capsule})
                assert not answer.isError, (name,answer)
                assert answer.structuredContent
                if name=='memory.resume':
                    assert answer.structuredContent['capsule_id']==capsule
                    assert answer.structuredContent['complete_handoff'] is True
                if name=='memory.handoff':
                    assert answer.structuredContent['items'][0]['citations'][0]['quote']=='Preserve evidence.'
                    assert answer.structuredContent['items'][0]['evidence_check']['status']=='review_required'
                if name=='memory.evidence_directory':
                    assert answer.structuredContent['total']==1
asyncio.run(check())
print('installed CLI composition and native memory MCP passed')
'''
    output=run(sys.executable,'-I','-c',program,str(installed),str(tmp_path/'state'),
               str(ROOT/'config/desk-context'),cwd=tmp_path)
    assert 'native memory MCP passed' in output
