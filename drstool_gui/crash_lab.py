"""Crash Lab tab: crash triage (what happened) and the breaking-setting search (what causes it).

Triage hands its suspects and the time-to-crash to the search. The search keeps its session
(full configuration, answers, play time per test) in QSettings after every step, so DRSTool can
be closed while the game runs. On every answer it reads the kernel log for the test's time
window and questions a "works" that contradicts it (an Xid was logged, or the test ran shorter
than earlier crashes took).
"""

import json
import math
import time
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel, QListWidget, QMessageBox, QSpinBox,
    QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from drstool_core import kernel_events, xid_analysis
from drstool_core.bisect_search import CrashSearch, _mmss, early_works_warning, order_by_priority
from drstool_core.crash_triage import format_report, kernel_window, suggest_suspects, triage, xid_hint

from .base import APP_ID
from .i18n import tr
from .state import bisect_items, bisect_state, copy_state
from .tasks import TASKS
from .theme import btn_ss, T
from .tool_ui import ROLE_GLYPH, ROLE_SUB, ToolNavDelegate
from .widgets import make_list_item, setup_sidebar_list

_KEY = "crashlab/search"
_LIST_MAX = 12
TRIAGE, SEARCH = "Crash triage", "Find the breaking setting"


def _label(item) -> str:
    kind, key = item
    return {"settings": "DRS", "env": "env", "vkd3d": "VKD3D_CONFIG", "gs_on": "gamescope"}[kind] + (f" {key}" if key else "")


def parse_suspect(text: str):
    """"env:NAME" / "vkd3d:flag" / "settings:0xID" -> search item tuple."""
    kind, _, key = text.partition(":")
    return (kind, key)


def _read(paths) -> list:
    lines = []
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines += f.read().splitlines()
    return lines


def analyse(paths, clean_paths=(), use_journal=True, collect=kernel_events.collect_any):
    """-> (result, clean result or None, note about the kernel log)."""
    lines = _read(paths)
    result = triage(lines)
    note = "Kernel log: not read."
    window = kernel_window(result) if use_journal else None
    if use_journal and not window:
        note = "Kernel log: the game log has no timestamps to place the run in time."
    elif window:
        k = collect(*window)
        events = k.get("events", [])
        note = f"Kernel log: {k.get('status')} — {len(events)} relevant event(s). {k.get('reason', '')}".strip()
        if events:
            result = triage(lines + [e["message"] for e in events])
    clean = triage(_read(clean_paths)) if clean_paths else None
    return result, clean, note


def _mono(w):
    w.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
    return w


def _text(word_wrap=True) -> QLabel:
    lb = QLabel()
    lb.setWordWrap(word_wrap)
    lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


class _TriageWorker(QThread):
    """analyse() off the GUI thread: a multi-MB log plus the journal / dmesg read takes seconds."""
    done = Signal(int, object, object, str)      # request number, result, clean result, kernel-log note
    failed = Signal(int, str)

    def __init__(self, seq, paths, clean, use_journal, collect, parent=None):
        super().__init__(parent)
        self.seq, self._args = seq, (paths, clean, use_journal, collect)

    def run(self):
        try:
            self.done.emit(self.seq, *analyse(*self._args))
        except OSError as ex:
            self.failed.emit(self.seq, str(ex))


# ─────────────────────────────── triage ───────────────────────────────
class CrashTriageWidget(QWidget):
    send_to_search = Signal(list, object, object)  # suspects, seconds until the crash, Xid hint (or None)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._paths: list = []
        self._clean: list = []
        self._result = self._clean_result = None
        self._seq = 0                            # a slow earlier load must not overwrite a newer one
        self._worker = None
        self.collect = kernel_events.collect_any
        lay = QVBoxLayout(self)
        self._files = _text()
        self._files.setText("1. Add the game / Proton log of a crashed run (a saved “sudo dmesg > dmesg.txt” "
                            "can be added too).\n2. Optional: add the log of a run that did not crash, to see "
                            "what differed.\n3. Send the suspects to the search.")
        lay.addWidget(self._files)
        self._journal = QCheckBox("Read the kernel log around the run's time (journal, then dmesg)")
        self._journal.setChecked(True)
        lay.addWidget(self._journal)
        self._view = _mono(QPlainTextEdit())
        self._view.setReadOnly(True)
        lay.addWidget(self._view, 1)
        row = QHBoxLayout()
        add = QPushButton("Crashed run log(s)…")
        add.setStyleSheet(btn_ss("primary"))
        clean = QPushButton("Clean run log…")
        self._export = QPushButton("Export Markdown…")
        self._send = QPushButton("Search, suspects first →")
        add.clicked.connect(self._pick)
        clean.clicked.connect(self._pick_clean)
        self._export.clicked.connect(self._save)
        self._send.clicked.connect(self._send_suspects)
        for b in (add, clean, self._export):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self._send)
        lay.addLayout(row)
        self._export.setEnabled(False)
        self._send.setEnabled(False)

    def _pick(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Crashed run: game log and kernel log"), str(Path.home()))
        if paths:
            self.load(paths, self._clean)

    def _pick_clean(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Clean run log (same game, no crash)"), str(Path.home()))
        if paths:
            self._clean = paths
            if self._paths:
                self.load(self._paths, paths)

    def load(self, paths, clean=()):
        self._paths, self._clean = list(paths), list(clean)
        self._seq += 1
        self._export.setEnabled(False)
        self._send.setEnabled(False)
        self._view.setPlainText(tr("Analysing the logs…"))
        worker = self._worker = _TriageWorker(self._seq, self._paths, self._clean, self._journal.isChecked(),
                                              self.collect)       # no parent: TASKS keeps it alive until done
        worker.done.connect(self._loaded)
        worker.failed.connect(self._failed)
        TASKS.track(worker, "Crash triage", fail=worker.failed)
        worker.start()

    def _loaded(self, seq, result, clean_result, note):
        if seq != self._seq:
            return
        self._result, self._clean_result = result, clean_result
        self._files.setText(tr("Crashed:") + " " + ", ".join(Path(p).name for p in self._paths)
                            + ("\n" + tr("Clean:") + " " + ", ".join(Path(p).name for p in self._clean)
                               if self._clean else ""))
        self._view.setPlainText(format_report(result, clean=clean_result) + "\n\n" + note)
        self._export.setEnabled(True)
        self._send.setEnabled(True)

    def _failed(self, seq, error):
        if seq == self._seq:
            self._view.setPlainText(tr("Could not read the logs: {error}").format(error=error))

    def suspects(self) -> list:
        return suggest_suspects(self._result, self._clean_result) if self._result else []

    def crash_seconds(self):
        if not (self._result and self._result["crashed"]):
            return None
        d = self._result["fingerprint"].get("duration_s")
        return int(d) if d else None

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, tr("Export crash report"), str(Path.home() / "crash-report.md"),
                                              "Markdown (*.md)")
        if path:
            Path(path).write_text(format_report(self._result, clean=self._clean_result, markdown=True) + "\n",
                                  encoding="utf-8")

    def _send_suspects(self):
        self.send_to_search.emit(self.suspects(), self.crash_seconds(),
                                 xid_hint(self._result) if self._result else None)


# ─────────────────────────────── search ───────────────────────────────
class CrashSearchWidget(QWidget):
    """idle → running ⇄ paused → done. The editor holds the test configuration; the user writes it
    to the game (Games tab / launcher tab), plays, and answers here."""
    status_changed = Signal(str)

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self._win = win
        self.now = time.time
        self.collect = kernel_events.collect_any
        self.confirm = self._ask
        self._search = None
        self._full = None
        self._priority: list = []
        self._crash_hint = None
        self._xid_hint = None
        self._elapsed: list = []
        self._xids: list = []                    # kernel Xids logged during each answered test
        self._applied_at = None
        lay = QVBoxLayout(self)
        self._intro = _text()
        lay.addWidget(self._intro)
        self._keep_drs = QCheckBox("Keep DRS settings on (test only env vars, VKD3D_CONFIG flags and gamescope)")
        self._keep_drs.setChecked(True)
        lay.addWidget(self._keep_drs)
        self._repeat = QCheckBox("Repeat each deterministic test twice (intermittent crashes)")
        lay.addWidget(self._repeat)
        self._adaptive = QCheckBox("Adaptive search: weighs play time and the Xid, tolerates intermittent "
                                   "crashes and a wrong answer")
        lay.addWidget(self._adaptive)
        self._bayes_options = QWidget()
        opts = QHBoxLayout(self._bayes_options)
        opts.setContentsMargins(0, 0, 0, 0)
        opts.addWidget(QLabel(tr("Posterior threshold")))
        self._confidence = QSpinBox()
        self._confidence.setRange(70, 99)
        self._confidence.setValue(95)
        self._confidence.setSuffix(" %")
        opts.addWidget(self._confidence)
        opts.addWidget(QLabel(tr("Maximum tests")))
        self._max_tests = QSpinBox()
        self._max_tests.setRange(5, 200)
        self._max_tests.setValue(40)
        opts.addWidget(self._max_tests)
        opts.addStretch(1)
        lay.addWidget(self._bayes_options)
        self._adaptive.toggled.connect(self._bayes_options.setVisible)
        self._actual_duration = QDoubleSpinBox()
        self._actual_duration.setRange(0, 864000)
        self._actual_duration.setDecimals(1)
        self._actual_duration.setSuffix(" s")
        self._actual_duration.setSpecialValueText(tr("Actual play time (enter seconds; 0 = estimate)"))
        self._actual_duration.setToolTip(tr("Enter active gameplay seconds, not time spent in the editor or away. For a crash, enter time until the crash."))
        lay.addWidget(self._actual_duration)
        self._legacy_reset = False
        self._text = _text()
        lay.addWidget(self._text)
        self._evidence = _text()
        self._evidence.setStyleSheet(f"color:{T['warn']};")
        lay.addWidget(self._evidence)
        lay.addStretch(1)
        row = QHBoxLayout()
        self._start = QPushButton("Start with the editor's configuration")
        self._start.setStyleSheet(btn_ss("primary"))
        self._resume = QPushButton("Continue the saved search")
        self._resume.setStyleSheet(btn_ss("primary"))
        self._ok = QPushButton("✓ Works")
        self._bad = QPushButton("✗ Still crashes")
        self._bad.setStyleSheet(btn_ss("danger"))
        self._retest = QPushButton("Inconclusive / Retest")
        self._retest.clicked.connect(lambda: self._step())
        self._undo = QPushButton("Undo")
        self._kernel = QPushButton("Check kernel log")
        self._restart = QPushButton("Start over")
        self._finish_btn = QPushButton("Keep result")
        self._stop = QPushButton("Stop and restore")
        self._start.clicked.connect(self.start)
        self._resume.clicked.connect(self.resume)
        self._ok.clicked.connect(lambda: self.answer(False))
        self._bad.clicked.connect(lambda: self.answer(True))
        self._undo.clicked.connect(self.undo)
        self._kernel.clicked.connect(self._show_kernel)
        self._restart.clicked.connect(self._start_over)
        self._finish_btn.clicked.connect(self.keep_result)
        self._stop.clicked.connect(self.restore)
        self._buttons = (self._start, self._resume, self._ok, self._bad, self._retest, self._undo, self._kernel,
                         self._restart, self._finish_btn, self._stop)
        for b in self._buttons:
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        saved = self._load()
        if saved:
            self._from_saved(saved)
            self._show("paused")
        else:
            self._show("idle")

    # ── session storage ──
    @staticmethod
    def _load() -> dict:
        try:
            s = json.loads(str(QSettings(APP_ID, APP_ID).value(_KEY, "") or "{}"))
            return s if isinstance(s, dict) and isinstance(s.get("full"), dict) else {}
        except ValueError:
            return {}

    def _save(self):
        QSettings(APP_ID, APP_ID).setValue(_KEY, json.dumps({
            "search_version": 4, "confidence": self._confidence.value(), "max_tests": self._max_tests.value(),
            "tested": self._tested() if isinstance(self._search, xid_analysis.AdaptiveSearch) else None,
            "pending": [xid_analysis.item_key(i) for i in self._search.pending] if isinstance(self._search, xid_analysis.AdaptiveSearch) and self._search.pending is not None else None,
            "planned_seconds": self._search.play_time() if isinstance(self._search, xid_analysis.AdaptiveSearch) else None, "repeats": 2 if self._repeat.isChecked() else 1, "full": self._full, "keep_drs": self._keep_drs.isChecked(), "adaptive": self._adaptive.isChecked(),
            "priority": [list(p) for p in self._priority], "answers": self._search.answers,
            "elapsed": self._elapsed, "xids": self._xids, "crash_hint": self._crash_hint,
            "xid_hint": self._xid_hint, "applied_at": self._applied_at}))

    def _clear(self):
        QSettings(APP_ID, APP_ID).remove(_KEY)

    def _from_saved(self, s: dict):
        self._full = copy_state(s["full"])
        self._keep_drs.setChecked(bool(s.get("keep_drs", True)))
        self._adaptive.setChecked(bool(s.get("adaptive", False)))
        self._repeat.setChecked(s.get("repeats", 1) == 2)
        self._priority = [tuple(p) for p in s.get("priority", [])]
        try:
            hint = float(s["crash_hint"]) if s.get("crash_hint") is not None else None
            self._crash_hint = hint if hint is not None and math.isfinite(hint) and hint > 0 else None
        except (TypeError, ValueError):
            self._crash_hint = None
        self._xid_hint = s.get("xid_hint") if isinstance(s.get("xid_hint"), dict) else None
        self._elapsed = list(s.get("elapsed", []))
        self._xids = [list(x) for x in s.get("xids", [])]
        self._applied_at = s.get("applied_at")
        try:
            self._confidence.setValue(int(s.get("confidence", 95)))
            self._max_tests.setValue(int(s.get("max_tests", 40)))
        except (TypeError, ValueError, OverflowError):
            self._confidence.setValue(95)
            self._max_tests.setValue(40)
        version = s.get("search_version")
        adaptive = self._adaptive.isChecked()
        try:
            if adaptive and version == 4 and isinstance(s.get("tested"), list):
                self._build(s.get("answers", []), tested=s["tested"], pending=s.get("pending"),
                            planned_seconds=s.get("planned_seconds"))
            elif not adaptive and version in (2, 3, 4):
                self._build(s.get("answers", []))
            else:
                self._legacy_reset = True
                self._elapsed, self._xids, self._applied_at = [], [], None
                self._build([])
        except (ValueError, TypeError, RuntimeError, KeyError):
            self._legacy_reset = True
            self._elapsed, self._xids, self._applied_at = [], [], None
            self._build([])

    def _build(self, answers, *, tested=None, pending=None, planned_seconds=None):
        items = bisect_items(self._full)
        self._fixed = [i for i in items if i[0] == "settings"] if self._keep_drs.isChecked() else []
        rest = [i for i in items if i not in self._fixed]
        if self._adaptive.isChecked():
            self._search = xid_analysis.AdaptiveSearch(rest, answers, self._elapsed, prior=self._prior(rest),
                                                       crash_seconds=self._crash_hint, tested=tested, pending=pending,
                                                       planned_seconds=planned_seconds,
                                                       threshold=self._confidence.value()/100, max_tests=self._max_tests.value(),
                                                       known_baseline=False)
        else:
            self._search = CrashSearch(order_by_priority(rest, self._priority), answers, repeats=2 if self._repeat.isChecked() else 1)
        self._elapsed = self._elapsed[:len(self._search.answers)]
        self._xids = self._xids[:len(self._search.answers)]

    def _prior(self, items) -> dict:
        """Log prior weights for the adaptive search: the Xid knowledge base for the crash's root
        Xid (with the GPU arch and driver), plus a boost for the suspects triage sent."""
        prior = {}
        h = self._xid_hint
        if h and h.get("xid") is not None:
            prior = {k: w for k, (w, _) in xid_analysis.prior_weights(
                int(h["xid"]), h.get("subtypes", ()), items, arch=self._full.get("arch"),
                driver=h.get("driver")).items()}
        keys = [xid_analysis.item_key(p) for p in self._priority]
        for rank, k in enumerate(keys):
            if not prior.get(k):                 # triage also lists knowledge-base links: count them once
                prior[k] = max(1.0 - 0.15 * rank, 0.3)
        return prior

    # ── public API (also used by the triage hand-over) ──
    def set_hints(self, suspects, crash_seconds=None, xid=None):
        """Suspects to test first, how long the crashed run lasted and the crash's Xid
        ({"xid", "subtypes", "driver"}, weighs settings in the adaptive search). Applies to the next start."""
        if self._search is not None:
            # Do not mutate priors of an in-progress search (which would change its replay).
            self._evidence.setText(tr("A search is already in progress, so the triage suspects were not applied. "
                                      "Use “Stop and restore”, then send them again."))
            return
        self._priority = [parse_suspect(s) if isinstance(s, str) else tuple(s) for s in suspects]
        self._crash_hint = crash_seconds if crash_seconds and math.isfinite(crash_seconds) and crash_seconds > 0 else None
        self._xid_hint = dict(xid) if xid else None
        if self._search is None:
            self._show("idle")

    @property
    def running(self) -> bool:
        return self._search is not None

    def start(self):
        self._full = copy_state(self._win.capture_state())
        self._elapsed, self._xids, self._applied_at = [], [], None
        self._build([])
        self._step()

    def resume(self):
        self._step(keep_clock=True)

    def answer(self, fails: bool):
        s = self._search
        elapsed = self.now() - self._applied_at if self._applied_at else 0.0
        if isinstance(s, xid_analysis.AdaptiveSearch):
            measured = self._actual_duration.value()
            if measured > 0:
                elapsed = measured
            elif elapsed <= 0 or not math.isfinite(elapsed):
                self._evidence.setText(tr("Enter actual play time in seconds before recording the test."))
                return
            elif not self.confirm(tr("Confirm active gameplay time"), tr("The elapsed wall time is {time}. Count it only if you were actually playing for that duration, or enter the real duration.").format(time=_mmss(elapsed))):
                return
        xids = self._kernel_xids()
        if not fails:
            doubt = [early_works_warning(elapsed, [self._crash_hint],
                                         [e for e, a in zip(self._elapsed, s.answers) if a])]
            if xids:
                doubt.append(tr("The kernel logged Xid {xids} during this test.").format(
                    xids=", ".join(map(str, xids))))
            doubt = [d for d in doubt if d]
            if doubt and not self.confirm(tr("Count this test as working?"), "\n\n".join(doubt)):
                return
        if isinstance(s, xid_analysis.AdaptiveSearch):
            s.answer(fails, elapsed)
        else:
            s.answer(fails)
        self._elapsed.append(elapsed if isinstance(s, xid_analysis.AdaptiveSearch) else round(elapsed, 1))
        self._xids.append(xids)
        self._step()

    def undo(self):
        self._search.undo()
        self._elapsed = self._elapsed[:len(self._search.answers)]
        self._xids = self._xids[:len(self._search.answers)]
        self._step()

    def keep_result(self):
        """Leave the editor as it is (every setting but the culprits) and forget the session."""
        self._clear()
        self._search = None
        self._show("idle")

    def restore(self):
        self._clear()
        if self._full is not None:
            self._win.apply_state(self._full, "Crash search: restore")
        self._search = None
        self._show("idle")

    def _start_over(self):
        self._elapsed, self._xids, self._applied_at = [], [], None
        self._build([])
        self._step()

    # ── evidence ──
    def _kernel_xids(self) -> list:
        if not self._applied_at:
            return []
        k = self.collect(self._applied_at, self.now() + 5)
        return list(dict.fromkeys(e["xid"] for e in k.get("events", []) if e.get("xid") is not None))

    def _show_kernel(self):
        if not self._applied_at:
            self._evidence.setText(tr("No test is running."))
            return
        k = self.collect(self._applied_at, self.now() + 5)
        ev = k.get("events", [])
        if k.get("status") != "ok":
            self._evidence.setText(tr("Kernel log unavailable: {reason}").format(reason=k.get("reason", "")))
        elif not ev:
            self._evidence.setText(tr("No GPU errors in the kernel log since this test started."))
        else:
            self._evidence.setText(tr("Since this test started:") + "\n" + "\n".join(e["message"][:200] for e in ev[-5:]))

    @staticmethod
    def _ask(title, text) -> bool:
        return QMessageBox.question(None, title, text) == QMessageBox.Yes

    # ── display ──
    def _show(self, mode: str):
        visible = {"idle": {self._start},
                   "paused": {self._resume, self._restart, self._stop},
                   "running": {self._ok, self._bad, self._retest, self._undo, self._kernel, self._restart, self._stop},
                   "done": {self._undo, self._finish_btn, self._stop},
                   "empty": {self._stop}}[mode]
        for b in self._buttons:
            b.setVisible(b in visible)
        self._undo.setEnabled(bool(self._search and self._search.tests_done))
        self._keep_drs.setVisible(mode == "idle")
        self._adaptive.setVisible(mode == "idle")
        self._repeat.setVisible(mode == "idle")
        self._bayes_options.setVisible(mode == "idle" and self._adaptive.isChecked())
        self._actual_duration.setVisible(mode == "running" and isinstance(self._search, xid_analysis.AdaptiveSearch))
        self._evidence.clear()
        if mode == "idle":
            pri = ", ".join(_label(p) for p in self._priority)
            self._intro.setText(
                tr("Load the profile that crashes into the editor, then start. Each step puts a test "
                   "configuration into the editor: write it to the game, play until the crash would have "
                   "happened, and answer. The search also finds settings that only crash together and "
                   "keeps going if there is more than one cause.")
                + (tr("\n\nTested first (from triage): {suspects}").format(suspects=pri) if pri else "")
                + (tr("\nThe crashed run lasted {seconds} s; shorter “works” answers are questioned.").format(
                    seconds=self._crash_hint) if self._crash_hint else ""))
            self._text.clear()
            self.status_changed.emit(tr("Not running"))
        elif mode == "paused":
            self._intro.setText(tr("A saved search was found."))
            self._text.setText(tr("{n} test(s) answered. “Continue” puts the pending "
                                  "test configuration back into the editor.").format(n=self._search.tests_done))
            if self._legacy_reset:
                self._text.setText(self._text.text() + "\n" + tr("The saved search uses an older or invalid evidence format. Its original profile is preserved; the search will restart without reassigning old answers to new configurations."))
            self.status_changed.emit(tr("Paused at test {n}").format(n=self._search.tests_done + 1))

    def _step(self, keep_clock=False):
        s = self._search
        self._actual_duration.setValue(0)
        if not s.items:
            self._intro.setText("")
            self._text.setText(tr("No settings to test: the editor has nothing besides what is kept on."))
            self._show("empty")
            return
        if not keep_clock or not self._applied_at:
            self._applied_at = None if s.done else self.now()
        self._save()
        if s.done:
            self._finish()
            return
        self._show("running")
        on = self._fixed + s.pending
        self._win.apply_state(bisect_state(self._full, on), f"Crash search test {s.tests_done + 1}")
        n = s.tests_done + 1
        if s.phase == "fixed":
            head = tr("Test {n} — baseline: only the settings that are always kept on").format(n=n) + (
                " (DRS)" if self._fixed else "") + "."
        elif s.phase == "baseline":
            head = tr("Test {n} — confirm that the full configuration still crashes.").format(n=n)
        elif s.phase == "verify":
            found = ", ".join(_label(i) for g in s.found_so_far for i in g)
            if isinstance(s, xid_analysis.AdaptiveSearch):
                head = tr("Test {n} — verification: everything except {found} is on. A crash updates the model; a short successful run is weak evidence.").format(n=n, found=found)
            else:
                head = tr("Test {n} — check: everything except {found} is on. "
                          "If it still crashes there is another, independent cause.").format(n=n, found=found)
        elif len(s.pending) <= _LIST_MAX:
            head = tr("Test {n}: on —").format(n=n) + " " + ", ".join(_label(i) for i in s.pending)
        else:
            head = tr("Test {n}: {on} of {total} tested settings on.").format(
                n=n, on=len(s.pending), total=len(s.items))
        cands = [] if s.phase == "verify" else s.candidates
        still = (", ".join(_label(i) for i in cands) if len(cands) <= _LIST_MAX
                 else tr("{n} settings").format(n=len(cands))) or "—"
        self._intro.setText("")
        kept = tr("\nAlways on: {n} DRS setting(s) and the GPU arch.").format(n=len(self._fixed)) if self._fixed else ""
        self._text.setText(
            head + tr("\n\nStill suspected: {still}\nAbout {left} more test(s) if one setting "
                      "is the cause.").format(still=still, left=s.estimate_left())
            + kept + self._adaptive_note() + tr("\n\nWrite the configuration to the game, play, then answer."))
        if isinstance(s, CrashSearch):
            lo, hi = s.estimate_range()
            self._text.setText(self._text.text() + tr("\nRemaining tests: {lo}–{hi} (conservative theoretical bound; typical searches are much shorter).").format(lo=lo, hi=hi))
        self.status_changed.emit(tr("Test {n} · ≈{left} left").format(n=n, left=s.estimate_left()))

    def _adaptive_note(self) -> str:
        """Adaptive search only: leading hypotheses and how long to play the pending test."""
        s = self._search
        if not isinstance(s, xid_analysis.AdaptiveSearch):
            return ""
        lead = "\n".join(f"  {p:5.0%}  {self._describe(h)}" for h, p in s.post[:3])
        out = "\n\n" + tr("Most likely cause:") + "\n" + lead
        out += "\n" + tr("Probabilities are conditional on the bounded hypothesis model; untested combinations may be missing.")
        out += "\n" + tr("Hypotheses: {n} · Confidence target: {target}% · Budget: {left} tests").format(n=len(s.model.hyps), target=round(s.threshold*100), left=max(0, s.max_tests-s.tests_done))
        if not s.done:
            out += "\n" + tr("Play at least {time} for the planned observation (shorter runs still count as weaker evidence).").format(time=_mmss(s.play_time()))
            out += "\n" + tr("Predicted crash probability: {risk:.0%} · Expected information: {bits:.3f} bits").format(risk=s.predicted_risk(), bits=s.expected_gain)
            if s.phase == "verify":
                out += "\n" + tr("Verification exposure: {p:.0%} conditional detection probability (target 80%).").format(p=s.verification_probability)
        return out

    @staticmethod
    def _describe(h) -> str:
        names = [_label(parse_suspect(k)) for k in h[1:]]
        if h[0] == "and":
            return " + ".join(names) + tr(" (only together)")
        if h[0] == "mixed":
            return "(" + " + ".join(names[:2]) + ") " + tr("or") + " " + names[2]
        if h[0] == "mixed2":
            return "(" + " + ".join(names[:2]) + ") " + tr("or") + " (" + " + ".join(names[2:]) + ")"
        if h[0] == "or":
            return (" " + tr("or") + " ").join(names) + tr(" (independent causes)")
        return names[0] if h[0] == "one" else tr("none of the tested settings (the cause is elsewhere)")

    def _xid_links(self) -> str:
        """Which setting each Xid seen during the tests is tied to (needs a crash with an Xid)."""
        s = self._search
        if not any(self._xids) or not s.items:
            return ""
        runs = [{"on": [xid_analysis.item_key(i) for i in on], "seconds": t,
                 "crashed": a, "xids": x}
                for on, t, a, x in zip(self._tested(), self._elapsed, s.answers, self._xids) if t > 0]
        links = xid_analysis.associate([xid_analysis.item_key(i) for i in s.items], runs,
                                       crash_seconds=self._crash_hint)
        rows = []
        for sym, ranked in sorted(links.items()):
            k, p = ranked[0]
            name = self._describe(("none",)) if k == "none" else _label(parse_suspect(k))
            title = xid_analysis.info(int(sym))["title"] if sym.isdigit() else tr("secondary Xids only (root unknown)") if sym == "secondary-only" else tr("no Xid")
            rows.append(f"  Xid {sym} ({title}): {name} {p:.0%}" if sym.isdigit() else f"  {title}: {name} {p:.0%}")
        return "\n\n" + tr("Xid ↔ setting links measured in these tests:") + "\n" + "\n".join(rows) if rows else ""

    def _tested(self) -> list:
        """The tested subset of every answered test, replayed from the answers."""
        s = self._search
        if isinstance(s, xid_analysis.AdaptiveSearch):
            return [sorted(on) for on, _t, _c in s.observations]
        replay, out = CrashSearch(s.items, repeats=s.repeats), []
        for a in s.answers:
            out.append([xid_analysis.item_key(i) for i in replay.pending])
            replay.answer(a)
        return out

    def _finish(self):
        s = self._search
        self._show("done")
        kind, groups = s.outcome
        if kind in ("no-candidates", "not-reproduced", "inconclusive"):
            self._win.apply_state(self._full, "Crash search: restore")
            self._intro.setText("")
            self._text.setText(tr("No verified culprit. The original configuration was restored. Retest or use deterministic bisect for larger interactions."))
            self.status_changed.emit(tr("Inconclusive"))
            return
        if kind == "fixed":
            self._win.apply_state(self._full, "Crash search: restore")
            self._intro.setText("")
            self._text.setText(tr("It crashes even with only the kept settings on, so none of the tested settings "
                                  "is needed for the crash. Untick “Keep DRS settings on” and start again to test "
                                  "the DRS settings too — or the cause is outside DRSTool (driver, game, runner)."))
            self.status_changed.emit(tr("Kept settings alone crash"))
            return
        culprits = s.culprits
        others = [i for i in bisect_items(self._full) if i not in culprits]
        self._win.apply_state(bisect_state(self._full, others), "Without " + ", ".join(map(_label, culprits)))
        lines = []
        for k, g in enumerate(groups, 1):
            names = " + ".join(_label(i) for i in g)
            lines.append(f"{k}. {names}" + (tr("   (only together: switching off one of them is enough)")
                                             if len(g) > 1 else ""))
        more = (tr("\n\nThere are independent cause groups: each group can cause a crash. Disable at least one setting from each group.")
                if len(groups) > 1 else "")
        self._intro.setText("")
        self._text.setText(tr("Found after {n} test(s):").format(n=s.tests_done) + "\n" + "\n".join(lines) + more
                           + self._adaptive_note()
                           + tr("\n\nThe editor now has every other setting on — verified to work in the last "
                                "test. “Keep result” leaves it so; “Stop and restore” brings the full "
                                "configuration back.") + self._xid_links())
        self.status_changed.emit(tr("Found: {names}").format(names=", ".join(_label(i) for i in culprits)))


# ─────────────────────────────── tab ───────────────────────────────
class CrashLabWidget(QTabWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        from .extra_tools import ExtraToolsWidget
        self.setStyleSheet(ExtraToolsWidget._TAB_SS)
        self.triage = CrashTriageWidget()
        self.search = CrashSearchWidget(win)
        self.addTab(self.triage, TRIAGE)
        self.addTab(self.search, SEARCH)
        self.triage.send_to_search.connect(self._hand_over)

    def _hand_over(self, suspects, seconds, xid=None):
        self.search.set_hints(suspects, seconds, xid)
        self.show_tab(SEARCH)

    def show_tab(self, title: str) -> bool:
        for i in range(self.count()):
            if self.tabText(i) == title:
                self.setCurrentIndex(i)
                return True
        return False


class CrashLabSidebar(QListWidget):
    NAV = ((TRIAGE, "⚕", "What happened: signatures, Xid, run diff"),
           (SEARCH, "⌕", "What causes it: guided test runs"))

    def __init__(self, parent=None):
        super().__init__(parent)
        setup_sidebar_list(self)
        self.setItemDelegate(ToolNavDelegate(self))
        self._lab = None
        self._items = {}
        for title, glyph, sub in self.NAV:
            it = make_list_item(tr(title), title)
            it.setData(ROLE_GLYPH, glyph)
            it.setData(ROLE_SUB, tr(sub))
            it.setToolTip(tr(sub))
            self.addItem(it)
            self._items[title] = it
        self.currentItemChanged.connect(lambda cur, _p=None: cur and self._lab and self._lab.show_tab(cur.data(Qt.UserRole)))

    def bind(self, lab: CrashLabWidget):
        self._lab = lab
        lab.currentChanged.connect(lambda i: self._select(lab.tabText(i)))
        lab.search.status_changed.connect(lambda t: self._items[SEARCH].setData(ROLE_SUB, t))
        self._items[SEARCH].setData(ROLE_SUB, tr("Paused — continue it here") if lab.search.running
                                    else tr(self.NAV[1][2]))
        self._select(lab.tabText(lab.currentIndex()))

    def _select(self, title):
        it = self._items.get(title)
        if it is not None and it is not self.currentItem():
            self.blockSignals(True)
            self.setCurrentItem(it)
            self.blockSignals(False)
