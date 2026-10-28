"""Steam non-Steam-game shortcuts (userdata/<id>/config/shortcuts.vdf).

shortcuts.vdf is *binary* KeyValues:

    0x00 <key>\\0  ...children...  0x08     nested map
    0x01 <key>\\0 <utf-8 text>\\0           string
    0x02 <key>\\0 <int32 LE>                int
    0x03 <key>\\0 <float32 LE>              float
    0x07 <key>\\0 <uint64 LE>               64-bit int

The whole file is one map ("shortcuts") whose children are "0", "1", … and
ends with an extra 0x08. The parser keeps every value's type and the key
order, so a parse → dump round trip reproduces the file byte for byte and
unknown fields written by newer Steam versions survive an edit.

Steam keeps its own copy in memory and rewrites the file when it exits, so
writes must only happen while Steam is closed (the GUI checks that).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

T_MAP, T_STR, T_INT, T_FLOAT, T_PTR, T_WSTR, T_COLOR, T_U64, T_END = 0, 1, 2, 3, 4, 5, 6, 7, 8
T_END_ALT = 11          # some writers end maps with 0x0B

DRSTOOL_TAG = "DRSTool"


class VdfError(ValueError):
    pass


class KV:
    """Ordered, typed key/value map. Values: KV | (type, python value)."""

    __slots__ = ("items",)

    def __init__(self, items: Optional[List[Tuple[str, object]]] = None):
        self.items: List[Tuple[str, object]] = list(items or [])

    # dict-like helpers (case-insensitive lookup, Steam itself is inconsistent:
    # older files say "appname"/"exe", newer ones "AppName"/"Exe")
    def _idx(self, key: str) -> int:
        low = key.lower()
        for i, (k, _v) in enumerate(self.items):
            if k.lower() == low:
                return i
        return -1

    def get(self, key: str, default=None):
        i = self._idx(key)
        if i < 0:
            return default
        v = self.items[i][1]
        return v if isinstance(v, KV) else v[1]

    def set(self, key: str, typ: int, value):
        i = self._idx(key)
        entry = value if typ == T_MAP else (typ, value)
        if i < 0:
            self.items.append((key, entry))
        else:
            self.items[i] = (self.items[i][0], entry)

    def remove(self, key: str) -> bool:
        i = self._idx(key)
        if i < 0:
            return False
        del self.items[i]
        return True

    def keys(self) -> List[str]:
        return [k for k, _ in self.items]

    def __eq__(self, other):
        return isinstance(other, KV) and self.items == other.items

    def __repr__(self):
        return f"KV({self.items!r})"


# ── parse / dump ────────────────────────────────────────────────────────────

def _cstr(data: bytes, pos: int) -> Tuple[str, int]:
    end = data.find(b"\x00", pos)
    if end < 0:
        raise VdfError("unterminated string")
    return data[pos:end].decode("utf-8", "surrogateescape"), end + 1


def _parse_map(data: bytes, pos: int, depth: int) -> Tuple[KV, int]:
    if depth > 32:
        raise VdfError("nesting too deep")
    kv = KV()
    n = len(data)
    while True:
        if pos >= n:
            raise VdfError("unexpected end of file")
        t = data[pos]
        pos += 1
        if t in (T_END, T_END_ALT):
            return kv, pos
        key, pos = _cstr(data, pos)
        if t == T_MAP:
            child, pos = _parse_map(data, pos, depth + 1)
            kv.items.append((key, child))
        elif t in (T_STR, T_WSTR):
            if t == T_WSTR:
                end = pos
                while end + 1 < n and data[end:end + 2] != b"\x00\x00":
                    end += 2
                if end + 1 >= n:
                    raise VdfError("unterminated wide string")
                kv.items.append((key, (T_WSTR, data[pos:end].decode("utf-16-le", "surrogatepass"))))
                pos = end + 2
            else:
                val, pos = _cstr(data, pos)
                kv.items.append((key, (T_STR, val)))
        elif t in (T_INT, T_PTR, T_COLOR):
            if pos + 4 > n:
                raise VdfError("truncated int")
            kv.items.append((key, (t, struct.unpack_from("<i", data, pos)[0])))
            pos += 4
        elif t == T_FLOAT:
            if pos + 4 > n:
                raise VdfError("truncated float")
            kv.items.append((key, (t, struct.unpack_from("<f", data, pos)[0])))
            pos += 4
        elif t == T_U64:
            if pos + 8 > n:
                raise VdfError("truncated uint64")
            kv.items.append((key, (t, struct.unpack_from("<Q", data, pos)[0])))
            pos += 8
        else:
            raise VdfError(f"unknown value type 0x{t:02x} at offset {pos - 1}")


def parse(data: bytes) -> KV:
    """Whole binary VDF file -> root KV (holding e.g. the "shortcuts" map)."""
    if not data:
        return KV()
    root, pos = _parse_map(data, 0, 0)
    # The top level is terminated by its own trailing 0x08 (already consumed).
    if data[pos:].strip(b"\x00\x08"):
        raise VdfError("trailing data after the root map")
    return root


def _dump_map(kv: KV, out: bytearray):
    for key, val in kv.items:
        kb = key.encode("utf-8", "surrogateescape") + b"\x00"
        if isinstance(val, KV):
            out += bytes([T_MAP]) + kb
            _dump_map(val, out)
            out.append(T_END)
            continue
        t, v = val
        out += bytes([t]) + kb
        if t == T_STR:
            out += str(v).encode("utf-8", "surrogateescape") + b"\x00"
        elif t == T_WSTR:
            out += str(v).encode("utf-16-le", "surrogatepass") + b"\x00\x00"
        elif t in (T_INT, T_PTR, T_COLOR):
            out += struct.pack("<i", int(v))
        elif t == T_FLOAT:
            out += struct.pack("<f", float(v))
        elif t == T_U64:
            out += struct.pack("<Q", int(v))
        else:
            raise VdfError(f"cannot write value type {t}")


def dump(root: KV) -> bytes:
    out = bytearray()
    _dump_map(root, out)
    out.append(T_END)
    return bytes(out)


# ── shortcut ids ────────────────────────────────────────────────────────────

def quote_path(p: str) -> str:
    p = p.strip()
    return p if (len(p) >= 2 and p.startswith('"') and p.endswith('"')) else f'"{p}"'


def unquote_path(p: str) -> str:
    p = (p or "").strip()
    return p[1:-1] if len(p) >= 2 and p.startswith('"') and p.endswith('"') else p


def shortcut_appid(exe_quoted: str, name: str) -> int:
    """Steam's id for a non-Steam game (unsigned 32-bit): crc32(exe + name) | 0x80000000.

    This is the number used for grid artwork file names and as the key of the
    per-game Proton mapping (CompatToolMapping) in config.vdf."""
    return (zlib.crc32((exe_quoted + name).encode("utf-8")) & 0xFFFFFFFF) | 0x80000000


def to_signed32(u: int) -> int:
    return u - (1 << 32) if u & 0x80000000 else u


def to_unsigned32(s: int) -> int:
    return s & 0xFFFFFFFF


def rungameid(appid_u32: int) -> int:
    """The 64-bit id behind steam://rungameid/<id> for a shortcut."""
    return (appid_u32 << 32) | 0x02000000


# ── shortcuts.vdf model ─────────────────────────────────────────────────────

@dataclass
class Shortcut:
    index: str                 # key inside the "shortcuts" map ("0", "1", …)
    appid: int                 # unsigned 32-bit
    name: str
    exe: str                   # unquoted
    start_dir: str             # unquoted
    launch_options: str
    icon: str
    tags: List[str]
    hidden: bool

    @property
    def is_drstool(self) -> bool:
        return DRSTOOL_TAG in self.tags


def shortcuts_path(userdata_account_dir: Path) -> Path:
    return userdata_account_dir / "config" / "shortcuts.vdf"


def load(path: Path) -> KV:
    """Root KV of a shortcuts.vdf; an empty skeleton if the file does not exist yet."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return KV([("shortcuts", KV())])
    root = parse(data)
    if not isinstance(root.get("shortcuts"), KV):
        if root.items:
            raise VdfError('no "shortcuts" map in this file')
        root = KV([("shortcuts", KV())])
    return root


def _entries(root: KV) -> KV:
    sc = root.get("shortcuts")
    if not isinstance(sc, KV):
        raise VdfError('no "shortcuts" map in this file')
    return sc


def _tags(entry: KV) -> List[str]:
    t = entry.get("tags")
    if not isinstance(t, KV):
        return []
    return [str(v[1]) for _k, v in t.items if not isinstance(v, KV)]


def list_shortcuts(root: KV) -> List[Shortcut]:
    out = []
    for idx, entry in _entries(root).items:
        if not isinstance(entry, KV):
            continue
        name = str(entry.get("AppName", "") or "")
        exe_q = str(entry.get("Exe", "") or "")
        aid = entry.get("appid")
        appid = to_unsigned32(aid) if isinstance(aid, int) else shortcut_appid(exe_q, name)
        out.append(Shortcut(idx, appid, name, unquote_path(exe_q),
                            unquote_path(str(entry.get("StartDir", "") or "")),
                            str(entry.get("LaunchOptions", "") or ""),
                            str(entry.get("icon", "") or ""), _tags(entry),
                            bool(entry.get("IsHidden", 0))))
    return out


def _new_entry(appid_u32: int, name: str, exe_q: str, start_q: str, launch: str, icon: str,
               tags: List[str]) -> KV:
    tag_kv = KV([(str(i), (T_STR, t)) for i, t in enumerate(tags)])
    return KV([
        ("appid", (T_INT, to_signed32(appid_u32))),
        ("AppName", (T_STR, name)),
        ("Exe", (T_STR, exe_q)),
        ("StartDir", (T_STR, start_q)),
        ("icon", (T_STR, icon)),
        ("ShortcutPath", (T_STR, "")),
        ("LaunchOptions", (T_STR, launch)),
        ("IsHidden", (T_INT, 0)),
        ("AllowDesktopConfig", (T_INT, 1)),
        ("AllowOverlay", (T_INT, 1)),
        ("OpenVR", (T_INT, 0)),
        ("Devkit", (T_INT, 0)),
        ("DevkitGameID", (T_STR, "")),
        ("DevkitOverrideAppID", (T_INT, 0)),
        ("LastPlayTime", (T_INT, 0)),
        ("FlatpakAppID", (T_STR, "")),
        ("tags", tag_kv),
    ])


def upsert_shortcut(root: KV, name: str, exe: str, start_dir: str = "", launch_options: str = "",
                    icon: str = "", tag_drstool: bool = True) -> Tuple[int, bool]:
    """Add a shortcut, or update the one with the same (exe, name).

    Returns (appid_u32, created). An existing entry keeps every field this
    function does not manage (play time, hidden flag, Steam's own extras)."""
    name = name.strip()
    exe = unquote_path(exe)
    if not name:
        raise ValueError("the shortcut needs a name")
    if not exe:
        raise ValueError("the shortcut needs a program to run")
    exe_q = quote_path(exe)
    start = unquote_path(start_dir) or str(Path(exe).parent)
    start_q = quote_path(start)
    appid = shortcut_appid(exe_q, name)
    sc = _entries(root)
    for _idx, entry in sc.items:
        if not isinstance(entry, KV):
            continue
        same = (str(entry.get("AppName", "")) == name
                and unquote_path(str(entry.get("Exe", ""))) == exe)
        if not same:
            continue
        entry.set("appid", T_INT, to_signed32(appid))
        entry.set("StartDir", T_STR, start_q)
        entry.set("LaunchOptions", T_STR, launch_options)
        if icon:
            entry.set("icon", T_STR, icon)
        if tag_drstool:
            tags = entry.get("tags")
            if not isinstance(tags, KV):
                tags = KV()
                entry.set("tags", T_MAP, tags)
            if DRSTOOL_TAG not in _tags(entry):
                tags.items.append((str(len(tags.items)), (T_STR, DRSTOOL_TAG)))
        return appid, False
    used = {k for k, _ in sc.items}
    idx = 0
    while str(idx) in used:
        idx += 1
    sc.items.append((str(idx), _new_entry(appid, name, exe_q, start_q, launch_options, icon,
                                          [DRSTOOL_TAG] if tag_drstool else [])))
    return appid, True


def remove_shortcut(root: KV, appid_u32: int) -> bool:
    """Remove a shortcut and renumber the rest ("0".."n-1"), as Steam does."""
    sc = _entries(root)
    keep, removed = [], False
    for k, entry in sc.items:
        if isinstance(entry, KV) and not removed:
            aid = entry.get("appid")
            cur = to_unsigned32(aid) if isinstance(aid, int) else \
                shortcut_appid(str(entry.get("Exe", "")), str(entry.get("AppName", "")))
            if cur == appid_u32:
                removed = True
                continue
        keep.append(entry)
    if removed:
        sc.items = [(str(i), e) for i, e in enumerate(keep)]
    return removed


def find_accounts(steam_root: Path) -> List[Path]:
    """userdata/<id> directories of real accounts (id 0 is the anonymous one)."""
    ud = steam_root / "userdata"
    try:
        return sorted(p for p in ud.iterdir() if p.is_dir() and p.name.isdigit() and p.name != "0")
    except OSError:
        return []
