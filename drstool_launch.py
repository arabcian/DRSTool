#!/usr/bin/env python3
"""Thin entry point: DRSTool.py is imported as a module (not run as __main__), so Python
keeps its compiled bytecode (.pyc) instead of recompiling 25k lines on every start."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from DRSTool import main  # noqa: E402

main()
