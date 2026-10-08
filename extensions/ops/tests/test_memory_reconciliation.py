import hashlib
from pathlib import Path
import pytest
from scripts.reconcile_desk_history import reconcile
from test_imported_desk_runtime import setup
from test_legacy_desk_import import _node,_export


def test_isolated_reconciliation_preserves_live_state_and_replays(tmp_path):
    config,rows=setup(tmp_path)
    source=tmp_path/'state';catalog=tmp_path/'catalog.json'
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob('*.sqlite3')}
    row=rows[0];text='Explicit source memory.'
    attrs={k:row[k] for k in ('tenant_id','role','repo_key')}
    note=_node('note:import','DeskNote',dict(attrs,text=text,content_digest=hashlib.sha256(text.encode()).hexdigest()))
    export=_export(tmp_path,[_node(row['binding_key'],'Binding',row)],[note])
    target=tmp_path/'rehearsal'
    result=reconcile(source,catalog,export,target)
    assert result['status']=='reconciled'
    assert result['apply']['imported']==1 and result['replay']['already_present']==1
    assert result['replay']['imported']==0
    assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob('*.sqlite3')}
    assert (target/'source-export.json').read_bytes()==export.read_bytes()
    with pytest.raises(ValueError,match='fresh isolated'):
        reconcile(source,catalog,export,target)


def test_live_destination_refused_before_creation(tmp_path):
    config,rows=setup(tmp_path)
    export=_export(tmp_path,[],[])
    with pytest.raises(ValueError,match='fresh isolated'):
        reconcile(tmp_path/'state',tmp_path/'catalog.json',export,tmp_path/'state'/'child')
    assert not (tmp_path/'state'/'child').exists()
