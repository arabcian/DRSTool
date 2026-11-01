"""Shared pieces of the Extra Tools pages: button factory, group styles, navigation list."""

from typing import Dict, Optional, TYPE_CHECKING

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QPushButton, QStyle

from .theme import APPEARANCE, btn_ss, DENSITY_ROWS, T
from .widgets import (
    make_header_item, make_list_item, ROLE_KIND, setup_sidebar_list, SidebarDelegate,
)

if TYPE_CHECKING:
    from .extra_tools import ExtraToolsWidget


FLM_FIELD_SS = f"""
QLineEdit{{
    background:{T['sunken']}; border:1px solid {T['border_hi']}; border-radius:6px;
    color:{T['text']}; font-family:{T['mono']}; font-size:12px; padding:5px 10px;
}}
QLineEdit:focus{{ border:1px solid {T['accent']}; }}
QLineEdit:hover{{ border-color:#38414f; }}
"""

# Shared stylesheet for group boxes inside the Extra Tools panels: a card
_TOOL_GROUP_SS = f"""
QGroupBox{{
    color:{T['text2']}; font-size:12px; font-weight:700;
    background:{T['surface']}; border:1px solid {T['border']}; border-radius:10px;
    margin-top:16px; padding:16px 12px 12px 12px;
}}
QGroupBox::title{{ subcontrol-origin:margin; left:14px; padding:0 6px; color:{T['muted']}; }}
"""


# Shared button factory. The colour arguments are kept for the many existing
# call sites; they are mapped onto the design system's roles.
def _tool_btn(label: str, bg="", border="", fg="", bold=False) -> QPushButton:
    btn = QPushButton(label)
    btn.setCursor(Qt.PointingHandCursor)
    blob = f"{bg}{border}{fg}".lower()
    if any(k in blob for k in ("#26330f", "#76b900", "#4a7300", "#1d2a10")):
        btn.setStyleSheet(btn_ss("primary"))
    elif any(k in blob for k in ("#3a1010", "#7a2020", "#e07070", T["danger"])):
        btn.setStyleSheet(btn_ss("danger"))
    elif any(k in blob for k in ("#33270f", T["warn"].lower())):
        btn.setStyleSheet(
            f"QPushButton{{background:rgba(227,167,63,0.12);border:1px solid {T['warn']};color:{T['warn']};"
            "border-radius:6px;padding:5px 14px;font-weight:700;font-size:12px;}"
            "QPushButton:hover{background:rgba(227,167,63,0.22);}"
            f"QPushButton:disabled{{color:{T['faint']};border-color:{T['border']};background:transparent;}}")
    else:
        btn.setStyleSheet(btn_ss("default"))
    return btn


# Extra Tools navigation: (section, tab title, glyph, one-line subtitle)
TOOL_NAV = [
    ("Build & install", "vk_flip_meter", "\u23f1", "Frame-pacing Vulkan layer · build + live tuning"),
    ("Build & install", "lutris-game-tune", "\u2699", "System tuner: governor, ASPM, C-states, CCD"),
    ("Build & install", "DLL Manager", "\u21e9", "DLSS / Streamline / DXVK / vkd3d / nvapi in every game"),
    ("Build & install", "Tool Manager", "\u2193", "Download Proton / Wine / DXVK / vkd3d"),
    ("Diagnose", "Linter", "\u2713", "Contradictions in the current config"),
    ("Diagnose", "System", "\u25a3", "Driver, session, Vulkan extensions"),
    ("Diagnose", "Live process", "\u25c9", "Did the env / DLLs reach the game?"),
    ("Diagnose", "Logs", "\u2261", "Proton / DXVK logs, known failures"),
    ("Diagnose", "Frametime", "\u223f", "MangoHud / FLM CSV, A/B compare"),
    ("Diagnose", "Cache", "\u25a4", "Shader & pipeline caches"),
    ("Overlays", "MangoHud", "\u25a6", "HUD config builder"),
    ("Overlays", "vkBasalt", "\u25d0", "Post-processing effect chain"),
    ("Knowledge", "Game hints", "\u2605", "ProtonDB + PCGamingWiki per game"),
    ("Knowledge", "Community tips", "\u2691", "User-reported settings (at own risk)"),
    ("Knowledge", "Upstream drift", "\u21c4", "Catalog vs. upstream sources"),
]
ROLE_SUB = Qt.UserRole + 30
ROLE_GLYPH = Qt.UserRole + 31
ROLE_ALERT = Qt.UserRole + 32


class ToolNavDelegate(SidebarDelegate):
    ITEM_H = 50

    def paint(self, p: QPainter, option, index):
        if (index.data(ROLE_KIND) or "item") == "header":
            return super().paint(p, option, index)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect.adjusted(6, 2, -6, -2)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        if selected:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(118, 185, 0, 30))
            p.drawRoundedRect(r, 8, 8)
            p.setBrush(QColor(T["accent"]))
            p.drawRoundedRect(QRect(r.left(), r.top() + 10, 3, r.height() - 20), 1.5, 1.5)
        elif hovered:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T["hover"]))
            p.drawRoundedRect(r, 8, 8)
        icon = QRect(r.left() + 10, r.center().y() - 15, 30, 30)
        p.setPen(QPen(QColor(T["accent_dim"] if selected else T["border_hi"]), 1))
        p.setBrush(QColor(T["raised"] if not selected else "#1b2412"))
        p.drawRoundedRect(icon, 8, 8)
        gf = QFont(option.font)
        gf.setPointSizeF(option.font.pointSizeF() + 2)
        p.setFont(gf)
        p.setPen(QColor(T["accent_hi"] if selected else T["text2"]))
        p.drawText(icon, Qt.AlignCenter, index.data(ROLE_GLYPH) or "")
        tx = icon.right() + 12
        tw = r.right() - tx - 22
        tf = QFont(option.font)
        tf.setWeight(QFont.DemiBold)
        p.setFont(tf)
        p.setPen(QColor(T["text"] if selected or hovered else T["text2"]))
        p.drawText(QRect(tx, r.top() + 6, tw, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(tf).elidedText(index.data(Qt.DisplayRole) or "", Qt.ElideRight, tw))
        sf = QFont(option.font)
        sf.setPointSizeF(max(7.0, option.font.pointSizeF() - 1.5))
        p.setFont(sf)
        p.setPen(QColor(T["muted"]))
        p.drawText(QRect(tx, r.top() + 24, tw, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(sf).elidedText(index.data(ROLE_SUB) or "", Qt.ElideRight, tw))
        if index.data(ROLE_ALERT):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T["warn"]))
            p.drawEllipse(QRect(r.right() - 16, r.center().y() - 4, 8, 8))
        p.restore()


class ExtraToolsSidebarWidget(QListWidget):
    """Left-sidebar navigation for the Extra Tools tab (replaces the old
    13-tab strip and the static text column next to it)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        setup_sidebar_list(self)
        self.setItemDelegate(ToolNavDelegate(self))
        self._tools: Optional["ExtraToolsWidget"] = None
        self._by_title: Dict[str, QListWidgetItem] = {}
        section = None
        for sec, title, glyph, sub in TOOL_NAV:
            if sec != section:
                section = sec
                self.addItem(make_header_item(sec))
            it = make_list_item(title, title)
            it.setData(ROLE_SUB, sub)
            it.setData(ROLE_GLYPH, glyph)
            it.setToolTip(sub)
            self.addItem(it)
            self._by_title[title] = it
        self.currentItemChanged.connect(self._go)

    def bind(self, tools: "ExtraToolsWidget"):
        self._tools = tools
        # Any tool tab not listed above still gets an entry, so nothing is unreachable.
        extra = [t for t in tools.tab_titles() if t not in self._by_title]
        if extra:
            self.addItem(make_header_item("More"))
            for t in extra:
                it = make_list_item(t, t)
                it.setData(ROLE_GLYPH, "\u25c7")
                self.addItem(it)
                self._by_title[t] = it
        tools._tabs.currentChanged.connect(self._sync)
        tools.cache.stale_changed.connect(lambda st: self.set_alert("Cache", st))
        self._sync(tools._tabs.currentIndex())

    def set_alert(self, title: str, on: bool):
        it = self._by_title.get(title)
        if it is not None:
            it.setData(ROLE_ALERT, on)

    def _go(self, cur, _prev=None):
        if cur is not None and self._tools is not None and cur.data(Qt.UserRole):
            self._tools.show_tab(cur.data(Qt.UserRole))

    def _sync(self, idx: int):
        if self._tools is None:
            return
        title = self._tools._tabs.tabText(idx).replace(" \u26a0", "")
        it = self._by_title.get(title)
        if it is not None and it is not self.currentItem():
            self.blockSignals(True)
            self.setCurrentItem(it)
            self.blockSignals(False)


ToolNavDelegate.ITEM_H = DENSITY_ROWS[APPEARANCE["density"]][2]
