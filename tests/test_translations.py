"""Every literal passed to tr() in the GUI has a Turkish catalog entry. No Qt required."""
import ast
from pathlib import Path

from drstool_core.i18n import TR

ROOT = Path(__file__).resolve().parents[1]


def test_every_gui_tr_literal_is_in_the_catalog():
    missing = []
    for path in sorted((ROOT / "drstool_gui").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "tr" and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                    and node.args[0].value not in TR):
                missing.append(f"{path.name}:{node.lineno} {node.args[0].value[:60]!r}")
    assert missing == []
