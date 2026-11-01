#!/usr/bin/env python3
"""Thin entry point used by the installers: imports DRSTool (which starts nothing by itself)
and calls main()."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from DRSTool import main  # noqa: E402

main()
