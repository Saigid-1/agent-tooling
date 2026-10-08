import pytest
from kp_agent_tooling._impl.service.import_context import import_context


def context(monkeypatch, text, symbol='handler', citations=None, **kwargs):
    monkeypatch.setattr('kp_agent_tooling._impl.service.import_context.source', lambda *a: ('b'*40, text))
    return import_context('repo', 'a'*40, 'ats', 'route.py', symbol,
                          citations if citations is not None else [{'start_line': 3, 'end_line': 20, 'revision': 'a'*40, 'path': 'route.py', 'blob_sha': 'b'*40}], **kwargs)


def test_exact_import_ownership_and_use_coordinate(monkeypatch):
    result = context(monkeypatch, 'from product.graph.registry import run_query\nfrom kp_core.api.db import get_tenant_db\ndef handler():\n    db = get_tenant_db()\n    return run_query(db)\n')
    rows = {r['local_name']: r for r in result['imports']}
    assert rows['run_query']['module'] == 'product.graph.registry'
    assert rows['get_tenant_db']['module'] == 'kp_core.api.db'
    assert rows['run_query']['next_call']['arguments'] == {'repo_key':'ats','target_revision':'a'*40,'path':'route.py','line':5}
    assert rows['get_tenant_db']['lookup_line'] == 4
    assert all(r['binding_status'] == 'unresolved' for r in rows.values())


def test_imported_name_without_serena_declaration(monkeypatch):
    result = context(monkeypatch, 'from .registry import (\n    run_query as execute,\n)\n', symbol='execute', citations=[])
    assert result['imports'][0]['module'] == '.registry'
    assert result['imports'][0]['lookup_line'] == 2


def test_ambiguous_and_shadowed_imports_are_not_resolved(monkeypatch):
    result = context(monkeypatch, 'from a import go\nfrom b import go\ndef handler(go):\n    return go()\n')
    assert len(result['imports']) == 2
    assert all(r['binding_status'] == 'unresolved' for r in result['imports'])


def test_budget_and_no_result_do_not_claim_absence(monkeypatch):
    result = context(monkeypatch, 'from a import go\nfrom b import go\ndef handler():\n    go()\n', limit=1)
    assert len(result['imports']) == 1 and result['omitted'] == 1
    result = context(monkeypatch, 'def handler():\n    return 1\n')
    assert result['status'] == 'no_results' and result['absence_verdict'] == 'not-established'


def test_syntax_failure_explicit(monkeypatch):
    assert context(monkeypatch, 'def !')['reason'] == 'source_parse_failed'


def test_unsupported_language_does_not_guess():
    assert import_context('repo','a'*40,'ats','app.ts','go',[])['reason'] == 'language_not_supported'


def test_stale_citation_is_not_joined(monkeypatch):
    result = context(monkeypatch, 'from a import go\n', citations=[{'start_line':1,'end_line':1,'revision':'c'*40,'path':'route.py','blob_sha':'b'*40}])
    assert result['reason'] == 'citation_source_mismatch' and result['imports'] == []
