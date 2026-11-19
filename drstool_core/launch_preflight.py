"""Conservative, read-only pre-launch checks for games and saved profiles.

No process is started or privileged information collected. Unknown runtime state is
never treated as a confirmed incompatibility. Caller owns launch policy.
"""
from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from typing import Mapping, Sequence


def _finding(severity: str, code: str, message: str, evidence: str, check: str) -> dict:
    return dict(severity=severity, code=code, message=message, evidence=evidence, verify=check)


def evaluate(payload: Mapping, *, command: Sequence[str] = (), prefix: str = "",
             runtime: Mapping | None = None, env_base: Mapping | None = None,
             which=None, path_exists=None, path_is_dir=None, path_executable=None) -> dict:
    """Return findings (error/warn/info/unknown) and a status, without writes.

    Optional functions allow safe unit tests and avoid relying on host tool paths.
    `prefix` is the *Wine prefix* rather than STEAM_COMPAT_DATA_PATH.
    """
    env_base = os.environ if env_base is None else env_base
    which = which or shutil.which
    path_exists = path_exists or os.path.exists
    path_is_dir = path_is_dir or os.path.isdir
    path_executable = path_executable or (lambda p: os.access(p, os.X_OK))
    overrides = payload.get('env') or {}
    env = {**env_base, **overrides}
    findings = []
    add = lambda *args: findings.append(_finding(*args))
    command = list(command or ())

    if command:
        exe = str(command[0])
        if exe in ('%command%', '$@'):
            add('unknown', 'launch-placeholder', 'The launcher will supply the executable at runtime.',
                'Command contains an unresolved launcher placeholder.', 'Inspect the launch option in the launcher.')
        elif '/' in exe:
            if not path_exists(exe):
                add('error', 'executable-missing', 'Launch executable does not exist.',
                    f'Command first argument basename: {Path(exe).name}', 'Correct the executable path in launch options.')
            elif path_is_dir(exe) or not path_executable(exe):
                add('error', 'executable-unusable', 'Launch target is not an executable file.',
                    f'Command first argument basename: {Path(exe).name}', 'Check executable permission and file type.')
        elif not which(exe, path=env.get('PATH')):
            add('error', 'executable-not-in-path', f'Command is not in PATH: {exe}',
                f'First argument basename: {Path(exe).name}', 'Inspect PATH and the launch command.')
    else:
        add('unknown', 'launch-command-unknown', 'Game executable cannot be verified without a launch command.',
            'No command supplied.', 'Use drstool --preflight PROFILE -- COMMAND or inspect launcher settings.')

    if payload.get('gs_on'):
        if not which('gamescope', path=env.get('PATH')):
            add('error', 'gamescope-unavailable', 'Gamescope is enabled but no gamescope binary is found in PATH.',
                'Profile gamescope_enabled=true.', 'Install gamescope or disable the gamescope wrapper.')

    actual_prefix = prefix or str(env.get('WINEPREFIX') or '')
    if actual_prefix:
        p = os.path.expanduser(actual_prefix)
        if not path_exists(p):
            add('warn', 'prefix-not-created', 'Wine prefix does not exist yet; it may be created on first run.',
                'Explicit Wine prefix path was supplied.', 'Verify intended prefix and whether the runner creates it.')
        elif not path_is_dir(p):
            add('error', 'prefix-not-directory', 'Wine prefix points to a non-directory.',
                'Explicit Wine prefix path was supplied.', 'Select the correct prefix directory.')
    else:
        add('unknown', 'prefix-unknown', 'Wine prefix is not specified; launcher may select it dynamically.',
            'No WINEPREFIX and no game prefix supplied.', 'Inspect launcher and STEAM_COMPAT_DATA_PATH.')

    for name in ('VK_LAYER_PATH', 'VK_ADD_LAYER_PATH'):
        value = env.get(name, '')
        if not value:
            continue
        parts = str(value).split(os.pathsep)
        absent = [p for p in parts if p and not path_is_dir(os.path.expanduser(p))]
        if absent:
            add('warn', 'layer-dir-missing', f'{name} contains a missing directory.',
                f'{name}: {len(absent)} nonexistent path(s)', 'Inspect configured Vulkan layer search paths.')
    preload = str(env.get('LD_PRELOAD') or '').strip()
    if preload:
        try:
            paths = shlex.split(preload)
        except ValueError:
            paths = []
            add('warn', 'preload-malformed', 'LD_PRELOAD cannot be parsed as a shell-style list.',
                'LD_PRELOAD syntax.', 'Check quotes and whitespace.')
        missing = [x for x in paths if x.startswith('/') and not path_exists(x)]
        if missing:
            add('warn', 'preload-missing', f'LD_PRELOAD references {len(missing)} missing absolute file(s).',
                'One or more absolute paths do not exist.', 'Inspect LD_PRELOAD paths; relative library names need runtime lookup.')

    if (str(env.get('PROTON_ENABLE_NVAPI', '')).strip().lower() in ('1', 'true', 'yes') and
            str(env.get('PROTON_DISABLE_NVAPI', '')).strip().lower() in ('1', 'true', 'yes')):
        add('warn', 'nvapi-conflicting-switches', 'NVAPI enable and disable switches both set.',
            'PROTON_ENABLE_NVAPI and PROTON_DISABLE_NVAPI are enabled.',
            'Check the profile linter for a suggested resolution.')

    runtime = runtime or {}
    if runtime.get('runner') and not runtime.get('configured'):
        add('unknown', 'runner-build-unknown', 'Runner build cannot be verified from current metadata.',
            f'Runner: {runtime["runner"]}', 'Check the game compatibility tool in your launcher.')
    if str(env.get('FLM_MODE', '')).lower() not in ('', 'off') and not env.get('VK_INSTANCE_LAYERS'):
        add('unknown', 'flm-layer-unverified', 'FLM configuration exists, but layer activation is not proven.',
            'Layer activation requires a runtime Vulkan inspection.',
            'Inspect the game log and active Vulkan layers (not just VK_INSTANCE_LAYERS).')
    return {'status': 'blocked' if any(f['severity'] == 'error' for f in findings) else
            'review' if any(f['severity'] == 'warn' for f in findings) else 'ready-unverified',
            'counts': {s: sum(f['severity'] == s for f in findings) for s in
                       ('error', 'warn', 'info', 'unknown')}, 'findings': findings,
            'read_only': True}
