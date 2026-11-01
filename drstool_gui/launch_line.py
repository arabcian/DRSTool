"""Builds the runnable launch line from a payload."""

import shlex
from typing import Dict, List

from .gamescope import GamescopeFlagsWidget


# ============================================================================
# Runnable launch line
# ============================================================================

def build_launch_line(payload: dict, command: str = "%command%", use_env: bool = True) -> str:
    """`env K=V … gamescope … -- [mangohud --dlsym] <command>` from a launch payload."""
    parts: List[str] = []
    env = payload.get("env") or {}
    if env:
        kv = [f"{k}={shlex.quote(str(v))}" for k, v in env.items()]
        parts.append(("env " if use_env else "") + " ".join(kv))
    gs = payload.get("gs_cmd") or ""
    if gs:
        parts.append(gs)
    parts.append(command)
    return " ".join(parts)


def gamescope_arg_string(flags: Dict[str, str]) -> str:
    """Flag part of the gamescope command (no program name, no '--', no mangohud)."""
    toks = []
    for flag, val in flags.items():
        if flag == GamescopeFlagsWidget.MANGOHUD_KEY:
            continue
        toks.append(flag if val == "" else f"{flag} {shlex.quote(val)}")
    return " ".join(toks)
