"""T11b regression set: every T11a golden entry that differs in this tree, listed for the meet.

Order: docs/work/orders/T11b-leaf-behaviour.md, "Carried from T11a": "The base for base-vs-head
comparisons is T11a's merged head on main. T11a's goldens (P1, P3, P4, P7 and U3) are T11b's
regression set. T11b changes only the leaf arguments its properties rule; every other golden must
stay byte-identical."

The goldens are NOT regenerated: they were generated at T11a's merged head, which is T11b's base,
so they are the base reading. This check recomputes each one in this tree with T11a's own committed
generator (tests/fixtures/t11a/generate_*.py) and lists EVERY entry that differs:

  golden                          generator part   entry (one case)
  identity_corpus[_ops].json      P1 core / ops    <section>/<scheme>/<input>  (inputs/<name>)
  private_file_sequences[_ops]    P3 core / ops    helpers/<helper>/<case>
  connection_profiles[_ops].json  P4 core / ops    steps/<step>/#<connection>
  rollout_capture.json            P7               captures/<class>/<case>
  store_path_decisions.json       U3               <consumer>/<case>

Each differing entry is printed with the sub-paths that changed, base and head values, and a LABEL
that only describes the change, so that the meet can match it to a ruling:
- P4: ``Q2 resolved URI`` when only ``database``/``uri`` changed and the head value is a ``file:``
  URI; otherwise ``NOT A Q2 FIELD: <fields>``;
- P3: ``Q3 fsync added``, ``Q3 chmod removed``, ``Q3 O_NOFOLLOW added``, ``Q3 creation mode``, and
  ``outcome changed`` / ``tree changed`` when those differ;
- U3: ``config_template`` cases are labelled ``Q4 template``, control-character cases ``Q1 control
  character``; any other ``store-path decision``;
- P1, P7: ``NOT RULED: P1/P7 must stay byte-identical``.
A label never excuses an entry. ``RULINGS`` maps an entry to the ruling the meet matched it to;
it is empty here (no ruling exists before the meet). The test fails on every differing entry
without a ruling, and on every ruling whose entry does not differ (a ruling cannot hide anything).

``python tests/test_t11b_regression_set.py`` prints the whole listing (every golden, every entry)
without asserting.

T12b's B2 kind (docs/work/orders/T12b-one-indexer-outbox.md, B4, "T12b's own regression set": "a B2 kind in
`label()`/`ruling_mismatch`: connection removed from a seal flow, a step's connection count changed, and an index
open replaced by `drain`/the reindex"). A P4 entry is labelled with a B2 kind only when its WHOLE STEP reads as one:
the base step holds at least one index open (a connection to the index file), and the head step is the base step
with every index run (a maximal run of consecutive index and store connections that contains an index open)
replaced by a run, possibly empty, of index-family (the index, or a build file beside it) and store connections,
each with the open profile (every field but the database text and the first statement) of a base connection of the
same kind; one more such run may follow the step's last connection (a drain after the flow). Every connection
outside the index runs stays byte-identical, first statement included. The entry's label is then `B2 index open
replaced by drain/reindex` (a base index open that the head replaces), `B2 connection removed from a seal flow` (a
base index-run connection that the head drops) or `B2 connection count changed` (a connection shifted, added or
removed by those). A step whose head records a step error, or changes anything else, keeps its T11b label. A ruling
of this kind starts `T12b B2:`; `ruling_mismatch` accepts it only for an entry whose label is a B2 kind. RULINGS stays
empty at T12b's base: the meet fills it. P1 and P7 accept no ruling (P7 stays byte-identical).

T12b's B1 kind (T12b Amendment 1, meet): a P4 entry is labelled `B1 ensure check` when its connection is a writable
store open (the store file, `mode=rw`) whose head first statement is exactly `PRAGMA user_version` (the outbox check,
B1) and whose every other field is identical. Nothing else is excused: DDL or any write as a first statement, the
check on another file or on a read-only open, or the check beside any other changed field keeps its T11b label. A
ruling of this kind starts `T12b B1:`. The B2 kind compares the connections outside the index runs modulo the B1
kind (a step can carry both). One named connection may be retired with the index run it immediately precedes
(`B2_RUN_WRAPPERS`, the meet): the import job's journal transaction that wrapped its inline index repair
(Amendment 1 R3, `test_session_import_job` index repair). The head may keep it or drop it; dropped, its entry reads
`B2 connection removed from a seal flow`. No other connection outside the index and store files may be dropped.

GREEN-IF: no golden entry differs, or every differing entry has a ruling in RULINGS and every
ruling names a differing entry. At base: 0 differing entries.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from t11a_golden import FIXTURES, load_generator  # noqa: E402

# entry key -> the ruling the meet matched it to (order section and its words). Empty before the meet.
# The T12b meet ruled the 54 P4 entries below (T12b Amendment 1): 23 of the B1 kind, 31 of the B2 kind (two
# added at the meet for the chunked post-swap reset's store connections (the bounded-reset fix); three for the host
# drain's bounded mismatch reset in the codex hook step (the universal bound); one for the reindex's clear of
# coverage_marks_lost after its full re-mark).
# T11b's meet ruled 83 entries here (T11b Amendment 1); the note below says why the dict is now empty.
RULINGS: dict[str, str] = {
    'connection_profiles.json:steps/claude capture: capture/#8':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/desk memory: initialize/#10':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/desk memory: initialize/#11':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/desk memory: initialize/#12':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/desk memory: initialize/#13':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/desk memory: initialize/#2':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/desk memory: initialize/#3':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/desk memory: initialize/#6':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/desk memory: initialize/#7':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/desk memory: initialize/#8':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/desk memory: initialize/#9':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/episode store: capture/#2':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/import jobs: advance/#1':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/import jobs: advance/#10':
        "T12b B2: B4 and Amendment 1 R3, the import job's inline index repair and its journal transaction retired; the host drains after the batch",
    'connection_profiles.json:steps/import jobs: advance/#11':
        "T12b B2: B4 and Amendment 1 R3, the import job's inline index repair and its journal transaction retired; the host drains after the batch",
    'connection_profiles.json:steps/import jobs: advance/#12':
        "T12b B2: B4 and Amendment 1 R3, the import job's inline index repair and its journal transaction retired; the host drains after the batch",
    'connection_profiles.json:steps/import jobs: advance/#13':
        "T12b B2: B4 and Amendment 1 R3, the import job's inline index repair and its journal transaction retired; the host drains after the batch",
    'connection_profiles.json:steps/import jobs: advance/#14':
        "T12b B2: B4 and Amendment 1 R3, the import job's inline index repair and its journal transaction retired; the host drains after the batch",
    'connection_profiles.json:steps/import jobs: advance/#2':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/import jobs: advance/#3':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/import jobs: advance/#4':
        "T12b B2: B4 and Amendment 1 R3, the import job's inline index repair and its journal transaction retired; the host drains after the batch",
    'connection_profiles.json:steps/import jobs: advance/#5':
        "T12b B2: B4 and Amendment 1 R3, the import job's inline index repair and its journal transaction retired; the host drains after the batch",
    'connection_profiles.json:steps/import jobs: advance/#6':
        "T12b B2: B4 and Amendment 1 R3, the import job's inline index repair and its journal transaction retired; the host drains after the batch",
    'connection_profiles.json:steps/import jobs: advance/#8':
        "T12b B2: B4 and Amendment 1 R3, the import job's inline index repair and its journal transaction retired; the host drains after the batch",
    'connection_profiles.json:steps/import jobs: advance/#9':
        "T12b B2: B4 and Amendment 1 R3, the import job's inline index repair and its journal transaction retired; the host drains after the batch",
    'connection_profiles.json:steps/import jobs: apply/#1':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/import jobs: construct/#0':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/import jobs: status/#1':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/launch: first codex hook/#26':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/launch: first codex hook/#39':
        "T12b B2: B4, the hook's inline index upsert retired; the host drains after the flow",
    'connection_profiles.json:steps/launch: first codex hook/#40':
        "T12b B2: B4, the hook's inline index upsert retired; the host drains after the flow",
    'connection_profiles.json:steps/launch: first codex hook/#41':
        "T12b B2: B4, the hook's inline index upsert retired; the host drains after the flow",
    'connection_profiles.json:steps/launch: first codex hook/#42':
        "T12b B2: B4, the hook's inline index upsert retired; the host drains after the flow",
    'connection_profiles.json:steps/launch: first codex hook/#43':
        "T12b B2: B4, the hook's inline index upsert retired; the host drains after the flow",
    'connection_profiles.json:steps/launch: first codex hook/#44':
        "T12b B2: B4, the hook's inline index upsert retired; the host drains after the flow",
    'connection_profiles.json:steps/launch: first codex hook/#45':
        "T12b B2: B4, the hook's inline index upsert retired; the host drains after the flow",
    'connection_profiles.json:steps/launch: first codex hook/#46':
        "T12b B2: B4, the hook's inline index upsert retired; the host drains after the flow",
    'connection_profiles.json:steps/launch: first codex hook/#47':
        "T12b B2: B4, the hook's inline index upsert retired; the host drains after the flow",
    'connection_profiles.json:steps/launch: first codex hook/#48':
        "T12b B2: B4, the hook's inline index upsert retired; the host drains after the flow",
    'connection_profiles.json:steps/launch: first codex hook/#49':
        "T12b B2: B4, the hook's inline index upsert retired; the host drains after the flow",
    'connection_profiles.json:steps/rollout capture: capture/#6':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/search index: rebuild/#0':
        'T12b B2: B3, rebuild() replaced by build-and-swap into the build file',
    'connection_profiles.json:steps/search index: rebuild/#1':
        'T12b B2: B3, rebuild() replaced by build-and-swap into the build file',
    'connection_profiles.json:steps/search index: rebuild/#2':
        'T12b B2: B3, rebuild() replaced by build-and-swap into the build file',
    'connection_profiles.json:steps/search index: rebuild/#3':
        'T12b B2: B3, rebuild() replaced by build-and-swap into the build file',
    'connection_profiles.json:steps/search index: rebuild/#4':
        'T12b B2: B3, rebuild() replaced by build-and-swap into the build file',
    'connection_profiles.json:steps/search index: rebuild/#5':
        'T12b B2: B3, rebuild() replaced by build-and-swap into the build file',
    'connection_profiles.json:steps/search index: rebuild/#6':
        'T12b B2: B3, rebuild() replaced by build-and-swap into the build file',
    'connection_profiles.json:steps/search index: rebuild/#7':
        'T12b B2: B3, rebuild() replaced by build-and-swap into the build file',
    'connection_profiles.json:steps/search index: rebuild/#8':
        'T12b B2: B3, rebuild() replaced by build-and-swap into the build file',
    'connection_profiles.json:steps/search index: rebuild/#9':
        'T12b B2: B3, rebuild() replaced by build-and-swap into the build file',
    'connection_profiles.json:steps/session sources: upgrade/#0':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/session sources: upgrade/#1':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
    'connection_profiles.json:steps/workspace capture: initialize/#0':
        "T12b B1: Amendment 1, the outbox check (PRAGMA user_version) is a writable store open's first statement",
}
# Emptied at T12b's base (Coordinator golden base-reset in the T12b freeze, Verification's option a): the T11a goldens
# were regenerated at that base by their committed generators, and the old-to-new difference was exactly T11b's 83
# ruled entries. T11b's rulings stay in git history (this file before T12b's golden base-reset) and in T11b's Amendment 1. A later
# slice rules its own differences here against this base.

# (label, generator module, part, golden file, entry depth, regression family)
GOLDENS = (
    ('P1 identity corpus', 'generate_identity_corpus', 'core', 'identity_corpus.json', 3, 'P1'),
    ('P1 identity corpus (ops)', 'generate_identity_corpus', 'ops', 'identity_corpus_ops.json', 3, 'P1'),
    ('P3 private-file sequences', 'generate_private_file_sequences', 'core', 'private_file_sequences.json', 3, 'P3'),
    ('P3 private-file sequences (ops)', 'generate_private_file_sequences', 'ops',
     'private_file_sequences_ops.json', 3, 'P3'),
    ('P4 connection profiles', 'generate_connection_profiles', 'core', 'connection_profiles.json', 3, 'P4'),
    ('P4 connection profiles (ops)', 'generate_connection_profiles', 'ops', 'connection_profiles_ops.json', 3, 'P4'),
    ('P7 rollout capture', 'generate_rollout_capture', None, 'rollout_capture.json', 3, 'P7'),
    ('U3 store-path decisions', 'generate_store_path_decisions', None, 'store_path_decisions.json', 2, 'U3'),
)
OPS_PARTS = {'ops'}
ABSENT = '<absent>'
# T12b B2 kind: the index file, the store file, and the step context of each P4 entry under comparison.
B2_INDEX = 'episode-search.sqlite3'
B2_STORE = 'episodes.sqlite3'
B2_PROFILE = ('timeout', 'isolation_level', 'uri', 'check_same_thread', 'detect_types', 'autocommit')
B2_KINDS = ('B2 index open replaced by drain/reindex', 'B2 connection removed from a seal flow',
            'B2 connection count changed')
# T12b B1 kind (Amendment 1, meet): a writable store open whose first statement became the outbox check.
B1_CHECK = 'PRAGMA user_version'
B1_KIND = 'B1 ensure check'
# The one connection outside the index and store files that T12b may retire with the index run it immediately
# precedes (Amendment 1 R3, meet): the import job's journal transaction that wrapped its inline index repair
# (session_import_job._repair_index, retired). (file name, mode, first statement)
B2_RUN_WRAPPERS = (('session-import-jobs.sqlite3', 'mode=rw', 'BEGIN IMMEDIATE'),)
# (golden file, step) -> (base step connections, head step connections), filled where differences are computed.
_P4_STEPS: dict = {}


def build(module, part):
    """The golden text this tree produces, with T11a's own generator."""
    generator = load_generator(module)
    if part is None:
        return generator.build()
    built = generator.build(part)
    return built[0] if isinstance(built, tuple) else built


def entries(document, depth):
    """{key: value} of the document's entries: dict keys and list positions, ``depth`` levels down."""
    out = {}

    def walk(node, path):
        if len(path) < depth and isinstance(node, dict) and node:
            for key, child in node.items():
                walk(child, path + [str(key)])
        elif len(path) < depth and isinstance(node, list) and node:
            for index, child in enumerate(node):
                walk(child, path + [f'#{index}'])
        else:
            out['/'.join(path)] = node
    walk(document, [])
    return out


def changes(base, head, path=''):
    """[(sub-path, base, head)] of every leaf that differs between two entry values."""
    if isinstance(base, dict) and isinstance(head, dict):
        found = []
        for key in sorted(set(base) | set(head)):
            found += changes(base.get(key, ABSENT), head.get(key, ABSENT), f'{path}.{key}' if path else str(key))
        return found
    if isinstance(base, list) and isinstance(head, list) and len(base) == len(head):
        found = []
        for index, (old, new) in enumerate(zip(base, head)):
            found += changes(old, new, f'{path}[{index}]')
        return found
    return [] if base == head else [(path or '.', base, head)]


def _events(value):
    return value.get('events', []) if isinstance(value, dict) else []


def _b2_file(profile):
    """'index' (the index or a build file beside it), 'store', 'other' or None (not a connection)."""
    if not isinstance(profile, dict) or 'database' not in profile:
        return None
    value = str((profile.get('database') or {}).get('value', ''))
    name = value.split('?', 1)[0].rsplit('/', 1)[-1]
    if name == B2_STORE:
        return 'store'
    try:
        import t12b_seams
        family = t12b_seams.index_family(name)
    except ImportError:
        family = name.startswith(B2_INDEX.split('.')[0])
    return 'index' if name == B2_INDEX or family else 'other'


def _b2_open_profile(profile):
    return tuple((name, json.dumps(profile.get(name), sort_keys=True)) for name in B2_PROFILE) + (
        ('type', (profile.get('database') or {}).get('type')),)


def _query(profile):
    value = str((profile.get('database') or {}).get('value', ''))
    name, _, query = value.partition('?')
    return name.rsplit('/', 1)[-1], query.split('&')


def b1_change(base, head):
    """True when a connection changed only as T12b's B1 kind (see the module docstring)."""
    if not isinstance(base, dict) or not isinstance(head, dict) or _b2_file(base) != 'store':
        return False
    if 'mode=rw' not in _query(base)[1] or base.get('first_statement') == B1_CHECK:
        return False
    rest = lambda profile: {k: v for k, v in profile.items() if k != 'first_statement'}  # noqa: E731
    return head.get('first_statement') == B1_CHECK and rest(base) == rest(head)


def _same(base, head):
    """Equal connection lists, modulo the B1 kind."""
    return len(base) == len(head) and all(b == h or b1_change(b, h) for b, h in zip(base, head))


def _b2_wrapper(profile):
    if not isinstance(profile, dict) or 'database' not in profile:
        return False
    name, query = _query(profile)
    return any(name == file and mode in query and profile.get('first_statement') == first
               for file, mode, first in B2_RUN_WRAPPERS)


def _b2_index_runs(base):
    """[(start, end)] of the base step's index runs (end exclusive)."""
    runs, position = [], 0
    while position < len(base):
        if _b2_file(base[position]) in ('index', 'store'):
            end = position
            while end < len(base) and _b2_file(base[end]) in ('index', 'store'):
                end += 1
            if any(_b2_file(base[i]) == 'index' for i in range(position, end)):
                runs.append((position, end))
            position = end
        else:
            position += 1
    return runs


def b2_step(base, head):
    """The base-to-head correspondence of a step whose change reads as T12b's B2 kind, else None.

    Returns {head position: base position or None} for the connections the head keeps outside the replaced runs
    (so a shifted connection can be told from a replaced one), see the module docstring for the rule."""
    if not isinstance(base, list) or not isinstance(head, list) or base == head:
        return None
    if any(_b2_file(p) is None for p in base + head):
        return None  # a step error, or anything that is not a connection profile
    runs = _b2_index_runs(base)
    if not runs:
        return None
    allowed = {kind: {_b2_open_profile(p) for p in base if _b2_file(p) == kind} for kind in ('index', 'store')}

    def drain_connection(profile):
        kind = _b2_file(profile)
        return kind in ('index', 'store') and _b2_open_profile(profile) in allowed[kind]

    kept, cursor, at = {}, 0, 0
    for start, end in runs + [(len(base), len(base))]:
        segment = base[cursor:start]
        length = len(segment)
        if not _same(segment, head[at:at + length]):
            # the wrapper that immediately precedes an index run may be retired with it (B2_RUN_WRAPPERS)
            if not (start < len(base) and segment and _b2_wrapper(segment[-1])
                    and _same(segment[:-1], head[at:at + length - 1])):
                return None
            length -= 1
            retired = True
        else:
            retired = False
        for offset in range(length):
            kept[at + offset] = cursor + offset
        at += length
        run_at = at
        while at < len(head) and (start, end) != (len(base), len(base)) and drain_connection(head[at]):
            at += 1
        if retired and _same(base[start:end], head[run_at:at]):
            return None  # a wrapper is retired only WITH its index run, never beside an unchanged one
        cursor = end
    while at < len(head) and drain_connection(head[at]):
        at += 1
    return kept if at == len(head) else None


def _b2_label(key, base, head):
    """The B2 kind of a P4 entry whose step reads as B2, else None."""
    golden, _, rest = key.partition(':')
    parts = rest.split('/')
    if len(parts) != 3 or parts[0] != 'steps' or not parts[2].startswith('#'):
        return None
    context = _P4_STEPS.get((golden, parts[1]))
    if context is None:
        return None
    kept = b2_step(*context)
    if kept is None:
        return None
    position = int(parts[2][1:])
    if kept.get(position) == position and b1_change(base, head):
        return B1_KIND  # a kept connection at its own position, changed only as B1
    if position < len(context[0]) and _b2_wrapper(context[0][position]) and position not in kept.values():
        return B2_KINDS[1]  # the wrapper, retired with its index run
    in_run = any(start <= position < end for start, end in _b2_index_runs(context[0]))
    if in_run and base != ABSENT:
        # a base connection of an index run: dropped (nothing, or a kept connection shifted into its slot) or
        # replaced by a drain/reindex connection
        return B2_KINDS[1] if head == ABSENT or position in kept else B2_KINDS[0]
    return B2_KINDS[2]


def _remember_steps(golden, base_document, head_document):
    """Record each P4 step's base and head connections, for the B2 kind (labels read them by entry key)."""
    if not isinstance(base_document, dict) or not isinstance(head_document, dict):
        return
    base_steps, head_steps = base_document.get('steps') or {}, head_document.get('steps') or {}
    for step in set(base_steps) | set(head_steps):
        _P4_STEPS[(golden, step)] = (base_steps.get(step, []), head_steps.get(step, []))


def label(family, key, base, head):
    if family == 'P4':
        kind = _b2_label(key, base, head)
        if kind is not None:
            return kind
    if base == ABSENT or head == ABSENT:
        return f'entry {"added" if base == ABSENT else "removed"}'
    if family in ('P1', 'P7'):
        return 'NOT RULED: P1/P7 must stay byte-identical'
    if family == 'P4' and b1_change(base, head):
        return B1_KIND
    if family == 'P4':
        fields = sorted({sub.split('.')[0].split('[')[0] for sub, _, _ in changes(base, head)})
        uri = isinstance(head, dict) and str((head.get('database') or {}).get('value', '')).startswith('file:')
        if set(fields) <= {'database', 'uri'} and uri:
            return 'Q2 resolved URI'
        return 'NOT A Q2 FIELD: ' + ', '.join(f for f in fields if f not in ('database', 'uri'))
    if family == 'P3':
        labels = []
        old, new = _events(base), _events(head)
        count = lambda events, kind: sum(1 for e in events if e and e[0] == kind)  # noqa: E731
        if count(new, 'os.fsync') > count(old, 'os.fsync'):
            labels.append('Q3 fsync added')
        if count(new, 'os.chmod') < count(old, 'os.chmod'):
            labels.append('Q3 chmod removed')
        nofollow = lambda events: sum(1 for e in events if e and e[0] == 'open' and 'O_NOFOLLOW' in str(e[-1]))  # noqa: E731
        if nofollow(new) > nofollow(old):
            labels.append('Q3 O_NOFOLLOW added')
        creating = lambda events: [(e[2], e[3]) for e in events if e and e[0] == 'open' and 'O_CREAT' in str(e[-1])]  # noqa: E731
        if creating(new) != creating(old):
            labels.append('Q3 creation mode/flags')
        if isinstance(base, dict) and isinstance(head, dict):
            if base.get('outcome') != head.get('outcome'):
                labels.append('outcome changed')
            if base.get('tree') != head.get('tree'):
                labels.append('tree changed')
        if old != new and not any(item.startswith('Q3') for item in labels):
            labels.append('events changed (not a Q3 kind)')
        return '; '.join(labels) or 'changed'
    if family == 'U3':
        if key.split('/', 1)[-1].startswith('config_template|'):
            return 'Q4 template'
        if any(ord(c) < 32 or ord(c) == 127 or c == '\\' for c in key):
            return 'Q1 control character'
        return 'store-path decision'
    return 'changed'


def differences(selected=None):
    """[(golden label, family, key, base, head)] for every differing entry; skipped parts are reported."""
    found, skipped = [], []
    for name, module, part, golden, depth, family in GOLDENS:
        if selected is not None and name not in selected:
            continue
        if part in OPS_PARTS:
            try:
                import kp_agent_tooling_ops  # noqa: F401
            except ImportError:
                skipped.append(name)
                continue
        base_document, head_document = json.loads((FIXTURES / golden).read_text()), json.loads(build(module, part))
        if family == 'P4':
            _remember_steps(golden, base_document, head_document)
        base, head = entries(base_document, depth), entries(head_document, depth)
        for key in sorted(set(base) | set(head)):
            old, new = base.get(key, ABSENT), head.get(key, ABSENT)
            if old != new:
                found.append((name, family, f'{golden}:{key}', old, new))
    return found, skipped


def render(found, *, limit=6):
    lines = []
    for name, family, key, old, new in found:
        lines.append(f'[{name}] {key}\n    label: {label(family, key, old, new)}'
                     + (f'\n    ruling: {RULINGS[key]}' if key in RULINGS else ''))
        delta = changes(old, new) if old != ABSENT and new != ABSENT else [('.', old, new)]
        for sub, a, b in delta[:limit]:
            lines.append(f'    {sub}: {json.dumps(a, ensure_ascii=True)[:300]}  ->  {json.dumps(b, ensure_ascii=True)[:300]}')
        if len(delta) > limit:
            lines.append(f'    ... {len(delta) - limit} more changed sub-paths')
    return '\n'.join(lines)


def ruling_mismatch(family, key, old, new):
    """None when a ruled entry's change is only of its ruling's kind, else why not (meet, T11b Amendment 1). A ruling
    names a property; the entry's label must describe that property's change and nothing else: Q2 reads exactly
    ``Q2 resolved URI``, Q3 only Q3 kinds (no outcome, tree or other event change), Q4 exactly ``Q4 template``. So a
    ruled connection whose timeout also changed, or a ruled helper whose outcome also changed, is not excused.
    (U3's label is read from the entry's key, so for Q4 the property tests, not this check, read the change.)"""
    ruling, text = RULINGS[key], label(family, key, old, new)
    if ruling.startswith('T11b Q2:'):
        ok = text == 'Q2 resolved URI'
    elif ruling.startswith('T11b Q3:'):
        ok = all(part.startswith('Q3 ') for part in text.split('; '))
    elif ruling.startswith('T11b Q4:'):
        ok = text == 'Q4 template'
    elif ruling.startswith('T12b B2:'):
        ok = family == 'P4' and text in B2_KINDS
    elif ruling.startswith('T12b B1:'):
        ok = family == 'P4' and text == B1_KIND
    else:
        ok = False
    return None if ok else f'ruled {ruling.split(":")[0]!r} but its change reads {text!r}'


def assert_matches_base_except_ruled(actual_text, golden_name):
    """T11a's P3, P4 and U3 reading at T11b's head (T11b Amendment 1, meet): the golden recomputed in this tree is
    byte-identical to base, or each entry that differs has a ruling in RULINGS and every other entry is identical.
    P1 and P7 keep T11a's byte-for-byte assertion: no ruling can name them. That every ruling names a differing entry
    is test_every_regression_set_difference_is_listed_and_ruled's check, over the whole set."""
    expected = (FIXTURES / golden_name).read_text(encoding='utf-8')
    if actual_text == expected:
        return
    (name, _module, _part, _golden, depth, family), = [row for row in GOLDENS if row[3] == golden_name]
    if family == 'P4':
        _remember_steps(golden_name, json.loads(expected), json.loads(actual_text))
    base, head = entries(json.loads(expected), depth), entries(json.loads(actual_text), depth)
    found = [(name, family, f'{golden_name}:{key}', base.get(key, ABSENT), head.get(key, ABSENT))
             for key in sorted(set(base) | set(head)) if base.get(key, ABSENT) != head.get(key, ABSENT)]
    unruled = [row for row in found if row[2] not in RULINGS]
    mismatched = [(row, ruling_mismatch(*row[1:])) for row in found if row[2] in RULINGS]
    mismatched = [(row, why) for row, why in mismatched if why]
    assert found, f'{golden_name} differs from base in its text but in no entry'
    assert not unruled, (f'{golden_name}: {len(unruled)} entries differ from base with no T11b ruling:\n'
                         + render(unruled))
    assert not mismatched, (f'{golden_name}: {len(mismatched)} ruled entries changed beyond their ruling:\n'
                            + '\n'.join(f'{row[2]}: {why}' for row, why in mismatched))


@pytest.fixture(autouse=True)
def _rulings_unchanged():
    """Every test here leaves RULINGS exactly as the meet wrote it: other files' tests (T11a P3/P4/U3, T11b Q2) read
    it in the same process, so an instrument test that set a temporary ruling must restore the real one (meet: TEST's
    `_p4_mutated` deleted the real rulings of the keys it touched once RULINGS was filled)."""
    before = dict(RULINGS)
    yield
    assert RULINGS == before, 'this test left RULINGS changed'


def test_every_regression_set_difference_is_listed_and_ruled():
    """GREEN-IF every T11a golden entry that differs in this tree has a ruling in RULINGS, every ruling names an
    entry that differs, and each ruled entry's change is only of its ruling's kind (ruling_mismatch; meet, T11b
    Amendment 1). The failure lists EVERY differing entry, unruled first, with its label and change."""
    found, skipped = differences()
    keys = {key for _, _, key, _, _ in found}
    unruled = [row for row in found if row[2] not in RULINGS]
    mismatched = [f'{row[2]}: {why}' for row in found if row[2] in RULINGS and (why := ruling_mismatch(*row[1:]))]
    compared = {golden for name, _module, _part, golden, _depth, _family in GOLDENS if name not in skipped}
    # A ruling on a golden that was not compared (extensions/ops absent: CI's core-only jobs) is not stale; the
    # ext job collects this test too (extensions/ops/tests/test_t11a_ops_goldens.py), where every golden is.
    stale = sorted(key for key in set(RULINGS) - keys if key.split(':', 1)[0] in compared)
    message = []
    if unruled:
        per = {}
        for row in unruled:
            per[row[0]] = per.get(row[0], 0) + 1
        message.append(f'{len(unruled)} golden entries differ from base with no ruling '
                       f'({", ".join(f"{k}: {v}" for k, v in per.items())}):\n' + render(unruled))
    if stale:
        message.append(f'{len(stale)} rulings name an entry that does not differ: {stale}')
    if mismatched:
        message.append(f'{len(mismatched)} ruled entries changed beyond their ruling:\n' + '\n'.join(mismatched))
    if skipped:
        message.append(f'(not compared, extensions/ops not installed: {skipped})')
    assert not unruled and not stale and not mismatched, '\n\n'.join(message)


def test_instrument_the_check_lists_exactly_the_entries_that_differ():
    """Instrument check: on the base goldens with two entries altered in memory (one P3 event, one U3 decision),
    the differ reports exactly those two entries and nothing else."""
    for golden, depth, mutate in (
            ('private_file_sequences.json', 3,
             lambda d: d['helpers']['launch_binding.write_private']['new file']['events'].pop()),
            ('store_path_decisions.json', 2,
             lambda d: d['container.operator_store_paths']["state_root|'/state/registry'"].update(refuse=False))):
        text = (FIXTURES / golden).read_text()
        base = entries(json.loads(text), depth)
        document = json.loads(text)
        mutate(document)
        head = entries(document, depth)
        differing = [key for key in sorted(set(base) | set(head)) if base.get(key) != head.get(key)]
        assert len(differing) == 1, f'{golden}: the differ reported {differing}'
        assert changes(base[differing[0]], head[differing[0]]), 'the changed sub-path was not found'



def _p4_mutated(mutate):
    """[(entry key, label, ruling_mismatch under a `T12b B2:` ruling)] of every entry of the P4 core golden that
    differs once `mutate` is applied to a copy in memory (the ruling is set for the call only)."""
    text = (FIXTURES / 'connection_profiles.json').read_text()
    base, head = json.loads(text), json.loads(text)
    mutate(head)
    _remember_steps('connection_profiles.json', base, head)
    old_entries, new_entries = entries(base, 3), entries(head, 3)
    rows = []
    for key in sorted(set(old_entries) | set(new_entries)):
        old, new = old_entries.get(key, ABSENT), new_entries.get(key, ABSENT)
        if old == new:
            continue
        full = f'connection_profiles.json:{key}'
        saved = RULINGS.get(full)  # the meet's real ruling for this key, if any: restored, never deleted
        RULINGS[full] = 'T12b B2: instrument check'
        try:
            rows.append((full, label('P4', full, old, new), ruling_mismatch('P4', full, old, new)))
        finally:
            if saved is None:
                del RULINGS[full]
            else:
                RULINGS[full] = saved
    return rows


def _drop(step, start, end):
    def mutate(document):
        del document['steps'][step][start:end]
    return mutate


def _drained_tail(document):
    """The Codex hook's inline index run removed, and a drain (an index and a store connection with the base
    profiles) appended after the step's last connection."""
    steps = document['steps']['launch: first codex hook']
    run = steps[39:43]
    del steps[39:43]
    steps += [dict(run[2]), dict(run[0])]


def _build_file(document):
    step = document['steps']['search index: rebuild']
    profile = json.loads(json.dumps(step[1]))
    profile['database']['value'] = profile['database']['value'].replace(B2_INDEX, 'episode-search.build.sqlite3')
    step[1] = profile


def test_instrument_t12b_b2_kind_reads_an_index_open_removed_or_replaced():
    """Instrument check (T12b B4): on the base P4 golden mutated in memory as T12b's rulings name (`import jobs:
    advance` #7 and `launch: first codex hook` #41 removed with their index runs, a drain after the flow, `search
    index: rebuild` #1 replaced by a build file), every differing entry reads as a B2 kind and a `T12b B2:` ruling
    accepts it. GREEN at base (it tests the instrument)."""
    for mutate in (_drop('import jobs: advance', 5, 9), _drop('launch: first codex hook', 39, 43), _drained_tail,
                   _build_file):
        rows = _p4_mutated(mutate)
        assert rows, 'the mutation changed no entry'
        wrong = [(key, text, why) for key, text, why in rows if text not in B2_KINDS or why is not None]
        assert not wrong, f'entries that should read as B2: {wrong}'
    assert {text for _, text, _ in _p4_mutated(_build_file)} == {B2_KINDS[0]}
    assert B2_KINDS[1] in {text for _, text, _ in _p4_mutated(_drop('import jobs: advance', 5, 9))}


def test_instrument_t12b_b2_kind_excuses_nothing_else():
    """Instrument check (T12b B4): a `T12b B2:` ruling is refused for an entry whose change is not of the B2 kind:
    a timeout changed beside a removed index run, a store connection's first statement changed (as an outbox check
    on a seal connection would change it), a step error instead of the rebuild's connections, and any P7 entry.
    GREEN at base (it tests the instrument)."""
    def timeout(document):
        _drop('import jobs: advance', 5, 9)(document)
        document['steps']['import jobs: advance'][0]['timeout'] = 5.0

    def first_statement(document):
        document['steps']['episode store: capture'][2]['first_statement'] = 'SELECT 1 FROM sqlite_master WHERE name=\'\''

    def step_error(document):
        document['steps']['search index: rebuild'] = [{'step_error': "AttributeError: no rebuild"}]
    for mutate in (timeout, first_statement, step_error):
        rows = _p4_mutated(mutate)
        assert rows and all(why is not None for _, _, why in rows), (
            f'a B2 ruling excused a change that is not of the B2 kind: {[r for r in rows if r[2] is None]}')
    text = (FIXTURES / 'rollout_capture.json').read_text()
    key = next(iter(entries(json.loads(text), 3)))
    RULINGS['rollout_capture.json:' + key] = 'T12b B2: instrument check'
    try:
        assert ruling_mismatch('P7', 'rollout_capture.json:' + key, 'a', 'b') is not None, 'a B2 ruling excused P7'
    finally:
        del RULINGS['rollout_capture.json:' + key]



def _ruled_rows(mutate):
    """[(entry key, label, ruling_mismatch)] of every entry of the P4 core golden that differs once `mutate` is
    applied in memory, each under the ruling of its label's kind (`T12b B1:` for B1, else `T12b B2:`)."""
    text = (FIXTURES / 'connection_profiles.json').read_text()
    base, head = json.loads(text), json.loads(text)
    mutate(head)
    _remember_steps('connection_profiles.json', base, head)
    old_entries, new_entries = entries(base, 3), entries(head, 3)
    rows = []
    for key in sorted(set(old_entries) | set(new_entries)):
        old, new = old_entries.get(key, ABSENT), new_entries.get(key, ABSENT)
        if old == new:
            continue
        full = f'connection_profiles.json:{key}'
        text_label = label('P4', full, old, new)
        saved = RULINGS.get(full)
        RULINGS[full] = 'T12b B1: instrument check' if text_label == B1_KIND else 'T12b B2: instrument check'
        try:
            rows.append((full, text_label, ruling_mismatch('P4', full, old, new)))
        finally:
            if saved is None:
                del RULINGS[full]
            else:
                RULINGS[full] = saved
    return rows


def _first(step, position, statement):
    def mutate(document):
        document['steps'][step][position]['first_statement'] = statement
    return mutate


def test_instrument_t12b_b1_kind_reads_the_ensure_check_and_nothing_else():
    """Instrument check (T12b Amendment 1, meet): a writable store open whose first statement became exactly
    `PRAGMA user_version`, every other field identical, reads `B1 ensure check` and a `T12b B1:` ruling accepts it;
    a `T12b B2:` ruling does not. Refused under every ruling: DDL as the first statement, the check on another file,
    the check on a read-only store open, and the check beside a changed timeout. GREEN at base (instrument)."""
    rows = _ruled_rows(_first('episode store: capture', 2, B1_CHECK))
    assert [(text, why) for _, text, why in rows] == [(B1_KIND, None)], rows
    key, _, _ = rows[0]
    saved = RULINGS.get(key)
    RULINGS[key] = 'T12b B2: instrument check'
    try:
        text = (FIXTURES / 'connection_profiles.json').read_text()
        old = entries(json.loads(text), 3)[key.split(':', 1)[1]]
        new = dict(old, first_statement=B1_CHECK)
        assert ruling_mismatch('P4', key, old, new) is not None, 'a B2 ruling excused a B1 change'
    finally:
        if saved is None:
            del RULINGS[key]
        else:
            RULINGS[key] = saved

    def timeout_too(document):
        _first('episode store: capture', 2, B1_CHECK)(document)
        document['steps']['episode store: capture'][2]['timeout'] = 30.0
    refused = (
        ('DDL first', _first('episode store: capture', 2, 'CREATE TABLE IF NOT EXISTS index_outbox (seq INTEGER)')),
        ('another file', _first('launch: first codex hook', 43, B1_CHECK)),   # rollout-capture rw
        ('read-only store open', _first('import jobs: advance', 11, B1_CHECK)),  # outside every index run
        ('timeout changed too', timeout_too),
    )
    for name, mutate in refused:
        rows = _ruled_rows(mutate)
        assert rows and all(text != B1_KIND for _, text, _ in rows), f'{name}: read as B1: {rows}'
        assert all(why is not None for _, _, why in rows), f'{name}: excused: {rows}'


def test_instrument_t12b_b2_wrapper_is_retired_only_with_its_index_run():
    """Instrument check (T12b Amendment 1 R3, meet): the import job's journal transaction (`import jobs: advance`
    #4) may be dropped together with the index run it immediately precedes (#5..#8), or kept while the run is
    dropped; it may not be dropped alone, and no other non-index connection may be dropped with a run (the job's
    read at #9 after the run). B1 changes outside the runs are read as B1 in the same step. GREEN at base."""
    accepted = (_drop('import jobs: advance', 4, 9), _drop('import jobs: advance', 5, 9))

    def with_b1(document):
        _drop('import jobs: advance', 4, 9)(document)
        _first('import jobs: advance', 1, B1_CHECK)(document)
    for mutate in accepted + (with_b1,):
        rows = _ruled_rows(mutate)
        assert rows and all(why is None for _, _, why in rows), f'not excused: {[r for r in rows if r[2]]}'
    assert B1_KIND in {text for _, text, _ in _ruled_rows(with_b1)}
    assert B2_KINDS[1] in {text for _, text, _ in _ruled_rows(_drop('import jobs: advance', 4, 9))}
    for name, mutate in (('wrapper alone', _drop('import jobs: advance', 4, 5)),
                         ('the read after the run', _drop('import jobs: advance', 5, 10)),
                         ('the first job transaction', _drop('import jobs: advance', 0, 1))):
        rows = _ruled_rows(mutate)
        assert rows and any(why is not None for _, _, why in rows), f'{name}: every entry excused: {rows}'


if __name__ == '__main__':
    rows, skipped_parts = differences()
    print(f'T11b regression set: {len(rows)} differing golden entries in this tree'
          + (f' (not compared: {skipped_parts})' if skipped_parts else ''))
    for name, *_ in GOLDENS:
        mine = [row for row in rows if row[0] == name]
        print(f'  {name}: {len(mine)}')
    if rows:
        print()
        print(render(rows, limit=50))
