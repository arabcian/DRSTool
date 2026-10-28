"""Read-only facts about one game for the game card: which Proton / Wine build runs it,
where its logs and frametime captures are. Qt-free, best effort: anything unreadable is
simply left out.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import game_scan

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None
_YAML_LOADER = getattr(yaml, "CSafeLoader", getattr(yaml, "SafeLoader", None))

_DXVK_LOG = re.compile(r"_(d3d8|d3d9|d3d10|d3d10core|d3d11|dxgi|d3d12|d3d12core)\.log$", re.I)
_SKIP = {"windows", "shadercache", "__pycache__", ".git", "dosdevices"}


def steam_appid(game) -> str:
    return game.key.split(":", 1)[1] if game.key.startswith("steam:") else ""


def _load_yaml(path: Path):
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None, ""
    if yaml is not None:
        try:
            return yaml.load(text, Loader=_YAML_LOADER), text
        except Exception:
            pass
    return None, text


def _vdf_block(text: str, name: str) -> str:
    """Body of the first `"name" { … }` block (nested braces counted)."""
    m = re.search(r'"' + re.escape(name) + r'"\s*\{', text, re.I)
    if not m:
        return ""
    depth, i = 1, m.end()
    while i < len(text) and depth:
        c = text[i]
        if c == '"':                                   # skip quoted strings (may hold braces)
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return text[m.end():i - 1]


def steam_compat_tool(appid: str, home: Optional[Path] = None) -> str:
    """Per-game compatibility tool from config.vdf ("" = Steam default)."""
    for root in game_scan.steam_roots(home):
        try:
            text = (root / "config" / "config.vdf").read_text(errors="replace")
        except OSError:
            continue
        body = _vdf_block(text, "CompatToolMapping")
        m = re.search(r'"' + re.escape(appid) + r'"\s*\{([^{}]*)\}', body)
        if m:
            n = re.search(r'"name"\s+"([^"]*)"', m.group(1))
            if n and n.group(1):
                return n.group(1)
    return ""


def _prefix_proton_version(pfx: Optional[Path]) -> str:
    """compatdata/<appid>/version: the Proton build that last ran the prefix."""
    if pfx is None:
        return ""
    for f in (pfx.parent / "version", pfx.parent / "config_info"):
        try:
            line = f.read_text(errors="replace").strip().splitlines()[0].strip()
        except (OSError, IndexError):
            continue
        if line:
            return line.split()[-1]
    return ""


def runtime_info(game, home: Optional[Path] = None) -> Dict[str, str]:
    """-> {"runner": "Proton"/"Wine"/…, "configured": build set in the launcher,
           "last_used": build that last ran the prefix, "extra": "DXVK dxvk-2.3 · …"}"""
    out = {"runner": "", "configured": "", "last_used": "", "extra": ""}
    if game.launcher == "Steam":
        out["runner"] = "Proton" if game.prefix is not None else ""
        out["configured"] = steam_compat_tool(steam_appid(game), home) or "Steam default"
        out["last_used"] = _prefix_proton_version(game.prefix)
        if game.prefix is None and not out["last_used"]:
            out["runner"], out["configured"] = "native / not run yet", ""
    elif game.launcher == "Lutris":
        cfg = game.key.split(":", 1)[1]
        for base in game_scan.lutris_dirs(home):
            data, text = _load_yaml(base / "games" / f"{cfg}.yml")
            if data is None and not text:
                continue
            if isinstance(data, dict):
                runner = str((data.get("system") or {}).get("runner") or "") if isinstance(
                    data.get("system"), dict) else ""
                wine = data.get("wine") if isinstance(data.get("wine"), dict) else {}
                out["runner"] = runner or ("Wine" if game.prefix else "")
                out["configured"] = str(wine.get("version") or "") or ("Lutris default" if game.prefix else "")
            else:
                m = re.search(r"^wine:\s*\n(?:[ \t]+.*\n)*?[ \t]+version:\s*(\S+)", text, re.M)
                out["runner"] = "Wine" if game.prefix else ""
                out["configured"] = m.group(1) if m else ""
            break
    elif game.launcher == "Heroic":
        app = game.key.split(":", 1)[1]
        for root in game_scan.heroic_roots(home):
            d = game_scan._json(root / "GamesConfig" / f"{app}.json")
            node = d.get(app) if isinstance(d, dict) else None
            if isinstance(node, dict):
                wv = node.get("wineVersion") if isinstance(node.get("wineVersion"), dict) else {}
                out["runner"] = str(wv.get("type") or "").capitalize() or ("Wine" if game.prefix else "")
                out["configured"] = str(wv.get("name") or "")
            break
        out["last_used"] = _prefix_proton_version(game.prefix) if game.prefix_manager == "Proton" else ""
    elif game.launcher == "Bottles" and game.prefix is not None:
        data, _t = _load_yaml(game.prefix / "bottle.yml")
        if isinstance(data, dict):
            out["runner"] = "Bottles"
            out["configured"] = str(data.get("Runner") or "")
            parts = [f"{k} {data[k]}" for k in ("DXVK", "VKD3D", "NVAPI") if data.get(k)]
            out["extra"] = " · ".join(parts)
    elif game.prefix is not None:
        out["runner"] = "Proton" if game.prefix_manager == "Proton" else "Wine"
        out["last_used"] = _prefix_proton_version(game.prefix)
    return out


def game_files(game, max_entries: int = 30_000, max_depth: int = 4) -> Dict[str, list]:
    """One bounded walk over the game's folders: executable names and DXVK / d3d logs."""
    exes: List[str] = []
    logs: List[Path] = []
    count = 0
    for top in game.folders:
        if not top.is_dir():
            continue
        base = len(top.parts)
        for dp, dns, fns in os.walk(top, followlinks=False):
            count += len(dns) + len(fns)
            if count > max_entries:
                dns[:] = []
                break
            depth = len(Path(dp).parts) - base
            dns[:] = [d for d in dns if d.lower() not in _SKIP and depth < max_depth]
            for f in fns:
                low = f.lower()
                if low.endswith(".exe") and not low.startswith(("unins", "crashreport", "vc_redist",
                                                                 "dxsetup", "ue4prereq", "dotnet")):
                    stem = f[:-4]
                    if stem not in exes:
                        exes.append(stem)
                elif low.endswith(".log") and (_DXVK_LOG.search(f) or "vkd3d" in low):
                    logs.append(Path(dp) / f)
    return {"exes": exes, "dxvk_logs": logs}


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def find_logs(game, dirs: Iterable[Path], dxvk_logs: Iterable[Path] = (),
              exe_stems: Iterable[str] = ()) -> List[Path]:
    """Proton / DXVK / vkd3d logs of this game, newest first."""
    found = {p for p in dxvk_logs if p.is_file()}
    appid = steam_appid(game)
    stems = {s.casefold() for s in exe_stems if s}
    for d in dirs:
        try:
            if appid:
                p = Path(d) / f"steam-{appid}.log"
                if p.is_file():
                    found.add(p)
            if stems:
                for p in Path(d).glob("*.log"):
                    m = _DXVK_LOG.search(p.name)
                    if m and p.name[:m.start()].casefold() in stems:
                        found.add(p)
        except OSError:
            continue
    return sorted(found, key=_mtime, reverse=True)


def mangohud_output_dirs(env: Optional[Dict[str, str]] = None, home: Optional[Path] = None) -> List[Path]:
    """Folders MangoHud writes its CSV logs to (MANGOHUD_CONFIG, MangoHud.conf)."""
    h = Path(home) if home else Path.home()
    out: List[Path] = []

    def add(v: str):
        v = v.strip().strip("'\"")
        if v:
            p = Path(os.path.expanduser(v.replace("$HOME", str(h))))
            if p not in out:
                out.append(p)
    for blob in ((env or {}).get("MANGOHUD_CONFIG", ""), os.environ.get("MANGOHUD_CONFIG", "")):
        for part in blob.split(","):
            if part.strip().startswith("output_folder="):
                add(part.split("=", 1)[1])
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or h / ".config")
    for f in (cfg / "MangoHud" / "MangoHud.conf",
              h / ".var/app/com.valvesoftware.Steam/config/MangoHud/MangoHud.conf"):
        try:
            for ln in f.read_text(errors="replace").splitlines():
                ln = ln.strip()
                if ln.startswith("output_folder") and "=" in ln:
                    add(ln.split("=", 1)[1])
        except OSError:
            continue
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.casefold())


def find_frametime_logs(game, dirs: Iterable[Path], exe_stems: Iterable[str] = ()) -> List[Path]:
    """MangoHud CSV captures whose file name starts with one of the game's executables
    (MangoHud names them <process>_<date>.csv) or its title; newest first."""
    keys = {_norm(s) for s in exe_stems if len(_norm(s)) >= 3}
    nm = _norm(game.name)
    if len(nm) >= 4:
        keys.add(nm)
    if not keys:
        return []
    found = set()
    for d in dirs:
        try:
            for p in Path(d).glob("*.csv"):
                low = p.name.lower()
                if low.endswith("_summary.csv"):
                    continue
                head = _norm(re.split(r"_\d{4}-\d{2}-\d{2}", p.stem)[0])
                if head and any(head == k or head.startswith(k) for k in keys):
                    found.add(p)
        except OSError:
            continue
    return sorted(found, key=_mtime, reverse=True)


def read_tail(path: Path, max_bytes: int = 2_000_000) -> List[str]:
    """Last max_bytes of a text file as lines (the first, possibly cut, line dropped)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if size > max_bytes and lines else lines
