"""Which profile was written to which game.

One JSON file, $XDG_CONFIG_HOME/drstool/game_links.json:

    {"version": 1,
     "games": {"steam:42": {"name": "Cool Game", "launcher": "Steam",
                            "profile": "DLSS", "fp": "<fingerprint of what was written>",
                            "applied_at": 1730000000.0,
                            "target": {"kind": "steam", "key": "42", "path": ".../localconfig.vdf",
                                       "title": "Cool Game  [42]"},
                            "appid": "42", "frametime": "/path/run.csv"}}}

Game keys are game_scan.GameEntry.key ("steam:<appid>", "lutris:<config>", "heroic:<app>",
"bottles:<bottle>", "custom:<path>"). "fp" is a fingerprint of the configuration that was
written; comparing it with the fingerprint of the saved profile says whether the game is
behind its profile. Every change re-reads the file first, so two DRSTool windows do not
undo each other's links. Qt-free.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

STORE_VERSION = 1
_LINK_FIELDS = ("profile", "fp", "applied_at", "target")


def links_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "drstool" / "game_links.json"


def fingerprint(flat: Dict) -> str:
    """Stable hash of a flat {(section, key): value} mapping (state.canon_flat output)."""
    items = sorted(("\x1f".join(map(str, k)) if isinstance(k, tuple) else str(k), str(v))
                   for k, v in (flat or {}).items())
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()[:16]


class LinkStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else links_path()
        self._games: Dict[str, dict] = {}
        self.reload()

    # ── file ────────────────────────────────────────────────────────────────
    def reload(self) -> None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            g = d.get("games") if isinstance(d, dict) else None
            self._games = {str(k): v for k, v in g.items() if isinstance(v, dict)} \
                if isinstance(g, dict) else {}
        except (OSError, ValueError):
            self._games = {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"version": STORE_VERSION, "games": self._games}, indent=2, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _change(self, fn) -> bool:
        """Re-read, apply fn(games) -> changed?, write when it changed."""
        self.reload()
        changed = bool(fn(self._games))
        if changed:
            self._write()
        return changed

    # ── queries ─────────────────────────────────────────────────────────────
    def get(self, key: str) -> dict:
        return dict(self._games.get(key) or {})

    def all(self) -> Dict[str, dict]:
        return {k: dict(v) for k, v in self._games.items()}

    def games_for(self, profile: str) -> List[str]:
        return sorted((k for k, v in self._games.items() if v.get("profile") == profile),
                      key=lambda k: str(self._games[k].get("name") or k).casefold())

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for v in self._games.values():
            p = v.get("profile")
            if p:
                out[p] = out.get(p, 0) + 1
        return out

    def outdated(self, profile: str, fp: str) -> List[str]:
        """Games linked to `profile` whose last write differs from fingerprint `fp`."""
        return [k for k in self.games_for(profile) if self._games[k].get("fp") != fp]

    # ── changes ─────────────────────────────────────────────────────────────
    def record(self, key: str, *, name: str, launcher: str, target: dict,
               profile: Optional[str], fp: str) -> None:
        """A configuration was written to the game (profile None = editor without a profile).
        A name the game already has (from the launcher's library) is kept."""
        def fn(g):
            e = g.setdefault(key, {})
            if name and not e.get("name"):
                e["name"] = name
            e.update(launcher=launcher, target=dict(target), profile=profile or None,
                     fp=fp, applied_at=time.time())
            return True
        self._change(fn)

    def assign(self, key: str, profile: Optional[str], *, name: str = "", launcher: str = "") -> None:
        """Link (or with None unlink) a profile without writing anything to the game."""
        def fn(g):
            e = g.get(key)
            if profile is None:
                if not e:
                    return False
                for f in _LINK_FIELDS:
                    e.pop(f, None)
                if not e.get("appid") and not e.get("frametime"):
                    g.pop(key, None)
                return True
            e = g.setdefault(key, {})
            if e.get("profile") != profile:
                e["profile"] = profile
                e["fp"] = ""                    # nothing of this profile was written yet
            if name:
                e["name"] = name
            if launcher:
                e["launcher"] = launcher
            return True
        self._change(fn)

    def set_field(self, key: str, field: str, value: str, *, name: str = "") -> None:
        """Per-game extras the card remembers: "appid", "frametime"."""
        def fn(g):
            e = g.setdefault(key, {})
            if value:
                e[field] = value
            else:
                e.pop(field, None)
            if name and not e.get("name"):
                e["name"] = name
            if not e:
                g.pop(key, None)
            return True
        self._change(fn)

    def rename_profile(self, old: str, new: str) -> int:
        n = [0]

        def fn(g):
            for e in g.values():
                if e.get("profile") == old:
                    e["profile"] = new
                    n[0] += 1
            return n[0] > 0
        self._change(fn)
        return n[0]

    def drop_profile(self, name: str) -> int:
        """The profile was deleted: its games keep their last write but lose the link."""
        n = [0]

        def fn(g):
            for e in g.values():
                if e.get("profile") == name:
                    e["profile"] = None
                    n[0] += 1
            return n[0] > 0
        self._change(fn)
        return n[0]
