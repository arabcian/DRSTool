"""Dialogs: change plan / import preview, recipe gallery, changes, share-code import."""

import logging
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .base import _CORE_ERR, share_code
from .diag_panels import _SEV_STYLE
from .history import ProfileDiffTable
from .launch_import import (
    apply_ops, op_label, op_status, ops_from_parsed, parse_launch_text, ParsedLaunch,
)
from .launch_line import gamescope_arg_string
from .lint import lint_config
from .lookup import CUSTOM_ENV_CAT, drs_lookup, env_def
from .profile_io import lint_profile, unique_profile_name
from .recipes import Recipe, recipe_coverage, recipe_ops, recipe_requirement, RECIPES
from .state import copy_state, state_diff, state_payload
from .sysinfo import SYSHUB
from .theme import btn_ss, caption, T
from .tool_ui import _tool_btn
from .widgets import _COMBO_QSS, _LINEEDIT_QSS, _MONO_BOX_QSS, _pack2_table_ss, _pack2_title

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .settings_manager import SettingsManager


# ----------------------------------------------------------------------------
# Plan preview dialog (paste / explain / recipe)
# ----------------------------------------------------------------------------

_STATUS_STYLE = {"new": ("+ new", "accent"), "change": ("~ change", "warn"), "same": ("= same", "muted"),
                 "remove": ("− remove", "danger"), "absent": ("already absent", "muted")}
_NOTE_STYLE = {"warn": ("▲", "warn"), "info": ("ℹ", "muted"), "conv": ("⇄", "accent")}

_PRIMARY_BTN_SS = (f"QPushButton{{background:{T['accent']};border:1px solid {T['accent']};color:#0b0d10;"
                   f"border-radius:4px;font-weight:700;padding:5px 16px;}}"
                   f"QPushButton:hover{{background:{T['accent_hi']};}}"
                   f"QPushButton:disabled{{background:{T['raised']};color:{T['muted']};"
                   f"border-color:{T['border_hi']};}}")


class PlanDialog(QDialog):
    """Preview of a set of operations against the current editor state.

    mode="paste": text box on top, parsed live.  mode="explain": read-only
    breakdown of the current launch line.  mode="recipe": fixed operations."""

    def __init__(self, cur_state: dict, mode: str = "paste", title: str = "", subtitle: str = "",
                 ops: Optional[List[dict]] = None, text: str = "", gs_replace: bool = True,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(title or "Import launch line")
        self.resize(940, 680)
        self._cur = copy_state(cur_state)
        self._mode = mode
        self._ops: List[dict] = list(ops or [])
        self._parsed: Optional[ParsedLaunch] = None
        self.result_state: Optional[dict] = None
        self.result_label = title

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        lay.addWidget(_pack2_title(title or "Import a launch line", subtitle or (
            "Paste Steam launch options, a shell snippet, export lines or a bare DXVK_NVAPI_DRS_SETTINGS "
            "value. Nothing changes until you press Apply — and Apply is undoable (Ctrl+Z).")))

        self._text = QPlainTextEdit()
        self._text.setPlaceholderText('e.g.  PROTON_DLSS_UPGRADE=1 DXVK_NVAPI_DRS_SETTINGS=0x10E41DF3=0xffffff '
                                      'gamescope -W 2560 -H 1440 --hdr-enabled -- mangohud %command%')
        self._text.setStyleSheet(_MONO_BOX_QSS)
        self._text.setFixedHeight(92)
        self._text.setVisible(mode in ("paste", "explain"))
        self._text.setReadOnly(mode == "explain")
        if text:
            self._text.setPlainText(text)
        self._parse_timer = QTimer(self)
        self._parse_timer.setSingleShot(True)
        self._parse_timer.setInterval(180)
        self._parse_timer.timeout.connect(self._reparse)
        self._text.textChanged.connect(self._parse_timer.start)
        lay.addWidget(self._text)
        if mode == "paste":
            row = QHBoxLayout()
            b = _tool_btn("Paste from clipboard")
            b.clicked.connect(lambda: self._text.setPlainText(QApplication.clipboard().text()))
            row.addWidget(b)
            row.addStretch(1)
            lay.addLayout(row)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["Item", "Value", "Effect"])
        self._tree.setStyleSheet(_pack2_table_ss())
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self._tree.header()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._tree.setColumnWidth(0, 360)
        self._tree.itemChanged.connect(lambda *_: self._update_summary())
        self._tree.currentItemChanged.connect(self._show_desc)
        lay.addWidget(self._tree, 3)

        self._desc = QLabel("")
        self._desc.setWordWrap(True)
        self._desc.setStyleSheet(f"color:{T['text2']}; font-size:12px; padding:4px 2px;")
        self._desc.setMinimumHeight(36)
        lay.addWidget(self._desc)

        self._notes = QLabel("")
        self._notes.setWordWrap(True)
        self._notes.setTextFormat(Qt.RichText)
        self._notes.setStyleSheet(f"font-size:12px; background:{T['surface']}; border:1px solid "
                                  f"{T['border']}; border-radius:4px; padding:6px 8px;")
        lay.addWidget(self._notes)

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.setTextFormat(Qt.RichText)
        self._summary.setStyleSheet("font-size:12px;")
        lay.addWidget(self._summary)

        bottom = QHBoxLayout()
        self._mode_box = QComboBox()
        self._mode_box.addItems(["Merge into the current configuration", "Replace the whole configuration"])
        self._mode_box.setStyleSheet(_COMBO_QSS)
        self._mode_box.currentIndexChanged.connect(lambda _i: self._update_summary())
        self._gs_replace = QCheckBox("Replace existing gamescope flags")
        self._gs_replace.setChecked(gs_replace)
        self._gs_replace.setStyleSheet(f"color:{T['text2']};")
        self._gs_replace.toggled.connect(lambda _c: self._update_summary())
        bottom.addWidget(self._mode_box)
        bottom.addWidget(self._gs_replace)
        bottom.addStretch(1)
        self._copy_btn = _tool_btn("Copy explanation")
        self._copy_btn.clicked.connect(self._copy_explanation)
        bottom.addWidget(self._copy_btn)
        cancel = _tool_btn("Close" if mode == "explain" else "Cancel")
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        self._apply = QPushButton("Apply")
        self._apply.setStyleSheet(_PRIMARY_BTN_SS)
        self._apply.setCursor(Qt.PointingHandCursor)
        self._apply.clicked.connect(self._on_apply)
        bottom.addWidget(self._apply)
        if mode == "explain":
            for w in (self._mode_box, self._gs_replace, self._apply):
                w.hide()
        lay.addLayout(bottom)

        if mode in ("paste", "explain"):
            self._reparse()
        else:
            self._fill([], self._ops)
        if mode == "paste":
            self._text.setFocus()

    # -- data ----------------------------------------------------------------
    def _reparse(self):
        self._parsed = parse_launch_text(self._text.toPlainText())
        self._fill(self._parsed.notes, ops_from_parsed(self._parsed))
        has_gs = self._parsed.gs_on
        self._gs_replace.setEnabled(has_gs)

    def _fill(self, notes: List[tuple], ops: List[dict]):
        self._ops = ops
        self._tree.blockSignals(True)
        self._tree.clear()
        groups: Dict[str, QTreeWidgetItem] = {}
        order = {"arch": "GPU arch", "drs": "DRS settings", "drs_unset": "DRS settings",
                 "env": "Environment", "env_unset": "Environment", "gs_on": "Gamescope", "gs_flag": "Gamescope"}
        for o in ops:
            gname = order[o["kind"]]
            if o["kind"] in ("env", "env_unset") and env_def(o["key"]) and \
                    env_def(o["key"]).cat == CUSTOM_ENV_CAT:
                gname = "Environment — not in catalog"
            if o["kind"] == "drs" and drs_lookup(o["key"]) is None:
                gname = "DRS settings — custom IDs"
            g = groups.get(gname)
            if g is None:
                g = QTreeWidgetItem([gname, "", ""])
                g.setFlags(Qt.ItemIsEnabled)
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                g.setForeground(0, QColor(T["muted"]))
                self._tree.addTopLevelItem(g)
                groups[gname] = g
            name, shown, desc = op_label(o)
            st = op_status(o, self._cur)
            stxt, scol = _STATUS_STYLE[st]
            it = QTreeWidgetItem([name, shown, stxt])
            it.setData(0, Qt.UserRole, o)
            it.setData(1, Qt.UserRole, desc)
            it.setToolTip(0, desc)
            it.setForeground(2, QColor(T[scol]))
            if self._mode != "explain":
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                it.setCheckState(0, Qt.Checked)
            if st in ("same", "absent"):
                it.setForeground(0, QColor(T["muted"]))
                it.setForeground(1, QColor(T["muted"]))
            g.addChild(it)
        self._tree.expandAll()
        self._tree.blockSignals(False)
        html = []
        for sev, txt in notes:
            icon, col = _NOTE_STYLE.get(sev, ("ℹ", "muted"))
            html.append(f'<span style="color:{T[col]}">{icon}</span>&nbsp;'
                        f'<span style="color:{T["text2"]}">{_html_esc(txt)}</span>')
        self._notes.setText("<br>".join(html))
        self._notes.setVisible(bool(html))
        if not ops:
            empty = ("Nothing recognisable yet." if self._mode != "recipe"
                     else "This recipe has nothing to apply on this system.")
            self._summary.setText(f'<span style="color:{T["muted"]}">{empty}</span>')
            self._apply.setEnabled(False)
            return
        self._update_summary()

    def _checked_ops(self) -> List[dict]:
        out = []
        for gi in range(self._tree.topLevelItemCount()):
            g = self._tree.topLevelItem(gi)
            for ci in range(g.childCount()):
                it = g.child(ci)
                if self._mode == "explain" or it.checkState(0) == Qt.Checked:
                    out.append(it.data(0, Qt.UserRole))
        return out

    def _result(self) -> dict:
        return apply_ops(self._cur, self._checked_ops(), self._mode_box.currentIndex() == 1,
                         self._gs_replace.isChecked() and self._gs_replace.isEnabled())

    def _update_summary(self):
        if self._mode == "explain":
            n = len(self._ops)
            self._summary.setText(f'<span style="color:{T["muted"]}">{n} item(s) in this launch line. '
                                  'Select a row to read what it does.</span>')
            return
        res = self._result()
        rows = state_diff(self._cur, res)
        add = sum(1 for r in rows if r[2] is None)
        rem = sum(1 for r in rows if r[3] is None)
        chg = len(rows) - add - rem
        try:
            p = state_payload(res)
            issues = lint_config(p["env"], p["drs"], p["arch"], p["gs_on"], p["gs_flags"],
                                 p["mangohud"], SYSHUB.info)
            q = state_payload(self._cur)
            before = lint_config(q["env"], q["drs"], q["arch"], q["gs_on"], q["gs_flags"],
                                 q["mangohud"], SYSHUB.info)
        except Exception:
            _log.debug("ignored error", exc_info=True)
            issues, before = [], []
        before_codes = {i.code for i in before}
        fresh = [i for i in issues if i.code not in before_codes and i.sev in ("error", "warn")]
        gone = [i for i in before if i.code not in {x.code for x in issues} and i.sev in ("error", "warn")]
        parts = [f'<b style="color:{T["text"]}">Result:</b> '
                 f'<span style="color:{T["accent"]}">+{add} added</span> · '
                 f'<span style="color:{T["warn"]}">~{chg} changed</span> · '
                 f'<span style="color:{T["danger"]}">−{rem} removed</span>']
        if fresh:
            parts.append(f'<b style="color:{T["warn"]}">Lint forecast — new findings:</b>')
            for i in fresh[:5]:
                icon, col = _SEV_STYLE[i.sev]
                parts.append(f'<span style="color:{col}">{icon}</span> '
                             f'<span style="color:{T["text2"]}">{_html_esc(i.msg)}</span>')
        elif gone:
            parts.append(f'<span style="color:{T["accent"]}">✔ Lint forecast: resolves '
                         f'{len(gone)} existing finding(s), adds none.</span>')
        else:
            parts.append(f'<span style="color:{T["muted"]}">✔ Lint forecast: no new findings.</span>')
        self._summary.setText("<br>".join(parts))
        self._apply.setEnabled(bool(rows))
        self._apply.setText("Apply" if rows else "Nothing to change")

    def _show_desc(self, cur, _prev=None):
        if cur is None or cur.data(0, Qt.UserRole) is None:
            self._desc.setText("")
            return
        d = cur.data(1, Qt.UserRole) or ""
        self._desc.setText(f"<b>{_html_esc(cur.text(0))}</b> — {_html_esc(d)}" if d else
                           f"<b>{_html_esc(cur.text(0))}</b>")

    def _copy_explanation(self):
        lines = []
        for gi in range(self._tree.topLevelItemCount()):
            g = self._tree.topLevelItem(gi)
            lines.append(f"## {g.text(0)}")
            for ci in range(g.childCount()):
                it = g.child(ci)
                d = it.data(1, Qt.UserRole) or ""
                lines.append(f"- {it.text(0)} = {it.text(1)}" + (f"\n    {d}" if d else ""))
        QApplication.clipboard().setText("\n".join(lines))
        self._copy_btn.setText("Copied ✔")
        QTimer.singleShot(1500, lambda: self._copy_btn.setText("Copy explanation"))

    def _on_apply(self):
        res = self._result()
        if self._mode_box.currentIndex() == 1:
            rem = sum(1 for r in state_diff(self._cur, res) if r[3] is None)
            if rem and QMessageBox.question(
                    self, "Replace configuration",
                    f"Replace mode removes {rem} item(s) that are not in this import.\n"
                    "(You can undo it with Ctrl+Z.)\n\nContinue?") != QMessageBox.Yes:
                return
        self.result_state = res
        self.accept()


def _html_esc(s: str) -> str:
    import html as _h
    return _h.escape(str(s))


# ----------------------------------------------------------------------------
# Recipe gallery
# ----------------------------------------------------------------------------

class RecipeGallery(QDialog):
    def __init__(self, cur_state: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recipes")
        self.resize(900, 560)
        self._cur = copy_state(cur_state)
        self.chosen: Optional[Recipe] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        lay.addWidget(_pack2_title(
            "Recipes", "Curated bundles of settings for common goals. Each one opens a preview with a "
            "lint forecast before anything changes; every value comes from DRSTool's own catalog."))
        body = QHBoxLayout()
        self._list = QListWidget()
        self._list.setStyleSheet(
            f"QListWidget{{background:{T['bg']};border:1px solid {T['border']};border-radius:5px;outline:none;}}"
            f"QListWidget::item{{padding:7px 8px;color:{T['text2']};border-bottom:1px solid {T['border']};}}"
            f"QListWidget::item:selected{{background:rgba(118,185,0,0.18);color:{T['text']};}}")
        self._list.setFixedWidth(330)
        for r in RECIPES:
            done, total, full = recipe_coverage(r, self._cur)
            why = recipe_requirement(r)
            mark = "✔ " if full else "◐ " if done else "   "
            li = QListWidgetItem(f"{mark}{r.title}\n      {' · '.join(r.tags)}"
                                 + ("   ⚠" if why else ""))
            li.setData(Qt.UserRole, r)
            if why:
                li.setForeground(QColor(T["muted"]))
            self._list.addItem(li)
        self._list.currentItemChanged.connect(self._show)
        self._list.itemDoubleClicked.connect(lambda _i: self._choose())
        body.addWidget(self._list)
        right = QVBoxLayout()
        self._title = QLabel()
        self._title.setStyleSheet(f"color:{T['text']}; font-size:16px; font-weight:700;")
        self._req = QLabel()
        self._req.setWordWrap(True)
        self._body = QLabel()
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.RichText)
        self._body.setStyleSheet(f"color:{T['text2']}; font-size:13px;")
        self._items = QLabel()
        self._items.setWordWrap(True)
        self._items.setTextFormat(Qt.RichText)
        self._items.setStyleSheet(f"font-family:{T['mono']}; font-size:12px; background:{T['surface']};"
                                  f"border:1px solid {T['border']}; border-radius:4px; padding:8px;")
        for w in (self._title, self._req, self._body, self._items):
            right.addWidget(w)
        right.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        close = _tool_btn("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        self._go = QPushButton("Preview && apply…")
        self._go.setStyleSheet(_PRIMARY_BTN_SS)
        self._go.clicked.connect(self._choose)
        row.addWidget(self._go)
        right.addLayout(row)
        body.addLayout(right, 1)
        lay.addLayout(body, 1)
        self._list.setCurrentRow(0)

    def _show(self, cur, _prev=None):
        if cur is None:
            return
        r: Recipe = cur.data(Qt.UserRole)
        self._title.setText(r.title)
        why = recipe_requirement(r)
        if why:
            self._req.setText(f"⚠ {why} — you can still apply it (e.g. for another PC).")
            self._req.setStyleSheet(f"color:{T['warn']}; font-size:12px;")
        else:
            self._req.setText("✔ Requirements met (or not detectable here)")
            self._req.setStyleSheet(f"color:{T['accent']}; font-size:12px;")
        self._body.setText(f"<p>{_html_esc(r.summary)}</p><p>{r.details}</p>")
        rows = []
        for o in recipe_ops(r):
            name, shown, _d = op_label(o)
            st = op_status(o, self._cur)
            stxt, scol = _STATUS_STYLE[st]
            rows.append(f'<span style="color:{T["text"]}">{_html_esc(name)}</span> = '
                        f'<span style="color:{T["accent"]}">{_html_esc(shown)}</span> '
                        f'<span style="color:{T[scol]}">({stxt})</span>')
        self._items.setText("<br>".join(rows) or "—")

    def _choose(self):
        it = self._list.currentItem()
        if it:
            self.chosen = it.data(Qt.UserRole)
            self.accept()


# ----------------------------------------------------------------------------
# Changes dialog (unsaved changes / compare a profile with the editor)
# ----------------------------------------------------------------------------

class ChangesDialog(QDialog):
    def __init__(self, a: dict, b: dict, label_a: str, label_b: str, title: str,
                 revert_text: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 480)
        self.revert = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        table = ProfileDiffTable()
        n = table.show_diff(a, b, label_a, label_b)
        lay.addWidget(_pack2_title(title, f"{n} difference(s)." if n else "No differences."))
        lay.addWidget(table, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        if revert_text and n:
            rb = _tool_btn(revert_text, fg=T["warn"])
            rb.clicked.connect(lambda: (setattr(self, "revert", True), self.accept()))
            row.addWidget(rb)
        cb = _tool_btn("Close")
        cb.clicked.connect(self.reject)
        row.addWidget(cb)
        lay.addLayout(row)


class ShareImportDialog(QDialog):
    """Paste a share code → see what it contains and how it fits this machine → save it."""

    def __init__(self, settings_manager: "SettingsManager", text: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import a shared profile")
        self.resize(760, 640)
        self._sm = settings_manager
        self._share = None
        self.saved_name: Optional[str] = None
        self.load_after = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        lay.addWidget(_pack2_title("Import a shared profile",
                                   "Paste a code that starts with DRSTOOL1: — from a forum post, a chat or a "
                                   "friend. Nothing is saved until you press Save."))
        self._text = QPlainTextEdit()
        self._text.setPlaceholderText("DRSTOOL1:eNq…")
        self._text.setStyleSheet(_MONO_BOX_QSS)
        self._text.setFixedHeight(84)
        lay.addWidget(self._text)
        row = QHBoxLayout()
        b = _tool_btn("Paste from clipboard")
        b.clicked.connect(lambda: self._text.setPlainText(QApplication.clipboard().text()))
        row.addWidget(b)
        self._state = QLabel("")
        self._state.setWordWrap(True)
        row.addWidget(self._state, 1)
        lay.addLayout(row)

        self._summary = QLabel("")
        self._summary.setTextFormat(Qt.RichText)
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(f"color:{T['text2']}; font-size:12px;")
        lay.addWidget(self._summary)

        lay.addWidget(caption("Content"))
        self._content = QPlainTextEdit()
        self._content.setReadOnly(True)
        self._content.setStyleSheet(_MONO_BOX_QSS)
        lay.addWidget(self._content, 1)

        lay.addWidget(caption("Check against this machine"))
        self._lint = QLabel("")
        self._lint.setTextFormat(Qt.RichText)
        self._lint.setWordWrap(True)
        lay.addWidget(self._lint)

        self._flag_box = QCheckBox("")
        self._flag_box.setStyleSheet(f"color:{T['warn']};")
        self._flag_box.toggled.connect(lambda _c: self._render())
        lay.addWidget(self._flag_box)

        nrow = QHBoxLayout()
        nrow.addWidget(QLabel("Save as:"))
        self._name = QLineEdit()
        self._name.setStyleSheet(_LINEEDIT_QSS)
        nrow.addWidget(self._name, 1)
        lay.addLayout(nrow)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = _tool_btn("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self._save = _tool_btn("Save as profile")
        self._save.clicked.connect(lambda: self._accept(False))
        btns.addWidget(self._save)
        self._save_load = QPushButton("Save && load")
        self._save_load.setStyleSheet(btn_ss("primary"))
        self._save_load.clicked.connect(lambda: self._accept(True))
        btns.addWidget(self._save_load)
        lay.addLayout(btns)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._decode)
        self._text.textChanged.connect(self._timer.start)
        if _CORE_ERR:
            self._state.setText(f'<span style="color:{T["danger"]}">{_html_esc(_CORE_ERR)}</span>')
            self._text.setEnabled(False)
        elif text:
            self._text.setPlainText(text)
        self._decode()

    def _decode(self):
        self._share = None
        txt = self._text.toPlainText().strip()
        if _CORE_ERR or not txt:
            if not _CORE_ERR:
                self._state.setText("")
            self._render()
            return
        try:
            self._share = share_code.decode(txt, home=str(Path.home()))
        except share_code.ShareCodeError as ex:
            self._state.setText(f'<span style="color:{T["danger"]}">✖ {_html_esc(str(ex))}</span>')
            self._render()
            return
        self._state.setText(f'<span style="color:{T["accent"]}">✓ valid share code</span>')
        self._name.setText(unique_profile_name(self._share.name, self._sm.get_profiles()))
        self._flag_box.blockSignals(True)
        self._flag_box.setChecked(False)
        self._flag_box.blockSignals(False)
        self._render()

    def _profile(self) -> Optional[dict]:
        if self._share is None:
            return None
        return self._share.profile if self._flag_box.isChecked() else self._share.profile_without_flagged()

    def _render(self):
        sh = self._share
        ok = sh is not None
        for w in (self._save, self._save_load, self._name):
            w.setEnabled(ok)
        if not ok:
            self._summary.setText("")
            self._content.setPlainText("")
            self._lint.setText("")
            self._flag_box.setVisible(False)
            return
        self._summary.setText("<br>".join(_html_esc(s) for s in share_code.summary_lines(sh)))
        p = sh.profile
        lines = []
        for k, v in p["settings"].items():
            s = drs_lookup(k)
            lines.append(f"DRS  {k}={v}" + (f"   # {s.name}" if s else ""))
        for k, v in p["env_vars"].items():
            mark = "  ⚠ " + sh.flagged[k] if k in sh.flagged else ("" if env_def(k) else "   (not in catalog)")
            lines.append(f"ENV  {k}={v}{mark}")
        if p["gamescope_enabled"]:
            lines.append("GAMESCOPE  " + (gamescope_arg_string(p["gamescope_flags"]) or "(no flags)"))
        for d in sh.dropped:
            lines.append(f"skipped: {d}")
        self._content.setPlainText("\n".join(lines))
        if sh.flagged:
            self._flag_box.setText(f"Also import {len(sh.flagged)} flagged variable(s): "
                                   + ", ".join(sorted(sh.flagged)) + " — only if you trust the sender")
        self._flag_box.setVisible(bool(sh.flagged))
        try:
            issues = lint_profile(self._profile(), SYSHUB.info)
        except Exception as ex:                       # lint must never block an import
            self._lint.setText(f'<span style="color:{T["muted"]}">lint unavailable: {_html_esc(str(ex))}</span>')
            return
        if not issues:
            self._lint.setText(f'<span style="color:{T["accent"]}">✓ no conflicts found for this system</span>')
            return
        col = {"error": T["danger"], "warn": T["warn"]}
        self._lint.setText("<br>".join(
            f'<span style="color:{col.get(i.sev, T["muted"])}">{i.sev.upper()}</span>&nbsp;'
            f'{_html_esc(i.msg)}' for i in issues[:8]) + (f"<br>… {len(issues) - 8} more" if len(issues) > 8 else ""))

    def _accept(self, load: bool):
        prof = self._profile()
        name = self._name.text().strip()
        if prof is None or not name:
            return
        if name in self._sm.get_profiles():
            if QMessageBox.question(self, "Import shared profile", f'Replace the existing profile "{name}"?',
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            self._sm.restore_profile(name, prof)
            self.saved_name = name
        else:
            added = self._sm.import_profiles({name: prof})
            self.saved_name = added[0] if added else None
        self.load_after = load
        if self.saved_name:
            self.accept()
