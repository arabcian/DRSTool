"""F06/F08/F11 fixtures: fabricated kernel logs, read-only backup trees, private exports."""
import json
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from drstool_core import kernel_events, recovery_center, game_report


class KernelEventTests(unittest.TestCase):
    def sample(self):
        return ("1763500000.100000 pc kernel: NVRM: Xid (PCI:0000:01:00): 154, pid=42, name=game\n"
                "1763500001.200000 pc kernel: pcieport 0000:00:01.1: AER: Corrected error\n"
                "1763500002.300000 pc kernel: NVRM: normal heartbeat\n"
                "1763500003.400000 pc kernel: Out of memory: Killed process 345\n")

    def test_parse_xid_pcie_oom_and_ignore_noise(self):
        events = kernel_events.parse_lines(self.sample().splitlines())
        self.assertEqual([x['category'] for x in events], ['xid', 'pcie-aer', 'oom'])
        self.assertEqual(events[0]['xid'], 154)
        self.assertEqual(events[0]['pci_device'], '0000:01:00')

    def test_bad_timestamps_ignore_and_cap(self):
        self.assertEqual(kernel_events.parse_lines(['bad', 'inf pc kernel: Xid 154']), [])
        self.assertEqual(len(kernel_events.parse_lines(self.sample().splitlines(), limit=1)), 1)

    def test_runner_no_shell_no_privilege_with_dates(self):
        captured = []
        def runner(argv, **kwargs):
            captured.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, self.sample(), '')
        out = kernel_events.collect(1763499999, 1763500010, runner=runner)
        self.assertEqual(out['status'], 'ok')
        self.assertEqual(len(out['events']), 3)
        self.assertEqual(captured[0][0][0], 'journalctl')
        self.assertIn('--kernel', captured[0][0])
        self.assertEqual(captured[0][1]['timeout'], 8)
        self.assertNotIn('sudo', captured[0][0])

    def test_failed_journal_is_not_clear_bill_of_health(self):
        r = kernel_events.collect(1763499999, 1763500010,
            runner=lambda *a, **k: subprocess.CompletedProcess(a[0], 1, '', 'permission denied'))
        self.assertEqual(r['status'], 'unavailable')
        self.assertEqual(r['events'], [])

    def test_correlate_bounded_before_during_after(self):
        session = {'id': '1'*32, 'started_at': 100., 'ended_at': 200.}
        def runner(start, end):
            self.assertEqual((start, end), (70., 230.))
            return {'status': 'ok', 'events': [{'timestamp': x, 'category': 'xid'}
                                             for x in (50., 75., 150., 225., 260.)]}
        report = kernel_events.correlate(session, collector=runner)
        self.assertEqual([x['relation'] for x in report['events']], ['before', 'during', 'after'])
        self.assertEqual(report['count'], 3)
        self.assertIn('not establish causation', report['disclaimer'])

    def test_reject_outsize_or_invalid_window(self):
        with self.assertRaises(ValueError):
            kernel_events.correlate({'started_at': 1, 'ended_at': 2e6}, collector=lambda a,b: {})
        with self.assertRaises(ValueError):
            kernel_events.collect(float('nan'), 1000)


class RecoveryCenterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {'XDG_CONFIG_HOME': str(self.home/'config'), 'XDG_DATA_HOME': str(self.home/'data')}

    def inv(self, **kw):
        return recovery_center.inventory(home=self.home, env=self.env, **kw)

    def test_list_profile_and_game_dll_and_tool_journal(self):
        hist = self.home/'config/drstool/history'
        hist.mkdir(parents=True)
        (hist/'profiles-20260101.json').write_text('{}')
        dll = self.home/'data/drstool/dll-backups/steam-7/2026'
        dll.mkdir(parents=True)
        (dll/'manifest.json').write_text(json.dumps({'state': 'committed', 'entries': []}))
        tools = self.home/'data/Steam/compatibilitytools.d'
        tools.mkdir(parents=True)
        (tools/('.drstool-tm-install-' + 'a'*32 + '.json')).write_text('{}')
        result = self.inv()
        self.assertEqual(result['total'], 3)
        self.assertEqual(set(result['counts']), {'profile-snapshot', 'game-dll', 'tool-install-journal'})
        self.assertFalse(result['automatic_restore'])
        only_game = self.inv(game_key='steam:7')
        self.assertEqual([x['kind'] for x in only_game['entries']], ['game-dll'])

    def test_launcher_file_backup_from_known_game_link(self):
        path = self.home/'config/drstool/game_links.json'
        path.parent.mkdir(parents=True)
        target = self.home/'lutris/games/foo.yml'
        target.parent.mkdir(parents=True)
        target.write_text('game: foo')
        backup = target.with_name('foo.yml.20261012.bak')
        backup.write_text('older')
        (target.parent/'unrelated.yml.20261012.bak').write_text('not ours')
        path.write_text(json.dumps({'games': {'lutris:foo': {'target': {'path': str(target)}}}}))
        inv = self.inv(game_key='lutris:foo')
        self.assertEqual(inv['total'], 1)
        self.assertEqual(inv['entries'][0]['kind'], 'launcher-config')
        self.assertEqual(inv['entries'][0]['name'], backup.name)

    def test_symlink_backup_is_skipped(self):
        hist = self.home/'config/drstool/history'
        hist.mkdir(parents=True)
        (hist/'profiles-a.json').symlink_to('/etc/passwd')
        self.assertEqual(self.inv()['total'], 0)

    def test_prefix_dll_inventory_and_reset_aside(self):
        pfx = self.home/'games'/'prefix'
        backup = pfx/'.drstool-backup'/'snap'
        backup.mkdir(parents=True)
        (backup/'prefix-dxvk.json').write_text('{}')
        aside = pfx.parent/'prefix.drstool-reset-20260101'
        aside.mkdir()
        result = self.inv(game_key='steam:7', prefix=pfx)
        self.assertEqual(set(result['counts']), {'prefix-dll', 'prefix-reset-aside'})

    def test_oversize_and_symlinked_manifests_are_not_read(self):
        root = self.home/'data/drstool/dll-backups'
        oversized = root/'steam-over'/'backup'
        oversized.mkdir(parents=True)
        (oversized/'manifest.json').write_bytes(b'{' + b' ' * (262144 + 2) + b'}')
        symlinked = root/'steam-link'/'backup'
        symlinked.mkdir(parents=True)
        (symlinked/'manifest.json').symlink_to('/etc/passwd')
        out = self.inv()
        self.assertEqual(out['total'], 1)
        self.assertEqual(out['entries'][0]['kind'], 'game-dll')
        self.assertEqual(out['entries'][0]['state'], 'unreadable')

    def test_missing_item_does_not_hide_other_items(self):
        root = self.home/'config/drstool/history'
        root.mkdir(parents=True)
        (root/'profiles-20260101.json').write_text('{}')
        (root/'profiles-20260102.json').write_text('{}')
        self.assertEqual(self.inv()['counts']['profile-snapshot'], 2)

    def test_no_unbounded_entries(self):
        self.assertLessEqual(self.inv(max_entries=0)['total'], 1)


class StubSessionStore:
    def __init__(self):
        self.session = {'id':'c'*32, 'game_key':'steam:123', 'status':'completed',
                        'started_at': 100.0, 'ended_at': 200.0, 'profile': 'Gaming',
                        'performance': {'path': '/non-home/personal.csv', 'avg': 87.5}}
    def read(self, session_id):
        return self.session
    def recent(self, game_key='', limit=12):
        return [self.session] if game_key == 'steam:123' else []


class StubLinks:
    def get(self, key):
        return {'profile': 'Gaming', 'fp': '123', 'perf': [{'avg': 87.5, 'path':'/private/data.csv',
                                                            'profile':'Gaming', 'fp':'123', 'mtime':100}]}


class GameReportTests(unittest.TestCase):
    def report(self, **kwargs):
        return game_report.compile_report('steam:123', session_store=StubSessionStore(),
                    link_store=StubLinks(), system_info={'platform':'Linux'}, **kwargs)

    def test_does_not_export_capture_paths(self):
        payload = json.dumps(self.report())
        self.assertNotIn('/non-home', payload)
        self.assertNotIn('/private', payload)
        self.assertIn('87.5', payload)
        self.assertEqual(self.report()['kernel']['status'], 'not-requested')

    def test_kernel_explicit(self):
        result = self.report(include_kernel=True,
            event_collector=lambda a,b: {'status':'ok', 'events':[{'timestamp':150,'category':'xid'}]})
        self.assertEqual(result['kernel']['count'], 1)
        self.assertEqual(result['kernel']['events'][0]['relation'], 'during')

    def test_cross_game_session_forbidden(self):
        with self.assertRaises(ValueError):
            game_report.compile_report('steam:999', session_id='c'*32, session_store=StubSessionStore(),
                                        link_store=StubLinks(), system_info={})

    def test_custom_game_key_redacted(self):
        class Fake:
            def get(self, k): return {}
        class SS:
            def recent(self, **kw): return []
        data = game_report.compile_report('custom:/private/location/game.exe', session_store=SS(),
                                           link_store=Fake(), system_info={})
        self.assertTrue(data['game_key'].startswith('custom:sha256:'))
        self.assertNotIn('/private/location', json.dumps(data))

    def test_zip_permissions_contents_and_opt_in_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root/'game.log'
            log.write_text('frames=60\nAuthorization: Bearer mysupersecret1234567\n')
            dest = root/'diag.zip'
            game_report.export_zip(dest, self.report(), log_file=log)
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
            with zipfile.ZipFile(dest) as z:
                self.assertEqual(set(z.namelist()), {'report.json','README.txt','log_excerpt.txt'})
                self.assertNotIn('mysupersecret1234567', z.read('log_excerpt.txt').decode())
            with self.assertRaises(FileExistsError):
                game_report.export_zip(dest, self.report())

    def test_reject_symlink_log_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            badlog = root/'fake.log'
            badlog.symlink_to('/etc/passwd')
            with self.assertRaises(OSError):
                game_report.export_zip(root/'diag.zip', self.report(), log_file=badlog)
            target = root/'target.zip'
            target.write_bytes(b'original')
            bad = root/'diag.zip'
            bad.symlink_to(target)
            with self.assertRaises(ValueError):
                game_report.export_zip(bad, self.report())
            self.assertEqual(target.read_bytes(), b'original')


if __name__ == '__main__':
    unittest.main()
