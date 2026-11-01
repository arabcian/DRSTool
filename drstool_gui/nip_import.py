"""NVIDIA Profile Inspector (.nip) import."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .lookup import canonical_drs_key, canonical_drs_value, drs_lookup
from .theme import T
from .tool_ui import _tool_btn
from .widgets import _fmt_drs_value, _pack2_table_ss, _pack2_title

if TYPE_CHECKING:
    from .settings_manager import SettingsManager


# ----------------------------------------------------------------------------
# NVIDIA Profile Inspector (.nip) import
# ----------------------------------------------------------------------------

def parse_nip(text: str) -> List[dict]:
    """Parse a Profile Inspector export. Returns [{name, exes, settings:[{id,value,vtype,label}]}]."""
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
        raise ValueError("DTD / entity declarations are not allowed in a .nip file")
    root = ET.fromstring(text)
    out = []
    for prof in root.iter("Profile"):
        settings = []
        for ps in prof.findall("./Settings/ProfileSetting"):
            try:
                sid = int((ps.findtext("SettingID") or "").strip(), 0)
            except ValueError:
                continue
            raw = (ps.findtext("SettingValue") or "").strip()
            vtype = (ps.findtext("ValueType") or "Dword").strip()
            val: Optional[int] = None
            if vtype.lower() == "dword":
                try:
                    val = int(raw, 0) & 0xFFFFFFFF
                except ValueError:
                    val = None
            settings.append({"id": sid, "value": val, "raw": raw, "vtype": vtype,
                             "label": (ps.findtext("SettingNameInfo") or "").strip()})
        out.append({"name": (prof.findtext("ProfileName") or "").strip(),
                    "exes": [(e.text or "").strip() for e in prof.findall("./Executeables/string")],
                    "settings": settings})
    return out


def decode_nip_bytes(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig", errors="replace")


class NipImportWidget(QWidget):
    COL_USE, COL_NAME, COL_ID, COL_VAL, COL_STATUS = range(5)

    def __init__(self, settings_manager: "SettingsManager"):
        super().__init__()
        self.sm = settings_manager
        self._profiles: List[dict] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.addWidget(_pack2_title(
            "Import NVIDIA Profile Inspector (.nip)",
            "Reads a .nip export and converts its DWORD settings into DXVK_NVAPI_DRS_SETTINGS pairs "
            "(0xID=0xVALUE). Settings are applied to the editor — nothing is written to the driver. "
            "String / binary values cannot be expressed in DXVK_NVAPI_DRS_SETTINGS and are skipped."))
        row = QHBoxLayout()
        b = _tool_btn("Open .nip…", bold=True)
        b.clicked.connect(self._open)
        row.addWidget(b)
        self._prof_combo = QComboBox()
        self._prof_combo.currentIndexChanged.connect(self._fill)
        row.addWidget(self._prof_combo, 1)
        lay.addLayout(row)
        self._info = QLabel("")
        self._info.setWordWrap(True)
        self._info.setStyleSheet(f"color:{T['muted']};font-size:12px;")
        lay.addWidget(self._info)
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["Use", "Setting", "ID", "Value", "Status"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(self.COL_NAME, QHeaderView.Stretch)
        for c in (self.COL_USE, self.COL_ID, self.COL_VAL, self.COL_STATUS):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self._table.setStyleSheet(_pack2_table_ss())
        self._table.itemChanged.connect(lambda _i: self._update_preview())
        lay.addWidget(self._table, 1)
        self._preview = QLineEdit()
        self._preview.setReadOnly(True)
        self._preview.setStyleSheet(
            f"QLineEdit{{background:{T['bg']};border:1px solid {T['border_hi']};color:{T['text']};"
            f"font-family:{T['mono']};padding:4px 6px;}}")
        lay.addWidget(self._preview)
        r2 = QHBoxLayout()
        self._mode = QComboBox()
        self._mode.addItems(["Merge into current settings", "Replace current DRS settings"])
        r2.addWidget(self._mode)
        self._btn_apply = _tool_btn("Apply to editor", bold=True)
        self._btn_apply.clicked.connect(self._apply)
        self._btn_apply.setEnabled(False)
        b_copy = _tool_btn("Copy DXVK_NVAPI_DRS_SETTINGS=…")
        b_copy.clicked.connect(lambda: QApplication.clipboard().setText(self._preview.text()))
        r2.addWidget(self._btn_apply)
        r2.addWidget(b_copy)
        r2.addStretch(1)
        lay.addLayout(r2)

    def _open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "NVIDIA Profile Inspector export", str(Path.home()), "Profile Inspector (*.nip);;All files (*)")
        if path:
            self.load_file(Path(path))

    def load_file(self, path: Path):
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("file is larger than 8 MiB")
            self._profiles = parse_nip(decode_nip_bytes(path.read_bytes()))
        except Exception as e:
            self._profiles = []
            self._prof_combo.clear()
            self._table.setRowCount(0)
            self._info.setText(f"Could not read {path.name}: {e}")
            self._update_preview()
            return
        self._prof_combo.blockSignals(True)
        self._prof_combo.clear()
        for p in self._profiles:
            self._prof_combo.addItem(f'{p["name"] or "(unnamed)"}  ·  {len(p["settings"])} setting(s)')
        self._prof_combo.blockSignals(False)
        self._info.setText(f"{path.name}: {len(self._profiles)} profile(s)")
        self._fill()

    @staticmethod
    def _fmt_value(v: int) -> str:
        return f"0x{v:X}"

    def _fill(self, *_):
        t = self._table
        t.blockSignals(True)
        t.setRowCount(0)
        i = self._prof_combo.currentIndex()
        if not (0 <= i < len(self._profiles)):
            t.blockSignals(False)
            self._update_preview()
            return
        prof = self._profiles[i]
        exes = ", ".join(e for e in prof["exes"] if e)
        self._info.setText((f"Profile applies to: {exes}" if exes else "Profile has no executables listed")
                           + "  ·  ✓ rows are converted")
        for r, s in enumerate(prof["settings"]):
            t.insertRow(r)
            key = f'0x{s["id"]:08X}'
            known = drs_lookup(key)
            if known is not None:
                key = known.id
            ok = s["value"] is not None
            if not ok:
                status = f'skipped: {s["vtype"]} value'
            elif known is None:
                status = "not in DRSTool catalog (kept as raw pair)"
            else:
                status = "known"
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled if ok else Qt.NoItemFlags)
            chk.setCheckState(Qt.Checked if ok else Qt.Unchecked)
            chk.setData(Qt.UserRole, (key, self._fmt_value(s["value"]) if ok else None))
            t.setItem(r, self.COL_USE, chk)
            name = known.name if known else (s["label"] or "(unknown)")
            val_txt = (f"{_fmt_drs_value(known, self._fmt_value(s['value']))}  [{self._fmt_value(s['value'])}]"
                       if ok and known and _fmt_drs_value(known, self._fmt_value(s['value'])) != self._fmt_value(s['value'])
                       else (self._fmt_value(s["value"]) if ok else s["raw"][:40]))
            for c, txt in ((self.COL_NAME, name), (self.COL_ID, key), (self.COL_VAL, val_txt),
                           (self.COL_STATUS, status)):
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemIsEnabled)
                if not ok:
                    it.setForeground(QColor(T["muted"]))
                elif known is None and c == self.COL_STATUS:
                    it.setForeground(QColor(T["warn"]))
                t.setItem(r, c, it)
        t.blockSignals(False)
        self._update_preview()

    def _checked_pairs(self) -> List[tuple]:
        pairs = []
        for r in range(self._table.rowCount()):
            it = self._table.item(r, self.COL_USE)
            if it is not None and it.checkState() == Qt.Checked:
                k, v = it.data(Qt.UserRole)
                if v is not None:
                    pairs.append((k, v))
        return pairs

    def _update_preview(self):
        pairs = self._checked_pairs()
        self._preview.setText("DXVK_NVAPI_DRS_SETTINGS=" + ",".join(f"{k}={v}" for k, v in pairs)
                              if pairs else "")
        self._btn_apply.setEnabled(bool(pairs))

    def _apply(self):
        pairs = self._checked_pairs()
        if not pairs:
            return
        replace = self._mode.currentIndex() == 1
        if replace and QMessageBox.question(
                self, "Replace settings",
                "Clear all current DRS settings in the editor before applying?") != QMessageBox.Yes:
            return
        new = {} if replace else self.sm.get_settings_list()
        for k, v in pairs:
            new[canonical_drs_key(k)] = canonical_drs_value(drs_lookup(k), v)
        win = self.window()
        if hasattr(win, "_pending_label"):
            win._pending_label = f"Import .nip ({len(pairs)} setting(s))"
        arch = self.sm.get_arch()
        self.sm.replace_state(new, arch.code if arch else None)
        self._info.setText(f"Applied {len(pairs)} setting(s) to the editor.")
