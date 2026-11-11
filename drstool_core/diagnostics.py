"""Read-only diagnostic summary independent of Qt and privileged operations.

Only counts/statuses are shown for profiles, transactions and game prefixes;
paths, individual profile contents and executable launch flags are not exported.
"""
from __future__ import annotations
import importlib.util
import json
import os
import platform
import hashlib
from datetime import datetime, timezone
import shutil
from pathlib import Path

from .support_bundle import _redact_obj


def _safe_count(path: Path, suffix: str = '') -> int:
    try:
        if not path.is_dir() or path.is_symlink():
            return 0
        return sum(1 for p in path.iterdir() if not p.is_symlink() and
                   (p.name.endswith(suffix) if suffix else p.is_dir()))
    except OSError:
        return 0


def transaction_status(data_home: Path, *, max_entries: int = 20, max_dirs: int = 2000) -> list[dict]:
    """Read the newest transaction journals without revealing game paths.

    Only the bounded application backup tree is traversed; symlinks are never
    followed and raw journal content is not included in any report.
    """
    if max_entries <= 0 or max_dirs <= 0:
        return []
    root = data_home / 'drstool' / 'dll-backups'
    if not root.is_dir() or root.is_symlink():
        return []
    records: list[tuple[float, dict]] = []
    visited = 0
    try:
        for game in root.iterdir():
            if visited >= max_dirs:
                break
            if not game.is_dir() or game.is_symlink():
                continue
            for backup in game.iterdir():
                visited += 1
                if visited >= max_dirs:
                    break
                if not backup.is_dir() or backup.is_symlink():
                    continue
                for manifest in backup.iterdir():
                    if manifest.name != 'manifest.json' and not (
                            manifest.name == 'transaction.json' or
                            (manifest.name.startswith('prefix-') and manifest.name.endswith('.json'))):
                        continue
                    if not manifest.is_file() or manifest.is_symlink():
                        continue
                    try:
                        stat = manifest.stat()
                        if stat.st_size > 1024 * 1024:
                            raise ValueError('oversized transaction journal')
                        with manifest.open(encoding='utf-8') as stream:
                            data = json.load(stream)
                        if not isinstance(data, dict):
                            raise ValueError('not a transaction journal')
                        state = str(data.get('state', 'unknown'))[:64]
                        entries = data.get('entries')
                        count = len(entries) if isinstance(entries, list) else 0
                    except (OSError, ValueError, UnicodeError, TypeError):
                        stat = manifest.stat()
                        state, count = 'unreadable', 0
                    identifier = hashlib.sha256(str(manifest.relative_to(root)).encode()).hexdigest()[:10]
                    records.append((stat.st_mtime, {
                        'id': identifier,
                        'kind': 'prefix' if manifest.name.startswith('prefix-') else 'game-dll',
                        'updated_utc': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                        'state': state, 'file_count': count,
                        'needs_attention': state in ('prepared', 'recovery-needed', 'unreadable'),
                    }))
    except OSError:
        pass
    records.sort(key=lambda record: record[0], reverse=True)
    return [record for _, record in records[:max_entries]]


def gather(*, home: Path | None = None, env: dict | None = None, si=None) -> dict:
    home = home or Path.home()
    env = os.environ if env is None else env
    tools = ('nvidia-smi', 'vulkaninfo', 'wine', 'protontricks', 'gamescope', 'mangohud', 'zstd')
    config_home = Path(env.get('XDG_CONFIG_HOME') or home / '.config')
    data_home = Path(env.get('XDG_DATA_HOME') or home / '.local/share')
    result = {
        'platform': platform.platform(), 'python': platform.python_version(),
        'session': env.get('XDG_SESSION_TYPE', 'unknown'),
        'modules': {name: importlib.util.find_spec(name) is not None for name in ('PySide6', 'yaml', 'ruamel')},
        'tools': {tool: bool(shutil.which(tool)) for tool in tools},
        'ntsync': Path('/dev/ntsync').exists(),
        'profiles_file_exists': (config_home / 'drstool/profiles.json').is_file(),
        'game_links_file_exists': (config_home / 'drstool/game_links.json').is_file(),
        'proton_tools_count': _safe_count(data_home / 'Steam/compatibilitytools.d'),
        'transactions': transaction_status(data_home),
    }
    result['transactions_need_attention'] = sum(x['needs_attention'] for x in result['transactions'])
    if si is not None:
        result['gpu'] = {
            'nvidia_driver': getattr(si, 'nv_driver', '') or 'not detected',
            'nvidia_gpu_count': len(getattr(si, 'nv_gpus', [])),
            'vulkan_ready': bool(getattr(si, 'vk_ok', False)),
            'vulkan_gpu_count': len(getattr(si, 'vk_gpus', [])),
        }
    return _redact_obj(result)


def to_json(**kwargs) -> str:
    return json.dumps(gather(**kwargs), ensure_ascii=False, indent=2)
