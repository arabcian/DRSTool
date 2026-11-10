"""Pure-logic tests for drstool_gui modules that need no window: linter, launch-line
parser, undo timeline, .nip import, frametime stats, log rules, Lutris YAML merge,
GPU generation detection, and the shared core helpers.

    QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drstool_core import fsutil, launcher_paths  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def setUpModule():
    # drstool_gui is imported here, not at discovery time: test_gui needs to import it
    # only after it has set up its fake home.
    global frame_stats, parse_launch_text, lint_config, _log_analyse, detect_real_arch
    global LutrisSyncWidget, parse_nip, UndoTimeline, SysInfo
    from drstool_gui.frametime import frame_stats
    from drstool_gui.launch_import import parse_launch_text
    from drstool_gui.lint import lint_config
    from drstool_gui.logs import _log_analyse
    from drstool_gui.lookup import detect_real_arch
    from drstool_gui.lutris_sync import LutrisSyncWidget
    from drstool_gui.nip_import import parse_nip
    from drstool_gui.state import UndoTimeline
    from drstool_gui.sysinfo import SysInfo


def codes(env=None, drs=None, arch="", gs_on=False, gs_flags=None, mangohud=False, si=None):
    return {i.code for i in lint_config(env or {}, drs or {}, arch, gs_on, gs_flags or {}, mangohud, si)}


class GpuGenerationTests(unittest.TestCase):
    CASES = [
        ("NVIDIA GeForce RTX 5090", "GB200"),
        ("NVIDIA RTX PRO 6000 Blackwell Workstation Edition", "GB200"),
        ("NVIDIA GeForce RTX 4070 Ti", "AD100"),
        ("NVIDIA RTX 5000 Ada Generation", "AD100"),
        ("NVIDIA RTX 2000 Ada Generation Laptop GPU", "AD100"),
        ("NVIDIA RTX 3500 Ada Generation Laptop GPU", "AD100"),
        ("NVIDIA GeForce RTX 3080", "GA100"),
        ("NVIDIA RTX A5000", "GA100"),
        ("Quadro RTX 5000", "TU100"),
        ("Quadro RTX 4000", "TU100"),
        ("NVIDIA GeForce RTX 2060", "TU100"),
        ("NVIDIA GeForce GTX 1660 SUPER", "TU100"),
        ("NVIDIA TITAN RTX", "TU100"),
        ("NVIDIA GeForce GTX 1080 Ti", "GP100"),
        ("NVIDIA GeForce GTX 750 Ti", "GM000"),
        ("NVIDIA GeForce GTX 970", "GM200"),
        ("AMD Radeon RX 7900 XTX", ""),
    ]

    def test_model_names(self):
        for name, want in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(detect_real_arch(SysInfo(nv_gpus=[name])), want)


class LintTests(unittest.TestCase):
    # (code, env that triggers it, env that must not)
    ENV_RULES = [
        ("nvlibs-wow64", {"PROTON_NVIDIA_LIBS": "1", "PROTON_USE_WOW64": "1"}, {"PROTON_NVIDIA_LIBS": "1"}),
        ("nvlibs-no32-orphan", {"PROTON_NVIDIA_LIBS_NO_32BIT": "1"},
         {"PROTON_NVIDIA_LIBS_NO_32BIT": "1", "PROTON_NVIDIA_LIBS": "1"}),
        ("hdr-contradiction", {"DXVK_HDR": "1", "DXVK_NO_HDR": "1"}, {"DXVK_HDR": "1"}),
        ("hdr-no-path", {"DXVK_HDR": "1"}, {"DXVK_HDR": "1", "PROTON_ENABLE_WAYLAND": "1"}),
        ("wayland-vs-x11", {"PROTON_ENABLE_WAYLAND": "1", "SDL_VIDEODRIVER": "x11"},
         {"PROTON_ENABLE_WAYLAND": "1", "SDL_VIDEODRIVER": "wayland"}),
        ("wined3d-dxvk", {"PROTON_USE_WINED3D": "1", "DXVK_HUD": "fps"}, {"PROTON_USE_WINED3D": "1"}),
        ("no-d3d12-vkd3d", {"PROTON_NO_D3D12": "1", "VKD3D_DEBUG": "none"}, {"PROTON_NO_D3D12": "1"}),
        ("no-d3d11-dxvk", {"PROTON_NO_D3D11": "1", "DXVK_HUD": "fps"}, {"PROTON_NO_D3D11": "1"}),
        ("nvapi-both", {"PROTON_ENABLE_NVAPI": "1", "PROTON_DISABLE_NVAPI": "1"}, {"PROTON_ENABLE_NVAPI": "1"}),
        ("nvapi-hidden", {"PROTON_ENABLE_NVAPI": "1", "PROTON_HIDE_NVIDIA_GPU": "1"},
         {"PROTON_ENABLE_NVAPI": "1", "PROTON_HIDE_NVIDIA_GPU": "0"}),
        ("vkreflex-both", {"DXVK_NVAPI_VKREFLEX": "1", "DISABLE_DXVK_NVAPI_VKREFLEX": "1"},
         {"DXVK_NVAPI_VKREFLEX": "1"}),
        ("latency-stack", {"DXVK_NVAPI_VKREFLEX": "1", "LOW_LATENCY_LAYER": "1"}, {"LOW_LATENCY_LAYER": "1"}),
        ("ntsync-both", {"PROTON_USE_NTSYNC": "1", "PROTON_NO_NTSYNC": "1"}, {"PROTON_USE_NTSYNC": "1"}),
        ("prime-mixed", {"__NV_PRIME_RENDER_OFFLOAD": "1", "DRI_PRIME": "1"}, {"__NV_PRIME_RENDER_OFFLOAD": "1"}),
        ("multi-limiter", {"DXVK_FRAME_RATE": "60", "VKD3D_FRAME_RATE": "60"}, {"DXVK_FRAME_RATE": "60"}),
        ("async-gpl", {"DXVK_ASYNC": "1"}, {"DXVK_ASYNC": "0"}),
        ("optiscaler-upgrades", {"PROTON_USE_OPTISCALER": "1", "PROTON_DLSS_UPGRADE": "1"},
         {"PROTON_USE_OPTISCALER": "1"}),
        ("gswsi-both", {"ENABLE_GAMESCOPE_WSI": "1", "DISABLE_GAMESCOPE_WSI": "1"}, {"ENABLE_GAMESCOPE_WSI": "1"}),
        ("gs-env-no-gs", {"GAMESCOPE_WAYLAND_DISPLAY": "x"}, {}),
        ("hud-twice", {"DXVK_HUD": "fps", "MANGOHUD": "1"}, {"DXVK_HUD": "fps"}),
    ]

    def test_env_rules_trigger_and_stay_quiet(self):
        for code, hit, miss in self.ENV_RULES:
            with self.subTest(code=code):
                self.assertIn(code, codes(hit))
                self.assertNotIn(code, codes(miss))

    def test_rules_that_need_system_info(self):
        nv = SysInfo(probed=True, nv_driver="595.10", nv_gpus=["NVIDIA GeForce RTX 4080"])
        self.assertIn("fsr4-nvidia", codes({"PROTON_FSR4_UPGRADE": "1"}, si=nv))
        self.assertIn("hdr-wsi-595", codes({"ENABLE_HDR_WSI": "1"}, si=nv))
        self.assertNotIn("hdr-wsi-595", codes({"ENABLE_HDR_WSI": "1"},
                                              si=SysInfo(probed=True, nv_driver="580.1")))
        self.assertIn("ntsync-missing", codes({"PROTON_USE_NTSYNC": "1"}, si=SysInfo(probed=True)))
        self.assertNotIn("ntsync-missing", codes({"PROTON_USE_NTSYNC": "1"},
                                                 si=SysInfo(probed=True, ntsync_dev=True)))

    def test_gamescope_and_mangohud_rules(self):
        self.assertIn("hdr-gs-flag", codes({"DXVK_HDR": "1"}, gs_on=True))
        self.assertNotIn("hdr-gs-flag", codes({"DXVK_HDR": "1"}, gs_on=True, gs_flags={"--hdr-enabled": ""}))
        self.assertIn("multi-limiter", codes({"DXVK_FRAME_RATE": "60"}, gs_on=True,
                                             gs_flags={"--framerate-limit": "60"}))
        self.assertIn("mangohud-twice", codes({"MANGOHUD": "1"}, mangohud=True))

    def test_arch_spoof_against_real_card(self):
        si = SysInfo(probed=True, nv_gpus=["NVIDIA GeForce RTX 3080"])
        self.assertIn("arch-redundant", codes(arch="GA100", si=si))
        self.assertIn("arch-newer", codes(arch="AD100", si=si))
        self.assertIn("arch-older", codes(arch="TU100", si=si))

    def test_sorted_by_severity(self):
        issues = lint_config({"DXVK_ASYNC": "1", "PROTON_ENABLE_NVAPI": "1", "PROTON_DISABLE_NVAPI": "1"},
                             {}, "", False, {}, False)
        self.assertEqual(issues[0].sev, "error")


class LaunchLineTests(unittest.TestCase):
    def test_steam_launch_options(self):
        p = parse_launch_text("PROTON_ENABLE_NVAPI=1 DXVK_HUD=fps mangohud %command%")
        self.assertEqual(p.env.get("PROTON_ENABLE_NVAPI"), "1")
        self.assertEqual(p.env.get("DXVK_HUD"), "fps")
        self.assertEqual((p.mangohud, p.env.get("MANGOHUD")), (False, "1"))   # outside gamescope → env var
        self.assertTrue(parse_launch_text("gamescope -- mangohud %command%").mangohud)

    def test_gamescope_prefix(self):
        p = parse_launch_text("gamescope -W 2560 -H 1440 --hdr-enabled -- %command%")
        self.assertTrue(p.gs_on)
        self.assertIn("--hdr-enabled", p.gs_flags)

    def test_export_lines_and_empty(self):
        p = parse_launch_text("export DXVK_HUD=fps\nexport PROTON_USE_WOW64=1\n")
        self.assertEqual(p.env, {"DXVK_HUD": "fps", "PROTON_USE_WOW64": "1"})
        self.assertTrue(parse_launch_text("   ").empty())


class UndoTimelineTests(unittest.TestCase):
    def _st(self, v):
        return {"settings": {}, "arch": None, "env": {"A": v}, "gs_on": False, "gs_flags": {}, "mangohud": False}

    def test_undo_redo_and_coalescing(self):
        t = UndoTimeline(self._st("0"))
        t.push(self._st("1"), "edit", key="A")
        t.push(self._st("2"), "edit", key="A")          # same key, within COALESCE_S → one step
        self.assertTrue(t.can_undo())
        self.assertEqual(t.undo()["env"]["A"], "0")
        self.assertFalse(t.can_undo())
        self.assertEqual(t.redo()["env"]["A"], "2")
        self.assertFalse(t.can_redo())

    def test_limit(self):
        t = UndoTimeline(self._st("0"))
        for i in range(UndoTimeline.LIMIT + 20):
            t.push(self._st(str(i)), "edit", key=f"k{i}")
        self.assertLessEqual(len(t.entries()), UndoTimeline.LIMIT)


class NipTests(unittest.TestCase):
    NIP = """<?xml version="1.0" encoding="utf-16"?>
<ArrayOfProfile><Profile><ProfileName>Game</ProfileName>
<Executeables><string>game.exe</string></Executeables>
<Settings><ProfileSetting><SettingNameInfo>Frame Rate Limiter</SettingNameInfo>
<SettingID>277041154</SettingID><SettingValue>0x3C</SettingValue><ValueType>Dword</ValueType>
</ProfileSetting><ProfileSetting><SettingID>bad</SettingID></ProfileSetting></Settings>
</Profile></ArrayOfProfile>"""

    def test_parse(self):
        [p] = parse_nip(self.NIP.replace('encoding="utf-16"', ""))
        self.assertEqual((p["name"], p["exes"]), ("Game", ["game.exe"]))
        self.assertEqual([(s["id"], s["value"]) for s in p["settings"]], [(277041154, 60)])

    def test_rejects_entities(self):
        with self.assertRaises(ValueError):
            parse_nip('<!DOCTYPE x [<!ENTITY a "b">]><ArrayOfProfile/>')


class FrameStatsTests(unittest.TestCase):
    def test_values(self):
        ft = [10.0] * 990 + [60.0] * 10
        st = {label: v for label, v, *_ in frame_stats(ft)}
        self.assertEqual(st["Frames"], 1000)
        self.assertAlmostEqual(st["1% low FPS"], 1000 / 60)
        self.assertEqual(st["Median frametime (ms)"], 10.0)
        self.assertEqual(st["Hitches (> 50 ms)"], 10)
        self.assertEqual(st["Stutters (> 2× median)"], 10)


class LogRuleTests(unittest.TestCase):
    def test_rules_match(self):
        lines = ["err:module:import_dll Library nvcuda.dll (which is needed by L\"x\") not found",
                 "info: everything fine", "VK_ERROR_DEVICE_LOST"]
        levels, hits = _log_analyse(lines)
        self.assertEqual(len(levels), 3)
        self.assertIn("nvcuda", hits)
        self.assertIn("device-lost", hits)
        self.assertNotIn("oom", hits)


class LutrisMergeTests(unittest.TestCase):
    def test_keeps_hand_written_keys(self):
        original = ("game:\n  exe: /games/x.exe\nsystem:\n  env:\n    MY_OWN: keep\n"
                    "  prefix_command: taskset\nwine:\n  version: ge\n")
        text, *_ = LutrisSyncWidget._merge_yaml(original, {}, {"DXVK_HUD": "fps"})
        import yaml
        d = yaml.safe_load(text)
        self.assertEqual(d["game"]["exe"], "/games/x.exe")
        self.assertEqual(d["wine"]["version"], "ge")
        self.assertEqual(d["system"]["env"]["DXVK_HUD"], "fps")
        self.assertEqual(d["system"]["env"].get("MY_OWN"), "keep")


class CoreHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_atomic_write_keeps_mode_and_follows_symlink(self):
        real = self.root / "real.json"
        real.write_text("old")
        real.chmod(0o600)
        link = self.root / "link.json"
        link.symlink_to(real)
        fsutil.atomic_write(link, "new")
        fsutil.atomic_write(self.root / "sub" / "b.bin", b"\x00\x01")
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), "new")
        self.assertEqual(real.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "sub" / "b.bin").read_bytes(), b"\x00\x01")
        self.assertEqual([p.name for p in self.root.iterdir() if "drstool-tmp" in p.name], [])

    def test_snap_steam_root_and_appmanifests(self):
        root = self.root / "snap/steam/common/.steam/root"
        (root / "steamapps").mkdir(parents=True)
        (root / "steamapps" / "appmanifest_10.acf").write_text(
            '"AppState"\n{\n "appid" "10"\n "name" "Counter-Strike"\n "installdir" "Half-Life"\n}\n')
        self.assertEqual(launcher_paths.steam_roots(self.root), [root.resolve()])
        [lib] = launcher_paths.steam_libraries(root)
        self.assertEqual(list(launcher_paths.steam_apps(lib)), [("10", "Counter-Strike", "Half-Life")])


class RepoConsistencyTests(unittest.TestCase):
    def test_readme_env_var_count(self):
        n = sum(len(v) for v in json.loads((ROOT / "drstool_core/data/env_vars.json").read_text()).values())
        for readme in ("README.md", "README.tr.md"):
            with self.subTest(readme=readme):
                m = re.search(r"(\d+) (?:variables|değişkeni)", (ROOT / readme).read_text(encoding="utf-8"))
                self.assertEqual(int(m.group(1)), n)

    def test_tm_tools_json_unique_ids(self):
        tools = json.loads((ROOT / "drstool_core/data/tm_tools.json").read_text())
        ids = [t["id"] for t in tools]
        self.assertEqual(len(ids), len(set(ids)))

    def test_user_tm_tools_merge(self):
        from drstool_gui import tool_manager, user_catalog
        extra = [{"id": "my-proton", "name": "My Proton", "kind": "proton", "desc": "", "url": "",
                  "src": "github"}, {"id": "ge-proton", "name": "dup", "kind": "proton", "desc": "",
                                     "url": "", "src": "github"}, {"bogus": 1}]
        with mock.patch.object(tool_manager, "user_tm_tools", return_value=extra):
            tools = tool_manager._load_tm_tools()
        ids = [t.id for t in tools]
        self.assertIn("my-proton", ids)
        self.assertEqual(ids.count("ge-proton"), 1)
        self.assertEqual(next(t for t in tools if t.id == "ge-proton").name, "GE-Proton")
        self.assertTrue(callable(user_catalog.user_tm_tools))


if __name__ == "__main__":
    unittest.main()
