"""Shortcut sheet, Appearance dialog, accessibility labelling."""

import re
import sys

from PySide6.QtCore import QEvent, QObject, QProcess, QSettings, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QAbstractSlider, QAbstractSpinBox, QCheckBox, QComboBox,
    QDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSpinBox, QTabBar, QTextEdit, QVBoxLayout, QWidget,
)

from .base import APP_ID
from .theme import ACCENT_PRESETS, APPEARANCE, btn_ss, T
from .widgets import NavTab


# ----------------------------------------------------------------------------
# Keyboard shortcut sheet (F1) — the single list of what the program binds
# ----------------------------------------------------------------------------

SHORTCUT_SHEET = [
    ("Navigate", [
        ("Ctrl+K", "Command palette — settings, variables, architectures, tools, actions"),
        ("Ctrl+F", "Filter the current list"),
        ("Esc", "Clear the filter"),
        ("Ctrl+1 … 5", "Switch the top tabs"),
        ("F1  /  Ctrl+/", "This sheet"),
        ("Ctrl+,", "Appearance (theme, accent, density, scale)"),
        ("Tab / Shift+Tab", "Move between controls"),
    ]),
    ("Edit", [
        ("Ctrl+Z", "Undo"),
        ("Ctrl+Shift+Z  /  Ctrl+Y", "Redo"),
        ("Ctrl+D", "Star / unstar the selected entry"),
        ("Ctrl+R", "Recipes"),
        ("Ctrl+Shift+V", "Import a launch line or a DRSTOOL1: share code"),
    ]),
    ("Profile & output", [
        ("Ctrl+S", "Save the current profile"),
        ("Ctrl+Shift+C", "Copy the launch string"),
    ]),
]


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.setModal(True)
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(6)
        for sec, rows in SHORTCUT_SHEET:
            h = QLabel(sec.upper())
            h.setStyleSheet(f"color:{T['muted']};font-size:10px;font-weight:700;letter-spacing:1.2px;"
                            "margin-top:8px;")
            lay.addWidget(h)
            grid = QGridLayout()
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(5)
            for r, (keys, what) in enumerate(rows):
                k = QLabel(keys)
                k.setStyleSheet(f"background:{T['sunken']};border:1px solid {T['border_hi']};border-radius:5px;"
                                f"padding:2px 8px;font-family:{T['mono']};font-size:11px;color:{T['text']};")
                k.setAlignment(Qt.AlignCenter)
                d = QLabel(what)
                d.setStyleSheet(f"color:{T['text2']};font-size:12px;")
                grid.addWidget(k, r, 0, Qt.AlignLeft)
                grid.addWidget(d, r, 1)
            grid.setColumnStretch(1, 1)
            lay.addLayout(grid)
        close = QPushButton("Close")
        close.setStyleSheet(btn_ss("default", "md"))
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        lay.addSpacing(8)
        lay.addLayout(row)


# ----------------------------------------------------------------------------
# Appearance dialog
# ----------------------------------------------------------------------------

class AppearanceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Appearance")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.restart_requested = False
        self.needs_restart = False
        ap = APPEARANCE
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(12)
        title = QLabel("Appearance")
        title.setStyleSheet(f"color:{T['text']};font-size:16px;font-weight:700;")
        lay.addWidget(title)
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        self._theme = QComboBox()
        self._theme.addItem("Dark", "dark")
        self._theme.addItem("Light", "light")
        self._accent = QComboBox()
        for key, col in ACCENT_PRESETS.items():
            pm = QPixmap(14, 14)
            pm.fill(QColor(col))
            self._accent.addItem(QIcon(pm), key.capitalize(), key)
        self._density = QComboBox()
        for k, lbl in (("compact", "Compact"), ("default", "Default"), ("spacious", "Spacious")):
            self._density.addItem(lbl, k)
        self._scale = QSpinBox()
        self._scale.setRange(70, 200)
        self._scale.setSingleStep(5)
        self._scale.setSuffix(" %")
        for combo, key in ((self._theme, "theme"), (self._accent, "accent"), (self._density, "density")):
            combo.setCurrentIndex(max(0, combo.findData(ap[key])))
        self._scale.setValue(ap["scale"])
        form.addRow("Theme", self._theme)
        form.addRow("Accent colour", self._accent)
        form.addRow("List density", self._density)
        form.addRow("UI scale", self._scale)
        lay.addLayout(form)
        self._restore = QCheckBox("Restore the last profile and selections on start")
        self._restore.setChecked(ap["restore"])
        lay.addWidget(self._restore)
        note = QLabel("Theme, accent, density and scale are read at start-up, so they apply after a restart. "
                      "The accent also colours “ok” states, which are green by default.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{T['muted']};font-size:11px;")
        lay.addWidget(note)
        row = QHBoxLayout()
        reset = QPushButton("Reset")
        reset.setStyleSheet(btn_ss("ghost", "md"))
        reset.clicked.connect(self._reset)
        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(btn_ss("default", "md"))
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setStyleSheet(btn_ss("default", "md"))
        save.clicked.connect(lambda: self._save(False))
        both = QPushButton("Save && restart")
        both.setStyleSheet(btn_ss("primary", "md"))
        both.clicked.connect(lambda: self._save(True))
        row.addWidget(reset)
        row.addStretch(1)
        for b in (cancel, save, both):
            row.addWidget(b)
        lay.addLayout(row)

    def _reset(self):
        self._theme.setCurrentIndex(self._theme.findData("dark"))
        self._accent.setCurrentIndex(self._accent.findData("green"))
        self._density.setCurrentIndex(self._density.findData("default"))
        self._scale.setValue(100)
        self._restore.setChecked(True)

    def _save(self, restart: bool):
        ap = APPEARANCE
        new = {"theme": self._theme.currentData(), "accent": self._accent.currentData(),
               "density": self._density.currentData(), "scale": self._scale.value()}
        self.needs_restart = any(new[k] != ap[k] for k in new)
        try:
            qs = QSettings(APP_ID, APP_ID)
            for k, v in new.items():
                qs.setValue(f"appearance/{k}", v)
            qs.setValue("appearance/restore_session", self._restore.isChecked())
            qs.sync()
        except Exception:
            pass
        self.restart_requested = restart and self.needs_restart
        self.accept()


def _spawn_restart():
    """Start a fresh copy of this program once the current one has exited."""
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable] + sys.argv)
    QProcess.startDetached("/bin/sh", ["-c", 'sleep 0.7; exec "$@"', "sh"] + argv)


# ----------------------------------------------------------------------------
# Accessible names (one application-wide event filter)
# ----------------------------------------------------------------------------

def _a11y_label(w: QWidget):
    tip = w.toolTip() or ""
    name = ""
    if isinstance(w, NavTab):
        name = w._title.text()
    elif isinstance(w, QAbstractButton):
        name = re.sub(r"&", "", w.text() or "").strip()
        if name and not any(c.isalnum() for c in name):
            name = ""                                   # glyph-only button: use the tooltip
    elif isinstance(w, QLineEdit):
        name = w.placeholderText()
    elif isinstance(w, QGroupBox):
        name = w.title()
    if not name and w.parentWidget() is not None:
        # Buddy labels are siblings: a recursive search here ran once per newly
        # shown control over the whole subtree (quadratic on the big tool pages).
        for lbl in w.parentWidget().findChildren(QLabel, options=Qt.FindDirectChildrenOnly):
            if lbl.buddy() is w and lbl.text():
                name = re.sub(r"<[^>]+>|&", "", lbl.text()).strip()
                break
    if not name and tip:
        name = re.sub(r"<[^>]+>", "", tip).split("\n", 1)[0]
    if name:
        w.setAccessibleName(name[:120])
    if tip and not w.accessibleDescription():
        w.setAccessibleDescription(re.sub(r"<[^>]+>", "", tip)[:300])


class UXFilter(QObject):
    """Names unlabeled controls (accessible names) when they are first shown.
    The Tab focus ring was removed: the option grids cannot be browsed with the arrow keys,
    and the ring also wrapped the big list / editor panels in a stray green box."""
    _A11Y = (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox, QAbstractSlider,
             QAbstractItemView, QPlainTextEdit, QTextEdit, QTabBar, QGroupBox)

    def __init__(self, app):
        super().__init__(app)
        app.installEventFilter(self)

    def label_tree(self, root: QWidget):
        """Name the controls that already exist, once. Installing the filter before the window
        is built made ~62,000 Python eventFilter calls (~0.5 s) during construction."""
        for w in root.findChildren(QWidget):
            if isinstance(w, self._A11Y) and not w.property("_a11y"):
                w.setProperty("_a11y", True)
                if not w.accessibleName():
                    _a11y_label(w)

    def eventFilter(self, obj, ev):           # noqa: N802
        try:
            if ev.type() == QEvent.Show and isinstance(obj, self._A11Y) and not obj.property("_a11y"):
                obj.setProperty("_a11y", True)
                if not obj.accessibleName():
                    _a11y_label(obj)
        except Exception:
            pass
        return False


def _qbool(v) -> bool:
    return str(v).lower() in ("true", "1", "yes")
