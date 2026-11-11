"""Bounded, metadata-cached Lutris game config discovery without Qt.

The caller can run this in a background thread. Files are parsed only when
(path, mtime, ctime, inode, size) changes; deletions are removed from the cache.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import yaml

_LOG = logging.getLogger(__name__)
_LOADER = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)


def scan_game_configs(folder: Path, cached: dict | None = None,
                      cancelled: Callable[[], bool] = lambda: False) -> tuple[list, dict]:
    """Return rows [(path, slug, display_name, runner)] and new cache.

    Malformed YAML is skipped, not fatal; caller can request a full scan by
    passing an empty cache. The scan never follows symlinks to game configs.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f'Folder not found: {folder}')
    cache_in = cached or {}
    cache_out = {}
    rows = []
    paths = sorted(folder.glob('*.yml')) + sorted(folder.glob('*.yaml'))
    for path in paths:
        if cancelled():
            break
        try:
            if path.is_symlink():
                continue
            st = path.stat()
            signature = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
            previous = cache_in.get(str(path))
            if previous is not None and previous[0] == signature:
                row = previous[1]
            else:
                with path.open('r', encoding='utf-8') as file:
                    data = yaml.load(file, Loader=_LOADER) or {}
                if not isinstance(data, dict):
                    continue
                game = data.get('game')
                game = game if isinstance(game, dict) else {}
                exe = str(game.get('exe') or '')
                system = data.get('system')
                runner = str(system.get('runner') or '') if isinstance(system, dict) else ''
                row = (path.stem, Path(exe).stem if exe else path.stem, runner)
            cache_out[str(path)] = (signature, row)
            rows.append((path, *row))
        except Exception:
            _LOG.debug('Lutris YAML scan skipped: %s', path, exc_info=True)
    return rows, cache_out
