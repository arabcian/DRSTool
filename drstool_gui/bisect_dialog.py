"""Find the setting(s) that break a game: delta debugging (ddmin), one manual test at a time.

The session (full configuration + answers) is saved after every answer, so closing DRSTool
mid-search and opening the dialog again resumes it. Undo replays the answers minus the last.
"""

import json

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from drstool_core.bisect_search import DDMin, order_by_priority

from .base import APP_ID
from .state import bisect_items, bisect_state, copy_state

_KEY = "bisect/session"
_LIST_MAX = 12


def _label(item) -> str:
    kind, key = item
    return {"settings": "DRS", "env": "env", "vkd3d": "VKD3D_CONFIG", "gs_on": "gamescope"}[kind] + (f" {key}" if key else "")


def parse_suspect(text: str):
    """"env:NAME" / "vkd3d:flag" / "settings:0xID" -> bisect item tuple."""
    kind, _, key = text.partition(":")
    return (kind, key)


def _load() -> dict:
    try:
        s = json.loads(str(QSettings(APP_ID, APP_ID).value(_KEY, "") or "{}"))
        return s if isinstance(s, dict) and isinstance(s.get("full"), dict) else {}
    except ValueError:
        return {}


class BisectDialog(QDialog):
    """Non-modal: the editor holds the test configuration; the user writes it to the game
    (Games tab / launcher tab) and launches it, then answers here."""

    def __init__(self, win, priority=()):
        super().__init__(win)
        self.setWindowTitle("Find the breaking setting")
        self._win = win
        saved = _load()
        self._resumed = bool(saved)
        self._full = copy_state(saved["full"]) if saved else copy_state(win.capture_state())
        self._priority = ([tuple(p) for p in saved.get("priority", [])] if saved
                          else [parse_suspect(p) if isinstance(p, str) else tuple(p) for p in priority])
        lay = QVBoxLayout(self)
        self._keep_drs = QCheckBox("Keep DRS settings on (test only env vars, VKD3D_CONFIG flags and gamescope)")
        self._keep_drs.setChecked(saved.get("keep_drs", True) if saved else True)
        self._keep_drs.toggled.connect(lambda _: self._rebuild([]))
        lay.addWidget(self._keep_drs)
        self._text = QLabel()
        self._text.setWordWrap(True)
        lay.addWidget(self._text)
        row = QHBoxLayout()
        self._ok = QPushButton("Works")
        self._bad = QPushButton("Still broken")
        self._undo = QPushButton("Undo")
        restart = QPushButton("Start over")
        stop = QPushButton("Stop and restore")
        self._ok.clicked.connect(lambda: self._answer(False))
        self._bad.clicked.connect(lambda: self._answer(True))
        self._undo.clicked.connect(self._undo_answer)
        restart.clicked.connect(lambda: self._rebuild([]))
        stop.clicked.connect(self._restore)
        for b in (self._ok, self._bad, self._undo, restart, stop):
            row.addWidget(b)
        lay.addLayout(row)
        self._rebuild(saved.get("answers", []) if saved else [])

    # ── search ──
    def _rebuild(self, answers):
        items = bisect_items(self._full)
        self._fixed = [i for i in items if i[0] == "settings"] if self._keep_drs.isChecked() else []
        rest = [i for i in items if i not in self._fixed]
        self._search = DDMin(order_by_priority(rest, self._priority), answers)
        self._step()

    def _save(self):
        QSettings(APP_ID, APP_ID).setValue(_KEY, json.dumps({
            "full": self._full, "keep_drs": self._keep_drs.isChecked(),
            "priority": [list(p) for p in self._priority], "answers": self._search.answers}))

    def _answer(self, fails: bool):
        self._search.answer(fails)
        self._step()

    def _undo_answer(self):
        self._search.undo()
        self._step()

    def _step(self):
        s = self._search
        self._keep_drs.setEnabled(s.tests_done == 0)
        self._undo.setEnabled(s.tests_done > 0)
        self._save()
        if not s.items:
            self._ok.hide()
            self._bad.hide()
            self._text.setText("No settings to test: the editor has nothing besides what is kept on.")
            return
        if s.done:
            self._finish()
            return
        self._ok.show()
        self._bad.show()
        on = self._fixed + s.pending
        self._win.apply_state(bisect_state(self._full, on), f"Bisect test {s.tests_done + 1}")
        if not s.pending:
            what = "only the settings that are always kept on" + (" (DRS)" if self._fixed else "")
        elif len(s.pending) <= _LIST_MAX:
            what = ", ".join(_label(i) for i in s.pending)
        else:
            what = f"{len(s.pending)} of {len(s.items)} tested settings"
        self._text.setText(
            ("Resumed the saved search.\n\n" if self._resumed and s.tests_done else "")
            + f"Test {s.tests_done + 1}: the editor now has {what} switched on.\n\n"
            "Write it to the game, launch it and play long enough for the crash to show up, "
            "then answer below. The search also finds settings that only crash together.")

    def _finish(self):
        self._ok.hide()
        self._bad.hide()
        kind, culprits = self._search.outcome
        if kind == "fixed":
            self._win.apply_state(self._full, "Bisect: restore")
            self._text.setText("It fails even with only the kept settings on, so none of the tested settings "
                               "is needed for the crash. Untick “Keep DRS settings on” and start over to test "
                               "the DRS settings too.")
            return
        others = [i for i in bisect_items(self._full) if i not in culprits]
        self._win.apply_state(bisect_state(self._full, others), "Without " + ", ".join(map(_label, culprits)))
        names = "\n".join("• " + _label(i) for i in culprits)
        together = ("\n\nThey only fail together: switching off any one of them is enough."
                    if len(culprits) > 1 else "")
        self._text.setText(f"Breaking setting(s) after {self._search.tests_done} test(s):\n{names}{together}\n\n"
                           "The editor now has every other setting on. “Stop and restore” brings the full "
                           "configuration back.")

    def _restore(self):
        QSettings(APP_ID, APP_ID).remove(_KEY)
        self._win.apply_state(self._full, "Bisect: restore")
        self.close()
