"""Deterministic, side-effect-free profile changes for human review."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Change:
    path: str
    action: str
    before: Any
    after: Any


def plan(before: Mapping, after: Mapping) -> list[Change]:
    changes: list[Change] = []

    def visit(old: Mapping, new: Mapping, prefix: str) -> None:
        for key in sorted(set(old) | set(new), key=str):
            keystr = str(key)
            if any(ch in keystr for ch in '\n\r\x00'):
                raise ValueError('Unsafe key in profile')
            path = f'{prefix}.{keystr}' if prefix else keystr
            if key not in old:
                changes.append(Change(path, 'add', None, new[key]))
            elif key not in new:
                changes.append(Change(path, 'remove', old[key], None))
            elif isinstance(old[key], Mapping) and isinstance(new[key], Mapping):
                visit(old[key], new[key], path)
            elif old[key] != new[key]:
                changes.append(Change(path, 'modify', old[key], new[key]))

    visit(before, after, '')
    return changes


def render(changes: list[Change], *, limit: int = 80) -> str:
    lines = []
    for ch in changes[:limit]:
        if ch.action == 'add':
            lines.append(f'+ {ch.path}: {ch.after!r}')
        elif ch.action == 'remove':
            lines.append(f'- {ch.path}: {ch.before!r}')
        else:
            lines.append(f'~ {ch.path}: {ch.before!r} → {ch.after!r}')
    if len(changes) > limit:
        lines.append(f'... plus {len(changes) - limit} more changes')
    return '\n'.join(lines) if changes else 'No changes'
