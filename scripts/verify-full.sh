#!/usr/bin/env bash
# Reproduce all available checks on a Gentoo/other Linux build machine.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
echo '== Qt-free reproducible checks =='
bash scripts/verify-core.sh
echo '== Qt-enabled full GUI suite =='
python - <<'PYCODE'
import importlib.util
if importlib.util.find_spec('PySide6') is None:
    raise SystemExit('PySide6 unavailable: GUI tests cannot run; install the GUI dependency')
PYCODE
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
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
