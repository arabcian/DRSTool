"""Banner and watcher for files modified outside DRSTool."""

from pathlib import Path
from typing import Dict, List

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .theme import T
from .tool_ui import _tool_btn
from .widgets import _file_sha


# ----------------------------------------------------------------------------
# External-change watcher + banner (profiles.json, the selected Lutris YAML)
# ----------------------------------------------------------------------------

class ExternalChangeBanner(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"QFrame{{background:#2a2410;border-bottom:1px solid {T['warn']};}}"
                           f"QLabel{{color:{T['text']};}}")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(10, 4, 10, 4)
        self._lay.setSpacing(2)
        self._rows: Dict[str, QWidget] = {}
        self.hide()

    def post(self, key: str, text: str, actions: List[tuple]):
        self.drop(key)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        lab = QLabel("⚠ " + text)
        lab.setWordWrap(True)
        h.addWidget(lab, 1)
        for label, cb in actions:
            b = _tool_btn(label)
            b.clicked.connect(lambda _c=False, f=cb: (self.drop(key), f()))
            h.addWidget(b)
        ig = _tool_btn("Ignore")
        ig.clicked.connect(lambda _c=False: self.drop(key))
        h.addWidget(ig)
        self._rows[key] = row
        self._lay.addWidget(row)
        self.show()

    def drop(self, key: str):
        row = self._rows.pop(key, None)
        if row is not None:
            row.setParent(None)
            row.deleteLater()
        if not self._rows:
            self.hide()


class ExternalChangeWatcher(QObject):
    """Content-hash based, so our own writes (mark()) and plain `touch`es never raise a notice."""
    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._w = QFileSystemWatcher(self)
        self._items: Dict[str, dict] = {}
        self._pending: set = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._flush)
        self._w.fileChanged.connect(self._on_path)
        self._w.directoryChanged.connect(self._on_path)

    def _arm(self, path: Path):
        have = set(self._w.files()) | set(self._w.directories())
        for p in (path, path.parent):
            if p.exists() and str(p) not in have:
                self._w.addPath(str(p))

    def track(self, key: str, path):
        self.untrack(key)
        path = Path(path)
        self._items[key] = {"path": path, "known": _file_sha(path)}
        self._arm(path)

    def untrack(self, key: str):
        it = self._items.pop(key, None)
        if it is None:
            return
        still = [i["path"] for i in self._items.values()]
        if it["path"] not in still:
            self._w.removePath(str(it["path"]))
        if it["path"].parent not in [p.parent for p in still]:
            self._w.removePath(str(it["path"].parent))

    def mark(self, key: str):
        it = self._items.get(key)
        if it:
            it["known"] = _file_sha(it["path"])
            self._arm(it["path"])

    def _on_path(self, p: str):
        for key, it in self._items.items():
            if str(it["path"]) == p or str(it["path"].parent) == p:
                self._pending.add(key)
        self._timer.start()

    def _flush(self):
        keys, self._pending = self._pending, set()
        for key in keys:
            it = self._items.get(key)
            if not it:
                continue
            self._arm(it["path"])
            cur = _file_sha(it["path"])
            if cur != it["known"]:
                it["known"] = cur
                self.changed.emit(key)
