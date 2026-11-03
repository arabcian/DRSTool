"""Detail editor for one DRS setting."""

import re
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from drstool_core.models import Setting
from drstool_core.validate import check_hex

from .field_check import watch
from .lookup import _parse_int_any
from .settings_manager import SettingsManager
from .sysinfo import drs_unavailable, unavail_banner
from .theme import btn_ss, caption, SCROLLABLE_CONTROL_QSS, T
from .widgets import _set_layout_enabled


# ============================================================================
# Setting Editor - Modern Card View
# ============================================================================


OPTION_BTN_SS = f"""
QPushButton{{
    background:{T['surface']}; border:1px solid {T['border_hi']}; border-radius:7px;
    color:{T['text2']}; padding:7px 10px; font-size:12px; font-weight:600; min-height:18px;
}}
QPushButton:hover{{ background:{T['hover']}; border-color:{T['accent_dim']}; color:{T['text']}; }}
QPushButton:checked{{
    background:qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 rgba(118,185,0,0.22), stop:1 rgba(118,185,0,0.08));
    border:1px solid {T['accent']}; color:{T['accent_hi']};
}}
QPushButton:disabled{{ color:{T['faint']}; border-color:{T['border']}; }}
"""
FLAG_BTN_SS = OPTION_BTN_SS + "QPushButton{ text-align:left; padding:6px 12px; }"

EDITOR_TITLE_SS = f"QLabel{{ color:{T['text']}; font-size:21px; font-weight:700; background:transparent; }}"
EDITOR_CRUMB_SS = (f"QLabel{{ color:{T['muted']}; font-size:10px; font-weight:700; letter-spacing:1.4px;"
                   " background:transparent; }")
EDITOR_ID_SS = (f"QLabel{{ background:{T['sunken']}; border:1px solid {T['border_hi']}; border-radius:6px;"
                f" padding:4px 10px; color:{T['muted']}; font-family:{T['mono']}; font-size:11px; }}")
EDITOR_VALUE_SS = (f"QLabel{{ background:rgba(118,185,0,0.12); border:1px solid {T['accent_dim']};"
                   f" border-radius:6px; padding:4px 10px; color:{T['accent_hi']}; font-family:{T['mono']};"
                   " font-size:11px; font-weight:700; }")
EDITOR_DESC_SS = (f"QLabel{{ color:{T['text2']}; font-size:13px; background:{T['surface']};"
                  f" border:1px solid {T['border']}; border-left:3px solid {T['accent_dim']};"
                  " border-radius:8px; padding:12px 14px; }")

def _make_scrollable_control_area():
    """
    Build the (control_widget, control_layout, scroll_area) trio shared by
    SettingEditorWidget and EnvVarEditorWidget for their control area.

    Wrapped in a QScrollArea because some settings/env-vars have a lot of
    options — e.g. the "AA Method" DRS setting has 47 enum values, and
    VKD3D_CONFIG has 33 flags each with its own description — and the
    right-hand editor panel isn't itself inside a scroll area, so that
    content used to just get visually cut off at the bottom of the window
    with no way to reach the rest of it. The header, description, and
    remove/clear button stay fixed; only this middle area scrolls.
    """
    control_widget = QWidget()
    control_layout = QVBoxLayout(control_widget)
    control_layout.setContentsMargins(0, 6, 10, 0)   # keep text off the scrollbar
    control_layout.setSpacing(6)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(control_widget)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setStyleSheet(SCROLLABLE_CONTROL_QSS)
    return control_widget, control_layout, scroll


class SettingEditorWidget(QWidget):
    cleared = Signal()  # emitted when the current setting's value is removed

    # NOTE: this widget used to have its own `setting_changed` signal, emitted
    # right after settings_manager.set_setting(...) in every setter below, in
    # addition to settings_manager's own `settings_changed` signal. Both were
    # connected to MainWindow._on_setting_changed, so every click did a full
    # sidebar rebuild twice and a full editor rebuild twice. Removed:
    # settings_manager.settings_changed is the single source of truth and
    # _update_display() (connected to it below) already keeps this widget
    # in sync.

    def __init__(self, settings_manager: SettingsManager):
        super().__init__()
        self.settings_manager = settings_manager
        self._current_setting: Optional[Setting] = None
        self._current_value: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self._crumb = QLabel()
        self._crumb.setStyleSheet(EDITOR_CRUMB_SS)
        layout.addWidget(self._crumb)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._name_label = QLabel()
        self._name_label.setStyleSheet(EDITOR_TITLE_SS)
        self._name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._name_label, 1)

        self._id_label = QLabel()
        self._id_label.setStyleSheet(EDITOR_ID_SS)
        self._id_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._id_label.setToolTip("DRS setting ID")
        header.addWidget(self._id_label, 0, Qt.AlignVCenter)

        self._value_label = QLabel()
        self._value_label.setStyleSheet(EDITOR_VALUE_SS)
        self._value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._value_label.setToolTip("Value written to DXVK_NVAPI_DRS_SETTINGS")
        header.addWidget(self._value_label, 0, Qt.AlignVCenter)
        self._value_label.hide()

        self._remove_btn = QPushButton("Remove")
        self._remove_btn.setToolTip("Remove this setting from the launch string")
        self._remove_btn.setCursor(Qt.PointingHandCursor)
        self._remove_btn.clicked.connect(self._remove_setting)
        self._remove_btn.setStyleSheet(btn_ss("danger", "sm"))
        header.addWidget(self._remove_btn, 0, Qt.AlignVCenter)
        self._remove_btn.hide()
        layout.addLayout(header)

        self._desc_label = QLabel()
        self._desc_label.setStyleSheet(EDITOR_DESC_SS)
        self._desc_label.setWordWrap(True)
        self._desc_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # Minimum vertical policy so long word-wrapped descriptions reserve
        # their full height before the control area below is laid out.
        self._desc_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self._desc_label)

        layout.addSpacing(4)
        self._value_caption = caption("Value")
        layout.addWidget(self._value_caption)

        self._control_widget, self._control_layout, control_scroll = _make_scrollable_control_area()
        layout.addWidget(control_scroll, 1)  # stretch=1: absorb all extra space

        self.settings_manager.settings_changed.connect(self._update_display)

    def set_setting(self, setting: Setting):
        self._current_setting = setting
        self._current_value = self.settings_manager.get_setting(setting.id)
        self._build_editor()

    def _build_editor(self):
        if not self._current_setting:
            return

        s = self._current_setting
        cur = self._current_value

        self._name_label.setText(s.name)
        self._crumb.setText(f"DRS SETTINGS  \u203a  {s.cat.upper()}")
        self._value_caption.setText({"enum": "CHOOSE ONE", "hex-preset": "PRESETS",
                                     "bitfield": "FLAGS", "numeric": "VALUE"}.get(s.type, "VALUE"))
        self._id_label.setText(s.id)
        if cur is not None:
            self._value_label.setText(f"= {cur}")
            self._value_label.show()
        else:
            self._value_label.hide()

        desc_text = s.detailed_desc if s.detailed_desc else s.desc
        self._desc_label.setText(desc_text)
        # Detailed descriptions vary a lot in length (a one-line desc vs. a
        # multi-sentence detailed_desc), which changes how many lines the
        # word-wrapped label needs. Explicitly recompute its height for the
        # current width now, and re-activate the layout, so the control area
        # added below it is positioned using the up-to-date height instead of
        # a stale one from the previous setting - otherwise long descriptions
        # could visually overlap the first control(s).
        self._desc_label.setMinimumHeight(0)
        self._desc_label.updateGeometry()
        self.layout().activate()

        self._clear_layout(self._control_layout)
        self._remove_btn.hide()
        _why = drs_unavailable(s)
        if _why:
            self._control_layout.addWidget(unavail_banner(
                _why, lambda: _set_layout_enabled(self._control_layout, True, 1)))

        if s.type == "enum":
            self._build_enum_control(s, cur)
        elif s.type == "hex-preset":
            self._build_preset_control(s, cur)
        elif s.type == "numeric":
            self._build_numeric_control(s, cur)
        elif s.type == "dec-hex":
            self._build_dec_hex_control(s, cur)
        elif s.type == "bitfield":
            self._build_bitfield_control(s, cur)
        else:
            self._build_hex_control(s, cur)

        # Push the control(s) to the top of the scroll area instead of
        # letting Qt vertically center them when the content is shorter
        # than the scroll area's viewport (which is what happens by default
        # when a QVBoxLayout with no stretch item lives inside a
        # setWidgetResizable(True) QScrollArea).
        self._control_layout.addStretch()
        if _why and cur is None:
            _set_layout_enabled(self._control_layout, False, 1)

        if cur is not None:
            self._remove_btn.show()

    def _clear_layout(self, layout):
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    def _build_enum_control(self, s: Setting, cur: Optional[str]):
        num_options = len(s.values)
        if num_options <= 2:
            cols = 2
        elif num_options <= 4:
            cols = 4
        else:
            cols = 5

        grid = QGridLayout()
        grid.setSpacing(4)

        # Enum settings accept exactly one value. Group the buttons so Qt
        # enforces single-selection (checking one unchecks the others) instead
        # of letting every button toggle independently — which previously left
        # several options highlighted at once.
        # One group per rebuild: parented to self, the previous ones were never
        # freed, so every click on an enum option leaked another QButtonGroup.
        if getattr(self, "_enum_group", None) is not None:
            self._enum_group.deleteLater()
        group = self._enum_group = QButtonGroup(self)
        self._enum_setting_id = s.id
        group.setExclusive(True)

        row, col = 0, 0
        for val in s.values:
            btn = QPushButton(val.name)
            btn.setCheckable(True)
            btn.setProperty("value", val.val)
            if val.val == cur:
                btn.setChecked(True)
            if val.val == s.default:
                btn.setText(f"* {val.name}")

            btn.setStyleSheet(OPTION_BTN_SS)

            group.addButton(btn)
            # `checked` reflects the button's state *after* the click. When the
            # user clicks the already-active option, Qt (with an exclusive
            # group) keeps it checked and re-fires with checked=True — treat
            # that as "toggle off" and remove the setting entirely, so the
            # highlight and the stored value never diverge.
            btn.clicked.connect(
                lambda checked, bid=s.id, bval=val.val: self._toggle_enum_value(bid, bval)
            )
            grid.addWidget(btn, row, col)

            col += 1
            if col >= cols:
                col = 0
                row += 1

        self._control_layout.addLayout(grid)

    def _build_preset_control(self, s: Setting, cur: Optional[str]):
        grid = QGridLayout()
        grid.setSpacing(4)

        row, col = 0, 0
        for preset in s.presets:
            btn = QPushButton(preset.name)
            btn.clicked.connect(lambda checked, bid=s.id, bval=preset.val: self._toggle_enum_value(bid, bval))

            if preset.val == cur:
                btn.setStyleSheet("""
QPushButton{
    background:rgba(118, 185, 0, 0.12);
    border:1px solid #76b900;
    border-radius:6px;
    color:#76b900;
    padding:5px;
    font-size:11px;
    font-weight:600;
}
QPushButton:hover{
    background:#1f2430;
    border:1px solid #76b900;
}
""")
            else:
                btn.setStyleSheet("""
QPushButton{
    background:#181c24;
    border:1px solid #2a313d;
    border-radius:6px;
    color:#dfe3ea;
    padding:5px;
    font-size:11px;
    font-weight:600;
}
QPushButton:hover{
    background:#1f2430;
    border:1px solid #76b900;
}
""")
            grid.addWidget(btn, row, col)

            col += 1
            if col >= 5:
                col = 0
                row += 1

        self._control_layout.addLayout(grid)

    def _build_numeric_control(self, s: Setting, cur: Optional[str]):
        hbox = QHBoxLayout()
        hbox.setSpacing(8)

        spin = QSpinBox()
        spin.setRange(s.min, s.max)
        # A malformed stored value (hand-edited profile, imported line) used to
        # raise ValueError inside the slot and leave a half-built editor.
        iv = _parse_int_any(cur) if cur is not None else None
        if iv is None:
            iv = _parse_int_any(s.default)
        spin.setValue(iv if iv is not None else s.min)
        spin.setFixedHeight(30)
        spin.setStyleSheet("""
QSpinBox{
    background:#181c24;
    border:1px solid #2a313d;
    border-radius:6px;
    color:#dfe3ea;
    padding:3px 8px;
    font-size:12px;
}
QSpinBox:focus{
    border:1px solid #76b900;
}
QSpinBox:hover{
    background:#1f2430;
}
""")
        spin.valueChanged.connect(lambda v: self._set_numeric(s.id, v))
        hbox.addWidget(spin)

        range_label = QLabel(f"Range: {s.min}–{s.max}")
        range_label.setStyleSheet("font-family: monospace; font-size: 11px; color: #7b8496;")
        hbox.addWidget(range_label)

        hbox.addStretch()
        self._control_layout.addLayout(hbox)

        # Optional one-click values (e.g. common refresh rates, "Auto").
        if s.presets:
            quick = QHBoxLayout()
            quick.setSpacing(4)
            cur_int = spin.value() if cur is not None else None
            for pr in s.presets:
                try:
                    pv = int(pr.val, 16)
                except ValueError:
                    continue
                b = QPushButton(pr.name)
                b.setCheckable(True)
                b.setChecked(cur_int is not None and pv == cur_int)
                b.setFixedHeight(28)
                b.setStyleSheet(f"""
QPushButton{{background:{T['raised']};border:1px solid {T['border_hi']};border-radius:6px;
    color:{T['text2']};padding:0 12px;font-size:11px;font-weight:600;}}
QPushButton:hover{{border-color:{T['accent']};}}
QPushButton:checked{{background:rgba(118,185,0,0.12);border-color:{T['accent']};color:{T['accent']};}}
""")
                b.clicked.connect(lambda _c=False, v=pv, sid=s.id: self._apply_numeric_preset(sid, v))
                quick.addWidget(b)
            quick.addStretch()
            self._control_layout.addLayout(quick)

    def _build_dec_hex_control(self, s: Setting, cur: Optional[str]):
        grid = QGridLayout()
        grid.setSpacing(6)

        dec_label = QLabel("Decimal:")
        dec_label.setStyleSheet("font-size: 11px; color: #7b8496;")
        grid.addWidget(dec_label, 0, 0)

        dec_spin = QSpinBox()
        # NOTE: QSpinBox uses a 32-bit *signed* int internally, so the true
        # unsigned 32-bit max (0xFFFFFFFF) overflows and raises OverflowError.
        # No current setting uses type="dec-hex"; if one ever does and needs
        # the full unsigned range, this control needs a QLineEdit + validator
        # instead of QSpinBox.
        dec_spin.setRange(0, 0x7FFFFFFF)
        if cur is not None:
            dec_spin.setValue(int(cur, 16))
        dec_spin.setFixedHeight(30)
        dec_spin.setStyleSheet("""
QSpinBox{
    background:#181c24;
    border:1px solid #2a313d;
    border-radius:6px;
    color:#dfe3ea;
    padding:3px 8px;
    font-size:12px;
}
QSpinBox:focus{
    border:1px solid #76b900;
}
QSpinBox:hover{
    background:#1f2430;
}
""")
        dec_spin.valueChanged.connect(lambda v: self._set_dec_hex(s.id, v))
        grid.addWidget(dec_spin, 0, 1)

        hex_label = QLabel("Hex: 0x")
        hex_label.setStyleSheet("font-size: 11px; color: #7b8496;")
        grid.addWidget(hex_label, 0, 2)

        hex_edit = QLineEdit()
        if cur is not None:
            hex_edit.setText(cur.replace("0x", "").upper())
        else:
            hex_edit.setText("0")
        hex_edit.setFixedHeight(30)
        hex_edit.setStyleSheet("""
QLineEdit{
    background:#181c24;
    border:1px solid #2a313d;
    border-radius:6px;
    color:#9be238;
    font-family:monospace;
    font-size:12px;
    padding:3px 8px;
}
QLineEdit:focus{
    border:1px solid #76b900;
}
QLineEdit:hover{
    background:#1f2430;
}
""")
        watch(hex_edit, check_hex)
        hex_edit.textChanged.connect(lambda t: self._set_hex_from_edit(s.id, t))
        grid.addWidget(hex_edit, 0, 3)

        self._control_layout.addLayout(grid)

    def _build_bitfield_control(self, s: Setting, cur: Optional[str]):
        cur_val = (_parse_int_any(cur) or 0) if cur else 0

        grid = QGridLayout()
        grid.setSpacing(3)

        row, col = 0, 0

        for bit in s.bits:
            # A bit is "active" if it's the value currently stored in its
            # sub-field. For a masked multi-bit field (mask != 0), that means
            # the masked-out bits of cur_val exactly equal this option's
            # value - so at most one button per mask lights up, matching the
            # mutually-exclusive semantics of a real bitfield sub-field.
            # Independent single-bit flags (mask == 0) just test their bit.
            if bit.mask:
                active = (cur_val & bit.mask) == bit.val
            elif bit.val == 0:
                # "Default"/0x0 flag: active only when nothing else is set.
                active = (cur_val == 0)
            else:
                active = bool(cur_val & bit.val)

            btn = QPushButton(f"{bit.name}\n0x{bit.val:08X}")
            btn.setCheckable(True)
            btn.setChecked(active)
            btn.setProperty("bit_value", bit.val)
            btn.clicked.connect(lambda checked, bid=s.id, bval=bit.val, bmask=bit.mask: self._toggle_bitfield(bid, bval, bmask))

            if active:
                btn.setStyleSheet("""
QPushButton{
    background:rgba(118, 185, 0, 0.12);
    border:1px solid #76b900;
    border-radius:6px;
    color:#76b900;
    padding:3px 5px;
    font-size:10px;
    font-family:monospace;
    min-height:30px;
}
QPushButton:hover{
    background:#1f2430;
    border:1px solid #76b900;
}
""")
            else:
                btn.setStyleSheet("""
QPushButton{
    background:#181c24;
    border:1px solid #2a313d;
    border-radius:6px;
    color:#dfe3ea;
    padding:3px 5px;
    font-size:10px;
    font-family:monospace;
    min-height:30px;
}
QPushButton:hover{
    background:#1f2430;
    border:1px solid #76b900;
}
""")

            grid.addWidget(btn, row, col)

            col += 1
            if col >= 3:
                col = 0
                row += 1

        self._control_layout.addLayout(grid)

        val_layout = QHBoxLayout()
        val_layout.setSpacing(8)

        calc_label = QLabel("Combined Value:")
        calc_label.setStyleSheet("font-size: 11px; color: #7b8496; font-family: monospace;")
        val_layout.addWidget(calc_label)

        calc_value = QLabel(f"0x{cur_val:08X}" if cur_val else "0x0")
        calc_value.setStyleSheet("font-size: 12px; color: #9be238; font-family: monospace;")
        calc_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        val_layout.addWidget(calc_value)

        val_layout.addStretch()
        self._control_layout.addLayout(val_layout)

    def _build_hex_control(self, s: Setting, cur: Optional[str]):
        hbox = QHBoxLayout()
        hbox.setSpacing(8)

        hex_label = QLabel("0x")
        hex_label.setStyleSheet("font-size: 11px; color: #7b8496;")
        hbox.addWidget(hex_label)

        hex_edit = QLineEdit()
        if cur is not None:
            hex_edit.setText(cur.replace("0x", "").upper())
        else:
            hex_edit.setText("0")
        hex_edit.setFixedHeight(30)
        hex_edit.setStyleSheet("""
QLineEdit{
    background:#181c24;
    border:1px solid #2a313d;
    border-radius:6px;
    color:#9be238;
    font-family:monospace;
    font-size:12px;
    padding:3px 8px;
}
QLineEdit:focus{
    border:1px solid #76b900;
}
QLineEdit:hover{
    background:#1f2430;
}
""")
        watch(hex_edit, check_hex)
        hex_edit.textChanged.connect(lambda t: self._set_hex_from_edit(s.id, t))
        hbox.addWidget(hex_edit)

        hbox.addStretch()
        self._control_layout.addLayout(hbox)

    def _toggle_enum_value(self, setting_id: str, value: str):
        # Single-select enum click: if this value is already the stored one the
        # user is clicking to deselect it, so remove the setting; otherwise set
        # it. This keeps the button highlight and the underlying value in sync.
        if self.settings_manager.get_setting(setting_id) == value:
            self.settings_manager.remove_setting(setting_id)
        else:
            self.settings_manager.set_setting(setting_id, value)

    def _set_value(self, setting_id: str, value: str):
        # settings_manager.set_setting() emits settings_changed synchronously,
        # which is connected to self._update_display() below — that already
        # refreshes self._current_value and rebuilds the editor. No need to
        # duplicate that work here.
        self.settings_manager.set_setting(setting_id, value)

    def _set_numeric(self, setting_id: str, value: int):
        hex_val = f"0x{value:X}"
        self.settings_manager.set_setting(setting_id, hex_val)

    def _apply_numeric_preset(self, setting_id: str, value: int):
        # The clicked button keeps focus inside the control area, which makes
        # _update_display() take its "user is typing" shortcut and skip the
        # rebuild — so the spin box and button highlight would go stale.
        # A preset click is a discrete choice: rebuild explicitly.
        self._set_numeric(setting_id, value)
        self._current_value = self.settings_manager.get_setting(setting_id)
        self._build_editor()

    def _set_dec_hex(self, setting_id: str, value: int):
        hex_val = f"0x{value:X}"
        self.settings_manager.set_setting(setting_id, hex_val)

    def _set_hex_from_edit(self, setting_id: str, value: str):
        # Strip an optional pasted "0x" first: removing only non-hex chars
        # turned "0x1F" into "01F" -> fine, but "0X1F" kept the leading 0 and
        # values longer than a DWORD were accepted. DRS values are 32-bit.
        value = value.strip()
        if value[:2].lower() == "0x":
            value = value[2:]
        clean = re.sub(r'[^0-9a-fA-F]', '', value)
        if len(clean) > 8:
            return
        if clean:
            hex_val = f"0x{clean.upper()}"
            self.settings_manager.set_setting(setting_id, hex_val)

    def _toggle_bitfield(self, setting_id: str, bit_val: int, mask: int = 0):
        cur = self.settings_manager.get_setting(setting_id)
        cur_val = (_parse_int_any(cur) or 0) if cur else 0

        if mask:
            # Multi-bit sub-field (e.g. 2 bits shared by 3 mutually-exclusive
            # options): clear just this sub-field's bits, then OR the new
            # value in - unless this option is already the active one, in
            # which case clicking it again clears the sub-field entirely
            # (same "click active = deselect" behavior as the enum grids).
            if (cur_val & mask) == bit_val:
                new_val = cur_val & ~mask
            else:
                new_val = (cur_val & ~mask) | bit_val
        elif bit_val == 0:
            # A "Default"/"None" style flag defined as 0x0 (e.g. Shim
            # Rendering Options' "Default") can't be XOR'd - cur_val ^ 0 is
            # always cur_val, so the button was a dead no-op. Treat it as an
            # explicit "clear every independent flag on this setting".
            new_val = 0
        else:
            # Independent single-bit flag: plain XOR toggle.
            new_val = cur_val ^ bit_val

        hex_val = f"0x{new_val:08X}"
        self.settings_manager.set_setting(setting_id, hex_val)

    def _remove_setting(self):
        if self._current_setting:
            # remove_setting() emits settings_changed, which drives
            # _update_display() to hide this widget since the value is gone.
            self.settings_manager.remove_setting(self._current_setting.id)

    def _update_display(self):
        if self._current_setting:
            new_value = self.settings_manager.get_setting(self._current_setting.id)
            if new_value is None:
                self._current_value = None
                # Rebuild so the option grid drops its stale "checked" state
                # (buttons unchecked, Remove Setting hidden) instead of
                # freezing on whatever was highlighted before removal.
                self._build_editor()
                self.cleared.emit()
                return
            if new_value == self._current_value:
                # Value unchanged (redundant signal) - nothing to do.
                return
            self._current_value = new_value
            # If the change came from a control the user is actively typing
            # into/dragging (it or a child still has focus), skip the full
            # rebuild: recreating the widgets would drop focus and cursor
            # position mid-edit. Just refresh the "= value" chip instead.
            #
            # Bitfield/enum controls are grids of QPushButtons: clicking one
            # gives THAT button focus, so it always satisfied this "still
            # has focus" check and skipped _build_editor() below - meaning
            # the pressed (and any other) button in the grid never got its
            # checked/highlight style refreshed after the click. Those
            # button grids have no mid-edit cursor/focus state worth
            # protecting, so force the full rebuild for them regardless of
            # focus; only line-edit/spinbox style controls take the
            # shortcut.
            focus_widget = QApplication.focusWidget()
            no_shortcut_types = ("bitfield", "enum", "hex-preset")   # preset grids are buttons too
            if (self._current_setting.type not in no_shortcut_types
                    and focus_widget is not None
                    and self._control_widget.isAncestorOf(focus_widget)):
                self._value_label.setText(f"= {new_value}")
                self._value_label.show()
                if not self._remove_btn.isVisible():
                    self._remove_btn.show()
                return
            if self._sync_enum_buttons(new_value):
                return
            self._build_editor()

    def _sync_enum_buttons(self, value: str) -> bool:
        """Same enum setting, new value: move the highlight instead of rebuilding every button
        (a full rebuild cost ~30 ms per click). False = nothing reusable, caller rebuilds."""
        s, grp = self._current_setting, getattr(self, "_enum_group", None)
        if s is None or s.type != "enum" or grp is None or getattr(self, "_enum_setting_id", None) != s.id:
            return False
        hit = next((b for b in grp.buttons() if b.property("value") == value), None)
        if hit is None:
            return False
        hit.setChecked(True)                  # the exclusive group unchecks the others
        self._value_label.setText(f"= {value}")
        self._value_label.show()
        self._remove_btn.show()
        return True
