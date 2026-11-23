"""Crash Lab: breaking-setting search widget, triage hand-over, main-window tab. Offscreen."""
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
    from drstool_gui import crash_lab
    from drstool_gui.base import APP_ID
except ImportError:                                  # PySide6 missing
    crash_lab = None

FULL = {"settings": {"0x10E41DF3": "0x1"}, "arch": "GB200",
        "env": {"A": "1", "B": "1", "C": "1", "VKD3D_CONFIG": "dxr,force_static_cbv,descriptor_heap"},
        "gs_on": False, "gs_flags": {}}


def has(st, item) -> bool:
    kind, key = item
    if kind == "vkd3d":
        return key in st["env"].get("VKD3D_CONFIG", "").split(",")
    return key in st[kind]


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@unittest.skipIf(crash_lab is None, "PySide6 not installed")
class SearchWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        QSettings(APP_ID, APP_ID).remove("crashlab/search")
        win = self.win = QWidget()
        win.state = dict(FULL)
        win.capture_state = lambda: win.state
        win.apply_state = lambda st, label=None: setattr(win, "state", st)
        self.clock = Clock()
        self.asked = []

    def widget(self, confirm=True, kernel=()):
        w = crash_lab.CrashSearchWidget(self.win)
        w.now = self.clock
        w.collect = lambda s, e: {"status": "ok", "events": [{"xid": x, "message": f"Xid {x}"} for x in kernel]}
        w.confirm = lambda title, text: self.asked.append(text) or confirm
        return w

    def play(self, w, causes, seconds=600):
        """Answer like a game that crashes when every item of one cause group is on."""
        for _ in range(80):
            if w._search is None or w._search.done:
                return
            self.clock.t += seconds
            w.answer(any(all(has(self.win.state, i) for i in g) for g in causes))
        self.fail("search did not finish")

    def test_pair_found_with_drs_kept_on(self):
        w = self.widget()
        self.assertTrue(w._start.isVisibleTo(w))
        w.start()
        self.assertEqual(self.win.state["settings"], FULL["settings"])     # DRS kept on in every test
        self.assertIn("baseline", w._text.text())
        self.play(w, [[("vkd3d", "dxr"), ("env", "B")]])
        self.assertEqual(sorted(w._search.culprits), [("env", "B"), ("vkd3d", "dxr")])
        self.assertIn("only together", w._text.text())
        self.assertFalse(has(self.win.state, ("vkd3d", "dxr")) and has(self.win.state, ("env", "B")))
        self.assertEqual(self.win.state["env"]["VKD3D_CONFIG"], "force_static_cbv,descriptor_heap")
        w.keep_result()
        self.assertEqual(crash_lab.CrashSearchWidget._load(), {})
        self.assertEqual(self.asked, [])                                   # 600 s per test: no doubts

    def test_two_independent_causes(self):
        w = self.widget()
        w.start()
        self.play(w, [[("env", "A")], [("vkd3d", "descriptor_heap")]])
        self.assertEqual(sorted(w._search.culprits), [("env", "A"), ("vkd3d", "descriptor_heap")])
        self.assertIn("independent causes", w._text.text())

    def test_short_works_is_questioned(self):
        w = self.widget(confirm=False)
        w.set_hints([], crash_seconds=300)
        w.start()
        self.clock.t += 60
        w.answer(False)                                                    # declined: not counted
        self.assertEqual(w._search.tests_done, 0)
        self.assertIn("7 min 30 s", self.asked[0])
        self.clock.t += 400                                                # 460 s now > 1.5 × 300
        w.answer(False)
        self.assertEqual(w._search.tests_done, 1)
        self.assertEqual(len(self.asked), 1)

    def test_kernel_xid_contradicts_works(self):
        w = self.widget(confirm=False, kernel=[69])
        w.start()
        self.clock.t += 900
        w.answer(False)
        self.assertEqual(w._search.tests_done, 0)
        self.assertIn("Xid 69", self.asked[0])
        w.answer(True)                                                     # "still crashes" is not questioned
        self.assertEqual(w._search.tests_done, 1)

    def test_suspects_from_triage_go_first(self):
        w = self.widget()
        w.set_hints(["vkd3d:descriptor_heap", "env:C", "env:NOT_SET"])
        self.assertIn("VKD3D_CONFIG descriptor_heap", w._intro.text())
        w.start()
        self.assertEqual(w._search.items[:2], [("vkd3d", "descriptor_heap"), ("env", "C")])

    def test_pause_resume_undo_and_restore(self):
        w = self.widget()
        w.start()
        self.play_steps(w, 2)
        pending, answers = w._search.pending, list(w._search.answers)
        self.win.state = {"settings": {}, "arch": None, "env": {}, "gs_on": False, "gs_flags": {}}
        again = self.widget()                                              # app restarted
        self.assertTrue(again._resume.isVisibleTo(again))
        self.assertEqual(self.win.state["env"], {})                        # nothing applied at startup
        again.resume()
        self.assertEqual((again._search.answers, again._search.pending), (answers, pending))
        self.clock.t += 600
        again.answer(True)
        again.undo()
        self.assertEqual(again._search.pending, pending)
        again.restore()
        self.assertEqual(self.win.state, FULL)
        self.assertEqual(crash_lab.CrashSearchWidget._load(), {})
        self.assertTrue(again._start.isVisibleTo(again))

    def play_steps(self, w, n):
        for _ in range(n):
            self.clock.t += 600
            w.answer(has(self.win.state, ("env", "C")))

    def test_kept_settings_alone_crash(self):
        w = self.widget()
        w.start()
        w.answer(True)
        self.assertEqual(w._search.outcome, ("fixed", []))
        self.assertIn("Untick", w._text.text())
        self.assertEqual(self.win.state, FULL)

    def test_nothing_to_test(self):
        self.win.state = dict(FULL, env={})
        w = self.widget()
        w.start()
        self.assertIn("No settings to test", w._text.text())
        self.assertFalse(w._ok.isVisibleTo(w))


@unittest.skipIf(crash_lab is None, "PySide6 not installed")
class TriageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_kernel_events_are_merged(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "game.log")
            log.write_text("(2026-11-23 18:00:00) start\n(2026-11-23 18:01:00) We've lost the Device\n")
            seen = []

            def collect(start, end):
                seen.append(end - start)
                return {"status": "ok", "reason": "", "events": [
                    {"message": "NVRM: Xid (PCI:0000:01:00): 69, pid=1, Class Error: ChId 0017, Class 0000ce97"}]}
            result, clean, note = crash_lab.analyse([log], collect=collect)
        self.assertEqual(seen, [60 + 30 + 120])
        self.assertIsNone(clean)
        self.assertEqual(result["xid"], [{"xid": 69, "channel": "0x17", "class": "0xce97"}])
        self.assertIn("1 relevant event", note)

    def test_journal_off_or_no_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "game.log")
            log.write_text("VK_ERROR_DEVICE_LOST\n")
            boom = lambda *a: self.fail("kernel log must not be read")       # noqa: E731
            self.assertIn("not read", crash_lab.analyse([log], use_journal=False, collect=boom)[2])
            self.assertIn("no timestamps", crash_lab.analyse([log], collect=boom)[2])

    def test_hand_over_to_search(self):
        QSettings(APP_ID, APP_ID).remove("crashlab/search")
        win = QWidget()
        win.capture_state = lambda: dict(FULL)
        win.apply_state = lambda st, label=None: None
        lab = crash_lab.CrashLabWidget(win)
        with tempfile.TemporaryDirectory() as d:
            crashed, clean = Path(d, "crash.log"), Path(d, "clean.log")
            crashed.write_text("(2026-11-23 18:00:00) x\n"
                               "info:vkd3d-proton:vkd3d_config_flags_init_once: VKD3D_CONFIG='dxr'.\n"
                               "(2026-11-23 18:04:00) We've lost the Device\n")
            clean.write_text("info:vkd3d-proton:vkd3d_config_flags_init_once: VKD3D_CONFIG=''.\n")
            lab.triage.collect = lambda s, e: {"status": "ok", "events": []}
            lab.triage.load([crashed], [clean])
        lab.triage._send.click()
        self.assertEqual(lab.tabText(lab.currentIndex()), crash_lab.SEARCH)
        self.assertEqual(lab.search._priority, [("vkd3d", "dxr")])
        self.assertEqual(lab.search._crash_hint, 240)


@unittest.skipIf(crash_lab is None, "PySide6 not installed")
class MainWindowTabTests(unittest.TestCase):
    def test_crash_lab_tab_and_menu(self):
        app = QApplication.instance() or QApplication([])
        from drstool_gui import main_window
        win = main_window.MainWindow()
        try:
            self.assertIn("Menu", win._menu_btn.text())
            texts = [a.text() for a in win._menu_btn.menu().actions()]
            self.assertTrue(any(t.startswith("Crash triage") for t in texts))
            win._open_bisect()
            self.assertEqual(win._sidebar_stack.currentIndex(), win.TAB_CRASH)
            self.assertEqual(win._right_stack.currentIndex(), win.PAGE_CRASH)
            self.assertEqual(win._crash_lab.tabText(win._crash_lab.currentIndex()), crash_lab.SEARCH)
            self.assertEqual(win._crash_sidebar.currentItem().text(), crash_lab.SEARCH)
            win._crash_sidebar.setCurrentRow(0)
            self.assertEqual(win._crash_lab.tabText(win._crash_lab.currentIndex()), crash_lab.TRIAGE)
            win._switch_tab(win.TAB_DRS)
            win._nav_tabs[win.TAB_CRASH].click()
            self.assertEqual(win._right_stack.currentIndex(), win.PAGE_CRASH)
        finally:
            win.close()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
