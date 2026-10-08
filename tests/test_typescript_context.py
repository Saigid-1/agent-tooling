import json
from pathlib import Path
import pytest
from kp_agent_tooling._impl.typescript_context import context

@pytest.fixture
def compiler():
    path=Path('config/agent-tooling.local.json')
    if not path.exists(): pytest.skip('optional operator TypeScript runtime required')
    return json.loads(path.read_text())['typescript']


def test_alias_type_and_reexport_coordinates(monkeypatch, compiler):
    text = '''// import Fake from "dead";
import Def, {type Shape, original as alias} from "@/actual";
import * as Namespace from "pkg";
export {alias as exposed} from "./bridge";
export * from "./other";
'''
    monkeypatch.setattr('kp_agent_tooling._impl.typescript_context.source',lambda *a:('b'*40,text))
    result=context('repo','a'*40,'ats','studio/input.ts',compiler)
    assert result['status']=='ok'
    rows=result['imports']
    assert len(rows)==6
    alias=next(r for r in rows if r['local_name']=='alias')
    assert alias['imported_name']=='original' and alias['lookup_line']==2
    assert alias['next_call']['arguments']['path']=='studio/input.ts'
    assert next(r for r in rows if r['local_name']=='Shape')['type_only']
    assert all(r['binding_status']=='unresolved' for r in rows)
    assert rows[-1]['kind']=='re_export'


def test_invalid_syntax_and_budget(monkeypatch, compiler):
    monkeypatch.setattr('kp_agent_tooling._impl.typescript_context.source',lambda *a:('b'*40,'import { from'))
    assert context('repo','a'*40,'ats','x.ts',compiler)['reason']=='source_parse_failed'
    monkeypatch.setattr('kp_agent_tooling._impl.typescript_context.source',lambda *a:('b'*40,'import "x";\n'*30))
    result=context('repo','a'*40,'ats','x.ts',compiler)
    assert len(result['imports'])==24 and result['omitted']==6


def test_compiler_drift_refused(monkeypatch, compiler):
    monkeypatch.setattr('kp_agent_tooling._impl.typescript_context.source',lambda *a:('b'*40,''))
    assert context('repo','a'*40,'ats','x.ts',{**compiler,'sha256':'0'*64})['reason']=='compiler_digest_mismatch'
