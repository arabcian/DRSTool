"""Regressions for metadata-cached game inventories (requires no Qt)."""

import os
import tempfile
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drstool_core import game_info, game_scan


class GameFileInventoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "Game"
        (self.root / "Binaries").mkdir(parents=True)
        (self.root / "Binaries" / "Game.exe").write_bytes(b"MZ")
        self.game = game_scan.GameEntry("Custom", "perf:test-inventory", "Test", [self.root])
        game_info.clear_game_files_cache(self.game.key)
        self.addCleanup(game_info.clear_game_files_cache, self.game.key)

    def test_warm_lookup_skips_full_walk(self):
        self.assertEqual(game_info.game_files(self.game)["exes"], ["Game"])
        with mock.patch.object(game_info.os, "walk", side_effect=AssertionError("unnecessary walk")):
            self.assertEqual(game_info.game_files(self.game)["exes"], ["Game"])

    def test_added_nested_file_invalidates_metadata_cache(self):
        game_info.game_files(self.game)
        (self.root / "Binaries" / "New.exe").write_bytes(b"MZ")
        self.assertEqual(game_info.game_files(self.game)["exes"], ["Game", "New"])
        (self.root / "Binaries" / "New.exe").rename(self.root / "Binaries" / "Different.exe")
        self.assertCountEqual(game_info.game_files(self.game)["exes"], ["Game", "Different"])
        (self.root / "Binaries" / "Different.exe").unlink()
        self.assertEqual(game_info.game_files(self.game)["exes"], ["Game"])

    def test_cached_result_never_exposes_shared_mutable_lists(self):
        initial = game_info.game_files(self.game)
        initial["exes"].append("Injected")
        subsequent = game_info.game_files(self.game)
        self.assertEqual(subsequent["exes"], ["Game"])
        subsequent["dxvk_logs"].append(Path("fake.log"))
        self.assertEqual(game_info.game_files(self.game)["dxvk_logs"], [])

    def test_renamed_directory_invalidates(self):
        game_info.game_files(self.game)
        (self.root / "Binaries").rename(self.root / "Renamed")
        self.assertEqual(game_info.game_files(self.game)["exes"], ["Game"])

    def test_no_cache_if_game_folder_does_not_exist_yet(self):
        absent = self.root / "Missing"
        game = game_scan.GameEntry("Custom", "perf:missing-folder", "Missing", [absent])
        self.assertEqual(game_info.game_files(game)["exes"], [])
        absent.mkdir()
        (absent / "Ready.exe").touch()
        self.assertEqual(game_info.game_files(game)["exes"], ["Ready"])
        game_info.clear_game_files_cache(game.key)

    def test_partial_max_entries_scans_not_cached(self):
        calls = 0
        real = game_info.os.walk

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real(*args, **kwargs)

        with mock.patch.object(game_info.os, "walk", side_effect=counted):
            for _ in range(2):
                self.assertEqual(game_info.game_files(self.game, max_entries=0)["exes"], [])
        self.assertEqual(calls, 2)

    def test_cache_uses_bounded_lru(self):
        old_limit = game_info._FILES_CACHE_LIMIT
        game_info._FILES_CACHE_LIMIT = 2
        self.addCleanup(setattr, game_info, "_FILES_CACHE_LIMIT", old_limit)
        game_info.clear_game_files_cache()
        self.addCleanup(game_info.clear_game_files_cache)
        for n in range(3):
            g = game_scan.GameEntry("Custom", f"perf:lru:{n}", str(n), [self.root])
            game_info.game_files(g)
        self.assertEqual(len(game_info._FILES_CACHE), 2)
        self.assertNotIn(("perf:lru:0", (str(self.root),), 30000, 4), game_info._FILES_CACHE)


class OptiscalerScanCacheTests(unittest.TestCase):
    def test_proxy_replacement_invalidates_dll_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Game"
            root.mkdir()
            game = game_scan.GameEntry("Custom", "perf:optiscaler-proxy", "OptiScaler", [root])
            game_scan.clear_dll_scan_cache(game.key)
            (root / "OptiScaler.ini").write_text("[OptiScaler]\n")
            proxy = root / "dxgi.dll"
            proxy.write_bytes(b"MZ" + bytes(100))
            with mock.patch.object(game_scan.pe_version, "read_version", return_value=(1, 0, 0)) as version:
                game_scan.find_dlls(game)
                self.assertEqual(version.call_count, 1)
                game_scan.find_dlls(game)
                self.assertEqual(version.call_count, 1)
                proxy.write_bytes(b"MZ" + bytes(200))
                game_scan.find_dlls(game)
                self.assertEqual(version.call_count, 2)
            game_scan.clear_dll_scan_cache(game.key)


class _CoarseStat:
    """os.stat result as a filesystem without multigrain timestamps reports it:
    mtime/ctime cut to a 4 ms tick and a constant directory size (ext4, xfs)."""
    TICK = 4_000_000

    def __init__(self, st):
        self._st = st

    def __getattr__(self, name):
        value = getattr(self._st, name)
        if name in ("st_mtime_ns", "st_ctime_ns"):
            return value // self.TICK * self.TICK
        if name == "st_size" and os.path.stat.S_ISDIR(self._st.st_mode):
            return 4096
        return value


class CoarseTimestampCacheTests(unittest.TestCase):
    """A change in the same timestamp tick as the scan leaves the directory stat identical;
    the caches must still notice it (failed on a real Gentoo machine)."""

    def setUp(self):
        real_stat, real_clock = os.stat, time.clock_gettime_ns
        tick = _CoarseStat.TICK
        for target, fake in ((game_scan.os, "stat"), (game_info.os, "stat")):
            p = mock.patch.object(target, fake, lambda *a, **k: _CoarseStat(real_stat(*a, **k)))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(game_scan.time, "clock_gettime_ns", lambda c: real_clock(c) // tick * tick)
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "Game"
        (self.root / "Binaries").mkdir(parents=True)
        (self.root / "Binaries" / "Game.exe").write_bytes(b"MZ")
        self.game = game_scan.GameEntry("Custom", "perf:coarse", "Test", [self.root])
        for clear in (game_info.clear_game_files_cache, game_scan.clear_dll_scan_cache):
            clear(self.game.key)
            self.addCleanup(clear, self.game.key)

    def test_game_files_sees_a_file_added_in_the_same_tick(self):
        self.assertEqual(game_info.game_files(self.game)["exes"], ["Game"])
        (self.root / "Binaries" / "New.exe").write_bytes(b"MZ")
        self.assertEqual(game_info.game_files(self.game)["exes"], ["Game", "New"])

    def test_find_dlls_sees_a_dll_added_in_the_same_tick(self):
        self.assertEqual(len(game_scan.find_dlls(self.game).dlls), 0)
        (self.root / "Binaries" / "nvngx_dlss.dll").write_bytes(b"dummy DLL")
        self.assertEqual(len(game_scan.find_dlls(self.game).dlls), 1)


if __name__ == "__main__":
    unittest.main()
