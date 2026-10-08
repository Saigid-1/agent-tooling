"""T12b B2: an in-process Compose-semantics control (meet item 6, Verification's ruling, 2026-10-04).

Order: docs/work/orders/T12b-one-indexer-outbox.md, B2: "Inside Compose, sealers never drain. The `indexer` role is
the only drainer. Inside Compose means `AGENT_MEMORY_VOLUME` is set ... No hook, capture pass, board process or
gateway in the runtime opens the index." Every other in-process test runs with the marker unset (host semantics,
where a sealer drains once after its seal); this one runs each former inline site under the marker, so a regression
that drains inline inside Compose is red in-process, not only in the image suite.

The marker: tests/t12b_seams.py `compose_marker(root)`, which sets AGENT_MEMORY_VOLUME and points the leaf's volume
root (`leaf.STORE_ROOT`, '/state/memory' in a role) at this test's temporary root, so T11b's store-path check
(`leaf.check_store`) passes for the stores under it instead of refusing them. Nothing else is patched: the product's
own runtime predicate (`leaf.docker_runtime()`) reads the real variable.

Sites (each in its own state root, its index created empty before the seal, so a writer that touches only an
existing index is exercised too): W1 the Claude hook (`handle_hook` with the capture's index configured, as the hook
CLIs configure it), W4 the model gateway (`record_in_memory`), W5 a workspace capture pass (`once`), W6 a session
import job (`advance`), W7 a host card (`attach_card --apply`).

GREEN-IF, per site, under the marker: the T10 honesty instrument (t10_instruments `recording().connected`) counts no
connection to the index file (and at least one to the store: positive control); every call of the product's
`drain_after_seal` (wherever a product module bound it) returned None, and the site called it at least once; the
seal's outbox rows are still there after the site returns; and only the drain this test then calls (marker unset)
applies them: the outbox is empty after it and every sealed episode is indexed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import t10_corpus as corpus
import t10_world as w
import t12b_seams as seams
from t10_instruments import recording

SITES = ('W1 claude hook', 'W4 gateway', 'W5 workspace capture pass', 'W6 session import advance', 'W7 host card')


class Site:
    def __init__(self, store, run):
        self.store, self.run = store, run

    @property
    def store_path(self):
        return Path(str(self.store.path))

    @property
    def index_path(self):
        return seams.index_path_of(self.store)


def _claude_hook(root):
    from test_claude_capture_integration import _row, _setup, _write_rows
    from kp_agent_tooling._impl.service.claude_memory_hook import HookTelemetry, handle_hook
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
    import t12b_flows as flows
    root.mkdir(parents=True)
    store, queue, capture = _setup(root)
    flows.ensure_index(store)
    capture.index = EpisodicSearchIndex(seams.index_path_of(store), episode_store=store)
    transcript = root / 'session.jsonl'
    _write_rows(transcript, [_row('user', 'Compose hook juniper.'), _row('assistant', 'Noted.')])
    telemetry = HookTelemetry(root / 'state/telemetry.sqlite3')
    telemetry.initialize()
    payload = {'session_id': 'session-1', 'transcript_path': str(transcript), 'hook_event_name': 'PreCompact'}
    return Site(store, lambda: handle_hook(payload, session='session-1', transcript_path=transcript, store=store,
                                          queue=queue, capture=capture, telemetry=telemetry))


def _gateway(root):
    from kp_agent_tooling._impl.service.model_gateway import record_in_memory
    import t12b_flows as flows
    world = w.World(root, corpus.DESKS, corpus.SESSIONS)
    world.initialize()
    store = world.store()
    flows.ensure_index(store)
    config = world.configs[corpus.A]
    provenance = {'call_id': 'call-t12b-compose', 'capability': 'text', 'provider': 'fixture', 'model': 'fixture'}

    def run():
        result = record_in_memory(config, provenance)
        assert result.get('status') == 'recorded', f'precondition: the gateway recorded nothing: {result}'
        return result
    return Site(store, run)


def _flow(name):
    def build(root):
        import t12b_flows as flows
        flow = flows.SETUPS[name](root)
        return Site(flow.store, flow.run)
    return build


def _host_card(root):
    from test_host_card_bridge import setup
    from kp_agent_tooling._impl.service.desk_memory_runtime import components
    from kp_agent_tooling._impl.service.host_card_bridge import attach_card
    import t12b_flows as flows
    root.mkdir(parents=True)
    config, event = setup(root)
    store = components(config)[3]
    flows.ensure_index(store)
    return Site(store, lambda: attach_card(config, event, apply=True))


BUILDERS = {'W1 claude hook': _claude_hook, 'W4 gateway': _gateway,
            'W5 workspace capture pass': _flow('workspace capture once'),
            'W6 session import advance': _flow('session import advance'), 'W7 host card': _host_card}


@pytest.mark.parametrize('site', SITES)
def test_b2_inside_compose_a_former_inline_site_seals_and_never_opens_the_index(tmp_path, site):
    root = tmp_path / 'root'
    built = BUILDERS[site](root)
    assert built.index_path.exists(), 'precondition: the index exists before the seal'
    before = seams.outbox_count(built.store_path)
    with seams.compose_marker(tmp_path), seams.drain_after_seal_spy() as calls, recording() as recorder:
        recorder.phase = 'call'
        built.run()
    sealed = seams.outbox_count(built.store_path)
    index_connects = recorder.connected(built.index_path, phases=('call',))
    problems = []
    if recorder.connected(built.store_path, phases=('call',)) < 1:
        problems.append('positive control: the site never connected to its store')
    if index_connects:
        problems.append(f'inside Compose the site connected to the index {index_connects} times')
    if not calls:
        problems.append('positive control: the site never reached drain_after_seal')
    if any(value is not None for _, value in calls):
        problems.append(f'drain_after_seal returned {[value for _, value in calls]} inside Compose (None expected)')
    if sealed <= before:
        problems.append(f'the seal\'s outbox rows are not waiting after the site: {before} -> {sealed}')
    assert not problems, f'{site} under the Compose marker:\n' + '\n'.join(problems)
    seams.drain(built.store)
    with seams.ro(built.store_path) as db:
        episodes = {row[0] for row in db.execute('SELECT id FROM episodes UNION SELECT id FROM source_episodes')}
    with seams.ro(built.index_path) as db:
        indexed = {row[0] for row in db.execute('SELECT episode_id FROM indexed_episodes')}
    assert seams.outbox_count(built.store_path) == 0, 'the test\'s drain left outbox rows'
    assert episodes and episodes <= indexed, (f'after the test\'s drain {len(episodes - indexed)} of {len(episodes)} '
                                              'sealed episodes are not indexed')
