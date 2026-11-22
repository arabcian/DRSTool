"""Layered profiles, bisect, perf history, AWACY/umu parsing helpers, sensor summary."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import _home  # noqa: E402,F401 - fake HOME before drstool_gui (and with it QSettings) is loaded

try:
    from drstool_gui import state
    from drstool_gui.frametime import _parse_frametimes
    from drstool_gui.live_process import drs_ids_in_logs
except ImportError:                                  # PySide6 missing
    state = None
from drstool_core import game_links


@unittest.skipIf(state is None, "PySide6 not installed")
class LayerTests(unittest.TestCase):
    base = {"settings": {"0x10E41DF3": "0x1"}, "arch": "AD100", "env_vars": {"A": "1", "B": "2"},
            "gamescope_enabled": True, "gamescope_flags": {"-W": "1920"}}

    def test_diff_roundtrip(self):
        full = {"settings": {"0x10E41DF3": "0x1"}, "arch": "AD100", "env_vars": {"A": "1", "C": "3"},
                "gamescope_enabled": False, "gamescope_flags": {}, "notes": "why"}
        child = dict(state.layer_diff(self.base, full), base="b", notes="why")
        got = state.resolve_layers({"b": self.base, "c": child}, "c")
        self.assertEqual(state.canon_flat(got), state.canon_flat(full))
        self.assertEqual(got["notes"], "why")

    def test_cycle_stops(self):
        ps = {"a": {"base": "b", "env_vars": {"X": "1"}}, "b": {"base": "a", "env_vars": {"Y": "1"}}}
        self.assertEqual(state.base_chain(ps, "a"), ["a", "b"])
        self.assertEqual(state.resolve_layers(ps, "a")["env_vars"], {"X": "1", "Y": "1"})

    def test_bisect_state(self):
        st = state.state_from_profile(self.base)
        items = state.bisect_items(st)
        self.assertEqual(len(items), 4)
        half = state.bisect_state(st, [("env", "A")])
        self.assertEqual(half["env"], {"A": "1"})
        self.assertFalse(half["settings"] or half["gs_on"])
        self.assertEqual(half["arch"], st["arch"])


@unittest.skipIf(state is None, "PySide6 not installed")
class RunCommandTests(unittest.TestCase):
    """drstool --run PROFILE -- COMMAND"""
    prof = {"settings": {"0x10E41DF3": "0x1"}, "arch": "AD100",
            "env_vars": {"DXVK_HUD": "a b'c $HOME", "EMPTY": ""},
            "gamescope_enabled": True,
            "gamescope_flags": {"-W": "1920", "--hdr-enabled": "", "mangohud": "mangohud --dlsym"}}

    def test_env_is_literal_and_wrappers_come_first(self):
        from drstool_gui import cli
        env, argv = cli.run_argv(self.prof, ["game.exe", "--", "-dx12"])
        self.assertEqual(env, {"DXVK_NVAPI_GPU_ARCH": "AD100", "DXVK_NVAPI_DRS_SETTINGS": "0x10E41DF3=0x1",
                               "DXVK_HUD": "a b'c $HOME"})
        self.assertEqual(argv, ["gamescope", "--output-width", "1920", "--hdr-enabled", "--",
                                "mangohud", "--dlsym", "game.exe", "--", "-dx12"])
        self.assertEqual(cli.run_argv({"env_vars": {"A": "1"}}, ["x"]), ({"A": "1"}, ["x"]))

    def test_invalid_env_names_from_profile_file_are_dropped(self):
        # A hand-edited / imported profile must not reach execvpe (ValueError) or a launch line.
        from drstool_gui import cli
        env, _argv = cli.run_argv({"env_vars": {"X=1; curl x|sh #": "1", "A=B": "2", "OK": "3"}}, ["x"])
        self.assertEqual(env, {"OK": "3"})

    def test_file_import_reports_sensitive_env(self):
        from drstool_gui.profile_io import import_flags
        profs = {"P": {"env_vars": {"LD_PRELOAD": "/tmp/x.so", "DXVK_HUD": "fps"}},
                 "Q": {"env_vars": {"DXVK_HUD": "fps"}}}
        self.assertEqual(set(import_flags(profs)), {"P"})
        self.assertIn("LD_PRELOAD", import_flags(profs)["P"])

    def _exec(self, argv, profiles):
        from unittest import mock
        from drstool_gui import cli
        sm = mock.Mock()
        sm.get_profiles.return_value = profiles
        with mock.patch.object(cli, "SettingsManager", return_value=sm), \
                mock.patch.object(cli.os, "execvpe") as ex, mock.patch.object(cli.sys, "stderr"):
            try:
                cli.cli_exec(argv)
            except SystemExit as stop:
                return stop.code, ex
        return None, ex

    def test_exec_resolves_the_profile_and_keeps_the_environment(self):
        os.environ["DRSTOOL_TEST_KEEP"] = "kept"
        self.addCleanup(os.environ.pop, "DRSTOOL_TEST_KEEP", None)
        code, ex = self._exec(["--run", "my", "--", "true", "--lint"], {"My Game": {"env_vars": {"A": "1"}}})
        self.assertIsNone(code)
        file, argv, env = ex.call_args.args
        self.assertEqual((file, argv), ("true", ["true", "--lint"]))
        self.assertEqual((env["A"], env["DRSTOOL_TEST_KEEP"]), ("1", "kept"))

    def test_bad_usage_and_unknown_profile_never_exec(self):
        for argv in (["--run", "P"], ["--run", "P", "true"], ["--run", "P", "--"], ["--run", "nope", "--", "true"]):
            code, ex = self._exec(argv, {"P": {"env_vars": {"A": "1"}}})
            self.assertEqual(code, 2, argv)
            ex.assert_not_called()
        code, ex = self._exec(["--lint", "P"], {})          # not ours: falls through to the normal CLI
        self.assertIsNone(code)
        ex.assert_not_called()


@unittest.skipIf(state is None, "PySide6 not installed")
class ToolManagerGetTests(unittest.TestCase):
    """Checksums and API metadata come through _tm_get: same HTTPS rule as the archive."""

    def _get(self, url, final_url, body=b"ok"):
        import io
        from unittest import mock
        from drstool_gui import tool_manager as tm
        reply = io.BytesIO(body)
        reply.url = final_url
        with mock.patch.object(tm.net, "open_url", return_value=reply):
            return tm._tm_get(url)

    def test_https_ok(self):
        self.assertEqual(self._get("https://a.invalid/x", "https://a.invalid/x"), b"ok")

    def test_plain_http_and_http_redirect_refused(self):
        with self.assertRaises(RuntimeError):
            self._get("http://a.invalid/x", "http://a.invalid/x")
        with self.assertRaises(RuntimeError):
            self._get("https://a.invalid/x", "http://b.invalid/x")

    def test_oversized_reply_refused(self):
        from drstool_gui import tool_manager as tm
        with self.assertRaises(RuntimeError):
            self._get("https://a.invalid/x", "https://a.invalid/x", b"x" * (tm._TM_MAX_REPLY + 1))


class PerfHistoryTests(unittest.TestCase):
    def test_tagging_and_dedupe(self):
        with tempfile.TemporaryDirectory() as d:
            s = game_links.LinkStore(Path(d) / "l.json")
            s.record("steam:1", name="G", launcher="Steam", target={}, profile="P", fp="f1")
            at = s.get("steam:1")["applied_at"]
            self.assertTrue(s.add_perf("steam:1", {"path": "x", "mtime": at + 5, "avg": 60}))
            self.assertFalse(s.add_perf("steam:1", {"path": "x", "mtime": at + 5, "avg": 60}))
            s.add_perf("steam:1", {"path": "y", "mtime": at - 5, "avg": 50})
            fps = {r["path"]: r["fp"] for r in s.get("steam:1")["perf"]}
            self.assertEqual(fps, {"x": "f1", "y": ""})


@unittest.skipIf(state is None, "PySide6 not installed")
class SensorAndLogTests(unittest.TestCase):
    def test_mangohud_sensors(self):
        head = ["os,cpu,gpu", "Linux,x,y", "fps,frametime,gpu_load,gpu_temp,gpu_vram_used"]
        rows = [f"60,{16.6 if i % 10 else 50},90,70,{7.9 if i % 10 == 0 else 6.0}" for i in range(40)]
        d = _parse_frametimes("t.csv", head, iter(rows))
        self.assertEqual(len(d["sensors"]["VRAM"]), 40)
        self.assertEqual(d["extra"]["stutters at ≥95% peak VRAM"], "4/4")

    def test_drs_ids_in_logs(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dxvk-nvapi.log"
            p.write_text("info: DRS 0x10E41DF3 set\n")
            self.assertEqual(drs_ids_in_logs("0x10E41DF3=1,0x00000001=2", [p]), (["0x10E41DF3"], ["0x00000001"]))


if __name__ == "__main__":
    unittest.main()
