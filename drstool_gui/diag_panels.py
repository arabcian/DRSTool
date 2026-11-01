"""Linter and System info pages."""

from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .lint import LintIssue
from .sysinfo import SYSHUB, SysInfo
from .theme import T
from .tool_ui import _tool_btn
from .widgets import _MONO_BOX_QSS


# ============================================================================
# Linter / System panels
# ============================================================================

_SEV_STYLE = {"error": ("●", "#d05050"), "warn": ("▲", "#d2a03c"), "info": ("ℹ", "#7d8597")}


class LinterWidget(QWidget):
    fix_requested = Signal(object)          # LintIssue

    def __init__(self, parent=None):
        super().__init__(parent)
        self._issues: List[LintIssue] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        head = QHBoxLayout()
        self._summary = QLabel("Consistency check")
        self._summary.setStyleSheet(f"color:{T['text']}; font-size:15px; font-weight:700;")
        head.addWidget(self._summary, 1)
        b = _tool_btn("Re-run")
        b.clicked.connect(lambda: hasattr(self.window(), "_run_lint") and self.window()._run_lint())
        head.addWidget(b)
        lay.addLayout(head)
        d = QLabel("Looks for combinations that contradict each other or can't work on this "
                   "system (PROTON_NVIDIA_LIBS + WoW64, FSR4 on NVIDIA, ENABLE_HDR_WSI on drivers ≥ 595, "
                   "double latency stacks, …). Runs automatically whenever the configuration changes.")
        d.setWordWrap(True)
        d.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(d)
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["", "Finding", ""])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        self._table.setWordWrap(True)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._table.setStyleSheet(
            f"QTableWidget{{background:{T['surface']};border:1px solid {T['border']};color:{T['text2']};"
            f"gridline-color:{T['border']};}}QHeaderView::section{{background:{T['raised']};"
            f"color:{T['muted']};border:none;padding:4px;}}")
        lay.addWidget(self._table, 1)

    def set_issues(self, issues: List[LintIssue]):
        self._issues = issues
        t = self._table
        t.setRowCount(0)
        n_e = sum(1 for i in issues if i.sev == "error")
        n_w = sum(1 for i in issues if i.sev == "warn")
        n_i = len(issues) - n_e - n_w
        self._summary.setText("Consistency check — " + (
            "all clear ✔" if not issues else f"{n_e} error(s), {n_w} warning(s), {n_i} note(s)"))
        for row, iss in enumerate(issues):
            t.insertRow(row)
            sym, col = _SEV_STYLE.get(iss.sev, ("•", T["muted"]))
            s = QTableWidgetItem(sym)
            s.setForeground(QColor(col))
            s.setTextAlignment(Qt.AlignCenter)
            t.setItem(row, 0, s)
            txt = iss.msg + (f"\n  → {', '.join(iss.involved)}" if iss.involved else "")
            m = QTableWidgetItem(txt)
            t.setItem(row, 1, m)
            if iss.fixes:
                b = _tool_btn(iss.fix_label or "Fix")
                b.clicked.connect(lambda _c=False, i=iss: self.fix_requested.emit(i))
                t.setCellWidget(row, 2, b)
        t.resizeRowsToContents()


class SystemInfoWidget(QWidget):
    WATCH = [("VK_NV_low_latency2", "Reflex / DXVK low-latency"),
             ("VK_EXT_descriptor_heap", "descriptor heap (DXVK / VKD3D flag)"),
             ("VK_EXT_descriptor_buffer", "descriptor buffer"),
             ("VK_EXT_hdr_metadata", "HDR metadata"),
             ("VK_EXT_swapchain_colorspace", "HDR colour spaces (instance)"),
             ("VK_NV_present_barrier", "present barrier"),
             ("VK_KHR_present_wait", "present wait"),
             ("VK_KHR_present_id", "present id"),
             ("VK_EXT_swapchain_maintenance1", "swapchain maintenance"),
             ("VK_NV_optical_flow", "optical flow (frame gen)"),
             ("VK_NV_cooperative_matrix2", "cooperative matrix 2"),
             ("VK_EXT_mesh_shader", "mesh shaders")]

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        t = QLabel("System")
        t.setStyleSheet(f"color:{T['text']}; font-size:15px; font-weight:700;")
        lay.addWidget(t)
        self._grey = QCheckBox("Grey out options this system can't run (you can still force them with “Edit anyway”)")
        self._grey.setChecked(SYSHUB.enabled)
        self._grey.setStyleSheet(f"color:{T['text2']};")
        self._grey.toggled.connect(SYSHUB.set_enabled)
        lay.addWidget(self._grey)
        row = QHBoxLayout()
        b = _tool_btn("Re-probe")
        b.clicked.connect(SYSHUB.refresh)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setStyleSheet(_MONO_BOX_QSS)
        lay.addWidget(self._text, 1)
        SYSHUB.info_changed.connect(self.update_info)
        self.update_info(SYSHUB.info)

    def update_info(self, si: SysInfo):
        L = []
        if not si.probed:
            L.append("Probing…")
        else:
            L.append(f"NVIDIA driver : {si.nv_driver or 'not detected'}"
                     + (f"   ({si.nv_kernel_flavor})" if si.nv_kernel_flavor else ""))
            if si.nv_gpus:
                L.append("nvidia-smi    : " + "; ".join(si.nv_gpus))
            L.append(f"Session       : {si.session or 'unknown'}")
            L.append(f"/dev/ntsync   : {'present' if si.ntsync_dev else 'missing'}")
            for tname in ("gamescope", "mangohud", "vulkaninfo", "nvidia-smi", "wine", "zstd"):
                L.append(f"{tname:<13} : {si.tools.get(tname, '—')}")
            L.append("")
            if si.vk_ok:
                for i, g in enumerate(si.vk_gpus):
                    L.append(f"Vulkan GPU{i}   : {g.name}  [{g.driver or 'driver ?'}]  {len(g.exts)} device exts")
                L.append("")
                L.append("Watched extensions (NVIDIA device if present, else all):")
                for ext, what in self.WATCH:
                    v = si.has_ext(ext)
                    L.append(f"  {'✔' if v else '✘'}  {ext:<34} {what}")
            else:
                L.append(f"Vulkan        : {si.vulkaninfo_error or 'unknown'}")
                L.append("  (nothing is greyed out while Vulkan data is unavailable)")
        self._text.setPlainText("\n".join(L))
