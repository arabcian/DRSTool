"""Offscreen GUI tests: the whole window starts, every tool page and sync tab is built,
and the new features work end to end against a fake home folder (no network).

Skipped automatically when PySide6 is not installed.

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_gui -v
"""

import contextlib
import importlib
import importlib.util
import io
import pkgutil
import os
import struct
import sys
import tarfile
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HAVE_QT = importlib.util.find_spec("PySide6") is not None


class GuiNamespace:
    """The old tests used one big DRSTool module; the GUI is now the drstool_gui package.
    Attribute reads look the name up in every submodule of the package."""

    def __init__(self):
        import drstool_gui
        names = sorted(i.name for i in pkgutil.iter_modules(drstool_gui.__path__))
        self.modules = [importlib.import_module(f"drstool_gui.{n}") for n in names]

    def __getattr__(self, name):
        for mod in self.modules:
            if name in vars(mod):
                return vars(mod)[name]
        raise AttributeError(name)


@contextlib.contextmanager
def patch_name(ns, name, **kw):
    """mock.patch a function in every module that imported it (each module has its own binding)."""
    with contextlib.ExitStack() as stack:
        hit = [mod for mod in ns.modules if name in vars(mod)]
        assert hit, f"{name} not found in drstool_gui"
        for mod in hit:
            stack.enter_context(mock.patch.object(mod, name, **kw))
        yield

_TMP = tempfile.TemporaryDirectory(prefix="drstool-gui-")
HOME = Path(_TMP.name)


def fake_dll(path: Path, ver=(310, 1, 0, 0), extra=b"") -> Path:
    ms, ls = (ver[0] << 16) | ver[1], (ver[2] << 16) | ver[3]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\0" * 62 + extra + b"\xbd\x04\xef\xfe"
                     + struct.pack("<III", 0x00010000, ms, ls) + b"\0" * 64)
    return path


def dxvk_dll(path: Path, ver: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\0" * 100 + b"DXVK\0" + b"\0" * 20 + f"v{ver}".encode() + b"\0" + b"\0" * 64)
    return path


def build_fake_home():
    os.environ["HOME"] = str(HOME)
    for k in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        os.environ.pop(k, None)
    steam = HOME / ".local/share/Steam"
    apps = steam / "steamapps"
    (apps / "appmanifest_42.acf").parent.mkdir(parents=True, exist_ok=True)
    (apps / "appmanifest_42.acf").write_text('"AppState"\n{\n "appid" "42"\n "name" "Cool Game"\n'
                                             ' "installdir" "Cool Game"\n}\n')
    g = apps / "common/Cool Game/bin"
    fake_dll(g / "nvngx_dlss.dll", (310, 1, 0, 0))
    fake_dll(g / "sl.interposer.dll", (2, 7, 0, 0))
    fake_dll(g / "sl.dlss.dll", (2, 7, 0, 0))
    fake_dll(g / "sl.old_plugin.dll", (2, 7, 0, 0))
    (apps / "compatdata/42/pfx/drive_c/windows/system32").mkdir(parents=True)
    acc = steam / "userdata/12345/config"
    acc.mkdir(parents=True)
    (acc / "localconfig.vdf").write_text('"UserLocalConfigStore"\n{\n\t"friends"\n\t{\n\t\t"PersonaName"\t\t"tester"\n'
                                         '\t}\n\t"Software"\n\t{\n\t}\n}\n')
    (steam / "config").mkdir(parents=True)
    (steam / "config/config.vdf").write_text('"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
                                             '\t\t\t"Steam"\n\t\t\t{\n\t\t\t}\n\t\t}\n\t}\n}\n')
    ct = steam / "compatibilitytools.d/GE-Proton10-1"
    ct.mkdir(parents=True)
    (ct / "proton").write_text("#!/bin/sh\n")
    (ct / "compatibilitytool.vdf").write_text('"compatibilitytools"\n{\n "compat_tools"\n {\n  "GE-Proton10-1"\n'
                                              '  {\n   "display_name" "GE-Proton10-1"\n  }\n }\n}\n')
    # Lutris game with its own Wine prefix (DXVK 2.3 inside)
    lut = HOME / ".local/share/lutris/games"
    lut.mkdir(parents=True)
    pfx = HOME / "Games/rpg/prefix"
    for d in ("system32", "syswow64"):
        (pfx / "drive_c/windows" / d).mkdir(parents=True)
    for n in ("d3d11", "dxgi"):
        dxvk_dll(pfx / f"drive_c/windows/system32/{n}.dll", "2.3")
        dxvk_dll(pfx / f"drive_c/windows/syswow64/{n}.dll", "2.3")
    exe = HOME / "Games/rpg/game/rpg.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    fake_dll(exe.parent / "nvngx_dlssg.dll", (310, 1, 0, 0))
    (HOME / "Games/rpg/game/EasyAntiCheat").mkdir()
    (lut / "rpg-1.yml").write_text(f"game:\n  exe: {exe}\n  prefix: {pfx}\nsystem: {{}}\n")
    # NGX builds already in DRSTool's cache → no download
    cache = HOME / ".cache/drstool/dll-manager/ngx"
    fake_dll(cache / "dlss/310.4.0/nvngx_dlss.dll", (310, 4, 0, 0))
    fake_dll(cache / "dlssg/310.4.0/nvngx_dlssg.dll", (310, 4, 0, 0))
    fake_dll(cache / "sl_sdk_0/2.9.0/sl.interposer.dll", (2, 9, 0, 0))
    fake_dll(cache / "sl_sdk_0/2.9.0/sl.dlss.dll", (2, 9, 0, 0))
    # DXVK release archive already in the Prefix Doctor cache
    pdc = HOME / ".cache/drstool/prefix-doctor/v2.6"
    pdc.mkdir(parents=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for arch in ("x64", "x32"):
            for n in ("d3d11", "dxgi", "d3d9", "d3d10core", "d3d8"):
                data = b"MZ" + b"\0" * 100 + b"DXVK\0" + b"\0" * 20 + b"v2.6\0" + b"\0" * 64
                ti = tarfile.TarInfo(f"dxvk-2.6/{arch}/{n}.dll")
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    (pdc / "dxvk-2.6.tar.gz").write_bytes(buf.getvalue())
    return pfx, exe


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        cls.pfx, cls.exe = build_fake_home()
        m = GuiNamespace()      # imports the package after the fake home is in place
        cls.m = m
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        m.apply_theme(cls.app)
        cls.errors = []
        sys.excepthook = lambda *a: cls.errors.append("".join(traceback.format_exception(*a)))
        cls.win = m.MainWindow()
        cls.win.resize(1400, 900)
        cls.win.show()
        cls.pump()

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.pump()

    @classmethod
    def pump(cls, n=5):
        for _ in range(n):
            cls.app.processEvents()

    def wait_for(self, cond, timeout=30):
        t0 = time.time()
        while not cond():
            self.pump(1)
            time.sleep(0.01)
            if time.time() - t0 > timeout:
                self.fail("timed out")

    def tearDown(self):
        self.pump()
        if self.errors:
            msg = "\n".join(self.errors)
            self.errors.clear()
            self.fail("uncaught exception in a Qt slot:\n" + msg)

    # ── whole window ────────────────────────────────────────────────────────
    def test_every_page_builds(self):
        et = self.win._extra_tools
        self.assertIn("DLL Manager", et.tab_titles())
        self.assertNotIn("Prefix Doctor", et.tab_titles())
        self.assertTrue(et.show_tab("Prefix Doctor"))          # old session / palette entries still work
        self.assertEqual(et._tabs.tabText(et._tabs.currentIndex()), "DLL Manager")
        for t in et.tab_titles():
            self.assertTrue(et.show_tab(t))
            self.pump()
        st = self.win._sync_tabs
        titles = [st.tabText(i) for i in range(st.count())]
        self.assertIn("Add to Steam", titles)
        for i in range(st.count()):
            st.setCurrentIndex(i)
            self.pump()

    # ── share codes ─────────────────────────────────────────────────────────
    def test_share_roundtrip_through_dialog(self):
        m, sm = self.m, self.win.settings_manager
        sm.import_profiles({"Orig": {"settings": {"0x10E41DF3": "0x1"}, "arch": None,
                                     "env_vars": {"PROTON_ENABLE_NVAPI": "1", "LD_PRELOAD": "/x.so"},
                                     "gamescope_enabled": False, "gamescope_flags": {}}})
        code = m.profile_share_code("Orig", sm.get_profiles()["Orig"])
        dlg = m.ShareImportDialog(sm, "see " + code + " bye")
        self.pump()
        self.assertIsNotNone(dlg._share)
        self.assertTrue(dlg._save.isEnabled())
        self.assertTrue(dlg._flag_box.isVisible() or dlg._flag_box.text())
        dlg._name.setText("Shared")
        dlg._accept(False)
        p = sm.get_profiles()["Shared"]
        self.assertNotIn("LD_PRELOAD", p["env_vars"])          # flagged var left out by default
        self.assertEqual(p["env_vars"]["PROTON_ENABLE_NVAPI"], "1")
        bad = m.ShareImportDialog(sm, "DRSTOOL1:garbage")
        self.pump()
        self.assertFalse(bad._save.isEnabled())
        self.assertIn("damaged", bad._state.text())
        # Ctrl+Shift+V routing: a share code opens the share dialog, not the launch-line one
        with mock.patch.object(m.ProfileManagerWidget, "open_share_import") as osi:
            self.win.open_import_dialog(code)
            osi.assert_called_once()

    # ── add to Steam ────────────────────────────────────────────────────────
    def test_add_to_steam(self):
        m = self.m
        w = self.win._steam_shortcuts
        w.rescan()
        self.pump()
        self.assertEqual(w._acct.count(), 1)
        w._src.setCurrentIndex(w.SRC_PROGRAM)
        w._name.setText("My Game")
        w._exe.setText(str(self.exe))
        gi = w._compat.findData("GE-Proton10-1")
        self.assertGreater(gi, 0)
        w._compat.setCurrentIndex(gi)
        self.win.set_env_var("PROTON_ENABLE_NVAPI", "1")
        self.pump()
        with patch_name(m, "_proc_running", return_value=False):
            w._apply()
        self.assertIn("Added", w._status.text(), w._status.text())
        vdf = HOME / ".local/share/Steam/userdata/12345/config/shortcuts.vdf"
        root = m.steam_shortcuts.load(vdf)
        sc = m.steam_shortcuts.list_shortcuts(root)
        self.assertEqual(len(sc), 1)
        self.assertEqual(sc[0].name, "My Game")
        self.assertIn("PROTON_ENABLE_NVAPI=1", sc[0].launch_options)
        self.assertTrue(sc[0].launch_options.endswith("%command%"))
        cfg = (HOME / ".local/share/Steam/config/config.vdf").read_text()
        self.assertIn(str(sc[0].appid), cfg)
        self.assertIn("GE-Proton10-1", cfg)
        # running Steam blocks the write
        with patch_name(m, "_proc_running", return_value=True), \
                mock.patch.object(m.QMessageBox, "warning") as warn:
            w._apply()
            warn.assert_called_once()
        # update in place, then remove
        w._args.setText("-dx12")
        with patch_name(m, "_proc_running", return_value=False):
            w._apply()
        sc = m.steam_shortcuts.list_shortcuts(m.steam_shortcuts.load(vdf))
        self.assertEqual(len(sc), 1)
        self.assertIn("%command% -dx12", sc[0].launch_options)
        self.assertEqual(w._existing.count(), 1)
        with patch_name(m, "_proc_running", return_value=False), \
                mock.patch.object(m.QMessageBox, "question", return_value=m.QMessageBox.Yes):
            w._remove()
        self.assertEqual(m.steam_shortcuts.list_shortcuts(m.steam_shortcuts.load(vdf)), [])
        # a native script never gets a Proton mapping, even if one was picked for an .exe before
        w._exe.setText(str(self.exe))
        w._compat.setCurrentIndex(gi)
        w._exe.setText("/usr/bin/true")
        self.assertEqual(w._plan()["compat"], "")
        self.assertNotIn(str(sc[0].appid), (HOME / ".local/share/Steam/config/config.vdf").read_text())
        self.win.set_env_var("PROTON_ENABLE_NVAPI", "")

    # ── lutris-game-tune without setuid ─────────────────────────────────────
    def test_lgtune_polkit_commands(self):
        m = self.m
        ls = self.win._lutris_sync_widget
        ls._chk_lgtune.setChecked(True)
        w = m.ShortcutWidget.LGTUNE
        try:
            with patch_name(m, "lgtune_mode", return_value="polkit"):
                keys, _env, _warn = ls._split_gamescope({})
                self.assertEqual(keys["prelaunch_command"], f"pkexec {w} PRE")
                self.assertEqual(keys["postexit_command"], f"pkexec {w} POST")
                self.assertTrue(keys["prefix_command"].startswith(f"{w} RUN "))   # RUN never via pkexec
                sc = self.win._shortcut
                sc._lg.setEnabled(True)
                sc._lg.setChecked(True)
                script = sc.build_script()
                self.assertIn('pkexec "$W" PRE', script)
                self.assertIn("trap 'pkexec \"$W\" POST' EXIT", script)
            with patch_name(m, "lgtune_mode", return_value="setuid"):
                keys, _env, _warn = ls._split_gamescope({})
                self.assertEqual(keys["prelaunch_command"], f"{w} PRE")
        finally:
            ls._chk_lgtune.setChecked(False)
            self.win._shortcut._lg.setChecked(False)

    # ── DLL manager ─────────────────────────────────────────────────────────
    def test_dll_manager(self):
        m = self.m
        et = self.win._extra_tools
        et.show_tab("DLL Manager")
        w = et.dllmgr
        self.wait_for(lambda: w._scan_worker is not None and not w._scan_worker.isRunning()
                      and len(w._states) >= 2)
        names = {s.game.name: s for s in w._states.values()}
        self.assertIn("Cool Game", names)
        rpg = next(s for s in w._states.values() if s.game.launcher == "Lutris")
        self.assertEqual(self.m._vtuple(rpg.prefix["dxvk"]["version"]), (2, 3))
        self.assertTrue(rpg.dlls.anti_cheat)
        # what "Check for updates" would return, without the network
        builds = [m.NgxBuild("dlss", m.ngx_ver_pack("310.4.0"), m.ngx_universal_payload("dlss"), 100),
                  m.NgxBuild("dlssg", m.ngx_ver_pack("310.4.0"), m.ngx_universal_payload("dlssg"), 100),
                  m.NgxBuild("sl_sdk_0", m.ngx_ver_pack("2.9.0"), m.ngx_universal_payload("sl_sdk_0"), 100)]
        rels = {"dxvk": {"tag_name": "v2.6", "assets": [{
            "name": "dxvk-2.6.tar.gz",
            "size": (HOME / ".cache/drstool/prefix-doctor/v2.6/dxvk-2.6.tar.gz").stat().st_size,
            "browser_download_url": "https://invalid.example/never-used"}]}}
        w._on_online(builds, rels)
        self.pump()
        cool = names["Cool Game"]
        plan = m.dllm_plan(cool, w._targets())
        self.assertEqual(len(plan["files"]), 2)               # SR + Streamline
        self.assertEqual(plan["prefix"], [])                  # Proton prefix is left alone
        rplan = m.dllm_plan(rpg, w._targets())
        self.assertEqual(rplan["prefix"], ["dxvk"])
        w._select_outdated()
        self.assertEqual(w._items[cool.game.key].checkState(0), m.Qt.Checked)
        self.assertEqual(w._items[rpg.game.key].checkState(0), m.Qt.Unchecked)   # anti-cheat
        with mock.patch.object(m.QMessageBox, "question", return_value=m.QMessageBox.Yes):
            w._items[rpg.game.key].setCheckState(0, m.Qt.Checked)
            self.pump()
            with patch_name(m, "_proc_running", return_value=False):
                w._update()
        self.wait_for(lambda: not w._busy())
        self.pump(20)
        self.wait_for(lambda: not w._busy())
        g = HOME / ".local/share/Steam/steamapps/common/Cool Game/bin"
        self.assertEqual(m.pe_version.read_version(g / "nvngx_dlss.dll"), (310, 4, 0, 0))
        self.assertEqual(m.pe_version.read_version(g / "sl.interposer.dll"), (2, 9, 0, 0))
        self.assertEqual(m.pe_version.read_version(g / "sl.old_plugin.dll"), (2, 7, 0, 0))
        self.assertEqual(m.pe_version.read_version(self.exe.parent / "nvngx_dlssg.dll"), (310, 4, 0, 0))
        self.assertEqual(m._vtuple(m.detect_installed(self.pfx, m._DLLM_PD["dxvk"])["version"]), (2, 6))
        log = w._log.toPlainText()
        self.assertIn("not part of Streamline", log)
        # after the rescan nothing is outdated any more
        self.wait_for(lambda: m.dllm_plan(w._states[cool.game.key], w._targets())["files"] == [])
        # restore puts the originals back
        bk = m.dll_ops.list_backups(cool.game.key)
        self.assertEqual(len(bk), 1)
        n, _notes = m.dll_ops.restore(bk[0][0])
        self.assertEqual(n, 3)
        self.assertEqual(m.pe_version.read_version(g / "nvngx_dlss.dll"), (310, 1, 0, 0))
        n, stamp = m.restore_prefix_backup(self.pfx)
        self.assertGreater(n, 0)
        self.assertEqual(m._vtuple(m.detect_installed(self.pfx, m._DLLM_PD["dxvk"])["version"]), (2, 3))
        dlg = m.DllRestoreDialog(w._states[cool.game.key])
        self.assertEqual(dlg._list.topLevelItemCount(), 1)

    def test_dll_manager_scan_button(self):
        # clicked(bool) must not reach scan(only=...) as only=False
        w = self.win._extra_tools.dllmgr
        self.win._extra_tools.show_tab("DLL Manager")
        self.wait_for(lambda: not w._busy())
        w._scan_btn.click()
        self.wait_for(lambda: w._scan_worker is not None and not w._scan_worker.isRunning())
        self.pump(10)
        self.assertGreaterEqual(len(w._states), 2)

    def test_launcher_apply_refuses_unreadable_config(self):
        # A config that does not parse must not be replaced by one holding only DRSTool's keys
        m = self.m
        payload = {"env": {"PROTON_ENABLE_NVAPI": "1"}, "gs_on": False, "gs_flags": {}, "mangohud": False}
        bottle = HOME / "bad-bottle/bottle.yml"
        bottle.parent.mkdir(parents=True, exist_ok=True)
        bottle.write_text("Name: x\nRunner: [unclosed\n")
        heroic = HOME / "bad-heroic/GamesConfig/abc.json"
        heroic.parent.mkdir(parents=True, exist_ok=True)
        heroic.write_text('{"abc": {"winePrefix": "/x", ')
        for backend, path in ((m.BottlesBackend(), bottle), (m.HeroicBackend(), heroic)):
            before = path.read_text()
            with self.assertRaises(Exception):
                backend.apply(m.LEntry("abc", "t", path), payload, {})
            self.assertEqual(path.read_text(), before, backend.name)

    def test_empty_xdg_vars_mean_default(self):
        # XDG spec: an empty variable counts as unset (not a path relative to the cwd)
        m = self.m
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "", "XDG_CACHE_HOME": "", "XDG_DATA_HOME": ""}):
            self.assertEqual(m.SettingsManager._profiles_path(), HOME / ".config/drstool/profiles.json")
            self.assertEqual(m.user_cache_dir(), HOME / ".cache/drstool")
            self.assertEqual(m._cache_dir(), HOME / ".cache/drstool/prefix-doctor")
            self.assertEqual(m.LutrisSyncWidget._default_games_dir(), HOME / ".local/share/lutris/games")
            self.assertTrue(all(Path(n["path"]).is_absolute()
                                for n in m.discover_cache_nodes({}, []) if n.get("path")))

    def test_tasks_wait_all_before_exit(self):
        # a worker still running when the window goes away aborts the process
        # ("QThread: Destroyed while thread is still running")
        m = self.m

        class Slow(m.QThread):
            cancelled = False

            def cancel(self):
                Slow.cancelled = True

            def run(self):
                t0 = time.time()
                while not Slow.cancelled and time.time() - t0 < 5:
                    time.sleep(0.01)
        th = Slow()
        m.TASKS.track(th, "slow")
        th.start()
        m.TASKS.wait_all()
        self.assertTrue(Slow.cancelled)
        self.assertFalse(th.isRunning())

    def test_dll_manager_streamline_plugins_on_target(self):
        # NGX's Streamline package has no interposer: plugins already on the target version
        # mean "up to date", whatever interposer the game ships.
        m = self.m
        game = HOME / "Games/SL Game"
        fake_dll(game / "sl.interposer.dll", (2, 14, 1, 0))
        fake_dll(game / "sl.common.dll", (2, 14, 16, 0))
        fake_dll(game / "sl.dlss.dll", (2, 14, 16, 0))
        entry = m.game_scan.GameEntry("Custom", "custom:sl", "SL Game", [game])
        st = m.DllGameState(entry, m.game_scan.find_dlls(entry))
        targets = {"sl_sdk_0": m.NgxBuild("sl_sdk_0", m.ngx_ver_pack("2.14.16"),
                                          m.ngx_universal_payload("sl_sdk_0"), 100)}
        self.assertEqual(m.dllm_plan(st, targets)["files"], [])
        self.assertEqual(m.dllm_cell(st, "sl_sdk_0", targets), ("2.14.16", "ok"))
        # plugins that really are older are still offered
        stale = HOME / "Games/Stale SL Game"
        fake_dll(stale / "sl.interposer.dll", (2, 8, 0, 0))
        fake_dll(stale / "sl.common.dll", (2, 8, 0, 0))
        entry = m.game_scan.GameEntry("Custom", "custom:stale", "Stale SL Game", [stale])
        st = m.DllGameState(entry, m.game_scan.find_dlls(entry))
        self.assertEqual(len(m.dllm_plan(st, targets)["files"]), 1)
        self.assertEqual(m.dllm_cell(st, "sl_sdk_0", targets), ("2.8.0", "old"))

    # ── logging ─────────────────────────────────────────────────────────────
    def test_log_profiles_match_var_options(self):
        m = self.m
        for prof in (m.LOG_ENV_ON, m.LOG_ENV_OFF):
            for k, v in prof.items():
                ev = m.env_def(k)
                self.assertIsNotNone(ev, k)
                if ev.vtype == "enum":
                    self.assertIn(v, ev.options, k)
                elif ev.vtype == "flags":
                    toks = v.split(",")
                    self.assertEqual([t for t in toks if t not in ev.options], [], k)
                    self.assertLessEqual(len(set(toks) & set(ev.exclusive)), 1, k)
        # ON must be verbose: no level left at error/err
        self.assertNotIn(m.LOG_ENV_ON["DXVK_LOG_LEVEL"], ("none", "error"))
        self.assertNotIn("-all", m.LOG_ENV_ON["WINEDEBUG"].split(","))

    def test_log_toggle_and_winedebug_exclusive(self):
        ew, ed = self.win._env_widget, self.win._env_editor
        ew.reset_all_values()
        ew.set_logging(True)
        self.assertEqual(ew.logging_state(), "on")
        ed.set_var(self.m.env_def("WINEDEBUG"))
        btns = ed._flag_btns
        self.assertTrue(btns["warn+all"].isChecked())
        btns["-all"].click()                        # base levels are exclusive
        self.pump()
        self.assertFalse(btns["warn+all"].isChecked())
        val = ew.get_value("WINEDEBUG").split(",")
        self.assertIn("-all", val)
        self.assertNotIn("warn+all", val)
        self.assertIn("+seh", val)                  # channel toggles untouched
        self.assertEqual(ew.logging_state(), "mixed")
        btns["+relay"].click()                      # non-group flags still combine
        self.pump()
        self.assertTrue(btns["-all"].isChecked())
        # unknown tokens stay visible/editable instead of hidden
        ew.set_value("WINEDEBUG", "-all,warn+dll")
        ed.set_var(self.m.env_def("WINEDEBUG"))
        self.assertEqual(ed._flag_extra_tokens, ["warn+dll"])
        ew.set_logging(False)
        self.assertEqual(ew.logging_state(), "off")
        self.assertEqual(ew.get_value("WINEDEBUG"), "-all")

    def test_log_viewer_enable_sets_full_profile(self):
        ew = self.win._env_widget
        ew.reset_all_values()
        ew.set_logging(False)                       # leftover WINEDEBUG=-all
        lv = self.m.LogViewerWidget()
        with mock.patch.object(lv, "window", return_value=self.win):
            lv._enable_logging()
        self.assertEqual(ew.logging_state(), "on")
        self.assertTrue(ew.get_value("PROTON_LOG_DIR"))
        lv.deleteLater()
        ew.reset_all_values()


    def test_vdf_unbalanced_block_is_rejected(self):
        m = self.m
        good = '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n'
        bad = good.rsplit("}", 2)[0]                       # truncated: outer blocks never closed
        self.assertIn('"7"', m.vdf_set_launch_options(good, "7", "x"))
        with self.assertRaises(ValueError):
            m.vdf_set_launch_options(bad, "7", "x")
        self.assertEqual(m.vdf_get_launch_options(bad, "7"), "")

    def test_load_frametimes_streams_both_formats(self):
        m = self.m
        with tempfile.TemporaryDirectory() as td:
            mh = Path(td) / "mh.csv"
            mh.write_text("os,cpu\nLinux,x\nfps,frametime,cpu_load\n"
                          + "".join(f"60,{16.6 + i % 3},5\n" for i in range(50)))
            d = m.load_frametimes(str(mh))
            self.assertEqual((d["kind"], len(d["ft"])), ("mangohud", 50))
            flm = Path(td) / "flm.csv"                       # frametime is the LAST column, CRLF endings
            flm.write_text("flip_ns,interval_ns,is_hitch\r\n"
                           + "".join(f"{i},16666666,{int(i == 3)}\r\n" for i in range(40)))
            d = m.load_frametimes(str(flm))
            self.assertEqual((d["kind"], len(d["ft"]), d["extra"]["FLM hitches"]), ("flm", 40, 1))
            junk = Path(td) / "junk.txt"
            junk.write_text("hello\nworld\n")
            with self.assertRaises(ValueError):
                m.load_frametimes(str(junk))

    def test_save_profiles_unique_tmp_and_mode(self):
        sm = self.win.settings_manager
        path = sm._profiles_path()
        before = path.stat().st_mode & 0o777 if path.exists() else None
        self.assertTrue(sm._save_profiles())
        self.assertEqual([p.name for p in path.parent.glob("*.tmp")], [])      # no leftovers
        if before is not None:
            self.assertEqual(path.stat().st_mode & 0o777, before)
        with mock.patch.object(sm, "_snapshot_old", side_effect=RuntimeError("boom")), \
                mock.patch.object(self.m.QMessageBox, "warning"):          # the error signal opens a modal box
            self.assertFalse(sm._save_profiles())
        self.assertEqual([p.name for p in path.parent.glob("*.tmp")], [])      # cleaned after a failure

    def test_log_search_is_debounced(self):
        lv = self.m.LogViewerWidget()
        with mock.patch.object(lv, "_render") as r:
            lv._find_timer.timeout.disconnect()
            lv._find_timer.timeout.connect(r)
            for ch in "abc":
                lv._find.setText(lv._find.text() + ch)
            self.assertEqual(r.call_count, 0)
            self.pump()
            time.sleep(0.25)
            self.pump()
            self.assertEqual(r.call_count, 1)
        lv.deleteLater()

    def test_share_code_flags_shell_env(self):
        from drstool_core import share_code
        for k in ("BASH_ENV", "ENV", "SHELLOPTS", "IFS"):
            self.assertIn(k, share_code.SENSITIVE_ENV)


    # ── Games tab: game card and profile ↔ game links ───────────────────────
    def test_games_tab_card_and_profile_links(self):
        m, win, sm = self.m, self.win, self.win.settings_manager
        (HOME / "steam-42.log").write_text("info: start\nerr:module:import_dll Library MSVCP140.dll not found\n")
        rows = "".join(f"{60 + i % 3},{16.0 + (i % 5)}\n" for i in range(200))
        (HOME / "CoolGame_2026-10-01_10-00-00.csv").write_text("fps,frametime\n" + rows)
        lcf = HOME / ".local/share/Steam/userdata/12345/config/localconfig.vdf"
        orig = lcf.read_text()
        self.addCleanup(lcf.write_text, orig)
        lcf.write_text(orig.replace('"Software"\n\t{\n\t}',
                                    '"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
                                    '\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}'))
        win._switch_tab(win.TAB_GAMES)
        gl, card = win._games_list, win._game_card
        self.wait_for(lambda: gl.game("steam:42") is not None and gl.game("lutris:rpg-1") is not None)
        self.assertTrue(gl.select_key("steam:42"))
        self.wait_for(lambda: card.game is not None and card.game.key == "steam:42" and card._info)
        self.assertEqual(win._right_stack.currentIndex(), win.PAGE_GAMES)
        self.assertIn("DLSS 310.1.0", card.t_dlls.value.text())
        self.assertIn("2.7.0", card.t_dlls.detail.text())            # Streamline
        self.assertEqual(card.t_log.value.text(), "1 known problem")
        self.assertIn("FPS avg", card.t_ft.value.text())
        self.assertEqual(card.t_pdb.value.text(), "not fetched")       # no network unless asked
        self.assertEqual(card.t_profile.value.text(), "No profile")

        # profile → card → write to Steam: the link is recorded with the profile's fingerprint
        win.load_profile_by_name("GamesLinkP") if "GamesLinkP" in sm.get_profiles() else None
        win.set_env_var("PROTON_ENABLE_NVAPI", "1")
        sm.save_profile("GamesLinkP", win._env_widget.get_env_dict())
        self.pump()
        card._prof_combo.setCurrentIndex(card._prof_combo.findData("GamesLinkP"))
        card._assign_from_combo(0)
        self.assertIn("not written", card.t_profile.detail.text())
        with patch_name(m, "_proc_running", return_value=False), \
                mock.patch.object(m.QMessageBox, "question", return_value=m.QMessageBox.Yes):
            card._write()
        ln = m.LINKS.get("steam:42")
        self.assertEqual(ln["profile"], "GamesLinkP")
        self.assertEqual(ln["fp"], m.profile_fp(sm.get_profiles()["GamesLinkP"]))
        self.assertEqual(ln["name"], "Cool Game")
        self.assertIn("PROTON_ENABLE_NVAPI=1", lcf.read_text())
        self.assertIn("up to date", card.t_profile.detail.text())

        # a write from the Lutris sync tab links that game as well
        ls = win._lutris_sync_widget
        i = next(i for i, g in enumerate(ls._games) if g.path.stem == "rpg-1")
        ls._game_combo.setCurrentIndex(i)
        self.assertTrue(ls._apply_to_game(confirm=False))
        self.assertEqual(m.LINKS.get("lutris:rpg-1")["profile"], "GamesLinkP")
        self.assertEqual(m.LINKS.games_for("GamesLinkP"), ["steam:42", "lutris:rpg-1"])
        self.assertIn("2 games", win._profiles_widget._profile_list.findItems(
            "GamesLinkP", m.Qt.MatchExactly)[0].data(m.ROLE_VALUE))

        # changing and saving the profile offers to update both games
        win.set_env_var("DXVK_ASYNC", "1")
        with mock.patch("drstool_gui.main_window.toast") as t:
            sm.save_profile("GamesLinkP", win._env_widget.get_env_dict())
        offers = [c for c in t.call_args_list if len(c.args) > 3 and c.args[3]]
        self.assertTrue(offers and "2 games" in offers[-1].args[0], t.call_args_list)
        dlg = m.LinkedGamesDialog("GamesLinkP", sm.get_profiles(), win)
        self.assertEqual(len(dlg._checked()), 2)
        dlg._accept()
        with patch_name(m, "_proc_running", return_value=False):
            res = win.write_profile_to_games("GamesLinkP", dlg.chosen, confirm=False)
        self.assertTrue(all(ok for _n, ok, _m in res), res)
        fp = m.profile_fp(sm.get_profiles()["GamesLinkP"])
        self.assertEqual(m.LINKS.outdated("GamesLinkP", fp), [])
        self.assertIn("DXVK_ASYNC", (HOME / ".local/share/lutris/games/rpg-1.yml").read_text())

        # rename keeps the links, delete drops them (the last write stays known)
        self.assertTrue(sm.rename_profile("GamesLinkP", "GamesLinkQ"))
        self.assertEqual(len(m.LINKS.games_for("GamesLinkQ")), 2)
        sm.delete_profile("GamesLinkQ")
        self.assertEqual(m.LINKS.games_for("GamesLinkQ"), [])
        self.assertIsNone(m.LINKS.get("steam:42")["profile"])
        self.assertIn("without a profile", card.t_profile.detail.text())
        for k in ("PROTON_ENABLE_NVAPI", "DXVK_ASYNC"):
            win.set_env_var(k, "")
        win._switch_tab(win.TAB_DRS)


if __name__ == "__main__":
    unittest.main()
