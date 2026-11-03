"""Bounded, path-checked ZIP/tar extraction, including tar.zst on Python 3.12.

All extraction occurs inside a caller-owned staging directory. External tar is
never allowed to write entries before their paths and types have been checked.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tarfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

MAX_FILES = 100_000
MAX_TOTAL_BYTES = 12 * 1024**3
MAX_FILE_BYTES = 4 * 1024**3
MAX_RATIO = 1000


def _inside(root: Path, target: Path) -> bool:
    try:
        return target.is_relative_to(root)
    except ValueError:
        return False


def _path(root: Path, name: str) -> Path:
    if not name or '\\' in name or '\x00' in name:
        raise ValueError(f"unsafe archive path: {name!r}")
    p = PurePosixPath(name)
    if not p.parts:
        if name in ('.', './'):
            return root  # many tar programs include an explicit root directory
        raise ValueError(f"unsafe archive path: {name!r}")
    if p.is_absolute() or '..' in p.parts or ':' in p.parts[0]:
        raise ValueError(f"unsafe archive path: {name!r}")
    dest = root.joinpath(*p.parts)
    if not _inside(root, dest.resolve(strict=False)):
        raise ValueError(f"archive path escapes staging directory: {name!r}")
    return dest


def _limits(count: int, total: int, size: int, compressed: int = 0) -> None:
    if (count > MAX_FILES or size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or
            (compressed > 0 and size > 4 * 1024 * 1024 and size > compressed * MAX_RATIO)):
        raise ValueError("archive exceeds safe extraction limits")


@contextmanager
def _tar_reader(archive: Path) -> Iterator[tarfile.TarFile]:
    process = None
    if archive.name.endswith('.zst'):
        exe = shutil.which('zstd') or shutil.which('unzstd')
        if exe is None:
            try:
                with tarfile.open(archive, 'r:zst') as tf:
                    yield tf
                return
            except (tarfile.CompressionError, ValueError) as e:
                raise RuntimeError('tar.zst requires the zstd utility or Python with zstd support') from e
        process = subprocess.Popen([exe, '-dc', '--', str(archive)], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        try:
            assert process.stdout is not None
            with tarfile.open(fileobj=process.stdout, mode='r|') as tf:
                yield tf
            process.stdout.close()
            if process.wait(timeout=20) != 0:
                raise RuntimeError('zstd decompression failed')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    else:
        with tarfile.open(archive, 'r:*') as tf:
            yield tf


def extract_tar(archive: Path, dest: Path, *, cancel=None,
                progress: Callable[[int, int], None] | None = None,
                allow_internal_links: bool = False) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    count = total = 0
    with _tar_reader(archive) as tf:
        for member in tf:
            if cancel is not None and cancel.is_set():
                raise RuntimeError('extraction cancelled')
            count += 1
            total += member.size
            _limits(count, total, member.size)
            if archive.stat().st_size and total > 4 * 1024 * 1024 and \
                    total > archive.stat().st_size * MAX_RATIO:
                raise ValueError('tar archive exceeds compression ratio budget')
            target = _path(root, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                _path(root, member.name)  # verify after parent creation
                if target.is_symlink():
                    raise ValueError(f"archive attempts to replace a symlink: {member.name}")
                source = tf.extractfile(member)
                if source is None:
                    raise ValueError(f"cannot read tar member: {member.name}")
                # 'xb' also disallows collisions with pre-existing files.
                with source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                target.chmod((member.mode & 0o755) | 0o400)
            elif allow_internal_links and (member.issym() or member.islnk()):
                target.parent.mkdir(parents=True, exist_ok=True)
                # Always reject links to absolute paths and traversals out of stage.
                if member.issym():
                    link = Path(member.linkname)
                    if link.is_absolute() or not _inside(root, (target.parent / link).resolve()):
                        raise ValueError(f"unsafe archive symlink: {member.name}")
                    target.symlink_to(member.linkname)
                else:
                    source = _path(root, member.linkname)
                    if not source.is_file() or source.is_symlink():
                        raise ValueError(f"invalid tar hardlink: {member.name}")
                    os.link(source, target)
            else:
                raise ValueError(f"unsupported archive entry type: {member.name}")
            if progress:
                progress(count, max(count, MAX_FILES))


def extract_zip(archive: Path, dest: Path, *, cancel=None,
                progress: Callable[[int, int], None] | None = None) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        total = sum(i.file_size for i in infos)
        _limits(len(infos), total, max((i.file_size for i in infos), default=0), archive.stat().st_size)
        for number, info in enumerate(infos, 1):
            if cancel is not None and cancel.is_set():
                raise RuntimeError('extraction cancelled')
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f"zip symlink rejected: {info.filename}")
            target = _path(root, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                _path(root, info.filename)
                with zf.open(info) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                perm = (info.external_attr >> 16) & 0o777
                target.chmod((perm & 0o755) | 0o400)
            if progress:
                progress(number, len(infos))
