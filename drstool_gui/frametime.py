"""Frametime analysis (MangoHud CSV / FLM dumps)."""

import bisect
import itertools
import math
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QPointF, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .base import APP_ID
from .theme import T
from .tool_ui import _tool_btn
from .widgets import _desc_label, _LINEEDIT_QSS, _table_qss, _title_label


# ----------------------------------------------------------------------------
# Frametime analyser (MangoHud CSV / FLM_CSV) with A/B comparison
# ----------------------------------------------------------------------------


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
    if kind == "mangohud":
        ix = cols.index("frametime")
        for ln in body:
            p = ln.split(",", ix + 1)
            try:
                v = float(p[ix])
            except (ValueError, IndexError):
                continue
            if 0 < v < 10000:
                ft.append(v)
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
    return {"name": Path(path).name, "ft": ft, "sorted": srt, "kind": kind, "extra": extra,
            "stats": frame_stats(ft, srt)}


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


# ----------------------------------------------------------------------------
# Frametime analyser (plot)
# ----------------------------------------------------------------------------

class FrametimePlot(QWidget):
    COLORS = ["#76b900", "#5aa0ff"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series: List[tuple] = []          # (label, ft, ft_sorted)
        self._buckets: dict = {}
        self.setMinimumHeight(170)

    def set_series(self, series: List[tuple]):
        """series: [(label, ft, ft_sorted)]"""
        self._series = series
        self._buckets = {}                      # (series index, width) -> per-pixel maxima
        # Scale once per data change, not per repaint (was a full sort of every
        # series on each paintEvent / resize).
        if series:
            self._ymax = max(max(srt[int(len(ft) * 0.999)] for _l, ft, srt in series) * 1.4, 10.0)
            self._tmax = max(sum(ft) for _l, ft, _s in series) / 1000
        self.update()

    def paintEvent(self, _ev):              # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(T["surface"]))
        if not self._series:
            p.setPen(QColor(T["muted"]))
            p.drawText(self.rect(), Qt.AlignCenter, "Load a log to see the frametime graph")
            return
        L, B, R, TOP = 46, 20, 8, 8
        W, H = self.width() - L - R, self.height() - B - TOP
        if W < 20 or H < 20:
            return
        ymax, tmax = self._ymax, self._tmax
        p.setPen(QPen(QColor(T["border"])))
        f = QFont()
        f.setPointSize(8)
        p.setFont(f)
        for i in range(5):
            y = TOP + H - H * i / 4
            p.drawLine(L, int(y), L + W, int(y))
            p.setPen(QColor(T["muted"]))
            p.drawText(2, int(y) + 4, f"{ymax * i / 4:.0f} ms")
            p.setPen(QPen(QColor(T["border"])))
        for i in range(0, 5):
            p.setPen(QColor(T["muted"]))
            p.drawText(L + int(W * i / 4) - 10, self.height() - 5, f"{tmax * i / 4:.0f}s")
        for si, (label, ft, _srt) in enumerate(self._series):
            col = QColor(self.COLORS[si % 2])
            buckets = self._buckets.get((si, W))
            if buckets is None:                 # one pass over the samples per (data, width), not per repaint
                buckets = [None] * W
                t = 0.0
                for v in ft:
                    t += v
                    b = min(W - 1, int(t / 1000 / tmax * (W - 1)))
                    if buckets[b] is None or v > buckets[b]:
                        buckets[b] = v
                self._buckets[(si, W)] = buckets
            p.setPen(QPen(col, 1))
            poly = QPolygonF([QPointF(L + x, int(TOP + H - min(v, ymax) / ymax * H))
                              for x, v in enumerate(buckets) if v is not None])
            p.drawPolyline(poly)
            p.setPen(col)
            p.drawText(L + 8 + si * 200, TOP + 12, "■ " + label[:28])


class _FrametimeLoadThread(QThread):
    """Parsing + sorting + stats of a long log took ~1 s on the UI thread."""
    done = Signal(int, object, str)
    failed = Signal(str)

    def __init__(self, k: int, path: str):
        super().__init__()
        self._k, self._path = k, path

    def run(self):
        try:
            d = load_frametimes(self._path)
        except Exception as ex:
            self.failed.emit(f"⚠ {Path(self._path).name}: {ex}")
            return
        self.done.emit(self._k, d, self._path)


class FrametimeWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: List[Optional[dict]] = [None, None]
        self._loaders: List[QThread] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("Frametime analyser"))
        lay.addWidget(_desc_label(
            "Loads MangoHud logs (enable output_folder in the MangoHud tab, log with the toggle key) or "
            "vk_flip_meter FLM_CSV dumps. Load a second run to compare two configurations side by side."))
        self._edits: List[QLineEdit] = []
        for i, nm in enumerate(("A", "B")):
            row = QHBoxLayout()
            lab = QLabel(f"Run {nm}:")
            lab.setStyleSheet(f"color:{FrametimePlot.COLORS[i]}; font-weight:700;")
            row.addWidget(lab)
            e = QLineEdit()
            e.setReadOnly(True)
            e.setPlaceholderText("no file" + (" (optional)" if i else ""))
            e.setStyleSheet(_LINEEDIT_QSS)
            self._edits.append(e)
            row.addWidget(e, 1)
            b = _tool_btn("Open…")
            b.clicked.connect(lambda _c=False, k=i: self._open(k))
            row.addWidget(b)
            c = _tool_btn("Clear")
            c.clicked.connect(lambda _c=False, k=i: self._set(k, None, ""))
            row.addWidget(c)
            lay.addLayout(row)
        self._plot = FrametimePlot()
        lay.addWidget(self._plot)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Metric", "A", "B", "B vs A"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.setStyleSheet(_table_qss())
        lay.addWidget(self._table, 1)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)
        self._qs = QSettings(APP_ID, APP_ID)

    def _open(self, k: int):
        start = str(self._qs.value("frametime/last_dir", str(Path.home())))
        p, _ = QFileDialog.getOpenFileName(self, "Open frametime log", start,
                                           "Logs (*.csv *.log *.txt);;All files (*)")
        if not p:
            return
        self._qs.setValue("frametime/last_dir", str(Path(p).parent))
        self.load_file(k, p)

    def load_file(self, k: int, p: str):
        """Load a capture as run A (k=0) or B (k=1) — also used by the game card."""
        self._status.setText(f"Loading {Path(p).name}…")
        th = _FrametimeLoadThread(k, p)
        self._loaders.append(th)
        th.done.connect(self._set)
        th.failed.connect(self._status.setText)
        th.finished.connect(lambda t=th: self._loaders.remove(t))
        th.start()

    def _set(self, k: int, d: Optional[dict], path: str):
        self._data[k] = d
        self._edits[k].setText(path)
        self._render()

    def _render(self):
        ds = self._data
        self._plot.set_series([(d["name"], d["ft"], d["sorted"]) for d in ds if d])
        stats = [d["stats"] if d else None for d in ds]
        ref = next((s for s in stats if s), None)
        t = self._table
        t.setRowCount(0)
        if ref is None:
            return
        for r, (label, _v, hib, fmt) in enumerate(ref):
            t.insertRow(r)
            t.setItem(r, 0, QTableWidgetItem(label))
            vals = [s[r][1] if s else None for s in stats]
            for c in (0, 1):
                t.setItem(r, c + 1, QTableWidgetItem("" if vals[c] is None else fmt.format(vals[c])))
            if vals[0] is not None and vals[1] is not None and vals[0] != 0 and hib is not None:
                dpct = (vals[1] - vals[0]) / vals[0] * 100
                good = (dpct > 0) == hib
                it = QTableWidgetItem(f"{dpct:+.1f}%")
                it.setForeground(QColor(T["accent"] if abs(dpct) < 0.05 or good else T["danger"]))
                if abs(dpct) < 0.5:
                    it.setForeground(QColor(T["muted"]))
                t.setItem(r, 3, it)
        extra = [f"{d['name']}: " + ", ".join(f"{k}={v}" for k, v in d["extra"].items())
                 for d in ds if d and d["extra"]]
        self._status.setText("  ".join(extra))
