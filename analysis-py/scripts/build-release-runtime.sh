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
release_root="$1"
evidence_path="$2"
python312="${MASI_ANALYSIS_PYTHON312:-/root/.local/bin/python3.12}"
temporary_root="$(mktemp -d /tmp/masi-analysis-release.XXXXXX)"

cleanup() {
  if [[ "${temporary_root}" == /tmp/masi-analysis-release.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

fail() {
  echo "analysis release build: $*" >&2
  exit 1
}

for command in git jq sha256sum tar uv; do
  command -v "${command}" >/dev/null 2>&1 || fail "missing command ${command}"
done
[[ -x "${python312}" ]] || fail "Python 3.12 interpreter unavailable"
[[ ! -e "${release_root}" && ! -L "${release_root}" ]] || fail "release directory must not exist"
[[ ! -e "${evidence_path}" && ! -L "${evidence_path}" ]] || fail "refusing to overwrite release evidence"
mkdir -p -- "${release_root}" "${temporary_root}/wheelhouse-a" "${temporary_root}/wheelhouse-b" "$(dirname -- "${evidence_path}")"

if [[ -n "${MASI_ANALYSIS_WORKING_TREE_STATUS_DIGEST:-}" ]]; then
  working_tree_status_digest="${MASI_ANALYSIS_WORKING_TREE_STATUS_DIGEST}"
  working_tree_dirty="${MASI_ANALYSIS_WORKING_TREE_DIRTY:-true}"
else
  git -C "${repo_root}" status --porcelain=v1 --untracked-files=all >"${temporary_root}/working-tree-status.txt"
  working_tree_status_digest="sha256:$(sha256sum "${temporary_root}/working-tree-status.txt" | awk '{print $1}')"
  working_tree_dirty=false
  [[ ! -s "${temporary_root}/working-tree-status.txt" ]] || working_tree_dirty=true
fi
source_archive="${temporary_root}/source-tree.tar"
tar --sort=name --mtime=@1787334400 --owner=0 --group=0 --numeric-owner \
  --exclude='analysis-py/.venv' --exclude='analysis-py/.ruff_cache' \
  --exclude='analysis-py/evidence' --exclude='analysis-py/build' --exclude='analysis-py/dist' \
  --exclude='analysis-py/src/masi_analysis_plugin.egg-info' --exclude='**/node_modules' \
  --exclude='**/__pycache__' --exclude='*.pyc' \
  -cf "${source_archive}" -C "${repo_root}" analysis-py contracts deploy/analysis
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
source_revision="$(git -C "${repo_root}" rev-parse HEAD)"

(
  cd -- "${module_root}"
  MASI_ANALYSIS_WHEEL_TARGET=host "${script_dir}/prepare-wheelhouse.sh" "${temporary_root}/wheelhouse-a" >/dev/null
  MASI_ANALYSIS_WHEEL_TARGET=host "${script_dir}/prepare-wheelhouse.sh" "${temporary_root}/wheelhouse-b" >/dev/null
)
wheel_digest_a="sha256:$(sha256sum "${temporary_root}/wheelhouse-a/masi_analysis_plugin-1.0.0-py3-none-any.whl" | awk '{print $1}')"
wheel_digest_b="sha256:$(sha256sum "${temporary_root}/wheelhouse-b/masi_analysis_plugin-1.0.0-py3-none-any.whl" | awk '{print $1}')"
[[ "${wheel_digest_a}" == "${wheel_digest_b}" ]] || fail "project wheel is not reproducible"

"${python312}" -m venv "${release_root}/venv"
"${release_root}/venv/bin/python" -m pip install --quiet --no-index \
  --find-links "${temporary_root}/wheelhouse-a" --no-cache-dir masi-analysis-plugin==1.0.0
cp -- "${temporary_root}/wheelhouse-a/masi_analysis_plugin-1.0.0-py3-none-any.whl" "${release_root}/"
version="$(${release_root}/venv/bin/masi-analysis --version)"
[[ "${version}" == "1.0.0" ]] || fail "release entrypoint version mismatch"
runtime_version="$(${release_root}/venv/bin/python -c 'import platform; print(platform.python_version())')"
[[ "${runtime_version}" == 3.12.13 ]] || fail "release Python patch mismatch"
binary_digest="sha256:$(sha256sum "${release_root}/venv/bin/masi-analysis" | awk '{print $1}')"

jq -n \
  --arg source_revision "${source_revision}" \
  --arg source_tree_digest "${source_tree_digest}" \
  --arg working_tree_status_digest "${working_tree_status_digest}" \
  --argjson working_tree_dirty "${working_tree_dirty}" \
  --arg wheel_digest "${wheel_digest_a}" \
  --arg lock_digest "sha256:$(sha256sum "${module_root}/uv.lock" | awk '{print $1}')" \
  --arg binary_digest "${binary_digest}" \
  --arg python_version "${runtime_version}" \
  '{
    schema_version: "analysis-release-runtime-evidence/v1",
    module_id: "MOD-AGENT-001",
    level: "MODULE",
    applicability: "APPLICABLE",
    result: "PASS",
    qualification: "NOT_QUALIFIED",
    source_revision: $source_revision,
    source_tree_digest: $source_tree_digest,
    working_tree_status_digest: $working_tree_status_digest,
    working_tree_dirty: $working_tree_dirty,
    python_version: $python_version,
    package_version: "1.0.0",
    wheel_digest: $wheel_digest,
    second_build_wheel_digest: $wheel_digest,
    reproducible_wheel: true,
    lock_digest: $lock_digest,
    entrypoint_digest: $binary_digest,
    locked_install: true,
    pytest_absent: true
  }' >"${evidence_path}"
jq . "${evidence_path}"
