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
import stat
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import applog

_log = logging.getLogger(__name__)

# Do not let profile exports or debug logs disclose credentials accidentally.
_SENSITIVE_KEY = re.compile(r'(?:token|secret|password|passwd|api[_-]?key|auth(?:orization)?|'
                            r'private[_-]?key|access[_-]?key|client[_-]?secret|cookie)', re.I)
_SECRET_REGEXES = [
    # Quoted shell/JSON-style secrets may contain spaces; redact the whole value.
    (re.compile(r"(?im)((?<![A-Za-z0-9_-])(?:[A-Za-z0-9_-]{0,128}(?:token|password|passwd|secret|api[_-]?key|cookie|authorization)[A-Za-z0-9_-]{0,128})[\"']?\s*[:=]\s*)([\"'])(?:\\.|(?!\2)[^\\\r\n])*(?:\2|(?=\r?$))"), r'\1[REDACTED]'),
    (re.compile(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9_+.~\-/=]+'), r'\1 [REDACTED]'),
    (re.compile(r'(?i)([?&](?:token|access_token|api_key|key|secret|password)=)[^&#\s]+'),
     r'\1[REDACTED]'),
    (re.compile(r'(?i)((?:token|password|api[_-]?key|secret|authorization)\s*[:=]\s*[\"\']?)'
                r'[^\s,\"\']+'), r'\1[REDACTED]'),
    (re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|'
                r'AKIA[0-9A-Z]{16})\b'), '[REDACTED]'),
]


def _user_name() -> str:
    return os.environ.get("USER") or Path.home().name


def redact(text: str) -> str:
    home = str(Path.home()).rstrip("/")
    if len(home) > 1:
        text = text.replace(home + "/", "~/").replace(home, "~")
    for term, repl in ((_user_name(), "<user>"), (socket.gethostname(), "<host>")):
        if len(term) >= 3:
            text = re.sub(rf"(?<![\w.-]){re.escape(term)}(?![\w.-])", repl, text)
    for pattern, replacement in _SECRET_REGEXES:
        text = pattern.sub(replacement, text)
    return text


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {redact(str(k)): ('[REDACTED]' if _SENSITIVE_KEY.search(str(k))
                                else _redact_obj(v)) for k, v in obj.items()}
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


def build(dest: Path, profiles: Dict[str, Dict], lines: Iterable[str] = (),
          *, include_logs: bool = True, include_profiles: bool = True) -> Path:
    """Write the anonymised bundle to `dest` (a .zip path) and return it."""
    dest = Path(dest)
    log_files = sorted(applog.log_path().parent.glob("debug.log*"))[:32] if include_logs else []
    summary = list(lines) + ["", "[debug log]",
                             f"files: {', '.join(p.name for p in log_files) or 'none (no errors recorded yet)'}"]
    fd, temporary = tempfile.mkstemp(prefix='.support-', suffix='.zip', dir=dest.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("system.txt", redact("\n".join(summary)) + "\n")
            if include_profiles:
                zf.writestr("profiles.json", json.dumps(_redact_obj(profiles), indent=2, ensure_ascii=False) + "\n")
            for p in log_files:
                try:
                    log_fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(log_fd, 'rb') as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        cap = 2 * 1024 * 1024
                        # Keep the latest complete lines, never load an unlimited log.
                        truncated = info.st_size > cap
                        if truncated:
                            stream.seek(info.st_size - cap)
                            stream.readline(cap)
                        raw = stream.read(cap)
                    body = ('[Log truncated to final 2 MiB]\n' if truncated else '') + raw.decode('utf-8', 'replace')
                    zf.writestr(f"log/{p.name}", redact(body))
                except OSError:
                    _log.debug('log unavailable for support bundle: %s', p.name)
        os.replace(temporary, dest)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return dest
