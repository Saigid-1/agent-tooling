"""T2 P3: history is preserved; plus the P4 compatibility of the imported path.

The fixture is a pre-T2 memory store built only with the current public CLIs
(``kp-agent-desk initialize/admit``, ``kp-agent-host-card --apply``,
``kp-agent-memory call memory.propose``) over an ``ops.imported-desk-catalog.v1``
catalog. A registry descriptor for the same tenant then opens the same state and
``kp-agent-desk-registry import-catalog --catalog <path>`` imports it.

Readings (reported under AMBIGUITY): an existing registry desk whose preserved
key matches a catalog binding but whose label differs is "conflicting"; the
existing admission of the old session is expected to resolve through the
registry authority because its binding key is preserved; the importer never
modifies its source catalog.
"""
import hashlib
import json

from t2_harness import (Legacy, binding_key, desks_of, mcp, memory_call, private_json,
                        report_count, roles_of)


def _desks_by_name(world):
    return {d['name']: d for d in desks_of(world.listing())}


def test_import_preserves_keys_roles_labels_and_repos_and_reimport_creates_nothing(tmp_path):
    legacy = Legacy.create(tmp_path / 'legacy')
    world = legacy.registry()
    report = world.registry('import-catalog', None, '--catalog', legacy.catalog).ok()
    assert (report_count(report, 'created'), report_count(report, 'unchanged'),
            report_count(report, 'conflicting')) == (3, 0, 0), report

    assert sorted(b['binding_key'] for b in world.bindings()) == sorted(r['binding_key'] for r in legacy.rows)
    desks = _desks_by_name(world)
    assert set(desks) == {r['desk_label'] for r in legacy.rows}
    for row in legacy.rows:
        desk = desks[row['desk_label']]
        assert desk['role'] == row['role']
        assert row['repo_key'] in desk['repos']
        assert desk['desk_id'].startswith('desk:')
    roster = {r['role_id'] for r in world.roles()}
    assert roster >= {'Scribe', 'Verification', 'Deployment Engineering', 'Curator'}
    assert [r['role_id'] for r in roles_of(world.listing())].count('Curator') == 1

    again = world.registry('import-catalog', None, '--catalog', legacy.catalog).ok()
    assert (report_count(again, 'created'), report_count(again, 'unchanged'),
            report_count(again, 'conflicting')) == (0, 3, 0), again
    assert sorted(d['desk_id'] for d in desks_of(world.listing())) == sorted(d['desk_id'] for d in desks.values())
    assert sorted(b['binding_key'] for b in world.bindings()) == sorted(r['binding_key'] for r in legacy.rows)
    assert sorted(r['role_id'] for r in world.roles()) == sorted(roster)


def test_imported_history_is_found_by_desk_search_through_the_registry(tmp_path):
    legacy = Legacy.create(tmp_path / 'legacy')
    world = legacy.registry()
    world.registry('import-catalog', None, '--catalog', legacy.catalog).ok()
    verification = _desks_by_name(world)['Verification desk']

    successor = 'successor-session-1'
    world.bind(desk_id=verification['desk_id'], session=successor, source='import').ok()
    _, replies = mcp(world.session_config(successor), calls=[
        ('memory.bindings', {}),
        ('memory.search', {'query': 'Cedar lantern'}),
        ('memory.list', {'kind': 'episodes'}),
        ('memory.list', {'kind': 'capsules'}),
    ])
    bindings, found, episodes, capsules = replies
    assert not any(r.is_error for r in replies), [r.value for r in replies]
    own = [b['binding_key'] for b in bindings.value['bindings'] if b['own']]
    assert own == [legacy.rows[0]['binding_key']]
    assert found.value['binding_key'] == legacy.rows[0]['binding_key']
    assert legacy.episode_id in [h['episode_id'] for h in found.value['results']]
    assert legacy.episode_id in [e['episode_id'] for e in episodes.value['entries']]
    assert legacy.capsule_id in [c['capsule_id'] for c in capsules.value['entries']]

    # A desk that recorded nothing under the old catalog has no history to find.
    deploy = _desks_by_name(world)['Deploy desk']
    world.bind(desk_id=deploy['desk_id'], session='successor-session-2').ok()
    _, replies = mcp(world.session_config('successor-session-2'), list_tools=False,
                     calls=[('memory.search', {'query': 'Cedar lantern'})])
    assert replies[0].value['binding_key'] == legacy.rows[1]['binding_key']
    assert replies[0].value['results'] == []


def test_import_reports_a_conflicting_desk_without_changing_or_duplicating_it(tmp_path):
    legacy = Legacy.create(tmp_path / 'legacy')
    world = legacy.registry()
    world.registry('import-catalog', None, '--catalog', legacy.catalog).ok()
    before = _desks_by_name(world)

    changed = json.loads(legacy.catalog.read_text())
    changed['bindings'][0]['desk_label'] = 'Renamed verification desk'
    edited = private_json(tmp_path / 'edited-catalog.json', changed)
    report = world.registry('import-catalog', None, '--catalog', edited).ok()
    assert (report_count(report, 'created'), report_count(report, 'unchanged'),
            report_count(report, 'conflicting')) == (0, 2, 1), report
    if isinstance(report.get('conflicting'), list):
        assert legacy.rows[0]['binding_key'] in json.dumps(report['conflicting']) or \
            before['Verification desk']['desk_id'] in json.dumps(report['conflicting'])

    after = _desks_by_name(world)
    assert set(after) == set(before)
    assert {n: (d['desk_id'], d['role']) for n, d in after.items()} == {
        n: (d['desk_id'], d['role']) for n, d in before.items()}
    assert sorted(b['binding_key'] for b in world.bindings()) == sorted(r['binding_key'] for r in legacy.rows)


def test_import_leaves_the_source_catalog_and_existing_admission_working(tmp_path):
    legacy = Legacy.create(tmp_path / 'legacy')
    source = legacy.catalog.read_bytes()
    mode = legacy.catalog.stat().st_mode
    world = legacy.registry()
    world.registry('import-catalog', None, '--catalog', legacy.catalog).ok()
    assert hashlib.sha256(legacy.catalog.read_bytes()).hexdigest() == hashlib.sha256(source).hexdigest()
    assert legacy.catalog.stat().st_mode == mode

    # The unchanged ops.desk-memory.local.v1 config over the imported catalog keeps working.
    found = memory_call(legacy.config, 'memory.search', {'query': 'Cedar lantern'}).ok()
    assert legacy.episode_id in [h['episode_id'] for h in found['results']]
    listed = memory_call(legacy.config, 'memory.list', {'kind': 'capsules'}).ok()
    assert legacy.capsule_id in [c['capsule_id'] for c in listed['entries']]

    # The old session's admission, opened through the registry descriptor, keeps its desk.
    through_registry = world.session_config(legacy.session)
    _, replies = mcp(through_registry, list_tools=False, calls=[
        ('memory.connection_status', {}),
        ('memory.search', {'query': 'Cedar lantern'}),
    ])
    assert replies[0].value['status'] == 'ready'
    assert replies[1].value['binding_key'] == binding_key(legacy.tenant, 'Verification', 'ops')
    assert legacy.episode_id in [h['episode_id'] for h in replies[1].value['results']]
