"""Publish selected maintained Git documents and reference evidence offline."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_memory_runtime import private_json
from ._impl.service.document_coordinates import _chunk_specs, _chunk_id
from ._impl.service.document_corpus import DocumentCorpus
from ._impl.embeddings.desk_docs import upsert_desk_doc_embedding
from kp_agent_tooling._impl.embeddings.embedders import RealEmbedder
from ._impl.code_references.publication_facts import PublicationFacts
from ._impl.code_references.population import populate_python_targets
from ._impl.code_references.resolution import ReferenceResolver
from ._impl.code_references.indexing import DocumentReferenceIndexer
from ._impl.code_references.manifest import SQLiteReferenceManifestStore, freeze_reference_retrieval
from ._impl.code_references.models import ChunkRange
from ._impl.code_references.portable_generation import generation_document
from ._impl.code_references.registry import read_committed_registry, read_committed_operation_declarations


def _git(root, *args):
    return subprocess.check_output(['git', '--no-optional-locks', '-C', str(root), *args], stderr=subprocess.PIPE, timeout=60)


def _write(path, value):
    return leaf.write_new_canonical(path, value, ascii=True, allow_nan=True, fsync=True)


def publish(request, *, embedder=None):
    """Build into a fresh directory; publish its completion receipt last.

    No current pointer, lifecycle ledger, catalog, admission or live state is
    mutated. A scheduler may prepare a generation; operator activation remains
    an explicit atomic configuration replacement after reviewing the receipt.
    """
    required = {'schema_version','runtime_config','destination','source_repo_key','code_revisions','paths','registry_path'}
    if (not isinstance(request, dict) or not required <= request.keys()
            or set(request) - required - {'registry_kind'}
            or request['schema_version'] != 'agent-tooling.knowledge-publication.v1'):
        raise ValueError('invalid knowledge publication request')
    if not isinstance(request['code_revisions'], dict) or not isinstance(request['source_repo_key'], str):
        raise ValueError('source key and revision mapping required')
    kind = request.get('registry_kind', 'bindings')
    if kind not in {'bindings', 'operation_declarations'}:
        raise ValueError('unsupported registry kind')
    config = private_json(request['runtime_config'])
    catalog = private_json(config['catalog_path'])
    source_key = request['source_repo_key']
    repos = catalog['repositories']
    if source_key not in repos or not 1 <= len(request['code_revisions']) <= 8:
        raise ValueError('explicit registered source and bounded target revisions required')
    roots = {}
    for key, revision in request['code_revisions'].items():
        if key not in repos or not re.fullmatch('[0-9a-f]{40}', revision):
            raise ValueError('registered repository and exact full revision required')
        if config['tenant_id'] not in repos[key].get('tenant_ids', []):
            raise ValueError('target repository tenant not authorized')
        root = Path(repos[key]['path'])
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError('physical repository root required')
        if _git(root, 'rev-parse', revision+'^{commit}').decode().strip() != revision:
            raise ValueError('revision is not an exact commit')
        roots[key] = root
    if source_key not in roots:
        raise ValueError('source revision required')
    paths = request['paths']
    if not isinstance(paths,list) or not 1 <= len(paths) <= 1000 or len(set(paths)) != len(paths):
        raise ValueError('bounded unique document paths required')
    source = repos[source_key]
    if config['tenant_id'] not in source['tenant_ids']:
        raise ValueError('source tenant not authorized')
    for path in paths:
        if not isinstance(path,str) or not path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
            raise ValueError('repository-relative document path required')
        artifact = source.get('artifacts',{}).get(path,{})
        if artifact.get('status') != 'maintained' or not artifact.get('owner'):
            raise ValueError('publication requires a maintained owned artifact')
    # Withdrawals remain a mandatory read-time boundary; never initialize one here.
    from ._impl.service.knowledge_lifecycle import LifecycleLedger
    LifecycleLedger(config['lifecycle_path']).entries()
    destination = Path(request['destination'])
    if not destination.is_absolute() or destination.parent.resolve() != destination.parent or destination.exists():
        raise ValueError('fresh destination below a physical existing directory required')
    leaf.mkdir_private(destination)
    embedder = embedder or RealEmbedder(**config['embedding'])
    previous = DocumentCorpus(**config['documents'], embedder=embedder)
    chunks = json.loads(previous.path.read_text())['chunks']
    vectors = previous.vectors
    scope = source.get('corpus_scope',source_key)
    # Selected paths are a complete maintained scope for this publication.
    for row in chunks:
        if row['attributes']['repo_key'] == scope:
            row['attributes']['is_current'] = False
    by_id = {row['id']:row for row in chunks}
    facts = PublicationFacts()
    coverages = {key:populate_python_targets(graph=facts,root=root,repo_key=key,revision=request['code_revisions'][key]) for key,root in roots.items()}
    resolver = ReferenceResolver(repositories=roots,graph=facts,symbol_index_fingerprint='unavailable',symbol_index_complete=False,coverage_provider=lambda k,r:coverages.get(k))
    manifests = SQLiteReferenceManifestStore(leaf.store_path(destination, leaf.REFERENCES_DB))
    indexer = DocumentReferenceIndexer(graph=facts,resolver=resolver,manifests=manifests)
    revision = request['code_revisions'][source_key]
    read_registry = read_committed_operation_declarations if kind == 'operation_declarations' else read_committed_registry
    registry = read_registry(roots[source_key],revision,request['registry_path']) if request['registry_path'] else None
    receipts=[]; embedded=0
    for path in sorted(paths):
        entry = _git(roots[source_key],'ls-tree','-z',revision,'--',path).decode().rstrip('\0').split(None,3)
        if len(entry)!=4 or entry[0] not in {'100644','100755'} or entry[1]!='blob' or entry[3]!=path:
            raise ValueError('document is not an exact regular Git blob')
        blob=entry[2]; raw=_git(roots[source_key],'cat-file','blob',blob)
        if len(raw)>4*1024*1024: raise ValueError('document exceeds 4 MiB')
        raw.decode('utf-8'); ranges=[]
        for offset,length,heading in _chunk_specs(raw,1500):
            identity=_chunk_id(scope,path,blob,offset,length)
            attrs={'repo_key':scope,'path':path,'blob_sha':blob,'byte_offset':offset,'byte_length':length,'heading':heading,'local_path':str(roots[source_key]),'is_current':True,'content_digest':hashlib.sha256(raw[offset:offset+length]).hexdigest()}
            node={'id':identity,'entity_type':'DeskDocumentChunk','attributes':attrs}
            if identity in by_id:
                # A root relocation does not alter the original vector coordinates.
                node['attributes']['local_path']=by_id[identity]['attributes']['local_path']
                if {k:v for k,v in by_id[identity]['attributes'].items() if k!='is_current'} != {k:v for k,v in node['attributes'].items() if k!='is_current'}:
                    raise ValueError('existing document identity conflict')
                by_id[identity]['attributes']['is_current']=True
            else:
                chunks.append(node);by_id[identity]=node
            relocated={**node,'attributes':{**node['attributes'],'local_path':str(roots[source_key])}}
            # Old unchanged vectors are reused after their immutable source is verified.
            from ._impl.embeddings.desk_docs import build_desk_doc_embedding_input, desk_doc_embedding_revision
            item=build_desk_doc_embedding_input(relocated)
            er=desk_doc_embedding_revision(embedder)
            old=vectors.get(identity,er.revision_id,content_kind='desk_doc')
            if old is not None:
                if old.content_digest != item.content_digest: raise ValueError('existing vector source mismatch')
            else:
                embedded += int(upsert_desk_doc_embedding(relocated,embedder=embedder,store=vectors).upserted)
                by_id[identity]['attributes']['local_path']=str(roots[source_key])
            ranges.append(ChunkRange(identity,offset,length))
        receipts.append(asdict(indexer.index_document(raw,source_repo_key=source_key,document_path=path,document_blob_sha=blob,chunks=ranges,registry_view=registry,code_revisions=request['code_revisions'])))
    corpus={'schema_version':'agent-tooling.document-corpus.v1','chunks':chunks,'vectors':[asdict(v) for v in vectors.records]}
    corpus_sha=_write(destination/'corpus.json',corpus)
    DocumentCorpus(destination/'corpus.json',sha256=corpus_sha,repositories=config['documents']['repositories'],embedder=embedder)
    frozen=freeze_reference_retrieval(manifests,facts,source_repo_key=source_key,target_revisions=request['code_revisions'])
    ref_sha=_write(destination/'reference-generation.json',generation_document(frozen))
    updated={**config,'documents':{**config['documents'],'path':str(destination/'corpus.json'),'sha256':corpus_sha},'reference_generation':{'path':str(destination/'reference-generation.json'),'sha256':ref_sha}}
    _write(destination/'runtime.json',updated)
    receipt={'schema_version':'agent-tooling.knowledge-publication-receipt.v1','status':'prepared','code_revisions':request['code_revisions'],'paths':paths,'embedded':embedded,'chunks':len(chunks),'references':receipts,'generation':frozen.generation_metadata,'runtime_config_sha256':hashlib.sha256((destination/'runtime.json').read_bytes()).hexdigest(),'activated':False}
    _write(destination/'receipt.json',receipt)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(publish(private_json(args.request)),sort_keys=True));return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status':'error','reason':str(exc)}));return 1

if __name__=='__main__':
    raise SystemExit(main())
