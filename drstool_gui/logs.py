"""Proton / DXVK / vkd3d-proton log viewer."""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPlainTextEdit, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .catalog_data import LOG_ENV_ON
from .theme import T
from .tool_ui import _tool_btn
from .widgets import (
    _COMBO_QSS, _desc_label, _LINEEDIT_QSS, _MONO_BOX_QSS, _table_qss, _title_label, _xdg,
)


# ----------------------------------------------------------------------------
# Log viewer (Proton / DXVK / vkd3d-proton) with known-problem detection
# ----------------------------------------------------------------------------

_LOG_ERR = re.compile(r"\b(?:err|error|fatal)\b\s*:|VK_ERROR_|\bERR\b:", re.I)
_LOG_WARN = re.compile(r"\bwarn(?:ing)?\b\s*:", re.I)
_LOG_FIXME = re.compile(r"\bfixme\b\s*:", re.I)

# (code, regex, title, hint, fixes[(var, value)])
_LOG_RULES = [
    ("nvapi", r"(?i)nvapi\w*\.dll.*(not found|failed)|nvapi.*(initiali[sz]e|init).*(fail|error)",
     "NVAPI unavailable",
     "dxvk-nvapi did not initialise. Needs PROTON_ENABLE_NVAPI=1 (Proton) / DXVK_ENABLE_NVAPI=1 and an NVIDIA GPU "
     "that is not hidden (Linter: nvapi-hidden).", [("PROTON_ENABLE_NVAPI", "1")]),
    ("nvcuda", r"(?i)library nvcuda\.dll.*not found|nvcuda\.dll.*not found",
     "nvcuda.dll missing",
     "CUDA/PhysX path: enable NVAPI/NVIDIA libs in Proton (PROTON_ENABLE_NVAPI, PROTON_NVIDIA_LIBS).", []),
    ("device-lost", r"VK_ERROR_DEVICE_LOST|\bdevice lost\b",
     "GPU device lost (hang/reset)",
     "Often an unstable GPU overclock/undervolt or driver fault: test at stock clocks and a default V/F curve.", []),
    ("oom", r"VK_ERROR_OUT_OF_(DEVICE|HOST)_MEMORY|out of (video|device|gpu) memory",
     "Out of memory",
     "VRAM or RAM exhausted: lower textures/resolution; DXVK_CONFIG dxgi.maxDeviceMemory caps reported VRAM.", []),
    ("ntsync", r"(?i)ntsync.*(fail|cannot|unavailable|not (found|supported))|/dev/ntsync",
     "ntsync not usable",
     "Kernel module not loaded (modprobe ntsync / CONFIG_NTSYNC) — or unset PROTON_USE_NTSYNC (Linter: ntsync-missing).", []),
    ("esync", r"(?i)too many open files|esync:.*(limit|fail)",
     "esync/fsync file-descriptor limit",
     "Raise the nofile hard limit (≥ 524288) in limits.conf / the service, or set PROTON_NO_ESYNC=1.", []),
    ("vk-device", r"(?i)vkCreate(Device|Instance).*fail|VK_ERROR_INCOMPATIBLE_DRIVER|VK_ERROR_INITIALIZATION_FAILED|"
                  r"no suitable vulkan|failed to create (a )?vulkan (instance|device)",
     "Vulkan device/instance creation failed",
     "Driver/ICD problem: check the System tab (vulkaninfo), DXVK_FILTER_DEVICE_NAME and the PRIME offload variables.", []),
    ("vk-ext", r"VK_ERROR_(EXTENSION|FEATURE)_NOT_PRESENT|required vulkan (extension|feature)|(unsupported|not supported) vulkan",
     "Missing Vulkan extension/feature",
     "Driver too old for this DXVK/vkd3d-proton build: update the driver or use an older build (DLL Manager).", []),
    ("ngx", r"(?i)(ngx|nvngx).*(fail|error|not found)",
     "NGX / DLSS failed to load",
     "Check the DLL Manager tab and PROTON_ENABLE_NVAPI=1; a mismatching nvngx_dlss.dll in the game folder also causes this.",
     [("PROTON_ENABLE_NVAPI", "1")]),
    ("import-dll", r"err:module:import_dll",
     "Missing DLL import",
     "A required runtime DLL is absent in the prefix (VC++ redistributables, d3dcompiler_47, xinput …).", []),
]


# Lower-case substrings that every match of the rule must contain (a superset of its pattern's
# literals). The regex only runs on lines that hold one of them: 111k lines took 1.2 s without.
_LOG_GATES = {
    "nvapi": ("nvapi",),
    "nvcuda": ("nvcuda",),
    "device-lost": ("vk_error_device_lost", "device lost"),
    "oom": ("memory",),
    "ntsync": ("ntsync",),
    "esync": ("too many open files", "esync"),
    "vk-device": ("vkcreate", "vk_error_", "no suitable vulkan", "failed to create"),
    "vk-ext": ("vk_error_", "vulkan"),
    "ngx": ("ngx",),
    "import-dll": ("import_dll",),
}


def _log_level(ln: str) -> str:
    low = ln.lower()
    if ("err" in low or "fatal" in low) and _LOG_ERR.search(ln):
        return "err"
    if "warn" in low and _LOG_WARN.search(ln):
        return "warn"
    if "fixme" in low and _LOG_FIXME.search(ln):
        return "fixme"
    return "info"


def _log_analyse(lines: List[str]):
    """-> (severity per line, {rule code: match count}); same result as running every
    regex over every line, only cheaper."""
    levels = [_log_level(ln) for ln in lines]
    text = "\n".join(lines)
    low = text.lower()
    hits: Dict[str, int] = {}
    for code, rx, *_ in _LOG_RULES:
        n = 0
        if len(low) != len(text):                 # lower() changed the length: offsets would drift
            n = len(re.findall(rx, text))
        else:
            crx, seen = re.compile(rx), set()
            for key in _LOG_GATES[code]:
                i = low.find(key)
                while i >= 0:
                    ls = text.rfind("\n", 0, i) + 1
                    le = text.find("\n", i)
                    le = len(text) if le < 0 else le
                    if ls not in seen:
                        seen.add(ls)
                        n += len(crx.findall(text, ls, le))
                    i = low.find(key, le)
        if n:
            hits[code] = n
    return levels, hits


class LogHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self._fmts = []
        for rx, col in ((_LOG_ERR, T["danger"]), (_LOG_WARN, T["warn"]), (_LOG_FIXME, T["muted"])):
            f = QTextCharFormat()
            f.setForeground(QColor(col))
            self._fmts.append((rx, f))

    def highlightBlock(self, text):        # noqa: N802
        for rx, f in self._fmts:
            if rx.search(text):
                self.setFormat(0, len(text), f)
                return


class _LogLoadThread(QThread):
    done = Signal(int, int, object, object, object)     # generation, file position, lines, levels, hits
    failed = Signal(int, str)

    def __init__(self, gen: int, path: Path, max_read: int):
        super().__init__()
        self._gen, self._path, self._max = gen, path, max_read

    def run(self):
        try:
            size = self._path.stat().st_size
            with open(self._path, "rb") as f:
                if size > self._max:
                    f.seek(size - self._max)
                    f.readline()
                start = f.tell()
                data = f.read()
                # where we actually stopped: the file may have grown since stat(),
                # and taking `size` made the next _tail() append those lines twice
                pos = start + len(data)
        except OSError as ex:
            self.failed.emit(self._gen, f"⚠ {ex}")
            return
        lines = data.decode("utf-8", "replace").splitlines()
        if len(lines) > 200000:
            lines = lines[-150000:]
        levels, hits = _log_analyse(lines)
        self.done.emit(self._gen, pos, lines, levels, hits)


class LogViewerWidget(QWidget):
    MAX_READ = 6 * 1024 * 1024
    MAX_SHOW = 4000

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path: Optional[Path] = None
        self._pos = 0
        self._lines: List[str] = []
        self._levels: List[str] = []
        self._hits: Dict[str, int] = {}
        self._gen = 0                         # id of the latest load request
        self._loading = False
        self._loaders: List[QThread] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("Log viewer"))
        lay.addWidget(_desc_label(
            "Opens a Proton log (PROTON_LOG=1 → ~/steam-<appid>.log), DXVK / vkd3d-proton logs, or any text log. "
            "Filters by severity, follows a running game, and recognises common failure signatures "
            "(NVAPI, ntsync, device lost, OOM, esync limits …)."))
        row = QHBoxLayout()
        self._combo = QComboBox()
        self._combo.setStyleSheet(_COMBO_QSS)
        self._combo.currentIndexChanged.connect(self._on_pick)
        row.addWidget(self._combo, 1)
        for label, fn in (("Browse…", self._browse), ("Rescan", self._scan_logs),
                          ("Enable logging vars", self._enable_logging)):
            b = _tool_btn(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        row = QHBoxLayout()
        self._chk = {}
        for key, label, on in (("err", "errors", True), ("warn", "warnings", True),
                               ("fixme", "fixme", False), ("info", "other", False)):
            c = QCheckBox(label)
            c.setChecked(on)
            c.setStyleSheet(f"color:{T['text2']};")
            c.toggled.connect(lambda _c: self._render())
            self._chk[key] = c
            row.addWidget(c)
        self._find = QLineEdit()
        self._find.setPlaceholderText("search…")
        self._find.setClearButtonEnabled(True)
        self._find.setStyleSheet(_LINEEDIT_QSS)
        # debounce: each keystroke used to re-render up to MAX_SHOW lines + the highlighter
        self._find_timer = QTimer(self)
        self._find_timer.setSingleShot(True)
        self._find_timer.setInterval(150)
        self._find_timer.timeout.connect(self._render)
        self._find.textChanged.connect(lambda _t: self._find_timer.start())
        row.addWidget(self._find, 1)
        self._follow = QCheckBox("follow")
        self._follow.setStyleSheet(f"color:{T['text2']};")
        self._follow.toggled.connect(lambda on: self._timer.start() if on else self._timer.stop())
        row.addWidget(self._follow)
        lay.addLayout(row)
        self._rules = QTableWidget(0, 4)
        self._rules.setHorizontalHeaderLabels(["Detected", "×", "Hint", ""])
        self._rules.verticalHeader().setVisible(False)
        self._rules.setEditTriggers(QTableWidget.NoEditTriggers)
        self._rules.setSelectionMode(QTableWidget.NoSelection)
        self._rules.setWordWrap(True)
        self._rules.setMaximumHeight(150)
        hh = self._rules.horizontalHeader()
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self._rules.setStyleSheet(_table_qss())
        lay.addWidget(self._rules)
        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._view.setStyleSheet(_MONO_BOX_QSS)
        self._hl = LogHighlighter(self._view.document())
        lay.addWidget(self._view, 1)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tail)
        self._first = True

    # -- discovery --------------------------------------------------------
    @staticmethod
    def _log_dirs() -> List[Path]:
        dirs = [Path.home(), _xdg("XDG_CACHE_HOME", ".cache") / "drstool" / "logs",
                _xdg("XDG_DATA_HOME", ".local/share") / "lutris" / "logs"]
        return dirs

    def _scan_logs(self):
        w = self.window()
        env = w.collect_launch_payload().get("env", {}) if hasattr(w, "collect_launch_payload") else {}
        dirs = self._log_dirs()
        for k in ("PROTON_LOG_DIR", "DXVK_LOG_PATH"):
            if env.get(k):
                dirs.append(Path(os.path.expanduser(env[k])))
        found = {}
        for d in dirs:
            try:
                for p in list(d.glob("steam-*.log")) + list(d.glob("*.log")):
                    if p.is_file():
                        found[p] = p.stat().st_mtime
            except OSError:
                continue
        cur = self._combo.currentData()
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("Select a log…", None)
        for p, mt in sorted(found.items(), key=lambda kv: -kv[1])[:60]:
            self._combo.addItem(f"{p.name}   {datetime.fromtimestamp(mt):%m-%d %H:%M}   "
                                f"{p.stat().st_size / 1048576:.1f} MB   {p.parent}", p)
        i = self._combo.findData(cur) if cur else -1
        self._combo.setCurrentIndex(max(i, 0))
        self._combo.blockSignals(False)

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        if self._first:
            self._first = False
            self._scan_logs()

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open log", str(Path.home()), "Logs (*.log *.txt);;All files (*)")
        if p:
            self._combo.blockSignals(True)
            self._combo.addItem(Path(p).name + "   (browsed)", Path(p))
            self._combo.setCurrentIndex(self._combo.count() - 1)
            self._combo.blockSignals(False)
            self._load(Path(p))

    def _on_pick(self, _i):
        p = self._combo.currentData()
        if p:
            self._load(p)

    def _enable_logging(self):
        w = self.window()
        d = _xdg("XDG_CACHE_HOME", ".cache") / "drstool" / "logs"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")
            return
        # Full verbose profile, not just PROTON_LOG=1: a WINEDEBUG=-all /
        # DXVK_LOG_LEVEL=none left over from "Log off" would otherwise win over
        # Proton's logging defaults and leave the log nearly empty.
        for k, v in (*LOG_ENV_ON.items(), ("PROTON_LOG_DIR", str(d)), ("DXVK_LOG_PATH", str(d))):
            w.set_env_var(k, v)
        self._status.setText(f"Verbose logging profile set (Log on; PROTON_LOG_DIR, DXVK_LOG_PATH → {d}). "
                             "Run the game, then Rescan.")

    # -- reading ----------------------------------------------------------
    @staticmethod
    def _whole_lines(data: bytes, final: bool) -> bytes:
        """Cut a trailing, still-being-written line off (it is read again on the
        next tail) — reading mid-line used to split it into two bogus lines."""
        if final or data.endswith(b"\n"):
            return data
        cut = data.rfind(b"\n")
        return data[:cut + 1] if cut >= 0 else b""

    def _ingest(self, lines: List[str], reset: bool):
        """Per-line severity and signature counts are computed once per line
        (they used to be recomputed over the whole buffer on every 1 s tail)."""
        if reset:
            self._lines, self._levels, self._hits = [], [], {}
        levels, hits = _log_analyse(lines)
        self._lines += lines
        self._levels += levels
        if len(self._lines) > 200000:
            self._lines, self._levels = self._lines[-150000:], self._levels[-150000:]
            hits = _log_analyse(self._lines)[1]          # trimmed: recount what is left
            self._hits = {}
        for code, n in hits.items():
            self._hits[code] = self._hits.get(code, 0) + n

    def _load(self, path: Path):
        """Reads and analyses on a worker thread (a 6 MB log froze the window for ~1.5 s)."""
        self._path = path
        self._gen += 1
        self._loading = True
        self._status.setText(f"Loading {path.name}…")
        th = _LogLoadThread(self._gen, path, self.MAX_READ)
        self._loaders.append(th)
        th.done.connect(self._on_loaded)
        th.failed.connect(self._on_load_failed)
        th.finished.connect(lambda t=th: self._loaders.remove(t))
        th.start()

    def _on_load_failed(self, gen: int, msg: str):
        if gen == self._gen:
            self._loading = False
            self._status.setText(msg)

    def _on_loaded(self, gen: int, pos: int, lines, levels, hits):
        if gen != self._gen:                  # a newer file was picked meanwhile
            return
        self._loading = False
        self._pos = pos
        self._lines, self._levels, self._hits = lines, levels, hits
        self._render()

    def _tail(self):
        p = self._path
        if p is None or self._loading:
            return
        try:
            size = p.stat().st_size
        except OSError:
            return
        if size < self._pos:
            self._load(p)
            return
        if size == self._pos:
            return
        with open(p, "rb") as f:
            f.seek(self._pos)
            data = f.read(self.MAX_READ)
        # A single line longer than MAX_READ is taken as it is.
        data = self._whole_lines(data, len(data) >= self.MAX_READ)
        if not data:
            return
        self._pos += len(data)
        self._ingest(data.decode("utf-8", "replace").splitlines(), False)
        self._render()

    def _render(self):
        want = {k for k, c in self._chk.items() if c.isChecked()}
        needle = self._find.text().lower()
        shown = [ln for ln, lv in zip(self._lines, self._levels)
                 if lv in want and (not needle or needle in ln.lower())]
        trimmed = len(shown) > self.MAX_SHOW
        sb = self._view.verticalScrollBar()
        at_end = sb.value() >= sb.maximum() - 4
        self._view.setPlainText("\n".join(shown[-self.MAX_SHOW:]))
        if at_end or self._follow.isChecked():
            sb.setValue(sb.maximum())
        # signature scan over the whole buffer
        self._rules.setRowCount(0)
        w = self.window()
        for code, rx, title, hint, fixes in _LOG_RULES:
            hits = self._hits.get(code, 0)
            if not hits:
                continue
            r = self._rules.rowCount()
            self._rules.insertRow(r)
            self._rules.setItem(r, 0, QTableWidgetItem(title))
            self._rules.setItem(r, 1, QTableWidgetItem(str(hits)))
            self._rules.setItem(r, 2, QTableWidgetItem(hint))
            if fixes and hasattr(w, "set_env_var"):
                b = _tool_btn("Apply: " + ", ".join(f"{k}={v}" for k, v in fixes))
                b.clicked.connect(lambda _c=False, fx=fixes: [w.set_env_var(k, v) for k, v in fx])
                self._rules.setCellWidget(r, 3, b)
        self._rules.resizeRowsToContents()
        n_err = self._levels.count("err")
        self._status.setText(f"{self._path.name if self._path else ''}: {len(self._lines)} lines, {n_err} error line(s)"
                             + (f" — showing the last {self.MAX_SHOW}" if trimmed else ""))
