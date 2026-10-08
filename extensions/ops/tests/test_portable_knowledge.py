import hashlib
import json
import subprocess

import pytest
from kp_agent_tooling_ops._impl.service.portable_knowledge import PortableKnowledgeProvider
from kp_agent_tooling_ops._impl.service.document_corpus import DocumentCorpus
from kp_agent_tooling._impl.embeddings.embedders import DeterministicEmbedder
from kp_agent_tooling_ops._impl.embeddings.desk_docs import desk_doc_embedding_revision
from kp_agent_tooling_ops._impl.code_references.manifest import FrozenReferenceRetrieval
from kp_agent_tooling_ops._impl.code_references.portable_generation import generation_document, load_generation


def write(path, value):
    path.write_text(json.dumps(value)); path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path):
    repo = tmp_path/'repo';repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
    git('init','-q');git('config','user.email','fixture@example.invalid');git('config','user.name','Test Fixture')
    text = '# Guide\nUse the portable context builder.\n'
    (repo/'guide.md').write_text(text);git('add','.');git('commit','-qm','fixture')
    revision=git('rev-parse','HEAD');blob=git('rev-parse','HEAD:guide.md')
    embedder=DeterministicEmbedder();er=desk_doc_embedding_revision(embedder)
    attrs={'repo_key':'repo','path':'guide.md','blob_sha':blob,'byte_offset':0,'byte_length':len(text.encode()),'heading':'Guide','local_path':'/retired/checkout','is_current':True,'content_digest':hashlib.sha256(text.encode()).hexdigest()}
    chunk={'id':'deskdoc:one','entity_type':'DeskDocumentChunk','attributes':attrs}
    provenance={k:v for k,v in attrs.items() if k not in {'is_current','content_digest'}}
    vector={'chunk_content_id':'deskdoc:one','revision_id':er.revision_id,'vector':embedder.embed([text])[0],'content_digest':attrs['content_digest'],'provenance':provenance,'content_kind':'desk_doc','embedding_model_id':er.model_id}
    corpus=tmp_path/'corpus.json';sha=write(corpus,{'schema_version':'agent-tooling.document-corpus.v1','chunks':[chunk],'vectors':[vector]})
    catalog=tmp_path/'catalog.json';write(catalog,{'schema_version':'ops.knowledge-config.v1','repositories':{'repo':{'path':str(repo),'ref':revision,'default_branch_ref':'refs/heads/'+git('branch','--show-current'),'corpus_scope':'repo','tenant_ids':['tenant-a'],'capabilities':{},'artifacts':{'guide.md':{'owner':'fixture','status':'maintained'}}}}})
    from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger
    lifecycle=LifecycleLedger.initialize(tmp_path/'lifecycle.sqlite3')
    config=tmp_path/'runtime.json';write(config,{'schema_version':'agent-tooling.knowledge-runtime.v1','tenant_id':'tenant-a','lifecycle_path':str(lifecycle.path),'catalog_path':str(catalog),'documents':{'path':str(corpus),'sha256':sha,'repositories':{'repo':str(repo)}},'embedding':{}})
    return PortableKnowledgeProvider(config,embedder=embedder),config,corpus,catalog,repo,revision,text


def test_portable_retrieval_and_catalog_without_gateway(tmp_path):
    provider,config,corpus,catalog,repo,revision,text=fixture(tmp_path)
    assert len(provider.tools()) == 9
    caps=provider.call('knowledge.capabilities',{'repo_key':'repo'})
    assert caps['status']=='no_results'
    result=provider.call('knowledge.retrieve',{'repo_key':'repo','query':'portable','target_revision':revision})
    assert result['data']['results'][0]['text']==text
    assert result['data']['results'][0]['revision_proof']=='exact_path_blob_match'
    assert result['data']['target_revision']==revision
    assert provider.reader() is provider.reader()
    assert provider.reader().reconciliation['orphan_vector_ids']==[]
    (repo/'guide.md').write_text('dirty tree must not be read')
    assert provider.call('knowledge.retrieve',{'repo_key':'repo','query':'portable','target_revision':revision})['data']['results'][0]['text']==text


def test_withdrawal_and_tenant_are_checked_on_each_call(tmp_path):
    provider,config,corpus,catalog,repo,revision,text=fixture(tmp_path)
    c=json.loads(catalog.read_text());c['repositories']['repo']['artifacts']['guide.md']['status']='withdrawn';write(catalog,c)
    result=provider.call('knowledge.retrieve',{'repo_key':'repo','query':'portable','target_revision':revision})
    assert result['data']['results']==[]
    c['repositories']['repo']['tenant_ids']=['other'];write(catalog,c)
    with pytest.raises(PermissionError):provider.call('knowledge.capabilities',{'repo_key':'repo'})


def test_tampered_corpus_and_runtime_refused(tmp_path):
    provider,config,corpus,*_=fixture(tmp_path)
    reader=provider.reader();corpus.write_text('{}')
    with pytest.raises(ValueError,match='changed'):reader.recall(query='x',top_k=1)
    config.write_text('{}')
    with pytest.raises(ValueError,match='changed'):provider.call('knowledge.capabilities',{'repo_key':'repo'})


def test_generation_identity_survives_portable_roundtrip(tmp_path):
    f=FrozenReferenceRetrieval((),{},source_repo_key='repo',target_revisions={'repo':'a'*40})
    p=tmp_path/'refs.json';sha=write(p,generation_document(f))
    assert load_generation(p,sha256=sha).generation_metadata==f.generation_metadata
    doc=json.loads(p.read_text());doc['metadata']['generation_digest']='wrong';sha=write(p,doc)
    with pytest.raises(ValueError,match='identity'):load_generation(p,sha256=sha)


def test_portable_cli_native_mcp_parity_without_legacy_gateway(tmp_path):
    import asyncio
    from datetime import timedelta
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    provider,config,corpus,catalog,repo,revision,text=fixture(tmp_path)
    adapter_config=tmp_path/'tooling.json'
    write(adapter_config,{'schema_version':'ops.agent-tooling.v1',
        'repos':{'repo':{'path':str(repo),'revision':revision}},
        'portable_knowledge_config':str(config),'delivery_root':str(tmp_path/'delivery'),
        'enabled_tools':[t['name'] for t in provider.tools()]})
    command=[sys.executable,'-m','kp_agent_tooling.cli','--config',str(adapter_config)]
    args={'repo_key':'repo'}
    result=subprocess.run(command+['call','--tool','knowledge.capabilities','--arguments',json.dumps(args)],capture_output=True,text=True,check=True)
    expected=json.loads(result.stdout)
    async def check():
        params=StdioServerParameters(command=command[0],args=command[1:]+['serve'])
        async with stdio_client(params) as (read,write_stream):
            async with ClientSession(read,write_stream,read_timeout_seconds=timedelta(seconds=20)) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools)==9
                reply=await session.call_tool('knowledge.capabilities',args)
                assert not reply.isError
                assert reply.structuredContent==expected==json.loads(reply.content[0].text)
    asyncio.run(check())
