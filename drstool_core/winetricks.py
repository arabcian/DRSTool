"""winetricks / protontricks command planning for a Wine prefix (Qt-free).

Steam (Proton) prefixes live at steamapps/compatdata/<appid>/pfx and are driven by
protontricks, which needs the app id. Every other prefix (Lutris, Bottles, ~/.wine,
custom folders) is driven by winetricks with WINEPREFIX set. Only the components in
COMPONENTS can be requested; the command is always an argv list, never a shell string.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# (winetricks verb, label shown in the UI). Verbs are fixed here on purpose: a verb
# typed by the user is never passed to the tool.
COMPONENTS: Tuple[Tuple[str, str], ...] = (
    ("vcrun2019", "Visual C++ 2015-2019 runtime"),
    ("vcrun2022", "Visual C++ 2015-2022 runtime"),
    ("d3dcompiler_47", "D3D compiler 47 (shader compiler)"),
    ("d3dx9", "D3DX9 (older games)"),
    ("xact", "XACT audio"),
)
_KNOWN = frozenset(c for c, _ in COMPONENTS)


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


def plan(prefix: Path, components: Sequence[str]) -> Plan:
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
    if appid:
        return Plan([exe, appid, "-q", *components], {}, tool)
    return Plan([exe, "-q", *components], {"WINEPREFIX": str(prefix)}, tool)
