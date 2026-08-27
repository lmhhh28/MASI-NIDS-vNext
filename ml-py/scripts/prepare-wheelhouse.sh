#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 EMPTY_OUTPUT_DIRECTORY" >&2
  exit 64
fi

output_dir=$1
python312=${MASI_ML_PYTHON312:-/root/.local/bin/python3.12}
if [[ ! -x "${python312}" ]]; then
  echo "Python 3.12 interpreter unavailable: ${python312}" >&2
  exit 2
fi
if [[ -e "${output_dir}" ]] && [[ -n "$(find "${output_dir}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "wheelhouse output directory must be empty: ${output_dir}" >&2
  exit 2
fi
mkdir -p "${output_dir}" "${output_dir}/project-dist"

export SOURCE_DATE_EPOCH=1787334400
export PYTHONHASHSEED=0
for generated in build src/masi_offline_ml.egg-info; do
  if [[ -e "${generated}" ]]; then
    find "${generated}" -depth -delete
  fi
done
uv export --frozen --no-dev --no-emit-project --format requirements.txt \
  --output-file "${output_dir}/requirements.lock.txt" >/dev/null
"${python312}" -m pip download --quiet --only-binary=:all: --dest "${output_dir}" \
  --requirement "${output_dir}/requirements.lock.txt"
uv build --quiet --wheel --out-dir "${output_dir}/project-dist"
cp "${output_dir}"/project-dist/masi_offline_ml-1.0.0-py3-none-any.whl "${output_dir}/"
find "${output_dir}/project-dist" -depth -delete
rm "${output_dir}/requirements.lock.txt"
find build src/masi_offline_ml.egg-info -depth -delete

find "${output_dir}" -maxdepth 1 -type f -name '*.whl' -print0 \
  | sort -z \
  | xargs -0 sha256sum
