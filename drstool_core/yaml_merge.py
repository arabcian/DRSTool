"""Lutris YAML merge that preserves user comments, quotes and unrelated keys.

Qt-free so the exact write plan can be regression-tested without a GUI.
"""
from __future__ import annotations

import io
from typing import Any


def merge_lutris_yaml(original_text: str, system_keys: dict[str, Any],
                      env_vars: dict[str, str], owned: dict | None = None):
    """Return (updated text, system added/changed, env added/changed,
    system removed, env removed), touching only requested or owned keys.

    If the round-trip YAML library is not installed, never silently discard
    comments or quote styles. In that case the caller is told to install it.
    """
    try:
        from ruamel.yaml import YAML
    except ImportError as exc:
        raise RuntimeError('Editing Lutris YAML requires ruamel.yaml to preserve '
                           'comments and quote styles') from exc
    parser = YAML(typ='rt')
    parser.preserve_quotes = True
    parser.width = 1_000_000
    doc = parser.load(original_text) or {}
    if not isinstance(doc, dict):
        raise ValueError("the game's YAML top level is not a mapping")
    if not isinstance(doc.get('system'), dict):
        doc['system'] = {}
    system = doc['system']
    owned = owned or {}

    sys_removed = set()
    for key, previous in owned.get('system', {}).items():
        if key not in system_keys and key in system and system[key] == previous:
            del system[key]
            sys_removed.add(key)
    sys_changed = set()
    for key, value in system_keys.items():
        if key in system and system[key] == value and type(system[key]) is type(value):
            continue
        system[key] = value
        sys_changed.add(key)

    env = system.get('env')
    if not isinstance(env, dict):
        # Do not fabricate an env object if there is nothing to set.
        env = {}
        if env_vars:
            system['env'] = env
    env_removed = set()
    for key, previous in owned.get('env', {}).items():
        if key not in env_vars and key in env and str(env[key]) == str(previous):
            del env[key]
            env_removed.add(key)
    env_changed = set()
    for key, value in env_vars.items():
        if key in env and str(env[key]) == str(value):
            continue
        env[key] = str(value)
        env_changed.add(key)
    if not env and isinstance(system.get('env'), dict):
        del system['env']

    if not (sys_changed or env_changed or sys_removed or env_removed):
        return original_text, sys_changed, env_changed, sys_removed, env_removed
    result = io.StringIO()
    parser.dump(doc, result)
    return result.getvalue(), sys_changed, env_changed, sys_removed, env_removed
