"""Persist/restore the selected profile, navigation and filter state."""
from PySide6.QtCore import Qt
from . import base as _base
from .app_dialogs import _qbool
from .theme import APPEARANCE
import logging
_log = logging.getLogger(__name__)

class SessionMixin:
    def _save_session(self):
        qs = self._qsettings
        qs.setValue("session/profile", self.settings_manager.get_current_profile() or "")
        qs.setValue("session/chip_set", self._chip_set.isChecked())
        qs.setValue("session/chip_fav", self._chip_fav.isChecked())
        tab = self._sidebar_stack.currentIndex()
        key = ""
        if tab == self.TAB_DRS and self._settings_list.currentItem():
            key = str(self._settings_list.currentItem().data(Qt.UserRole) or "")
        elif tab == self.TAB_ENV and self._env_widget.currentItem():
            key = str(self._env_widget.currentItem().data(Qt.UserRole) or "")
        qs.setValue("session/key", key)
        qs.setValue("session/key_tab", tab)
        tabs = self._extra_tools._tabs
        qs.setValue("session/tool", tabs.tabText(tabs.currentIndex()).replace(" \u26a0", ""))
        qs.setValue("session/sync", self._sync_tabs.tabText(self._sync_tabs.currentIndex()))
    def _restore_session(self):
        if _base._CLI_MODE or not APPEARANCE["restore"]:
            return
        qs = self._qsettings
        try:
            name = str(qs.value("session/profile", "") or "")
            if name:
                self.load_profile_by_name(name)
            if _qbool(qs.value("session/chip_set", False)):
                self._chip_set.setChecked(True)
            if _qbool(qs.value("session/chip_fav", False)):
                self._chip_fav.setChecked(True)
            tab = self._sidebar_stack.currentIndex()
            key = str(qs.value("session/key", "") or "")
            if key and int(qs.value("session/key_tab", -1)) == tab:
                if tab == self.TAB_DRS:
                    self._select_item(self._settings_list, key)
                elif tab == self.TAB_ENV:
                    self._select_item(self._env_widget, key)
            if tab == self.TAB_TOOLS:
                tool = str(qs.value("session/tool", "") or "")
                if tool:
                    self._extra_tools.show_tab(tool)
            elif tab == self.TAB_PROFILES:
                sync = str(qs.value("session/sync", "") or "")
                for i in range(self._sync_tabs.count()):
                    if self._sync_tabs.tabText(i) == sync:
                        self._sync_tabs.setCurrentIndex(i)
                        break
        except Exception:
            _log.debug("ignored error", exc_info=True)
            pass
