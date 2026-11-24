"""Shared Qt-free MangoHud and FLM capture parsing.

The analyzer GUI imports the same implementation as recorded CLI sessions.
"""

import csv
from statistics import median
import itertools
import math
from pathlib import Path
from typing import List, Optional
from .frame_statistics import metrics

def load_frametimes(path: str) -> dict:
    # Streamed: a long MangoHud log can be 100 MB; only the first 12 lines are needed up front.
    with open(path, encoding="utf-8", errors="replace") as fh:
        head = [ln.rstrip("\r\n") for ln in itertools.islice(fh, 12)]
        return _parse_frametimes(path, head, fh)


def _parse_frametimes(path: str, head: List[str], rest) -> dict:
    hdr, kind = None, ""
    for i, ln in enumerate(head):
        candidate = [x.strip().lower() for x in next(csv.reader([ln]))]
        if candidate and candidate[0] == "flip_ns" and "interval_ns" in candidate:
            hdr, kind = i, "flm"
            break
        if "frametime" in candidate:
            hdr, kind = i, "mangohud"
            break
    if hdr is None:
        raise ValueError("not a MangoHud log (no 'frametime' column) or an FLM_CSV file")
    cols = [c.strip().lower() for c in next(csv.reader([head[hdr]]))]
    body = itertools.chain(head[hdr + 1:], rest)
    ft: List[float] = []
    extra = {}
    sensors: dict = {}
    if kind == "mangohud":
        ix = cols.index("frametime")
        # GPU sensor columns MangoHud logs next to the frametime (sample cadence may differ from frame cadence)
        sx = {lab: cols.index(c) for lab, c in (("GPU load %", "gpu_load"), ("VRAM", "gpu_vram_used"),
                                                ("GPU temp °C", "gpu_temp")) if c in cols}
        sensors = {lab: [] for lab in sx}
        for p in csv.reader(body):
            try:
                v = float(p[ix])
            except (ValueError, IndexError):
                continue
            if math.isfinite(v) and v > 0:
                if len(ft) >= 2_000_000:
                    raise ValueError("capture exceeds 2 million samples; split it before analysis")
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
        for p in csv.reader(body):
            try:
                v = float(p[ix]) / 1e6
            except (ValueError, IndexError):
                continue
            if math.isfinite(v) and v > 0:
                if len(ft) >= 2_000_000:
                    raise ValueError("capture exceeds 2 million samples; split it before analysis")
                ft.append(v)
                if 0 <= ih < len(p) and p[ih].strip() == "1":
                    hit += 1
        extra["FLM hitches"] = hit
    if kind == 'mangohud':
        extra['Capture semantics'] = 'MangoHud samples; frame count, duration and FPS are estimates unless per-frame logging was used.'
    if len(ft) < 10:
        raise ValueError("fewer than 10 valid samples in the log")
    srt = sorted(ft)            # sorted once: frame_stats and the plot's scale both need it
    extra.update(_sensor_summary(ft, srt, sensors))
    return {"name": Path(path).name, "ft": ft, "sorted": srt, "kind": kind, "extra": extra,
            "stats": frame_stats(ft, srt), "sensors": sensors}


def _sensor_summary(ft: List[float], srt: List[float], sensors: dict) -> dict:
    """Peaks of the logged GPU sensors and how many stutters happened with VRAM near its peak."""
    out = {}
    for lab, vals in sensors.items():
        peak = max((v for v in vals if math.isfinite(v)), default=None)
        if peak is not None:
            out[f"max {lab}"] = f"{peak:.0f}"
    vram = sensors.get("VRAM")
    if vram and any(math.isfinite(v) for v in vram):
        top = max(v for v in vram if math.isfinite(v))
        lim = 2 * median(srt)
        st = [i for i, v in enumerate(ft) if v > lim]
        if st:
            near = sum(1 for i in st if math.isfinite(vram[i]) and vram[i] >= 0.95 * top)
            out["stutters at ≥95% observed VRAM peak (not capacity)"] = f"{near}/{len(st)}"
    return out


def frame_stats(ft: List[float], s: Optional[List[float]] = None) -> List[tuple]:
    """[(label, value, higher_is_better|None, fmt)]; `s` = ft already sorted (optional)."""
    m = metrics(ft, s)
    return [("Frames", m['frames'], None, "{:.0f}"),
            ("Duration (s)", m['duration_s'], None, "{:.1f}"),
            ("Average FPS", m['average_fps'], True, "{:.1f}"),
            ("1% low FPS", m['one_percent_low_fps'], True, "{:.1f}"),
            ("0.1% low FPS", m['point_one_percent_low_fps'], True, "{:.1f}"),
            ("Median frametime (ms)", m['median_ms'], False, "{:.2f}"),
            ("p99 frametime (ms)", m['p99_ms'], False, "{:.2f}"),
            ("p99.9 frametime (ms)", m['p999_ms'], False, "{:.2f}"),
            ("Max frametime (ms)", m['max_ms'], False, "{:.1f}"),
            ("Frametime σ (ms)", m['sigma_ms'], False, "{:.2f}"),
            ("Stutters (> 2× median)", m['stutters'], False, "{:.0f}"),
            ("Hitches (> 50 ms)", m['hitches'], False, "{:.0f}")]
