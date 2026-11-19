"""Qt-free repeat-run metadata comparisons for game capture histories.

Summaries make *descriptive*, conditional assessments only. A matching label
alone does not establish identical scenes, settings, or causal attribution.
"""
from __future__ import annotations

import math
from collections import defaultdict
from statistics import median, pstdev

_METRICS = {'avg': False, 'low1': False, 'p99': True, 'stutters': True}
_THRESHOLDS = {'avg': 5.0, 'low1': 8.0, 'p99': 10.0, 'stutters': 100.0}


def _finite(v) -> bool:
    return isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def group_captures(history: list[dict]) -> list[dict]:
    """Use full profile+fingerprint, capture type, scene, driver and runner keys."""
    groups: dict[tuple, list] = defaultdict(list)
    for r in history:
        if not isinstance(r, dict) or not _finite(r.get('avg')) or r.get('avg') == 0:
            continue
        key = (r.get('profile') or '', r.get('fp') or '', r.get('kind') or '',
               r.get('scene') or '', r.get('driver') or '', r.get('proton') or '',
               r.get('dxvk') or '', r.get('vkd3d') or '')
        groups[key].append(r)
    out = []
    for (profile, fp, kind, scene, driver, proton, dxvk, vkd3d), records in groups.items():
        records = sorted(records, key=lambda x: x.get('mtime') or 0)
        metrics = {k: median(r[k] for r in records if _finite(r.get(k)))
                   for k in _METRICS if any(_finite(r.get(k)) for r in records)}
        avgs = [r['avg'] for r in records]
        durations = [r['dur'] for r in records if _finite(r.get('dur')) and r['dur'] > 0]
        cv = 100 * pstdev(avgs) / (sum(avgs) / len(avgs)) if len(avgs) > 1 else None
        out.append({'profile': profile, 'fp': fp, 'kind': kind, 'scene': scene,
                    'driver': driver, 'proton': proton, 'dxvk': dxvk, 'vkd3d': vkd3d,
                    'runs': len(records),
                    'last': records[-1].get('mtime') or 0, 'metrics': metrics,
                    'fps_cv_pct': cv, 'duration_s': median(durations) if durations else None,
                    'captures': [r.get('path', '') for r in records]})
    return sorted(out, key=lambda g: g['last'])


def _context(a: dict, b: dict) -> list[str]:
    warnings = []
    if a['profile'] != b['profile']:
        warnings.append('Different profile names')
    for k in ('kind', 'scene', 'driver', 'proton', 'dxvk', 'vkd3d'):
        if not a[k] or not b[k]:
            warnings.append(f'{k} not recorded for both groups')
        elif a[k] != b[k]:
            warnings.append(f'{k} differs')
    if a['fp'] == b['fp']:
        warnings.append('Same profile fingerprint; no configuration change to compare')
    da, db = a.get('duration_s'), b.get('duration_s')
    if da is None or db is None:
        warnings.append('Capture durations missing')
    else:
        if min(da, db) < 30:
            warnings.append('At least one median capture duration is under 30s')
        if max(da, db) / min(da, db) > 1.2:
            warnings.append('Capture durations differ by more than 20%')
    if min(a['runs'], b['runs']) < 3:
        warnings.append('At least 3 separate runs per group are recommended')
    if any((g['fps_cv_pct'] is not None and g['fps_cv_pct'] > 5) for g in (a, b)):
        warnings.append('Run-to-run FPS variability exceeds 5%')
    return warnings


def compare_groups(a: dict, b: dict) -> dict:
    warnings = _context(a, b)
    changes = {}
    for metric in _METRICS:
        va, vb = a['metrics'].get(metric), b['metrics'].get(metric)
        if va is None or vb is None:
            continue
        if va == 0:
            # An infinite relative change for stutter=0 has no stable interpretation.
            changes[metric] = {'before': va, 'after': vb, 'pct': None}
            continue
        pct = 100 * (vb / va - 1)
        changes[metric] = {'before': va, 'after': vb, 'pct': pct}
    signals = []
    if not warnings:
        for metric, worse_when_increases in _METRICS.items():
            change = changes.get(metric)
            if change is None or change['pct'] is None:
                continue
            bad = change['pct'] >= _THRESHOLDS[metric] if worse_when_increases else change['pct'] <= -_THRESHOLDS[metric]
            if bad:
                signals.append(metric)
    return {'from_fp': a['fp'], 'to_fp': b['fp'], 'from_profile': a['profile'], 'to_profile': b['profile'],
            'warnings': warnings, 'comparable': not warnings,
            'changes': changes, 'possible_regression': bool(signals), 'regressed_metrics': signals,
            'disclaimer': 'Descriptive association only; not proof of causation or test-scene equivalence.'}


def trend_report(history: list[dict], *, profile: str = '') -> dict:
    """Compare last two distinct fingerprints of the same profile when possible."""
    groups = group_captures(history)
    if profile:
        groups = [g for g in groups if g['profile'] == profile]
    pairs = [(a, b) for i, a in enumerate(groups) for b in groups[i + 1:]
             if a['fp'] != b['fp'] and a['profile'] == b['profile']]
    if not pairs:
        return {'groups': groups, 'comparison': None,
                'message': 'At least two profile fingerprints with captured runs are required.'}
    a, b = max(pairs, key=lambda pair: pair[1]['last'])
    comparison = compare_groups(a, b)
    return {'groups': groups, 'comparison': comparison,
            'message': 'Possible performance regression' if comparison['possible_regression']
            else ('Insufficient or different test conditions' if not comparison['comparable']
                  else 'No threshold-crossing regression observed')}
