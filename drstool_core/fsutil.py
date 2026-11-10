"""Filesystem helpers shared by core and GUI (no Qt)."""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Union


def _umask() -> int:
    m = os.umask(0)
    os.umask(m)
    return m


def atomic_write(path: Union[str, Path], data: Union[str, bytes]) -> None:
    """Write via an unpredictable temp file + fsync + rename. Follows a symlinked
    target (os.replace on the link would turn it into a plain file), keeps the
    original file's permission bits and creates the parent directory."""
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".drstool-tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            shutil.copymode(target, tmp)
        else:
            os.chmod(tmp, 0o644 & ~_umask())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:                                   # make the rename itself durable
        dfd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
