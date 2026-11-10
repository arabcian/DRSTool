"""Upstream drift check (new / removed env vars, dxvk.conf keys, DRS ids upstream)."""

import base64
import fnmatch
import gzip
import http.client
import io
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from drstool_core import net

from .base import APP_ID
from .catalog_data import (
    ALL_ENV_VARS, ALL_SETTINGS, DXVK_CONFIG_KEY_NAMES, DXVK_KEY_GROUPS, GAMESCOPE_FLAGS,
    VKD3D_CONFIG_DESCS, VKD3D_ENV_VARS, VKD3D_FLAG_GROUPS,
)
from .lookup import _SETTING_BY_ID
from .tasks import _NAV, TASKS
from .theme import T
from .tool_ui import _tool_btn
from .user_catalog import (
    _UC_LABEL, _UC_LISTS, _uc_add, _uc_as_json, _uc_form, _uc_merge, _uc_record, _uc_validate,
    load_user_catalog, save_user_catalog, user_catalog_path,
)
from .widgets import _COMBO_QSS, _LINEEDIT_QSS, _pack2_table_ss, _pack2_title, _slugify

_log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Upstream drift check
# ----------------------------------------------------------------------------

_DRIFT_RAW = "https://raw.githubusercontent.com"
_DRIFT_UA = "DRSTool/upstream-drift (+https://github.com/arabcian/DRSTool)"
_DRIFT_MAX_BYTES = 4 * 1024 * 1024

_DXVK_FILES = ["README.md", "dxvk.conf", "src/util/util_env.cpp", "src/util/config/config.cpp",
               "src/util/log/log.cpp", "src/dxvk/dxvk_instance.cpp", "src/dxvk/dxvk_options.cpp",
               "src/dxvk/dxvk_device_filter.cpp", "src/dxvk/hud/dxvk_hud.cpp", "src/wsi/wsi_platform.cpp",
               "src/dxgi/dxgi_options.cpp", "src/d3d9/d3d9_options.cpp", "src/d3d11/d3d11_options.cpp",
               "src/d3d8/d3d8_options.cpp", "src/dxvk/dxvk_shader_cache.cpp", "src/dxvk/dxvk_latency_builtin.cpp"]
_VKD3D_FILES = ["README.md", "include/private/config_flag_decl.h", "libs/vkd3d/vkd3d_main.c",
                "libs/vkd3d/device.c", "libs/vkd3d/state.c", "libs/vkd3d/command.c", "libs/vkd3d/utils.c",
                "libs/vkd3d/cache.c", "libs/vkd3d/swapchain.c", "libs/vkd3d/breadcrumbs.c",
                "libs/vkd3d-common/debug.c", "libs/vkd3d-shader/vkd3d_shader_main.c"]
_NVAPI_FILES = ["README.md", "src/util/util_env.cpp", "src/nvapi.cpp", "src/nvapi_globals.cpp",
                "src/nvapi_d3d12.cpp", "src/nvapi_drs.cpp", "src/sysinfo/nvapi_adapter.cpp",
                "src/vkreflex/vkreflex_layer.cpp"]

DRIFT_SOURCES: List[dict] = [
    {"key": "dxvk", "title": "DXVK", "repo": "doitsujin/dxvk", "ref": "master", "files": _DXVK_FILES,
     "env": {"prefixes": ("DXVK_",), "exclude": ("DXVK_NVAPI_",), "cats": ("DXVK",)},
     "conf_keys": True},
    {"key": "vkd3d", "title": "vkd3d-proton", "repo": "HansKristian-Work/vkd3d-proton", "ref": "master",
     "files": _VKD3D_FILES,
     "env": {"prefixes": ("VKD3D_",), "exclude": (), "cats": ("VKD3D-Proton",)},
     "vkd3d_flags": "include/private/config_flag_decl.h"},
    {"key": "dxvk-nvapi", "title": "dxvk-nvapi", "repo": "jp7677/dxvk-nvapi", "ref": "master",
     "files": _NVAPI_FILES,
     "env": {"prefixes": ("DXVK_NVAPI_", "DISABLE_DXVK_NVAPI"), "exclude": (), "cats": ("DXVK-NVAPI",)}},
    {"key": "gamescope", "title": "gamescope", "repo": "ValveSoftware/gamescope", "ref": "master",
     "files": ["README.md", "src/main.cpp"],
     "env": {"prefixes": ("GAMESCOPE_", "ENABLE_GAMESCOPE"), "exclude": (), "cats": ("Gamescope",)},
     "gs_flags": "src/main.cpp"},
    {"key": "nvapi-hdr", "title": "NVAPI header", "repo": "NVIDIA/nvapi", "ref": "main",
     "files": ["NvApiDriverSettings.h"], "drs_header": "NvApiDriverSettings.h"},
    {"key": "proton", "title": "Proton", "repo": "ValveSoftware/Proton", "ref": None,
     "ref_fallback": "proton_11.0", "files": ["README.md", "proton"], "optional": True,
     "env": {"prefixes": ("PROTON_",), "exclude": (), "cats": ("Proton",)}},
]


def _drift_fork_source(base: str, key: str, title: str, host: str, repo: str, ref_fallback: str,
                       fork_cat: str, discover=(), files=(), optional: bool = False,
                       ref: Optional[str] = None) -> dict:
    """A fork scans like its base source but is compared against it (status "fork").
    ref=None → the default branch is resolved at scan time (ref_fallback if that fails)."""
    b = next(s for s in DRIFT_SOURCES if s["key"] == base)
    s = {k: v for k, v in b.items() if k in ("env", "conf_keys", "vkd3d_flags", "gs_flags")}
    s.update(key=key, title=title, host=host, repo=repo, ref=ref, ref_fallback=ref_fallback, kind="fork",
             base=base, files=list(b["files"]) + [f for f in files if f not in b["files"]],
             discover=list(discover), fork_cat=fork_cat, fork_tag=title, optional=optional)
    return s


DRIFT_SOURCES += [
    _drift_fork_source("dxvk", "dxvk-ll", "dxvk-low-latency", "github", "netborg-afps/dxvk-low-latency",
                       "low-latency-framepacing", "DXVK Forks", discover=["src/dxvk/framepacer/*"]),
    _drift_fork_source("dxvk", "dxvk-gplall", "dxvk-gplasync-lowlatency", "github",
                       "Digger1955/dxvk-gplasync-lowlatency", "GPLALL-master-2.6.9", "DXVK Forks",
                       discover=["src/dxvk/framepacer/*"], files=["src/dxvk/dxvk_state_cache.cpp"]),
    _drift_fork_source("dxvk", "dxvk-sarek", "DXVK-Sarek", "github", "pythonlover02/DXVK-Sarek", "main",
                       "DXVK Forks", discover=["src/dxvk/framepacer/*"],
                       files=["src/ddraw/ddraw_options.cpp", "src/dxvk/dxvk_state_cache.cpp"]),
    _drift_fork_source("dxvk", "d7vk", "d7vk", "github", "WinterSnowfall/d7vk", "devel", "DXVK Forks",
                       files=["src/ddraw/ddraw_options.cpp"]),
    _drift_fork_source("vkd3d", "vkd3d-ll", "vkd3d-low-latency", "github", "netborg-afps/vkd3d-low-latency",
                       "master", "VKD3D Forks", discover=["*pacer*.[ch]", "*latency*.[ch]"]),
    _drift_fork_source("dxvk", "dxvk-gplasync", "dxvk-gplasync", "gitlab", "Ph42oN/dxvk-gplasync", "master",
                       "DXVK Forks", discover=["patches/*.patch"]),
    _drift_fork_source("proton", "proton-ge", "Proton-GE", "github", "GloriousEggroll/proton-ge-custom",
                       "master", "Proton", optional=True),
    _drift_fork_source("proton", "proton-cachyos", "Proton-CachyOS", "github", "CachyOS/proton-cachyos",
                       "cachyos_main", "Proton", discover=["**/proton"], optional=True),
]


def _drift_catalog() -> dict:
    env_by_cat: Dict[str, set] = {}
    for ev in ALL_ENV_VARS:
        env_by_cat.setdefault(ev.cat, set()).add(ev.name)
    vk_flags = set(VKD3D_CONFIG_DESCS)
    for ev in VKD3D_ENV_VARS:
        if ev.name == "VKD3D_CONFIG":
            vk_flags |= set(ev.options)
    return {
        # DRS_SETTINGS / GPU_ARCH are generated from the DRS and Arch tabs, not catalogued as env vars
        "env_all": {ev.name for ev in ALL_ENV_VARS} | {"DXVK_NVAPI_DRS_SETTINGS", "DXVK_NVAPI_GPU_ARCH"},
        "env_by_cat": env_by_cat,
        "env_desc": {ev.name: (ev.cat, ev.desc) for ev in ALL_ENV_VARS},
        "conf_keys": set(DXVK_CONFIG_KEY_NAMES),
        "vkd3d_flags": vk_flags,
        "gs_flags": {g.flag.lstrip("-") for g in GAMESCOPE_FLAGS},
        "drs_ids": set(_SETTING_BY_ID),
    }


_drift_tls = threading.local()          # one keep-alive connection per host and worker thread


def _drift_conn(u, timeout: float):
    """HTTPS connection to u.netloc, through the system https proxy when there is one."""
    px = None if urllib.request.proxy_bypass(u.hostname) else urllib.request.getproxies().get("https")
    if not px:
        return http.client.HTTPSConnection(u.netloc, timeout=timeout)
    pu = urllib.parse.urlsplit(px if "//" in px else "//" + px)
    conn = http.client.HTTPSConnection(pu.hostname, pu.port or 8080, timeout=timeout)
    auth = {}
    if pu.username:
        cred = f"{urllib.parse.unquote(pu.username)}:{urllib.parse.unquote(pu.password or '')}"
        auth["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
    conn.set_tunnel(u.hostname, u.port or 443, headers=auth)
    return conn


def _drift_fetch(url: str, timeout: float = 25.0) -> Optional[str]:
    """GET as text, None on 404. Connections are kept alive per thread (one TLS handshake per host
    instead of per file) and gzip is accepted; up to 3 redirects (renamed repos) are followed."""
    conns = _drift_tls.__dict__.setdefault("conns", {})
    for _redirect in range(4):
        u = urllib.parse.urlsplit(url)
        path = u.path + (f"?{u.query}" if u.query else "")
        for attempt in (0, 1):                          # a stale kept-alive socket → retry once on a fresh one
            conn = conns.get(u.netloc) or conns.setdefault(u.netloc, _drift_conn(u, timeout))
            try:
                conn.request("GET", path, headers={"User-Agent": _DRIFT_UA, "Accept-Encoding": "gzip"})
                r = conn.getresponse()
                data = r.read(_DRIFT_MAX_BYTES + 1)
                break
            except (http.client.HTTPException, OSError):
                conn.close()
                conns.pop(u.netloc, None)
                if attempt:
                    raise
        if r.status in (301, 302, 307, 308) and r.getheader("Location"):
            url = urllib.parse.urljoin(url, r.getheader("Location"))
            continue
        if r.status == 404:
            return None
        if r.status != 200:
            raise OSError(f"HTTP {r.status} for {url}")
        if r.getheader("Content-Encoding") == "gzip":
            data = gzip.GzipFile(fileobj=io.BytesIO(data)).read(_DRIFT_MAX_BYTES + 1)
        return data[:_DRIFT_MAX_BYTES].decode("utf-8", errors="replace")
    raise OSError(f"too many redirects: {url}")


def _drift_raw_url(src: dict, ref: str, rel: str) -> str:
    """Upstream sources keep the plain `{repo}/{ref}/{rel}` form; sources with a resolved ref use
    refs/heads/ so branch names containing "/" work."""
    if src.get("host") == "gitlab":
        return f'https://gitlab.com/{src["repo"]}/-/raw/{ref}/{rel}'
    mid = f"refs/heads/{ref}" if src.get("ref") is None else ref
    return f'{_DRIFT_RAW}/{src["repo"]}/{mid}/{rel}'


def _drift_fetch_json(url: str, timeout: float = 25.0):
    """-> (data, note, headers). data is None on 403/429 (rate limit), HTTP errors and network errors."""
    try:
        with net.open_url(url, timeout=timeout, accept="application/vnd.github+json", ua=_DRIFT_UA) as r:
            return json.loads(r.read(16 * 1024 * 1024).decode("utf-8", errors="replace")), "", r.headers
    except urllib.error.HTTPError as e:
        return None, ("API rate limit" if e.code in (403, 429) else "API error") + f" (HTTP {e.code})", None
    except Exception as e:                                     # network / TLS / timeout / bad JSON
        return None, f"{type(e).__name__}: {e}", None


def _drift_resolve_ref(src: dict):
    """-> (ref, note). A fixed ref is used as is; ref=None → default branch via the host API,
    cached 24 h in QSettings (drift/ref/<key>); ref_fallback when that fails."""
    if src.get("ref"):
        return src["ref"], ""
    fb = src.get("ref_fallback") or "master"
    ck = f'drift/ref/{src["key"]}'
    qs = QSettings(APP_ID, APP_ID)
    try:
        repo, ref, ts = str(qs.value(ck, "")).split("|")
        if repo == src["repo"] and time.time() - float(ts) < 86400:
            return ref, ""
    except ValueError:
        _log.debug("ignored error", exc_info=True)
    if src.get("host") == "gitlab":
        url = f'https://gitlab.com/api/v4/projects/{urllib.parse.quote(src["repo"], safe="")}'
    else:
        url = f'https://api.github.com/repos/{src["repo"]}'
    data, note, _h = _drift_fetch_json(url)
    ref = data.get("default_branch") if isinstance(data, dict) else None
    if not ref:
        return fb, note or "no default branch in the API answer"
    try:
        qs.setValue(ck, f'{src["repo"]}|{ref}|{int(time.time())}')
    except Exception:
        _log.debug("ignored error", exc_info=True)
    return ref, ""


def _drift_discover(src: dict, ref: str):
    """-> (paths, note): repo files matching src["discover"] (fnmatch patterns, max 40)."""
    pats = src.get("discover") or []
    if not pats:
        return [], ""
    paths: List[str] = []
    note = ""
    q = urllib.parse.quote
    if src.get("host") == "gitlab":
        page = "1"
        for _ in range(5):
            data, note, hdr = _drift_fetch_json(
                f'https://gitlab.com/api/v4/projects/{q(src["repo"], safe="")}/repository/tree'
                f'?recursive=true&per_page=100&ref={q(ref, safe="")}&page={page}')
            if not isinstance(data, list):
                break
            paths += [t["path"] for t in data if t.get("type") == "blob"]
            page = (hdr.get("x-next-page") if hdr else "") or ""
            if not page:
                break
    else:
        data, note, _h = _drift_fetch_json(
            f'https://api.github.com/repos/{src["repo"]}/git/trees/{q(ref)}?recursive=1')
        if isinstance(data, dict):
            paths = [t["path"] for t in data.get("tree", []) if t.get("type") == "blob"]
    return sorted(p for p in paths if any(fnmatch.fnmatchcase(p, pt) for pt in pats))[:40], note


def _drift_patch_added_lines(text: str) -> str:
    """Only what a patch adds (`+` lines, not `+++`), without the leading `+`."""
    return "\n".join(l[1:] for l in text.splitlines() if l.startswith("+") and not l.startswith("+++"))


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _drift_env_names(files: Dict[str, str], prefixes: tuple, exclude: tuple) -> Dict[str, str]:
    """name -> 'file:line' of first sighting. Code: quoted strings only. Docs/conf: any word."""
    pfx = "|".join(re.escape(p) for p in prefixes)
    quoted = re.compile(r'"((?:%s)[A-Z0-9_]*)"' % pfx)
    word = re.compile(r'(?<![A-Za-z0-9_])((?:%s)[A-Z0-9_]*)(?![A-Za-z0-9_])' % pfx)
    found: Dict[str, str] = {}
    for rel, txt in files.items():
        rx = word if rel.lower().endswith((".md", ".conf", ".txt")) else quoted
        for m in rx.finditer(txt):
            name = m.group(1)
            if name.endswith("_") or "_TEST_" in name or any(name.startswith(x) for x in exclude):
                continue
            if len(name) <= min(len(p) for p in prefixes) + 1:
                continue
            found.setdefault(name, f"{rel}:{_line_of(txt, m.start())}")
    return found


def _drift_analyse(src: dict, files: Dict[str, str], cat: dict) -> List[dict]:
    rows: List[dict] = []
    title = src["title"]

    def row(kind, name, status, where="", note=""):
        rows.append({"src": title, "kind": kind, "name": name, "status": status,
                     "where": where, "note": note})

    gone_note = "not found in the scanned files (removed upstream, fork-only, or defined in a file not scanned)"
    spec = src.get("env")
    if spec:
        found = _drift_env_names(files, spec["prefixes"], spec["exclude"])
        for name, where in sorted(found.items()):
            if name not in cat["env_all"]:
                row("env var", name, "new", where)
        mine = set()
        for c in spec["cats"]:
            mine |= cat["env_by_cat"].get(c, set())
        for name in sorted(mine):
            if name.startswith(spec["prefixes"]) and not name.startswith(spec["exclude"] or ("\0",)) \
                    and name not in found:
                row("env var", name, "gone", note=gone_note)
    if src.get("conf_keys"):
        keys: Dict[str, str] = {}
        conf_rx = re.compile(r'^\s*#?\s*([A-Za-z0-9_]+\.[A-Za-z0-9_.]+)\s*=', re.M)
        opt_rx = re.compile(r'"((?:dxvk|dxgi|ddraw|d3d8|d3d9|d3d10|d3d11)\.[A-Za-z0-9_.]+)"')
        for rel, txt in files.items():
            rx = conf_rx if rel.endswith(".conf") else opt_rx
            for m in rx.finditer(txt):
                keys.setdefault(m.group(1), f"{rel}:{_line_of(txt, m.start())}")
        # config.cpp holds per-game blocks → ignore it for 'gone' detection, only options files count
        for k, where in sorted(keys.items()):
            if k not in cat["conf_keys"] and not k.endswith(".conf") \
                    and k.startswith(("dxvk.", "dxgi.", "ddraw.", "d3d9.", "d3d11.", "d3d8.")):
                row("dxvk.conf key", k, "new", where)
        for k in sorted(cat["conf_keys"] - set(keys)):
            row("dxvk.conf key", k, "gone", note=gone_note)
    rel = src.get("vkd3d_flags")
    if rel and rel in files:
        txt = files[rel]
        up: Dict[str, str] = {}
        for m in re.finditer(r'VKD3D_DECL_CONFIG\(\s*"([a-z0-9_]+)"', txt):
            up.setdefault(m.group(1), f"{rel}:{_line_of(txt, m.start())}")
        # PLAIN flags have no VKD3D_CONFIG string (the macro expands to nothing in device.c's option table),
        # so they are internal — unless device.c lists them by hand after the include (breadcrumbs_*).
        dev = files.get("libs/vkd3d/device.c")
        manual = {m.group(1): _line_of(dev, m.start()) for m in re.finditer(
            r'\{\s*"([a-z0-9_]+)"\s*,\s*VKD3D_CONFIG_FLAG_INIT_STATIC', dev)} if dev else None
        for name, line in (manual or {}).items():
            up.setdefault(name, f"libs/vkd3d/device.c:{line}")
        for m in re.finditer(r'VKD3D_DECL_CONFIG_PLAIN\(\s*([A-Z0-9_]+)\s*\)', txt):
            if manual is None:                  # device.c not scanned: cannot tell
                up.setdefault(m.group(1).lower(), f"{rel}:{_line_of(txt, m.start())}  "
                              "(declared without a config string — may be internal)")
        for k, where in sorted(up.items()):
            if k not in cat["vkd3d_flags"]:
                row("VKD3D_CONFIG flag", k, "new", where)
        for k in sorted(cat["vkd3d_flags"] - set(up)):
            row("VKD3D_CONFIG flag", k, "gone", note=gone_note)
    rel = src.get("gs_flags")
    if rel and rel in files:
        txt = files[rel]
        up = {}
        for m in re.finditer(r'\{\s*"([a-z0-9-]+)"\s*,\s*(?:no|required|optional)_argument', txt):
            up.setdefault(m.group(1), f"{rel}:{_line_of(txt, m.start())}")
        up.pop("help", None)
        up.pop("version", None)
        for k, where in sorted(up.items()):
            if k not in cat["gs_flags"]:
                row("CLI flag", f"--{k}", "new", where)
        for k in sorted(cat["gs_flags"] - set(up)):
            row("CLI flag", f"--{k}", "gone", note=gone_note)
    rel = src.get("drs_header")
    if rel and rel in files:
        txt = files[rel]
        up = {}
        # Current header: enum entries `NAME_ID = 0x1234ABCD,`; older ones: `#define NAME_ID 0x1234ABCD`.
        for m in re.finditer(r'^\s*(?:#define\s+)?([A-Z0-9_]+)_ID\s*(?:=\s*)?0x([0-9A-Fa-f]{1,8})\b', txt, re.M):
            up.setdefault(f"0X{int(m.group(2), 16):08X}", (m.group(1), f"{rel}:{_line_of(txt, m.start())}"))
        if not up:      # never report the whole catalog as "gone" because the parser found nothing
            row("DRS setting", "(header parse)", "gone",
                note="no setting IDs found in NvApiDriverSettings.h — format changed or file truncated; "
                     "DRS comparison skipped")
            return rows
        mine = {}
        for k, st in _SETTING_BY_ID.items():
            try:
                mine[f"0X{int(k, 16):08X}"] = st
            except ValueError:
                pass
        for sid, (nm, where) in sorted(up.items()):
            if sid not in mine:
                row("DRS setting", f"0x{sid[2:]}  {nm}", "new", where)
        for sid in sorted(set(mine) - set(up)):
            row("DRS setting", f"{mine[sid].id}  {mine[sid].name}".strip(), "gone",
                note="catalog ID is not in the public NvApiDriverSettings.h (undocumented / private setting?)")
    return rows


def _drift_fork_delta(src: dict, files: Dict[str, str], fork_rows: List[dict], base_rows: List[dict],
                      cat: dict) -> List[dict]:
    """Fork rows: what the fork has that neither its base upstream nor the catalog has (status "fork").
    "gone" only for catalog entries tagged for this fork (fork_cat + fork_tag in the description);
    never relative to upstream (Sarek is 1.10-based, that would be hundreds of false hits)."""
    upstream = {(r["kind"], r["name"]) for r in base_rows if r["status"] == "new"}
    out = [dict(r, status="fork", base=src["base"]) for r in fork_rows
           if r["status"] == "new" and (r["kind"], r["name"]) not in upstream]
    spec = src.get("env")
    if spec and src.get("fork_cat") and src.get("fork_tag"):
        found = _drift_env_names(files, spec["prefixes"], spec["exclude"])
        for name, (c, d) in sorted(cat["env_desc"].items()):
            if c == src["fork_cat"] and src["fork_tag"] in d and name.startswith(spec["prefixes"]) \
                    and name not in found:
                out.append({"src": src["title"], "kind": "env var", "name": name, "status": "gone",
                            "where": "", "note": "catalogued for this fork, no longer found in it"})
    return out


def _drift_attach_ctx(rows: List[dict], files: Dict[str, str], src: dict, ref: str) -> None:
    """new/fork rows get `ctx` (±6 source lines around the sighting) and `blob_url` for the add dialog."""
    for r in rows:
        if r["status"] not in ("new", "fork"):
            continue
        r["fork_cat"] = src.get("fork_cat", "")
        m = re.match(r"(.+?):(\d+)", r.get("where") or "")
        if not m or m.group(1) not in files:
            continue
        rel, line = m.group(1), int(m.group(2))
        lo = max(0, line - 7)
        r["ctx"] = {"rel": rel, "line": line, "text": "\n".join(files[rel].splitlines()[lo:line + 6])}
        if not rel.lower().endswith((".patch", ".diff")):        # patch lines are renumbered, no valid link
            site = "gitlab.com" if src.get("host") == "gitlab" else "github.com"
            sep = "/-/blob/" if src.get("host") == "gitlab" else "/blob/"
            r["blob_url"] = f'https://{site}/{src["repo"]}{sep}{ref}/{rel}#L{line}'


class _DriftWorker(QThread):
    progress = Signal(str)
    done = Signal(list, list)

    def __init__(self, sources: List[dict], cat: dict):
        super().__init__()
        self._sources, self._cat = sources, cat

    def _load(self, src: dict):
        """-> (files, ref, total, net_err, notes) for one source; ref/discovery are resolved first."""
        notes: List[str] = []
        ref, note = _drift_resolve_ref(src)
        if note:
            notes.append(f'{src["title"]}: {note} — using {ref}')
        found, note = _drift_discover(src, ref)
        if note:
            notes.append(f'{src["title"]}: file discovery failed ({note}) — static file list only')
        rels = list(src["files"]) + [p for p in found if p not in src["files"]]
        files: Dict[str, str] = {}
        net_err = ""
        total = len(rels)
        # fetched in parallel (each file was a separate sequential TLS round trip),
        # consumed in the original order so results and the first-error rule stay the same
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [(rel, pool.submit(_drift_fetch, _drift_raw_url(src, ref, rel))) for rel in rels]
            for i, (rel, fut) in enumerate(futs, 1):
                if self.isInterruptionRequested():
                    break
                self.progress.emit(f'{src["title"]}: {rel}  ({i}/{total})')
                try:
                    txt = fut.result()
                except Exception as e:                     # network / TLS / timeout
                    net_err = f"{type(e).__name__}: {e}"
                    break
                if txt is not None:
                    files[rel] = _drift_patch_added_lines(txt) if rel.lower().endswith((".patch", ".diff")) else txt
            for _rel, fut in futs:
                fut.cancel()                               # drop what has not started yet
        return files, ref, total, net_err, notes

    def run(self):
        rows: List[dict] = []
        notes: List[str] = []
        by_key = {s["key"]: s for s in DRIFT_SOURCES}
        wanted = {s["key"]: s for s in self._sources}         # + the bases forks are compared with
        for s in self._sources:
            if s.get("kind") == "fork" and s.get("base") in by_key:
                wanted.setdefault(s["base"], by_key[s["base"]])
        base_rows: Dict[str, List[dict]] = {}                 # base key -> its rows
        # sources download side by side (each also fetches its files in parallel); a base shared by
        # several forks, or selected itself, is downloaded once. Results are consumed in order.
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {k: pool.submit(self._load, s) for k, s in wanted.items()}
            try:
                for src in self._sources:
                    if self.isInterruptionRequested():
                        break
                    files, ref, total, net_err, snotes = futs[src["key"]].result()
                    notes += snotes
                    if net_err:
                        notes.append(f'{src["title"]}: network error — {net_err}')
                        continue
                    if not files:
                        notes.append(f'{src["title"]}: none of the {total} source files exist at {src["repo"]}@{ref}')
                        continue
                    src_rows = _drift_analyse(src, files, self._cat)
                    if src.get("kind") == "fork":
                        base = by_key.get(src.get("base"))
                        if base is None:
                            notes.append(f'{src["title"]}: unknown base source "{src.get("base")}"')
                            continue
                        if base["key"] not in base_rows:      # compare only: its own rows are not shown
                            bfiles, _bref, _bt, berr, bnotes = futs[base["key"]].result()
                            if base["key"] not in {s["key"] for s in self._sources}:
                                notes += bnotes
                            if berr or not bfiles:
                                notes.append(f'{src["title"]}: network error — {berr or "no"} base files '
                                             f'({base["title"]}) to compare with')
                                continue
                            base_rows[base["key"]] = _drift_analyse(base, bfiles, self._cat)
                        src_rows = _drift_fork_delta(src, files, src_rows, base_rows[base["key"]], self._cat)
                    else:
                        base_rows[src["key"]] = src_rows
                    _drift_attach_ctx(src_rows, files, src, ref)
                    rows += src_rows
                    notes.append(f'{src["title"]}: scanned {len(files)}/{total} files')
            finally:
                for f in futs.values():
                    f.cancel()                                # interrupted: drop what has not started
        self.done.emit(rows, notes)


_DRIFT_LABEL = {"new": "NEW", "fork": "FORK", "gone": "gone"}


class UpstreamDriftWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._rows: List[dict] = []
        self._worker: Optional[_DriftWorker] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_pack2_title(
            "Upstream drift check",
            "Downloads the current DXVK, vkd3d-proton, dxvk-nvapi, gamescope sources and NVIDIA's "
            "NvApiDriverSettings.h and compares them with DRSTool's catalog: env vars, dxvk.conf keys, "
            "VKD3D_CONFIG flags, gamescope CLI flags and DRS setting IDs. “New” = upstream has it, the "
            "catalog does not. “Gone” = the catalog has it, upstream no longer mentions it. Forks "
            "(unchecked by default) add “Fork-only” = a fork has it, its upstream and the catalog do not. "
            "Select rows and add them to the catalog, or mark them reviewed to hide them next time."))
        srow, frow2 = QHBoxLayout(), QHBoxLayout()
        self._src_rows = {False: srow, True: frow2}
        self._src_pos = {False: 1, True: 1}
        self._src_checks: Dict[str, QCheckBox] = {}
        srow.addWidget(QLabel("Upstream"))
        frow2.addWidget(QLabel("Forks"))
        srow.addStretch(1)
        frow2.addStretch(1)
        for s in DRIFT_SOURCES:
            self._add_src_check(s)
        self._btn_check = _tool_btn("Check now", bold=True)
        self._btn_check.clicked.connect(self._start)
        srow.addWidget(self._btn_check)
        b_src = _tool_btn("Sources…")
        b_src.clicked.connect(self._add_source)
        frow2.addWidget(b_src)
        lay.addLayout(srow)
        lay.addLayout(frow2)
        frow = QHBoxLayout()
        self._filter = QComboBox()
        self._filter.addItems(["New upstream", "Fork-only", "Gone", "All"])
        self._filter.currentIndexChanged.connect(self._refill)
        self._hide_ack = QCheckBox("Hide reviewed")
        self._hide_ack.setChecked(True)
        self._hide_ack.setStyleSheet(f"color:{T['text2']};")
        self._hide_ack.toggled.connect(self._refill)
        frow.addWidget(QLabel("Show"))
        frow.addWidget(self._filter)
        frow.addWidget(self._hide_ack)
        frow.addStretch(1)
        self._status = QLabel("Not checked yet.")
        self._status.setStyleSheet(f"color:{T['muted']};font-size:12px;")
        frow.addWidget(self._status)
        lay.addLayout(frow)
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["Source", "Kind", "Name", "Status", "Where / note"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.ExtendedSelection)
        hh = self._table.horizontalHeader()
        for c in (0, 1, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self._table.setStyleSheet(_pack2_table_ss())
        self._table.itemSelectionChanged.connect(self._sel_changed)
        self._table.cellDoubleClicked.connect(lambda *_: self._add_selected())
        lay.addWidget(self._table, 1)
        brow = QHBoxLayout()
        b1 = _tool_btn("Mark selected reviewed")
        b1.clicked.connect(lambda: self._ack(selected_only=True))
        b2 = _tool_btn("Mark all shown reviewed")
        b2.clicked.connect(lambda: self._ack(selected_only=False))
        b3 = _tool_btn("Copy report")
        b3.clicked.connect(self._copy_report)
        b4 = _tool_btn("Forget reviewed")
        b4.clicked.connect(self._forget)
        self._btn_add = _tool_btn("Add selected to catalog…", bold=True)
        self._btn_add.setEnabled(False)
        self._btn_add.clicked.connect(self._add_selected)
        b5 = _tool_btn("Manage user catalog…")
        b5.clicked.connect(lambda: UserCatalogDialog(self).exec())
        for b in (self._btn_add, b1, b2, b3, b4, b5):
            brow.addWidget(b)
        brow.addStretch(1)
        lay.addLayout(brow)

    def _add_src_check(self, s: dict):
        c = QCheckBox(s["title"])
        c.setChecked(s.get("kind") != "fork" and not s.get("optional"))
        c.setStyleSheet(f"color:{T['text2']};")
        self._src_checks[s["key"]] = c
        fork = s.get("kind") == "fork"
        self._src_rows[fork].insertWidget(self._src_pos[fork], c)    # after the label, before the stretch
        self._src_pos[fork] += 1

    # reviewed set (QSettings) ------------------------------------------------
    @staticmethod
    def _acks() -> set:
        try:
            v = QSettings(APP_ID, APP_ID).value("drift/reviewed", [])
            if isinstance(v, str):
                v = [v] if v else []
            return set(str(x) for x in (v or []))
        except Exception:
            _log.debug("ignored error", exc_info=True)
            return set()

    @staticmethod
    def _row_key(r: dict) -> str:
        return f'{r["src"]}|{r["kind"]}|{r["status"]}|{r["name"]}'

    def _save_acks(self, s: set):
        try:
            QSettings(APP_ID, APP_ID).setValue("drift/reviewed", sorted(s))
        except Exception:
            _log.debug("ignored error", exc_info=True)

    def _start(self):
        if self._worker is not None and self._worker.isRunning():
            return
        srcs = [s for s in DRIFT_SOURCES if self._src_checks[s["key"]].isChecked()]
        if not srcs:
            return
        self._btn_check.setEnabled(False)
        self._status.setText("Checking…")
        self._worker = _DriftWorker(srcs, _drift_catalog())
        TASKS.track(self._worker, "Upstream drift: check", _NAV("Upstream drift"), self._worker.progress)
        self._worker.progress.connect(self._status.setText)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, rows, notes):
        self._rows = rows
        self._btn_check.setEnabled(True)
        n_new = sum(1 for r in rows if r["status"] == "new")
        n_gone = sum(1 for r in rows if r["status"] == "gone")
        n_fork = sum(1 for r in rows if r["status"] == "fork")
        self._status.setText(f"{n_new} new · {n_fork} fork-only · {n_gone} gone")
        self._status.setToolTip("\n".join(notes))
        if any("error" in n or "none of" in n for n in notes):
            QMessageBox.warning(self, "Upstream drift", "\n".join(notes))
        self._refill()

    def _visible_rows(self) -> List[dict]:
        mode = self._filter.currentIndex()
        acks = self._acks() if self._hide_ack.isChecked() else set()
        out = []
        for r in self._rows:
            if mode < 3 and r["status"] != ("new", "fork", "gone")[mode]:
                continue
            if self._row_key(r) in acks:
                continue
            out.append(r)
        return out

    def _refill(self, *_):
        rows = self._visible_rows()
        t = self._table
        t.setRowCount(0)
        for i, r in enumerate(rows):
            t.insertRow(i)
            col = {"new": T["accent"], "fork": T["info"]}.get(r["status"], T["warn"])
            vals = [r["src"], r["kind"], r["name"], _DRIFT_LABEL[r["status"]],
                    r["where"] or r["note"]]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 3:
                    it.setForeground(QColor(col))
                if c == 4 and not r["where"]:
                    it.setForeground(QColor(T["muted"]))
                if c == 0:
                    it.setData(Qt.UserRole, r)
                t.setItem(i, c, it)

    def _sel_rows(self) -> List[dict]:
        return [self._table.item(r, 0).data(Qt.UserRole)
                for r in sorted({i.row() for i in self._table.selectedItems()})]

    def _sel_changed(self):
        self._btn_add.setEnabled(any(r["status"] in ("new", "fork") for r in self._sel_rows()))

    def _add_selected(self):
        rows = [r for r in self._sel_rows() if r["status"] in ("new", "fork")]
        if not rows:
            return
        forms = [dict(_drift_guess(r), rk=self._row_key(r)) for r in rows]
        dlg = DriftAddDialog(forms, self)
        if dlg.exec() != QDialog.Accepted:
            return
        data = load_user_catalog()
        for f in dlg.result_forms:
            _uc_add(data, f["target"], _uc_record(f))
        try:
            save_user_catalog(data)
        except OSError as e:
            QMessageBox.warning(self, "User catalog", f"Could not write {user_catalog_path()}:\n{e}")
            return
        self._save_acks(self._acks() | {f["rk"] for f in dlg.result_forms})
        self._refill()
        self._status.setText(f"{len(dlg.result_forms)} entries saved — restart DRSTool to apply")

    def _add_source(self):
        dlg = DriftSourceDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        rec = dlg.record
        if any(s["key"] == rec["key"] or s["repo"].lower() == rec["repo"].lower() for s in DRIFT_SOURCES):
            QMessageBox.information(self, "Add drift source", f'{rec["repo"]} is already a source.')
            return
        data = load_user_catalog()
        data["sources"].append(rec)
        try:
            save_user_catalog(data)
        except OSError as e:
            QMessageBox.warning(self, "User catalog", f"Could not write {user_catalog_path()}:\n{e}")
            return
        src = _drift_source_from_record(rec)
        DRIFT_SOURCES.append(src)
        self._add_src_check(src)

    def _ack(self, selected_only: bool):
        if selected_only:
            rows = sorted({i.row() for i in self._table.selectedItems()})
            targets = [self._table.item(r, 0).data(Qt.UserRole) for r in rows]
        else:
            targets = self._visible_rows()
        if not targets:
            return
        acks = self._acks()
        acks.update(self._row_key(r) for r in targets)
        self._save_acks(acks)
        self._refill()

    def _forget(self):
        self._save_acks(set())
        self._refill()

    def _copy_report(self):
        rows = self._visible_rows()
        lines = [f"# DRSTool upstream drift — {datetime.now():%Y-%m-%d}", ""]
        cur = None
        for r in sorted(rows, key=lambda r: (r["src"], r["kind"], r["status"], r["name"])):
            if (r["src"], r["kind"]) != cur:
                cur = (r["src"], r["kind"])
                lines += ["", f'## {r["src"]} — {r["kind"]}']
            lines.append(f'- {_DRIFT_LABEL[r["status"]]}: `{r["name"]}`'
                         + (f' ({r["where"]})' if r["where"] else ""))
        QApplication.clipboard().setText("\n".join(lines))
        self._status.setText(f"Copied report ({len(rows)} rows)")


def _drift_source_from_record(rec: dict) -> dict:
    base = str(rec.get("base") or "dxvk")
    cat = rec.get("fork_cat") or {"dxvk": "DXVK Forks", "vkd3d": "VKD3D Forks"}.get(base, "Proton")
    ref = rec.get("ref") or None
    return _drift_fork_source(base, str(rec["key"]), str(rec.get("title") or rec["key"]),
                              str(rec.get("host") or "github"), str(rec["repo"]), ref or "master", cat,
                              discover=rec.get("discover") or [], files=rec.get("files") or [], ref=ref)


# ── drift row → form defaults ─────────────────────────────────────────────────

_DRIFT_TARGET = {"env var": "env", "VKD3D_CONFIG flag": "vkd3d", "dxvk.conf key": "conf",
                 "CLI flag": "gs", "DRS setting": "drs"}
_DRIFT_ENV_CAT = {"DXVK": "DXVK", "vkd3d-proton": "VKD3D-Proton", "dxvk-nvapi": "DXVK-NVAPI",
                  "gamescope": "Gamescope", "Proton": "Proton"}


def _drift_group_guess(target: str, name: str) -> str:
    n = name.lower()
    if target == "conf" and n.startswith("ddraw."):
        return "🗿 DirectDraw (d7vk/Sarek)"
    if any(w in n for w in ("latency", "frame", "pace")):
        return "⚡ Performance & frame pacing" if target == "vkd3d" else "⏱ Frame pacing & latency"
    return "Other"


def _drift_guess(row: dict) -> dict:
    """Form defaults for a drift row, read from the ±6 source lines in row["ctx"]. Pure."""
    target = _DRIFT_TARGET.get(row["kind"], "env")
    name = row["name"]
    ctx = row.get("ctx") or {}
    text, rel = ctx.get("text", ""), ctx.get("rel", "")
    lines = text.splitlines()
    fork = row.get("status") == "fork"
    f = {"target": target, "name": name, "cat": "", "type": "", "options": [], "default": "", "placeholder": "",
         "group": "", "desc": "", "src": row["src"], "ctx": text, "url": row.get("blob_url", "")}
    if target == "env":
        f["cat"] = row.get("fork_cat") if fork and row.get("fork_cat") else _DRIFT_ENV_CAT.get(row["src"], "Other")
        f["type"] = "string"
        if rel.endswith(".md"):
            f["desc"] = re.sub(r"^[\s>*+-]+", "", next((l for l in lines if name in l), "")).replace("`", "").strip()
        elif '== "1"' in text:
            f["type"], f["options"] = "enum", ["1"]
    elif target == "vkd3d":
        m = re.search(r"`%s`\s*[:\-–]\s*(.+)" % re.escape(name), text)
        f["desc"] = m.group(1).strip() if m else ""
        f["group"] = _drift_group_guess(target, name)
    elif target == "conf":
        f["type"], f["group"] = "text", _drift_group_guess(target, name)
        idx = next((i for i, l in enumerate(lines) if name in l), None)
        if rel.endswith(".conf") and idx is not None:
            block, j = [], idx - 1
            key_rx = re.compile(r"^\s*#?\s*[A-Za-z0-9_]+\.[\w.]+\s*=")
            while j >= 0 and lines[j].lstrip().startswith("#") and not key_rx.match(lines[j]):
                block.insert(0, lines[j].lstrip("# ").strip())
                j -= 1
            f["desc"] = " ".join(block)
            m = re.search(r"Supported values:\s*(.+?)(?:\.\s|\.$|$)", f["desc"])
            opts = [o for o in re.split(r"[,\s|/]+", m.group(1)) if o] if m else []
            m = re.search(re.escape(name) + r"\s*=\s*(\S.*)", lines[idx])
            f["placeholder"] = m.group(1).strip() if m else ""
            if opts and {o.lower() for o in opts} <= {"true", "false", "auto"}:
                f["type"], f["options"] = "enum", opts
                f["default"] = f["placeholder"] if f["placeholder"] in opts else ""
        else:
            if "getOption<bool>" in text:
                f["type"], f["options"] = "enum", ["True", "False"]
            m = re.search(r'"%s"\s*,\s*([^),]+)' % re.escape(name), text)
            d = m.group(1).strip() if m else ""
            f["default"] = d.capitalize() if f["type"] == "enum" and d.lower() in ("true", "false") else d
            f["placeholder"] = "" if f["type"] == "enum" else d
    elif target == "gs":
        ln = next((l for l in lines if '"%s"' % name.lstrip("-") in l), "")
        f.update(cat="Session", type="toggle" if "no_argument" in ln else "string",
                 label=name.lstrip("-").replace("-", " ").title())
    else:
        m = re.match(r"(0x[0-9A-Fa-f]{8})\s+(.*)", name)
        f.update(name=m.group(1) if m else name, label=m.group(2) if m else name, cat="Misc",
                 type="hex", default="0x0")
    if fork and f["desc"] is not None:
        f["desc"] = f"[{row['src']}] " + f["desc"]
    return f


class DriftAddDialog(QDialog):
    """Edit one or more catalog entries (prefilled by _drift_guess, or from user-catalog records).
    On accept `result_forms` holds the checked, validated forms."""
    TARGETS = [("env", "Env var"), ("vkd3d", "VKD3D_CONFIG flag"), ("conf", "dxvk.conf key"),
               ("gs", "Gamescope flag"), ("drs", "DRS setting")]
    TYPES = {"env": ["enum", "int", "string", "bool", "flags"], "vkd3d": [], "conf": ["enum", "text"],
             "gs": ["toggle", "int", "enum", "string"], "drs": ["hex", "enum", "numeric"]}

    def __init__(self, forms: List[dict], parent=None, edit: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Edit catalog entry" if edit else "Add to catalog")
        self.resize(1000, 660)
        self._forms = [dict(f) for f in forms]
        self._edit, self._cur, self._loading = edit, -1, False
        self.result_forms: List[dict] = []
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(QLabel("Entries (checked = saved)"))
        self._list = QListWidget()
        self._list.setFixedWidth(250)
        for f in self._forms:
            it = QListWidgetItem(f["name"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self._list.addItem(it)
        left.addWidget(self._list, 1)
        lay.addLayout(left)
        right = QVBoxLayout()
        lay.addLayout(right, 1)
        form = QFormLayout()
        self._target = QComboBox()
        for k, label in self.TARGETS:
            self._target.addItem(label, k)
        self._name = QLineEdit()
        self._name.setReadOnly(True)
        self._cat = QComboBox()
        self._cat.setEditable(True)
        self._type = QComboBox()
        self._opts = QLineEdit()
        self._opts.setPlaceholderText("comma separated; DRS: Label=0x0, Label2=0x1")
        self._default = QLineEdit()
        self._ph = QLineEdit()
        self._group = QComboBox()
        self._group.setEditable(True)
        self._desc = QPlainTextEdit()
        self._desc.setFixedHeight(90)
        self._ctx = QPlainTextEdit()
        self._ctx.setReadOnly(True)
        self._ctx.setStyleSheet(f"font-family:{T['mono']};font-size:12px;")
        for le in (self._name, self._opts, self._default, self._ph):
            le.setStyleSheet(_LINEEDIT_QSS)
        for cb in (self._target, self._cat, self._type, self._group):
            cb.setStyleSheet(_COMBO_QSS)
        for label, w in (("Add as", self._target), ("Name", self._name), ("Category", self._cat),
                         ("Type", self._type), ("Options", self._opts), ("Default", self._default),
                         ("Placeholder", self._ph), ("Group", self._group), ("Description", self._desc)):
            form.addRow(label, w)
        right.addLayout(form)
        crow = QHBoxLayout()
        crow.addWidget(QLabel("Source context"))
        crow.addStretch(1)
        self._open = _tool_btn("Open on GitHub")
        self._open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self._forms[self._cur]["url"])))
        crow.addWidget(self._open)
        right.addLayout(crow)
        right.addWidget(self._ctx, 1)
        brow = QHBoxLayout()
        brow.addStretch(1)
        cancel, save = _tool_btn("Cancel"), _tool_btn("Save", bold=True)
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._accept)
        brow.addWidget(cancel)
        brow.addWidget(save)
        right.addLayout(brow)
        self._target.currentIndexChanged.connect(self._on_target)
        self._list.currentRowChanged.connect(self._switch)
        self._list.setCurrentRow(0)
        if self._cur < 0:
            self._switch(0)

    def _fill_combos(self, t: str):
        """Type / category / group choices for target `t`."""
        self._type.clear()
        self._type.addItems(self.TYPES[t])
        cats = {"env": sorted({e.cat for e in ALL_ENV_VARS} | {"DXVK Forks", "VKD3D Forks"}),
                "gs": sorted({g.cat for g in GAMESCOPE_FLAGS}),
                "drs": sorted({s.cat for s in ALL_SETTINGS})}.get(t, [])
        self._cat.clear()
        self._cat.addItems(cats)
        groups = {"vkd3d": VKD3D_FLAG_GROUPS, "conf": DXVK_KEY_GROUPS}.get(t, [])
        extra = ["🗿 DirectDraw (d7vk/Sarek)"] if t == "conf" else []
        self._group.clear()
        self._group.addItems([g[0] for g in groups] + extra + (["Other"] if groups else []))
        used = {"env": (1, 1, 1, 1, 1, 0, 0, 0), "vkd3d": (0, 0, 0, 0, 0, 0, 0, 1),
                "conf": (0, 1, 1, 0, 1, 1, 1, 1), "gs": (1, 1, 1, 0, 0, 0, 0, 0),
                "drs": (1, 1, 1, 1, 0, 0, 0, 0)}[t]
        for w, on in zip((self._cat, self._type, self._opts, self._default, self._ph, self._group), used):
            w.setEnabled(bool(on))

    def _on_target(self, *_):
        if not self._loading:
            self._fill_combos(self._target.currentData())

    def _store(self):
        if self._cur < 0:
            return
        self._forms[self._cur].update(
            target=self._target.currentData(), cat=self._cat.currentText().strip(),
            type=self._type.currentText(), options=[o.strip() for o in self._opts.text().split(",") if o.strip()],
            default=self._default.text().strip(), placeholder=self._ph.text().strip(),
            group=self._group.currentText().strip(), desc=self._desc.toPlainText().strip())

    def _switch(self, i: int):
        self._store()
        self._cur = i
        if i < 0:
            return
        f = self._forms[i]
        self._loading = True
        self._target.setCurrentIndex(self._target.findData(f["target"]))
        self._fill_combos(f["target"])
        self._loading = False
        self._name.setText(f["name"] + (f"   {f['label']}" if f.get("label") else ""))
        self._cat.setCurrentText(f["cat"])
        self._type.setCurrentText(f["type"])
        self._opts.setText(", ".join(f["options"]))
        self._default.setText(f["default"])
        self._ph.setText(f["placeholder"])
        self._group.setCurrentText(f["group"])
        self._desc.setPlainText(f["desc"])
        self._ctx.setPlainText(f["ctx"] or "(no source context)")
        self._open.setVisible(bool(f["url"]))

    def _accept(self):
        self._store()
        cat = None if self._edit else _drift_catalog()
        errs, out = [], []
        for i, f in enumerate(self._forms):
            if self._list.item(i).checkState() != Qt.Checked:
                continue
            e = _uc_validate(f, cat)
            if e:
                errs.append(f'{f["name"]}: {e}')
            else:
                out.append(f)
        if errs or not out:
            QMessageBox.warning(self, self.windowTitle(), "\n".join(errs) or "Nothing is checked.")
            return
        self.result_forms = out
        self.accept()


class UserCatalogDialog(QDialog):
    """List / edit / delete / import / export the user catalog; 'Copy as JSON' gives entries for drstool_core/data/*.json."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("User catalog")
        self.resize(860, 480)
        self._data = load_user_catalog()
        lay = QVBoxLayout(self)
        self._info = QLabel(f"{user_catalog_path()}  —  changes apply after restarting DRSTool.")
        self._info.setStyleSheet(f"color:{T['muted']};font-size:12px;")
        lay.addWidget(self._info)
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["Type", "Name", "Category", "Source", "Added"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.ExtendedSelection)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.setStyleSheet(_pack2_table_ss())
        lay.addWidget(self._table, 1)
        brow = QHBoxLayout()
        for label, fn in (("Edit…", self._edit), ("Delete", self._delete), ("Export JSON…", self._export),
                          ("Import JSON…", self._import), ("Copy as JSON", self._copy_json)):
            b = _tool_btn(label)
            b.clicked.connect(fn)
            brow.addWidget(b)
        brow.addStretch(1)
        close = _tool_btn("Close")
        close.clicked.connect(self.accept)
        brow.addWidget(close)
        lay.addLayout(brow)
        self._refill()

    def _refill(self):
        t = self._table
        t.setRowCount(0)
        for target, (lst, kf) in _UC_LISTS.items():
            for rec in self._data.get(lst, []):
                i = t.rowCount()
                t.insertRow(i)
                vals = [_UC_LABEL[target], str(rec.get(kf, "")),
                        str(rec.get("cat") or rec.get("group") or rec.get("base") or ""),
                        str(rec.get("source") or rec.get("repo") or ""), str(rec.get("added", ""))]
                for c, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    if c == 0:
                        it.setData(Qt.UserRole, (target, rec))
                    t.setItem(i, c, it)

    def _selected(self) -> List[tuple]:
        return [self._table.item(r, 0).data(Qt.UserRole)
                for r in sorted({i.row() for i in self._table.selectedItems()})]

    def _save(self):
        try:
            save_user_catalog(self._data)
        except OSError as e:
            QMessageBox.warning(self, "User catalog", f"Could not write {user_catalog_path()}:\n{e}")
        self._refill()

    def _edit(self):
        sel = self._selected()
        if len(sel) != 1 or sel[0][0] == "src":
            QMessageBox.information(self, "User catalog", "Select one catalog entry (sources can only be deleted).")
            return
        target, rec = sel[0]
        dlg = DriftAddDialog([_uc_form(target, rec)], self, edit=True)
        if dlg.exec() != QDialog.Accepted:
            return
        f = dlg.result_forms[0]
        self._data[_UC_LISTS[target][0]].remove(rec)
        _uc_add(self._data, f["target"], _uc_record(f))
        self._save()

    def _delete(self):
        sel = self._selected()
        if sel and QMessageBox.question(self, "User catalog", f"Delete {len(sel)} entr{'y' if len(sel) == 1 else 'ies'}?") \
                == QMessageBox.Yes:
            for target, rec in sel:
                self._data[_UC_LISTS[target][0]].remove(rec)
            self._save()

    def _export(self):
        path, _f = QFileDialog.getSaveFileName(self, "Export user catalog", "user_catalog.json", "JSON (*.json)")
        if path:
            Path(path).write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _import(self):
        path, _f = QFileDialog.getOpenFileName(self, "Import user catalog", "", "JSON (*.json)")
        if not path:
            return
        try:
            other = json.loads(Path(path).read_text(encoding="utf-8"))
            n = _uc_merge(self._data, other if isinstance(other, dict) else {})
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Import", f"Could not read {path}:\n{e}")
            return
        self._save()
        QMessageBox.information(self, "Import", f"{n} entries added (existing names were kept).")

    def _copy_json(self):
        sel = self._selected() or [(t, r) for t, (lst, _k) in _UC_LISTS.items() for r in self._data.get(lst, [])]
        QApplication.clipboard().setText("\n".join(f"{_UC_LABEL[t]}:  {_uc_as_json(t, r)}" for t, r in sel))


class DriftSourceDialog(QDialog):
    """Add a fork (any GitHub/GitLab repo) to scan against one of the upstream sources."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add drift source")
        self.record: Optional[dict] = None
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self._host = QComboBox()
        self._host.addItems(["github", "gitlab"])
        self._repo = QLineEdit()
        self._repo.setPlaceholderText("owner/repo")
        self._ref = QLineEdit()
        self._ref.setPlaceholderText("empty = default branch")
        self._base = QComboBox()
        self._base.addItems(["dxvk", "vkd3d", "proton"])
        self._cat = QComboBox()
        self._cat.setEditable(True)
        self._cat.addItems(["DXVK Forks", "VKD3D Forks", "Proton"])
        self._title = QLineEdit()
        self._title.setPlaceholderText("empty = repo name")
        for label, w in (("Host", self._host), ("Repository", self._repo), ("Branch / ref", self._ref),
                         ("Base (compared with)", self._base), ("Category", self._cat), ("Title", self._title)):
            form.addRow(label, w)
        lay.addLayout(form)
        brow = QHBoxLayout()
        brow.addStretch(1)
        cancel, ok = _tool_btn("Cancel"), _tool_btn("Add", bold=True)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self._accept)
        brow.addWidget(cancel)
        brow.addWidget(ok)
        lay.addLayout(brow)

    def _accept(self):
        repo = self._repo.text().strip().strip("/")
        if not re.fullmatch(r"[\w.-]+(/[\w.-]+)+", repo):
            QMessageBox.warning(self, "Add drift source", "Repository must look like owner/repo.")
            return
        title = self._title.text().strip() or repo.rsplit("/", 1)[-1]
        self.record = {"key": "user-" + _slugify(repo), "title": title, "host": self._host.currentText(),
                       "repo": repo, "ref": self._ref.text().strip(), "base": self._base.currentText(),
                       "files": [], "discover": [], "kind": "fork", "fork_cat": self._cat.currentText().strip(),
                       "added": datetime.now().strftime("%Y-%m-%d")}
        self.accept()
