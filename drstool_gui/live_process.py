"""Live process page: compare configured and actual environment of a running game."""

import os
import re
from pathlib import Path
from typing import Dict, List

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .catalog_data import ALL_ENV_VARS
from .lint import _on
from .theme import T
from .tool_ui import _tool_btn
from .widgets import _desc_label, _LAUNCHER_QSS, _table_qss, _title_label


# ----------------------------------------------------------------------------
# Live process check: did the configured environment / DLLs reach the game?
# ----------------------------------------------------------------------------

_WINE_HINTS = (".exe", "wine", "proton", "gamescope", "umu-run", "reaper")
_WINE_NOISE = ("services.exe", "winedevice.exe", "plugplay.exe", "explorer.exe", "rpcss.exe",
               "svchost.exe", "conhost.exe", "start.exe", "tabtip.exe", "wineserver", "crashhandler",
               "steamwebhelper", "steam.exe", "pressure-vessel", "reaper", "lsteamclient", "winedbg")

_MOD_RULES = [
    ("DXVK (d3d8/9/10/11/dxgi)", r"/(d3d8|d3d9|d3d10core|d3d11|dxgi)\.dll$"),
    ("D3D12 (vkd3d-proton)", r"/(d3d12|d3d12core)\.dll$"),
    ("dxvk-nvapi", r"/nvapi(64)?\.dll$"),
    ("NGX / DLSS", r"/_?nvngx[^/]*\.dll$"),
    ("Streamline", r"/sl\.[a-z_.]+\.dll$"),
    ("nvcuda / PhysX", r"/(nvcuda|physxcore|physx3)[^/]*\.dll$"),
    ("winevulkan", r"/winevulkan\.(dll|so)$"),
    ("MangoHud", r"libMangoHud"),
    ("vk_flip_meter", r"cpu_flip_meter"),
    ("Vulkan layer / overlay", r"VkLayer|vkbasalt|libgamescope|gamescope_layer"),
    ("OptiScaler / ReShade", r"(?i)optiscaler|reshade"),
    ("NVIDIA driver", r"libGLX_nvidia|libnvidia-glcore|libnvidia-vulkan|libnvidia-rtcore|libnvidia-eglcore"),
    ("Mesa Vulkan", r"libvulkan_(radeon|intel|lvp)"),
]


def _origin(path: str) -> str:
    lp = path.lower()
    for key, name in (("dxvk-nvapi", "dxvk-nvapi"), ("vkd3d", "vkd3d-proton"), ("dxvk", "DXVK")):
        if key in lp:
            return name
    if "/lib/wine/" in lp or "/lib64/wine/" in lp or "/files/lib" in lp:
        return "Wine builtin"
    if "/steamapps/common/" in lp:
        return "game / Proton folder"
    return ""


def list_game_procs(only_wine: bool = True) -> List[tuple]:
    """[(pid, rss_mb, name, cmdline)] of this user's processes, biggest first."""
    uid, me, page = os.getuid(), os.getpid(), os.sysconf("SC_PAGE_SIZE")
    out = []
    try:
        names = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return out
    for n in names:
        pid = int(n)
        if pid == me:
            continue
        try:
            if os.stat(f"/proc/{n}").st_uid != uid:
                continue
            with open(f"/proc/{n}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            if not cmd:
                continue
            low = cmd.lower()
            if only_wine and (not any(h in low for h in _WINE_HINTS)
                              or any(x in low for x in _WINE_NOISE)):
                continue
            with open(f"/proc/{n}/statm") as f:
                rss = int(f.read().split()[1]) * page / 1048576
            with open(f"/proc/{n}/comm") as f:
                comm = f.read().strip()
        except (OSError, ValueError, IndexError):
            continue
        out.append((pid, rss, comm, cmd))
    out.sort(key=lambda r: -r[1])
    return out


def read_proc_environ(pid: int) -> Dict[str, str]:
    env = {}
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    for item in raw.split(b"\0"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def read_proc_modules(pid: int) -> List[str]:
    paths, seen = [], set()
    with open(f"/proc/{pid}/maps", "rb") as f:
        for ln in f:
            parts = ln.decode("utf-8", "replace").rstrip("\n").split(None, 5)
            if len(parts) == 6 and parts[5].startswith("/"):
                p = parts[5].replace(" (deleted)", "")
                if p not in seen:
                    seen.add(p)
                    paths.append(p)
    return paths


class LiveProcessWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("Live process check"))
        lay.addWidget(_desc_label(
            "Select the running game and compare what DRSTool is configured to set with the environment "
            "the process really received, plus which DXVK / vkd3d-proton / dxvk-nvapi / NGX DLLs and Vulkan "
            "layers are actually mapped into it. Answers “did the launcher swallow my variables?”."))
        row = QHBoxLayout()
        self._only = QCheckBox("only Wine / Proton processes")
        self._only.setChecked(True)
        self._only.setStyleSheet(f"color:{T['text2']};")
        self._only.toggled.connect(lambda _c: self.rescan())
        row.addWidget(self._only)
        row.addStretch(1)
        rb = _tool_btn("Refresh")
        rb.clicked.connect(self.rescan)
        row.addWidget(rb)
        lay.addLayout(row)
        self._plist = QListWidget()
        self._plist.setStyleSheet(_LAUNCHER_QSS)
        self._plist.setMaximumHeight(130)
        self._plist.currentItemChanged.connect(lambda *_: self.inspect())
        lay.addWidget(self._plist)
        self._sum = QLabel("")
        self._sum.setWordWrap(True)
        self._sum.setStyleSheet(f"color:{T['text2']}; font-size:12px;")
        lay.addWidget(self._sum)
        self._env = QTableWidget(0, 4)
        self._env.setHorizontalHeaderLabels(["Variable", "DRSTool", "Process", ""])
        self._mod = QTableWidget(0, 3)
        self._mod.setHorizontalHeaderLabels(["Component", "File", "Origin"])
        for t, stretch in ((self._env, 1), (self._mod, 1)):
            t.verticalHeader().setVisible(False)
            t.setEditTriggers(QTableWidget.NoEditTriggers)
            t.setSelectionMode(QTableWidget.NoSelection)
            t.setStyleSheet(_table_qss())
            t.horizontalHeader().setStretchLastSection(True)
            lay.addWidget(t, stretch)
        self._first = True

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        if self._first:
            self._first = False
            self.rescan()

    def rescan(self):
        self._plist.blockSignals(True)
        self._plist.clear()
        for pid, rss, comm, cmd in list_game_procs(self._only.isChecked()):
            it = QListWidgetItem(f"{pid:>7}  {rss:7.0f} MB  {comm[:18]:<18}  {cmd[:110]}")
            it.setData(Qt.UserRole, pid)
            self._plist.addItem(it)
        self._plist.blockSignals(False)
        if self._plist.count():
            self._plist.setCurrentRow(0)
        else:
            self._env.setRowCount(0)
            self._mod.setRowCount(0)
            self._sum.setText("No matching process running.")

    def inspect(self):
        it = self._plist.currentItem()
        if it is None:
            return
        pid = it.data(Qt.UserRole)
        w = self.window()
        exp = w.collect_launch_payload().get("env", {}) if hasattr(w, "collect_launch_payload") else {}
        try:
            env = read_proc_environ(pid)
            mods = read_proc_modules(pid)
        except OSError as ex:
            self._sum.setText(f"⚠ cannot read process {pid}: {ex}")
            return
        t = self._env
        t.setRowCount(0)
        ok = bad = 0
        names = set(exp)
        known = {e.name for e in ALL_ENV_VARS}
        extra = sorted(k for k in env if k in known and k not in names)
        for k in sorted(exp) + extra:
            r = t.rowCount()
            t.insertRow(r)
            want, got = exp.get(k), env.get(k)
            if want is None:
                st, col = "process only", T["muted"]
            elif got is None:
                st, col = "✗ missing", T["danger"]
                bad += 1
            elif str(got) != str(want):
                st, col = "≠ differs", T["warn"]
                bad += 1
            else:
                st, col = "✔", T["accent"]
                ok += 1
            for c, txt in enumerate((k, "" if want is None else str(want), "" if got is None else str(got), st)):
                cell = QTableWidgetItem(txt)
                if c == 3:
                    cell.setForeground(QColor(col))
                t.setItem(r, c, cell)
        m = self._mod
        m.setRowCount(0)
        found = {}
        for path in mods:
            for label, rx in _MOD_RULES:
                if re.search(rx, path):
                    r = m.rowCount()
                    m.insertRow(r)
                    for c, txt in enumerate((label, os.path.basename(path), _origin(path) or path)):
                        cell = QTableWidgetItem(txt)
                        cell.setToolTip(path)
                        m.setItem(r, c, cell)
                    found.setdefault(label, []).append(path)
                    break
        notes = []
        if "DXVK_NVAPI_DRS_SETTINGS" in exp and "dxvk-nvapi" not in found:
            notes.append("⚠ DRS settings are configured but no nvapi DLL is mapped — they cannot take effect "
                         "(PROTON_ENABLE_NVAPI / DXVK_ENABLE_NVAPI missing, or the game has not loaded NVAPI yet).")
        if _on(exp, "MANGOHUD") and "MangoHud" not in found:
            notes.append("⚠ MANGOHUD is set but libMangoHud is not loaded.")
        self._sum.setText(f"pid {pid}: {ok} variable(s) match, {bad} do not. "
                          f"{len(found)} component group(s) loaded. " + " ".join(notes))
