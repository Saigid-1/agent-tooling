"""K1 FEATURE world: an operator catalog and two published generations (A, B).

Three repositories exist; the operator serves two (``core``, ``ops``) and refresh
publishes all three (``extra`` is published only). Generation B moves ``core`` to a
new commit in a new clone, and adds a capability for it. The operator's own clone of
``core`` never receives B's commit (the 2026-10-07 attempt-1 failure).

Everything is built under the caller's directory (pytest's ``tmp_path``, which lives
under TMPDIR). No network, no model: the embedder is the deterministic stub.
"""
import hashlib
import json
import os
import subprocess
from pathlib import Path

from kp_agent_tooling._impl.scip_navigation import build_index, write_partitioned_index

SERVED = ('core', 'ops')
PUBLISHED_ONLY = 'extra'
OPERATOR_TENANT = 'operator-tenant'
REFRESH_TENANT = 'refresh-tenant'
SERVER = 'def inspect():\n return custody()\n'
KNOWLEDGE_TOOLS = ['knowledge.capabilities', 'knowledge.reference_recovery', 'knowledge.reference_diagnostics',
                   'knowledge.platform', 'knowledge.symbol', 'knowledge.context', 'knowledge.retrieve',
                   'knowledge.check_references', 'knowledge.discover']


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True,
                                   stderr=subprocess.DEVNULL).strip()


def write_private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scip(root, revision, key):
    doc = {'metadata': {'tool_info': {'name': 'scip-python', 'version': '0.6.6'}}, 'documents': [
        {'relative_path': 'server.py', 'occurrences': [
            {'symbol': f'scip-python python {key} 1 inspect().', 'symbol_roles': 1, 'range': [0, 4, 11]}]}]}
    return build_index(str(root), revision, key, doc, provenance={'fixture': 'k1-feature'})


class World:
    def __init__(self, base):
        self.base = Path(base)
        self.src = self.base / 'src'
        self.state = self.base / 'state'
        self.config_dir = self.base / 'config'
        self.generations = self.state / 'navigation' / 'generations'
        self.current = self.state / 'navigation' / 'current.json'
        for path in (self.src, self.config_dir, self.generations, self.state / 'repositories'):
            path.mkdir(parents=True, exist_ok=True)
        self.revisions = {'A': {}, 'B': {}}
        for key in (*SERVED, PUBLISHED_ONLY):
            repo = self.src / key
            repo.mkdir()
            git(repo, 'init', '-q', '-b', 'main')
            git(repo, 'config', 'user.email', 'fixture@example.invalid')
            git(repo, 'config', 'user.name', 'K1 Fixture')
            (repo / 'server.py').write_text(SERVER)
            (repo / 'docs').mkdir()
            (repo / 'docs' / 'guide.md').write_text('# Guide\nUse the portable context builder.\n')
            git(repo, 'add', '.')
            git(repo, 'commit', '-qm', 'generation A')
            self.revisions['A'][key] = git(repo, 'rev-parse', 'HEAD')
            # The operator's static clone: it is never refreshed, so it never sees B.
            subprocess.check_call(['git', 'clone', '-q', '--no-hardlinks', str(repo),
                                   str(self.state / 'repositories' / key)], stderr=subprocess.DEVNULL)
        core = self.src / 'core'
        (core / 'server.py').write_text(SERVER + '\n\ndef advanced():\n return 2\n')
        (core / 'docs' / 'advanced.md').write_text('# Advanced\n')
        git(core, 'add', '.')
        git(core, 'commit', '-qm', 'generation B')
        self.revisions['B'] = dict(self.revisions['A'], core=git(core, 'rev-parse', 'HEAD'))
        self.capabilities = {
            'A': {key: {'cap-a': 'docs/guide.md'} for key in (*SERVED, PUBLISHED_ONLY)},
            'B': {key: {'cap-a': 'docs/guide.md'} for key in (*SERVED, PUBLISHED_ONLY)},
        }
        self.capabilities['B']['core'] = {'cap-a': 'docs/guide.md', 'cap-new': 'docs/advanced.md'}
        self.catalogs = {name: self._generation(name) for name in ('A', 'B')}
        self.profiles = {name: self._profile(name) for name in ('A', 'B')}
        self.operator_catalog = self.config_dir / 'knowledge.json'
        self.write_operator(self.operator_value())
        self.runtime = self.config_dir / 'knowledge-runtime.json'
        self._runtime()
        self.tooling = self.config_dir / 'tooling.json'
        self.tooling_value = {
            'schema_version': 'ops.agent-tooling.v1',
            'repos': {key: {'path': self.catalogs['A']['repositories'][key]['path'],
                            'revision': self.revisions['A'][key]} for key in (*SERVED, PUBLISHED_ONLY)},
            'navigation_profile': str(self.current),
            'portable_knowledge_config': str(self.runtime),
            'delivery_root': str(self._mkdir(self.base / 'delivery')),
            'enabled_tools': list(KNOWLEDGE_TOOLS),
        }
        self.write_tooling(self.tooling_value)
        self.publish('A')

    @staticmethod
    def _mkdir(path):
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- generations -----------------------------------------------------------------

    def _generation(self, name):
        root = self._mkdir(self.generations / name)
        repositories, scip, sources = {}, {}, {}
        for key in (*SERVED, PUBLISHED_ONLY):
            revision = self.revisions[name][key]
            clone = root / key
            subprocess.check_call(['git', 'clone', '-q', '--no-hardlinks', str(self.src / key), str(clone)],
                                  stderr=subprocess.DEVNULL)
            git(clone, 'checkout', '-q', '--detach', revision)
            built = _scip(clone, revision, key)
            index = root / f'{key}-python.ops.json'
            written = write_partitioned_index(index, built)
            scip[key] = [{'path': str(index), 'revision': revision, 'sha256': written['sha256']}]
            repositories[key] = {'path': str(clone), 'ref': revision, 'corpus_scope': key,
                                 'tenant_ids': [REFRESH_TENANT],
                                 'capabilities': dict(self.capabilities[name][key])}
            sources[key] = {'revision': revision, 'artifacts': [{'path': 'server.py', 'role': 'dependency'}]}
        platforms = {
            'core': {'schema': 'ops.platform-request.v1', 'owner': 'OPS', 'profile': 'k1-fixture',
                     'sources': {key: sources[key] for key in SERVED}},
            PUBLISHED_ONLY: {'schema': 'ops.platform-request.v1', 'owner': 'OPS', 'profile': 'k1-fixture',
                             'sources': {PUBLISHED_ONLY: sources[PUBLISHED_ONLY]}},
        }
        return {'schema_version': 'ops.knowledge-config.v1', 'repositories': repositories,
                'platforms': platforms, 'scip_indexes': scip}

    def _profile(self, name, catalog=None):
        catalog = self.catalogs[name] if catalog is None else catalog
        path = self.generations / name / 'knowledge.json'
        path.write_text(json.dumps(catalog))
        return {'schema_version': 'ops.navigation-profile.v1', 'profile': 'dev-current',
                'published_at': f'generation-{name}',
                'repos': {key: {'path': row['path'], 'revision': row['ref']}
                          for key, row in catalog['repositories'].items()},
                'knowledge_config': str(path),
                'knowledge_config_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def publish(self, name, *, catalog=None, profile=None):
        """Flip current.json to a generation, as refresh does; returns the profile digest."""
        if catalog is not None:
            self.profiles[name] = self._profile(name, catalog)
        value = self.profiles[name] if profile is None else profile
        temporary = self.current.with_suffix('.tmp')
        temporary.write_text(json.dumps(value))
        os.replace(temporary, self.current)
        return hashlib.sha256(self.current.read_bytes()).hexdigest()

    # -- operator files ----------------------------------------------------------------

    def operator_value(self):
        """The operator's catalog: hand copy of generation A, operator fields, served subset."""
        catalog_a = self.catalogs['A']
        repositories = {}
        for key in SERVED:
            repositories[key] = {
                'path': str(self.state / 'repositories' / key), 'ref': self.revisions['A'][key],
                'default_branch_ref': 'refs/heads/main', 'corpus_scope': key,
                'tenant_ids': [OPERATOR_TENANT],
                'capabilities': {'cap-a': 'docs/guide.md'},
                'entry_symbols': {'cap-a': 'server.inspect'},
                'artifacts': {'docs/guide.md': {'status': 'maintained', 'owner': f'operator-{key}'}},
            }
        return {'schema_version': 'ops.knowledge-config.v1', 'repositories': repositories,
                'navigation': {'provider': 'published_scip', 'profile_path': str(self.current),
                               'published_root': str(self.state), 'local_root': str(self.state)},
                'platforms': {'core': json.loads(json.dumps(catalog_a['platforms']['core']))},
                'scip_indexes': {key: json.loads(json.dumps(catalog_a['scip_indexes'][key])) for key in SERVED}}

    def write_operator(self, value):
        write_private(self.operator_catalog, value)

    def _runtime(self):
        from kp_agent_tooling._impl.embeddings.embedders import DeterministicEmbedder
        from kp_agent_tooling_ops._impl.embeddings.desk_docs import desk_doc_embedding_revision
        from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger
        operator_core = self.state / 'repositories' / 'core'
        text = (operator_core / 'docs' / 'guide.md').read_text()
        blob = git(operator_core, 'rev-parse', 'HEAD:docs/guide.md')
        embedder = DeterministicEmbedder()
        revision = desk_doc_embedding_revision(embedder)
        attrs = {'repo_key': 'core', 'path': 'docs/guide.md', 'blob_sha': blob, 'byte_offset': 0,
                 'byte_length': len(text.encode()), 'heading': 'Guide', 'local_path': '/retired/checkout',
                 'is_current': True, 'content_digest': hashlib.sha256(text.encode()).hexdigest()}
        chunk = {'id': 'deskdoc:core-guide', 'entity_type': 'DeskDocumentChunk', 'attributes': attrs}
        provenance = {k: v for k, v in attrs.items() if k not in {'is_current', 'content_digest'}}
        vector = {'chunk_content_id': 'deskdoc:core-guide', 'revision_id': revision.revision_id,
                  'vector': embedder.embed([text])[0], 'content_digest': attrs['content_digest'],
                  'provenance': provenance, 'content_kind': 'desk_doc', 'embedding_model_id': revision.model_id}
        corpus = self.config_dir / 'corpus.json'
        sha = write_private(corpus, {'schema_version': 'agent-tooling.document-corpus.v1',
                                     'chunks': [chunk], 'vectors': [vector]})
        lifecycle = LifecycleLedger.initialize(self.config_dir / 'lifecycle.sqlite3')
        self.embedder = embedder
        write_private(self.runtime, {
            'schema_version': 'agent-tooling.knowledge-runtime.v1', 'tenant_id': OPERATOR_TENANT,
            'lifecycle_path': str(lifecycle.path), 'catalog_path': str(self.operator_catalog),
            'documents': {'path': str(corpus), 'sha256': sha,
                          'repositories': {'core': str(operator_core)}},
            'embedding': {}})

    def write_tooling(self, value):
        self.tooling_value = value
        write_private(self.tooling, value)

    # -- surfaces ----------------------------------------------------------------------

    def adapter(self):
        """The served surface: AgentTooling with the installed OPS provider."""
        from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
        adapter = AgentTooling(self.tooling)
        # The stub embedder, set before any read: never a model download.
        adapter.knowledge_provider._embedder = self.embedder
        return adapter

    def provider(self, *, navigation=True):
        from kp_agent_tooling_ops._impl.service.portable_knowledge import PortableKnowledgeProvider
        try:
            return PortableKnowledgeProvider(self.runtime, embedder=self.embedder,
                                             navigation=self.tooling_value if navigation else None)
        except TypeError:  # base: the provider has no navigation seam
            return PortableKnowledgeProvider(self.runtime, embedder=self.embedder)

    def symbol(self, name='B', key='core'):
        return {'repo_key': key, 'target_revision': self.revisions[name][key], 'path': 'server.py', 'line': 1}

    def profile_sha256(self):
        return hashlib.sha256(self.current.read_bytes()).hexdigest()
