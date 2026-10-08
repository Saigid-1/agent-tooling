import json
import hashlib
from pathlib import Path
import pytest
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling


def test_doctor_reports_server_build_and_raw_config_digest(tmp_path, monkeypatch):
    config=tmp_path/'config.json'
    config.write_text('{"schema_version":"ops.agent-tooling.v1","repos":{}}\n')
    adapter=AgentTooling(config)
    native=adapter.call('tooling.identity',{})
    monkeypatch.setattr(adapter,'tools',lambda:[])
    result=adapter.doctor()
    assert result['server_build_revision']==native['server_build_revision']
    assert native['identity_observed_at']=='server_startup'
    assert native['status'] in {'known','unknown'}
    assert result['config_sha256']==hashlib.sha256(config.read_bytes()).hexdigest()
    assert 'config_path' not in result

def test_standalone_catalog_requires_no_gateway_or_product_evidence(tmp_path, monkeypatch):
    config=tmp_path/'standalone.json'
    config.write_text(json.dumps({'schema_version':'ops.agent-tooling.v1',
        'repos':{'legal':{'path':'unused','revision':'a'*40}},
        'enabled_tools':['serena.inspect','navigation.imports','navigation.dependencies','delivery.read']}))
    adapter=AgentTooling(config)
    monkeypatch.setattr(adapter,'rpc',lambda *a:pytest.fail('standalone discovery contacted gateway'))
    assert {t['name'] for t in adapter.tools()} == set(adapter.config['enabled_tools'])
    assert 'tooling.identity' not in {t['name'] for t in adapter.tools()}
    assert adapter.url is None
    with pytest.raises(ValueError,match='allowlist'):
        adapter.call('verification.packet',{'slice_id':'ats-pool-lifecycle'})
    with pytest.raises(ValueError,match='allowlist'):
        adapter.call('knowledge.context',{})

def test_standalone_rejects_unknown_configured_operation(tmp_path):
    config=tmp_path/'standalone.json'
    config.write_text(json.dumps({'schema_version':'ops.agent-tooling.v1','repos':{},'enabled_tools':['invented.tool']}))
    with pytest.raises(ValueError,match='unknown configured operations'):
        AgentTooling(config).tools()

@pytest.fixture
def adapter(tmp_path):
    gateway=tmp_path/'gateway.toml';gateway.write_text('[mcp_servers.ops-gateway]\nurl="http://127.0.0.1:8400/api/mcp"\nhttp_headers={}\n')
    config=tmp_path/'config.json';config.write_text(json.dumps({'schema_version':'ops.agent-tooling.v1','gateway_config':str(gateway),
      'repos':{'ats':{'path':'unused','revision':'0'*40}},'evidence':{'summary':'records/example.json'},
      'evidence_repo':'unused','evidence_revision':'a'*40}))
    a=AgentTooling(config);a.catalog={}
    return a

def test_rejects_mutating_tool(adapter):
    with pytest.raises(ValueError,match='allowlist'):adapter.call('mutate.move_card',{})

def test_rejects_arbitrary_evidence_path(adapter):
    with pytest.raises(Exception):adapter.call('lifecycle.evidence',{'evidence_id':'../../secrets'})

def test_rejects_serena_path_escape(adapter):
    with pytest.raises(ValueError,match='canonical'):adapter.call('serena.inspect',{'repo_key':'ats','path':'../x.py','symbol':'x'})

def test_evidence_uses_configured_commit_not_working_tree(adapter,monkeypatch):
    calls=[]
    def source(repo,revision,path):
        calls.append((repo,revision,path));return 'b'*40,'{"status":"fixture"}'
    monkeypatch.setattr('kp_agent_tooling._impl.service.agent_tooling.source',source)
    result=adapter.call('lifecycle.evidence',{'evidence_id':'summary'})
    assert calls==[('unused','a'*40,'records/example.json')]
    assert result['content']=={'status':'fixture'} and result['blob_sha']=='b'*40

def test_unavailable_gateway_tool_not_fabricated(adapter):
    assert {t['name'] for t in adapter.tools()}=={'knowledge.rationale','serena.find','serena.overview','navigation.snapshot','navigation.paths','navigation.search','navigation.manifest','navigation.search_page','navigation.batch','navigation.semantic','verification.review','navigation.workspace','navigation.source','tooling.identity','serena.inspect','lifecycle.evidence','navigation.imports','navigation.dependencies','verification.guide','verification.packet','verification.finding','verification.observations','verification.plan','verification.handoff','delivery.read'}

def test_serena_context_composition_preserves_provider_and_source_scope(adapter, monkeypatch, tmp_path):
    adapter.config['serena'] = {'command': '/provider/bin/serena', 'python': '/provider/bin/python', 'runtime_home': str(tmp_path)}
    monkeypatch.setattr('kp_agent_tooling._impl.service.agent_tooling.SerenaNavigationProvider.inspect',
        lambda *a: {'status':'ok','invocation':{'provider_response_received':True},'report':{'citations':[],'status':'no_results'}})
    monkeypatch.setattr('kp_agent_tooling._impl.service.import_context.source', lambda *a: ('b'*40, 'from product.graph.registry import run_query\n'))
    monkeypatch.setattr('kp_agent_tooling._impl.service.agent_tooling.source', lambda *a: ('b'*40, 'from product.graph.registry import run_query\n'))
    # Isolate environment mutation from other tests.
    monkeypatch.setattr('kp_agent_tooling._impl.service.agent_tooling.os.environ', {'PATH':'/opt/toolchain/node_modules/.bin:/usr/local/bin:/usr/bin'})
    result = adapter.call('serena.inspect', {'repo_key':'ats','path':'route.py','symbol':'run_query'})
    assert result['report']['status'] == 'no_results'
    assert result['report']['import_context']['imports'][0]['module'] == 'product.graph.registry'
    assert result['report']['source_snapshot']['ats']['revision'] == '0'*40
    assert result['invocation']['provider_response_received']
    import os
    assert '/opt/toolchain/node_modules/.bin' in os.environ['PATH'].split(os.pathsep)
    assert '/opt/homebrew/bin' not in os.environ['PATH']


def test_typescript_context_tool_reports_missing_runtime(adapter):
    result=adapter.call('navigation.imports',{'repo_key':'ats','path':'studio/app.ts'})
    assert result=={'status':'unavailable','reason':'typescript_runtime_not_configured'}


def test_dependency_provider_failure_stays_partial(adapter, monkeypatch):
    adapter.config['serena']={'python':'/unused'}
    monkeypatch.setattr('kp_agent_tooling._impl.dependency_identity.declared',lambda *a:{'source_revision':'0'*40})
    monkeypatch.setattr('kp_agent_tooling._impl.dependency_identity.provider',lambda *a:{'status':'unavailable'})
    result=adapter.call('navigation.dependencies',{'repo_key':'ats','path':'app.ts'})
    assert result['status']=='partial' and result['deployed']=='not-assessed'


def test_finding_schema_and_missing_payload(adapter):
    assert adapter.call('verification.finding',{'mode':'schema'})['schema']['properties']['schema_version']['const']=='ops.verification-finding.v5'
    result=adapter.call('verification.finding',{'mode':'validate'})
    assert result['status']=='invalid' and result['semantic_verdict']=='not-assessed'


def test_local_validation_does_not_contact_gateway(adapter, monkeypatch):
    adapter.catalog=None
    def forbidden(*args, **kwargs):
        raise AssertionError('local operation attempted network discovery')
    monkeypatch.setattr(adapter, 'rpc', forbidden)
    assert adapter.call('verification.finding', {'mode':'schema'})['status']=='ok'
    assert adapter.call('verification.finding', {'mode':'validate','finding':{}})['status']=='invalid'
    tool=next(t for t in adapter.tools(include_gateway=False) if t['name']=='verification.finding')
    assert tool['annotations']['readOnlyHint'] is True
    assert tool['annotations']['openWorldHint'] is False


def test_observations_read_only_adapter(adapter, tmp_path, monkeypatch):
    from kp_agent_tooling_ops._impl.observations import ObservationStore, make_record
    store=ObservationStore(tmp_path/'registry')
    scope={'sources':{'ats':'a'*40},'binding_sha256':'b'*64}
    oid=store.append(make_record('fixture','invocation','route','static',scope,{},[],[],['test']))
    adapter.config['observation_registry']=str(store.root)
    adapter.catalog=None
    monkeypatch.setattr(adapter,'rpc',lambda *a:pytest.fail('unexpected gateway invocation'))
    assert adapter.call('verification.observations',{'mode':'list'})['records'][0]['id']==oid
    assert adapter.call('verification.observations',{'mode':'read','id':oid})['original']=={}
    assert adapter.call('verification.observations',{'mode':'assess','ids':[oid],'expected_scope':scope})['execution']=='unknown'


def test_catalog_preserves_local_operations_during_gateway_outage(adapter,monkeypatch):
    adapter.catalog=None
    def offline(*args):raise PermissionError('network unavailable')
    monkeypatch.setattr(adapter,'rpc',offline)
    names={t['name'] for t in adapter.tools()}
    assert 'verification.observations' in names and 'knowledge.platform' not in names
    gap=adapter.call('knowledge.platform',{'repo_key':'ats'})
    assert gap['status']=='unavailable' and gap['reason']=='gateway_unavailable'
    assert gap['stage']=='discovery' and gap['evidence_status']=='not_obtained'
    assert adapter.call('verification.finding',{'mode':'schema'})['status']=='ok'


def test_registry_mode_schema_and_recovery(adapter):
    import jsonschema
    schema=next(t['inputSchema'] for t in adapter.tools(include_gateway=False) if t['name']=='verification.observations')
    bad={'mode':'read','ids':['a'*64,'b'*64],'budget':22000}
    from kp_agent_tooling_ops._impl.observation_contract import mode_errors
    assert mode_errors(bad)
    result=adapter.call('verification.observations',bad)
    assert result['status']=='invalid_arguments'
    assert [x['arguments']['id'] for x in result['next_calls']]==['a'*64,'b'*64]
    bad={'mode':'assess','ids':['a'*64],'expected_scope':{'subject':'ats::graph_query','sources':{'ats':'a'*40}}}
    result=adapter.call('verification.observations',bad)
    assert result['status']=='invalid_arguments'
    assert 'binding_sha256' in result['guidance']
    assert result['next_calls'][0]['arguments']=={'mode':'list'}
    assert adapter.call('verification.observations',{'mode':'list','id':'a'*64})['status']=='invalid_arguments'


def test_packet_internal_delivery_bypass_is_not_public(adapter):
    import jsonschema
    with pytest.raises(jsonschema.ValidationError):
        adapter.call('verification.packet',{'slice_id':'ats-pool-lifecycle','mode':'read','_internal':True})


def test_compact_serena_keeps_analysis_metadata_retrievable(adapter, monkeypatch, tmp_path):
    adapter.config['serena']={'command':'/provider/serena','python':'/provider/python','runtime_home':str(tmp_path)}
    monkeypatch.setattr('kp_agent_tooling._impl.service.agent_tooling.os.environ',{})
    monkeypatch.setattr('kp_agent_tooling._impl.service.agent_tooling.source',lambda *a:('b'*40,'VALUE=1'))
    def inspect(*a,**kw):
        assert kw['response_mode']=='compact' and kw['include_references'] is False
        return {'status':'ok','environment':{'inputs':{'config':'observed'}},'report':{'status':'ok','provider_identity':{'version':'fixture'},'citations':[{'retrieval':{'operation':'navigation.source','arguments':{'path':'a.py','target_revision':'0'*40,'start_line':1,'line_count':1}}}]}}
    monkeypatch.setattr('kp_agent_tooling._impl.service.agent_tooling.SerenaNavigationProvider.inspect',inspect)
    r=adapter.call('serena.inspect',{'repo_key':'ats','path':'a.py','symbol':'VALUE','response_mode':'compact','include_references':False})
    assert 'environment' not in r and 'import_context' not in r['report']
    assert r['report']['citations'][0]['retrieval']['arguments']['repo_key']=='ats'
    page=adapter.call('delivery.read',r['analysis_details']['next_call']['arguments'])
    metadata=json.loads(page['text'])
    assert metadata['environment']['inputs']['config']=='observed'
    assert metadata['provider_identity']['version']=='fixture'
