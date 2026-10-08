import json
from pathlib import Path
import pytest
from kp_agent_tooling._impl.service.desk_memory_runtime import initialize, admit, from_local_config, components
from kp_agent_tooling._impl.service.desk_binding import DeskLaunchConflict, DeskLaunchUnavailable


def config(tmp_path, session='claude_session', instance='claude', role='coordinator'):
    root=tmp_path/'state'; root.mkdir(mode=0o700,exist_ok=True)
    registry=tmp_path/'catalog.json'
    if not registry.exists():
        import shutil
        source=Path(__file__).resolve().parents[1]/'config/desk-context'
        shutil.copyfile(source/'doctrine.md',tmp_path/'doctrine.md')
        registry.write_text((source/'catalog.example.json').read_text()); registry.chmod(0o600)
    cfg=tmp_path/(session+'.json')
    cfg.write_text(json.dumps({'schema_version':'ops.desk-memory.local.v1','state_root':str(root),
        'catalog_path':str(registry),'workspace_root':str(tmp_path),'provider_instance':instance,'provider_session_id':session}));cfg.chmod(0o600)
    return cfg


def bind(path,role='implementation',model='model-a'):
    return admit(path,desk_id=role+'-desk',provider_id='test',model_id=model)



def episode_store(tmp_path):
    """Reusable portable admission: two same-desk sessions and one other role."""
    first = config(tmp_path, 'session-1', 'fixture')
    initialize(first)
    bind(first, model='model-one')
    bind(config(tmp_path, 'session-2', 'fixture'), model='model-two')
    bind(config(tmp_path, 'session-3', 'fixture'), 'verification', model='model-one')
    return components(first)[3]


def test_cross_harness_same_desk_and_scope_isolation(tmp_path):
    a=config(tmp_path); initialize(a); bind(a)
    _,_,_,store=components(a)
    receipt=store.capture('claude_session',source_ref='transcript:1',events=[{'event_id':'1','role':'user','text':'Retain exact evidence.'}])
    b=config(tmp_path,'codex_session','codex'); bind(b,model='different-model')
    tools=from_local_config(b)
    assert tools.call('memory.read_event',{'episode_id':receipt['episode_id'],'event_id':'1'})['text']=='Retain exact evidence.'
    c=config(tmp_path,'other','codex');bind(c,'verification')
    with pytest.raises(Exception,match='unavailable'):
        from_local_config(c).call('memory.read_event',{'episode_id':receipt['episode_id'],'event_id':'1'})
    assert not any(t['name'].startswith(('dispatch','desk.admit')) for t in tools.tools())


def test_rebinding_denied_and_registry_withdrawal_is_live(tmp_path):
    p=config(tmp_path);initialize(p);bind(p)
    with pytest.raises(DeskLaunchConflict):bind(p,'verification')
    adapter=from_local_config(p)
    registry=tmp_path/'catalog.json'; data=json.loads(registry.read_text()); data['desks']=[]; data['team']['roles']=[]; registry.write_text(json.dumps(data))
    with pytest.raises((DeskLaunchUnavailable, ValueError)):adapter.call('memory.status',{})


def test_missing_state_is_not_recreated(tmp_path):
    p=config(tmp_path);initialize(p);bind(p)
    (tmp_path/'state/sessions.sqlite3').unlink()
    with pytest.raises(DeskLaunchUnavailable):from_local_config(p)
    assert not (tmp_path/'state/sessions.sqlite3').exists()


def test_private_configuration_and_no_ambiguous_identity(tmp_path):
    p=config(tmp_path);p.chmod(0o644)
    with pytest.raises(ValueError,match='0600'):initialize(p)
    from kp_agent_tooling._impl.service.desk_identity import binding_key
    with pytest.raises(ValueError):binding_key(tenant_id='a|b',role='c',repo_key='d')


def test_admission_preserves_context_and_detects_tampering(tmp_path):
    import sqlite3
    from kp_agent_tooling._impl.service.desk_memory_runtime import read_context
    p=config(tmp_path);initialize(p); original=bind(p)['context']
    assert read_context(p)==original
    with sqlite3.connect(tmp_path/'state/sessions.sqlite3') as db:
        body=dict(original);body['doctrine_text']='tampered'
        db.execute('UPDATE desk_contexts SET context=?',(json.dumps(body),))
    with pytest.raises(ValueError,match='integrity'):
        from_local_config(p)


def test_catalog_change_does_not_rewrite_admitted_context(tmp_path):
    from kp_agent_tooling._impl.service.desk_memory_runtime import read_context
    p=config(tmp_path);initialize(p);original=bind(p)['context']
    catalog=tmp_path/'catalog.json';data=json.loads(catalog.read_text())
    data['project']['source_revision']='b'*40;catalog.write_text(json.dumps(data))
    assert read_context(p)==original
    assert bind(p)['context']==original


def test_claude_hook_to_successor_exact_recovery_and_revocation(tmp_path):
    """One local composition trial across admission, hook, queue and read tools."""
    from kp_agent_tooling._impl.service.claude_episode_capture import ClaudeEpisodeCapture
    from kp_agent_tooling._impl.service.claude_memory_hook import HookTelemetry, handle_hook
    from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue

    first = config(tmp_path, 'claude-first', 'claude-local')
    initialize(first)
    bind(first)
    _, _, _, store = components(first)
    queue = ConsolidationQueue(tmp_path/'state/queue.sqlite3', store=store)
    queue.initialize()
    capture = ClaudeEpisodeCapture(tmp_path/'state/capture.sqlite3')
    capture.initialize()
    telemetry = HookTelemetry(tmp_path/'state/hooks.sqlite3')
    telemetry.initialize()
    transcript = tmp_path/'claude-first.jsonl'
    transcript.write_text(json.dumps({'sessionId':'claude-first','type':'user',
        'message':{'role':'user','content':'Trial marker: Cedar 04:17 UTC.'}})+'\n')
    result = handle_hook({'session_id':'claude-first','transcript_path':str(transcript),
        'hook_event_name':'SessionEnd'},session='claude-first',transcript_path=transcript,
        store=store,queue=queue,capture=capture,telemetry=telemetry)
    assert result['status']=='captured' and result['pending_source_bytes']==0
    assert result['queue']['counts']=={'queued':1}
    receipt = capture.receipts('claude-first', store=store)[0]
    assert receipt['state']=='captured' and receipt['event_count']==1

    successor = config(tmp_path, 'claude-next', 'claude-local')
    bind(successor, model='model-b')
    tools = from_local_config(successor)
    episode = receipt['episode_id']
    assert tools.call('memory.list', {'kind':'episodes'})['entries'][0]['episode_id']==episode
    event = tools.call('memory.episode_directory', {'episode_id':episode})['entries'][0]
    source = tools.call('memory.read_event', {'episode_id':episode,'event_id':event['event_id']})
    quote = 'Cedar 04:17 UTC'
    start = source['text'].index(quote)
    proposed = tools.call('memory.propose', {'episode_ids':[episode],
        'items':[{'kind':'observation','text':'Recovery marker was recorded.',
                  'citations':[{'episode_id':episode,'event_id':event['event_id'],
                                'start':start,'end':start+len(quote),'quote':quote}]}],
        'unresolved_questions':[]})
    capsule = proposed['capsule_id']
    assert tools.call('memory.resume', {'capsule_id':capsule})['complete_handoff']
    citation = tools.call('memory.handoff', {'capsule_id':capsule})['items'][0]['citations'][0]
    exact = tools.call('memory.read_event', {'episode_id':episode,
        'event_id':citation['event_id'],'start':citation['start'],
        'length':citation['end']-citation['start']})
    assert exact['text']==citation['quote']==quote

    other = config(tmp_path, 'other-role', 'claude-local')
    bind(other, 'verification')
    with pytest.raises(Exception, match='unavailable'):
        from_local_config(other).call('memory.read_event', {'episode_id':episode,
            'event_id':event['event_id']})
    catalog = tmp_path/'catalog.json'
    data = json.loads(catalog.read_text())
    data['desks'] = [desk for desk in data['desks'] if desk['role_id']!='implementation']
    data['team']['roles'] = [role for role in data['team']['roles']
                             if role['role_id']!='implementation']
    catalog.write_text(json.dumps(data))
    with pytest.raises(DeskLaunchUnavailable):
        tools.call('memory.read_event', {'episode_id':episode,'event_id':event['event_id']})


def test_quiescent_five_database_backup_restores_exact_source_and_scope(tmp_path):
    import hashlib
    import shutil
    import sqlite3
    from kp_agent_tooling._impl.service.claude_episode_capture import ClaudeEpisodeCapture
    from kp_agent_tooling._impl.service.claude_memory_hook import HookTelemetry, handle_hook
    from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue

    first=config(tmp_path,'backup-first','claude-local');initialize(first);bind(first)
    successor=config(tmp_path,'backup-next','claude-local');bind(successor)
    other=config(tmp_path,'backup-other','claude-local');bind(other,'verification')
    _,_,_,store=components(first)
    queue=ConsolidationQueue(tmp_path/'state/queue.sqlite3',store=store);queue.initialize()
    capture=ClaudeEpisodeCapture(tmp_path/'state/capture.sqlite3');capture.initialize()
    telemetry=HookTelemetry(tmp_path/'state/hooks.sqlite3');telemetry.initialize()
    transcript=tmp_path/'backup-first.jsonl'
    transcript.write_text(json.dumps({'sessionId':'backup-first','type':'user',
        'message':{'role':'user','content':'Restore marker: Willow 05:29 UTC.'}})+'\n')
    report=handle_hook({'session_id':'backup-first','transcript_path':str(transcript),
        'hook_event_name':'SessionEnd'},session='backup-first',transcript_path=transcript,
        store=store,queue=queue,capture=capture,telemetry=telemetry)
    assert report['status']=='captured' and report['pending_source_bytes']==0
    receipt=capture.receipts('backup-first',store=store)[0]
    episode=receipt['episode_id']
    live=from_local_config(successor)
    event=live.call('memory.episode_directory',{'episode_id':episode})['entries'][0]
    quote='Willow 05:29 UTC'
    source=live.call('memory.read_event',{'episode_id':episode,'event_id':event['event_id']})['text']
    start=source.index(quote)
    citation={'episode_id':episode,'event_id':event['event_id'],'start':start,
              'end':start+len(quote),'quote':quote}
    capsule=live.call('memory.propose',{'episode_ids':[episode],
        'items':[{'kind':'observation','text':'Restore marker seen.','citations':[citation]}],
        'unresolved_questions':[]})['capsule_id']
    # All writers are now finished. Each SQLite backup is taken while the entire
    # five-database state is quiescent; no online cross-database snapshot is claimed.
    root=tmp_path/'restored';root.mkdir(mode=0o700)
    restored_state=root/'state';restored_state.mkdir(mode=0o700)
    names=('sessions.sqlite3','episodes.sqlite3','queue.sqlite3',
           'capture.sqlite3','hooks.sqlite3')
    hashes={}
    for name in names:
        original=tmp_path/'state'/name
        restored=restored_state/name
        with sqlite3.connect(original) as source_db, sqlite3.connect(restored) as target_db:
            source_db.backup(target_db)
            assert target_db.execute('PRAGMA integrity_check').fetchone()==('ok',)
        restored.chmod(0o600)
        hashes[name]=hashlib.sha256(restored.read_bytes()).hexdigest()
    assert len(hashes)==5 and all(len(digest)==64 for digest in hashes.values())
    shutil.copyfile(tmp_path/'catalog.json',root/'catalog.json')
    (root/'catalog.json').chmod(0o600)
    shutil.copyfile(tmp_path/'doctrine.md',root/'doctrine.md')

    def restored_config(original):
        body=json.loads(original.read_text())
        body.update(state_root=str(restored_state),catalog_path=str(root/'catalog.json'),
                    workspace_root=str(root))
        path=root/original.name
        path.write_text(json.dumps(body));path.chmod(0o600)
        return path

    next_restored=from_local_config(restored_config(successor))
    other_restored=from_local_config(restored_config(other))
    recovered=next_restored.call('memory.read_event',{'episode_id':episode,
        'event_id':event['event_id'],'start':start,'length':len(quote)})
    assert recovered['text']==quote
    assert next_restored.call('memory.handoff',{'capsule_id':capsule})['items'][0]['citations'][0]==citation
    with pytest.raises(Exception,match='unavailable'):
        other_restored.call('memory.read_event',{'episode_id':episode,'event_id':event['event_id']})
    catalog=root/'catalog.json';data=json.loads(catalog.read_text())
    data['desks']=[desk for desk in data['desks'] if desk['role_id']!='implementation']
    data['team']['roles']=[role for role in data['team']['roles'] if role['role_id']!='implementation']
    catalog.write_text(json.dumps(data))
    with pytest.raises(DeskLaunchUnavailable):
        next_restored.call('memory.read_event',{'episode_id':episode,'event_id':event['event_id']})


def test_queue_retains_safe_partial_transport_and_never_retries(tmp_path):
    from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
    from kp_agent_tooling._impl.service.episodic_summarizer import SummaryUnavailable
    from test_episodic_summarizer import configured
    path=config(tmp_path);initialize(path);bind(path)
    _,_,_,store=components(path)
    ep=store.capture('claude_session',source_ref='fixture:transport',events=[
        {'event_id':'one','role':'user','text':'Keep the pending decision.'}])['episode_id']
    queue=ConsolidationQueue(tmp_path/'state/queue.sqlite3',store=store);queue.initialize()
    job=queue.enqueue('claude_session',episode_ids=[ep])
    class Failed:
        calls=0
        last_receipt=None
        def __call__(self,packet):
            self.calls+=1
            self.last_receipt={'schema_version':'ops.summary-call.v1','prompt_version':'desk-handoff-v2',
                'transport':{'stage':'reading_body','http_status':200,'headers_received_ms':7,
                    'response_ids':{'x-request-id':'req-test','authorization':'secret','cf-ray':'bad\nvalue'},
                    'body':'secret','response_bytes':10}}
            raise SummaryUnavailable('timeout',category='provider_timeout')
    proposer=Failed();budget=configured(lambda *a:None).budget
    with pytest.raises(SummaryUnavailable):
        queue.run_once('claude_session','test',propose=proposer,budget=budget,approve_sources=lambda _:True)
    record=queue.get('claude_session',job['job_id'])
    assert record['state']=='needs_review'
    receipt=record['attempts'][0]['receipt']
    assert receipt['transport']=={'stage':'reading_body','http_status':200,'headers_received_ms':7,
        'response_ids':{'x-request-id':'req-test'},'response_bytes':10}
    assert 'secret' not in json.dumps(receipt)
    assert queue.run_once('claude_session','test',propose=proposer,budget=budget,approve_sources=lambda _:True) is None
    assert proposer.calls==1
