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
import stat
import uuid
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


def _safe_parent(path: Path):
    """Open every directory component without following directory symlinks.

    The returned fd pins the parent directory across the final rename, even
    when another process changes a path component concurrently. The caller
    owns this descriptor. Use lexical, not resolved, components here.
    """
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for part in absolute.parent.parts[1:]:
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def assert_safe_path(path: Path, *, allow_missing: bool = False):
    """Reject symlinks anywhere in the path (including parent directories)."""
    fd = _safe_parent(path)
    try:
        try:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return
            raise
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'not a regular file (or symlink): {path}')
    finally:
        os.close(fd)


def atomic_copy(src: Path, dst: Path):
    """Copy through anchored dirfds and rename without following symlinks.

    Both parent paths are opened component-by-component with O_NOFOLLOW;
    destination lookup and os.replace use that same pinned parent descriptor.
    """
    src = Path(src)
    dst = Path(dst)
    sfd = _safe_parent(src)
    dfd = None
    name = None
    try:
        dfd = _safe_parent(dst)
        source = os.open(src.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=sfd)
        try:
            if not stat.S_ISREG(os.fstat(source).st_mode):
                raise ValueError(f'unsafe source: {src}')
            try:
                old_stat = os.stat(dst.name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                old_stat = None
            if old_stat is not None and not stat.S_ISREG(old_stat.st_mode):
                raise ValueError(f'unsafe destination: {dst}')
            name = f'.drstool-{uuid.uuid4().hex}.tmp'
            temp = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600, dir_fd=dfd)
            try:
                with os.fdopen(os.dup(source), 'rb') as inp, os.fdopen(temp, 'wb') as out:
                    shutil.copyfileobj(inp, out, 1 << 20)
                    os.fchmod(out.fileno(), stat.S_IMODE((old_stat or os.fstat(source)).st_mode))
                    out.flush()
                    os.fsync(out.fileno())
            except BaseException:
                # fdopen owns temp even if the copy fails
                raise
            # Refuse concurrent replacement, including symlink substitution.
            try:
                now = os.stat(dst.name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                now = None
            if ((old_stat is None) != (now is None) or
                    (old_stat is not None and
                     (old_stat.st_dev, old_stat.st_ino, old_stat.st_size,
                      old_stat.st_mtime_ns, old_stat.st_ctime_ns) !=
                     (now.st_dev, now.st_ino, now.st_size,
                      now.st_mtime_ns, now.st_ctime_ns))):
                raise RuntimeError(f'target changed during replacement: {dst}')
            os.replace(name, dst.name, src_dir_fd=dfd, dst_dir_fd=dfd)
            name = None
            os.fsync(dfd)
        finally:
            os.close(source)
    finally:
        if name is not None and dfd is not None:
            try:
                os.unlink(name, dir_fd=dfd)
            except FileNotFoundError:
                pass
        if dfd is not None:
            os.close(dfd)
        os.close(sfd)


class TransactionCommittedError(RuntimeError):
    """Files committed, but recording the final commit state failed."""

    def __init__(self, journal: Path, changed: list[Path], cause: Exception):
        self.journal, self.changed = journal, list(changed)
        super().__init__(f"files changed successfully; final journal update failed: {cause}; "
                         f"journal retained at {journal}")


def safe_unlink(path: Path):
    """Unlink a regular file through an anchored directory, never a symlink."""
    fd = _safe_parent(path)
    try:
        old = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(old.st_mode):
            raise ValueError(f'unsafe unlink target: {path}')
        os.unlink(path.name, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)


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
        assert_safe_path(src)
        assert_safe_path(target, allow_missing=True)
        if not src.is_file():
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
            # The backup must reach the disk before the target is replaced (and
            # fsynced) below, or a power cut could keep the new file and lose the original.
            with bpath.open('rb') as stream:
                os.fsync(stream.fileno())
            _sync_dir(bpath.parent)
    journal['entries'] += new_entries
    journal['state'] = 'prepared'
    atomic_write_json(path, journal)
    changed = []
    try:
        for c, e in zip(change_list, new_entries):
            assert_safe_path(c.target, allow_missing=True)
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
                assert_safe_path(t, allow_missing=True)
                if t.is_file() and sha256(t) == e['new_sha256']:
                    if e['old_sha256']:
                        atomic_copy(backup / 'files' / e['backup'], t)
                    else:
                        safe_unlink(t)
            except (OSError, ValueError):
                log(f'  ⚠ automatic rollback failed: {t}; backup preserved')
        journal['state'] = 'recovery-needed' if any(
            ((t := Path(e['target'])).is_file() and sha256(t) == e['new_sha256']) for e in new_entries
        ) else ('committed' if original_entries else 'rolled-back')
        atomic_write_json(path, journal)
        raise
    journal['state'] = 'committed'
    try:
        atomic_write_json(path, journal)
    except (OSError, ValueError, RuntimeError) as ex:
        # All files have been atomically replaced and individually checked.
        # The earlier prepared/applied journal still supports recovery.
        raise TransactionCommittedError(path, changed, ex) from ex
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
        try:
            assert_safe_path(target, allow_missing=True)
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
                # Destination is validated immediately before unlink.
                safe_unlink(target)
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
