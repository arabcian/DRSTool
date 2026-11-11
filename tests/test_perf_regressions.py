"""Regressions for metadata-cached game inventories (requires no Qt)."""

import tempfile
import sys
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


if __name__ == "__main__":
    unittest.main()
