"""Layered profiles, bisect, perf history, AWACY/umu parsing helpers, sensor summary."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
