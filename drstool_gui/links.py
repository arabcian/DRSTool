"""Profile ↔ game links (drstool_core.game_links) for the GUI: one shared store that
announces changes, plus the fingerprints that tell whether a game is behind its profile."""

import re
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Signal

from drstool_core import game_links

from .state import canon_flat, state_as_profile, state_from_profile


def state_fp(state: dict) -> str:
    """Fingerprint of an editor state (MainWindow.capture_state())."""
    return game_links.fingerprint(canon_flat(state_as_profile(state)))


def profile_fp(profile) -> str:
    """Fingerprint of a saved profile; equal to state_fp() of the same profile loaded."""
    return state_fp(state_from_profile(profile))


# target["kind"] -> game_scan key prefix
_KIND_PREFIX = {"lutris": "lutris", "steam": "steam", "heroic": "heroic", "bottles": "bottles"}


def game_key_for_target(target: dict) -> str:
    """game_scan.GameEntry.key of the game a sync tab wrote to ("" = not a single game)."""
    kind, key = target.get("kind", ""), str(target.get("key", ""))
    if kind not in _KIND_PREFIX or not key or key.startswith("__"):
        return ""
    return f"{_KIND_PREFIX[kind]}:{key}"


def plain_title(title: str) -> str:
    """'Cool Game  [42]' -> 'Cool Game'."""
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", title or "").strip()


class _LinkHub(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self._store: Optional[game_links.LinkStore] = None

    @property
    def store(self) -> game_links.LinkStore:
        # Created on first use and again when XDG_CONFIG_HOME / HOME changed (tests).
        if self._store is None or self._store.path != game_links.links_path():
            self._store = game_links.LinkStore()
        return self._store

    def get(self, key: str) -> dict:
        return self.store.get(key)

    def all(self) -> Dict[str, dict]:
        return self.store.all()

    def counts(self) -> Dict[str, int]:
        return self.store.counts()

    def games_for(self, profile: str) -> List[str]:
        return self.store.games_for(profile)

    def outdated(self, profile: str, fp: str) -> List[str]:
        return self.store.outdated(profile, fp)

    def _do(self, fn, *a, **kw):
        try:
            res = fn(*a, **kw)
        except OSError:
            return None                   # links are a convenience: never break a write over them
        self.changed.emit()
        return res

    def record(self, key: str, **kw):
        return self._do(self.store.record, key, **kw)

    def assign(self, key: str, profile: Optional[str], **kw):
        return self._do(self.store.assign, key, profile, **kw)

    def set_field(self, key: str, field: str, value: str, **kw):
        return self._do(self.store.set_field, key, field, value, **kw)

    def rename_profile(self, old: str, new: str):
        return self._do(self.store.rename_profile, old, new)

    def drop_profile(self, name: str):
        return self._do(self.store.drop_profile, name)


LINKS = _LinkHub()
