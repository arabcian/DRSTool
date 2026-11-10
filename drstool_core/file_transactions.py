"""Crash-aware, optimistic-concurrency-checked file replacement transactions.

Write-ahead SHA-256 records make a restore safe even when the process dies
between os.replace and updating a manifest. Backup files are never overwritten.
"""
from __future__ import annotations

import json
import fcntl
from contextlib import contextmanager
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .fsutil import atomic_write


def sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with p.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def atomic_write_json(path: Path, obj: object):
    atomic_write(path, json.dumps(obj, indent=2))


def _sync_dir(path: Path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_copy(src: Path, dst: Path):
    if dst.is_symlink() or src.is_symlink():
        raise ValueError(f'symlink file swap rejected: {dst}')
    fd, tmp = tempfile.mkstemp(prefix='.drstool-', suffix='.tmp', dir=dst.parent)
    try:
        with os.fdopen(fd, 'wb') as out, src.open('rb') as inp:
            shutil.copyfileobj(inp, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        shutil.copymode(dst if dst.exists() else src, tmp)
        os.replace(tmp, dst)
        _sync_dir(dst.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@dataclass
class Change:
    target: Path
    source: Path
    backup_name: str
    label: str = ''


def apply(changes: Iterable[Change], backup: Path, *, log: Callable[[str], None] = lambda _m: None,
          journal_name: str = 'transaction.json', existing: bool = False) -> list[Path]:
    """All-or-nothing swap; explicit journal and content-addressed restore.

    On an interrupted process the manifest is sufficient for safe manual
    recovery. Caller chooses a unique backup directory (never reused).
    """
    change_list = list(changes)
    if not change_list:
        return []
    backup.mkdir(parents=True, exist_ok=True)
    path = backup / journal_name
    if path.exists() and not existing:
        raise FileExistsError(path)
    journal = json.loads(path.read_text()) if path.exists() else {'state': 'prepared', 'entries': []}
    if journal['state'] not in ('prepared', 'committed'):
        raise ValueError('existing transaction is not clean')
    original_entries = list(journal['entries'])
    new_entries = []
    seen = {e['target'] for e in original_entries}
    for c in change_list:
        target, src = c.target, c.source
        if str(target) in seen:
            raise ValueError(f'duplicate transaction target: {target}')
        seen.add(str(target))
        if target.is_symlink() or src.is_symlink() or not src.is_file():
            raise ValueError(f'unsafe replacement: {target}')
        if not target.parent.is_dir():
            raise FileNotFoundError(f'missing destination directory: {target.parent}')
        old = sha256(target) if target.is_file() else ''
        bn = c.backup_name
        bpath = backup / 'files' / bn
        if not bpath.resolve().is_relative_to((backup / 'files').resolve()):
            raise ValueError('invalid backup name')
        new_entries.append({'target': str(target), 'backup': bn if old else '',
                            'old_sha256': old, 'new_sha256': sha256(src),
                            'label': c.label, 'state': 'prepared'})
    (backup / 'files').mkdir(exist_ok=True)
    for e in new_entries:
        if e['old_sha256']:
            bpath = backup / 'files' / e['backup']
            bpath.parent.mkdir(parents=True, exist_ok=True)
            if bpath.exists():
                raise FileExistsError(bpath)
            shutil.copy2(e['target'], bpath)
            if sha256(bpath) != e['old_sha256']:
                raise RuntimeError(f'target changed during backup: {e["target"]}')
    journal['entries'] += new_entries
    journal['state'] = 'prepared'
    atomic_write_json(path, journal)
    changed = []
    try:
        for c, e in zip(change_list, new_entries):
            current = sha256(c.target) if c.target.is_file() else ''
            if c.target.is_symlink() or current != e['old_sha256']:
                raise RuntimeError(f'target modified concurrently: {c.target}')
            atomic_copy(c.source, c.target)
            if sha256(c.target) != e['new_sha256']:
                raise RuntimeError(f'written file failed checksum: {c.target}')
            changed.append(c.target)
            e['state'] = 'applied'
            atomic_write_json(path, journal)
            log(f'  ✓ {c.target.name}: {c.label}')
    except BaseException:
        for e in reversed(new_entries):
            t = Path(e['target'])
            try:
                if not t.is_symlink() and t.is_file() and sha256(t) == e['new_sha256']:
                    if e['old_sha256']:
                        atomic_copy(backup / 'files' / e['backup'], t)
                    else:
                        t.unlink()
            except (OSError, ValueError):
                log(f'  ⚠ automatic rollback failed: {t}; backup preserved')
        journal['state'] = 'recovery-needed' if any(
            ((t := Path(e['target'])).is_file() and sha256(t) == e['new_sha256']) for e in new_entries
        ) else ('committed' if original_entries else 'rolled-back')
        atomic_write_json(path, journal)
        raise
    journal['state'] = 'committed'
    atomic_write_json(path, journal)
    return changed


def restore(backup: Path, *, journal_name: str = 'transaction.json',
            log: Callable[[str], None] = lambda _m: None) -> tuple[int, list[str]]:
    path = backup / journal_name
    journal = json.loads(path.read_text(encoding='utf-8'))
    notes = []
    restored = 0
    for e in reversed(journal['entries']):
        target = Path(e['target'])
        original = backup / 'files' / e['backup'] if e.get('backup') else None
        if target.is_symlink():
            notes.append(f'{target}: symlink detected; untouched')
            continue
        try:
            current = sha256(target) if target.is_file() else ''
            if current == e['old_sha256']:
                continue
            if current != e['new_sha256']:
                notes.append(f'{target}: changed externally; untouched')
                continue
            if original:
                if not original.is_file() or sha256(original) != e['old_sha256']:
                    notes.append(f'{target}: missing or corrupt backup')
                    continue
                atomic_copy(original, target)
            elif target.is_file():
                target.unlink()
            restored += 1
            log(f'  ↺ {target}')
        except (OSError, ValueError) as ex:
            notes.append(f'{target}: {ex}')
    if not notes:
        journal['state'] = 'restored'
        atomic_write_json(path, journal)
    return restored, notes


@contextmanager
def file_lock(path: Path):
    """Cross-process advisory exclusive lock for DRSTool's JSON stores."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lockfile = path.with_name(path.name + '.lock')
    with lockfile.open('a+b') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
