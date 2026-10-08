"""T2 P1: desks are portable.

A desk whose role is in the configured roster, but not among the OPS nine, can
be created, bound and used for memory write and search, with no doctrine,
approval or reviewer anywhere in the flow. A role absent from the configured
roster is refused.

Readings (reported under AMBIGUITY): the registry CLI runs with an operator
memory config whose ``catalog_path`` names the registry descriptor and whose
``provider_session_id`` is not itself admitted; the memory state is set up with
the existing ``kp-agent-desk initialize`` followed by the existing
``kp-agent-desk-registry initialize``; a new role is added with
``expected_version`` 0 and a role saved once from a fresh roster is updated
with ``expected_version`` 1.
"""
import json
from importlib.resources import files

from t12b_seams import drain  # T12b B4: the one drain helper
from t2_harness import (GOVERNANCE_WORDS, OPS_NINE, ROSTER_SCHEMA, Registry, binding_key,
                        capture_card, desks_of, mcp, private_json, proposal_from_hit, roles_of)

ROSTER = [{'role_id': 'Scribe', 'label': 'Scribe', 'purpose': 'Record decisions as they are made.'},
          {'role_id': 'Curator', 'label': 'Curator', 'purpose': 'Keep the shared archive tidy.'}]


def _is_ops_name(value: str) -> bool:
    return value.strip().casefold() in {name.casefold() for name in OPS_NINE}


def _asset_rosters():
    """Every JSON asset shipped in the package, by path, with its parsed content."""
    found = {}
    stack = [files('kp_agent_tooling').joinpath('assets')]
    while stack:
        node = stack.pop()
        for child in node.iterdir():
            if child.is_dir():
                stack.append(child)
            elif child.name.endswith('.json'):
                try:
                    found[child.name] = json.loads(child.read_text())
                except ValueError:
                    pass
    return found


def test_default_roster_is_generic_and_ops_nine_ship_only_as_an_example(tmp_path):
    assets = _asset_rosters()
    rosters = {name: value for name, value in assets.items()
               if isinstance(value, dict) and value.get('schema_version') == ROSTER_SCHEMA}
    generic = {name: value for name, value in rosters.items()
               if isinstance(value.get('roles'), list) and 1 <= len(value['roles']) <= 200
               and not any(_is_ops_name(r.get('role_id', '')) or _is_ops_name(r.get('label', ''))
                           for r in value['roles'])}
    assert generic, f'no generic {ROSTER_SCHEMA} default roster among package assets {sorted(assets)}'
    example = [name for name, value in assets.items() if isinstance(value, dict)
               and {r.get('role_id') for r in (value.get('roles') or []) if isinstance(r, dict)} >= set(OPS_NINE)]
    assert example, 'the nine OPS roles must still ship as an importable example asset'
    assert not set(example) & set(generic)

    # The shipped default is a usable roster through the public `roles` action.
    name, default = sorted(generic.items())[0]
    world = Registry.create(tmp_path / 'default', default['roles'])
    listed = world.roles()
    assert [r['role_id'] for r in listed] == [r['role_id'] for r in default['roles']], name
    assert all(set(r) >= {'role_id', 'label', 'purpose'} for r in listed)
    assert not any(_is_ops_name(r['role_id']) for r in listed)


def test_roles_lists_the_configured_roster_and_save_role_adds_and_updates(tmp_path):
    world = Registry.create(tmp_path, ROSTER)
    assert [(r['role_id'], r['label'], r['purpose']) for r in world.roles()] == [
        (r['role_id'], r['label'], r['purpose']) for r in ROSTER]
    # The directory Kanban reads carries the configured roster, not the bundled OPS nine.
    listed = world.listing()
    assert [r['role_id'] for r in roles_of(listed)] == ['Scribe', 'Curator']

    added = {'role_id': 'Archivist', 'label': 'Archivist', 'purpose': 'Retire stale records.',
             'expected_version': 0}
    world.registry('save-role', added).ok()
    by_id = {r['role_id']: r for r in world.roles()}
    assert set(by_id) == {'Scribe', 'Curator', 'Archivist'}
    assert by_id['Archivist']['purpose'] == 'Retire stale records.'

    world.registry('save-role', dict(added, label='Records archivist', expected_version=1)).ok()
    assert {r['role_id']: r['label'] for r in world.roles()}['Archivist'] == 'Records archivist'

    # Stale versions, oversized identifiers and missing fields are refused and change nothing.
    world.registry('save-role', dict(added, label='Stale', expected_version=999)).refused()
    world.registry('save-role', dict(added, role_id='x' * 129, expected_version=0)).refused()
    world.registry('save-role', {k: v for k, v in added.items() if k != 'purpose'}).refused()
    assert {r['role_id']: r['label'] for r in world.roles()} == {
        'Scribe': 'Scribe', 'Curator': 'Curator', 'Archivist': 'Records archivist'}

    # A roster file outside 1..200 unique roles is not a roster.
    for broken in ([], [ROSTER[0], dict(ROSTER[0], label='Duplicate')],
                   [{'role_id': f'role-{i}', 'label': f'Role {i}', 'purpose': 'Bulk.'} for i in range(201)]):
        private_json(world.roster, {'schema_version': ROSTER_SCHEMA, 'roles': broken})
        world.registry('roles').refused()


def test_save_accepts_only_roles_in_the_configured_roster(tmp_path):
    world = Registry.create(tmp_path, ROSTER)
    request, saved = world.save_desk(role='Curator', name='Archive curator', repos=['alpha', 'beta'],
                                     capture=True, memory_write=True,
                                     context_doc='Keep the archive consistent.')
    saved.ok()
    for rejected in ('Coordinator', 'Verification', 'Ghost role'):
        world.save_desk(role=rejected, name=f'Desk for {rejected}')[1].refused()
    world.save_desk(role='Curator', name='Too many repos',
                    repos=[f'repo-{i}' for i in range(33)])[1].refused()
    world.save_desk(role='Curator', name='Oversized context', context_doc='x' * 16385)[1].refused()

    desks = desks_of(world.listing())
    assert [d['desk_id'] for d in desks] == [request['desk_id']]
    desk = desks[0]
    assert (desk['name'], desk['role']) == ('Archive curator', 'Curator')
    assert sorted(desk['repos']) == ['alpha', 'beta']
    assert desk['capture'] is True and desk['memory_write'] is True
    # One desk, one binding: the repository-independent key of a new registry desk.
    expected = binding_key(world.tenant, request['desk_id'], '*')
    assert [b['binding_key'] for b in world.bindings()] == [expected]


def test_portable_desk_is_bound_and_used_for_memory_write_and_search(tmp_path):
    # Portable: a non-OPS role and zero repositories, with nothing but the roster and descriptor.
    world = Registry.create(tmp_path, ROSTER)
    request, saved = world.save_desk(role='Scribe', name='Decision scribe', repos=[],
                                     capture=True, memory_write=True)
    saved.ok()
    session = 'portable-session-1'
    world.bind(desk_id=request['desk_id'], session=session).ok()
    config = world.session_config(session)
    receipt = capture_card(config, session=session, text='Cedar lantern portable evidence')
    drain(config)  # T12b B4: the indexer, not the capture, indexes

    expected = binding_key(world.tenant, request['desk_id'], '*')
    tools, replies = mcp(config, calls=[
        ('memory.connection_status', {}),
        ('memory.bindings', {}),
        ('memory.search', {'query': 'Cedar lantern'}),
        ('memory.propose', lambda r: proposal_from_hit(
            next(h for h in r[2].value['results'] if h['episode_id'] == receipt['episode_id']))),
        ('memory.list', {'kind': 'capsules'}),
    ])
    status, bindings, found, proposed, capsules = replies
    assert not status.is_error and status.value['status'] == 'ready'
    own = [b['binding_key'] for b in bindings.value['bindings'] if b['own']]
    assert own == [expected]
    assert not found.is_error and found.value['binding_key'] == expected
    assert receipt['episode_id'] in [h['episode_id'] for h in found.value['results']]
    assert not proposed.is_error, proposed.value
    assert proposed.value['capsule_id'] in [c['capsule_id'] for c in capsules.value['entries']]

    # Nothing in the flow carried or needed a governance field.
    for document in (world.roster, world.descriptor):
        assert not any(word in document.read_text().casefold() for word in GOVERNANCE_WORDS)
    for payload in (request, world.binding_request(desk_id=request['desk_id'], session=session)):
        assert not any(word in key for key in payload for word in GOVERNANCE_WORDS)


def test_memory_write_false_desk_is_searchable_but_refuses_proposals(tmp_path):
    world = Registry.create(tmp_path, ROSTER)
    request, saved = world.save_desk(role='Curator', name='Read-only curator', capture=True,
                                     memory_write=False)
    saved.ok()
    session = 'read-only-session'
    world.bind(desk_id=request['desk_id'], session=session).ok()
    config = world.session_config(session)
    receipt = capture_card(config, session=session, text='Cedar lantern read only evidence')
    drain(config)  # T12b B4: the indexer, not the capture, indexes
    _, replies = mcp(config, calls=[
        ('memory.search', {'query': 'Cedar lantern'}),
        ('memory.propose', lambda r: proposal_from_hit(
            next(h for h in r[0].value['results'] if h['episode_id'] == receipt['episode_id']))),
    ])
    found, proposed = replies
    assert not found.is_error and found.value['results']
    assert proposed.is_error and proposed.value['category'] == 'operation_not_permitted'
