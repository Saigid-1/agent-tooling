"""T4 P4: delegation needs evidence.

A child session binds under its parent only with transcript evidence. Without
it, the event is refused.
Falsifier: an unverified child accepted, or a verified child refused.

Evidence used (what a real Codex child rollout carries, cited from this tree):
- `extensions/ops/src/kp_agent_tooling_ops/_impl/service/delegation_attribution.py:96-105`
  and `docs/memory/DELEGATION-ATTRIBUTION.md:3-9`: a child rollout's first
  `session_meta` row has `payload.id` = the child, and
  `payload.source.subagent.thread_spawn` = {`parent_thread_id`, `agent_path`,
  `depth` >= 1, `agent_role`}; `payload.session_id` and `payload.parent_thread_id`,
  when present, equal the parent. `CODEX_SESSION_ID` may be inherited and is not
  proof.
- `packages/tooling/src/kp_agent_tooling/_impl/service/native_history_import.py:252-259`
  and `:310-321`: the importer reads the parent from `payload.parent_thread_id`, or
  from an inherited `payload.session_id` that differs from the rollout's own
  `id`, and rejects a row whose two parent fields disagree
  (`conflicting_parent_thread_identity`).
- `apps/kanban/src/commands/hook-events/codex-hook-events.ts:608-614` (fixture
  `apps/kanban/test/runtime/hooks-codex-parser.test.ts:126-140`): Kanban treats
  `payload.source.subagent.thread_spawn` as the mark of a descendant session.
- `apps/kanban/.plan/docs/hooks-update/codex-hooks-research.md:31-38`: Codex's
  `notify` payload carries `thread-id`, `turn-id`, `cwd` and messages, and no
  parentage, so a hook payload is not evidence.
- T3 (`launch_binding.codex_meta_matches`) already refuses a rollout whose
  `session_meta` source is a subagent when it would bind the launch's own session.

Readings (reported under AMBIGUITY):
- the child's events arrive through the parent launch's own hook command (Codex
  child threads run under the same process and configuration), after the
  parent is bound; the verified child is bound with `parent_session_id` = the
  launch's session, the same desk and harness; its `source` is not asserted;
- capture of the child's own turns is not asserted, only binding and admission;
- an unverified child is refused: no binding, no admission, and none of its text
  in the desk's memory; the parent's later turn in the same spool file proves the
  refused event was processed;
- a rollout that names another parent, whose two parent fields disagree, whose
  `session_meta` is another session's, or whose parentage appears only in the
  hook payload, does not prove parentage.
"""
from host_harness import HostWorld, codex_meta, new_session_id


def _child_meta(child, parent, cwd, *, meta_id=None, inherited=None):
    return codex_meta(meta_id or child, cwd,
                      source={'subagent': {'thread_spawn': {'parent_thread_id': parent, 'depth': 1,
                                                            'agent_path': '/root/worker', 'agent_role': None}}},
                      parent_thread_id=parent, session_id=inherited or parent)


def _launch(hw, desk, children):
    """A Codex launch whose parent binds first; `children(parent)` adds steps; the parent's last turn is the sentinel."""
    parent = new_session_id()
    first, sentinel = 'Spruce parent marker', 'Larch parent sentinel'
    plan = [hw.codex_turn('parent-prompt', parent, f'Remember {first}.', event='UserPromptSubmit',
                          meta=codex_meta(parent, hw.workspace)),
            hw.codex_turn('parent-stop', parent, f'Recorded {first}.'),
            *children(parent),
            hw.codex_turn('parent-sentinel', parent, f'Next {sentinel}.', start_role='user')]
    launched = hw.launch('codex', desk, provider='openai', plan=plan).ok()
    for item in plan:
        assert launched.runs(item['label']), f'no hook ran for {item["label"]}'
    return launched, parent, sentinel


def _assert_refused(hw, desk, parent, children):
    assert [(b['native_session_id'], b['desk_id'], b['source'], b['parent_session_id']) for b in hw.bindings()] == [
        (parent, desk, 'host', None)], hw.bindings()
    for child, marker in children:
        assert hw.binding_for(child) == [], f'unverified child {child} was bound'
        assert not hw.ready(child), f'unverified child {child} was admitted'
        assert hw.hits(parent, marker) == 0, f'unverified child text {marker!r} reached the desk'


def test_codex_child_with_rollout_parentage_binds_under_its_parent(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    child, marker = new_session_id(), 'Aspen verified child marker'
    launched, parent, sentinel = _launch(hw, desk, lambda parent: [
        hw.codex_turn('child-stop', child, f'Remember {marker}.', f'Recorded {marker}.',
                      meta=_child_meta(child, parent, hw.workspace))])

    hw.ingest(lambda: hw.hits_now(parent, sentinel) >= 1 and hw.bound_now(child))
    recorded = hw.binding_for(child)
    assert [(b['desk_id'], b['harness'], b['parent_session_id']) for b in recorded] == [(desk, 'codex', parent)], (
        recorded)
    assert hw.ready(child) and hw.own_keys(child) == [hw.desk_key(desk)]
    assert [(b['desk_id'], b['source'], b['parent_session_id']) for b in hw.binding_for(parent)] == [
        (desk, 'host', None)]


def test_codex_child_without_rollout_parentage_is_refused(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    child, marker = new_session_id(), 'Cypress unproven child marker'
    _, parent, sentinel = _launch(hw, desk, lambda parent: [
        hw.codex_turn('child-stop', child, f'Remember {marker}.', f'Recorded {marker}.',
                      meta=codex_meta(child, hw.workspace))])

    hw.ingest(lambda: hw.hits_now(parent, sentinel) >= 1)
    _assert_refused(hw, desk, parent, [(child, marker)])


def test_codex_child_whose_rollout_names_another_or_conflicting_parent_is_refused(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    stranger = new_session_id()
    other, conflicting, spawn_only = new_session_id(), new_session_id(), new_session_id()
    other_marker, conflict_marker = 'Juniper other-parent marker', 'Sorrel conflicting-parent marker'
    spawn_only_marker = 'Hazel spawn-only other-parent marker'
    # Meet addition (Coordinator mutation-RED): a rollout whose only parent claim is the
    # thread_spawn record, naming another session. No top-level parent field backs it up,
    # so the spawn record's own parent must be checked against the launch session.
    spawn_to_stranger = {'subagent': {'thread_spawn': {'parent_thread_id': stranger, 'depth': 1,
                                                       'agent_path': '/root/worker', 'agent_role': None}}}
    _, parent, sentinel = _launch(hw, desk, lambda parent: [
        hw.codex_turn('other-parent', other, f'Remember {other_marker}.', 'Recorded.',
                      meta=_child_meta(other, stranger, hw.workspace)),
        hw.codex_turn('conflicting-parent', conflicting, f'Remember {conflict_marker}.', 'Recorded.',
                      meta=_child_meta(conflicting, parent, hw.workspace, inherited=stranger)),
        hw.codex_turn('spawn-only-other-parent', spawn_only, f'Remember {spawn_only_marker}.', 'Recorded.',
                      meta=codex_meta(spawn_only, hw.workspace, source=spawn_to_stranger))])

    hw.ingest(lambda: hw.hits_now(parent, sentinel) >= 1)
    _assert_refused(hw, desk, parent, [(other, other_marker), (conflicting, conflict_marker),
                                       (spawn_only, spawn_only_marker)])


def test_codex_parentage_claimed_outside_the_childs_own_rollout_is_refused(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    claimed, borrowed, owner = new_session_id(), new_session_id(), new_session_id()
    claimed_marker, borrowed_marker = 'Teak payload-claim marker', 'Walnut borrowed-meta marker'

    def children(parent):
        spawn = {'subagent': {'thread_spawn': {'parent_thread_id': parent, 'depth': 1, 'agent_path': '/root/worker'}}}
        return [
            # Parentage only in the hook payload; the rollout is a root session.
            hw.codex_turn('payload-claim', claimed, f'Remember {claimed_marker}.', 'Recorded.',
                          meta=codex_meta(claimed, hw.workspace), parent_session_id=parent,
                          parent_thread_id=parent, source=spawn, thread_spawn=spawn['subagent']['thread_spawn']),
            # The rollout is named for this session, but its session_meta is another session's child row.
            hw.codex_turn('borrowed-meta', borrowed, f'Remember {borrowed_marker}.', 'Recorded.',
                          meta=_child_meta(borrowed, parent, hw.workspace, meta_id=owner)),
        ]

    _, parent, sentinel = _launch(hw, desk, children)
    hw.ingest(lambda: hw.hits_now(parent, sentinel) >= 1)
    _assert_refused(hw, desk, parent, [(claimed, claimed_marker), (borrowed, borrowed_marker)])
    assert hw.binding_for(owner) == []
