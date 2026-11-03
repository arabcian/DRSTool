#!/usr/bin/env bash
# Build-only sanitizer smoke tests. The Vulkan layer must be exercised with
# validation-enabled test games separately to detect runtime races.
set -euo pipefail
project="$(cd -- "$(dirname -- "$0")/.." && pwd)"
for sanitizer in ASAN UBSAN TSAN; do
  dir="${TMPDIR:-/tmp}/drstool-flm-${sanitizer,,}-$$"
  trap 'rm -rf "${dir}"' EXIT
  echo "==> Checking FLM under ${sanitizer}"
  cmake -S "${project}/vk-flip-meter-main" -B "${dir}" -DCMAKE_BUILD_TYPE=Debug \
    "-DENABLE_${sanitizer}=ON" -DFLM_NATIVE_BUILD=OFF
  cmake --build "${dir}" --parallel 2
  rm -rf "${dir}"
done
echo 'Sanitizer builds successful; GPU runtime validation is still required.'
