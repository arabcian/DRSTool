"""Upstream drift check (new / removed env vars, dxvk.conf keys, DRS ids upstream)."""

import re
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Optional

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .base import APP_ID
from .catalog_data import (
    ALL_ENV_VARS, DXVK_CONFIG_KEY_NAMES, GAMESCOPE_FLAGS, VKD3D_CONFIG_DESCS, VKD3D_ENV_VARS,
)
from .lookup import _SETTING_BY_ID
from .tasks import _NAV, TASKS
from .theme import T
from .tool_ui import _tool_btn
from .widgets import _pack2_table_ss, _pack2_title


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
        "conf_keys": set(DXVK_CONFIG_KEY_NAMES),
        "vkd3d_flags": vk_flags,
        "gs_flags": {g.flag.lstrip("-") for g in GAMESCOPE_FLAGS},
        "drs_ids": set(_SETTING_BY_ID),
    }


def _drift_fetch(url: str, timeout: float = 25.0) -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": _DRIFT_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(_DRIFT_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if len(data) > _DRIFT_MAX_BYTES:
        data = data[:_DRIFT_MAX_BYTES]
    return data.decode("utf-8", errors="replace")


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
        opt_rx = re.compile(r'"((?:dxvk|dxgi|d3d8|d3d9|d3d10|d3d11)\.[A-Za-z0-9_.]+)"')
        for rel, txt in files.items():
            rx = conf_rx if rel.endswith(".conf") else opt_rx
            for m in rx.finditer(txt):
                keys.setdefault(m.group(1), f"{rel}:{_line_of(txt, m.start())}")
        # config.cpp holds per-game blocks → ignore it for 'gone' detection, only options files count
        for k, where in sorted(keys.items()):
            if k not in cat["conf_keys"] and not k.endswith(".conf") \
                    and k.startswith(("dxvk.", "dxgi.", "d3d9.", "d3d11.", "d3d8.")):
                row("dxvk.conf key", k, "new", where)
        for k in sorted(cat["conf_keys"] - set(keys)):
            row("dxvk.conf key", k, "gone", note=gone_note)
    rel = src.get("vkd3d_flags")
    if rel and rel in files:
        txt = files[rel]
        up: Dict[str, str] = {}
        for m in re.finditer(r'VKD3D_DECL_CONFIG\(\s*"([a-z0-9_]+)"', txt):
            up.setdefault(m.group(1), f"{rel}:{_line_of(txt, m.start())}")
        for m in re.finditer(r'VKD3D_DECL_CONFIG_PLAIN\(\s*([A-Z0-9_]+)\s*\)', txt):
            up.setdefault(m.group(1).lower(),
                          f"{rel}:{_line_of(txt, m.start())}  (declared without a config string — may be internal)")
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


class _DriftWorker(QThread):
    progress = Signal(str)
    done = Signal(list, list)

    def __init__(self, sources: List[dict], cat: dict):
        super().__init__()
        self._sources, self._cat = sources, cat

    def run(self):
        rows: List[dict] = []
        notes: List[str] = []
        for src in self._sources:
            if self.isInterruptionRequested():
                break
            files: Dict[str, str] = {}
            net_err = ""
            total = len(src["files"])
            # fetched in parallel (each file was a separate sequential TLS round trip),
            # consumed in the original order so results and the first-error rule stay the same
            with ThreadPoolExecutor(max_workers=6) as pool:
                futs = [(rel, pool.submit(_drift_fetch, f'{_DRIFT_RAW}/{src["repo"]}/{src["ref"]}/{rel}'))
                        for rel in src["files"]]
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
                        files[rel] = txt
                for _rel, fut in futs:
                    fut.cancel()                               # drop what has not started yet
            if net_err:
                notes.append(f'{src["title"]}: network error — {net_err}')
                continue
            if not files:
                notes.append(f'{src["title"]}: none of the {total} source files exist at {src["repo"]}@{src["ref"]}')
                continue
            rows += _drift_analyse(src, files, self._cat)
            notes.append(f'{src["title"]}: scanned {len(files)}/{total} files')
        self.done.emit(rows, notes)


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
            "catalog does not. “Gone” = the catalog has it, upstream no longer mentions it. Mark rows "
            "as reviewed to hide them next time."))
        srow = QHBoxLayout()
        self._src_checks: Dict[str, QCheckBox] = {}
        for s in DRIFT_SOURCES:
            c = QCheckBox(s["title"])
            c.setChecked(True)
            c.setStyleSheet(f"color:{T['text2']};")
            self._src_checks[s["key"]] = c
            srow.addWidget(c)
        srow.addStretch(1)
        self._btn_check = _tool_btn("Check now", bold=True)
        self._btn_check.clicked.connect(self._start)
        srow.addWidget(self._btn_check)
        lay.addLayout(srow)
        frow = QHBoxLayout()
        self._filter = QComboBox()
        self._filter.addItems(["New upstream", "Gone upstream", "All"])
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
        for b in (b1, b2, b3, b4):
            brow.addWidget(b)
        brow.addStretch(1)
        lay.addLayout(brow)

    # reviewed set (QSettings) ------------------------------------------------
    @staticmethod
    def _acks() -> set:
        try:
            v = QSettings(APP_ID, APP_ID).value("drift/reviewed", [])
            if isinstance(v, str):
                v = [v] if v else []
            return set(str(x) for x in (v or []))
        except Exception:
            return set()

    @staticmethod
    def _row_key(r: dict) -> str:
        return f'{r["src"]}|{r["kind"]}|{r["status"]}|{r["name"]}'

    def _save_acks(self, s: set):
        try:
            QSettings(APP_ID, APP_ID).setValue("drift/reviewed", sorted(s))
        except Exception:
            pass

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
        self._status.setText(f"{n_new} new · {n_gone} gone")
        self._status.setToolTip("\n".join(notes))
        if any("error" in n or "none of" in n for n in notes):
            QMessageBox.warning(self, "Upstream drift", "\n".join(notes))
        self._refill()

    def _visible_rows(self) -> List[dict]:
        mode = self._filter.currentIndex()
        acks = self._acks() if self._hide_ack.isChecked() else set()
        out = []
        for r in self._rows:
            if mode == 0 and r["status"] != "new":
                continue
            if mode == 1 and r["status"] != "gone":
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
            col = T["accent"] if r["status"] == "new" else T["warn"]
            vals = [r["src"], r["kind"], r["name"], "NEW" if r["status"] == "new" else "gone",
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
            lines.append(f'- {"NEW" if r["status"] == "new" else "gone"}: `{r["name"]}`'
                         + (f' ({r["where"]})' if r["where"] else ""))
        QApplication.clipboard().setText("\n".join(lines))
        self._status.setText(f"Copied report ({len(rows)} rows)")
