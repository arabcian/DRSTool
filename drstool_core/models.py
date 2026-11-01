"""Dataclasses describing the DRSTool catalog (DRS settings, env vars, gamescope flags, GPU archs).

Plain Python (stdlib only). The catalog *data* lives in ``drstool_core/data/*.json`` and is
loaded by :mod:`drstool_core.catalog`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class GPUArch:
    name: str
    arch: str
    prefix: str
    code: str
    example: str


@dataclass
class SettingValue:
    name: str
    val: str
    desc: str = ""


@dataclass
class BitField:
    name: str
    val: int
    # Optional explicit sub-field mask. When several BitFields share the same
    # mask (e.g. Override→App Ctrl=0x1, Override→Enhance=0x2,
    # Disable Override=0x3 all live in bits 0-1, mask=0x3), they represent
    # mutually-exclusive values of ONE multi-bit field rather than
    # independent flags. Previously this grouping wasn't modeled at all -
    # _toggle_bitfield guessed "is this a mask" purely from whether bit.val
    # itself was a power of two, so 0x1/0x2 were XOR'd independently while
    # 0x3 was mask-toggled, even though all three read/write the same two
    # bits. That let 0x1 and 0x2 be turned on together, silently producing
    # 0x3 (== "Disable Override") without that button ever being clicked,
    # and clicking 0x3 afterwards behaved inconsistently with the other two.
    # If left as 0 (default), the field falls back to per-bit XOR toggling
    # for plain independent flags - unaffected by this fix.
    mask: int = 0


@dataclass
class Preset:
    name: str
    val: str


@dataclass
class Setting:
    id: str
    name: str
    cat: str
    type: str
    default: str
    desc: str
    detailed_desc: str = ""
    values: List[SettingValue] = field(default_factory=list)
    bits: List[BitField] = field(default_factory=list)
    presets: List[Preset] = field(default_factory=list)
    min: int = 0
    max: int = 0


@dataclass
class EnvVarDef:
    """Definition of an environment variable with its metadata."""
    name: str
    cat: str          # "DXVK" or "VKD3D-Proton"
    vtype: str        # "string", "enum", "bool", "int", "flags"
    default: str
    desc: str
    options: List[str] = field(default_factory=list)  # for enum/flags types
    placeholder: str = ""
    exclusive: List[str] = field(default_factory=list)  # flags: at most one of these at a time


@dataclass
class GamescopeFlagDef:
    """
    Definition of a real `gamescope` command-line argument (per `gamescope --help`).
    NOT an environment variable — gamescope reads these from argv only, e.g.:
        gamescope -W 2560 -H 1440 --hdr-enabled --adaptive-sync --
    (nothing is assumed after `--`; the launcher/shell appends its own command)
    kind:
      "toggle" — bare flag, included with no value when enabled (e.g. --rt)
      "int"    — flag takes a numeric argument (e.g. -r 165)
      "enum"   — flag takes one of `options` as its argument (e.g. -F fsr)
      "string" — flag takes a free-form string/path argument
    short: the short form (e.g. "-W"), if any — used in the preview when set.
    """
    name: str          # human label, e.g. "Output Width"
    flag: str          # long flag as it appears in --help, e.g. "--output-width"
    cat: str           # grouping shown in the UI, e.g. "Geometry", "HDR", "Debug"
    kind: str          # "toggle" | "int" | "enum" | "string"
    desc: str
    short: str = ""
    options: List[str] = field(default_factory=list)
    placeholder: str = ""
