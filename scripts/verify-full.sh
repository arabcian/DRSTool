#!/usr/bin/env bash
# Reproduce all available checks on a Gentoo/other Linux build machine.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
echo '== Python syntax and unit tests =='
python -m compileall -q drstool_core drstool_gui tests
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
echo '== Tuning regression tests =='
bash lutris-game-tune-main/tests/run-tests.sh
echo '== Native smoke build =='
gcc -Wall -Wextra -Werror -fsyntax-only lutris-game-tune-main/lutris-game-tune-wrapper.c
echo '== Dependency check =='
python - <<'PY'
import importlib.util
import shutil
missing = []
for name in ('PySide6', 'yaml', 'ruamel'):
    ok = importlib.util.find_spec(name) is not None
    print(f'{name}: {"available" if ok else "MISSING"}')
    if not ok: missing.append(name)
for name in ('cmake', 'vulkaninfo'):
    print(f'{name}: {"available" if shutil.which(name) else "unavailable"}')
if missing:
    raise SystemExit('Missing Python GUI/round-trip dependencies: ' + ', '.join(missing))
PY
if [[ ! -f /usr/include/vulkan/vulkan.h && ! -f /usr/local/include/vulkan/vulkan.h ]]; then
    echo 'Vulkan SDK headers missing; full verification CANNOT pass' >&2
    exit 2
fi
bash scripts/test-flm-sanitizers.sh
echo 'Full build checks passed. GPU runtime validation is still a separate step.'
