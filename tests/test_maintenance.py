"""Regressions for the November maintenance pass (stability, performance, dead code)."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from drstool_core import game_scan, xid_analysis  # noqa: E402
from drstool_core.game_links import LinkStore  # noqa: E402
from drstool_core.session_recorder import SessionStore  # noqa: E402

HAVE_QT = importlib.util.find_spec("PySide6") is not None


class GameRootTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.lib = Path(self._td.name) / "Games"

    def tearDown(self):
        self._td.cleanup()

    def test_ue3_layout_does_not_claim_the_whole_library(self):
        exe = self.lib / "Borderlands 2" / "Binaries" / "Win32" / "Borderlands2.exe"
        exe.parent.mkdir(parents=True)
        (self.lib / "Borderlands 2" / "Engine").mkdir()
        (self.lib / "Other Game").mkdir()
        self.assertEqual(game_scan._game_root_from_exe(exe), self.lib / "Borderlands 2")

    def test_ue4_layout_still_returns_the_game_folder(self):
        exe = self.lib / "Alan Wake 2" / "AlanWake2" / "Binaries" / "Win64" / "AlanWake2.exe"
        exe.parent.mkdir(parents=True)
        (self.lib / "Alan Wake 2" / "Engine").mkdir()
        self.assertEqual(game_scan._game_root_from_exe(exe), self.lib / "Alan Wake 2")

    def test_plain_layout_is_the_exe_folder(self):
        exe = self.lib / "Indie" / "game.exe"
        self.assertEqual(game_scan._game_root_from_exe(exe), self.lib / "Indie")

    def test_yaml_null_values_are_dropped(self):
        if game_scan.yaml is None:
            self.skipTest("PyYAML not installed")
        g = game_scan._yaml_game_section("game:\n  exe: null\n  working_dir: ~\n  prefix: /p\n")
        self.assertEqual(g, {"prefix": "/p"})


class LinkStoreCacheTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "game_links.json"

    def tearDown(self):
        self._td.cleanup()

    def _write(self, games):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "games": games}), encoding="utf-8")
        os.replace(tmp, self.path)              # what atomic_write_json does

    def test_unchanged_file_is_not_parsed_again(self):
        self._write({"steam:1": {"profile": "P"}})
        store = LinkStore(self.path)
        with mock.patch("drstool_core.game_links.json.loads", side_effect=AssertionError("re-parsed")):
            for _ in range(5):
                self.assertEqual(store.get("steam:1")["profile"], "P")
                self.assertEqual(store.counts(), {"P": 1})

    def test_other_window_write_is_seen(self):
        self._write({"steam:1": {"profile": "P"}})
        store = LinkStore(self.path)
        self.assertEqual(store.get("steam:1")["profile"], "P")
        self._write({"steam:1": {"profile": "Q"}, "steam:2": {"profile": "Q"}})
        self.assertEqual(store.counts(), {"Q": 2})

    def test_own_write_and_a_second_instance_agree(self):
        a, b = LinkStore(self.path), LinkStore(self.path)
        a.assign("steam:7", "P", name="Seven")
        self.assertEqual(b.get("steam:7")["profile"], "P")
        b.assign("steam:7", "Q")
        self.assertEqual(a.get("steam:7")["profile"], "Q")

    def test_corrupt_store_keeps_snapshot_and_reports(self):
        self._write({"steam:1": {"profile": "P"}})
        store = LinkStore(self.path)
        self.path.write_bytes(b"\xff\xfe not json")
        self.assertEqual(store.get("steam:1")["profile"], "P")
        self.assertTrue(store.load_error)
        with self.assertRaises(ValueError):
            store.assign("steam:1", "Q")
        self._write({"steam:1": {"profile": "R"}})
        self.assertEqual(store.get("steam:1")["profile"], "R")
        self.assertIsNone(store.load_error)

    def test_missing_file(self):
        store = LinkStore(self.path)
        self.assertEqual(store.all(), {})
        self.assertIsNone(store.load_error)


class ClauseEquivalenceTests(unittest.TestCase):
    """_holds is now set inclusion on precompiled clauses; it must match the old semantics."""

    @staticmethod
    def _old(h, on):
        kind = h[0]
        if kind == "none":
            return True
        if kind == "one":
            return h[1] in on
        if kind == "and":
            return all(i in on for i in h[1:])
        if kind == "or":
            return any(i in on for i in h[1:])
        if kind == "mixed":
            return (h[1] in on and h[2] in on) or h[3] in on
        return (h[1] in on and h[2] in on) or (h[3] in on and h[4] in on)

    def test_every_kind_on_every_subset(self):
        from itertools import combinations
        u = "abcde"
        hyps = [("none",), ("one", "a"), ("and", "a", "b"), ("and", "a", "b", "c", "d"), ("or", "a", "c"),
                ("mixed", "a", "b", "c"), ("mixed2", "a", "b", "c", "d")]
        for n in range(len(u) + 1):
            for on in map(frozenset, combinations(u, n)):
                for h in hyps:
                    self.assertEqual(xid_analysis.CrashModel._holds(h, on), self._old(h, on), (h, on))

    def test_unknown_kind_still_rejected(self):
        with self.assertRaises(ValueError):
            xid_analysis.CrashModel._holds(("weird", "a"), frozenset("a"))


class SessionRecentTests(unittest.TestCase):
    def test_file_vanishing_during_sort_does_not_empty_the_list(self):
        with tempfile.TemporaryDirectory() as td:
            store = SessionStore(Path(td))
            e1 = store.begin(profile="P", profile_fp="x", command=["/bin/true"], env={})
            e2 = store.begin(profile="P", profile_fp="x", command=["/bin/true"], env={})
            real_stat = Path.stat
            gone = Path(td) / f"{e1['id']}.json"

            def flaky(self, *a, **kw):
                # the mtime stat used for sorting fails; lstat (is_symlink) still sees the entry
                if self == gone and kw.get("follow_symlinks", True):
                    raise FileNotFoundError(self)
                return real_stat(self, *a, **kw)
            with mock.patch.object(Path, "stat", flaky):
                ids = [e["id"] for e in store.recent()]
            self.assertIn(e2["id"], ids)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class GuiMaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import _home  # noqa: F401 - fake HOME before drstool_gui reads settings
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_parse_nip_rejects_bad_xml_with_value_error(self):
        from drstool_gui.nip_import import parse_nip
        with self.assertRaises(ValueError):
            parse_nip("not xml at all")

    def test_retained_thread_blocks_close_until_finished(self):
        from PySide6.QtCore import QThread
        from drstool_gui.tasks import TaskCenter
        tc = TaskCenter()
        th = QThread()
        th.run = lambda: QThread.msleep(150)
        tc.retain(th)
        th.start()
        try:
            self.assertIn(th, tc.running())
            self.assertEqual(tc.tasks(), [])              # not listed in the task button
        finally:
            th.wait()
        self.assertEqual(tc.running(), [])

    def test_stale_card_refresh_is_ignored(self):
        from PySide6.QtCore import QObject, Signal
        from drstool_gui import game_card as gc
        from drstool_gui.settings_manager import SettingsManager

        made = []

        class FakeWorker(QObject):
            done = Signal(str, dict)
            finished = Signal()

            def __init__(self, *a, **kw):
                super().__init__()
                made.append(self)

            def start(self):
                pass

            def isRunning(self):  # noqa: N802
                return False

        card = gc.GameCardWidget(SettingsManager())
        game = game_scan.GameEntry("Custom", "custom:x", "X", [])
        with mock.patch.object(gc, "_CardWorker", FakeWorker):
            card.set_game(game)
            card.reload()
            first, second = made[-2], made[-1]
            seen = []
            with mock.patch.object(card, "_on_info", lambda k, i: seen.append(i.get("n"))):
                second.done.emit(game.key, {"n": 2})
                first.done.emit(game.key, {"n": 1})     # older refresh finishing last
            self.assertEqual(seen, [2])
        card.deleteLater()

    def test_flow_layout_wraps_and_right_aligns(self):
        from PySide6.QtWidgets import QPushButton, QWidget
        from drstool_gui.widgets import FlowLayout
        host = QWidget()
        lay = FlowLayout(host)
        left = [QPushButton(f"Left button {i}") for i in range(4)]
        for b in left:
            lay.addWidget(b)
        lay.add_stretch()
        right = QPushButton("Right")
        lay.addWidget(right)
        wide = lay.sizeHint().width()
        self.assertEqual(lay.minimumSize().width(), max(b.sizeHint().width() for b in left + [right]))
        h1 = lay.heightForWidth(wide)
        self.assertGreater(lay.heightForWidth(wide // 3), h1)             # wraps onto more lines
        host.resize(wide + 200, h1)
        lay.setGeometry(host.rect())
        self.assertEqual(right.geometry().right(), host.rect().right())  # after the stretch: right end
        self.assertEqual(left[0].geometry().left(), 0)
        for b in left + [right]:                                          # never squeezed
            self.assertEqual(b.width(), b.sizeHint().width())
        extra = QPushButton("Inserted")
        lay.insertWidget(1, extra)                                       # before the stretch
        lay.setGeometry(host.rect())
        self.assertLess(extra.geometry().left(), left[1].geometry().left())
        self.assertEqual(right.geometry().right(), host.rect().right())
        host.deleteLater()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class NarrowWindowLayoutTests(unittest.TestCase):
    """Every page at a small laptop size: no button drawn narrower than its text, nothing cut off
    at the right edge of its scroll area. The window's minimum width (980 px) overrides the
    layouts' own minimum, so an over-wide row is squeezed or clipped instead of refused."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import _home  # noqa: F401
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _problems(self, win):
        from PySide6.QtCore import QPoint
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QAbstractButton, QScrollArea
        # A card refreshed by a worker shows a button just before this check: let every pending
        # layout pass run, or the button is measured at its pre-layout size.
        for _ in range(10):
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        out = []
        for b in win.findChildren(QAbstractButton):
            if not b.isVisible() or not b.text() or b is getattr(win, "_tasks_btn", None):
                continue
            if b.width() + 2 < b.sizeHint().width():
                out.append(f"squeezed: {b.text()!r} {b.width()} < {b.sizeHint().width()}")
                continue
            par = b.parentWidget()
            while par is not None and not isinstance(par, QScrollArea):
                par = par.parentWidget()
            if par is not None:
                right = b.mapTo(win, QPoint(b.width(), 0)).x()
                edge = par.viewport().mapTo(win, QPoint(par.viewport().width(), 0)).x()
                if right > edge + 1:
                    out.append(f"clipped: {b.text()!r} by {right - edge}px")
        return out

    def test_all_pages_fit_1024x700(self):
        from drstool_gui.main_window import MainWindow
        win = MainWindow()
        try:
            win.resize(1024, 700)
            win.show()
            problems = []
            for i in range(win._sidebar_stack.count()):
                win._switch_tab(i)
                problems += [f"tab {i}: {p}" for p in self._problems(win)]
            win._switch_tab(win.TAB_PROFILES)
            for j in range(win._sync_tabs.count()):
                win._sync_tabs.setCurrentIndex(j)
                problems += [f"{win._sync_tabs.tabText(j)}: {p}" for p in self._problems(win)]
            win._switch_tab(win.TAB_TOOLS)
            for title in win._extra_tools.tab_titles():
                win._extra_tools.show_tab(title)
                problems += [f"{title}: {p}" for p in self._problems(win)]
            self.assertEqual(problems, [])
        finally:
            win.hide()
            win.deleteLater()


if __name__ == "__main__":
    unittest.main()
