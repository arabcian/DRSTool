"""NVIDIA NGX OTA client (DLSS / Streamline builds) used by the DLL Manager."""

import hashlib
import os
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QColor

from .theme import T


# ============================================================================
# NVIDIA NGX OTA client (used by Extra Tools → "DLL Manager")
# ----------------------------------------------------------------------------
# NVIDIA's NGX updater pulls feature DLLs from a public, listable S3 bucket.
# Server layout this client relies on:
#   <ns>/org/nvidia/team/ngx/models/<feature>/versions/<packed>/files/<arch>_<app>.<bin|zip>[.sha256]
#   * packed version      = major << 16 | minor << 8 | patch
#   * dlss* .bin payloads = plain PE DLLs  → saved as nvngx_<feature>.dll
#   * sl_sdk* .zip        = Streamline archive with sl.*.dll members
#   * <ns>/…/models/config/versions/2/files/nvngx_server_config.txt pins the
#     current version per feature ("[dlss]\napp_E658700 = 310.9.0")
# The listing endpoint stops at 1000 keys, so newer namespaces may be cut off.
# Discovery therefore also HEAD-checks the config pins and the next few minor
# versions after the newest one seen.
# Stdlib only (urllib / ElementTree / hashlib / zipfile) — no new deps.
# ============================================================================


NGX_HOST = "https://ngx.download.nvidia.com"
_NGX_MODELS = "org/nvidia/team/ngx/models"
_NGX_UA = "DRSTool/ngx-check (+https://github.com/arabcian/DRSTool)"
_NGX_KEY_RE = re.compile(
    r"^(?P<ns>[^/]+)/" + re.escape(_NGX_MODELS) +
    r"/(?P<feat>[^/]+)/versions/(?P<vid>\d+)/files/"
    r"(?P<payload>(?P<arch>[0-9A-Fa-f]+)_(?P<app>[0-9A-Fa-f]+)\.(?P<ext>bin|zip))"
    r"(?P<sha>\.sha256)?$")
_NGX_ARCH_UNIVERSAL = "160"          # Turing arch id; NVIDIA ships this build to every RTX gen
_NGX_APP_DLSS = "E658700"            # universal app id for nvngx_dlss*.dll
_NGX_APP_SL = "E658703"              # universal app id for the Streamline SDK zip
_NGX_TITLES = {
    "dlss": "DLSS Super Resolution",
    "dlssd": "DLSS Ray Reconstruction",
    "dlssg": "DLSS Frame Generation",
    "sl_sdk_0": "Streamline SDK",
}
_NGX_ORDER = ("dlss", "dlssd", "dlssg", "sl_sdk_0")
_NGX_PROBE_MINORS = 3                # look this many minor versions ahead
_NGX_PROBE_BUILDS = (0, 128)         # NVIDIA's usual patch numbers for new drops


def ngx_is_sl(feat: str) -> bool:
    return feat.startswith("sl_sdk")


def ngx_is_feature(feat: str) -> bool:
    """DLSS family (any future dlss* dir, minus the override bundles) + Streamline."""
    return ngx_is_sl(feat) or (feat.startswith("dlss") and "override" not in feat)


def ngx_title(feat: str) -> str:
    return _NGX_TITLES.get(feat, feat)


def ngx_target_name(feat: str) -> Optional[str]:
    """File name a game expects; None for Streamline (zip members are already named)."""
    return None if ngx_is_sl(feat) else f"nvngx_{feat}.dll"


def ngx_app_id(feat: str) -> str:
    return _NGX_APP_SL if ngx_is_sl(feat) else _NGX_APP_DLSS


def ngx_universal_payload(feat: str) -> str:
    return f"{_NGX_ARCH_UNIVERSAL}_{ngx_app_id(feat)}.{'zip' if ngx_is_sl(feat) else 'bin'}"


def ngx_feature_rank(feat: str):
    return (_NGX_ORDER.index(feat), "") if feat in _NGX_ORDER else (len(_NGX_ORDER), feat)


def ngx_ver_str(vid: int) -> str:
    return f"{vid >> 16}.{(vid >> 8) & 0xFF}.{vid & 0xFF}"


def ngx_ver_pack(text: str) -> Optional[int]:
    m = re.fullmatch(r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*", text or "")
    if not m:
        return None
    major, minor, patch = (int(g or 0) for g in m.groups())
    if minor > 0xFF or patch > 0xFF:
        return None
    return (major << 16) | (minor << 8) | patch


def ngx_human(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{int(v)} B" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{n} B"


def ngx_parse_server_config(text: str) -> List[tuple]:
    """'[dlss]\\napp_E658700 = 310.9.0' → [('dlss', 'E658700', '310.9.0'), …]"""
    pins, section = [], ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        key, sep, val = line.partition("=")
        key = key.strip()
        if sep and ngx_is_feature(section) and key.lower().startswith("app_"):
            pins.append((section, key[4:].upper(), val.strip()))
    return pins


class NgxCancelled(Exception):
    pass


@dataclass
class NgxBuild:
    """One downloadable payload (feature + version), possibly on several namespaces."""
    feature: str
    vid: int
    payload: str
    size: int = 0
    mirrors: Dict[str, bool] = field(default_factory=dict)   # ns → has .sha256 sidecar

    @property
    def version(self) -> str:
        return ngx_ver_str(self.vid)

    @property
    def verified(self) -> bool:
        return any(self.mirrors.values())

    def url(self, ns: str) -> str:
        return f"{NGX_HOST}/{ns}/{_NGX_MODELS}/{self.feature}/versions/{self.vid}/files/{self.payload}"

    def sources(self) -> List[tuple]:
        """[(url, sha_url | None)] — mirrors with a checksum first, then by name."""
        order = sorted(self.mirrors.items(), key=lambda kv: (not kv[1], kv[0]))
        return [(self.url(ns), self.url(ns) + ".sha256" if sha else None) for ns, sha in order]

    def tag(self) -> str:
        arch, app = self.payload.rsplit(".", 1)[0].split("_", 1)
        tags = []
        if arch.upper() != _NGX_ARCH_UNIVERSAL:
            tags.append(arch.upper())
        if app.upper() != ngx_app_id(self.feature):
            tags.append(f"app_{app.upper()}")
        return "/".join(tags)

    def rank(self) -> int:
        """Preference among payloads of the same version (universal build wins)."""
        arch, app = self.payload.rsplit(".", 1)[0].split("_", 1)
        return (app.upper() == ngx_app_id(self.feature)) * 2 + (arch.upper() == _NGX_ARCH_UNIVERSAL)


class NgxClient:
    """Discovery + download against the NGX bucket. Blocking; run it off the GUI thread."""

    def __init__(self, log=None, cancel: Optional[threading.Event] = None):
        self._log = log or (lambda level, msg: None)
        self._cancel = cancel or threading.Event()

    # ── HTTP ────────────────────────────────────────────────────────────────
    def _check(self):
        if self._cancel.is_set():
            raise NgxCancelled()

    def _open(self, url: str, method: str = "GET", timeout: float = 20):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": _NGX_UA})
        return urllib.request.urlopen(req, timeout=timeout)

    def _text(self, url: str, timeout: float = 20) -> str:
        with self._open(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")

    def _head(self, url: str) -> Optional[int]:
        """Content-Length if the object exists, else None."""
        try:
            with self._open(url, "HEAD", timeout=12) as r:
                return int(r.headers.get("Content-Length") or 0) if r.status == 200 else None
        except (urllib.error.URLError, OSError, ValueError):
            return None

    # ── discovery ───────────────────────────────────────────────────────────
    def list_bucket(self) -> List[tuple]:
        keys, token = [], ""
        for page in range(1, 101):
            self._check()
            q = {"list-type": "2", "max-keys": "1000"}
            if token:
                q["continuation-token"] = token
            root = ET.fromstring(self._text(f"{NGX_HOST}/?{urllib.parse.urlencode(q)}", timeout=30))
            n = 0
            for el in root.iter():
                if el.tag.rsplit("}", 1)[-1] != "Contents":
                    continue
                key = size = None
                for ch in el:
                    name = ch.tag.rsplit("}", 1)[-1]
                    if name == "Key":
                        key = ch.text
                    elif name == "Size":
                        size = ch.text
                if key:
                    keys.append((key, int(size or 0)))
                    n += 1
            self._log("info", f"listing page {page}: {n} objects")
            token = next((el.text or "" for el in root.iter()
                          if el.tag.rsplit("}", 1)[-1] == "NextContinuationToken"), "")
            if not token:
                return keys
        raise RuntimeError("bucket listing did not terminate after 100 pages")

    def discover(self) -> List[NgxBuild]:
        self._log("info", f"querying {NGX_HOST} …")
        builds: Dict[tuple, NgxBuild] = {}
        skipped: Dict[str, int] = {}

        def add(ns, feat, vid, payload, size, sidecar):
            b = builds.setdefault((feat, vid, payload), NgxBuild(feat, vid, payload))
            b.size = max(b.size, size)
            b.mirrors[ns] = b.mirrors.get(ns, False) or sidecar

        for key, size in self.list_bucket():
            m = _NGX_KEY_RE.match(key)
            if not m or not ngx_is_feature(m["feat"]) \
                    or (m["ext"] == "zip") != ngx_is_sl(m["feat"]):
                if _NGX_MODELS in key and not key.endswith("/"):
                    d = key.split(_NGX_MODELS + "/", 1)[1].split("/", 1)[0]
                    if d and d != "config" and not ngx_is_feature(d):
                        skipped[d] = skipped.get(d, 0) + 1
                continue
            sidecar = bool(m["sha"])
            add(m["ns"], m["feat"], int(m["vid"]), m["payload"], 0 if sidecar else size, sidecar)
        if not builds:
            raise RuntimeError("server answered but no DLSS/Streamline objects found — layout changed?")
        if skipped:
            self._log("info", "other NGX features ignored: " +
                      ", ".join(f"{d} ({n})" for d, n in sorted(skipped.items())))

        namespaces = sorted({ns for b in builds.values() for ns in b.mirrors})
        self._log("info", "namespaces: " + ", ".join(namespaces))

        # Candidate (ns, feature, vid) objects the listing may have missed.
        seen = {(f, v) for (f, v, _p) in builds}
        jobs = set()
        for ns in namespaces:
            self._check()
            url = f"{NGX_HOST}/{ns}/{_NGX_MODELS}/config/versions/2/files/nvngx_server_config.txt"
            try:
                pins = ngx_parse_server_config(self._text(url))
            except (urllib.error.URLError, OSError):
                continue
            for feat, app, ver in pins:
                vid = ngx_ver_pack(ver)
                if vid is not None and app == ngx_app_id(feat):
                    jobs.add((ns, feat, vid, "pin"))
        newest: Dict[str, int] = {}
        for (f, v) in seen:
            newest[f] = max(newest.get(f, 0), v)
        for f, v in newest.items():
            major, minor = v >> 16, (v >> 8) & 0xFF
            for mi in range(minor + 1, min(minor + _NGX_PROBE_MINORS, 0xFF) + 1):
                for build in _NGX_PROBE_BUILDS:
                    vid = (major << 16) | (mi << 8) | build
                    for ns in namespaces:
                        jobs.add((ns, f, vid, "probe"))
        # A pin for something already listed on that ns needs no check.
        jobs = {j for j in jobs
                if not ((j[1], j[2], ngx_universal_payload(j[1])) in builds
                        and j[0] in builds[(j[1], j[2], ngx_universal_payload(j[1]))].mirrors)}

        def check(job):
            if self._cancel.is_set():
                return job, None, False
            ns, feat, vid, _why = job
            url = NgxBuild(feat, vid, ngx_universal_payload(feat)).url(ns)
            size = self._head(url)
            return job, size, size is not None and self._head(url + ".sha256") is not None

        if jobs:
            self._log("info", f"checking {len(jobs)} pinned/staged candidates …")
            with ThreadPoolExecutor(max_workers=8) as pool:
                for (ns, feat, vid, why), size, sidecar in pool.map(check, sorted(jobs)):
                    if size is None:
                        continue
                    if (feat, vid) not in seen:
                        self._log("ok", f"{'config pin' if why == 'pin' else 'staged build'}: "
                                        f"{ngx_title(feat)} {ngx_ver_str(vid)} on {ns}")
                        seen.add((feat, vid))
                    add(ns, feat, vid, ngx_universal_payload(feat), size, sidecar)
        self._check()

        # One row per (feature, version): keep the universal payload when present.
        best: Dict[tuple, NgxBuild] = {}
        for b in builds.values():
            cur = best.get((b.feature, b.vid))
            if cur is None or b.rank() > cur.rank():
                best[(b.feature, b.vid)] = b
        return sorted(best.values(), key=lambda b: (ngx_feature_rank(b.feature), -b.vid))

    # ── download ────────────────────────────────────────────────────────────
    def download(self, b: NgxBuild, dest: Path, progress=None) -> tuple:
        """Fetch b into dest. Returns (written file names, sha256 hex, checksum_verified)."""
        dest.mkdir(parents=True, exist_ok=True)
        errors = []
        for i, (url, sha_url) in enumerate(b.sources()):
            if i:
                self._log("warn", "  trying next mirror …")
            try:
                return self._fetch(b, url, sha_url, dest, progress)
            except NgxCancelled:
                raise
            except Exception as e:  # noqa: BLE001 — every mirror failure is reported
                ns = urllib.parse.urlsplit(url).path.split("/")[1]
                errors.append(f"{ns}: {e}")
                self._log("warn", f"  {ns}: {e}")
        raise RuntimeError("; ".join(errors) or "no mirror available")

    def _fetch(self, b: NgxBuild, url: str, sha_url: Optional[str], dest: Path, progress) -> tuple:
        expected = None
        if sha_url:
            body = self._text(sha_url).split()
            expected = body[0].lower() if body else ""
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("malformed .sha256 sidecar")
        fd, tmp = tempfile.mkstemp(prefix=".ngx-", suffix=".part", dir=dest)
        try:
            h, done = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as out, self._open(url, timeout=30) as r:
                total = int(r.headers.get("Content-Length") or 0) or b.size
                while True:
                    self._check()
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            digest = h.hexdigest()
            if expected and digest != expected:
                raise ValueError(f"sha256 mismatch ({digest[:12]}… ≠ {expected[:12]}…)")
            if not expected and total and done != total:
                raise ValueError(f"truncated download ({done}/{total} bytes)")
            return self._install(b, Path(tmp), dest), digest, bool(expected)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _install(b: NgxBuild, tmp: Path, dest: Path) -> List[str]:
        name = ngx_target_name(b.feature)
        if name:
            with open(tmp, "rb") as f:
                if f.read(2) != b"MZ":
                    raise ValueError("payload is not a PE/DLL file")
            os.replace(tmp, dest / name)
            return [name]
        written = []
        with zipfile.ZipFile(tmp) as zf:
            for info in zf.infolist():
                base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
                if info.is_dir() or not base.lower().endswith(".dll") or base in (".", ".."):
                    continue
                fd, part = tempfile.mkstemp(prefix=".ngx-", suffix=".part", dir=dest)
                try:
                    with os.fdopen(fd, "wb") as out, zf.open(info) as src:
                        shutil.copyfileobj(src, out, 1 << 16)
                    os.replace(part, dest / base)
                finally:
                    if os.path.exists(part):
                        os.unlink(part)
                written.append(base)
        if not written:
            raise ValueError("Streamline archive contained no .dll files")
        return sorted(written)


class _NgxWorker(QThread):
    """Runs NgxClient.discover() or a batch download off the GUI thread."""
    log = Signal(str, str)                   # level, message
    progress = Signal(float, str)            # fraction 0..1, label
    discovered = Signal(object)              # List[NgxBuild]
    downloaded = Signal(int, int, str)       # ok, failed, dest dir
    failed = Signal(str)
    state = Signal(str, str)                 # feature, run | ok | err

    def __init__(self, builds: Optional[List[NgxBuild]] = None,
                 dest: Optional[Path] = None, parent=None):
        super().__init__(parent)
        self._builds, self._dest = builds, dest
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        client = NgxClient(lambda lvl, msg: self.log.emit(lvl, msg), self._cancel)
        try:
            if self._builds is None:
                self.discovered.emit(client.discover())
                return
            total = sum(b.size for b in self._builds) or 1
            base, ok, bad = 0, 0, 0
            for b in self._builds:
                label = f"{ngx_title(b.feature)} {b.version}"
                self.log.emit("info", f"↓ {label} ({ngx_human(b.size)})")
                self.state.emit(b.feature, "run")
                cb = lambda d, _t, base=base, label=label: \
                    self.progress.emit(min(1.0, (base + d) / total), label)
                try:
                    names, digest, verified = client.download(b, self._dest, cb)
                    ok += 1
                    self.state.emit(b.feature, "ok")
                    self.log.emit("ok", f"✓ {label} → {', '.join(names)}  "
                                        f"[sha256 {digest[:16]}… "
                                        f"{'verified' if verified else 'no sidecar, size-checked'}]")
                except NgxCancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    bad += 1
                    self.state.emit(b.feature, "err")
                    self.log.emit("err", f"✗ {label}: {e}")
                base += b.size
            self.progress.emit(1.0, "done")
            self.downloaded.emit(ok, bad, str(self._dest))
        except NgxCancelled:
            self.failed.emit("cancelled")
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


_NGX_ACCENTS = {
    "dlss": "#76b900",       # SR — NVIDIA green
    "dlssd": "#3fa7d6",      # RR — cyan
    "dlssg": "#b07cf0",      # FG — violet
    "sl_sdk_0": "#d2a03c",   # Streamline — amber
}
_NGX_SHORT = {"dlss": "SR", "dlssd": "RR", "dlssg": "FG", "sl_sdk_0": "SL"}


def _ngx_accent(feat: str) -> str:
    return _NGX_ACCENTS.get(feat, T["text2"])


def _ngx_rgba(hex_color: str, alpha: float) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"
