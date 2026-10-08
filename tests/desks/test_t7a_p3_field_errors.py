"""T7a P3: field errors name the field.

Order: docs/work/orders/T7a-role-readiness.md, P3.
Every malformed registry request (`save`, `save-role`, `bind`, `annotate`) is refused
as a ValueError-category error naming the offending field. Examples: a missing or
null `desk_id`, a non-canonical desk UUID, a missing `expected_version`, or a wrong type.
Falsifier: a malformed request reported as "Registry unavailable" or with a
non-ValueError category.

Surface: the `kp-agent-desk-registry --config C <action>` console script with the
request on stdin (docs/DESK-MENU.md "Registry actions"), over a portable registry
built through the public CLIs (tests/desks/t2_harness.py). Each malformed request is
a valid request (accepted in the positive control) with exactly one field broken.

Readings (repeated in the arm report under AMBIGUITY):
- "ValueError-category" is the printed `category` equal to `ValueError`;
- "naming the offending field" is the field's exact name (e.g. `desk_id`,
  `expected_version`) appearing as a word in the error body (its message or any other
  value except `category`);
- a message that names the field only by listing every field of the request does
  not identify it: for two requests of the same action that each lack a different
  field, the two error bodies must differ.
"""
from __future__ import annotations

import json
import re
import uuid

import pytest

from t2_harness import Registry, capture_card

ROSTER = [{'role_id': 'Scribe', 'label': 'Scribe', 'purpose': 'Record decisions as they are made.'}]


def _drop(field):
    return lambda request: {k: v for k, v in request.items() if k != field}


def _set(field, value):
    return lambda request: {**request, field: value}


def _upper_desk_id(request):
    return {**request, 'desk_id': 'desk:' + request['desk_id'].removeprefix('desk:').upper()}


CASES = {
    'save': [
        ('missing desk_id', _drop('desk_id'), 'desk_id'),
        ('null desk_id', _set('desk_id', None), 'desk_id'),
        ('integer desk_id', _set('desk_id', 42), 'desk_id'),
        ('malformed desk UUID', _set('desk_id', 'desk:not-a-uuid'), 'desk_id'),
        ('non-canonical desk UUID', _upper_desk_id, 'desk_id'),
        ('missing expected_version', _drop('expected_version'), 'expected_version'),
        ('string expected_version', _set('expected_version', '0'), 'expected_version'),
        ('integer name', _set('name', 123), 'name'),
        ('string capture', _set('capture', 'yes'), 'capture'),
    ],
    'save-role': [
        ('missing role_id', _drop('role_id'), 'role_id'),
        ('null role_id', _set('role_id', None), 'role_id'),
        ('missing expected_version', _drop('expected_version'), 'expected_version'),
        ('string expected_version', _set('expected_version', '0'), 'expected_version'),
        ('null label', _set('label', None), 'label'),
    ],
    'bind': [
        ('missing desk_id', _drop('desk_id'), 'desk_id'),
        ('null desk_id', _set('desk_id', None), 'desk_id'),
        ('malformed desk UUID', _set('desk_id', 'desk:not-a-uuid'), 'desk_id'),
        ('missing native_session_id', _drop('native_session_id'), 'native_session_id'),
        ('integer harness', _set('harness', 42), 'harness'),
        ('unknown source', _set('source', 'robot'), 'source'),
    ],
    'annotate': [
        ('missing desk_id', _drop('desk_id'), 'desk_id'),
        ('null desk_id', _set('desk_id', None), 'desk_id'),
        ('missing expected_version', _drop('expected_version'), 'expected_version'),
        ('string expected_version', _set('expected_version', '0'), 'expected_version'),
        ('string repos', _set('repos', 'product'), 'repos'),
    ],
}

# Two missing-field cases per action whose bodies must differ.
DISTINCT = {
    'save': ('missing desk_id', 'missing expected_version'),
    'save-role': ('missing role_id', 'missing expected_version'),
    'bind': ('missing desk_id', 'missing native_session_id'),
    'annotate': ('missing desk_id', 'missing expected_version'),
}


class Fixture:
    """A registry with one desk and one captured, bound source session (for `annotate`)."""

    def __init__(self, root):
        self.world = Registry.create(root, ROSTER)
        desk, run = self.world.save_desk(role='Scribe', name='Field desk')
        run.ok()
        self.desk_id = desk['desk_id']
        session = 'field-source-1'
        self.world.bind(desk_id=self.desk_id, session=session).ok()
        capture_card(self.world.session_config(session), session=session, text='Cedar field source')
        sessions = self.world.registry('list').ok().get('sessions') or []
        assert sessions and sessions[0].get('source_session_id'), f'no captured source session listed: {sessions}'
        self.source_session_id = sessions[0]['source_session_id']

    def valid(self, action) -> dict:
        if action == 'save':
            return self.world.desk_input(role='Scribe', name='Another desk')
        if action == 'save-role':
            return {'role_id': 'Curator', 'label': 'Curator', 'purpose': 'Keep the archive tidy.',
                    'expected_version': self.world.registry('roles').ok().get('version', 0)}
        if action == 'bind':
            return self.world.binding_request(desk_id=self.desk_id, session='bound-' + uuid.uuid4().hex[:12])
        contexts = self.world.registry('list').ok().get('contexts') or []
        version = max((c.get('version', 0) for c in contexts
                       if c.get('source_session_id') == self.source_session_id), default=0)
        return {'source_session_id': self.source_session_id, 'desk_id': self.desk_id, 'repos': ['product'],
                'adrs': [], 'cards': [], 'account_ref': None, 'provider': None, 'model': None,
                'expected_version': version, 'source_ref': 'operator:t7a-fixture'}

    def refuse(self, action, request) -> dict:
        run = self.world.registry(action, request)
        body = run.json
        assert run.code != 0, f'the malformed {action} request was accepted\n' + run.describe()
        assert isinstance(body, dict) and body.get('status') == 'error', (
            f'{action} did not print an error body\n' + run.describe())
        return body


@pytest.fixture(scope='module')
def fixture(tmp_path_factory):
    return Fixture(tmp_path_factory.mktemp('t7a-p3'))


def _named(body: dict, field: str) -> bool:
    text = json.dumps({k: v for k, v in body.items() if k != 'category'})
    return re.search(r'(?<![A-Za-z0-9_])' + re.escape(field) + r'(?![A-Za-z0-9_])', text) is not None


@pytest.mark.parametrize('action', sorted(CASES))
def test_valid_request_is_accepted(fixture, action):
    """Positive control: the request every malformed case starts from is accepted."""
    run = fixture.world.registry(action, fixture.valid(action))
    assert run.code == 0, f'the valid {action} request was refused, so its malformed variants prove nothing\n' \
        + run.describe()


@pytest.mark.parametrize('action,label,mutate,field', [
    (action, label, mutate, field) for action, cases in sorted(CASES.items()) for label, mutate, field in cases],
    ids=[f'{action}:{label}' for action, cases in sorted(CASES.items()) for label, _, _ in cases])
def test_malformed_request_is_a_value_error_naming_the_field(fixture, action, label, mutate, field):
    """GREEN-IF the refusal's category is ValueError, it is not 'Registry unavailable', and it names the field."""
    body = fixture.refuse(action, mutate(fixture.valid(action)))
    assert 'Registry unavailable' not in json.dumps(body), f'{action} {label}: reported as unavailable: {body}'
    assert body.get('category') == 'ValueError', f'{action} {label}: category {body.get("category")!r}: {body}'
    assert _named(body, field), f'{action} {label}: the error does not name {field!r}: {body}'


@pytest.mark.parametrize('action', sorted(DISTINCT))
def test_missing_field_errors_identify_which_field(fixture, action):
    """GREEN-IF two requests each missing a different field get different errors, each naming its own field."""
    labels = DISTINCT[action]
    cases = {label: (mutate, field) for label, mutate, field in CASES[action]}
    bodies = {}
    for label in labels:
        mutate, field = cases[label]
        bodies[label] = fixture.refuse(action, mutate(fixture.valid(action)))
        assert _named(bodies[label], field), f'{action} {label}: the error does not name {field!r}: {bodies[label]}'
    first, second = (json.dumps({k: v for k, v in bodies[label].items()}, sort_keys=True) for label in labels)
    assert first != second, (f'{action}: requests missing different fields get the same error, which therefore '
                             f'does not identify the offending field: {first}')
