"""Offscreen GUI tests: the whole window starts, every tool page and sync tab is built,
and the new features work end to end against a fake home folder (no network).

Skipped automatically when PySide6 is not installed.

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_gui -v
"""

import importlib.util
import io
import json
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
class DriftCatalogTests(unittest.TestCase):
    """User catalog, fork analysis and drift worker. No network: fetches are mocked."""

    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        cls._old_mod = sys.modules.get("DRSTool")
        cls._tmp = tempfile.TemporaryDirectory(prefix="drstool-uc-")
        cls._env = {k: os.environ.get(k) for k in ("HOME", "XDG_CONFIG_HOME")}
        os.environ["HOME"] = cls._tmp.name
        os.environ["XDG_CONFIG_HOME"] = str(Path(cls._tmp.name) / "cfg")
        spec = importlib.util.spec_from_file_location("DRSTool", ROOT / "DRSTool.py")
        cls.m = importlib.util.module_from_spec(spec)
        sys.modules["DRSTool"] = cls.m
        spec.loader.exec_module(cls.m)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        if cls._old_mod is not None:
            sys.modules["DRSTool"] = cls._old_mod
        cls._tmp.cleanup()

    def cat_file(self):
        return self.m.user_catalog_path()

    # ── load / apply ────────────────────────────────────────────────────────
    def test_load_missing_and_corrupt(self):
        m, p = self.m, self.cat_file()
        self.assertFalse(p.exists())
        self.assertEqual(m.load_user_catalog()["env_vars"], [])
        p.parent.mkdir(parents=True, exist_ok=True)
        for junk in ("{not json", "[1, 2]", '{"env_vars": "x", "vkd3d_flags": [1, {"name": "a"}]}'):
            p.write_text(junk)
            d = m.load_user_catalog()
            self.assertEqual(d["env_vars"], [])
        self.assertEqual(d["vkd3d_flags"], [{"name": "a"}])
        p.unlink()

    def test_save_roundtrip(self):
        m = self.m
        d = m.load_user_catalog()
        d["env_vars"].append({"name": "X_ROUNDTRIP"})
        m.save_user_catalog(d)
        self.assertEqual(m.load_user_catalog()["env_vars"], [{"name": "X_ROUNDTRIP"}])
        self.cat_file().unlink()

    def test_apply_all_targets(self):
        m = self.m
        vk = next(e for e in m.VKD3D_ENV_VARS if e.name == "VKD3D_CONFIG")
        data = {
            "env_vars": [{"name": "UC_TEST_ENV", "cat": "DXVK Forks", "vtype": "enum", "options": ["1"], "desc": "d"},
                         {"name": "DXVK_HUD"}],                              # built-in → skipped
            "vkd3d_flags": [{"name": "uc_flag", "desc": "vk desc", "group": "🔦 Raytracing (DXR)"}],
            "dxvk_conf_keys": [{"key": "ddraw.ucKey", "kind": "enum", "opt": ["True", "False"], "desc": "k",
                                "group": "🗿 DirectDraw (d7vk/Sarek)"},
                               {"key": "ddraw.bad", "kind": "enum", "opt": []}],  # invalid → skipped
            "gamescope_flags": [{"name": "Uc Flag", "flag": "--uc-flag", "cat": "Debug", "kind": "toggle", "desc": "g"}],
            "drs_settings": [{"id": "0xDEAD0001", "name": "UC SETTING", "cat": "Misc", "type": "enum", "default": "0x0",
                              "desc": "s", "values": [["Off", "0x0"], ["On", "0x1"]]},
                             {"id": "nope"}],
            "sources": [{"key": "uc-src", "title": "UcFork", "host": "gitlab", "repo": "a/b", "base": "vkd3d"}],
        }
        notes = m.apply_user_catalog(data)
        self.assertEqual(len(notes), 3, notes)                                # DXVK_HUD, bad conf, bad drs id
        ev = m._ENV_BY_NAME["UC_TEST_ENV"]
        self.assertIn(ev, m.ALL_ENV_VARS)
        self.assertEqual(ev.options, ["1"])
        self.assertIn("uc_flag", vk.options)
        self.assertEqual(m.VKD3D_CONFIG_DESCS["uc_flag"], "vk desc")
        groups = dict(m.grouped_names(m.VKD3D_FLAG_GROUPS, vk.options))
        self.assertIn("uc_flag", groups["🔦 Raytracing (DXR)"])
        self.assertIn("ddraw.ucKey", m.DXVK_CONFIG_KEY_NAMES)
        self.assertNotIn("ddraw.bad", m.DXVK_CONFIG_KEY_NAMES)
        self.assertIn("ddraw.ucKey", dict(m.grouped_names(m.DXVK_KEY_GROUPS, m.DXVK_CONFIG_KEY_NAMES))["🗿 DirectDraw (d7vk/Sarek)"])
        self.assertTrue(any(g.flag == "--uc-flag" for g in m.GAMESCOPE_FLAGS))
        st = m._SETTING_BY_ID["0XDEAD0001"]
        self.assertIs(m._SETTING_BY_INT[0xDEAD0001], st)
        self.assertIn(st, m.ALL_SETTINGS)
        self.assertEqual([v.val for v in st.values], ["0x0", "0x1"])
        self.assertEqual(next(s for s in m.DRIFT_SOURCES if s["key"] == "uc-src")["base"], "vkd3d")
        # the next drift scan sees them as catalogued
        cat = m._drift_catalog()
        self.assertIn("UC_TEST_ENV", cat["env_all"])
        self.assertIn("uc_flag", cat["vkd3d_flags"])
        self.assertIn("ddraw.ucKey", cat["conf_keys"])
        self.assertIn("uc-flag", cat["gs_flags"])
        self.assertIn("0XDEAD0001", cat["drs_ids"])
        self.assertIn("env:UC_TEST_ENV", m.USER_CATALOG_KEYS)

    def test_form_record_roundtrip_and_validate(self):
        m = self.m
        f = m._uc_form("drs", {"id": "0xAB", "name": "N", "type": "enum", "values": [["A", "0x1"]]})
        f["name"] = "0x0000ABCD"
        rec = m._uc_record(f)
        self.assertEqual((rec["id"], rec["values"]), ("0x0000ABCD", [["A", "0x1"]]))
        self.assertEqual(m._uc_validate(f), "")
        self.assertIn("8 hex", m._uc_validate(dict(f, name="0x12")))
        self.assertIn("option", m._uc_validate(dict(f, options=[])))
        self.assertIn("Label=", m._uc_validate(dict(f, options=["x"])))
        self.assertIn("empty", m._uc_validate(dict(f, name=" ")))
        self.assertIn("already", m._uc_validate({"target": "env", "name": "DXVK_HUD", "type": "string", "options": []},
                                                 m._drift_catalog()))
        self.assertIn("Setting('0x0000ABCD'", m._uc_as_python("drs", rec))
        d = {}
        self.assertTrue(m._uc_add(d, "env", {"name": "A"}))
        self.assertFalse(m._uc_add(d, "env", {"name": "a", "desc": "x"}))      # same key → replaced
        self.assertEqual(d["env_vars"], [{"name": "a", "desc": "x"}])
        self.assertEqual(m._uc_merge(d, {"env_vars": [{"name": "A"}, {"name": "B"}, "junk"]}), 1)

    # ── sources / urls ──────────────────────────────────────────────────────
    def test_raw_url_and_patch_lines(self):
        m = self.m
        up = {"repo": "o/r", "ref": "master"}
        self.assertEqual(m._drift_raw_url(up, "master", "a/b"), "https://raw.githubusercontent.com/o/r/master/a/b")
        fork = {"repo": "o/r", "ref": None}
        self.assertEqual(m._drift_raw_url(fork, "feat/x", "a"), "https://raw.githubusercontent.com/o/r/refs/heads/feat/x/a")
        gl = {"repo": "Ph/dx", "host": "gitlab", "ref": None}
        self.assertEqual(m._drift_raw_url(gl, "main", "p/a.patch"), "https://gitlab.com/Ph/dx/-/raw/main/p/a.patch")
        self.assertEqual(m._drift_patch_added_lines("--- a\n+++ b\n+x\n y\n-z\n+\"DXVK_ASYNC\"\n"), 'x\n"DXVK_ASYNC"')

    def test_builtin_fork_sources(self):
        srcs = {s["key"]: s for s in self.m.DRIFT_SOURCES}
        for k in ("dxvk-ll", "dxvk-gplall", "dxvk-sarek", "d7vk", "vkd3d-ll", "dxvk-gplasync", "proton-ge", "proton-cachyos"):
            self.assertEqual(srcs[k]["kind"], "fork")
            self.assertIsNone(srcs[k]["ref"])                                   # never a pinned branch
            self.assertIn(srcs[k]["base"], srcs)
        self.assertEqual(srcs["dxvk"]["ref"], "master")                         # upstream untouched

    def test_resolve_ref_and_discover(self):
        m = self.m
        store = {}

        class QS:
            def __init__(self, *a): pass
            def value(self, k, d=None): return store.get(k, d)
            def setValue(self, k, v): store[k] = v

        src = dict(next(s for s in m.DRIFT_SOURCES if s["key"] == "dxvk-sarek"), discover=["src/dxvk/framepacer/*"])
        with mock.patch.object(m, "QSettings", QS), \
                mock.patch.object(m, "_drift_fetch_json", return_value=({"default_branch": "feat/x"}, "", None)) as fj:
            self.assertEqual(m._drift_resolve_ref(src), ("feat/x", ""))
            self.assertEqual(m._drift_resolve_ref(src), ("feat/x", ""))        # cached: one API call only
            self.assertEqual(fj.call_count, 1)
        store.clear()
        with mock.patch.object(m, "QSettings", QS), \
                mock.patch.object(m, "_drift_fetch_json", return_value=(None, "API rate limit (HTTP 403)", None)):
            ref, note = m._drift_resolve_ref(src)
            self.assertEqual(ref, "main")                                       # ref_fallback
            self.assertIn("403", note)
        tree = {"tree": [{"path": "src/dxvk/framepacer/a.cpp", "type": "blob"},
                         {"path": "src/dxvk/other.cpp", "type": "blob"},
                         {"path": "src/dxvk/framepacer", "type": "tree"}]}
        with mock.patch.object(m, "_drift_fetch_json", return_value=(tree, "", None)):
            self.assertEqual(m._drift_discover(src, "main")[0], ["src/dxvk/framepacer/a.cpp"])
        with mock.patch.object(m, "_drift_fetch_json", return_value=(None, "API rate limit (HTTP 403)", None)):
            self.assertEqual(m._drift_discover(src, "main"), ([], "API rate limit (HTTP 403)"))

    def test_fetch_json_rate_limit(self):
        m = self.m
        err = m.urllib.error.HTTPError("u", 403, "rate", {}, None)
        with mock.patch.object(m.urllib.request, "urlopen", side_effect=err):
            data, note, _h = m._drift_fetch_json("https://api.github.com/x")
        self.assertIsNone(data)
        self.assertIn("rate limit", note)

    # ── fork analysis ───────────────────────────────────────────────────────
    def test_fork_delta_sarek_like(self):
        m = self.m
        sarek = next(s for s in m.DRIFT_SOURCES if s["key"] == "dxvk-sarek")
        base = next(s for s in m.DRIFT_SOURCES if s["key"] == "dxvk")
        cat = m._drift_catalog()
        base_files = {"src/dxvk/dxvk_options.cpp": 'x("dxvk.upstreamNew")\n'}
        fork_files = {"src/dxvk/dxvk_options.cpp": 'x("dxvk.upstreamNew")\nx("dxvk.forkOnlyKey")\n',
                      "src/ddraw/ddraw_options.cpp": '\nx("ddraw.forkDd")\n',
                      "README.md": "`DXVK_FORK_ONLY_ENV` and DXVK_HUD\n"}
        brows = m._drift_analyse(base, base_files, cat)
        frows = m._drift_analyse(sarek, fork_files, cat)
        rows = m._drift_fork_delta(sarek, fork_files, frows, brows, cat)
        fork = {(r["kind"], r["name"]) for r in rows if r["status"] == "fork"}
        self.assertEqual(fork, {("dxvk.conf key", "dxvk.forkOnlyKey"), ("dxvk.conf key", "ddraw.forkDd"),
                                ("env var", "DXVK_FORK_ONLY_ENV")})
        self.assertTrue(all(r["base"] == "dxvk" for r in rows if r["status"] == "fork"))
        gone = {(r["kind"], r["name"]) for r in rows if r["status"] == "gone"}
        self.assertIn(("env var", "DXVK_FRAME_PACE"), gone)                     # tagged for DXVK-Sarek, absent here
        self.assertFalse([g for g in gone if g[0] != "env var"])               # never "gone" relative to upstream
        m._drift_attach_ctx(rows, fork_files, sarek, "main")
        r = next(r for r in rows if r["name"] == "ddraw.forkDd")
        self.assertIn("ddraw.forkDd", r["ctx"]["text"])
        self.assertEqual(r["blob_url"], "https://github.com/pythonlover02/DXVK-Sarek/blob/main/src/ddraw/ddraw_options.cpp#L2")

    def test_worker_urls_fallback_and_patches(self):
        m = self.m
        gp = dict(next(s for s in m.DRIFT_SOURCES if s["key"] == "dxvk-gplasync"))
        urls = []

        def fetch(url, timeout=25.0):
            urls.append(url)
            if url.endswith("/dxvk.conf"):
                return "# dxvk.baseKey = 1\n"
            if url.endswith("a.patch"):
                return '--- a\n+++ b\n+  "DXVK_PATCH_ONLY_ENV"\n+  x("dxvk.enableAsyncPatch")\n'
            return None

        got = {}
        w = m._DriftWorker([gp], m._drift_catalog())
        w.done.connect(lambda rows, notes: got.update(rows=rows, notes=notes))
        tree = [{"path": "patches/a.patch", "type": "blob"}]
        calls = iter([({"default_branch": "feat/x"}, "", None), (tree, "", {})])
        with mock.patch.object(m, "_drift_fetch", fetch), \
                mock.patch.object(m, "_drift_fetch_json", lambda *a, **k: next(calls)), \
                mock.patch.object(m, "QSettings", type("QS", (), {"__init__": lambda s, *a: None,
                                                                  "value": lambda s, k, d=None: d,
                                                                  "setValue": lambda s, k, v: None})):
            w.run()
        self.assertIn("https://gitlab.com/Ph42oN/dxvk-gplasync/-/raw/feat/x/patches/a.patch", urls)
        self.assertIn("https://gitlab.com/Ph42oN/dxvk-gplasync/-/raw/feat/x/dxvk.conf", urls)
        names = {r["name"]: r["status"] for r in got["rows"]}
        self.assertEqual(names.get("dxvk.enableAsyncPatch"), "fork")
        self.assertEqual(names.get("DXVK_PATCH_ONLY_ENV"), "fork")
        self.assertNotIn("DXVK_ASYNC", names)                                  # catalogued already
        self.assertNotIn("dxvk.baseKey", names)                                 # base-only: upstream's job

    def test_worker_network_error_is_a_note(self):
        m = self.m
        gp = next(s for s in m.DRIFT_SOURCES if s["key"] == "d7vk")
        got = {}
        w = m._DriftWorker([gp], m._drift_catalog())
        w.done.connect(lambda rows, notes: got.update(rows=rows, notes=notes))
        with mock.patch.object(m, "_drift_fetch", side_effect=OSError("boom")), \
                mock.patch.object(m, "_drift_fetch_json", return_value=(None, "API rate limit (HTTP 403)", None)):
            w.run()
        self.assertEqual(got["rows"], [])
        self.assertTrue(any("network error" in n for n in got["notes"]))

    def test_vkd3d_plain_flags_are_internal(self):
        m = self.m
        src = next(s for s in m.DRIFT_SOURCES if s["key"] == "vkd3d")
        decl = ('VKD3D_DECL_CONFIG("uc_public", UC_PUBLIC)\nVKD3D_DECL_CONFIG_PLAIN(UC_INTERNAL)\n'
                'VKD3D_DECL_CONFIG_PLAIN(UC_HAND_LISTED)\n')
        dev = ('static const struct o t[] = {\n#include "config_flag_decl.h"\n'
               '    {"uc_hand_listed", VKD3D_CONFIG_FLAG_INIT_STATIC(.UC_HAND_LISTED = 1)},\n};\n')
        rel = "include/private/config_flag_decl.h"

        def new_flags(files):
            rows = m._drift_analyse(src, files, m._drift_catalog())
            return {r["name"]: r["where"] for r in rows if r["kind"] == "VKD3D_CONFIG flag" and r["status"] == "new"}

        got = new_flags({rel: decl, "libs/vkd3d/device.c": dev})
        self.assertEqual(sorted(got), ["uc_hand_listed", "uc_public"])          # uc_internal: no config string
        self.assertNotIn("internal", got["uc_hand_listed"])
        got = new_flags({rel: decl})                                             # device.c missing: cannot tell
        self.assertIn("may be internal", got["uc_internal"])

    def test_guess(self):
        m = self.m
        conf = {"src": "DXVK", "kind": "dxvk.conf key", "name": "dxvk.latencySleep", "status": "new",
                "ctx": {"rel": "dxvk.conf", "line": 4, "text":
                        "\n# Enables foo.\n# Supported values: True, False, Auto\n# dxvk.latencySleep = Auto\n\n"}}
        g = m._drift_guess(conf)
        self.assertEqual((g["target"], g["type"], g["options"], g["default"]), ("conf", "enum", ["True", "False", "Auto"], "Auto"))
        self.assertEqual(g["desc"], "Enables foo. Supported values: True, False, Auto")
        self.assertEqual(g["group"], "⏱ Frame pacing & latency")
        cpp = {"src": "DXVK-Sarek", "kind": "dxvk.conf key", "name": "ddraw.forceProxy", "status": "fork", "fork_cat": "DXVK Forks",
               "ctx": {"rel": "src/ddraw/ddraw_options.cpp", "line": 9,
                       "text": 'this->x = config.getOption<bool>("ddraw.forceProxy", false);'}}
        g = m._drift_guess(cpp)
        self.assertEqual((g["type"], g["options"], g["default"], g["group"]), ("enum", ["True", "False"], "False", "🗿 DirectDraw (d7vk/Sarek)"))
        self.assertTrue(g["desc"].startswith("[DXVK-Sarek] "))
        env = {"src": "Proton-GE", "kind": "env var", "name": "PROTON_USE_HDR", "status": "fork", "fork_cat": "Proton",
               "ctx": {"rel": "README.md", "line": 3, "text": "- `PROTON_USE_HDR` - Enable HDR.\n"}}
        g = m._drift_guess(env)
        self.assertEqual((g["cat"], g["desc"]), ("Proton", "[Proton-GE] PROTON_USE_HDR - Enable HDR."))
        vk = {"src": "vkd3d-proton", "kind": "VKD3D_CONFIG flag", "name": "pacer_debug", "status": "new",
              "ctx": {"rel": "README.md", "line": 1, "text": "- `pacer_debug`: Log pacing.\n"}}
        self.assertEqual(m._drift_guess(vk)["desc"], "Log pacing.")
        drs = {"src": "NVAPI header", "kind": "DRS setting", "name": "0x1234ABCD  FOO_BAR", "status": "new"}
        g = m._drift_guess(drs)
        self.assertEqual((g["name"], g["label"], g["type"]), ("0x1234ABCD", "FOO_BAR", "hex"))


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        cls.pfx, cls.exe = build_fake_home()
        spec = importlib.util.spec_from_file_location("DRSTool", ROOT / "DRSTool.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules["DRSTool"] = m
        spec.loader.exec_module(m)
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
        with mock.patch.object(m, "_proc_running", return_value=False):
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
        with mock.patch.object(m, "_proc_running", return_value=True), \
                mock.patch.object(m.QMessageBox, "warning") as warn:
            w._apply()
            warn.assert_called_once()
        # update in place, then remove
        w._args.setText("-dx12")
        with mock.patch.object(m, "_proc_running", return_value=False):
            w._apply()
        sc = m.steam_shortcuts.list_shortcuts(m.steam_shortcuts.load(vdf))
        self.assertEqual(len(sc), 1)
        self.assertIn("%command% -dx12", sc[0].launch_options)
        self.assertEqual(w._existing.count(), 1)
        with mock.patch.object(m, "_proc_running", return_value=False), \
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
            with mock.patch.object(m, "lgtune_mode", return_value="polkit"):
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
            with mock.patch.object(m, "lgtune_mode", return_value="setuid"):
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
            with mock.patch.object(m, "_proc_running", return_value=False):
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

    def test_drift_add_to_user_catalog(self):
        m = self.m
        w = m.UpstreamDriftWidget()
        self.assertEqual(w._filter.itemText(1), "Fork-only")
        self.assertFalse(w._src_checks["dxvk-sarek"].isChecked())            # forks are opt-in
        self.assertTrue(w._src_checks["dxvk"].isChecked())
        row = {"src": "DXVK-Sarek", "kind": "dxvk.conf key", "name": "ddraw.guiKey", "status": "fork",
               "where": "src/ddraw/ddraw_options.cpp:3", "note": "", "base": "dxvk", "fork_cat": "DXVK Forks",
               "blob_url": "https://github.com/pythonlover02/DXVK-Sarek/blob/main/x#L3",
               "ctx": {"rel": "src/ddraw/ddraw_options.cpp", "line": 3,
                       "text": 'x = config.getOption<bool>("ddraw.guiKey", true);'}}
        w._rows = [row]
        w._filter.setCurrentIndex(1)
        self.assertEqual(w._table.rowCount(), 1)
        self.assertEqual(w._table.item(0, 3).text(), "FORK")
        self.assertFalse(w._btn_add.isEnabled())
        w._table.selectRow(0)
        self.pump()
        self.assertTrue(w._btn_add.isEnabled())
        seen = {}

        def run(dlg):                                    # what the user does: check the prefilled form, press Save
            seen.update(name=dlg._forms[0]["name"], type=dlg._type.currentText(), opts=dlg._opts.text(),
                        group=dlg._group.currentText(), desc=dlg._desc.toPlainText())
            dlg._desc.setPlainText("[DXVK-Sarek] edited")
            dlg._accept()
            return dlg.result()

        with mock.patch.object(m.DriftAddDialog, "exec", run):
            w._add_selected()
        self.assertEqual((seen["type"], seen["opts"], seen["group"]),
                         ("enum", "True, False", "🗿 DirectDraw (d7vk/Sarek)"))
        path = m.user_catalog_path()
        self.assertEqual(path, HOME / ".config/drstool/user_catalog.json")
        rec = json.loads(path.read_text())["dxvk_conf_keys"][0]
        self.assertEqual((rec["key"], rec["kind"], rec["opt"], rec["desc"]),
                         ("ddraw.guiKey", "enum", ["True", "False"], "[DXVK-Sarek] edited"))
        self.assertEqual(w._table.rowCount(), 0)                             # saved rows count as reviewed
        self.assertIn("restart", w._status.text())
        # manage dialog: lists it, 'Copy as Python' works, delete removes it
        dlg = m.UserCatalogDialog()
        self.assertEqual(dlg._table.item(0, 1).text(), "ddraw.guiKey")
        dlg._table.selectRow(0)
        dlg._copy_py()
        self.assertIn("('ddraw.guiKey', 'enum', ['True', 'False']", m.QApplication.clipboard().text())
        with mock.patch.object(m.QMessageBox, "question", return_value=m.QMessageBox.Yes):
            dlg._delete()
        self.assertEqual(json.loads(path.read_text())["dxvk_conf_keys"], [])
        # user-defined source: checkbox appears in the Forks row and the source is scannable
        with mock.patch.object(m.DriftSourceDialog, "exec", lambda self: (self._repo.setText("me/my-dxvk"),
                                                                           self._accept(), self.result())[2]):
            w._add_source()
        self.assertIn("user-me-my-dxvk", w._src_checks)
        self.assertEqual(json.loads(path.read_text())["sources"][0]["repo"], "me/my-dxvk")
        self.assertEqual(next(s for s in m.DRIFT_SOURCES if s["key"] == "user-me-my-dxvk")["base"], "dxvk")
        dlg.deleteLater()
        w.deleteLater()

    def test_share_code_flags_shell_env(self):
        from drstool_core import share_code
        for k in ("BASH_ENV", "ENV", "SHELLOPTS", "IFS"):
            self.assertIn(k, share_code.SENSITIVE_ENV)


if __name__ == "__main__":
    unittest.main()
