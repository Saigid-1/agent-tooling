"""Operator-built, source-verified full-text projection of immutable episodes.

This index ranks literal evidence. It makes no semantic or truth claim, and a
model-facing search never changes either the index or the episode store.

One indexer (T12b). Every insert into a watched store table appends a row to the
store's ``index_outbox`` in the same transaction (``episodic_memory``). ``drain`` is
the one writer of this file's postings, its outbox watermark and its coverage token:
under the lease, a non-blocking lock on ``<state_root>/index.lock``, it applies the
outbox rows above the watermark, at most ``batch`` per index transaction, and advances
the watermark in that transaction; then it deletes the applied rows, at most ``batch``
per store transaction. A ``reindex`` row makes it build a new file beside this one and
swap it in; nothing is deleted in place. Inside Compose (``AGENT_MEMORY_VOLUME`` set)
only the ``indexer`` role drains (``watch``); a host install drains once after each seal
(``drain_after_seal``) and never waits for the lease.

Per-desk postings (T12c). Each event row carries the scope tokens of its episode (its desks and
tenant, from the store's scope projection when the row is applied), so a desk or topic search
matches its scope AND its phrase in one full-text query and ranks nothing outside the scope. A
``desks_changed`` outbox row rewrites its session's tokens when it is applied; until then a read
consults the row in the store. The index schema carries a version: a file of another version is
rebuilt by the indexer's own reindex request, and answers searches until the swap.
"""

import json
import os
import re
import secrets
import socket
import sqlite3
import time
import unicodedata
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_binding import call_scope
from kp_agent_tooling._impl.service.episodic_memory import EpisodeStore, EpisodeUnavailable, _id, outbox_lag
from kp_agent_tooling._impl.service.episodic_provenance import origin


# The index schema (T12c). Version 2 adds the postings: each event row carries, in its indexed ``scope`` column, a
# token per scope its episode is in, keyed as the store's scope aggregates are (``scope_counts``): ('owner', desk,
# tenant) and ('tenant', tenant, '') for a linked episode, ('storage', binding, '') for an unlinked one; plus its
# session's token (a reassignment not yet applied is read through it) or, when the store had no scope projection
# for the episode, the token ``u``, which every narrowed search includes. A desk or topic search matches its
# scope's tokens AND the phrase in one full-text query, so it narrows by scope before anything is ranked
# (``_scoped_candidates``). ``indexed_episodes`` records each episode's scope tokens and the rowid range of its
# events (they are written together); ``event_tokens`` each event's text and scope token counts. The
# ranking is unchanged: FTS5's bm25 over the text alone, recomputed from the index's own statistics (``_ranked``,
# ``_RANK_KEY``). A file of another version is rebuilt by the indexer (``_drain``); version 1, the schema before
# postings, keeps answering searches until then.
INDEX_VERSION = 2
_SCHEMA = """
CREATE TABLE metadata (version INTEGER NOT NULL);
INSERT INTO metadata VALUES (2);
CREATE TABLE indexed_episodes (
    episode_id TEXT PRIMARY KEY, binding TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    first_event INTEGER NOT NULL, last_event INTEGER NOT NULL, scope TEXT NOT NULL
);
CREATE VIRTUAL TABLE event_search USING fts5(
    binding UNINDEXED, episode_id UNINDEXED, event_id UNINDEXED, text, scope,
    tokenize='unicode61'
);
CREATE TABLE event_tokens (id INTEGER PRIMARY KEY, text_tokens INTEGER NOT NULL, scope_tokens INTEGER NOT NULL);
CREATE TABLE coverage_token (token TEXT NOT NULL);
CREATE TABLE outbox_watermark (id INTEGER PRIMARY KEY CHECK (id = 0), seq INTEGER NOT NULL);
"""
# Versions a reader can search: version 1 (no postings) is searched as before T12c until its rebuild lands.
_READABLE = (1, INDEX_VERSION)
# The scope token of an episode the store had no projection for (``_scopes``).
_UNPOSTED = 'u'
# event_search's text and scope columns (their position in a ``%_docsize`` record and in highlight()).
_TEXT, _SCOPE = 3, 4
# Identity of this index file. The source store records which index its
# per-episode coverage marks describe; a replaced or recreated index starts
# from no coverage. Older readers ignore the table.
_TOKEN = 'CREATE TABLE IF NOT EXISTS coverage_token (token TEXT NOT NULL)'
# The outbox watermark: the highest store outbox seq this file has applied. It advances
# in the transaction that writes the postings, so a restart resumes exactly above it.
# Older readers ignore the table; a file without it has applied no outbox row.
_WATERMARK = ('CREATE TABLE IF NOT EXISTS outbox_watermark '
              '(id INTEGER PRIMARY KEY CHECK (id = 0), seq INTEGER NOT NULL)')
# Outbox rows applied per index transaction, and deleted per store transaction.
DRAIN_BATCH = 500
# Episodes per coverage-mark store transaction (SessionSources.mark_coverage, every caller), and
# episode_scope rows per coverage-reset transaction (SessionSources._reset_coverage). Its own
# quantity, not DRAIN_BATCH (outbox rows per index transaction): it bounds each write lock coverage
# holds on the store, so a sealer's commit waits for one short transaction, well inside its busy
# timeout, even under the indexer role's CPU limit.
MARK_BATCH = 100
# The indexer is unhealthy when its watermark has not advanced across this many
# consecutive drains that were not lease skips while outbox rows exist.
STALL_DRAINS = 5
# A search on a version 2 index whose scope holds at most SMALL_MATCHES matches of its phrase (and whose phrase
# tokens cannot repeat) takes FTS5's phrase frequency from highlight() and ranks in Python (``_ranked``); any other
# ranks in SQL from two bm25() calls (``_RANK_KEY``): after the first read when the scope holds more matches, at
# once for a phrase whose tokens may repeat. Both reproduce FTS5's ``ORDER BY rank`` over the text column exactly.
# The first reads nothing that grows with other scopes' matches; the second has bm25 count the phrase's matches in
# the whole index (in C, not in SQLite's VM), and costs less per match.
SMALL_MATCHES = 2000
# A desk search finds the waiting reassignments into an unfiltered desk of at most SMALL_SCOPE episodes by walking
# the desk's episodes in the store; a larger or filtered one, by the claims of the waiting sessions
# (``_scoped_candidates``).
SMALL_SCOPE = 2000
# A search ranked in SQL reads content and checks the scope predicate for the first RANK_WINDOW matches by rank, and
# for more only when fewer than 201 of them pass (``_scoped_candidates``).
RANK_WINDOW = 1000
# FTS5's bm25 constants (fts5_aux.c, fts5Bm25Function): k1 and b.
_BM25_K1 = 1.2
_BM25_B = 0.75
# Lost-marks records whose own write failed (the store busy), by store path: written again on the next pass.
_LOST_UNWRITTEN = {}
INDEXER_SCHEMA = 'agent-tooling.indexer.v1'
# The indexer walks at most this many directories under its root per pass (launch directories and
# symlinks excluded); stores beyond the bound are not drained in that pass.
WALK_BOUND = 10000
# A failed indexer pass waits interval * 2**(attempt - 1), at most this many intervals (T12a).
BACKOFF_CAP_INTERVALS = 10
MESSAGE_CHARS = 512


class IndexBusy(EpisodeUnavailable):
    """The index lease, or the old index file, is held by another writer: a reindex does not start or
    swap (it never waits)."""
    category = 'index_busy'


class ReindexRefused(EpisodeUnavailable):
    """A built index does not hold every (episode, digest) the store's coverage marks claim."""
    category = 'reindex_refused'


class OutboxAhead(EpisodeUnavailable):
    """The index has applied outbox rows this store never issued: it belongs to another store history."""
    category = 'index_ahead_of_outbox'


def _ensure_token(db):
    db.execute(_TOKEN)
    row = db.execute('SELECT token FROM coverage_token').fetchone()
    if row is None:
        token = secrets.token_hex(16)
        db.execute('INSERT INTO coverage_token (token) VALUES (?)', (token,))
        return token
    return row[0]


def _watermark(db):
    try:
        row = db.execute('SELECT seq FROM outbox_watermark WHERE id = 0').fetchone()
    except sqlite3.OperationalError:
        return 0  # no table: this file has applied no outbox row
    return 0 if row is None else row[0]


def _events(kind, identity, owner, session, raw):
    """The [(episode, event, text)] of one sealed row, verified as ``EpisodeStore._read`` (kind 0,
    ``episodes``, ``owner`` its binding) and ``SessionSources.read`` (kind 1, ``source_episodes``,
    ``owner`` its tenant) verify it."""
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if kind == 0:
        if not isinstance(value, dict) or _id('episode', value) != identity or value.get('binding_key') != owner:
            raise EpisodeUnavailable('record integrity mismatch')
    elif (not isinstance(value, dict) or _id('episode', value) != identity
          or (value.get('tenant_id'), value.get('source_session_id')) != (owner, session)):
        raise EpisodeUnavailable('source episode integrity mismatch')
    return [(identity, event['event_id'], event['text']) for event in value['events']]


def _verified(rows):
    """{episode: (binding, payload digest, events)} of sealed rows ``(kind, id, owner, session, payload)``.
    The binding is the storage binding of an ``episodes`` row and ``''`` for a source episode."""
    verified = {}
    for kind, identity, owner, session, raw in rows:
        events = _events(kind, identity, owner, session, raw)
        verified[identity] = (owner if kind == 0 else '', leaf.sha256_hex(raw), events)
    return verified


def _has_sources(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'source_episodes'"
                      ).fetchone() is not None


def _sealed(db, identities):
    """{episode: verified row} for the given sealed episodes, in one read of the store."""
    ids = json.dumps(identities)
    sql = 'SELECT 0, id, binding, NULL, payload FROM episodes WHERE id IN (SELECT value FROM json_each(?))'
    params = [ids]
    if _has_sources(db):
        sql += (' UNION ALL SELECT 1, id, tenant, session_id, payload FROM source_episodes'
                ' WHERE id IN (SELECT value FROM json_each(?))')
        params.append(ids)
    found = _verified(db.execute(sql, params))
    if len(found) != len(identities):
        raise EpisodeUnavailable('source episode unavailable for indexing')
    # In the caller's order (the outbox's seq order): postings are written in it, and FTS rank
    # ties keep that order.
    return {identity: found[identity] for identity in identities}


def _token(kind, value):
    """One scope token: ``kind`` (o owner, t tenant) and the first 16 hex digits of the sha256 of ``value``. It holds
    only [0-9a-z], so unicode61 reads it as one token. Two keys that share a token only nominate each other's
    episodes: the store's scope predicate still decides."""
    return kind + leaf.sha256_hex(value.encode())[:16]


def _storage_token(binding):
    """An unlinked episode's token: ``b`` and the hex of its storage binding's last 16 characters. A topic read
    builds the same text in SQL, ``'b' || lower(hex(substr(key, -16)))``, for the tenant's bindings that hold
    unlinked episodes (``_scoped_candidates``)."""
    return 'b' + binding[-16:].encode().hex()


def _session_token(session):
    """A session's token: ``s`` and the hex of the 16 characters after its ``source-session:sha256:`` prefix (the
    first 64 bits of its digest). A read builds the same text in SQL, ``'s' || lower(hex(substr(session_id, 23,
    16)))`` (``_scoped_candidates``), and the hex holds only [0-9a-f] whatever the identifier holds."""
    return 's' + session[22:38].encode().hex()


def _scopes(source, episodes, sessions):
    """{episode: scope tokens} of ``episodes`` and of every episode of ``sessions``, read from the store's scope
    projection now, on the caller's store connection: a linked episode is in its session's tenant, in each of its
    desks with that tenant (``episode_desks``) and carries its session's token; an unlinked one is in its storage
    binding (the ``scope_counts`` keys of ``SessionSources._contributions``, the ``unresolved`` count aside). An
    episode the store has no projection row for gets ``_UNPOSTED``."""
    from kp_agent_tooling._impl.service.session_sources import _projecting
    episodes = list(dict.fromkeys(episodes))
    if not _projecting(source):
        return dict.fromkeys(episodes, _UNPOSTED)
    found = source.execute(
        'WITH touched(episode_id) AS (SELECT value FROM json_each(?)'
        ' UNION SELECT episode_id FROM episode_scope WHERE source_session_id IN (SELECT value FROM json_each(?)))'
        ' SELECT t.episode_id, s.kind, s.tenant, s.storage_binding, s.source_session_id, d.desk FROM touched t'
        ' LEFT JOIN episode_scope s ON s.episode_id = t.episode_id'
        ' LEFT JOIN episode_desks d ON d.episode_id = t.episode_id',
        (json.dumps(episodes), json.dumps(list(dict.fromkeys(sessions))))).fetchall()
    tokens = {}
    for identity, kind, tenant, storage, session, desk in found:
        held = tokens.setdefault(identity, set())
        if kind is None:
            continue
        if tenant is None:
            held.add(_storage_token(storage))
            continue
        held.add(_token('t', tenant))
        if session is not None:
            held.add(_session_token(session))
        if desk is not None:
            held.add(_token('o', desk + '\x1f' + tenant))
    return {identity: ' '.join(sorted(held)) or _UNPOSTED for identity, held in tokens.items()}


def _post(db, verified, scopes):
    """Write the postings of verified episodes, and rewrite the scope tokens of ``scopes``' episodes, in the
    caller's index transaction.

    An episode's events are inserted together, so their rowids are the range [first_event, last_event] its
    indexed_episodes row records, beside the scope tokens they carry. An episode already indexed with the same
    binding and digest keeps its rows; one whose digest changed has its range deleted and written again. An indexed
    episode whose scope tokens changed (``scopes``, read from the store at apply time) has its events' ``scope``
    rewritten in place, keeping their rowids, so FTS rank ties keep their order.
    """
    for identity, (binding, digest, events) in verified.items():
        scope = scopes.get(identity, _UNPOSTED)
        prior = db.execute('SELECT binding, payload_sha256, first_event, last_event, scope FROM indexed_episodes'
                           ' WHERE episode_id = ?', (identity,)).fetchone()
        if prior is not None and prior[:2] == (binding, digest):
            continue  # its scope, if changed, is rewritten below
        if prior is not None:
            db.execute('DELETE FROM event_search WHERE rowid BETWEEN ? AND ?', prior[2:4])
            db.execute('DELETE FROM event_tokens WHERE id BETWEEN ? AND ?', prior[2:4])
        db.executemany('INSERT INTO event_search(binding,episode_id,event_id,text,scope) VALUES (?,?,?,?,?)',
                       [(binding, *event, scope) for event in events])
        last = db.execute('SELECT last_insert_rowid()').fetchone()[0] if events else 0
        db.executemany('INSERT INTO event_tokens (id, text_tokens, scope_tokens) VALUES (?, ?, ?)', [
            (rowid, *_varints(sizes)[_TEXT:_SCOPE + 1]) for rowid, sizes in db.execute(
                'SELECT id, sz FROM event_search_docsize WHERE id BETWEEN ? AND ?', (last - len(events) + 1, last))])
        db.execute('INSERT OR REPLACE INTO indexed_episodes (episode_id, binding, payload_sha256, first_event,'
                   ' last_event, scope) VALUES (?,?,?,?,?,?)',
                   (identity, binding, digest, last - len(events) + 1, last, scope))
    if not scopes:
        return
    for identity, first, last, held in db.execute(
            'SELECT episode_id, first_event, last_event, scope FROM indexed_episodes'
            ' WHERE episode_id IN (SELECT value FROM json_each(?))', (json.dumps(list(scopes)),)).fetchall():
        if held != scopes[identity]:
            db.execute('UPDATE event_search SET scope = ? WHERE rowid BETWEEN ? AND ?', (scopes[identity], first, last))
            db.execute('UPDATE event_tokens SET scope_tokens = ? WHERE id BETWEEN ? AND ?',
                       (len(scopes[identity].split(' ')), first, last))
            db.execute('UPDATE indexed_episodes SET scope = ? WHERE episode_id = ?', (scopes[identity], identity))


def _varints(blob):
    """The SQLite varints of an FTS5 record (the ``%_docsize`` sizes, the ``%_data`` averages), in order."""
    values, at = [], 0
    while at < len(blob):
        value = 0
        for position in range(9):
            byte = blob[at]
            at += 1
            if position == 8:
                value = (value << 8) | byte
                break
            value = (value << 7) | (byte & 0x7f)
            if not byte & 0x80:
                break
        values.append(value)
    return values


def _repeats(terms):
    """Whether the phrase of ``terms`` may hold a token twice once FTS5 tokenizes it (unicode61 folds case and
    diacritics). Then two of its matches can overlap, and FTS5's highlight() would count them as one: such a
    phrase is ranked in SQL from bm25(), which counts each match. Conservative: the folding here is at least as
    coarse as unicode61's and its split at least as fine, so a repeat there is a repeat here."""
    pieces = []
    for term in terms:
        folded = unicodedata.normalize('NFKD', unicodedata.normalize('NFKD', term).casefold())
        folded = ''.join(char for char in folded if not unicodedata.category(char).startswith('M'))
        pieces += [piece for piece in re.split(r'[\W_]+', folded) if piece]
    return len(pieces) != len(set(pieces))


def _ranked(rows, averages, limit):
    """The first ``limit`` of ``rows`` in the order FTS5's ``ORDER BY rank`` gives them on the text alone (bm25,
    fts5_aux.c), as a version 1 index (text only) ranks them.

    Each row is (rowid, phrase frequency, text token count, *candidate). bm25's score for a one-phrase query is
    idf * f*(k1+1) / (f + k1*(1 - b + b*D/avgdl)): idf is the same for every row, so the order is that of the
    fraction, computed here as fts5Bm25Function computes it, from the text column's statistics (D, the row's text
    token count, ``event_tokens``, copied from ``%_docsize``; avgdl, the text column's token total over the row
    count in the ``%_data`` averages record; f, the phrase's instances in the row). Equal scores keep rowid order,
    as FTS5's sorter does."""
    if not rows:
        return []
    count, *totals = _varints(averages)
    average = float(totals[_TEXT]) / float(count)
    keyed = []
    for rowid, frequency, tokens, *candidate in rows:
        length = float(tokens)
        freq = float(frequency)
        score = (freq * (_BM25_K1 + 1.0)) / (freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / average))
        keyed.append((-score, rowid, tuple(candidate)))
    keyed.sort(key=lambda item: item[:2])
    return [candidate for _, _, candidate in keyed[:limit]]


# A search that ranks in SQLite computes f, the phrase's instances in a row, from two bm25() calls weighing only the
# text column by 1 and 2 (``one``, ``two``): their ratio r = 2(f + k1*K)/(2f + k1*K), with K = 1 - b + b*D/avgdl
# over every column (``dt`` the row's text tokens plus ``ns`` its scope tokens, both from ``event_tokens``; the
# table's average is the parameter), gives f = k1*K*(2 - r)/(2(r - 1)), an integer; idf cancels.
_FREQUENCY = (f'CAST(round({_BM25_K1!r} * (1 - {_BM25_B!r} + {_BM25_B!r} * (dt + ns) / ?) * (2 - two / one)'
              ' / (2 * (two / one - 1))) AS INTEGER)')
# ``_ranked``'s fraction in SQL, from f and ``dt`` and the text column's average (the parameter).
_RANK_KEY = f'((f * ({_BM25_K1!r} + 1.0)) / (f + {_BM25_K1!r} * (1 - {_BM25_B!r} + {_BM25_B!r} * dt / ?)))'


def _counts(verified):
    return {'indexed_episodes': len(verified),
            'indexed_events': sum(len(events) for _, _, events in verified.values())}


def _digests(verified):
    return {identity: digest for identity, (_, digest, _) in verified.items()}


def _pending(outbox):
    """The ``pending`` CTE: the store's desks_changed outbox rows above the index's watermark (session, linked
    episode), the reassignments the index may not hold yet. The rows at or below the watermark that retention has
    not deleted were applied with the tokens: they are not read."""
    if not outbox:
        return 'pending(session_id, episode_id) AS (SELECT NULL, NULL WHERE 0)'
    return ("pending(session_id, episode_id) AS (SELECT session_id, episode_id FROM index_outbox"
            " WHERE seq > coalesce((SELECT seq FROM idx.outbox_watermark WHERE id = 0), 0)"
            " AND reason = 'desks_changed')")


def _scoped_candidates(source, plan, phrase, terms, total, averages, outbox):
    """The plan's candidates in rank order, at most 201, as (episode, event, text, indexed digest, projected
    digest, covered): one statement on the call's store connection, the index attached.

    The full-text query is the scope's postings AND the phrase: ``scope : (<tokens>) AND text : "<phrase>"``. The
    tokens are the scope's keys (as ``SessionSources.counts`` keys it: desk ('owner', desk, tenant) and ('storage',
    desk, ''); topic ('tenant', tenant, '') and ('storage', b, '') for each of the tenant's bindings in the live
    registry that holds unlinked episodes, by the store's ``scope_counts``, read in the statement) and
    ``_UNPOSTED``. So nothing outside the scope is matched or ranked. The tokens only nominate: every candidate
    passes the store's scope predicate (``Scope.member``) and the plan's filters, as before.

    A reassignment the index has not applied yet (``_pending``) is read from the store in the same statement:
    - a claim can move a session's episodes into a desk. A desk search adds the token of each waiting session with
      an episode in the desk by the store's projection (an unfiltered desk of at most SMALL_SCOPE episodes: a walk
      of its episodes), or of each waiting session of its tenant one of whose claims names the desk (otherwise: a
      session's desks come only from its owner claims, so this is a superset; a claim that is superseded, ranged or
      of another predicate only adds a candidate). Claims do not move episodes between tenants.
    - a link moves one episode into its session. Its events carry the tokens it had before, so the episode of each
      waiting link is matched by itself, within its rowid range, when the store places it in the scope.

    When the phrase's tokens cannot repeat (``_repeats``), at most SMALL_MATCHES + 1 matches are read with their
    phrase frequency (highlight()) and text token count; at most SMALL_MATCHES are ranked in Python (``_ranked``).
    Otherwise, in a second statement, SQLite ranks every match by ``_RANK_KEY`` from the index alone and reads content
    and the scope predicate only for the first RANK_WINDOW, in rank order (more only when fewer than 201 of them
    pass), and returns the first 201."""
    if plan.scope == 'desk':
        tokens = [_token('o', plan.binding + '\x1f' + plan.tenant), _storage_token(plan.binding)]
        if not plan.filtered and total <= SMALL_SCOPE:
            # The desk's own episodes in the store whose session waits (nothing at all when no row waits): costs the
            # desk's size, whatever the waiting rows touch.
            added = (" || coalesce((SELECT group_concat(token, '') FROM (SELECT DISTINCT"
                     " ' OR s' || lower(hex(substr(p.source_session_id, 23, 16))) AS token"
                     ' FROM (SELECT 1 FROM pending LIMIT 1) g CROSS JOIN episode_desks w CROSS JOIN episode_scope p'
                     " WHERE w.desk = ? AND w.tenant IN (?, '') AND p.episode_id = w.episode_id"
                     ' AND +p.source_session_id IN (SELECT session_id FROM pending))), \'\')')
            added_params = [plan.binding, plan.tenant]
        else:
            # Each waiting session of the tenant one of whose claims names the desk: costs the waiting sessions and
            # their claims, whatever the desk's size.
            added = (" || coalesce((SELECT group_concat(' OR s' || lower(hex(substr(q.session_id, 23, 16))), '')"
                     ' FROM (SELECT DISTINCT session_id FROM pending WHERE session_id IS NOT NULL) q'
                     ' CROSS JOIN source_sessions w'
                     ' WHERE w.id = q.session_id AND w.tenant = ? AND EXISTS (SELECT 1 FROM session_claims c'
                     ' WHERE c.session_id = q.session_id AND instr(CAST(c.payload AS TEXT), ?) > 0)), \'\')')
            added_params = [plan.tenant, json.dumps(plan.binding)]
    else:
        tokens = [_token('t', plan.tenant)]
        added = (" || coalesce((SELECT group_concat(' OR b' || lower(hex(substr(c.key, -16))), '')"
                 " FROM scope_counts c WHERE c.scope = 'storage' AND c.key IN (SELECT value FROM json_each(?))"
                 " AND c.tenant = '' AND c.episodes > 0), '')")
        added_params = [json.dumps(sorted(plan.tenant_bindings))]
    member, member_params = plan.member()
    filters, filter_params = plan.filters()
    kept, kept_params, _, _ = plan.view_terms()
    scoped = ' AND '.join([member] + filters + [kept])
    scope_params = member_params + filter_params + kept_params
    pending = _pending(outbox)
    # The scope's postings AND the phrase.
    match = f'e.event_search MATCH (?{added} || ?)'
    match_params = ['scope : (' + ' OR '.join(tokens + [_UNPOSTED])] + added_params + [') AND text : ' + phrase]
    frequency = f"length(highlight(e.event_search, {_TEXT}, char(1), '')) - length(e.text)"
    candidate = 'e.episode_id, e.event_id, e.text, i.payload_sha256, s.payload_sha256, s.covered'
    main = (' FROM idx.event_search e CROSS JOIN idx.event_tokens t CROSS JOIN episode_scope s'
            ' LEFT JOIN idx.indexed_episodes i ON i.episode_id = s.episode_id'
            f' WHERE {match} AND t.id = e.rowid AND s.episode_id = e.episode_id AND {scoped}')
    # The episodes of waiting links, each matched within its own rowid range.
    linked = (' FROM (SELECT DISTINCT episode_id FROM pending WHERE episode_id IS NOT NULL) l'
              ' CROSS JOIN episode_scope s CROSS JOIN idx.indexed_episodes i CROSS JOIN idx.event_search e'
              f' CROSS JOIN idx.event_tokens t WHERE s.episode_id = l.episode_id AND {scoped}'
              ' AND i.episode_id = s.episode_id AND e.event_search MATCH ?'
              ' AND e.rowid BETWEEN i.first_event AND i.last_event AND t.id = e.rowid')
    linked_params = scope_params + ['text : ' + phrase]
    if not _repeats(terms):
        rows = source.execute(
            f'WITH {pending} SELECT e.rowid, {frequency}, t.text_tokens, {candidate}{main}'
            f' UNION ALL SELECT e.rowid, {frequency}, t.text_tokens, {candidate}{linked} LIMIT ?',
            match_params + scope_params + linked_params + [SMALL_MATCHES + 1]).fetchall()
        if len(rows) <= SMALL_MATCHES:
            return _ranked(list({row[0]: row for row in rows}.values()), averages, 201)  # a row once
    count, *totals = _varints(averages) if averages is not None else (1, *[0] * (_SCOPE + 1))
    # Ranked in SQLite from the full-text index alone (bm25() and ``event_tokens``, no content read): the first
    # ``window`` rows by rank, then their content and the scope predicate, in rank order. When fewer than 201 of a
    # full window pass, the window grows; the first 201 that pass are the candidates either way.
    ranked = (f'ranked(r, g) AS MATERIALIZED (SELECT r, {_RANK_KEY} FROM (SELECT r, {_FREQUENCY} AS f, dt FROM'
              ' (SELECT e.rowid AS r, bm25(e.event_search, 0, 0, 0, 1, 0) AS one,'
              ' bm25(e.event_search, 0, 0, 0, 2, 0) AS two, t.text_tokens AS dt, t.scope_tokens AS ns'
              f' FROM idx.event_search e CROSS JOIN idx.event_tokens t WHERE {match} AND t.id = e.rowid LIMIT -1)'
              f' UNION SELECT e.rowid, {frequency}, t.text_tokens{linked}) ORDER BY 2 DESC, 1 LIMIT ?)')
    window = RANK_WINDOW
    while True:
        rows = source.execute(
            f'WITH {pending}, {ranked} SELECT (SELECT count(*) FROM ranked), {candidate} FROM ranked q'
            ' CROSS JOIN idx.event_search e CROSS JOIN episode_scope s'
            ' LEFT JOIN idx.indexed_episodes i ON i.episode_id = s.episode_id'
            f' WHERE e.rowid = q.r AND s.episode_id = e.episode_id AND {scoped} ORDER BY q.g DESC, q.r LIMIT 201',
            [float(totals[_TEXT]) / float(count) or 1.0, float(sum(totals)) / float(count) or 1.0] + match_params
            + linked_params + [window + 1 if window else -1] + scope_params).fetchall()
        if len(rows) == 201 or not window or (rows and rows[0][0] <= window):
            return [row[1:] for row in rows]
        window = window * 8 if rows else 0  # none passed in a full window: rank every match, once


class EpisodicSearchIndex:
    def __init__(self, path, *, episode_store):
        self.path = leaf.as_path(path)
        self.episodes = episode_store

    def _connect(self, *, writable=False, versions=None):
        """An index connection (None when the file is absent and ``writable`` is false; a writable open creates
        it with this code's schema). ``versions``: the schema versions accepted, by default INDEX_VERSION for a
        writer and every readable version for a reader; ``()`` accepts any index file."""
        if versions is None:
            versions = (INDEX_VERSION,) if writable else _READABLE
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise EpisodeUnavailable('episode search index unavailable')
        if not self.path.exists():
            if not writable:
                return None
            # The store check first, so a marked path outside the volume creates nothing (T12b).
            leaf.check_store(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                db = leaf.sqlite_create(self.path)
            except FileExistsError:
                db = None  # created concurrently: opened below like any existing index
            if db is not None:
                try:
                    db.executescript(_SCHEMA)
                except Exception:
                    db.close()
                    self.path.unlink(missing_ok=True)
                    raise
                return db
        mode = 'rw' if writable else 'ro'
        try:
            db = leaf.sqlite_connect(self.path, mode=mode, resolve=True)
            found = db.execute('SELECT version FROM metadata').fetchone()
            if found is None or (versions and found[0] not in versions):
                raise sqlite3.DatabaseError('unsupported search index schema')
            return db
        except sqlite3.Error as error:
            try:
                db.close()
            except (NameError, sqlite3.Error):
                pass
            raise EpisodeUnavailable('episode search index unavailable') from error

    def initialize(self):
        """Create an empty index during operator setup."""
        if self.path.exists() or self.path.is_symlink():
            raise ValueError('episode search index already exists')
        with closing(self._connect(writable=True)):
            pass

    def rebuild(self):
        """Operator-only full reindex: a new file built beside this one under the lease, verified and
        renamed into place (``reindex``). Nothing is deleted in place."""
        return reindex(self.episodes, self, lease_of(self))

    def token(self):
        """This file's coverage token, read only (None when the file or its token is absent), whatever its schema
        version (a reindex carries an older file's token)."""
        db = self._connect(versions=())
        if db is None:
            return None
        with closing(db):
            try:
                row = db.execute('SELECT token FROM coverage_token').fetchone()
            except sqlite3.OperationalError:
                return None
        return None if row is None else row[0]

    def watermark(self):
        """The highest outbox seq this file has applied (0 when it is absent or has applied none)."""
        return self._state()[1]

    def _state(self):
        """(schema version, watermark) of this file in one read: (None, 0) when it is absent."""
        db = self._connect(versions=())
        if db is None:
            return None, 0
        with closing(db):
            return db.execute('SELECT version FROM metadata').fetchone()[0], _watermark(db)

    def digests(self, episode_ids):
        """Indexed payload digests of the given episodes, in one read."""
        db = self._connect()
        if db is None:
            return {}
        with closing(db):
            return dict(db.execute('SELECT episode_id, payload_sha256 FROM indexed_episodes'
                                   ' WHERE episode_id IN (SELECT value FROM json_each(?))',
                                   (json.dumps(list(episode_ids)),)))

    def _commit(self, verified, scopes=None, *, applied=None, seq=None):
        """The postings of ``verified`` and the scope tokens ``scopes`` (``_scopes``) in one index transaction;
        with ``seq`` the watermark moves from ``applied`` to ``seq`` in that same transaction. Returns this file's
        coverage token."""
        with closing(self._connect(writable=True)) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                token = _ensure_token(db)
                if seq is not None:
                    db.execute(_WATERMARK)
                    if _watermark(db) != applied:
                        raise EpisodeUnavailable('the index watermark moved outside the lease')
                    db.execute('INSERT OR REPLACE INTO outbox_watermark (id, seq) VALUES (0, ?)', (seq,))
                _post(db, verified, scopes or {})
                db.commit()
            except Exception:
                db.rollback()
                raise
        return token

    def upsert_episodes(self, episode_ids):
        """Verify and project only the supplied immutable source episodes.

        Capture and search live in separate SQLite files. A failed projection
        leaves the source sealed and can be retried with the same episode IDs.
        Existing unrelated search rows are never rebuilt or removed. No product
        path calls it: ``drain`` applies the outbox (T12b). Its episodes' scope tokens are
        read from the store's projection in the same read; it moves no watermark.
        """
        from kp_agent_tooling._impl.service.session_sources import SessionSources
        identities = list(dict.fromkeys(episode_ids))
        if len(identities) > 2000:
            raise ValueError('index batch exceeds 2000 episodes')
        with closing(self.episodes._connect(readonly=True)) as source:
            verified = _sealed(source, identities)
            scopes = _scopes(source, list(verified), ())
        token = self._commit(verified, scopes)
        # Coverage follows the index commit: a failure here under-reports coverage
        # until the next drain or reindex, never over-reports it.
        SessionSources(self.episodes).mark_coverage(_digests(verified), token)
        return _counts(verified)

    def search(self, session, *, query, limit=10, binding_key=None, scope="desk",
               attribution="any", claims=None, session_id=None, view=None):
        """Search one admitted desk and return spans reopened from source records."""
        return self.search_records(session, query=query, limit=limit, binding_key=binding_key, scope=scope,
                                   attribution=attribution, claims=claims, session_id=session_id, view=view)[0]

    def search_records(self, session, *, query, limit=10, binding_key=None, scope="desk",
                       attribution="any", claims=None, session_id=None, view=None):
        """The search result, and the reopened (record, item) of each result.

        Scope comes from the source store's projection, joined to the FTS match
        in one statement (the index is attached read-only to the source
        connection). Candidates are verified against the indexed text; only the
        returned records are reopened from their sealed rows, in one batched read.
        """
        from kp_agent_tooling._impl.service.session_sources import SessionSources
        with call_scope():
            sources = SessionSources(self.episodes)
            if view is not None:
                if binding_key is not None:
                    raise ValueError("view and binding_key are mutually exclusive")
                scope = "topic"
            plan = sources.resolve_scope(session, scope=scope, binding_key=binding_key, attribution=attribution,
                                         claims=claims, session_id=session_id)
            with closing(sources.connect()) as source:
                if view is not None:
                    from kp_agent_tooling._impl.service.desk_profiles import DeskProfiles
                    plan.restrict(DeskProfiles(self.episodes, session).view_filter(view))
                if not isinstance(query, str) or not query.strip() or len(query.encode('utf-8')) > 256:
                    raise ValueError('bounded nonempty search query required')
                if type(limit) is not int or not 1 <= limit <= 20:
                    raise ValueError('search limit must be 1..20')
                terms = re.findall(r'\w+', query, flags=re.UNICODE)
                if not terms or len(terms) > 16:
                    raise ValueError('search query must contain 1..16 words')
                total, covered, excluded, unverified = sources.counts(source, plan)
                scope_report = plan.report(excluded)
                if view is not None:
                    scope_report['historical_claim_intervals_unverified'] = unverified
                    scope_report['view'] = view
                    scope_report['boundary'] = 'tenant-scoped view; metadata does not grant access'
                binding = scope_report['binding_key']
                # FTS syntax is entirely generated, never taken from caller input.
                phrase = '"' + ' '.join(terms) + '"'
                candidate_limit = min(200, limit * 10)
                if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
                    raise EpisodeUnavailable('episode search index unavailable')
                if not self.path.exists():
                    self._verify_session_filter(sources, source, plan, total)
                    return ({'search_scope': scope_report, 'binding_key': binding, 'index_status': 'index_unavailable',
                             'covered_episodes': 0, 'total_episodes': total, 'results': [],
                             'match_mode': 'literal_phrase', 'absence_verdict': 'not-established',
                             'candidate_limit': candidate_limit,
                             'candidate_limit_reached': False, 'result_limit': limit,
                             'result_limit_reached': False}, [])
                member, member_params = plan.member()
                filters, filter_params = plan.filters()
                kept, kept_params, _, _ = plan.view_terms()
                try:
                    source.execute('ATTACH DATABASE ? AS idx', (leaf.sqlite_uri(self.path, 'ro', resolve=True),))
                    # The index's schema version, whether the store has its outbox (the unapplied rows a narrowed
                    # search consults), and FTS5's averages record (row and token totals), in one statement.
                    found = source.execute(
                        "SELECT m.version, (SELECT count(*) FROM main.sqlite_master WHERE type = 'table'"
                        " AND name = 'index_outbox'), (SELECT block FROM idx.event_search_data WHERE id = 1)"
                        ' FROM idx.metadata m').fetchone()
                    if found is None or found[0] not in _READABLE:
                        raise sqlite3.DatabaseError('unsupported search index schema')
                    try:
                        token = source.execute('SELECT token FROM idx.coverage_token').fetchone()
                    except sqlite3.OperationalError:
                        token = None
                    if found[0] == INDEX_VERSION:
                        # Version 2: the scope's postings narrow the query, the text alone is ranked.
                        candidates = _scoped_candidates(source, plan, phrase, terms, total, found[2], found[1])
                    else:
                        # The cross-file join: FTS match (index) x scope projection (source).
                        # A fixed candidate window keeps the statement work independent of limit.
                        candidates = source.execute(
                            'SELECT e.episode_id, e.event_id, e.text, i.payload_sha256, s.payload_sha256, s.covered'
                            ' FROM idx.event_search e CROSS JOIN episode_scope s'
                            ' LEFT JOIN idx.indexed_episodes i ON i.episode_id = s.episode_id'
                            ' WHERE e.event_search MATCH ? AND s.episode_id = e.episode_id AND '
                            + ' AND '.join([member] + filters + [kept]) + ' ORDER BY e.rank LIMIT ?',
                            [phrase] + member_params + filter_params + kept_params + [201]).fetchall()
                except sqlite3.Error as error:
                    raise EpisodeUnavailable('episode search index unavailable') from error
                if token is None or token[0] != sources.state['coverage_token']:
                    covered = 0  # coverage recorded for another index (or none) is not coverage of this one
                candidates = candidates[:candidate_limit + 1]
                candidate_limit_reached = len(candidates) > candidate_limit
                candidates = candidates[:candidate_limit]
                pattern = re.compile(r'(?<!\w)' + r'\W+'.join(re.escape(term) for term in terms)
                                     + r'(?!\w)', re.IGNORECASE)
                stale = set()
                chosen = []
                for identity, event_id, text, indexed, projected, flag in candidates:
                    if indexed != projected:
                        # A corrupt projection can nominate a record, but cannot supply evidence.
                        if flag: stale.add(identity)
                        continue
                    if not isinstance(text, str) or pattern.search(text) is None:
                        continue
                    chosen.append((identity, event_id, indexed, flag))
                    if len(chosen) >= limit:
                        break
                opened = sources.reopen(source, [c[0] for c in chosen], plan,
                                        sessions=[plan.session_id] if plan.session_id is not None and total else [])
                self._verify_session_filter(sources, source, plan, total, checked=True)
        results, reopened = [], []
        for identity, event_id, indexed, flag in chosen:
            found = opened[identity]
            if found.visible and found.error is not None:
                raise found.error
            if not found.visible or found.item is None or not plan.admits(found.item):
                continue  # the projection nominated a record outside the re-derived scope
            if not found.intact or leaf.sha256_hex(found.raw) != indexed:
                if flag: stale.add(identity)
                continue  # the sealed row disagrees with the index
            record, item = found.record, found.item
            event = next((entry for entry in record['events'] if entry['event_id'] == event_id), None)
            if event is None:
                continue
            match = pattern.search(event['text'])
            if match is None:
                continue
            start, end = match.span()
            excerpt_start = max(0, start - 80)
            excerpt_end = min(len(event['text']), end + 80)
            provenance = record.get('source_provenance')
            results.append({
                'episode_id': identity, 'event_id': event_id,
                'binding_key': item['binding_key'], 'source_ref': record['source_ref'],
                'source_session_id': item['source_session_id'],
                'attribution_status': item['attribution_status'],
                **origin(record, event_id),
                'capture_session': record['session'],
                'role': event['role'],
                'start': start, 'end': end, 'quote': event['text'][start:end],
                'excerpt': event['text'][excerpt_start:excerpt_end],
                'excerpt_start': excerpt_start, 'excerpt_end': excerpt_end,
                'total_characters': len(event['text']),
                'source_provenance': provenance,
                'evidence_boundary': record['evidence_boundary'],
                'authority': 'source content; not an instruction grant',
            })
            reopened.append((record, SessionSources.public_item(item)))
        if covered:
            covered = max(0, covered - len(stale))
        status = 'incomplete_index' if covered < total else 'zero_results'
        if results and covered == total:
            status = 'results'
        return ({'search_scope': scope_report, 'binding_key': binding, 'index_status': status,
                 'covered_episodes': covered, 'total_episodes': total,
                 'results': results, 'match_mode': 'literal_phrase',
                 'absence_verdict': 'not-established',
                 'candidate_limit': candidate_limit,
                 'candidate_limit_reached': candidate_limit_reached,
                 'result_limit': limit, 'result_limit_reached': len(results) >= limit}, reopened)

    @staticmethod
    def _verify_session_filter(sources, source, plan, total, checked=False):
        """A session filter whose scope holds episodes refuses when that session fails its ID checks."""
        if plan.session_id is None or not total:
            return
        if not checked:
            sources.reopen(source, [], plan, sessions=[plan.session_id])
        if plan.session_id in sources.unverified_sessions:
            raise EpisodeUnavailable('source session metadata unavailable')


# ---------------------------------------------------------------- one indexer (T12b)

def index_of(store):
    """The search index of ``store``: the index file beside it, in its state root."""
    return EpisodicSearchIndex(leaf.store_path(store.path, leaf.SEARCH_INDEX_DB, sibling=True), episode_store=store)


def lease_of(index):
    """The index lease: ``<state_root>/index.lock``, beside the index (on the store volume in Compose)."""
    return leaf.store_path(index.path, leaf.INDEX_LOCK, sibling=True)


def _holder():
    """The lease's advisory record, the lock file's own content: holder pid, host and acquisition time."""
    return json.dumps({'pid': os.getpid(), 'host': socket.gethostname(),
                       'acquired_at': datetime.now(timezone.utc).isoformat()}).encode()


def index_lag(store):
    """``index_lag`` of ``store`` in one read-only store connection (``episodic_memory.outbox_lag``)."""
    with closing(store._connect(readonly=True)) as db:
        return outbox_lag(db)


def request_reindex(store, *, once=False):
    """Request a full reindex: one ``reindex`` outbox row, applied by the drainer's build-and-swap.

    Callers: ``index-history``, ``import-native-history`` and, ``once`` (only when no
    reindex row is waiting), ``upgrade-sources`` for an older-writer store. Returns the row's seq, or
    None when ``once`` found one waiting. The indexer infers that a full build is wanted in one case only
    (T12c, amending T12b B3): an index file whose schema version is not INDEX_VERSION (``_drain`` requests its
    own, ``once``). Never for anything else: an empty or absent index is ``upgrade-sources``' or an operator's
    request.
    """
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if once and db.execute("SELECT 1 FROM index_outbox WHERE reason = 'reindex' LIMIT 1").fetchone():
            return None
        return db.execute("INSERT INTO index_outbox (reason) VALUES ('reindex')").lastrowid


def reindex_counts(report):
    """A receipt's ``indexed_episodes``/``indexed_events``: the reindex this call's drain built (0 when its
    drain built none: inside Compose the indexer applies the request)."""
    built = report.get('reindex') if isinstance(report, dict) else None
    return {'indexed_episodes': built['indexed_episodes'] if built else 0,
            'indexed_events': built['indexed_events'] if built else 0}


def drain(store, index, lease, batch=DRAIN_BATCH):
    """Apply the store's outbox to ``index`` from its watermark: the one index writer (T12b B2).

    ``lease`` is the lock file (``lease_of(index)``). It is taken without waiting: when another drainer
    holds it this returns ``status: skipped`` and writes nothing. Under it, each batch of at most
    ``batch`` outbox rows above the watermark is applied in one index transaction that also moves the
    watermark to the batch's last seq; the store's coverage marks follow that commit; then the rows at
    or below the watermark are deleted, at most ``batch`` per store transaction (retention, by this
    drainer only). ``seal`` rows are verified and posted; a ``desks_changed`` row rewrites the scope postings of
    its session's episodes (and of its linked episode), read from the store's projection at apply time, in that
    same index transaction (T12c); a ``reindex`` row builds a new file and swaps it in (``_build_and_swap``), and
    the rows sealed during that build are applied after the swap. An index file of another schema version is
    never written: the drain requests its own reindex and builds the new file first (T12c).
    """
    if type(batch) is not int or not 1 <= batch <= 2000:
        raise ValueError('drain batch must be 1..2000')
    with leaf.nonblocking_lock(lease, record=_holder()) as held:
        if not held:
            return {'status': 'skipped', 'reason': 'lease_held', 'index_lag': index_lag(store)}
        return _drain(store, index, batch)


# The outbox state the drainer reads first: the highest seq the store has issued and the lowest row left.
_OUTBOX_STATE = ("SELECT coalesce((SELECT seq FROM sqlite_sequence WHERE name = 'index_outbox'), 0),"
                 ' (SELECT min(seq) FROM index_outbox)')


def _drain(store, index, batch):
    from kp_agent_tooling._impl.service.session_sources import SessionSources
    version, watermark = index._state()
    # T12c C3: a file of another schema version (version 1 has no postings) is the one case in which the indexer
    # requests its own reindex; the build below replaces it, and the old file answers searches until the rename.
    stale = version is not None and version != INDEX_VERSION
    if stale:
        request_reindex(store, once=True)
    start = watermark
    report = {'status': 'idle', 'applied_rows': 0, 'batches': 0, 'indexed_episodes': 0, 'indexed_events': 0,
              'reindex': None}
    while True:
        # The writable open ensures the outbox on a store no writer has opened since T12b (B1).
        with closing(store._connect()) as db:
            issued, low = db.execute(_OUTBOX_STATE).fetchone()
            if issued < watermark:
                # The index has applied rows this store never issued (a restored store, or another
                # store's index): only a requested reindex resolves it, and nothing here is retained.
                low = None
                rows = db.execute("SELECT seq, reason, episode_id, session_id FROM index_outbox"
                                  " WHERE reason = 'reindex' ORDER BY seq LIMIT 1").fetchall()
                if not rows:
                    raise OutboxAhead(f'the index has applied outbox seq {watermark} but this store has issued '
                                      f'{issued}: it is not this store\'s index; request a reindex '
                                      '(kp-agent-desk ... index-history)')
            else:
                rows = db.execute('SELECT seq, reason, episode_id, session_id FROM index_outbox WHERE seq > ?'
                                  ' ORDER BY seq LIMIT ?', (watermark, batch)).fetchall()
        if not report['batches'] and low is not None and low <= watermark:
            _retain(store, watermark, batch)  # rows applied before an interrupted retention
        if not rows:
            break
        if stale or any(reason == 'reindex' for _, reason, _, _ in rows):
            stale = False
            report['reindex'] = _build_and_swap(store, index, batch)
            watermark = report['reindex']['watermark']
        else:
            ids = list(dict.fromkeys(identity for _, reason, identity, _ in rows if reason == 'seal'))
            # desks_changed rows (a claim: its session; a link: its session and episode): the scope tokens of the
            # session's episodes are rewritten from the store's projection as it is now (T12c C2), in the index
            # transaction that moves the watermark past the rows. The rows' ``detail`` is not read.
            changed = [(identity, session) for _, reason, identity, session in rows if reason == 'desks_changed']
            with closing(store._connect(readonly=True)) as source:
                verified = _sealed(source, ids) if ids else {}
                scopes = _scopes(source, list(verified) + [identity for identity, _ in changed if identity],
                                 [session for _, session in changed if session])
            token = index._commit(verified, scopes, applied=watermark, seq=rows[-1][0])
            if verified:
                # Coverage follows the index commit: under-reported until it lands, never over-reported. A
                # failure from here on is held in the store (coverage_marks_lost) until a later mark lands.
                try:
                    SessionSources(store).mark_coverage(_digests(verified), token)
                except BaseException as error:
                    _marks_lost(store, error, rows[-1][0])
                    raise
            for key, value in _counts(verified).items():
                report[key] += value
            watermark = rows[-1][0]
        report['applied_rows'] += len(rows)
        report['batches'] += 1
        _retain(store, watermark, batch)
    report.update(status='drained' if report['applied_rows'] else 'idle', watermark=watermark,
                  advanced=watermark != start, index_lag=index_lag(store))
    return report


def _retain(store, watermark, batch):
    """Retention (S1): delete the applied rows, those at or below the watermark, at most ``batch`` per store
    transaction. Only the drainer that applied them calls it, under the lease."""
    with closing(store._connect()) as db:
        while True:
            db.execute('BEGIN IMMEDIATE')
            deleted = db.execute('DELETE FROM index_outbox WHERE seq IN (SELECT seq FROM index_outbox'
                                 ' WHERE seq <= ? ORDER BY seq LIMIT ?)', (watermark, batch)).rowcount
            db.commit()
            if deleted < batch:
                return


def _journal(path):
    """The rollback journal SQLite keeps beside ``path`` during a write (journal_mode DELETE)."""
    return path.with_name(path.name + '-journal')


def _discard(path):
    for name in (path, _journal(path)):
        name.unlink(missing_ok=True)


def _no_hot_journal(index_path, built):
    """The swap's "no hot journal" check (T12b B3): at the rename, neither the old index nor the built file
    has a rollback journal beside it.

    Renaming a new file in beside a hot journal of the old one would let SQLite play the old file's pages
    into the new file at its next open. A journal a crashed writer left beside the old file is rolled back
    first, by reading the old file (SQLite rolls a hot journal back when it reads); a journal still there
    belongs to a live writer outside the lease, and the swap is refused (IndexBusy), keeping the old file.
    """
    journal = _journal(index_path)
    if os.path.lexists(journal) and index_path.is_file():
        with closing(leaf.sqlite_connect(index_path, mode='rw', resolve=True)) as db:
            db.execute('SELECT count(*) FROM metadata').fetchone()
    for path in (journal, _journal(built)):
        if os.path.lexists(path):
            raise IndexBusy(f'{path.name} exists: a writer outside the index lease holds the index; '
                            'the swap is refused and the old index kept')


def _unheld(store, built):
    """Episodes the store's coverage marks claim (``covered``, with their sealed digest) that ``built``
    does not hold with that digest: one statement, the built file attached read-only."""
    with closing(store._connect(readonly=True)) as db:
        db.execute('ATTACH DATABASE ? AS built', (leaf.sqlite_uri(built, 'ro', resolve=True),))
        return [row[0] for row in db.execute(
            'SELECT s.episode_id FROM episode_scope s LEFT JOIN built.indexed_episodes i'
            ' ON i.episode_id = s.episode_id WHERE s.covered = 1 AND i.payload_sha256 IS NOT s.payload_sha256'
            ' ORDER BY s.episode_id LIMIT 21')]


def _build_and_swap(store, index, batch):
    """Build-and-swap (T12b B3), under the caller's lease.

    A new file is built beside the index from the store's sealed rows, read in batches of ``batch``,
    each its own read transaction (no store read is held across the build), verified as a read verifies
    them, and renamed into place by ``leaf.rename_into_place`` after ``_no_hot_journal``. Readers keep
    attaching ``?mode=ro``: they see the old file or the new one. The new file's watermark is the outbox
    seq the store had issued when the build started, so rows sealed during the build are drained after
    the swap.

    Coverage token, option (i): the new file carries the old file's token only when the build holds every
    (episode, digest) the store's coverage marks claim for that token; otherwise the reindex is refused
    (ReindexRefused, naming the episodes) and the old file is kept. When the marks describe no token of
    the old file, the new file gets a new token and the marks are reset to it after the swap.

    The new file has this code's schema (INDEX_VERSION), whatever the old one's: each batch's scope tokens are
    read from the store's projection in the batch's own read (``_scopes``). A reassignment committed after the
    build started has its outbox row above the new watermark, so the drain after the swap applies it.
    """
    from kp_agent_tooling._impl.service.session_sources import SessionSources, _projecting
    with closing(store._connect()) as db:
        start = db.execute(_OUTBOX_STATE).fetchone()[0]
        claimed = (db.execute("SELECT value FROM scope_state WHERE key = 'coverage_token'").fetchone()
                   if _projecting(db) else None)
    old = index.token()
    carried = old is not None and claimed is not None and claimed[0] == old
    built = leaf.store_path(index.path, leaf.SEARCH_INDEX_BUILD_DB, sibling=True)
    _discard(built)  # a build interrupted earlier
    digests, counts = {}, {'indexed_episodes': 0, 'indexed_events': 0}
    try:
        with closing(leaf.sqlite_create(built)) as db:
            db.executescript(_SCHEMA)
            for kind, table, owner in ((0, 'episodes', 'binding, NULL'), (1, 'source_episodes', 'tenant, session_id')):
                last = 0
                while True:
                    with closing(store._connect(readonly=True)) as source:
                        rows = [] if kind and not _has_sources(source) else source.execute(
                            f'SELECT rowid, id, {owner}, payload FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?',
                            (last, batch)).fetchall()
                        # The batch's scope postings, from the projection in the same read (T12c).
                        scopes = _scopes(source, [row[1] for row in rows], ()) if rows else {}
                    if not rows:
                        break
                    verified = _verified((kind, *row[1:]) for row in rows)
                    db.execute('BEGIN')
                    _post(db, verified, scopes)
                    db.commit()
                    digests.update(_digests(verified))
                    for key, value in _counts(verified).items():
                        counts[key] += value
                    last = rows[-1][0]
            if carried:
                unheld = _unheld(store, built)
                if unheld:
                    raise ReindexRefused(
                        f'reindex refused, the old index is kept: the store\'s coverage marks claim '
                        f'{len(unheld) if len(unheld) <= 20 else "more than 20"} episodes the new build does not '
                        f'hold with their sealed digest ({", ".join(unheld[:20])}); run the store integrity check '
                        '(PRAGMA quick_check, docs/DOCKER.md), then the recovery steps there')
            token = old if carried else secrets.token_hex(16)
            db.execute('BEGIN')
            db.execute("INSERT INTO event_search(event_search) VALUES ('optimize')")
            db.execute(_TOKEN)
            db.execute('INSERT INTO coverage_token (token) VALUES (?)', (token,))
            db.execute(_WATERMARK)
            db.execute('INSERT INTO outbox_watermark (id, seq) VALUES (0, ?)', (start,))
            db.commit()
        _no_hot_journal(index.path, built)
        leaf.rename_into_place(built, index.path)
    except BaseException:
        _discard(built)
        raise
    # After the swap: under-reported until this lands, never over-reported. One mark_coverage call, which
    # writes in store transactions of at most MARK_BATCH episodes (the bound lives there alone), so no
    # sealer waits on one long write lock. With the token carried, the marks already claim what they
    # claimed (verified held above): only the episodes not yet covered are marked. With a new token, its
    # reset clears the old marks in transactions of at most MARK_BATCH rows while the store still names
    # the old token (readers report covered 0 on a token mismatch), then names the new token in one
    # transaction that changes no episode_scope row (SessionSources._reset_coverage holds the argument).
    sources = SessionSources(store)
    if carried:
        with closing(store._connect(readonly=True)) as db:
            uncovered = {row[0] for row in db.execute('SELECT episode_id FROM episode_scope WHERE covered = 0')}
        items = [(identity, digest) for identity, digest in digests.items() if identity in uncovered]
    else:
        items = list(digests.items())
    try:
        sources.mark_coverage(dict(items), token, reset=not carried)
    except BaseException as error:  # after the swap: the new file is in place, its marks are not
        _marks_lost(store, error, start)
        raise
    # Every row the new file holds is now marked (new token: all; carried: the uncovered, the rest verified
    # held above): a full re-mark, which alone clears a lost-marks record.
    sources.clear_marks_lost()
    return {**counts, 'watermark': start, 'token': 'carried' if carried else 'new'}


def reindex(store, index, lease, batch=DRAIN_BATCH):
    """Build-and-swap now, under the lease (never waited for: IndexBusy when it is held), then retention.

    W11's isolated copy and ``EpisodicSearchIndex.rebuild`` call it; a live caller requests one with
    ``request_reindex`` and the drainer builds it.
    """
    with leaf.nonblocking_lock(lease, record=_holder()) as held:
        if not held:
            raise IndexBusy('the index lease is held by another drainer; retry the reindex')
        built = _build_and_swap(store, index, batch)
        _retain(store, built['watermark'], batch)
    return {'indexed_episodes': built['indexed_episodes'], 'indexed_events': built['indexed_events']}


def drain_after_seal(store, index=None):
    """A host install's drain after a seal: once, never waiting for the lease (a held lease is a skip).

    Inside Compose (``AGENT_MEMORY_VOLUME`` set) sealers never drain and never open the index: the
    ``indexer`` role is the only drainer, so this returns None. A failure is returned, never raised:
    the seal is committed and its outbox row waits for the next drain.
    """
    if leaf.docker_runtime():
        return None
    index = index_of(store) if index is None else index
    try:
        return drain(store, index, lease_of(index), DRAIN_BATCH)
    except Exception as error:  # the outbox keeps the work for the next drain
        return {'status': 'error', 'category': _category(error), 'message': str(error)[:MESSAGE_CHARS]}


# ---------------------------------------------------------------- the indexer role

def _category(error):
    declared = getattr(error, 'category', None)
    return declared if isinstance(declared, str) and declared else type(error).__name__


def backoff(interval, attempt):
    """Seconds to wait after the ``attempt``-th consecutive failed pass (1-based), as T12a's loops do."""
    return min(interval * 2 ** (attempt - 1), interval * BACKOFF_CAP_INTERVALS)


def stores_under(root):
    """Every episode store under ``root`` (the store volume): each state root holding an episodes file.

    Launch directories hold no store and are not walked; symlinked directories are not followed. At most
    WALK_BOUND directories are walked per pass, in sorted order; a store beyond the bound is not found."""
    found, walked = [], 0
    for current, dirnames, filenames in os.walk(root):
        walked += 1
        if walked > WALK_BOUND:
            break
        dirnames[:] = sorted(name for name in dirnames
                             if name != 'launches' and not os.path.islink(os.path.join(current, name)))
        if leaf.EPISODES_DB in filenames:
            found.append(EpisodeStore(leaf.store_file(current, leaf.EPISODES_DB), session_ledger=None, registry=None))
    return found


def _record_marks_lost(store, value):
    """Write ``store``'s coverage_marks_lost row in its own small transaction; returns the write's error, or None."""
    from kp_agent_tooling._impl.service.session_sources import MARKS_LOST_KEY
    try:
        with closing(store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO scope_state (key, value) VALUES (?, ?)', (MARKS_LOST_KEY, value))
    except Exception as error:
        return error
    return None


def _marks_lost(store, error, seq):
    """A mark phase failed after its index commit (T12b meet ruling (c)): hold it in the store.

    One scope_state row, ``coverage_marks_lost``: the category and bounded message of ``error``, ``seq`` (the
    watermark the batch reached) and the time. It is held in the store, not the process, so a restarted role
    reads it again; only a full re-mark deletes it (``SessionSources.clear_marks_lost``: a reindex, or
    ``upgrade-sources``), never a drain's incremental marks. The caller re-raises ``error``:
    a failed write of the row never replaces it, it is attached to it (``coverage_record``) for the drain
    line and written again on the next pass."""
    value = json.dumps({'category': _category(error), 'message': str(error)[:MESSAGE_CHARS], 'seq': seq,
                        'at': datetime.now(timezone.utc).isoformat()}, sort_keys=True)
    failed = _record_marks_lost(store, value)
    key = str(store.path)
    if failed is None:
        _LOST_UNWRITTEN.pop(key, None)
        return
    _LOST_UNWRITTEN[key] = value
    try:
        error.coverage_record = {'status': 'unwritten', 'category': _category(failed),
                                 'message': str(failed)[:MESSAGE_CHARS]}
    except Exception:  # an exception type that takes no attributes: the pending record still stands
        pass


def coverage_lost(store):
    """``store``'s coverage_marks_lost record (a dict), or None, in one read (None on a store without a
    projection, which has no scope_state)."""
    from kp_agent_tooling._impl.service.session_sources import MARKS_LOST_KEY
    with closing(store._connect(readonly=True)) as db:
        try:
            row = db.execute('SELECT value FROM scope_state WHERE key = ?', (MARKS_LOST_KEY,)).fetchone()
        except sqlite3.OperationalError:
            return None
    return json.loads(row[0]) if row else None


def _lost_summary(held):
    return {key: held.get(key) for key in ('category', 'seq', 'at')}


def drain_line(store, batch, stalls, lost=None):
    """One drain of ``store`` as the indexer's JSON line, with this store's health.

    ``stalls`` counts, per store, consecutive non-skipped drains in which the watermark did not advance
    while outbox rows exist; a lease skip leaves it as it is (a skip is a clean pass). ``advanced`` is the
    truth on every path: on an error it compares the index watermark after the failure with the one read
    before the pass (null when either read failed). The store's ``coverage_marks_lost`` row is read every
    pass: while it is present the line carries ``coverage: lost`` (its category and seq) and the store is
    unhealthy, whether or not rows are waiting; ``lost`` (the role's map) is kept to it."""
    index = index_of(store)
    key = str(store.path)
    if key in _LOST_UNWRITTEN and _record_marks_lost(store, _LOST_UNWRITTEN[key]) is None:
        _LOST_UNWRITTEN.pop(key, None)  # a record whose write failed on an earlier pass
    try:
        before = index.watermark()
    except Exception:
        before = None
    try:
        report = drain(store, index, lease_of(index), batch)
    except Exception as error:
        try:
            after = index.watermark()
        except Exception:
            after = None
        report = {'status': 'error', 'category': _category(error), 'exception': type(error).__name__,
                  'message': str(error)[:MESSAGE_CHARS],
                  'advanced': None if before is None or after is None else after != before}
        if getattr(error, 'coverage_record', None):
            report['coverage_record'] = error.coverage_record
        try:
            report['index_lag'] = index_lag(store)
        except Exception:  # an unreadable store: the rows are unknown, so they count as waiting
            report['index_lag'] = None
    if report['status'] != 'skipped':
        waiting = report.get('index_lag') != 0
        stalls[key] = 0 if report.get('advanced') or not waiting else stalls.get(key, 0) + 1
    count = stalls.get(key, 0)
    line = {'schema_version': INDEXER_SCHEMA, 'store': key, **report, 'stalled_drains': count}
    try:
        held = coverage_lost(store)
    except Exception:  # unreadable now: the role's map keeps what the last read said
        held = lost.get(key) if lost is not None else None
        line['coverage'] = 'unreadable'
    if held is None and key in _LOST_UNWRITTEN:
        held = dict(json.loads(_LOST_UNWRITTEN[key]), recorded=False)
    if held:
        line['coverage'] = 'lost'
        line['coverage_lost'] = _lost_summary(held)
        if lost is not None:
            lost[key] = line['coverage_lost']
    elif lost is not None and line.get('coverage') != 'unreadable':
        lost.pop(key, None)
    line['health'] = 'unhealthy' if count >= STALL_DRAINS or held else 'healthy'
    return line


def _process_start(pid):
    """The start time (clock ticks since boot) of process ``pid`` from /proc, or None where /proc has none."""
    try:
        stat_line = Path(f'/proc/{int(pid)}/stat').read_text()
        return int(stat_line.rsplit(')', 1)[1].split()[19])
    except (OSError, ValueError, TypeError, IndexError):
        return None


def _health_record(stalls, lost=None):
    stalled = {store: count for store, count in sorted(stalls.items()) if count >= STALL_DRAINS}
    lost = dict(sorted((lost or {}).items()))
    return {'schema_version': INDEXER_SCHEMA, 'status': 'unhealthy' if stalled or lost else 'healthy',
            'stalled': stalled, 'coverage_lost': lost,
            'writer': {'pid': os.getpid(), 'started': _process_start(os.getpid())},
            'rule': f'unhealthy while a store\'s watermark has not advanced across {STALL_DRAINS} consecutive '
                    'non-skipped drains with outbox rows waiting; a store whose outbox cannot be read '
                    '(index_lag null) counts as rows waiting; unhealthy while a store holds a '
                    'coverage_marks_lost record (a mark phase failed after its index commit) until a later mark '
                    'clears it; an absent record, or one whose writer is not running, reads unhealthy'}


def _write_health(path, record, written, passes):
    """Write the health record when it changed; returns what the file now holds. A failed write is reported
    as its own JSON line (category and message, bounded like a drain error) and never ends the loop: the
    record is kept unwritten, so the next pass writes it again (T12a's loop rules)."""
    if record == written:
        return written
    try:
        leaf.replace_file(path, json.dumps(record, sort_keys=True).encode() + b'\n', cleanup='on-error',
                          sibling_tag='tmp')
    except Exception as error:  # a signal (KeyboardInterrupt, SystemExit) still ends the loop
        print(json.dumps({'schema_version': INDEXER_SCHEMA, 'status': 'error', 'scope': 'health_write',
                          'category': _category(error), 'exception': type(error).__name__,
                          'message': str(error)[:MESSAGE_CHARS], 'pass': passes, 'health': record['status']},
                         sort_keys=True), flush=True)
        return written
    return record


def watch(root, interval, *, max_passes=None, batch=DRAIN_BATCH, clock=time):
    """The ``indexer`` role: drain every store under ``root`` every ``interval`` seconds (T12a's loop rules).

    Each drain prints one JSON line (``drain_line``: ``index_lag``, the watermark, the store's health). A
    lease skip is a clean pass and never advances the backoff; a pass with a failed drain waits with
    bounded exponential backoff, and a clean pass resets it. The role's health (``health``) is written to
    ``<root>/indexer-health.json`` before the first pass and whenever it changes; a failed write is reported
    and retried, never fatal (``_write_health``). Only a signal or ``max_passes`` ends the loop; returns 0,
    or 1 when the last pass failed.
    """
    health_path = leaf.store_path(leaf.mark_store(root), leaf.INDEXER_HEALTH)
    stalls, lost, passes, attempt = {}, {}, 0, 0
    try:  # the stores' lost-marks records, before the first health write: a restart starts from them
        for store in stores_under(root):
            held = coverage_lost(store)
            if held:
                lost[str(store.path)] = _lost_summary(held)
    except Exception:  # read again by every pass
        pass
    written = _write_health(health_path, _health_record(stalls, lost), None, passes)
    while True:
        passes += 1
        last = max_passes is not None and passes >= max_passes
        failed = False
        try:
            stores = stores_under(root)
        except Exception as error:  # a signal (KeyboardInterrupt, SystemExit) still ends the loop
            stores, failed = [], True
            print(json.dumps({'schema_version': INDEXER_SCHEMA, 'status': 'error', 'category': _category(error),
                              'message': str(error)[:MESSAGE_CHARS], 'root': str(root)}, sort_keys=True), flush=True)
        for store in stores:
            line = drain_line(store, batch, stalls, lost)
            failed = failed or line['status'] == 'error'
            print(json.dumps(line, sort_keys=True), flush=True)
        written = _write_health(health_path, _health_record(stalls, lost), written, passes)
        attempt = attempt + 1 if failed else 0
        if last:
            return 1 if failed else 0
        clock.sleep(backoff(interval, attempt) if attempt else interval)


def health(root):
    """(exit code, record) of the indexer role's health, from ``<root>/indexer-health.json``: 0 when the
    record says healthy, 1 otherwise. Fail-safe: an absent or unreadable record, and a stale one (its writer
    is not a running process, by pid and start time, where /proc shows processes), read unhealthy."""
    path = leaf.store_path(leaf.mark_store(root), leaf.INDEXER_HEALTH)
    try:
        record = json.loads(path.read_bytes())
    except FileNotFoundError:
        return 1, {'schema_version': INDEXER_SCHEMA, 'status': 'absent',
                   'detail': 'no health record: the indexer has not written one'}
    except (OSError, ValueError) as error:
        return 1, {'schema_version': INDEXER_SCHEMA, 'status': 'unreadable', 'category': _category(error)}
    writer = record.get('writer') if isinstance(record, dict) else None
    if not isinstance(record, dict) or not isinstance(writer, dict):
        return 1, {'schema_version': INDEXER_SCHEMA, 'status': 'unreadable', 'category': 'record_shape'}
    if Path('/proc/self/stat').exists() and _process_start(writer.get('pid')) != writer.get('started'):
        return 1, dict(record, status='stale', detail='the process that wrote this record is not running')
    return (0 if record.get('status') == 'healthy' else 1), record
