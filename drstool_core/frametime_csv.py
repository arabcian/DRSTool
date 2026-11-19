"""Shared Qt-free MangoHud and FLM capture parsing.

The analyzer GUI imports the same implementation as recorded CLI sessions.
"""

import bisect
import itertools
import math
from pathlib import Path
from typing import List, Optional

def load_frametimes(path: str) -> dict:
    # Streamed: a long MangoHud log can be 100 MB; only the first 12 lines are needed up front.
    with open(path, encoding="utf-8", errors="replace") as fh:
        head = [ln.rstrip("\r\n") for ln in itertools.islice(fh, 12)]
        return _parse_frametimes(path, head, fh)


def _parse_frametimes(path: str, head: List[str], rest) -> dict:
    hdr, kind = None, ""
    for i, ln in enumerate(head):
        low = ln.lower()
        if low.startswith("flip_ns,"):
            hdr, kind = i, "flm"
            break
        if "frametime" in low and "," in low:
            hdr, kind = i, "mangohud"
            break
    if hdr is None:
        raise ValueError("not a MangoHud log (no 'frametime' column) or an FLM_CSV file")
    cols = [c.strip().lower() for c in head[hdr].split(",")]
    body = itertools.chain(head[hdr + 1:], rest)
    ft: List[float] = []
    extra = {}
    sensors: dict = {}
    if kind == "mangohud":
        ix = cols.index("frametime")
        # GPU sensor columns MangoHud logs next to the frametime (one sample per frame)
        sx = {lab: cols.index(c) for lab, c in (("GPU load %", "gpu_load"), ("VRAM", "gpu_vram_used"),
                                                ("GPU temp °C", "gpu_temp")) if c in cols}
        sensors = {lab: [] for lab in sx}
        cut = max([ix] + list(sx.values())) + 1
        for ln in body:
            p = ln.split(",", cut)
            try:
                v = float(p[ix])
            except (ValueError, IndexError):
                continue
            if 0 < v < 10000:
                ft.append(v)
                for lab, j in sx.items():
                    try:
                        sensors[lab].append(float(p[j]))
                    except (ValueError, IndexError):
                        sensors[lab].append(float("nan"))
    else:
        ix = cols.index("interval_ns")
        ih = cols.index("is_hitch") if "is_hitch" in cols else -1
        hit = 0
        for ln in body:
            p = ln.split(",", max(ix, ih) + 1)
            try:
                v = float(p[ix]) / 1e6
            except (ValueError, IndexError):
                continue
            if 0 < v < 10000:
                ft.append(v)
                if ih >= 0 and p[ih].strip() == "1":
                    hit += 1
        extra["FLM hitches"] = hit
    if len(ft) < 10:
        raise ValueError("fewer than 10 frames in the log")
    srt = sorted(ft)            # sorted once: frame_stats and the plot's scale both need it
    extra.update(_sensor_summary(ft, srt, sensors))
    return {"name": Path(path).name, "ft": ft, "sorted": srt, "kind": kind, "extra": extra,
            "stats": frame_stats(ft, srt), "sensors": sensors}


def _sensor_summary(ft: List[float], srt: List[float], sensors: dict) -> dict:
    """Peaks of the logged GPU sensors and how many stutters happened with VRAM near its peak."""
    out = {}
    for lab, vals in sensors.items():
        good = [v for v in vals if v == v]
        if good:
            out[f"max {lab}"] = f"{max(good):.0f}"
    vram = sensors.get("VRAM")
    if vram and any(v == v for v in vram):
        top = max(v for v in vram if v == v)
        lim = 2 * srt[len(srt) // 2]
        st = [i for i, v in enumerate(ft) if v > lim]
        if st:
            near = sum(1 for i in st if vram[i] == vram[i] and vram[i] >= 0.95 * top)
            out["stutters at ≥95% peak VRAM"] = f"{near}/{len(st)}"
    return out


def frame_stats(ft: List[float], s: Optional[List[float]] = None) -> List[tuple]:
    """[(label, value, higher_is_better|None, fmt)]; `s` = ft already sorted (optional)."""
    n, total = len(ft), sum(ft)
    if s is None:
        s = sorted(ft)
    med = s[n // 2]
    mean = total / n

    def worst(frac):
        k = max(1, int(n * frac))
        return sum(s[-k:]) / k

    def pct(p):
        return s[min(n - 1, int(p / 100 * n))]
    return [("Frames", n, None, "{:.0f}"),
            ("Duration (s)", total / 1000, None, "{:.1f}"),
            ("Average FPS", 1000 * n / total, True, "{:.1f}"),
            ("1% low FPS", 1000 / worst(0.01), True, "{:.1f}"),
            ("0.1% low FPS", 1000 / worst(0.001), True, "{:.1f}"),
            ("Median frametime (ms)", med, False, "{:.2f}"),
            ("p99 frametime (ms)", pct(99), False, "{:.2f}"),
            ("p99.9 frametime (ms)", pct(99.9), False, "{:.2f}"),
            ("Max frametime (ms)", s[-1], False, "{:.1f}"),
            ("Frametime σ (ms)", math.sqrt(math.fsum((x - mean) ** 2 for x in ft) / n), False, "{:.2f}"),
            ("Stutters (> 2× median)", n - bisect.bisect_right(s, 2 * med), False, "{:.0f}"),
            ("Hitches (> 50 ms)", n - bisect.bisect_right(s, 50), False, "{:.0f}")]


