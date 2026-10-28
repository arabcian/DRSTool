"""System probe (driver, Vulkan devices, tools, session) shared by the Linter and by the 'not
available on this system' greying.
"""

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from .base import _run, APP_ID
from .theme import T

if TYPE_CHECKING:
    from drstool_core.models import EnvVarDef, Setting


# ----------------------------------------------------------------------------
# System probe: NVIDIA driver version + Vulkan device extensions
# ----------------------------------------------------------------------------

@dataclass
class VkGpu:
    name: str = ""
    vendor_id: int = 0
    driver: str = ""
    exts: set = field(default_factory=set)

    @property
    def is_nvidia(self) -> bool:
        return self.vendor_id == 0x10DE or "nvidia" in self.name.lower()


@dataclass
class SysInfo:
    probed: bool = False
    nv_driver: str = ""               # "595.58.03" or ""
    nv_kernel_flavor: str = ""        # "Open Kernel Module" / "proprietary" / ""
    nv_gpus: List[str] = field(default_factory=list)
    vk_ok: bool = False               # vulkaninfo ran and produced devices
    vk_gpus: List[VkGpu] = field(default_factory=list)
    vk_instance_exts: set = field(default_factory=set)
    vulkaninfo_error: str = ""
    session: str = ""
    ntsync_dev: bool = False
    tools: Dict[str, str] = field(default_factory=dict)
    raw_version: str = ""

    @property
    def nv_major(self) -> int:
        m = re.match(r"(\d+)", self.nv_driver or "")
        return int(m.group(1)) if m else 0

    @property
    def has_nvidia(self) -> bool:
        return bool(self.nv_driver) or any(g.is_nvidia for g in self.vk_gpus)

    @property
    def nvidia_known_absent(self) -> bool:
        """Probe finished and positively found no NVIDIA anything."""
        return self.probed and self.vk_ok and not self.has_nvidia

    def device_exts(self, nvidia_first: bool = True) -> set:
        """Extensions of the NVIDIA device if there is one, else the union."""
        if nvidia_first:
            nv = [g for g in self.vk_gpus if g.is_nvidia]
            if nv:
                out = set()
                for g in nv:
                    out |= g.exts
                return out
        out = set()
        for g in self.vk_gpus:
            out |= g.exts
        return out

    def has_ext(self, ext: str) -> Optional[bool]:
        """True/False if known, None if the probe could not tell."""
        if not self.vk_ok:
            return None
        return ext in self.device_exts() or ext in self.vk_instance_exts


def parse_vulkaninfo(text: str):
    """-> (instance_exts:set, [VkGpu]).  Tolerant line parser for `vulkaninfo`."""
    ext_re = re.compile(r"^\s*(VK_[A-Za-z0-9_]+)\s*:\s*extension revision", re.M)
    # split into the instance section and per-GPU sections
    parts = re.split(r"^GPU(\d+):\s*$", text, flags=re.M)
    head = parts[0]
    inst = set(ext_re.findall(head))
    gpus: List[VkGpu] = []
    for i in range(1, len(parts) - 1, 2):
        body = parts[i + 1]
        g = VkGpu()
        m = re.search(r"deviceName\s*=\s*(.+)", body)
        if m:
            g.name = m.group(1).strip()
        m = re.search(r"vendorID\s*=\s*(0x[0-9a-fA-F]+|\d+)", body)
        if m:
            try:
                g.vendor_id = int(m.group(1), 0)
            except ValueError:
                pass
        m = re.search(r"driverInfo\s*=\s*(.+)", body)
        if m:
            g.driver = m.group(1).strip()
        g.exts = set(ext_re.findall(body))
        gpus.append(g)
    return inst, gpus


def probe_system() -> SysInfo:
    si = SysInfo()
    # --- NVIDIA kernel/driver version -------------------------------------
    try:
        raw = Path("/proc/driver/nvidia/version").read_text(errors="replace")
        si.raw_version = raw.strip()
        m = re.search(r"NVRM version:\s*(.*?)\s+(\d+\.\d+(?:\.\d+)?)\s", raw)
        if m:
            si.nv_driver = m.group(2)
            si.nv_kernel_flavor = "Open Kernel Module" if "Open" in m.group(1) else "proprietary"
    except OSError:
        pass
    smi = shutil.which("nvidia-smi")
    if smi:
        out = _run([smi, "--query-gpu=driver_version,name", "--format=csv,noheader"], 8)
        for ln in out.splitlines():
            if "," in ln:
                ver, name = [x.strip() for x in ln.split(",", 1)]
                si.nv_gpus.append(name)
                if not si.nv_driver and re.match(r"\d+\.\d+", ver):
                    si.nv_driver = ver
    # --- Vulkan -------------------------------------------------------------
    vi = shutil.which("vulkaninfo")
    if vi:
        out = _run([vi], 25)
        inst, gpus = parse_vulkaninfo(out)
        if gpus:
            si.vk_gpus, si.vk_instance_exts, si.vk_ok = gpus, inst, True
        else:
            si.vulkaninfo_error = "vulkaninfo produced no device list"
    else:
        si.vulkaninfo_error = "vulkaninfo not installed (dev-util/vulkan-tools)"
    # --- misc ---------------------------------------------------------------
    si.session = os.environ.get("XDG_SESSION_TYPE", "") or (
        "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11" if os.environ.get("DISPLAY") else "")
    si.ntsync_dev = Path("/dev/ntsync").exists()
    for t in ("gamescope", "mangohud", "vulkaninfo", "nvidia-smi", "wine", "zstd"):
        p = shutil.which(t)
        if p:
            si.tools[t] = p
    si.probed = True
    return si


class _ProbeThread(QThread):
    done = Signal(object)

    def run(self):
        try:
            self.done.emit(probe_system())
        except Exception as e:                       # never kill the GUI for a probe
            si = SysInfo(probed=True)
            si.vulkaninfo_error = f"probe failed: {e}"
            self.done.emit(si)


class SysHub(QObject):
    """Holds the latest SysInfo and re-probes in the background."""
    info_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.info = SysInfo()
        self.enabled = True       # "grey out unsupported options"
        self._thread: Optional[_ProbeThread] = None
        try:
            self.enabled = QSettings(APP_ID, APP_ID).value("sys/grey_unsupported", True, type=bool)
        except Exception:
            pass

    def set_enabled(self, on: bool):
        self.enabled = on
        try:
            QSettings(APP_ID, APP_ID).setValue("sys/grey_unsupported", on)
        except Exception:
            pass
        self.info_changed.emit(self.info)

    def refresh(self):
        if self._thread is not None and self._thread.isRunning():
            return
        self._thread = _ProbeThread()
        self._thread.done.connect(self._on_done)
        self._thread.start()

    def _on_done(self, info):
        self.info = info
        self.info_changed.emit(info)

    def shutdown(self):
        if self._thread is not None and self._thread.isRunning():
            self._thread.wait(30000)


SYSHUB = SysHub()


# ----------------------------------------------------------------------------
# Availability rules ("why is this greyed out?").  Deliberately conservative:
# an option is only flagged when the probe POSITIVELY says it cannot work.
# Unknown probe data never greys anything out.
# ----------------------------------------------------------------------------

_NV_ONLY_PREFIXES = ("__GL_", "__NV_", "DXVK_NVAPI_", "NVPRESENT_", "PROTON_NVIDIA_",
                     "DISABLE_DXVK_NVAPI_", "__GLX_VENDOR")
_NV_ONLY_NAMES = {"PROTON_ENABLE_NVAPI", "PROTON_DISABLE_NVAPI", "PROTON_FORCE_NVAPI",
                  "PROTON_HIDE_NVIDIA_GPU", "PROTON_NVAPI_BYPASS", "PROTON_ENABLE_NGX_UPDATER",
                  "PROTON_DLSS_UPGRADE", "PROTON_DLSS_INDICATOR", "DXVK_ENABLE_NVAPI"}

# env var / dxvk.conf key / vkd3d flag  ->  required Vulkan extension
_EXT_REQ_ENV = {"DXVK_NVAPI_VKREFLEX": "VK_NV_low_latency2"}
_EXT_REQ_DXVK_CFG = {
    "dxvk.enableDescriptorHeap": "VK_EXT_descriptor_heap",
    "dxvk.enableDescriptorBuffer": "VK_EXT_descriptor_buffer",
}
_EXT_REQ_VKD3D_FLAG = {"descriptor_heap": "VK_EXT_descriptor_heap"}


def _sys_active() -> Optional[SysInfo]:
    return SYSHUB.info if (SYSHUB.enabled and SYSHUB.info.probed) else None


def _need_ext(si: SysInfo, ext: str) -> str:
    if si.has_ext(ext) is False:
        return f"{ext} is not exposed by this system's Vulkan driver"
    return ""


def env_unavailable(ev: "EnvVarDef") -> str:
    si = _sys_active()
    if si is None:
        return ""
    n = ev.name
    if (n in _NV_ONLY_NAMES or n.startswith(_NV_ONLY_PREFIXES)) and si.nvidia_known_absent:
        return "No NVIDIA driver / GPU detected on this system"
    ext = _EXT_REQ_ENV.get(n)
    return _need_ext(si, ext) if ext else ""


def drs_unavailable(s: "Setting") -> str:
    si = _sys_active()
    if si is None:
        return ""
    if si.nvidia_known_absent:
        return "DRS settings are read by dxvk-nvapi from the NVIDIA driver — none detected here"
    return ""


def vkd3d_flag_unavailable(flag: str) -> str:
    si = _sys_active()
    ext = _EXT_REQ_VKD3D_FLAG.get(flag)
    return _need_ext(si, ext) if (si and ext) else ""


def dxvk_cfg_unavailable(key: str) -> str:
    si = _sys_active()
    ext = _EXT_REQ_DXVK_CFG.get(key)
    return _need_ext(si, ext) if (si and ext) else ""


def unavail_banner(why: str, on_override=None) -> QWidget:
    """Amber note shown on top of an editor whose option the system can't run."""
    box = QFrame()
    box.setStyleSheet("QFrame{background:rgba(210,160,60,0.10);border:1px solid %s;"
                      "border-radius:5px;}" % T['warn'])
    lay = QHBoxLayout(box)
    lay.setContentsMargins(10, 6, 10, 6)
    lbl = QLabel(f"⚠  Not supported here: {why}")
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color:{T['warn']}; font-size:12px; border:none; background:transparent;")
    lay.addWidget(lbl, 1)
    if on_override is not None:
        b = QPushButton("Edit anyway")
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(f"QPushButton{{color:{T['text']};background:{T['raised']};"
                        f"border:1px solid {T['border_hi']};border-radius:4px;padding:2px 10px;}}"
                        f"QPushButton:hover{{border-color:{T['warn']};}}")
        b.clicked.connect(lambda: (on_override(), box.hide()))
        lay.addWidget(b)
    return box
