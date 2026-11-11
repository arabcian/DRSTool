"""Optional Qt GUI timing probes for before/after performance comparisons.

Set DRSTOOL_PROFILE_GUI=1 and collect the drstool_gui.perf_trace DEBUG logs.
The probes are disabled by default and never change event-loop scheduling.
"""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import QElapsedTimer

_LOG = logging.getLogger(__name__)
_ENABLED = os.environ.get('DRSTOOL_PROFILE_GUI', '').lower() in {'1', 'true', 'yes'}


class GuiPerfTimer:
    def __init__(self, label: str):
        self.label = label
        self.timer = QElapsedTimer() if _ENABLED else None
        if self.timer is not None:
            self.timer.start()

    def report(self, *, count: int | None = None):
        if self.timer is not None:
            _LOG.warning('GUI_PERF %s: %.3f ms%s', self.label,
                         self.timer.nsecsElapsed() / 1_000_000,
                         f' items={count}' if count is not None else '')
