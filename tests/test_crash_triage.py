"""drstool_core.crash_triage (no Qt)."""
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drstool_core import crash_triage  # noqa: E402

# Lines from a Crimson Desert run (2026-11-23) that exited cleanly with VKD3D_CONFIG empty.
CLEAN_RUN = [
    "13364.120:0020:0024:info:vkd3d-proton:vkd3d_config_flags_init_once: VKD3D_CONFIG=''.",
    "(2026-11-23 18:34:08) (0x440 ) [DATA_ERROR] [Success](0) [Error: [18-34-08][streamline][error][tid:420]"
    "[18s:899ms:676us]generic.cpp:1408[setReflexSyncFG] NvAPI_D3D_SetReflexSync((IUnknown*)m_typelessDevice, "
    "(NV_SET_REFLEX_SYNC_PARAMS*)&m_cachedReflexSyncParams) failed error -3",
    "(2026-11-23 18:34:08) (0x440 ) [DATA_ERROR] [Success](0) [Error: [18-34-08][streamline][error][tid:420]"
    "[18s:899ms:791us]rsync.cpp:660[setDynamicMFGParams] RSYNC: setDynamicMFGParams failed with status 1",
    "(2026-11-23 18:34:33) (0x440 ) [DATA_ERROR] [Success](0) [Error: [18-34-33][streamline][error][tid:36]"
    "[43s:967ms:869us]rsync.cpp:607[setReflexTiming] RSYNC: setReflexTiming failed with status 1",
    "Monitored process exited.",
    "Exit with return code 0",
]

CRASH_RUN = [
    "Started initial process 1 from /usr/lib/wine-cachyos-11.0.20261107/bin/wine X:/CrimsonDesert.exe",
    "(2026-11-23 18:00:00) (0x36  ) [I] [Success](0) [GameVersion: 1.14.00]]",
    "13364.120:0020:0024:info:vkd3d-proton:vkd3d_config_flags_init_once: VKD3D_CONFIG='dxr,force_static_cbv'.",
    "13364.120:0020:0024:info:vkd3d-proton:dxgi_vk_swap_chain_recreate_swapchain_in_present_task: Got 1 swapchain images.",
    CLEAN_RUN[2],
    "(2026-11-23 18:01:30) (0x440 ) [E] [Fail](0) [We've lost the Device]]",
    "Exit with return code 1",
]

XID_69 = ("NVRM: Xid (PCI:0000:01:00): 69, pid=633300, name=CrimsonDesert.e, Class Error: ChId 0017, "
          "Class 0000ce97, Offset 000017c8, Data 00000000, ErrorCode 0000000c")


class CrashTriageTests(unittest.TestCase):
    def test_fingerprint_context_and_window(self):
        r = crash_triage.triage(CRASH_RUN)
        self.assertTrue(r["crashed"])
        self.assertEqual(r["fingerprint"], {
            "runner": "wine-cachyos-11.0.20261107", "game_version": "1.14.00",
            "vkd3d_config": "dxr,force_static_cbv", "swapchain_images": "1", "exit_code": "1",
            "duration_s": "90"})
        # noise lines are left out of the context, the fatal line closes it
        self.assertEqual([n for n, _ in r["context"]], [1, 2, 3, 4, 6])
        start, end = crash_triage.kernel_window(r)
        self.assertEqual(end - start, 90 + 30 + 120)
        self.assertIsNone(crash_triage.kernel_window(crash_triage.triage(["no times"])))

    def test_compare_with_clean_run(self):
        rows = crash_triage.compare(crash_triage.triage(CRASH_RUN), crash_triage.triage(CLEAN_RUN))
        self.assertIn(("vkd3d:dxr", "on", None), rows)
        self.assertIn(("vkd3d:force_static_cbv", "on", None), rows)
        self.assertIn(("swapchain_images", "1", None), rows)
        self.assertIn(("exit_code", "1", "0"), rows)
        self.assertNotIn("duration_s", [k for k, *_ in rows])
        text = crash_triage.format_report(crash_triage.triage(CRASH_RUN), clean=crash_triage.triage(CLEAN_RUN),
                                          markdown=True)
        self.assertIn("## Differences from the clean run", text)
        self.assertIn("- vkd3d:dxr: `on` → clean `—`", text)

    def test_rule_suspects_reach_the_result(self):
        rules = [dict(r, suspects=["vkd3d:dxr", "env:VKD3D_SWAPCHAIN_IMAGES"]) if r["id"] == "device-lost" else r
                 for r in crash_triage.RULES]
        with mock.patch.object(crash_triage, "RULES", rules):
            r = crash_triage.triage(CRASH_RUN)
        self.assertEqual(r["suspects"], ["vkd3d:dxr", "env:VKD3D_SWAPCHAIN_IMAGES"])
        self.assertEqual(crash_triage.triage(CLEAN_RUN)["suspects"], [])

    def test_nonzero_exit_without_signature(self):
        self.assertIn("exited with code 3", crash_triage.triage(["Exit with return code 3"])["verdict"])

    def test_clean_run_is_only_noise(self):
        r = crash_triage.triage(CLEAN_RUN)
        self.assertFalse(r["crashed"])
        self.assertEqual([(f["id"], f["kind"], f["count"]) for f in r["findings"]], [("reflex-mfg", "noise", 3)])
        self.assertEqual(r["xid"], [])

    def test_xid_69_with_device_lost(self):
        r = crash_triage.triage(CLEAN_RUN + ["We've lost the Device", XID_69, XID_69])
        self.assertTrue(r["crashed"])
        ids = [f["id"] for f in r["findings"]]
        self.assertEqual(ids[:2], ["device-lost", "xid-69"])       # fatal first, then by line
        self.assertNotIn("xid", ids)                                 # first matching rule only
        self.assertEqual(r["findings"][1]["count"], 2)
        self.assertEqual(r["xid"], [{"xid": 69, "channel": "0x17", "class": "0xce97",
                                     "offset": "0x17c8", "error_code": "0xc"}])
        self.assertIn("Xid 69 channel=0x17 class=0xce97 offset=0x17c8 error_code=0xc",
                      crash_triage.format_report(r))

    def test_other_xid_falls_back_to_generic_rule(self):
        r = crash_triage.triage(["NVRM: Xid (PCI:0000:01:00): 79, pid=1, GPU has fallen off the bus."])
        self.assertEqual([f["id"] for f in r["findings"]], ["xid"])
        self.assertEqual(r["xid"], [{"xid": 79}])

    def test_device_lost_variants(self):
        for line in ("err: VK_ERROR_DEVICE_LOST", "DXGI_ERROR_DEVICE_HUNG", "vkd3d: device lost"):
            self.assertTrue(crash_triage.triage([line])["crashed"], line)


if __name__ == "__main__":
    unittest.main()
