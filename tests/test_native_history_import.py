"""Synthetic native-history import contracts; no host history is opened."""
import json
import sqlite3

import pytest

from kp_agent_tooling._impl.service.episodic_memory import EpisodeConflict
from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
from kp_agent_tooling._impl.service.native_history_import import import_native_history
from kp_agent_tooling._impl.service.session_sources import SessionSources
from t12b_seams import drain  # T12b B4: the one drain helper
from test_portable_desk_memory import episode_store


PARENT = '12345678-1234-1234-1234-123456789abc'
CODEX = '87654321-4321-4321-4321-cba987654321'


def line(value):
    return (json.dumps(value, separators=(',', ':')) + '\n').encode()


def claude_row(text, *, session=PARENT, agent=None, timestamp='2026-09-23T12:00:00Z'):
    row = {'type':'user', 'sessionId':session, 'timestamp':timestamp,
           'message':{'role':'user','content':text}}
    if agent:
        row['agentId'] = agent
    return row


def selection(store, path, *, runtime='claude', apply=False, **kwargs):
    tenant = store.sessions.resolve('session-1', store.registry).tenant_id
    return import_native_history(store=store, tenant_id=tenant, runtime=runtime,
                                 files=[path], apply=apply, **kwargs)


def test_claude_preview_apply_replay_and_search(tmp_path):
    store = episode_store(tmp_path)
    source = tmp_path / f'{PARENT}.jsonl'
    source.write_bytes(line(claude_row('Cedar visible history')))
    before = store.path.read_bytes()
    preview = selection(store, source)
    assert preview['status'] == 'preview'
    assert preview['counts']['would_import'] == 1
    assert store.path.read_bytes() == before
    applied = selection(store, source, apply=True)
    assert applied['counts']['imported'] == 1
    assert applied['counts']['indexed_episodes'] == 1
    again = selection(store, source, apply=True)
    assert again['counts']['already_present'] == 1
    assert again['counts'].get('imported', 0) == 0
    drain(store)  # T12b B4: the indexer, not the import, indexes
    found = EpisodicSearchIndex(store.path.with_name('episode-search.sqlite3'),
                                episode_store=store).search('session-1', query='Cedar visible', scope='topic')
    assert found['results'][0]['attribution_status'] == 'unresolved'
    sid = found['results'][0]['source_session_id']
    assert SessionSources(store).metadata(sid)['native_id'] == PARENT
    event = SessionSources(store).read(found['results'][0]['episode_id'])
    provenance = event['source_provenance']
    assert provenance['source_artifact_sha256'] == applied['files'][0]['sha256']
    assert provenance['source_coordinates']['start'] == 0
    assert provenance['row_digest']


def test_child_header_is_parent_but_child_identity_is_preserved(tmp_path):
    store = episode_store(tmp_path)
    folder = tmp_path / PARENT / 'subagents'
    folder.mkdir(parents=True)
    source = folder / 'agent-child_7.jsonl'
    source.write_bytes(line(claude_row('Child cedar text', agent='child_7')))
    report = selection(store, source, apply=True)
    assert report['counts']['imported'] == 1
    with sqlite3.connect(store.path) as db:
        payload = json.loads(db.execute('SELECT payload FROM source_sessions').fetchone()[0])
        episode = json.loads(db.execute('SELECT payload FROM source_episodes').fetchone()[0])
    assert payload['native_id'] == PARENT + '/subagents/agent-child_7'
    assert episode['source_provenance']['native_parent_id'] == PARENT
    assert episode['source_provenance']['native_child_id'] == 'child_7'


def test_missing_identity_malformed_partial_and_changed_source(tmp_path):
    store = episode_store(tmp_path)
    source = tmp_path / f'{PARENT}.jsonl'
    good = line(claude_row('First immutable row'))
    source.write_bytes(good + b'{broken}\n' + line({'type':'user','message':{'role':'user','content':'no identity'}}) + b'{"type":"user"')
    preview = selection(store, source)
    assert preview['counts']['quarantined'] == 2
    assert preview['counts']['partial_trailing_lines'] == 1
    assert preview['files'][0]['next_offset'] == len(source.read_bytes()) - len(b'{"type":"user"')
    applied = selection(store, source, apply=True)
    assert applied['counts']['imported'] == 1
    old = source.read_bytes()
    source.write_bytes(line(claude_row('Changed immutable row')) + old[len(good):])
    with pytest.raises(EpisodeConflict, match='coordinate changed'):
        selection(store, source, apply=True)


def test_codex_metadata_project_date_and_rotation(tmp_path):
    store = episode_store(tmp_path)
    source = tmp_path / f'rollout-2026-09-23T12-00-00-{CODEX}.jsonl'
    meta = line({'type':'session_meta','timestamp':'2026-09-23T12:00:00Z',
                 'payload':{'id':CODEX,'cwd':'/synthetic/repo'}})
    visible = line({'type':'response_item','timestamp':'2026-09-23T12:01:00Z',
                    'payload':{'type':'message','role':'assistant','content':[
                        {'type':'output_text','text':'Codex cedar answer'}]}})
    source.write_bytes(meta + visible)
    preview = selection(store, source, runtime='codex', project='/synthetic/repo', day='2026-09-23')
    assert preview['counts']['would_import'] == 1
    assert selection(store, source, runtime='codex', project='/wrong', day='2026-09-23', apply=True)['counts']['quarantined'] == 2
    assert selection(store, source, runtime='codex', project='/synthetic/repo', day='2026-09-23', apply=True)['counts']['imported'] == 1
    source.write_bytes(meta + visible + line({'type':'event_msg','timestamp':'2026-09-23T12:02:00Z',
                                              'payload':{'type':'user_message','message':'appended cedar'}}))
    appended = selection(store, source, runtime='codex', project='/synthetic/repo', day='2026-09-23', apply=True)
    assert appended['counts']['already_present'] == 1
    assert appended['counts']['imported'] == 1
    # Replacing a file in place keeps its coordinate but changes sealed evidence.
    source.write_bytes(meta + visible.replace(b'Codex cedar answer', b'Codex maple answer'))
    with pytest.raises(EpisodeConflict):
        selection(store, source, runtime='codex', project='/synthetic/repo', day='2026-09-23', apply=True)


def test_folder_selection_is_shallow_project_date_and_bounded(tmp_path):
    store = episode_store(tmp_path)
    folder = tmp_path / 'native-project'
    folder.mkdir()
    source = folder / f'{PARENT}.jsonl'
    source.write_bytes(line(claude_row('Dated text')) + line(claude_row('Other day', timestamp='2026-09-22T12:00:00Z')))
    tenant = store.sessions.resolve('session-1', store.registry).tenant_id
    result = import_native_history(store=store, tenant_id=tenant, runtime='claude',
                                   source_folder=folder, project='native-project', day='2026-09-23')
    assert result['counts']['would_import'] == 1
    assert result['counts']['outside_date'] == 1
    with pytest.raises(ValueError, match='project and date'):
        import_native_history(store=store, tenant_id=tenant, runtime='claude',source_folder=folder)


def test_resume_cursor_append_and_prefix_change(tmp_path):
    store = episode_store(tmp_path)
    source = tmp_path / f'{PARENT}.jsonl'
    source.write_bytes(line(claude_row('First cedar row')))
    first = selection(store, source, apply=True)
    cursor = first['files'][0]['resume_cursor']
    with source.open('ab') as stream:
        stream.write(line(claude_row('Second cedar row')))
    resumed = selection(store, source, apply=True, cursors={str(source): cursor})
    assert resumed['counts']['imported'] == 1
    assert resumed['files'][0]['next_offset'] == source.stat().st_size
    assert selection(store, source, apply=True)['counts']['already_present'] == 2
    old = source.read_bytes()
    source.write_bytes(old.replace(b'First cedar', b'Other cedar'))
    with pytest.raises(ValueError, match='cursor prefix changed'):
        selection(store, source, cursors={str(source): cursor})


def test_codex_child_identity_and_possible_mirror_retains_both_rows(tmp_path):
    store = episode_store(tmp_path)
    source = tmp_path / f'rollout-2026-09-23T12-00-00-{CODEX}.jsonl'
    source.write_bytes(b''.join([
        line({'type':'session_meta','timestamp':'2026-09-23T12:00:00Z',
              'payload':{'id':CODEX,'session_id':PARENT,'parent_thread_id':PARENT,'cwd':'/repo'}}),
        line({'type':'event_msg','timestamp':'2026-09-23T12:01:00Z',
              'payload':{'type':'user_message','message':'Mirrored cedar prompt'}}),
        line({'type':'response_item','timestamp':'2026-09-23T12:01:01Z',
              'payload':{'type':'message','role':'user','content':[
                  {'type':'input_text','text':'Mirrored cedar prompt'}]}}),
    ]))
    report = selection(store, source, runtime='codex', apply=True)
    assert report['counts']['possible_mirror_rows'] == 1
    assert report['counts']['imported'] == 2
    with sqlite3.connect(store.path) as db:
        session = json.loads(db.execute('SELECT payload FROM source_sessions').fetchone()[0])
        episodes = [json.loads(row[0]) for row in db.execute('SELECT payload FROM source_episodes')]
    assert session['native_id'] == CODEX
    assert {episode['source_provenance']['native_parent_id'] for episode in episodes} == {PARENT}
    assert sorted(e['source_provenance']['possible_mirror_of_response_item'] for e in episodes) == [False, True]


def test_codex_repeated_identical_content_is_not_deduplicated(tmp_path):
    store = episode_store(tmp_path)
    source = tmp_path / f'rollout-2026-09-23T12-00-00-{CODEX}.jsonl'
    source.write_bytes(b''.join([
        line({'type':'session_meta','timestamp':'2026-09-23T12:00:00Z',
              'payload':{'id':CODEX,'cwd':'/repo'}}),
        line({'type':'event_msg','timestamp':'2026-09-23T12:01:00Z',
              'payload':{'type':'user_message','message':'yes'}}),
        line({'type':'event_msg','timestamp':'2026-09-23T12:02:00Z',
              'payload':{'type':'user_message','message':'yes'}}),
        line({'type':'response_item','timestamp':'2026-09-23T12:03:00Z',
              'payload':{'type':'message','role':'user','content':[
                  {'type':'input_text','text':'yes'}]}}),
    ]))
    report = selection(store, source, runtime='codex', apply=True)
    assert report['counts']['imported'] == 3
    assert report['counts']['possible_mirror_rows'] == 2
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes').fetchone()[0] == 3


def test_codex_copied_parent_header_cannot_supply_child_identity(tmp_path):
    store = episode_store(tmp_path)
    source = tmp_path / f'rollout-2026-09-23T12-00-00-{CODEX}.jsonl'
    source.write_bytes(b''.join([
        line({'type':'session_meta','timestamp':'2026-09-23T12:00:00Z',
              'payload':{'id':PARENT,'session_id':PARENT,'cwd':'/repo'}}),
        line({'type':'response_item','timestamp':'2026-09-23T12:00:01Z',
              'payload':{'type':'message','role':'assistant','content':[
                  {'type':'output_text','text':'wrong identity cedar'}]}}),
    ]))
    report = selection(store, source, runtime='codex', apply=True)
    assert report['counts']['quarantined'] == 2
    assert report['counts'].get('imported', 0) == 0
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM source_sessions').fetchone()[0] == 0


def test_codex_parent_header_after_child_quarantines_ambiguous_replay(tmp_path):
    store = episode_store(tmp_path)
    source = tmp_path / f'rollout-2026-09-23T12-00-00-{CODEX}.jsonl'
    source.write_bytes(b''.join([
        line({'type':'session_meta','timestamp':'2026-09-23T12:00:00Z',
              'payload':{'id':CODEX,'session_id':PARENT,'cwd':'/repo'}}),
        line({'type':'response_item','timestamp':'2026-09-23T12:00:01Z',
              'payload':{'type':'message','role':'assistant','content':[
                  {'type':'output_text','text':'certain child cedar'}]}}),
        line({'type':'session_meta','timestamp':'2026-09-23T12:00:02Z',
              'payload':{'id':PARENT,'cwd':'/repo'}}),
        line({'type':'response_item','timestamp':'2026-09-23T12:00:03Z',
              'payload':{'type':'message','role':'assistant','content':[
                  {'type':'output_text','text':'ambiguous parent cedar'}]}}),
    ]))
    report = selection(store, source, runtime='codex', apply=True)
    assert report['counts']['imported'] == 1
    assert report['counts']['quarantined'] == 2
    assert report['files'][0]['rejections'][-1]['reason'] == 'ambiguous_replayed_session_context'


def test_replay_rebuilds_missing_projection_and_bounds_file(tmp_path):
    """T12b R3 replacement (ruled exception, Verification, 2026-10-04; tests/t12b_ruled_exceptions.py): a replay
    of an applied import, with the index file gone, seals exactly once and `indexed_episodes == 1` comes from one
    drain, not inline: every write to the index (or a build file beside it) during the replay has the indexer
    (`drain`) on its stack, and the rebuilt index holds that one episode. The eight-megabyte bound is unchanged."""
    from t12b_seams import traced
    store = episode_store(tmp_path)
    source = tmp_path / f'{PARENT}.jsonl'
    source.write_bytes(line(claude_row('Index recovery cedar')))
    assert selection(store, source, apply=True)['counts']['imported'] == 1
    index = store.path.with_name('episode-search.sqlite3')
    if index.exists():
        index.unlink()
    with traced() as trace:
        replay = selection(store, source, apply=True)
    assert replay['counts']['already_present'] == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM source_episodes').fetchone()[0] == 1, 'the replay sealed again'
    writes = [e for e in trace.family() if e.write]
    assert writes and all(e.drain for e in writes), (
        f'the replay wrote the index outside the indexer: {[e.sql[:80] for e in writes if not e.drain][:3]}')
    assert replay['counts']['indexed_episodes'] == 1
    with sqlite3.connect(index) as db:
        assert db.execute('SELECT count(*) FROM indexed_episodes').fetchone()[0] == 1
    source.write_bytes(b'x' * 8_000_001)
    with pytest.raises(ValueError, match='eight-megabyte'):
        selection(store, source)


def test_relocated_rollout_deduplicates_by_native_identity_and_offset(tmp_path):
    store = episode_store(tmp_path)
    first_dir, second_dir = tmp_path / 'first', tmp_path / 'second'
    first_dir.mkdir(); second_dir.mkdir()
    source = first_dir / f'{PARENT}.jsonl'
    source.write_bytes(line(claude_row('Rotated cedar row')))
    assert selection(store, source, apply=True)['counts']['imported'] == 1
    relocated = second_dir / source.name
    relocated.write_bytes(source.read_bytes())
    replay = selection(store, relocated, apply=True)
    assert replay['counts']['already_present'] == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes').fetchone()[0] == 1
