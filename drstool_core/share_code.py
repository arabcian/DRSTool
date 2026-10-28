"""Profile share codes: one line of text that carries a whole DRSTool profile.

    DRSTOOL1:<base64url( zlib( json ) )>

The JSON is {"v": 1, "name": str, "profile": {...}} where "profile" has the
same shape as an entry of profiles.json (settings / arch / env_vars /
gamescope_enabled / gamescope_flags).

The code survives being pasted into a forum post or chat: decoding finds the
first "DRSTOOL1:" in any surrounding text and ignores whitespace and line
breaks inside the code.

Decoding never trusts the input: the size is bounded before and after
decompression, every field is type-checked, and variables that can change
which code a game loads (LD_PRELOAD, Vulkan layer paths, …) are reported as
"flagged" so the importer can leave them out unless the user opts in.
"""

from __future__ import annotations

import base64
import json
import re
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

PREFIX = "DRSTOOL1:"
VERSION = 1

MAX_CODE_CHARS = 64 * 1024          # a real profile is a few hundred characters
MAX_JSON_BYTES = 512 * 1024         # decompression bomb guard
MAX_KEY = 256
MAX_VALUE = 8192

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CODE_RE = re.compile(re.escape(PREFIX) + r"([A-Za-z0-9_\-\s=]+)")

# Variables that decide which libraries / layers / programs get loaded. A shared
# profile can legitimately carry them, but the receiver should look first.
SENSITIVE_ENV = {
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "PATH", "PYTHONPATH", "PYTHONHOME",
    "VK_ADD_LAYER_PATH", "VK_LAYER_PATH", "VK_ADD_IMPLICIT_LAYER_PATH", "VK_IMPLICIT_LAYER_PATH",
    "VK_ICD_FILENAMES", "VK_DRIVER_FILES", "VK_ADD_DRIVER_FILES",
    "WINEDLLPATH", "WINELOADER", "WINESERVER", "WINEPREFIX",
    "STEAM_COMPAT_DATA_PATH", "STEAM_COMPAT_CLIENT_INSTALL_PATH", "PROTONPATH",
    "DXVK_CONFIG_FILE", "VKD3D_SHADER_CACHE_PATH", "DXVK_STATE_CACHE_PATH",
    "LIBGL_DRIVERS_PATH", "__EGL_VENDOR_LIBRARY_FILENAMES", "__GLX_VENDOR_LIBRARY_NAME",
    "GST_PLUGIN_PATH", "GST_PLUGIN_SYSTEM_PATH", "QT_PLUGIN_PATH",
    "BASH_ENV", "ENV", "SHELLOPTS", "IFS",                  # change how a shell wrapper / launch script runs
}
_SHELL_META_RE = re.compile(r"[`$;|&<>]|\$\(")


class ShareCodeError(ValueError):
    """The text holds no usable share code. The message is user-facing."""


@dataclass
class DecodedShare:
    name: str
    profile: dict
    flagged: Dict[str, str] = field(default_factory=dict)   # env var -> reason
    dropped: List[str] = field(default_factory=list)        # entries removed while sanitising

    def profile_without_flagged(self) -> dict:
        p = json.loads(json.dumps(self.profile))
        for k in self.flagged:
            p.get("env_vars", {}).pop(k, None)
        return p


# ── encoding ────────────────────────────────────────────────────────────────

def _redact(value: str, home: Optional[str]) -> str:
    if home and len(home) > 1:
        value = value.replace(home.rstrip("/") + "/", "~/")
        if value == home.rstrip("/"):
            value = "~"
    return value


def encode(name: str, profile: dict, home: Optional[str] = None) -> str:
    """Profile -> share code. With `home`, that path prefix is written as "~"
    in env values so a share does not leak the sender's user name."""
    prof = sanitize_profile(profile)[0]
    if home:
        prof["env_vars"] = {k: _redact(v, home) for k, v in prof["env_vars"].items()}
        prof["gamescope_flags"] = {k: _redact(v, home) for k, v in prof["gamescope_flags"].items()}
    doc = {"v": VERSION, "name": str(name)[:MAX_KEY], "profile": prof}
    raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    packed = zlib.compress(raw, 9)
    return PREFIX + base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")


def wrap(code: str, width: int = 76) -> str:
    """Hard-wrap a long code for e-mail / forums; decode() accepts it unchanged."""
    return "\n".join(code[i:i + width] for i in range(0, len(code), width))


# ── decoding ────────────────────────────────────────────────────────────────

def _candidates(text: str) -> List[str]:
    """Possible codes after the first prefix, longest first.

    A wrapped code spans several lines, but so does "CODE<newline>Have fun!" —
    the base64 alphabet cannot tell them apart. Every prefix of the whitespace-
    separated chunks is a candidate; zlib's checksum picks the right one."""
    if not isinstance(text, str):
        return []
    m = _CODE_RE.search(text[: MAX_CODE_CHARS * 2])
    if not m:
        return []
    chunks = [c for c in m.group(1).split() if c]
    out = []
    for n in range(len(chunks), 0, -1):
        body = "".join(chunks[:n]).rstrip("=")
        if body:
            out.append(PREFIX + body)
    return out


def find_code(text: str) -> Optional[str]:
    """The share code inside arbitrary text (line breaks inside it removed), or None."""
    for cand in _candidates(text):
        try:
            _unpack(cand)
            return cand
        except ShareCodeError:
            continue
    return None


def looks_like_code(text: str) -> bool:
    """Cheap test (no decoding) for "does this text contain a share code"."""
    return isinstance(text, str) and PREFIX in text


def _unpack(code: str) -> dict:
    body = code[len(PREFIX):]
    if len(body) > MAX_CODE_CHARS:
        raise ShareCodeError("the code is too long to be a profile")
    try:
        packed = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError):
        raise ShareCodeError("the code is damaged (not valid base64) — was it copied completely?")
    try:
        d = zlib.decompressobj()
        raw = d.decompress(packed, MAX_JSON_BYTES)
        if d.unconsumed_tail:
            raise ShareCodeError("the code expands to more data than a profile can hold")
        if not d.eof:
            raise ShareCodeError("the code is incomplete — was it copied completely?")
    except zlib.error:
        raise ShareCodeError("the code is damaged — was it copied completely?")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ShareCodeError("the code does not contain valid profile data")
    if not isinstance(doc, dict):
        raise ShareCodeError("the code does not contain valid profile data")
    return doc


def _str_map(obj, key_ok, what: str, dropped: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if obj is None:
        return out
    if not isinstance(obj, dict):
        dropped.append(f"{what}: not a mapping")
        return out
    for k, v in obj.items():
        if not isinstance(k, str) or not k.strip() or len(k) > MAX_KEY or not key_ok(k):
            dropped.append(f"{what}: invalid key {str(k)[:40]!r}")
            continue
        if isinstance(v, bool) or v is None or isinstance(v, (dict, list)):
            dropped.append(f"{what}: {k} has a non-text value")
            continue
        v = str(v)
        if len(v) > MAX_VALUE or "\x00" in v:
            dropped.append(f"{what}: {k} value too long or binary")
            continue
        out[k.strip()] = v
    return out


def sanitize_profile(p) -> tuple:
    """-> (clean profile dict, [dropped entry notes]). Raises ShareCodeError if p is not a profile."""
    if not isinstance(p, dict):
        raise ShareCodeError("the code does not contain a profile")
    dropped: List[str] = []
    settings = _str_map(p.get("settings"), lambda k: "\n" not in k, "DRS", dropped)
    env = _str_map(p.get("env_vars"), lambda k: bool(_ENV_NAME_RE.match(k)), "env", dropped)
    flags = _str_map(p.get("gamescope_flags"), lambda k: (k.startswith("-") or k == "mangohud") and " " not in k,
                     "gamescope", dropped)
    arch = p.get("arch")
    if arch is not None and (not isinstance(arch, str) or len(arch) > 32):
        dropped.append("arch: invalid value")
        arch = None
    clean = {"settings": settings, "arch": arch or None, "env_vars": env,
             "gamescope_enabled": bool(p.get("gamescope_enabled", False)),
             "gamescope_flags": flags}
    if not (settings or env or flags or clean["arch"] or clean["gamescope_enabled"]):
        raise ShareCodeError("the shared profile is empty")
    return clean, dropped


def flag_env(env: Dict[str, str]) -> Dict[str, str]:
    """{name: reason} for variables the receiver should review before using."""
    out = {}
    for k, v in env.items():
        if k.upper() in SENSITIVE_ENV:
            out[k] = "changes which libraries / layers / programs are loaded"
        elif _SHELL_META_RE.search(v):
            out[k] = "value contains shell syntax"
    return out


_TILDE_RE = re.compile(r"(^|[:=, ;])~(?=/|$|[:, ;])")


def _expand(value: str, home: Optional[str]) -> str:
    """Undo encode()'s home redaction for the receiver ("~/x" -> "<their home>/x").
    A launch line quotes every value, so a literal "~" would never be expanded later."""
    if not home:
        return value
    return _TILDE_RE.sub(lambda m: m.group(1) + home.rstrip("/"), value)


def decode(text: str, home: Optional[str] = None) -> DecodedShare:
    """Text containing a share code -> DecodedShare. With `home`, "~" in values
    is replaced by that folder (pass the receiver's home)."""
    cands = _candidates(text)
    if not cands:
        raise ShareCodeError(f"no share code found (it starts with {PREFIX})")
    doc, first_err = None, None
    for cand in cands:
        try:
            doc = _unpack(cand)
            break
        except ShareCodeError as ex:
            first_err = first_err or ex
    if doc is None:
        raise first_err
    ver = doc.get("v")
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
        raise ShareCodeError("unknown share code format")
    if ver > VERSION:
        raise ShareCodeError(f"this code was made by a newer DRSTool (format {ver}); update DRSTool")
    profile, dropped = sanitize_profile(doc.get("profile"))
    if home:
        profile["env_vars"] = {k: _expand(v, home) for k, v in profile["env_vars"].items()}
        profile["gamescope_flags"] = {k: _expand(v, home) for k, v in profile["gamescope_flags"].items()}
    name = doc.get("name")
    name = name.strip()[:MAX_KEY] if isinstance(name, str) and name.strip() else "Shared profile"
    name = re.sub(r"[\x00-\x1f]", " ", name)
    return DecodedShare(name, profile, flag_env(profile["env_vars"]), dropped)


def summary_lines(share: DecodedShare) -> List[str]:
    p = share.profile
    lines = [f"Name: {share.name}",
             f"DRS settings: {len(p['settings'])}",
             f"Environment variables: {len(p['env_vars'])}",
             f"GPU arch spoof: {p['arch'] or '—'}",
             f"Gamescope: {'on' if p['gamescope_enabled'] else 'off'}"
             + (f" ({len(p['gamescope_flags'])} flag(s))" if p["gamescope_flags"] else "")]
    return lines
