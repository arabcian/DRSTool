"""drstool_core.bisect_search (no Qt)."""
import itertools
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drstool_core.bisect_search import CrashSearch, early_works_warning, order_by_priority  # noqa: E402


def run(items, causes, fixed_fails=False):
    """Drive the search with an oracle. `causes`: list of groups; the game fails when every
    setting of at least one group is on."""
    s, asked = CrashSearch(items), []
    while not s.done:
        asked.append(frozenset(s.pending))
        on = set(s.pending)
        s.answer(fixed_fails or any(set(g) <= on for g in causes))
    return s, asked


def groups(s):
    return sorted(sorted(g) for g in s.outcome[1])


class CrashSearchTests(unittest.TestCase):
    items = [f"f{i:02d}" for i in range(20)]

    def test_single_culprit_every_position(self):
        for c in self.items:
            s, asked = run(self.items, [[c]])
            self.assertEqual(s.outcome, ("found", [[c]]))
            self.assertEqual(s.culprits, [c])
            self.assertLessEqual(len(asked), 13)        # fixed + ddmin + verification

    def test_pair_that_only_fails_together(self):
        for a, b in [("f00", "f19"), ("f03", "f04"), ("f07", "f12")]:
            self.assertEqual(groups(run(self.items, [[a, b]])[0]), [[a, b]])

    def test_all_pairs_small_set(self):
        items = list("abcdefg")
        for pair in itertools.combinations(items, 2):
            self.assertEqual(groups(run(items, [pair])[0]), [sorted(pair)])

    def test_two_independent_causes(self):
        s, _ = run(self.items, [["f02"], ["f15", "f16"]])
        self.assertEqual(groups(s), [["f02"], ["f15", "f16"]])
        s, _ = run(self.items, [["f02"], ["f09"], ["f17"]])
        self.assertEqual(groups(s), [["f02"], ["f09"], ["f17"]])

    def test_fixed_items_alone_fail(self):
        s, asked = run(self.items, [], fixed_fails=True)
        self.assertEqual(s.outcome, ("fixed", []))
        self.assertEqual(asked, [frozenset()])

    def test_no_subset_is_asked_twice(self):
        for causes in ([["f00", "f01"]], [["f05", "f15"]], [["f02"], ["f09"]]):
            _, asked = run(self.items, causes)
            self.assertEqual(len(asked), len(set(asked)))

    def test_phases_and_estimate(self):
        s = CrashSearch(self.items)
        self.assertEqual((s.phase, s.pending), ("fixed", []))
        self.assertEqual(s.estimate_left(), 2 * 5 + 3)
        s.answer(False)
        self.assertEqual(s.phase, "baseline")
        s.answer(True)
        self.assertEqual(s.phase, "search")
        while s.phase != "verify":
            s.answer("f04" in s.pending)
        self.assertEqual(s.estimate_left(), 1)
        self.assertNotIn("f04", s.pending)
        s.answer(False)
        self.assertEqual((s.phase, s.estimate_left()), ("done", 0))

    def test_replay_and_undo(self):
        s, _ = run(self.items, [["f13"]])
        again = CrashSearch(self.items, s.answers)
        self.assertEqual(again.outcome, s.outcome)
        part = CrashSearch(self.items, s.answers[:3])
        expected = part.pending
        part.answer(not s.answers[3])          # misclick
        part.undo()
        self.assertEqual(part.pending, expected)
        self.assertEqual(part.tests_done, 3)

    def test_answer_after_done_raises(self):
        s, _ = run(["a"], [["a"]])
        self.assertEqual(s.outcome, ("found", [["a"]]))
        with self.assertRaises(RuntimeError):
            s.answer(True)

    def test_priority_order(self):
        self.assertEqual(order_by_priority(["a", "b", "c", "d"], ["c", "x", "a", "c"]), ["c", "a", "b", "d"])

    def test_early_works_warning(self):
        self.assertIsNone(early_works_warning(400, [240]))                 # 400 ≥ 1.5 × 240
        self.assertIn("6 min 00 s", early_works_warning(300, [240]))
        self.assertIsNone(early_works_warning(600, upper_bounds=[600]))   # answer times are not stretched
        self.assertIn("10 min 00 s", early_works_warning(300, upper_bounds=[600]))
        self.assertIn("more time", early_works_warning(30))
        self.assertIsNone(early_works_warning(90, [None, 0], [0]))

if __name__ == "__main__":
    unittest.main()
