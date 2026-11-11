#!/usr/bin/env python3
"""Repeatable, Qt/GPU-free I/O profile for the plan's Lutris + DLL scans.

Creates synthetic fixtures in the chosen filesystem (use --base-path on
Gentoo F2FS vs mounted NTFS). Does not edit any installed games.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from drstool_core.game_scan import GameEntry, clear_dll_scan_cache, find_dlls  # noqa: E402
from drstool_core.lutris_config_scan import scan_game_configs  # noqa: E402


def timed(fn, repeats=3):
    out = []
    for _ in range(repeats):
        t0 = time.perf_counter_ns()
        fn()
        out.append((time.perf_counter_ns() - t0) / 1e6)
    return {'median_ms': round(statistics.median(out), 3),
            'samples_ms': [round(v, 3) for v in out]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-path', type=Path, default=None,
                        help='temporary-fixture parent (e.g. F2FS/NTFS mount)')
    parser.add_argument('--games', nargs='+', type=int, default=[50, 250, 1000])
    parser.add_argument('--out', type=Path, help='save JSON measurements here')
    args = parser.parse_args()
    if any(n < 1 or n > 10000 for n in args.games):
        parser.error('--games must be between 1 and 10000')
    result = {'note': 'Synthetic filesystem / process timings only; not GPU/FLM evidence',
              'results': []}
    with tempfile.TemporaryDirectory(prefix='drstool-perf-',
                                     dir=str(args.base_path) if args.base_path else None) as td:
        root = Path(td)
        for count in args.games:
            games = root / f'lutris-{count}'
            games.mkdir()
            for i in range(count):
                (games / f'game-{i:05d}.yml').write_text(
                    f'game:\n  exe: /tmp/game_{i}.exe\nsystem:\n  runner: wine\n', encoding='utf-8')
            cold = timed(lambda: scan_game_configs(games, {}), repeats=3)
            _, cache = scan_game_configs(games)
            warm = timed(lambda: scan_game_configs(games, cache), repeats=3)
            result['results'].append({'task': 'lutris_yaml', 'files': count,
                                      'cold': cold, 'warm': warm})
        game_root = root / 'synthetic-game'
        for i in range(8):
            folder = game_root / f'tree-{i}' / 'nested'
            folder.mkdir(parents=True)
            for j in range(100):
                (folder / f'content-{j}.txt').write_bytes(b'data')
        game = GameEntry('Custom', 'custom:benchmark', 'Benchmark', [game_root])
        cold = timed(lambda: find_dlls(game, force_rescan=True))
        clear_dll_scan_cache(game.key)
        find_dlls(game)
        warm = timed(lambda: find_dlls(game))
        result['results'].append({'task': 'dll_scan', 'files': 800,
                                  'cold': cold, 'warm': warm})
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
