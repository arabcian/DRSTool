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


# Presentation only: (section title, [names]). Anything not listed lands in
# "Other" so a newly added flag/key is never hidden.
VKD3D_FLAG_GROUPS = [
    ("⚡ Performance & frame pacing", ["force_static_cbv", "no_clear_uav_sync", "prefer_thin_uav_tiling",
        "no_staggered_submit", "memory_allocator_skip_clear", "memory_allocator_skip_image_heap_clear",
        "recycle_command_pools", "allow_image_heap_suballocation"]),
    ("🧠 Memory & VRAM", ["no_upload_hvv", "no_gpu_upload_heap", "small_vram_rebar", "host_import_fallback",
        "disallow_committed_texture_suballocation", "avoid_image_buffer_aliasing",
        "defer_resource_destruction", "retain_psos"]),
    ("🔦 Raytracing (DXR)", ["nodxr", "dxr", "dxr12", "allow_sbt_collection", "extra_rtas_sync",
        "rtas_allow_blas_rebuild_sizes", "zero_fill_blp"]),
    ("💾 Pipeline & shader cache", ["pipeline_library_ignore_mismatch_driver", "pipeline_library_app_cache",
        "pipeline_library_no_serialize_spirv", "pipeline_library_ignore_spirv",
        "pipeline_library_sanitize_spirv", "pipeline_library_log", "global_pipeline_cache",
        "shader_cache_sync"]),
    ("🧩 Descriptors & binding", ["descriptor_heap", "descriptor_heap_robustness", "mutable_single_set",
        "force_raw_va_cbv", "preallocate_srv_mip_clamps"]),
    ("🛠 Troubleshooting: rendering & corruption", ["no_invariant_position", "disable_uav_compression",
        "disable_simultaneous_uav_compression", "disable_color_compression", "disable_depth_compression",
        "skip_null_sparse_tiles", "force_dynamic_msaa", "reject_padded_small_resource_alignment",
        "force_initial_transition"]),
    ("🛠 Troubleshooting: crashes, hangs & sync", ["single_queue", "one_time_submit", "ignore_shared_fence"]),
    ("🖥 GPU / driver specific", ["no_nvx", "enable_experimental_features", "adreno_7xx_extra_compat"]),
    ("🐞 Debugging & diagnostics", ["breadcrumbs", "breadcrumbs_sync", "breadcrumbs_trace", "fault",
        "log_memory_budget", "vk_debug", "debug_utils", "app_debug_marker_only", "queue_profile_extra",
        "descriptor_qa_checks", "instruction_qa_checks", "damage_not_zeroed_allocations",
        "debug_wave64_simulation", "fail_unsupported_state_template", "skip_application_workarounds",
        "skip_driver_workarounds", "force_host_cached"]),
]

DXVK_KEY_GROUPS = [
    ("⚡ Performance", ["dxvk.enableGraphicsPipelineLibrary", "dxvk.numCompilerThreads", "d3d11.maxTessFactor",
        "d3d11.relaxedBarriers", "d3d11.relaxedGraphicsBarriers", "d3d11.disableMsaa",
        "d3d11.cachedDynamicResources", "d3d9.cachedWriteOnlyBuffers", "d3d9.deviceLocalConstantBuffers",
        "d3d9.useFP16"]),
    ("⏱ Frame pacing & latency", ["dxvk.latencySleep", "dxvk.latencyTolerance", "dxvk.disableNvLowLatency2",
        "dxvk.maxFrameRate", "dxgi.maxFrameRate", "d3d9.maxFrameRate", "dxgi.maxFrameLatency",
        "d3d9.maxFrameLatency", "dxgi.syncInterval", "d3d9.presentInterval", "dxvk.tearFree",
        "dxvk.enablePresentTiming"]),
    ("🖥 Display & HDR", ["dxgi.forceRefreshRate", "d3d9.forceRefreshRate", "dxgi.enableHDR",
        "d3d9.forceAspectRatio"]),
    ("🎞 Texture filtering", ["d3d11.samplerAnisotropy", "d3d9.samplerAnisotropy", "d3d11.samplerLodBias",
        "d3d9.samplerLodBias", "d3d11.clampNegativeLodBias", "d3d9.clampNegativeLodBias"]),
    ("🧠 Memory & VRAM", ["dxgi.maxDeviceMemory", "dxgi.maxSharedMemory", "dxvk.maxMemoryBudget",
        "d3d9.maxAvailableMemory", "dxvk.zeroMappedMemory", "dxvk.enableMemoryDefrag"]),
    ("🎭 GPU identification & spoofing", ["dxgi.hideNvidiaGpu", "dxgi.hideAmdGpu", "dxgi.hideIntelGpu",
        "dxgi.hideNvkGpu", "dxvk.hideIntegratedGraphics", "dxvk.enableNvCudaInterop", "dxgi.customVendorId",
        "dxgi.customDeviceId", "dxgi.customDeviceDesc", "dxvk.deviceFilter", "dxgi.enableUe4Workarounds"]),
    ("🧩 Vulkan backend & descriptors", ["dxvk.enableDescriptorHeap", "dxvk.enableDescriptorBuffer",
        "dxvk.useRawSsbo", "dxvk.enableNvRawAccessChains", "dxvk.enableImplicitResolves",
        "dxvk.enableUnifiedImageLayouts", "dxvk.lowerSinCos", "dxvk.tilerMode"]),
    ("🛠 Troubleshooting: Direct3D 11", ["d3d11.maxFeatureLevel", "d3d11.forceComputeLdsBarriers",
        "d3d11.forceComputeUavBarriers", "d3d11.forceSampleRateShading", "d3d11.disableDirectImageMapping",
        "d3d11.exposeDriverCommandLists", "d3d11.enableContextLock"]),
    ("🛠 Troubleshooting: Direct3D 9", ["d3d9.forceSampleRateShading", "d3d9.floatEmulation",
        "d3d9.lenientClear", "d3d9.seamlessCubes"]),
    ("🐞 Debugging", ["dxvk.enableDebugUtils", "dxvk.trackPipelineLifetime"]),
]


def grouped_names(groups, names):
    """[(title, [name,...])] following `groups`, keeping only names that exist;
    unlisted names go to a trailing "Other" section."""
    have = list(names)
    seen = set()
    out = []
    for title, lst in groups:
        sel = [n for n in lst if n in have and n not in seen]
        seen.update(sel)
        if sel:
            out.append((title, sel))
    rest = [n for n in have if n not in seen]
    if rest:
        out.append(("Other", rest))
    return out
