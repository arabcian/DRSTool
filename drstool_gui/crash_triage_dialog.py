"""Crash triage: known signatures, run fingerprint, kernel journal and a clean-run comparison."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QVBoxLayout,
)

from drstool_core import kernel_events
from drstool_core.crash_triage import format_report, kernel_window, triage


def _read(paths) -> list:
    lines = []
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines += f.read().splitlines()
    return lines


def analyse(paths, clean_paths=(), use_journal=True, collect=kernel_events.collect):
    """-> (result, clean result or None, note about the kernel journal)."""
    lines = _read(paths)
    result = triage(lines)
    note = "Kernel journal: not read."
    window = kernel_window(result) if use_journal else None
    if use_journal and not window:
        note = "Kernel journal: the log has no timestamps to place the run in time."
    elif window:
        k = collect(*window)
        events = k.get("events", [])
        note = f"Kernel journal: {k.get('status')} — {len(events)} relevant event(s). {k.get('reason', '')}".strip()
        if events:
            result = triage(lines + [e["message"] for e in events])
    clean = triage(_read(clean_paths)) if clean_paths else None
    return result, clean, note


class CrashTriageDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.setWindowTitle("Crash triage")
        self.resize(900, 640)
        self._win = win
        self._paths: list = []
        self._clean: list = []
        self._result = self._clean_result = None
        lay = QVBoxLayout(self)
        self._files = QLabel("Add the game / Proton log of the crashed run. A saved kernel log "
                             "(dmesg > file) can be added too.")
        self._files.setWordWrap(True)
        lay.addWidget(self._files)
        self._journal = QCheckBox("Read the kernel journal around the run's time (journalctl -k, no root)")
        self._journal.setChecked(True)
        lay.addWidget(self._journal)
        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        lay.addWidget(self._view, 1)
        row = QHBoxLayout()
        add = QPushButton("Crashed run log(s)…")
        clean = QPushButton("Clean run log…")
        self._export = QPushButton("Export Markdown…")
        self._bisect = QPushButton("Bisect, suspects first")
        close = QPushButton("Close")
        add.clicked.connect(self._pick)
        clean.clicked.connect(self._pick_clean)
        self._export.clicked.connect(self._save)
        self._bisect.clicked.connect(self._open_bisect)
        close.clicked.connect(self.close)
        for b in (add, clean, self._export, self._bisect, close):
            row.addWidget(b)
        lay.addLayout(row)
        self._export.setEnabled(False)
        self._bisect.setEnabled(False)

    def _pick(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Crashed run: game log and kernel log", str(Path.home()))
        if paths:
            self._paths = paths
            self._run()

    def _pick_clean(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Clean run log (same game, no crash)", str(Path.home()))
        if paths:
            self._clean = paths
            if self._paths:
                self._run()

    def _run(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._result, self._clean_result, note = analyse(self._paths, self._clean, self._journal.isChecked())
        finally:
            QApplication.restoreOverrideCursor()
        self._files.setText("Crashed: " + ", ".join(Path(p).name for p in self._paths)
                            + ("\nClean: " + ", ".join(Path(p).name for p in self._clean) if self._clean else ""))
        self._view.setPlainText(format_report(self._result, clean=self._clean_result) + "\n\n" + note)
        self._export.setEnabled(True)
        self._bisect.setEnabled(True)

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export crash report", str(Path.home() / "crash-report.md"),
                                              "Markdown (*.md)")
        if path:
            Path(path).write_text(format_report(self._result, clean=self._clean_result, markdown=True) + "\n",
                                  encoding="utf-8")

    def _open_bisect(self):
        from .bisect_dialog import BisectDialog
        BisectDialog(self._win, priority=self._result["suspects"] if self._result else ()).show()
