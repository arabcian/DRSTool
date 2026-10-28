"""Games tab: every installed game in one list, and one card per game that brings together
what is otherwise spread over the DLL Manager, Game hints, Logs, Frametime and the sync tabs:
the linked profile, the Proton / Wine build, DLSS / DXVK versions, the last frametime capture,
the last log errors and the ProtonDB tier.

Everything here only reads; writing a profile to a game goes through the sync tabs'
apply_target() (MainWindow.write_profile_to_games)."""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QPushButton, QScrollArea, QSizePolicy, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from drstool_core import game_info

from .base import _CORE_ERR, APP_ID, game_scan
from .dll_manager import detect_installed, PD_COMPONENTS
from .frametime import load_frametimes
from .game_hints import _HINT_CACHE_VER, _hints_cache_path, _HintWorker
from .launchers import SteamBackend
from .links import LINKS, profile_fp
from .logs import _log_analyse, _LOG_RULES, LogViewerWidget
from .tasks import TASKS
from .theme import btn_ss, caption, SCROLLABLE_CONTROL_QSS, T
from .toasts import toast
from .widgets import (
    _COMBO_QSS, _LINEEDIT_QSS, _pack2_table_ss, ElidedLabel, make_header_item, make_list_item,
    setup_sidebar_list,
)

_KIND_TITLES = {"dlss": "DLSS SR", "dlssd": "DLSS RR", "dlssg": "DLSS FG", "sl_sdk_0": "Streamline"}
_TIER_COLOR = {"platinum": "#b9cde4", "gold": "#d8b638", "silver": "#b0b6c0", "bronze": "#cd8a4a",
               "borked": T["danger"], "native": T["accent"]}
_RULE_TITLE = {code: title for code, _rx, title, *_ in _LOG_RULES}
_LAUNCHERS = ("Steam", "Lutris", "Heroic", "Bottles", "Custom")


def _ago(ts: float) -> str:
    if not ts:
        return ""
    d = max(0, time.time() - ts)
    for unit, sec in (("d", 86400), ("h", 3600), ("min", 60)):
        if d >= sec:
            return f"{int(d // sec)} {unit} ago"
    return "just now"


def _when(ts: float) -> str:
    return f"{datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}  ·  {_ago(ts)}" if ts else ""


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def default_target(game) -> Optional[dict]:
    """Where a sync tab would write this game's configuration (None = no launcher config)."""
    kind, key = game.launcher.lower(), game.key.split(":", 1)[1]
    if game.launcher == "Steam":
        acc = SteamBackend().accounts()
        if not acc:
            return None
        return {"kind": kind, "key": key, "path": acc[0][1], "title": f"{game.name}  [{key}]"}
    if game.launcher == "Lutris":
        for base in game_scan.lutris_dirs():
            yml = base / "games" / f"{key}.yml"
            if yml.is_file():
                return {"kind": kind, "key": key, "path": str(yml), "title": game.name}
        return None
    if game.launcher == "Heroic":
        for root in game_scan.heroic_roots():
            return {"kind": kind, "key": key, "path": str(root / "GamesConfig" / f"{key}.json"),
                    "title": f"{game.name}  [{key}]"}
        return None
    if game.launcher == "Bottles" and game.prefix is not None and (game.prefix / "bottle.yml").is_file():
        return {"kind": kind, "key": key, "path": str(game.prefix / "bottle.yml"),
                "title": f"{game.name}  [{key}]"}
    return None


def link_status(link: dict, profiles: dict) -> tuple:
    """-> (code, text) with code ok | behind | never | noprofile | missing | none."""
    prof = link.get("profile")
    if not prof:
        return ("noprofile", "written without a profile") if link.get("applied_at") else ("none", "")
    if prof not in profiles:
        return "missing", f"profile “{prof}” no longer exists"
    if not link.get("fp") or not link.get("applied_at"):
        return "never", "linked — not written to the game yet"
    if link["fp"] != profile_fp(profiles[prof]):
        return "behind", "the profile changed since the last write"
    return "ok", "game is up to date with the profile"


# ============================================================================
# Background work
# ============================================================================

class _GameScanWorker(QThread):
    done = Signal(object)                 # game_scan.ScanResult

    def __init__(self, custom: List[str], parent=None):
        super().__init__(parent)
        self._custom = custom

    def run(self):
        try:
            res = game_scan.scan_all(custom=self._custom)
        except Exception as ex:           # noqa: BLE001
            res = game_scan.ScanResult(errors=[str(ex)])
        self.done.emit(res)


class _CardWorker(QThread):
    """Collects everything the card shows (disk only, no network)."""
    done = Signal(str, dict)

    def __init__(self, game, env: Dict[str, str], link: dict, parent=None):
        super().__init__(parent)
        self._g, self._env, self._link = game, dict(env), dict(link)

    def run(self):
        g, info = self._g, {}
        for name, fn in (("runtime", self._runtime), ("dlls", self._dlls), ("files", self._files),
                         ("log", self._log), ("ft", self._ft), ("pdb", self._pdb)):
            try:
                info[name] = fn(info)
            except Exception as ex:        # noqa: BLE001 - one section must not hide the others
                info[name] = {"error": f"{type(ex).__name__}: {ex}"}
        self.done.emit(g.key, info)

    def _runtime(self, _info):
        return game_info.runtime_info(self._g)

    def _dlls(self, _info):
        g = self._g
        found = game_scan.find_dlls(g)
        out = {"ngx": {k: sorted({d.version_str for d in found.of(k)}) for k in _KIND_TITLES},
               "anti_cheat": list(found.anti_cheat), "truncated": found.truncated, "prefix": {}}
        if g.prefix is not None:
            for c in PD_COMPONENTS:
                st = detect_installed(g.prefix, c)
                out["prefix"][c["name"]] = (st["state"], st["version"])
        return out

    def _files(self, _info):
        return game_info.game_files(self._g)

    def _log(self, info):
        files = info.get("files") or {}
        dirs = LogViewerWidget._log_dirs()
        for k in ("PROTON_LOG_DIR", "DXVK_LOG_PATH"):
            if self._env.get(k):
                dirs.append(Path(self._env[k]).expanduser())
        logs = game_info.find_logs(self._g, dirs, files.get("dxvk_logs", []), files.get("exes", []))
        if not logs:
            return {}
        p = logs[0]
        lines = game_info.read_tail(p)
        levels, hits = _log_analyse(lines)
        errs = [ln.strip() for ln, lv in zip(lines, levels) if lv == "err"]
        return {"path": str(p), "mtime": p.stat().st_mtime, "n_err": len(errs), "errors": errs[-4:],
                "hits": sorted(hits.items(), key=lambda kv: -kv[1]), "others": [str(x) for x in logs[1:6]]}

    def _ft(self, info):
        files = info.get("files") or {}
        dirs = game_info.mangohud_output_dirs(self._env) + [Path.home()]
        cands = game_info.find_frametime_logs(self._g, dirs, files.get("exes", []))
        pinned = self._link.get("frametime")
        if pinned and Path(pinned).is_file():
            cands = [Path(pinned)] + cands
        if not cands:
            return {}
        p = max(cands, key=lambda c: c.stat().st_mtime)
        d = load_frametimes(str(p))
        st = {label: v for label, v, *_ in d["stats"]}
        return {"path": str(p), "mtime": p.stat().st_mtime, "kind": d["kind"],
                "avg": st.get("Average FPS"), "low1": st.get("1% low FPS"), "low01": st.get("0.1% low FPS"),
                "p99": st.get("p99 frametime (ms)"), "stutters": st.get("Stutters (> 2× median)"),
                "dur": st.get("Duration (s)"), "pinned": str(p) == pinned}

    def _pdb(self, _info):
        appid = self._link.get("appid") or game_info.steam_appid(self._g)
        if not appid:
            return {"appid": ""}
        try:
            d = json.loads(_hints_cache_path(appid).read_text())
        except (OSError, ValueError):
            return {"appid": appid}
        if d.get("v") != _HINT_CACHE_VER:
            return {"appid": appid}
        return {"appid": appid, "protondb": d.get("protondb"), "ts": d.get("ts", 0)}


# ============================================================================
# Sidebar: the game list
# ============================================================================

class GameListWidget(QWidget):
    """Every game found by game_scan, grouped by launcher; linked profiles shown on the right."""
    game_selected = Signal(object)        # game_scan.GameEntry

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.sm = settings_manager
        self.games: List = []
        self._by_key: Dict[str, object] = {}
        self._filter = ""
        self._worker: Optional[_GameScanWorker] = None
        self._scanned = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        row = QHBoxLayout()
        self._count = QLabel("")
        self._count.setStyleSheet(f"color:{T['muted']}; font-size:11px;")
        row.addWidget(self._count, 1)
        self._rescan_btn = QPushButton("↻ Rescan")
        self._rescan_btn.setCursor(Qt.PointingHandCursor)
        self._rescan_btn.setStyleSheet(btn_ss("ghost", "sm"))
        self._rescan_btn.setToolTip("Find games again (Steam, Lutris, Heroic, Bottles and the "
                                    "folders added in the DLL Manager)")
        self._rescan_btn.clicked.connect(self.rescan)
        row.addWidget(self._rescan_btn)
        lay.addLayout(row)
        self.list = QListWidget()
        setup_sidebar_list(self.list)
        self.list.currentItemChanged.connect(self._on_current)
        lay.addWidget(self.list, 1)
        LINKS.changed.connect(self.refresh)
        settings_manager.profiles_changed.connect(self.refresh)
        settings_manager.profile_saved.connect(lambda _n: self.refresh())

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        if not self._scanned:
            self.rescan()

    def rescan(self):
        if _CORE_ERR:
            self._count.setText(_CORE_ERR)
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._scanned = True
        v = QSettings(APP_ID, APP_ID).value("dllmgr/custom", [])      # the DLL Manager's extra folders
        if isinstance(v, str):
            v = [v] if v else []
        custom = [str(x) for x in (v or [])]
        self._count.setText("finding games …")
        self._rescan_btn.setEnabled(False)
        w = _GameScanWorker(custom, self)
        self._worker = w
        w.done.connect(self._on_scanned)
        w.finished.connect(lambda: self._rescan_btn.setEnabled(True))
        TASKS.track(w, "Games: scan")
        w.start()

    def _on_scanned(self, res):
        self.games = list(res.games)
        self._by_key = {g.key: g for g in self.games}
        self.refresh()
        tail = f"  ·  {len(res.errors)} launcher error(s)" if res.errors else ""
        self._count.setText(f"{len(self.games)} game(s){tail}")
        self._count.setToolTip("\n".join(res.errors))
        if self.list.currentItem() is None:
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.UserRole):
                    self.list.setCurrentRow(i)
                    break

    def game(self, key: str):
        return self._by_key.get(key)

    def set_filter(self, text: str):
        self._filter = text.strip().casefold()
        self.refresh()

    def refresh(self, *_):
        cur = self.list.currentItem()
        cur_key = cur.data(Qt.UserRole) if cur else None
        links = LINKS.all()
        profiles = self.sm.get_profiles()
        self.list.blockSignals(True)
        self.list.clear()
        for launcher in _LAUNCHERS:
            rows = [g for g in self.games if g.launcher == launcher
                    and (not self._filter or self._filter in g.name.casefold()
                         or self._filter in str((links.get(g.key) or {}).get("profile") or "").casefold())]
            if not rows:
                continue
            self.list.addItem(make_header_item(launcher, sum(1 for g in rows if (links.get(g.key) or {})
                                                             .get("profile"))))
            for g in rows:
                ln = links.get(g.key) or {}
                code, why = link_status(ln, profiles)
                prof = ln.get("profile") or ""
                val = ("⟳ " if code in ("behind", "never") else "") + prof
                it = make_list_item(g.name, g.key, is_set=bool(prof), value=val)
                it.setToolTip(f"{g.name}\n{g.key}" + (f"\nProfile: {prof} — {why}" if prof else ""))
                self.list.addItem(it)
                if g.key == cur_key:
                    self.list.setCurrentItem(it)
        self.list.blockSignals(False)

    def select_key(self, key: str):
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == key:
                self.list.setCurrentRow(i)
                return True
        return False

    def _on_current(self, it, _prev=None):
        g = self._by_key.get(it.data(Qt.UserRole)) if it else None
        if g is not None:
            self.game_selected.emit(g)


# ============================================================================
# The card
# ============================================================================

class _Tile(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("gcTile")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"#gcTile {{ background:{T['surface']}; border:1px solid {T['border']};"
                           " border-radius:10px; }")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(5)
        lay.addWidget(caption(title))
        self.value = QLabel("…")
        self.value.setStyleSheet(f"color:{T['text']}; font-size:16px; font-weight:700; background:transparent;")
        self.value.setWordWrap(True)
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.value)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.RichText)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setStyleSheet(f"color:{T['text2']}; font-size:12px; background:transparent;")
        lay.addWidget(self.detail)
        self.body = QVBoxLayout()
        self.body.setSpacing(6)
        lay.addLayout(self.body)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(6)
        lay.addSpacing(2)
        lay.addLayout(self.buttons)
        self.buttons.addStretch(1)

    def add_button(self, text: str, fn, kind: str = "default", tip: str = "") -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(btn_ss(kind, "sm"))
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(fn)
        self.buttons.insertWidget(self.buttons.count() - 1, b)
        return b

    def set(self, value: str, detail: str = "", color: str = ""):
        self.value.setText(value)
        self.value.setStyleSheet(f"color:{color or T['text']}; font-size:16px; font-weight:700;"
                                 " background:transparent;")
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))


def _rows_html(rows: List[tuple]) -> str:
    """[(label, value, color)] as a two-column table."""
    out = []
    for label, value, color in rows:
        out.append(f'<tr><td style="color:{T["muted"]}; padding-right:14px;">{_esc(label)}</td>'
                   f'<td style="color:{color or T["text"]}; font-family:monospace;">{_esc(value)}</td></tr>')
    return "<table cellspacing=0 cellpadding=1>" + "".join(out) + "</table>"


class GameCardWidget(QWidget):
    """Right-hand page of the Games tab."""

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.sm = settings_manager
        self.game = None
        self._info: dict = {}
        self._worker: Optional[_CardWorker] = None
        self._threads: List[QThread] = []
        self._narrow: Optional[bool] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setStyleSheet(SCROLLABLE_CONTROL_QSS)
        outer.addWidget(sc)
        inner = QWidget()
        sc.setWidget(inner)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(12)

        head = QHBoxLayout()
        tv = QVBoxLayout()
        tv.setSpacing(2)
        self._title = QLabel("")
        self._title.setStyleSheet(f"color:{T['text']}; font-size:20px; font-weight:800;")
        self._title.setWordWrap(True)
        tv.addWidget(self._title)
        self._sub = ElidedLabel("")
        self._sub.setStyleSheet(f"color:{T['muted']}; font-size:11px; font-family:{T['mono']};")
        tv.addWidget(self._sub)
        head.addLayout(tv, 1)
        self._reload_btn = QPushButton("↻ Refresh")
        self._reload_btn.setStyleSheet(btn_ss("ghost", "sm"))
        self._reload_btn.setCursor(Qt.PointingHandCursor)
        self._reload_btn.setToolTip("Read the game's files, logs and captures again")
        self._reload_btn.clicked.connect(self.reload)
        head.addWidget(self._reload_btn, 0, Qt.AlignTop)
        self._folder_btn = QPushButton("Open folder")
        self._folder_btn.setStyleSheet(btn_ss("ghost", "sm"))
        self._folder_btn.setCursor(Qt.PointingHandCursor)
        self._folder_btn.clicked.connect(self._open_folder)
        head.addWidget(self._folder_btn, 0, Qt.AlignTop)
        lay.addLayout(head)

        # ── tiles ───────────────────────────────────────────────────────────
        self.t_profile = _Tile("Profile")
        prow = QHBoxLayout()
        self._prof_combo = QComboBox()
        self._prof_combo.setStyleSheet(_COMBO_QSS)
        self._prof_combo.setToolTip("Link a profile to this game. Writing it to the game is a separate step.")
        self._prof_combo.activated.connect(self._assign_from_combo)
        prow.addWidget(self._prof_combo, 1)
        self.t_profile.body.addLayout(prow)
        self._write_btn = self.t_profile.add_button("Write to game", self._write, "primary")
        self._load_btn = self.t_profile.add_button("Load in editor", self._load_profile)
        self._unlink_btn = self.t_profile.add_button("Unlink", lambda: self._assign(None), "ghost")

        self.t_runtime = _Tile("Proton / Wine")

        self.t_dlls = _Tile("DLSS / DXVK")
        self.t_dlls.add_button("DLL Manager", lambda: self._open_tool("DLL Manager"), "ghost")

        self.t_ft = _Tile("Last frametime capture")
        self.t_ft.add_button("Pick capture…", self._pick_capture, "ghost",
                             "Use this MangoHud / FLM_CSV file for the game (the newest capture wins)")
        self._ft_open = self.t_ft.add_button("Open in Frametime", self._open_frametime, "ghost")

        self.t_log = _Tile("Last log errors")
        self._log_open = self.t_log.add_button("Open in Logs", self._open_log, "ghost")

        self.t_pdb = _Tile("ProtonDB")
        arow = QHBoxLayout()
        self._appid = QLineEdit()
        self._appid.setPlaceholderText("Steam appid")
        self._appid.setStyleSheet(_LINEEDIT_QSS)
        self._appid.setMaximumWidth(150)
        self._appid.setToolTip("Games that are not on Steam: the Steam appid of the same game "
                               "(Game hints › Find appid looks it up)")
        self._appid.editingFinished.connect(self._save_appid)
        arow.addWidget(self._appid)
        arow.addStretch(1)
        self.t_pdb.body.addLayout(arow)
        self._pdb_btn = self.t_pdb.add_button("Fetch", self._fetch_pdb, "default",
                                              "Download the ProtonDB summary (network)")
        self.t_pdb.add_button("Game hints", self._open_hints, "ghost")

        self._tiles = [self.t_profile, self.t_runtime, self.t_dlls, self.t_ft, self.t_log, self.t_pdb]
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(12)
        lay.addLayout(self._grid)
        self._reflow(False)
        lay.addStretch(1)

        LINKS.changed.connect(self._render_profile)
        settings_manager.profiles_changed.connect(self._render_profile)
        settings_manager.profile_saved.connect(lambda _n: self._render_profile())

    # ── layout ──────────────────────────────────────────────────────────────
    def _reflow(self, narrow: bool):
        if narrow == self._narrow:
            return
        self._narrow = narrow
        for t in self._tiles:
            self._grid.removeWidget(t)
        cols = 1 if narrow else 2
        for i, t in enumerate(self._tiles):
            self._grid.addWidget(t, i // cols, i % cols, Qt.AlignTop)
        for c in range(2):
            self._grid.setColumnStretch(c, 1 if c < cols else 0)

    def resizeEvent(self, ev):             # noqa: N802
        super().resizeEvent(ev)
        self._reflow(self.width() < 720)

    # ── data ────────────────────────────────────────────────────────────────
    def _win(self):
        return self.window()

    def link(self) -> dict:
        return LINKS.get(self.game.key) if self.game else {}

    def set_game(self, game):
        self.game = game
        self._info = {}
        self._title.setText(game.name)
        folders = "  ·  ".join(str(f) for f in game.folders) or (str(game.prefix) if game.prefix else "")
        self._sub.setText(f"{game.launcher}  ·  {game.key}  ·  {folders}")
        self._sub.setToolTip("\n".join([game.key] + [str(f) for f in game.folders]
                                       + ([f"prefix: {game.prefix}"] if game.prefix else [])))
        self._folder_btn.setEnabled(bool(game.folders))
        for t in self._tiles[1:]:
            t.set("…", "reading …", T["muted"])
        self._render_profile()
        self.reload()

    def reload(self):
        if self.game is None:
            return
        w = self._win()
        env = w.collect_launch_payload().get("env", {}) if hasattr(w, "collect_launch_payload") else {}
        wk = _CardWorker(self.game, env, self.link(), self)
        self._worker = wk
        self._threads = [t for t in self._threads if t.isRunning()] + [wk]
        wk.done.connect(self._on_info)
        self._reload_btn.setEnabled(False)
        wk.finished.connect(lambda: self._reload_btn.setEnabled(True))
        wk.start()

    def _on_info(self, key: str, info: dict):
        if self.game is None or key != self.game.key:
            return                         # the user moved on to another game
        self._info = info
        self._render_runtime(info.get("runtime") or {})
        self._render_dlls(info.get("dlls") or {})
        self._render_ft(info.get("ft") or {})
        self._render_log(info.get("log") or {})
        self._render_pdb(info.get("pdb") or {})

    # ── tiles ───────────────────────────────────────────────────────────────
    def _render_profile(self, *_):
        if self.game is None:
            return
        ln = self.link()
        profiles = self.sm.get_profiles()
        prof = ln.get("profile") or ""
        code, why = link_status(ln, profiles)
        color = {"ok": T["accent"], "behind": T["warn"], "never": T["warn"],
                 "missing": T["danger"]}.get(code, T["muted"])
        lines = []
        if why:
            glyph = {"ok": "✔", "behind": "⟳", "never": "○", "missing": "✖", "noprofile": "•"}.get(code, "")
            lines.append(f'<span style="color:{color}">{glyph} {_esc(why)}</span>')
        if ln.get("applied_at"):
            tgt = ln.get("target") or {}
            lines.append(f"Last written {_esc(_when(ln['applied_at']))}"
                         + (f"<br><span style='color:{T['muted']}'>{_esc(Path(tgt.get('path', '')).name)}"
                            f" ({_esc(tgt.get('kind', ''))})</span>" if tgt else ""))
        n_same = len(LINKS.games_for(prof)) - 1 if prof else 0
        if n_same > 0:
            lines.append(f"{n_same} other game(s) use this profile")
        self.t_profile.set(prof or "No profile", "<br>".join(lines), T["text"] if prof else T["muted"])

        self._prof_combo.blockSignals(True)
        self._prof_combo.clear()
        self._prof_combo.addItem("— link a profile —", None)
        for n in sorted(profiles, key=str.lower):
            self._prof_combo.addItem(n, n)
        i = self._prof_combo.findData(prof) if prof else 0
        self._prof_combo.setCurrentIndex(max(i, 0))
        self._prof_combo.blockSignals(False)

        tgt = ln.get("target") or default_target(self.game)
        can_write = tgt is not None and code != "missing"
        self._write_btn.setEnabled(can_write)
        self._write_btn.setText("Write to game" if prof else "Write editor to game")
        self._write_btn.setToolTip(
            ("Write the linked profile into the launcher's config of this game "
             "(loads it into the editor first)." if prof else
             "Write the editor's current configuration into the launcher's config of this game.")
            if can_write else "No launcher config to write to (custom folder or launcher not found).")
        self._load_btn.setEnabled(bool(prof) and prof in profiles)
        self._unlink_btn.setEnabled(bool(prof))

    def _render_runtime(self, rt: dict):
        if rt.get("error"):
            self.t_runtime.set("?", _esc(rt["error"]), T["danger"])
            return
        conf = rt.get("configured") or ""
        if conf == "Steam default" and rt.get("last_used"):
            conf = ""                      # the build that really runs it says more
        value = conf or rt.get("last_used") or rt.get("runner") or "—"
        rows = []
        if rt.get("runner"):
            rows.append(("runner", rt["runner"], ""))
        if rt.get("configured"):
            rows.append(("configured", rt["configured"], ""))
        if rt.get("last_used"):
            differs = rt.get("configured") not in ("", "Steam default", rt["last_used"])
            rows.append(("last ran with", rt["last_used"], T["warn"] if differs else ""))
        if rt.get("extra"):
            rows.append(("bottle", rt["extra"], ""))
        if self.game.prefix is not None:
            rows.append(("prefix", str(self.game.prefix), T["muted"]))
        self.t_runtime.set(value, _rows_html(rows))

    def _render_dlls(self, d: dict):
        if d.get("error"):
            self.t_dlls.set("?", _esc(d["error"]), T["danger"])
            return
        ngx = d.get("ngx") or {}
        rows = [(_KIND_TITLES[k], " / ".join(v) if v else "—", "" if v else T["faint"])
                for k, v in ngx.items()]
        for name, (state, ver) in (d.get("prefix") or {}).items():
            if self.game.prefix_manager == "Proton" and state in ("none", ""):
                txt, col = "from Proton", T["muted"]
            else:
                txt = {"none": "—", "unknown": "?", "mixed": f"mixed ({ver})"}.get(state, ver or "?")
                col = T["warn"] if state in ("mixed", "unknown") else ("" if state == "ok" else T["faint"])
            rows.append((name, txt, col))
        dlss = ngx.get("dlss") or []
        head = f"DLSS {dlss[-1]}" if dlss else ("Streamline" if ngx.get("sl_sdk_0") else "no DLSS")
        dx = next((v for n, (s, v) in (d.get("prefix") or {}).items() if n == "DXVK" and s == "ok" and v), "")
        value = head + (f"  ·  DXVK {dx}" if dx else "")
        extra = ""
        if d.get("anti_cheat"):
            extra += (f'<br><span style="color:{T["warn"]}">⚠ anti-cheat: '
                      f'{_esc(", ".join(d["anti_cheat"][:3]))} — swap DLLs with care</span>')
        if d.get("truncated"):
            extra += f'<br><span style="color:{T["muted"]}">folder too large, scan stopped early</span>'
        self.t_dlls.set(value, _rows_html(rows) + extra)

    def _render_ft(self, ft: dict):
        self._ft_open.setEnabled(bool(ft.get("path")))
        if ft.get("error"):
            self.t_ft.set("unreadable", _esc(ft["error"]), T["danger"])
            return
        if not ft.get("path"):
            self.t_ft.set("no capture", "No MangoHud log with this game's name in the MangoHud "
                          "output folder. Log a run (MangoHud tab: output_folder + toggle key) or pick a file.",
                          T["muted"])
            return
        fmt = lambda v, f="{:.1f}": "—" if v is None else f.format(v)   # noqa: E731
        rows = [("1% low", fmt(ft.get("low1")) + " FPS", ""), ("0.1% low", fmt(ft.get("low01")) + " FPS", ""),
                ("p99 frametime", fmt(ft.get("p99"), "{:.2f}") + " ms", ""),
                ("stutters", fmt(ft.get("stutters"), "{:.0f}"), T["warn"] if (ft.get("stutters") or 0) else ""),
                ("duration", fmt(ft.get("dur")) + " s", "")]
        src = (f'<br><span style="color:{T["muted"]}">{_esc(Path(ft["path"]).name)}'
               f'{" (picked)" if ft.get("pinned") else ""} · {_esc(_when(ft.get("mtime", 0)))}</span>')
        self.t_ft.set(f"{fmt(ft.get('avg'))} FPS avg", _rows_html(rows) + src)

    def _render_log(self, lg: dict):
        self._log_open.setEnabled(bool(lg.get("path")))
        if lg.get("error"):
            self.t_log.set("unreadable", _esc(lg["error"]), T["danger"])
            return
        if not lg.get("path"):
            hint = ("Run the game with logging on (top bar: Log on, or PROTON_LOG=1)."
                    if self.game.prefix is not None else "No DXVK / Proton log found for this game.")
            self.t_log.set("no log", hint, T["muted"])
            return
        hits = lg.get("hits") or []
        n_err = lg.get("n_err", 0)
        if hits:
            value, color = f"{len(hits)} known problem{'s' if len(hits) != 1 else ''}", T["danger"]
        elif n_err:
            value, color = f"{n_err} error line{'s' if n_err != 1 else ''}", T["warn"]
        else:
            value, color = "no errors", T["accent"]
        parts = []
        if hits:
            parts.append("<br>".join(f'<span style="color:{T["danger"]}">●</span> '
                                     f"{_esc(_RULE_TITLE.get(c, c))} <span style='color:{T['muted']}'>×{n}</span>"
                                     for c, n in hits[:5]))
        if lg.get("errors"):
            parts.append("<span style='font-family:monospace; font-size:11px; color:%s'>%s</span>" % (
                T["text2"], "<br>".join(_esc(e if len(e) < 150 else e[:147] + "…") for e in lg["errors"])))
        parts.append(f'<span style="color:{T["muted"]}">{_esc(Path(lg["path"]).name)} · '
                     f'{_esc(_when(lg.get("mtime", 0)))}</span>')
        self.t_log.set(value, "<br>".join(parts), color)

    def _render_pdb(self, p: dict):
        appid = p.get("appid", "")
        is_steam = bool(game_info.steam_appid(self.game))
        self._appid.setVisible(not is_steam)
        if not self._appid.hasFocus():
            self._appid.setText(appid if not is_steam else "")
        self._pdb_btn.setEnabled(bool(appid))
        pd = p.get("protondb")
        if p.get("error"):
            self.t_pdb.set("?", _esc(p["error"]), T["danger"])
        elif not appid:
            self.t_pdb.set("no appid", "Not a Steam game: enter its Steam appid to look it up.", T["muted"])
        elif not pd or not pd.get("tier"):
            self.t_pdb.set("not fetched", f"appid {appid} · press Fetch to download the ProtonDB summary.",
                           T["muted"])
        else:
            tier = str(pd.get("tier"))
            rows = [("trending", str(pd.get("trendingTier") or "—"), _TIER_COLOR.get(str(pd.get("trendingTier")), "")),
                    ("best reported", str(pd.get("bestReportedTier") or "—"),
                     _TIER_COLOR.get(str(pd.get("bestReportedTier")), "")),
                    ("reports", str(pd.get("total") or "—"), ""), ("confidence", str(pd.get("confidence") or "—"), "")]
            self.t_pdb.set(tier.capitalize(), _rows_html(rows) +
                           f'<br><span style="color:{T["muted"]}">appid {_esc(appid)} · fetched {_ago(p.get("ts", 0))}</span>',
                           _TIER_COLOR.get(tier, T["text"]))

    # ── actions ─────────────────────────────────────────────────────────────
    def _assign_from_combo(self, _i):
        self._assign(self._prof_combo.currentData())

    def _assign(self, profile: Optional[str]):
        if self.game is None:
            return
        LINKS.assign(self.game.key, profile, name=self.game.name, launcher=self.game.launcher)
        if profile:
            toast(f"Linked “{profile}” to {self.game.name} — Write to game puts it into the launcher", "ok")

    def _write(self):
        w = self._win()
        if self.game is None or not hasattr(w, "write_profile_to_games"):
            return
        ln = self.link()
        tgt = ln.get("target") or default_target(self.game)
        if tgt is None:
            return
        w.write_profile_to_games(ln.get("profile") or None, [(self.game.key, self.game.name, tgt)])

    def _load_profile(self):
        prof = self.link().get("profile")
        w = self._win()
        if prof and hasattr(w, "load_profile_for_game"):
            w.load_profile_for_game(prof)

    def _open_folder(self):
        if self.game and self.game.folders:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.game.folders[0])))

    def _open_tool(self, title: str):
        w = self._win()
        if hasattr(w, "navigate_to"):
            w.navigate_to({"kind": "tool", "key": title})
        return w

    def _pick_capture(self):
        if self.game is None:
            return
        start = str(QSettings(APP_ID, APP_ID).value("frametime/last_dir", str(Path.home())))
        p, _ = QFileDialog.getOpenFileName(self, f"Frametime capture of {self.game.name}", start,
                                           "Logs (*.csv *.log *.txt);;All files (*)")
        if p:
            LINKS.set_field(self.game.key, "frametime", p, name=self.game.name)
            self.reload()

    def _open_frametime(self):
        p = (self._info.get("ft") or {}).get("path")
        w = self._open_tool("Frametime")
        if p and hasattr(w, "_extra_tools"):
            w._extra_tools.frametime.load_file(0, p)

    def _open_log(self):
        p = (self._info.get("log") or {}).get("path")
        w = self._open_tool("Logs")
        if p and hasattr(w, "_extra_tools"):
            w._extra_tools.logview.open_path(Path(p))

    def _open_hints(self):
        appid = (self._info.get("pdb") or {}).get("appid", "")
        w = self._open_tool("Game hints")
        if appid and hasattr(w, "_extra_tools"):
            hw = w._extra_tools.hints
            hw._appid.setText(appid)
            hw._fetch(False)

    def _save_appid(self):
        if self.game is None or game_info.steam_appid(self.game):
            return
        v = self._appid.text().strip()
        if v and not v.isdigit():
            toast("A Steam appid is a number", "warn")
            return
        if v != (self.link().get("appid") or ""):
            LINKS.set_field(self.game.key, "appid", v, name=self.game.name)
            self.reload()

    def _fetch_pdb(self):
        appid = (self._info.get("pdb") or {}).get("appid") or self._appid.text().strip()
        if not appid.isdigit():
            return
        self._pdb_btn.setEnabled(False)
        self.t_pdb.set("fetching …", f"appid {appid}", T["muted"])
        th = _HintWorker(appid, True, self)
        self._threads = [t for t in self._threads if t.isRunning()] + [th]
        th.done.connect(lambda res: self._on_pdb(appid, res))
        TASKS.track(th, f"ProtonDB: {appid}")
        th.start()

    def _on_pdb(self, appid: str, res: dict):
        self._pdb_btn.setEnabled(True)
        cur = (self._info.get("pdb") or {}).get("appid") or self._appid.text().strip()
        if cur != appid:
            return
        p = {"appid": appid, "protondb": res.get("protondb"), "ts": res.get("ts", time.time())}
        if not res.get("protondb"):
            p["error"] = "; ".join(e for e in res.get("errors", []) if e.startswith("ProtonDB")) or "no data"
        self._info["pdb"] = p
        self._render_pdb(p)

    def shutdown(self):
        for t in self._threads:
            t.wait(3000)


# ============================================================================
# "This profile is used by N games" — update dialog
# ============================================================================

class LinkedGamesDialog(QDialog):
    """Games linked to one profile; tick the ones to write the saved profile to."""

    def __init__(self, profile: str, profiles: dict, parent=None, only_outdated: bool = True):
        super().__init__(parent)
        self.chosen: List[tuple] = []          # [(game key, name, target)]
        self.setWindowTitle(f"Games using “{profile}”")
        self.resize(720, 420)
        lay = QVBoxLayout(self)
        keys = LINKS.games_for(profile)
        links = LINKS.all()
        fp = profile_fp(profiles.get(profile))
        behind = [k for k in keys if links[k].get("fp") != fp]
        intro = QLabel(
            f"<b>{len(keys)}</b> game(s) are linked to <b>{_esc(profile)}</b>; "
            f"<b>{len(behind)}</b> of them still have an older version of it." if keys else
            f"No game is linked to <b>{_esc(profile)}</b> yet — write it to a game from the Games tab "
            "or a sync tab.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{T['text2']};")
        lay.addWidget(intro)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Game", "Launcher", "Last written", "Status"])
        self._tree.setRootIsDecorated(False)
        self._tree.setStyleSheet(_pack2_table_ss())
        self._tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self._tree, 1)
        for k in keys:
            ln = links[k]
            code, why = link_status(ln, profiles)
            tgt = ln.get("target")
            it = QTreeWidgetItem([str(ln.get("name") or k), str(ln.get("launcher") or ""),
                                  _when(ln.get("applied_at", 0)) or "never", why])
            it.setData(0, Qt.UserRole, (k, str(ln.get("name") or k), tgt))
            it.setToolTip(0, k)
            if tgt:
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(0, Qt.Checked if (code != "ok" or not only_outdated) else Qt.Unchecked)
            else:
                it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
                it.setText(3, "not written yet — use Write to game on its card once")
            self._tree.addTopLevelItem(it)
        btns = QHBoxLayout()
        note = QLabel("The saved profile is loaded into the editor and written with a backup of each file. "
                      "Steam must be closed for Steam games.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{T['muted']}; font-size:11px;")
        btns.addWidget(note, 1)
        self._ok = QPushButton("Update selected")
        self._ok.setStyleSheet(btn_ss("primary"))
        self._ok.clicked.connect(self._accept)
        cancel = QPushButton("Close")
        cancel.setStyleSheet(btn_ss("ghost"))
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        btns.addWidget(self._ok)
        lay.addLayout(btns)
        self._tree.itemChanged.connect(lambda *_: self._sync())
        self._sync()

    def _checked(self) -> List[tuple]:
        out = []
        for i in range(self._tree.topLevelItemCount()):
            it = self._tree.topLevelItem(i)
            if it.flags() & Qt.ItemIsUserCheckable and it.checkState(0) == Qt.Checked:
                out.append(it.data(0, Qt.UserRole))
        return out

    def _sync(self):
        n = len(self._checked())
        self._ok.setEnabled(n > 0)
        self._ok.setText(f"Update {n} game{'s' if n != 1 else ''}" if n else "Update selected")

    def _accept(self):
        self.chosen = self._checked()
        self.accept()

