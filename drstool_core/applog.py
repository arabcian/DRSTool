"""DRSTool's own debug log file.

Errors that used to be swallowed silently are now recorded at DEBUG level by the
module that caught them (``_log = logging.getLogger(__name__)``). Nothing is written
unless ``setup()`` runs, so tests and imports stay quiet. The file is what a
"Sorun bildir" support bundle packs up (see support_bundle).
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PACKAGES = ("drstool_core", "drstool_gui")
_MAX_BYTES = 1_000_000
_BACKUPS = 3


def log_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "drstool" / "debug.log"


def setup() -> Path:
    """Send DEBUG records from DRSTool's packages to log_path(). Never raises:
    a read-only home directory must not stop the app from starting."""
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
    except OSError:
        return path
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for pkg in _PACKAGES:
        lg = logging.getLogger(pkg)
        lg.setLevel(logging.DEBUG)
        lg.addHandler(handler)
    return path
