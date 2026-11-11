"""Background task centre (the 'N running' button in the top bar)."""

import time
from typing import List, Optional

from PySide6.QtCore import QObject, Signal

from . import toasts as _toasts
from .toasts import desktop_notify, toast


def _NAV(tool_title: str) -> dict:
    return {"kind": "tool", "key": tool_title, "title": tool_title}


class TaskCenter(QObject):
    """Lists the worker threads (downloads, scans, lookups) so they stay visible from any tab."""
    changed = Signal()

    def __init__(self):
        super().__init__()
        self._tasks: List[dict] = []
        self._seq = 0
        self._threads: list = []       # every tracked thread, so wait_all() can stop them on exit

    def tasks(self) -> List[dict]:
        return list(self._tasks)

    def running(self) -> list:
        """Strong references for unfinished QThreads; none may be destroyed running."""
        self._threads = [th for th in self._threads if th.isRunning()]
        return list(self._threads)

    def request_shutdown(self):
        """Non-blocking cancellation, called before closing a window."""
        for th in self.running():
            if hasattr(th, 'cancel'):
                try:
                    th.cancel()
                except Exception:
                    pass
            th.requestInterruption()

    def wait_all(self, timeout_ms: int = 1500) -> bool:
        """Bounded join; False means a thread still needs time to stop.

        Callers MUST retain this TaskCenter and let the Qt event loop run
        until running() returns empty rather than destroying active QThreads.
        """
        self.request_shutdown()
        for th in self.running():
            if not th.wait(max(0, timeout_ms)):
                return False
        return not self.running()

    def track(self, thread, title: str, nav: Optional[dict] = None, progress=None, fail=None) -> dict:
        self._seq += 1
        t = {"id": self._seq, "title": title, "nav": nav, "state": "running",
             "detail": "", "t0": time.monotonic(), "t1": None}
        self._tasks.append(t)
        del self._tasks[:-25]
        self._threads = [th for th in self._threads if th.isRunning()] + [thread]
        thread.finished.connect(lambda: self._finish(t))
        if progress is not None:
            progress.connect(lambda *a: self._progress(t, a))
        if fail is not None:
            fail.connect(lambda *a: self._fail(t, a))
        self.changed.emit()
        return t

    @staticmethod
    def _first_str(args) -> str:
        return next((x for x in args if isinstance(x, str) and x.strip()), "")

    def _progress(self, t: dict, args):
        s = self._first_str(args)
        if s and t["state"] == "running":
            t["detail"] = s.strip().splitlines()[-1][:80]
            self.changed.emit()

    def _fail(self, t: dict, args):
        err = self._first_str(args)
        if err:
            t["state"], t["detail"] = "failed", err.strip().splitlines()[0][:120]

    def _finish(self, t: dict):
        t["t1"] = time.monotonic()
        if t["state"] == "running":
            t["state"] = "done"
        self.changed.emit()
        dur = t["t1"] - t["t0"]
        failed = t["state"] == "failed"
        if dur < 3 and not failed:
            return
        win = _toasts.TOASTS._win if _toasts.TOASTS is not None else None
        msg = f"{t['title']}: " + (f"failed — {t['detail']}" if failed else "finished")
        action = ("Open", lambda n=t["nav"]: win.navigate_to(n)) if (win is not None and t["nav"]) else None
        toast(msg, "error" if failed else "ok", 6000, action)
        if win is not None and not win.isActiveWindow() and dur >= 4:
            desktop_notify("DRSTool", msg)

    def clear_finished(self):
        self._tasks = [t for t in self._tasks if t["state"] == "running"]
        self.changed.emit()


TASKS = TaskCenter()
