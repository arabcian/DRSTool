"""Steam / Heroic / Bottles backends, launcher sync page and Launch line page."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QVBoxLayout, QWidget,
)

from .base import _atomic_write, _backup_file, _HAVE_YAML, _proc_running, _YAML_LOADER, yaml
from .gamescope import GamescopeFlagsWidget
from .launch_line import build_launch_line, gamescope_arg_string
from .lutris_sync import LutrisSyncWidget
from .steam_utils import (
    _vdf_unesc, proton_tool_dirs, STEAM_COMPAT_PATH, steam_compat_tools, steam_config_vdf,
    UMU_STORES, vdf_get_app_value, vdf_get_launch_options, vdf_remove_app_block,
    vdf_set_app_value, vdf_set_launch_options,
)
from .theme import T
from .toasts import offer_undo
from .tool_ui import _tool_btn
from .widgets import _COMBO_QSS, _LAUNCHER_QSS, _LINEEDIT_QSS, _MONO_BOX_QSS


# ── backends ────────────────────────────────────────────────────────────────

@dataclass
class LEntry:
    key: str
    title: str
    path: Optional[Path] = None
    extra: dict = field(default_factory=dict)


class LauncherBackend:
    name = ""
    blurb = ""

    def unavailable(self) -> str:
        return ""

    def accounts(self) -> List[tuple]:
        return []                      # [(label, id)]

    def scan(self, account=None) -> List[LEntry]:
        return []

    def current(self, e: LEntry) -> str:
        return ""

    def proposed(self, e: LEntry, payload: dict, opts: dict) -> str:
        return ""

    def busy(self) -> str:
        return ""

    def extra_preview(self, e: LEntry, opts: dict) -> str:
        return ""

    def apply(self, e: LEntry, payload: dict, opts: dict) -> str:
        raise NotImplementedError


class SteamBackend(LauncherBackend):
    name = "Steam"
    blurb = ("Writes the launch options of an installed game into Steam's localconfig.vdf "
             "(a timestamped .bak is kept next to it). Steam must be closed — it rewrites that "
             "file on exit and would discard the change.")

    @staticmethod
    def roots() -> List[Path]:
        h = Path.home()
        cands = [h / ".local/share/Steam", h / ".steam/steam", h / ".steam/root",
                 h / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
                 h / ".var/app/com.valvesoftware.Steam/data/Steam"]
        seen, out = set(), []
        for c in cands:
            try:
                r = c.resolve()
            except OSError:
                continue
            if (r / "userdata").is_dir() and r not in seen:
                seen.add(r)
                out.append(r)
        return out

    def unavailable(self):
        return "" if self.roots() else "No Steam installation found (~/.local/share/Steam, flatpak)."

    def accounts(self):
        res = []
        for root in self.roots():
            for ud in sorted((root / "userdata").iterdir()):
                cfg = ud / "config" / "localconfig.vdf"
                if ud.name.isdigit() and ud.name != "0" and cfg.is_file():
                    name = ""
                    try:
                        m = re.search(r'"PersonaName"\s+"([^"]*)"',
                                      cfg.read_text(errors="replace")[:200000])
                        name = m.group(1) if m else ""
                    except OSError:
                        pass
                    res.append((f"{name or 'account'}  ({ud.name})", str(cfg)))
        return res

    @staticmethod
    def _libraries(root: Path) -> List[Path]:
        libs = [root / "steamapps"]
        vf = root / "steamapps" / "libraryfolders.vdf"
        try:
            for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', vf.read_text(errors="replace")):
                p = Path(_vdf_unesc(m.group(1))) / "steamapps"
                if p not in libs:
                    libs.append(p)
        except OSError:
            pass
        return [l for l in libs if l.is_dir()]

    def scan(self, account=None):
        cfg = Path(account) if account else None
        if cfg is None:
            acc = self.accounts()
            cfg = Path(acc[0][1]) if acc else None
        if cfg is None:
            return []
        root = cfg.parents[3]                      # <root>/userdata/<id>/config/localconfig.vdf
        out, seen = [], set()
        for lib in self._libraries(root):
            for acf in sorted(lib.glob("appmanifest_*.acf")):
                try:
                    t = acf.read_text(errors="replace")
                except OSError:
                    continue
                a = re.search(r'"appid"\s+"(\d+)"', t)
                n = re.search(r'"name"\s+"((?:[^"\\]|\\.)*)"', t)
                if not a or a.group(1) in seen:
                    continue
                name = _vdf_unesc(n.group(1)) if n else a.group(1)
                if re.match(r"(Proton\b|Steam Linux Runtime|Steamworks Common)", name):
                    continue
                seen.add(a.group(1))
                out.append(LEntry(a.group(1), f"{name}  [{a.group(1)}]", cfg))
        out.sort(key=lambda e: e.title.lower())
        return out

    def current(self, e):
        try:
            return vdf_get_launch_options(e.path.read_text(errors="replace"), e.key)
        except OSError:
            return ""

    def proposed(self, e, payload, opts):
        return build_launch_line(payload, "%command%", opts.get("use_env", True))

    def busy(self):
        return "Steam is running — close it first (it rewrites localconfig.vdf on exit)." \
            if _proc_running("steam", "steamwebhelper") else ""

    def apply(self, e, payload, opts):
        text = e.path.read_text(errors="replace")
        new = vdf_set_launch_options(text, e.key, self.proposed(e, payload, opts))
        # sanity: re-parse and read back
        if vdf_get_launch_options(new, e.key) != self.proposed(e, payload, opts):
            raise ValueError("Self-check failed: edited VDF does not read back identically — aborted.")
        ct = opts.get("compat_tool") or ""
        cfg_new = None
        if ct:                                     # prepare first so a failure aborts before any write
            cfg = steam_config_vdf(e.path)
            ctext = cfg.read_text(errors="replace")
            if ct == "__default__":
                cfg_new = vdf_remove_app_block(ctext, STEAM_COMPAT_PATH, e.key)
                ok = vdf_get_app_value(cfg_new, STEAM_COMPAT_PATH, e.key, "name") == ""
            else:
                cfg_new = vdf_set_app_value(ctext, STEAM_COMPAT_PATH, e.key, "name", ct)
                cfg_new = vdf_set_app_value(cfg_new, STEAM_COMPAT_PATH, e.key, "config",
                                            vdf_get_app_value(cfg_new, STEAM_COMPAT_PATH, e.key, "config"))
                if not vdf_get_app_value(cfg_new, STEAM_COMPAT_PATH, e.key, "priority"):
                    cfg_new = vdf_set_app_value(cfg_new, STEAM_COMPAT_PATH, e.key, "priority", "250")
                ok = vdf_get_app_value(cfg_new, STEAM_COMPAT_PATH, e.key, "name") == ct
            if not ok:
                raise ValueError("Self-check failed: config.vdf does not read back as intended — aborted.")
        bak = _backup_file(e.path)
        _atomic_write(e.path, new)
        msg = f"Steam launch options written for {e.title}. Backup: {bak.name}"
        if cfg_new is not None:
            cbak = _backup_file(cfg)
            _atomic_write(cfg, cfg_new)
            msg += (" Proton override removed." if ct == "__default__" else f" Proton set to {ct}.") + \
                f" Backup: {cbak.name}"
        return msg

    def extra_preview(self, e, opts):
        try:
            cur = vdf_get_app_value(steam_config_vdf(e.path).read_text(errors="replace"),
                                    STEAM_COMPAT_PATH, e.key, "name")
        except (OSError, ValueError):
            cur = ""
        ct = opts.get("compat_tool") or ""
        new = ("remove the per-game override" if ct == "__default__" else ct) if ct else "unchanged"
        return f"── Proton (config.vdf) ──\ncurrent: {cur or '(Steam default)'}\nnew:     {new}"


class HeroicBackend(LauncherBackend):
    name = "Heroic"
    blurb = ("Writes environment variables and gamescope/MangoHud wrappers into Heroic's "
             "GamesConfig/<game>.json (or the global defaults). Only variables DRSTool owns are "
             "replaced; your other entries stay. Close Heroic first, otherwise it may overwrite.")

    @staticmethod
    def roots() -> List[Path]:
        h = Path.home()
        return [p for p in (h / ".config/heroic",
                            h / ".var/app/com.heroicgameslauncher.hgl/config/heroic") if p.is_dir()]

    def unavailable(self):
        return "" if self.roots() else "No Heroic config found (~/.config/heroic, flatpak)."

    @staticmethod
    def _titles(root: Path) -> Dict[str, str]:
        titles: Dict[str, str] = {}
        files = [root / "store_cache" / n for n in
                 ("legendary_library.json", "gog_library.json", "nile_library.json",
                  "zoom_library.json")] + [root / "sideload_apps" / "library.json"]

        def walk(o):
            if isinstance(o, dict):
                if "app_name" in o and "title" in o:
                    titles[str(o["app_name"])] = str(o["title"])
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        for f in files:
            try:
                walk(json.loads(f.read_text(errors="replace")))
            except Exception:
                pass
        return titles

    def scan(self, account=None):
        out = []
        for root in self.roots():
            cfgf = root / "config.json"
            out.append(LEntry("__global__", "★ All games (global defaults)", cfgf, {"global": True}))
            titles = self._titles(root)
            gdir = root / "GamesConfig"
            if gdir.is_dir():
                for f in sorted(gdir.glob("*.json")):
                    out.append(LEntry(f.stem, f"{titles.get(f.stem, f.stem)}  [{f.stem}]", f))
            break                                   # first existing root only
        return out

    @staticmethod
    def _node(data: dict, e: LEntry) -> dict:
        if e.extra.get("global"):
            return data.setdefault("defaultSettings", {})
        return data.setdefault(e.key, {})

    def _load(self, e):
        try:
            d = json.loads(e.path.read_text(errors="replace"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def current(self, e):
        d = self._load(e)
        node = d.get("defaultSettings", {}) if e.extra.get("global") else d.get(e.key, {})
        envs = [f'{x.get("key")}={x.get("value")}' for x in node.get("enviromentOptions", [])
                if isinstance(x, dict)]
        wr = [f'{x.get("exe")} {x.get("args", "")}'.strip() for x in node.get("wrapperOptions", [])
              if isinstance(x, dict)]
        return "env:\n  " + "\n  ".join(envs or ["(none)"]) + "\nwrappers:\n  " + "\n  ".join(wr or ["(none)"])

    @staticmethod
    def _wrappers(payload):
        w = []
        if payload.get("gs_on"):
            args = gamescope_arg_string(payload.get("gs_flags") or {})
            w.append({"exe": "gamescope", "args": (args + " --").strip()})
            if payload.get("mangohud"):
                w.append({"exe": "mangohud", "args": "--dlsym"})
        return w

    def proposed(self, e, payload, opts):
        envs = [f"{k}={v}" for k, v in (payload.get("env") or {}).items()]
        wr = [f'{w["exe"]} {w["args"]}' for w in self._wrappers(payload)]
        return "env:\n  " + "\n  ".join(envs or ["(none)"]) + "\nwrappers:\n  " + "\n  ".join(wr or ["(none)"])

    def busy(self):
        return "Heroic is running — it may overwrite this file when it exits." \
            if _proc_running("heroic") else ""

    def apply(self, e, payload, opts):
        # Not _load(): it maps an unreadable file to {}, which would then be written back
        # with only DRSTool's keys in it.
        data = json.loads(e.path.read_text(errors="replace")) if e.path.is_file() else {}
        if not isinstance(data, dict):
            raise ValueError(f"{e.path.name} is not a JSON object — nothing written.")
        node = self._node(data, e)
        owned = LutrisSyncWidget._managed_env_keys()
        keep = [x for x in node.get("enviromentOptions", [])
                if isinstance(x, dict) and x.get("key") not in owned]
        keep += [{"key": k, "value": str(v)} for k, v in (payload.get("env") or {}).items()]
        node["enviromentOptions"] = keep
        wkeep = [w for w in node.get("wrapperOptions", [])
                 if isinstance(w, dict) and os.path.basename(str(w.get("exe", ""))) not in ("gamescope", "mangohud")]
        new_w = self._wrappers(payload)
        node["wrapperOptions"] = new_w + wkeep
        if any(w["exe"] == "mangohud" for w in new_w):
            node["showMangohud"] = False           # avoid wrapping gamescope from the outside
        bak = _backup_file(e.path) if e.path.is_file() else None
        e.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(e.path, json.dumps(data, indent=2, ensure_ascii=False))
        return f"Heroic config written for {e.title}." + (f" Backup: {bak.name}" if bak else "")


class BottlesBackend(LauncherBackend):
    name = "Bottles"
    blurb = ("Writes Environment_Variables and the gamescope/MangoHud toggles into the bottle's "
             "bottle.yml. Bottles settings are per bottle (not per game). Gamescope flags without a "
             "Bottles equivalent are listed in the preview and skipped.")

    # gamescope flag -> (Parameters key, type)
    _MAP = {"--nested-width": ("gamescope_game_width", int), "--nested-height": ("gamescope_game_height", int),
            "--output-width": ("gamescope_window_width", int), "--output-height": ("gamescope_window_height", int),
            "--framerate-limit": ("gamescope_fps", int), "--nested-unfocused-refresh": ("gamescope_fps_no_focus", int),
            "--borderless": ("gamescope_borderless", bool), "--fullscreen": ("gamescope_fullscreen", bool)}

    @staticmethod
    def roots() -> List[Path]:
        h = Path.home()
        return [p for p in (h / ".local/share/bottles/bottles",
                            h / ".var/app/com.usebottles.bottles/data/bottles/bottles") if p.is_dir()]

    def unavailable(self):
        if not _HAVE_YAML:
            return "PyYAML is required to edit bottle.yml."
        return "" if self.roots() else "No Bottles directory found (~/.local/share/bottles, flatpak)."

    def scan(self, account=None):
        out = []
        for root in self.roots():
            for d in sorted(root.iterdir()):
                f = d / "bottle.yml"
                if f.is_file():
                    name = d.name
                    try:
                        name = (yaml.load(f.read_text(), Loader=_YAML_LOADER) or {}).get("Name", d.name)
                    except Exception:
                        pass
                    out.append(LEntry(d.name, f"{name}  [{d.name}]", f))
        return out

    def _load(self, e):
        try:
            d = yaml.load(e.path.read_text(), Loader=_YAML_LOADER) or {}
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _plan(self, payload):
        params: Dict[str, object] = {}
        skipped = []
        gs_on = bool(payload.get("gs_on"))
        params["gamescope"] = gs_on
        params["mangohud"] = bool(payload.get("mangohud"))
        if gs_on:
            flags = payload.get("gs_flags") or {}
            for flag, val in flags.items():
                if flag == GamescopeFlagsWidget.MANGOHUD_KEY:
                    continue
                m = self._MAP.get(flag)
                if not m:
                    skipped.append(flag if val == "" else f"{flag} {val}")
                    continue
                key, typ = m
                try:
                    params[key] = True if typ is bool else int(val)
                except ValueError:
                    skipped.append(f"{flag} {val}")
        return params, skipped

    def current(self, e):
        d = self._load(e)
        envs = d.get("Environment_Variables") or {}
        p = d.get("Parameters") or {}
        gp = {k: v for k, v in p.items() if k.startswith("gamescope") or k == "mangohud"}
        return ("Environment_Variables:\n  " + "\n  ".join(f"{k}={v}" for k, v in envs.items()) +
                "\nParameters:\n  " + "\n  ".join(f"{k}: {v}" for k, v in gp.items()))

    def proposed(self, e, payload, opts):
        params, skipped = self._plan(payload)
        txt = ("Environment_Variables (DRSTool-owned part):\n  " +
               "\n  ".join(f"{k}={v}" for k, v in (payload.get("env") or {}).items()) +
               "\nParameters:\n  " + "\n  ".join(f"{k}: {v}" for k, v in params.items()))
        if skipped:
            txt += "\n\nNo Bottles equivalent (skipped): " + ", ".join(skipped)
        return txt

    def apply(self, e, payload, opts):
        # Not _load(): it maps an unreadable file to {}, which would wipe the bottle's config.
        d = yaml.load(e.path.read_text(), Loader=_YAML_LOADER) or {}
        if not isinstance(d, dict):
            raise ValueError(f"{e.path.name} is not a YAML mapping — nothing written.")
        envs = dict(d.get("Environment_Variables") or {})
        owned = LutrisSyncWidget._managed_env_keys()
        for k in list(envs):
            if k in owned:
                del envs[k]
        envs.update({k: str(v) for k, v in (payload.get("env") or {}).items()})
        d["Environment_Variables"] = envs
        params = dict(d.get("Parameters") or {})
        for k in [k for k in params if k.startswith("gamescope") and k not in ("gamescope",)]:
            if k in {v[0] for v in self._MAP.values()}:
                del params[k]
        new_p, _skipped = self._plan(payload)
        params.update(new_p)
        d["Parameters"] = params
        bak = _backup_file(e.path)
        _atomic_write(e.path, yaml.safe_dump(d, sort_keys=False, allow_unicode=True,
                                             default_flow_style=False))
        return f"bottle.yml updated for {e.title}. Backup: {bak.name}"


class LauncherSyncWidget(QWidget):
    """One tab per launcher (Steam / Heroic / Bottles): pick target -> preview -> apply."""
    # After a successful write: {"kind", "key", "path", "title"} (MainWindow links it to the profile)
    applied = Signal(dict)

    def __init__(self, backend: LauncherBackend, parent=None):
        super().__init__(parent)
        self.backend = backend
        self._entries: List[LEntry] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)

        info = QLabel(backend.blurb)
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(info)

        row = QHBoxLayout()
        self._acct = QComboBox()
        self._acct.setVisible(False)
        self._acct.currentIndexChanged.connect(lambda _i: self.rescan(keep_account=True))
        row.addWidget(self._acct)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter targets…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        row.addWidget(self._filter, 1)
        b = _tool_btn("Rescan")
        b.clicked.connect(lambda: self.rescan())
        row.addWidget(b)
        lay.addLayout(row)

        self._list = QListWidget()
        self._list.setStyleSheet(_LAUNCHER_QSS)
        self._list.setMinimumHeight(150)
        self._list.currentItemChanged.connect(lambda *_: self.refresh_preview())
        lay.addWidget(self._list, 1)

        self._use_env = QCheckBox("Prefix variables with  env  (env A=1 gamescope … -- %command%)")
        self._use_env.setChecked(True)
        self._use_env.setVisible(backend.name == "Steam")
        self._use_env.toggled.connect(lambda _c: self.refresh_preview())
        self._use_env.setStyleSheet(f"color:{T['text2']};")
        lay.addWidget(self._use_env)

        self._compat_root = None
        self._compat = QComboBox()
        self._compat.setStyleSheet(_COMBO_QSS)
        self._compat.setVisible(backend.name == "Steam")
        self._compat.setToolTip("Writes the per-game compatibility tool (CompatToolMapping in "
                                "config.vdf) together with the launch options.")
        self._compat.currentIndexChanged.connect(lambda _i: self.refresh_preview())
        lay.addWidget(self._compat)

        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setStyleSheet(_MONO_BOX_QSS)
        self._preview.setMinimumHeight(150)
        lay.addWidget(self._preview, 1)

        btns = QHBoxLayout()
        self._apply_btn = _tool_btn(f"Apply to {backend.name}", bg="#26330f", border="#4a7300",
                                    fg="#cfe9a0", bold=True)
        self._apply_btn.clicked.connect(self._apply)
        btns.addWidget(self._apply_btn)
        cp = _tool_btn("Copy new value")
        cp.clicked.connect(self._copy)
        btns.addWidget(cp)
        btns.addStretch(1)
        lay.addLayout(btns)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(self._status)

        un = backend.unavailable()
        if un:
            self._status.setText("⚠ " + un)
            self._apply_btn.setEnabled(False)
        self._first_scan = True

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        if self._first_scan:
            self._first_scan = False
            QTimer.singleShot(0, self.rescan)

    # -- data -----------------------------------------------------------------
    def _payload(self) -> dict:
        w = self.window()
        return w.collect_launch_payload() if hasattr(w, "collect_launch_payload") else {}

    def rescan(self, keep_account: bool = False):
        be = self.backend
        if be.unavailable():
            return
        if not keep_account:
            self._acct.blockSignals(True)
            self._acct.clear()
            for label, ident in be.accounts():
                self._acct.addItem(label, ident)
            self._acct.setVisible(self._acct.count() > 1)
            self._acct.blockSignals(False)
        acct = self._acct.currentData() if self._acct.count() else None
        try:
            self._entries = be.scan(acct)
        except Exception as ex:
            self._entries = []
            self._status.setText(f"⚠ scan failed: {ex}")
        self._list.clear()
        for e in self._entries:
            it = QListWidgetItem(e.title)
            it.setData(Qt.UserRole, e)
            self._list.addItem(it)
        self._apply_filter(self._filter.text())
        if self._entries:
            self._status.setText(f"{len(self._entries)} target(s) found.")
        elif not self._status.text().startswith("⚠"):
            self._status.setText("No targets found.")
        self.refresh_preview()

    def _apply_filter(self, text):
        t = text.lower().strip()
        for i in range(self._list.count()):
            it = self._list.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def _sel(self) -> Optional[LEntry]:
        it = self._list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _opts(self) -> dict:
        return {"use_env": self._use_env.isChecked(),
                "compat_tool": (self._compat.currentData() or "") if self.backend.name == "Steam" else ""}

    def _fill_compat(self, e):
        if self.backend.name != "Steam":
            return
        root = e.path.parents[3]
        if root == self._compat_root:
            return
        self._compat_root = root
        self._compat.blockSignals(True)
        self._compat.clear()
        self._compat.addItem("Proton: leave unchanged", "")
        self._compat.addItem("Proton: Steam default (remove per-game override)", "__default__")
        for name, label in steam_compat_tools(root):
            self._compat.addItem("Proton: " + label, name)
        self._compat.blockSignals(False)

    def refresh_preview(self):
        e = self._sel()
        if e is None:
            self._preview.setPlainText("Select a target above to preview what will be written.")
            return
        p = self._payload()
        try:
            self._fill_compat(e)
            cur = self.backend.current(e)
            new = self.backend.proposed(e, p, self._opts())
            more = self.backend.extra_preview(e, self._opts())
        except Exception as ex:
            self._preview.setPlainText(f"⚠ {ex}")
            return
        self._preview.setPlainText(f"── current ──\n{cur or '(empty)'}\n\n── new ──\n{new or '(empty)'}"
                                   + (f"\n\n{more}" if more else ""))

    def _copy(self):
        e = self._sel()
        p = self._payload()
        txt = (self.backend.proposed(e, p, self._opts()) if e else
               build_launch_line(p, "%command%", True))
        QApplication.clipboard().setText(txt)
        self._status.setText("Copied.")

    def _apply(self):
        e = self._sel()
        if e is None:
            self._status.setText("Pick a target first.")
            return
        busy = self.backend.busy()
        if busy:
            QMessageBox.warning(self, self.backend.name, busy)
            return
        if QMessageBox.question(
                self, f"Write to {self.backend.name}",
                f"Write the current DRSTool configuration to:\n\n{e.title}\n\n"
                "A timestamped backup of the file is kept.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            msg = self.backend.apply(e, self._payload(), self._opts())
            self._status.setText("✔ " + msg)
            offer_undo(msg)
            self.applied.emit(self._target(e))
        except Exception as ex:
            self._status.setText(f"⚠ failed: {ex}")
            QMessageBox.critical(self, self.backend.name, str(ex))
        self.refresh_preview()

    def _target(self, e: LEntry) -> dict:
        return {"kind": self.backend.name.lower(), "key": e.key, "path": str(e.path), "title": e.title}

    def apply_target(self, target: dict) -> tuple:
        """Write the current configuration to a target recorded by `applied` earlier, without
        asking (the caller confirmed). The Proton override is left as it is. -> (ok, message)"""
        busy = self.backend.busy()
        if busy:
            return False, busy
        path = Path(target.get("path") or "")
        if not path.is_file() and self.backend.name != "Heroic":
            return False, f"{path} no longer exists"
        e = LEntry(str(target.get("key", "")), str(target.get("title", "")), path)
        try:
            msg = self.backend.apply(e, self._payload(), {"use_env": True, "compat_tool": ""})
        except Exception as ex:                    # noqa: BLE001 - reported per game
            return False, str(ex)
        self.applied.emit(self._target(e))
        if self.isVisible():
            self.refresh_preview()
        return True, msg


class LaunchLineWidget(QWidget):
    """One-click `env … gamescope … -- %command%` (or any other command)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        t = QLabel("Runnable launch line")
        t.setStyleSheet(f"color:{T['text']}; font-size:15px; font-weight:700;")
        lay.addWidget(t)
        d = QLabel("Built from everything currently set (arch, DRS settings, environment, "
                   "gamescope, MangoHud). Use %command% for Steam, or type your own command to "
                   "get a line you can paste into a terminal or save as a launcher script.")
        d.setWordWrap(True)
        d.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(d)

        form = QHBoxLayout()
        _cl = QLabel("Command:")
        _cl.setStyleSheet(f"color:{T['text2']};")
        form.addWidget(_cl)
        self._cmd = QLineEdit("%command%")
        self._cmd.setStyleSheet(f"QLineEdit{{background:{T['surface']};border:1px solid {T['border']};"
                                f"color:{T['text']};padding:3px 8px;border-radius:4px;min-height:26px;"
                                f"font-family:{T['mono']};}}")
        self._cmd.textChanged.connect(lambda _t: self.refresh())
        form.addWidget(self._cmd, 1)
        self._use_env = QCheckBox("use  env  prefix")
        self._use_env.setChecked(True)
        self._use_env.setStyleSheet(f"color:{T['text2']};")
        self._use_env.toggled.connect(lambda _c: self.refresh())
        form.addWidget(self._use_env)
        lay.addLayout(form)

        # umu-launcher: run the .exe through umu-run with PROTONPATH / GAMEID / STORE
        urow = QHBoxLayout()
        self._umu = QCheckBox("umu-run")
        self._umu.setToolTip("Wrap the command with umu-launcher (command = path to the .exe). "
                             "Adds PROTONPATH, GAMEID, STORE and WINEPREFIX.")
        self._umu.setStyleSheet(f"color:{T['text2']};")
        self._umu.toggled.connect(lambda _c: self.refresh())
        urow.addWidget(self._umu)
        self._umu_proton = QComboBox()
        self._umu_proton.setEditable(True)
        self._umu_proton.setStyleSheet(_COMBO_QSS)
        self._umu_proton.setToolTip("PROTONPATH: GE-Proton / UMU-Latest (umu downloads it) or an installed Proton directory")
        self._umu_proton.addItem("GE-Proton")
        self._umu_proton.addItem("UMU-Latest")
        for label, d in proton_tool_dirs():
            self._umu_proton.addItem(label, d)
        self._umu_proton.currentTextChanged.connect(lambda _t: self.refresh())
        urow.addWidget(self._umu_proton, 2)
        self._umu_gid = QLineEdit("umu-0")
        self._umu_gid.setToolTip("GAMEID: umu-<steam appid> picks umu's protonfixes for that game; umu-0 = none")
        self._umu_gid.setStyleSheet(_LINEEDIT_QSS)
        self._umu_gid.setMaximumWidth(120)
        self._umu_gid.textChanged.connect(lambda _t: self.refresh())
        urow.addWidget(self._umu_gid)
        self._umu_store = QComboBox()
        self._umu_store.setStyleSheet(_COMBO_QSS)
        self._umu_store.setToolTip("STORE")
        for st in UMU_STORES:
            self._umu_store.addItem(st or "(no STORE)", st)
        self._umu_store.currentIndexChanged.connect(lambda _i: self.refresh())
        urow.addWidget(self._umu_store)
        self._umu_pfx = QLineEdit()
        self._umu_pfx.setPlaceholderText("WINEPREFIX (optional)")
        self._umu_pfx.setStyleSheet(_LINEEDIT_QSS)
        self._umu_pfx.textChanged.connect(lambda _t: self.refresh())
        urow.addWidget(self._umu_pfx, 2)
        lay.addLayout(urow)

        self._out = QPlainTextEdit()
        self._out.setReadOnly(True)
        self._out.setStyleSheet(_MONO_BOX_QSS)
        self._out.setMinimumHeight(140)
        lay.addWidget(self._out, 1)

        row = QHBoxLayout()
        b1 = _tool_btn("Copy line", bg="#26330f", border="#4a7300", fg="#cfe9a0", bold=True)
        b1.clicked.connect(self._copy)
        row.addWidget(b1)
        b2 = _tool_btn("Copy for Steam (%command%)")
        b2.clicked.connect(self._copy_steam)
        row.addWidget(b2)
        b3 = _tool_btn("Save as launcher script…")
        b3.clicked.connect(self._save_script)
        row.addWidget(b3)
        row.addStretch(1)
        lay.addLayout(row)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(self._status)
        lay.addStretch(0)

    def _payload(self):
        w = self.window()
        return w.collect_launch_payload() if hasattr(w, "collect_launch_payload") else {}

    def build_line(self, cmd: Optional[str] = None, steam: bool = False, force_env: bool = False) -> str:
        """The launch line; with umu-run enabled the command is wrapped as `umu-run <exe>`."""
        p = dict(self._payload())
        cmd = (self._cmd.text().strip() if cmd is None else cmd.strip()) or "%command%"
        if self._umu.isChecked() and not steam:
            env = dict(p.get("env") or {})
            pp = self._umu_proton.currentData() or self._umu_proton.currentText().strip()
            if pp:
                env["PROTONPATH"] = pp
            if self._umu_gid.text().strip():
                env["GAMEID"] = self._umu_gid.text().strip()
            if self._umu_store.currentData():
                env["STORE"] = self._umu_store.currentData()
            if self._umu_pfx.text().strip():
                env["WINEPREFIX"] = os.path.expanduser(self._umu_pfx.text().strip())
            p["env"] = env
            cmd = "umu-run " + ("<game.exe>" if cmd == "%command%" else cmd)
        return build_launch_line(p, cmd, self._use_env.isChecked() or force_env)

    def refresh(self):
        self._out.setPlainText(self.build_line())

    def showEvent(self, ev):               # noqa: N802
        super().showEvent(ev)
        self.refresh()

    def _copy(self):
        QApplication.clipboard().setText(self._out.toPlainText())
        self._status.setText("Copied.")

    def _copy_steam(self):
        QApplication.clipboard().setText(self.build_line("%command%", steam=True))
        self._status.setText("Copied Steam launch options.")

    def _save_script(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save launcher script",
                                              str(Path.home() / "drstool-launch.sh"), "Shell script (*.sh)")
        if not path:
            return
        cmd = self._cmd.text().strip()
        inner = '"$@"' if cmd in ("", "%command%") else cmd
        # `exec A=1 cmd` makes sh exec a program named "A=1": a script always needs `env`.
        line = self.build_line(inner, force_env=True)
        try:
            Path(path).write_text(f"#!/bin/sh\n# generated by DRSTool\nexec {line}\n")
            os.chmod(path, 0o755)
            self._status.setText(f"Saved {path}")
        except OSError as ex:
            self._status.setText(f"⚠ {ex}")
