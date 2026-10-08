import json
import subprocess
import sys
from kp_agent_tooling._impl.dependency_identity import declared, provider


def test_oversized_requirements_blob_is_rejected_before_materialization(tmp_path, monkeypatch):
    import kp_agent_tooling._impl.source_citations as citations
    (tmp_path/'app.py').write_text('pass\n')
    (tmp_path/'requirements.txt').write_text('x==1\n' * 60_000)
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    subprocess.run(['git','-C',str(tmp_path),'add','.'],check=True)
    subprocess.run(['git','-C',str(tmp_path),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture'],check=True)
    rev=subprocess.check_output(['git','-C',str(tmp_path),'rev-parse','HEAD'],text=True).strip()
    requirements_blob=subprocess.check_output(['git','-C',str(tmp_path),'rev-parse',
                                               f'{rev}:requirements.txt'],text=True).strip()
    original = citations._git
    fetched = []
    def watched(repo, *args):
        if args[:2] == ('cat-file', 'blob'):
            fetched.append(args[2])
        return original(repo, *args)
    monkeypatch.setattr(citations, '_git', watched)
    row=next(r for r in declared(tmp_path,rev,'app.py')['manifests'] if r['path']=='requirements.txt')
    assert row['status']=='manifest_budget_exceeded'
    assert requirements_blob not in fetched


def test_repeated_empty_includes_use_global_read_budget_without_false_cycle(tmp_path, monkeypatch):
    import kp_agent_tooling._impl.source_citations as citations
    (tmp_path/'app.py').write_text('pass\n')
    (tmp_path/'requirements.txt').write_text('-r empty.txt\n' * 80)
    (tmp_path/'empty.txt').write_text('')
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    subprocess.run(['git','-C',str(tmp_path),'add','.'],check=True)
    subprocess.run(['git','-C',str(tmp_path),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture'],check=True)
    rev=subprocess.check_output(['git','-C',str(tmp_path),'rev-parse','HEAD'],text=True).strip()
    empty_blob=subprocess.check_output(['git','-C',str(tmp_path),'rev-parse',
                                        f'{rev}:empty.txt'],text=True).strip()
    original = citations._git
    fetched = []
    def watched(repo, *args):
        if args[:2] == ('cat-file', 'blob'):
            fetched.append(args[2])
        return original(repo, *args)
    monkeypatch.setattr(citations, '_git', watched)
    row=next(r for r in declared(tmp_path,rev,'app.py')['manifests'] if r['path']=='requirements.txt')
    assert row['status']=='partial'
    assert {gap['reason'] for gap in row['unresolved']} == {'include_budget_exceeded'}
    assert fetched.count(empty_blob) == 63


def test_committed_requirements_includes_report_pins_and_unresolved_references(tmp_path):
    (tmp_path/'src').mkdir()
    (tmp_path/'src/app.py').write_text('pass\n')
    (tmp_path/'requirements.txt').write_text('-r pins/core.txt\n-c pins/constraints.txt\n-r ../outside.txt\n--index-url https://secret.invalid/path\n')
    (tmp_path/'pins').mkdir()
    (tmp_path/'pins/core.txt').write_text('kp-core[all]==0.5.1\n-r ../requirements.txt\n')
    (tmp_path/'pins/constraints.txt').write_text('httpx==0.28.1\n')
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    subprocess.run(['git','-C',str(tmp_path),'add','.'],check=True)
    subprocess.run(['git','-C',str(tmp_path),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture'],check=True)
    rev=subprocess.check_output(['git','-C',str(tmp_path),'rev-parse','HEAD'],text=True).strip()
    (tmp_path/'pins/core.txt').write_text('kp-core==99\n')
    row=next(r for r in declared(tmp_path,rev,'src/app.py')['manifests'] if r['path']=='requirements.txt')
    assert [(d['name'],d['requirement']) for d in row['declarations']] == [('kp-core','kp-core[all]==0.5.1'),('httpx','httpx==0.28.1')]
    assert row['declarations'][0]['origin']['path']=='pins/core.txt'
    assert row['declarations'][0]['origin']['revision']==rev
    assert {g['reason'] for g in row['unresolved']} == {'include_cycle','include_unresolved','unsupported_requirement'}
    assert 'secret.invalid' not in json.dumps(row)


def test_nested_manifests_are_pinned_and_credentials_not_exported(tmp_path):
    (tmp_path/'studio').mkdir()
    (tmp_path/'studio/app.ts').write_text('export const x=1;')
    (tmp_path/'studio/package.json').write_text(json.dumps({'dependencies':{'react':'^19','private':'https://user:secret@host/repo'}}))
    (tmp_path/'pyproject.toml').write_text('[project]\ndependencies=["httpx>=0.28"]\n')
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    subprocess.run(['git','-C',str(tmp_path),'add','.'],check=True)
    subprocess.run(['git','-C',str(tmp_path),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture'],check=True)
    rev=subprocess.check_output(['git','-C',str(tmp_path),'rev-parse','HEAD']).decode().strip()
    (tmp_path/'studio/package.json').write_text('{}')
    result=declared(tmp_path,rev,'studio/app.ts')
    assert [r['path'] for r in result['manifests']]==['studio/package.json','pyproject.toml']
    assert result['manifests'][0]['declarations'][0]['requirement']=='^19'
    assert 'secret' not in json.dumps(result)
    assert result['installed_equivalence']=='not-established'


def test_provider_inventory_is_separate_and_bounded():
    result=provider(sys.executable)
    assert result['status']=='observed' and len(result['packages'])==5
    assert 'product runtime' in result['scope']
