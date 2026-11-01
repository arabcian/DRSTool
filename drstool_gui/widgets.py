"""Small reusable widgets and style fragments: sidebar list delegate, elided labels, nav tabs,
empty states, shared QSS strings.
"""

import hashlib as _hl
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QSizePolicy,
    QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from .base import APP_TITLE
from .theme import _tc, APPEARANCE, DENSITY_ROWS, T

if TYPE_CHECKING:
    from drstool_core.models import Setting


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
    HEADER_H = 34
    ITEM_H = 30
    _value_fonts: dict = {}          # point size -> (QFont, QFontMetrics) of the value chip

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
            f.setPointSizeF(max(7.0, base_font.pointSizeF() - 1.5))
            f.setLetterSpacing(QFont.AbsoluteSpacing, 1.2)
            f.setCapitalization(QFont.AllUppercase)
            p.setFont(f)
            fm = QFontMetrics(f)
            text_rect = QRect(r.left() + 14, r.top() + 10, r.width() - 28, r.height() - 10)
            p.setPen(QColor(T["muted"]))
            p.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, name)
            x0 = text_rect.left() + fm.horizontalAdvance(name.upper()) + 12
            x1 = r.right() - 14
            cnt = index.data(ROLE_COUNT) or 0
            if cnt:
                badge = str(cnt)
                bw = max(18, fm.horizontalAdvance(badge) + 12)
                br = QRect(x1 - bw, text_rect.center().y() - 8, bw, 16)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(118, 185, 0, 40))
                p.drawRoundedRect(br, 8, 8)
                p.setPen(QColor(T["accent_hi"]))
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
        card = r.adjusted(6, 1, -6, -1)

        if selected:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(118, 185, 0, 30))
            p.drawRoundedRect(card, 6, 6)
            p.setBrush(QColor(T["accent"]))
            p.drawRoundedRect(QRect(card.left(), card.top() + 6, 3, card.height() - 12), 1.5, 1.5)
        elif hovered:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T["hover"]))
            p.drawRoundedRect(card, 6, 6)

        if is_set:
            c = card.center().y()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(118, 185, 0, 55))
            p.drawEllipse(QRect(card.left() + 9, c - 5, 10, 10))
            p.setBrush(QColor(T["accent_hi"]))
            p.drawEllipse(QRect(card.left() + 11, c - 3, 6, 6))

        value = index.data(ROLE_VALUE) or ""
        pad_l = card.left() + 26
        avail = card.right() - 8 - pad_l
        val_w = 0
        if value:
            vsize = max(7.0, base_font.pointSizeF() - 1.5)
            cached = self._value_fonts.get(vsize)       # built once per size, not once per painted row
            if cached is None:
                vf = QFont()
                vf.setFamilies(["JetBrains Mono", "Fira Code", "DejaVu Sans Mono", "monospace"])
                vf.setStyleHint(QFont.Monospace)
                vf.setPointSizeF(vsize)
                cached = self._value_fonts[vsize] = (vf, QFontMetrics(vf))
            vf, vfm = cached
            max_val = int(avail * 0.45)
            shown = vfm.elidedText(value, Qt.ElideRight, max_val - 12)
            tw = vfm.horizontalAdvance(shown)
            val_w = tw + 12
            chip = QRect(card.right() - 6 - val_w, card.center().y() - 9, val_w, 18)
            p.setPen(Qt.NoPen)
            if is_set:
                p.setBrush(QColor(118, 185, 0, 28))
                p.drawRoundedRect(chip, 5, 5)
            p.setFont(vf)
            p.setPen(QColor(T["accent_hi"]) if is_set else QColor(T["faint"]))
            p.drawText(chip, Qt.AlignCenter, shown)

        fav = bool(index.data(ROLE_FAV))
        unavail = bool(index.data(ROLE_UNAVAIL))
        nf = QFont(base_font)
        if is_set or selected:
            nf.setWeight(QFont.DemiBold)
        if unavail:
            nf.setItalic(True)
        p.setFont(nf)
        nfm = QFontMetrics(nf)
        name_w = avail - (val_w + 10 if val_w else 0)
        if kind == "special":
            color = QColor(T["warn"])
        elif unavail and not is_set:
            color = QColor(T["faint"])
        elif is_set or selected:
            color = QColor(T["text"])
        else:
            color = QColor(T["text2"])
        text_x = pad_l
        if fav:
            p.setPen(QColor(_tc("#e8b84a")))
            p.drawText(QRect(pad_l, card.top(), 14, card.height()), Qt.AlignLeft | Qt.AlignVCenter, "\u2605")
            text_x += 16
            name_w -= 16
        p.setPen(color)
        p.drawText(QRect(text_x, card.top(), max(0, name_w), card.height()),
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


class BadgeElidedLabel(ElidedLabel):
    """ElidedLabel for header value badges: asks for its full text width but
    may shrink (eliding) when space runs out, instead of forcing the whole
    panel to be as wide as e.g. a 12-flag VKD3D_CONFIG string."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def setText(self, text: str):          # noqa: N802
        super().setText(text)
        self.updateGeometry()

    def sizeHint(self):                   # noqa: N802
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 26,
                     QLabel.sizeHint(self).height())


class NavTab(QPushButton):
    """Segmented top navigation: glyph + title + a small live badge."""

    def __init__(self, title: str, glyph: str = "", parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(7)
        self._glyph = QLabel(glyph)
        self._title = QLabel(title)
        self._badge = QLabel()
        for w in (self._glyph, self._title, self._badge):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._glyph.setVisible(bool(glyph))
        self._badge.setFixedHeight(17)
        lay.addWidget(self._glyph, 0, Qt.AlignVCenter)
        lay.addWidget(self._title, 0, Qt.AlignVCenter)
        lay.addWidget(self._badge, 0, Qt.AlignVCenter)
        lay.addStretch(1)
        self._badge.hide()
        self.setFixedHeight(32)
        self.setStyleSheet(f"""
QPushButton {{ background:transparent; border:1px solid transparent; border-radius:8px; }}
QPushButton:hover {{ background:{T['hover']}; }}
QPushButton:checked {{ background:{T['overlay']}; border:1px solid {T['border_hi']}; }}
""")
        self.toggled.connect(self._restyle)
        self._restyle(False)
        self._fit()

    def set_badge(self, text: str):
        self._badge.setText(text)
        self._badge.setVisible(bool(text))
        self._fit()

    def _fit(self):
        """Labels get their final (bold) font from a stylesheet only once
        polished, so the layout's hint can be too small; size from metrics."""
        f = QFont(self.font())
        f.setPixelSize(12)
        f.setWeight(QFont.DemiBold)
        w = 28 + QFontMetrics(f).horizontalAdvance(self._title.text()) + 6
        if self._glyph.isVisibleTo(self) or self._glyph.text():
            w += 24
        if self._badge.text():
            fb = QFont(f)
            fb.setPixelSize(10)
            fb.setFamilies(["JetBrains Mono", "Fira Code", "DejaVu Sans Mono"])
            fb.setWeight(QFont.ExtraBold)
            w += 9 + QFontMetrics(fb).horizontalAdvance(self._badge.text()) + 22
        self.setMinimumWidth(w)
        self.updateGeometry()

    def sizeHint(self):                   # noqa: N802
        return self.layout().sizeHint().expandedTo(QSize(self.minimumWidth(), 32))

    def minimumSizeHint(self):            # noqa: N802
        return self.sizeHint()

    def _restyle(self, checked: bool):
        self._glyph.setStyleSheet(
            f"color:{T['accent_hi'] if checked else T['faint']}; font-size:13px; background:transparent;")
        self._title.setStyleSheet(
            f"color:{T['text'] if checked else T['muted']}; font-size:12px; font-weight:600;"
            " background:transparent;")
        self._badge.setStyleSheet(
            f"color:{'#0a0f02' if checked else T['text2']};"
            f" background:{T['accent'] if checked else T['raised']};"
            " border-radius:8px; padding:0 6px; font-size:10px; font-weight:800;"
            f" font-family:{T['mono']};")


class BrandMark(QWidget):
    """App logo: a green tile with a stylised frametime trace + wordmark."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._compact = False
        self.setFixedSize(150, 34)
        self.setToolTip(APP_TITLE)

    def set_compact(self, on: bool):
        if on != self._compact:
            self._compact = on
            self.setFixedSize(30 if on else 150, 34)
            self.update()

    def paintEvent(self, _e):             # noqa: N802
        from PySide6.QtGui import QLinearGradient, QPainterPath
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        tile = QRect(0, 3, 28, 28)
        g = QLinearGradient(0, 3, 28, 31)
        g.setColorAt(0, QColor(_tc("#9be12a")))
        g.setColorAt(1, QColor(_tc("#4f8a00")))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(tile, 7, 7)
        path = QPainterPath()
        pts = [(5, 22), (9, 22), (11, 13), (14, 25), (17, 9), (20, 20), (23, 20)]
        path.moveTo(*pts[0])
        for x, y in pts[1:]:
            path.lineTo(x, y)
        pen = QPen(QColor(_tc("#0b1300")), 2.2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        if self._compact:
            p.end()
            return
        f = QFont(self.font())
        f.setPointSizeF(12)
        f.setWeight(QFont.Bold)
        p.setFont(f)
        p.setPen(QColor(T["text"]))
        p.drawText(QRect(38, 2, 120, 18), Qt.AlignLeft | Qt.AlignVCenter, "DRSTool")
        f2 = QFont(self.font())
        f2.setPointSizeF(7.5)
        f2.setLetterSpacing(QFont.AbsoluteSpacing, 0.8)
        p.setFont(f2)
        p.setPen(QColor(T["muted"]))
        p.drawText(QRect(38, 19, 120, 14), Qt.AlignLeft | Qt.AlignVCenter, "DXVK-NVAPI · DRS")
        p.end()


class EmptyState(QWidget):
    """Centered placeholder: big glyph, title, hint and keycap shortcuts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        self._glyph = QLabel()
        self._glyph.setAlignment(Qt.AlignCenter)
        self._glyph.setFixedSize(64, 64)
        self._glyph.setStyleSheet(
            f"background:{T['surface']}; border:1px solid {T['border_hi']}; border-radius:16px;"
            f" color:{T['accent']}; font-size:26px;")
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._glyph)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addSpacing(14)
        self._title = QLabel()
        self._title.setAlignment(Qt.AlignCenter)
        self._title.setStyleSheet(f"color:{T['text']}; font-size:15px; font-weight:700;")
        outer.addWidget(self._title)
        self._hint = QLabel()
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        outer.addWidget(self._hint)
        outer.addSpacing(16)
        self._keys = QLabel()
        self._keys.setAlignment(Qt.AlignCenter)
        self._keys.setTextFormat(Qt.RichText)
        outer.addWidget(self._keys)
        outer.addStretch(2)
        self.set_content("◆", "", "")

    @staticmethod
    def _kbd(k: str) -> str:
        return (f'<span style="background:{T["raised"]}; color:{T["text"]}; font-family:monospace;'
                f' font-size:11px;">&nbsp;{k}&nbsp;</span>')

    def set_content(self, glyph: str, title: str, hint: str,
                    keys=(("Ctrl+K", "search everything"), ("Ctrl+F", "filter"),
                          ("Ctrl+Shift+V", "import a launch line"), ("Ctrl+R", "recipes"))):
        self._glyph.setText(glyph)
        self._title.setText(title)
        self._title.setVisible(bool(title))
        self._hint.setText(hint)
        parts = [f'{self._kbd(k)}&nbsp;<span style="color:{T["muted"]}">{d}</span>' for k, d in keys]
        self._keys.setText("&nbsp;&nbsp;&nbsp;&nbsp;".join(parts))

    # Old code called setText()/text() on the QLabel placeholder.
    def setText(self, text: str):         # noqa: N802
        lines = (text or "").split("\n", 1)
        self.set_content(self._glyph.text(), lines[0], lines[1] if len(lines) > 1 else "")


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


_LAUNCHER_QSS = (f"QListWidget{{background:{T['surface']};border:1px solid {T['border']};"
                 f"border-radius:4px;color:{T['text2']};font-size:12px;}}"
                 f"QListWidget::item{{padding:3px 6px;}}"
                 f"QListWidget::item:selected{{background:rgba(118,185,0,0.18);color:{T['text']};}}")
_MONO_BOX_QSS = (f"QPlainTextEdit{{background:{T['bg']};border:1px solid {T['border_hi']};"
                 f"border-radius:4px;color:{T['text']};font-family:{T['mono']};font-size:12px;"
                 "padding:4px 6px;}")


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


# ============================================================================
# Shared helpers for the Extra Tools and sync pages
# ============================================================================


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


# Row density (Appearance dialog). The classes above are already defined here.
SidebarDelegate.ITEM_H, SidebarDelegate.HEADER_H = DENSITY_ROWS[APPEARANCE["density"]][:2]
