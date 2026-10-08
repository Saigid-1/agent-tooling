"""T12c C1: search cost is independent of other desks' and other tenants' matching text.

Order: docs/work/orders/T12c-per-desk-postings.md, C1 (and A2, Verification). Fixture and measurement:
tests/t12c_c1_world.py. Falsifier: "C1's growth beyond its bounds at N = 10,000 or 100,000, in either scope, at
K = 0 or K = 1,000 unapplied `desks_changed` rows".

- test_c1_vm_steps_bound: GREEN-IF, in each scope (desk, topic) and at each K (0, 1,000), the VM steps of one
  search at N grow at most 1.5x against N = 0 at the same K.
- test_c1_median_wall_bound: GREEN-IF the median of 7 warm in-process runs grows at most 2x against N = 0 at
  the same K. The failure message carries the ratio and the raw samples.
- test_c1_results_do_not_change_with_n (a guard, green at base): the searched scope's results (ids, events,
  order), total, covered and status at N equal those at N = 0, and covered == total.
- test_c1_unapplied_rows_change_no_result (a guard, green at base): at each N, the K = 1,000 results equal the
  K = 0 ones (tag claims move no episode).

N = 10,000 runs in the default suite; N = 100,000 runs only with T12C_C1_LARGE=1 (t12c_seams.LARGE_OPT_IN).
Every test prints the ratio table (pytest -s shows it; a failure carries it).
"""
from __future__ import annotations

import os

import pytest

import t12c_c1_world as c1
from t12c_seams import LARGE_OPT_IN

LARGE = 100_000
DEFAULT = 10_000
SIZES = [DEFAULT, pytest.param(LARGE, marks=pytest.mark.skipif(
    os.environ.get(LARGE_OPT_IN) != '1', reason=f'N = 100,000 is opt-in: {LARGE_OPT_IN}=1'))]
CASES = [(scope, k) for scope in ('desk', 'topic') for k in (0, c1.K)]


class Families:
    """Each family (desk, topic) is built once, at N = 0, 10,000 and (opted in) 100,000, and measured together."""

    def __init__(self, factory):
        self.factory = factory
        self.families = {}

    def get(self, family, n):
        if family not in self.families:
            sizes = [0, DEFAULT] + ([LARGE] if os.environ.get(LARGE_OPT_IN) == '1' else [])
            built = c1.build_family(family, sizes, self.factory.mktemp(f't12c-c1-{family}'))
            for handle in built.values():
                self._check(handle)
            self.families[family] = built
        return self.families[family][n]

    @staticmethod
    def _check(handle):
        """Fixture checks (positive controls): the searched desk holds its 79 episodes and searches answer; the N
        other episodes are indexed and match, outside the searched scope; K rows wait, unapplied."""
        problems = []
        for label in ('k0', f'k{c1.K}'):
            if handle.controls[f'{label}:own_total'] != c1.OWN_EPISODES:
                problems.append(f'{label}: desk alpha total {handle.controls[f"{label}:own_total"]}, not {c1.OWN_EPISODES}')
        for (scope, k), sample in handle.samples.items():
            if sample.error is not None or len(sample.output.get('results', [])) != 20:
                problems.append(f'{scope}/K={k}: the search did not return 20 results: {sample.error}')
        if handle.n:
            other = handle.n // 2 if handle.family == 'desk' else handle.n
            if (handle.controls['k0:omega_topic_total'] or 0) < other or \
                    handle.controls['k0:omega_topic_covered'] != handle.controls['k0:omega_topic_total'] or \
                    handle.controls['k0:omega_topic_results'] != 20:
                problems.append(f'the other tenant does not hold {other} indexed matching episodes: {handle.controls}')
            if handle.family == 'desk' and ((handle.controls['k0:beta_total'] or 0) < handle.n // 2 or
                                            handle.controls['k0:beta_covered'] != handle.controls['k0:beta_total'] or
                                            handle.controls['k0:beta_results'] != 20):
                problems.append(f'desk beta does not hold {handle.n // 2} indexed matching episodes: {handle.controls}')
        if handle.controls['k0:outbox'] != 0:
            problems.append(f'the drain left {handle.controls["k0:outbox"]} outbox rows before K')
        if handle.controls[f'k{c1.K}:outbox'] != c1.K:
            problems.append(f'{handle.controls[f"k{c1.K}:outbox"]} outbox rows wait, not K = {c1.K} (indexer stopped)')
        assert not problems, f'fixture {handle.family} N={handle.n}:\n' + '\n'.join(problems)


@pytest.fixture(scope='module')
def families(tmp_path_factory):
    return Families(tmp_path_factory)


def _family(scope):
    return 'desk' if scope == 'desk' else 'topic'


def _pair(families, scope, n):
    family = _family(scope)
    return families.get(family, 0), families.get(family, n)


def _report(families, n):
    lines = []
    for scope, k in CASES:
        base, grown = _pair(families, scope, n)
        lines += c1.ratio_lines(base, grown, scope, k)
    return '\n'.join(lines)


@pytest.mark.parametrize('n', SIZES)
@pytest.mark.parametrize('scope,k', CASES, ids=[f'{s}-K{k}' for s, k in CASES])
def test_c1_vm_steps_bound(families, scope, k, n):
    base, grown = _pair(families, scope, n)
    b, g = base.samples[(scope, k)], grown.samples[(scope, k)]
    table = _report(families, n)
    print(f'\nT12c C1 at N={n}\n{table}')
    assert g.steps <= c1.STEP_RATIO * b.steps, (
        f'{scope} scope, N={n}, K={k}: VM steps {b.steps} -> {g.steps} ({g.steps / b.steps:.2f}x > {c1.STEP_RATIO}x)'
        f'\n{table}')


@pytest.mark.parametrize('n', SIZES)
@pytest.mark.parametrize('scope,k', CASES, ids=[f'{s}-K{k}' for s, k in CASES])
def test_c1_median_wall_bound(families, scope, k, n):
    base, grown = _pair(families, scope, n)
    b, g = base.samples[(scope, k)], grown.samples[(scope, k)]
    table = _report(families, n)
    print(f'\nT12c C1 at N={n}\n{table}')
    assert g.median_ms <= c1.WALL_RATIO * b.median_ms, (
        f'{scope} scope, N={n}, K={k}: median wall {b.median_ms:.2f} -> {g.median_ms:.2f} ms '
        f'({g.median_ms / b.median_ms:.2f}x > {c1.WALL_RATIO}x)\nraw N=0: {b.wall_ms}\nraw N={n}: {g.wall_ms}\n{table}')


@pytest.mark.parametrize('n', SIZES)
@pytest.mark.parametrize('scope,k', CASES, ids=[f'{s}-K{k}' for s, k in CASES])
def test_c1_results_do_not_change_with_n(families, scope, k, n):
    base, grown = _pair(families, scope, n)
    before, after = c1.result_view(base.samples[(scope, k)]), c1.result_view(grown.samples[(scope, k)])
    assert after == before, f'{scope} scope, K={k}: the result at N={n} differs from N=0:\nN=0: {before}\nN={n}: {after}'
    assert after['covered'] == after['total'] and after['status'] == 'results', after


@pytest.mark.parametrize('n', [0] + SIZES)
@pytest.mark.parametrize('scope', ['desk', 'topic'])
def test_c1_unapplied_rows_change_no_result(families, scope, n):
    handle = families.get(_family(scope), n)
    k0, kk = c1.result_view(handle.samples[(scope, 0)]), c1.result_view(handle.samples[(scope, c1.K)])
    assert kk == k0, f'{scope} scope, N={n}: K={c1.K} tag claims changed the result:\nK=0: {k0}\nK={c1.K}: {kk}'
