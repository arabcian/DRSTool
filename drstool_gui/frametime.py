"""Frametime analysis (MangoHud CSV / FLM dumps)."""

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QPointF, QSettings, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from drstool_core.benchmark import compare as compare_captures, compare_repeats

from .base import APP_ID
from .i18n import tr
from .theme import T
from .tool_ui import _tool_btn
from .widgets import _desc_label, _LINEEDIT_QSS, _table_qss, _title_label


# ----------------------------------------------------------------------------
# Frametime analyser (MangoHud CSV / FLM_CSV) with A/B comparison
# ----------------------------------------------------------------------------


from drstool_core.frametime_csv import load_frametimes, _parse_frametimes, frame_stats  # noqa: F401 (re-exported)


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

    def set_series(self, series: List[tuple], vram: Optional[List[Optional[list]]] = None):
        """series: [(label, ft, ft_sorted)]; vram: per series the per-frame VRAM samples (or None)"""
        self._series = series
        self._vram = vram or [None] * len(series)
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
            vr = self._vram[si] if si < len(self._vram) else None
            vmax = max((v for v in vr or () if v == v), default=0)
            if vmax > 0:
                pts, t = {}, 0.0
                for v, m in zip(ft, vr):
                    t += v
                    if m == m:
                        pts[min(W - 1, int(t / 1000 / tmax * (W - 1)))] = m
                p.setPen(QPen(col, 1, Qt.DotLine))
                p.drawPolyline(QPolygonF([QPointF(L + x, TOP + H - m / vmax * H) for x, m in sorted(pts.items())]))
                p.drawText(L + 8 + si * 200, TOP + 26, f"┄ VRAM (peak {vmax:.0f})")


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


class _RepeatThread(QThread):
    """Load 3+ captures per configuration and run benchmark.compare_repeats on them."""
    done = Signal(str)

    def __init__(self, a: List[str], b: List[str], same_scene: bool):
        super().__init__()
        self._a, self._b, self._same = a, b, same_scene

    def run(self):
        try:
            a, b = [load_frametimes(x) for x in self._a], [load_frametimes(x) for x in self._b]
            r = compare_repeats([x['ft'] for x in a], [x['ft'] for x in b],
                                same_scene_confirmed=self._same,
                                kinds_a=[x['kind'] for x in a], kinds_b=[x['kind'] for x in b])
        except (ValueError, OSError) as ex:
            self.done.emit(f"⚠ Repeated comparison: {ex}")
            return
        lo, hi = r["bootstrap_interval_pct"]
        self.done.emit("\n".join(
            [f"Repeated runs ({r['a_runs']} A / {r['b_runs']} B): median FPS {r['median_fps_a']:.1f} → "
             f"{r['median_fps_b']:.1f} ({r['median_change_pct']:+.1f}%), 95% bootstrap {lo:+.1f}% … {hi:+.1f}%",
             f"Run-to-run variation: A {r['run_cv_pct_a']:.1f}%, B {r['run_cv_pct_b']:.1f}%",
             f"Verdict: {r['verdict']}"] + ["⚠ " + w for w in r["warnings"]]))


class FrametimeWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: List[Optional[dict]] = [None, None]
        self._loaders: List[QThread] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label(tr("Frametime analyser")))
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
        rep = QHBoxLayout()
        rb = _tool_btn("Repeated runs (3+ A / 3+ B)…")
        rb.setToolTip("Pick at least three captures per configuration; compares the run medians "
                      "with run-to-run variation and a bootstrap interval")
        rb.clicked.connect(self._repeat)
        rep.addWidget(rb)
        self._same_scene = QCheckBox("Same scene and conditions")
        rep.addWidget(self._same_scene)
        rep.addStretch(1)
        lay.addLayout(rep)
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

    def _repeat(self):
        start = str(self._qs.value("frametime/last_dir", str(Path.home())))
        runs = []
        for nm in ("A", "B"):
            ps, _ = QFileDialog.getOpenFileNames(self, f"Captures of configuration {nm} (3 or more)", start,
                                                 "Logs (*.csv *.log *.txt);;All files (*)")
            if not ps:
                return
            start = str(Path(ps[0]).parent)
            runs.append(ps)
        self._qs.setValue("frametime/last_dir", start)
        self._status.setText(tr("Comparing repeated runs…"))
        th = _RepeatThread(runs[0], runs[1], self._same_scene.isChecked())
        self._loaders.append(th)
        th.done.connect(self._status.setText)
        th.finished.connect(lambda t=th: self._drop_loader(t))
        th.start()

    def _drop_loader(self, th: QThread):
        # `finished` is emitted from the worker just before it exits: wait for the
        # exit before dropping the last reference, or a still-running QThread is destroyed.
        th.wait()
        self._loaders.remove(th)

    def load_file(self, k: int, p: str):
        """Load a capture as run A (k=0) or B (k=1) — also used by the game card."""
        self._status.setText(f"Loading {Path(p).name}…")
        th = _FrametimeLoadThread(k, p)
        self._loaders.append(th)
        th.done.connect(self._set)
        th.failed.connect(self._status.setText)
        th.finished.connect(lambda t=th: self._drop_loader(t))
        th.start()

    def _set(self, k: int, d: Optional[dict], path: str):
        self._data[k] = d
        self._edits[k].setText(path)
        self._render()

    def _render(self):
        ds = self._data
        self._plot.set_series([(d["name"], d["ft"], d["sorted"]) for d in ds if d],
                              [(d.get("sensors") or {}).get("VRAM") for d in ds if d])
        stats = [d["stats"] if d else None for d in ds]
        ref = next((s for s in stats if s), None)
        t = self._table
        t.setRowCount(0)
        if ref is None:
            return
        for r, (label, _v, hib, fmt) in enumerate(ref):
            t.insertRow(r)
            sampled = any(d and d['kind'] == 'mangohud' for d in ds)
            display_label = label
            if sampled:
                display_label = {"Frames": "Samples / frames", "Duration (s)": "Summed intervals (s; not wall time)",
                                 "Average FPS": "Estimated average FPS"}.get(label, label)
            t.setItem(r, 0, QTableWidgetItem(tr(display_label)))
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
        if ds[0] and ds[1]:
            # Descriptive A/B only; explicitly refuse a comparable verdict
            # without repeated same-scene conditions supplied by the user.
            try:
                analysis = compare_captures(ds[0]['ft'], ds[1]['ft'],
                                            kind_a=ds[0]['kind'], kind_b=ds[1]['kind'])
                extra.append(f"B vs A: average FPS {analysis['fps_change_pct']:+.1f}%, "
                             f"1% low {analysis['low_change_pct']:+.1f}% "
                             "(descriptive, not a controlled benchmark)")
                extra.extend('⚠ ' + w for w in analysis['warnings'])
            except ValueError as exc:
                extra.append(f'⚠ Comparison unavailable: {exc}')
            # Averages from radically different scenes or durations are not
            # a defensible A/B result. Advise rather than hiding the data.
            durations = [sum(d['ft']) for d in ds]
            if max(durations) / max(min(durations), 0.001) > 1.2:
                extra.append('⚠ Capture durations differ by over 20%; '
                             'run the same repeatable scene for a fair comparison')
            if ds[0]['kind'] != ds[1]['kind']:
                extra.append('⚠ Different log types; verify both capture the same frame stream')
        self._status.setText("  ".join(extra))
