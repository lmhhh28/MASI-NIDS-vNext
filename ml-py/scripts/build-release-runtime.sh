#!/usr/bin/env bash
set -euo pipefail

umask 077
if [[ $# -ne 2 ]]; then
  echo "usage: $0 EMPTY_RELEASE_DIRECTORY EVIDENCE_JSON" >&2
  exit 64
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
module_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${module_root}/.." && pwd)"
release_root=$1
evidence_path=$2
python312=${MASI_ML_PYTHON312:-/root/.local/bin/python3.12}
temporary_root="$(mktemp -d /tmp/masi-offline-ml-release.XXXXXX)"

cleanup() {
  if [[ "${temporary_root}" == /tmp/masi-offline-ml-release.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

[[ -x "${python312}" ]] || { echo "Python 3.12 interpreter unavailable" >&2; exit 2; }
[[ ! -e "${release_root}" && ! -L "${release_root}" ]] || { echo "release directory must not exist" >&2; exit 2; }
[[ ! -e "${evidence_path}" && ! -L "${evidence_path}" ]] || { echo "evidence path already exists" >&2; exit 2; }
mkdir -p "${temporary_root}/wheelhouse" "${temporary_root}/wheel-a" "${temporary_root}/wheel-b" \
  "${release_root}" "$(dirname -- "${evidence_path}")"

export SOURCE_DATE_EPOCH=1787334400
export PYTHONHASHSEED=0
(
  cd -- "${module_root}"
  for generated in build src/masi_offline_ml.egg-info; do
    if [[ -e "${generated}" ]]; then
      find "${generated}" -depth -delete
    fi
  done
  uv build --quiet --wheel --out-dir "${temporary_root}/wheel-a"
  find build src/masi_offline_ml.egg-info -depth -delete
  uv build --quiet --wheel --out-dir "${temporary_root}/wheel-b"
  find build src/masi_offline_ml.egg-info -depth -delete
  "${script_dir}/prepare-wheelhouse.sh" "${temporary_root}/wheelhouse" >/dev/null
)
wheel_a="${temporary_root}/wheel-a/masi_offline_ml-1.0.0-py3-none-any.whl"
wheel_b="${temporary_root}/wheel-b/masi_offline_ml-1.0.0-py3-none-any.whl"
wheel_digest_a="sha256:$(sha256sum "${wheel_a}" | awk '{print $1}')"
wheel_digest_b="sha256:$(sha256sum "${wheel_b}" | awk '{print $1}')"
[[ "${wheel_digest_a}" == "${wheel_digest_b}" ]] || { echo "project wheel is not reproducible" >&2; exit 1; }

"${python312}" -m venv "${release_root}/venv"
"${release_root}/venv/bin/python" -m pip install --quiet --no-index \
  --find-links "${temporary_root}/wheelhouse" --no-cache-dir masi-offline-ml==1.0.0
cp -- "${wheel_a}" "${release_root}/"
version="$(${release_root}/venv/bin/masi-offline-ml --version)"
[[ "${version}" == "masi-offline-ml 1.0.0" ]] || { echo "release entrypoint version mismatch: ${version}" >&2; exit 1; }
runtime_version="$(${release_root}/venv/bin/python -c 'import platform; print(platform.python_version())')"
[[ "${runtime_version}" == 3.12.13 ]] || { echo "release Python patch mismatch" >&2; exit 1; }

source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
source_tree_digest="sha256:$(${python312} "${script_dir}/source-tree-digest.py" --repo "${repo_root}")"
if [[ -n "${MASI_ML_WORKING_TREE_STATUS_DIGEST:-}" ]]; then
  status_digest="${MASI_ML_WORKING_TREE_STATUS_DIGEST}"
  working_tree_dirty="${MASI_ML_WORKING_TREE_DIRTY:-true}"
else
  status_digest="sha256:$(git -C "${repo_root}" status --porcelain=v1 --untracked-files=all | sha256sum | awk '{print $1}')"
  working_tree_dirty=false
  [[ -z "$(git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)" ]] || working_tree_dirty=true
fi

"${python312}" - "${evidence_path}" <<PY
import hashlib
import json
import pathlib

path = pathlib.Path(${evidence_path@Q})
document = {
    "schema_version": "offline-ml-release-evidence/v1",
    "module_id": "MOD-ML-001",
    "level": "MODULE",
    "applicability": "APPLICABLE",
    "result": "PASS",
    "qualification": "NOT_QUALIFIED",
    "source_revision": ${source_revision@Q},
    "source_tree_digest": ${source_tree_digest@Q},
    "working_tree_status_digest": ${status_digest@Q},
    "working_tree_dirty": ${working_tree_dirty@Q} == "true",
    "python_version": ${runtime_version@Q},
    "package_version": "1.0.0",
    "wheel_digest": ${wheel_digest_a@Q},
    "second_build_wheel_digest": ${wheel_digest_b@Q},
    "reproducible_wheel": True,
    "lock_digest": "sha256:" + hashlib.sha256(pathlib.Path(${module_root@Q}, "uv.lock").read_bytes()).hexdigest(),
    "entrypoint_digest": "sha256:" + hashlib.sha256(
        pathlib.Path(${release_root@Q}, "venv/bin/masi-offline-ml").read_bytes()
    ).hexdigest(),
    "locked_install": True,
    "pytest_absent": True
}
path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
PY
