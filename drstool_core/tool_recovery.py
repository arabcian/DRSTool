"""Crash recovery for compatibility-tool installations.

Install journals are durable before any visible directory rename. A new scan
recovers interrupted installations and completes committed cleanup; an
unverified/incomplete new folder is quarantined rather than deleted.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Callable

from .file_transactions import atomic_write_json

_JOURNAL_RE = re.compile(r'^\.drstool-tm-install-([0-9a-f]{32})\.json$')
_LEGACY_RE = re.compile(r'^\.drstool-previous-\d+-(.+)$')


def _name(s: str) -> str:
    if (not isinstance(s, str) or not s or s in ('.', '..') or '/' in s or '\\' in s
            or s.startswith('.') or '\0' in s):
        raise ValueError('unsafe compatibility tool folder name in recovery journal')
    return s


def _entry(folder: Path, transaction_id: str, name: str) -> dict:
    name = _name(name)
    target = folder / name
    exists = target.exists() or target.is_symlink()
    original = os.lstat(target) if exists else None
    return {'name': name, 'had_old': exists,
            'old_device': original.st_dev if original is not None else None,
            'old_inode': original.st_ino if original is not None else None,
            'backup': f'.drstool-previous-{transaction_id}-{name}'}


def create_journal(folder: Path, dirs: list[Path], *, tool: str, version: str,
                   main: str) -> Path:
    """Persist an installation plan before any old directory is renamed."""
    transaction_id = uuid.uuid4().hex
    journal = folder / f'.drstool-tm-install-{transaction_id}.json'
    entries = [_entry(folder, transaction_id, p.name) for p in dirs]
    atomic_write_json(journal, {'state': 'installing', 'id': transaction_id,
                                'tool': tool, 'version': version, 'main': _name(main),
                                'entries': entries})
    return journal


def _load(path: Path) -> dict:
    match = _JOURNAL_RE.fullmatch(path.name)
    if not match or path.is_symlink():
        raise ValueError(f'invalid recovery journal: {path}')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('id') != match.group(1):
        raise ValueError(f'corrupt recovery journal: {path}')
    if data.get('state') not in ('installing', 'committed'):
        raise ValueError(f'unknown recovery state: {path}')
    if not isinstance(data.get('entries'), list) or not data['entries']:
        raise ValueError(f'empty recovery journal: {path}')
    _name(data.get('main'))
    seen = set()
    for e in data['entries']:
        if not isinstance(e, dict):
            raise ValueError(f'invalid recovery entry: {path}')
        name = _name(e.get('name'))
        if name in seen or type(e.get('had_old')) is not bool:
            raise ValueError(f'duplicate/invalid recovery entry: {path}')
        seen.add(name)
        if e.get('backup') != f'.drstool-previous-{data["id"]}-{name}':
            raise ValueError(f'unsafe backup reference: {path}')
    if data['main'] not in seen:
        raise ValueError(f'main tool absent from recovery entries: {path}')
    return data


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove(path: Path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _quarantine(folder: Path, transaction_id: str, name: str, log: Callable[[str], None]):
    target = folder / name
    if not _exists(target):
        return
    for number in range(1000):
        extra = f'-{number}' if number else ''
        dest = folder / f'.drstool-incomplete-{transaction_id}-{name}{extra}'
        if not _exists(dest):
            os.replace(target, dest)
            log(f'  Incomplete compatibility tool moved aside: {dest}')
            return
    raise RuntimeError(f'cannot quarantine incomplete tool: {target}')


def _valid_main(folder: Path, data: dict) -> bool:
    main = folder / data['main']
    if not main.is_dir() or main.is_symlink():
        return False
    try:
        info = json.loads((main / 'protondl_version.json').read_text(encoding='utf-8'))
        return (info.get('compat_tool') == data['tool'] and
                info.get('version') == data['version'])
    except (OSError, ValueError, AttributeError):
        return False


def _recover_one(folder: Path, journal: Path, log: Callable[[str], None]):
    data = _load(journal)
    entries = data['entries']
    commit_valid = (data['state'] == 'committed' and _valid_main(folder, data)
                    and all((folder / e['name']).is_dir() and
                            not (folder / e['name']).is_symlink() for e in entries))
    if commit_valid:
        for e in entries:
            backup = folder / e['backup']
            if _exists(backup):
                _remove(backup)
        journal.unlink()
        log(f'  Cleaned up completed tool installation: {data["main"]}')
        return
    # The journal was never committed, or the committed installation became
    # incomplete. Restore any known originals and quarantine new folders.
    for e in reversed(entries):
        target, backup = folder / e['name'], folder / e['backup']
        if _exists(backup):
            _quarantine(folder, data['id'], e['name'], log)
            os.replace(backup, target)
        elif e['had_old']:
            if not _exists(target):
                raise RuntimeError(f'missing original and backup for {target}; journal kept')
            current = os.lstat(target)
            if ((current.st_dev, current.st_ino) !=
                    (e.get('old_device'), e.get('old_inode'))):
                raise RuntimeError(f'original backup missing, installed folder differs: '
                                   f'{target}; journal kept for manual recovery')
            # Old folder was never renamed (or a prior recovery restored it).
        else:
            _quarantine(folder, data['id'], e['name'], log)
    journal.unlink()
    log(f'  Recovered interrupted compatibility-tool installation: {data["main"]}')


def recover(folder: Path, log: Callable[[str], None] = lambda _: None) -> None:
    """Recover after SIGKILL/power loss. Requires a cross-process install lock."""
    if not folder.is_dir() or folder.is_symlink():
        return
    for journal in sorted(folder.glob('.drstool-tm-install-*.json')):
        _recover_one(folder, journal, log)
    # Legacy DRSTool versions did not write a journal. Restore only when the
    # original name is completely absent; never overwrite a healthy install.
    for backup in sorted(folder.glob('.drstool-previous-*')):
        m = _LEGACY_RE.fullmatch(backup.name)
        if not m or backup.is_symlink():
            continue
        try:
            name = _name(m.group(1))
        except ValueError:
            continue
        target = folder / name
        if not _exists(target):
            os.replace(backup, target)
            log(f'  Restored legacy compatibility-tool backup: {target}')
        else:
            log(f'  Old tool backup retained for review (new folder exists): {backup}')
