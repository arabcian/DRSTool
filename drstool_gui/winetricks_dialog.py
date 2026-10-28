"""Install Windows runtime components (winetricks / protontricks) into one Wine prefix."""

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout,
)

from .base import winetricks
from .theme import T
from .tool_ui import _tool_btn
from .widgets import _MONO_BOX_QSS, _desc_label, _title_label


class _TricksWorker(QThread):
    line = Signal(str)
    done = Signal(int)                  # exit code; -1 when it could not start

    def __init__(self, argv, env, parent=None):
        super().__init__(parent)
        self._argv, self._env = argv, env
        self._proc = None

    def cancel(self):
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()

    def run(self):
        try:
            self._proc = subprocess.Popen(
                self._argv, env={**os.environ, **self._env}, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        except OSError as ex:
            self.line.emit(f"✗ could not start: {ex}")
            self.done.emit(-1)
            return
        for raw in self._proc.stdout:
            self.line.emit(raw.rstrip())
        self.done.emit(self._proc.wait())


class WinetricksDialog(QDialog):
    def __init__(self, prefix: Path, game_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Windows components")
        self.setMinimumWidth(520)
        self._prefix = Path(prefix)
        self._worker = None

        lay = QVBoxLayout(self)
        lay.addWidget(_title_label("Windows components"))
        lay.addWidget(_desc_label(
            f"Installs runtime components into the Wine prefix of {game_name} with winetricks, or "
            "protontricks for Steam (Proton) games. Only the components listed here can be installed. "
            "Large downloads can take a while; the log below shows what the tool is doing."))
        self._checks = {}
        for verb, label in winetricks.COMPONENTS:
            cb = QCheckBox(f"{label}  ({verb})")
            cb.setStyleSheet(f"color:{T['text2']};")
            cb.toggled.connect(lambda _c: self._sync())
            lay.addWidget(cb)
            self._checks[verb] = cb

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:11px;")
        lay.addWidget(self._status)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(_MONO_BOX_QSS)
        self._log.setFixedHeight(180)
        lay.addWidget(self._log)

        row = QHBoxLayout()
        row.addStretch(1)
        self._cancel_btn = _tool_btn("Cancel")
        self._cancel_btn.clicked.connect(lambda: self._worker and self._worker.cancel())
        row.addWidget(self._cancel_btn)
        self._run_btn = _tool_btn("Install selected", bg="#1d2a10", border="#4a7300", fg="#cfe8a8", bold=True)
        self._run_btn.clicked.connect(self._run)
        row.addWidget(self._run_btn)
        close = _tool_btn("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        lay.addLayout(row)
        self._sync()

    def _selected(self):
        return [v for v, cb in self._checks.items() if cb.isChecked()]

    def _sync(self):
        busy = self._worker is not None and self._worker.isRunning()
        try:
            tool = winetricks.required_tool(self._prefix)
            found = winetricks.tool_path(self._prefix) is not None
        except Exception:                                # noqa: BLE001
            tool, found = "winetricks", False
        if found:
            self._status.setText(f"Using {tool}.")
        else:
            self._status.setText(f"⚠ {tool} is not installed or not on PATH. Install it with your "
                                 "package manager, then reopen this window.")
        self._run_btn.setEnabled(not busy and found and bool(self._selected()))
        self._cancel_btn.setVisible(busy)
        for cb in self._checks.values():
            cb.setEnabled(not busy)

    def _run(self):
        try:
            p = winetricks.plan(self._prefix, self._selected())
        except (ValueError, RuntimeError) as ex:
            self._log.appendPlainText(f"✗ {ex}")
            return
        self._log.appendPlainText("$ " + " ".join(p.argv))
        self._worker = _TricksWorker(p.argv, p.env, self)
        self._worker.line.connect(self._log.appendPlainText)
        self._worker.done.connect(self._finished)
        self._worker.start()
        self._sync()

    def _finished(self, rc: int):
        self._log.appendPlainText("✓ done" if rc == 0 else f"✗ exited with code {rc}")
        self._sync()

    def closeEvent(self, ev):
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        super().closeEvent(ev)
