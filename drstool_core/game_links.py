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
from copy import deepcopy
import json
import os
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from .file_transactions import atomic_write_json, file_lock

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
        self._sig = None             # identity of the file version self._games was parsed from
        self.load_error = None
        self.reload()

    # ── file ────────────────────────────────────────────────────────────────
    def reload(self, *, force: bool = False) -> None:
        """Re-read the store if the file changed since the last read.

        Queries run on the GUI thread, often once per game in a list, and the file grows with
        every capture remembered; parsing it per call froze the UI for seconds. Every write
        (ours and other windows') goes through an atomic replace, so the file's inode/mtime/size
        signature changes with each version and an unchanged signature means unchanged content.
        """
        try:
            with open(self.path, encoding="utf-8") as fh:
                st = os.fstat(fh.fileno())
                sig = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
                if not force and sig == self._sig:
                    return
                text = fh.read()
        except FileNotFoundError:
            self._games, self._sig, self.load_error = {}, "missing", None
            return
        except (OSError, ValueError) as exc:     # ValueError: not UTF-8
            self._sig, self.load_error = None, str(exc)
            return
        self._sig, self.load_error = sig, None
        try:
            d = json.loads(text)
            g = d.get("games") if isinstance(d, dict) else None
            if not isinstance(g, dict) or any(not isinstance(v, dict) for v in g.values()):
                raise ValueError('invalid game links store')
            self._games = g
        except ValueError as exc:
            self.load_error = str(exc)  # retain last valid snapshot, never overwrite corrupt data

    def _write(self) -> None:
        atomic_write_json(self.path, {"version": STORE_VERSION, "games": self._games})

    def _change(self, fn) -> bool:
        """Lock before reload+mutate+write so concurrent windows preserve edits."""
        with file_lock(self.path):
            self.reload(force=True)
            if self.load_error:
                raise ValueError(f"Game links store cannot be read; original preserved: {self.path}: {self.load_error}")
            previous = deepcopy(self._games)
            try:
                changed = bool(fn(self._games))
                if changed:
                    if self.path.exists():
                        atomic_write_json(self.path.with_suffix(self.path.suffix + '.bak'),
                                          {'version': STORE_VERSION, 'games': previous})
                    self._write()
            finally:
                # Next query re-reads whatever is on disk now (also drops an in-memory mutation
                # whose write failed).
                self._sig = None
            return changed

    def recover_backup(self, *, only_if_corrupt: bool = False) -> Path:
        """Explicit recovery only; preserve the corrupt original for investigation."""
        with file_lock(self.path):
            self.reload(force=True)
            if only_if_corrupt and not self.load_error:
                raise ValueError('Game links store is no longer corrupt; recovery cancelled')
            candidate = self.path.with_suffix(self.path.suffix + '.bak')
            doc = json.loads(candidate.read_text(encoding='utf-8'))
            if not isinstance(doc, dict) or not isinstance(doc.get('games'), dict) or any(
                    not isinstance(v, dict) for v in doc['games'].values()):
                raise ValueError('invalid game links backup')
            quarantine = self.path.with_name(self.path.name + '.corrupt-' + uuid.uuid4().hex)
            if self.path.exists():
                from .file_transactions import atomic_copy
                atomic_copy(self.path, quarantine)
            atomic_write_json(self.path, doc)
            self.reload(force=True)
            return quarantine

    # ── queries ─────────────────────────────────────────────────────────────
    def get(self, key: str) -> dict:
        self.reload()
        return deepcopy(self._games.get(key) or {})

    def all(self) -> Dict[str, dict]:
        self.reload()
        return deepcopy(self._games)

    def games_for(self, profile: str) -> List[str]:
        self.reload()
        return sorted((k for k, v in self._games.items() if v.get("profile") == profile),
                      key=lambda k: str(self._games[k].get("name") or k).casefold())

    def counts(self) -> Dict[str, int]:
        self.reload()
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
        A nonempty current launcher name replaces the previously recorded name."""
        def fn(g):
            e = g.setdefault(key, {})
            if name:
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
                if not e.get("appid") and not e.get("frametime") and not e.get("perf"):
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

    def add_perf(self, key: str, run: dict, limit: int = 40) -> bool:
        """Remember one frametime capture of the game ({"path", "mtime", "avg", "low1", ...}),
        tagged with the profile and fingerprint written to the game when it was recorded
        (fp "" = the capture predates the last write). Returns False if already known."""
        def fn(g):
            e = g.setdefault(key, {})
            hist = e.setdefault("perf", [])
            if any(r.get("path") == run.get("path") and r.get("mtime") == run.get("mtime") for r in hist):
                return False
            newer = (run.get("mtime") or 0) >= (e.get("applied_at") or 0)
            # A supervised session pins the profile at launch time; a GUI
            # assignment made while the game ran must not relabel the capture.
            explicit = bool(run.get('session_id'))
            hist.append(dict(run,
                             fp=run.get('fp', '') if explicit else (e.get('fp', '') if newer else ''),
                             profile=run.get('profile') if explicit else (e.get('profile') if newer else None)))
            hist.sort(key=lambda r: r.get("mtime") or 0)
            del hist[:-max(1, int(limit))]
            return True
        return self._change(fn)

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
