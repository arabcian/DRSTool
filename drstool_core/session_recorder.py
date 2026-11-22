"""Opt-in, Qt-free game session recording for ``drstool --record``.

Only metadata (never full command lines or the whole environment) is persisted.
The supervisor records the *launched command* and its observed descendants, not
an unverified assertion that the game's rendering process stayed alive.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

_VERSION = 1
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def sessions_path() -> Path:
    base = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
    return base / 'drstool' / 'sessions'


def _private_directory(path: Path) -> None:
    """Create a private directory; reject existing symlinks and foreign owners."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f'unsafe session directory: {path}')
    if st.st_mode & 0o077:
        path.chmod(0o700)


def _write_private(path: Path, obj: dict) -> None:
    """A 0600, replacement-atomic JSON record. UUID names prevent path traversal."""
    if not _ID_RE.fullmatch(path.stem):
        raise ValueError('invalid session id')
    _private_directory(path.parent)
    if path.is_symlink():
        raise OSError('refusing to overwrite a session symlink')
    fd, tmp = tempfile.mkstemp(prefix='.session-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _proc_start(pid: int) -> Optional[int]:
    """Linux /proc starttime, used with PID to detect PID reuse."""
    try:
        txt = Path(f'/proc/{pid}/stat').read_text()
        return int(txt[txt.rfind(')') + 2:].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _observed_children(root_pid: int, *, max_processes: int = 24) -> list[dict]:
    """Best-effort process descendants. Does not inspect private command lines."""
    try:
        proc_names = [name for name in os.listdir('/proc') if name.isdigit()]
    except OSError:
        return []
    parents: dict[int, int] = {}
    observed: dict[int, dict] = {}
    for name in proc_names:
        pid = int(name)
        try:
            if os.stat(f'/proc/{name}').st_uid != os.getuid():
                continue
            txt = Path(f'/proc/{name}/stat').read_text()
            tail = txt[txt.rfind(')') + 2:].split()
            parents[pid] = int(tail[1])
            observed[pid] = {'pid': pid, 'name': txt[txt.find('(') + 1:txt.rfind(')')][:64],
                             'start_ticks': int(tail[19])}
        except (OSError, ValueError, IndexError):
            continue
    descendants = {root_pid}
    for _ in range(16):
        more = {pid for pid, ppid in parents.items() if ppid in descendants}
        if more <= descendants:
            break
        descendants.update(more)
    return [observed[pid] for pid in sorted(descendants) if pid != root_pid and pid in observed][:max_processes]


def _runtime_versions(env: dict) -> dict:
    """Known evidence only. Absence is unknown; never guess DXVK from filenames."""
    result = {}
    for k, out in (('PROTONPATH', 'proton'), ('STEAM_COMPAT_TOOL_PATHS', 'steam_compat_tool')):
        v = env.get(k)
        if v:
            result[out] = Path(str(v).split(':', 1)[0].rstrip('/')).name
    try:
        nvidia = Path('/proc/driver/nvidia/version').read_text(errors='replace')
        m = re.search(r'(?:Kernel Module|NVIDIA UNIX)\s+([0-9]+(?:\.[0-9]+){1,3})', nvidia)
        if m:
            result['nvidia_driver'] = m.group(1)
    except OSError:
        pass
    return result


def _safe_game_key(key: str) -> str:
    if key and not re.fullmatch(r'(steam|lutris|heroic|bottles|custom):[^\x00-\x1f]{1,180}', key):
        raise ValueError('game key must be launcher:id (steam/lutris/heroic/bottles/custom)')
    return key


def inferred_game_key(env: dict) -> str:
    for var in ('SteamAppId', 'SteamGameId'):
        value = str(env.get(var, ''))
        if re.fullmatch(r'\d{1,12}', value) and value != '0':
            return f'steam:{value}'
    return ''


class SessionStore:
    def __init__(self, directory: Optional[Path] = None):
        self.directory = Path(directory) if directory is not None else sessions_path()

    def begin(self, *, profile: str, profile_fp: str, command: list[str], env: dict,
              game_key: str = '', scene: str = '', capture: str = '') -> dict:
        if not command:
            raise ValueError('no launch command')
        game_key = _safe_game_key(game_key or inferred_game_key(env))
        now = time.time()
        entry = {'version': _VERSION, 'id': uuid.uuid4().hex, 'status': 'starting',
                 'started_at': now, 'ended_at': None, 'supervisor_pid': os.getpid(),
                 'supervisor_start_ticks': _proc_start(os.getpid()),
                 'profile': profile, 'profile_fp': profile_fp,
                 'game_key': game_key, 'scene': scene[:160],
                 'executable': Path(command[0]).name[:128],
                 'runtime': _runtime_versions(env), 'processes': [],
                 'capture': capture, 'exit_code': None, 'signal': None}
        self.write(entry)
        return entry

    def write(self, entry: dict) -> None:
        _write_private(self.directory / f'{entry["id"]}.json', entry)

    def finish(self, entry: dict, *, returncode: Optional[int] = None,
               error: str = '', capture_metrics: Optional[dict] = None) -> dict:
        entry['ended_at'] = time.time()
        if error:
            entry['status'] = 'launch_failed'
            entry['error'] = str(error)[:256]
        else:
            entry['exit_code'] = returncode if returncode is not None and returncode >= 0 else None
            entry['signal'] = -returncode if returncode is not None and returncode < 0 else None
            entry['status'] = ('signaled' if entry['signal'] is not None else
                               'completed' if returncode == 0 else 'nonzero_exit')
        if capture_metrics:
            entry['performance'] = capture_metrics
        self.write(entry)
        return entry

    def read(self, session_id: str) -> dict:
        if not _ID_RE.fullmatch(session_id):
            raise ValueError('invalid session id')
        path = self.directory / f'{session_id}.json'
        if path.is_symlink():
            raise OSError('refusing session symlink')
        entry = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(entry, dict) or entry.get('id') != session_id:
            raise ValueError('corrupt session record')
        if entry.get('status') in ('starting', 'running'):
            pid, start = entry.get('supervisor_pid'), entry.get('supervisor_start_ticks')
            if not isinstance(pid, int) or start is None or _proc_start(pid) != start:
                entry['status'] = 'interrupted'     # don't mutate the original for forensic integrity
        return entry

    def recent(self, *, game_key: str = '', limit: int = 40) -> list[dict]:
        if game_key:
            _safe_game_key(game_key)
        if not self.directory.is_dir():
            return []
        entries = []
        # At most the most recent 300 records. Avoid unbounded IO on very old installs.
        try:
            paths = sorted((p for p in self.directory.iterdir() if _ID_RE.fullmatch(p.stem)
                            and p.suffix == '.json' and not p.is_symlink()),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:300]
        except OSError:
            return []
        for p in paths:
            try:
                e = self.read(p.stem)
            except (OSError, ValueError, TypeError, KeyError):
                continue
            if not game_key or e.get('game_key') == game_key:
                entries.append(e)
                if len(entries) >= max(1, min(limit, 300)):
                    break
        return entries


def supervise(store: SessionStore, entry: dict, command: list[str], env: dict,
              *, update_interval: float = 2.0) -> tuple[int, dict]:
    """Start a foreground process, forward termination, preserve actual exit code.

    This is opt-in because start_new_session and supervision necessarily differ
    from --run's exec replacement. SIGKILL leaves a detectable partial record.
    """
    # The game gets its own session (so a stop reaches its whole tree via
    # killpg), which also means SIGKILL of this supervisor would orphan it:
    # PR_SET_PDEATHSIG sends it SIGTERM when we die. The kernel clears it when
    # a setuid / file-capability binary is exec'd as the command itself.
    parent = os.getpid()
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        prctl = None

    def die_with_parent():
        if prctl is not None:
            prctl(1, signal.SIGTERM, 0, 0, 0)        # 1 = PR_SET_PDEATHSIG
            if os.getppid() != parent:               # we already lost the race
                os._exit(1)

    try:
        child = subprocess.Popen(command, env=env, start_new_session=True, preexec_fn=die_with_parent)
    except OSError as ex:
        store.finish(entry, error=f'{type(ex).__name__}: {ex}')
        return 127, entry
    entry.update(status='running', pid=child.pid, pid_start_ticks=_proc_start(child.pid))
    store.write(entry)
    signals_received = []
    saved_handlers = {}

    def forward(sig, _frame):
        signals_received.append(sig)
        try:
            os.killpg(child.pid, sig)
        except OSError:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            saved_handlers[sig] = signal.signal(sig, forward)
        except ValueError:   # only main thread can install signal handlers
            pass
    seen = set()
    try:
        while True:
            try:
                rc = child.wait(timeout=update_interval)
                break
            except subprocess.TimeoutExpired:
                if child.poll() is not None:
                    rc = child.returncode
                    break
                for proc in _observed_children(child.pid):
                    key = (proc['pid'], proc['start_ticks'])
                    if key not in seen and len(seen) < 48:
                        seen.add(key)
                        entry['processes'].append(proc)
        entry['termination_requested'] = bool(signals_received)
        store.finish(entry, returncode=rc)
    finally:
        for sig, handler in saved_handlers.items():
            signal.signal(sig, handler)
    return (128 - rc if rc < 0 else rc), entry
