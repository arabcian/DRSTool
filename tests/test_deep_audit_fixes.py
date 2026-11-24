"""Regression coverage for the deep audit. No Qt, GPU or root required."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from drstool_core import file_transactions as tx, share_code, xid_analysis as xa
from drstool_core.bisect_search import CrashSearch
from drstool_core.cache_ops import clear_cache_nodes
from drstool_core.benchmark import summarize, compare_repeats
from drstool_core.frametime_csv import _parse_frametimes, frame_stats
from drstool_core.game_links import LinkStore
from drstool_core.launcher_paths import steam_libraries, steam_apps


class DeepAuditTests(unittest.TestCase):
    def test_clean_full_baseline_does_not_name_culprits(self):
        s = CrashSearch(list('abc'))
        s.answer(False)
        self.assertEqual(s.pending, list('abc'))
        s.answer(False)
        self.assertEqual(s.outcome, ('not-reproduced', []))
        empty = CrashSearch([])
        empty.answer(False)
        self.assertEqual(empty.outcome, ('no-candidates', []))

    def test_repeated_answers_disagree_and_retest_does_not_record(self):
        s = CrashSearch(['a'], repeats=2)
        s.answer(None)
        self.assertEqual(s.tests_done, 0)
        s.answer(False)
        self.assertEqual(s.pending, [])
        s.answer(True)
        self.assertEqual(s.outcome, ('inconclusive', []))
        s.undo()
        self.assertFalse(s.done)
        self.assertEqual(s.pending, [])

    def test_cache_root_parent_symlink_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            external = root / 'external'; external.mkdir()
            victim = external / 'cache'; victim.mkdir(); (victim / 'keep').write_text('keep')
            link = root / 'link'; link.symlink_to(external, target_is_directory=True)
            result = clear_cache_nodes([{'path': str(link / 'cache')}], lambda _: True,
                                       lambda: False, lambda *_: None, lambda _: True)
            self.assertTrue((victim / 'keep').exists())
            self.assertTrue(result['errors'] or result['refused'])

    def test_source_changes_during_staging_no_target_change(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td); src = p/'source'; dst = p/'target'; backup = p/'backup'
            src.write_bytes(b'new'); dst.write_bytes(b'old')
            real = tx.shutil.copy2
            def inject(a, b, *args, **kw):
                if Path(a) == src:
                    src.write_bytes(b'modified')
                return real(a, b, *args, **kw)
            with patch.object(tx.shutil, 'copy2', inject), self.assertRaises(RuntimeError):
                tx.apply([tx.Change(dst, src, 'original')], backup)
            self.assertEqual(dst.read_bytes(), b'old')
            self.assertEqual(json.loads((backup/'transaction.json').read_text())['state'], 'rolled-back')

    def test_rollback_runtime_error_does_not_hide_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td); backup = p/'backup'
            changes = []
            for i in range(3):
                a, b = p/f'target{i}', p/f'source{i}'
                a.write_bytes(b'old'); b.write_bytes(b'new'); changes.append(tx.Change(a, b, a.name))
            real = tx.atomic_copy
            def inject(a,b):
                if b == changes[2].target or (b == changes[1].target and Path(a).is_relative_to(backup)):
                    raise RuntimeError('injected')
                return real(a,b)
            with patch.object(tx, 'atomic_copy', inject), self.assertRaises(RuntimeError):
                tx.apply(changes, backup)
            self.assertEqual(changes[0].target.read_bytes(), b'old')
            self.assertEqual(changes[1].target.read_bytes(), b'new')
            self.assertEqual(json.loads((backup/'transaction.json').read_text())['state'], 'recovery-needed')
            self.assertEqual(tx.restore(backup)[0], 1)

    def test_consistent_metrics_long_stall_and_csv_quotes(self):
        vals = [8.0]*5 + [16.0]*5
        st = {k:v for k,v,*_ in frame_stats(vals)}
        self.assertEqual(st['Median frametime (ms)'], summarize(vals).median_ms)
        self.assertEqual(st['Median frametime (ms)'], 12)
        rows = ['"game,name",8,inf']*9 + ['"game,name",12000,7']
        d = _parse_frametimes('test.csv', ['name,frametime,gpu_vram_used'], iter(rows))
        self.assertEqual(max(d['ft']), 12000)
        self.assertEqual(d['extra']['max VRAM'], '7')
        self.assertIn('Capture semantics', d['extra'])
        r = compare_repeats([vals]*3, [vals]*3, kinds_a=['flm']*3, kinds_b=['mangohud']*3)
        self.assertTrue(any('formats' in w for w in r['warnings']))

    def test_gpu_and_time_boundaries_and_followup(self):
        lines = ['[10.000] NVRM: Xid (PCI:0000:01:00): 13, fault',
                 '[11.000] NVRM: Xid (PCI:0000:02:00): 13, fault',
                 '[12.000] NVRM: Xid (PCI:0000:01:00): 45, followup',
                 '[1.000] NVRM: Xid (PCI:0000:01:00): 45, followup',
                 'NVRM: Xid (PCI:0000:01:00): 45, followup']
        incs = xa.incidents(xa.parse_events(lines)[0])
        self.assertEqual([x['codes'] for x in incs], [[13,45], [13], [45], [45]])
        self.assertEqual(incs[-1]['scope'], 'unknown')
        self.assertFalse(incs[-1]['root_known'])
        a = xa.parse_event('2026-01-01T12:00:00+03:00 NVRM: Xid: 13, fault')
        b = xa.parse_event('2026-01-01T09:00:00Z NVRM: Xid: 13, fault')
        self.assertEqual(a['ts'], b['ts'])

    def test_triple_model_and_general_bisect(self):
        m = xa.CrashModel(['a','b','c'], triple_limit=3)
        h = ('and','a','b','c')
        self.assertIn(h, m.hyps)
        self.assertFalse(m._holds(h, frozenset('ab')))
        self.assertTrue(m._holds(h, frozenset('abc')))
        s = CrashSearch(list('abcd'))
        while not s.done:
            s.answer(set('abc') <= set(s.pending))
        self.assertEqual(set(s.culprits), set('abc'))

    def test_share_code_long_trailing_prose_and_false_type(self):
        profile = {'env_vars': {'DXVK_HUD':'fps', 'LD_DEBUG':'all'}, 'gamescope_enabled':'false',
                   'gamescope_flags': {'--output-width':'abc','--not-a-real-flag':'x'}}
        code = share_code.encode('test', profile)
        d = share_code.decode(share_code.wrap(code, 31) + '\n' + 'word '*2000)
        self.assertFalse(d.profile['gamescope_enabled'])
        self.assertFalse(d.profile['gamescope_flags'])
        self.assertIn('LD_DEBUG', d.flagged)

    def test_game_links_preserves_corrupt_and_refreshes(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/'links.json'; a,b = LinkStore(p),LinkStore(p)
            a.assign('game','p', name='first')
            self.assertEqual(b.get('game')['name'], 'first')
            a.record('game',name='renamed',launcher='Steam',target={},profile='p',fp='a')
            self.assertEqual(b.get('game')['name'],'renamed')
            p.write_text('{bad')
            with self.assertRaises(ValueError):
                a.assign('other','p')
            self.assertEqual(p.read_text(), '{bad')
            q = a.recover_backup()
            self.assertEqual(q.read_text(), '{bad')
            self.assertEqual(a.get('game')['name'], 'first')

    def test_vdf_scope_ignores_comments_and_nested_fake_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); apps=root/'steamapps'; apps.mkdir()
            valid=root/'library'; (valid/'steamapps').mkdir(parents=True)
            fake=root/'fake'; (fake/'steamapps').mkdir(parents=True)
            (apps/'libraryfolders.vdf').write_text(f'"libraryfolders" {{ // "path" "{fake}"\n "1" {{ "path" "{valid}" "apps" {{ "path" "{fake}" }} }} }}')
            self.assertEqual(steam_libraries(root), [apps,valid/'steamapps'])
            (apps/'appmanifest_1.acf').write_text('"AppState" { "appid" "1" "name" "Real" "Nested" { "appid" "99" } }')
            self.assertEqual(list(steam_apps(apps)), [('1','Real','')])
