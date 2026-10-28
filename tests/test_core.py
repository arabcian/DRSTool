"""Unit tests for drstool_core (no Qt, no network).

    python3 -m unittest discover -s tests -v
"""

import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drstool_core import catalog, dll_ops, game_scan, pe_version, share_code, steam_shortcuts as ss, support_bundle, validate, winetricks  # noqa: E402


def fake_dll(path: Path, ver=(310, 2, 1, 0), filler=b"") -> Path:
    """Minimal 'PE' with a VS_FIXEDFILEINFO block."""
    ms = (ver[0] << 16) | ver[1]
    ls = (ver[2] << 16) | ver[3]
    body = b"MZ" + b"\0" * 62 + filler + b"\xbd\x04\xef\xfe" + struct.pack("<III", 0x00010000, ms, ls)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body + b"\0" * 64)
    return path


class WinetricksPlanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _prefix(self, *parts) -> Path:
        p = self.root.joinpath(*parts)
        (p / "drive_c" / "windows").mkdir(parents=True)
        return p

    def test_lutris_prefix_uses_winetricks_with_wineprefix(self):
        pfx = self._prefix("lutris", "game")
        with mock.patch.object(winetricks.shutil, "which", lambda n: f"/usr/bin/{n}"):
            p = winetricks.plan(pfx, ["vcrun2022", "xact"])
        self.assertEqual(p.tool, "winetricks")
        self.assertEqual(p.argv, ["/usr/bin/winetricks", "-q", "vcrun2022", "xact"])
        self.assertEqual(p.env, {"WINEPREFIX": str(pfx)})

    def test_steam_prefix_uses_protontricks_with_appid(self):
        pfx = self._prefix("steamapps", "compatdata", "1245620", "pfx")
        with mock.patch.object(winetricks.shutil, "which", lambda n: f"/usr/bin/{n}"):
            p = winetricks.plan(pfx, ["d3dx9"])
        self.assertEqual(p.tool, "protontricks")
        self.assertEqual(p.argv, ["/usr/bin/protontricks", "1245620", "-q", "d3dx9"])
        self.assertEqual(p.env, {})

    def test_unknown_component_is_rejected(self):
        pfx = self._prefix("game")
        with mock.patch.object(winetricks.shutil, "which", lambda n: f"/usr/bin/{n}"):
            with self.assertRaises(ValueError):
                winetricks.plan(pfx, ["vcrun2022; rm -rf ~"])

    def test_folder_without_drive_c_is_rejected(self):
        with self.assertRaises(ValueError):
            winetricks.plan(self.root / "nothing", ["xact"])

    def test_missing_tool_is_reported(self):
        pfx = self._prefix("game")
        with mock.patch.object(winetricks.shutil, "which", lambda n: None):
            with self.assertRaises(RuntimeError):
                winetricks.plan(pfx, ["xact"])


class ValidateTests(unittest.TestCase):
    def test_int(self):
        self.assertEqual(validate.check_int(""), "")
        self.assertEqual(validate.check_int("60"), "")
        self.assertEqual(validate.check_int(" 60 "), "")
        self.assertNotEqual(validate.check_int("6o"), "")
        self.assertNotEqual(validate.check_int("1.5"), "")
        self.assertNotEqual(validate.check_int("-1"), "")
        self.assertNotEqual(validate.check_int("3000000000"), "")

    def test_hex(self):
        self.assertEqual(validate.check_hex(""), "")
        self.assertEqual(validate.check_hex("0x1F"), "")
        self.assertEqual(validate.check_hex("DEADBEEF"), "")
        self.assertNotEqual(validate.check_hex("0xZZ"), "")
        self.assertNotEqual(validate.check_hex("123456789"), "")

    def test_dir(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "file.txt"
            f.write_text("x")
            self.assertEqual(validate.check_dir(""), "")
            self.assertEqual(validate.check_dir(d), "")
            self.assertNotEqual(validate.check_dir(str(Path(d) / "missing")), "")
            self.assertNotEqual(validate.check_dir(str(f)), "")


class ShareCodeTests(unittest.TestCase):
    PROF = {"settings": {"0x10E41DF3": "0x1"}, "arch": "ad100",
            "env_vars": {"DXVK_HUD": "fps", "PROTON_ENABLE_NVAPI": "1"},
            "gamescope_enabled": True, "gamescope_flags": {"-W": "2560", "mangohud": ""}}

    def test_roundtrip(self):
        code = share_code.encode("Cyberpunk", self.PROF)
        self.assertTrue(code.startswith(share_code.PREFIX))
        d = share_code.decode(code)
        self.assertEqual(d.name, "Cyberpunk")
        self.assertEqual(d.profile, self.PROF)
        self.assertEqual(d.flagged, {})

    def test_wrapped_inside_text(self):
        code = share_code.encode("x", self.PROF)
        post = f"Try this:\n\n{share_code.wrap(code, 20)}\nHave fun!\n"
        self.assertEqual(share_code.decode(post).profile, self.PROF)

    def test_home_is_redacted(self):
        p = dict(self.PROF, env_vars={"DXVK_LOG_PATH": "/home/alice/logs"})
        d = share_code.decode(share_code.encode("x", p, home="/home/alice"))
        self.assertEqual(d.profile["env_vars"]["DXVK_LOG_PATH"], "~/logs")
        p2 = dict(self.PROF, env_vars={"A": "/home/alice/a:/home/alice/b", "B": "x~/y", "C": "/home/alice"})
        d = share_code.decode(share_code.encode("x", p2, home="/home/alice"), home="/home/bob/")
        self.assertEqual(d.profile["env_vars"], {"A": "/home/bob/a:/home/bob/b", "B": "x~/y", "C": "/home/bob"})

    def test_flagged_and_sanitised(self):
        p = {"env_vars": {"LD_PRELOAD": "/tmp/x.so", "A": "$(rm -rf ~)", "bad name": "1", "OK": "1"},
             "settings": {"0x1": {"nested": 1}}}
        d = share_code.decode(share_code.encode("x", p))
        self.assertIn("LD_PRELOAD", d.flagged)
        self.assertIn("A", d.flagged)
        self.assertNotIn("bad name", d.profile["env_vars"])
        self.assertEqual(d.profile["settings"], {})
        self.assertEqual(d.profile_without_flagged()["env_vars"], {"OK": "1"})

    def test_errors(self):
        code = share_code.encode("x", self.PROF)
        for bad in ("", "hello", share_code.PREFIX + "!!!", code[:-6], share_code.PREFIX + "eJw"):
            with self.assertRaises(share_code.ShareCodeError):
                share_code.decode(bad)
        with self.assertRaises(share_code.ShareCodeError):
            share_code.encode("x", {})

    def test_decompression_bomb(self):
        import base64
        import zlib
        raw = json.dumps({"v": 1, "name": "x", "profile": {"env_vars": {"A": "x" * 2_000_000}}}).encode()
        code = share_code.PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")
        with self.assertRaises(share_code.ShareCodeError):
            share_code.decode(code)

    def test_newer_format(self):
        import base64
        import zlib
        raw = json.dumps({"v": 99, "profile": self.PROF}).encode()
        code = share_code.PREFIX + base64.urlsafe_b64encode(zlib.compress(raw)).decode()
        with self.assertRaisesRegex(share_code.ShareCodeError, "newer"):
            share_code.decode(code)


class VdfTests(unittest.TestCase):
    def sample(self) -> bytes:
        root = ss.KV([("shortcuts", ss.KV())])
        ss.upsert_shortcut(root, "Game A", "/games/a/run.sh", "", "FOO=1 %command%")
        ss.upsert_shortcut(root, "Game B", "/games/b/B.exe", "/games/b", "")
        e = root.get("shortcuts").get("1")
        e.items.append(("SomethingNew", (ss.T_U64, 2 ** 40)))
        e.items.append(("Ratio", (ss.T_FLOAT, 0.5)))
        return ss.dump(root)

    def test_roundtrip_bytes(self):
        data = self.sample()
        self.assertEqual(ss.dump(ss.parse(data)), data)
        self.assertEqual(data[-2:], b"\x08\x08")

    def test_appid(self):
        # Reference value from Steam ROM Manager's algorithm
        exe, name = '"/usr/bin/foo"', "Foo"
        import zlib
        expect = (zlib.crc32((exe + name).encode()) & 0xFFFFFFFF) | 0x80000000
        self.assertEqual(ss.shortcut_appid(exe, name), expect)
        self.assertTrue(ss.shortcut_appid(exe, name) & 0x80000000)
        self.assertEqual(ss.to_unsigned32(ss.to_signed32(expect)), expect)

    def test_upsert_update_and_remove(self):
        root = ss.parse(self.sample())
        aid, created = ss.upsert_shortcut(root, "Game A", "/games/a/run.sh", "", "BAR=2 %command%")
        self.assertFalse(created)
        lst = ss.list_shortcuts(root)
        self.assertEqual(len(lst), 2)
        a = next(s for s in lst if s.name == "Game A")
        self.assertEqual(a.launch_options, "BAR=2 %command%")
        self.assertEqual(a.start_dir, "/games/a")
        self.assertTrue(a.is_drstool)
        self.assertEqual(a.appid, aid)
        self.assertTrue(ss.remove_shortcut(root, aid))
        lst = ss.list_shortcuts(root)
        self.assertEqual([s.name for s in lst], ["Game B"])
        self.assertEqual(lst[0].index, "0")                 # renumbered
        # unknown field types survive
        self.assertEqual(root.get("shortcuts").get("0").get("SomethingNew"), 2 ** 40)

    def test_lowercase_legacy_keys(self):
        legacy = ss.KV([("shortcuts", ss.KV([("0", ss.KV([
            ("appname", (ss.T_STR, "Old")), ("exe", (ss.T_STR, '"/x/old"')),
            ("StartDir", (ss.T_STR, '"/x"'))]))]))])
        root = ss.parse(ss.dump(legacy))
        s = ss.list_shortcuts(root)[0]
        self.assertEqual((s.name, s.exe), ("Old", "/x/old"))
        _aid, created = ss.upsert_shortcut(root, "Old", "/x/old", "", "-x")
        self.assertFalse(created)

    def test_bad_files(self):
        for bad in (b"\x00shortcuts\x00\x01k\x00noterm", b"\x00a\x00\x09", b"\x00a\x00\x02k\x00\x01"):
            with self.assertRaises(ss.VdfError):
                ss.parse(bad)

    def test_load_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = ss.load(Path(td) / "shortcuts.vdf")
            self.assertEqual(ss.list_shortcuts(root), [])


class PeVersionTests(unittest.TestCase):
    def test_read(self):
        with tempfile.TemporaryDirectory() as td:
            p = fake_dll(Path(td) / "nvngx_dlss.dll", (310, 4, 0, 0), filler=b"\xbd\x04\xef\xfe" + b"\0" * 12)
            self.assertEqual(pe_version.read_version(p), (310, 4, 0, 0))
            q = Path(td) / "x.dll"
            q.write_bytes(b"not a pe" * 20)
            self.assertIsNone(pe_version.read_version(q))

    def test_compare(self):
        self.assertEqual(pe_version.compare((310, 2, 1, 0), pe_version.parse_version("310.2.1")), 0)
        self.assertEqual(pe_version.compare((3, 7), (3, 10)), -1)
        self.assertEqual(pe_version.parse_version("v2.7.30-rc1"), (2, 7, 30))


class GameScanTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.h = Path(self.td.name)
        self._xdg = {k: os.environ.pop(k, None) for k in ("XDG_DATA_HOME", "XDG_CONFIG_HOME")}

    def tearDown(self):
        for k, v in self._xdg.items():
            if v is not None:
                os.environ[k] = v
        self.td.cleanup()

    def _prefix(self, p: Path) -> Path:
        (p / "drive_c" / "windows" / "system32").mkdir(parents=True)
        return p

    def test_steam(self):
        root = self.h / ".local/share/Steam"
        apps = root / "steamapps"
        (apps / "common" / "Cool Game" / "bin").mkdir(parents=True)
        (apps / "appmanifest_42.acf").write_text('"AppState"\n{\n "appid" "42"\n "name" "Cool Game"\n'
                                                 ' "installdir" "Cool Game"\n}\n')
        (apps / "appmanifest_1.acf").write_text('"AppState"{ "appid" "1" "name" "Proton 9.0"'
                                                ' "installdir" "Proton 9.0" }')
        (apps / "common" / "Proton 9.0").mkdir()
        self._prefix(apps / "compatdata" / "42" / "pfx")
        fake_dll(apps / "common/Cool Game/bin/nvngx_dlss.dll", (310, 1, 0, 0))
        fake_dll(apps / "common/Cool Game/bin/sl.interposer.dll", (2, 7, 3, 0))
        fake_dll(apps / "common/Cool Game/bin/sl.dlss.dll", (2, 7, 3, 0))
        (apps / "common/Cool Game/EasyAntiCheat").mkdir()
        games = game_scan.scan_steam(self.h)
        self.assertEqual([g.name for g in games], ["Cool Game"])
        g = games[0]
        self.assertEqual(g.prefix_manager, "Proton")
        found = game_scan.find_dlls(g)
        self.assertEqual([d.kind for d in found.dlls], ["dlss", "sl_sdk_0"])
        self.assertEqual(found.dlls[0].version_str, "310.1.0")
        self.assertEqual(len(found.dlls[1].group), 2)
        self.assertEqual(found.anti_cheat, ["EasyAntiCheat"])

    def test_lutris_db_and_yaml(self):
        base = self.h / ".local/share/lutris"
        (base / "games").mkdir(parents=True)
        gdir = self.h / "Games/witcher"
        exe = gdir / "Witcher3/bin/x64/witcher3.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        pfx = self._prefix(self.h / "Games/witcher/prefix")
        (base / "games/witcher-1.yml").write_text(f"game:\n  exe: {exe}\n  prefix: {pfx}\nsystem: {{}}\n")
        con = sqlite3.connect(base / "pga.db")
        con.execute("CREATE TABLE games (id INTEGER, name TEXT, slug TEXT, runner TEXT, directory TEXT,"
                    " configpath TEXT, installed INTEGER)")
        con.execute("INSERT INTO games VALUES (1, 'The Witcher 3', 'witcher', 'wine', ?, 'witcher-1', 1)",
                    (str(gdir),))
        con.execute("INSERT INTO games VALUES (2, 'Gone', 'gone', 'wine', '', 'gone-2', 0)")
        con.commit()
        con.close()
        games = game_scan.scan_lutris(self.h)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].name, "The Witcher 3")
        self.assertEqual(games[0].prefix, pfx)
        self.assertEqual(games[0].folders, [gdir])

    def test_heroic_and_bottles(self):
        hr = self.h / ".config/heroic"
        inst = self.h / "Games/Heroic/Control"
        inst.mkdir(parents=True)
        (hr / "legendaryConfig/legendary").mkdir(parents=True)
        (hr / "legendaryConfig/legendary/installed.json").write_text(
            json.dumps({"Calluna": {"install_path": str(inst), "title": "Control"}}))
        (hr / "GamesConfig").mkdir()
        pfx = self._prefix(self.h / "Games/Heroic/Prefixes/Control")
        (hr / "GamesConfig/Calluna.json").write_text(json.dumps({"Calluna": {"winePrefix": str(pfx),
                                                                            "wineVersion": {"type": "wine"}}}))
        b = self.h / ".local/share/bottles/bottles/gaming"
        self._prefix(b)
        (b / "bottle.yml").write_text("Name: Gaming Bottle\nExternal_Programs: {}\n")
        hg = game_scan.scan_heroic(self.h)
        self.assertEqual((hg[0].name, hg[0].prefix), ("Control", pfx))
        bg = game_scan.scan_bottles(self.h)
        self.assertEqual(bg[0].name, "Gaming Bottle")
        self.assertEqual(bg[0].prefix, b)

    def test_scan_all_survives_broken_launcher(self):
        hr = self.h / ".config/heroic/legendaryConfig/legendary"
        hr.mkdir(parents=True)
        (hr / "installed.json").write_text("{ not json")
        custom = self.h / "MyGame"
        custom.mkdir()
        res = game_scan.scan_all(self.h, [str(custom), str(self.h / "missing")])
        self.assertEqual([g.launcher for g in res.games], ["Custom"])
        self.assertTrue(any("missing" in e for e in res.errors))

    def test_symlinks_not_followed(self):
        real = self.h / "real"
        fake_dll(real / "nvngx_dlss.dll")
        game = self.h / "game"
        game.mkdir()
        (game / "linkdir").symlink_to(real, target_is_directory=True)
        (game / "nvngx_dlssg.dll").symlink_to(real / "nvngx_dlss.dll")
        found = game_scan.find_dlls(game_scan.GameEntry("Custom", "k", "g", [game]))
        self.assertEqual(found.dlls, [])

    def test_streamline_version_is_that_of_the_files_ngx_can_update(self):
        # NGX's Streamline package holds the plugins but no sl.interposer.dll, so an older
        # interposer must not keep the group "outdated" once its plugins are on the target.
        game = self.h / "game"
        fake_dll(game / "sl.interposer.dll", (2, 14, 1, 0))
        fake_dll(game / "sl.common.dll", (2, 14, 16, 0))
        fake_dll(game / "sl.dlss.dll", (2, 14, 16, 0))
        found = game_scan.find_dlls(game_scan.GameEntry("Custom", "k", "g", [game]))
        self.assertEqual(found.of(game_scan.SL_FEATURE)[0].version, (2, 14, 16, 0))
        # without sl.common.dll the interposer is all there is to go by
        solo = self.h / "solo"
        fake_dll(solo / "sl.interposer.dll", (2, 9, 0, 0))
        found = game_scan.find_dlls(game_scan.GameEntry("Custom", "k2", "g2", [solo]))
        self.assertEqual(found.of(game_scan.SL_FEATURE)[0].version, (2, 9, 0, 0))


class DllOpsTests(unittest.TestCase):
    def test_replace_and_restore(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            game = td / "game"
            old = fake_dll(game / "nvngx_dlss.dll", (310, 1, 0, 0))
            newsrc = fake_dll(td / "src/nvngx_dlss.dll", (310, 4, 0, 0))
            os.chmod(old, 0o640)
            missing = game / "nvngx_dlssd.dll"
            res = dll_ops.replace_files("steam:1", "G", [(old, newsrc, "SR 310.4"), (missing, newsrc, "RR")],
                                        root=td / "bk")
            self.assertEqual(res.replaced, [old])
            self.assertEqual(len(res.skipped), 1)
            self.assertFalse(missing.exists())
            self.assertEqual(pe_version.read_version(old), (310, 4, 0, 0))
            self.assertEqual(os.stat(old).st_mode & 0o777, 0o640)
            # same version again -> skipped, no new backup
            res2 = dll_ops.replace_files("steam:1", "G", [(old, newsrc, "SR")], root=td / "bk")
            self.assertIsNone(res2.backup_dir)
            backups = dll_ops.list_backups("steam:1", td / "bk")
            self.assertEqual(len(backups), 1)
            n, notes = dll_ops.restore(backups[0][0])
            self.assertEqual(n, 1, notes)
            self.assertEqual(pe_version.read_version(old), (310, 1, 0, 0))
            n, notes = dll_ops.restore(backups[0][0])
            self.assertEqual(n, 0)
            self.assertIn("already the original", notes[0])

    def test_restore_skips_files_changed_by_game(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            old = fake_dll(td / "g/nvngx_dlss.dll", (1, 0, 0, 0))
            res = dll_ops.replace_files("k", "G", [(old, fake_dll(td / "n.dll", (2, 0, 0, 0)), "x")],
                                        root=td / "bk")
            fake_dll(old, (3, 0, 0, 0))                     # game patched itself
            n, notes = dll_ops.restore(res.backup_dir)
            self.assertEqual(n, 0)
            self.assertIn("changed since", notes[0])

    def test_refuses_symlink_and_non_pe(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            real = fake_dll(td / "real.dll")
            link = td / "g/nvngx_dlss.dll"
            link.parent.mkdir()
            link.symlink_to(real)
            junk = td / "junk.dll"
            junk.write_text("hello")
            tgt = fake_dll(td / "g/nvngx_dlssg.dll")
            res = dll_ops.replace_files("k", "G", [(link, real, "x"), (tgt, junk, "y")], root=td / "bk")
            self.assertEqual(res.replaced, [])
            self.assertEqual(len(res.skipped), 2)

    def test_prune(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for i in range(dll_ops.KEEP_BACKUPS + 3):
                d = td / "bk" / "k" / f"2026010{i:02d}"
                (d / "files").mkdir(parents=True)
                (d / "manifest.json").write_text('{"entries": []}')
            dll_ops.prune("k", td / "bk")
            left = dll_ops.list_backups("k", td / "bk")
            self.assertEqual(len(left), dll_ops.KEEP_BACKUPS)
            # the oldest backup (the game's original DLLs) is never pruned
            self.assertEqual(left[-1][0].name, "202601000")


class CatalogTests(unittest.TestCase):
    """The catalog lives in drstool_core/data/*.json; these catch a broken hand edit early."""

    def test_counts_and_loading(self):
        self.assertEqual(len(catalog.gpu_archs()), 9)
        self.assertEqual(len(catalog.drs_settings()), 118)
        self.assertEqual(len(catalog.gamescope_flags()), 66)
        self.assertEqual(len(catalog.dxvk_config_keys()), 70)
        self.assertEqual(sum(len(v) for v in catalog.env_var_groups().values()), 289)

    def test_drs_settings(self):
        sett = catalog.drs_settings()
        ids = [s.id.upper() for s in sett]
        self.assertEqual(len(ids), len(set(ids)), "duplicate DRS setting id")
        for s in sett:
            self.assertRegex(s.id, r"^0x[0-9A-Fa-f]{1,8}$", s.name)
            self.assertIn(s.type, {"enum", "numeric", "hex", "hex-preset", "bitfield"}, s.id)
            if s.type == "enum":
                self.assertTrue(s.values, f"{s.id} {s.name}: enum without values")
            if s.type == "bitfield":
                self.assertTrue(s.bits, f"{s.id} {s.name}: bitfield without bits")

    def test_env_vars(self):
        groups = catalog.env_var_groups()
        names = [e.name for v in groups.values() for e in v]
        self.assertEqual(len(names), len(set(names)), "duplicate env var name")
        for e in (e for v in groups.values() for e in v):
            if e.vtype in ("enum", "flags"):
                self.assertTrue(e.options, f"{e.name}: {e.vtype} without options")
            for ex in e.exclusive:
                self.assertIn(ex, e.options, f"{e.name}: exclusive entry not in options")

    def test_gamescope_flags(self):
        flags = catalog.gamescope_flags()
        self.assertEqual(len({f.flag for f in flags}), len(flags), "duplicate gamescope flag")
        for f in flags:
            self.assertIn(f.kind, {"toggle", "int", "enum", "string"}, f.flag)
            if f.kind == "enum":
                self.assertTrue(f.options, f.flag)

    def test_gpu_archs_and_config_keys(self):
        codes = [a.code for a in catalog.gpu_archs()]
        self.assertEqual(len(codes), len(set(codes)))
        keys = [k[0] for k in catalog.dxvk_config_keys()]
        self.assertEqual(len(keys), len(set(keys)))
        for k in catalog.dxvk_config_keys():
            self.assertEqual(len(k), 4, k[0])
            if k[1] == "enum":
                self.assertIsInstance(k[2], list, k[0])
        self.assertTrue(all(isinstance(v, str) and v for v in catalog.vkd3d_config_descs().values()))


class PackagingTests(unittest.TestCase):
    """pyproject.toml has to ship every package and the catalog data files."""

    def test_pyproject_lists_packages_and_catalog_data(self):
        import tomllib
        root = Path(__file__).resolve().parents[1]
        cfg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
        on_disk = {p.parent.name for p in root.glob("*/__init__.py") if p.parent.name.startswith("drstool_")}
        self.assertEqual(set(cfg["packages"]), on_disk)
        self.assertTrue(list((root / "drstool_core" / "data").glob("*.json")))
        self.assertEqual(cfg["package-data"]["drstool_core"], ["data/*.json"])


class GuiLayoutTests(unittest.TestCase):
    """drstool_gui modules import only downwards (no import cycles), checked without Qt."""

    @staticmethod
    def _imports():
        import ast
        pkg = Path(__file__).resolve().parents[1] / "drstool_gui"
        graph = {}
        for f in sorted(pkg.glob("*.py")):
            if f.stem == "__init__":
                continue
            deps = set()
            for node in ast.parse(f.read_text(encoding="utf-8")).body:      # top level only: lazy and TYPE_CHECKING imports are skipped
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    deps.update([node.module] if node.module else [a.name for a in node.names])
            graph[f.stem] = deps
        return graph

    def test_no_import_cycles(self):
        graph = self._imports()
        state = {}

        def visit(m, path):
            self.assertNotEqual(state.get(m), 1, "import cycle: " + " -> ".join(path + [m]))
            if state.get(m) == 2:
                return
            state[m] = 1
            for d in sorted(graph[m]):
                self.assertIn(d, graph, f"{m} imports unknown module {d}")
                visit(d, path + [m])
            state[m] = 2
        for m in graph:
            visit(m, [])

    def test_bottom_layers(self):
        graph = self._imports()
        self.assertEqual(graph["base"], set())
        self.assertLessEqual(graph["theme"], {"base"})
        self.assertLessEqual(graph["widgets"], {"base", "theme"})



class GameLinkTests(unittest.TestCase):
    def setUp(self):
        from drstool_core import game_links
        self.gl = game_links
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "game_links.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fingerprint_is_order_independent(self):
        a = {("Env", "A"): "1", ("DRS", "0x1"): "0x2"}
        b = {("DRS", "0x1"): "0x2", ("Env", "A"): "1"}
        self.assertEqual(self.gl.fingerprint(a), self.gl.fingerprint(b))
        self.assertNotEqual(self.gl.fingerprint(a), self.gl.fingerprint({("Env", "A"): "2"}))

    def test_record_outdated_rename_drop(self):
        s = self.gl.LinkStore(self.path)
        tgt = {"kind": "steam", "key": "42", "path": "/x/localconfig.vdf", "title": "Cool"}
        s.record("steam:42", name="Cool", launcher="Steam", target=tgt, profile="P", fp="aaa")
        s.record("lutris:rpg-1", name="RPG", launcher="Lutris", target={"kind": "lutris"}, profile="P", fp="bbb")
        s.record("steam:7", name="Other", launcher="Steam", target=tgt, profile="Q", fp="aaa")
        # a second store (another window) sees the same file
        s2 = self.gl.LinkStore(self.path)
        self.assertEqual(s2.games_for("P"), ["steam:42", "lutris:rpg-1"])
        self.assertEqual(s2.counts(), {"P": 2, "Q": 1})
        self.assertEqual(s2.outdated("P", "aaa"), ["lutris:rpg-1"])
        self.assertEqual(s.rename_profile("P", "P2"), 2)
        s2.reload()
        self.assertEqual(len(s2.games_for("P2")), 2)
        self.assertEqual(s.drop_profile("P2"), 2)
        self.assertIsNone(s.get("steam:42")["profile"])
        self.assertEqual(s.get("steam:42")["target"], tgt)  # the last write is still known

    def test_assign_and_extras(self):
        s = self.gl.LinkStore(self.path)
        s.assign("heroic:abc", "P", name="H", launcher="Heroic")
        self.assertEqual(s.get("heroic:abc")["fp"], "")    # linked but never written
        self.assertEqual(s.outdated("P", "zzz"), ["heroic:abc"])
        s.set_field("heroic:abc", "appid", "1091500")
        s.assign("heroic:abc", None)
        e = s.get("heroic:abc")
        self.assertNotIn("profile", e)
        self.assertEqual(e["appid"], "1091500")
        s.set_field("heroic:abc", "appid", "")
        self.assertEqual(s.get("heroic:abc"), {"name": "H", "launcher": "Heroic"})
        self.path.write_text("not json")                  # a broken file never raises
        self.assertEqual(self.gl.LinkStore(self.path).all(), {})


class GameInfoTests(unittest.TestCase):
    def setUp(self):
        from drstool_core import game_info
        self.gi = game_info
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_steam_runtime_and_logs(self):
        h = self.home
        apps = h / ".local/share/Steam/steamapps"
        (apps / "common/Cool/bin").mkdir(parents=True)
        (apps / "common/Cool/bin/Cool-Win64.exe").write_bytes(b"MZ")
        (apps / "common/Cool/bin/unins000.exe").write_bytes(b"MZ")
        (apps / "common/Cool/bin/Cool-Win64_d3d11.log").write_text("err: x\n")
        pfx = apps / "compatdata/42/pfx"
        (pfx / "drive_c/windows/system32").mkdir(parents=True)
        (apps / "compatdata/42/version").write_text("GE-Proton10-1\n")
        (h / ".local/share/Steam/config").mkdir(parents=True)
        (h / ".local/share/Steam/config/config.vdf").write_text(
            '"InstallConfigStore"\n{\n "Software"\n {\n  "Valve"\n  {\n   "Steam"\n   {\n'
            '    "CompatToolMapping"\n    {\n     "7"\n     {\n      "name" "proton_9"\n     }\n'
            '     "42"\n     {\n      "name" "GE-Proton10-1"\n      "config" ""\n     }\n    }\n'
            '   }\n  }\n }\n}\n')
        g = game_scan.GameEntry("Steam", "steam:42", "Cool", [apps / "common/Cool"], pfx, "Proton")
        rt = self.gi.runtime_info(g, h)
        self.assertEqual((rt["runner"], rt["configured"], rt["last_used"]),
                         ("Proton", "GE-Proton10-1", "GE-Proton10-1"))
        files = self.gi.game_files(g)
        self.assertEqual(files["exes"], ["Cool-Win64"])
        (h / "steam-42.log").write_text("x")
        (h / "steam-43.log").write_text("x")
        logs = self.gi.find_logs(g, [h], files["dxvk_logs"], files["exes"])
        self.assertEqual({p.name for p in logs}, {"steam-42.log", "Cool-Win64_d3d11.log"})

    def test_lutris_runtime_and_frametime(self):
        h = self.home
        lut = h / ".local/share/lutris/games"
        lut.mkdir(parents=True)
        (lut / "rpg-1.yml").write_text("game:\n  exe: /x/rpg.exe\nwine:\n  version: wine-ge-8-26\n"
                                       "system:\n  runner: wine\n")
        g = game_scan.GameEntry("Lutris", "lutris:rpg-1", "The RPG", [h], None, "")
        self.assertEqual(self.gi.runtime_info(g, h)["configured"], "wine-ge-8-26")
        mh = h / "mh"
        mh.mkdir()
        for n in ("rpg_2026-10-01_10-00-00.csv", "rpg_2026-10-01_10-00-00_summary.csv",
                  "TheRPG_2026-10-02_10-00-00.csv", "other_2026-10-02_10-00-00.csv"):
            (mh / n).write_text("fps,frametime\n")
        os.utime(mh / "TheRPG_2026-10-02_10-00-00.csv", (2e9, 2e9))
        got = [p.name for p in self.gi.find_frametime_logs(g, [mh], ["rpg"])]
        self.assertEqual(got, ["TheRPG_2026-10-02_10-00-00.csv", "rpg_2026-10-01_10-00-00.csv"])
        env = {"MANGOHUD_CONFIG": "fps,output_folder=~/caps,log_duration=30"}
        self.assertIn(Path(os.path.expanduser("~/caps")), self.gi.mangohud_output_dirs(env, h))

    def test_read_tail(self):
        p = self.home / "big.log"
        p.write_text("".join(f"line {i}\n" for i in range(1000)))
        tail = self.gi.read_tail(p, 100)
        self.assertEqual(tail[-1], "line 999")
        self.assertTrue(all(ln.startswith("line ") for ln in tail))


class SupportBundleTests(unittest.TestCase):
    def test_redact_hides_home_user_and_host(self):
        home = str(Path.home())
        with mock.patch.dict(os.environ, {"USER": "tester_x"}), \
                mock.patch("drstool_core.support_bundle.socket.gethostname", return_value="mybox"):
            out = support_bundle.redact(f"{home}/games/x.exe /home/tester_x/a mybox:1 tester_xy")
        self.assertEqual(out, "~/games/x.exe /home/<user>/a <host>:1 tester_xy")

    def test_bundle_contains_anonymised_log_and_profiles(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": td, "USER": "tester_x"}):
                (Path(td) / "drstool").mkdir()
                (Path(td) / "drstool" / "debug.log").write_text("Traceback in /home/tester_x/app.py\n")
                dest = Path(td) / "report.zip"
                prof = {"Cool": {"env_vars": {"WINEPREFIX": f"{Path.home()}/pfx"}}}
                support_bundle.build(dest, prof, ["nvidia driver: 595.58.03"])
            with zipfile.ZipFile(dest) as zf:
                self.assertEqual(set(zf.namelist()), {"system.txt", "profiles.json", "log/debug.log"})
                log = zf.read("log/debug.log").decode()
                profiles = zf.read("profiles.json").decode()
                system = zf.read("system.txt").decode()
        self.assertNotIn("tester_x", log)
        self.assertIn("/home/<user>/app.py", log)
        self.assertIn("~/pfx", profiles)
        self.assertIn("nvidia driver: 595.58.03", system)


if __name__ == "__main__":
    unittest.main()
