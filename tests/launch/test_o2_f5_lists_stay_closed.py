"""O2 F5: an `opencode` profile within closed lists (docs/work/orders/O2-opencode-third-harness.md, R1, F5).

- The packaged `opencode` profile validates, uses the existing `hook` session strategy and the existing
  event vocabulary, and names one parser that `PARSERS` gained (O2-S1).
- Unknown strategies (in every slot), an unknown parser and unknown fields are refused, starting from the
  `opencode` profile itself.
- The claude and codex entries, `CAPTURE_EVENTS` and the event pattern are unchanged (guards: GREEN at base).
- A variant of the `opencode` profile under another id is configuration only (T3 P1's property, for the new
  strategies): prepared and bound under its own id with no code change.
- T3's swap-by-configuration tests run unedited: their files are byte-identical to the order's base (guard:
  GREEN at base); the suite runs them.

RED at base for the order's reason: there is no `opencode` profile (and so no parser).
"""
from __future__ import annotations

import copy
import hashlib
import uuid
from pathlib import Path

import pytest

import o2_seams as seams
from o2_test_harness import FakeOpenCode, OpenCodeSession, OpenCodeWorld, count_text, opencode_profile, parser_of, profiles

REPO_ROOT = Path(__file__).resolve().parents[2]
# T3's swap-by-configuration tests and their harness at the order's base (sha256).
T3_UNEDITED = {
    'tests/launch/test_t3_p1_swap_by_configuration.py': '933379ef4d70fa33e5f4cdbe44688df696ecfae6d0668b6a09ec86ee43a5d8c1',
    'tests/launch/t3_harness.py': '51530c66cec5dfadc230f1cb5fc6883fb1ae82e7040a917caba4981d039634db',
}
SLOTS = ('session_id', 'mcp', 'hooks', 'capture')


def _refused(profile) -> str | None:
    try:
        profiles().validate_profile(profile)
    except ValueError as error:
        return str(error) or type(error).__name__
    return None


def test_f5_the_opencode_profile_validates_with_the_existing_vocabulary():
    """GREEN-IF the packaged `opencode` profile validates to itself, is enabled and selectable, binds by the
    existing `hook` strategy, configures exactly Stop, PreCompact and SessionEnd, and names a parser in
    PARSERS that is not one of the base parsers."""
    module = profiles()
    profile = opencode_profile()
    assert module.validate_profile(copy.deepcopy(profile)) == profile
    assert module.select(module.load(), seams.HARNESS) == profile
    assert profile['enabled'] is True and profile['harness'] == seams.HARNESS
    assert profile['session_id']['strategy'] == seams.SESSION_STRATEGY, profile['session_id']
    assert sorted(profile['hooks']['events']) == sorted(seams.EVENTS), profile['hooks']
    parser = parser_of(profile)
    assert parser in module.PARSERS and parser not in seams.BASE_PARSERS, (parser, module.PARSERS)


def test_f5_parsers_gain_exactly_one():
    """GREEN-IF PARSERS is the base parsers plus the `opencode` profile's parser, and nothing else."""
    parser = parser_of(opencode_profile())
    assert sorted(profiles().PARSERS) == sorted((*seams.BASE_PARSERS, parser)), profiles().PARSERS


@pytest.mark.parametrize('slot', SLOTS)
def test_f5_an_unknown_strategy_is_refused(slot):
    """GREEN-IF the `opencode` profile with its `slot` strategy (or capture mode) replaced by an unknown member is
    refused, while the profile itself validates."""
    profile = opencode_profile()
    assert _refused(profile) is None
    key = 'mode' if slot == 'capture' else 'strategy'
    changed = copy.deepcopy(profile)
    changed[slot][key] = 'o2-unknown-' + uuid.uuid4().hex[:8]
    assert _refused(changed), f'an unknown {slot} {key} was accepted: {changed[slot]}'


def test_f5_an_unknown_parser_and_unknown_fields_are_refused():
    """GREEN-IF the `opencode` profile is refused with an unknown parser, with an unknown top-level field and
    with an unknown field inside each strategy slot."""
    profile = opencode_profile()
    changed = copy.deepcopy(profile)
    changed['capture']['parser'] = 'o2-unknown-parser'
    assert _refused(changed), 'an unknown parser was accepted'
    assert _refused({**copy.deepcopy(profile), 'o2_unknown_field': True}), 'an unknown profile field was accepted'
    for slot in SLOTS:
        changed = copy.deepcopy(profile)
        changed[slot]['o2_unknown_field'] = 'x'
        assert _refused(changed), f'an unknown field in {slot} was accepted: {changed[slot]}'


def test_f5_dotted_opencode_event_names_stay_refused():
    """GREEN-IF the event pattern and CAPTURE_EVENTS are unchanged and a dotted OpenCode event name in the
    `opencode` profile is refused (the plugin maps OpenCode's events to the existing vocabulary)."""
    from kp_agent_tooling._impl.service import launch_binding
    assert profiles()._EVENT.pattern == seams.EVENT_PATTERN
    assert tuple(launch_binding.CAPTURE_EVENTS) == seams.CAPTURE_EVENTS
    changed = copy.deepcopy(opencode_profile())
    changed['hooks']['events'] = ['session.idle']
    assert _refused(changed), 'a dotted event name was accepted'


def test_f5_claude_and_codex_profiles_are_unchanged():
    """Guard (GREEN at base): the packaged claude and codex entries equal the order's base."""
    packaged = profiles().default_profiles()
    for harness, expected in seams.BASE_PROFILES.items():
        assert packaged[harness] == expected, (harness, packaged[harness])


def test_f5_t3_swap_by_configuration_tests_are_unedited():
    """Guard (GREEN at base): T3's swap-by-configuration tests and their harness are byte-identical to the
    order's base, so their run in the suite is the unedited run F5 names."""
    for name, digest in T3_UNEDITED.items():
        assert hashlib.sha256((REPO_ROOT / name).read_bytes()).hexdigest() == digest, f'{name} was edited'


def test_f5_an_opencode_variant_is_configuration_only(tmp_path):
    """T3 P1 for the new strategies. GREEN-IF an operator profile file adding `opencode-alt` (the packaged
    `opencode` profile under another id, its executable an absolute path) is prepared, bound by its first
    Stop under `opencode-alt`, and captures that turn, with no code change."""
    fake = FakeOpenCode(tmp_path / 'opencode-fake', (tmp_path / 'world').resolve() / 'home')
    variant = {**copy.deepcopy(opencode_profile()), 'harness': 'opencode-alt',
               'executable': str(fake.bin / 'opencode')}
    oc = OpenCodeWorld(tmp_path, operator_profiles=[variant], fake=fake)
    desk = oc.save_desk(name='Variant desk')
    prepared = oc.prepare(desk, harness='opencode-alt')
    session = OpenCodeSession(oc.world.workspace)
    session.user('Variant ask: Lousewort.')
    session.assistant('Variant answer: Lousewort.')
    oc.stop(prepared, session)
    assert [(b['harness'], b['desk_id']) for b in oc.bindings(session.id)] == [('opencode-alt', desk)]
    assert count_text(oc.events(session.id), 'Variant answer: Lousewort', 'assistant') == 1
