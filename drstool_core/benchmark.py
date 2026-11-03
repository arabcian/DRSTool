"""Repeatable, Qt-independent frame-capture quality and A/B analysis.

No statistical significance is asserted for different scenes or capture sources.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median, pstdev
from random import Random
from typing import Sequence


@dataclass(frozen=True)
class CaptureSummary:
    frames: int
    duration_s: float
    average_fps: float
    one_percent_low_fps: float
    p99_ms: float
    median_ms: float
    stutters: int


def summarize(frames: Sequence[float]) -> CaptureSummary:
    if len(frames) < 10 or any(not math.isfinite(x) or x <= 0 or x >= 10000 for x in frames):
        raise ValueError('Capture requires >=10 finite, positive frametimes below 10000 ms')
    ordered = sorted(frames)
    total = math.fsum(frames)
    worst = ordered[-max(1, math.ceil(len(frames) * .01)):]
    med = median(ordered)
    return CaptureSummary(
        len(frames), total / 1000, len(frames) * 1000 / total,
        1000 * len(worst) / math.fsum(worst),
        ordered[min(len(frames) - 1, math.ceil(.99 * len(frames)) - 1)], med,
        sum(x > 2 * med for x in frames),
    )


def compare(a: Sequence[float], b: Sequence[float], *, kind_a: str = '', kind_b: str = '',
            scene_confirmed: bool = False) -> dict:
    """Return descriptive differences; do not imply a controlled A/B test."""
    sa, sb = summarize(a), summarize(b)
    warnings = []
    if max(sa.duration_s, sb.duration_s) / min(sa.duration_s, sb.duration_s) > 1.2:
        warnings.append('Capture durations differ by more than 20%')
    if kind_a and kind_b and kind_a != kind_b:
        warnings.append('Capture formats may represent different frame streams')
    if not scene_confirmed:
        warnings.append('Same game scene and matching test conditions are not confirmed')
    if min(sa.duration_s, sb.duration_s) < 30:
        warnings.append('Capture under 30s; longer repeatable captures recommended')
    return {
        'a': sa.__dict__, 'b': sb.__dict__,
        'fps_change_pct': (sb.average_fps / sa.average_fps - 1) * 100,
        'low_change_pct': (sb.one_percent_low_fps / sa.one_percent_low_fps - 1) * 100,
        'p99_change_pct': (sb.p99_ms / sa.p99_ms - 1) * 100,
        'warnings': warnings,
        'comparable': not warnings,
    }


def compare_repeats(a_runs: Sequence[Sequence[float]], b_runs: Sequence[Sequence[float]],
                    *, same_scene_confirmed: bool = False) -> dict:
    """Compare 3+ independent repeated runs, using median FPS per group."""
    if len(a_runs) < 3 or len(b_runs) < 3:
        raise ValueError('At least three captures per configuration are required')
    a = [summarize(x) for x in a_runs]
    b = [summarize(x) for x in b_runs]
    am, bm = median(s.average_fps for s in a), median(s.average_fps for s in b)
    warnings = []
    if not same_scene_confirmed:
        warnings.append('Identical scene and test conditions have not been confirmed')
    if min(s.duration_s for s in a + b) < 30:
        warnings.append('At least one capture is shorter than 30 seconds')
    if max(s.duration_s for s in a + b) / min(s.duration_s for s in a + b) > 1.2:
        warnings.append('Capture durations differ by more than 20%')
    # Run-level variability, rather than per-frame variability, is what
    # determines whether repeated tests are consistent enough to compare.
    aframes = [s.average_fps for s in a]
    bframes = [s.average_fps for s in b]
    cv_a = 100 * pstdev(aframes) / (sum(aframes) / len(aframes))
    cv_b = 100 * pstdev(bframes) / (sum(bframes) / len(bframes))
    if max(cv_a, cv_b) > 5:
        warnings.append('Run-to-run FPS variability exceeds 5%; comparisons may be unreliable')
    # Deterministic, run-level bootstrap interval. Not a causal or formal
    # significance claim; captures are usually autocorrelated within a run.
    rng = Random(0)
    diffs = []
    for _ in range(1000):
        sampled_a = median(rng.choice(aframes) for _ in aframes)
        sampled_b = median(rng.choice(bframes) for _ in bframes)
        diffs.append((sampled_b / sampled_a - 1) * 100)
    diffs.sort()
    low, high = diffs[25], diffs[974]
    verdict = 'inconclusive'
    if not warnings:
        if low > 0:
            verdict = 'B appears faster (repeat-run evidence only)'
        elif high < 0:
            verdict = 'B appears slower (repeat-run evidence only)'
    return {'a_runs': len(a), 'b_runs': len(b), 'median_fps_a': am,
            'median_fps_b': bm, 'median_change_pct': (bm / am - 1) * 100,
            'run_cv_pct_a': cv_a, 'run_cv_pct_b': cv_b,
            'bootstrap_interval_pct': [low, high], 'verdict': verdict,
            'warnings': warnings, 'comparable': not warnings}
