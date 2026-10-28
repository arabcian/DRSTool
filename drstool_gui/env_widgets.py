"""Environment variable list and editor pages."""

import shlex
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from drstool_core.models import EnvVarDef

from .catalog_data import (
    ALL_ENV_VARS, DXVK_CONFIG_KEY_NAMES, DXVK_CONFIG_KEYS, ENV_CATEGORY_ORDER, LOG_ENV_OFF,
    LOG_ENV_ON, LOG_ENV_UNSET_OFF, LOG_VKD3D_CONFIG_FLAGS, VKD3D_CONFIG_DESCS,
    vkd3d_config_tokens,
)
from .favorites import FAVS
from .field_check import watch
from .lookup import _ENV_BY_NAME, _ENV_NAME_RX, CUSTOM_ENV_CAT, env_def
from .setting_editor import (
    _make_scrollable_control_area, EDITOR_CRUMB_SS, EDITOR_DESC_SS, EDITOR_ID_SS,
    EDITOR_TITLE_SS, EDITOR_VALUE_SS, FLAG_BTN_SS, OPTION_BTN_SS,
)
from .settings_manager import SettingsManager
from drstool_core.validate import check_int
from .sysinfo import dxvk_cfg_unavailable, env_unavailable, unavail_banner, vkd3d_flag_unavailable
from .theme import btn_ss, caption, T
from .widgets import (
    _NO_SCROLL_FILTER, _set_layout_enabled, BadgeElidedLabel, make_header_item, make_list_item,
    ROLE_COUNT, ROLE_KIND, ROLE_SET, ROLE_VALUE, setup_sidebar_list,
)


# ============================================================================
# Env Vars Tab Widget
# ============================================================================

# ============================================================================
# Env Var List Widget  (left sidebar — same pattern as SettingsListWidget)
# ============================================================================

class EnvVarsWidget(QListWidget):
    """
    Left-sidebar list of every env var category (DXVK, VKD3D-Proton, forks,
    NVIDIA, Proton, Wine, FLM, Gamescope, ...). Drawn by SidebarDelegate:
    set vars get a green dot and their value previewed on the right.
    """
    env_var_selected = Signal(str)   # emits var name
    env_changed = Signal()           # emits when a value changes (forwarded from editor)

    GAMESCOPE_KEY = "__GAMESCOPE_FLAGS__"

    def __init__(self, settings_manager: SettingsManager, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        # { var_name: current_value_str }  — single source of truth
        self._values: Dict[str, str] = {}
        self._gamescope_on = False
        self.only_set = False
        self.only_fav = False

        self.setSelectionMode(QListWidget.SingleSelection)
        self.itemClicked.connect(self._on_item_clicked)
        self.currentItemChanged.connect(self._on_current_item_changed)
        setup_sidebar_list(self)
        self.populate()

    @staticmethod
    def _categories() -> List[tuple]:
        """
        (label, [EnvVarDef]) in display order, derived from ALL_ENV_VARS.
        This used to be a hand-written list that silently left out the
        "DXVK Forks" / "VKD3D Forks" categories, so those vars existed in the
        catalog (and in profiles / Lutris sync) but never showed up here.
        Any category not named in ENV_CATEGORY_ORDER is appended at the end
        rather than dropped.
        """
        groups: Dict[str, List[EnvVarDef]] = {}
        for ev in ALL_ENV_VARS:
            groups.setdefault(ev.cat, []).append(ev)
        order = [c for c in ENV_CATEGORY_ORDER if c in groups]
        order += [c for c in groups if c not in order]
        return [(c, groups[c]) for c in order]

    def _all_categories(self) -> List[tuple]:
        """Catalog categories + one for set variables the catalog doesn't know
        (imported launch lines, hand-edited profiles) so they stay editable."""
        cats = self._categories()
        custom = sorted(n for n in self._values if n not in _ENV_BY_NAME)
        if custom:
            cats.append((CUSTOM_ENV_CAT, [env_def(n) for n in custom if env_def(n)]))
        return cats

    def populate(self, filter_text: str = ""):
        """
        (Re)build the list. With filter_text, only vars whose name, category,
        description or current value match are shown; empty categories are
        skipped entirely.
        """
        current_item = self.currentItem()
        current_name = current_item.data(Qt.UserRole) if current_item else None

        # clear()+re-add can fire spurious selection-changed signals while
        # typing in the search box — block them for the rebuild.
        self.blockSignals(True)
        self.clear()
        filter_lower = filter_text.lower()

        for cat_label, env_list in self._all_categories():
            if filter_text:
                matching = [
                    ev for ev in env_list
                    if filter_lower in ev.name.lower()
                    or filter_lower in ev.cat.lower()
                    or filter_lower in ev.desc.lower()
                    or filter_lower in self._values.get(ev.name, "").lower()
                ]
                show_gs = cat_label == "Gamescope" and "gamescope" in filter_lower
            else:
                matching = env_list
                show_gs = cat_label == "Gamescope"
            if self.only_set or self.only_fav:
                matching = [ev for ev in matching
                            if (not self.only_set or ev.name in self._values)
                            and (not self.only_fav or FAVS.is_fav("env", ev.name))]
                show_gs = show_gs and not self.only_fav and (not self.only_set or self._gamescope_on)
            if not matching and not show_gs:
                continue

            n_set = sum(1 for ev in matching if ev.name in self._values)
            if show_gs and self._gamescope_on:
                n_set += 1
            self.addItem(make_header_item(cat_label, n_set))

            if show_gs:
                self.addItem(make_list_item(
                    "Gamescope launch flags", self.GAMESCOPE_KEY,
                    is_set=self._gamescope_on,
                    value="on" if self._gamescope_on else "CLI builder",
                    kind="special"))

            for ev in matching:
                val = self._values.get(ev.name)
                self.addItem(make_list_item(ev.name, ev.name,
                                            is_set=val is not None, value=val or "",
                                            fav=FAVS.is_fav("env", ev.name),
                                            unavail=env_unavailable(ev)))

        if current_name:
            for i in range(self.count()):
                item = self.item(i)
                if item.data(Qt.UserRole) == current_name:
                    self.setCurrentItem(item)
                    break
        self.blockSignals(False)

    def set_gamescope_enabled(self, on: bool):
        if on != self._gamescope_on:
            self._gamescope_on = on
            self.refresh_colors()

    def refresh_colors(self):
        """Refresh set-state, value previews and header counts in place."""
        header = None
        count = 0
        for i in range(self.count()):
            item = self.item(i)
            if item.data(ROLE_KIND) == "header":
                if header is not None:
                    header.setData(ROLE_COUNT, count)
                header, count = item, 0
                continue
            name = item.data(Qt.UserRole)
            if name == self.GAMESCOPE_KEY:
                item.setData(ROLE_SET, self._gamescope_on)
                item.setData(ROLE_VALUE, "on" if self._gamescope_on else "CLI builder")
                count += 1 if self._gamescope_on else 0
                continue
            val = self._values.get(name)
            item.setData(ROLE_SET, val is not None)
            item.setData(ROLE_VALUE, val or "")
            if val is not None:
                count += 1
        if header is not None:
            header.setData(ROLE_COUNT, count)

    def _on_item_clicked(self, item):
        name = item.data(Qt.UserRole)
        if name:
            self.env_var_selected.emit(name)

    def _on_current_item_changed(self, current, previous):
        if current is None:
            return
        name = current.data(Qt.UserRole)
        if name:
            self.env_var_selected.emit(name)

    # ── Value store (read/written by EnvVarEditorWidget via MainWindow) ───────

    def set_value(self, name: str, value: str):
        if value and not _ENV_NAME_RX.fullmatch(name or ""):
            return          # never let a non-identifier name reach a shell line
        if value:
            self._values[name] = value
        else:
            self._values.pop(name, None)
        self.refresh_colors()
        self.env_changed.emit()

    def clear_value(self, name: str):
        self._values.pop(name, None)
        self.refresh_colors()
        self.env_changed.emit()

    def get_value(self, name: str) -> str:
        return self._values.get(name, "")

    def get_env_dict(self) -> Dict[str, str]:
        return self._values.copy()

    def get_env_string(self) -> str:
        # shlex.quote() only wraps a value in quotes if it actually needs it
        # (contains spaces, globs, etc), so plain hex/enum values are left
        # untouched. Without this, a value like DXVK_LOG_PATH=/home/cihan/my
        # logs silently split into two separate shell tokens when pasted.
        return " ".join(f"{k}={shlex.quote(v)}" for k, v in self._values.items()
                        if _ENV_NAME_RX.fullmatch(k))

    def reset_all_values(self):
        """Clear all env var values and refresh list colors."""
        self._values.clear()
        self.refresh_colors()
        self.env_changed.emit()

    def logging_state(self) -> str:
        """\"on\" / \"off\" if every logging var matches that profile, else \"mixed\"."""
        v = self._values
        if all(v.get(k) == x for k, x in LOG_ENV_ON.items()):
            return "on"
        if (all(v.get(k) == x for k, x in LOG_ENV_OFF.items())
                and not any(k in v for k in LOG_ENV_UNSET_OFF)
                and not (set(vkd3d_config_tokens(v.get("VKD3D_CONFIG", ""))) & set(LOG_VKD3D_CONFIG_FLAGS))):
            return "off"
        return "mixed"

    def set_logging(self, on: bool):
        """Master logging toggle: apply the ON or OFF profile in one step."""
        if on:
            self._values.update(LOG_ENV_ON)
        else:
            self._values.update(LOG_ENV_OFF)
            for k in LOG_ENV_UNSET_OFF:
                self._values.pop(k, None)
            cfg = [f for f in vkd3d_config_tokens(self._values.get("VKD3D_CONFIG", ""))
                   if f not in LOG_VKD3D_CONFIG_FLAGS]
            if cfg:
                self._values["VKD3D_CONFIG"] = ",".join(cfg)
            else:
                self._values.pop("VKD3D_CONFIG", None)
        self.refresh_colors()
        self.env_changed.emit()

    def load_values(self, values: Dict[str, str]):
        """Replace all values with the given dict and refresh."""
        # Profiles can be imported from other people's files. A key like
        # "X=1; curl …|sh #" was emitted verbatim as `KEY=value` in every
        # copied / synced launch line (only the value was shlex-quoted), i.e.
        # shell injection. Keep only real identifiers with string values.
        self._values = {str(k): str(v) for k, v in (values or {}).items()
                        if _ENV_NAME_RX.fullmatch(str(k)) and v is not None and str(v) != ""}
        self.refresh_colors()
        self.env_changed.emit()

    def get_full_combined_string(self) -> str:
        parts = []
        nvapi_str = self.settings_manager.get_full_env_string()
        if nvapi_str:
            parts.append(nvapi_str)
        env_str = self.get_env_string()
        if env_str:
            parts.append(env_str)
        return " ".join(parts)


# ============================================================================
# Env Var Editor Widget  (right panel — same pattern as SettingEditorWidget)
# ============================================================================

class EnvVarEditorWidget(QWidget):
    """
    Right-panel editor for a single env var.
    Header: large name + category badge + current-value badge.
    Body: description + input control + Clear button.
    Matches SettingEditorWidget layout exactly.
    """
    value_changed = Signal(str, str)   # (name, new_value)  empty = cleared

    _EDIT_SS = """
QLineEdit{
    background:#181c24;
    border:1px solid #2a313d;
    border-radius:6px;
    color:#dfe3ea;
    font-family:monospace;
    font-size:12px;
    padding:5px 10px;
}
QLineEdit:focus{ border:1px solid #76b900; }
QLineEdit:hover{ background:#1f2430; }
"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_ev: Optional[EnvVarDef] = None
        self._current_name: str = ""
        self._list_widget: Optional[EnvVarsWidget] = None   # back-ref set by MainWindow

        # Free-text fields (_build_text / _build_flags) used to call
        # _set_value() on every single keystroke via textChanged. _set_value
        # propagates into EnvVarsWidget (re-colors ~65 items) and emits
        # value_changed, which MainWindow forwards to a statusbar update and
        # an OutputBar refresh. Typing a path like
        # "/home/cihan/some/long/cache/dir" fired that whole chain ~30 times.
        # Debounce: update the header badge immediately (cheap, local-only),
        # but only commit to the shared list widget after a short pause.
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._commit_pending_value)
        self._pending_value: str = ""
        self._value_check = None          # validity check of the current field, if any

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self._crumb = QLabel()
        self._crumb.setStyleSheet(EDITOR_CRUMB_SS)
        layout.addWidget(self._crumb)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._name_label = QLabel()
        self._name_label.setStyleSheet(EDITOR_TITLE_SS + f"QLabel{{ font-family:{T['mono']}; font-size:19px; }}")
        self._name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._name_label, 1)

        self._cat_label = QLabel()
        self._cat_label.setStyleSheet(EDITOR_ID_SS)
        self._cat_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._cat_label, 0, Qt.AlignVCenter)

        # Elided badge: a long VKD3D_CONFIG / DXVK_HUD value used to set the
        # editor's minimum width (~1500px for a dozen flags), so narrowing the
        # window/splitter clipped the whole panel. Full value is in the
        # tooltip and copied on click.
        self._value_label = BadgeElidedLabel()
        self._value_label.setStyleSheet(EDITOR_VALUE_SS)
        header.addWidget(self._value_label, 0, Qt.AlignVCenter)
        self._value_label.hide()

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setToolTip("Remove this variable from the launch string")
        self._clear_btn.setCursor(Qt.PointingHandCursor)
        self._clear_btn.clicked.connect(self._on_clear)
        self._clear_btn.setStyleSheet(btn_ss("danger", "sm"))
        header.addWidget(self._clear_btn, 0, Qt.AlignVCenter)
        self._clear_btn.hide()
        layout.addLayout(header)

        self._desc_label = QLabel()
        self._desc_label.setStyleSheet(EDITOR_DESC_SS)
        self._desc_label.setWordWrap(True)
        self._desc_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._desc_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self._desc_label)

        layout.addSpacing(4)
        self._value_caption = caption("Value")
        layout.addWidget(self._value_caption)

        self._control_widget, self._control_layout, control_scroll = _make_scrollable_control_area()
        layout.addWidget(control_scroll, 1)

    # ── Public ────────────────────────────────────────────────────────────────

    def set_list_widget(self, lw: EnvVarsWidget):
        self._list_widget = lw

    def set_var(self, ev: EnvVarDef):
        # Flush any pending debounced edit for the *previous* var before
        # switching context, or that keystroke would either be lost or
        # (worse) get committed under the new var's name.
        if self._debounce_timer.isActive():
            self._debounce_timer.stop()
            self._commit_pending_value()
        self._current_ev = ev
        self._current_name = ev.name
        self._build()

    # ── Builder ───────────────────────────────────────────────────────────────

    def _build(self):
        ev = self._current_ev
        if not ev:
            return

        cur = self._list_widget.get_value(ev.name) if self._list_widget else ""

        self._name_label.setText(ev.name)
        self._cat_label.setText(ev.cat)
        self._crumb.setText(f"ENVIRONMENT  \u203a  {ev.cat.upper()}")
        self._value_caption.setText({"enum": "CHOOSE ONE", "flags": "TOGGLE FLAGS",
                                     "vkd3d_config": "TOGGLE FLAGS", "dxvk_config": "DXVK.CONF KEYS"}
                                    .get(ev.vtype, "VALUE"))

        if cur:
            self._value_label.setText(f"= {cur}")
            self._value_label.show()
            self._clear_btn.show()
        else:
            self._value_label.hide()
            self._clear_btn.hide()

        self._desc_label.setText(ev.desc)
        self._desc_label.setMinimumHeight(0)
        self._desc_label.updateGeometry()
        self.layout().activate()
        self._clear_layout(self._control_layout)
        self._value_check = None
        _why = env_unavailable(ev)
        if _why:
            self._control_layout.addWidget(unavail_banner(
                _why, lambda: _set_layout_enabled(self._control_layout, True, 1)))

        if ev.vtype == "enum" and ev.options:
            self._build_enum(ev, cur)
        elif ev.vtype == "vkd3d_config":
            self._build_vkd3d_config(ev, cur)
        elif ev.vtype == "dxvk_config":
            self._build_dxvk_config(ev, cur)
        elif ev.vtype == "flags" and ev.options:
            self._build_flags(ev, cur)
        else:
            self._build_text(ev, cur)

        # Same reasoning as SettingEditorWidget._build_editor(): without this,
        # short controls (e.g. a single line edit) get vertically centered
        # inside the scroll area instead of sitting at the top.
        self._control_layout.addStretch()
        if _why and not cur:
            _set_layout_enabled(self._control_layout, False, 1)

    def _clear_layout(self, layout):
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    def _build_enum(self, ev: EnvVarDef, cur: str):
        """Enum: same button-grid style as _build_enum_control."""
        cols = min(len(ev.options), 5)
        grid = QGridLayout()
        grid.setSpacing(4)

        # Enum env vars (e.g. on/off __GL_* switches) accept exactly one
        # value. Without a QButtonGroup, each button toggled independently
        # and both e.g. "0" and "1" could end up checked at once. Group them
        # so Qt enforces single-selection, same fix as _build_enum_control.
        # One group per rebuild: parented to self, the previous ones were never
        # freed, so every click on an enum option leaked another QButtonGroup.
        if getattr(self, "_enum_group", None) is not None:
            self._enum_group.deleteLater()
        group = self._enum_group = QButtonGroup(self)
        group.setExclusive(True)

        row, col = 0, 0
        for opt in ev.options:
            btn = QPushButton(opt)
            btn.setCheckable(True)
            if opt == cur:
                btn.setChecked(True)
            btn.setFixedHeight(30)
            btn.setStyleSheet(OPTION_BTN_SS)
            group.addButton(btn)
            # Same reasoning as SettingEditorWidget._build_enum_control's
            # _toggle_enum_value: with an exclusive QButtonGroup, clicking
            # the already-active option re-fires clicked(checked=True)
            # instead of unchecking it (Qt won't let an exclusive group end
            # up with zero checked buttons via a plain click). Previously
            # this connected straight to _set_value(opt), so clicking the
            # active option was a no-op and the only way to clear the var
            # was the separate "Clear Variable" button below - clicking a
            # highlighted option a second time never removed it. Route
            # through a toggle helper that clears the var in that case.
            btn.clicked.connect(lambda checked, o=opt: self._toggle_enum_option(o))
            grid.addWidget(btn, row, col)
            col += 1
            if col >= cols:
                col = 0
                row += 1
        self._control_layout.addLayout(grid)

    def _toggle_enum_option(self, opt: str):
        """Enum env-var button click: clicking the currently-set option
        clears the variable instead of re-setting the same value, mirroring
        DRS Settings' enum behavior."""
        cur = self._list_widget.get_value(self._current_name) if self._list_widget else ""
        if cur == opt:
            self._on_clear()
        else:
            self._set_value(opt)

    def _build_vkd3d_config(self, ev: EnvVarDef, cur: str):
        """
        VKD3D_CONFIG: checkable button grid, one button per flag.
        Active flags are parsed from the comma/semicolon-separated cur value.
        Toggling any button rebuilds the value string and emits it.
        """
        tokens = vkd3d_config_tokens(cur)
        active = set(tokens)
        # Tokens the grid doesn't know (flags removed/renamed upstream, fork
        # flags, typos) used to be dropped on the next toggle. Keep them
        # verbatim and append them after the grid's flags.
        self._vkd3d_extra = [t for t in tokens if t not in ev.options]

        # We keep a local dict so toggle logic can read state without re-querying widgets
        self._vkd3d_btns: Dict[str, QPushButton] = {}

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        # Flag column sized to its longest button, description takes the rest.
        # The old 1:2 stretch split made buttons ~500px wide on a wide panel
        # and squeezed the word-wrapped descriptions into tall narrow columns
        # on a narrow one.
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)

        # Column headers
        hdr_flag = QLabel("Flag")
        hdr_flag.setStyleSheet("color:#7b8496; font-size:10px; font-weight:600;")
        hdr_desc = QLabel("Description")
        hdr_desc.setStyleSheet("color:#7b8496; font-size:10px; font-weight:600;")
        grid.addWidget(hdr_flag, 0, 0)
        grid.addWidget(hdr_desc, 0, 1)

        # Separator line
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("border: none; border-top: 1px solid #1d222c;")
        sep.setFixedHeight(1)
        grid.addWidget(sep, 1, 0, 1, 2)

        for r_idx, flag in enumerate(ev.options):
            btn = QPushButton(flag)
            btn.setCheckable(True)
            btn.setChecked(flag in active)
            _fw = vkd3d_flag_unavailable(flag)
            if _fw and flag not in active:
                btn.setEnabled(False)
                btn.setToolTip("Not supported here: " + _fw)
            btn.setFixedHeight(30)
            self._apply_flag_btn_style(btn)
            btn.toggled.connect(lambda checked, f=flag: self._on_vkd3d_flag_toggled())
            self._vkd3d_btns[flag] = btn

            desc_text = VKD3D_CONFIG_DESCS.get(flag, "")
            desc_lbl = QLabel(desc_text)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet("color:#b3bac7; font-size:11px;")
            desc_lbl.setMinimumWidth(120)
            desc_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

            # Top-aligned so a 3-line description doesn't leave the button
            # floating in the middle of a tall row.
            grid.addWidget(btn,      r_idx + 2, 0, Qt.AlignTop)
            grid.addWidget(desc_lbl, r_idx + 2, 1, Qt.AlignVCenter)

        # Uniform button width = widest flag name, so the column lines up and
        # never grows past what the text needs.
        if self._vkd3d_btns:
            for b in self._vkd3d_btns.values():
                b.ensurePolished()      # stylesheet padding must count
            bw = max(b.sizeHint().width() for b in self._vkd3d_btns.values())
            for b in self._vkd3d_btns.values():
                b.setFixedWidth(bw)

        self._control_layout.addLayout(grid)
        if self._vkd3d_extra:
            note = QLabel("Kept as-is (not a current upstream flag — no effect unless your "
                          "vkd3d-proton build/fork knows it): " + ", ".join(self._vkd3d_extra))
            note.setWordWrap(True)
            note.setStyleSheet("color:#c9a227; font-size:10px;")
            self._control_layout.addWidget(note)

    # ── DXVK_CONFIG key picker ───────────────────────────────────────────
    @staticmethod
    def _parse_dxvk_config(cur: str):
        """'a = 1; b = 2' -> ([(key, value), ...] in order). Entries without '='
        are kept with value None so they round-trip verbatim."""
        out = []
        for part in (cur or "").split(";"):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
                out.append((k.strip(), v.strip()))
            else:
                out.append((part, None))
        return out

    def _build_dxvk_config(self, ev: EnvVarDef, cur: str):
        """
        DXVK_CONFIG: one row per curated dxvk.conf key (combo for Auto/True/False
        style keys, line edit for numbers). Unset rows are simply omitted from
        the value. Anything not in DXVK_CONFIG_KEYS stays in 'Other entries'
        verbatim, so hand-written keys are never lost.
        """
        entries = self._parse_dxvk_config(cur)
        known = {k: v for k, v in entries if k in DXVK_CONFIG_KEY_NAMES and v is not None}
        extras = [(f"{k} = {v}" if v is not None else k) for k, v in entries
                  if not (k in DXVK_CONFIG_KEY_NAMES and v is not None)]

        self._dxvkcfg_widgets: Dict[str, QWidget] = {}
        grid = QGridLayout()
        grid.setSpacing(6)
        grid.setColumnStretch(2, 1)
        for c, t in enumerate(("Key", "Value", "Description")):
            h = QLabel(t)
            h.setStyleSheet("color:#7b8496; font-size:10px; font-weight:600;")
            grid.addWidget(h, 0, c)

        combo_ss = ("QComboBox{background:#181c24;border:1px solid #2a313d;border-radius:6px;"
                    "color:#dfe3ea;padding:3px 8px;font-size:11px;}"
                    "QComboBox:hover{border:1px solid #76b900;}")
        for r, (key, kind, opt, desc) in enumerate(DXVK_CONFIG_KEYS, start=1):
            lbl = QLabel(key)
            lbl.setStyleSheet("color:#dfe3ea; font-family:monospace; font-size:11px;")
            val = known.get(key, "")
            if kind == "enum":
                w = QComboBox()
                w.addItem("")
                w.addItems(opt)
                # case-insensitive match (dxvk accepts true/True)
                for i in range(w.count()):
                    if w.itemText(i).lower() == val.lower():
                        w.setCurrentIndex(i)
                        break
                else:
                    if val:
                        w.addItem(val)
                        w.setCurrentText(val)
                w.setStyleSheet(combo_ss)
                w.setFixedHeight(28)
                w.setMinimumWidth(90)
                w.installEventFilter(_NO_SCROLL_FILTER)
                w.currentIndexChanged.connect(lambda _i: self._set_value(self._dxvkcfg_value()))
            else:
                w = QLineEdit(val)
                w.setStyleSheet(self._EDIT_SS)
                w.setPlaceholderText(opt)
                w.setFixedHeight(28)
                w.setMinimumWidth(90)
                w.textChanged.connect(lambda _t: self._on_dxvkcfg_text())
            self._dxvkcfg_widgets[key] = w
            _kw = dxvk_cfg_unavailable(key)
            if _kw and not val:
                w.setEnabled(False)
                w.setToolTip("Not supported here: " + _kw)
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet("color:#b3bac7; font-size:11px;")
            grid.addWidget(lbl, r, 0)
            grid.addWidget(w, r, 1)
            grid.addWidget(d, r, 2)
        self._control_layout.addLayout(grid)

        hint = QLabel("Other entries (any other dxvk.conf key, ; separated):")
        hint.setStyleSheet("color:#7b8496; font-size:10px; font-weight:600;")
        self._control_layout.addWidget(hint)
        self._dxvkcfg_extra = QLineEdit("; ".join(extras))
        self._dxvkcfg_extra.setStyleSheet(self._EDIT_SS)
        self._dxvkcfg_extra.setPlaceholderText(ev.placeholder)
        self._dxvkcfg_extra.setFixedHeight(32)
        self._dxvkcfg_extra.textChanged.connect(lambda _t: self._on_dxvkcfg_text())
        self._control_layout.addWidget(self._dxvkcfg_extra)

    def _dxvkcfg_value(self) -> str:
        parts = []
        for key in DXVK_CONFIG_KEY_NAMES:
            w = self._dxvkcfg_widgets.get(key)
            v = (w.currentText() if isinstance(w, QComboBox) else w.text()).strip() if w else ""
            if v:
                parts.append(f"{key} = {v}")
        extra = self._dxvkcfg_extra.text().strip() if hasattr(self, "_dxvkcfg_extra") else ""
        parts += [p.strip() for p in extra.split(";") if p.strip()]
        return "; ".join(parts)

    def _on_dxvkcfg_text(self):
        """Text rows: debounce like other free-text fields."""
        v = self._dxvkcfg_value()
        self._update_value_badge(v)
        self._pending_value = v
        self._debounce_timer.start(200)

    def _apply_flag_btn_style(self, btn: QPushButton):
        btn.setStyleSheet(FLAG_BTN_SS)

    def _on_vkd3d_flag_toggled(self):
        """Rebuild VKD3D_CONFIG value from current checkbox states."""
        if not hasattr(self, '_vkd3d_btns'):
            return
        active = [f for f, btn in self._vkd3d_btns.items() if btn.isChecked()]
        self._set_value(",".join(active + getattr(self, "_vkd3d_extra", [])))

    def _build_flags(self, ev: EnvVarDef, cur: str):
        """
        Toggleable multi-select flags (currently only DXVK_HUD uses this
        vtype): same checkable button-grid pattern as _build_vkd3d_config,
        just without per-flag descriptions since DXVK_HUD's flags
        (fps, memory, devinfo, ...) are self-explanatory single words.

        A couple of DXVK_HUD's options aren't simple on/off toggles though —
        "scale=N" takes a parameter — so anything containing "=" is pulled
        out of the button grid and left editable in a small text field
        underneath, and both are combined into the final comma-separated
        value.
        """
        plain_options = [o for o in ev.options if "=" not in o]
        param_options = [o for o in ev.options if "=" in o]

        active_tokens = [t.strip() for t in cur.replace(";", ",").split(",") if t.strip()] if cur else []
        active_plain = set(t for t in active_tokens if t in plain_options)
        # Anything active that isn't a plain toggle (a "scale=2", or some
        # unrecognized token) is preserved verbatim in the text field rather
        # than silently dropped.
        self._flag_extra_tokens = [t for t in active_tokens if t not in plain_options]

        self._flag_btns: Dict[str, QPushButton] = {}
        cols = min(len(plain_options), 5) or 1
        grid = QGridLayout()
        grid.setSpacing(4)
        row, col = 0, 0
        for opt in plain_options:
            btn = QPushButton(opt)
            btn.setCheckable(True)
            btn.setChecked(opt in active_plain)
            btn.setFixedHeight(30)
            self._apply_flag_btn_style(btn)
            btn.toggled.connect(lambda checked, o=opt: self._on_flag_toggled(o, checked))
            self._flag_btns[opt] = btn
            grid.addWidget(btn, row, col)
            col += 1
            if col >= cols:
                col = 0
                row += 1
        self._control_layout.addLayout(grid)

        if param_options:
            hint = QLabel("Also available (type manually, comma-separated): " + ", ".join(param_options))
            hint.setStyleSheet("color:#3a4a5a; font-size:10px; font-family:monospace;")
            hint.setWordWrap(True)
            self._control_layout.addWidget(hint)

        # Always shown: tokens without a button (e.g. 'warn+dll' in WINEDEBUG)
        # were kept in the value but invisible and uneditable.
        extra_edit = QLineEdit(",".join(self._flag_extra_tokens))
        extra_edit.setStyleSheet(self._EDIT_SS)
        extra_edit.setPlaceholderText("e.g. scale=2" if param_options else "other tokens, comma-separated")
        extra_edit.setFixedHeight(32)
        extra_edit.textChanged.connect(self._on_extra_flags_changed)
        self._control_layout.addWidget(extra_edit)

    def _on_flag_toggled(self, opt: str, checked: bool):
        """Button click: commit immediately, same as VKD3D_CONFIG's flags.
        Checking a flag from the var's exclusive group (e.g. WINEDEBUG's
        -all / +all base levels) unchecks the rest of that group."""
        ev = self._current_ev
        if checked and ev and opt in ev.exclusive:
            for o in ev.exclusive:
                b = self._flag_btns.get(o)
                if o != opt and b is not None and b.isChecked():
                    b.blockSignals(True)
                    b.setChecked(False)
                    b.blockSignals(False)
        self._commit_flags()

    def _on_extra_flags_changed(self, text: str):
        """Free-text 'scale=N'-style field: debounce like other text fields."""
        self._flag_extra_tokens = [t.strip() for t in text.split(",") if t.strip()]
        combined = self._combined_flags_value()
        self._update_value_badge(combined.strip())
        self._pending_value = combined
        self._debounce_timer.start(200)

    def _combined_flags_value(self) -> str:
        active = [f for f, btn in self._flag_btns.items() if btn.isChecked()]
        return ",".join(active + self._flag_extra_tokens)

    def _commit_flags(self):
        self._set_value(self._combined_flags_value())

    def _build_text(self, ev: EnvVarDef, cur: str):
        """String / int: plain line edit."""
        edit = QLineEdit(cur)
        edit.setStyleSheet(self._EDIT_SS)
        edit.setPlaceholderText(ev.placeholder or ev.default or "")
        edit.setFixedHeight(32)
        if ev.vtype == "int":
            # Vars declared as "int" (FLM_TARGET_FPS, FLM_RT_PRIORITY, ...) only
            # ever hold a non-negative integer. A typo like "6o" is shown as a
            # warning and is not committed, so the layer never sees it.
            # String-typed vars (paths, CPU ranges) are not checked here.
            warn = QLabel()
            warn.setStyleSheet("color:#d9534f; font-size:11px;")
            self._value_check = watch(edit, check_int, warn)
        edit.textChanged.connect(self._on_text_changed)
        self._control_layout.addWidget(edit)
        if ev.vtype == "int":
            self._control_layout.addWidget(warn)

    # ── Setters ───────────────────────────────────────────────────────────────

    def _set_value(self, value: str):
        """Immediate commit: used by buttons (enum/vkd3d flags) which don't
        fire repeatedly the way keystrokes do, so no debounce needed here."""
        if not self._current_name:
            return
        # An immediate commit supersedes any pending debounced text: otherwise
        # e.g. a combo/flag click within 200 ms of typing was overwritten by
        # the timer firing with the older value.
        self._debounce_timer.stop()
        if self._list_widget:
            self._list_widget.set_value(self._current_name, value.strip())
        self.value_changed.emit(self._current_name, value.strip())
        self._update_value_badge(value.strip())

    def _update_value_badge(self, cur: str):
        """Cheap, local-only header refresh — safe to call on every keystroke."""
        if cur:
            self._value_label.setText(f"= {cur}")
            self._value_label.show()
            self._clear_btn.show()
        else:
            self._value_label.hide()
            self._clear_btn.hide()

    def _on_text_changed(self, value: str):
        """Handler for free-text fields (_build_text / _build_flags). Updates
        the header badge immediately, but defers the expensive propagation
        into the shared EnvVarsWidget (which re-colors ~65 sidebar items and
        triggers a statusbar/output-bar refresh) until typing pauses."""
        self._update_value_badge(value.strip())
        self._pending_value = value
        self._debounce_timer.start(200)

    def _commit_pending_value(self):
        if self._value_check is not None and not self._value_check():
            return                        # invalid: keep the last valid value, the warning stays visible
        self._set_value(self._pending_value)

    def discard_pending_edit(self):
        """Cancel any pending debounced keystroke without committing it.
        Call this before hiding/resetting the editor out from under the
        user (profile load, reset-all) so a stale edit can't land on the
        wrong var afterwards."""
        self._debounce_timer.stop()

    def _on_clear(self):
        if not self._current_name:
            return
        self._debounce_timer.stop()  # discard any pending unsaved keystroke
        if self._list_widget:
            self._list_widget.clear_value(self._current_name)
        self.value_changed.emit(self._current_name, "")
        self._value_label.hide()
        self._clear_btn.hide()
        self._build()   # rebuild control to reset input widget state
