"""Contract tests for order S1: ``knowledge.platform`` tool coverage.

Order: docs/work/orders/S1-knowledge-platform-coverage.md, frozen at its merge
commit. These tests address
``kp_agent_tooling_ops._impl.service.knowledge_coverage.platform_coverage(config, anchor)``
only through the order's interface surface and properties P1 and P2; they assume
nothing else about how it is built.

Fixture: one git repository with three commits, two configured platforms
(``alpha`` with members ``alpha`` and ``beta``; ``gamma`` alone) and one
repository (``delta``) that belongs to no platform. SCIP indexes are published
with the package's own partitioned writer, so a spec is valid exactly when the
fixture leaves it untouched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from kp_agent_tooling._impl.scip_navigation import digest, write_partitioned_index
from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService
from kp_agent_tooling_ops._impl.service.knowledge_eligibility import eligible_paths

VERIFIED = 'digest-verified'
GAP = 'index_unavailable_or_mismatched'
FACETS = {'context', 'retrieve', 'discover', 'symbol'}


def platform_coverage(config, anchor):
    # Resolved per call: an absent module fails each test on its own, not collection.
    from kp_agent_tooling_ops._impl.service.knowledge_coverage import platform_coverage as contract
    return contract(config, anchor)


def _no_rag():
    pytest.fail('platform coverage must not open the document index')


def _as_json(value):
    """Compare values the way the transport sees them (a tuple and a list are both an array)."""
    return json.loads(json.dumps(value))


def _git(repo, *args):
    return subprocess.run(
        ['git', '-C', str(repo), '-c', 'user.name=S1 Test', '-c', 'user.email=s1@example.invalid',
         '-c', 'commit.gpgsign=false', *args],
        check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, files, message):
    for relative, text in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', message)
    return _git(repo, 'rev-parse', 'HEAD')


class Platform:
    """A valid knowledge configuration over one real repository and published indexes."""

    def __init__(self, root: Path):
        self.repo = root / 'source'
        self.indexes = root / 'indexes'
        self.repo.mkdir()
        self.indexes.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.rev = {
            'alpha': _commit(self.repo, {'pyproject.toml': '[project]\nname = "demo"\n',
                                         'pkg/mod.py': 'def f():\n    return 1\n'}, 'first'),
            'beta': _commit(self.repo, {'pkg/mod.py': 'def f():\n    return 2\n'}, 'second'),
            'gamma': _commit(self.repo, {'pkg/mod.py': 'def f():\n    return 3\n'}, 'third'),
        }

    def _repository(self, capabilities, artifacts):
        return {'path': str(self.repo), 'ref': 'HEAD', 'corpus_scope': 'docs',
                'tenant_ids': ['tenant'], 'capabilities': capabilities, 'artifacts': artifacts}

    def _member(self, key):
        return {'revision': self.rev[key],
                'artifacts': [{'path': 'pyproject.toml', 'role': 'build_definition'}]}

    def config(self, scip_indexes=None):
        maintained = {'status': 'maintained', 'owner': 'OPS'}
        config = {
            'schema_version': 'ops.knowledge-config.v1',
            'repositories': {
                # Capability ids are declared out of order on purpose.
                'alpha': self._repository(
                    {'zeta-flow': 'caps/zeta.json', 'alpha-flow': 'caps/alpha.json',
                     'mid-flow': 'caps/mid.json'},
                    {'docs/a.md': dict(maintained), 'docs/b.md': dict(maintained),
                     'docs/old.md': {'status': 'historical', 'owner': 'OPS'},
                     'docs/gone.md': {'status': 'withdrawn', 'owner': 'OPS'},
                     'docs/next.md': {'status': 'superseded', 'owner': 'OPS',
                                      'replacement': 'docs/a.md'}}),
                'beta': self._repository(
                    {'beta-read': 'caps/beta.json'},
                    {'docs/c.md': dict(maintained),
                     'docs/d.md': {'status': 'historical', 'owner': 'OPS'}}),
                'gamma': self._repository({}, {}),
                'delta': self._repository({'delta-only': 'caps/delta.json'},
                                          {'docs/z.md': dict(maintained)}),
            },
            'platforms': {
                'alpha': {'schema': 'ops.platform-request.v1', 'owner': 'OPS', 'profile': 'trial',
                          'sources': {'alpha': self._member('alpha'), 'beta': self._member('beta')}},
                'gamma': {'schema': 'ops.platform-request.v1', 'owner': 'OPS', 'profile': 'trial',
                          'sources': {'gamma': self._member('gamma')}},
            },
        }
        if scip_indexes is not None:
            config['scip_indexes'] = scip_indexes
        KnowledgeService(config, _no_rag)  # precondition: the service accepts this configuration
        return config

    def index(self, name, *, repo_key, revision):
        """Publish a partitioned index with the package writer and return its configured spec."""
        blob = _git(self.repo, 'rev-parse', f'{revision}:pkg/mod.py')
        data = {'schema': 'ops.scip-navigation.v1', 'repo_key': repo_key, 'revision': revision,
                'producer': {'name': 'scip-python', 'version': '0.6.6'},
                'provenance': {'fixture': 'S1', 'name': name}, 'blobs': {'pkg/mod.py': blob},
                'occurrences': [{'symbol': 'scip-python python demo 0.1 pkg/mod/f().',
                                 'path': 'pkg/mod.py', 'blob_sha': blob, 'byte_offset': 4,
                                 'byte_length': 1, 'definition': True, 'local': False}],
                'gaps': [], 'limitations': []}
        path = self.indexes / f'{name}.scip.json'
        manifest = write_partitioned_index(path, {'sha256': digest(data), 'data': data})
        return {'path': str(path), 'revision': revision, 'sha256': manifest['sha256']}

    def valid(self, key, name=None):
        return self.index(name or f'{key}-valid', repo_key=key, revision=self.rev[key])

    def full_config(self):
        """Every platform member has exactly one valid index spec."""
        return self.config({key: [self.valid(key)] for key in ('alpha', 'beta', 'gamma')})


@pytest.fixture
def platform(tmp_path):
    return Platform(tmp_path)


def _coverages(config):
    return {anchor: platform_coverage(config, anchor) for anchor in config['platforms']}


# P1 -------------------------------------------------------------------------------

@pytest.mark.parametrize('form', ['dict', 'path'])
@pytest.mark.parametrize('anchor', ['alpha', 'gamma'])
def test_p1_platform_operation_reports_tool_coverage(platform, tmp_path, anchor, form):
    """P1: for every configured anchor the operation returns status and data.tool_coverage,
    and data.tool_coverage == platform_coverage(config, anchor). The configuration holds one
    deliberately mismatched index spec (beta)."""
    mismatched = dict(platform.valid('beta'), sha256=digest({'not': 'the published index'}))
    config = platform.config({'alpha': [platform.valid('alpha')], 'beta': [mismatched]})
    source = config
    if form == 'path':
        source = tmp_path / 'knowledge.json'
        source.write_text(json.dumps(config))
    report = KnowledgeService(source, _no_rag).execute('platform', {'repo_key': anchor})
    assert 'status' in report
    assert 'tool_coverage' in report['data'], report['data']
    assert report['data']['tool_coverage'] == platform_coverage(config, anchor)


# Interface surface -----------------------------------------------------------------

def test_one_entry_per_platform_source_with_four_facets(platform):
    config = platform.full_config()
    for anchor, coverage in _coverages(config).items():
        assert isinstance(coverage, dict)
        assert set(coverage) == set(config['platforms'][anchor]['sources'])
        for key, entry in coverage.items():
            assert FACETS <= set(entry), (anchor, key, sorted(entry))


def test_context_lists_sorted_capability_ids(platform):
    config = platform.full_config()
    repositories = config['repositories']
    # Precondition: insertion order is not sorted order, and the members differ.
    assert list(repositories['alpha']['capabilities']) != sorted(repositories['alpha']['capabilities'])
    for coverage in _coverages(config).values():
        for key, entry in coverage.items():
            assert _as_json(entry['context']['capability_ids']) == sorted(repositories[key]['capabilities'])


def test_retrieve_counts_eligible_paths(platform):
    config = platform.full_config()
    repositories = config['repositories']
    counts = {key: len(eligible_paths(repositories[key])) for key in ('alpha', 'beta', 'gamma')}
    # Precondition: eligibility differs from declaration, and each member has its own count.
    assert counts['alpha'] != len(repositories['alpha']['artifacts'])
    assert len(set(counts.values())) == 3
    for coverage in _coverages(config).values():
        for key, entry in coverage.items():
            count = entry['retrieve']['eligible_path_count']
            assert type(count) is int
            assert count == counts[key], key


def test_symbol_platform_key_is_the_anchor(platform):
    config = platform.full_config()
    coverages = _coverages(config)
    assert 'beta' in coverages['alpha']  # precondition: a member whose key is not its anchor
    for anchor, coverage in coverages.items():
        for key, entry in coverage.items():
            assert entry['symbol']['platform_key'] == anchor, (anchor, key)


def test_symbol_next_call_targets_each_member_revision(platform):
    config = platform.full_config()
    assert len(set(platform.rev.values())) == 3  # precondition: member revisions differ
    for anchor, coverage in _coverages(config).items():
        for key, entry in coverage.items():
            revision = config['platforms'][anchor]['sources'][key]['revision']
            assert _as_json(entry['symbol']['next_call']) == {
                'tool': 'knowledge.symbol',
                'arguments': {'repo_key': key, 'target_revision': revision},
                'supply': ['path', 'line']}, (anchor, key)


def test_symbol_indexes_follow_configuration_order(platform):
    first = platform.valid('alpha', 'alpha-first')
    second = platform.valid('alpha', 'alpha-second')
    assert first['sha256'] != second['sha256']  # precondition: the two valid specs are distinguishable
    absent = dict(first, path=str(platform.indexes / 'absent.scip.json'))
    wrong_digest = dict(second, sha256=digest({'not': 'the published index'}))
    config = platform.config({'alpha': [first, absent, wrong_digest, second],
                              'beta': [platform.valid('beta')], 'gamma': [platform.valid('gamma')]})
    rows = platform_coverage(config, 'alpha')['alpha']['symbol']['indexes']
    assert len(rows) == 4
    assert [row.get('status') == VERIFIED for row in rows] == [True, False, False, True]
    assert [rows[1].get('gap'), rows[2].get('gap')] == [GAP, GAP]
    assert [rows[0].get('sha256'), rows[3].get('sha256')] == [first['sha256'], second['sha256']]


def test_verified_index_row_reports_digest_and_documents(platform):
    specs = {key: platform.valid(key) for key in ('alpha', 'beta', 'gamma')}
    config = platform.config({key: [spec] for key, spec in specs.items()})
    for coverage in _coverages(config).values():
        for key, entry in coverage.items():
            rows = entry['symbol']['indexes']
            assert len(rows) == 1, key
            assert rows[0]['status'] == VERIFIED, (key, rows[0])
            assert rows[0]['sha256'] == specs[key]['sha256'], key
            assert 'documents' in rows[0], key


@pytest.mark.parametrize('shape', ['no_scip_indexes', 'member_without_specs'])
def test_members_without_index_specs_have_no_index_rows(platform, shape):
    config = (platform.config() if shape == 'no_scip_indexes'
              else platform.config({'alpha': [platform.valid('alpha')]}))
    coverage = platform_coverage(config, 'alpha')
    assert set(coverage) == {'alpha', 'beta'}
    assert list(coverage['beta']['symbol']['indexes']) == []
    assert list(platform_coverage(config, 'gamma')['gamma']['symbol']['indexes']) == []
    expected_alpha = 0 if shape == 'no_scip_indexes' else 1
    assert len(coverage['alpha']['symbol']['indexes']) == expected_alpha


# P2 -------------------------------------------------------------------------------

P2_CASES = ['file_absent', 'declared_digest_differs', 'manifest_bytes_tampered',
            'partition_bytes_tampered', 'spec_revision_differs', 'index_revision_differs',
            'stale_index', 'index_repo_key_differs']


def _suspect(platform, case, control):
    member, other = platform.rev['alpha'], platform.rev['gamma']
    if case == 'file_absent':
        return dict(control, path=str(platform.indexes / 'absent.scip.json'))
    if case == 'declared_digest_differs':
        return dict(platform.valid('alpha', 'suspect'), sha256=digest({'not': 'the published index'}))
    if case == 'manifest_bytes_tampered':
        spec = platform.valid('alpha', 'suspect')
        manifest_path = Path(spec['path'])
        manifest = json.loads(manifest_path.read_text())
        manifest['data']['provenance'] = {'fixture': 'tampered'}  # declared sha256 left as published
        manifest_path.write_text(json.dumps(manifest))
        return spec
    if case == 'partition_bytes_tampered':
        spec = platform.valid('alpha', 'suspect')
        parts = sorted(platform.indexes.glob('suspect.scip.json.*.part.json'))
        assert len(parts) == 1, parts
        raw = parts[0].read_text()
        assert '"byte_offset":4' in raw
        parts[0].write_text(raw.replace('"byte_offset":4', '"byte_offset":5'))  # same size, new bytes
        return spec
    if case == 'spec_revision_differs':
        return dict(platform.index('suspect', repo_key='alpha', revision=member), revision=other)
    if case == 'index_revision_differs':
        return dict(platform.index('suspect', repo_key='alpha', revision=other), revision=member)
    if case == 'stale_index':
        return platform.index('suspect', repo_key='alpha', revision=other)
    if case == 'index_repo_key_differs':
        return platform.index('suspect', repo_key='beta', revision=member)
    raise AssertionError(case)


@pytest.mark.parametrize('case', P2_CASES)
def test_p2_mismatched_index_is_never_digest_verified(platform, case):
    """P2: absent file, differing digest, revision other than the member revision, or a
    different repo_key never yields digest-verified. Row 0 is the unchanged control."""
    control = platform.valid('alpha', 'control')
    suspect = _suspect(platform, case, control)
    config = platform.config({'alpha': [control, suspect]})
    rows = platform_coverage(config, 'alpha')['alpha']['symbol']['indexes']
    assert len(rows) == 2
    assert rows[0].get('status') == VERIFIED, ('control must verify', rows[0])
    assert rows[1].get('status') != VERIFIED, (case, rows[1])
    assert rows[1].get('gap') == GAP, (case, rows[1])


# Constraint: no filesystem writes, no network access --------------------------------

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_MUTATING_EVENTS = {'os.rename', 'os.remove', 'os.mkdir', 'os.rmdir', 'os.truncate', 'os.link',
                    'os.symlink', 'os.chmod', 'os.chown', 'os.utime', 'shutil.copyfile',
                    'shutil.copymode', 'shutil.copystat', 'shutil.copytree', 'shutil.move',
                    'shutil.rmtree', 'tempfile.mkstemp', 'tempfile.mkdtemp'}
_NETWORK_EVENTS = {'socket.connect', 'socket.bind', 'socket.getaddrinfo', 'socket.gethostbyname',
                   'socket.gethostbyaddr', 'socket.sendto', 'socket.sendmsg', 'urllib.Request',
                   'http.client.connect'}
_PROBE = {'installed': False, 'events': None}


def _audit(event, args):
    events = _PROBE['events']
    if events is None:
        return
    if event == 'open':
        path, mode, flags = args
        writes = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS)
        if writes and '__pycache__' not in str(path):
            events.append(('write', str(path)))
    elif event in _MUTATING_EVENTS:
        events.append(('write', event))
    elif event in _NETWORK_EVENTS:
        events.append(('network', event))


@contextmanager
def _side_effects():
    """Record filesystem writes and network calls made in this process while active."""
    if not _PROBE['installed']:
        sys.addaudithook(_audit)  # inert outside this context
        _PROBE['installed'] = True
    events = []
    _PROBE['events'] = events
    try:
        yield events
    finally:
        _PROBE['events'] = None


def test_platform_coverage_performs_no_writes_or_network(platform, tmp_path):
    good = platform.valid('alpha')
    config = platform.config({
        'alpha': [good, dict(good, path=str(platform.indexes / 'absent.scip.json'))],
        'beta': [dict(platform.valid('beta'), sha256=digest({'not': 'the published index'}))],
        'gamma': [platform.valid('gamma')]})
    for anchor in config['platforms']:
        platform_coverage(config, anchor)  # warm-up: lazy imports may compile bytecode
    with _side_effects() as calibration:
        (tmp_path / 'calibration.txt').write_text('x')
        sys.audit('socket.connect', None, ('192.0.2.1', 9))
    # Precondition: the instrument sees a write and a network call.
    assert {kind for kind, _ in calibration} == {'write', 'network'}
    with _side_effects() as observed:
        for anchor in config['platforms']:
            platform_coverage(config, anchor)
    assert observed == []
