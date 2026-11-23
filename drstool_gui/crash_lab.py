"""Crash Lab tab: crash triage (what happened) and the breaking-setting search (what causes it).

Triage hands its suspects and the time-to-crash to the search. The search keeps its session
(full configuration, answers, play time per test) in QSettings after every step, so DRSTool can
be closed while the game runs. On every answer it reads the kernel log for the test's time
window and questions a "works" that contradicts it (an Xid was logged, or the test ran shorter
than earlier crashes took).
"""

import json
import time
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QListWidget, QMessageBox,
    QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from drstool_core import kernel_events
from drstool_core.bisect_search import CrashSearch, early_works_warning, order_by_priority
from drstool_core.crash_triage import format_report, kernel_window, suggest_suspects, triage

from .base import APP_ID
from .state import bisect_items, bisect_state, copy_state
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


# ─────────────────────────────── triage ───────────────────────────────
class CrashTriageWidget(QWidget):
    send_to_search = Signal(list, object)        # suspects, seconds until the crash (or None)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._paths: list = []
        self._clean: list = []
        self._result = self._clean_result = None
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
        paths, _ = QFileDialog.getOpenFileNames(self, "Crashed run: game log and kernel log", str(Path.home()))
        if paths:
            self.load(paths, self._clean)

    def _pick_clean(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Clean run log (same game, no crash)", str(Path.home()))
        if paths:
            self._clean = paths
            if self._paths:
                self.load(self._paths, paths)

    def load(self, paths, clean=()):
        self._paths, self._clean = list(paths), list(clean)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._result, self._clean_result, note = analyse(self._paths, self._clean,
                                                            self._journal.isChecked(), self.collect)
        finally:
            QApplication.restoreOverrideCursor()
        self._files.setText("Crashed: " + ", ".join(Path(p).name for p in self._paths)
                            + ("\nClean: " + ", ".join(Path(p).name for p in self._clean) if self._clean else ""))
        self._view.setPlainText(format_report(self._result, clean=self._clean_result) + "\n\n" + note)
        self._export.setEnabled(True)
        self._send.setEnabled(True)

    def suspects(self) -> list:
        return suggest_suspects(self._result, self._clean_result) if self._result else []

    def crash_seconds(self):
        if not (self._result and self._result["crashed"]):
            return None
        d = self._result["fingerprint"].get("duration_s")
        return int(d) if d else None

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export crash report", str(Path.home() / "crash-report.md"),
                                              "Markdown (*.md)")
        if path:
            Path(path).write_text(format_report(self._result, clean=self._clean_result, markdown=True) + "\n",
                                  encoding="utf-8")

    def _send_suspects(self):
        self.send_to_search.emit(self.suspects(), self.crash_seconds())


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
        self._elapsed: list = []
        self._applied_at = None
        lay = QVBoxLayout(self)
        self._intro = _text()
        lay.addWidget(self._intro)
        self._keep_drs = QCheckBox("Keep DRS settings on (test only env vars, VKD3D_CONFIG flags and gamescope)")
        self._keep_drs.setChecked(True)
        lay.addWidget(self._keep_drs)
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
        self._buttons = (self._start, self._resume, self._ok, self._bad, self._undo, self._kernel,
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
            "full": self._full, "keep_drs": self._keep_drs.isChecked(),
            "priority": [list(p) for p in self._priority], "answers": self._search.answers,
            "elapsed": self._elapsed, "crash_hint": self._crash_hint, "applied_at": self._applied_at}))

    def _clear(self):
        QSettings(APP_ID, APP_ID).remove(_KEY)

    def _from_saved(self, s: dict):
        self._full = copy_state(s["full"])
        self._keep_drs.setChecked(bool(s.get("keep_drs", True)))
        self._priority = [tuple(p) for p in s.get("priority", [])]
        self._crash_hint = s.get("crash_hint")
        self._elapsed = list(s.get("elapsed", []))
        self._applied_at = s.get("applied_at")
        self._build(s.get("answers", []))

    def _build(self, answers):
        items = bisect_items(self._full)
        self._fixed = [i for i in items if i[0] == "settings"] if self._keep_drs.isChecked() else []
        rest = [i for i in items if i not in self._fixed]
        self._search = CrashSearch(order_by_priority(rest, self._priority), answers)
        self._elapsed = self._elapsed[:len(self._search.answers)]

    # ── public API (also used by the triage hand-over) ──
    def set_hints(self, suspects, crash_seconds=None):
        """Suspects to test first and how long the crashed run lasted. Applies to the next start."""
        self._priority = [parse_suspect(s) if isinstance(s, str) else tuple(s) for s in suspects]
        if crash_seconds:
            self._crash_hint = crash_seconds
        if self._search is None:
            self._show("idle")

    @property
    def running(self) -> bool:
        return self._search is not None

    def start(self):
        self._full = copy_state(self._win.capture_state())
        self._elapsed, self._applied_at = [], None
        self._build([])
        self._step()

    def resume(self):
        self._step(keep_clock=True)

    def answer(self, fails: bool):
        s = self._search
        elapsed = self.now() - self._applied_at if self._applied_at else 0.0
        if not fails:
            doubt = [early_works_warning(elapsed, [self._crash_hint],
                                         [e for e, a in zip(self._elapsed, s.answers) if a])]
            xids = self._kernel_xids()
            if xids:
                doubt.append(f"The kernel logged Xid {', '.join(map(str, xids))} during this test.")
            doubt = [d for d in doubt if d]
            if doubt and not self.confirm("Count this test as working?", "\n\n".join(doubt)):
                return
        s.answer(fails)
        self._elapsed.append(round(elapsed, 1))
        self._step()

    def undo(self):
        self._search.undo()
        self._elapsed = self._elapsed[:len(self._search.answers)]
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
        self._elapsed, self._applied_at = [], None
        self._build([])
        self._step()

    # ── evidence ──
    def _kernel_xids(self) -> list:
        if not self._applied_at:
            return []
        k = self.collect(self._applied_at, self.now() + 5)
        return sorted({e["xid"] for e in k.get("events", []) if e.get("xid") is not None})

    def _show_kernel(self):
        if not self._applied_at:
            self._evidence.setText("No test is running.")
            return
        k = self.collect(self._applied_at, self.now() + 5)
        ev = k.get("events", [])
        if k.get("status") != "ok":
            self._evidence.setText(f"Kernel log unavailable: {k.get('reason', '')}")
        elif not ev:
            self._evidence.setText("No GPU errors in the kernel log since this test started.")
        else:
            self._evidence.setText("Since this test started:\n" + "\n".join(e["message"][:200] for e in ev[-5:]))

    @staticmethod
    def _ask(title, text) -> bool:
        return QMessageBox.question(None, title, text + "\n\nCount it as working anyway?") == QMessageBox.Yes

    # ── display ──
    def _show(self, mode: str):
        visible = {"idle": {self._start},
                   "paused": {self._resume, self._restart, self._stop},
                   "running": {self._ok, self._bad, self._undo, self._kernel, self._restart, self._stop},
                   "done": {self._undo, self._finish_btn, self._stop},
                   "empty": {self._stop}}[mode]
        for b in self._buttons:
            b.setVisible(b in visible)
        self._undo.setEnabled(bool(self._search and self._search.tests_done))
        self._keep_drs.setVisible(mode == "idle")
        self._evidence.clear()
        if mode == "idle":
            pri = ", ".join(_label(p) for p in self._priority)
            self._intro.setText(
                "Load the profile that crashes into the editor, then start. Each step puts a test "
                "configuration into the editor: write it to the game, play until the crash would have "
                "happened, and answer. The search also finds settings that only crash together and "
                "keeps going if there is more than one cause."
                + (f"\n\nTested first (from triage): {pri}" if pri else "")
                + (f"\nThe crashed run lasted {self._crash_hint} s; shorter “works” answers are questioned."
                   if self._crash_hint else ""))
            self._text.clear()
            self.status_changed.emit("Not running")
        elif mode == "paused":
            self._intro.setText("A saved search was found.")
            self._text.setText(f"{self._search.tests_done} test(s) answered. “Continue” puts the pending "
                               "test configuration back into the editor.")
            self.status_changed.emit(f"Paused at test {self._search.tests_done + 1}")

    def _step(self, keep_clock=False):
        s = self._search
        if not s.items:
            self._intro.setText("")
            self._text.setText("No settings to test: the editor has nothing besides what is kept on.")
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
            head = f"Test {n} — baseline: only the settings that are always kept on" + (
                " (DRS)" if self._fixed else "") + "."
        elif s.phase == "verify":
            found = ", ".join(_label(i) for g in s.found_so_far for i in g)
            head = (f"Test {n} — check: everything except {found} is on. "
                    "If it still crashes there is another, independent cause.")
        elif len(s.pending) <= _LIST_MAX:
            head = f"Test {n}: on — " + ", ".join(_label(i) for i in s.pending)
        else:
            head = f"Test {n}: {len(s.pending)} of {len(s.items)} tested settings on."
        cands = [] if s.phase == "verify" else s.candidates
        still = (", ".join(_label(i) for i in cands) if len(cands) <= _LIST_MAX
                 else f"{len(cands)} settings") or "—"
        self._intro.setText("")
        kept = f"\nAlways on: {len(self._fixed)} DRS setting(s) and the GPU arch." if self._fixed else ""
        self._text.setText(
            head + f"\n\nStill suspected: {still}\nAbout {s.estimate_left()} more test(s) if one setting "
            f"is the cause.{kept}\n\nWrite the configuration to the game, play, then answer.")
        self.status_changed.emit(f"Test {n} · ≈{s.estimate_left()} left")

    def _finish(self):
        s = self._search
        self._show("done")
        kind, groups = s.outcome
        if kind == "fixed":
            self._win.apply_state(self._full, "Crash search: restore")
            self._intro.setText("")
            self._text.setText("It crashes even with only the kept settings on, so none of the tested settings "
                               "is needed for the crash. Untick “Keep DRS settings on” and start again to test "
                               "the DRS settings too — or the cause is outside DRSTool (driver, game, runner).")
            self.status_changed.emit("Kept settings alone crash")
            return
        culprits = s.culprits
        others = [i for i in bisect_items(self._full) if i not in culprits]
        self._win.apply_state(bisect_state(self._full, others), "Without " + ", ".join(map(_label, culprits)))
        lines = []
        for k, g in enumerate(groups, 1):
            names = " + ".join(_label(i) for i in g)
            lines.append(f"{k}. {names}" + ("   (only together: switching off one of them is enough)"
                                             if len(g) > 1 else ""))
        more = ("\n\nThere are independent causes: each one crashes on its own, so all of them must be off."
                if len(groups) > 1 else "")
        self._intro.setText("")
        self._text.setText(f"Found after {s.tests_done} test(s):\n" + "\n".join(lines) + more
                           + "\n\nThe editor now has every other setting on — verified to work in the last "
                           "test. “Keep result” leaves it so; “Stop and restore” brings the full configuration back.")
        self.status_changed.emit("Found: " + ", ".join(_label(i) for i in culprits))


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

    def _hand_over(self, suspects, seconds):
        self.search.set_hints(suspects, seconds)
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
            it = make_list_item(title, title)
            it.setData(ROLE_GLYPH, glyph)
            it.setData(ROLE_SUB, sub)
            it.setToolTip(sub)
            self.addItem(it)
            self._items[title] = it
        self.currentItemChanged.connect(lambda cur, _p=None: cur and self._lab and self._lab.show_tab(cur.text()))

    def bind(self, lab: CrashLabWidget):
        self._lab = lab
        lab.currentChanged.connect(lambda i: self._select(lab.tabText(i)))
        lab.search.status_changed.connect(lambda t: self._items[SEARCH].setData(ROLE_SUB, t))
        self._items[SEARCH].setData(ROLE_SUB, "Paused — continue it here" if lab.search.running
                                    else self.NAV[1][2])
        self._select(lab.tabText(lab.currentIndex()))

    def _select(self, title):
        it = self._items.get(title)
        if it is not None and it is not self.currentItem():
            self.blockSignals(True)
            self.setCurrentItem(it)
            self.blockSignals(False)
