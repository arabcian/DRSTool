"""Gamescope command-line flag builder."""

import shlex
from typing import Dict, List, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from drstool_core.models import GamescopeFlagDef

from .base import _as_dict
from .catalog_data import GAMESCOPE_FLAGS
from .widgets import _NO_SCROLL_FILTER


def normalize_gs_flags(flags) -> Tuple[Dict[str, str], List[str]]:
    """Hand-edited profiles may use short flags (-W) or other case; map them to the
    long flag names the UI uses. Returns (flags, dropped) - unknown keys are reported."""
    by_key: Dict[str, str] = {}
    for gf in GAMESCOPE_FLAGS:
        by_key[gf.flag.lower()] = gf.flag
        if gf.short:
            by_key[gf.short] = gf.flag          # short flags are case-sensitive (-W vs -w)
    out: Dict[str, str] = {}
    dropped: List[str] = []
    for k, v in _as_dict(flags).items():
        k = str(k)
        key = k if k == GamescopeFlagsWidget.MANGOHUD_KEY else (
            by_key.get(k) or by_key.get(k.lower()))
        if key is None:
            dropped.append(k)
        else:
            out[key] = str(v)
    return out, dropped


# ============================================================================
# Gamescope — command-line flag builder
# ============================================================================

class GamescopeFlagsWidget(QWidget):
    # Pseudo-flag key for the MangoHud prefix. Deliberately not '-'-prefixed
    # so it can never be mistaken for a real gamescope argument by the
    # leftover-flag packer or the CLI parser.
    MANGOHUD_KEY = "mangohud"
    MANGOHUD_PREFIX = "mangohud --dlsym"

    """
    CLI-flag builder for `gamescope` itself (see GAMESCOPE_FLAGS).
    A master 'Enable Gamescope' checkbox gates every control below it; while
    unchecked, the whole form is disabled and the preview/output is empty.
    Produces a real invocation ending in a bare `--`, e.g.:
        gamescope -W 2560 -H 1440 --hdr-enabled --adaptive-sync --
    This tool isn't Steam-specific, so nothing after `--` is assumed —
    whoever launches this (Steam, Lutris, a shell script, ...) appends their
    own command themselves.
    Genuine Gamescope *env vars* (Steam-side) stay in the Env Vars tab under
    the "Gamescope" category — this widget only deals with argv flags.
    """
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._controls: Dict[str, QWidget] = {}   # flag -> control widget

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        header = QLabel("Gamescope Command Builder")
        header.setStyleSheet("color:#e8eaf0; font-size:14px; font-weight:700;")
        outer.addWidget(header)

        desc = QLabel(
            "These are real `gamescope` command-line arguments (per `gamescope --help`), "
            "not environment variables — gamescope only reads them from argv. Enable the "
            "wrapper below to build a launch command; Steam-side Gamescope env vars "
            "(STEAM_GAMESCOPE_*, ENABLE_GAMESCOPE_WSI, ...) are still in the Env Vars tab."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#a8adb8; font-size:12px;")
        outer.addWidget(desc)

        self._enable_chk = QCheckBox("Enable Gamescope wrapper")
        self._enable_chk.setStyleSheet(
            "QCheckBox{color:#e8eaf0; font-size:13px; font-weight:600;} "
            "QCheckBox::indicator{width:16px; height:16px; border:1px solid #4a5468; "
            "border-radius:3px; background:#181c24;} "
            "QCheckBox::indicator:hover{border-color:#76b900;} "
            "QCheckBox::indicator:checked{background:#76b900; border:1px solid #76b900;}"
        )
        self._enable_chk.toggled.connect(self._on_master_toggled)
        outer.addWidget(self._enable_chk)

        # MangoHud is not a gamescope flag - it is a wrapper for the GAME, so
        # it belongs after gamescope's trailing '--', not among the arguments
        # gamescope itself parses. `--dlsym` makes the mangohud script hook
        # through dlsym() instead of relying on the loader, which is what makes
        # the overlay show up under a nested compositor and on OpenGL titles.
        self._mangohud_chk = QCheckBox(
            "Enable MangoHud  (appends `mangohud --dlsym` after `--`)")
        self._mangohud_chk.setStyleSheet(
            "QCheckBox{color:#e8eaf0; font-size:13px; font-weight:600;} "
            "QCheckBox::indicator{width:16px; height:16px; border:1px solid #4a5468; "
            "border-radius:3px; background:#181c24;} "
            "QCheckBox::indicator:hover{border-color:#76b900;} "
            "QCheckBox::indicator:checked{background:#76b900; border:1px solid #76b900;} "
            "QCheckBox:disabled{color:#5a606c;}"
        )
        self._mangohud_chk.setToolTip(
            "gamescope <flags> -- mangohud --dlsym <your command>\n\n"
            "Requires the `mangohud` wrapper script in PATH (media-libs/mangohud).\n"
            "On Lutris Game Sync this is written to system.prefix_command instead, "
            "which is where Lutris expects a command prefix."
        )
        self._mangohud_chk.setEnabled(False)
        self._mangohud_chk.toggled.connect(self._emit_changed)
        outer.addWidget(self._mangohud_chk)

        self._form_container = QWidget()
        self._form_container.setEnabled(False)
        form_outer = QVBoxLayout(self._form_container)
        form_outer.setContentsMargins(0, 0, 0, 0)
        form_outer.setSpacing(8)

        by_cat: Dict[str, List[GamescopeFlagDef]] = {}
        for gf in GAMESCOPE_FLAGS:
            by_cat.setdefault(gf.cat, []).append(gf)

        for cat, flags in by_cat.items():
            box = QGroupBox(cat)
            box.setStyleSheet(
                "QGroupBox{color:#d46a6a; font-size:12px; font-weight:700; "
                "border:1px solid #2a3244; border-radius:3px; margin-top:8px; padding-top:8px;} "
                "QGroupBox::title{subcontrol-origin:margin; left:8px; padding:0 4px;}"
            )
            grid = QFormLayout(box)
            grid.setSpacing(4)
            grid.setLabelAlignment(Qt.AlignRight)
            grid.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

            for gf in flags:
                label_txt = gf.name + (f"  ({gf.short})" if gf.short else "")
                lbl = QLabel(label_txt)
                lbl.setStyleSheet("color:#dde1e8; font-size:12px;")
                lbl.setToolTip(gf.desc)

                if gf.kind == "toggle":
                    ctrl = QCheckBox()
                    ctrl.setStyleSheet(
                        "QCheckBox::indicator{width:15px; height:15px; border:1px solid #4a5468; "
                        "border-radius:3px; background:#181c24;} "
                        "QCheckBox::indicator:hover{border-color:#76b900;} "
                        "QCheckBox::indicator:checked{background:#76b900; border:1px solid #76b900;} "
                        "QCheckBox::indicator:disabled{background:#14171d; border:1px solid #2a3040;}"
                    )
                    ctrl.toggled.connect(lambda _=None: self._emit_changed())
                elif gf.kind == "enum":
                    ctrl = QComboBox()
                    ctrl.addItem("")  # unset
                    ctrl.addItems(gf.options)
                    ctrl.currentTextChanged.connect(lambda _=None: self._emit_changed())
                    ctrl.installEventFilter(_NO_SCROLL_FILTER)
                    ctrl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                    ctrl.setMinimumWidth(110)
                else:  # "int" / "string"
                    ctrl = QLineEdit()
                    if gf.placeholder:
                        ctrl.setPlaceholderText(gf.placeholder)
                    ctrl.textChanged.connect(lambda _=None: self._emit_changed())

                ctrl.setToolTip(gf.desc)
                if gf.kind != "toggle":
                    ctrl.setStyleSheet(
                        "QLineEdit,QComboBox{background:#181d28; border:1px solid #3a4256; "
                        "color:#e8eaf0; font-size:12px; padding:2px 4px; min-height:22px; "
                        "border-radius:3px;} "
                        "QLineEdit:focus,QComboBox:focus{border-color:#76b900;} "
                        "QLineEdit:disabled,QComboBox:disabled{color:#6a7284; background:#14171d; "
                        "border-color:#242a38;} "
                        # Distinct fill so a combo box visibly reads as a
                        # selector, not a text field — but no custom
                        # ::drop-down / ::down-arrow / popup-view overrides,
                        # since those broke the native popup entirely.
                        # Qt's own arrow + popup rendering are left alone.
                        "QComboBox{background:#232b3d;} "
                        "QComboBox:on{border-color:#76b900;} "
                        "QComboBox:disabled{background:#14171d;}"
                    )
                self._controls[gf.flag] = ctrl
                grid.addRow(lbl, ctrl)

            form_outer.addWidget(box)

        outer.addWidget(self._form_container)

        # ── Live command preview ────────────────────────────────────────────
        preview_lbl = QLabel("Preview")
        preview_lbl.setStyleSheet("color:#a8adb8; font-size:12px; font-weight:700;")
        outer.addWidget(preview_lbl)

        self._preview = QLineEdit()
        self._preview.setReadOnly(True)
        self._preview.setStyleSheet(
            "QLineEdit{background:#0c0e12; border:1px solid #1d222c; color:#76b900; "
            "font-family:monospace; font-size:12px; padding:4px 6px;}"
        )
        outer.addWidget(self._preview)

        copy_row = QHBoxLayout()
        copy_row.addStretch()
        copy_btn = QPushButton("Copy Command")
        copy_btn.setStyleSheet(
            "QPushButton{background:#12151b; border:1px solid #1d222c; color:#e8eaf0; "
            "font-size:12px; padding:4px 10px; border-radius:3px;} "
            "QPushButton:hover{border-color:#4a7300;}"
        )
        copy_btn.clicked.connect(self._copy_command)
        copy_row.addWidget(copy_btn)
        outer.addLayout(copy_row)

        self.changed.connect(self._update_preview)
        self._update_preview()

    # ── internals ────────────────────────────────────────────────────────────

    def _on_master_toggled(self, checked: bool):
        self._form_container.setEnabled(checked)
        # The prefix is defined relative to gamescope's '--', so it only means
        # anything while the wrapper is on. Left checked-but-greyed so the
        # choice survives an off/on cycle.
        self._mangohud_chk.setEnabled(checked)
        self._emit_changed()

    def _emit_changed(self):
        self.changed.emit()

    def _copy_command(self):
        QApplication.clipboard().setText(self._preview.text())

    def is_enabled(self) -> bool:
        return self._enable_chk.isChecked()

    def mangohud_enabled(self) -> bool:
        """True only when the gamescope wrapper is on AND MangoHud is ticked —
        the prefix is positioned relative to gamescope's '--'."""
        return self.is_enabled() and self._mangohud_chk.isChecked()

    def get_flag_values(self) -> Dict[str, str]:
        """
        { flag: value } for every flag currently set.
        Bare toggles map to "" (present, no argument); everything else maps
        to its string value. Empty dict if the master toggle is off.
        """
        return self._collect_flags() if self.is_enabled() else {}

    def get_raw_flag_values(self) -> Dict[str, str]:
        """Like get_flag_values() but also while the wrapper is off (undo snapshots
        must not forget the flags a user switched off temporarily)."""
        return self._collect_flags()

    def _collect_flags(self) -> Dict[str, str]:
        values: Dict[str, str] = {}
        # Pseudo-flag: not a gamescope argument, but it rides along in the same
        # dict so profile save/load and the Lutris round-trip pick it up
        # without a second plumbing path. It can never collide with a real
        # entry — every GAMESCOPE_FLAGS key starts with '-'.
        if self._mangohud_chk.isChecked():
            values[self.MANGOHUD_KEY] = self.MANGOHUD_PREFIX
        for gf in GAMESCOPE_FLAGS:
            ctrl = self._controls[gf.flag]
            if gf.kind == "toggle":
                if ctrl.isChecked():
                    values[gf.flag] = ""
            elif gf.kind == "enum":
                v = ctrl.currentText().strip()
                if v:
                    values[gf.flag] = v
            else:
                v = ctrl.text().strip()
                if v:
                    values[gf.flag] = v
        return values

    def build_command(self, inner: str = "") -> str:
        """
        `gamescope <flags> -- [inner]` string, or '' if disabled.
        `inner` is left empty by default: this isn't a Steam-only tool, so
        we don't presume %command% — whoever launches this (Steam, Lutris,
        a raw shell script, ...) appends their own command after `--`.
        """
        if not self.is_enabled():
            return ""
        parts = ["gamescope"]
        for flag, val in self.get_flag_values().items():
            if flag == self.MANGOHUD_KEY:
                continue                      # goes AFTER '--', not before
            if val == "":
                parts.append(flag)
            else:
                parts.append(f"{flag} {shlex.quote(val)}")
        parts.append("--")
        # Everything past '--' wraps the game, so MangoHud sits here — in front
        # of whatever command the launcher appends.
        if self._mangohud_chk.isChecked():
            parts.append(self.MANGOHUD_PREFIX)
        if inner:
            parts.append(inner)
        return " ".join(parts)

    def _update_preview(self):
        self._preview.setText(self.build_command())

    def reset_all_values(self):
        self.load_flag_values(False, {})

    def load_flag_values(self, enabled: bool, values: Dict[str, str]):
        """Restore state, e.g. from a saved profile.

        Every control setter below used to emit `changed` on its own — ~60
        signals per profile load, each re-running the Lutris preview, badges
        and output bar. Batched into a single emission now."""
        self.blockSignals(True)
        try:
            self._load_flag_values(enabled, values)
        finally:
            self.blockSignals(False)
        self._emit_changed()

    def _load_flag_values(self, enabled: bool, values: Dict[str, str]):
        self._enable_chk.setChecked(enabled)
        self._mangohud_chk.setChecked(self.MANGOHUD_KEY in values)
        for gf in GAMESCOPE_FLAGS:
            ctrl = self._controls[gf.flag]
            v = values.get(gf.flag)
            if gf.kind == "toggle":
                ctrl.setChecked(gf.flag in values)
            elif gf.kind == "enum":
                # setCurrentText() on a non-editable combo silently does nothing
                # for a value it doesn't list, so the PREVIOUS profile's choice
                # stayed selected (and was written out again). Keep unknown
                # values selectable instead.
                if v and ctrl.findText(v) < 0:
                    ctrl.addItem(v)
                ctrl.setCurrentText(v or "")
            else:
                ctrl.setText(v or "")
