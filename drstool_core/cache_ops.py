"""Cancellation-aware, symlink-safe cache cleanup (no Qt dependency)."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable, Iterable


def _remove_tree(path: Path, cancelled: Callable[[], bool], on_file: Callable[[], None],
                 errors: list[str]) -> None:
    """Unlink leaf files and directories without following directory symlinks."""
    stack = [(path, False)]
    while stack:
        if cancelled():
            return
        current, ready = stack.pop()
        try:
            st = current.lstat()
            if not stat.S_ISDIR(st.st_mode):
                current.unlink()
                on_file()
                continue
            if ready:
                current.rmdir()
                continue
            with os.scandir(current) as entries:
                children = [Path(e.path) for e in entries]
            stack.append((current, True))
            stack.extend((item, False) for item in children)
        except FileNotFoundError:
            continue  # Entry was concurrently removed by the game or another cleaner.
        except OSError as exc:
            errors.append(f'{current}: {exc}')


def clear_cache_nodes(nodes: Iterable[dict], allowed: Callable[[str], bool],
                      cancelled: Callable[[], bool], progress: Callable[[int, str], None],
                      cache_name_check: Callable[[str], bool]) -> dict:
    """Delete only validated paths, retaining each cache root directory.

    Per-entry errors are surfaced, not silently discarded; progress counts
    successfully unlinked files. A cancellation keeps the remainder intact.
    """
    deleted = kept = refused = 0
    errors: list[str] = []

    def advance():
        nonlocal deleted
        deleted += 1
        progress(deleted, str(path))

    for node in nodes:
        if cancelled():
            break
        path = Path(node['path'])
        if (not allowed(str(path)) or path.is_symlink() or
                not path.is_absolute()):
            refused += 1
            continue
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f'{path}: {exc}')
            continue
        if stat.S_ISDIR(mode):
            try:
                with os.scandir(path) as it:
                    children = [Path(entry.path) for entry in it]
            except OSError as exc:
                errors.append(f'{path}: {exc}')
                continue
            for child in children:
                if cancelled():
                    break
                if node.get('user_path') and not cache_name_check(child.name):
                    kept += 1
                    continue
                _remove_tree(child, cancelled, advance, errors)
        else:
            if node.get('user_path') and not cache_name_check(path.name):
                kept += 1
                continue
            _remove_tree(path, cancelled, advance, errors)
    return {'deleted': deleted, 'kept': kept, 'refused': refused,
            'errors': errors, 'cancelled': bool(cancelled())}
