"""Recipes: curated bundles of settings."""

import sys
from dataclasses import dataclass
from typing import List

from .launch_import import _PRIME_RUN_ENV, op_status
from .lookup import (
    _ENV_BY_NAME, _parse_int_any, arch_by_code, arch_rank, canonical_drs_value,
    detect_real_arch, drs_lookup,
)
from .lutris_sync import LutrisSyncWidget
from .sysinfo import SYSHUB


# ----------------------------------------------------------------------------
# Recipes: curated, catalog-validated bundles
# ----------------------------------------------------------------------------

@dataclass
class Recipe:
    rid: str
    title: str
    tags: List[str]
    summary: str
    details: str
    build: object                   # callable(SysInfo) -> List[op]
    requires: object = None         # callable(SysInfo) -> str   ("" = fine)
    gs_replace: bool = False


def _e(name, value):
    return dict(kind="env", key=name, value=value)


def _u(name):
    return dict(kind="env_unset", key=name, value=None)


def _d(sid, value):
    return dict(kind="drs", key=sid, value=value)


def _g(flag, value=""):
    return dict(kind="gs_flag", key=flag, value=value)


def _req_gen(min_code: str, label: str):
    def check(si):
        real = detect_real_arch(si)
        if not real:
            return "" if not si.probed or not si.nvidia_known_absent else "No NVIDIA GPU detected"
        if arch_rank(real) < arch_rank(min_code):
            a = arch_by_code(real)
            return f"Needs {label} — this machine has {a.name if a else real}"
        return ""
    return check


def _req_ext(ext: str):
    def check(si):
        return f"{ext} is not exposed by this driver" if si.has_ext(ext) is False else ""
    return check


def _hdr_wayland_ops(si):
    ops = [_e("PROTON_ENABLE_WAYLAND", "1"), _e("DXVK_HDR", "1"), _e("PROTON_ENABLE_HDR", "1"),
           _u("DXVK_NO_HDR")]
    if si.nv_major and si.nv_major < 595:
        ops.append(_e("ENABLE_HDR_WSI", "1"))
    return ops


def _req_wayland(si):
    if si.session and "wayland" not in si.session.lower():
        return f"Needs a Wayland session (this one is {si.session})"
    return ""


def _req_hybrid(si):
    if si.vk_ok and len(si.vk_gpus) < 2:
        return "Only one GPU detected — no hybrid setup to steer"
    return ""


def _req_ntsync(si):
    return "/dev/ntsync not found (kernel 6.14+ with CONFIG_NTSYNC)" if si.probed and not si.ntsync_dev else ""


RECIPES: List[Recipe] = [
    Recipe("dlss-latest", "DLSS: newest models", ["DLSS", "quality"],
           "Driver picks the newest DLSS Super Resolution / Ray Reconstruction model, "
           "and Proton swaps in the newest DLL.",
           "Sets the SR and RR presets to <b>Latest</b> (the same thing the NVIDIA App's "
           "\"Latest\" override does) and PROTON_DLSS_UPGRADE=1 so Proton builds that support it "
           "download the newest nvngx_dlss DLL instead of the one the game shipped. "
           "Has no effect in games without DLSS.",
           lambda si: [_d("0x10E41DF3", "0xffffff"), _d("0x10E41DF7", "0xffffff"),
                       _e("PROTON_DLSS_UPGRADE", "1")],
           _req_gen("TU100", "an RTX (Turing or newer) GPU")),
    Recipe("dlaa", "DLSS: force DLAA", ["DLSS", "quality", "AA"],
           "Native-resolution DLSS anti-aliasing instead of upscaling.",
           "Forces the DLAA override and puts DLSS-SR into DLAA mode. Costs more GPU time than "
           "Quality mode but gives the cleanest image; pair with <i>DLSS: newest models</i>.",
           lambda si: [_d("0x10E41DF4", "0x1"), _d("0x10AFB768", "0x4")],
           _req_gen("TU100", "an RTX (Turing or newer) GPU")),
    Recipe("mfg-4x", "Frame Generation: Multi-Frame 4x", ["DLSS", "FPS", "RTX 50"],
           "Turns a game's DLSS Frame Generation into 4x multi-frame generation.",
           "Sets DLSS-G Multi-Frame to 4x and the Frame Generation preset to Latest. The game must "
           "already offer DLSS Frame Generation; enable it in-game. Multi-frame generation is an "
           "RTX 50 (Blackwell) feature.",
           lambda si: [_d("0x104D6667", "0x3"), _d("0x10E41DF1", "0xffffff")],
           _req_gen("GB200", "an RTX 50 (Blackwell) GPU")),
    Recipe("dlss-verify", "DLSS: show the on-screen indicator", ["DLSS", "debug"],
           "Proves DLSS actually runs and shows which version / preset is active.",
           "PROTON_DLSS_INDICATOR=1 draws NVIDIA's DLSS debug overlay in a corner; "
           "DXVK_NVAPI_LOG_LEVEL=info makes dxvk-nvapi log what it reports to the game. "
           "Remove both again once you have checked.",
           lambda si: [_e("PROTON_DLSS_INDICATOR", "1"), _e("DXVK_NVAPI_LOG_LEVEL", "info")]),
    Recipe("reflex-vk", "Reflex in native-Vulkan games", ["latency"],
           "Enables dxvk-nvapi's Vulkan Reflex layer for titles like DOOM: The Dark Ages.",
           "DXVK_NVAPI_VKREFLEX=1 (and removes DISABLE_DXVK_NVAPI_VKREFLEX). Only for games that "
           "render in Vulkan natively; D3D11/D3D12 games get Reflex through DXVK / VKD3D-Proton.",
           lambda si: [_e("DXVK_NVAPI_VKREFLEX", "1"), _u("DISABLE_DXVK_NVAPI_VKREFLEX")],
           _req_ext("VK_NV_low_latency2")),
    Recipe("smooth-motion", "NVIDIA Smooth Motion", ["FPS", "RTX 50"],
           "Driver-side frame interpolation for Vulkan games without DLSS Frame Generation.",
           "NVPRESENT_ENABLE_SMOOTH_MOTION=1 loads VK_LAYER_NV_present. Do not combine with in-game "
           "frame generation.",
           lambda si: [_e("NVPRESENT_ENABLE_SMOOTH_MOTION", "1")],
           _req_gen("GB200", "an RTX 50 GPU (newer drivers may extend this)")),
    Recipe("hdr-wayland", "HDR without gamescope (Wayland)", ["HDR", "Wayland"],
           "Native Wine Wayland driver + HDR10 output, no nested compositor.",
           "PROTON_ENABLE_WAYLAND=1 switches Wine to winewayland.drv; DXVK_HDR / PROTON_ENABLE_HDR "
           "expose the HDR10 colour space to the game. On drivers older than 595 ENABLE_HDR_WSI=1 is "
           "added too. The Steam overlay does not work on winewayland in several builds.",
           _hdr_wayland_ops, _req_wayland),
    Recipe("hdr-gamescope", "HDR through gamescope", ["HDR", "gamescope"],
           "Runs the game in gamescope with HDR output.",
           "Enables the gamescope wrapper with --hdr-enabled and DXVK_HDR=1. Set the output "
           "resolution / refresh in the Gamescope builder afterwards.",
           lambda si: [dict(kind="gs_on", key="enabled", value="on"), _g("--hdr-enabled"),
                       _e("DXVK_HDR", "1"), _u("DXVK_NO_HDR")]),
    Recipe("shader-cache", "Fewer shader-compile stutters", ["stutter", "cache"],
           "Big, persistent, per-game NVIDIA shader cache that never gets evicted.",
           "Enables the NVIDIA disk cache, raises its size to 10 GiB, disables automatic cleanup "
           "and gives every game its own cache directory (PROTON_LOCAL_SHADER_CACHE). Shaders are "
           "still compiled the first time — this stops them from being thrown away again.",
           lambda si: [_e("__GL_SHADER_DISK_CACHE", "1"), _e("__GL_SHADER_DISK_CACHE_SIZE", "10737418240"),
                       _e("__GL_SHADER_DISK_CACHE_SKIP_CLEANUP", "1"), _e("PROTON_LOCAL_SHADER_CACHE", "1")]),
    Recipe("hybrid-dgpu", "Hybrid laptop: render on the RTX GPU", ["laptop", "PRIME"],
           "Same as prefixing with prime-run, expressed as environment variables.",
           "Routes Vulkan and GLX to the NVIDIA GPU on Optimus / PRIME laptops and removes a "
           "conflicting Mesa DRI_PRIME. Unnecessary when the iGPU is disabled in firmware "
           "(dGPU-only / MUX mode).",
           lambda si: [_e(k, v) for k, v in _PRIME_RUN_ENV.items()] + [_u("DRI_PRIME")],
           _req_hybrid),
    Recipe("ntsync", "NTSync", ["CPU", "sync"],
           "Kernel-backed Windows synchronisation primitives for Proton builds that need opting in.",
           "PROTON_USE_NTSYNC=1 (and removes PROTON_NO_NTSYNC). Builds where ntsync is already the "
           "default ignore it.",
           lambda si: [_e("PROTON_USE_NTSYNC", "1"), _u("PROTON_NO_NTSYNC")], _req_ntsync),
    Recipe("dxvk-lowlatency", "Low-latency DXVK (Proton-CachyOS)", ["latency", "CachyOS"],
           "Uses the dxvk-low-latency fork bundled with Proton-CachyOS.",
           "PROTON_DXVK_LOWLATENCY=1. Mostly helps single-player D3D9-11 games; leave it off where "
           "Reflex is available.",
           lambda si: [_e("PROTON_DXVK_LOWLATENCY", "1")]),
]


def _validate_ops(ops: List[dict]) -> List[dict]:
    """Drop recipe ops that no longer match the catalog, so a catalog edit can never
    make a recipe write a misspelt variable."""
    good = []
    gs_table = LutrisSyncWidget._flag_lookup()
    for o in ops:
        k = o["kind"]
        ok = True
        if k in ("env", "env_unset"):
            ev = _ENV_BY_NAME.get(o["key"])
            ok = ev is not None and (k == "env_unset" or ev.vtype != "enum" or not ev.options
                                     or o["value"] in ev.options)
        elif k == "drs":
            s = drs_lookup(o["key"])
            ok = s is not None and (s.type != "enum" or any(
                _parse_int_any(v.val) == _parse_int_any(o["value"]) for v in s.values))
            if ok:
                o = dict(o, key=s.id, value=canonical_drs_value(s, o["value"]))
        elif k == "gs_flag":
            ok = o["key"] in gs_table
        if ok:
            good.append(o)
        else:
            print(f"DRSTool: recipe op dropped (not in catalog): {o}", file=sys.stderr)
    return good


def recipe_ops(r: Recipe, si=None) -> List[dict]:
    try:
        return _validate_ops(r.build(si or SYSHUB.info))
    except Exception as ex:                     # a recipe bug must not break the dialog
        print(f"DRSTool: recipe {r.rid} failed: {ex}", file=sys.stderr)
        return []


def recipe_requirement(r: Recipe, si=None) -> str:
    try:
        return r.requires(si or SYSHUB.info) if r.requires else ""
    except Exception:
        return ""


def recipe_coverage(r: Recipe, cur: dict) -> tuple:
    """(positive ops already in place, positive ops, fully applied?). Removals only
    count towards "fully applied" — an already-absent variable is not progress."""
    ops = recipe_ops(r)
    pos = [o for o in ops if o["kind"] not in ("env_unset", "drs_unset")]
    done = sum(1 for o in pos if op_status(o, cur) == "same")
    clean = all(op_status(o, cur) == "absent" for o in ops if o not in pos)
    return done, len(pos), bool(pos) and done == len(pos) and clean
