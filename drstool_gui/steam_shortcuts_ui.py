"""Shortcut (launcher script) and Add-to-Steam pages."""

import logging
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QSpinBox, QVBoxLayout, QWidget,
)

from .base import (
    _atomic_write, _atomic_write_bytes, _backup_file, _CORE_ERR, _proc_running, _run, game_scan,
    steam_shortcuts,
)
from .launch_line import build_launch_line
from .launchers import SteamBackend
from .lgtune import lgtune_mode
from .steam_utils import (
    STEAM_COMPAT_PATH, steam_compat_tools, vdf_get_app_value, vdf_remove_app_block,
    vdf_set_app_value,
)
from .theme import T
from .toasts import offer_undo, toast
from .tool_ui import _tool_btn
from .widgets import (
    _COMBO_QSS, _desc_label, _LINEEDIT_QSS, _MONO_BOX_QSS, _slugify, _title_label, _xdg,
)

_log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Shortcut: standalone launcher script + .desktop entry
# ----------------------------------------------------------------------------

class ShortcutWidget(QWidget):
    """Writes ~/.local/share/drstool/launchers/<slug>.sh and a .desktop entry that runs it —
    the current DRSTool configuration baked into a launcher that works without Lutris/Steam."""

    LGTUNE = "/usr/local/bin/lutris-game-tune-wrapper"

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("Standalone launcher shortcut"))
        lay.addWidget(_desc_label(
            "Bakes the current configuration (env, DRS settings, gamescope, MangoHud, and the "
            "umu-run wrapper if enabled on the Launch line tab) into a shell script plus a "
            ".desktop entry. The script is a snapshot: re-create it after changing the setup."))
        form = QGridLayout()
        form.setSpacing(6)
        form.setColumnStretch(1, 1)

        def row(r, label, widget, extra=None):
            l = QLabel(label)
            l.setStyleSheet(f"color:{T['text2']};")
            form.addWidget(l, r, 0)
            form.addWidget(widget, r, 1)
            if extra is not None:
                form.addWidget(extra, r, 2)

        self._name = QLineEdit()
        self._name.setPlaceholderText("Alan Wake 2")
        self._cmd = QLineEdit()
        self._cmd.setPlaceholderText("exe or script — with umu: path to the .exe; e.g. ~/Games/aw2/AlanWake2.exe")
        self._wd = QLineEdit()
        self._wd.setPlaceholderText("optional working directory (cd before launch)")
        self._icon = QLineEdit()
        self._icon.setPlaceholderText("optional icon path or theme icon name")
        for w in (self._name, self._cmd, self._wd, self._icon):
            w.setStyleSheet(_LINEEDIT_QSS)
            w.textChanged.connect(lambda _t: self.refresh())
        b_cmd = _tool_btn("Browse…")
        b_cmd.clicked.connect(lambda: self._browse(self._cmd, False))
        b_wd = _tool_btn("Browse…")
        b_wd.clicked.connect(lambda: self._browse(self._wd, True))
        b_ic = _tool_btn("Browse…")
        b_ic.clicked.connect(lambda: self._browse(self._icon, False))
        row(0, "Name:", self._name)
        row(1, "Command:", self._cmd, b_cmd)
        row(2, "Working dir:", self._wd, b_wd)
        row(3, "Icon:", self._icon, b_ic)
        lay.addLayout(form)

        opt = QHBoxLayout()
        self._lg = QCheckBox("run through lutris-game-tune (PRE → RUN → POST)")
        self._lg.setStyleSheet(f"color:{T['text2']};")
        self._lg.setEnabled(Path(self.LGTUNE).exists())
        self._lg.setToolTip(f"{self.LGTUNE}" + ("" if Path(self.LGTUNE).exists() else "  — not installed"))
        self._lg.toggled.connect(lambda _c: self.refresh())
        opt.addWidget(self._lg)
        self._nice = QSpinBox()
        self._nice.setRange(-20, 19)
        self._nice.setValue(-10)
        self._nice.valueChanged.connect(lambda _v: self.refresh())
        opt.addWidget(QLabel("nice"))
        opt.addWidget(self._nice)
        opt.addStretch(1)
        lay.addLayout(opt)

        self._out = QPlainTextEdit()
        self._out.setReadOnly(True)
        self._out.setStyleSheet(_MONO_BOX_QSS)
        self._out.setMinimumHeight(150)
        lay.addWidget(self._out, 1)

        btns = QHBoxLayout()
        self._create = _tool_btn("Create launcher", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        self._create.clicked.connect(self._write)
        btns.addWidget(self._create)
        btns.addWidget(QLabel("Existing:"))
        self._existing = QComboBox()
        self._existing.setStyleSheet(_COMBO_QSS)
        self._existing.setMinimumWidth(200)
        btns.addWidget(self._existing)
        rm = _tool_btn("Remove")
        rm.clicked.connect(self._remove)
        btns.addWidget(rm)
        btns.addStretch(1)
        lay.addLayout(btns)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(self._status)
        self._list_existing()

    # -- paths ------------------------------------------------------------
    @staticmethod
    def _script_dir() -> Path:
        return _xdg("XDG_DATA_HOME", ".local/share") / "drstool" / "launchers"

    @staticmethod
    def _desktop_dir() -> Path:
        return _xdg("XDG_DATA_HOME", ".local/share") / "applications"

    def _browse(self, edit: QLineEdit, folder: bool):
        if folder:
            p = QFileDialog.getExistingDirectory(self, "Working directory", edit.text() or str(Path.home()))
        else:
            p, _ = QFileDialog.getOpenFileName(self, "Select file", edit.text() or str(Path.home()))
        if p:
            edit.setText(p)

    def _list_existing(self):
        self._existing.clear()
        try:
            for p in sorted(self._desktop_dir().glob("drstool-*.desktop")):
                self._existing.addItem(p.stem[len("drstool-"):], p)
        except OSError:
            pass

    # -- content ----------------------------------------------------------
    def _line_cmd(self) -> str:
        c = self._cmd.text().strip()
        return c

    def build_script(self) -> str:
        w = self.window()
        ll = getattr(w, "_launch_line", None)
        cmd = self._line_cmd()
        payload = w.collect_launch_payload() if hasattr(w, "collect_launch_payload") else {}
        if ll is not None:
            # Always `env …`: without it the `exec A=1 cmd` line below fails in sh.
            line = ll.build_line(cmd or "<command>", force_env=True)
        else:
            line = build_launch_line(payload, cmd or "<command>", True)
        lines = ["#!/bin/sh", f"# generated by DRSTool for: {self._name.text().strip() or '(unnamed)'}"]
        wd = self._wd.text().strip()
        if wd:
            lines.append(f"cd {shlex.quote(os.path.expanduser(wd))} || exit 1")
        if self._lg.isChecked():
            priv = "pkexec " if lgtune_mode() == "polkit" else ""
            lines += [f"W={shlex.quote(self.LGTUNE)}", f'{priv}"$W" PRE', f"trap '{priv}\"$W\" POST' EXIT",
                      f'"$W" RUN {self._nice.value()} {line}']
        else:
            lines.append(f"exec {line}")
        return "\n".join(lines) + "\n"

    def refresh(self):
        self._out.setPlainText(self.build_script())

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        self.refresh()

    def _write(self):
        name = self._name.text().strip()
        if not name or not self._cmd.text().strip():
            self._status.setText("⚠ Name and command are required.")
            return
        slug = _slugify(name)
        sp = self._script_dir() / f"{slug}.sh"
        dp = self._desktop_dir() / f"drstool-{slug}.desktop"
        if (sp.exists() or dp.exists()) and QMessageBox.question(
                self, "Overwrite launcher", f"{dp.name} already exists. Overwrite it?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        # .desktop values are line-based: a CR/LF in the name or icon would start a new
        # key (e.g. a second Exec=). Inside a quoted Exec argument the spec also requires
        # escaping \ " ` $ and doubling % (field codes).
        icon = " ".join(self._icon.text().split())
        qn = " ".join(name.split())
        exec_path = re.sub(r'([\\"`$])', r"\\\1", str(sp)).replace("%", "%%")
        desktop = ("[Desktop Entry]\nType=Application\n"
                   f"Name={qn}\nExec=\"{exec_path}\"\n"
                   + (f"Icon={icon}\n" if icon else "") +
                   "Terminal=false\nCategories=Game;\nX-DRSTool=true\n")
        try:
            sp.parent.mkdir(parents=True, exist_ok=True)
            dp.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(sp, self.build_script())
            os.chmod(sp, 0o755)
            _atomic_write(dp, desktop)
            os.chmod(dp, 0o755)
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")
            return
        if shutil.which("update-desktop-database"):
            _run(["update-desktop-database", str(self._desktop_dir())], 5)
        self._status.setText(f"✔ Created {dp}  →  {sp}")
        self._list_existing()

    def _remove(self):
        dp = self._existing.currentData()
        if not dp:
            return
        slug = dp.stem[len("drstool-"):]
        if QMessageBox.question(self, "Remove launcher", f"Delete {dp.name} and its script?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        for p in (dp, self._script_dir() / f"{slug}.sh"):
            try:
                p.unlink()
            except OSError:
                pass
        self._status.setText(f"Removed {dp.name}")
        self._list_existing()


_WIN_EXE_RE = re.compile(r"\.(exe|bat|msi|lnk)$", re.I)


def _launcher_cmd(kind: str) -> tuple:
    """(program, argument prefix) that starts Lutris / Heroic, native or Flatpak."""
    if kind == "lutris":
        native, flat = shutil.which("lutris"), "net.lutris.Lutris"
    else:
        native, flat = shutil.which("heroic"), "com.heroicgameslauncher.hgl"
    if native:
        return native, ""
    fp = shutil.which("flatpak")
    if fp and _run([fp, "info", flat], 5).strip():
        return fp, f"run {flat} "
    return (shutil.which("xdg-open") or "xdg-open"), ""


class SteamShortcutsWidget(QWidget):
    """Profiles › sync tab "Add to Steam": a program, a DRSTool launcher, a Lutris or a Heroic
    game becomes a non-Steam game in the Steam library, with the current configuration in its
    launch options and optionally a Proton version."""

    SRC_PROGRAM, SRC_LAUNCHER, SRC_LUTRIS, SRC_HEROIC = range(4)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._games: Dict[str, list] = {}
        self._compat_root: Optional[Path] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("Add a game to Steam"))
        lay.addWidget(_desc_label(
            "Creates a non-Steam game in the Steam library (shortcuts.vdf). For a program or "
            "script the current DRSTool configuration goes into its launch options, and a "
            "Windows .exe can get a Proton version. Lutris and Heroic games are started through "
            "their launcher — configure those with the Lutris / Heroic tabs. Steam must be "
            "closed; a backup of shortcuts.vdf is kept next to it."))
        if _CORE_ERR:
            lay.addWidget(_desc_label("⚠ " + _CORE_ERR))
            lay.addStretch(1)
            self._ok = False
            return
        self._ok = True

        form = QGridLayout()
        form.setSpacing(6)
        form.setColumnStretch(1, 1)
        r = 0

        def add_row(label, widget, extra=None):
            nonlocal r
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color:{T['text2']};")
            form.addWidget(lbl, r, 0)
            form.addWidget(widget, r, 1)
            if extra is not None:
                form.addWidget(extra, r, 2)
            r += 1
            return lbl

        self._acct = QComboBox()
        self._acct.setStyleSheet(_COMBO_QSS)
        self._acct.currentIndexChanged.connect(lambda _i: self._on_account())
        add_row("Steam account:", self._acct)

        self._src = QComboBox()
        self._src.setStyleSheet(_COMBO_QSS)
        for t in ("Program or script", "DRSTool launcher (Shortcut tab)", "Lutris game", "Heroic game"):
            self._src.addItem(t)
        self._src.currentIndexChanged.connect(lambda _i: self._on_source())
        add_row("Add:", self._src)

        self._pick = QComboBox()
        self._pick.setStyleSheet(_COMBO_QSS)
        self._pick.currentIndexChanged.connect(lambda _i: self._on_pick())
        self._pick_lbl = add_row("Game:", self._pick)

        self._name = QLineEdit()
        self._name.setPlaceholderText("Name shown in the Steam library")
        self._exe = QLineEdit()
        self._exe.setPlaceholderText("program, script or Windows .exe")
        self._dir = QLineEdit()
        self._dir.setPlaceholderText("start folder (default: the program's folder)")
        self._icon = QLineEdit()
        self._icon.setPlaceholderText("optional .png / .ico")
        self._args = QLineEdit()
        self._args.setPlaceholderText("extra arguments for the game (optional)")
        for w in (self._name, self._exe, self._dir, self._icon, self._args):
            w.setStyleSheet(_LINEEDIT_QSS)
            w.textChanged.connect(lambda _t: self.refresh_preview())
        b_exe = _tool_btn("Browse…")
        b_exe.clicked.connect(lambda: self._browse(self._exe, False))
        b_dir = _tool_btn("Browse…")
        b_dir.clicked.connect(lambda: self._browse(self._dir, True))
        b_ic = _tool_btn("Browse…")
        b_ic.clicked.connect(lambda: self._browse(self._icon, False))
        add_row("Name:", self._name)
        self._exe_lbl = add_row("Program:", self._exe, b_exe)
        self._b_exe = b_exe
        add_row("Start in:", self._dir, b_dir)
        add_row("Icon:", self._icon, b_ic)
        self._args_lbl = add_row("Arguments:", self._args)

        self._compat = QComboBox()
        self._compat.setStyleSheet(_COMBO_QSS)
        self._compat.currentIndexChanged.connect(lambda _i: self.refresh_preview())
        self._compat_touched = False
        self._compat.activated.connect(lambda _i: setattr(self, "_compat_touched", True))
        self._exe.textChanged.connect(self._auto_compat)
        self._compat_lbl = add_row("Proton:", self._compat)
        lay.addLayout(form)

        self._embed = QCheckBox("Put the current DRSTool configuration into the launch options")
        self._embed.setChecked(True)
        self._embed.setStyleSheet(f"color:{T['text2']};")
        self._embed.toggled.connect(lambda _c: self.refresh_preview())
        lay.addWidget(self._embed)

        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setStyleSheet(_MONO_BOX_QSS)
        self._preview.setMinimumHeight(130)
        lay.addWidget(self._preview, 1)

        btns = QHBoxLayout()
        self._add_btn = _tool_btn("Add to Steam", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        self._add_btn.clicked.connect(self._apply)
        btns.addWidget(self._add_btn)
        btns.addSpacing(16)
        btns.addWidget(QLabel("Added by DRSTool:"))
        self._existing = QComboBox()
        self._existing.setStyleSheet(_COMBO_QSS)
        self._existing.setMinimumWidth(200)
        btns.addWidget(self._existing, 1)
        ed = _tool_btn("Edit")
        ed.setToolTip("Load the selected shortcut into the form (Add then updates it)")
        ed.clicked.connect(self._edit_existing)
        btns.addWidget(ed)
        rm = _tool_btn("Remove")
        rm.clicked.connect(self._remove)
        btns.addWidget(rm)
        lay.addLayout(btns)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(self._status)
        self._first = True

    # -- data -------------------------------------------------------------
    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        if self._ok and self._first:
            self._first = False
            QTimer.singleShot(0, self.rescan)

    def rescan(self):
        self._acct.blockSignals(True)
        self._acct.clear()
        for root in SteamBackend.roots():
            for acc in steam_shortcuts.find_accounts(root):
                name = ""
                try:
                    m = re.search(r'"PersonaName"\s+"([^"]*)"',
                                  (acc / "config" / "localconfig.vdf").read_text(errors="replace")[:200000])
                    name = m.group(1) if m else ""
                except OSError:
                    pass
                self._acct.addItem(f"{name or 'account'}  ({acc.name})", (str(root), str(acc)))
        self._acct.blockSignals(False)
        if not self._acct.count():
            self._status.setText("⚠ No Steam account found (~/.local/share/Steam, ~/.steam, Flatpak).")
            self._add_btn.setEnabled(False)
        self._games = {}
        try:
            self._games["lutris"] = game_scan.scan_lutris()
        except Exception:
            _log.debug("ignored error", exc_info=True)
            self._games["lutris"] = []
        try:
            self._games["heroic"] = game_scan.scan_heroic()
        except Exception:
            _log.debug("ignored error", exc_info=True)
            self._games["heroic"] = []
        self._on_account()
        self._on_source()

    def _account(self) -> Optional[tuple]:
        d = self._acct.currentData()
        return (Path(d[0]), Path(d[1])) if d else None

    def _vdf_path(self) -> Optional[Path]:
        a = self._account()
        return steam_shortcuts.shortcuts_path(a[1]) if a else None

    def _on_account(self):
        a = self._account()
        if a is None:
            return
        root = a[0]
        if root != self._compat_root:
            self._compat_root = root
            self._compat.blockSignals(True)
            self._compat.clear()
            self._compat.addItem("(none — run natively)", "")
            for internal, label in steam_compat_tools(root):
                self._compat.addItem(label, internal)
            self._compat.setProperty("_pe", self._preferred_compat())
            self._compat.blockSignals(False)
        self._list_existing()
        self.refresh_preview()

    def _list_existing(self):
        self._existing.clear()
        p = self._vdf_path()
        if p is None:
            return
        try:
            root = steam_shortcuts.load(p)
            for s in steam_shortcuts.list_shortcuts(root):
                if s.is_drstool:
                    self._existing.addItem(s.name, s.appid)
        except (OSError, steam_shortcuts.VdfError) as ex:
            self._status.setText(f"⚠ cannot read {p.name}: {ex}")

    def _on_source(self):
        src = self._src.currentIndex()
        if src == self.SRC_PROGRAM and getattr(self, "_last_src", src) != src:
            for w in (self._name, self._exe, self._dir, self._args):     # drop the auto-filled values
                w.clear()
        self._last_src = src
        self._pick.blockSignals(True)
        self._pick.clear()
        if src == self.SRC_LAUNCHER:
            try:
                for sp in sorted(ShortcutWidget._script_dir().glob("*.sh")):
                    self._pick.addItem(sp.stem, str(sp))
            except OSError:
                pass
        elif src in (self.SRC_LUTRIS, self.SRC_HEROIC):
            for g in self._games.get("lutris" if src == self.SRC_LUTRIS else "heroic", []):
                if g.launch_id:
                    self._pick.addItem(g.name, g)
        self._pick.blockSignals(False)
        picking = src != self.SRC_PROGRAM
        self._pick.setVisible(picking)
        self._pick_lbl.setVisible(picking)
        prog = src == self.SRC_PROGRAM
        self._exe.setReadOnly(not prog)
        self._b_exe.setEnabled(prog)
        self._embed.setEnabled(prog)
        self._args.setEnabled(src in (self.SRC_PROGRAM, self.SRC_LAUNCHER))
        if picking and not self._pick.count():
            self._status.setText({self.SRC_LAUNCHER: "No DRSTool launchers yet — create one on the Shortcut tab.",
                                  self.SRC_LUTRIS: "No installed Lutris games found.",
                                  self.SRC_HEROIC: "No installed Heroic games found."}[src])
        else:
            self._status.setText("")
        self._on_pick()

    def _on_pick(self):
        src = self._src.currentIndex()
        data = self._pick.currentData()
        if src == self.SRC_LAUNCHER and data:
            self._name.setText(self._pick.currentText())
            self._exe.setText(data)
            self._dir.setText(str(Path(data).parent))
        elif src in (self.SRC_LUTRIS, self.SRC_HEROIC) and data is not None:
            prog, _pre = _launcher_cmd("lutris" if src == self.SRC_LUTRIS else "heroic")
            self._name.setText(data.name)
            self._exe.setText(prog)
            self._dir.setText(str(data.folders[0]) if data.folders else "")
        self.refresh_preview()

    def _preferred_compat(self) -> int:
        """Index of the newest GE-Proton, else the newest official Proton, else Experimental."""
        best, best_key = -1, None
        for i in range(1, self._compat.count()):
            internal = str(self._compat.itemData(i))
            low = internal.lower()
            if low.startswith("ge-proton"):
                rank = 3
            elif re.fullmatch(r"proton_\d+", low):
                rank = 2
            elif low == "proton_experimental":
                rank = 1
            else:
                continue
            key = (rank, tuple(int(x) for x in re.findall(r"\d+", internal)))
            if best_key is None or key > best_key:
                best, best_key = i, key
        return best

    def _auto_compat(self, text: str):
        """A Windows program gets a Proton pre-selected until the user picks one themselves."""
        if self._compat_touched or self._compat.currentIndex() > 0 or not _WIN_EXE_RE.search(text.strip()):
            return
        pe = self._compat.property("_pe")
        if isinstance(pe, int) and pe > 0:
            self._compat.setCurrentIndex(pe)

    def _browse(self, edit: QLineEdit, folder: bool):
        if folder:
            p = QFileDialog.getExistingDirectory(self, "Start folder", edit.text() or str(Path.home()))
        else:
            p, _ = QFileDialog.getOpenFileName(self, "Select file", edit.text() or str(Path.home()))
        if p:
            edit.setText(p)
            if edit is self._exe and not self._name.text().strip():
                self._name.setText(Path(p).stem)

    def _launch_options(self) -> str:
        src = self._src.currentIndex()
        extra = self._args.text().strip()
        if src == self.SRC_LUTRIS:
            g = self._pick.currentData()
            _prog, pre = _launcher_cmd("lutris")
            return f"{pre}lutris:rungame/{g.launch_id}" if g else ""
        if src == self.SRC_HEROIC:
            g = self._pick.currentData()
            prog, pre = _launcher_cmd("heroic")
            nogui = "" if os.path.basename(prog) == "xdg-open" else "--no-gui "
            return f"{pre}{nogui}heroic://launch/{g.launch_id}" if g else ""
        if src == self.SRC_PROGRAM and self._embed.isChecked():
            w = self.window()
            payload = w.collect_launch_payload() if hasattr(w, "collect_launch_payload") else {}
            if payload.get("env") or payload.get("gs_cmd"):
                # gs_cmd already ends with "-- [mangohud --dlsym]"
                return build_launch_line(payload, "%command%" + (f" {extra}" if extra else ""), True)
        return extra

    def _plan(self) -> dict:
        exe = os.path.expanduser(self._exe.text().strip())
        start = os.path.expanduser(self._dir.text().strip()) or (str(Path(exe).parent) if exe else "")
        name = self._name.text().strip()
        # Proton only for Windows programs; the (hidden) combo may still hold an
        # earlier pick after the program was changed to a native one.
        compat = (self._compat.currentData() or "") if _WIN_EXE_RE.search(exe) else ""
        return {"name": name, "exe": exe, "start": start, "icon": os.path.expanduser(self._icon.text().strip()),
                "launch": self._launch_options(), "compat": compat}

    def refresh_preview(self):
        if not self._ok:
            return
        pl = self._plan()
        exe_q = steam_shortcuts.quote_path(pl["exe"]) if pl["exe"] else ""
        appid = steam_shortcuts.shortcut_appid(exe_q, pl["name"]) if (exe_q and pl["name"]) else 0
        win = bool(_WIN_EXE_RE.search(pl["exe"]))
        self._compat.setVisible(win)
        self._compat_lbl.setVisible(win)
        lines = [f"Name:            {pl['name'] or '—'}",
                 f"Target:          {exe_q or '—'}",
                 f"Start in:        {steam_shortcuts.quote_path(pl['start']) if pl['start'] else '—'}",
                 f"Launch options:  {pl['launch'] or '(none)'}",
                 f"Proton:          {pl['compat'] or ('(none)' if win else 'n/a (native program)')}"]
        if appid:
            lines.append(f"Steam app id:    {appid}   (steam://rungameid/{steam_shortcuts.rungameid(appid)})")
        if pl["exe"] and not Path(pl["exe"]).exists() and self._src.currentIndex() == self.SRC_PROGRAM:
            lines.append("\n⚠ the program does not exist (yet)")
        if win and not pl["compat"]:
            lines.append("\n⚠ a Windows program without Proton will not start")
        self._preview.setPlainText("\n".join(lines))

    # -- actions ----------------------------------------------------------
    def _steam_busy(self) -> bool:
        if _proc_running("steam", "steamwebhelper"):
            QMessageBox.warning(self, "Add to Steam", "Steam is running. Close Steam first — it keeps its own "
                                "copy of the shortcuts and would overwrite this change when it exits.")
            return True
        return False

    def _write(self, root_kv, extra_cfg=None) -> str:
        """Dump + self-check + backup + atomic write. Returns the backup file name ('' if new)."""
        p = self._vdf_path()
        data = steam_shortcuts.dump(root_kv)
        check = steam_shortcuts.parse(data)
        if steam_shortcuts.dump(check) != data:
            raise ValueError("self-check failed: the new shortcuts.vdf does not read back identically")
        bak = _backup_file(p).name if p.is_file() else ""
        _atomic_write_bytes(p, data)
        return bak

    def _set_compat(self, appid: int, tool: str) -> str:
        a = self._account()
        cfg = a[0] / "config" / "config.vdf"
        if not cfg.is_file():
            return " (config.vdf not found — set Proton in Steam's properties)" if tool else ""
        text = cfg.read_text(errors="replace")
        key = str(appid)
        if tool:
            new = vdf_set_app_value(text, STEAM_COMPAT_PATH, key, "name", tool)
            new = vdf_set_app_value(new, STEAM_COMPAT_PATH, key, "config", "")
            new = vdf_set_app_value(new, STEAM_COMPAT_PATH, key, "priority", "250")
            ok = vdf_get_app_value(new, STEAM_COMPAT_PATH, key, "name") == tool
        else:
            new = vdf_remove_app_block(text, STEAM_COMPAT_PATH, key)
            ok = vdf_get_app_value(new, STEAM_COMPAT_PATH, key, "name") == ""
        if not ok:
            raise ValueError("self-check failed: config.vdf does not read back as intended")
        if new == text:
            return ""
        _backup_file(cfg)
        _atomic_write(cfg, new)
        return f" Proton: {tool}." if tool else " Proton mapping removed."

    def _apply(self):
        pl = self._plan()
        if not pl["name"] or not pl["exe"]:
            self._status.setText("⚠ Name and program are required.")
            return
        if self._vdf_path() is None:
            self._status.setText("⚠ No Steam account selected.")
            return
        if self._steam_busy():
            return
        try:
            root_kv = steam_shortcuts.load(self._vdf_path())
            appid, created = steam_shortcuts.upsert_shortcut(root_kv, pl["name"], pl["exe"], pl["start"],
                                                            pl["launch"], pl["icon"])
            bak = self._write(root_kv)
            msg = self._set_compat(appid, pl["compat"]) if (pl["compat"] or not created) else ""
        except (OSError, ValueError, steam_shortcuts.VdfError) as ex:
            self._status.setText(f"⚠ {ex}")
            toast(f"Add to Steam failed: {ex}", "error")
            return
        verb = "Added" if created else "Updated"
        self._status.setText(f"✔ {verb} “{pl['name']}” in the Steam library (app id {appid}).{msg}"
                             + (f" Backup: {bak}" if bak else "") + " Start Steam to see it.")
        offer_undo(f"{verb} “{pl['name']}” in Steam")
        self._list_existing()
        i = self._existing.findData(appid)
        if i >= 0:
            self._existing.setCurrentIndex(i)

    def _edit_existing(self):
        appid = self._existing.currentData()
        p = self._vdf_path()
        if appid is None or p is None:
            return
        try:
            s = next((x for x in steam_shortcuts.list_shortcuts(steam_shortcuts.load(p)) if x.appid == appid), None)
        except (OSError, steam_shortcuts.VdfError) as ex:
            self._status.setText(f"⚠ {ex}")
            return
        if s is None:
            return
        self._src.setCurrentIndex(self.SRC_PROGRAM)
        self._embed.setChecked(False)
        self._name.setText(s.name)
        self._exe.setText(s.exe)
        self._dir.setText(s.start_dir)
        self._icon.setText(s.icon)
        self._args.setText(s.launch_options)
        a = self._account()
        try:
            cur = vdf_get_app_value((a[0] / "config" / "config.vdf").read_text(errors="replace"),
                                    STEAM_COMPAT_PATH, str(appid), "name")
        except (OSError, ValueError):
            cur = ""
        i = self._compat.findData(cur)
        self._compat.setCurrentIndex(i if i >= 0 else 0)
        self._status.setText(f"Editing “{s.name}” — its launch options are in Arguments; tick the box "
                             "above to replace them with the current configuration.")

    def _remove(self):
        appid = self._existing.currentData()
        name = self._existing.currentText()
        if appid is None or self._vdf_path() is None:
            return
        if QMessageBox.question(self, "Remove from Steam", f"Remove “{name}” from the Steam library?",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        if self._steam_busy():
            return
        try:
            root_kv = steam_shortcuts.load(self._vdf_path())
            if not steam_shortcuts.remove_shortcut(root_kv, appid):
                self._status.setText("Already gone.")
                self._list_existing()
                return
            bak = self._write(root_kv)
            self._set_compat(appid, "")
        except (OSError, ValueError, steam_shortcuts.VdfError) as ex:
            self._status.setText(f"⚠ {ex}")
            return
        self._status.setText(f"Removed “{name}”. Backup: {bak}")
        offer_undo(f"Removed “{name}” from Steam")
        self._list_existing()
