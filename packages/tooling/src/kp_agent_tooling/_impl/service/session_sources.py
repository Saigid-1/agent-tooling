"""Canonical source sessions and append-only claims, independent of admission.

Only trusted operator/capture adapters mutate this catalog. Claims describe
history; they never grant memory permissions. Legacy episode bytes stay sealed.

Reads use a scope projection kept in this store (``episode_scope`` and its
companions). Seal, claim and link write it in their own transaction, so a read
never walks the store. The projection only nominates records: every record a
read returns or cites is reopened from its sealed row, and its scope re-derived
from the session's content-addressed claims, in one batched read per call.
Registry membership is never projected; it comes from the call's registry read.
"""
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.episodic_memory import (
    EpisodeConflict, EpisodeProjectionIncomplete, EpisodeUnavailable, _bytes, _id, _text, outbox_lag, outbox_schema)

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS source_sessions (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS session_claims (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, payload BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS claims_session ON session_claims(session_id);
CREATE TABLE IF NOT EXISTS session_episodes (
 episode_id TEXT PRIMARY KEY, session_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS episodes_session ON session_episodes(session_id);
CREATE TABLE IF NOT EXISTS source_episodes (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, session_id TEXT NOT NULL,
 source_ref TEXT NOT NULL, payload BLOB NOT NULL, UNIQUE(session_id, source_ref));
CREATE TABLE IF NOT EXISTS source_projects (
 session_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, repo_key TEXT NOT NULL,
 evidence BLOB NOT NULL, digest TEXT NOT NULL);
''' + outbox_schema('session_claims', 'session_episodes', 'source_episodes')  # their outbox triggers (T12b B1)

PROJECTION_VERSION = 'agent-tooling.scope-projection.v1'
# Tables whose rows the projection reflects. A trigger counts every inserted
# row (``written``); the writer or backfill that projects a row counts it
# (``projected``). Any difference is a row an older writer sealed without a
# projection, detected in O(1).
_MARKED = ('episodes', 'source_episodes', 'session_claims', 'session_episodes')
# The catalog tables (and their indexes) writers need; ``ensure_catalog`` creates them.
_CATALOG = ('source_sessions', 'session_claims', 'claims_session', 'session_episodes', 'episodes_session',
            'source_episodes', 'source_projects')
# Every projection table declares its primary-key columns before any other column.
# On the runtime image's SQLite (3.40.1), PRAGMA quick_check of a WITHOUT ROWID
# table with a NOT NULL column declared between its key columns reports NULL
# values that are not there (T10h D1). Desk-scope membership: the at-episode
# owner desks of a linked episode (with its session tenant), or the storage
# binding of an unlinked one (tenant '').
_DESKS = ('(desk TEXT NOT NULL, kind INTEGER NOT NULL, seq INTEGER NOT NULL, episode_id TEXT NOT NULL,'
          ' tenant TEXT NOT NULL, PRIMARY KEY (desk, kind, seq, episode_id)) WITHOUT ROWID')
_DESK_COLUMNS = ('desk', 'kind', 'seq', 'episode_id', 'tenant')
# The T10 declaration, ``tenant`` at cid 1 between key columns; ``upgrade`` rebuilds it.
_OLD_DESK_COLUMNS = ['desk', 'tenant', 'kind', 'seq', 'episode_id']
_PROJECTION_DDL = (
    'CREATE TABLE IF NOT EXISTS scope_marks (tbl TEXT PRIMARY KEY, written INTEGER NOT NULL,'
    ' projected INTEGER NOT NULL) WITHOUT ROWID',
    'CREATE TABLE IF NOT EXISTS scope_state (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID',
    # One row per sealed episode: kind 0 is the episodes table, 1 source_episodes;
    # seq is the row's rowid at seal (the base read order). tenant and
    # source_session_id describe the linked session; an unlinked legacy episode
    # has neither and is scoped by its storage binding against the live registry.
    'CREATE TABLE IF NOT EXISTS episode_scope (episode_id TEXT PRIMARY KEY, kind INTEGER NOT NULL,'
    ' seq INTEGER NOT NULL, storage_binding TEXT, tenant TEXT, source_session_id TEXT,'
    ' payload_sha256 TEXT NOT NULL, coord_path TEXT, coord_start TEXT, status TEXT NOT NULL,'
    ' binding_key TEXT, owners_unbounded INTEGER NOT NULL, covered INTEGER NOT NULL) WITHOUT ROWID',
    'CREATE INDEX IF NOT EXISTS episode_scope_tenant ON episode_scope(tenant, kind, seq)',
    'CREATE INDEX IF NOT EXISTS episode_scope_session ON episode_scope(source_session_id)',
    'CREATE INDEX IF NOT EXISTS episode_scope_unlinked ON episode_scope(storage_binding)'
    ' WHERE tenant IS NULL',
    'CREATE TABLE IF NOT EXISTS episode_desks ' + _DESKS,
    'CREATE INDEX IF NOT EXISTS episode_desks_episode ON episode_desks(episode_id)',
    # The at-episode active claims, for claim filters by indexed lookup.
    'CREATE TABLE IF NOT EXISTS episode_claims (episode_id TEXT NOT NULL, predicate TEXT NOT NULL,'
    ' object_kind TEXT NOT NULL, object_id TEXT NOT NULL,'
    ' PRIMARY KEY (episode_id, predicate, object_kind, object_id)) WITHOUT ROWID',
    # Maintained aggregates: ('owner', desk, tenant), ('storage', binding, ''),
    # ('tenant', tenant, ''), ('unresolved', tenant, '').
    'CREATE TABLE IF NOT EXISTS scope_counts (scope TEXT NOT NULL, key TEXT NOT NULL,'
    ' tenant TEXT NOT NULL, episodes INTEGER NOT NULL, covered INTEGER NOT NULL,'
    ' PRIMARY KEY (scope, key, tenant)) WITHOUT ROWID',
    'CREATE TABLE IF NOT EXISTS projected_claims (claim_id TEXT PRIMARY KEY) WITHOUT ROWID',
    'CREATE TABLE IF NOT EXISTS projected_links (episode_id TEXT PRIMARY KEY) WITHOUT ROWID',
    'CREATE INDEX IF NOT EXISTS source_projects_tenant ON source_projects(tenant)',
    'CREATE INDEX IF NOT EXISTS capsules_binding ON capsules(binding)',
)
_TRIGGERS = tuple(
    f'CREATE TRIGGER IF NOT EXISTS scope_written_{table} AFTER INSERT ON {table} '
    f"BEGIN UPDATE scope_marks SET written = written + 1 WHERE tbl = '{table}'; END"
    for table in _MARKED)
_GUIDANCE = ('The memory store has rows without a scope projection. Ask the operator to run '
             '`kp-agent-desk --config <session.json> upgrade-sources`; reads resume when it reports complete.')

_EACH = 'SELECT value FROM json_each(?)'
# Coverage resets one mark_coverage call may make on a token mismatch before it gives up (T12b meet ruling):
# a stable store needs at most one; a token switch that never lands must surface, not spin.
COVERAGE_RESETS = 3


# The store's sticky signal that a mark phase failed after its index commit (T12b meet ruling (c)): one
# scope_state row, written by the drainer (episodic_search._marks_lost). Only a full re-mark of the store
# deletes it (SessionSources.clear_marks_lost): a reindex's post-swap marks, upgrade-sources' re-mark and,
# later, T12c's repair pass. A drain's incremental marks never do.
MARKS_LOST_KEY = 'coverage_marks_lost'


class CoverageTokenUnstable(EpisodeUnavailable):
    """The store still names another coverage token after COVERAGE_RESETS resets to the index's token."""
    category = 'coverage_token_unstable'
_ROW = "json_extract(value, '$[{}]')"


def _row_columns(count):
    return ', '.join(_ROW.format(i) for i in range(count))


_SCOPE_COLUMNS = ('episode_id, kind, seq, storage_binding, tenant, source_session_id, payload_sha256,'
                  ' coord_path, coord_start, status, binding_key, owners_unbounded, covered')
_REOPEN = f'''
WITH caps(id) AS ({_EACH}),
 want(id) AS ({_EACH}
  UNION SELECT j.value FROM capsules k, json_each(CASE WHEN json_valid(CAST(k.payload AS TEXT))
   THEN CAST(k.payload AS TEXT) ELSE '{{}}' END, '$.handoff.source_episode_ids') j
   WHERE k.id IN (SELECT id FROM caps) AND k.binding = ?),
 linked(episode_id, session_id) AS (
  SELECT episode_id, session_id FROM session_episodes WHERE episode_id IN (SELECT id FROM want)),
 sealed(id) AS (
  SELECT session_id FROM linked
  UNION SELECT session_id FROM source_episodes WHERE id IN (SELECT id FROM want)
  UNION {_EACH})
SELECT 0, e.id, e.binding, e.payload, (SELECT l.session_id FROM linked l WHERE l.episode_id = e.id)
 FROM episodes e WHERE e.id IN (SELECT id FROM want)
UNION ALL
SELECT 1, x.id, x.tenant, x.payload, x.session_id
 FROM source_episodes x WHERE x.id IN (SELECT id FROM want)
UNION ALL
SELECT 2, s.id, s.tenant, s.payload, NULL
 FROM source_sessions s WHERE s.id IN (SELECT id FROM sealed)
UNION ALL
SELECT 3, c.id, c.session_id, c.payload, c.rowid
 FROM session_claims c WHERE c.session_id IN (SELECT id FROM sealed)
UNION ALL
SELECT 4, k.id, k.binding, k.payload, NULL
 FROM capsules k WHERE k.id IN (SELECT id FROM caps) AND k.binding = ?
'''


def _time(value):
    if not isinstance(value, str):
        raise ValueError('timezone-aware ISO timestamp required')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone-aware ISO timestamp required')
    return parsed


def _status(desks):
    return 'unresolved' if not desks else 'resolved' if len(desks) == 1 else 'conflicting'


def _describe(session_id, payload, claims):
    superseded = {r['supersedes'] for r in claims if r['supersedes']}
    active = [r for r in claims if r['claim_id'] not in superseded and not r['retracted']]
    # Active means currently asserted historical claims, not currently admitted seats.
    desks = sorted({r['object']['id'] for r in active
                    if r['predicate'] == 'session.owner' and r['object']['kind'] == 'desk'})
    return dict(payload, session_id=session_id, claims=claims, active_claims=active,
                desk_bindings=desks, attribution_status=_status(desks),
                authority='historical claims; no admission or authentication grant')


def _verified_session(identity, row_tenant, raw, claim_rows, tenant=None):
    """Session metadata from sealed rows, with the checks of ``metadata``."""
    if row_tenant is None or (tenant is not None and row_tenant != tenant):
        raise EpisodeUnavailable('source session unavailable to this tenant')
    payload = json.loads(raw)
    if _id('source-session', payload) != identity or payload['tenant_id'] != row_tenant:
        raise EpisodeUnavailable('source session integrity mismatch')
    claims = []
    for claim_id, claim_raw in claim_rows:
        p = json.loads(claim_raw)
        if _id('session-claim', p) != claim_id or p['session_id'] != identity:
            raise EpisodeUnavailable('session claim integrity mismatch')
        claims.append(dict(p, claim_id=claim_id))
    return _describe(identity, payload, claims)


def _coordinates(payload):
    """Source coordinates as the base read derived them.

    That read took ``json_extract(payload, '$.source_provenance.source_coordinates')``
    and ``json.loads`` of it when truthy, so a JSON object or array comes back
    as itself and a non-empty scalar is parsed again (and may fail as it did).
    """
    provenance = payload.get('source_provenance') if isinstance(payload, dict) else None
    if not isinstance(provenance, dict):
        return None
    value = provenance.get('source_coordinates')
    if value is None or isinstance(value, (dict, list)):
        return value
    raw = int(value) if isinstance(value, bool) else value
    return json.loads(raw) if raw else None


def _coordinate_facts(coordinates):
    """The type-guarded facts a ranged claim reads: a text path and an int start."""
    if not isinstance(coordinates, dict):
        return None, None
    path, start = coordinates.get('path'), coordinates.get('start')
    return (path if isinstance(path, str) else None), (str(start) if type(start) is int else None)


def _applies(claim, native_id, path, start):
    """Whether a claim covers an episode, exactly as ``SessionSources._at_episode``."""
    boundary = claim.get('source_range')
    if boundary is None:
        return True
    return (path is not None and boundary['native_id'] == native_id and
            path == boundary['source_file'] and start is not None and
            start >= boundary['start_offset'])


def _derive(meta, path, start):
    active = [c for c in meta['active_claims'] if _applies(c, meta['native_id'], path, start)]
    desks = sorted({c['object']['id'] for c in active
                    if c['predicate'] == 'session.owner' and c['object']['kind'] == 'desk'})
    owners = [c for c in active if c['predicate'] == 'session.owner']
    return {'desks': desks, 'status': _status(desks),
            'binding_key': desks[0] if len(desks) == 1 else None,
            'owners_unbounded': int(bool(owners) and all(c['valid_from'] is None and c['valid_until'] is None
                                                         for c in owners)),
            'pairs': sorted({(c['predicate'], c['object']['kind'], c['object']['id']) for c in active})}


def _contributions(row, desks):
    """The aggregate keys one projected episode counts toward."""
    if row['tenant'] is None:
        return [('storage', row['storage_binding'], '')]
    keys = [('tenant', row['tenant'], '')] + [('owner', desk, row['tenant']) for desk in desks]
    if row['status'] == 'unresolved':
        keys.append(('unresolved', row['tenant'], ''))
    return keys


def _cited(raw):
    """Episode IDs a capsule payload cites (empty when it is not readable)."""
    try:
        cited = json.loads(raw)['handoff']['source_episode_ids']
    except (ValueError, KeyError, TypeError):
        return []
    return [identity for identity in cited if isinstance(identity, str)] if isinstance(cited, list) else []


def _projecting(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scope_marks'").fetchone() is not None


class Scope:
    """One call's resolved read scope. Registry membership is the call's own read."""

    def __init__(self, *, scope, tenant, binding, tenant_bindings, attribution, claims, session_id):
        self.scope, self.tenant, self.binding = scope, tenant, binding
        self.tenant_bindings = frozenset(tenant_bindings)
        self.attribution, self.claims, self.session_id = attribution, claims, session_id
        self.view_ids = self.view_aliases = None
        self.view_agent = False
        terms = []
        for c in claims:
            obj, predicate = c['object'], c['predicate']
            exact = (isinstance(predicate, str) and isinstance(obj, dict) and set(obj) == {'kind', 'id'} and
                     isinstance(obj['kind'], str) and isinstance(obj['id'], str))
            terms.append((predicate, obj['kind'], obj['id']) if exact else None)
        self.claim_terms = terms

    def restrict(self, view):
        """Apply a desk-profile view (``DeskProfiles.view_filter``)."""
        if view is not None and view['ids'] is not None:
            self.view_ids = frozenset(view['ids'])
            self.view_agent = view['agent']
            self.view_aliases = frozenset(view['aliases'])

    @property
    def filtered(self):
        return (self.attribution != 'any' or bool(self.claims) or self.session_id is not None or
                self.view_ids is not None)

    def report(self, excluded_unresolved):
        return {'scope': self.scope, 'binding_key': self.binding, 'attribution': self.attribution,
                'claim_filters': self.claims, 'session_id': self.session_id,
                'excluded_unresolved_episodes': excluded_unresolved,
                'unresolved_included': self.scope == 'topic' and self.attribution in ('any', 'unresolved')}

    # -- SQL over the projection (alias s = episode_scope) -------------------
    def filters(self):
        sql, params = [], []
        if self.attribution != 'any':
            sql.append('s.status = ?'); params.append(self.attribution)
        if self.session_id is not None:
            sql.append('s.source_session_id = ?'); params.append(self.session_id)
        for term in self.claim_terms:
            if term is None:
                sql.append('0')
            else:
                sql.append('EXISTS (SELECT 1 FROM episode_claims c WHERE c.episode_id = s.episode_id'
                           ' AND c.predicate = ? AND c.object_kind = ? AND c.object_id = ?)')
                params.extend(term)
        return sql, params

    def view_terms(self):
        """(kept, params, unverified, params) for a view; kept is '1' without one."""
        if self.view_ids is None:
            return '1', [], '0', []
        ids = json.dumps(sorted(self.view_ids))
        kept = f's.source_session_id IN ({_EACH})'
        if not self.view_agent:
            return kept, [ids], '0', []
        aliases = json.dumps(sorted(self.view_aliases))
        alias = f"s.binding_key IN ({_EACH}) AND s.status = 'resolved'"
        direct = 'coalesce(s.storage_binding = s.binding_key, 0)'
        keep = f'({kept} OR ({alias} AND ({direct} OR s.owners_unbounded = 1)))'
        unverified = f'(coalesce({kept}, 0) = 0 AND {alias} AND {direct} = 0 AND s.owners_unbounded = 0)'
        return keep, [ids, aliases], unverified, [ids, aliases]

    def member(self):
        """Per-row scope membership of s (used after an FTS match)."""
        if self.scope == 'desk':
            return ("EXISTS (SELECT 1 FROM episode_desks d WHERE d.desk = ? AND d.kind = s.kind"
                    " AND d.seq = s.seq AND d.episode_id = s.episode_id AND d.tenant IN (?, ''))",
                    [self.binding, self.tenant])
        return (f'(s.tenant = ? OR (s.tenant IS NULL AND s.storage_binding IN ({_EACH})))',
                [self.tenant, json.dumps(sorted(self.tenant_bindings))])

    def branches(self):
        """FROM/WHERE branches enumerating the scope through an index."""
        if self.scope == 'desk':
            return [('episode_desks d JOIN episode_scope s ON s.episode_id = d.episode_id',
                     "d.desk = ? AND d.tenant IN (?, '')", [self.binding, self.tenant])]
        return [('episode_scope s', 's.tenant = ?', [self.tenant]),
                ('episode_scope s', f's.tenant IS NULL AND s.storage_binding IN ({_EACH})',
                 [json.dumps(sorted(self.tenant_bindings))])]

    # -- re-derived checks for a reopened record ------------------------------
    def admits(self, item):
        if self.scope == 'desk' and self.binding not in item['desks']:
            return False
        if self.attribution != 'any' and item['attribution_status'] != self.attribution:
            return False
        if self.session_id is not None and self.session_id != item['source_session_id']:
            return False
        meta = item['metadata']
        if not all(meta and any(r['predicate'] == c['predicate'] and r['object'] == c['object']
                                for r in meta['active_claims']) for c in self.claims):
            return False
        if self.view_ids is None or item['source_session_id'] in self.view_ids:
            return True
        if not self.view_agent or item['binding_key'] not in self.view_aliases or item['attribution_status'] != 'resolved':
            return False
        if item['storage_binding'] == item['binding_key']:
            return True
        owners = [c for c in meta['active_claims'] if c['predicate'] == 'session.owner'] if meta else []
        return bool(owners) and all(c['valid_from'] is None and c['valid_until'] is None for c in owners)


class Opened:
    """One record reopened from its sealed row, with its scope re-derived."""
    __slots__ = ('identity', 'kind', 'raw', 'record', 'intact', 'visible', 'item', 'error')

    def __init__(self, identity):
        self.identity, self.kind, self.raw, self.record = identity, None, None, None
        self.intact = self.visible = False
        self.item = self.error = None

    def integrity_error(self):
        return EpisodeUnavailable('record integrity mismatch' if self.kind == 0 else
                                  'source episode integrity mismatch')


class SessionSources:
    def __init__(self, store):
        self.store = store

    def available(self, db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE name='source_sessions'").fetchone() is not None

    def upgrade(self, *, batch_rows=500, coverage_index=None):
        """The operator step: catalog, key-first rebuild, then the bounded backfill.

        An ``episode_desks`` declared by T10 (``tenant`` between key columns) is
        rebuilt key-first in one transaction and the store is then checked with
        ``PRAGMA quick_check``. A complete projection with nothing behind is not
        checked or scanned again (see ``backfill``).
        """
        self.ensure_catalog()
        rebuilt = self._rebuild_desks()
        if rebuilt is not None:
            self._check_integrity()
        report = {'schema_version': 'ops.session-catalog.v1', 'status': 'ready',
                  'rebuilt': {} if rebuilt is None else {'episode_desks': rebuilt}}
        report.update(self._backfill(batch_rows, coverage_index, checked=rebuilt is not None))
        return report

    def ensure_catalog(self):
        """Schema-only entry for writers: the catalog tables their own writes need.

        It never installs, rebuilds or backfills the scope projection and never
        runs ``PRAGMA quick_check``; those are the operator's ``upgrade-sources``.
        Steady state costs one read of ``sqlite_master`` (beside the writable
        connection's outbox check, T12b). A watched catalog table is created with its
        outbox trigger in the same transaction. The writers (seal, claim,
        link, ``import_episode``, ``record_project``) then:

        - projection not installed (no ``scope_marks``): seal without projecting
          (``_reflect`` returns). Their rows count as ``written`` only once
          ``upgrade-sources`` installs the marks, which counts every existing row;
        - installed but incomplete: project only the rows they seal, in the same
          transaction (the trigger counts the row ``written``, the writer counts it
          ``projected``). Existing rows stay unprojected, so reads keep refusing
          with ``projection_incomplete`` until ``upgrade-sources``;
        - installed and complete: the same, so the projection stays complete.
        """
        with closing(self.store._connect()) as db, db:
            present = {row[0] for row in db.execute(
                f"SELECT name FROM sqlite_master WHERE type IN ('table', 'index') AND name IN ({_EACH})",
                (json.dumps(_CATALOG),))}
            if present != set(_CATALOG):
                # One schema step: a watched table and its outbox trigger commit together (T12b B1).
                db.executescript('BEGIN IMMEDIATE;\n' + _SCHEMA + 'COMMIT;\n')

    def _capture_identity(self, db, session, binding):
        if not self.available(db): return None, None
        admission = self.store.sessions.resolve(session, self.store.registry)
        payload = {'schema_version': 'ops.source-session.v1', 'tenant_id': admission.tenant_id,
                   'runtime': self.store.sessions.provider_instance, 'native_id': session}
        identity = _id('source-session', payload)
        db.execute('INSERT OR IGNORE INTO source_sessions VALUES (?,?,?)', (identity, admission.tenant_id, _bytes(payload)))
        claims = self._claims(db, identity)
        claimed = None
        if not claims:
            claim = dict(schema_version='ops.session-claim.v1', session_id=identity,
                         predicate='session.owner', object={'kind': 'desk', 'id': binding},
                         asserted_by='admission-ledger:' + self.store.sessions.provider_instance,
                         recorded_at=datetime.now(timezone.utc).isoformat(), evidence=['admitted-capture:' + session],
                         valid_from=None, valid_until=None, supersedes=None, retracted=False)
            claimed = _id('session-claim', claim)
            db.execute('INSERT INTO session_claims VALUES (?,?,?)', (claimed, identity, _bytes(claim)))
        return identity, claimed

    def register(self, *, tenant_id, runtime, native_id):
        for name, value in [('tenant', tenant_id), ('runtime', runtime), ('native session', native_id)]:
            _text(value, 512, name)
        payload = {'schema_version': 'ops.source-session.v1', 'tenant_id': tenant_id,
                   'runtime': runtime, 'native_id': native_id}
        identity = _id('source-session', payload)
        with closing(self.store._connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO source_sessions VALUES (?,?,?)',
                       (identity, tenant_id, _bytes(payload)))
        return identity

    def record_project(self, *, session_id, tenant_id, repo_key, evidence):
        """Seal verified workspace membership without claiming desk ownership."""
        _text(repo_key, 512, 'repository key')
        if not isinstance(evidence, dict) or set(evidence) != {'cwd', 'git_common_dir', 'source_file'}:
            raise ValueError('exact workspace evidence required')
        for value in evidence.values():
            _text(value, 4096, 'workspace evidence')
        with closing(self.store._connect()) as db, db:
            self._session(db, session_id, tenant_id)
            proof = {'session_id': session_id, 'tenant_id': tenant_id,
                     'repo_key': repo_key, 'evidence': evidence}
            digest = _id('source-project', proof)
            prior = db.execute('SELECT repo_key,evidence,digest FROM source_projects WHERE session_id=?',
                               (session_id,)).fetchone()
            if prior:
                old = json.loads(prior[1])
                old_proof = {'session_id': session_id, 'tenant_id': tenant_id,
                             'repo_key': prior[0], 'evidence': old}
                if (_id('source-project', old_proof) != prior[2] or prior[0] != repo_key or
                        old['git_common_dir'] != evidence['git_common_dir']):
                    raise EpisodeConflict('source project membership changed')
            if not prior:
                db.execute('INSERT INTO source_projects VALUES (?,?,?,?,?)',
                           (session_id, tenant_id, repo_key, _bytes(evidence), digest))

    def project_ids(self, *, tenant_id, repo_key):
        """Sessions of one repository; every project row of the tenant is verified in one read."""
        with closing(self.store._connect(readonly=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='source_projects'").fetchone() is None:
                return set()
            rows = db.execute('SELECT p.session_id, p.tenant, p.repo_key, p.evidence, p.digest, s.tenant, s.payload'
                              ' FROM source_projects p LEFT JOIN source_sessions s ON s.id = p.session_id'
                              ' WHERE p.tenant = ? ORDER BY p.rowid', (tenant_id,)).fetchall()
        selected = set()
        for sid, tenant, repo, raw, digest, session_tenant, session_raw in rows:
            proof = {'session_id': sid, 'tenant_id': tenant,
                     'repo_key': repo, 'evidence': json.loads(raw)}
            if _id('source-project', proof) != digest:
                raise EpisodeUnavailable('source project projection mismatch')
            if session_tenant is None or session_tenant != tenant_id:
                raise EpisodeUnavailable('source session unavailable to this tenant')
            payload = json.loads(session_raw)
            if _id('source-session', payload) != sid or payload['tenant_id'] != session_tenant:
                raise EpisodeUnavailable('source session integrity mismatch')
            if repo == repo_key: selected.add(sid)
        return selected

    def _session(self, db, identity, tenant=None):
        row = db.execute('SELECT tenant,payload FROM source_sessions WHERE id=?', (identity,)).fetchone()
        if row is None or (tenant is not None and row[0] != tenant):
            raise EpisodeUnavailable('source session unavailable to this tenant')
        payload = json.loads(row[1])
        if _id('source-session', payload) != identity or payload['tenant_id'] != row[0]:
            raise EpisodeUnavailable('source session integrity mismatch')
        return payload

    def claim(self, *, session_id, predicate, object, asserted_by, recorded_at,
              evidence, valid_from=None, valid_until=None, supersedes=None,
              retracted=False, source_range=None):
        """Append a typed claim; vocabulary is namespaced, not a role-name enum.

        owner and contributor are distinct predicates with the same claim envelope.
        Supersession targets one claim on this session/predicate and cannot fork.
        The session's episodes are re-projected in the same transaction.
        """
        if not isinstance(predicate, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,127}', predicate):
            raise ValueError('namespaced claim predicate required')
        if not isinstance(object, dict) or set(object) != {'kind', 'id'}:
            raise ValueError('typed claim object required')
        _text(object['kind'], 128, 'object kind'); _text(object['id'], 1024, 'object identity')
        _text(asserted_by, 512, 'claim actor'); _time(recorded_at)
        if valid_from is not None: _time(valid_from)
        if valid_until is not None: _time(valid_until)
        if valid_from and valid_until and _time(valid_from) >= _time(valid_until):
            raise ValueError('claim interval is empty')
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 32:
            raise ValueError('1..32 claim evidence references required')
        for item in evidence: _text(item, 4096, 'claim evidence reference')
        if source_range is not None:
            if (not isinstance(source_range, dict) or
                    set(source_range) != {'native_id', 'source_file', 'start_offset'} or
                    not isinstance(source_range['native_id'], str) or
                    not isinstance(source_range['source_file'], str) or
                    not Path(source_range['source_file']).is_absolute() or
                    type(source_range['start_offset']) is not int or
                    source_range['start_offset'] < 0):
                raise ValueError('exact native source range required')
            _text(source_range['native_id'], 512, 'source range native identity')
            _text(source_range['source_file'], 4096, 'source range file')
        if type(retracted) is not bool or (retracted and supersedes is None):
            raise ValueError('retraction must supersede a claim')
        payload = dict(schema_version='ops.session-claim.v1', session_id=session_id,
                       predicate=predicate, object=object, asserted_by=asserted_by,
                       recorded_at=recorded_at, evidence=evidence, valid_from=valid_from,
                       valid_until=valid_until, supersedes=supersedes, retracted=retracted)
        if source_range is not None:
            payload['source_range'] = source_range
        identity = _id('session-claim', payload)
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            source_session = self._session(db, session_id)
            if source_range is not None and source_range['native_id'] != source_session['native_id']:
                raise ValueError('source range must name this native session')
            rows = self._claims(db, session_id)
            if identity in {r['claim_id'] for r in rows}: return identity
            if supersedes is not None:
                prior = next((r for r in rows if r['claim_id'] == supersedes), None)
                if prior is None or prior['predicate'] != predicate:
                    raise ValueError('supersession must reference same-session same-predicate claim')
                if any(r['supersedes'] == supersedes for r in rows):
                    raise EpisodeConflict('claim already superseded')
                if _time(recorded_at) < _time(prior['recorded_at']):
                    raise ValueError('supersession cannot predate original claim')
                if retracted and object != prior['object']:
                    raise ValueError('retraction must retain original object')
            db.execute('INSERT INTO session_claims VALUES (?,?,?)', (identity, session_id, _bytes(payload)))
            self._reflect(db, sessions=[session_id], claims=[identity])
        return identity

    def _claims(self, db, session_id):
        result = []
        for identity, raw in db.execute('SELECT id,payload FROM session_claims WHERE session_id=? ORDER BY rowid', (session_id,)):
            p = json.loads(raw)
            if _id('session-claim', p) != identity or p['session_id'] != session_id:
                raise EpisodeUnavailable('session claim integrity mismatch')
            result.append(dict(p, claim_id=identity))
        return result

    def metadata(self, session_id, tenant=None, *, db=None):
        if db is None:
            with closing(self.store._connect(readonly=True)) as connection:
                return self.metadata(session_id, tenant, db=connection)
        payload = self._session(db, session_id, tenant)
        return _describe(session_id, payload, self._claims(db, session_id))

    @staticmethod
    def _at_episode(meta, coordinates):
        """Narrow ranged historical claims to the exact source byte interval."""
        if meta is None:
            return None
        active = []
        for claim in meta['active_claims']:
            boundary = claim.get('source_range')
            if boundary is None:
                active.append(claim)
            elif (isinstance(coordinates, dict) and
                  boundary['native_id'] == meta['native_id'] and
                  coordinates.get('path') == boundary['source_file'] and
                  type(coordinates.get('start')) is int and
                  coordinates['start'] >= boundary['start_offset']):
                active.append(claim)
        desks = sorted({claim['object']['id'] for claim in active
                        if claim['predicate'] == 'session.owner' and
                        claim['object']['kind'] == 'desk'})
        return {**meta, 'active_claims': active, 'desk_bindings': desks,
                'attribution_status': ('unresolved' if not desks else
                                       'resolved' if len(desks) == 1 else 'conflicting')}

    def link(self, *, episode_id, session_id):
        """Attach canonical identity without rewriting an existing episode."""
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            session = self._session(db, session_id)
            row = db.execute('SELECT binding FROM episodes WHERE id=?', (episode_id,)).fetchone()
            if row:
                self.store._read(row[0], episode_id, 'episodes', 'episode')
                bindings = {r.binding_key: r.tenant_id for r in self.store.registry.list_bindings()}
                if bindings.get(row[0]) != session['tenant_id']:
                    raise EpisodeUnavailable('episode tenant differs from source session')
            else:
                row = db.execute('SELECT tenant,session_id FROM source_episodes WHERE id=?', (episode_id,)).fetchone()
                if not row or row != (session['tenant_id'], session_id):
                    raise EpisodeUnavailable('source episode unavailable')
            prior = db.execute('SELECT session_id FROM session_episodes WHERE episode_id=?', (episode_id,)).fetchone()
            if prior and prior[0] != session_id: raise EpisodeConflict('episode already linked to another session')
            inserted = db.execute('INSERT OR IGNORE INTO session_episodes VALUES (?,?)',
                                  (episode_id, session_id)).rowcount == 1
            if inserted:
                self._reflect(db, episodes=[episode_id], links=[episode_id])

    def import_episode(self, *, session_id, source_ref, events, provenance):
        _text(source_ref, 1024, 'source reference')
        if not isinstance(events, list) or not 1 <= len(events) <= 125:
            raise ValueError('1..125 visible events required')
        seen = set()
        for event in events:
            if not isinstance(event, dict) or set(event) != {'event_id', 'role', 'text'}:
                raise ValueError('exact visible event required')
            _text(event['event_id'], 128, 'event identity'); _text(event['text'], 128000, 'event text')
            if event['event_id'] in seen or event['role'] not in {'user', 'assistant', 'tool'}:
                raise ValueError('unique visible event identity and role required')
            seen.add(event['event_id'])
        if not isinstance(provenance, dict) or not all(k in provenance for k in
                ('source_system', 'row_id', 'row_digest', 'evidence_event_map', 'import_actor')):
            raise ValueError('source provenance and import actor required')
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            session = self._session(db, session_id)
            payload = dict(schema_version='ops.episode.v2', tenant_id=session['tenant_id'],
                           source_session_id=session_id, provider_instance='operator-transcript-import',
                           session=session_id, source_ref=source_ref, events=events, source_provenance=provenance,
                           evidence_boundary='visible historical source; claims and truth not established by storage')
            raw = _bytes(payload)
            if len(raw) > 2_000_000: raise ValueError('episode exceeds two-megabyte bound')
            identity = _id('episode', payload)
            prior = db.execute('SELECT id FROM source_episodes WHERE session_id=? AND source_ref=?', (session_id, source_ref)).fetchone()
            if prior and prior[0] != identity: raise EpisodeConflict('source reference already sealed with different evidence')
            cursor = db.execute('INSERT OR IGNORE INTO source_episodes VALUES (?,?,?,?,?)',
                                (identity, session['tenant_id'], session_id, source_ref, raw))
            sealed = [self._sealed_source(identity, cursor.lastrowid, session_id, raw, payload)] if cursor.rowcount == 1 else []
            linked = db.execute('INSERT OR IGNORE INTO session_episodes VALUES (?,?)',
                                (identity, session_id)).rowcount == 1
            self._reflect(db, new=sealed, links=[identity] if linked else [])
        return {'episode_id': identity, 'status': 'already_present' if prior else 'imported'}

    def raw_rows(self, db):
        yield from db.execute('SELECT id,binding,payload FROM episodes')
        if self.available(db):
            yield from db.execute('SELECT id,NULL,payload FROM source_episodes')

    def read(self, identity, binding=None):
        if binding is not None:
            return self.store._read(binding, identity, 'episodes', 'episode')
        with closing(self.store._connect(readonly=True)) as db:
            row = db.execute('SELECT tenant,session_id,payload FROM source_episodes WHERE id=?', (identity,)).fetchone()
        if row is None: raise EpisodeUnavailable('source episode unavailable')
        p = json.loads(row[2])
        if _id('episode', p) != identity or (p['tenant_id'], p['source_session_id']) != row[:2]:
            raise EpisodeUnavailable('source episode integrity mismatch')
        return p

    def select(self, session, *, scope='desk', binding_key=None, attribution='any', claims=None, session_id=None):
        """Whole-store reference selection, kept as a compatibility and test oracle.

        Model reads use the scope projection (``resolve_scope``/``reopen``).
        """
        if scope not in ('desk', 'topic') or attribution not in ('any', 'resolved', 'unresolved', 'conflicting'):
            raise ValueError('invalid search scope or attribution filter')
        if scope == 'topic' and binding_key is not None:
            raise ValueError('binding_key selects desk scope; use desk scope with a query')
        admitted = self.store.sessions.resolve(session, self.store.registry)
        binding = self.store.read_binding(session, binding_key) if scope == 'desk' else None
        tenant_bindings = {r.binding_key for r in self.store.registry.list_bindings() if r.tenant_id == admitted.tenant_id}
        if claims is None: claims = []
        if not isinstance(claims, list) or len(claims) > 8: raise ValueError('at most eight claim filters')
        for c in claims:
            if not isinstance(c, dict) or set(c) != {'predicate', 'object'}: raise ValueError('typed claim filter required')
        selected = {}; metadata = {}; excluded_unresolved = 0
        with closing(self.store._connect()) as db:
            links = dict(db.execute('SELECT episode_id,session_id FROM session_episodes')) if self.available(db) else {}
            for identity, sid in links.items():
                if sid not in metadata:
                    try: metadata[sid] = self.metadata(sid, admitted.tenant_id, db=db)
                    except EpisodeUnavailable: metadata[sid] = None
            for identity, original_binding in db.execute('SELECT id,binding FROM episodes'):
                if original_binding in tenant_bindings or metadata.get(links.get(identity)) is not None:
                    selected[identity] = {'storage_binding': original_binding,
                                          'metadata': self._at_episode(metadata.get(links.get(identity)), None),
                                          'source_session_id': links.get(identity)}
            if self.available(db):
                for identity, sid, coordinates_raw in db.execute(
                        "SELECT id,session_id,json_extract(payload,'$.source_provenance.source_coordinates') "
                        'FROM source_episodes WHERE tenant=?', (admitted.tenant_id,)):
                    coordinates = json.loads(coordinates_raw) if coordinates_raw else None
                    selected[identity] = {'storage_binding': None,
                        'metadata': self._at_episode(metadata.get(sid), coordinates),
                        'source_session_id': sid}
            for identity, item in list(selected.items()):
                meta = item['metadata']
                if item['source_session_id'] and meta is None:
                    raise EpisodeUnavailable('source session metadata unavailable')
                desks = meta['desk_bindings'] if meta else [item['storage_binding']]
                status = meta['attribution_status'] if meta else 'resolved'
                item.update(binding_key=desks[0] if len(desks) == 1 else None, attribution_status=status)
                if scope == 'desk' and status == 'unresolved': excluded_unresolved += 1
                matching = (scope == 'topic' or binding in desks) and (attribution == 'any' or status == attribution)
                matching = matching and (session_id is None or session_id == item['source_session_id'])
                matching = matching and all(meta and any(r['predicate'] == c['predicate'] and r['object'] == c['object']
                                                         for r in meta['active_claims']) for c in claims)
                if not matching: del selected[identity]
        return selected, {'scope': scope, 'binding_key': binding, 'attribution': attribution,
                          'claim_filters': claims, 'session_id': session_id,
                          'excluded_unresolved_episodes': excluded_unresolved,
                          'unresolved_included': scope == 'topic' and attribution in ('any', 'unresolved')}

    # -- projection reads ---------------------------------------------------------
    def resolve_scope(self, session, *, scope='desk', binding_key=None, attribution='any', claims=None,
                      session_id=None):
        """Validate and resolve one call's scope; admission and registry are the call's own reads."""
        if scope not in ('desk', 'topic') or attribution not in ('any', 'resolved', 'unresolved', 'conflicting'):
            raise ValueError('invalid search scope or attribution filter')
        if scope == 'topic' and binding_key is not None:
            raise ValueError('binding_key selects desk scope; use desk scope with a query')
        admitted = self.store.sessions.resolve(session, self.store.registry)
        binding = self.store.read_binding(session, binding_key) if scope == 'desk' else None
        tenant_bindings = {r.binding_key for r in self.store.registry.list_bindings() if r.tenant_id == admitted.tenant_id}
        if claims is None: claims = []
        if not isinstance(claims, list) or len(claims) > 8: raise ValueError('at most eight claim filters')
        for c in claims:
            if not isinstance(c, dict) or set(c) != {'predicate', 'object'}: raise ValueError('typed claim filter required')
        return Scope(scope=scope, tenant=admitted.tenant_id, binding=binding, tenant_bindings=tenant_bindings,
                     attribution=attribution, claims=claims, session_id=session_id)

    def connect(self):
        """A read connection whose projection is complete (one statement)."""
        db = self.store._connect(readonly=True)
        try:
            self.state = self.projection_state(db)
        except BaseException:
            db.close()
            raise
        return db

    def projection_state(self, db):
        try:
            row = db.execute(
                "SELECT (SELECT count(*) FROM scope_marks WHERE written != projected),"
                " (SELECT count(*) FROM scope_marks),"
                " (SELECT value FROM scope_state WHERE key = 'complete'),"
                " (SELECT value FROM scope_state WHERE key = 'coverage_token')").fetchone()
        except sqlite3.Error as error:
            raise EpisodeProjectionIncomplete(_GUIDANCE) from error
        if row[0] or row[1] != len(_MARKED) or row[2] != PROJECTION_VERSION:
            raise EpisodeProjectionIncomplete(_GUIDANCE)
        return {'coverage_token': row[3]}

    def projection_ready(self):
        """Whether reads are served; ``memory.connection_status`` reports it."""
        return self.connection_state()[0]

    def connection_state(self):
        """(reads served, ``index_lag``) in one read connection: ``memory.connection_status``.

        ``index_lag`` is the store's index outbox row count (T12b): rows the indexer has not yet
        applied and deleted; 0 when the store has no outbox yet."""
        with closing(self.store._connect(readonly=True)) as db:
            try:
                self.state = self.projection_state(db)
                ready = True
            except EpisodeProjectionIncomplete:
                ready = False
            lag = outbox_lag(db)
        return ready, lag

    def counts(self, db, plan, *, coverage=True):
        """(total, covered, excluded_unresolved, unverified) for one resolved scope.

        Unfiltered desk and topic scopes read maintained aggregates. Filtered
        scopes count their own index range.
        """
        unresolved = [('unresolved', plan.tenant, '')] if plan.scope == 'desk' else []
        if not plan.filtered:
            if plan.scope == 'desk':
                keys = [('owner', plan.binding, plan.tenant), ('storage', plan.binding, '')]
            else:
                keys = [('tenant', plan.tenant, '')] + [('storage', b, '') for b in sorted(plan.tenant_bindings)]
            rows = db.execute(
                'SELECT c.scope, c.episodes, c.covered FROM json_each(?) j JOIN scope_counts c'
                " ON c.scope = json_extract(j.value, '$[0]') AND c.key = json_extract(j.value, '$[1]')"
                " AND c.tenant = json_extract(j.value, '$[2]')", (json.dumps(keys + unresolved),)).fetchall()
            total = sum(n for scope, n, _ in rows if scope != 'unresolved')
            covered = sum(c for scope, _, c in rows if scope != 'unresolved')
            excluded = sum(n for scope, n, _ in rows if scope == 'unresolved')
            return total, covered if coverage else 0, excluded, 0
        kept, kept_params, unverified, unverified_params = plan.view_terms()
        filters, filter_params = plan.filters()
        parts, params = [], []
        for source, where, where_params in plan.branches():
            parts.append(f'SELECT CASE WHEN {kept} THEN 1 ELSE 0 END AS k, s.covered AS c,'
                         f' CASE WHEN {unverified} THEN 1 ELSE 0 END AS u FROM {source}'
                         f' WHERE ' + ' AND '.join([where] + filters))
            params += kept_params + unverified_params + where_params + filter_params
        unresolved_sql = ("(SELECT episodes FROM scope_counts WHERE scope = 'unresolved' AND key = ? AND tenant = '')"
                          if unresolved else '0')
        params = ([plan.tenant] if unresolved else []) + params
        row = db.execute(f'SELECT coalesce(sum(k), 0), coalesce(sum(k * c), 0), coalesce(sum(u), 0), '
                         f'{unresolved_sql} FROM (' + ' UNION ALL '.join(parts) + ')', params).fetchone()
        return row[0], row[1] if coverage else 0, row[3] or 0, row[2]

    def page(self, db, plan, *, offset, limit):
        """One page of an unfiltered desk scope in base order (newest source records first)."""
        return [row[0] for row in db.execute(
            "SELECT d.episode_id FROM episode_desks d WHERE d.desk = ? AND d.tenant IN (?, '')"
            ' ORDER BY d.kind DESC, d.seq DESC LIMIT ? OFFSET ?',
            (plan.binding, plan.tenant, limit, offset))]

    def reopen(self, db, identities, plan, *, sessions=(), capsules=(), capsule_binding=None):
        """Sealed rows, links, sessions and claims for ``identities`` in ONE statement.

        Each record's scope is re-derived from its session's content-addressed
        claims exactly as ``select`` derives it; the projection grants nothing.
        ``capsules`` (of ``capsule_binding``) are read in the same statement,
        with the episodes they cite; their rows are left in ``capsule_rows``.
        """
        identities = list(dict.fromkeys(identities))
        self.capsule_rows = {}
        if not identities and not sessions and not capsules:
            return {}
        episodes, sources, session_rows, claim_rows = {}, {}, {}, defaultdict(list)
        for kind, identity, owner, raw, extra in db.execute(
                _REOPEN, (json.dumps(list(capsules)), json.dumps(identities), capsule_binding,
                          json.dumps(list(sessions)), capsule_binding)):
            if kind == 0: episodes[identity] = (owner, raw, extra)
            elif kind == 1: sources[identity] = (owner, raw, extra)
            elif kind == 2: session_rows[identity] = (owner, raw)
            elif kind == 3: claim_rows[owner].append((extra, identity, raw))
            else: self.capsule_rows[identity] = raw
        for raw in self.capsule_rows.values():
            identities.extend(_cited(raw))
        identities = list(dict.fromkeys(identities))
        result = {identity: Opened(identity) for identity in identities}
        described = {}
        self.unverified_sessions = set()

        def meta_of(sid):
            if sid not in described:
                row = session_rows.get(sid)
                try:
                    described[sid] = _verified_session(sid, row[0] if row else None, row[1] if row else None,
                                                       [(i, r) for _, i, r in sorted(claim_rows.get(sid, []))],
                                                       plan.tenant)
                except EpisodeUnavailable:
                    described[sid] = None
                    if row is not None and row[0] == plan.tenant:
                        # Sealed in this tenant, but its session or a claim fails its ID check.
                        self.unverified_sessions.add(sid)
            return described[sid]

        for sid in sessions:
            meta_of(sid)
        for identity in identities:
            opened = result[identity]
            if identity in episodes:
                binding, raw, sid = episodes[identity]
                opened.kind, opened.raw = 0, raw
                meta = meta_of(sid) if sid else None
                opened.visible = binding in plan.tenant_bindings or meta is not None
                if opened.visible and sid and meta is None:
                    opened.error = EpisodeUnavailable('source session metadata unavailable')
                try:
                    value = json.loads(raw)
                    opened.intact = _id('episode', value) == identity and value.get('binding_key') == binding
                except ValueError:
                    value = None
                opened.record = value if opened.intact else None
                at = self._at_episode(meta, None)
                storage = binding
            elif identity in sources:
                tenant, raw, sid = sources[identity]
                opened.kind, opened.raw = 1, raw
                opened.visible = tenant == plan.tenant
                at, storage = None, None
                try:
                    value = json.loads(raw)
                except ValueError:
                    value = None
                    opened.error = EpisodeUnavailable('source episode integrity mismatch')
                if value is not None:
                    opened.intact = (_id('episode', value) == identity and
                                     (value.get('tenant_id'), value.get('source_session_id')) == (tenant, sid))
                    opened.record = value if opened.intact else None
                if opened.visible and opened.error is None:
                    try:
                        coordinates = _coordinates(value)
                    except (ValueError, TypeError) as error:
                        opened.error = error
                    else:
                        meta = meta_of(sid)
                        if meta is None:
                            opened.error = EpisodeUnavailable('source session metadata unavailable')
                        at = self._at_episode(meta, coordinates)
            else:
                continue
            desks = at['desk_bindings'] if at else [storage]
            opened.item = {'storage_binding': storage, 'metadata': at, 'source_session_id': sid,
                           'binding_key': desks[0] if len(desks) == 1 else None,
                           'attribution_status': at['attribution_status'] if at else 'resolved',
                           'desks': desks}
        return result

    @staticmethod
    def public_item(item):
        return {k: item[k] for k in ('storage_binding', 'metadata', 'source_session_id',
                                     'binding_key', 'attribution_status')}

    def capsule(self, identity, binding):
        """A capsule read by the last ``reopen``, verified as ``EpisodeStore._read`` does."""
        raw = self.capsule_rows.get(identity)
        if raw is None:
            raise EpisodeUnavailable('record unavailable to this desk')
        value = json.loads(raw)
        if _id('episode-capsule', value) != identity or value.get('binding_key') != binding:
            raise EpisodeUnavailable('record integrity mismatch')
        return value

    def capsules_with_cited(self, session, binding, capsule_ids):
        """Capsules of ``binding`` and every episode they cite (topic scope), in one batched read."""
        topic = self.resolve_scope(session, scope='topic')
        with closing(self.connect()) as db:
            opened = self.reopen(db, [], topic, capsules=capsule_ids, capsule_binding=binding)
        records = [(identity, self.capsule(identity, binding)) for identity in capsule_ids]
        cited = {}
        for _, record in records:
            for identity in record['handoff']['source_episode_ids']:
                if identity not in cited:
                    cited[identity] = self.require(opened.get(identity, Opened(identity)), topic)[0]
        return records, cited

    def require(self, opened, plan, *, missing='record unavailable to this scope'):
        """A reopened record the caller must have: refuse exactly as the base read did."""
        if opened.visible and opened.error is not None:
            raise opened.error
        if not opened.visible or opened.item is None or not plan.admits(opened.item):
            raise EpisodeUnavailable(missing)
        if not opened.intact:
            raise opened.integrity_error()
        return opened.record, self.public_item(opened.item)

    def scoped_records(self, session, identities, *, scope='desk', binding_key=None, attribution='any',
                       missing='record unavailable to this scope'):
        """{id: (record, item)} for records the caller must have, in one batched read."""
        plan = self.resolve_scope(session, scope=scope, binding_key=binding_key, attribution=attribution)
        with closing(self.connect()) as db:
            opened = self.reopen(db, identities, plan)
        return {identity: self.require(opened[identity], plan, missing=missing) for identity in identities}

    def read_scoped(self, session, identity, *, scope='desk', binding_key=None):
        return self.scoped_records(session, [identity], scope=scope, binding_key=binding_key)[identity]

    # -- projection writes -----------------------------------------------------------
    @staticmethod
    def _sealed_legacy(identity, rowid, binding, raw):
        return {'episode_id': identity, 'kind': 0, 'seq': rowid, 'storage_binding': binding,
                'payload_sha256': leaf.sha256_hex(raw), 'coord_path': None, 'coord_start': None, 'session': None}

    @staticmethod
    def _sealed_source(identity, rowid, session_id, raw, payload):
        try:
            path, start = _coordinate_facts(_coordinates(payload))
        except (ValueError, TypeError):
            # The base read refused such coordinates; reopening this record still does.
            path = start = None
        return {'episode_id': identity, 'kind': 1, 'seq': rowid, 'storage_binding': None,
                'payload_sha256': leaf.sha256_hex(raw), 'coord_path': path, 'coord_start': start, 'session': session_id}

    def _reflect(self, db, *, new=(), episodes=(), sessions=(), claims=(), links=()):
        """Project rows sealed in this transaction and re-project what they change.

        Set-based and bounded: the new rows, the named episodes and the episodes
        of the named sessions. Statements never depend on the rest of the store.
        """
        if not _projecting(db):
            return
        targets = {row['episode_id']: dict(row, covered=0) for row in new}
        reproject = [i for i in dict.fromkeys(episodes) if i not in targets]
        sessions = list(dict.fromkeys(sessions))
        # The sessions whose claims or links are reflected here: the outbox row their insert's
        # trigger wrote (desks_changed) gets the old and new desk sets of the session's episodes
        # (T12b B1, reserved for T12c).
        changed = dict.fromkeys(sessions)
        if links:
            changed.update(dict.fromkeys(row[0] for row in db.execute(
                f'SELECT DISTINCT session_id FROM session_episodes WHERE episode_id IN ({_EACH})',
                (json.dumps(list(links)),))))
        desk_sets = {sid: (set(), set()) for sid in changed}
        old = {}
        if reproject or sessions:
            for row in db.execute(
                    f'SELECT {_SCOPE_COLUMNS} FROM episode_scope WHERE episode_id IN ({_EACH})'
                    f' UNION SELECT {_SCOPE_COLUMNS} FROM episode_scope WHERE source_session_id IN ({_EACH})',
                    (json.dumps(reproject), json.dumps(sessions))):
                (identity, kind, seq, storage, tenant, sid, digest, path, start, status, _, _, covered) = row
                old[identity] = {'tenant': tenant, 'storage_binding': storage, 'status': status,
                                 'covered': covered, 'desks': []}
                targets[identity] = {'episode_id': identity, 'kind': kind, 'seq': seq, 'storage_binding': storage,
                                     'payload_sha256': digest, 'coord_path': path, 'coord_start': start,
                                     'session': sid if kind == 1 else None, 'covered': covered}
        if targets:
            ids = json.dumps(list(targets))
            for identity, desk in db.execute(f'SELECT episode_id, desk FROM episode_desks WHERE episode_id IN ({_EACH})', (ids,)):
                if identity in old:
                    old[identity]['desks'].append(desk)
            linked = dict(db.execute(f'SELECT episode_id, session_id FROM session_episodes WHERE episode_id IN ({_EACH})', (ids,)))
            metas = {}
            deltas = defaultdict(lambda: [0, 0])
            scope_rows, desk_rows, claim_rows = [], [], []
            for identity, base in targets.items():
                sid = base['session'] if base['kind'] == 1 else linked.get(identity)
                if sid is None:
                    tenant, desks, status, binding_key, unbounded, pairs = (
                        None, [base['storage_binding']], 'resolved', base['storage_binding'], 0, [])
                else:
                    if sid not in metas:
                        metas[sid] = self.metadata(sid, None, db=db)
                    meta = metas[sid]
                    start = int(base['coord_start']) if base['coord_start'] is not None else None
                    derived = _derive(meta, base['coord_path'], start)
                    tenant, desks, status = meta['tenant_id'], derived['desks'], derived['status']
                    binding_key, unbounded, pairs = derived['binding_key'], derived['owners_unbounded'], derived['pairs']
                row = {'tenant': tenant, 'storage_binding': base['storage_binding'], 'status': status}
                if sid in desk_sets:
                    desk_sets[sid][0].update(old[identity]['desks'] if identity in old else ())
                    desk_sets[sid][1].update(desks)
                if identity in old:
                    for key in _contributions(old[identity], old[identity]['desks']):
                        deltas[key][0] -= 1; deltas[key][1] -= old[identity]['covered']
                for key in _contributions(row, desks):
                    deltas[key][0] += 1; deltas[key][1] += base['covered']
                scope_rows.append([identity, base['kind'], base['seq'], base['storage_binding'], tenant, sid,
                                   base['payload_sha256'], base['coord_path'], base['coord_start'], status,
                                   binding_key, unbounded, base['covered']])
                desk_rows += [[desk, tenant if tenant is not None else '', base['kind'], base['seq'], identity]
                              for desk in desks]
                claim_rows += [[identity, *pair] for pair in pairs]
            db.execute(f'DELETE FROM episode_desks WHERE episode_id IN ({_EACH})', (ids,))
            db.execute(f'DELETE FROM episode_claims WHERE episode_id IN ({_EACH})', (ids,))
            db.execute(f'INSERT OR REPLACE INTO episode_scope ({_SCOPE_COLUMNS}) SELECT {_row_columns(13)}'
                       ' FROM json_each(?)', (json.dumps(scope_rows),))
            db.execute(f'INSERT INTO episode_desks (desk, tenant, kind, seq, episode_id) SELECT {_row_columns(5)}'
                       ' FROM json_each(?)', (json.dumps(desk_rows),))
            db.execute(f'INSERT INTO episode_claims (episode_id, predicate, object_kind, object_id)'
                       f' SELECT {_row_columns(4)} FROM json_each(?)', (json.dumps(claim_rows),))
            self._count(db, deltas)
        for sid, (before, after) in desk_sets.items():
            # The row its trigger just wrote: the latest desks_changed row of the session, by seq
            # (a descending rowid walk that stops at the first match, near the top).
            db.execute("UPDATE index_outbox SET detail = ? WHERE seq = (SELECT seq FROM index_outbox"
                       " WHERE session_id = ? AND reason = 'desks_changed' ORDER BY seq DESC LIMIT 1)"
                       " AND detail IS NULL",
                       (json.dumps({'old_desks': sorted(before), 'new_desks': sorted(after)}), sid))
        for table, kind in (('episodes', 0), ('source_episodes', 1)):
            fresh = sum(1 for row in new if row['kind'] == kind)
            if fresh:
                db.execute('UPDATE scope_marks SET projected = projected + ? WHERE tbl = ?', (fresh, table))
        for table, tracking, column, values in (('session_claims', 'projected_claims', 'claim_id', claims),
                                                 ('session_episodes', 'projected_links', 'episode_id', links)):
            if values:
                done = db.execute(f'INSERT OR IGNORE INTO {tracking} ({column}) {_EACH}',
                                  (json.dumps(list(dict.fromkeys(values))),)).rowcount
                db.execute('UPDATE scope_marks SET projected = projected + ? WHERE tbl = ?', (done, table))

    @staticmethod
    def _count(db, deltas):
        items = [[*key, change[0], change[1]] for key, change in deltas.items() if change[0] or change[1]]
        if not items:
            return
        payload = json.dumps(items)
        db.execute(f'INSERT INTO scope_counts (scope, key, tenant, episodes, covered) SELECT {_row_columns(5)}'
                   ' FROM json_each(?) WHERE 1 ON CONFLICT (scope, key, tenant) DO UPDATE SET'
                   ' episodes = episodes + excluded.episodes, covered = covered + excluded.covered', (payload,))
        db.execute(f'DELETE FROM scope_counts WHERE episodes = 0 AND (scope, key, tenant) IN'
                   f' (SELECT {_row_columns(3)} FROM json_each(?))', (payload,))

    def mark_coverage(self, digests, token, *, reset=False, held=None):
        """Index writer: record which sealed episodes the index ``token`` covers; returns the rows flipped.

        ``digests`` maps episode IDs to the payload digest the index now holds.
        Coverage follows reassignment without opening the index.

        The one bound (T12b meet ruling), whatever the caller (a drain's batch,
        ``upsert_episodes``, the post-swap marks, ``upgrade-sources``): the marks
        are written in store transactions of at most ``episodic_search.MARK_BATCH``
        episodes. Each chunk commits under the token it checked, after the index
        commit it follows, so a reader between chunks under-reports, never
        over-reports. With ``held`` (``upgrade-sources``), ``digests`` is the
        episode IDs and ``held(db, ids)`` returns their indexed digests, read in
        the chunk's own transaction under the store's write lock, so an index
        writer's later mark always lands after it.

        A different index (token) starts from no coverage: when the store names
        another token (or none), as after an index file is lost and the next drain
        recreates it, the marks are cleared in bounded store transactions
        (``_reset_coverage``) before this call marks; after COVERAGE_RESETS such
        resets with the store still naming another token, ``CoverageTokenUnstable``
        (``coverage_token_unstable``) is raised. ``reset`` (a build-and-swap's
        new token, T12b B3) clears them first in the same way. A chunk never deletes
        the store's ``coverage_marks_lost`` row: only a full re-mark does
        (``clear_marks_lost``).
        """
        from kp_agent_tooling._impl.service.episodic_search import MARK_BATCH
        if reset:
            self._reset_coverage(token)
        items = list(digests) if held is not None else list(digests.items())
        flipped = resets = 0
        for first in range(0, max(len(items), 1), MARK_BATCH):
            chunk = items[first:first + MARK_BATCH]
            while True:
                with closing(self.store._connect()) as db, db:
                    db.execute('BEGIN IMMEDIATE')
                    if not _projecting(db):
                        return flipped
                    current = db.execute("SELECT value FROM scope_state WHERE key = 'coverage_token'").fetchone()
                    if current is not None and current[0] == token:
                        flipped += self._cover(db, held(db, chunk) if held is not None else dict(chunk))
                        break
                # The store names another index's token (or none): never one bulk reset under the write lock.
                # The retry is capped: a switch that never lands raises (a drain error), it does not spin.
                if resets >= COVERAGE_RESETS:
                    raise CoverageTokenUnstable(
                        f'coverage_token_unstable: the store {self.store.path} still names coverage token '
                        f'{current[0] if current else None!r}, not the index token {token!r}, after '
                        f'{COVERAGE_RESETS} resets')
                resets += 1
                self._reset_coverage(token)
        return flipped

    def clear_marks_lost(self):
        """A full re-mark of this store has completed: delete its ``coverage_marks_lost`` row, in one small
        transaction after the last chunk, so a failure before it leaves the record in place (T12b meet ruling).
        Its callers: a reindex once its post-swap marks complete (``_build_and_swap``, a new token or a carried
        one) and ``upgrade-sources`` once its re-mark completes (``_establish_coverage``). A drain's incremental
        marks never call it."""
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if _projecting(db):
                db.execute('DELETE FROM scope_state WHERE key = ?', (MARKS_LOST_KEY,))

    def _reset_coverage(self, token):
        """Coverage of the index ``token`` starts from none (T12b B3, meet ruling), and no store transaction
        here changes more than ``episodic_search.MARK_BATCH`` episode_scope rows, so a sealer's commit never
        waits on one long write lock. Every coverage reset is this one: a build-and-swap's new token
        (``mark_coverage(reset=True)``), a mark for an index the store does not name (``mark_coverage``: an
        index file lost and recreated by the next drain) and ``upgrade-sources`` (``_establish_coverage``).

        The ordering rests on the readers' rule: coverage recorded for another index is reported as none.
        ``EpisodicSearchIndex.search_records`` reads the index's ``SELECT token FROM idx.coverage_token`` and
        sets ``covered = 0`` when ``token is None or token[0] != sources.state['coverage_token']``, the store's
        token read by ``projection_state`` (``(SELECT value FROM scope_state WHERE key = 'coverage_token')``).
        It is the only read that reports coverage: ``memory.connection_status`` (``connection_state``) reports
        readiness and ``index_lag``, and the listings count episodes with ``counts(..., coverage=False)``.
        Each caller resets to the token of the index file in place while the store names another token (or
        none), so until step 2 every reader reports covered 0, whatever ``episode_scope.covered`` and
        ``scope_counts`` say:

        1. While the store still names the old token, ``covered`` is zeroed in transactions of at most
           MARK_BATCH rows. The rows are found by a read outside the write lock (keyset by episode_id); the
           passes repeat until one finds no covered row.
        2. One transaction names ``token`` and zeroes ``scope_counts``; it changes no episode_scope row.
        3. The caller's marks follow, at most MARK_BATCH episodes per transaction (``mark_coverage``).

        A reader sees the old token (covered 0) or ``token`` with only marks this index holds: coverage is
        under-reported until the marks land, never over-reported. Between the last pass and step 2 only an index
        writer covers a row: the drainer is this one (the index lease), and ``upgrade-sources``
        (``_establish_coverage``) covers by digest equality with the index file it reads; a row it covers then
        is left out of ``scope_counts`` (an under-count).
        """
        from kp_agent_tooling._impl.service.episodic_search import MARK_BATCH
        with closing(self.store._connect(readonly=True)) as reader, closing(self.store._connect()) as writer:
            if not _projecting(reader):
                return
            found = True
            while found:
                found, last = False, ''
                while True:
                    ids = [row[0] for row in reader.execute(
                        'SELECT episode_id FROM episode_scope WHERE episode_id > ? AND covered = 1'
                        ' ORDER BY episode_id LIMIT ?', (last, MARK_BATCH))]
                    if not ids:
                        break
                    found, last = True, ids[-1]
                    with writer:
                        writer.execute('BEGIN IMMEDIATE')
                        writer.execute(f'UPDATE episode_scope SET covered = 0 WHERE covered = 1'
                                       f' AND episode_id IN ({_EACH})', (json.dumps(ids),))
            with writer:
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('UPDATE scope_counts SET covered = 0 WHERE covered != 0')
                writer.execute("INSERT OR REPLACE INTO scope_state (key, value) VALUES ('coverage_token', ?)",
                               (token,))

    def _cover(self, db, digests):
        """Mark ``digests`` (one ``mark_coverage`` chunk) in the caller's transaction; returns the rows flipped."""
        if not digests:
            return 0
        ids = json.dumps(list(digests))
        rows = db.execute(f'SELECT episode_id, tenant, storage_binding, status, payload_sha256, covered'
                          f' FROM episode_scope WHERE episode_id IN ({_EACH})', (ids,)).fetchall()
        desks = defaultdict(list)
        for identity, desk in db.execute(f'SELECT episode_id, desk FROM episode_desks WHERE episode_id IN ({_EACH})', (ids,)):
            desks[identity].append(desk)
        deltas = defaultdict(lambda: [0, 0])
        flips = {0: [], 1: []}
        for identity, tenant, storage, status, digest, covered in rows:
            wanted = int(digests[identity] == digest)
            if wanted != covered:
                flips[wanted].append(identity)
                row = {'tenant': tenant, 'storage_binding': storage, 'status': status}
                for key in _contributions(row, desks[identity]):
                    deltas[key][1] += 1 if wanted else -1
        for value, flipped in flips.items():
            if flipped:
                db.execute(f'UPDATE episode_scope SET covered = ? WHERE episode_id IN ({_EACH})',
                           (value, json.dumps(flipped)))
        self._count(db, deltas)
        return len(flips[0]) + len(flips[1])

    # -- explicit operator backfill ------------------------------------------------
    def _rebuild_desks(self):
        """Rebuild an ``episode_desks`` declared with ``tenant`` between its key columns.

        Detected, not assumed: ``PRAGMA table_info`` with ``tenant`` at cid 1. In
        one ``BEGIN IMMEDIATE`` transaction it creates the key-first table, copies
        every row, drops the old table, renames the new one and recreates every
        index and trigger that was on it. Returns the rows copied, or None when
        the table is absent or already key-first.
        """
        def columns(db):
            return [row[1] for row in db.execute("PRAGMA table_info('episode_desks')")]

        with closing(self.store._connect()) as db, db:
            if columns(db) != _OLD_DESK_COLUMNS:
                return None
            db.execute('BEGIN IMMEDIATE')
            if columns(db) != _OLD_DESK_COLUMNS:  # another upgrade rebuilt it first
                return None
            dependents = [row[0] for row in db.execute(
                "SELECT sql FROM sqlite_master WHERE type IN ('index', 'trigger')"
                " AND tbl_name = 'episode_desks' AND sql IS NOT NULL ORDER BY type, name")]
            names = ', '.join(_DESK_COLUMNS)
            db.execute('CREATE TABLE episode_desks_rebuild ' + _DESKS)
            copied = db.execute(f'INSERT INTO episode_desks_rebuild ({names}) SELECT {names} FROM episode_desks').rowcount
            db.execute('DROP TABLE episode_desks')
            db.execute('ALTER TABLE episode_desks_rebuild RENAME TO episode_desks')
            for statement in dependents:
                db.execute(statement)
            return copied

    def _check_integrity(self):
        """The whole-store integrity check: an operator step, never a per-read or per-pass cost."""
        with closing(self.store._connect(readonly=True)) as db:
            if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise EpisodeUnavailable('episode store integrity check failed; backfill refused')

    def _complete_marks(self):
        """The per-table marks when the projection is complete and nothing is behind, else None.

        It reads only ``sqlite_master``, ``scope_state`` and ``scope_marks``.
        """
        with closing(self.store._connect(readonly=True)) as db:
            if not _projecting(db):
                return None
            rows = db.execute("SELECT m.tbl, m.written, m.projected, s.value FROM scope_marks m"
                              " LEFT JOIN scope_state s ON s.key = 'complete'").fetchall()
        marks = {tbl: (written, projected) for tbl, written, projected, _ in rows}
        if (sorted(marks) != sorted(_MARKED) or any(written != projected for written, projected in marks.values())
                or any(value != PROJECTION_VERSION for *_, value in rows)):
            return None
        return marks

    def _install(self):
        """Create the projection schema, marks and triggers in one transaction."""
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if _projecting(db):
                return False
            for statement in _PROJECTION_DDL:
                db.execute(statement)
            for table in _MARKED:
                written = db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                db.execute('INSERT INTO scope_marks (tbl, written, projected) VALUES (?, ?, 0)', (table, written))
            for statement in _TRIGGERS:
                db.execute(statement)
            return True

    def backfill(self, *, batch_rows=500, coverage_index=None):
        """Project rows sealed without a projection, in bounded batches.

        Each batch is its own transaction, so new-version writers (which project
        their own rows) may run alongside. Rows are verified against their
        content address before projection; a mismatch stops the backfill with
        the store still marked incomplete. A second run changes nothing.

        The completion marker and the per-table marks are read first. When the
        projection is complete and nothing is behind, it projects nothing and
        runs neither ``PRAGMA quick_check`` nor any scan; only the coverage step
        runs, when ``coverage_index`` names an index. Otherwise ``quick_check``
        runs before anything is projected.
        """
        return self._backfill(batch_rows, coverage_index, checked=False)

    def _backfill(self, batch_rows, coverage_index, *, checked):
        if type(batch_rows) is not int or not 1 <= batch_rows <= 100000:
            raise ValueError('batch_rows must be 1..100000')
        report = {'projection': PROJECTION_VERSION, 'installed': False, 'batches': 0,
                  'projected': {table: 0 for table in _MARKED},
                  'already_projected': {table: 0 for table in _MARKED},
                  'verified_episodes': 0}
        marks = self._complete_marks()
        if marks is not None:
            report['already_complete'] = True
            if coverage_index is not None:
                report['coverage'] = self._establish_coverage(Path(coverage_index))
            report['marks'] = {table: {'written': marks[table][0], 'projected': marks[table][1]} for table in _MARKED}
            report['status'] = 'complete'
            return report
        if not checked:
            self._check_integrity()
        report['installed'] = self._install()
        with closing(self.store._connect()) as db:
            done = db.execute("SELECT value FROM scope_state WHERE key = 'complete'").fetchone()
            behind = db.execute('SELECT count(*) FROM scope_marks WHERE written != projected').fetchone()[0]
        if done and done[0] == PROJECTION_VERSION and not behind:
            report['already_complete'] = True
        else:
            report['already_complete'] = False
            while True:
                progress = False
                for table in _MARKED:
                    while self._backfill_batch(table, batch_rows, report):
                        progress = True
                with closing(self.store._connect()) as db:
                    behind = db.execute('SELECT count(*) FROM scope_marks WHERE written != projected').fetchone()[0]
                if not behind:
                    break
                if not progress:
                    raise EpisodeUnavailable('projection backfill cannot account for every sealed row')
        if coverage_index is not None:
            report['coverage'] = self._establish_coverage(Path(coverage_index))
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            marks = {row[0]: row[1:] for row in db.execute('SELECT tbl, written, projected FROM scope_marks')}
            if any(written != projected for written, projected in marks.values()):
                raise EpisodeUnavailable('projection backfill incomplete; rerun upgrade-sources')
            db.execute("INSERT INTO scope_state (key, value) VALUES ('complete', ?)"
                       ' ON CONFLICT (key) DO UPDATE SET value = excluded.value WHERE value != excluded.value',
                       (PROJECTION_VERSION,))
            db.execute("DELETE FROM scope_state WHERE key LIKE 'cursor:%'")
        report['marks'] = {table: {'written': marks[table][0], 'projected': marks[table][1]} for table in _MARKED}
        report['status'] = 'complete'
        return report

    def _backfill_batch(self, table, batch_rows, report):
        columns = {'episodes': 'rowid, id, binding, payload', 'source_episodes': 'rowid, id, session_id, payload',
                   'session_claims': 'rowid, id, session_id', 'session_episodes': 'rowid, episode_id, session_id'}[table]
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            cursor_key = 'cursor:' + table
            row = db.execute('SELECT value FROM scope_state WHERE key = ?', (cursor_key,)).fetchone()
            cursor = int(row[0]) if row else 0
            rows = db.execute(f'SELECT {columns} FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?',
                              (cursor, batch_rows)).fetchall()
            if not rows:
                return False
            ids = json.dumps([r[1] for r in rows])
            if table in ('episodes', 'source_episodes'):
                present = {r[0] for r in db.execute(f'SELECT episode_id FROM episode_scope WHERE episode_id IN ({_EACH})', (ids,))}
            else:
                tracking, column = (('projected_claims', 'claim_id') if table == 'session_claims'
                                    else ('projected_links', 'episode_id'))
                present = {r[0] for r in db.execute(f'SELECT {column} FROM {tracking} WHERE {column} IN ({_EACH})', (ids,))}
            pending = [r for r in rows if r[1] not in present]
            if table == 'episodes':
                sealed = []
                for rowid, identity, binding, raw in pending:
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        value = None
                    if not isinstance(value, dict) or _id('episode', value) != identity or value.get('binding_key') != binding:
                        raise EpisodeUnavailable('projection backfill refused: sealed episode integrity mismatch')
                    sealed.append(self._sealed_legacy(identity, rowid, binding, raw))
                self._reflect(db, new=sealed)
                report['verified_episodes'] += len(sealed)
            elif table == 'source_episodes':
                tenants = dict(db.execute(f'SELECT id, tenant FROM source_episodes WHERE id IN ({_EACH})', (ids,)))
                sealed = []
                for rowid, identity, session_id, raw in pending:
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        value = None
                    if (not isinstance(value, dict) or _id('episode', value) != identity or
                            (value.get('tenant_id'), value.get('source_session_id')) != (tenants[identity], session_id)):
                        raise EpisodeUnavailable('projection backfill refused: sealed source episode integrity mismatch')
                    sealed.append(self._sealed_source(identity, rowid, session_id, raw, value))
                self._reflect(db, new=sealed)
                report['verified_episodes'] += len(sealed)
            elif table == 'session_claims':
                self._reflect(db, sessions=[r[2] for r in pending], claims=[r[1] for r in pending])
            else:
                # A link changes a projection only when the episode's projected session differs.
                projected = dict(db.execute(f'SELECT episode_id, source_session_id FROM episode_scope'
                                            f' WHERE episode_id IN ({_EACH})', (json.dumps([r[1] for r in pending]),)))
                self._reflect(db, episodes=[r[1] for r in pending if r[1] in projected and projected[r[1]] != r[2]],
                              links=[r[1] for r in pending])
            report['projected'][table] += len(pending)
            report['already_projected'][table] += len(rows) - len(pending)
            report['batches'] += 1
            db.execute('INSERT OR REPLACE INTO scope_state (key, value) VALUES (?, ?)', (cursor_key, str(rows[-1][0])))
            return True

    def _establish_coverage(self, index_path):
        """Record which projected episodes an existing index covers (digest equality).

        The index is only read (T12b: its token, postings and watermark are written by its
        drainer alone). An index without a token covers nothing, and no marks are written for it.
        An *older-writer store* is one whose index does not cover every sealed (episode, digest)
        that has no ``seal`` outbox row waiting: rows sealed before the outbox existed and never
        indexed. For it, once (unless a ``reindex`` row is already waiting), one ``reindex`` outbox
        row is written; a complete store gets none. An absent index covers nothing: a store with sealed
        episodes and no index file is requested its reindex on this first run (T12b meet); an empty store,
        or one whose every sealed episode has a seal row waiting, requests nothing.

        When the store names another token than the index file (or none), its coverage is reset to the file's
        token by ``_reset_coverage``, in bounded transactions. This runs without the index lease, and the reset
        stays sound: while the store names a token other than the file in place, every reader reports covered
        0 (``search_records``' token rule), so the zeroing passes change nothing a reader reports. The switch
        names the token of the file read here; if a drainer has swapped in another file since, the tokens differ
        and readers still report 0, and that drainer resets to its own token before naming it (after its swap,
        or at its next mark through ``mark_coverage``'s mismatch branch), clearing these marks. Every mark below
        is digest equality with the file at ``index_path``, read under the store's write lock, so a reader that
        sees matching tokens sees only marks of the file it reads: coverage can be under-reported, never
        over-reported.
        """
        from kp_agent_tooling._impl.service.episodic_search import MARK_BATCH, EpisodicSearchIndex, request_reindex
        if not index_path.exists() and not index_path.is_symlink():
            with closing(self.store._connect(readonly=True)) as db:
                sealed, waiting = db.execute(
                    "SELECT (SELECT count(*) FROM episode_scope),"
                    " (SELECT count(DISTINCT episode_id) FROM index_outbox WHERE reason = 'seal')").fetchone()
            uncovered = max(0, sealed - waiting)
            requested = request_reindex(self.store, once=True) is not None if uncovered else False
            return {'index': 'absent', 'uncovered_episodes': uncovered, 'reindex_requested': requested}
        if index_path.is_symlink() or not index_path.is_file():
            return {'index': 'absent'}
        index = EpisodicSearchIndex(index_path, episode_store=self.store)
        token = index.token()
        flipped = examined = uncovered = 0
        if token is not None:
            with closing(self.store._connect(readonly=True)) as db:
                current = db.execute("SELECT value FROM scope_state WHERE key = 'coverage_token'").fetchone()
            if current is None or current[0] != token:
                # Coverage recorded for another index is not coverage of this one (bounded; see above).
                self._reset_coverage(token)
        last = ''
        while True:
            # Pages of MARK_BATCH episodes (``--batch-rows`` sizes the projection backfill only); each is marked
            # by ``mark_coverage`` in one chunk.
            with closing(self.store._connect(readonly=True)) as db:
                ids = [row[0] for row in db.execute('SELECT episode_id FROM episode_scope WHERE episode_id > ?'
                                                    ' ORDER BY episode_id LIMIT ?', (last, MARK_BATCH))]
            if not ids:
                break
            page = {}

            def held(db, chunk, page=page):
                # In the chunk's store transaction: the write lock is held while the index is read, so an
                # index writer's later coverage mark always lands after this chunk.
                rows = db.execute(f'SELECT episode_id, payload_sha256 FROM episode_scope WHERE episode_id IN ({_EACH})',
                                  (json.dumps(chunk),)).fetchall()
                indexed = index.digests(chunk)
                missing = [identity for identity, digest in rows if indexed.get(identity) != digest]
                waiting = {row[0] for row in db.execute(
                    f"SELECT episode_id FROM index_outbox WHERE reason = 'seal' AND episode_id IN ({_EACH})",
                    (json.dumps(missing),))} if missing else set()
                page['uncovered'] = sum(1 for identity in missing if identity not in waiting)
                return {identity: indexed.get(identity) for identity in chunk}

            if token is not None:
                flipped += self.mark_coverage(ids, token, held=held)
            else:
                with closing(self.store._connect(readonly=True)) as db:
                    held(db, ids)
            uncovered += page.get('uncovered', 0)
            examined += len(ids)
            last = ids[-1]
        if token is not None:
            self.clear_marks_lost()  # every projected episode re-marked against this index: a full re-mark
        requested = request_reindex(self.store, once=True) is not None if uncovered else False
        return {'index': 'present', 'examined_episodes': examined, 'coverage_changes': flipped,
                'uncovered_episodes': uncovered, 'reindex_requested': requested}
