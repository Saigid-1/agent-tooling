import io
import json
from pathlib import Path
import shlex
import sys

import pytest
from kp_agent_tooling._impl.service.claude_memory_hook import HookTelemetry,handle_hook,HookCaptureIncomplete
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
from t12b_seams import drain  # T12b B4: the one drain helper
from test_claude_capture_integration import _setup,_row,_write_rows


def setup(tmp_path):
    store,queue,capture=_setup(tmp_path)
    path=tmp_path/'session.jsonl'
    _write_rows(path,[_row('user','Keep the private rendezvous: Cedar.')])
    telemetry=HookTelemetry(tmp_path/'state/telemetry.sqlite3')
    telemetry.initialize()
    return dict(session='session-1',transcript_path=path,store=store,queue=queue,
                capture=capture,telemetry=telemetry)


def payload(args,event):
    return {'session_id':args['session'],'transcript_path':str(args['transcript_path']),
            'hook_event_name':event}


def test_small_stop_waits_and_precompact_flushes_with_content_free_telemetry(tmp_path):
    args=setup(tmp_path)
    result=handle_hook(payload(args,'Stop'),**args)
    assert result['status']=='not_due'
    assert 'current_window_occupancy_unavailable' in result['decision']['unknowns']
    assert args['queue'].list('session-1')['entries']==[]
    result=handle_hook(payload(args,'PreCompact'),**args)
    assert result['status']=='captured' and result['pending_source_bytes']==0
    assert result['queue']['counts']=={'queued':1}
    assert result['model_requests']==0
    assert 'private rendezvous' not in str(result)
    assert result['queue']['oldest_unfinished_age_seconds']>=0
    replay=handle_hook(payload(args,'PreCompact'),**args)
    assert replay['queue']['counts']=={'queued':1}
    assert len(args['telemetry'].recent(args['store'],'session-2'))==3
    assert args['telemetry'].recent(args['store'],'session-3')==[]


def test_partial_tail_cannot_report_successful_flush(tmp_path):
    args=setup(tmp_path)
    with args['transcript_path'].open('ab') as source:
        source.write(b'{"type":"user"')
    with pytest.raises(HookCaptureIncomplete):
        handle_hook(payload(args,'PreCompact'),**args)
    report=args['telemetry'].recent(args['store'],'session-1')[0]
    assert report['status']=='capture_incomplete' and report['pending_source_bytes']>0
    assert report['model_requests']==0


def test_hook_payload_cannot_rebind_session_or_file(tmp_path):
    args=setup(tmp_path)
    for field,value in [('session_id','session-2'),('transcript_path','/private/tmp/elsewhere.jsonl')]:
        data=payload(args,'PreCompact');data[field]=value
        with pytest.raises(ValueError,match='operator binding'):
            handle_hook(data,**args)
    assert args['queue'].list('session-1')['entries']==[]


def test_explicit_host_to_container_transcript_mapping_is_fixed_by_operator(tmp_path):
    args=setup(tmp_path)
    host_path=tmp_path/'host-visible'/'session.jsonl'
    request=payload(args,'PreCompact')
    request['transcript_path']=str(host_path)
    with pytest.raises(ValueError,match='operator binding'):
        handle_hook(request,**args)
    result=handle_hook(request,**args,hook_transcript_path=host_path)
    assert result['status']=='captured' and result['pending_source_bytes']==0
    request['transcript_path']=str(tmp_path/'different.jsonl')
    with pytest.raises(ValueError,match='operator binding'):
        handle_hook(request,**args,hook_transcript_path=host_path)


def test_stop_batch_and_session_end_flush(tmp_path):
    args=setup(tmp_path)
    _write_rows(args['transcript_path'],[_row('assistant','x'*1200)])
    result=handle_hook(payload(args,'Stop'),**args,source_batch_bytes=1000)
    assert result['decision']['reason']=='batch'
    _write_rows(args['transcript_path'],[_row('user','Small final correction.')])
    result=handle_hook(payload(args,'SessionEnd'),**args)
    assert result['decision']['reason']=='session_end' and result['pending_source_bytes']==0
    assert result['queue']['counts']=={'queued':2}


def test_cli_precompact_fails_loudly_and_settings_preserve_spaced_paths(tmp_path,monkeypatch,capsys):
    import kp_agent_tooling_ops.claude_hook_cli as cli
    args=setup(tmp_path)
    adapter=EpisodicMemoryTools(args['store'],'session-1')
    monkeypatch.setattr(cli,'from_config',lambda _:adapter)
    prefix=['claude_memory_hook','--memory-config',str(tmp_path/'session-1.json'),
            '--capture-ledger',str(args['capture'].path),'--queue',str(args['queue'].path),
            '--telemetry',str(args['telemetry'].path),'--transcript',str(args['transcript_path'])]
    monkeypatch.setattr(sys,'argv',prefix+['settings'])
    assert cli.main()==0
    settings=json.loads(capsys.readouterr().out)
    command=settings['hooks']['PreCompact'][0]['hooks'][0]['command']
    assert str(args['capture'].path) in shlex.split(command)
    monkeypatch.setattr(sys,'argv',prefix+['--host-command-prefix',
        '/usr/local/bin/docker exec -i ops-agent-tooling',
        '--hook-transcript-path',str(tmp_path/'host session.jsonl'),'settings'])
    assert cli.main()==0
    wrapped=json.loads(capsys.readouterr().out)
    command=wrapped['hooks']['SessionEnd'][0]['hooks'][0]['command']
    assert shlex.split(command)[:4]==['/usr/local/bin/docker','exec','-i','ops-agent-tooling']
    assert shlex.split(command)[4:6]==[sys.executable,str(Path(cli.__file__).resolve())]
    assert shlex.split(command)[-3:-1]==['--hook-transcript-path',str(tmp_path/'host session.jsonl')]
    with args['transcript_path'].open('ab') as stream:stream.write(b'partial')
    monkeypatch.setattr(sys,'argv',prefix+['hook'])
    monkeypatch.setattr(sys,'stdin',io.TextIOWrapper(io.BytesIO(json.dumps(payload(args,'PreCompact')).encode())))
    assert cli.main()==2
    error=json.loads(capsys.readouterr().err)
    assert error['category']=='HookCaptureIncomplete' and 'Cedar' not in str(error)


@pytest.mark.parametrize('event',['Stop','PreCompact','SessionEnd'])
def test_mapped_native_hook_flushes_small_turn_and_indexes(tmp_path,event):
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
    args=setup(tmp_path)
    capture=args['capture']
    capture.native_session_id='native'
    capture.index=EpisodicSearchIndex(tmp_path/'state/episode-search.sqlite3',episode_store=args['store'])
    capture.bind(args['session'],store=args['store'],transcript_path=args['transcript_path'])
    request=payload(args,event)
    with pytest.raises(ValueError):
        handle_hook(request,**args)
    request['session_id']='native'
    result=handle_hook(request,**args)
    assert result['pending_source_bytes']==0
    assert result['queue']['counts']=={'queued':1}
    drain(args['store'])  # T12b B4: the indexer, not the hook, indexes
    search=capture.index.search('session-2',query='Cedar')
    assert len(search['results'])==1
    assert search['covered_episodes']==1
    handle_hook(request,**args)
    assert args['queue'].metrics('session-1')['counts']=={'queued':1}


def test_mapped_large_attachment_does_not_block_short_stop(tmp_path):
    """T12b R3 replacement (ruled exception, Verification, 2026-10-04; tests/t12b_ruled_exceptions.py): the short
    stop still does not block behind a large attachment, and the seal never opens the index, so the test is not
    vacuous: the hook runs with the capture's index configured (as the hook CLIs configure it) under the Compose
    marker (tests/t12b_seams.py `compose_marker`), where the T10 P4 honesty instrument
    (`t10_instruments.recording().connected`) counts no connection to the index and at least one to the store; the
    next drain then indexes the turn. GREEN-IF all of that, with the old flush receipt (pending bytes 0, one queued
    job, two events, one omission)."""
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
    from t10_instruments import recording
    from t12b_seams import compose_marker
    args=setup(tmp_path)
    capture=args['capture'];capture.native_session_id='native'
    capture.bind(args['session'],store=args['store'],transcript_path=args['transcript_path'])
    _write_rows(args['transcript_path'],[{'type':'attachment','sessionId':'native','data':'x'*174934},
        {'type':'user','sessionId':'native','message':{'role':'user','content':'marker after attachment'}}])
    request=payload(args,'Stop');request['session_id']='native'
    index_path=tmp_path/'state/episode-search.sqlite3'
    capture.index=EpisodicSearchIndex(index_path,episode_store=args['store'])
    capture.index.initialize()
    with compose_marker(tmp_path), recording() as recorder:
        recorder.phase='call'
        result=handle_hook(request,**args)
    assert recorder.connected(args['store'].path,phases=('call',))>=1, 'positive control: the seal reached the store'
    assert recorder.connected(index_path,phases=('call',))==0, 'the seal opened the index'
    assert result['pending_source_bytes']==0
    assert result['queue']['counts']=={'queued':1}
    assert result['batches'][0]['omission_count']==1
    assert result['batches'][0]['event_count']==2
    assert capture.index.search('session-2',query='marker after attachment')['results']==[]
    drain(args['store'])  # T12b: the indexer, not the hook, indexes
    assert len(capture.index.search('session-2',query='marker after attachment')['results'])==1