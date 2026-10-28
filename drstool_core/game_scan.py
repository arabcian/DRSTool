"""Find installed games and the upgradable NVIDIA DLLs inside them.

Launchers covered:
    Steam    every library folder; Proton prefix = compatdata/<appid>/pfx
    Lutris   pga.db (names, install folders) + games/*.yml (exe, prefix)
    Heroic   Epic (legendary), GOG, Amazon (nile) and side-loaded games
    Bottles  one entry per bottle (its external programs as folders)
    Custom   folders the user adds (a game folder or a Wine prefix)

Nothing here writes anything. Every launcher is read best-effort: a missing
or malformed file of one launcher never hides the games of another; the
problem is reported in ScanResult.errors instead.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

try:
    import yaml  # type: ignore
except ImportError:          # PyYAML is optional; a regex fallback covers the keys we need
    yaml = None

# libyaml-backed loader is ~8x faster than the pure-Python SafeLoader (same safe semantics)
_YAML_LOADER = getattr(yaml, "CSafeLoader", getattr(yaml, "SafeLoader", None))

from . import pe_version

# NGX feature id -> DLL file name the game ships (Streamline is a group of sl.*.dll)
DLSS_FILES = {"dlss": "nvngx_dlss.dll", "dlssd": "nvngx_dlssd.dll", "dlssg": "nvngx_dlssg.dll"}
SL_FEATURE = "sl_sdk_0"
SL_ANCHOR = "sl.interposer.dll"
# NGX's Streamline package ships the plugins but no interposer, so the group's version is read
# from a plugin every install has — an older interposer would otherwise stay "outdated" for good.
SL_VERSION_DLL = "sl.common.dll"
KIND_TITLES = {"dlss": "DLSS SR", "dlssd": "DLSS RR", "dlssg": "DLSS FG", SL_FEATURE: "Streamline"}
KINDS = ("dlss", "dlssd", "dlssg", SL_FEATURE)

_ANTI_CHEAT = ("easyanticheat", "battleye", "easyanticheat_eos", "beservice", "vanguard",
               "xigncode", "nprotect", "gameguard", "ricochet", "eaanticheat")
_SKIP_DIRS = {"shadercache", "compatdata", "__pycache__", ".git", "$recycle.bin",
              "steamapps", "dosdevices", ".drstool-backup", ".drstool-dll-backup"}

MAX_DEPTH = 9
MAX_ENTRIES = 250_000


@dataclass
class GameEntry:
    launcher: str                      # Steam | Lutris | Heroic | Bottles | Custom
    key: str                           # stable id, e.g. "steam:1091500"
    name: str
    folders: List[Path] = field(default_factory=list)   # where the game's own files live
    prefix: Optional[Path] = None      # Wine prefix (contains drive_c/)
    prefix_manager: str = ""           # "Proton" / "Bottles" when something else keeps it in sync
    note: str = ""
    launch_id: str = ""                # Lutris: slug · Heroic: "<runner>/<appName>" (for launch URIs)

    @property
    def label(self) -> str:
        return f"{self.name}  [{self.launcher}]"


@dataclass
class FoundDll:
    kind: str                          # dlss | dlssd | dlssg | sl_sdk_0
    path: Path                         # the DLL (for Streamline: sl.interposer.dll)
    version: tuple                     # (major, minor, build, rev) or ()
    group: List[Path] = field(default_factory=list)   # Streamline: every sl.*.dll next to it

    @property
    def version_str(self) -> str:
        return pe_version.version_str(self.version) or "?"


@dataclass
class GameDlls:
    dlls: List[FoundDll] = field(default_factory=list)
    anti_cheat: List[str] = field(default_factory=list)
    truncated: bool = False

    def of(self, kind: str) -> List[FoundDll]:
        return [d for d in self.dlls if d.kind == kind]


@dataclass
class ScanResult:
    games: List[GameEntry] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _home(home: Optional[Path]) -> Path:
    return Path(home) if home else Path.home()


def _xdg(env: str, default: Path, home: Path) -> Path:
    v = os.environ.get(env)
    if v and home == Path.home():
        return Path(v)
    return default


def _is_prefix(p: Path) -> bool:
    try:
        return (p / "drive_c" / "windows").is_dir()
    except OSError:
        return False


def _vdf_unesc(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


# ── Steam ───────────────────────────────────────────────────────────────────

def steam_roots(home: Optional[Path] = None) -> List[Path]:
    h = _home(home)
    cands = [h / ".local/share/Steam", h / ".steam/steam", h / ".steam/root",
             h / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
             h / ".var/app/com.valvesoftware.Steam/data/Steam"]
    seen, out = set(), []
    for c in cands:
        try:
            r = c.resolve()
            if (r / "steamapps").is_dir() and r not in seen:
                seen.add(r)
                out.append(r)
        except OSError:
            continue
    return out


def steam_libraries(root: Path) -> List[Path]:
    libs = [root / "steamapps"]
    try:
        text = (root / "steamapps" / "libraryfolders.vdf").read_text(errors="replace")
        for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
            p = Path(_vdf_unesc(m.group(1))) / "steamapps"
            if p not in libs:
                libs.append(p)
    except OSError:
        pass
    return [lib for lib in libs if lib.is_dir()]


_STEAM_TOOLS = re.compile(r"^(Proton\b|Steam Linux Runtime|Steamworks Common|SteamVR\b)", re.I)


def scan_steam(home: Optional[Path] = None) -> List[GameEntry]:
    out, seen = [], set()
    for root in steam_roots(home):
        for lib in steam_libraries(root):
            for acf in sorted(lib.glob("appmanifest_*.acf")):
                try:
                    t = acf.read_text(errors="replace")
                except OSError:
                    continue
                a = re.search(r'"appid"\s+"(\d+)"', t)
                n = re.search(r'"name"\s+"((?:[^"\\]|\\.)*)"', t)
                d = re.search(r'"installdir"\s+"((?:[^"\\]|\\.)*)"', t)
                if not (a and d) or a.group(1) in seen:
                    continue
                name = _vdf_unesc(n.group(1)) if n else a.group(1)
                if _STEAM_TOOLS.match(name):
                    continue
                folder = lib / "common" / _vdf_unesc(d.group(1))
                if not folder.is_dir():
                    continue
                seen.add(a.group(1))
                pfx = lib / "compatdata" / a.group(1) / "pfx"
                out.append(GameEntry("Steam", f"steam:{a.group(1)}", name, [folder],
                                     pfx if _is_prefix(pfx) else None,
                                     "Proton" if _is_prefix(pfx) else ""))
    return out


# ── Lutris ──────────────────────────────────────────────────────────────────

def lutris_dirs(home: Optional[Path] = None) -> List[Path]:
    h = _home(home)
    data = _xdg("XDG_DATA_HOME", h / ".local/share", h) / "lutris"
    cands = [data, h / ".var/app/net.lutris.Lutris/data/lutris", h / ".config/lutris"]
    return [c for c in cands if c.is_dir()]


def _yaml_game_section(text: str) -> Dict[str, str]:
    if yaml is not None:
        try:
            d = yaml.load(text, Loader=_YAML_LOADER) or {}
            g = d.get("game") if isinstance(d, dict) else None
            return {str(k): str(v) for k, v in g.items()} if isinstance(g, dict) else {}
        except Exception:
            pass
    out, inside = {}, False
    for line in text.splitlines():
        if re.match(r"^game:\s*$", line):
            inside = True
            continue
        if inside and re.match(r"^\S", line):
            break
        m = re.match(r"^\s+(exe|prefix|working_dir|main_file|path):\s*(.*?)\s*$", line) if inside else None
        if m:
            out[m.group(1)] = m.group(2).strip("'\"")
    return out


def _game_root_from_exe(exe: Path) -> Path:
    """Unreal-style <Game>/<Project>/Binaries/Win64/x.exe -> <Game>; otherwise the exe's folder."""
    parts = exe.parent.parts
    for i in range(len(parts) - 1, 0, -1):
        if parts[i].lower() == "binaries" and i >= 2:
            return Path(*parts[:i - 1])
    return exe.parent


def scan_lutris(home: Optional[Path] = None) -> List[GameEntry]:
    out, seen = [], set()
    for base in lutris_dirs(home):
        games_dir = base / "games"
        rows = []
        db = base / "pga.db"
        if db.is_file():
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
                try:
                    cols = {r[1] for r in con.execute("PRAGMA table_info(games)")}
                    want = [c for c in ("slug", "name", "runner", "directory", "configpath", "installed")
                            if c in cols]
                    for r in con.execute(f"SELECT {', '.join(want)} FROM games"):
                        rows.append(dict(zip(want, r)))
                finally:
                    con.close()
            except sqlite3.Error:
                rows = []
        if not rows and games_dir.is_dir():
            rows = [{"slug": re.sub(r"-\d+$", "", p.stem), "name": re.sub(r"-\d+$", "", p.stem),
                     "configpath": p.stem, "installed": 1}
                    for p in sorted(games_dir.glob("*.yml"))]
        for r in rows:
            if r.get("installed") in (0, "0") or not r.get("configpath"):
                continue
            yml = games_dir / f"{r['configpath']}.yml"
            key = f"lutris:{r['configpath']}"
            if key in seen:
                continue
            g = {}
            try:
                g = _yaml_game_section(yml.read_text(errors="replace"))
            except OSError:
                pass
            folders: List[Path] = []
            directory = str(r.get("directory") or "").strip()
            if directory and Path(directory).is_dir():
                folders.append(Path(directory))
            exe = g.get("exe") or g.get("main_file") or ""
            if exe:
                ep = Path(os.path.expanduser(exe))
                if not ep.is_absolute() and folders:
                    ep = folders[0] / ep
                if ep.is_absolute():
                    root = _game_root_from_exe(ep)
                    if root.is_dir() and not any(_inside(f, root) for f in folders):
                        folders.append(root)
            wd = g.get("working_dir")
            if not folders and wd and Path(os.path.expanduser(wd)).is_dir():
                folders.append(Path(os.path.expanduser(wd)))
            pfx = Path(os.path.expanduser(g["prefix"])) if g.get("prefix") else None
            if pfx is not None and not _is_prefix(pfx):
                pfx = None
            # Prefix-only games keep their files inside the prefix
            folders = [f for f in folders if not (pfx and f == pfx)]
            if not folders and pfx is None:
                continue
            seen.add(key)
            out.append(GameEntry("Lutris", key, str(r.get("name") or r.get("slug") or r["configpath"]),
                                 folders, pfx, "", launch_id=str(r.get("slug") or "")))
    return out


def _inside(base: Path, p: Path) -> bool:
    try:
        return os.path.commonpath([str(base), str(p)]) == str(base)
    except ValueError:
        return False


# ── Heroic ──────────────────────────────────────────────────────────────────

def heroic_roots(home: Optional[Path] = None) -> List[Path]:
    h = _home(home)
    return [p for p in (h / ".config/heroic", h / ".var/app/com.heroicgameslauncher.hgl/config/heroic")
            if p.is_dir()]


def _json(path: Path):
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None


def _heroic_titles(root: Path) -> Dict[str, str]:
    titles: Dict[str, str] = {}

    def walk(o, depth=0):
        if depth > 6:
            return
        if isinstance(o, dict):
            if "app_name" in o and "title" in o:
                titles[str(o["app_name"])] = str(o["title"])
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)
    for f in ("legendary_library.json", "gog_library.json", "nile_library.json"):
        walk(_json(root / "store_cache" / f))
    return titles


def scan_heroic(home: Optional[Path] = None) -> List[GameEntry]:
    h = _home(home)
    out, seen = [], set()
    for root in heroic_roots(home):
        titles = _heroic_titles(root)
        found: List[tuple] = []          # (store, app, title, path)
        for f in (root / "legendaryConfig/legendary/installed.json", h / ".config/legendary/installed.json"):
            d = _json(f)
            if isinstance(d, dict):
                for app, info in d.items():
                    if isinstance(info, dict) and info.get("install_path"):
                        found.append(("Epic", app, info.get("title") or titles.get(app, app), info["install_path"]))
        d = _json(root / "gog_store/installed.json")
        for info in (d.get("installed", []) if isinstance(d, dict) else []):
            if isinstance(info, dict) and info.get("install_path"):
                app = str(info.get("appName", ""))
                found.append(("GOG", app, titles.get(app, app), info["install_path"]))
        d = _json(root / "nile_config/nile/installed.json")
        for info in (d if isinstance(d, list) else []):
            if isinstance(info, dict) and info.get("path"):
                app = str(info.get("id", ""))
                found.append(("Amazon", app, titles.get(app, app), info["path"]))
        d = _json(root / "sideload_apps/library.json")
        for g in (d.get("games", []) if isinstance(d, dict) else []):
            if not isinstance(g, dict):
                continue
            exe = ((g.get("install") or {}).get("executable") or "") if isinstance(g.get("install"), dict) else ""
            folder = g.get("folder_name") or (str(Path(exe).parent) if exe else "")
            if folder:
                found.append(("Sideload", str(g.get("app_name", "")), g.get("title") or "?", folder))
        for store, app, title, path in found:
            key = f"heroic:{app}"
            p = Path(os.path.expanduser(str(path)))
            if key in seen or not p.is_dir():
                continue
            seen.add(key)
            cfg = _json(root / "GamesConfig" / f"{app}.json")
            node = cfg.get(app, {}) if isinstance(cfg, dict) else {}
            pfx, manager = None, ""
            if isinstance(node, dict) and node.get("winePrefix"):
                cand = Path(os.path.expanduser(str(node["winePrefix"])))
                if _is_prefix(cand / "pfx"):          # Proton layout: <prefix>/pfx
                    cand = cand / "pfx"
                if _is_prefix(cand):
                    pfx = cand
                    wv = node.get("wineVersion") if isinstance(node.get("wineVersion"), dict) else {}
                    manager = "Proton" if str(wv.get("type", "")).lower() == "proton" else ""
            runner = {"Epic": "legendary", "GOG": "gog", "Amazon": "nile", "Sideload": "sideload"}[store]
            out.append(GameEntry("Heroic", key, str(title), [p], pfx, manager, store,
                                 launch_id=f"{runner}/{app}"))
    return out


# ── Bottles ─────────────────────────────────────────────────────────────────

def bottles_roots(home: Optional[Path] = None) -> List[Path]:
    h = _home(home)
    return [p for p in (h / ".local/share/bottles/bottles",
                        h / ".var/app/com.usebottles.bottles/data/bottles/bottles") if p.is_dir()]


def scan_bottles(home: Optional[Path] = None) -> List[GameEntry]:
    out = []
    for root in bottles_roots(home):
        try:
            bottles = sorted(d for d in root.iterdir() if (d / "bottle.yml").is_file())
        except OSError:
            continue
        for b in bottles:
            name, folders = b.name, []
            text = ""
            try:
                text = (b / "bottle.yml").read_text(errors="replace")
            except OSError:
                pass
            data = None
            if yaml is not None and text:
                try:
                    data = yaml.load(text, Loader=_YAML_LOADER)
                except Exception:
                    data = None
            if isinstance(data, dict):
                name = str(data.get("Name") or name)
                progs = data.get("External_Programs") or {}
                for prog in (progs.values() if isinstance(progs, dict) else []):
                    if isinstance(prog, dict) and prog.get("path"):
                        exe = Path(str(prog["path"]))
                        root_dir = _game_root_from_exe(exe)
                        if root_dir.is_dir() and root_dir not in folders:
                            folders.append(root_dir)
            else:
                m = re.search(r"^Name:\s*(.+)$", text, re.M)
                if m:
                    name = m.group(1).strip().strip("'\"")
            if not folders and (b / "drive_c").is_dir():
                folders = [b / "drive_c"]
            out.append(GameEntry("Bottles", f"bottles:{b.name}", name, folders,
                                 b if _is_prefix(b) else None, "Bottles",
                                 "Bottles installs DXVK/vkd3d per bottle; changing a component "
                                 "there can overwrite DLLs updated here."))
    return out


# ── Custom folders ──────────────────────────────────────────────────────────

def custom_entry(path: Path) -> Optional[GameEntry]:
    p = Path(os.path.expanduser(str(path)))
    if not p.is_dir():
        return None
    if _is_prefix(p):
        if p.name == "pfx":                 # Steam/umu Proton layout: <compatdata>/pfx
            return GameEntry("Custom", f"custom:{p}", p.parent.name, [p / "drive_c"], p, "Proton")
        return GameEntry("Custom", f"custom:{p}", p.name or str(p), [p / "drive_c"], p, "")
    return GameEntry("Custom", f"custom:{p}", p.name or str(p), [p], None, "")


# ── everything ──────────────────────────────────────────────────────────────

def scan_all(home: Optional[Path] = None, custom: Iterable[str] = ()) -> ScanResult:
    res = ScanResult()
    scanners = (("Steam", scan_steam), ("Lutris", scan_lutris),
                ("Heroic", scan_heroic), ("Bottles", scan_bottles))
    with ThreadPoolExecutor(max_workers=len(scanners)) as pool:   # mostly file I/O, releases the GIL
        futures = [(label, pool.submit(fn, home)) for label, fn in scanners]
        for label, fut in futures:       # collected in launcher order, so results stay deterministic
            try:
                res.games += fut.result()
            except Exception as ex:      # one broken launcher must not hide the others
                res.errors.append(f"{label}: {type(ex).__name__}: {ex}")
    keys = {g.key for g in res.games}
    for c in custom:
        e = custom_entry(Path(c))
        if e is None:
            res.errors.append(f"Custom folder not found: {c}")
        elif e.key not in keys:
            keys.add(e.key)
            res.games.append(e)
    res.games.sort(key=lambda g: (g.name.casefold(), g.launcher))
    return res


# ── DLLs inside a game ──────────────────────────────────────────────────────

def find_dlls(game: GameEntry, max_entries: int = MAX_ENTRIES) -> GameDlls:
    """DLSS / Streamline DLLs under the game's folders (symlinks are not followed)."""
    res = GameDlls()
    seen_files = set()
    count = 0
    for top in game.folders:
        if not top.is_dir():
            continue
        top_depth = len(top.parts)
        for dp, dns, fns in os.walk(top, followlinks=False):
            count += len(dns) + len(fns)
            if count > max_entries:
                res.truncated = True
                dns[:] = []
                break
            depth = len(Path(dp).parts) - top_depth
            in_drive_c = Path(dp).name.lower() == "drive_c"
            dns[:] = [d for d in dns if d.lower() not in _SKIP_DIRS and depth < MAX_DEPTH
                      and not (in_drive_c and d.lower() == "windows")
                      and not os.path.islink(os.path.join(dp, d))]
            for d in dns:
                if d.lower() in _ANTI_CHEAT and d not in res.anti_cheat:
                    res.anti_cheat.append(d)
            low = {f.lower(): f for f in fns}
            for f in fns:
                stem = f.lower().rsplit(".", 1)[0]
                if stem in _ANTI_CHEAT and f not in res.anti_cheat:
                    res.anti_cheat.append(f)
            for kind, fname in DLSS_FILES.items():
                real = low.get(fname)
                if real:
                    p = Path(dp) / real
                    if p.is_symlink() or p in seen_files:
                        continue
                    seen_files.add(p)
                    res.dlls.append(FoundDll(kind, p, pe_version.read_version(p) or ()))
            real = low.get(SL_ANCHOR)
            if real:
                p = Path(dp) / real
                if not p.is_symlink() and p not in seen_files:
                    seen_files.add(p)
                    group = sorted(Path(dp) / f for f in fns
                                   if f.lower().startswith("sl.") and f.lower().endswith(".dll")
                                   and not os.path.islink(os.path.join(dp, f)))
                    common = low.get(SL_VERSION_DLL)
                    ver = pe_version.read_version(Path(dp) / common) if common else None
                    res.dlls.append(FoundDll(SL_FEATURE, p, ver or pe_version.read_version(p) or (), group))
        if res.truncated:
            break
    res.dlls.sort(key=lambda d: (KINDS.index(d.kind), str(d.path)))
    return res
