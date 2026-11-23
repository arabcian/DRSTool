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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import file_transactions as tx
from .fsutil import atomic_write

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


def _pe_machine(path: Path) -> int | None:
    """Return validated x86/x64 PE machine type or None for corrupt/unknown PE."""
    try:
        with path.open('rb') as f:
            head = f.read(64)
            if len(head) != 64 or head[:2] != b'MZ':
                return None
            offset = int.from_bytes(head[0x3c:0x40], 'little')
            if not (64 <= offset <= 1024 * 1024):
                return None
            f.seek(offset)
            pe = f.read(24)
            if len(pe) != 24 or pe[:4] != b'PE\0\0':
                return None
            arch = int.from_bytes(pe[4:6], 'little')
            # Real PE executables set COFF characteristics; refuse known
            # non-DLLs. Some older synthetic tests omit these flags entirely.
            characteristics = int.from_bytes(pe[22:24], 'little')
            if characteristics and not characteristics & 0x2000:
                return None
            return arch if arch in (0x14c, 0x8664) else None
    except (OSError, ValueError):
        return None


def _is_pe(path: Path, machine: int | None = None) -> bool:
    arch = _pe_machine(path)
    return arch is not None and (machine is None or arch == machine)


def atomic_copy(src: Path, dst: Path):
    """Atomic, fsync'd copy guarded against symlink substitution."""
    tx.atomic_copy(src, dst)


@dataclass
class ReplaceResult:
    backup_dir: Optional[Path]
    replaced: List[Path]
    skipped: List[Tuple[Path, str]]
    status: str = "committed"  # committed | committed_metadata_error | failed | no_changes
    detail: str = ""


def check_target(target: Path) -> str:
    """'' if target may be replaced, else the reason why not."""
    try:
        tx.assert_safe_path(target)
    except (OSError, ValueError) as ex:
        return f"unsafe or missing target path: {ex}"
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
        if not why:
            try:
                tx.assert_safe_path(src)
            except (OSError, ValueError) as ex:
                why = f"unsafe replacement path: {ex}"
        if not why:
            dst_arch, src_arch = _pe_machine(target), _pe_machine(src)
            if src_arch is None:
                why = f"replacement {src.name} is not a valid x86/x64 Windows DLL"
            elif dst_arch is None:
                why = f"target {target.name} has an unknown/invalid PE architecture"
            elif src_arch != dst_arch:
                why = (f"DLL architecture mismatch: target {dst_arch:#x}, "
                       f"replacement {src_arch:#x}")
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
        return ReplaceResult(None, [], skipped, status="no_changes")

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
    except tx.TransactionCommittedError as ex:
        log(f"  ⚠ DLL replacement committed, final journal metadata failed: {ex}")
        return ReplaceResult(bdir, ex.changed, skipped,
                             status="committed_metadata_error", detail=str(ex))
    except (OSError, ValueError, RuntimeError) as ex:
        # tx.apply implements its own rollback and preserves the journal for
        # recovery. Metadata updates must never be confused with this phase.
        log(f"  ✗ game DLL transaction failed (see journal for recovery): {ex}")
        skipped.extend((target, f"transaction failed: {ex}") for target, _src, _label in work)
        return ReplaceResult(bdir, [], skipped, status="failed", detail=str(ex))

    try:
        manifest = json.loads((bdir / "manifest.json").read_text())
        manifest.update(game_key=game_key, game=game_name, created=stamp)
        _write_json(bdir / "manifest.json", manifest)
    except (OSError, ValueError, RuntimeError) as ex:
        # Commit already succeeded. Return actual installed paths and preserve
        # the transaction journal instead of falsely claiming a rollback.
        log(f"  ⚠ DLLs installed but backup metadata could not be updated: {ex}; "
            f"recoverable journal: {bdir / 'manifest.json'}")
        return ReplaceResult(bdir, replaced, skipped,
                             status="committed_metadata_error", detail=str(ex))
    prune(game_key, root)
    return ReplaceResult(bdir, replaced, skipped)


def _write_json(path: Path, data: dict):
    atomic_write(path, json.dumps(data, indent=2))


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


def _relocate(target: Path, folders: Sequence[Path]) -> Optional[Path]:
    """The same file under a current game folder, for a game that moved or was renamed
    since the backup. Tries the longest matching tail of the old path first (bin64/x.dll
    before x.dll); the sha256 checks in restore() make a wrong match harmless."""
    parts = target.parent.parts
    for folder in folders:
        for i in range(1, len(parts) + 1):
            cand = Path(folder, *parts[i:], target.name)
            if cand.is_file():
                return cand
    return None


def unlocated(backup_dir: Path, folders: Sequence[Path] = ()) -> List[Path]:
    """Targets of a backup whose folder is gone and not found under `folders` either."""
    m = json.loads((backup_dir / "manifest.json").read_text())
    out = []
    for e in m.get("entries", []):
        try:
            target = Path(e["target"])
        except (KeyError, TypeError):
            continue
        if not target.parent.is_dir() and _relocate(target, folders) is None:
            out.append(target)
    return out


def restore(backup_dir: Path, log: Callable[[str], None] = lambda _m: None,
            folders: Sequence[Path] = ()) -> Tuple[int, List[str]]:
    """Put the originals from one backup back. Returns (restored count, notes).

    `folders`: the game's current folders (from the launcher, or picked by hand), used
    when a recorded folder no longer exists."""
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
        if not target.parent.is_dir():
            moved = _relocate(target, folders)
            if moved is None:
                notes.append(f"{target}: game folder not found (moved or renamed?)")
                continue
            log(f"  ⤷ {target.name}: game folder moved, using {moved.parent}")
            target = moved
        try:
            tx.assert_safe_path(target, allow_missing=True)
        except (OSError, ValueError) as ex:
            notes.append(f"{target}: unsafe path — left alone ({ex})")
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
