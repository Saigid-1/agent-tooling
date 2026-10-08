"""Native child identity and parent-authored role claims remain separate from admission."""
from __future__ import annotations

import json
import sqlite3

import pytest

from kp_agent_tooling_ops._impl.service.delegation_attribution import (
    apply_codex_delegation, plan_codex_delegation,
)
from kp_agent_tooling._impl.service.desk_memory_runtime import components
from kp_agent_tooling._impl.service.session_sources import SessionSources
from kp_agent_tooling_ops.delegation_cli import main
from test_portable_desk_memory import bind, config, episode_store


PARENT = '11111111-1111-1111-1111-111111111111'
CHILD = '22222222-2222-2222-2222-222222222222'
OTHER = '33333333-3333-3333-3333-333333333333'
AT = '2026-09-24T17:00:00Z'


def native_file(tmp_path, identity, *, parent=None, path='/root/analyst'):
    source = ('vscode' if parent is None else
              {'subagent': {'thread_spawn': {
                  'parent_thread_id': parent, 'agent_path': path,
                  'depth': 1, 'agent_role': None}}})
    payload = {'id': identity, 'source': source,
               'session_id': identity if parent is None else parent}
    if parent is not None:
        payload['parent_thread_id'] = parent
    file = tmp_path / f'rollout-2026-09-24T17-00-00-{identity}.jsonl'
    file.write_text(json.dumps({'type': 'session_meta', 'timestamp': AT,
                                'payload': payload}) + '\n')
    return file


def arguments(tmp_path):
    return dict(parent_file=native_file(tmp_path, PARENT),
                child_file=native_file(tmp_path, CHILD, parent=PARENT),
                agent_path='/root/analyst', role_id='analyst',
                asserted_by='parent:operator-session',
                assignment_ref='parent-assignment:turn-17', recorded_at=AT)


def test_verified_native_child_is_distinct_from_inherited_parent_environment(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    monkeypatch.setenv('CODEX_THREAD_ID', OTHER)
    monkeypatch.setenv('CODEX_SESSION_ID', PARENT)
    preview = plan_codex_delegation(**args, approved_role_ids={'implementation'})
    assert preview['parent_native_id'] == PARENT
    assert preview['child_native_id'] == CHILD
    assert preview['role_catalog_status'] == 'unregistered_role_claim'
    assert preview['native_agent_role'] is None
    assert preview['authority'].endswith('no desk admission')
    assert len(preview['evidence']) == 3
    assert all(str(tmp_path) not in ref for ref in preview['evidence'])


def test_existing_catalog_role_identity_is_preserved_exactly(tmp_path):
    args = arguments(tmp_path)
    args['role_id'] = 'Verification'
    approved = plan_codex_delegation(**args, approved_role_ids={'Verification'})
    assert approved['assigned_role_id'] == 'Verification'
    assert approved['role_catalog_status'] == 'approved_catalog_role'
    args['role_id'] = 'Deployment Engineering'
    assert plan_codex_delegation(**args, approved_role_ids={'Deployment Engineering'})['assigned_role_id'] == 'Deployment Engineering'
    args['role_id'] = 'implementer'
    assert plan_codex_delegation(**args, approved_role_ids={'Verification'})['role_catalog_status'] == 'unregistered_role_claim'
    args['role_id'] = 'reviewer\nadmin'
    with pytest.raises(ValueError, match='control'):
        plan_codex_delegation(**args)


def test_explicit_source_ranges_must_match_selected_native_files_and_row_boundaries(tmp_path):
    args = arguments(tmp_path)
    child_range = {'native_id': CHILD, 'source_file': str(args['child_file']),
                   'start_offset': args['child_file'].stat().st_size}
    parent_range = {'native_id': PARENT, 'source_file': str(args['parent_file']),
                    'start_offset': args['parent_file'].stat().st_size}
    result = plan_codex_delegation(**args, child_source_range=child_range,
                                   parent_source_range=parent_range)
    assert result['child_source_range'] == child_range
    assert result['parent_source_range'] == parent_range
    with pytest.raises(ValueError, match='source range'):
        plan_codex_delegation(**args, child_source_range={**child_range, 'native_id': PARENT})
    with pytest.raises(ValueError, match='boundary'):
        plan_codex_delegation(**args, child_source_range={**child_range, 'start_offset': 1})


@pytest.mark.parametrize('tamper', ['wrong_parent', 'wrong_agent_path', 'same_file', 'mismatched_filename'])
def test_mismatched_or_fabricated_native_delegation_is_refused(tmp_path, tamper):
    args = arguments(tmp_path)
    if tamper == 'wrong_parent':
        args['child_file'] = native_file(tmp_path, CHILD, parent=OTHER)
    elif tamper == 'wrong_agent_path':
        args['agent_path'] = '/root/reviewer'
    elif tamper == 'same_file':
        args['child_file'] = args['parent_file']
    else:
        args['child_file'].rename(tmp_path / f'rollout-{OTHER}.jsonl')
        args['child_file'] = tmp_path / f'rollout-{OTHER}.jsonl'
    with pytest.raises(ValueError):
        plan_codex_delegation(**args)


def test_apply_claims_role_and_contribution_without_owner_or_child_admission(tmp_path):
    store = episode_store(tmp_path)
    sources = SessionSources(store)
    sources.upgrade()
    args = arguments(tmp_path)
    result = apply_codex_delegation(store=store, tenant_id='workspace-demo',
                                    approved_role_ids={'implementation'}, **args)
    child = sources.metadata(result['child_source_session_id'])
    parent = sources.metadata(result['parent_source_session_id'])
    assert child['native_id'] == CHILD and parent['native_id'] == PARENT
    assert child['desk_bindings'] == parent['desk_bindings'] == []
    assert child['attribution_status'] == parent['attribution_status'] == 'unresolved'
    assert {(claim['predicate'], claim['object']['id']) for claim in child['active_claims']} == {
        ('session.assigned_role', 'analyst'),
        ('session.delegated_from', result['parent_source_session_id']),
    }
    assert [(claim['predicate'], claim['object']['id']) for claim in parent['active_claims']] == [
        ('session.contributor', result['child_source_session_id'])]
    assert all(claim['valid_from'] == AT for claim in child['claims'] + parent['claims'])
    assert result['owner_claim_created'] is False and result['desk_admitted'] is False
    assert apply_codex_delegation(store=store, tenant_id='workspace-demo',
                                  approved_role_ids={'implementation'}, **args)['claim_ids'] == result['claim_ids']
    _, registry, ledger, _ = components(config(tmp_path, 'session-1', 'fixture'))
    with pytest.raises(Exception, match='no desk admission'):
        ledger.resolve(CHILD, registry)


def test_explicit_source_ranges_attach_to_matching_child_and_parent_claims(tmp_path):
    store = episode_store(tmp_path)
    sources = SessionSources(store)
    sources.upgrade()
    args = arguments(tmp_path)
    child_range = {'native_id': CHILD, 'source_file': str(args['child_file']),
                   'start_offset': args['child_file'].stat().st_size}
    parent_range = {'native_id': PARENT, 'source_file': str(args['parent_file']),
                    'start_offset': args['parent_file'].stat().st_size}
    receipt = apply_codex_delegation(
        store=store, tenant_id='workspace-demo', **args,
        child_source_range=child_range, parent_source_range=parent_range,
    )
    child = sources.metadata(receipt['child_source_session_id'])
    parent = sources.metadata(receipt['parent_source_session_id'])
    assert {json.dumps(claim['source_range'], sort_keys=True) for claim in child['claims']} == {
        json.dumps(child_range, sort_keys=True)}
    assert parent['claims'][0]['source_range'] == parent_range


def test_ranged_claims_do_not_relabel_older_linked_legacy_episode(tmp_path):
    store = episode_store(tmp_path)
    sources = SessionSources(store)
    sources.upgrade()
    legacy = store.capture('session-1', source_ref='legacy:before-delegation',
                           events=[{'event_id': 'old', 'role': 'user',
                                    'text': 'Earlier work without this role.'}])['episode_id']
    with sqlite3.connect(store.path) as db:
        source_session = db.execute(
            'SELECT session_id FROM session_episodes WHERE episode_id=?',
            (legacy,),
        ).fetchone()[0]
    native_id = sources.metadata(source_session)['native_id']
    range_ = {'native_id': native_id,
              'source_file': str(tmp_path / 'later-native.jsonl'),
              'start_offset': 100}
    common = dict(session_id=source_session, asserted_by='parent:operator',
                  recorded_at=AT, evidence=['parent-assignment:later'],
                  source_range=range_)
    sources.claim(predicate='session.assigned_role',
                  object={'kind': 'role', 'id': 'reviewer'}, **common)
    sources.claim(predicate='session.owner',
                  object={'kind': 'desk', 'id': store._binding('session-3')}, **common)

    assert {claim['predicate'] for claim in sources.metadata(source_session)['active_claims']} >= {
        'session.assigned_role', 'session.owner'}
    assert legacy not in sources.select('session-3', scope='desk')[0]
    assert legacy not in sources.select(
        'session-1', scope='desk',
        claims=[{'predicate': 'session.assigned_role',
                 'object': {'kind': 'role', 'id': 'reviewer'}}],
    )[0]
    assert store.read_event('session-1', episode_id=legacy, event_id='old')['text'] == \
        'Earlier work without this role.'


def test_same_parent_two_child_sessions_keep_distinct_role_claims(tmp_path):
    store = episode_store(tmp_path)
    sources = SessionSources(store)
    sources.upgrade()
    args = arguments(tmp_path)
    first = apply_codex_delegation(store=store, tenant_id='workspace-demo', **args)
    args.update(child_file=native_file(tmp_path, OTHER, parent=PARENT, path='/root/reviewer'),
                agent_path='/root/reviewer', role_id='reviewer',
                assignment_ref='parent-assignment:turn-18')
    second = apply_codex_delegation(store=store, tenant_id='workspace-demo', **args)
    assert first['parent_source_session_id'] == second['parent_source_session_id']
    assert first['child_source_session_id'] != second['child_source_session_id']
    assert {claim['object']['id'] for claim in sources.metadata(first['parent_source_session_id'])['active_claims']} == {
        first['child_source_session_id'], second['child_source_session_id']}


def test_interrupted_claim_sequence_reports_partial_state_and_exact_retry_repairs(tmp_path, monkeypatch):
    store = episode_store(tmp_path)
    sources = SessionSources(store)
    sources.upgrade()
    args = arguments(tmp_path)
    original = SessionSources.claim
    failed = False

    def interrupted(self, **kwargs):
        nonlocal failed
        if kwargs['predicate'] == 'session.delegated_from' and not failed:
            failed = True
            raise OSError('synthetic interrupted write')
        return original(self, **kwargs)

    monkeypatch.setattr(SessionSources, 'claim', interrupted)
    with pytest.raises(RuntimeError, match='partially recorded; retry with identical arguments'):
        apply_codex_delegation(store=store, tenant_id='workspace-demo', **args)
    repaired = apply_codex_delegation(store=store, tenant_id='workspace-demo', **args)
    assert len(sources.metadata(repaired['child_source_session_id'])['claims']) == 2
    assert len(sources.metadata(repaired['parent_source_session_id'])['claims']) == 1


def test_operator_cli_preview_then_apply_uses_same_native_proof(tmp_path, capsys):
    store = episode_store(tmp_path)
    SessionSources(store).upgrade()
    args = arguments(tmp_path)
    operator_config = config(tmp_path, PARENT, 'fixture')
    bind(operator_config)
    cli = ['--config', str(operator_config)]
    for name, value in args.items():
        cli += ['--' + name.replace('_', '-'), str(value)]
    assert main(cli) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['applied'] is False
    assert preview['parent_identity_verification_basis'] == 'native_header_and_configured_operator_session'
    assert main(cli + ['--apply']) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied['applied'] is True
    assert applied['child_native_id'] == preview['child_native_id']
    assert SessionSources(store).metadata(applied['child_source_session_id'])['desk_bindings'] == []


def test_cli_requires_explicit_historical_parent_selection(tmp_path, capsys):
    store = episode_store(tmp_path)
    SessionSources(store).upgrade()
    args = arguments(tmp_path)
    cli = ['--config', str(config(tmp_path, 'session-1', 'fixture'))]
    for name, value in args.items():
        cli += ['--' + name.replace('_', '-'), str(value)]
    assert main(cli) == 1
    assert 'differs from configured operator session' in capsys.readouterr().err
    assert main(cli + ['--historical-parent']) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['parent_identity_verification_basis'] == 'native_header_and_explicit_historical_parent_selection'
