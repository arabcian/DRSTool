"""Read-only diagnostic panel; no privileged operations or system changes."""
import json

from PySide6.QtWidgets import QWidget, QVBoxLayout, QPlainTextEdit, QLabel, QPushButton, QApplication

from drstool_core.diagnostics import gather
from .sysinfo import SYSHUB
from .theme import T
from .i18n import tr


class DiagnosticsWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        title = QLabel(tr('System compatibility / diagnostic report'))
        title.setStyleSheet(f"color:{T['text']};font-size:15px;font-weight:700;")
        lay.addWidget(title)
        note = QLabel(tr('Read-only information. The report excludes usernames, tokens and full paths; review before sharing.'))
        note.setWordWrap(True)
        lay.addWidget(note)
        self._transaction_alert = QLabel()
        self._transaction_alert.setWordWrap(True)
        lay.addWidget(self._transaction_alert)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        lay.addWidget(self.output)
        refresh = QPushButton(tr('Refresh'))
        refresh.clicked.connect(self.refresh)
        lay.addWidget(refresh)
        copy = QPushButton(tr('Copy report'))
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.output.toPlainText()))
        lay.addWidget(copy)
        self.refresh()

    def refresh(self):
        result = gather(si=SYSHUB.info)
        pending = result['transactions_need_attention']
        self._transaction_alert.setText(
            (f'⚠ {pending} DLL transaction(s) need manual attention; inspect backup journals before restoring.'
             if pending else 'No incomplete DLL transactions detected in the managed backup directory.'))
        self._transaction_alert.setStyleSheet(
            f"color:{T['warn'] if pending else T['muted']};font-size:12px;")
        self.output.setPlainText(json.dumps(result, indent=2, ensure_ascii=False))
