"""Swap DLLs inside game folders with a restorable backup.

Backups live outside the game folder (so "verify game files" in a launcher
never trips over them):

    $XDG_DATA_HOME/drstool/dll-backups/<game>/<timestamp>/
        manifest.json     what was replaced, sha256 before and after
        files/<n>-<name>  the original files

Rules:
  * only files that already exist are replaced — nothing is added to a game;
  * symlinks are never written through;
  * each file is replaced atomically (temp file in the same folder + rename);
  * the manifest is written before the first replacement and updated after
    each one, so an interrupted run can still be restored;
  * restore only puts a file back if it is still the one we installed — if the
    game updated it in the meantime, that file is left alone and reported.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import file_transactions as tx

KEEP_BACKUPS = 10


def backup_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "drstool" / "dll-backups"


def _slug(s: str) -> str:
    return (re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.") or "game")[:80]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_pe(path: Path, machine: int | None = None) -> bool:
    """Validate DOS header, bounded PE offset, signature and DLL machine type.

    Merely checking b'MZ' accepts arbitrary files and truncated downloads.
    """
    try:
        with path.open("rb") as f:
            head = f.read(64)
            if len(head) != 64 or head[:2] != b"MZ":
                return False
            offset = int.from_bytes(head[0x3c:0x40], 'little')
            if not (64 <= offset <= 1024 * 1024):
                return False
            f.seek(offset)
            pe = f.read(24)
            if len(pe) < 24 or pe[:4] != b"PE\0\0":
                return False
            arch = int.from_bytes(pe[4:6], 'little')
            return arch in (0x14c, 0x8664) and (machine is None or arch == machine)
    except (OSError, ValueError):
        return False


def atomic_copy(src: Path, dst: Path):
    """Atomic, fsync'd copy guarded against symlink substitution."""
    tx.atomic_copy(src, dst)


@dataclass
class ReplaceResult:
    backup_dir: Optional[Path]
    replaced: List[Path]
    skipped: List[Tuple[Path, str]]


def check_target(target: Path) -> str:
    """'' if target may be replaced, else the reason why not."""
    if target.is_symlink():
        return "is a symlink (not written through)"
    if not target.is_file():
        return "does not exist (nothing is added to games)"
    if not os.access(target.parent, os.W_OK) or not os.access(target, os.W_OK):
        return "not writable"
    return ""


def replace_files(game_key: str, game_name: str, pairs: List[Tuple[Path, Path, str]],
                  log: Callable[[str], None] = lambda _m: None,
                  root: Optional[Path] = None) -> ReplaceResult:
    """pairs: [(target in the game, new file, label e.g. 'DLSS SR 310.2.1')]."""
    root = root or backup_root()
    work, skipped = [], []
    src_sha: Dict[Path, str] = {}       # one source file often goes to several targets
    old_sha: Dict[Path, str] = {}       # target digest, reused as the backup's old_sha256
    for target, src, label in pairs:
        why = check_target(target)
        if not why and not _is_pe(src):
            why = f"replacement {src.name} is not a Windows DLL"
        if not why:
            try:
                if src not in src_sha:
                    src_sha[src] = sha256(src)
                old_sha[target] = sha256(target)
                if old_sha[target] == src_sha[src]:
                    why = "already this version"
            except OSError as ex:
                why = str(ex)
        if why:
            skipped.append((target, why))
            log(f"  – {target.name}: {why}")
        else:
            work.append((target, src, label))
    if not work:
        return ReplaceResult(None, [], skipped)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bdir = root / _slug(game_key) / stamp
    n = 1
    while bdir.exists():
        n += 1
        bdir = root / _slug(game_key) / f"{stamp}-{n}"
    (bdir / "files").mkdir(parents=True)
    changes = [tx.Change(target, src, f"{i:03d}-{target.name}", label)
               for i, (target, src, label) in enumerate(work)]
    try:
        replaced = tx.apply(changes, bdir, log=log, journal_name="manifest.json")
        manifest = json.loads((bdir / "manifest.json").read_text())
        manifest.update(game_key=game_key, game=game_name, created=stamp)
        _write_json(bdir / "manifest.json", manifest)
    except (OSError, ValueError, RuntimeError) as ex:
        # All files in a single game are rolled back as a group.  Keep the
        # transaction journal for recovery from a rare rollback failure.
        log(f"  ✗ game DLL transaction rolled back: {ex}")
        skipped.extend((target, f"transaction failed: {ex}") for target, _src, _label in work)
        return ReplaceResult(bdir, [], skipped)
    prune(game_key, root)
    return ReplaceResult(bdir, replaced, skipped)


def _write_json(path: Path, data: dict):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".manifest-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def list_backups(game_key: str, root: Optional[Path] = None) -> List[Tuple[Path, dict]]:
    """[(backup dir, manifest)] newest first; entries without a usable manifest are ignored."""
    d = (root or backup_root()) / _slug(game_key)
    out = []
    try:
        subs = sorted((p for p in d.iterdir() if p.is_dir()), reverse=True)
    except OSError:
        return []
    for p in subs:
        try:
            m = json.loads((p / "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and isinstance(m.get("entries"), list):
            out.append((p, m))
    return out


def restore(backup_dir: Path, log: Callable[[str], None] = lambda _m: None) -> Tuple[int, List[str]]:
    """Put the originals from one backup back. Returns (restored count, notes)."""
    m = json.loads((backup_dir / "manifest.json").read_text())
    restored, notes = 0, []
    for e in reversed(m.get("entries", [])):
        try:
            target = Path(e["target"])
            bfile = backup_dir / "files" / Path(e["backup"]).name
        except (KeyError, TypeError):
            notes.append("malformed manifest entry skipped")
            continue
        if not bfile.is_file():
            notes.append(f"{target.name}: backup file missing")
            continue
        if target.is_symlink():
            notes.append(f"{target}: is a symlink now — left alone")
            continue
        try:
            cur = sha256(target) if target.is_file() else ""
        except OSError as ex:
            notes.append(f"{target}: {ex}")
            continue
        if cur and cur == e.get("old_sha256"):
            notes.append(f"{target.name}: already the original")
            continue
        # Fail closed for interrupted/legacy records without a known installed
        # digest. Never overwrite an independently patched game DLL.
        if not e.get("new_sha256") or cur != e["new_sha256"]:
            notes.append(f"{target}: changed since the update or unverified backup — left alone")
            continue
        if sha256(bfile) != e.get("old_sha256"):
            notes.append(f"{target}: corrupt backup checksum — left alone")
            continue
        if not cur and not target.parent.is_dir():
            notes.append(f"{target}: game folder is gone")
            continue
        try:
            atomic_copy(bfile, target)
            restored += 1
            log(f"  ↺ {target}")
        except OSError as ex:
            notes.append(f"{target}: {ex}")
    return restored, notes


def prune(game_key: str, root: Optional[Path] = None, keep: int = KEEP_BACKUPS):
    """Keep `keep` backups in total: the newest keep-1 plus the oldest one, which holds
    the game's original DLLs and must survive any number of later updates."""
    backups = list_backups(game_key, root)          # newest first
    if len(backups) <= keep:
        return
    for p, _m in backups[keep - 1:-1]:
        shutil.rmtree(p, ignore_errors=True)
