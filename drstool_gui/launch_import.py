"""Parses pasted launch lines / shell snippets into editor changes."""

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .catalog_data import GPU_ARCHS
from .gamescope import GamescopeFlagsWidget
from .lookup import (
    _ENV_BY_NAME, _parse_int_any, arch_by_code, canonical_drs_key, canonical_drs_value,
    drs_lookup, env_def,
)
from .state import _gs_flag_name, copy_state, empty_state
from .widgets import _fmt_drs_value


# ----------------------------------------------------------------------------
# Launch-line parser:  "A=1 B=2 gamescope -W 2560 -- mangohud %command% -dx12"
# ----------------------------------------------------------------------------

@dataclass
class ParsedLaunch:
    env: Dict[str, str] = field(default_factory=dict)
    unset: List[str] = field(default_factory=list)
    drs: Dict[str, str] = field(default_factory=dict)
    arch: Optional[str] = None
    gs_on: bool = False
    gs_flags: Dict[str, str] = field(default_factory=dict)
    mangohud: bool = False
    notes: List[tuple] = field(default_factory=list)     # (sev, text)  sev: info|warn|conv

    def empty(self) -> bool:
        return not (self.env or self.unset or self.drs or self.arch or self.gs_on or self.mangohud)


_ASSIGN_RX = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.S)
_BARE_DRS_RX = re.compile(r"(?:0x[0-9a-fA-F]+|\d+)=[^,\s]+(?:[,;](?:0x[0-9a-fA-F]+|\d+)=[^,\s]+)*[,;]?")
# prime-run is a 3-line script on every distro; expressing it as env vars keeps
# the launch line portable to launchers that have no "prefix command" field.
_PRIME_RUN_ENV = {"__NV_PRIME_RENDER_OFFLOAD": "1", "__VK_LAYER_NV_optimus": "NVIDIA_only",
                  "__GLX_VENDOR_LIBRARY_NAME": "nvidia"}
_WRAP_ARGS = {          # wrapper -> how to skip its own arguments
    "taskset": "taskset", "nice": "nice", "chrt": "chrt", "ionice": "ionice",
}
_WRAP_PLAIN = ("gamemoderun", "game-performance", "obs-gamecapture", "systemd-inhibit",
               "steam-runtime", "pressure-vessel-wrap", "zink-run", "dlss-swapper")
_WRAP_LGT = ("taskset", "nice", "chrt", "ionice")
_COMMAND_HINTS = ("umu-run", "proton", "wine", "wine64", "steam", "lutris", "heroic", "bottles-cli")


def _parse_drs_string(raw: str, out: ParsedLaunch):
    for pair in re.split(r"[,;]", raw or ""):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            out.notes.append(("warn", f"DRS pair without '=' ignored: {pair}"))
            continue
        k, v = (x.strip() for x in pair.split("=", 1))
        s = drs_lookup(k)
        key = canonical_drs_key(k)
        val = canonical_drs_value(s, v)
        if _parse_int_any(v) is None:
            out.notes.append(("warn", f"{key}: value '{v}' is not a number — kept verbatim"))
        if s is None:
            out.notes.append(("info", f"DRS ID {key} is not in the catalog — kept as a custom ID"))
        elif s.type == "enum" and s.values and not any(
                _parse_int_any(x.val) == _parse_int_any(v) for x in s.values):
            out.notes.append(("warn", f"{s.name}: {val} is not one of its documented values"))
        out.drs[key] = val


def _tokenize(text: str, out: ParsedLaunch) -> List[str]:
    text = re.sub(r"\\\r?\n", " ", text)
    toks: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            lt = shlex.split(line, comments=True)
        except ValueError as ex:
            out.notes.append(("warn", f"Shell quoting error ({ex}) — fell back to splitting on spaces"))
            lt = line.split()
        if lt and lt[0] == "export":
            lt = lt[1:]
        toks += lt
    return toks


def parse_launch_text(text: str) -> ParsedLaunch:
    """Turn a pasted launch line (Steam launch options, a shell snippet, a .env
    file, `export` lines, a bare DXVK_NVAPI_DRS_SETTINGS value) into editor data.
    Never guesses silently: everything dropped or converted is listed in .notes."""
    from .lutris_sync import LutrisSyncWidget
    out = ParsedLaunch()
    stripped = (text or "").strip()
    if not stripped:
        return out
    if _BARE_DRS_RX.fullmatch(stripped):
        out.notes.append(("conv", "Recognised a bare DXVK_NVAPI_DRS_SETTINGS value"))
        _parse_drs_string(stripped, out)
        return out
    toks = _tokenize(stripped, out)
    gs_table = LutrisSyncWidget._flag_lookup()
    wrappers: List[str] = []
    i, n = 0, len(toks)
    while i < n:
        tok = toks[i]
        base = os.path.basename(tok)
        m = _ASSIGN_RX.fullmatch(tok)
        if m and not tok.startswith("-"):
            out.env[m.group(1)] = m.group(2)
            i += 1
            continue
        if tok in ("env", "exec", "command", ";", "&&"):
            i += 1
            while tok == "env" and i < n and toks[i].startswith("-"):
                opt = toks[i]
                if opt in ("-u", "--unset") and i + 1 < n:
                    out.unset.append(toks[i + 1])
                    i += 2
                elif opt.startswith("--unset="):
                    out.unset.append(opt.split("=", 1)[1])
                    i += 1
                else:
                    out.notes.append(("info", f"env option {opt} ignored"))
                    i += 1
            continue
        if tok == "%command%":
            rest = toks[i + 1:]
            if rest:
                out.notes.append(("info", "Game arguments after %command% are not part of DRSTool's "
                                          "configuration and were left out: " + " ".join(rest)))
            break
        if base == "gamescope":
            out.gs_on = True
            i += 1
            while i < n and toks[i] != "--":
                t = toks[i]
                gf = gs_table.get(t)
                if gf is None and t.startswith("--") and "=" in t:
                    k, v = t.split("=", 1)
                    gf = gs_table.get(k)
                    if gf is not None:
                        out.gs_flags[gf.flag] = "" if gf.kind == "toggle" else v
                        i += 1
                        continue
                if gf is None:
                    if i + 1 < n and toks[i + 1] != "--" and not toks[i + 1].startswith("-"):
                        out.notes.append(("warn", f"Unknown gamescope argument dropped: {t} {toks[i + 1]}"))
                        i += 2
                    else:
                        out.notes.append(("warn", f"Unknown gamescope argument dropped: {t}"))
                        i += 1
                    continue
                if gf.kind == "toggle":
                    out.gs_flags[gf.flag] = ""
                    i += 1
                elif i + 1 < n and toks[i + 1] != "--":
                    out.gs_flags[gf.flag] = toks[i + 1]
                    i += 2
                else:
                    out.notes.append(("warn", f"gamescope {t} has no value — dropped"))
                    i += 1
            i += 1                                   # skip the '--'
            continue
        if base == "mangohud":
            out.mangohud = True
            i += 1
            if i < n and toks[i] == "--dlsym":
                i += 1
            continue
        if base == "prime-run":
            for k, v in _PRIME_RUN_ENV.items():
                out.env.setdefault(k, v)
            out.notes.append(("conv", "prime-run converted to its environment variables "
                                      "(__NV_PRIME_RENDER_OFFLOAD, __VK_LAYER_NV_optimus, "
                                      "__GLX_VENDOR_LIBRARY_NAME)"))
            i += 1
            continue
        if base == "strangle":
            i += 1
            if i < n and toks[i].isdigit():
                fps = toks[i]
                out.env.setdefault("DXVK_FRAME_RATE", fps)
                out.env.setdefault("VKD3D_FRAME_RATE", fps)
                out.notes.append(("conv", f"strangle {fps} converted to DXVK_FRAME_RATE / VKD3D_FRAME_RATE"))
                i += 1
            else:
                wrappers.append("strangle")
            continue
        if base in _WRAP_ARGS:
            wrappers.append(base)
            i += 1
            if base == "taskset":
                if i < n and toks[i].startswith("-"):
                    i += 1
                i += 1
            elif base in ("nice", "chrt"):
                while i < n and toks[i].startswith("-"):
                    i += 1 if re.fullmatch(r"-\d+", toks[i]) else 2 if toks[i] in ("-n", "--adjustment") else 1
                if base == "chrt" and i < n and toks[i].isdigit():
                    i += 1
            elif base == "ionice":
                while i < n and toks[i].startswith("-"):
                    i += 2 if toks[i] in ("-c", "-n", "-p") else 1
            continue
        if base in _WRAP_PLAIN:
            wrappers.append(base)
            i += 1
            continue
        if base in _COMMAND_HINTS or "/" in tok or tok.lower().endswith((".exe", ".sh", ".bat")):
            rest = " ".join(toks[i:])
            out.notes.append(("info", f"The actual game command was left out: {rest[:160]}"))
            break
        if tok == "--":
            i += 1
            continue
        out.notes.append(("warn", f"Unrecognised token ignored: {tok}"))
        i += 1

    if wrappers:
        lgt = [w for w in wrappers if w in _WRAP_LGT]
        out.notes.append(("info", "Command wrappers can't be stored in a DRSTool profile and were left out: "
                          + ", ".join(dict.fromkeys(wrappers))
                          + ("  —  CPU pinning / priority belong in Extra Tools › lutris-game-tune."
                             if lgt else "")))
    drs_raw = out.env.pop("DXVK_NVAPI_DRS_SETTINGS", None)
    if drs_raw is not None:
        _parse_drs_string(drs_raw, out)
    arch_raw = out.env.pop("DXVK_NVAPI_GPU_ARCH", None)
    if arch_raw is not None:
        a = arch_by_code(arch_raw.strip().upper())
        if a:
            out.arch = a.code
        else:
            out.notes.append(("warn", f"DXVK_NVAPI_GPU_ARCH={arch_raw} is not a code dxvk-nvapi accepts "
                                      f"({', '.join(x.code for x in GPU_ARCHS)}) — dropped"))
    if out.mangohud and not out.gs_on:
        out.mangohud = False
        if "MANGOHUD" not in out.env:
            out.env["MANGOHUD"] = "1"
            out.notes.append(("conv", "mangohud wrapper outside gamescope converted to MANGOHUD=1"))
    if out.mangohud:
        out.gs_flags[GamescopeFlagsWidget.MANGOHUD_KEY] = GamescopeFlagsWidget.MANGOHUD_PREFIX
    for k, v in out.env.items():
        ev = _ENV_BY_NAME.get(k)
        if ev is None:
            out.notes.append(("info", f"{k} is not in the catalog — imported as a custom variable"))
        elif ev.vtype == "enum" and ev.options and v not in ev.options:
            out.notes.append(("warn", f"{k}={v} is not one of its known values ({', '.join(ev.options[:8])})"))
    return out


def looks_like_launch_text(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 20000:
        return False
    return bool(_BARE_DRS_RX.fullmatch(t) or "%command%" in t or re.search(
        r"(?:^|\s)(?:DXVK|VKD3D|PROTON|WINE|__GL|__NV|NVPRESENT|FLM|MANGOHUD|DXVK_NVAPI)[A-Z0-9_]*=", t)
        or re.search(r"(?:^|\s)gamescope\s", t))


# ----------------------------------------------------------------------------
# Plans: a list of checkable operations + how they combine with the editor
# ----------------------------------------------------------------------------
# op = dict(kind, key, value)   kind: arch | drs | drs_unset | env | env_unset | gs_on | gs_flag

def ops_from_parsed(p: ParsedLaunch) -> List[dict]:
    ops: List[dict] = []
    if p.arch:
        ops.append(dict(kind="arch", key="GPU arch", value=p.arch))
    for k, v in p.drs.items():
        ops.append(dict(kind="drs", key=k, value=v))
    for k, v in p.env.items():
        ops.append(dict(kind="env", key=k, value=v))
    for k in p.unset:
        ops.append(dict(kind="env_unset", key=k, value=None))
    if p.gs_on:
        ops.append(dict(kind="gs_on", key="enabled", value="on"))
    for k, v in p.gs_flags.items():
        ops.append(dict(kind="gs_flag", key=k, value=v))
    return ops


def apply_ops(cur: dict, ops: List[dict], replace: bool, gs_replace: bool) -> dict:
    st = empty_state() if replace else copy_state(cur)
    if gs_replace and any(o["kind"] in ("gs_on", "gs_flag") for o in ops):
        st["gs_flags"] = {}
    for o in ops:
        k, v = o["kind"], o["value"]
        if k == "arch":
            st["arch"] = v
        elif k == "drs":
            st["settings"][o["key"]] = v
        elif k == "drs_unset":
            st["settings"].pop(o["key"], None)
        elif k == "env":
            st["env"][o["key"]] = v
        elif k == "env_unset":
            st["env"].pop(o["key"], None)
        elif k == "gs_on":
            st["gs_on"] = True
        elif k == "gs_flag":
            st["gs_flags"][o["key"]] = v
    return st


def op_label(o: dict) -> tuple:
    """(name, shown value, description) for a plan row."""
    from .lutris_sync import LutrisSyncWidget
    k = o["kind"]
    if k == "arch":
        a = arch_by_code(o["value"])
        return ("GPU arch spoof", f"{o['value']}  ({a.arch})" if a else o["value"],
                "DXVK_NVAPI_GPU_ARCH — makes dxvk-nvapi report this generation.")
    if k in ("drs", "drs_unset"):
        s = drs_lookup(o["key"])
        name = f"{s.name}  ({o['key']})" if s else f"{o['key']}  (custom ID)"
        if k == "drs_unset":
            return name, "remove", s.desc if s else ""
        shown = _fmt_drs_value(s, o["value"]) if s else o["value"]
        if shown != o["value"]:
            shown = f"{shown}  [{o['value']}]"
        return name, shown, (s.detailed_desc or s.desc) if s else "Passed through unchanged."
    if k in ("env", "env_unset"):
        ev = env_def(o["key"])
        return (o["key"], "remove" if k == "env_unset" else o["value"],
                ev.desc if ev else "")
    if k == "gs_on":
        return "Gamescope wrapper", "enabled", "Wrap the game in gamescope … --"
    gf = LutrisSyncWidget._flag_lookup().get(o["key"])
    return (_gs_flag_name(o["key"]), "on" if o["value"] == "" else o["value"],
            gf.desc if gf else "")


def op_status(o: dict, cur: dict) -> str:
    k, key, v = o["kind"], o["key"], o["value"]
    have = {"arch": cur["arch"], "drs": cur["settings"].get(key), "drs_unset": cur["settings"].get(key),
            "env": cur["env"].get(key), "env_unset": cur["env"].get(key),
            "gs_on": "on" if cur["gs_on"] else None, "gs_flag": cur["gs_flags"].get(key)}[k]
    if k in ("drs_unset", "env_unset"):
        return "absent" if have is None else "remove"
    if have is None:
        return "new"
    if k == "drs":
        return "same" if _parse_int_any(have) == _parse_int_any(v) and _parse_int_any(v) is not None \
            or have == v else "change"
    return "same" if str(have) == str(v) else "change"
