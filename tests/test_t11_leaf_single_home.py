"""T11a guard: each invariant has one home, the leaf (docs/work/orders/T11a-leaf-consolidation.md).

P2, P3, P4, P5, P6, P7 and P8 as AST counts over the product modules, using the census
patterns (a)-(h) of T11a-census-1e74728.md (instrument: tests/t11a_census.py). Outside
``packages/tooling/src/kp_agent_tooling/_impl/leaf.py`` and the ALLOWLIST below, the guard
asserts zero:

- (a) canonical ``json.dumps``/``json.dump`` calls (sort_keys + compact separators);
- (a5) sorted_json serialisations that reach a digest;
- (ii) serialisation -> digest identity constructions (any JSON form, a serialiser helper, or a
  leaf serialiser feeding a digest), wire_hash and file_content_hash members excepted;
- (b-i) generic sha256 helper definitions;
- (b-iii) second implementations of a domain identity scheme;
- (c) private-file helper definitions (the census's 30) and the order's P3 primitives (the rest of
  the census's primitive set is a --report reading only: Verification ruling, carries to T11b);
- (d) ``sqlite3.connect`` calls;
- (h) store-filename literals;

and exactly one store-path rule (e), no near-duplicate function pair (f), and no
unreferenced definition outside the P8 allowlist (g). A one-statement delegating ``def``
under an old name (P9) is not a definition.

Base reading (L9): ``python tests/test_t11_leaf_single_home.py --report`` prints every
category's count, module and embedded separately, beside the order's Re-census numbers,
without asserting. ``--sites`` adds every site with file:line.

GREEN-IF: at head, every count above is zero outside the leaf and the allowlist, the leaf
holds the one store-path rule, (f) reports no pair and (g) nothing outside its allowlist.
RED at base by construction (the leaf does not exist).
"""
from __future__ import annotations

import ast
import functools
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import t11a_census as census_module  # noqa: E402
from t11a_census import D, LEAF, O, T  # noqa: E402


# ---------------------------------------------------------------- the allowlist

@dataclass(frozen=True)
class Member:
    """One allowlisted site: matched by file, enclosing def (suffix of its qualname) and kind,
    never by line, so it survives line moves. ``line`` is where the order names it (base)."""
    rel: str
    function: str
    kind: str
    line: int

    def matches(self, site):
        return (site.rel == self.rel and (site.function == self.function
                                          or site.function.endswith('.' + self.function))
                and (self.kind == '*' or site.kind == self.kind))


# One constant, by REASON CLASS (Coordinator rule, 2026-10-03). An entry outside the leaf is
# admissible only for a reason the order already recognises. Members are listed where the
# order names them; a class without members is structural (a pattern, not a list).
ALLOWLIST = {
    'embedded_script': {
        'reason': 'stdlib-only script run under `python3 -I -c` (P2 frozen allowlist; '
                  'runtime_install.py:1118-1119). Its sites are counted as embedded, never asserted.',
        'members': (
            Member(T + '_impl/runtime_install.py', '_INSPECT', 'script', 1122),
            Member(T + '_impl/runtime_install.py', '_OWN', 'script', 1134),
            Member(T + '_impl/runtime_install.py', '_CLEAR', 'script', 1145),
            Member(T + '_impl/runtime_install.py', '_STORE_LIB', 'script', 1170),
            Member(T + '_impl/runtime_install.py', '_MIGRATE', 'script', 1333),
            Member(T + '_impl/dependency_identity.py', 'script', 'script', 124),
        ),
    },
    'streaming_sha256': {
        'reason': 'streaming hashlib.sha256() + .update (P2: may stay). Zero-argument sha256() '
                  'calls are not matched by the generic-helper or digest patterns at all.',
        'members': (
            Member(T + '_impl/service/claude_episode_capture.py', 'ClaudeEpisodeCapture.seed', 'streaming', 153),
            Member(T + '_impl/service/launch_binding.py', 'RolloutCapture._page', 'streaming', 633),
            Member(T + '_impl/service/spool_ingest.py', 'ChildRolloutCapture._page', 'streaming', 217),
            Member(T + '_impl/service/spool_ingest.py', '_consume', 'streaming', 605),
            Member(T + '_impl/service/session_import_job.py', '_digest_prefix', 'streaming', 80),
        ),
    },
    'raw_bytes_sha256': {
        'reason': 'raw-bytes inline hashlib.sha256(<bytes>).hexdigest() (Q1): may stay; not '
                  'counted by the (b) generic-helper or the (a5/ii) canonical->digest patterns.',
        'members': (),
    },
    'directory_rename': {
        'reason': 'a directory rename that is not a private file (P3; census (c) false positives)',
        'members': (
            Member(T + '_impl/service/model_gateway.py', 'write_outputs', 'os.rename', 488),
            Member(T + '_impl/refresh_retention.py', 'remove_generation', 'os.rename', 211),
            Member(T + '_impl/runtime_install.py', 'migrate', 'os.rename', 1664),
            Member(T + '_impl/runtime_install.py', 'undo', 'os.rename', 1673),
            # Meet (Coordinator, T11b Amendment 1): T11b Q3 asserts the wider set, which reaches this site. It
            # renames a temporary staging directory (leaf.make_temp_dir) into the bundle path: a directory, not a
            # private file.
            Member(T + '_impl/workspace_setup.py', 'apply', 'Path.rename', 178),
        ),
    },
    'readonly_delivery': {
        'reason': 'the 0o400 read-only delivery at tool_delivery.py:57 (P3). Read narrowly: the '
                  '0o400 chmod only; the rest of the delivery sequence is counted (AMBIGUITY).',
        'members': (
            Member(T + '_impl/tool_delivery.py', 'DeliveryStore.deliver', 'os.chmod', 57),
        ),
    },
    'wire_hash': {
        'reason': 'a digest of the exact bytes sent over a wire, kept as a receipt or record field, and never '
                  're-derived from a value (Coordinator ruling 2026-10-03). Not an identity construction.',
        'members': (
            # Unsorted dumps of the SCIP entry as emitted (P1/P2, M1); its digest is in the P1 corpus.
            Member(T + '_impl/service/scip_entry.py', 'symbol_report', 'unsorted', 41),
            # request_sha256 (:346) of the compact request bytes (:343).
            Member(T + '_impl/service/episodic_summarizer.py', 'OpenRouterEpisodeSummarizer.__call__', 'unsorted', 343),
        ),
    },
    'file_content_hash': {
        'reason': 'a digest of the exact bytes written to a file, recorded beside it (a manifest or receipt) '
                  'and verifiable only against that file; the JSON form is a file format, not an identity '
                  'scheme (Coordinator ruling 2026-10-03). Not an identity construction.',
        'members': (
            Member(O + 'capture_cli.py', 'main', 'sorted+indent', 50),       # hashed at :53
            Member(O + 'transcript_cli.py', 'main', 'sorted+indent', 36),    # hashed at :39
            Member(O + 'transcript_cli.py', 'main', 'sorted+indent', 43),    # hashed at :46
            # Hashed at :59; the descriptor records the part file's sha256, verified only against
            # that file's bytes (_partition_rows). Joined by Coordinator ruling, 2026-10-03.
            Member(T + '_impl/scip_navigation.py', 'write_partitioned_index', 'serialiser-helper _bytes', 58),
            # The two below are named by the ruling, but the guard's pattern counts no site there:
            # the hashed bytes are built in another function (runtime_install._json via render();
            # host_adapter.merge_claude/merge_codex). Kind 'raw-bytes' matches no counted site, so
            # neither member can allow any other construction in its function.
            Member(T + '_impl/runtime_install.py', '_build', 'raw-bytes', 945),
            Member(T + '_impl/service/host_adapter.py', 'install_hooks', 'raw-bytes', 784),
        ),
    },
    'dead_code': {
        'reason': 'P8 dead-code allowlist: stdlib framework overrides, ProductionLocalEmbedding '
                  '(kept for T13), and Protocol stubs (structural)',
        'members': (
            Member(T + '_impl/workspace_setup_server.py', 'do_GET', 'def', 124),
            Member(T + '_impl/workspace_setup_server.py', 'do_POST', 'def', 144),
            Member(T + '_impl/workspace_setup_server.py', 'log_message', 'def', 75),
            Member(T + '_impl/workspace_setup_server.py', 'get_request', 'def', 48),
            Member(T + '_impl/embeddings/embedders.py', 'ProductionLocalEmbedding', 'def', 198),
        ),
    },
    'delegation': {
        'reason': 'a one-statement delegating def under an old name (P9) is not a definition',
        'members': (),
    },
}

EMBEDDED_SCRIPTS = {(m.rel, m.function) for m in ALLOWLIST['embedded_script']['members']}
# file_content_hash-shaped sites ruled identity constructions (content addresses other records
# cite, or a census domain hasher): they stay counted, and P1's corpus covers their schemes.
RULED_IDENTITY = {(T + '_impl/navigation_search_pages.py', '_save'), (T + '_impl/navigation_snapshot.py', 'capture'),
                  (O + 'knowledge_publish_cli.py', '_write')}
ORDER_P3_PRIMITIVES = {'os.open+O_CREAT', 'O_NOFOLLOW', 'open-x', 'tempfile.mkstemp', 'tempfile.NamedTemporaryFile',
                       'tempfile.mkdtemp', 'os.replace', 'os.link', 'os.fdopen'}


def allowed(site, *classes):
    return any(member.matches(site) for name in classes for member in ALLOWLIST[name]['members'])


# ---------------------------------------------------------------- readings

@functools.lru_cache(maxsize=None)
def reading():
    return census_module.Census()


@functools.lru_cache(maxsize=None)
def dead_reading():
    return census_module.dead_code(reading())


def counted(sites):
    """Sites the guard counts: outside the leaf and not in an allowlisted embedded script."""
    return [s for s in sites if s.rel != LEAF and (not s.embedded or (s.rel, s.embedded) not in EMBEDDED_SCRIPTS)]


def module_sites(category, census=None):
    return counted((census or reading()).sites[category])


def fmt(sites, limit=200):
    lines = [f'  {s}' for s in sorted(sites, key=lambda s: (s.rel, s.line))[:limit]]
    more = len(sites) - limit
    return '\n'.join(lines + ([f'  ... {more} more'] if more > 0 else []))


def canonical_violations(census=None):
    return module_sites('a.canonical', census)


def sorted_json_violations(census=None):
    return module_sites('a.sorted_json', census)


def digest_construction_violations(census=None):
    """(ii): serialisation -> digest constructions outside the leaf that are identity constructions,
    i.e. not a wire_hash or file_content_hash member."""
    return [s for s in module_sites('a.digest', census) if not allowed(s, 'wire_hash', 'file_content_hash')]


# Domain identity hashers keep their names and schemes and call the leaf (P2). A member is exempt
# from P2(i) only while it is ONE statement whose digest is ONE call into a leaf digest function
# (LEAF_DIGESTS; ``sha256_hex`` only over a leaf serialiser), with no hashlib or json call of its
# own. Membership is fixed to the census's 12 domain identity hashers (Coordinator, Verification
# precision, 2026-10-03): nothing outside them may enter. Another function FEATURE classifies as
# an identity scheme becomes a P1 corpus entry and an order amendment, never an exemption.
CENSUS_DOMAIN_HASHERS = frozenset({
    (O + '_impl/code_references/artifact_identity.py', 'change_lineage_key'),
    (O + '_impl/code_references/artifact_identity.py', 'change_occurrence_id'),
    (O + '_impl/code_references/retrieval.py', '_chunk_id'),
    (O + '_impl/service/document_coordinates.py', '_chunk_id'),
    (T + '_impl/repository_manifest.py', '_identity'),
    (T + '_impl/service/desk_identity.py', 'binding_key'),
    (T + '_impl/navigation_search_pages.py', '_manifest_cache_path'),
    (T + '_impl/service/claude_episode_capture.py', '_digest'),
    (T + '_impl/service/host_adapter.py', 'codex_trust_entry'),
    (T + '_impl/service/launch_binding.py', 'codex_trust_entry'),
    (T + '_impl/embeddings/revision.py', 'embedding_identity'),
    (O + 'knowledge_publish_cli.py', '_write'),
})
DOMAIN_HASHERS = CENSUS_DOMAIN_HASHERS
LEAF_DIGESTS = {'canonical_sha256', 'sorted_sha256', 'content_id', 'sha256_hex'}


def domain_hasher_qualifies(unit, fn, census=None):
    """The exemption's shape: one statement; exactly one leaf digest call; nothing else hashes or
    serialises; ``sha256_hex`` only over a leaf serialiser."""
    body = census_module.strip_doc(fn.body)
    if len(body) != 1 or not isinstance(body[0], (ast.Return, ast.Expr)) or body[0].value is None:
        return False
    if any(census_module.is_sha256(unit, n) or census_module.is_dumps(unit, n) for n in unit.own_nodes(fn)):
        return False
    calls = [n for n in ast.walk(body[0].value) if isinstance(n, ast.Call)
             and (unit.qual(n.func) or '').startswith(census_module.LEAF_MODULE + '.')
             and unit.qual(n.func).rsplit('.', 1)[1] in LEAF_DIGESTS]
    if len(calls) != 1:
        return False
    call = calls[0]
    if unit.qual(call.func).rsplit('.', 1)[1] == 'sha256_hex':
        argument = census_module._strip_encode(call.args[0]) if call.args else None
        return argument is not None and (census or reading()).leaf_role(unit, argument) == 'serialiser'
    return True


def generic_helper_violations(census=None, domain_hashers=None):
    census = census or reading()
    members = DOMAIN_HASHERS if domain_hashers is None else domain_hashers
    assert census is not reading() or members <= CENSUS_DOMAIN_HASHERS, 'DOMAIN_HASHERS outside the census 12'
    found = []
    for site in module_sites('b.generic', census):
        if (site.rel, site.function) in members:
            unit, fn = census.functions[(site.rel, site.function)]
            if domain_hasher_qualifies(unit, fn, census):
                continue
        found.append(site)
    return found


def identity_scheme_violations(census=None):
    return census_module.second_implementations(census or reading())


def private_helper_violations(census=None):
    census = census or reading()
    found = []
    for rel, qualname in census_module.CENSUS_PRIVATE_HELPERS:
        hit = census.functions.get((rel, qualname))
        if hit is not None and not census_module.is_delegation(*hit):
            unit, fn = hit
            found.append(census_module.Site('c.helper', 'def', rel, unit.line(fn), None, qualname,
                                            'census-named private-file helper'))
    return found


def private_primitive_violations(census=None):
    return [s for s in module_sites('c.primitive', census) + module_sites('c.nofollow', census)
            if not allowed(s, 'directory_rename', 'readonly_delivery')]


def connect_violations(census=None):
    return module_sites('d.connect', census)


def store_file_violations(census=None):
    return module_sites('h.store_file', census)


def store_rule_files(census=None):
    return sorted({s.rel for s in (census or reading()).sites['e.rule'] if not s.embedded})


def near_duplicate_pairs(census=None):
    return census_module.near_duplicates(census or reading())


def _dead_allowed(definition, containers):
    if definition.protocol_stub or definition.leaf_delegation:
        return True  # Protocol stubs (P8); a P9 old name delegating to the leaf is not a definition
    # An allowlisted definition covers itself and every definition nested inside it, whether or
    # not it is itself unreferenced (the census counts ProductionLocalEmbedding and its nested
    # embed_one as one entry).
    return any(o.rel == definition.rel and o.start <= definition.start and definition.end <= o.end
               for o in containers)


def dead_allowlisted_containers(census=None):
    definitions, _ = census_module._definitions(census or reading())
    return [d for d in definitions
            if allowed(census_module.Site('g.dead', 'def', d.rel, d.line, None, d.qualname), 'dead_code')]


def dead_code_violations():
    dead, _, _ = dead_reading()
    containers = dead_allowlisted_containers()
    return [d for d in dead if not _dead_allowed(d, containers)]


# ---------------------------------------------------------------- the asserting guard (RED at base)

def test_p2_a_zero_canonical_dumps_outside_the_leaf():
    found = canonical_violations()
    assert not found, f'{len(found)} canonical json.dumps/dump calls outside the leaf:\n{fmt(found)}'


def test_p2_a5_zero_sorted_json_digest_constructions_outside_the_leaf():
    found = sorted_json_violations()
    assert not found, (f'{len(found)} sort_keys-only (sorted_json) serialisations reach a digest '
                       f'outside the leaf:\n{fmt(found)}')


def test_p2_ii_zero_identity_constructions_from_a_serialisation_outside_the_leaf():
    found = digest_construction_violations()
    assert not found, (f'{len(found)} serialisation -> digest identity constructions outside the leaf '
                       f'(allowlist: wire_hash, file_content_hash):\n{fmt(found)}')


def test_p2_i_zero_generic_sha256_helper_definitions_outside_the_leaf():
    found = generic_helper_violations()
    assert not found, f'{len(found)} generic sha256 helper definitions outside the leaf:\n{fmt(found)}'


def test_p2_iii_zero_second_implementations_of_an_identity_scheme():
    groups = identity_scheme_violations()
    seconds = sum(len(g) - 1 for g in groups)
    detail = '\n'.join('  ' + ' == '.join(f'{s.rel}:{s.line} {s.function}' for s in g) for g in groups)
    assert not groups, f'{seconds} second implementations of an identity scheme:\n{detail}'


def test_p3_zero_private_file_helper_definitions_outside_the_leaf():
    found = private_helper_violations()
    assert not found, (f'{len(found)} census-named private-file helpers are still definitions (not '
                       f'one-statement delegations) outside the leaf:\n{fmt(found)}')


def test_p3_zero_order_listed_private_file_primitives_outside_the_leaf():
    """The order's P3 list: os.open with O_CREAT, O_NOFOLLOW, 'x' modes, tempfile.mkstemp/
    NamedTemporaryFile/mkdtemp, os.replace, os.link, os.fdopen."""
    found = [s for s in private_primitive_violations() if s.kind in ORDER_P3_PRIMITIVES]
    kinds = Counter(s.kind for s in found)
    assert not found, (f'{len(found)} private-file primitives of the order\'s P3 list outside the leaf and the '
                       f'allowlist {dict(sorted(kinds.items()))}:\n{fmt(found)}')


def test_p4_zero_sqlite3_connect_outside_the_leaf():
    found = connect_violations()
    assert not found, f'{len(found)} sqlite3.connect calls outside the leaf:\n{fmt(found)}'


def test_p5_exactly_one_store_path_rule_and_it_is_the_leafs():
    files = store_rule_files()
    assert files == [LEAF], (f'the store-path rule (/state/memory, STORE_KEYS/TEMPLATE_KEY/LEGACY_STORES '
                             f'literals, outside_store/required_store_path definitions) is implemented in '
                             f'{len(files)} module(s): {files}; expected exactly one, the leaf')


def test_p6_zero_store_filename_literals_outside_the_leaf():
    found = store_file_violations()
    assert not found, f'{len(found)} store-filename literals outside the leaf:\n{fmt(found)}'


def test_p7_no_near_duplicate_function_pair():
    pairs = near_duplicate_pairs()
    assert not pairs, f'{len(pairs)} near-duplicate pairs (>=15 statements, >=0.90): {pairs}'


def test_p8_no_unreferenced_definition_outside_the_allowlist():
    found = dead_code_violations()
    detail = '\n'.join(f'  {d.rel}:{d.line}-{d.end} {d.qualname}' for d in found)
    assert not found, f'{len(found)} unreferenced definitions outside the P8 allowlist:\n{detail}'


# ---------------------------------------------------------------- the instrument's own checks (GREEN at base)

def test_instrument_embedded_scripts_are_exactly_the_frozen_allowlist():
    found = {(u.rel, u.embedded) for u in reading().units if u.embedded}
    assert found <= EMBEDDED_SCRIPTS, f'embedded scripts outside the frozen allowlist: {found - EMBEDDED_SCRIPTS}'


def _probe(source, rel='packages/tooling/src/kp_agent_tooling/_impl/probe.py', tmp=None):
    path = tmp / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return census_module.Census(root=tmp, files=[rel])


PROBE = '''
import hashlib as _h, json as _j, sqlite3, os, tempfile
from hashlib import sha256
from json import dumps as d

def _sha(raw):
    return _h.sha256(raw).hexdigest()

def _digest(value):
    return sha256(d(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def old_name(raw):
    return leafish(raw)

def key(a, b):
    return 'k:' + _h.sha256('|'.join((a, b)).encode()).hexdigest()

def snapshot(row):
    return {'x': _h.sha256(_j.dumps(row, sort_keys=True).encode()).hexdigest()}

def store(p):
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return sqlite3.connect(str(p) + '/episodes.sqlite3')

SCRIPT = r"""
import sqlite3
sqlite3.connect("x.sqlite3")
"""
'''


def test_instrument_resolves_aliases_and_ignores_delegations(tmp_path):
    census = _probe(PROBE, tmp=tmp_path)
    generic = {s.function for s in census.sites['b.generic']}
    assert generic == {'_sha', '_digest'}, generic  # aliases resolved; old_name is a delegation
    assert {s.function for s in census.sites['b.domain']} == {'key'}
    assert len(census.sites['a.canonical']) == 1 and len(census.sites['a.sorted_json']) == 1
    assert len(census.sites['b.sha256']) == 4
    module, embedded = census.split('d.connect')
    assert (len(module), len(embedded)) == (1, 1)
    assert [s.embedded for s in embedded] == ['SCRIPT']
    assert {s.kind for s in census.sites['c.primitive']} == {'os.open+O_CREAT'}
    assert len(census.sites['c.nofollow']) == 1
    module, embedded = census.split('h.store_file')
    assert (len(module), len(embedded)) == (1, 1)


def test_instrument_near_duplicate_guard_matches_renamed_aliases(tmp_path):
    body = '\n'.join(f'    x{i} = f(x{i - 1}, "msg {i}")' for i in range(1, 20))
    source = (f'def one(x0):\n{body}\n    raise Err("a")\n\n'
              f'def two(x0):\n    conflict = mod.Err\n{body.replace("msg", "other")}\n    raise conflict("b")\n')
    pairs = census_module.near_duplicates(_probe(source, tmp=tmp_path))
    assert len(pairs) == 1 and pairs[0][0] >= 0.9, pairs


GREEN_TREE = {
    LEAF: '''"""A miniature leaf: every site lives here."""
import hashlib, json, os, sqlite3, tempfile
STORE_ROOT = '/state/memory'
STORE_KEYS = ('state_root', 'roster_path')
EPISODES_DB = 'episodes.sqlite3'

def canonical_bytes(value, *, ascii=True, allow_nan=True):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=ascii, allow_nan=allow_nan).encode()

def sorted_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()

def write_private_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(data)
        os.fsync(out.fileno())

def sqlite_open(path, *, timeout=5.0):
    return sqlite3.connect(path, timeout=timeout)

def required_store_path(key, value):
    return None if str(value).startswith(STORE_ROOT) else key
''',
    T + '_impl/service/consumer.py': '''"""A consumer after consolidation: old names delegate in one statement."""
import hashlib
from kp_agent_tooling._impl import leaf

def _sha(raw):
    return leaf.sha256_hex(raw)

def _digest(value):
    return leaf.sorted_sha256(value)

def write_private(path, value):
    leaf.write_private_new(path, value)

def _db(path):
    return leaf.sqlite_open(path, timeout=10)

def store(state):
    return leaf.sqlite_open(state / leaf.EPISODES_DB)

def file_digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        digest.update(stream.read())
    return digest.hexdigest()

def record(path, raw):
    path.write_bytes(raw)
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
''',
}


def test_instrument_reads_zero_outside_a_leaf_that_holds_every_site(tmp_path):
    """GREEN-IF stub in miniature: the categories read zero when the sites live in the leaf."""
    files = []
    for rel, source in GREEN_TREE.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        files.append(rel)
    census = census_module.Census(root=tmp_path, files=files)
    assert census.leaf
    assert len(census.sites['a.canonical']) == 1 and len(census.sites['d.connect']) == 1  # in the leaf
    for found in (canonical_violations(census), sorted_json_violations(census), digest_construction_violations(census),
                  generic_helper_violations(census), identity_scheme_violations(census),
                  private_primitive_violations(census), connect_violations(census), store_file_violations(census),
                  near_duplicate_pairs(census)):
        assert not found, found
    assert store_rule_files(census) == [LEAF]


DOMAIN_TREE = {
    LEAF: '''"""A miniature leaf."""
import hashlib, json

def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()

def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()

def content_id(kind, value):
    return kind + ':sha256:' + sha256_hex(canonical_bytes(value))
''',
    T + '_impl/service/hashers.py': '''"""Domain hashers after consolidation."""
from kp_agent_tooling._impl import leaf

def binding_key(value):
    return 'binding:' + leaf.sha256_hex(leaf.canonical_bytes(value))

def raw_key(raw):
    return 'raw:' + leaf.sha256_hex(raw)

def checked_key(value):
    assert value
    return 'checked:' + leaf.sha256_hex(leaf.canonical_bytes(value))
''',
}


def test_instrument_domain_hashers_are_exactly_the_census_twelve():
    assert DOMAIN_HASHERS <= CENSUS_DOMAIN_HASHERS and len(CENSUS_DOMAIN_HASHERS) == 12
    resolved = {key for key in CENSUS_DOMAIN_HASHERS if key in reading().functions}
    assert resolved == CENSUS_DOMAIN_HASHERS, sorted(CENSUS_DOMAIN_HASHERS - resolved)


def test_instrument_domain_hashers_are_exempt_only_in_the_one_statement_leaf_shape(tmp_path):
    files = []
    for rel, source in DOMAIN_TREE.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        files.append(rel)
    census = census_module.Census(root=tmp_path, files=files)
    hashers = T + '_impl/service/hashers.py'
    flagged = {s.function for s in generic_helper_violations(census, domain_hashers=frozenset())}
    assert flagged == {'binding_key', 'raw_key', 'checked_key'}, flagged  # not exempt unless named
    members = frozenset((hashers, name) for name in ('binding_key', 'raw_key', 'checked_key'))
    flagged = {s.function for s in generic_helper_violations(census, domain_hashers=members)}
    # raw_key: sha256_hex over raw bytes, not a leaf serialiser; checked_key: two statements.
    assert flagged == {'raw_key', 'checked_key'}, flagged


# ---------------------------------------------------------------- the base reading

ORDER = {  # the order's "Re-census at the freeze base" (module + embedded)
    'a.canonical': (49, 0), 'a.split': (26, 23), 'a.sorted_json': (9, 1),
    'b.sha256': (172, 4), 'b.generic': (24, 3), 'b.domain': 12,
    'c.helpers': (30, 6), 'c.primitive': (132, 12),
    'd.connect': (36, 3), 'd.helper': (10, 2),
    'e.state_root': 14, 'e.rule': 2, 'f': '1 pair: _page, 0.968', 'g': (24, 514), 'h.store_file': (27, 2),
}


def report(show_sites=False):
    census = reading()
    out = []
    w = out.append

    def pair(category, sites=None):
        module, embedded = census.split(category, sites)
        return len(module), len(embedded)

    head = subprocess.run(['git', '-C', str(census.root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    dirty = subprocess.run(['git', '-C', str(census.root), 'status', '--porcelain', '--untracked-files=no', '--',
                            T, 'extensions/ops/src', D], capture_output=True, text=True)
    w(f'T11a guard: base reading (L9). tree {head.stdout.strip()}'
      f"{' + uncommitted product changes' if dirty.stdout.strip() else ''}; "
      f"{len([u for u in census.units if not u.embedded])} modules, "
      f"{len([u for u in census.units if u.embedded])} embedded scripts; leaf present: {census.leaf}")
    w('Counts are module + embedded, outside the leaf. [order: ...] is the Re-census reading.')
    w('')
    canonical = census.sites['a.canonical']
    split = Counter(s.kind for s in canonical if s.rel != LEAF)
    w(f"(a) canonical json.dumps/dump        {pair('a.canonical')}   [order: {ORDER['a.canonical']}]")
    w(f"    helpers / inline (<=3 stmts)     {split['helper']} / {split['inline']}   [order: 26 / 23, purpose reading]")
    w(f"    output variants                  {dict(sorted(Counter(s.detail for s in canonical).items()))}")
    w(f"(a5) sorted_json -> digest           {pair('a.sorted_json')}   [order: {ORDER['a.sorted_json']}]")
    constructions = census.sites['a.digest']
    exempt = [s for s in counted(constructions) if allowed(s, 'wire_hash', 'file_content_hash')]
    w(f"(ii) serialisation -> digest         {pair('a.digest')}; wire_hash/file_content_hash members {len(exempt)}; "
      f"asserted identity constructions {len(digest_construction_violations(census))}   [not a Re-census line]")
    sha = census.sites['b.sha256']
    streaming_fns = {(s.rel, s.function) for s in sha if s.kind == 'streaming' and s.embedded}
    w(f"(b) hashlib.sha256 calls             {pair('b.sha256')}   [order: {ORDER['b.sha256']}]")
    w(f"    of which zero-argument (streaming/empty constant): "
      f"{pair('b.sha256', [s for s in sha if s.kind == 'streaming'])}")
    w(f"    generic helpers (pattern)        {pair('b.generic')}; embedded streaming digest functions "
      f"{len(streaming_fns)} ({', '.join(sorted(f for _, f in streaming_fns))})   [order: 24 + 3]")
    w(f"    domain identity hashers          {pair('b.domain')[0]}   [order: {ORDER['b.domain']}]")
    exempt = [s for s in module_sites('b.generic', census) if (s.rel, s.function) in DOMAIN_HASHERS]
    w(f"    DOMAIN_HASHERS (P2(i) exemption) {len(DOMAIN_HASHERS)} named; read as generic helpers here: "
      + (', '.join(f'{s.rel}:{s.line} {s.function} '
                   f"{'qualifies' if domain_hasher_qualifies(*census.functions[(s.rel, s.function)], census) else 'DOES NOT QUALIFY'}"
                   for s in exempt) or 'none'))
    groups = census_module.second_implementations(census)
    w(f"    second implementations (iii)     {sum(len(g) - 1 for g in groups)}: "
      + '; '.join(' == '.join(f'{Path(s.rel).name}:{s.line} {s.function}' for s in g) for g in groups))
    named = [(rel, q) for rel, q in census_module.CENSUS_PRIVATE_HELPERS if (rel, q) in census.functions]
    helpers = private_helper_violations()
    embedded_fns = [s for s in census.sites['c.functions'] if s.embedded]
    w(f"(c) private-file helpers             {len(helpers)} census-named definitions ({len(named)} of "
      f"{len(census_module.CENSUS_PRIVATE_HELPERS)} names resolve) + {len(embedded_fns)} embedded functions "
      f"containing a primitive   [order: {ORDER['c.helpers']}]")
    w(f"    module functions containing a primitive (informational): {pair('c.functions')[0]}")
    prim = census.sites['c.primitive']
    census_set = [s for s in prim if s.kind in census_module.CENSUS_PRIMITIVES]
    w(f"    primitives (census set)          {pair('c.primitive', census_set)}   [order: {ORDER['c.primitive']}]")
    w(f"      by kind: {dict(sorted(Counter(s.kind for s in census_set if s.rel != LEAF and not s.embedded).items()))}")
    w(f"    + os.fdopen (P3 list, not in the census's 144): "
      f"{pair('c.primitive', [s for s in prim if s.kind == 'os.fdopen'])}")
    w(f"    O_NOFOLLOW markers               {pair('c.nofollow')}   [census: 12]")
    outside = private_primitive_violations()
    listed = [s for s in outside if s.kind in ORDER_P3_PRIMITIVES]
    rest = [s for s in outside if s.kind not in ORDER_P3_PRIMITIVES]
    w(f"    asserted (order's P3 list, incl. O_NOFOLLOW, os.fdopen), outside the allowlist: {len(listed)}")
    w(f"    reading only, not asserted in T11a (Verification ruling; carries to T11b): the rest of the census "
      f"set outside the allowlist: {len(rest)} {dict(sorted(Counter(s.kind for s in rest).items()))}")
    w(f"(d) sqlite3.connect                  {pair('d.connect')}   [order: {ORDER['d.connect']}]")
    w(f"    connection helpers (returns a new connection) {pair('d.helper')}   [order: {ORDER['d.helper']}]")
    w(f"(e) store-path rule copies           {len(store_rule_files())}: {', '.join(store_rule_files())}"
      f"   [order: {ORDER['e.rule']}]")
    w(f"    ['state_root'] subscript reads   {pair('e.state_root')[0]}   [order: 14 = 11 reads + 3 parameter sites]")
    pairs = near_duplicate_pairs()
    w(f"(f) near-duplicate pairs             {len(pairs)}: {pairs}   [order: {ORDER['f']}]")
    dead, nested, total = dead_reading()
    maximal = [d for d in dead if d not in nested]
    census_dead = [d for d in maximal if d.name in CENSUS_DEAD]
    beyond = [d for d in maximal if d.name not in CENSUS_DEAD]
    w(f"(g) unreferenced definitions         {len(maximal)} maximal ({len(nested)} nested inside them), of "
      f"{total} definitions; census 24 found: {len(census_dead)}, "
      f"{sum(d.end - d.line + 1 for d in census_dead)} lines   [order: 24, 514 lines]")
    w(f"    framework overrides (census step 5, live roots): {', '.join(census_module.FRAMEWORK_OVERRIDES)}")
    w(f"(h) store-filename literals          {pair('h.store_file')}   [order: {ORDER['h.store_file']}]")
    w('')
    w('Beyond the Re-census (sites the census did not list; order amendments, not allowlist entries):')
    for d in beyond:
        w(f'  (g) {d.rel}:{d.line}-{d.end} {d.qualname} ({d.end - d.line + 1} lines): no reference '
          f'in any file that names its module')
    for s in digest_construction_violations(census):
        if 'also written' in s.detail:
            ruled = any(s.rel == rel and s.function.split('.')[-1] == name for rel, name in RULED_IDENTITY)
            w(f"  (ii) file_content_hash shape, counted: "
              f"{'ruled an identity construction (Coordinator 2026-10-03)' if ruled else 'NOT YET RULED'}: {s}")
    w('')
    w('Allowlist (by reason class; members matched by file + def + kind):')
    for name, entry in ALLOWLIST.items():
        w(f"  {name}: {entry['reason']}")
        for member in entry['members']:
            if name == 'embedded_script':
                hit = [u for u in census.units if (u.rel, u.embedded) == (member.rel, member.function)]
                where = f'found at {hit[0].offset + 1}' if hit else 'NOT FOUND'
            elif name == 'dead_code':
                hit = [d for d in census_module._definitions(census)[0]
                       if d.rel == member.rel and d.name == member.function]
                where = f'found at {hit[0].line}' if hit else 'NOT FOUND'
            else:
                sites = [s for c in ('c.primitive', 'b.sha256', 'a.digest') for s in census.sites[c]
                         if member.matches(s)]
                where = (f"found at {', '.join(str(s.line) for s in sites)}" if sites else
                         'no counted site (the pattern does not count this construction)' if member.kind == 'raw-bytes'
                         else 'NOT FOUND')
            w(f'    {member.rel}:{member.line} [{member.function}] {member.kind}: {where}')
    if show_sites:
        w('')
        for category in sorted(census.sites):
            w(f'== {category}')
            w(fmt(census.sites[category], limit=10_000))
    return '\n'.join(out)


CENSUS_DEAD = {
    'replace_provenance', 'desk_note_candidates', '_desk_note_scope_matches', 'retrieve_desk_docs',
    'DeskDocRetrievalResult', '_repo_scope', 'render_bounded', '_render', '_markdown_text',
    'NavigationReadiness', 'prepare_targets', 'export_receipt', 'default_lifecycle',
    'ProductionLocalEmbedding', 'EmbeddingRunManifest', 'capture_identity', 'scope_ids',
    'SummaryProposer', 'to_dict',
}


if __name__ == '__main__':
    if '--report' in sys.argv or '--sites' in sys.argv:
        print(report(show_sites='--sites' in sys.argv))
        raise SystemExit(0)
    raise SystemExit('usage: python tests/test_t11_leaf_single_home.py --report [--sites]')
