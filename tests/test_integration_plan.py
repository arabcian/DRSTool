"""Regression tests for the 2026-11 DRSTool performance/safety plan.

All core tests work without PySide6, GPU access, or internet.
"""
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from drstool_core.cache_ops import clear_cache_nodes
from drstool_core.file_transactions import Change, apply
from drstool_core import file_transactions as transactions
from drstool_core import game_scan
from drstool_core.http_download import DownloadCancelled, download
from drstool_core.lutris_config_scan import scan_game_configs
from drstool_core.yaml_merge import merge_lutris_yaml


class _Reply(io.BytesIO):
    def __init__(self, code, headers, data):
        super().__init__(data)
        self.status = code
        self.headers = headers

    def getcode(self):
        return self.status


class ResumeTests(unittest.TestCase):
    URL = 'https://example.invalid/release.tar.gz'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / 'part.tar.gz'
        self.cancel = threading.Event()

    def run_download(self, responses, *, progress=None):
        requests = []
        def open_fake(request, timeout):
            self.assertEqual(timeout, 20)
            requests.append(request)
            resp = responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp
        with mock.patch('drstool_core.http_download.urllib.request.urlopen', side_effect=open_fake):
            download(self.URL, self.dest, 100, self.cancel, progress or (lambda *_: None),
                     user_agent='DRSTool/test', chunk_size=3)
        return requests

    def create_partial(self):
        def stop(_step, done, _total):
            if done >= 3:
                self.cancel.set()
        with self.assertRaises(DownloadCancelled):
            self.run_download([_Reply(200, {'ETag': '"v1"', 'Content-Length': '9'}, b'abcdefghi')], progress=stop)
        self.cancel.clear()
        self.assertEqual(self.dest.read_bytes(), b'abc')
        self.assertTrue(self.dest.with_name(self.dest.name + '.http.json').is_file())

    def test_new_download(self):
        requests = self.run_download([_Reply(200, {'ETag': '"v1"', 'Content-Length': '6'}, b'abcdef')])
        self.assertEqual(self.dest.read_bytes(), b'abcdef')
        self.assertNotIn('Range', requests[0].headers)
        self.assertFalse(self.dest.with_name(self.dest.name + '.http.json').exists())

    def test_correct_resume_with_if_range(self):
        self.create_partial()
        reqs = self.run_download([_Reply(206, {'ETag': '"v1"', 'Content-Range': 'bytes 3-8/9',
                                                    'Content-Length': '6'}, b'defghi')])
        self.assertEqual(self.dest.read_bytes(), b'abcdefghi')
        self.assertEqual(reqs[0].get_header('Range'), 'bytes=3-')
        self.assertEqual(reqs[0].get_header('If-range'), '"v1"')

    def test_invalid_partial_offset_forces_fresh(self):
        self.create_partial()
        reqs = self.run_download([
            _Reply(206, {'ETag': '"v1"', 'Content-Range': 'bytes 2-8/9'}, b'cdefghi'),
            _Reply(200, {'ETag': '"v2"', 'Content-Length': '6'}, b'123456'),
        ])
        self.assertEqual(self.dest.read_bytes(), b'123456')
        self.assertTrue(reqs[0].get_header('Range'))
        self.assertIsNone(reqs[1].get_header('Range'))

    def test_416_forces_fresh(self):
        self.create_partial()
        error = urllib.error.HTTPError(self.URL, 416, 'Range invalid', {}, None)
        requests = self.run_download([
            error, _Reply(200, {'ETag': '"v1"', 'Content-Length': '2'}, b'xy')])
        self.assertEqual(self.dest.read_bytes(), b'xy')
        self.assertIsNone(requests[1].get_header('Range'))

    def test_200_fallback_replaces_partial(self):
        self.create_partial()
        self.run_download([_Reply(200, {'ETag': '"v2"', 'Content-Length': '4'}, b'new!')])
        self.assertEqual(self.dest.read_bytes(), b'new!')

    def test_partial_without_validator_cannot_resume(self):
        self.dest.write_bytes(b'previous')
        requests = self.run_download([_Reply(200, {'Content-Length': '3'}, b'new')])
        self.assertEqual(self.dest.read_bytes(), b'new')
        self.assertIsNone(requests[0].get_header('Range'))

    def test_truncated_response_keeps_resumable_partial(self):
        with self.assertRaisesRegex(RuntimeError, 'truncated'):
            self.run_download([_Reply(200, {'ETag': '"v1"', 'Content-Length': '10'}, b'1234')])
        self.assertEqual(self.dest.read_bytes(), b'1234')
        self.assertTrue(self.dest.with_name(self.dest.name + '.http.json').is_file())

    def test_truncated_206_even_without_content_length(self):
        self.create_partial()
        with self.assertRaisesRegex(RuntimeError, 'truncated'):
            self.run_download([_Reply(206, {'ETag': '"v1"',
                                                   'Content-Range': 'bytes 3-8/9'}, b'de')])


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        self.a, self.b = self.p/'localconfig.vdf', self.p/'config.vdf'
        self.x, self.y = self.p/'local-new', self.p/'config-new'
        self.a.write_bytes(b'local old')
        self.b.write_bytes(b'config old')
        self.x.write_bytes(b'local new')
        self.y.write_bytes(b'config new')
        self.changes = [Change(self.a, self.x, 'localconfig.vdf'),
                        Change(self.b, self.y, 'config.vdf')]

    def test_second_write_failure_restores_first(self):
        real_copy = transactions.atomic_copy
        calls = []
        def inject(src, dst):
            calls.append((src, dst))
            if dst == self.b:
                raise OSError('injected failure on second VDF')
            return real_copy(src, dst)
        with mock.patch.object(transactions, 'atomic_copy', side_effect=inject):
            with self.assertRaisesRegex(OSError, 'injected failure'):
                apply(self.changes, self.p/'journal')
        self.assertEqual(self.a.read_bytes(), b'local old')
        self.assertEqual(self.b.read_bytes(), b'config old')
        self.assertEqual(json.loads((self.p/'journal/transaction.json').read_text())['state'], 'rolled-back')

    def test_external_change_to_second_file_must_not_be_overwritten(self):
        real_copy = transactions.atomic_copy
        def change_second(src, dst):
            if dst == self.a:
                self.b.write_bytes(b'updated by Steam externally')
            return real_copy(src, dst)
        with mock.patch.object(transactions, 'atomic_copy', side_effect=change_second):
            with self.assertRaisesRegex(RuntimeError, 'modified concurrently'):
                apply(self.changes, self.p/'journal')
        self.assertEqual(self.a.read_bytes(), b'local old')
        self.assertEqual(self.b.read_bytes(), b'updated by Steam externally')

    def test_success_commits_both_files(self):
        apply(self.changes, self.p/'journal')
        self.assertEqual((self.a.read_bytes(), self.b.read_bytes()), (b'local new', b'config new'))


class CacheCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root/'cache'
        self.cache.mkdir()

    def clear(self, nodes, cancelled=lambda: False, progress=lambda *_: None):
        return clear_cache_nodes(nodes, lambda path: path.startswith(str(self.root)),
                                 cancelled, progress, lambda name: 'cache' in name)

    def test_preserve_unrelated_custom_items_and_symlink_target(self):
        (self.cache/'keep.txt').write_text('keep')
        sub = self.cache/'shadercache'
        sub.mkdir()
        (sub/'cache.bin').write_text('delete')
        outside = self.root/'outside.txt'
        outside.write_text('NEVER DELETE')
        (sub/'linked').symlink_to(outside)
        result = self.clear([{'path': str(self.cache), 'user_path': True}])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['kept'], 1)
        self.assertFalse(sub.exists())
        self.assertEqual((self.cache/'keep.txt').read_text(), 'keep')
        self.assertEqual(outside.read_text(), 'NEVER DELETE')

    def test_symlink_as_cache_root_is_refused(self):
        link = self.root/'outside-link'
        link.symlink_to(self.cache, target_is_directory=True)
        result = self.clear([{'path': str(link), 'user_path': False}])
        self.assertEqual(result['refused'], 1)
        self.assertTrue(link.is_symlink())

    def test_cancel_leaves_remainder_intact(self):
        for i in range(30):
            (self.cache/f'{i}.bin').write_bytes(b'x')
        stop = threading.Event()
        result = self.clear([{'path': str(self.cache)}], stop.is_set,
                            lambda *_: stop.set())
        self.assertTrue(result['cancelled'])
        self.assertGreaterEqual(result['deleted'], 1)
        self.assertGreater(len(list(self.cache.iterdir())), 0)

    def test_permission_error_is_reported(self):
        victim = self.cache/'locked.bin'
        victim.write_bytes(b'x')
        original = Path.unlink
        def fail(self, *args, **kwargs):
            if self == victim:
                raise PermissionError('permission denied')
            return original(self, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', fail):
            result = self.clear([{'path': str(self.cache)}])
        self.assertEqual(len(result['errors']), 1)
        self.assertTrue(victim.exists())


class LutrisRoundtripTests(unittest.TestCase):
    ORIGINAL = """# game comment
game:
  exe: 'C:/Games/Example/game.exe'
system:
  # preserve this note
  env:
    USER_SETTING: "my custom value" # do not change
    DXVK_HUD: 'old'
"""

    def test_comments_and_quotes_remain(self):
        out, sys_add, env_add, sys_rem, env_rem = merge_lutris_yaml(
            self.ORIGINAL, {'gamescope': True}, {'DXVK_HUD': 'fps'})
        self.assertIn('# game comment', out)
        self.assertIn('# preserve this note', out)
        self.assertIn('USER_SETTING: "my custom value" # do not change', out)
        self.assertIn("exe: 'C:/Games/Example/game.exe'", out)
        self.assertEqual((sys_add, env_add, sys_rem, env_rem),
                         ({'gamescope'}, {'DXVK_HUD'}, set(), set()))

    def test_identical_merge_returns_exact_original_bytes(self):
        out, *changes = merge_lutris_yaml(self.ORIGINAL, {}, {})
        self.assertEqual(out, self.ORIGINAL)
        self.assertTrue(all(not change for change in changes))

    def test_removed_only_owned_previous_values(self):
        out, _, _, sys_removed, env_removed = merge_lutris_yaml(
            self.ORIGINAL, {}, {}, {'env': {'DXVK_HUD': 'old', 'USER_SETTING': 'not owner'}})
        self.assertIn('USER_SETTING', out)
        self.assertNotIn('DXVK_HUD:', out)
        self.assertEqual(sys_removed, set())
        self.assertEqual(env_removed, {'DXVK_HUD'})


class LutrisScanCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def test_50_250_1000_and_warm_cache(self):
        for count in (50, 250, 1000):
            with self.subTest(game_configs=count):
                for i in range(count):
                    (self.folder/f'{i:05d}.yml').write_text(
                        f'game:\n  exe: /games/game_{i}.exe\nsystem:\n  runner: wine\n')
                rows, cached = scan_game_configs(self.folder)
                self.assertEqual(len(rows), count)
                from drstool_core import lutris_config_scan as scan_module
                with mock.patch.object(scan_module.yaml, 'load',
                                       side_effect=AssertionError('parsed cached YAML')):
                    warm, warm_cache = scan_game_configs(self.folder, cached)
                self.assertEqual(len(warm), count)
                self.assertEqual(cached, warm_cache)
                for file in self.folder.glob('*.yml'):
                    file.unlink()

    def test_changed_file_and_deleted_file_invalidate(self):
        a, b = self.folder/'a.yml', self.folder/'b.yml'
        a.write_text('game:\n  exe: /games/one.exe\n')
        b.write_text('game:\n  exe: /games/two.exe\n')
        _, cache = scan_game_configs(self.folder)
        a.write_text('game:\n  exe: /games/changed_game.exe\n')
        b.unlink()
        rows, cache2 = scan_game_configs(self.folder, cache)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 'changed_game')
        self.assertEqual(set(cache2), {str(a)})

    def test_corrupted_yaml_and_external_symlink_skipped(self):
        (self.folder/'broken.yml').write_text('game: [unterminated\n')
        # Avoid depending on any outside files: self-owned temporary fixture.
        with tempfile.TemporaryDirectory() as td:
            external = Path(td)/'game.yml'
            external.write_text('game: {exe: /outside/game.exe}\n')
            (self.folder/'external.yml').symlink_to(external)
            rows, _ = scan_game_configs(self.folder)
        self.assertEqual(rows, [])


class IncrementalDllScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'Game'
        (self.root/'deep'/'nested').mkdir(parents=True)
        self.game = game_scan.GameEntry('Custom', 'custom:test-metadata', 'Test Game', [self.root])
        game_scan.clear_dll_scan_cache(self.game.key)

    def tearDown(self):
        game_scan.clear_dll_scan_cache(self.game.key)

    def test_warm_scan_avoids_os_walk(self):
        game_scan.find_dlls(self.game)
        with mock.patch.object(game_scan.os, 'walk', side_effect=AssertionError('walk on warm scan')):
            game_scan.find_dlls(self.game)

    def test_added_nested_dll_invalidates_cache(self):
        first = game_scan.find_dlls(self.game)
        self.assertEqual(len(first.dlls), 0)
        (self.root/'deep'/'nested'/'nvngx_dlss.dll').write_bytes(b'dummy DLL')
        second = game_scan.find_dlls(self.game)
        self.assertEqual(len(second.dlls), 1)

    def test_in_place_dll_update_invalidates_cache(self):
        file = self.root/'nvngx_dlss.dll'
        file.write_bytes(b'old fake data')
        game_scan.find_dlls(self.game)
        count = 0
        real_walk = game_scan.os.walk
        def walked(*a, **kw):
            nonlocal count
            count += 1
            return real_walk(*a, **kw)
        file.write_bytes(b'new different data')
        with mock.patch.object(game_scan.os, 'walk', side_effect=walked):
            game_scan.find_dlls(self.game)
        self.assertEqual(count, 1)

    def test_force_full_ignores_valid_cache(self):
        game_scan.find_dlls(self.game)
        count = 0
        real_walk = game_scan.os.walk
        def walked(*a, **kw):
            nonlocal count
            count += 1
            return real_walk(*a, **kw)
        with mock.patch.object(game_scan.os, 'walk', side_effect=walked):
            game_scan.find_dlls(self.game, force_rescan=True)
        self.assertEqual(count, 1)


if __name__ == '__main__':
    unittest.main()
