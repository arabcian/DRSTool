"""Lutris game-config sync (reads and writes a game's YAML)."""

import logging
import glob
import os
import shlex
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from drstool_core.models import GamescopeFlagDef

from .base import _HAVE_YAML, _PENDING_BAKS, _YAML_LOADER, yaml
from .catalog_data import ALL_ENV_VARS, GAMESCOPE_FLAGS
from .flm import FLM_LEGACY_ENV_KEYS
from .gamescope import GamescopeFlagsWidget
from .launch_import import _parse_drs_string, ParsedLaunch
from .lgtune import lgtune_cmd
from .lookup import arch_by_code
from .settings_manager import SettingsManager
from .toasts import offer_undo

_log = logging.getLogger(__name__)


# ============================================================================
# Lutris game-config sync
# ============================================================================
#
# Lutris stores one YAML file per game under
# ~/.local/share/lutris/games/<slug>-<id>.yml (or under
# $XDG_DATA_HOME/lutris/games/ if XDG_DATA_HOME is set). The env vars we
# care about live at system.env — a flat string->string mapping, exactly
# like the sample alan-wake-2-*.yml. This widget lets the user pick a
# discovered Lutris game and merge the currently-configured env vars
# (DRS settings + DXVK/VKD3D/NV/FLM vars) straight into that file's
# system.env block, instead of retyping them one at a time into Lutris'
# own UI or hand-writing a pre-launch script.
#
# Merge, never blind-overwrite: existing keys in the game's system.env
# that we're not touching are left alone; keys we ARE writing are
# overwritten with the tool's current value; everything else in the
# YAML (game.*, wine.*, system.gamescope, etc.) is round-tripped
# untouched. A timestamped .bak copy of the original file is written
# next to it before every save.
class LutrisGameEntry:
    __slots__ = ("path", "slug", "game_name", "runner")

    def __init__(self, path: Path, slug: str, game_name: str, runner: str):
        self.path = path
        self.slug = slug
        self.game_name = game_name
        self.runner = runner


class LutrisSyncWidget(QWidget):
    """Discovers Lutris per-game YAML configs and writes whatever DRSTool
    currently has configured into the game's YAML.

    Gamescope settings come from two different places and are merged here:

      From the Gamescope CLI-flag builder (DXVK/VKD3D/NV/FLM/Gamescope tab ->
      "Gamescope Launch Flags" item):
        Master "Enable Gamescope" toggle -> system.gamescope: true
          (Lutris's own native "launch this game through gamescope" switch —
          Lutris builds the actual gamescope invocation itself; we never
          write a raw "gamescope ..." command string into the YAML)
        --output-width + --output-height     -> gamescope_output_res        "WxH"
        --nested-width + --nested-height     -> gamescope_game_res          "WxH"
        --framerate-limit                    -> gamescope_fps_limiter
        --sharpness                          -> gamescope_fsr_sharpness
        --hdr-enabled                        -> gamescope_hdr              (bool)
        --force-grab-cursor                  -> gamescope_force_grab_cursor (bool)
      (the per-flag mappings above are only written while the master toggle
      is enabled — get_flag_values() returns {} otherwise; this key set
      matches Cihan's confirmed working Lutris YAML layout — no
      gamescope_hdr_enabled / gamescope_grab / gamescope_vrr_enabled /
      gamescope_upscale_mode / gamescope_fps_limiter_no_focus keys exist)

      Every other gamescope flag that's currently set (--filter, --scaler,
      --adaptive-sync, --rt, --nested-unfocused-refresh, ...) has no native
      Lutris key, so it's packed into a single raw CLI-flag string and
      written to gamescope_flags, e.g.:
        gamescope_flags: --scaler integer --filter nearest --adaptive-sync --rt

      From the Env Vars tab (Gamescope category — genuine env vars only:
      STEAM_GAMESCOPE_*, ENABLE_GAMESCOPE_WSI, STEAM_MULTIPLE_XWAYLANDS, ...)
      goes straight into system.env, same as every other env var category.

      Lutris Game Tune (optional, own group below): when enabled, writes
      prelaunch_command / postexit_command / prefix_command pointing at
      lutris-game-tune-wrapper PRE / POST / RUN <nice>, straight into
      system.* — no extra confirmation beyond the normal "Write to
      Lutris config" dialog.
    """

    # ── Gamescope CLI flag → Lutris system.* key mappings ─────────────────────
    # Single flag → single Lutris key (string value, passed through as-is)
    _FLAG_TO_LUTRIS: Dict[str, str] = {
        "--framerate-limit":            "gamescope_fps_limiter",
        "--sharpness":                  "gamescope_fsr_sharpness",
    }
    # Single flag → single Lutris key (bare toggle -> Python bool)
    _FLAG_TO_LUTRIS_BOOL: Dict[str, str] = {
        "--hdr-enabled":       "gamescope_hdr",
        "--force-grab-cursor": "gamescope_force_grab_cursor",
    }
    # Two flags (W, H) → one Lutris key as "WxH" string
    _FLAG_PAIR_TO_LUTRIS: Dict[str, tuple] = {
        "gamescope_output_res": ("--output-width",  "--output-height"),
        "gamescope_game_res":   ("--nested-width",   "--nested-height"),
    }
    # lutris-game-tune-wrapper install path (kept in sync with LGTUNE_WRAPPER)
    # After a successful write: {"kind": "lutris", "key": <yml stem>, "path", "title"}
    applied = Signal(dict)

    _LGTUNE_WRAPPER_PATH = "/usr/local/bin/lutris-game-tune-wrapper"
    # system.* keys wired to the lutris-game-tune checkbox
    _LGTUNE_KEYS = ("prelaunch_command", "postexit_command", "prefix_command")

    @classmethod
    def _managed_system_keys(cls) -> set:
        """Every system.* key DRSTool considers itself the owner of.

        Sync used to be purely ADDITIVE: a key was written when its control was
        set and simply omitted when it wasn't, and _merge_yaml only ever did
        `data["system"][k] = v`. That makes every set -> unset transition
        invisible on disk — the previous value survives and the YAML silently
        disagrees with the GUI. Declaring ownership up front lets the merge
        delete what the GUI no longer asks for, so a written file always
        matches what is on screen.
        """
        keys = {"gamescope", "gamescope_flags"}
        keys |= set(cls._FLAG_TO_LUTRIS.values())
        keys |= set(cls._FLAG_TO_LUTRIS_BOOL.values())
        keys |= set(cls._FLAG_PAIR_TO_LUTRIS.keys())
        keys |= set(cls._LGTUNE_KEYS)
        return keys

    # Env vars are derived from the DRS grid + arch selector rather than typed
    # into the Env Vars tab, so they never appear in ALL_ENV_VARS.
    _DERIVED_ENV_KEYS = ("DXVK_NVAPI_DRS_SETTINGS", "DXVK_NVAPI_GPU_ARCH")

    @classmethod
    def _managed_env_keys(cls) -> set:
        """Every system.env variable DRSTool considers itself the owner of.

        Same ownership rule as _managed_system_keys, applied to the env block:
        without it, dropping a variable in the Env Vars tab left the old value
        sitting in the YAML forever. That is worse here than in system.*
        because the env block is where the bulk of the configuration lives —
        cutting an FLM setup from 28 variables down to a profile plus four
        would otherwise leave 24 stale ones behind, all still read by the
        layer at startup and all invisible in the GUI that supposedly wrote
        the file.

        Ownership is exactly DRSTool's own catalogue. Anything not in it was
        put there by hand, by Lutris, or by another tool, and is left alone.
        """
        # FLM v3.0 removed these knobs (now auto-tuned inside the layer).
        # They were DRSTool's own catalogue entries, so they are still ours
        # to remove: listing them here makes "Apply" clean old Lutris YAMLs
        # instead of reporting them as unrecognised forever.
        return ({v.name for v in ALL_ENV_VARS} | set(cls._DERIVED_ENV_KEYS)
                | set(FLM_LEGACY_ENV_KEYS))

    def __init__(self, settings_manager: SettingsManager, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self._games: List[LutrisGameEntry] = []
        self._current_yaml_text: Optional[str] = None
        self._gamescope_widget: Optional["GamescopeFlagsWidget"] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QLabel("Lutris Game Sync")
        header.setStyleSheet("color:#e8eaf0; font-size:13px; font-weight:700;")
        layout.addWidget(header)

        if not _HAVE_YAML:
            warn = QLabel(
                "PyYAML not found (import yaml failed). Install it "
                "(pip install pyyaml / your distro's python-yaml package) "
                "to enable Lutris sync."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#e84545; font-size:12px;")
            layout.addWidget(warn)
            layout.addStretch()
            return

        desc = QLabel(
            "Scans your Lutris games folder and writes whatever is currently "
            "configured in DRSTool directly into the game's YAML.  Gamescope env "
            "vars are automatically routed: those with a Lutris native key "
            "(output res, game res, fps limit, upscale filter, HDR, VRR, …) go "
            "into system.* — the rest go into system.env as plain env vars."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#a8adb8; font-size:11px;")
        layout.addWidget(desc)

        # ── Games folder + rescan ─────────────────────────────────────────────
        scan_row = QHBoxLayout()
        scan_row.setSpacing(4)
        self._path_label = QLineEdit()
        self._path_label.setReadOnly(True)
        self._path_label.setStyleSheet("""
            QLineEdit {
                background: #12151b; border: 1px solid #1d222c;
                color: #a8adb8; font-size: 11px; font-family: monospace;
                padding: 3px 8px; border-radius: 3px; min-height: 26px;
            }
        """)
        scan_row.addWidget(self._path_label, stretch=1)
        _btn_ss = """
            QPushButton {
                border: 1px solid #1d222c; border-radius: 3px; padding: 3px 8px;
                background: #12151b; color: #c8cdd8; font-size: 11px; min-height: 26px;
            }
            QPushButton:hover { border-color: #4a7300; }
        """
        browse_btn = QPushButton("...")
        browse_btn.setFixedWidth(28)
        browse_btn.clicked.connect(self._browse_games_dir)
        browse_btn.setStyleSheet(_btn_ss)
        rescan_btn = QPushButton("Rescan")
        rescan_btn.clicked.connect(self._scan_games)
        rescan_btn.setStyleSheet(_btn_ss)
        scan_row.addWidget(browse_btn)
        scan_row.addWidget(rescan_btn)
        layout.addLayout(scan_row)

        self._games_dir: Path = self._default_games_dir()
        self._path_label.setText(str(self._games_dir))

        # ── Game picker ───────────────────────────────────────────────────────
        self._game_combo = QComboBox()
        self._game_combo.setStyleSheet("""
            QComboBox {
                background: #12151b; border: 1px solid #1d222c; color: #e8eaf0;
                font-size: 12px; padding: 3px 8px; border-radius: 3px; min-height: 28px;
            }
            QComboBox:hover { border-color: #4a7300; }
        """)
        self._game_combo.currentIndexChanged.connect(self._on_game_selected)
        layout.addWidget(self._game_combo)

        # ── Lutris Game Tune (PRE/POST/RUN wrapper commands) ──────────────────
        # Everything DRSTool currently has configured (DRS settings + all
        # env vars from whichever tabs/profile are active) is always synced —
        # there's no opt-out here anymore. This box only holds the handful of
        # settings that aren't already tracked elsewhere in the GUI.
        _grp_ss = """
            QGroupBox {
                color: #a8adb8; font-size: 11px; font-weight: 700;
                border: 1px solid #1d222c; border-radius: 3px;
                margin-top: 8px; padding-top: 14px;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 6px; padding: 0 4px; }
        """
        # Explicit indicator styling — the default Qt checkbox indicator is
        # nearly invisible against this dark theme, so give it a real
        # border + a bright checked state.
        _chk_ss = """
            QCheckBox { color:#c8cdd8; font-size:13px; }
            QCheckBox::indicator {
                width:15px; height:15px; border:1px solid #5a6478;
                border-radius:3px; background:#181c24;
            }
            QCheckBox::indicator:hover { border-color:#76b900; }
            QCheckBox::indicator:checked { background:#76b900; border:1px solid #76b900; }
        """
        lgtune_group = QGroupBox("Lutris Game Tune")
        lgtune_group.setStyleSheet(_grp_ss)
        lgtune_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lgtune_layout = QVBoxLayout(lgtune_group)
        lgtune_layout.setSpacing(10)

        self._chk_lgtune = QCheckBox(
            "Wire lutris-game-tune (prelaunch_command / postexit_command / "
            "prefix_command)"
        )
        self._chk_lgtune.setStyleSheet(_chk_ss)
        lgtune_layout.addWidget(self._chk_lgtune)

        nice_row = QHBoxLayout()
        nice_row.setSpacing(6)
        nice_lbl = QLabel("RUN nice value:")
        nice_lbl.setStyleSheet("color:#c8cdd8; font-size:13px;")
        self._lgtune_nice = QSpinBox()
        self._lgtune_nice.setRange(-20, 19)
        self._lgtune_nice.setValue(-10)
        self._lgtune_nice.setStyleSheet("""
            QSpinBox {
                background: #12151b; border: 1px solid #1d222c; color: #e8eaf0;
                font-size: 13px; font-family: monospace; padding: 2px 4px;
                border-radius: 3px; min-height: 24px;
            }
            QSpinBox:hover { border-color: #4a7300; }
        """)
        nice_row.addWidget(nice_lbl)
        nice_row.addWidget(self._lgtune_nice)
        nice_row.addStretch()
        lgtune_layout.addLayout(nice_row)

        lgtune_note = QLabel(
            f"When enabled, writes:\n"
            f"  prelaunch_command: {lgtune_cmd('PRE', self._LGTUNE_WRAPPER_PATH)}\n"
            f"  postexit_command:  {lgtune_cmd('POST', self._LGTUNE_WRAPPER_PATH)}\n"
            f"  prefix_command:    {self._LGTUNE_WRAPPER_PATH} RUN <nice>\n"
            "directly into system.* — no separate prompt, same "
            "'Write to Lutris config' confirmation as everything else."
        )
        lgtune_note.setWordWrap(True)
        lgtune_note.setStyleSheet(
            "color:#7b8496; font-size:13px; font-family:monospace;"
        )
        lgtune_layout.addWidget(lgtune_note)

        layout.addWidget(lgtune_group)

        # ── Preview ───────────────────────────────────────────────────────────
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setStyleSheet("""
            QPlainTextEdit {
                background: #0c0e12; border: 1px solid #1d222c;
                color: #9fd63a; font-family: monospace; font-size: 11px; border-radius: 3px;
            }
        """)
        self._preview.setPlaceholderText("Select a game to preview the resulting YAML changes...")
        layout.addWidget(self._preview, stretch=1)

        # ── Actions ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._apply_btn = QPushButton("Write to Lutris config")
        self._apply_btn.clicked.connect(self._apply_to_game)
        self._apply_btn.setStyleSheet("""
            QPushButton {
                background: #76b900; color: #000; border: 1px solid #76b900;
                border-radius: 3px; padding: 4px 12px; font-size: 12px;
                font-weight: 700; min-height: 28px;
            }
            QPushButton:hover { background: #8fd400; }
            QPushButton:disabled { background: #2a2f38; color: #7b8496; border-color: #1d222c; }
        """)
        btn_row.addWidget(self._apply_btn)

        # Read-back. Selecting a game used to load its YAML for the PREVIEW
        # only — nothing was ever parsed back into the controls, so DRSTool
        # had no idea what the file already contained and every write was a
        # blind overlay of the current GUI state onto whatever was there.
        self._import_btn = QPushButton("Load from selected game")
        self._import_btn.setToolTip(
            "Parse the selected game's YAML and populate DRSTool from it: "
            "gamescope flags, env vars and lutris-game-tune wiring.\n"
            "Replaces the current GUI state — save a profile first if you "
            "want to keep it."
        )
        self._import_btn.clicked.connect(self._import_from_yaml)
        self._import_btn.setStyleSheet("""
            QPushButton {
                background: #181c24; color: #dfe3ea; border: 1px solid #2a313d;
                border-radius: 3px; padding: 4px 12px; font-size: 12px;
                font-weight: 600; min-height: 28px;
            }
            QPushButton:hover { background: #232a35; border-color: #4a7300; }
            QPushButton:disabled { background: #14181f; color: #6a7080; }
        """)
        btn_row.addWidget(self._import_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color:#a8adb8; font-size:11px;")
        layout.addWidget(self._status_label)

        for w in (self._chk_lgtune,):
            w.stateChanged.connect(self._update_preview)
        self._lgtune_nice.valueChanged.connect(self._update_preview)
        self.settings_manager.settings_changed.connect(self._update_preview)
        self.settings_manager.arch_changed.connect(self._update_preview)

        self._scan_games()

    # ── Discovery ─────────────────────────────────────────────────────────────

    @staticmethod
    def _default_games_dir() -> Path:
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        return data_home / "lutris" / "games"

    def _browse_games_dir(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Lutris games folder", str(self._games_dir)
        )
        if chosen:
            self._games_dir = Path(chosen)
            self._path_label.setText(str(self._games_dir))
            self._scan_games()

    def _scan_games(self):
        self._games = []
        self._game_combo.blockSignals(True)
        self._game_combo.clear()

        if not self._games_dir.exists():
            self._status_label.setText(f"Folder not found: {self._games_dir}")
            self._game_combo.blockSignals(False)
            self._apply_btn.setEnabled(False)
            return

        found = sorted(self._games_dir.glob("*.yml")) + sorted(self._games_dir.glob("*.yaml"))
        for path in found:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.load(f, Loader=_YAML_LOADER) or {}
            except Exception:
                _log.debug("ignored error", exc_info=True)
                continue
            if not isinstance(data, dict):
                continue            # a list/scalar YAML used to crash the whole scan
            game_section = data.get("game")
            if not isinstance(game_section, dict):
                game_section = {}
            exe = str(game_section.get("exe") or "")
            slug = path.stem
            display_name = Path(exe).stem if exe else slug
            runner = data.get("system", {}).get("runner", "") \
                if isinstance(data.get("system"), dict) else ""
            self._games.append(LutrisGameEntry(path, slug, display_name, runner))
            self._game_combo.addItem(f"{display_name}  ({slug})")

        self._game_combo.blockSignals(False)
        if self._games:
            self._status_label.setText(
                f"Found {len(self._games)} game config(s) in {self._games_dir}"
            )
            self._game_combo.setCurrentIndex(0)
            self._on_game_selected(0)
        else:
            self._status_label.setText(f"No .yml files found in {self._games_dir}")
            self._apply_btn.setEnabled(False)
            self._preview.clear()

    # ── Selection / preview ───────────────────────────────────────────────────

    def _on_game_selected(self, index: int):
        self._current_yaml_text = None
        if 0 <= index < len(self._games):
            entry = self._games[index]
            try:
                with open(entry.path, "r", encoding="utf-8") as f:
                    self._current_yaml_text = f.read()
            except Exception as e:
                self._status_label.setText(f"Could not read {entry.path}: {e}")
            w = self.window()
            if hasattr(w, "_ext_track_lutris"):
                w._ext_track_lutris(entry.path)
        self._update_preview()

    def refresh_yaml_from_disk(self):
        """Re-read the selected game's YAML (after an outside edit) and redraw the preview."""
        index = self._game_combo.currentIndex()
        if 0 <= index < len(self._games):
            try:
                self._current_yaml_text = self._games[index].path.read_text(encoding="utf-8")
            except Exception as e:
                self._status_label.setText(f"Could not read {self._games[index].path}: {e}")
                return
            self._update_preview()

    def _collect_all_env(self) -> Dict[str, str]:
        """All env vars currently set in DRSTool (DRS settings + every env
        var from whichever tabs/profile are active) — always synced, no
        opt-out checkboxes."""
        result: Dict[str, str] = {}
        arch = self.settings_manager.get_arch()
        if arch:
            result["DXVK_NVAPI_GPU_ARCH"] = arch.code
        settings_str = self.settings_manager.get_settings_string()
        if settings_str:
            result["DXVK_NVAPI_DRS_SETTINGS"] = settings_str
        win = self.window()
        if win and hasattr(win, "_env_widget"):
            result.update(win._env_widget.get_env_dict())
        return result

    def set_gamescope_widget(self, widget: "GamescopeFlagsWidget"):
        """Wire up the CLI-flag builder (Extra Tools -> Gamescope) so its
        settings can be folded into Lutris system.* keys alongside env vars."""
        self._gamescope_widget = widget

    # ── Read-back ──────────────────────────────────────────────────

    @staticmethod
    def _flag_lookup() -> Dict[str, "GamescopeFlagDef"]:
        """Long flag AND short flag -> definition, for parsing a raw CLI string."""
        table: Dict[str, GamescopeFlagDef] = {}
        for gf in GAMESCOPE_FLAGS:
            table[gf.flag] = gf
            if gf.short:
                table[gf.short] = gf
        return table

    @classmethod
    def _parse_gamescope_flags_string(cls, raw: str) -> Dict[str, str]:
        """Turn a gamescope_flags CLI string back into {long_flag: value}.

        Unknown tokens are skipped rather than guessed at: writing them back in
        a mangled form would be worse than dropping them, and the preview shows
        exactly what is about to be written either way.
        """
        table = cls._flag_lookup()
        out: Dict[str, str] = {}
        try:
            tokens = shlex.split(raw or "")
        except ValueError:
            return out
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            gf = table.get(tok)
            if gf is None:
                i += 1
                continue
            if gf.kind == "toggle":
                out[gf.flag] = ""
                i += 1
            else:
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                    out[gf.flag] = tokens[i + 1]
                    i += 2
                else:
                    i += 1
        return out

    def _unrecognised_env_vars(self) -> set:
        """Vars in the selected file that carry one of our prefixes but are not
        in ALL_ENV_VARS — reported, never removed."""
        if self._current_yaml_text is None:
            return set()
        try:
            data = yaml.load(self._current_yaml_text, Loader=_YAML_LOADER) or {}
        except Exception:
            _log.debug("ignored error", exc_info=True)
            return set()
        sysdict = data.get("system") or {}
        env_sec = (sysdict.get("env") or {}) if isinstance(sysdict, dict) else {}
        if not isinstance(env_sec, dict):
            return set()
        prefixes = ("FLM_", "DXVK_", "VKD3D_", "PROTON_", "NVPRESENT_")
        known = self._managed_env_keys()
        return {str(k) for k in env_sec
                if str(k).startswith(prefixes) and str(k) not in known}

    def _import_from_yaml(self):
        """Populate DRSTool's controls from the selected game's YAML.

        Counterpart to _apply_to_game. Without it the two directions were
        asymmetric: DRSTool could write into a file but never learn what was
        already in it, which is what made stale keys invisible in the first
        place.
        """
        if self._current_yaml_text is None:
            self._status_label.setText("Select a game first.")
            return
        try:
            data = yaml.load(self._current_yaml_text, Loader=_YAML_LOADER) or {}
        except Exception as e:
            QMessageBox.critical(self, "Parse failed", f"Could not parse YAML: {e}")
            return
        sysdict = data.get("system") or {}
        if not isinstance(sysdict, dict):
            sysdict = {}

        reply = QMessageBox.question(
            self, "Load from Lutris config",
            "This replaces the gamescope flags, env vars and lutris-game-tune "
            "settings currently in DRSTool with whatever is in this game's YAML.\n\n"
            "Save a profile first if you want to keep the current state. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        notes: List[str] = []

        # ── Gamescope: native system.* keys + the raw catch-all string ─────────
        if self._gamescope_widget is not None:
            flags: Dict[str, str] = {}
            enabled = bool(sysdict.get("gamescope", False))

            for flag, key in self._FLAG_TO_LUTRIS.items():
                val = sysdict.get(key)
                if val not in (None, ""):
                    flags[flag] = str(val)

            for flag, key in self._FLAG_TO_LUTRIS_BOOL.items():
                if sysdict.get(key):
                    flags[flag] = ""

            for key, (flag_w, flag_h) in self._FLAG_PAIR_TO_LUTRIS.items():
                raw = str(sysdict.get(key, "") or "").strip().lower()
                if not raw:
                    continue
                parts = raw.split("x")
                if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
                    flags[flag_w] = parts[0].strip()
                    flags[flag_h] = parts[1].strip()
                else:
                    notes.append(f"{key}={raw!r} is not a WxH pair — skipped")

            raw_flags = str(sysdict.get("gamescope_flags", "") or "")
            if raw_flags:
                parsed = self._parse_gamescope_flags_string(raw_flags)
                flags.update(parsed)
                known = set(parsed)
                try:
                    tok_flags = {t for t in shlex.split(raw_flags) if t.startswith("-")}
                except ValueError:
                    tok_flags = set()
                table = self._flag_lookup()
                unknown = {t for t in tok_flags
                           if t not in table or table[t].flag not in known}
                if unknown:
                    notes.append("gamescope_flags: unrecognised token(s) dropped: "
                                 + " ".join(sorted(unknown)))

            # prefix_command round-trip: a MangoHud prefix written by a
            # previous sync (alone, or trailing the lutris-game-tune wrapper)
            # comes back as the pseudo-flag.
            if GamescopeFlagsWidget.MANGOHUD_PREFIX in str(
                    sysdict.get("prefix_command", "") or ""):
                flags[GamescopeFlagsWidget.MANGOHUD_KEY] = \
                    GamescopeFlagsWidget.MANGOHUD_PREFIX

            self._gamescope_widget.load_flag_values(enabled, flags)

        # ── Env vars + DRS settings + GPU arch ─────────────────────────
        env_sec = sysdict.get("env") or {}
        if not isinstance(env_sec, dict):
            env_sec = {}
        # DXVK_NVAPI_DRS_SETTINGS / DXVK_NVAPI_GPU_ARCH are DERIVED from the
        # DRS settings grid + arch selector, so they must not land in the env
        # widget (a second, conflicting source of truth). They go to the
        # SettingsManager instead, mirroring what _apply_to_game wrote.
        derived = {"DXVK_NVAPI_DRS_SETTINGS", "DXVK_NVAPI_GPU_ARCH"}
        win = self.window()
        if win is not None and hasattr(win, "_env_widget"):
            vals = {str(k): str(v) for k, v in env_sec.items() if str(k) not in derived}
            win._env_widget.load_values(vals)

        parsed = ParsedLaunch()
        drs_raw = env_sec.get("DXVK_NVAPI_DRS_SETTINGS")
        if drs_raw not in (None, ""):
            _parse_drs_string(str(drs_raw), parsed)
        arch_code: Optional[str] = None
        arch_raw = env_sec.get("DXVK_NVAPI_GPU_ARCH")
        if arch_raw not in (None, ""):
            a = arch_by_code(str(arch_raw).strip().upper())
            if a:
                arch_code = a.code
            else:
                notes.append(f"DXVK_NVAPI_GPU_ARCH={arch_raw!r} is not a known arch — skipped")
        notes.extend(text for sev, text in parsed.notes if sev == "warn")
        self.settings_manager.replace_state(parsed.drs, arch_code)

        # ── lutris-game-tune ───────────────────────────────────────
        prefix_cmd = str(sysdict.get("prefix_command", "") or "")
        lgtune_on = self._LGTUNE_WRAPPER_PATH in prefix_cmd
        self._chk_lgtune.setChecked(lgtune_on)
        if lgtune_on:
            parts = prefix_cmd.split()
            if "RUN" in parts:
                idx = parts.index("RUN")
                if idx + 1 < len(parts):
                    try:
                        self._lgtune_nice.setValue(int(parts[idx + 1]))
                    except ValueError:
                        notes.append(f"prefix_command: could not read the nice value "
                                     f"from {prefix_cmd!r}")

        self._update_preview()
        msg = "Loaded from YAML."
        if notes:
            msg += "  " + "  |  ".join(notes)
        self._status_label.setText(msg)

    def _split_gamescope(self, env: Dict[str, str]):
        """Split settings into (system_keys, remaining_env).

        system_keys: {lutris_key: value} — goes into system.*
        remaining_env: {var: value}       — goes into system.env

        `env` here is genuine env vars only (Gamescope's real STEAM_GAMESCOPE_*
        / ENABLE_GAMESCOPE_WSI vars included) — they all pass straight through
        to system.env. Gamescope's CLI flags (resolution, HDR, VRR, ...) come
        from self._gamescope_widget separately and are the only things mapped
        into system.* here.
        """
        system_keys: Dict[str, object] = {}
        remaining: Dict[str, str] = dict(env)
        warnings: List[str] = []

        flags: Dict[str, str] = {}
        gamescope_on = False
        if self._gamescope_widget is not None:
            gamescope_on = self._gamescope_widget.is_enabled()
            flags = self._gamescope_widget.get_flag_values()

        # Master toggle -> Lutris's own native "run this game through
        # gamescope" boolean. Lutris builds the actual gamescope invocation
        # itself from this + the gamescope_* keys below; we never write a
        # raw "gamescope ..." command string into the YAML.
        # Written EITHER WAY. Emitting the key only when the toggle is on made
        # turning gamescope OFF a no-op: the YAML kept its old `gamescope: true`
        # and Lutris went on launching the game through gamescope while the GUI
        # showed it disabled.
        system_keys["gamescope"] = bool(gamescope_on)

        # Flags consumed by an explicit native-key mapping below — anything
        # left over falls through to the gamescope_flags catch-all string.
        consumed_flags = set(self._FLAG_TO_LUTRIS) | set(self._FLAG_TO_LUTRIS_BOOL)
        for flag_w, flag_h in self._FLAG_PAIR_TO_LUTRIS.values():
            consumed_flags.add(flag_w)
            consumed_flags.add(flag_h)

        # Single → single string mappings
        for flag, lutris_key in self._FLAG_TO_LUTRIS.items():
            if flag in flags and flags[flag]:
                system_keys[lutris_key] = str(flags[flag])

        # Single → single bool mappings. Absent = explicit False, not "skip":
        # a skipped key left the previous `gamescope_hdr: true` in place, so
        # unchecking the box in the GUI changed nothing on disk.
        for flag, lutris_key in self._FLAG_TO_LUTRIS_BOOL.items():
            if gamescope_on:
                system_keys[lutris_key] = flag in flags

        # Pair → "WxH" string mappings.
        # A Lutris resolution key is a single "WxH" string, so it needs BOTH
        # halves. The old code silently skipped the key whenever either half
        # was missing or unparseable — which is why filling in only the width
        # (or clearing only the height) appeared to "not take": the stale
        # WxH already in the YAML simply survived untouched. Now a partial
        # pair is surfaced as a warning, and the key is still emitted as a
        # removal so the file never disagrees with the GUI.
        for lutris_key, (flag_w, flag_h) in self._FLAG_PAIR_TO_LUTRIS.items():
            w_raw = str(flags.get(flag_w, "")).strip()
            h_raw = str(flags.get(flag_h, "")).strip()
            if not gamescope_on:
                continue
            if not w_raw and not h_raw:
                continue                      # deliberately unset → managed removal
            try:
                w_int, h_int = int(w_raw), int(h_raw)
            except (ValueError, TypeError):
                warnings.append(
                    f"{lutris_key}: needs both {flag_w} and {flag_h} "
                    f"(got {flag_w}={w_raw or '<empty>'}, {flag_h}={h_raw or '<empty>'}) "
                    f"— key will be removed instead of written"
                )
                continue
            if w_int > 0 and h_int > 0:
                system_keys[lutris_key] = f"{w_int}x{h_int}"
            else:
                warnings.append(
                    f"{lutris_key}: {flag_w}/{flag_h} must both be > 0 "
                    f"(got {w_int}x{h_int}) — key will be removed instead of written"
                )

        # Everything else currently set (--filter, --scaler, --adaptive-sync,
        # --rt, --nested-unfocused-refresh, ...) has no native Lutris key —
        # pack it into one raw CLI-flag string, e.g.:
        #   gamescope_flags: --scaler integer --filter nearest --adaptive-sync --rt
        if gamescope_on:
            leftover_parts: List[str] = []
            for gf in GAMESCOPE_FLAGS:
                flag = gf.flag
                if flag in consumed_flags or flag not in flags:
                    continue
                val = flags[flag]
                leftover_parts.append(flag if val == "" else f"{flag} {shlex.quote(val)}")
            if leftover_parts:
                system_keys["gamescope_flags"] = " ".join(leftover_parts)

        # MangoHud. Lutris builds the gamescope invocation itself, so there is
        # no '--' here to hang the prefix off — its equivalent is
        # system.prefix_command, which Lutris puts in front of the game's
        # execution command (i.e. exactly where `-- mangohud --dlsym` would
        # have landed in a hand-written command line).
        mangohud_on = (self._gamescope_widget is not None
                       and self._gamescope_widget.mangohud_enabled())

        # lutris-game-tune wiring — written straight into system.* like
        # everything else here; no separate confirmation prompt.
        if self._chk_lgtune.isChecked():
            nice = self._lgtune_nice.value()
            system_keys["prelaunch_command"] = lgtune_cmd("PRE", self._LGTUNE_WRAPPER_PATH)
            system_keys["postexit_command"] = lgtune_cmd("POST", self._LGTUNE_WRAPPER_PATH)
            # Both want prefix_command. They compose rather than conflict: the
            # tune wrapper execs whatever follows it, so mangohud goes second
            # and ends up wrapping the game while still running under the
            # wrapper's scheduling settings.
            prefix = f"{self._LGTUNE_WRAPPER_PATH} RUN {nice}"
            if mangohud_on:
                prefix += f" {GamescopeFlagsWidget.MANGOHUD_PREFIX}"
            system_keys["prefix_command"] = prefix
        elif mangohud_on:
            system_keys["prefix_command"] = GamescopeFlagsWidget.MANGOHUD_PREFIX

        return system_keys, remaining, warnings

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        if getattr(self, "_preview_stale", False):
            self._update_preview()

    def _update_preview(self):
        # Connected to every DRS / env / gamescope edit, and each run parses the
        # YAML three times + dumps it. Skip while the tab is hidden; showEvent
        # catches up. Without PyYAML the widget has no preview/buttons at all,
        # and every env/gamescope edit used to raise AttributeError here.
        if not _HAVE_YAML:
            return
        if not self.isVisible():
            self._preview_stale = True
            return
        self._preview_stale = False
        # Read-back only needs a parsed file, not a configured GUI.
        self._import_btn.setEnabled(self._current_yaml_text is not None)
        if not self._games or self._current_yaml_text is None:
            self._preview.clear()
            self._apply_btn.setEnabled(False)
            return

        all_env = self._collect_all_env()
        system_keys, env_vars, warnings = self._split_gamescope(all_env)
        if not system_keys and not env_vars:
            self._preview.setPlainText(
                "(Nothing configured yet — set DRS settings, env vars, "
                "gamescope flags, or lutris-game-tune "
                "in the other tabs/groups first.)"
            )
            self._apply_btn.setEnabled(False)
            return

        try:
            new_text, sys_changed, env_changed, sys_removed, env_removed = self._merge_yaml(
                self._current_yaml_text, system_keys, env_vars
            )
        except Exception as e:
            self._preview.setPlainText(f"(Could not parse this game's YAML: {e})")
            self._apply_btn.setEnabled(False)
            return

        lines = []
        if warnings:
            lines.append("# ⚠ incomplete input — these keys cannot be written:")
            for w in warnings:
                lines.append(f"#   {w}")
            lines.append("")
        if sys_removed:
            lines.append(f"# {len(sys_removed)} stale key(s) REMOVED from system.*:")
            for k in sorted(sys_removed):
                lines.append(f"#   - {k}")
            lines.append("")
        if env_removed:
            lines.append(f"# {len(env_removed)} stale var(s) REMOVED from system.env:")
            for k in sorted(env_removed):
                lines.append(f"#   - {k}")
            lines.append("")
        # Advisory only. A variable that looks like one of ours but isn't in
        # the catalogue is almost always a typo or a flag that was dropped
        # upstream (FLM_VERBOSE, for instance, is read by nothing). It is NOT
        # deleted: DRSTool only removes what it can positively identify as its
        # own, so an unknown name is left for the user to judge.
        unknown = self._unrecognised_env_vars()
        if unknown:
            lines.append(f"# {len(unknown)} unrecognised var(s) left untouched "
                         f"(typo, or dropped upstream?):")
            for k in sorted(unknown):
                lines.append(f"#   ? {k}")
            lines.append("")
        if sys_changed:
            lines.append(f"# {len(sys_changed)} key(s) → system.* (Lutris native):")
            for k in sorted(sys_changed):
                lines.append(f"#   {k}: {system_keys[k]!r}")
        if env_changed:
            if lines:
                lines.append("")
            lines.append(f"# {len(env_changed)} key(s) → system.env:")
            for k in sorted(env_changed):
                lines.append(f"#   {k}={env_vars[k]!r}")
        lines.append("")
        lines.append("─── resulting system section ───")
        try:
            data = yaml.load(new_text, Loader=_YAML_LOADER) or {}
            sys_sec = data.get("system") or {}
            for k in sorted(sys_sec):
                if k == "env":
                    continue
                lines.append(f"  {k}: {sys_sec[k]!r}")
            env_sec = sys_sec.get("env") or {}
            if env_sec:
                lines.append("  env:")
                for k in sorted(env_sec):
                    lines.append(f"    {k}: {env_sec[k]!r}")
        except Exception:
            _log.debug("ignored error", exc_info=True)
            pass
        self._preview.setPlainText("\n".join(lines))
        self._apply_btn.setEnabled(True)

    # ── Merge / write ─────────────────────────────────────────────────────────

    @classmethod
    def _merge_yaml(
        cls,
        original_text: str,
        system_keys: Dict[str, object],
        env_vars: Dict[str, str],
    ):
        """Parse original_text, write system_keys into system.* and env_vars
        into system.env.  Returns
        (new_yaml_text, sys_changed, env_changed, sys_removed, env_removed).
        Comments are not preserved (PyYAML limitation); all other structure is.

        Keys DRSTool owns (see _managed_system_keys) but that are NOT in
        system_keys this round are REMOVED rather than left behind. Without
        that step the merge could only ever add or overwrite, so unchecking a
        box in the GUI had no effect on an existing file.
        """
        data = yaml.load(original_text, Loader=_YAML_LOADER) or {}
        if not isinstance(data, dict):
            raise ValueError("the game YAML's top level is not a mapping")
        if "system" not in data or not isinstance(data.get("system"), dict):
            data["system"] = {}
        sysdict = data["system"]

        # Drop owned keys the GUI is no longer asking for.
        sys_removed = set()
        for k in cls._managed_system_keys() - set(system_keys):
            if k not in sysdict:
                continue
            # lutris-game-tune keys are only ours while they still point at our
            # wrapper — never clobber a hand-written prelaunch_command.
            # prefix_command has a second owner: a MangoHud-only prefix carries
            # no wrapper path, so without this it could never be removed and
            # unticking MangoHud would leave `prefix_command: mangohud --dlsym`
            # behind forever.
            cur_val = str(sysdict.get(k, ""))
            if k in cls._LGTUNE_KEYS and \
               cls._LGTUNE_WRAPPER_PATH not in cur_val and \
               cur_val.strip() != GamescopeFlagsWidget.MANGOHUD_PREFIX:
                continue
            del sysdict[k]
            sys_removed.add(k)

        # Only keys whose value really differs count as "changed"; identical
        # ones are left untouched so the file keeps its own types, and a fully
        # matching file reports "Nothing to write" instead of a phantom N changes.
        sys_changed = set()
        for k, v in system_keys.items():
            if k in sysdict and sysdict[k] == v and type(sysdict[k]) is type(v):
                continue
            sysdict[k] = v
            sys_changed.add(k)

        if "env" not in sysdict or not isinstance(sysdict.get("env"), dict):
            sysdict["env"] = {}
        envdict = sysdict["env"]

        env_removed = set()
        for k in cls._managed_env_keys() - set(env_vars):
            if k in envdict:
                del envdict[k]
                env_removed.add(k)

        env_changed = set()
        for k, v in env_vars.items():
            if k in envdict and str(envdict[k]) == str(v):
                continue
            envdict[k] = str(v)
            env_changed.add(k)

        # Don't leave an empty mapping behind if the block ends up cleared.
        if not envdict:
            del sysdict["env"]

        new_text = yaml.dump(
            data, default_flow_style=False, sort_keys=False, allow_unicode=True,
            width=1_000_000,   # no folding of long gamescope_flags / prefix_command
        )
        return new_text, sys_changed, env_changed, sys_removed, env_removed

    _BACKUP_KEEP = 20

    @classmethod
    def _prune_backups(cls, target: Path):
        """Keep only the newest few timestamped .bak files of this game."""
        try:
            olds = sorted(target.parent.glob(glob.escape(target.name) + ".*.bak"))
            for p in olds[:-cls._BACKUP_KEEP]:
                p.unlink(missing_ok=True)
        except OSError:
            pass

    def _apply_to_game(self, *_, confirm: bool = True) -> bool:
        """True when the file was written or already matched; False on any failure/abort
        (the CLI reports its exit status from this, not from the status label)."""
        index = self._game_combo.currentIndex()
        if not (0 <= index < len(self._games)):
            return False
        entry = self._games[index]
        all_env = self._collect_all_env()
        system_keys, env_vars, warnings = self._split_gamescope(all_env)

        try:
            with open(entry.path, "r", encoding="utf-8") as f:
                original_text = f.read()
            new_text, sys_changed, env_changed, sys_removed, env_removed = self._merge_yaml(
                original_text, system_keys, env_vars
            )
        except Exception as e:
            QMessageBox.critical(self, "Merge failed", f"Could not merge: {e}")
            return False

        # A removal-only round is still a real change. The old guard bailed
        # out whenever nothing was being WRITTEN, so clearing a control in
        # the GUI could never propagate to disk.
        total = (len(sys_changed) + len(env_changed)
                 + len(sys_removed) + len(env_removed))
        if total == 0:
            self._status_label.setText("Nothing to write — file already matches the GUI.")
            self.applied.emit(self._target(entry))
            return True
        sys_line = f"\n  • {len(sys_changed)} key(s) into system.*" if sys_changed else ""
        env_line = f"\n  • {len(env_changed)} var(s) into system.env" if env_changed else ""
        del_line = (f"\n  • {len(sys_removed)} stale key(s) removed from system.*: "
                    f"{', '.join(sorted(sys_removed))}") if sys_removed else ""
        envdel_line = (f"\n  • {len(env_removed)} stale var(s) removed from system.env: "
                       f"{', '.join(sorted(env_removed))}") if env_removed else ""
        warn_line = ("\n\n⚠ " + "\n⚠ ".join(warnings)) if warnings else ""
        reply = QMessageBox.Yes if not confirm else QMessageBox.question(
            self, "Write Lutris config",
            f"Write {total} change(s) into:\n{entry.path}"
            f"{sys_line}{env_line}{del_line}{envdel_line}{warn_line}\n\n"
            "A timestamped backup will be created first.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return False

        # Lutris (or the user) may have touched the file while the dialog was
        # open; merging into stale text would silently overwrite that change.
        try:
            with open(entry.path, "r", encoding="utf-8") as f:
                fresh = f.read()
        except Exception as e:
            QMessageBox.critical(self, "Write failed", f"Could not re-read {entry.path}: {e}")
            return False
        if fresh != original_text:
            self._current_yaml_text = fresh
            self._update_preview()
            QMessageBox.warning(
                self, "File changed on disk",
                f"{entry.path.name} was modified while the confirmation dialog was open.\n"
                "Nothing was written. The preview has been refreshed — review it and apply again.")
            return False

        try:
            # Resolve symlinks first: os.replace() on the link itself would
            # swap the symlink for a regular file.
            target = Path(os.path.realpath(entry.path))
            # Microseconds: two writes in the same second used to reuse one
            # name, so the second copy2 overwrote the backup of the original.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup_path = target.with_name(f"{target.name}.{stamp}.bak")
            shutil.copy2(target, backup_path)
            _PENDING_BAKS.append((target, backup_path, time.monotonic()))
            fd, tmp_name = tempfile.mkstemp(dir=str(target.parent),
                                            prefix=f".{target.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(new_text)
                    f.flush()
                    os.fsync(f.fileno())
                shutil.copymode(target, tmp_name)   # mkstemp is 0600; keep the original mode
                os.replace(tmp_name, target)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            self._prune_backups(target)
        except Exception as e:
            QMessageBox.critical(self, "Write failed", f"Could not write {entry.path}: {e}")
            return False

        self._current_yaml_text = new_text
        w = self.window()
        if hasattr(w, "_ext_mark_lutris"):
            w._ext_mark_lutris()
        self._status_label.setText(
            f"Wrote {total} change(s) to {entry.path.name}  (backup: {backup_path.name})"
        )
        self._update_preview()
        offer_undo(f"Wrote {total} change(s) to {entry.path.name}")
        self.applied.emit(self._target(entry))
        return True

    @staticmethod
    def _target(entry: "LutrisGameEntry") -> dict:
        return {"kind": "lutris", "key": entry.path.stem, "path": str(entry.path), "title": entry.game_name}

    def apply_target(self, target: dict) -> tuple:
        """Select the game YAML a link points to and write without asking. -> (ok, message)"""
        path = Path(target.get("path") or "")
        if not path.is_file():
            return False, f"{path} no longer exists"

        def find():
            return next((i for i, g in enumerate(self._games) if g.path == path), -1)
        i = find()
        if i < 0:                                   # the game lives in another Lutris folder
            self._games_dir = path.parent
            self._path_label.setText(str(self._games_dir))
            self._scan_games()
            i = find()
        if i < 0:
            return False, f"{path.name} could not be read"
        self._game_combo.blockSignals(True)
        self._game_combo.setCurrentIndex(i)
        self._game_combo.blockSignals(False)
        self._on_game_selected(i)
        ok = self._apply_to_game(confirm=False)
        return ok, self._status_label.text()
