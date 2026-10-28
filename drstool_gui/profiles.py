"""Profile manager page: save / load / rename / import / export / share codes."""

from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLineEdit, QListWidget, QMenu, QMessageBox,
    QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from .base import _as_dict, _atomic_write, _CORE_ERR, share_code
from .dialogs import ChangesDialog, ShareImportDialog
from .profile_io import parse_profile_import, profile_export_text, profile_share_code
from .settings_manager import SettingsManager
from .state import state_as_profile
from .toasts import toast
from .widgets import _slugify, make_list_item, SidebarDelegate


# ============================================================================
# Profile Manager
# ============================================================================

class ProfileManagerWidget(QWidget):
    profile_loaded = Signal(str)

    def __init__(self, settings_manager: SettingsManager):
        super().__init__()
        self.settings_manager = settings_manager

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        create_layout = QHBoxLayout()
        create_layout.setSpacing(4)

        self._profile_name = QLineEdit()
        self._profile_name.setPlaceholderText("Profile name")
        self._profile_name.setStyleSheet("""
            QLineEdit {
                background: #12151b;
                border: 1px solid #1d222c;
                color: #e8eaf0;
                font-size: 12px;
                padding: 3px 8px;
                border-radius: 3px;
                min-height: 26px;
            }
            QLineEdit:focus {
                border-color: #4a7300;
            }
        """)
        self._profile_name.returnPressed.connect(self._create_profile)
        create_layout.addWidget(self._profile_name)

        save_btn = QPushButton("Save Profile")
        save_btn.clicked.connect(self._create_profile)
        save_btn.setStyleSheet("""
            QPushButton {
                background: #76b900;
                color: #000;
                border: 1px solid #76b900;
                border-radius: 3px;
                padding: 3px 12px;
                font-size: 12px;
                font-weight: 700;
                min-height: 26px;
            }
            QPushButton:hover {
                background: #8fd400;
            }
        """)
        create_layout.addWidget(save_btn)
        layout.addLayout(create_layout)

        self._profile_list = QListWidget()
        self._profile_list.setItemDelegate(SidebarDelegate(self._profile_list))
        self._profile_list.setMouseTracking(True)
        self._profile_list.viewport().setAttribute(Qt.WA_Hover, True)
        self._profile_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._profile_list.setStyleSheet("""
            QListWidget {
                background: #12151b;
                border: 1px solid #1d222c;
                border-radius: 3px;
                color: #c8cdd8;
                font-size: 12px;
            }
            QListWidget::item {
                padding: 5px 10px;
                border-bottom: 1px solid #1d222c;
            }
            QListWidget::item:selected {
                background: rgba(118, 185, 0, 0.08);
                border-color: #76b900;
            }
            QListWidget::item:hover {
                background: #0c0e12;
            }
        """)
        self._profile_list.itemDoubleClicked.connect(self._load_selected)
        layout.addWidget(self._profile_list)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)

        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self._load_selected)
        load_btn.setStyleSheet("""
            QPushButton {
                border: 1px solid #1d222c;
                border-radius: 3px;
                padding: 3px 10px;
                background: #12151b;
                color: #c8cdd8;
                font-size: 11px;
                min-height: 24px;
            }
            QPushButton:hover {
                border-color: #4a7300;
            }
        """)
        btn_layout.addWidget(load_btn)

        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._delete_selected)
        del_btn.setStyleSheet("""
            QPushButton {
                border: 1px solid #1d222c;
                border-radius: 3px;
                padding: 3px 10px;
                background: #12151b;
                color: #e84545;
                font-size: 11px;
                min-height: 24px;
            }
            QPushButton:hover {
                border-color: #e84545;
            }
        """)
        btn_layout.addWidget(del_btn)

        more_btn = QToolButton()
        more_btn.setText("More ▾")
        more_btn.setPopupMode(QToolButton.InstantPopup)
        more_btn.setStyleSheet(f"""
            QToolButton {{ border:1px solid #1d222c; border-radius:3px; padding:3px 10px;
                          background:#12151b; color:#c8cdd8; font-size:11px; min-height:18px; }}
            QToolButton:hover {{ border-color:#4a7300; }}
            QToolButton::menu-indicator {{ image:none; width:0; }}""")
        self._more_menu = QMenu(more_btn)
        self._more_menu.aboutToShow.connect(lambda: self._fill_menu(self._more_menu, True))
        more_btn.setMenu(self._more_menu)
        btn_layout.addWidget(more_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self._profile_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._profile_list.customContextMenuRequested.connect(self._context_menu)

        # profiles_changed (not settings_changed) — the profile list only
        # needs a rebuild when a profile is saved/loaded/deleted, not on
        # every single DRS setting edit.
        self.settings_manager.profiles_changed.connect(self._refresh)
        self.settings_manager.profile_loaded.connect(lambda _name: self._refresh())
        self._refresh()

    def _create_profile(self):
        name = self._profile_name.text().strip()
        if name and name in self.settings_manager.get_profiles():
            reply = QMessageBox.question(
                self, "Overwrite profile",
                f'A profile named "{name}" already exists.\n\n'
                "Replace it with the current settings?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
        if name:
            # Collect env vars from MainWindow's env_widget if available
            env_vars = {}
            gs_enabled = False
            gs_flags: Dict[str, str] = {}
            win = self.window()
            if win and hasattr(win, '_env_widget'):
                env_vars = win._env_widget.get_env_dict()
            if win and hasattr(win, '_gamescope'):
                gs_enabled = win._gamescope.is_enabled()
                gs_flags = win._gamescope.get_flag_values()
            self.settings_manager.save_profile(
                name, env_vars,
                gamescope_enabled=gs_enabled,
                gamescope_flags=gs_flags,
            )
            self._profile_name.clear()
            self._refresh()

    def _load_selected(self):
        item = self._profile_list.currentItem()
        if item and item.data(Qt.UserRole):
            name = item.text()
            self.settings_manager.load_profile(name)
            self.profile_loaded.emit(name)
            self._refresh()

    def _delete_selected(self):
        item = self._profile_list.currentItem()
        if item and item.data(Qt.UserRole):
            reply = QMessageBox.question(self, "Delete", f'Delete "{item.text()}"?',
                                        QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.settings_manager.delete_profile(item.text())
                self._refresh()

    # ── Rename / duplicate / compare / export / import ───────────────────
    def _selected_name(self) -> Optional[str]:
        item = self._profile_list.currentItem()
        return item.text() if item and item.data(Qt.UserRole) else None

    def _context_menu(self, pos):
        item = self._profile_list.itemAt(pos)
        if item and item.data(Qt.UserRole):
            self._profile_list.setCurrentItem(item)
        menu = QMenu(self)
        self._fill_menu(menu, False)
        menu.exec(self._profile_list.viewport().mapToGlobal(pos))

    def _fill_menu(self, menu: QMenu, include_import: bool):
        menu.clear()
        name = self._selected_name()
        acts = [("Load", self._load_selected), ("Rename…", self._rename_selected),
                ("Duplicate", self._duplicate_selected), ("Compare with editor…", self._compare_selected),
                ("Export to file…", self._export_selected),
                ("Copy share code", self._copy_share_code), (None, None),
                ("Delete", self._delete_selected)]
        for label, fn in acts:
            if label is None:
                menu.addSeparator()
                continue
            a = menu.addAction(label)
            a.setEnabled(bool(name) and (label != "Copy share code" or not _CORE_ERR))
            a.triggered.connect(fn)
        menu.addSeparator()
        menu.addAction("Import from file…").triggered.connect(self._import_file)
        a = menu.addAction("Import share code…")
        a.setEnabled(not _CORE_ERR)
        a.triggered.connect(lambda: self.open_share_import())

    def _copy_share_code(self):
        name = self._selected_name()
        prof = self.settings_manager.get_profiles().get(name) if name else None
        if not isinstance(prof, dict):
            return
        try:
            code = profile_share_code(name, prof)
        except share_code.ShareCodeError as ex:
            QMessageBox.information(self, "Share profile", f'Cannot share "{name}": {ex}.')
            return
        QApplication.clipboard().setText(code)
        toast(f"Copied share code for “{name}” ({len(code)} characters) — paste it anywhere; "
              "DRSTool imports it with Profiles › Import share code or Ctrl+Shift+V.", "ok", 6000)

    def open_share_import(self, text: str = ""):
        if not text:
            clip = QApplication.clipboard().text()
            text = clip if (share_code and share_code.looks_like_code(clip)) else ""
        dlg = ShareImportDialog(self.settings_manager, text, self)
        if dlg.exec() == QDialog.Accepted and dlg.saved_name:
            self._select_name(dlg.saved_name)
            if dlg.load_after:
                self.settings_manager.load_profile(dlg.saved_name)
                self.profile_loaded.emit(dlg.saved_name)
            toast(f"Imported shared profile “{dlg.saved_name}”", "ok")

    def _rename_selected(self):
        old = self._selected_name()
        if not old:
            return
        from PySide6.QtWidgets import QInputDialog
        new, ok = QInputDialog.getText(self, "Rename profile", "New name:", text=old)
        new = (new or "").strip()
        if not ok or not new or new == old:
            return
        if new in self.settings_manager.get_profiles():
            QMessageBox.warning(self, "Rename profile", f'A profile named "{new}" already exists.')
            return
        self.settings_manager.rename_profile(old, new)
        w = self.window()
        if hasattr(w, "_update_window_title"):
            w._update_window_title()

    def _duplicate_selected(self):
        name = self._selected_name()
        if name:
            new = self.settings_manager.duplicate_profile(name)
            if new:
                self._select_name(new)

    def _compare_selected(self):
        name = self._selected_name()
        w = self.window()
        if name and hasattr(w, "capture_state"):
            prof = self.settings_manager.get_profiles().get(name)
            dlg = ChangesDialog(prof, state_as_profile(w.capture_state()), f"“{name}”", "Editor",
                                f"Profile “{name}” vs. the editor", "Load this profile", self)
            if dlg.exec() == QDialog.Accepted and dlg.revert:
                self.settings_manager.load_profile(name)
                self.profile_loaded.emit(name)

    def _export_selected(self):
        name = self._selected_name()
        prof = self.settings_manager.get_profiles().get(name) if name else None
        if not isinstance(prof, dict):
            return
        default = Path.home() / f"{_slugify(name) or 'profile'}.drstool.json"
        path, _f = QFileDialog.getSaveFileName(self, "Export profile", str(default),
                                               "DRSTool profile (*.json);;All files (*)")
        if not path:
            return
        try:
            _atomic_write(Path(path), profile_export_text(name, prof))
        except OSError as ex:
            QMessageBox.warning(self, "Export profile", f"Could not write {path}:\n{ex}")

    def _import_file(self):
        path, _f = QFileDialog.getOpenFileName(self, "Import profile(s)", str(Path.home()),
                                               "DRSTool profile / profiles.json (*.json);;All files (*)")
        if not path:
            return
        try:
            profs = parse_profile_import(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as ex:
            QMessageBox.warning(self, "Import profile", f"Could not import {Path(path).name}:\n{ex}")
            return
        names = self.settings_manager.import_profiles(profs)
        if names:
            self._select_name(names[0])
            w = self.window()
            if w and w.statusBar():
                w.statusBar().showMessage(f"Imported {len(names)} profile(s): " + ", ".join(names[:4]), 6000)

    def _select_name(self, name: str):
        for i in range(self._profile_list.count()):
            it = self._profile_list.item(i)
            if it.data(Qt.UserRole) == name:
                self._profile_list.setCurrentItem(it)
                break

    def _refresh(self):
        self._profile_list.clear()
        profiles = self.settings_manager.get_profiles()
        current = self.settings_manager.get_current_profile()

        if not profiles:
            hint = make_list_item("No profiles yet — name one above and save", None)
            hint.setFlags(Qt.NoItemFlags)
            self._profile_list.addItem(hint)
            return
        for name in sorted(profiles, key=str.lower):
            prof = profiles.get(name)
            summary, tip = self._summarize(prof)
            item = make_list_item(name, name, is_set=(name == current), value=summary)
            item.setToolTip(tip)
            self._profile_list.addItem(item)

    @staticmethod
    def _summarize(prof) -> tuple:
        if not isinstance(prof, dict):
            return "malformed", "This entry in profiles.json is not a valid profile."
        n_drs = len(_as_dict(prof.get("settings")))
        n_env = len(_as_dict(prof.get("env_vars")))
        arch = str(prof.get("arch") or "")      # hand-edited non-string arch crashed the list
        gs = bool(prof.get("gamescope_enabled"))
        short = " ".join(p for p in (
            f"{n_drs} drs" if n_drs else "",
            f"{n_env} env" if n_env else "",
            arch, "gs" if gs else "") if p)
        tip = (f"{n_drs} DRS setting(s), {n_env} env var(s)\n"
               f"GPU arch: {arch or 'not set'}\n"
               f"Gamescope: {'on' if gs else 'off'}\n\nDouble-click to load.")
        return short or "empty", tip
