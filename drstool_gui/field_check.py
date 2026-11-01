"""Live warning for free-text fields: red border, tooltip and optional label while the text is invalid."""

from typing import Callable, Optional

from PySide6.QtWidgets import QLabel, QLineEdit

_BAD_QSS = "QLineEdit{border:1px solid #d9534f;}"


def watch(edit: QLineEdit, checker: Callable[[str], str], label: Optional[QLabel] = None) -> Callable[[], bool]:
    """Check `edit` on every keystroke. Returns a function that re-checks and gives True when valid."""
    base_style = edit.styleSheet()
    base_tip = edit.toolTip()

    def check() -> bool:
        msg = checker(edit.text())
        edit.setStyleSheet(base_style + _BAD_QSS if msg else base_style)
        edit.setToolTip(msg or base_tip)
        if label is not None:
            label.setText(f"⚠ {msg}" if msg else "")
            label.setVisible(bool(msg))
        return not msg

    edit.textChanged.connect(lambda _t: check())
    check()
    return check
