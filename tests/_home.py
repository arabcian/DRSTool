"""One throw-away HOME for the whole test run. Import this before anything from drstool_gui.

QSettings fixes its file locations the first time it is used, and drstool_gui reads its
settings while it is imported. A test module that imported the package before the fake home
existed therefore sent every later QSettings write of the run to the developer's real
~/.config/drstool/drstool.conf — and a "reviewed" mark left there made the next run fail.
"""
import os
import tempfile
from pathlib import Path

_TMP = tempfile.TemporaryDirectory(prefix="drstool-tests-")
HOME = Path(_TMP.name)
os.environ["HOME"] = str(HOME)
for _k in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
    os.environ.pop(_k, None)
