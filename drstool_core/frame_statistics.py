"""Single definition of frame metrics for captures, history and benchmarks."""
from __future__ import annotations
import bisect
import math
from typing import Sequence


def metrics(frames: Sequence[float], ordered: Sequence[float] | None = None) -> dict:
    if not frames or any(not math.isfinite(x) or x <= 0 for x in frames):
        raise ValueError('Capture requires finite, positive frametimes')
    s = sorted(frames) if ordered is None else ordered
    n = len(frames)
    if len(s) != n:
        raise ValueError('Invalid sorted capture length')
    total = math.fsum(frames)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    mean = total / n

    def low(fraction):
        k = max(1, math.ceil(n * fraction))
        return 1000 * k / math.fsum(s[-k:])

    def percentile(fraction):
        return s[min(n - 1, max(0, math.ceil(n * fraction) - 1))]

    return dict(frames=n, duration_s=total / 1000, average_fps=1000 * n / total,
                one_percent_low_fps=low(.01), point_one_percent_low_fps=low(.001),
                median_ms=med, p99_ms=percentile(.99), p999_ms=percentile(.999),
                max_ms=s[-1], sigma_ms=math.sqrt(math.fsum((x - mean) ** 2 for x in frames) / n),
                stutters=n - bisect.bisect_right(s, 2 * med), hitches=n - bisect.bisect_right(s, 50))
