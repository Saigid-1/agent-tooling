"""K1: the OPS knowledge tools follow the published navigation profile.

One test per falsifier F1-F9 of `docs/work/orders/K1-published-knowledge-catalog.md`.
Every interface name these tests assume lives in `k1_seams.py`; the meet reconciles
names there, never the assertions here.

The world (built per test, in-process, no Docker, no network, stub embedder):
- upstream `core` with history c0 -> O -> A -> B; `ops`, `ats`, `extra` with one commit;
- refresh's publication: generation A, then generation B, each a snapshot clone per
  repo, a SCIP index for `core`, a published catalog and a profile; B gives `core` a
  new ref AND a new path and adds a capability document (`core-new`); the published
  capability map is empty, as refresh writes it today (amendment 1, shape (a)), except
  where F9 publishes its shape (b);
- the operator: `config/knowledge.json` (the static catalog `knowledge-runtime.json`
  names) pinning `core` at O, with its own tenant, served set (3 of the 4 published
  repos), `corpus_scope`, `artifacts`, `entry_symbols`, `navigation` section and
  `capabilities` map, all different from the published catalog's; a full clone of `core` (has B) and a stale
  clone (no B) for F2; the operator's document corpus and lifecycle ledger.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess

import pytest

import k1_seams as seams


TENANT_OPERATOR = 'tenant-operator'
TENANT_PUBLISHED = 'tenant-refresh'
TENANT_EXTRA = 'tenant-published-extra'
OPERATOR_SCOPE = 'core-operator-docs'
SERVED = ('core', 'ops', 'ats')
PUBLISHED = ('core', 'ops', 'ats', 'extra')

SERVER_PY = 'def inspect():\n    return custody()\n'
GUIDE = '# Guide\nUse the portable context builder for knowledge.\n'
SCIP_DOCUMENT = {'metadata': {'tool_info': {'name': 'scip-python', 'version': '0.6.6'}},
                 'documents': [{'relative_path': 'server.py', 'occurrences': [
                     {'symbol': 'scip-python python core 1 inspect().', 'symbol_roles': 1,
                      'range': [0, 4, 11]}]}]}


# -- isolation -----------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_network_and_stub_embedder(monkeypatch):
    """No socket leaves the process, and the production embedder is a stub."""
    real_connect = socket.socket.connect

    def refuse(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f'K1 tests make no network connection: {address!r}')
        return real_connect(self, address)
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    from kp_agent_tooling._impl.embeddings.embedders import DeterministicEmbedder
    seams.install_stub_embedder(monkeypatch, lambda **_: DeterministicEmbedder())


# -- the world ------------------------------------------------------------------------------

def git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    return subprocess.check_output(['git', '-C', str(cwd), *args], text=True, env=env,
                                   stderr=subprocess.PIPE).strip()


def commit(repo, files, message):
    for name, text in files.items():
        path = Path(repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', message)
    return git(repo, 'rev-parse', 'HEAD')


def snapshot(upstream, target, revision):
    target.parent.mkdir(parents=True, exist_ok=True)
    git(target.parent, 'clone', '-q', str(upstream), str(target))
    git(target, 'checkout', '-q', '--detach', revision)
    return target


def manifest(capability, baseline, server_blob):
    return json.dumps({'schema_version': 'ops.capability-map.v1', 'capability_id': capability,
                       'summary': f'{capability} fixture capability', 'source_revision': baseline,
                       'references': [{'id': 'server', 'role': 'entrypoint', 'path': 'server.py',
                                       'blob_sha': server_blob}]})


def platform(revision, name):
    return {'schema': 'ops.platform-request.v1', 'owner': 'k1-fixture', 'profile': name,
            'sources': {'core': {'revision': revision,
                                 'artifacts': [{'path': 'server.py', 'role': 'dependency'}]}}}


def scip_index(checkout, revision, path):
    from kp_agent_tooling._impl.scip_navigation import build_index, write_partitioned_index
    built = build_index(str(checkout), revision, 'core', SCIP_DOCUMENT, provenance={'fixture': 'k1'})
    written = write_partitioned_index(path, built)
    return {'path': str(path), 'revision': revision, 'sha256': written['sha256']}


class World:
    """Two refresh generations (A, then B) over one operator overlay."""

    def __init__(self, root):
        self.root = root = Path(root)
        upstream = root / 'upstream'
        for key in PUBLISHED:
            (upstream / key).mkdir(parents=True)
            git(upstream / key, 'init', '-q', '-b', 'main')
            git(upstream / key, 'config', 'user.email', 'k1@example.invalid')
            git(upstream / key, 'config', 'user.name', 'K1 Fixture')
        core = upstream / 'core'
        self.c0 = commit(core, {'server.py': SERVER_PY, 'docs/guide.md': GUIDE,
                                'docs/old.md': '# Old\nRetired guidance.\n'}, 'c0')
        server_blob = git(core, 'rev-parse', 'HEAD:server.py')
        self.guide_blob = git(core, 'rev-parse', 'HEAD:docs/guide.md')
        self.O = commit(core, {'caps/core.json': manifest('core-entry', self.c0, server_blob),
                               'caps/retired.json': manifest('core-retired', self.c0, server_blob)},
                        'operator pin O')
        self.A = commit(core, {'notes/generation.txt': 'A\n'}, 'generation A')
        self.other = {key: commit(upstream / key, {'README.md': f'# {key}\n'}, key)
                      for key in PUBLISHED if key != 'core'}
        # The operator's stale clone was made before B existed (attempt-1's /state/repositories/core).
        self.stale_core = snapshot(core, root / 'operator' / 'stale' / 'core', self.O)
        generations = root / 'published' / 'generations'
        self.paths = {'A': {'core': snapshot(core, generations / 'A' / 'repos' / 'core', self.A)}}
        for key, revision in self.other.items():
            self.paths['A'][key] = snapshot(upstream / key, generations / 'A' / 'repos' / key, revision)
        self.B = commit(core, {'caps/new.json': manifest('core-new', self.c0, server_blob),
                               'notes/generation.txt': 'B\n'}, 'generation B')
        self.paths['B'] = dict(self.paths['A'],
                               core=snapshot(core, generations / 'B' / 'repos' / 'core', self.B))
        self.revisions = {'A': dict(self.other, core=self.A), 'B': dict(self.other, core=self.B)}
        # Amendment 1: `capabilities` is an operator field. Shape (a), the default, is the
        # empty map refresh publishes today; shape (b) is non-empty and differs from the
        # operator's (B adds `core-new`, which only the published map carries).
        self.published_capabilities = {
            'empty': {'A': {}, 'B': {}},
            'differing': {'A': {'core-entry': 'caps/core.json'},
                          'B': {'core-entry': 'caps/core.json', 'core-new': 'caps/new.json'}}}
        self.capability_shape = 'empty'
        self.indexes = {gen: scip_index(self.paths[gen]['core'], self.revisions[gen]['core'],
                                        generations / gen / 'core-python.ops.json')
                        for gen in ('A', 'B')}
        # The operator: a full clone (holds B), its own pins, catalog, corpus and ledger.
        operator = root / 'operator'
        self.operator_paths = {'core': snapshot(core, operator / 'repositories' / 'core', self.O)}
        for key in ('ops', 'ats'):
            self.operator_paths[key] = snapshot(upstream / key, operator / 'repositories' / key,
                                                self.other[key])
        self.operator_index = scip_index(self.operator_paths['core'], self.O,
                                         operator / 'scip' / 'core-O.ops.json')
        self.operator_capabilities = {'core-entry': 'caps/core.json',
                                      'core-retired': 'caps/retired.json'}
        self.operator_artifacts = {'docs/guide.md': {'owner': 'operator', 'status': 'maintained'},
                                   'docs/old.md': {'owner': 'operator', 'status': 'withdrawn'}}
        self.publication = root / 'published' / 'profile.json'
        self.operator_catalog = self.write_operator_catalog('knowledge.json', self.operator_paths['core'])
        self.stale_operator_catalog = self.write_operator_catalog('knowledge-stale.json', self.stale_core)
        from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger
        self.lifecycle = LifecycleLedger.initialize(operator / 'lifecycle.sqlite3').path
        self.corpus, self.corpus_sha = self.write_corpus(operator / 'corpus.json')
        self.profile_sha = {}

    # -- operator files ----------------------------------------------------------------

    def operator_rows(self, core_path):
        rows = {'core': {'path': str(core_path), 'ref': self.O, 'default_branch_ref': 'refs/heads/main',
                         'corpus_scope': OPERATOR_SCOPE, 'tenant_ids': [TENANT_OPERATOR],
                         'capabilities': dict(self.operator_capabilities),
                         'entry_symbols': {'core-entry': 'server.inspect'},
                         'artifacts': json.loads(json.dumps(self.operator_artifacts))}}
        for key in ('ops', 'ats'):
            rows[key] = {'path': str(self.operator_paths[key]), 'ref': self.other[key],
                         'corpus_scope': f'{key}-operator-docs', 'tenant_ids': [TENANT_OPERATOR],
                         'capabilities': {}}
        return rows

    def write_operator_catalog(self, name, core_path):
        path = self.root / 'operator' / 'config' / name
        seams.write_private_json(path, {
            'schema_version': 'ops.knowledge-config.v1', 'repositories': self.operator_rows(core_path),
            'platforms': {'core': platform(self.O, 'operator-hand-copy')},
            'scip_indexes': {'core': [self.operator_index]},
            'navigation': {'provider': 'published_scip', 'profile_path': str(self.publication),
                           'published_root': str(self.root), 'local_root': str(self.root)}})
        return path

    def write_corpus(self, path):
        from kp_agent_tooling._impl.embeddings.embedders import DeterministicEmbedder
        from kp_agent_tooling_ops._impl.embeddings.desk_docs import desk_doc_embedding_revision
        embedder = DeterministicEmbedder()
        revision = desk_doc_embedding_revision(embedder)
        attrs = {'repo_key': OPERATOR_SCOPE, 'path': 'docs/guide.md', 'blob_sha': self.guide_blob,
                 'byte_offset': 0, 'byte_length': len(GUIDE.encode()), 'heading': 'Guide',
                 'local_path': '/retired/checkout', 'is_current': True,
                 'content_digest': hashlib.sha256(GUIDE.encode()).hexdigest()}
        provenance = {k: v for k, v in attrs.items() if k not in {'is_current', 'content_digest'}}
        sha = seams.write_private_json(path, {
            'schema_version': 'agent-tooling.document-corpus.v1',
            'chunks': [{'id': 'deskdoc:guide', 'entity_type': 'DeskDocumentChunk', 'attributes': attrs}],
            'vectors': [{'chunk_content_id': 'deskdoc:guide', 'revision_id': revision.revision_id,
                         'vector': embedder.embed([GUIDE])[0], 'content_digest': attrs['content_digest'],
                         'provenance': provenance, 'content_kind': 'desk_doc',
                         'embedding_model_id': revision.model_id}]})
        return path, sha

    def runtime(self, name, *, tenant=TENANT_OPERATOR, catalog=None, docs_root=None):
        path = self.root / 'operator' / 'config' / f'knowledge-runtime-{name}.json'
        seams.write_private_json(path, {
            'schema_version': seams.RUNTIME_SCHEMA, 'tenant_id': tenant,
            'lifecycle_path': str(self.lifecycle),
            'catalog_path': str(catalog or self.operator_catalog),
            'documents': {'path': str(self.corpus), 'sha256': self.corpus_sha,
                          'repositories': {OPERATOR_SCOPE: str(docs_root or self.operator_paths['core'])}},
            'embedding': {}})
        return path

    def tooling(self, name, *, tenant=TENANT_OPERATOR, profile=True, catalog=None, docs_root=None,
                repos=PUBLISHED):
        """The tooling configuration a deployment serves (its `repos` equal the profile's)."""
        config = {'schema_version': seams.TOOLING_SCHEMA,
                  'repos': {key: {'path': str(self.paths['A'][key]), 'revision': self.revisions['A'][key]}
                            for key in repos},
                  seams.PORTABLE_CONFIG_KEY: str(self.runtime(name, tenant=tenant, catalog=catalog,
                                                              docs_root=docs_root)),
                  'delivery_root': str(self.root / 'delivery' / name)}
        if profile:
            config[seams.PROFILE_CONFIG_KEY] = str(self.publication)
        path = self.root / 'tooling' / f'{name}.json'
        seams.write_private_json(path, config)
        return path

    # -- refresh's publication ----------------------------------------------------------

    def catalog(self, gen, *, tenants=(TENANT_PUBLISHED,), repos=PUBLISHED):
        rows = {}
        for key in repos:
            rows[key] = {'path': str(self.paths[gen][key]), 'ref': self.revisions[gen][key],
                         'corpus_scope': key, 'tenant_ids': list(tenants),
                         'capabilities': (dict(self.published_capabilities[self.capability_shape][gen])
                                          if key == 'core' else {})}
        return {'schema_version': 'ops.knowledge-config.v1', 'repositories': rows,
                'platforms': {'core': platform(self.revisions[gen]['core'], f'generation-{gen}')},
                'scip_indexes': {'core': [dict(self.indexes[gen])]}}

    def profile(self, gen, catalog_path, catalog_sha, *, repos=PUBLISHED, name=None):
        return {'schema_version': 'ops.navigation-profile.v1', 'profile': name or f'generation-{gen}',
                'published_at': '2026-10-07T00:53:00Z',
                'repos': {key: {'path': str(self.paths[gen][key]), 'revision': self.revisions[gen][key],
                                'default_ref': 'refs/heads/main'} for key in repos},
                'knowledge_config': str(catalog_path), 'knowledge_config_sha256': catalog_sha}

    def publish(self, gen, *, name=None, catalog=None, repos=PUBLISHED, profile_repos=None):
        """Write `generations/<name>/{knowledge,profile}.json`, then replace the publication."""
        name = name or gen
        directory = self.root / 'published' / 'generations' / name
        catalog_path = directory / 'knowledge.json'
        catalog_sha = seams.write_private_json(catalog_path, catalog or self.catalog(gen, repos=repos))
        profile = self.profile(gen, catalog_path, catalog_sha, repos=profile_repos or repos,
                               name=f'generation-{name}')
        seams.write_private_json(directory / 'profile.json', profile)
        self.profile_sha[name] = seams.publish_profile(self.publication, profile)
        return catalog_path

    def operator_bytes(self):
        directory = self.root / 'operator' / 'config'
        return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / 'k1')


def symbol_args(revision, repo='core'):
    return {'repo_key': repo, 'target_revision': revision, 'path': 'server.py', 'line': 1}


def assert_answers_at(result, revision):
    assert isinstance(result, dict), result
    gaps = seams.symbol_gaps(result)
    assert seams.status(result) == 'ok', (seams.status(result), gaps, seams.error_of(result))
    assert not gaps, gaps
    assert seams.symbol_results(result), result
    assert seams.answered_revisions(result) == {revision}, seams.answered_revisions(result)


def call_without_raising(server, name, arguments):
    try:
        return seams.call(server, name, arguments)
    except Exception as error:  # P3: never a raised error that a transport would surface
        pytest.fail(f'{name} raised {type(error).__name__}: {error}')


# -- F1 ---------------------------------------------------------------------------------------

def test_f1_a_new_generation_is_followed(world):
    """F1. GREEN-IF: a server built while generation A is published, after generation B
    (a new `ref` AND a new `path` for `core`) is published over A without any operator
    file changing, answers `knowledge.symbol` for `core` at B's ref with status `ok`, no
    gaps, every answered revision (indexed scope and resolved definitions) equal to B's
    ref, and the result carries `navigation_profile.profile_sha256` equal to the sha256 of
    B's published profile. RED at base: it answers from the operator's pin (a gap at B)
    and carries no `navigation_profile`.
    """
    world.publish('A')
    server = seams.build_server(world.tooling('operator'))
    before = world.operator_bytes()
    world.publish('B')
    assert world.operator_bytes() == before
    result = seams.call(server, 'knowledge.symbol', symbol_args(world.B))
    assert_answers_at(result, world.B)
    assert seams.profile_sha(result) == world.profile_sha['B']


# -- F2 ---------------------------------------------------------------------------------------

def test_f2_stale_operator_path_does_not_decide(world):
    """F2. GREEN-IF: with the operator catalog's `core` `path` naming a clone that lacks
    B's commit (the 2026-10-07 attempt-1 regression), a server built after B is published
    still gives F1's observable: `knowledge.symbol` at B's ref answers `ok` with no gaps,
    every answered revision equal to B's ref, `navigation_profile.profile_sha256` equal to
    B's profile, and no `knowledge_unavailable` anywhere in the result.
    """
    with pytest.raises(subprocess.CalledProcessError):
        git(world.stale_core, 'cat-file', '-e', world.B + '^{commit}')
    world.publish('A')
    world.publish('B')
    server = seams.build_server(world.tooling('stale', catalog=world.stale_operator_catalog,
                                              docs_root=world.stale_core))
    result = seams.call(server, 'knowledge.symbol', symbol_args(world.B))
    assert 'knowledge_unavailable' not in json.dumps(result)
    assert_answers_at(result, world.B)
    assert seams.profile_sha(result) == world.profile_sha['B']


# -- F3 ---------------------------------------------------------------------------------------

def test_f3_operator_fields_survive(world):
    """F3. GREEN-IF: after following B (`knowledge.symbol` at B's ref answers at B):
    `knowledge.context` for the operator's `core-entry` at B resolves the operator's entry
    symbol `server.inspect` through the operator's `navigation` section (navigation status
    `ok`, `entry_symbol` `server.inspect`); `knowledge.retrieve` at B admits exactly the
    operator's declared maintained artifacts (`declared_artifacts` 1, the published
    catalog declares none); the operator's tenant answers; and a server whose tenant is
    the published catalog's tenant is refused on the same call.
    """
    world.publish('A')
    world.publish('B')
    server = seams.build_server(world.tooling('operator'))
    assert_answers_at(seams.call(server, 'knowledge.symbol', symbol_args(world.B)), world.B)
    context = seams.call(server, 'knowledge.context',
                         {'repo_key': 'core', 'capability_id': 'core-entry', 'target_revision': world.B})
    assert seams.context_navigation(context) == ('ok', 'server.inspect'), context
    retrieved = seams.call(server, 'knowledge.retrieve',
                           {'repo_key': 'core', 'query': 'portable context builder', 'target_revision': world.B})
    assert seams.declared_artifacts(retrieved) == 1, retrieved
    published_tenant = seams.build_server(world.tooling('published-tenant', tenant=TENANT_PUBLISHED))
    assert seams.tenant_refused(lambda: seams.call(published_tenant, 'knowledge.symbol',
                                                   symbol_args(world.B)))


# -- F4 ---------------------------------------------------------------------------------------

def test_f4_published_only_repository_is_not_served(world):
    """F4 (guard, GREEN at base). GREEN-IF: with the published catalog listing a fourth
    repo (`extra`) that the operator catalog does not serve, the operator's tenant gets an
    answer for a served repo (`knowledge.capabilities` for `core`), while `extra` is absent
    from the knowledge tools' advertised repo keys and `knowledge.capabilities`,
    `knowledge.platform` and `knowledge.symbol` for `extra` each answer nothing (a refusal
    or a raised refusal, never an `ok`/`partial` answer with rows).
    """
    world.publish('A')
    world.publish('B')
    server = seams.build_server(world.tooling('operator'))
    ok, detail = seams.answered(lambda: seams.call(server, 'knowledge.capabilities', {'repo_key': 'core'}))
    assert ok, detail
    assert 'extra' not in seams.advertised_repositories(server)
    for name, arguments in (('knowledge.capabilities', {'repo_key': 'extra'}),
                            ('knowledge.platform', {'repo_key': 'extra'}),
                            ('knowledge.symbol', {'repo_key': 'extra', 'target_revision': world.other['extra'],
                                                  'path': 'README.md', 'line': 1})):
        ok, detail = seams.answered(lambda: seams.call(server, name, arguments))
        assert not ok, (name, detail)


# -- F5 ---------------------------------------------------------------------------------------

def _drop(catalog, section, key):
    catalog[section].pop(key)
    return catalog


F5_CASES = ['identity', 'membership', 'source_differs', 'served_unpublished',
            'platform_missing', 'scip_missing']


@pytest.mark.parametrize('case', F5_CASES)
def test_f5_refusals_name_the_case_and_answer_nothing(world, case):
    """F5. GREEN-IF, for each case: the call returns (never raises) a refusal whose status is
    in the knowledge refusal vocabulary and whose reason is the case's word, with no answer
    rows and no answered revision from A, B or the operator's pin. The server answered at A
    before the faulty publication (except `served_unpublished`, whose fault exists from the
    start). Cases and words:
    - `identity`: B's catalog bytes differ from the profile's `knowledge_config_sha256`
      (active() passes through "navigation catalog identity mismatch");
    - `membership`: B's catalog omits served `ats` while the profile lists it
      (active() passes through "catalog repository membership mismatch");
    - `source_differs`: B's catalog gives `core` a ref that is not the profile's
      (active() passes through "catalog source differs from active profile");
    - `served_unpublished`: the operator serves `ats`, which the profile, the tooling `repos`
      and the published catalog all omit (FEATURE's word);
    - `platform_missing` / `scip_missing`: B's catalog has no `platforms` / `scip_indexes`
      entry that served `core` needs (FEATURE's one word for this case).
    """
    if case == 'served_unpublished':
        repos = ('core', 'ops', 'extra')
        world.publish('A', repos=repos)
        server = seams.build_server(world.tooling('operator', repos=repos))
        calls = [('knowledge.capabilities', {'repo_key': 'ats'})]
        word = 'served_unpublished'
    else:
        world.publish('A')
        server = seams.build_server(world.tooling('operator'))
        assert_answers_at(seams.call(server, 'knowledge.symbol', symbol_args(world.A)), world.A)
        catalog = world.catalog('B')
        if case == 'identity':
            path = world.publish('B', name='B-identity')
            path.write_bytes(path.read_bytes() + b'\n')
        elif case == 'membership':
            world.publish('B', name='B-membership', catalog=_drop(catalog, 'repositories', 'ats'))
        elif case == 'source_differs':
            catalog['repositories']['core']['ref'] = world.A
            world.publish('B', name='B-source', catalog=catalog)
        elif case == 'platform_missing':
            world.publish('B', name='B-platform', catalog=_drop(catalog, 'platforms', 'core'))
        else:
            world.publish('B', name='B-scip', catalog=_drop(catalog, 'scip_indexes', 'core'))
        word = {'identity': 'identity', 'membership': 'membership', 'source_differs': 'source_differs',
                'platform_missing': 'generation_entry_missing',
                'scip_missing': 'generation_entry_missing'}[case]
        calls = [('knowledge.symbol', symbol_args(world.B))]
        if word in ('identity', 'membership', 'source_differs'):
            calls.append(('knowledge.capabilities', {'repo_key': 'core'}))
    for name, arguments in calls:
        result = call_without_raising(server, name, arguments)
        reason = seams.refusal_reason(result)
        assert seams.reason_matches(reason, word), (name, case, reason, seams.status(result), result)
        assert seams.status(result) in seams.REFUSAL_STATUSES, result
        assert seams.answer_rows(result) == [], result
        assert not seams.answered_revisions(result) & {world.O, world.A, world.B}, result


# -- F6 ---------------------------------------------------------------------------------------

def test_f6_configured_pins_unchanged_without_a_profile(world):
    """F6 (guard, GREEN at base). GREEN-IF: with no `navigation_profile` configured (while a
    generation B is published on disk), every knowledge call answers byte for byte what the
    knowledge service over the operator's catalog answers for the operator's tenant:
    `capabilities`, `symbol` at the operator's pin O and at B, `retrieve` at O, and
    `context` at O. None of the results carries a `navigation_profile` field.
    """
    from kp_agent_tooling._impl.embeddings.embedders import DeterministicEmbedder
    from kp_agent_tooling_ops._impl.service.document_corpus import DocumentCorpus
    from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService
    from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger
    world.publish('A')
    world.publish('B')
    server = seams.build_server(world.tooling('pins', profile=False))
    reader = DocumentCorpus(world.corpus, sha256=world.corpus_sha,
                            repositories={OPERATOR_SCOPE: str(world.operator_paths['core'])},
                            embedder=DeterministicEmbedder())
    reference = KnowledgeService(world.operator_catalog, lambda: reader,
                                 lifecycle=LifecycleLedger(world.lifecycle))
    calls = [('capabilities', {'repo_key': 'core'}),
             ('symbol', symbol_args(world.O)),
             ('symbol', symbol_args(world.B)),
             ('retrieve', {'repo_key': 'core', 'query': 'portable context builder', 'target_revision': world.O}),
             ('context', {'repo_key': 'core', 'capability_id': 'core-entry', 'target_revision': world.O})]
    for operation, arguments in calls:
        served = seams.call(server, 'knowledge.' + operation, arguments)
        expected = reference.execute_for_tenant(operation, dict(arguments), TENANT_OPERATOR)
        assert seams.PROFILE_FIELD not in served
        assert json.dumps(served, sort_keys=True) == json.dumps(expected, sort_keys=True), operation
    assert_answers_at(seams.call(server, 'knowledge.symbol', symbol_args(world.O)), world.O)


# -- F7 ---------------------------------------------------------------------------------------

def test_f7_profile_advance_without_reload_and_reuse_on_same_generation(world, monkeypatch):
    """F7. GREEN-IF: in one server process, two `knowledge.symbol` calls at generation A both
    answer at A with the same knowledge service object, and two `knowledge.retrieve` calls
    at A open the operator's document corpus no more often than the first did (no corpus
    reopened on the same generation); then, after B is published, the next call raises
    nothing (no "reload required") and answers at B, and a further call at B reuses the
    service object that answered at B.
    """
    opened = seams.count_corpus_opens(monkeypatch)
    world.publish('A')
    server = seams.build_server(world.tooling('operator'))
    assert_answers_at(seams.call(server, 'knowledge.symbol', symbol_args(world.A)), world.A)
    service_a = seams.knowledge_service(server)
    assert_answers_at(seams.call(server, 'knowledge.symbol', symbol_args(world.A)), world.A)
    assert seams.knowledge_service(server) is service_a
    retrieve = {'repo_key': 'core', 'query': 'portable context builder', 'target_revision': world.A}
    seams.call(server, 'knowledge.retrieve', retrieve)
    after_first = len(opened)
    seams.call(server, 'knowledge.retrieve', retrieve)
    assert len(opened) == after_first, 'document corpus reopened on the same generation'
    assert seams.knowledge_service(server) is service_a
    world.publish('B')
    result = call_without_raising(server, 'knowledge.symbol', symbol_args(world.B))
    assert_answers_at(result, world.B)
    service_b = seams.knowledge_service(server)
    assert_answers_at(seams.call(server, 'knowledge.symbol', symbol_args(world.B)), world.B)
    assert seams.knowledge_service(server) is service_b


# -- F8 ---------------------------------------------------------------------------------------

def test_f8_published_catalog_never_widens(world):
    """F8 (guard, GREEN at base). GREEN-IF, for a published B whose tenant ids are
    different (`tenant-refresh` only) and then extra (`tenant-operator` plus
    `tenant-published-extra`), and whose `core` `corpus_scope` differs from the operator's:
    the operator's tenant answers `knowledge.retrieve` for `core` at B from the operator's
    corpus scope (report scope and every row's scope `core-operator-docs`, the guide's
    text); a server whose tenant appears only in the published catalog is refused for the
    served `core` and for the published-only fourth repo `extra`.
    """
    world.publish('A')
    world.publish('B')
    operator = seams.build_server(world.tooling('operator'))
    query = {'repo_key': 'core', 'query': 'portable context builder', 'target_revision': world.B}

    def operator_answers_from_its_corpus():
        result = seams.call(operator, 'knowledge.retrieve', query)
        assert seams.status(result) == 'ok', result
        report_scopes, row_scopes, texts = seams.retrieval_scopes(result)
        assert report_scopes == [OPERATOR_SCOPE], result
        assert row_scopes and set(row_scopes) == {OPERATOR_SCOPE}, row_scopes
        assert texts[0] == GUIDE

    for label, tenant in (('different', TENANT_PUBLISHED), ('extra', TENANT_EXTRA)):
        if label == 'extra':
            world.publish('B', name='B-extra-tenants',
                          catalog=world.catalog('B', tenants=(TENANT_OPERATOR, TENANT_EXTRA)))
        operator_answers_from_its_corpus()
        outsider = seams.build_server(world.tooling(f'outsider-{label}', tenant=tenant))
        assert seams.tenant_refused(lambda: seams.call(outsider, 'knowledge.retrieve', query)), label
        assert seams.tenant_refused(lambda: seams.call(outsider, 'knowledge.capabilities',
                                                       {'repo_key': 'extra'})), label


# -- F9 ---------------------------------------------------------------------------------------

@pytest.mark.parametrize('shape', ['empty', 'differing'])
def test_f9_capabilities_stay_the_operators(world, shape):
    """F9, inverted by amendment 1 (guard, GREEN at base). GREEN-IF, in each shape of the
    published capability map for `core`:
    - (a) `empty`: `{}` in A and in B, as refresh writes it today;
    - (b) `differing`: non-empty and different from the operator's (A: `core-entry`;
      B: `core-entry` and `core-new`, a capability document only B adds);
    a server built while A is published lists through `knowledge.capabilities` for `core`
    exactly the operator's map (`core-entry` -> `caps/core.json`, `core-retired` ->
    `caps/retired.json`); after B is published, with no operator edit, the next call lists
    exactly the operator's map again, and no capability id or manifest path that only the
    published map carries (`core-new`, `caps/new.json`) appears.
    """
    world.capability_shape = shape
    world.publish('A')
    server = seams.build_server(world.tooling('operator'))

    def listed():
        result = seams.call(server, 'knowledge.capabilities', {'repo_key': 'core'})
        assert seams.status(result) == 'ok', result
        return seams.capability_map(result)
    assert listed() == world.operator_capabilities
    before = world.operator_bytes()
    world.publish('B')
    assert world.operator_bytes() == before
    listing = listed()
    assert listing == world.operator_capabilities
    published = world.published_capabilities[shape]['B']
    only_published = {cap: path for cap, path in published.items()
                      if world.operator_capabilities.get(cap) != path}
    assert not set(only_published) & set(listing), listing
    assert not set(only_published.values()) & set(listing.values()), listing
