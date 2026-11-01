"""dxvk.conf page."""

import difflib as _dl
import os
import re
from pathlib import Path
from typing import List, Optional

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QVBoxLayout, QWidget,
)

from drstool_core.validate import check_dir
from .base import _atomic_write, _backup_file
from .field_check import watch
from .env_widgets import EnvVarEditorWidget
from .steam_utils import steam_install_dirs
from .theme import T
from .toasts import offer_undo
from .tool_ui import _tool_btn
from .widgets import (
    _COMBO_QSS, _desc_label, _LINEEDIT_QSS, _MONO_BOX_QSS, _slugify, _title_label, _xdg,
)


# ----------------------------------------------------------------------------
# dxvk.conf writer (DXVK_CONFIG -> file)
# ----------------------------------------------------------------------------

_DXVK_MARK_B = "# >>> DRSTool (managed) >>>"
_DXVK_MARK_E = "# <<< DRSTool <<<"


def _strip_dxvk_block(text: str) -> str:
    return re.sub(re.escape(_DXVK_MARK_B) + r".*?" + re.escape(_DXVK_MARK_E) + r"\n?", "",
                  text, flags=re.S)


def merge_dxvk_conf(existing: str, pairs: List[tuple]) -> str:
    """Hand-written lines stay; keys DRSTool sets are moved into one managed block."""
    keys = {k for k, _ in pairs}
    out = []
    for ln in _strip_dxvk_block(existing).splitlines():
        m = re.match(r"\s*([A-Za-z0-9_.]+)\s*=", ln)
        if m and m.group(1) in keys:
            continue
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    # One key per line: a CR/LF inside a value (e.g. from an imported profile) would
    # otherwise write extra, unreviewed dxvk.conf keys outside what the picker shows.
    block = ([_DXVK_MARK_B]
             + [f"{k} = {re.sub(r'[\r\n]+', ' ', str(v))}" for k, v in pairs if re.fullmatch(r"[A-Za-z0-9_.]+", k)]
             + [_DXVK_MARK_E])
    return ("\n".join(out) + "\n\n" if out else "") + "\n".join(block) + "\n"


class DxvkConfWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("dxvk.conf writer"))
        lay.addWidget(_desc_label(
            "Turns the DXVK_CONFIG key picker (Environment tab) into a real dxvk.conf. DXVK reads "
            "dxvk.conf from the game's working directory, or from the file named by DXVK_CONFIG_FILE. "
            "Hand-written lines in an existing file are kept; DRSTool's keys live in one marked block. "
            "vkd3d-proton has no config file — VKD3D_CONFIG stays an environment variable."))
        row = QHBoxLayout()
        row.addWidget(QLabel("Mode:"))
        self._mode = QComboBox()
        self._mode.setStyleSheet(_COMBO_QSS)
        self._mode.addItem("dxvk.conf in the game folder", 0)
        self._mode.addItem("File in ~/.config/drstool/dxvk/ + set DXVK_CONFIG_FILE", 1)
        self._mode.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(self._mode, 1)
        lay.addLayout(row)
        row = QHBoxLayout()
        self._steam = QComboBox()
        self._steam.setStyleSheet(_COMBO_QSS)
        self._steam.addItem("Steam game…", None)
        for label, p, _aid in steam_install_dirs():
            self._steam.addItem(label, p)
        self._steam.currentIndexChanged.connect(self._pick_steam)
        row.addWidget(self._steam, 1)
        self._dir = QLineEdit()
        self._dir.setPlaceholderText("game folder (where the .exe runs) / name for mode 2")
        self._dir.setStyleSheet(_LINEEDIT_QSS)
        self._dir.textChanged.connect(lambda _t: self.refresh())
        self._dir_check = watch(self._dir, lambda t: check_dir(t) if self._mode.currentData() == 0 else "")
        self._mode.currentIndexChanged.connect(lambda _i: self._dir_check())
        row.addWidget(self._dir, 2)
        b = _tool_btn("Browse…")
        b.clicked.connect(self._browse)
        row.addWidget(b)
        lay.addLayout(row)
        self._clear_env = QCheckBox("clear DXVK_CONFIG from the environment after writing "
                                    "(the file replaces it)")
        self._clear_env.setStyleSheet(f"color:{T['text2']};")
        lay.addWidget(self._clear_env)
        self._out = QPlainTextEdit()
        self._out.setReadOnly(True)
        self._out.setStyleSheet(_MONO_BOX_QSS)
        lay.addWidget(self._out, 1)
        btns = QHBoxLayout()
        self._wr = _tool_btn("Write dxvk.conf", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        self._wr.clicked.connect(self._write)
        btns.addWidget(self._wr)
        rm = _tool_btn("Remove DRSTool block")
        rm.clicked.connect(self._remove)
        btns.addWidget(rm)
        btns.addStretch(1)
        lay.addLayout(btns)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(self._status)

    def _pick_steam(self, _i):
        p = self._steam.currentData()
        if p is not None:
            self._dir.setText(str(p) if self._mode.currentData() == 0
                              else _slugify(re.sub(r"\s*\[\d+\]$", "", self._steam.currentText())))

    def _browse(self):
        p = QFileDialog.getExistingDirectory(self, "Game folder", self._dir.text() or str(Path.home()))
        if p:
            self._dir.setText(p)

    def _pairs(self) -> List[tuple]:
        w = self.window()
        env = w.collect_launch_payload().get("env", {}) if hasattr(w, "collect_launch_payload") else {}
        return [(k, v) for k, v in EnvVarEditorWidget._parse_dxvk_config(env.get("DXVK_CONFIG", ""))
                if v is not None]

    def _target(self) -> Optional[Path]:
        t = self._dir.text().strip()
        if not t:
            return None
        if self._mode.currentData() == 1:
            return _xdg("XDG_CONFIG_HOME", ".config") / "drstool" / "dxvk" / f"{_slugify(t)}.conf"
        p = Path(os.path.expanduser(t))
        return p if p.name == "dxvk.conf" else p / "dxvk.conf"

    def _new_text(self, path: Path) -> str:
        try:
            old = path.read_text(errors="replace")
        except OSError:
            old = ""
        return merge_dxvk_conf(old, self._pairs()), old

    def refresh(self):
        pairs = self._pairs()
        path = self._target()
        if not pairs:
            self._out.setPlainText("DXVK_CONFIG is empty — set some keys in Environment → DXVK_CONFIG first.")
            return
        if path is None:
            self._out.setPlainText("Pick a game folder (or a name in mode 2).\n\nGenerated block:\n" +
                                   "\n".join(f"{k} = {v}" for k, v in pairs))
            return
        new, old = self._new_text(path)
        diff = "\n".join(_dl.unified_diff(old.splitlines(), new.splitlines(),
                                          f"{path} (current)", f"{path} (new)", lineterm=""))
        self._out.setPlainText(f"{path}\n\n" + (diff or "(no changes)"))

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        self.refresh()

    def _write(self):
        path = self._target()
        if path is None or not self._pairs():
            self._status.setText("⚠ Nothing to write (no DXVK_CONFIG keys or no destination).")
            return
        new, old = self._new_text(path)
        if new == old:
            self._status.setText("Already up to date.")
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                _backup_file(path)
            _atomic_write(path, new)
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")
            return
        msg = f"✔ Wrote {path}"
        w = self.window()
        if self._mode.currentData() == 1 and hasattr(w, "set_env_var"):
            w.set_env_var("DXVK_CONFIG_FILE", str(path))
            msg += "  (DXVK_CONFIG_FILE set)"
        if self._clear_env.isChecked() and hasattr(w, "set_env_var"):
            w.set_env_var("DXVK_CONFIG", "")
            msg += "  (DXVK_CONFIG cleared)"
        self._status.setText(msg)
        offer_undo(msg)
        self.refresh()

    def _remove(self):
        path = self._target()
        if path is None or not path.exists():
            self._status.setText("No file at the destination.")
            return
        old = path.read_text(errors="replace")
        new = _strip_dxvk_block(old)
        if new == old:
            self._status.setText("No DRSTool block in that file.")
            return
        try:
            _backup_file(path)
            _atomic_write(path, new.rstrip("\n") + "\n" if new.strip() else "")
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")
            return
        self._status.setText(f"✔ Removed the DRSTool block from {path}")
        offer_undo(f"Removed the DRSTool block from {path.name}")
        self.refresh()
