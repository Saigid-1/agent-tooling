"""P1 golden corpus generator (docs/work/orders/T11a-leaf-consolidation.md, P1).

Generated at base (main after T10h, plus the frozen order) by:

    python tests/fixtures/t11a/generate_identity_corpus.py

which rewrites tests/fixtures/t11a/identity_corpus.json. tests/test_t11a_p1_identity_corpus.py
recomputes it with ``build()`` and compares the two texts byte for byte.

Every scheme is called through the public or old name the order lists (P9 keeps them
importable), over one fixed input set: non-ASCII text, nested values, empty values, and
NaN/Infinity (recorded as an output where the variant allows it, as the error otherwise).
The corpus is compared on every CPython >= 3.11: the json refusal of NaN/Infinity is recorded
without the value CPython 3.12 appends to its message (the only measured version difference).
Inline sorted_json digest sites, which have no name of their own, are driven through
their public enclosing operation: the digest is read where the product compares it
(``t11a_golden.Probe``) or where it stores it, with only collaborators patched.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tests/

from t11a_golden import Probe, golden, jsonable, patched, scratch, write_golden  # noqa: E402

# The core part runs with the core alone (the CI core job); the ops part needs extensions/ops.
GOLDENS = {'core': 'identity_corpus.json', 'ops': 'identity_corpus_ops.json'}

VALUES = (
    ('empty_object', {}),
    ('empty_list', []),
    ('empty_string', ''),
    ('null', None),
    ('true', True),
    ('zero', 0),
    ('negative_zero', -0.0),
    ('float', 1.5),
    ('large_float', 1e300),
    ('large_int', 2 ** 70),
    ('ascii_nested', {'b': 1, 'a': [1, 2, {'d': None, 'c': True}], 'e': {'g': [], 'f': {}}}),
    ('non_ascii', {'ключ': 'значение', 'emoji': '😀 𝄞', 'é': ['ü', 'ß'], 'cjk': '漢字'}),
    ('escapes', {'s': 'tab\tnl\nnul\x00quote"bs\\ sep\u2028\u2029 del\x7f'}),
    ('deep', {'z': {'y': {'x': [[], {}, [{'w': ''}], [[[]]]]}}}),
    ('key_order', {'b': 1, 'B': 2, 'á': 3, 'a': 4, '_': 5, '10': 6, '9': 7}),
    ('list_of_mixed', [None, 1, 'one', 1.25, [], {}, False]),
    ('nan', float('nan')),
    ('infinity', float('inf')),
    ('nan_nested', {'v': [float('nan'), float('-inf')]}),
    ('mixed_key_types', {1: 'int key', '1': 'str key'}),
    # Lone surrogates: escaped by an ASCII form, unencodable as UTF-8 by a UTF-8 form, so the
    # error's type, text and chain are part of each scheme (Verification U1).
    ('lone_surrogate_high', '\ud800'),
    ('lone_surrogate_low', '\udc00'),
    ('lone_surrogates_nested', {'a': ['ok', '\ud800'], 'b': {'\udc00key': 'x\udc00y'}}),
)

# The one version-dependent text in the corpus (measured at base): CPython 3.12 appends the value
# to the stdlib json refusal of NaN/Infinity ("...not JSON compliant: nan"); 3.11 does not. The
# refusal itself (type and text) stays in the corpus; only the appended value is dropped.
JSON_OUT_OF_RANGE = re.compile(r'^(ValueError: Out of range float values are not JSON compliant)(: .*)?$')


def normalised_error(text):
    return JSON_OUT_OF_RANGE.sub(r'\1', text)


def error_record(error):
    """An exception as the corpus holds it: type and text, and the chain's shape."""
    return {'error': normalised_error(f'{type(error).__name__}: {error}'),
            'cause': None if error.__cause__ is None else type(error.__cause__).__name__,
            'suppress_context': error.__suppress_context__}


def outcome(call, *args, **kwargs):
    try:
        return {'value': jsonable(call(*args, **kwargs))}
    except Exception as error:  # the type, the text and the chain are part of the golden
        return error_record(error)


TEXTS = ('plain', '', 'café ☃ 𝄞', 'pipe|inside', 'nul\x00inside', ' padded ', '漢字/路径.py', '\ud800', 'x\udc00y')


def values_of(scheme, function, *, prefix=()):
    return {case: outcome(function, *prefix, value) for case, value in VALUES}


def corpus_core():
    from kp_agent_tooling._impl.service import episodic_memory, desk_identity, desk_write_scope
    from kp_agent_tooling._impl.service import episodic_queue, memory_budget, session_bindings
    from kp_agent_tooling._impl.service import claude_episode_capture, host_adapter, launch_binding
    from kp_agent_tooling._impl.embeddings import revision
    from kp_agent_tooling._impl import scip_navigation, workspace_setup
    from kp_agent_tooling import refresh_cli

    out = {}
    for kind in ('episode', 'claim', ''):
        out[f'episodic_memory._id[{kind!r}]'] = values_of(None, episodic_memory._id, prefix=(kind,))
    out['episodic_memory._bytes'] = values_of(None, episodic_memory._bytes)
    out['desk_identity.binding_key'] = {
        f'{t!r}|{r!r}|{k!r}': outcome(desk_identity.binding_key, tenant_id=t, role=r, repo_key=k)
        for t in TEXTS for r in ('coordinator', 'vérification') for k in ('agent-tooling', 'répo')}
    for namespace in ('embedding-revision', 'ünïcode', ''):
        out[f'revision._sha256_identity[{namespace!r}]'] = values_of(
            None, revision._sha256_identity, prefix=(namespace,))
    revisions = {
        'default': dict(model_id='model', model_digest='sha256:' + 'a' * 64),
        'surrogate': dict(model_id='mod\ud800', model_digest='sha256:' + 'd' * 64, params={'k': '\udc00'}),
        'params': dict(model_id='modèle', model_digest='sha256:' + 'b' * 64,
                       params={'dim': 384, 'é': [1.5, None]}, normalization={'unit': True},
                       chunking_spec={'window': 2000, 'overlap': 0}),
    }
    out['revision.embedding_identity'] = {}
    for name, fields in revisions.items():
        made = outcome(lambda: revision.EmbeddingRevision(**fields).revision_id)
        out['revision.embedding_identity'][f'{name}.revision_id'] = made
        for content in ('sha256:' + 'c' * 64, 'é', '', '\ud800', 'x\udc00y'):
            out['revision.embedding_identity'][f'{name}|{content!r}'] = outcome(
                lambda: revision.embedding_identity(content, revision.EmbeddingRevision(**fields)))
    out['scip_navigation.digest'] = values_of(None, scip_navigation.digest)
    out['desk_write_scope._digest'] = values_of(None, desk_write_scope._digest)
    out['episodic_queue._digest'] = values_of(None, episodic_queue._digest)
    out['memory_budget._digest'] = values_of(None, memory_budget._digest)
    out['session_bindings._digest'] = values_of(None, session_bindings._digest)
    out['workspace_setup.digest'] = values_of(None, workspace_setup.digest)
    out['refresh_cli.digest_json'] = values_of(None, refresh_cli.digest_json)

    chain, previous = {}, '0' * 64
    lines = (b'{"type":"user"}\n', 'é ☃\n'.encode(), b'', b'\n', b'\xff\xfe not utf-8\n', b'no newline')
    for index, line in enumerate(lines):
        result = outcome(claude_episode_capture._digest, previous, line)
        chain[f'{index}:{line.hex()}'] = result
        previous = result.get('value', previous)
    chain['bad_previous'] = outcome(claude_episode_capture._digest, 'not-hex', b'x')
    out['claude_episode_capture._digest (hash chain)'] = chain

    commands = ('kp-agent-host hook --event x', 'cmd "quoted" \\ back', 'café ☃', 'cmd \ud800')
    events = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'Stop')
    sources = ('/home/user/.codex/config.toml', 'C:\\Users\\user\\.codex\\config.toml',
               '/<session-flags>/config.toml', 'C:\\<session-flags>\\config.toml')
    out['host_adapter.codex_trust_entry (timeout 5, no matcher, given source)'] = {
        f'{s!r}|{e}|{c!r}|{g}': outcome(host_adapter.codex_trust_entry, s, e, c, g)
        for s in sources for e in events for c in commands for g in (0, 3)}
    trust = {}
    for os_name in ('posix', 'nt'):
        with patched(os, 'name', os_name):
            for matcher in (None, 'Bash', '*', 'é|ü'):
                for e in events:
                    for c in commands:
                        for g in (0, 3):
                            trust[f'os.name={os_name}|matcher={matcher!r}|{e}|{c!r}|{g}'] = outcome(
                                launch_binding.codex_trust_entry, e, c, g, matcher=matcher)
    out['launch_binding.codex_trust_entry (timeout 30, matcher, session-flags source)'] = trust
    return out


def corpus_ops():
    from kp_agent_tooling_ops._impl.code_references import models, retrieval, artifact_identity
    from kp_agent_tooling_ops._impl.service import document_coordinates
    from kp_agent_tooling_ops._impl import behavior_model, observations, review_ledger, verification_finding

    out = {}
    for prefix in ('reference', 'artifact:x', ''):
        out[f'code_references.models.canonical_digest[{prefix!r}]'] = values_of(
            None, models.canonical_digest, prefix=(prefix,))
    out['behavior_model.digest'] = values_of(None, behavior_model.digest)
    out['observations.digest'] = values_of(None, observations.digest)
    out['review_ledger.canonical_digest'] = values_of(None, review_ledger.canonical_digest)
    out['verification_finding.finding_digest'] = values_of(None, verification_finding.finding_digest)

    coordinates = (('agent-tooling', 'docs/README.md', 'a' * 40, 0, 512),
                   ('répo', 'docs/漢字 file.md', 'b' * 40, 4096, 1),
                   ('k', '', '', 0, 0),
                   ('répo', 'x\udc00.md', 'c' * 40, 7, 9))
    out['deskdoc: retrieval._chunk_id'] = {
        repr(c): outcome(retrieval._chunk_id, dict(repo_key=c[0], path=c[1], blob_sha=c[2],
                                                   byte_offset=c[3], byte_length=c[4])) for c in coordinates}
    out['deskdoc: document_coordinates._chunk_id'] = {
        repr(c): outcome(document_coordinates._chunk_id, *c) for c in coordinates}

    out['artifact_identity.change_lineage_key'] = {
        f'{r!r}|{p!r}': outcome(artifact_identity.change_lineage_key, r, p)
        for r in ('agent-tooling', 'répo', '', '\ud800') for p in ('src/a.py', '漢字.py', 'nul\x00', 'x\udc00.py')}
    out['artifact_identity.change_occurrence_id'] = {
        f'{r!r}|{c!r}|{p!r}|{b!r}': outcome(artifact_identity.change_occurrence_id, r, c, p, b)
        for r in ('agent-tooling', 'répo') for c in ('c' * 40,) for p in ('src/a.py', '漢字.py', 'x\udc00.py')
        for b in (None, 'b' * 40, '')}
    return out


# ---------------------------------------------------------------- inline sorted_json sites

def snapshot_site():
    """commit_rationale.py:689, :693, :695 through RationaleCatalog.snapshot()."""
    from kp_agent_tooling_ops._impl.commit_rationale import RationaleCatalog
    out = {}
    for case, value in VALUES:
        catalog = object.__new__(RationaleCatalog)
        catalog.entries = (
            {'id': 'reviewed-1', 'review_state': 'reviewed', 'lifecycle': 'current', 'payload': value},
            {'id': 'draft-2', 'review_state': 'draft', 'lifecycle': 'current', 'payload': value},
        )
        catalog.compound_questions = [{'repo_key': 'répo', 'target_revision': 'a' * 40,
                                       'questions': ['why?', 'pourquoi ☃'], 'payload': value}]
        out[case] = outcome(catalog.snapshot)
    return out


def _host(value, profile):
    return {'profile': profile, 'pool_size': 8, 'payload': value, 'note': 'hôte ☃'}


def validate_join_site():
    """verification_packet.py:63 through validate_join(); the receipt is a Probe."""
    import hashlib
    from kp_agent_tooling_ops._impl import verification_packet as vp
    out = {}
    with patched(vp, 'render_markdown', lambda gate: None):
        for case, value in VALUES:
            candidate, runner, transform = 'def graph_query(): pass\n', 'runner ☃', 'transform'
            host = dict(_host(value, vp.PROFILE), source={},
                        candidate_sha256=hashlib.sha256(candidate.encode()).hexdigest(),
                        runner_sha256=hashlib.sha256(runner.encode()).hexdigest())
            gate = {'source': {}, 'candidate_sources': {vp.PROFILE: candidate},
                    'runner_sha256': hashlib.sha256(transform.encode()).hexdigest()}
            seen = []
            audit = {'receipts_sha256': {'thread-8-a': Probe(seen)}}
            result = outcome(vp.validate_join, gate, host, audit, {}, runner, transform)
            out[case] = {'result': result, 'compared_digest': seen}
    return out


def adjacency_site():
    """verification_adjacency.py:80 through assemble_adjacent(); receipts are Probes."""
    from kp_agent_tooling_ops._impl import verification_adjacency as va
    from kp_agent_tooling_ops._impl import verification_packet as vp
    out = {}
    for case, value in VALUES:
        seen = []
        audit = {'receipts_sha256': {label: Probe(seen) for label in ('async-8-a', 'async-8-b')}}
        hosts = {'records/lifecycle-30/async-8-a.json.gz': _host(value, 'mismatched-profile')}

        def artifact(repo, rev, path, hosts=hosts):
            if path.endswith('load.json'):
                return {'runs': [{'label': 'async-8-a'}, {'label': 'async-8-b'}]}, {'path': path}
            if path in hosts:
                return hosts[path], {'path': path}
            return {}, {'path': path}

        with patched(vp, 'assemble', lambda config: {'collector': {'receipt': audit}}), \
                patched(vp, 'artifact', artifact), patched(va, 'source', lambda repo, rev, path: ('blob', 'runner')):
            result = outcome(va.assemble_adjacent, {'evidence_repo': '/repo', 'evidence_revision': 'r'},
                             'ats-worker-performance')
        out[case] = {'result': result, 'compared_digest': seen}
    return out


def correctness_site():
    """verification_correctness.py:29 through correctness(); receipts are Probes."""
    import hashlib
    from kp_agent_tooling_ops._impl import verification_correctness as vc
    from kp_agent_tooling_ops._impl import verification_packet as vp
    client = 'def one():\n    pass\n\nfor path, expected in [("a", 1)]:\n    pass\n'
    fixture, runner = 'select 1;', 'runner'
    texts = {'scripts/lifecycle_workload_cli.py': client, 'records/lifecycle-30/fixture.sql': fixture,
             'scripts/lifecycle_workload_server.py': runner}
    labels = [f'{mode}-{pool}-{order}' for mode in ('async', 'thread') for pool in (2, 8) for order in ('a', 'b')]
    out = {}
    for case, value in VALUES:
        seen = []
        load = {'client_sha256': hashlib.sha256(client.encode()).hexdigest(),
                'fixture_sha256': hashlib.sha256(fixture.encode()).hexdigest(),
                'runs': [{'label': label} for label in labels]}

        def artifact(repo, rev, path, load=load, value=value):
            if path.endswith('load.json'):
                return load, {'path': path}
            return _host(value, 'mismatched-profile'), {'path': path}

        base = {'collector': {'receipt': {'receipts_sha256': {label: Probe(seen) for label in labels}}}}
        with patched(vp, 'artifact', artifact), patched(vc, 'source', lambda repo, rev, path: ('blob', texts[path])):
            result = outcome(vc.correctness, {'evidence_repo': '/repo', 'evidence_revision': 'r'}, base, {})
        out[case] = {'result': result, 'compared_digest': seen}
    return out


class _Stop(Exception):
    pass


def agent_tooling_site():
    """agent_tooling.py:66: the delivery namespace of AgentTooling(config)."""
    from kp_agent_tooling._impl import tool_delivery
    from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
    out = {}
    with scratch('t11a-p1-tooling-') as root:
        for case, value in VALUES:
            config = {'schema_version': 'ops.agent-tooling.v1', 'repos': {}, 'x-t11a-corpus': value}
            path = root / f'{case}.json'
            try:
                path.write_text(json.dumps(config, allow_nan=True))
            except ValueError as error:
                out[case] = error_record(error)
                continue
            seen = []

            def store(delivery_root, **kwargs):
                seen.append(Path(delivery_root).name)
                raise _Stop

            with patched(tool_delivery, 'DeliveryStore', store):
                try:
                    AgentTooling(path)
                    out[case] = {'error': 'constructed without reaching DeliveryStore'}
                except _Stop:
                    out[case] = {'value': seen[0]}
                except Exception as error:
                    out[case] = error_record(error)
    return out


def workspace_capture_site():
    """workspace_capture.py:269: the policy_sha256 WorkspaceCapture.initialize() stores."""
    from kp_agent_tooling._impl.service import workspace_capture as wc
    from test_portable_desk_memory import episode_store
    out = {}
    with scratch('t11a-p1-capture-') as root:
        for index, (case, value) in enumerate(VALUES):
            folder = root / str(index)
            folder.mkdir()
            store = episode_store(folder)
            with patched(wc, 'validate_policy', lambda policy: {}):
                try:
                    capture = wc.WorkspaceCapture(store, {'t11a-corpus': value, 'tenant': 'ténant'})
                    capture.initialize()
                except Exception as error:
                    out[case] = error_record(error)
                    continue
            path = store.path.with_name('workspace-capture.sqlite3')
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute("SELECT value FROM state WHERE key='policy_sha256'").fetchone()
            out[case] = {'value': row[0] if row else None}
    return out


def scip_entry_site():
    """scip_entry.py:41: paths_sha256, the unsorted wire hash (allowlisted under P2)."""
    from kp_agent_tooling._impl.service import scip_entry
    path_sets = {'empty': [], 'ascii': ['b.py', 'a.py', 'src/c.py'],
                 'non_ascii': ['漢字.py', 'café/ü.py', 'a b.py'], 'escapes': ['q"uote.py', 'back\\slash.py']}
    out = {}
    for case, paths in path_sets.items():
        envelope = {'data': {'repo_key': 'repo', 'blobs': {p: 'b' * 40 for p in paths}}}
        config = {'platforms': {'repo': {'sources': {'repo': {'revision': 'r' * 40}}}},
                  'scip_indexes': {'repo': [{'revision': 'r' * 40, 'path': '/index', 'sha256': 'x'}]},
                  'repositories': {'repo': {'path': '/repo'}}}
        with patched(scip_entry, 'load_partitioned_index', lambda *a, envelope=envelope, **k: envelope), \
                patched(scip_entry, 'validate_index', lambda env, revision: env['data']), \
                patched(scip_entry, 'lookup', lambda *a, **k: {'omitted': 0, 'occurrences': []}), \
                patched(scip_entry, 'definitions', lambda *a, **k: []):
            result = outcome(scip_entry.symbol_report, config, 'repo', 'r' * 40, paths[0] if paths else 'x.py', 1)
        scope = result.get('value', {}).get('data', {}).get('indexed_scope') if 'value' in result else None
        out[case] = {'indexed_scope': scope} if scope is not None else result
    return out


def search_cursor_site():
    """navigation_search_pages.py:53 (_save): the search cursor's content address."""
    from kp_agent_tooling._impl import navigation_search_pages as nsp
    out = {}
    with scratch('t11a-p1-cursor-') as root:
        for index, (case, value) in enumerate(VALUES):
            folder = root / str(index)
            folder.mkdir(mode=0o700)
            out[case] = outcome(nsp._save, folder, {'query': 'é ☃', 'offset': 3, 'state': value})
    return out


def snapshot_capture_site():
    """navigation_snapshot.py:92/:104/:108 (capture): frozen profile, analysis config and selection.

    Repository paths are fixed absolute strings (capture and active() check only their form), so
    every hashed byte is independent of the scratch directory."""
    from kp_agent_tooling._impl import navigation_snapshot
    out = {}
    with scratch('t11a-p1-snapshot-') as root:
        for index, (case, value) in enumerate(VALUES):
            folder = root / str(index)
            registry = folder / 'registry'
            registry.mkdir(parents=True)
            repos = {'répo': {'path': '/t11a/répo', 'revision': 'a' * 40}, 'b': {'path': '/t11a/b', 'revision': 'b' * 40}}
            profile = folder / 'profile.json'
            try:
                profile.write_text(json.dumps({'schema_version': 'ops.navigation-profile.v1', 'profile': 'dev-current',
                                               'repos': repos, 'published_at': 'fixture', 'x-t11a': value,
                                               'check_status': 'mutable'}, allow_nan=True))
            except (TypeError, ValueError) as error:
                out[case] = error_record(error)
                continue
            config = {'repos': repos, 'navigation_profile': str(profile),
                      'serena': {'command': '/provider/serena', 'note': value},
                      'import_mappings': {'pkg': {'repo_key': 'b', 'distribution': 'dist-é'}}}
            out[case] = outcome(navigation_snapshot.capture, config, registry)
    return out


def publish_write_site():
    """knowledge_publish_cli.py:31 (_write): the hash of the canonical file it writes (ops)."""
    from kp_agent_tooling_ops import knowledge_publish_cli
    out = {}
    with scratch('t11a-p1-publish-') as root:
        for index, (case, value) in enumerate(VALUES):
            out[case] = outcome(knowledge_publish_cli._write, root / f'{index}.json', value)
    return out


_TOKEN_TEXT = re.compile(r'"(device|inode)":\d+')


def _token_reading(token, root):
    """A plan token read through its own framing: the decoded body with the scratch path and the
    source's device/inode replaced, and whether the token is urlsafe base64 (unpadded) of that
    body, a dot, and the body's sha256. Device, inode and path vary by run, so the body cannot be
    compared as raw bytes; everything else in it can."""
    import base64
    import hashlib
    body, _, digest = token.rpartition('.')
    raw = base64.urlsafe_b64decode(body + '=' * (-len(body) % 4))
    text = _TOKEN_TEXT.sub(lambda m: f'"{m.group(1)}":"<{m.group(1)}>"', raw.decode('ascii'))
    return {'body': text.replace(str(root), '<root>'),
            'body_is_unpadded_urlsafe_base64_of_it': body == base64.urlsafe_b64encode(raw).decode().rstrip('='),
            'digest_is_sha256_of_body': digest == hashlib.sha256(raw).hexdigest()}


def plan_token_site():
    """session_import_job plan token (census-missed scheme, FEATURE classification (b)): read
    through SessionImportJobs.preview(), resumed through apply(), refused for junk tokens."""
    import base64
    import hashlib
    from test_session_import_job import NATIVE, assistant, meta, setup, user
    out = {}

    def framed(raw):
        return base64.urlsafe_b64encode(raw).decode().rstrip('=') + '.' + hashlib.sha256(raw).hexdigest()

    with scratch('t11a-p1-token-') as root:
        for mode in ('full', 'current-turn-and-forward'):
            folder = root / mode
            folder.mkdir()
            store, jobs, source, request = setup(folder, mode=mode)
            source.write_bytes(meta() + user('héllo ☃') + assistant('réponse') + user('later', kind='response_item'))
            preview = jobs.preview(request)
            token = preview['plan_token']
            reading = _token_reading(token, folder)
            first = outcome(jobs.apply, token, consent=True)
            again = outcome(jobs.apply, token, consent=True)
            reading['apply'] = {k: v for k, v in first.get('value', first).items() if k in ('phase', 'error')}
            reading['resume_is_the_same_job'] = (first.get('value', {}).get('job_id') ==
                                                 again.get('value', {}).get('job_id') is not None)
            out[mode] = reading
        (root / 'junk-world').mkdir()
        store, jobs, source, request = setup(root / 'junk-world')
        source.write_bytes(meta() + user('x'))
        valid = jobs.preview(request)['plan_token']
        body, _, digest = valid.rpartition('.')
        raw = base64.urlsafe_b64decode(body + '=' * (-len(body) % 4))
        junk = {
            'not a token': 'junk',
            'empty': '',
            'digest altered': body + '.' + ('0' * 64),
            'body altered': 'A' + body[1:] + '.' + digest,
            'not base64': '!!!.' + digest,
            'not json, framed': framed(b'not json'),
            'oversized, framed': framed(b'"' + b'x' * 8200 + b'"'),
            'selection changed, framed': framed(raw.replace(b'"follow":false', b'"follow":true')),
            'not a string': None,
        }

        def applied(token, consent=True):
            result = outcome(jobs.apply, token, consent=consent)
            return {'accepted_phase': result['value'].get('phase')} if 'value' in result else result

        out['refusals'] = {label: applied(token) for label, token in junk.items()}
        out['refusals']['no consent'] = applied(valid, consent=False)
    return out


def _transcript_line(session, *blocks, meta=None, ending=b'\n', ascii=False):
    row = {'sessionId': session, 'message': {'content': list(blocks)}}
    if meta is not None:
        row['mcpMeta'] = {'structuredContent': meta}
    return json.dumps(row, separators=(',', ':'), ensure_ascii=ascii).encode() + ending


def transcript_record_site():
    """host_transcript_capture record ids (census-missed scheme, FEATURE classification (b)):
    read from extract_host_transcript_file()'s bundle, as transcript_cli.py consumes it."""
    from kp_agent_tooling_ops._impl.host_transcript_capture import extract_host_transcript_file

    def use(call_id, name, arguments=None):
        return {'type': 'tool_use', 'id': call_id, 'name': 'mcp__ops-agent-tooling__' + name,
                'input': arguments or {}}

    def result(call_id, value):
        text = json.dumps(value, separators=(',', ':'), ensure_ascii=False)
        try:
            text.encode()
        except UnicodeEncodeError:
            text = json.dumps(value, separators=(',', ':'))
        return {'type': 'tool_result', 'tool_use_id': call_id, 'content': [{'type': 'text', 'text': text}]}

    identity = {'status': 'ok', 'server': 'agent-tooling ☃', 'revision': 'a' * 40}
    context = {'status': 'ok', 'items': [{'text': 'réponse', 'n': 1}], 'empty': {}}
    transcripts = {
        'two calls, malformed and foreign rows': (
            _transcript_line('s', use('a', 'tooling_identity')) +
            b'{not json\n' + b'[1, 2]\r\n' +
            _transcript_line('other', use('x', 'tooling_identity')) +
            _transcript_line('s', result('a', identity), meta=identity, ending=b'\r\n') +
            _transcript_line('s', use('b', 'knowledge_context', {'query': 'é'})) +
            _transcript_line('s', result('b', context), ending=b''),
            ['a', 'b']),
        'one call': (_transcript_line('s', use('a', 'tooling_identity')) +
                     _transcript_line('s', result('a', identity)), ['a']),
        'missing result': (_transcript_line('s', use('a', 'tooling_identity')), ['a']),
        'escaped lone surrogates': (_transcript_line('s', use('a', 'tooling_identity', {'q': '\ud800'}), ascii=True) +
                                    _transcript_line('s', result('a', {'status': 'ok', 'text': 'x\udc00'}), ascii=True),
                                    ['a']),
    }
    out = {}
    with scratch('t11a-p1-transcript-') as root:
        for label, (raw, calls) in transcripts.items():
            path = root / 'transcript.jsonl'
            path.write_bytes(raw)
            out[label] = outcome(extract_host_transcript_file, path, session_id='s', call_ids=calls)
    return out


def build(part='core'):
    if part == 'core':
        corpus = {'schemes': corpus_core(), 'sorted_json_sites': {
            'agent_tooling.AgentTooling namespace (:66)': agent_tooling_site(),
            'workspace_capture.WorkspaceCapture.initialize policy_sha256 (:269)': workspace_capture_site(),
        }, 'census_missed_schemes': {
            'session_import_job plan token (preview / apply)': plan_token_site(),
        }, 'content_address_sites': {
            'navigation_search_pages._save cursor (:53)': search_cursor_site(),
            'navigation_snapshot.capture (:92 :104 :108)': snapshot_capture_site(),
        }, 'wire_hash_sites': {
            'scip_entry.symbol_report paths_sha256 (:41)': scip_entry_site(),
        }}
    else:
        corpus = {'schemes': corpus_ops(), 'sorted_json_sites': {
            'commit_rationale.RationaleCatalog.snapshot (:689 :693 :695)': snapshot_site(),
            'verification_packet.validate_join (:63)': validate_join_site(),
            'verification_adjacency.assemble_adjacent (:80)': adjacency_site(),
            'verification_correctness.correctness (:29)': correctness_site(),
        }, 'census_missed_schemes': {
            'host_transcript_capture record ids (extract_host_transcript_file)': transcript_record_site(),
        }, 'content_address_sites': {
            'knowledge_publish_cli._write (:31)': publish_write_site(),
        }}
    corpus['inputs'] = {case: repr(value) for case, value in VALUES}
    return golden(jsonable(corpus))


def main():
    for part, name in GOLDENS.items():
        text = build(part)
        write_golden(text, name)
        print(f'wrote tests/fixtures/t11a/{name} ({len(text)} bytes)')


if __name__ == '__main__':
    main()
