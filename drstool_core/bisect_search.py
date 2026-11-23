"""Interactive delta debugging (ddmin) for "which settings make the game crash".

The user runs every test by hand, so the search is a pure function of the answers given so far:
`DDMin(items, answers)` replays them, which makes undo and resuming after a restart trivial.

Each test switches on the `fixed` items plus a subset of `items`; the answer is True when the
game still fails. The first test is the fixed items alone: if they fail, nothing in `items`
is needed. Otherwise the full set is taken as failing (that is why the user is here) and
ddmin shrinks it to a 1-minimal failing set: removing any single item from the result makes
the failure go away. Unlike halving this also finds causes that need two settings together.
"""
from __future__ import annotations

from typing import Iterable, Sequence


def order_by_priority(items: Sequence, priority: Iterable) -> list:
    """Items named in `priority` first (in that order), the rest keep their order.

    ddmin splits the list into consecutive chunks, so suspects that sit together are
    tested together early."""
    pri = [p for p in priority if p in items]
    seen = set(pri)
    return list(dict.fromkeys(pri)) + [i for i in items if i not in seen]


def _split(c: list, n: int) -> list:
    size, rest = divmod(len(c), n)
    out, i = [], 0
    for k in range(n):
        j = i + size + (1 if k < rest else 0)
        out.append(c[i:j])
        i = j
    return [d for d in out if d]


def _search(items: list):
    """Generator: yields subsets to test, receives True (fails) / False (works).
    Returns ("fixed", []) or ("found", minimal failing subset)."""
    if (yield []):
        return "fixed", []
    c, n = list(items), 2
    while len(c) >= 2:
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
    return "found", c


class DDMin:
    def __init__(self, items: Sequence, answers: Iterable[bool] = ()):
        self.items = list(items)
        self.answers: list = []
        self.cache: dict = {}                       # frozenset(subset) -> fails
        self.outcome = None                         # ("fixed" | "found", items) when done
        self.pending = None                         # subset the user has to test next
        self._gen = None
        self._restart()
        for a in answers:
            self.answer(a)

    def _restart(self):
        self.answers, self.cache, self.outcome = [], {}, None
        self._gen = _search(self.items)
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

    def answer(self, fails: bool):
        if self.done:
            raise RuntimeError("search finished")
        self.answers.append(bool(fails))
        self.cache[frozenset(self.pending)] = bool(fails)
        self._advance(bool(fails))

    def undo(self):
        answers = self.answers[:-1]
        self._restart()
        for a in answers:
            self.answer(a)
