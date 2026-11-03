"""Regression tests for benchmark, risk classifier and read-only diagnostics."""
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from drstool_core.benchmark import summarize, compare, compare_repeats
from drstool_core.risk import assess_profile, risk_level
from drstool_core.diagnostics import gather, to_json


class BenchmarkTests(unittest.TestCase):
    def test_constant_frames(self):
        s = summarize([10.0] * 3000)
        self.assertAlmostEqual(s.average_fps, 100)
        self.assertEqual(s.stutters, 0)
        self.assertEqual(s.p99_ms, 10)
        self.assertAlmostEqual(s.duration_s, 30)

    def test_finite_validation(self):
        for val in (math.nan, math.inf, -1, 0, 10000):
            with self.subTest(val=val), self.assertRaises(ValueError):
                summarize([10.0] * 100 + [val])

    def test_comparison_descriptive(self):
        v = compare([10.] * 3000, [8.] * 3750, scene_confirmed=True)
        self.assertAlmostEqual(v['fps_change_pct'], 25)
        self.assertTrue(v['comparable'])

    def test_mismatched_scenes_warning(self):
        v = compare([10.] * 3000, [10.] * 3000)
        self.assertFalse(v['comparable'])
        self.assertTrue(any('scene' in msg for msg in v['warnings']))

    def test_repeat_requires_three(self):
        with self.assertRaises(ValueError):
            compare_repeats([[10.]*3000]*2, [[9.]*3334]*3)

    def test_three_repeat_medians(self):
        a = [[10.] * 3000, [9.] * 3400, [11.] * 2800]
        b = [[8.] * 4000, [7.] * 4500, [9.] * 3500]
        v = compare_repeats(a, b, same_scene_confirmed=True)
        self.assertFalse(v['comparable'])  # very different baseline runs: >5% CV
        self.assertGreater(v['median_change_pct'], 0)
        self.assertIn('run_cv_pct_a', v)

    def test_consistent_repeats_and_bootstrap(self):
        a = [[10.0] * 3000 for _ in range(4)]
        b = [[9.5] * 3200 for _ in range(4)]
        v = compare_repeats(a, b, same_scene_confirmed=True)
        self.assertTrue(v['comparable'])
        self.assertEqual(v['verdict'], 'B appears faster (repeat-run evidence only)')
        self.assertLess(v['bootstrap_interval_pct'][0], v['bootstrap_interval_pct'][1] + .0001)


class RiskTests(unittest.TestCase):
    def test_standard_profile(self):
        self.assertEqual(risk_level({'DXVK_HUD': 'fps'}), 'standard')

    def test_preload_is_advanced(self):
        self.assertEqual(risk_level({'LD_PRELOAD': '/tmp/library.so'}), 'advanced')

    def test_missing_ntsync(self):
        issues = assess_profile({'PROTON_USE_NTSYNC': '1'}, has_ntsync=False)
        self.assertTrue(any(i.level == 'incompatible' for i in issues))

    def test_no_nvidia(self):
        self.assertEqual(risk_level({'DXVK_NVAPI_GPU_ARCH': 'GB200'}, has_nvidia=False), 'incompatible')

    def test_disabled_settings_ignored(self):
        self.assertEqual(risk_level({'PROTON_USE_NTSYNC': '0'}, has_ntsync=False), 'standard')


class DiagnosticsTests(unittest.TestCase):
    def test_serializable_and_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            out = gather(home=Path(d), env={'XDG_SESSION_TYPE': 'wayland'})
            self.assertEqual(out['session'], 'wayland')
            self.assertEqual(json.loads(to_json(home=Path(d)))['python'], out['python'])
            self.assertFalse(out['profiles_file_exists'])


if __name__ == '__main__':
    unittest.main()

class ChangePlanTests(unittest.TestCase):
    def test_nested_changes(self):
        from drstool_core.change_plan import plan, render
        changes = plan({'env_vars': {'A': '1'}, 'arch': 'GB200'},
                       {'env_vars': {'A': '2', 'B': 'x'}, 'arch': None})
        self.assertEqual(len(changes), 3)
        self.assertIn('env_vars.A', render(changes))
        self.assertEqual([x.path for x in changes], sorted(x.path for x in changes))

    def test_no_changes(self):
        from drstool_core.change_plan import plan
        self.assertEqual(plan({'X': {'Y': 1}}, {'X': {'Y': 1}}), [])

class TransactionReportTests(unittest.TestCase):
    def test_no_paths_leak_and_status_recovered(self):
        from drstool_core.diagnostics import transaction_status
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'drstool/dll-backups/private-game/20261103'
            p.mkdir(parents=True)
            (p / 'manifest.json').write_text(json.dumps({'state': 'recovery-needed',
                                                          'entries': [{'target': '/secret/private.dll'}]}))
            report = transaction_status(Path(d))
            self.assertEqual(len(report), 1)
            self.assertTrue(report[0]['needs_attention'])
            self.assertNotIn('private-game', str(report))
            self.assertNotIn('/secret', str(report))

class TranslationTests(unittest.TestCase):
    def test_technical_identifiers_preserved(self):
        from drstool_core.i18n import translate, language_tag
        self.assertEqual(translate('VK_KHR_present_wait', 'tr_TR'), 'VK_KHR_present_wait')
        self.assertEqual(translate('Save', 'tr_TR'), 'Kaydet')
        self.assertEqual(translate('Save', 'en_US'), 'Save')
        self.assertEqual(language_tag('auto', 'tr_TR'), 'tr')
