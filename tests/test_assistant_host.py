import json
from pathlib import Path
import pytest
from t12b_seams import drain  # T12b B4: the one drain helper
from kp_agent_tooling.assistant_host_cli import prepare, hook
from kp_agent_tooling._impl.service.desk_memory_runtime import components
from kp_agent_tooling._impl.service.episodic_memory_tools import from_config
from test_assistant_memory_slice import _desk, _private


def test_launch_hook_captures_indexes_and_replays_without_global_access(tmp_path):
    home, state, key, session = _desk(tmp_path,'assistant')
    transcripts=tmp_path/'transcripts'; transcripts.mkdir()
    contract=_private(home/'host.json',dict(schema_version='agent.assistant-host.v1',
        config_template=str(session('session-a','codex')),transcript_root=str(transcripts)))
    launch=prepare(contract, str(home), '__home_agent__:fixture:claude')
    receipt=json.loads(Path(launch['launch_receipt']).read_text())
    path=Path(receipt['transcript']); path.parent.mkdir()
    row={'type':'user','sessionId':launch['native_session_id'],
         'message':{'role':'user','content':'cedar isolation marker - verified capture'}}
    path.write_text(json.dumps(row)+'\n')
    payload=dict(session_id=launch['native_session_id'],cwd=str(home),transcript_path=str(path),hook_event_name='Stop')
    assert hook(launch['launch_receipt'],payload)=={'suppressOutput':True}
    drain(receipt['config'])  # T12b B4: the indexer, not the hook, indexes
    adapter=from_config(receipt['config'])
    try:
        found=adapter.call('memory.search',{'query':'cedar isolation marker'})
        assert found['results']
        before=adapter.call('memory.list',{'kind':'episodes'})
        hook(launch['launch_receipt'],payload)
        assert adapter.call('memory.list',{'kind':'episodes'})==before
        with pytest.raises(ValueError): hook(launch['launch_receipt'],dict(payload,session_id='wrong'))
        with pytest.raises(ValueError): hook(launch['launch_receipt'],dict(payload,transcript_path=str(tmp_path/'foreign')))
        assert components(receipt['config'])[0]['state_root']==str(state)
        assert set(json.loads(Path(launch['mcp_path']).read_text())['mcpServers'])=={'assistant-memory'}
    finally: adapter.close()
    with pytest.raises(ValueError): prepare(contract, str(tmp_path),'__home_agent__:fixture:claude')
    with pytest.raises(ValueError): prepare(contract,str(home),'ordinary-card')
