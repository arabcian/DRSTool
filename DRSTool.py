#!/usr/bin/env python3
"""DRSTool — dxvk-nvapi / NVIDIA DRS profile editor with Proton, DXVK, VKD3D and gamescope tooling.

Entry script. The GUI lives in the ``drstool_gui`` package (``drstool_gui.app.main``), the
Qt-free helpers and the catalog data in ``drstool_core``.
"""
import sys
from pathlib import Path

# git checkout: drstool_gui / drstool_core sit next to this file
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from drstool_gui.app import main  # noqa: E402

if __name__ == "__main__":
    main()
