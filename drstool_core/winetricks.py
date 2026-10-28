"""winetricks / protontricks command planning for a Wine prefix (Qt-free).

Steam (Proton) prefixes live at steamapps/compatdata/<appid>/pfx and are driven by
protontricks, which needs the app id. Every other prefix (Lutris, Bottles, ~/.wine,
custom folders) is driven by winetricks with WINEPREFIX set. Only the verbs in the catalog
(data/winetricks_dlls.json, every winetricks library) can be requested; the command is always an argv list, never a shell string.
"""

import json
import re
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

_DATA = Path(__file__).resolve().parent / "data" / "winetricks_dlls.json"

# Used only when the catalog file is missing or unreadable.
_FALLBACK = (
    ("vcrun2019", "Visual C++ 2015-2019 runtime"),
    ("vcrun2022", "Visual C++ 2015-2022 runtime"),
    ("d3dcompiler_47", "D3D compiler 47 (shader compiler)"),
    ("d3dx9", "D3DX9 (older games)"),
    ("xact", "XACT audio"),
)


@lru_cache(maxsize=1)
def _load() -> Tuple[List[dict], List[dict]]:
    """-> (categories, verbs) from data/winetricks_dlls.json: every library verb of
    winetricks' "dlls" category, with a description, a category and (for the ones games
    most often ask for) a one-line reason in "pop"."""
    try:
        d = json.loads(_DATA.read_text(encoding="utf-8"))
        verbs = [v for v in d["verbs"] if isinstance(v, dict) and re.fullmatch(r"[a-z0-9_.\-]+", str(v.get("verb", "")))]
        if verbs:
            return list(d.get("categories") or []), verbs
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return ([{"id": "pop", "title": "Common"}],
            [{"verb": v, "desc": label, "cat": "pop", "pop": label} for v, label in _FALLBACK])


def categories() -> List[dict]:
    return [dict(c) for c in _load()[0]]


def catalog() -> List[dict]:
    """Every library verb, sorted by name (copies: callers may change them)."""
    return [dict(v) for v in sorted(_load()[1], key=lambda v: v["verb"])]


def recommended() -> List[dict]:
    """The libraries games ask for most often, in catalog order."""
    return [v for v in catalog() if v.get("pop")]


# (winetricks verb, label shown in the UI) of the common ones; kept for older callers.
COMPONENTS: Tuple[Tuple[str, str], ...] = tuple((v["verb"], v["pop"]) for v in recommended())
_KNOWN = frozenset(v["verb"] for v in _load()[1])

# A game's log line -> the library that fixes it. (regex on a lower-cased line, verb, why)
LOG_HINTS: Tuple[Tuple[str, str, str], ...] = (
    (r"(msvcp|vcruntime|msvcr|concrt)140|ucrtbase", "vcrun2022", "Visual C++ 2015-2022 runtime"),
    (r"msvc[pr]120", "vcrun2013", "Visual C++ 2013 runtime"),
    (r"msvc[pr]110", "vcrun2012", "Visual C++ 2012 runtime"),
    (r"msvc[pr]100", "vcrun2010", "Visual C++ 2010 runtime"),
    (r"msvc[pr]90", "vcrun2008", "Visual C++ 2008 runtime"),
    (r"msvc[pr]80", "vcrun2005", "Visual C++ 2005 runtime"),
    (r"d3dcompiler_47", "d3dcompiler_47", "D3D compiler 47"),
    (r"d3dcompiler_43", "d3dcompiler_43", "D3D compiler 43"),
    (r"d3dx9_\d+", "d3dx9", "D3DX9"),
    (r"d3dx10_\d+", "d3dx10", "D3DX10"),
    (r"d3dx11_\d+", "d3dx11_43", "D3DX11"),
    (r"xactengine|x3daudio|xapofx", "xact", "XACT audio"),
    (r"xaudio2_9", "xaudio29", "XAudio 2.9"),
    (r"physx", "physx", "PhysX"),
)


def suggest_from_text(text: str) -> List[Tuple[str, str]]:
    """Libraries a log excerpt asks for -> [(verb, reason)], each verb once, in log order."""
    seen: Dict[str, str] = {}
    for line in (text or "").lower().splitlines():
        if not any(w in line for w in ("not found", "cannot find", "failed to load", "could not load",
                                       "err:module", "import_dll", "unable to load", "missing")):
            continue
        for pat, verb, why in LOG_HINTS:
            if verb not in seen and re.search(pat, line):
                seen[verb] = why
    return list(seen.items())


def installed_verbs(prefix: Path) -> Set[str]:
    """Verbs winetricks logged as installed in this prefix (<prefix>/winetricks.log)."""
    try:
        text = (Path(prefix) / "winetricks.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {ln.strip() for ln in text.splitlines() if re.fullmatch(r"[a-z0-9_.\-]+", ln.strip())}


@dataclass(frozen=True)
class Plan:
    argv: List[str]
    env: Dict[str, str]        # extra environment, merged over os.environ
    tool: str                  # "protontricks" or "winetricks"


def steam_appid(prefix: Path) -> Optional[str]:
    """steamapps/compatdata/<appid>/pfx -> '<appid>', anything else -> None."""
    prefix = Path(prefix)
    if prefix.name == "pfx" and prefix.parent.name.isdigit():
        return prefix.parent.name
    return None


def required_tool(prefix: Path) -> str:
    return "protontricks" if steam_appid(prefix) else "winetricks"


def tool_path(prefix: Path) -> Optional[str]:
    """Absolute path of the tool this prefix needs, or None when it is not installed."""
    return shutil.which(required_tool(prefix))


def plan(prefix: Path, components: Sequence[str], force: bool = False) -> Plan:
    """Build the command that installs `components` into `prefix`.

    Raises ValueError for an unknown component or a folder that is not a Wine prefix,
    and RuntimeError when the needed tool is not installed.
    """
    prefix = Path(prefix)
    if not (prefix / "drive_c" / "windows").is_dir():
        raise ValueError(f"not a Wine prefix (drive_c/windows missing): {prefix}")
    if not components:
        raise ValueError("no component selected")
    bad = [c for c in components if c not in _KNOWN]
    if bad:
        raise ValueError("unknown component: " + ", ".join(bad))
    tool = required_tool(prefix)
    exe = shutil.which(tool)
    if exe is None:
        raise RuntimeError(f"{tool} is not installed")
    appid = steam_appid(prefix)
    opts = ["-q", "--force"] if force else ["-q"]
    if appid:
        return Plan([exe, appid, *opts, *components], {}, tool)
    return Plan([exe, *opts, *components], {"WINEPREFIX": str(prefix)}, tool)
