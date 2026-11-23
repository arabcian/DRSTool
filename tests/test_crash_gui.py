"""Bisect dialog (ddmin, resume, undo) and crash triage dialog helpers, offscreen."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import _home  # noqa: E402,F401

try:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QWidget
    from drstool_gui import bisect_dialog, crash_triage_dialog
    from drstool_gui.base import APP_ID
except ImportError:                                  # PySide6 missing
    bisect_dialog = None

FULL = {"settings": {"0x10E41DF3": "0x1"}, "arch": "GB200",
        "env": {"A": "1", "B": "1", "VKD3D_CONFIG": "dxr,force_static_cbv,descriptor_heap"},
        "gs_on": False, "gs_flags": {}}


def crashes(st) -> bool:
    """Fake game: crashes when dxr and B are both on."""
    return "dxr" in st["env"].get("VKD3D_CONFIG", "").split(",") and "B" in st["env"]


@unittest.skipIf(bisect_dialog is None, "PySide6 not installed")
class BisectDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        QSettings(APP_ID, APP_ID).remove("bisect/session")
        win = self.win = QWidget()
        win.state = dict(FULL)
        win.capture_state = lambda: win.state
        win.apply_state = lambda st, label=None: setattr(win, "state", st)

    def answer_until_done(self, dlg):
        for _ in range(50):
            if dlg._search.done:
                return
            dlg._answer(crashes(self.win.state))
        self.fail("search did not finish")

    def test_finds_pair_and_keeps_drs_on(self):
        dlg = bisect_dialog.BisectDialog(self.win)
        self.assertEqual(self.win.state["settings"], FULL["settings"])     # DRS kept on in every test
        self.answer_until_done(dlg)
        self.assertEqual(sorted(dlg._search.outcome[1]), [("env", "B"), ("vkd3d", "dxr")])
        self.assertIn("only fail together", dlg._text.text())
        self.assertFalse(crashes(self.win.state))                           # editor: everything else on
        self.assertEqual(self.win.state["env"]["VKD3D_CONFIG"], "force_static_cbv,descriptor_heap")

    def test_resume_undo_and_restore(self):
        dlg = bisect_dialog.BisectDialog(self.win, priority=["vkd3d:dxr", "env:B"])
        self.assertEqual(dlg._search.items[:2], [("vkd3d", "dxr"), ("env", "B")])
        dlg._answer(crashes(self.win.state))
        dlg._answer(crashes(self.win.state))
        pending, answers = dlg._search.pending, list(dlg._search.answers)
        dlg.close()
        self.win.state = {"settings": {}, "arch": None, "env": {}, "gs_on": False, "gs_flags": {}}
        again = bisect_dialog.BisectDialog(self.win)                       # editor content is ignored
        self.assertEqual((again._search.answers, again._search.pending), (answers, pending))
        self.assertIn("Resumed", again._text.text())
        again._answer(not crashes(self.win.state))                         # misclick
        again._undo_answer()
        self.assertEqual(again._search.pending, pending)
        again._restore()
        self.assertEqual(self.win.state, FULL)
        self.assertEqual(bisect_dialog._load(), {})

    def test_nothing_to_test(self):
        self.win.state = dict(FULL, env={})
        dlg = bisect_dialog.BisectDialog(self.win)
        self.assertIn("No settings to test", dlg._text.text())
        self.assertTrue(dlg._ok.isHidden())

    def test_fixed_settings_alone_crash(self):
        dlg = bisect_dialog.BisectDialog(self.win)
        dlg._answer(True)
        self.assertEqual(dlg._search.outcome, ("fixed", []))
        self.assertIn("Untick", dlg._text.text())
        self.assertEqual(self.win.state, FULL)


@unittest.skipIf(bisect_dialog is None, "PySide6 not installed")
class TriageAnalyseTests(unittest.TestCase):
    def test_kernel_events_are_merged(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "game.log")
            log.write_text("(2026-11-23 18:00:00) start\n(2026-11-23 18:01:00) We've lost the Device\n")
            seen = []

            def collect(start, end):
                seen.append(end - start)
                return {"status": "ok", "reason": "", "events": [
                    {"message": "NVRM: Xid (PCI:0000:01:00): 69, pid=1, Class Error: ChId 0017, Class 0000ce97"}]}
            result, clean, note = crash_triage_dialog.analyse([log], collect=collect)
        self.assertEqual(seen, [60 + 30 + 120])
        self.assertIsNone(clean)
        self.assertEqual(result["xid"], [{"xid": 69, "channel": "0x17", "class": "0xce97"}])
        self.assertIn("1 relevant event", note)

    def test_journal_off_or_no_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "game.log")
            log.write_text("VK_ERROR_DEVICE_LOST\n")
            boom = lambda *a: self.fail("journal must not be read")         # noqa: E731
            self.assertIn("not read", crash_triage_dialog.analyse([log], use_journal=False, collect=boom)[2])
            self.assertIn("no timestamps", crash_triage_dialog.analyse([log], collect=boom)[2])


if __name__ == "__main__":
    unittest.main()
