"""Regression tests for v3 enhancements (no Qt/GPU required)."""
from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from drstool_core.benchmark import compare_repeats
from drstool_core.diagnostics import gather, transaction_status
from drstool_core.i18n import TR, translate

ROOT = Path(__file__).resolve().parents[1]


class FrameAnalysisTests(unittest.TestCase):
    def test_bootstrap_is_deterministic_and_does_not_mislead(self):
        a = [[10.0] * 3200 for _ in range(4)]
        b = [[9.5] * 3300 for _ in range(4)]
        res = compare_repeats(a, b, same_scene_confirmed=True)
        self.assertEqual(res, compare_repeats(a, b, same_scene_confirmed=True))
        self.assertEqual(res['verdict'], 'B appears faster (repeat-run evidence only)')
        self.assertGreater(res['bootstrap_interval_pct'][0], 0)

    def test_high_variation_makes_result_inconclusive(self):
        a = [[10.] * 3100, [15.] * 2100, [7.] * 4500]
        b = [[9.] * 3450, [12.] * 2550, [6.] * 5200]
        res = compare_repeats(a, b, same_scene_confirmed=True)
        self.assertFalse(res['comparable'])
        self.assertEqual(res['verdict'], 'inconclusive')
        self.assertGreater(res['run_cv_pct_a'], 5)

    def test_missing_scene_confirmation_is_not_a_speed_claim(self):
        a = [[10.] * 3200 for _ in range(3)]
        b = [[9.] * 3500 for _ in range(3)]
        res = compare_repeats(a, b)
        self.assertEqual(res['verdict'], 'inconclusive')


class JournalAuditTests(unittest.TestCase):
    def test_reads_actual_journal_filename_without_leaking_private_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jdir = root / 'drstool/dll-backups/secret-game/20261103'
            jdir.mkdir(parents=True)
            (jdir / 'transaction.json').write_text(json.dumps({
                'state': 'prepared', 'entries': [{'target': '/private/username/game.dll'}]}),
                encoding='utf-8')
            result = gather(home=root, env={'XDG_DATA_HOME': str(root)})
            self.assertEqual(result['transactions_need_attention'], 1)
            report = str(result)
            self.assertNotIn('secret-game', report)
            self.assertNotIn('username', report)
            self.assertIn('updated_utc', report)

    def test_symlink_journal_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jdir = root / 'drstool/dll-backups/game/backup'
            jdir.mkdir(parents=True)
            (root / 'secret.json').write_text('{"state":"prepared"}')
            (jdir / 'transaction.json').symlink_to(root / 'secret.json')
            self.assertEqual(transaction_status(root), [])

    def test_capped_and_oversized_journals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for idx in range(5):
                target = root / f'drstool/dll-backups/g/{idx}/transaction.json'
                target.parent.mkdir(parents=True)
                target.write_text('X' * (1024*1024+1) if idx==0 else '{}')
            result = transaction_status(root, max_entries=2)
            self.assertEqual(len(result), 2)
            self.assertEqual(transaction_status(root, max_entries=0), [])


class SourceIntegrityTests(unittest.TestCase):
    def test_prefix_backup_path_parenthesized(self):
        # A regression for the prior Path + str TypeError on prefix updates.
        source = (ROOT / 'drstool_gui/dll_manager.py').read_text()
        self.assertIn('/ (datetime.now().strftime(', source)
        ast.parse(source)

    def test_history_and_session_are_separate_mixins(self):
        source = (ROOT / 'drstool_gui/main_window.py').read_text()
        self.assertIn('class MainWindow(HistoryMixin, SessionMixin, QMainWindow):', source)
        history = (ROOT / 'drstool_gui/main_history.py').read_text()
        self.assertIn('def apply_state(', history)
        ast.parse(history)
        session = (ROOT / 'drstool_gui/main_session.py').read_text()
        self.assertIn('def _restore_session(', session)
        ast.parse(session)

    def test_user_facing_translation_preserves_technical_values(self):
        self.assertGreaterEqual(len(TR), 300)
        self.assertEqual(translate('Build & Install (pkexec)', 'tr_TR'), 'Derle ve kur (pkexec)')
        self.assertEqual(translate('VKD3D_CONFIG', 'tr_TR'), 'VKD3D_CONFIG')
        self.assertEqual(translate('LD_PRELOAD', 'tr_TR'), 'LD_PRELOAD')
        self.assertEqual(translate('Build & Install (pkexec)', 'en_US'), 'Build & Install (pkexec)')

if __name__ == '__main__':
    unittest.main()
