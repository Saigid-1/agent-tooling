"""Order T8 contract: claim notes can cite ordinary episodes; refusals say why.

Order: docs/work/orders/T8-claim-note-budget.md (frozen at its merge commit). These
tests drive the public memory tool surface -- ``EpisodicMemoryTools.call`` and the
``kp-agent-memory --config ... serve`` stdio MCP server -- with synthetic episodes
captured through ``EpisodeStore.capture``. A refusal's public form is what the
server returns for it: ``tool_failure(name, error)``.

The order names some outputs without naming their fields: the per-event omission
record, the record of which events were scanned, and the ``review_required`` mark
with reason ``source_qualifiers_omitted``. Those are located structurally in what
the stored capsule reads back, never by a field name.
"""
import asyncio
import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from test_portable_desk_memory import episode_store
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools, tool_failure


QUALIFIER_BUDGET = 64          # negated sentences a capsule keeps (order: Problem, P2)
SENTENCE_BOUND = 2000          # bytes in one qualifier sentence (order: Problem, P2)
BASE_CAPSULE = Path(__file__).resolve().parent / 'fixtures/t8-claim-note-budget/base-capsule.json'
# Categories that do not name a bound. ``source_conflict`` is allowed only for a
# citation range, where the existing category already points at the citation.
NOT_A_BOUND = {'invalid_arguments', 'memory_unavailable', 'unknown_operation',
               'unknown_binding', 'operation_not_permitted', 'source_conflict'}
SHAPE = 'invalid_arguments'
REASON = 'source_qualifiers_omitted'


# ---------------------------------------------------------------- source helpers

def _event(event_id, role, sentences):
    """Join sentences with one space; return the event and each sentence's exact span."""
    text, spans = '', []
    for sentence in sentences:
        if text:
            text += ' '
        spans.append((len(text), len(text) + len(sentence), sentence))
        text += sentence
    return {'event_id': event_id, 'role': role, 'text': text}, spans


def _negated(word, count, first=0):
    return [f'{word} check {index:03d} was not verified.' for index in range(first, first + count)]


def _overlong(word, index):
    sentence = f'{word} archive {index} ' + 'segment ' * 300 + 'was not reviewed.'
    assert len(sentence.encode()) > SENTENCE_BOUND and not re.search(r'[.!?\n]', sentence[:-1])
    return sentence


class Desk:
    """One admitted desk: session-1 captures (operator path), session-2 proposes and reads."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.store = episode_store(tmp_path)
        self.tools = EpisodicMemoryTools(self.store, 'session-2')
        self.source_refs, self.sentences = [], []

    def capture(self, source_ref, events):
        self.source_refs.append(source_ref)
        for event, spans in events:
            self.sentences += [sentence for _, _, sentence in spans]
        return self.store.capture('session-1', source_ref=source_ref,
                                  events=[event for event, _ in events])['episode_id']

    def stored(self, capsule_id):
        """What the stored capsule reads back as: its handoff and its full evidence directory."""
        handoff = self.tools.call('memory.handoff', {'capsule_id': capsule_id})
        rows, offset = [], 0
        while offset is not None:
            page = self.tools.call('memory.evidence_directory',
                                   {'capsule_id': capsule_id, 'offset': offset, 'limit': 100})
            rows += page['entries']
            offset = page['next_offset']
        return handoff, rows

    def capsule_ids(self):
        return [entry['capsule_id'] for entry in
                self.tools.call('memory.list', {'kind': 'capsules', 'limit': 50})['entries']]

    def forbidden(self, arguments):
        """Strings no refusal may carry: uncited source text, source paths and refs,
        desk identifiers and binding keys the caller did not supply."""
        supplied = json.dumps(arguments)
        catalog = json.loads((self.tmp_path / 'catalog.json').read_text())
        desks = [catalog['project'][key] for key in ('tenant_id', 'project_id', 'repo_key')]
        for desk in catalog['desks']:
            desks += [desk['desk_id'], desk['label'], desk['binding_id'], desk['memory_binding_id']]
        for binding in self.tools.call('memory.bindings', {})['bindings']:
            desks += [binding['binding_key'], binding['binding_key'].split(':', 1)[1],
                      binding['repo_key'], binding['desk_label']]
        paths = [str(self.tmp_path), str(self.tmp_path.resolve()), str(self.store.path)]
        candidates = self.sentences + self.source_refs + desks + paths
        return sorted({value for value in candidates if value and value not in supplied})


def _note(episode_id, event_id, span, kind='observation'):
    """An item quoting one whole source sentence, so its own evidence check matches."""
    start, end, quote = span
    return {'kind': kind, 'text': quote, 'citations': [{'episode_id': episode_id,
            'event_id': event_id, 'start': start, 'end': end, 'quote': quote}]}


def _qualifier_keys(handoff):
    return sorted((q['citation']['episode_id'], q['citation']['event_id'], q['citation']['start'],
                   q['citation']['end'], q['citation']['quote']) for q in handoff['source_qualifiers'])


def _expected(episode_id, event_id, spans, indices):
    return [(episode_id, event_id, spans[i][0], spans[i][1], spans[i][2]) for i in indices]


# ------------------------------------------------- structural search of read-backs

def _strings(node):
    """Every string value and dict key in a JSON document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)
    elif isinstance(node, str):
        yield node


def _leaves(node, context=()):
    """Yield (leaf, context). Context holds the keys on the leaf's path and the string
    values of every dict on that path (a flat record's identifying siblings)."""
    if isinstance(node, dict):
        own = tuple(value for value in node.values() if isinstance(value, str))
        for key, value in node.items():
            yield from _leaves(value, context + own + (key,))
    elif isinstance(node, list):
        for value in node:
            yield from _leaves(value, context)
    else:
        yield node, context


def _nodes(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _nodes(value)


def _mentions(context, leaf, identifier):
    return any(isinstance(s, str) and identifier in s for s in context + (leaf,))


def _recorded_count(documents, episode_id, event_id, count):
    """An integer equal to ``count`` recorded against exactly this (episode, event)."""
    return any(type(leaf) is int and leaf == count
               and _mentions(context, None, episode_id) and _mentions(context, None, event_id)
               for document in documents for leaf, context in _leaves(document))


def _referenced_pairs(node, pairs):
    found = set()
    for leaf, context in _leaves(node):
        for episode_id, event_id in pairs:
            if _mentions(context, leaf, episode_id) and _mentions(context, leaf, event_id):
                found.add((episode_id, event_id))
    return found


def _without_items(handoff):
    return {key: value for key, value in handoff.items() if key != 'items'}


_BASE_DIRECTORY_KEYS = {'episode_id', 'event_id', 'role', 'characters', 'cited', 'cited_ranges',
                        'uncited_ranges', 'cited_characters', 'coordinate_system', 'meaning_preservation'}


def _records_exactly(handoff, rows, cited, every_pair):
    """Some stored record names exactly the cited (episode, event) pairs.

    Either a node of the handoff (outside its items, which trivially name their own
    citations) references exactly the cited pairs, or the evidence directory carries
    a new per-event field that is set on exactly the cited rows."""
    if any(_referenced_pairs(node, every_pair) == cited
           for node in _nodes(_without_items(handoff)) if isinstance(node, (dict, list))):
        return True
    extra = {key for row in rows for key in row} - _BASE_DIRECTORY_KEYS
    return any({(row['episode_id'], row['event_id']) for row in rows if row.get(key)} == cited
               for key in extra)


def _review_mark(document):
    """``review_required`` carried as a string value (alone or in a list), or as a
    key set to a truthy value."""
    for node in _nodes(document):
        if isinstance(node, dict) and node.get('review_required'):
            return True
        if node == 'review_required':
            return True
    return False


def _marked_omitted(handoff, rows):
    documents = [handoff, rows]
    return (any(REASON in set(_strings(document)) for document in documents)
            and any(_review_mark(document) for document in documents))


# ------------------------------------------------------------------- refusals

def _refusal(desk, arguments, name='memory.propose'):
    """Call the tool, require a refusal that stores nothing and leaks nothing."""
    before = desk.capsule_ids()
    with pytest.raises(Exception) as caught:
        desk.tools.call(name, arguments)
    failure = tool_failure(name, caught.value)
    assert failure['status'] == 'error'
    assert isinstance(failure['category'], str) and isinstance(failure['guidance'], str)
    published = json.dumps(failure, ensure_ascii=False)
    assert [value for value in desk.forbidden(arguments) if value in published] == []
    assert desk.capsule_ids() == before
    return failure


def _names_bound(failure, *words, limit=None, allowed=frozenset()):
    assert failure['category'] not in NOT_A_BOUND - set(allowed), failure
    guidance = failure['guidance'].lower()
    assert all(word in guidance for word in words), failure
    if limit is not None:
        assert re.search(limit, guidance), failure


def _plain_episode(desk, ref='srcref-plain'):
    event, spans = _event('plain', 'user', ['Kestrel pool B carries the cutover.',
                                            'Quillon notes stay archived.'])
    return desk.capture(ref, [(event, spans)]), spans


# ======================================================================= P1

def test_p1_note_citing_short_event_beside_heavy_negation_succeeds(tmp_path):
    """An ordinary 85-event episode: the cited event is short; >64 negated sentences
    and an over-long one sit in uncited events. Qualifiers come from the cited event."""
    desk = Desk(tmp_path)
    short, short_spans = _event('short', 'user', ['Use pool B for the cutover.',
                                                  'Pool A was not retired.'])
    noise = [_event(f'e{index:02d}', 'assistant', _negated('Marrow', 1, index)) for index in range(83)]
    long_event = _event('long-uncited', 'tool', [_overlong('Tamsin', 0)])
    episode = desk.capture('srcref-ordinary-85', [(short, short_spans)] + noise + [long_event])
    assert len(noise) + 2 == 85

    result = desk.tools.call('memory.propose', {'episode_ids': [episode], 'budget_bytes': 24000,
        'items': [_note(episode, 'short', short_spans[0], 'decision')], 'unresolved_questions': []})

    expected = _expected(episode, 'short', short_spans, [1])
    assert _qualifier_keys(result['handoff']) == expected
    assert [q['role'] for q in result['handoff']['source_qualifiers']] == ['user']
    handoff, rows = desk.stored(result['capsule_id'])
    assert _qualifier_keys(handoff) == expected
    assert REASON not in set(_strings([handoff, rows]))      # nothing in the cited event was omitted
    assert result['capsule_id'] in desk.capsule_ids()


def test_p1_qualifiers_are_every_negated_sentence_of_cited_events_and_only_those(tmp_path):
    """Below the budget: every negated sentence of a cited event (inside or outside
    the quoted span) is kept; none comes from an uncited event, from an uncited event
    that shares a cited event's id in another episode, or from a listed-but-uncited episode."""
    desk = Desk(tmp_path)
    a_log, a_spans = _event('log', 'assistant', ['Kestrel deploy plan approved.',
        'The Kestrel smoke test was not run.', 'Rollback lever stays armed.',
        'Kestrel canary never reached full traffic.'])
    a_side, a_side_spans = _event('side', 'user', ['Quillon audit was not scheduled.',
                                                   'Archive the Quillon notes.'])
    b_plan, b_spans = _event('plan', 'user', ['Marrow migration starts Tuesday.',
                                              'Marrow backups were not restored in rehearsal.'])
    b_log, b_log_spans = _event('log', 'tool', ['Marrow log rotation is not configured.',
                                                'Disk pressure is low.'])
    c_only, c_spans = _event('only', 'user', ['Tamsin ticket was never triaged.'])
    first = desk.capture('srcref-a', [(a_log, a_spans), (a_side, a_side_spans)])
    second = desk.capture('srcref-b', [(b_plan, b_spans), (b_log, b_log_spans)])
    third = desk.capture('srcref-c', [(c_only, c_spans)])

    result = desk.tools.call('memory.propose', {'episode_ids': [first, second, third],
        'items': [_note(first, 'log', a_spans[0], 'decision'), _note(second, 'plan', b_spans[0])],
        'unresolved_questions': [], 'budget_bytes': 24000})

    expected = sorted(_expected(first, 'log', a_spans, [1, 3]) + _expected(second, 'plan', b_spans, [1]))
    assert _qualifier_keys(result['handoff']) == expected
    roles = {(q['citation']['episode_id'], q['citation']['event_id']): q['role']
             for q in result['handoff']['source_qualifiers']}
    assert roles == {(first, 'log'): 'assistant', (second, 'plan'): 'user'}
    handoff, rows = desk.stored(result['capsule_id'])
    assert _qualifier_keys(handoff) == expected
    assert REASON not in set(_strings([handoff, rows]))


def test_p1_capsule_records_which_events_were_scanned(tmp_path):
    """The stored capsule names the scanned events: exactly the cited (episode, event)
    pairs, including a cited event that holds no negated sentence."""
    desk = Desk(tmp_path)
    alpha, alpha_spans = _event('alpha-plain', 'user', ['Kestrel pool B carries the cutover.'])
    charlie, charlie_spans = _event('charlie-uncited', 'assistant', ['Quillon audit was not scheduled.'])
    bravo, bravo_spans = _event('bravo-negations', 'tool', ['Marrow backup finished.',
                                                            'Marrow restore was not rehearsed.'])
    first = desk.capture('srcref-scan-1', [(alpha, alpha_spans), (charlie, charlie_spans)])
    second = desk.capture('srcref-scan-2', [(bravo, bravo_spans)])

    result = desk.tools.call('memory.propose', {'episode_ids': [first, second],
        'items': [_note(first, 'alpha-plain', alpha_spans[0], 'decision'),
                  _note(second, 'bravo-negations', bravo_spans[0])],
        'unresolved_questions': [], 'budget_bytes': 24000})

    cited = {(first, 'alpha-plain'), (second, 'bravo-negations')}
    every_pair = cited | {(first, 'charlie-uncited')}
    handoff, rows = desk.stored(result['capsule_id'])
    assert _records_exactly(handoff, rows, cited, every_pair)


# ======================================================================= P2

def test_p2_cited_event_over_qualifier_count_is_recorded_not_refused(tmp_path):
    desk = Desk(tmp_path)
    heavy, heavy_spans = _event('heavy', 'assistant', _negated('Kestrel', QUALIFIER_BUDGET + 9))
    plain, plain_spans = _event('plain', 'user', ['Quillon pool B carries the cutover.'])
    episode = desk.capture('srcref-heavy', [(heavy, heavy_spans), (plain, plain_spans)])
    note = _note(episode, 'heavy', heavy_spans[0])

    result = desk.tools.call('memory.propose', {'episode_ids': [episode], 'items': [note],
                                                'unresolved_questions': [], 'budget_bytes': 24000})

    assert result['capsule_id'] in desk.capsule_ids()
    handoff, rows = desk.stored(result['capsule_id'])
    assert handoff['source_qualifiers'] == result['handoff']['source_qualifiers']
    sentences = set(_expected(episode, 'heavy', heavy_spans, range(len(heavy_spans))))
    kept = _qualifier_keys(handoff)
    assert len(kept) == QUALIFIER_BUDGET and set(kept) <= sentences
    assert _recorded_count([handoff, rows], episode, 'heavy', 9)
    assert _marked_omitted(handoff, rows)


def test_p2_overlong_negated_sentence_in_cited_event_is_recorded_not_refused(tmp_path):
    desk = Desk(tmp_path)
    sentences = ['Ship the green build.', 'The gate was not run.'] + [
        _overlong('Kestrel', index) for index in range(3)]
    cited, spans = _event('long', 'tool', sentences)
    episode = desk.capture('srcref-long', [(cited, spans)])

    result = desk.tools.call('memory.propose', {'episode_ids': [episode],
        'items': [_note(episode, 'long', spans[0], 'decision')],
        'unresolved_questions': [], 'budget_bytes': 24000})

    assert result['capsule_id'] in desk.capsule_ids()
    handoff, rows = desk.stored(result['capsule_id'])
    assert _qualifier_keys(handoff) == _expected(episode, 'long', spans, [1])
    assert _recorded_count([handoff, rows], episode, 'long', 3)
    assert _marked_omitted(handoff, rows)


# ======================================================================= P3

def test_p3_more_than_32_items_names_the_items_bound(tmp_path):
    desk = Desk(tmp_path)
    episode, spans = _plain_episode(desk)
    failure = _refusal(desk, {'episode_ids': [episode], 'unresolved_questions': [],
                              'items': [_note(episode, 'plain', spans[0])] * 33})
    _names_bound(failure, 'item', limit=r'\b32\b')


def test_p3_more_than_32_episodes_names_the_episodes_bound(tmp_path):
    desk = Desk(tmp_path)
    episodes, first_spans = [], None
    for index in range(33):
        event, spans = _event('plain', 'user', [f'Kestrel slice {index:02d} is ready.'])
        episodes.append(desk.capture(f'srcref-slice-{index:02d}', [(event, spans)]))
        first_spans = first_spans or spans
    failure = _refusal(desk, {'episode_ids': episodes, 'unresolved_questions': [],
                              'items': [_note(episodes[0], 'plain', first_spans[0])]})
    _names_bound(failure, 'episode', limit=r'\b32\b')


@pytest.mark.parametrize('text', ['x' * 2001, '€' * 700],
                         ids=['over-2000-characters', 'over-2000-bytes'])
def test_p3_oversized_item_text_names_the_text_bound(tmp_path, text):
    desk = Desk(tmp_path)
    episode, spans = _plain_episode(desk)
    item = dict(_note(episode, 'plain', spans[0]), text=text)
    failure = _refusal(desk, {'episode_ids': [episode], 'items': [item], 'unresolved_questions': []})
    _names_bound(failure, 'text', limit=r'\b2,?000\b')


@pytest.mark.parametrize('start,end', [(0, 'past-end'), (6, 3), (-1, 5)],
                         ids=['end-beyond-event', 'reversed', 'negative-start'])
def test_p3_bad_citation_range_names_the_citation_range(tmp_path, start, end):
    desk = Desk(tmp_path)
    episode, spans = _plain_episode(desk)
    item = _note(episode, 'plain', spans[0])
    text = desk.store.read_event('session-2', episode_id=episode, event_id='plain', length=8000)['text']
    end = len(text) + 5 if end == 'past-end' else end
    quote = text[start:end] if 0 <= start < end else ''
    item['citations'][0].update(start=start, end=end, quote=quote if end <= len(text) else text)
    failure = _refusal(desk, {'episode_ids': [episode], 'items': [item], 'unresolved_questions': []})
    _names_bound(failure, 'citation', 'range', allowed={'source_conflict'})


def test_p3_handoff_byte_budget_names_the_budget(tmp_path):
    desk = Desk(tmp_path)
    episode, spans = _plain_episode(desk)
    item = dict(_note(episode, 'plain', spans[0]), text='Kestrel summary ' + 'y' * 1700)
    failure = _refusal(desk, {'episode_ids': [episode], 'items': [item],
                              'unresolved_questions': [], 'budget_bytes': 1000})
    _names_bound(failure, 'budget')


def test_p3_schema_shape_errors_keep_invalid_arguments_while_bounds_do_not(tmp_path):
    desk = Desk(tmp_path)
    episode, spans = _plain_episode(desk)
    good = {'episode_ids': [episode], 'items': [_note(episode, 'plain', spans[0])],
            'unresolved_questions': []}
    no_quote = _note(episode, 'plain', spans[0])
    del no_quote['citations'][0]['quote']
    shapes = [dict(good, session='session-1'),                     # unknown property
              dict(good, items='Kestrel pool B carries the cutover.'),  # wrong type
              {key: good[key] for key in ('episode_ids', 'items')},  # missing required
              dict(good, items=[dict(good['items'][0], kind='charter')]),  # outside enum
              dict(good, episode_ids=['episode:not-a-digest']),     # malformed identifier
              dict(good, items=[no_quote])]                         # incomplete citation
    for arguments in shapes:
        assert _refusal(desk, arguments)['category'] == SHAPE, arguments
    bound = _refusal(desk, dict(good, items=good['items'] * 33))
    assert bound['category'] != SHAPE


# ======================================================================= P4

def test_p4_base_capsule_reads_back_unchanged_beside_a_new_cited_scope_capsule(tmp_path):
    """A capsule the base code stored (frozen bytes, including a qualifier drawn from an
    uncited event) reads back and verifies exactly as the base read it, before and
    after the same note is proposed again under the cited-event scope."""
    frozen = json.loads(BASE_CAPSULE.read_text())
    desk = Desk(tmp_path)
    episode = desk.store.capture(frozen['capture_session'], source_ref=frozen['source_ref'],
                                 events=frozen['events'])['episode_id']
    assert episode == frozen['episode_id']
    row = frozen['capsule_row']
    with closing(sqlite3.connect(desk.store.path)) as db, db:
        db.execute('INSERT INTO capsules VALUES (?,?,?)',
                   (row['id'], row['binding'], row['payload'].encode('ascii')))
    old_id, reads = row['id'], frozen['reads']

    def base_reads_match():
        assert desk.tools.call('memory.handoff', {'capsule_id': old_id}) == reads['memory.handoff']
        assert desk.tools.call('memory.evidence_directory', {'capsule_id': old_id}) == \
            reads['memory.evidence_directory']
        assert desk.tools.call('memory.handoff_page', {'capsule_id': old_id, 'budget_bytes': 24000}) == \
            reads['memory.handoff_page']
        assert desk.tools.call('memory.resume', {'capsule_id': old_id, 'budget_bytes': 24000}) == \
            reads['memory.resume']

    base_reads_match()
    old_quotes = [q['citation']['quote'] for q in reads['memory.handoff']['source_qualifiers']]
    assert old_quotes == ['The canary gate was not run.', 'Staging was never load tested.']

    again = desk.tools.call('memory.propose', dict(frozen['propose_arguments'], budget_bytes=24000))
    assert [q['citation']['quote'] for q in again['handoff']['source_qualifiers']] == \
        ['The canary gate was not run.']
    assert again['capsule_id'] != old_id
    assert {old_id, again['capsule_id']} <= set(desk.capsule_ids())
    base_reads_match()


@pytest.mark.parametrize('unrepresentable', ['count', 'length'])
def test_p4_preflight_refuses_unrepresentable_input_before_any_proposal_with_specific_category(
        tmp_path, unrepresentable):
    from test_episodic_summarizer import configured
    desk = Desk(tmp_path)
    sentences = (_negated('Kestrel', QUALIFIER_BUDGET + 1) if unrepresentable == 'count'
                 else ['Ship the green build.', _overlong('Kestrel', 0)])
    event, spans = _event('source', 'assistant', sentences)
    episode = desk.capture('srcref-preflight', [(event, spans)])
    calls = []

    def proposer(packet):
        calls.append(packet)
        return {'items': [], 'unresolved_questions': []}

    for chunked in (False, True):
        with pytest.raises(Exception) as caught:
            if chunked:
                desk.store.consolidate_chunked_with('session-2', episode_ids=[episode],
                    propose=proposer, budget=configured(None).budget)
            else:
                desk.store.consolidate_with('session-2', episode_ids=[episode], propose=proposer)
        failure = tool_failure('memory.propose', caught.value)
        assert failure['category'] not in NOT_A_BOUND, (chunked, failure)
        guidance = failure['guidance'].lower()
        assert 'budget' in guidance or 'qualifier' in guidance, failure
        published = json.dumps(failure, ensure_ascii=False)
        assert [value for value in desk.forbidden({'episode_ids': [episode]}) if value in published] == []
    assert calls == []
    assert desk.capsule_ids() == []


# ================================================================ MCP server

def test_mcp_server_records_over_budget_note_and_names_bound_refusals(tmp_path):
    """The kp-agent-memory stdio server: an over-budget cited event is stored with its
    omission, a bounded refusal names the bound, a shape error stays invalid_arguments."""
    desk = Desk(tmp_path)
    heavy, heavy_spans = _event('heavy', 'assistant', _negated('Kestrel', QUALIFIER_BUDGET + 9))
    episode = desk.capture('srcref-mcp-heavy', [(heavy, heavy_spans)])
    note = _note(episode, 'heavy', heavy_spans[0])
    over_items = {'episode_ids': [episode], 'items': [note] * 33, 'unresolved_questions': []}
    forbidden = desk.forbidden(over_items)
    root = Path(__file__).resolve().parents[1]

    async def check():
        params = StdioServerParameters(command=sys.executable,
            args=['-m', 'kp_agent_tooling.memory_cli', '--config', str(tmp_path / 'session-2.json'), 'serve'],
            env={'PYTHONPATH': str(root / 'packages/tooling/src') + ':' + str(root)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
                await session.initialize()
                stored = await session.call_tool('memory.propose', {'episode_ids': [episode],
                    'items': [note], 'unresolved_questions': [], 'budget_bytes': 24000})
                assert not stored.isError, stored.structuredContent
                assert len(stored.structuredContent['handoff']['source_qualifiers']) == QUALIFIER_BUDGET
                capsule_id = stored.structuredContent['capsule_id']
                handoff = await session.call_tool('memory.handoff', {'capsule_id': capsule_id})
                directory = await session.call_tool('memory.evidence_directory',
                                                    {'capsule_id': capsule_id, 'limit': 100})
                assert not handoff.isError and not directory.isError
                assert directory.structuredContent['next_offset'] is None
                rows = directory.structuredContent['entries']
                assert _marked_omitted(handoff.structuredContent, rows)
                assert _recorded_count([handoff.structuredContent, rows], episode, 'heavy', 9)

                refused = await session.call_tool('memory.propose', over_items)
                assert refused.isError
                _names_bound(refused.structuredContent, 'item', limit=r'\b32\b')
                text = refused.content[0].text
                assert [value for value in forbidden if value in text] == []

                shape = await session.call_tool('memory.propose', dict(over_items, items=[note], session='session-1'))
                assert shape.isError and shape.structuredContent['category'] == SHAPE

    asyncio.run(check())
    assert len(desk.capsule_ids()) == 1
