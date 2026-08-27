#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 EMPTY_OUTPUT_DIRECTORY" >&2
  exit 2
fi

output_dir=$1
wheel_target=${MASI_ANALYSIS_WHEEL_TARGET:-host}
if [[ "${wheel_target}" != host && "${wheel_target}" != musl ]]; then
  echo "MASI_ANALYSIS_WHEEL_TARGET must be host or musl" >&2
  exit 2
fi
if [[ -e "${output_dir}" ]] && [[ -n "$(find "${output_dir}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "wheelhouse output directory must be empty: ${output_dir}" >&2
  exit 2
fi
mkdir -p "${output_dir}" "${output_dir}/project-dist"

python312=${MASI_ANALYSIS_PYTHON312:-/root/.local/bin/python3.12}
if [[ ! -x "${python312}" ]]; then
  echo "Python 3.12 interpreter unavailable: ${python312}" >&2
  exit 2
fi

export SOURCE_DATE_EPOCH=1787334400
export PYTHONHASHSEED=0

uv export --frozen --no-dev --no-emit-project --format requirements.txt \
  --output-file "${output_dir}/requirements.lock.txt" >/dev/null
pip_download=("${python312}" -m pip download --quiet --only-binary=:all: --dest "${output_dir}")
if [[ "${wheel_target}" == musl ]]; then
  pip_download+=(
    --platform musllinux_1_2_x86_64 --platform musllinux_1_1_x86_64
    --python-version 312 --implementation cp --abi cp312 --abi abi3
  )
fi
"${pip_download[@]}" --requirement "${output_dir}/requirements.lock.txt"
uv build --quiet --wheel --out-dir "${output_dir}/project-dist"
cp "${output_dir}"/project-dist/masi_analysis_plugin-1.0.0-py3-none-any.whl "${output_dir}/"
rm -rf "${output_dir}/project-dist" "${output_dir}/requirements.lock.txt" build src/masi_analysis_plugin.egg-info

find "${output_dir}" -maxdepth 1 -type f -name '*.whl' -print0 \
  | sort -z \
  | xargs -0 sha256sum
