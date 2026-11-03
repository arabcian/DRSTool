#!/usr/bin/env python3
"""Read-only offline system report. With --check-transactions, exit 2 on unresolved journals."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drstool_core.diagnostics import gather  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-transactions', action='store_true',
                        help='exit 2 if unresolved or unreadable transaction journals were found')
    args = parser.parse_args()
    result = gather()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.check_transactions and result['transactions_need_attention']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
