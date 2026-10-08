"""Durable withdrawal must outlive a catalog rollback and failed index cleanup."""
import json
from dataclasses import replace
import pytest
from test_knowledge_service import setup, Reader
from test_knowledge_context import case, commit, git


def test_withdrawal_survives_restart_and_maintained_catalog(case, tmp_path):
    from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger
    from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService
    path = tmp_path / 'lifecycle.sqlite3'
    ledger = LifecycleLedger.initialize(path)
    ledger.withdraw('docs', 'docs/guide.md', reason='superseded')
    service = KnowledgeService(case[1], lambda: Reader([case[4]]), lifecycle=LifecycleLedger(path))
    result = service.execute('retrieve', {'repo_key':'repo','query':'guidance'})
    assert result['status'] == 'corpus_empty'
    assert result['data']['reason'] == 'all_declared_artifacts_withdrawn'
    assert case[4].text not in json.dumps(result)
    assert len(ledger.entries()) == 1
    ledger.withdraw('docs','docs/guide.md',reason='superseded')
    assert len(ledger.entries()) == 1


def test_blob_withdrawal_allows_new_revision_only(case, tmp_path):
    from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger
    from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService
    ledger = LifecycleLedger.initialize(tmp_path/'state.db')
    ledger.withdraw('docs','docs/guide.md',blob_sha=case[4].blob_sha,reason='superseded')
    repo = case[0]
    (repo / 'docs/guide.md').write_text('replacement')
    commit(repo, 'replacement guide')
    new = replace(case[4], blob_sha=git(repo, 'rev-parse', 'HEAD:docs/guide.md'), text='replacement')
    service = KnowledgeService(case[1],lambda:Reader([case[4],new]),lifecycle=ledger)
    result = service.execute('retrieve',{'repo_key':'repo','query':'guidance'})
    assert [row['text'] for row in result['data']['results']] == ['replacement']


def test_missing_ledger_fails_closed(case, tmp_path):
    from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger
    from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService
    ledger = LifecycleLedger.initialize(tmp_path/'state.db')
    service = KnowledgeService(case[1], lambda:Reader([case[4]]), lifecycle=ledger)
    ledger.path.unlink()
    result = service.execute('retrieve',{'repo_key':'repo','query':'guidance'})
    assert result['status'] == 'error'
    assert case[4].text not in json.dumps(result)


def test_durable_intent_precedes_cleanup_failure_and_retry(tmp_path):
    from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger, withdraw_documents
    ledger=LifecycleLedger.initialize(tmp_path/'state.db')
    def unavailable(): raise RuntimeError('backend offline')
    with pytest.raises(RuntimeError):
        withdraw_documents(ledger, unavailable, repo_key='docs',path='docs/old.md',reason='withdrawn')
    assert not ledger.permits('docs','docs/old.md','a'*40)
    class Store:
        def remove_documents(self, **kwargs): self.called=kwargs;return 3
    store=Store()
    assert ledger.reconcile(lambda:store)==3
    assert store.called=={'repo_key':'docs','path':'docs/old.md','blob_sha':None}


def test_memory_store_removes_only_matching_document_rows():
    from kp_agent_tooling_ops._impl.embeddings.store import MemoryVectorStore
    store=MemoryVectorStore()
    for id,kind,repo,path,blob in [('old','desk_doc','docs','old.md','a'*40),('new','desk_doc','docs','old.md','b'*40),('other','desk_doc','other','old.md','a'*40),('card','card','docs','old.md','a'*40)]:
        store.upsert(id,'rev',(1.,0.),content_digest='digest',provenance={'repo_key':repo,'path':path,'blob_sha':blob},content_kind=kind)
    assert store.remove_documents(repo_key='docs',path='old.md',blob_sha='a'*40)==1
    assert store.get('old','rev',content_kind='desk_doc') is None
    assert len(store.records)==3
    assert store.remove_documents(repo_key='docs',path='old.md')==1
    assert store.remove_documents(repo_key='docs',path='old.md')==0


def test_pg_removal_is_parameterized_all_revisions_and_document_only():
    from kp_agent_tooling_ops._impl.embeddings.store import PgVectorStore
    class Cursor:
        rowcount=4
        def execute(self,sql,params): self.sql,self.params=sql,params
        def close(self): pass
    class Connection:
        committed=False
        def __init__(self): self.c=Cursor()
        def cursor(self): return self.c
        def commit(self): self.committed=True
        def rollback(self): pytest.fail('unexpected rollback')
    conn=Connection(); store=PgVectorStore(conn)
    assert store.remove_documents(repo_key='repo',path="docs/quote's.md",blob_sha='a'*40)==4
    assert "content_kind = 'desk_doc'" in conn.c.sql
    assert 'revision_id' not in conn.c.sql
    assert "quote's" not in conn.c.sql
    assert conn.c.params==('repo',"docs/quote's.md",'a'*40)
    assert conn.committed


def test_initialization_cannot_silently_reset_missing_state(tmp_path):
    from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger
    ledger=LifecycleLedger.initialize(tmp_path/'state.db')
    ledger.withdraw('repo','docs/old.md')
    ledger.path.unlink()
    with pytest.raises(ValueError): LifecycleLedger.initialize(ledger.path)




def test_blob_filter_backfills_top_k(case, tmp_path):
    from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger
    from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService
    ledger=LifecycleLedger.initialize(tmp_path/'state.db')
    ledger.withdraw('docs','docs/guide.md',blob_sha=case[4].blob_sha)
    repo = case[0]
    (repo / 'docs/guide.md').write_text('replacement')
    commit(repo, 'replacement guide')
    hits=[case[4],replace(case[4],blob_sha=git(repo, 'rev-parse', 'HEAD:docs/guide.md'),text='replacement')]
    class Ranked:
        def recall(self,**kwargs): return hits[:kwargs['top_k']]
    report=KnowledgeService(case[1],Ranked,lifecycle=ledger).execute('retrieve',{'repo_key':'repo','query':'guidance','top_k':1})
    assert report['data']['results'][0]['text']=='replacement'


def test_catalog_omission_removes_orphan_and_rollback_cannot_restore(case, tmp_path):
    from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger, reconcile_catalog
    from kp_agent_tooling_ops._impl.embeddings.store import MemoryVectorStore
    from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService
    ledger=LifecycleLedger.initialize(tmp_path/'state.db');store=MemoryVectorStore()
    store.upsert('old','rev',(1.,0.),content_digest='digest',provenance={
        'repo_key':'docs','path':'docs/guide.md','blob_sha':case[4].blob_sha},content_kind='desk_doc')
    repository=dict(case[1]['repositories']['repo']);repository['artifacts']={}
    receipt=reconcile_catalog(ledger,lambda:store,repository)
    assert receipt['removed_vectors']==1
    assert not store.document_sources(repo_key='docs')
    assert reconcile_catalog(ledger,lambda:store,repository)['removed_vectors']==0
    result=KnowledgeService(case[1],lambda:Reader([case[4]]),lifecycle=ledger).execute('retrieve',{'repo_key':'repo','query':'guidance'})
    assert result['status']=='corpus_empty'


def test_init_does_not_repair_corruption_by_erasing_history(tmp_path):
    import sqlite3
    from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger
    ledger=LifecycleLedger.initialize(tmp_path/'state.db')
    ledger.path.write_bytes(b'corrupt durable state')
    with pytest.raises(sqlite3.DatabaseError): LifecycleLedger.initialize(ledger.path)
    assert ledger.path.read_bytes()==b'corrupt durable state'
