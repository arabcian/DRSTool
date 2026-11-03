#!/usr/bin/env python3
"""Audit statically discoverable GUI labels against the Turkish exact-match catalog.

This is an inventory, not a claim that every visible GUI string can be found by
AST analysis: f-strings, data-driven strings and nested dialogs need manual review.
"""
from __future__ import annotations
import argparse
import ast
import json
from collections import defaultdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drstool_core.i18n import TR  # noqa: E402

CALLS = {'QLabel','QPushButton','QCheckBox','QGroupBox','_tool_btn','_title_label','_desc_label',
         'setText','setWindowTitle','setToolTip','setPlaceholderText','setTitle','addAction',
         'addTab','addItem','setHorizontalHeaderLabels','setStatusTip'}


def audit(root: Path) -> dict:
    strings: dict[str,set[str]] = defaultdict(set)
    for p in sorted(root.glob('*.py')):
        tree = ast.parse(p.read_text(encoding='utf-8'), filename=str(p))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ''
            if name not in CALLS:
                continue
            candidates = node.args[:2 if name in ('addTab','addItem') else 1]
            for arg in candidates:
                elements = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
                for element in elements:
                    if isinstance(element, ast.Constant) and isinstance(element.value,str) and element.value.strip():
                        strings[element.value].add(p.name)
    translated = sorted(s for s in strings if s in TR)
    pending = {s: sorted(strings[s]) for s in sorted(strings) if s not in TR}
    return {'static_labels': len(strings), 'catalogued_static_labels': len(translated),
            'not_catalogued': len(pending), 'untranslated': pending,
            'note': 'Dynamic labels, strings composed with f-strings and Qt tooltips may not be covered.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--json', action='store_true', help='include source files and untranslated labels in JSON')
    args = p.parse_args()
    result = audit(Path(__file__).resolve().parents[1] / 'drstool_gui')
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for k in ('static_labels','catalogued_static_labels','not_catalogued','note'):
            print(f'{k}: {result[k]}')
        for line in list(result['untranslated'])[:35]:
            print('  -', line[:120])

if __name__ == '__main__':
    main()
