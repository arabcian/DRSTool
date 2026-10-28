"""Install Windows libraries (winetricks / protontricks) into one Wine prefix.

Every library of winetricks' "dlls" category is listed (drstool_core/data/winetricks_dlls.json),
searchable, grouped, with the ones already installed in the prefix marked and the ones games
ask for most often on top. A caller that knows what is missing (a log that says
"MSVCP140.dll not found") passes `suggest` and the dialog opens with that library ticked."""

import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .base import winetricks
from .theme import btn_ss, T
from .tool_ui import _tool_btn
from .widgets import _COMBO_QSS, _LINEEDIT_QSS, _MONO_BOX_QSS, _desc_label, _pack2_table_ss, _title_label

_log = logging.getLogger(__name__)

_VERB, _DESC, _STATE = 0, 1, 2
# view-combo entries that are not a catalog category
_V_REC, _V_ALL, _V_INST, _V_SEL = "__rec", "__all", "__inst", "__sel"


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
    """`suggest`: [(verb, why)] to tick and explain on opening (unknown verbs are ignored)."""

    def __init__(self, prefix: Path, game_name: str, parent=None,
                 suggest: Optional[Sequence[Tuple[str, str]]] = None):
        super().__init__(parent)
        self.setWindowTitle(f"Windows libraries — {game_name}")
        self.resize(780, 700)
        self.setMinimumWidth(560)
        self._prefix = Path(prefix)
        self._worker: Optional[_TricksWorker] = None
        self._entries: List[dict] = winetricks.catalog()
        self._by_verb: Dict[str, dict] = {e["verb"]: e for e in self._entries}
        self._cat_title: Dict[str, str] = {c["id"]: c["title"] for c in winetricks.categories()}
        self._installed = winetricks.installed_verbs(self._prefix)
        self._checked: set = set()                      # survives filtering / searching
        self._items: Dict[str, QTreeWidgetItem] = {}
        self._building = False
        self._suggest = [(v, w) for v, w in (suggest or []) if v in self._by_verb]

        lay = QVBoxLayout(self)
        lay.addWidget(_title_label("Windows libraries"))
        lay.addWidget(_desc_label(
            f"Installs Windows libraries into the Wine prefix of {game_name} with winetricks, or "
            "protontricks for Steam (Proton) games: Visual C++ runtimes, DirectX / D3DX, "
            ".NET, codecs and every other library winetricks offers. Games usually need one of the "
            "<b>Recommended</b> ones when they say “… .dll not found”. Large downloads can take a "
            "while; the log below shows what the tool is doing."))

        self._hint = QLabel()
        self._hint.setWordWrap(True)
        self._hint.setTextFormat(Qt.RichText)
        self._hint.setStyleSheet(
            f"background:{T['accent_bg']}; border:1px solid {T['accent_dim']}; border-radius:8px;"
            f"color:{T['text']}; padding:8px 10px; font-size:12px;")
        lay.addWidget(self._hint)
        self._hint.setVisible(bool(self._suggest))
        if self._suggest:
            self._hint.setText("<b>Suggested from this game's log:</b><br>" + "<br>".join(
                f"• <b>{v}</b> — {w}" for v, w in self._suggest))
            self._checked.update(v for v, _w in self._suggest)

        row = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("search libraries…  (vcrun, d3dx9, dotnet, msxml …)")
        self._search.setStyleSheet(_LINEEDIT_QSS)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _t: self._rebuild())
        row.addWidget(self._search, 1)
        self._view = QComboBox()
        self._view.setStyleSheet(_COMBO_QSS)
        n_rec = len(winetricks.recommended())
        self._view.addItem(f"★ Recommended for games ({n_rec})", _V_REC)
        self._view.addItem(f"All libraries ({len(self._entries)})", _V_ALL)
        for c in winetricks.categories():
            self._view.addItem("   " + c["title"], c["id"])
        self._view.addItem("✓ Installed in this prefix", _V_INST)
        self._view.addItem("☑ Selected", _V_SEL)
        # a suggestion that is not on the recommended list must still be visible
        if any(not self._by_verb[v].get("pop") for v, _w in self._suggest):
            self._view.setCurrentIndex(1)
        self._view.currentIndexChanged.connect(lambda _i: self._rebuild())
        row.addWidget(self._view)
        lay.addLayout(row)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["Library", "What it is", "In this prefix"])
        self._tree.setStyleSheet(_pack2_table_ss())
        self._tree.setUniformRowHeights(True)
        self._tree.setRootIsDecorated(True)
        hh = self._tree.header()
        hh.setSectionResizeMode(_VERB, QHeaderView.Interactive)
        hh.setSectionResizeMode(_DESC, QHeaderView.Stretch)
        hh.setSectionResizeMode(_STATE, QHeaderView.ResizeToContents)
        self._tree.setColumnWidth(_VERB, 190)
        self._tree.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self._tree, 1)

        row = QHBoxLayout()
        self._count = QLabel()
        self._count.setStyleSheet(f"color:{T['text2']}; font-size:12px;")
        row.addWidget(self._count, 1)
        self._clear_btn = QPushButton("Clear selection")
        self._clear_btn.setStyleSheet(btn_ss("ghost", "sm"))
        self._clear_btn.setCursor(Qt.PointingHandCursor)
        self._clear_btn.clicked.connect(self._clear)
        row.addWidget(self._clear_btn)
        lay.addLayout(row)

        self._force = QCheckBox("Reinstall even if winetricks says it is already installed (--force)")
        self._force.setStyleSheet(f"color:{T['text2']};")
        self._force.toggled.connect(lambda _c: self._sync())
        lay.addWidget(self._force)

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:11px;")
        lay.addWidget(self._status)
        self._cmd = QLabel()
        self._cmd.setWordWrap(True)
        self._cmd.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._cmd.setStyleSheet(f"color:{T['text2']}; font-family:{T['mono']}; font-size:11px;")
        lay.addWidget(self._cmd)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(_MONO_BOX_QSS)
        self._log.setFixedHeight(130)
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
        self._rebuild()

    # ── list ────────────────────────────────────────────────────────────────
    def _visible(self) -> List[dict]:
        mode = self._view.currentData()
        q = self._search.text().strip().casefold()
        out = []
        for e in self._entries:
            v = e["verb"]
            if mode == _V_REC and not e.get("pop"):
                continue
            if mode == _V_INST and v not in self._installed:
                continue
            if mode == _V_SEL and v not in self._checked:
                continue
            if mode not in (_V_REC, _V_ALL, _V_INST, _V_SEL) and e.get("cat") != mode:
                continue
            if q and q not in v.casefold() and q not in e.get("desc", "").casefold() \
                    and q not in e.get("pop", "").casefold():
                continue
            out.append(e)
        return out

    def _rebuild(self):
        self._building = True
        self._tree.clear()
        self._items.clear()
        mode = self._view.currentData()
        entries = self._visible()
        groups: Dict[str, QTreeWidgetItem] = {}
        flat = mode != _V_ALL                          # a single list; only "All" is grouped
        for e in entries:
            parent = None
            if not flat:
                cid = e.get("cat", "")
                if cid not in groups:
                    g = QTreeWidgetItem([self._cat_title.get(cid, cid or "Other"), "", ""])
                    g.setFlags(Qt.ItemIsEnabled)
                    f = g.font(0)
                    f.setBold(True)
                    g.setFont(0, f)
                    self._tree.addTopLevelItem(g)
                    groups[cid] = g
                parent = groups[cid]
            it = QTreeWidgetItem([e["verb"], e.get("pop") or e.get("desc", ""), ""])
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            it.setCheckState(_VERB, Qt.Checked if e["verb"] in self._checked else Qt.Unchecked)
            tip = e.get("desc", "")
            if e.get("pop"):
                tip += f"\n\nCommon: {e['pop']}"
            if e.get("warn"):
                tip += f"\n\n⚠ {e['warn']}"
            it.setToolTip(_VERB, tip)
            it.setToolTip(_DESC, tip)
            self._paint(it, e)
            (parent.addChild if parent is not None else self._tree.addTopLevelItem)(it)
            self._items[e["verb"]] = it
        for g in groups.values():
            g.setExpanded(True)
            g.setText(_DESC, f"{g.childCount()}")
        self._building = False
        self._sync()

    def _paint(self, it: QTreeWidgetItem, e: dict):
        v = e["verb"]
        if v in self._installed:
            it.setText(_STATE, "✓ installed")
            it.setForeground(_STATE, QColor(T["accent"]))
        elif e.get("warn"):
            it.setText(_STATE, "⚠ see tooltip")
            it.setForeground(_STATE, QColor(T["warn"]))
        else:
            it.setText(_STATE, "")
        it.setForeground(_VERB, QColor(T["text"] if v not in self._installed else T["muted"]))

    def _on_item_changed(self, it: QTreeWidgetItem, col: int):
        if self._building or col != _VERB:
            return
        v = it.text(_VERB)
        if v not in self._by_verb:
            return
        (self._checked.add if it.checkState(_VERB) == Qt.Checked else self._checked.discard)(v)
        self._sync()

    def _clear(self):
        self._checked.clear()
        self._rebuild()

    # ── public helpers (also used by the tests) ─────────────────────────────
    def set_checked(self, verb: str, on: bool = True) -> bool:
        if verb not in self._by_verb:
            return False
        (self._checked.add if on else self._checked.discard)(verb)
        it = self._items.get(verb)
        if it is not None:
            self._building = True
            it.setCheckState(_VERB, Qt.Checked if on else Qt.Unchecked)
            self._building = False
        self._sync()
        return True

    def _selected(self) -> List[str]:
        return [e["verb"] for e in self._entries if e["verb"] in self._checked]    # catalog order

    # ── state ───────────────────────────────────────────────────────────────
    def _sync(self):
        busy = self._worker is not None and self._worker.isRunning()
        sel = self._selected()
        try:
            tool = winetricks.required_tool(self._prefix)
            found = winetricks.tool_path(self._prefix) is not None
        except Exception:                                # noqa: BLE001
            _log.debug("ignored error", exc_info=True)
            tool, found = "winetricks", False
        if found:
            self._status.setText(f"Using {tool} on {self._prefix}")
        else:
            self._status.setText(f"⚠ {tool} is not installed or not on PATH. Install it with your "
                                 "package manager, then reopen this window.")
        n_inst = sum(1 for v in sel if v in self._installed)
        self._count.setText(
            (f"{len(sel)} selected" + (f"  ·  {n_inst} already installed" if n_inst else "")) if sel
            else "Nothing selected — tick the libraries to install")
        if sel and n_inst and not self._force.isChecked():
            self._count.setText(self._count.text() + " (skipped by winetricks unless “reinstall” is ticked)")
        self._cmd.setText("")
        if sel and found:
            try:
                p = winetricks.plan(self._prefix, sel, self._force.isChecked())
                self._cmd.setText("$ " + " ".join(p.argv))
            except (ValueError, RuntimeError):
                pass
        self._run_btn.setEnabled(not busy and found and bool(sel))
        self._run_btn.setText(f"Install {len(sel)} selected" if sel else "Install selected")
        self._clear_btn.setEnabled(not busy and bool(sel))
        self._cancel_btn.setVisible(busy)
        for w in (self._tree, self._view, self._search, self._force):
            w.setEnabled(not busy)

    # ── run ─────────────────────────────────────────────────────────────────
    def _run(self):
        sel = self._selected()
        warns = [(v, self._by_verb[v]["warn"]) for v in sel if self._by_verb[v].get("warn")]
        if warns and QMessageBox.question(
                self, "Windows libraries",
                "Some of the selected libraries need care:\n\n"
                + "\n".join(f"• {v}: {w}" for v, w in warns[:8]) + "\n\nInstall anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            p = winetricks.plan(self._prefix, sel, self._force.isChecked())
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
        self._installed = winetricks.installed_verbs(self._prefix)
        if rc == 0:
            self._checked.difference_update(self._installed)
        for v, it in self._items.items():
            self._paint(it, self._by_verb[v])
        self._sync()

    def closeEvent(self, ev):                            # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        super().closeEvent(ev)

    def done(self, r):                                   # reject()/Close must stop a running install too
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        super().done(r)
