"""The catalog as module-level lists: DRS settings, GPU architectures, env var groups, gamescope
flags. Data lives in drstool_core/data/*.json.
"""

from typing import Dict, List

from drstool_core import catalog
from drstool_core.models import EnvVarDef, GamescopeFlagDef, GPUArch, Setting


# ============================================================================
# GPU Architectures
# ============================================================================

GPU_ARCHS: List[GPUArch] = catalog.gpu_archs()


# ============================================================================
# DRS settings (from NvApiDriverSettings.h) — data: drstool_core/data/drs_settings.json
# ============================================================================

# Loaded once at import time and shared everywhere (SettingsManager and MainWindow both
# use this list instead of building their own).
ALL_SETTINGS: List[Setting] = catalog.drs_settings()


# ============================================================================
# Environment Variable Definitions (DXVK + VKD3D-Proton)
# ============================================================================

_ENV = catalog.env_var_groups()      # data: drstool_core/data/env_vars.json
DXVK_ENV_VARS: List[EnvVarDef] = _ENV["DXVK_ENV_VARS"]

# Per-flag descriptions for VKD3D_CONFIG checkbox UI
# ============================================================================
# DXVK_CONFIG key picker — curated dxvk.conf keys (upstream dxvk.conf, 2026-09)
# (key, kind, options-or-placeholder, description)
#   kind "enum": options list; "" (unset) is always added as first choice
#   kind "num" : free numeric text (ints, floats or negatives allowed)
# Keys not listed here are preserved verbatim in the "Other entries" field.
# ============================================================================
DXVK_CONFIG_KEYS = catalog.dxvk_config_keys()
DXVK_CONFIG_KEY_NAMES = [k[0] for k in DXVK_CONFIG_KEYS]

VKD3D_CONFIG_DESCS: Dict[str, str] = catalog.vkd3d_config_descs()

VKD3D_ENV_VARS: List[EnvVarDef] = _ENV["VKD3D_ENV_VARS"]


# ============================================================================
# NVIDIA OpenGL Environment Variables (__GL_*)
# ============================================================================

NV_ENV_VARS: List[EnvVarDef] = _ENV["NV_ENV_VARS"]

# ============================================================================
# NVIDIA PRIME / Hybrid GPU Environment Variables (Optimus laptops)
# ============================================================================

NVIDIA_PRIME_ENV_VARS: List[EnvVarDef] = _ENV["NVIDIA_PRIME_ENV_VARS"]

FLM_ENV_VARS: List[EnvVarDef] = _ENV["FLM_ENV_VARS"]

# ============================================================================
# Proton Environment Variables (PROTON_* runtime config, Valve + Proton-CachyOS)
# ============================================================================

PROTON_ENV_VARS: List[EnvVarDef] = _ENV["PROTON_ENV_VARS"]

# ============================================================================
# Wine Environment Variables (WINE_* / WINEDEBUG, Wine + Proton runtime)
# ============================================================================

WINE_ENV_VARS: List[EnvVarDef] = _ENV["WINE_ENV_VARS"]

# ============================================================================
# DXVK-NVAPI Environment Variables (jp7677/dxvk-nvapi runtime tuning)
# ============================================================================

DXVK_NVAPI_ENV_VARS: List[EnvVarDef] = _ENV["DXVK_NVAPI_ENV_VARS"]

# ============================================================================
# NVIDIA Smooth Motion Environment Variables (VK_LAYER_NV_present / NVPresent)
# ============================================================================

NVPRESENT_ENV_VARS: List[EnvVarDef] = _ENV["NVPRESENT_ENV_VARS"]

# ============================================================================
# System / Loader Environment Variables (Vulkan loader, dynamic linker, SDL)
# ============================================================================

SYS_ENV_VARS: List[EnvVarDef] = _ENV["SYS_ENV_VARS"]

# ============================================================================
# Gamescope — Steam-side Environment Variables
# These are genuinely read from the environment (by the Steam client's own
# gamescope-session integration, or by gamescope's Steam-overlay path) —
# confirmed against gamescope's GitHub issues/source and Steam community
# session scripts. They are NOT read by the gamescope binary's own argv
# parser. Everything that IS a gamescope command-line option (resolution,
# HDR, upscaling, VRR, debug flags, etc.) lives in GAMESCOPE_FLAGS below and
# is rendered as a CLI flag builder, not as env vars.
# ============================================================================

GAMESCOPE_ENV_VARS: List[EnvVarDef] = _ENV["GAMESCOPE_ENV_VARS"]

# ============================================================================
# Gamescope — Command-Line Flags (real, from `gamescope --help`)
# gamescope has NO config-file or env-var input for these; they are argv-only.
# Rendered by GamescopeFlagsWidget as a CLI-flag builder gated behind a master
# "Enable Gamescope" toggle, producing:  gamescope <flags...> --
# (ends in a bare "--" — not Steam-specific, no %command% assumed)
# ============================================================================

GAMESCOPE_FLAGS: List[GamescopeFlagDef] = catalog.gamescope_flags()


# ============================================================================
# DXVK / VKD3D FORK ENV VARS
# ----------------------------------------------------------------------------
# Read straight out of the forks' own sources rather than their READMEs, so
# what's here is what the code actually looks up:
#
#   DXVK-Sarek          github.com/pythonlover02/DXVK-Sarek
#                       Vulkan 1.1/1.2 DXVK for hardware below upstream's
#                       requirements (1.10.x branch + backports).
#                       Officially supported by proton-cachyos:
#                       PROTON_DXVK_SAREK=1
#
#   dxvk-low-latency    github.com/netborg-afps/dxvk-low-latency
#                       Upstream DXVK plus a low-latency frame pacer.
#                       Shipped in proton-cachyos:
#                       PROTON_DXVK_LOWLATENCY=1
#                       (otherwise: drop the .dll files into a custom Proton)
#
#   vkd3d-low-latency   github.com/netborg-afps/vkd3d-low-latency
#                       vkd3d-proton plus the same frame pacer, wired to the
#                       Reflex API and waitable DXGI swapchains. Shipped in
#                       proton-cachyos: PROTON_VKD3D_LOWLATENCY=1
#                       (otherwise: replace d3d12.dll / d3d12core.dll inside a
#                       custom Proton build by hand).
#
#   d7vk                github.com/WinterSnowfall/d7vk
#                       D3D7/6/5/3 on top of DXVK's D3D9 backend. Standalone
#                       layer, no Proton switch. (Sarek carries the same
#                       feature set for older hardware.)
#
# Vars that turned out to be plain upstream DXVK / vkd3d-proton ones that
# DRSTool simply didn't cover yet are filed under the existing "DXVK" and
# "VKD3D-Proton" categories instead of here — labelling them as fork flags
# would be wrong.
# ============================================================================

FORK_ENV_VARS: List[EnvVarDef] = _ENV["FORK_ENV_VARS"]


OVERLAY_ENV_VARS: List[EnvVarDef] = _ENV["OVERLAY_ENV_VARS"]

ALL_ENV_VARS = (DXVK_ENV_VARS + VKD3D_ENV_VARS + NV_ENV_VARS + NVIDIA_PRIME_ENV_VARS +
                 PROTON_ENV_VARS + WINE_ENV_VARS + DXVK_NVAPI_ENV_VARS + NVPRESENT_ENV_VARS +
                 SYS_ENV_VARS + FLM_ENV_VARS + GAMESCOPE_ENV_VARS +
                 FORK_ENV_VARS + OVERLAY_ENV_VARS)

# ── Master logging toggle ────────────────────────────────────────────────────
# ON  = verbose debugging: every picker at its most detailed level short of
#       per-call/per-frame tracing (trace / FLM DEBUG / WINEDEBUG +all would
#       make the game itself unplayable and the log gigabytes).
#       WINEDEBUG = Proton's own PROTON_LOG=1 channel set + warn+all. It must
#       be set explicitly: Proton only applies its set when WINEDEBUG is
#       unset, and plain Wine/Lutris never does.
# OFF = every picker at its none/off value; pure debug switches removed.
# Log *path/file* vars are left alone in both states.
LOG_ENV_ON: Dict[str, str] = {
    "DXVK_LOG_LEVEL": "debug",
    "D7VK_LOG_LEVEL": "debug",
    "VKD3D_DEBUG": "warn",              # none<err<info<fixme<warn<trace
    "VKD3D_SHADER_DEBUG": "warn",
    "DXVK_NVAPI_LOG_LEVEL": "info",     # only other level is per-call trace
    "DXVK_NVAPI_VKREFLEX_LAYER_LOG_LEVEL": "debug",
    "NVPRESENT_LOG_LEVEL": "4",         # NVIDIA README: 4 = debug logging
    "FLM_LOG_LEVEL": "INFO",            # DEBUG is near per-frame
    "FLM_STATS": "1",
    "__GL_EVENT_LOGLEVEL": "DEBUG",
    "PROTON_LOG": "1",
    "WINEDEBUG": "warn+all,+timestamp,+pid,+tid,+seh,+unwind,+threadname,"
                 "+debugstr,+loaddll,+mscoree",
    "VK_LOADER_DEBUG": "error,warn,info,layer,driver",   # levels aren't cumulative
}
LOG_ENV_OFF: Dict[str, str] = {
    "DXVK_LOG_LEVEL": "none",
    "D7VK_LOG_LEVEL": "none",
    "VKD3D_DEBUG": "none",
    "VKD3D_SHADER_DEBUG": "none",
    "DXVK_NVAPI_LOG_LEVEL": "none",
    "DXVK_NVAPI_VKREFLEX_LAYER_LOG_LEVEL": "none",
    "NVPRESENT_LOG_LEVEL": "0",
    "FLM_LOG_LEVEL": "ERROR",      # FLM has no "off"; ERROR is its quietest
    "__GL_EVENT_LOGLEVEL": "OFF",
    "PROTON_LOG": "0",
    "WINEDEBUG": "-all",
}
LOG_ENV_UNSET_OFF = ("VK_LOADER_DEBUG", "FLM_STATS", "FLM_CSV",
                     "VKD3D_LOG_BUFFERED", "DXVK_DEBUG")
LOG_VKD3D_CONFIG_FLAGS = ("breadcrumbs", "breadcrumbs_sync", "breadcrumbs_trace",
                          "log_memory_budget", "fault", "pipeline_library_log")


def vkd3d_config_tokens(value: str) -> List[str]:
    """Split a VKD3D_CONFIG value the same way the flag grid does (',' or ';',
    whitespace stripped). Plain split(",") elsewhere missed 'a; breadcrumbs'
    and ' fault' style values."""
    return [t.strip() for t in (value or "").replace(";", ",").split(",") if t.strip()]

# Sidebar display order for the Environment tab. Forks sit right under the
# upstream project they patch.
ENV_CATEGORY_ORDER = [
    "DXVK", "DXVK Forks", "VKD3D-Proton", "VKD3D Forks", "DXVK-NVAPI",
    "NVIDIA __GL", "NVIDIA PRIME", "NVIDIA Smooth Motion",
    "Proton", "Wine", "System / Loader", "MangoHud / vkBasalt", "vk_flip_meter", "Gamescope",
]
