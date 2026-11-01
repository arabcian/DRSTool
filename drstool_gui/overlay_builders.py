"""MangoHud and vkBasalt config builders."""

from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from .base import _atomic_write, _backup_file
from .theme import SCROLLABLE_CONTROL_QSS, T
from .tool_ui import _tool_btn
from .widgets import _LAUNCHER_QSS, _MONO_BOX_QSS, _NO_SCROLL_FILTER


# ============================================================================
# MangoHud / vkBasalt config builders
# ============================================================================

# (key, kind, default-or-choices, group, description)
#   kind: bool (bare key) | int | float | str | enum | color
MANGOHUD_OPTS: List[tuple] = [
    ("fps", "bool", "", "Performance", "FPS counter"),
    ("fps_only", "bool", "", "Performance", "Show only the FPS counter"),
    ("frametime", "bool", "", "Performance", "Frametime in ms"),
    ("frame_timing", "bool", "", "Performance", "Frametime graph"),
    ("histogram", "bool", "", "Performance", "Histogram instead of line graph"),
    ("fps_color_change", "bool", "", "Performance", "Colour the FPS by thresholds"),
    ("fps_value", "str", "30,60", "Performance", "FPS thresholds for the colour change"),
    ("fps_color", "str", "b22222,fdfd09,39f900", "Performance", "Colours: low, medium, high"),
    ("show_fps_limit", "bool", "", "Performance", "Show the active FPS limit"),
    ("fps_limit", "str", "0", "Performance", "FPS limit(s), e.g. 144 or 60,0 (0 = off)"),
    ("fps_limit_method", "enum", ["late", "early"], "Performance", "late = lower latency, early = smoother"),
    ("toggle_fps_limit", "str", "Shift_L+F1", "Performance", "Hotkey to cycle FPS limits"),
    ("throttling_status", "bool", "", "Performance", "Show GPU throttling status"),
    ("throttling_status_graph", "bool", "", "Performance", "Throttling history graph"),

    ("cpu_stats", "bool", "", "CPU", "CPU load"),
    ("cpu_temp", "bool", "", "CPU", "CPU temperature"),
    ("cpu_power", "bool", "", "CPU", "CPU power draw"),
    ("cpu_mhz", "bool", "", "CPU", "CPU clock"),
    ("core_load", "bool", "", "CPU", "Per-core load"),
    ("core_bars", "bool", "", "CPU", "Per-core load as bars"),
    ("cpu_load_change", "bool", "", "CPU", "Colour CPU load by thresholds"),
    ("cpu_text", "str", "CPU", "CPU", "Label text"),
    ("cpu_color", "color", "2e97cb", "CPU", "Label colour (RRGGBB)"),

    ("gpu_stats", "bool", "", "GPU", "GPU load"),
    ("gpu_temp", "bool", "", "GPU", "GPU temperature"),
    ("gpu_junction_temp", "bool", "", "GPU", "GPU junction/hotspot temperature"),
    ("gpu_mem_temp", "bool", "", "GPU", "GPU memory temperature"),
    ("gpu_power", "bool", "", "GPU", "GPU power draw"),
    ("gpu_core_clock", "bool", "", "GPU", "GPU core clock"),
    ("gpu_mem_clock", "bool", "", "GPU", "GPU memory clock"),
    ("gpu_fan", "bool", "", "GPU", "GPU fan speed"),
    ("gpu_name", "bool", "", "GPU", "GPU name"),
    ("gpu_load_change", "bool", "", "GPU", "Colour GPU load by thresholds"),
    ("gpu_text", "str", "GPU", "GPU", "Label text"),
    ("gpu_color", "color", "2e9762", "GPU", "Label colour (RRGGBB)"),
    ("vram", "bool", "", "GPU", "VRAM usage"),
    ("vram_color", "color", "ad64c1", "GPU", "VRAM colour (RRGGBB)"),

    ("ram", "bool", "", "Memory / IO", "System RAM usage"),
    ("swap", "bool", "", "Memory / IO", "Swap usage"),
    ("procmem", "bool", "", "Memory / IO", "Process memory (resident)"),
    ("procmem_shared", "bool", "", "Memory / IO", "Process shared memory"),
    ("procmem_virt", "bool", "", "Memory / IO", "Process virtual memory"),
    ("io_read", "bool", "", "Memory / IO", "Disk read rate"),
    ("io_write", "bool", "", "Memory / IO", "Disk write rate"),
    ("ram_color", "color", "c26693", "Memory / IO", "RAM colour (RRGGBB)"),
    ("io_color", "color", "a491d3", "Memory / IO", "IO colour (RRGGBB)"),

    ("vulkan_driver", "bool", "", "Info", "Vulkan driver name/version"),
    ("gamemode", "bool", "", "Info", "GameMode status"),
    ("vkbasalt", "bool", "", "Info", "vkBasalt status"),
    ("wine", "bool", "", "Info", "Wine/Proton version"),
    ("engine_version", "bool", "", "Info", "Engine (DXVK / VKD3D) version"),
    ("engine_short_names", "bool", "", "Info", "Short engine names"),
    ("resolution", "bool", "", "Info", "Render resolution"),
    ("display_server", "bool", "", "Info", "Display server (X11 / Wayland)"),
    ("present_mode", "bool", "", "Info", "Present mode (FIFO / MAILBOX …)"),
    ("refresh_rate", "bool", "", "Info", "Display refresh rate"),
    ("hdr", "bool", "", "Info", "HDR state"),
    ("fsr", "bool", "", "Info", "FSR state (gamescope)"),
    ("arch", "bool", "", "Info", "Process architecture"),
    ("time", "bool", "", "Info", "Clock"),
    ("time_format", "str", "%T", "Info", "strftime format of the clock"),
    ("version", "bool", "", "Info", "MangoHud version"),
    ("battery", "bool", "", "Info", "Battery status"),
    ("battery_watt", "bool", "", "Info", "Battery power draw"),
    ("engine_color", "color", "eb5b5b", "Info", "Engine colour (RRGGBB)"),
    ("wine_color", "color", "eb5b5b", "Info", "Wine colour (RRGGBB)"),
    ("exec_name", "bool", "", "Info", "Executable name"),

    ("position", "enum", ["top-left", "top-center", "top-right", "middle-left", "middle-right",
                          "bottom-left", "bottom-center", "bottom-right"], "Layout", "HUD corner"),
    ("horizontal", "bool", "", "Layout", "Horizontal bar layout"),
    ("horizontal_stretch", "bool", "", "Layout", "Stretch the horizontal bar across the screen"),
    ("hud_compact", "bool", "", "Layout", "Compact layout"),
    ("hud_no_margin", "bool", "", "Layout", "Remove margins"),
    ("legacy_layout", "bool", "", "Layout", "Old single-column layout"),
    ("table_columns", "int", "3", "Layout", "Number of table columns"),
    ("font_size", "int", "24", "Layout", "Font size"),
    ("font_scale", "float", "1.0", "Layout", "Font scale"),
    ("font_file", "str", "", "Layout", "Path to a TTF font"),
    ("text_outline", "bool", "", "Layout", "Outline text"),
    ("width", "int", "", "Layout", "Fixed width (px)"),
    ("height", "int", "", "Layout", "Fixed height (px)"),
    ("offset_x", "int", "0", "Layout", "Horizontal offset"),
    ("offset_y", "int", "0", "Layout", "Vertical offset"),
    ("background_alpha", "float", "0.5", "Layout", "Background opacity (0-1)"),
    ("alpha", "float", "1.0", "Layout", "Overall opacity (0-1)"),
    ("round_corners", "int", "0", "Layout", "Corner radius"),
    ("background_color", "color", "020202", "Layout", "Background colour (RRGGBB)"),
    ("text_color", "color", "ffffff", "Layout", "Text colour (RRGGBB)"),
    ("frametime_color", "color", "00ff00", "Layout", "Frametime graph colour (RRGGBB)"),
    ("no_display", "bool", "", "Layout", "Start with the HUD hidden"),

    ("toggle_hud", "str", "Shift_R+F12", "Control / logging", "Hotkey to show/hide the HUD"),
    ("toggle_logging", "str", "Shift_L+F2", "Control / logging", "Hotkey to start/stop logging"),
    ("reload_cfg", "str", "Shift_L+F4", "Control / logging", "Hotkey to reload this config"),
    ("output_folder", "str", "", "Control / logging", "Folder for log files"),
    ("log_duration", "int", "30", "Control / logging", "Stop logging after N seconds"),
    ("autostart_log", "int", "", "Control / logging", "Start logging N seconds after launch"),
    ("log_interval", "int", "100", "Control / logging", "Log sample interval (ms)"),
]

MANGOHUD_PRESETS = {
    "Minimal FPS": {"fps": "", "fps_only": "", "position": "top-left", "font_size": "24"},
    "Frametime": {"fps": "", "frametime": "", "frame_timing": "", "position": "top-left",
                  "fps_color_change": "", "fps_value": "60,120"},
    "Everything (NVIDIA)": {"fps": "", "frametime": "", "frame_timing": "", "cpu_stats": "", "cpu_temp": "",
                            "gpu_stats": "", "gpu_temp": "", "gpu_power": "", "gpu_core_clock": "",
                            "vram": "", "ram": "", "vulkan_driver": "", "engine_version": "", "wine": "",
                            "present_mode": "", "position": "top-left", "background_alpha": "0.4"},
    "Latency / limiter": {"fps": "", "frametime": "", "frame_timing": "", "present_mode": "",
                          "show_fps_limit": "", "fps_limit_method": "late", "toggle_fps_limit": "Shift_L+F1",
                          "position": "top-right"},
}


def mangohud_conf_text(vals: Dict[str, str]) -> str:
    lines = ["# generated by DRSTool"]
    for k, v in vals.items():
        lines.append(k if v == "" else f"{k}={v}")
    return "\n".join(lines) + "\n"


def mangohud_env_string(vals: Dict[str, str]) -> str:
    parts = []
    for k, v in vals.items():
        parts.append(k if v == "" else f"{k}={v.replace(',', '+')}")
    return ",".join(parts)


def parse_kv_conf(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
        else:
            out[ln] = ""
    return out


def _save_text_dialog(parent: QWidget, title: str, default: Path, text: str) -> Optional[Path]:
    default.parent.mkdir(parents=True, exist_ok=True)
    path, _ = QFileDialog.getSaveFileName(parent, title, str(default), "Config (*.conf);;All files (*)")
    if not path:
        return None
    p = Path(path)
    try:
        if p.is_file():
            _backup_file(p)
        _atomic_write(p, text)
    except OSError as ex:
        QMessageBox.critical(parent, title, str(ex))
        return None
    return p


def _set_env_on_window(w: QWidget, name: str, value: str) -> bool:
    win = w.window()
    if hasattr(win, "set_env_var"):
        win.set_env_var(name, value)
        return True
    return False


class MangoHudBuilder(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: Dict[str, tuple] = {}
        self._group_labels: Dict[str, QLabel] = {}
        self._row_widgets: Dict[str, List[QWidget]] = {}
        self._saved_path: Optional[Path] = None
        root = QHBoxLayout(self)
        root.setContentsMargins(4, 8, 4, 4)
        root.setSpacing(10)

        # left: options
        left = QVBoxLayout()
        top = QHBoxLayout()
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter options…")
        self._filter.setClearButtonEnabled(True)
        self._filter.setMinimumWidth(170)
        self._filter.textChanged.connect(self._apply_filter)
        top.addWidget(self._filter, 1)
        for name in MANGOHUD_PRESETS:
            b = _tool_btn(name)
            b.clicked.connect(lambda _c=False, n=name: self._preset(n))
            top.addWidget(b)
        b = _tool_btn("Clear")
        b.clicked.connect(lambda: self._load({}))
        top.addWidget(b)
        left.addLayout(top)

        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setStyleSheet(SCROLLABLE_CONTROL_QSS)
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(4, 4, 8, 4)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        grid.setColumnStretch(2, 1)
        r = 0
        cur_group = None
        edit_ss = (f"QLineEdit{{background:{T['surface']};border:1px solid {T['border']};color:{T['text']};"
                   f"padding:1px 6px;border-radius:3px;min-height:22px;font-family:{T['mono']};font-size:11px;}}")
        for key, kind, dflt, group, desc in MANGOHUD_OPTS:
            if group != cur_group:
                cur_group = group
                gl = QLabel(group.upper())
                gl.setStyleSheet(f"color:{T['accent']}; font-size:11px; font-weight:700; padding-top:8px;")
                grid.addWidget(gl, r, 0, 1, 3)
                self._group_labels[group] = gl
                r += 1
            cb = QCheckBox(key)
            cb.setStyleSheet(f"color:{T['text']}; font-family:{T['mono']}; font-size:11px;")
            ed = None
            if kind == "enum":
                ed = QComboBox()
                ed.addItems(dflt)
                ed.setFixedHeight(22)
                ed.installEventFilter(_NO_SCROLL_FILTER)
                ed.currentIndexChanged.connect(lambda _i, k=key: self._on_edit(k))
            elif kind != "bool":
                ed = QLineEdit()
                ed.setPlaceholderText(str(dflt))
                ed.setStyleSheet(edit_ss)
                ed.setFixedWidth(130)
                ed.textEdited.connect(lambda _t, k=key: self._on_edit(k))
            cb.toggled.connect(lambda _c, k=key: self._refresh())
            d = QLabel(desc)
            d.setStyleSheet(f"color:{T['muted']}; font-size:11px;")
            grid.addWidget(cb, r, 0)
            if ed is not None:
                grid.addWidget(ed, r, 1)
            grid.addWidget(d, r, 2)
            self._rows[key] = (cb, ed, kind, dflt)
            self._row_widgets[key] = [w for w in (cb, ed, d) if w is not None]
            r += 1
        sc.setWidget(host)
        left.addWidget(sc, 1)
        root.addLayout(left, 3)

        # right: preview + actions
        right = QVBoxLayout()
        right.addWidget(self._cap("MangoHud.conf"))
        self._prev = QPlainTextEdit()
        self._prev.setReadOnly(True)
        self._prev.setStyleSheet(_MONO_BOX_QSS)
        right.addWidget(self._prev, 3)
        right.addWidget(self._cap("MANGOHUD_CONFIG (env form)"))
        self._envprev = QPlainTextEdit()
        self._envprev.setReadOnly(True)
        self._envprev.setStyleSheet(_MONO_BOX_QSS)
        self._envprev.setMaximumHeight(90)
        right.addWidget(self._envprev, 1)
        self._also_on = QCheckBox("also set MANGOHUD=1")
        self._also_on.setChecked(True)
        self._also_on.setStyleSheet(f"color:{T['text2']};")
        right.addWidget(self._also_on)
        for label, fn, primary in (("Set as MANGOHUD_CONFIG env var", self._to_env, True),
                                   ("Save config file…  (+ MANGOHUD_CONFIGFILE)", self._save, False),
                                   ("Load .conf…", self._load_file, False),
                                   ("Copy conf text", lambda: QApplication.clipboard().setText(self._prev.toPlainText()), False)):
            b = (_tool_btn(label, bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True) if primary
                 else _tool_btn(label))
            b.clicked.connect(fn)
            right.addWidget(b)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:11px;")
        right.addWidget(self._status)
        right.addStretch(1)
        holder = QWidget()
        holder.setLayout(right)
        holder.setMinimumWidth(300)
        holder.setMaximumWidth(420)
        root.addWidget(holder, 2)
        self._refresh()

    @staticmethod
    def _cap(t):
        l = QLabel(t)
        l.setStyleSheet(f"color:{T['muted']}; font-size:11px; font-weight:700;")
        return l

    def _on_edit(self, key):
        cb = self._rows[key][0]
        if not cb.isChecked():
            cb.setChecked(True)               # typing a value implies "enabled"
        self._refresh()

    def values(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for key, (cb, ed, kind, dflt) in self._rows.items():
            if not cb.isChecked():
                continue
            if kind == "bool":
                out[key] = ""
            elif kind == "enum":
                out[key] = ed.currentText()
            else:
                v = ed.text().strip() or (str(dflt) if dflt else "")
                if v:
                    out[key] = v.lstrip("#") if kind == "color" else v
        return out

    def _refresh(self):
        v = self.values()
        self._prev.setPlainText(mangohud_conf_text(v))
        self._envprev.setPlainText(mangohud_env_string(v))

    def _load(self, vals: Dict[str, str]):
        for key, (cb, ed, kind, dflt) in self._rows.items():
            cb.blockSignals(True)
            cb.setChecked(key in vals)
            cb.blockSignals(False)
            if ed is not None:
                if kind == "enum":
                    i = ed.findText(vals.get(key, ""))
                    ed.blockSignals(True)
                    ed.setCurrentIndex(max(0, i))
                    ed.blockSignals(False)
                else:
                    ed.setText(vals.get(key, "") if key in vals else "")
        self._refresh()

    def _preset(self, name):
        self._load(dict(MANGOHUD_PRESETS[name]))

    def _apply_filter(self, text):
        t = text.lower().strip()
        shown_groups = set()
        for key, ws in self._row_widgets.items():
            desc = ws[-1].text().lower()
            vis = (not t) or t in key or t in desc
            for w in ws:
                w.setVisible(vis)
            if vis:
                shown_groups.add(next(o[3] for o in MANGOHUD_OPTS if o[0] == key))
        for g, lbl in self._group_labels.items():
            lbl.setVisible(g in shown_groups)

    def _load_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load MangoHud config",
                                              str(Path.home() / ".config/MangoHud"), "Config (*.conf);;All (*)")
        if not path:
            return
        try:
            vals = parse_kv_conf(Path(path).read_text(errors="replace"))
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")
            return
        known = {k for k, *_ in MANGOHUD_OPTS}
        self._load({k: v for k, v in vals.items() if k in known})
        unk = [k for k in vals if k not in known]
        self._status.setText(f"Loaded {len(vals) - len(unk)} option(s)" +
                             (f"; ignored unknown: {', '.join(unk[:8])}" if unk else "."))

    def _to_env(self):
        v = self.values()
        if not v:
            self._status.setText("Nothing enabled.")
            return
        ok = _set_env_on_window(self, "MANGOHUD_CONFIG", mangohud_env_string(v))
        if ok and self._also_on.isChecked():
            _set_env_on_window(self, "MANGOHUD", "1")
        self._status.setText("MANGOHUD_CONFIG written to the Environment tab (option values containing "
                             "commas use '+' in this form)." if ok else "⚠ no main window")

    def _save(self):
        v = self.values()
        p = _save_text_dialog(self, "Save MangoHud config",
                              Path.home() / ".config/MangoHud/MangoHud.conf", mangohud_conf_text(v))
        if p is None:
            return
        self._saved_path = p
        default = Path.home() / ".config/MangoHud/MangoHud.conf"
        if p != default:
            _set_env_on_window(self, "MANGOHUD_CONFIGFILE", str(p))
            self._status.setText(f"Saved {p} and set MANGOHUD_CONFIGFILE.")
        else:
            self._status.setText(f"Saved {p} (MangoHud reads this path by default).")


# ── vkBasalt ────────────────────────────────────────────────────────────────

VKB_EFFECTS = [("cas", "Contrast Adaptive Sharpening"), ("dls", "Denoised Luma Sharpening"),
               ("fxaa", "FXAA"), ("smaa", "SMAA"), ("lut", "Colour LUT")]
# key, kind, default, effect-or-None, (min, max, step) / choices, description
VKB_PARAMS = [
    ("casSharpness", "float", 0.4, "cas", (0.0, 1.0, 0.05), "CAS strength (0 = none, 1 = max)"),
    ("dlsSharpness", "float", 0.5, "dls", (0.0, 1.0, 0.05), "DLS sharpness"),
    ("dlsDenoise", "float", 0.17, "dls", (0.0, 1.0, 0.01), "DLS denoise"),
    ("fxaaQualitySubpix", "float", 0.75, "fxaa", (0.0, 1.0, 0.05), "FXAA sub-pixel quality"),
    ("fxaaQualityEdgeThreshold", "float", 0.125, "fxaa", (0.0, 1.0, 0.005), "FXAA edge threshold"),
    ("fxaaQualityEdgeThresholdMin", "float", 0.0312, "fxaa", (0.0, 1.0, 0.001), "FXAA minimum edge threshold"),
    ("smaaEdgeDetection", "enum", "luma", "smaa", ["luma", "color"], "SMAA edge detection mode"),
    ("smaaThreshold", "float", 0.05, "smaa", (0.0, 0.5, 0.005), "SMAA threshold"),
    ("smaaMaxSearchSteps", "int", 32, "smaa", (0, 112, 1), "SMAA search steps"),
    ("smaaMaxSearchStepsDiag", "int", 16, "smaa", (0, 20, 1), "SMAA diagonal search steps"),
    ("smaaCornerRounding", "int", 25, "smaa", (0, 100, 1), "SMAA corner rounding"),
]


def vkbasalt_conf_text(effects: List[str], params: Dict[str, str], extra_effects: Dict[str, str],
                       reshade_tex: str, reshade_inc: str, toggle_key: str, on_launch: bool,
                       lut_file: str) -> str:
    names = list(effects) + list(extra_effects)
    lines = ["# generated by DRSTool"]
    lines.append("effects = " + (":".join(names) if names else ""))
    for n, p in extra_effects.items():
        lines.append(f"{n} = {p}")
    if reshade_tex:
        lines.append(f"reshadeTexturePath = {reshade_tex}")
    if reshade_inc:
        lines.append(f"reshadeIncludePath = {reshade_inc}")
    for k, v in params.items():
        lines.append(f"{k} = {v}")
    if "lut" in effects and lut_file:
        lines.append(f"lutFile = {lut_file}")
    lines.append(f"toggleKey = {toggle_key or 'Home'}")
    lines.append(f"enableOnLaunch = {'True' if on_launch else 'False'}")
    return "\n".join(lines) + "\n"


class VkBasaltBuilder(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._params: Dict[str, tuple] = {}
        root = QHBoxLayout(self)
        root.setContentsMargins(4, 8, 4, 4)
        root.setSpacing(10)
        left = QVBoxLayout()
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setStyleSheet(SCROLLABLE_CONTROL_QSS)
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(4, 4, 8, 4)
        lay.addWidget(MangoHudBuilder._cap("EFFECTS  (drag to reorder; checked ones run top→bottom)"))
        self._effects = QListWidget()
        self._effects.setStyleSheet(_LAUNCHER_QSS)
        self._effects.setDragDropMode(QListWidget.InternalMove)
        self._effects.setFixedHeight(28 * len(VKB_EFFECTS) + 10)
        for eid, label in VKB_EFFECTS:
            it = QListWidgetItem(f"{eid}  —  {label}")
            it.setData(Qt.UserRole, eid)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            self._effects.addItem(it)
        self._effects.itemChanged.connect(lambda _i: self._refresh())
        self._effects.model().rowsMoved.connect(lambda *_: self._refresh())
        lay.addWidget(self._effects)

        lay.addWidget(MangoHudBuilder._cap("PARAMETERS"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)
        for key, kind, dflt, eff, rng, desc in VKB_PARAMS:
            if kind == "enum":
                w = QComboBox()
                w.addItems(rng)
                w.setCurrentText(dflt)
                w.currentIndexChanged.connect(lambda _i: self._refresh())
                w.installEventFilter(_NO_SCROLL_FILTER)
            elif kind == "int":
                w = QSpinBox()
                w.setRange(int(rng[0]), int(rng[1]))
                w.setValue(int(dflt))
                w.valueChanged.connect(lambda _v: self._refresh())
                w.installEventFilter(_NO_SCROLL_FILTER)
            else:
                w = QDoubleSpinBox()
                w.setDecimals(4)
                w.setRange(rng[0], rng[1])
                w.setSingleStep(rng[2])
                w.setValue(dflt)
                w.valueChanged.connect(lambda _v: self._refresh())
                w.installEventFilter(_NO_SCROLL_FILTER)
            w.setToolTip(desc)
            self._params[key] = (w, kind, eff)
            lab = QLabel(f"{key}")
            lab.setToolTip(desc)
            lab.setStyleSheet(f"color:{T['text2']}; font-family:{T['mono']}; font-size:11px;")
            form.addRow(lab, w)
        lay.addLayout(form)

        lay.addWidget(MangoHudBuilder._cap("LUT / RESHADE / KEYS"))
        ff = QFormLayout()
        self._lut = QLineEdit()
        self._lut.setPlaceholderText("lutFile  (only used when 'lut' is checked)")
        self._rtex = QLineEdit()
        self._rtex.setPlaceholderText("reshadeTexturePath")
        self._rinc = QLineEdit()
        self._rinc.setPlaceholderText("reshadeIncludePath")
        self._key = QLineEdit("Home")
        self._on = QCheckBox("enableOnLaunch")
        self._on.setChecked(True)
        for w in (self._lut, self._rtex, self._rinc, self._key):
            w.textChanged.connect(lambda _t: self._refresh())
        self._on.toggled.connect(lambda _c: self._refresh())
        ff.addRow("lutFile", self._lut)
        ff.addRow("reshadeTexturePath", self._rtex)
        ff.addRow("reshadeIncludePath", self._rinc)
        ff.addRow("toggleKey", self._key)
        ff.addRow("", self._on)
        lay.addLayout(ff)
        lay.addWidget(MangoHudBuilder._cap("EXTRA RESHADE EFFECTS   (one per line:  name = /path/to/effect.fx)"))
        self._extra = QPlainTextEdit()
        self._extra.setStyleSheet(_MONO_BOX_QSS)
        self._extra.setFixedHeight(80)
        self._extra.textChanged.connect(self._refresh)
        lay.addWidget(self._extra)
        lay.addStretch(1)
        sc.setWidget(host)
        left.addWidget(sc, 1)
        root.addLayout(left, 3)

        right = QVBoxLayout()
        right.addWidget(MangoHudBuilder._cap("vkBasalt.conf"))
        self._prev = QPlainTextEdit()
        self._prev.setReadOnly(True)
        self._prev.setStyleSheet(_MONO_BOX_QSS)
        right.addWidget(self._prev, 1)
        self._also_on = QCheckBox("also set ENABLE_VKBASALT=1")
        self._also_on.setChecked(True)
        self._also_on.setStyleSheet(f"color:{T['text2']};")
        right.addWidget(self._also_on)
        for label, fn, primary in (("Save config file…  (+ VKBASALT_CONFIG_FILE)", self._save, True),
                                   ("Load .conf…", self._load_file, False),
                                   ("Copy conf text", lambda: QApplication.clipboard().setText(self._prev.toPlainText()), False)):
            b = (_tool_btn(label, bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True) if primary
                 else _tool_btn(label))
            b.clicked.connect(fn)
            right.addWidget(b)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:11px;")
        right.addWidget(self._status)
        holder = QWidget()
        holder.setLayout(right)
        holder.setMinimumWidth(300)
        holder.setMaximumWidth(420)
        root.addWidget(holder, 2)
        self._refresh()

    def checked_effects(self) -> List[str]:
        out = []
        for i in range(self._effects.count()):
            it = self._effects.item(i)
            if it.checkState() == Qt.Checked:
                out.append(it.data(Qt.UserRole))
        return out

    def _extra_effects(self) -> Dict[str, str]:
        out = {}
        for ln in self._extra.toPlainText().splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                n, p = ln.split("=", 1)
                if n.strip() and p.strip():
                    out[n.strip()] = p.strip()
        return out

    def conf_text(self) -> str:
        eff = self.checked_effects()
        params: Dict[str, str] = {}
        for key, (w, kind, e) in self._params.items():
            if e not in eff:
                continue
            if kind == "enum":
                params[key] = w.currentText()
            elif kind == "int":
                params[key] = str(w.value())
            else:
                params[key] = f"{w.value():g}"
        return vkbasalt_conf_text(eff, params, self._extra_effects(), self._rtex.text().strip(),
                                  self._rinc.text().strip(), self._key.text().strip(), self._on.isChecked(),
                                  self._lut.text().strip())

    def _refresh(self, *_):
        self._prev.setPlainText(self.conf_text())

    def _load_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load vkBasalt config",
                                              str(Path.home() / ".config/vkBasalt"), "Config (*.conf);;All (*)")
        if not path:
            return
        try:
            raw = {}
            for ln in Path(path).read_text(errors="replace").splitlines():
                ln = ln.split("#", 1)[0].strip()
                if "=" in ln:
                    k, v = ln.split("=", 1)
                    raw[k.strip()] = v.strip().strip('"')
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")
            return
        names = [n for n in raw.get("effects", "").split(":") if n]
        builtin = {e for e, _ in VKB_EFFECTS}
        order = [n for n in names if n in builtin]
        rest = [e for e, _ in VKB_EFFECTS if e not in order]
        self._effects.blockSignals(True)
        self._effects.clear()
        for eid in order + rest:
            label = dict(VKB_EFFECTS)[eid]
            it = QListWidgetItem(f"{eid}  —  {label}")
            it.setData(Qt.UserRole, eid)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if eid in order else Qt.Unchecked)
            self._effects.addItem(it)
        self._effects.blockSignals(False)
        for key, (w, kind, e) in self._params.items():
            if key in raw:
                try:
                    if kind == "enum":
                        w.setCurrentText(raw[key])
                    elif kind == "int":
                        w.setValue(int(float(raw[key])))
                    else:
                        w.setValue(float(raw[key]))
                except ValueError:
                    pass
        self._lut.setText(raw.get("lutFile", ""))
        self._rtex.setText(raw.get("reshadeTexturePath", ""))
        self._rinc.setText(raw.get("reshadeIncludePath", ""))
        self._key.setText(raw.get("toggleKey", "Home"))
        self._on.setChecked(raw.get("enableOnLaunch", "True").lower() != "false")
        self._extra.setPlainText("\n".join(f"{n} = {raw[n]}" for n in names if n not in builtin and n in raw))
        self._refresh()
        self._status.setText(f"Loaded {path}")

    def _save(self):
        p = _save_text_dialog(self, "Save vkBasalt config",
                              Path.home() / ".config/vkBasalt/vkBasalt.conf", self.conf_text())
        if p is None:
            return
        _set_env_on_window(self, "VKBASALT_CONFIG_FILE", str(p))
        if self._also_on.isChecked():
            _set_env_on_window(self, "ENABLE_VKBASALT", "1")
        self._status.setText(f"Saved {p}; VKBASALT_CONFIG_FILE set in the Environment tab.")
