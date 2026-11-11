"""Find the one setting that breaks a game by switching half of the suspects off at a time."""

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .state import bisect_items, bisect_state, copy_state


def _label(item: tuple) -> str:
    kind, key = item
    return {"settings": "DRS", "env": "env", "gs_on": "gamescope"}[kind] + (f" {key}" if key else "")


class BisectDialog(QDialog):
    """Non-modal: the editor holds the test configuration; the user writes it to the game
    (Games tab / launcher tab) and launches it, then answers here."""

    def __init__(self, win):
        super().__init__(win)
        self.setWindowTitle("Find the breaking setting")
        self._win = win
        self._full = copy_state(win.capture_state())
        self._suspects = bisect_items(self._full)
        self._good: list = []                        # proven innocent, stay on in every test
        lay = QVBoxLayout(self)
        self._text = QLabel()
        self._text.setWordWrap(True)
        lay.addWidget(self._text)
        row = QHBoxLayout()
        self._ok = QPushButton("Works")
        self._bad = QPushButton("Still broken")
        stop = QPushButton("Stop and restore")
        self._ok.clicked.connect(lambda: self._answer(True))
        self._bad.clicked.connect(lambda: self._answer(False))
        stop.clicked.connect(self._restore)
        for b in (self._ok, self._bad, stop):
            row.addWidget(b)
        lay.addLayout(row)
        self._step()

    def _half(self) -> list:
        return self._suspects[:len(self._suspects) // 2]

    def _step(self):
        if len(self._suspects) <= 1:
            self._finish()
            return
        on = self._good + self._half()
        self._win.apply_state(bisect_state(self._full, on), "Bisect test")
        self._text.setText(
            f"{len(self._suspects)} suspects left (about {max(1, (len(self._suspects) - 1).bit_length())} "
            f"more test(s)). The editor now has {len(on)} of {len(bisect_items(self._full))} settings on.\n\n"
            "Write it to the game, launch it and answer below. This assumes a single setting is the cause.")

    def _answer(self, works: bool):
        half = self._half()
        if works:                                    # the switched-on half is innocent
            self._good += half
            self._suspects = self._suspects[len(half):]
        else:
            self._suspects = half
        self._step()

    def _finish(self):
        self._ok.hide()
        self._bad.hide()
        if not self._suspects:
            self._text.setText("No settings to test.")
            return
        culprit = self._suspects[0]
        self._win.apply_state(bisect_state(self._full, [i for i in bisect_items(self._full) if i != culprit]),
                              f"Without {_label(culprit)}")
        self._text.setText(f"Likely culprit: {_label(culprit)}\n\nThe editor now has every other setting on. "
                           "“Stop and restore” brings the full configuration back.")

    def _restore(self):
        self._win.apply_state(self._full, "Bisect: restore")
        self.close()
