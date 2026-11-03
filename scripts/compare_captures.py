#!/usr/bin/env python3
"""Offline repeatable MangoHud / FLM frame benchmark (3+ captures per setting)."""
import argparse
import csv
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drstool_core.benchmark import compare_repeats


def load(path):
    with open(path, encoding='utf-8-sig', errors='replace', newline='') as f:
        lines = (x for x in f if x.strip() and not x.startswith('#'))
        for line in lines:
            columns = [c.strip().lower() for c in line.split(',')]
            if 'frametime' in columns or ('interval_ns' in columns and 'flip_ns' in columns):
                break
        else:
            raise ValueError(f'{path}: frametime / interval_ns header not found')
        key = 'frametime' if 'frametime' in columns else 'interval_ns'
        ix = columns.index(key)
        unit = 1 if key == 'frametime' else 1_000_000
        values = []
        for row in csv.reader(lines):
            try:
                v = float(row[ix]) / unit
            except (ValueError, IndexError):
                continue
            if 0 < v < 10000:
                values.append(v)
        return values


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--a', nargs='+', required=True, help='baseline captures (3 or more)')
    p.add_argument('--b', nargs='+', required=True, help='candidate captures (3 or more)')
    p.add_argument('--same-scene', action='store_true', help='confirm identical test scene and conditions')
    args = p.parse_args()
    try:
        result = compare_repeats([load(x) for x in args.a], [load(x) for x in args.b],
                                 same_scene_confirmed=args.same_scene)
    except (ValueError, OSError) as ex:
        p.error(str(ex))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
