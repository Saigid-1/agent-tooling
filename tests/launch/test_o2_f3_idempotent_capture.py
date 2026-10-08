"""O2 F3: idempotent capture and recovery (docs/work/orders/O2-opencode-third-harness.md, R3, R4, F3).

Each test prepares a launch with the packaged `opencode` profile (through `kp-agent-launch prepare`), serves
an OpenCode session snapshot through a fake `opencode export` (tests/launch/o2_test_harness.py), and runs the launch's
own hook command for Stop with the payload the plugin knows at turn end (seams O2-S2..S4). Capture is
observed only through the desk-memory MCP tools of that session and the queue's status (O2-S6).

RED at base: there is no `opencode` profile (the harness has no profile, so prepare refuses it), no
parser and no per-launch binding.

Mutant named by the order: event ids derived from position instead of part ids
(test_f3_event_ids_follow_part_ids_not_positions).
"""
from __future__ import annotations

import pytest

import o2_seams as seams
from o2_test_harness import OpenCodeSession, OpenCodeWorld, count_text, describe


@pytest.fixture
def oc(tmp_path):
    return OpenCodeWorld(tmp_path)


def _bound(oc, prepared, session):
    bound = [(b['harness'], b['source']) for b in oc.bindings(session.id)]
    assert bound == [(seams.HARNESS, 'board')], f'the first Stop did not bind {session.id}: {bound}'


def test_f3_same_snapshot_twice_publishes_nothing_new(oc):
    """GREEN-IF the first Stop captures each visible text of the turn once (user and assistant text in their
    roles, the completed tool part as `tool`; reasoning and synthetic text never), and a second Stop over
    the SAME snapshot adds zero events and zero queue jobs."""
    desk = oc.save_desk(name='Idempotent desk')
    prepared = oc.prepare(desk)
    session = OpenCodeSession(oc.world.workspace)
    session.user('Please remember: Quillwort idempotent ask.')
    session.assistant('Noted, Quillwort idempotent answer.', reasoning='Hemlock private reasoning',
                      tool={'tool': 'bash', 'output': 'Saxifrage tool output'},
                      synthetic='Mullein synthetic reminder')

    oc.stop(prepared, session)
    _bound(oc, prepared, session)
    first = oc.events(session.id)
    jobs = oc.queue_jobs(prepared, session.id)
    assert count_text(first, 'Quillwort idempotent ask', 'user') == 1, describe(first)
    assert count_text(first, 'Quillwort idempotent answer', 'assistant') == 1, describe(first)
    assert count_text(first, 'Saxifrage tool output', 'tool') == 1, describe(first)
    assert count_text(first, 'Hemlock private reasoning') == 0, f'reasoning was captured:\n{describe(first)}'
    assert count_text(first, 'Mullein synthetic reminder') == 0, f'synthetic text was captured:\n{describe(first)}'
    assert len(jobs) >= 1, f'the capture enqueued nothing: {jobs}'

    oc.stop(prepared, session)
    again = oc.events(session.id)
    assert again == first, f'the same snapshot captured twice added events:\nbefore {describe(first)}\nafter {describe(again)}'
    assert oc.queue_jobs(prepared, session.id) == jobs, 'the same snapshot captured twice enqueued again'


def test_f3_a_dropped_stop_is_recovered_by_the_next_stop(oc):
    """GREEN-IF, when turn 1's Stop never ran, turn 2's Stop captures BOTH turns, each text exactly once,
    and a further Stop over the same snapshot adds nothing."""
    desk = oc.save_desk(name='Recovery desk')
    prepared = oc.prepare(desk)
    session = OpenCodeSession(oc.world.workspace)
    session.user('Turn one ask: Bladderwort.')
    session.assistant('Turn one answer: Bladderwort kept.')
    oc.fake.serve(session)  # turn 1 ended; its Stop was dropped (never run)
    session.user('Turn two ask: Sundew.')
    session.assistant('Turn two answer: Sundew kept.')

    oc.stop(prepared, session)
    events = oc.events(session.id)
    for text, role in (('Turn one ask: Bladderwort', 'user'), ('Turn one answer: Bladderwort kept', 'assistant'),
                       ('Turn two ask: Sundew', 'user'), ('Turn two answer: Sundew kept', 'assistant')):
        assert count_text(events, text, role) == 1, f'{text!r} not captured exactly once:\n{describe(events)}'

    oc.stop(prepared, session)
    assert oc.events(session.id) == events, 'a repeated Stop over the same snapshot added events'


def test_f3_an_unfinished_reply_is_captured_once_when_it_completes(oc):
    """R3 ("only COMPLETED assistant messages") under R4's recovery. GREEN-IF a Stop that sees the reply still
    streaming captures none of the partial reply, and the next Stop, after the same text part (same part id)
    has its final text, captures the ask and the final reply exactly once each."""
    desk = oc.save_desk(name='Streaming desk')
    prepared = oc.prepare(desk)
    session = OpenCodeSession(oc.world.workspace)
    session.user('Streaming ask: Pipewort.')
    reply = session.assistant('Pipewort partial', completed=False)

    oc.stop(prepared, session)
    early = oc.events(session.id)
    assert count_text(early, 'Pipewort partial') == 0, f'an unfinished reply was captured:\n{describe(early)}'

    session.complete(reply, 'Pipewort partial and now the complete answer.')
    oc.stop(prepared, session)
    events = oc.events(session.id)
    assert count_text(events, 'Streaming ask: Pipewort', 'user') == 1, describe(events)
    assert count_text(events, 'Pipewort partial and now the complete answer', 'assistant') == 1, describe(events)
    assert count_text(events, 'Pipewort partial') == 1, f'the partial text was captured on its own:\n{describe(events)}'


def test_f3_event_ids_follow_part_ids_not_positions(oc):
    """R3: event ids derive from OpenCode's part ids. GREEN-IF, after turn 2 is reverted and turn 3 takes its
    positions (new message and part ids), the next Stop captures turn 3 exactly once, turns 1 and 2 stay
    exactly once, and every captured event id contains its part's id (O2-S5).
    Mutant: ids derived from position (turn 3 collides with turn 2's ids, or ids carry no part id)."""
    desk = oc.save_desk(name='Revert desk')
    prepared = oc.prepare(desk)
    session = OpenCodeSession(oc.world.workspace)
    session.user('First ask: Butterwort.')
    session.assistant('First answer: Butterwort.')
    session.user('Second ask: Sweetgale.')
    session.assistant('Second answer: Sweetgale.')
    oc.stop(prepared, session)
    first_parts = OpenCodeSession.text_parts(session.snapshot())

    session.revert_after(2)
    session.user('Third ask: Bogbean.')
    session.assistant('Third answer: Bogbean.')
    oc.stop(prepared, session)
    events = oc.events(session.id)
    for text in ('First ask: Butterwort', 'First answer: Butterwort', 'Second ask: Sweetgale',
                 'Second answer: Sweetgale', 'Third ask: Bogbean', 'Third answer: Bogbean'):
        assert count_text(events, text) == 1, f'{text!r} not captured exactly once:\n{describe(events)}'

    parts = {part_id: text for part_id, _, text in first_parts + OpenCodeSession.text_parts(session.snapshot())}
    if seams.EVENT_ID_CONTAINS_PART_ID:
        for event in events:
            owners = [part_id for part_id, text in parts.items() if part_id in event['event_id']]
            assert len(owners) == 1, f'event id {event["event_id"]!r} names no part id of the session'
            assert event['text'] in parts[owners[0]], (event, parts[owners[0]])
