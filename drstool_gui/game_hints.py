"""Game hints: Steam appid lookup, ProtonDB / PCGamingWiki hints and the ProtonDB report index."""

import codecs as _codecs
import json
import os
import re
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .catalog_data import ALL_ENV_VARS
from .launchers import BottlesBackend, HeroicBackend, SteamBackend
from .steam_utils import steam_compat_tools, steam_install_dirs
from .sysinfo import SYSHUB
from .tasks import _NAV, TASKS
from .theme import T
from .tool_ui import _tool_btn
from .widgets import (
    _COMBO_QSS, _desc_label, _LAUNCHER_QSS, _LINEEDIT_QSS, _MONO_BOX_QSS, _table_qss,
    _title_label, _xdg,
)


# ----------------------------------------------------------------------------
# Game hints: ProtonDB summary + PCGamingWiki API facts (read-only, cached)
# ----------------------------------------------------------------------------

def _http_get(url: str, timeout: float = 12.0, limit: int = 2_000_000):
    req = urllib.request.Request(url, headers={
        "User-Agent": "DRSTool/1.0 (+https://github.com/arabcian/DRSTool; game hint lookup) python-urllib",
        "Accept": "application/json, text/plain;q=0.9, */*;q=0.5", "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit), r.geturl()


def _hints_cache_path(appid: str) -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "drstool" / "hints" / f"{appid}.json"


def _pcgw_param(raw: str, name: str) -> str:
    m = re.search(r"\|\s*" + re.escape(name) + r"\s*=\s*([^\n|}]*)", raw, re.I)
    return m.group(1).strip() if m else ""


def _fetch_game_hints(appid: str, use_cache: bool = True) -> dict:
    appid = str(appid).strip()
    if not re.fullmatch(r"\d{1,10}", appid):      # also used as a cache file name and in URLs
        raise ValueError(f"not a Steam appid: {appid!r}")
    cp = _hints_cache_path(appid)
    if use_cache:
        try:
            d = json.loads(cp.read_text())
            if d.get("v") == _HINT_CACHE_VER and time.time() - d.get("ts", 0) < 7 * 86400:
                d["cached"] = True
                return d
        except (OSError, ValueError):
            pass
    res = {"v": _HINT_CACHE_VER, "appid": appid, "ts": time.time(), "protondb": None, "pcgw": None,
           "reports": None, "errors": []}
    try:
        body, _u = _http_get(f"https://www.protondb.com/api/v1/reports/summaries/{appid}.json")
        j = json.loads(body.decode("utf-8", "replace"))
        res["protondb"] = {k: j.get(k) for k in ("tier", "bestReportedTier", "trendingTier",
                                                "score", "confidence", "total")}
    except Exception as ex:
        res["errors"].append(f"ProtonDB: {ex}")
    try:
        api = "https://www.pcgamingwiki.com/w/api.php"
        title = ""
        try:        # 1) Cargo: Steam appid -> page name (bot-friendly API endpoint)
            q = urllib.parse.urlencode({"action": "cargoquery", "format": "json", "tables": "Infobox_game",
                                        "fields": "_pageName=Page", "limit": "1",
                                        "where": f'Steam_AppID HOLDS "{appid}"'})
            j = json.loads(_http_get(f"{api}?{q}")[0].decode("utf-8", "replace"))
            title = j["cargoquery"][0]["title"]["Page"]
        except Exception as ex1:
            res["errors"].append(f"PCGamingWiki (cargo lookup): {ex1}")
        if not title:   # 2) documented redirect helper
            _b, final = _http_get(f"https://www.pcgamingwiki.com/api/appid.php?appid={appid}")
            title = urllib.parse.unquote(final.rstrip("/").rsplit("/", 1)[-1])
        final = "https://www.pcgamingwiki.com/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
        # 3) wikitext through the MediaWiki API (not index.php?action=raw)
        q = urllib.parse.urlencode({"action": "query", "prop": "revisions", "rvprop": "content",
                                    "rvslots": "main", "format": "json", "formatversion": "2", "titles": title})
        j = json.loads(_http_get(f"{api}?{q}")[0].decode("utf-8", "replace"))
        txt = j["query"]["pages"][0]["revisions"][0]["slots"]["main"]["content"]
        res["errors"] = [e for e in res["errors"] if not e.startswith("PCGamingWiki (cargo")]
        nums = lambda s: re.findall(r"\d+(?:\.\d+)?", s)
        res["pcgw"] = {"title": title.replace("_", " "), "url": final,
                       "engines": [e.strip() for e in re.findall(r"Infobox game/row/engine\|([^}|]+)", txt)],
                       "d3d": nums(_pcgw_param(txt, "direct3d versions")),
                       "vulkan": nums(_pcgw_param(txt, "vulkan versions")),
                       "opengl": nums(_pcgw_param(txt, "opengl versions"))}
    except Exception as ex:
        res["errors"].append(f"PCGamingWiki: {ex}")
    if res["protondb"] or res["pcgw"]:
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(res))
        except OSError:
            pass
    return res


_HINT_CACHE_VER = 4


def fetch_game_hints(appid: str, use_cache: bool = True) -> dict:
    d = _fetch_game_hints(appid, use_cache)
    d["reports"] = reports_for_app(appid)          # from the local ProtonDB dump index (never cached with the rest)
    return d
_TIER_RANK = {"borked": 0, "bronze": 1, "silver": 2, "gold": 3, "platinum": 4}
_COMMUNITY_SKIP = ("DXVK_HUD", "MANGOHUD", "PROTON_LOG", "WINEDEBUG", "DXVK_LOG", "VKD3D_DEBUG",
                   "VKD3D_LOG", "WINE_LOG")
_ENV_RX = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]{2,})=(\"[^\"]*\"|'[^']*'|[^\s\"']+)")


def _walk_kv(o, out: list, key: str = "", depth: int = 0):
    if depth > 6:
        return
    if isinstance(o, str):
        out.append((key, o))
    elif isinstance(o, dict):
        for k, v in o.items():
            _walk_kv(v, out, str(k), depth + 1)
    elif isinstance(o, (list, tuple)):
        for v in o:
            _walk_kv(v, out, key, depth + 1)


_KEY_NORM: Dict[str, str] = {}      # report keys repeat across millions of reports; normalise each once


def _norm_key(k) -> str:
    n = _KEY_NORM.get(k) if isinstance(k, str) else None
    if n is None:
        n = str(k).lower().replace("_", "").replace("-", "")
        if isinstance(k, str) and len(_KEY_NORM) < 20000:
            _KEY_NORM[k] = n
    return n


def _contains_eq(o) -> bool:
    """True when any key or string value holds '=' (what `"=" in json.dumps(o)` answered, without building the text)."""
    stack = [o]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            if "=" in x:
                return True
        elif isinstance(x, dict):
            for k, v in x.items():
                if "=" in k:
                    return True
                stack.append(v)
        elif isinstance(x, list):
            stack.extend(x)
    return False


def _find_key(o, names: set, depth: int = 0):
    """First scalar whose key (lower-cased, no '_' / '-') is in names — the mirror's schema is not guaranteed."""
    if depth > 4:
        return None
    if isinstance(o, dict):
        for k, v in o.items():
            if _norm_key(k) in names and isinstance(v, (str, int, float)) \
                    and not isinstance(v, bool) and str(v).strip():
                return str(v).strip()
        for v in o.values():
            r = _find_key(v, names, depth + 1)
            if r:
                return r
    elif isinstance(o, (list, tuple)):
        for v in o[:6]:
            r = _find_key(v, names, depth + 1)
            if r:
                return r
    return None


_NV_RX = re.compile(r"nvidia|geforce|\brtx\b|\bgtx\b", re.I)
_AMD_RX = re.compile(r"\bamd\b|radeon|\brx\s?\d|\bnavi|\brdna|\bvega", re.I)
_INTEL_RX = re.compile(r"\barc\b|\biris\b|\buhd\b|intel.*graphics", re.I)


def _vendor_of(gpu: str, blob: str) -> str:
    if gpu:
        return "nvidia" if _NV_RX.search(gpu) else "amd" if _AMD_RX.search(gpu) else \
            "intel" if _INTEL_RX.search(gpu) else ""
    return "nvidia" if _NV_RX.search(blob) else ""


def _parse_ts(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    try:
        x = float(v)
        return x / 1000 if x > 1e12 else x
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_F_GPU = {"gpu", "gpumodel", "gpuinfo", "graphicscard", "videocard"}
_F_DRV = {"gpudriver", "driver", "graphicsdriver", "driverversion", "drivername"}
_F_OS = {"os", "distro", "operatingsystem", "osname"}
_F_KERN = {"kernel", "kernelversion"}
_F_CPU = {"cpu", "processor"}
_F_RAM = {"ram", "memory"}
_F_PROTON = {"protonversion", "proton"}
_F_TS = {"timestamp", "time", "date", "created", "createdat", "reportdate", "submitted", "submittedat"}
_F_VERDICT = {"rating", "verdict", "tier", "protondbrating"}


def parse_community_reports(j, names: set, cap: int = 600) -> dict:
    """Per-report records (hardware, Proton, date, env assignments). Untrusted data: only KEY=VALUE tokens with
    a plausible variable name and a harmless-looking value are kept; nothing is ever applied automatically."""
    reps = j if isinstance(j, list) else ((j.get("reports") or []) if isinstance(j, dict) else [])
    items: list = []
    protons: Dict[str, int] = {}
    agg: Dict[tuple, int] = {}
    total = 0
    keys: list = []
    for r in reps:
        if not isinstance(r, dict):
            continue
        total += 1
        if not keys:
            keys = sorted(r.keys())[:24]
        kv: list = []
        _walk_kv(r, kv)
        envs, seen = [], set()
        for where_key, text in kv:
            for m in _ENV_RX.finditer(text):
                k, v = m.group(1), m.group(2).strip("\"'")
                if k.startswith(_COMMUNITY_SKIP) or (k, v) in seen:
                    continue
                if k not in names and not re.fullmatch(r"[A-Z_][A-Z0-9_]{2,}", k):
                    continue
                if not re.fullmatch(r"[A-Za-z0-9_.,:;=+-]{1,64}", v):
                    continue
                seen.add((k, v))
                envs.append([k, v, "launch options" if "launch" in where_key.lower() else "notes"])
        pv = _find_key(r, {"customprotonversion"}) or _find_key(r, _F_PROTON)
        if pv and len(pv) < 40:
            protons[pv] = protons.get(pv, 0) + 1
        if not envs:
            continue
        for k, v, _w in envs:
            agg[(k, v)] = agg.get((k, v), 0) + 1
        gpu = _find_key(r, _F_GPU) or ""
        blob = " ".join(t for _k, t in kv)
        sysinfo = ""
        if not gpu:
            m = re.search(r"[^\n|;]{0,60}(?:nvidia|geforce|radeon)[^\n|;]{0,90}", blob, re.I)
            sysinfo = m.group(0).strip() if m else ""
        app = ((r.get("app") or {}).get("steam") or {}).get("appId") if isinstance(r.get("app"), dict) else None
        items.append({"app": str(app or r.get("appId") or ""), "t": _parse_ts(_find_key(r, _F_TS)), "gpu": gpu[:80], "drv": (_find_key(r, _F_DRV) or "")[:40],
                      "os": (_find_key(r, _F_OS) or "")[:60], "kern": (_find_key(r, _F_KERN) or "")[:40],
                      "cpu": (_find_key(r, _F_CPU) or "")[:70], "ram": (_find_key(r, _F_RAM) or "")[:20],
                      "proton": (pv or "")[:40], "verdict": (_find_key(r, _F_VERDICT) or "")[:20],
                      "vendor": _vendor_of(gpu, blob), "sys": sysinfo[:120], "envs": envs[:12]})
    items.sort(key=lambda x: -(x["t"] or 0))
    return {"n": total, "n_env": len(items), "items": items[:cap], "keys": keys,
            "envs": [[k, v, c, 0] for (k, v), c in sorted(agg.items(), key=lambda kv: -kv[1])[:20] if c >= 2],
            "proton": sorted(protons.items(), key=lambda kv: -kv[1])[:5]}


# ---- ProtonDB report data: monthly public dump (github.com/bdefore/protondb-data, ODbL) -> local env index ----

PROTONDB_DUMP_BASE = "https://raw.githubusercontent.com/bdefore/protondb-data/master/reports/"
_IDX_MONTHS = 12                      # reports older than this are not kept in the index
_IDX_MEM: dict = {"mtime": None, "data": None}


def _index_path() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "drstool" / "protondb_env_index.json"


class _CountingReader:
    def __init__(self, f, total: int, cb):
        self._f, self._total, self._cb, self._n, self._last = f, total, cb, 0, 0.0

    def read(self, n=-1):
        b = self._f.read(n)
        self._n += len(b)
        now = time.time()
        if self._cb and now - self._last > 0.3:
            self._last = now
            self._cb(self._n, self._total)
        return b


def _iter_json_array(f):
    """Stream the objects of a huge top-level JSON array without loading it all into memory."""
    dec, inc, buf = json.JSONDecoder(), _codecs.getincrementaldecoder("utf-8")("replace"), ""
    while True:
        chunk = f.read(1 << 22)
        buf += inc.decode(chunk, final=not chunk)
        i, n = 0, len(buf)
        while True:
            while i < n and buf[i] in " \r\n\t,[":
                i += 1
            if i >= n or buf[i] == "]":
                break
            try:
                obj, j = dec.raw_decode(buf, i)
            except ValueError:
                break                                   # object continues in the next chunk
            yield obj
            i = j
        buf = buf[i:]
        if not chunk:
            return


def _dump_candidates(months: int = 9) -> List[str]:
    mon = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    y, m = datetime.now().year, datetime.now().month
    out = []
    for _ in range(months):
        out.append(f"reports_{mon[m - 1]}1_{y}.tar.gz")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def find_latest_dump() -> str:
    for name in _dump_candidates():
        try:
            req = urllib.request.Request(PROTONDB_DUMP_BASE + name, method="HEAD",
                                         headers={"User-Agent": "DRSTool/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    return name
        except Exception:
            continue
    raise RuntimeError("no ProtonDB dump found in the last 9 months at " + PROTONDB_DUMP_BASE)


def build_env_index(stream, dump_name: str, progress=None) -> dict:
    """Read a ProtonDB dump (tar.gz stream) and keep, per Steam appid, the reports of the last
    _IDX_MONTHS months that mention environment-variable assignments. Written to the cache."""
    cutoff = time.time() - _IDX_MONTHS * 30.44 * 86400
    names = {e.name for e in ALL_ENV_VARS}
    by_app: Dict[str, list] = {}
    scanned = kept = 0
    batch: list = []

    def flush():
        nonlocal kept
        if not batch:
            return
        for it in parse_community_reports(batch, names, cap=10 ** 9)["items"]:
            if it["app"]:
                by_app.setdefault(it["app"], []).append(it)
                kept += 1
        batch.clear()

    with tarfile.open(fileobj=stream, mode="r|gz") as tf:
        for m in tf:
            if not (m.isfile() and m.name.endswith(".json")):
                continue
            for r in _iter_json_array(tf.extractfile(m)):
                scanned += 1
                # timestamp may be a number or an ISO string depending on the dump —
                # comparing a str with a float raised TypeError and aborted the build.
                if not isinstance(r, dict) or (_parse_ts(str(r.get("timestamp") or "")) or 0) < cutoff:
                    continue
                resp = r.get("responses")
                if isinstance(resp, dict) and _contains_eq(resp):
                    batch.append(r)
                    if len(batch) >= 5000:
                        flush()
            break
    flush()
    for lst in by_app.values():
        lst.sort(key=lambda x: -(x["t"] or 0))
    data = {"v": 1, "dump": dump_name, "built": time.time(), "scanned": scanned, "kept": kept, "by_app": by_app}
    p = _index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    os.replace(tmp, p)
    _IDX_MEM["mtime"] = None
    return data


def load_env_index() -> Optional[dict]:
    p = _index_path()
    try:
        mt = p.stat().st_mtime
        if _IDX_MEM["mtime"] != mt:
            _IDX_MEM["data"], _IDX_MEM["mtime"] = json.loads(p.read_text()), mt
        return _IDX_MEM["data"]
    except (OSError, ValueError):
        return None


def reports_for_app(appid: str) -> Optional[dict]:
    """Reports dict (same shape parse_community_reports returns) from the local index, or None."""
    idx = load_env_index()
    if not idx:
        return None
    items = idx["by_app"].get(str(appid), [])
    pv: Dict[str, int] = {}
    for it in items:
        if it.get("proton"):
            pv[it["proton"]] = pv.get(it["proton"], 0) + 1
    return {"n": len(items), "n_env": len(items), "items": items, "keys": [], "envs": [],
            "proton": sorted(pv.items(), key=lambda kv: -kv[1])[:5],
            "dump": idx.get("dump", ""), "built": idx.get("built", 0)}


class _IndexWorker(QThread):
    progress = Signal(str)
    done = Signal(str)                                  # "" = ok, else the error text

    def run(self):
        try:
            name = find_latest_dump()
            self.progress.emit(f"Downloading {name} …")
            req = urllib.request.Request(PROTONDB_DUMP_BASE + name, headers={"User-Agent": "DRSTool/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                total = int(r.headers.get("Content-Length") or 0)
                cr = _CountingReader(r, total, lambda n, t: self.progress.emit(
                    f"Reading {name}: {n / 1e6:.0f}" + (f" / {t / 1e6:.0f}" if t else "") + " MB …"))
                build_env_index(cr, name)
            self.done.emit("")
        except Exception as ex:
            self.done.emit(f"{type(ex).__name__}: {ex}")


def proton_fork_advice(pd: Optional[dict], installed: List[str]) -> List[str]:
    """Which Proton to try, from ProtonDB's overall and trending tier."""
    if not pd:
        return []
    tier, trend = str(pd.get("tier") or "").lower(), str(pd.get("trendingTier") or "").lower()
    r, t = _TIER_RANK.get(tier), _TIER_RANK.get(trend)
    out = []
    if r is None:
        out.append("ProtonDB has no usable rating yet — start with Proton Experimental, then GE-Proton.")
    elif r >= 3:
        out.append(f"ProtonDB {tier}: stock Valve Proton (latest / Experimental) should be fine — no fork needed.")
    elif r == 2:
        out.append("ProtonDB silver: try Proton Experimental or the latest GE-Proton before tuning env vars.")
    else:
        out.append(f"ProtonDB {tier}: try GE-Proton or Proton-CachyOS; if it used to work, try an older Valve "
                   "Proton (e.g. 9.0) — regressions are common.")
    if r is not None and t is not None and t > r:
        out.append(f"Trending tier ({trend}) is better than the overall tier: recent Proton builds improved it.")
    elif r is not None and t is not None and t < r:
        out.append(f"Trending tier ({trend}) is worse than the overall tier: a recent regression is likely — "
                   "consider the previous Proton build.")
    ins = [n for n in installed if re.search(r"ge-proton|cachyos|proton-em|experimental|proton_9|proton_10", n, re.I)]
    if ins:
        out.append("Installed here: " + ", ".join(ins[:6]) + "  (set per game on the Steam sync tab).")
    return out


def hint_suggestions(info: dict, si, cur_env: Dict[str, str]):
    """-> (rows[(VAR, value, why, source, preselected)], notes[str]). Variables must exist in the catalog."""
    names = {e.name for e in ALL_ENV_VARS}
    rows: list = []
    notes: List[str] = []

    def add(n, v, why, src, pre=True):
        if n not in names:
            return
        cur = cur_env.get(n)
        if cur == v:
            return                                   # already configured
        rows.append((n, v, why + (f"  (currently {cur})" if cur not in (None, "") else ""), src, pre))

    pc = info.get("pcgw") or {}
    d3d = {x.split(".")[0] for x in pc.get("d3d", [])}
    engines = " ".join(pc.get("engines", [])).lower()
    nv = bool(si.has_nvidia)
    if nv and d3d & {"11", "12"}:
        add("PROTON_ENABLE_NVAPI", "1", "Direct3D 11/12 title on NVIDIA: dxvk-nvapi exposes DLSS / Reflex", "heuristic")
        add("__GL_SHADER_DISK_CACHE_SKIP_CLEANUP", "1",
            "stops the NVIDIA driver from pruning its shader cache between runs (fewer repeat compile stutters)",
            "heuristic")
    if pc and not d3d and pc.get("opengl"):
        add("__GL_THREADED_OPTIMIZATIONS", "1", "OpenGL-only title: NVIDIA threaded optimization", "heuristic")
    # engine-based
    if "unreal engine" in engines:
        notes.append("Unreal Engine: first-run shader compilation stutter is normal — keep the shader caches "
                     "(Cache tab) instead of clearing them.")
    if "unity" in engines:
        notes.append("Unity: if the game crashes or stutters, try its renderer launch arguments "
                     "(-force-vulkan / -force-d3d11) — game arguments, not environment variables.")
    if (pc.get("vulkan") and not d3d) or re.search(r"source 2|idtech|godot|id tech", engines):
        notes.append("Native Vulkan/OpenGL engine: DXVK / vkd3d-proton variables do not apply to this game.")
    if d3d and max(int(x) for x in d3d if x.isdigit()) <= 9:
        notes.append("Direct3D 9-era engine: DXVK's d3d9 layer is used — per-game d3d9.* keys in dxvk.conf "
                     "(dxvk.conf tab) often matter more than environment variables.")
    # system-aware
    if si.ntsync_dev:
        add("PROTON_USE_NTSYNC", "1", "/dev/ntsync exists; needs a Proton build with NTSync (GE, CachyOS, recent Valve)",
            "system", False)
    gpus = list(getattr(si, "vk_gpus", []) or [])
    if any(g.is_nvidia for g in gpus) and any(not g.is_nvidia for g in gpus):
        add("__NV_PRIME_RENDER_OFFLOAD", "1", "hybrid graphics: render on the NVIDIA GPU", "system")
        add("__VK_LAYER_NV_optimus", "NVIDIA_only", "hybrid graphics: Vulkan picks the NVIDIA device", "system")
        add("__GLX_VENDOR_LIBRARY_NAME", "nvidia", "hybrid graphics: OpenGL/GLX on NVIDIA", "system")
    if str(si.session).lower() == "wayland":
        add("PROTON_ENABLE_WAYLAND", "1", "Wayland session; native Wayland driver is experimental (overlays / Steam "
            "Input may break)", "system", False)
    # ProtonDB tier -> Proton fork advice
    installed: List[str] = []
    for root in SteamBackend.roots()[:1]:
        installed = [n for n, _l in steam_compat_tools(root)]
    notes = proton_fork_advice(info.get("protondb"), installed) + notes
    rep = info.get("reports") or {}
    if rep.get("proton"):
        notes.append("Proton builds most used in reports: " +
                     ", ".join(f"{v} ×{c}" for v, c in rep["proton"][:4]))
    return rows, notes


def community_entries(info: dict, si, cur_env: Dict[str, str], months: int = 6,
                      nvidia_only: bool = True, catalog_only: bool = False):
    """-> (entries, stats). One entry per (report, variable): even a single mention is listed, together with the
    reporter's hardware / Proton / system so the user can judge whether it applies to them."""
    names = {e.name for e in ALL_ENV_VARS}
    items = (info.get("reports") or {}).get("items", [])
    cutoff = time.time() - months * 30.44 * 86400 if months else 0
    vendors_known = any(it.get("vendor") for it in items)
    sel = []
    for it in items:
        if it.get("t") and it["t"] < cutoff:
            continue
        if nvidia_only and vendors_known and it.get("vendor") != "nvidia":
            continue
        sel.append(it)
    count: Dict[tuple, int] = {}
    for it in sel:
        for k, v, _w in it["envs"]:
            count[(k, v)] = count.get((k, v), 0) + 1
    out = []
    for it in sel:
        for k, v, w in it["envs"]:
            known = k in names
            if catalog_only and not known:
                continue
            out.append({"name": k, "value": v, "seen": count[(k, v)], "where": w, "known": known,
                        "current": cur_env.get(k, ""), "item": it})
    out.sort(key=lambda e: (-e["seen"], -(e["item"].get("t") or 0)))
    return out[:300], {"total": len(items), "window": len(sel), "vendors_known": vendors_known}


_EDITION_RX = re.compile(r"\b(game of the year|goty|definitive|complete|deluxe|ultimate|enhanced|gold|"
                         r"anniversary|special|legendary|collector'?s|director'?s cut|edition|bundle|collection)\b", re.I)


def _norm_title(t: str) -> str:
    t = re.sub(r"[\(\[].*?[\)\]]", " ", t)
    t = re.sub(r"[™®©:–—\-_.,!?'\"]", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def steam_appid_search(name: str) -> List[dict]:
    """Steam store search by game name -> [{'id', 'name', 'score'}] best match first.
    Uses the public store search endpoint (no key); a game that is not on Steam simply gets no good match."""
    raw = name.strip()
    variants = [raw]
    stripped = re.sub(r"\s+", " ", _EDITION_RX.sub(" ", re.sub(r"[\(\[].*?[\)\]]", " ", raw))).strip(" -:")
    if stripped and stripped.lower() != raw.lower():
        variants.append(stripped)
    found: Dict[str, dict] = {}
    err = None
    for v in variants:
        try:
            q = urllib.parse.urlencode({"term": v, "l": "english", "cc": "US"})
            body, _u = _http_get(f"https://store.steampowered.com/api/storesearch/?{q}")
            for it in json.loads(body.decode("utf-8", "replace")).get("items", []):
                if it.get("type", "app") == "app" and it.get("id"):
                    found.setdefault(str(it["id"]), {"id": str(it["id"]), "name": str(it.get("name", ""))})
        except Exception as ex:
            err = ex
    if not found and err is not None:
        raise err
    ref = {_norm_title(raw), _norm_title(stripped)}
    import difflib as _d
    for it in found.values():
        n = _norm_title(it["name"])
        sc = max(_d.SequenceMatcher(None, n, r).ratio() for r in ref if r)
        if any(r and (r in n or n in r) for r in ref):
            sc = max(sc, 0.8)
        it["score"] = round(sc, 2)
    return sorted(found.values(), key=lambda x: -x["score"])


class _AppidWorker(QThread):
    done = Signal(list, str)

    def __init__(self, term: str, parent=None):
        super().__init__(parent)
        self._term = term

    def run(self):
        try:
            self.done.emit(steam_appid_search(self._term), "")
        except Exception as ex:
            self.done.emit([], str(ex))


def launcher_game_names(win) -> List[tuple]:
    """[(label, plain name)] of games known to Lutris / Heroic / Bottles (best effort)."""
    out, seen = [], set()
    try:
        for g in getattr(win, "_lutris_sync_widget", None)._games:
            nm = (g.game_name or "").strip()
            if nm and nm.lower() not in seen:
                seen.add(nm.lower())
                out.append((f"Lutris: {nm}", nm))
    except Exception:
        pass
    for be in (HeroicBackend(), BottlesBackend()):
        try:
            if be.unavailable():
                continue
            for e in be.scan(None):
                nm = re.sub(r"\s*\[.*?\]\s*$", "", e.title).strip()
                if nm and nm.lower() not in seen:
                    seen.add(nm.lower())
                    out.append((f"{be.name}: {nm}", nm))
        except Exception:
            continue
    return out


class _HintWorker(QThread):
    done = Signal(dict)

    def __init__(self, appid: str, force: bool, parent=None):
        super().__init__(parent)
        self._appid, self._force = appid, force

    def run(self):
        # An exception here used to end the thread without `done`, leaving the tab busy forever.
        try:
            res = fetch_game_hints(self._appid, not self._force)
        except Exception as ex:                       # noqa: BLE001
            res = {"v": _HINT_CACHE_VER, "appid": str(self._appid), "ts": time.time(), "protondb": None,
                   "pcgw": None, "reports": None, "errors": [str(ex)]}
        self.done.emit(res)


class GameHintsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: Optional[_HintWorker] = None
        self._info: dict = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("Game hints (ProtonDB · PCGamingWiki)"))
        lay.addWidget(_desc_label(
            "Looks up a Steam appid: ProtonDB rating and PCGamingWiki's graphics-API facts, cached for a week. "
            "Suggestions come from (1) simple heuristics on those facts and on this system, (2) the engine, "
            "(3) the ProtonDB tier (which Proton build to try). Settings that several ProtonDB user reports mention "
            "are kept apart on the “Community tips” tab. Not on Steam? Find the game's appid by name below."))
        row = QHBoxLayout()
        self._games = QComboBox()
        self._games.setStyleSheet(_COMBO_QSS)
        self._games.addItem("Installed Steam game…", None)
        for label, _p, aid in steam_install_dirs():
            self._games.addItem(label, aid)
        self._games.currentIndexChanged.connect(
            lambda _i: self._games.currentData() and self._appid.setText(self._games.currentData()))
        row.addWidget(self._games, 1)
        self._appid = QLineEdit()
        self._appid.setPlaceholderText("Steam appid")
        self._appid.setMaximumWidth(140)
        self._appid.setStyleSheet(_LINEEDIT_QSS)
        self._appid.returnPressed.connect(lambda: self._fetch(False))
        row.addWidget(self._appid)
        self._go = _tool_btn("Look up", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        self._go.clicked.connect(lambda: self._fetch(False))
        row.addWidget(self._go)
        rf = _tool_btn("Refresh")
        rf.clicked.connect(lambda: self._fetch(True))
        row.addWidget(rf)
        lay.addLayout(row)

        # appid finder for games that are not on Steam's library (Lutris / Heroic / Bottles / manual)
        frow = QHBoxLayout()
        self._launcher_games = QComboBox()
        self._launcher_games.setStyleSheet(_COMBO_QSS)
        self._launcher_games.setToolTip("Games known to Lutris / Heroic / Bottles")
        self._launcher_games.currentIndexChanged.connect(self._pick_launcher_game)
        frow.addWidget(self._launcher_games, 1)
        self._find_name = QLineEdit()
        self._find_name.setPlaceholderText("Game name → find its Steam appid (for games not added through Steam)")
        self._find_name.setStyleSheet(_LINEEDIT_QSS)
        self._find_name.returnPressed.connect(self._find_appid)
        frow.addWidget(self._find_name, 2)
        self._find_btn = _tool_btn("Find appid")
        self._find_btn.clicked.connect(self._find_appid)
        frow.addWidget(self._find_btn)
        lay.addLayout(frow)
        self._matches = QListWidget()
        self._matches.setStyleSheet(_LAUNCHER_QSS)
        self._matches.setMaximumHeight(110)
        self._matches.hide()
        self._matches.itemDoubleClicked.connect(lambda _i: self._use_match(False))
        lay.addWidget(self._matches)
        mrow = QHBoxLayout()
        self._use_btn = _tool_btn("Use selected appid")
        self._use_btn.clicked.connect(lambda: self._use_match(False))
        self._umu_btn = _tool_btn("Use + set as umu GAMEID")
        self._umu_btn.setToolTip("Fills the Launch line tab's GAMEID with umu-<appid> so umu applies its protonfixes")
        self._umu_btn.clicked.connect(lambda: self._use_match(True))
        for b_ in (self._use_btn, self._umu_btn):
            b_.hide()
            mrow.addWidget(b_)
        mrow.addStretch(1)
        lay.addLayout(mrow)
        self._summary = QPlainTextEdit()
        self._summary.setReadOnly(True)
        self._summary.setStyleSheet(_MONO_BOX_QSS)
        self._summary.setMaximumHeight(230)
        lay.addWidget(self._summary)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Variable", "Value", "Source", "Why"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.MultiSelection)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self._table.setStyleSheet(_table_qss())
        lay.addWidget(self._table, 1)
        btns = QHBoxLayout()
        ctb = _tool_btn("Community tips ⚠", bg="#33270f", border=T["warn"], fg="#f0d79a")
        ctb.setToolTip("User-reported settings from ProtonDB — separate tab, use at your own risk")
        ctb.clicked.connect(lambda: self.window()._extra_tools.show_tab("Community tips"))
        btns.addWidget(ctb)
        self._apply = _tool_btn("Apply selected", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        self._apply.clicked.connect(self._apply_selected)
        btns.addWidget(self._apply)
        for label, fn in (("Open ProtonDB", lambda: self._open("https://www.protondb.com/app/{id}")),
                          ("Open PCGamingWiki", self._open_pcgw)):
            b = _tool_btn(label)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(self._status)

    # -- appid finder -----------------------------------------------------
    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        self._fill_launcher_games()

    def _fill_launcher_games(self):
        names = launcher_game_names(self.window())
        if [self._launcher_games.itemData(i) for i in range(1, self._launcher_games.count())] == [n for _l, n in names]:
            return
        self._launcher_games.blockSignals(True)
        self._launcher_games.clear()
        self._launcher_games.addItem("Game from Lutris / Heroic / Bottles…", None)
        for label, nm in names:
            self._launcher_games.addItem(label, nm)
        self._launcher_games.blockSignals(False)

    def _pick_launcher_game(self, _i):
        nm = self._launcher_games.currentData()
        if nm:
            self._find_name.setText(nm)
            self._find_appid()

    def _find_appid(self):
        name = self._find_name.text().strip()
        if len(name) < 2:
            return
        if getattr(self, "_aw", None) and self._aw.isRunning():
            return
        self._status.setText("Searching Steam…")
        self._find_btn.setEnabled(False)
        self._aw = _AppidWorker(name, self)
        TASKS.track(self._aw, "Game hints: find appid", _NAV("Game hints"), None, self._aw.done)
        self._aw.done.connect(self._matches_ready)
        self._aw.start()

    def _matches_ready(self, items: list, err: str):
        self._find_btn.setEnabled(True)
        self._matches.clear()
        if err:
            self._status.setText(f"⚠ Steam search failed: {err}")
            return
        for it in items[:12]:
            tag = "  ← likely" if it["score"] >= 0.9 else ""
            li = QListWidgetItem(f"{it['id']:>9}   {it['name']}   ({int(it['score'] * 100)}%){tag}")
            li.setData(Qt.UserRole, it["id"])
            self._matches.addItem(li)
        has = bool(items)
        for w_ in (self._matches, self._use_btn, self._umu_btn):
            w_.setVisible(has)
        if has:
            self._matches.setCurrentRow(0)
            self._status.setText(f"{len(items)} match(es). Pick the right one — games that are not on Steam have no appid.")
        else:
            self._status.setText("No Steam match: this game is probably not sold on Steam (use umu-0 as GAMEID).")

    def _use_match(self, umu: bool):
        it = self._matches.currentItem()
        if it is None:
            return
        aid = it.data(Qt.UserRole)
        self._appid.setText(aid)
        if umu:
            ll = getattr(self.window(), "_launch_line", None)
            if ll is not None:
                ll._umu_gid.setText(f"umu-{aid}")
                self._status.setText(f"GAMEID set to umu-{aid} on the Launch line tab (enable its umu-run box to use it).")
        self._fetch(False)

    def _open(self, tmpl: str):
        aid = self._appid.text().strip()
        if aid.isdigit():
            QDesktopServices.openUrl(QUrl(tmpl.format(id=aid)))

    def _open_pcgw(self):
        aid = self._appid.text().strip()
        if aid.isdigit():
            QDesktopServices.openUrl(QUrl(f"https://www.pcgamingwiki.com/api/appid.php?appid={aid}"))

    def _fetch(self, force: bool):
        aid = self._appid.text().strip()
        if not aid.isdigit():
            self._status.setText("⚠ Enter a numeric Steam appid.")
            return
        if self._worker and self._worker.isRunning():
            return
        self._status.setText("Looking up…")
        self._go.setEnabled(False)
        self._worker = _HintWorker(aid, force, self)
        TASKS.track(self._worker, "Game hints: lookup", _NAV("Game hints"))
        self._worker.done.connect(self._show)
        self._worker.start()

    def _show(self, info: dict):
        self._go.setEnabled(True)
        self._info = info
        lines = [f"appid {info['appid']}" + ("  (cached)" if info.get("cached") else "")]
        pd = info.get("protondb")
        if pd:
            lines.append(f"ProtonDB: {pd.get('tier')}  (trending {pd.get('trendingTier')}, best {pd.get('bestReportedTier')}, "
                         f"{pd.get('total')} reports, confidence {pd.get('confidence')})")
        pc = info.get("pcgw")
        if pc:
            lines.append(f"PCGamingWiki: {pc['title']}")
            lines.append("  Engine: " + (", ".join(pc["engines"]) or "?"))
            lines.append("  Direct3D: " + (", ".join(pc["d3d"]) or "—") +
                         "   Vulkan: " + (", ".join(pc["vulkan"]) or "—") +
                         "   OpenGL: " + (", ".join(pc["opengl"]) or "—"))
        rp = info.get("reports")
        if rp:
            lines.append(f"ProtonDB reports with settings (last {_IDX_MONTHS} months, {rp.get('dump', '')}): {rp.get('n', 0)}")
        else:
            lines.append("ProtonDB user-report data is not downloaded yet — see the Community tips tab.")
        w = self.window()
        cur = w.collect_launch_payload().get("env", {}) if hasattr(w, "collect_launch_payload") else {}
        rows, notes = hint_suggestions(info, SYSHUB.info, cur)
        crows, _st = community_entries(info, SYSHUB.info, cur, 6, bool(SYSHUB.info.has_nvidia), False)
        if crows:
            notes.append(f"{len(crows)} setting example(s) from recent ProtonDB user reports (with the reporters' hardware "
                         "and Proton) are on the “Community tips” tab — unverified, use at your own risk.")
        ct = getattr(getattr(w, "_extra_tools", None), "community", None)
        if ct is not None:
            ct.set_data(info)
        lines += [f"→ {n}" for n in notes]
        lines += [f"⚠ {e}" for e in info.get("errors", [])]
        self._summary.setPlainText("\n".join(lines))
        self._table.setRowCount(0)
        for r, (n, v, why, src, pre) in enumerate(rows):
            self._table.insertRow(r)
            for c, t in enumerate((n, v, src, why)):
                self._table.setItem(r, c, QTableWidgetItem(t))
            if pre:
                self._table.selectRow(r)
        self._status.setText(f"{len(rows)} suggestion(s); {sum(1 for x in rows if x[4])} preselected."
                             if rows else "No suggestions for this title.")

    def _apply_selected(self):
        w = self.window()
        rows = sorted({i.row() for i in self._table.selectedIndexes()})
        for r in rows:
            w.set_env_var(self._table.item(r, 0).text(), self._table.item(r, 1).text())
        self._status.setText(f"Applied {len(rows)} variable(s) to the Environment tab.")
