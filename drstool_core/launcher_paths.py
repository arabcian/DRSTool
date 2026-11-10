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


def steam_libraries(root: Path) -> List[Path]:
    """<library>/steamapps dirs listed by root's libraryfolders.vdf (root's own first)."""
    libs = [root / "steamapps"]
    try:
        text = (root / "steamapps" / "libraryfolders.vdf").read_text(errors="replace")
        for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
            p = Path(vdf_unesc(m.group(1))) / "steamapps"
            if p not in libs:
                libs.append(p)
    except OSError:
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
        a = re.search(r'"appid"\s+"(\d+)"', t)
        if not a:
            continue
        n = re.search(r'"name"\s+"((?:[^"\\]|\\.)*)"', t)
        d = re.search(r'"installdir"\s+"((?:[^"\\]|\\.)*)"', t)
        yield (a.group(1), vdf_unesc(n.group(1)) if n else a.group(1),
               vdf_unesc(d.group(1)) if d else "")


def heroic_roots(home: Optional[Path] = None) -> List[Path]:
    h = _home(home)
    return [p for p in (h / ".config/heroic", h / ".var/app/com.heroicgameslauncher.hgl/config/heroic")
            if p.is_dir()]


def bottles_roots(home: Optional[Path] = None) -> List[Path]:
    h = _home(home)
    return [p for p in (h / ".local/share/bottles/bottles",
                        h / ".var/app/com.usebottles.bottles/data/bottles/bottles") if p.is_dir()]
