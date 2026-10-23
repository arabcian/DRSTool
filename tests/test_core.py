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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drstool_core import dll_ops, game_scan, pe_version, share_code, steam_shortcuts as ss  # noqa: E402


def fake_dll(path: Path, ver=(310, 2, 1, 0), filler=b"") -> Path:
    """Minimal 'PE' with a VS_FIXEDFILEINFO block."""
    ms = (ver[0] << 16) | ver[1]
    ls = (ver[2] << 16) | ver[3]
    body = b"MZ" + b"\0" * 62 + filler + b"\xbd\x04\xef\xfe" + struct.pack("<III", 0x00010000, ms, ls)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body + b"\0" * 64)
    return path


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
            self.assertEqual(len(dll_ops.list_backups("k", td / "bk")), dll_ops.KEEP_BACKUPS)


if __name__ == "__main__":
    unittest.main()
