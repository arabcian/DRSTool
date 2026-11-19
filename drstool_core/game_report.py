"""F11: privacy-minimized, per-game diagnostic report and opt-in ZIP exporter.

No shell, no settings changes and no implicit uploads. Log excerpts are only
included when explicitly supplied; application secrets are filtered recursively.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from . import diagnostics
from .game_links import LinkStore
from .kernel_events import correlate
from .performance_history import trend_report
from .session_recorder import SessionStore, _safe_game_key
from .support_bundle import _redact_obj, redact

_MAX_LOG = 128 * 1024


def _session_summary(session: dict) -> dict:
    keys = ('id', 'started_at', 'ended_at', 'status', 'exit_code', 'signal', 'profile',
            'profile_fp', 'scene', 'game_key', 'runtime', 'performance')
    safe = {k: session[k] for k in keys if k in session}
    if isinstance(safe.get('performance'), dict):
        # Session captures include an on-disk CSV path which is not needed in a diagnostic export.
        safe['performance'] = {k: v for k, v in safe['performance'].items()
                               if k in ('avg', 'low1', 'low01', 'p99', 'stutters', 'dur',
                                        'kind', 'mtime', 'scene', 'driver', 'proton', 'dxvk', 'vkd3d')}
    return safe


def compile_report(game_key: str, *, session_id: str = '', include_kernel: bool = False,
                   session_store: SessionStore | None = None, link_store: LinkStore | None = None,
                   event_collector=None, system_info=None) -> dict:
    """Produce in-memory, shareable summary, omitting executable argv and environment."""
    _safe_game_key(game_key)
    if not game_key:
        raise ValueError('game key required')
    store = session_store if session_store is not None else SessionStore()
    links = link_store if link_store is not None else LinkStore()
    link = links.get(game_key) or {}
    if session_id:
        session = store.read(session_id)
        if session.get('game_key') != game_key:
            raise ValueError('session does not belong to selected game')
        sessions = [session]
    else:
        sessions = store.recent(game_key=game_key, limit=12)
    latest = sessions[0] if sessions else None
    perf = trend_report(link.get('perf') or [])
    # Performance-history groups contain absolute capture filenames used by the UI,
    # but sharing those filenames is unnecessary and may expose private directories.
    for group in perf['groups']:
        group.pop('captures', None)
    event_result = {'status': 'not-requested', 'events': [], 'count': 0,
                    'disclaimer': 'Kernel events are opt-in and only show temporal correlation.'}
    if include_kernel:
        if latest:
            kw = {'collector': event_collector} if event_collector is not None else {}
            try:
                event_result = correlate(latest, **kw)
            except (OSError, ValueError) as ex:
                event_result = {'status': 'unavailable', 'reason': type(ex).__name__,
                                'count': 0, 'events': []}
        else:
            event_result = {'status': 'no-session', 'events': [], 'count': 0,
                            'reason': 'A recorded game session is required for time correlation'}
    info = diagnostics.gather() if system_info is None else system_info
    # Custom game IDs often contain a full user-selected executable path.
    exported_key = ('custom:sha256:' + hashlib.sha256(game_key.encode()).hexdigest()[:16]
                    if game_key.startswith('custom:') else game_key)
    report = {'schema': 1, 'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'game_key': exported_key, 'linked_profile': link.get('profile'),
              'profile_fingerprint': link.get('fp', ''),
              'last_applied_at': link.get('applied_at'),
              'sessions': [_session_summary(s) for s in sessions],
              'performance': perf, 'kernel': event_result, 'system': info,
              'notes': [
                  'All records are observations, not proof of a cause or a fault.',
                  'No full launch command, environment variables or passwords are exported.',
                  'Kernel events depend on permissions to read the system journal.',
                  'Read the report before sharing it externally.'
              ]}
    # Mask per-session custom keys too; otherwise a path reappears in sessions[]
    for entry in report['sessions']:
        if str(entry.get('game_key', '')).startswith('custom:'):
            entry['game_key'] = exported_key
    return _redact_obj(report)


def _log_tail(log_file: Path) -> str:
    """Read an opt-in bounded text tail without following symbolic links."""
    log_file = Path(log_file)
    fd = os.open(log_file, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('log input must be a regular file')
        os.lseek(fd, max(info.st_size - _MAX_LOG, 0), os.SEEK_SET)
        buf = os.read(fd, _MAX_LOG)
    finally:
        os.close(fd)
    # Avoid first partial line if the file was truncated to its last 128 KiB.
    if info.st_size > _MAX_LOG and b'\n' in buf:
        buf = buf.split(b'\n', 1)[1]
    return redact(buf.decode('utf-8', errors='replace'))


def default_report_path(*, game_key: str = '') -> Path:
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
    from .dll_ops import _slug
    return state / 'drstool' / 'reports' / f'{_slug(game_key)}-{time.strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:6]}.zip'


def export_zip(output: Path, report: dict, *, log_file: Path | None = None) -> Path:
    """Write 0600 ZIP atomically to an explicitly chosen destination."""
    from .file_transactions import assert_safe_path
    path = Path(output).expanduser().absolute()
    if path.suffix.lower() != '.zip':
        raise ValueError('diagnostic export must end in .zip')
    # Only create parents of the normal app-owned report directory in callers;
    # an explicitly chosen parent must exist to prevent arbitrary directory creation.
    assert_safe_path(path, allow_missing=True)
    if path.exists():
        raise FileExistsError(f'report exists: {path}')
    excerpt = _log_tail(log_file) if log_file is not None else None
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix='.drstool-report-', suffix='.zip', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr('report.json', json.dumps(_redact_obj(report), indent=2,
                                                    ensure_ascii=False, allow_nan=False) + '\n')
                if excerpt is not None:
                    z.writestr('log_excerpt.txt', excerpt)
                z.writestr('README.txt', 'DRSTool game diagnostic bundle. Local-only export.\n'
                           'Review all contents before sharing. Journal correlations are not causal.\n'
                           'Original game paths, launch command and environment are intentionally omitted.\n')
            stream.flush()
            os.fsync(stream.fileno())
        assert_safe_path(path, allow_missing=True)
        if path.exists():
            raise FileExistsError(f'report appeared during export: {path}')
        os.link(tmp_name, path, follow_symlinks=False)  # atomic no-overwrite reservation
        os.unlink(tmp_name)
        tmp_name = None
        return path
    finally:
        if tmp_name is not None:
            os.unlink(tmp_name)


def export_game(game_key: str, *, output: Path | None = None, include_kernel: bool = False,
                log_file: Path | None = None, session_id: str = '', **kwargs) -> Path:
    report = compile_report(game_key, include_kernel=include_kernel,
                            session_id=session_id, **kwargs)
    path = output if output is not None else default_report_path(game_key=game_key)
    if output is None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink():
            raise OSError('unsafe report directory')
        path.parent.chmod(0o700)
    return export_zip(path, report, log_file=log_file)
