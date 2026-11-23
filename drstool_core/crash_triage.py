"""Crash triage: match a game / Proton log and kernel log lines against known signatures.

Rules live in data/crash_signatures.json. Each line counts for the first rule that matches.
kind "fatal" means the run crashed; kind "noise" marks errors also seen in runs that did not.
A rule may name "suspects" ("env:NAME", "vkd3d:flag", "settings:0xID") that the bisect tests first.

Besides signatures the log yields a fingerprint of the run (VKD3D_CONFIG as vkd3d-proton saw it,
swapchain size, frame latency, exit code, duration, …) so a crashed and a clean run can be
compared line by line.
"""
from __future__ import annotations

import json
import re
from collections import deque
from datetime import datetime
from typing import Iterable

from .catalog import DATA_DIR

RULES = [dict(r, rx=re.compile(r["regex"]), suspects=r.get("suspects", []))
         for r in json.loads((DATA_DIR / "crash_signatures.json").read_text(encoding="utf-8"))]

_XID_CODE = re.compile(r"\bXid\b\s*(?:\([^)]*\))?\s*:\s*(\d{1,4})\b")
_XID_FIELDS = {name: re.compile(rf"\b{rx}\s+(?:0x)?([0-9a-fA-F]+)\b")
               for name, rx in (("channel", "ChI[dD]"), ("class", "Class"),
                                ("offset", "Offset"), ("error_code", "ErrorCode"))}

# fingerprint key -> regex with one group; the last match in the log wins
_FINGERPRINT = {
    "vkd3d_config": re.compile(r"vkd3d_config_flags_init_once: VKD3D_CONFIG='([^']*)'"),
    "dxvk_version": re.compile(r"\bDXVK: (v\S+)"),
    "game_version": re.compile(r"\bGameVersion: ([\w.]+)"),
    "runner": re.compile(r"/((?:wine|proton|GE-Proton|Proton)[^/\s]*)/(?:bin|files)/", re.I),
    "swapchain_images": re.compile(r"Got (\d+) swapchain images"),
    "swapchain_buffers": re.compile(r"Creating swapchain \(\d+ x \d+\), BufferCount = (\d+)"),
    "max_latency_frames": re.compile(r"maximum latency of (\d+) frames"),
    "exit_code": re.compile(r"return code:? (-?\d+)"),
}
_GAME_TS = re.compile(r"^\((\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\)")
CONTEXT_LINES = 8


def parse_xid(line: str) -> dict | None:
    """Xid number and, when present, ChId / Class / Offset / ErrorCode as 0x… strings."""
    m = _XID_CODE.search(line)
    if not m:
        return None
    out = {"xid": int(m.group(1))}
    for name, rx in _XID_FIELDS.items():
        f = rx.search(line, m.end())
        if f:
            out[name] = hex(int(f.group(1), 16))
    return out


def _ts(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S") if text else None
    except ValueError:
        return None


def triage(lines: Iterable[str]) -> dict:
    """-> {"crashed", "verdict", "findings", "xid", "fingerprint", "context", "suspects",
    "started", "ended"}; times are naive local datetimes from the game's own log lines."""
    found: dict = {}
    xids: list = []
    fp: dict = {}
    recent: deque = deque(maxlen=CONTEXT_LINES)
    context: list = []
    first = last = None
    for n, line in enumerate(lines, 1):
        if line.startswith("("):
            m = _GAME_TS.match(line)
            if m:
                first, last = first or m.group(1), m.group(1)
        rule = next((r for r in RULES if r["rx"].search(line)), None)
        if not rule or rule["kind"] != "noise":
            for key, rx in _FINGERPRINT.items():
                m = rx.search(line)
                if m:
                    fp[key] = m.group(1)
        if rule:
            f = found.setdefault(rule["id"], {k: rule[k] for k in ("id", "kind", "title", "hint", "suspects")}
                                 | {"count": 0, "first_line": n, "sample": line.strip()[:300]})
            f["count"] += 1
            x = parse_xid(line) if rule["id"].startswith("xid") else None
            if x and x not in xids:
                xids.append(x)
            if rule["kind"] == "fatal" and not context:
                context = list(recent) + [(n, line.strip()[:300])]
            if rule["kind"] == "noise":
                continue
        if line.strip():
            recent.append((n, line.strip()[:300]))
    first, last = _ts(first), _ts(last)
    if first and last:
        fp["duration_s"] = str(int((last - first).total_seconds()))
    findings = sorted(found.values(), key=lambda f: (f["kind"] != "fatal", f["first_line"]))
    crashed = any(f["kind"] == "fatal" for f in findings)
    suspects = list(dict.fromkeys(s for f in findings if f["kind"] == "fatal" for s in f["suspects"]))
    return {"crashed": crashed, "verdict": _verdict(crashed, findings, fp), "findings": findings,
            "xid": xids, "fingerprint": fp, "context": context, "suspects": suspects,
            "started": first, "ended": last}


def _verdict(crashed: bool, findings: list, fp: dict) -> str:
    if crashed:
        return "Crashed: " + ", ".join(f["title"] for f in findings if f["kind"] == "fatal") + "."
    code = fp.get("exit_code")
    if code not in (None, "0"):
        return f"No known crash signature, but the game exited with code {code}."
    if any(f["kind"] == "noise" for f in findings):
        return "No crash signature. The errors found are also seen in runs that do not crash."
    return "No crash signature found."


def compare(crashed: dict, clean: dict) -> list:
    """(key, value in the crashed run, value in the clean run) for every fingerprint difference.
    VKD3D_CONFIG is compared flag by flag."""
    a, b = crashed["fingerprint"], clean["fingerprint"]
    rows = []
    for key in sorted(set(a) | set(b)):
        if key == "duration_s":
            continue
        if key == "vkd3d_config":
            fa = set(filter(None, a.get(key, "").replace(";", ",").split(",")))
            fb = set(filter(None, b.get(key, "").replace(";", ",").split(",")))
            rows += [(f"vkd3d:{f}", "on" if f in fa else None, "on" if f in fb else None)
                     for f in sorted(fa ^ fb)]
        elif a.get(key) != b.get(key):
            rows.append((key, a.get(key), b.get(key)))
    return rows


def kernel_window(result: dict, *, before: int = 30, after: int = 120):
    """(start, end) epoch seconds to read the kernel journal for this run, or None."""
    if not result.get("started"):
        return None
    return result["started"].timestamp() - before, result["ended"].timestamp() + after


def format_report(result: dict, *, clean: dict | None = None, markdown: bool = False) -> str:
    h = (lambda s: f"## {s}") if markdown else (lambda s: f"── {s} ──")
    code = (lambda s: f"`{s}`") if markdown else (lambda s: s)
    out = [("# Crash triage\n\n" if markdown else "") + result["verdict"]]
    if result["findings"]:
        out += ["", h("Signatures")]
        for f in result["findings"]:
            out += [f"- [{f['kind']}] {f['title']} — {f['count']}×, first at line {f['first_line']}",
                    f"  {code(f['sample'])}", f"  {f['hint']}"]
    for x in result["xid"]:
        out.append("- " + " ".join([f"Xid {x['xid']}"] + [f"{k}={v}" for k, v in x.items() if k != "xid"]))
    if result["context"]:
        out += ["", h("Before the first fatal line")]
        out += [f"    {n}: {ln}" for n, ln in result["context"]]
    if result["fingerprint"]:
        out += ["", h("Run")]
        out += [f"- {k}: {code(v)}" for k, v in sorted(result["fingerprint"].items())]
    if result["suspects"]:
        out += ["", h("Test first"), "- " + ", ".join(result["suspects"])]
    if clean is not None:
        rows = compare(result, clean)
        out += ["", h("Differences from the clean run")]
        out += [f"- {k}: {code(a or '—')} → clean {code(b or '—')}" for k, a, b in rows] or ["- none in the log"]
    return "\n".join(out)
