"""Conservative F02/F04/F09 rules: no graphics devices or Qt required."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from drstool_core.launch_preflight import evaluate
from drstool_core.pacing_advisor import advise
from drstool_core.compatibility import assess, version_tuple, rules


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.fake_which = lambda exe, path=None: f'/usr/bin/{exe}' if exe == 'python3' else None

    def check(self, payload=None, **kwargs):
        return evaluate(payload or {'env': {}, 'gs_on': False}, env_base={'PATH': '/usr/bin'},
                        which=self.fake_which, **kwargs)

    def test_no_command_is_unknown_not_error(self):
        r = self.check()
        self.assertEqual(r['status'], 'ready-unverified')
        self.assertEqual(r['counts']['error'], 0)
        self.assertTrue(any(f['code'] == 'launch-command-unknown' for f in r['findings']))

    def test_real_missing_command_blocks(self):
        r = self.check(command=['missing-example-binary-99'])
        self.assertEqual(r['status'], 'blocked')
        self.assertTrue(any(f['code'] == 'executable-not-in-path' for f in r['findings']))

    def test_known_executable(self):
        r = self.check(command=['python3'])
        self.assertNotEqual(r['status'], 'blocked')

    def test_wrapper_gamescope_absent(self):
        r = self.check({'env': {}, 'gs_on': True}, command=['python3'])
        self.assertTrue(any(f['code'] == 'gamescope-unavailable' for f in r['findings']))
        self.assertEqual(r['status'], 'blocked')

    def test_prefix_may_be_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.check(prefix=str(Path(tmp) / 'not-existing'), command=['python3'])
        self.assertEqual(r['status'], 'review')
        self.assertTrue(any(f['code'] == 'prefix-not-created' for f in r['findings']))

    def test_prefix_file_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'prefix'; target.write_text('file')
            r = self.check(prefix=str(target), command=['python3'])
        self.assertEqual(r['status'], 'blocked')

    def test_preload_missing_absolute_only_and_no_raw_secrets(self):
        r = self.check({'env': {'LD_PRELOAD': '/nonexistent-secret/lib.so libnvidia.so'}, 'gs_on': False})
        self.assertTrue(any(f['code'] == 'preload-missing' for f in r['findings']))
        self.assertNotIn('nonexistent-secret', json.dumps(r))

    def test_bad_layer_dir(self):
        r = self.check({'env': {'VK_LAYER_PATH': '/very/unlikely/nonexistent/path'}, 'gs_on': False})
        self.assertTrue(any(f['code'] == 'layer-dir-missing' for f in r['findings']))

    def test_steam_command_placeholder_unknown(self):
        r = self.check(command=['%command%'])
        self.assertEqual(r['status'], 'ready-unverified')

    def test_real_env_command_conflict(self):
        r = self.check({'env': {'PROTON_ENABLE_NVAPI': '1', 'PROTON_DISABLE_NVAPI': '1'}})
        self.assertTrue(any(f['code'] == 'nvapi-conflicting-switches' for f in r['findings']))


class PacingTests(unittest.TestCase):
    def test_caps_are_not_measured_fps(self):
        r = advise({'env': {'FLM_TARGET_FPS': '65', 'FLM_MFG_MULTIPLIER': '3'},
                    'gs_on': False}, refresh_hz=240, vrr_min=72, vrr_max=240)
        self.assertNotIn('estimated_display_fps', [x['key'] for x in r['observations']])
        self.assertTrue(any(x['code'] == 'display-cadence-unknown' for x in r['findings']))

    def test_explicit_base_fps_times_forced_mfg_is_hypothetical(self):
        r = advise({'env': {'FLM_MFG_MULTIPLIER': '3'}}, base_fps=65,
                   refresh_hz=240, vrr_min=72, vrr_max=240)
        self.assertEqual(next(x['value'] for x in r['observations']
                              if x['key'] == 'estimated_display_fps'), 195)
        self.assertFalse(any(x['code'] == 'estimated-outside-vrr' for x in r['findings']))
        self.assertTrue(any(x['code'] == 'mfg-estimate' for x in r['findings']))

    def test_output_outside_vrr(self):
        r = advise({'env': {'FLM_MFG_MULTIPLIER': '4'}}, base_fps=70,
                   refresh_hz=240, vrr_min=72, vrr_max=240)
        self.assertTrue(any(x['code'] == 'estimated-outside-vrr' for x in r['findings']))
        self.assertTrue(any(x['code'] == 'estimated-above-refresh' for x in r['findings']))

    def test_auto_mfg_never_assumes_multiplier(self):
        r = advise({'env': {'FLM_MFG_MULTIPLIER': '0'}}, base_fps=65, refresh_hz=240)
        self.assertNotIn('estimated_display_fps', [x['key'] for x in r['observations']])

    def test_multiple_limiters(self):
        r = advise({'env': {'DXVK_FRAME_RATE': '60', 'FLM_TARGET_FPS': '90'},
                    'gs_on': True, 'gs_flags': {'--framerate-limit': '100'}})
        self.assertTrue(any(x['code'] == 'multiple-limiters' for x in r['findings']))

    def test_gamescope_refresh_is_not_a_limiter(self):
        r = advise({'env': {}, 'gs_on': True, 'gs_flags': {'-r': '240'}})
        self.assertEqual(r['observations'][0]['value'], {})

    def test_invalid_inputs_do_not_crash(self):
        r = advise({'env': {'FLM_MFG_MULTIPLIER': 'nan'}}, base_fps=float('nan'),
                   refresh_hz=0, vrr_min=144, vrr_max=72)
        self.assertTrue(any(x['code'] == 'mfg-invalid' for x in r['findings']))
        self.assertTrue(any(x['code'] == 'vrr-bounds-invalid' for x in r['findings']))

    def test_cap_without_target(self):
        r = advise({'env': {'FLM_MODE': 'cap', 'FLM_TARGET_FPS': '0'}})
        self.assertTrue(any(x['code'] == 'flm-cap-without-target' for x in r['findings']))


class CompatibilityTests(unittest.TestCase):
    def test_metadata_is_shipped(self):
        self.assertIn('DXVK_FRAME_RATE', rules())

    def test_upstream_removed(self):
        r = assess({'env': {'DXVK_FRAME_RATE': '60'}}, versions={'dxvk': '3.0'},
                   builds={'dxvk': 'upstream'})
        self.assertEqual(r['items'][0]['status'], 'unsupported')

    def test_old_supported_only_by_catalog(self):
        r = assess({'env': {'DXVK_FRAME_RATE': '60'}}, versions={'dxvk': '2.7'},
                   builds={'dxvk': 'upstream'})
        self.assertEqual(r['items'][0]['status'], 'supported-by-catalog')

    def test_unknown_and_fork_not_conflated(self):
        for build in ('fork', 'unknown'):
            r = assess({'env': {'DXVK_FRAME_RATE': '60'}}, versions={'dxvk': '3.0'},
                       builds={'dxvk': build})
            self.assertEqual(r['items'][0]['status'], 'unknown')

    def test_uncovered_version_gap_is_unknown(self):
        r = assess({'env': {'DXVK_FRAME_RATE': '60'}}, versions={'dxvk': '2.9'},
                   builds={'dxvk': 'upstream'})
        self.assertEqual(r['items'][0]['status'], 'unknown')

    def test_fork_only_flag_not_upstream(self):
        r = assess({'env': {'DXVK_ASYNC': '1'}}, builds={'dxvk': 'upstream'})
        self.assertEqual(r['items'][0]['status'], 'unsupported')

    def test_unmapped_is_unknown(self):
        r = assess({'env': {'FUTURE_FLAG': '1'}})
        self.assertEqual(r['items'][0]['status'], 'unknown')

    def test_parse_version_rejects_vague(self):
        self.assertEqual(version_tuple('v3.0.1'), (3, 0, 1))
        self.assertIsNone(version_tuple('dxvk-3.0'))
        self.assertIsNone(version_tuple(''))


if __name__ == '__main__':
    unittest.main()
