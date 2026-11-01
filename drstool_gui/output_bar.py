"""Output bar: command / environment preview with copy and apply actions."""

from typing import Dict, Optional, TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .base import APP_TITLE
from .launch_line import build_launch_line
from .settings_manager import SettingsManager
from .theme import btn_ss, caption, T
from .widgets import ElidedLabel

if TYPE_CHECKING:
    from .env_widgets import EnvVarsWidget
    from .gamescope import GamescopeFlagsWidget


# ============================================================================
# Output Bar - Modern Layout
# ============================================================================

class OutputBarWidget(QWidget):
    """
    The generated launch string, always visible above the tabs.
    Left: one row per output (NVAPI arch + DRS string, env vars, gamescope),
    each value elided to the available width and click-to-copy.
    Right: Copy (with a Steam %command% variant), Save, Reset.
    """
    feedback = Signal(str)

    def __init__(self, settings_manager: SettingsManager, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self._env_widget: Optional['EnvVarsWidget'] = None  # set after construction
        self._gamescope_widget: Optional['GamescopeFlagsWidget'] = None  # set after construction
        self._dirty = False

        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("outputBar")
        self.setStyleSheet(f"#outputBar {{ background:{T['surface']}; border-top:1px solid {T['border']}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 10, 16, 12)
        root.setSpacing(8)

        # ── Header: caption + live summary + actions ─────────────────────
        head = QHBoxLayout()
        head.setSpacing(8)
        dot = QLabel("\u25cf")
        dot.setStyleSheet(f"color:{T['accent']}; font-size:9px; background:transparent;")
        head.addWidget(dot)
        head.addWidget(caption("Launch string", T["text2"]))
        self._summary = QLabel("")
        self._summary.setStyleSheet(f"color:{T['muted']}; font-size:11px; background:transparent;")
        head.addSpacing(6)
        head.addWidget(self._summary)
        head.addStretch(1)

        self._btn_ss_primary = btn_ss("primary")
        self._btn_ss_quiet = btn_ss("default")
        self._btn_ss_dirty = (
            f"QPushButton{{background:rgba(227,167,63,0.14);border:1px solid {T['warn']};"
            f"color:{T['warn']};border-radius:6px;padding:5px 14px;font-weight:700;font-size:12px;}}"
            f"QPushButton:hover{{background:rgba(227,167,63,0.24);}}"
        )

        reset_btn = QPushButton("Reset")
        reset_btn.setStyleSheet(btn_ss("danger"))
        reset_btn.setToolTip("Clear every DRS setting, the GPU arch, env vars and gamescope flags")
        reset_btn.clicked.connect(self._reset_all)

        self._save_profile_btn = QPushButton("Save")
        self._save_profile_btn.setStyleSheet(self._btn_ss_quiet)
        self._save_profile_btn.setEnabled(False)
        self._save_profile_btn.clicked.connect(self._save_to_profile)

        steam_btn = QPushButton("Steam line")
        steam_btn.setStyleSheet(self._btn_ss_quiet)
        steam_btn.setToolTip("Copy  env … gamescope … -- %command%  for Steam's launch options")
        steam_btn.clicked.connect(self._copy_steam_env)

        copy_btn = QToolButton()
        copy_btn.setText("Copy all")
        copy_btn.setToolTip("Copy the full launch string  (Ctrl+Shift+C)")
        copy_btn.setPopupMode(QToolButton.MenuButtonPopup)
        copy_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        copy_btn.setStyleSheet(self._btn_ss_primary)
        menu = QMenu(copy_btn)
        act_plain = QAction("Copy as shell environment", menu)
        act_plain.triggered.connect(lambda: self._copy_all(steam=False))
        act_steam = QAction("Copy as Steam launch options  (… %command%)", menu)
        act_steam.triggered.connect(lambda: self._copy_all(steam=True))
        menu.addAction(act_plain)
        menu.addAction(act_steam)
        menu.addSeparator()
        act_imp = QAction("Import a launch line…  (Ctrl+Shift+V)", menu)
        act_imp.triggered.connect(lambda: self._call_win("open_import_dialog"))
        act_exp = QAction("Explain the current launch line…", menu)
        act_exp.triggered.connect(lambda: self._call_win("open_explain_dialog"))
        menu.addAction(act_imp)
        menu.addAction(act_exp)
        menu.addSeparator()
        act_share = QAction("Copy share code (DRSTOOL1:…)", menu)
        act_share.setToolTip("The whole configuration as one line of text — paste it in a chat or forum")
        act_share.triggered.connect(lambda: self._call_win("copy_share_code"))
        menu.addAction(act_share)
        copy_btn.setMenu(menu)
        copy_btn.clicked.connect(lambda: self._copy_all(steam=False))

        copy_btn.setMinimumWidth(116)
        for b in (reset_btn, self._save_profile_btn, steam_btn, copy_btn):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(30)
            head.addWidget(b)
        root.addLayout(head)

        # ── Console: one tagged row per output ───────────────────────────
        console = QFrame()
        console.setObjectName("launchConsole")
        console.setStyleSheet(f"#launchConsole {{ background:{T['sunken']}; border:1px solid {T['border']};"
                              " border-radius:9px; }")
        grid = QGridLayout(console)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)

        def tag(text, color, tip):
            k = QLabel(text)
            k.setToolTip(tip)
            k.setAlignment(Qt.AlignCenter)
            k.setFixedWidth(78)
            c = QColor(color)
            k.setStyleSheet(f"color:{color}; background:rgba({c.red()},{c.green()},{c.blue()},0.12);"
                            " border-radius:4px; padding:2px 0; font-size:10px; font-weight:800;"
                            " letter-spacing:1px;")
            return k

        def val(text):
            v = ElidedLabel(text)
            v.copied.connect(lambda _t: self._show_feedback("Copied"))
            return v

        self._val_labels = []
        grid.addWidget(tag("ARCH", T["info"], "DXVK_NVAPI_GPU_ARCH"), 0, 0)
        arch_row = QHBoxLayout()
        arch_row.setSpacing(10)
        self._arch_value = val("not set")
        self._arch_value.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._arch_value.setFixedWidth(96)
        arch_row.addWidget(self._arch_value)
        arch_row.addWidget(tag("DRS", T["accent"], "DXVK_NVAPI_DRS_SETTINGS"))
        self._settings_value = val("none")
        arch_row.addWidget(self._settings_value, 1)
        grid.addLayout(arch_row, 0, 1)

        grid.addWidget(tag("ENV", "#c792ea", "Environment variables"), 1, 0)
        self._env_value = val("none")
        grid.addWidget(self._env_value, 1, 1)

        grid.addWidget(tag("GAMESCOPE", T["warn"], "gamescope … --"), 2, 0)
        self._gamescope_value = val("disabled")
        grid.addWidget(self._gamescope_value, 2, 1)
        grid.setColumnStretch(1, 1)
        self._val_labels = [self._arch_value, self._settings_value, self._env_value, self._gamescope_value]
        root.addWidget(console)

        self.settings_manager.settings_changed.connect(self._update)
        self.settings_manager.arch_changed.connect(self._update)
        self.settings_manager.profile_loaded.connect(self._on_profile_loaded)
        self.settings_manager.profiles_changed.connect(self._update)
        self._update()

    def set_dirty(self, dirty: bool):
        self._dirty = dirty
        self._update()

    _EMPTY_VALUES = ("none", "not set", "disabled")

    def _style_values(self):
        for lab in getattr(self, "_val_labels", []):
            empty = lab.text() in self._EMPTY_VALUES
            # setStyleSheet() re-polishes the label; this runs on every edit,
            # so only touch it when the empty/filled state actually flips.
            if lab.property("_empty_ss") == empty:
                continue
            lab.setProperty("_empty_ss", empty)
            lab.setStyleSheet(
                f"QLabel{{ background:transparent; border:none; padding:3px 4px;"
                f" color:{T['faint'] if empty else T['text']}; font-family:{T['mono']}; font-size:12px;"
                f" font-style:{'italic' if empty else 'normal'}; border-radius:4px; }}"
                f"QLabel:hover{{ background:{T['hover']}; }}")
        n_drs = len(self.settings_manager.get_settings_list())
        n_env = len(self._env_widget.get_env_dict()) if self._env_widget is not None else 0
        gs = self._gamescope_widget is not None and self._gamescope_widget.is_enabled()
        self._summary.setText(f"{n_drs} DRS  ·  {n_env} env  ·  gamescope {'on' if gs else 'off'}"
                              "   —  click a value to copy it")

    def _update(self):
        arch = self.settings_manager.get_arch()
        if arch:
            self._arch_value.setText(arch.code)

        else:
            self._arch_value.setText("not set")


        settings_str = self.settings_manager.get_settings_string()
        if settings_str:
            self._settings_value.setText(settings_str)

        else:
            self._settings_value.setText("none")


        # Row 2: env vars string
        if self._env_widget is not None:
            env_str = self._env_widget.get_env_string()
        else:
            env_str = ""
        self._env_value.setText(env_str if env_str else "none")


        # Row 2b: gamescope command (CLI flags, ends in '--' — no %command%
        # or launcher assumed; the person appends their own launch command)
        if self._gamescope_widget is not None and self._gamescope_widget.is_enabled():
            gs_str = self._gamescope_widget.build_command()
            self._gamescope_value.setText(gs_str)

        else:
            self._gamescope_value.setText("disabled")


        current = self.settings_manager.get_current_profile()
        self._save_profile_btn.setEnabled(current is not None)
        dirty = bool(current) and self._dirty
        self._save_profile_btn.setText("Save •" if dirty else "Save")
        self._save_profile_btn.setStyleSheet(self._btn_ss_dirty if dirty else self._btn_ss_quiet)
        if current:
            self._save_profile_btn.setToolTip(
                f"Save to profile \"{current}\"  (Ctrl+S)"
                + ("\nThere are unsaved changes." if dirty else ""))
        else:
            self._save_profile_btn.setToolTip(
                "Load or create a profile in the Profiles tab first")
        self._style_values()

    def _on_profile_loaded(self, name):
        self._update()

    def build_launch_string(self, steam: bool = False) -> str:
        if self._env_widget is not None:
            text = self._env_widget.get_full_combined_string()
        else:
            text = self.settings_manager.get_full_env_string()
        if self._gamescope_widget is not None and self._gamescope_widget.is_enabled():
            gs_str = self._gamescope_widget.build_command()
            if gs_str:
                text = f"{text} {gs_str}" if text else gs_str
        if steam:
            # gamescope's command already ends in "--" (plus mangohud if
            # enabled), so %command% slots straight in after it.
            text = f"{text} %command%" if text else "%command%"
        return text

    def _copy_all(self, steam: bool = False):
        text = self.build_launch_string(steam)
        if text and text != "%command%":
            QApplication.clipboard().setText(text)
            self._show_feedback("Copied Steam launch options" if steam else "Copied launch string")
        else:
            self._show_feedback("Nothing to copy yet")

    def _copy_steam_env(self):
        win = self.window()
        if not hasattr(win, "collect_launch_payload"):
            return self._copy_all(steam=True)
        p = win.collect_launch_payload()
        if not p["env"] and not p["gs_cmd"]:
            self._show_feedback("Nothing to copy yet")
            return
        QApplication.clipboard().setText(build_launch_line(p, "%command%", True))
        self._show_feedback("Copied: env … gamescope … -- %command%")

    def _save_to_profile(self):
        current = self.settings_manager.get_current_profile()
        if current:
            env_vars = self._env_widget.get_env_dict() if self._env_widget else {}
            gs_enabled = False
            gs_flags: Dict[str, str] = {}
            if self._gamescope_widget is not None:
                gs_enabled = self._gamescope_widget.is_enabled()
                gs_flags = self._gamescope_widget.get_flag_values()
            self.settings_manager.save_profile(
                current, env_vars,
                gamescope_enabled=gs_enabled,
                gamescope_flags=gs_flags,
            )
            self._show_feedback(f"Saved to: {current}")

    def _reset_all(self):
        reply = QMessageBox.question(
            self,
            "Reset All Settings",
            "Do you want to reset all settings to default?\n\n"
            "This will remove all configured settings, clear GPU architecture "
            "selection, and clear all DXVK/VKD3D-Proton env vars.\n\n"
            "(Undo with Ctrl+Z.)",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            win = self.window()
            if win is not None and hasattr(win, "_pending_label"):
                win._pending_label = "Reset everything"
            self.settings_manager.reset_everything()
            if win:
                # Clear env widget
                if hasattr(win, '_env_widget'):
                    win._env_widget.reset_all_values()
                # Clear Gamescope CLI-flag builder
                if hasattr(win, '_gamescope'):
                    win._gamescope.reset_all_values()
                # Clear env editor right panel
                if hasattr(win, '_env_editor'):
                    win._env_editor.discard_pending_edit()
                # Reset window title
                win.setWindowTitle(APP_TITLE)
                # Forget remembered right-panel state per tab — otherwise
                # switching tabs after a reset could restore a now-stale
                # editor page index with nothing behind it.
                if hasattr(win, '_tab_right_memory'):
                    win._tab_right_memory = {}
                # Restore the correct right-panel page for whichever tab is
                # currently active.  Simply calling setCurrentIndex(0) (the
                # old behaviour) left the Extra Tools tab (index 3) showing
                # a blank panel because its right-panel page is 4, not 0 —
                # and _switch_tab(3) is never re-called while the user stays
                # on that tab, so the page never got fixed without a
                # restart or profile load.
                if hasattr(win, '_sidebar_stack') and hasattr(win, '_switch_tab'):
                    active = win._sidebar_stack.currentIndex()
                    win._switch_tab(active)
                if hasattr(win, '_populate_settings'):
                    win._populate_settings()
                if hasattr(win, '_populate_arch_list'):
                    win._populate_arch_list()
            self._show_feedback("All settings reset!")

    def _show_feedback(self, msg):
        self.window().statusBar().showMessage(msg, 2000)

    def _call_win(self, fn: str):
        w = self.window()
        if hasattr(w, fn):
            getattr(w, fn)()
