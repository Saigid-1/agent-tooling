"""T10 P5: results are identical to the goldens and to the ``select`` oracle.

Order: docs/work/orders/T10-read-path-projection.md (P5). The corpus
(``t10_corpus.STEPS``) covers the order's list: several desks with resolved,
unresolved and conflicting attribution; legacy rows (one whose binding leaves
the registry, one linked to a session whose owner claims become inactive, one
linked later); source episodes with and without ``source_coordinates``,
non-dict coordinates, string/float/bool ``start``, two files in one session,
``start == start_offset``; ranged claims, a supersession chain of three with
the superseding claim retracted, an owner reassignment, ``valid_from`` /
``valid_until``; a linked session of another tenant; more than
``candidate_limit`` matches in another desk; ``foo_bar`` / ``café bar``
FTS-vs-regex disagreements ranked above valid matches; desk and topic scope,
attribution, claim and session filters, profile views; ``memory.list`` order
and error categories. Reads run after every write step.

The goldens were generated FROM BASE with a frozen clock by
``tests/fixtures/t10-read-path-projection/generate.py`` (command in its
docstring). They record that generator's own sha256 and the content hash of
the base product tree (the generator refuses any other tree), not a commit. The
oracle is the base ``SessionSources.select`` (``t10_oracle``).
"""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

import t10_corpus

GOLDENS = Path(__file__).resolve().parent / 'fixtures' / 't10-read-path-projection' / 'goldens.json'
GENERATOR = GOLDENS.with_name('generate.py')
STEP_NAMES = [name for name, _ in t10_corpus.STEPS]
# T12b R2 (ruled exception, Verification, 2026-10-04): the `status` read (memory.connection_status) may differ from
# its golden ONLY by an added integer `index_lag`, pinned here: 0 at every step the corpus helper drained
# (t10_corpus.run), and at the one step that leaves its seal for the indexer (t10_corpus.UNDRAINED_STEPS: one seal
# row and one link row) the rows that wait. Every other read and every write receipt stays byte-identical.
STATUS_KEY = 'status'
INDEX_LAG = {name: (2 if name in t10_corpus.UNDRAINED_STEPS else 0) for name in STEP_NAMES}


def _r2_status(output, step):
    """The `status` output with the ruled `index_lag` removed, or a reason it is not the ruled difference."""
    if not isinstance(output, dict) or 'index_lag' not in output:
        return output, None  # base: no index_lag
    lag = output['index_lag']
    if type(lag) is not int or lag != INDEX_LAG[step]:
        return output, f'index_lag {lag!r}, pinned {INDEX_LAG[step]} at {step}'
    return {k: v for k, v in output.items() if k != 'index_lag'}, None


@pytest.fixture(scope='module')
def goldens():
    goldens = json.loads(GOLDENS.read_text())
    # Fixture checks: the committed goldens are the base's, for this corpus and battery: written by this
    # generator (its own sha256), from the base product tree it pins by content.
    spec = importlib.util.spec_from_file_location('t10_generate', GENERATOR)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert goldens['meta']['generator_sha256'] == hashlib.sha256(GENERATOR.read_bytes()).hexdigest()
    assert goldens['meta']['product_sha256'] == generator.BASE_PRODUCT_SHA256
    assert [s['step'] for s in goldens['steps']] == STEP_NAMES
    tools = {call['tool'] for call in goldens['calls'].values()}
    assert {'memory.connection_status', 'memory.search', 'memory.session', 'memory.bindings', 'memory.resume',
            'memory.handoff_page', 'memory.status', 'memory.list', 'memory.handoff', 'memory.evidence_directory',
            'memory.episode_directory', 'memory.read_event'} <= tools
    assert any(isinstance(s['write'], dict) and 'capsule_id' in s['write'] for s in goldens['steps'])
    return goldens


@pytest.fixture(scope='module')
def replay(tmp_path_factory):
    oracle = {}

    def check(ctx, name):
        oracle[name] = t10_corpus.oracle_mismatches(ctx)

    _, record = t10_corpus.run(tmp_path_factory.mktemp('t10-p5') / 'corpus', on_step=check)
    return record, oracle


def _golden_output(goldens, key, number):
    stored = goldens['outputs'][key]
    target = goldens['steps'][number]['digests'][key]
    for name in ('first', 'final'):
        if t10_corpus.digest(stored[name]) == target:
            return stored[name]
    return None


def _explain(goldens, key, number, actual):
    golden = _golden_output(goldens, key, number)
    call = goldens['calls'][key]
    text = json.dumps(actual, sort_keys=True)[:1500]
    if golden is None:
        return (f'{key} {call["tool"]} {call["arguments"]}: output differs from the golden digest '
                f'(rerun the generator at base to see that step in full)\nactual: {text}')
    return (f'{key} {call["tool"]} {call["arguments"]}\ngolden: {json.dumps(golden, sort_keys=True)[:1500]}\n'
            f'actual: {text}')


@pytest.mark.parametrize('number', range(len(STEP_NAMES)), ids=STEP_NAMES)
def test_p5_outputs_equal_goldens(goldens, replay, number):
    record, _ = replay
    golden, actual = goldens['steps'][number], record[number]
    assert actual['step'] == golden['step']
    assert actual['write'] == golden['write'], f'write receipt differs: {actual["write"]}'
    assert set(actual['reads']) == set(golden['digests']), 'the read battery differs from the goldens'
    status, lag_problem = _r2_status(actual['reads'][STATUS_KEY]['output'], golden['step'])
    assert lag_problem is None, f'the status read differs from its golden by more than the ruled index_lag: {lag_problem}'
    outputs = {key: (status if key == STATUS_KEY else actual['reads'][key]['output']) for key in golden['digests']}
    differing = [key for key, digest in golden['digests'].items() if t10_corpus.digest(outputs[key]) != digest]
    assert not differing, (f'{len(differing)} read(s) differ after step {golden["step"]}:\n' +
                           '\n'.join(_explain(goldens, key, number, actual['reads'][key]['output'])
                                     for key in differing[:3]))


@pytest.mark.parametrize('number', range(len(STEP_NAMES)), ids=STEP_NAMES)
def test_p5_scope_equals_select_oracle(replay, number):
    _, oracle = replay
    problems = oracle[STEP_NAMES[number]]
    assert not problems, '\n'.join(problems[:20])
