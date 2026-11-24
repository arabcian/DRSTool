#!/usr/bin/env bash
# Runs on any Python>=3.12 installation with PyYAML and ruamel.yaml.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
python -m compileall -q drstool_core drstool_gui tests
python -m unittest discover -s tests -p test_audit_regressions.py -v
python -m unittest discover -s tests -p test_core.py -v
python -m unittest discover -s tests -p test_crash_triage.py -v
python -m unittest discover -s tests -p test_bisect_search.py -v
python -m unittest discover -s tests -p test_xid_analysis.py -v
python -m unittest discover -s tests -p test_new_features.py -v
python -m unittest discover -s tests -p test_integration_plan.py -v
python -m unittest discover -s tests -p test_perf_regressions.py -v
python -m unittest discover -s tests -p test_launch_advisors.py -v
python -m unittest discover -s tests -p test_incident_recovery.py -v
python -m unittest discover -s tests -p test_maintenance.py -v
bash lutris-game-tune-main/tests/run-tests.sh
bash lutris-game-tune-main/tests/wrapper-tests.sh   # needs root; skips otherwise
gcc -Wall -Wextra -Werror -fsyntax-only lutris-game-tune-main/lutris-game-tune-wrapper.c
