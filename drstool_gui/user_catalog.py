"""User catalog: entries added from the drift table (or imported), kept in
~/.config/drstool/user_catalog.json and merged into the built-in catalog lists once at start-up
(``apply_user_catalog``). Built-in entries always win; a bad record is skipped, never fatal.
Qt-free; the drift dialogs that edit it live in ``upstream_drift``.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from drstool_core.fsutil import atomic_write
from drstool_core.models import EnvVarDef, GamescopeFlagDef, Setting, SettingValue

from .catalog_data import (
    ALL_ENV_VARS, ALL_SETTINGS, DXVK_CONFIG_KEY_NAMES, DXVK_CONFIG_KEYS, DXVK_KEY_GROUPS,
    GAMESCOPE_FLAGS, VKD3D_CONFIG_DESCS, VKD3D_ENV_VARS, VKD3D_FLAG_GROUPS,
)
from .lookup import _ENV_BY_NAME, _SETTING_BY_ID, _SETTING_BY_INT
from .widgets import _xdg

_log = logging.getLogger(__name__)


USER_CATALOG_KEYS: set = set()      # "env:NAME", "vkd3d:flag", "conf:key", "gs:--flag", "drs:0xID", "src:key"
_UC_LISTS = {"env": ("env_vars", "name"), "vkd3d": ("vkd3d_flags", "name"),
             "conf": ("dxvk_conf_keys", "key"), "gs": ("gamescope_flags", "flag"),
             "drs": ("drs_settings", "id"), "src": ("sources", "key")}
_UC_LABEL = {"env": "Env var", "vkd3d": "VKD3D_CONFIG flag", "conf": "dxvk.conf key",
             "gs": "Gamescope flag", "drs": "DRS setting", "src": "Drift source"}


def user_catalog_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "drstool" / "user_catalog.json"


def load_user_catalog() -> dict:
    """Never raises: a missing/corrupt file is an empty catalog; non-dict entries are dropped."""
    try:
        raw = json.loads(user_catalog_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as e:
        _log.warning("user catalog ignored (%s)", e)
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    data: dict = {"version": 1}
    for lst, _kf in _UC_LISTS.values():
        v = raw.get(lst)
        data[lst] = [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    return data


def user_tm_tools() -> List[dict]:
    """The user's own Tool Manager sources ("tm_tools": [{id, name, kind, desc, url, src, api, …}])."""
    try:
        v = json.loads(user_catalog_path().read_text(encoding="utf-8")).get("tm_tools")
    except (OSError, ValueError, AttributeError):
        return []
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def save_user_catalog(data: dict) -> None:
    atomic_write(user_catalog_path(), json.dumps(data, indent=2, ensure_ascii=False))


def _uc_group(groups: list, group: str, name: str) -> None:
    """Add `name` to the presentation group titled `group` (created if new); ''/'Other' → left to 'Other'."""
    if group and group != "Other":
        for title, lst in groups:
            if title == group:
                lst.append(name)
                return
        groups.append((group, [name]))


def _uc_apply_one(target: str, key: str, rec: dict, vk_env) -> bool:
    """Merge one record. False = already present (nothing changed)."""
    desc = str(rec.get("desc", ""))
    opts = [str(o) for o in rec["options"]] if isinstance(rec.get("options"), list) else []
    group = str(rec.get("group", ""))
    if target == "env":
        if key in _ENV_BY_NAME:
            return False
        ev = EnvVarDef(key, str(rec.get("cat") or "Other"), str(rec.get("vtype") or "string"),
                       str(rec.get("default", "")), desc, options=opts,
                       placeholder=str(rec.get("placeholder", "")))
        ALL_ENV_VARS.append(ev)
        _ENV_BY_NAME[key] = ev
    elif target == "vkd3d":
        if vk_env is None or key in vk_env.options or key in VKD3D_CONFIG_DESCS:
            return False
        vk_env.options.append(key)
        VKD3D_CONFIG_DESCS[key] = desc
        _uc_group(VKD3D_FLAG_GROUPS, group, key)
    elif target == "conf":
        if key in DXVK_CONFIG_KEY_NAMES:
            return False
        kind = str(rec.get("kind") or "text")
        opt = rec.get("opt", [])
        if kind == "enum" and not (isinstance(opt, list) and opt):
            raise ValueError("enum needs an option list")
        DXVK_CONFIG_KEYS.append((key, kind, [str(o) for o in opt] if kind == "enum" else str(opt or ""), desc))
        DXVK_CONFIG_KEY_NAMES.append(key)
        _uc_group(DXVK_KEY_GROUPS, group, key)
    elif target == "gs":
        if key in {g.flag for g in GAMESCOPE_FLAGS}:
            return False
        GAMESCOPE_FLAGS.append(GamescopeFlagDef(
            str(rec.get("name") or key), key, str(rec.get("cat") or "Session"), str(rec.get("kind") or "toggle"),
            desc, short=str(rec.get("short", "")), options=opts))
    elif target == "drs":
        if not re.fullmatch(r"0[xX][0-9A-Fa-f]{8}", key):
            raise ValueError("DRS id must be 0x + 8 hex digits")
        sid = "0x" + key[2:].upper()
        if sid.upper() in _SETTING_BY_ID:
            return False
        st = Setting(sid, str(rec.get("name") or sid), str(rec.get("cat") or "Misc"), str(rec.get("type") or "hex"),
                     str(rec.get("default") or "0x0"), desc,
                     values=[SettingValue(str(v[0]), str(v[1])) for v in rec.get("values", [])])
        ALL_SETTINGS.append(st)
        _SETTING_BY_ID[sid.upper()] = st
        _SETTING_BY_INT[int(sid, 16)] = st
    else:                                                   # "src"
        from .upstream_drift import DRIFT_SOURCES, _drift_source_from_record   # (circular at import time)
        if key in {s["key"] for s in DRIFT_SOURCES}:
            return False
        DRIFT_SOURCES.append(_drift_source_from_record(rec))
    return True


def apply_user_catalog(data: dict) -> List[str]:
    """Merge the user catalog into the built-in lists, in place. Returns notes for skipped entries."""
    notes: List[str] = []
    vk_env = next((e for e in VKD3D_ENV_VARS if e.name == "VKD3D_CONFIG"), None)
    for target, (lst, kf) in _UC_LISTS.items():
        for rec in data.get(lst, []):
            try:
                key = str(rec[kf]).strip()
                if not key:
                    raise ValueError("empty name")
                if _uc_apply_one(target, key, rec, vk_env):
                    USER_CATALOG_KEYS.add(f"{target}:{key}")
                else:
                    notes.append(f"{_UC_LABEL[target]} {key}: already in the catalog — skipped")
            except Exception as e:                          # one bad record must not stop the others
                notes.append(f"{_UC_LABEL[target]} entry skipped ({type(e).__name__}: {e})")
    return notes


def _uc_add(data: dict, target: str, rec: dict) -> bool:
    """Insert, or replace the record with the same key. True = new."""
    lst, kf = _UC_LISTS[target]
    items = data.setdefault(lst, [])
    for i, x in enumerate(items):
        if str(x.get(kf, "")).lower() == str(rec[kf]).lower():
            items[i] = rec
            return False
    items.append(rec)
    return True


def _uc_merge(data: dict, other: dict) -> int:
    """Import: add records of `other` whose key is not in `data` yet. Returns how many were added."""
    n = 0
    for target, (lst, kf) in _UC_LISTS.items():
        have = {str(x.get(kf, "")).lower() for x in data.setdefault(lst, [])}
        for rec in other.get(lst, []) if isinstance(other.get(lst), list) else []:
            if isinstance(rec, dict) and str(rec.get(kf, "")).strip() and str(rec[kf]).lower() not in have:
                data[lst].append(rec)
                have.add(str(rec[kf]).lower())
                n += 1
    return n


def _uc_in_catalog(target: str, name: str, cat: dict) -> bool:
    return {"env": name in cat["env_all"], "vkd3d": name in cat["vkd3d_flags"],
            "conf": name in cat["conf_keys"], "gs": name.lstrip("-") in cat["gs_flags"],
            "drs": ("0X" + name[2:].upper()) in cat["drs_ids"]}.get(target, False)


def _uc_validate(f: dict, cat: Optional[dict] = None) -> str:
    """'' when the form is fine, else why not. `cat` (from _drift_catalog) enables the duplicate check."""
    t, n = f["target"], f["name"].strip()
    if not n:
        return "name is empty"
    if t == "drs" and not re.fullmatch(r"0[xX][0-9A-Fa-f]{8}", n):
        return "DRS id must be 0x followed by 8 hex digits"
    if t != "vkd3d" and f["type"] == "enum" and not f["options"]:
        return "an enum needs at least one option"
    if t == "drs" and f["type"] == "enum" and any("=" not in o for o in f["options"]):
        return "DRS values must look like Label=0x0"
    if cat is not None and _uc_in_catalog(t, n, cat):
        return "already in the catalog (built-in entries win)"
    return ""


def _uc_record(f: dict) -> dict:
    """Form dict → JSON record (see _drift_guess for the form shape)."""
    t, n = f["target"], f["name"].strip()
    rec: dict
    if t == "env":
        rec = {"name": n, "cat": f["cat"], "vtype": f["type"], "default": f["default"], "desc": f["desc"],
               "options": list(f["options"]), "placeholder": f["placeholder"]}
    elif t == "vkd3d":
        rec = {"name": n, "desc": f["desc"], "group": f["group"]}
    elif t == "conf":
        rec = {"key": n, "kind": f["type"],
               "opt": list(f["options"]) if f["type"] == "enum" else f["placeholder"],
               "desc": f["desc"], "group": f["group"]}
    elif t == "gs":
        rec = {"name": f.get("label") or n.lstrip("-").replace("-", " ").title(), "flag": n, "cat": f["cat"],
               "kind": f["type"], "desc": f["desc"], "short": f.get("short", ""), "options": list(f["options"])}
    else:
        rec = {"id": "0x" + n[2:].upper(), "name": f.get("label") or n, "cat": f["cat"], "type": f["type"],
               "default": f["default"] or "0x0", "desc": f["desc"],
               "values": [[a.strip(), b.strip()] for a, _, b in (o.partition("=") for o in f["options"])]}
    rec["source"] = f.get("src", "")
    rec["added"] = f.get("added") or datetime.now().strftime("%Y-%m-%d")
    return rec


def _uc_form(target: str, rec: dict) -> dict:
    """JSON record → form dict (for editing)."""
    f = {"target": target, "cat": rec.get("cat", ""), "type": "", "options": [], "default": "",
         "placeholder": "", "group": rec.get("group", ""), "desc": rec.get("desc", ""),
         "src": rec.get("source", ""), "added": rec.get("added", ""), "ctx": "", "url": ""}
    if target == "env":
        f.update(name=rec["name"], type=rec.get("vtype", "string"), default=rec.get("default", ""),
                 options=list(rec.get("options", [])), placeholder=rec.get("placeholder", ""))
    elif target == "vkd3d":
        f.update(name=rec["name"])
    elif target == "conf":
        opt = rec.get("opt", "")
        f.update(name=rec["key"], type=rec.get("kind", "text"), options=list(opt) if isinstance(opt, list) else [],
                 placeholder="" if isinstance(opt, list) else str(opt))
    elif target == "gs":
        f.update(name=rec["flag"], label=rec.get("name", ""), type=rec.get("kind", "toggle"),
                 options=list(rec.get("options", [])), short=rec.get("short", ""))
    else:
        f.update(name=rec["id"], label=rec.get("name", ""), type=rec.get("type", "hex"),
                 default=rec.get("default", ""), options=[f"{a}={b}" for a, b in rec.get("values", [])])
    return f


def _uc_as_json(target: str, rec: dict) -> str:
    """The record as an entry for drstool_core/data/*.json, to make it a built-in one."""
    g = rec.get
    if target == "env":       # env_vars.json, inside its group's list
        e = {"name": rec["name"], "cat": g("cat", ""), "vtype": g("vtype", "string"),
             "default": g("default", ""), "desc": g("desc", "")}
        e.update({k: g(k) for k in ("options", "placeholder") if g(k)})
    elif target == "vkd3d":   # vkd3d_config_descs.json
        e = {rec["name"]: g("desc", "")}
    elif target == "conf":    # dxvk_config_keys.json
        e = [rec["key"], g("kind", "text"), g("opt", ""), g("desc", "")]
    elif target == "gs":      # gamescope_flags.json
        e = {"name": g("name", ""), "flag": rec["flag"], "cat": g("cat", ""), "kind": g("kind", "toggle"),
             "desc": g("desc", "")}
        e.update({k: g(k) for k in ("short", "options") if g(k)})
    elif target == "drs":     # drs_settings.json
        e = {"id": rec["id"], "name": g("name", ""), "cat": g("cat", ""), "type": g("type", "hex"),
             "default": g("default", "0x0"), "desc": g("desc", ""),
             "values": [{"name": a, "val": b} for a, b in g("values", [])]}
    else:
        e = rec                   # drift source: user catalog only
    return json.dumps(e, ensure_ascii=False)
