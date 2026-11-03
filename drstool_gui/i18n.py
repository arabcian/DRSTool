"""GUI language selection, persisted in Qt settings."""
import os
from PySide6.QtCore import QLocale, QSettings
from drstool_core.i18n import translate, language_tag
from .base import APP_ID


def current_language() -> str:
    override = os.environ.get('DRSTOOL_LANG')
    saved = QSettings(APP_ID, APP_ID).value('appearance/language', 'auto')
    return language_tag(override or str(saved), QLocale.system().name())


def tr(s: str) -> str:
    return translate(s, current_language())
