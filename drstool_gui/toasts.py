"""Toast notifications, the Undo offer after file writes and desktop notifications."""

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QEvent, QObject, QProcess, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStatusBar, QToolButton,
)

from .base import _PENDING_BAKS
from .theme import btn_ss, T

_log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Toasts
# ----------------------------------------------------------------------------

TOASTS: Optional["ToastHost"] = None


def toast(text: str, kind: str = "info", timeout: int = 3500, action=None):
    """Non-blocking message over the main window. action = (label, callable)."""
    if TOASTS is not None:
        try:
            TOASTS.show_toast(text, kind, timeout, action)
        except Exception:
            _log.debug("ignored error", exc_info=True)
            pass


def _guess_kind(text: str) -> str:
    t = text.lstrip()
    low = t.lower()
    if "failed" in low or "error" in low or t.startswith("✖"):
        return "error"
    if t.startswith("⚠") or low.startswith(("nothing", "no ")):
        return "warn"
    if t.startswith(("✔", "✓")) or low.startswith(
            ("saved", "copied", "wrote", "applied", "imported", "loaded", "undid", "redid",
             "restored", "added", "removed", "★ added")):
        return "ok"
    return "info"


class _Toast(QFrame):
    dismissed = Signal(object)

    def __init__(self, text: str, kind: str, action, parent, timeout: int):
        super().__init__(parent)
        self.text, self.action = text, action
        self.setObjectName("toast")
        self.setAttribute(Qt.WA_StyledBackground, True)
        col = {"ok": T["accent"], "warn": T["warn"], "error": T["danger"]}.get(kind, T["info"])
        glyph = {"ok": "✔", "warn": "⚠", "error": "✖"}.get(kind, "ℹ")
        self.setStyleSheet(
            f"#toast{{background:{T['overlay']};border:1px solid {T['border_hi']};"
            f"border-left:3px solid {col};border-radius:8px;}}"
            f"#toast QLabel{{background:transparent;color:{T['text']};font-size:12px;}}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 9, 8, 9)
        lay.setSpacing(10)
        g = QLabel(glyph)
        g.setStyleSheet(f"color:{col};font-size:13px;font-weight:700;background:transparent;")
        lay.addWidget(g, 0, Qt.AlignTop)
        self._lbl = QLabel(text)
        self._lbl.setWordWrap(True)
        self._lbl.setMaximumWidth(380)
        lay.addWidget(self._lbl, 1)
        if action:
            b = QPushButton(action[0])
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet(btn_ss("ghost", "sm"))
            b.clicked.connect(self._run_action)
            lay.addWidget(b, 0, Qt.AlignVCenter)
        x = QToolButton()
        x.setText("✕")
        x.setCursor(Qt.PointingHandCursor)
        x.setFocusPolicy(Qt.NoFocus)
        x.setAccessibleName("Dismiss notification")
        x.setStyleSheet(f"QToolButton{{border:none;color:{T['muted']};padding:2px 5px;background:transparent;}}"
                        f"QToolButton:hover{{color:{T['text']};}}")
        x.clicked.connect(self.dismiss)
        lay.addWidget(x, 0, Qt.AlignTop)
        self.setAccessibleName("Notification")
        self.setAccessibleDescription(text)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self.restart(timeout)

    def restart(self, timeout: int):
        self._timer.start(max(1500, int(timeout)))

    def _run_action(self):
        self.dismiss()
        try:
            self.action[1]()
        except Exception as ex:
            toast(f"Action failed: {ex}", "error")

    def dismiss(self):
        self._timer.stop()
        self.dismissed.emit(self)

    def enterEvent(self, e):              # noqa: N802
        self._timer.stop()                # hold while the pointer is on it
        super().enterEvent(e)

    def leaveEvent(self, e):              # noqa: N802
        self.restart(1800)
        super().leaveEvent(e)


class ToastHost(QObject):
    """Stacks toasts in the top-right corner of the main window, under the nav bar."""
    MAX = 4

    def __init__(self, win: QMainWindow):
        super().__init__(win)
        self._win = win
        self._toasts: List[_Toast] = []
        win.installEventFilter(self)

    def show_toast(self, text: str, kind: str = "info", timeout: int = 3500, action=None):
        text = str(text).strip()
        if not text:
            return
        for t in self._toasts:                       # same message again: refresh, don't stack
            if t.text == text and action is None and t.action is None:
                t.restart(timeout)
                return
        t = _Toast(text, kind, action, self._win, timeout)
        t.dismissed.connect(self._remove)
        self._toasts.append(t)
        while len(self._toasts) > self.MAX:
            self._remove(self._toasts[0])
        t.show()
        self._layout()

    def _remove(self, t: "_Toast"):
        if t in self._toasts:
            self._toasts.remove(t)
            t.hide()
            t.deleteLater()
            self._layout()

    def _layout(self):
        w = self._win
        y = 66
        for t in self._toasts:
            t.adjustSize()
            t.move(max(8, w.width() - t.width() - 16), y)
            t.raise_()
            y += t.height() + 8

    def eventFilter(self, obj, ev):           # noqa: N802
        if obj is self._win and ev.type() == QEvent.Resize:
            self._layout()
        return False


def _install_toasts(win: QMainWindow):
    global TOASTS
    TOASTS = ToastHost(win)


class ToastStatusBar(QStatusBar):
    """Every timed status message also appears as a toast (the bar text is easy to miss)."""

    def showMessage(self, text, timeout=0):       # noqa: N802
        super().showMessage(text, timeout)
        if timeout and text:
            toast(text, _guess_kind(text), min(max(timeout, 2500), 9000))


def _file_sig(p: Path) -> tuple:
    st = p.stat()
    return (st.st_mtime_ns, st.st_size)


def offer_undo(label: str):
    now = time.monotonic()
    pend, seen = [], set()
    for tgt, bak, t0 in reversed(_PENDING_BAKS):
        try:
            if now - t0 <= 15 and tgt not in seen and bak.is_file() and tgt.is_file():
                seen.add(tgt)
                pend.append((tgt, bak, _file_sig(tgt)))
        except OSError:
            continue
    _PENDING_BAKS.clear()
    if not pend:
        toast(label, "ok", 5000)
        return

    def undo():
        done, bad = 0, []
        for tgt, bak, sig in pend:
            try:
                if _file_sig(tgt) != sig:
                    bad.append(f"{tgt.name} changed since the write")
                    continue
                # follows a symlinked target and keeps its mode (the old fixed
                # ".drstool-tmp" + os.replace replaced the link itself)
                real = Path(os.path.realpath(tgt))
                fd, tmp = tempfile.mkstemp(dir=str(real.parent), prefix=f".{real.name}.",
                                           suffix=".drstool-tmp")
                try:
                    with os.fdopen(fd, "wb") as out, open(bak, "rb") as src:
                        shutil.copyfileobj(src, out)
                    shutil.copymode(real, tmp)
                    os.replace(tmp, real)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
                done += 1
            except OSError as ex:
                bad.append(f"{tgt.name}: {ex}")
        w = TOASTS._win if TOASTS is not None else None
        for fn in ("_refresh_sync_previews", "_ext_mark_lutris"):
            if w is not None and hasattr(w, fn):
                try:
                    getattr(w, fn)()
                except Exception:
                    _log.debug("ignored error", exc_info=True)
                    pass
        toast(f"Restored {done} file(s) from backup" + ((" — " + "; ".join(bad)) if bad else ""),
              "warn" if bad else "ok", 7000)

    toast(label, "ok", 12000, ("Undo", undo))


# ----------------------------------------------------------------------------
# Background task centre + desktop notifications
# ----------------------------------------------------------------------------

def desktop_notify(title: str, body: str = ""):
    """freedesktop notification through notify-send, or gdbus as a fallback."""
    try:
        ns = shutil.which("notify-send")
        if ns:
            QProcess.startDetached(ns, ["-a", "DRSTool", "-t", "6000", title, body])
            return
        gd = shutil.which("gdbus")
        if gd:
            QProcess.startDetached(gd, [
                "call", "--session", "--dest", "org.freedesktop.Notifications",
                "--object-path", "/org/freedesktop/Notifications",
                "--method", "org.freedesktop.Notifications.Notify",
                "DRSTool", "0", "", title, body, "[]", "{}", "6000"])
    except Exception:
        _log.debug("ignored error", exc_info=True)
        pass
