"""T2 P2: a binding admits exactly one session.

After ``bind``, the ``kp-agent-memory`` MCP with a config for that exact
``native_session_id`` can call ``memory.search`` and ``memory.propose`` for that
desk. A config for any other session ID, or a desk the session is not bound to,
is refused. No MCP tool (``tools/list`` of ``kp-agent-memory`` and of
``kp-agent-tooling``) creates bindings.

Readings (reported under AMBIGUITY): "a desk the session is not bound to is
refused" is tested as refusal to re-bind the session to that desk and refusal
of ``memory.propose`` targeting it. Same-tenant cross-desk reads are an
existing, tested feature and are not asserted either way. ``bind`` stdin is
exactly the nine fields the order lists for ``agent-tooling.session-binding.v1``.
"""
import json
import re
from datetime import datetime

from t12b_seams import drain  # T12b B4: the one drain helper
from t2_harness import (BINDING_FIELDS, BINDING_SCHEMA, Registry, binding_key, capture_card,
                        find_record, mcp, private_json, proposal_from_hit)

ROSTER = [{'role_id': 'Scribe', 'label': 'Scribe', 'purpose': 'Record decisions as they are made.'},
          {'role_id': 'Curator', 'label': 'Curator', 'purpose': 'Keep the shared archive tidy.'}]

# Properties a tool would need in order to create or alter a binding or an admission.
BINDING_INPUTS = {'native_session_id', 'desk_id', 'harness', 'parent_session_id',
                  'provider_session_id'}


def _two_desks(tmp_path):
    world = Registry.create(tmp_path, ROSTER)
    first, run = world.save_desk(role='Scribe', name='First desk')
    run.ok()
    second, run = world.save_desk(role='Curator', name='Second desk')
    run.ok()
    return world, first['desk_id'], second['desk_id']


def _properties(schema, found):
    if isinstance(schema, dict):
        for name, value in (schema.get('properties') or {}).items():
            found.add(name)
            _properties(value, found)
        for key in ('items', 'anyOf', 'oneOf', 'allOf', 'additionalProperties'):
            value = schema.get(key)
            for child in (value if isinstance(value, list) else [value]):
                _properties(child, found)
    return found


def test_bind_admits_that_exact_session_and_refuses_any_other(tmp_path):
    world, first, _ = _two_desks(tmp_path)
    bound, other = 'bound-session-1', 'unbound-session-2'
    world.bind(desk_id=first, session=bound).ok()
    receipt = capture_card(world.session_config(bound), session=bound,
                           text='Cedar lantern exact session evidence')
    drain(world.session_config(bound))  # T12b B4: the indexer, not the capture, indexes

    _, replies = mcp(world.session_config(bound), calls=[
        ('memory.connection_status', {}),
        ('memory.search', {'query': 'Cedar lantern'}),
        ('memory.propose', lambda r: proposal_from_hit(
            next(h for h in r[1].value['results'] if h['episode_id'] == receipt['episode_id']))),
    ])
    assert [r.is_error for r in replies] == [False, False, False], [r.value for r in replies]
    assert replies[0].value['status'] == 'ready'
    assert replies[1].value['binding_key'] == binding_key(world.tenant, first, '*')

    # Same state, same registry, same harness instance: only the session ID differs.
    _, denied = mcp(world.session_config(other), list_tools=False, calls=[
        ('memory.connection_status', {}),
        ('memory.search', {'query': 'Cedar lantern'}),
        ('memory.propose', proposal_from_hit({'episode_id': receipt['episode_id'], 'event_id': 'host-card',
                                              'start': 0, 'end': 1, 'quote': '{'})),
    ])
    assert denied[0].value['status'] != 'ready'
    assert denied[1].is_error and denied[2].is_error


def test_bound_session_cannot_be_rebound_or_write_to_another_desk(tmp_path):
    world, first, second = _two_desks(tmp_path)
    session = 'bound-session-1'
    world.bind(desk_id=first, session=session).ok()
    # Moving an admitted session to another desk is refused, whatever the source.
    world.bind(desk_id=second, session=session).refused()
    world.bind(desk_id=second, session=session, source='board').refused()

    config = world.session_config(session)
    receipt = capture_card(config, session=session, text='Cedar lantern first desk only')
    drain(config)  # T12b B4: the indexer, not the capture, indexes
    first_key = binding_key(world.tenant, first, '*')
    second_key = binding_key(world.tenant, second, '*')
    _, replies = mcp(config, calls=[
        ('memory.bindings', {}),
        ('memory.search', {'query': 'Cedar lantern'}),
        ('memory.propose', lambda r: dict(proposal_from_hit(
            next(h for h in r[1].value['results'] if h['episode_id'] == receipt['episode_id'])),
            target_binding_key=second_key)),
        ('memory.list', {'kind': 'capsules', 'binding_key': second_key}),
    ])
    bindings, found, proposed, capsules = replies
    assert {b['binding_key']: (b['own'], b['writable']) for b in bindings.value['bindings']} == {
        first_key: (True, True), second_key: (False, False)}
    assert found.value['binding_key'] == first_key
    assert proposed.is_error and proposed.value['category'] == 'operation_not_permitted'
    assert capsules.is_error or capsules.value['total'] == 0

    # A second session may be bound to the second desk; each keeps its own admission.
    world.bind(desk_id=second, session='bound-session-2').ok()
    _, replies = mcp(world.session_config('bound-session-2'), list_tools=False,
                     calls=[('memory.bindings', {})])
    assert [b['binding_key'] for b in replies[0].value['bindings'] if b['own']] == [second_key]


def test_no_mcp_tool_creates_or_alters_bindings(tmp_path):
    world, first, _ = _two_desks(tmp_path)
    session = 'bound-session-1'
    world.bind(desk_id=first, session=session).ok()
    memory_tools, _ = mcp(world.session_config(session))
    tooling = private_json(tmp_path / 'tooling.json', {'schema_version': 'ops.agent-tooling.v1', 'repos': {}})
    tooling_tools, _ = mcp(tooling, script='kp-agent-tooling')
    assert memory_tools and tooling_tools
    for tool in memory_tools + tooling_tools:
        name = tool['name']
        if name != 'memory.bindings':  # read-only listing of registered bindings
            assert not re.search(r'bind|admi(t|ssion)|enrol', name, re.IGNORECASE), name
        inputs = _properties(tool.get('inputSchema') or {}, set())
        assert not inputs & BINDING_INPUTS, (name, sorted(inputs & BINDING_INPUTS))


def test_bind_records_a_bounded_session_binding_for_a_registered_desk(tmp_path):
    world, first, _ = _two_desks(tmp_path)
    request = world.binding_request(desk_id=first, session='record-session-1', source='board',
                                    parent='parent-session-0')
    run = world.registry('bind', request)
    output = run.ok()
    assert BINDING_SCHEMA in run.stdout
    record = find_record(output)
    assert record is not None, output
    assert {k: record[k] for k in BINDING_FIELDS if k != 'recorded_at'} == {
        k: v for k, v in request.items() if k != 'recorded_at'}
    assert (datetime.fromisoformat(record['recorded_at'].replace('Z', '+00:00'))
            == datetime.fromisoformat(request['recorded_at']))

    for source in ('board', 'host', 'operator', 'import'):
        world.bind(desk_id=first, session=f'source-{source}', source=source).ok()
    refused = [
        world.binding_request(desk_id=first, session='bad-source', source='model'),
        world.binding_request(desk_id='desk:00000000-0000-4000-8000-000000000000', session='no-desk'),
        {k: v for k, v in world.binding_request(desk_id=first, session='no-model').items() if k != 'model'},
        world.binding_request(desk_id=first, session='long-model', model='m' * 5000),
        world.binding_request(desk_id=first, session='bad-parent', parent=17),
    ]
    for body in refused:
        world.registry('bind', body).refused()
        session = body.get('native_session_id')
        _, replies = mcp(world.session_config(session), list_tools=False,
                         calls=[('memory.connection_status', {})])
        assert replies[0].value['status'] != 'ready', json.dumps(body)
