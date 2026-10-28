"""MainWindow."""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from PySide6.QtCore import QPoint, QSettings, Qt, QTimer
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget,
    QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from drstool_core import support_bundle
from drstool_core.models import GPUArch

from . import base as _base
from .app_dialogs import _qbool, _spawn_restart, AppearanceDialog, ShortcutsDialog
from .arch_widgets import ArchDetailWidget, ArchListWidget
from .base import _CORE_ERR, APP_ID, APP_TITLE, ICON_PATH, share_code
from .catalog_data import ALL_ENV_VARS, ALL_SETTINGS, GPU_ARCHS
from .dialogs import ChangesDialog, PlanDialog, RecipeGallery
from .dxvk_conf import DxvkConfWidget
from .env_widgets import EnvVarEditorWidget, EnvVarsWidget
from .external_change import ExternalChangeBanner, ExternalChangeWatcher
from .extra_tools import ExtraToolsWidget
from .favorites import FAVS, install_fav_menu
from .game_card import GameCardWidget, GameListWidget, LinkedGamesDialog
from .gamescope import GamescopeFlagsWidget
from .history import HistoryWidget
from .launch_import import looks_like_launch_text
from .launch_line import build_launch_line
from .launchers import (
    BottlesBackend, HeroicBackend, LauncherSyncWidget, LaunchLineWidget, SteamBackend,
)
from .links import game_key_for_target, LINKS, plain_title, profile_fp, state_fp
from .lint import lint_config, LintIssue
from .lookup import (
    _ENV_BY_NAME, canonical_drs_key, canonical_drs_value, custom_drs_setting, drs_lookup,
    env_def,
)
from .lutris_sync import LutrisSyncWidget
from .nip_import import NipImportWidget
from .output_bar import OutputBarWidget
from .palette import CommandPalette
from .profile_io import profile_share_code
from .profiles import ProfileManagerWidget
from .recipes import recipe_coverage, recipe_ops, recipe_requirement, RECIPES
from .setting_editor import SettingEditorWidget
from .settings_list import SettingsListWidget
from .settings_manager import SettingsManager
from .state import (
    canon_flat, describe_change, single_change_key, state_as_profile, state_from_profile,
    UndoTimeline,
)
from .steam_shortcuts_ui import ShortcutWidget, SteamShortcutsWidget
from .sysinfo import drs_unavailable, env_unavailable, SYSHUB
from .tasks import TASKS
from .theme import APPEARANCE, btn_ss, pill_ss, SCROLLABLE_CONTROL_QSS, T
from .toasts import _install_toasts, toast, ToastStatusBar
from .tool_ui import ExtraToolsSidebarWidget
from .widgets import _fmt_drs_value, BrandMark, EmptyState, NavTab

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .recipes import Recipe


class MainWindow(QMainWindow):
    # Tab indices (sidebar stack pages). Nav order on screen matches.
    TAB_DRS, TAB_ARCH, TAB_ENV, TAB_TOOLS, TAB_PROFILES, TAB_GAMES = range(6)
    # Right-stack pages
    PAGE_EMPTY, PAGE_SETTING, PAGE_ARCH, PAGE_ENV, PAGE_GAMESCOPE, PAGE_TOOLS, PAGE_LUTRIS, PAGE_GAMES = range(8)

    EMPTY_HINTS = {
        0: "Pick a DRS setting\nGreen dots mark settings already in the launch string; their value shows on the right.",
        1: "Pick a GPU generation to spoof\nSets DXVK_NVAPI_GPU_ARCH — leave it unset to report the real card.",
        2: "Pick an environment variable\nDXVK, VKD3D-Proton, Proton, Wine, NVIDIA, Gamescope and more — set ones carry a green dot.",
        4: "",
        5: "Pick a game\nOne card per game: linked profile, Proton build, DLSS / DXVK versions, "
           "the last frametime capture, log errors and the ProtonDB tier.",
    }

    def __init__(self):
        super().__init__()
        self.settings_manager = SettingsManager()
        self.all_settings = ALL_SETTINGS
        self._dirty = False
        # Undo timeline: every change settles for a moment, then is snapshotted.
        self._timeline: Optional[UndoTimeline] = None
        self._pending_label: Optional[str] = None
        self._restoring = False
        self._shown_custom_drs: frozenset = frozenset()
        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(350)
        self._state_timer.timeout.connect(self._on_state_settled)
        self._qsettings = QSettings(APP_ID, APP_ID)
        self._restart_pending = False

        self.setWindowTitle(APP_TITLE)
        if ICON_PATH.is_file():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setMinimumSize(980, 640)
        self.setStyleSheet(f"""
            QMainWindow {{ background: {T['bg']}; }}
            QSplitter::handle:horizontal {{ background: {T['bg']}; width: 4px; }}
            QSplitter::handle:horizontal:hover {{ background: {T['accent_dim']}; }}
        """)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self._output_bar = OutputBarWidget(self.settings_manager, self)

        # ===== Top navigation =====
        # Used to be five buttons squeezed into the 320px sidebar, where the
        # labels were clipped ("S Settin", "PU Arch"). Now a full-width bar
        # with live badges (how many things each tab contributes).
        nav = QWidget()
        nav.setObjectName("navBar")
        nav.setAttribute(Qt.WA_StyledBackground, True)
        nav.setStyleSheet(f"#navBar {{ background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                          f" stop:0 #11141a, stop:1 {T['bg']}); border-bottom:1px solid {T['border']}; }}")
        nav.setFixedHeight(56)
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(16, 0, 14, 0)
        nav_layout.setSpacing(0)
        self._brand = BrandMark()
        nav_layout.addWidget(self._brand)
        nav_layout.addSpacing(18)

        seg = QFrame()
        seg.setObjectName("navSeg")
        seg.setStyleSheet(f"#navSeg {{ background:{T['sunken']}; border:1px solid {T['border']};"
                          " border-radius:10px; }")
        seg_l = QHBoxLayout(seg)
        seg_l.setContentsMargins(3, 3, 3, 3)
        seg_l.setSpacing(2)

        self._settings_tab = NavTab("DRS Settings", "\u25c8")
        self._arch_tab = NavTab("GPU Arch", "\u25a3")
        self._env_tab = NavTab("Environment", "\u2630")
        self._env_tab.setToolTip("DXVK, VKD3D-Proton, forks, DXVK-NVAPI, NVIDIA, Proton, "
                                 "Wine, vk_flip_meter and Gamescope")
        self._flm_tab = NavTab("Extra Tools", "\u2692")
        self._profiles_tab = NavTab("Profiles", "\u25a4")
        self._games_tab = NavTab("Games", "\u25b6")
        self._games_tab.setToolTip("Every installed game with its profile, Proton, DLSS / DXVK, "
                                   "frametime, log errors and ProtonDB tier")
        self._nav_tabs = [self._settings_tab, self._arch_tab, self._env_tab,
                          self._flm_tab, self._profiles_tab, self._games_tab]
        for idx, btn in enumerate(self._nav_tabs):
            btn.clicked.connect(lambda _c=False, i=idx: self._switch_tab(i))
            btn.setToolTip((btn.toolTip() + "\n" if btn.toolTip() else "") + f"Ctrl+{idx + 1}")
            seg_l.addWidget(btn)
        self._settings_tab.setChecked(True)
        seg.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        nav_layout.addWidget(seg, 0, Qt.AlignVCenter)
        nav_layout.addStretch(1)

        _nav_btn_ss = btn_ss("ghost", "sm")
        self._undo_btn = QToolButton()
        self._undo_btn.setText("↶")
        self._undo_btn.setPopupMode(QToolButton.MenuButtonPopup)
        self._undo_menu = QMenu(self._undo_btn)
        self._undo_menu.aboutToShow.connect(self._fill_history_menu)
        self._undo_btn.setMenu(self._undo_menu)
        self._undo_btn.clicked.connect(self.undo)
        self._redo_btn = QToolButton()
        self._redo_btn.setText("↷")
        self._redo_btn.clicked.connect(self.redo)
        self._recipes_btn = QPushButton("✦ Recipes")
        self._recipes_btn.setToolTip("Curated one-click bundles (DLSS, HDR, Reflex, stutter, laptop dGPU…)\n"
                                     "Every recipe opens a preview with a lint forecast first.")
        self._recipes_btn.clicked.connect(self.open_recipes)
        for _b in (self._undo_btn, self._redo_btn, self._recipes_btn):
            _b.setCursor(Qt.PointingHandCursor)
            _b.setFixedHeight(30)
            _b.setStyleSheet(_nav_btn_ss)
            _b.setFocusPolicy(Qt.NoFocus)
            nav_layout.addWidget(_b)
            nav_layout.addSpacing(2)
        _sep = QFrame()
        _sep.setFixedSize(1, 22)
        _sep.setStyleSheet(f"background:{T['border_hi']};")
        nav_layout.addSpacing(8)
        nav_layout.addWidget(_sep)
        nav_layout.addSpacing(10)

        self._log_btn = QPushButton()
        self._log_btn.setCursor(Qt.PointingHandCursor)
        self._log_btn.setFixedHeight(28)
        self._log_btn.clicked.connect(self._toggle_logging)
        nav_layout.addWidget(self._log_btn)
        nav_layout.addSpacing(6)
        self._lint_btn = QPushButton("Lint \u2714")
        self._lint_btn.setCursor(Qt.PointingHandCursor)
        self._lint_btn.setFixedHeight(28)
        self._lint_btn.setToolTip("Configuration consistency check — click to see the findings")
        self._lint_btn.clicked.connect(self._open_linter)
        self._set_lint_button(0, 0)
        nav_layout.addWidget(self._lint_btn)
        nav_layout.addSpacing(6)
        self._tasks_btn = QPushButton()
        self._tasks_btn.setCursor(Qt.PointingHandCursor)
        self._tasks_btn.setFixedHeight(28)
        self._tasks_btn.clicked.connect(self._tasks_menu)
        self._tasks_btn.hide()
        nav_layout.addWidget(self._tasks_btn)
        TASKS.changed.connect(self._update_tasks_btn)
        nav_layout.addSpacing(4)

        self._profile_chip = QPushButton()
        self._profile_chip.setFlat(True)
        self._profile_chip.setCursor(Qt.PointingHandCursor)
        self._profile_chip.setFocusPolicy(Qt.NoFocus)
        self._profile_chip.clicked.connect(self._show_unsaved_changes)
        self._style_chip(T["muted"], False)
        nav_layout.addWidget(self._profile_chip)
        nav_layout.addSpacing(6)
        self._menu_btn = QToolButton()
        self._menu_btn.setText("⚙")
        self._menu_btn.setToolTip("Appearance, keyboard shortcuts, background tasks")
        self._menu_btn.setPopupMode(QToolButton.InstantPopup)
        self._menu_btn.setCursor(Qt.PointingHandCursor)
        self._menu_btn.setFixedHeight(30)
        self._menu_btn.setStyleSheet(_nav_btn_ss + "QToolButton::menu-indicator{image:none;width:0;}")
        _mm = QMenu(self._menu_btn)
        _mm.addAction("Appearance…\tCtrl+,").triggered.connect(self.open_appearance)
        _mm.addAction("Keyboard shortcuts\tF1").triggered.connect(self.open_shortcuts)
        _mm.addAction("Background tasks").triggered.connect(self._tasks_menu)
        self._menu_btn.setMenu(_mm)
        nav_layout.addWidget(self._menu_btn)
        main_layout.addWidget(nav)
        self._ext_banner = ExternalChangeBanner()
        main_layout.addWidget(self._ext_banner)

        splitter = QSplitter(Qt.Horizontal)
        self._splitter = splitter
        # The sidebar is one shared widget behind every tab: if the splitter
        # could collapse it to 0 every tab's left pane would vanish at once
        # with no visible handle to drag it back.
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(4)

        # ===== Sidebar =====
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setAttribute(Qt.WA_StyledBackground, True)
        sidebar.setStyleSheet(f"#sidebar {{ background:{T['bg']}; }}")
        sidebar.setMinimumWidth(260)
        sidebar.setMaximumWidth(900)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(10, 12, 6, 8)
        sidebar_layout.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("\u2315  Filter…   Ctrl+F   ·   everything: Ctrl+K")
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(f"""
            QLineEdit {{
                background: {T['sunken']}; border: 1px solid {T['border']};
                color: {T['text']}; font-size: 12px; padding: 4px 10px;
                border-radius: 8px; min-height: 30px;
            }}
            QLineEdit:hover {{ border-color: {T['border_hi']}; }}
            QLineEdit:focus {{ border-color: {T['accent']}; background:{T['surface']}; }}
            QLineEdit:disabled {{ color: {T['muted']}; background: {T['bg']}; }}
        """)
        # Debounced: every keystroke used to rebuild a ~250-item list.
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(120)
        self._filter_timer.timeout.connect(lambda: self._filter(self._search.text()))
        self._search.textChanged.connect(lambda _t: self._filter_timer.start())
        sidebar_layout.addWidget(self._search)

        # Filter chips: "only set" and "★ favorites" for the DRS / Arch / Env lists
        self._chips = QWidget()
        _cl = QHBoxLayout(self._chips)
        _cl.setContentsMargins(0, 0, 0, 0)
        _cl.setSpacing(6)
        _chip_ss = (f"QToolButton{{background:transparent;border:1px solid {T['border_hi']};"
                    f"border-radius:11px;color:{T['muted']};padding:3px 11px;font-size:11px;font-weight:600;}}"
                    f"QToolButton:hover{{border-color:{T['accent_dim']};}}"
                    f"QToolButton:checked{{background:rgba(118,185,0,0.18);border-color:{T['accent']};"
                    f"color:{T['text']};}}")
        self._chip_set = QToolButton()
        self._chip_set.setText("\u25cf Only set")
        self._chip_set.setToolTip("Show only entries that are part of the launch string")
        self._chip_fav = QToolButton()
        self._chip_fav.setText("\u2605 Favorites")
        self._chip_fav.setToolTip("Show only favorites  (right-click an entry or Ctrl+D to star it)")
        for _c in (self._chip_set, self._chip_fav):
            _c.setCheckable(True)
            _c.setCursor(Qt.PointingHandCursor)
            _c.setStyleSheet(_chip_ss)
            _c.toggled.connect(lambda _x: self._on_chips())
            _cl.addWidget(_c)
        _cl.addStretch(1)
        sidebar_layout.addWidget(self._chips)

        self._sidebar_stack = QStackedWidget()
        sidebar_layout.addWidget(self._sidebar_stack)

        # Page 0: Settings list
        self._settings_list = SettingsListWidget()
        self._settings_list.setting_selected.connect(self._open_setting)
        self._sidebar_stack.addWidget(self._settings_list)

        # Page 1: Architecture list
        self._arch_list = ArchListWidget()
        self._arch_list.arch_selected.connect(self._select_architecture)
        self._sidebar_stack.addWidget(self._arch_list)

        # Page 2: Env Vars list
        self._env_widget = EnvVarsWidget(self.settings_manager)
        self._env_widget.env_var_selected.connect(self._open_env_var)
        self._env_widget.env_changed.connect(self._on_env_changed)
        self._update_log_btn()
        self._sidebar_stack.addWidget(self._env_widget)

        # Page 3: Extra Tools sidebar (orientation text for both sub-tools)
        self._flm_sidebar = ExtraToolsSidebarWidget()
        self._sidebar_stack.addWidget(self._flm_sidebar)

        # Page 4: Saved profiles (sidebar) — Lutris Sync lives on the right
        # now; it used to be a sub-tab squeezed into this narrow column while
        # the whole right panel sat empty.
        self._profiles_widget = ProfileManagerWidget(self.settings_manager)
        self._profiles_widget.profile_loaded.connect(self._on_profile_loaded)
        profiles_scroll = QScrollArea()
        profiles_scroll.setWidgetResizable(True)
        profiles_scroll.setWidget(self._profiles_widget)
        profiles_scroll.setStyleSheet(SCROLLABLE_CONTROL_QSS)
        self._sidebar_stack.addWidget(profiles_scroll)

        # Page 5: Games (one card per game on the right)
        self._games_list = GameListWidget(self.settings_manager)
        self._sidebar_stack.addWidget(self._games_list)

        self._lutris_sync_widget = LutrisSyncWidget(self.settings_manager)
        self._env_widget.env_changed.connect(self._lutris_sync_widget._update_preview)
        lutris_scroll = QScrollArea()
        lutris_scroll.setWidgetResizable(True)
        lutris_scroll.setWidget(self._lutris_sync_widget)
        lutris_scroll.setStyleSheet(SCROLLABLE_CONTROL_QSS)

        # Launcher sync tabs: runnable line + Lutris + Steam + Heroic + Bottles
        self._launch_line = LaunchLineWidget()
        self._steam_sync = LauncherSyncWidget(SteamBackend())
        self._heroic_sync = LauncherSyncWidget(HeroicBackend())
        self._bottles_sync = LauncherSyncWidget(BottlesBackend())
        self._sync_tabs = QTabWidget()
        self._sync_tabs.setStyleSheet(ExtraToolsWidget._TAB_SS)
        self._sync_tabs.addTab(self._launch_line, "Launch line")
        self._sync_tabs.addTab(lutris_scroll, "Lutris")
        for _w, _t in ((self._steam_sync, "Steam"), (self._heroic_sync, "Heroic"),
                       (self._bottles_sync, "Bottles")):
            _sc = QScrollArea()
            _sc.setWidgetResizable(True)
            _sc.setWidget(_w)
            _sc.setStyleSheet(SCROLLABLE_CONTROL_QSS)
            self._sync_tabs.addTab(_sc, _t)
        self._history = HistoryWidget(self.settings_manager)
        self._nip_import = NipImportWidget(self.settings_manager)
        self._dxvk_conf = DxvkConfWidget()
        self._shortcut = ShortcutWidget()
        self._steam_shortcuts = SteamShortcutsWidget()
        _ssc = QScrollArea()
        _ssc.setWidgetResizable(True)
        _ssc.setWidget(self._steam_shortcuts)
        _ssc.setStyleSheet(SCROLLABLE_CONTROL_QSS)
        self._sync_tabs.addTab(self._dxvk_conf, "dxvk.conf")
        self._sync_tabs.addTab(self._shortcut, "Shortcut")
        self._sync_tabs.addTab(_ssc, "Add to Steam")
        self._sync_tabs.addTab(self._history, "History")
        self._sync_tabs.addTab(self._nip_import, "Import .nip")
        self._sync_tabs.currentChanged.connect(lambda _i: self._refresh_sync_previews())

        splitter.addWidget(sidebar)

        # ===== Right side (editor area) =====
        editor = QWidget()
        editor.setObjectName("editorPane")
        editor.setAttribute(Qt.WA_StyledBackground, True)
        editor.setStyleSheet(f"#editorPane {{ background:#0e1015; border-left:1px solid {T['border']}; }}")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(28, 20, 28, 16)
        editor_layout.setSpacing(0)

        self._right_stack = QStackedWidget()
        editor_layout.addWidget(self._right_stack)

        # Page 0: Empty state (text depends on the active tab)
        self._placeholder = EmptyState()
        self._placeholder.set_content("\u25c8", *self.EMPTY_HINTS[0].split("\n", 1))
        self._right_stack.addWidget(self._placeholder)

        # Page 1: Setting editor
        self._setting_editor = SettingEditorWidget(self.settings_manager)
        self._setting_editor.cleared.connect(self._on_setting_cleared)
        self._right_stack.addWidget(self._setting_editor)

        # Page 2: Architecture detail
        self._arch_detail = ArchDetailWidget()
        self._arch_detail.clear_requested.connect(self._clear_architecture)
        self._right_stack.addWidget(self._arch_detail)

        # Page 3: Env var editor
        self._env_editor = EnvVarEditorWidget()
        self._env_editor.set_list_widget(self._env_widget)
        self._right_stack.addWidget(self._env_editor)

        # Page 4: Gamescope CLI-flag builder (real argv flags, not env vars)
        self._gamescope = GamescopeFlagsWidget()
        gamescope_scroll = QScrollArea()
        gamescope_scroll.setWidgetResizable(True)
        gamescope_scroll.setWidget(self._gamescope)
        gamescope_scroll.setStyleSheet(SCROLLABLE_CONTROL_QSS)
        self._right_stack.addWidget(gamescope_scroll)

        # Page 5: Extra Tools panel (vk_flip_meter + lutris-game-tune sub-tabs)
        self._extra_tools = ExtraToolsWidget()
        self._right_stack.addWidget(self._extra_tools)
        self._flm_sidebar.bind(self._extra_tools)

        # Page 6: Lutris Sync (Profiles tab)
        self._right_stack.addWidget(self._sync_tabs)

        # Page 7: Game card (Games tab)
        self._game_card = GameCardWidget(self.settings_manager)
        self._right_stack.addWidget(self._game_card)
        self._games_list.game_selected.connect(self._open_game)

        # Wire the Gamescope CLI-flag builder into the Lutris sync mapping
        self._lutris_sync_widget.set_gamescope_widget(self._gamescope)
        self._gamescope.changed.connect(self._lutris_sync_widget._update_preview)

        self._right_stack.setCurrentIndex(self.PAGE_EMPTY)

        splitter.addWidget(editor)
        # setSizes() must run after both panes are in the splitter.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 900])
        main_layout.addWidget(splitter, 1)
        main_layout.addWidget(self._output_bar)

        # Status bar
        status_bar = ToastStatusBar()
        self.setStatusBar(status_bar)
        _install_toasts(self)
        status_bar.setStyleSheet(f"""
            QStatusBar {{
                background: {T['sunken']}; color: {T['muted']}; font-size: 11px;
                border-top: 1px solid {T['border']}; min-height: 24px; padding-left:8px;
            }}
            QStatusBar::item {{ border:none; }}
        """)
        status_bar.showMessage("Ready")
        self._sys_lbl = QLabel("probing system…")
        self._sys_lbl.setStyleSheet(f"color:{T['faint']}; font-size:11px; padding:0 10px;"
                                    f" font-family:{T['mono']};")
        status_bar.addPermanentWidget(self._sys_lbl)

        # Signals
        self.settings_manager.settings_changed.connect(self._on_setting_changed)
        self.settings_manager.arch_changed.connect(self._update_arch_ui)
        self.settings_manager.profile_loaded.connect(lambda _n: self._update_window_title())
        self.settings_manager.profile_save_error.connect(self._on_profile_save_error)
        if self.settings_manager.startup_notice:
            QTimer.singleShot(0, lambda m=self.settings_manager.startup_notice:
                              self._on_profile_save_error(m))
        self.settings_manager.profile_saved.connect(lambda _n: self._set_dirty(False))
        self.settings_manager.profiles_changed.connect(self._refresh_badges)
        self.settings_manager.profiles_changed.connect(self._update_window_title)
        # Profile ↔ game links: every write from a sync tab is remembered per game, and saving
        # a profile offers to bring the games that use it up to date.
        for _w in (self._lutris_sync_widget, self._steam_sync, self._heroic_sync, self._bottles_sync):
            _w.applied.connect(self._on_game_written)
        self.settings_manager.profile_saved.connect(self._suggest_linked_update)
        LINKS.changed.connect(self._refresh_badges)

        # Anything that changes the output marks a loaded profile dirty.
        self.settings_manager.settings_changed.connect(self._mark_dirty)
        self._env_widget.env_changed.connect(self._mark_dirty)
        self._gamescope.changed.connect(self._mark_dirty)
        self._gamescope.changed.connect(
            lambda: self._env_widget.set_gamescope_enabled(self._gamescope.is_enabled()))
        self._gamescope.changed.connect(self._refresh_badges)

        # Feature pack: lint, system probe, favorites, launcher previews
        self._lint_timer = QTimer(self)
        self._lint_timer.setSingleShot(True)
        self._lint_timer.setInterval(250)
        self._lint_timer.timeout.connect(self._run_lint)
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(200)
        self._sync_timer.timeout.connect(self._refresh_sync_previews)
        self._lint_issue_count = (0, 0)
        for _sig in (self.settings_manager.settings_changed, self._env_widget.env_changed,
                     self._gamescope.changed):
            _sig.connect(self._schedule_lint)
            _sig.connect(lambda *_: self._sync_timer.start())
        self._extra_tools.linter.fix_requested.connect(self._apply_lint_fix)
        self._ext_watch = ExternalChangeWatcher(self)
        self._ext_watch.track("profiles", SettingsManager._profiles_path())
        self.settings_manager.disk_written.connect(lambda: self._ext_watch.mark("profiles"))
        self._ext_watch.changed.connect(self._on_external_change)
        self._extra_tools.cache.driver_changed.connect(
            lambda msg: self.statusBar().showMessage(msg, 20000))
        SYSHUB.info_changed.connect(self._on_sysinfo)
        FAVS.changed.connect(self._on_favs_changed)
        install_fav_menu(self._settings_list, "drs")
        install_fav_menu(self._env_widget, "env")
        install_fav_menu(self._arch_list, "arch", key_of=lambda a: getattr(a, "code", None))
        self._seen_set_ids = frozenset()
        self._seen_env_keys = frozenset()

        # Initial population
        self._populate_settings()
        self._populate_arch_list()

        # Wire env_widget into output_bar so Copy All includes env vars
        self._output_bar._env_widget = self._env_widget
        self._output_bar._gamescope_widget = self._gamescope
        self._gamescope.changed.connect(self._output_bar._update)
        self._output_bar._update()
        # Per-tab right-panel memory
        self._tab_right_memory: Dict[int, int] = {}

        self._install_shortcuts()
        self._refresh_badges()
        self._update_window_title()
        self._restore_ui_state()
        self._restore_session()
        self._chips.setVisible(self._sidebar_stack.currentIndex() in (self.TAB_DRS, self.TAB_ARCH, self.TAB_ENV))
        QTimer.singleShot(300, SYSHUB.refresh)
        self._schedule_lint()
        self._timeline = UndoTimeline(self.capture_state())
        self._update_undo_ui()
        self.setAcceptDrops(True)
        self._install_a11y()

    # ===== Feature pack: launch payload / lint / system / favorites / palette =====
    def collect_launch_payload(self) -> dict:
        """Everything the launch string is made of, as plain data."""
        env: Dict[str, str] = {}
        arch = self.settings_manager.get_arch()
        if arch:
            env["DXVK_NVAPI_GPU_ARCH"] = arch.code
        drs = self.settings_manager.get_settings_list()
        sstr = self.settings_manager.get_settings_string()
        if sstr:
            env["DXVK_NVAPI_DRS_SETTINGS"] = sstr
        env.update(self._env_widget.get_env_dict())
        gs_on = self._gamescope.is_enabled()
        return {"env": env, "drs": drs, "arch": arch.code if arch else "",
                "gs_on": gs_on,
                "gs_flags": self._gamescope.get_flag_values() if gs_on else {},
                "mangohud": self._gamescope.mangohud_enabled(),
                "gs_cmd": self._gamescope.build_command() if gs_on else ""}

    def set_env_var(self, name: str, value: str):
        """Set (or, with an empty value, clear) an environment variable programmatically."""
        if value:
            self._env_widget.set_value(name, value)
        else:
            self._env_widget.clear_value(name)
        if getattr(self._env_editor, "_current_name", "") == name:
            ev = env_def(name)
            if ev:
                self._env_editor.set_var(ev)

    def _refresh_sync_previews(self):
        # Runs 200 ms after every edit; the Steam/Heroic previews re-read and
        # re-parse localconfig.vdf / JSON each time. Only worth it while shown —
        # _switch_tab() refreshes when the Profiles tab is opened.
        if not self._sync_tabs.isVisible():
            return
        w = self._sync_tabs.currentWidget()
        if isinstance(w, QScrollArea):
            w = w.widget()
        for fn in ("refresh_preview", "refresh"):
            if hasattr(w, fn):
                getattr(w, fn)()
                break

    def _schedule_lint(self, *_):
        self._lint_timer.start()

    def _set_lint_button(self, n_err: int, n_warn: int):
        if n_err:
            txt, col = f"\u25cf {n_err} error{'s' if n_err != 1 else ''}", T["danger"]
        elif n_warn:
            txt, col = f"\u25b2 {n_warn} warn", T["warn"]
        else:
            txt, col = "\u2713 Lint", T["muted"]
        self._lint_btn.setText(txt)
        self._lint_btn.setStyleSheet(
            pill_ss(col))

    # -- external change notices (item: profiles.json / selected Lutris YAML) --
    def _ext_track_lutris(self, path):
        self._ext_watch.track("lutris", path)

    def _ext_mark_lutris(self):
        self._ext_watch.mark("lutris")

    def _on_external_change(self, key: str):
        sb = self.statusBar()
        if key == "profiles":
            def do(merge):
                sb.showMessage(self.settings_manager.reload_from_disk(merge), 8000)
            self._ext_banner.post(
                "profiles", "profiles.json was changed outside DRSTool (another instance or a script). "
                "Saving now would overwrite those changes.",
                [("Reload from disk", lambda: do(False)), ("Merge (add new)", lambda: do(True))])
        elif key == "lutris":
            lw = self._lutris_sync_widget
            def refresh():
                lw.refresh_yaml_from_disk()
                sb.showMessage("Lutris preview refreshed from disk.", 5000)
            def load():
                lw.refresh_yaml_from_disk()
                lw._import_from_yaml()
            self._ext_banner.post(
                "lutris", "The selected Lutris game config changed on disk outside DRSTool.",
                [("Refresh preview", refresh), ("Load into DRSTool…", load)])

    def _run_lint(self):
        p = self.collect_launch_payload()
        try:
            issues = lint_config(p["env"], p["drs"], p["arch"], p["gs_on"], p["gs_flags"],
                                 p["mangohud"], SYSHUB.info)
        except Exception as ex:                      # a linter bug must never break the editor
            issues = [LintIssue("info", "lint-crash", f"Linter failed: {ex}")]
        self._extra_tools.linter.set_issues(issues)
        self._set_lint_button(sum(1 for i in issues if i.sev == "error"),
                              sum(1 for i in issues if i.sev == "warn"))

    def _open_linter(self):
        self._switch_tab(self.TAB_TOOLS)
        self._extra_tools.show_tab("Linter")

    def _apply_lint_fix(self, issue):
        for fx in issue.fixes:
            kind = fx[0]
            if kind == "env_unset":
                self.set_env_var(fx[1], "")
            elif kind == "env_set":
                self.set_env_var(fx[1], fx[2])
            elif kind == "drs_unset":
                self.settings_manager.remove_setting(fx[1])
            elif kind == "gamescope_off":
                self._gamescope._enable_chk.setChecked(False)
            elif kind == "arch_unset":
                self.settings_manager.set_arch(None)
            elif kind == "drs_set":
                s_ = drs_lookup(fx[1])
                self.settings_manager.set_setting(canonical_drs_key(fx[1]), canonical_drs_value(s_, fx[2]))
        self._pending_label = f"Lint fix: {issue.fix_label}"
        self.statusBar().showMessage(f"Applied fix: {issue.fix_label}", 3000)
        self._run_lint()

    def _on_sysinfo(self, info):
        if info.probed:
            gpu = (info.nv_gpus[0] if info.nv_gpus else
                   next((g.name for g in info.vk_gpus if g.is_nvidia), "")) or "no NVIDIA GPU"
            parts = [gpu, f"driver {info.nv_driver}" if info.nv_driver else "", info.session or ""]
            self._sys_lbl.setText("  \u00b7  ".join(p for p in parts if p))
        self._refresh_availability()
        self._populate_arch_list()
        if self.settings_manager.get_arch():
            self._arch_detail.set_arch(self.settings_manager.get_arch())
        self._schedule_lint()
        if info.probed:
            drv = info.nv_driver or "no NVIDIA driver"
            self.statusBar().showMessage(
                f"System probe: {drv}" + (f" \u00b7 Vulkan: {len(info.vk_gpus)} GPU(s)" if info.vk_ok else ""), 4000)

    def _refresh_availability(self):
        tab = self._sidebar_stack.currentIndex()
        self._populate_settings(self._search.text() if tab == self.TAB_DRS else "", quiet=True)
        self._env_widget.populate(self._search.text() if tab == self.TAB_ENV else "")
        cur = self._setting_editor._current_setting
        if cur is not None:
            self._setting_editor.set_setting(cur)
        name = getattr(self._env_editor, "_current_name", "")
        ev = env_def(name)
        if ev:
            self._env_editor.set_var(ev)

    def _on_favs_changed(self):
        tab = self._sidebar_stack.currentIndex()
        self._populate_settings(self._search.text() if tab == self.TAB_DRS else "", quiet=True)
        self._env_widget.populate(self._search.text() if tab == self.TAB_ENV else "")
        self._populate_arch_list()

    def _on_chips(self):
        so, sf = self._chip_set.isChecked(), self._chip_fav.isChecked()
        for lw in (self._settings_list, self._env_widget):
            lw.only_set, lw.only_fav = so, sf
        tab = self._sidebar_stack.currentIndex()
        self._populate_settings(self._search.text() if tab == self.TAB_DRS else "", quiet=True)
        self._env_widget.populate(self._search.text() if tab == self.TAB_ENV else "")
        self._populate_arch_list()

    def _toggle_current_fav(self):
        tab = self._sidebar_stack.currentIndex()
        kind, key = None, None
        if tab == self.TAB_DRS and self._settings_list.currentItem():
            kind, key = "drs", self._settings_list.currentItem().data(Qt.UserRole)
        elif tab == self.TAB_ENV and self._env_widget.currentItem():
            kind, key = "env", self._env_widget.currentItem().data(Qt.UserRole)
        elif tab == self.TAB_ARCH and self._arch_list.currentItem():
            a = self._arch_list.currentItem().data(Qt.UserRole)
            kind, key = "arch", getattr(a, "code", None)
        if not key or (isinstance(key, str) and key.startswith("__")):
            return
        now = FAVS.toggle(kind, key)
        verb = "\u2605 Added" if now else "Removed"
        self.statusBar().showMessage(f"{verb} favorite: {key}", 2500)

    def _palette_items(self) -> List[dict]:
        items: List[dict] = []
        state = self.settings_manager.get_settings_list()
        for s in self.all_settings:
            cur = state.get(s.id)
            items.append(dict(kind="drs", key=s.id, title=s.name, sub=f"DRS \u00b7 {s.cat}", desc=s.desc,
                              is_set=cur is not None,
                              value=_fmt_drs_value(s, cur) if cur is not None else "",
                              fav=FAVS.is_fav("drs", s.id), fav_kind="drs", fav_key=s.id,
                              unavail=drs_unavailable(s)))
        vals = self._env_widget.get_env_dict()
        for ev in ALL_ENV_VARS:
            items.append(dict(kind="env", key=ev.name, title=ev.name, sub=f"Env \u00b7 {ev.cat}", desc=ev.desc,
                              is_set=ev.name in vals, value=vals.get(ev.name, ""),
                              fav=FAVS.is_fav("env", ev.name), fav_kind="env", fav_key=ev.name,
                              unavail=env_unavailable(ev)))
        sel = self.settings_manager.get_arch()
        for a in GPU_ARCHS:
            items.append(dict(kind="arch", key=a.code, title=f"{a.name} ({a.arch})", sub="GPU arch",
                              desc=a.example, is_set=bool(sel and sel.code == a.code), value=a.code,
                              fav=FAVS.is_fav("arch", a.code), fav_kind="arch", fav_key=a.code))
        items.append(dict(kind="gamescope", key="gamescope", title="Gamescope launch flags", sub="Builder",
                          desc="gamescope CLI flags", is_set=self._gamescope.is_enabled()))
        # Built from the real tab bars, so new tools never go missing from Ctrl+K.
        for title in self._extra_tools.tab_titles():
            items.append(dict(kind="tool", key=title, title=title, sub="Extra Tools", desc=""))
        for i in range(self._sync_tabs.count()):
            title = self._sync_tabs.tabText(i)
            shown = f"{title} sync" if title in ("Lutris", "Steam", "Heroic", "Bottles") else title
            items.append(dict(kind="sync", key=title, title=shown, sub="Profiles \u203a sync", desc=""))
        cur = self.capture_state()
        for r in RECIPES:
            _done, _total, full = recipe_coverage(r, cur)
            items.append(dict(kind="recipe", key=r.rid, title=f"Recipe: {r.title}", sub="Recipe",
                              desc=r.summary + " " + " ".join(r.tags),
                              is_set=full,
                              unavail=recipe_requirement(r)))
        for key, title, desc in (
                ("undo", "Undo", "Ctrl+Z"), ("redo", "Redo", "Ctrl+Shift+Z"),
                ("import", "Import a launch line…", "paste Steam launch options / shell snippet  Ctrl+Shift+V"),
                ("share_import", "Import a share code…", "paste a DRSTOOL1: profile code from someone else"),
                ("share_copy", "Copy share code", "the current editor state as one line of text to share"),
                ("explain", "Explain the current launch line", "what every token does"),
                ("recipes", "Recipes…", "curated bundles  Ctrl+R"),
                ("changes", "Review unsaved changes", "diff editor vs saved profile"),
                ("logging", "Toggle logging", "all log levels on/off"),
                ("copy_steam", "Copy Steam launch options", "env … gamescope … -- %command%"),
                ("save", "Save profile", "Ctrl+S"),
                ("shortcuts", "Keyboard shortcuts", "F1"),
                ("appearance", "Appearance…", "theme, accent, density, UI scale  Ctrl+,"),
                ("tasks", "Background tasks", "running and finished downloads / scans"),
                ("support", "Create support bundle…", "system info, debug log and anonymised profiles in one zip")):
            items.append(dict(kind="action", key=key, title=title, sub="Action", desc=desc))
        return items

    def _open_palette(self):
        dlg = CommandPalette(self._palette_items(), self, self._palette_recent())
        g = self.geometry()
        dlg.move(g.center().x() - dlg.width() // 2, g.top() + 90)
        if dlg.exec() == QDialog.Accepted and dlg.chosen:
            self._palette_remember(dlg.chosen)
            self.navigate_to(dlg.chosen)

    def navigate_to(self, it: dict):
        kind, key = it["kind"], it["key"]
        if kind in ("drs", "env", "arch"):
            for c in (self._chip_set, self._chip_fav):          # never hide the target
                c.blockSignals(True)
                c.setChecked(False)
                c.blockSignals(False)
            for lw in (self._settings_list, self._env_widget):
                lw.only_set = lw.only_fav = False
            self._search.blockSignals(True)
            self._search.clear()
            self._search.blockSignals(False)
        if kind == "drs":
            self._switch_tab(self.TAB_DRS)
            self._select_item(self._settings_list, key)
        elif kind == "env":
            self._switch_tab(self.TAB_ENV)
            self._select_item(self._env_widget, key)
        elif kind == "arch":
            self._switch_tab(self.TAB_ARCH)
            for i in range(self._arch_list.count()):
                a = self._arch_list.item(i).data(Qt.UserRole)
                if getattr(a, "code", None) == key:
                    self._arch_list.scrollToItem(self._arch_list.item(i), QListWidget.PositionAtCenter)
                    break
        elif kind == "gamescope":
            self._switch_tab(self.TAB_ENV)
            self._right_stack.setCurrentIndex(self.PAGE_GAMESCOPE)
        elif kind == "tool":
            self._switch_tab(self.TAB_TOOLS)
            self._extra_tools.show_tab(key)
        elif kind == "sync":
            self._switch_tab(self.TAB_PROFILES)
            for i in range(self._sync_tabs.count()):
                if self._sync_tabs.tabText(i) == key:
                    self._sync_tabs.setCurrentIndex(i)
                    break
        elif kind == "recipe":
            r = next((x for x in RECIPES if x.rid == key), None)
            if r:
                self.apply_recipe(r)
        elif kind == "action":
            {"undo": self.undo, "redo": self.redo, "import": self.open_import_dialog,
             "share_import": self.open_share_import, "share_copy": self.copy_share_code,
             "explain": self.open_explain_dialog, "recipes": self.open_recipes,
             "changes": self._show_unsaved_changes, "logging": self._toggle_logging,
             "copy_steam": self._output_bar._copy_steam_env, "save": self._shortcut_save,
             "shortcuts": self.open_shortcuts, "appearance": self.open_appearance,
             "tasks": self._tasks_menu, "support": self._create_support_bundle}[key]()

    def _select_item(self, lw: QListWidget, key: str):
        for i in range(lw.count()):
            item = lw.item(i)
            if item.data(Qt.UserRole) == key:
                lw.setCurrentItem(item)
                lw.scrollToItem(item, QListWidget.PositionAtCenter)
                return

    # ===== Feature pack 4: editor state, undo/redo, import, recipes =====
    def capture_state(self) -> dict:
        arch = self.settings_manager.get_arch()
        return {"settings": self.settings_manager.get_settings_list(),
                "arch": arch.code if arch else None,
                "env": self._env_widget.get_env_dict(),
                "gs_on": self._gamescope.is_enabled(),
                "gs_flags": self._gamescope.get_raw_flag_values()}

    def apply_state(self, st: dict, label: Optional[str] = None):
        """Put a whole state into the editor. With a label it becomes an undo step."""
        self._state_timer.stop()
        self._restoring = True
        try:
            self._env_editor.discard_pending_edit()
            self.settings_manager.replace_state(st["settings"], st["arch"])
            self._env_widget.load_values(st["env"])
            self._gamescope.load_flag_values(st["gs_on"], st["gs_flags"])
            self._env_widget.set_gamescope_enabled(st["gs_on"])
            tab = self._sidebar_stack.currentIndex()
            self._populate_settings(self._search.text() if tab == self.TAB_DRS else "", quiet=True)
            self._env_widget.populate(self._search.text() if tab == self.TAB_ENV else "")
            self._seen_env_keys = frozenset(self._env_widget.get_env_dict())
            self._populate_arch_list()
            name = getattr(self._env_editor, "_current_name", "")
            ev = env_def(name)
            if ev:
                self._env_editor.set_var(ev)
            cur = self._setting_editor._current_setting
            if cur is not None:
                self._setting_editor.set_setting(cur)
            self._refresh_badges()
            self._output_bar._update()
        finally:
            self._restoring = False
        self._state_timer.stop()
        if label is not None:
            self._pending_label = label
        self._on_state_settled()

    def _on_state_settled(self):
        if self._timeline is None:
            return
        cur = self.capture_state()
        prev = self._timeline.current()
        if cur != prev:
            label = self._pending_label or describe_change(prev, cur)
            key = None if self._pending_label else single_change_key(prev, cur)
            self._timeline.push(cur, label, key)
        self._pending_label = None
        self._update_undo_ui()
        self._reeval_dirty(cur)

    def _saved_state(self) -> Optional[dict]:
        name = self.settings_manager.get_current_profile()
        prof = self.settings_manager.get_profiles().get(name) if name else None
        return state_from_profile(prof) if isinstance(prof, dict) else None

    def _reeval_dirty(self, cur: Optional[dict] = None):
        """Dirty = the editor differs from what is saved — so undoing back to the
        saved state (or re-setting the same value) clears the marker again."""
        saved = self._saved_state()
        if saved is None:
            return
        cur = cur or self.capture_state()
        dirty = canon_flat(state_as_profile(cur)) != canon_flat(state_as_profile(saved))
        if dirty != self._dirty:
            self._set_dirty(dirty)

    def undo(self):
        if self._state_timer.isActive():            # an edit is still settling: record it first
            self._state_timer.stop()
            self._on_state_settled()
        if not self._timeline or not self._timeline.can_undo():
            self.statusBar().showMessage("Nothing to undo", 2000)
            return
        what = self._timeline.undo_label()
        self.apply_state(self._timeline.undo())
        self.statusBar().showMessage(f"Undid: {what}", 4000)

    def redo(self):
        if not self._timeline or not self._timeline.can_redo():
            self.statusBar().showMessage("Nothing to redo", 2000)
            return
        what = self._timeline.redo_label()
        self.apply_state(self._timeline.redo())
        self.statusBar().showMessage(f"Redid: {what}", 4000)

    def _jump_history(self, idx: int):
        if self._state_timer.isActive():
            self._state_timer.stop()
            self._on_state_settled()
        st = self._timeline.jump(idx) if self._timeline else None
        if st is not None:
            self.apply_state(st)
            self.statusBar().showMessage("Jumped in history", 3000)

    def _update_undo_ui(self):
        tl = self._timeline
        can_u, can_r = bool(tl and tl.can_undo()), bool(tl and tl.can_redo())
        self._undo_btn.setEnabled(bool(tl))
        self._undo_btn.setText("↶")
        self._undo_btn.setToolTip(f"Undo: {tl.undo_label()}  (Ctrl+Z)\nArrow: history" if can_u
                                  else "Nothing to undo — arrow: history")
        self._redo_btn.setEnabled(can_r)
        self._redo_btn.setToolTip(f"Redo: {tl.redo_label()}  (Ctrl+Shift+Z)" if can_r else "Nothing to redo")

    def _fill_history_menu(self):
        m = self._undo_menu
        m.clear()
        if not self._timeline:
            return
        entries = self._timeline.entries()
        shown = entries[-30:]
        if len(entries) > len(shown):
            a = m.addAction(f"… {len(entries) - len(shown)} older step(s)")
            a.setEnabled(False)
        for idx, label, ts, is_cur in reversed(shown):
            a = m.addAction(f"{'●' if is_cur else '   '}  {label}    ·  {datetime.fromtimestamp(ts):%H:%M:%S}")
            if is_cur:
                a.setEnabled(False)
            else:
                a.triggered.connect(lambda _c=False, i=idx: self._jump_history(i))

    def _chip_text(self, glyph: str, text: str, tail: str = "") -> str:
        fm = self._profile_chip.fontMetrics()
        return f"{glyph}  " + fm.elidedText(text, Qt.ElideMiddle, 150) + tail

    def _style_chip(self, color: str, bold: bool):
        self._profile_chip.setStyleSheet(
            f"QPushButton{{color:{color};background:{T['surface']};border:1px solid {T['border_hi']};"
            f"border-radius:14px;font-size:12px;font-weight:{600 if bold else 500};padding:5px 14px;}}"
            f"QPushButton:hover{{border-color:{color};background:{T['raised']};}}")

    def _show_unsaved_changes(self):
        name = self.settings_manager.get_current_profile()
        saved = self._saved_state()
        if not name or saved is None:
            self._switch_tab(self.TAB_PROFILES)
            return
        dlg = ChangesDialog(state_as_profile(saved), state_as_profile(self.capture_state()),
                            "Saved", "Editor", f"Unsaved changes — “{name}”", "Revert to saved", self)
        if dlg.exec() == QDialog.Accepted and dlg.revert:
            self.settings_manager.load_profile(name)
            self._on_profile_loaded(name)
            self._pending_label = f"Revert to saved “{name}”"

    def open_import_dialog(self, text: str = ""):
        if not text:
            clip = QApplication.clipboard().text()
            if share_code and share_code.looks_like_code(clip):
                self.open_share_import(clip)
                return
            text = clip if looks_like_launch_text(clip) else ""
        elif share_code and share_code.looks_like_code(text):
            self.open_share_import(text)
            return
        dlg = PlanDialog(self.capture_state(), "paste", "Import a launch line", text=text, parent=self)
        if dlg.exec() == QDialog.Accepted and dlg.result_state is not None:
            self.apply_state(dlg.result_state, "Import launch line")
            self.statusBar().showMessage("Imported launch line — Ctrl+Z to undo", 5000)

    def open_share_import(self, text: str = ""):
        self._profiles_widget.open_share_import(text)

    def copy_share_code(self):
        """Share code of the editor as it is now (saved or not)."""
        if _CORE_ERR:
            toast(_CORE_ERR, "error", 8000)
            return
        name = self.settings_manager.get_current_profile() or "Shared profile"
        try:
            code = profile_share_code(name, state_as_profile(self.capture_state()))
        except share_code.ShareCodeError as ex:
            toast(f"Nothing to share: {ex}", "warn")
            return
        QApplication.clipboard().setText(code)
        toast(f"Copied share code of the current editor state ({len(code)} characters)", "ok")

    def open_explain_dialog(self):
        p = self.collect_launch_payload()
        line = build_launch_line(p, "%command%", False) if (p["env"] or p["gs_cmd"]) else ""
        if not line:
            self.statusBar().showMessage("The launch line is empty — nothing to explain", 3000)
            return
        PlanDialog(self.capture_state(), "explain", "Explain the launch line",
                   "Every part of the current launch string, decoded with its catalog description.",
                   text=line, parent=self).exec()

    def open_recipes(self):
        dlg = RecipeGallery(self.capture_state(), self)
        if dlg.exec() == QDialog.Accepted and dlg.chosen:
            self.apply_recipe(dlg.chosen)

    def apply_recipe(self, r: "Recipe"):
        ops = recipe_ops(r)
        why = recipe_requirement(r)
        sub = r.summary + (f"   ⚠ {why}" if why else "")
        dlg = PlanDialog(self.capture_state(), "recipe", f"Recipe: {r.title}", sub, ops=ops,
                         gs_replace=r.gs_replace, parent=self)
        if dlg.exec() == QDialog.Accepted and dlg.result_state is not None:
            self.apply_state(dlg.result_state, f"Recipe: {r.title}")
            self.statusBar().showMessage(f"Applied recipe “{r.title}” — Ctrl+Z to undo", 5000)

    # Drop a launch line (text) anywhere on the window → import preview
    def dragEnterEvent(self, e):          # noqa: N802
        md = e.mimeData()
        if md.hasText() and not md.hasUrls() and (
                looks_like_launch_text(md.text())
                or (share_code is not None and share_code.looks_like_code(md.text()))):
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dropEvent(self, e):               # noqa: N802
        md = e.mimeData()
        if md.hasText() and not md.hasUrls():
            e.acceptProposedAction()
            QTimer.singleShot(0, lambda t=md.text(): self.open_import_dialog(t))
        else:
            super().dropEvent(e)

    # ===== Games: game card and profile ↔ game links =====
    def _open_game(self, game):
        self._game_card.set_game(game)
        if self._sidebar_stack.currentIndex() == self.TAB_GAMES:
            self._right_stack.setCurrentIndex(self.PAGE_GAMES)

    def _on_game_written(self, target: dict):
        """A sync tab wrote the editor to a game: remember which profile it was."""
        key = game_key_for_target(target)
        if not key:
            return
        LINKS.record(key, name=plain_title(target.get("title", "")),
                     launcher=str(target.get("kind", "")).capitalize(), target=target,
                     profile=self.settings_manager.get_current_profile(), fp=state_fp(self.capture_state()))

    def _apply_target(self, target: dict) -> tuple:
        w = {"lutris": self._lutris_sync_widget, "steam": self._steam_sync,
             "heroic": self._heroic_sync, "bottles": self._bottles_sync}.get(target.get("kind"))
        if w is None:
            return False, f"no sync tab for “{target.get('kind')}”"
        return w.apply_target(target)

    def _confirm_load_for_write(self, profile: str) -> bool:
        """Writing a profile writes the editor, so the saved profile has to be in it."""
        if self.settings_manager.get_current_profile() == profile and not self._dirty:
            return True
        msg = f"“{profile}” is loaded into the editor first; that is what gets written."
        if self._dirty:
            msg += ("\n\nThe editor has unsaved changes"
                    f" to “{self.settings_manager.get_current_profile()}”. They are replaced "
                    "(Ctrl+Z brings them back).")
        if QMessageBox.question(self, "Load profile", msg, QMessageBox.Ok | QMessageBox.Cancel,
                                QMessageBox.Ok) != QMessageBox.Ok:
            return False
        return self.load_profile_by_name(profile)

    def write_profile_to_games(self, profile: Optional[str], items: List[tuple], confirm: bool = True) -> list:
        """Write a saved profile (None = the editor as it is) to [(game key, name, target)].
        -> [(name, ok, message)]"""
        if not items:
            return []
        if profile:
            if profile not in self.settings_manager.get_profiles():
                QMessageBox.warning(self, "Write to game", f"Profile “{profile}” does not exist any more.")
                return []
            if confirm:
                names = "\n".join(f"  • {n}" for _k, n, _t in items)
                if QMessageBox.question(
                        self, "Write to game",
                        f"Write “{profile}” to:\n\n{names}\n\nA timestamped backup of each file is kept.",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
                    return []
            if not self._confirm_load_for_write(profile):
                return []
        elif confirm and QMessageBox.question(
                self, "Write to game",
                "No profile is linked — write the editor's current configuration to:\n\n"
                + "\n".join(f"  • {n}" for _k, n, _t in items) + "\n\nA timestamped backup is kept.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
            return []
        results = []
        for _key, name, target in items:
            ok, msg = self._apply_target(target)
            results.append((name, ok, msg))
        bad = [r for r in results if not r[1]]
        what = f"“{profile}”" if profile else "the editor"
        if not bad:
            toast(f"Wrote {what} to {len(results)} game(s)", "ok", 6000)
        else:
            QMessageBox.warning(
                self, "Write to game",
                f"Wrote {what} to {len(results) - len(bad)} of {len(results)} game(s).\n\n"
                + "\n".join(f"{'✔' if ok else '✖'} {n}: {m}" for n, ok, m in results))
        return results

    def load_profile_for_game(self, name: str):
        if self._dirty and QMessageBox.question(
                self, "Load profile",
                f"Load “{name}”? The editor's unsaved changes are replaced (Ctrl+Z brings them back).",
                QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok) != QMessageBox.Ok:
            return
        if self.load_profile_by_name(name):
            toast(f"Loaded “{name}” into the editor", "ok")

    def open_linked_games(self, name: str):
        """List the games that use a profile and update the chosen ones."""
        profiles = self.settings_manager.get_profiles()
        if name not in profiles:
            return
        dlg = LinkedGamesDialog(name, profiles, self)
        if dlg.exec() == QDialog.Accepted and dlg.chosen:
            self.write_profile_to_games(name, dlg.chosen, confirm=False)

    def _suggest_linked_update(self, name: str):
        prof = self.settings_manager.get_profiles().get(name)
        if not isinstance(prof, dict):
            return
        keys = [k for k in LINKS.outdated(name, profile_fp(prof)) if LINKS.get(k).get("target")]
        if keys:
            n = len(keys)
            toast(f"{n} game{'s' if n != 1 else ''} use “{name}” with an older version of it",
                  "info", 15000, (f"Update {n} game{'s' if n != 1 else ''}…", lambda: self.open_linked_games(name)))

    # ===== Shortcuts / persisted UI state =====
    def _install_shortcuts(self):
        def sc(seq, fn, parent=None, ctx=Qt.WindowShortcut):
            s_ = QShortcut(QKeySequence(seq), parent or self)
            s_.setContext(ctx)
            s_.activated.connect(fn)
            return s_

        sc("Ctrl+F", self._focus_search)
        sc("Escape", self._clear_search, self._search, Qt.WidgetShortcut)
        for i in range(len(self._nav_tabs)):
            sc(f"Ctrl+{i + 1}", lambda i=i: self._switch_tab(i))
        sc("Ctrl+S", self._shortcut_save)
        sc("Ctrl+Shift+C", lambda: self._output_bar._copy_all(steam=False))
        sc("Ctrl+K", self._open_palette)
        sc("Ctrl+D", self._toggle_current_fav)
        sc("Ctrl+Z", self.undo)
        sc("Ctrl+Shift+Z", self.redo)
        sc("Ctrl+Y", self.redo)
        sc("Ctrl+Shift+V", self.open_import_dialog)
        sc("Ctrl+R", self.open_recipes)
        sc("F1", self.open_shortcuts)
        sc("Ctrl+/", self.open_shortcuts)
        sc("Ctrl+,", self.open_appearance)

    # ===== Feature pack 6: help, appearance, tasks, session, accessibility =====
    def open_shortcuts(self):
        ShortcutsDialog(self).exec()

    def open_appearance(self):
        dlg = AppearanceDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.restart_requested:
            self._restart_now()
        elif dlg.needs_restart:
            toast("Appearance saved \u2014 it applies after a restart", "ok", 9000,
                  ("Restart now", self._restart_now))
        else:
            toast("Settings saved", "ok")

    def _restart_now(self):
        self._restart_pending = True
        self.close()                     # asks about unsaved changes first; closeEvent starts the new copy

    def _update_tasks_btn(self):
        ts = TASKS.tasks()
        if not ts:
            self._tasks_btn.hide()
            return
        run = [t for t in ts if t["state"] == "running"]
        if run:
            txt, col = f"\u27f3 {len(run)}", T["accent"]
            tip = "Running: " + ", ".join(t["title"] for t in run)
        else:
            bad = ts[-1]["state"] == "failed"
            txt, col = ("\u2716" if bad else "\u2714"), (T["danger"] if bad else T["muted"])
            tip = "Last task: " + ts[-1]["title"]
        self._tasks_btn.setText(txt)
        self._tasks_btn.setToolTip(tip + "\nClick for the task list")
        self._tasks_btn.setAccessibleName("Background tasks")
        self._tasks_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:1px solid {T['border_hi']};border-radius:8px;"
            f"color:{col};padding:2px 10px;font-weight:700;}}QPushButton:hover{{border-color:{col};}}")
        self._tasks_btn.show()

    def _tasks_menu(self):
        m = QMenu(self)
        ts = TASKS.tasks()
        if not ts:
            m.addAction("No background tasks yet").setEnabled(False)
        now = time.monotonic()
        for t in reversed(ts):
            glyph = {"running": "\u27f3", "done": "\u2714", "failed": "\u2716"}.get(t["state"], "\u2022")
            tail = t["detail"] or {"running": "running\u2026", "done": "done", "failed": "failed"}[t["state"]]
            a = m.addAction(f"{glyph}  {t['title']}  \u2014  {tail}  ({(t['t1'] or now) - t['t0']:.0f}s)")
            if t["nav"]:
                a.triggered.connect(lambda _c=False, n=t["nav"]: self.navigate_to(n))
        m.addSeparator()
        m.addAction("Clear finished").triggered.connect(TASKS.clear_finished)
        anchor = self._tasks_btn if self._tasks_btn.isVisible() else self._menu_btn
        m.exec(anchor.mapToGlobal(QPoint(0, anchor.height())))

    def load_profile_by_name(self, name: str) -> bool:
        if name not in self.settings_manager.get_profiles():
            return False
        self.settings_manager.load_profile(name)
        if self.settings_manager.get_current_profile() != name:
            return False
        self._on_profile_loaded(name)
        self._profiles_widget._refresh()
        return True

    def _palette_recent(self) -> List[str]:
        v = self._qsettings.value("palette/recent", [])
        if isinstance(v, str):
            v = [v] if v else []
        return [str(x) for x in (v or [])]

    def _palette_remember(self, it: dict):
        rid = f'{it["kind"]}|{it["key"]}'
        self._qsettings.setValue("palette/recent", ([rid] + [x for x in self._palette_recent() if x != rid])[:12])

    def _save_session(self):
        qs = self._qsettings
        qs.setValue("session/profile", self.settings_manager.get_current_profile() or "")
        qs.setValue("session/chip_set", self._chip_set.isChecked())
        qs.setValue("session/chip_fav", self._chip_fav.isChecked())
        tab = self._sidebar_stack.currentIndex()
        key = ""
        if tab == self.TAB_DRS and self._settings_list.currentItem():
            key = str(self._settings_list.currentItem().data(Qt.UserRole) or "")
        elif tab == self.TAB_ENV and self._env_widget.currentItem():
            key = str(self._env_widget.currentItem().data(Qt.UserRole) or "")
        qs.setValue("session/key", key)
        qs.setValue("session/key_tab", tab)
        tabs = self._extra_tools._tabs
        qs.setValue("session/tool", tabs.tabText(tabs.currentIndex()).replace(" \u26a0", ""))
        qs.setValue("session/sync", self._sync_tabs.tabText(self._sync_tabs.currentIndex()))

    def _restore_session(self):
        if _base._CLI_MODE or not APPEARANCE["restore"]:
            return
        qs = self._qsettings
        try:
            name = str(qs.value("session/profile", "") or "")
            if name:
                self.load_profile_by_name(name)
            if _qbool(qs.value("session/chip_set", False)):
                self._chip_set.setChecked(True)
            if _qbool(qs.value("session/chip_fav", False)):
                self._chip_fav.setChecked(True)
            tab = self._sidebar_stack.currentIndex()
            key = str(qs.value("session/key", "") or "")
            if key and int(qs.value("session/key_tab", -1)) == tab:
                if tab == self.TAB_DRS:
                    self._select_item(self._settings_list, key)
                elif tab == self.TAB_ENV:
                    self._select_item(self._env_widget, key)
            if tab == self.TAB_TOOLS:
                tool = str(qs.value("session/tool", "") or "")
                if tool:
                    self._extra_tools.show_tab(tool)
            elif tab == self.TAB_PROFILES:
                sync = str(qs.value("session/sync", "") or "")
                for i in range(self._sync_tabs.count()):
                    if self._sync_tabs.tabText(i) == sync:
                        self._sync_tabs.setCurrentIndex(i)
                        break
        except Exception:
            _log.debug("ignored error", exc_info=True)
            pass

    def _install_a11y(self):
        self.setAccessibleName(APP_TITLE)
        for btn in self._nav_tabs:
            btn.setFocusPolicy(Qt.TabFocus)            # reachable with Tab, never steals focus on click
        for w, nm in ((self._undo_btn, "Undo"), (self._redo_btn, "Redo")):
            w.setAccessibleName(nm)
        for w in (self._undo_btn, self._redo_btn, self._recipes_btn, self._profile_chip):
            w.setFocusPolicy(Qt.TabFocus)
        self._menu_btn.setFocusPolicy(Qt.TabFocus)
        self._menu_btn.setAccessibleName("Menu: appearance, shortcuts, tasks")
        self._search.setAccessibleName("Filter the current list")
        for lw, nm in ((self._settings_list, "DRS settings list"), (self._env_widget, "Environment variables list"),
                       (self._arch_list, "GPU architecture list"), (self._flm_sidebar, "Extra tools list"),
                       (self._games_list.list, "Games list")):
            lw.setAccessibleName(nm)
        chain = [*self._nav_tabs, self._undo_btn, self._redo_btn, self._recipes_btn, self._log_btn,
                 self._lint_btn, self._tasks_btn, self._profile_chip, self._menu_btn, self._search,
                 self._chip_set, self._chip_fav]
        for a, b in zip(chain, chain[1:]):
            QWidget.setTabOrder(a, b)

    def _focus_search(self):
        if self._search.isEnabled():
            self._search.setFocus()
            self._search.selectAll()

    def _clear_search(self):
        self._search.clear()
        self._filter("")

    def _shortcut_save(self):
        if self.settings_manager.get_current_profile():
            self._output_bar._save_to_profile()
        else:
            self._switch_tab(self.TAB_PROFILES)
            self._profiles_widget._profile_name.setFocus()
            self.statusBar().showMessage("No profile loaded — type a name and press Enter to save one", 4000)

    def _restore_ui_state(self):
        try:
            geo = self._qsettings.value("window/geometry")
            if geo is not None:
                self.restoreGeometry(geo)
            else:
                self.resize(1440, 900)
            split = self._qsettings.value("window/splitter")
            if split is not None:
                self._splitter.restoreState(split)
            tab = int(self._qsettings.value("window/tab", 0))
            if 0 < tab < len(self._nav_tabs):
                self._switch_tab(tab)
        except Exception:
            _log.debug("ignored error", exc_info=True)
            self.resize(1440, 900)

    def resizeEvent(self, e):             # noqa: N802
        super().resizeEvent(e)
        if hasattr(self, "_brand"):
            self._brand.set_compact(self.width() < 1420)

    def closeEvent(self, event):          # noqa: N802
        restart, self._restart_pending = self._restart_pending, False
        if self._state_timer.isActive():
            self._state_timer.stop()
            self._on_state_settled()
        if self._dirty:
            name = self.settings_manager.get_current_profile() or ""
            box = QMessageBox.question(
                self, "Unsaved changes",
                f'Profile "{name}" has unsaved changes.\n\nSave them before closing?',
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save)
            if box == QMessageBox.Cancel:
                event.ignore()
                return
            if box == QMessageBox.Save:
                self._output_bar._save_to_profile()
                if self._dirty:          # save failed (profile_saved never fired)
                    event.ignore()
                    return
        try:
            self._qsettings.setValue("window/geometry", self.saveGeometry())
            self._qsettings.setValue("window/splitter", self._splitter.saveState())
            self._qsettings.setValue("window/tab", self._sidebar_stack.currentIndex())
            self._save_session()
        except Exception:
            _log.debug("ignored error", exc_info=True)
            pass
        try:
            SYSHUB.shutdown()
            self._game_card.shutdown()
            if self._extra_tools.is_built("dllmgr"):
                self._extra_tools.dllmgr.shutdown()
            self.hide()                  # downloads may need a moment to notice the cancel
            TASKS.wait_all()
        except Exception:
            _log.debug("ignored error", exc_info=True)
            pass
        if restart:
            _spawn_restart()
        super().closeEvent(event)

    # ===== Dirty tracking / badges =====
    def _mark_dirty(self, *_):
        self._state_timer.start()
        if self.settings_manager.get_current_profile() and not self._dirty and not self._restoring:
            self._set_dirty(True)

    def _set_dirty(self, dirty: bool):
        self._dirty = dirty
        self._output_bar.set_dirty(dirty)
        self._update_window_title()

    def _refresh_badges(self, *_):
        n_drs = len(self.settings_manager.get_settings_list())
        self._settings_tab.set_badge(str(n_drs) if n_drs else "")
        arch = self.settings_manager.get_arch()
        self._arch_tab.set_badge(arch.code if arch else "")
        n_env = len(self._env_widget.get_env_dict())
        if self._gamescope.is_enabled():
            n_env += 1
        self._env_tab.set_badge(str(n_env) if n_env else "")
        n_prof = len(self.settings_manager.get_profiles())
        self._profiles_tab.set_badge(str(n_prof) if n_prof else "")
        n_linked = sum(LINKS.counts().values())
        self._games_tab.set_badge(str(n_linked) if n_linked else "")

    # ===== Tab switching =====
    def _switch_tab(self, idx):
        # Save current right-panel index for the tab we're leaving
        prev = self._sidebar_stack.currentIndex()
        self._tab_right_memory[prev] = self._right_stack.currentIndex()

        self._sidebar_stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._nav_tabs):
            btn.setChecked(i == idx)

        # The filter only applies to the long lists.
        filterable = idx in (self.TAB_DRS, self.TAB_ENV, self.TAB_GAMES)
        self._search.setEnabled(filterable)
        self._search.setVisible(filterable)
        self._chips.setVisible(idx in (self.TAB_DRS, self.TAB_ARCH, self.TAB_ENV))

        # Re-apply whatever is typed in the search box to the list we just
        # switched to, so filtering never looks stale across tabs.
        if idx == self.TAB_DRS:
            self._populate_settings(self._search.text())
        elif idx == self.TAB_ENV:
            self._env_widget.populate(self._search.text())
        elif idx == self.TAB_ARCH:
            self._populate_arch_list()
        elif idx == self.TAB_GAMES:
            self._games_list.set_filter(self._search.text())

        _hint = self.EMPTY_HINTS.get(idx, "")
        self._placeholder.set_content({0: "\u25c8", 1: "\u25a3", 2: "\u2630", 5: "\u25b6"}.get(idx, "\u25c7"),
                                      *(_hint.split("\n", 1) + [""])[:2])

        if idx == self.TAB_PROFILES:
            self._right_stack.setCurrentIndex(self.PAGE_LUTRIS)
            self._refresh_sync_previews()
        elif idx == self.TAB_GAMES:
            self._right_stack.setCurrentIndex(self.PAGE_GAMES if self._game_card.game else self.PAGE_EMPTY)
        elif idx in self._tab_right_memory:
            self._right_stack.setCurrentIndex(self._tab_right_memory[idx])
        else:
            if idx == self.TAB_ARCH:
                arch = self.settings_manager.get_arch()
                if arch:
                    self._arch_detail.set_arch(arch)
                    self._right_stack.setCurrentIndex(self.PAGE_ARCH)
                else:
                    self._right_stack.setCurrentIndex(self.PAGE_EMPTY)
            elif idx == self.TAB_TOOLS:
                self._right_stack.setCurrentIndex(self.PAGE_TOOLS)
            else:
                self._right_stack.setCurrentIndex(self.PAGE_EMPTY)

    # ===== Settings =====
    def _populate_settings(self, filter_text: str = "", quiet: bool = False):
        current_item = self._settings_list.currentItem()
        current_id = current_item.data(Qt.UserRole) if current_item else None

        state = self.settings_manager.get_settings_list()
        custom = sorted(k for k in state if drs_lookup(k) is None)
        self._shown_custom_drs = frozenset(custom)
        self._settings_list.populate(self.all_settings + [custom_drs_setting(k) for k in custom],
                                     state, filter_text)
        self._seen_set_ids = frozenset(state)

        if current_id:
            for i in range(self._settings_list.count()):
                item = self._settings_list.item(i)
                if item.data(Qt.UserRole) == current_id:
                    if quiet:
                        self._settings_list.blockSignals(True)
                    self._settings_list.setCurrentItem(item)
                    if quiet:
                        self._settings_list.blockSignals(False)
                    break

    def _filter(self, text):
        # Route the search box to whichever list is actually visible. Before
        # this, typing in the search box always filtered the DRS Settings
        # list even while on the Env Vars tab — so filtering silently did
        # nothing for the 62 DXVK/VKD3D/NVIDIA __GL vars.
        active = self._sidebar_stack.currentIndex()
        if active == self.TAB_ENV:
            self._env_widget.populate(text)
        elif active == self.TAB_DRS:
            self._populate_settings(text)
        elif active == self.TAB_GAMES:
            self._games_list.set_filter(text)

    def _open_setting(self, setting_id):
        setting = self._settings_list._by_id.get(setting_id) or drs_lookup(setting_id)
        if setting:
            self._setting_editor.set_setting(setting)
            # Only steal the right panel if the DRS Settings sidebar page is
            # actually the one showing. Without this guard, anything that
            # re-fires currentItemChanged on the (hidden) settings list --
            # e.g. _populate_settings() restoring the previously-selected
            # item after a rebuild -- pops the setting editor into view even
            # while the user is looking at a completely different tab, like
            # Profiles. The editor's content is still kept up to date above;
            # we just don't force it on screen.
            if self._sidebar_stack.currentIndex() == self.TAB_DRS:
                self._right_stack.setCurrentIndex(self.PAGE_SETTING)

    def _on_setting_cleared(self):
        # Fired when the currently-open setting's value is removed (via the
        # Remove Setting button). The editor panel should stay visible,
        # showing the setting with nothing selected yet (SettingEditorWidget
        # already rebuilds itself for this) - it must NOT fall back to the
        # placeholder page, which used to hide the whole right-hand panel.
        pass

    def _open_env_var(self, var_name: str):
        if var_name == EnvVarsWidget.GAMESCOPE_KEY:
            # Gamescope's settings are real argv flags, not KEY=VALUE env
            # vars — show the CLI-flag builder panel instead.
            if self._sidebar_stack.currentIndex() == 2:
                self._right_stack.setCurrentIndex(4)
            return
        ev = env_def(var_name)
        if ev:
            self._env_editor.set_var(ev)
            # Same guard as _open_setting: don't yank the right panel onto
            # the env var editor unless the Env Vars sidebar page is what's
            # actually showing right now.
            if self._sidebar_stack.currentIndex() == 2:
                self._right_stack.setCurrentIndex(3)

    # ===== Architecture =====
    def _populate_arch_list(self):
        self._arch_list.populate(GPU_ARCHS, self.settings_manager.get_arch(),
                                 self._chip_set.isChecked(), self._chip_fav.isChecked())

    def _select_architecture(self, arch: GPUArch):
        self.settings_manager.set_arch(arch)
        # The arch_changed signal will update the UI, but we update immediately too
        self._arch_detail.set_arch(arch)
        self._right_stack.setCurrentIndex(2)
        self._populate_arch_list()
        self.statusBar().showMessage(f"Selected architecture: {arch.name} ({arch.code})")

    def _clear_architecture(self):
        self.settings_manager.set_arch(None)
        # The arch_changed signal will update the UI
        self._right_stack.setCurrentIndex(0)
        self._populate_arch_list()
        self.statusBar().showMessage("Architecture cleared")

    def _update_arch_ui(self):
        """Called when arch changes (via settings_manager.arch_changed)."""
        arch = self.settings_manager.get_arch()
        self._populate_arch_list()
        if arch:
            self._arch_detail.set_arch(arch)
            # Only switch to arch detail if the arch tab is active
            if self._sidebar_stack.currentIndex() == 1:
                self._right_stack.setCurrentIndex(2)
        else:
            self._arch_detail.set_arch(None)
            if self._sidebar_stack.currentIndex() == 1:
                self._right_stack.setCurrentIndex(0)
        self.statusBar().showMessage("Architecture updated" if arch else "Architecture cleared")

    # ===== General UI updates =====
    def _on_setting_changed(self):
        # This fires on every single setting edit (settings_changed signal),
        # so it must stay cheap. It used to call _populate_settings(), which
        # clears and rebuilds the entire ~130-item sidebar list (with fresh
        # QListWidgetItem/QFont objects) on every keystroke-adjacent click.
        # refresh_colors() just re-colors the existing items in place.
        count = len(self.settings_manager.get_settings_list())
        arch = self.settings_manager.get_arch()
        arch_str = f" [Arch: {arch.code}]" if arch else ""
        env_count = len(self._env_widget.get_env_dict()) if hasattr(self, '_env_widget') else 0
        env_str = f" · {env_count} env var{'s' if env_count != 1 else ''}" if env_count else ""
        self.statusBar().showMessage(f"{count} setting{'s' if count != 1 else ''} configured{arch_str}{env_str}")
        self._settings_list.refresh_colors(self.settings_manager.get_settings_list())
        custom_now = frozenset(k for k in self.settings_manager.get_settings_list() if drs_lookup(k) is None)
        if custom_now != self._shown_custom_drs:
            self._populate_settings(self._search.text() if self._sidebar_stack.currentIndex() == self.TAB_DRS
                                    else "", quiet=True)
        elif self._settings_list.only_set and \
                frozenset(self.settings_manager.get_settings_list()) != self._seen_set_ids:
            self._populate_settings(self._search.text() if self._sidebar_stack.currentIndex() == self.TAB_DRS else "",
                                    quiet=True)
        self._refresh_badges()

    def _create_support_bundle(self):
        """'Sorun bildir': one anonymised zip with system info, DRSTool's debug log and the saved profiles."""
        default = Path.home() / f"drstool-report-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "Create support bundle", str(default), "Zip archive (*.zip)")
        if not path:
            return
        try:
            out = support_bundle.build(Path(path), self.settings_manager.get_profiles(),
                                       support_bundle.system_lines(SYSHUB.info))
        except Exception as ex:
            toast(f"Support bundle failed: {ex}", "error")
            return
        toast(f"Support bundle saved: {out}", "ok", 6000)

    def _toggle_logging(self):
        """One click: off -> ON (verbose); on or custom -> OFF."""
        self._env_widget.set_logging(self._env_widget.logging_state() == "off")
        # Re-open the visible env var so its editor shows the new value.
        name = getattr(self._env_editor, "_current_name", "")
        ev = env_def(name)
        if ev:
            self._env_editor.set_var(ev)

    def _update_log_btn(self):
        st = self._env_widget.logging_state()
        txt, col = {"on": ("\u25cf Log on", T['accent']),
                    "off": ("\u25cb Log off", T['muted']),
                    "mixed": ("\u25d0 Log custom", T['warn'])}[st]
        self._log_btn.setText(txt)
        self._log_btn.setToolTip(
            "One-click logging control for all env vars.\n"
            "Off  -> every log level = none/off\n"
            "On   -> verbose: every log level at debug/warn (not trace),\n"
            "        WINEDEBUG = Proton log channels + warn+all\n"
            "Click: Off -> On, On/custom -> Off.")
        self._log_btn.setStyleSheet(
            pill_ss(col))

    def _on_env_changed(self):
        self._update_log_btn()
        env_count = len(self._env_widget.get_env_dict())
        arch = self.settings_manager.get_arch()
        arch_str = f" [Arch: {arch.code}]" if arch else ""
        drs_count = len(self.settings_manager.get_settings_list())
        self.statusBar().showMessage(
            f"{drs_count} DRS setting{'s' if drs_count != 1 else ''} · "
            f"{env_count} env var{'s' if env_count != 1 else ''} set{arch_str}"
        )
        # Keep output bar Copy All up to date
        self._output_bar._update()
        self._refresh_badges()
        keys_now = frozenset(self._env_widget.get_env_dict())
        custom_changed = ({k for k in keys_now if k not in _ENV_BY_NAME}
                          != {k for k in self._seen_env_keys if k not in _ENV_BY_NAME})
        if (self._env_widget.only_set or custom_changed) and keys_now != self._seen_env_keys:
            self._env_widget.populate(self._search.text() if self._sidebar_stack.currentIndex() == self.TAB_ENV else "")
        self._seen_env_keys = frozenset(self._env_widget.get_env_dict())

    def _on_profile_loaded(self, name):
        self._pending_label = f"Load profile “{name}”"
        if self.settings_manager.load_warnings:
            self.show_toast(f"Profile “{name}”: " + "; ".join(self.settings_manager.load_warnings), "warn")
        # 1. Reset and reload env vars from profile
        saved_env = self.settings_manager.get_loaded_env_vars()
        self._env_widget.load_values(saved_env)
        # 1b. Restore Gamescope CLI-flag builder state from profile
        self._gamescope.load_flag_values(
            self.settings_manager.get_loaded_gamescope_enabled(),
            self.settings_manager.get_loaded_gamescope_flags(),
        )
        # 2. Drop any pending edit in the env editor (stale data). No hide()
        #    here: QStackedWidget.setCurrentIndex() shows the page again
        #    anyway, so the call was a no-op that only obscured the logic.
        self._env_editor.discard_pending_edit()
        # 3. Clear per-tab right-panel memory so tabs start fresh
        self._tab_right_memory = {}
        # 4. Repopulate lists (the search box only filters the DRS list while
        #    the DRS tab is showing — an env filter must not hide DRS rows)
        self._populate_settings(self._search.text()
                                if self._sidebar_stack.currentIndex() == self.TAB_DRS else "")
        self._populate_arch_list()
        # 5. Set right panel appropriately for active tab
        active_tab = self._sidebar_stack.currentIndex()
        if active_tab == 1:
            arch = self.settings_manager.get_arch()
            if arch:
                self._arch_detail.set_arch(arch)
                self._right_stack.setCurrentIndex(2)
            else:
                self._right_stack.setCurrentIndex(0)
        elif active_tab == self.TAB_PROFILES:
            self._right_stack.setCurrentIndex(self.PAGE_LUTRIS)
        elif active_tab == self.TAB_TOOLS:
            self._right_stack.setCurrentIndex(self.PAGE_TOOLS)
        elif active_tab == self.TAB_GAMES:
            self._right_stack.setCurrentIndex(self.PAGE_GAMES if self._game_card.game else self.PAGE_EMPTY)
        else:
            self._right_stack.setCurrentIndex(self.PAGE_EMPTY)
        self._env_widget.set_gamescope_enabled(self._gamescope.is_enabled())
        self._refresh_badges()
        # Loading replays settings/env/gamescope changes; none of that is an
        # unsaved edit.
        self._set_dirty(False)
        self.statusBar().showMessage(f"Loaded profile: {name}")

    def _update_window_title(self, *_):
        name = self.settings_manager.get_current_profile()
        if not name:
            self._dirty = False
            self.setWindowTitle(APP_TITLE)
            self._profile_chip.setText("\u25cb  No profile")
            self._profile_chip.setToolTip("Save a profile in the Profiles tab (Ctrl+5)")
            self._style_chip(T["muted"], False)
            return
        mark = " •" if self._dirty else ""
        self.setWindowTitle(f"{APP_TITLE} — {name}{mark}")
        if self._dirty:
            self._profile_chip.setText(self._chip_text("\u25cf", name, "  \u2022 unsaved"))
            self._profile_chip.setToolTip("Click to review what changed since the last save")
            self._style_chip(T["warn"], True)
        else:
            self._profile_chip.setText(self._chip_text("\u25c9", name))
            self._profile_chip.setToolTip("Click to compare the editor with the saved profile")
            self._style_chip(T["text2"], True)

    def _on_profile_save_error(self, message: str):
        # Profile persistence failures are silent data-loss risks (the user
        # thinks they clicked "Save" and it worked) — a status bar blip
        # isn't enough visibility for that, so use a modal warning.
        QMessageBox.warning(self, "Profile Save Failed", message)
