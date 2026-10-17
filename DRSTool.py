#!/usr/bin/env python3
"""
DXVK NVAPI DRS Settings Configurator
Complete settings with detailed descriptions
Updated based on NvApiDriverSettings.h (latest)
"""

import sys
import os
import json
import re
import shlex
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal, QObject, QTimer, QProcess, QSize, QSettings, QRect
from datetime import datetime

from PySide6.QtGui import (QColor, QPalette, QFont, QIcon, QIntValidator, QPainter,
                           QFontMetrics, QKeySequence, QShortcut, QAction, QPen)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QStackedWidget,
    QLabel, QLineEdit, QPushButton, QSpinBox,
    QScrollArea, QFrame, QMessageBox,
    QStatusBar, QGridLayout, QSizePolicy,
    QPlainTextEdit, QCheckBox, QFileDialog,
    QComboBox, QGroupBox, QTabWidget,
    QButtonGroup, QFormLayout,
    QStyledItemDelegate, QStyle, QToolButton, QMenu,
    QDialog, QDoubleSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
)
import shutil
import glob
import signal
import tempfile
import time
import platform

try:
    import yaml
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class GPUArch:
    name: str
    arch: str
    prefix: str
    code: str
    example: str


@dataclass
class SettingValue:
    name: str
    val: str
    desc: str = ""


@dataclass
class BitField:
    name: str
    val: int
    # Optional explicit sub-field mask. When several BitFields share the same
    # mask (e.g. Override→App Ctrl=0x1, Override→Enhance=0x2,
    # Disable Override=0x3 all live in bits 0-1, mask=0x3), they represent
    # mutually-exclusive values of ONE multi-bit field rather than
    # independent flags. Previously this grouping wasn't modeled at all -
    # _toggle_bitfield guessed "is this a mask" purely from whether bit.val
    # itself was a power of two, so 0x1/0x2 were XOR'd independently while
    # 0x3 was mask-toggled, even though all three read/write the same two
    # bits. That let 0x1 and 0x2 be turned on together, silently producing
    # 0x3 (== "Disable Override") without that button ever being clicked,
    # and clicking 0x3 afterwards behaved inconsistently with the other two.
    # If left as 0 (default), the field falls back to per-bit XOR toggling
    # for plain independent flags - unaffected by this fix.
    mask: int = 0


@dataclass
class Preset:
    name: str
    val: str


@dataclass
class Setting:
    id: str
    name: str
    cat: str
    type: str
    default: str
    desc: str
    detailed_desc: str = ""
    values: List[SettingValue] = field(default_factory=list)
    bits: List[BitField] = field(default_factory=list)
    presets: List[Preset] = field(default_factory=list)
    min: int = 0
    max: int = 0


@dataclass
class EnvVarDef:
    """Definition of an environment variable with its metadata."""
    name: str
    cat: str          # "DXVK" or "VKD3D-Proton"
    vtype: str        # "string", "enum", "bool", "int", "flags"
    default: str
    desc: str
    options: List[str] = field(default_factory=list)  # for enum/flags types
    placeholder: str = ""


@dataclass
class GamescopeFlagDef:
    """
    Definition of a real `gamescope` command-line argument (per `gamescope --help`).
    NOT an environment variable — gamescope reads these from argv only, e.g.:
        gamescope -W 2560 -H 1440 --hdr-enabled --adaptive-sync --
    (nothing is assumed after `--`; the launcher/shell appends its own command)
    kind:
      "toggle" — bare flag, included with no value when enabled (e.g. --rt)
      "int"    — flag takes a numeric argument (e.g. -r 165)
      "enum"   — flag takes one of `options` as its argument (e.g. -F fsr)
      "string" — flag takes a free-form string/path argument
    short: the short form (e.g. "-W"), if any — used in the preview when set.
    """
    name: str          # human label, e.g. "Output Width"
    flag: str          # long flag as it appears in --help, e.g. "--output-width"
    cat: str           # grouping shown in the UI, e.g. "Geometry", "HDR", "Debug"
    kind: str          # "toggle" | "int" | "enum" | "string"
    desc: str
    short: str = ""
    options: List[str] = field(default_factory=list)
    placeholder: str = ""


APP_TITLE = "DXVK NVAPI DRS Settings Configurator"

# Uygulama ikonu: script'in yanındaki assets/drstool.png dosyasından yüklenir.
# Wayland altında sistem barı/taskbar ikonu genellikle .desktop dosyasındaki
# Icon= alanına göre eşleşir; paket kurulumunda .desktop girdisini ebuild
# sağlar (bkz. main()).
APP_ID = "drstool"
_SCRIPT_DIR = Path(__file__).resolve().parent
ICON_PATH = _SCRIPT_DIR / "assets" / "drstool.png"


def _as_dict(v) -> Dict:
    """Hand-edited profiles.json may hold null / a list where a mapping is expected."""
    return dict(v) if isinstance(v, dict) else {}


def find_bundled_dir(name: str, marker: str) -> Path:
    """Locate a bundled helper source tree (vk-flip-meter-main, lutris-game-tune-main).

    Checked in order: $DRSTOOL_DATA_DIR, next to this script (git checkout),
    /usr/share/drstool and /usr/local/share/drstool (ebuild / package install,
    where the script itself lives under /usr/lib/python-exec/ and has no
    sibling directories)."""
    roots: List[Path] = []
    env = os.environ.get("DRSTOOL_DATA_DIR")
    if env:
        roots.append(Path(env))
    roots += [_SCRIPT_DIR, Path("/usr/share/drstool"), Path("/usr/local/share/drstool")]
    for root in roots:
        if (root / name / marker).is_file():
            return root / name
    return _SCRIPT_DIR / name


def user_cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "drstool"


def default_flm_conf_path() -> Path:
    """Per-user, non-world-writable default for the FLM live-tuning file."""
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt and os.path.isdir(rt) and os.access(rt, os.W_OK):
        return Path(rt) / "flm.conf"
    return Path(tempfile.gettempdir()) / f"flm-{os.getuid()}.conf"


def detect_libdir() -> str:
    """Distro-appropriate library directory (relative to the install prefix)."""
    lib64 = Path("/usr/lib64")
    if lib64.is_dir() and not lib64.is_symlink():
        return "lib64"                                   # Gentoo, Fedora, openSUSE
    multiarch = f"lib/{platform.machine()}-linux-gnu"
    if Path("/usr", multiarch).is_dir():
        return multiarch                                 # Debian / Ubuntu
    return "lib"                                         # Arch (lib64 -> lib symlink), others


def pkexec_auth_failed(exit_code: int, output: str) -> bool:
    """126/127 are ambiguous: pkexec uses them for a dismissed/denied auth
    dialog, but a missing program inside `bash -c` also exits 127. Only
    treat them as "authorization failed" when pkexec said so."""
    if exit_code not in (126, 127):
        return False
    low = output.lower()
    return any(m in low for m in ("dismissed", "not authorized", "authentication"))

try:
    import setproctitle
    _HAVE_SETPROCTITLE = True
except ImportError:
    _HAVE_SETPROCTITLE = False


def apply_wayland_app_id(app_id: str) -> None:
    """python-exec2c gibi bir wrapper üzerinden (emerge/ebuild kurulumu) veya
    doğrudan 'python3 DRSTool.py' ile çalıştırıldığında, gerçek process adı
    'python3'/'python3.14' olarak kalır. KDE Plasma Wayland (KWin) altında
    Qt'nin QApplication.setDesktopFileName() çağrısı bu durumda native
    Wayland app_id protokol mesajını etkilemiyor; KWin app_id'yi gerçek
    process adından türetiyor ve .desktop/Icon eşleşmesini kaybedip jenerik
    Wayland ikonuna düşüyor.
    setproctitle mevcutsa (dev-python/setproctitle veya `pip install
    setproctitle`), argv[0] belleğini gerçekten patch'leyerek process adını
    app_id ile aynı yapar — hem ebuild kurulumunda hem de repo'yu klonlayıp
    doğrudan çalıştıranlarda (kurulum betiği/elle) aynı şekilde çalışır.
    setproctitle yoksa sessizce atlanır; sadece ikon eşleşmesi etkilenir,
    program normal çalışmaya devam eder."""
    if not _HAVE_SETPROCTITLE:
        return
    try:
        setproctitle.setproctitle(app_id)
    except Exception:
        pass


# Shared style for the scrollable control area added to SettingEditorWidget
# and EnvVarEditorWidget (see _build_scrollable_control_area below). Matches
# the dark scrollbar look already used by the sidebar list widgets.
SCROLLABLE_CONTROL_QSS = """
QScrollArea{ border:none; background:transparent; }
QScrollArea > QWidget > QWidget{ background:transparent; }
QScrollBar:vertical{
    background:#0d0f12;
    width:5px;
}
QScrollBar::handle:vertical{
    background:#1e2535;
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
    "bg":        "#0d0f12",
    "surface":   "#141720",
    "raised":    "#1a1f2e",
    "border":    "#1e2535",
    "border_hi": "#2b3444",
    "text":      "#e8eaf0",
    "text2":     "#b9bfcc",
    "muted":     "#7d8597",
    "accent":    "#76b900",
    "accent_hi": "#8fd400",
    "accent_dim":"#4a7300",
    "warn":      "#d2a03c",
    "danger":    "#d05050",
    "mono":      "monospace",
}

APP_QSS = f"""
QToolTip {{
    background:{T['raised']}; color:{T['text']};
    border:1px solid {T['border_hi']}; padding:6px 8px; border-radius:4px;
}}
QScrollBar:vertical {{ background:transparent; width:8px; margin:0; }}
QScrollBar::handle:vertical {{ background:{T['border']}; border-radius:4px; min-height:28px; }}
QScrollBar::handle:vertical:hover {{ background:#3a4458; }}
QScrollBar:horizontal {{ background:transparent; height:8px; margin:0; }}
QScrollBar::handle:horizontal {{ background:{T['border']}; border-radius:4px; min-width:28px; }}
QScrollBar::handle:horizontal:hover {{ background:#3a4458; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; border:none; background:none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background:none; }}
QMenu {{
    background:{T['surface']}; color:{T['text']};
    border:1px solid {T['border_hi']}; padding:4px;
}}
QMenu::item {{ padding:6px 18px; border-radius:3px; }}
QMenu::item:selected {{ background:rgba(118,185,0,0.16); color:{T['text']}; }}
QMenu::separator {{ height:1px; background:{T['border']}; margin:4px 6px; }}
QMessageBox, QDialog {{ background:{T['surface']}; }}
QMessageBox QLabel {{ color:{T['text']}; }}
QMessageBox QPushButton, QDialogButtonBox QPushButton {{
    background:{T['raised']}; border:1px solid {T['border_hi']}; border-radius:4px;
    color:{T['text']}; padding:5px 14px; min-width:72px;
}}
QMessageBox QPushButton:hover, QDialogButtonBox QPushButton:hover {{ border-color:{T['accent']}; }}
QMessageBox QPushButton:default, QDialogButtonBox QPushButton:default {{ border-color:{T['accent_dim']}; }}
QComboBox QAbstractItemView {{
    background:{T['surface']}; color:{T['text']}; border:1px solid {T['border_hi']};
    selection-background-color:rgba(118,185,0,0.18); selection-color:{T['text']};
}}
"""


def apply_theme(app: "QApplication") -> None:
    """Palette + app-wide QSS. Called once from main()."""
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(T["bg"]))
    pal.setColor(QPalette.WindowText, QColor(T["text2"]))
    pal.setColor(QPalette.Base, QColor(T["surface"]))
    pal.setColor(QPalette.AlternateBase, QColor(T["raised"]))
    pal.setColor(QPalette.Text, QColor(T["text"]))
    pal.setColor(QPalette.Button, QColor(T["surface"]))
    pal.setColor(QPalette.ButtonText, QColor(T["text2"]))
    pal.setColor(QPalette.Highlight, QColor(T["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor(0, 0, 0))
    pal.setColor(QPalette.ToolTipBase, QColor(T["raised"]))
    pal.setColor(QPalette.ToolTipText, QColor(T["text"]))
    pal.setColor(QPalette.PlaceholderText, QColor(T["muted"]))
    pal.setColor(QPalette.Link, QColor(T["accent"]))
    app.setPalette(pal)
    app.setStyleSheet(APP_QSS)


# ============================================================================
# Sidebar list delegate
# ----------------------------------------------------------------------------
# Draws every sidebar list (DRS settings, env vars, GPU archs, profiles) the
# same way: a quiet category header with a "N set" count, and item rows with
# a green dot + the current value previewed on the right. Replaces the old
# red "─── Cat ───" text headers and per-item foreground colors.
# ============================================================================

ROLE_KIND  = Qt.UserRole + 10   # "header" | "item" | "special"
ROLE_VALUE = Qt.UserRole + 11   # short value preview shown on the right
ROLE_SET   = Qt.UserRole + 12   # bool: item is part of the output
ROLE_COUNT = Qt.UserRole + 13   # header: how many of its items are set
ROLE_FAV = Qt.UserRole + 14     # bool: item is a favorite
ROLE_UNAVAIL = Qt.UserRole + 15  # str: why the system can't run it ("" = fine)


class SidebarDelegate(QStyledItemDelegate):
    HEADER_H = 30
    ITEM_H = 26

    def sizeHint(self, option, index):
        kind = index.data(ROLE_KIND)
        return QSize(0, self.HEADER_H if kind == "header" else self.ITEM_H)

    def paint(self, p: QPainter, option, index):
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect
        kind = index.data(ROLE_KIND) or "item"
        name = index.data(Qt.DisplayRole) or ""
        base_font = QFont(option.font)

        if kind == "header":
            f = QFont(base_font)
            f.setBold(True)
            f.setPointSizeF(max(7.5, base_font.pointSizeF() - 0.5))
            p.setFont(f)
            fm = QFontMetrics(f)
            text_rect = QRect(r.left() + 12, r.top() + 8, r.width() - 24, r.height() - 8)
            p.setPen(QColor(T["muted"]))
            p.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, name)
            x0 = text_rect.left() + fm.horizontalAdvance(name) + 10
            x1 = r.right() - 12
            cnt = index.data(ROLE_COUNT) or 0
            if cnt:
                badge = f"{cnt} set"
                bw = fm.horizontalAdvance(badge) + 12
                br = QRect(x1 - bw, text_rect.center().y() - 8, bw, 16)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(118, 185, 0, 36))
                p.drawRoundedRect(br, 8, 8)
                p.setPen(QColor(T["accent"]))
                p.drawText(br, Qt.AlignCenter, badge)
                x1 = br.left() - 8
            if x1 > x0:
                p.setPen(QPen(QColor(T["border"]), 1))
                y = text_rect.center().y() + 1
                p.drawLine(x0, y, x1, y)
            p.restore()
            return

        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        is_set = bool(index.data(ROLE_SET))

        if selected:
            p.fillRect(r, QColor(118, 185, 0, 26))
            p.fillRect(QRect(r.left(), r.top(), 2, r.height()), QColor(T["accent"]))
        elif hovered:
            p.fillRect(r, QColor(T["surface"]))

        if is_set:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T["accent"]))
            p.drawEllipse(QRect(r.left() + 11, r.center().y() - 3, 6, 6))

        value = index.data(ROLE_VALUE) or ""
        pad_l = r.left() + 24
        avail = r.right() - 10 - pad_l
        val_w = 0
        if value:
            vf = QFont(T["mono"])
            vf.setStyleHint(QFont.Monospace)
            vf.setPointSizeF(max(7.5, base_font.pointSizeF() - 0.5))
            vfm = QFontMetrics(vf)
            max_val = int(avail * 0.45)
            shown = vfm.elidedText(value, Qt.ElideRight, max_val)
            val_w = vfm.horizontalAdvance(shown)
            p.setFont(vf)
            p.setPen(QColor(T["accent"]) if is_set else QColor(T["muted"]))
            p.drawText(QRect(r.right() - 10 - val_w, r.top(), val_w, r.height()),
                       Qt.AlignRight | Qt.AlignVCenter, shown)

        fav = bool(index.data(ROLE_FAV))
        unavail = bool(index.data(ROLE_UNAVAIL))
        nf = QFont(base_font)
        if is_set:
            nf.setWeight(QFont.DemiBold)
        if unavail:
            nf.setItalic(True)
        p.setFont(nf)
        nfm = QFontMetrics(nf)
        name_w = avail - (val_w + 12 if val_w else 0)
        if kind == "special":
            color = QColor(T["warn"])
        elif unavail and not is_set:
            color = QColor(T["muted"])
            color.setAlpha(150)
        elif is_set or selected:
            color = QColor(T["text"])
        else:
            color = QColor(T["text2"])
        text_x = pad_l
        if fav:
            p.setPen(QColor("#e0b341"))
            p.drawText(QRect(pad_l, r.top(), 14, r.height()), Qt.AlignLeft | Qt.AlignVCenter, "\u2605")
            text_x += 15
            name_w -= 15
        p.setPen(color)
        p.drawText(QRect(text_x, r.top(), max(0, name_w), r.height()),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   nfm.elidedText(name, Qt.ElideRight, max(0, name_w)))
        p.restore()


SIDEBAR_LIST_QSS = f"""
QListWidget {{ background:{T['bg']}; border:none; outline:none; }}
QListWidget::item {{ border:none; }}
"""


def setup_sidebar_list(lw: QListWidget) -> None:
    """Shared look/behaviour for every delegate-drawn sidebar list."""
    lw.setItemDelegate(SidebarDelegate(lw))
    lw.setAlternatingRowColors(False)
    lw.setMouseTracking(True)
    lw.viewport().setAttribute(Qt.WA_Hover, True)
    lw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    lw.setVerticalScrollMode(QListWidget.ScrollPerPixel)
    lw.setStyleSheet(SIDEBAR_LIST_QSS)


def make_header_item(text: str, count: int = 0) -> QListWidgetItem:
    item = QListWidgetItem(text)
    item.setFlags(Qt.NoItemFlags)
    item.setData(ROLE_KIND, "header")
    item.setData(ROLE_COUNT, count)
    return item


def make_list_item(text: str, key, is_set: bool = False, value: str = "",
                   kind: str = "item", fav: bool = False, unavail: str = "") -> QListWidgetItem:
    item = QListWidgetItem(text)
    item.setData(Qt.UserRole, key)
    item.setData(ROLE_KIND, kind)
    item.setData(ROLE_SET, is_set)
    item.setData(ROLE_VALUE, value)
    item.setData(ROLE_FAV, fav)
    item.setData(ROLE_UNAVAIL, unavail)
    if unavail:
        item.setToolTip("Not supported here: " + unavail)
    return item


def _fmt_drs_value(setting: "Setting", raw: str) -> str:
    """Human label for a stored DRS value ('Latest', '4x', '0x3C' ...)."""
    if raw is None:
        return ""
    try:
        iv = int(raw, 16) if str(raw).lower().startswith("0x") else int(raw)
    except (TypeError, ValueError):
        return str(raw)
    for v in setting.values:
        try:
            if int(v.val, 16) == iv:
                return v.name
        except ValueError:
            pass
    for pr in setting.presets:
        try:
            if int(pr.val, 16) == iv:
                return pr.name
        except ValueError:
            pass
    if setting.type == "numeric":
        return str(iv)
    return str(raw)


# ============================================================================
# Small reusable widgets
# ============================================================================

class ElidedLabel(QLabel):
    """
    QLabel that never widens the window: it elides its text to the space it
    is given and keeps the full text for tooltip / copy. Clicking it copies
    the full text (emits `copied`). Stylesheet boxes (border/padding) still
    render because painting is left to QLabel — only the shown text changes.
    """
    copied = Signal(str)

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self._copyable = True
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text: str):          # noqa: N802
        self._full = text or ""
        self.setToolTip(self._full + ("\n\nClick to copy" if self._copyable and self._full else ""))
        self._refresh()

    def text(self) -> str:
        return self._full

    def set_copyable(self, on: bool):
        self._copyable = on
        self.setCursor(Qt.PointingHandCursor if on else Qt.ArrowCursor)

    def _refresh(self):
        avail = max(20, self.width() - 22)
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, avail)
        QLabel.setText(self, shown)

    def resizeEvent(self, e):             # noqa: N802
        super().resizeEvent(e)
        self._refresh()

    def minimumSizeHint(self):            # noqa: N802
        return QSize(40, super().minimumSizeHint().height())

    def sizeHint(self):                   # noqa: N802
        return QSize(120, super().sizeHint().height())

    def mousePressEvent(self, e):         # noqa: N802
        if self._copyable and e.button() == Qt.LeftButton and self._full:
            QApplication.clipboard().setText(self._full)
            self.copied.emit(self._full)
        super().mousePressEvent(e)


class NavTab(QPushButton):
    """Top navigation tab: title + a small live badge (counts / arch code)."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(7)
        self._title = QLabel(title)
        self._badge = QLabel()
        for w in (self._title, self._badge):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._badge.setFixedHeight(17)
        lay.addWidget(self._title, 0, Qt.AlignVCenter)
        lay.addWidget(self._badge, 0, Qt.AlignVCenter)
        self._badge.hide()
        self.setFixedHeight(38)
        self.setStyleSheet(f"""
QPushButton {{ background:transparent; border:none; border-bottom:2px solid transparent; }}
QPushButton:hover {{ background:{T['surface']}; }}
QPushButton:checked {{ border-bottom:2px solid {T['accent']}; }}
""")
        self.toggled.connect(self._restyle)
        self._restyle(False)

    def set_badge(self, text: str):
        self._badge.setText(text)
        self._badge.setVisible(bool(text))
        self.updateGeometry()

    def sizeHint(self):                   # noqa: N802
        return self.layout().sizeHint().expandedTo(QSize(0, 38))

    def minimumSizeHint(self):            # noqa: N802
        return self.sizeHint()

    def _restyle(self, checked: bool):
        self._title.setStyleSheet(
            f"color:{T['text'] if checked else T['muted']}; font-size:12px; font-weight:600;"
            " background:transparent;")
        self._badge.setStyleSheet(
            f"color:{T['accent'] if checked else T['text2']};"
            f" background:{'rgba(118,185,0,0.14)' if checked else T['raised']};"
            " border-radius:8px; padding:0 7px; font-size:10px; font-weight:700;"
            f" font-family:{T['mono']};")


# ============================================================================
# GPU Architectures
# ============================================================================

GPU_ARCHS = [
    # Codes = exactly the strings dxvk-nvapi's DXVK_NVAPI_GPU_ARCH parser
    # accepts (src/util/util_env.cpp, CHECK_ARCH list). Anything else is
    # logged as "unrecognized" and ignored.
    GPUArch("GeForce 600/700", "Kepler", "GK", "GK100", "GTX 780 Ti"),
    GPUArch("GeForce 750", "Maxwell 1st gen", "GM", "GM000", "GTX 750 Ti"),
    GPUArch("GeForce 900", "Maxwell", "GM", "GM200", "GTX 980 Ti"),
    GPUArch("GeForce 10", "Pascal", "GP", "GP100", "GTX 1080"),
    GPUArch("Titan V", "Volta", "GV", "GV100", "TITAN V"),
    GPUArch("GeForce 16/20", "Turing", "TU", "TU100", "RTX 2080 Ti"),
    GPUArch("RTX 30", "Ampere", "GA", "GA100", "RTX 3090"),
    GPUArch("RTX 40", "Ada", "AD", "AD100", "RTX 4090"),
    GPUArch("RTX 50", "Blackwell", "GB", "GB200", "RTX 5090"),
]


# ============================================================================
# Complete Settings (Updated from NvApiDriverSettings.h)
# ============================================================================

def create_all_settings() -> List[Setting]:
    """Create all settings from NvApiDriverSettings.h (latest)"""
    settings = []

    # ===== OpenGL Settings =====
    settings.extend([
        Setting("0x2089BF6C", "OGL AA Line Gamma", "OpenGL", "enum", "0x10",
                "Antialiasing - Line gamma",
                "Controls gamma correction for antialiased lines in OpenGL.",
                values=[
                    SettingValue("Disabled", "0x10"),
                    SettingValue("Enabled", "0x23"),
                ]),
        Setting("0x2072C5A3", "OGL GDI Compatibility", "OpenGL", "enum", "0x2",
                "OpenGL GDI compatibility",
                "Controls OpenGL interaction with Windows GDI.",
                values=[
                    SettingValue("Prefer Disabled", "0x0"),
                    SettingValue("Prefer Enabled", "0x1"),
                    SettingValue("Auto", "0x2"),
                ]),
        Setting("0x20D690F8", "OGL Present Method", "OpenGL", "enum", "0x2",
                "Vulkan/OpenGL present method",
                "Controls presentation method for Vulkan/OpenGL.",
                values=[
                    SettingValue("Prefer Disabled", "0x0"),
                    SettingValue("Prefer Enabled", "0x1"),
                    SettingValue("Auto", "0x2"),
                ]),
        Setting("0x2097C2F6", "OGL Deep Color", "OpenGL", "enum", "0x1",
                "Deep color for 3D applications",
                "Enables deep color (10/12-bit) support.",
                values=[
                    SettingValue("Disable", "0x0"),
                    SettingValue("Enable", "0x1"),
                ]),
        Setting("0x206A6582", "OGL Swap Interval", "OpenGL", "enum", "0x1",
                "OpenGL default swap interval",
                "Controls VSync behavior for OpenGL.",
                values=[
                    SettingValue("Tear", "0x0"),
                    SettingValue("VSync 1x", "0x1"),
                    SettingValue("Force Off", "0xf0000000"),
                    SettingValue("Force On", "0x10000000"),
                    SettingValue("App Controlled", "0x0"),
                    SettingValue("Disable", "0xffffffff"),
                ]),
        Setting("0x206C4581", "OGL Swap Interval Frac", "OpenGL", "numeric", "0",
                "OpenGL swap interval fraction",
                "Fractional swap interval for VSync tuning.",
                min=0, max=100),
        Setting("0x20655CFA", "OGL Swap Interval Sign", "OpenGL", "enum", "0x0",
                "OpenGL swap interval sign",
                "Controls swap interval sign.",
                values=[
                    SettingValue("Positive", "0x0"),
                    SettingValue("Negative", "0x1"),
                ]),
        Setting("0x201F619F", "OGL Force Blit", "OpenGL", "enum", "0x0",
                "Buffer-flipping mode",
                "Controls frame presentation mode.",
                values=[
                    SettingValue("Off (flip)", "0x0"),
                    SettingValue("On (blit)", "0x1"),
                ]),
        Setting("0x204D9A0C", "OGL Force Stereo", "OpenGL", "enum", "0x0",
                "Force Stereo shuttering",
                "Forces stereo shutter glasses support.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x208E55E3", "OGL Max Frames", "OpenGL", "numeric", "0",
                "Maximum frames allowed",
                "Limits maximum frames rendered ahead.",
                min=0, max=255),
        Setting("0x20C1221E", "OGL Threaded Opt", "OpenGL", "enum", "0x0",
                "Threaded optimization",
                "Enables multi-threaded OpenGL optimization.",
                values=[
                    SettingValue("Driver Default", "0x0"),
                    SettingValue("Enable", "0x1"),
                    SettingValue("Disable", "0x2"),
                ]),
        Setting("0x20FDD1F9", "OGL Triple Buffer", "OpenGL", "enum", "0x0",
                "Triple buffering",
                "Enables triple buffering.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Enabled", "0x1"),
                ]),
        # ---- New OpenGL settings from header ----
        Setting("0x209DF23E", "OGL Event Log Severity", "OpenGL", "enum", "0x4",
                "Event Log Severity Threshold",
                "Sets the minimum severity level for OpenGL event log messages.",
                values=[
                    SettingValue("Disable", "0x0"),
                    SettingValue("Critical", "0x1"),
                    SettingValue("Warning", "0x2"),
                    SettingValue("Information", "0x3"),
                    SettingValue("All", "0x4"),
                ]),
        Setting("0x209AE66F", "OGL Overlay Pixel Type", "OpenGL", "enum", "0x1",
                "Exported Overlay pixel types",
                "Specifies which pixel types are supported for overlay.",
                values=[
                    SettingValue("None", "0x0"),
                    SettingValue("CI", "0x1"),
                    SettingValue("RGBA", "0x2"),
                    SettingValue("CI and RGBA", "0x3"),
                ]),
        Setting("0x206C28C4", "OGL Overlay Support", "OpenGL", "enum", "0x0",
                "Enable overlay",
                "Controls overlay support in OpenGL.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                    SettingValue("Force SW", "0x2"),
                ]),
        Setting("0x20797D6C", "OGL Quality Enhancements", "OpenGL", "enum", "0x0",
                "OpenGL quality enhancements",
                "High level control of rendering quality in OpenGL.",
                values=[
                    SettingValue("High Quality", "0xfffffff6"),
                    SettingValue("Quality", "0x0"),
                    SettingValue("Performance", "0xa"),
                    SettingValue("High Performance", "0x14"),
                ]),
        Setting("0x20A29055", "OGL Single Backdepth", "OpenGL", "enum", "0x0",
                "Unified back/depth buffer",
                "Controls unified back/depth buffer usage.",
                values=[
                    SettingValue("Disable", "0x0"),
                    SettingValue("Enable", "0x1"),
                    SettingValue("Use HW Default", "0xffffffff"),
                ]),
        Setting("0x2092D3BE", "OGL SLI Multicast", "OpenGL", "enum", "0x0",
                "Enable NV_gpu_multicast",
                "Enables the NV_gpu_multicast extension for OpenGL.",
                values=[
                    SettingValue("Disable", "0x0"),
                    SettingValue("Enable", "0x1"),
                    SettingValue("Force Disable", "0x2"),
                    SettingValue("Allow Mosaic", "0x4"),
                ]),
        Setting("0x202888C1", "OGL TMON Level", "OpenGL", "enum", "0x4",
                "Event Log Tmon Severity",
                "Sets the severity level for TMON events in OpenGL.",
                values=[
                    SettingValue("Disable", "0x0"),
                    SettingValue("Critical", "0x1"),
                    SettingValue("Warning", "0x2"),
                    SettingValue("Information", "0x3"),
                    SettingValue("Most", "0x4"),
                    SettingValue("Verbose", "0x5"),
                ]),
    ])

    # ===== Anti-Aliasing Settings =====
    settings.extend([
        Setting("0x10ECDB82", "AA Behavior Flags", "Anti-Aliasing", "bitfield", "0x0",
                "AA behavior flags",
                "Controls how AA modes interact with applications.",
                bits=[
                    # Bits 0-1 (mask 0x3): mutually-exclusive Override sub-field
                    BitField("Override→App Ctrl", 0x00000001, mask=0x00000003),
                    BitField("Override→Enhance", 0x00000002, mask=0x00000003),
                    BitField("Disable Override", 0x00000003, mask=0x00000003),
                    # Bits 2-3 (mask 0xc): mutually-exclusive Enhance sub-field
                    BitField("Enhance→App Ctrl", 0x00000004, mask=0x0000000c),
                    BitField("Enhance→Override", 0x00000008, mask=0x0000000c),
                    BitField("Disable Enhance", 0x0000000c, mask=0x0000000c),
                    # Independent single-bit flags outside those sub-fields
                    BitField("VCAA→Multisample", 0x00010000),
                    BitField("SLI Disable TSS", 0x00020000),
                    BitField("Disable CPLAA", 0x00040000),
                    BitField("Skip RT Dim", 0x00080000),
                    BitField("Disable SLIAA", 0x00100000),
                ]),
        Setting("0x10FC2D9C", "AA Transparency MS", "Anti-Aliasing", "enum", "0x0",
                "AA - Transparency Multisampling",
                "Controls alpha-to-coverage transparency AA.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x4"),
                ]),
        Setting("0x107D639D", "AA Gamma Correction", "Anti-Aliasing", "enum", "0x0",
                "AA - Gamma correction",
                "Controls gamma correction for AA edges.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On if FOS", "0x1"),
                    SettingValue("On always", "0x2"),
                ]),
        # AA Method - updated with all values from header
        Setting("0x10D773D2", "AA Method", "Anti-Aliasing", "enum", "0x0",
                "AA - Setting",
                "Selects anti-aliasing sample method.",
                values=[
                    SettingValue("None", "0x0"),
                    SettingValue("Supersample 2x H", "0x1"),
                    SettingValue("Supersample 2x V", "0x2"),
                    SettingValue("Supersample 1.5x1.5", "0x2"),
                    SettingValue("Free 0x03", "0x3"),
                    SettingValue("Free 0x04", "0x4"),
                    SettingValue("Supersample 4x", "0x5"),
                    SettingValue("Supersample 4x Bias", "0x6"),
                    SettingValue("Supersample 4x Gaussian", "0x7"),
                    SettingValue("Free 0x08", "0x8"),
                    SettingValue("Free 0x09", "0x9"),
                    SettingValue("Supersample 9x", "0xa"),
                    SettingValue("Supersample 9x Bias", "0xb"),
                    SettingValue("Supersample 16x", "0xc"),
                    SettingValue("Supersample 16x Bias", "0xd"),
                    SettingValue("Multisample 2x Diagonal", "0xe"),
                    SettingValue("Multisample 2x Quincunx", "0xf"),
                    SettingValue("Multisample 4x", "0x10"),
                    SettingValue("Free 0x11", "0x11"),
                    SettingValue("Multisample 4x Gaussian", "0x12"),
                    SettingValue("Mixedsample 4x Skewed", "0x13"),
                    SettingValue("Free 0x14", "0x14"),
                    SettingValue("Free 0x15", "0x15"),
                    SettingValue("Mixedsample 6x", "0x16"),
                    SettingValue("Mixedsample 6x Skewed", "0x17"),
                    SettingValue("Mixedsample 8x", "0x18"),
                    SettingValue("Mixedsample 8x Skewed", "0x19"),
                    SettingValue("Mixedsample 16x", "0x1a"),
                    SettingValue("Multisample 4x Gamma", "0x1b"),
                    SettingValue("Multisample 16x", "0x1c"),
                    SettingValue("VCAA 32x (8v24)", "0x1d"),
                    SettingValue("Corruption Check", "0x1e"),
                    SettingValue("6x CT", "0x1f"),
                    SettingValue("Multisample 2x Diag Gamma", "0x20"),
                    SettingValue("Supersample 4x Gamma", "0x21"),
                    SettingValue("Multisample 4x FOSGamma", "0x22"),
                    SettingValue("Multisample 2x Diag FOSGamma", "0x23"),
                    SettingValue("Supersample 4x FOSGamma", "0x24"),
                    SettingValue("Multisample 8x", "0x25"),
                    SettingValue("VCAA 8x (4v4)", "0x26"),
                    SettingValue("VCAA 16x (4v12)", "0x27"),
                    SettingValue("VCAA 16x (8v8)", "0x28"),
                    SettingValue("Mixedsample 32x", "0x29"),
                    SettingValue("SuperVCAA 64x (4v12)", "0x2a"),
                    SettingValue("SuperVCAA 64x (8v8)", "0x2b"),
                    SettingValue("Mixedsample 64x", "0x2c"),
                    SettingValue("Mixedsample 128x", "0x2d"),
                ]),
        Setting("0x10D48A85", "AA Transp. SS", "Anti-Aliasing", "bitfield", "0x0",
                "AA - Transparency Supersampling",
                "Controls supersampling for transparent textures. Combines a "
                "replay MODE sub-field (bits 0-3, mask 0xf) with a SAMPLES "
                "sub-field (bits 4-6, mask 0x70) and an independent "
                "Disallow TRAA flag (bit 8, 0x100).",
                bits=[
                    # MODE sub-field (mask 0xf) - mutually exclusive
                    BitField("Mode: Off", 0x00000000, mask=0x0000000f),
                    BitField("Mode: Alpha Test", 0x00000001, mask=0x0000000f),
                    BitField("Mode: Pixel Kill", 0x00000002, mask=0x0000000f),
                    BitField("Mode: Dynamic Branch", 0x00000004, mask=0x0000000f),
                    BitField("Mode: All", 0x00000008, mask=0x0000000f),
                    # SAMPLES sub-field (mask 0x70) - mutually exclusive
                    BitField("Samples: One", 0x00000000, mask=0x00000070),
                    BitField("Samples: Two", 0x00000010, mask=0x00000070),
                    BitField("Samples: Four", 0x00000020, mask=0x00000070),
                    BitField("Samples: Eight", 0x00000030, mask=0x00000070),
                    # Independent single-bit flag outside those sub-fields
                    BitField("Disallow TRAA", 0x00000100),
                ]),
        Setting("0x107EFC5B", "AA Mode Selector", "Anti-Aliasing", "enum", "0x0",
                "AA - Mode",
                "Global anti-aliasing mode selection.",
                values=[
                    SettingValue("App Controlled", "0x0"),
                    SettingValue("Override", "0x1"),
                    SettingValue("Enhance", "0x2"),
                ]),
        Setting("0x107AFC5B", "AA Mode SLIAA", "Anti-Aliasing", "enum", "0x0",
                "AA - SLI AA",
                "Enables SLI-optimized anti-aliasing.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Enabled", "0x1"),
                ]),
    ])

    # ===== Texture Filtering =====
    settings.extend([
        Setting("0x101E61A9", "Aniso Level", "Texture Filtering", "enum", "0x1",
                "Anisotropic filtering level",
                "Controls anisotropic filtering (AF) level.",
                values=[
                    SettingValue("None/Point", "0x0"),
                    SettingValue("Linear", "0x1"),
                    SettingValue("2x", "0x2"),
                    SettingValue("4x", "0x4"),
                    SettingValue("8x", "0x8"),
                    SettingValue("16x", "0x10"),
                ]),
        Setting("0x10D2BB16", "Aniso Mode", "Texture Filtering", "enum", "0x0",
                "Anisotropic filtering mode",
                "Controls how AF is applied.",
                values=[
                    SettingValue("App Controlled", "0x0"),
                    SettingValue("User Override", "0x1"),
                    SettingValue("Conditional", "0x2"),
                ]),
        Setting("0x00CE2691", "Tex Filter Quality", "Texture Filtering", "enum", "0x0",
                "Texture filtering quality",
                "Overall texture filtering quality.",
                values=[
                    SettingValue("High Quality", "0xfffffff6"),
                    SettingValue("Quality", "0x0"),
                    SettingValue("Performance", "0xa"),
                    SettingValue("High Performance", "0x14"),
                ]),
        Setting("0x00E73211", "Aniso Optimize", "Texture Filtering", "enum", "0x0",
                "Anisotropic optimization",
                "Optimizes AF samples for performance.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x0084CD70", "Bilinear in Aniso", "Texture Filtering", "enum", "0x0",
                "Bilinear in Aniso",
                "Controls bilinear filtering within AF.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x002ECAF2", "Trilinear Slope Opt", "Texture Filtering", "enum", "0x0",
                "Trilinear optimization",
                "Optimizes trilinear filtering.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x0019BB68", "No Neg LOD Bias", "Texture Filtering", "enum", "0x0",
                "Negative LOD bias",
                "Controls negative LOD bias clamping.",
                values=[
                    SettingValue("Allow negative", "0x0"),
                    SettingValue("Clamp", "0x1"),
                ]),
        Setting("0x00638E8F", "Driver LOD Adjust", "Texture Filtering", "enum", "0x1",
                "Driver LOD Bias",
                "Allows driver to adjust LOD bias.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x00738E8F", "LOD Bias Adjust", "Texture Filtering", "hex", "0x0",
                "LOD Bias Adjust",
                "Manual LOD bias adjustment."),
        Setting("0x00CE2692", "Quality Substitution", "Texture Filtering", "enum", "0x0",
                "Quality Substitution",
                "Controls quality setting substitutions.",
                values=[
                    SettingValue("No Substitution", "0x0"),
                    SettingValue("HQ→Quality", "0x1"),
                ]),
    ])

    # ===== DLSS / NGX Settings =====
    settings.extend([
        Setting("0x10E41E01", "DLSS-SR Override", "DLSS / NGX", "enum", "0x0",
                "DLSS-SR override",
                "Enables override for DLSS Super Resolution.",
                values=[SettingValue("Off", "0x0"), SettingValue("On", "0x1")]),
        Setting("0x10E41E02", "DLSS-RR Override", "DLSS / NGX", "enum", "0x0",
                "DLSS-RR override",
                "Enables override for DLSS Ray Reconstruction.",
                values=[SettingValue("Off", "0x0"), SettingValue("On", "0x1")]),
        Setting("0x10E41E03", "DLSS-FG Override", "DLSS / NGX", "enum", "0x0",
                "DLSS-FG override",
                "Enables override for DLSS Frame Generation.",
                values=[SettingValue("Off", "0x0"), SettingValue("On", "0x1")]),
        Setting("0x10E41DF1", "DLSS-FG Preset", "DLSS / NGX", "enum", "0x0",
                "DLSS-FG Preset",
                "Forces specific DLSS Frame Generation preset.",
                values=[
                    SettingValue("Off", "0x0"), SettingValue("A", "0x1"),
                    SettingValue("B", "0x2"), SettingValue("C", "0x3"),
                    SettingValue("D", "0x4"), SettingValue("E", "0x5"),
                    SettingValue("F", "0x6"), SettingValue("G", "0x7"),
                    SettingValue("H", "0x8"), SettingValue("I", "0x9"),
                    SettingValue("J", "0xa"), SettingValue("K", "0xb"),
                    SettingValue("L", "0xc"), SettingValue("M", "0xd"),
                    SettingValue("N", "0xe"), SettingValue("O", "0xf"),
                    SettingValue("P", "0x10"), SettingValue("Q", "0x11"),
                    SettingValue("R", "0x12"), SettingValue("S", "0x13"),
                    SettingValue("T", "0x14"), SettingValue("U", "0x15"),
                    SettingValue("V", "0x16"), SettingValue("W", "0x17"),
                    SettingValue("X", "0x18"), SettingValue("Y", "0x19"),
                    SettingValue("Z", "0x1a"),
                    SettingValue("Default", "0xfffffe"),
                    SettingValue("Latest", "0xffffff"),
                ]),
        Setting("0x10E41DF3", "DLSS-SR Preset", "DLSS / NGX", "enum", "0x0",
                "DLSS-SR Preset",
                "Forces specific DLSS Super Resolution preset.",
                values=[
                    SettingValue("Off", "0x0"), SettingValue("A", "0x1"),
                    SettingValue("B", "0x2"), SettingValue("C", "0x3"),
                    SettingValue("D", "0x4"), SettingValue("E", "0x5"),
                    SettingValue("F", "0x6"), SettingValue("G", "0x7"),
                    SettingValue("H", "0x8"), SettingValue("I", "0x9"),
                    SettingValue("J", "0xa"), SettingValue("K", "0xb"),
                    SettingValue("L", "0xc"), SettingValue("M", "0xd"),
                    SettingValue("N", "0xe"), SettingValue("O", "0xf"),
                    SettingValue("Latest", "0xffffff"),
                ]),
        Setting("0x10E41DF7", "DLSS-RR Preset", "DLSS / NGX", "enum", "0x0",
                "DLSS-RR Preset",
                "Forces specific DLSS Ray Reconstruction preset.",
                values=[
                    SettingValue("Off", "0x0"), SettingValue("A", "0x1"),
                    SettingValue("B", "0x2"), SettingValue("C", "0x3"),
                    SettingValue("D", "0x4"), SettingValue("E", "0x5"),
                    SettingValue("F", "0x6"), SettingValue("G", "0x7"),
                    SettingValue("H", "0x8"), SettingValue("I", "0x9"),
                    SettingValue("J", "0xa"), SettingValue("K", "0xb"),
                    SettingValue("L", "0xc"), SettingValue("M", "0xd"),
                    SettingValue("N", "0xe"), SettingValue("O", "0xf"),
                    SettingValue("Latest", "0xffffff"),
                ]),
        Setting("0x10AFB768", "DLSS-SR Mode", "DLSS / NGX", "enum", "0x3",
                "DLSS-SR Mode",
                "Forces DLSS Super Resolution quality mode.",
                values=[
                    SettingValue("Performance", "0x0"),
                    SettingValue("Balanced", "0x1"),
                    SettingValue("Quality", "0x2"),
                    SettingValue("Snippet Ctrl", "0x3"),
                    SettingValue("DLAA", "0x4"),
                    SettingValue("Ultra Perf", "0x5"),
                    SettingValue("Custom", "0x6"),
                    SettingValue("Reserved", "0x7"),
                ]),
        Setting("0x10BD9423", "DLSS-RR Mode", "DLSS / NGX", "enum", "0x3",
                "DLSS-RR Mode",
                "Forces DLSS Ray Reconstruction quality mode.",
                values=[
                    SettingValue("Performance", "0x0"),
                    SettingValue("Balanced", "0x1"),
                    SettingValue("Quality", "0x2"),
                    SettingValue("Snippet Ctrl", "0x3"),
                    SettingValue("DLAA", "0x4"),
                    SettingValue("Ultra Perf", "0x5"),
                    SettingValue("Custom", "0x6"),
                ]),
        Setting("0x10E41DF4", "DLAA Override", "DLSS / NGX", "enum", "0x0",
                "DLAA Override",
                "Forces DLSS to operate in DLAA mode.",
                values=[
                    SettingValue("Default", "0x0"),
                    SettingValue("Force DLAA", "0x1"),
                ]),
        Setting("0x10E41DF5", "DLSS-SR Scaling", "DLSS / NGX", "numeric", "0",
                "DLSS-SR Scaling",
                "Custom scaling ratio for DLSS-SR.",
                min=33, max=100),
        Setting("0x10C7D4A2", "DLSS-RR Scaling", "DLSS / NGX", "numeric", "0",
                "DLSS-RR Scaling",
                "Custom scaling ratio for DLSS-RR.",
                min=33, max=100),
        Setting("0x104D6667", "DLSS-G Multi-Frame", "DLSS / NGX", "enum", "0x0",
                "DLSS-G Multi-Frame",
                "Controls DLSS Frame Generation frame count.",
                values=[
                    SettingValue("Off / App", "0x0"), SettingValue("2x", "0x1"),
                    SettingValue("3x", "0x2"), SettingValue("4x", "0x3"),
                    SettingValue("5x", "0x4"), SettingValue("6x", "0x5"),
                ]),
        Setting("0x10308298", "DLSSG Mode", "DLSS / NGX", "enum", "0x0",
                "DLSSG Mode",
                "Controls DLSS Frame Generation behavior.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Off", "0x1"),
                    SettingValue("On", "0x2"),
                    SettingValue("Auto", "0x3"),
                    SettingValue("Dynamic", "0x4"),
                ]),
        Setting("0x10562D0F", "DLSSG Dynamic Max", "DLSS / NGX", "enum", "0x0",
                "DLSSG Dynamic Max",
                "Maximum DLSSG dynamic multi-frame count.",
                values=[
                    SettingValue("Off", "0x0"), SettingValue("Up to 2x", "0x1"),
                    SettingValue("Up to 3x", "0x2"), SettingValue("Up to 4x", "0x3"),
                    SettingValue("Up to 5x", "0x4"), SettingValue("Up to 6x", "0x5"),
                ]),
        # Header: DISABLED=0, MIN=1, MAX=0x00FFFFFF, AUTO=0x01000000. The old
        # range (min=1, max=0xFFFFFF) made both "disabled" and "auto"
        # impossible to enter from the spin box.
        Setting("0x10CF4125", "DLSSG Target FPS", "DLSS / NGX", "numeric", "0x0",
                "DLSSG Target FPS",
                "Sets target FPS for DLSS Frame Generation. 0 = disabled, 16777216 (0x1000000) = auto.",
                presets=[Preset("Disabled", "0x0"), Preset("60", "0x3C"),
                         Preset("120", "0x78"), Preset("144", "0x90"),
                         Preset("165", "0xA5"), Preset("240", "0xF0"),
                         Preset("Auto", "0x1000000")],
                min=0, max=0x1000000),
        Setting("0x10AFB76C", "DLSS Ultra-Perf", "DLSS / NGX", "enum", "0x0",
                "DLSS Ultra-Performance",
                "Forces DLSS into Ultra Performance mode.",
                values=[
                    SettingValue("None", "0x0"),
                    SettingValue("Force Ultra Perf", "0x1"),
                ]),
        Setting("0x10444444", "NVIDIA Upscaling", "DLSS / NGX", "enum", "0x0",
                "NVIDIA Upscaling",
                "Enables NVIDIA Image Scaling (NIS).",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        # ---- New DLSS NR settings ----
        Setting("0x10E41E04", "DLSS-NR Override", "DLSS / NGX", "enum", "0x0",
                "DLSS-NR override",
                "Enables override for DLSS Noise Reduction.",
                values=[SettingValue("Off", "0x0"), SettingValue("On", "0x1")]),
        Setting("0x10E41DF8", "DLSS-NR Preset", "DLSS / NGX", "enum", "0x0",
                "DLSS-NR Preset",
                "Forces specific DLSS Noise Reduction preset.",
                values=[
                    SettingValue("Off", "0x0"), SettingValue("A", "0x1"),
                    SettingValue("B", "0x2"), SettingValue("C", "0x3"),
                    SettingValue("D", "0x4"),
                    SettingValue("Latest", "0xffffff"),
                ]),
        Setting("0x10E41E05", "DLSS-NR SL Override", "DLSS / NGX", "enum", "0x0",
                "DLSS-NR SL override",
                "Enables override for DLSS Noise Reduction Super Lens.",
                values=[SettingValue("Off", "0x0"), SettingValue("On", "0x1")]),
        Setting("0x10E41E06", "Streamline Override", "DLSS / NGX", "enum", "0x0",
                "Enable Streamline override",
                "Enables the driver-side override for Streamline-integrated DLSS.",
                values=[SettingValue("Off", "0x0"), SettingValue("On", "0x1")]),
    ])

    # ===== Power / Performance =====
    settings.extend([
        Setting("0x1057EB71", "GPU Power Mode", "Power", "enum", "0x5",
                "GPU Power Mode",
                "Controls GPU power management behavior.",
                values=[
                    SettingValue("Adaptive", "0x0"),
                    SettingValue("Prefer Max", "0x1"),
                    SettingValue("Driver Ctrl", "0x2"),
                    SettingValue("Consistent Perf", "0x3"),
                    SettingValue("Prefer Min", "0x4"),
                    SettingValue("Optimal Power", "0x5"),
                ]),
        Setting("0x10D1EF29", "GPU Max Power", "Power", "numeric", "0x0",
                "GPU Max Power",
                "Maximum GPU power limit in watts.",
                min=0, max=175),
        Setting("0x00AE785C", "Power Throttle", "Power", "enum", "0x0",
                "Power Throttle",
                "PCIe slot power compliance throttling.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
    ])

    # ===== Frame Rate / Latency =====
    settings.extend([
        Setting("0x007BA09E", "Pre-Render Limit", "Frame Rate", "numeric", "0",
                "Pre-Rendered Frames",
                "Maximum frames CPU can prepare ahead.",
                min=0, max=255),
        Setting("0x10835002", "FPS Limiter", "Frame Rate", "numeric", "0x0",
                "FPS Limiter",
                "Hard frame rate cap. 60=0x3C, 120=0x78, 144=0x90",
                presets=[Preset("Off", "0x0"), Preset("60", "0x3C"),
                         Preset("120", "0x78"), Preset("144", "0x90"),
                         Preset("165", "0xA5"), Preset("240", "0xF0")],
                min=0, max=1023),  # Düzeltildi: header'dan FRL_FPS_MIN/MAX
        Setting("0x10835016", "Idle FPS Limit", "Frame Rate", "numeric", "0x14",
                "Idle FPS Limit",
                "FPS cap when app loses focus. 20=0x14, 30=0x1E, 60=0x3C",
                min=0, max=1023),  # Düzeltildi: header'daki FRL_FPS ile aynı
        Setting("0x10835017", "Idle Threshold", "Frame Rate", "numeric", "3",
                "Idle Threshold",
                "Seconds before idle FPS limit applies.",
                min=0, max=3600),
        Setting("0x10111133", "VR Pre-Render", "Frame Rate", "numeric", "1",
                "VR Pre-Rendered Frames",
                "Pre-rendered frames for VR applications.",
                min=0, max=255),
        Setting("0x1095F170", "Latency Autoalign", "Frame Rate", "enum", "0x1",
                "Latency Autoalign",
                "Auto-alignment of latency flash indicators.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Enabled", "0x1"),
                ]),
    ])

    # ===== VRR / G-Sync =====
    settings.extend([
        Setting("0x1194F158", "G-Sync/VRR Mode", "VRR / G-Sync", "enum", "0x1",
                "G-Sync/VRR Mode",
                "Controls G-SYNC/VRR globally.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Fullscreen", "0x1"),
                    SettingValue("Full+Window", "0x2"),
                ]),
        Setting("0x1094F1F7", "VRR Request", "VRR / G-Sync", "enum", "0x1",
                "VRR Request State",
                "Specifies VRR modes application can request.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Fullscreen", "0x1"),
                    SettingValue("Full+Window", "0x2"),
                ]),
        Setting("0x10A879CE", "VRR Control", "VRR / G-Sync", "enum", "0x1",
                "VRR Control",
                "Enables/disables VRR per application.",
                values=[
                    SettingValue("Disable", "0x0"),
                    SettingValue("Enable", "0x1"),
                    SettingValue("Not Supported", "0x9f95128e"),
                ]),
        Setting("0x00A879CF", "VSync Mode", "VRR / G-Sync", "hex-preset", "0x60925292",
                "VSync Mode",
                "Advanced VSync control with VRR.",
                presets=[
                    Preset("Passive", "0x60925292"),
                    Preset("Force Off", "0x08416747"),
                    Preset("Force On", "0x47814940"),
                    Preset("Flip 2", "0x32610244"),
                    Preset("Flip 3", "0x71271021"),
                    Preset("Flip 4", "0x13245256"),
                    Preset("Virtual", "0x18888888"),
                ]),
        Setting("0x10A879CF", "G-Sync Override", "VRR / G-Sync", "enum", "0x0",
                "G-Sync Override",
                "Application-level G-SYNC override.",
                values=[
                    SettingValue("Allow", "0x0"),
                    SettingValue("Force Off", "0x1"),
                    SettingValue("Disallow", "0x2"),
                    SettingValue("ULMB", "0x3"),
                    SettingValue("Fixed Ref", "0x4"),
                ]),
        Setting("0x10A879AC", "VRR App Override", "VRR / G-Sync", "enum", "0x0",
                "VRR App Override",
                "Request state for G-SYNC per application.",
                values=[
                    SettingValue("Allow", "0x0"),
                    SettingValue("Force Off", "0x1"),
                    SettingValue("Disallow", "0x2"),
                    SettingValue("ULMB", "0x3"),
                    SettingValue("Fixed Ref", "0x4"),
                ]),
        Setting("0x1094F157", "VRR Feature", "VRR / G-Sync", "hex-preset", "0x1",
                "VRR Feature",
                "Toggle VRR global feature indicator.",
                presets=[
                    Preset("Disabled", "0x0"),
                    Preset("Enabled", "0x1"),
                ]),
        Setting("0x1095F16F", "VRR Overlay", "VRR / G-Sync", "hex-preset", "0x1",
                "VRR Overlay",
                "Display VRR overlay indicator.",
                presets=[
                    Preset("Disabled", "0x0"),
                    Preset("Enabled", "0x1"),
                ]),
    ])

    # ===== VSync / Flip =====
    settings.extend([
        Setting("0x005A375C", "Tear Control", "VSync / Flip", "hex-preset", "0x96861077",
                "Tear Control",
                "Controls screen tearing behavior.",
                presets=[
                    Preset("Disable tearing", "0x96861077"),
                    Preset("Enable tearing", "0x99941284"),
                ]),
        Setting("0x101AE763", "Smooth AFR", "VSync / Flip", "enum", "0x0",
                "Smooth AFR",
                "Controls smooth Alternate Frame Rendering.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x10FDEC23", "VSync Flags", "VSync / Flip", "enum", "0x0",
                "VSync Flags",
                "Additional VSync behavior controls.",
                values=[
                    SettingValue("Default", "0x0"),
                    SettingValue("Ignore Flip Interval", "0x1"),
                ]),
    ])

    # ===== Shader Cache =====
    settings.extend([
        Setting("0x00198FFF", "Shader Cache", "Shader", "enum", "0x1",
                "Shader Cache",
                "Enables/disables shader disk cache.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x00AC8497", "Cache Max Size", "Shader", "enum", "0x4000",
                "Cache Max Size",
                "Maximum shader cache size in MB.",
                values=[
                    SettingValue("Default", "0x0"),
                    SettingValue("4096 MB", "0x1000"),
                    SettingValue("8192 MB", "0x2000"),
                    SettingValue("16384 MB", "0x4000"),
                    SettingValue("32768 MB", "0x8000"),
                ]),
        Setting("0x00D74EF6", "Offline Compiler", "Shader", "enum", "0x1",
                "Offline Compiler",
                "Enables offline shader compilation.",
                values=[
                    SettingValue("Off", "0x07184358"),
                    SettingValue("On", "0x64318112"),
                ]),
    ])

    # ===== Ambient Occlusion =====
    settings.extend([
        Setting("0x00667329", "AO Mode", "AO", "enum", "0x0",
                "AO Mode",
                "Controls Ambient Occlusion quality.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("Low", "0x1"),
                    SettingValue("Medium", "0x2"),
                    SettingValue("High", "0x3"),
                ]),
        Setting("0x00664339", "AO Usage", "AO", "enum", "0x0",
                "AO Usage",
                "Enables/disables AO per application.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Enabled", "0x1"),
                ]),
    ])

    # ===== FXAA =====
    settings.extend([
        Setting("0x1034CB89", "FXAA Allow", "FXAA", "enum", "0x1",
                "FXAA Allow",
                "Controls whether FXAA is allowed.",
                values=[
                    SettingValue("Disallowed", "0x0"),
                    SettingValue("Allowed", "0x1"),
                ]),
        Setting("0x1074C972", "FXAA Enable", "FXAA", "enum", "0x0",
                "FXAA Enable",
                "Enables/disables FXAA globally.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x1068FB9C", "FXAA Indicator", "FXAA", "enum", "0x0",
                "FXAA Indicator",
                "Shows FXAA on-screen indicator.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
    ])

    # ===== Ansel =====
    settings.extend([
        Setting("0x1035DB89", "Ansel Allow", "Ansel", "enum", "0x1",
                "Ansel Allow",
                "Controls whether NVIDIA Ansel is allowed.",
                values=[
                    SettingValue("Disallowed", "0x0"),
                    SettingValue("Allowed", "0x1"),
                ]),
        Setting("0x1085DA8A", "Ansel Allowlisted", "Ansel", "enum", "0x0",
                "Ansel Allowlisted",
                "Marks applications as Ansel-enabled.",
                values=[
                    SettingValue("Not allowlisted", "0x0"),
                    SettingValue("Allowlisted", "0x1"),
                ]),
        Setting("0x1075D972", "Ansel Enable", "Ansel", "enum", "0x1",
                "Ansel Enable",
                "Enables/disables NVIDIA Ansel.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
    ])

    # ===== Stereo / 3D =====
    settings.extend([
        Setting("0x11AE435C", "Stereo Eyes Exchange", "Stereo", "enum", "0x0",
                "Stereo - Eyes Exchange",
                "Swaps left and right eye in stereo 3D.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x11E91A61", "Stereo Display Mode", "Stereo", "enum", "0x0",
                "Stereo - Display Mode",
                "Selects stereo 3D display method.",
                values=[
                    SettingValue("Shutter Glasses", "0x0"),
                    SettingValue("Vertical Interlaced", "0x1"),
                    SettingValue("TwinView", "0x2"),
                    SettingValue("NV17 Auto", "0x3"),
                    SettingValue("NV17 DAC0", "0x4"),
                    SettingValue("NV17 DAC1", "0x5"),
                    SettingValue("Color Line", "0x6"),
                    SettingValue("Color Interleaved", "0x7"),
                    SettingValue("Anaglyph", "0x8"),
                    SettingValue("Horizontal Interlaced", "0x9"),
                    SettingValue("Side Field", "0xa"),
                    SettingValue("Sub Field", "0xb"),
                    SettingValue("Checkerboard", "0xc"),
                    SettingValue("Inverse Checkerboard", "0xd"),
                    SettingValue("Tridelity SL", "0xe"),
                    SettingValue("Tridelity MV", "0xf"),
                    SettingValue("SeeFront", "0x10"),
                    SettingValue("Stereo Mirror", "0x11"),
                    SettingValue("Frame Sequential", "0x12"),
                    SettingValue("Autodetect Passive", "0x13"),
                    SettingValue("AEGIS DT", "0x14"),
                    SettingValue("OEM Emitter", "0x15"),
                    SettingValue("DP Inband", "0x16"),
                    SettingValue("HW Default", "0xffffffff"),
                ]),
        Setting("0x112493BD", "Stereo Dongle", "Stereo", "enum", "0x1",
                "Stereo - Dongle Support",
                "Configures stereo dongle support.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("DAC", "0x1"),
                    SettingValue("DLP", "0x2"),
                ]),
        Setting("0x11AA9E99", "Stereo Support", "Stereo", "enum", "0x0",
                "Stereo - Support",
                "Enables/disables stereo 3D rendering.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x11333333", "Stereo Swap Mode", "Stereo", "enum", "0x0",
                "Stereo - Swap Mode",
                "Controls stereo swap behavior.",
                values=[
                    SettingValue("App Control", "0x0"),
                    SettingValue("Per Eye", "0x1"),
                    SettingValue("Per Eye Pair", "0x2"),
                    SettingValue("Legacy", "0x3"),
                    SettingValue("Per Eye Swap", "0x4"),
                ]),
    ])

    # ===== Misc Settings =====
    settings.extend([
        Setting("0x108F0841", "Export Perf Counters", "Misc", "enum", "0x0",
                "Export Performance Counters",
                "Enables export of GPU performance counters.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x10115C8D", "XQM Mode", "Misc", "enum", "0x0",
                "XQM Mode",
                "External Quiet Mode for reduced noise.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x10287051", "SLI Indicator", "Misc", "hex-preset", "0x34534064",
                "SLI Indicator",
                "Shows on-screen SLI status indicator.",
                presets=[
                    Preset("Disabled", "0x34534064"),
                    Preset("Enabled", "0x24545582"),
                ]),
        Setting("0x1094F16F", "PhysX Indicator", "Misc", "hex-preset", "0x34534064",
                "PhysX Indicator",
                "Shows on-screen PhysX status indicator.",
                presets=[
                    Preset("Disabled", "0x34534064"),
                    Preset("Enabled", "0x24545582"),
                ]),
        Setting("0x10115C8C", "Battery Boost", "Misc", "numeric", "0",
                "Battery Boost",
                "Target FPS for Battery Boost mode.",
                min=0, max=1023),
        Setting("0x104554B6", "Profile Timeout", "Misc", "enum", "0x0",
                "Profile Timeout",
                "Application profile notification timeout.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("9 Sec", "0x9"),
                    SettingValue("15 Sec", "0xf"),
                    SettingValue("30 Sec", "0x1e"),
                    SettingValue("1 Min", "0x3c"),
                    SettingValue("2 Min", "0x78"),
                ]),
        Setting("0x107CDDBC", "Steam ID", "Misc", "hex", "0x0",
                "Steam ID",
                "Steam Application ID for profile."),
        Setting("0x106D5CFF", "CPL Hidden", "Misc", "enum", "0x0",
                "CPL Hidden",
                "Hides profile from NVIDIA Control Panel.",
                values=[
                    SettingValue("Disabled", "0x0"),
                    SettingValue("Enabled", "0x1"),
                ]),
        Setting("0x10354FF8", "CUDA Excluded", "Misc", "hex", "0x0",
                "CUDA Excluded",
                "Excludes specific GPUs from CUDA."),
        Setting("0x10F9DC83", "Optimus Max AA", "Misc", "numeric", "0",
                "Optimus Max AA",
                "Maximum AA samples for Optimus.",
                min=0, max=16),
        Setting("0x103BCCB5", "Prevent UI AF", "Misc", "enum", "0x0",
                "Prevent UI AF",
                "Prevents AF override in UI elements.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x00AB8687", "Set VAB Data", "Misc", "enum", "0xffffffff",
                "Set VAB Data",
                "VAB Default Data values.",
                values=[
                    SettingValue("Zero", "0x0"),
                    SettingValue("UINT One", "0x1"),
                    SettingValue("Float One", "0x3f800000"),
                    SettingValue("Float Inf", "0x7f800000"),
                    SettingValue("Float NaN", "0x7fc00000"),
                    SettingValue("API Defaults", "0xffffffff"),
                ]),
        Setting("0x0098C1AC", "MFAA", "Misc", "enum", "0x0",
                "MFAA",
                "Multi-Frame Anti-Aliasing.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
        Setting("0x0064B541", "Refresh Rate", "Misc", "enum", "0x0",
                "Refresh Rate",
                "Preferred display refresh rate.",
                values=[
                    SettingValue("App Controlled", "0x0"),
                    SettingValue("Highest Available", "0x1"),
                    SettingValue("LL RR Mask", "0x00000FF0"),
                ]),
        Setting("0x00B65E72", "Perf Counters DX9", "Misc", "enum", "0x0",
                "Perf Counters DX9",
                "Performance counters for DX9 only.",
                values=[
                    SettingValue("Off", "0x0"),
                    SettingValue("On", "0x1"),
                ]),
    ])

    # ===== SLI Settings =====
    settings.extend([
        Setting("0x1033DCD1", "SLI GPU Count", "SLI", "enum", "0x0",
                "SLI GPU Count",
                "Number of GPUs to use in SLI.",
                values=[
                    SettingValue("Autoselect", "0x0"),
                    SettingValue("1", "0x1"),
                    SettingValue("2", "0x2"),
                    SettingValue("3", "0x3"),
                    SettingValue("4", "0x4"),
                ]),
        Setting("0x1033DCD2", "SLI Predefined Count", "SLI", "enum", "0x0",
                "SLI Predefined Count",
                "Predefined SLI GPU count.",
                values=[
                    SettingValue("Autoselect", "0x0"),
                    SettingValue("1", "0x1"),
                    SettingValue("2", "0x2"),
                    SettingValue("3", "0x3"),
                    SettingValue("4", "0x4"),
                ]),
        Setting("0x1033DCD3", "SLI Predefined Count DX10", "SLI", "enum", "0x0",
                "SLI Predefined Count DX10",
                "Predefined SLI GPU count for DX10.",
                values=[
                    SettingValue("Autoselect", "0x0"),
                    SettingValue("1", "0x1"),
                    SettingValue("2", "0x2"),
                    SettingValue("3", "0x3"),
                    SettingValue("4", "0x4"),
                ]),
        Setting("0x1033CEC1", "SLI Predefined Mode", "SLI", "enum", "0x0",
                "SLI Predefined Mode",
                "Predefined SLI rendering mode.",
                values=[
                    SettingValue("Autoselect", "0x0"),
                    SettingValue("Force Single", "0x1"),
                    SettingValue("Force AFR", "0x2"),
                    SettingValue("Force AFR2", "0x3"),
                    SettingValue("Force SFR", "0x4"),
                    SettingValue("AFR of SFR", "0x5"),
                ]),
        Setting("0x1033CEC2", "SLI Predefined Mode DX10", "SLI", "enum", "0x0",
                "SLI Predefined Mode DX10",
                "Predefined SLI mode for DX10.",
                values=[
                    SettingValue("Autoselect", "0x0"),
                    SettingValue("Force Single", "0x1"),
                    SettingValue("Force AFR", "0x2"),
                    SettingValue("Force AFR2", "0x3"),
                    SettingValue("Force SFR", "0x4"),
                    SettingValue("AFR of SFR", "0x5"),
                ]),
        Setting("0x1033CED1", "SLI Rendering Mode", "SLI", "enum", "0x0",
                "SLI Rendering Mode",
                "SLI rendering mode for application.",
                values=[
                    SettingValue("Autoselect", "0x0"),
                    SettingValue("Force Single", "0x1"),
                    SettingValue("Force AFR", "0x2"),
                    SettingValue("Force AFR2", "0x3"),
                    SettingValue("Force SFR", "0x4"),
                    SettingValue("AFR of SFR", "0x5"),
                ]),
    ])

    # ===== Optimus Settings =====
    settings.extend([
        Setting("0x10F9DC80", "Shim MCCompat", "Optimus", "enum", "0x10",
                "Shim MCCompat",
                "Optimus multi-compatibility mode.",
                values=[
                    SettingValue("Integrated", "0x0"),
                    SettingValue("Enable", "0x1"),
                    SettingValue("User Editable", "0x2"),
                    SettingValue("Video", "0x4"),
                    SettingValue("Varying", "0x8"),
                    SettingValue("Auto Select", "0x10"),
                    SettingValue("Override", "0x80000000"),
                ]),
        Setting("0x10F9DC81", "Shim Rendering Mode", "Optimus", "enum", "0x10",
                "Shim Rendering Mode",
                "Controls which GPU renders on Optimus.",
                values=[
                    SettingValue("Integrated", "0x0"),
                    SettingValue("Enable", "0x1"),
                    SettingValue("User Editable", "0x2"),
                    SettingValue("Video", "0x4"),
                    SettingValue("Varying", "0x8"),
                    SettingValue("Auto Select", "0x10"),
                    SettingValue("Override", "0x80000000"),
                ]),
        Setting("0x10F9DC82", "Shim Max Res", "Optimus", "hex", "0x0",
                "Shim Max Res",
                "Maximum resolution for Optimus."),
        Setting("0x10F9DC84", "Shim Rendering Options", "Optimus", "bitfield", "0x0",
                "Shim Rendering Options",
                "Advanced Optimus rendering options.",
                bits=[
                    BitField("Default Rendering Mode", 0x00000000),
                    BitField("Disable Async Present", 0x00000001),
                    BitField("EHSHELL Detect", 0x00000002),
                    BitField("FlashPlayer Host Detect", 0x00000004),
                    BitField("Video DRM App Detect", 0x00000008),
                    BitField("Ignore Overrides", 0x00000010),
                    # 0x00000020 = SHIM_RENDERING_OPTIONS_RESERVED1 (skipped)
                    BitField("Enable DWM Async Present", 0x00000040),
                    # 0x00000080 = SHIM_RENDERING_OPTIONS_RESERVED2 (skipped)
                    BitField("Allow Inheritance", 0x00000100),
                    BitField("Disable Wrappers", 0x00000200),
                    BitField("Disable DXGI Wrappers", 0x00000400),
                    BitField("Prune Unsupported Formats", 0x00000800),
                    BitField("Enable Alpha Format", 0x00001000),
                    BitField("iGPU Transcoding", 0x00002000),
                    BitField("Disable CUDA", 0x00004000),
                    BitField("Allow CP Caps For Video", 0x00008000),
                    BitField("iGPU Transcoding Fwd Optimus", 0x00010000),
                    BitField("Disable During Secure Boot", 0x00020000),
                    BitField("Invert For Quadro", 0x00040000),
                    BitField("Invert For MSHybrid", 0x00080000),
                    BitField("Register Process Enable Gold", 0x00100000),
                    BitField("Handle Windowed Mode Perf Opt", 0x00200000),
                    BitField("Handle Win7 Async Runtime Bug", 0x00400000),
                    BitField("Explicit Adapter Opted By App", 0x00800000),
                    BitField("Allow Dynamic Display Mux Switch", 0x01000000),
                    BitField("Disallow Dynamic Display Mux Switch", 0x02000000),
                    BitField("Disable Turing Power Policy", 0x04000000),
                    BitField("Allow Dynamic Display Mux Switch MDM", 0x08000000),
                    BitField("Disallow Dynamic Display Mux Switch MDM", 0x10000000),
                ]),
    ])

    # =========================================================================
    # Detailed descriptions – applied by ID after all settings are created.
    # Keeps the setting definitions above clean while adding rich help text.
    # =========================================================================
    DETAILED_DESCS: Dict[str, str] = {

        # ── OpenGL ───────────────────────────────────────────────────────────
        "0x2089BF6C": (
            "Applies gamma correction to the edges of antialiased lines in OpenGL. "
            "When enabled, line edges are blended in linear light space before being "
            "converted back to gamma-corrected output, which produces smoother and "
            "more visually accurate results on non-linear (sRGB) displays. "
            "Most users can leave this at its default (Disabled). "
            "Enable it if you notice harsh or jagged line edges in OpenGL content."
        ),
        "0x2072C5A3": (
            "Controls how the OpenGL driver interacts with the Windows GDI (Graphics "
            "Device Interface) subsystem. GDI compatibility is required by legacy "
            "applications that mix OpenGL rendering with GDI calls (e.g. drawing text "
            "or UI elements on top of an OpenGL window). "
            "'Auto' lets the driver decide per application; 'Prefer Enabled' forces "
            "GDI compatibility mode which may reduce performance slightly; "
            "'Prefer Disabled' maximises performance but can break apps that rely on "
            "GDI/OpenGL interop."
        ),
        "0x20D690F8": (
            "Selects the method used to present rendered frames to the display for "
            "Vulkan and OpenGL. 'Auto' allows the driver to choose the best path. "
            "'Prefer Enabled' forces direct present (lower latency, better VRR "
            "compatibility). 'Prefer Disabled' uses a blit/copy path which can help "
            "with capture tools or older display configurations but adds latency. "
            "On Linux/Proton leave at 'Auto' unless you encounter tearing or sync issues."
        ),
        "0x2097C2F6": (
            "Enables 10-bit (Deep Color) framebuffer output for OpenGL applications. "
            "When enabled, the driver allocates a 10-bpc render target, allowing up to "
            "1024 discrete values per color channel instead of 256. "
            "Requires a display connected via DisplayPort or HDMI 2.0+ that supports "
            "10-bit color. Has no visual effect on 8-bit displays. "
            "Disable if you see color banding artifacts or driver-level compatibility issues."
        ),
        "0x206A6582": (
            "Sets the default vertical sync (VSync) interval for OpenGL applications "
            "that do not explicitly request one. "
            "'VSync 1x' caps frame rate to the monitor refresh and eliminates tearing. "
            "'Tear' (0) presents frames immediately, enabling max FPS with potential tearing. "
            "'Force Off' overrides any per-app VSync request and disables sync entirely. "
            "'Force On' overrides per-app requests and always enables sync. "
            "'App Controlled' respects the application's own wglSwapIntervalEXT() call. "
            "'Disable' (0xFFFFFFFF) is a special sentinel that prevents the driver from "
            "applying any default — the app must set its own interval."
        ),
        "0x20C1221E": (
            "Enables multi-threaded OpenGL command submission, allowing the driver to "
            "process OpenGL calls on a separate background thread. This can improve "
            "CPU-side performance in single-threaded games by overlapping CPU work with "
            "GPU command processing. However, it introduces a small amount of latency "
            "and may cause glitches in poorly written apps. "
            "'Driver Default' lets the driver decide per application. "
            "'Enable' forces it on; 'Disable' forces it off."
        ),
        "0x20FDD1F9": (
            "Enables triple-buffering for OpenGL applications. With double-buffering "
            "and VSync, the GPU stalls waiting for vblank. Triple-buffering adds a third "
            "buffer so the GPU can keep rendering while the front buffer is being "
            "displayed, reducing stuttering and improving smoothness. "
            "Most useful in VSync-on scenarios. With VSync off, triple buffering has "
            "minimal benefit and wastes VRAM."
        ),

        # ── Anti-Aliasing ─────────────────────────────────────────────────────
        "0x10ECDB82": (
            "Bitfield that controls how the global AA override and enhancement modes "
            "interact with per-application settings. Individual bits select transition "
            "behaviors: e.g. whether 'Override' mode can revert to 'App Controlled', "
            "whether SLI-AA is disabled, whether CPLAA (Coverage Programmable Line AA) "
            "is suppressed, etc. Leave at 0x0 (all bits off) unless you are "
            "troubleshooting AA conflicts with a specific application."
        ),
        "0x10D773D2": (
            "Selects the anti-aliasing sample pattern and method applied by the driver. "
            "Supersampling (SSAA) renders at a higher internal resolution and downscales "
            "— highest quality but very expensive. Multisampling (MSAA) only resolves "
            "edges of geometry — good quality/performance balance. "
            "VCAA and Mixedsample modes use coverage data from rasterisation combined "
            "with color samples for very high effective AA at lower cost. "
            "Values prefixed with 'Free' are unlabelled slots in the driver header. "
            "Only effective when AA Mode is set to 'Override' or 'Enhance'."
        ),
        "0x107EFC5B": (
            "Global anti-aliasing mode selector. "
            "'App Controlled' (default) — the game decides its own AA method. "
            "'Override' — the driver replaces whatever the app requests with the "
            "AA Method setting above. "
            "'Enhance' — adds driver AA on top of the application's own AA. "
            "Use 'Override' with MSAA 4x or 8x for older DX9/OpenGL titles that lack "
            "built-in AA options. Modern titles with TAA/DLSS should stay at 'App Controlled'."
        ),
        "0x107D639D": (
            "Applies gamma correction to antialiased geometry edges. Without correction, "
            "AA is computed in gamma space which can produce slightly darker edges. "
            "'Off' — standard behavior. "
            "'On if FOS' — correction only when Full Order Supersampling is active. "
            "'On always' — always correct to linear space before resolving. "
            "Most modern games handle this internally; this override is mainly useful "
            "for older OpenGL/DX9 titles."
        ),
        "0x10FC2D9C": (
            "Enables alpha-to-coverage (A2C) multisampling for transparent geometry. "
            "Alpha-to-coverage uses the MSAA sample mask to approximate transparency "
            "without alpha blending, which is more compatible with depth buffering. "
            "Useful in vegetation-heavy scenes (grass, foliage, fences) where alpha "
            "clipping with standard MSAA produces harsh edges. "
            "Only effective when MSAA is enabled."
        ),
        "0x10D48A85": (
            "Enables supersampling for transparent (alpha-tested) geometry. "
            "Unlike alpha-to-coverage which approximates transparency, supersampling "
            "fully renders transparent surfaces at a higher sample count. "
            "'Alpha Test' — only supersample alpha-test geometry. "
            "'Pixel Kill' — use pixel kill optimization. "
            "'Dynamic Branch' — supersample via shader dynamic branching. "
            "'All' — apply to all transparency types. "
            "Most performance-intensive of the transparency AA options."
        ),

        # ── Texture Filtering ─────────────────────────────────────────────────
        "0x101E61A9": (
            "Sets the anisotropic filtering (AF) sample level. AF corrects texture "
            "blur on surfaces viewed at an oblique angle. Higher values sharpen "
            "textures at steep angles but increase GPU texture unit load. "
            "2x is nearly free on modern hardware; 8x–16x has a small cost. "
            "Only effective when Aniso Mode is set to 'User Override'. "
            "Recommended: 16x for modern NVIDIA GPUs with negligible performance cost."
        ),
        "0x10D2BB16": (
            "Controls when anisotropic filtering is applied. "
            "'App Controlled' — the game decides its own AF level (recommended for "
            "most modern titles). "
            "'User Override' — forces the driver AF level set in Aniso Level above. "
            "'Conditional' — applies driver AF only when the app requests a lower level."
        ),
        "0x00CE2691": (
            "High-level quality/performance trade-off for all texture filtering operations. "
            "'High Quality' applies the most accurate filtering algorithms and may enable "
            "negative LOD bias correction for sharper results. "
            "'Quality' is the balanced default. "
            "'Performance' and 'High Performance' use faster but less accurate filter "
            "implementations to save GPU texture unit bandwidth. "
            "Most users should leave this at 'Quality'."
        ),
        "0x00E73211": (
            "When enabled, the driver may reduce the effective AF sample count on "
            "surfaces that are less oblique, where full AF would be wasted. "
            "This optimization is nearly invisible in practice and saves GPU bandwidth. "
            "Disable only if you notice unexpected texture blurring on surfaces that "
            "should be filtered more sharply."
        ),
        "0x0019BB68": (
            "Controls whether shaders can use negative LOD (Level of Detail) bias to "
            "sharpen textures beyond their native resolution. "
            "'Allow negative' — permits shaders to specify negative bias values, "
            "which can over-sharpen textures and introduce aliasing or shimmer. "
            "'Clamp' (recommended with DLSS/TAA) — prevents negative bias, which "
            "avoids the shimmering artifact that occurs when temporal upscalers "
            "combine negative bias with reconstructed sub-pixel data."
        ),

        # ── DLSS / NGX ────────────────────────────────────────────────────────
        "0x10E41E01": (
            "Master override switch for DLSS Super Resolution (DLSS-SR). "
            "When set to 'On', DXVK-NVAPI allows the driver-level SR settings below "
            "(mode, preset, scaling) to take effect, overriding whatever the game "
            "engine has configured. Required before any DLSS-SR sub-setting takes effect. "
            "Set via env var: DXVK_NVAPI_DRS_NGX_DLSS_SR_OVERRIDE=on"
        ),
        "0x10E41E02": (
            "Master override switch for DLSS Ray Reconstruction (DLSS-RR, formerly "
            "DLSS 3.5 Denoiser). When 'On', driver-level RR settings override the "
            "in-game configuration. DLSS-RR replaces traditional denoising in path-traced "
            "games (e.g. Cyberpunk 2077 with RT Overdrive) with an AI denoiser that "
            "recovers more detail and reduces noise. "
            "Set via env var: DXVK_NVAPI_DRS_NGX_DLSS_RR_OVERRIDE=on"
        ),
        "0x10E41E03": (
            "Master override switch for DLSS Frame Generation (DLSS-FG / DLSSG). "
            "When 'On', allows the driver to control FG behavior independent of in-game "
            "settings. Frame Generation uses AI to synthesize intermediate frames between "
            "rendered frames, multiplying perceived frame rate. Requires RTX 40+ (Ada) "
            "or newer. On Turing/Ampere GPUs, DXVK_NVAPI_GPU_ARCH spoofing to GB200 "
            "is required to enable the FG code path. "
            "Set via env var: DXVK_NVAPI_DRS_NGX_DLSS_FG_OVERRIDE=on"
        ),
        "0x10E41DF3": (
            "Forces a specific DLSS Super Resolution neural network preset. "
            "Presets are purpose-built variants of the DLSS model that trade off "
            "temporal stability, sharpness, and ghosting differently:\n"
            "  A — Legacy default for Perf/Balanced/Quality. Good anti-ghosting for "
            "games missing motion vectors.\n"
            "  B — Legacy default for Ultra Performance mode.\n"
            "  C — Prefers current-frame data; less ghosting but less stable.\n"
            "  D — Favors temporal stability; was the modern default before E.\n"
            "  E — Current recommended default for Perf/Balanced/Quality (DLSS 3.7+). "
            "Better detail, sharpness, and reduced smearing vs D.\n"
            "  F — Current recommended default for Ultra Performance and DLAA (DLSS 3.7+).\n"
            "  'Latest' (0xFFFFFF) — always use the recommended preset for each mode.\n"
            "Presets K–O require DLSS 4.5+. When in doubt use 'Latest'."
        ),
        "0x10E41DF7": (
            "Forces a specific neural network preset for DLSS Ray Reconstruction. "
            "Same preset alphabet as DLSS-SR (see DLSS-SR Preset). "
            "RR presets control how aggressively the denoiser reconstructs ray-traced "
            "detail — higher letter presets generally prioritize temporal stability "
            "and reduced flickering in complex RT lighting. "
            "Recommended: 'Latest' (0xFFFFFF) to always get NVIDIA's current best "
            "model for RR."
        ),
        "0x10E41DF1": (
            "Forces a specific neural network preset for DLSS Frame Generation. "
            "FG presets control the motion interpolation model behavior, trading "
            "artifact reduction vs. motion clarity at high speeds. "
            "The preset alphabet is the same as DLSS-SR but the internal models differ. "
            "Presets K–O require DLSS 4.5+. "
            "Recommended: 'Latest' (0xFFFFFF) to use NVIDIA's current recommended FG model."
        ),
        "0x10AFB768": (
            "Forces the DLSS Super Resolution quality tier, overriding the in-game setting. "
            "'Performance' — renders at ~50% of output resolution (e.g. 1080p→2160p). "
            "Highest FPS boost, some quality loss.\n"
            "'Balanced' — renders at ~58% resolution. Good all-round choice.\n"
            "'Quality' — renders at ~67% resolution. Closest to native with DLSS.\n"
            "'DLAA' — renders at full native resolution and only applies anti-aliasing. "
            "Best quality, no performance gain.\n"
            "'Ultra Perf' — renders at ~33% resolution. Extreme FPS boost, soft image.\n"
            "'Custom' — uses the ratio set in DLSS-SR Scaling below.\n"
            "'Snippet Ctrl' (0x3) — leave game in control (default, no override).\n"
            "Only active when DLSS-SR Override is On."
        ),
        "0x10BD9423": (
            "Forces the DLSS Ray Reconstruction quality tier. Options are identical "
            "in name to DLSS-SR modes but map to different internal render ratios "
            "tuned for denoising rather than upscaling. "
            "Most users running RT Overdrive or other path-traced titles should leave "
            "this at 'Snippet Ctrl' and only change it if RR image quality is unsatisfactory. "
            "Only active when DLSS-RR Override is On."
        ),
        "0x10E41DF4": (
            "Forces DLSS to operate in DLAA (Deep Learning Anti-Aliasing) mode "
            "regardless of the quality mode the game sets. DLAA runs at full native "
            "resolution and uses the DLSS neural network solely for anti-aliasing — "
            "it does not upscale. Provides the highest image quality of all DLSS modes "
            "at the cost of not boosting frame rate. "
            "Only meaningful when DLSS-SR Override is On."
        ),
        "0x10E41DF5": (
            "Custom render resolution ratio for DLSS Super Resolution, expressed as "
            "an integer percentage (33–100). For example, 75 means the game renders "
            "at 75% of the output resolution before DLSS upscales to full output. "
            "Only active when DLSS-SR Mode is set to 'Custom'. "
            "Example: for 4K output at 75%, the internal render resolution is 2880×1620. "
            "Values below 50 are generally not recommended due to visible quality loss."
        ),
        "0x10C7D4A2": (
            "Custom render resolution ratio for DLSS Ray Reconstruction, expressed as "
            "an integer percentage (33–100). Analogous to DLSS-SR Scaling but applies "
            "only to the RR denoising pass. "
            "Only active when DLSS-RR Mode is set to 'Custom'. "
            "Requires DLSS-RR Override to be On."
        ),
        "0x104D6667": (
            "Controls how many interpolated frames DLSS Frame Generation inserts "
            "between each rendered frame (Multi-Frame Generation / MFG). "
            "'Off / App' (0) — no override; the game's own FG setting applies. "
            "'2x'–'6x' (1–5) — 1 to 5 generated frames per rendered frame. "
            "The header allows up to 15 (16x) but dxvk-nvapi documents 5 (6x) as the "
            "highest currently supported value, so larger values are not offered. "
            "Higher multipliers amplify latency proportionally; NVIDIA Reflex is "
            "strongly recommended to compensate. RTX 50 (Blackwell) supports MFG "
            "natively; earlier cards require DXVK_NVAPI_GPU_ARCH=GB200 spoofing."
        ),
        "0x10308298": (
            "Controls the activation state of DLSS Frame Generation (DLSSG). "
            "'Disabled' (0x0) — the DLSSG runtime is not initialized at all. "
            "'Off' (0x1) — runtime initializes but FG is inactive. "
            "'On' (0x2) — FG is active; generates frames between rendered frames. "
            "'Auto' (0x3) — driver decides based on frame rate and system load. "
            "'Dynamic' (0x4) — DLSSG dynamically adjusts the multiplier based on "
            "target FPS set in DLSSG Target FPS. Requires DLSS-FG Override to be On."
        ),
        "0x10CF4125": (
            "Sets the target output frame rate for DLSS Frame Generation in Dynamic mode. "
            "The driver adjusts the MFG multiplier in real time to try to hit this FPS. "
            "0 disables it. Specify in frames per second as a decimal integer: "
            "60 = 0x3C, 120 = 0x78, 144 = 0x90, 240 = 0xF0. "
            "Special value 0x1000000 = automatic (driver picks the target). "
            "Only meaningful when DLSSG Mode is set to 'Dynamic'."
        ),
        "0x10562D0F": (
            "Maximum number of generated frames DLSS Frame Generation can insert per "
            "rendered frame when operating in Dynamic mode. Acts as a ceiling to prevent "
            "excessive latency even if the dynamic algorithm would otherwise go higher. "
            "Values per the dxvk-nvapi wiki: 0 = off, 1 = up to 2x ... 5 = up to 6x. "
            "Only relevant when DLSSG Mode is 'Dynamic'."
        ),
        "0x10AFB76C": (
            "Forces DLSS Super Resolution into Ultra Performance mode at the driver level, "
            "bypassing the in-game preset menu. Ultra Performance renders at ~33% of "
            "output resolution — the most aggressive upscaling ratio — providing the "
            "largest frame rate boost at the cost of image quality. "
            "Useful on lower-end RTX cards at 4K output when DLAA/Quality would be "
            "too slow. 'None' leaves mode selection to the game."
        ),
        "0x10444444": (
            "Enables NVIDIA Image Scaling (NIS), NVIDIA's older spatial upscaler that "
            "does not require Tensor cores. NIS uses an adaptive sharpening + upscaling "
            "algorithm and works on all Turing and later GPUs. It is less accurate than "
            "DLSS Super Resolution but has near-zero latency overhead. "
            "Note: on Linux/Proton, NIS is typically configured per-game inside "
            "NVIDIA Control Panel or via game settings rather than this DRS flag."
        ),
        "0x10E41E04": (
            "Master override switch for DLSS Noise Reduction (DLSS-NR), an AI-based "
            "denoiser primarily aimed at ray-traced effects in titles that expose the "
            "NGX NR interface. When 'On', preset and mode settings for NR take effect. "
            "This is a newer NGX feature; availability depends on game and driver version."
        ),
        "0x10E41E06": (
            "SL_DLSS_OVERRIDE (0x10E41E06). Enables the driver's override path for games "
            "that integrate DLSS through NVIDIA Streamline rather than calling NGX "
            "directly. Named key in dxvk-nvapi: DXVK_NVAPI_DRS_SL_DLSS_OVERRIDE=on. "
            "Pair with the SR/RR/FG override switches and PROTON_ENABLE_NGX_UPDATER=1 "
            "when forcing newer presets in Streamline titles."
        ),
        "0x10E41DF8": (
            "Selects the neural network preset for DLSS Noise Reduction. "
            "NVIDIA's header only defines presets A through D plus Latest for NR "
            "(unlike SR/RR, which go up to O). "
            "See DLSS-SR Preset for descriptions of preset characteristics. "
            "Recommended: 'Latest' to always use NVIDIA's current best NR model."
        ),

        # ── VRR / G-Sync ──────────────────────────────────────────────────────
        "0x1194F158": (
            "Global G-SYNC / VRR enable switch that applies to all applications. "
            "'Disabled' — VRR is completely off; monitor runs at fixed refresh rate. "
            "'Fullscreen' — VRR only active when the application is in exclusive "
            "fullscreen mode. Most compatible and lowest overhead. "
            "'Full+Window' — extends VRR to borderless/windowed applications. "
            "Requires a G-SYNC compatible or native G-SYNC display. "
            "On Wayland/Proton, VRR state is also influenced by the compositor "
            "(KDE Plasma → Force fullscreen repaint or allow VRR per-window)."
        ),
        "0x1094F1F7": (
            "Specifies which VRR modes an application is permitted to request from the driver. "
            "This is the capability advertisement; the actual behavior also depends on "
            "G-Sync/VRR Mode above. "
            "'Fullscreen' — apps can only activate VRR in exclusive fullscreen. "
            "'Full+Window' — apps can request VRR in borderless/windowed modes too. "
            "In most cases this should match the global G-Sync/VRR Mode setting."
        ),
        "0x10A879CE": (
            "Per-application VRR enable/disable switch. "
            "'Enable' — allows this application to use VRR when the global setting "
            "and monitor capability permit it. "
            "'Disable' — forces fixed refresh rate for this application even if VRR "
            "is globally on (useful for locked-framerate competitive titles). "
            "'Not Supported' — marks the app as VRR-incompatible at the API level."
        ),
        "0x00A879CF": (
            "Advanced VSync presentation mode control that interacts with VRR. "
            "'Passive' (0x60925292) — standard behavior; VSync state controlled by "
            "application and global settings. "
            "'Force Off' — disables VSync regardless of app request (max FPS, tearing possible). "
            "'Force On' — always enables VSync (capped to refresh rate, no tearing). "
            "'Flip 2/3/4' — controls flip queue depth (number of queued frames). Deeper "
            "queues smooth out CPU/GPU spikes at the cost of extra latency. "
            "'Virtual' (0x18888888) — virtual VSync mode for special presentation paths."
        ),
        "0x10A879CF": (
            "Per-application G-SYNC mode override. "
            "'Allow' (default) — G-SYNC operates according to global settings. "
            "'Force Off' — disables G-SYNC for this app. "
            "'Disallow' — prevents the app from requesting G-SYNC. "
            "'ULMB' — Ultra Low Motion Blur mode (strobed backlight, incompatible with VRR). "
            "'Fixed Ref' — fixed refresh rate reference mode. "
            "ULMB requires a G-SYNC native (not compatible) monitor."
        ),

        # ── VSync / Flip ──────────────────────────────────────────────────────
        "0x005A375C": (
            "Top-level control for screen tearing behavior. "
            "'Disable tearing' (0x96861077) — enables VSync/flip synchronisation to "
            "prevent torn frames. Default for most applications. "
            "'Enable tearing' (0x99941284) — forces immediate presentation with no "
            "synchronisation, allowing the display to show partial frames from "
            "different render cycles. Minimizes latency but produces visible tearing. "
            "Override this at the per-app level alongside G-Sync settings for "
            "latency-critical titles."
        ),

        # ── Frame Rate / Latency ──────────────────────────────────────────────
        "0x007BA09E": (
            "Maximum number of frames the CPU is allowed to queue ahead of the GPU. "
            "Lower values (1–2) reduce input latency because the CPU cannot race too far "
            "ahead of the GPU. Higher values (3+) can smooth out frame time variance "
            "at the cost of increased latency. "
            "0 = driver default (typically 3). "
            "NVIDIA Reflex supersedes this setting for latency reduction in supported games; "
            "if using Reflex, leave Pre-Render Limit at its default."
        ),
        "0x10835002": (
            "Hard frame rate cap applied at the driver's flip queue level. "
            "The limiter fires before the frame is queued for presentation, so it "
            "removes idle GPU spinning and reduces power/heat. "
            "Common values: 30=0x1E, 60=0x3C, 120=0x78, 144=0x90, 165=0xA5, 240=0xF0. "
            "0 = no limit. "
            "Prefer an in-game limiter or NVIDIA Reflex's built-in cap for the most "
            "latency-friendly implementation; use this DRS limiter as a fallback."
        ),
        "0x10835016": (
            "Frame rate cap applied when the application loses focus (e.g. alt-tabbed "
            "or minimized). Prevents the GPU from running at full speed while the game "
            "window is not visible, saving power and thermal headroom. "
            "Default is 20 FPS (0x14). "
            "Common values: 20=0x14, 30=0x1E, 60=0x3C. "
            "Set to a low value like 20–30 FPS for laptops on battery to conserve energy."
        ),
        "0x10835017": (
            "Number of seconds the application must be in the background (unfocused) "
            "before the Idle FPS Limit kicks in. "
            "Default is 3 seconds. Increase if you use alt-tab frequently and find the "
            "FPS drop too jarring when quickly switching back."
        ),
        "0x1095F170": (
            "When enabled, the driver automatically aligns the NVIDIA FrameView / "
            "Reflex latency flash trigger to the correct render event. "
            "Leave Enabled unless you are manually calibrating a latency measurement "
            "setup and need precise manual alignment."
        ),

        # ── Power / Performance ───────────────────────────────────────────────
        "0x1057EB71": (
            "Controls the GPU power management policy. "
            "'Adaptive' — clocks scale with workload; best balance of performance and power. "
            "'Prefer Max' — boosts clocks as high as possible; maximum performance but "
            "higher power draw and temperatures. "
            "'Driver Ctrl' — driver manages power state autonomously. "
            "'Consistent Perf' — minimizes clock variation for repeatable benchmarks. "
            "'Prefer Min' — aggressively clocks down; minimum power, lowest performance. "
            "'Optimal Power' (default, 0x5) — NVIDIA's recommended mode; efficient "
            "performance scaling. On the Alienware M16 R1, 'Prefer Max' paired with a "
            "custom Curve Optimizer may improve 1% lows under sustained load."
        ),
        "0x10D1EF29": (
            "Sets the maximum GPU power limit in watts at the driver level. "
            "Range 0–175 W. 0 = use the GPU's built-in default TDP. "
            "Lowering this value can reduce temperatures and fan noise on thermally "
            "constrained systems (e.g. laptops). "
            "For the RTX 4080 Mobile the factory TGP is typically 60–150 W depending "
            "on the OEM configuration; setting a value below the base TGP will throttle "
            "GPU clocks. Pair with Ryzenadj for coordinated CPU+GPU power budgeting."
        ),
        "0x00AE785C": (
            "Controls whether the GPU throttles its power draw to comply with the PCIe "
            "slot power specification (typically 75 W for standard slots). "
            "'Off' — no PCIe slot throttling; the GPU may exceed slot limits, relying "
            "on supplemental PCIe power connectors. Normal for desktop GPUs. "
            "'On' — enforces slot power compliance; relevant for small-form-factor or "
            "riser-connected configurations."
        ),

        # ── Shader Cache ──────────────────────────────────────────────────────
        "0x00198FFF": (
            "Enables the driver's on-disk shader cache. When enabled, compiled GPU "
            "shader programs are stored to disk so they can be reused on subsequent "
            "launches without recompilation. Disabling this forces the driver to "
            "recompile shaders from scratch every launch, causing stuttering the first "
            "time each shader is used. Leave Enabled unless diagnosing a shader cache "
            "corruption or disk space issue."
        ),
        "0x00AC8497": (
            "Maximum size of the driver shader disk cache in megabytes. "
            "Once the cache reaches this size, older entries are evicted. "
            "The default is 16384 MB (16 GB). For heavy workloads (multiple large games, "
            "content creation), 32768 MB (32 GB) prevents premature eviction. "
            "If disk space is limited, reduce to 4096 MB. "
            "The cache is typically stored under "
            "%LOCALAPPDATA%/NVIDIA/DXCache or ~/.nv/ComputeCache on Linux."
        ),

        # ── Ambient Occlusion ─────────────────────────────────────────────────
        "0x00667329": (
            "Controls the quality of NVIDIA's driver-injected Ambient Occlusion. "
            "AO darkens crevices, corners, and surfaces near occluders to add contact "
            "shadows and depth. The driver AO is a post-process approximation. "
            "'Off' — no driver AO (use the game's built-in AO instead). "
            "'Low/Medium/High' — increasing quality at increasing cost. "
            "For modern titles with HBAO+ or RTAO built-in, set to 'Off' to avoid "
            "double-AO artifacts."
        ),
        "0x00664339": (
            "Per-application enable switch for driver-level Ambient Occlusion. "
            "When Disabled, the AO Mode setting above is ignored for this application. "
            "Enable only for older DX9/DX10/OpenGL games that lack built-in AO."
        ),

        # ── FXAA ──────────────────────────────────────────────────────────────
        "0x1034CB89": (
            "Controls whether FXAA (Fast Approximate Anti-Aliasing) is permitted to "
            "activate for this application. 'Allowed' means FXAA can be enabled by "
            "the FXAA Enable setting below; 'Disallowed' prevents FXAA even if "
            "globally requested. Use 'Disallowed' for games that have their own "
            "temporal AA or DLSS where FXAA would add unwanted blur."
        ),
        "0x1074C972": (
            "Enables or disables NVIDIA FXAA post-process anti-aliasing. FXAA is a "
            "screen-space, single-pass AA technique with very low performance cost. "
            "It smooths jagged edges by blending neighboring pixels along contrast "
            "boundaries. Quality is lower than MSAA or DLSS but it works on any game "
            "regardless of engine support. "
            "On modern titles with temporal AA or DLSS, FXAA often adds unwanted "
            "softness — disable it in those cases."
        ),

        # ── Ansel ─────────────────────────────────────────────────────────────
        "0x1035DB89": (
            "Controls whether NVIDIA Ansel (the driver-level screenshot tool) is "
            "permitted to activate in this application. 'Allowed' lets Ansel intercept "
            "the Alt+F2 shortcut to enter free-camera screenshot mode. "
            "'Disallowed' prevents Ansel from loading into the process entirely. "
            "Disallow if you experience game crashes or anti-cheat conflicts related "
            "to Ansel's injection."
        ),
        "0x1075D972": (
            "Globally enables or disables NVIDIA Ansel. When off, the Ansel overlay "
            "is not loaded regardless of the 'Ansel Allow' setting. "
            "Disable system-wide if you use a different screenshot tool (e.g. Fraps, "
            "OBS, Reshade) and don't want Ansel to interfere."
        ),

        # ── SLI ────────────────────────────────────────────────────────────────
        "0x1033CED1": (
            "Sets the SLI (multi-GPU) rendering mode for the application. "
            "'Autoselect' — driver picks the best SLI mode. "
            "'Force Single' — disables SLI, uses only the primary GPU. "
            "'Force AFR' — Alternate Frame Rendering: GPUs alternate rendering complete frames. "
            "'Force AFR2' — AFR with 2-frame offset for better overlap. "
            "'Force SFR' — Split Frame Rendering: each GPU renders a horizontal band. "
            "'AFR of SFR' — AFR applied to SFR pairs. "
            "Note: SLI is deprecated on RTX 30/40 series; these settings are only "
            "relevant on legacy Maxwell/Pascal/Turing multi-GPU configurations."
        ),

        # ── Optimus ────────────────────────────────────────────────────────────
        "0x10F9DC81": (
            "Selects which GPU handles rendering on NVIDIA Optimus (hybrid graphics) laptops. "
            "'Integrated' — force iGPU rendering (power saving). "
            "'Enable' — use the discrete NVIDIA GPU. "
            "'Auto Select' (default) — driver decides based on application workload. "
            "'Override' (0x80000000) — allow user-level profile override. "
            "On the Alienware M16 R1 AMD, the AMD iGPU handles display output while the "
            "RTX 4080 Mobile renders; this setting has limited effect on that platform "
            "compared to AMD-specific MUX/switchable graphics controls."
        ),
        "0x10F9DC84": (
            "Advanced bitfield controlling Optimus rendering pipeline options. "
            "Individual bits enable/disable specific behaviors: "
            "'Disable Async Present' — prevents asynchronous frame presentation (fixes "
            "some tearing issues on Optimus). "
            "'Enable DWM Async' — allows async presentation through DWM compositor. "
            "'Disable DXGI Wrappers' — bypasses DXGI interop wrappers (can help "
            "compatibility with custom D3D12 present paths). "
            "'Prune Unsupported' — removes unsupported resource formats from the list. "
            "'iGPU Transcoding' — uses the iGPU to transcode Optimus blit operations. "
            "Leave at 0 (default) unless you are diagnosing a specific Optimus issue."
        ),

        # ── Misc ───────────────────────────────────────────────────────────────
        "0x108F0841": (
            "Allows applications (profilers, benchmark tools) to read GPU hardware "
            "performance counters via NVAPI. When enabled, counters such as shader "
            "utilization, memory bandwidth, and occupancy are accessible to user-space "
            "profiling tools. Has no effect on rendering; disable if you want to prevent "
            "third-party tools from reading detailed GPU metrics."
        ),
        "0x0098C1AC": (
            "Enables MFAA (Multi-Frame Anti-Aliasing), an NVIDIA technique that "
            "distributes MSAA sample positions temporally across frames. MFAA provides "
            "similar visual quality to 4x MSAA at roughly 2x MSAA cost. "
            "Only effective in DX11 and OpenGL; has no effect in DX12/Vulkan titles. "
            "Combine with AA Mode 'Override' or 'Enhance' and an MSAA level for best results."
        ),
        "0x107CDDBC": (
            "Associates this driver profile with a specific Steam application ID. "
            "The value corresponds to the numeric Steam App ID (appid) found in the "
            "game's Steam store URL. When set, NVAPI will match the profile to the "
            "correct game even if the executable name is ambiguous. "
            "Example: Cyberpunk 2077 = 0x00124B6D (1203220 decimal). "
            "Leave at 0x0 for non-Steam titles or when matching by executable name."
        ),
    }

    # Apply detailed descriptions to settings by ID
    for s in settings:
        if s.id in DETAILED_DESCS:
            s.detailed_desc = DETAILED_DESCS[s.id]

    return settings


# Built once at import time and shared everywhere. create_all_settings()
# builds ~117 dataclass instances from scratch; it was previously being
# called twice (once in SettingsManager.__init__, once in MainWindow.__init__)
# for no reason other than duplicated wiring.
ALL_SETTINGS: List[Setting] = create_all_settings()


# ============================================================================
# Settings Manager
# ============================================================================

class SettingsManager(QObject):
    settings_changed = Signal()
    arch_changed = Signal()
    profile_loaded = Signal(str)
    # Fired only when the profile *list itself* (names/current) changes —
    # save/load/delete. Previously ProfileManagerWidget rebuilt its whole
    # list on every settings_changed, i.e. on every single DRS setting edit,
    # even though the set of saved profiles hadn't changed at all.
    profiles_changed = Signal()
    profile_save_error = Signal(str)
    # Fired after profiles.json was (re)written or (re)read by us; the external-change watcher uses it.
    disk_written = Signal()
    # Emitted after a profile was written successfully (drives the
    # "unsaved changes" indicator back to clean).
    profile_saved = Signal(str)

    def __init__(self):
        super().__init__()
        self._settings: Dict[str, str] = {}
        self._arch: Optional[GPUArch] = None
        self._profiles: Dict[str, Dict] = {}
        self._current_profile: Optional[str] = None
        self._loaded_env_vars: Dict[str, str] = {}
        self._loaded_gamescope_enabled = False
        self._loaded_gamescope_flags: Dict[str, str] = {}
        self._all_settings = ALL_SETTINGS
        # profile_save_error fires from the constructor before MainWindow has
        # connected it, so the message would be lost; MainWindow re-emits this.
        self.startup_notice: str = ""
        self._load_profiles()

    def get_setting(self, setting_id: str) -> Optional[str]:
        return self._settings.get(setting_id)

    def get_settings_list(self) -> Dict[str, str]:
        return self._settings.copy()

    def set_setting(self, setting_id: str, value: str):
        self._settings[setting_id] = value
        self.settings_changed.emit()

    def remove_setting(self, setting_id: str):
        if setting_id in self._settings:
            del self._settings[setting_id]
            self.settings_changed.emit()

    def clear_all(self):
        self._settings.clear()
        self.settings_changed.emit()

    def reset_everything(self):
        """Clear DRS settings, GPU arch, and the active-profile marker in one
        go. Used by the 'Reset All' button. Replaces the previous pattern of
        calling clear_all() + set_arch(None) separately (which fired
        settings_changed twice) and reaching into the private
        self._current_profile field directly from OutputBarWidget."""
        self._settings.clear()
        self._arch = None
        self._current_profile = None
        self.arch_changed.emit()
        self.settings_changed.emit()
        self.profiles_changed.emit()  # "current profile" highlight must clear too

    def get_arch(self) -> Optional[GPUArch]:
        return self._arch

    def set_arch(self, arch: Optional[GPUArch]):
        self._arch = arch
        self.arch_changed.emit()
        self.settings_changed.emit()

    def get_settings_string(self) -> str:
        pairs = [f"{k}={v}" for k, v in self._settings.items() if v is not None]
        return ",".join(pairs)

    def get_full_env_string(self) -> str:
        parts = []
        if self._arch:
            parts.append(f"DXVK_NVAPI_GPU_ARCH={self._arch.code}")
        settings_str = self.get_settings_string()
        if settings_str:
            parts.append(f"DXVK_NVAPI_DRS_SETTINGS={settings_str}")
        return " ".join(parts)

    def get_profiles(self) -> Dict[str, Dict]:
        return self._profiles

    def get_current_profile(self) -> Optional[str]:
        return self._current_profile

    def save_profile(self, name: str, env_vars: Optional[Dict] = None,
                     gamescope_enabled: bool = False,
                     gamescope_flags: Optional[Dict[str, str]] = None):
        self._profiles[name] = {
            "settings": self._settings.copy(),
            "arch": self._arch.code if self._arch else None,
            "env_vars": env_vars.copy() if env_vars else {},
            "gamescope_enabled": gamescope_enabled,
            "gamescope_flags": gamescope_flags.copy() if gamescope_flags else {},
        }
        self._current_profile = name
        ok = self._save_profiles()
        self.profiles_changed.emit()
        if ok:
            self.profile_saved.emit(name)

    def load_profile(self, name: str):
        if name not in self._profiles:
            return
        profile = self._profiles[name]
        if not isinstance(profile, dict):
            # Hand-edited/corrupt entry — refuse to load rather than crash
            # on profile.get(...) below.
            self.profile_save_error.emit(
                f'Profile "{name}" is malformed in profiles.json and cannot be loaded.'
            )
            return
        self._settings = _as_dict(profile.get("settings"))
        arch_code = profile.get("arch")
        if arch_code:
            self._arch = next((a for a in GPU_ARCHS if a.code == arch_code), None)
        else:
            self._arch = None
        self._current_profile = name
        self._loaded_env_vars = _as_dict(profile.get("env_vars"))
        self._loaded_gamescope_enabled = bool(profile.get("gamescope_enabled", False))
        self._loaded_gamescope_flags = _as_dict(profile.get("gamescope_flags"))
        self.profile_loaded.emit(name)
        self.settings_changed.emit()
        self.arch_changed.emit()

    def get_loaded_env_vars(self) -> Dict:
        """Returns env vars from the last loaded profile."""
        return self._loaded_env_vars

    def get_loaded_gamescope_enabled(self) -> bool:
        """Returns gamescope enabled state from the last loaded profile."""
        return self._loaded_gamescope_enabled

    def get_loaded_gamescope_flags(self) -> Dict[str, str]:
        """Returns gamescope flag values from the last loaded profile."""
        return self._loaded_gamescope_flags

    def delete_profile(self, name: str):
        if name in self._profiles:
            del self._profiles[name]
            if self._current_profile == name:
                self._current_profile = None
            self._save_profiles()
            self.profiles_changed.emit()

    # ── Profile file location ────────────────────────────────────────────────
    # Moved to XDG_CONFIG_HOME to match the other tools in this toolbox
    # (ryzenadj_gui.py etc. all use ~/.config/<tool>/...), instead of a
    # dotfile directly in $HOME. Old installs are migrated once, in-place.
    _OLD_PROFILES_PATH = Path.home() / ".drs_configurator_profiles.json"

    @staticmethod
    def _profiles_path() -> Path:
        config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "drstool"
        return config_dir / "profiles.json"

    def _load_profiles(self):
        data_file = self._profiles_path()
        try:
            if not data_file.exists() and self._OLD_PROFILES_PATH.exists():
                # One-time migration from the old ~/.drs_configurator_profiles.json
                data_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._OLD_PROFILES_PATH, "r", encoding="utf-8") as f:
                    legacy = json.load(f)
                # A top-level non-dict (e.g. a list from a truncated/edited
                # file) would crash every `for name in profiles` later —
                # validate before adopting it.
                if not isinstance(legacy, dict):
                    raise ValueError("legacy profiles file is not a JSON object")
                self._profiles = legacy
                self._save_profiles()
                return
            if data_file.exists():
                with open(data_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    raise ValueError("profiles file is not a JSON object")
                self._profiles = loaded
        except Exception:
            # Corrupt or unreadable file. Starting with an empty in-memory set
            # is fine, but the very next save_profile() would rewrite the file
            # and destroy whatever was recoverable in it — so move it aside
            # first instead of merely leaving it "untouched".
            moved = ""
            try:
                if data_file.exists():
                    bad = data_file.with_name(
                        f"profiles.json.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
                    os.replace(data_file, bad)
                    moved = f" The unreadable file was kept as {bad.name}."
            except OSError:
                pass
            self._profiles = {}
            msg = (f"Could not read profiles from {data_file} — starting with no "
                   f"profiles this session.{moved}")
            self.startup_notice = msg
            self.profile_save_error.emit(msg)

    # ── Profile history (snapshots of the state each save replaces) ──────────
    _HISTORY_KEEP = 60

    @staticmethod
    def _history_dir() -> Path:
        return SettingsManager._profiles_path().parent / "history"

    def list_snapshots(self) -> List[Path]:
        try:
            return sorted(self._history_dir().glob("profiles-*.json"), reverse=True)
        except OSError:
            return []

    def _snapshot_old(self, data_file: Path, new_text: str):
        """Keep the file we are about to overwrite, unless nothing actually changes."""
        try:
            if not data_file.exists():
                return
            old = data_file.read_bytes()
            if not old.strip() or old == new_text.encode("utf-8"):
                return
            hd = self._history_dir()
            hd.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            (hd / f"profiles-{stamp}.json").write_bytes(old)
            for p in sorted(hd.glob("profiles-*.json"))[:-self._HISTORY_KEEP]:
                p.unlink(missing_ok=True)
        except OSError:
            pass

    def restore_profile(self, name: str, data) -> bool:
        if not isinstance(data, dict):
            return False
        self._profiles[name] = json.loads(json.dumps(data))
        ok = self._save_profiles()
        self.profiles_changed.emit()
        return ok

    def restore_all_profiles(self, profiles: Dict) -> bool:
        if not isinstance(profiles, dict):
            return False
        self._profiles = json.loads(json.dumps(profiles))
        if self._current_profile not in self._profiles:
            self._current_profile = None
        ok = self._save_profiles()
        self.profiles_changed.emit()
        return ok

    def reload_from_disk(self, merge: bool) -> str:
        """Adopt an externally modified profiles.json. merge=True keeps our profiles on
        name conflicts and only adds the ones that exist solely on disk."""
        data_file = self._profiles_path()
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if not isinstance(disk, dict):
                raise ValueError("not a JSON object")
        except Exception as ex:
            self.disk_written.emit()
            return f"profiles.json on disk is unreadable ({ex}) — kept the in-memory profiles."
        if merge:
            added = [n for n in disk if n not in self._profiles]
            clash = [n for n in disk if n in self._profiles and disk[n] != self._profiles[n]]
            for n in added:
                self._profiles[n] = disk[n]
            ok = self._save_profiles()
            self.profiles_changed.emit()
            return (f"Merged: {len(added)} profile(s) added from disk, "
                    f"{len(clash)} differing one(s) kept as in DRSTool." + ("" if ok else " (save failed)"))
        self._profiles = disk
        if self._current_profile not in self._profiles:
            self._current_profile = None
        self.disk_written.emit()
        self.profiles_changed.emit()
        return f"Reloaded {len(disk)} profile(s) from disk."

    def _save_profiles(self) -> bool:
        """Write profiles to disk atomically. Returns True on success."""
        data_file = self._profiles_path()
        try:
            data_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = data_file.with_suffix(".json.tmp")
            text = json.dumps(self._profiles, indent=2)
            with open(tmp_file, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            self._snapshot_old(data_file, text)
            os.replace(tmp_file, data_file)  # atomic on POSIX — no partial-write corruption
            self.disk_written.emit()
            return True
        except Exception as e:
            self.profile_save_error.emit(f"Failed to save profiles to {data_file}: {e}")
            return False


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
        self.setStyleSheet(f"#outputBar {{ background:{T['surface']}; border-bottom:1px solid {T['border']}; }}")

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(12)

        lbl_ss = (f"font-family:{T['mono']}; font-size:11px; color:{T['accent']};"
                  " font-weight:600; background:transparent;")
        box_ss = (f"QLabel{{ background:{T['bg']}; border:1px solid {T['border_hi']};"
                  f" border-radius:4px; padding:3px 8px; color:{T['text']};"
                  f" font-family:{T['mono']}; font-size:12px; }}"
                  f"QLabel:hover{{ border-color:{T['accent_dim']}; }}")

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        grid.setContentsMargins(0, 0, 0, 0)

        def key(text):
            k = QLabel(text)
            k.setStyleSheet(lbl_ss)
            k.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return k

        def val(text):
            v = ElidedLabel(text)
            v.setStyleSheet(box_ss)
            v.copied.connect(lambda _t: self._show_feedback("Copied"))
            return v

        # Row 0: GPU arch + DRS settings share a row
        grid.addWidget(key("DXVK_NVAPI_GPU_ARCH="), 0, 0)
        arch_row = QHBoxLayout()
        arch_row.setSpacing(8)
        self._arch_value = val("not set")
        self._arch_value.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._arch_value.setFixedWidth(84)
        arch_row.addWidget(self._arch_value)
        arch_row.addWidget(key("DXVK_NVAPI_DRS_SETTINGS="))
        self._settings_value = val("none")
        arch_row.addWidget(self._settings_value, 1)
        grid.addLayout(arch_row, 0, 1)

        grid.addWidget(key("ENV VARS="), 1, 0)
        self._env_value = val("none")
        grid.addWidget(self._env_value, 1, 1)

        grid.addWidget(key("GAMESCOPE="), 2, 0)
        self._gamescope_value = val("disabled")
        grid.addWidget(self._gamescope_value, 2, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid, 1)

        # ── Action column ────────────────────────────────────────────────
        col = QVBoxLayout()
        col.setSpacing(5)
        col.setContentsMargins(0, 0, 0, 0)

        btn_base = ("border-radius:4px; font-weight:600; font-size:12px;"
                    " min-height:24px; padding:0 10px;")
        self._btn_ss_primary = (
            f"QToolButton,QPushButton{{background:{T['accent']};border:1px solid {T['accent']};"
            f"color:#0b0d10;{btn_base}}}"
            f"QToolButton:hover,QPushButton:hover{{background:{T['accent_hi']};}}"
            "QToolButton::menu-button{border-left:1px solid rgba(0,0,0,0.25);width:18px;}"
        )
        self._btn_ss_quiet = (
            f"QPushButton{{background:{T['raised']};border:1px solid {T['border_hi']};"
            f"color:{T['text2']};{btn_base}}}"
            f"QPushButton:hover{{border-color:{T['accent_dim']};color:{T['text']};}}"
            f"QPushButton:disabled{{color:#4b5264;border-color:{T['border']};}}"
        )
        self._btn_ss_dirty = (
            f"QPushButton{{background:rgba(210,160,60,0.14);border:1px solid {T['warn']};"
            f"color:{T['warn']};{btn_base}}}"
            f"QPushButton:hover{{background:rgba(210,160,60,0.24);}}"
        )
        btn_ss_danger = (
            f"QPushButton{{background:transparent;border:1px solid {T['border_hi']};"
            f"color:{T['danger']};{btn_base}}}"
            f"QPushButton:hover{{border-color:{T['danger']};background:rgba(208,80,80,0.10);}}"
        )

        copy_btn = QToolButton()
        copy_btn.setText("Copy all")
        copy_btn.setToolTip("Copy the full launch string  (Ctrl+Shift+C)")
        copy_btn.setPopupMode(QToolButton.MenuButtonPopup)
        copy_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        copy_btn.setStyleSheet(self._btn_ss_primary)
        copy_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        menu = QMenu(copy_btn)
        act_plain = QAction("Copy as shell environment", menu)
        act_plain.triggered.connect(lambda: self._copy_all(steam=False))
        act_steam = QAction("Copy as Steam launch options  (… %command%)", menu)
        act_steam.triggered.connect(lambda: self._copy_all(steam=True))
        menu.addAction(act_plain)
        menu.addAction(act_steam)
        copy_btn.setMenu(menu)
        copy_btn.clicked.connect(lambda: self._copy_all(steam=False))
        col.addWidget(copy_btn)

        steam_btn = QPushButton("Steam line")
        steam_btn.setStyleSheet(self._btn_ss_quiet)
        steam_btn.setToolTip("Copy  env … gamescope … -- %command%  for Steam's launch options")
        steam_btn.clicked.connect(self._copy_steam_env)
        col.addWidget(steam_btn)

        self._save_profile_btn = QPushButton("Save")
        self._save_profile_btn.setStyleSheet(self._btn_ss_quiet)
        self._save_profile_btn.setEnabled(False)
        self._save_profile_btn.clicked.connect(self._save_to_profile)
        col.addWidget(self._save_profile_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.setStyleSheet(btn_ss_danger)
        reset_btn.setToolTip("Clear every DRS setting, the GPU arch, env vars and gamescope flags")
        reset_btn.clicked.connect(self._reset_all)
        col.addWidget(reset_btn)

        holder = QWidget()
        holder.setLayout(col)
        holder.setFixedWidth(128)
        root.addWidget(holder, 0, Qt.AlignTop)

        self.settings_manager.settings_changed.connect(self._update)
        self.settings_manager.arch_changed.connect(self._update)
        self.settings_manager.profile_loaded.connect(self._on_profile_loaded)
        self.settings_manager.profiles_changed.connect(self._update)
        self._update()

    def set_dirty(self, dirty: bool):
        self._dirty = dirty
        self._update()

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
            "selection, and clear all DXVK/VKD3D-Proton env vars.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.settings_manager.reset_everything()
            win = self.window()
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


# ============================================================================
# Setting Editor - Modern Card View
# ============================================================================

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
    control_layout.setContentsMargins(0, 6, 0, 0)
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
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(10)

        self._name_label = QLabel()
        self._name_label.setStyleSheet("""
QLabel{
    color:#f2f2f2;
    font-size:18px;
    font-weight:700;
}
""")
        self._name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._name_label, 1)

        self._id_label = QLabel()
        self._id_label.setStyleSheet("""
QLabel{
    background:#10141c;
    border:1px solid #313a48;
    border-radius:6px;
    padding:4px 10px;
    color:#8ea0ba;
    font-family:monospace;
    font-size:11px;
}
""")
        self._id_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._id_label)

        self._value_label = QLabel()
        self._value_label.setStyleSheet("""
QLabel{
    background:#152013;
    border:1px solid #76b900;
    border-radius:6px;
    padding:4px 10px;
    color:#9be238;
    font-family:monospace;
    font-size:11px;
    font-weight:600;
}
""")
        self._value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._value_label)
        self._value_label.hide()

        layout.addLayout(header)

        self._desc_label = QLabel()
        self._desc_label.setStyleSheet("""
QLabel{
    color:#a7afbc;
    font-size:13px;
    line-height:140%;
}
""")
        self._desc_label.setWordWrap(True)
        self._desc_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # Word-wrapped QLabels don't reliably report a correct heightForWidth
        # to the surrounding QVBoxLayout on first layout pass (Qt has to know
        # the label's actual width before it can compute how many lines a
        # long detailed_desc wraps to). Left at the default Preferred/Preferred
        # policy, long descriptions could end up laid out with too little
        # vertical space reserved, so the control area below it (added right
        # after) would visually overlap the last line or two of text.
        # Minimum vertical policy + explicit resize-event driven rewrap fixes
        # that: see _resize_desc_label below.
        self._desc_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self._desc_label)

        self._control_widget, self._control_layout, control_scroll = _make_scrollable_control_area()
        layout.addWidget(control_scroll, 1)  # stretch=1: absorb all extra space, keep header/desc/button fixed

        remove_layout = QHBoxLayout()
        remove_layout.addStretch()
        self._remove_btn = QPushButton("Remove Setting")
        self._remove_btn.clicked.connect(self._remove_setting)
        self._remove_btn.setStyleSheet("""
QPushButton{
    background:#b93b3b;
    border:1px solid #e45d5d;
    border-radius:5px;
    color:white;
    padding:4px 16px;
    font-weight:600;
    font-size:12px;
}
QPushButton:hover{
    background:#ca4545;
}
QPushButton:pressed{
    background:#a73434;
}
""")
        remove_layout.addWidget(self._remove_btn)
        layout.addLayout(remove_layout)
        self._remove_btn.hide()

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
        group = QButtonGroup(self)
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

            btn.setStyleSheet("""
QPushButton{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:5px;
    font-size:11px;
    font-weight:600;
}
QPushButton:hover{
    background:#252c37;
    border:1px solid #76b900;
}
QPushButton:checked{
    background:rgba(118, 185, 0, 0.12);
    border:1px solid #76b900;
    color:#76b900;
}
""")

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
    background:#252c37;
    border:1px solid #76b900;
}
""")
            else:
                btn.setStyleSheet("""
QPushButton{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:5px;
    font-size:11px;
    font-weight:600;
}
QPushButton:hover{
    background:#252c37;
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
        if cur is not None:
            spin.setValue(int(cur, 16) if cur.startswith("0x") else int(cur))
        else:
            spin.setValue(int(s.default, 16) if s.default.startswith("0x") else int(s.default))
        spin.setFixedHeight(30)
        spin.setStyleSheet("""
QSpinBox{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:3px 8px;
    font-size:12px;
}
QSpinBox:focus{
    border:1px solid #76b900;
}
QSpinBox:hover{
    background:#252c37;
}
""")
        spin.valueChanged.connect(lambda v: self._set_numeric(s.id, v))
        hbox.addWidget(spin)

        range_label = QLabel(f"Range: {s.min}–{s.max}")
        range_label.setStyleSheet("font-family: monospace; font-size: 11px; color: #8a92a5;")
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
        dec_label.setStyleSheet("font-size: 11px; color: #8a92a5;")
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
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:3px 8px;
    font-size:12px;
}
QSpinBox:focus{
    border:1px solid #76b900;
}
QSpinBox:hover{
    background:#252c37;
}
""")
        dec_spin.valueChanged.connect(lambda v: self._set_dec_hex(s.id, v))
        grid.addWidget(dec_spin, 0, 1)

        hex_label = QLabel("Hex: 0x")
        hex_label.setStyleSheet("font-size: 11px; color: #8a92a5;")
        grid.addWidget(hex_label, 0, 2)

        hex_edit = QLineEdit()
        if cur is not None:
            hex_edit.setText(cur.replace("0x", "").upper())
        else:
            hex_edit.setText("0")
        hex_edit.setFixedHeight(30)
        hex_edit.setStyleSheet("""
QLineEdit{
    background:#1a1f28;
    border:1px solid #323c4b;
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
    background:#252c37;
}
""")
        hex_edit.textChanged.connect(lambda t: self._set_hex_from_edit(s.id, t))
        grid.addWidget(hex_edit, 0, 3)

        self._control_layout.addLayout(grid)

    def _build_bitfield_control(self, s: Setting, cur: Optional[str]):
        cur_val = int(cur, 16) if cur else 0

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
    background:#252c37;
    border:1px solid #76b900;
}
""")
            else:
                btn.setStyleSheet("""
QPushButton{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:3px 5px;
    font-size:10px;
    font-family:monospace;
    min-height:30px;
}
QPushButton:hover{
    background:#252c37;
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
        calc_label.setStyleSheet("font-size: 11px; color: #8a92a5; font-family: monospace;")
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
        hex_label.setStyleSheet("font-size: 11px; color: #8a92a5;")
        hbox.addWidget(hex_label)

        hex_edit = QLineEdit()
        if cur is not None:
            hex_edit.setText(cur.replace("0x", "").upper())
        else:
            hex_edit.setText("0")
        hex_edit.setFixedHeight(30)
        hex_edit.setStyleSheet("""
QLineEdit{
    background:#1a1f28;
    border:1px solid #323c4b;
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
    background:#252c37;
}
""")
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
        clean = re.sub(r'[^0-9a-fA-F]', '', value)
        if clean:
            hex_val = f"0x{clean.upper()}"
            self.settings_manager.set_setting(setting_id, hex_val)

    def _toggle_bitfield(self, setting_id: str, bit_val: int, mask: int = 0):
        cur = self.settings_manager.get_setting(setting_id)
        cur_val = int(cur, 16) if cur else 0

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
            no_shortcut_types = ("bitfield", "enum")
            if (self._current_setting.type not in no_shortcut_types
                    and focus_widget is not None
                    and self._control_widget.isAncestorOf(focus_widget)):
                self._value_label.setText(f"= {new_value}")
                self._value_label.show()
                if not self._remove_btn.isVisible():
                    self._remove_btn.show()
                return
            self._build_editor()


# ============================================================================
# Arch List - Same style as Settings List
# ============================================================================

class ArchListWidget(QListWidget):
    """List of GPU architectures, drawn by the shared SidebarDelegate."""
    arch_selected = Signal(object)  # emits GPUArch

    def __init__(self):
        super().__init__()
        self.setSelectionMode(QListWidget.SingleSelection)
        self.itemClicked.connect(self._on_item_clicked)
        self.currentItemChanged.connect(self._on_current_item_changed)
        setup_sidebar_list(self)

    def populate(self, archs: List[GPUArch], selected_arch: Optional[GPUArch],
                 only_set: bool = False, only_fav: bool = False):
        """Fill the list with architecture items, highlighting the selected one."""
        # Block currentItemChanged for the rebuild - clear()+re-add can fire
        # spurious selection-changed signals we don't want driving navigation.
        self.blockSignals(True)
        self.clear()
        self.addItem(make_header_item("GPU architectures", 1 if selected_arch else 0))
        for arch in archs:
            is_sel = bool(selected_arch and arch.code == selected_arch.code)
            if only_set and not is_sel:
                continue
            if only_fav and not FAVS.is_fav("arch", arch.code):
                continue
            self.addItem(make_list_item(f"{arch.name}  ·  {arch.arch}", arch,
                                        is_set=is_sel, value=arch.code,
                                        fav=FAVS.is_fav("arch", arch.code)))
        self.blockSignals(False)

    def _on_item_clicked(self, item):
        arch = item.data(Qt.UserRole)
        if arch:
            self.arch_selected.emit(arch)

    def _on_current_item_changed(self, current, previous):
        if current is None:
            return
        arch = current.data(Qt.UserRole)
        if arch:
            self.arch_selected.emit(arch)


# ============================================================================
# Arch Detail - Shows info and a Clear button (similar to SettingEditor)
# ============================================================================

class ArchDetailWidget(QWidget):
    """Detail view for a selected architecture, with a Clear button."""
    clear_requested = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(0, 0, 0, 0)

        # Name (large, bold)
        self._name_label = QLabel()
        self._name_label.setStyleSheet("""
QLabel{
    color:#f2f2f2;
    font-size:18px;
    font-weight:700;
}
""")
        self._name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._name_label)

        # Architecture field
        self._arch_label = QLabel()
        self._arch_label.setStyleSheet("color: #a7afbc; font-size: 13px;")
        self._arch_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._arch_label)

        # Code and example
        self._code_label = QLabel()
        self._code_label.setStyleSheet("font-family: monospace; font-size: 12px; color: #8ea0ba;")
        self._code_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._code_label)

        # Short description
        self._desc_label = QLabel(
            "Makes DXVK-NVAPI report this architecture instead of your real "
            "GPU's (DXVK_NVAPI_GPU_ARCH). It is a spoof meant for "
            "troubleshooting or unlocking features a game gates on a newer "
            "generation — leave it unset to report the real card."
        )
        self._desc_label.setWordWrap(True)
        self._desc_label.setStyleSheet("color: #a7afbc; font-size: 13px;")
        layout.addWidget(self._desc_label)

        layout.addStretch()

        # Clear button (styled like the "Remove Setting" button)
        self._clear_btn = QPushButton("Clear Architecture")
        self._clear_btn.clicked.connect(self.clear_requested)
        self._clear_btn.setStyleSheet("""
QPushButton{
    background:#b93b3b;
    border:1px solid #e45d5d;
    border-radius:5px;
    color:white;
    padding:4px 16px;
    font-weight:600;
    font-size:12px;
}
QPushButton:hover{
    background:#ca4545;
}
QPushButton:pressed{
    background:#a73434;
}
""")
        layout.addWidget(self._clear_btn)

    def set_arch(self, arch: Optional[GPUArch]):
        """Update the detail view with the given architecture (or clear it)."""
        if arch:
            self._name_label.setText(arch.name)
            self._arch_label.setText(f"Architecture: {arch.arch}")
            self._code_label.setText(f"Code: {arch.code}  ·  Example: {arch.example}")
            self._clear_btn.show()
        else:
            self._name_label.setText("")
            self._arch_label.setText("")
            self._code_label.setText("")
            self._clear_btn.hide()


# ============================================================================
# Environment Variable Definitions (DXVK + VKD3D-Proton)
# ============================================================================

DXVK_ENV_VARS: List[EnvVarDef] = [
    # ── HUD ──────────────────────────────────────────────────────────────────
    EnvVarDef("DXVK_HUD", "DXVK", "flags", "",
              "In-game HUD overlay. Comma-separated list of elements to display.",
              options=["devinfo", "fps", "frametimes", "submissions", "drawcalls",
                       "pipelines", "descriptors", "memory", "allocations", "gpuload",
                       "version", "api", "cs", "compiler", "samplers", "swvp", "latency",
                       "1", "full", "scale=N", "opacity=N"],
              placeholder="e.g. devinfo,fps,memory"),
    # ── Frame Rate ───────────────────────────────────────────────────────────
    EnvVarDef("DXVK_FRAME_RATE", "DXVK", "int", "0",
              "Frame rate limiter. 0 = uncapped. Positive value limits to N FPS.\n"
              "REMOVED in upstream DXVK 3.0 — the variable is no longer read there. "
              "Still honoured by DXVK <= 2.7 and forks such as dxvk-low-latency. "
              "On DXVK 3.x use DXVK_CONFIG=\"dxvk.maxFrameRate = 60\" instead, or an "
              "external limiter (gamescope -r, MangoHud, vk_flip_meter).",
              placeholder="e.g. 60"),
    # ── Logging ──────────────────────────────────────────────────────────────
    EnvVarDef("DXVK_LOG_LEVEL", "DXVK", "enum", "",
              "Controls message logging verbosity.",
              options=["none", "error", "warn", "info", "debug"]),
    EnvVarDef("DXVK_LOG_PATH", "DXVK", "string", "",
              "Directory path for DXVK log files (app_d3d11.log, app_dxgi.log etc.).",
              placeholder="/path/to/dir"),
    # ── Shader Cache ─────────────────────────────────────────────────────────
    EnvVarDef("DXVK_SHADER_DUMP_PATH", "DXVK", "string", "",
              "Dump compiled shader bytecode to this directory for debugging.",
              placeholder="/tmp/shaders"),
    EnvVarDef("DXVK_SHADER_CACHE_PATH", "DXVK", "string", "",
              "Override the shader pipeline state cache directory.",
              placeholder="/path/to/cache"),
    EnvVarDef("DXVK_STATE_CACHE", "DXVK", "enum", "",
              "[Legacy] Pipeline state cache control. Set to 0 to disable. Not read by "
              "current upstream DXVK (the state cache is gone; DXVK 3.x caches compiled "
              "shaders instead — see DXVK_SHADER_CACHE). Kept for older builds and "
              "DXVK-Sarek (1.10-based).",
              options=["0", "1"]),
    EnvVarDef("DXVK_STATE_CACHE_PATH", "DXVK", "string", "",
              "[Legacy] Directory for pipeline state cache files. Same caveat as "
              "DXVK_STATE_CACHE — use DXVK_SHADER_CACHE_PATH on DXVK 3.x.",
              placeholder="/path/to/cache"),
    # ── Device Selection ─────────────────────────────────────────────────────
    EnvVarDef("DXVK_FILTER_DEVICE_NAME", "DXVK", "string", "",
              "Select GPU by substring match on Vulkan device name.",
              placeholder="e.g. RTX 4080"),
    EnvVarDef("DXVK_FILTER_DEVICE_UUID", "DXVK", "string", "",
              "Select GPU by 32-char hex Vulkan device UUID (no dashes).",
              placeholder="00000000000000000000000000000001"),
    # ── HDR ──────────────────────────────────────────────────────────────────
    EnvVarDef("DXVK_HDR", "DXVK", "enum", "",
              "Enable HDR10 color space exposure (DXGI_COLOR_SPACE_RGB_FULL_G2084). "
              "Needed by some HDR-capable games.",
              options=["0", "1"]),
    EnvVarDef("DXVK_NO_HDR", "DXVK", "enum", "",
              "[Proton-GE / Proton-CachyOS / Proton-EM] Opposite of DXVK_HDR=1: forcefully "
              "disables HDR when it would otherwise be switched on automatically (current "
              "Protons detect HDR by themselves). Useful on GNOME, which does not yet expose "
              "enough colour-management features for DXVK's HDR path.",
              options=["0", "1"]),
    # ── Debug ────────────────────────────────────────────────────────────────
    EnvVarDef("DXVK_DEBUG", "DXVK", "enum", "",
              "Enables one DXVK debugging mode.\n"
              "capture: debug names and markers for render passes/shaders (default under "
              "some capture tools).\n"
              "hang: detects GPU hangs / VK_ERROR_DEVICE_LOST and logs the failing "
              "command(s) — attach the log to bug reports.\n"
              "markers: forwards application-provided resource names and markers via "
              "VK_EXT_debug_utils.\n"
              "validation: enables the validation callback; also needs "
              "VK_INSTANCE_LAYERS=VK_LAYER_KHRONOS_validation.",
              options=["capture", "hang", "markers", "validation"]),
    EnvVarDef("DXVK_CAPTURE_FRAMES", "DXVK", "string", "",
              "Triggers a frame capture (RenderDoc) for a range of frames: "
              "'first' or 'first:count'. E.g. 500:3 captures frames 500-502. Malformed "
              "values are logged and ignored. Debugging only.",
              placeholder="e.g. 500:3"),
    EnvVarDef("DXVK_ASYNC", "DXVK", "enum", "",
              "Async shader compilation (unofficial/patched builds). 1 = enable.",
              options=["0", "1"]),
    # ── Vulkan Layers ────────────────────────────────────────────────────────
    EnvVarDef("VK_INSTANCE_LAYERS", "DXVK", "string", "",
              "Enable Vulkan instance layers. Use VK_LAYER_KHRONOS_validation for debug.",
              placeholder="VK_LAYER_KHRONOS_validation"),
    # ── NVAPI ────────────────────────────────────────────────────────────────
    EnvVarDef("DXVK_ENABLE_NVAPI", "DXVK", "enum", "",
              "Enable NVAPI support. Required for DLSS, Reflex, and MFG.",
              options=["0", "1"]),
    # ── Shader Compiler ──────────────────────────────────────────────────────
    EnvVarDef("DXVK_NUM_COMPILER_THREADS", "DXVK", "int", "0",
              "Number of pipeline compiler threads. 0 = use all available CPU cores; "
              "a positive number enforces that exact thread count. Some threads are "
              "reserved for high-priority work when the graphics pipeline library "
              "feature is enabled. Set to 1 to force fully single-threaded shader "
              "compilation (deterministic, but slower cold-start compiles).\n"
              "Note: upstream DXVK 3.x does not read this variable — use "
              "DXVK_CONFIG=\"dxvk.numCompilerThreads = N\" there.",
              placeholder="0 = all cores, or e.g. 1"),
    # ── Inline Config Override ───────────────────────────────────────────────
    EnvVarDef("DXVK_CONFIG_FILE", "DXVK", "string", "",
              "Overrides the path DXVK searches for its dxvk.conf. By default DXVK looks "
              "for dxvk.conf in the working directory (usually the game's exe folder). "
              "Useful for keeping a shared config outside a Windows-only game folder.",
              placeholder="/home/user/dxvk.conf"),
    EnvVarDef("DXVK_CONFIG", "DXVK", "dxvk_config", "",
              "Sets one or more dxvk.conf keys directly through the environment instead of "
              "a config file, using the same key = value syntax, with ; as a separator "
              "between entries. Any key documented in dxvk.conf can be set this way "
              "(dxgi.*, d3d9.*, d3d11.*, dxvk.*), e.g. dxgi.maxFrameLatency, dxgi.hideNvidiaGpu, "
              "dxvk.enableGraphicsPipelineLibrary, d3d11.samplerAnisotropy. Takes priority "
              "over an on-disk dxvk.conf for the keys it sets.\n"
              "The picker below covers the latency/Reflex, present, pipeline, NVIDIA and "
              "filtering keys from upstream dxvk.conf; any other key goes into "
              "'Other entries' and is kept verbatim.",
              placeholder="dxgi.hideAmdGpu = True; dxgi.syncInterval = 0"),
    EnvVarDef("DXVK_WSI_DRIVER", "DXVK", "enum", "",
              "Selects the windowing backend DXVK talks to. On Linux builds this is NOT "
              "optional — DXVK throws DxvkError(\"DXVK_WSI_DRIVER environment variable "
              "unset\") when it is empty, since only the Win32 build has a sane fallback. "
              "Proton and Lutris set it for you; you only touch it when running DXVK "
              "outside a launcher, or to force SDL3 vs SDL2 while chasing a "
              "fullscreen/HDR/cursor bug. The value must match a backend that was actually "
              "compiled in, otherwise WSI init fails outright. "
              "Syntax: DXVK_WSI_DRIVER=SDL3 %command%",
              options=["SDL3", "SDL2", "GLFW", "Win32"]),

    EnvVarDef("DXVK_NO_VR", "DXVK", "enum", "",
              "Stops DXVK from loading and querying OpenVR. DXVK asks OpenVR which Vulkan "
              "instance/device extensions a VR runtime needs; on a machine with SteamVR "
              "installed but no headset in use that probe can cost startup time or hang if "
              "the runtime is in a bad state. Set to 1 for non-VR games on a VR-capable "
              "system. "
              "Syntax: DXVK_NO_VR=1 %command%",
              options=["1"]),

    EnvVarDef("DXVK_FORCE_WINDOWED", "DXVK", "enum", "",
              "Forces D3D9 swapchains to stay windowed, ignoring the game's request for "
              "exclusive fullscreen. Useful for old D3D9 titles that mode-switch badly on "
              "Wayland or on a multi-monitor setup, and for keeping a compositor's VRR / "
              "HDR path in charge instead of handing the display over to the game. D3D9 "
              "path only. "
              "Syntax: DXVK_FORCE_WINDOWED=1 %command%",
              options=["1"]),

    EnvVarDef("DXVK_SHADER_CACHE", "DXVK", "enum", "",
              "Set to 0 to disable the driver-level shader cache DXVK enables by default "
              "(only checked when no shader dump path is set). Distinct from "
              "DXVK_STATE_CACHE, which is DXVK's own pipeline state cache. Mostly a "
              "debugging switch — disabling it means every run pays full compilation cost. "
              "Syntax: DXVK_SHADER_CACHE=0 %command%",
              options=["0", "1"]),

]

# Per-flag descriptions for VKD3D_CONFIG checkbox UI
# ============================================================================
# DXVK_CONFIG key picker — curated dxvk.conf keys (upstream dxvk.conf, 2026-09)
# (key, kind, options-or-placeholder, description)
#   kind "enum": options list; "" (unset) is always added as first choice
#   kind "num" : free numeric text (ints, floats or negatives allowed)
# Keys not listed here are preserved verbatim in the "Other entries" field.
# ============================================================================
_TRI = ["Auto", "True", "False"]
_BOOL = ["True", "False"]
DXVK_CONFIG_KEYS = [
    # ── Latency / Reflex ────────────────────────────────────────────────────
    ("dxvk.latencySleep", "enum", _TRI,
     "Latency sleep / Reflex. Auto: Reflex only in D3D11 games that use it, with "
     "VK_NV_low_latency2 + dxvk-nvapi. True: DXVK's built-in latency reduction for "
     "every game (no effect with in-game/external limiters). False: disable both."),
    ("dxvk.latencyTolerance", "num", "1000",
     "Latency-sleep heuristic tolerance in µs. Higher = more latency, maybe smoother "
     "pacing. No effect when VK_NV_low_latency2 is used."),
    ("dxvk.disableNvLowLatency2", "enum", _TRI,
     "Don't use VK_NV_low_latency2 (Reflex unavailable; latencySleep=True then uses "
     "the custom algorithm). Auto disables it for 32-bit games only."),
    # ── Frame pacing / present ──────────────────────────────────────────────
    ("dxvk.maxFrameRate", "num", "0",
     "Frame rate limit (0 = off). DXVK 3.x replacement for DXVK_FRAME_RATE."),
    ("dxgi.maxFrameLatency", "num", "0",
     "Enforce stricter max frame latency (0 = app value, 1-16)."),
    ("dxgi.syncInterval", "num", "-1",
     "Override Vsync for D3D10/11/12: 0 = off, n = on (every n-th vblank), -1 = app."),
    ("d3d9.presentInterval", "num", "-1",
     "Override Vsync for D3D9: 0 = off, n = on, -1 = app."),
    ("dxvk.tearFree", "enum", _TRI,
     "True: mailbox when in-game Vsync is off (no tearing). False: FIFO_RELAXED when "
     "Vsync is on (tears instead of stuttering below refresh)."),
    ("dxvk.enablePresentTiming", "enum", _BOOL,
     "Present-timing features (non-native refresh emulation, frame limiter). "
     "Default True; disable only for debugging."),
    ("dxgi.forceRefreshRate", "num", "0",
     "Only expose display modes with this refresh rate (0 = off). Use with caution."),
    # ── Pipelines / descriptors ─────────────────────────────────────────────
    ("dxvk.enableGraphicsPipelineLibrary", "enum", _TRI,
     "Auto: GPL + optimized pipelines in background. True: GPL only. False: off "
     "(more stutter). Debug option."),
    ("dxvk.enableDescriptorHeap", "enum", _TRI,
     "VK_EXT_descriptor_heap. Auto: known-good drivers only. True: force if supported."),
    ("dxvk.enableDescriptorBuffer", "enum", _TRI,
     "VK_EXT_descriptor_buffer (descriptor heap takes precedence when both are on)."),
    ("dxvk.numCompilerThreads", "num", "0",
     "Pipeline compiler threads (0 = all cores). Lower it to cut compile-time "
     "CPU contention on high-core CPUs."),
    ("dxvk.enableMemoryDefrag", "enum", _TRI,
     "Video-memory defragmentation. Debug option."),
    # ── NVIDIA / GPU exposure ───────────────────────────────────────────────
    ("dxgi.hideNvidiaGpu", "enum", _TRI,
     "Report NVIDIA GPUs as AMD. Auto hides unless NVAPI is enabled in Proton — "
     "set False when you need DLSS/Reflex via dxvk-nvapi."),
    ("dxvk.enableNvCudaInterop", "enum", _BOOL,
     "VK_NVX_* CUDA-interop extensions required for DLSS (default True on 64-bit NVIDIA)."),
    ("dxvk.hideIntegratedGraphics", "enum", _BOOL,
     "Hide the iGPU when a dGPU exists. Prefer DXVK_FILTER_DEVICE_NAME if possible."),
    ("dxgi.maxDeviceMemory", "num", "0",
     "Override reported VRAM in MB (0 = off). Fixes texture streaming in games "
     "that mis-handle large VRAM. Not a hard cap."),
    ("dxgi.enableHDR", "enum", _BOOL,
     "Expose the HDR10 colour space (defaults to DXVK_HDR)."),
    # ── Texture filtering ───────────────────────────────────────────────────
    ("d3d11.samplerAnisotropy", "num", "-1",
     "Force AF for all D3D11 samplers (0-16, -1 = app). Can break bilinear-dependent passes."),
    ("d3d9.samplerAnisotropy", "num", "-1",
     "Force AF for all D3D9 samplers (0-16, -1 = app)."),
    ("d3d11.samplerLodBias", "num", "0.0",
     "Add to the app's mip LOD bias (-2.0 .. 1.0). Negative = sharper + shimmer."),
    ("d3d11.clampNegativeLodBias", "enum", _BOOL,
     "Clamp negative LOD bias to 0 (after samplerLodBias)."),
    # ── Added from upstream dxvk.conf (DXVK 3.1.x, 2026-10). Kinds other than ──
    # ── "enum" render as a free text box (placeholder = third field).        ──
    # ── Frame limiter / latency (per API) ───────────────────────────────────
    ("dxgi.maxFrameRate", "num", "0",
     "Legacy per-API D3D10/11 frame limiter (0 = off). Prefer dxvk.maxFrameRate, or "
     "MangoHud / Gamescope limiters, which pace better."),
    ("d3d9.maxFrameRate", "num", "0",
     "Legacy per-API D3D9 frame limiter (0 = off). Prefer dxvk.maxFrameRate or an "
     "external limiter."),
    ("d3d9.maxFrameLatency", "num", "0",
     "Enforce stricter max frame latency for D3D9 (0 = app value)."),
    # ── GPU identification / spoofing ───────────────────────────────────────
    ("dxgi.hideAmdGpu", "enum", _TRI,
     "Report AMD GPUs as NVIDIA GPUs (workaround for AMDAGS / AMD-specific code "
     "paths). Auto = only for known-affected games."),
    ("dxgi.hideIntelGpu", "enum", _TRI,
     "Report Intel GPUs as AMD GPUs (workaround for Intel-specific libraries such as "
     "XeSS). Auto = known-affected games; defaults to True for D3D8/9."),
    ("dxgi.hideNvkGpu", "enum", _TRI,
     "Report NVIDIA GPUs running on NVK (Mesa) as AMD GPUs. Irrelevant with the "
     "proprietary NVIDIA driver."),
    ("dxgi.customVendorId", "str", "e.g. 10de",
     "Report this PCI vendor ID (hex) instead of the real one. 10de = NVIDIA, "
     "1002 = AMD, 8086 = Intel."),
    ("dxgi.customDeviceId", "str", "e.g. 2704",
     "Report this PCI device ID (hex) instead of the real one."),
    ("dxgi.customDeviceDesc", "str", "e.g. NVIDIA GeForce RTX 4080",
     "Report this adapter description string instead of the real GPU name."),
    ("dxgi.maxSharedMemory", "num", "0",
     "Override reported shared system memory in MB (0 = off). Companion of "
     "dxgi.maxDeviceMemory; not a hard cap."),
    ("dxvk.deviceFilter", "str", "e.g. RTX 5080",
     "Only expose Vulkan devices whose name contains this string. Config-file "
     "equivalent of DXVK_FILTER_DEVICE_NAME."),
    ("dxgi.enableUe4Workarounds", "enum", _BOOL,
     "Unreal Engine 4 HDR workarounds for games not named *-Win64-Shipping.exe "
     "(avoids AMDAGS crashes in D3D11 on HDR systems)."),
    # ── D3D11 performance / compatibility ───────────────────────────────────
    ("d3d11.maxFeatureLevel", "enum",
     ["9_1", "9_2", "9_3", "10_0", "10_1", "11_0", "11_1", "12_0", "12_1"],
     "Cap the D3D11 feature level a device can be created with (default 12_1). "
     "Raising it can unblock apps that fail device creation."),
    ("d3d11.maxTessFactor", "num", "0",
     "Cap the tessellation factor (8-64, 0 = off). Big win in tessellation-heavy games."),
    ("d3d11.relaxedBarriers", "enum", _BOOL,
     "Relax UAV pipeline barriers (ignore WAW in compute, all UAV hazards in graphics). "
     "Can raise performance; may cause rendering glitches."),
    ("d3d11.relaxedGraphicsBarriers", "enum", _BOOL,
     "Like relaxedBarriers but only for graphics-shader UAVs (compute untouched)."),
    ("d3d11.forceComputeLdsBarriers", "enum", _BOOL,
     "Insert barriers after group-shared-memory writes in compute shaders. Workaround "
     "for games with missing GroupMemoryBarrier; costs performance."),
    ("d3d11.forceComputeUavBarriers", "enum", _BOOL,
     "Insert barriers after UAV writes in compute shaders. Workaround for missing "
     "UAV sync; costs performance."),
    ("d3d11.forceSampleRateShading", "enum", _BOOL,
     "Per-sample instead of per-pixel shading when MSAA is on. Cleaner image, big "
     "performance cost."),
    ("d3d9.forceSampleRateShading", "enum", _BOOL,
     "Per-sample shading when MSAA is on (D3D9). Cleaner image, big performance cost."),
    ("d3d11.disableMsaa", "enum", _BOOL,
     "Force every texture to 1 sample and fix up resolves/shaders. Big FPS gain at the "
     "cost of no MSAA."),
    ("d3d11.disableDirectImageMapping", "enum", _BOOL,
     "Don't map dynamic images directly. Fixes corruption in games that ignore the "
     "driver's row pitch."),
    ("d3d11.cachedDynamicResources", "str", "e.g. vicr",
     "Put dynamic resources with these bind flags in cached system memory for fast CPU "
     "readback (v=vertex, i=index, c=constant, r=shader resource ...). Buggy-app workaround."),
    ("d3d11.exposeDriverCommandLists", "enum", _BOOL,
     "Advertise native driver command-list support (default True). Turn off if a game "
     "misbehaves with deferred contexts."),
    ("d3d11.enableContextLock", "enum", _BOOL,
     "Force-enable the D3D11 context lock (ID3D10Multithread). Race-condition debugging."),
    # ── D3D9 ────────────────────────────────────────────────────────────────
    ("d3d9.samplerLodBias", "num", "0.0",
     "Add to the app's mip LOD bias for D3D9 samplers."),
    ("d3d9.clampNegativeLodBias", "enum", _BOOL,
     "Clamp negative D3D9 LOD bias to 0 (after samplerLodBias)."),
    ("d3d9.floatEmulation", "enum", ["Auto", "True", "False", "Strict"],
     "x*0 floating-point quirk emulation. True = fast/less exact, Strict = slow/exact, "
     "False = off. Auto = per-game."),
    ("d3d9.deviceLocalConstantBuffers", "enum", _TRI,
     "Stream shader constants into VRAM; may help GPU-bound scenes. Auto = discrete GPUs only."),
    ("d3d9.lenientClear", "enum", _BOOL,
     "Fast-path clears that nearly cover the whole render target."),
    ("d3d9.maxAvailableMemory", "num", "4096",
     "Initial value (MB) for GetAvailableTextureMem tracking. Raise for old games that "
     "refuse to start with 'too little' video memory."),
    ("d3d9.forceRefreshRate", "num", "0",
     "Only expose D3D9 display modes with this refresh rate (0 = off)."),
    ("d3d9.seamlessCubes", "enum", _BOOL,
     "Always use seamless cube maps (prettier edges, deviates from real D3D9)."),
    ("d3d9.useFP16", "enum", _TRI,
     "FP16 for partial-precision instructions. May help weak GPUs, can regress desktop GPUs."),
    ("d3d9.cachedWriteOnlyBuffers", "enum", _BOOL,
     "Allocate write-only D3DPOOL_DEFAULT buffers in cached system memory (buggy-app readback fix)."),
    ("d3d9.forceAspectRatio", "str", "e.g. 16:9",
     "Force the aspect ratio of exposed D3D9 display modes."),
    # ── Memory / Vulkan backend (mostly debug or driver-specific) ───────────
    ("dxvk.maxMemoryBudget", "num", "0",
     "Cap the VRAM DXVK will use, in MB (0 = off). Debug only: severe slowdown when hit."),
    ("dxvk.zeroMappedMemory", "enum", _BOOL,
     "Zero mapped memory when suballocations are freed. Huge CPU cost; last resort for "
     "games that don't initialise their buffers."),
    ("dxvk.useRawSsbo", "enum", _TRI,
     "Raw/structured buffer views via SSBOs. Auto = on where offset alignment is 4 B "
     "(AMD). Unsafe on hardware with stricter alignment."),
    ("dxvk.enableNvRawAccessChains", "enum", _BOOL,
     "Use VK_NV_raw_access_chains on NVIDIA (default True). Disabling reduces performance; debug only."),
    ("dxvk.enableImplicitResolves", "enum", _BOOL,
     "Resolve multisampled images when a game samples them as plain textures (fixes "
     "striped artifacts on NVIDIA). Default True."),
    ("dxvk.enableUnifiedImageLayouts", "enum", _BOOL,
     "Use VK_KHR_unified_image_layouts where available. Default True; debug only."),
    ("dxvk.trackPipelineLifetime", "enum", _TRI,
     "Free pipeline libraries aggressively to save memory/address space. Auto = 32-bit games only."),
    ("dxvk.lowerSinCos", "enum", _TRI,
     "Custom sin/cos approximation. Auto = only on drivers with poor native precision."),
    ("dxvk.tilerMode", "enum", _TRI,
     "Tiler (mobile GPU) submission heuristics. For perf testing only."),
    ("dxvk.enableDebugUtils", "enum", _BOOL,
     "Enable VK_EXT_debug_utils object names/labels (RenderDoc / Nsight)."),
]
DXVK_CONFIG_KEY_NAMES = [k[0] for k in DXVK_CONFIG_KEYS]

VKD3D_CONFIG_DESCS: Dict[str, str] = {
    # ── Raytracing ────────────────────────────────────────────────────────────
    "nodxr":                                "Disable DXR (raytracing) support entirely.",
    "dxr":                                  "Force-enable DXR even when considered unsafe (auto-enabled since v2.11).",
    "dxr12":                                "Experimental DXR 1.2 support (requires VK_EXT_opacity_micromap).",
    "allow_sbt_collection":                "Allow shader-binding-table collection for DXR pipelines. Required for Cyberpunk 2077 DXR to function correctly.",
    # ── Performance / ReBAR / Memory ─────────────────────────────────────────
    "force_static_cbv":                     "Speed hack on NVIDIA — may give performance uplift or cause issues. Unsafe.",
    "single_queue":                         "Disable async compute/transfer queues, use a single queue. Trades perf for stability.",
    "no_upload_hvv":                        "Block host-visible VRAM (resizable BAR) for the UPLOAD heap. Frees VRAM at cost of GPU perf. Auto-applied for Halo Infinite, Age of Wonders 4, Red Dead Redemption, Monster Hunter Wilds, Death Stranding.",
    "small_vram_rebar":                     "Use a conservative ReBAR budget (good for 8 GB GPUs). Auto-applied for Serious Sam 4 and all Unreal Engine 5 games (-Win64-Shipping.exe).",
    "recycle_command_pools":                "Recycle Vulkan command pools instead of freeing them. Reduces memory fragmentation. Auto-applied for Elden Ring.",
    "memory_allocator_skip_clear":          "Skip zeroing newly allocated committed memory. Reduces stutter in allocation-heavy games. Auto-applied for Elden Ring. Use only if game initializes its own buffers.",
    "host_import_fallback":                "Use a fallback path for host-memory import instead of the primary DMA path. Workaround for amdgpu kernel bug with concurrent submissions. Auto-applied for Halo Infinite, A Plague Tale Requiem.",
    # ── Submission / Frame Timing ─────────────────────────────────────────────
    "no_staggered_submit":                  "Disable staggered command-buffer submission. Auto-applied for all UE5 games and TLOU Part I. Reduces frame-time spikes in affected titles.",
    "one_time_submit":                      "Force one-shot command-buffer submission mode. Workaround for GPU hang in Star Wars Outlaws.",
    # ── PSO / Pipeline Cache ──────────────────────────────────────────────────
    "pipeline_library_ignore_mismatch_driver": "Ignore driver-version mismatch when loading the pipeline library cache. Useful after driver updates to avoid cold compiles. Auto-applied for Elden Ring.",
    "retain_psos":                          "Keep PSOs (pipeline state objects) alive instead of freeing them immediately. Prevents use-after-free crashes in FFVII Rebirth, Ark Ascended, and REANIMAL.",
    # ── Descriptor Heap (2026) ────────────────────────────────────────────────
    "descriptor_heap":                      "Enable the new VK_EXT_descriptor_heap code path (merged May 2026). Requires Mesa ≥ 26.1 or NVIDIA driver with descriptor heap support. Fixes Xid 109 crashes and Crimson Desert hang on Blackwell.",
    # ── CBV / SRV Binding Workarounds ────────────────────────────────────────
    "force_raw_va_cbv":                    "Force constant-buffer views to use raw GPU virtual addresses instead of descriptor-based binding. Fixes GPU hangs or corruption in Halo Infinite, Eve Online, Guardians of the Galaxy.",
    "preallocate_srv_mip_clamps":          "Pre-allocate mip-clamp descriptors for all SRVs at resource creation time. Workaround for a descriptor aliasing bug. Auto-applied for Halo Infinite.",
    # ── Rendering / Compression Workarounds ──────────────────────────────────
    "no_invariant_position":               "Disable the invariant-position workaround (ON by default). Try if you see Z-fighting or vertex-position artifacts.",
    "disable_uav_compression":             "Disable UAV texture compression for all images. Fixes rendering corruption in A Plague Tale Requiem, Shadow of Tomb Raider, Marvel's Spider-Man.",
    "disable_simultaneous_uav_compression": "Disable compression only for resources with simultaneous-access flag. More targeted than disable_uav_compression. Auto-applied for Witcher 3.",
    "disable_color_compression":           "Disable color (render target) texture compression. Fixes rendering glitches where a render target is simultaneously read/written. Auto-applied for Rise of the Tomb Raider.",
    "force_initial_transition":            "Force all resources to perform their initial D3D12 state transition. Fixes GPU hang or rendering corruption caused by missing transitions. Auto-applied for Lost Judgment, Spider-Man, Miles Morales, Deus Ex Mankind Divided, FFXVI.",
    "defer_resource_destruction":          "Defer resource destruction to avoid GPU use-after-free bugs with sparse resources. Auto-applied for AC: Valhalla.",
    "prefer_thin_uav_tiling":             "Use thin image tiling for 3D UAV textures, which can help performance on some titles. Auto-applied for The Last of Us Part I.",
    "skip_null_sparse_tiles":             "Skip GPU map operations for null/empty sparse tiles. Workaround for driver crash or hang with sparse textures. Auto-applied for Monster Hunter Wilds.",
    "force_dynamic_msaa":                 "Force dynamic MSAA resolve mode. Workaround for MSAA rendering artifacts. Auto-applied for World of Warcraft.",
    # ── Alignment ────────────────────────────────────────────────
    "reject_padded_small_resource_alignment": "Reject the padded small-resource alignment path. Workaround for memory alignment bugs in Starfield.",
    # ── Shader / Subgroup ─────────────────────────────────────────────────────
    # ── Debug (informational) ─────────────────────────────────────────────────
    "vk_debug":                            "Enable Vulkan debug extensions and loads validation layer.",
    "skip_application_workarounds":        "Skip all application-specific workarounds. For debugging only.",
    "force_host_cached":                   "Force all host-visible allocations to CACHED. Speeds up GPU captures with RenderDoc.",
    # ── Added from upstream config_flag_decl.h (2026-09) ─────────────────
    "extra_rtas_sync":                     "Insert extra barriers around acceleration-structure builds. Try for DXR flicker/corruption or RT GPU hangs.",
    "rtas_allow_blas_rebuild_sizes":       "Size BLAS allocations for full rebuilds instead of updates. Auto-applied for some RT titles; fixes RT hangs/corruption.",
    "zero_fill_blp":                       "Zero-fill build-layout prebuild scratch memory for RT builds. Debug/workaround for garbage RT geometry.",
    "no_gpu_upload_heap":                  "Don't expose D3D12 GPU upload heaps (ReBAR-backed). Try if a game that uses GPU upload heaps stutters or runs out of VRAM.",
    "memory_allocator_skip_image_heap_clear": "Skip zeroing heaps used for placed textures. Less allocation stutter; can expose garbage in games that read uninitialised memory.",
    "disallow_committed_texture_suballocation": "Give each committed texture its own allocation (better driver memory priorities, higher CPU cost). Default suballocates because Diablo 4 regresses badly otherwise.",
    "allow_image_heap_suballocation":      "Suballocate image heaps even when the driver supports dynamic memory priorities. Lower allocation overhead, worse paging under VRAM pressure.",
    "avoid_image_buffer_aliasing":         "Avoid aliasing images and buffers in the same memory. Auto-applied for World of Warcraft; fixes corruption in heap-aliasing games.",
    "no_clear_uav_sync":                   "Skip the extra sync after UAV clears. Small perf gain; may cause flicker in games that depend on it.",
    "pipeline_library_app_cache":          "Use only the game's own ID3D12PipelineLibrary cache, not vkd3d-proton's internal cache.",
    "global_pipeline_cache":               "Use one global VkPipelineCache instead of per-PSO caches. Useful when the game creates PSOs very late.",
    "shader_cache_sync":                   "Write the vkd3d-proton shader cache synchronously. For debugging cache corruption only; causes hitching.",
    "disable_depth_compression":           "Disable depth/stencil compression. Workaround for depth corruption / shadow artifacts.",
    "no_nvx":                              "Don't use NVX_binary_import / NVX_image_view_handle. These NVIDIA extensions are what DLSS/NGX uses — only disable for debugging (DLSS will stop working).",
    "skip_driver_workarounds":             "Skip vkd3d-proton's built-in per-driver bug workarounds. Debug only.",
    "enable_experimental_features":        "Expose experimental features (e.g. work graphs, maintenance8 paths) even when not considered production-ready.",
    "ignore_shared_fence":                 "Treat D3D12_FENCE_FLAG_SHARED fences as regular fences. Workaround for games/overlays that break on shared fences.",
    "breadcrumbs":                         "Enable GPU breadcrumbs: on device lost, vkd3d-proton logs which command list / dispatch hung. Small overhead.",
    "breadcrumbs_sync":                    "Breadcrumbs + synchronous submission for exact hang location. Large overhead; debug GPU hangs only.",
    "breadcrumbs_trace":                   "Breadcrumbs + full trace logging of every command. Very slow; debug only.",
    "log_memory_budget":                   "Log VRAM / ReBAR budget decisions and allocations to the vkd3d log. Handy for diagnosing VRAM-limited stutter.",
    "debug_utils":                         "Enable VK_EXT_debug_utils labels (object names/markers) for RenderDoc/Nsight captures.",
    # ── Added from upstream config_flag_decl.h (2026-10, vkd3d-proton 3.0.1+) ─
    "fault":                               "Report GPU faults (device lost) with detailed fault info and keep the app blocked on its fences while the report is dumped. Lighter than breadcrumbs; debug GPU hangs only.",
    "app_debug_marker_only":               "With debug_utils: only forward the application's own debug markers/labels, not vkd3d-proton's internal ones. Cleaner RenderDoc/Nsight captures.",
    "queue_profile_extra":                 "Extra detail in VKD3D_QUEUE_PROFILE queue-timeline traces (more events per submission). Profiling only; adds overhead.",
    "descriptor_qa_checks":                "Instrument shaders to validate descriptor access (needs a build with -Denable_descriptor_qa=true). Very slow; QA/debug only.",
    "instruction_qa_checks":               "Instrument shaders for per-instruction buffer sync validation (needs a descriptor-QA build, see docs/syncval.md). Very slow; QA/debug only.",
    "pipeline_library_ignore_spirv":       "Ignore the SPIR-V stored in the pipeline library cache and recompile from DXIL. Forced on automatically by VKD3D_SHADER_OVERRIDE / VKD3D_SHADER_DUMP_PATH / VKD3D_QA_HASHES.",
    "pipeline_library_no_serialize_spirv": "Don't store SPIR-V in the pipeline library cache (smaller cache file, but a driver/vkd3d-proton update then forces a cold DXIL recompile).",
    "pipeline_library_sanitize_spirv":     "Re-validate SPIR-V loaded from the pipeline library before use. Catches corrupted caches at a startup cost; debug only.",
    "pipeline_library_log":                "Log pipeline library hits/misses and load/save decisions to the vkd3d log. Useful for diagnosing cold-compile stutter.",
    "mutable_single_set":                  "Force the single-descriptor-set path for mutable descriptors (auto-enabled on Intel). Workaround/test for descriptor-set limits; may cost performance.",
    "damage_not_zeroed_allocations":       "Fill D3D12_HEAP_FLAG_CREATE_NOT_ZEROED buffers with 0xFF on creation to expose games that wrongly assume zeroed memory. Debug only.",
    "debug_wave64_simulation":             "Report a wave size of 64 to the game (WaveLaneCountMin forced up) to test wave64-dependent shader paths on wave32 GPUs. Debug only.",
    "fail_unsupported_state_template":     "Fail loudly instead of silently ignoring when a command-list state template is unsupported. Debug only.",
    "adreno_7xx_extra_compat":             "Qualcomm Adreno 7xx only: use slower, more spec-compliant paths to isolate whether a game hits them. Irrelevant on NVIDIA/AMD.",
}

VKD3D_ENV_VARS: List[EnvVarDef] = [
    # ── Config Flags ─────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_CONFIG", "VKD3D-Proton", "vkd3d_config", "",
              "Comma/semicolon-separated list of behavior flags for vkd3d-proton.",
              options=[
                  # Raytracing
                  "nodxr",
                  "dxr",
                  "dxr12",
                  "allow_sbt_collection",
                  "extra_rtas_sync",
                  "rtas_allow_blas_rebuild_sizes",
                  "zero_fill_blp",
                  # Performance / ReBAR / Memory
                  "force_static_cbv",
                  "single_queue",
                  "no_upload_hvv",
                  "no_gpu_upload_heap",
                  "small_vram_rebar",
                  "recycle_command_pools",
                  "memory_allocator_skip_clear",
                  "memory_allocator_skip_image_heap_clear",
                  "host_import_fallback",
                  "disallow_committed_texture_suballocation",
                  "allow_image_heap_suballocation",
                  "avoid_image_buffer_aliasing",
                  # Submission / Frame Timing
                  "no_staggered_submit",
                  "one_time_submit",
                  "no_clear_uav_sync",
                  # PSO / Pipeline Cache
                  "pipeline_library_ignore_mismatch_driver",
                  "pipeline_library_app_cache",
                  "pipeline_library_no_serialize_spirv",
                  "pipeline_library_ignore_spirv",
                  "pipeline_library_sanitize_spirv",
                  "pipeline_library_log",
                  "global_pipeline_cache",
                  "shader_cache_sync",
                  "retain_psos",
                  # Descriptor Heap
                  "descriptor_heap",
                  "mutable_single_set",
                  # CBV / SRV Binding
                  "force_raw_va_cbv",
                  "preallocate_srv_mip_clamps",
                  # Rendering / Compression Workarounds
                  "no_invariant_position",
                  "disable_uav_compression",
                  "disable_simultaneous_uav_compression",
                  "disable_color_compression",
                  "disable_depth_compression",
                  "force_initial_transition",
                  "defer_resource_destruction",
                  "prefer_thin_uav_tiling",
                  "skip_null_sparse_tiles",
                  "force_dynamic_msaa",
                  "reject_padded_small_resource_alignment",
                  # NVIDIA / Driver
                  "no_nvx",
                  "skip_driver_workarounds",
                  "enable_experimental_features",
                  "ignore_shared_fence",
                  # Debug / Crash diagnostics
                  "breadcrumbs",
                  "breadcrumbs_sync",
                  "breadcrumbs_trace",
                  "fault",
                  "log_memory_budget",
                  "vk_debug",
                  "debug_utils",
                  "app_debug_marker_only",
                  "queue_profile_extra",
                  "descriptor_qa_checks",
                  "instruction_qa_checks",
                  "damage_not_zeroed_allocations",
                  "debug_wave64_simulation",
                  "fail_unsupported_state_template",
                  "adreno_7xx_extra_compat",
                  "skip_application_workarounds",
                  "force_host_cached",
              ],
              placeholder="e.g. dxr,retain_descriptor_heaps"),
    # ── Frame Rate ───────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_FRAME_RATE", "VKD3D-Proton", "int", "0",
              "Frame rate limiter. 0 = uncapped. Positive value = limit to N FPS.",
              placeholder="e.g. 120"),
    # ── Logging ──────────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_DEBUG", "VKD3D-Proton", "enum", "",
              "Debug log verbosity for vkd3d-proton runtime.",
              options=["none", "err", "info", "fixme", "warn", "trace"]),
    EnvVarDef("VKD3D_SHADER_DEBUG", "VKD3D-Proton", "enum", "",
              "Debug log verbosity for shader compilers.",
              options=["none", "err", "info", "fixme", "warn", "trace"]),
    EnvVarDef("VKD3D_LOG_FILE", "VKD3D-Proton", "string", "",
              "Redirect VKD3D_DEBUG log output to this file.",
              placeholder="/tmp/vkd3d.log"),
    # ── Device Selection ─────────────────────────────────────────────────────
    EnvVarDef("VKD3D_VULKAN_DEVICE", "VKD3D-Proton", "int", "",
              "Zero-based Vulkan device index to force device selection.",
              placeholder="0"),
    EnvVarDef("VKD3D_FILTER_DEVICE_NAME", "VKD3D-Proton", "string", "",
              "Skip Vulkan devices that don't contain this substring.",
              placeholder="e.g. RTX 4080"),
    EnvVarDef("VKD3D_DISABLE_EXTENSIONS", "VKD3D-Proton", "string", "",
              "Comma-separated list of Vulkan extensions to disable.",
              placeholder="VK_EXT_foo,VK_KHR_bar"),
    # ── Shader Cache ─────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_SHADER_CACHE_PATH", "VKD3D-Proton", "string", "",
              "Override directory for vkd3d-proton.cache. Set to '0' to disable.",
              placeholder="/path/to/cache or 0"),
    EnvVarDef("VKD3D_SHADER_DUMP_PATH", "VKD3D-Proton", "string", "",
              "Dump shader bytecode (SPIR-V/DXBC/DXIL) to this directory.",
              placeholder="/tmp/vkd3d-shaders"),
    EnvVarDef("VKD3D_SHADER_OVERRIDE", "VKD3D-Proton", "string", "",
              "Directory containing override SPIR-V shaders by hash.",
              placeholder="/path/to/overrides"),
    # ── Swapchain ────────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_SWAPCHAIN_PRESENT_MODE", "VKD3D-Proton", "enum", "",
              "Force a specific Vulkan present mode for the swapchain.",
              options=["IMMEDIATE", "MAILBOX", "FIFO", "FIFO_RELAXED", "FIFO_LATEST_READY"]),
    # ── Descriptor Debug ─────────────────────────────────────────────────────
    EnvVarDef("VKD3D_DESCRIPTOR_QA_LOG", "VKD3D-Proton", "string", "",
              "Path to log descriptor heap operations. Requires descriptor_qa build.",
              placeholder="/tmp/desc_qa.log"),
    # ── Debug Ring ───────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_SHADER_DEBUG_RING_SIZE_LOG2", "VKD3D-Proton", "int", "",
              "Log2 size in bytes of the shader printf debug ring buffer (e.g. 28 = 256 MiB).",
              placeholder="28"),
    # ── Swapchain ────────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_SWAPCHAIN_LATENCY_FRAMES", "VKD3D-Proton", "int", "",
              "Maximum number of frames in flight, enforced through KHR_present_wait "
              "(default 3). Lower = less input lag at the cost of CPU/GPU starvation risk; "
              "2 is often stable, 1 is aggressive. Also accepted on the DXGI path. Default "
              "removed in v2.14 (was previously forced to 2).\n"
              "Upstream applies it to the swapchain's internal frame latency, accepting 1 "
              "up to the DXGI swap-chain buffer maximum.\n"
              "vkd3d-low-latency ALSO feeds it to the frame pacer's wait latency, and there "
              "only 1-3 is accepted. That fork deliberately defaults to 3 rather than "
              "dxvk-low-latency's 2: at 2 a lot of D3D12 games throttle their fps, so 2 is "
              "left as a manual choice for the titles where it doesn't. "
              "Syntax: VKD3D_SWAPCHAIN_LATENCY_FRAMES=2 %command%",
              placeholder="2  (1-3 on vkd3d-low-latency)"),
    # ── Tessellation ─────────────────────────────────────────────────────────
    EnvVarDef("VKD3D_LIMIT_TESS_FACTORS", "VKD3D-Proton", "enum", "",
              "Clamp tessellation factors to reduce GPU load in titles with excessive tessellation "
              "(e.g. Wo Long: Fallen Dynasty). 0 = disabled (default). "
              "Automatically enabled for known problematic games.",
              options=["0", "1"]),
    # ── Pipeline Library / Threading ──────────────────────────────────────────
    EnvVarDef("VKD3D_FEATURE_LEVEL", "VKD3D-Proton", "enum", "",
              "Force the reported D3D12 feature level, regardless of what the Vulkan driver "
              "would otherwise expose. Useful for games that refuse to start when they detect "
              "a feature level they don't expect, or that under-report driver capability.",
              options=["12_0", "12_1"]),
    EnvVarDef("VKD3D_WORKER_THREAD_COUNT", "VKD3D-Proton", "int", "",
              "Number of background worker threads vkd3d-proton uses for shader/pipeline "
              "compilation and pipeline-library disk-cache I/O. Fewer threads reduce CPU "
              "contention from compile storms on busy many-core systems; more can speed up "
              "cold-cache compile bursts at the cost of using more cores at once.",
              placeholder="e.g. 2"),
    EnvVarDef("VKD3D_PIPELINE_LIBRARY_SIZE", "VKD3D-Proton", "int", "",
              "Caps the size (in bytes) of the automatic vkd3d-proton.cache pipeline library "
              "vkd3d-proton maintains on disk. Once the cap is hit, older/least-used entries "
              "are evicted rather than letting the cache grow without bound.",
              placeholder="e.g. 2147483648 (2 GiB)"),
    EnvVarDef("VKD3D_PIPELINE_LIBRARY_APP_CACHE_ONLY", "VKD3D-Proton", "enum", "",
              "Restrict pipeline caching to whatever the application itself explicitly manages "
              "through ID3D12PipelineLibrary, instead of also letting vkd3d-proton silently "
              "cache every PSO it sees in its own automatic vkd3d-proton.cache disk cache.",
              options=["0", "1"]),
    EnvVarDef("VKD3D_SWAPCHAIN_IMAGES", "VKD3D-Proton", "int", "",
              "Overrides the swapchain's minimum image count. The driver minimum is still "
              "respected, so this can only raise it. More images smooth over presentation "
              "hitches at the cost of latency and VRAM; fewer is not achievable this way. "
              "Syntax: VKD3D_SWAPCHAIN_IMAGES=3 %command%",
              placeholder="e.g. 3"),

    EnvVarDef("VKD3D_SWAPCHAIN_DEBUG_LATENCY", "VKD3D-Proton", "enum", "",
              "Logs per-present latency information from the swapchain. Diagnostic — pair "
              "with PROTON_LOG=1 when checking whether a game actually drives the "
              "low-latency path at all. "
              "Syntax: VKD3D_SWAPCHAIN_DEBUG_LATENCY=1 %command%",
              options=["1"]),

    EnvVarDef("VKD3D_SHADER_MODEL", "VKD3D-Proton", "enum", "",
              "Caps the shader model vkd3d-proton reports to the game. Lowering it can "
              "steer a title onto a simpler rendering path (or work around a broken "
              "SM6.6+ path); raising it above what the driver supports will not work. "
              "Unrecognised strings are ignored. "
              "Syntax: VKD3D_SHADER_MODEL=6_6 %command%",
              options=["5_1", "6_0", "6_1", "6_2", "6_3", "6_4",
                       "6_5", "6_6", "6_7", "6_8", "6_9"]),

    EnvVarDef("VKD3D_LOG_BUFFERED", "VKD3D-Proton", "int", "",
              "Buffers log output in memory instead of writing it out line by line; the "
              "value is the ring buffer size in bytes. Keeps heavy VKD3D_DEBUG logging "
              "from distorting the very frame timings you are trying to measure. Note the "
              "trade-off: on a hard crash the tail of the log may never be flushed. "
              "Syntax: VKD3D_LOG_BUFFERED=1048576 VKD3D_DEBUG=info %command%",
              placeholder="e.g. 1048576 (bytes)"),

    EnvVarDef("VKD3D_UNIX_ENV", "VKD3D-Proton", "string", "",
              "Passes environment variables from the Unix side into the D3D12 core module "
              "at load time (applied early, before device creation). Escape hatch for "
              "launchers that cannot set a variable in the Wine environment directly. "
              "Syntax: VKD3D_UNIX_ENV=\"VKD3D_CONFIG=dxr\" %command%",
              placeholder='e.g. VKD3D_CONFIG=dxr'),

    EnvVarDef("VKD3D_UNIX_POST_ENV", "VKD3D-Proton", "string", "",
              "Same mechanism as VKD3D_UNIX_ENV but applied LATE in initialisation, for "
              "settings that must not be visible during early setup. "
              "Syntax: VKD3D_UNIX_POST_ENV=\"...\" %command%",
              placeholder='e.g. VAR=value'),

    EnvVarDef("VKD3D_QUEUE_PROFILE", "VKD3D-Proton", "string", "",
              "Writes a queue timeline trace to the given path — submissions, waits and "
              "signals per Vulkan queue. The tool of choice when frametime spikes look "
              "like queue starvation or a serialised submit rather than raw GPU cost. "
              "Syntax: VKD3D_QUEUE_PROFILE=/tmp/queue.json %command%",
              placeholder="e.g. /tmp/queue.json"),

    EnvVarDef("VKD3D_QUEUE_PROFILE_ABSOLUTE", "VKD3D-Proton", "enum", "",
              "Emits queue-profile timestamps as absolute values instead of relative ones, "
              "so the trace lines up with Wine's QPC-relative logs. Only meaningful "
              "together with VKD3D_QUEUE_PROFILE. "
              "Syntax: VKD3D_QUEUE_PROFILE=/tmp/q.json VKD3D_QUEUE_PROFILE_ABSOLUTE=1 %command%",
              options=["1"]),

    EnvVarDef("VKD3D_TIMESTAMP_PROFILE", "VKD3D-Proton", "string", "",
              "Writes GPU timestamp profiling data to the given path — per-pass GPU cost, "
              "as opposed to VKD3D_QUEUE_PROFILE's submission-level view. "
              "Syntax: VKD3D_TIMESTAMP_PROFILE=/tmp/ts.json %command%",
              placeholder="e.g. /tmp/timestamps.json"),

    EnvVarDef("VKD3D_PROFILE_PATH", "VKD3D-Proton", "string", "",
              "Output path for vkd3d-proton's built-in profiling counters (enabled in "
              "builds compiled with profiling support). "
              "Syntax: VKD3D_PROFILE_PATH=/tmp/vkd3d-profile %command%",
              placeholder="e.g. /tmp/vkd3d-profile"),

    EnvVarDef("VKD3D_AUTO_CAPTURE_SHADER", "VKD3D-Proton", "string", "",
              "RenderDoc auto-capture: captures the frame in which the shader with this "
              "hash is used. Requires the RenderDoc layer to be loaded; vkd3d warns and "
              "does nothing when neither this nor VKD3D_AUTO_CAPTURE_COUNTS is set. "
              "Syntax: VKD3D_AUTO_CAPTURE_SHADER=<hash> %command%",
              placeholder="e.g. deadbeefdeadbeef"),

    EnvVarDef("VKD3D_AUTO_CAPTURE_COUNTS", "VKD3D-Proton", "string", "",
              "RenderDoc auto-capture by frame number — a comma-separated list of frame "
              "indices to capture. Can be used on its own or alongside "
              "VKD3D_AUTO_CAPTURE_SHADER. "
              "Syntax: VKD3D_AUTO_CAPTURE_COUNTS=100,200,300 %command%",
              placeholder="e.g. 100,200,300"),

    EnvVarDef("VKD3D_QA_HASHES", "VKD3D-Proton", "string", "",
              "Path to a file of shader hashes for descriptor QA / validation runs. Note "
              "the side effect: setting this (like VKD3D_SHADER_OVERRIDE or "
              "VKD3D_SHADER_DUMP_PATH) forces the pipeline_library_ignore_spirv config "
              "flag on, so the pipeline library stops being reused — expect slower "
              "startup while it is set. "
              "Syntax: VKD3D_QA_HASHES=/tmp/hashes.txt %command%",
              placeholder="e.g. /tmp/hashes.txt"),

    EnvVarDef("VKD3D_BARRIER_HASHES", "VKD3D-Proton", "string", "",
              "Path to a file of shader hashes used by the breadcrumb/barrier debugging "
              "path, for narrowing a GPU hang down to specific barriers. Debug only. "
              "Syntax: VKD3D_BARRIER_HASHES=/tmp/barriers.txt %command%",
              placeholder="e.g. /tmp/barriers.txt"),

    EnvVarDef("VKD3D_SHADER_QUIRKS", "VKD3D-Proton", "string", "",
              "Path to a file of per-shader quirk overrides, applied at shader compile "
              "time. Development aid for testing a workaround before it is baked into "
              "vkd3d-proton's own quirk tables. "
              "Syntax: VKD3D_SHADER_QUIRKS=/tmp/quirks.txt %command%",
              placeholder="e.g. /tmp/quirks.txt"),

    EnvVarDef("VKD3D_BLOOM_BUFFER_SIZE_LOG2", "VKD3D-Proton", "int", "24",
              "Size of the descriptor-QA bloom filter buffer, as a power of two "
              "(24 = 16 MiB, the default). Only allocated when descriptor QA with sync "
              "validation is active; raise it if the QA layer reports collisions. "
              "Syntax: VKD3D_BLOOM_BUFFER_SIZE_LOG2=26 %command%",
              placeholder="e.g. 24 (log2 of bytes)"),

]


# ============================================================================
# NVIDIA OpenGL Environment Variables (__GL_*)
# ============================================================================

NV_ENV_VARS: List[EnvVarDef] = [

    # ── Rendering / Protocol ─────────────────────────────────────────────────
    EnvVarDef("__GL_ALLOW_UNOFFICIAL_PROTOCOL", "NVIDIA __GL", "enum", "",
              "Allows the NVIDIA GLX client to use 'unofficial' GLX protocol extensions for "
              "OpenGL features beyond v1.5. Requires NVIDIA GLX libraries on both client and server.",
              options=["0", "1"]),

    EnvVarDef("__GL_FORCE_INDIRECT", "NVIDIA __GL", "enum", "",
              "Forces the NVIDIA OpenGL driver to use indirect GLX rendering — sends GL commands "
              "over the network to an X server. Workaround for certain compatibility issues. (1 = enable)",
              options=["0", "1"]),

    EnvVarDef("__GL_DISALLOW_SOFTWARE_FALLBACK", "NVIDIA __GL", "enum", "",
              "Prevents the OpenGL driver from falling back to software rendering when hardware "
              "acceleration fails.",
              options=["0", "1"]),

    EnvVarDef("__GL_NO_DSO_FINALIZER", "NVIDIA __GL", "enum", "",
              "Workaround for multithreaded OpenGL apps: forces libGL's DSO finalizer to leave "
              "resources in place on exit so other threads can still call OpenGL safely. "
              "OS reclaims all memory when the process terminates.",
              options=["0", "1"]),

    EnvVarDef("__GL_WRITE_TEXT_SECTION", "NVIDIA __GL", "enum", "",
              "Controls whether the NVIDIA OpenGL driver can write to executable memory sections. "
              "Set to 0 to disable this optimization — workaround for segfaults in some apps.",
              options=["0", "1"]),

    EnvVarDef("__GL_IGNORE_GLSL_EXT_REQS", "NVIDIA __GL", "enum", "",
              "Ignores certain GLSL extension requirements. Useful workaround for specific games "
              "such as Dying Light. (1 = enable)",
              options=["0", "1"]),

    EnvVarDef("__GL_YIELD", "NVIDIA __GL", "enum", "",
              "Specifies what the NVIDIA OpenGL driver does when it needs to yield CPU time. "
              "USLEEP changes the yielding behavior to usleep(). Workaround for scheduling problems.",
              options=["USLEEP", "NOTHING"],
              placeholder="e.g. USLEEP"),

    EnvVarDef("__GL_SYNC_DISPLAY_DEVICE", "NVIDIA __GL", "string", "",
              "Specifies which display connector to synchronize rendering to (VSync target). "
              "Use in multi-monitor setups to eliminate tearing. Also works with Vulkan apps. "
              "Example values: HDMI-0, DP-4, DVI-D-0",
              placeholder="e.g. HDMI-0 or DP-4"),

    # ── Shader Cache ─────────────────────────────────────────────────────────
    EnvVarDef("__GL_SHADER_DISK_CACHE_PATH", "NVIDIA __GL", "string", "",
              "Overrides the directory path for the NVIDIA OpenGL shader disk cache. "
              "By default the cache lives in ~/.nv. Useful to redirect to faster storage or "
              "a shared location across users.",
              placeholder="/path/to/cache/dir"),

    EnvVarDef("__GL_SHADER_DISK_CACHE_APP_NAME", "NVIDIA __GL", "string", "",
              "Specifies an application name for the shader disk cache, creating app-specific "
              "cache entries. Prevents cache key collisions between different applications.",
              placeholder="MyAppName"),

    EnvVarDef("__GL_SHADER_DISK_CACHE_READ_ONLY", "NVIDIA __GL", "enum", "",
              "Makes the shader disk cache read-only. When set, the driver will not write "
              "new compiled shader entries to the cache — useful for locked/shared deployments.",
              options=["0", "1"]),

    EnvVarDef("__GL_SHADER_DISK_CACHE_READ_ONLY_APP_NAME", "NVIDIA __GL", "string", "",
              "Specifies an application name for a read-only shader disk cache. "
              "Used in conjunction with __GL_SHADER_DISK_CACHE_READ_ONLY.",
              placeholder="MyAppName"),

    EnvVarDef("__GL_SHADER_DISK_CACHE_SKIP_CLEANUP", "NVIDIA __GL", "enum", "",
              "Prevents the NVIDIA driver from cleaning up (evicting) old shader disk cache entries. "
              "Can help reduce shader compilation stuttering by keeping all cached shaders intact. "
              "(1 = skip cleanup)",
              options=["0", "1"]),

    # ── Image Sharpening ──────────────────────────────────────────────────────
    EnvVarDef("__GL_SHARPEN_ALLOW", "NVIDIA __GL", "enum", "",
              "Allows the NVIDIA Image Sharpening feature to be used by this process. "
              "Must be enabled before __GL_SHARPEN_ENABLE takes effect.",
              options=["0", "1"]),

    EnvVarDef("__GL_SHARPEN_ENABLE", "NVIDIA __GL", "enum", "",
              "Enables NVIDIA Image Sharpening for OpenGL and Vulkan games. "
              "Improves clarity and sharpness. Available since driver 441.41. "
              "Requires __GL_SHARPEN_ALLOW=1.",
              options=["0", "1"]),

    EnvVarDef("__GL_SHARPEN_VALUE", "NVIDIA __GL", "int", "",
              "Controls the sharpening strength. Higher values produce a stronger effect. "
              "Typical range 0–100.",
              placeholder="0-100"),

    EnvVarDef("__GL_SHARPEN_IGNORE_FILM_GRAIN", "NVIDIA __GL", "enum", "",
              "Controls whether the sharpening filter ignores film grain effects. "
              "Prevents the sharpening pass from accentuating synthetic film grain noise.",
              options=["0", "1"]),

    EnvVarDef("__GL_SHARPEN_INDICATOR_ENABLE", "NVIDIA __GL", "enum", "",
              "Shows an on-screen indicator when NVIDIA Image Sharpening is active.",
              options=["0", "1"]),

    # ── Added from the NVIDIA 595.58 OpenGL environment-variable README ───────
    EnvVarDef("__GL_LOG_MAX_ANISO", "NVIDIA __GL", "enum", "",
              "Enables automatic anisotropic texture filtering for OpenGL: 0 = none, "
              "1 = 2x, 2 = 4x, 3 = 8x, 4 = 16x.",
              options=["0", "1", "2", "3", "4"]),

    EnvVarDef("__GL_ALLOW_FXAA_USAGE", "NVIDIA __GL", "enum", "",
              "Set to 0 to let an individual application opt out of FXAA when FXAA is "
              "enabled globally in nvidia-settings.",
              options=["0", "1"]),

    EnvVarDef("__GL_HEAP_ALLOC_LIMIT", "NVIDIA __GL", "string", "",
              "Per-process limit on the driver's heap allocations, written as BYTES with an "
              "optional case-insensitive suffix (k, kB, M, MB, G, GB); no suffix = bytes. "
              "Values below 12 MB are clamped up to 12 MB.",
              placeholder="e.g. 4G"),

    EnvVarDef("__GL_ConformantBlitFramebufferScissor", "NVIDIA __GL", "enum", "",
              "glBlitFramebuffer() scissor test: enabled by default (spec-conformant). Set "
              "0 to disable it for old applications that relied on the previous behaviour.",
              options=["0", "1"]),

    EnvVarDef("__GL_SORT_FBCONFIGS", "NVIDIA __GL", "enum", "",
              "Set to 0 to return GLX FBConfigs in the order the X server reports them "
              "instead of the driver's sorted order.",
              options=["0", "1"]),

    # ── Display / HUD ─────────────────────────────────────────────────────────
    EnvVarDef("__GL_SHOW_GRAPHICS_OSD", "NVIDIA __GL", "enum", "",
              "Shows an on-screen display (OSD) indicator for the graphics API currently in use. "
              "Can also be set via nvidia-settings or an application profile.",
              options=["0", "1"]),

    # ── App Profile / Platform ────────────────────────────────────────────────
    EnvVarDef("__GL_APPLICATION_PROFILE", "NVIDIA __GL", "string", "",
              "Specifies an application profile name to load. Application profiles override "
              "global and base profile settings. The driver loads these on process init.",
              placeholder="ProfileName"),

    EnvVarDef("__GL_SELINUX_BOOLEANS", "NVIDIA __GL", "enum", "",
              "Controls SELinux policy detection for the NVIDIA OpenGL driver. "
              "Some fallback allocation methods may be prohibited under SELinux; "
              "this variable allows manual override of the detection.",
              options=["0", "1"]),

    EnvVarDef("__GL_MAYA_OPTIMIZE", "NVIDIA __GL", "enum", "",
              "Enables/disables OpenGL optimizations for Autodesk Maya. "
              "The NVIDIA Linux driver 295.59 disabled certain OpenGL optimizations that "
              "affect Maya performance. Set to 1 to re-enable if you experience performance loss.",
              options=["0", "1"]),

    # ── Debug / Event Logging ─────────────────────────────────────────────────
    EnvVarDef("__GL_EVENT_LOGFILE", "NVIDIA __GL", "string", "",
              "Specifies a file path for NVIDIA OpenGL event logging. "
              "Used for debugging and troubleshooting by NVIDIA support.",
              placeholder="/tmp/gl_events.log"),

    EnvVarDef("__GL_EVENT_LOGLEVEL", "NVIDIA __GL", "enum", "",
              "Controls the verbosity level of NVIDIA OpenGL event logging. "
              "TRACE is most verbose; OFF disables logging entirely.",
              options=["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "CRITICAL", "OFF"]),

    EnvVarDef("__GL_EXPERT_DETAIL_LEVEL", "NVIDIA __GL", "int", "",
              "Controls the level of detail for GLExpert debugging output. "
              "GLExpert is part of the instrumented driver providing real-time OpenGL runtime info. "
              "Higher values produce more detailed output.",
              placeholder="0-5"),

    EnvVarDef("__GL_EXPERT_OUTPUT_MASK", "NVIDIA __GL", "string", "",
              "Bitmask controlling where GLExpert debugging output is sent "
              "(e.g. console, stdout, debugger). Used together with __GL_EXPERT_REPORT_MASK.",
              placeholder="0x01 (stdout)"),

    EnvVarDef("__GL_EXPERT_REPORT_MASK", "NVIDIA __GL", "string", "",
              "Bitmask filtering which types of GLExpert debugging reports are generated. "
              "Used together with __GL_EXPERT_OUTPUT_MASK.",
              placeholder="0xFF (all)"),

    EnvVarDef("__GL_DEBUG_FILENAME", "NVIDIA __GL", "string", "",
              "Specifies a filename for debug trace output from the NVIDIA OpenGL driver. "
              "Used in conjunction with __GL_DEBUG to redirect trace output.",
              placeholder="/tmp/gl_debug.log"),

    EnvVarDef("__GL_FIX_VIEWPERF2020_BUFFER_NAME_BUG", "NVIDIA __GL", "enum", "",
              "Fixes a specific bug in SPEC Viewperf 2020 benchmarks related to buffer naming. "
              "Viewperf data sets are GL API traces from CAD apps; the bug caused rendering "
              "to appear darker than expected.",
              options=["0", "1"]),

    EnvVarDef("__GL_DOOM3", "NVIDIA __GL", "enum", "",
              "Legacy variable for the Doom 3 game. A driver bug (fixed in 319.32) caused "
              "crashes when this was set. Kept here for reference; not needed on modern drivers.",
              options=["0", "1"]),

    EnvVarDef("__GL_13ebad", "NVIDIA __GL", "enum", "",
              "idTech engine VRAM placement workaround. Tells the NVIDIA Vulkan driver to "
              "override application-requested memory locations and force performance-critical "
              "resources into video memory. First needed for Doom Eternal (2020), later "
              "confirmed for Indiana Jones and the Great Circle (Dec 2024). Workaround "
              "confirmed by an NVIDIA engineer on the developer forum; planned for "
              "auto-enablement in a future driver release. Only effective on idTech-based "
              "games with the proprietary NVIDIA Linux Vulkan driver. "
              "Steam launch options: __GL_13ebad=0x1 %command%",
              options=["0x1"]),
    # ── Latency / Frame Queue ────────────────────────────────────────────────
    EnvVarDef("__GL_MaxFramesAllowed", "NVIDIA __GL", "enum", "",
              "Caps how many frames the OpenGL driver will queue ahead of the display "
              "(pre-rendered frames). Defaults to 2. Set to 1 for the Linux equivalent of "
              "Windows' NVIDIA 'Low Latency Mode: On' — noticeably reduces input lag and "
              "frame-pacing jitter in compositors/games that rely on it. 0 aims for the "
              "'Ultra' equivalent but is reported to not fully work on Linux.",
              options=["0", "1", "2"]),
    EnvVarDef("__GL_THREADED_OPTIMIZATIONS", "NVIDIA __GL", "enum", "",
              "Offloads some of the OpenGL driver's CPU-side work to a separate worker "
              "thread. Helps CPU-bound OpenGL titles; can hurt titles that make heavy "
              "synchronous calls like glGet*. Requires the app to link pthreads (use "
              "LD_PRELOAD=libpthread.so.0 if it doesn't). Enabled by default under some "
              "conditions with self-disable if it isn't helping; this forces it on.",
              options=["0", "1"]),
    EnvVarDef("__GL_SYNC_TO_VBLANK", "NVIDIA __GL", "enum", "",
              "Classic OpenGL VSync toggle. 0 lets glXSwapBuffers return immediately "
              "without waiting for vblank (tearing possible, lowest latency); 1 (default) "
              "syncs every swap to the display's vertical refresh.",
              options=["0", "1"]),
    # ── G-SYNC / VRR ─────────────────────────────────────────────────────────
    EnvVarDef("__GL_GSYNC_ALLOWED", "NVIDIA __GL", "enum", "",
              "Allows (1) or blocks (0) G-SYNC/variable refresh rate on a G-SYNC-capable "
              "display for OpenGL applications. NOTE: only affects OpenGL — Vulkan/DXVK/"
              "VKD3D-Proton titles ignore this entirely; VRR for those is controlled at the "
              "compositor/display-server level instead.",
              options=["0", "1"]),
    EnvVarDef("__GL_VRR_ALLOWED", "NVIDIA __GL", "enum", "",
              "Same G-SYNC/adaptive-sync allow/block toggle as __GL_GSYNC_ALLOWED, under the "
              "more generic 'VRR' name. Same OpenGL-only caveat applies — has no effect on "
              "Vulkan applications.",
              options=["0", "1"]),
    # ── Shader Disk Cache ────────────────────────────────────────────────────
    EnvVarDef("__GL_SHADER_DISK_CACHE", "NVIDIA __GL", "enum", "",
              "Master on/off switch for the NVIDIA OpenGL driver's shader disk cache. The "
              "other __GL_SHADER_DISK_CACHE_* variables only take effect while this is on "
              "(on by default when a cache directory is resolvable).",
              options=["0", "1"]),
    EnvVarDef("__GL_SHADER_DISK_CACHE_SIZE", "NVIDIA __GL", "int", "",
              "Maximum size in bytes of the NVIDIA OpenGL shader disk cache. Once exceeded, "
              "older entries are evicted. Raise this if a big/long game library keeps "
              "thrashing the cache and causing re-compiles.",
              placeholder="e.g. 1073741824 (1 GiB)"),
    # ── Antialiasing ─────────────────────────────────────────────────────────
    EnvVarDef("__GL_FSAA_MODE", "NVIDIA __GL", "int", "",
              "Forces a specific driver-level full-screen antialiasing mode, using the same "
              "integer values as `nvidia-settings --assign FSAA=N`. Run `nvidia-settings "
              "--query fsaa` to list the modes your GPU supports. Only takes effect for "
              "modes the app doesn't already control itself (see FSAAAppControlled).",
              placeholder="e.g. 5"),
]

# ============================================================================
# NVIDIA PRIME / Hybrid GPU Environment Variables (Optimus laptops)
# ============================================================================

NVIDIA_PRIME_ENV_VARS: List[EnvVarDef] = [
    EnvVarDef("__NV_PRIME_RENDER_OFFLOAD", "NVIDIA PRIME", "enum", "",
              "On a hybrid laptop (iGPU driving the display, NVIDIA dGPU idle), forces the "
              "OpenGL/EGL side of an application to render on the NVIDIA GPU via PRIME "
              "render offload instead of the iGPU. Pair with __GLX_VENDOR_LIBRARY_NAME=nvidia "
              "(System / Loader category) and __VK_LAYER_NV_optimus below for the Vulkan half. "
              "Directly relevant on this machine's Ryzen iGPU + RTX 4080 Mobile combo.",
              options=["0", "1"]),
    EnvVarDef("__NV_PRIME_RENDER_OFFLOAD_PROVIDER", "NVIDIA PRIME", "string", "",
              "Only needed with more than one discrete GPU to offload to: names the specific "
              "Xorg PRIME provider to render on, e.g. 'NVIDIA-G0'. Find yours with "
              "`xrandr --listproviders`. Leave unset on a normal single-dGPU laptop.",
              placeholder="e.g. NVIDIA-G0"),
    EnvVarDef("__VK_LAYER_NV_optimus", "NVIDIA PRIME", "enum", "",
              "Loads NVIDIA's Vulkan PRIME-offload layer so a Vulkan app actually picks the "
              "NVIDIA GPU. Needed because Vulkan apps otherwise just take the first device "
              "the loader enumerates, which on a hybrid laptop is often the iGPU. "
              "'NVIDIA_only' is the value actually used, not a boolean.",
              options=["NVIDIA_only"]),
    EnvVarDef("DRI_PRIME", "NVIDIA PRIME", "string", "",
              "The Mesa-side equivalent PRIME offload switch (1 = use the secondary GPU, or "
              "a specific 'pci-xxxx_xx_xx_x' bus address). Mainly relevant here if you're "
              "running the open-source Nouveau driver on the RTX 4080 Mobile instead of the "
              "proprietary driver — has no effect on the proprietary NVIDIA driver path.",
              placeholder="1 or pci-0000_01_00_0"),
]

FLM_ENV_VARS: List[EnvVarDef] = [
    EnvVarDef("ENABLE_LAYER_cpu_flip_meter", "vk_flip_meter", "enum", "",
              "Activates the Vulkan implicit layer. This is the enable_environment key from "
              "the layer manifest; without it the layer stays inactive even if loaded. "
              "Syntax: ENABLE_LAYER_cpu_flip_meter=1 %command%",
              options=["1"]),

    EnvVarDef("DISABLE_LAYER_cpu_flip_meter", "vk_flip_meter", "enum", "",
              "Globally disables the layer (e.g. to turn off a system-wide implicit layer "
              "for one specific game). "
              "Syntax: DISABLE_LAYER_cpu_flip_meter=1 %command%",
              options=["1"]),

    EnvVarDef("FLM_MODE", "vk_flip_meter", "enum", "auto",
              "v3.0: the layer configures itself — frame-generation multiplier, floor "
              "ratio, spin margin, hitch handling and FIFO fixed-refresh/VRR detection are "
              "all automatic. auto: FPS cap if FLM_TARGET_FPS>0, otherwise the floor pacer "
              "wherever it can help (presentWait + MAILBOX/IMMEDIATE, or FIFO whose "
              "measured cadence is VRR). latency: like auto with a looser floor and faster "
              "hitch recovery. present: like auto, but also paces FIFO swapchains judged "
              "fixed-refresh. cap: limiter only (needs FLM_TARGET_FPS). off: layer loaded "
              "but inactive (A/B baseline). The legacy FLM_PROFILE=vrr|mfg|latency|cap|off "
              "and FLM_MODE=limiter are still accepted as aliases. Hot-reloadable. "
              "Syntax: FLM_MODE=latency %command%",
              options=["auto", "latency", "present", "cap", "off"]),

    EnvVarDef("FLM_TARGET_FPS", "vk_flip_meter", "int", "0",
              ">0 = FPS cap (absolute-timeline limiter, no presentWait needed, works on "
              "every driver). 0 = natural cadence: the floor pacer smooths delivery "
              "without capping. Hot-reloadable. "
              "Syntax: FLM_TARGET_FPS=120 %command%",
              placeholder="e.g. 120"),

    EnvVarDef("FLM_FLOOR_RATIO", "vk_flip_meter", "int", "",
              "Optional override of the floor pacer's base ratio (500-1000; 1000 = one "
              "full slot). Unset = mode default (auto 850, latency 780). The closed loop "
              "still adjusts around it per frame-generation multiplier, and the ratchet "
              "guard still keeps the floor below the slot. Higher = flatter, lower = less "
              "held. Leave unset unless A/B data says otherwise. Hot-reloadable. "
              "Syntax: FLM_FLOOR_RATIO=900 %command%",
              placeholder="unset = auto"),

    EnvVarDef("FLM_MFG_MULTIPLIER", "vk_flip_meter", "enum", "0",
              "Frame-generation multiplier. 0 = auto-detect (default; re-checked "
              "periodically, follows in-game changes). 1-4 = force. Load-time only. "
              "Syntax: FLM_MFG_MULTIPLIER=4 %command%",
              options=["0", "1", "2", "3", "4"]),

    EnvVarDef("FLM_MEASURE_CPU", "vk_flip_meter", "string", "",
              "CPU affinity of the measurement thread — comma list of cores/ranges, "
              "e.g. '0-3,8'. Useful with CCD isolation. Load-time only. "
              "Syntax: FLM_MEASURE_CPU=0-3 %command%",
              placeholder="e.g. 0-3"),

    EnvVarDef("FLM_RT_PRIORITY", "vk_flip_meter", "int", "0",
              "SCHED_FIFO priority of the measurement thread (1-99, needs CAP_SYS_NICE). "
              "0 = normal scheduling. Load-time only. "
              "Syntax: FLM_RT_PRIORITY=40 %command%",
              placeholder="e.g. 40"),

    EnvVarDef("FLM_LOG_LEVEL", "vk_flip_meter", "enum", "WARN",
              "Log verbosity (default WARN). INFO shows the resolved config, MFG "
              "multiplier changes and the FIFO/VRR verdict; DEBUG is near per-frame. "
              "Removed v2.x variables are reported once at WARN. Hot-reloadable. "
              "Syntax: FLM_LOG_LEVEL=INFO %command%",
              options=["DEBUG", "INFO", "WARN", "ERROR"]),

    EnvVarDef("FLM_LOG_FILE", "vk_flip_meter", "string", "",
              "Log file path (default stderr, which Steam/Lutris often swallow). "
              "Load-time only. "
              "Syntax: FLM_LOG_LEVEL=INFO FLM_LOG_FILE=/tmp/flm.log %command%",
              placeholder="e.g. /tmp/flm.log"),

    EnvVarDef("FLM_STATS", "vk_flip_meter", "enum", "0",
              "1 = summary line every 5 s at INFO: avg, p99, max interval, fake/hitch "
              "counts, detected multiplier, effective floor ratio (0 = pacer not running "
              "on this swapchain) and the FIFO verdict. Load-time only. "
              "Syntax: FLM_LOG_LEVEL=INFO FLM_STATS=1 %command%",
              options=["0", "1"]),

    EnvVarDef("FLM_CSV", "vk_flip_meter", "string", "",
              "Per-flip measurement dump for A/B analysis (columns flip_ns, interval_ns, "
              "is_fake, is_hitch, slot, mfg, slot_mean_ns, pacing). Survives swapchain "
              "recreation (appends), flushed every 5 s. Load-time only. "
              "Syntax: FLM_MODE=off FLM_CSV=/tmp/off.csv %command%",
              placeholder="e.g. /tmp/flm.csv"),

    EnvVarDef("FLM_CONFIG", "vk_flip_meter", "string", "",
              "KEY=VALUE file re-read on SIGUSR1 for live tuning. Hot-reloadable keys: "
              "FLM_MODE, FLM_TARGET_FPS, FLM_FLOOR_RATIO, FLM_LOG_LEVEL (FLM_PROFILE "
              "alias too). Each reload starts from defaults → env → file, so deleting a "
              "line reverts it. "
              "Syntax: FLM_CONFIG=/tmp/flm.conf %command%  →  then: "
              "echo 'FLM_MODE=latency' > /tmp/flm.conf && kill -USR1 $(pgrep -f game)",
              placeholder="e.g. /tmp/flm.conf"),
]

# ============================================================================
# Proton Environment Variables (PROTON_* runtime config, Valve + Proton-CachyOS)
# ============================================================================

PROTON_ENV_VARS: List[EnvVarDef] = [
    # ── Sync / Scheduling ────────────────────────────────────────────────────
    EnvVarDef("PROTON_USE_NTSYNC", "Proton", "enum", "",
              "Use the kernel-backed ntsync driver for Wine's in-process synchronization "
              "primitives instead of Esync/Fsync. Lower CPU overhead and more consistent "
              "timing than Fsync in titles that need it. On newer Proton-CachyOS/Valve "
              "Proton 11 builds ntsync is already the default and this variable has been "
              "removed there — use PROTON_NO_NTSYNC=1 on those builds to go back to Fsync.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_NTSYNC", "Proton", "enum", "",
              "Disable ntsync (the default kernel sync driver on Proton 11 / current "
              "Proton-CachyOS) and fall back to Fsync. Useful for the rare title that "
              "misbehaves specifically under ntsync.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_ESYNC", "Proton", "enum", "",
              "Do not use eventfd-based in-process synchronization primitives (disables Esync).",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_FSYNC", "Proton", "enum", "",
              "Do not use futex-based in-process synchronization primitives (disables Fsync). "
              "Automatically disabled anyway on systems without FUTEX_WAIT_MULTIPLE support.",
              options=["0", "1"]),
    EnvVarDef("PROTON_PRIORITY_HIGH", "Proton", "enum", "",
              "Not part of any official Proton, Proton-CachyOS or Proton-GE variable table — "
              "no confirmed source reads this exact name. Community shorthand for 'give the "
              "game process a higher scheduling priority' is usually done via a launch wrapper "
              "(gamemoderun, CachyOS's game-performance) rather than an env var read by Proton "
              "itself. Kept here so the value is captured, but verify it actually does anything "
              "on your setup before relying on it.",
              options=["0", "1"]),
    # ── HDR / Display ────────────────────────────────────────────────────────
    EnvVarDef("PROTON_ENABLE_HDR", "Proton", "enum", "",
              "Legacy HDR toggle. Retired in current Proton-CachyOS in favour of DXVK_HDR "
              "(see the DXVK category), but Valve's stable Proton and most Proton-GE builds "
              "still read this name directly. Combine with ENABLE_HDR_WSI if needed.",
              options=["0", "1"]),
    EnvVarDef("ENABLE_HDR_WSI", "Proton", "enum", "",
              "Enables the HDR window-system-integration Vulkan extension path. Needed "
              "alongside DXVK_HDR/PROTON_ENABLE_HDR on NVIDIA driver versions older than "
              "595.x, or together with a compositor-side HDR layer (e.g. vk-hdr-layer on "
              "KDE 6) when the compositor itself needs to be told HDR is coming.",
              options=["0", "1"]),
    EnvVarDef("PROTON_ENABLE_WAYLAND", "Proton", "enum", "",
              "Enable Wine's native winewayland.drv instead of XWayland/winex11.drv. "
              "Experimental — can improve latency and frame pacing and is required for "
              "HDR without Gamescope, but currently breaks the Steam Overlay and Steam "
              "Input in several builds. Alias: PROTON_USE_WAYLAND.",
              options=["0", "1"]),
    # ── NVIDIA / NGX ─────────────────────────────────────────────────────────
    EnvVarDef("PROTON_HIDE_NVIDIA_GPU", "Proton", "enum", "",
              "Force NVIDIA GPUs to always be reported as AMD GPUs. Some games require this "
              "if they depend on Windows-only NVIDIA driver functionality. See also DXVK's "
              "nvapiHack config, which only affects reporting from Direct3D.",
              options=["0", "1"]),
    EnvVarDef("PROTON_ENABLE_NVAPI", "Proton", "enum", "",
              "Enable NVIDIA's NVAPI GPU support library inside the Wine prefix. Needed on "
              "older Proton (<= 9) and some forks. Valve Proton 10 copies nvapi64.dll into "
              "the prefix by default unless the game is marked 'disablenvapi' — there, use "
              "PROTON_FORCE_NVAPI=1 to override such a per-game block instead. "
              "Usually paired with PROTON_HIDE_NVIDIA_GPU=0.",
              options=["0", "1"]),
    EnvVarDef("PROTON_DISABLE_NVAPI", "Proton", "enum", "",
              "Disable NVIDIA's NVAPI GPU support library. Occasionally fixes crashes or low "
              "performance in titles that have buggy NVAPI code paths on Linux.",
              options=["0", "1"]),
    EnvVarDef("PROTON_FORCE_NVAPI", "Proton", "enum", "",
              "Proton 10: forces NVAPI on even for titles Proton disables it for by "
              "default (maps to the 'forcenvapi' compat flag, which wins over "
              "'disablenvapi'). Use when DLSS/Reflex is missing in a game Proton "
              "blocks NVAPI for.",
              options=["1"]),
    EnvVarDef("PROTON_HIDE_APU", "Proton", "enum", "",
              "Hides an AMD APU's integrated GPU from the game (Proton sets "
              "WINE_HIDE_APU=1). On hybrid laptops (Ryzen iGPU + NVIDIA dGPU) this "
              "stops games that enumerate adapters from picking or mis-detecting the "
              "iGPU. Proton applies it automatically for a few titles (e.g. Deathloop).",
              options=["1"]),
    EnvVarDef("PROTON_LIMIT_RESOLUTIONS", "Proton", "int", "",
              "Caps how many display modes Wine reports to the game. Some titles crash "
              "or misbehave with very long mode lists on high-refresh panels; Proton "
              "sets 16/32 automatically for e.g. Sekiro, Nier: Automata, Dark Souls III.",
              placeholder="e.g. 32"),
    EnvVarDef("PROTON_CPU_TOPOLOGY", "Proton", "string", "",
              "Proton alias for WINE_CPU_TOPOLOGY ('N:i,j,k,...'); copied into "
              "WINE_CPU_TOPOLOGY by the Proton script. Useful for pinning a game to "
              "specific cores (e.g. one CCD / the V-Cache CCD).",
              placeholder="e.g. 8:0,1,2,3,4,5,6,7"),
    EnvVarDef("PROTON_NVAPI_BYPASS", "Proton", "enum", "",
              "Not a documented Proton/DXVK/VKD3D variable — no official source for this "
              "exact name was found. Most likely a mix-up with PROTON_DISABLE_NVAPI or "
              "DXVK_ENABLE_NVAPI (see the DXVK category). Kept here so the typed value is "
              "captured, but treat it as unconfirmed.",
              options=["0", "1"]),
    EnvVarDef("PROTON_ENABLE_NGX_UPDATER", "Proton", "enum", "",
              "Enables NVIDIA's NGX Updater inside the Wine prefix, letting NGX Core download "
              "newer DLSS/DLSS-G DLLs at runtime instead of using the ones shipped with the "
              "game. This is the one-launch-option way to force a newer DLSS preset via "
              "DXVK_NVAPI_DRS_NGX_* (see DXVK-NVAPI category). Known to cause 2nd-run crashes "
              "in a few Unreal Engine titles — disable it again if that happens.",
              options=["0", "1"]),
    EnvVarDef("PROTON_DLSS_UPGRADE", "Proton", "string", "",
              "Automatically download and use a newer nvngx_dlss(d|g).dll than the one the "
              "game ships with. Set to 1 for the latest known-good version, or a specific "
              "version string (e.g. '310.2') to pin one. Also sets DXVK_NVAPI_DRS_SETTINGS "
              "to the latest preset unless you override it yourself.",
              placeholder="1 or e.g. 310.2"),
    EnvVarDef("PROTON_NVIDIA_LIBS", "Proton", "enum", "",
              "Enable alternative NVIDIA library shims (nvcuda, nvenc, nvml, nvoptix) missing "
              "from stock Proton. Needed for things like hardware-accelerated PhysX. Only "
              "enable when a game actually needs it — incompatible with PROTON_USE_WOW64=1.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NVIDIA_LIBS_NO_32BIT", "Proton", "enum", "",
              "Use together with PROTON_NVIDIA_LIBS to restrict the shims to 64-bit only. "
              "Fixes bad performance/crashes on RTX 4000/5000-series cards when the 32-bit "
              "nvidia-libs shims get loaded for a 32-bit game.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NVIDIA_NVCUDA", "Proton", "enum", "",
              "Enable only the alternative nvcuda.dll shim from nvidia-libs, without pulling "
              "in nvenc/nvml/nvoptix too. Narrower alternative to PROTON_NVIDIA_LIBS.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NVIDIA_NVENC", "Proton", "enum", "",
              "Enable only the alternative NVENC hardware-encode shim from nvidia-libs. "
              "Relevant for in-game recording/streaming pipelines that call NVENC directly.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NVIDIA_NVML", "Proton", "enum", "",
              "Enable only the NVML shim from nvidia-libs (GPU temperature/utilization "
              "queries). Lets in-game or overlay tools read real GPU stats. Enabled by "
              "default in Proton-CachyOS.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NVIDIA_NVOPTIX", "Proton", "enum", "",
              "Enable only the OptiX ray-tracing shim from nvidia-libs. Needed by the rare "
              "title that uses NVIDIA OptiX directly rather than DXR/VKD3D-Proton's "
              "raytracing path.",
              options=["0", "1"]),
    EnvVarDef("PROTON_DLSS_INDICATOR", "Proton", "enum", "",
              "Show a small on-screen DLSS status overlay confirming DLSS is actually active "
              "and which mode it's running in — the same overlay toggled by "
              "DXVK_NVAPI_SET_NGX_DEBUG_OPTIONS=DLSSIndicator=1024 (DXVK-NVAPI category), "
              "just as a one-flag shortcut.",
              options=["0", "1"]),
    # ── Renderer / Latency ───────────────────────────────────────────────────
    EnvVarDef("PROTON_USE_WINED3D", "Proton", "enum", "",
              "Use OpenGL-based wined3d instead of Vulkan-based DXVK for d3d11/d3d10/d3d9. "
              "Rarely an improvement, but occasionally works around a DXVK-specific bug.",
              options=["0", "1"]),
    EnvVarDef("PROTON_DXVK_SAREK", "Proton", "enum", "",
              "[proton-cachyos] Switches the DXVK shipped with proton-cachyos over to "
              "DXVK-Sarek, a Vulkan 1.1/1.2 build maintained off the 1.10.x branch for GPUs "
              "that no longer meet upstream DXVK's requirements (and for Box64/FEX on "
              "Mali/Adreno). Sarek is the officially supported way to use it. On current "
              "hardware there is no reason to enable this — upstream DXVK is newer — "
              "except to reach Sarek-only options such as dyasync (DXVK_DISABLE_DYASYNC) "
              "or DXVK_ALL_CORES. Mutually exclusive in practice with "
              "PROTON_DXVK_LOWLATENCY: both replace the same DXVK. "
              "Syntax: PROTON_DXVK_SAREK=1 %command%",
              options=["1"]),

    EnvVarDef("PROTON_DXVK_LOWLATENCY", "Proton", "enum", "",
              "Enable the dxvk-low-latency fork (Proton-CachyOS/community builds), which adds "
              "low-latency frame pacing on top of DXVK to reduce input lag and improve frame "
              "pacing consistency. Mainly worth it in single-player titles.",
              options=["0", "1"]),
    EnvVarDef("PROTON_DXVK_LLASYNC", "Proton", "string", "",
              "Not a documented Proton/Proton-CachyOS variable under this exact spelling. "
              "Most likely intended as PROTON_DXVK_LOWLATENCY (low-latency frame pacing) or "
              "the now-removed PROTON_DXVK_GPLASYNC (old async-compile patch). Kept here so "
              "the typed value is captured, but treat it as unconfirmed.",
              placeholder="likely meant PROTON_DXVK_LOWLATENCY"),
    EnvVarDef("PROTON_VKD3D_HEAP", "Proton", "enum", "",
              "Retired in current Proton-CachyOS with no replacement. Historically toggled an "
              "alternate vkd3d-proton heap allocation strategy. Kept here for older builds.",
              options=["0", "1"]),
    EnvVarDef("PROTON_LOCAL_SHADER_CACHE", "Proton", "enum", "",
              "Enable a per-game shader cache even when Steam's own 'Shader Pre-Caching' is "
              "off, mimicking it by keeping each game's cache under "
              "<steamlibrary>/shadercache/<appid>. Does not precompile shaders ahead of time — "
              "it only isolates each game's cache.",
              options=["0", "1"]),
    # ── Input / Window ───────────────────────────────────────────────────────
    EnvVarDef("PROTON_PREFER_SDL", "Proton", "enum", "",
              "Use SDL for controller input instead of HIDRAW/Steam Input. Can help controller "
              "detection issues under winewayland.drv. Alias: PROTON_USE_SDL.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_WM_DECORATION", "Proton", "enum", "",
              "Disable window decorations drawn by the window manager, letting Wine draw its "
              "own decorations instead. Occasionally fixes windowed-mode focus/resize glitches.",
              options=["0", "1"]),
    EnvVarDef("PROTON_FORCE_LARGE_ADDRESS_AWARE", "Proton", "enum", "",
              "Force Wine to enable the LARGE_ADDRESS_AWARE flag for all executables, letting "
              "32-bit processes use more than 2 GiB of address space. Enabled by default.",
              options=["0", "1"]),
    EnvVarDef("PROTON_SET_GAME_DRIVE", "Proton", "enum", "",
              "Create an S: drive inside the Wine prefix pointing at the Steam library that "
              "contains the game. Some titles look for their install on a specific drive letter.",
              options=["0", "1"]),
    # ── Meta / Compatibility ─────────────────────────────────────────────────
    EnvVarDef("PROTON_ADD_CONFIG", "Proton", "flags", "",
              "[Proton-EM / Proton-CachyOS] Comma-separated shortcut list that expands to "
              "several other variables at once, e.g. PROTON_ADD_CONFIG=wayland,ntsync is the "
              "same as setting PROTON_USE_WAYLAND=1 PROTON_USE_NTSYNC=1. Handy for combining "
              "several of the toggles above without a wall of launch options.",
              options=["wayland", "ntsync", "nontsync", "sdlinput", "wow64"]),
    EnvVarDef("PROTON_USE_WOW64", "Proton", "enum", "",
              "[GE-Proton / Proton-EM / Proton-CachyOS, experimental] Run the game through "
              "Wine's newer WoW64 architecture (32-bit game, 64-bit Unix process via thunks) "
              "instead of a traditional 32-bit process. Some 32-bit titles report smoother "
              "frame timings with this on; others are still rough since it's experimental. "
              "Incompatible with PROTON_NVIDIA_LIBS.",
              options=["0", "1"]),
    EnvVarDef("PROTON_HEAP_DELAY_FREE", "Proton", "enum", "",
              "Delay freeing some memory instead of releasing it immediately, to work around "
              "application use-after-free bugs. A stability tweak more than a speed one, but "
              "avoids the stutter/crash a UAF bug would otherwise cause mid-game.",
              options=["0", "1"]),
    # ── Debug ────────────────────────────────────────────────────────────────
    EnvVarDef("PROTON_LOG", "Proton", "enum", "",
              "Dump a debug log to $PROTON_LOG_DIR/steam-$APPID.log. Set to 1 for the default "
              "WINEDEBUG channels, or a string to append extra channels to the default set.",
              options=["0", "1"]),
    EnvVarDef("PROTON_LOG_DIR", "Proton", "string", "",
              "Directory to write PROTON_LOG output into. Defaults to your home directory.",
              placeholder="/path/to/log/dir"),
    EnvVarDef("PROTON_DEBUG_DIR", "Proton", "string", "",
              "Root directory Proton writes its per-game debug helper scripts into "
              "(proton_$USER/ subfolder, e.g. gdb_run for attaching a debugger). "
              "Defaults to /tmp. Only useful together with debugging workflows; has no "
              "effect on normal gameplay.",
              placeholder="/tmp"),
    # ── Anti-Cheat Runtimes ──────────────────────────────────────────────────
    EnvVarDef("PROTON_EAC_RUNTIME", "Proton", "string", "",
              "Path to the 'Proton EasyAntiCheat Runtime' Steam depot, needed by some "
              "EAC-protected games that don't auto-detect it (install the runtime via "
              "Steam's 'Tools' category first, then point this at its folder). Usually not "
              "needed manually when the game's Steam depot already declares the dependency.",
              placeholder='"/home/user/.steam/steam/steamapps/common/Proton EasyAntiCheat Runtime/"'),
    EnvVarDef("PROTON_BATTLEYE_RUNTIME", "Proton", "string", "",
              "Path to the 'Proton BattlEye Runtime' Steam depot, needed by some "
              "BattlEye-protected games that don't auto-detect it. Same idea as "
              "PROTON_EAC_RUNTIME — install the Steam Tools depot, then point here.",
              placeholder='"/home/user/.steam/steam/steamapps/common/Proton BattlEye Runtime/"'),

    # ── Added from Proton 10 / experimental proton script (2026-09) ────────
    EnvVarDef("PROTON_DXVK_D3D8", "Proton", "enum", "",
              "Use DXVK's d3d8 implementation instead of wined3d for Direct3D 8 games.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_D3D10", "Proton", "enum", "",
              "Disable d3d10 / d3d10_1 AND dxgi DLL overrides (blank override = not loaded). "
              "Forces games with another renderer to fall back to it.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_D3D11", "Proton", "enum", "",
              "Disable the d3d11 DLL (blank override). Forces games with a D3D9/D3D12 renderer "
              "to pick another API.",
              options=["0", "1"]),
    EnvVarDef("PROTON_USE_WINED3D11", "Proton", "enum", "",
              "Use wined3d (OpenGL) for d3d11 only, keeping DXVK for the rest. Debug/compat.",
              options=["0", "1"]),
    EnvVarDef("PROTON_HEAP_ZERO_MEMORY", "Proton", "enum", "",
              "Zero all Win32 heap allocations (sets WINE_HEAP_ZERO_MEMORY=1). Fixes crashes "
              "in games that read uninitialised heap memory; small CPU cost.",
              options=["0", "1"]),
    EnvVarDef("PROTON_LIMIT_ADDRESS_SPACE", "Proton", "enum", "",
              "Limit the process's usable virtual address space (read by Proton's Wine). "
              "Proton auto-sets it for AppIDs 1282270 and 2963870; try it for games that crash "
              "when given a very large address space.",
              options=["0", "1"]),
    EnvVarDef("PROTON_USE_XALIA", "Proton", "enum", "",
              "Xalia: controller-navigable UI for launchers/dialogs without gamepad support. "
              "Proton enables it by default (only for supported apps). Set 0 if it grabs "
              "input or adds an overlay you don't want.",
              options=["0", "1"]),
    EnvVarDef("PROTON_DISABLE_HIDRAW", "Proton", "string", "",
              "Comma-separated VID/PID list of controllers that must NOT be exposed through "
              "hidraw (they fall back to SDL/evdev). Proton sets Sony DS4/DualSense IDs by "
              "default for some games.",
              placeholder="e.g. 0x054C/0x0CE6,0x054C/0x0DF2"),
    EnvVarDef("PROTON_CRASH_REPORT_DIR", "Proton", "string", "",
              "Write Wine crash reports (minidumps) to this directory "
              "(sets WINE_CRASH_REPORT_DIR).",
              placeholder="e.g. /tmp/proton-crashes"),

    # ── Added 2026-10 from Valve Proton / Proton-GE / Proton-CachyOS / Proton-EM ──
    # ── Upscaler DLL upgrades & OptiScaler ───────────────────────────────────
    EnvVarDef("PROTON_FSR4_UPGRADE", "Proton", "string", "",
              "[Proton-GE / Proton-CachyOS] Download amdxcffx64.dll and upgrade games that "
              "ship FSR 3.1 to FSR 4. 1 = default version from the upscaler manifest, or "
              "pin a version (e.g. 4.1.1). Opt-in. Needs a GPU/driver with FSR4 support — "
              "AMD RDNA3/4 only; it has no effect on an NVIDIA GPU (use "
              "PROTON_DLSS_UPGRADE there). Replaces the obsolete PROTON_FSR4_RDNA3_UPGRADE.",
              placeholder="1 or e.g. 4.1.1"),
    EnvVarDef("PROTON_FSR4_INDICATOR", "Proton", "enum", "",
              "[Proton-GE / Proton-CachyOS] Show the FSR upscaling and frame-generation "
              "watermarks (shorthand for FSR_WATERMARK=1 and FSR_FG_WATERMARK=1).",
              options=["0", "1"]),
    EnvVarDef("PROTON_FSR3_UPGRADE", "Proton", "string", "",
              "[Proton-GE] Download and use a newer FSR 3.x FidelityFX DLL than the game "
              "ships with. 1 = latest from the manifest, or pin a version string.",
              placeholder="1 or a version"),
    EnvVarDef("PROTON_FFX3_UPGRADE", "Proton", "string", "",
              "[Proton-CachyOS] FidelityFX 3 counterpart of PROTON_FSR3_UPGRADE (used by "
              "the OptiScaler/upgrade path; not described in the fork's README table).",
              placeholder="1 or a version"),
    EnvVarDef("PROTON_FFX4_UPGRADE", "Proton", "string", "",
              "[Proton-CachyOS] FidelityFX 4 counterpart of PROTON_FSR4_UPGRADE (used by "
              "the OptiScaler/upgrade path; not described in the fork's README table).",
              placeholder="1 or a version"),
    EnvVarDef("PROTON_XESS_UPGRADE", "Proton", "string", "",
              "[Proton-GE / Proton-CachyOS] Download and use a newer Intel XeSS DLL than "
              "the game ships with. 1 = latest from the manifest, or pin a version string. "
              "XeSS runs on NVIDIA GPUs through its DP4a path.",
              placeholder="1 or a version"),
    EnvVarDef("PROTON_MLFG_UPGRADE", "Proton", "enum", "",
              "[Proton-GE / Proton-CachyOS] Enable MLFG (ML frame generation) together with "
              "FSR 4 >= 4.0.3. AMD-only; set together with PROTON_FSR4_UPGRADE.",
              options=["0", "1"]),
    EnvVarDef("PROTON_USE_OPTISCALER", "Proton", "enum", "",
              "[Proton-GE / Proton-CachyOS] Inject OptiScaler automatically so games can "
              "swap their upscaler (DLSS / FSR / XeSS) and add frame generation where "
              "the game has no option. The PROTON_<UPSCALER>_UPGRADE variables then choose "
              "the DLL versions. Config files live in "
              "<prefix>/drive_c/windows/system32/umu/.",
              options=["0", "1"]),
    EnvVarDef("PROTON_OPTISCALER_NAME", "Proton", "enum", "",
              "[Proton-CachyOS] File name OptiScaler is injected as. Defaults to dxgi.dll; "
              "switch if the game already uses that DLL name.",
              options=["dxgi.dll", "d3d12.dll", "dbghelp.dll"]),
    EnvVarDef("PROTON_OPTISCALER_CONFIG", "Proton", "string", "",
              "[Proton-CachyOS] Semicolon-separated {section}.{option}={value} entries "
              "written into OptiScaler.ini (only options that already exist in the file). "
              "Changes persist until OptiScaler is upgraded and strip the file's comments. "
              "Example: Upscalers.Dx11Upscaler=fsr31;Upscalers.Dx12Upscaler=dlss",
              placeholder="Upscalers.Dx12Upscaler=dlss"),
    # ── Low-latency forks & legacy API ───────────────────────────────────────
    EnvVarDef("PROTON_VKD3D_LOWLATENCY", "Proton", "enum", "",
              "[Proton-CachyOS] Switch to the vkd3d-low-latency fork of vkd3d-proton "
              "(low-latency frame pacing for Direct3D 12 with a native Reflex "
              "implementation). Companion of PROTON_DXVK_LOWLATENCY for D3D12 titles.",
              options=["0", "1"]),
    EnvVarDef("PROTON_D7VK_DDRAW", "Proton", "enum", "",
              "[Proton-CachyOS] Use ddraw.dll from the D7VK project for DirectX 7-and-older "
              "games instead of Wine's built-in ddraw.",
              options=["0", "1"]),
    EnvVarDef("PROTON_USE_D7VK", "Proton", "enum", "",
              "[Proton-GE] Use D7VK's ddraw implementation for DirectX 7-and-older games "
              "(GE spelling of PROTON_D7VK_DDRAW).",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_D3D9", "Proton", "enum", "",
              "[Proton-GE] Disable the D3D9 DLLs, for games that run better on another API.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_D3D12", "Proton", "enum", "",
              "[Proton-GE] Disable the D3D12 DLLs, so a game with a DX11 fallback uses it "
              "instead of vkd3d-proton.",
              options=["0", "1"]),
    EnvVarDef("LOW_LATENCY_LAYER", "Proton", "enum", "",
              "[Proton-GE] Enable the low_latency_layer Vulkan layer (Korthos-Software), "
              "which provides Reflex-style latency reduction (VK_NV_low_latency2) on "
              "GPUs that lack it natively. Not needed on the proprietary NVIDIA driver.",
              options=["0", "1"]),
    EnvVarDef("ENABLE_VKBASALT", "Proton", "enum", "",
              "[Proton-GE] Load the vkBasalt post-processing Vulkan layer (sharpening, "
              "CAS, SMAA, LUTs). Configure it with VKBASALT_CONFIG_FILE.",
              options=["0", "1"]),
    EnvVarDef("PYROVEIL", "Proton", "enum", "",
              "[Proton-GE] Enable the Pyroveil Vulkan layer, which patches broken "
              "shader/pipeline behaviour per game; the config name goes in PYROVEIL_CONFIG.",
              options=["0", "1"]),
    # ── Compatibility / memory behaviour ─────────────────────────────────────
    EnvVarDef("PROTON_NATIVE_AGS", "Proton", "enum", "",
              "[Proton-GE / Proton-EM] Use AMD's proprietary AMD GPU Services binary "
              "instead of Proton's built-in one, and allow statically linked AGS. Mainly "
              "useful to enable HDR in some UE4 D3D11 games on AMD GPUs.",
              options=["0", "1"]),
    EnvVarDef("PROTON_USE_WRITECOPY", "Proton", "enum", "",
              "[Proton-GE] Simulate Windows copy-on-write (writecopy) semantics for loaded "
              "images (sets WINE_SIMULATE_WRITECOPY=1). Needed by some DRM/anti-cheat.",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_WRITE_WATCH", "Proton", "enum", "",
              "[Proton-GE] Disable memory write-watch support in ntdll. Can improve "
              "performance in a few specific games; only use after verifying the game "
              "runs without it.",
              options=["0", "1"]),
    EnvVarDef("PROTON_OLD_GL_STRING", "Proton", "enum", "",
              "Limit the length of the GL extension string through driver overrides "
              "(sets MESA_EXTENSION_MAX_YEAR=2003 and __GL_ExtensionStringVersion=17700), "
              "for old OpenGL games that crash on very long strings.",
              options=["0", "1"]),
    EnvVarDef("PROTON_HIDE_INTEL_GPU", "Proton", "enum", "",
              "Hide Intel GPUs from the game (sets WINE_HIDE_INTEL_GPU=1), for titles that "
              "break on Intel-specific code paths. Relevant on hybrid laptops with an "
              "Intel iGPU.",
              options=["0", "1"]),
    EnvVarDef("PROTON_HIDE_VANGOGH_GPU", "Proton", "enum", "",
              "Hide the Steam Deck (Van Gogh) GPU from the game (sets "
              "WINE_HIDE_VANGOGH_GPU=1).",
              options=["0", "1"]),
    EnvVarDef("PROTON_NO_XIM", "Proton", "enum", "",
              "Disable the X Input Method (XIM) in Wine's X11 driver. Works around "
              "text-input/focus problems in a few games.",
              options=["0", "1"]),
    EnvVarDef("PROTON_SET_STEAM_DRIVE", "Proton", "enum", "",
              "Create a T: drive in the Wine prefix pointing at the Steam installation "
              "directory (counterpart of PROTON_SET_GAME_DRIVE's S: drive).",
              options=["0", "1"]),
    EnvVarDef("PROTON_DLL_COPY", "Proton", "string", "",
              "Comma-separated list of built-in DLL globs Proton copies into the prefix "
              "(default: the DirectX / VC++ redistributable set, e.g. d3dcompiler_*.dll, "
              "xaudio*.dll, msvcp1*.dll). Setting it REPLACES that default list.",
              placeholder="e.g. d3dcompiler_*.dll,xaudio*.dll"),
    # ── Wayland / input / audio / media ──────────────────────────────────────
    EnvVarDef("PROTON_USE_X11_EXCLUSIVE", "Proton", "string", "",
              "[Proton-GE] Under Wine-Wayland, run only this executable on winex11.drv "
              "while everything else stays on Wayland. Takes an exact basename "
              "(Launcher.exe) or a case-insensitive path fragment.",
              placeholder="e.g. Launcher.exe"),
    EnvVarDef("PROTON_WAYLAND_MONITOR", "Proton", "string", "",
              "[Proton-GE] Primary monitor for Wine-Wayland (sets WAYLANDDRV_PRIMARY_MONITOR). "
              "Auto-detected when unset.",
              placeholder="e.g. DP-1"),
    EnvVarDef("PROTON_NO_STEAMINPUT", "Proton", "enum", "",
              "[Proton-GE / Proton-CachyOS / Proton-EM] Disable Steam Input support for the "
              "game. Proton-GE also sets it by itself in some controller-fallback cases; "
              "disable Steam Input for the game in Steam too.",
              options=["0", "1"]),
    EnvVarDef("PROTON_EMULATE_STEAMINPUT", "Proton", "enum", "",
              "[Proton-GE / Proton-EM] Force Steam Input emulation on. Explicitly setting "
              "it also disables Proton-GE's automatic Sony-controller compatibility fallback.",
              options=["0", "1"]),
    EnvVarDef("PROTON_USE_PIPEWIRE", "Proton", "enum", "",
              "[Proton-CachyOS] Use the native winepipewire.drv audio driver (enabled by "
              "default). Set 0 to fall back to winepulse.drv.",
              options=["0", "1"]),
    EnvVarDef("PROTON_MEDIA_FORCE_GST", "Proton", "enum", "",
              "[Proton-CachyOS] Force the GStreamer media path. Fixes cutscene video/audio "
              "in some games.",
              options=["0", "1"]),
]

# ============================================================================
# Wine Environment Variables (WINE_* / WINEDEBUG, Wine + Proton runtime)
# ============================================================================

WINE_ENV_VARS: List[EnvVarDef] = [
    # ── CPU / Scheduling ─────────────────────────────────────────────────────
    EnvVarDef("WINE_CPU_TOPOLOGY", "Wine", "string", "",
              "Overrides the CPU topology Wine reports to the game: 'N:i,j,k,...' exposes N "
              "logical CPUs, mapped onto host CPU indices i,j,k,.... Works around games that "
              "crash or refuse to launch on very high thread-count systems (Far Cry 4, "
              "Warhammer 40k: Space Marine and others cap out around 26-31 threads).",
              placeholder="e.g. 12:0,1,2,3,4,5,6,7,8,9,10,11"),
    EnvVarDef("WINE_DISABLE_HARDWARE_SCHEDULING", "Wine", "enum", "",
              "Disable Wine's hardware-accelerated GPU scheduling emulation path. Community "
              "workaround for titles (e.g. RTX Remix ports) that misbehave when this path is "
              "active on Linux.",
              options=["0", "1"]),
    # ── Rendering Workarounds ────────────────────────────────────────────────
    EnvVarDef("WINE_DISABLE_VULKAN_OPWR", "Wine", "enum", "",
              "Disable Vulkan 'other process window rendering'. Works around issues on "
              "Wayland where the blit ends up one frame behind (compat string: noopwr).",
              options=["0", "1"]),
    EnvVarDef("WINE_FULLSCREEN_INTEGER_SCALING", "Wine", "enum", "",
              "Enable integer scaling in fullscreen mode, for sharp pixel-perfect upscaling "
              "instead of a blurry bilinear stretch.",
              options=["0", "1"]),
    EnvVarDef("WINE_DO_NOT_CREATE_DXGI_DEVICE_MANAGER", "Wine", "enum", "",
              "Workaround for video/audio playback issues in some games caused by incomplete "
              "IMFDXGIDeviceManager support (compat string: nomfdxgiman).",
              options=["0", "1"]),
    EnvVarDef("WINE_USE_KWIN_HACKS", "Wine", "enum", "",
              "Enable KDE-specific windowing workarounds that can help on KDE Plasma older "
              "than 6.4 on Wayland or older than 6.6 on X11.",
              options=["0", "1"]),
    # ── Wayland Input (Proton-EM only) ───────────────────────────────────────
    EnvVarDef("WAYLANDDRV_RAWINPUT", "Wine", "string", "",
              "[Proton-EM only, 10.0-1e+] Tunes mouse input under winewayland.drv. Set to 0 "
              "to fall back to accelerated (OS pointer-accel) input if raw input feels overly "
              "sensitive. On Proton-EM 10.0-2D+ you can instead pass any positive real number "
              "(e.g. 0.5) as a sensitivity multiplier for raw input — directly useful for "
              "dialing in mouse feel in FPS/aim-heavy titles run through the Wayland driver.",
              placeholder="0, or a multiplier like 0.5"),
    EnvVarDef("WAYLANDDRV_PRIMARY_MONITOR", "Wine", "string", "",
              "[Proton-EM only, 10.0-1b+] Explicitly names which monitor winewayland.drv "
              "should treat as primary (e.g. for fullscreen placement), since Wayland has no "
              "concept of a global primary monitor the way X11 does. Value is a compositor "
              "output name like 'eDP-1'. Shouldn't be needed on Proton-EM 10.0-25+.",
              placeholder="e.g. eDP-1"),
    EnvVarDef("WINE_WAYLAND_HACKS", "Wine", "enum", "",
              "[Proton-EM / Proton-CachyOS] Master toggle for winewayland.drv-specific "
              "workarounds. Set to 0 to disable them if they're the cause of an issue — one "
              "known case is critical-section timeouts in some launchers (e.g. EA App via "
              "the link2ea protocol) that clear up with this off.",
              options=["0", "1"]),
    # ── Audio ────────────────────────────────────────────────────────────────
    EnvVarDef("WINEPULSE_FAST_POLLING", "Wine", "enum", "",
              "Retired variable (no longer read by current Proton-CachyOS). Used to help "
              "eliminate crackling with the default PulseAudio driver, at the cost of rare "
              "crackling in some titles (e.g. God of War Ragnarok) with power-of-two quantum "
              "sizes like 512/768/1024. Kept here for older Proton/Wine builds.",
              options=["0", "1"]),
    EnvVarDef("WINEALSA_CHANNELS", "Wine", "enum", "",
              "Select the channel count for the winealsa driver, or force-disable spatial "
              "audio. 2 disables spatial audio; 4/6/8 select 2-front+2-rear/5.1/7.1. Useful "
              "when dialogue or effects come out of the wrong speaker.",
              options=["2", "4", "6", "8"]),
    EnvVarDef("WINEALSA_SPATIAL", "Wine", "enum", "",
              "Properly downmix spatial audio (including height channels) with winealsa. "
              "Only recommended if WINEALSA_CHANNELS alone still sounds wrong.",
              options=["0", "1"]),
    # ── Network ──────────────────────────────────────────────────────────────
    EnvVarDef("WINE_BLOCK_HOSTS", "Wine", "string", "",
              "Comma- or semicolon-separated list of hosts Wine should refuse to connect to "
              "(max 16 hosts, 256 chars each). Useful for blocking telemetry/DRM phone-home "
              "hosts that cause launch hangs.",
              placeholder="host1.org,host2.net"),
    EnvVarDef("WINE_FULLSCREEN_FSR", "Wine", "enum", "",
              "[Proton-GE] Enable AMD FSR upscaling inside Wine's fullscreen hack, so a "
              "game running at a lower fake resolution is upscaled to the monitor. Works "
              "in Vulkan games only (DXVK and VKD3D-Proton included) and needs the "
              "fullscreen hack, i.e. not under Wine-Wayland. Vendor-neutral, so it also "
              "runs on NVIDIA GPUs.",
              options=["0", "1"]),
    EnvVarDef("WINE_FULLSCREEN_FSR_STRENGTH", "Wine", "enum", "",
              "[Proton-GE] FSR sharpening for WINE_FULLSCREEN_FSR, 0-5. 0 is the sharpest, "
              "higher values sharpen less; 2 is AMD's recommended default and GE's default.",
              options=["0", "1", "2", "3", "4", "5"]),
    EnvVarDef("WINE_FULLSCREEN_FSR_CUSTOM_MODE", "Wine", "string", "",
              "[Proton-GE] Fake screen resolution (WIDTHxHEIGHT) shown to the game while "
              "FSR upscales to the real mode. Useful for games that always render at "
              "native resolution.",
              placeholder="e.g. 1280x720"),
    EnvVarDef("WINE_ENABLE_TIMEOUT_FIX", "Wine", "enum", "",
              "[Proton-DW (dwproton) only] Works around connection-timeout failures some "
              "setups hit launching live-service titles (originally documented for Genshin "
              "Impact / Zenless Zone Zero) that otherwise hang or fail to reach their servers.",
              options=["0", "1"]),
    # ── Debug ────────────────────────────────────────────────────────────────
    EnvVarDef("WINEDEBUG", "Wine", "flags", "",
              "Wine's own debug channel logging control. '-all' silences everything (fastest, "
              "recommended for normal play); '+all' is maximally verbose. Combine toggles with "
              "commas, e.g. '-all,+loaded' to see only module loads.",
              options=["-all", "err+all", "+all", "+relay", "+seh", "+heap", "+loaded",
                       "+module", "+process", "+timestamp", "+pid", "+tid"]),
    # ── Sync Primitives (plain Wine/Lutris — Proton uses PROTON_NO_ESYNC/ ───
    # ── PROTON_NO_FSYNC/PROTON_USE_NTSYNC instead, see Proton category) ──────
    EnvVarDef("WINEESYNC", "Wine", "enum", "1",
              "Enables the eventfd-based esync fast synchronization primitives in vanilla "
              "Wine/Lutris runners (not the Steam Proton wrapper, which is toggled via "
              "PROTON_NO_ESYNC instead). Reduces sync overhead vs the default server-side "
              "wineserver sync. Superseded by fsync/ntsync on kernels that support them, but "
              "still the common baseline on custom Wine builds without those patches.",
              options=["0", "1"]),
    EnvVarDef("WINEFSYNC", "Wine", "enum", "1",
              "Enables futex-based fsync synchronization in vanilla Wine/Lutris runners "
              "(requires a Wine build with the fsync patches and a reasonably recent kernel). "
              "Lower overhead than esync; takes priority over WINEESYNC when both a build "
              "supports fsync and this is set. Proton's equivalent toggle is PROTON_NO_FSYNC.",
              options=["0", "1"]),
    # ── DLL Overrides ─────────────────────────────────────────────────────────
    EnvVarDef("WINEDLLOVERRIDES", "Wine", "string", "",
              "Per-DLL override list controlling whether Wine uses its own built-in "
              "implementation or a native Windows DLL placed in the prefix, e.g. for staging "
              "in a DXVK/VKD3D DLL manually, or forcing a component to 'disabled' to work "
              "around a broken native/builtin implementation. Comma-separated "
              "'dllname=mode' pairs; mode is n (native), b (builtin), n,b or b,n (try both, "
              "first listed wins), or empty (disabled).",
              placeholder="e.g. d3d11,dxgi=n;winemenubuilder.exe=d"),
]

# ============================================================================
# DXVK-NVAPI Environment Variables (jp7677/dxvk-nvapi runtime tuning)
# ============================================================================

DXVK_NVAPI_ENV_VARS: List[EnvVarDef] = [
    # ── D3D12 / Shader Extensions ────────────────────────────────────────────
    EnvVarDef("DXVK_NVAPI_D3D12_NV_SHADER_EXTN", "DXVK-NVAPI", "enum", "",
              "Enables experimental support for NVIDIA shader extensions in D3D12 titles "
              "(via VKD3D-Proton).",
              options=["0", "1"]),
    # ── NGX / DLSS Debug ─────────────────────────────────────────────────────
    EnvVarDef("DXVK_NVAPI_SET_NGX_DEBUG_OPTIONS", "DXVK-NVAPI", "string", "",
              "Comma-separated key=value pairs forwarded to NVIDIA NGX's own debug-option "
              "system — the same mechanism as the Windows registry indicator keys. Commonly "
              "used to show an on-screen DLSS/DLSS-G indicator to confirm which preset/build "
              "is actually active.",
              placeholder="DLSSIndicator=1024,DLSSGIndicator=2"),
    EnvVarDef("__NV_SIGNED_LOAD_CHECK", "DXVK-NVAPI", "enum", "",
              "Disables the NGX Core signature check that normally blocks loading non-"
              "NVIDIA-signed NGX features. Only disable this when you're confident every NGX "
              "feature DLL on the system is authentic — it exists to stop malicious NGX "
              "feature loading.",
              options=["none"]),
    # ── Vulkan Reflex Layer ──────────────────────────────────────────────────
    EnvVarDef("DXVK_NVAPI_VKREFLEX", "DXVK-NVAPI", "enum", "",
              "Enables DXVK-NVAPI's own Vulkan Reflex compatibility layer, for Reflex support "
              "in native-Vulkan titles (Portal RTX, Path of Exile 1/2, DOOM: The Dark Ages). "
              "Disabled by default even when installed, since it can interfere with other "
              "Vulkan apps that don't use Reflex. Formerly named PROTON_VKREFLEX.",
              options=["0", "1"]),
    EnvVarDef("DISABLE_DXVK_NVAPI_VKREFLEX", "DXVK-NVAPI", "enum", "",
              "Any non-empty value forcibly disables the Vulkan Reflex layer, overriding "
              "DXVK_NVAPI_VKREFLEX=1.",
              options=["1"]),
    EnvVarDef("DXVK_NVAPI_VKREFLEX_LAYER_LOG_LEVEL", "DXVK-NVAPI", "enum", "",
              "Log verbosity for the Vulkan Reflex compatibility layer specifically (separate "
              "from DXVK_NVAPI_LOG_LEVEL below).",
              options=["none", "error", "warn", "info", "debug", "trace"]),
    # ── General Logging / Driver Reporting ───────────────────────────────────
    EnvVarDef("DXVK_NVAPI_LOG_LEVEL", "DXVK-NVAPI", "enum", "",
              "Log verbosity for DXVK-NVAPI itself; logging is off by default. "
              "'info' logs the normal messages (incl. which DRS settings the game "
              "requested). 'trace' (newer releases) logs every entry point enter/exit — "
              "heavy on performance and log size.",
              options=["none", "info", "trace"]),
    EnvVarDef("DXVK_NVAPI_LOG_PATH", "DXVK-NVAPI", "string", "",
              "Also write DXVK-NVAPI's log to nvapi.log / nvapi64.log / nvofapi64.log (per DLL) "
              "in this directory, in addition to console output. Needs LOG_LEVEL info or "
              "trace. Entries are appended to an existing file.",
              placeholder="/path/to/log/dir"),
    EnvVarDef("DXVK_NVAPI_DRIVER_VERSION", "DXVK-NVAPI", "int", "",
              "Override the driver version DXVK-NVAPI reports to the game. Value is the "
              "version number with no dots, e.g. 47141 reports as driver 471.41.",
              placeholder="e.g. 47141"),
    EnvVarDef("DXVK_NVAPI_ALLOW_OTHER_DRIVERS", "DXVK-NVAPI", "enum", "",
              "Allow using DXVK-NVAPI without an NVIDIA GPU on the proprietary driver. Useful "
              "for exercising NVAPI D3D11 extensions on a non-NVIDIA GPU (e.g. Mesa/NVK).",
              options=["0", "1"]),
    EnvVarDef("DXVK_NVAPI_DISABLE_ENTRYPOINTS", "DXVK-NVAPI", "string", "",
              "Comma-separated NVAPI entry point names to hide from the game — the "
              "game then sees those functions as unavailable. Workaround tool for "
              "titles that crash in one specific NVAPI path.",
              placeholder="e.g. NvAPI_D3D11_BeginUAVOverlap,NvAPI_D3D11_EndUAVOverlap"),
    EnvVarDef("DXVK_NVAPI_FAKE_VKREFLEX", "DXVK-NVAPI", "enum", "",
              "1 = let Vulkan Reflex initialise successfully even when the dxvk-nvapi "
              "Vulkan Reflex layer is NOT installed (fake success, no latency "
              "reduction). 0 = never fake. Enabled automatically for DOOM: The Dark "
              "Ages to avoid its pink-tint bug. For real Reflex install the layer and "
              "use DXVK_NVAPI_VKREFLEX=1.",
              options=["0", "1"]),

    EnvVarDef("DXVK_NVAPI_VKREFLEX_INJECT_SUBMIT_FRAME_IDS", "DXVK-NVAPI", "enum", "",
              "VK Reflex layer: inject frame IDs (from NvAPI_Vulkan_SetLatencyMarker) into "
              "vkQueueSubmit* calls, helping the driver correlate submits with frames. Enables "
              "VK_KHR_present_id. May interfere with the game's own present IDs. "
              "Requires DXVK_NVAPI_VKREFLEX=1.",
              options=["0", "1"]),
    EnvVarDef("DXVK_NVAPI_VKREFLEX_INJECT_PRESENT_FRAME_IDS", "DXVK-NVAPI", "enum", "",
              "VK Reflex layer: inject frame IDs into vkQueuePresentKHR. Same caveats as "
              "..._INJECT_SUBMIT_FRAME_IDS. Requires DXVK_NVAPI_VKREFLEX=1.",
              options=["0", "1"]),
    EnvVarDef("DXVK_NVAPI_VKREFLEX_ALLOW_FALLBACK_TO_OOB_FRAME_ID", "DXVK-NVAPI", "enum", "",
              "VK Reflex layer: when the game's latency markers carry no usable frame ID, "
              "the layer falls back to an out-of-band frame ID in submit and present calls "
              "(default on). 0 = disable that fallback. Only meaningful together with the "
              "INJECT_*_FRAME_IDS options. Requires DXVK_NVAPI_VKREFLEX=1.",
              options=["0", "1"]),
    EnvVarDef("DXVK_NVAPI_VKREFLEX_ALLOW_FALLBACK_TO_PRESENT_FRAME_ID", "DXVK-NVAPI", "enum", "",
              "VK Reflex layer: allow falling back to the present frame ID in vkQueueSubmit* "
              "calls (default on). 0 = disable that fallback. Only meaningful together with "
              "the INJECT_*_FRAME_IDS options. Requires DXVK_NVAPI_VKREFLEX=1.",
              options=["0", "1"]),
    EnvVarDef("DXVK_NVAPI_VKREFLEX_ALLOW_FALLBACK_TO_SIMULATION_FRAME_ID", "DXVK-NVAPI", "enum", "",
              "VK Reflex layer: allow falling back to the simulation frame ID in "
              "vkQueueSubmit* calls (default on). 0 = disable that fallback. Only meaningful "
              "together with the INJECT_*_FRAME_IDS options. Requires DXVK_NVAPI_VKREFLEX=1.",
              options=["0", "1"]),
]

# ============================================================================
# NVIDIA Smooth Motion Environment Variables (VK_LAYER_NV_present / NVPresent)
# ============================================================================

NVPRESENT_ENV_VARS: List[EnvVarDef] = [
    EnvVarDef("NVPRESENT_ENABLE_SMOOTH_MOTION", "NVIDIA Smooth Motion", "enum", "",
              "Enables VK_LAYER_NV_present, NVIDIA's driver-based frame-interpolation layer "
              "for Vulkan titles that don't have DLSS Frame Generation. RTX 50-series only.",
              options=["0", "1"]),
    EnvVarDef("NVPRESENT_QUEUE_FAMILY", "NVIDIA Smooth Motion", "enum", "",
              "The layer presents from an asynchronous compute queue by default, which can "
              "conflict with some third-party overlays. Set to 1 to present from the graphics "
              "queue instead, at a small performance cost.",
              options=["0", "1"]),
    EnvVarDef("NVPRESENT_LOG_LEVEL", "NVIDIA Smooth Motion", "enum", "",
              "Debug log verbosity for the NVPresent/Smooth Motion layer. Logs go to stderr "
              "by default; redirect with NVPRESENT_LOG_FILE. If nothing gets logged, check "
              "with VK_LOADER_DEBUG=layer whether the layer is loading at all.",
              options=["0", "1", "2", "3", "4"]),
    EnvVarDef("NVPRESENT_LOG_FILE", "NVIDIA Smooth Motion", "string", "",
              "Redirect NVPresent/Smooth Motion debug logging to this file instead of stderr.",
              placeholder="/tmp/nvpresent.log"),
]

# ============================================================================
# System / Loader Environment Variables (Vulkan loader, dynamic linker, SDL)
# ============================================================================

SYS_ENV_VARS: List[EnvVarDef] = [
    # ── Vulkan Loader ────────────────────────────────────────────────────────
    EnvVarDef("VK_DRIVER_FILES", "System / Loader", "string", "",
              "Colon-separated list of Vulkan ICD manifest JSON files for the loader to use, "
              "overriding normal driver discovery. Modern replacement for the older "
              "VK_ICD_FILENAMES name (both are still accepted).",
              placeholder="/usr/share/vulkan/icd.d/nvidia_icd.json"),
    EnvVarDef("VK_LOADER_DEBUG", "System / Loader", "flags", "",
              "Vulkan loader debug output channels. 'layer' shows which implicit/explicit "
              "layers are being discovered and loaded — the first thing to check when a "
              "Vulkan layer (NVPresent, MangoHud, gamescope's WSI layer, ...) doesn't seem "
              "to be active.",
              options=["error", "warn", "info", "debug", "layer", "all"]),
    # ── Dynamic Linker ───────────────────────────────────────────────────────
    EnvVarDef("LD_PRELOAD", "System / Loader", "string", "",
              "Colon-separated list of shared libraries to load before all others for every "
              "process. Commonly used to preload an alternative allocator such as jemalloc "
              "or tcmalloc to reduce heap fragmentation/allocation overhead in some titles, "
              "or to inject overlay/hook libraries.",
              placeholder="/usr/lib64/libjemalloc.so:/usr/lib32/libjemalloc.so"),
    EnvVarDef("LD_BIND_NOW", "System / Loader", "enum", "",
              "Force immediate (non-lazy) symbol binding at process start instead of lazy "
              "PLT resolution on first call. Slightly longer startup in exchange for removing "
              "small first-call resolution stutters later — mostly a micro-optimization.",
              options=["0", "1"]),
    # ── Display Backend ──────────────────────────────────────────────────────
    EnvVarDef("__GLX_VENDOR_LIBRARY_NAME", "System / Loader", "enum", "",
              "Forces which GLX vendor implementation libglvnd's vendor-neutral dispatch "
              "loads. Set to 'nvidia' to force the NVIDIA GLX path on hybrid/PRIME laptops "
              "where Mesa might otherwise be picked for the display GPU.",
              options=["nvidia", "mesa"]),
    EnvVarDef("SDL_VIDEODRIVER", "System / Loader", "enum", "",
              "SDL2 hint forcing which video backend SDL uses. 'wayland' forces native "
              "Wayland instead of SDL's default XWayland fallback; useful together with "
              "PROTON_ENABLE_WAYLAND.",
              options=["x11", "wayland", "kmsdrm", "offscreen"]),
]

# ============================================================================
# Gamescope — Steam-side Environment Variables
# These are genuinely read from the environment (by the Steam client's own
# gamescope-session integration, or by gamescope's Steam-overlay path) —
# confirmed against gamescope's GitHub issues/source and Steam community
# session scripts. They are NOT read by the gamescope binary's own argv
# parser. Everything that IS a gamescope command-line option (resolution,
# HDR, upscaling, VRR, debug flags, etc.) lives in GAMESCOPE_FLAGS below and
# is rendered as a CLI flag builder, not as env vars.
# ============================================================================

GAMESCOPE_ENV_VARS: List[EnvVarDef] = [

    EnvVarDef("STEAMID", "Gamescope", "string", "",
              "The Steam App ID of the game currently running inside gamescope.  "
              "Set automatically by the Steam client when launching through Steam.  "
              "gamescope reads this to look up the game's artwork for the overlay, "
              "apply per-game DRS presets on Steam Deck, and for FPS limit reporting "
              "in the Steam HUD.  You can set it manually in a Lutris command to give "
              "gamescope the right game context without going through Steam.  "
              "Example: STEAMID=730  (Counter-Strike 2)",
              placeholder="e.g. 730"),

    EnvVarDef("STEAM_GAMESCOPE_HW_UPSCALE_FILTER", "Gamescope", "enum", "",
              "Steam-specific override to select the upscale filter for gamescope "
              "via the Steam Settings UI path.  Maps to the same -F / --filter flag "
              "as the CLI 'Upscale Filter' option below but is written by the Steam "
              "client itself based on the per-game 'Scaling Filter' setting.  Setting "
              "it manually overrides whatever the Steam UI chose.  "
              "Example: STEAM_GAMESCOPE_HW_UPSCALE_FILTER=fsr",
              options=["fsr", "nis", "pixel", "linear", "nearest"]),

    EnvVarDef("STEAM_GAMESCOPE_HW_UPSCALE_SHARPNESS", "Gamescope", "int", "",
              "Steam-specific sharpness override corresponding to the sharpness slider "
              "in Steam Settings -> Resolution.  Written by the client; override manually "
              "to bypass the UI.  Same 0-20 (FSR) / 0-5 (NIS) scale as the CLI --sharpness "
              "flag.  "
              "Example: STEAM_GAMESCOPE_HW_UPSCALE_SHARPNESS=5",
              placeholder="0-20 (FSR) or 0-5 (NIS)"),

    EnvVarDef("STEAM_GAMESCOPE_VRR_ENABLED", "Gamescope", "enum", "",
              "Steam client's per-game VRR toggle, written into gamescope's environment "
              "when you enable 'Allow Tearing' or 'VRR' in the Steam per-game settings.  "
              "Same effect as the CLI --adaptive-sync flag; the STEAM_ prefix just "
              "indicates it was set by the client rather than the user.  Override to "
              "force VRR on/off regardless of what the Steam UI shows.  "
              "Example: STEAM_GAMESCOPE_VRR_ENABLED=1",
              options=["0", "1"]),

    EnvVarDef("STEAM_GAMESCOPE_TEARING_SUPPORT", "Gamescope", "enum", "",
              "Steam client flag advertising that the gamescope session supports tearing "
              "presents.  When set, the Steam HUD and the per-game settings panel show "
              "the 'Allow Tearing' toggle.  Set automatically in SteamOS; on desktop "
              "you may need to set it manually if you want Steam's tearing toggle to "
              "have any effect.  "
              "Example: STEAM_GAMESCOPE_TEARING_SUPPORT=1",
              options=["0", "1"]),

    EnvVarDef("STEAM_GAMESCOPE_DYNAMIC_REFRESH_SUPPORT", "Gamescope", "enum", "",
              "Advertises VRR / dynamic refresh rate support to the Steam client so the "
              "per-game refresh-rate selector is shown.  Required on desktop gamescope "
              "if you want Steam's frame-rate-limit UI to work correctly.  "
              "Example: STEAM_GAMESCOPE_DYNAMIC_REFRESH_SUPPORT=1",
              options=["0", "1"]),

    EnvVarDef("STEAM_GAMESCOPE_LIMITER_RES", "Gamescope", "enum", "",
              "Enables gamescope's own Steam-integrated FPS limiter UI.  "
              "When set to 1, the Steam overlay's frame-rate slider adjusts gamescope's "
              "-r cap in real time without restarting the game.  Set to 0 to manage the "
              "cap entirely via the CLI --framerate-limit flag below.  "
              "Example: STEAM_GAMESCOPE_LIMITER_RES=1",
              options=["0", "1"]),

    EnvVarDef("STEAM_GAMESCOPE_XWAYLAND_DISPLAY", "Gamescope", "string", "",
              "The :DISPLAY number of the XWayland server gamescope started internally.  "
              "Set automatically by gamescope; forwarded to the game process so it can "
              "connect to gamescope's private XWayland rather than the host X server.  "
              "You normally never set this manually - it appears here so you can inspect "
              "or pin it when debugging multi-compositor scenarios.  "
              "Example: STEAM_GAMESCOPE_XWAYLAND_DISPLAY=:1",
              placeholder="e.g. :1"),

    EnvVarDef("DISABLE_GAMESCOPE_WSI", "Gamescope", "enum", "",
              "Disable switch of gamescope's Vulkan WSI layer (VkLayer_FROG_gamescope_wsi): "
              "the layer manifest's disable_environment, so DISABLE_GAMESCOPE_WSI=1 stops "
              "the layer from loading at all. Use it when a game misbehaves with the layer "
              "(black video, bad swapchain) and ENABLE_GAMESCOPE_WSI=0 is not enough.",
              options=["0", "1"]),
    EnvVarDef("GAMESCOPE_MANGOAPP_SOCKET_DISABLE", "Gamescope", "enum", "",
              "Read by gamescope itself (set it on the gamescope process): stops gamescope "
              "from sending its per-frame data to mangoapp over the mangoapp socket. "
              "Debug/compat switch, e.g. when mangoapp is not draining the socket.",
              options=["0", "1"]),
    EnvVarDef("ENABLE_GAMESCOPE_WSI", "Gamescope", "enum", "",
              "Toggles gamescope's Vulkan WSI (VK_layer) integration for the wrapped game. "
              "Proton sets this to 1 automatically when it detects it's running under "
              "gamescope, which lets the game present directly through gamescope's "
              "low-latency compositing path. Native (non-Proton) titles launched directly "
              "under gamescope do NOT get this set automatically, so setting it manually "
              "can improve latency for native Linux games. Conversely, some games show "
              "black/corrupted video playback or cutscenes with the WSI layer active - set "
              "to 0 as a workaround in that case. Community-sourced (gamescope issue "
              "#1958 / #1537), not in gamescope --help. "
              "Example: ENABLE_GAMESCOPE_WSI=0 gamescope -- %command%",
              options=["0", "1"]),

    EnvVarDef("STEAM_MULTIPLE_XWAYLANDS", "Gamescope", "enum", "",
              "Requests a second, dedicated XWayland server from gamescope so overlay "
              "windows (Steam overlay, some anti-cheat/launcher UIs) run in their own "
              "XWayland instance separate from the game's - pair with the CLI "
              "--xwayland-count 2 flag below. Reduces overlay-related stutter/input "
              "issues in embedded gamescope sessions. Community-sourced session-script "
              "convention, not in gamescope --help. "
              "Example: STEAM_MULTIPLE_XWAYLANDS=1 gamescope --xwayland-count 2 -e -- steam -gamepadui",
              options=["0", "1"]),

    EnvVarDef("STEAM_GAMESCOPE_VRR_SUPPORTED", "Gamescope", "enum", "",
              "Advertises to the Steam client that the current gamescope session is capable "
              "of VRR at all (distinct from STEAM_GAMESCOPE_VRR_ENABLED, which is the "
              "per-game on/off toggle). Needed on some desktop setups for Steam's VRR "
              "toggle to appear/function in per-game display settings. Community-sourced, "
              "not in gamescope --help. "
              "Example: STEAM_GAMESCOPE_VRR_SUPPORTED=1",
              options=["0", "1"]),

    # ── Added from gamescope getenv() calls (2026-09) ──────────────────────
    EnvVarDef("GAMESCOPE_WSI_MIN_IMAGE_COUNT", "Gamescope", "int", "3",
              "Minimum swapchain image count the gamescope WSI layer reports to the game "
              "(default 3). Lower = less queued latency but more risk of stalls; "
              "higher = smoother under load. Also honours vk_wsi_override_min_image_count. "
              "Takes effect only with the WSI layer active (ENABLE_GAMESCOPE_WSI).",
              placeholder="e.g. 2"),
    EnvVarDef("GAMESCOPE_WSI_ENSURE_MIN_IMAGE_COUNT", "Gamescope", "enum", "",
              "Force the game's swapchain to use at least GAMESCOPE_WSI_MIN_IMAGE_COUNT "
              "images even if it asks for fewer.",
              options=["0", "1"]),
    EnvVarDef("GAMESCOPE_WSI_FORCE_BYPASS", "Gamescope", "enum", "",
              "Ask gamescope to bypass composition (direct scanout) for this client whenever "
              "possible. Lowest latency path; may break overlays.",
              options=["0", "1"]),
    EnvVarDef("GAMESCOPE_WSI_FRAME_LIMITER_AWARE", "Gamescope", "enum", "",
              "Tell gamescope the game's swapchain handles the gamescope frame limiter itself. "
              "Auto-enabled for DXVK >= 2.3 and vkd3d >= 2.12; set 1 for native Vulkan games "
              "that pace correctly, 0 to force gamescope-side limiting.",
              options=["0", "1"]),
    EnvVarDef("GAMESCOPE_DISABLE_ASYNC_FLIPS", "Gamescope", "enum", "",
              "DRM backend: never use async (tearing) page flips. Workaround for "
              "flicker/corruption with --immediate-flips on some drivers.",
              options=["0", "1"]),
    EnvVarDef("GAMESCOPE_FORCE_GENERAL_QUEUE", "Gamescope", "enum", "",
              "Run gamescope's compositing on the general graphics queue instead of an async "
              "compute queue. Workaround for driver issues; may add latency.",
              options=["0", "1"]),
    EnvVarDef("GAMESCOPE_DISABLE_TIMERFD", "Gamescope", "enum", "",
              "Use the fallback sleep-based vblank timer instead of timerfd. Debug only.",
              options=["0", "1"]),
    EnvVarDef("GAMESCOPE_LIFTOFF_CACHE_DISABLE", "Gamescope", "enum", "",
              "DRM backend: disable libliftoff's plane-allocation cache. Debug for "
              "direct-scanout / overlay-plane glitches.",
              options=["0", "1"]),
]

# ============================================================================
# Gamescope — Command-Line Flags (real, from `gamescope --help`)
# gamescope has NO config-file or env-var input for these; they are argv-only.
# Rendered by GamescopeFlagsWidget as a CLI-flag builder gated behind a master
# "Enable Gamescope" toggle, producing:  gamescope <flags...> --
# (ends in a bare "--" — not Steam-specific, no %command% assumed)
# ============================================================================

GAMESCOPE_FLAGS: List[GamescopeFlagDef] = [

    # ── Output / window geometry ────────────────────────────────────────────
    GamescopeFlagDef("Output Width", "--output-width", "Geometry", "int",
                      "Output width in pixels presented to the display/host compositor.",
                      short="-W", placeholder="e.g. 2560"),
    GamescopeFlagDef("Output Height", "--output-height", "Geometry", "int",
                      "Output height in pixels presented to the display/host compositor.",
                      short="-H", placeholder="e.g. 1440"),
    GamescopeFlagDef("Game Width", "--nested-width", "Geometry", "int",
                      "Render width presented to the game itself; gamescope up/downscales "
                      "from this to the output resolution.",
                      short="-w", placeholder="e.g. 1920"),
    GamescopeFlagDef("Game Height", "--nested-height", "Geometry", "int",
                      "Render height presented to the game itself.",
                      short="-h", placeholder="e.g. 1080"),
    GamescopeFlagDef("Game Refresh Rate", "--nested-refresh", "Geometry", "int",
                      "Game refresh rate in frames per second.",
                      short="-r", placeholder="e.g. 165"),
    GamescopeFlagDef("Unfocused Refresh Rate", "--nested-unfocused-refresh", "Geometry", "int",
                      "Game refresh rate to use only while the gamescope window is "
                      "unfocused (nested mode). Lets you drop clocks/power when alt-tabbed.",
                      short="-o", placeholder="e.g. 30"),
    GamescopeFlagDef("Max Scale", "--max-scale", "Geometry", "int",
                      "Maximum scale factor gamescope will apply.",
                      short="-m", placeholder="e.g. 2"),
    GamescopeFlagDef("Force Windows Fullscreen", "--force-windows-fullscreen", "Geometry", "toggle",
                      "Force windows inside gamescope to always be the size of the nested "
                      "display (fullscreen)."),
    GamescopeFlagDef("Cursor Scale Height", "--cursor-scale-height", "Geometry", "int",
                      "Base output height to linearly scale the cursor image against.",
                      placeholder="e.g. 1080"),
    GamescopeFlagDef("Force Orientation", "--force-orientation", "Geometry", "enum",
                      "Rotate the internal display.",
                      options=["left", "right", "normal", "upsidedown"]),

    # ── Upscaling ────────────────────────────────────────────────────────────
    GamescopeFlagDef("Scaler", "--scaler", "Upscaling", "enum",
                      "Upscaler type. 'integer' restricts scaling to integer multiples only "
                      "(best for pixel-art with the pixel/nearest filter).",
                      short="-S", options=["auto", "integer", "fit", "fill", "stretch"]),
    GamescopeFlagDef("Upscale Filter", "--filter", "Upscaling", "enum",
                      "Upscaler filter. fsr = AMD FidelityFX Super Resolution 1.0. "
                      "nis = NVIDIA Image Scaling v1.0.3.",
                      short="-F", options=["linear", "nearest", "fsr", "nis", "pixel", "sgsr"]),
    GamescopeFlagDef("Sharpness", "--sharpness", "Upscaling", "int",
                      "Upscaler sharpness, 0 (max) to 20 (min). Alias: --fsr-sharpness.",
                      placeholder="0-20"),

    # ── HDR ──────────────────────────────────────────────────────────────────
    GamescopeFlagDef("HDR Enabled", "--hdr-enabled", "HDR", "toggle",
                      "Enable HDR output. Needs the Gamescope WSI layer enabled on the "
                      "client side for HDR clients to be supported."),
    GamescopeFlagDef("SDR Gamut Wideness", "--sdr-gamut-wideness", "HDR", "string",
                      "Wideness of the gamut for SDR content, 0-1.", placeholder="0.0-1.0"),
    GamescopeFlagDef("SDR Content Nits", "--hdr-sdr-content-nits", "HDR", "int",
                      "Luminance of SDR content in nits. Default: 400.", placeholder="e.g. 400"),
    GamescopeFlagDef("Inverse Tonemap SDR->HDR", "--hdr-itm-enabled", "HDR", "toggle",
                      "Enable SDR->HDR inverse tone mapping. Only affects SDR input."),
    GamescopeFlagDef("ITM Input Nits", "--hdr-itm-sdr-nits", "HDR", "int",
                      "Luminance of SDR content used as input for inverse tone mapping. "
                      "Default 100, max 1000.", placeholder="e.g. 100"),
    GamescopeFlagDef("ITM Target Nits", "--hdr-itm-target-nits", "HDR", "int",
                      "Target luminance of the inverse tone mapping process. "
                      "Default 1000, max 10000.", placeholder="e.g. 1000"),

    # ── Frame limiting / VRR / tearing ──────────────────────────────────────
    GamescopeFlagDef("Frame-rate Limit", "--framerate-limit", "Frame Pacing", "int",
                      "Simple frame-rate limit, used as a divisor of the refresh rate "
                      "(rounds down): 60/59->60fps, 60/25->30fps. 0 = disabled.",
                      placeholder="e.g. 60"),
    GamescopeFlagDef("Adaptive Sync (VRR)", "--adaptive-sync", "Frame Pacing", "toggle",
                      "Enable adaptive sync / VRR if the display and driver support it."),
    GamescopeFlagDef("Immediate Flips", "--immediate-flips", "Frame Pacing", "toggle",
                      "Allow immediate (tearing) flips instead of FIFO presentation - "
                      "trades a compositor frame of latency for possible tear lines."),
    GamescopeFlagDef("Realtime Scheduling", "--rt", "Frame Pacing", "toggle",
                      "Use realtime scheduling for gamescope's own threads."),

    # ── Input / window behaviour ─────────────────────────────────────────────
    GamescopeFlagDef("Mouse Sensitivity", "-s", "Input", "string",
                      "Multiply mouse movement by the given decimal factor.",
                      placeholder="e.g. 1.2"),
    GamescopeFlagDef("Force Grab Cursor", "--force-grab-cursor", "Input", "toggle",
                      "Always use relative mouse mode instead of flipping depending on "
                      "cursor visibility."),
    GamescopeFlagDef("Hide Cursor Delay", "--hide-cursor-delay", "Input", "int",
                      "Hide the cursor image after this many milliseconds of inactivity.",
                      short="-C", placeholder="e.g. 3000"),
    GamescopeFlagDef("Cursor Image", "--cursor", "Input", "string",
                      "Path to a default cursor image."),
    GamescopeFlagDef("Grab Keyboard (nested)", "--grab", "Input", "toggle",
                      "Grab the keyboard (nested mode only).", short="-g"),
    GamescopeFlagDef("Default Touch Mode", "--default-touch-mode", "Input", "enum",
                      "0: hover, 1: left, 2: right, 3: middle, 4: passthrough.",
                      options=["0", "1", "2", "3", "4"]),

    # ── Session / integration ────────────────────────────────────────────────
    GamescopeFlagDef("Steam Integration", "--steam", "Session", "toggle",
                      "Enable Steam integration.", short="-e"),
    GamescopeFlagDef("XWayland Count", "--xwayland-count", "Session", "int",
                      "Create N XWayland servers (2 lets overlay windows run isolated "
                      "from the game's own XWayland instance - see STEAM_MULTIPLE_XWAYLANDS "
                      "in the env-var list above).", placeholder="e.g. 2"),
    GamescopeFlagDef("Expose Wayland (xdg-shell)", "--expose-wayland", "Session", "toggle",
                      "Support Wayland clients using xdg-shell."),
    GamescopeFlagDef("Backend", "--backend", "Session", "enum",
                      "Rendering backend. auto autodetects (default).",
                      options=["auto", "drm", "sdl", "headless", "wayland"]),
    GamescopeFlagDef("Prefer VK Device", "--prefer-vk-device", "Session", "string",
                      "Prefer this Vulkan device for compositing.",
                      placeholder="e.g. 10de:2820 (RTX 4080 Mobile)"),
    GamescopeFlagDef("Prefer Output (embedded)", "--prefer-output", "Session", "string",
                      "List of connectors in order of preference (embedded mode only).",
                      short="-O", placeholder="e.g. DP-1,DP-2,HDMI-A-1"),
    GamescopeFlagDef("Display Index (nested)", "--display-index", "Session", "int",
                      "Force a specific display index in nested mode.", placeholder="e.g. 0"),
    GamescopeFlagDef("Generate DRM Mode", "--generate-drm-mode", "Session", "enum",
                      "DRM mode generation algorithm (embedded mode).",
                      options=["cvt", "fixed"]),
    GamescopeFlagDef("Virtual Connector Strategy", "--virtual-connector-strategy", "Session", "string",
                      "Specifies how virtual connectors are created."),
    GamescopeFlagDef("Ready FD", "--ready-fd", "Session", "int",
                      "Notify this FD when gamescope is ready.", short="-R", placeholder="e.g. 3"),
    GamescopeFlagDef("Stats Path", "--stats-path", "Session", "string",
                      "Write statistics to this path.", short="-T"),
    GamescopeFlagDef("Allow Deferred Backend", "--allow-deferred-backend", "Session", "toggle",
                      "Allow initting the backend in a deferred way if it doesn't work "
                      "immediately (minor correctness trade-off with modifiers etc)."),
    GamescopeFlagDef("Keep Alive", "--keep-alive", "Session", "toggle",
                      "Keep gamescope alive even after the primary process has died."),
    GamescopeFlagDef("Fullscreen (nested)", "--fullscreen", "Session", "toggle",
                      "Make the nested window fullscreen.", short="-f"),
    GamescopeFlagDef("Borderless (nested)", "--borderless", "Session", "toggle",
                      "Make the nested window borderless.", short="-b"),

    # ── Reshade ──────────────────────────────────────────────────────────────
    GamescopeFlagDef("ReShade Effect", "--reshade-effect", "ReShade", "string",
                      "Name of a ReShade shader to use, looked up in "
                      "/usr/share/gamescope/reshade/Shaders or "
                      "~/.local/share/gamescope/reshade/Shaders. Requires a gamescope "
                      "build with ReShade support compiled in.",
                      placeholder="e.g. CRT-Royale"),
    GamescopeFlagDef("ReShade Technique Index", "--reshade-technique-idx", "ReShade", "int",
                      "Technique index to use from the selected ReShade effect.",
                      placeholder="e.g. 0"),

    # ── Steam Deck ───────────────────────────────────────────────────────────
    GamescopeFlagDef("Mura Map", "--mura-map", "Steam Deck", "string",
                      "Path to the mura compensation map to use for the display "
                      "(Steam Deck panel calibration)."),

    # ── Debug / diagnostics ──────────────────────────────────────────────────
    GamescopeFlagDef("Disable Hardware Planes", "--disable-layers", "Debug", "toggle",
                      "Disable libliftoff (hardware plane) usage - forces full "
                      "compositing. Useful to rule out plane-assignment issues."),
    GamescopeFlagDef("Debug Layers", "--debug-layers", "Debug", "toggle",
                      "Debug libliftoff plane assignment."),
    GamescopeFlagDef("Debug Focus", "--debug-focus", "Debug", "toggle",
                      "Debug XWM focus handling."),
    GamescopeFlagDef("Synchronous X11", "--synchronous-x11", "Debug", "toggle",
                      "Force synchronous X11 connection (slower, deterministic ordering "
                      "for debugging)."),
    GamescopeFlagDef("Debug HUD", "--debug-hud", "Debug", "toggle",
                      "Paint a HUD with gamescope's own debug info."),
    GamescopeFlagDef("Debug Events", "--debug-events", "Debug", "toggle",
                      "Log X11 events."),
    GamescopeFlagDef("Force Composition", "--force-composition", "Debug", "toggle",
                      "Disable direct scan-out; always composite through gamescope's "
                      "Vulkan renderer. Useful to isolate whether an artifact comes from "
                      "the direct-scanout path or the compositor itself."),
    GamescopeFlagDef("Composite Debug", "--composite-debug", "Debug", "toggle",
                      "Draw frame markers on alternating screen corners while compositing."),
    GamescopeFlagDef("Disable Color Management", "--disable-color-management", "Debug", "toggle",
                      "Disable gamescope's color management pipeline."),
    GamescopeFlagDef("Disable XRes", "--disable-xres", "Debug", "toggle",
                      "Disable XRes-based PID lookup."),
    GamescopeFlagDef("Force HDR Support", "--hdr-debug-force-support", "Debug", "toggle",
                      "Force HDR support even if the display doesn't advertise it. HDR "
                      "clients are still tone-mapped to SDR output in that case."),
    GamescopeFlagDef("Force HDR Output", "--hdr-debug-force-output", "Debug", "toggle",
                      "Force HDR10 PQ output even if the display doesn't support it - "
                      "looks very wrong if the display genuinely can't do it."),
    GamescopeFlagDef("HDR Debug Heatmap", "--hdr-debug-heatmap", "Debug", "toggle",
                      "Display a heatmap-style debug view of HDR luminance across the scene."),

    # ── Added from gamescope main.cpp (2026-09) ─────────────────────────────
    GamescopeFlagDef("MangoApp Overlay", "--mangoapp", "Session", "toggle",
                      "Launch with the mangoapp (MangoHud) performance overlay. gamescope's "
                      "recommended way to get MangoHud: use this instead of MANGOHUD=1 on the "
                      "game or on gamescope itself (avoids double overlays and the extra "
                      "present hook on the game's swapchain). Don't combine with the "
                      "'MangoHud' prefix toggle in this builder — that runs a second "
                      "MangoHud instance on the game."),
    GamescopeFlagDef("Mouse Sensitivity", "--mouse-sensitivity", "Input", "string",
                      "Multiply mouse movement by the given decimal number (e.g. 0.5, 1.25). "
                      "Useful when the game renders at a lower -w/-h than the output.",
                      short="-s", placeholder="e.g. 1.0"),
    GamescopeFlagDef("Cursor Hotspot", "--cursor-hotspot", "Input", "string",
                      "Hotspot of the default cursor image set with --cursor, as x,y.",
                      placeholder="e.g. 0,0"),
    GamescopeFlagDef("Force Touch Pointer Emulation", "--xwayland-force-touch-pointer-emulation",
                      "Input", "toggle",
                      "Force XWayland to emulate pointer events from touch input."),
    GamescopeFlagDef("Fade-out Duration", "--fade-out-duration", "Session", "int",
                      "Duration in ms of the fade-out when switching between windows/apps.",
                      placeholder="e.g. 200"),
    GamescopeFlagDef("Force Composition Rotation", "--force-composition-rotation", "Debug", "toggle",
                      "Always rotate the output in the compositor instead of at scanout "
                      "(autodetected otherwise). Costs a composite pass every frame."),
]


# ============================================================================
# DXVK / VKD3D FORK ENV VARS
# ----------------------------------------------------------------------------
# Read straight out of the forks' own sources rather than their READMEs, so
# what's here is what the code actually looks up:
#
#   DXVK-Sarek          github.com/pythonlover02/DXVK-Sarek
#                       Vulkan 1.1/1.2 DXVK for hardware below upstream's
#                       requirements (1.10.x branch + backports).
#                       Officially supported by proton-cachyos:
#                       PROTON_DXVK_SAREK=1
#
#   dxvk-low-latency    github.com/netborg-afps/dxvk-low-latency
#                       Upstream DXVK plus a low-latency frame pacer.
#                       Shipped in proton-cachyos:
#                       PROTON_DXVK_LOWLATENCY=1
#                       (otherwise: drop the .dll files into a custom Proton)
#
#   vkd3d-low-latency   github.com/netborg-afps/vkd3d-low-latency
#                       vkd3d-proton plus the same frame pacer, wired to the
#                       Reflex API and waitable DXGI swapchains. Shipped in
#                       proton-cachyos: PROTON_VKD3D_LOWLATENCY=1
#                       (otherwise: replace d3d12.dll / d3d12core.dll inside a
#                       custom Proton build by hand).
#
#   d7vk                github.com/WinterSnowfall/d7vk
#                       D3D7/6/5/3 on top of DXVK's D3D9 backend. Standalone
#                       layer, no Proton switch. (Sarek carries the same
#                       feature set for older hardware.)
#
# Vars that turned out to be plain upstream DXVK / vkd3d-proton ones that
# DRSTool simply didn't cover yet are filed under the existing "DXVK" and
# "VKD3D-Proton" categories instead of here — labelling them as fork flags
# would be wrong.
# ============================================================================

FORK_ENV_VARS: List[EnvVarDef] = [
    # ── dxvk-low-latency + DXVK-Sarek ───────────────────────────────────
    EnvVarDef("DXVK_FRAME_PACE", "DXVK Forks", "enum", "",
              "[dxvk-low-latency | DXVK-Sarek] Frame pacing mode — how CPU and GPU are "
              "synchronised. Overrides the dxvk.framePace config option.\n"
              "max-frame-latency: stable latency in the GPU limit as long as render times "
              "are stable; higher latency but very smooth. Also what both forks fall back "
              "to when the value isn't recognised.\n"
              "low-latency: the default in both forks. Lower latency in the GPU limit, "
              "fine-tunable via dxvk.lowLatencyOffset and "
              "dxvk.lowLatencyAllowCpuFramesOverlap.\n"
              "min-latency: potentially the lowest latency, at the cost of fps — it stalls "
              "the GPU between frames. Generally not recommended; useful for tuning "
              "low-latency against, or for CPU-limited games.\n"
              "low-latency-vrr-<Hz>: dxvk-low-latency ONLY — VRR-aware pacing at the given "
              "refresh rate. The fork's README suggests DXVK_FRAME_RATE=225 together with "
              "low-latency-vrr-235 on a 240 Hz VRR panel. Sarek does not implement this "
              "mode and will fall back to max-frame-latency.\n"
              "Proton: proton-cachyos ships dxvk-low-latency behind "
              "PROTON_DXVK_LOWLATENCY=1 and DXVK-Sarek behind PROTON_DXVK_SAREK=1. "
              "Syntax: DXVK_FRAME_PACE=low-latency-vrr-235 DXVK_FRAME_RATE=225 %command%",
              options=["max-frame-latency", "low-latency", "min-latency",
                       "low-latency-vrr-165", "low-latency-vrr-235"]),

    # ── DXVK-Sarek only ────────────────────────────────────────────
    EnvVarDef("DXVK_ALL_CORES", "DXVK Forks", "enum", "",
              "[DXVK-Sarek] Lets the pipeline compiler and the state-cache worker use every "
              "logical core instead of Sarek's default reduced thread count. Sarek targets "
              "older/weaker CPUs where saturating all cores during shader compilation "
              "starves the render thread, so it holds threads back by default. Worth "
              "setting on a modern many-core CPU where that reasoning no longer applies. "
              "Not present in upstream DXVK. "
              "Syntax: PROTON_DXVK_SAREK=1 DXVK_ALL_CORES=1 %command%",
              options=["1"]),

    EnvVarDef("DXVK_DISABLE_DYASYNC", "DXVK Forks", "enum", "",
              "[DXVK-Sarek] Turns OFF dyasync (Dynamic Asynchronous Pipeline Compilation), "
              "which Sarek enables by default (dxvk.enableDyasync = True). With dyasync, a "
              "shader is compiled synchronously the first time it is seen; every later "
              "VARIANT of it (new blend mode, depth test, cull mode, render pass — same "
              "shaders) compiles in the background while DXVK draws with the closest "
              "already-compiled pipeline as a placeholder, then swaps it in. Safer than a "
              "classic async patch because something is always drawn, but it is still "
              "pipeline substitution — disable it for multiplayer titles with strict "
              "anti-cheat. Thread count is dxvk.numDyasyncThreads. "
              "Syntax: PROTON_DXVK_SAREK=1 DXVK_DISABLE_DYASYNC=1 %command%",
              options=["1"]),

    EnvVarDef("DXVK_PERF_EVENTS", "DXVK Forks", "enum", "",
              "[DXVK-Sarek] Enables VK_EXT_debug_utils so performance/debug events reach "
              "capture tools. Sarek hides the extension behind this variable because it "
              "adds real overhead in winevulkan — the fork logs a warning when it is on. "
              "Profiling only; leave unset for play. "
              "Syntax: PROTON_DXVK_SAREK=1 DXVK_PERF_EVENTS=1 %command%",
              options=["1"]),

    # ── d7vk ──────────────────────────────────────────────────────
    EnvVarDef("D7VK_LOG_LEVEL", "DXVK Forks", "enum", "",
              "[d7vk] Log level for the D3D7/6/5/3 translation layer. d7vk keeps its own "
              "logger separate from the DXVK D3D9 backend it sits on, so DXVK_LOG_LEVEL "
              "does NOT cover it — set both when tracing a DDraw-era title. "
              "Syntax: D7VK_LOG_LEVEL=debug D7VK_LOG_PATH=/tmp %command%",
              options=["trace", "debug", "info", "warn", "error", "none"]),

    EnvVarDef("D7VK_LOG_PATH", "DXVK Forks", "string", "",
              "[d7vk] Directory for the d7vk log file. Same split as D7VK_LOG_LEVEL: "
              "independent of DXVK_LOG_PATH. Use 'none' to suppress the file entirely. "
              "Syntax: D7VK_LOG_PATH=/tmp %command%",
              placeholder="e.g. /tmp  or  none"),

    # ── vkd3d-low-latency ──────────────────────────────────────
    EnvVarDef("VKD3D_LOW_LATENCY_OFFSET", "VKD3D Forks", "int", "100",
              "[vkd3d-low-latency] Fine-tunes the low-latency frame pacer, in "
              "MICROSECONDS. Larger values start a frame later, which can improve "
              "responsiveness slightly; the fork defaults to 100us rather than 0 because "
              "it does not yet measure the blit cost. Clamped to 0-500 in the fork's code "
              "(values are parsed unsigned, so a negative number wraps — don't use one "
              "here, unlike DXVK's dxvk.lowLatencyOffset which genuinely accepts "
              "-10000..10000).\n"
              "Only active when the game actually drives the pacer: it needs either the "
              "Nvidia Reflex API or a waitable DXGI swapchain. Confirm with PROTON_LOG=1 "
              "and look for the swapchain lines in the vkd3d log.\n"
              "Proton-CachyOS: enable the fork with PROTON_VKD3D_LOWLATENCY=1; other "
              "Protons need d3d12.dll and d3d12core.dll replaced by hand. "
              "Syntax: VKD3D_LOW_LATENCY_OFFSET=100 %command%",
              placeholder="e.g. 100 (microseconds, 0-500)"),
]


OVERLAY_ENV_VARS: List[EnvVarDef] = [
    EnvVarDef("MANGOHUD", "MangoHud / vkBasalt", "enum", "",
              "Load the MangoHud Vulkan layer for this process (alternative to the `mangohud` "
              "command prefix). Options come from MANGOHUD_CONFIG / MANGOHUD_CONFIGFILE or "
              "~/.config/MangoHud/MangoHud.conf.", options=["0", "1"]),
    EnvVarDef("MANGOHUD_CONFIG", "MangoHud / vkBasalt", "string", "",
              "Inline MangoHud options, comma separated (fps,cpu_temp,position=top-left). "
              "Overrides the config file for the keys it sets. Use the MangoHud builder "
              "(Extra Tools) to generate it.", placeholder="fps,frametime,position=top-left"),
    EnvVarDef("MANGOHUD_CONFIGFILE", "MangoHud / vkBasalt", "string", "",
              "Path of an explicit MangoHud config file instead of the per-user default.",
              placeholder="/home/you/.config/MangoHud/game.conf"),
    EnvVarDef("MANGOHUD_DLSYM", "MangoHud / vkBasalt", "enum", "",
              "Hook dlsym so MangoHud also works with OpenGL / when injected via LD_PRELOAD. "
              "The `mangohud --dlsym` prefix sets this for you.", options=["0", "1"]),
    EnvVarDef("VKBASALT_CONFIG_FILE", "MangoHud / vkBasalt", "string", "",
              "Path of the vkBasalt config file (default: ~/.config/vkBasalt/vkBasalt.conf). "
              "Pair with ENABLE_VKBASALT=1.", placeholder="/home/you/.config/vkBasalt/game.conf"),
    EnvVarDef("VKBASALT_LOG_LEVEL", "MangoHud / vkBasalt", "enum", "",
              "vkBasalt log verbosity.", options=["trace", "debug", "info", "warn", "error", "none"]),
    EnvVarDef("VKBASALT_LOG_FILE", "MangoHud / vkBasalt", "string", "",
              "Write the vkBasalt log to this file instead of stderr.", placeholder="/tmp/vkbasalt.log"),
]

ALL_ENV_VARS = (DXVK_ENV_VARS + VKD3D_ENV_VARS + NV_ENV_VARS + NVIDIA_PRIME_ENV_VARS +
                 PROTON_ENV_VARS + WINE_ENV_VARS + DXVK_NVAPI_ENV_VARS + NVPRESENT_ENV_VARS +
                 SYS_ENV_VARS + FLM_ENV_VARS + GAMESCOPE_ENV_VARS +
                 FORK_ENV_VARS + OVERLAY_ENV_VARS)

# ── Master logging toggle ────────────────────────────────────────────────────
# ON  = every logging switch enabled, every verbosity picker at ERROR level
#       (never debug/trace/info, except DXVK_NVAPI_LOG_LEVEL which has no
#       error level: "info" is its lowest "on" value).
# OFF = every picker at its none/off value; pure debug switches removed.
# Log *path/file* vars are left alone in both states.
LOG_ENV_ON: Dict[str, str] = {
    "DXVK_LOG_LEVEL": "error",
    "D7VK_LOG_LEVEL": "error",
    "VKD3D_DEBUG": "err",
    "VKD3D_SHADER_DEBUG": "err",
    "DXVK_NVAPI_LOG_LEVEL": "info",
    "DXVK_NVAPI_VKREFLEX_LAYER_LOG_LEVEL": "error",
    "NVPRESENT_LOG_LEVEL": "1",
    "FLM_LOG_LEVEL": "ERROR",
    "__GL_EVENT_LOGLEVEL": "ERROR",
    "PROTON_LOG": "1",
    "WINEDEBUG": "-all,err+all",
    "VK_LOADER_DEBUG": "error",
}
LOG_ENV_OFF: Dict[str, str] = {
    "DXVK_LOG_LEVEL": "none",
    "D7VK_LOG_LEVEL": "none",
    "VKD3D_DEBUG": "none",
    "VKD3D_SHADER_DEBUG": "none",
    "DXVK_NVAPI_LOG_LEVEL": "none",
    "DXVK_NVAPI_VKREFLEX_LAYER_LOG_LEVEL": "none",
    "NVPRESENT_LOG_LEVEL": "0",
    "FLM_LOG_LEVEL": "ERROR",      # FLM has no "off"; ERROR is its quietest
    "__GL_EVENT_LOGLEVEL": "OFF",
    "PROTON_LOG": "0",
    "WINEDEBUG": "-all",
}
LOG_ENV_UNSET_OFF = ("VK_LOADER_DEBUG", "FLM_STATS", "FLM_CSV",
                     "VKD3D_LOG_BUFFERED", "DXVK_DEBUG")
LOG_VKD3D_CONFIG_FLAGS = ("breadcrumbs", "breadcrumbs_sync", "breadcrumbs_trace",
                          "log_memory_budget", "fault", "pipeline_library_log")

# Sidebar display order for the Environment tab. Forks sit right under the
# upstream project they patch.
ENV_CATEGORY_ORDER = [
    "DXVK", "DXVK Forks", "VKD3D-Proton", "VKD3D Forks", "DXVK-NVAPI",
    "NVIDIA __GL", "NVIDIA PRIME", "NVIDIA Smooth Motion",
    "Proton", "Wine", "System / Loader", "MangoHud / vkBasalt", "vk_flip_meter", "Gamescope",
]


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

        for cat_label, env_list in self._categories():
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
        return " ".join(f"{k}={shlex.quote(v)}" for k, v in self._values.items())

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
                and not (set(v.get("VKD3D_CONFIG", "").split(",")) & set(LOG_VKD3D_CONFIG_FLAGS))):
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
            cfg = [f for f in self._values.get("VKD3D_CONFIG", "").split(",")
                   if f and f not in LOG_VKD3D_CONFIG_FLAGS]
            if cfg:
                self._values["VKD3D_CONFIG"] = ",".join(cfg)
            else:
                self._values.pop("VKD3D_CONFIG", None)
        self.refresh_colors()
        self.env_changed.emit()

    def load_values(self, values: Dict[str, str]):
        """Replace all values with the given dict and refresh."""
        self._values = values.copy()
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
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    font-family:monospace;
    font-size:12px;
    padding:5px 10px;
}
QLineEdit:focus{ border:1px solid #76b900; }
QLineEdit:hover{ background:#252c37; }
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── Header ────────────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(10)

        self._name_label = QLabel()
        self._name_label.setStyleSheet("""
QLabel{
    color:#f2f2f2;
    font-size:18px;
    font-weight:700;
}
""")
        self._name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._name_label, 1)

        self._cat_label = QLabel()
        self._cat_label.setStyleSheet("""
QLabel{
    background:#10141c;
    border:1px solid #313a48;
    border-radius:6px;
    padding:4px 10px;
    color:#8ea0ba;
    font-family:monospace;
    font-size:11px;
}
""")
        self._cat_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._cat_label)

        self._value_label = QLabel()
        self._value_label.setStyleSheet("""
QLabel{
    background:#152013;
    border:1px solid #76b900;
    border-radius:6px;
    padding:4px 10px;
    color:#9be238;
    font-family:monospace;
    font-size:11px;
    font-weight:600;
}
""")
        self._value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._value_label)
        self._value_label.hide()

        layout.addLayout(header)

        # ── Description ───────────────────────────────────────────────────────
        self._desc_label = QLabel()
        self._desc_label.setStyleSheet("""
QLabel{
    color:#a7afbc;
    font-size:13px;
    line-height:140%;
}
""")
        self._desc_label.setWordWrap(True)
        self._desc_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._desc_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self._desc_label)

        # ── Control area ──────────────────────────────────────────────────────
        self._control_widget, self._control_layout, control_scroll = _make_scrollable_control_area()
        layout.addWidget(control_scroll, 1)  # stretch=1: absorb all extra space, keep header/desc/button fixed

        # ── Clear button (mirrors "Remove Setting") ───────────────────────────
        remove_layout = QHBoxLayout()
        remove_layout.addStretch()
        self._clear_btn = QPushButton("Clear Variable")
        self._clear_btn.clicked.connect(self._on_clear)
        self._clear_btn.setStyleSheet("""
QPushButton{
    background:#b93b3b;
    border:1px solid #e45d5d;
    border-radius:5px;
    color:white;
    padding:4px 16px;
    font-weight:600;
    font-size:12px;
}
QPushButton:hover{ background:#ca4545; }
QPushButton:pressed{ background:#a73434; }
""")
        remove_layout.addWidget(self._clear_btn)
        layout.addLayout(remove_layout)
        self._clear_btn.hide()

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
        group = QButtonGroup(self)
        group.setExclusive(True)

        row, col = 0, 0
        for opt in ev.options:
            btn = QPushButton(opt)
            btn.setCheckable(True)
            if opt == cur:
                btn.setChecked(True)
            btn.setFixedHeight(30)
            btn.setStyleSheet("""
QPushButton{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:5px;
    font-size:11px;
    font-weight:600;
}
QPushButton:hover{
    background:#252c37;
    border:1px solid #76b900;
}
QPushButton:checked{
    background:rgba(118, 185, 0, 0.12);
    border:1px solid #76b900;
    color:#76b900;
}
""")
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
        tokens = [f.strip() for f in cur.replace(";", ",").split(",") if f.strip()] if cur else []
        active = set(tokens)
        # Tokens the grid doesn't know (flags removed/renamed upstream, fork
        # flags, typos) used to be dropped on the next toggle. Keep them
        # verbatim and append them after the grid's flags.
        self._vkd3d_extra = [t for t in tokens if t not in ev.options]

        # We keep a local dict so toggle logic can read state without re-querying widgets
        self._vkd3d_btns: Dict[str, QPushButton] = {}

        grid = QGridLayout()
        grid.setSpacing(6)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 2)

        # Column headers
        hdr_flag = QLabel("Flag")
        hdr_flag.setStyleSheet("color:#8a92a5; font-size:10px; font-weight:600;")
        hdr_desc = QLabel("Description")
        hdr_desc.setStyleSheet("color:#8a92a5; font-size:10px; font-weight:600;")
        grid.addWidget(hdr_flag, 0, 0)
        grid.addWidget(hdr_desc, 0, 1)

        # Separator line
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("border: none; border-top: 1px solid #1e2535;")
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
            desc_lbl.setStyleSheet("color:#a7afbc; font-size:11px;")

            grid.addWidget(btn,      r_idx + 2, 0)
            grid.addWidget(desc_lbl, r_idx + 2, 1)

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
            h.setStyleSheet("color:#8a92a5; font-size:10px; font-weight:600;")
            grid.addWidget(h, 0, c)

        combo_ss = ("QComboBox{background:#1a1f28;border:1px solid #323c4b;border-radius:6px;"
                    "color:#d8d8d8;padding:3px 8px;font-size:11px;}"
                    "QComboBox:hover{border:1px solid #76b900;}")
        for r, (key, kind, opt, desc) in enumerate(DXVK_CONFIG_KEYS, start=1):
            lbl = QLabel(key)
            lbl.setStyleSheet("color:#d8d8d8; font-family:monospace; font-size:11px;")
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
            d.setStyleSheet("color:#a7afbc; font-size:11px;")
            grid.addWidget(lbl, r, 0)
            grid.addWidget(w, r, 1)
            grid.addWidget(d, r, 2)
        self._control_layout.addLayout(grid)

        hint = QLabel("Other entries (any other dxvk.conf key, ; separated):")
        hint.setStyleSheet("color:#8a92a5; font-size:10px; font-weight:600;")
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
        btn.setStyleSheet("""
QPushButton{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    padding:4px 10px;
    font-size:11px;
    font-weight:600;
    text-align:left;
}
QPushButton:hover{
    background:#252c37;
    border:1px solid #76b900;
}
QPushButton:checked{
    background:rgba(118, 185, 0, 0.12);
    border:1px solid #76b900;
    color:#76b900;
}
""")

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
            btn.toggled.connect(lambda checked, o=opt: self._on_flag_toggled())
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

            extra_edit = QLineEdit(",".join(self._flag_extra_tokens))
            extra_edit.setStyleSheet(self._EDIT_SS)
            extra_edit.setPlaceholderText("e.g. scale=2")
            extra_edit.setFixedHeight(32)
            extra_edit.textChanged.connect(self._on_extra_flags_changed)
            self._control_layout.addWidget(extra_edit)

    def _on_flag_toggled(self):
        """Button click: commit immediately, same as VKD3D_CONFIG's flags."""
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
            # ever hold a non-negative integer — flip_meter.cpp atoi/atoll's
            # them and clamps. Reject non-numeric keystrokes here so a typo
            # like "6o" can't be committed and silently parsed as 6 (or 0)
            # by the layer. String-typed vars (paths, CPU ranges) untouched.
            edit.setValidator(QIntValidator(0, 2_000_000_000, edit))
        edit.textChanged.connect(self._on_text_changed)
        self._control_layout.addWidget(edit)

    # ── Setters ───────────────────────────────────────────────────────────────

    def _set_value(self, value: str):
        """Immediate commit: used by buttons (enum/vkd3d flags) which don't
        fire repeatedly the way keystrokes do, so no debounce needed here."""
        if not self._current_name:
            return
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


# ============================================================================
# Settings List - RED Categories, GREEN Active Settings
# ============================================================================

class SettingsListWidget(QListWidget):
    setting_selected = Signal(str)

    def __init__(self):
        super().__init__()
        self.setSelectionMode(QListWidget.SingleSelection)
        self.itemClicked.connect(self._on_item_clicked)
        # currentItemChanged also fires on keyboard Up/Down navigation, so
        # arrowing through the list opens each setting like a click does.
        # Headers have Qt.NoItemFlags, so keyboard navigation skips them.
        self.currentItemChanged.connect(self._on_current_item_changed)
        setup_sidebar_list(self)
        self._by_id: Dict[str, Setting] = {}
        self.only_set = False
        self.only_fav = False

    def populate(self, settings: List[Setting], current_state: Dict[str, str], filter_text: str = ""):
        # Block selection signals for the rebuild (see EnvVarsWidget.populate).
        self.blockSignals(True)
        self.clear()
        self._by_id = {s.id: s for s in settings}
        filter_lower = filter_text.lower()

        categories: Dict[str, List[Setting]] = {}
        for s in settings:
            if filter_text and not (
                filter_lower in s.name.lower() or
                filter_lower in s.id.lower() or
                filter_lower in s.cat.lower() or
                filter_lower in s.desc.lower()
            ):
                continue
            if self.only_set and s.id not in current_state:
                continue
            if self.only_fav and not FAVS.is_fav("drs", s.id):
                continue
            categories.setdefault(s.cat, []).append(s)

        for cat, items in categories.items():
            n_set = sum(1 for s in items if s.id in current_state)
            self.addItem(make_header_item(cat, n_set))
            for s in items:
                cur = current_state.get(s.id)
                self.addItem(make_list_item(
                    s.name, s.id, is_set=cur is not None,
                    value=_fmt_drs_value(s, cur) if cur is not None else "",
                    fav=FAVS.is_fav("drs", s.id), unavail=drs_unavailable(s)))
        self.blockSignals(False)

    def refresh_colors(self, current_state: Dict[str, str]):
        """
        Refresh set-state, value previews and header counts in place, without
        rebuilding the list. Use populate() only when the filter changes.
        """
        header = None
        count = 0
        for i in range(self.count()):
            item = self.item(i)
            if item.data(ROLE_KIND) == "header":
                if header is not None:
                    header.setData(ROLE_COUNT, count)
                header, count = item, 0
                continue
            sid = item.data(Qt.UserRole)
            cur = current_state.get(sid)
            item.setData(ROLE_SET, cur is not None)
            s = self._by_id.get(sid)
            item.setData(ROLE_VALUE, _fmt_drs_value(s, cur) if (s and cur is not None) else "")
            if cur is not None:
                count += 1
        if header is not None:
            header.setData(ROLE_COUNT, count)

    def _on_item_clicked(self, item: QListWidgetItem):
        setting_id = item.data(Qt.UserRole)
        if setting_id:
            self.setting_selected.emit(setting_id)

    def _on_current_item_changed(self, current: Optional[QListWidgetItem], previous: Optional[QListWidgetItem]):
        if current is None:
            return
        setting_id = current.data(Qt.UserRole)
        if setting_id:
            self.setting_selected.emit(setting_id)


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
                background: #141720;
                border: 1px solid #1e2535;
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
                background: #141720;
                border: 1px solid #1e2535;
                border-radius: 3px;
                color: #c8cdd8;
                font-size: 12px;
            }
            QListWidget::item {
                padding: 5px 10px;
                border-bottom: 1px solid #1e2535;
            }
            QListWidget::item:selected {
                background: rgba(118, 185, 0, 0.08);
                border-color: #76b900;
            }
            QListWidget::item:hover {
                background: #0d0f12;
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
                border: 1px solid #1e2535;
                border-radius: 3px;
                padding: 3px 10px;
                background: #141720;
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
                border: 1px solid #1e2535;
                border-radius: 3px;
                padding: 3px 10px;
                background: #141720;
                color: #e84545;
                font-size: 11px;
                min-height: 24px;
            }
            QPushButton:hover {
                border-color: #e84545;
            }
        """)
        btn_layout.addWidget(del_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

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

    def _refresh(self):
        self._profile_list.clear()
        profiles = self.settings_manager.get_profiles()
        current = self.settings_manager.get_current_profile()

        if not profiles:
            hint = make_header_item("No profiles yet — name one above and save")
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
        n_drs = len(prof.get("settings") or {})
        n_env = len(prof.get("env_vars") or {})
        arch = prof.get("arch") or ""
        gs = bool(prof.get("gamescope_enabled"))
        short = " ".join(p for p in (
            f"{n_drs} drs" if n_drs else "",
            f"{n_env} env" if n_env else "",
            arch, "gs" if gs else "") if p)
        tip = (f"{n_drs} DRS setting(s), {n_env} env var(s)\n"
               f"GPU arch: {arch or 'not set'}\n"
               f"Gamescope: {'on' if gs else 'off'}\n\nDouble-click to load.")
        return short or "empty", tip


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

from PySide6.QtCore import QEvent

class _NoScrollFilter(QObject):
    """Event filter that swallows wheel events so QComboBox/QSpinBox inside
    a QScrollArea don't change value when the user scrolls the page.
    Install with widget.installEventFilter(_NO_SCROLL_FILTER)."""

    def eventFilter(self, obj, event):          # noqa: N802
        if event.type() == QEvent.Wheel:
            # Let the scroll area (or any ancestor) handle the scroll instead
            event.ignore()
            return True
        return False


# Singleton — one instance shared across all widgets that need it.
_NO_SCROLL_FILTER = _NoScrollFilter()


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
                background: #141720; border: 1px solid #1e2535;
                color: #a8adb8; font-size: 11px; font-family: monospace;
                padding: 3px 8px; border-radius: 3px; min-height: 26px;
            }
        """)
        scan_row.addWidget(self._path_label, stretch=1)
        _btn_ss = """
            QPushButton {
                border: 1px solid #1e2535; border-radius: 3px; padding: 3px 8px;
                background: #141720; color: #c8cdd8; font-size: 11px; min-height: 26px;
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
                background: #141720; border: 1px solid #1e2535; color: #e8eaf0;
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
                border: 1px solid #1e2535; border-radius: 3px;
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
                border-radius:3px; background:#1a1f28;
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
                background: #141720; border: 1px solid #1e2535; color: #e8eaf0;
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
            f"  prelaunch_command: {self._LGTUNE_WRAPPER_PATH} PRE\n"
            f"  postexit_command:  {self._LGTUNE_WRAPPER_PATH} POST\n"
            f"  prefix_command:    {self._LGTUNE_WRAPPER_PATH} RUN <nice>\n"
            "directly into system.* — no separate prompt, same "
            "'Write to Lutris config' confirmation as everything else."
        )
        lgtune_note.setWordWrap(True)
        lgtune_note.setStyleSheet(
            "color:#8a92a5; font-size:13px; font-family:monospace;"
        )
        lgtune_layout.addWidget(lgtune_note)

        layout.addWidget(lgtune_group)

        # ── Preview ───────────────────────────────────────────────────────────
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setStyleSheet("""
            QPlainTextEdit {
                background: #0d0f12; border: 1px solid #1e2535;
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
            QPushButton:disabled { background: #2a2f38; color: #8a92a5; border-color: #1e2535; }
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
                background: #1a1f28; color: #d8d8d8; border: 1px solid #323c4b;
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
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
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
                    data = yaml.safe_load(f) or {}
            except Exception:
                continue
            game_section = data.get("game") or {}
            exe = game_section.get("exe", "")
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
            data = yaml.safe_load(self._current_yaml_text) or {}
        except Exception:
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
            data = yaml.safe_load(self._current_yaml_text) or {}
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

        # ── Env vars ────────────────────────────────────────────────
        env_sec = sysdict.get("env") or {}
        if isinstance(env_sec, dict):
            win = self.window()
            if win is not None and hasattr(win, "_env_widget"):
                # These two are DERIVED from the DRS settings grid + arch
                # selector, not free-form env vars — pushing them into the env
                # widget would create a second, conflicting source of truth.
                derived = {"DXVK_NVAPI_DRS_SETTINGS", "DXVK_NVAPI_GPU_ARCH"}
                vals = {str(k): str(v) for k, v in env_sec.items() if str(k) not in derived}
                skipped = derived & {str(k) for k in env_sec}
                if skipped:
                    notes.append("not imported (owned by the DRS settings tab): "
                                 + ", ".join(sorted(skipped)))
                win._env_widget.load_values(vals)

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
            system_keys["prelaunch_command"] = f"{self._LGTUNE_WRAPPER_PATH} PRE"
            system_keys["postexit_command"] = f"{self._LGTUNE_WRAPPER_PATH} POST"
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

    def _update_preview(self):
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
            data = yaml.safe_load(new_text) or {}
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
        data = yaml.safe_load(original_text) or {}
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

    def _apply_to_game(self):
        index = self._game_combo.currentIndex()
        if not (0 <= index < len(self._games)):
            return
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
            return

        # A removal-only round is still a real change. The old guard bailed
        # out whenever nothing was being WRITTEN, so clearing a control in
        # the GUI could never propagate to disk.
        total = (len(sys_changed) + len(env_changed)
                 + len(sys_removed) + len(env_removed))
        if total == 0:
            self._status_label.setText("Nothing to write — file already matches the GUI.")
            return
        sys_line = f"\n  • {len(sys_changed)} key(s) into system.*" if sys_changed else ""
        env_line = f"\n  • {len(env_changed)} var(s) into system.env" if env_changed else ""
        del_line = (f"\n  • {len(sys_removed)} stale key(s) removed from system.*: "
                    f"{', '.join(sorted(sys_removed))}") if sys_removed else ""
        envdel_line = (f"\n  • {len(env_removed)} stale var(s) removed from system.env: "
                       f"{', '.join(sorted(env_removed))}") if env_removed else ""
        warn_line = ("\n\n⚠ " + "\n⚠ ".join(warnings)) if warnings else ""
        reply = QMessageBox.question(
            self, "Write Lutris config",
            f"Write {total} change(s) into:\n{entry.path}"
            f"{sys_line}{env_line}{del_line}{envdel_line}{warn_line}\n\n"
            "A timestamped backup will be created first.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # Lutris (or the user) may have touched the file while the dialog was
        # open; merging into stale text would silently overwrite that change.
        try:
            with open(entry.path, "r", encoding="utf-8") as f:
                fresh = f.read()
        except Exception as e:
            QMessageBox.critical(self, "Write failed", f"Could not re-read {entry.path}: {e}")
            return
        if fresh != original_text:
            self._current_yaml_text = fresh
            self._update_preview()
            QMessageBox.warning(
                self, "File changed on disk",
                f"{entry.path.name} was modified while the confirmation dialog was open.\n"
                "Nothing was written. The preview has been refreshed — review it and apply again.")
            return

        try:
            # Resolve symlinks first: os.replace() on the link itself would
            # swap the symlink for a regular file.
            target = Path(os.path.realpath(entry.path))
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = target.with_name(f"{target.name}.{stamp}.bak")
            shutil.copy2(target, backup_path)
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
            return

        self._current_yaml_text = new_text
        w = self.window()
        if hasattr(w, "_ext_mark_lutris"):
            w._ext_mark_lutris()
        self._status_label.setText(
            f"Wrote {total} change(s) to {entry.path.name}  (backup: {backup_path.name})"
        )
        self._update_preview()


# FLM v2.x variables removed in v3.0 (mirrors k_legacy_keys in
# flip_meter.cpp). Owned by DRSTool → deleted from game YAMLs on Apply.
FLM_LEGACY_ENV_KEYS = (
    "FLM_PACE_POINT", "FLM_PACE_FIFO", "FLM_PRESENT_LEAD_NS", "FLM_SPIN_NS",
    "FLM_SPIN_ADAPT", "FLM_DRIFT_TOLERANCE_NS", "FLM_FLOOR_PACING",
    "FLM_FLOOR_MFG_ADAPT", "FLM_FLOOR_MFG_STEP", "FLM_FLOOR_AUTOTUNE",
    "FLM_FLOOR_AUTOTUNE_MAX", "FLM_WARMUP_FRAMES", "FLM_HITCH_RECOVERY",
    "FLM_HITCH_THRESHOLD_MS", "FLM_PROBE_PERIOD_S", "FLM_PROBE_FLIPS",
    "FLM_STATS_INTERVAL", "FLM_CSV_SYNC_S", "FLM_VERBOSE",
)

# Keys the layer accepts through the FLM_CONFIG file + SIGUSR1 hot-reload.
# Mirrors apply_dynamic_kv() in flip_meter.cpp (v3.0) —
# keep in sync when the layer gains/loses reloadable knobs.
FLM_RELOADABLE_KEYS = (
    "FLM_MODE", "FLM_PROFILE", "FLM_TARGET_FPS", "FLM_FLOOR_RATIO", "FLM_LOG_LEVEL",
)

# ============================================================================
# Extra Tools tab — vk_flip_meter + lutris-game-tune
# ============================================================================
#
# vk_flip_meter build: UNPRIVILEGED (cmake configure + cmake --build, plain
#   QProcess). Only cmake --install and the manifest path fixup need root —
#   those two steps run through pkexec.
#
# lutris-game-tune: all operations that touch /etc/ or the setuid wrapper
#   run through pkexec. Config is read from /etc/lutris-game-tune.conf at
#   startup and written back atomically via pkexec tee.

# Installation paths (detection)
LGTUNE_WRAPPER = Path("/usr/local/bin/lutris-game-tune-wrapper")
LGTUNE_CONF    = Path("/etc/lutris-game-tune.conf")
LGTUNE_LOG     = Path("/var/log/lutris-game-tune.log")
LGTUNE_LIB_DIR = Path("/usr/local/lib/lutris-game-tune")

FLM_FIELD_SS = """
QLineEdit{
    background:#1a1f28;
    border:1px solid #323c4b;
    border-radius:6px;
    color:#d8d8d8;
    font-family:monospace;
    font-size:12px;
    padding:5px 10px;
}
QLineEdit:focus{ border:1px solid #76b900; }
QLineEdit:hover{ background:#252c37; }
"""

# Shared stylesheet for group boxes inside the Extra Tools panels
_TOOL_GROUP_SS = """
QGroupBox{
    color:#8ea0ba; font-size:12px; font-weight:700;
    border:1px solid #1e2535; border-radius:6px;
    margin-top:8px; padding-top:14px;
}
QGroupBox::title{ subcontrol-origin:margin; left:8px; padding:0 4px; }
"""

# Shared button factory (used by both FlmInstallWidget and LgtuneWidget)
def _tool_btn(label: str, bg="#1a1f28", border="#323c4b", fg="#d8d8d8",
               bold=False) -> QPushButton:
    weight = 700 if bold else 600
    btn = QPushButton(label)
    btn.setStyleSheet(f"""
QPushButton{{
    background:{bg}; border:1px solid {border}; border-radius:5px;
    color:{fg}; padding:5px 14px; font-weight:{weight}; font-size:12px;
}}
QPushButton:hover{{ border:1px solid #76b900; }}
QPushButton:disabled{{ color:#3a4250; border:1px solid #232a36; background:#12151b; }}
""")
    return btn


class ExtraToolsSidebarWidget(QWidget):
    """
    Left-sidebar page for the Extra Tools tab.  Not a selectable list —
    just orientation text for all ten sub-tabs.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        title = QLabel("Extra Tools")
        title.setStyleSheet("color:#e8eaf0; font-size:15px; font-weight:700;")
        layout.addWidget(title)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.HLine)
        sep1.setStyleSheet("color:#1e2535;")
        layout.addWidget(sep1)

        # ── vk_flip_meter blurb ──────────────────────────────────────────────
        vkfm_title = QLabel("vk_flip_meter")
        vkfm_title.setStyleSheet("color:#c8cdd8; font-size:13px; font-weight:700;")
        layout.addWidget(vkfm_title)

        vkfm_sub = QLabel("Frame Pacing / Cadence Modulation Vulkan Layer")
        vkfm_sub.setWordWrap(True)
        vkfm_sub.setStyleSheet("color:#8a92a5; font-size:13px;")
        layout.addWidget(vkfm_sub)

        vkfm_info = QLabel(
            "Build & install the layer on the right (cmake, pkexec for the "
            "install step).  Runtime env vars (FLM_MODE, FLM_TARGET_FPS, …) "
            "live on the \"DXVK / VKD3D / NV / FLM\" tab — they're added to "
            "Copy All automatically.  The Live Tuning section lets you "
            "hot-reload knobs into a running game via FLM_CONFIG + SIGUSR1."
        )
        vkfm_info.setWordWrap(True)
        vkfm_info.setStyleSheet("color:#a7afbc; font-size:12px; line-height:145%;")
        layout.addWidget(vkfm_info)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("color:#1e2535;")
        layout.addWidget(sep2)

        # ── lutris-game-tune blurb ───────────────────────────────────────────
        lgt_title = QLabel("lutris-game-tune")
        lgt_title.setStyleSheet("color:#c8cdd8; font-size:13px; font-weight:700;")
        layout.addWidget(lgt_title)

        lgt_sub = QLabel("System-wide Lutris / Wine performance tuner")
        lgt_sub.setWordWrap(True)
        lgt_sub.setStyleSheet("color:#8a92a5; font-size:13px;")
        layout.addWidget(lgt_sub)

        lgt_info = QLabel(
            "Manages CPU governor, PCIe ASPM, deep C-states, VM swappiness, "
            "Transparent HugePages, and CCD/CCX core isolation at game launch "
            "via a setuid root wrapper.  Settings are stored system-wide in "
            "/etc/lutris-game-tune.conf (root-owned, written via pkexec).  "
            "Not part of DRSTool profiles — applies to all games globally."
        )
        lgt_info.setWordWrap(True)
        lgt_info.setStyleSheet("color:#a7afbc; font-size:12px; line-height:145%;")
        layout.addWidget(lgt_info)

        sep3 = QFrame()
        sep3.setFrameShape(QFrame.HLine)
        sep3.setStyleSheet("color:#1e2535;")
        layout.addWidget(sep3)

        # ── DLSS Updater blurb ───────────────────────────────────────────────
        ngx_title_lbl = QLabel("DLSS Updater")
        ngx_title_lbl.setStyleSheet("color:#c8cdd8; font-size:13px; font-weight:700;")
        layout.addWidget(ngx_title_lbl)

        ngx_sub = QLabel("DLSS SR / RR / FG + Streamline from NVIDIA's NGX servers")
        ngx_sub.setWordWrap(True)
        ngx_sub.setStyleSheet("color:#8a92a5; font-size:13px;")
        layout.addWidget(ngx_sub)

        ngx_info = QLabel(
            "Lists every build NVIDIA hosts for the driver's NGX updater — including "
            "staged ones found via server-config pins and version probes — and saves "
            "the selected ones as nvngx_dlss*.dll / sl.*.dll into a version-named "
            "folder, SHA-256 checked.  Read-only: talks only to "
            "ngx.download.nvidia.com, never touches game folders."
        )
        ngx_info.setWordWrap(True)
        ngx_info.setStyleSheet("color:#a7afbc; font-size:12px; line-height:145%;")
        layout.addWidget(ngx_info)

        # ── remaining sub-tabs (Linter … Upstream drift) ─────────────────────
        _more = (
            ("Linter", "Configuration consistency check",
             "Scans the current configuration for combinations that contradict "
             "each other or cannot work on this system (e.g. PROTON_NVIDIA_LIBS + "
             "WoW64, FSR4 on NVIDIA, ENABLE_HDR_WSI on drivers \u2265 595, double "
             "latency stacks).  Re-runs automatically on every change; each "
             "finding can be jumped to."),
            ("System", "Detected hardware / driver / Vulkan info",
             "Shows the NVIDIA driver, session type (X11/Wayland) and each Vulkan "
             "GPU with the device extensions DRSTool cares about (Reflex low-"
             "latency, descriptor heap/buffer, HDR, present wait/id, optical "
             "flow, mesh shaders…).  Optionally greys out options this system "
             "can't run \u2014 you can still force them with \u201cEdit anyway\u201d."),
            ("Prefix Doctor", "Wine prefix DLL checker / updater",
             "Compares the DXVK, vkd3d-proton and dxvk-nvapi DLLs inside a Wine "
             "prefix with the latest GitHub releases and replaces outdated ones "
             "(originals are backed up).  For Wine / Lutris / Bottles / Heroic "
             "prefixes \u2014 Proton re-syncs its own prefixes on launch."),
            ("MangoHud", "MangoHud overlay config builder",
             "Pick overlay options (with filter and presets) and either set the "
             "result as the MANGOHUD_CONFIG env var or save a config file and "
             "point MANGOHUD_CONFIGFILE at it."),
            ("vkBasalt", "vkBasalt post-processing builder",
             "Choose and drag-reorder vkBasalt effects (checked ones run top to "
             "bottom), tweak their parameters, set a LUT file and add extra "
             "ReShade effects (name = /path/to/effect.fx) to generate a "
             "vkBasalt config."),
            ("Cache", "Shader / pipeline cache manager",
             "Shows sizes of the NVIDIA, Mesa, DXVK and vkd3d-proton caches: "
             "driver caches, Steam shadercache/<appid> folders (all libraries), "
             "Lutris per-game folders and paths from the Env Vars tab.  Lets you "
             "clear them; \u201cAdd folder\u2026\u201d includes .dxvk-cache files "
             "next to game executables.  The tab shows \u26a0 when caches are stale."),
            ("Upstream drift", "Catalog vs. upstream comparison",
             "Downloads current DXVK, vkd3d-proton, dxvk-nvapi and gamescope "
             "sources plus NVIDIA's NvApiDriverSettings.h and compares them with "
             "DRSTool's catalog (env vars, dxvk.conf keys, VKD3D_CONFIG flags, "
             "gamescope flags, DRS IDs).  \u201cNew\u201d = upstream only, "
             "\u201cGone\u201d = catalog only; reviewed rows can be hidden."),
        )
        for _t, _s, _i in _more:
            _sep = QFrame()
            _sep.setFrameShape(QFrame.HLine)
            _sep.setStyleSheet("color:#1e2535;")
            layout.addWidget(_sep)
            _tl = QLabel(_t)
            _tl.setStyleSheet("color:#c8cdd8; font-size:13px; font-weight:700;")
            layout.addWidget(_tl)
            _sl = QLabel(_s)
            _sl.setWordWrap(True)
            _sl.setStyleSheet("color:#8a92a5; font-size:13px;")
            layout.addWidget(_sl)
            _il = QLabel(_i)
            _il.setWordWrap(True)
            _il.setStyleSheet("color:#a7afbc; font-size:12px; line-height:145%;")
            layout.addWidget(_il)

        layout.addStretch()


class FlmInstallWidget(QWidget):
    """
    Right-panel build/install widget for vk_flip_meter.
    Source is bundled at _SCRIPT_DIR/vk-flip-meter-main — no picker needed.
    Unprivileged: cmake configure, cmake --build.
    Privileged (pkexec, graphical password prompt): cmake --install.
    """

    _STEPS = ("configure", "build", "install", "verify")

    # Bundled source path (repo layout: DRSTool/vk-flip-meter-main/)
    _BUNDLED_SRC = find_bundled_dir("vk-flip-meter-main", "CMakeLists.txt")
    _out_buf: str = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: Optional[QProcess] = None
        self._phase: str = ""
        self._build_dir: str = ""
        self._prefix_used: str = ""
        self._libdir_used: str = "lib64"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        header = QLabel("vk_flip_meter — Build & Install")
        header.setStyleSheet("color:#f2f2f2; font-size:18px; font-weight:700;")
        layout.addWidget(header)

        desc = QLabel(
            "Source is bundled in the repository at <code>vk-flip-meter-main/</code>.  "
            "The build (cmake configure + build) runs as a normal user.  Only "
            "<code>cmake --install</code> — the step that writes to <code>/usr/local</code> — "
            "runs through pkexec with a graphical password prompt."
        )
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.RichText)
        desc.setStyleSheet("color:#a7afbc; font-size:13px; line-height:140%;")
        layout.addWidget(desc)

        # Source path banner (read-only, bundled)
        src_row = QHBoxLayout()
        src_row.setSpacing(6)
        src_icon = QLabel("📁")
        src_lbl = QLabel("Source:")
        src_lbl.setStyleSheet("color:#8ea0ba; font-size:12px; font-weight:600;")
        self._src_path_lbl = QLabel(str(self._BUNDLED_SRC))
        self._src_path_lbl.setStyleSheet(
            "color:#76b900; font-size:12px; font-family:monospace;"
        )
        src_ok = (self._BUNDLED_SRC / "CMakeLists.txt").is_file()
        self._src_status = QLabel("✔ found" if src_ok else "✘ not found")
        self._src_status.setStyleSheet(
            f"color:{'#76b900' if src_ok else '#c04040'}; font-size:12px; font-weight:700;"
        )
        src_row.addWidget(src_icon)
        src_row.addWidget(src_lbl)
        src_row.addWidget(self._src_path_lbl, 1)
        src_row.addWidget(self._src_status)
        layout.addLayout(src_row)

        form = QGridLayout()
        form.setSpacing(8)

        prefix_lbl = QLabel("Install prefix:")
        prefix_lbl.setStyleSheet("color:#8ea0ba; font-size:12px; font-weight:600;")
        self._prefix_edit = QLineEdit("/usr/local")
        self._prefix_edit.setStyleSheet(FLM_FIELD_SS)
        form.addWidget(prefix_lbl, 0, 0)
        form.addWidget(self._prefix_edit, 0, 1)

        layout.addLayout(form)

        self._native_chk = QCheckBox(
            "FLM_NATIVE_BUILD  (-O3 -march=native -mtune=native -flto — specific to this machine, not portable)"
        )
        self._native_chk.setStyleSheet("""
QCheckBox{ color:#d8d8d8; font-size:12px; spacing:8px; }
QCheckBox::indicator{ width:14px; height:14px; border:1px solid #323c4b; border-radius:3px; background:#1a1f28; }
QCheckBox::indicator:checked{ background:#76b900; border:1px solid #76b900; }
""")
        layout.addWidget(self._native_chk)

        native_warn = QLabel(
            "If enabled: the .so built for the 7845HX may not run on a different CPU (a different machine)."
        )
        native_warn.setStyleSheet("color:#8a92a5; font-size:13px; font-style:italic;")
        layout.addWidget(native_warn)

        btn_row = QHBoxLayout()
        self._build_btn = _tool_btn("Build & Install (pkexec)", "#76b900", "#76b900", "#0d0f12", bold=True)
        self._build_btn.clicked.connect(self._on_build_clicked)

        self._verify_btn = _tool_btn("Verify Installation")
        self._verify_btn.clicked.connect(self._on_verify_clicked)

        clear_btn = _tool_btn("Clear Console")
        clear_btn.clicked.connect(lambda: self._console.clear())

        btn_row.addWidget(self._build_btn)
        btn_row.addWidget(self._verify_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._status_lbl = QLabel("Ready.")
        self._status_lbl.setStyleSheet("color:#8a92a5; font-size:13px;")
        layout.addWidget(self._status_lbl)

        # ── Live tuning: FLM_CONFIG file + SIGUSR1 ───────────────────────────
        # The layer's env is snapshotted once at init [FIX-26]; the ONLY way
        # to retune a running game is FLM_CONFIG file + SIGUSR1. This section
        # turns the manual `echo KEY=VAL > /tmp/flm.conf && kill -USR1 ...`
        # dance into two buttons: dump the currently-set hot-reloadable FLM_*
        # vars (from the Env Vars tab) into the .conf, then signal the game.
        live_group = QGroupBox("Live Tuning  (FLM_CONFIG + SIGUSR1)")
        live_group.setStyleSheet("""
QGroupBox{ color:#8ea0ba; font-size:12px; font-weight:700;
    border:1px solid #1e2535; border-radius:6px;
    margin-top:8px; padding-top:12px; }
QGroupBox::title{ subcontrol-origin: margin; left:8px; padding:0 4px; }
""")
        live_layout = QGridLayout(live_group)
        live_layout.setSpacing(6)

        conf_lbl = QLabel("Config file:")
        conf_lbl.setStyleSheet("color:#8ea0ba; font-size:13px; font-weight:600;")
        self._conf_edit = QLineEdit(str(default_flm_conf_path()))
        self._conf_edit.setStyleSheet(FLM_FIELD_SS)
        self._write_conf_btn = _tool_btn("Write .conf")
        self._write_conf_btn.clicked.connect(self._on_write_conf)
        live_layout.addWidget(conf_lbl, 0, 0)
        live_layout.addWidget(self._conf_edit, 0, 1)
        live_layout.addWidget(self._write_conf_btn, 0, 2)

        proc_lbl = QLabel("Process match:")
        proc_lbl.setStyleSheet("color:#8ea0ba; font-size:13px; font-weight:600;")
        self._proc_edit = QLineEdit()
        self._proc_edit.setStyleSheet(FLM_FIELD_SS)
        self._proc_edit.setPlaceholderText("optional regex on the command line, e.g. GameName.exe")
        self._sigusr1_btn = _tool_btn("Send SIGUSR1")
        self._sigusr1_btn.clicked.connect(self._on_send_sigusr1)
        live_layout.addWidget(proc_lbl, 1, 0)
        live_layout.addWidget(self._proc_edit, 1, 1)
        live_layout.addWidget(self._sigusr1_btn, 1, 2)

        live_hint = QLabel(
            "Only works if the game was launched with FLM_CONFIG pointing at "
            "this file. Init-only vars (FLM_CSV, FLM_RT_PRIORITY, ...) are "
            "written as comments — the layer can't change them at runtime."
        )
        live_hint.setWordWrap(True)
        live_hint.setStyleSheet("color:#8a92a5; font-size:13px; font-style:italic;")
        live_layout.addWidget(live_hint, 2, 0, 1, 3)

        layout.addWidget(live_group)

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setStyleSheet("""
QPlainTextEdit{
    background:#0a0c0f;
    border:1px solid #1e2535;
    border-radius:6px;
    color:#9be238;
    font-family:monospace;
    font-size:13px;
    padding:6px;
}
""")
        layout.addWidget(self._console, 1)

        self._autocheck_src()

    # ── bundled source check ─────────────────────────────────────────────────

    def _autocheck_src(self):
        """Update the source status banner for the bundled path."""
        src_ok = (self._BUNDLED_SRC / "CMakeLists.txt").is_file()
        self._src_status.setText("✔ found" if src_ok else "✘ not found")
        self._src_status.setStyleSheet(
            f"color:{'#76b900' if src_ok else '#c04040'}; font-size:12px; font-weight:700;"
        )

    # ── console ──────────────────────────────────────────────────────────────

    def _log(self, text: str):
        self._console.appendPlainText(text)
        self._console.verticalScrollBar().setValue(self._console.verticalScrollBar().maximum())

    def _set_busy(self, busy: bool, status: str = ""):
        self._build_btn.setEnabled(not busy)
        self._verify_btn.setEnabled(not busy)
        self._status_lbl.setText(status or ("Working..." if busy else "Ready."))

    # ── live tuning (FLM_CONFIG + SIGUSR1) ───────────────────────────────────

    def _collect_flm_values(self) -> Dict[str, str]:
        """Currently-set FLM_* env vars from the shared Env Vars tab."""
        win = self.window()
        if win and hasattr(win, "_env_widget"):
            return {k: v for k, v in win._env_widget.get_env_dict().items()
                    if k.startswith("FLM_")}
        return {}

    def _on_write_conf(self):
        path_str = self._conf_edit.text().strip()
        if not path_str:
            self._log("ERROR: config file path is empty.")
            return
        values = self._collect_flm_values()
        if not values:
            self._log("Nothing to write — no FLM_* vars are set on the Env Vars tab.")
            return

        reloadable = {k: v for k, v in values.items() if k in FLM_RELOADABLE_KEYS}
        init_only  = {k: v for k, v in values.items()
                      if k not in FLM_RELOADABLE_KEYS and k != "FLM_CONFIG"}

        lines = [
            "# vk_flip_meter live config — written by DRSTool",
            f"# {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "# Apply with: kill -USR1 <game pid>  (or the Send SIGUSR1 button)",
            "",
        ]
        lines += [f"{k}={v}" for k, v in reloadable.items()]
        if init_only:
            lines.append("")
            lines.append("# init-only (not hot-reloadable — set in the launch env instead):")
            lines += [f"# {k}={v}" for k, v in init_only.items()]

        try:
            path = Path(os.path.expanduser(path_str))
            path.parent.mkdir(parents=True, exist_ok=True)
            # Unpredictable temp name in the same directory, then an atomic
            # rename: SIGUSR1 mid-write can't see a torn file.
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            self._log(f"ERROR: could not write {path_str}: {e}")
            return

        self._log(f"Wrote {len(reloadable)} reloadable key(s) to {path_str}"
                  + (f" ({len(init_only)} init-only key(s) as comments)" if init_only else ""))
        # If the game env doesn't point FLM_CONFIG at this exact path, the
        # reload will read a different file (or nothing) — warn early.
        cfg = values.get("FLM_CONFIG", "")
        if not cfg:
            self._log("NOTE: FLM_CONFIG isn't set on the Env Vars tab — the game must be "
                      f"launched with FLM_CONFIG={path_str} for SIGUSR1 to pick this up.")
        elif cfg != path_str:
            self._log(f"WARNING: Env Vars tab has FLM_CONFIG={cfg}, but this file was "
                      f"written to {path_str} — the running game will reload the former.")

    @staticmethod
    def _find_layer_pids(pattern: str):
        """(pid, cmdline) of this user's processes that have libvk_flip_meter
        mapped, optionally filtered by a regex on the command line. `pkill -f`
        matched ANY command line containing the pattern (Lutris, wrappers,
        DRSTool itself) and SIGUSR1's default action kills a process."""
        rx = None
        if pattern:
            try:
                rx = re.compile(pattern)
            except re.error:
                rx = re.compile(re.escape(pattern))
        me, uid = os.getpid(), os.getuid()
        found = []
        for d in os.listdir("/proc"):
            if not d.isdigit() or int(d) == me:
                continue
            pid = int(d)
            try:
                if os.stat(f"/proc/{pid}").st_uid != uid:
                    continue
                with open(f"/proc/{pid}/maps", "rb") as f:
                    if b"libvk_flip_meter" not in f.read():
                        continue
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
            except OSError:
                continue
            if rx and not rx.search(cmd):
                continue
            found.append((pid, cmd))
        return found

    def _on_send_sigusr1(self):
        pattern = self._proc_edit.text().strip()
        pids = self._find_layer_pids(pattern)
        if not pids:
            self._log("No process with the vk_flip_meter layer loaded"
                      + (f" matches '{pattern}'" if pattern else "")
                      + " — is the game running with ENABLE_LAYER_cpu_flip_meter=1?")
            self._status_lbl.setText("No process matched.")
            return
        sent = 0
        for pid, cmd in pids:
            try:
                os.kill(pid, signal.SIGUSR1)
                sent += 1
                self._log(f"SIGUSR1 -> {pid}  {cmd[:100]}")
            except OSError as e:
                self._log(f"Could not signal {pid}: {e}")
        if sent:
            self._log("SIGUSR1 sent — the layer will re-read FLM_CONFIG on its next frame.")
            self._status_lbl.setText(f"SIGUSR1 sent to {sent} process(es).")

    # ── build / install pipeline ─────────────────────────────────────────────

    def _on_build_clicked(self):
        src = str(self._BUNDLED_SRC)
        prefix = self._prefix_edit.text().strip() or "/usr/local"

        if not (Path(src) / "CMakeLists.txt").is_file():
            QMessageBox.warning(
                self, "Bundled source not found",
                f"CMakeLists.txt not found at:\n  {src}\n\n"
                "Make sure the vk-flip-meter-main/ directory is present "
                "next to DRSTool.py (it is part of the DRSTool repository)."
            )
            return

        self._prefix_used = prefix
        # Out-of-tree build in the user's cache: the bundled source may be
        # read-only (/usr/share/drstool) and must stay clean when it is not.
        self._build_dir = str(self._prepare_build_dir(Path(src)))
        native = "ON" if self._native_chk.isChecked() else "OFF"
        generator = "Ninja" if shutil.which("ninja") else "Unix Makefiles"

        self._console.clear()
        self._log(f"==> Source: {src}")
        self._log(f"==> Configuring  (generator={generator}, FLM_NATIVE_BUILD={native}, prefix={prefix})")
        self._set_busy(True, "Configuring (cmake configure)...")
        self._phase = "configure"

        # FLM_LIB_PATH is injected so the manifest template gets the correct
        # lib64 path; CMake's configure_file handles the manifest (FIX-52/61).
        libdir = detect_libdir()
        self._libdir_used = libdir
        flm_lib_path = f"{prefix}/{libdir}/libvk_flip_meter.so"
        args = [
            "-S", src, "-B", self._build_dir,
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_INSTALL_PREFIX={prefix}",
            f"-DCMAKE_INSTALL_LIBDIR={libdir}",
            f"-DFLM_NATIVE_BUILD={native}",
            f"-DFLM_LIB_PATH={flm_lib_path}",
            "-G", generator,
        ]
        self._run_process("cmake", args, use_pkexec=False)

    @staticmethod
    def _prepare_build_dir(src: Path) -> Path:
        build = user_cache_dir() / "flm-build"
        cache = build / "CMakeCache.txt"
        try:
            if cache.is_file():
                m = re.search(r"^CMAKE_HOME_DIRECTORY:INTERNAL=(.*)$",
                              cache.read_text(errors="replace"), re.M)
                # A cache from a different source path makes cmake refuse to configure.
                if m and Path(m.group(1)) != src:
                    shutil.rmtree(build, ignore_errors=True)
        except OSError:
            pass
        build.mkdir(parents=True, exist_ok=True)
        return build

    def _on_verify_clicked(self):
        self._console.clear()
        self._log("==> vulkaninfo --summary | grep -i flip_meter")
        self._set_busy(True, "Verifying...")
        self._phase = "verify"
        cmd = "vulkaninfo --summary 2>/dev/null | grep -i flip_meter || echo 'flip_meter not found — check whether the layer is loaded / vulkaninfo is installed.'"
        self._run_process("bash", ["-c", cmd], use_pkexec=False)

    def _run_process(self, program: str, args: List[str], use_pkexec: bool):
        if self._proc is not None and self._proc.state() != QProcess.NotRunning:
            self._log("A step is already running — please wait.")
            return
        self._out_buf = ""
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(lambda p=proc: self._on_output(p))
        proc.finished.connect(self._on_proc_finished)
        # QProcess.finished NEVER fires on FailedToStart (binary not found /
        # not executable) — without this handler a missing cmake, ninja, or
        # pkexec left the whole widget stuck on "Working..." with both
        # buttons disabled until app restart.
        proc.errorOccurred.connect(lambda err, p=proc: self._on_proc_error(err, p))
        self._proc = proc
        if use_pkexec:
            proc.start("pkexec", [program] + args)
        else:
            proc.start(program, args)

    def _on_output(self, proc: QProcess):
        data = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        if data:
            self._out_buf = (self._out_buf + data)[-4000:]
            self._log(data.rstrip("\n"))

    def _on_proc_error(self, error, proc: QProcess):
        if error == QProcess.FailedToStart:
            self._log(f"ERROR: could not start '{proc.program()}' — is it installed and in PATH?")
            self._set_busy(False, f"Error: '{proc.program()}' failed to start.")
        # Crashed is followed by finished(CrashExit) — handled there.

    def _on_proc_finished(self, exit_code: int, exit_status):
        if exit_status == QProcess.CrashExit:
            self._log(f"ERROR: step '{self._phase}' crashed (killed by a signal).")
            self._set_busy(False, f"Error: step '{self._phase}' crashed.")
            return
        if exit_code != 0:
            # 126/127 only mean "auth cancelled" when pkexec says so (a missing
            # program exits 127 as well).
            if self._phase == "install" and pkexec_auth_failed(exit_code, self._out_buf):
                self._log("Authorization cancelled/denied — installation aborted (build output kept).")
                self._set_busy(False, "Install cancelled (pkexec authorization).")
                return
            self._log(f"ERROR: step '{self._phase}' failed (exit code {exit_code}).")
            self._set_busy(False, f"Error: step '{self._phase}' failed.")
            return

        if self._phase == "configure":
            self._log("==> Building...")
            self._set_busy(True, "Building (cmake --build)...")
            self._phase = "build"
            nproc = str(os.cpu_count() or 4)
            self._run_process("cmake", ["--build", self._build_dir, "-j", nproc], use_pkexec=False)

        elif self._phase == "build":
            self._log("==> Requesting pkexec authorization for install (root required)...")
            self._set_busy(True, "Waiting for pkexec password...")
            self._phase = "install"
            # cmake --install writes the manifest with the correct FLM_LIB_PATH
            # baked in at configure time (FIX-52/61) — no post-install sed needed.
            self._run_process("cmake", ["--install", self._build_dir], use_pkexec=True)

        elif self._phase == "install":
            self._log("==> Installation complete.")
            self._log(f"    Layer installed to: {self._prefix_used}/{self._libdir_used}/libvk_flip_meter.so")
            self._log("    Manifest: share/vulkan/implicit_layer.d/VkLayer_cpu_flip_meter.json")
            self._log("    Verify: vulkaninfo --summary | grep -i flip_meter")
            self._set_busy(False, "Installation complete.")

        elif self._phase == "verify":
            self._set_busy(False, "Ready.")


# ============================================================================
# lutris-game-tune — Settings + Installer widget
# ============================================================================
#
# All operations that touch /etc/ or /usr/local/bin/ are routed through
# pkexec so the GUI itself never runs as root.
#
# Config key whitelist mirrors load_config() in lutris-game-tune.sh v4.1.
# The script parses KEY=VALUE strictly (no shell source), so we generate
# the same format: one KEY=VALUE per line, comment headers for readability.
#
# Detection heuristic: wrapper binary at /usr/local/bin/lutris-game-tune-wrapper
# must exist and be executable.  Config at /etc/lutris-game-tune.conf is
# optional (populated with defaults when absent).

# ── Config key metadata ───────────────────────────────────────────────────────
# Each entry: (key, widget_type, default, label, description, options/range)
#   widget_type: "check" | "combo" | "spin" | "line" | "line_space"
#   "line_space" = QLineEdit accepting space-separated tokens (proc names etc.)
#   options: list[str] for combo, (min,max) tuple for spin, None otherwise

LGTUNE_KEYS: List[tuple] = [
    # ── CPU Governor / EPP ────────────────────────────────────────────────────
    ("SET_CPU_GOVERNOR", "check", "1",
     "Set CPU governor on game start",
     "Write the chosen CPU_GOVERNOR (and EPP if amd-pstate is active) when a "
     "game launches via the PRE hook, then restore the previous governor on "
     "POST.  Disabling this leaves the governor wherever the system had it."),

    ("CPU_GOVERNOR", "combo", "performance",
     "CPU governor",
     "Frequency-scaling governor applied during gameplay.  'performance' pins "
     "all cores at max boost and also sets the Energy Performance Preference "
     "(EPP) to 'performance' when amd-pstate active mode is active — "
     "critical for Ryzen mobile CPUs where boost is EPP-gated.  'schedutil' "
     "is a good compromise for battery: it reacts to scheduler load signals "
     "rather than a fixed ceiling.",
     ["performance", "powersave", "schedutil", "ondemand", "conservative"]),

    ("SET_EPP_BOOST", "check", "1",
     "Enable amd-pstate per-core EPP boost during gameplay",
     "Writes 1 to /sys/module/amd_pstate/parameters/epp_boost on PRE and "
     "restores the previous value on POST.  This is the per-core epp_boost "
     "mechanism: an update-util hook samples each core's C0 residency every "
     "10 ms and, when a core is at least 50% busy, pins that core's EPP field "
     "to performance (0) until 300 ms pass without another busy sample.  It "
     "targets the case a plain governor change cannot fix — a render thread "
     "that blocks briefly on a futex or GPU fence every frame decays the "
     "hardware's performance signal, so each post-wakeup burst restarts at a "
     "low operating point and inflates the frame-time tail.  Unlike forcing "
     "EPP=performance globally, the boost is per-core and short-lived, so "
     "idle cores are not held at high power.  NOTE: this knob is NOT in "
     "mainline as of this writing — it only exists on kernels built with the "
     "epp_boost patch series.  On kernels without it the script skips the "
     "write silently, so leaving this enabled is harmless either way."),

    # ── AMD 3D V-Cache ────────────────────────────────────────────────────────
    ("SET_X3D_VCACHE_MODE", "check", "1",
     "Set AMD 3D V-Cache core preference on game start",
     "Writes X3D_VCACHE_MODE to the amd_x3d_vcache driver's amd_x3d_mode "
     "sysfs file on PRE and restores the previous value on POST.  AMD 3D "
     "V-Cache CPUs only (dual-CCD parts where one CCD has the extra L3 die "
     "stacked on it).  The driver's instance directory (an ACPI device id, "
     "e.g. AMDI0101:00) is discovered at runtime rather than hardcoded, "
     "since it isn't guaranteed to be the same on every board/BIOS.  "
     "Silently skipped if the driver isn't bound (non-X3D CPU or a kernel "
     "without the driver), so leaving this enabled is harmless either way."),

    ("X3D_VCACHE_MODE", "combo", "frequency",
     "AMD 3D V-Cache core preference",
     "Value written to the amd_x3d_mode sysfs file.  'frequency' asks the "
     "scheduler to prefer the higher-clocking CCD (usually better for "
     "CPU-bound / high-refresh-rate titles).  'cache' asks it to prefer "
     "cores on the CCD with the larger L3 (usually better for games that "
     "are sensitive to cache misses, e.g. large open-world titles).",
     ["frequency", "cache"]),

    # ── PCIe ASPM ─────────────────────────────────────────────────────────────
    ("SET_ASPM", "check", "1",
     "Set PCIe ASPM policy on game start",
     "Write ASPM_POLICY to /sys/module/pcie_aspm/parameters/policy during PRE "
     "and restore the previous policy on POST.  PCIe ASPM (Active State Power "
     "Management) controls link-power transitions between the CPU and "
     "PCIe devices.  In the 'default' or 'powersave' modes the link can "
     "briefly enter low-power states, adding ~100 µs wake-up latency each "
     "time.  Switching to 'performance' prevents those transitions during "
     "gameplay, which can reduce frame-time jitter on GPU-bound titles."),

    ("ASPM_POLICY", "combo", "performance",
     "PCIe ASPM policy",
     "Policy written to /sys/module/pcie_aspm/parameters/policy.  "
     "'performance' disables link power-state transitions entirely — "
     "the PCIe bus stays at full power.  'default' lets the firmware/driver "
     "decide (usually a mild power-save level).  'powersave' maximises "
     "link-power gating (useful for battery, bad for latency).",
     ["performance", "default", "powersave", "powersupersave"]),

    # ── Deep C-states ─────────────────────────────────────────────────────────
    ("DISABLE_DEEP_CSTATES", "check", "0",
     "Disable deep CPU C-states during gameplay",
     "Writes '1' to /sys/devices/system/cpu/cpu*/cpuidle/state{N}/disable for "
     "all states deeper than CSTATE_KEEP_MAX.  Deep C-states (C6/C7/C10 on "
     "Zen 4) have longer exit latencies (up to ~250 µs for CC10) that can "
     "appear as occasional frame-time spikes ('hitches') when the package "
     "wakes from a deep sleep mid-frame.  Disabling them keeps cores in "
     "C1/POLL at the cost of ~3–8 W of extra idle power draw — worth it "
     "while plugged in on a laptop, less so on battery."),

    ("CSTATE_KEEP_MAX", "spin", "1",
     "Deepest C-state to keep enabled",
     "Index of the deepest idle state that stays enabled when "
     "DISABLE_DEEP_CSTATES=1.  0 = POLL (active spin, lowest latency, "
     "highest power), 1 = C1/C1E (light halt, ~1 µs exit, recommended).  "
     "States at index > this value are disabled.  On Zen 4 (7845HX): "
     "state 0=POLL, 1=C1, 2=C6, 3=C7, 4=CC10 — so CSTATE_KEEP_MAX=1 "
     "disables C6/C7/CC10 while keeping the fast C1 path alive.",
     (0, 10)),

    # ── VM ────────────────────────────────────────────────────────────────────
    ("VM_SWAPPINESS", "spin", "10",
     "vm.swappiness during gameplay",
     "Value written to /proc/sys/vm/swappiness via sysctl during PRE, "
     "restored on POST.  Controls how aggressively the kernel reclaims "
     "anonymous pages by swapping them out.  The Linux default is 60; "
     "a lower value (10–20) makes the kernel prefer reclaiming the page "
     "cache instead of swapping game data out, reducing random-access "
     "latency spikes caused by swap I/O during gameplay.  Setting it to 0 "
     "disables swap entirely while in game — only safe if you have enough "
     "RAM for the game plus the OS.",
     (0, 100)),

    # ── Transparent HugePages ─────────────────────────────────────────────────
    ("THP_ENABLED", "combo", "madvise",
     "THP enabled",
     "Value written to /sys/kernel/mm/transparent_hugepage/enabled.  "
     "'always' maps all anonymous allocations to 2 MB huge pages — lowest "
     "TLB pressure but can fragment memory and cause occasional allocation "
     "stalls.  'madvise' (recommended) only uses huge pages for regions "
     "that explicitly call madvise(MADV_HUGEPAGE) — Wine, DXVK, and VKD3D "
     "all do this, so game data gets huge pages while OS metadata doesn't.  "
     "'never' disables THP completely.",
     ["always", "madvise", "never"]),

    ("THP_SHMEM_ENABLED", "combo", "madvise",
     "THP shmem_enabled",
     "Value written to /sys/kernel/mm/transparent_hugepage/shmem_enabled.  "
     "Controls THP for shared-memory (shmem/tmpfs) mappings — used by "
     "Vulkan drivers for command/descriptor buffers and by Wine's shared "
     "memory segments.  'madvise' is the safe default: huge pages are used "
     "only when the allocation explicitly requests them.",
     ["always", "within_size", "advise", "never", "deny", "force"]),

    ("THP_DEFRAG", "combo", "madvise",
     "THP defrag",
     "Value written to /sys/kernel/mm/transparent_hugepage/defrag.  "
     "Controls how hard the kernel tries to compact memory to satisfy "
     "huge-page requests.  'always' defragments synchronously — can stall "
     "allocations for tens of milliseconds.  'defer' defrags asynchronously "
     "via a background khugepaged thread — no stalls but slower promotion.  "
     "'madvise' only defrags when madvise(MADV_HUGEPAGE) was called.  "
     "'never' skips defrag entirely.",
     ["always", "defer", "defer+madvise", "madvise", "never"]),

    # ── CCD/CCX core isolation ────────────────────────────────────────────────
    ("CCD_ISOLATION_ENABLED", "check", "1",
     "Enable CCD/CCX core isolation",
     "When enabled, the script detects the CPU's CCD topology at PRE time "
     "and pins the game (and its child processes) to CCD 0 while confining "
     "background system processes to CCD 1.  On a Ryzen 9 7845HX (1×CCD, "
     "single-CCX) this is automatically skipped — the script silently does "
     "nothing.  On multi-CCD Ryzen desktop parts (e.g. 5900X, 7950X) it "
     "removes cross-CCD memory-latency variance from game threads, which "
     "can reduce frame-time jitter.  Disable if you see scheduling issues "
     "or if you only have one CCD."),

    ("CCD_LAUNCHER", "line", "lutris",
     "Launcher process name",
     "Process name searched for with 'pgrep -x' to identify the launcher "
     "that will be the root of the CCD isolation group.  'lutris' for Lutris, "
     "'steam' for Steam, 'heroic' for Heroic, 'bottles' for Bottles.  "
     "The script pins this process and all of its descendants to CCD 0 "
     "(theGood cgroup)."),

    ("CCD_EXTRA_GOOD_PROCS", "line_space", "",
     "Extra processes for the performance CCD",
     "Space-separated list of process names (pgrep -x) to explicitly add to "
     "theGood (CCD 0 / performance) cgroup in addition to the launcher's "
     "process tree.  Useful for helper processes that are launched by the "
     "game but are not direct children of the launcher — e.g. gamescope, "
     "picom, wine64-preloader.  Leave empty if CCD_ISOLATION_ENABLED=0."),

    ("CCD_MONITOR_SECONDS", "spin", "30",
     "Child-process scan duration (seconds)",
     "How long (in seconds) after the launcher is moved to theGood the "
     "script keeps scanning for newly spawned child processes (shader "
     "compilation threads, DXVK/VKD3D background workers) and adds them "
     "to the performance CCD.  0 = one-shot scan only.  30–60 s covers "
     "most shader-compilation windows at game startup.",
     (0, 300)),

    ("CCD_PROTECTED_PROCS", "line_space",
     "elogind elogind-daemon systemd-logind dbus-daemon dbus-broker "
     "polkitd openrc-user login gdm gdm3 sddm lightdm",
     "Protected process names (never moved)",
     "Space-separated list of process names that must never be swept into "
     "the system CCD even if found sitting directly in the cgroup-v2 root.  "
     "Covers OpenRC (elogind) and systemd (systemd-logind) session-manager "
     "names so the same list works on both init systems.  The default list "
     "is safe to leave unchanged — adding an unknown daemon here has no "
     "cost, missing one risks breaking D-Bus/PolicyKit session tracking."),

    ("CCD_GOOD_PARTITION_TYPE", "combo", "root",
     "theGood cgroup partition type",
     "cpuset partition type for the game's CCD cgroup.  'root' is stable "
     "and allows the kernel's housekeeping tasks to migrate onto those CPUs "
     "if needed.  'isolated' gives stronger isolation (no housekeeping) but "
     "can cause stability issues on some kernels / firmware combinations — "
     "test carefully before deploying.",
     ["root", "isolated"]),

    ("CCD_UGLY_PARTITION_TYPE", "combo", "member",
     "theUgly cgroup partition type",
     "cpuset partition type for the system/background CCD cgroup.  'member' "
     "is the recommended default and matches the script's own fallback: it "
     "leaves this group as a plain (non-exclusive) cgroup.  If BOTH theGood "
     "and theUgly claim their CPUs as exclusive partition roots ('root' or "
     "'isolated'), the top-level cgroup can be left with no CPUs of its "
     "own, and the kernel silently marks the partition invalid — i.e. no "
     "isolation at all, with no error anywhere.  Only pick 'root'/'isolated' "
     "here if you specifically need theUgly to also be an exclusive "
     "partition and understand this trade-off.",
     ["member", "root", "isolated"]),

    # ── PCI latency ───────────────────────────────────────────────────────────
    ("SET_PCI_LATENCY", "check", "1",
     "Set PCI latency timer",
     "Write the optimal PCI latency timer value to all PCI devices on PRE "
     "and restore on POST.  The latency timer limits how long a PCI master "
     "can hold the bus before yielding; older PCI devices (especially legacy "
     "audio or capture cards) sometimes have conservative defaults that add "
     "bus arbitration latency.  Harmless on PCIe-only systems (PCIe doesn't "
     "use the latency timer), but the script still writes it for completeness."),

    # ── Intel CPUs (GenuineIntel only; AMD-only knobs above are skipped there) ──
    ("SET_INTEL_TURBO", "check", "1",
     "Force Intel turbo on during gameplay",
     "Writes 0 to intel_pstate/no_turbo on PRE and restores it on POST.  Intel CPUs only; skipped when intel_pstate is absent."),

    ("SET_INTEL_HWP_BOOST", "check", "1",
     "Enable intel_pstate HWP dynamic boost",
     "Writes 1 to intel_pstate/hwp_dynamic_boost (active mode with HWP only)."),

    ("INTEL_MAX_PERF_PCT", "spin", "100",
     "intel_pstate max_perf_pct",
     "Upper performance limit in percent while gaming.  0 = leave unchanged.",
     (0, 100)),

    ("INTEL_MIN_PERF_PCT", "spin", "0",
     "intel_pstate min_perf_pct",
     "Lower performance limit in percent while gaming.  0 = leave unchanged.",
     (0, 100)),

    ("SET_INTEL_EPB", "check", "1",
     "Set energy_perf_bias to 0 (max performance)",
     "Writes 0 to every cpuN/power/energy_perf_bias on PRE; restored on POST."),

    ("SET_INTEL_ITMT", "check", "1",
     "Enable ITMT favored-core scheduling",
     "Writes 1 to /proc/sys/kernel/sched_itmt_enabled (Turbo Boost Max 3.0 / hybrid CPUs)."),

    ("HYBRID_CORE_ISOLATION", "check", "0",
     "Hybrid P/E-core isolation (opt-in)",
     "Intel hybrid CPUs (Alder Lake and newer): game on the P-cores, rest of the system on the E-cores, via the same cpuset machinery as CCD isolation.  Skipped when the P-cores expose fewer than HYBRID_MIN_P_THREADS logical CPUs."),

    ("HYBRID_MIN_P_THREADS", "spin", "8",
     "Minimum P-core threads for hybrid isolation",
     "Hybrid isolation is skipped when the P-cores expose fewer logical CPUs than this.",
     (1, 512)),

    # ── Proton / Wine friendliness ───────────────────────────────────────────
    ("VM_MAX_MAP_COUNT", "spin", "2147483642",
     "vm.max_map_count (raise-only)",
     "Raised to this value while gaming, never lowered.  Some Proton titles crash on the stock 65530.  0 = leave alone.",
     (0, 2147483647)),

    ("SET_VM_DIRTY", "check", "1",
     "Limit dirty-page window during gameplay",
     "Writes smaller dirty_bytes / dirty_background_bytes limits (smooths shader-cache and save-file write bursts).  All four ratio/bytes originals are saved and restored."),

    ("VM_DIRTY_BACKGROUND_BYTES", "spin", "67108864",
     "vm.dirty_background_bytes",
     "Background writeback threshold in bytes (must be smaller than dirty_bytes).",
     (1048576, 2147483647)),

    ("VM_DIRTY_BYTES", "spin", "268435456",
     "vm.dirty_bytes",
     "Hard dirty-page limit in bytes.  Skipped when dirty_bytes is already at or below this.",
     (1048576, 2147483647)),

    ("SET_NUMA_BALANCING", "check", "1",
     "Disable NUMA auto-balancing during gameplay",
     "Writes 0 to kernel.numa_balancing (background page scanning/migration adds page-fault noise)."),

    # ── IRQ affinity ─────────────────────────────────────────────────────────
    ("SET_IRQ_AFFINITY", "check", "0",
     "Move device IRQs to the system CPUs (opt-in)",
     "Moves the IRQs of the chosen PCI classes onto the system CPUs (IRQ_AFFINITY_CPUS, or the second CCD group).  Managed IRQs such as NVMe queues cannot be moved and are skipped.  Stop irqbalance, or it undoes this."),

    ("IRQ_AFFINITY_CLASSES", "line_space", "gpu nvme net usb",
     "IRQ classes to move",
     "Space-separated, any of: gpu nvme net usb audio.",
     None),

    ("IRQ_AFFINITY_CPUS", "line", "",
     "IRQ target CPUs (cpulist)",
     "Explicit target cpulist such as 8-15,24-31.  Empty = use the second CCD/CCX group (needs a multi-CCD CPU).",
     None),

    # ── GPU / network / storage ──────────────────────────────────────────────
    ("SET_NVIDIA_PERSISTENCE", "check", "0",
     "Enable NVIDIA persistence mode (opt-in)",
     "Turns persistence mode on for GPUs where it is currently off, and back off on POST.  Small effect while a game already holds the GPU."),

    ("SET_AMDGPU_PERF_LEVEL", "check", "0",
     "Force amdgpu performance level (opt-in)",
     "Writes AMDGPU_PERF_LEVEL to power_dpm_force_performance_level on every amdgpu card.  Raises power draw."),

    ("AMDGPU_PERF_LEVEL", "combo", "high",
     "amdgpu performance level",
     "Value for power_dpm_force_performance_level.",
     ["high", "auto", "low", "manual", "profile_standard", "profile_min_sclk", "profile_min_mclk", "profile_peak"]),

    ("SET_WIFI_POWERSAVE", "check", "1",
     "Turn Wi-Fi power save off during gameplay",
     "Runs 'iw dev <if> set power_save off' on interfaces where it is on; restored on POST.  Needs the iw tool."),

    ("SET_NVME_SCHED", "check", "1",
     "Set NVMe I/O scheduler during gameplay",
     "Writes NVME_IO_SCHEDULER to every NVMe queue/scheduler that offers it."),

    ("NVME_IO_SCHEDULER", "combo", "none",
     "NVMe I/O scheduler",
     "none is the usual best choice for NVMe; the others are for experiments.",
     ["none", "mq-deadline", "kyber", "bfq"]),

    # ── Logging ───────────────────────────────────────────────────────────────
    ("LOG_LEVEL", "combo", "INFO",
     "Log level",
     "Verbosity of /var/log/lutris-game-tune.log.  "
     "'DEBUG' logs every sysfs read/write and cgroup operation — very noisy, "
     "use only when diagnosing a specific issue.  "
     "'INFO' logs PRE/POST lifecycle events and all tuning steps applied.  "
     "'WARN' logs only unexpected conditions (skipped steps, deprecated keys).  "
     "'ERROR' logs only fatal failures.",
     ["DEBUG", "INFO", "WARN", "ERROR"]),
]


class LgtuneWidget(QWidget):
    """
    Right-panel widget for the lutris-game-tune sub-tab inside Extra Tools.
    Source is bundled at _SCRIPT_DIR/lutris-game-tune-main/ — no zip needed.

    Layout (scrollable):
      ┌─ How lutris-game-tune works ─────────────────────────────────────────┐
      │  Usage explanation: PRE / POST / RUN / STATUS, Lutris wiring        │
      └──────────────────────────────────────────────────────────────────────┘
      ┌─ Installation ───────────────────────────────────────────────────────┐
      │  [detected / not detected]  Install / Uninstall (pkexec) buttons    │
      └──────────────────────────────────────────────────────────────────────┘
      ┌─ System-wide Settings  (/etc/lutris-game-tune.conf) ─────────────────┐
      │  [per-key rows: label + description + control]                       │
      │  [ Save to /etc/ (pkexec) ]  [ Run STATUS ]  [ View Log ]           │
      └──────────────────────────────────────────────────────────────────────┘
      console (QPlainTextEdit — install/status/log output)
    """

    # Bundled source path (repo layout: DRSTool/lutris-game-tune-main/)
    _BUNDLED_SRC = find_bundled_dir("lutris-game-tune-main", "install.sh")
    _out_buf: str = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: Optional[QProcess] = None

        # Master layout (no margins — scroll area provides padding)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(SCROLLABLE_CONTROL_QSS)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(14)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        header = QLabel("lutris-game-tune — System-wide Game Tuner")
        header.setStyleSheet("color:#f2f2f2; font-size:18px; font-weight:700;")
        layout.addWidget(header)

        # ── How it works / usage ─────────────────────────────────────────────
        usage_group = QGroupBox("How lutris-game-tune works")
        usage_group.setStyleSheet(_TOOL_GROUP_SS)
        usage_layout = QVBoxLayout(usage_group)
        usage_layout.setSpacing(8)

        usage_text = QLabel(
            "<b>lutris-game-tune</b> is a Bash script + setuid-root C wrapper that "
            "applies system-level performance tweaks at game launch and reverts them "
            "cleanly when the game exits.  The wrapper (<code>lutris-game-tune-wrapper</code>) "
            "is installed as setuid root so Lutris (running as your user) can trigger it "
            "without a password prompt each time.<br><br>"

            "<b>Modes / commands:</b><br>"
            "&nbsp;&nbsp;<code>lutris-game-tune-wrapper PRE</code> — run before the game starts: "
            "saves current system state, then applies CPU governor, PCIe ASPM, C-state, "
            "THP, swappiness, PCI latency, and CCD/CCX isolation settings from "
            "<code>/etc/lutris-game-tune.conf</code>.<br>"
            "&nbsp;&nbsp;<code>lutris-game-tune-wrapper POST</code> — run after the game exits: "
            "restores every setting that PRE changed back to its original value.  Safe to "
            "call even if PRE didn't run (idempotent).<br>"
            "&nbsp;&nbsp;<code>lutris-game-tune-wrapper STATUS</code> — prints the current "
            "active state (which settings are applied, how many games are running) to stdout.  "
            "Useful to confirm the tuner is active during a game session.<br>"
            "&nbsp;&nbsp;<code>lutris-game-tune-wrapper RUN &lt;nice&gt; [--] &lt;command…&gt;</code> — "
            "starts the game command with the given nice value (e.g. <code>-5</code>) using "
            "root privilege for the <code>nice()</code> syscall, then immediately drops to your "
            "user — the game itself never runs as root.  Goes in Lutris's "
            "<i>Command prefix</i> field.<br><br>"

            "<b>Wiring in Lutris:</b><br>"
            "Go to <i>Configure → System options</i> (per-game) or "
            "<i>Preferences → System options</i> (global) and set:<br>"
            "&nbsp;&nbsp;<b>Pre-game script:</b> <code>/usr/local/bin/lutris-game-tune-wrapper PRE</code><br>"
            "&nbsp;&nbsp;<b>Post-game script:</b> <code>/usr/local/bin/lutris-game-tune-wrapper POST</code><br>"
            "Optionally, to start the game at a higher CPU priority:<br>"
            "&nbsp;&nbsp;<b>Command prefix:</b> <code>/usr/local/bin/lutris-game-tune-wrapper RUN -5</code><br><br>"

            "<b>Security model:</b>  The wrapper binary is setuid root; it validates "
            "arguments strictly, drops root immediately after the privileged syscall "
            "(for RUN mode), and delegates all logic to the Bash script at "
            "<code>/usr/local/lib/lutris-game-tune/lutris-game-tune.sh</code> — "
            "which in turn reads <code>/etc/lutris-game-tune.conf</code> in a "
            "safe, no-<code>source</code> parser (whitelist-only KEY=VALUE).  "
            "The config file must be root-owned and not group/other writable "
            "(mode 644 or 640) — the script refuses to load it otherwise.<br><br>"

            "<b>Log file:</b> <code>/var/log/lutris-game-tune.log</code> — rotated "
            "automatically at 1 MB.  Use the <i>View Log</i> button below to tail it."
        )
        usage_text.setWordWrap(True)
        usage_text.setTextFormat(Qt.RichText)
        usage_text.setStyleSheet(
            "color:#a7afbc; font-size:12px; line-height:155%; "
            "background:transparent;"
        )
        usage_layout.addWidget(usage_text)
        layout.addWidget(usage_group)

        # ── Installation status + installer ──────────────────────────────────
        install_group = QGroupBox("Installation")
        install_group.setStyleSheet(_TOOL_GROUP_SS)
        install_layout = QVBoxLayout(install_group)
        install_layout.setSpacing(8)

        self._status_banner = QLabel()
        self._status_banner.setWordWrap(True)
        self._status_banner.setStyleSheet("font-size:12px; font-weight:700; padding:4px;")
        install_layout.addWidget(self._status_banner)

        # Bundled source path banner (read-only)
        src_row = QHBoxLayout()
        src_row.setSpacing(6)
        src_lbl = QLabel("📁 Source:")
        src_lbl.setStyleSheet("color:#8ea0ba; font-size:12px; font-weight:600;")
        self._lgt_src_lbl = QLabel(str(self._BUNDLED_SRC))
        self._lgt_src_lbl.setStyleSheet(
            "color:#76b900; font-size:12px; font-family:monospace;"
        )
        lgt_src_ok = (self._BUNDLED_SRC / "install.sh").is_file()
        self._lgt_src_status = QLabel("✔ found" if lgt_src_ok else "✘ not found")
        self._lgt_src_status.setStyleSheet(
            f"color:{'#76b900' if lgt_src_ok else '#c04040'}; "
            "font-size:12px; font-weight:700;"
        )
        src_row.addWidget(src_lbl)
        src_row.addWidget(self._lgt_src_lbl, 1)
        src_row.addWidget(self._lgt_src_status)
        install_layout.addLayout(src_row)

        # Action buttons row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._install_btn = _tool_btn(
            "Install (pkexec)", "#76b900", "#76b900", "#0d0f12", bold=True
        )
        self._install_btn.clicked.connect(self._on_install)
        self._uninstall_btn = _tool_btn("Uninstall (pkexec)", "#3a1010", "#7a2020", "#e07070")
        self._uninstall_btn.clicked.connect(self._on_uninstall)
        btn_row.addWidget(self._install_btn)
        btn_row.addWidget(self._uninstall_btn)
        btn_row.addStretch()
        install_layout.addLayout(btn_row)

        install_note = QLabel(
            "Install runs <code>lutris-game-tune-main/install.sh</code> (bundled in "
            "the DRSTool repository) via pkexec.  "
            "Uninstall runs <code>uninstall.sh</code> from the installed lib dir via pkexec.  "
            "The config file at <code>/etc/lutris-game-tune.conf</code> is "
            "<b>not removed</b> on uninstall — your settings are preserved."
        )
        install_note.setWordWrap(True)
        install_note.setTextFormat(Qt.RichText)
        install_note.setStyleSheet("color:#8a92a5; font-size:13px; font-style:italic;")
        install_layout.addWidget(install_note)

        layout.addWidget(install_group)

        # ── Settings group ────────────────────────────────────────────────────
        self._settings_group = QGroupBox(
            "System-wide Settings  —  /etc/lutris-game-tune.conf  (root-owned)"
        )
        self._settings_group.setStyleSheet(_TOOL_GROUP_SS)
        settings_layout = QVBoxLayout(self._settings_group)
        settings_layout.setSpacing(10)

        # Build per-key widgets
        self._key_widgets: Dict[str, QWidget] = {}  # key → control widget
        grid = QGridLayout()
        grid.setSpacing(6)
        grid.setColumnStretch(1, 1)

        for row_idx, entry in enumerate(LGTUNE_KEYS):
            key, wtype, default, label, desc, *rest = entry
            opts_or_range = rest[0] if rest else None

            # Label (key name, bold)
            key_lbl = QLabel(f"<b>{key}</b>")
            key_lbl.setStyleSheet("color:#c8cdd8; font-size:13px;")
            key_lbl.setToolTip(desc)
            grid.addWidget(key_lbl, row_idx * 2, 0, Qt.AlignTop)

            # Control widget
            if wtype == "check":
                ctrl = QCheckBox(label)
                ctrl.setChecked(default == "1")
                ctrl.setStyleSheet(
                    "QCheckBox{ color:#d8d8d8; font-size:13px; spacing:6px; }"
                    "QCheckBox::indicator{ width:14px; height:14px; "
                    "border:1px solid #323c4b; border-radius:3px; background:#1a1f28; }"
                    "QCheckBox::indicator:checked{ background:#76b900; border:1px solid #76b900; }"
                )

            elif wtype == "combo":
                ctrl = QComboBox()
                ctrl.addItems(opts_or_range or [])
                idx_default = opts_or_range.index(default) if opts_or_range and default in opts_or_range else 0
                ctrl.setCurrentIndex(idx_default)
                ctrl.setStyleSheet("""
QComboBox{ background:#1a1f28; border:1px solid #323c4b; border-radius:4px;
           color:#d8d8d8; font-size:13px; padding:3px 8px; min-height:26px; }
QComboBox:hover{ border:1px solid #76b900; }
QComboBox QAbstractItemView{ background:#141720; color:#d8d8d8; selection-background-color:#1e2535; }
""")
                ctrl.installEventFilter(_NO_SCROLL_FILTER)

            elif wtype == "spin":
                ctrl = QSpinBox()
                lo, hi = opts_or_range if opts_or_range else (0, 9999)
                ctrl.setRange(lo, hi)
                ctrl.setValue(int(default))
                ctrl.setStyleSheet("""
QSpinBox{ background:#1a1f28; border:1px solid #323c4b; border-radius:4px;
          color:#d8d8d8; font-size:13px; padding:3px 6px; }
QSpinBox:hover{ border:1px solid #76b900; }
QSpinBox::up-button, QSpinBox::down-button{ width:16px; }
""")
                ctrl.installEventFilter(_NO_SCROLL_FILTER)

            elif wtype in ("line", "line_space"):
                ctrl = QLineEdit(default)
                ctrl.setStyleSheet(FLM_FIELD_SS)
                if wtype == "line_space":
                    ctrl.setPlaceholderText("space-separated names")

            else:
                ctrl = QLineEdit(default)
                ctrl.setStyleSheet(FLM_FIELD_SS)

            ctrl.setToolTip(desc)
            grid.addWidget(ctrl, row_idx * 2, 1)
            self._key_widgets[key] = ctrl

            # Description (smaller, muted, spans full row)
            desc_lbl = QLabel(desc)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet(
                "color:#b4bcc9; font-size:13px; line-height:150%; padding:1px 0 6px 0;"
            )
            grid.addWidget(desc_lbl, row_idx * 2 + 1, 0, 1, 2)

        # ── Runtime availability hint for SET_EPP_BOOST ───────────────────────
        # epp_boost is not upstream yet, so annotate the checkbox with whether
        # the knob actually exists on the running kernel. Purely informational:
        # the option stays writable either way, since the user may be
        # configuring for a patched kernel they haven't booted yet.
        _epp_ctrl = self._key_widgets.get("SET_EPP_BOOST")
        if isinstance(_epp_ctrl, QCheckBox):
            _epp_present = Path("/sys/module/amd_pstate/parameters/epp_boost").exists()
            if _epp_present:
                _epp_ctrl.setText(_epp_ctrl.text() + "   [kernel: supported]")
            else:
                _epp_ctrl.setText(_epp_ctrl.text() + "   [kernel: not present]")
                _epp_ctrl.setToolTip(
                    _epp_ctrl.toolTip()
                    + "\n\nThis kernel does not expose "
                      "/sys/module/amd_pstate/parameters/epp_boost — the setting "
                      "will be saved but skipped at PRE time until you boot a "
                      "kernel with the epp_boost patch series applied."
                )

        # ── Runtime availability hint for SET_X3D_VCACHE_MODE ─────────────────
        # amd_x3d_mode only exists when the amd_x3d_vcache driver is bound (AMD
        # 3D V-Cache CPU + supporting kernel). The instance directory name is an
        # ACPI device id (e.g. AMDI0101:00) that isn't guaranteed to be the same
        # on every board/BIOS, so it is globbed for rather than hardcoded.
        _x3d_ctrl = self._key_widgets.get("SET_X3D_VCACHE_MODE")
        if isinstance(_x3d_ctrl, QCheckBox):
            _x3d_matches = list(Path("/sys/bus/platform/drivers/amd_x3d_vcache").glob("*/amd_x3d_mode")) \
                if Path("/sys/bus/platform/drivers/amd_x3d_vcache").is_dir() else []
            if _x3d_matches:
                _x3d_ctrl.setText(_x3d_ctrl.text() + "   [kernel: supported]")
            else:
                _x3d_ctrl.setText(_x3d_ctrl.text() + "   [kernel: not present]")
                _x3d_ctrl.setToolTip(
                    _x3d_ctrl.toolTip()
                    + "\n\nNo amd_x3d_vcache driver instance was found under "
                      "/sys/bus/platform/drivers/amd_x3d_vcache — either this "
                      "isn't an AMD 3D V-Cache CPU, or the running kernel doesn't "
                      "have the driver.  The setting will be saved but skipped at "
                      "PRE time until the driver is bound."
                )

        settings_layout.addLayout(grid)

        # Save / status / log button row
        action_row = QHBoxLayout()
        action_row.setSpacing(6)
        self._save_btn = _tool_btn(
            "💾  Save to /etc/ (pkexec)", "#76b900", "#76b900", "#0d0f12", bold=True
        )
        self._save_btn.clicked.connect(self._on_save)
        self._status_btn = _tool_btn("▶  Run STATUS")
        self._status_btn.clicked.connect(self._on_run_status)
        self._log_btn = _tool_btn("📄  View Log")
        self._log_btn.clicked.connect(self._on_view_log)
        action_row.addWidget(self._save_btn)
        action_row.addWidget(self._status_btn)
        action_row.addWidget(self._log_btn)
        action_row.addStretch()
        settings_layout.addLayout(action_row)

        save_note = QLabel(
            "Writes <code>/etc/lutris-game-tune.conf</code> as root via pkexec.  "
            "Changes take effect on the <b>next PRE run</b> — no restart needed."
        )
        save_note.setWordWrap(True)
        save_note.setTextFormat(Qt.RichText)
        save_note.setStyleSheet("color:#8a92a5; font-size:13px; font-style:italic;")
        settings_layout.addWidget(save_note)

        layout.addWidget(self._settings_group)

        # ── Console ───────────────────────────────────────────────────────────
        console_hdr = QHBoxLayout()
        console_lbl = QLabel("Output")
        console_lbl.setStyleSheet("color:#8ea0ba; font-size:12px; font-weight:700;")
        clear_btn = _tool_btn("Clear")
        clear_btn.clicked.connect(lambda: self._console.clear())
        console_hdr.addWidget(console_lbl)
        console_hdr.addStretch()
        console_hdr.addWidget(clear_btn)
        layout.addLayout(console_hdr)

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setFixedHeight(160)
        self._console.setStyleSheet("""
QPlainTextEdit{
    background:#0a0c0f; border:1px solid #1e2535; border-radius:6px;
    color:#9be238; font-family:monospace; font-size:13px; padding:6px;
}
""")
        layout.addWidget(self._console)
        layout.addStretch()

        # Populate from /etc/ on first show
        self._refresh_install_state()
        self._load_conf()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _log(self, text: str):
        self._console.appendPlainText(text)
        self._console.verticalScrollBar().setValue(
            self._console.verticalScrollBar().maximum()
        )

    def _is_installed(self) -> bool:
        return LGTUNE_WRAPPER.exists() and os.access(str(LGTUNE_WRAPPER), os.X_OK)

    def _refresh_install_state(self):
        src_ok = (self._BUNDLED_SRC / "install.sh").is_file()
        self._lgt_src_status.setText("✔ found" if src_ok else "✘ not found")
        self._lgt_src_status.setStyleSheet(
            f"color:{'#76b900' if src_ok else '#c04040'}; "
            "font-size:12px; font-weight:700;"
        )
        if self._is_installed():
            self._status_banner.setText(
                f"✔  Installed  —  {LGTUNE_WRAPPER}"
            )
            self._status_banner.setStyleSheet(
                "color:#76b900; font-size:12px; font-weight:700; padding:4px;"
            )
            self._install_btn.hide()
            self._uninstall_btn.show()
            self._settings_group.setEnabled(True)
        else:
            self._status_banner.setText(
                "✘  Not installed  —  wrapper not found at "
                f"{LGTUNE_WRAPPER}.  "
                "Click Install to build and install from the bundled source."
            )
            self._status_banner.setStyleSheet(
                "color:#c04040; font-size:12px; font-weight:700; padding:4px;"
            )
            self._install_btn.show()
            self._uninstall_btn.hide()
            self._settings_group.setEnabled(False)

    def _load_conf(self):
        """Parse /etc/lutris-game-tune.conf and populate the UI widgets."""
        if not LGTUNE_CONF.is_file():
            return
        try:
            text = LGTUNE_CONF.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return

        values: Dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip().strip('"').strip("'")

        for key, ctrl in self._key_widgets.items():
            val = values.get(key)
            if val is None:
                continue
            if isinstance(ctrl, QCheckBox):
                ctrl.setChecked(val in ("1", "true", "yes"))
            elif isinstance(ctrl, QComboBox):
                idx = ctrl.findText(val, Qt.MatchFixedString)
                if idx >= 0:
                    ctrl.setCurrentIndex(idx)
            elif isinstance(ctrl, QSpinBox):
                try:
                    ctrl.setValue(int(val))
                except ValueError:
                    pass
            elif isinstance(ctrl, QLineEdit):
                ctrl.setText(val)

    def _build_conf_text(self) -> str:
        """Generate the full /etc/lutris-game-tune.conf content from UI state."""
        lines = [
            "# /etc/lutris-game-tune.conf — written by DRSTool",
            f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "#",
            "# This file is read by lutris-game-tune.sh (whitelist-only KEY=VALUE parser).",
            "# Must be owned by root and not group/other writable (mode 644 or 640).",
            "# Changes take effect on the next PRE run.",
            "",
            "# --- CPU Governor / EPP -------------------------------------------",
        ]
        def _val(key: str) -> str:
            ctrl = self._key_widgets.get(key)
            if ctrl is None:
                return ""
            if isinstance(ctrl, QCheckBox):
                return "1" if ctrl.isChecked() else "0"
            elif isinstance(ctrl, QComboBox):
                return ctrl.currentText()
            elif isinstance(ctrl, QSpinBox):
                return str(ctrl.value())
            elif isinstance(ctrl, QLineEdit):
                return ctrl.text().strip()
            return ""

        lines += [
            f"SET_CPU_GOVERNOR={_val('SET_CPU_GOVERNOR')}",
            f"CPU_GOVERNOR={_val('CPU_GOVERNOR')}",
            "",
            "# amd-pstate per-core EPP boost (kernel module parameter; only present",
            "# on kernels built with the epp_boost patch series — skipped silently",
            "# otherwise). 1=on, 0=off.",
            f"SET_EPP_BOOST={_val('SET_EPP_BOOST')}",
            "",
            "# AMD 3D V-Cache core preference (amd_x3d_vcache driver; AMD 3D",
            "# V-Cache CPUs only). frequency = prefer higher-clocking CCD,",
            "# cache = prefer cores on the CCD with the larger L3. Skipped",
            "# silently if the driver isn't bound.",
            f"SET_X3D_VCACHE_MODE={_val('SET_X3D_VCACHE_MODE')}",
            f"X3D_VCACHE_MODE={_val('X3D_VCACHE_MODE')}",
            "",
            "# --- PCIe ASPM ---------------------------------------------------",
            f"SET_ASPM={_val('SET_ASPM')}",
            f"ASPM_POLICY={_val('ASPM_POLICY')}",
            "",
            "# --- Deep C-state disabling (optional) ---------------------------",
            f"DISABLE_DEEP_CSTATES={_val('DISABLE_DEEP_CSTATES')}",
            f"CSTATE_KEEP_MAX={_val('CSTATE_KEEP_MAX')}",
            "",
            "# --- VM ----------------------------------------------------------",
            f"VM_SWAPPINESS={_val('VM_SWAPPINESS')}",
            "",
            "# --- Transparent HugePages ---------------------------------------",
            f"THP_ENABLED={_val('THP_ENABLED')}",
            f"THP_SHMEM_ENABLED={_val('THP_SHMEM_ENABLED')}",
            f"THP_DEFRAG={_val('THP_DEFRAG')}",
            "",
            "# --- CCD/CCX core isolation ---------------------------------------",
            f"CCD_ISOLATION_ENABLED={_val('CCD_ISOLATION_ENABLED')}",
            f"CCD_LAUNCHER={_val('CCD_LAUNCHER')}",
            f'CCD_EXTRA_GOOD_PROCS="{_val("CCD_EXTRA_GOOD_PROCS")}"',
            f"CCD_MONITOR_SECONDS={_val('CCD_MONITOR_SECONDS')}",
            f"CCD_GOOD_PARTITION_TYPE={_val('CCD_GOOD_PARTITION_TYPE')}",
            f"CCD_UGLY_PARTITION_TYPE={_val('CCD_UGLY_PARTITION_TYPE')}",
            f'CCD_PROTECTED_PROCS={_val("CCD_PROTECTED_PROCS")}',
            "",
            "# --- PCI latency -------------------------------------------------",
            f"SET_PCI_LATENCY={_val('SET_PCI_LATENCY')}",
            "",
            "# --- Intel CPUs (GenuineIntel only) --------------------------------",
            f"SET_INTEL_TURBO={_val('SET_INTEL_TURBO')}",
            f"SET_INTEL_HWP_BOOST={_val('SET_INTEL_HWP_BOOST')}",
            f"INTEL_MAX_PERF_PCT={_val('INTEL_MAX_PERF_PCT')}",
            f"INTEL_MIN_PERF_PCT={_val('INTEL_MIN_PERF_PCT')}",
            f"SET_INTEL_EPB={_val('SET_INTEL_EPB')}",
            f"SET_INTEL_ITMT={_val('SET_INTEL_ITMT')}",
            f"HYBRID_CORE_ISOLATION={_val('HYBRID_CORE_ISOLATION')}",
            f"HYBRID_MIN_P_THREADS={_val('HYBRID_MIN_P_THREADS')}",
            "",
            "# --- Proton / Wine friendliness ------------------------------------",
            f"VM_MAX_MAP_COUNT={_val('VM_MAX_MAP_COUNT')}",
            f"SET_VM_DIRTY={_val('SET_VM_DIRTY')}",
            f"VM_DIRTY_BACKGROUND_BYTES={_val('VM_DIRTY_BACKGROUND_BYTES')}",
            f"VM_DIRTY_BYTES={_val('VM_DIRTY_BYTES')}",
            f"SET_NUMA_BALANCING={_val('SET_NUMA_BALANCING')}",
            "",
            "# --- IRQ affinity (opt-in) -----------------------------------------",
            f"SET_IRQ_AFFINITY={_val('SET_IRQ_AFFINITY')}",
            f'IRQ_AFFINITY_CLASSES="{_val("IRQ_AFFINITY_CLASSES")}"',
            f"IRQ_AFFINITY_CPUS={_val('IRQ_AFFINITY_CPUS')}",
            "",
            "# --- GPU / network / storage ---------------------------------------",
            f"SET_NVIDIA_PERSISTENCE={_val('SET_NVIDIA_PERSISTENCE')}",
            f"SET_AMDGPU_PERF_LEVEL={_val('SET_AMDGPU_PERF_LEVEL')}",
            f"AMDGPU_PERF_LEVEL={_val('AMDGPU_PERF_LEVEL')}",
            f"SET_WIFI_POWERSAVE={_val('SET_WIFI_POWERSAVE')}",
            f"SET_NVME_SCHED={_val('SET_NVME_SCHED')}",
            f"NVME_IO_SCHEDULER={_val('NVME_IO_SCHEDULER')}",
            "",
            "# --- Logging -----------------------------------------------------",
            f"LOG_LEVEL={_val('LOG_LEVEL')}",
            "",
        ]
        return "\n".join(lines)

    # ── action handlers ───────────────────────────────────────────────────────

    def _on_install(self):
        install_sh = self._BUNDLED_SRC / "install.sh"
        if not install_sh.is_file():
            QMessageBox.warning(
                self, "Bundled source not found",
                f"install.sh not found at:\n  {install_sh}\n\n"
                "Make sure the lutris-game-tune-main/ directory is present "
                "next to DRSTool.py (it is part of the DRSTool repository)."
            )
            return
        self._log(f"==> Running pkexec bash {install_sh}")
        self._run_proc("pkexec", ["bash", str(install_sh)], tag="install")

    def _on_uninstall(self):
        reply = QMessageBox.question(
            self, "Confirm uninstall",
            "Uninstall lutris-game-tune?\n\n"
            "/etc/lutris-game-tune.conf will be kept (your settings are preserved).\n"
            "The wrapper binary and lib directory will be removed.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        uninstall_sh = str(LGTUNE_LIB_DIR / "uninstall.sh")
        if not Path(uninstall_sh).is_file():
            self._log(f"ERROR: uninstall.sh not found at {uninstall_sh}")
            return
        self._log(f"==> Running pkexec bash {uninstall_sh}")
        self._run_proc("pkexec", ["bash", uninstall_sh], tag="uninstall")

    def _on_save(self):
        conf_text = self._build_conf_text()
        # Content goes through stdin. It is written to a temp file next to the
        # target and renamed over it, so an interrupted write can never leave
        # an empty/truncated /etc/lutris-game-tune.conf behind.
        self._log("==> Writing /etc/lutris-game-tune.conf via pkexec …")
        script = (
            "set -e; "
            "tmp=$(mktemp /etc/.lutris-game-tune.conf.XXXXXX); "
            "trap 'rm -f \"$tmp\"' EXIT; "
            "cat > \"$tmp\"; "
            "chmod 644 \"$tmp\"; chown root:root \"$tmp\"; "
            "mv -f \"$tmp\" /etc/lutris-game-tune.conf; "
            "trap - EXIT; "
            "echo 'Saved: /etc/lutris-game-tune.conf'"
        )
        self._run_proc(
            "pkexec",
            ["bash", "-c", script],
            tag="save",
            stdin_data=conf_text.encode()
        )

    def _on_run_status(self):
        if not self._is_installed():
            self._log("lutris-game-tune is not installed.")
            return
        self._log(f"==> {LGTUNE_WRAPPER} STATUS")
        self._run_proc("pkexec", [str(LGTUNE_WRAPPER), "STATUS"], tag="status")

    def _on_view_log(self):
        if not LGTUNE_LOG.is_file():
            self._log(f"Log file not found: {LGTUNE_LOG}")
            return
        self._log(f"==> tail -80 {LGTUNE_LOG}")
        self._run_proc("tail", ["-n", "80", str(LGTUNE_LOG)], tag="log")

    # ── QProcess plumbing ─────────────────────────────────────────────────────

    def _run_proc(self, program: str, args: List[str], tag: str,
                  stdin_data: Optional[bytes] = None):
        # Callers that need root pass "pkexec" as the program themselves.
        if self._proc and self._proc.state() != QProcess.NotRunning:
            self._log("A process is already running — please wait.")
            return
        self._out_buf = ""
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(lambda p=proc: self._on_output(p))
        proc.finished.connect(lambda code, st, t=tag: self._on_finished(code, st, t))
        proc.errorOccurred.connect(
            lambda err, p=proc: self._on_proc_error(err, p)
        )
        self._proc = proc
        proc.start(program, args)
        if stdin_data is not None:
            proc.write(stdin_data)
            proc.closeWriteChannel()

    def _on_output(self, proc: QProcess):
        data = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        self._out_buf = (self._out_buf + data)[-4000:]
        if data.strip():
            self._log(data.rstrip("\n"))

    def _on_proc_error(self, error, proc: QProcess):
        if error == QProcess.FailedToStart:
            self._log(
                f"ERROR: could not start '{proc.program()}' — is pkexec installed?"
            )

    def _on_finished(self, exit_code: int, exit_status, tag: str):
        if exit_status == QProcess.CrashExit:
            self._log(f"ERROR: '{tag}' process crashed.")
            return
        if tag != "log" and pkexec_auth_failed(exit_code, self._out_buf):
            self._log("Authorization cancelled or denied (pkexec).")
            return
        if exit_code != 0 and tag not in ("log", "status"):
            self._log(f"ERROR: '{tag}' failed (exit code {exit_code}).")
            return
        if tag == "install":
            self._log("==> Installation complete.")
            self._refresh_install_state()
            self._load_conf()
        elif tag == "uninstall":
            self._log("==> Uninstall complete.")
            self._refresh_install_state()
        elif tag == "save":
            self._log("==> Config saved successfully.")


# ============================================================================
# ExtraToolsWidget — QTabWidget wrapping vk_flip_meter + lutris-game-tune
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
            "border-radius:3px; background:#1a1f28;} "
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
            "border-radius:3px; background:#1a1f28;} "
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
                        "border-radius:3px; background:#1a1f28;} "
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
            "QLineEdit{background:#0d0f12; border:1px solid #1e2535; color:#76b900; "
            "font-family:monospace; font-size:12px; padding:4px 6px;}"
        )
        outer.addWidget(self._preview)

        copy_row = QHBoxLayout()
        copy_row.addStretch()
        copy_btn = QPushButton("Copy Command")
        copy_btn.setStyleSheet(
            "QPushButton{background:#141720; border:1px solid #1e2535; color:#e8eaf0; "
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
        if not self.is_enabled():
            return {}
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
        self._enable_chk.setChecked(False)
        self._mangohud_chk.setChecked(False)
        for gf in GAMESCOPE_FLAGS:
            ctrl = self._controls[gf.flag]
            if gf.kind == "toggle":
                ctrl.setChecked(False)
            elif gf.kind == "enum":
                ctrl.setCurrentIndex(0)
            else:
                ctrl.clear()
        self._emit_changed()

    def load_flag_values(self, enabled: bool, values: Dict[str, str]):
        """Restore state, e.g. from a saved profile."""
        self._enable_chk.setChecked(enabled)
        self._mangohud_chk.setChecked(self.MANGOHUD_KEY in values)
        for gf in GAMESCOPE_FLAGS:
            ctrl = self._controls[gf.flag]
            v = values.get(gf.flag)
            if gf.kind == "toggle":
                ctrl.setChecked(gf.flag in values)
            elif gf.kind == "enum":
                ctrl.setCurrentText(v or "")
            else:
                ctrl.setText(v or "")
        self._emit_changed()


# ============================================================================
# DLSS Updater — NVIDIA NGX OTA client (Extra Tools → "DLSS Updater")
# ----------------------------------------------------------------------------
# NVIDIA's NGX updater pulls feature DLLs from a public, listable S3 bucket.
# Server layout this client relies on:
#   <ns>/org/nvidia/team/ngx/models/<feature>/versions/<packed>/files/<arch>_<app>.<bin|zip>[.sha256]
#   * packed version      = major << 16 | minor << 8 | patch
#   * dlss* .bin payloads = plain PE DLLs  → saved as nvngx_<feature>.dll
#   * sl_sdk* .zip        = Streamline archive with sl.*.dll members
#   * <ns>/…/models/config/versions/2/files/nvngx_server_config.txt pins the
#     current version per feature ("[dlss]\napp_E658700 = 310.9.0")
# The listing endpoint stops at 1000 keys, so newer namespaces may be cut off.
# Discovery therefore also HEAD-checks the config pins and the next few minor
# versions after the newest one seen.
# Stdlib only (urllib / ElementTree / hashlib / zipfile) — no new deps.
# ============================================================================

import hashlib
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QThread, QUrl, QStandardPaths, QPointF
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QProgressBar

NGX_HOST = "https://ngx.download.nvidia.com"
_NGX_MODELS = "org/nvidia/team/ngx/models"
_NGX_UA = "DRSTool/ngx-check (+https://github.com/arabcian/DRSTool)"
_NGX_KEY_RE = re.compile(
    r"^(?P<ns>[^/]+)/" + re.escape(_NGX_MODELS) +
    r"/(?P<feat>[^/]+)/versions/(?P<vid>\d+)/files/"
    r"(?P<payload>(?P<arch>[0-9A-Fa-f]+)_(?P<app>[0-9A-Fa-f]+)\.(?P<ext>bin|zip))"
    r"(?P<sha>\.sha256)?$")
_NGX_ARCH_UNIVERSAL = "160"          # Turing arch id; NVIDIA ships this build to every RTX gen
_NGX_APP_DLSS = "E658700"            # universal app id for nvngx_dlss*.dll
_NGX_APP_SL = "E658703"              # universal app id for the Streamline SDK zip
_NGX_TITLES = {
    "dlss": "DLSS Super Resolution",
    "dlssd": "DLSS Ray Reconstruction",
    "dlssg": "DLSS Frame Generation",
    "sl_sdk_0": "Streamline SDK",
}
_NGX_ORDER = ("dlss", "dlssd", "dlssg", "sl_sdk_0")
_NGX_PROBE_MINORS = 3                # look this many minor versions ahead
_NGX_PROBE_BUILDS = (0, 128)         # NVIDIA's usual patch numbers for new drops


def ngx_is_sl(feat: str) -> bool:
    return feat.startswith("sl_sdk")


def ngx_is_feature(feat: str) -> bool:
    """DLSS family (any future dlss* dir, minus the override bundles) + Streamline."""
    return ngx_is_sl(feat) or (feat.startswith("dlss") and "override" not in feat)


def ngx_title(feat: str) -> str:
    return _NGX_TITLES.get(feat, feat)


def ngx_target_name(feat: str) -> Optional[str]:
    """File name a game expects; None for Streamline (zip members are already named)."""
    return None if ngx_is_sl(feat) else f"nvngx_{feat}.dll"


def ngx_app_id(feat: str) -> str:
    return _NGX_APP_SL if ngx_is_sl(feat) else _NGX_APP_DLSS


def ngx_universal_payload(feat: str) -> str:
    return f"{_NGX_ARCH_UNIVERSAL}_{ngx_app_id(feat)}.{'zip' if ngx_is_sl(feat) else 'bin'}"


def ngx_feature_rank(feat: str):
    return (_NGX_ORDER.index(feat), "") if feat in _NGX_ORDER else (len(_NGX_ORDER), feat)


def ngx_ver_str(vid: int) -> str:
    return f"{vid >> 16}.{(vid >> 8) & 0xFF}.{vid & 0xFF}"


def ngx_ver_pack(text: str) -> Optional[int]:
    m = re.fullmatch(r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*", text or "")
    if not m:
        return None
    major, minor, patch = (int(g or 0) for g in m.groups())
    if minor > 0xFF or patch > 0xFF:
        return None
    return (major << 16) | (minor << 8) | patch


def ngx_human(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{int(v)} B" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{n} B"


def ngx_parse_server_config(text: str) -> List[tuple]:
    """'[dlss]\\napp_E658700 = 310.9.0' → [('dlss', 'E658700', '310.9.0'), …]"""
    pins, section = [], ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        key, sep, val = line.partition("=")
        key = key.strip()
        if sep and ngx_is_feature(section) and key.lower().startswith("app_"):
            pins.append((section, key[4:].upper(), val.strip()))
    return pins


class NgxCancelled(Exception):
    pass


@dataclass
class NgxBuild:
    """One downloadable payload (feature + version), possibly on several namespaces."""
    feature: str
    vid: int
    payload: str
    size: int = 0
    mirrors: Dict[str, bool] = field(default_factory=dict)   # ns → has .sha256 sidecar

    @property
    def version(self) -> str:
        return ngx_ver_str(self.vid)

    @property
    def verified(self) -> bool:
        return any(self.mirrors.values())

    def url(self, ns: str) -> str:
        return f"{NGX_HOST}/{ns}/{_NGX_MODELS}/{self.feature}/versions/{self.vid}/files/{self.payload}"

    def sources(self) -> List[tuple]:
        """[(url, sha_url | None)] — mirrors with a checksum first, then by name."""
        order = sorted(self.mirrors.items(), key=lambda kv: (not kv[1], kv[0]))
        return [(self.url(ns), self.url(ns) + ".sha256" if sha else None) for ns, sha in order]

    def tag(self) -> str:
        arch, app = self.payload.rsplit(".", 1)[0].split("_", 1)
        tags = []
        if arch.upper() != _NGX_ARCH_UNIVERSAL:
            tags.append(arch.upper())
        if app.upper() != ngx_app_id(self.feature):
            tags.append(f"app_{app.upper()}")
        return "/".join(tags)

    def rank(self) -> int:
        """Preference among payloads of the same version (universal build wins)."""
        arch, app = self.payload.rsplit(".", 1)[0].split("_", 1)
        return (app.upper() == ngx_app_id(self.feature)) * 2 + (arch.upper() == _NGX_ARCH_UNIVERSAL)


class NgxClient:
    """Discovery + download against the NGX bucket. Blocking; run it off the GUI thread."""

    def __init__(self, log=None, cancel: Optional[threading.Event] = None):
        self._log = log or (lambda level, msg: None)
        self._cancel = cancel or threading.Event()

    # ── HTTP ────────────────────────────────────────────────────────────────
    def _check(self):
        if self._cancel.is_set():
            raise NgxCancelled()

    def _open(self, url: str, method: str = "GET", timeout: float = 20):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": _NGX_UA})
        return urllib.request.urlopen(req, timeout=timeout)

    def _text(self, url: str, timeout: float = 20) -> str:
        with self._open(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")

    def _head(self, url: str) -> Optional[int]:
        """Content-Length if the object exists, else None."""
        try:
            with self._open(url, "HEAD", timeout=12) as r:
                return int(r.headers.get("Content-Length") or 0) if r.status == 200 else None
        except (urllib.error.URLError, OSError, ValueError):
            return None

    # ── discovery ───────────────────────────────────────────────────────────
    def list_bucket(self) -> List[tuple]:
        keys, token = [], ""
        for page in range(1, 101):
            self._check()
            q = {"list-type": "2", "max-keys": "1000"}
            if token:
                q["continuation-token"] = token
            root = ET.fromstring(self._text(f"{NGX_HOST}/?{urllib.parse.urlencode(q)}", timeout=30))
            n = 0
            for el in root.iter():
                if el.tag.rsplit("}", 1)[-1] != "Contents":
                    continue
                key = size = None
                for ch in el:
                    name = ch.tag.rsplit("}", 1)[-1]
                    if name == "Key":
                        key = ch.text
                    elif name == "Size":
                        size = ch.text
                if key:
                    keys.append((key, int(size or 0)))
                    n += 1
            self._log("info", f"listing page {page}: {n} objects")
            token = next((el.text or "" for el in root.iter()
                          if el.tag.rsplit("}", 1)[-1] == "NextContinuationToken"), "")
            if not token:
                return keys
        raise RuntimeError("bucket listing did not terminate after 100 pages")

    def discover(self) -> List[NgxBuild]:
        self._log("info", f"querying {NGX_HOST} …")
        builds: Dict[tuple, NgxBuild] = {}
        skipped: Dict[str, int] = {}

        def add(ns, feat, vid, payload, size, sidecar):
            b = builds.setdefault((feat, vid, payload), NgxBuild(feat, vid, payload))
            b.size = max(b.size, size)
            b.mirrors[ns] = b.mirrors.get(ns, False) or sidecar

        for key, size in self.list_bucket():
            m = _NGX_KEY_RE.match(key)
            if not m or not ngx_is_feature(m["feat"]) \
                    or (m["ext"] == "zip") != ngx_is_sl(m["feat"]):
                if _NGX_MODELS in key and not key.endswith("/"):
                    d = key.split(_NGX_MODELS + "/", 1)[1].split("/", 1)[0]
                    if d and d != "config" and not ngx_is_feature(d):
                        skipped[d] = skipped.get(d, 0) + 1
                continue
            sidecar = bool(m["sha"])
            add(m["ns"], m["feat"], int(m["vid"]), m["payload"], 0 if sidecar else size, sidecar)
        if not builds:
            raise RuntimeError("server answered but no DLSS/Streamline objects found — layout changed?")
        if skipped:
            self._log("info", "other NGX features ignored: " +
                      ", ".join(f"{d} ({n})" for d, n in sorted(skipped.items())))

        namespaces = sorted({ns for b in builds.values() for ns in b.mirrors})
        self._log("info", "namespaces: " + ", ".join(namespaces))

        # Candidate (ns, feature, vid) objects the listing may have missed.
        seen = {(f, v) for (f, v, _p) in builds}
        jobs = set()
        for ns in namespaces:
            self._check()
            url = f"{NGX_HOST}/{ns}/{_NGX_MODELS}/config/versions/2/files/nvngx_server_config.txt"
            try:
                pins = ngx_parse_server_config(self._text(url))
            except (urllib.error.URLError, OSError):
                continue
            for feat, app, ver in pins:
                vid = ngx_ver_pack(ver)
                if vid is not None and app == ngx_app_id(feat):
                    jobs.add((ns, feat, vid, "pin"))
        newest: Dict[str, int] = {}
        for (f, v) in seen:
            newest[f] = max(newest.get(f, 0), v)
        for f, v in newest.items():
            major, minor = v >> 16, (v >> 8) & 0xFF
            for mi in range(minor + 1, min(minor + _NGX_PROBE_MINORS, 0xFF) + 1):
                for build in _NGX_PROBE_BUILDS:
                    vid = (major << 16) | (mi << 8) | build
                    for ns in namespaces:
                        jobs.add((ns, f, vid, "probe"))
        # A pin for something already listed on that ns needs no check.
        jobs = {j for j in jobs
                if not ((j[1], j[2], ngx_universal_payload(j[1])) in builds
                        and j[0] in builds[(j[1], j[2], ngx_universal_payload(j[1]))].mirrors)}

        def check(job):
            if self._cancel.is_set():
                return job, None, False
            ns, feat, vid, _why = job
            url = NgxBuild(feat, vid, ngx_universal_payload(feat)).url(ns)
            size = self._head(url)
            return job, size, size is not None and self._head(url + ".sha256") is not None

        if jobs:
            self._log("info", f"checking {len(jobs)} pinned/staged candidates …")
            with ThreadPoolExecutor(max_workers=8) as pool:
                for (ns, feat, vid, why), size, sidecar in pool.map(check, sorted(jobs)):
                    if size is None:
                        continue
                    if (feat, vid) not in seen:
                        self._log("ok", f"{'config pin' if why == 'pin' else 'staged build'}: "
                                        f"{ngx_title(feat)} {ngx_ver_str(vid)} on {ns}")
                        seen.add((feat, vid))
                    add(ns, feat, vid, ngx_universal_payload(feat), size, sidecar)
        self._check()

        # One row per (feature, version): keep the universal payload when present.
        best: Dict[tuple, NgxBuild] = {}
        for b in builds.values():
            cur = best.get((b.feature, b.vid))
            if cur is None or b.rank() > cur.rank():
                best[(b.feature, b.vid)] = b
        return sorted(best.values(), key=lambda b: (ngx_feature_rank(b.feature), -b.vid))

    # ── download ────────────────────────────────────────────────────────────
    def download(self, b: NgxBuild, dest: Path, progress=None) -> tuple:
        """Fetch b into dest. Returns (written file names, sha256 hex, checksum_verified)."""
        dest.mkdir(parents=True, exist_ok=True)
        errors = []
        for i, (url, sha_url) in enumerate(b.sources()):
            if i:
                self._log("warn", "  trying next mirror …")
            try:
                return self._fetch(b, url, sha_url, dest, progress)
            except NgxCancelled:
                raise
            except Exception as e:  # noqa: BLE001 — every mirror failure is reported
                ns = urllib.parse.urlsplit(url).path.split("/")[1]
                errors.append(f"{ns}: {e}")
                self._log("warn", f"  {ns}: {e}")
        raise RuntimeError("; ".join(errors) or "no mirror available")

    def _fetch(self, b: NgxBuild, url: str, sha_url: Optional[str], dest: Path, progress) -> tuple:
        expected = None
        if sha_url:
            body = self._text(sha_url).split()
            expected = body[0].lower() if body else ""
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("malformed .sha256 sidecar")
        fd, tmp = tempfile.mkstemp(prefix=".ngx-", suffix=".part", dir=dest)
        try:
            h, done = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as out, self._open(url, timeout=30) as r:
                total = int(r.headers.get("Content-Length") or 0) or b.size
                while True:
                    self._check()
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            digest = h.hexdigest()
            if expected and digest != expected:
                raise ValueError(f"sha256 mismatch ({digest[:12]}… ≠ {expected[:12]}…)")
            if not expected and total and done != total:
                raise ValueError(f"truncated download ({done}/{total} bytes)")
            return self._install(b, Path(tmp), dest), digest, bool(expected)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _install(b: NgxBuild, tmp: Path, dest: Path) -> List[str]:
        name = ngx_target_name(b.feature)
        if name:
            with open(tmp, "rb") as f:
                if f.read(2) != b"MZ":
                    raise ValueError("payload is not a PE/DLL file")
            os.replace(tmp, dest / name)
            return [name]
        written = []
        with zipfile.ZipFile(tmp) as zf:
            for info in zf.infolist():
                base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
                if info.is_dir() or not base.lower().endswith(".dll") or base in (".", ".."):
                    continue
                fd, part = tempfile.mkstemp(prefix=".ngx-", suffix=".part", dir=dest)
                try:
                    with os.fdopen(fd, "wb") as out, zf.open(info) as src:
                        shutil.copyfileobj(src, out, 1 << 16)
                    os.replace(part, dest / base)
                finally:
                    if os.path.exists(part):
                        os.unlink(part)
                written.append(base)
        if not written:
            raise ValueError("Streamline archive contained no .dll files")
        return sorted(written)


class _NgxWorker(QThread):
    """Runs NgxClient.discover() or a batch download off the GUI thread."""
    log = Signal(str, str)                   # level, message
    progress = Signal(float, str)            # fraction 0..1, label
    discovered = Signal(object)              # List[NgxBuild]
    downloaded = Signal(int, int, str)       # ok, failed, dest dir
    failed = Signal(str)
    state = Signal(str, str)                 # feature, run | ok | err

    def __init__(self, builds: Optional[List[NgxBuild]] = None,
                 dest: Optional[Path] = None, parent=None):
        super().__init__(parent)
        self._builds, self._dest = builds, dest
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        client = NgxClient(lambda lvl, msg: self.log.emit(lvl, msg), self._cancel)
        try:
            if self._builds is None:
                self.discovered.emit(client.discover())
                return
            total = sum(b.size for b in self._builds) or 1
            base, ok, bad = 0, 0, 0
            for b in self._builds:
                label = f"{ngx_title(b.feature)} {b.version}"
                self.log.emit("info", f"↓ {label} ({ngx_human(b.size)})")
                self.state.emit(b.feature, "run")
                cb = lambda d, _t, base=base, label=label: \
                    self.progress.emit(min(1.0, (base + d) / total), label)
                try:
                    names, digest, verified = client.download(b, self._dest, cb)
                    ok += 1
                    self.state.emit(b.feature, "ok")
                    self.log.emit("ok", f"✓ {label} → {', '.join(names)}  "
                                        f"[sha256 {digest[:16]}… "
                                        f"{'verified' if verified else 'no sidecar, size-checked'}]")
                except NgxCancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    bad += 1
                    self.state.emit(b.feature, "err")
                    self.log.emit("err", f"✗ {label}: {e}")
                base += b.size
            self.progress.emit(1.0, "done")
            self.downloaded.emit(ok, bad, str(self._dest))
        except NgxCancelled:
            self.failed.emit("cancelled")
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


_NGX_ACCENTS = {
    "dlss": "#76b900",       # SR — NVIDIA green
    "dlssd": "#3fa7d6",      # RR — cyan
    "dlssg": "#b07cf0",      # FG — violet
    "sl_sdk_0": "#d2a03c",   # Streamline — amber
}
_NGX_SHORT = {"dlss": "SR", "dlssd": "RR", "dlssg": "FG", "sl_sdk_0": "SL"}


def _ngx_accent(feat: str) -> str:
    return _NGX_ACCENTS.get(feat, T["text2"])


def _ngx_rgba(hex_color: str, alpha: float) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


_NGX_COMBO_SS = f"""
QComboBox {{ background:{T['bg']}; border:1px solid {T['border_hi']}; border-radius:6px;
            color:{T['text']}; font-family:monospace; font-size:12px; padding:5px 10px; }}
QComboBox:hover {{ border-color:{T['accent_dim']}; }}
QComboBox:disabled {{ color:#3a4250; border-color:#232a36; }}
QComboBox::drop-down {{ border:none; width:24px; }}
QComboBox::down-arrow {{ image:none; width:0; height:0; }}
QComboBox QAbstractItemView {{ background:{T['surface']}; color:{T['text']};
    border:1px solid {T['border_hi']}; selection-background-color:rgba(118,185,0,0.18);
    font-family:monospace; font-size:12px; outline:0; }}
"""


class _NgxVersionCombo(QComboBox):
    """Version picker with an "N versions" hint pinned left of the arrow."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hint = QLabel(self)
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)

    def refresh_hint(self):
        n = self.count()
        self._hint.setText(f"{n} version{'s' if n != 1 else ''}")
        col = T["accent_hi"] if n > 1 and self.isEnabled() else T["muted"]
        self._hint.setStyleSheet(f"color:{col}; background:transparent; font-size:11px; font-weight:700;")
        self._hint.adjustSize()
        self._place_hint()

    def _place_hint(self):
        self._hint.move(self.width() - 28 - self._hint.width(),
                        (self.height() - self._hint.height()) // 2)

    def resizeEvent(self, e):             # noqa: N802
        super().resizeEvent(e)
        self._place_hint()

    def paintEvent(self, e):              # noqa: N802
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(T["text2"] if self.isEnabled() else "#3a4250"), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        cx, cy = self.width() - 14, self.height() / 2
        p.drawPolyline([QPointF(cx - 4, cy - 2), QPointF(cx, cy + 2), QPointF(cx + 4, cy - 2)])
        p.end()

    def changeEvent(self, e):             # noqa: N802
        super().changeEvent(e)
        if e.type() == QEvent.EnabledChange:
            self.refresh_hint()


class _NgxFeatureCard(QFrame):
    """One card per NGX feature: include toggle, version picker, build facts."""
    changed = Signal()

    def __init__(self, feature: str, builds: List[NgxBuild], parent=None):
        super().__init__(parent)
        self.feature = feature
        self._builds = builds                      # newest first
        self._accent = _ngx_accent(feature)
        self.setObjectName("ngxCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._badge = QLabel(_NGX_SHORT.get(feature, feature[:2].upper()))
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setFixedSize(30, 22)
        top.addWidget(self._badge)
        self._on = QCheckBox(ngx_title(feature))
        self._on.setChecked(True)
        self._on.setCursor(Qt.PointingHandCursor)
        self._on.toggled.connect(self._sync)
        top.addWidget(self._on, 1)
        lay.addLayout(top)

        mid = QHBoxLayout()
        mid.setSpacing(10)
        self._ver = QLabel()
        mid.addWidget(self._ver, 0, Qt.AlignBottom)
        self._age = QLabel()
        mid.addWidget(self._age, 0, Qt.AlignVCenter)
        mid.addStretch(1)
        self._file = QLabel(ngx_target_name(feature) or "sl.*.dll")
        self._file.setToolTip(ngx_target_name(feature) or
                              "sl.common.dll, sl.dlss.dll, sl.dlss_g.dll, … (unpacked from the SDK zip)")
        mid.addWidget(self._file, 0, Qt.AlignBottom)
        lay.addLayout(mid)

        self._combo = _NgxVersionCombo()
        self._combo.setStyleSheet(_NGX_COMBO_SS + "QComboBox { padding-right:96px; }")
        for b in builds:
            tag = f"  [{b.tag()}]" if b.tag() else ""
            self._combo.addItem(f"v{b.version}{tag}  ·  {ngx_human(b.size)}")
        self._combo.setMaxVisibleItems(16)
        self._combo.refresh_hint()
        self._combo.currentIndexChanged.connect(self._sync)
        lay.addWidget(self._combo)

        self._meta = QLabel()
        self._meta.setTextFormat(Qt.RichText)
        lay.addWidget(self._meta)

        self._sync()

    # ── state ───────────────────────────────────────────────────────────────
    def selected(self) -> Optional[NgxBuild]:
        if not self._on.isChecked() or self._combo.currentIndex() < 0:
            return None
        return self._builds[self._combo.currentIndex()]

    def set_pick(self, include: bool, newest: bool = False):
        for w in (self._on, self._combo):
            w.blockSignals(True)
        self._on.setChecked(include)
        if newest:
            self._combo.setCurrentIndex(0)
        for w in (self._on, self._combo):
            w.blockSignals(False)
        self._sync()

    def set_state(self, text: str = "", color: str = ""):
        self._state_txt, self._state_col = text, color
        self._render_meta()

    def _render_meta(self):
        idx = max(0, self._combo.currentIndex())
        b = self._builds[idx]
        dim = not self._on.isChecked()
        sep = f'<span style="color:{T["border_hi"]}">&nbsp;│&nbsp;</span>'
        sha = (f'<span style="color:{T["accent_hi"] if not dim else T["muted"]}">SHA-256 sidecar ✓</span>'
               if b.verified else f'<span style="color:{T["warn"] if not dim else T["muted"]}">'
                                  f'no sidecar · size-checked</span>')
        mirrors = f"{len(b.mirrors)} mirror" + ("s" if len(b.mirrors) != 1 else "")
        parts = [mirrors, sha]
        st = getattr(self, "_state_txt", "")
        if st:
            parts.append(f'<b style="color:{self._state_col or T["text"]}">{st}</b>')
        self._meta.setText(sep.join(parts))
        self._meta.setStyleSheet(f"color:{T['muted']}; font-size:11px;")

    def _sync(self, *_):
        on = self._on.isChecked()
        idx = max(0, self._combo.currentIndex())
        b = self._builds[idx]
        acc = self._accent
        self._combo.setEnabled(on)
        self._ver.setText(b.version)
        self._ver.setStyleSheet(
            f"color:{acc if on else T['muted']}; font-family:monospace; font-size:24px; "
            f"font-weight:700; letter-spacing:-0.5px;")
        if idx == 0:
            age, fg, bg = "NEWEST", acc, _ngx_rgba(acc, 0.14)
        else:
            age, fg, bg = f"{idx} BEHIND", T["warn"], _ngx_rgba(T["warn"], 0.12)
        if not on:
            fg, bg = T["muted"], "transparent"
        self._age.setText(age)
        self._age.setStyleSheet(f"color:{fg}; background:{bg}; border:1px solid {fg}; "
                                f"border-radius:8px; padding:1px 7px; font-size:10px; "
                                f"font-weight:700; letter-spacing:0.6px;")
        self._file.setStyleSheet(f"color:{T['text2'] if on else T['muted']}; "
                                 f"font-family:monospace; font-size:12px;")
        self._badge.setStyleSheet(
            f"color:{T['bg'] if on else T['muted']}; background:{acc if on else T['raised']}; "
            f"border-radius:5px; font-size:11px; font-weight:800;")
        self._on.setStyleSheet(f"""
QCheckBox {{ color:{T['text'] if on else T['muted']}; font-size:13px; font-weight:700; spacing:8px; }}
QCheckBox::indicator {{ width:30px; height:14px; border-radius:7px;
    border:1px solid {T['border_hi']}; background:{T['bg']}; }}
QCheckBox::indicator:checked {{ background:{acc}; border-color:{acc}; }}
""")
        self.setStyleSheet(f"""
QFrame#ngxCard {{ background:{T['surface'] if on else T['bg']};
    border:1px solid {_ngx_rgba(acc, 0.55) if on else T['border']};
    border-top:3px solid {acc if on else T['border_hi']}; border-radius:9px; }}
""")
        self.setToolTip("\n".join(u for u, _s in b.sources()))
        self._render_meta()
        self.changed.emit()


class NgxUpdaterWidget(QWidget):
    """Extra Tools sub-tab: NGX DLSS / Streamline builds as feature cards + a download dock."""

    _LOG_COLORS = {"info": T["text2"], "ok": T["accent_hi"], "warn": T["warn"], "err": T["danger"]}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: Optional[_NgxWorker] = None
        self._qs = QSettings(APP_ID, APP_ID)
        self._last_dest: Optional[Path] = None
        self._cards: List[_NgxFeatureCard] = []
        self._cols = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(12)

        # ── hero strip: what this is + server state + check ─────────────────
        hero = QFrame()
        hero.setObjectName("ngxHero")
        hero.setAttribute(Qt.WA_StyledBackground, True)
        hero.setStyleSheet(f"""
QFrame#ngxHero {{ border:1px solid {T['border']}; border-radius:10px;
    background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #17200f, stop:0.45 {T['surface']},
                               stop:1 {T['surface']}); }}
""")
        h = QHBoxLayout(hero)
        h.setContentsMargins(18, 14, 16, 14)
        h.setSpacing(14)
        col = QVBoxLayout()
        col.setSpacing(3)
        title = QLabel("DLSS Updater")
        title.setStyleSheet(f"color:{T['text']}; font-size:17px; font-weight:800;")
        col.addWidget(title)
        chips = " ".join(
            f'<span style="color:{_ngx_accent(f)}">■</span>&nbsp;{_NGX_SHORT[f]}' for f in _NGX_ORDER)
        sub = QLabel(f"{chips}&nbsp;&nbsp;<span style='color:{T['muted']}'>straight from NVIDIA's NGX "
                     f"OTA bucket — staged drops included</span>")
        sub.setTextFormat(Qt.RichText)
        sub.setStyleSheet(f"color:{T['text2']}; font-size:12px;")
        col.addWidget(sub)
        hero.setToolTip(
            "Lists every DLSS Super Resolution, Ray Reconstruction, Frame Generation and Streamline SDK "
            "build on ngx.download.nvidia.com (including staged builds the NVIDIA App does not offer "
            "yet) and saves them with the file names games expect: nvngx_dlss.dll, nvngx_dlssd.dll, "
            "nvngx_dlssg.dll, sl.*.dll. Downloads are checked against NVIDIA's SHA-256 sidecars "
            "when the server publishes them.")
        h.addLayout(col, 1)
        self._pill = QLabel()
        h.addWidget(self._pill, 0, Qt.AlignVCenter)
        self._check_btn = _tool_btn("⟳  Check NGX servers", bg="#1d2a10", border="#4a7300",
                                    fg="#cfe8a8", bold=True)
        self._check_btn.clicked.connect(self._start_check)
        h.addWidget(self._check_btn, 0, Qt.AlignVCenter)
        lay.addWidget(hero)

        # ── quick selection row ─────────────────────────────────────────────
        self._quick = QWidget()
        q = QHBoxLayout(self._quick)
        q.setContentsMargins(2, 0, 2, 0)
        q.setSpacing(4)
        cap = QLabel("PICK")
        cap.setStyleSheet(f"color:{T['muted']}; font-size:10px; font-weight:800; letter-spacing:1.2px;")
        q.addWidget(cap)
        link_ss = (f"QPushButton{{background:transparent; border:none; color:{T['text2']}; "
                   f"font-size:12px; padding:2px 6px; text-decoration:underline;}}"
                   f"QPushButton:hover{{color:{T['accent_hi']};}}"
                   f"QPushButton:disabled{{color:#3a4250;}}")
        self._latest_btn = QPushButton("newest of each")
        self._clear_btn = QPushButton("none")
        for b, fn in ((self._latest_btn, lambda: self._select(latest=True)),
                      (self._clear_btn, lambda: self._select(latest=False))):
            b.setStyleSheet(link_ss)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(fn)
            q.addWidget(b)
        q.addStretch(1)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        q.addWidget(self._status)
        lay.addWidget(self._quick)

        # ── cards (or an empty-state panel before the first check) ──────────
        self._stack = QStackedWidget()
        empty = QLabel("No builds loaded yet.\n\nPress “Check NGX servers” to list every DLSS and "
                       "Streamline drop\nNVIDIA currently hosts — one card per feature.")
        empty.setAlignment(Qt.AlignCenter)
        empty.setMinimumHeight(200)
        empty.setStyleSheet(f"color:{T['muted']}; font-size:13px; border:1px dashed {T['border_hi']}; "
                            f"border-radius:10px; background:{T['bg']};")
        self._stack.addWidget(empty)
        host = QWidget()
        self._grid = QGridLayout(host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(10)
        self._stack.addWidget(host)
        lay.addWidget(self._stack)

        # ── download dock ───────────────────────────────────────────────────
        dock = QFrame()
        dock.setObjectName("ngxDock")
        dock.setAttribute(Qt.WA_StyledBackground, True)
        dock.setStyleSheet(f"QFrame#ngxDock {{ background:{T['raised']}; border:1px solid "
                           f"{T['border_hi']}; border-radius:10px; }}")
        d = QVBoxLayout(dock)
        d.setContentsMargins(14, 12, 14, 12)
        d.setSpacing(8)

        r1 = QHBoxLayout()
        r1.setSpacing(6)
        save = QLabel("Save to")
        save.setStyleSheet(f"color:{T['text2']}; font-size:12px; font-weight:700;")
        r1.addWidget(save)
        default_base = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation) \
            or str(Path.home() / "Downloads")
        self._base = QLineEdit(str(self._qs.value("ngx/base_dir", default_base)))
        self._base.setStyleSheet(FLM_FIELD_SS)
        self._base.textChanged.connect(self._refresh_dest)
        r1.addWidget(self._base, 3)
        browse = _tool_btn("…")
        browse.setToolTip("Choose base folder")
        browse.clicked.connect(self._browse)
        r1.addWidget(browse)
        slash = QLabel("/")
        slash.setStyleSheet(f"color:{T['muted']}; font-family:monospace; font-size:14px;")
        r1.addWidget(slash)
        self._sub = QLineEdit()
        self._sub.setStyleSheet(FLM_FIELD_SS)
        self._sub.setPlaceholderText("subfolder")
        self._sub.setMaximumWidth(160)
        self._sub.textChanged.connect(self._refresh_dest)
        r1.addWidget(self._sub, 1)
        self._auto = QCheckBox("auto")
        self._auto.setToolTip("Name the subfolder after the newest selected version")
        self._auto.setChecked(True)
        self._auto.setStyleSheet(f"color:{T['text2']}; font-size:12px;")
        self._auto.toggled.connect(self._refresh_dest)
        r1.addWidget(self._auto)
        d.addLayout(r1)

        self._dest_lbl = ElidedLabel()
        self._dest_lbl.setStyleSheet(f"color:{T['muted']}; font-family:monospace; font-size:11px;")
        d.addWidget(self._dest_lbl)

        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(5)
        self._bar.setStyleSheet(f"""
QProgressBar {{ background:{T['bg']}; border:none; border-radius:2px; }}
QProgressBar::chunk {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 {T['accent_dim']}, stop:1 {T['accent_hi']}); border-radius:2px; }}
""")
        d.addWidget(self._bar)

        r2 = QHBoxLayout()
        r2.setSpacing(8)
        self._bar_lbl = QLabel("")
        self._bar_lbl.setStyleSheet(f"color:{T['text2']}; font-size:11px;")
        r2.addWidget(self._bar_lbl, 1)
        self._open_btn = _tool_btn("Open folder")
        self._open_btn.clicked.connect(self._open_dest)
        self._cancel_btn = _tool_btn("Cancel")
        self._cancel_btn.clicked.connect(self._cancel)
        self._dl_btn = QPushButton("Download")
        self._dl_btn.setCursor(Qt.PointingHandCursor)
        self._dl_btn.setMinimumHeight(34)
        self._dl_btn.setStyleSheet(f"""
QPushButton {{ background:{T['accent']}; color:#0b1200; border:none; border-radius:7px;
              padding:6px 20px; font-size:13px; font-weight:800; }}
QPushButton:hover {{ background:{T['accent_hi']}; }}
QPushButton:disabled {{ background:#1c2230; color:#4a5263; }}
""")
        self._dl_btn.clicked.connect(self._start_download)
        for w in (self._open_btn, self._cancel_btn, self._dl_btn):
            r2.addWidget(w)
        d.addLayout(r2)
        lay.addWidget(dock)

        # ── activity (collapsible log) ──────────────────────────────────────
        act = QHBoxLayout()
        act.setSpacing(8)
        self._log_toggle = QToolButton()
        self._log_toggle.setCheckable(True)
        self._log_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._log_toggle.setStyleSheet(
            f"QToolButton{{background:transparent; border:none; color:{T['text2']}; "
            f"font-size:12px; font-weight:700;}} QToolButton:hover{{color:{T['text']};}}")
        self._log_toggle.toggled.connect(self._toggle_log)
        act.addWidget(self._log_toggle)
        self._last_line = ElidedLabel()
        self._last_line.set_copyable(False)
        self._last_line.setStyleSheet(f"color:{T['muted']}; font-family:monospace; font-size:11px;")
        act.addWidget(self._last_line, 1)
        lay.addLayout(act)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        self._log.setMinimumHeight(150)
        self._log.setStyleSheet(f"""
QPlainTextEdit {{ background:{T['bg']}; border:1px solid {T['border']}; border-left:2px solid
                 {T['accent_dim']}; border-radius:4px; color:{T['text2']}; font-family:monospace;
                 font-size:11px; padding:6px; }}
""")
        lay.addWidget(self._log)
        lay.addStretch(1)
        self._log_toggle.setChecked(self._qs.value("ngx/log_open", False, type=bool))
        self._toggle_log(self._log_toggle.isChecked())

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown)
        self._set_pill("not checked", T["muted"])
        self._set_busy(False)
        self._refresh_dest()

    # ── helpers ─────────────────────────────────────────────────────────────
    def _set_pill(self, text: str, color: str):
        self._pill.setText(f"●  {text}")
        self._pill.setStyleSheet(f"color:{color}; background:{_ngx_rgba(color, 0.10)}; "
                                 f"border:1px solid {_ngx_rgba(color, 0.45)}; border-radius:11px; "
                                 f"padding:3px 10px; font-size:11px; font-weight:700;")

    def _toggle_log(self, on: bool):
        self._log.setVisible(on)
        self._log_toggle.setText(("▾" if on else "▸") + "  Activity")
        self._qs.setValue("ngx/log_open", on)

    def _append(self, level: str, msg: str):
        color = self._LOG_COLORS.get(level, T["text2"])
        stamp = datetime.now().strftime("%H:%M:%S")
        safe = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._log.appendHtml(f'<span style="color:{T["muted"]}">{stamp}</span> '
                             f'<span style="color:{color}">{safe}</span>')
        self._last_line.setText(msg)
        self._last_line.setStyleSheet(f"color:{color if level != 'info' else T['muted']}; "
                                      f"font-family:monospace; font-size:11px;")
        if level == "err" and not self._log_toggle.isChecked():
            self._log_toggle.setChecked(True)

    def _set_busy(self, busy: bool):
        has_rows = bool(self._cards)
        self._check_btn.setEnabled(not busy)
        self._latest_btn.setEnabled(not busy and has_rows)
        self._clear_btn.setEnabled(not busy and has_rows)
        self._quick.setVisible(has_rows)
        for c in self._cards:
            c.setEnabled(not busy)
        self._cancel_btn.setVisible(busy)
        self._dl_btn.setEnabled(not busy and bool(self._checked()))
        self._open_btn.setEnabled(not busy and self._last_dest is not None
                                  and self._last_dest.is_dir())

    def _checked(self) -> List[NgxBuild]:
        return [b for b in (c.selected() for c in self._cards) if b is not None]

    def _auto_subfolder(self) -> str:
        sel = self._checked()
        dlss = [b.vid for b in sel if not ngx_is_sl(b.feature)]
        sl = [b.vid for b in sel if ngx_is_sl(b.feature)]
        pick = max(dlss) if dlss else (max(sl) if sl else None)
        return ngx_ver_str(pick) if pick is not None else ""

    def _dest(self) -> Optional[Path]:
        base = self._base.text().strip()
        sub = self._sub.text().strip().strip("/\\")
        if not base or any(p in ("..", ".") for p in re.split(r"[/\\]", sub) if p):
            return None
        p = Path(os.path.expanduser(base))
        return p / sub if sub else p

    def _refresh_dest(self, *_):
        self._sub.setReadOnly(self._auto.isChecked())
        if self._auto.isChecked():
            self._sub.blockSignals(True)
            self._sub.setText(self._auto_subfolder())
            self._sub.blockSignals(False)
        d = self._dest()
        self._dest_lbl.setText(f"→ {d}" if d else "→ (invalid output path)")
        sel = self._checked()
        self._dl_btn.setText(f"Download {len(sel)}  ·  {ngx_human(sum(b.size for b in sel))}"
                             if sel else "Download")
        self._set_busy(self._worker is not None and self._worker.isRunning())

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Base folder", self._base.text())
        if d:
            self._base.setText(d)

    def _select(self, latest: bool):
        for c in self._cards:
            c.set_pick(latest, newest=latest)
        self._refresh_dest()

    def _reflow(self):
        w = self._stack.width()
        cols = 1 if w < 620 else (2 if w < 1180 else 3)
        if cols == self._cols and all(self._grid.indexOf(c) >= 0 for c in self._cards):
            return
        self._cols = cols
        for c in self._cards:
            self._grid.removeWidget(c)
        for i, c in enumerate(self._cards):
            self._grid.addWidget(c, i // cols, i % cols)
        for i in range(3):
            self._grid.setColumnStretch(i, 1 if i < cols else 0)

    def resizeEvent(self, e):             # noqa: N802
        super().resizeEvent(e)
        if self._cards:
            self._reflow()

    def _populate(self, builds: List[NgxBuild]):
        for c in self._cards:
            self._grid.removeWidget(c)
            c.deleteLater()
        groups: Dict[str, List[NgxBuild]] = {}
        for b in builds:        # already sorted: feature order, newest first
            groups.setdefault(b.feature, []).append(b)
        self._cards = []
        for feat, lst in groups.items():
            card = _NgxFeatureCard(feat, lst)
            card.changed.connect(self._refresh_dest)
            self._cards.append(card)
        self._cols = 0
        self._stack.setCurrentIndex(1 if self._cards else 0)
        self._reflow()

    def _card(self, feature: str) -> Optional[_NgxFeatureCard]:
        return next((c for c in self._cards if c.feature == feature), None)

    # ── actions ─────────────────────────────────────────────────────────────
    def _spawn(self, worker: _NgxWorker):
        self._worker = worker
        worker.log.connect(self._append)
        worker.progress.connect(self._on_progress)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_finished)
        self._set_busy(True)
        worker.start()

    def _on_progress(self, frac: float, label: str):
        self._bar.setValue(int(frac * 1000))
        self._bar_lbl.setText(f"{label}  ·  {int(frac * 100)}%")

    def _on_failed(self, msg: str):
        cancelled = msg == "cancelled"
        self._append("warn" if cancelled else "err", f"stopped: {msg}")
        self._status.setText(msg)
        self._bar_lbl.setText(msg)
        if not self._cards:
            self._set_pill("cancelled" if cancelled else "check failed",
                           T["warn"] if cancelled else T["danger"])

    def _on_card_state(self, feature: str, state: str):
        card = self._card(feature)
        if card is not None:
            card.set_state(*{"run": ("downloading…", T["text"]),
                             "ok": ("✓ saved", T["accent_hi"]),
                             "err": ("✗ failed", T["danger"])}.get(state, ("", "")))

    def _on_finished(self):
        w, self._worker = self._worker, None
        if w is not None:
            w.deleteLater()
        self._set_busy(False)

    def _start_check(self):
        if self._worker is not None:
            return
        self._set_pill("checking…", T["warn"])
        self._status.setText("")
        self._bar_lbl.setText("listing ngx.download.nvidia.com …")
        self._bar.setRange(0, 0)              # busy indicator while listing
        w = _NgxWorker(parent=self)
        w.discovered.connect(self._on_discovered)
        w.finished.connect(lambda: (self._bar.setRange(0, 1000), self._bar.setValue(0)))
        self._spawn(w)

    def _on_discovered(self, builds: List[NgxBuild]):
        self._populate(builds)
        feats = len({b.feature for b in builds})
        self._set_pill(f"{len(builds)} builds · {feats} features · "
                       f"{datetime.now().strftime('%H:%M')}", T["accent"])
        self._status.setText(f"{feats} cards · newest of each pre-selected")
        self._bar_lbl.setText("")
        self._append("ok", f"found {len(builds)} builds across {feats} features")
        self._refresh_dest()

    def _start_download(self):
        if self._worker is not None:
            return
        sel = self._checked()
        dest = self._dest()
        if not sel:
            return
        if dest is None:
            QMessageBox.warning(self, "DLSS Updater", "Output path is empty or contains '..'.")
            return
        feats = [b.feature for b in sel]
        if len(feats) != len(set(feats)) or sum(ngx_is_sl(f) for f in feats) > 1:
            QMessageBox.warning(self, "DLSS Updater",
                                "Two selected builds would write the same file names into "
                                f"{dest}.\nPick one version per feature, or download them "
                                "separately into different subfolders.")
            return
        self._qs.setValue("ngx/base_dir", self._base.text().strip())
        self._last_dest = dest
        self._bar.setValue(0)
        for c in self._cards:
            c.set_state("queued" if c.selected() else "", T["muted"])
        self._set_pill("downloading…", T["warn"])
        self._append("info", f"saving to {dest}")
        w = _NgxWorker(sel, dest, parent=self)
        w.downloaded.connect(self._on_downloaded)
        w.state.connect(self._on_card_state)
        self._spawn(w)

    def _on_downloaded(self, ok: int, bad: int, dest: str):
        text = f"{ok} downloaded" + (f", {bad} failed" if bad else "")
        self._status.setText(text)
        self._bar_lbl.setText(f"{text} → {dest}")
        self._set_pill(text, T["danger"] if bad and not ok else (T["warn"] if bad else T["accent"]))
        self._append("ok" if not bad else "warn", f"finished: {ok} ok, {bad} failed → {dest}")

    def _cancel(self):
        if self._worker is not None:
            self._worker.cancel()

    def _open_dest(self):
        if self._last_dest is not None and self._last_dest.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_dest)))

    def _shutdown(self):
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(5000)


class ExtraToolsWidget(QWidget):
    """
    Container that holds both sub-tools (vk_flip_meter build panel and
    lutris-game-tune settings/installer) as QTabWidget sub-tabs inside the
    Extra Tools top-level tab.
    """
    # Shared QTabWidget stylesheet (same look as the Profiles subtabs)
    _TAB_SS = """
QTabWidget::pane { border: 1px solid #1e2535; top: -1px; }
QTabBar::tab {
    background: #141720; color: #a8adb8;
    border: 1px solid #1e2535; border-bottom: none;
    padding: 4px 14px; font-size:13px; font-weight: 600;
}
QTabBar::tab:selected { background: #1a1f2e; color: #e8eaf0; border-color: #4a7300; }
QTabBar::tab:hover { color: #c8cdd8; }
"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        tabs = QTabWidget()
        tabs.setStyleSheet(self._TAB_SS)

        # ── vk_flip_meter sub-tab ────────────────────────────────────────────
        self._flm_install = FlmInstallWidget()
        flm_scroll = QScrollArea()
        flm_scroll.setWidgetResizable(True)
        flm_scroll.setWidget(self._flm_install)
        flm_scroll.setStyleSheet(SCROLLABLE_CONTROL_QSS)
        tabs.addTab(flm_scroll, "vk_flip_meter")

        # ── lutris-game-tune sub-tab ─────────────────────────────────────────
        self._lgtune = LgtuneWidget()
        tabs.addTab(self._lgtune, "lutris-game-tune")

        # ── DLSS Updater sub-tab (NGX OTA DLSS / Streamline downloads) ────────
        self._ngx = NgxUpdaterWidget()
        ngx_scroll = QScrollArea()
        ngx_scroll.setWidgetResizable(True)
        ngx_scroll.setWidget(self._ngx)
        ngx_scroll.setStyleSheet(SCROLLABLE_CONTROL_QSS)
        tabs.addTab(ngx_scroll, "DLSS Updater")

        # ── feature pack: linter / system / prefix doctor / overlays ──────────
        self._tabs = tabs
        self.linter = LinterWidget()
        tabs.addTab(self.linter, "Linter")
        self.sysinfo = SystemInfoWidget()
        tabs.addTab(self.sysinfo, "System")
        self.prefix_doctor = PrefixDoctorWidget()
        tabs.addTab(self.prefix_doctor, "Prefix Doctor")
        self.mangohud = MangoHudBuilder()
        tabs.addTab(self.mangohud, "MangoHud")
        self.vkbasalt = VkBasaltBuilder()
        tabs.addTab(self.vkbasalt, "vkBasalt")
        self.cache = CacheManagerWidget()
        tabs.addTab(self.cache, "Cache")
        self.cache.stale_changed.connect(
            lambda stale: tabs.setTabText(tabs.indexOf(self.cache), "Cache ⚠" if stale else "Cache"))
        self.drift = UpstreamDriftWidget()
        tabs.addTab(self.drift, "Upstream drift")
        self.hints = GameHintsWidget()
        tabs.addTab(self.hints, "Game hints")
        self.frametime = FrametimeWidget()
        tabs.addTab(self.frametime, "Frametime")
        self.live = LiveProcessWidget()
        tabs.addTab(self.live, "Live process")
        self.logview = LogViewerWidget()
        tabs.addTab(self.logview, "Logs")

        layout.addWidget(tabs)

    def show_tab(self, title: str) -> bool:
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == title:
                self._tabs.setCurrentIndex(i)
                return True
        return False


# ============================================================================
# Feature pack: favorites, system probe, availability greying, consistency
# linter.  (Everything below is self-contained and only touches the rest of
# the program through the small hooks in the list widgets / MainWindow.)
# ============================================================================

import subprocess

ROLE_FAV = Qt.UserRole + 14       # bool: item is a favorite
ROLE_UNAVAIL = Qt.UserRole + 15   # str: why the item is unsupported ("" = fine)


# ----------------------------------------------------------------------------
# Favorites (persisted in QSettings, shared by DRS / Env / Arch lists)
# ----------------------------------------------------------------------------

class FavoritesStore(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self._data: Optional[Dict[str, List[str]]] = None

    def _load(self) -> Dict[str, List[str]]:
        if self._data is None:
            self._data = {}
            try:
                qs = QSettings(APP_ID, APP_ID)
                for kind in ("drs", "env", "arch"):
                    v = qs.value(f"favorites/{kind}", [])
                    if isinstance(v, str):
                        v = [v] if v else []
                    self._data[kind] = [str(x) for x in (v or [])]
            except Exception:
                self._data = {}
        return self._data

    def _save(self):
        try:
            qs = QSettings(APP_ID, APP_ID)
            for kind, lst in self._load().items():
                qs.setValue(f"favorites/{kind}", list(lst))
        except Exception:
            pass

    def is_fav(self, kind: str, key: str) -> bool:
        return key in self._load().get(kind, [])

    def all(self, kind: str) -> List[str]:
        return list(self._load().get(kind, []))

    def toggle(self, kind: str, key: str) -> bool:
        lst = self._load().setdefault(kind, [])
        if key in lst:
            lst.remove(key)
            now = False
        else:
            lst.append(key)
            now = True
        self._save()
        self.changed.emit()
        return now


FAVS = FavoritesStore()


def install_fav_menu(lw: QListWidget, kind: str, key_of=None) -> None:
    """Right-click -> add/remove favorite on any sidebar list item."""
    lw.setContextMenuPolicy(Qt.CustomContextMenu)

    def _menu(pos):
        item = lw.itemAt(pos)
        if item is None or item.data(ROLE_KIND) == "header":
            return
        key = item.data(Qt.UserRole)
        if key_of is not None:
            key = key_of(key)
        if not key or (isinstance(key, str) and key.startswith("__")):
            return
        m = QMenu(lw)
        a = m.addAction("Remove from favorites" if FAVS.is_fav(kind, key)
                        else "★  Add to favorites   (Ctrl+D)")
        if m.exec(lw.viewport().mapToGlobal(pos)) is a:
            FAVS.toggle(kind, key)

    lw.customContextMenuRequested.connect(_menu)


# ----------------------------------------------------------------------------
# System probe: NVIDIA driver version + Vulkan device extensions
# ----------------------------------------------------------------------------

@dataclass
class VkGpu:
    name: str = ""
    vendor_id: int = 0
    driver: str = ""
    exts: set = field(default_factory=set)

    @property
    def is_nvidia(self) -> bool:
        return self.vendor_id == 0x10DE or "nvidia" in self.name.lower()


@dataclass
class SysInfo:
    probed: bool = False
    nv_driver: str = ""               # "595.58.03" or ""
    nv_kernel_flavor: str = ""        # "Open Kernel Module" / "proprietary" / ""
    nv_gpus: List[str] = field(default_factory=list)
    vk_ok: bool = False               # vulkaninfo ran and produced devices
    vk_gpus: List[VkGpu] = field(default_factory=list)
    vk_instance_exts: set = field(default_factory=set)
    vulkaninfo_error: str = ""
    session: str = ""
    ntsync_dev: bool = False
    tools: Dict[str, str] = field(default_factory=dict)
    raw_version: str = ""

    @property
    def nv_major(self) -> int:
        m = re.match(r"(\d+)", self.nv_driver or "")
        return int(m.group(1)) if m else 0

    @property
    def has_nvidia(self) -> bool:
        return bool(self.nv_driver) or any(g.is_nvidia for g in self.vk_gpus)

    @property
    def nvidia_known_absent(self) -> bool:
        """Probe finished and positively found no NVIDIA anything."""
        return self.probed and self.vk_ok and not self.has_nvidia

    def device_exts(self, nvidia_first: bool = True) -> set:
        """Extensions of the NVIDIA device if there is one, else the union."""
        if nvidia_first:
            nv = [g for g in self.vk_gpus if g.is_nvidia]
            if nv:
                out = set()
                for g in nv:
                    out |= g.exts
                return out
        out = set()
        for g in self.vk_gpus:
            out |= g.exts
        return out

    def has_ext(self, ext: str) -> Optional[bool]:
        """True/False if known, None if the probe could not tell."""
        if not self.vk_ok:
            return None
        return ext in self.device_exts() or ext in self.vk_instance_exts


def _run(cmd: List[str], timeout: float = 15.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LC_ALL": "C"})
        return r.stdout or ""
    except Exception:
        return ""


def parse_vulkaninfo(text: str):
    """-> (instance_exts:set, [VkGpu]).  Tolerant line parser for `vulkaninfo`."""
    ext_re = re.compile(r"^\s*(VK_[A-Za-z0-9_]+)\s*:\s*extension revision", re.M)
    # split into the instance section and per-GPU sections
    parts = re.split(r"^GPU(\d+):\s*$", text, flags=re.M)
    head = parts[0]
    inst = set(ext_re.findall(head))
    gpus: List[VkGpu] = []
    for i in range(1, len(parts) - 1, 2):
        body = parts[i + 1]
        g = VkGpu()
        m = re.search(r"deviceName\s*=\s*(.+)", body)
        if m:
            g.name = m.group(1).strip()
        m = re.search(r"vendorID\s*=\s*(0x[0-9a-fA-F]+|\d+)", body)
        if m:
            try:
                g.vendor_id = int(m.group(1), 0)
            except ValueError:
                pass
        m = re.search(r"driverInfo\s*=\s*(.+)", body)
        if m:
            g.driver = m.group(1).strip()
        g.exts = set(ext_re.findall(body))
        gpus.append(g)
    return inst, gpus


def probe_system() -> SysInfo:
    si = SysInfo()
    # --- NVIDIA kernel/driver version -------------------------------------
    try:
        raw = Path("/proc/driver/nvidia/version").read_text(errors="replace")
        si.raw_version = raw.strip()
        m = re.search(r"NVRM version:\s*(.*?)\s+(\d+\.\d+(?:\.\d+)?)\s", raw)
        if m:
            si.nv_driver = m.group(2)
            si.nv_kernel_flavor = "Open Kernel Module" if "Open" in m.group(1) else "proprietary"
    except OSError:
        pass
    smi = shutil.which("nvidia-smi")
    if smi:
        out = _run([smi, "--query-gpu=driver_version,name", "--format=csv,noheader"], 8)
        for ln in out.splitlines():
            if "," in ln:
                ver, name = [x.strip() for x in ln.split(",", 1)]
                si.nv_gpus.append(name)
                if not si.nv_driver and re.match(r"\d+\.\d+", ver):
                    si.nv_driver = ver
    # --- Vulkan -------------------------------------------------------------
    vi = shutil.which("vulkaninfo")
    if vi:
        out = _run([vi], 25)
        inst, gpus = parse_vulkaninfo(out)
        if gpus:
            si.vk_gpus, si.vk_instance_exts, si.vk_ok = gpus, inst, True
        else:
            si.vulkaninfo_error = "vulkaninfo produced no device list"
    else:
        si.vulkaninfo_error = "vulkaninfo not installed (dev-util/vulkan-tools)"
    # --- misc ---------------------------------------------------------------
    si.session = os.environ.get("XDG_SESSION_TYPE", "") or (
        "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11" if os.environ.get("DISPLAY") else "")
    si.ntsync_dev = Path("/dev/ntsync").exists()
    for t in ("gamescope", "mangohud", "vulkaninfo", "nvidia-smi", "wine", "zstd"):
        p = shutil.which(t)
        if p:
            si.tools[t] = p
    si.probed = True
    return si


class _ProbeThread(QThread):
    done = Signal(object)

    def run(self):
        try:
            self.done.emit(probe_system())
        except Exception as e:                       # never kill the GUI for a probe
            si = SysInfo(probed=True)
            si.vulkaninfo_error = f"probe failed: {e}"
            self.done.emit(si)


class SysHub(QObject):
    """Holds the latest SysInfo and re-probes in the background."""
    info_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.info = SysInfo()
        self.enabled = True       # "grey out unsupported options"
        self._thread: Optional[_ProbeThread] = None
        try:
            self.enabled = QSettings(APP_ID, APP_ID).value("sys/grey_unsupported", True, type=bool)
        except Exception:
            pass

    def set_enabled(self, on: bool):
        self.enabled = on
        try:
            QSettings(APP_ID, APP_ID).setValue("sys/grey_unsupported", on)
        except Exception:
            pass
        self.info_changed.emit(self.info)

    def refresh(self):
        if self._thread is not None and self._thread.isRunning():
            return
        self._thread = _ProbeThread()
        self._thread.done.connect(self._on_done)
        self._thread.start()

    def _on_done(self, info):
        self.info = info
        self.info_changed.emit(info)

    def shutdown(self):
        if self._thread is not None and self._thread.isRunning():
            self._thread.wait(30000)


SYSHUB = SysHub()


# ----------------------------------------------------------------------------
# Availability rules ("why is this greyed out?").  Deliberately conservative:
# an option is only flagged when the probe POSITIVELY says it cannot work.
# Unknown probe data never greys anything out.
# ----------------------------------------------------------------------------

_NV_ONLY_PREFIXES = ("__GL_", "__NV_", "DXVK_NVAPI_", "NVPRESENT_", "PROTON_NVIDIA_",
                     "DISABLE_DXVK_NVAPI_", "__GLX_VENDOR")
_NV_ONLY_NAMES = {"PROTON_ENABLE_NVAPI", "PROTON_DISABLE_NVAPI", "PROTON_FORCE_NVAPI",
                  "PROTON_HIDE_NVIDIA_GPU", "PROTON_NVAPI_BYPASS", "PROTON_ENABLE_NGX_UPDATER",
                  "PROTON_DLSS_UPGRADE", "PROTON_DLSS_INDICATOR", "DXVK_ENABLE_NVAPI"}

# env var / dxvk.conf key / vkd3d flag  ->  required Vulkan extension
_EXT_REQ_ENV = {"DXVK_NVAPI_VKREFLEX": "VK_NV_low_latency2"}
_EXT_REQ_DXVK_CFG = {
    "dxvk.enableDescriptorHeap": "VK_EXT_descriptor_heap",
    "dxvk.enableDescriptorBuffer": "VK_EXT_descriptor_buffer",
}
_EXT_REQ_VKD3D_FLAG = {"descriptor_heap": "VK_EXT_descriptor_heap"}


def _sys_active() -> Optional[SysInfo]:
    return SYSHUB.info if (SYSHUB.enabled and SYSHUB.info.probed) else None


def _need_ext(si: SysInfo, ext: str) -> str:
    if si.has_ext(ext) is False:
        return f"{ext} is not exposed by this system's Vulkan driver"
    return ""


def env_unavailable(ev: "EnvVarDef") -> str:
    si = _sys_active()
    if si is None:
        return ""
    n = ev.name
    if (n in _NV_ONLY_NAMES or n.startswith(_NV_ONLY_PREFIXES)) and si.nvidia_known_absent:
        return "No NVIDIA driver / GPU detected on this system"
    ext = _EXT_REQ_ENV.get(n)
    return _need_ext(si, ext) if ext else ""


def drs_unavailable(s: "Setting") -> str:
    si = _sys_active()
    if si is None:
        return ""
    if si.nvidia_known_absent:
        return "DRS settings are read by dxvk-nvapi from the NVIDIA driver — none detected here"
    return ""


def vkd3d_flag_unavailable(flag: str) -> str:
    si = _sys_active()
    ext = _EXT_REQ_VKD3D_FLAG.get(flag)
    return _need_ext(si, ext) if (si and ext) else ""


def dxvk_cfg_unavailable(key: str) -> str:
    si = _sys_active()
    ext = _EXT_REQ_DXVK_CFG.get(key)
    return _need_ext(si, ext) if (si and ext) else ""


def unavail_banner(why: str, on_override=None) -> QWidget:
    """Amber note shown on top of an editor whose option the system can't run."""
    box = QFrame()
    box.setStyleSheet("QFrame{background:rgba(210,160,60,0.10);border:1px solid %s;"
                      "border-radius:5px;}" % T['warn'])
    lay = QHBoxLayout(box)
    lay.setContentsMargins(10, 6, 10, 6)
    lbl = QLabel(f"⚠  Not supported here: {why}")
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color:{T['warn']}; font-size:12px; border:none; background:transparent;")
    lay.addWidget(lbl, 1)
    if on_override is not None:
        b = QPushButton("Edit anyway")
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(f"QPushButton{{color:{T['text']};background:{T['raised']};"
                        f"border:1px solid {T['border_hi']};border-radius:4px;padding:2px 10px;}}"
                        f"QPushButton:hover{{border-color:{T['warn']};}}")
        b.clicked.connect(lambda: (on_override(), box.hide()))
        lay.addWidget(b)
    return box


# ----------------------------------------------------------------------------
# Consistency linter
# ----------------------------------------------------------------------------

@dataclass
class LintIssue:
    sev: str                       # "error" | "warn" | "info"
    code: str
    msg: str
    involved: List[str] = field(default_factory=list)
    # list of ("env_unset", NAME) | ("env_set", NAME, VALUE) | ("drs_unset", ID)
    # | ("gamescope_off",)
    fixes: List[tuple] = field(default_factory=list)
    fix_label: str = "Fix"


_FALSY = {"", "0", "false", "no", "off", "none"}


def _on(env: Dict[str, str], name: str) -> bool:
    return name in env and str(env[name]).strip().lower() not in _FALSY


def _driver_tuple(v: str):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
    except Exception:
        return ()


def lint_config(env: Dict[str, str], drs: Dict[str, str], arch_code: str,
                gs_on: bool, gs_flags: Dict[str, str], mangohud: bool,
                si: Optional[SysInfo] = None) -> List[LintIssue]:
    """Pure function: configuration in, list of issues out (unit-testable)."""
    si = si or SysInfo()
    out: List[LintIssue] = []

    def add(sev, code, msg, involved=(), fixes=(), label="Fix"):
        out.append(LintIssue(sev, code, msg, list(involved), list(fixes), label))

    def unset(*names):
        return [("env_unset", n) for n in names]

    nvapi_cfg = bool(arch_code or drs or any(k.startswith("DXVK_NVAPI_") for k in env))

    # Does the user steer rendering to a non-NVIDIA device?
    flt = (env.get("DXVK_FILTER_DEVICE_NAME", "") + " " + env.get("VKD3D_FILTER_DEVICE_NAME", "")
           + " " + env.get("VKD3D_VULKAN_DEVICE", "")).lower()
    amd_target = bool(re.search(r"amd|radeon|rdna|intel|arc\b", flt)) or "DRI_PRIME" in env and not _on(env, "__NV_PRIME_RENDER_OFFLOAD")
    nv_render = (si.has_nvidia or nvapi_cfg) and not amd_target

    # 1. PROTON_NVIDIA_LIBS + WoW64
    if _on(env, "PROTON_NVIDIA_LIBS") and _on(env, "PROTON_USE_WOW64"):
        sev = "info" if _on(env, "PROTON_NVIDIA_LIBS_NO_32BIT") else "error"
        add(sev, "nvlibs-wow64",
            "PROTON_NVIDIA_LIBS is incompatible with PROTON_USE_WOW64 (the 32-bit nvidia-libs "
            "shims have no place to load in a WoW64 process)."
            + (" PROTON_NVIDIA_LIBS_NO_32BIT is set, so only the 64-bit shims load — likely OK."
               if sev == "info" else ""),
            ["PROTON_NVIDIA_LIBS", "PROTON_USE_WOW64"],
            unset("PROTON_USE_WOW64") if sev == "error" else [], "Unset WOW64")
        if sev == "error":
            out[-1].fixes = unset("PROTON_USE_WOW64")
            add("info", "nvlibs-wow64-alt", "Alternative: keep WoW64 and drop the NVIDIA shims instead.",
                ["PROTON_NVIDIA_LIBS"], unset("PROTON_NVIDIA_LIBS", "PROTON_NVIDIA_LIBS_NO_32BIT"),
                "Drop shims")
    # 2. Sub-switches that do nothing without the master
    if _on(env, "PROTON_NVIDIA_LIBS_NO_32BIT") and not _on(env, "PROTON_NVIDIA_LIBS"):
        add("warn", "nvlibs-no32-orphan",
            "PROTON_NVIDIA_LIBS_NO_32BIT only restricts PROTON_NVIDIA_LIBS — which is not enabled.",
            ["PROTON_NVIDIA_LIBS_NO_32BIT"], unset("PROTON_NVIDIA_LIBS_NO_32BIT"))
    # 3. FSR4 on NVIDIA
    fsr4 = [n for n in ("PROTON_FSR4_UPGRADE", "PROTON_FSR4_INDICATOR", "PROTON_FFX4_UPGRADE")
            if _on(env, n)]
    if fsr4 and nv_render:
        add("warn", "fsr4-nvidia",
            "FSR 4 needs an AMD RDNA3/4 GPU; on the NVIDIA GPU these variables do nothing. "
            "Use PROTON_DLSS_UPGRADE instead (or point the game at the AMD iGPU with "
            "DXVK_FILTER_DEVICE_NAME).", fsr4, unset(*fsr4), "Unset FSR4")
    # 4. HDR WSI layer + new drivers
    drv = _driver_tuple(si.nv_driver)
    if _on(env, "ENABLE_HDR_WSI") and drv and drv[0] >= 595 and not amd_target:
        add("warn", "hdr-wsi-595",
            f"ENABLE_HDR_WSI is only needed on NVIDIA drivers older than 595; this system runs "
            f"{si.nv_driver}, which has native HDR WSI — the extra layer is redundant and can "
            f"misbehave.", ["ENABLE_HDR_WSI"], unset("ENABLE_HDR_WSI"), "Unset")
    if _on(env, "ENABLE_HDR_WSI") and drv and drv[0] < 595 and not (
            _on(env, "DXVK_HDR") or _on(env, "PROTON_ENABLE_HDR")):
        add("info", "hdr-wsi-orphan",
            "ENABLE_HDR_WSI has no visible effect without DXVK_HDR=1 / PROTON_ENABLE_HDR=1.",
            ["ENABLE_HDR_WSI"])
    # 5. HDR without a compositor path
    hdr_on = _on(env, "DXVK_HDR") or _on(env, "PROTON_ENABLE_HDR")
    if hdr_on:
        if _on(env, "DXVK_NO_HDR"):
            add("error", "hdr-contradiction", "DXVK_HDR and DXVK_NO_HDR are both set.",
                ["DXVK_HDR", "DXVK_NO_HDR"], unset("DXVK_NO_HDR"), "Unset NO_HDR")
        if not gs_on and not _on(env, "PROTON_ENABLE_WAYLAND") and si.session != "x11":
            add("warn", "hdr-no-path",
                "HDR output needs the Wayland driver (PROTON_ENABLE_WAYLAND=1) or gamescope with "
                "--hdr-enabled; neither is configured.", ["PROTON_ENABLE_HDR", "DXVK_HDR"],
                [("env_set", "PROTON_ENABLE_WAYLAND", "1")], "Enable Wayland")
        if gs_on and "--hdr-enabled" not in gs_flags:
            add("warn", "hdr-gs-flag", "HDR is requested but gamescope runs without --hdr-enabled.",
                ["--hdr-enabled"])
    # 6. Wayland vs forced X11
    if _on(env, "PROTON_ENABLE_WAYLAND") and env.get("SDL_VIDEODRIVER", "").lower() == "x11":
        add("warn", "wayland-vs-x11", "PROTON_ENABLE_WAYLAND=1 contradicts SDL_VIDEODRIVER=x11.",
            ["PROTON_ENABLE_WAYLAND", "SDL_VIDEODRIVER"], unset("SDL_VIDEODRIVER"), "Unset SDL_VIDEODRIVER")
    # 7. WineD3D replaces DXVK/VKD3D
    if _on(env, "PROTON_USE_WINED3D"):
        dead = [k for k in env if k.startswith(("DXVK_", "VKD3D_")) and k != "DXVK_NVAPI_DRS_SETTINGS"
                and not k.startswith("DXVK_NVAPI_")]
        if dead or nvapi_cfg:
            add("warn", "wined3d-dxvk",
                "PROTON_USE_WINED3D=1 replaces DXVK/VKD3D-Proton, so DXVK_*/VKD3D_*/NVAPI settings "
                "have no effect.", ["PROTON_USE_WINED3D"] + dead[:6], unset("PROTON_USE_WINED3D"),
                "Unset WINED3D")
    if _on(env, "PROTON_NO_D3D12") and any(k.startswith("VKD3D_") for k in env):
        add("info", "no-d3d12-vkd3d", "PROTON_NO_D3D12 disables VKD3D-Proton; VKD3D_* variables are unused.",
            ["PROTON_NO_D3D12"])
    if _on(env, "PROTON_NO_D3D11") and any(k.startswith("DXVK_") and not k.startswith("DXVK_NVAPI")
                                           for k in env):
        add("info", "no-d3d11-dxvk", "PROTON_NO_D3D11 is set; DXVK_* variables only apply to the "
            "remaining D3D8/9/10 paths.", ["PROTON_NO_D3D11"])
    # 8. NVAPI switches
    if _on(env, "PROTON_ENABLE_NVAPI") and _on(env, "PROTON_DISABLE_NVAPI"):
        add("error", "nvapi-both", "PROTON_ENABLE_NVAPI and PROTON_DISABLE_NVAPI are both set.",
            ["PROTON_ENABLE_NVAPI", "PROTON_DISABLE_NVAPI"], unset("PROTON_DISABLE_NVAPI"), "Unset DISABLE")
    if _on(env, "PROTON_ENABLE_NVAPI") and _on(env, "PROTON_HIDE_NVIDIA_GPU"):
        add("warn", "nvapi-hidden",
            "NVAPI is enabled but PROTON_HIDE_NVIDIA_GPU=1 reports the NVIDIA GPU as AMD — "
            "NVAPI/DLSS will not find it. Set PROTON_HIDE_NVIDIA_GPU=0.",
            ["PROTON_ENABLE_NVAPI", "PROTON_HIDE_NVIDIA_GPU"],
            [("env_set", "PROTON_HIDE_NVIDIA_GPU", "0")], "Set HIDE=0")
    if _on(env, "DXVK_ENABLE_NVAPI") is False and "DXVK_ENABLE_NVAPI" in env and nvapi_cfg:
        add("warn", "nvapi-hack-off",
            "DXVK_ENABLE_NVAPI=0 turns DXVK's NVAPI GPU spoof off, but DRS/arch settings expect "
            "dxvk-nvapi to be active.", ["DXVK_ENABLE_NVAPI"])
    # 9. Reflex / latency stacks
    if _on(env, "DXVK_NVAPI_VKREFLEX") and _on(env, "DISABLE_DXVK_NVAPI_VKREFLEX"):
        add("error", "vkreflex-both", "DXVK_NVAPI_VKREFLEX and DISABLE_DXVK_NVAPI_VKREFLEX are both set.",
            ["DXVK_NVAPI_VKREFLEX", "DISABLE_DXVK_NVAPI_VKREFLEX"],
            unset("DISABLE_DXVK_NVAPI_VKREFLEX"), "Unset DISABLE")
    lat = [n for n in ("DXVK_NVAPI_VKREFLEX", "LOW_LATENCY_LAYER", "PROTON_DXVK_LOWLATENCY",
                       "PROTON_VKD3D_LOWLATENCY") if _on(env, n)]
    if len(lat) >= 2:
        add("warn", "latency-stack",
            "Several latency-reduction paths are active at once (" + ", ".join(lat) +
            "); they all drive VK_NV_low_latency2 and can fight each other. Keep one.", lat)
    # 10. Sync primitives
    if _on(env, "PROTON_USE_NTSYNC") and _on(env, "PROTON_NO_NTSYNC"):
        add("error", "ntsync-both", "PROTON_USE_NTSYNC and PROTON_NO_NTSYNC are both set.",
            ["PROTON_USE_NTSYNC", "PROTON_NO_NTSYNC"], unset("PROTON_NO_NTSYNC"), "Unset NO_NTSYNC")
    if _on(env, "PROTON_USE_NTSYNC") and si.probed and not si.ntsync_dev:
        add("warn", "ntsync-missing",
            "PROTON_USE_NTSYNC=1 but /dev/ntsync does not exist (modprobe ntsync, kernel ≥ 6.14).",
            ["PROTON_USE_NTSYNC"])
    # 11. PRIME / device selection
    if _on(env, "__NV_PRIME_RENDER_OFFLOAD") and "DRI_PRIME" in env:
        add("warn", "prime-mixed", "__NV_PRIME_RENDER_OFFLOAD (NVIDIA) and DRI_PRIME (Mesa) are both set; "
            "use only the one that matches the GPU you want.", ["__NV_PRIME_RENDER_OFFLOAD", "DRI_PRIME"])
    if re.search(r"amd|radeon", flt) and nvapi_cfg:
        add("warn", "filter-amd-nvapi",
            "A DXVK/VKD3D device filter selects an AMD GPU, but DRS/arch/NVAPI settings only mean "
            "something on the NVIDIA GPU.", ["DXVK_FILTER_DEVICE_NAME"])
    # 12. Frame limiters
    limiters = {n: env[n] for n in ("DXVK_FRAME_RATE", "VKD3D_FRAME_RATE") if _on(env, n)}
    if "--framerate-limit" in gs_flags:
        limiters["gamescope --framerate-limit"] = gs_flags["--framerate-limit"]
    if len(limiters) >= 2:
        add("info", "multi-limiter", "More than one frame limiter is active ("
            + ", ".join(f"{k}={v}" for k, v in limiters.items()) +
            "); the lowest wins and pacing can get uneven.", list(limiters))
    # 13. Async shader patch
    if _on(env, "DXVK_ASYNC") or _on(env, "PROTON_DXVK_LLASYNC"):
        add("info", "async-gpl",
            "DXVK_ASYNC / PROTON_DXVK_LLASYNC only exist in dxvk-async/GE-style builds; upstream "
            "DXVK 2.x uses Graphics Pipeline Libraries instead and ignores them.",
            [n for n in ("DXVK_ASYNC", "PROTON_DXVK_LLASYNC") if n in env])
    # 14. OptiScaler vs individual upgrades
    ups = [n for n in ("PROTON_DLSS_UPGRADE", "PROTON_FSR3_UPGRADE", "PROTON_FSR4_UPGRADE",
                       "PROTON_XESS_UPGRADE", "PROTON_FFX3_UPGRADE", "PROTON_FFX4_UPGRADE") if _on(env, n)]
    if _on(env, "PROTON_USE_OPTISCALER") and ups:
        add("info", "optiscaler-upgrades",
            "With OptiScaler the PROTON_*_UPGRADE variables choose the DLL versions it injects; "
            "make sure they are the upscalers you actually want OptiScaler to swap.", ups)
    # 15. Gamescope WSI switches
    if _on(env, "ENABLE_GAMESCOPE_WSI") and _on(env, "DISABLE_GAMESCOPE_WSI"):
        add("error", "gswsi-both", "ENABLE_GAMESCOPE_WSI and DISABLE_GAMESCOPE_WSI are both set.",
            ["ENABLE_GAMESCOPE_WSI", "DISABLE_GAMESCOPE_WSI"], unset("DISABLE_GAMESCOPE_WSI"), "Unset DISABLE")
    gs_env = [k for k in env if k.startswith(("STEAM_GAMESCOPE_", "GAMESCOPE_"))]
    if gs_env and not gs_on:
        add("info", "gs-env-no-gs", "Gamescope-specific variables are set but gamescope itself is "
            "disabled in the Gamescope builder.", gs_env[:5])
    # 16. Overlays
    if mangohud and not gs_on:
        pass  # (the checkbox is greyed without gamescope, nothing to flag)
    if mangohud and _on(env, "MANGOHUD"):
        add("info", "mangohud-twice", "MangoHud is added both as a command prefix and via MANGOHUD=1 "
            "— two HUDs may draw on top of each other.", ["MANGOHUD"], unset("MANGOHUD"), "Unset MANGOHUD")
    if _on(env, "DXVK_HUD") and (mangohud or _on(env, "MANGOHUD")):
        add("info", "hud-twice", "DXVK_HUD and MangoHud are both active — you will see two overlays.",
            ["DXVK_HUD"])
    # 17. Vulkan feature requirements (needs probe)
    if si.vk_ok:
        if _on(env, "DXVK_NVAPI_VKREFLEX") and si.has_ext("VK_NV_low_latency2") is False:
            add("warn", "no-ll2", "DXVK_NVAPI_VKREFLEX needs VK_NV_low_latency2, which this driver "
                "does not expose.", ["DXVK_NVAPI_VKREFLEX"], unset("DXVK_NVAPI_VKREFLEX"), "Unset")
        flags = [f.strip() for f in env.get("VKD3D_CONFIG", "").split(",") if f.strip()]
        if "descriptor_heap" in flags and si.has_ext("VK_EXT_descriptor_heap") is False:
            add("warn", "no-descheap", "VKD3D_CONFIG=descriptor_heap needs VK_EXT_descriptor_heap, "
                "which this driver does not expose.", ["VKD3D_CONFIG"],
                [("env_set", "VKD3D_CONFIG", ",".join(f for f in flags if f != "descriptor_heap"))],
                "Remove flag")
    # 18. nvidia-only vars with no nvidia
    if si.nvidia_known_absent and (nvapi_cfg or any(k.startswith(("__GL_", "PROTON_NVIDIA_")) for k in env)):
        add("info", "no-nvidia", "No NVIDIA driver was detected on this machine; NVIDIA-specific "
            "settings only matter if this configuration is for another PC.", [])
    order = {"error": 0, "warn": 1, "info": 2}
    out.sort(key=lambda i: order.get(i.sev, 3))
    return out


def _set_layout_enabled(layout, enabled: bool, skip: int = 0) -> None:
    """Enable/disable every widget in a layout tree, leaving the first `skip` items alone."""
    for i in range(layout.count()):
        if i < skip:
            continue
        it = layout.itemAt(i)
        if it is None:
            continue
        w = it.widget()
        if w is not None:
            w.setEnabled(enabled)
        elif it.layout() is not None:
            _set_layout_enabled(it.layout(), enabled)


# ============================================================================
# Runnable launch line + Steam / Heroic / Bottles sync
# ============================================================================

def build_launch_line(payload: dict, command: str = "%command%", use_env: bool = True) -> str:
    """`env K=V … gamescope … -- [mangohud --dlsym] <command>` from a launch payload."""
    parts: List[str] = []
    env = payload.get("env") or {}
    if env:
        kv = [f"{k}={shlex.quote(str(v))}" for k, v in env.items()]
        parts.append(("env " if use_env else "") + " ".join(kv))
    gs = payload.get("gs_cmd") or ""
    if gs:
        parts.append(gs)
    parts.append(command)
    return " ".join(parts)


def gamescope_arg_string(flags: Dict[str, str]) -> str:
    """Flag part of the gamescope command (no program name, no '--', no mangohud)."""
    toks = []
    for flag, val in flags.items():
        if flag == GamescopeFlagsWidget.MANGOHUD_KEY:
            continue
        toks.append(flag if val == "" else f"{flag} {shlex.quote(val)}")
    return " ".join(toks)


def _backup_file(path: Path) -> Path:
    bak = path.with_name(f"{path.name}.drstool-{datetime.now():%Y%m%d-%H%M%S}.bak")
    shutil.copy2(path, bak)
    # keep the 20 newest backups of this file
    olds = sorted(path.parent.glob(f"{path.name}.drstool-*.bak"))
    for o in olds[:-20]:
        try:
            o.unlink()
        except OSError:
            pass
    return bak


def _atomic_write(path: Path, text: str):
    tmp = path.with_name(path.name + ".drstool-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _proc_running(*names: str) -> bool:
    for n in names:
        if _run(["pgrep", "-x", n], 3).strip():
            return True
    return False


# ── minimal VDF (Valve KeyValues text) editor ───────────────────────────────

_VDF_TOK = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])', re.S)


def _vdf_unesc(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def _vdf_esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _vdf_parse(text: str):
    toks = []
    for m in _VDF_TOK.finditer(text):
        if m.group(2) == "{":
            toks.append(("open", "", m.start(), m.end()))
        elif m.group(2) == "}":
            toks.append(("close", "", m.start(), m.end()))
        else:
            toks.append(("str", _vdf_unesc(m.group(1)), m.start(), m.end()))

    def block(i):
        items = []
        while i < len(toks):
            k = toks[i]
            if k[0] == "close":
                return items, i
            if k[0] == "str" and i + 1 < len(toks):
                n = toks[i + 1]
                if n[0] == "str":
                    items.append((k, "val", n))
                    i += 2
                    continue
                if n[0] == "open":
                    sub, j = block(i + 2)
                    items.append((k, "block", (n, sub, toks[j] if j < len(toks) else n)))
                    i = j + 1
                    continue
            i += 1
        return items, i

    items, _ = block(0)
    return items


def _vdf_find(items, key: str, kind: str):
    kl = key.lower()
    for k, kd, payload in items:
        if kd == kind and k[1].lower() == kl:
            return payload
    return None


def _vdf_apps_block(text: str):
    items = _vdf_parse(text)
    node = items
    for part in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps"):
        b = _vdf_find(node, part, "block")
        if b is None:
            raise ValueError(f'"{part}" section not found in localconfig.vdf '
                             "(start Steam once with this account first)")
        node = b[1]
        last = b
    return last            # (open_tok, items, close_tok) of "apps"


def vdf_get_launch_options(text: str, appid: str) -> str:
    try:
        apps = _vdf_apps_block(text)
    except ValueError:
        return ""
    app = _vdf_find(apps[1], str(appid), "block")
    if app is None:
        return ""
    v = _vdf_find(app[1], "LaunchOptions", "val")
    return v[1] if v else ""


def vdf_set_launch_options(text: str, appid: str, value: str) -> str:
    apps = _vdf_apps_block(text)
    appid = str(appid)
    app = _vdf_find(apps[1], appid, "block")
    esc = _vdf_esc(value)

    def child_insert(close_tok, body: str) -> str:
        pos = close_tok[2]
        ls = text.rfind("\n", 0, pos) + 1
        indent = text[ls:pos]
        if indent.strip() == "":                       # brace alone on its line
            return text[:ls] + body.replace("\n", "\n", 1) + text[ls:]
        return text[:pos] + "\n" + body + text[pos:]

    def indent_of(close_tok) -> str:
        pos = close_tok[2]
        ls = text.rfind("\n", 0, pos) + 1
        ind = text[ls:pos]
        return ind if ind.strip() == "" else ""

    if app is not None:
        v = _vdf_find(app[1], "LaunchOptions", "val")
        if v is not None:
            tok = v
            return text[:tok[2]] + f'"{esc}"' + text[tok[3]:]
        ind = indent_of(app[2]) + "\t"
        return child_insert(app[2], f'{ind}"LaunchOptions"\t\t"{esc}"\n')
    ind = indent_of(apps[2]) + "\t"
    body = (f'{ind}"{appid}"\n{ind}{{\n{ind}\t"LaunchOptions"\t\t"{esc}"\n{ind}}}\n')
    return child_insert(apps[2], body)


# ── backends ────────────────────────────────────────────────────────────────

@dataclass
class LEntry:
    key: str
    title: str
    path: Optional[Path] = None
    extra: dict = field(default_factory=dict)


class LauncherBackend:
    name = ""
    blurb = ""

    def unavailable(self) -> str:
        return ""

    def accounts(self) -> List[tuple]:
        return []                      # [(label, id)]

    def scan(self, account=None) -> List[LEntry]:
        return []

    def current(self, e: LEntry) -> str:
        return ""

    def proposed(self, e: LEntry, payload: dict, opts: dict) -> str:
        return ""

    def busy(self) -> str:
        return ""

    def extra_preview(self, e: LEntry, opts: dict) -> str:
        return ""

    def apply(self, e: LEntry, payload: dict, opts: dict) -> str:
        raise NotImplementedError


class SteamBackend(LauncherBackend):
    name = "Steam"
    blurb = ("Writes the launch options of an installed game into Steam's localconfig.vdf "
             "(a timestamped .bak is kept next to it). Steam must be closed — it rewrites that "
             "file on exit and would discard the change.")

    @staticmethod
    def roots() -> List[Path]:
        h = Path.home()
        cands = [h / ".local/share/Steam", h / ".steam/steam", h / ".steam/root",
                 h / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
                 h / ".var/app/com.valvesoftware.Steam/data/Steam"]
        seen, out = set(), []
        for c in cands:
            try:
                r = c.resolve()
            except OSError:
                continue
            if (r / "userdata").is_dir() and r not in seen:
                seen.add(r)
                out.append(r)
        return out

    def unavailable(self):
        return "" if self.roots() else "No Steam installation found (~/.local/share/Steam, flatpak)."

    def accounts(self):
        res = []
        for root in self.roots():
            for ud in sorted((root / "userdata").iterdir()):
                cfg = ud / "config" / "localconfig.vdf"
                if ud.name.isdigit() and ud.name != "0" and cfg.is_file():
                    name = ""
                    try:
                        m = re.search(r'"PersonaName"\s+"([^"]*)"',
                                      cfg.read_text(errors="replace")[:200000])
                        name = m.group(1) if m else ""
                    except OSError:
                        pass
                    res.append((f"{name or 'account'}  ({ud.name})", str(cfg)))
        return res

    @staticmethod
    def _libraries(root: Path) -> List[Path]:
        libs = [root / "steamapps"]
        vf = root / "steamapps" / "libraryfolders.vdf"
        try:
            for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', vf.read_text(errors="replace")):
                p = Path(_vdf_unesc(m.group(1))) / "steamapps"
                if p not in libs:
                    libs.append(p)
        except OSError:
            pass
        return [l for l in libs if l.is_dir()]

    def scan(self, account=None):
        cfg = Path(account) if account else None
        if cfg is None:
            acc = self.accounts()
            cfg = Path(acc[0][1]) if acc else None
        if cfg is None:
            return []
        root = cfg.parents[3]                      # <root>/userdata/<id>/config/localconfig.vdf
        out, seen = [], set()
        for lib in self._libraries(root):
            for acf in sorted(lib.glob("appmanifest_*.acf")):
                try:
                    t = acf.read_text(errors="replace")
                except OSError:
                    continue
                a = re.search(r'"appid"\s+"(\d+)"', t)
                n = re.search(r'"name"\s+"((?:[^"\\]|\\.)*)"', t)
                if not a or a.group(1) in seen:
                    continue
                name = _vdf_unesc(n.group(1)) if n else a.group(1)
                if re.match(r"(Proton\b|Steam Linux Runtime|Steamworks Common)", name):
                    continue
                seen.add(a.group(1))
                out.append(LEntry(a.group(1), f"{name}  [{a.group(1)}]", cfg))
        out.sort(key=lambda e: e.title.lower())
        return out

    def current(self, e):
        try:
            return vdf_get_launch_options(e.path.read_text(errors="replace"), e.key)
        except OSError:
            return ""

    def proposed(self, e, payload, opts):
        return build_launch_line(payload, "%command%", opts.get("use_env", True))

    def busy(self):
        return "Steam is running — close it first (it rewrites localconfig.vdf on exit)." \
            if _proc_running("steam", "steamwebhelper") else ""

    def apply(self, e, payload, opts):
        text = e.path.read_text(errors="replace")
        new = vdf_set_launch_options(text, e.key, self.proposed(e, payload, opts))
        # sanity: re-parse and read back
        if vdf_get_launch_options(new, e.key) != self.proposed(e, payload, opts):
            raise ValueError("Self-check failed: edited VDF does not read back identically — aborted.")
        ct = opts.get("compat_tool") or ""
        cfg_new = None
        if ct:                                     # prepare first so a failure aborts before any write
            cfg = steam_config_vdf(e.path)
            ctext = cfg.read_text(errors="replace")
            if ct == "__default__":
                cfg_new = vdf_remove_app_block(ctext, STEAM_COMPAT_PATH, e.key)
                ok = vdf_get_app_value(cfg_new, STEAM_COMPAT_PATH, e.key, "name") == ""
            else:
                cfg_new = vdf_set_app_value(ctext, STEAM_COMPAT_PATH, e.key, "name", ct)
                cfg_new = vdf_set_app_value(cfg_new, STEAM_COMPAT_PATH, e.key, "config",
                                            vdf_get_app_value(cfg_new, STEAM_COMPAT_PATH, e.key, "config"))
                if not vdf_get_app_value(cfg_new, STEAM_COMPAT_PATH, e.key, "priority"):
                    cfg_new = vdf_set_app_value(cfg_new, STEAM_COMPAT_PATH, e.key, "priority", "250")
                ok = vdf_get_app_value(cfg_new, STEAM_COMPAT_PATH, e.key, "name") == ct
            if not ok:
                raise ValueError("Self-check failed: config.vdf does not read back as intended — aborted.")
        bak = _backup_file(e.path)
        _atomic_write(e.path, new)
        msg = f"Steam launch options written for {e.title}. Backup: {bak.name}"
        if cfg_new is not None:
            cbak = _backup_file(cfg)
            _atomic_write(cfg, cfg_new)
            msg += (" Proton override removed." if ct == "__default__" else f" Proton set to {ct}.") + \
                f" Backup: {cbak.name}"
        return msg

    def extra_preview(self, e, opts):
        try:
            cur = vdf_get_app_value(steam_config_vdf(e.path).read_text(errors="replace"),
                                    STEAM_COMPAT_PATH, e.key, "name")
        except OSError:
            cur = ""
        ct = opts.get("compat_tool") or ""
        new = ("remove the per-game override" if ct == "__default__" else ct) if ct else "unchanged"
        return f"── Proton (config.vdf) ──\ncurrent: {cur or '(Steam default)'}\nnew:     {new}"


class HeroicBackend(LauncherBackend):
    name = "Heroic"
    blurb = ("Writes environment variables and gamescope/MangoHud wrappers into Heroic's "
             "GamesConfig/<game>.json (or the global defaults). Only variables DRSTool owns are "
             "replaced; your other entries stay. Close Heroic first, otherwise it may overwrite.")

    @staticmethod
    def roots() -> List[Path]:
        h = Path.home()
        return [p for p in (h / ".config/heroic",
                            h / ".var/app/com.heroicgameslauncher.hgl/config/heroic") if p.is_dir()]

    def unavailable(self):
        return "" if self.roots() else "No Heroic config found (~/.config/heroic, flatpak)."

    @staticmethod
    def _titles(root: Path) -> Dict[str, str]:
        titles: Dict[str, str] = {}
        files = [root / "store_cache" / n for n in
                 ("legendary_library.json", "gog_library.json", "nile_library.json",
                  "zoom_library.json")] + [root / "sideload_apps" / "library.json"]

        def walk(o):
            if isinstance(o, dict):
                if "app_name" in o and "title" in o:
                    titles[str(o["app_name"])] = str(o["title"])
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        for f in files:
            try:
                walk(json.loads(f.read_text(errors="replace")))
            except Exception:
                pass
        return titles

    def scan(self, account=None):
        out = []
        for root in self.roots():
            cfgf = root / "config.json"
            out.append(LEntry("__global__", "★ All games (global defaults)", cfgf, {"global": True}))
            titles = self._titles(root)
            gdir = root / "GamesConfig"
            if gdir.is_dir():
                for f in sorted(gdir.glob("*.json")):
                    out.append(LEntry(f.stem, f"{titles.get(f.stem, f.stem)}  [{f.stem}]", f))
            break                                   # first existing root only
        return out

    @staticmethod
    def _node(data: dict, e: LEntry) -> dict:
        if e.extra.get("global"):
            return data.setdefault("defaultSettings", {})
        return data.setdefault(e.key, {})

    def _load(self, e):
        try:
            d = json.loads(e.path.read_text(errors="replace"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def current(self, e):
        d = self._load(e)
        node = d.get("defaultSettings", {}) if e.extra.get("global") else d.get(e.key, {})
        envs = [f'{x.get("key")}={x.get("value")}' for x in node.get("enviromentOptions", [])
                if isinstance(x, dict)]
        wr = [f'{x.get("exe")} {x.get("args", "")}'.strip() for x in node.get("wrapperOptions", [])
              if isinstance(x, dict)]
        return "env:\n  " + "\n  ".join(envs or ["(none)"]) + "\nwrappers:\n  " + "\n  ".join(wr or ["(none)"])

    @staticmethod
    def _wrappers(payload):
        w = []
        if payload.get("gs_on"):
            args = gamescope_arg_string(payload.get("gs_flags") or {})
            w.append({"exe": "gamescope", "args": (args + " --").strip()})
            if payload.get("mangohud"):
                w.append({"exe": "mangohud", "args": "--dlsym"})
        return w

    def proposed(self, e, payload, opts):
        envs = [f"{k}={v}" for k, v in (payload.get("env") or {}).items()]
        wr = [f'{w["exe"]} {w["args"]}' for w in self._wrappers(payload)]
        return "env:\n  " + "\n  ".join(envs or ["(none)"]) + "\nwrappers:\n  " + "\n  ".join(wr or ["(none)"])

    def busy(self):
        return "Heroic is running — it may overwrite this file when it exits." \
            if _proc_running("heroic") else ""

    def apply(self, e, payload, opts):
        data = self._load(e) if e.path.is_file() else {}
        node = self._node(data, e)
        owned = LutrisSyncWidget._managed_env_keys()
        keep = [x for x in node.get("enviromentOptions", [])
                if isinstance(x, dict) and x.get("key") not in owned]
        keep += [{"key": k, "value": str(v)} for k, v in (payload.get("env") or {}).items()]
        node["enviromentOptions"] = keep
        wkeep = [w for w in node.get("wrapperOptions", [])
                 if isinstance(w, dict) and os.path.basename(str(w.get("exe", ""))) not in ("gamescope", "mangohud")]
        new_w = self._wrappers(payload)
        node["wrapperOptions"] = new_w + wkeep
        if any(w["exe"] == "mangohud" for w in new_w):
            node["showMangohud"] = False           # avoid wrapping gamescope from the outside
        bak = _backup_file(e.path) if e.path.is_file() else None
        e.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(e.path, json.dumps(data, indent=2, ensure_ascii=False))
        return f"Heroic config written for {e.title}." + (f" Backup: {bak.name}" if bak else "")


class BottlesBackend(LauncherBackend):
    name = "Bottles"
    blurb = ("Writes Environment_Variables and the gamescope/MangoHud toggles into the bottle's "
             "bottle.yml. Bottles settings are per bottle (not per game). Gamescope flags without a "
             "Bottles equivalent are listed in the preview and skipped.")

    # gamescope flag -> (Parameters key, type)
    _MAP = {"--nested-width": ("gamescope_game_width", int), "--nested-height": ("gamescope_game_height", int),
            "--output-width": ("gamescope_window_width", int), "--output-height": ("gamescope_window_height", int),
            "--framerate-limit": ("gamescope_fps", int), "--nested-unfocused-refresh": ("gamescope_fps_no_focus", int),
            "--borderless": ("gamescope_borderless", bool), "--fullscreen": ("gamescope_fullscreen", bool)}

    @staticmethod
    def roots() -> List[Path]:
        h = Path.home()
        return [p for p in (h / ".local/share/bottles/bottles",
                            h / ".var/app/com.usebottles.bottles/data/bottles/bottles") if p.is_dir()]

    def unavailable(self):
        if not _HAVE_YAML:
            return "PyYAML is required to edit bottle.yml."
        return "" if self.roots() else "No Bottles directory found (~/.local/share/bottles, flatpak)."

    def scan(self, account=None):
        out = []
        for root in self.roots():
            for d in sorted(root.iterdir()):
                f = d / "bottle.yml"
                if f.is_file():
                    name = d.name
                    try:
                        name = (yaml.safe_load(f.read_text()) or {}).get("Name", d.name)
                    except Exception:
                        pass
                    out.append(LEntry(d.name, f"{name}  [{d.name}]", f))
        return out

    def _load(self, e):
        try:
            d = yaml.safe_load(e.path.read_text()) or {}
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _plan(self, payload):
        params: Dict[str, object] = {}
        skipped = []
        gs_on = bool(payload.get("gs_on"))
        params["gamescope"] = gs_on
        params["mangohud"] = bool(payload.get("mangohud"))
        if gs_on:
            flags = payload.get("gs_flags") or {}
            for flag, val in flags.items():
                if flag == GamescopeFlagsWidget.MANGOHUD_KEY:
                    continue
                m = self._MAP.get(flag)
                if not m:
                    skipped.append(flag if val == "" else f"{flag} {val}")
                    continue
                key, typ = m
                try:
                    params[key] = True if typ is bool else int(val)
                except ValueError:
                    skipped.append(f"{flag} {val}")
        return params, skipped

    def current(self, e):
        d = self._load(e)
        envs = d.get("Environment_Variables") or {}
        p = d.get("Parameters") or {}
        gp = {k: v for k, v in p.items() if k.startswith("gamescope") or k == "mangohud"}
        return ("Environment_Variables:\n  " + "\n  ".join(f"{k}={v}" for k, v in envs.items()) +
                "\nParameters:\n  " + "\n  ".join(f"{k}: {v}" for k, v in gp.items()))

    def proposed(self, e, payload, opts):
        params, skipped = self._plan(payload)
        txt = ("Environment_Variables (DRSTool-owned part):\n  " +
               "\n  ".join(f"{k}={v}" for k, v in (payload.get("env") or {}).items()) +
               "\nParameters:\n  " + "\n  ".join(f"{k}: {v}" for k, v in params.items()))
        if skipped:
            txt += "\n\nNo Bottles equivalent (skipped): " + ", ".join(skipped)
        return txt

    def apply(self, e, payload, opts):
        d = self._load(e)
        envs = dict(d.get("Environment_Variables") or {})
        for k in list(envs):
            if k in LutrisSyncWidget._managed_env_keys():
                del envs[k]
        envs.update({k: str(v) for k, v in (payload.get("env") or {}).items()})
        d["Environment_Variables"] = envs
        params = dict(d.get("Parameters") or {})
        for k in [k for k in params if k.startswith("gamescope") and k not in ("gamescope",)]:
            if k in {v[0] for v in self._MAP.values()}:
                del params[k]
        new_p, _skipped = self._plan(payload)
        params.update(new_p)
        d["Parameters"] = params
        bak = _backup_file(e.path)
        _atomic_write(e.path, yaml.safe_dump(d, sort_keys=False, allow_unicode=True,
                                             default_flow_style=False))
        return f"bottle.yml updated for {e.title}. Backup: {bak.name}"


_LAUNCHER_QSS = (f"QListWidget{{background:{T['surface']};border:1px solid {T['border']};"
                 f"border-radius:4px;color:{T['text2']};font-size:12px;}}"
                 f"QListWidget::item{{padding:3px 6px;}}"
                 f"QListWidget::item:selected{{background:rgba(118,185,0,0.18);color:{T['text']};}}")
_MONO_BOX_QSS = (f"QPlainTextEdit{{background:{T['bg']};border:1px solid {T['border_hi']};"
                 f"border-radius:4px;color:{T['text']};font-family:{T['mono']};font-size:12px;"
                 "padding:4px 6px;}")


class LauncherSyncWidget(QWidget):
    """One tab per launcher (Steam / Heroic / Bottles): pick target -> preview -> apply."""

    def __init__(self, backend: LauncherBackend, parent=None):
        super().__init__(parent)
        self.backend = backend
        self._entries: List[LEntry] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)

        info = QLabel(backend.blurb)
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(info)

        row = QHBoxLayout()
        self._acct = QComboBox()
        self._acct.setVisible(False)
        self._acct.currentIndexChanged.connect(lambda _i: self.rescan(keep_account=True))
        row.addWidget(self._acct)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter targets…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        row.addWidget(self._filter, 1)
        b = _tool_btn("Rescan")
        b.clicked.connect(lambda: self.rescan())
        row.addWidget(b)
        lay.addLayout(row)

        self._list = QListWidget()
        self._list.setStyleSheet(_LAUNCHER_QSS)
        self._list.setMinimumHeight(150)
        self._list.currentItemChanged.connect(lambda *_: self.refresh_preview())
        lay.addWidget(self._list, 1)

        self._use_env = QCheckBox("Prefix variables with  env  (env A=1 gamescope … -- %command%)")
        self._use_env.setChecked(True)
        self._use_env.setVisible(backend.name == "Steam")
        self._use_env.toggled.connect(lambda _c: self.refresh_preview())
        self._use_env.setStyleSheet(f"color:{T['text2']};")
        lay.addWidget(self._use_env)

        self._compat_root = None
        self._compat = QComboBox()
        self._compat.setStyleSheet(_COMBO_QSS)
        self._compat.setVisible(backend.name == "Steam")
        self._compat.setToolTip("Writes the per-game compatibility tool (CompatToolMapping in "
                                "config.vdf) together with the launch options.")
        self._compat.currentIndexChanged.connect(lambda _i: self.refresh_preview())
        lay.addWidget(self._compat)

        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setStyleSheet(_MONO_BOX_QSS)
        self._preview.setMinimumHeight(150)
        lay.addWidget(self._preview, 1)

        btns = QHBoxLayout()
        self._apply_btn = _tool_btn(f"Apply to {backend.name}", bg="#26330f", border="#4a7300",
                                    fg="#cfe9a0", bold=True)
        self._apply_btn.clicked.connect(self._apply)
        btns.addWidget(self._apply_btn)
        cp = _tool_btn("Copy new value")
        cp.clicked.connect(self._copy)
        btns.addWidget(cp)
        btns.addStretch(1)
        lay.addLayout(btns)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(self._status)

        un = backend.unavailable()
        if un:
            self._status.setText("⚠ " + un)
            self._apply_btn.setEnabled(False)
        self._first_scan = True

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        if self._first_scan:
            self._first_scan = False
            QTimer.singleShot(0, self.rescan)

    # -- data -----------------------------------------------------------------
    def _payload(self) -> dict:
        w = self.window()
        return w.collect_launch_payload() if hasattr(w, "collect_launch_payload") else {}

    def rescan(self, keep_account: bool = False):
        be = self.backend
        if be.unavailable():
            return
        if not keep_account:
            self._acct.blockSignals(True)
            self._acct.clear()
            for label, ident in be.accounts():
                self._acct.addItem(label, ident)
            self._acct.setVisible(self._acct.count() > 1)
            self._acct.blockSignals(False)
        acct = self._acct.currentData() if self._acct.count() else None
        try:
            self._entries = be.scan(acct)
        except Exception as ex:
            self._entries = []
            self._status.setText(f"⚠ scan failed: {ex}")
        self._list.clear()
        for e in self._entries:
            it = QListWidgetItem(e.title)
            it.setData(Qt.UserRole, e)
            self._list.addItem(it)
        self._apply_filter(self._filter.text())
        if self._entries:
            self._status.setText(f"{len(self._entries)} target(s) found.")
        elif not self._status.text().startswith("⚠"):
            self._status.setText("No targets found.")
        self.refresh_preview()

    def _apply_filter(self, text):
        t = text.lower().strip()
        for i in range(self._list.count()):
            it = self._list.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def _sel(self) -> Optional[LEntry]:
        it = self._list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _opts(self) -> dict:
        return {"use_env": self._use_env.isChecked(),
                "compat_tool": (self._compat.currentData() or "") if self.backend.name == "Steam" else ""}

    def _fill_compat(self, e):
        if self.backend.name != "Steam":
            return
        root = e.path.parents[3]
        if root == self._compat_root:
            return
        self._compat_root = root
        self._compat.blockSignals(True)
        self._compat.clear()
        self._compat.addItem("Proton: leave unchanged", "")
        self._compat.addItem("Proton: Steam default (remove per-game override)", "__default__")
        for name, label in steam_compat_tools(root):
            self._compat.addItem("Proton: " + label, name)
        self._compat.blockSignals(False)

    def refresh_preview(self):
        e = self._sel()
        if e is None:
            self._preview.setPlainText("Select a target above to preview what will be written.")
            return
        p = self._payload()
        try:
            self._fill_compat(e)
            cur = self.backend.current(e)
            new = self.backend.proposed(e, p, self._opts())
            more = self.backend.extra_preview(e, self._opts())
        except Exception as ex:
            self._preview.setPlainText(f"⚠ {ex}")
            return
        self._preview.setPlainText(f"── current ──\n{cur or '(empty)'}\n\n── new ──\n{new or '(empty)'}"
                                   + (f"\n\n{more}" if more else ""))

    def _copy(self):
        e = self._sel()
        p = self._payload()
        txt = (self.backend.proposed(e, p, self._opts()) if e else
               build_launch_line(p, "%command%", True))
        QApplication.clipboard().setText(txt)
        self._status.setText("Copied.")

    def _apply(self):
        e = self._sel()
        if e is None:
            self._status.setText("Pick a target first.")
            return
        busy = self.backend.busy()
        if busy:
            QMessageBox.warning(self, self.backend.name, busy)
            return
        if QMessageBox.question(
                self, f"Write to {self.backend.name}",
                f"Write the current DRSTool configuration to:\n\n{e.title}\n\n"
                "A timestamped backup of the file is kept.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            msg = self.backend.apply(e, self._payload(), self._opts())
            self._status.setText("✔ " + msg)
        except Exception as ex:
            self._status.setText(f"⚠ failed: {ex}")
            QMessageBox.critical(self, self.backend.name, str(ex))
        self.refresh_preview()


class LaunchLineWidget(QWidget):
    """One-click `env … gamescope … -- %command%` (or any other command)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        t = QLabel("Runnable launch line")
        t.setStyleSheet(f"color:{T['text']}; font-size:15px; font-weight:700;")
        lay.addWidget(t)
        d = QLabel("Built from everything currently set (arch, DRS settings, environment, "
                   "gamescope, MangoHud). Use %command% for Steam, or type your own command to "
                   "get a line you can paste into a terminal or save as a launcher script.")
        d.setWordWrap(True)
        d.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(d)

        form = QHBoxLayout()
        _cl = QLabel("Command:")
        _cl.setStyleSheet(f"color:{T['text2']};")
        form.addWidget(_cl)
        self._cmd = QLineEdit("%command%")
        self._cmd.setStyleSheet(f"QLineEdit{{background:{T['surface']};border:1px solid {T['border']};"
                                f"color:{T['text']};padding:3px 8px;border-radius:4px;min-height:26px;"
                                f"font-family:{T['mono']};}}")
        self._cmd.textChanged.connect(lambda _t: self.refresh())
        form.addWidget(self._cmd, 1)
        self._use_env = QCheckBox("use  env  prefix")
        self._use_env.setChecked(True)
        self._use_env.setStyleSheet(f"color:{T['text2']};")
        self._use_env.toggled.connect(lambda _c: self.refresh())
        form.addWidget(self._use_env)
        lay.addLayout(form)

        # umu-launcher: run the .exe through umu-run with PROTONPATH / GAMEID / STORE
        urow = QHBoxLayout()
        self._umu = QCheckBox("umu-run")
        self._umu.setToolTip("Wrap the command with umu-launcher (command = path to the .exe). "
                             "Adds PROTONPATH, GAMEID, STORE and WINEPREFIX.")
        self._umu.setStyleSheet(f"color:{T['text2']};")
        self._umu.toggled.connect(lambda _c: self.refresh())
        urow.addWidget(self._umu)
        self._umu_proton = QComboBox()
        self._umu_proton.setEditable(True)
        self._umu_proton.setStyleSheet(_COMBO_QSS)
        self._umu_proton.setToolTip("PROTONPATH: GE-Proton / UMU-Latest (umu downloads it) or an installed Proton directory")
        self._umu_proton.addItem("GE-Proton")
        self._umu_proton.addItem("UMU-Latest")
        for label, d in proton_tool_dirs():
            self._umu_proton.addItem(label, d)
        self._umu_proton.currentTextChanged.connect(lambda _t: self.refresh())
        urow.addWidget(self._umu_proton, 2)
        self._umu_gid = QLineEdit("umu-0")
        self._umu_gid.setToolTip("GAMEID: umu-<steam appid> picks umu's protonfixes for that game; umu-0 = none")
        self._umu_gid.setStyleSheet(_LINEEDIT_QSS)
        self._umu_gid.setMaximumWidth(120)
        self._umu_gid.textChanged.connect(lambda _t: self.refresh())
        urow.addWidget(self._umu_gid)
        self._umu_store = QComboBox()
        self._umu_store.setStyleSheet(_COMBO_QSS)
        self._umu_store.setToolTip("STORE")
        for st in UMU_STORES:
            self._umu_store.addItem(st or "(no STORE)", st)
        self._umu_store.currentIndexChanged.connect(lambda _i: self.refresh())
        urow.addWidget(self._umu_store)
        self._umu_pfx = QLineEdit()
        self._umu_pfx.setPlaceholderText("WINEPREFIX (optional)")
        self._umu_pfx.setStyleSheet(_LINEEDIT_QSS)
        self._umu_pfx.textChanged.connect(lambda _t: self.refresh())
        urow.addWidget(self._umu_pfx, 2)
        lay.addLayout(urow)

        self._out = QPlainTextEdit()
        self._out.setReadOnly(True)
        self._out.setStyleSheet(_MONO_BOX_QSS)
        self._out.setMinimumHeight(140)
        lay.addWidget(self._out, 1)

        row = QHBoxLayout()
        b1 = _tool_btn("Copy line", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        b1.clicked.connect(self._copy)
        row.addWidget(b1)
        b2 = _tool_btn("Copy for Steam (%command%)")
        b2.clicked.connect(self._copy_steam)
        row.addWidget(b2)
        b3 = _tool_btn("Save as launcher script…")
        b3.clicked.connect(self._save_script)
        row.addWidget(b3)
        row.addStretch(1)
        lay.addLayout(row)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(self._status)
        lay.addStretch(0)

    def _payload(self):
        w = self.window()
        return w.collect_launch_payload() if hasattr(w, "collect_launch_payload") else {}

    def build_line(self, cmd: Optional[str] = None, steam: bool = False, force_env: bool = False) -> str:
        """The launch line; with umu-run enabled the command is wrapped as `umu-run <exe>`."""
        p = dict(self._payload())
        cmd = (self._cmd.text().strip() if cmd is None else cmd.strip()) or "%command%"
        if self._umu.isChecked() and not steam:
            env = dict(p.get("env") or {})
            pp = self._umu_proton.currentData() or self._umu_proton.currentText().strip()
            if pp:
                env["PROTONPATH"] = pp
            if self._umu_gid.text().strip():
                env["GAMEID"] = self._umu_gid.text().strip()
            if self._umu_store.currentData():
                env["STORE"] = self._umu_store.currentData()
            if self._umu_pfx.text().strip():
                env["WINEPREFIX"] = os.path.expanduser(self._umu_pfx.text().strip())
            p["env"] = env
            cmd = "umu-run " + ("<game.exe>" if cmd == "%command%" else cmd)
        return build_launch_line(p, cmd, self._use_env.isChecked() or force_env)

    def refresh(self):
        self._out.setPlainText(self.build_line())

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        self.refresh()

    def _copy(self):
        QApplication.clipboard().setText(self._out.toPlainText())
        self._status.setText("Copied.")

    def _copy_steam(self):
        QApplication.clipboard().setText(self.build_line("%command%", steam=True))
        self._status.setText("Copied Steam launch options.")

    def _save_script(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save launcher script",
                                              str(Path.home() / "drstool-launch.sh"), "Shell script (*.sh)")
        if not path:
            return
        cmd = self._cmd.text().strip()
        inner = '"$@"' if cmd in ("", "%command%") else cmd
        line = self.build_line(inner)
        try:
            Path(path).write_text(f"#!/bin/sh\n# generated by DRSTool\nexec {line}\n")
            os.chmod(path, 0o755)
            self._status.setText(f"Saved {path}")
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")


# ============================================================================
# Prefix doctor: DXVK / vkd3d-proton / dxvk-nvapi inside a Wine prefix
# ============================================================================

import tarfile
import hashlib as _hashlib
from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView

PD_COMPONENTS = [
    {"id": "dxvk", "name": "DXVK", "repo": "doitsujin/dxvk",
     "dlls": ["d3d8", "d3d9", "d3d10core", "d3d11", "dxgi"],
     "markers": [b"DXVK", b"dxvk"], "ver_prefix_required": True,
     "asset_re": r"^dxvk-v?\d[\d.]*\.tar\.(gz|zst)$", "d64": "x64", "d32": "x32"},
    {"id": "vkd3d", "name": "vkd3d-proton", "repo": "HansKristian-Work/vkd3d-proton",
     "dlls": ["d3d12", "d3d12core"],
     "markers": [b"vkd3d-proton", b"VKD3D_CONFIG"], "ver_prefix_required": False,
     "asset_re": r"^vkd3d-proton-v?\d[\d.]*\.tar\.(zst|gz)$", "d64": "x64", "d32": "x86"},
    {"id": "nvapi", "name": "dxvk-nvapi", "repo": "jp7677/dxvk-nvapi",
     "dlls": ["nvapi64", "nvapi", "nvofapi64"],
     "markers": [b"DXVK-NVAPI", b"dxvk-nvapi", b"NVAPI"], "ver_prefix_required": True,
     "asset_re": r"^dxvk-nvapi-v?\d[\d.]*\.tar\.(gz|zst)$", "d64": "x64", "d32": "x32"},
]
_PD_UA = "DRSTool-prefix-doctor"
_PD_MANIFEST = ".drstool-doctor.json"


def _vtuple(s: str) -> tuple:
    m = re.search(r"(\d+(?:\.\d+)+)", s or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _vcmp(a: tuple, b: tuple) -> int:
    n = max(len(a), len(b), 3)
    a2, b2 = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a2 > b2) - (a2 < b2)


def scan_dll_version(path: Path, comp: dict) -> Optional[str]:
    """Heuristic: version-looking string nearest to the component's marker; falls back to
    the PE FileVersion resource. Returns None for 'not this component', '' for 'unknown'."""
    try:
        if path.stat().st_size > 80 * 1024 * 1024:
            return ""
        data = path.read_bytes()
    except OSError:
        return None
    pos = [data.find(m) for m in comp["markers"] if data.find(m) >= 0]
    if not pos:
        return None                                    # a Wine builtin / something else
    anchor = min(pos)
    vpre = rb"v" if comp["ver_prefix_required"] else rb"v?"
    pat = re.compile(rb"(?<![\w.])(" + vpre + rb"\d+\.\d+(?:\.\d+)?(?:-\d+-g[0-9a-f]{6,})?[+\w\-]{0,12})\x00")
    cands = [(abs(m.start() - anchor), m.group(1).decode("ascii", "replace"))
             for m in pat.finditer(data)]
    if cands:
        cands.sort()
        if cands[0][0] < 4096:                         # must sit close to the marker
            return cands[0][1]
    m = re.search(rb"F\x00i\x00l\x00e\x00V\x00e\x00r\x00s\x00i\x00o\x00n\x00\x00\x00((?:[0-9A-Za-z.\-+_]\x00)+)", data)
    if m:
        v = m.group(1).decode("utf-16-le", "replace")
        if re.match(r"\d+\.\d+", v) and not v.startswith(("0.0", "1.0.0.0")):
            return v
    return ""


def _sha256(p: Path) -> str:
    h = _hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def prefix_sysdirs(prefix: Path):
    win = prefix / "drive_c" / "windows"
    s32, s64 = win / "system32", win / "syswow64"
    return win.is_dir(), s32, (s64 if s64.is_dir() else None)


def detect_installed(prefix: Path, comp: dict) -> dict:
    """-> {"state": none|ok|mixed|unknown, "version": str, "source": manifest|scan, "files": [...]}"""
    ok, s32, s64 = prefix_sysdirs(prefix)
    files = []
    for d, tag in ((s32, "system32"), (s64, "syswow64")):
        if d is None:
            continue
        for dll in comp["dlls"]:
            p = d / f"{dll}.dll"
            if p.is_file():
                files.append((f"{tag}/{dll}.dll", p))
    manifest = {}
    try:
        manifest = json.loads((prefix / _PD_MANIFEST).read_text()).get(comp["id"], {})
    except Exception:
        pass
    res = {"state": "none", "version": "", "source": "", "files": [r for r, _ in files]}
    if not files:
        return res
    # authoritative: DRSTool put these exact files there
    mf = manifest.get("files") or {}
    if mf and all(rel in mf and _sha256(p) == mf[rel] for rel, p in files if rel in mf) \
            and any(rel in mf for rel, _ in files):
        res.update(state="ok", version=manifest.get("version", ""), source="manifest")
        return res
    versions, other = set(), 0
    for rel, p in files:
        v = scan_dll_version(p, comp)
        if v is None:
            other += 1
        else:
            versions.add(v)
    if not versions:
        res["state"] = "none"                          # only Wine builtins present
        return res
    real = {v for v in versions if v}
    if len(real) > 1 or (other and real):
        res.update(state="mixed", version=" / ".join(sorted(real)) or "?", source="scan")
    elif real:
        res.update(state="ok", version=next(iter(real)), source="scan")
    else:
        res.update(state="unknown", version="?", source="scan")
    return res


def dll_override_gaps(prefix: Path, comp: dict) -> List[str]:
    try:
        t = (prefix / "user.reg").read_text(errors="replace")
    except OSError:
        return []
    m = re.search(r"^\[Software\\\\Wine\\\\DllOverrides\][^\n]*\n(.*?)(?:\n\[|\Z)", t, re.S | re.M)
    block = m.group(1) if m else ""
    have = {k.lower(): v.lower() for k, v in re.findall(r'^"([^"]+)"="([^"]*)"', block, re.M)}
    return [d for d in comp["dlls"]
            if d in ("d3d9", "d3d10core", "d3d11", "dxgi", "d3d12", "d3d12core", "d3d8", "nvapi", "nvapi64", "nvofapi64")
            and "native" not in have.get(d, "")]


def find_prefixes() -> List[tuple]:
    h = Path.home()
    cands: List[tuple] = []

    def add(label, p):
        try:
            p = Path(p)
            if (p / "drive_c" / "windows").is_dir() and all(str(p) != str(c[1]) for c in cands):
                cands.append((label, p))
        except OSError:
            pass
    if os.environ.get("WINEPREFIX"):
        add("$WINEPREFIX", os.environ["WINEPREFIX"])
    add("~/.wine", h / ".wine")
    for root in SteamBackend.roots():
        for lib in SteamBackend._libraries(root):
            cd = lib / "compatdata"
            if cd.is_dir():
                for d in sorted(cd.iterdir()):
                    add(f"Steam/Proton {d.name}", d / "pfx")
    for broot in BottlesBackend.roots():
        for d in sorted(broot.iterdir()):
            add(f"Bottles: {d.name}", d)
    for base in (h / "Games", h / ".local/share/lutris/prefixes"):
        if base.is_dir():
            for d in sorted(base.iterdir()):
                add(f"{base.name}/{d.name}", d)
                if d.is_dir():
                    for d2 in sorted(d.iterdir())[:40]:
                        add(f"{base.name}/{d.name}/{d2.name}", d2)
    return cands


def _gh_release(repo: str) -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"User-Agent": _PD_UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _pick_asset(rel: dict, comp: dict) -> Optional[dict]:
    pat = re.compile(comp["asset_re"])
    assets = [a for a in rel.get("assets", []) if pat.match(a.get("name", ""))]
    if not assets:
        return None
    assets.sort(key=lambda a: (a["name"].endswith(".zst"), a["name"]))   # prefer .gz
    return assets[0]


def _safe_extract(tf: tarfile.TarFile, dest: Path):
    base = dest.resolve()
    for m in tf.getmembers():
        tgt = (dest / m.name).resolve()
        if not str(tgt).startswith(str(base)) or m.issym() or m.islnk() or m.isdev():
            raise RuntimeError(f"unsafe path in archive: {m.name}")
    tf.extractall(dest)


def _extract_archive(path: Path, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    if path.name.endswith(".zst"):
        try:
            with tarfile.open(path, "r:zst") as tf:           # Python >= 3.14
                _safe_extract(tf, dest)
                return
        except RuntimeError:
            raise
        except Exception:
            pass
        for cmd in (["tar", "--zstd", "-xf", str(path), "-C", str(dest)],
                    ["tar", "--use-compress-program=unzstd", "-xf", str(path), "-C", str(dest)]):
            try:
                if subprocess.run(cmd, capture_output=True, timeout=300).returncode == 0:
                    return
            except Exception:
                continue
        raise RuntimeError("cannot unpack .tar.zst — install zstd (app-arch/zstd) or use Python ≥ 3.14")
    with tarfile.open(path, "r:*") as tf:
        _safe_extract(tf, dest)


def _cache_dir() -> Path:
    d = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "drstool" / "prefix-doctor"
    d.mkdir(parents=True, exist_ok=True)
    return d


def apply_component_update(prefix: Path, comp: dict, rel: dict, backup_root: Path, log) -> int:
    asset = _pick_asset(rel, comp)
    if asset is None:
        raise RuntimeError(f"no matching release asset for {comp['name']}")
    cache = _cache_dir() / rel.get("tag_name", "latest")
    cache.mkdir(parents=True, exist_ok=True)
    arch = cache / asset["name"]
    if not arch.is_file() or arch.stat().st_size != asset.get("size", -1):
        log(f"  downloading {asset['name']} …")
        req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": _PD_UA})
        tmp = arch.with_suffix(arch.suffix + ".part")
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(tmp, arch)
    work = Path(tempfile.mkdtemp(prefix="pd-", dir=str(cache)))
    try:
        _extract_archive(arch, work)
        tops = [p for p in work.iterdir() if p.is_dir()]
        root = tops[0] if len(tops) == 1 else work
        ok, s32, s64 = prefix_sysdirs(prefix)
        if not ok:
            raise RuntimeError("not a Wine prefix (drive_c/windows missing)")
        jobs = []
        if s64 is not None:                            # 64-bit prefix
            jobs.append((root / comp["d64"], s32, "system32"))
            jobs.append((root / comp["d32"], s64, "syswow64"))
        else:                                          # 32-bit prefix
            jobs.append((root / comp["d32"], s32, "system32"))
        n, manifest_files = 0, {}
        for src, dst, tag in jobs:
            if not src.is_dir():
                continue
            for dll in comp["dlls"]:
                f = src / f"{dll}.dll"
                if not f.is_file():
                    continue
                target = dst / f.name
                if target.exists() or target.is_symlink():
                    bdir = backup_root / tag
                    bdir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, bdir / f.name)
                    target.unlink()
                shutil.copy2(f, target)
                manifest_files[f"{tag}/{f.name}"] = _sha256(target)
                n += 1
                log(f"  {tag}/{f.name}")
        mpath = prefix / _PD_MANIFEST
        try:
            man = json.loads(mpath.read_text())
        except Exception:
            man = {}
        man[comp["id"]] = {"version": rel.get("tag_name", "").lstrip("v"), "files": manifest_files,
                           "updated": datetime.now().isoformat(timespec="seconds")}
        mpath.write_text(json.dumps(man, indent=2))
        return n
    finally:
        shutil.rmtree(work, ignore_errors=True)


class _PDWorker(QThread):
    log = Signal(str)
    checked = Signal(dict)
    updated = Signal(bool)

    def __init__(self, mode: str, prefix: Path, comps: List[dict], releases: Optional[dict] = None):
        super().__init__()
        self.mode, self.prefix, self.comps = mode, prefix, comps
        self.releases = releases or {}

    def run(self):
        try:
            if self.mode == "check":
                out = {}
                for c in self.comps:
                    info = detect_installed(self.prefix, c)
                    rel, err = None, ""
                    try:
                        rel = _gh_release(c["repo"])
                    except Exception as ex:
                        err = str(ex)
                    out[c["id"]] = {"installed": info, "release": rel, "error": err,
                                    "gaps": dll_override_gaps(self.prefix, c)}
                self.checked.emit(out)
            else:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup = self.prefix / ".drstool-backup" / stamp
                ok = True
                for c in self.comps:
                    rel = self.releases.get(c["id"])
                    self.log.emit(f"{c['name']} → {rel.get('tag_name', '?') if rel else '?'}")
                    try:
                        n = apply_component_update(self.prefix, c, rel, backup, self.log.emit)
                        self.log.emit(f"  ✔ {n} file(s) installed")
                    except Exception as ex:
                        ok = False
                        self.log.emit(f"  ✘ {ex}")
                if backup.exists():
                    self.log.emit(f"Backup of replaced DLLs: {backup}")
                self.updated.emit(ok)
        except Exception as ex:
            self.log.emit(f"✘ {ex}")
            if self.mode == "check":
                self.checked.emit({})
            else:
                self.updated.emit(False)


class PrefixDoctorWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: Optional[_PDWorker] = None
        self._state: dict = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        t = QLabel("Prefix doctor")
        t.setStyleSheet(f"color:{T['text']}; font-size:15px; font-weight:700;")
        lay.addWidget(t)
        d = QLabel("Compares the DXVK, vkd3d-proton and dxvk-nvapi DLLs inside a Wine prefix with the "
                   "latest GitHub release and replaces outdated ones (originals are backed up). "
                   "Versions are read from the DLLs heuristically; DLLs installed by this tool are "
                   "tracked exactly. Proton prefixes are re-synced by Proton on launch — use this "
                   "for Wine / Lutris / Bottles / Heroic prefixes.")
        d.setWordWrap(True)
        d.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(d)

        row = QHBoxLayout()
        self._combo = QComboBox()
        self._combo.setEditable(True)
        self._combo.setMinimumWidth(320)
        self._combo.lineEdit().setPlaceholderText("Path to a Wine prefix (contains drive_c/)")
        row.addWidget(self._combo, 1)
        b = _tool_btn("Browse…")
        b.clicked.connect(self._browse)
        row.addWidget(b)
        b = _tool_btn("Find prefixes")
        b.clicked.connect(self._discover)
        row.addWidget(b)
        lay.addLayout(row)

        self._table = QTableWidget(len(PD_COMPONENTS), 5)
        self._table.setHorizontalHeaderLabels(["", "Component", "In prefix", "Latest", "Status"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self._table.setStyleSheet(
            f"QTableWidget{{background:{T['surface']};border:1px solid {T['border']};color:{T['text2']};"
            f"gridline-color:{T['border']};}}QHeaderView::section{{background:{T['raised']};"
            f"color:{T['muted']};border:none;padding:4px;}}")
        self._table.setFixedHeight(40 + 34 * len(PD_COMPONENTS))
        self._checks: Dict[str, QCheckBox] = {}
        for r, c in enumerate(PD_COMPONENTS):
            cb = QCheckBox()
            cb.setEnabled(False)
            cb.toggled.connect(lambda _c: self._set_busy(False))
            self._checks[c["id"]] = cb
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(8, 0, 0, 0)
            hl.addWidget(cb)
            self._table.setCellWidget(r, 0, holder)
            self._table.setItem(r, 1, QTableWidgetItem(c["name"]))
            for col in (2, 3, 4):
                self._table.setItem(r, col, QTableWidgetItem("—"))
        lay.addWidget(self._table)

        row = QHBoxLayout()
        self._btn_check = _tool_btn("Check prefix", bold=True)
        self._btn_check.clicked.connect(self.check)
        row.addWidget(self._btn_check)
        self._btn_upd = _tool_btn("Update selected", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        self._btn_upd.clicked.connect(self.update_selected)
        self._btn_upd.setEnabled(False)
        row.addWidget(self._btn_upd)
        b = _tool_btn("Restore last backup")
        b.clicked.connect(self._restore)
        row.addWidget(b)
        b = _tool_btn("Open prefix folder")
        b.clicked.connect(lambda: self._prefix() and QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._prefix()))))
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(_MONO_BOX_QSS)
        self._log.setMinimumHeight(140)
        lay.addWidget(self._log, 1)
        QTimer.singleShot(0, self._discover)

    # -- helpers --------------------------------------------------------------
    def _say(self, msg: str):
        self._log.appendPlainText(msg)

    def _prefix(self) -> Optional[Path]:
        txt = self._combo.currentText().strip()
        i = self._combo.findText(txt)
        if i >= 0 and self._combo.itemData(i):
            txt = self._combo.itemData(i)
        if not txt:
            return None
        p = Path(txt).expanduser()
        return p if (p / "drive_c" / "windows").is_dir() else None

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select Wine prefix", str(Path.home()))
        if d:
            self._combo.setEditText(d)
            self.check()

    def _discover(self):
        cur = self._combo.currentText()
        self._combo.clear()
        for label, p in find_prefixes():
            self._combo.addItem(f"{label}   —   {p}", str(p))
        if cur:
            self._combo.setEditText(cur)
        if self._combo.count() and not cur:
            self._combo.setCurrentIndex(0)

    def _set_busy(self, busy: bool):
        self._btn_check.setEnabled(not busy)
        self._btn_upd.setEnabled(not busy and any(c.isEnabled() and c.isChecked() for c in self._checks.values()))

    # -- check ----------------------------------------------------------------
    def check(self):
        p = self._prefix()
        if p is None:
            self._say("⚠ Select a valid Wine prefix first (the folder that contains drive_c/).")
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._say(f"Checking {p} …")
        self._set_busy(True)
        self._worker = _PDWorker("check", p, PD_COMPONENTS)
        self._worker.checked.connect(self._on_checked)
        self._worker.log.connect(self._say)
        self._worker.start()

    def _on_checked(self, res: dict):
        self._state = res
        p = self._prefix()
        is_proton = bool(p and ((p.parent / "version").is_file() or p.name == "pfx"))
        for r, c in enumerate(PD_COMPONENTS):
            e = res.get(c["id"])
            cb = self._checks[c["id"]]
            cb.setChecked(False)
            cb.setEnabled(False)
            if not e:
                continue
            inst, rel, err = e["installed"], e["release"], e["error"]
            latest = (rel or {}).get("tag_name", "")
            vi = {"none": "not installed", "mixed": f"mixed: {inst['version']}"}.get(
                inst["state"], inst["version"] + (" (exact)" if inst["source"] == "manifest" else " (scan)"))
            status, can = "", False
            if rel is None:
                status = f"⚠ cannot reach GitHub: {err}"[:120]
            elif _pick_asset(rel, c) is None:
                status = "⚠ no matching release asset"
            elif inst["state"] == "none":
                status, can = "Wine builtin / missing — can install", True
            elif inst["state"] in ("mixed", "unknown"):
                status, can = "version unclear — reinstall recommended", True
            else:
                a, b = _vtuple(inst["version"]), _vtuple(latest)
                dev = bool(re.search(r"-\d+-g[0-9a-f]+", inst["version"]))
                if a and b:
                    cmp_ = _vcmp(a, b)
                    if cmp_ < 0:
                        status, can = "outdated", True
                    elif dev:
                        status = "up to date (dev build after the release)"
                    else:
                        status = "up to date" if cmp_ == 0 else "newer than latest release"
                else:
                    status, can = "version unreadable — update to be sure", True
            gaps = e.get("gaps") or []
            if gaps and not is_proton and inst["state"] != "none":
                status += f"   (no native override: {', '.join(gaps[:4])}{'…' if len(gaps) > 4 else ''})"
            self._table.item(r, 2).setText(vi)
            self._table.item(r, 3).setText(latest.lstrip("v") or "?")
            self._table.item(r, 4).setText(status)
            col = T["warn"] if can else (T["accent"] if status.startswith("up to date") else T["muted"])
            self._table.item(r, 4).setForeground(QColor(col))
            cb.setEnabled(can)
            cb.setChecked(can and inst["state"] != "none")
        if is_proton:
            self._say("Note: this looks like a Proton prefix — Proton re-copies its own DXVK/VKD3D on "
                      "version change; a manual update may be overwritten.")
        gaps_any = any((res.get(c["id"]) or {}).get("gaps") for c in PD_COMPONENTS)
        if gaps_any and not is_proton:
            self._say("Hint: DLLs without a native override are ignored by Wine. Run once with e.g. "
                      "WINEDLLOVERRIDES=\"d3d11,dxgi,d3d9,d3d10core=n,b\" or set them in winecfg.")
        self._say("Check finished.")
        self._set_busy(False)

    # -- update ---------------------------------------------------------------
    def update_selected(self):
        p = self._prefix()
        if p is None:
            return
        comps = [c for c in PD_COMPONENTS if self._checks[c["id"]].isChecked()]
        if not comps:
            self._say("Nothing selected.")
            return
        if QMessageBox.question(
                self, "Update prefix",
                f"Replace {', '.join(c['name'] for c in comps)} DLLs in\n{p}\n\n"
                "Replaced files are backed up inside the prefix (.drstool-backup).",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        rels = {c["id"]: self._state.get(c["id"], {}).get("release") for c in comps}
        self._set_busy(True)
        self._worker = _PDWorker("update", p, comps, rels)
        self._worker.log.connect(self._say)
        self._worker.updated.connect(self._on_updated)
        self._worker.start()

    def _on_updated(self, ok: bool):
        self._say("Done." if ok else "Finished with errors — see log.")
        self._set_busy(False)
        self.check()

    def _restore(self):
        p = self._prefix()
        if p is None:
            return
        bdir = p / ".drstool-backup"
        stamps = sorted(x for x in bdir.iterdir() if x.is_dir()) if bdir.is_dir() else []
        if not stamps:
            self._say("No backups in this prefix.")
            return
        last = stamps[-1]
        if QMessageBox.question(self, "Restore backup", f"Copy the DLLs from\n{last}\nback into the prefix?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        win = p / "drive_c" / "windows"
        n = 0
        for sub in ("system32", "syswow64"):
            for f in (last / sub).glob("*.dll") if (last / sub).is_dir() else []:
                shutil.copy2(f, win / sub / f.name)
                n += 1
        try:
            (p / _PD_MANIFEST).unlink()
        except OSError:
            pass
        self._say(f"Restored {n} file(s) from {last.name}.")
        self.check()


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


# ============================================================================
# Linter / System panels and the Ctrl+K palette
# ============================================================================

_SEV_STYLE = {"error": ("●", "#d05050"), "warn": ("▲", "#d2a03c"), "info": ("ℹ", "#7d8597")}


class LinterWidget(QWidget):
    fix_requested = Signal(object)          # LintIssue

    def __init__(self, parent=None):
        super().__init__(parent)
        self._issues: List[LintIssue] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        head = QHBoxLayout()
        self._summary = QLabel("Consistency check")
        self._summary.setStyleSheet(f"color:{T['text']}; font-size:15px; font-weight:700;")
        head.addWidget(self._summary, 1)
        b = _tool_btn("Re-run")
        b.clicked.connect(lambda: hasattr(self.window(), "_run_lint") and self.window()._run_lint())
        head.addWidget(b)
        lay.addLayout(head)
        d = QLabel("Looks for combinations that contradict each other or can't work on this "
                   "system (PROTON_NVIDIA_LIBS + WoW64, FSR4 on NVIDIA, ENABLE_HDR_WSI on drivers ≥ 595, "
                   "double latency stacks, …). Runs automatically whenever the configuration changes.")
        d.setWordWrap(True)
        d.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(d)
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["", "Finding", ""])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        self._table.setWordWrap(True)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._table.setStyleSheet(
            f"QTableWidget{{background:{T['surface']};border:1px solid {T['border']};color:{T['text2']};"
            f"gridline-color:{T['border']};}}QHeaderView::section{{background:{T['raised']};"
            f"color:{T['muted']};border:none;padding:4px;}}")
        lay.addWidget(self._table, 1)

    def set_issues(self, issues: List[LintIssue]):
        self._issues = issues
        t = self._table
        t.setRowCount(0)
        n_e = sum(1 for i in issues if i.sev == "error")
        n_w = sum(1 for i in issues if i.sev == "warn")
        n_i = len(issues) - n_e - n_w
        self._summary.setText("Consistency check — " + (
            "all clear ✔" if not issues else f"{n_e} error(s), {n_w} warning(s), {n_i} note(s)"))
        for row, iss in enumerate(issues):
            t.insertRow(row)
            sym, col = _SEV_STYLE.get(iss.sev, ("•", T["muted"]))
            s = QTableWidgetItem(sym)
            s.setForeground(QColor(col))
            s.setTextAlignment(Qt.AlignCenter)
            t.setItem(row, 0, s)
            txt = iss.msg + (f"\n  → {', '.join(iss.involved)}" if iss.involved else "")
            m = QTableWidgetItem(txt)
            t.setItem(row, 1, m)
            if iss.fixes:
                b = _tool_btn(iss.fix_label or "Fix")
                b.clicked.connect(lambda _c=False, i=iss: self.fix_requested.emit(i))
                t.setCellWidget(row, 2, b)
        t.resizeRowsToContents()


class SystemInfoWidget(QWidget):
    WATCH = [("VK_NV_low_latency2", "Reflex / DXVK low-latency"),
             ("VK_EXT_descriptor_heap", "descriptor heap (DXVK / VKD3D flag)"),
             ("VK_EXT_descriptor_buffer", "descriptor buffer"),
             ("VK_EXT_hdr_metadata", "HDR metadata"),
             ("VK_EXT_swapchain_colorspace", "HDR colour spaces (instance)"),
             ("VK_NV_present_barrier", "present barrier"),
             ("VK_KHR_present_wait", "present wait"),
             ("VK_KHR_present_id", "present id"),
             ("VK_EXT_swapchain_maintenance1", "swapchain maintenance"),
             ("VK_NV_optical_flow", "optical flow (frame gen)"),
             ("VK_NV_cooperative_matrix2", "cooperative matrix 2"),
             ("VK_EXT_mesh_shader", "mesh shaders")]

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        t = QLabel("System")
        t.setStyleSheet(f"color:{T['text']}; font-size:15px; font-weight:700;")
        lay.addWidget(t)
        self._grey = QCheckBox("Grey out options this system can't run (you can still force them with “Edit anyway”)")
        self._grey.setChecked(SYSHUB.enabled)
        self._grey.setStyleSheet(f"color:{T['text2']};")
        self._grey.toggled.connect(SYSHUB.set_enabled)
        lay.addWidget(self._grey)
        row = QHBoxLayout()
        b = _tool_btn("Re-probe")
        b.clicked.connect(SYSHUB.refresh)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setStyleSheet(_MONO_BOX_QSS)
        lay.addWidget(self._text, 1)
        SYSHUB.info_changed.connect(self.update_info)
        self.update_info(SYSHUB.info)

    def update_info(self, si: SysInfo):
        L = []
        if not si.probed:
            L.append("Probing…")
        else:
            L.append(f"NVIDIA driver : {si.nv_driver or 'not detected'}"
                     + (f"   ({si.nv_kernel_flavor})" if si.nv_kernel_flavor else ""))
            if si.nv_gpus:
                L.append("nvidia-smi    : " + "; ".join(si.nv_gpus))
            L.append(f"Session       : {si.session or 'unknown'}")
            L.append(f"/dev/ntsync   : {'present' if si.ntsync_dev else 'missing'}")
            for tname in ("gamescope", "mangohud", "vulkaninfo", "nvidia-smi", "wine", "zstd"):
                L.append(f"{tname:<13} : {si.tools.get(tname, '—')}")
            L.append("")
            if si.vk_ok:
                for i, g in enumerate(si.vk_gpus):
                    L.append(f"Vulkan GPU{i}   : {g.name}  [{g.driver or 'driver ?'}]  {len(g.exts)} device exts")
                L.append("")
                L.append("Watched extensions (NVIDIA device if present, else all):")
                for ext, what in self.WATCH:
                    v = si.has_ext(ext)
                    L.append(f"  {'✔' if v else '✘'}  {ext:<34} {what}")
            else:
                L.append(f"Vulkan        : {si.vulkaninfo_error or 'unknown'}")
                L.append("  (nothing is greyed out while Vulkan data is unavailable)")
        self._text.setPlainText("\n".join(L))


class CommandPalette(QDialog):
    """Ctrl+K: search every DRS setting / env var / GPU arch / tool page."""

    def __init__(self, items: List[dict], parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        self.setStyleSheet(f"QDialog{{background:{T['raised']};border:1px solid {T['accent_dim']};border-radius:8px;}}")
        self.resize(720, 480)
        self._items = items
        self.chosen: Optional[dict] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText("Search settings, env vars, GPU archs, tools…   "
                                      "(Enter: open · Ctrl+D: favorite · Esc: close)")
        self._edit.setStyleSheet(f"QLineEdit{{background:{T['surface']};border:1px solid {T['border_hi']};"
                                 f"color:{T['text']};padding:6px 10px;border-radius:5px;font-size:14px;}}")
        self._edit.textChanged.connect(self._refill)
        self._edit.installEventFilter(self)
        lay.addWidget(self._edit)
        row = QHBoxLayout()
        self._only_set = QCheckBox("only set")
        self._only_fav = QCheckBox("★ favorites")
        for c in (self._only_set, self._only_fav):
            c.setStyleSheet(f"color:{T['text2']};")
            c.toggled.connect(lambda _c: self._refill())
            row.addWidget(c)
        row.addStretch(1)
        self._count = QLabel("")
        self._count.setStyleSheet(f"color:{T['muted']}; font-size:11px;")
        row.addWidget(self._count)
        lay.addLayout(row)
        self._list = QListWidget()
        self._list.setStyleSheet(
            f"QListWidget{{background:{T['bg']};border:1px solid {T['border']};border-radius:5px;outline:none;}}"
            f"QListWidget::item{{padding:5px 8px;color:{T['text2']};}}"
            f"QListWidget::item:selected{{background:rgba(118,185,0,0.20);color:{T['text']};}}")
        self._list.itemActivated.connect(self._accept_item)
        self._list.itemDoubleClicked.connect(self._accept_item)
        lay.addWidget(self._list, 1)
        self._refill()
        self._edit.setFocus()

    @staticmethod
    def _score(q: List[str], it: dict) -> int:
        name = it["title"].lower()
        hay = f'{name} {it.get("key", "").lower()} {it.get("sub", "").lower()} {it.get("desc", "").lower()}'
        s = 0
        for tok in q:
            if tok in name:
                s += 100 - min(60, name.index(tok)) + (30 if name.startswith(tok) else 0)
            elif tok in hay:
                s += 25
            else:
                # subsequence fallback on the name
                i = 0
                for ch in name:
                    if i < len(tok) and ch == tok[i]:
                        i += 1
                if i == len(tok) and len(tok) >= 3:
                    s += 8
                else:
                    return -1
        return s

    def _refill(self):
        q = self._edit.text().lower().split()
        scored = []
        for it in self._items:
            if self._only_set.isChecked() and not it.get("is_set"):
                continue
            if self._only_fav.isChecked() and not it.get("fav"):
                continue
            sc = self._score(q, it) if q else (50 if it.get("fav") else 10 if it.get("is_set") else 0)
            if sc < 0:
                continue
            if it.get("fav"):
                sc += 15
            if it.get("is_set"):
                sc += 10
            scored.append((-sc, it["title"].lower(), it))
        scored.sort(key=lambda x: (x[0], x[1]))
        self._list.clear()
        for _s, _n, it in scored[:300]:
            star = "★ " if it.get("fav") else "   "
            dot = "● " if it.get("is_set") else "   "
            val = f'   = {it["value"]}' if it.get("value") else ""
            li = QListWidgetItem(f'{star}{dot}{it["title"]}{val}    ·  {it.get("sub", "")}')
            li.setData(Qt.UserRole, it)
            if it.get("unavail"):
                li.setForeground(QColor(T["muted"]))
                li.setToolTip(it["unavail"])
            self._list.addItem(li)
        self._count.setText(f"{min(len(scored), 300)} of {len(self._items)}")
        if self._list.count():
            self._list.setCurrentRow(0)

    def _accept_item(self, li):
        self.chosen = li.data(Qt.UserRole)
        self.accept()

    def eventFilter(self, obj, ev):           # noqa: N802
        if obj is self._edit and ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k in (Qt.Key_Down, Qt.Key_Up):
                r = self._list.currentRow() + (1 if k == Qt.Key_Down else -1)
                self._list.setCurrentRow(max(0, min(self._list.count() - 1, r)))
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if self._list.currentItem():
                    self._accept_item(self._list.currentItem())
                return True
            if k == Qt.Key_D and ev.modifiers() & Qt.ControlModifier:
                self._toggle_fav()
                return True
        return super().eventFilter(obj, ev)

    def _toggle_fav(self):
        li = self._list.currentItem()
        if not li:
            return
        it = li.data(Qt.UserRole)
        if it.get("fav_kind") and it.get("fav_key"):
            it["fav"] = FAVS.toggle(it["fav_kind"], it["fav_key"])
            row = self._list.currentRow()
            self._refill()
            self._list.setCurrentRow(min(row, self._list.count() - 1))


# ============================================================================
# Feature pack 2 — history / diff / restore, shader-cache manager,
# NVIDIA Profile Inspector (.nip) import, upstream drift check.
# Self-contained: touches the rest of the program only through
# SettingsManager (snapshots / restore) and a few tab registrations.
# ============================================================================

import difflib
from PySide6.QtWidgets import QTextEdit, QTreeWidget, QTreeWidgetItem

_SETTING_BY_ID: Dict[str, "Setting"] = {s.id.upper(): s for s in ALL_SETTINGS}


def _fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def _pack2_table_ss() -> str:
    return (f"QTableWidget,QTreeWidget{{background:{T['surface']};border:1px solid {T['border']};"
            f"color:{T['text2']};gridline-color:{T['border']};}}"
            f"QHeaderView::section{{background:{T['raised']};color:{T['muted']};border:none;padding:4px;}}"
            f"QTreeWidget::item:selected,QTableWidget::item:selected{{background:rgba(118,185,0,0.20);"
            f"color:{T['text']};}}")


def _pack2_title(text: str, sub: str = "") -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    t = QLabel(text)
    t.setStyleSheet(f"color:{T['text']}; font-size:15px; font-weight:700;")
    v.addWidget(t)
    if sub:
        d = QLabel(sub)
        d.setWordWrap(True)
        d.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        v.addWidget(d)
    return w


# ----------------------------------------------------------------------------
# Profile flatten / diff
# ----------------------------------------------------------------------------

def _flat_profile(p) -> Dict[tuple, str]:
    out: Dict[tuple, str] = {}
    if not isinstance(p, dict):
        return out
    for k, v in _as_dict(p.get("settings")).items():
        out[("DRS", str(k).upper())] = str(v)
    if p.get("arch"):
        out[("Arch", "GPU arch")] = str(p["arch"])
    for k, v in _as_dict(p.get("env_vars")).items():
        out[("Env", str(k))] = str(v)
    if p.get("gamescope_enabled"):
        out[("Gamescope", "enabled")] = "on"
    for k, v in _as_dict(p.get("gamescope_flags")).items():
        out[("Gamescope", str(k))] = str(v)
    return out


def diff_profiles(a, b) -> List[tuple]:
    """(section, key, value_in_a, value_in_b) for every difference; None = absent."""
    fa, fb = _flat_profile(a), _flat_profile(b)
    order = {"Arch": 0, "DRS": 1, "Env": 2, "Gamescope": 3}
    rows = []
    for key in sorted(set(fa) | set(fb), key=lambda k: (order.get(k[0], 9), k[1])):
        va, vb = fa.get(key), fb.get(key)
        if va != vb:
            rows.append((key[0], key[1], va, vb))
    return rows


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

    def _on_profiles_changed(self):
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
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
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


# ----------------------------------------------------------------------------
# NVIDIA Profile Inspector (.nip) import
# ----------------------------------------------------------------------------

def parse_nip(text: str) -> List[dict]:
    """Parse a Profile Inspector export. Returns [{name, exes, settings:[{id,value,vtype,label}]}]."""
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
        raise ValueError("DTD / entity declarations are not allowed in a .nip file")
    root = ET.fromstring(text)
    out = []
    for prof in root.iter("Profile"):
        settings = []
        for ps in prof.findall("./Settings/ProfileSetting"):
            try:
                sid = int((ps.findtext("SettingID") or "").strip(), 0)
            except ValueError:
                continue
            raw = (ps.findtext("SettingValue") or "").strip()
            vtype = (ps.findtext("ValueType") or "Dword").strip()
            val: Optional[int] = None
            if vtype.lower() == "dword":
                try:
                    val = int(raw, 0) & 0xFFFFFFFF
                except ValueError:
                    val = None
            settings.append({"id": sid, "value": val, "raw": raw, "vtype": vtype,
                             "label": (ps.findtext("SettingNameInfo") or "").strip()})
        out.append({"name": (prof.findtext("ProfileName") or "").strip(),
                    "exes": [(e.text or "").strip() for e in prof.findall("./Executeables/string")],
                    "settings": settings})
    return out


def decode_nip_bytes(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig", errors="replace")


class NipImportWidget(QWidget):
    COL_USE, COL_NAME, COL_ID, COL_VAL, COL_STATUS = range(5)

    def __init__(self, settings_manager: "SettingsManager"):
        super().__init__()
        self.sm = settings_manager
        self._profiles: List[dict] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.addWidget(_pack2_title(
            "Import NVIDIA Profile Inspector (.nip)",
            "Reads a .nip export and converts its DWORD settings into DXVK_NVAPI_DRS_SETTINGS pairs "
            "(0xID=0xVALUE). Settings are applied to the editor — nothing is written to the driver. "
            "String / binary values cannot be expressed in DXVK_NVAPI_DRS_SETTINGS and are skipped."))
        row = QHBoxLayout()
        b = _tool_btn("Open .nip…", bold=True)
        b.clicked.connect(self._open)
        row.addWidget(b)
        self._prof_combo = QComboBox()
        self._prof_combo.currentIndexChanged.connect(self._fill)
        row.addWidget(self._prof_combo, 1)
        lay.addLayout(row)
        self._info = QLabel("")
        self._info.setWordWrap(True)
        self._info.setStyleSheet(f"color:{T['muted']};font-size:12px;")
        lay.addWidget(self._info)
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["Use", "Setting", "ID", "Value", "Status"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(self.COL_NAME, QHeaderView.Stretch)
        for c in (self.COL_USE, self.COL_ID, self.COL_VAL, self.COL_STATUS):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self._table.setStyleSheet(_pack2_table_ss())
        self._table.itemChanged.connect(lambda _i: self._update_preview())
        lay.addWidget(self._table, 1)
        self._preview = QLineEdit()
        self._preview.setReadOnly(True)
        self._preview.setStyleSheet(
            f"QLineEdit{{background:{T['bg']};border:1px solid {T['border_hi']};color:{T['text']};"
            f"font-family:{T['mono']};padding:4px 6px;}}")
        lay.addWidget(self._preview)
        r2 = QHBoxLayout()
        self._mode = QComboBox()
        self._mode.addItems(["Merge into current settings", "Replace current DRS settings"])
        r2.addWidget(self._mode)
        self._btn_apply = _tool_btn("Apply to editor", bold=True)
        self._btn_apply.clicked.connect(self._apply)
        self._btn_apply.setEnabled(False)
        b_copy = _tool_btn("Copy DXVK_NVAPI_DRS_SETTINGS=…")
        b_copy.clicked.connect(lambda: QApplication.clipboard().setText(self._preview.text()))
        r2.addWidget(self._btn_apply)
        r2.addWidget(b_copy)
        r2.addStretch(1)
        lay.addLayout(r2)

    def _open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "NVIDIA Profile Inspector export", str(Path.home()), "Profile Inspector (*.nip);;All files (*)")
        if path:
            self.load_file(Path(path))

    def load_file(self, path: Path):
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("file is larger than 8 MiB")
            self._profiles = parse_nip(decode_nip_bytes(path.read_bytes()))
        except Exception as e:
            self._profiles = []
            self._prof_combo.clear()
            self._table.setRowCount(0)
            self._info.setText(f"Could not read {path.name}: {e}")
            self._update_preview()
            return
        self._prof_combo.blockSignals(True)
        self._prof_combo.clear()
        for p in self._profiles:
            self._prof_combo.addItem(f'{p["name"] or "(unnamed)"}  ·  {len(p["settings"])} setting(s)')
        self._prof_combo.blockSignals(False)
        self._info.setText(f"{path.name}: {len(self._profiles)} profile(s)")
        self._fill()

    @staticmethod
    def _fmt_value(v: int) -> str:
        return f"0x{v:X}"

    def _fill(self, *_):
        t = self._table
        t.blockSignals(True)
        t.setRowCount(0)
        i = self._prof_combo.currentIndex()
        if not (0 <= i < len(self._profiles)):
            t.blockSignals(False)
            self._update_preview()
            return
        prof = self._profiles[i]
        exes = ", ".join(e for e in prof["exes"] if e)
        self._info.setText((f"Profile applies to: {exes}" if exes else "Profile has no executables listed")
                           + "  ·  ✓ rows are converted")
        for r, s in enumerate(prof["settings"]):
            t.insertRow(r)
            key = f'0x{s["id"]:08X}'
            known = _SETTING_BY_ID.get(key)
            ok = s["value"] is not None
            if not ok:
                status = f'skipped: {s["vtype"]} value'
            elif known is None:
                status = "not in DRSTool catalog (kept as raw pair)"
            else:
                status = "known"
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled if ok else Qt.NoItemFlags)
            chk.setCheckState(Qt.Checked if ok else Qt.Unchecked)
            chk.setData(Qt.UserRole, (key, self._fmt_value(s["value"]) if ok else None))
            t.setItem(r, self.COL_USE, chk)
            name = known.name if known else (s["label"] or "(unknown)")
            val_txt = (f"{_fmt_drs_value(known, self._fmt_value(s['value']))}  [{self._fmt_value(s['value'])}]"
                       if ok and known and _fmt_drs_value(known, self._fmt_value(s['value'])) != self._fmt_value(s['value'])
                       else (self._fmt_value(s["value"]) if ok else s["raw"][:40]))
            for c, txt in ((self.COL_NAME, name), (self.COL_ID, key), (self.COL_VAL, val_txt),
                           (self.COL_STATUS, status)):
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemIsEnabled)
                if not ok:
                    it.setForeground(QColor(T["muted"]))
                elif known is None and c == self.COL_STATUS:
                    it.setForeground(QColor(T["warn"]))
                t.setItem(r, c, it)
        t.blockSignals(False)
        self._update_preview()

    def _checked_pairs(self) -> List[tuple]:
        pairs = []
        for r in range(self._table.rowCount()):
            it = self._table.item(r, self.COL_USE)
            if it is not None and it.checkState() == Qt.Checked:
                k, v = it.data(Qt.UserRole)
                if v is not None:
                    pairs.append((k, v))
        return pairs

    def _update_preview(self):
        pairs = self._checked_pairs()
        self._preview.setText("DXVK_NVAPI_DRS_SETTINGS=" + ",".join(f"{k}={v}" for k, v in pairs)
                              if pairs else "")
        self._btn_apply.setEnabled(bool(pairs))

    def _apply(self):
        pairs = self._checked_pairs()
        if not pairs:
            return
        replace = self._mode.currentIndex() == 1
        if replace and QMessageBox.question(
                self, "Replace settings",
                "Clear all current DRS settings in the editor before applying?") != QMessageBox.Yes:
            return
        if replace:
            self.sm.clear_all()
        for k, v in pairs:
            self.sm.set_setting(k, v)
        self._info.setText(f"Applied {len(pairs)} setting(s) to the editor.")


# ----------------------------------------------------------------------------
# Shader / pipeline cache manager
# ----------------------------------------------------------------------------

def _path_stats(path: str) -> tuple:
    """(disk bytes, file count, newest mtime) of a file or directory tree; symlinks not followed."""
    total = files = 0
    newest = 0.0
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0, 0.0
    if not os.path.isdir(path) or os.path.islink(path):
        return getattr(st, "st_blocks", 0) * 512 or st.st_size, 1, st.st_mtime
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                            continue
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += getattr(s, "st_blocks", 0) * 512 or s.st_size
                    files += 1
                    if s.st_mtime > newest:
                        newest = s.st_mtime
        except OSError:
            continue
    return total, files, newest


def _steam_roots() -> List[Path]:
    home = Path.home()
    cands = [home / ".local/share/Steam", home / ".steam/steam", home / ".steam/root",
             home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    seen, out = set(), []
    for c in cands:
        try:
            rp = c.resolve()
        except OSError:
            continue
        if rp.is_dir() and rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def _steam_libraries() -> List[Path]:
    libs, seen = [], set()
    for root in _steam_roots():
        paths = [root]
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            paths += [Path(m) for m in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace"))]
        except OSError:
            pass
        for p in paths:
            try:
                rp = p.resolve()
            except OSError:
                continue
            if rp not in seen and (rp / "steamapps").is_dir():
                seen.add(rp)
                libs.append(rp)
    return libs


def _classify_cache_entry(name: str) -> tuple:
    """(group, driver_bound) for an entry inside steamapps/shadercache/<appid>/."""
    n = name.lower()
    if n.startswith("nvidia"):
        return "NVIDIA GL / compute", True
    if n.startswith("vkd3d"):
        return "vkd3d-proton", True
    if "dxvk" in n:
        return "DXVK", False
    if n.startswith(("mesa", "radv", "foz", "fossilize")):
        return "Mesa / Fossilize", False
    return "Other", False


CACHE_GROUP_ORDER = ["NVIDIA GL / compute", "vkd3d-proton", "DXVK", "Mesa / Fossilize",
                     "Lutris (per game)", "Custom folders", "Other"]


def discover_cache_nodes(user_env: Dict[str, str], extra: List[str]) -> List[dict]:
    """Every cache location we know about. Pure filesystem work — safe in a thread."""
    home = Path.home()
    xdg_cache = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))

    def env(name: str) -> str:
        return (user_env.get(name) or os.environ.get(name) or "").strip()

    nodes: List[dict] = []
    seen = set()

    def add(group: str, label: str, path, driver: bool = False, note: str = ""):
        p = str(path)
        try:
            rp = os.path.realpath(p)
        except OSError:
            return
        if rp in seen or not os.path.exists(p):
            return
        seen.add(rp)
        nodes.append({"group": group, "label": label, "path": p, "driver": driver, "note": note})

    # NVIDIA driver caches (keyed by driver version → stale after an update)
    for var in ("__GL_SHADER_DISK_CACHE_PATH",):
        if env(var):
            add("NVIDIA GL / compute", f"{var} (your setting)", env(var), True)
    add("NVIDIA GL / compute", "OpenGL/Vulkan shader cache (~/.nv/GLCache)", home / ".nv/GLCache", True)
    add("NVIDIA GL / compute", "OpenGL/Vulkan shader cache (~/.cache/nvidia/GLCache)",
        xdg_cache / "nvidia/GLCache", True)
    add("NVIDIA GL / compute", "CUDA compute cache (~/.nv/ComputeCache)", home / ".nv/ComputeCache", True)
    add("NVIDIA GL / compute", "CUDA compute cache (~/.cache/nvidia/ComputeCache)",
        xdg_cache / "nvidia/ComputeCache", True)
    # Mesa
    md = env("MESA_SHADER_CACHE_DIR")
    for base in ([Path(md)] if md else []) + [xdg_cache]:
        add("Mesa / Fossilize", f"Mesa shader cache ({base})", base / "mesa_shader_cache")
        add("Mesa / Fossilize", f"Mesa shader cache DB ({base})", base / "mesa_shader_cache_db")
    add("Mesa / Fossilize", "RADV built-in shaders", xdg_cache / "radv_builtin_shaders")
    # user-configured paths
    for var, grp, drv in (("DXVK_STATE_CACHE_PATH", "DXVK", False), ("DXVK_SHADER_CACHE_PATH", "DXVK", False),
                          ("VKD3D_SHADER_CACHE_PATH", "vkd3d-proton", True)):
        v = env(var)
        if v and v != "0":
            add(grp, f"{var} (your setting)", v, drv)
    # Lutris keeps per-game shader caches here
    lut = xdg_cache / "lutris" / "shaders"
    if lut.is_dir():
        try:
            for sub in sorted(lut.iterdir()):
                add("Lutris (per game)", sub.name, sub)
        except OSError:
            pass
    # Steam: shadercache/<appid>/<kind>
    for lib in _steam_libraries():
        sc = lib / "steamapps" / "shadercache"
        if not sc.is_dir():
            continue
        try:
            apps = sorted(sc.iterdir())
        except OSError:
            continue
        for app in apps:
            if not app.name.isdigit() or not app.is_dir():
                continue
            gname = app.name
            try:
                m = re.search(r'"name"\s+"([^"]+)"',
                              (lib / "steamapps" / f"appmanifest_{app.name}.acf").read_text(errors="replace"))
                if m:
                    gname = f"{m.group(1)} ({app.name})"
            except OSError:
                pass
            try:
                entries = sorted(app.iterdir())
            except OSError:
                continue
            for ent in entries:
                grp, drv = _classify_cache_entry(ent.name)
                add(grp, f"{gname} — {ent.name}", ent, drv)
    for p in extra:
        add("Custom folders", p, p)
    return nodes


def _safe_cache_path(p: str, extra: List[str]) -> bool:
    """Refuse to delete anything that is not clearly a cache location."""
    try:
        rp = Path(os.path.realpath(p))
        home = Path(os.path.realpath(Path.home()))
    except OSError:
        return False
    if rp == home or rp == Path("/") or len(rp.parts) < 4:
        return False
    if p in extra:
        return True
    return home in rp.parents or "shadercache" in rp.parts


class _CacheScanThread(QThread):
    done = Signal(object)

    def __init__(self, user_env: Dict[str, str], extra: List[str]):
        super().__init__()
        self._env, self._extra = user_env, extra

    def run(self):
        nodes = discover_cache_nodes(self._env, self._extra)
        for n in nodes:
            n["size"], n["files"], n["mtime"] = _path_stats(n["path"])
        self.done.emit(nodes)


class CacheManagerWidget(QWidget):
    driver_changed = Signal(str)      # human message, emitted when the driver differs from the last acknowledged one
    stale_changed = Signal(bool)

    CACHE_ENV = ("__GL_SHADER_DISK_CACHE", "__GL_SHADER_DISK_CACHE_SIZE", "__GL_SHADER_DISK_CACHE_PATH",
                 "__GL_SHADER_DISK_CACHE_SKIP_CLEANUP", "DXVK_STATE_CACHE_PATH", "DXVK_SHADER_CACHE_PATH",
                 "DXVK_SHADER_CACHE", "VKD3D_SHADER_CACHE_PATH", "MESA_SHADER_CACHE_DIR")

    def __init__(self):
        super().__init__()
        self._thread: Optional[_CacheScanThread] = None
        self._nodes: List[dict] = []
        self._stale = False
        self._driver_msg_sent = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_pack2_title(
            "Shader / pipeline caches",
            "Sizes of the NVIDIA, Mesa, DXVK and vkd3d-proton caches DRSTool can find: the driver "
            "caches, Steam's shadercache/<appid> folders (every library), Lutris' per-game folder and "
            "the paths you set in the Env Vars tab. Use “Add folder…” for .dxvk-cache files next to "
            "game executables."))
        self._banner = QLabel("")
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(
            f"background:rgba(210,80,80,0.14);border:1px solid {T['danger']};border-radius:5px;"
            f"color:{T['text']};padding:6px 10px;font-size:12px;")
        self._banner.hide()
        lay.addWidget(self._banner)
        brow = QHBoxLayout()
        self._btn_clear_stale = _tool_btn("Clear driver-bound caches")
        self._btn_clear_stale.clicked.connect(self._clear_stale)
        self._btn_ack = _tool_btn("Keep & acknowledge")
        self._btn_ack.clicked.connect(self._acknowledge)
        brow.addWidget(self._btn_clear_stale)
        brow.addWidget(self._btn_ack)
        brow.addStretch(1)
        self._banner_btns = QWidget()
        self._banner_btns.setLayout(brow)
        self._banner_btns.hide()
        lay.addWidget(self._banner_btns)
        self._env_line = QLabel("")
        self._env_line.setWordWrap(True)
        self._env_line.setStyleSheet(f"color:{T['muted']};font-size:12px;")
        lay.addWidget(self._env_line)
        self._tree = QTreeWidget()
        self._tree.setColumnCount(5)
        self._tree.setHeaderLabels(["Cache", "Size", "Files", "Last written", "Path"])
        self._tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self._tree.setUniformRowHeights(True)
        self._tree.setStyleSheet(_pack2_table_ss())
        hdr = self._tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Interactive)
        self._tree.setColumnWidth(4, 260)
        lay.addWidget(self._tree, 1)
        row = QHBoxLayout()
        b_scan = _tool_btn("Rescan", bold=True)
        b_scan.clicked.connect(self.rescan)
        b_add = _tool_btn("Add folder…")
        b_add.clicked.connect(self._add_folder)
        b_open = _tool_btn("Open folder")
        b_open.clicked.connect(self._open_selected)
        b_clear = _tool_btn("Clear selected")
        b_clear.clicked.connect(self._clear_selected)
        for b in (b_scan, b_add, b_open, b_clear):
            row.addWidget(b)
        row.addStretch(1)
        self._total = QLabel("")
        self._total.setStyleSheet(f"color:{T['text2']};font-size:12px;")
        row.addWidget(self._total)
        lay.addLayout(row)
        SYSHUB.info_changed.connect(lambda _i: self._driver_check())
        QTimer.singleShot(600, self.rescan)

    # ── data ──────────────────────────────────────────────────────────────
    def _user_env(self) -> Dict[str, str]:
        win = self.window()
        w = getattr(win, "_env_widget", None)
        try:
            return dict(w.get_env_dict()) if w is not None else {}
        except Exception:
            return {}

    @staticmethod
    def _extra_folders() -> List[str]:
        try:
            v = QSettings(APP_ID, APP_ID).value("cache/extra_folders", [])
            if isinstance(v, str):
                v = [v] if v else []
            return [str(x) for x in (v or [])]
        except Exception:
            return []

    def _save_extra(self, lst: List[str]):
        try:
            QSettings(APP_ID, APP_ID).setValue("cache/extra_folders", list(lst))
        except Exception:
            pass

    def rescan(self):
        if self._thread is not None and self._thread.isRunning():
            return
        env = self._user_env()
        shown = [f"{k}={env[k]}" for k in self.CACHE_ENV if env.get(k)]
        self._env_line.setText("Cache variables set in the Env Vars tab: " + "  ".join(shown)
                               if shown else "No cache-related variables are set in the Env Vars tab.")
        self._total.setText("Scanning…")
        self._thread = _CacheScanThread(env, self._extra_folders())
        self._thread.done.connect(self._on_scanned)
        self._thread.start()

    def _on_scanned(self, nodes):
        self._nodes = nodes
        self._rebuild()
        self._driver_check()

    def _rebuild(self):
        self._tree.clear()
        groups: Dict[str, QTreeWidgetItem] = {}
        order = {g: i for i, g in enumerate(CACHE_GROUP_ORDER)}
        by_group: Dict[str, List[dict]] = {}
        for n in self._nodes:
            by_group.setdefault(n["group"], []).append(n)
        total = 0
        for g in sorted(by_group, key=lambda x: order.get(x, 99)):
            items = sorted(by_group[g], key=lambda n: -n["size"])
            gsize = sum(n["size"] for n in items)
            gfiles = sum(n["files"] for n in items)
            total += gsize
            newest = max((n["mtime"] for n in items), default=0)
            top = QTreeWidgetItem([g, _fmt_bytes(gsize), str(gfiles), self._fmt_time(newest), ""])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            groups[g] = top
            self._tree.addTopLevelItem(top)
            for n in items:
                warn = self._stale and n["driver"]
                child = QTreeWidgetItem([("⚠ " if warn else "") + n["label"], _fmt_bytes(n["size"]),
                                         str(n["files"]), self._fmt_time(n["mtime"]), n["path"]])
                child.setData(0, Qt.UserRole, n)
                if warn:
                    for c in range(5):
                        child.setForeground(c, QColor(T["warn"]))
                    child.setToolTip(0, "Built for an older NVIDIA driver — stale, will be rebuilt.")
                top.addChild(child)
            top.setExpanded(len(items) <= 12)
        self._total.setText(f"{len(self._nodes)} location(s) · {_fmt_bytes(total)} total")

    @staticmethod
    def _fmt_time(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"

    # ── driver change detection ───────────────────────────────────────────
    def _driver_check(self):
        cur = SYSHUB.info.nv_driver
        if not cur:
            return
        qs = QSettings(APP_ID, APP_ID)
        seen = str(qs.value("cache/driver_seen", "") or "")
        if not seen:
            qs.setValue("cache/driver_seen", cur)
            seen = cur
        stale = seen != cur
        if stale != self._stale:
            self._stale = stale
            self._rebuild()
            self.stale_changed.emit(stale)
        if stale:
            self._banner.setText(
                f"⚠ NVIDIA driver changed: {seen} → {cur}. Driver-bound caches (NVIDIA GL/compute, "
                f"vkd3d-proton pipeline caches — marked ⚠) were built for the old driver and are "
                f"invalid: the first run of each game rebuilds them, so expect shader-compile stutter "
                f"once. DXVK state caches and Mesa/Fossilize data stay valid.")
            self._banner.show()
            self._banner_btns.show()
            if not self._driver_msg_sent:
                self._driver_msg_sent = True
                self.driver_changed.emit(
                    f"NVIDIA driver changed ({seen} → {cur}): shader caches are invalid — see Extra Tools → Cache")
        else:
            self._banner.hide()
            self._banner_btns.hide()

    def _acknowledge(self):
        cur = SYSHUB.info.nv_driver
        if cur:
            QSettings(APP_ID, APP_ID).setValue("cache/driver_seen", cur)
        self._driver_msg_sent = False
        self._driver_check()

    # ── actions ───────────────────────────────────────────────────────────
    def _selected_nodes(self) -> List[dict]:
        out = []
        for it in self._tree.selectedItems():
            n = it.data(0, Qt.UserRole)
            if n:
                out.append(n)
            else:                                   # group row → all its children
                for i in range(it.childCount()):
                    out.append(it.child(i).data(0, Qt.UserRole))
        seen, uniq = set(), []
        for n in out:
            if n["path"] not in seen:
                seen.add(n["path"])
                uniq.append(n)
        return uniq

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Cache folder to track", str(Path.home()))
        if d:
            lst = self._extra_folders()
            if d not in lst:
                lst.append(d)
                self._save_extra(lst)
            self.rescan()

    def _open_selected(self):
        nodes = self._selected_nodes()
        if not nodes:
            return
        p = Path(nodes[0]["path"])
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p if p.is_dir() else p.parent)))

    def _clear_selected(self):
        self._clear(self._selected_nodes())

    def _clear_stale(self):
        self._clear([n for n in self._nodes if n["driver"]])

    def _clear(self, nodes: List[dict]):
        if not nodes:
            return
        extra = self._extra_folders()
        ok = [n for n in nodes if _safe_cache_path(n["path"], extra)]
        refused = len(nodes) - len(ok)
        if not ok:
            QMessageBox.warning(self, "Clear caches", "Nothing selected is a recognised cache location.")
            return
        size = sum(n["size"] for n in ok)
        preview = "\n".join(f"  • {n['path']}  ({_fmt_bytes(n['size'])})" for n in ok[:12])
        if len(ok) > 12:
            preview += f"\n  … and {len(ok) - 12} more"
        if QMessageBox.question(
                self, "Clear caches",
                f"Delete the contents of {len(ok)} location(s), freeing about {_fmt_bytes(size)}?\n\n{preview}"
                + (f"\n\n{refused} location(s) skipped (not recognised as caches)." if refused else "")
                + "\n\nGames rebuild their caches on the next run (one-time stutter).") != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        errors = 0
        try:
            for n in ok:
                p = Path(n["path"])
                try:
                    if p.is_dir() and not p.is_symlink():
                        for child in p.iterdir():
                            if child.is_dir() and not child.is_symlink():
                                shutil.rmtree(child, ignore_errors=True)
                            else:
                                child.unlink(missing_ok=True)
                    else:
                        p.unlink(missing_ok=True)
                except OSError:
                    errors += 1
        finally:
            QApplication.restoreOverrideCursor()
        if errors:
            QMessageBox.warning(self, "Clear caches", f"{errors} location(s) could not be fully cleared.")
        self.rescan()


# ----------------------------------------------------------------------------
# Upstream drift check
# ----------------------------------------------------------------------------

_DRIFT_RAW = "https://raw.githubusercontent.com"
_DRIFT_UA = "DRSTool/upstream-drift (+https://github.com/arabcian/DRSTool)"
_DRIFT_MAX_BYTES = 4 * 1024 * 1024

_DXVK_FILES = ["README.md", "dxvk.conf", "src/util/util_env.cpp", "src/util/config/config.cpp",
               "src/util/log/log.cpp", "src/dxvk/dxvk_instance.cpp", "src/dxvk/dxvk_options.cpp",
               "src/dxvk/dxvk_device_filter.cpp", "src/dxvk/hud/dxvk_hud.cpp", "src/wsi/wsi_platform.cpp",
               "src/dxgi/dxgi_options.cpp", "src/d3d9/d3d9_options.cpp", "src/d3d11/d3d11_options.cpp",
               "src/d3d8/d3d8_options.cpp", "src/dxvk/dxvk_shader_cache.cpp", "src/dxvk/dxvk_latency_builtin.cpp"]
_VKD3D_FILES = ["README.md", "include/private/config_flag_decl.h", "libs/vkd3d/vkd3d_main.c",
                "libs/vkd3d/device.c", "libs/vkd3d/state.c", "libs/vkd3d/command.c", "libs/vkd3d/utils.c",
                "libs/vkd3d/cache.c", "libs/vkd3d/swapchain.c", "libs/vkd3d/breadcrumbs.c",
                "libs/vkd3d-common/debug.c", "libs/vkd3d-shader/vkd3d_shader_main.c"]
_NVAPI_FILES = ["README.md", "src/util/util_env.cpp", "src/nvapi.cpp", "src/nvapi_globals.cpp",
                "src/nvapi_d3d12.cpp", "src/nvapi_drs.cpp", "src/sysinfo/nvapi_adapter.cpp",
                "src/vkreflex/vkreflex_layer.cpp"]

DRIFT_SOURCES: List[dict] = [
    {"key": "dxvk", "title": "DXVK", "repo": "doitsujin/dxvk", "ref": "master", "files": _DXVK_FILES,
     "env": {"prefixes": ("DXVK_",), "exclude": ("DXVK_NVAPI_",), "cats": ("DXVK",)},
     "conf_keys": True},
    {"key": "vkd3d", "title": "vkd3d-proton", "repo": "HansKristian-Work/vkd3d-proton", "ref": "master",
     "files": _VKD3D_FILES,
     "env": {"prefixes": ("VKD3D_",), "exclude": (), "cats": ("VKD3D-Proton",)},
     "vkd3d_flags": "include/private/config_flag_decl.h"},
    {"key": "dxvk-nvapi", "title": "dxvk-nvapi", "repo": "jp7677/dxvk-nvapi", "ref": "master",
     "files": _NVAPI_FILES,
     "env": {"prefixes": ("DXVK_NVAPI_", "DISABLE_DXVK_NVAPI"), "exclude": (), "cats": ("DXVK-NVAPI",)}},
    {"key": "gamescope", "title": "gamescope", "repo": "ValveSoftware/gamescope", "ref": "master",
     "files": ["README.md", "src/main.cpp"],
     "env": {"prefixes": ("GAMESCOPE_", "ENABLE_GAMESCOPE"), "exclude": (), "cats": ("Gamescope",)},
     "gs_flags": "src/main.cpp"},
    {"key": "nvapi-hdr", "title": "NVAPI header", "repo": "NVIDIA/nvapi", "ref": "main",
     "files": ["NvApiDriverSettings.h"], "drs_header": "NvApiDriverSettings.h"},
]


def _drift_catalog() -> dict:
    env_by_cat: Dict[str, set] = {}
    for ev in ALL_ENV_VARS:
        env_by_cat.setdefault(ev.cat, set()).add(ev.name)
    vk_flags = set(VKD3D_CONFIG_DESCS)
    for ev in VKD3D_ENV_VARS:
        if ev.name == "VKD3D_CONFIG":
            vk_flags |= set(ev.options)
    return {
        # DRS_SETTINGS / GPU_ARCH are generated from the DRS and Arch tabs, not catalogued as env vars
        "env_all": {ev.name for ev in ALL_ENV_VARS} | {"DXVK_NVAPI_DRS_SETTINGS", "DXVK_NVAPI_GPU_ARCH"},
        "env_by_cat": env_by_cat,
        "conf_keys": set(DXVK_CONFIG_KEY_NAMES),
        "vkd3d_flags": vk_flags,
        "gs_flags": {g.flag.lstrip("-") for g in GAMESCOPE_FLAGS},
        "drs_ids": set(_SETTING_BY_ID),
    }


def _drift_fetch(url: str, timeout: float = 25.0) -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": _DRIFT_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(_DRIFT_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if len(data) > _DRIFT_MAX_BYTES:
        data = data[:_DRIFT_MAX_BYTES]
    return data.decode("utf-8", errors="replace")


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _drift_env_names(files: Dict[str, str], prefixes: tuple, exclude: tuple) -> Dict[str, str]:
    """name -> 'file:line' of first sighting. Code: quoted strings only. Docs/conf: any word."""
    pfx = "|".join(re.escape(p) for p in prefixes)
    quoted = re.compile(r'"((?:%s)[A-Z0-9_]*)"' % pfx)
    word = re.compile(r'(?<![A-Za-z0-9_])((?:%s)[A-Z0-9_]*)(?![A-Za-z0-9_])' % pfx)
    found: Dict[str, str] = {}
    for rel, txt in files.items():
        rx = word if rel.lower().endswith((".md", ".conf", ".txt")) else quoted
        for m in rx.finditer(txt):
            name = m.group(1)
            if name.endswith("_") or "_TEST_" in name or any(name.startswith(x) for x in exclude):
                continue
            if len(name) <= min(len(p) for p in prefixes) + 1:
                continue
            found.setdefault(name, f"{rel}:{_line_of(txt, m.start())}")
    return found


def _drift_analyse(src: dict, files: Dict[str, str], cat: dict) -> List[dict]:
    rows: List[dict] = []
    title = src["title"]

    def row(kind, name, status, where="", note=""):
        rows.append({"src": title, "kind": kind, "name": name, "status": status,
                     "where": where, "note": note})

    gone_note = "not found in the scanned files (removed upstream, fork-only, or defined in a file not scanned)"
    spec = src.get("env")
    if spec:
        found = _drift_env_names(files, spec["prefixes"], spec["exclude"])
        for name, where in sorted(found.items()):
            if name not in cat["env_all"]:
                row("env var", name, "new", where)
        mine = set()
        for c in spec["cats"]:
            mine |= cat["env_by_cat"].get(c, set())
        for name in sorted(mine):
            if name.startswith(spec["prefixes"]) and not name.startswith(spec["exclude"] or ("\0",)) \
                    and name not in found:
                row("env var", name, "gone", note=gone_note)
    if src.get("conf_keys"):
        keys: Dict[str, str] = {}
        conf_rx = re.compile(r'^\s*#?\s*([A-Za-z0-9_]+\.[A-Za-z0-9_.]+)\s*=', re.M)
        opt_rx = re.compile(r'"((?:dxvk|dxgi|d3d8|d3d9|d3d10|d3d11)\.[A-Za-z0-9_.]+)"')
        for rel, txt in files.items():
            rx = conf_rx if rel.endswith(".conf") else opt_rx
            for m in rx.finditer(txt):
                keys.setdefault(m.group(1), f"{rel}:{_line_of(txt, m.start())}")
        # config.cpp holds per-game blocks → ignore it for 'gone' detection, only options files count
        for k, where in sorted(keys.items()):
            if k not in cat["conf_keys"] and not k.endswith(".conf") \
                    and k.startswith(("dxvk.", "dxgi.", "d3d9.", "d3d11.", "d3d8.")):
                row("dxvk.conf key", k, "new", where)
        for k in sorted(cat["conf_keys"] - set(keys)):
            row("dxvk.conf key", k, "gone", note=gone_note)
    rel = src.get("vkd3d_flags")
    if rel and rel in files:
        txt = files[rel]
        up: Dict[str, str] = {}
        for m in re.finditer(r'VKD3D_DECL_CONFIG\(\s*"([a-z0-9_]+)"', txt):
            up.setdefault(m.group(1), f"{rel}:{_line_of(txt, m.start())}")
        for m in re.finditer(r'VKD3D_DECL_CONFIG_PLAIN\(\s*([A-Z0-9_]+)\s*\)', txt):
            up.setdefault(m.group(1).lower(),
                          f"{rel}:{_line_of(txt, m.start())}  (declared without a config string — may be internal)")
        for k, where in sorted(up.items()):
            if k not in cat["vkd3d_flags"]:
                row("VKD3D_CONFIG flag", k, "new", where)
        for k in sorted(cat["vkd3d_flags"] - set(up)):
            row("VKD3D_CONFIG flag", k, "gone", note=gone_note)
    rel = src.get("gs_flags")
    if rel and rel in files:
        txt = files[rel]
        up = {}
        for m in re.finditer(r'\{\s*"([a-z0-9-]+)"\s*,\s*(?:no|required|optional)_argument', txt):
            up.setdefault(m.group(1), f"{rel}:{_line_of(txt, m.start())}")
        up.pop("help", None)
        up.pop("version", None)
        for k, where in sorted(up.items()):
            if k not in cat["gs_flags"]:
                row("CLI flag", f"--{k}", "new", where)
        for k in sorted(cat["gs_flags"] - set(up)):
            row("CLI flag", f"--{k}", "gone", note=gone_note)
    rel = src.get("drs_header")
    if rel and rel in files:
        txt = files[rel]
        up = {}
        # Current header: enum entries `NAME_ID = 0x1234ABCD,`; older ones: `#define NAME_ID 0x1234ABCD`.
        for m in re.finditer(r'^\s*(?:#define\s+)?([A-Z0-9_]+)_ID\s*(?:=\s*)?0x([0-9A-Fa-f]{1,8})\b', txt, re.M):
            up.setdefault(f"0X{int(m.group(2), 16):08X}", (m.group(1), f"{rel}:{_line_of(txt, m.start())}"))
        if not up:      # never report the whole catalog as "gone" because the parser found nothing
            row("DRS setting", "(header parse)", "gone",
                note="no setting IDs found in NvApiDriverSettings.h — format changed or file truncated; "
                     "DRS comparison skipped")
            return rows
        mine = {}
        for k, st in _SETTING_BY_ID.items():
            try:
                mine[f"0X{int(k, 16):08X}"] = st
            except ValueError:
                pass
        for sid, (nm, where) in sorted(up.items()):
            if sid not in mine:
                row("DRS setting", f"0x{sid[2:]}  {nm}", "new", where)
        for sid in sorted(set(mine) - set(up)):
            row("DRS setting", f"{mine[sid].id}  {mine[sid].name}".strip(), "gone",
                note="catalog ID is not in the public NvApiDriverSettings.h (undocumented / private setting?)")
    return rows


class _DriftWorker(QThread):
    progress = Signal(str)
    done = Signal(list, list)

    def __init__(self, sources: List[dict], cat: dict):
        super().__init__()
        self._sources, self._cat = sources, cat

    def run(self):
        rows: List[dict] = []
        notes: List[str] = []
        for src in self._sources:
            if self.isInterruptionRequested():
                break
            files: Dict[str, str] = {}
            net_err = ""
            total = len(src["files"])
            for i, rel in enumerate(src["files"], 1):
                if self.isInterruptionRequested():
                    break
                self.progress.emit(f'{src["title"]}: {rel}  ({i}/{total})')
                try:
                    txt = _drift_fetch(f'{_DRIFT_RAW}/{src["repo"]}/{src["ref"]}/{rel}')
                except Exception as e:                         # network / TLS / timeout
                    net_err = f"{type(e).__name__}: {e}"
                    break
                if txt is not None:
                    files[rel] = txt
            if net_err:
                notes.append(f'{src["title"]}: network error — {net_err}')
                continue
            if not files:
                notes.append(f'{src["title"]}: none of the {total} source files exist at {src["repo"]}@{src["ref"]}')
                continue
            rows += _drift_analyse(src, files, self._cat)
            notes.append(f'{src["title"]}: scanned {len(files)}/{total} files')
        self.done.emit(rows, notes)


class UpstreamDriftWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._rows: List[dict] = []
        self._worker: Optional[_DriftWorker] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_pack2_title(
            "Upstream drift check",
            "Downloads the current DXVK, vkd3d-proton, dxvk-nvapi, gamescope sources and NVIDIA's "
            "NvApiDriverSettings.h and compares them with DRSTool's catalog: env vars, dxvk.conf keys, "
            "VKD3D_CONFIG flags, gamescope CLI flags and DRS setting IDs. “New” = upstream has it, the "
            "catalog does not. “Gone” = the catalog has it, upstream no longer mentions it. Mark rows "
            "as reviewed to hide them next time."))
        srow = QHBoxLayout()
        self._src_checks: Dict[str, QCheckBox] = {}
        for s in DRIFT_SOURCES:
            c = QCheckBox(s["title"])
            c.setChecked(True)
            c.setStyleSheet(f"color:{T['text2']};")
            self._src_checks[s["key"]] = c
            srow.addWidget(c)
        srow.addStretch(1)
        self._btn_check = _tool_btn("Check now", bold=True)
        self._btn_check.clicked.connect(self._start)
        srow.addWidget(self._btn_check)
        lay.addLayout(srow)
        frow = QHBoxLayout()
        self._filter = QComboBox()
        self._filter.addItems(["New upstream", "Gone upstream", "All"])
        self._filter.currentIndexChanged.connect(self._refill)
        self._hide_ack = QCheckBox("Hide reviewed")
        self._hide_ack.setChecked(True)
        self._hide_ack.setStyleSheet(f"color:{T['text2']};")
        self._hide_ack.toggled.connect(self._refill)
        frow.addWidget(QLabel("Show"))
        frow.addWidget(self._filter)
        frow.addWidget(self._hide_ack)
        frow.addStretch(1)
        self._status = QLabel("Not checked yet.")
        self._status.setStyleSheet(f"color:{T['muted']};font-size:12px;")
        frow.addWidget(self._status)
        lay.addLayout(frow)
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["Source", "Kind", "Name", "Status", "Where / note"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.ExtendedSelection)
        hh = self._table.horizontalHeader()
        for c in (0, 1, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self._table.setStyleSheet(_pack2_table_ss())
        lay.addWidget(self._table, 1)
        brow = QHBoxLayout()
        b1 = _tool_btn("Mark selected reviewed")
        b1.clicked.connect(lambda: self._ack(selected_only=True))
        b2 = _tool_btn("Mark all shown reviewed")
        b2.clicked.connect(lambda: self._ack(selected_only=False))
        b3 = _tool_btn("Copy report")
        b3.clicked.connect(self._copy_report)
        b4 = _tool_btn("Forget reviewed")
        b4.clicked.connect(self._forget)
        for b in (b1, b2, b3, b4):
            brow.addWidget(b)
        brow.addStretch(1)
        lay.addLayout(brow)

    # reviewed set (QSettings) ------------------------------------------------
    @staticmethod
    def _acks() -> set:
        try:
            v = QSettings(APP_ID, APP_ID).value("drift/reviewed", [])
            if isinstance(v, str):
                v = [v] if v else []
            return set(str(x) for x in (v or []))
        except Exception:
            return set()

    @staticmethod
    def _row_key(r: dict) -> str:
        return f'{r["src"]}|{r["kind"]}|{r["status"]}|{r["name"]}'

    def _save_acks(self, s: set):
        try:
            QSettings(APP_ID, APP_ID).setValue("drift/reviewed", sorted(s))
        except Exception:
            pass

    def _start(self):
        if self._worker is not None and self._worker.isRunning():
            return
        srcs = [s for s in DRIFT_SOURCES if self._src_checks[s["key"]].isChecked()]
        if not srcs:
            return
        self._btn_check.setEnabled(False)
        self._status.setText("Checking…")
        self._worker = _DriftWorker(srcs, _drift_catalog())
        self._worker.progress.connect(self._status.setText)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, rows, notes):
        self._rows = rows
        self._btn_check.setEnabled(True)
        n_new = sum(1 for r in rows if r["status"] == "new")
        n_gone = sum(1 for r in rows if r["status"] == "gone")
        self._status.setText(f"{n_new} new · {n_gone} gone")
        self._status.setToolTip("\n".join(notes))
        if any("error" in n or "none of" in n for n in notes):
            QMessageBox.warning(self, "Upstream drift", "\n".join(notes))
        self._refill()

    def _visible_rows(self) -> List[dict]:
        mode = self._filter.currentIndex()
        acks = self._acks() if self._hide_ack.isChecked() else set()
        out = []
        for r in self._rows:
            if mode == 0 and r["status"] != "new":
                continue
            if mode == 1 and r["status"] != "gone":
                continue
            if self._row_key(r) in acks:
                continue
            out.append(r)
        return out

    def _refill(self, *_):
        rows = self._visible_rows()
        t = self._table
        t.setRowCount(0)
        for i, r in enumerate(rows):
            t.insertRow(i)
            col = T["accent"] if r["status"] == "new" else T["warn"]
            vals = [r["src"], r["kind"], r["name"], "NEW" if r["status"] == "new" else "gone",
                    r["where"] or r["note"]]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 3:
                    it.setForeground(QColor(col))
                if c == 4 and not r["where"]:
                    it.setForeground(QColor(T["muted"]))
                if c == 0:
                    it.setData(Qt.UserRole, r)
                t.setItem(i, c, it)

    def _ack(self, selected_only: bool):
        if selected_only:
            rows = sorted({i.row() for i in self._table.selectedItems()})
            targets = [self._table.item(r, 0).data(Qt.UserRole) for r in rows]
        else:
            targets = self._visible_rows()
        if not targets:
            return
        acks = self._acks()
        acks.update(self._row_key(r) for r in targets)
        self._save_acks(acks)
        self._refill()

    def _forget(self):
        self._save_acks(set())
        self._refill()

    def _copy_report(self):
        rows = self._visible_rows()
        lines = [f"# DRSTool upstream drift — {datetime.now():%Y-%m-%d}", ""]
        cur = None
        for r in sorted(rows, key=lambda r: (r["src"], r["kind"], r["status"], r["name"])):
            if (r["src"], r["kind"]) != cur:
                cur = (r["src"], r["kind"])
                lines += ["", f'## {r["src"]} — {r["kind"]}']
            lines.append(f'- {"NEW" if r["status"] == "new" else "gone"}: `{r["name"]}`'
                         + (f' ({r["where"]})' if r["where"] else ""))
        QApplication.clipboard().setText("\n".join(lines))
        self._status.setText(f"Copied report ({len(rows)} rows)")


# ============================================================================
# Feature pack 2: Steam per-game Proton, umu, shortcuts, dxvk.conf, game hints,
# frametime analyser, live process check, log viewer, external-change watcher
# ============================================================================

import hashlib as _hl
import difflib as _dl
from PySide6.QtCore import QFileSystemWatcher
from PySide6.QtGui import QSyntaxHighlighter, QTextCharFormat, QBrush


def _table_qss() -> str:
    return (f"QTableWidget{{background:{T['surface']};border:1px solid {T['border']};color:{T['text2']};"
            f"gridline-color:{T['border']};}}QHeaderView::section{{background:{T['raised']};"
            f"color:{T['muted']};border:none;padding:4px;}}")


def _title_label(text: str) -> QLabel:
    t = QLabel(text)
    t.setStyleSheet(f"color:{T['text']}; font-size:15px; font-weight:700;")
    return t


def _desc_label(text: str) -> QLabel:
    d = QLabel(text)
    d.setWordWrap(True)
    d.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
    return d


_LINEEDIT_QSS = (f"QLineEdit{{background:{T['surface']};border:1px solid {T['border']};"
                 f"color:{T['text']};padding:3px 8px;border-radius:4px;min-height:26px;"
                 f"font-family:{T['mono']};}}")
_COMBO_QSS = (f"QComboBox{{background:{T['surface']};border:1px solid {T['border']};"
              f"color:{T['text']};padding:3px 8px;border-radius:4px;min-height:26px;}}"
              f"QComboBox QAbstractItemView{{background:{T['surface']};color:{T['text']};}}")


def _main_win(w: QWidget):
    return w.window()


def _slugify(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s.strip()).strip("-.")
    return s[:60] or "game"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or (Path.home() / default))


def _file_sha(path: Path) -> str:
    try:
        return _hl.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# ----------------------------------------------------------------------------
# VDF: generic "set a value inside <path>/<appid>" (Steam config.vdf)
# ----------------------------------------------------------------------------

def _vdf_indent_at(text: str, close_tok) -> str:
    pos = close_tok[2]
    ls = text.rfind("\n", 0, pos) + 1
    ind = text[ls:pos]
    return ind if ind.strip() == "" else ""


def _vdf_insert_before_close(text: str, close_tok, body: str) -> str:
    pos = close_tok[2]
    ls = text.rfind("\n", 0, pos) + 1
    if text[ls:pos].strip() == "":
        return text[:ls] + body + text[ls:]
    return text[:pos] + "\n" + body + text[pos:]


def _vdf_descend(text: str, path: tuple):
    """-> (deepest existing block or None, #path parts that exist)."""
    node, blk, depth = _vdf_parse(text), None, 0
    for part in path:
        b = _vdf_find(node, part, "block")
        if b is None:
            break
        blk, node, depth = b, b[1], depth + 1
    return blk, depth


def vdf_get_app_value(text: str, path: tuple, appid: str, key: str) -> str:
    blk, depth = _vdf_descend(text, path)
    if blk is None or depth < len(path):
        return ""
    app = _vdf_find(blk[1], str(appid), "block")
    if app is None:
        return ""
    v = _vdf_find(app[1], key, "val")
    return v[1] if v else ""


def vdf_set_app_value(text: str, path: tuple, appid: str, key: str, value: str) -> str:
    appid, esc = str(appid), _vdf_esc(value)
    blk, depth = _vdf_descend(text, path)
    if blk is None:
        raise ValueError(f'"{path[0]}" section not found in the VDF file')
    if depth < len(path):                      # container missing -> create the chain
        cur = _vdf_indent_at(text, blk[2]) + "\t"
        lines = []
        for n in path[depth:]:
            lines.append(f'{cur}"{n}"\n{cur}{{\n')
            cur += "\t"
        lines.append(f'{cur}"{appid}"\n{cur}{{\n{cur}\t"{key}"\t\t"{esc}"\n{cur}}}\n')
        for _ in path[depth:]:
            cur = cur[:-1]
            lines.append(f"{cur}}}\n")
        return _vdf_insert_before_close(text, blk[2], "".join(lines))
    app = _vdf_find(blk[1], appid, "block")
    if app is None:
        ind = _vdf_indent_at(text, blk[2]) + "\t"
        return _vdf_insert_before_close(
            text, blk[2], f'{ind}"{appid}"\n{ind}{{\n{ind}\t"{key}"\t\t"{esc}"\n{ind}}}\n')
    v = _vdf_find(app[1], key, "val")
    if v is not None:
        return text[:v[2]] + f'"{esc}"' + text[v[3]:]
    ind = _vdf_indent_at(text, app[2]) + "\t"
    return _vdf_insert_before_close(text, app[2], f'{ind}"{key}"\t\t"{esc}"\n')


def vdf_remove_app_block(text: str, path: tuple, appid: str) -> str:
    blk, depth = _vdf_descend(text, path)
    if blk is None or depth < len(path):
        return text
    app = _vdf_find(blk[1], str(appid), "block")
    if app is None:
        return text
    # app = (open_tok, items, close_tok); the key token sits right before open_tok
    open_tok, close_tok = app[0], app[2]
    items = blk[1]
    key_tok = next((k for k, kd, pl in items if kd == "block" and pl is app), None)
    if key_tok is None:
        return text
    start = text.rfind("\n", 0, key_tok[2]) + 1
    if text[start:key_tok[2]].strip():
        start = key_tok[2]
    end = close_tok[3]
    if text[end:end + 1] == "\n":
        end += 1
    return text[:start] + text[end:]


STEAM_COMPAT_PATH = ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping")


def _official_proton_name(dirname: str) -> str:
    low = dirname.lower()
    if "experimental" in low:
        return "proton_experimental"
    if "hotfix" in low:
        return "proton_hotfix"
    m = re.match(r"proton\s+(\d+)\.(\d+)", low)
    if m:
        major, minor = m.groups()
        return f"proton_{major}" if minor == "0" else f"proton_{major}{minor}"
    return ""


def steam_compat_tools(root: Path) -> List[tuple]:
    """[(internal_name, label)] of Proton builds Steam can use (custom + official)."""
    out, seen = [], set()
    cdirs = [root / "compatibilitytools.d", Path.home() / ".steam/root/compatibilitytools.d",
             Path("/usr/share/steam/compatibilitytools.d"),
             Path("/usr/local/share/steam/compatibilitytools.d")]
    for cd in cdirs:
        try:
            subs = sorted(p for p in cd.iterdir() if p.is_dir())
        except OSError:
            continue
        for d in subs:
            internal, disp = d.name, ""
            try:
                t = (d / "compatibilitytool.vdf").read_text(errors="replace")
                m = re.search(r'"compat_tools"\s*\{\s*"([^"]+)"', t)
                if m:
                    internal = m.group(1)
                m = re.search(r'"display_name"\s+"([^"]+)"', t)
                if m:
                    disp = m.group(1)
            except OSError:
                pass
            if internal not in seen:
                seen.add(internal)
                out.append((internal, f"{disp or d.name}  ({internal})" if (disp or d.name) != internal
                            else internal))
    for lib in SteamBackend._libraries(root):
        try:
            subs = sorted(p for p in (lib / "common").iterdir()
                          if p.is_dir() and p.name.lower().startswith("proton") and (p / "proton").exists())
        except OSError:
            continue
        for d in subs:
            nm = _official_proton_name(d.name)
            if nm and nm not in seen:
                seen.add(nm)
                out.append((nm, f"{d.name}  ({nm})"))
    return out


def steam_config_vdf(localconfig: Path) -> Path:
    return localconfig.parents[3] / "config" / "config.vdf"


def steam_install_dirs() -> List[tuple]:
    """[(label, installdir Path)] of installed Steam games."""
    res, seen = [], set()
    for root in SteamBackend.roots():
        for lib in SteamBackend._libraries(root):
            for acf in sorted(lib.glob("appmanifest_*.acf")):
                try:
                    t = acf.read_text(errors="replace")
                except OSError:
                    continue
                a = re.search(r'"appid"\s+"(\d+)"', t)
                n = re.search(r'"name"\s+"((?:[^"\\]|\\.)*)"', t)
                d = re.search(r'"installdir"\s+"((?:[^"\\]|\\.)*)"', t)
                if not (a and d) or a.group(1) in seen:
                    continue
                p = lib / "common" / _vdf_unesc(d.group(1))
                name = _vdf_unesc(n.group(1)) if n else a.group(1)
                if p.is_dir() and not re.match(r"(Proton\b|Steam Linux Runtime|Steamworks Common)", name):
                    seen.add(a.group(1))
                    res.append((f"{name}  [{a.group(1)}]", p, a.group(1)))
    res.sort(key=lambda x: x[0].lower())
    return res


# ----------------------------------------------------------------------------
# umu-launcher helpers
# ----------------------------------------------------------------------------

UMU_STORES = ["", "none", "steam", "gog", "egs", "amazon", "battlenet", "ea", "humble",
              "itchio", "ubisoft", "zoomplatform"]


def proton_tool_dirs() -> List[tuple]:
    """[(label, absolute dir)] of installed Proton builds, for umu's PROTONPATH."""
    out, seen = [], set()
    roots = [Path.home() / ".local/share/Steam", Path.home() / ".steam/root"]
    cds = [r / "compatibilitytools.d" for r in roots]
    cds += [Path.home() / ".local/share/umu/compatibilitytools",
            Path("/usr/share/steam/compatibilitytools.d")]
    for cd in cds:
        try:
            for d in sorted(cd.iterdir()):
                rp = d.resolve()
                if d.is_dir() and (d / "proton").exists() and rp not in seen:
                    seen.add(rp)
                    out.append((d.name, str(d)))
        except OSError:
            continue
    return out


# ----------------------------------------------------------------------------
# Shortcut: standalone launcher script + .desktop entry
# ----------------------------------------------------------------------------

class ShortcutWidget(QWidget):
    """Writes ~/.local/share/drstool/launchers/<slug>.sh and a .desktop entry that runs it —
    the current DRSTool configuration baked into a launcher that works without Lutris/Steam."""

    LGTUNE = "/usr/local/bin/lutris-game-tune-wrapper"

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("Standalone launcher shortcut"))
        lay.addWidget(_desc_label(
            "Bakes the current configuration (env, DRS settings, gamescope, MangoHud, and the "
            "umu-run wrapper if enabled on the Launch line tab) into a shell script plus a "
            ".desktop entry. The script is a snapshot: re-create it after changing the setup."))
        form = QGridLayout()
        form.setSpacing(6)
        form.setColumnStretch(1, 1)

        def row(r, label, widget, extra=None):
            l = QLabel(label)
            l.setStyleSheet(f"color:{T['text2']};")
            form.addWidget(l, r, 0)
            form.addWidget(widget, r, 1)
            if extra is not None:
                form.addWidget(extra, r, 2)

        self._name = QLineEdit()
        self._name.setPlaceholderText("Alan Wake 2")
        self._cmd = QLineEdit()
        self._cmd.setPlaceholderText("exe or script — with umu: path to the .exe; e.g. ~/Games/aw2/AlanWake2.exe")
        self._wd = QLineEdit()
        self._wd.setPlaceholderText("optional working directory (cd before launch)")
        self._icon = QLineEdit()
        self._icon.setPlaceholderText("optional icon path or theme icon name")
        for w in (self._name, self._cmd, self._wd, self._icon):
            w.setStyleSheet(_LINEEDIT_QSS)
            w.textChanged.connect(lambda _t: self.refresh())
        b_cmd = _tool_btn("Browse…")
        b_cmd.clicked.connect(lambda: self._browse(self._cmd, False))
        b_wd = _tool_btn("Browse…")
        b_wd.clicked.connect(lambda: self._browse(self._wd, True))
        b_ic = _tool_btn("Browse…")
        b_ic.clicked.connect(lambda: self._browse(self._icon, False))
        row(0, "Name:", self._name)
        row(1, "Command:", self._cmd, b_cmd)
        row(2, "Working dir:", self._wd, b_wd)
        row(3, "Icon:", self._icon, b_ic)
        lay.addLayout(form)

        opt = QHBoxLayout()
        self._lg = QCheckBox("run through lutris-game-tune (PRE → RUN → POST)")
        self._lg.setStyleSheet(f"color:{T['text2']};")
        self._lg.setEnabled(Path(self.LGTUNE).exists())
        self._lg.setToolTip(f"{self.LGTUNE}" + ("" if Path(self.LGTUNE).exists() else "  — not installed"))
        self._lg.toggled.connect(lambda _c: self.refresh())
        opt.addWidget(self._lg)
        self._nice = QSpinBox()
        self._nice.setRange(-20, 19)
        self._nice.setValue(-10)
        self._nice.valueChanged.connect(lambda _v: self.refresh())
        opt.addWidget(QLabel("nice"))
        opt.addWidget(self._nice)
        opt.addStretch(1)
        lay.addLayout(opt)

        self._out = QPlainTextEdit()
        self._out.setReadOnly(True)
        self._out.setStyleSheet(_MONO_BOX_QSS)
        self._out.setMinimumHeight(150)
        lay.addWidget(self._out, 1)

        btns = QHBoxLayout()
        self._create = _tool_btn("Create launcher", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        self._create.clicked.connect(self._write)
        btns.addWidget(self._create)
        btns.addWidget(QLabel("Existing:"))
        self._existing = QComboBox()
        self._existing.setStyleSheet(_COMBO_QSS)
        self._existing.setMinimumWidth(200)
        btns.addWidget(self._existing)
        rm = _tool_btn("Remove")
        rm.clicked.connect(self._remove)
        btns.addWidget(rm)
        btns.addStretch(1)
        lay.addLayout(btns)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(self._status)
        self._list_existing()

    # -- paths ------------------------------------------------------------
    @staticmethod
    def _script_dir() -> Path:
        return _xdg("XDG_DATA_HOME", ".local/share") / "drstool" / "launchers"

    @staticmethod
    def _desktop_dir() -> Path:
        return _xdg("XDG_DATA_HOME", ".local/share") / "applications"

    def _browse(self, edit: QLineEdit, folder: bool):
        if folder:
            p = QFileDialog.getExistingDirectory(self, "Working directory", edit.text() or str(Path.home()))
        else:
            p, _ = QFileDialog.getOpenFileName(self, "Select file", edit.text() or str(Path.home()))
        if p:
            edit.setText(p)

    def _list_existing(self):
        self._existing.clear()
        try:
            for p in sorted(self._desktop_dir().glob("drstool-*.desktop")):
                self._existing.addItem(p.stem[len("drstool-"):], p)
        except OSError:
            pass

    # -- content ----------------------------------------------------------
    def _line_cmd(self) -> str:
        c = self._cmd.text().strip()
        return c

    def build_script(self) -> str:
        w = self.window()
        ll = getattr(w, "_launch_line", None)
        cmd = self._line_cmd()
        payload = w.collect_launch_payload() if hasattr(w, "collect_launch_payload") else {}
        if ll is not None:
            line = ll.build_line(cmd or "<command>", force_env=self._lg.isChecked())
        else:
            line = build_launch_line(payload, cmd or "<command>", True)
        lines = ["#!/bin/sh", f"# generated by DRSTool for: {self._name.text().strip() or '(unnamed)'}"]
        wd = self._wd.text().strip()
        if wd:
            lines.append(f"cd {shlex.quote(os.path.expanduser(wd))} || exit 1")
        if self._lg.isChecked():
            lines += [f"W={shlex.quote(self.LGTUNE)}", '"$W" PRE', "trap '\"$W\" POST' EXIT",
                      f'"$W" RUN {self._nice.value()} {line}']
        else:
            lines.append(f"exec {line}")
        return "\n".join(lines) + "\n"

    def refresh(self):
        self._out.setPlainText(self.build_script())

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        self.refresh()

    def _write(self):
        name = self._name.text().strip()
        if not name or not self._cmd.text().strip():
            self._status.setText("⚠ Name and command are required.")
            return
        slug = _slugify(name)
        sp = self._script_dir() / f"{slug}.sh"
        dp = self._desktop_dir() / f"drstool-{slug}.desktop"
        if (sp.exists() or dp.exists()) and QMessageBox.question(
                self, "Overwrite launcher", f"{dp.name} already exists. Overwrite it?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        icon = self._icon.text().strip()
        qn = name.replace("\n", " ")
        exec_path = str(sp).replace("\\", "\\\\").replace('"', '\\"')
        desktop = ("[Desktop Entry]\nType=Application\n"
                   f"Name={qn}\nExec=\"{exec_path}\"\n"
                   + (f"Icon={icon}\n" if icon else "") +
                   "Terminal=false\nCategories=Game;\nX-DRSTool=true\n")
        try:
            sp.parent.mkdir(parents=True, exist_ok=True)
            dp.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(sp, self.build_script())
            os.chmod(sp, 0o755)
            _atomic_write(dp, desktop)
            os.chmod(dp, 0o755)
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")
            return
        if shutil.which("update-desktop-database"):
            _run(["update-desktop-database", str(self._desktop_dir())], 5)
        self._status.setText(f"✔ Created {dp}  →  {sp}")
        self._list_existing()

    def _remove(self):
        dp = self._existing.currentData()
        if not dp:
            return
        slug = dp.stem[len("drstool-"):]
        if QMessageBox.question(self, "Remove launcher", f"Delete {dp.name} and its script?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        for p in (dp, self._script_dir() / f"{slug}.sh"):
            try:
                p.unlink()
            except OSError:
                pass
        self._status.setText(f"Removed {dp.name}")
        self._list_existing()


# ----------------------------------------------------------------------------
# dxvk.conf writer (DXVK_CONFIG -> file)
# ----------------------------------------------------------------------------

_DXVK_MARK_B = "# >>> DRSTool (managed) >>>"
_DXVK_MARK_E = "# <<< DRSTool <<<"


def _strip_dxvk_block(text: str) -> str:
    return re.sub(re.escape(_DXVK_MARK_B) + r".*?" + re.escape(_DXVK_MARK_E) + r"\n?", "",
                  text, flags=re.S)


def merge_dxvk_conf(existing: str, pairs: List[tuple]) -> str:
    """Hand-written lines stay; keys DRSTool sets are moved into one managed block."""
    keys = {k for k, _ in pairs}
    out = []
    for ln in _strip_dxvk_block(existing).splitlines():
        m = re.match(r"\s*([A-Za-z0-9_.]+)\s*=", ln)
        if m and m.group(1) in keys:
            continue
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    block = [_DXVK_MARK_B] + [f"{k} = {v}" for k, v in pairs] + [_DXVK_MARK_E]
    return ("\n".join(out) + "\n\n" if out else "") + "\n".join(block) + "\n"


class DxvkConfWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("dxvk.conf writer"))
        lay.addWidget(_desc_label(
            "Turns the DXVK_CONFIG key picker (Environment tab) into a real dxvk.conf. DXVK reads "
            "dxvk.conf from the game's working directory, or from the file named by DXVK_CONFIG_FILE. "
            "Hand-written lines in an existing file are kept; DRSTool's keys live in one marked block. "
            "vkd3d-proton has no config file — VKD3D_CONFIG stays an environment variable."))
        row = QHBoxLayout()
        row.addWidget(QLabel("Mode:"))
        self._mode = QComboBox()
        self._mode.setStyleSheet(_COMBO_QSS)
        self._mode.addItem("dxvk.conf in the game folder", 0)
        self._mode.addItem("File in ~/.config/drstool/dxvk/ + set DXVK_CONFIG_FILE", 1)
        self._mode.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(self._mode, 1)
        lay.addLayout(row)
        row = QHBoxLayout()
        self._steam = QComboBox()
        self._steam.setStyleSheet(_COMBO_QSS)
        self._steam.addItem("Steam game…", None)
        for label, p, _aid in steam_install_dirs():
            self._steam.addItem(label, p)
        self._steam.currentIndexChanged.connect(self._pick_steam)
        row.addWidget(self._steam, 1)
        self._dir = QLineEdit()
        self._dir.setPlaceholderText("game folder (where the .exe runs) / name for mode 2")
        self._dir.setStyleSheet(_LINEEDIT_QSS)
        self._dir.textChanged.connect(lambda _t: self.refresh())
        row.addWidget(self._dir, 2)
        b = _tool_btn("Browse…")
        b.clicked.connect(self._browse)
        row.addWidget(b)
        lay.addLayout(row)
        self._clear_env = QCheckBox("clear DXVK_CONFIG from the environment after writing "
                                    "(the file replaces it)")
        self._clear_env.setStyleSheet(f"color:{T['text2']};")
        lay.addWidget(self._clear_env)
        self._out = QPlainTextEdit()
        self._out.setReadOnly(True)
        self._out.setStyleSheet(_MONO_BOX_QSS)
        lay.addWidget(self._out, 1)
        btns = QHBoxLayout()
        self._wr = _tool_btn("Write dxvk.conf", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        self._wr.clicked.connect(self._write)
        btns.addWidget(self._wr)
        rm = _tool_btn("Remove DRSTool block")
        rm.clicked.connect(self._remove)
        btns.addWidget(rm)
        btns.addStretch(1)
        lay.addLayout(btns)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(self._status)

    def _pick_steam(self, _i):
        p = self._steam.currentData()
        if p is not None:
            self._dir.setText(str(p) if self._mode.currentData() == 0
                              else _slugify(re.sub(r"\s*\[\d+\]$", "", self._steam.currentText())))

    def _browse(self):
        p = QFileDialog.getExistingDirectory(self, "Game folder", self._dir.text() or str(Path.home()))
        if p:
            self._dir.setText(p)

    def _pairs(self) -> List[tuple]:
        w = self.window()
        env = w.collect_launch_payload().get("env", {}) if hasattr(w, "collect_launch_payload") else {}
        return [(k, v) for k, v in EnvVarEditorWidget._parse_dxvk_config(env.get("DXVK_CONFIG", ""))
                if v is not None]

    def _target(self) -> Optional[Path]:
        t = self._dir.text().strip()
        if not t:
            return None
        if self._mode.currentData() == 1:
            return _xdg("XDG_CONFIG_HOME", ".config") / "drstool" / "dxvk" / f"{_slugify(t)}.conf"
        p = Path(os.path.expanduser(t))
        return p if p.name == "dxvk.conf" else p / "dxvk.conf"

    def _new_text(self, path: Path) -> str:
        try:
            old = path.read_text(errors="replace")
        except OSError:
            old = ""
        return merge_dxvk_conf(old, self._pairs()), old

    def refresh(self):
        pairs = self._pairs()
        path = self._target()
        if not pairs:
            self._out.setPlainText("DXVK_CONFIG is empty — set some keys in Environment → DXVK_CONFIG first.")
            return
        if path is None:
            self._out.setPlainText("Pick a game folder (or a name in mode 2).\n\nGenerated block:\n" +
                                   "\n".join(f"{k} = {v}" for k, v in pairs))
            return
        new, old = self._new_text(path)
        diff = "\n".join(_dl.unified_diff(old.splitlines(), new.splitlines(),
                                          f"{path} (current)", f"{path} (new)", lineterm=""))
        self._out.setPlainText(f"{path}\n\n" + (diff or "(no changes)"))

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        self.refresh()

    def _write(self):
        path = self._target()
        if path is None or not self._pairs():
            self._status.setText("⚠ Nothing to write (no DXVK_CONFIG keys or no destination).")
            return
        new, old = self._new_text(path)
        if new == old:
            self._status.setText("Already up to date.")
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                _backup_file(path)
            _atomic_write(path, new)
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")
            return
        msg = f"✔ Wrote {path}"
        w = self.window()
        if self._mode.currentData() == 1 and hasattr(w, "set_env_var"):
            w.set_env_var("DXVK_CONFIG_FILE", str(path))
            msg += "  (DXVK_CONFIG_FILE set)"
        if self._clear_env.isChecked() and hasattr(w, "set_env_var"):
            w.set_env_var("DXVK_CONFIG", "")
            msg += "  (DXVK_CONFIG cleared)"
        self._status.setText(msg)
        self.refresh()

    def _remove(self):
        path = self._target()
        if path is None or not path.exists():
            self._status.setText("No file at the destination.")
            return
        old = path.read_text(errors="replace")
        new = _strip_dxvk_block(old)
        if new == old:
            self._status.setText("No DRSTool block in that file.")
            return
        try:
            _backup_file(path)
            _atomic_write(path, new.rstrip("\n") + "\n" if new.strip() else "")
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")
            return
        self._status.setText(f"✔ Removed the DRSTool block from {path}")
        self.refresh()


# ----------------------------------------------------------------------------
# Game hints: ProtonDB summary + PCGamingWiki API facts (read-only, cached)
# ----------------------------------------------------------------------------

def _http_get(url: str, timeout: float = 12.0):
    req = urllib.request.Request(url, headers={
        "User-Agent": "DRSTool/1.0 (+https://github.com/arabcian/DRSTool; game hint lookup) python-urllib",
        "Accept": "application/json, text/plain;q=0.9, */*;q=0.5", "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(2_000_000), r.geturl()


def _hints_cache_path(appid: str) -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "drstool" / "hints" / f"{appid}.json"


def _pcgw_param(raw: str, name: str) -> str:
    m = re.search(r"\|\s*" + re.escape(name) + r"\s*=\s*([^\n|}]*)", raw, re.I)
    return m.group(1).strip() if m else ""


def fetch_game_hints(appid: str, use_cache: bool = True) -> dict:
    cp = _hints_cache_path(appid)
    if use_cache:
        try:
            d = json.loads(cp.read_text())
            if time.time() - d.get("ts", 0) < 7 * 86400:
                d["cached"] = True
                return d
        except (OSError, ValueError):
            pass
    res = {"appid": appid, "ts": time.time(), "protondb": None, "pcgw": None, "errors": []}
    try:
        body, _u = _http_get(f"https://www.protondb.com/api/v1/reports/summaries/{appid}.json")
        j = json.loads(body.decode("utf-8", "replace"))
        res["protondb"] = {k: j.get(k) for k in ("tier", "bestReportedTier", "trendingTier",
                                                "score", "confidence", "total")}
    except Exception as ex:
        res["errors"].append(f"ProtonDB: {ex}")
    try:
        api = "https://www.pcgamingwiki.com/w/api.php"
        title = ""
        try:        # 1) Cargo: Steam appid -> page name (bot-friendly API endpoint)
            q = urllib.parse.urlencode({"action": "cargoquery", "format": "json", "tables": "Infobox_game",
                                        "fields": "_pageName=Page", "limit": "1",
                                        "where": f'Steam_AppID HOLDS "{appid}"'})
            j = json.loads(_http_get(f"{api}?{q}")[0].decode("utf-8", "replace"))
            title = j["cargoquery"][0]["title"]["Page"]
        except Exception as ex1:
            res["errors"].append(f"PCGamingWiki (cargo lookup): {ex1}")
        if not title:   # 2) documented redirect helper
            _b, final = _http_get(f"https://www.pcgamingwiki.com/api/appid.php?appid={appid}")
            title = urllib.parse.unquote(final.rstrip("/").rsplit("/", 1)[-1])
        final = "https://www.pcgamingwiki.com/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
        # 3) wikitext through the MediaWiki API (not index.php?action=raw)
        q = urllib.parse.urlencode({"action": "query", "prop": "revisions", "rvprop": "content",
                                    "rvslots": "main", "format": "json", "formatversion": "2", "titles": title})
        j = json.loads(_http_get(f"{api}?{q}")[0].decode("utf-8", "replace"))
        txt = j["query"]["pages"][0]["revisions"][0]["slots"]["main"]["content"]
        res["errors"] = [e for e in res["errors"] if not e.startswith("PCGamingWiki (cargo")]
        nums = lambda s: re.findall(r"\d+(?:\.\d+)?", s)
        res["pcgw"] = {"title": title.replace("_", " "), "url": final,
                       "engines": [e.strip() for e in re.findall(r"Infobox game/row/engine\|([^}|]+)", txt)],
                       "d3d": nums(_pcgw_param(txt, "direct3d versions")),
                       "vulkan": nums(_pcgw_param(txt, "vulkan versions")),
                       "opengl": nums(_pcgw_param(txt, "opengl versions"))}
    except Exception as ex:
        res["errors"].append(f"PCGamingWiki: {ex}")
    if res["protondb"] or res["pcgw"]:
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(res))
        except OSError:
            pass
    return res


def hint_suggestions(info: dict, has_nvidia: bool) -> List[tuple]:
    """[(VAR, value, reason)] — heuristics only, and only variables that exist in the catalog."""
    names = {e.name for e in ALL_ENV_VARS}
    out = []
    pc = info.get("pcgw") or {}
    d3d = {x.split(".")[0] for x in pc.get("d3d", [])}
    if has_nvidia and d3d & {"11", "12"} and "PROTON_ENABLE_NVAPI" in names:
        out.append(("PROTON_ENABLE_NVAPI", "1",
                    "Direct3D 11/12 title on NVIDIA: dxvk-nvapi exposes DLSS / Reflex"))
    if pc and not d3d and pc.get("opengl") and "__GL_THREADED_OPTIMIZATIONS" in names:
        out.append(("__GL_THREADED_OPTIMIZATIONS", "1", "OpenGL-only title: NVIDIA threaded optimization"))
    return [o for o in out if o[0] in names]


class _HintWorker(QThread):
    done = Signal(dict)

    def __init__(self, appid: str, force: bool, parent=None):
        super().__init__(parent)
        self._appid, self._force = appid, force

    def run(self):
        self.done.emit(fetch_game_hints(self._appid, not self._force))


class GameHintsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: Optional[_HintWorker] = None
        self._info: dict = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("Game hints (ProtonDB · PCGamingWiki)"))
        lay.addWidget(_desc_label(
            "Looks up a Steam appid: ProtonDB rating and PCGamingWiki's graphics-API facts, cached for a week. "
            "Suggested variables are simple heuristics derived from those facts, never copied from user reports; "
            "apply them with one click and review them in the Linter."))
        row = QHBoxLayout()
        self._games = QComboBox()
        self._games.setStyleSheet(_COMBO_QSS)
        self._games.addItem("Installed Steam game…", None)
        for label, _p, aid in steam_install_dirs():
            self._games.addItem(label, aid)
        self._games.currentIndexChanged.connect(
            lambda _i: self._games.currentData() and self._appid.setText(self._games.currentData()))
        row.addWidget(self._games, 1)
        self._appid = QLineEdit()
        self._appid.setPlaceholderText("Steam appid")
        self._appid.setMaximumWidth(140)
        self._appid.setStyleSheet(_LINEEDIT_QSS)
        self._appid.returnPressed.connect(lambda: self._fetch(False))
        row.addWidget(self._appid)
        self._go = _tool_btn("Look up", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        self._go.clicked.connect(lambda: self._fetch(False))
        row.addWidget(self._go)
        rf = _tool_btn("Refresh")
        rf.clicked.connect(lambda: self._fetch(True))
        row.addWidget(rf)
        lay.addLayout(row)
        self._summary = QPlainTextEdit()
        self._summary.setReadOnly(True)
        self._summary.setStyleSheet(_MONO_BOX_QSS)
        self._summary.setMaximumHeight(170)
        lay.addWidget(self._summary)
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Variable", "Value", "Why"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._table.setStyleSheet(_table_qss())
        lay.addWidget(self._table, 1)
        btns = QHBoxLayout()
        self._apply = _tool_btn("Apply selected", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        self._apply.clicked.connect(self._apply_selected)
        btns.addWidget(self._apply)
        for label, fn in (("Open ProtonDB", lambda: self._open("https://www.protondb.com/app/{id}")),
                          ("Open PCGamingWiki", self._open_pcgw)):
            b = _tool_btn(label)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(self._status)

    def _open(self, tmpl: str):
        aid = self._appid.text().strip()
        if aid.isdigit():
            QDesktopServices.openUrl(QUrl(tmpl.format(id=aid)))

    def _open_pcgw(self):
        aid = self._appid.text().strip()
        if aid.isdigit():
            QDesktopServices.openUrl(QUrl(f"https://www.pcgamingwiki.com/api/appid.php?appid={aid}"))

    def _fetch(self, force: bool):
        aid = self._appid.text().strip()
        if not aid.isdigit():
            self._status.setText("⚠ Enter a numeric Steam appid.")
            return
        if self._worker and self._worker.isRunning():
            return
        self._status.setText("Looking up…")
        self._go.setEnabled(False)
        self._worker = _HintWorker(aid, force, self)
        self._worker.done.connect(self._show)
        self._worker.start()

    def _show(self, info: dict):
        self._go.setEnabled(True)
        self._info = info
        lines = [f"appid {info['appid']}" + ("  (cached)" if info.get("cached") else "")]
        pd = info.get("protondb")
        if pd:
            lines.append(f"ProtonDB: {pd.get('tier')}  (trending {pd.get('trendingTier')}, best {pd.get('bestReportedTier')}, "
                         f"{pd.get('total')} reports, confidence {pd.get('confidence')})")
            if str(pd.get("tier")) in ("borked", "bronze", "pending"):
                lines.append("  → few/poor working reports: try another Proton build (Steam tab) before tuning.")
        pc = info.get("pcgw")
        if pc:
            lines.append(f"PCGamingWiki: {pc['title']}")
            lines.append("  Engine: " + (", ".join(pc["engines"]) or "?"))
            lines.append("  Direct3D: " + (", ".join(pc["d3d"]) or "—") +
                         "   Vulkan: " + (", ".join(pc["vulkan"]) or "—") +
                         "   OpenGL: " + (", ".join(pc["opengl"]) or "—"))
        lines += [f"⚠ {e}" for e in info.get("errors", [])]
        self._summary.setPlainText("\n".join(lines))
        sug = hint_suggestions(info, bool(SYSHUB.info.has_nvidia))
        self._table.setRowCount(0)
        for r, (n, v, why) in enumerate(sug):
            self._table.insertRow(r)
            for c, t in enumerate((n, v, why)):
                self._table.setItem(r, c, QTableWidgetItem(t))
        self._table.selectAll()
        self._status.setText(f"{len(sug)} suggestion(s)." if sug else "No heuristic suggestions for this title.")

    def _apply_selected(self):
        w = self.window()
        rows = sorted({i.row() for i in self._table.selectedIndexes()})
        for r in rows:
            w.set_env_var(self._table.item(r, 0).text(), self._table.item(r, 1).text())
        self._status.setText(f"Applied {len(rows)} variable(s) to the Environment tab.")


# ----------------------------------------------------------------------------
# Frametime analyser (MangoHud CSV / FLM_CSV) with A/B comparison
# ----------------------------------------------------------------------------

import statistics as _stat


def load_frametimes(path: str) -> dict:
    lines = Path(path).read_text(errors="replace").splitlines()
    hdr, kind = None, ""
    for i, ln in enumerate(lines[:12]):
        low = ln.lower()
        if low.startswith("flip_ns,"):
            hdr, kind = i, "flm"
            break
        if "frametime" in low and "," in low:
            hdr, kind = i, "mangohud"
            break
    if hdr is None:
        raise ValueError("not a MangoHud log (no 'frametime' column) or an FLM_CSV file")
    cols = [c.strip().lower() for c in lines[hdr].split(",")]
    ft: List[float] = []
    extra = {}
    if kind == "mangohud":
        ix = cols.index("frametime")
        for ln in lines[hdr + 1:]:
            p = ln.split(",")
            try:
                v = float(p[ix])
            except (ValueError, IndexError):
                continue
            if 0 < v < 10000:
                ft.append(v)
    else:
        ix = cols.index("interval_ns")
        ih = cols.index("is_hitch") if "is_hitch" in cols else -1
        hit = 0
        for ln in lines[hdr + 1:]:
            p = ln.split(",")
            try:
                v = float(p[ix]) / 1e6
            except (ValueError, IndexError):
                continue
            if 0 < v < 10000:
                ft.append(v)
                if ih >= 0 and p[ih].strip() == "1":
                    hit += 1
        extra["FLM hitches"] = hit
    if len(ft) < 10:
        raise ValueError("fewer than 10 frames in the log")
    return {"name": Path(path).name, "ft": ft, "kind": kind, "extra": extra}


def frame_stats(ft: List[float]) -> List[tuple]:
    """[(label, value, higher_is_better|None, fmt)]"""
    n, total = len(ft), sum(ft)
    s = sorted(ft)
    med = s[n // 2]

    def worst(frac):
        k = max(1, int(n * frac))
        return sum(s[-k:]) / k

    def pct(p):
        return s[min(n - 1, int(p / 100 * n))]
    return [("Frames", n, None, "{:.0f}"),
            ("Duration (s)", total / 1000, None, "{:.1f}"),
            ("Average FPS", 1000 * n / total, True, "{:.1f}"),
            ("1% low FPS", 1000 / worst(0.01), True, "{:.1f}"),
            ("0.1% low FPS", 1000 / worst(0.001), True, "{:.1f}"),
            ("Median frametime (ms)", med, False, "{:.2f}"),
            ("p99 frametime (ms)", pct(99), False, "{:.2f}"),
            ("p99.9 frametime (ms)", pct(99.9), False, "{:.2f}"),
            ("Max frametime (ms)", s[-1], False, "{:.1f}"),
            ("Frametime σ (ms)", _stat.pstdev(ft), False, "{:.2f}"),
            ("Stutters (> 2× median)", sum(1 for x in ft if x > 2 * med), False, "{:.0f}"),
            ("Hitches (> 50 ms)", sum(1 for x in ft if x > 50), False, "{:.0f}")]


class FrametimePlot(QWidget):
    COLORS = ["#76b900", "#5aa0ff"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series: List[tuple] = []          # (label, ft)
        self.setMinimumHeight(170)

    def set_series(self, series: List[tuple]):
        self._series = series
        self.update()

    def paintEvent(self, _ev):              # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(T["surface"]))
        if not self._series:
            p.setPen(QColor(T["muted"]))
            p.drawText(self.rect(), Qt.AlignCenter, "Load a log to see the frametime graph")
            return
        L, B, R, TOP = 46, 20, 8, 8
        W, H = self.width() - L - R, self.height() - B - TOP
        if W < 20 or H < 20:
            return
        ymax = max(max(sorted(ft)[int(len(ft) * 0.999)] for _l, ft in self._series) * 1.4, 10.0)
        tmax = max(sum(ft) for _l, ft in self._series) / 1000
        p.setPen(QPen(QColor(T["border"])))
        f = QFont()
        f.setPointSize(8)
        p.setFont(f)
        for i in range(5):
            y = TOP + H - H * i / 4
            p.drawLine(L, int(y), L + W, int(y))
            p.setPen(QColor(T["muted"]))
            p.drawText(2, int(y) + 4, f"{ymax * i / 4:.0f} ms")
            p.setPen(QPen(QColor(T["border"])))
        for i in range(0, 5):
            p.setPen(QColor(T["muted"]))
            p.drawText(L + int(W * i / 4) - 10, self.height() - 5, f"{tmax * i / 4:.0f}s")
        for si, (label, ft) in enumerate(self._series):
            col = QColor(self.COLORS[si % 2])
            buckets: List[Optional[float]] = [None] * W
            t = 0.0
            for v in ft:
                t += v
                b = min(W - 1, int(t / 1000 / tmax * (W - 1)))
                if buckets[b] is None or v > buckets[b]:
                    buckets[b] = v
            p.setPen(QPen(col, 1))
            last = None
            for x, v in enumerate(buckets):
                if v is None:
                    continue
                y = TOP + H - min(v, ymax) / ymax * H
                if last is not None:
                    p.drawLine(L + last[0], int(last[1]), L + x, int(y))
                last = (x, y)
            p.setPen(col)
            p.drawText(L + 8 + si * 200, TOP + 12, "■ " + label[:28])


class FrametimeWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: List[Optional[dict]] = [None, None]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("Frametime analyser"))
        lay.addWidget(_desc_label(
            "Loads MangoHud logs (enable output_folder in the MangoHud tab, log with the toggle key) or "
            "vk_flip_meter FLM_CSV dumps. Load a second run to compare two configurations side by side."))
        self._edits: List[QLineEdit] = []
        for i, nm in enumerate(("A", "B")):
            row = QHBoxLayout()
            lab = QLabel(f"Run {nm}:")
            lab.setStyleSheet(f"color:{FrametimePlot.COLORS[i]}; font-weight:700;")
            row.addWidget(lab)
            e = QLineEdit()
            e.setReadOnly(True)
            e.setPlaceholderText("no file" + (" (optional)" if i else ""))
            e.setStyleSheet(_LINEEDIT_QSS)
            self._edits.append(e)
            row.addWidget(e, 1)
            b = _tool_btn("Open…")
            b.clicked.connect(lambda _c=False, k=i: self._open(k))
            row.addWidget(b)
            c = _tool_btn("Clear")
            c.clicked.connect(lambda _c=False, k=i: self._set(k, None, ""))
            row.addWidget(c)
            lay.addLayout(row)
        self._plot = FrametimePlot()
        lay.addWidget(self._plot)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Metric", "A", "B", "B vs A"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.setStyleSheet(_table_qss())
        lay.addWidget(self._table, 1)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)
        self._qs = QSettings(APP_ID, APP_ID)

    def _open(self, k: int):
        start = str(self._qs.value("frametime/last_dir", str(Path.home())))
        p, _ = QFileDialog.getOpenFileName(self, "Open frametime log", start,
                                           "Logs (*.csv *.log *.txt);;All files (*)")
        if not p:
            return
        self._qs.setValue("frametime/last_dir", str(Path(p).parent))
        try:
            d = load_frametimes(p)
        except Exception as ex:
            self._status.setText(f"⚠ {Path(p).name}: {ex}")
            return
        self._set(k, d, p)

    def _set(self, k: int, d: Optional[dict], path: str):
        self._data[k] = d
        self._edits[k].setText(path)
        self._render()

    def _render(self):
        ds = self._data
        self._plot.set_series([(d["name"], d["ft"]) for d in ds if d])
        stats = [frame_stats(d["ft"]) if d else None for d in ds]
        ref = next((s for s in stats if s), None)
        t = self._table
        t.setRowCount(0)
        if ref is None:
            return
        for r, (label, _v, hib, fmt) in enumerate(ref):
            t.insertRow(r)
            t.setItem(r, 0, QTableWidgetItem(label))
            vals = [s[r][1] if s else None for s in stats]
            for c in (0, 1):
                t.setItem(r, c + 1, QTableWidgetItem("" if vals[c] is None else fmt.format(vals[c])))
            if vals[0] is not None and vals[1] is not None and vals[0] != 0 and hib is not None:
                dpct = (vals[1] - vals[0]) / vals[0] * 100
                good = (dpct > 0) == hib
                it = QTableWidgetItem(f"{dpct:+.1f}%")
                it.setForeground(QColor(T["accent"] if abs(dpct) < 0.05 or good else T["danger"]))
                if abs(dpct) < 0.5:
                    it.setForeground(QColor(T["muted"]))
                t.setItem(r, 3, it)
        extra = [f"{d['name']}: " + ", ".join(f"{k}={v}" for k, v in d["extra"].items())
                 for d in ds if d and d["extra"]]
        self._status.setText("  ".join(extra))


# ----------------------------------------------------------------------------
# Live process check: did the configured environment / DLLs reach the game?
# ----------------------------------------------------------------------------

_WINE_HINTS = (".exe", "wine", "proton", "gamescope", "umu-run", "reaper")
_WINE_NOISE = ("services.exe", "winedevice.exe", "plugplay.exe", "explorer.exe", "rpcss.exe",
               "svchost.exe", "conhost.exe", "start.exe", "tabtip.exe", "wineserver", "crashhandler",
               "steamwebhelper", "steam.exe", "pressure-vessel", "reaper", "lsteamclient", "winedbg")

_MOD_RULES = [
    ("DXVK (d3d8/9/10/11/dxgi)", r"/(d3d8|d3d9|d3d10core|d3d11|dxgi)\.dll$"),
    ("D3D12 (vkd3d-proton)", r"/(d3d12|d3d12core)\.dll$"),
    ("dxvk-nvapi", r"/nvapi(64)?\.dll$"),
    ("NGX / DLSS", r"/_?nvngx[^/]*\.dll$"),
    ("Streamline", r"/sl\.[a-z_.]+\.dll$"),
    ("nvcuda / PhysX", r"/(nvcuda|physxcore|physx3)[^/]*\.dll$"),
    ("winevulkan", r"/winevulkan\.(dll|so)$"),
    ("MangoHud", r"libMangoHud"),
    ("vk_flip_meter", r"cpu_flip_meter"),
    ("Vulkan layer / overlay", r"VkLayer|vkbasalt|libgamescope|gamescope_layer"),
    ("OptiScaler / ReShade", r"(?i)optiscaler|reshade"),
    ("NVIDIA driver", r"libGLX_nvidia|libnvidia-glcore|libnvidia-vulkan|libnvidia-rtcore|libnvidia-eglcore"),
    ("Mesa Vulkan", r"libvulkan_(radeon|intel|lvp)"),
]


def _origin(path: str) -> str:
    lp = path.lower()
    for key, name in (("dxvk-nvapi", "dxvk-nvapi"), ("vkd3d", "vkd3d-proton"), ("dxvk", "DXVK")):
        if key in lp:
            return name
    if "/lib/wine/" in lp or "/lib64/wine/" in lp or "/files/lib" in lp:
        return "Wine builtin"
    if "/steamapps/common/" in lp:
        return "game / Proton folder"
    return ""


def list_game_procs(only_wine: bool = True) -> List[tuple]:
    """[(pid, rss_mb, name, cmdline)] of this user's processes, biggest first."""
    uid, me, page = os.getuid(), os.getpid(), os.sysconf("SC_PAGE_SIZE")
    out = []
    try:
        names = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return out
    for n in names:
        pid = int(n)
        if pid == me:
            continue
        try:
            if os.stat(f"/proc/{n}").st_uid != uid:
                continue
            cmd = Path(f"/proc/{n}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            if not cmd:
                continue
            low = cmd.lower()
            if only_wine and (not any(h in low for h in _WINE_HINTS)
                              or any(x in low for x in _WINE_NOISE)):
                continue
            rss = int(Path(f"/proc/{n}/statm").read_text().split()[1]) * page / 1048576
            comm = Path(f"/proc/{n}/comm").read_text().strip()
        except (OSError, ValueError, IndexError):
            continue
        out.append((pid, rss, comm, cmd))
    out.sort(key=lambda r: -r[1])
    return out


def read_proc_environ(pid: int) -> Dict[str, str]:
    env = {}
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    for item in raw.split(b"\0"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def read_proc_modules(pid: int) -> List[str]:
    paths, seen = [], set()
    with open(f"/proc/{pid}/maps", "rb") as f:
        for ln in f:
            parts = ln.decode("utf-8", "replace").rstrip("\n").split(None, 5)
            if len(parts) == 6 and parts[5].startswith("/"):
                p = parts[5].replace(" (deleted)", "")
                if p not in seen:
                    seen.add(p)
                    paths.append(p)
    return paths


class LiveProcessWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("Live process check"))
        lay.addWidget(_desc_label(
            "Select the running game and compare what DRSTool is configured to set with the environment "
            "the process really received, plus which DXVK / vkd3d-proton / dxvk-nvapi / NGX DLLs and Vulkan "
            "layers are actually mapped into it. Answers “did the launcher swallow my variables?”."))
        row = QHBoxLayout()
        self._only = QCheckBox("only Wine / Proton processes")
        self._only.setChecked(True)
        self._only.setStyleSheet(f"color:{T['text2']};")
        self._only.toggled.connect(lambda _c: self.rescan())
        row.addWidget(self._only)
        row.addStretch(1)
        rb = _tool_btn("Refresh")
        rb.clicked.connect(self.rescan)
        row.addWidget(rb)
        lay.addLayout(row)
        self._plist = QListWidget()
        self._plist.setStyleSheet(_LAUNCHER_QSS)
        self._plist.setMaximumHeight(130)
        self._plist.currentItemChanged.connect(lambda *_: self.inspect())
        lay.addWidget(self._plist)
        self._sum = QLabel("")
        self._sum.setWordWrap(True)
        self._sum.setStyleSheet(f"color:{T['text2']}; font-size:12px;")
        lay.addWidget(self._sum)
        self._env = QTableWidget(0, 4)
        self._env.setHorizontalHeaderLabels(["Variable", "DRSTool", "Process", ""])
        self._mod = QTableWidget(0, 3)
        self._mod.setHorizontalHeaderLabels(["Component", "File", "Origin"])
        for t, stretch in ((self._env, 1), (self._mod, 1)):
            t.verticalHeader().setVisible(False)
            t.setEditTriggers(QTableWidget.NoEditTriggers)
            t.setSelectionMode(QTableWidget.NoSelection)
            t.setStyleSheet(_table_qss())
            t.horizontalHeader().setStretchLastSection(True)
            lay.addWidget(t, stretch)
        self._first = True

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        if self._first:
            self._first = False
            self.rescan()

    def rescan(self):
        self._plist.blockSignals(True)
        self._plist.clear()
        for pid, rss, comm, cmd in list_game_procs(self._only.isChecked()):
            it = QListWidgetItem(f"{pid:>7}  {rss:7.0f} MB  {comm[:18]:<18}  {cmd[:110]}")
            it.setData(Qt.UserRole, pid)
            self._plist.addItem(it)
        self._plist.blockSignals(False)
        if self._plist.count():
            self._plist.setCurrentRow(0)
        else:
            self._env.setRowCount(0)
            self._mod.setRowCount(0)
            self._sum.setText("No matching process running.")

    def inspect(self):
        it = self._plist.currentItem()
        if it is None:
            return
        pid = it.data(Qt.UserRole)
        w = self.window()
        exp = w.collect_launch_payload().get("env", {}) if hasattr(w, "collect_launch_payload") else {}
        try:
            env = read_proc_environ(pid)
            mods = read_proc_modules(pid)
        except OSError as ex:
            self._sum.setText(f"⚠ cannot read process {pid}: {ex}")
            return
        t = self._env
        t.setRowCount(0)
        ok = bad = 0
        names = set(exp)
        known = {e.name for e in ALL_ENV_VARS}
        extra = sorted(k for k in env if k in known and k not in names)
        for k in sorted(exp) + extra:
            r = t.rowCount()
            t.insertRow(r)
            want, got = exp.get(k), env.get(k)
            if want is None:
                st, col = "process only", T["muted"]
            elif got is None:
                st, col = "✗ missing", T["danger"]
                bad += 1
            elif str(got) != str(want):
                st, col = "≠ differs", T["warn"]
                bad += 1
            else:
                st, col = "✔", T["accent"]
                ok += 1
            for c, txt in enumerate((k, "" if want is None else str(want), "" if got is None else str(got), st)):
                cell = QTableWidgetItem(txt)
                if c == 3:
                    cell.setForeground(QColor(col))
                t.setItem(r, c, cell)
        m = self._mod
        m.setRowCount(0)
        found = {}
        for path in mods:
            for label, rx in _MOD_RULES:
                if re.search(rx, path):
                    r = m.rowCount()
                    m.insertRow(r)
                    for c, txt in enumerate((label, os.path.basename(path), _origin(path) or path)):
                        cell = QTableWidgetItem(txt)
                        cell.setToolTip(path)
                        m.setItem(r, c, cell)
                    found.setdefault(label, []).append(path)
                    break
        notes = []
        if "DXVK_NVAPI_DRS_SETTINGS" in exp and "dxvk-nvapi" not in found:
            notes.append("⚠ DRS settings are configured but no nvapi DLL is mapped — they cannot take effect "
                         "(PROTON_ENABLE_NVAPI / DXVK_ENABLE_NVAPI missing, or the game has not loaded NVAPI yet).")
        if _on(exp, "MANGOHUD") and "MangoHud" not in found:
            notes.append("⚠ MANGOHUD is set but libMangoHud is not loaded.")
        self._sum.setText(f"pid {pid}: {ok} variable(s) match, {bad} do not. "
                          f"{len(found)} component group(s) loaded. " + " ".join(notes))


# ----------------------------------------------------------------------------
# Log viewer (Proton / DXVK / vkd3d-proton) with known-problem detection
# ----------------------------------------------------------------------------

_LOG_ERR = re.compile(r"\b(?:err|error|fatal)\b\s*:|VK_ERROR_|\bERR\b:", re.I)
_LOG_WARN = re.compile(r"\bwarn(?:ing)?\b\s*:", re.I)
_LOG_FIXME = re.compile(r"\bfixme\b\s*:", re.I)

# (code, regex, title, hint, fixes[(var, value)])
_LOG_RULES = [
    ("nvapi", r"(?i)nvapi\w*\.dll.*(not found|failed)|nvapi.*(initiali[sz]e|init).*(fail|error)",
     "NVAPI unavailable",
     "dxvk-nvapi did not initialise. Needs PROTON_ENABLE_NVAPI=1 (Proton) / DXVK_ENABLE_NVAPI=1 and an NVIDIA GPU "
     "that is not hidden (Linter: nvapi-hidden).", [("PROTON_ENABLE_NVAPI", "1")]),
    ("nvcuda", r"(?i)library nvcuda\.dll.*not found|nvcuda\.dll.*not found",
     "nvcuda.dll missing",
     "CUDA/PhysX path: enable NVAPI/NVIDIA libs in Proton (PROTON_ENABLE_NVAPI, PROTON_NVIDIA_LIBS).", []),
    ("device-lost", r"VK_ERROR_DEVICE_LOST|\bdevice lost\b",
     "GPU device lost (hang/reset)",
     "Often an unstable GPU overclock/undervolt or driver fault: test at stock clocks and a default V/F curve.", []),
    ("oom", r"VK_ERROR_OUT_OF_(DEVICE|HOST)_MEMORY|out of (video|device|gpu) memory",
     "Out of memory",
     "VRAM or RAM exhausted: lower textures/resolution; DXVK_CONFIG dxgi.maxDeviceMemory caps reported VRAM.", []),
    ("ntsync", r"(?i)ntsync.*(fail|cannot|unavailable|not (found|supported))|/dev/ntsync",
     "ntsync not usable",
     "Kernel module not loaded (modprobe ntsync / CONFIG_NTSYNC) — or unset PROTON_USE_NTSYNC (Linter: ntsync-missing).", []),
    ("esync", r"(?i)too many open files|esync:.*(limit|fail)",
     "esync/fsync file-descriptor limit",
     "Raise the nofile hard limit (≥ 524288) in limits.conf / the service, or set PROTON_NO_ESYNC=1.", []),
    ("vk-device", r"(?i)vkCreate(Device|Instance).*fail|VK_ERROR_INCOMPATIBLE_DRIVER|VK_ERROR_INITIALIZATION_FAILED|"
                  r"no suitable vulkan|failed to create (a )?vulkan (instance|device)",
     "Vulkan device/instance creation failed",
     "Driver/ICD problem: check the System tab (vulkaninfo), DXVK_FILTER_DEVICE_NAME and the PRIME offload variables.", []),
    ("vk-ext", r"VK_ERROR_(EXTENSION|FEATURE)_NOT_PRESENT|required vulkan (extension|feature)|(unsupported|not supported) vulkan",
     "Missing Vulkan extension/feature",
     "Driver too old for this DXVK/vkd3d-proton build: update the driver or use an older build (Prefix Doctor).", []),
    ("ngx", r"(?i)(ngx|nvngx).*(fail|error|not found)",
     "NGX / DLSS failed to load",
     "Check the DLSS Updater tab and PROTON_ENABLE_NVAPI=1; a mismatching nvngx_dlss.dll in the game folder also causes this.",
     [("PROTON_ENABLE_NVAPI", "1")]),
    ("import-dll", r"err:module:import_dll",
     "Missing DLL import",
     "A required runtime DLL is absent in the prefix (VC++ redistributables, d3dcompiler_47, xinput …).", []),
]


class LogHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self._fmts = []
        for rx, col in ((_LOG_ERR, T["danger"]), (_LOG_WARN, T["warn"]), (_LOG_FIXME, T["muted"])):
            f = QTextCharFormat()
            f.setForeground(QColor(col))
            self._fmts.append((rx, f))

    def highlightBlock(self, text):        # noqa: N802
        for rx, f in self._fmts:
            if rx.search(text):
                self.setFormat(0, len(text), f)
                return


class LogViewerWidget(QWidget):
    MAX_READ = 6 * 1024 * 1024
    MAX_SHOW = 4000

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path: Optional[Path] = None
        self._pos = 0
        self._lines: List[str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("Log viewer"))
        lay.addWidget(_desc_label(
            "Opens a Proton log (PROTON_LOG=1 → ~/steam-<appid>.log), DXVK / vkd3d-proton logs, or any text log. "
            "Filters by severity, follows a running game, and recognises common failure signatures "
            "(NVAPI, ntsync, device lost, OOM, esync limits …)."))
        row = QHBoxLayout()
        self._combo = QComboBox()
        self._combo.setStyleSheet(_COMBO_QSS)
        self._combo.currentIndexChanged.connect(self._on_pick)
        row.addWidget(self._combo, 1)
        for label, fn in (("Browse…", self._browse), ("Rescan", self._scan_logs),
                          ("Enable logging vars", self._enable_logging)):
            b = _tool_btn(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        row = QHBoxLayout()
        self._chk = {}
        for key, label, on in (("err", "errors", True), ("warn", "warnings", True),
                               ("fixme", "fixme", False), ("info", "other", False)):
            c = QCheckBox(label)
            c.setChecked(on)
            c.setStyleSheet(f"color:{T['text2']};")
            c.toggled.connect(lambda _c: self._render())
            self._chk[key] = c
            row.addWidget(c)
        self._find = QLineEdit()
        self._find.setPlaceholderText("search…")
        self._find.setClearButtonEnabled(True)
        self._find.setStyleSheet(_LINEEDIT_QSS)
        self._find.textChanged.connect(lambda _t: self._render())
        row.addWidget(self._find, 1)
        self._follow = QCheckBox("follow")
        self._follow.setStyleSheet(f"color:{T['text2']};")
        self._follow.toggled.connect(lambda on: self._timer.start() if on else self._timer.stop())
        row.addWidget(self._follow)
        lay.addLayout(row)
        self._rules = QTableWidget(0, 4)
        self._rules.setHorizontalHeaderLabels(["Detected", "×", "Hint", ""])
        self._rules.verticalHeader().setVisible(False)
        self._rules.setEditTriggers(QTableWidget.NoEditTriggers)
        self._rules.setSelectionMode(QTableWidget.NoSelection)
        self._rules.setWordWrap(True)
        self._rules.setMaximumHeight(150)
        hh = self._rules.horizontalHeader()
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self._rules.setStyleSheet(_table_qss())
        lay.addWidget(self._rules)
        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._view.setStyleSheet(_MONO_BOX_QSS)
        self._hl = LogHighlighter(self._view.document())
        lay.addWidget(self._view, 1)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tail)
        self._first = True

    # -- discovery --------------------------------------------------------
    @staticmethod
    def _log_dirs() -> List[Path]:
        dirs = [Path.home(), _xdg("XDG_CACHE_HOME", ".cache") / "drstool" / "logs",
                _xdg("XDG_DATA_HOME", ".local/share") / "lutris" / "logs"]
        return dirs

    def _scan_logs(self):
        w = self.window()
        env = w.collect_launch_payload().get("env", {}) if hasattr(w, "collect_launch_payload") else {}
        dirs = self._log_dirs()
        for k in ("PROTON_LOG_DIR", "DXVK_LOG_PATH"):
            if env.get(k):
                dirs.append(Path(os.path.expanduser(env[k])))
        found = {}
        for d in dirs:
            try:
                for p in list(d.glob("steam-*.log")) + list(d.glob("*.log")):
                    if p.is_file():
                        found[p] = p.stat().st_mtime
            except OSError:
                continue
        cur = self._combo.currentData()
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("Select a log…", None)
        for p, mt in sorted(found.items(), key=lambda kv: -kv[1])[:60]:
            self._combo.addItem(f"{p.name}   {datetime.fromtimestamp(mt):%m-%d %H:%M}   "
                                f"{p.stat().st_size / 1048576:.1f} MB   {p.parent}", p)
        i = self._combo.findData(cur) if cur else -1
        self._combo.setCurrentIndex(max(i, 0))
        self._combo.blockSignals(False)

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        if self._first:
            self._first = False
            self._scan_logs()

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open log", str(Path.home()), "Logs (*.log *.txt);;All files (*)")
        if p:
            self._combo.blockSignals(True)
            self._combo.addItem(Path(p).name + "   (browsed)", Path(p))
            self._combo.setCurrentIndex(self._combo.count() - 1)
            self._combo.blockSignals(False)
            self._load(Path(p))

    def _on_pick(self, _i):
        p = self._combo.currentData()
        if p:
            self._load(p)

    def _enable_logging(self):
        w = self.window()
        d = _xdg("XDG_CACHE_HOME", ".cache") / "drstool" / "logs"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")
            return
        for k, v in (("PROTON_LOG", "1"), ("PROTON_LOG_DIR", str(d)), ("DXVK_LOG_PATH", str(d))):
            w.set_env_var(k, v)
        self._status.setText(f"Logging variables set (PROTON_LOG, PROTON_LOG_DIR, DXVK_LOG_PATH → {d}). "
                             "Run the game, then Rescan.")

    # -- reading ----------------------------------------------------------
    def _load(self, path: Path):
        self._path = path
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                if size > self.MAX_READ:
                    f.seek(size - self.MAX_READ)
                    f.readline()
                data = f.read()
            self._pos = size
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")
            return
        self._lines = data.decode("utf-8", "replace").splitlines()
        self._render()

    def _tail(self):
        p = self._path
        if p is None:
            return
        try:
            size = p.stat().st_size
        except OSError:
            return
        if size < self._pos:
            self._load(p)
            return
        if size == self._pos:
            return
        with open(p, "rb") as f:
            f.seek(self._pos)
            data = f.read(self.MAX_READ)
        self._pos += len(data)
        self._lines += data.decode("utf-8", "replace").splitlines()
        if len(self._lines) > 200000:
            self._lines = self._lines[-150000:]
        self._render()

    @staticmethod
    def _level(ln: str) -> str:
        if _LOG_ERR.search(ln):
            return "err"
        if _LOG_WARN.search(ln):
            return "warn"
        if _LOG_FIXME.search(ln):
            return "fixme"
        return "info"

    def _render(self):
        want = {k for k, c in self._chk.items() if c.isChecked()}
        needle = self._find.text().lower()
        shown = [ln for ln in self._lines
                 if self._level(ln) in want and (not needle or needle in ln.lower())]
        trimmed = len(shown) > self.MAX_SHOW
        sb = self._view.verticalScrollBar()
        at_end = sb.value() >= sb.maximum() - 4
        self._view.setPlainText("\n".join(shown[-self.MAX_SHOW:]))
        if at_end or self._follow.isChecked():
            sb.setValue(sb.maximum())
        # signature scan over the whole buffer
        self._rules.setRowCount(0)
        text = "\n".join(self._lines)
        w = self.window()
        for code, rx, title, hint, fixes in _LOG_RULES:
            hits = re.findall(rx, text)
            if not hits:
                continue
            r = self._rules.rowCount()
            self._rules.insertRow(r)
            self._rules.setItem(r, 0, QTableWidgetItem(title))
            self._rules.setItem(r, 1, QTableWidgetItem(str(len(hits))))
            self._rules.setItem(r, 2, QTableWidgetItem(hint))
            if fixes and hasattr(w, "set_env_var"):
                b = _tool_btn("Apply: " + ", ".join(f"{k}={v}" for k, v in fixes))
                b.clicked.connect(lambda _c=False, fx=fixes: [w.set_env_var(k, v) for k, v in fx])
                self._rules.setCellWidget(r, 3, b)
        self._rules.resizeRowsToContents()
        n_err = sum(1 for ln in self._lines if _LOG_ERR.search(ln))
        self._status.setText(f"{self._path.name if self._path else ''}: {len(self._lines)} lines, {n_err} error line(s)"
                             + (f" — showing the last {self.MAX_SHOW}" if trimmed else ""))


# ----------------------------------------------------------------------------
# External-change watcher + banner (profiles.json, the selected Lutris YAML)
# ----------------------------------------------------------------------------

class ExternalChangeBanner(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"QFrame{{background:#2a2410;border-bottom:1px solid {T['warn']};}}"
                           f"QLabel{{color:{T['text']};}}")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(10, 4, 10, 4)
        self._lay.setSpacing(2)
        self._rows: Dict[str, QWidget] = {}
        self.hide()

    def post(self, key: str, text: str, actions: List[tuple]):
        self.drop(key)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        lab = QLabel("⚠ " + text)
        lab.setWordWrap(True)
        h.addWidget(lab, 1)
        for label, cb in actions:
            b = _tool_btn(label)
            b.clicked.connect(lambda _c=False, f=cb: (self.drop(key), f()))
            h.addWidget(b)
        ig = _tool_btn("Ignore")
        ig.clicked.connect(lambda _c=False: self.drop(key))
        h.addWidget(ig)
        self._rows[key] = row
        self._lay.addWidget(row)
        self.show()

    def drop(self, key: str):
        row = self._rows.pop(key, None)
        if row is not None:
            row.setParent(None)
            row.deleteLater()
        if not self._rows:
            self.hide()


class ExternalChangeWatcher(QObject):
    """Content-hash based, so our own writes (mark()) and plain `touch`es never raise a notice."""
    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._w = QFileSystemWatcher(self)
        self._items: Dict[str, dict] = {}
        self._pending: set = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._flush)
        self._w.fileChanged.connect(self._on_path)
        self._w.directoryChanged.connect(self._on_path)

    def _arm(self, path: Path):
        have = set(self._w.files()) | set(self._w.directories())
        for p in (path, path.parent):
            if p.exists() and str(p) not in have:
                self._w.addPath(str(p))

    def track(self, key: str, path):
        self.untrack(key)
        path = Path(path)
        self._items[key] = {"path": path, "known": _file_sha(path)}
        self._arm(path)

    def untrack(self, key: str):
        it = self._items.pop(key, None)
        if it is None:
            return
        still = [i["path"] for i in self._items.values()]
        if it["path"] not in still:
            self._w.removePath(str(it["path"]))
        if it["path"].parent not in [p.parent for p in still]:
            self._w.removePath(str(it["path"].parent))

    def mark(self, key: str):
        it = self._items.get(key)
        if it:
            it["known"] = _file_sha(it["path"])
            self._arm(it["path"])

    def _on_path(self, p: str):
        for key, it in self._items.items():
            if str(it["path"]) == p or str(it["path"].parent) == p:
                self._pending.add(key)
        self._timer.start()

    def _flush(self):
        keys, self._pending = self._pending, set()
        for key in keys:
            it = self._items.get(key)
            if not it:
                continue
            self._arm(it["path"])
            cur = _file_sha(it["path"])
            if cur != it["known"]:
                it["known"] = cur
                self.changed.emit(key)


# ============================================================================
# Main Window
# ============================================================================

class MainWindow(QMainWindow):
    # Tab indices (sidebar stack pages). Nav order on screen matches.
    TAB_DRS, TAB_ARCH, TAB_ENV, TAB_TOOLS, TAB_PROFILES = range(5)
    # Right-stack pages
    PAGE_EMPTY, PAGE_SETTING, PAGE_ARCH, PAGE_ENV, PAGE_GAMESCOPE, PAGE_TOOLS, PAGE_LUTRIS = range(7)

    EMPTY_HINTS = {
        0: "Pick a DRS setting on the left.\nGreen dots mark settings already in the launch string.",
        1: "Pick your GPU generation on the left.\nIt sets DXVK_NVAPI_GPU_ARCH.",
        2: "Pick a variable on the left.\nGreen dots mark variables that are set; their value is shown on the right.",
        4: "",
    }

    def __init__(self):
        super().__init__()
        self.settings_manager = SettingsManager()
        self.all_settings = ALL_SETTINGS
        self._dirty = False
        self._qsettings = QSettings(APP_ID, APP_ID)

        self.setWindowTitle(APP_TITLE)
        if ICON_PATH.is_file():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setMinimumSize(980, 640)
        self.setStyleSheet(f"""
            QMainWindow {{ background: {T['bg']}; }}
            QSplitter::handle:horizontal {{ background: {T['border']}; width: 4px; }}
            QSplitter::handle:horizontal:hover {{ background: {T['accent_dim']}; }}
        """)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self._output_bar = OutputBarWidget(self.settings_manager, self)
        main_layout.addWidget(self._output_bar)

        # ===== Top navigation =====
        # Used to be five buttons squeezed into the 320px sidebar, where the
        # labels were clipped ("S Settin", "PU Arch"). Now a full-width bar
        # with live badges (how many things each tab contributes).
        nav = QWidget()
        nav.setObjectName("navBar")
        nav.setAttribute(Qt.WA_StyledBackground, True)
        nav.setStyleSheet(f"#navBar {{ background:{T['bg']}; border-bottom:1px solid {T['border']}; }}")
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(6, 0, 12, 0)
        nav_layout.setSpacing(0)

        self._settings_tab = NavTab("DRS Settings")
        self._arch_tab = NavTab("GPU Arch")
        self._env_tab = NavTab("Environment")
        self._env_tab.setToolTip("DXVK, VKD3D-Proton, forks, DXVK-NVAPI, NVIDIA, Proton, "
                                 "Wine, vk_flip_meter and Gamescope")
        self._flm_tab = NavTab("Extra Tools")
        self._profiles_tab = NavTab("Profiles")
        self._nav_tabs = [self._settings_tab, self._arch_tab, self._env_tab,
                          self._flm_tab, self._profiles_tab]
        for idx, btn in enumerate(self._nav_tabs):
            btn.clicked.connect(lambda _c=False, i=idx: self._switch_tab(i))
            btn.setToolTip((btn.toolTip() + "\n" if btn.toolTip() else "") + f"Ctrl+{idx + 1}")
            nav_layout.addWidget(btn)
        self._settings_tab.setChecked(True)
        nav_layout.addStretch(1)

        self._log_btn = QPushButton()
        self._log_btn.setCursor(Qt.PointingHandCursor)
        self._log_btn.setFixedHeight(24)
        self._log_btn.clicked.connect(self._toggle_logging)
        nav_layout.addWidget(self._log_btn)
        nav_layout.addSpacing(6)
        self._lint_btn = QPushButton("Lint \u2714")
        self._lint_btn.setCursor(Qt.PointingHandCursor)
        self._lint_btn.setFixedHeight(24)
        self._lint_btn.setToolTip("Configuration consistency check — click to see the findings")
        self._lint_btn.clicked.connect(self._open_linter)
        self._set_lint_button(0, 0)
        nav_layout.addWidget(self._lint_btn)
        nav_layout.addSpacing(10)

        self._profile_chip = QLabel()
        self._profile_chip.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        nav_layout.addWidget(self._profile_chip)
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
        sidebar_layout.setContentsMargins(8, 8, 4, 6)
        sidebar_layout.setSpacing(6)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter  (Ctrl+F)   ·   search everything: Ctrl+K")
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(f"""
            QLineEdit {{
                background: {T['surface']}; border: 1px solid {T['border']};
                color: {T['text']}; font-size: 12px; padding: 3px 8px;
                border-radius: 4px; min-height: 28px;
            }}
            QLineEdit:focus {{ border-color: {T['accent_dim']}; }}
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
                    f"border-radius:10px;color:{T['text2']};padding:2px 10px;font-size:11px;}}"
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
        _flm_sb_scroll = QScrollArea()
        _flm_sb_scroll.setWidgetResizable(True)
        _flm_sb_scroll.setWidget(self._flm_sidebar)
        _flm_sb_scroll.setStyleSheet(SCROLLABLE_CONTROL_QSS)
        self._sidebar_stack.addWidget(_flm_sb_scroll)

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
        self._sync_tabs.addTab(self._dxvk_conf, "dxvk.conf")
        self._sync_tabs.addTab(self._shortcut, "Shortcut")
        self._sync_tabs.addTab(self._history, "History")
        self._sync_tabs.addTab(self._nip_import, "Import .nip")
        self._sync_tabs.currentChanged.connect(lambda _i: self._refresh_sync_previews())

        splitter.addWidget(sidebar)

        # ===== Right side (editor area) =====
        editor = QWidget()
        editor.setObjectName("editorPane")
        editor.setAttribute(Qt.WA_StyledBackground, True)
        editor.setStyleSheet(f"#editorPane {{ background:{T['bg']}; border-left:1px solid {T['border']}; }}")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(20, 16, 20, 16)
        editor_layout.setSpacing(0)

        self._right_stack = QStackedWidget()
        editor_layout.addWidget(self._right_stack)

        # Page 0: Empty state (text depends on the active tab)
        self._placeholder = QLabel(self.EMPTY_HINTS[0])
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet(f"color: {T['muted']}; font-size: 13px; line-height: 150%;")
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

        # Page 6: Lutris Sync (Profiles tab)
        self._right_stack.addWidget(self._sync_tabs)

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

        # Status bar
        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        status_bar.setStyleSheet(f"""
            QStatusBar {{
                background: {T['bg']}; color: {T['muted']}; font-size: 11px;
                border-top: 1px solid {T['border']}; min-height: 24px;
            }}
        """)
        status_bar.showMessage("Ready")

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
        self._chips.setVisible(self._sidebar_stack.currentIndex() in (self.TAB_DRS, self.TAB_ARCH, self.TAB_ENV))
        QTimer.singleShot(300, SYSHUB.refresh)
        self._schedule_lint()

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
            ev = next((e for e in ALL_ENV_VARS if e.name == name), None)
            if ev:
                self._env_editor.set_var(ev)

    def _refresh_sync_previews(self):
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
            txt, col = f"Lint: {n_err} \u25cf", T["danger"]
        elif n_warn:
            txt, col = f"Lint: {n_warn} \u25b2", T["warn"]
        else:
            txt, col = "Lint \u2714", T["muted"]
        self._lint_btn.setText(txt)
        self._lint_btn.setStyleSheet(
            f"QPushButton {{ color:{col}; background:transparent; border:1px solid {col};"
            f" border-radius:4px; padding:0 10px; font-size:12px; }}"
            f"QPushButton:hover {{ background:{T['surface']}; }}")

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
        self.statusBar().showMessage(f"Applied fix: {issue.fix_label}", 3000)
        self._run_lint()

    def _on_sysinfo(self, info):
        self._refresh_availability()
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
        ev = next((e for e in ALL_ENV_VARS if e.name == name), None)
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
        for title in ("Linter", "System", "Prefix Doctor", "MangoHud", "vkBasalt"):
            items.append(dict(kind="tool", key=title, title=f"{title}", sub="Extra Tools", desc=""))
        for title in ("Launch line", "Lutris", "Steam", "Heroic", "Bottles"):
            items.append(dict(kind="sync", key=title, title=f"{title} sync" if title != "Launch line" else title,
                              sub="Profiles \u203a sync", desc=""))
        return items

    def _open_palette(self):
        dlg = CommandPalette(self._palette_items(), self)
        g = self.geometry()
        dlg.move(g.center().x() - dlg.width() // 2, g.top() + 90)
        if dlg.exec() == QDialog.Accepted and dlg.chosen:
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

    def _select_item(self, lw: QListWidget, key: str):
        for i in range(lw.count()):
            item = lw.item(i)
            if item.data(Qt.UserRole) == key:
                lw.setCurrentItem(item)
                lw.scrollToItem(item, QListWidget.PositionAtCenter)
                return

    # ===== Shortcuts / persisted UI state =====
    def _install_shortcuts(self):
        def sc(seq, fn, parent=None, ctx=Qt.WindowShortcut):
            s_ = QShortcut(QKeySequence(seq), parent or self)
            s_.setContext(ctx)
            s_.activated.connect(fn)
            return s_

        sc("Ctrl+F", self._focus_search)
        sc("Escape", self._clear_search, self._search, Qt.WidgetShortcut)
        for i in range(5):
            sc(f"Ctrl+{i + 1}", lambda i=i: self._switch_tab(i))
        sc("Ctrl+S", self._shortcut_save)
        sc("Ctrl+Shift+C", lambda: self._output_bar._copy_all(steam=False))
        sc("Ctrl+K", self._open_palette)
        sc("Ctrl+D", self._toggle_current_fav)

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
            if 0 <= tab < 5 and tab != 0:
                self._switch_tab(tab)
        except Exception:
            self.resize(1440, 900)

    def closeEvent(self, event):          # noqa: N802
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
        except Exception:
            pass
        try:
            SYSHUB.shutdown()
            self._extra_tools.prefix_doctor._worker and self._extra_tools.prefix_doctor._worker.wait(3000)
        except Exception:
            pass
        super().closeEvent(event)

    # ===== Dirty tracking / badges =====
    def _mark_dirty(self, *_):
        if self.settings_manager.get_current_profile() and not self._dirty:
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

    # ===== Tab switching =====
    def _switch_tab(self, idx):
        # Save current right-panel index for the tab we're leaving
        prev = self._sidebar_stack.currentIndex()
        self._tab_right_memory[prev] = self._right_stack.currentIndex()

        self._sidebar_stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._nav_tabs):
            btn.setChecked(i == idx)

        # The filter only applies to the two long lists.
        filterable = idx in (self.TAB_DRS, self.TAB_ENV)
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

        self._placeholder.setText(self.EMPTY_HINTS.get(idx, ""))

        if idx == self.TAB_PROFILES:
            self._right_stack.setCurrentIndex(self.PAGE_LUTRIS)
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
        self._settings_list.populate(self.all_settings, state, filter_text)
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

    def _open_setting(self, setting_id):
        setting = next((s for s in self.all_settings if s.id == setting_id), None)
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
        ev = next((e for e in ALL_ENV_VARS if e.name == var_name), None)
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
        if self._settings_list.only_set and \
                frozenset(self.settings_manager.get_settings_list()) != self._seen_set_ids:
            self._populate_settings(self._search.text() if self._sidebar_stack.currentIndex() == self.TAB_DRS else "",
                                    quiet=True)
        self._refresh_badges()

    def _toggle_logging(self):
        """One click: off -> ON (error level); on or custom -> OFF."""
        self._env_widget.set_logging(self._env_widget.logging_state() == "off")
        # Re-open the visible env var so its editor shows the new value.
        name = getattr(self._env_editor, "_current_name", "")
        ev = next((e for e in ALL_ENV_VARS if e.name == name), None)
        if ev:
            self._env_editor.set_var(ev)

    def _update_log_btn(self):
        st = self._env_widget.logging_state()
        txt, col = {"on": ("Logging: ON (error)", "#76b900"),
                    "off": ("Logging: OFF", T['muted']),
                    "mixed": ("Logging: custom", "#d9a441")}[st]
        self._log_btn.setText(txt)
        self._log_btn.setToolTip(
            "One-click logging control for all env vars.\n"
            "Off  -> every log level = none/off\n"
            "On   -> every log level = error (not debug)\n"
            "Click: Off -> On, On/custom -> Off.")
        self._log_btn.setStyleSheet(
            f"QPushButton {{ color:{col}; background:transparent; border:1px solid {col};"
            f" border-radius:4px; padding:0 10px; font-size:12px; }}"
            f"QPushButton:hover {{ background:{T['surface']}; }}")

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
        if self._env_widget.only_set and frozenset(self._env_widget.get_env_dict()) != self._seen_env_keys:
            self._env_widget.populate(self._search.text() if self._sidebar_stack.currentIndex() == self.TAB_ENV else "")
        self._seen_env_keys = frozenset(self._env_widget.get_env_dict())

    def _on_profile_loaded(self, name):
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
        # 4. Repopulate lists
        self._populate_settings(self._search.text())
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
            self._profile_chip.setText("No profile loaded")
            self._profile_chip.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
            return
        mark = " •" if self._dirty else ""
        self.setWindowTitle(f"{APP_TITLE} — {name}{mark}")
        if self._dirty:
            self._profile_chip.setText(f"{name} (unsaved changes)")
            self._profile_chip.setStyleSheet(f"color:{T['warn']}; font-size:12px; font-weight:600;")
        else:
            self._profile_chip.setText(name)
            self._profile_chip.setStyleSheet(f"color:{T['text2']}; font-size:12px; font-weight:600;")

    def _on_profile_save_error(self, message: str):
        # Profile persistence failures are silent data-loss risks (the user
        # thinks they clicked "Save" and it worked) — a status bar blip
        # isn't enough visibility for that, so use a modal warning.
        QMessageBox.warning(self, "Profile Save Failed", message)


def main():
    # Gerçek process adını (argv[0]) 'drstool' yapar — python-exec2c/python3
    # olarak görünmesini önler. QApplication oluşturulmadan önce, mümkün
    # olduğunca erken çağrılmalı ki Wayland platform eklentisi app_id'yi
    # okurken zaten doğru argv[0]'ı görsün.
    apply_wayland_app_id(APP_ID)

    # PySide6 prints an unhandled exception raised inside a slot to stderr and
    # then carries on, which can leave the UI half-rebuilt and silently broken
    # (e.g. a populate() that raised after self.clear() leaves an empty list
    # with signals still blocked). Surface it instead of losing it.
    _err = {"showing": False, "last": 0.0}

    def _excepthook(exc_type, exc_value, exc_tb):
        import traceback
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        traceback.print_exception(exc_type, exc_value, exc_tb)
        # A recurring error (e.g. inside paintEvent) must not stack modal
        # dialogs: one at a time, and at most one per 2 seconds.
        if _err["showing"] or time.monotonic() - _err["last"] < 2.0:
            return
        _err["showing"] = True
        try:
            QMessageBox.critical(
                None, "Internal error",
                "".join(traceback.format_exception_only(exc_type, exc_value)),
            )
        except Exception:
            pass
        finally:
            _err["showing"] = False
            _err["last"] = time.monotonic()

    sys.excepthook = _excepthook

    # Wayland altında pencere sınıfının .desktop girdisiyle eşleşmesi için
    # QApplication oluşturulmadan önce app adı ayarlanır.
    QApplication.setDesktopFileName(APP_ID)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName(APP_TITLE)

    if ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    else:
        # Paket kurulumunda script /usr/lib/python-exec/ altında durur ve yanında
        # assets/ yoktur; ebuild ikonu hicolor temasına kurar.
        themed = QIcon.fromTheme(APP_ID)
        if not themed.isNull():
            app.setWindowIcon(themed)
    # .desktop girdisini (Exec=drstool) ebuild /usr/share/applications altına
    # kurar; kullanıcı dizinine ikinci bir python3 sarmalı girdi yazmak KDE'de
    # StartupWMClass eşleşmesini bozduğu için uygulama bunu kendisi yapmaz.

    apply_theme(app)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
