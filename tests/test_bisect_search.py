"""drstool_core.bisect_search (no Qt)."""
import itertools
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drstool_core.bisect_search import DDMin, order_by_priority  # noqa: E402


def run(items, culprits, fixed_fails=False):
    """Drive the search with an oracle: the game fails when every culprit is on."""
    s, asked = DDMin(items), []
    while not s.done:
        asked.append(frozenset(s.pending))
        s.answer(fixed_fails or set(culprits) <= set(s.pending))
    return s, asked


class DDMinTests(unittest.TestCase):
    items = [f"f{i}" for i in range(20)]

    def test_single_culprit_every_position(self):
        for c in self.items:
            s, asked = run(self.items, [c])
            self.assertEqual(s.outcome, ("found", [c]))
            self.assertLessEqual(len(asked), 12)

    def test_pair_that_only_fails_together(self):
        for a, b in [("f0", "f19"), ("f3", "f4"), ("f7", "f12")]:
            s, _ = run(self.items, [a, b])
            self.assertEqual(sorted(s.outcome[1]), sorted([a, b]))

    def test_all_pairs_small_set(self):
        items = list("abcdefg")
        for pair in itertools.combinations(items, 2):
            self.assertEqual(sorted(run(items, pair)[0].outcome[1]), sorted(pair))

    def test_fixed_items_alone_fail(self):
        s, asked = run(self.items, [], fixed_fails=True)
        self.assertEqual(s.outcome, ("fixed", []))
        self.assertEqual(asked, [frozenset()])

    def test_no_subset_is_asked_twice(self):
        for pair in [("f0", "f1"), ("f5", "f15")]:
            _, asked = run(self.items, pair)
            self.assertEqual(len(asked), len(set(asked)))

    def test_replay_and_undo(self):
        s, _ = run(self.items, ["f13"])
        again = DDMin(self.items, s.answers)
        self.assertEqual(again.outcome, s.outcome)
        part = DDMin(self.items, s.answers[:3])
        expected = part.pending
        part.answer(not s.answers[3])          # misclick
        part.undo()
        self.assertEqual(part.pending, expected)
        self.assertEqual(part.tests_done, 3)

    def test_answer_after_done_raises(self):
        s, _ = run(["a"], ["a"])
        self.assertEqual(s.outcome, ("found", ["a"]))
        with self.assertRaises(RuntimeError):
            s.answer(True)

    def test_priority_order(self):
        self.assertEqual(order_by_priority(["a", "b", "c", "d"], ["c", "x", "a", "c"]), ["c", "a", "b", "d"])


if __name__ == "__main__":
    unittest.main()
