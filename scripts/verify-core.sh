#!/usr/bin/env bash
# Runs on any Python>=3.12 installation with PyYAML and ruamel.yaml.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
python -m compileall -q drstool_core drstool_gui tests
python -m unittest discover -s tests -p test_core.py -v
python -m unittest discover -s tests -p test_integration_plan.py -v
bash lutris-game-tune-main/tests/run-tests.sh
gcc -Wall -Wextra -Werror -fsyntax-only lutris-game-tune-main/lutris-game-tune-wrapper.c
