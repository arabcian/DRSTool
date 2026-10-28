"""Profile history, config backups and profile comparison."""

import difflib
import glob
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget,
)

from .base import _fmt_bytes
from .extra_tools import ExtraToolsWidget
from .lookup import _SETTING_BY_ID
from .lutris_sync import LutrisSyncWidget
from .settings_manager import SettingsManager
from .state import diff_profiles
from .theme import T
from .tool_ui import _tool_btn
from .widgets import _fmt_drs_value, _LAUNCHER_QSS, _pack2_table_ss, _pack2_title


class ProfileDiffTable(QTableWidget):
    def __init__(self):
        super().__init__(0, 4)
        self.setHorizontalHeaderLabels(["Section", "Key", "A", "B"])
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.setStyleSheet(_pack2_table_ss())

    @staticmethod
    def _label(section: str, key: str, val):
        if section == "DRS":
            s = _SETTING_BY_ID.get(key)
            shown_key = f"{s.name}  ({key})" if s else key
            if val is not None and s is not None:
                try:
                    return shown_key, f"{_fmt_drs_value(s, val)}  [{val}]" if _fmt_drs_value(s, val) != val else val
                except Exception:
                    pass
            return shown_key, val
        return key, val

    def show_diff(self, a, b, label_a: str, label_b: str) -> int:
        rows = diff_profiles(a, b)
        self.setHorizontalHeaderLabels(["Section", "Key", label_a, label_b])
        self.setRowCount(0)
        for r, (sec, key, va, vb) in enumerate(rows):
            self.insertRow(r)
            shown_key, _ = self._label(sec, key, None)
            _, sa = self._label(sec, key, va)
            _, sb = self._label(sec, key, vb)
            col = (T["accent"] if va is None else T["danger"] if vb is None else T["warn"])
            cells = [sec, shown_key, "—" if sa is None else sa, "—" if sb is None else sb]
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                if c >= 2:
                    it.setForeground(QColor(col))
                self.setItem(r, c, it)
        return len(rows)


# ----------------------------------------------------------------------------
# History widget: profile snapshots, config-file backups, profile compare
# ----------------------------------------------------------------------------

def _diff_html(old_lines: List[str], new_lines: List[str], from_label: str, to_label: str) -> str:
    import html as _html
    out = []
    for ln in difflib.unified_diff(old_lines, new_lines, from_label, to_label, lineterm="", n=2):
        esc = _html.escape(ln.rstrip("\n"))
        if ln.startswith("+++") or ln.startswith("---"):
            col = T["muted"]
        elif ln.startswith("@@"):
            col = "#6aa0d8"
        elif ln.startswith("+"):
            col = T["accent"]
        elif ln.startswith("-"):
            col = T["danger"]
        else:
            col = T["text2"]
        out.append(f'<span style="color:{col}">{esc or "&nbsp;"}</span>')
    if not out:
        return f'<span style="color:{T["muted"]}">No differences.</span>'
    return ('<pre style="font-family:monospace;font-size:12px;margin:0">'
            + "<br>".join(out) + "</pre>")


class HistoryWidget(QWidget):
    def __init__(self, settings_manager: "SettingsManager"):
        super().__init__()
        self.sm = settings_manager
        self._snap_data: Dict = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        tabs = QTabWidget()
        tabs.setStyleSheet(ExtraToolsWidget._TAB_SS)
        tabs.addTab(self._build_snapshots(), "Profile history")
        tabs.addTab(self._build_files(), "Config backups")
        tabs.addTab(self._build_compare(), "Compare profiles")
        lay.addWidget(tabs)
        self.sm.profiles_changed.connect(self._on_profiles_changed)
        self._reload_snapshots()
        self._scan_lutris()

    # ── Profile history ───────────────────────────────────────────────────
    def _build_snapshots(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_pack2_title(
            "Profile history",
            "Every time profiles.json is overwritten (save, overwrite, delete, restore) the previous "
            "state is kept as a snapshot. Pick a snapshot, pick a profile inside it, and compare it "
            "with the live profile of the same name."))
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("Snapshots (newest first)"))
        self._snap_list = QListWidget()
        self._snap_list.setStyleSheet(_LAUNCHER_QSS)
        self._snap_list.currentRowChanged.connect(self._snap_selected)
        lv.addWidget(self._snap_list, 1)
        lv.addWidget(QLabel("Profiles in snapshot   (= same · ≠ differs · ＋ deleted since)"))
        self._snap_profiles = QListWidget()
        self._snap_profiles.setStyleSheet(_LAUNCHER_QSS)
        self._snap_profiles.currentRowChanged.connect(self._snap_profile_selected)
        lv.addWidget(self._snap_profiles, 1)
        split.addWidget(left)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self._snap_summary = QLabel("")
        self._snap_summary.setStyleSheet(f"color:{T['muted']};font-size:12px;")
        rv.addWidget(self._snap_summary)
        self._snap_diff = ProfileDiffTable()
        rv.addWidget(self._snap_diff, 1)
        row = QHBoxLayout()
        self._btn_restore_one = _tool_btn("Restore this profile", bold=True)
        self._btn_restore_one.clicked.connect(self._restore_one)
        self._btn_restore_all = _tool_btn("Restore whole snapshot")
        self._btn_restore_all.clicked.connect(self._restore_all)
        b_open = _tool_btn("Open history folder")
        b_open.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(SettingsManager._history_dir()))))
        for b in (self._btn_restore_one, self._btn_restore_all, b_open):
            row.addWidget(b)
        row.addStretch(1)
        rv.addLayout(row)
        split.addWidget(right)
        split.setChildrenCollapsible(False)
        split.setSizes([320, 560])
        v.addWidget(split, 1)
        return w

    def refresh(self):
        self._reload_snapshots()
        self._scan_lutris()

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        if getattr(self, "_stale", False):
            self._on_profiles_changed()

    def _on_profiles_changed(self):
        # Every profile save re-read and JSON-parsed up to 60 snapshot files on
        # the GUI thread even with the History tab hidden; defer until shown.
        if not self.isVisible():
            self._stale = True
            return
        self._stale = False
        self._reload_snapshots()
        self._refresh_compare_combos()

    def _reload_snapshots(self):
        cur = self._snap_list.currentItem()
        cur_path = cur.data(Qt.UserRole) if cur else None
        self._snap_list.blockSignals(True)
        self._snap_list.clear()
        for p in self.sm.list_snapshots():
            n = ""
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                n = f" · {len(data)} profile(s)" if isinstance(data, dict) else " · unreadable"
            except Exception:
                n = " · unreadable"
            it = QListWidgetItem(f"{self._stamp_of(p)}{n}")
            it.setData(Qt.UserRole, p)
            self._snap_list.addItem(it)
            if cur_path is not None and p == cur_path:
                self._snap_list.setCurrentItem(it)
        self._snap_list.blockSignals(False)
        if self._snap_list.count() == 0:
            self._snap_list.addItem(QListWidgetItem("(no snapshots yet — they appear after the next save)"))
            self._snap_list.item(0).setFlags(Qt.NoItemFlags)
        if self._snap_list.currentRow() < 0 and self._snap_list.count() and \
                self._snap_list.item(0).flags() != Qt.NoItemFlags:
            self._snap_list.setCurrentRow(0)
        else:
            self._snap_selected(self._snap_list.currentRow())

    @staticmethod
    def _stamp_of(p: Path) -> str:
        m = re.match(r"profiles-(\d{8})-(\d{6})", p.name)
        if not m:
            return p.name
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").strftime(
                "%Y-%m-%d %H:%M:%S  (replaced)")
        except ValueError:
            return p.name

    def _snap_selected(self, _row: int):
        self._snap_profiles.blockSignals(True)
        self._snap_profiles.clear()
        self._snap_diff.setRowCount(0)
        self._snap_data = {}
        it = self._snap_list.currentItem()
        p = it.data(Qt.UserRole) if it else None
        if p is not None:
            try:
                data = json.loads(Path(p).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._snap_data = data
            except Exception:
                pass
        live = self.sm.get_profiles()
        for name in sorted(self._snap_data, key=str.lower):
            if name not in live:
                mark = "＋"
            elif diff_profiles(self._snap_data[name], live[name]):
                mark = "≠"
            else:
                mark = "="
            li = QListWidgetItem(f"{mark}  {name}")
            li.setData(Qt.UserRole, name)
            self._snap_profiles.addItem(li)
        self._snap_profiles.blockSignals(False)
        self._snap_summary.setText(
            "" if self._snap_data else "Select a snapshot.")
        self._btn_restore_all.setEnabled(bool(self._snap_data))
        self._btn_restore_one.setEnabled(False)

    def _snap_profile_selected(self, _row: int):
        it = self._snap_profiles.currentItem()
        if it is None:
            self._btn_restore_one.setEnabled(False)
            return
        name = it.data(Qt.UserRole)
        live = self.sm.get_profiles().get(name)
        n = self._snap_diff.show_diff(self._snap_data.get(name), live, "Snapshot", "Live now")
        self._snap_summary.setText(
            f'"{name}": {n} difference(s)' if live is not None
            else f'"{name}" no longer exists in the live profiles (deleted since).')
        self._btn_restore_one.setEnabled(True)

    def _restore_one(self):
        it = self._snap_profiles.currentItem()
        if it is None:
            return
        name = it.data(Qt.UserRole)
        if QMessageBox.question(
                self, "Restore profile",
                f'Replace the live profile "{name}" with the snapshot version?\n'
                "The current state is kept as a new snapshot first.") != QMessageBox.Yes:
            return
        self.sm.restore_profile(name, self._snap_data.get(name))

    def _restore_all(self):
        if not self._snap_data:
            return
        if QMessageBox.question(
                self, "Restore snapshot",
                f"Replace ALL live profiles with this snapshot ({len(self._snap_data)} profile(s))?\n"
                "The current state is kept as a new snapshot first.") != QMessageBox.Yes:
            return
        self.sm.restore_all_profiles(self._snap_data)

    # ── Config backups (Lutris YAML / any file with .bak siblings) ──────────
    def _build_files(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_pack2_title(
            "Config file backups",
            "Browse the timestamped .bak files DRSTool wrote next to Lutris game YAMLs (and Steam / "
            "Heroic / Bottles files). One backup selected: shows what restoring it would change. "
            "Two selected: diff between them. Restoring keeps the current file as a new backup."))
        row = QHBoxLayout()
        self._file_combo = QComboBox()
        self._file_combo.setMinimumWidth(300)
        self._file_combo.currentIndexChanged.connect(self._file_selected)
        row.addWidget(self._file_combo, 1)
        b_scan = _tool_btn("Rescan Lutris")
        b_scan.clicked.connect(self._scan_lutris)
        b_other = _tool_btn("Other file…")
        b_other.clicked.connect(self._pick_other_file)
        row.addWidget(b_scan)
        row.addWidget(b_other)
        v.addLayout(row)
        split = QSplitter(Qt.Horizontal)
        self._bak_list = QListWidget()
        self._bak_list.setSelectionMode(QListWidget.ExtendedSelection)
        self._bak_list.setStyleSheet(_LAUNCHER_QSS)
        self._bak_list.itemSelectionChanged.connect(self._bak_selection)
        split.addWidget(self._bak_list)
        self._bak_diff = QTextEdit()
        self._bak_diff.setReadOnly(True)
        self._bak_diff.setStyleSheet(
            f"QTextEdit{{background:{T['bg']};border:1px solid {T['border_hi']};border-radius:4px;}}")
        split.addWidget(self._bak_diff)
        split.setChildrenCollapsible(False)
        split.setSizes([300, 580])
        v.addWidget(split, 1)
        r2 = QHBoxLayout()
        self._btn_bak_restore = _tool_btn("Restore selected backup", bold=True)
        self._btn_bak_restore.setEnabled(False)
        self._btn_bak_restore.clicked.connect(self._restore_backup)
        r2.addWidget(self._btn_bak_restore)
        self._bak_status = QLabel("")
        self._bak_status.setStyleSheet(f"color:{T['muted']};font-size:12px;")
        r2.addWidget(self._bak_status, 1)
        v.addLayout(r2)
        return w

    def _lutris_dir(self) -> Path:
        win = self.window()
        d = getattr(getattr(win, "_lutris_sync_widget", None), "_games_dir", None)
        return Path(d) if d else LutrisSyncWidget._default_games_dir()

    @staticmethod
    def _backups_of(target: Path) -> List[Path]:
        try:
            baks = [p for p in target.parent.glob(glob.escape(target.name) + ".*")
                    if p.name.endswith(".bak") and p.is_file()]
        except OSError:
            return []
        return sorted(baks, key=lambda p: p.stat().st_mtime, reverse=True)

    def _scan_lutris(self):
        keep = self._file_combo.currentData()
        self._file_combo.blockSignals(True)
        self._file_combo.clear()
        d = self._lutris_dir()
        files = sorted(list(d.glob("*.yml")) + list(d.glob("*.yaml"))) if d.is_dir() else []
        for p in files:
            n = len(self._backups_of(p))
            self._file_combo.addItem(f"{p.name}   ({n} backup{'s' if n != 1 else ''})", str(p))
        self._file_combo.blockSignals(False)
        if keep:
            i = self._file_combo.findData(keep)
            if i >= 0:
                self._file_combo.setCurrentIndex(i)
        self._file_selected()

    def _pick_other_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "File whose backups to browse", str(Path.home()))
        if path:
            i = self._file_combo.findData(path)
            if i < 0:
                self._file_combo.addItem(path, path)
                i = self._file_combo.count() - 1
            self._file_combo.setCurrentIndex(i)

    def _cur_file(self) -> Optional[Path]:
        d = self._file_combo.currentData()
        return Path(d) if d else None

    def _file_selected(self, *_):
        self._bak_list.clear()
        self._bak_diff.clear()
        f = self._cur_file()
        if f is None:
            self._bak_status.setText(f"No game configs found in {self._lutris_dir()}")
            return
        baks = self._backups_of(f)
        for p in baks:
            st = p.stat()
            tail = p.name[len(f.name) + 1:]
            it = QListWidgetItem(f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}  ·  "
                                 f"{_fmt_bytes(st.st_size)}  ·  {tail}")
            it.setData(Qt.UserRole, p)
            self._bak_list.addItem(it)
        self._bak_status.setText(f"{len(baks)} backup(s) of {f.name}" if baks
                                 else f"No backups of {f.name} yet.")
        self._bak_selection()

    @staticmethod
    def _read_lines(p: Path) -> List[str]:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()

    def _bak_selection(self):
        sel = [i.data(Qt.UserRole) for i in self._bak_list.selectedItems()]
        f = self._cur_file()
        self._btn_bak_restore.setEnabled(len(sel) == 1)
        if f is None or not sel:
            self._bak_diff.clear()
            return
        try:
            if len(sel) == 1:
                cur = self._read_lines(f) if f.exists() else []
                html_ = _diff_html(cur, self._read_lines(sel[0]), f"{f.name} (current)",
                                   f"{sel[0].name} (after restore)")
            else:
                a, b = sorted(sel[:2], key=lambda p: p.stat().st_mtime)
                html_ = _diff_html(self._read_lines(a), self._read_lines(b), a.name, b.name)
        except OSError as e:
            html_ = f"Could not read: {e}"
        self._bak_diff.setHtml(html_)

    def _restore_backup(self):
        sel = self._bak_list.selectedItems()
        f = self._cur_file()
        if len(sel) != 1 or f is None:
            return
        bak: Path = sel[0].data(Qt.UserRole)
        if QMessageBox.question(
                self, "Restore backup",
                f"Restore {bak.name}\ninto {f}?\n\nThe current file is saved as a new .bak first.") != QMessageBox.Yes:
            return
        try:
            target = Path(os.path.realpath(f))
            if target.exists():
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")   # same-second restores must not overwrite
                shutil.copy2(target, target.with_name(f"{target.name}.{stamp}.pre-restore.bak"))
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as out, open(bak, "rb") as src:
                    shutil.copyfileobj(src, out)
                    out.flush()
                    os.fsync(out.fileno())
                shutil.copymode(target if target.exists() else bak, tmp)
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            QMessageBox.critical(self, "Restore failed", str(e))
            return
        self._bak_status.setText(f"Restored {bak.name}")
        self._file_selected()
        win = self.window()
        lw = getattr(win, "_lutris_sync_widget", None)
        if lw is not None and hasattr(lw, "_on_game_selected"):
            try:
                lw._on_game_selected(lw._game_combo.currentIndex())
            except Exception:
                pass

    # ── Compare profiles ──────────────────────────────────────────────────
    def _build_compare(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(_pack2_title(
            "Compare two profiles",
            "Shows only the differences in DRS settings, GPU arch, env vars and gamescope flags. "
            "“Current editor state” is what the output bar would produce right now."))
        row = QHBoxLayout()
        self._cmp_a = QComboBox()
        self._cmp_b = QComboBox()
        for c in (self._cmp_a, self._cmp_b):
            c.setMinimumWidth(200)
            c.currentIndexChanged.connect(self._update_compare)
        row.addWidget(QLabel("A"))
        row.addWidget(self._cmp_a, 1)
        row.addWidget(QLabel("B"))
        row.addWidget(self._cmp_b, 1)
        v.addLayout(row)
        self._cmp_summary = QLabel("")
        self._cmp_summary.setStyleSheet(f"color:{T['muted']};font-size:12px;")
        v.addWidget(self._cmp_summary)
        self._cmp_table = ProfileDiffTable()
        v.addWidget(self._cmp_table, 1)
        self._refresh_compare_combos()
        return w

    def _refresh_compare_combos(self):
        names = sorted(self.sm.get_profiles(), key=str.lower)
        for c, default in ((self._cmp_a, 0), (self._cmp_b, 1)):
            keep = c.currentData()
            c.blockSignals(True)
            c.clear()
            c.addItem("● Current editor state", "__current__")
            for n in names:
                c.addItem(n, n)
            i = c.findData(keep) if keep else -1
            c.setCurrentIndex(i if i >= 0 else min(default, c.count() - 1))
            c.blockSignals(False)
        self._update_compare()

    def _current_state(self) -> dict:
        win = self.window()
        env, gs_on, gs_flags = {}, False, {}
        if hasattr(win, "_env_widget"):
            env = win._env_widget.get_env_dict()
        if hasattr(win, "_gamescope"):
            gs_on = win._gamescope.is_enabled()
            gs_flags = win._gamescope.get_flag_values()
        arch = self.sm.get_arch()
        return {"settings": self.sm.get_settings_list(), "arch": arch.code if arch else None,
                "env_vars": env, "gamescope_enabled": gs_on, "gamescope_flags": gs_flags}

    def _get_profile(self, key):
        if key == "__current__":
            return self._current_state()
        return self.sm.get_profiles().get(key)

    def _update_compare(self, *_):
        ka, kb = self._cmp_a.currentData(), self._cmp_b.currentData()
        if ka is None or kb is None:
            return
        n = self._cmp_table.show_diff(self._get_profile(ka), self._get_profile(kb),
                                      self._cmp_a.currentText(), self._cmp_b.currentText())
        self._cmp_summary.setText("Identical." if n == 0 else f"{n} difference(s)")
