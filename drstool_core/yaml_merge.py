"""Lutris YAML merge that preserves user comments, quotes and unrelated keys.

Qt-free so the exact write plan can be regression-tested without a GUI.
"""
from __future__ import annotations

import io
from typing import Any


_YAML11_WORDS = {'y', 'n', 'yes', 'no', 'on', 'off', 'true', 'false', 'null', '~', ''}


def _lutris_env_value(value: Any):
    """The env value as a string that Lutris reads back as a string.

    ruamel (YAML 1.2) writes OFF / on / no / yes unquoted, but Lutris loads its configs with
    PyYAML (YAML 1.1), where those become booleans; Lutris then fails to start the
    pre-launch script ("expected str, bytes or os.PathLike object, not bool"). Such values are
    written quoted.
    """
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString
    text = str(value)
    try:
        import yaml
        if not isinstance(yaml.safe_load(text), str):
            return DoubleQuotedScalarString(text)
    except ImportError:
        if text.lower() in _YAML11_WORDS:
            return DoubleQuotedScalarString(text)
    except yaml.YAMLError:
        pass
    return text


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
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString
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
        new = _lutris_env_value(value)
        # A plain (unquoted) entry PyYAML would misread is rewritten even when the text is equal.
        if key in env and str(env[key]) == str(value) and not (
                isinstance(new, DoubleQuotedScalarString) and type(env[key]) is str):
            continue
        env[key] = new
        env_changed.add(key)
    if not env and isinstance(system.get('env'), dict):
        del system['env']

    if not (sys_changed or env_changed or sys_removed or env_removed):
        return original_text, sys_changed, env_changed, sys_removed, env_removed
    result = io.StringIO()
    parser.dump(doc, result)
    return result.getvalue(), sys_changed, env_changed, sys_removed, env_removed
