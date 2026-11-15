"""Short UI animations (page cross-fade). All of them are skipped when Appearance > Animations
is off or the widget is not on screen, so headless runs and tests behave as before.
"""

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QStackedWidget

from .theme import animations_on


class FadeStack(QStackedWidget):
    """QStackedWidget whose page changes cross-fade.

    The fade runs on a snapshot of the outgoing page laid over the stack, never on the pages
    themselves: a QGraphicsEffect on a live page would re-render its whole subtree every frame
    and breaks widgets that carry an effect of their own.
    """
    DURATION_MS = 150

    def __init__(self, parent=None):
        super().__init__(parent)
        self._veil = None

    def setCurrentIndex(self, index: int):       # noqa: N802
        if (index == self.currentIndex() or not animations_on() or not self.isVisible()
                or self.width() < 2 or self.height() < 2):
            super().setCurrentIndex(index)
            return
        shot = self.grab()
        super().setCurrentIndex(index)
        self._drop_veil()
        veil = QLabel(self)
        veil.setAttribute(Qt.WA_TransparentForMouseEvents)
        veil.setPixmap(shot)
        veil.setGeometry(self.rect())
        fx = QGraphicsOpacityEffect(veil)
        veil.setGraphicsEffect(fx)
        anim = QPropertyAnimation(fx, b"opacity", veil)
        anim.setDuration(self.DURATION_MS)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(lambda v=veil: self._drop_veil() if self._veil is v else None)
        self._veil = veil
        veil.show()
        veil.raise_()
        anim.start()

    def setCurrentWidget(self, widget):          # noqa: N802
        self.setCurrentIndex(self.indexOf(widget))

    def resizeEvent(self, e):                    # noqa: N802
        super().resizeEvent(e)
        self._drop_veil()                        # a stale snapshot would show at the old size

    def _drop_veil(self):
        veil, self._veil = self._veil, None
        if veil is not None:
            veil.hide()
            veil.deleteLater()
