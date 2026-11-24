"""Interactive delta debugging (ddmin) for "which settings make the game crash".

The user runs every test by hand, so the search is a pure function of the answers given so far:
`CrashSearch(items, answers)` replays them, which makes undo and resuming after a restart trivial.

Each test switches on the always-kept ("fixed") settings plus a subset of `items`; the answer is
True when the game still fails.

1. The fixed settings alone are tested first: if they fail, nothing in `items` is needed.
2. Verify that the full set fails, then ddmin shrinks it to a 1-minimal failing
   set: switching off any one of its settings stops the failure. Unlike halving it also finds
   settings that only crash together.
3. Verification: everything except the culprits found so far. If that still fails there is a
   second, independent cause and the search continues on what is left.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence


def order_by_priority(items: Sequence, priority: Iterable) -> list:
    """Items named in `priority` first (in that order), the rest keep their order.

    ddmin splits the list into consecutive chunks, so suspects that sit together are
    tested together early."""
    pri = list(dict.fromkeys(p for p in priority if p in items))
    seen = set(pri)
    return pri + [i for i in items if i not in seen]


def _split(c: list, n: int) -> list:
    size, rest = divmod(len(c), n)
    out, i = [], 0
    for k in range(n):
        j = i + size + (1 if k < rest else 0)
        out.append(c[i:j])
        i = j
    return [d for d in out if d]


def _ddmin(c: list, st: dict):
    n = 2
    while len(c) >= 2:
        st["phase"], st["candidates"] = "search", list(c)
        chunks = _split(c, n)
        for d in chunks:
            if (yield d):
                c, n = d, 2
                break
        else:
            for d in (chunks if n > 2 else []):
                comp = [i for i in c if i not in d]
                if (yield comp):
                    c, n = comp, max(n - 1, 2)
                    break
            else:
                if n >= len(c):
                    break
                n = min(2 * n, len(c))
    return c


def _search(items: list, st: dict):
    """Yields subsets to test, receives True (fails) / False (works).
    Returns ("fixed", []) or ("found", [culprit group, …])."""
    st["phase"], st["candidates"] = "fixed", list(items)
    if (yield []):
        return "fixed", []
    if not items:
        return "no-candidates", []
    st["phase"] = "baseline"
    if not (yield list(items)):
        return "not-reproduced", []
    groups, remaining = st.setdefault("groups", []), list(items)
    while True:
        group = yield from _ddmin(remaining, st)
        groups.append(group)
        remaining = [i for i in remaining if i not in group]
        st["phase"], st["candidates"] = "verify", list(remaining)
        if not remaining or not (yield remaining):
            return "found", groups


class CrashSearch:
    def __init__(self, items: Sequence, answers: Iterable[bool] = (), *, repeats: int = 1):
        self.items = list(dict.fromkeys(items))
        self.repeats = max(1, int(repeats))
        self._restart()
        for a in answers:
            self.answer(a)

    def _restart(self):
        self.answers: list = []
        self._votes = []
        self.cache: dict = {}                       # frozenset(subset) -> fails
        self.outcome = None                         # ("fixed" | "found", groups) when done
        self.pending = None                         # subset the user has to test next
        self._st: dict = {}
        self._gen = _search(self.items, self._st)
        self._advance(None, first=True)

    def _advance(self, value, first=False):
        """Feed `value` and skip over subsets whose answer is already cached."""
        try:
            sub = next(self._gen) if first else self._gen.send(value)
            while frozenset(sub) in self.cache:
                sub = self._gen.send(self.cache[frozenset(sub)])
            self.pending = sub
        except StopIteration as stop:
            self.pending, self.outcome = None, stop.value

    @property
    def done(self) -> bool:
        return self.outcome is not None

    @property
    def tests_done(self) -> int:
        return len(self.answers)

    @property
    def phase(self) -> str:
        """"fixed", "search" or "verify" for the pending test; "done" at the end."""
        return "done" if self.done else self._st["phase"]

    @property
    def culprits(self) -> list:
        return [i for g in (self.outcome[1] if self.done else []) for i in g]

    @property
    def found_so_far(self) -> list:
        """Culprit groups found before the pending verification / next round."""
        return [list(g) for g in self._st.get("groups", [])]

    @property
    def candidates(self) -> list:
        """What is still suspected while searching (what stays on while verifying)."""
        return list(self._st.get("candidates", []))

    def estimate_left(self) -> int:
        """Tests still needed if a single setting is the cause (ddmin: ≤ 2 per halving,
        plus the verification)."""
        if self.done:
            return 0
        n = len(self._st["candidates"])
        if self.phase == "verify":
            return 1
        steps = 2 * math.ceil(math.log2(n)) if n > 1 else 0
        return (steps + 1 + (2 if self.phase == "fixed" else int(self.phase == "baseline"))) * self.repeats

    def estimate_range(self) -> tuple[int, int]:
        if self.done:
            return 0, 0
        n = len(self.items)
        # Unique subset cache bounds all search/verification tests; repeats are counted separately.
        upper = max(1, min(1 << n, 4 * n**3 + 2*n + 2) - len(self.cache)) * self.repeats - len(self._votes)
        return 1, upper

    def answer(self, fails: bool | None):
        if self.done:
            raise RuntimeError("search finished")
        if fails is not None and not isinstance(fails, bool):
            raise ValueError('answer must be True, False or None (retest)')
        if fails is None:
            return  # Inconclusive: keep the same subset; no evidence recorded.
        self.answers.append(fails)
        self._votes.append(fails)
        if len(self._votes) < self.repeats:
            return
        if any(v != self._votes[0] for v in self._votes):
            self.outcome, self.pending = ('inconclusive', []), None
            return
        result = self._votes[0]
        self._votes.clear()
        self.cache[frozenset(self.pending)] = result
        self._advance(result)

    def undo(self):
        answers = self.answers[:-1]
        self._restart()
        for a in answers:
            self.answer(a)


def early_works_warning(elapsed: float, crash_times: Iterable[float] = (), upper_bounds: Iterable[float] = (),
                        *, factor: float = 1.5, floor: float = 60.0) -> str | None:
    """A reason to doubt a "works" answer given after `elapsed` seconds, or None.

    `crash_times`: measured time until the crash (the crashed run's log); an intermittent crash
    that took 4 minutes is not ruled out by 5, so a margin (`factor`) applies.
    `upper_bounds`: time until earlier "still crashes" answers. The crash came at some point
    before the answer, so these are not stretched: playing as long as that is enough."""
    exact = [t for t in crash_times if t and t > 0]
    upper = [t for t in upper_bounds if t and t > 0]
    need = max([t * factor for t in exact] + upper + [floor])
    if elapsed >= need:
        return None
    why = ("the crash may need more time to show up" if need == floor
           else f"earlier crashes needed up to {_mmss(max(exact + upper))}")
    return f"This test ran for {_mmss(elapsed)} only, and {why}. Playing at least {_mmss(need)} is safer."


def _mmss(s: float) -> str:
    s = int(s)
    return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"
