"""Version of a Windows PE file (DLL/EXE) without any third-party library.

Every Windows DLL with a version resource contains a VS_FIXEDFILEINFO block
that starts with the signature 0xFEEF04BD followed by the struct version and
then dwFileVersionMS / dwFileVersionLS. That is enough to read DLSS
(nvngx_dlss*.dll, e.g. 310.2.1.0) and Streamline (sl.*.dll) versions.

Only the resource section would be strictly correct, but the signature is
specific enough that the first hit after the PE header is reliable; the
struct version field (0x00010000) is checked as well.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional, Tuple

_SIG = b"\xbd\x04\xef\xfe"
_MAX_READ = 96 * 1024 * 1024           # nvngx_dlss.dll is ~50 MB; sl.*.dll are small


def fixed_file_version(data: bytes) -> Optional[Tuple[int, int, int, int]]:
    if data[:2] != b"MZ":
        return None
    pos = data.find(_SIG)
    while pos >= 0:
        if pos + 16 <= len(data):
            struc_ver, ms, ls = struct.unpack_from("<III", data, pos + 4)
            if struc_ver == 0x00010000:
                return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
        pos = data.find(_SIG, pos + 1)
    return None


def _rsrc_span(f) -> Optional[Tuple[int, int]]:
    """(file offset, size) of the .rsrc section from the PE headers, or None if they look off."""
    head = f.read(4096)
    if head[:2] != b"MZ" or len(head) < 0x40:
        return None
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    if pe + 24 > len(head) or head[pe:pe + 4] != b"PE\0\0":
        return None
    nsec = struct.unpack_from("<H", head, pe + 6)[0]
    sec = pe + 24 + struct.unpack_from("<H", head, pe + 20)[0]
    if sec + 40 * nsec > len(head):
        return None
    for i in range(nsec):
        o = sec + 40 * i
        if head[o:o + 8].rstrip(b"\0") == b".rsrc":
            size, off = struct.unpack_from("<II", head, o + 16)
            return (off, size) if size else None
    return None


def read_version(path: Path) -> Optional[Tuple[int, int, int, int]]:
    """(major, minor, build, revision) or None when the file has no version resource."""
    try:
        size = path.stat().st_size
        if size > _MAX_READ or size < 64:
            return None
        with open(path, "rb") as f:
            span = _rsrc_span(f)
            if span and span[0] + span[1] <= size:
                # the version resource lives in .rsrc: read only that instead of ~50 MB
                f.seek(span[0])
                ver = fixed_file_version(b"MZ" + f.read(span[1]))
                if ver:
                    return ver
            f.seek(0)
            data = f.read()
    except OSError:
        return None
    return fixed_file_version(data)


def version_str(v: Optional[Tuple[int, ...]], parts: int = 3) -> str:
    if not v:
        return ""
    return ".".join(str(x) for x in v[:parts])


def parse_version(text: str) -> Tuple[int, ...]:
    out = []
    for p in (text or "").strip().lstrip("vV").split("."):
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


def compare(a: Tuple[int, ...], b: Tuple[int, ...], parts: int = 3) -> int:
    """-1 / 0 / 1 comparing the first `parts` components (missing ones count as 0)."""
    a2 = tuple(a[:parts]) + (0,) * max(0, parts - len(a))
    b2 = tuple(b[:parts]) + (0,) * max(0, parts - len(b))
    return (a2 > b2) - (a2 < b2)
