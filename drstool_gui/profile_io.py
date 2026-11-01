"""Profile export / import text and share-code helpers."""

import json
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from .base import share_code
from .gamescope import GamescopeFlagsWidget
from .lint import lint_config
from .state import state_from_profile

if TYPE_CHECKING:
    from .lint import LintIssue
    from .sysinfo import SysInfo


# ----------------------------------------------------------------------------
# Profile file exchange
# ----------------------------------------------------------------------------

PROFILE_EXPORT_KEY = "drstool_profile"


def profile_export_text(name: str, prof: dict) -> str:
    return json.dumps({PROFILE_EXPORT_KEY: 1, "name": name, "profile": prof}, indent=2)


def _plausible_profile(p) -> bool:
    return isinstance(p, dict) and bool(set(p) & {"settings", "env_vars", "arch", "gamescope_flags",
                                                   "gamescope_enabled"})


def parse_profile_import(text: str) -> Dict[str, dict]:
    """Single-profile export, or a whole profiles.json. Raises ValueError."""
    data = json.loads(text)
    if isinstance(data, dict) and data.get(PROFILE_EXPORT_KEY):
        if not _plausible_profile(data.get("profile")):
            raise ValueError("the file has no valid profile in it")
        return {str(data.get("name") or "Imported"): data["profile"]}
    if isinstance(data, dict):
        out = {str(k): v for k, v in data.items() if _plausible_profile(v)}
        if out:
            return out
    raise ValueError("not a DRSTool profile export or profiles.json")


def unique_profile_name(base: str, taken) -> str:
    if base not in taken:
        return base
    i = 2
    while f"{base} ({i})" in taken:
        i += 1
    return f"{base} ({i})"


# ----------------------------------------------------------------------------
# Profile share codes (one line of text: DRSTOOL1:…)
# ----------------------------------------------------------------------------

def profile_share_code(name: str, prof: dict) -> str:
    """Share code for a saved / captured profile; the home folder is written as ~."""
    return share_code.encode(name, prof, home=str(Path.home()))


def lint_profile(prof: dict, si: Optional["SysInfo"] = None) -> List["LintIssue"]:
    """lint_config() for a profile dict (what the launch line of that profile would be)."""
    st = state_from_profile(prof)
    env: Dict[str, str] = {}
    if st["arch"]:
        env["DXVK_NVAPI_GPU_ARCH"] = st["arch"]
    if st["settings"]:
        env["DXVK_NVAPI_DRS_SETTINGS"] = ",".join(f"{k}={v}" for k, v in st["settings"].items())
    env.update(st["env"])
    gs_flags = st["gs_flags"] if st["gs_on"] else {}
    mangohud = st["gs_on"] and GamescopeFlagsWidget.MANGOHUD_KEY in st["gs_flags"]
    return lint_config(env, st["settings"], st["arch"] or "", st["gs_on"], gs_flags, mangohud, si)
