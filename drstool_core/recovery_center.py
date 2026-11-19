"""F08: unified, read-only recovery inventory for existing DRSTool mechanisms.

Restores remain in the existing, conflict-checked History and DLL Manager UIs;
this module never copies, deletes, unpacks, or mutates a user file.
"""
from __future__ import annotations

import json
import os
import glob
import stat
from datetime import datetime, timezone
from pathlib import Path


def _file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _add(records: list, kind: str, path: Path, *, state: str = 'available',
         target: str = '', game_key: str = '') -> None:
    try:
        info = path.lstat()
    except OSError:
        return
    if stat.S_ISLNK(info.st_mode):
        return
    records.append({'kind': kind, 'path': str(path), 'name': path.name, 'state': state,
                    'modified_utc': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                    'bytes': info.st_size if stat.S_ISREG(info.st_mode) else None,
                    'target': target, 'game_key': game_key})


def _direct_children(root: Path, pattern: str, *, limit: int = 80) -> list[Path]:
    if not _directory(root):
        return []
    children = []
    try:
        for path in root.glob(pattern):
            try:
                info = path.lstat()
            except OSError:
                # One disappearing backup must not hide all other backups.
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            children.append((info.st_mtime, path))
    except OSError:
        pass
    children.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _, path in children[:max(0, limit)]]


def _manifest(path: Path, *, limit: int = 262144) -> dict:
    """Read only a small regular manifest; never follow an attacker-controlled link."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('manifest is not a regular file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('oversized manifest')
        result = json.loads(raw.decode('utf-8'))
        if not isinstance(result, dict):
            raise ValueError('manifest root is not an object')
        return result
    finally:
        os.close(fd)


def inventory(*, home: Path | None = None, env: dict | None = None, game_key: str = '',
              prefix: Path | None = None, max_entries: int = 180) -> dict:
    """Inventory known managed roots without a filesystem-wide traversal."""
    home = Path(home) if home is not None else Path.home()
    env = os.environ if env is None else env
    conf = Path(env.get('XDG_CONFIG_HOME') or home / '.config')
    data = Path(env.get('XDG_DATA_HOME') or home / '.local/share')
    records = []
    if not game_key:
        for p in _direct_children(conf / 'drstool' / 'history', 'profiles-*.json'):
            if _file(p):
                _add(records, 'profile-snapshot', p, target='Profile History')
        for p in _direct_children(data / 'drstool' / 'prefix-backups', '*.tar.gz'):
            if _file(p):
                _add(records, 'prefix-archive', p, state='manual-restore-only', target='Prefix tools')
    # Include launcher config siblings created by the existing History mechanism.
    # Read only paths explicitly recorded in DRSTool's own game links database;
    # do not sweep Steam/Lutris/Heroic libraries for guessed backups.
    from .game_links import LinkStore
    links = LinkStore(conf / 'drstool' / 'game_links.json')
    selected = [(game_key, links.get(game_key))] if game_key else list(links.all().items())[:150]
    for key, meta in selected:
        target = meta.get('target') if isinstance(meta, dict) else None
        if not isinstance(target, dict) or not isinstance(target.get('path'), str):
            continue
        p = Path(target['path'])
        if not p.is_absolute() or not _directory(p.parent):
            continue
        for backup in _direct_children(p.parent, glob.escape(p.name) + '.*', limit=50):
            if backup.name.endswith('.bak') and _file(backup):
                _add(records, 'launcher-config', backup, target='Profile History', game_key=key)
    dll_root = data / 'drstool' / 'dll-backups'
    # Game ID/path is never interpolated into filesystem paths; read manifest paths only.
    roots = _direct_children(dll_root, '*', limit=250)
    for root in roots:
        if not _directory(root):
            continue
        for backup in _direct_children(root, '*', limit=45):
            manifest = backup / 'manifest.json'
            if not _directory(backup) or not _file(manifest):
                continue
            try:
                m = _manifest(manifest)
            except (OSError, ValueError, UnicodeError):
                _add(records, 'game-dll', backup, state='unreadable', target='DLL Manager')
                continue
            linked = str(m.get('game_key') or '')
            # Existing manifest versions often have a game key only in the slug.
            if game_key and linked != game_key:
                from .dll_ops import _slug
                if root.name != _slug(game_key):
                    continue
            state = str(m.get('state') or 'unknown')[:40]
            _add(records, 'game-dll', backup, state=state, target='DLL Manager', game_key=linked)
    if prefix is not None and _directory(Path(prefix)):
        for backup in _direct_children(Path(prefix) / '.drstool-backup', '*'):
            if _directory(backup):
                journals = _direct_children(backup, 'prefix-*.json', limit=20)
                if journals:
                    _add(records, 'prefix-dll', backup, target='DLL Manager', game_key=game_key)
        for aside in _direct_children(Path(prefix).parent, f'{glob.escape(Path(prefix).name)}.drstool-reset-*'):
            if _directory(aside):
                _add(records, 'prefix-reset-aside', aside, state='manual-restore-only', target='Prefix tools', game_key=game_key)
    if not game_key:
        for tools_dir in (data / 'Steam' / 'compatibilitytools.d',
                          home / '.steam' / 'root' / 'compatibilitytools.d'):
            for journal in _direct_children(tools_dir, '.drstool-tm-install-*.json'):
                if _file(journal):
                    _add(records, 'tool-install-journal', journal, state='review-needed', target='Tool Manager')
            for backup in _direct_children(tools_dir, '.drstool-incomplete-*'):
                if _directory(backup):
                    _add(records, 'tool-quarantined', backup, state='manual-review', target='Tool Manager')
    records.sort(key=lambda e: e['modified_utc'], reverse=True)
    records = records[:max(1, min(int(max_entries), 500))]
    kinds = {}
    for record in records:
        kinds[record['kind']] = kinds.get(record['kind'], 0) + 1
    return {'entries': records, 'counts': kinds, 'total': len(records),
            'automatic_restore': False,
            'note': 'Inventory only. Existing verified recovery workflows must confirm every restore.'}
