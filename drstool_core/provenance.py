"""Qt-free profile-origin and live-process environment inspection.

A difference proves *what* reached a process, not which intermediate launcher
changed it. /proc/PID/environ can reflect the original exec environment only.
"""
from __future__ import annotations

import re
from typing import Mapping, Optional

_SECRET = re.compile(r'(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|AUTH|CREDENTIAL|COOKIE|PRIVATE[_-]?KEY)', re.I)


def redact(name: str, value: object) -> str:
    if value is None:
        return '—'
    if _SECRET.search(name):
        return '<redacted>'
    s = str(value)
    return s if len(s) <= 240 else s[:237] + '…'


def source_chain(raw_profiles: Mapping[str, dict], name: str) -> dict[str, list[str]]:
    """For each configured env var: original declaring layers and final override.

    Track explicit `unset` entries and synthesized DRS/arch values; does not
    assume that a base profile already contains flattened child values.
    """
    chain = []
    cur = name
    while cur in raw_profiles and cur not in chain:
        chain.append(cur)
        raw = raw_profiles[cur]
        cur = raw.get('base') if isinstance(raw, dict) else None
    env_sources: dict[str, list[str]] = {}
    drs_sources: dict[str, list[str]] = {}
    arch_sources: list[str] = []
    for n in reversed(chain):
        p = raw_profiles[n]
        if not isinstance(p, dict):
            continue
        for entry in (p.get('unset') or []):
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                section, key = entry
                if section == 'Env':
                    env_sources.pop(str(key), None)
                elif section == 'DRS':
                    drs_sources.pop(str(key).upper(), None)
                elif section == 'Arch':
                    arch_sources.clear()
        if p.get('arch'):
            arch_sources.append(n)
        for key in (p.get('settings') or {}):
            drs_sources.setdefault(str(key).upper(), []).append(n)
        for key in (p.get('env_vars') or {}):
            env_sources.setdefault(str(key), []).append(n)
    result: dict[str, list[str]] = {}
    if drs_sources:
        result['DXVK_NVAPI_DRS_SETTINGS'] = list(dict.fromkeys(
            f'{source} (DRS)' for chain in drs_sources.values() for source in chain))
    if arch_sources:
        result['DXVK_NVAPI_GPU_ARCH'] = [n + ' (GPU arch)' for n in arch_sources]
    # Environment keys take precedence over the synthesized DRS / GPU arch
    # variables in state_payload(). A later layer can override an earlier one.
    result.update(env_sources)
    return result


def inspect_environment(expected: Mapping[str, str], observed: Optional[Mapping[str, str]],
                        *, sources: Optional[Mapping[str, list[str]]] = None,
                        known_keys: tuple[str, ...] = (), show_values: bool = False) -> list[dict]:
    """One evidence row per configured or explicitly requested known variable.

    `observed=None` means unreadable (unverifiable), NOT that everything is
    absent. Observed-only known keys are 'inherited', not 'configured'.
    """
    sources = sources or {}
    obs = observed if observed is not None else {}
    names = set(expected) | (set(obs) & set(known_keys))
    rows = []
    for key in sorted(names):
        configured = key in expected
        want = expected.get(key)
        got = obs.get(key)
        if observed is None:
            status = 'unverifiable'
        elif not configured:
            status = 'inherited'
        elif key not in obs:
            status = 'absent'
        elif str(want) != str(got):
            status = 'overridden'
        else:
            status = 'configured'
        rows.append({'key': key, 'source': list(sources.get(key) or (['active editor'] if configured else ['process / launcher'])),
                     'status': status,
                     'expected': (str(want) if show_values and not _SECRET.search(key) and want is not None
                                  else redact(key, want)),
                     'actual': (str(got) if show_values and not _SECRET.search(key) and got is not None
                                else redact(key, got)),
                     'matches': status == 'configured'})
    return rows
