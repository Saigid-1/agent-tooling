import json
import subprocess
import pytest
from test_portable_knowledge import fixture, write
from kp_agent_tooling_ops.knowledge_publish_cli import publish
from kp_agent_tooling._impl.embeddings.embedders import DeterministicEmbedder
from kp_agent_tooling_ops._impl.service.portable_knowledge import PortableKnowledgeProvider


def request(tmp_path, config, revision, name):
    return {'schema_version':'agent-tooling.knowledge-publication.v1','runtime_config':str(config),'destination':str(tmp_path/name),'source_repo_key':'repo','code_revisions':{'repo':revision},'paths':['guide.md'],'registry_path':None}


def test_publish_revision_replay_and_source_boundary(tmp_path):
    provider,config,corpus,catalog,repo,revision,text=fixture(tmp_path)
    original=config.read_bytes()
    result=publish(request(tmp_path,config,revision,'first'),embedder=DeterministicEmbedder())
    assert result['status']=='prepared' and result['activated'] is False
    assert config.read_bytes()==original
    next_config=tmp_path/'first/runtime.json'
    new_provider=PortableKnowledgeProvider(next_config,embedder=DeterministicEmbedder())
    assert new_provider.call('knowledge.retrieve',{'repo_key':'repo','query':'portable','target_revision':revision})['data']['results'][0]['text']==text
    result2=publish(request(tmp_path,next_config,revision,'replay'),embedder=DeterministicEmbedder())
    assert result2['embedded']==0
    (repo/'guide.md').write_text('# New\nNew exact committed content.\n')
    subprocess.run(['git','-C',str(repo),'commit','-am','changed'],check=True,capture_output=True)
    changed=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    publish(request(tmp_path,next_config,changed,'changed'),embedder=DeterministicEmbedder())
    data=json.loads((tmp_path/'changed/corpus.json').read_text())
    assert any(not c['attributes']['is_current'] for c in data['chunks'])
    assert len([c for c in data['chunks'] if c['attributes']['is_current']])==1
    assert config.read_bytes()==original


def test_unmaintained_and_failed_generation_never_activate(tmp_path):
    _,config,_,catalog,repo,revision,_=fixture(tmp_path)
    cat=json.loads(catalog.read_text());cat['repositories']['repo']['artifacts']['guide.md']['status']='withdrawn';write(catalog,cat)
    with pytest.raises(ValueError,match='maintained'):
        publish(request(tmp_path,config,revision,'blocked'),embedder=DeterministicEmbedder())
    assert not (tmp_path/'blocked').exists()


def test_reference_publication_materializes_committed_target(tmp_path):
    _,config,_,catalog,repo,revision,_=fixture(tmp_path)
    (repo/'src').mkdir()
    (repo/'src/worker.py').write_text('def execute():\n    return 1\n')
    (repo/'guide.md').write_text('# Guide\nSee `src/worker.py` and `execute`.\n')
    subprocess.run(['git','-C',str(repo),'add','.'],check=True)
    subprocess.run(['git','-C',str(repo),'commit','-m','references'],check=True,capture_output=True)
    revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    result=publish(request(tmp_path,config,revision,'references'),embedder=DeterministicEmbedder())
    assert sum(r['resolved'] for r in result['references']) >= 1
    doc=json.loads((tmp_path/'references/reference-generation.json').read_text())
    edges=[edge for view in doc['views'].values() for edge in view['positive_edges']]
    assert edges
    assert all(edge['attributes']['resolved_code_revision']==revision for edge in edges)


def test_failed_embedding_leaves_no_completion_receipt(tmp_path):
    _,config,_,_,repo,revision,_=fixture(tmp_path)
    class Broken(DeterministicEmbedder):
        def embed(self,texts):
            raise RuntimeError('injected provider failure')
    with pytest.raises(RuntimeError,match='injected'):
        publish(request(tmp_path,config,revision,'failed'),embedder=Broken())
    assert not (tmp_path/'failed/receipt.json').exists()
    assert not (tmp_path/'failed/runtime.json').exists()


def test_cross_repository_publication_refuses_other_tenant(tmp_path):
    _,config,_,catalog,repo,revision,_=fixture(tmp_path)
    cat=json.loads(catalog.read_text())
    cat['repositories']['other']={**cat['repositories']['repo'],'tenant_ids':['tenant-b']}
    write(catalog,cat)
    req=request(tmp_path,config,revision,'cross-tenant')
    req['code_revisions']['other']=revision
    with pytest.raises(ValueError,match='target repository tenant'):
        publish(req,embedder=DeterministicEmbedder())
    assert not (tmp_path/'cross-tenant').exists()


def test_current_operation_declaration_registry_is_supported(tmp_path):
    _,config,_,_,repo,revision,_=fixture(tmp_path)
    (repo/'handlers.py').write_text('def execute():\n    return 1\n')
    (repo/'registry.py').write_text("KNOWLEDGE_OPERATION_DECLARATIONS = ({'operation':'sample','description':'fixture','handler':'handlers.execute','dispatch_method':'execute','binding':'nameable'},)\n")
    (repo/'guide.md').write_text('# Guide\nCall `knowledge.sample`.\n')
    subprocess.run(['git','-C',str(repo),'add','.'],check=True)
    subprocess.run(['git','-C',str(repo),'commit','-qm','declared operation'],check=True)
    revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    req=request(tmp_path,config,revision,'declared');req.update(registry_path='registry.py',registry_kind='operation_declarations')
    result=publish(req,embedder=DeterministicEmbedder())
    assert sum(r['resolved'] for r in result['references'])==1
    doc=json.loads((tmp_path/'declared/reference-generation.json').read_text())
    outcomes=[o for shard in doc['shards'] for o in shard['outcomes']]
    assert any(o.get('registry_path')=='registry.py' and o['status']=='resolved' for o in outcomes)
