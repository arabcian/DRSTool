"""Cancellation-aware cache cleanup using pinned, no-follow directory descriptors."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable, Iterable
from .file_transactions import _safe_parent

_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _identity(st):
    return st.st_dev, st.st_ino


def _remove_at(parent, name, display, cancelled, on_file, errors, device):
    if cancelled():
        return
    try:
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            os.unlink(name, dir_fd=parent)
            on_file()
            return
        if st.st_dev != device:
            raise OSError('refusing to cross a filesystem boundary')
        fd = os.open(name, _FLAGS, dir_fd=parent)
        try:
            if _identity(os.fstat(fd)) != _identity(st):
                raise OSError('directory changed during cleanup')
            with os.scandir(fd) as entries:
                for entry in entries:
                    if cancelled():
                        return
                    _remove_at(fd, entry.name, display / entry.name, cancelled,
                               on_file, errors, device)
            if not cancelled():
                now = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if _identity(now) != _identity(st):
                    raise OSError('directory replaced during cleanup')
                os.rmdir(name, dir_fd=parent)
        finally:
            os.close(fd)
    except FileNotFoundError:
        pass
    except (OSError, RecursionError) as exc:
        errors.append(f'{display}: {exc}')


def clear_cache_nodes(nodes: Iterable[dict], allowed: Callable[[str], bool],
                      cancelled: Callable[[], bool], progress: Callable[[int, str], None],
                      cache_name_check: Callable[[str], bool]) -> dict:
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
        if not path.is_absolute() or path == Path('/') or '..' in path.parts or not allowed(str(path)):
            refused += 1
            continue
        parent = None
        try:
            parent = _safe_parent(path)
            st = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                refused += 1
                continue
            if stat.S_ISDIR(st.st_mode):
                fd = os.open(path.name, _FLAGS, dir_fd=parent)
                try:
                    if _identity(os.fstat(fd)) != _identity(st):
                        raise OSError('cache root changed during cleanup')
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            if cancelled():
                                break
                            if node.get('user_path') and not cache_name_check(entry.name):
                                kept += 1
                                continue
                            _remove_at(fd, entry.name, path / entry.name, cancelled, advance, errors, st.st_dev)
                finally:
                    os.close(fd)
            elif node.get('user_path') and not cache_name_check(path.name):
                kept += 1
            else:
                _remove_at(parent, path.name, path, cancelled, advance, errors, st.st_dev)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f'{path}: {exc}')
        finally:
            if parent is not None:
                os.close(parent)
    return {'deleted': deleted, 'kept': kept, 'refused': refused,
            'errors': errors, 'cancelled': bool(cancelled())}
