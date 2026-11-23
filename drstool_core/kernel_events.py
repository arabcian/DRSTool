"""F06: bounded, read-only Linux kernel event collection and session correlation.

The temporal relationship is evidence, never proof that a game caused an event.
No sudo, shell, process scanning or permanent log collection is performed.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
from datetime import datetime
from typing import Callable, Iterable

from .support_bundle import redact

_UNIX_LINE = re.compile(r'^\s*(\d{9,12}(?:\.\d+)?)\s+\S+\s+(?:kernel:\s*)?(.*)$')
_BDF = re.compile(r'\b(?:PCI:)?((?:[0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}(?:\.[0-7])?)\b')
_RULES = (
    ('xid', 'critical', re.compile(r'\bXid\b', re.I)),
    ('gpu-reset', 'critical', re.compile(r'\b(?:GPU has fallen off|GPU reset|GPU recovery|GPU is lost|fallen off the bus)\b', re.I)),
    ('pcie-aer', 'warning', re.compile(r'\b(?:AER:|PCIe Bus Error|DPC: containment|pcieport.*?(?:error|timeout))\b', re.I)),
    ('oom', 'warning', re.compile(r'(?:\bOut of memory:|\boom-kill:|\bKilled process \d+ .*total-vm)', re.I)),
    ('nvidia-uvm', 'warning', re.compile(r'\b(?:nvidia-uvm|NVRM:).*?\b(?:fault|error|timeout|failed)\b', re.I)),
    ('hardware', 'warning', re.compile(r'\b(?:Hardware Error|WHEA|MCE:|Machine check events logged)\b', re.I)),
)


def parse_lines(lines: Iterable[str], *, limit: int = 500) -> list[dict]:
    """Extract relevant entries from journalctl -k -o short-unix output."""
    events = []
    for line in lines:
        match = _UNIX_LINE.match(line)
        if not match:
            continue
        try:
            timestamp = float(match.group(1))
        except ValueError:
            continue
        if not math.isfinite(timestamp) or timestamp <= 0:
            continue
        message = match.group(2).strip()
        if message.startswith('kernel: '):
            message = message[8:]
        for category, severity, pattern in _RULES:
            if pattern.search(message):
                xid = None
                if category == 'xid':
                    # NVIDIA format: Xid (PCI:0000:01:00): 154, pid=...
                    code = re.search(r'\bXid\s*\([^)]*\)\s*:\s*(\d{1,4})', message, re.I)
                    if code:
                        xid = int(code.group(1))
                bdf = _BDF.search(message)
                events.append({'timestamp': timestamp, 'category': category, 'severity': severity,
                               'xid': xid, 'pci_device': bdf.group(1).lower() if bdf else None,
                               'message': redact(message[:440])})
                break
        if len(events) >= max(1, min(limit, 5000)):
            break
    return events


def collect(start: float, end: float, *, runner: Callable = subprocess.run,
            max_lines: int = 5000) -> dict:
    """Consult *unprivileged* journalctl; failure is reported, never treated as no events."""
    if not all(isinstance(t, (float, int)) and math.isfinite(t) for t in (start, end)):
        raise ValueError('non-finite event window')
    if start <= 0 or end < start or end - start > 14 * 86400:
        raise ValueError('event window outside supported bounds')
    if not shutil.which('journalctl') and runner is subprocess.run:
        return {'status': 'unavailable', 'reason': 'journalctl not installed', 'events': []}
    command = ['journalctl', '--no-pager', '--quiet', '--kernel', '--output=short-unix',
               '--lines', str(max(100, min(int(max_lines), 10000))),
               '--since', datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M:%S'),
               '--until', datetime.fromtimestamp(end).strftime('%Y-%m-%d %H:%M:%S')]
    try:
        result = runner(command, capture_output=True, text=True, errors='replace', timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return {'status': 'unavailable', 'reason': type(ex).__name__, 'events': []}
    if result.returncode != 0:
        # Journal errors may include paths or host identifiers; avoid echoing stderr.
        return {'status': 'unavailable', 'reason': f'journalctl returned {result.returncode} (permissions or journal)',
                'events': []}
    return {'status': 'ok', 'reason': 'Only events visible to the current user are included',
            'events': parse_lines(result.stdout.splitlines())}


_ISO_LINE = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:[.,]\d+)?(?:[+-]\d\d:?\d\d)?)\s+(.*)$')


def parse_dmesg_iso(lines: Iterable[str], start: float, end: float, *, limit: int = 500) -> list[dict]:
    """`dmesg --time-format=iso` lines inside [start, end] -> the same entries as parse_lines."""
    unix = []
    for line in lines:
        m = _ISO_LINE.match(line)
        if not m:
            continue
        try:
            t = datetime.fromisoformat(m.group(1).replace(',', '.')).timestamp()
        except ValueError:
            continue
        if start <= t <= end:
            unix.append(f'{t:.6f} host kernel: {m.group(2)}')
    return parse_lines(unix, limit=limit)


def collect_dmesg(start: float, end: float, *, runner: Callable = subprocess.run) -> dict:
    """Fallback for systems without a readable journal (OpenRC, no systemd-journal group).
    Needs kernel.dmesg_restrict=0; a refusal is reported, never treated as no events."""
    if not shutil.which('dmesg') and runner is subprocess.run:
        return {'status': 'unavailable', 'reason': 'dmesg not installed', 'events': []}
    try:
        result = runner(['dmesg', '--time-format=iso', '--nopager'], capture_output=True, text=True,
                        errors='replace', timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired) as ex:
        return {'status': 'unavailable', 'reason': type(ex).__name__, 'events': []}
    if result.returncode != 0:
        return {'status': 'unavailable', 'reason': f'dmesg returned {result.returncode} '
                '(kernel.dmesg_restrict=1 needs root)', 'events': []}
    return {'status': 'ok', 'reason': 'Read from dmesg (kernel ring buffer; old entries may have rotated out)',
            'events': parse_dmesg_iso(result.stdout.splitlines(), start, end)}


def collect_any(start: float, end: float, *, runner: Callable = subprocess.run) -> dict:
    """The journal first, dmesg when the journal cannot be read."""
    journal = collect(start, end, runner=runner)
    if journal['status'] == 'ok':
        return journal
    ring = collect_dmesg(start, end, runner=runner)
    if ring['status'] == 'ok':
        return ring
    return {'status': 'unavailable', 'reason': f"journal: {journal['reason']}; dmesg: {ring['reason']}",
            'events': []}


def correlate(session: dict, *, before_seconds: int = 30, after_seconds: int = 30,
              collector: Callable = collect, now: float | None = None) -> dict:
    """Correlate a recorded session and journal events. No causal attribution."""
    from time import time
    started = session.get('started_at')
    ended = session.get('ended_at') or (time() if now is None else now)
    if not isinstance(started, (int, float)) or not isinstance(ended, (int, float)):
        raise ValueError('session has no valid time window')
    if not all(map(math.isfinite, (started, ended))) or started <= 0 or ended < started:
        raise ValueError('invalid session timestamps')
    if ended - started > 14 * 86400:
        raise ValueError('session window exceeds maximum permitted duration')
    before_seconds = max(0, min(int(before_seconds), 300))
    after_seconds = max(0, min(int(after_seconds), 300))
    result = collector(max(1, started - before_seconds), ended + after_seconds)
    events = [dict(e, relation=('during' if started <= e['timestamp'] <= ended
                               else 'before' if e['timestamp'] < started else 'after'))
              for e in result.get('events', []) if started - before_seconds <= e.get('timestamp', 0) <= ended + after_seconds]
    return {'session_id': session.get('id', ''), 'status': result.get('status', 'unavailable'),
            'reason': result.get('reason', ''), 'events': events, 'count': len(events),
            'disclaimer': 'Temporal correlation only; these events do not establish causation.'}
