"""Lookups over the catalog: DRS key/value canonicalisation, custom (non-catalog) settings and env
vars, GPU architecture ranking.
"""

import re
from typing import Dict, Optional, TYPE_CHECKING

from drstool_core.models import EnvVarDef, GPUArch, Setting

from .catalog_data import ALL_ENV_VARS, ALL_SETTINGS, GPU_ARCHS
from .sysinfo import SYSHUB

if TYPE_CHECKING:
    from .sysinfo import SysInfo


# ============================================================================
# Catalog lookups (DRS ids and values, env vars, GPU architectures)
# ============================================================================


_SETTING_BY_ID: Dict[str, "Setting"] = {s.id.upper(): s for s in ALL_SETTINGS}


# ----------------------------------------------------------------------------
# DRS id / value canonicalisation
# ----------------------------------------------------------------------------
# The editor stores enum values exactly as the catalog spells them ("0xffffff")
# and highlights the active button by string compare. Anything that arrives
# from outside (a pasted launch line, a .nip export, a hand-written profile)
# spells the same number differently ("0x00FFFFFF", "16777215") and would show
# up as "set" with no button lit. Everything external goes through these.

_SETTING_BY_INT: Dict[int, Setting] = {int(s.id, 16): s for s in ALL_SETTINGS}


def _parse_int_any(raw) -> Optional[int]:
    t = str(raw if raw is not None else "").strip()
    if not t:
        return None
    try:
        if t.lower().startswith("0x"):
            return int(t, 16)
        if t.isdigit():
            return int(t, 10)
        if re.fullmatch(r"[0-9a-fA-F]+", t):
            return int(t, 16)
    except ValueError:
        pass
    return None


def drs_lookup(key) -> Optional[Setting]:
    iv = _parse_int_any(key)
    return _SETTING_BY_INT.get(iv) if iv is not None else None


def canonical_drs_key(key) -> str:
    s = drs_lookup(key)
    if s:
        return s.id
    iv = _parse_int_any(key)
    return f"0x{iv:08X}" if iv is not None else str(key).strip()


def canonical_drs_value(setting: Optional[Setting], raw) -> str:
    iv = _parse_int_any(raw)
    if iv is None:
        return str(raw).strip()
    if setting is not None:
        for v in setting.values:
            if _parse_int_any(v.val) == iv:
                return v.val
        for p in setting.presets:
            if _parse_int_any(p.val) == iv:
                return p.val
        if setting.type == "bitfield":
            return f"0x{iv:08X}"
    return f"0x{iv:X}"


CUSTOM_DRS_CAT = "Custom IDs (not in catalog)"
_CUSTOM_SETTINGS: Dict[str, Setting] = {}


def custom_drs_setting(key: str) -> Setting:
    s = _CUSTOM_SETTINGS.get(key)
    if s is None:
        s = Setting(key, f"Unknown {key}", CUSTOM_DRS_CAT, "hex", "0x0",
                    "Setting ID not in DRSTool's catalog",
                    "This ID is not part of the NvApiDriverSettings.h catalog DRSTool ships. "
                    "It came from an imported launch line, a .nip export or a hand-edited "
                    "profile and is passed through to DXVK_NVAPI_DRS_SETTINGS unchanged. "
                    "Edit the raw value below, or remove it.")
        _CUSTOM_SETTINGS[key] = s
    return s


# ----------------------------------------------------------------------------
# Env definitions for names outside the catalog
# ----------------------------------------------------------------------------

_ENV_BY_NAME: Dict[str, EnvVarDef] = {ev.name: ev for ev in ALL_ENV_VARS}
_CUSTOM_ENV: Dict[str, EnvVarDef] = {}
CUSTOM_ENV_CAT = "Custom (not in catalog)"
_ENV_NAME_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def env_def(name: str) -> Optional[EnvVarDef]:
    """Catalog definition, or a synthetic free-text one so imported variables
    stay visible, editable and removable instead of becoming invisible ballast."""
    from .env_widgets import EnvVarsWidget
    if not name or name == EnvVarsWidget.GAMESCOPE_KEY:
        return None
    ev = _ENV_BY_NAME.get(name) or _CUSTOM_ENV.get(name)
    if ev is None and _ENV_NAME_RX.fullmatch(name):
        ev = EnvVarDef(name, CUSTOM_ENV_CAT, "string", "",
                       "Not in DRSTool's catalog — imported from a launch line, recipe or profile. "
                       "It is written to every output exactly like the catalog variables.")
        _CUSTOM_ENV[name] = ev
    return ev


# ----------------------------------------------------------------------------
# Real GPU generation (the hardware, not the DXVK_NVAPI_GPU_ARCH spoof)
# ----------------------------------------------------------------------------

_GEN_RULES = [
    (r"\bRTX\s*(?:PRO\s*)?50\d\d|blackwell|\bGB20\d", "GB200"),
    (r"\bRTX\s*40\d\d|\bada\b|\bAD10\d", "AD100"),
    (r"\bRTX\s*30\d\d|\bampere\b|\bGA10\d|\bRTX\s*A\d{3,4}\b", "GA100"),
    (r"\bRTX\s*20\d\d|\bGTX\s*16\d\d|\bturing\b|\bTU1\d\d|TITAN\s*RTX|Quadro\s*RTX", "TU100"),
    (r"TITAN\s*V\b|\bvolta\b", "GV100"),
    (r"\bGTX\s*10\d\d|\bGT\s*10\d\d|\bpascal\b|TITAN\s*Xp", "GP100"),
    (r"\bGTX\s*75\d", "GM000"),
    (r"\bGTX\s*9\d\d|\bmaxwell\b", "GM200"),
    (r"\bGTX\s*[67]\d\d|\bkepler\b", "GK100"),
]
_ARCH_RANK = {a.code: i for i, a in enumerate(GPU_ARCHS)}


def detect_real_arch(si: Optional["SysInfo"] = None) -> str:
    """GPU_ARCHS code of the NVIDIA card in this machine, or '' if unknown."""
    si = si or SYSHUB.info
    names = list(si.nv_gpus) + [g.name for g in si.vk_gpus if g.is_nvidia]
    for n in names:
        for rx, code in _GEN_RULES:
            if re.search(rx, n, re.I):
                return code
    return ""


def arch_rank(code: str) -> int:
    return _ARCH_RANK.get(code or "", -1)


def arch_by_code(code: str) -> Optional[GPUArch]:
    return next((a for a in GPU_ARCHS if a.code == code), None)
