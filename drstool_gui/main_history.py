"""Editor history/dirty-state and profile chip behaviors.

Class is a mixin to keep MainWindow's widget composition separate from history
operations.  Does not construct widgets or alter the original event handlers.
"""
from datetime import datetime
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog
from .dialogs import ChangesDialog
from .lookup import env_def
from .state import canon_flat, describe_change, single_change_key, state_as_profile, state_from_profile
from .theme import T

class HistoryMixin:
    def capture_state(self) -> dict:
        arch = self.settings_manager.get_arch()
        return {"settings": self.settings_manager.get_settings_list(),
                "arch": arch.code if arch else None,
                "env": self._env_widget.get_env_dict(),
                "gs_on": self._gamescope.is_enabled(),
                "gs_flags": self._gamescope.get_raw_flag_values()}
    def apply_state(self, st: dict, label: Optional[str] = None):
        """Put a whole state into the editor. With a label it becomes an undo step."""
        self._state_timer.stop()
        self._restoring = True
        try:
            self._env_editor.discard_pending_edit()
            self.settings_manager.replace_state(st["settings"], st["arch"])
            self._env_widget.load_values(st["env"])
            self._gamescope.load_flag_values(st["gs_on"], st["gs_flags"])
            self._env_widget.set_gamescope_enabled(st["gs_on"])
            tab = self._sidebar_stack.currentIndex()
            self._populate_settings(self._search.text() if tab == self.TAB_DRS else "", quiet=True)
            self._env_widget.populate(self._search.text() if tab == self.TAB_ENV else "")
            self._seen_env_keys = frozenset(self._env_widget.get_env_dict())
            self._populate_arch_list()
            name = getattr(self._env_editor, "_current_name", "")
            ev = env_def(name)
            if ev:
                self._env_editor.set_var(ev)
            cur = self._setting_editor._current_setting
            if cur is not None:
                self._setting_editor.set_setting(cur)
            self._refresh_badges()
            self._output_bar._update()
        finally:
            self._restoring = False
        self._state_timer.stop()
        if label is not None:
            self._pending_label = label
        self._on_state_settled()
    def _on_state_settled(self):
        if self._timeline is None:
            return
        cur = self.capture_state()
        prev = self._timeline.current()
        if cur != prev:
            label = self._pending_label or describe_change(prev, cur)
            key = None if self._pending_label else single_change_key(prev, cur)
            self._timeline.push(cur, label, key)
        self._pending_label = None
        self._update_undo_ui()
        self._reeval_dirty(cur)
    def _saved_state(self) -> Optional[dict]:
        name = self.settings_manager.get_current_profile()
        prof = self.settings_manager.get_profiles().get(name) if name else None
        return state_from_profile(prof) if isinstance(prof, dict) else None
    def _reeval_dirty(self, cur: Optional[dict] = None):
        """Dirty = the editor differs from what is saved — so undoing back to the
        saved state (or re-setting the same value) clears the marker again."""
        saved = self._saved_state()
        if saved is None:
            return
        cur = cur or self.capture_state()
        dirty = canon_flat(state_as_profile(cur)) != canon_flat(state_as_profile(saved))
        if dirty != self._dirty:
            self._set_dirty(dirty)
    def undo(self):
        if self._state_timer.isActive():            # an edit is still settling: record it first
            self._state_timer.stop()
            self._on_state_settled()
        if not self._timeline or not self._timeline.can_undo():
            self.statusBar().showMessage("Nothing to undo", 2000)
            return
        what = self._timeline.undo_label()
        self.apply_state(self._timeline.undo())
        self.statusBar().showMessage(f"Undid: {what}", 4000)
    def redo(self):
        if not self._timeline or not self._timeline.can_redo():
            self.statusBar().showMessage("Nothing to redo", 2000)
            return
        what = self._timeline.redo_label()
        self.apply_state(self._timeline.redo())
        self.statusBar().showMessage(f"Redid: {what}", 4000)
    def _jump_history(self, idx: int):
        if self._state_timer.isActive():
            self._state_timer.stop()
            self._on_state_settled()
        st = self._timeline.jump(idx) if self._timeline else None
        if st is not None:
            self.apply_state(st)
            self.statusBar().showMessage("Jumped in history", 3000)
    def _update_undo_ui(self):
        tl = self._timeline
        can_u, can_r = bool(tl and tl.can_undo()), bool(tl and tl.can_redo())
        self._undo_btn.setEnabled(bool(tl))
        self._undo_btn.setText("↶")
        self._undo_btn.setToolTip(f"Undo: {tl.undo_label()}  (Ctrl+Z)\nArrow: history" if can_u
                                  else "Nothing to undo — arrow: history")
        self._redo_btn.setEnabled(can_r)
        self._redo_btn.setToolTip(f"Redo: {tl.redo_label()}  (Ctrl+Shift+Z)" if can_r else "Nothing to redo")
    def _fill_history_menu(self):
        m = self._undo_menu
        m.clear()
        if not self._timeline:
            return
        entries = self._timeline.entries()
        shown = entries[-30:]
        if len(entries) > len(shown):
            a = m.addAction(f"… {len(entries) - len(shown)} older step(s)")
            a.setEnabled(False)
        for idx, label, ts, is_cur in reversed(shown):
            a = m.addAction(f"{'●' if is_cur else '   '}  {label}    ·  {datetime.fromtimestamp(ts):%H:%M:%S}")
            if is_cur:
                a.setEnabled(False)
            else:
                a.triggered.connect(lambda _c=False, i=idx: self._jump_history(i))
    def _chip_text(self, glyph: str, text: str, tail: str = "") -> str:
        fm = self._profile_chip.fontMetrics()
        return f"{glyph}  " + fm.elidedText(text, Qt.ElideMiddle, 150) + tail
    def _style_chip(self, color: str, bold: bool):
        self._profile_chip.setStyleSheet(
            f"QPushButton{{color:{color};background:{T['surface']};border:1px solid {T['border_hi']};"
            f"border-radius:14px;font-size:12px;font-weight:{600 if bold else 500};padding:5px 14px;}}"
            f"QPushButton:hover{{border-color:{color};background:{T['raised']};}}")
    def _show_unsaved_changes(self):
        name = self.settings_manager.get_current_profile()
        saved = self._saved_state()
        if not name or saved is None:
            self._switch_tab(self.TAB_PROFILES)
            return
        dlg = ChangesDialog(state_as_profile(saved), state_as_profile(self.capture_state()),
                            "Saved", "Editor", f"Unsaved changes — “{name}”", "Revert to saved", self)
        if dlg.exec() == QDialog.Accepted and dlg.revert:
            self.settings_manager.load_profile(name)
            self._on_profile_loaded(name)
            self._pending_label = f"Revert to saved “{name}”"
