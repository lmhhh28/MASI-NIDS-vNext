#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 EMPTY_BUILD_DIRECTORY" >&2
  exit 64
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
module_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${module_root}/.." && pwd)"
build_dir=$1
[[ ! -e "${build_dir}" && ! -L "${build_dir}" ]] || { echo "build directory exists" >&2; exit 2; }
cmake -S "${repo_root}/infer-cpp" -B "${build_dir}" \
  -DCMAKE_BUILD_TYPE=Release -DMASI_INF_BUILD_TESTS=OFF
cmake --build "${build_dir}" --target masi_repository_validator --parallel 2
[[ -x "${build_dir}/masi_repository_validator" ]]
