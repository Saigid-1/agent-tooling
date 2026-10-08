"""Desk-scoped episodic records and loss-aware, evidence-linked handoffs.

Capture is an operator/provider adapter operation, not a model-authored fact.
Consolidation preserves interpretations separately from immutable source events.
"""
import json
import sqlite3
import copy
from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.episodic_provenance import origin
from datetime import datetime, timezone
from contextlib import closing
from kp_agent_tooling._impl.service.summary_contract import (full_request, slice_request,
    validate_summary_request, validate_summary_proposal)
from kp_agent_tooling._impl.service.desk_binding import call_memo, call_scope


def write_targets(store, session):
    """The operator write scope of this session, read once per memory call."""
    from kp_agent_tooling._impl.service.desk_write_scope import write_targets as read_targets
    memo = call_memo()
    if memo is None:
        return read_targets(store, session)
    key = ('write-targets', id(store), session)
    if key not in memo:
        memo[key] = read_targets(store, session)
    return memo[key]


class EpisodeConflict(ValueError):
    pass


class EpisodeUnavailable(RuntimeError):
    pass


class EpisodeUnknownBinding(ValueError):
    pass


class EpisodeProjectionIncomplete(EpisodeUnavailable):
    """Rows were sealed without a scope projection; reads refuse until an operator backfill.

    Reads never fall back to walking the store. ``kp-agent-desk ... upgrade-sources``
    projects the remaining rows in bounded batches.
    """
    category = 'projection_incomplete'


class EpisodeBoundExceeded(ValueError):
    """A declared input bound was exceeded. ``bound`` names it for public errors.

    The message stays the historical one; public errors are built from ``bound``
    and fixed text, never from the message or the caller's input.
    """
    category = 'bound_exceeded'

    def __init__(self, bound, message):
        super().__init__(message)
        self.bound = bound


class EpisodeCitationRange(EpisodeBoundExceeded, EpisodeConflict):
    """A citation range outside its event; still an EpisodeConflict for callers."""


def _bytes(value):
    return leaf.canonical_bytes(value, ascii=True, allow_nan=True)


def _id(kind, value):
    return leaf.content_id(kind, value, ascii=True, allow_nan=True)


# The index outbox (T12b B1), in the store. Every insert into a watched table appends exactly
# one row in the same transaction (an AFTER INSERT trigger, so an INSERT OR IGNORE that
# inserts nothing appends nothing, and a rolled-back insert leaves no row): ``seal`` for
# episodes and source_episodes (episode_id), ``desks_changed`` for session_episodes
# (episode_id, session_id) and session_claims (session_id). ``reindex`` rows are written by
# the callers that request a full reindex. ``detail`` is reserved for T12c's old and new
# desk sets (``SessionSources._reflect``). The one indexer drains it (``episodic_search``).
OUTBOX = 'index_outbox'
OUTBOX_WATCHED = ('episodes', 'source_episodes', 'session_claims', 'session_episodes')
OUTBOX_TABLE = ('CREATE TABLE IF NOT EXISTS index_outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT,'
                ' reason TEXT NOT NULL, episode_id TEXT, session_id TEXT, detail TEXT)')
_OUTBOX_ROW = {'episodes': ("'seal'", 'NEW.id', 'NULL'),
               'source_episodes': ("'seal'", 'NEW.id', 'NULL'),
               'session_episodes': ("'desks_changed'", 'NEW.episode_id', 'NEW.session_id'),
               'session_claims': ("'desks_changed'", 'NULL', 'NEW.session_id')}
# The store's user_version once the outbox exists with a trigger on every watched table: the
# writable connection's check reads it (one integer read of the header, no schema scan).
OUTBOX_VERSION = 1


def outbox_trigger(table):
    """The AFTER INSERT trigger appending ``table``'s outbox row."""
    reason, episode, session = _OUTBOX_ROW[table]
    return (f'CREATE TRIGGER IF NOT EXISTS {OUTBOX}_{table} AFTER INSERT ON {table} BEGIN'
            f' INSERT INTO {OUTBOX} (reason, episode_id, session_id) VALUES ({reason}, {episode}, {session}); END')


def outbox_schema(*tables):
    """The outbox and the triggers of ``tables``: part of the schema step that creates those tables."""
    return ''.join(statement + ';\n' for statement in (OUTBOX_TABLE, *map(outbox_trigger, tables)))


def ensure_outbox(db):
    """B1 on a writable store connection, before any seal in it: the outbox and a trigger on every
    watched table that exists.

    The check is exactly one read statement, ``PRAGMA user_version`` (OUTBOX_VERSION once installed).
    Only below it, on a store's first writable open by this code, does it write, in one ``BEGIN
    IMMEDIATE`` transaction: ``CREATE TABLE IF NOT EXISTS`` the outbox, ``CREATE TRIGGER IF NOT EXISTS``
    for every watched table that exists, and the version. Every later open shows only the read. A
    watched table created afterwards gets its trigger in the schema step that creates it.
    """
    if db.execute('PRAGMA user_version').fetchone()[0] >= OUTBOX_VERSION:
        return
    db.execute('BEGIN IMMEDIATE')
    try:
        # Read again under the write lock: another writer may have installed it first.
        if db.execute('PRAGMA user_version').fetchone()[0] < OUTBOX_VERSION:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN"
                                                   ' (SELECT value FROM json_each(?))', (json.dumps(OUTBOX_WATCHED),))}
            db.execute(OUTBOX_TABLE)
            for table in OUTBOX_WATCHED:
                if table in tables:
                    db.execute(outbox_trigger(table))
            db.execute(f'PRAGMA user_version = {OUTBOX_VERSION}')
        db.commit()
    except BaseException:
        db.rollback()
        raise


def outbox_lag(db):
    """``index_lag`` (T12b): the index outbox row count on the caller's store connection, the rows the indexer
    has not yet applied and deleted; 0 when the store has no outbox yet. The one implementation: it feeds
    memory.connection_status, the indexer's per-drain line and health, drain_after_seal's result and a host
    card's receipt."""
    try:
        return db.execute(f'SELECT count(*) FROM {OUTBOX}').fetchone()[0]
    except sqlite3.OperationalError as error:
        if 'no such table' in str(error):
            return 0  # a store no writable connection has opened since T12b
        raise


def _text(value, bound, field, key=None):
    if not isinstance(value, str) or not value:
        raise ValueError('invalid bounded ' + field)
    if len(value.encode('utf-8')) > bound:
        if key is not None:
            raise EpisodeBoundExceeded(key, 'invalid bounded ' + field)
        raise ValueError('invalid bounded ' + field)
    return value


class EpisodeStore:
    def __init__(self, path, *, session_ledger, registry):
        self.path = leaf.as_path(path)
        self.sessions, self.registry = session_ledger, registry

    def initialize(self):
        if self.path.exists() or self.path.is_symlink():
            raise EpisodeConflict('episode store already exists')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(leaf.sqlite_create(self.path)) as db:
            # The episodes table, the outbox, its trigger and the outbox version in one schema step
            # (T12b B1), on the file this call has just created (O_EXCL): no other writer has it yet.
            db.executescript('''
            CREATE TABLE episodes (id TEXT PRIMARY KEY, binding TEXT NOT NULL,
              session TEXT NOT NULL, source_ref TEXT NOT NULL, payload BLOB NOT NULL,
              UNIQUE(binding, session, source_ref));
            CREATE TABLE capsules (id TEXT PRIMARY KEY, binding TEXT NOT NULL,
              payload BLOB NOT NULL);
            ''' + outbox_schema('episodes') + f'PRAGMA user_version = {OUTBOX_VERSION};\n')
        from kp_agent_tooling._impl.service.session_sources import SessionSources
        SessionSources(self).upgrade()

    def _connect(self, readonly=False):
        """A store connection. A writable one ensures the index outbox first (``ensure_outbox``): a store
        created at any earlier version gets it on its first writable open, before any seal."""
        if self.path.is_symlink() or not self.path.is_file():
            raise EpisodeUnavailable('episode store unavailable')
        try:
            db = leaf.sqlite_connect(self.path, mode='ro' if readonly else 'rw', resolve=True)
        except sqlite3.Error as error:
            raise EpisodeUnavailable('episode store unavailable') from error
        if not readonly:
            try:
                ensure_outbox(db)
            except BaseException:
                db.close()
                raise
        return db

    def _binding(self, session):
        return self.sessions.resolve(session, self.registry).binding_key

    def read_binding(self, session, binding_key=None):
        """Resolve a model-requested read against current admission and registry."""
        admitted = self.sessions.resolve(session, self.registry)
        if binding_key is None:
            return admitted.binding_key
        if not isinstance(binding_key, str) or len(binding_key) > 128:
            raise ValueError('bounded binding key required')
        candidates = [record for record in self.registry.list_bindings()
                      if record.binding_key == binding_key]
        if not candidates:
            raise EpisodeUnknownBinding('requested binding is not registered')
        if candidates[0].tenant_id != admitted.tenant_id:
            raise EpisodeUnavailable('requested binding unavailable to this tenant')
        return binding_key

    def capture(self, session, *, source_ref, events):
        """Persist exact adapter-supplied visible events; never inferred reasoning."""
        with call_scope():
            return self._capture(session, source_ref=source_ref, events=events)

    def _capture(self, session, *, source_ref, events):
        binding = self._binding(session)
        _text(source_ref, 1024, 'source reference')
        if not isinstance(events, list) or not 1 <= len(events) <= 500:
            raise ValueError('1..500 visible events required')
        clean = []
        seen = set()
        for event in events:
            if not isinstance(event, dict) or set(event) != {'event_id', 'role', 'text'}:
                raise ValueError('event_id, role, text required')
            key = _text(event['event_id'], 128, 'event identity')
            if key in seen or event['role'] not in {'user', 'assistant', 'tool'}:
                raise ValueError('unique visible event identity and role required')
            seen.add(key)
            _text(event['text'], 128000, 'event text')
            clean.append(dict(event))
        payload = {'schema_version':'ops.episode.v1', 'binding_key':binding,
                   'provider_instance':self.sessions.provider_instance,
                   'session':session, 'source_ref':source_ref, 'events':clean,
                   'evidence_boundary':'adapter-supplied visible record; content truth not verified'}
        raw = _bytes(payload)
        if len(raw) > 2_000_000:
            raise ValueError('episode exceeds two-megabyte capture bound')
        identity = _id('episode', payload)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT id FROM episodes WHERE binding=? AND session=? AND source_ref=?',
                               (binding, session, source_ref)).fetchone()
            if prior and prior[0] != identity:
                raise EpisodeConflict('source reference already sealed with different events')
            cursor = db.execute('INSERT OR IGNORE INTO episodes VALUES (?,?,?,?,?)',
                                (identity, binding, session, source_ref, raw))
            from kp_agent_tooling._impl.service.session_sources import SessionSources
            sources = SessionSources(self)
            sealed = ([sources._sealed_legacy(identity, cursor.lastrowid, binding, raw)]
                      if cursor.rowcount == 1 else [])
            source_session, claimed = sources._capture_identity(db, session, binding)
            linked = False
            if source_session:
                linked = db.execute('INSERT OR IGNORE INTO session_episodes VALUES (?,?)',
                                    (identity, source_session)).rowcount == 1
            # The scope projection is written in this same transaction.
            sources._reflect(db, new=sealed, episodes=[identity] if linked else [],
                             sessions=[source_session] if claimed else [],
                             claims=[claimed] if claimed else [], links=[identity] if linked else [])
        return {'episode_id':identity, 'event_count':len(events), 'binding_key':binding}

    def import_operator_episode(self, *, tenant_id, role, repo_key, source_ref,
                                events, source_provenance):
        """Add legacy evidence through an operator path, without admitting a session.

        This method is deliberately absent from model tools. The importer's
        namespace is storage attribution, never the original agent identity.
        """
        binding = self.registry.resolve(tenant_id=tenant_id, role=role,
                                        repo_key=repo_key).binding_key
        _text(source_ref, 1024, 'source reference')
        if not isinstance(events, list) or not 1 <= len(events) <= 125:
            raise ValueError('1..125 imported evidence chunks required')
        if not isinstance(source_provenance, dict) or set(source_provenance) != {
                'source_system', 'row_id', 'row_digest', 'entity_type',
                'source_actor', 'source_observed_at', 'authored_at',
                'legacy_host_session_id', 'legacy_binding_provenance',
                'evidence_event_map'}:
            raise ValueError('complete legacy source provenance required')
        if source_provenance['source_system'] != 'postgres:org_ops.kp_nodes':
            raise ValueError('unsupported source system')
        clean = []
        for index, event in enumerate(events):
            if not isinstance(event, dict) or set(event) != {'event_id', 'role', 'text'}:
                raise ValueError('exact imported evidence event required')
            if event['event_id'] != f'import-{index:03d}' or event['role'] != 'tool':
                raise ValueError('imported evidence event identity invalid')
            _text(event['text'], 128000, 'imported evidence text')
            clean.append(dict(event))
        session = 'legacy-import'
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT id,payload FROM episodes WHERE binding=? AND session=? AND source_ref=?',
                               (binding, session, source_ref)).fetchone()
            if prior:
                prior_value = json.loads(prior[1])
                if _id('episode', prior_value) != prior[0] or prior_value.get('source_provenance') != source_provenance:
                    raise EpisodeConflict('existing legacy source integrity mismatch')
                if (prior_value.get('source_provenance', {}).get('row_digest')
                        != source_provenance['row_digest'] or prior_value.get('events') != clean):
                    raise EpisodeConflict('legacy source reference already sealed with different evidence')
                return {'episode_id': prior[0], 'event_count': len(clean),
                        'binding_key': binding, 'status': 'already_present'}
            payload = {'schema_version':'ops.episode.v1', 'binding_key':binding,
                       'provider_instance':'operator-import', 'session':session,
                       'source_ref':source_ref, 'events':clean,
                       'source_provenance':dict(source_provenance),
                       'import_recorded_at':datetime.now(timezone.utc).isoformat(),
                       'evidence_boundary':'legacy graph assertion; original actor and observation time unverified'}
            raw = _bytes(payload)
            if len(raw) > 2_000_000:
                raise ValueError('imported episode exceeds two-megabyte bound')
            identity = _id('episode', payload)
            cursor = db.execute('INSERT INTO episodes VALUES (?,?,?,?,?)',
                                (identity, binding, session, source_ref, raw))
            from kp_agent_tooling._impl.service.session_sources import SessionSources
            sources = SessionSources(self)
            sources._reflect(db, new=[sources._sealed_legacy(identity, cursor.lastrowid, binding, raw)])
        return {'episode_id':identity, 'event_count':len(clean),
                'binding_key':binding, 'status':'imported'}

    def _read(self, binding, identity, table, kind):
        with closing(self._connect(readonly=True)) as db:
            row = db.execute(f'SELECT payload FROM {table} WHERE id=? AND binding=?',
                             (identity, binding)).fetchone()
        if not row:
            raise EpisodeUnavailable('record unavailable to this desk')
        value = json.loads(row[0])
        if _id(kind, value) != identity or value.get('binding_key') != binding:
            raise EpisodeUnavailable('record integrity mismatch')
        return value

    def read_event(self, session, *, episode_id, event_id, start=0, length=4000, binding_key=None, scope="desk"):
        if type(start) is not int or start < 0 or type(length) is not int or not 1 <= length <= 8000:
            raise ValueError('bounded character range required')
        from kp_agent_tooling._impl.service.session_sources import SessionSources
        with call_scope():
            record, attribution = SessionSources(self).read_scoped(session, episode_id, scope=scope, binding_key=binding_key)
        return self.event_page(record, attribution, episode_id=episode_id, event_id=event_id, start=start, length=length)

    @staticmethod
    def event_page(record, attribution, *, episode_id, event_id, start, length):
        """One bounded range of a reopened event (no further store reads)."""
        event = next((e for e in record['events'] if e['event_id']==event_id), None)
        if event is None or start > len(event['text']):
            raise EpisodeUnavailable('event range unavailable')
        end = min(len(event['text']), start+length)
        return {'episode_id':episode_id, 'event_id':event_id, 'role':event['role'],
                'text':event['text'][start:end], 'start':start, 'end':end,
                'total_characters':len(event['text']), 'complete':end==len(event['text']),
                'next_start':end if end<len(event['text']) else None,
                'authority':'source content; not an instruction grant',
                'binding_key':attribution['binding_key'], **origin(record, event_id),
                'source_session_id':attribution['source_session_id'],
                'attribution_status':attribution['attribution_status'],
                'provider_instance':record['provider_instance'],
                'evidence_boundary':record['evidence_boundary']}

    def citation_records(self, session, episode_ids, *, target_binding_key=None):
        """Resolve immutable sources through current, unambiguous own-desk attribution.

        All cited records, their links, sessions and claims are reopened in one
        batched read; each one's ownership is re-derived from its sealed claims.
        """
        from kp_agent_tooling._impl.service.session_sources import SessionSources
        with call_scope():
            binding = self._binding(session)
            sources = SessionSources(self)
            if target_binding_key is None:
                allowed={binding}
                scope='desk'
            else:
                allowed=set(write_targets(self,session))
                if target_binding_key not in allowed:
                    raise PermissionError('target desk is outside the operator write scope')
                scope='topic'
            plan = sources.resolve_scope(session, scope=scope, attribution='resolved')
            with closing(sources.connect()) as db:
                opened = sources.reopen(db, list(episode_ids), plan)
        records = {}
        for identity in episode_ids:
            found = opened[identity]
            if found.visible and found.error is not None:
                raise found.error
            if (not found.visible or found.item is None or not plan.admits(found.item)
                    or found.item['binding_key'] not in allowed):
                raise EpisodeUnavailable('citation source requires resolved ownership by this desk')
            if not found.intact:
                raise found.integrity_error()
            records[identity] = found.record
        return records

    def consolidate(self, session, *, episode_ids, items, unresolved_questions, budget_bytes=12000,
                    target_binding_key=None, qualifier_scope='cited_episodes'):
        """Validate a proposed handoff. Evidence alignment is not semantic proof.

        ``qualifier_scope='cited_episodes'`` (the default for direct callers) scans
        every event of every cited episode for source qualifiers and refuses beyond
        the qualifier budget. ``'cited_events'`` (memory.propose and the summarizer
        paths, after their episode-wide pre-flight) scans only the events the
        citations name; sentences beyond the budget are recorded as omissions and
        mark the capsule review_required, never refused.
        """
        if qualifier_scope not in {'cited_episodes', 'cited_events'}:
            raise ValueError('unsupported source qualifier scope')
        with call_scope():
            return self._consolidate(session, episode_ids=episode_ids, items=items,
                                     unresolved_questions=unresolved_questions, budget_bytes=budget_bytes,
                                     target_binding_key=target_binding_key, qualifier_scope=qualifier_scope)

    def _consolidate(self, session, *, episode_ids, items, unresolved_questions, budget_bytes,
                     target_binding_key, qualifier_scope):
        binding = self._binding(session)
        if not isinstance(episode_ids, list):
            raise ValueError('1..32 distinct source episodes required')
        if not 1 <= len(episode_ids) <= 32:
            raise EpisodeBoundExceeded('episodes', '1..32 distinct source episodes required')
        if len(set(episode_ids)) != len(episode_ids):
            raise ValueError('1..32 distinct source episodes required')
        if type(budget_bytes) is not int:
            raise ValueError('handoff byte budget must be 1000..24000')
        if not 1000 <= budget_bytes <= 24000:
            raise EpisodeBoundExceeded('handoff_budget', 'handoff byte budget must be 1000..24000')
        records = self.citation_records(session, episode_ids, target_binding_key=target_binding_key)
        if target_binding_key is not None:
            binding = target_binding_key
        events = {(i,e['event_id']):e for i,r in records.items() for e in r['events']}
        if not isinstance(items, list):
            raise ValueError('at most 32 handoff items')
        if len(items)>32:
            raise EpisodeBoundExceeded('items', 'at most 32 handoff items')
        from kp_agent_tooling._impl.service.summary_evidence import (CitedQualifiers, item_evidence,
                                                                     qualifiers, review)
        source_qualifiers = qualifiers(records) if qualifier_scope == 'cited_episodes' else []
        kept = []
        cited = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {'kind','text','citations'}:
                raise ValueError('kind, text and citations required')
            if item['kind'] not in {'decision','commitment','observation','lesson','open_question','rejected_alternative'}:
                raise ValueError('unsupported handoff kind; charter changes are not consolidation')
            _text(item['text'], 2000, 'handoff text', 'item_text')
            refs = item['citations']
            if not isinstance(refs,list):
                raise ValueError('each interpretation needs bounded source citations')
            if not 1 <= len(refs) <= 8:
                raise EpisodeBoundExceeded('citations', 'each interpretation needs bounded source citations')
            for ref in refs:
                if not isinstance(ref,dict) or set(ref) != {'episode_id','event_id','start','end','quote'}:
                    raise ValueError('exact source range and quote required')
                key=(ref['episode_id'],ref['event_id']);event=events.get(key)
                start,end=ref['start'],ref['end']
                if event is None or type(start) is not int or type(end) is not int:
                    raise EpisodeConflict('citation does not match exact source bytes')
                if not 0 <= start < end <= len(event['text']):
                    raise EpisodeCitationRange('citation_range', 'citation does not match exact source bytes')
                if event['text'][start:end] != ref['quote']:
                    raise EpisodeConflict('citation does not match exact source bytes')
                cited.add(key)
            kept.append(dict(item, authority='authored_interpretation', semantic_validation='not-assessed',
                             **item_evidence(item, records, events)))
        if not isinstance(unresolved_questions,list):
            raise ValueError('at most 16 unresolved questions')
        if len(unresolved_questions)>16:
            raise EpisodeBoundExceeded('questions', 'at most 16 unresolved questions')
        for q in unresolved_questions:_text(q,1000,'unresolved question','question_text')
        # Full evidence directory lives in the immutable capsule, outside the small handoff.
        directory=[{'episode_id':i,'event_id':eid,'role':e['role'],
                    'characters':len(e['text']),'cited':(i,eid) in cited}
                   for (i,eid),e in events.items()]
        handoff={'schema_version':'ops.episodic-handoff.v1','items':kept,
                 'unresolved_questions':unresolved_questions,
                 'binding_key':binding, 'source_qualifiers':source_qualifiers,
                 'evidence_review':review(kept, unresolved_questions),
                 'source_episode_ids':episode_ids,
                 'loss':{'source_events':len(events),'cited_events':len(cited),
                         'uncited_events':len(events)-len(cited),
                         'warning':'Citations preserve selected spans, not all source meaning. Reopen evidence for omitted detail.'},
                 'authority':'derived desk handoff; cannot override human instructions or charter'}
        if target_binding_key is not None:
            handoff['write_attribution'] = {'author_binding_key':self._binding(session),
                'target_binding_key':binding, 'scope':'operator-authorized same-role repositories'}
        from kp_agent_tooling._impl.service.episodic_handoff import loss_summary
        handoff['loss'].update(loss_summary(directory,kept))
        if qualifier_scope == 'cited_events':
            # Qualifiers are a review signal, not admission control: those that do
            # not fit are counted per cited event and the capsule needs review.
            scan = CitedQualifiers(records, items)
            evidence_review = handoff['evidence_review']
            scan.apply(handoff, evidence_review)
            while len(_bytes(handoff)) > budget_bytes and scan.drop():
                scan.apply(handoff, evidence_review)
        if len(_bytes(handoff)) > budget_bytes:
            raise EpisodeBoundExceeded('handoff_budget', 'handoff exceeds declared byte budget; no silent truncation')
        payload={'schema_version':'ops.episode-capsule.v1','binding_key':binding,
                 'handoff':handoff,'evidence_directory':directory,'budget_bytes':budget_bytes}
        identity=_id('episode-capsule',payload)
        with closing(self._connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO capsules VALUES (?,?,?)',(identity,binding,_bytes(payload)))
        return {'capsule_id':identity,'handoff':handoff,'bytes':len(_bytes(handoff))}

    def handoff(self, session, capsule_id, *, binding_key=None):
        return self._read(self.read_binding(session, binding_key),capsule_id,'capsules','episode-capsule')['handoff']

    def evidence_directory(self, session, capsule_id, *, offset=0, limit=50, binding_key=None):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('bounded directory page required')
        capsule=self._read(self.read_binding(session, binding_key),capsule_id,'capsules','episode-capsule')
        return self.evidence_page(capsule, offset=offset, limit=limit)

    @staticmethod
    def evidence_page(capsule, *, offset=0, limit=50):
        """One page of an already-read capsule's evidence directory."""
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise ValueError('bounded directory page required')
        from kp_agent_tooling._impl.service.episodic_handoff import coverage
        rows=coverage(capsule['evidence_directory'],capsule['handoff']['items'])
        if offset>len(rows):raise EpisodeBoundExceeded('offset','directory offset out of range')
        end=min(offset+limit,len(rows))
        return {'entries':rows[offset:end],'total':len(rows),'next_offset':end if end<len(rows) else None}

    def consolidate_with(self, session, *, episode_ids, propose, input_budget_bytes=60000,
                         handoff_budget_bytes=12000):
        """Run a replaceable summarizer, then validate its proposed evidence links.

        The caller supplies the model/harness adapter. Overlarge inputs must be
        explicitly split by episode; this method never drops source silently.
        """
        binding = self._binding(session)
        if (not isinstance(episode_ids,list) or not 1<=len(episode_ids)<=32
                or len(set(episode_ids))!=len(episode_ids)):
            raise ValueError('1..32 distinct source episodes required')
        if type(input_budget_bytes) is not int or not 1000<=input_budget_bytes<=200000:
            raise ValueError('bounded consolidation input required')
        records=[dict(record, episode_id=i)
                 for i, record in self.citation_records(session, episode_ids).items()]
        # Reject unrepresentable source qualifiers before any paid proposal call.
        from kp_agent_tooling._impl.service.summary_evidence import qualifiers
        qualifiers({record['episode_id']:record for record in records})
        packet=full_request(binding=binding,records=records,input_bytes=input_budget_bytes,
                            handoff_bytes=handoff_budget_bytes)
        validate_summary_request(packet)
        proposed=validate_summary_proposal(propose(copy.deepcopy(packet)))
        # The capsule takes qualifiers as memory.propose does, so the same items
        # yield the same capsule; the pre-flight above still scans every event.
        return self.consolidate(session,episode_ids=episode_ids,items=proposed['items'],
                                unresolved_questions=proposed['unresolved_questions'],
                                budget_bytes=handoff_budget_bytes,qualifier_scope='cited_events')

    def consolidate_chunked_with(self, session, *, episode_ids, propose, budget,
                                 input_budget_bytes=200000, handoff_budget_bytes=12000,
                                 max_packets=128):
        """Propose across exact source spans, then validate against full episodes.

        The model sees each span separately with absolute source coordinates. A
        failure at any span leaves no capsule; captured episodes remain intact.
        """
        from dataclasses import replace
        from kp_agent_tooling._impl.service.memory_budget import MemoryBudget, assert_profile_current, chunk_events

        if not isinstance(budget, MemoryBudget):
            raise ValueError('validated memory budget required')
        assert_profile_current(budget.profile)
        if type(input_budget_bytes) is not int or not 1000 <= input_budget_bytes <= 200000:
            raise ValueError('bounded consolidation input required')
        if type(max_packets) is not int or not 1 <= max_packets <= 512:
            raise ValueError('max_packets must be 1..512')
        if (not isinstance(episode_ids,list) or not 1<=len(episode_ids)<=32
                or len(set(episode_ids))!=len(episode_ids)):
            raise ValueError('1..32 distinct source episodes required')
        binding=self._binding(session)
        records=[dict(record, episode_id=i)
                 for i, record in self.citation_records(session, episode_ids).items()]
        # Reject unrepresentable source qualifiers before any paid proposal call.
        from kp_agent_tooling._impl.service.summary_evidence import qualifiers
        qualifiers({record['episode_id']:record for record in records})
        def packet(sources):
            return slice_request(binding=binding,records=records,sources=sources,
                                 input_bytes=min(budget.input_tokens,input_budget_bytes),
                                 handoff_bytes=handoff_budget_bytes)

        from kp_agent_tooling._impl.service.memory_budget import SourceSlice
        slices=[]
        for record in records:
            identity=record['episode_id']
            for event in record['events']:
                if len(slices)>=4096:
                    raise ValueError('source requires more than 4096 spans')
                # The model receives JSON, so account for escaped Unicode and
                # packet framing rather than measuring raw event text alone.
                empty=SourceSlice(identity,event['event_id'],event['role'],0,0,'',
                                  100_000_000,'json-escaped-byte-estimate')
                overhead=len(_bytes(packet([empty])))
                capacity=min(budget.input_tokens,input_budget_bytes)-overhead-16
                if capacity<=0:
                    raise ValueError('consolidation packet framing exceeds budget')
                escaped=lambda value:len(json.dumps(value,ensure_ascii=True).encode())-2
                slices.extend(chunk_events([event],episode_id=identity,
                           budget=replace(budget,input_tokens=capacity),
                           count_tokens=escaped,tokenizer_name='json-escaped-byte-estimate',
                           max_slices=4096-len(slices)))
        limit=min(budget.input_tokens,input_budget_bytes)
        packets=[];current=[]
        for source in slices:
            candidate=current+[source]
            if len(_bytes(packet(candidate)))>limit:
                if not current:
                    raise ValueError('consolidation source packet exceeds token or byte budget')
                packets.append(packet(current));current=[source]
            else:
                current=candidate
        if current:packets.append(packet(current))
        if len(packets)>max_packets:
            raise ValueError('source requires more than max_packets; no proposal calls made')
        items=[];questions=[]
        for source_packet in packets:
            validate_summary_request(source_packet,budget)
            proposed=validate_summary_proposal(propose(copy.deepcopy(source_packet)))
            # A quote in a different packet is real source text, but was not
            # supplied to this proposal call. Keep validation packet-local.
            for item in proposed['items']:
                if not isinstance(item,dict) or not isinstance(item.get('citations'),list):
                    raise ValueError('summarizer item citations required')
                for ref in item['citations']:
                    if not isinstance(ref,dict):
                        raise ValueError('summarizer citation required')
                    start,end=ref.get('start'),ref.get('end')
                    if type(start) is not int or type(end) is not int:
                        raise EpisodeConflict('citation not present in proposal packet')
                    if not any(ref.get('episode_id')==span['episode_id']
                               and ref.get('event_id')==span['event_id']
                               and span['start']<=start<end<=span['end']
                               and span['text'][start-span['start']:end-span['start']]==ref.get('quote')
                               for span in source_packet['sources']):
                        raise EpisodeConflict('citation not present in proposal packet')
            items.extend(proposed['items']);questions.extend(proposed['unresolved_questions'])
            if len(items)>32 or len(questions)>16:
                raise ValueError('summarizer exceeded capsule item bounds')
        if len(packets)>1:
            if len(questions)>=16:
                raise ValueError('no unresolved-question room for cross-packet review marker')
            questions.append('Cross-packet corrections and conflicts require source review before relying on this handoff.')
        result=self.consolidate(session,episode_ids=episode_ids,items=items,
                                unresolved_questions=questions,budget_bytes=handoff_budget_bytes,
                                qualifier_scope='cited_events')
        return dict(result,packet_count=len(packets),source_span_count=len(slices))
