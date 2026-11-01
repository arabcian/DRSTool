"""DRS settings list (sidebar)."""

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from drstool_core.models import Setting

from .favorites import FAVS
from .sysinfo import drs_unavailable
from .widgets import (
    _fmt_drs_value, make_header_item, make_list_item, ROLE_COUNT, ROLE_KIND, ROLE_SET,
    ROLE_VALUE, setup_sidebar_list,
)


# ============================================================================
# Settings List - RED Categories, GREEN Active Settings
# ============================================================================

class SettingsListWidget(QListWidget):
    setting_selected = Signal(str)

    def __init__(self):
        super().__init__()
        self.setSelectionMode(QListWidget.SingleSelection)
        self.itemClicked.connect(self._on_item_clicked)
        # currentItemChanged also fires on keyboard Up/Down navigation, so
        # arrowing through the list opens each setting like a click does.
        # Headers have Qt.NoItemFlags, so keyboard navigation skips them.
        self.currentItemChanged.connect(self._on_current_item_changed)
        setup_sidebar_list(self)
        self._by_id: Dict[str, Setting] = {}
        self.only_set = False
        self.only_fav = False

    def populate(self, settings: List[Setting], current_state: Dict[str, str], filter_text: str = ""):
        # Block selection signals for the rebuild (see EnvVarsWidget.populate).
        self.blockSignals(True)
        self.clear()
        self._by_id = {s.id: s for s in settings}
        filter_lower = filter_text.lower()

        categories: Dict[str, List[Setting]] = {}
        for s in settings:
            if filter_text and not (
                filter_lower in s.name.lower() or
                filter_lower in s.id.lower() or
                filter_lower in s.cat.lower() or
                filter_lower in s.desc.lower() or
                (s.id in current_state
                 and filter_lower in _fmt_drs_value(s, current_state[s.id]).lower())
            ):
                continue
            if self.only_set and s.id not in current_state:
                continue
            if self.only_fav and not FAVS.is_fav("drs", s.id):
                continue
            categories.setdefault(s.cat, []).append(s)

        for cat, items in categories.items():
            n_set = sum(1 for s in items if s.id in current_state)
            self.addItem(make_header_item(cat, n_set))
            for s in items:
                cur = current_state.get(s.id)
                self.addItem(make_list_item(
                    s.name, s.id, is_set=cur is not None,
                    value=_fmt_drs_value(s, cur) if cur is not None else "",
                    fav=FAVS.is_fav("drs", s.id), unavail=drs_unavailable(s)))
        self.blockSignals(False)

    def refresh_colors(self, current_state: Dict[str, str]):
        """
        Refresh set-state, value previews and header counts in place, without
        rebuilding the list. Use populate() only when the filter changes.
        """
        header = None
        count = 0
        for i in range(self.count()):
            item = self.item(i)
            if item.data(ROLE_KIND) == "header":
                if header is not None:
                    header.setData(ROLE_COUNT, count)
                header, count = item, 0
                continue
            sid = item.data(Qt.UserRole)
            cur = current_state.get(sid)
            item.setData(ROLE_SET, cur is not None)
            s = self._by_id.get(sid)
            item.setData(ROLE_VALUE, _fmt_drs_value(s, cur) if (s and cur is not None) else "")
            if cur is not None:
                count += 1
        if header is not None:
            header.setData(ROLE_COUNT, count)

    def _on_item_clicked(self, item: QListWidgetItem):
        setting_id = item.data(Qt.UserRole)
        if setting_id:
            self.setting_selected.emit(setting_id)

    def _on_current_item_changed(self, current: Optional[QListWidgetItem], previous: Optional[QListWidgetItem]):
        if current is None:
            return
        setting_id = current.data(Qt.UserRole)
        if setting_id:
            self.setting_selected.emit(setting_id)
