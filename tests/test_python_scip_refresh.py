import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('refresh_navigation',Path(__file__).resolve().parents[1]/'packages/tooling/src/kp_agent_tooling/refresh_cli.py')
refresh=importlib.util.module_from_spec(spec);spec.loader.exec_module(refresh)


def test_python_command_uses_physical_checkout(tmp_path):
    physical=tmp_path/'physical';physical.mkdir()
    alias=tmp_path/'alias';alias.symlink_to(physical,target_is_directory=True)
    cmd=refresh.python_index_command(tmp_path/'tools',alias,'core','1.0',tmp_path/'env.json',tmp_path/'index.scip')
    assert cmd[cmd.index('--cwd')+1]==str(physical)


def test_empty_python_index_rejected_before_publication(tmp_path):
    publication=tmp_path/'current.json';publication.write_text('{"profile":"last-good"}')
    before=publication.read_bytes()
    with pytest.raises(refresh.IndexCoverageError,match='empty_python_index'):
        refresh.validate_python_coverage({'data':{'blobs':{},'occurrences':[]}},'core')
        refresh.atomic(publication,{'profile':'bad'})
    assert publication.read_bytes()==before


def test_python_documents_without_occurrences_rejected():
    with pytest.raises(refresh.IndexCoverageError,match='empty_python_index'):
        refresh.validate_python_coverage({'data':{'blobs':{'x.py':'sha'},'occurrences':[]}},'core')


def test_python_coverage_accepts_populated_index():
    refresh.validate_python_coverage({'data':{'blobs':{'x.py':'sha'},'occurrences':[{'symbol':'x'}]}},'core')


def test_failure_receipt_explains_operator_remedy_without_raw_error():
    error=refresh.IndexCoverageError('empty_python_index','core')
    receipt=refresh.failure_receipt(error)
    assert receipt['status']=='refresh_failed'
    assert receipt['reason']=='empty_python_index'
    assert 'physical' in receipt['remediation'] and 'scope' in receipt['remediation']
    assert receipt['last_good_profile']=='preserved'
    generic=refresh.failure_receipt(RuntimeError('secret remote URL'))
    assert 'secret' not in json.dumps(generic)


def test_refresh_pipeline_refuses_empty_index_and_reports_remedy(tmp_path,monkeypatch,capsys):
    publication=tmp_path/'current.json';publication.write_text('{"profile":"last-good"}')
    original=publication.read_bytes()
    template=tmp_path/'template.yml';template.write_text('read_only: true')
    chain=tmp_path/'tools';chain.mkdir();(chain/'package-lock.json').write_text('{}')
    request={'publication':str(publication),'check_status':str(tmp_path/'status.json'),
             'output_root':str(tmp_path/'snapshots'),'toolchain':str(chain),'node':'node',
             'repositories':{'core':{'repository':'fixture','serena_template':str(template),'python_scope':['pkg']}}}
    config=tmp_path/'request.json';config.write_text(json.dumps(request))
    revision='a'*40
    def run(args,**kwargs):
        if 'ls-remote' in args:return f'ref: refs/heads/main\tHEAD\n{revision}\tHEAD'
        if 'clone' in args:
            repo=Path(args[-1]);repo.mkdir(parents=True)
            (repo/'pyproject.toml').write_text('[project]\nversion="1.0"\n')
        if 'rev-parse' in args:return revision
        if '--output' in args:Path(args[args.index('--output')+1]).write_bytes(b'fixture')
        if args[0]=='node':return '{}'
        return ''
    monkeypatch.setattr(refresh,'run',run)
    monkeypatch.setattr(refresh,'build_index',lambda *a,**k:{'data':{'blobs':{},'occurrences':[]}})
    monkeypatch.setattr(refresh.sys,'argv',['refresh_navigation.py','--request',str(config)])
    assert refresh.main()==1
    assert publication.read_bytes()==original
    receipt=json.loads((tmp_path/'status.json').read_text())
    assert receipt['reason']=='empty_python_index' and receipt['repo_key']=='core'
    assert receipt['remediation'] in capsys.readouterr().out


def test_lookup_smoke_rejects_inconsistent_index(monkeypatch,tmp_path):
    (tmp_path/'x.py').write_text('name = 1\n')
    index={'data':{'occurrences':[{'definition':True,'path':'x.py','byte_offset':0,'symbol':'name'}]}}
    monkeypatch.setattr(refresh,'lookup',lambda *a,**k:{'status':'no_results','occurrences':[]})
    with pytest.raises(refresh.IndexCoverageError,match='index_lookup_failed'):
        refresh.verify_index_lookup(index,tmp_path,'a'*40,'core')


def test_indexer_failure_reports_exit_code_and_safe_category_only():
    import subprocess
    error = subprocess.CalledProcessError(137, ['indexer'], stderr=b'FATAL ERROR: JavaScript heap out of memory SECRET-CONTENT')
    result = refresh.failure_receipt(refresh.RefreshStepError('ops','python_index',error))
    assert result['reason']=='indexer_out_of_memory' and result['exit_code']==137
    assert 'SECRET-CONTENT' not in json.dumps(result)
