"""Consistency linter for env vars, DRS settings and gamescope flags."""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .catalog_data import ALL_SETTINGS, vkd3d_config_tokens
from .lookup import (
    _parse_int_any, arch_by_code, arch_rank, canonical_drs_key, detect_real_arch, drs_lookup,
)
from .sysinfo import SysInfo


# ----------------------------------------------------------------------------
# Consistency linter
# ----------------------------------------------------------------------------

@dataclass
class LintIssue:
    sev: str                       # "error" | "warn" | "info"
    code: str
    msg: str
    involved: List[str] = field(default_factory=list)
    # list of ("env_unset", NAME) | ("env_set", NAME, VALUE) | ("drs_unset", ID)
    # | ("gamescope_off",)
    fixes: List[tuple] = field(default_factory=list)
    fix_label: str = "Fix"


_FALSY = {"", "0", "false", "no", "off", "none"}


def _on(env: Dict[str, str], name: str) -> bool:
    return name in env and str(env[name]).strip().lower() not in _FALSY


def _driver_tuple(v: str):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
    except Exception:
        return ()


def lint_config(env: Dict[str, str], drs: Dict[str, str], arch_code: str,
                gs_on: bool, gs_flags: Dict[str, str], mangohud: bool,
                si: Optional[SysInfo] = None) -> List[LintIssue]:
    """Pure function: configuration in, list of issues out (unit-testable)."""
    si = si or SysInfo()
    out: List[LintIssue] = []

    def add(sev, code, msg, involved=(), fixes=(), label="Fix"):
        out.append(LintIssue(sev, code, msg, list(involved), list(fixes), label))

    def unset(*names):
        return [("env_unset", n) for n in names]

    nvapi_cfg = bool(arch_code or drs or any(k.startswith("DXVK_NVAPI_") for k in env))

    # Does the user steer rendering to a non-NVIDIA device?
    flt = (env.get("DXVK_FILTER_DEVICE_NAME", "") + " " + env.get("VKD3D_FILTER_DEVICE_NAME", "")
           + " " + env.get("VKD3D_VULKAN_DEVICE", "")).lower()
    amd_target = bool(re.search(r"amd|radeon|rdna|intel|arc\b", flt)) or "DRI_PRIME" in env and not _on(env, "__NV_PRIME_RENDER_OFFLOAD")
    nv_render = (si.has_nvidia or nvapi_cfg) and not amd_target

    # 1. PROTON_NVIDIA_LIBS + WoW64
    if _on(env, "PROTON_NVIDIA_LIBS") and _on(env, "PROTON_USE_WOW64"):
        sev = "info" if _on(env, "PROTON_NVIDIA_LIBS_NO_32BIT") else "error"
        add(sev, "nvlibs-wow64",
            "PROTON_NVIDIA_LIBS is incompatible with PROTON_USE_WOW64 (the 32-bit nvidia-libs "
            "shims have no place to load in a WoW64 process)."
            + (" PROTON_NVIDIA_LIBS_NO_32BIT is set, so only the 64-bit shims load — likely OK."
               if sev == "info" else ""),
            ["PROTON_NVIDIA_LIBS", "PROTON_USE_WOW64"],
            unset("PROTON_USE_WOW64") if sev == "error" else [], "Unset WOW64")
        if sev == "error":
            out[-1].fixes = unset("PROTON_USE_WOW64")
            add("info", "nvlibs-wow64-alt", "Alternative: keep WoW64 and drop the NVIDIA shims instead.",
                ["PROTON_NVIDIA_LIBS"], unset("PROTON_NVIDIA_LIBS", "PROTON_NVIDIA_LIBS_NO_32BIT"),
                "Drop shims")
    # 2. Sub-switches that do nothing without the master
    if _on(env, "PROTON_NVIDIA_LIBS_NO_32BIT") and not _on(env, "PROTON_NVIDIA_LIBS"):
        add("warn", "nvlibs-no32-orphan",
            "PROTON_NVIDIA_LIBS_NO_32BIT only restricts PROTON_NVIDIA_LIBS — which is not enabled.",
            ["PROTON_NVIDIA_LIBS_NO_32BIT"], unset("PROTON_NVIDIA_LIBS_NO_32BIT"))
    # 3. FSR4 on NVIDIA
    fsr4 = [n for n in ("PROTON_FSR4_UPGRADE", "PROTON_FSR4_INDICATOR", "PROTON_FFX4_UPGRADE")
            if _on(env, n)]
    if fsr4 and nv_render:
        add("warn", "fsr4-nvidia",
            "FSR 4 needs an AMD RDNA3/4 GPU; on the NVIDIA GPU these variables do nothing. "
            "Use PROTON_DLSS_UPGRADE instead (or point the game at the AMD iGPU with "
            "DXVK_FILTER_DEVICE_NAME).", fsr4, unset(*fsr4), "Unset FSR4")
    # 4. HDR WSI layer + new drivers
    drv = _driver_tuple(si.nv_driver)
    if _on(env, "ENABLE_HDR_WSI") and drv and drv[0] >= 595 and not amd_target:
        add("warn", "hdr-wsi-595",
            f"ENABLE_HDR_WSI is only needed on NVIDIA drivers older than 595; this system runs "
            f"{si.nv_driver}, which has native HDR WSI — the extra layer is redundant and can "
            f"misbehave.", ["ENABLE_HDR_WSI"], unset("ENABLE_HDR_WSI"), "Unset")
    if _on(env, "ENABLE_HDR_WSI") and drv and drv[0] < 595 and not (
            _on(env, "DXVK_HDR") or _on(env, "PROTON_ENABLE_HDR")):
        add("info", "hdr-wsi-orphan",
            "ENABLE_HDR_WSI has no visible effect without DXVK_HDR=1 / PROTON_ENABLE_HDR=1.",
            ["ENABLE_HDR_WSI"])
    # 5. HDR without a compositor path
    hdr_on = _on(env, "DXVK_HDR") or _on(env, "PROTON_ENABLE_HDR")
    if hdr_on:
        if _on(env, "DXVK_NO_HDR"):
            add("error", "hdr-contradiction", "DXVK_HDR and DXVK_NO_HDR are both set.",
                ["DXVK_HDR", "DXVK_NO_HDR"], unset("DXVK_NO_HDR"), "Unset NO_HDR")
        if not gs_on and not _on(env, "PROTON_ENABLE_WAYLAND") and si.session != "x11":
            add("warn", "hdr-no-path",
                "HDR output needs the Wayland driver (PROTON_ENABLE_WAYLAND=1) or gamescope with "
                "--hdr-enabled; neither is configured.", ["PROTON_ENABLE_HDR", "DXVK_HDR"],
                [("env_set", "PROTON_ENABLE_WAYLAND", "1")], "Enable Wayland")
        if gs_on and "--hdr-enabled" not in gs_flags:
            add("warn", "hdr-gs-flag", "HDR is requested but gamescope runs without --hdr-enabled.",
                ["--hdr-enabled"])
    # 6. Wayland vs forced X11
    if _on(env, "PROTON_ENABLE_WAYLAND") and env.get("SDL_VIDEODRIVER", "").lower() == "x11":
        add("warn", "wayland-vs-x11", "PROTON_ENABLE_WAYLAND=1 contradicts SDL_VIDEODRIVER=x11.",
            ["PROTON_ENABLE_WAYLAND", "SDL_VIDEODRIVER"], unset("SDL_VIDEODRIVER"), "Unset SDL_VIDEODRIVER")
    # 7. WineD3D replaces DXVK/VKD3D
    if _on(env, "PROTON_USE_WINED3D"):
        dead = [k for k in env if k.startswith(("DXVK_", "VKD3D_")) and k != "DXVK_NVAPI_DRS_SETTINGS"
                and not k.startswith("DXVK_NVAPI_")]
        if dead or nvapi_cfg:
            add("warn", "wined3d-dxvk",
                "PROTON_USE_WINED3D=1 replaces DXVK/VKD3D-Proton, so DXVK_*/VKD3D_*/NVAPI settings "
                "have no effect.", ["PROTON_USE_WINED3D"] + dead[:6], unset("PROTON_USE_WINED3D"),
                "Unset WINED3D")
    if _on(env, "PROTON_NO_D3D12") and any(k.startswith("VKD3D_") for k in env):
        add("info", "no-d3d12-vkd3d", "PROTON_NO_D3D12 disables VKD3D-Proton; VKD3D_* variables are unused.",
            ["PROTON_NO_D3D12"])
    if _on(env, "PROTON_NO_D3D11") and any(k.startswith("DXVK_") and not k.startswith("DXVK_NVAPI")
                                           for k in env):
        add("info", "no-d3d11-dxvk", "PROTON_NO_D3D11 is set; DXVK_* variables only apply to the "
            "remaining D3D8/9/10 paths.", ["PROTON_NO_D3D11"])
    # 8. NVAPI switches
    if _on(env, "PROTON_ENABLE_NVAPI") and _on(env, "PROTON_DISABLE_NVAPI"):
        add("error", "nvapi-both", "PROTON_ENABLE_NVAPI and PROTON_DISABLE_NVAPI are both set.",
            ["PROTON_ENABLE_NVAPI", "PROTON_DISABLE_NVAPI"], unset("PROTON_DISABLE_NVAPI"), "Unset DISABLE")
    if _on(env, "PROTON_ENABLE_NVAPI") and _on(env, "PROTON_HIDE_NVIDIA_GPU"):
        add("warn", "nvapi-hidden",
            "NVAPI is enabled but PROTON_HIDE_NVIDIA_GPU=1 reports the NVIDIA GPU as AMD — "
            "NVAPI/DLSS will not find it. Set PROTON_HIDE_NVIDIA_GPU=0.",
            ["PROTON_ENABLE_NVAPI", "PROTON_HIDE_NVIDIA_GPU"],
            [("env_set", "PROTON_HIDE_NVIDIA_GPU", "0")], "Set HIDE=0")
    if _on(env, "DXVK_ENABLE_NVAPI") is False and "DXVK_ENABLE_NVAPI" in env and nvapi_cfg:
        add("warn", "nvapi-hack-off",
            "DXVK_ENABLE_NVAPI=0 turns DXVK's NVAPI GPU spoof off, but DRS/arch settings expect "
            "dxvk-nvapi to be active.", ["DXVK_ENABLE_NVAPI"])
    # 9. Reflex / latency stacks
    if _on(env, "DXVK_NVAPI_VKREFLEX") and _on(env, "DISABLE_DXVK_NVAPI_VKREFLEX"):
        add("error", "vkreflex-both", "DXVK_NVAPI_VKREFLEX and DISABLE_DXVK_NVAPI_VKREFLEX are both set.",
            ["DXVK_NVAPI_VKREFLEX", "DISABLE_DXVK_NVAPI_VKREFLEX"],
            unset("DISABLE_DXVK_NVAPI_VKREFLEX"), "Unset DISABLE")
    lat = [n for n in ("DXVK_NVAPI_VKREFLEX", "LOW_LATENCY_LAYER", "PROTON_DXVK_LOWLATENCY",
                       "PROTON_VKD3D_LOWLATENCY") if _on(env, n)]
    if len(lat) >= 2:
        add("warn", "latency-stack",
            "Several latency-reduction paths are active at once (" + ", ".join(lat) +
            "); they all drive VK_NV_low_latency2 and can fight each other. Keep one.", lat)
    # 10. Sync primitives
    if _on(env, "PROTON_USE_NTSYNC") and _on(env, "PROTON_NO_NTSYNC"):
        add("error", "ntsync-both", "PROTON_USE_NTSYNC and PROTON_NO_NTSYNC are both set.",
            ["PROTON_USE_NTSYNC", "PROTON_NO_NTSYNC"], unset("PROTON_NO_NTSYNC"), "Unset NO_NTSYNC")
    if _on(env, "PROTON_USE_NTSYNC") and si.probed and not si.ntsync_dev:
        add("warn", "ntsync-missing",
            "PROTON_USE_NTSYNC=1 but /dev/ntsync does not exist (modprobe ntsync, kernel ≥ 6.14).",
            ["PROTON_USE_NTSYNC"])
    # 11. PRIME / device selection
    if _on(env, "__NV_PRIME_RENDER_OFFLOAD") and "DRI_PRIME" in env:
        add("warn", "prime-mixed", "__NV_PRIME_RENDER_OFFLOAD (NVIDIA) and DRI_PRIME (Mesa) are both set; "
            "use only the one that matches the GPU you want.", ["__NV_PRIME_RENDER_OFFLOAD", "DRI_PRIME"])
    if re.search(r"amd|radeon", flt) and nvapi_cfg:
        add("warn", "filter-amd-nvapi",
            "A DXVK/VKD3D device filter selects an AMD GPU, but DRS/arch/NVAPI settings only mean "
            "something on the NVIDIA GPU.", ["DXVK_FILTER_DEVICE_NAME"])
    # 12. Frame limiters
    limiters = {n: env[n] for n in ("DXVK_FRAME_RATE", "VKD3D_FRAME_RATE") if _on(env, n)}
    if "--framerate-limit" in gs_flags:
        limiters["gamescope --framerate-limit"] = gs_flags["--framerate-limit"]
    if len(limiters) >= 2:
        add("info", "multi-limiter", "More than one frame limiter is active ("
            + ", ".join(f"{k}={v}" for k, v in limiters.items()) +
            "); the lowest wins and pacing can get uneven.", list(limiters))
    # 13. Async shader patch
    if _on(env, "DXVK_ASYNC") or _on(env, "PROTON_DXVK_LLASYNC"):
        add("info", "async-gpl",
            "DXVK_ASYNC / PROTON_DXVK_LLASYNC only exist in dxvk-async/GE-style builds; upstream "
            "DXVK 2.x uses Graphics Pipeline Libraries instead and ignores them.",
            [n for n in ("DXVK_ASYNC", "PROTON_DXVK_LLASYNC") if n in env])
    # 14. OptiScaler vs individual upgrades
    ups = [n for n in ("PROTON_DLSS_UPGRADE", "PROTON_FSR3_UPGRADE", "PROTON_FSR4_UPGRADE",
                       "PROTON_XESS_UPGRADE", "PROTON_FFX3_UPGRADE", "PROTON_FFX4_UPGRADE") if _on(env, n)]
    if _on(env, "PROTON_USE_OPTISCALER") and ups:
        add("info", "optiscaler-upgrades",
            "With OptiScaler the PROTON_*_UPGRADE variables choose the DLL versions it injects; "
            "make sure they are the upscalers you actually want OptiScaler to swap.", ups)
    # 15. Gamescope WSI switches
    if _on(env, "ENABLE_GAMESCOPE_WSI") and _on(env, "DISABLE_GAMESCOPE_WSI"):
        add("error", "gswsi-both", "ENABLE_GAMESCOPE_WSI and DISABLE_GAMESCOPE_WSI are both set.",
            ["ENABLE_GAMESCOPE_WSI", "DISABLE_GAMESCOPE_WSI"], unset("DISABLE_GAMESCOPE_WSI"), "Unset DISABLE")
    gs_env = [k for k in env if k.startswith(("STEAM_GAMESCOPE_", "GAMESCOPE_"))]
    if gs_env and not gs_on:
        add("info", "gs-env-no-gs", "Gamescope-specific variables are set but gamescope itself is "
            "disabled in the Gamescope builder.", gs_env[:5])
    # 16. Overlays
    if mangohud and not gs_on:
        pass  # (the checkbox is greyed without gamescope, nothing to flag)
    if mangohud and _on(env, "MANGOHUD"):
        add("info", "mangohud-twice", "MangoHud is added both as a command prefix and via MANGOHUD=1 "
            "— two HUDs may draw on top of each other.", ["MANGOHUD"], unset("MANGOHUD"), "Unset MANGOHUD")
    if _on(env, "DXVK_HUD") and (mangohud or _on(env, "MANGOHUD")):
        add("info", "hud-twice", "DXVK_HUD and MangoHud are both active — you will see two overlays.",
            ["DXVK_HUD"])
    # 17. Vulkan feature requirements (needs probe)
    if si.vk_ok:
        if _on(env, "DXVK_NVAPI_VKREFLEX") and si.has_ext("VK_NV_low_latency2") is False:
            add("warn", "no-ll2", "DXVK_NVAPI_VKREFLEX needs VK_NV_low_latency2, which this driver "
                "does not expose.", ["DXVK_NVAPI_VKREFLEX"], unset("DXVK_NVAPI_VKREFLEX"), "Unset")
        flags = vkd3d_config_tokens(env.get("VKD3D_CONFIG", ""))
        if "descriptor_heap" in flags and si.has_ext("VK_EXT_descriptor_heap") is False:
            add("warn", "no-descheap", "VKD3D_CONFIG=descriptor_heap needs VK_EXT_descriptor_heap, "
                "which this driver does not expose.", ["VKD3D_CONFIG"],
                [("env_set", "VKD3D_CONFIG", ",".join(f for f in flags if f != "descriptor_heap"))],
                "Remove flag")
    # 18. nvidia-only vars with no nvidia
    if si.nvidia_known_absent and (nvapi_cfg or any(k.startswith(("__GL_", "PROTON_NVIDIA_")) for k in env)):
        add("info", "no-nvidia", "No NVIDIA driver was detected on this machine; NVIDIA-specific "
            "settings only matter if this configuration is for another PC.", [])
    # 19. DRS settings vs. the real GPU generation / mode dependencies
    out += lint_drs_rules(env, drs, arch_code, si)
    order = {"error": 0, "warn": 1, "info": 2}
    out.sort(key=lambda i: order.get(i.sev, 3))
    return out


# ----------------------------------------------------------------------------
# DRS-aware lint rules (called from lint_config)
# ----------------------------------------------------------------------------

def lint_drs_rules(env: Dict[str, str], drs: Dict[str, str], arch_code: str,
                   si: "SysInfo") -> List["LintIssue"]:
    out: List[LintIssue] = []
    norm = {canonical_drs_key(k): _parse_int_any(v) for k, v in drs.items()}

    def val(sid):
        return norm.get(sid)

    def add(sev, code, msg, involved=(), fixes=(), label="Fix"):
        out.append(LintIssue(sev, code, msg, list(involved), list(fixes), label))

    real = detect_real_arch(si)
    real_a = arch_by_code(real)
    # GPU arch spoof vs. the real card
    if arch_code and real:
        sa = arch_by_code(arch_code)
        if arch_code == real:
            add("info", "arch-redundant",
                f"DXVK_NVAPI_GPU_ARCH={arch_code} is the generation this card already is "
                f"({real_a.arch}) — the spoof changes nothing.",
                ["DXVK_NVAPI_GPU_ARCH"], [("arch_unset",)], "Clear arch")
        elif arch_rank(arch_code) > arch_rank(real):
            add("warn", "arch-newer",
                f"DXVK_NVAPI_GPU_ARCH reports {sa.arch if sa else arch_code} but the card is "
                f"{real_a.arch}: games may enable features this hardware cannot run "
                "(frame generation, newer DLSS paths) and crash or render black.",
                ["DXVK_NVAPI_GPU_ARCH"], [("arch_unset",)], "Clear arch")
        else:
            add("info", "arch-older",
                f"DXVK_NVAPI_GPU_ARCH reports an older generation ({sa.arch if sa else arch_code}) "
                f"than the real {real_a.arch}; games may hide DLSS / Frame Generation / Reflex. "
                "Meant only as a compatibility workaround.", ["DXVK_NVAPI_GPU_ARCH"])
    # Feature vs. hardware generation
    gen = arch_rank(real) if real else -1
    dlss_ids = {s.id for s in ALL_SETTINGS if s.cat == "DLSS / NGX"}
    used_dlss = [k for k in norm if k in dlss_ids and norm[k]]
    mfg = val("0x104D6667") or 0
    fg = bool(val("0x10E41E03")) or bool(val("0x10E41DF1")) or bool(val("0x10308298")) or mfg
    if real and used_dlss and gen < arch_rank("TU100"):
        add("warn", "dlss-gen", f"DLSS settings are set, but DLSS needs an RTX GPU — this card is "
            f"{real_a.name} ({real_a.arch}).", used_dlss,
            [("drs_unset", k) for k in used_dlss], "Remove DLSS settings")
    elif real and mfg >= 0x2 and gen < arch_rank("GB200"):
        add("warn", "mfg-gen", "DLSS-G Multi-Frame 3x or more needs an RTX 50 (Blackwell) GPU; "
            f"{real_a.arch} cards generate at most one frame (2x).", ["0x104D6667"],
            [("drs_set", "0x104D6667", "0x1")], "Use 2x")
    elif real and fg and gen < arch_rank("AD100"):
        add("warn", "fg-gen", "DLSS Frame Generation needs an RTX 40 or newer GPU; "
            f"this card is {real_a.arch}.", ["0x10E41E03", "0x10E41DF1", "0x104D6667"])
    # Settings that only matter in a particular mode
    sr_mode = val("0x10AFB768")
    if val("0x10E41DF5") and sr_mode is not None and sr_mode != 0x6:
        add("info", "dlss-scale-mode", "DLSS-SR Scaling is only used when DLSS-SR Mode is Custom.",
            ["0x10E41DF5", "0x10AFB768"], [("drs_set", "0x10AFB768", "0x6")], "Set mode Custom")
    if val("0x10E41DF4") == 0x1 and sr_mode is not None and sr_mode not in (0x3, 0x4):
        add("info", "dlaa-vs-mode", "DLAA Override and a non-DLAA DLSS-SR Mode pull in different "
            "directions — keep one of them.", ["0x10E41DF4", "0x10AFB768"],
            [("drs_set", "0x10AFB768", "0x4")], "Mode → DLAA")
    g_mode = val("0x10308298")
    if g_mode is not None and g_mode != 0x4 and (val("0x10562D0F") or val("0x10CF4125")):
        add("info", "dlssg-dynamic", "DLSSG Dynamic Max / Target FPS only take effect when DLSSG "
            "Mode is Dynamic.", ["0x10308298", "0x10562D0F", "0x10CF4125"],
            [("drs_set", "0x10308298", "0x4")], "Mode → Dynamic")
    if _on(env, "NVPRESENT_ENABLE_SMOOTH_MOTION") and fg:
        add("warn", "smooth-plus-fg", "NVIDIA Smooth Motion and DLSS Frame Generation are both "
            "requested — interpolating already generated frames adds latency and artefacts.",
            ["NVPRESENT_ENABLE_SMOOTH_MOTION"], [("env_unset", "NVPRESENT_ENABLE_SMOOTH_MOTION")],
            "Drop Smooth Motion")
    unknown = [k for k in drs if drs_lookup(k) is None]
    if unknown:
        add("info", "drs-unknown", f"{len(unknown)} DRS ID(s) are not in the catalog and are passed "
            "through unchanged: " + ", ".join(unknown[:6]), unknown)
    return out
