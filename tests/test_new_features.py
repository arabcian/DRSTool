"""F01/F03/F05: no Qt, no network, no GPU; provenance and crash-aware session tests."""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

from drstool_core import game_links
from drstool_core.frametime_csv import load_frametimes
from drstool_core.performance_history import group_captures, compare_groups, trend_report
from drstool_core.provenance import source_chain, inspect_environment
from drstool_core.session_recorder import SessionStore, inferred_game_key, session_comparison, supervise


class SessionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / 'sessions')

    def start(self, cmd=None, **kw):
        return self.store.begin(profile='Game', profile_fp='abcdef012345',
                                command=cmd or [sys.executable], env={'SteamAppId': '1091500'}, **kw)

    def test_supervision_tracks_exit_code_and_private_files(self):
        entry = self.start()
        rc, final = supervise(self.store, entry, [sys.executable, '-c', 'import sys;sys.exit(7)'],
                              os.environ.copy(), update_interval=.01)
        self.assertEqual(rc, 7)
        self.assertEqual(final['exit_code'], 7)
        self.assertEqual(self.store.read(entry['id'])['status'], 'nonzero_exit')
        self.assertEqual(final['game_key'], 'steam:1091500')
        mode = self.store.directory.stat().st_mode
        self.assertFalse(mode & (stat.S_IRWXG | stat.S_IRWXO))
        self.assertFalse((self.store.directory / (entry['id'] + '.json')).stat().st_mode & 0o077)
        self.assertNotIn('SteamAppId', json.dumps(final))
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(final))

    def test_reference_is_persistent_and_scoped_to_a_completed_game_session(self):
        key = 'steam:1091500'
        first = self.start()
        self.assertIsNone(self.store.reference(key))
        with self.assertRaises(ValueError):
            self.store.set_reference(key, first['id'])
        self.store.finish(first, returncode=0)
        self.store.set_reference(key, first['id'])
        with self.assertRaises(ValueError):
            self.store.set_reference('steam:2', first['id'])
        reopened = SessionStore(self.store.directory)
        self.assertEqual(reopened.reference(key)['id'], first['id'])
        self.assertIsNone(reopened.reference('steam:2'))
        second = self.store.finish(self.start(), returncode=0)
        reopened.set_reference(key, second['id'])
        self.assertEqual(self.store.reference(key)['id'], second['id'])
        (self.store.directory / f"{second['id']}.json").unlink()
        self.assertIsNone(self.store.reference(key))

    def test_session_comparison_preserves_unknown_runtime_values(self):
        reference = {'profile': 'Game', 'profile_fp': 'old', 'runtime': {'proton': 'GE-Proton-9'}}
        current = {'profile': 'Game', 'profile_fp': 'new', 'runtime': {'proton': 'GE-Proton-10',
                                                                     'nvidia_driver': '595.01'}}
        self.assertIn(('Profile fingerprint', 'old', 'new'), session_comparison(reference, current))
        self.assertIn(('Proton (PROTONPATH)', 'GE-Proton-9', 'GE-Proton-10'),
                      session_comparison(reference, current))
        self.assertIn(('NVIDIA driver', '', '595.01'), session_comparison(reference, current))

    def test_game_does_not_outlive_a_killed_supervisor(self):
        # The game runs in its own session, so SIGKILL of the supervisor used to orphan it.
        import signal
        import subprocess
        pidfile = self.root / 'game.pid'
        code = ("import os,sys; sys.path.insert(0, sys.argv[1]);"
                "from pathlib import Path; from drstool_core.session_recorder import SessionStore, supervise;"
                "s = SessionStore(Path(sys.argv[2]));"
                "e = s.begin(profile='G', profile_fp='0'*12, command=['sleep'], env={});"
                "supervise(s, e, ['sh', '-c', 'echo $$ > ' + sys.argv[3] + '; exec sleep 60'], dict(os.environ))")
        sup = subprocess.Popen([sys.executable, '-c', code, str(Path(__file__).resolve().parents[1]),
                                str(self.root / 's2'), str(pidfile)])
        self.addCleanup(lambda: sup.poll() is None and sup.kill())
        for _ in range(100):
            if pidfile.is_file() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        game = int(pidfile.read_text())
        sup.kill()
        sup.wait()
        for _ in range(60):
            try:
                os.kill(game, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(game, signal.SIGKILL)
            self.fail('game process survived SIGKILL of the supervisor')

    def test_missing_executable_is_launch_failed(self):
        entry = self.start()
        rc, result = supervise(self.store, entry, ['/not/a/real/executable'], os.environ.copy())
        self.assertEqual(rc, 127)
        self.assertEqual(result['status'], 'launch_failed')
        self.assertIn('error', result)

    def test_running_record_becomes_interrupted_when_pid_missing(self):
        e = self.start()
        e['status'] = 'running'
        e['supervisor_pid'] = 2147483000
        self.store.write(e)
        self.assertEqual(self.store.read(e['id'])['status'], 'interrupted')
        self.assertEqual(json.loads((self.store.directory / (e['id'] + '.json')).read_text())['status'], 'running')

    def test_recorded_run_resists_path_traversal_and_symlink(self):
        with self.assertRaises(ValueError):
            self.start(game_key='../../etc/passwd')
        with self.assertRaises(ValueError):
            self.store.read('../secret')
        e = self.start()
        (self.store.directory / f'{e["id"]}.json').unlink()
        (self.store.directory / f'{e["id"]}.json').symlink_to(self.root / 'elsewhere.json')
        with self.assertRaises(OSError):
            self.store.write(e)

    def test_no_full_commands_or_secret_env_persisted(self):
        e = self.store.begin(profile='Demo', profile_fp='abc',
            command=['wine', '--password=leak', 'private/path/to/game.exe'],
            env={'SteamGameId': '21', 'MY_API_TOKEN': 'never-save'})
        raw = (self.store.directory / f'{e["id"]}.json').read_text()
        self.assertNotIn('leak', raw)
        self.assertNotIn('never-save', raw)
        self.assertEqual(e['executable'], 'wine')
        self.assertEqual(inferred_game_key({'SteamGameId': '21'}), 'steam:21')
        self.assertEqual(inferred_game_key({'SteamGameId': 'invalid'}), '')

    def test_recent_lists_latest_and_ignores_other_games(self):
        first = self.start()
        second = self.start(game_key='lutris:some-game')
        self.assertEqual([r['id'] for r in self.store.recent(game_key='lutris:some-game')], [second['id']])
        self.assertIn(first['id'], [r['id'] for r in self.store.recent()])


class ProvenanceTests(unittest.TestCase):
    def test_layer_sources_unset_and_override(self):
        profiles = {
            'base': {'env_vars': {'A': '1', 'SECRET_TOKEN': '123'},
                     'settings': {'0x1': '1', '0x2': '2'}},
            'game': {'base': 'base', 'unset': [['Env', 'A'], ['DRS', '0x1']],
                     'env_vars': {'B': '3'}, 'settings': {'0x3': '4'}},
        }
        sources = source_chain(profiles, 'game')
        self.assertNotIn('A', sources)
        self.assertEqual(sources['B'], ['game'])
        self.assertEqual(sources['DXVK_NVAPI_DRS_SETTINGS'], ['base (DRS)', 'game (DRS)'])
        rows = inspect_environment({'B': '3', 'SECRET_TOKEN': '123'},
                                   {'B': 'diff', 'SECRET_TOKEN': '123', 'KNOWN': 'abc'},
                                   sources=sources, known_keys=('KNOWN',))
        by = {r['key']: r for r in rows}
        self.assertEqual(by['B']['status'], 'overridden')
        self.assertEqual(by['KNOWN']['status'], 'inherited')
        self.assertEqual(by['SECRET_TOKEN']['expected'], '<redacted>')
        self.assertEqual(by['SECRET_TOKEN']['actual'], '<redacted>')

    def test_unreadable_is_not_absent(self):
        rows = inspect_environment({'A': '123'}, None)
        self.assertEqual(rows[0]['status'], 'unverifiable')
        self.assertEqual(inspect_environment({'A': '123'}, {})[0]['status'], 'absent')

    def test_explicit_env_overrides_synthesized_settings(self):
        profiles = {'x': {'settings': {'0x1': '1'}, 'env_vars': {'DXVK_NVAPI_DRS_SETTINGS': 'override'}}}
        self.assertEqual(source_chain(profiles, 'x')['DXVK_NVAPI_DRS_SETTINGS'], ['x'])


class PerformanceHistoryTests(unittest.TestCase):
    def _run(self, fp, fps, when):
        return {'profile': 'p', 'fp': fp, 'avg': fps, 'low1': fps * .8,
                'p99': 1000 / fps * 1.4, 'stutters': 4, 'dur': 60.0, 'mtime': when,
                'kind': 'flm', 'scene': 'fixed-benchmark', 'driver': '615', 'proton': '11.0',
                'dxvk': '2.6', 'vkd3d': '2.14'}

    def test_detects_repeat_regression_but_no_causality_claim(self):
        history = [self._run('a', 100+i, 100+i) for i in range(3)]
        history += [self._run('b', 90+i, 200+i) for i in range(3)]
        report = trend_report(history)
        self.assertTrue(report['comparison']['comparable'])
        self.assertTrue(report['comparison']['possible_regression'])
        self.assertIn('avg', report['comparison']['regressed_metrics'])
        self.assertIn('not proof', report['comparison']['disclaimer'])

    def test_missing_scene_blocks_confident_conclusion(self):
        a = [self._run('a', 100+i, 100+i) for i in range(3)]
        b = [dict(self._run('b', 50+i, 200+i), scene='') for i in range(3)]
        res = trend_report(a+b)['comparison']
        self.assertFalse(res['comparable'])
        self.assertFalse(res['possible_regression'])
        self.assertTrue(res['warnings'])

    def test_different_durations_block_comparison(self):
        a = [self._run('a', 100+i, 100+i) for i in range(3)]
        b = [dict(self._run('b', 60+i, 200+i), dur=120.0) for i in range(3)]
        cmp = trend_report(a+b)['comparison']
        self.assertFalse(cmp['comparable'])
        self.assertFalse(cmp['possible_regression'])
        self.assertTrue(any('duration' in warning.lower() for warning in cmp['warnings']))

    def test_one_capture_not_enough(self):
        a = group_captures([self._run('a', 100, 1), self._run('b', 90, 2)])
        self.assertFalse(compare_groups(a[0], a[1])['comparable'])

    def test_session_capture_does_not_get_relabelled(self):
        with tempfile.TemporaryDirectory() as td:
            link = game_links.LinkStore(Path(td)/'link.json')
            link.record('steam:21', name='G', launcher='Steam', target={}, profile='later', fp='bbbb')
            link.add_perf('steam:21', dict(self._run('aaaa', 80, time.time()),
                                         profile='initial', session_id='id', path='/tmp/sample.csv'))
            run = link.get('steam:21')['perf'][0]
            self.assertEqual(run['fp'], 'aaaa')
            self.assertEqual(run['profile'], 'initial')

    def test_csv_parser_shared_with_gui_supports_finite_numbers(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/'flm.csv'
            p.write_text('flip_ns,interval_ns,is_hitch\n' +
                         ''.join(f'{i*10000000},10000000,0\n' for i in range(100)))
            capture = load_frametimes(str(p))
            self.assertEqual(capture['kind'], 'flm')
            self.assertAlmostEqual(dict((label, value) for label, value, *_ in capture['stats'])['Average FPS'], 100.0)


if __name__ == '__main__':
    unittest.main()
