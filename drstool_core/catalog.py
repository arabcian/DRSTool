"""Loads the DRSTool catalog from the JSON files in ``drstool_core/data/``.

Qt-free. Fields that equal the dataclass default are omitted from the files, so an entry
only has to spell out what is special about it.

    gpu_archs.json          list of GPUArch
    drs_settings.json       list of Setting (values / bits / presets nested)
    env_vars.json           {group name: [EnvVarDef, ...]} — group order is the sidebar order
    gamescope_flags.json    list of GamescopeFlagDef
    dxvk_config_keys.json   [[key, kind, options-or-default, description], ...]
    vkd3d_config_descs.json {VKD3D_CONFIG flag: description}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Tuple

from .models import (BitField, EnvVarDef, GamescopeFlagDef, GPUArch, Preset, Setting,
                     SettingValue)

DATA_DIR = Path(__file__).resolve().parent / "data"


def _read(name: str):
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _setting(d: dict) -> Setting:
    d = dict(d)
    d["values"] = [SettingValue(**v) for v in d.get("values", [])]
    d["bits"] = [BitField(**b) for b in d.get("bits", [])]
    d["presets"] = [Preset(**p) for p in d.get("presets", [])]
    return Setting(**d)


def gpu_archs() -> List[GPUArch]:
    return [GPUArch(**d) for d in _read("gpu_archs.json")]


def drs_settings() -> List[Setting]:
    return [_setting(d) for d in _read("drs_settings.json")]


def env_var_groups() -> Dict[str, List[EnvVarDef]]:
    return {g: [EnvVarDef(**d) for d in items] for g, items in _read("env_vars.json").items()}


def gamescope_flags() -> List[GamescopeFlagDef]:
    return [GamescopeFlagDef(**d) for d in _read("gamescope_flags.json")]


def dxvk_config_keys() -> List[Tuple]:
    return [tuple(k) for k in _read("dxvk_config_keys.json")]


def vkd3d_config_descs() -> Dict[str, str]:
    return _read("vkd3d_config_descs.json")
