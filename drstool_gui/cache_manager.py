"""Shader / driver cache manager."""

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .base import _fmt_bytes, APP_ID
from .sysinfo import SYSHUB
from .tasks import _NAV, TASKS
from .theme import T
from .tool_ui import _tool_btn
from .widgets import _pack2_table_ss, _pack2_title


# ----------------------------------------------------------------------------
# Shader / pipeline cache manager
# ----------------------------------------------------------------------------

def _path_stats(path: str) -> tuple:
    """(disk bytes, file count, newest mtime) of a file or directory tree; symlinks not followed."""
    total = files = 0
    newest = 0.0
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0, 0.0
    if not os.path.isdir(path) or os.path.islink(path):
        return getattr(st, "st_blocks", 0) * 512 or st.st_size, 1, st.st_mtime
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                            continue
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += getattr(s, "st_blocks", 0) * 512 or s.st_size
                    files += 1
                    if s.st_mtime > newest:
                        newest = s.st_mtime
        except OSError:
            continue
    return total, files, newest


def _steam_roots() -> List[Path]:
    home = Path.home()
    cands = [home / ".local/share/Steam", home / ".steam/steam", home / ".steam/root",
             home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    seen, out = set(), []
    for c in cands:
        try:
            rp = c.resolve()
        except OSError:
            continue
        if rp.is_dir() and rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def _steam_libraries() -> List[Path]:
    libs, seen = [], set()
    for root in _steam_roots():
        paths = [root]
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            paths += [Path(m) for m in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace"))]
        except OSError:
            pass
        for p in paths:
            try:
                rp = p.resolve()
            except OSError:
                continue
            if rp not in seen and (rp / "steamapps").is_dir():
                seen.add(rp)
                libs.append(rp)
    return libs


def _classify_cache_entry(name: str) -> tuple:
    """(group, driver_bound) for an entry inside steamapps/shadercache/<appid>/."""
    n = name.lower()
    if n.startswith("nvidia"):
        return "NVIDIA GL / compute", True
    if n.startswith("vkd3d"):
        return "vkd3d-proton", True
    if "dxvk" in n:
        return "DXVK", False
    if n.startswith(("mesa", "radv", "foz", "fossilize")):
        return "Mesa / Fossilize", False
    return "Other", False


CACHE_GROUP_ORDER = ["NVIDIA GL / compute", "vkd3d-proton", "DXVK", "Mesa / Fossilize",
                     "Lutris (per game)", "Custom folders", "Other"]


def discover_cache_nodes(user_env: Dict[str, str], extra: List[str]) -> List[dict]:
    """Every cache location we know about. Pure filesystem work — safe in a thread."""
    home = Path.home()
    xdg_cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")

    def env(name: str) -> str:
        return (user_env.get(name) or os.environ.get(name) or "").strip()

    nodes: List[dict] = []
    seen = set()

    def add(group: str, label: str, path, driver: bool = False, note: str = "", user: bool = False):
        p = str(path)
        try:
            rp = os.path.realpath(p)
        except OSError:
            return
        if rp in seen or not os.path.exists(p):
            return
        seen.add(rp)
        nodes.append({"group": group, "label": label, "path": p, "driver": driver, "note": note,
                      "user_path": user})

    # NVIDIA driver caches (keyed by driver version → stale after an update)
    for var in ("__GL_SHADER_DISK_CACHE_PATH",):
        if env(var):
            add("NVIDIA GL / compute", f"{var} (your setting)", os.path.expanduser(env(var)), True,
                user=True)
    add("NVIDIA GL / compute", "OpenGL/Vulkan shader cache (~/.nv/GLCache)", home / ".nv/GLCache", True)
    add("NVIDIA GL / compute", "OpenGL/Vulkan shader cache (~/.cache/nvidia/GLCache)",
        xdg_cache / "nvidia/GLCache", True)
    add("NVIDIA GL / compute", "CUDA compute cache (~/.nv/ComputeCache)", home / ".nv/ComputeCache", True)
    add("NVIDIA GL / compute", "CUDA compute cache (~/.cache/nvidia/ComputeCache)",
        xdg_cache / "nvidia/ComputeCache", True)
    # Mesa
    md = env("MESA_SHADER_CACHE_DIR")
    for base in ([Path(md)] if md else []) + [xdg_cache]:
        add("Mesa / Fossilize", f"Mesa shader cache ({base})", base / "mesa_shader_cache")
        add("Mesa / Fossilize", f"Mesa shader cache DB ({base})", base / "mesa_shader_cache_db")
    add("Mesa / Fossilize", "RADV built-in shaders", xdg_cache / "radv_builtin_shaders")
    # user-configured paths
    for var, grp, drv in (("DXVK_STATE_CACHE_PATH", "DXVK", False), ("DXVK_SHADER_CACHE_PATH", "DXVK", False),
                          ("VKD3D_SHADER_CACHE_PATH", "vkd3d-proton", True)):
        v = env(var)
        if v and v != "0":
            add(grp, f"{var} (your setting)", os.path.expanduser(v), drv, user=True)
    # Lutris keeps per-game shader caches here
    lut = xdg_cache / "lutris" / "shaders"
    if lut.is_dir():
        try:
            for sub in sorted(lut.iterdir()):
                add("Lutris (per game)", sub.name, sub)
        except OSError:
            pass
    # Steam: shadercache/<appid>/<kind>
    for lib in _steam_libraries():
        sc = lib / "steamapps" / "shadercache"
        if not sc.is_dir():
            continue
        try:
            apps = sorted(sc.iterdir())
        except OSError:
            continue
        for app in apps:
            if not app.name.isdigit() or not app.is_dir():
                continue
            gname = app.name
            try:
                m = re.search(r'"name"\s+"([^"]+)"',
                              (lib / "steamapps" / f"appmanifest_{app.name}.acf").read_text(errors="replace"))
                if m:
                    gname = f"{m.group(1)} ({app.name})"
            except OSError:
                pass
            try:
                entries = sorted(app.iterdir())
            except OSError:
                continue
            for ent in entries:
                grp, drv = _classify_cache_entry(ent.name)
                add(grp, f"{gname} — {ent.name}", ent, drv)
    for p in extra:
        add("Custom folders", p, p)
    return nodes


def _safe_cache_path(p: str, extra: List[str]) -> bool:
    """Refuse to delete anything that is not clearly a cache location."""
    try:
        rp = Path(os.path.realpath(p))
        home = Path(os.path.realpath(Path.home()))
    except OSError:
        return False
    if rp == home or rp == Path("/") or len(rp.parts) < 4:
        return False
    if p in extra:
        return True
    return home in rp.parents or "shadercache" in rp.parts


# Names DXVK / vkd3d-proton / NVIDIA / Mesa / Fossilize give their cache files and folders.
_CACHE_NAME_RX = re.compile(r"(?i)(cache|\.dxvk|dxvk-|vkd3d|\.foz|fozdb|fossilize|GLCache|ComputeCache)")


class _CacheScanThread(QThread):
    done = Signal(object)

    def __init__(self, user_env: Dict[str, str], extra: List[str]):
        super().__init__()
        self._env, self._extra = user_env, extra

    def run(self):
        nodes = discover_cache_nodes(self._env, self._extra)
        for n in nodes:
            n["size"], n["files"], n["mtime"] = _path_stats(n["path"])
        self.done.emit(nodes)


class CacheManagerWidget(QWidget):
    driver_changed = Signal(str)      # human message, emitted when the driver differs from the last acknowledged one
    stale_changed = Signal(bool)

    CACHE_ENV = ("__GL_SHADER_DISK_CACHE", "__GL_SHADER_DISK_CACHE_SIZE", "__GL_SHADER_DISK_CACHE_PATH",
                 "__GL_SHADER_DISK_CACHE_SKIP_CLEANUP", "DXVK_STATE_CACHE_PATH", "DXVK_SHADER_CACHE_PATH",
                 "DXVK_SHADER_CACHE", "VKD3D_SHADER_CACHE_PATH", "MESA_SHADER_CACHE_DIR")

    def __init__(self):
        super().__init__()
        self._thread: Optional[_CacheScanThread] = None
        self._nodes: List[dict] = []
        self._scanned = False
        self._stale = False
        self._driver_msg_sent = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_pack2_title(
            "Shader / pipeline caches",
            "Sizes of the NVIDIA, Mesa, DXVK and vkd3d-proton caches DRSTool can find: the driver "
            "caches, Steam's shadercache/<appid> folders (every library), Lutris' per-game folder and "
            "the paths you set in the Env Vars tab. Use “Add folder…” for .dxvk-cache files next to "
            "game executables."))
        self._banner = QLabel("")
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(
            f"background:rgba(210,80,80,0.14);border:1px solid {T['danger']};border-radius:5px;"
            f"color:{T['text']};padding:6px 10px;font-size:12px;")
        self._banner.hide()
        lay.addWidget(self._banner)
        brow = QHBoxLayout()
        self._btn_clear_stale = _tool_btn("Clear driver-bound caches")
        self._btn_clear_stale.clicked.connect(self._clear_stale)
        self._btn_ack = _tool_btn("Keep & acknowledge")
        self._btn_ack.clicked.connect(self._acknowledge)
        brow.addWidget(self._btn_clear_stale)
        brow.addWidget(self._btn_ack)
        brow.addStretch(1)
        self._banner_btns = QWidget()
        self._banner_btns.setLayout(brow)
        self._banner_btns.hide()
        lay.addWidget(self._banner_btns)
        self._env_line = QLabel("")
        self._env_line.setWordWrap(True)
        self._env_line.setStyleSheet(f"color:{T['muted']};font-size:12px;")
        lay.addWidget(self._env_line)
        self._tree = QTreeWidget()
        self._tree.setColumnCount(5)
        self._tree.setHeaderLabels(["Cache", "Size", "Files", "Last written", "Path"])
        self._tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self._tree.setUniformRowHeights(True)
        self._tree.setStyleSheet(_pack2_table_ss())
        hdr = self._tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Interactive)
        self._tree.setColumnWidth(4, 260)
        lay.addWidget(self._tree, 1)
        row = QHBoxLayout()
        b_scan = _tool_btn("Rescan", bold=True)
        b_scan.clicked.connect(self.rescan)
        b_add = _tool_btn("Add folder…")
        b_add.clicked.connect(self._add_folder)
        b_open = _tool_btn("Open folder")
        b_open.clicked.connect(self._open_selected)
        b_clear = _tool_btn("Clear selected")
        b_clear.clicked.connect(self._clear_selected)
        for b in (b_scan, b_add, b_open, b_clear):
            row.addWidget(b)
        row.addStretch(1)
        self._total = QLabel("")
        self._total.setStyleSheet(f"color:{T['text2']};font-size:12px;")
        row.addWidget(self._total)
        lay.addLayout(row)
        SYSHUB.info_changed.connect(lambda _i: self._driver_check())

    def showEvent(self, ev):                  # noqa: N802
        super().showEvent(ev)
        if not self._scanned:                 # scan the cache trees on first view, not at startup
            self._scanned = True
            self.rescan()

    # ── data ──────────────────────────────────────────────────────────────
    def _user_env(self) -> Dict[str, str]:
        win = self.window()
        w = getattr(win, "_env_widget", None)
        try:
            return dict(w.get_env_dict()) if w is not None else {}
        except Exception:
            return {}

    @staticmethod
    def _extra_folders() -> List[str]:
        try:
            v = QSettings(APP_ID, APP_ID).value("cache/extra_folders", [])
            if isinstance(v, str):
                v = [v] if v else []
            return [str(x) for x in (v or [])]
        except Exception:
            return []

    def _save_extra(self, lst: List[str]):
        try:
            QSettings(APP_ID, APP_ID).setValue("cache/extra_folders", list(lst))
        except Exception:
            pass

    def rescan(self):
        if self._thread is not None and self._thread.isRunning():
            return
        env = self._user_env()
        shown = [f"{k}={env[k]}" for k in self.CACHE_ENV if env.get(k)]
        self._env_line.setText("Cache variables set in the Env Vars tab: " + "  ".join(shown)
                               if shown else "No cache-related variables are set in the Env Vars tab.")
        self._total.setText("Scanning…")
        self._thread = _CacheScanThread(env, self._extra_folders())
        TASKS.track(self._thread, "Cache: scan", _NAV("Cache"))
        self._thread.done.connect(self._on_scanned)
        self._thread.start()

    def _on_scanned(self, nodes):
        self._nodes = nodes
        self._rebuild()
        self._driver_check()

    def _rebuild(self):
        self._tree.clear()
        groups: Dict[str, QTreeWidgetItem] = {}
        order = {g: i for i, g in enumerate(CACHE_GROUP_ORDER)}
        by_group: Dict[str, List[dict]] = {}
        for n in self._nodes:
            by_group.setdefault(n["group"], []).append(n)
        total = 0
        for g in sorted(by_group, key=lambda x: order.get(x, 99)):
            items = sorted(by_group[g], key=lambda n: -n["size"])
            gsize = sum(n["size"] for n in items)
            gfiles = sum(n["files"] for n in items)
            total += gsize
            newest = max((n["mtime"] for n in items), default=0)
            top = QTreeWidgetItem([g, _fmt_bytes(gsize), str(gfiles), self._fmt_time(newest), ""])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            groups[g] = top
            self._tree.addTopLevelItem(top)
            for n in items:
                warn = self._stale and n["driver"]
                child = QTreeWidgetItem([("⚠ " if warn else "") + n["label"], _fmt_bytes(n["size"]),
                                         str(n["files"]), self._fmt_time(n["mtime"]), n["path"]])
                child.setData(0, Qt.UserRole, n)
                if warn:
                    for c in range(5):
                        child.setForeground(c, QColor(T["warn"]))
                    child.setToolTip(0, "Built for an older NVIDIA driver — stale, will be rebuilt.")
                top.addChild(child)
            top.setExpanded(len(items) <= 12)
        self._total.setText(f"{len(self._nodes)} location(s) · {_fmt_bytes(total)} total")

    @staticmethod
    def _fmt_time(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"

    # ── driver change detection ───────────────────────────────────────────
    def _driver_check(self):
        cur = SYSHUB.info.nv_driver
        if not cur:
            return
        qs = QSettings(APP_ID, APP_ID)
        seen = str(qs.value("cache/driver_seen", "") or "")
        if not seen:
            qs.setValue("cache/driver_seen", cur)
            seen = cur
        stale = seen != cur
        if stale != self._stale:
            self._stale = stale
            self._rebuild()
            self.stale_changed.emit(stale)
        if stale:
            self._banner.setText(
                f"⚠ NVIDIA driver changed: {seen} → {cur}. Driver-bound caches (NVIDIA GL/compute, "
                f"vkd3d-proton pipeline caches — marked ⚠) were built for the old driver and are "
                f"invalid: the first run of each game rebuilds them, so expect shader-compile stutter "
                f"once. DXVK state caches and Mesa/Fossilize data stay valid.")
            self._banner.show()
            self._banner_btns.show()
            if not self._driver_msg_sent:
                self._driver_msg_sent = True
                self.driver_changed.emit(
                    f"NVIDIA driver changed ({seen} → {cur}): shader caches are invalid — see Extra Tools → Cache")
        else:
            self._banner.hide()
            self._banner_btns.hide()

    def _acknowledge(self):
        cur = SYSHUB.info.nv_driver
        if cur:
            QSettings(APP_ID, APP_ID).setValue("cache/driver_seen", cur)
        self._driver_msg_sent = False
        self._driver_check()

    # ── actions ───────────────────────────────────────────────────────────
    def _selected_nodes(self) -> List[dict]:
        out = []
        for it in self._tree.selectedItems():
            n = it.data(0, Qt.UserRole)
            if n:
                out.append(n)
            else:                                   # group row → all its children
                for i in range(it.childCount()):
                    out.append(it.child(i).data(0, Qt.UserRole))
        seen, uniq = set(), []
        for n in out:
            if n["path"] not in seen:
                seen.add(n["path"])
                uniq.append(n)
        return uniq

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Cache folder to track", str(Path.home()))
        if d:
            lst = self._extra_folders()
            if d not in lst:
                lst.append(d)
                self._save_extra(lst)
            self.rescan()

    def _open_selected(self):
        nodes = self._selected_nodes()
        if not nodes:
            return
        p = Path(nodes[0]["path"])
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p if p.is_dir() else p.parent)))

    def _clear_selected(self):
        self._clear(self._selected_nodes())

    def _clear_stale(self):
        self._clear([n for n in self._nodes if n["driver"]])

    def _clear(self, nodes: List[dict]):
        if not nodes:
            return
        extra = self._extra_folders()
        ok = [n for n in nodes if _safe_cache_path(n["path"], extra)]
        refused = len(nodes) - len(ok)
        if not ok:
            QMessageBox.warning(self, "Clear caches", "Nothing selected is a recognised cache location.")
            return
        size = sum(n["size"] for n in ok)
        preview = "\n".join(f"  • {n['path']}  ({_fmt_bytes(n['size'])})" for n in ok[:12])
        if len(ok) > 12:
            preview += f"\n  … and {len(ok) - 12} more"
        if QMessageBox.question(
                self, "Clear caches",
                f"Delete the contents of {len(ok)} location(s), freeing about {_fmt_bytes(size)}?\n\n{preview}"
                + (f"\n\n{refused} location(s) skipped (not recognised as caches)." if refused else "")
                + "\n\nGames rebuild their caches on the next run (one-time stutter).") != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        errors = kept = 0
        try:
            for n in ok:
                p = Path(n["path"])
                try:
                    if p.is_dir() and not p.is_symlink():
                        for child in p.iterdir():
                            # A *_CACHE_PATH env var may point at a shared folder (the
                            # game dir, ~/Games …). Wiping its whole content deleted
                            # unrelated files — there, only cache-named entries go.
                            if n.get("user_path") and not _CACHE_NAME_RX.search(child.name):
                                kept += 1
                                continue
                            if child.is_dir() and not child.is_symlink():
                                shutil.rmtree(child, ignore_errors=True)
                            else:
                                child.unlink(missing_ok=True)
                    else:
                        p.unlink(missing_ok=True)
                except OSError:
                    errors += 1
        finally:
            QApplication.restoreOverrideCursor()
        if errors:
            QMessageBox.warning(self, "Clear caches", f"{errors} location(s) could not be fully cleared.")
        if kept:
            self._total.setText(f"{kept} non-cache item(s) inside your *_CACHE_PATH folder(s) were left alone.")
        self.rescan()
