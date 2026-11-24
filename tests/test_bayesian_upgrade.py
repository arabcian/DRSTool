"""Regression tests for the adaptive Crash Lab survival model (Qt-free)."""
import math
import random

import pytest

from drstool_core import xid_analysis as xa


def test_no_synthetic_baseline_and_nonreproduction():
    s = xa.AdaptiveSearch(['a', 'b'], crash_seconds=100, known_baseline=False)
    assert s.model.runs == []
    assert s.phase == 'baseline' and set(s.pending) == {'a', 'b'}
    s.answer(False, 100)
    assert not s.done
    s.answer(False, 200)
    assert s.outcome == ('not-reproduced', [])
    assert not any(c for _, _, c in s.observations)


def test_prior_and_duplicate_keys_validation():
    m = xa.CrashModel(['a', 'a', 'b'], {'a': 1000, 'b': -1000}, p_none=0, p_or=0)
    assert m.items == ['a', 'b']
    assert math.fsum(p for _, p in m.posterior()) == pytest.approx(1)
    assert all(math.isfinite(p) for _, p in m.posterior())
    assert xa.CrashModel([]).posterior() == [(('none',), 1.0)]
    for kwargs in ({'crash_seconds': float('nan')}, {'background_hours': 0}, {'p_none': 0.9}, {'prior': {'a': float('inf')}}):
        with pytest.raises(ValueError):
            xa.CrashModel(['a', 'b'], **kwargs)
    with pytest.raises(ValueError):
        m.add(['unknown'], 1, False)


def test_mixed_hypotheses_truth_tables_and_groups():
    m = xa.CrashModel(list('abcdefgh'), triple_limit=8, higher_limit=8, mixed_limit=8, max_order=8)
    h = ('mixed', 'a', 'b', 'c')
    h2 = ('mixed2', 'a', 'b', 'c', 'd')
    assert h in m.hyps and h2 in m.hyps
    assert m._holds(h, frozenset('ab')) and m._holds(h, frozenset('c'))
    assert not m._holds(h, frozenset('a'))
    assert m._holds(h2, frozenset('ab')) and m._holds(h2, frozenset('cd'))
    assert not m._holds(h2, frozenset('ac'))
    assert xa.groups_of(h2) == [['a', 'b'], ['c', 'd']]
    assert ('and', *'abcdefgh') in m.hyps
    assert 'd' in xa.describe(h2)


@pytest.mark.parametrize('groups', [[set('ab'), set('c')], [set('ab'), set('cd')], [set('abcdef')]])
def test_end_to_end_complex_interactions(groups):
    s = xa.AdaptiveSearch(list('abcdefg'), crash_seconds=100, max_tests=80)
    while not s.done:
        crashed = any(g <= set(s.pending) for g in groups)
        s.answer(crashed, 100 if crashed else max(1000, s.play_time()))
    assert s.outcome[0] == 'found'
    assert {frozenset(g) for g in s.outcome[1]} == {frozenset(g) for g in groups}
    assert not s.observations[-1][2]
    assert s.verification_probability >= .8


def test_short_verification_never_sufficient():
    s = xa.AdaptiveSearch(['a'], crash_seconds=100)
    for _ in range(5):
        s.model.add(['a'], 10, True)
    s.model.add([], .01, False)
    s._plan()
    assert not s.done
    assert s.verification_probability < .8
    s.model.add([], 5000, False)
    s._plan()
    assert s.outcome == ('found', [['a']])


def test_saved_evidence_preserves_exact_subsets_and_duration_and_undo():
    keys = list('abcd')
    s = xa.AdaptiveSearch(keys, [True, False], [11.1, 92.25], tested=[['a','b'], ['b']],
                          pending=['c'], planned_seconds=77.5, crash_seconds=100)
    assert s.observations == [(frozenset('ab'), 11.1, True), (frozenset('b'), 92.25, False)]
    assert s.pending == ['c'] and s.play_time() == 77.5
    s.undo()
    assert s.pending == ['b']
    assert s.observations == [(frozenset('ab'), 11.1, True)]
    with pytest.raises(ValueError):
        xa.AdaptiveSearch(keys, [True], [11.1], tested=[])


def test_planner_gain_and_probability_match_public_methods():
    m = xa.CrashModel(list('abcdefg'), triple_limit=7, higher_limit=7, mixed_limit=7, max_order=8)
    m.add(list('abcdefg'), 110, True)
    m.add(['a','b'], 250, False)
    on, seconds, gain, risk = m.best_plan(100)
    assert 30 <= seconds <= 14400
    assert gain == pytest.approx(m.info_gain(on, seconds), abs=1e-12)
    assert risk == pytest.approx(m.crash_probability(on, seconds), abs=1e-12)
    assert 0 <= risk <= 1 and gain >= 0
    assert (on, seconds, gain, risk) == m.best_plan(100)


def test_incremental_sufficient_statistics_equal_rebuild():
    rng = random.Random(18)
    args = dict(triple_limit=6, higher_limit=6, mixed_limit=6, max_order=8)
    keys = list('abcdef')
    m = xa.CrashModel(keys, **args)
    for _ in range(15):
        m.posterior()
        m.add(rng.sample(keys, rng.randrange(7)), rng.uniform(.1, 1000), rng.choice([True, False]))
    other = xa.CrashModel(keys, **args)
    for on, t, c in m.runs:
        other.add(on, t, c)
    assert dict(m.posterior()) == pytest.approx(dict(other.posterior()))


def test_bounded_sample_reaches_beyond_focused_settings():
    keys = [f'k{i}' for i in range(150)]
    m = xa.CrashModel(keys, pair_limit=5, triple_limit=5, higher_limit=5, mixed_limit=5, max_order=8)
    assert len(m.hyps) < 4000
    assert any(h[0] == 'and' and len(h) == 4 and 'k149' in h for h in m.hyps)
    assert ('and', *keys) in m.hyps
    assert xa._symptom([45,154]) == 'secondary-only'
    assert xa._symptom([45,31,154]) == '31'
