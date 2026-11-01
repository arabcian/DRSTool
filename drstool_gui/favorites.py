"""Favourites store and the right-click menu that toggles them."""

import logging
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import QListWidget, QMenu

from .base import APP_ID
from .widgets import ROLE_KIND

_log = logging.getLogger(__name__)


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
                _log.debug("ignored error", exc_info=True)
                self._data = {}
        return self._data

    def _save(self):
        try:
            qs = QSettings(APP_ID, APP_ID)
            for kind, lst in self._load().items():
                qs.setValue(f"favorites/{kind}", list(lst))
        except Exception:
            _log.debug("ignored error", exc_info=True)
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
