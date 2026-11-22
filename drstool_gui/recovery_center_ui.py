"""Unified recovery inventory, delegates destructive actions to existing verified UIs."""
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView)

from drstool_core.recovery_center import inventory
from .theme import T, btn_ss


class RecoveryCenterWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        heading = QLabel('Recovery Center — existing backups and interrupted operations')
        heading.setStyleSheet(f"color:{T['text']}; font-size:15px; font-weight:700;")
        lay.addWidget(heading)
        desc = QLabel('Inventory only. Restore operations run through Profile History / DLL Manager / '
                      'Tool Manager, which apply their own safety checks. Prefix archives require manual inspection.')
        desc.setWordWrap(True)
        lay.addWidget(desc)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Type', 'Item', 'State', 'Modified (UTC)'])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        for label, fn in (('Refresh', self.refresh), ('Open location', self.open_location),
                          ('Open recovery tool', self.open_tool)):
            btn = QPushButton(label)
            btn.setStyleSheet(btn_ss('ghost', 'sm'))
            btn.clicked.connect(fn)
            row.addWidget(btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.notice = QLabel('')
        self.notice.setWordWrap(True)
        lay.addWidget(self.notice)
        self._entries = []
        self.refresh()

    def refresh(self):
        try:
            result = inventory()
            self._entries = result['entries']
            self.table.setRowCount(len(self._entries))
            for row, entry in enumerate(self._entries):
                for col, item in enumerate((entry['kind'], entry['name'], entry['state'],
                                            entry['modified_utc'][:19])):
                    cell = QTableWidgetItem(str(item))
                    cell.setToolTip(entry['path'])
                    self.table.setItem(row, col, cell)
            self.notice.setText(f"{len(self._entries)} item(s). Selecting a backup does not restore it.")
        except (OSError, ValueError) as ex:
            self._entries = []
            self.table.setRowCount(0)
            self.notice.setText(f'Could not read backup inventory: {ex}')

    def _selected(self):
        row = self.table.currentRow()
        return self._entries[row] if 0 <= row < len(self._entries) else None

    def open_location(self):
        item = self._selected()
        if item is not None:
            p = Path(item['path'])
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p if p.is_dir() else p.parent)))

    def open_tool(self):
        item = self._selected()
        if item is None:
            return
        w = self.window()
        target = item['target']
        if target == 'Profile History' and hasattr(w, 'navigate_to'):
            w.navigate_to({'kind': 'sync', 'key': 'History'})
        elif target in ('DLL Manager', 'Tool Manager') and hasattr(w, 'navigate_to'):
            w.navigate_to({'kind': 'tool', 'key': target})
        else:
            QMessageBox.information(self, 'Manual recovery',
                                    'This item does not have a safe automatic restore action. '
                                    'Use Open location to examine the original backup before doing anything.')
