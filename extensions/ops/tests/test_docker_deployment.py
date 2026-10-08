import importlib.util
from pathlib import Path
import subprocess
import pytest
from kp_agent_tooling_ops._impl.service.gateway_transport import endpoint_parts

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('docker_prepare',ROOT/'scripts/prepare_tooling_docker.py')
prepare=importlib.util.module_from_spec(spec);spec.loader.exec_module(prepare)


def test_export_clone_has_no_alternate_dependency(tmp_path):
    source=tmp_path/'source';source.mkdir()
    def git(*args):return prepare.git('-C',source,*args)
    git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.invalid')
    (source/'file').write_text('evidence');git('add','.');git('commit','-qm','source')
    revision=git('rev-parse','HEAD');shared=tmp_path/'shared'
    prepare.git('clone','--shared',source,shared)
    target=tmp_path/'export';prepare.clone(shared,target,revision)
    # Destroy the lending directories. Export still recovers the exact blob.
    import shutil
    shutil.rmtree(source);shutil.rmtree(shared)
    assert prepare.git('-C',target,'show',revision+':file')=='evidence'
    assert not (target/'.git/objects/info/alternates').exists()


def test_preparation_refuses_existing_destination_and_alias_parent(tmp_path):
    with pytest.raises(ValueError):prepare.prepare('unused',tmp_path)
    alias=tmp_path/'alias';alias.symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(ValueError):prepare.prepare('unused',alias/'new')


def test_explicit_docker_host_gateway_only(monkeypatch):
    monkeypatch.setenv("OPS_ALLOW_DOCKER_HOST_HTTP", "1")
    assert endpoint_parts('http://host.docker.internal:8400/api/mcp').hostname=='host.docker.internal'
    with pytest.raises(ValueError):endpoint_parts('http://arbitrary.internal:8400/api/mcp')
    with pytest.raises(ValueError):endpoint_parts('http://host.docker.internal.attacker:8400/api/mcp')


def test_service_import_does_not_load_retired_app():
    import sys
    result=subprocess.run([sys.executable,'-c',
        "import sys; import kp_agent_tooling._impl.service.desk_memory_runtime; import kp_agent_tooling._impl.service.agent_tooling; "
        "assert 'kp_agent_tooling._impl.service.app' not in sys.modules; assert 'kp_agent_tooling._impl.service.mutation' not in sys.modules"],
        cwd=ROOT,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
