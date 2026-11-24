"""Where launchers keep their data: Steam roots / libraries / app manifests,
Heroic and Bottles roots. One copy, used by the game scanner, the sync backends,
the cache page, the DLL manager and the Tool Manager."""

import re
from pathlib import Path
from typing import Iterator, List, Optional, Tuple


def _home(home: Optional[Path]) -> Path:
    return Path(home) if home else Path.home()


def vdf_unesc(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def steam_root_candidates(home: Optional[Path] = None) -> List[Tuple[str, Path]]:
    """[(label, unresolved path)] of every place a Steam install may live."""
    h = _home(home)
    return [("Steam", h / ".local/share/Steam"), ("Steam", h / ".steam/steam"),
            ("Steam", h / ".steam/root"), ("Steam", h / ".steam/debian-installation"),
            ("Steam (Flatpak)", h / ".var/app/com.valvesoftware.Steam/.local/share/Steam"),
            ("Steam (Flatpak)", h / ".var/app/com.valvesoftware.Steam/data/Steam"),
            ("Steam (Snap)", h / "snap/steam/common/.steam/root")]


def steam_roots(home: Optional[Path] = None, marker: str = "steamapps") -> List[Path]:
    """Resolved, de-duplicated Steam roots that contain the `marker` directory."""
    seen, out = set(), []
    for _label, c in steam_root_candidates(home):
        try:
            r = c.resolve()
            if (r / marker).is_dir() and r not in seen:
                seen.add(r)
                out.append(r)
        except OSError:
            continue
    return out


def _read_vdf(text: str) -> dict:
    token = re.compile(r'\s+|//[^\n]*|"((?:[^"\\]|\\.)*)"|([{}])')
    stack = [{}]
    key = None
    pos = 0
    text = text.lstrip('\ufeff')
    while pos < len(text):
        m = token.match(text, pos)
        if m is None:
            raise ValueError('invalid VDF token')
        pos = m.end()
        if m.group(1) is not None:
            value = vdf_unesc(m.group(1))
            if key is None:
                key = value.lower()
            else:
                stack[-1][key] = value
                key = None
        elif m.group(2) == '{':
            if key is None or len(stack) >= 64:
                raise ValueError('invalid VDF nesting')
            obj = {}
            stack[-1][key] = obj
            stack.append(obj)
            key = None
        elif m.group(2) == '}':
            if key is not None or len(stack) == 1:
                raise ValueError('unbalanced VDF')
            stack.pop()
    if key is not None or len(stack) != 1:
        raise ValueError('incomplete VDF')
    return stack[0]


def steam_libraries(root: Path) -> List[Path]:
    """<library>/steamapps dirs listed by root's libraryfolders.vdf (root's own first)."""
    libs = [root / "steamapps"]
    try:
        text = (root / "steamapps" / "libraryfolders.vdf").read_text(errors="replace")
        doc = _read_vdf(text)
        for key, entry in doc.get('libraryfolders', {}).items():
            if not key.isdigit():
                continue
            value = entry.get('path') if isinstance(entry, dict) else entry
            if isinstance(value, str) and Path(value).is_absolute():
                p = Path(value) / 'steamapps'
                if p not in libs:
                    libs.append(p)
    except (OSError, ValueError, AttributeError):
        pass
    return [lib for lib in libs if lib.is_dir()]


STEAM_TOOL_NAMES = re.compile(r"^(Proton\b|Steam Linux Runtime|Steamworks Common|SteamVR\b)", re.I)


def steam_apps(lib: Path) -> Iterator[Tuple[str, str, str]]:
    """(appid, name, installdir) per appmanifest in a steamapps dir; installdir may be ''."""
    for acf in sorted(lib.glob("appmanifest_*.acf")):
        try:
            t = acf.read_text(errors="replace")
        except OSError:
            continue
        try:
            app = _read_vdf(t).get('appstate', {})
            a = app.get('appid', '')
            if isinstance(a, str) and a.isdigit():
                n, d = app.get('name', a), app.get('installdir', '')
                if isinstance(n, str) and isinstance(d, str):
                    yield a, n, d
        except (ValueError, AttributeError):
            continue


def heroic_roots(home: Optional[Path] = None) -> List[Path]:
    h = _home(home)
    return [p for p in (h / ".config/heroic", h / ".var/app/com.heroicgameslauncher.hgl/config/heroic")
            if p.is_dir()]


def bottles_roots(home: Optional[Path] = None) -> List[Path]:
    h = _home(home)
    return [p for p in (h / ".local/share/bottles/bottles",
                        h / ".var/app/com.usebottles.bottles/data/bottles/bottles") if p.is_dir()]
