"""Tool Manager: download / install / update Proton, Wine, DXVK and vkd3d builds."""

import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import threading as _threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPlainTextEdit, QProgressBar, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from drstool_core import safe_archive
from drstool_core.file_transactions import atomic_write_json
from drstool_core import launcher_paths, net
from drstool_core.catalog import DATA_DIR

from .base import _atomic_write, _backup_file, _proc_running
from .ngx import ngx_human
from .steam_utils import STEAM_COMPAT_PATH, vdf_get_app_value, vdf_set_app_value
from .tasks import _NAV, TASKS
from .theme import T
from .toasts import toast
from .user_catalog import user_tm_tools
from .tool_ui import _tool_btn, _TOOL_GROUP_SS
from .widgets import _COMBO_QSS, _desc_label, _MONO_BOX_QSS

_log = logging.getLogger(__name__)


# ============================================================================
# Tool Manager — download / install / update / remove gaming compatibility
# tools (Proton forks, Wine builds, DXVK, vkd3d) for Steam, Lutris, Heroic,
# Bottles or any folder.  Same catalogue and on-disk conventions as protondl
# (the version file is protondl-compatible), implemented with the stdlib only.
# ============================================================================


@dataclass
class TMTool:
    id: str
    name: str
    kind: str                    # proton | wine | dxvk | vkd3d
    desc: str
    url: str                     # project page
    src: str                     # github | gitea | gitlab | actions | branch | stl
    api: str = ""                # releases API (github / gitea / gitlab)
    fmt: str = ".tar.gz"         # release-asset suffix to install
    csum: str = ""               # SHA-512 sidecar suffix ("" = none published)
    advanced: bool = False
    steam_only: bool = False
    fixed_dir: str = ""          # tools that always unpack into one folder name
    repo: str = ""               # actions: owner/name
    wf: str = ""                 # actions: workflow-file substring
    branch: str = ""             # actions: only runs from this branch
    ref: str = ""                # branch / tarball source URL


def _load_tm_tools() -> List[TMTool]:
    """Built-in tools (drstool_core/data/tm_tools.json), then the user's own
    "tm_tools" records from user_catalog.json; a built-in id always wins."""
    tools = [TMTool(**d) for d in json.loads((DATA_DIR / "tm_tools.json").read_text(encoding="utf-8"))]
    have = {t.id for t in tools}
    for d in user_tm_tools():
        try:
            t = TMTool(**d)
        except TypeError as e:
            _log.warning("user tool source skipped (%s)", e)
            continue
        if t.id not in have:
            have.add(t.id)
            tools.append(t)
    return tools


TM_TOOLS: List[TMTool] = _load_tm_tools()
TM_BY_NAME: Dict[str, TMTool] = {t.name: t for t in TM_TOOLS}
TM_KIND_LABEL = {"proton": "Proton", "wine": "Wine", "dxvk": "DXVK", "vkd3d": "VKD3D"}

# launcher key -> tool kind -> folder relative to the launcher root
_TM_DIRS = {
    "steam":   {"proton": "compatibilitytools.d"},
    "lutris":  {"proton": "runners/wine", "wine": "runners/wine",
                "dxvk": "runtime/dxvk", "vkd3d": "runtime/vkd3d"},
    "heroic":  {"proton": "tools/proton", "wine": "tools/wine",
                "dxvk": "tools/dxvk", "vkd3d": "tools/vkd3d"},
    "bottles": {"proton": "runners", "wine": "runners"},
    "umu":     {"proton": "."},
    "custom":  {"proton": ".", "wine": ".", "dxvk": ".", "vkd3d": "."},
}
_TM_UA = "DRSTool-tool-manager (+https://github.com/arabcian/DRSTool)"
_TM_VERSION_FILE = "protondl_version.json"       # same name/schema as protondl
_TM_ARM = re.compile(r"(aarch64|arm64|armv\d)", re.I)
_TM_FILTER = hasattr(tarfile, "tar_filter")
_STL_DIR, _STL_INTERNAL, _STL_PREFIX = "SteamTinkerLaunch", "Proton-stl", "sonic2kk-steamtinkerlaunch-"


class _TMCancelled(Exception):
    pass


@dataclass
class TMLauncher:
    key: str
    label: str
    root: Path

    def dir_for(self, kind: str) -> Optional[Path]:
        rel = _TM_DIRS[self.key].get(kind)
        return None if rel is None else self.root / rel

    def dirs(self) -> List[tuple]:
        """[(kinds, folder)] — kinds sharing one folder (Lutris/Bottles runners) are merged."""
        by: Dict[Path, List[str]] = {}
        for kind in _TM_DIRS[self.key]:
            by.setdefault(self.dir_for(kind), []).append(kind)
        return [(tuple(k), p) for p, k in by.items()]

    def supports(self, tool: TMTool) -> bool:
        return (self.key == "steam") if tool.steam_only else tool.kind in _TM_DIRS[self.key]


@dataclass
class TMVer:
    label: str                   # shown, and stored as "version" in the version file
    date: str = ""
    url: str = ""
    name: str = ""               # archive file name
    size: int = 0
    csum: str = ""               # SHA-512 sidecar URL
    ref: str = ""                # actions: workflow run id


@dataclass
class TMInstalled:
    path: Path
    kind: str
    tool: str                    # "" = not installed by this tool / protondl
    version: str
    ts: int = 0


# ── discovery ───────────────────────────────────────────────────────────────

def tm_discover() -> List[TMLauncher]:
    h = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME") or h / ".local/share")
    conf = Path(os.environ.get("XDG_CONFIG_HOME") or h / ".config")
    out: List[TMLauncher] = []

    def add(key, label, root, ok):
        try:
            r = root.resolve()
            if root.is_dir() and ok(r) and all(r != x.root for x in out if x.key == key):
                out.append(TMLauncher(key, label, r))
        except OSError:
            pass

    steam_ok = lambda p: (p / "config").is_dir() or (p / "ubuntu12_32").exists()
    add("steam", "Steam", data / "Steam", steam_ok)
    for label, c in launcher_paths.steam_root_candidates(h):
        add("steam", label, c, steam_ok)
    lut_ok = lambda p: (p / "runners").is_dir() or (p / "runtime").is_dir()
    add("lutris", "Lutris", data / "lutris", lut_ok)
    add("lutris", "Lutris (Flatpak)", h / ".var/app/net.lutris.Lutris/data/lutris", lut_ok)
    her_ok = lambda p: (p / "tools").is_dir() or (p / "config.json").is_file()
    add("heroic", "Heroic", conf / "heroic", her_ok)
    add("heroic", "Heroic (Flatpak)", h / ".var/app/com.heroicgameslauncher.hgl/config/heroic", her_ok)
    bot_ok = lambda p: (p / "runners").is_dir() or (p / "bottles").is_dir() or (p / "data.yml").is_file()
    add("bottles", "Bottles", data / "bottles", bot_ok)
    add("bottles", "Bottles (Flatpak)", h / ".var/app/com.usebottles.bottles/data/bottles", bot_ok)
    add("umu", "umu (Proton folder)", data / "umu/compatibilitytools", lambda p: True)
    return out


# ── network ─────────────────────────────────────────────────────────────────

def _tm_get(url: str, accept: str = "application/json", timeout: int = 25) -> bytes:
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        with net.open_url(url, timeout=timeout, accept=accept, ua=_TM_UA) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if net.rate_limit_note(e):
            raise RuntimeError(f"{host}: {net.rate_limit_note(e)}") from None
        raise RuntimeError(f"{host}: HTTP {e.code} for {urllib.parse.urlsplit(url).path}") from None
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"cannot reach {host or url}: {getattr(e, 'reason', e)}") from None


def _tm_json(url: str):
    try:
        return json.loads(_tm_get(url).decode("utf-8", "replace"))
    except ValueError:
        raise RuntimeError(f"unexpected (non-JSON) answer from {url}") from None


def _tm_cpu_level() -> int:
    flags: set = set()
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if ln.startswith("flags"):
                    flags = set(ln.split(":", 1)[1].split())
                    break
    except OSError:
        return 1
    v2 = {"cx16", "popcnt", "sse4_1", "sse4_2", "ssse3"}
    v3 = v2 | {"avx", "avx2", "bmi1", "bmi2", "fma", "f16c", "movbe", "abm"}
    v4 = v3 | {"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"}
    return 4 if v4 <= flags else 3 if v3 <= flags else 2 if v2 <= flags else 1


_TM_HWCAP = re.compile(r"-x86_64(?:_v([234]))?\.tar\.xz$")


def _tm_variants(tool: TMTool):
    """[(label(tag), asset-name predicate)] — a release may yield several installable builds."""
    ident = lambda t: t
    if tool.id == "dxvk":
        return [(ident, lambda n: "native" not in n.lower())]
    if tool.id == "lutris-wine":
        return [(ident, lambda n: "fshack" not in n),
                (lambda t: t.replace("lutris-", "lutris-fshack-", 1), lambda n: "fshack" in n)]
    if tool.id == "kron4ek-wine":
        ok = lambda n: "staging" not in n
        return [(lambda t: f"{t} (amd64)", lambda n: ok(n) and "amd64" in n and "wow64" not in n),
                (lambda t: f"{t} (wow64)", lambda n: ok(n) and "amd64-wow64" in n)]
    if tool.id == "proton-cachyos":
        lvl = _tm_cpu_level()

        def fits(n):
            m = _TM_HWCAP.search(n)
            return bool(m) and int(m.group(1) or 1) <= lvl
        return [(ident, fits)]
    return [(ident, lambda n: True)]


def _tm_prio(tool: TMTool, name: str) -> int:
    if tool.id == "proton-cachyos":
        m = _TM_HWCAP.search(name)
        return int(m.group(1) or 1) if m else 0
    return 0


def tm_variant(tool: TMTool, label: str) -> str:
    if tool.id == "lutris-wine":
        return "fshack" if "fshack-" in label else ""
    if tool.id == "kron4ek-wine":
        return "wow64" if label.endswith("(wow64)") else "amd64"
    return ""


def _tm_norm_assets(rel: dict) -> List[dict]:
    a = rel.get("assets")
    if isinstance(a, dict):                                  # GitLab: assets.links
        a = a.get("links", [])
    out = []
    for x in a or []:
        n = x.get("name") or ""
        u = x.get("browser_download_url") or x.get("url") or x.get("direct_asset_url") or ""
        if n and u:
            out.append({"name": n, "url": u, "size": int(x.get("size") or 0)})
    return out


def _tm_release_versions(tool: TMTool, rel: dict) -> List[TMVer]:
    tag = rel.get("name") if tool.src == "gitlab" else rel.get("tag_name")
    if not tag:
        return []
    date = str(rel.get("published_at") or rel.get("released_at") or rel.get("created_at") or "")[:10]
    assets = _tm_norm_assets(rel)
    out = []
    for label_fn, pred in _tm_variants(tool):
        best, bp = None, -1
        for a in assets:
            if a["name"].endswith(tool.fmt) and not _TM_ARM.search(a["name"]) and pred(a["name"]):
                p = _tm_prio(tool, a["name"])
                if p >= bp:
                    best, bp = a, p
        if best is None:
            continue
        cs = ""
        if tool.csum:
            stem = best["name"][:-len(tool.fmt)]
            names = {x["name"]: x["url"] for x in assets}
            cs = names.get(stem + tool.csum) or names.get(best["name"] + tool.csum, "")
            if not cs:
                side = [x["url"] for x in assets if x["name"].endswith(tool.csum)]
                cs = side[0] if len(side) == 1 else ""
        out.append(TMVer(label_fn(tag), date, best["url"], best["name"], best["size"], cs))
    return out


def _tm_actions_versions(tool: TMTool, count: int) -> List[TMVer]:
    base = f"https://api.github.com/repos/{tool.repo}/actions"
    out: List[TMVer] = []
    for wf in _tm_json(f"{base}/workflows?per_page=100").get("workflows", []):
        if wf.get("state") != "active" or tool.wf not in str(wf.get("path", "")):
            continue
        q = f"per_page={count}&status=success" + (f"&branch={tool.branch}" if tool.branch else "")
        for run in _tm_json(f"{base}/workflows/{wf['id']}/runs?{q}").get("workflow_runs", []):
            out.append(TMVer(str(run["id"]), str(run.get("created_at", ""))[:10], ref=str(run["id"])))
    if not out:
        raise RuntimeError(f"no successful CI run found for {tool.name} (workflow “{tool.wf}” in "
                           f"{tool.repo}) — the upstream CI layout may have changed")
    out.sort(key=lambda v: int(v.ref), reverse=True)
    return out[:count]


def tm_versions(tool: TMTool, count: int = 30) -> List[TMVer]:
    """Installable builds of `tool`, newest first."""
    if tool.src in ("github", "gitea", "gitlab"):
        q = "limit" if tool.src == "gitea" else "per_page"
        data = _tm_json(f"{tool.api}?{q}={count}")
        out: List[TMVer] = []
        for rel in data if isinstance(data, list) else []:
            if not rel.get("draft"):
                out += _tm_release_versions(tool, rel)
        return out
    if tool.src == "actions":
        return _tm_actions_versions(tool, count)
    if tool.src == "branch":
        return [TMVer(tool.ref.rsplit("/", 1)[-1].split(".tar")[0], "", tool.ref,
                      tool.ref.rsplit("/", 1)[-1])]
    if tool.src == "stl":
        if tool.ref:                                          # development branch
            return [TMVer("master", "", tool.ref, "master.tar.gz")]
        data = _tm_json(f"{tool.api}?per_page={count}")
        return [TMVer(r["tag_name"], str(r.get("published_at", ""))[:10], r["tarball_url"],
                      f"{r['tag_name']}.tar.gz")
                for r in (data if isinstance(data, list) else []) if r.get("tag_name") and r.get("tarball_url")]
    return []


def _tm_actions_resolve(tool: TMTool, ver: TMVer) -> TMVer:
    arts = _tm_json(f"https://api.github.com/repos/{tool.repo}/actions/runs/{ver.ref}/artifacts?per_page=100")
    items = [a for a in arts.get("artifacts", []) if not a.get("expired")]
    if not items:
        raise RuntimeError(f"CI run {ver.ref} has no (unexpired) artifact")
    # Never silently choose the first artifact; CI workflows often upload test
    # logs, source bundles and ARM builds alongside the Linux tool binary.
    expected = tool.id.replace('proton-', '').replace('-', '')
    candidates = [a for a in items if not _TM_ARM.search(a.get('name', '')) and
                  not re.search(r"(windows|win64|win32|macos|osx|darwin)", a.get('name', ''), re.I) and
                  not re.search(r"(debug|symbol|log|test-results|source|src-only)",
                                a.get('name', ''), re.I)]
    matching = [a for a in candidates if expected in re.sub(r'[^a-z0-9]', '',
                a.get('name', '').lower())]
    if matching:
        candidates = matching
    if len(candidates) != 1:
        names = ', '.join(str(a.get('name')) for a in items[:15])
        raise RuntimeError(f"ambiguous CI artifact for {tool.name} ({len(candidates)} candidates): {names}; "
                           "refusing to install an arbitrary artifact")
    a = candidates[0]
    ver.url = (f"https://nightly.link/{tool.repo}/actions/runs/{ver.ref}/"
               f"{urllib.parse.quote(a['name'])}.zip")
    ver.name, ver.size = a["name"] + ".zip", int(a.get("size_in_bytes") or 0)
    return ver


# ── install ─────────────────────────────────────────────────────────────────

def _tm_cache_dir() -> Path:
    d = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "drstool" / "tool-manager"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tm_download(url: str, dest: Path, hint: int, cancel, prog, digest=None):
    """Validated HTTP resume; partial files without server validators are replaced."""
    from drstool_core.http_download import download, DownloadCancelled
    try:
        download(url, dest, hint, cancel, prog, user_agent=_TM_UA)
    except DownloadCancelled:
        raise _TMCancelled() from None
    # Streaming verification during resume can accidentally hash stale bytes.
    # The caller verifies the fully assembled file from disk instead.


def _tm_checksum_value(text: str, filename: str) -> str:
    """Find the SHA-512 for exactly this archive, not the first digest in a list."""
    records = []
    bare = []
    for line in text.splitlines():
        m = re.match(r"^\s*([0-9a-fA-F]{128})\s+\*?(.+?)\s*$", line)
        if m:
            records.append((m.group(2).lstrip('./'), m.group(1).lower()))
            continue
        b = re.match(r"^\s*([0-9a-fA-F]{128})\s*$", line)
        if b:
            bare.append(b.group(1).lower())
    matched = [h for name, h in records if name == filename]
    if len(matched) == 1:
        return matched[0]
    if not records and len(bare) == 1:
        return bare[0]
    raise RuntimeError(f"no unambiguous SHA-512 checksum for {filename!r}")


def _tm_verify(path: Path, csum_url: str, cancel, prog, digest=None, filename: str | None = None) -> str:
    """`digest`: SHA-512 calculated while downloading, else hash from disk."""
    if not csum_url:
        return "UNVERIFIED — no checksum published for this build"
    checksum = _tm_checksum_value(_tm_get(csum_url, "*/*").decode("utf-8", "replace"),
                                  filename or path.name)
    h = digest
    if h is None:
        h, done, size = hashlib.sha512(), 0, max(path.stat().st_size, 1)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                if cancel.is_set():
                    raise _TMCancelled()
                h.update(chunk)
                done += len(chunk)
                prog("Verifying", done, size)
    if h.hexdigest() != checksum:
        raise RuntimeError("SHA-512 mismatch — the download is corrupted; nothing was installed")
    return "SHA-512 verified"


def _tm_untar_cli(path: Path, dest: Path, cancel):
    """Legacy entry point; now safely streams zstd through verified extraction."""
    return _tm_untar(path, dest, cancel, lambda *_: None)


def _tm_untar(path: Path, dest: Path, cancel, prog, label: str = "Extracting"):
    try:
        prog(label, 0, 0)  # unknown expanded size: use indeterminate progress
        safe_archive.extract_tar(path, dest, cancel=cancel, allow_internal_links=True)
        prog(label, 1, 1)
    except RuntimeError as ex:
        if 'cancelled' in str(ex):
            raise _TMCancelled() from ex
        raise


def _tm_unzip(path: Path, dest: Path, cancel, prog):
    try:
        safe_archive.extract_zip(path, dest, cancel=cancel,
                                 progress=lambda n, total: prog('Extracting', n, total))
    except RuntimeError as ex:
        if 'cancelled' in str(ex):
            raise _TMCancelled() from ex
        raise


def _tm_extract(tool: TMTool, ver: TMVer, archive: Path, stage: Path, cancel, prog):
    if not archive.name.endswith(".zip"):
        return _tm_untar(archive, stage, cancel, prog)
    tmp = stage / "_z"                                       # CI artifact: zip holding a tar
    _tm_unzip(archive, tmp, cancel, prog)
    tars = sorted(p for p in tmp.rglob("*") if p.is_file() and "native" not in p.name.lower()
                  and re.search(r"\.tar(\.\w+)?$", p.name))
    if not tars:                                             # files directly in the artifact
        tmp.rename(stage / f"{tool.id}-{ver.label}")
        return
    _tm_untar(tars[0], stage, cancel, prog)
    usr = stage / "usr"                                      # Arch-package layout
    if usr.is_dir():
        usr.rename(stage / re.sub(r"\.tar(\.\w+)?$", "", tars[0].name))
    for dot in (".BUILDINFO", ".INSTALL", ".MTREE", ".PKGINFO"):
        (stage / dot).unlink(missing_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)


def _tm_post(tool: TMTool, stage: Path) -> Optional[Path]:
    """Per-tool fix-ups inside the staging folder; returns the main folder name's path."""
    dirs = sorted(p for p in stage.iterdir() if p.is_dir() and not p.name.startswith("_"))
    if not dirs:
        raise RuntimeError("the archive contained no folder")
    if tool.id == "steam-play-none":
        if len(dirs) == 1 and dirs[0].name != "Steam-Play-None":
            dirs[0].rename(stage / "Steam-Play-None")
        return stage / "Steam-Play-None"
    if tool.src == "stl":
        src = ([d for d in dirs if d.name.startswith(_STL_PREFIX)] or dirs)[0]
        tgt = stage / _STL_DIR
        if tgt.exists() and tgt != src:
            shutil.rmtree(tgt)
        src.rename(tgt)
        (tgt / "compatibilitytool.vdf").write_text(
            '"compatibilitytools"\n{\n  "compat_tools"\n  {\n'
            f'    "{_STL_INTERNAL}" // Internal name of this tool\n    {{\n'
            '      "install_path" "."\n      "display_name" "Steam Tinker Launch"\n\n'
            '      "from_oslist"  "windows"\n      "to_oslist"    "linux"\n    }\n  }\n}\n', encoding="utf-8")
        (tgt / "toolmanifest.vdf").write_text(
            '"manifest"\n{\n  "commandline" "/steamtinkerlaunch run"\n'
            '  "commandline_waitforexitandrun" "/steamtinkerlaunch waitforexitandrun"\n}\n', encoding="utf-8")
        return tgt
    if tool.fixed_dir and (stage / tool.fixed_dir).is_dir():
        return stage / tool.fixed_dir
    return dirs[0]


def tm_find_installed(folder: Path, tool_name: str, label: str) -> Optional[Path]:
    try:
        for p in folder.iterdir():
            info = _tm_read_info(p)
            if info and info.get("compat_tool") == tool_name and info.get("version") == label:
                return p
    except OSError:
        pass
    return None


def _tm_read_info(p: Path) -> Optional[dict]:
    try:
        d = json.loads((p / _TM_VERSION_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("compat_tool") and d.get("version") else None
    except (OSError, ValueError):
        return None


def tm_install(tool: TMTool, ver: TMVer, launcher: TMLauncher, force: bool, cancel, log, prog) -> dict:
    dest = launcher.dir_for(tool.kind)
    if dest is None or not launcher.supports(tool):
        raise RuntimeError(f"{tool.name} cannot be installed for {launcher.label}")
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"cannot create {dest}: {e.strerror or e}") from None
    if not os.access(dest, os.W_OK):
        raise RuntimeError(f"no write permission for {dest}")
    have = tm_find_installed(dest, tool.name, ver.label)
    if have is not None and not force:
        raise RuntimeError(f"{tool.name} {ver.label} is already installed ({have.name}) — "
                           f"tick “Reinstall” to replace it")
    for old in dest.glob(".drstool-tm-*"):                   # leftovers of a crashed run
        if time.time() - old.stat().st_mtime > 86400:
            shutil.rmtree(old, ignore_errors=True)
    if tool.src == "actions":
        log(f"  resolving CI artifact of run {ver.ref} …")
        ver = _tm_actions_resolve(tool, ver)
    if not ver.url:
        raise RuntimeError(f"no download found for {tool.name} {ver.label}")
    if ver.size:
        for where, need in ((dest, ver.size * 4), (_tm_cache_dir(), ver.size * 1.2)):
            if shutil.disk_usage(where).free < need:
                raise RuntimeError(f"not enough free space in {where} (about {int(need / 1048576)} MB needed)")
    cache = _tm_cache_dir()
    for old in cache.glob("part-*"):                          # abandoned partial downloads
        if time.time() - old.stat().st_mtime > 7 * 86400:
            old.unlink(missing_ok=True)
            old.with_name(old.name + '.http.json').unlink(missing_ok=True)
    # Stable name per URL: an interrupted download is kept here and resumed next time.
    archive = cache / ("part-" + hashlib.sha256(ver.url.encode()).hexdigest()[:16] + "-"
                       + re.sub(r"[^\w.\-]", "_", ver.name or "x"))
    stage = Path(tempfile.mkdtemp(prefix=".drstool-tm-", dir=dest))
    ok = False
    try:
        log(f"  ↓ {ver.name or ver.url}" + (f"  ({ngx_human(ver.size)})" if ver.size else ""))
        _tm_download(ver.url, archive, ver.size, cancel, prog)
        log("  " + _tm_verify(archive, ver.csum, cancel, prog, filename=ver.name))
        _tm_extract(tool, ver, archive, stage, cancel, prog)
        prog("Installing", 0, 0)
        main = _tm_post(tool, stage)
        main_name = main.name
        # Stage the version metadata BEFORE any installation is visible.
        atomic_write_json(main / _TM_VERSION_FILE,
                          {"compat_tool": tool.name, "version": ver.label,
                           "installed_at": int(time.time()), "arch": "x86_64"})
        if launcher.key == "steam" and tool.kind == "proton" and not (main / "compatibilitytool.vdf").is_file():
            raise RuntimeError("invalid compatibility tool: compatibilitytool.vdf missing; nothing installed")
        dirs = sorted(x for x in stage.iterdir() if x.is_dir() and not x.name.startswith('_'))
        if main not in dirs or not dirs:
            raise RuntimeError('malformed tool archive: installation directory missing')
        for folder in dirs:
            if folder.is_symlink() or not folder.name or folder.name.startswith('.'):
                raise RuntimeError(f'unsafe install folder: {folder}')
            if (dest / folder.name).exists() and not force:
                raise RuntimeError(f'{dest / folder.name} already exists — tick “Reinstall” to replace it')
        moved = []
        renamed = []
        try:
            # Keep ALL old folders until the full set has been installed.
            for folder in dirs:
                tgt = dest / folder.name
                if tgt.exists() or tgt.is_symlink():
                    bak = dest / f'.drstool-previous-{os.getpid()}-{folder.name}'
                    if bak.exists():
                        raise RuntimeError(f'previous failed install requires recovery: {bak}')
                    os.replace(tgt, bak)
                    renamed.append((tgt, bak))
                os.replace(folder, tgt)
                moved.append(tgt)
            for tgt, bak in renamed:
                try:
                    if bak.is_dir() and not bak.is_symlink():
                        shutil.rmtree(bak)
                    else:
                        bak.unlink()
                except OSError as ex:
                    # Cleanup must never turn a completed install into a
                    # rollback after some older backups were deleted.
                    log(f'  old version kept at {bak}: {ex}')
            ok = True
            return {"path": dest / main_name, "tool": tool.name, "version": ver.label,
                    "note": "", "all": moved}
        except BaseException:
            for tgt in reversed(moved):
                if tgt.is_symlink() or tgt.is_file():
                    tgt.unlink()
                else:
                    shutil.rmtree(tgt)
            for tgt, bak in reversed(renamed):
                if bak.exists() or bak.is_symlink():
                    os.replace(bak, tgt)
            raise

    finally:
        if ok:
            try:
                archive.unlink(missing_ok=True)
                archive.with_name(archive.name + '.http.json').unlink(missing_ok=True)
            except OSError as ex:
                log(f'  installed successfully but could not clean download cache: {ex}')
        shutil.rmtree(stage, ignore_errors=True)


def tm_remove(path: Path, folder: Path):
    """Delete an installed tool folder; never follows a symlink and never leaves `folder`."""
    if path.parent.resolve() != folder.resolve() or path.name.startswith("."):
        raise RuntimeError(f"refusing to remove {path} (not directly inside {folder})")
    if path.is_symlink():
        path.unlink()
    else:
        shutil.rmtree(path)


# ── installed tools ─────────────────────────────────────────────────────────

def tm_scan(launcher: TMLauncher) -> List[TMInstalled]:
    out: List[TMInstalled] = []
    for kinds, folder in launcher.dirs():
        try:
            subs = sorted((p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")),
                          key=lambda p: p.name.lower())
        except OSError:
            continue
        for p in subs:
            info = _tm_read_info(p)
            tool = TM_BY_NAME.get(info["compat_tool"]) if info else None
            if len(kinds) == 1:
                kind = kinds[0]
            elif tool:
                kind = tool.kind
            elif (p / "proton").is_file():
                kind = "proton"
            elif (p / "bin" / "wine").exists() or (p / "files" / "bin" / "wine").exists():
                kind = "wine"
            elif "dxvk" in p.name.lower():
                kind = "dxvk"
            elif "vkd3d" in p.name.lower():
                kind = "vkd3d"
            else:
                kind = "?"
            out.append(TMInstalled(p, kind, info["compat_tool"] if info else "",
                                   str(info["version"]) if info else p.name,
                                   int(info.get("installed_at") or 0) if info else 0))
    return out


def _tm_steam_internal(d: Path) -> str:
    try:
        m = re.search(r'"compat_tools"\s*\{\s*"([^"]+)"', (d / "compatibilitytool.vdf").read_text(errors="replace"))
        if m:
            return m.group(1)
    except OSError:
        pass
    return d.name


def tm_set_steam_default(root: Path, internal: str) -> str:
    """Global Steam Play default (CompatToolMapping "0" in config.vdf), with backup + read-back."""
    cfg = root / "config" / "config.vdf"
    text = cfg.read_text(errors="replace")
    new = vdf_set_app_value(text, STEAM_COMPAT_PATH, "0", "name", internal)
    new = vdf_set_app_value(new, STEAM_COMPAT_PATH, "0", "config",
                            vdf_get_app_value(new, STEAM_COMPAT_PATH, "0", "config"))
    if not vdf_get_app_value(new, STEAM_COMPAT_PATH, "0", "priority"):
        new = vdf_set_app_value(new, STEAM_COMPAT_PATH, "0", "priority", "75")
    if vdf_get_app_value(new, STEAM_COMPAT_PATH, "0", "name") != internal:
        raise ValueError("Self-check failed: config.vdf does not read back as intended — aborted.")
    bak = _backup_file(cfg)
    _atomic_write(cfg, new)
    return bak.name


class _TMWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int, str)             # done, total, stage
    versions = Signal(str, object)               # tool id, List[TMVer]
    latest = Signal(object)                      # {(tool name, variant): TMVer}
    done = Signal(object)                        # List[result dict]
    failed = Signal(str)

    def __init__(self, mode: str, tool: Optional[TMTool] = None, launcher: Optional[TMLauncher] = None,
                 jobs=None, force: bool = False, tools=None, parent=None):
        super().__init__(parent)
        self.mode, self.tool, self.launcher = mode, tool, launcher
        self.jobs, self.force, self.tools = jobs or [], force, tools or []
        self._cancel = _threading.Event()
        self._t = 0.0

    def cancel(self):
        self._cancel.set()

    def _prog(self, stage, done, total):
        now = time.monotonic()
        if now - self._t > 0.1 or (total and done >= total):
            self._t = now
            self.progress.emit(done, total, stage)

    def run(self):
        try:
            if self.mode == "versions":
                self.versions.emit(self.tool.id, tm_versions(self.tool))
            elif self.mode == "latest":
                res = {}
                # ~25 independent API requests: run them in parallel, apply the results in tool order
                pool = ThreadPoolExecutor(max_workers=6)
                try:
                    futs = [(t, pool.submit(tm_versions, t, 12)) for t in self.tools]
                    for t, fut in futs:
                        if self._cancel.is_set():
                            raise _TMCancelled()
                        try:
                            for v in fut.result():
                                res.setdefault((t.name, tm_variant(t, v.label)), v)
                        except RuntimeError as e:
                            self.log.emit(f"⚠ {t.name}: {e}")
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
                self.latest.emit(res)
            else:                                # install / update: [(tool, ver, [old paths])]
                results = []
                for tool, ver, olds in self.jobs:
                    self.log.emit(f"{tool.name} {ver.label} → {self.launcher.label}")
                    r = tm_install(tool, ver, self.launcher, self.force, self._cancel,
                                   self.log.emit, self._prog)
                    self.log.emit(f"  ✔ {r['path']}" + (f"\n  ⚠ {r['note']}" if r["note"] else ""))
                    folder = r["path"].parent
                    for old in olds:
                        if old.resolve() != r["path"].resolve():
                            try:
                                tm_remove(old, folder)
                                self.log.emit(f"  removed old {old.name}")
                            except (OSError, RuntimeError) as e:
                                self.log.emit(f"  ⚠ could not remove {old.name}: {e}")
                    results.append(r)
                self.done.emit(results)
        except _TMCancelled:
            self.failed.emit("cancelled")
        except Exception as e:                   # noqa: BLE001
            self.failed.emit(str(e))


class ToolManagerWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: Optional[_TMWorker] = None
        self._launchers: List[TMLauncher] = []
        self._vers: Dict[str, List[TMVer]] = {}
        self._latest: dict = {}
        self._rows: List[TMInstalled] = []
        self._extra: List[TMLauncher] = []
        self._active = False                     # a worker is running (QThread.isRunning lags in `finished`)
        self._last = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        t = QLabel("Tool Manager")
        t.setStyleSheet(f"color:{T['text']}; font-size:15px; font-weight:700;")
        lay.addWidget(t)
        lay.addWidget(_desc_label(
            "Downloads Proton forks, Wine builds, DXVK and vkd3d straight into the right folder — "
            "Steam's compatibilitytools.d, Lutris, Heroic, Bottles or a folder of your choice. "
            "Archives are SHA-512-checked where the project publishes a checksum and unpacked into a "
            "staging folder first, so a failed install never leaves a half-written tool behind. "
            "Folders are shared with protondl: tools installed by either are recognised by both."))

        box = QGroupBox("Install")
        box.setStyleSheet(_TOOL_GROUP_SS)
        g = QVBoxLayout(box)
        r = QHBoxLayout()
        r.addWidget(QLabel("Install for"))
        self._lc = QComboBox()
        self._lc.setStyleSheet(_COMBO_QSS)
        self._lc.setMinimumWidth(340)
        self._lc.activated.connect(self._on_launcher)
        r.addWidget(self._lc, 1)
        b = _tool_btn("Rescan")
        b.clicked.connect(self._rescan)
        r.addWidget(b)
        b = _tool_btn("Open folder")
        b.clicked.connect(self._open_folder)
        r.addWidget(b)
        g.addLayout(r)
        r = QHBoxLayout()
        r.addWidget(QLabel("Tool"))
        self._tc = QComboBox()
        self._tc.setStyleSheet(_COMBO_QSS)
        self._tc.setMinimumWidth(300)
        self._tc.currentIndexChanged.connect(self._on_tool)
        r.addWidget(self._tc, 1)
        self._adv = QCheckBox("Show advanced / unstable")
        self._adv.toggled.connect(lambda _c: self._fill_tools())
        r.addWidget(self._adv)
        g.addLayout(r)
        self._info = QLabel("")
        self._info.setWordWrap(True)
        self._info.setOpenExternalLinks(True)
        self._info.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        g.addWidget(self._info)
        r = QHBoxLayout()
        r.addWidget(QLabel("Version"))
        self._vc = QComboBox()
        self._vc.setStyleSheet(_COMBO_QSS)
        self._vc.setMinimumWidth(300)
        self._vc.currentIndexChanged.connect(lambda _i: self._sel_changed())
        r.addWidget(self._vc, 1)
        self._bfetch = _tool_btn("Fetch versions")
        self._bfetch.clicked.connect(self._fetch)
        r.addWidget(self._bfetch)
        self._force = QCheckBox("Reinstall if present")
        r.addWidget(self._force)
        self._binst = _tool_btn("Install", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        self._binst.clicked.connect(self._install)
        r.addWidget(self._binst)
        self._bstop = _tool_btn("Cancel")
        self._bstop.clicked.connect(lambda: self._worker and self._worker.cancel())
        self._bstop.setEnabled(False)
        r.addWidget(self._bstop)
        g.addLayout(r)
        pr = QHBoxLayout()
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(8)
        self._bar.setStyleSheet(
            f"QProgressBar{{background:{T['sunken']};border:1px solid {T['border']};border-radius:4px;}}"
            f"QProgressBar::chunk{{background:{T['accent']};border-radius:3px;}}")
        pr.addWidget(self._bar, 1)
        self._stage = QLabel("")
        self._stage.setMinimumWidth(190)
        self._stage.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        pr.addWidget(self._stage)
        g.addLayout(pr)
        lay.addWidget(box)

        box = QGroupBox("Installed")
        box.setStyleSheet(_TOOL_GROUP_SS)
        g = QVBoxLayout(box)
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(["Tool", "Version", "Type", "Folder", "Installed", "Status"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.ExtendedSelection)
        hh = self._table.horizontalHeader()
        for c in range(5):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.Stretch)
        self._table.setStyleSheet(
            f"QTableWidget{{background:{T['surface']};border:1px solid {T['border']};color:{T['text2']};"
            f"gridline-color:{T['border']};}}QHeaderView::section{{background:{T['raised']};"
            f"color:{T['muted']};border:none;padding:4px;}}")
        self._table.setMinimumHeight(190)
        self._table.itemSelectionChanged.connect(self._sel_changed)
        g.addWidget(self._table)
        r = QHBoxLayout()
        b = _tool_btn("Refresh")
        b.clicked.connect(self._refresh)
        r.addWidget(b)
        self._bupd_chk = _tool_btn("Check for updates")
        self._bupd_chk.clicked.connect(self._check_updates)
        r.addWidget(self._bupd_chk)
        self._bupd = _tool_btn("Update selected", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        self._bupd.clicked.connect(self._update_selected)
        r.addWidget(self._bupd)
        self._rmold = QCheckBox("Remove the old version after updating")
        r.addWidget(self._rmold)
        r.addStretch(1)
        self._bdef = _tool_btn("Set as Steam default")
        self._bdef.setToolTip("Writes Steam's global Play-compatibility tool (config.vdf, with backup). "
                              "Steam must be closed.")
        self._bdef.clicked.connect(self._set_default)
        r.addWidget(self._bdef)
        self._brm = _tool_btn("Remove selected", bg="#3a1010", border="#7a2020", fg="#e07070")
        self._brm.clicked.connect(self._remove)
        r.addWidget(self._brm)
        g.addLayout(r)
        lay.addWidget(box)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(_MONO_BOX_QSS)
        self._log.setMinimumHeight(120)
        lay.addWidget(self._log, 1)
        self._rescan()

    # -- helpers --------------------------------------------------------------
    def _say(self, msg: str):
        self._log.appendPlainText(msg)

    def _launcher(self) -> Optional[TMLauncher]:
        return self._lc.currentData()

    def _busy(self, on: bool):
        self._active = on
        for w in (self._lc, self._tc, self._vc, self._bfetch, self._adv, self._bupd_chk, self._force):
            w.setEnabled(not on)
        self._bstop.setEnabled(on)
        if not on:
            self._bar.setRange(0, 100)
            self._bar.setValue(0)
            self._stage.setText("")
        self._sel_changed()

    def _start(self, w: _TMWorker, title: str):
        self._worker = w
        w.log.connect(self._say)
        w.progress.connect(self._on_progress)
        w.failed.connect(self._on_failed)
        w.finished.connect(lambda: self._busy(False))
        TASKS.track(w, title, _NAV("Tool Manager"), w.log, w.failed)
        self._busy(True)
        w.start()

    def _on_progress(self, done: int, total: int, stage: str):
        self._bar.setRange(0, total if total > 0 else 0)
        if total > 0:
            self._bar.setValue(min(done, total))
            self._stage.setText(f"{stage}  {ngx_human(done)} / {ngx_human(total)}" if stage == "Downloading"
                                else f"{stage}  {min(100, done * 100 // total)}%")
        else:
            self._stage.setText(stage)

    def _on_failed(self, msg: str):
        self._say("✘ cancelled" if msg == "cancelled" else f"✘ {msg}")

    # -- launchers / tools ----------------------------------------------------
    def _rescan(self):
        cur = self._launcher()
        self._launchers = tm_discover() + self._extra
        self._lc.clear()
        for l in self._launchers:
            self._lc.addItem(f"{l.label}  —  {l.root}", l)
        self._lc.addItem("Custom folder …", None)
        if cur is not None:
            for i in range(self._lc.count()):
                d = self._lc.itemData(i)
                if d is not None and d.root == cur.root and d.key == cur.key:
                    self._lc.setCurrentIndex(i)
                    break
        if self._lc.currentData() is None and self._launchers:
            self._lc.setCurrentIndex(0)
        self._last = self._lc.currentIndex()
        if not self._launchers:
            self._say("No Steam / Lutris / Heroic / Bottles installation found — use “Custom folder …”.")
        self._fill_tools()
        self._refresh()

    def _on_launcher(self, idx: int):
        if self._lc.itemData(idx) is None:                      # "Custom folder …"
            d = QFileDialog.getExistingDirectory(self, "Install tools into", str(Path.home()))
            if d:
                l = TMLauncher("custom", "Custom folder", Path(d))
                self._extra = [x for x in self._extra if x.root != l.root] + [l]
                self._rescan()
                for i in range(self._lc.count()):
                    x = self._lc.itemData(i)
                    if x is not None and x.key == "custom" and x.root == l.root:
                        self._lc.setCurrentIndex(i)
                        break
            else:
                self._lc.setCurrentIndex(self._last)
        self._last = self._lc.currentIndex()
        self._fill_tools()
        self._refresh()

    def _fill_tools(self):
        l = self._launcher()
        prev = self._tc.currentData()
        self._tc.blockSignals(True)
        self._tc.clear()
        if l is not None:
            for t in TM_TOOLS:
                if l.supports(t) and (self._adv.isChecked() or not t.advanced):
                    self._tc.addItem(f"{t.name}   [{TM_KIND_LABEL[t.kind]}]", t)
            for i in range(self._tc.count()):
                if prev is not None and self._tc.itemData(i).id == prev.id:
                    self._tc.setCurrentIndex(i)
                    break
        self._tc.blockSignals(False)
        self._on_tool()

    def _on_tool(self, _i=None):
        t = self._tc.currentData()
        self._vc.clear()
        if t is None:
            self._info.setText("")
            self._sel_changed()
            return
        self._info.setText(f"{t.desc}  <a href='{t.url}' style='color:{T['info']}'>{t.url}</a>")
        self._fill_versions(t)

    def _fill_versions(self, t: TMTool):
        self._vc.clear()
        vs = self._vers.get(t.id)
        if vs is None:
            self._vc.addItem("— press “Fetch versions” —", None)
        else:
            for v in vs:
                self._vc.addItem(v.label + (f"   ({v.date})" if v.date else ""), v)
            if not vs:
                self._vc.addItem("— no installable builds found —", None)
        self._sel_changed()

    def _open_folder(self):
        l, t = self._launcher(), self._tc.currentData()
        d = l.dir_for(t.kind if t else "proton") if l else None
        d = d or (l.root if l else None)
        if d is not None:
            d.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    # -- versions / install ---------------------------------------------------
    def _idle(self) -> bool:
        return not self._active

    def _fetch(self):
        t = self._tc.currentData()
        if t is None or not self._idle():
            return
        self._say(f"Fetching versions of {t.name} …")
        w = _TMWorker("versions", tool=t)
        w.versions.connect(self._on_versions)
        self._start(w, f"Tool Manager: versions of {t.name}")

    def _on_versions(self, tid: str, vs: list):
        self._vers[tid] = vs
        self._say(f"  {len(vs)} build(s) found." if vs else "  no installable build found.")
        t = self._tc.currentData()
        if t is not None and t.id == tid:
            self._fill_versions(t)

    def _install(self):
        t, v, l = self._tc.currentData(), self._vc.currentData(), self._launcher()
        if t is None or v is None or l is None or not self._idle():
            return
        dest = l.dir_for(t.kind)
        if dest is None:
            return
        replacing = tm_find_installed(dest, t.name, v.label)
        note = ("The existing installation will be replaced after a verified stage."
                if replacing is not None else "An additional tool installation will be created.")
        answer = QMessageBox.question(
            self, "Review tool installation",
            f"Tool: {t.name}\nVersion: {v.label}\nLauncher: {l.label}\n"
            f"Destination: {dest}\nArchive: {v.name or 'CI artifact'}\n"
            f"Checksum: {'SHA-512 published' if v.csum else 'not published; HTTPS only'}\n\n"
            f"{note}\n\nDownload and install?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        w = _TMWorker("install", launcher=l, jobs=[(t, v, [])], force=self._force.isChecked())
        w.done.connect(self._on_installed)
        self._start(w, f"Tool Manager: install {t.name} {v.label}")

    def _on_installed(self, results: list):
        l = self._launcher()
        self._refresh()
        if l is not None and l.key == "steam":
            self._say("Restart Steam to see the new tool under Properties › Compatibility.")
        toast(f"Installed {', '.join(r['tool'] + ' ' + r['version'] for r in results)}", "ok")

    # -- installed list -------------------------------------------------------
    def _refresh(self):
        l = self._launcher()
        self._rows = tm_scan(l) if l is not None else []
        self._table.setRowCount(len(self._rows))
        for r, e in enumerate(self._rows):
            vals = [e.tool or "—", e.version, TM_KIND_LABEL.get(e.kind, e.kind), e.path.name,
                    datetime.fromtimestamp(e.ts).strftime("%Y-%m-%d") if e.ts else "—",
                    "" if e.tool else "not installed by Tool Manager"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 0:
                    it.setData(Qt.UserRole, r)
                self._table.setItem(r, c, it)
        self._apply_latest()
        self._sel_changed()

    def _selected(self) -> List[TMInstalled]:
        rows = {i.row() for i in self._table.selectedIndexes()}
        return [self._rows[r] for r in sorted(rows) if r < len(self._rows)]

    def _sel_changed(self):
        sel = self._selected()
        idle = self._idle()
        l = self._launcher()
        self._binst.setEnabled(idle and self._vc.currentData() is not None)
        self._brm.setEnabled(idle and bool(sel))
        self._bupd.setEnabled(idle and any(self._update_for(e) for e in sel))
        self._bdef.setEnabled(idle and l is not None and l.key == "steam" and len(sel) == 1
                              and sel[0].kind == "proton")

    def _update_for(self, e: TMInstalled) -> Optional[TMVer]:
        t = TM_BY_NAME.get(e.tool)
        if t is None:
            return None
        lv = self._latest.get((t.name, tm_variant(t, e.version)))
        if lv is None or lv.label == e.version:
            return None
        if any(x.tool == t.name and x.version == lv.label for x in self._rows):
            return None
        return lv

    def _apply_latest(self):
        for r, e in enumerate(self._rows):
            if not e.tool:
                continue
            it = self._table.item(r, 5)
            lv = self._latest.get((e.tool, tm_variant(TM_BY_NAME[e.tool], e.version))) if e.tool in TM_BY_NAME else None
            if lv is None:
                it.setText("")
                continue
            up = self._update_for(e)
            it.setText(f"update available → {up.label}" if up else
                       "up to date" if lv.label == e.version else "older (newer one installed)")
            it.setForeground(QColor(T["warn"] if up else T["accent"] if lv.label == e.version else T["muted"]))

    def _check_updates(self):
        names = sorted({e.tool for e in self._rows if e.tool in TM_BY_NAME})
        if not names or not self._idle():
            self._say("Nothing to check — no tool installed by Tool Manager in this location.")
            return
        self._say("Checking for updates …")
        w = _TMWorker("latest", tools=[TM_BY_NAME[n] for n in names])
        w.latest.connect(self._on_latest)
        self._start(w, "Tool Manager: check updates")

    def _on_latest(self, res: dict):
        self._latest = res
        self._apply_latest()
        n = len({e.path for e in self._rows if self._update_for(e)})
        self._say(f"{n} update(s) available." if n else "Everything is up to date.")

    def _update_selected(self):
        l = self._launcher()
        sel = [e for e in self._selected() if self._update_for(e)]
        if l is None or not sel or not self._idle():
            return
        jobs: Dict[tuple, list] = {}
        for e in sel:
            t, v = TM_BY_NAME[e.tool], self._update_for(e)
            j = jobs.setdefault((t.id, v.label), [t, v, []])
            if self._rmold.isChecked():
                j[2].append(e.path)
        # force: tools with a fixed folder name (boxtron, luxtorpeda, SteamTinkerLaunch …)
        # unpack over the installed one, which without it always failed "already exists".
        w = _TMWorker("install", launcher=l, jobs=[tuple(j) for j in jobs.values()], force=True)
        w.done.connect(lambda _r: self._refresh())
        self._start(w, f"Tool Manager: update {len(jobs)} tool(s)")

    def _remove(self):
        l, sel = self._launcher(), self._selected()
        if l is None or not sel:
            return
        names = "\n".join(f"  • {e.path.name}" for e in sel[:15]) + (f"\n  … and {len(sel) - 15} more" if len(sel) > 15 else "")
        if QMessageBox.question(self, "Remove tools",
                                f"Delete {len(sel)} folder(s) from {sel[0].path.parent}?\n\n{names}\n\n"
                                "Games that use them fall back to another tool.") != QMessageBox.Yes:
            return
        for e in sel:
            try:
                tm_remove(e.path, e.path.parent)
                self._say(f"removed {e.path.name}")
            except (OSError, RuntimeError) as ex:
                self._say(f"✘ {e.path.name}: {ex}")
        self._refresh()

    def _set_default(self):
        l, sel = self._launcher(), self._selected()
        if l is None or len(sel) != 1:
            return
        if _proc_running("steam", "steamwebhelper"):
            QMessageBox.warning(self, "Steam is running",
                                "Close Steam first — it rewrites config.vdf on exit and would discard the change.")
            return
        internal = _tm_steam_internal(sel[0].path)
        if QMessageBox.question(self, "Steam default",
                                f"Make “{internal}” the default Steam Play tool for all games?\n"
                                "(config.vdf is backed up first.)") != QMessageBox.Yes:
            return
        try:
            bak = tm_set_steam_default(l.root, internal)
            self._say(f"Steam default → {internal}   (backup: {bak})")
            toast(f"Steam default: {internal}", "ok")
        except (OSError, ValueError) as ex:
            self._say(f"✘ {ex}")
            QMessageBox.critical(self, "Steam default", str(ex))
