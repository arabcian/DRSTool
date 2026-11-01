"""Editor state snapshots, diffs and the undo timeline."""

import time
from typing import Dict, List, Optional

from .base import _as_dict
from .gamescope import GamescopeFlagsWidget, normalize_gs_flags
from .launch_line import gamescope_arg_string
from .lookup import canonical_drs_key, canonical_drs_value, drs_lookup
from .widgets import _fmt_drs_value


# ----------------------------------------------------------------------------
# Profile flatten / diff
# ----------------------------------------------------------------------------

def _flat_profile(p) -> Dict[tuple, str]:
    out: Dict[tuple, str] = {}
    if not isinstance(p, dict):
        return out
    for k, v in _as_dict(p.get("settings")).items():
        # Canonical forms: "0x1" vs "0x00000001" (or a decimal ID) is the same
        # setting/value and must not show up as a difference.
        out[("DRS", canonical_drs_key(k).upper())] = canonical_drs_value(drs_lookup(k), v)
    if p.get("arch"):
        out[("Arch", "GPU arch")] = str(p["arch"])
    for k, v in _as_dict(p.get("env_vars")).items():
        out[("Env", str(k))] = str(v)
    if p.get("gamescope_enabled"):
        out[("Gamescope", "enabled")] = "on"
    for k, v in _as_dict(p.get("gamescope_flags")).items():
        out[("Gamescope", str(k))] = str(v)
    return out


def diff_profiles(a, b) -> List[tuple]:
    """(section, key, value_in_a, value_in_b) for every difference; None = absent."""
    fa, fb = _flat_profile(a), _flat_profile(b)
    order = {"Arch": 0, "DRS": 1, "Env": 2, "Gamescope": 3}
    rows = []
    for key in sorted(set(fa) | set(fb), key=lambda k: (order.get(k[0], 9), k[1])):
        va, vb = fa.get(key), fb.get(key)
        if va != vb:
            rows.append((key[0], key[1], va, vb))
    return rows


# ----------------------------------------------------------------------------
# Editor state: one plain dict for everything the launch string is made of
# ----------------------------------------------------------------------------

def empty_state() -> dict:
    return {"settings": {}, "arch": None, "env": {}, "gs_on": False, "gs_flags": {}}


def copy_state(st: dict) -> dict:
    return {"settings": dict(st.get("settings") or {}), "arch": st.get("arch"),
            "env": dict(st.get("env") or {}), "gs_on": bool(st.get("gs_on")),
            "gs_flags": dict(st.get("gs_flags") or {})}


def state_as_profile(st: dict, raw_gs: bool = False) -> dict:
    """Profile-shaped dict. raw_gs keeps gamescope flags while it is off (undo needs
    them; saved profiles don't store them)."""
    return {"settings": dict(st["settings"]), "arch": st["arch"], "env_vars": dict(st["env"]),
            "gamescope_enabled": st["gs_on"],
            "gamescope_flags": dict(st["gs_flags"]) if (st["gs_on"] or raw_gs) else {}}


def state_from_profile(p) -> dict:
    p = p if isinstance(p, dict) else {}
    return {"settings": {canonical_drs_key(k): canonical_drs_value(drs_lookup(k), v)
                         for k, v in _as_dict(p.get("settings")).items() if v is not None and str(v).strip()},
            "arch": str(p["arch"]).strip().upper() if p.get("arch") else None,
            "env": {str(k): str(v) for k, v in _as_dict(p.get("env_vars")).items()},
            "gs_on": bool(p.get("gamescope_enabled")),
            "gs_flags": normalize_gs_flags(p.get("gamescope_flags"))[0]}


def canon_flat(p) -> Dict[tuple, str]:
    """_flat_profile with DRS ids folded to one spelling — for equality tests."""
    out = {}
    for (sec, key), v in _flat_profile(p).items():
        if sec == "DRS":
            s = drs_lookup(key)
            key = canonical_drs_key(key).upper()
            v = canonical_drs_value(s, v).lower()
        out[(sec, key)] = v
    return out


def state_payload(st: dict) -> dict:
    """Same shape as MainWindow.collect_launch_payload(), computed from a state dict."""
    env: Dict[str, str] = {}
    if st["arch"]:
        env["DXVK_NVAPI_GPU_ARCH"] = st["arch"]
    if st["settings"]:
        env["DXVK_NVAPI_DRS_SETTINGS"] = ",".join(f"{k}={v}" for k, v in st["settings"].items())
    env.update(st["env"])
    gs_flags = dict(st["gs_flags"]) if st["gs_on"] else {}
    gs_cmd = ""
    if st["gs_on"]:
        gs_cmd = " ".join(p for p in ("gamescope", gamescope_arg_string(gs_flags), "--") if p)
        if GamescopeFlagsWidget.MANGOHUD_KEY in gs_flags:
            gs_cmd += " " + GamescopeFlagsWidget.MANGOHUD_PREFIX
    return {"env": env, "drs": dict(st["settings"]), "arch": st["arch"] or "", "gs_on": st["gs_on"],
            "gs_flags": gs_flags, "mangohud": GamescopeFlagsWidget.MANGOHUD_KEY in gs_flags,
            "gs_cmd": gs_cmd}


def _gs_flag_name(flag: str) -> str:
    from .lutris_sync import LutrisSyncWidget
    if flag == GamescopeFlagsWidget.MANGOHUD_KEY:
        return "MangoHud (after --)"
    gf = LutrisSyncWidget._flag_lookup().get(flag)
    return f"{gf.name}  ({flag})" if gf else flag


def _change_name(sec: str, key: str) -> str:
    if sec == "DRS":
        s = drs_lookup(key)
        return s.name if s else key
    if sec == "Arch":
        return "GPU arch"
    if sec == "Gamescope":
        return "gamescope" if key == "enabled" else f"gamescope {key}"
    return key


def _change_value(sec: str, key: str, val) -> str:
    if val is None:
        return ""
    if sec == "DRS":
        s = drs_lookup(key)
        return _fmt_drs_value(s, val) if s else str(val)
    return str(val) if val != "" else "on"


def state_diff(a: dict, b: dict) -> List[tuple]:
    return diff_profiles(state_as_profile(a, True), state_as_profile(b, True))


def describe_change(a: dict, b: dict) -> str:
    rows = state_diff(a, b)
    if not rows:
        return "No change"
    if len(rows) == 1:
        sec, key, va, vb = rows[0]
        name = _change_name(sec, key)
        if vb is None:
            return f"{name} cleared"
        return f"{name} → {_change_value(sec, key, vb)}"
    names = []
    for sec, key, _va, _vb in rows:
        n = _change_name(sec, key)
        if n not in names:
            names.append(n)
    shown = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
    return f"{len(rows)} changes: {shown}"


class UndoTimeline:
    """Linear undo/redo over whole-editor snapshots.

    Consecutive edits of the *same single key* within COALESCE_S collapse into
    one step, so dragging a spin box or typing a value is one undo, not twelve."""
    LIMIT = 150
    COALESCE_S = 1.5

    def __init__(self, state: dict):
        self._items: List[dict] = [dict(state=copy_state(state), label="Session start",
                                        t=time.time(), key=None)]
        self._idx = 0

    def current(self) -> dict:
        return copy_state(self._items[self._idx]["state"])

    def push(self, state: dict, label: str, key=None):
        del self._items[self._idx + 1:]
        last = self._items[-1]
        now = time.time()
        if (key is not None and last["key"] == key and self._idx > 0
                and now - last["t"] < self.COALESCE_S):
            last.update(state=copy_state(state), label=label, t=now)
            return
        self._items.append(dict(state=copy_state(state), label=label, t=now, key=key))
        if len(self._items) > self.LIMIT:
            self._items = self._items[-self.LIMIT:]
            self._items[0]["label"] = "(older history dropped)"
        self._idx = len(self._items) - 1

    def can_undo(self) -> bool:
        return self._idx > 0

    def can_redo(self) -> bool:
        return self._idx < len(self._items) - 1

    def undo_label(self) -> str:
        return self._items[self._idx]["label"] if self.can_undo() else ""

    def redo_label(self) -> str:
        return self._items[self._idx + 1]["label"] if self.can_redo() else ""

    def undo(self) -> Optional[dict]:
        if not self.can_undo():
            return None
        self._idx -= 1
        return self.current()

    def redo(self) -> Optional[dict]:
        if not self.can_redo():
            return None
        self._idx += 1
        return self.current()

    def jump(self, idx: int) -> Optional[dict]:
        if 0 <= idx < len(self._items) and idx != self._idx:
            self._idx = idx
            return self.current()
        return None

    def entries(self) -> List[tuple]:
        return [(i, it["label"], it["t"], i == self._idx) for i, it in enumerate(self._items)]


def single_change_key(a: dict, b: dict):
    rows = state_diff(a, b)
    return (rows[0][0], rows[0][1]) if len(rows) == 1 else None
