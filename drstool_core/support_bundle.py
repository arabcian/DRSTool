"""'Sorun bildir' support bundle: system info, the debug log and the saved profiles,
anonymised, in one zip file that can be attached to a bug report.

Anonymising replaces the home folder with "~", the user name with "<user>" and the
host name with "<host>" in every text that goes into the archive.
"""

import json
import logging
import os
import platform
import re
import socket
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import applog

_log = logging.getLogger(__name__)


def _user_name() -> str:
    return os.environ.get("USER") or Path.home().name


def redact(text: str) -> str:
    home = str(Path.home()).rstrip("/")
    if len(home) > 1:
        text = text.replace(home + "/", "~/").replace(home, "~")
    for term, repl in ((_user_name(), "<user>"), (socket.gethostname(), "<host>")):
        if len(term) >= 3:
            text = re.sub(rf"(?<![\w.-]){re.escape(term)}(?![\w.-])", repl, text)
    return text


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {redact(str(k)): _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj


def system_lines(si: Optional[Any] = None) -> List[str]:
    """Plain-text system summary. `si` is a drstool_gui.sysinfo.SysInfo (or None)."""
    lines = [
        f"created: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"python: {platform.python_version()} ({platform.python_implementation()})",
        f"platform: {platform.platform()}",
        f"machine: {platform.machine()}",
        f"session: {os.environ.get('XDG_SESSION_TYPE', '?')} / desktop {os.environ.get('XDG_CURRENT_DESKTOP', '?')}",
    ]
    try:
        lines += ["", "[os-release]"] + Path("/etc/os-release").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        _log.debug("no /etc/os-release", exc_info=True)
    if si is not None:
        lines += ["", "[gpu]",
                  f"nvidia driver: {si.nv_driver or '-'}",
                  f"nvidia gpus: {', '.join(si.nv_gpus) or '-'}",
                  f"vulkan ok: {si.vk_ok}",
                  f"vulkan gpus: {', '.join(g.name for g in si.vk_gpus) or '-'}",
                  f"vulkaninfo error: {si.vulkaninfo_error or '-'}",
                  f"ntsync device: {si.ntsync_dev}"]
    return lines


def build(dest: Path, profiles: Dict[str, Dict], lines: Iterable[str] = ()) -> Path:
    """Write the anonymised bundle to `dest` (a .zip path) and return it."""
    log_files = sorted(applog.log_path().parent.glob("debug.log*"))
    summary = list(lines) + ["", "[debug log]",
                             f"files: {', '.join(p.name for p in log_files) or 'none (no errors recorded yet)'}"]
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("system.txt", redact("\n".join(summary)) + "\n")
        zf.writestr("profiles.json", json.dumps(_redact_obj(profiles), indent=2, ensure_ascii=False) + "\n")
        for p in log_files:
            zf.writestr(f"log/{p.name}", redact(p.read_text(encoding="utf-8", errors="replace")))
    return dest
