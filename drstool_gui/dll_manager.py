"""DLL Manager: per-game DLSS / Streamline / DXVK / vkd3d-proton / dxvk-nvapi versions and updates.
"""

import logging
import bisect
import hashlib as _hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QSettings, QStandardPaths, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .base import _CORE_ERR, _proc_running, APP_ID, dll_ops, game_scan, pe_version, user_cache_dir
from .launchers import BottlesBackend, SteamBackend
from .ngx import (
    _ngx_accent, _NgxWorker, ngx_human, ngx_is_sl, ngx_target_name, ngx_title, ngx_ver_str,
    NgxBuild, NgxCancelled, NgxClient,
)
from .tasks import _NAV, TASKS
from .theme import btn_ss, caption, T
from .toasts import toast
from .tool_ui import _tool_btn
from .widgets import (
    _COMBO_QSS, _desc_label, _LINEEDIT_QSS, _MONO_BOX_QSS, _pack2_table_ss, _pack2_title,
    _title_label, ElidedLabel,
)

_log = logging.getLogger(__name__)


# ============================================================================
# Prefix doctor: DXVK / vkd3d-proton / dxvk-nvapi inside a Wine prefix
# ============================================================================


PD_COMPONENTS = [
    {"id": "dxvk", "name": "DXVK", "repo": "doitsujin/dxvk",
     "dlls": ["d3d8", "d3d9", "d3d10core", "d3d11", "dxgi"],
     "markers": [b"DXVK", b"dxvk"], "ver_prefix_required": True,
     "asset_re": r"^dxvk-v?\d[\d.]*\.tar\.(gz|zst)$", "d64": "x64", "d32": "x32"},
    {"id": "vkd3d", "name": "vkd3d-proton", "repo": "HansKristian-Work/vkd3d-proton",
     "dlls": ["d3d12", "d3d12core"],
     "markers": [b"vkd3d-proton", b"VKD3D_CONFIG"], "ver_prefix_required": False,
     "asset_re": r"^vkd3d-proton-v?\d[\d.]*\.tar\.(zst|gz)$", "d64": "x64", "d32": "x86"},
    {"id": "nvapi", "name": "dxvk-nvapi", "repo": "jp7677/dxvk-nvapi",
     "dlls": ["nvapi64", "nvapi", "nvofapi64"],
     "markers": [b"DXVK-NVAPI", b"dxvk-nvapi", b"NVAPI"], "ver_prefix_required": True,
     "asset_re": r"^dxvk-nvapi-v?\d[\d.]*\.tar\.(gz|zst)$", "d64": "x64", "d32": "x32"},
]
_PD_UA = "DRSTool-prefix-doctor"
_PD_MANIFEST = ".drstool-doctor.json"


def _vtuple(s: str) -> tuple:
    m = re.search(r"(\d+(?:\.\d+)+)", s or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _vcmp(a: tuple, b: tuple) -> int:
    n = max(len(a), len(b), 3)
    a2, b2 = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a2 > b2) - (a2 < b2)


_DLL_VER_CACHE: Dict[tuple, Optional[str]] = {}


def scan_dll_version(path: Path, comp: dict) -> Optional[str]:
    """Cached wrapper: many games share one prefix, and a full scan of a 7 MB DXVK DLL is not free."""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size, comp["id"])
    except OSError:
        return _scan_dll_version(path, comp)
    if key not in _DLL_VER_CACHE:
        _DLL_VER_CACHE[key] = _scan_dll_version(path, comp)
    return _DLL_VER_CACHE[key]


def _scan_dll_version(path: Path, comp: dict) -> Optional[str]:
    """Heuristic: version-looking string nearest to the component's marker; falls back to
    the PE FileVersion resource. Returns None for 'not this component', '' for 'unknown'."""
    try:
        if path.stat().st_size > 80 * 1024 * 1024:
            return ""
        data = path.read_bytes()
    except OSError:
        return None
    pos = [p for p in (data.find(m) for m in comp["markers"]) if p >= 0]
    if not pos:
        # DXVK >= 3.0 moved D3D10 into d3d11.dll; its d3d10core.dll is a marker-less shim that
        # forwards to d3d11.dll. Wine's own builtin carries a "Wine builtin DLL" signature.
        if (comp["id"] == "dxvk" and path.stem.lower() == "d3d10core"
                and b"Wine builtin DLL" not in data[:1024] and b"D3D11CoreCreateDevice" in data):
            return ""
        return None                                    # a Wine builtin / something else
    anchor = min(pos)
    vpre = rb"v" if comp["ver_prefix_required"] else rb"v?"
    pat = re.compile(rb"(?<![\w.])(" + vpre + rb"\d+\.\d+(?:\.\d+)?(?:-\d+-g[0-9a-f]{6,})?[+\w\-]{0,12})\x00")
    # Only hits < 4096 bytes from the marker are accepted below, so scan just that window
    # (+256 slack for the match length) instead of the whole multi-MB DLL.
    cands = [(abs(m.start() - anchor), m.group(1).decode("ascii", "replace"))
             for m in pat.finditer(data, max(0, anchor - 4096), anchor + 4096 + 256)]
    if cands:
        cands.sort()
        if cands[0][0] < 4096:                         # must sit close to the marker
            return cands[0][1]
    # The first marker hit is often an unrelated string far from the version (e.g. a "DXVK_..."
    # env name in dxvk-nvapi), so also try the version string nearest to ANY marker hit.
    # Speed matters here: Python's re holds the GIL, so a slow pass over a 7 MB DXVK DLL froze the
    # UI. A leading lookbehind defeats re's literal prefix scan (~180 ms vs ~4 ms per DLL), so the
    # "not preceded by [\w.]" check is done by hand on each hit instead.
    marks = sorted({m.start() for mk in comp["markers"] for m in re.finditer(re.escape(mk), data)})[:4000]

    def clean_start(i: int) -> bool:
        return i == 0 or not (chr(data[i - 1]).isalnum() or data[i - 1] in b"_.")

    def dist(i: int) -> int:
        k = bisect.bisect_left(marks, i)
        return min(abs(i - marks[j]) for j in (k - 1, k) if 0 <= j < len(marks))

    def pick(hits, limit):
        best = None
        for m in hits:
            if not clean_start(m.start(1)):
                continue
            d = dist(m.start(1))
            if d <= limit and (best is None or d < best[0]):
                best = (d, m.group(1).decode("ascii", "replace"))
        return best[1] if best else None
    fast = re.compile(rb"(" + vpre + rb"\d+\.\d+(?:\.\d+)?(?:-\d+-g[0-9a-f]{6,})?[+\w\-]{0,12})\x00")
    v = pick(fast.finditer(data), 4096)
    if v:
        return v
    if comp["ver_prefix_required"]:
        # Release-tarball builds (Gentoo, Arch, ...) have no git tag, so meson's vcs_tag falls
        # back to the bare project version: "DXVK: \0" "3.1.1" without the leading "v".
        # Only hits within 128 bytes of a marker count, so scan just those windows.
        bare = re.compile(rb"(\d+\.\d+\.\d+)\x00")
        hits, end = [], -1
        for mp in marks:
            lo, hi = max(mp - 128, end), mp + 128 + 32
            if lo < hi:
                hits.extend(bare.finditer(data, lo, hi))   # overlap is fine: duplicates, no gaps
            end = max(end, mp + 128)
        v = pick(hits, 128)
        if v:
            return v
    # Last resort: the source directory baked into assert/log paths ("../dxvk-3.1.1/src/...").
    # Literal-first pattern + manual separator check: a leading [/\\] class made re crawl.
    for m in re.finditer(re.escape(comp["repo"].split("/")[1].encode()) + rb"-v?(\d+\.\d+(?:\.\d+)?)[/\\]", data):
        if m.start() > 0 and data[m.start() - 1] in b"/\\":
            return m.group(1).decode("ascii")
    m = re.search(rb"F\x00i\x00l\x00e\x00V\x00e\x00r\x00s\x00i\x00o\x00n\x00\x00\x00((?:[0-9A-Za-z.\-+_]\x00)+)", data)
    if m:
        v = m.group(1).decode("utf-16-le", "replace")
        if re.match(r"\d+\.\d+", v) and not v.startswith(("0.0", "1.0.0.0")):
            return v
    return ""


def _sha256(p: Path) -> str:
    h = _hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def prefix_sysdirs(prefix: Path):
    win = prefix / "drive_c" / "windows"
    s32, s64 = win / "system32", win / "syswow64"
    return win.is_dir(), s32, (s64 if s64.is_dir() else None)


def detect_installed(prefix: Path, comp: dict) -> dict:
    """-> {"state": none|ok|mixed|unknown, "version": str, "source": manifest|scan, "files": [...]}"""
    ok, s32, s64 = prefix_sysdirs(prefix)
    files = []
    for d, tag in ((s32, "system32"), (s64, "syswow64")):
        if d is None:
            continue
        for dll in comp["dlls"]:
            p = d / f"{dll}.dll"
            if p.is_file():
                files.append((f"{tag}/{dll}.dll", p))
    manifest = {}
    try:
        manifest = json.loads((prefix / _PD_MANIFEST).read_text()).get(comp["id"], {})
    except Exception:
        _log.debug("ignored error", exc_info=True)
        pass
    res = {"state": "none", "version": "", "source": "", "files": [r for r, _ in files]}
    if not files:
        return res
    # authoritative: DRSTool put these exact files there
    mf = manifest.get("files") or {}
    if mf and all(rel in mf and _sha256(p) == mf[rel] for rel, p in files if rel in mf) \
            and any(rel in mf for rel, _ in files):
        res.update(state="ok", version=manifest.get("version", ""), source="manifest")
        return res
    versions, other = set(), 0
    for rel, p in files:
        v = scan_dll_version(p, comp)
        if v is None:
            other += 1
        else:
            versions.add(v)
    if not versions:
        res["state"] = "none"                          # only Wine builtins present
        return res
    real = {v for v in versions if v}
    if len(real) > 1 or (other and real):
        res.update(state="mixed", version=" / ".join(sorted(real)) or "?", source="scan")
    elif real:
        res.update(state="ok", version=next(iter(real)), source="scan")
    else:
        res.update(state="unknown", version="?", source="scan")
    return res


def dll_override_gaps(prefix: Path, comp: dict) -> List[str]:
    try:
        t = (prefix / "user.reg").read_text(errors="replace")
    except OSError:
        return []
    m = re.search(r"^\[Software\\\\Wine\\\\DllOverrides\][^\n]*\n(.*?)(?:\n\[|\Z)", t, re.S | re.M)
    block = m.group(1) if m else ""
    have = {k.lower(): v.lower() for k, v in re.findall(r'^"([^"]+)"="([^"]*)"', block, re.M)}
    return [d for d in comp["dlls"]
            if d in ("d3d9", "d3d10core", "d3d11", "dxgi", "d3d12", "d3d12core", "d3d8", "nvapi", "nvapi64", "nvofapi64")
            and "native" not in have.get(d, "")]


def find_prefixes() -> List[tuple]:
    h = Path.home()
    cands: List[tuple] = []

    def add(label, p):
        try:
            p = Path(p)
            if (p / "drive_c" / "windows").is_dir() and all(str(p) != str(c[1]) for c in cands):
                cands.append((label, p))
        except OSError:
            pass
    if os.environ.get("WINEPREFIX"):
        add("$WINEPREFIX", os.environ["WINEPREFIX"])
    add("~/.wine", h / ".wine")
    for root in SteamBackend.roots():
        for lib in SteamBackend._libraries(root):
            cd = lib / "compatdata"
            if cd.is_dir():
                for d in sorted(cd.iterdir()):
                    add(f"Steam/Proton {d.name}", d / "pfx")
    for broot in BottlesBackend.roots():
        for d in sorted(broot.iterdir()):
            add(f"Bottles: {d.name}", d)
    for base in (h / "Games", h / ".local/share/lutris/prefixes"):
        if base.is_dir():
            for d in sorted(base.iterdir()):
                add(f"{base.name}/{d.name}", d)
                if d.is_dir():
                    for d2 in sorted(d.iterdir())[:40]:
                        add(f"{base.name}/{d.name}/{d2.name}", d2)
    return cands


def _gh_release(repo: str) -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"User-Agent": _PD_UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _pick_asset(rel: dict, comp: dict) -> Optional[dict]:
    pat = re.compile(comp["asset_re"])
    assets = [a for a in rel.get("assets", []) if pat.match(a.get("name", ""))]
    if not assets:
        return None
    assets.sort(key=lambda a: (a["name"].endswith(".zst"), a["name"]))   # prefer .gz
    return assets[0]


def _inside(base: Path, tgt: Path) -> bool:
    """Path containment that a plain startswith() gets wrong ("/c/pd-ab" vs "/c/pd-abX/…")."""
    try:
        return os.path.commonpath([str(base), str(tgt)]) == str(base)
    except ValueError:
        return False


def _safe_extract(tf: tarfile.TarFile, dest: Path):
    base = dest.resolve()
    for m in tf.getmembers():
        tgt = (dest / m.name).resolve()
        if not _inside(base, tgt) or m.issym() or m.islnk() or m.isdev():
            raise RuntimeError(f"unsafe path in archive: {m.name}")
    try:
        tf.extractall(dest, filter="data")          # Python >= 3.12 (and backports)
    except TypeError:
        tf.extractall(dest)


def _reject_links(root: Path):
    """External `tar` extraction gets no member vetting; refuse any link it produced
    (shutil.copy2 follows symlinks, so a planted x64/d3d11.dll -> ~/secret would
    otherwise be copied into the prefix)."""
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            if os.path.islink(os.path.join(dp, n)):
                raise RuntimeError(f"unsafe link in archive: {os.path.relpath(os.path.join(dp, n), root)}")


def _extract_archive(path: Path, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    if path.name.endswith(".zst"):
        try:
            with tarfile.open(path, "r:zst") as tf:           # Python >= 3.14
                _safe_extract(tf, dest)
                return
        except RuntimeError:
            raise
        except Exception:
            _log.debug("ignored error", exc_info=True)
            pass
        for cmd in (["tar", "--zstd", "-xf", str(path), "-C", str(dest)],
                    ["tar", "--use-compress-program=unzstd", "-xf", str(path), "-C", str(dest)]):
            try:
                if subprocess.run(cmd, capture_output=True, timeout=300).returncode == 0:
                    _reject_links(dest)
                    return
            except RuntimeError:
                raise
            except Exception:
                _log.debug("ignored error", exc_info=True)
                continue
        raise RuntimeError("cannot unpack .tar.zst — install zstd (app-arch/zstd) or use Python ≥ 3.14")
    with tarfile.open(path, "r:*") as tf:
        _safe_extract(tf, dest)


def _cache_dir() -> Path:
    d = Path(os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")) / "drstool" / "prefix-doctor"
    d.mkdir(parents=True, exist_ok=True)
    return d


def apply_component_update(prefix: Path, comp: dict, rel: dict, backup_root: Path, log) -> int:
    asset = _pick_asset(rel, comp)
    if asset is None:
        raise RuntimeError(f"no matching release asset for {comp['name']}")
    # tag_name comes from the network: never let it climb out of the cache dir.
    tag = re.sub(r"[^A-Za-z0-9._+-]", "_", str(rel.get("tag_name") or "latest")).lstrip(".") or "latest"
    cache = _cache_dir() / tag
    cache.mkdir(parents=True, exist_ok=True)
    arch = cache / asset["name"]
    if not arch.is_file() or arch.stat().st_size != asset.get("size", -1):
        log(f"  downloading {asset['name']} …")
        req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": _PD_UA})
        tmp = arch.with_suffix(arch.suffix + ".part")
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(tmp, arch)
    work = Path(tempfile.mkdtemp(prefix="pd-", dir=str(cache)))
    try:
        _extract_archive(arch, work)
        tops = [p for p in work.iterdir() if p.is_dir()]
        root = tops[0] if len(tops) == 1 else work
        ok, s32, s64 = prefix_sysdirs(prefix)
        if not ok:
            raise RuntimeError("not a Wine prefix (drive_c/windows missing)")
        jobs = []
        if s64 is not None:                            # 64-bit prefix
            jobs.append((root / comp["d64"], s32, "system32"))
            jobs.append((root / comp["d32"], s64, "syswow64"))
        else:                                          # 32-bit prefix
            jobs.append((root / comp["d32"], s32, "system32"))
        n, manifest_files = 0, {}
        for src, dst, tag in jobs:
            if not src.is_dir():
                continue
            for dll in comp["dlls"]:
                f = src / f"{dll}.dll"
                if not f.is_file():
                    continue
                target = dst / f.name
                if target.exists() or target.is_symlink():
                    bdir = backup_root / tag
                    bdir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, bdir / f.name)
                    target.unlink()
                shutil.copy2(f, target)
                manifest_files[f"{tag}/{f.name}"] = _sha256(target)
                n += 1
                log(f"  {tag}/{f.name}")
        mpath = prefix / _PD_MANIFEST
        try:
            man = json.loads(mpath.read_text())
        except Exception:
            _log.debug("ignored error", exc_info=True)
            man = {}
        man[comp["id"]] = {"version": rel.get("tag_name", "").lstrip("v"), "files": manifest_files,
                           "updated": datetime.now().isoformat(timespec="seconds")}
        mpath.write_text(json.dumps(man, indent=2))
        return n
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ============================================================================
# DLL Manager — one page for every game on the system:
#   * DLSS SR / RR / FG and Streamline DLLs inside the game folders
#     (builds from NVIDIA's NGX servers, see NgxClient)
#   * DXVK / vkd3d-proton / dxvk-nvapi inside the game's Wine prefix
#     (GitHub releases, see PD_COMPONENTS / apply_component_update)
# Replaces the former "DLSS Updater" and "Prefix Doctor" pages. Game discovery
# and the file swapping live in drstool_core (game_scan / dll_ops).
# ============================================================================


_DLLM_NGX = ("dlss", "dlssd", "dlssg", "sl_sdk_0")
_DLLM_PFX = ("dxvk", "vkd3d", "nvapi")
DLLM_COLS = [("dlss", "DLSS SR"), ("dlssd", "DLSS RR"), ("dlssg", "DLSS FG"), ("sl_sdk_0", "Streamline"),
             ("dxvk", "DXVK"), ("vkd3d", "vkd3d"), ("nvapi", "nvapi")]
_DLLM_PD = {c["id"]: c for c in PD_COMPONENTS}


def restore_prefix_backup(prefix: Path) -> tuple:
    """Copy the newest .drstool-backup/<stamp> DLLs back into the prefix. -> (files, stamp)."""
    bdir = prefix / ".drstool-backup"
    stamps = sorted(x for x in bdir.iterdir() if x.is_dir()) if bdir.is_dir() else []
    if not stamps:
        raise FileNotFoundError("no DLL backups in this prefix")
    last = stamps[-1]
    win = prefix / "drive_c" / "windows"
    n = 0
    for sub in ("system32", "syswow64"):
        for f in (last / sub).glob("*.dll") if (last / sub).is_dir() else []:
            dst = win / sub / f.name
            if dst.is_symlink():          # copy2 would write through the link (into Proton's dist)
                dst.unlink()
            shutil.copy2(f, dst)
            n += 1
    try:
        (prefix / _PD_MANIFEST).unlink()
    except OSError:
        pass
    shutil.rmtree(last, ignore_errors=True)    # restored -> the next restore goes one step further back
    return n, last.name


def dllm_cache_dir(b: "NgxBuild") -> Path:
    return user_cache_dir() / "dll-manager" / "ngx" / b.feature / b.version


@dataclass
class DllGameState:
    game: object                                  # game_scan.GameEntry
    dlls: object                                  # game_scan.GameDlls
    prefix: Dict[str, dict] = field(default_factory=dict)   # comp id -> detect_installed()


def dllm_prefix_updatable(st: DllGameState) -> bool:
    g = st.game
    return g.prefix is not None and g.prefix_manager != "Proton"


def dllm_plan(st: DllGameState, targets: Dict[str, object], install_missing: bool = False) -> dict:
    """What updating this game to `targets` would touch.
    targets: NGX feature -> NgxBuild, prefix component id -> GitHub release dict.
    -> {"files": [(FoundDll, NgxBuild)], "prefix": [component id]}"""
    files = []
    for kind in _DLLM_NGX:
        b = targets.get(kind)
        if b is None:
            continue
        tv = pe_version.parse_version(b.version)
        for d in st.dlls.of(kind):
            if not d.version or pe_version.compare(d.version, tv) != 0:
                files.append((d, b))
    prefix = []
    if dllm_prefix_updatable(st):
        for cid in _DLLM_PFX:
            rel = targets.get(cid)
            if not rel or _pick_asset(rel, _DLLM_PD[cid]) is None:
                continue
            inst = st.prefix.get(cid) or {"state": "none", "version": ""}
            if inst["state"] == "none":
                if install_missing:
                    prefix.append(cid)
                continue
            have, latest = _vtuple(inst.get("version", "")), _vtuple(rel.get("tag_name", ""))
            dev = bool(re.search(r"-\d+-g[0-9a-f]+", inst.get("version", "")))
            if inst["state"] != "ok" or not have or (latest and _vcmp(have, latest) < 0 and not dev):
                prefix.append(cid)
    return {"files": files, "prefix": prefix}


def dllm_cell(st: DllGameState, cid: str, targets: Dict[str, object]) -> tuple:
    """-> (text, state) with state one of none | ok | old | managed | unknown."""
    if cid in _DLLM_NGX:
        found = st.dlls.of(cid)
        if not found:
            return "—", "none"
        vers = sorted({d.version_str for d in found})
        txt = " / ".join(vers) + (f"  ×{len(found)}" if len(found) > 1 else "")
        b = targets.get(cid)
        if b is None:
            return txt, "unknown"
        tv = pe_version.parse_version(b.version)
        old = any(not d.version or pe_version.compare(d.version, tv) < 0 for d in found)
        return txt, ("old" if old else "ok")
    g = st.game
    if g.prefix is None:
        return "—", "none"
    if not dllm_prefix_updatable(st):
        return "Proton", "managed"
    inst = st.prefix.get(cid)
    if not inst or inst["state"] == "none":
        return "—", "none"
    if inst["state"] == "mixed":
        return "mixed", "old"
    if inst["state"] == "unknown":
        return "?", "old"
    rel = targets.get(cid)
    if not rel:
        return inst["version"], "unknown"
    return inst["version"], ("old" if cid in dllm_plan(st, {cid: rel})["prefix"] else "ok")


class _DllScanWorker(QThread):
    game_ready = Signal(object)          # DllGameState
    progress = Signal(float, str)
    done = Signal(int, object)           # games scanned, [errors]

    def __init__(self, custom: List[str], only: Optional[list] = None, parent=None):
        super().__init__(parent)
        self._custom, self._only = list(custom), only
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        errors: List[str] = []
        if self._only is None:
            self.progress.emit(0.0, "finding games …")
            res = game_scan.scan_all(custom=self._custom)
            games, errors = res.games, list(res.errors)
        else:
            games = list(self._only)
        n = len(games)
        # Many games share one prefix (every Lutris game on ~/.wine): inspect it once per scan
        # instead of re-reading and re-hashing the same DLLs for each game.
        seen_pfx: Dict[tuple, dict] = {}
        for i, g in enumerate(games):
            if self._cancel.is_set():
                break
            self.progress.emit(i / max(1, n), g.name)
            try:
                dlls = game_scan.find_dlls(g)
            except Exception as ex:                       # noqa: BLE001 - one game must not stop the scan
                errors.append(f"{g.name}: {ex}")
                dlls = game_scan.GameDlls()
            pref: Dict[str, dict] = {}
            if g.prefix is not None and g.prefix_manager != "Proton":
                for c in PD_COMPONENTS:
                    try:
                        k = (str(g.prefix), c["id"])
                        if k not in seen_pfx:
                            seen_pfx[k] = detect_installed(g.prefix, c)
                        pref[c["id"]] = seen_pfx[k]
                    except Exception as ex:               # noqa: BLE001
                        errors.append(f"{g.name} ({c['name']}): {ex}")
            self.game_ready.emit(DllGameState(g, dlls, pref))
        self.progress.emit(1.0, "")
        self.done.emit(n, errors)


class _DllOnlineWorker(QThread):
    log = Signal(str, str)
    progress = Signal(float, str)
    ready = Signal(object, object)       # [NgxBuild], {component id: release}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        builds, rels = [], {}
        self.progress.emit(0.1, "NGX servers …")
        try:
            builds = NgxClient(lambda lvl, msg: self.log.emit(lvl, msg), self._cancel).discover()
        except NgxCancelled:
            self.log.emit("warn", "cancelled")
        except Exception as ex:                           # noqa: BLE001
            self.log.emit("err", f"NGX: {ex}")
        for i, c in enumerate(PD_COMPONENTS):
            if self._cancel.is_set():
                break
            self.progress.emit(0.6 + 0.13 * i, f"GitHub: {c['name']} …")
            try:
                rels[c["id"]] = _gh_release(c["repo"])
            except Exception as ex:                       # noqa: BLE001
                self.log.emit("err", f"GitHub {c['repo']}: {ex}")
        self.progress.emit(1.0, "")
        self.ready.emit(builds, rels)


class _DllUpdateWorker(QThread):
    log = Signal(str, str)
    progress = Signal(float, str)
    finished_ok = Signal(int, int, object)   # files replaced, failures, [GameEntry] touched

    def __init__(self, jobs: list, rels: dict, parent=None):
        super().__init__(parent)
        self._jobs, self._rels = jobs, rels
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        client = NgxClient(lambda lvl, msg: self.log.emit(lvl, msg), self._cancel)
        replaced = failed = 0
        touched = []
        # 1) every NGX build once, into the cache
        needed = {}
        for _st, plan in self._jobs:
            for _d, b in plan["files"]:
                needed[(b.feature, b.vid)] = b
        src_dirs: Dict[tuple, Path] = {}
        for i, b in enumerate(needed.values()):
            if self._cancel.is_set():
                break
            d = dllm_cache_dir(b)
            name = ngx_target_name(b.feature)
            have = (d / name).is_file() if name else any(d.glob("sl.*.dll"))
            label = f"{ngx_title(b.feature)} {b.version}"
            if have:
                self.log.emit("info", f"cached: {label}")
                src_dirs[(b.feature, b.vid)] = d
                continue
            self.log.emit("info", f"↓ {label} ({ngx_human(b.size)})")
            self.progress.emit(i / max(1, len(needed)) * 0.5, f"downloading {label}")
            try:
                _names, digest, verified = client.download(b, d)
                src_dirs[(b.feature, b.vid)] = d
                self.log.emit("ok", f"✓ {label}  [sha256 {digest[:16]}… "
                                    f"{'verified' if verified else 'no sidecar, size-checked'}]")
            except NgxCancelled:
                break
            except Exception as ex:                       # noqa: BLE001
                failed += 1
                self.log.emit("err", f"✗ {label}: {ex}")
        # 2) game by game
        n = len(self._jobs)
        for gi, (st, plan) in enumerate(self._jobs):
            if self._cancel.is_set():
                self.log.emit("warn", "cancelled")
                break
            g = st.game
            self.progress.emit(0.5 + gi / max(1, n) * 0.5, g.name)
            self.log.emit("info", f"── {g.name}  [{g.launcher}]")
            pairs = []
            for d, b in plan["files"]:
                src_dir = src_dirs.get((b.feature, b.vid))
                if src_dir is None:
                    self.log.emit("warn", f"  – {d.path.name}: {ngx_title(b.feature)} {b.version} not downloaded")
                    continue
                if d.kind == game_scan.SL_FEATURE:
                    avail = {p.name.lower(): p for p in src_dir.glob("*.dll")}
                    for f in d.group:
                        src = avail.get(f.name.lower())
                        if src is not None:
                            pairs.append((f, src, f"Streamline {b.version}"))
                        else:
                            self.log.emit("warn", f"  – {f.name}: not part of Streamline {b.version}, left as is")
                else:
                    pairs.append((d.path, src_dir / ngx_target_name(b.feature), f"{ngx_title(b.feature)} {b.version}"))
            if pairs:
                try:
                    res = dll_ops.replace_files(g.key, g.name, pairs, lambda m: self.log.emit("info", m))
                    replaced += len(res.replaced)
                    failed += sum(1 for _p, why in res.skipped if why != "already this version")
                    if res.backup_dir is not None:
                        self.log.emit("info", f"  backup: {res.backup_dir}")
                except Exception as ex:                   # noqa: BLE001
                    failed += 1
                    self.log.emit("err", f"  ✗ {ex}")
            if plan["prefix"]:
                backup = g.prefix / ".drstool-backup" / datetime.now().strftime("%Y%m%d-%H%M%S")
                for cid in plan["prefix"]:
                    comp, rel = _DLLM_PD[cid], self._rels.get(cid)
                    self.log.emit("info", f"  {comp['name']} → {rel.get('tag_name', '?') if rel else '?'}")
                    try:
                        cnt = apply_component_update(g.prefix, comp, rel, backup,
                                                     lambda m: self.log.emit("info", "  " + m.strip()))
                        replaced += cnt
                        self.log.emit("ok", f"  ✓ {cnt} file(s) in the prefix")
                    except Exception as ex:               # noqa: BLE001
                        failed += 1
                        self.log.emit("err", f"  ✗ {comp['name']}: {ex}")
            touched.append(g)
        self.progress.emit(1.0, "")
        self.finished_ok.emit(replaced, failed, touched)


class DllRestoreDialog(QDialog):
    """Backups of one game: DLLs swapped in the game folder and DLLs replaced in its prefix."""

    def __init__(self, st: DllGameState, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Restore — {st.game.name}")
        self.resize(720, 420)
        self._st = st
        self.restored = False
        lay = QVBoxLayout(self)
        lay.addWidget(_pack2_title(f"Restore {st.game.name}",
                                   "Pick a backup. A file the game has replaced since then (an update) is "
                                   "left alone."))
        self._list = QTreeWidget()
        self._list.setHeaderLabels(["Backup", "Contents"])
        self._list.setStyleSheet(_pack2_table_ss())
        self._list.setRootIsDecorated(False)
        self._list.header().setSectionResizeMode(1, QHeaderView.Stretch)
        lay.addWidget(self._list, 1)
        for bdir, m in dll_ops.list_backups(st.game.key):
            labels = sorted({e.get("label", "?") for e in m.get("entries", [])})
            it = QTreeWidgetItem([f"game folder · {bdir.name}", ", ".join(labels) or "(empty)"])
            it.setData(0, Qt.UserRole, ("dll", bdir))
            self._list.addTopLevelItem(it)
        pb = st.game.prefix / ".drstool-backup" if st.game.prefix is not None else None
        if pb is not None and pb.is_dir():
            stamps = sorted((x for x in pb.iterdir() if x.is_dir()), reverse=True)
            if stamps:
                files = sorted({f.name for sub in ("system32", "syswow64")
                                for f in (stamps[0] / sub).glob("*.dll")} if stamps else set())
                it = QTreeWidgetItem([f"prefix · {stamps[0].name}", ", ".join(files)[:300]])
                it.setData(0, Qt.UserRole, ("prefix", st.game.prefix))
                it.setToolTip(0, "The newest prefix backup; restoring it makes the one before it the newest.")
                self._list.addTopLevelItem(it)
        if self._list.topLevelItemCount():
            self._list.setCurrentItem(self._list.topLevelItem(0))
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(_MONO_BOX_QSS)
        self._log.setFixedHeight(110)
        lay.addWidget(self._log)
        row = QHBoxLayout()
        row.addStretch(1)
        close = _tool_btn("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        self._go = QPushButton("Restore selected")
        self._go.setStyleSheet(btn_ss("primary"))
        self._go.setEnabled(self._list.topLevelItemCount() > 0)
        self._go.clicked.connect(self._restore)
        row.addWidget(self._go)
        lay.addLayout(row)
        if not self._list.topLevelItemCount():
            self._log.setPlainText("No backups for this game.")

    def _restore(self):
        it = self._list.currentItem()
        if it is None:
            return
        kind, where = it.data(0, Qt.UserRole)
        try:
            if kind == "dll":
                n, notes = dll_ops.restore(where, self._log.appendPlainText)
                for note in notes:
                    self._log.appendPlainText("  " + note)
            else:
                n, stamp = restore_prefix_backup(where)
                self._log.appendPlainText(f"  restored {n} file(s) from {stamp}")
        except (OSError, ValueError) as ex:
            self._log.appendPlainText(f"✗ {ex}")
            return
        self._log.appendPlainText(f"✓ {n} file(s) restored")
        self.restored = self.restored or n > 0
        if kind == "prefix":
            self._list.takeTopLevelItem(self._list.indexOfTopLevelItem(it))


class DllManagerWidget(QWidget):
    """Extra Tools › DLL Manager."""

    _LOG_COLORS = {"info": T["text2"], "ok": T["accent_hi"], "warn": T["warn"], "err": T["danger"]}
    _STATE_COLORS = {"none": T["faint"], "ok": T["accent"], "old": T["warn"], "managed": T["muted"],
                     "unknown": T["text2"]}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._qs = QSettings(APP_ID, APP_ID)
        self._states: Dict[str, DllGameState] = {}
        self._items: Dict[str, QTreeWidgetItem] = {}
        self._builds: List["NgxBuild"] = []
        self._rels: Dict[str, dict] = {}
        self._worker: Optional[QThread] = None
        self._scan_worker: Optional[_DllScanWorker] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("DLL Manager"))
        lay.addWidget(_desc_label(
            "Finds the games on this system (Steam, Lutris, Heroic, Bottles and folders you add) and "
            "updates the DLLs they ship: DLSS Super Resolution / Ray Reconstruction / Frame Generation "
            "and Streamline straight from NVIDIA's NGX servers, and DXVK / vkd3d-proton / dxvk-nvapi "
            "inside their Wine prefixes from GitHub. Only files a game already has are replaced; every "
            "replaced file is backed up and can be restored. Proton prefixes are kept in sync by Proton "
            "itself and are left alone. Launchers may put the original files back on a game update or "
            "“verify files”; games with anti-cheat may refuse modified DLLs (they are never selected "
            "automatically)."))
        if _CORE_ERR:
            lay.addWidget(_desc_label("⚠ " + _CORE_ERR))
            lay.addStretch(1)
            self._ok = False
            return
        self._ok = True

        top = QHBoxLayout()
        self._scan_btn = _tool_btn("⟳  Scan games", bold=True)
        self._scan_btn.clicked.connect(lambda: self.scan())    # not clicked(bool) -> only=False
        top.addWidget(self._scan_btn)
        self._online_btn = _tool_btn("Check for updates", bg="#1d2a10", border="#4a7300", fg="#cfe8a8", bold=True)
        self._online_btn.setToolTip("Lists every DLSS / Streamline build on ngx.download.nvidia.com (staged "
                                    "drops included) and the newest DXVK, vkd3d-proton and dxvk-nvapi releases")
        self._online_btn.clicked.connect(self.check_online)
        top.addWidget(self._online_btn)
        add = _tool_btn("Add folder…")
        add.setToolTip("A game folder or a Wine prefix that the scan does not find on its own")
        add.clicked.connect(self._add_folder)
        top.addWidget(add)
        self._rm_custom = _tool_btn("Forget folder")
        self._rm_custom.setToolTip("Remove the selected custom folder from the list")
        self._rm_custom.clicked.connect(self._forget_folder)
        top.addWidget(self._rm_custom)
        top.addStretch(1)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("filter games…")
        self._filter.setStyleSheet(_LINEEDIT_QSS)
        self._filter.setMaximumWidth(220)
        self._filter.textChanged.connect(lambda _t: self._apply_filter())
        top.addWidget(self._filter)
        self._show = QComboBox()
        self._show.setStyleSheet(_COMBO_QSS)
        for t in ("Games with DLLs", "Updates available", "All games"):
            self._show.addItem(t)
        self._show.currentIndexChanged.connect(lambda _i: self._apply_filter())
        top.addWidget(self._show)
        lay.addLayout(top)

        # target versions
        tgt = QFrame()
        tgt.setObjectName("dllmTargets")
        tgt.setAttribute(Qt.WA_StyledBackground, True)
        tgt.setStyleSheet(f"QFrame#dllmTargets {{ background:{T['raised']}; border:1px solid {T['border_hi']};"
                          f" border-radius:8px; }}")
        tg = QGridLayout(tgt)
        tg.setContentsMargins(10, 8, 10, 8)
        tg.setHorizontalSpacing(10)
        tg.setVerticalSpacing(4)
        tg.addWidget(caption("Update to"), 0, 0, 1, 4)
        self._t_on: Dict[str, QCheckBox] = {}
        self._t_ver: Dict[str, QComboBox] = {}
        for i, (cid, title) in enumerate(DLLM_COLS):
            r, c = 1 + i // 4, (i % 4)
            cell = QHBoxLayout()
            cb = QCheckBox(title)
            cb.setChecked(True)
            cb.setStyleSheet(f"color:{_ngx_accent(cid) if cid in _DLLM_NGX else T['text2']}; font-weight:700;")
            cb.toggled.connect(lambda _c: self._targets_changed())
            cell.addWidget(cb)
            combo = QComboBox()
            combo.setStyleSheet(_COMBO_QSS)
            combo.setMinimumWidth(110)
            combo.addItem("not checked yet")
            combo.setEnabled(False)
            combo.currentIndexChanged.connect(lambda _i: self._targets_changed())
            cell.addWidget(combo, 1)
            tg.addLayout(cell, r, c)
            self._t_on[cid], self._t_ver[cid] = cb, combo
        self._install_missing = QCheckBox("Also install DXVK / vkd3d / nvapi into prefixes that don't have it")
        self._install_missing.setStyleSheet(f"color:{T['text2']};")
        self._install_missing.toggled.connect(lambda _c: self._targets_changed())
        tg.addWidget(self._install_missing, 3, 0, 1, 4)
        lay.addWidget(tgt)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3 + len(DLLM_COLS))
        self._tree.setHeaderLabels(["Game", "Launcher"] + [t for _c, t in DLLM_COLS] + ["Notes"])
        self._tree.setStyleSheet(_pack2_table_ss())
        self._tree.setUniformRowHeights(True)
        self._tree.setSortingEnabled(True)
        self._tree.sortByColumn(0, Qt.AscendingOrder)
        self._tree.setMinimumHeight(300)
        hh = self._tree.header()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        self._tree.setColumnWidth(0, 260)
        for i in range(1, 2 + len(DLLM_COLS)):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2 + len(DLLM_COLS), QHeaderView.Stretch)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.currentItemChanged.connect(lambda *_: self._sync_buttons())
        lay.addWidget(self._tree, 1)

        row = QHBoxLayout()
        link_ss = (f"QPushButton{{background:transparent; border:none; color:{T['text2']}; font-size:12px;"
                   f" padding:2px 6px; text-decoration:underline;}} QPushButton:hover{{color:{T['accent_hi']};}}"
                   f" QPushButton:disabled{{color:{T['faint']};}}")
        self._sel_old = QPushButton("select games with updates")
        self._sel_none = QPushButton("none")
        for b, fn in ((self._sel_old, self._select_outdated), (self._sel_none, self._select_none)):
            b.setStyleSheet(link_ss)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        self._summary = QLabel("")
        self._summary.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        row.addWidget(self._summary)
        lay.addLayout(row)

        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(5)
        lay.addWidget(self._bar)

        btns = QHBoxLayout()
        self._bar_lbl = ElidedLabel()
        self._bar_lbl.setStyleSheet(f"color:{T['text2']}; font-size:11px;")
        btns.addWidget(self._bar_lbl, 1)
        self._restore_btn = _tool_btn("Restore…")
        self._restore_btn.setToolTip("Put back the DLLs a previous update replaced in the selected game")
        self._restore_btn.clicked.connect(self._restore)
        btns.addWidget(self._restore_btn)
        self._tricks_btn = _tool_btn("Windows components…")
        self._tricks_btn.setToolTip("Install runtime components (vcrun, d3dcompiler, d3dx9, xact) into the "
                                    "selected game's Wine prefix with winetricks or protontricks")
        self._tricks_btn.clicked.connect(self._open_tricks)
        btns.addWidget(self._tricks_btn)
        self._open_btn = _tool_btn("Open folder")
        self._open_btn.clicked.connect(self._open_folder)
        btns.addWidget(self._open_btn)
        self._dl_only = _tool_btn("Download only…")
        self._dl_only.setToolTip("Save the chosen DLSS / Streamline versions into a folder without "
                                 "touching any game")
        self._dl_only.clicked.connect(self._download_only)
        btns.addWidget(self._dl_only)
        self._cancel_btn = _tool_btn("Cancel")
        self._cancel_btn.clicked.connect(self._cancel)
        btns.addWidget(self._cancel_btn)
        self._update_btn = QPushButton("Update selected")
        self._update_btn.setStyleSheet(btn_ss("primary", "lg"))
        self._update_btn.clicked.connect(self._update)
        btns.addWidget(self._update_btn)
        lay.addLayout(btns)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(3000)
        self._log.setMinimumHeight(120)
        self._log.setStyleSheet(_MONO_BOX_QSS)
        lay.addWidget(self._log)

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        self._sync_buttons()
        QTimer.singleShot(0, self.scan)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _say(self, level: str, msg: str):
        color = self._LOG_COLORS.get(level, T["text2"])
        safe = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._log.appendHtml(f'<span style="color:{T["muted"]}">{datetime.now():%H:%M:%S}</span> '
                             f'<span style="color:{color}">{safe}</span>')

    def _custom(self) -> List[str]:
        v = self._qs.value("dllmgr/custom", [])
        if isinstance(v, str):
            v = [v] if v else []
        return [str(x) for x in (v or [])]

    def _busy(self) -> bool:
        return any(w is not None and w.isRunning() for w in (self._worker, self._scan_worker))

    def _open_tricks(self):
        st = self._current_state()
        if st is None or st.game.prefix is None:
            return
        from .winetricks_dialog import WinetricksDialog
        WinetricksDialog(st.game.prefix, st.game.name, self).exec()

    def _sync_buttons(self):
        if not self._ok:
            return
        busy = self._busy()
        st = self._current_state()
        self._scan_btn.setEnabled(not busy)
        self._online_btn.setEnabled(not busy)
        self._cancel_btn.setVisible(busy)
        sel = self._checked_states()
        plan_n = sum(self._plan_size(s) for s in sel)
        self._update_btn.setEnabled(not busy and plan_n > 0)
        self._update_btn.setText(f"Update {len(sel)} game(s) · {plan_n} item(s)" if sel else "Update selected")
        self._restore_btn.setEnabled(not busy and st is not None)
        self._tricks_btn.setEnabled(not busy and st is not None and st.game.prefix is not None)
        self._open_btn.setEnabled(st is not None)
        self._dl_only.setEnabled(not busy and any(isinstance(self._target(c), NgxBuild) for c in _DLLM_NGX))
        self._rm_custom.setEnabled(st is not None and st.game.launcher == "Custom")
        self._sel_old.setEnabled(not busy and bool(self._states))

    def _target(self, cid: str):
        if not self._t_on[cid].isChecked():
            return None
        combo = self._t_ver[cid]
        if not combo.isEnabled() or combo.currentIndex() < 0:
            return None
        return combo.currentData()

    def _targets(self) -> Dict[str, object]:
        return {cid: t for cid, _ in DLLM_COLS if (t := self._target(cid)) is not None}

    def _plan_size(self, st: DllGameState) -> int:
        p = dllm_plan(st, self._targets(), self._install_missing.isChecked())
        return len(p["files"]) + len(p["prefix"])

    def _current_state(self) -> Optional[DllGameState]:
        it = self._tree.currentItem()
        while it is not None and it.parent() is not None:
            it = it.parent()
        return self._states.get(it.data(0, Qt.UserRole)) if it is not None else None

    def _checked_states(self) -> List[DllGameState]:
        out = []
        for key, it in self._items.items():
            if it.checkState(0) == Qt.Checked and key in self._states:
                out.append(self._states[key])
        return out

    # ── scan ────────────────────────────────────────────────────────────────
    def scan(self, only: Optional[list] = None):
        if not self._ok or self._busy():
            return
        if only is None:
            self._tree.clear()
            self._states.clear()
            self._items.clear()
        w = _DllScanWorker(self._custom(), only, self)
        self._scan_worker = w
        w.game_ready.connect(self._on_game)
        w.progress.connect(self._on_progress)
        w.done.connect(self._on_scan_done)
        w.finished.connect(self._sync_buttons)
        TASKS.track(w, "DLL Manager: scan", _NAV("DLL Manager"), w.progress)
        self._bar_lbl.setText("scanning …")
        w.start()
        self._sync_buttons()

    def _on_progress(self, frac: float, label: str):
        self._bar.setValue(int(frac * 1000))
        if label:
            self._bar_lbl.setText(label)

    def _on_game(self, st: DllGameState):
        key = st.game.key
        self._states[key] = st
        self._tree.setSortingEnabled(False)
        self._tree.blockSignals(True)
        it = self._items.get(key)
        if it is None:
            it = QTreeWidgetItem()
            it.setData(0, Qt.UserRole, key)
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Unchecked)
            self._tree.addTopLevelItem(it)
            self._items[key] = it
        else:
            it.takeChildren()
        g = st.game
        it.setText(0, g.name)
        it.setToolTip(0, "\n".join(str(f) for f in g.folders) + (f"\nprefix: {g.prefix}" if g.prefix else ""))
        it.setText(1, g.launcher + (f" · {g.note}" if g.launcher == "Heroic" and g.note else ""))
        for d in st.dlls.dlls:
            child = QTreeWidgetItem([str(d.path.parent), ""])
            col = 2 + [c for c, _t in DLLM_COLS].index(d.kind)
            child.setText(col, d.version_str)
            child.setToolTip(0, "\n".join(str(p) for p in (d.group or [d.path])))
            child.setForeground(0, QColor(T["muted"]))
            it.addChild(child)
        self._paint(it, st)
        self._tree.blockSignals(False)
        self._tree.setSortingEnabled(True)
        self._apply_filter_item(it)

    def _paint(self, it: QTreeWidgetItem, st: DllGameState):
        targets = self._targets_all()
        for i, (cid, _t) in enumerate(DLLM_COLS):
            txt, state = dllm_cell(st, cid, targets)
            it.setText(2 + i, txt)
            it.setForeground(2 + i, QColor(self._STATE_COLORS[state]))
        notes = []
        if st.dlls.anti_cheat:
            notes.append("⚠ anti-cheat: " + ", ".join(st.dlls.anti_cheat[:2]))
        if st.dlls.truncated:
            notes.append("folder too large, scan stopped early")
        if st.game.prefix_manager == "Bottles":
            notes.append("Bottles manages DXVK/vkd3d")
        it.setText(2 + len(DLLM_COLS), "  ·  ".join(notes))
        it.setToolTip(2 + len(DLLM_COLS), "\n".join(notes + ([st.game.note] if st.game.note and
                                                             st.game.launcher != "Heroic" else [])))
        it.setForeground(2 + len(DLLM_COLS), QColor(T["warn"] if st.dlls.anti_cheat else T["muted"]))

    def _targets_all(self) -> Dict[str, object]:
        """Newest-known targets for colouring, independent of the include ticks."""
        out = {}
        for cid, _ in DLLM_COLS:
            combo = self._t_ver[cid]
            if combo.isEnabled() and combo.currentIndex() >= 0 and combo.currentData() is not None:
                out[cid] = combo.currentData()
        return out

    def _on_scan_done(self, n: int, errors: list):
        for e in errors[:20]:
            self._say("warn", e)
        with_dlls = sum(1 for s in self._states.values() if s.dlls.dlls or s.prefix)
        self._bar_lbl.setText(f"{n} game(s) scanned · {with_dlls} with DLLs DRSTool can update")
        self._say("ok", f"scan finished: {len(self._states)} games, {with_dlls} with updatable DLLs")
        self._apply_filter()
        self._sync_buttons()

    def _apply_filter(self):
        for it in self._items.values():
            self._apply_filter_item(it)
        self._update_summary()

    def _apply_filter_item(self, it: QTreeWidgetItem):
        st = self._states.get(it.data(0, Qt.UserRole))
        if st is None:
            return
        t = self._filter.text().strip().casefold()
        mode = self._show.currentIndex()
        hide = bool(t) and t not in st.game.name.casefold()
        if mode == 0:
            hide = hide or not (st.dlls.dlls or any(v.get("state") != "none" for v in st.prefix.values()))
        elif mode == 1:
            hide = hide or self._plan_size_all(st) == 0
        it.setHidden(hide)

    def _plan_size_all(self, st: DllGameState) -> int:
        p = dllm_plan(st, self._targets_all(), False)
        return len(p["files"]) + len(p["prefix"])

    def _update_summary(self):
        shown = sum(1 for it in self._items.values() if not it.isHidden())
        self._summary.setText(f"{shown} of {len(self._items)} games shown")

    def _on_item_changed(self, it: QTreeWidgetItem, col: int):
        if col != 0 or it.parent() is not None:
            return
        st = self._states.get(it.data(0, Qt.UserRole))
        if st is not None and it.checkState(0) == Qt.Checked and st.dlls.anti_cheat \
                and not getattr(self, "_bulk", False):
            if QMessageBox.question(self, "Anti-cheat", f"{st.game.name} ships "
                                    f"{', '.join(st.dlls.anti_cheat)}. Anti-cheat software may refuse to start "
                                    "the game or flag the account when DLLs are changed. Select it anyway?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                self._tree.blockSignals(True)
                it.setCheckState(0, Qt.Unchecked)
                self._tree.blockSignals(False)
        self._sync_buttons()

    def _select_outdated(self):
        self._bulk = True
        try:
            for key, it in self._items.items():
                st = self._states[key]
                want = (not it.isHidden() and not st.dlls.anti_cheat and self._plan_size(st) > 0)
                it.setCheckState(0, Qt.Checked if want else Qt.Unchecked)
        finally:
            self._bulk = False
        self._sync_buttons()

    def _select_none(self):
        self._bulk = True
        try:
            for it in self._items.values():
                it.setCheckState(0, Qt.Unchecked)
        finally:
            self._bulk = False
        self._sync_buttons()

    def _targets_changed(self):
        for key, it in self._items.items():
            self._paint(it, self._states[key])
        self._apply_filter()
        self._sync_buttons()

    # ── online ──────────────────────────────────────────────────────────────
    def check_online(self):
        if not self._ok or self._busy():
            return
        w = _DllOnlineWorker(self)
        self._worker = w
        w.log.connect(self._say)
        w.progress.connect(self._on_progress)
        w.ready.connect(self._on_online)
        w.finished.connect(self._sync_buttons)
        TASKS.track(w, "DLL Manager: check for updates", _NAV("DLL Manager"), w.progress)
        self._say("info", "checking NVIDIA NGX and GitHub …")
        w.start()
        self._sync_buttons()

    def _on_online(self, builds: list, rels: dict):
        self._builds, self._rels = builds, rels
        by_feat: Dict[str, List["NgxBuild"]] = {}
        for b in builds:                       # sorted: feature order, newest first
            by_feat.setdefault(b.feature, []).append(b)
        for cid, _t in DLLM_COLS:
            combo = self._t_ver[cid]
            combo.blockSignals(True)
            combo.clear()
            if cid in _DLLM_NGX:
                for i, b in enumerate(by_feat.get(cid, [])):
                    tag = f" [{b.tag()}]" if b.tag() else ""
                    combo.addItem(f"{b.version}{tag}" + ("  (newest)" if i == 0 else ""), b)
            elif rels.get(cid):
                combo.addItem(str(rels[cid].get("tag_name", "?")).lstrip("v") + "  (latest release)", rels[cid])
            if not combo.count():
                combo.addItem("unavailable", None)
            combo.setEnabled(combo.itemData(0) is not None)
            combo.blockSignals(False)
        n = sum(len(v) for v in by_feat.values())
        self._say("ok", f"{n} NGX builds, {len(rels)} GitHub releases")
        self._show.setCurrentIndex(1 if self._states else 0)
        self._targets_changed()

    # ── update ──────────────────────────────────────────────────────────────
    def _update(self):
        if self._busy():
            return
        targets = self._targets()
        jobs = []
        files = prefix = 0
        ac = []
        for st in self._checked_states():
            plan = dllm_plan(st, targets, self._install_missing.isChecked())
            if plan["files"] or plan["prefix"]:
                jobs.append((st, plan))
                files += len(plan["files"])
                prefix += len(plan["prefix"])
                if st.dlls.anti_cheat:
                    ac.append(st.game.name)
        if not jobs:
            self._say("info", "nothing to update for the selected games and versions")
            return
        tl = [f"{t}: {self._t_ver[c].currentText().split('  (')[0]}" for c, t in DLLM_COLS if c in targets]
        msg = (f"Update {len(jobs)} game(s)?\n\n{files} DLSS/Streamline location(s) and {prefix} prefix "
               f"component(s).\nVersions: " + ", ".join(tl) +
               "\n\nOriginals are backed up (Restore… puts them back).")
        if ac:
            msg += "\n\n⚠ Anti-cheat present in: " + ", ".join(ac[:5])
        if QMessageBox.question(self, "DLL Manager", msg, QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        running = [n for n in ("steam", "lutris", "heroic", "bottles") if _proc_running(n)]
        w = _DllUpdateWorker(jobs, self._rels, self)
        self._worker = w
        w.log.connect(self._say)
        w.progress.connect(self._on_progress)
        w.finished_ok.connect(self._on_updated)
        w.finished.connect(self._sync_buttons)
        TASKS.track(w, "DLL Manager: update", _NAV("DLL Manager"), w.progress)
        if running:
            self._say("warn", "running: " + ", ".join(running) + " — a game that is open keeps the old DLLs "
                      "until it is restarted")
        w.start()
        self._sync_buttons()

    def _on_updated(self, replaced: int, failed: int, touched: list):
        text = f"{replaced} file(s) updated" + (f", {failed} problem(s) — see the log" if failed else "")
        self._say("ok" if not failed else "warn", text)
        self._bar_lbl.setText(text)
        toast(f"DLL Manager: {text}", "ok" if not failed else "warn")
        if touched:
            QTimer.singleShot(0, lambda: self.scan(only=touched))

    # ── misc actions ────────────────────────────────────────────────────────
    def _restore(self):
        st = self._current_state()
        if st is None:
            return
        dlg = DllRestoreDialog(st, self)
        dlg.exec()
        if dlg.restored:
            self.scan(only=[st.game])

    def _open_folder(self):
        st = self._current_state()
        if st is None:
            return
        target = st.game.folders[0] if st.game.folders else st.game.prefix
        if target is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Game folder or Wine prefix", str(Path.home()))
        if not d:
            return
        cur = self._custom()
        if d not in cur:
            cur.append(d)
            self._qs.setValue("dllmgr/custom", cur)
        e = game_scan.custom_entry(Path(d))
        if e is not None:
            self.scan(only=[e])

    def _forget_folder(self):
        st = self._current_state()
        if st is None or st.game.launcher != "Custom":
            return
        path = st.game.key.split(":", 1)[1]
        self._qs.setValue("dllmgr/custom", [c for c in self._custom()
                                            if str(Path(os.path.expanduser(c))) != path])
        it = self._items.pop(st.game.key, None)
        self._states.pop(st.game.key, None)
        if it is not None:
            self._tree.takeTopLevelItem(self._tree.indexOfTopLevelItem(it))
        self._sync_buttons()

    def _download_only(self):
        sel = [b for c in _DLLM_NGX if isinstance(b := self._target(c), NgxBuild)]
        if not sel or self._busy():
            return
        base = self._qs.value("ngx/base_dir", "") or \
            (QStandardPaths.writableLocation(QStandardPaths.DownloadLocation) or str(Path.home()))
        d = QFileDialog.getExistingDirectory(self, "Save the DLLs into…", str(base))
        if not d:
            return
        self._qs.setValue("ngx/base_dir", d)
        dlss = [b.vid for b in sel if not ngx_is_sl(b.feature)]
        sub = ngx_ver_str(max(dlss)) if dlss else ngx_ver_str(max(b.vid for b in sel))
        dest = Path(d) / sub
        w = _NgxWorker(sel, dest, parent=self)
        self._worker = w
        w.log.connect(self._say)
        w.progress.connect(self._on_progress)
        w.downloaded.connect(lambda ok, bad, where: (
            self._say("ok" if not bad else "warn", f"saved {ok} build(s) to {where}" + (f", {bad} failed" if bad else "")),
            QDesktopServices.openUrl(QUrl.fromLocalFile(where)) if ok else None))
        w.failed.connect(lambda m: self._say("err", m))
        w.finished.connect(self._sync_buttons)
        TASKS.track(w, "DLL Manager: download", _NAV("DLL Manager"), w.progress, w.failed)
        self._say("info", f"downloading {len(sel)} build(s) to {dest}")
        w.start()
        self._sync_buttons()

    def _cancel(self):
        for w in (self._worker, self._scan_worker):
            if w is not None and w.isRunning() and hasattr(w, "cancel"):
                w.cancel()

    def shutdown(self):
        self._cancel()
        for w in (self._worker, self._scan_worker):
            if w is not None:
                w.wait(5000)
