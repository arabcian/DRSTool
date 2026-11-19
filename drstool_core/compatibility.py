"""Evidence-aware compatibility checks for optional environment catalog metadata.

'unknown' is a first-class outcome. Upstream and fork version numbers are not
interchangeable; a note in the bundled catalog is not live upstream verification.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Mapping

_DATA = Path(__file__).resolve().parent / 'data' / 'compatibility_rules.json'
_VERSION = re.compile(r'^v?(\d+)\.(\d+)(?:\.(\d+))?(?:$|[+._-])', re.I)


@lru_cache(maxsize=1)
def rules() -> dict:
    data = json.loads(_DATA.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('env'), dict):
        raise ValueError('invalid compatibility catalog')
    return data['env']


def version_tuple(value: str):
    match = _VERSION.match(str(value).strip())
    return tuple(int(x or 0) for x in match.groups()) if match else None


def assess(payload: Mapping, *, versions: Mapping | None = None, builds: Mapping | None = None) -> dict:
    """Read-only report. versions={'dxvk':'3.0'}, builds={'dxvk':'upstream'}.

    Versions and build origins must be supplied by caller and are NOT detected
    from GPU vendor or Proton labels. 'fork' is never treated as 'upstream'.
    """
    versions = versions or {}
    builds = builds or {}
    items = []
    for key, value in sorted((payload.get('env') or {}).items()):
        if value is None or str(value) == '':
            continue
        rule = rules().get(key)
        if rule is None:
            items.append({'key': key, 'status': 'unknown', 'reason': 'No version-specific evidence in the bundled metadata.', 'source': ''})
            continue
        component = rule.get('component', 'dxvk')
        version = version_tuple(versions.get(component, ''))
        raw_version = str(versions.get(component) or '')
        build = str(builds.get(component, 'unknown')).lower()
        status = 'unknown'
        reason = rule.get('note', 'Compatibility requires runtime verification.')
        if rule.get('fork_only'):
            if build == 'upstream':
                status = 'unsupported'
                reason = 'Bundled catalog identifies this key as fork-only; it is not an upstream option.'
            else:
                reason = 'Fork-specific option: the exact fork and build must be checked.'
        elif build == 'upstream' and version is not None:
            removed = version_tuple(rule.get('removed_upstream_at', ''))
            until = version_tuple(rule.get('supported_upstream_through', ''))
            if removed and version >= removed:
                status = 'unsupported'
                reason = f'Bundled catalog states upstream {component} removed this key from {rule["removed_upstream_at"]}.'
            elif until and version <= until:
                status = 'supported-by-catalog'
                reason = f'Bundled catalog documents this option through upstream {rule["supported_upstream_through"]}; runtime use is not verified.'
        elif build not in ('upstream', 'fork', 'unknown'):
            reason = 'Unknown build origin. Choose upstream, fork, or unknown.'
        items.append({'key': key, 'status': status, 'reason': reason,
                      'component': component, 'version': raw_version, 'build': build,
                      'source': rule.get('evidence', ''), 'metadata': rule.get('note', '')})
    summary = {s: sum(i['status'] == s for i in items) for s in
               ('unsupported', 'supported-by-catalog', 'unknown')}
    return {'items': items, 'summary': summary, 'read_only': True,
            'catalog_basis': 'bundled descriptions only; not a live upstream verification'}
