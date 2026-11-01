"""Checks for free-text input fields (Qt-free).

Every checker takes the field text and returns an error message, or "" when the text
is acceptable. Empty text is always acceptable: it means "not set".
"""

import re
from pathlib import Path

_HEX_DIGITS = re.compile(r"[0-9a-fA-F]+")


def check_int(text: str, lo: int = 0, hi: int = 2_000_000_000) -> str:
    t = text.strip()
    if not t:
        return ""
    if not re.fullmatch(r"-?\d+", t):
        return "Enter a whole number (digits only)"
    if not lo <= int(t) <= hi:
        return f"Must be between {lo:,} and {hi:,}"
    return ""


def check_hex(text: str, max_digits: int = 8) -> str:
    t = text.strip()
    if t[:2].lower() == "0x":
        t = t[2:]
    if not t:
        return ""
    if not _HEX_DIGITS.fullmatch(t):
        return "Only hex digits 0-9 and A-F; other characters are ignored"
    if len(t) > max_digits:
        return f"At most {max_digits} hex digits (32-bit value)"
    return ""


def check_dir(text: str) -> str:
    t = text.strip()
    if not t:
        return ""
    try:
        p = Path(t).expanduser()
    except (RuntimeError, ValueError):
        return "Not a valid path"
    if not p.exists():
        return "This folder does not exist"
    if not p.is_dir():
        return "This is a file, not a folder"
    return ""
