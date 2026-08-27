#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
module_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${module_root}/.." && pwd)"
image_ref=${MASI_ML_IMAGE_REF:-masi-offline-ml:module-gates}
evidence_path=${MASI_ML_BUILD_EVIDENCE:-}
temporary_root="$(mktemp -d /tmp/masi-offline-ml-image.XXXXXX)"
probe_container=""

cleanup() {
  if [[ -n "${probe_container}" ]] && command -v docker >/dev/null 2>&1; then
    docker rm -f "${probe_container}" >/dev/null 2>&1 || true
  fi
  if [[ "${temporary_root}" == /tmp/masi-offline-ml-image.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

for command in docker git sha256sum uv; do
  command -v "${command}" >/dev/null 2>&1 || { echo "missing command ${command}" >&2; exit 2; }
done

python_builder_image="$(sed -n 's/^ARG PYTHON_BUILDER_IMAGE=//p' "${module_root}/Dockerfile")"
runtime_libraries_image="$(sed -n 's/^ARG RUNTIME_LIBRARIES_IMAGE=//p' "${module_root}/Dockerfile")"
runtime_image="$(sed -n 's/^ARG RUNTIME_IMAGE=//p' "${module_root}/Dockerfile")"
[[ "${python_builder_image}" =~ @sha256:[0-9a-f]{64}$ ]] || { echo "invalid Python builder image" >&2; exit 2; }
[[ "${runtime_libraries_image}" =~ @sha256:[0-9a-f]{64}$ ]] || { echo "invalid runtime libraries image" >&2; exit 2; }
[[ "${runtime_image}" =~ @sha256:[0-9a-f]{64}$ ]] || { echo "invalid runtime base image" >&2; exit 2; }
python_builder_digest="sha256:${python_builder_image##*@sha256:}"
runtime_libraries_digest="sha256:${runtime_libraries_image##*@sha256:}"
runtime_base_digest="sha256:${runtime_image##*@sha256:}"
expected_zlib_digest="sha256:cd0d4cbe528e1d83875605c2aa10f0a9ea2d34a055d24db96642e6f78ad7206d"
expected_libffi_digest="sha256:baae077ca48493f45210ce490bb281403b1d68362adef95495f5e0bc28e70eaa"

source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
source_tree_digest="sha256:$(${MASI_ML_PYTHON312:-/root/.local/bin/python3.12} \
  "${script_dir}/source-tree-digest.py" --repo "${repo_root}")"
if [[ -n "${MASI_ML_WORKING_TREE_STATUS_DIGEST:-}" ]]; then
  status_digest="${MASI_ML_WORKING_TREE_STATUS_DIGEST}"
  working_tree_dirty="${MASI_ML_WORKING_TREE_DIRTY:-true}"
else
  status_digest="sha256:$(git -C "${repo_root}" status --porcelain=v1 --untracked-files=all | sha256sum | awk '{print $1}')"
  working_tree_dirty=false
  [[ -z "$(git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)" ]] || working_tree_dirty=true
fi

mkdir -p "${temporary_root}/wheelhouse"
(
  cd -- "${module_root}"
  "${script_dir}/prepare-wheelhouse.sh" "${temporary_root}/wheelhouse" >/dev/null
)
wheelhouse_digest="sha256:$({
  find "${temporary_root}/wheelhouse" -maxdepth 1 -type f -name '*.whl' -printf '%f\n' \
    | LC_ALL=C sort \
    | while IFS= read -r wheel; do
        printf '%s  %s\n' "$(sha256sum "${temporary_root}/wheelhouse/${wheel}" | awk '{print $1}')" "${wheel}"
      done
} | sha256sum | awk '{print $1}')"

docker build \
  --platform linux/amd64 \
  --network=none \
  --provenance=false \
  --sbom=false \
  --build-context "wheelhouse=${temporary_root}/wheelhouse" \
  --build-arg "SOURCE_REVISION=${source_revision}" \
  --build-arg "SOURCE_TREE_DIGEST=${source_tree_digest}" \
  --tag "${image_ref}" \
  --file "${module_root}/Dockerfile" \
  "${repo_root}"

image_id="$(docker image inspect --format '{{.Id}}' "${image_ref}")"
label_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.source-tree.digest"}}' "${image_ref}")"
label_revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "${image_ref}")"
label_python_builder_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.python-builder.digest"}}' "${image_ref}")"
label_runtime_libraries_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-libraries.image.digest"}}' "${image_ref}")"
label_zlib_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-library.zlib.digest"}}' "${image_ref}")"
label_libffi_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-library.libffi.digest"}}' "${image_ref}")"
label_runtime_base_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-base.digest"}}' "${image_ref}")"
label_runtime_fallback="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-base.python.fallback"}}' "${image_ref}")"
image_user="$(docker image inspect --format '{{.Config.User}}' "${image_ref}")"
[[ "${label_digest}" == "${source_tree_digest}" ]] || { echo "image source digest label mismatch" >&2; exit 1; }
[[ "${label_revision}" == "${source_revision}" ]] || { echo "image revision label mismatch" >&2; exit 1; }
[[ "${label_python_builder_digest}" == "${python_builder_digest}" ]] || { echo "image Python builder label mismatch" >&2; exit 1; }
[[ "${label_runtime_libraries_digest}" == "${runtime_libraries_digest}" ]] || { echo "image runtime libraries label mismatch" >&2; exit 1; }
[[ "${label_zlib_digest}" == "${expected_zlib_digest}" ]] || { echo "image zlib label mismatch" >&2; exit 1; }
[[ "${label_libffi_digest}" == "${expected_libffi_digest}" ]] || { echo "image libffi label mismatch" >&2; exit 1; }
[[ "${label_runtime_base_digest}" == "${runtime_base_digest}" ]] || { echo "image runtime base label mismatch" >&2; exit 1; }
[[ "${label_runtime_fallback}" == "false" ]] || { echo "runtime base interpreter fallback must be disabled" >&2; exit 1; }
[[ "${image_user}" == "65532:65532" ]] || { echo "image must be non-root" >&2; exit 1; }

mkdir -p "${temporary_root}/runtime-libraries"
probe_container="$(docker create "${image_ref}")"
docker cp "${probe_container}:/usr/lib/libz.so.1" "${temporary_root}/runtime-libraries/libz.so.1"
docker cp "${probe_container}:/usr/lib/libz.so.1.3.2" "${temporary_root}/runtime-libraries/libz.so.1.3.2"
docker cp "${probe_container}:/usr/lib/libffi.so.8" "${temporary_root}/runtime-libraries/libffi.so.8"
docker cp "${probe_container}:/usr/lib/libffi.so.8.5.0" "${temporary_root}/runtime-libraries/libffi.so.8.5.0"
docker rm "${probe_container}" >/dev/null
probe_container=""
zlib_digest="sha256:$(sha256sum "${temporary_root}/runtime-libraries/libz.so.1" | awk '{print $1}')"
libffi_digest="sha256:$(sha256sum "${temporary_root}/runtime-libraries/libffi.so.8" | awk '{print $1}')"
[[ "${zlib_digest}" == "${expected_zlib_digest}" ]] || { echo "runtime zlib digest mismatch" >&2; exit 1; }
[[ "sha256:$(sha256sum "${temporary_root}/runtime-libraries/libz.so.1.3.2" | awk '{print $1}')" == "${expected_zlib_digest}" ]] || { echo "versioned zlib digest mismatch" >&2; exit 1; }
[[ "${libffi_digest}" == "${expected_libffi_digest}" ]] || { echo "runtime libffi digest mismatch" >&2; exit 1; }
[[ "sha256:$(sha256sum "${temporary_root}/runtime-libraries/libffi.so.8.5.0" | awk '{print $1}')" == "${expected_libffi_digest}" ]] || { echo "versioned libffi digest mismatch" >&2; exit 1; }

lock_digest="sha256:$(sha256sum "${module_root}/uv.lock" | awk '{print $1}')"
evidence="$(SOURCE_REVISION="${source_revision}" SOURCE_TREE_DIGEST="${source_tree_digest}" \
  STATUS_DIGEST="${status_digest}" WORKING_TREE_DIRTY="${working_tree_dirty}" IMAGE_REF="${image_ref}" \
  IMAGE_ID="${image_id}" IMAGE_USER="${image_user}" WHEELHOUSE_DIGEST="${wheelhouse_digest}" \
  LOCK_DIGEST="${lock_digest}" PYTHON_BUILDER_DIGEST="${python_builder_digest}" \
  RUNTIME_LIBRARIES_DIGEST="${runtime_libraries_digest}" RUNTIME_BASE_DIGEST="${runtime_base_digest}" \
  ZLIB_DIGEST="${zlib_digest}" LIBFFI_DIGEST="${libffi_digest}" \
  "${MASI_ML_PYTHON312:-/root/.local/bin/python3.12}" - <<'PY'
import json
import os

print(json.dumps({
    "schema_version": "offline-ml-image-build-evidence/v1",
    "module_id": "MOD-ML-001",
    "level": "MODULE",
    "applicability": "APPLICABLE",
    "result": "PASS",
    "qualification": "NOT_QUALIFIED",
    "source_revision": os.environ["SOURCE_REVISION"],
    "source_tree_digest": os.environ["SOURCE_TREE_DIGEST"],
    "working_tree_status_digest": os.environ["STATUS_DIGEST"],
    "working_tree_dirty": os.environ["WORKING_TREE_DIRTY"] == "true",
    "image_ref": os.environ["IMAGE_REF"],
    "image_id": os.environ["IMAGE_ID"],
    "image_user": os.environ["IMAGE_USER"],
    "python_builder_image_digest": os.environ["PYTHON_BUILDER_DIGEST"],
    "runtime_libraries_image_digest": os.environ["RUNTIME_LIBRARIES_DIGEST"],
    "runtime_base_image_digest": os.environ["RUNTIME_BASE_DIGEST"],
    "runtime_library_digests": {
        "zlib": os.environ["ZLIB_DIGEST"],
        "libffi": os.environ["LIBFFI_DIGEST"],
    },
    "wheelhouse_digest": os.environ["WHEELHOUSE_DIGEST"],
    "lock_digest": os.environ["LOCK_DIGEST"],
    "offline_image_build": True,
    "runtime_download": False,
}, sort_keys=True, indent=2))
PY
)"
if [[ -n "${evidence_path}" ]]; then
  [[ ! -e "${evidence_path}" && ! -L "${evidence_path}" ]] || { echo "evidence path exists" >&2; exit 2; }
  mkdir -p "$(dirname -- "${evidence_path}")"
  printf '%s\n' "${evidence}" >"${evidence_path}"
fi
printf '%s\n' "${evidence}"
