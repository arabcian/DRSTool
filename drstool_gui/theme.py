"""Colour tokens, appearance (theme / accent / density / scale), the app-wide stylesheet and
button/pill styles.
"""

import colorsys
import re
from typing import Dict

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from .base import APP_ID


# Shared style for the scrollable control area added to SettingEditorWidget
# and EnvVarEditorWidget (see _build_scrollable_control_area below). Matches
# the dark scrollbar look already used by the sidebar list widgets.
SCROLLABLE_CONTROL_QSS = """
QScrollArea{ border:none; background:transparent; }
QScrollArea > QWidget > QWidget{ background:transparent; }
QScrollBar:vertical{
    background:#0c0e12;
    width:5px;
}
QScrollBar::handle:vertical{
    background:#1d222c;
    border-radius:3px;
}
QScrollBar::handle:vertical:hover{
    background:#5a6070;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical{
    height:0px;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical{
    background:none;
}
"""


# ============================================================================
# Theme — one token set for the whole app
# ----------------------------------------------------------------------------
# Most widgets still carry their own local stylesheets (they predate this);
# these tokens + APP_QSS cover everything those sheets never styled
# (horizontal scrollbars, tooltips, menus, message boxes, combo popups) so
# no default-Fusion light chrome leaks into the dark UI anymore.
# ============================================================================

T = {
    # Layered graphite: sunken < bg < surface < raised < overlay
    "sunken":    "#08090c",
    "bg":        "#0c0e12",
    "surface":   "#12151b",
    "raised":    "#181c24",
    "overlay":   "#1f2430",
    "hover":     "#1c212b",
    "border":    "#1d222c",
    "border_hi": "#2a313d",
    "text":      "#eceff4",
    "text2":     "#b3bac7",
    "muted":     "#717a8b",
    "faint":     "#4a5262",
    "accent":    "#76b900",
    "accent_hi": "#93d81e",
    "accent_dim":"#3f6200",
    "accent_bg": "rgba(118,185,0,0.10)",
    "warn":      "#e3a73f",
    "danger":    "#ea5a50",
    "info":      "#5aa7ff",
    "mono":      "'JetBrains Mono','Fira Code','DejaVu Sans Mono',monospace",
    "radius":    "7px",
}


# ============================================================================
# Appearance: theme / accent / density / UI scale
# ----------------------------------------------------------------------------
# Read once at import time, BEFORE any stylesheet is built (every QSS constant
# below embeds T[...]), so changing it from the Appearance dialog needs a
# restart. Colour literals that are hard-coded inside stylesheets are mapped
# onto the chosen theme / accent by _restyle_qss(). With the defaults (dark +
# green) nothing is patched and the look is unchanged.
# ============================================================================

ACCENT_PRESETS = {
    "green": "#76b900", "blue": "#3d8bff", "cyan": "#1fb5c9", "violet": "#9b72f0",
    "orange": "#f08a24", "red": "#e5484d", "pink": "#e05297",
}
# density -> (list item height, list header height, Extra Tools navigation item height)
DENSITY_ROWS = {"compact": (25, 30, 42), "default": (30, 34, 50), "spacious": (36, 40, 58)}


def _load_appearance() -> dict:
    ap = {"theme": "dark", "accent": "green", "density": "default", "scale": 100, "restore": True}
    try:
        qs = QSettings(APP_ID, APP_ID)
        for k in ("theme", "accent", "density"):
            v = qs.value(f"appearance/{k}")
            if v:
                ap[k] = str(v)
        try:
            v = qs.value("appearance/scale")
            if v not in (None, ""):
                ap["scale"] = int(v)
        except (TypeError, ValueError):
            pass
        v = qs.value("appearance/restore_session")
        if v not in (None, ""):
            ap["restore"] = str(v).lower() not in ("false", "0", "no")
    except Exception:
        pass
    if ap["theme"] not in ("dark", "light"):
        ap["theme"] = "dark"
    if ap["accent"] not in ACCENT_PRESETS:
        ap["accent"] = "green"
    if ap["density"] not in DENSITY_ROWS:
        ap["density"] = "default"
    ap["scale"] = max(70, min(200, ap["scale"]))
    return ap


APPEARANCE = _load_appearance()
_LIGHT = APPEARANCE["theme"] == "light"
_ACC = ACCENT_PRESETS[APPEARANCE["accent"]]
_NEEDS_RESTYLE = _LIGHT or APPEARANCE["accent"] != "green"
ACCENT_RGB = (118, 185, 0)


def _hex_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _rgb_hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c * 255))) for c in (r, g, b))


if _LIGHT:
    T.update({
        "sunken": "#e8ebf0", "bg": "#f3f5f8", "surface": "#ffffff", "raised": "#eef1f5",
        "overlay": "#ffffff", "hover": "#e5e9ef", "border": "#d6dbe4", "border_hi": "#bcc4d1",
        "text": "#141a23", "text2": "#394252", "muted": "#667085", "faint": "#98a2b3",
        "warn": "#b4690e", "danger": "#c93a31", "info": "#2563c9",
    })
_ACC_H = colorsys.rgb_to_hls(*_hex_rgb(_ACC))[0]
if _NEEDS_RESTYLE:
    _h, _s, _v = colorsys.rgb_to_hsv(*_hex_rgb(_ACC))
    if _LIGHT:
        _acc_rgb = colorsys.hsv_to_rgb(_h, _s, _v * 0.84)
        T["accent"] = _rgb_hex(*_acc_rgb)
        T["accent_hi"] = _rgb_hex(*colorsys.hsv_to_rgb(_h, _s, _v * 0.68))
        T["accent_dim"] = _rgb_hex(*colorsys.hsv_to_rgb(_h, _s * 0.45, min(1.0, _v * 1.1)))
    else:
        _acc_rgb = _hex_rgb(_ACC)
        T["accent"] = _ACC
        T["accent_hi"] = _rgb_hex(*colorsys.hsv_to_rgb(_h, _s * 0.9, min(1.0, _v * 1.15)))
        T["accent_dim"] = _rgb_hex(*colorsys.hsv_to_rgb(_h, _s, _v * 0.55))
    ACCENT_RGB = tuple(round(c * 255) for c in _acc_rgb)
    T["accent_bg"] = "rgba(%d,%d,%d,%s)" % (*ACCENT_RGB, "0.12" if _LIGHT else "0.10")

# Colours that are already themed (T values) must not be mapped a second time.
_THEMED_HEX = {v.lower() for v in T.values() if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v)}
_HEX6_RE = re.compile(r"#[0-9a-fA-F]{6}\b")
_ACC_RGBA_RE = re.compile(r"rgba\(\s*118\s*,\s*185\s*,\s*0\s*,")
_hex_cache: Dict[str, str] = {}


def _map_hex(h: str) -> str:
    """Map a hard-coded dark-theme / NVIDIA-green literal onto the active appearance."""
    key = h.lower()
    if not _NEEDS_RESTYLE or key in _THEMED_HEX:
        return h
    got = _hex_cache.get(key)
    if got is not None:
        return got
    if key == "#76b900":
        out = T["accent"]
    else:
        hh, ll, ss = colorsys.rgb_to_hls(*_hex_rgb(key))
        if 0.16 <= hh <= 0.31 and ss >= 0.3 and APPEARANCE["accent"] != "green":
            hh = _ACC_H                                   # green family -> chosen accent hue
        if _LIGHT:
            if ss >= 0.5 and 0.45 <= ll <= 0.8:
                ll *= 0.78                                # saturated bright text/accents: darker on white
            else:
                ll = 1.0 - ll                             # dark surfaces <-> light surfaces
        out = _rgb_hex(*colorsys.hls_to_rgb(hh, ll, ss))
    _hex_cache[key] = out
    return out


def _restyle_qss(qss):
    if not qss or not _NEEDS_RESTYLE or not isinstance(qss, str):
        return qss
    qss = _HEX6_RE.sub(lambda m: _map_hex(m.group(0)), qss)
    return _ACC_RGBA_RE.sub("rgba(%d,%d,%d," % ACCENT_RGB, qss)


def _tc(h: str) -> str:
    """For QColor("#rrggbb") literals in paint code."""
    return _map_hex(h)


if _NEEDS_RESTYLE:
    _qw_set_ss, _qa_set_ss = QWidget.setStyleSheet, QApplication.setStyleSheet
    QWidget.setStyleSheet = lambda self, s: _qw_set_ss(self, _restyle_qss(s))
    QApplication.setStyleSheet = lambda self, s: _qa_set_ss(self, _restyle_qss(s))


APP_QSS = f"""
* {{ outline:0; }}
QWidget {{ color:{T['text2']}; selection-background-color:rgba(118,185,0,0.30); selection-color:{T['text']}; }}
QToolTip {{
    background:{T['overlay']}; color:{T['text']};
    border:1px solid {T['border_hi']}; padding:7px 9px; border-radius:6px;
}}
QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
QScrollBar::handle:vertical {{ background:{T['border_hi']}; border-radius:3px; min-height:32px; margin:0 2px; }}
QScrollBar::handle:vertical:hover {{ background:#3c4555; margin:0 1px; }}
QScrollBar:horizontal {{ background:transparent; height:10px; margin:2px; }}
QScrollBar::handle:horizontal {{ background:{T['border_hi']}; border-radius:3px; min-width:32px; margin:2px 0; }}
QScrollBar::handle:horizontal:hover {{ background:#3c4555; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; border:none; background:none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background:none; }}
QMenu {{
    background:{T['overlay']}; color:{T['text']};
    border:1px solid {T['border_hi']}; padding:5px; border-radius:8px;
}}
QMenu::item {{ padding:7px 22px 7px 14px; border-radius:5px; }}
QMenu::item:selected {{ background:rgba(118,185,0,0.16); color:{T['text']}; }}
QMenu::item:disabled {{ color:{T['faint']}; }}
QMenu::separator {{ height:1px; background:{T['border_hi']}; margin:5px 8px; }}
QMessageBox, QDialog {{ background:{T['surface']}; }}
QMessageBox QLabel {{ color:{T['text']}; }}
QPushButton {{
    background:{T['raised']}; border:1px solid {T['border_hi']}; border-radius:6px;
    color:{T['text']}; padding:5px 14px; min-height:20px;
}}
QPushButton:hover {{ background:{T['overlay']}; border-color:{T['accent_dim']}; }}
QPushButton:pressed {{ background:{T['hover']}; }}
QPushButton:checked {{ background:{T['accent_bg']}; border-color:{T['accent']}; color:{T['accent_hi']}; }}
QPushButton:disabled {{ color:{T['faint']}; border-color:{T['border']}; background:{T['surface']}; }}
QPushButton:default {{ border-color:{T['accent_dim']}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {{
    background:{T['sunken']}; border:1px solid {T['border_hi']}; border-radius:6px;
    color:{T['text']}; padding:4px 8px;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color:{T['accent']};
}}
QLineEdit:disabled, QSpinBox:disabled {{ color:{T['faint']}; }}
QComboBox {{
    background:{T['raised']}; border:1px solid {T['border_hi']}; border-radius:6px;
    color:{T['text']}; padding:4px 10px; min-height:22px;
}}
QComboBox:hover {{ border-color:{T['accent_dim']}; }}
QComboBox:on {{ border-color:{T['accent']}; }}
QComboBox QAbstractItemView {{
    background:{T['overlay']}; color:{T['text']}; border:1px solid {T['border_hi']};
    selection-background-color:rgba(118,185,0,0.18); selection-color:{T['text']}; padding:4px;
}}
QCheckBox, QRadioButton {{ color:{T['text2']}; spacing:8px; }}
QCheckBox::indicator {{
    width:15px; height:15px; border:1px solid {T['border_hi']}; border-radius:4px; background:{T['sunken']};
}}
QCheckBox::indicator:hover {{ border-color:{T['accent']}; }}
QCheckBox::indicator:checked {{ background:{T['accent']}; border-color:{T['accent']}; }}
QCheckBox::indicator:disabled {{ background:{T['surface']}; border-color:{T['border']}; }}
QGroupBox {{
    background:{T['surface']}; border:1px solid {T['border']}; border-radius:10px;
    margin-top:14px; padding:14px 10px 10px 10px; color:{T['text2']}; font-weight:600;
}}
QGroupBox::title {{
    subcontrol-origin:margin; left:12px; padding:0 6px;
    color:{T['muted']}; font-size:11px; letter-spacing:1px; text-transform:uppercase;
}}
QTabWidget::pane {{ border:none; border-top:1px solid {T['border']}; top:-1px; }}
QTabBar {{ qproperty-drawBase:0; }}
QTabBar::tab {{
    background:transparent; color:{T['muted']}; border:none;
    border-bottom:2px solid transparent; padding:7px 14px; font-weight:600;
}}
QTabBar::tab:hover {{ color:{T['text']}; }}
QTabBar::tab:selected {{ color:{T['text']}; border-bottom:2px solid {T['accent']}; }}
QTableWidget, QTreeWidget, QTableView, QTreeView {{
    background:{T['surface']}; alternate-background-color:{T['raised']};
    border:1px solid {T['border']}; border-radius:8px; gridline-color:{T['border']}; color:{T['text2']};
}}
QHeaderView::section {{
    background:{T['raised']}; color:{T['muted']}; border:none; border-bottom:1px solid {T['border']};
    padding:6px 8px; font-weight:600;
}}
QProgressBar {{ background:{T['sunken']}; border:none; border-radius:3px; }}
QProgressBar::chunk {{ background:{T['accent']}; border-radius:3px; }}
QStatusBar QLabel {{ color:{T['muted']}; }}
"""


# Reusable button looks. Local stylesheets beat APP_QSS, so widgets that need
# a specific role (primary / ghost / danger) set one of these explicitly.
def btn_ss(kind: str = "default", size: str = "md") -> str:
    pad = {"sm": "3px 10px", "md": "5px 14px", "lg": "7px 20px"}[size]
    base = (f"border-radius:6px; padding:{pad}; font-weight:600; font-size:12px;")
    if kind == "primary":
        return (f"QPushButton,QToolButton{{background:{T['accent']};border:1px solid {T['accent']};"
                f"color:#0a0f02;{base}}}"
                f"QPushButton:hover,QToolButton:hover{{background:{T['accent_hi']};border-color:{T['accent_hi']};}}"
                f"QPushButton:pressed,QToolButton:pressed{{background:#68a300;}}"
                f"QPushButton:disabled,QToolButton:disabled{{background:{T['raised']};color:{T['faint']};"
                f"border-color:{T['border']};}}"
                "QToolButton::menu-button{border-left:1px solid rgba(0,0,0,0.22);width:18px;}")
    if kind == "danger":
        return (f"QPushButton,QToolButton{{background:transparent;border:1px solid {T['border_hi']};"
                f"color:{T['danger']};{base}}}"
                f"QPushButton:hover,QToolButton:hover{{background:rgba(234,90,80,0.10);border-color:{T['danger']};}}"
                f"QPushButton:disabled{{color:{T['faint']};border-color:{T['border']};}}")
    if kind == "ghost":
        return (f"QPushButton,QToolButton{{background:transparent;border:1px solid transparent;"
                f"color:{T['text2']};{base}}}"
                f"QPushButton:hover,QToolButton:hover{{background:{T['hover']};color:{T['text']};}}"
                f"QPushButton:checked,QToolButton:checked{{background:{T['accent_bg']};color:{T['accent_hi']};}}"
                f"QPushButton:disabled,QToolButton:disabled{{color:{T['faint']};}}"
                "QToolButton::menu-button{width:14px;}")
    return (f"QPushButton,QToolButton{{background:{T['raised']};border:1px solid {T['border_hi']};"
            f"color:{T['text']};{base}}}"
            f"QPushButton:hover,QToolButton:hover{{background:{T['overlay']};border-color:{T['accent_dim']};}}"
            f"QPushButton:checked,QToolButton:checked{{background:{T['accent_bg']};border-color:{T['accent']};"
            f"color:{T['accent_hi']};}}"
            f"QPushButton:disabled,QToolButton:disabled{{color:{T['faint']};border-color:{T['border']};"
            f"background:{T['surface']};}}"
            "QToolButton::menu-button{width:16px;border-left:1px solid #2a313d;}")


def caption(text: str, color: str = "") -> QLabel:
    """Small uppercase section caption ("VALUE", "LAUNCH STRING" ...)."""
    lbl = QLabel(text.upper())
    lbl.setStyleSheet(f"color:{color or T['muted']}; font-size:10px; font-weight:700;"
                      " letter-spacing:1.4px; background:transparent;")
    return lbl


def pill_ss(col: str) -> str:
    """Rounded, tinted status pill (logging / lint state)."""
    c = QColor(col)
    rgb = f"{c.red()},{c.green()},{c.blue()}"
    return (f"QPushButton {{ color:{col}; background:rgba({rgb},0.10); border:1px solid rgba({rgb},0.45);"
            " border-radius:14px; padding:0 12px; font-size:11px; font-weight:700; }"
            f"QPushButton:hover {{ background:rgba({rgb},0.20); border-color:{col}; }}")


def apply_theme(app: "QApplication") -> None:
    """Palette + app-wide QSS + UI font. Called once from main()."""
    from PySide6.QtGui import QFontDatabase
    fams = set(QFontDatabase.families())
    for fam in ("Inter", "Inter Variable", "IBM Plex Sans", "Noto Sans", "Cantarell"):
        if fam in fams:
            f = QFont(fam)
            f.setPointSizeF(10)
            f.setHintingPreference(QFont.PreferNoHinting)
            app.setFont(f)
            break
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(T["bg"]))
    pal.setColor(QPalette.WindowText, QColor(T["text2"]))
    pal.setColor(QPalette.Base, QColor(T["surface"]))
    pal.setColor(QPalette.AlternateBase, QColor(T["raised"]))
    pal.setColor(QPalette.Text, QColor(T["text"]))
    pal.setColor(QPalette.Button, QColor(T["raised"]))
    pal.setColor(QPalette.ButtonText, QColor(T["text2"]))
    pal.setColor(QPalette.Highlight, QColor(T["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor(0, 0, 0))
    pal.setColor(QPalette.ToolTipBase, QColor(T["overlay"]))
    pal.setColor(QPalette.ToolTipText, QColor(T["text"]))
    pal.setColor(QPalette.PlaceholderText, QColor(T["faint"]))
    pal.setColor(QPalette.Link, QColor(T["accent"]))
    app.setPalette(pal)
    app.setStyleSheet(APP_QSS)
