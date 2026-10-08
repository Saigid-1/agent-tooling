"""Local durable document withdrawals, independent of catalog revision/rollback."""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
from pathlib import Path


from kp_agent_tooling._impl import leaf
from kp_agent_tooling_ops._impl.document_identity import document_identity as _identity

HELD_GUARDS = ContextVar('knowledge_lifecycle_guards', default=frozenset())


class LifecycleLedger:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.lock_path = self.path.with_suffix(self.path.suffix+'.lock')

    @classmethod
    def initialize(cls, path):
        ledger = cls(path)
        ledger.path.parent.mkdir(parents=True, exist_ok=True)
        # Only initialization creates state. Reads must never recreate a lost ledger.
        if ledger.lock_path.exists() and not ledger.path.exists():
            raise ValueError('lost lifecycle state requires recovery, not initialization')
        with leaf.private_lock(ledger.lock_path):
            if ledger.path.exists():
                ledger.entries()
                return ledger
            if not ledger.path.exists():
                leaf.create_new_empty(ledger.path, access='rw')
            db = leaf.sqlite_connect(ledger.path, mode='rwc', resolve=True)
            try:
                db.execute('CREATE TABLE IF NOT EXISTS withdrawals (repo_key TEXT NOT NULL, path TEXT NOT NULL, blob_sha TEXT NOT NULL, reason TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(repo_key,path,blob_sha))')
                db.commit()
            finally:
                db.close()
        return ledger

    @contextmanager
    def guard(self, *, exclusive=False):
        key = str(self.path)
        if key in HELD_GUARDS.get():
            if exclusive:
                raise ValueError('cannot withdraw inside an active read response')
            yield
            return
        with self.lock_path.open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            self.entries()
            token = HELD_GUARDS.set(HELD_GUARDS.get() | {key})
            try:
                yield
            finally:
                HELD_GUARDS.reset(token)

    def entries(self):
        db = leaf.sqlite_connect(self.path, mode='ro', resolve=True)
        try:
            return tuple(db.execute('SELECT repo_key,path,blob_sha,reason FROM withdrawals ORDER BY repo_key,path,blob_sha'))
        finally:
            db.close()

    def permits(self, repo_key, path, blob_sha):
        return not any(repo == repo_key and p == path and (not blob or blob == blob_sha)
                       for repo,p,blob,_ in self.entries())

    def _record(self, repo_key, path, blob_sha, reason):
        identity = _identity(repo_key,path,blob_sha)
        if reason not in {'withdrawn','superseded','historical','source_removed'}:
            raise ValueError('unsupported withdrawal reason')
        db = leaf.sqlite_connect(self.path, mode='rw', resolve=True)
        try:
            with db:
                db.execute('INSERT OR IGNORE INTO withdrawals(repo_key,path,blob_sha,reason) VALUES(?,?,?,?)', (*identity,reason))
        finally:
            db.close()

    def withdraw(self, repo_key, path, *, blob_sha=None, reason='withdrawn'):
        with self.guard(exclusive=True):
            self._record(repo_key,path,blob_sha,reason)

    def reconcile(self, store_factory):
        with self.guard(exclusive=True):
            return self._purge(store_factory)

    def _purge(self, store_factory):
        entries = self.entries()
        if not entries:
            return 0
        store = store_factory()
        return sum(store.remove_documents(repo_key=repo,path=path,blob_sha=blob or None)
                   for repo,path,blob,_ in entries)


def withdraw_documents(ledger, store_factory, *, repo_key, path, blob_sha=None, reason='withdrawn'):
    with ledger.guard(exclusive=True):
        ledger._record(repo_key,path,blob_sha,reason)
        # Durable intent commits before any fallible backend construction or deletion.
        return ledger._purge(store_factory)


def reconcile_catalog(ledger, store_factory, repository):
    """Explicit operator reconciliation; catalog absence cannot leave indexed orphans."""
    from kp_agent_tooling_ops._impl.tool_discovery import GitSource
    from kp_agent_tooling_ops._impl.service.knowledge_eligibility import eligible_paths
    source = GitSource(Path(repository['path']), repository['ref'])
    active = {}
    for path in eligible_paths(repository):
        entry = source.entry(path)
        if entry is None or entry[0] not in {'100644','100755'}:
            raise ValueError('maintained source unresolved; catalog review required')
        active[path] = entry[1]
    scope = repository['corpus_scope']
    with ledger.guard(exclusive=True):
        store = store_factory()
        sources = store.document_sources(repo_key=scope)
        plan = []
        for path, blob in sources:
            _identity(scope,path,blob)
            if path not in active:
                plan.append((path,None,'source_removed'))
            elif active[path] != blob:
                plan.append((path,blob,'superseded'))
        for path,blob,reason in plan:
            ledger._record(scope,path,blob,reason)
        removed = ledger._purge(lambda:store)
        return {'status':'reconciled','source_revision':source.revision,
                'removed_vectors':removed,'withdrawal_rules':len(ledger.entries())}
