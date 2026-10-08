"""Durable, desk-scoped consolidation of sealed incremental episodes.

Leases recover only before a proposal starts. An expired/incomplete external attempt
requires review, never automatic retransmission. The queue owns no harness lifecycle.

A refusal before the network (S1a P2: the model gateway refused before sending, so no
request can have reached a provider) is not an uncertain attempt: the attempt is recorded
``not_sent`` and the job is released to ``queued``, claimable again on a later run. Only a
request that may have reached the provider goes to review.
"""
from contextlib import closing
import json
import math
import sqlite3
import time

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.summary_contract import request_bytes


class ConsolidationConflict(ValueError):
    pass


# The attempt state of a request refused before the network (S1a P2).
NOT_SENT = 'not_sent'
# The refusals a proposer may report as not sent: the model gateway's pre-network vocabulary.
NOT_SENT_CATEGORIES = frozenset({
    'budget_exceeded', 'confirmation_required', 'secret_refused', 'budget_unconfigured',
    'budget_unpriced', 'configuration_invalid', 'unknown_capability', 'unsupported_modality',
    'route_params_refused', 'privacy_routing_required', 'model_pin_mismatch', 'invalid_request',
    'connection_not_established', 'internal_error'})


class RequestNotSent(RuntimeError):
    """A proposer's request was refused before the network: nothing can have reached a provider.

    ``category`` is one word of NOT_SENT_CATEGORIES (anything else reads ``refused``), never
    provider text. The queue records the attempt ``not_sent`` and releases the job.
    """

    def __init__(self, category):
        self.category = category if category in NOT_SENT_CATEGORIES else 'refused'
        super().__init__('request refused before the network: ' + self.category)


def _json(value):
    return leaf.canonical_json(value, ascii=True, allow_nan=True)


def _digest(value):
    return leaf.canonical_sha256(value, ascii=True, allow_nan=True)


def _name(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 512:
        raise ValueError('bounded identifier required')
    return value


class ConsolidationQueue:
    def __init__(self, path, *, store, clock=time.time):
        self.path, self.store, self.clock = leaf.as_path(path), store, clock

    def initialize(self):
        # Exclusive reservation avoids silently recreating a lost ledger in a worker.
        leaf.touch_new_private(self.path)
        with closing(leaf.sqlite_connect(self.path, mode='rwc', resolve=True)) as db, db:
            db.executescript("""
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY, binding TEXT NOT NULL, episodes TEXT NOT NULL,
                    reason TEXT NOT NULL, state TEXT NOT NULL, worker TEXT, generation INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL, capsule_id TEXT, error TEXT, created REAL NOT NULL);
                CREATE TABLE sources (
                    binding TEXT NOT NULL, episode TEXT NOT NULL, job TEXT NOT NULL,
                    PRIMARY KEY(binding,episode));
                CREATE TABLE attempts (
                    job TEXT NOT NULL, ordinal INTEGER NOT NULL, packet_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL, receipt TEXT, PRIMARY KEY(job,ordinal));
                PRAGMA user_version=1;
            """)

    def _db(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise ConsolidationConflict('queue unavailable; initialize explicitly')
        db = leaf.sqlite_connect(self.path, mode='rw', resolve=True)
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA user_version').fetchone()[0] != 1:
            db.close()
            raise ConsolidationConflict('queue schema unavailable')
        return db

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError('finite clock required')
        return now

    def enqueue(self, session, *, episode_ids, reason='batch'):
        binding = self.store._binding(session)
        if (not isinstance(episode_ids,list) or not 1 <= len(episode_ids) <= 32
                or any(not isinstance(i,str) for i in episode_ids)
                or len(set(episode_ids)) != len(episode_ids)):
            raise ValueError('1..32 distinct sealed episodes required')
        if reason not in {'batch','context_threshold','session_end','manual'}:
            raise ValueError('explicit consolidation trigger required')
        self.store.citation_records(session, episode_ids)
        identity = 'consolidation:'+_digest({'binding':binding,'episodes':episode_ids})
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM jobs WHERE id=?',(identity,)).fetchone()
            if old is None:
                if any(db.execute('SELECT 1 FROM sources WHERE binding=? AND episode=?',
                                  (binding,ep)).fetchone() for ep in episode_ids):
                    raise ConsolidationConflict('episode already assigned; enqueue only new sealed episodes')
                db.execute('INSERT INTO jobs(id,binding,episodes,reason,state,created) VALUES (?,?,?,?,?,?)',
                           (identity,binding,_json(episode_ids),reason,'queued',self._now()))
                db.executemany('INSERT INTO sources VALUES (?,?,?)',
                               [(binding,ep,identity) for ep in episode_ids])
        return self.get(session,identity)

    def get(self, session, identity):
        binding = self.store._binding(session)
        with closing(self._db()) as db:
            row = db.execute('SELECT * FROM jobs WHERE id=? AND binding=?',(identity,binding)).fetchone()
            if row is None:
                raise ConsolidationConflict('job unavailable to desk')
            attempts = db.execute('SELECT ordinal,packet_sha256,state,receipt FROM attempts WHERE job=? ORDER BY ordinal',
                                  (identity,)).fetchall()
        return {'job_id':identity,'state':row['state'],'episode_ids':json.loads(row['episodes']),
                'reason':row['reason'],'capsule_id':row['capsule_id'],'error':row['error'],
                'attempts':[dict(ordinal=r['ordinal'],packet_sha256=r['packet_sha256'],
                                 state=r['state'],receipt=json.loads(r['receipt']) if r['receipt'] else None)
                            for r in attempts]}

    def list(self, session, *, limit=20):
        binding = self.store._binding(session)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('bounded queue page required')
        with closing(self._db()) as db:
            rows = db.execute('SELECT id,state,reason,capsule_id,error FROM jobs WHERE binding=? ORDER BY created DESC,id LIMIT ?',
                              (binding,limit)).fetchall()
        return {'entries':[dict(row) for row in rows],'limit':limit}

    def metrics(self, session):
        binding = self.store._binding(session)
        with closing(self._db()) as db:
            rows = db.execute('SELECT state,count(*) AS count FROM jobs WHERE binding=? GROUP BY state',
                              (binding,)).fetchall()
            oldest = db.execute("SELECT min(created) FROM jobs WHERE binding=? AND state IN ('queued','leased','running')",
                                (binding,)).fetchone()[0]
        return {'counts':{row['state']:row['count'] for row in rows},
                'oldest_unfinished_age_seconds':None if oldest is None else max(0,self._now()-oldest)}

    def claim(self, session, worker, *, lease_seconds=300):
        binding = self.store._binding(session)
        _name(worker)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError('bounded lease required')
        now = self._now()
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            # A network call may have completed remotely even if its receipt is absent.
            db.execute("""UPDATE jobs SET state='needs_review',error='expired_after_proposal'
                          WHERE binding=? AND state='running' AND lease_until<=?""",(binding,now))
            row = db.execute("""SELECT * FROM jobs WHERE binding=? AND
                    (state='queued' OR (state='leased' AND lease_until<=?)) ORDER BY created,id LIMIT 1""",
                             (binding,now)).fetchone()
            if row is None:
                return None
            # A job released after a refusal before the network keeps its not_sent attempts;
            # this run's attempts continue their ordinals (S1a P2).
            recorded = db.execute('SELECT coalesce(max(ordinal),0) FROM attempts WHERE job=?',
                                  (row['id'],)).fetchone()[0]
            db.execute("""UPDATE jobs SET state='leased',worker=?,generation=generation+1,lease_until=?
                          WHERE id=?""",(worker,now+lease_seconds,row['id']))
            return {'job_id':row['id'],'worker':worker,'generation':row['generation']+1,
                    'episode_ids':json.loads(row['episodes']),'binding':binding,
                    'lease_seconds':lease_seconds,'recorded_attempts':recorded}

    def _fence(self, db, lease, session):
        binding = self.store._binding(session)
        row = db.execute('SELECT * FROM jobs WHERE id=?',(lease['job_id'],)).fetchone()
        if (row is None or row['binding'] != binding or row['worker'] != lease['worker']
                or row['generation'] != lease['generation'] or row['state'] not in {'leased','running'}
                or row['lease_until'] <= self._now()):
            raise ConsolidationConflict('consolidation lease lost or expired')
        return row

    def _intent(self, session, lease, ordinal, packet):
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._fence(db,lease,session)
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,NULL)',
                       (lease['job_id'],ordinal,_digest(packet),'attempting'))
            db.execute("UPDATE jobs SET state='running',lease_until=? WHERE id=?",
                       (self._now()+lease['lease_seconds'],lease['job_id']))

    def _receipt(self, session, lease, ordinal, state, receipt):
        # Only declared non-content fields from the existing adapter are retained.
        allowed = {'schema_version','model_requested','profile_sha256','request_sha256','input_bytes',
                   'token_measurement','started_at','status','retention','zdr_required','usage',
                   'semantic_validation','citation_validation','generation_id','model_reported',
                   'provider_reported','failure_category','http_status','finish_reason','elapsed_ms',
                   'allow_fallbacks','data_collection','prompt_version','prompt_sha256','reasoning_policy','response_format','message_content_bytes',
                   'gateway_call_id','request_target'}
        safe = {k:v for k,v in receipt.items() if k in allowed} if isinstance(receipt,dict) else {}
        if safe.get('data_collection') not in ('allow', 'deny'):
            safe.pop('data_collection', None)
        if isinstance(receipt,dict) and 'transport' in receipt:
            from kp_agent_tooling._impl.service.episodic_summarizer import safe_transport_diagnostics
            safe['transport']=safe_transport_diagnostics(receipt['transport'])
        if request_bytes(safe) > 4096:
            raise ConsolidationConflict('receipt exceeds bound')
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._fence(db,lease,session)
            count = db.execute("""UPDATE attempts SET state=?,receipt=? WHERE job=? AND ordinal=? AND state='attempting'""",
                               (state,_json(safe),lease['job_id'],ordinal)).rowcount
            if count != 1:
                raise ConsolidationConflict('attempt already resolved')

    def _finish(self, session, lease, *, capsule_id=None, error=None):
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._fence(db,lease,session)
            db.execute('UPDATE jobs SET state=?,capsule_id=?,error=?,lease_until=NULL WHERE id=?',
                       ('succeeded' if capsule_id else 'needs_review',capsule_id,error,lease['job_id']))
            if capsule_id:
                db.execute("UPDATE attempts SET state='citations_validated' WHERE job=? AND state='proposal_received'",
                           (lease['job_id'],))

    def _release(self, session, lease, *, error):
        """A refusal before the network (S1a P2): the job is queued again; nothing was sent."""
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._fence(db,lease,session)
            db.execute("UPDATE jobs SET state='queued',worker=NULL,lease_until=NULL,error=? WHERE id=?",
                       (error,lease['job_id']))

    def run_once(self, session, worker, *, propose, budget, approve_sources,
                 max_proposal_calls=4, lease_seconds=300):
        """One bounded job; operator supplies source policy and provider adapter.

        approve_sources receives canonical visible-event digests, never source text.
        Return exactly True to permit the selected episodes; no network access occurs
        before this decision. This does not install a policy or start a background daemon.
        """
        if not callable(approve_sources) or not callable(propose):
            raise ValueError('operator source policy and proposer required')
        if type(max_proposal_calls) is not int or not 1 <= max_proposal_calls <= 32:
            raise ValueError('bounded proposal limit required')
        lease = self.claim(session,worker,lease_seconds=lease_seconds)
        if lease is None:
            return None
        ordinal = lease.get('recorded_attempts',0)
        calls = 0
        sent = []  # this run's attempts that may have reached the provider
        def recorded(packet):
            nonlocal ordinal, calls
            calls += 1
            if calls > max_proposal_calls:
                raise ConsolidationConflict('proposal cap exceeded')
            ordinal += 1
            self._intent(session,lease,ordinal,packet)
            if hasattr(propose,'last_receipt'):
                propose.last_receipt = None
            try:
                result = propose(packet)
            except RequestNotSent:
                self._receipt(session,lease,ordinal,NOT_SENT,getattr(propose,'last_receipt',None))
                raise
            except Exception:
                sent.append(ordinal)
                self._receipt(session,lease,ordinal,'failed_or_uncertain',getattr(propose,'last_receipt',None))
                raise
            sent.append(ordinal)
            self._receipt(session,lease,ordinal,'proposal_received',getattr(propose,'last_receipt',None))
            return result
        try:
            digests = [_digest(record['events']) for record in
                       self.store.citation_records(session, lease['episode_ids']).values()]
            if approve_sources(tuple(digests)) is not True:
                raise ConsolidationConflict('source policy denied')
            capsule = self.store.consolidate_chunked_with(session,episode_ids=lease['episode_ids'],
                        propose=recorded,budget=budget,max_packets=max_proposal_calls)
            self._finish(session,lease,capsule_id=capsule['capsule_id'])
        except RequestNotSent as refusal:
            # Nothing of this request reached a provider. With no earlier packet of this job sent
            # in this run, the job is claimable again; after one was, resending it would retransmit
            # source the provider already received, so the job goes to review.
            try:
                if sent:
                    self._finish(session,lease,error=type(refusal).__name__)
                else:
                    self._release(session,lease,error=NOT_SENT+':'+refusal.category)
            except ConsolidationConflict:
                pass
            raise
        except Exception as error:
            try:
                self._finish(session,lease,error=type(error).__name__)
            except ConsolidationConflict:
                # Another claimant will move an expired running job to review.
                pass
            raise
        return self.get(session,lease['job_id'])
