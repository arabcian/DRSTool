"""Ctrl+K command palette."""

from typing import List, Optional

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QVBoxLayout,
)

from .favorites import FAVS
from .theme import T


class CommandPalette(QDialog):
    """Ctrl+K: search every DRS setting / env var / GPU arch / tool page."""

    def __init__(self, items: List[dict], parent=None, recent: Optional[List[str]] = None):
        super().__init__(parent)
        self._recent = {k: i for i, k in enumerate(recent or [])}
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        self.setStyleSheet(f"QDialog{{background:{T['raised']};border:1px solid {T['accent_dim']};border-radius:8px;}}")
        self.resize(720, 480)
        self._items = items
        self.chosen: Optional[dict] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText("Search settings, env vars, GPU archs, tools…   "
                                      "(Enter: open · Ctrl+D: favorite · Esc: close)")
        self._edit.setStyleSheet(f"QLineEdit{{background:{T['surface']};border:1px solid {T['border_hi']};"
                                 f"color:{T['text']};padding:6px 10px;border-radius:5px;font-size:14px;}}")
        self._edit.textChanged.connect(self._refill)
        self._edit.installEventFilter(self)
        lay.addWidget(self._edit)
        row = QHBoxLayout()
        self._only_set = QCheckBox("only set")
        self._only_fav = QCheckBox("★ favorites")
        for c in (self._only_set, self._only_fav):
            c.setStyleSheet(f"color:{T['text2']};")
            c.toggled.connect(lambda _c: self._refill())
            row.addWidget(c)
        row.addStretch(1)
        self._count = QLabel("")
        self._count.setStyleSheet(f"color:{T['muted']}; font-size:11px;")
        row.addWidget(self._count)
        lay.addLayout(row)
        self._list = QListWidget()
        self._list.setStyleSheet(
            f"QListWidget{{background:{T['bg']};border:1px solid {T['border']};border-radius:5px;outline:none;}}"
            f"QListWidget::item{{padding:5px 8px;color:{T['text2']};}}"
            f"QListWidget::item:selected{{background:rgba(118,185,0,0.20);color:{T['text']};}}")
        self._list.itemActivated.connect(self._accept_item)
        self._list.itemDoubleClicked.connect(self._accept_item)
        lay.addWidget(self._list, 1)
        self._refill()
        self._edit.setFocus()

    @staticmethod
    def _score(q: List[str], it: dict) -> int:
        name = it["title"].lower()
        hay = f'{name} {it.get("key", "").lower()} {it.get("sub", "").lower()} {it.get("desc", "").lower()}'
        s = 0
        for tok in q:
            if tok in name:
                s += 100 - min(60, name.index(tok)) + (30 if name.startswith(tok) else 0)
            elif tok in hay:
                s += 25
            else:
                # subsequence fallback on the name
                i = 0
                for ch in name:
                    if i < len(tok) and ch == tok[i]:
                        i += 1
                if i == len(tok) and len(tok) >= 3:
                    s += 8
                else:
                    return -1
        return s

    def _refill(self):
        q = self._edit.text().lower().split()
        scored = []
        for it in self._items:
            if self._only_set.isChecked() and not it.get("is_set"):
                continue
            if self._only_fav.isChecked() and not it.get("fav"):
                continue
            sc = self._score(q, it) if q else (50 if it.get("fav") else 10 if it.get("is_set") else 0)
            if sc < 0:
                continue
            _rr = self._recent.get(f'{it["kind"]}|{it["key"]}')
            if _rr is not None and not q:
                sc = 300 - _rr                   # last picks float to the top of the empty palette
            if it.get("fav"):
                sc += 15
            if it.get("is_set"):
                sc += 10
            scored.append((-sc, it["title"].lower(), it))
        scored.sort(key=lambda x: (x[0], x[1]))
        self._list.clear()
        for _s, _n, it in scored[:300]:
            star = ("↺ " if (not q and f'{it["kind"]}|{it["key"]}' in self._recent)
                    else "★ " if it.get("fav") else "   ")
            dot = "● " if it.get("is_set") else "   "
            val = f'   = {it["value"]}' if it.get("value") else ""
            li = QListWidgetItem(f'{star}{dot}{it["title"]}{val}    ·  {it.get("sub", "")}')
            li.setData(Qt.UserRole, it)
            if it.get("unavail"):
                li.setForeground(QColor(T["muted"]))
                li.setToolTip(it["unavail"])
            self._list.addItem(li)
        self._count.setText(f"{min(len(scored), 300)} of {len(self._items)}")
        if self._list.count():
            self._list.setCurrentRow(0)

    def _accept_item(self, li):
        self.chosen = li.data(Qt.UserRole)
        self.accept()

    def eventFilter(self, obj, ev):           # noqa: N802
        if obj is self._edit and ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k in (Qt.Key_Down, Qt.Key_Up):
                r = self._list.currentRow() + (1 if k == Qt.Key_Down else -1)
                self._list.setCurrentRow(max(0, min(self._list.count() - 1, r)))
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if self._list.currentItem():
                    self._accept_item(self._list.currentItem())
                return True
            if k == Qt.Key_D and ev.modifiers() & Qt.ControlModifier:
                self._toggle_fav()
                return True
        return super().eventFilter(obj, ev)

    def _toggle_fav(self):
        li = self._list.currentItem()
        if not li:
            return
        it = li.data(Qt.UserRole)
        if it.get("fav_kind") and it.get("fav_key"):
            it["fav"] = FAVS.toggle(it["fav_kind"], it["fav_key"])
            row = self._list.currentRow()
            self._refill()
            self._list.setCurrentRow(min(row, self._list.count() - 1))
