"""Validated, restartable downloads with strong partial-response checks.

A partial file is resumed only if the origin advertised an ETag or Last-Modified
validator on the earlier response. Unvalidated legacy partial files are replaced.
"""
from __future__ import annotations

import errno
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from .file_transactions import atomic_write_json

_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


class DownloadCancelled(Exception):
    """The caller requested cancellation; keep partial data for safe resume."""


def download(url: str, dest: Path, hint: int, cancelled, progress, *, user_agent: str,
             chunk_size: int = 1 << 20) -> None:
    """Download *url*, checking the requested offset and exact response size.

    Sidecar metadata survives cancellation so that If-Range protects a resumed
    download from mixing bytes from different server object versions.
    """
    dest = Path(dest)
    sidecar = dest.with_name(dest.name + '.http.json')
    have = dest.stat().st_size if dest.is_file() else 0
    try:
        meta = json.loads(sidecar.read_text(encoding='utf-8')) if have else {}
        if not isinstance(meta, dict):
            meta = {}
    except (OSError, ValueError, TypeError):
        meta = {}
    validator = meta.get('etag') or meta.get('last_modified')
    if meta.get('url') != url or not isinstance(validator, str) or not validator:
        have = 0
    for attempt in range(2):
        if cancelled.is_set():
            raise DownloadCancelled()
        headers = {'User-Agent': user_agent, 'Accept-Encoding': 'identity'}
        if have:
            headers['Range'] = f'bytes={have}-'
            headers['If-Range'] = validator
        req = urllib.request.Request(url, headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as exc:
            if have and exc.code == 416 and attempt == 0:
                have = 0
                sidecar.unlink(missing_ok=True)
                continue
            raise RuntimeError(f'download failed: HTTP {exc.code} — {url}') from None
        with response as r:
            status = getattr(r, 'status', None) or r.getcode()
            size_header = r.headers.get('Content-Length')
            try:
                content_len = int(size_header) if size_header is not None else None
            except ValueError:
                content_len = None
            content_range = r.headers.get('Content-Range', '')
            expected_bytes = content_len
            if status == 206:
                match = _RANGE.fullmatch(content_range.strip())
                valid = False
                if match and have:
                    start, end = int(match[1]), int(match[2])
                    expected_bytes = end - start + 1
                    all_size = None if match[3] == '*' else int(match[3])
                    valid = (start == have and end >= start and
                             (all_size is None or end < all_size) and
                             (content_len is None or content_len == end - start + 1))
                    # The server may change its validator between requests.
                    new_validator = r.headers.get('ETag') or r.headers.get('Last-Modified')
                    if new_validator and new_validator != validator:
                        valid = False
                if not valid:
                    if attempt == 0:
                        have = 0
                        sidecar.unlink(missing_ok=True)
                        continue
                    raise RuntimeError('invalid HTTP Content-Range for resumed download')
            elif status == 200:
                have = 0  # server ignored Range, a complete overwrite is required
            else:
                raise RuntimeError(f'unexpected HTTP status {status}')
            new_meta = {'url': url, 'etag': r.headers.get('ETag') or '',
                        'last_modified': r.headers.get('Last-Modified') or ''}
            atomic_write_json(sidecar, new_meta)
            base = have
            total = ((base + content_len) if content_len is not None else hint)
            received = 0
            try:
                with dest.open('ab' if have else 'wb') as stream:
                    while True:
                        if cancelled.is_set():
                            raise DownloadCancelled()
                        block = r.read(chunk_size)
                        if not block:
                            break
                        stream.write(block)
                        received += len(block)
                        progress('Downloading', base + received, total)
                if expected_bytes is not None and received != expected_bytes:
                    raise RuntimeError(f'download truncated ({received} of {expected_bytes} bytes)')
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    raise RuntimeError('no space left on device while downloading') from None
                raise
            sidecar.unlink(missing_ok=True)
            return
    raise RuntimeError('could not safely resume download; retry from scratch')
