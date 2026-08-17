#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
inf_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${inf_root}/.." && pwd)"
mode="${1:-}"

mapfile -t source_files < <(
  git -C "${repo_root}" ls-files --cached --others --exclude-standard -- \
    'infer-cpp/src/*.cc' 'infer-cpp/src/*.h' 'infer-cpp/tests/*.cc' \
    'infer-cpp/tests/*.h' 'infer-cpp/tests/support/*.h' |
    sed 's#^infer-cpp/##' | LC_ALL=C sort -u
)
if [[ "${#source_files[@]}" -eq 0 ]]; then
  echo "no Central Inference C++ files found" >&2
  exit 1
fi

cd -- "${inf_root}"
case "${mode}" in
  format)
    command -v clang-format >/dev/null 2>&1 || {
      echo "clang-format is required" >&2
      exit 2
    }
    clang-format --dry-run --Werror "${source_files[@]}"
    ;;
  tidy)
    command -v clang-tidy >/dev/null 2>&1 || {
      echo "clang-tidy is required" >&2
      exit 2
    }
    MASI_INF_GRPC_PREFIX="${MASI_INF_GRPC_PREFIX:-/opt/masi-toolchain/grpc-1.82.1}" \
      cmake --preset cpu-release >/dev/null
    cmake --build build/cpu-release --target masi_inference_gateway contract_golden \
      property_invariants module_blackbox --parallel "${MASI_INF_BUILD_JOBS:-4}" >/dev/null
    mapfile -t translation_units < <(printf '%s\n' "${source_files[@]}" | awk '/\.cc$/')
    clang-tidy --warnings-as-errors='*' -p build/cpu-release \
      "${translation_units[@]}"
    ;;
  *)
    echo "usage: $0 format|tidy" >&2
    exit 64
    ;;
esac
