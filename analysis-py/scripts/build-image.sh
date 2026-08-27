#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
module_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${module_root}/.." && pwd)"
image_ref="${MASI_ANALYSIS_IMAGE_REF:-masi-analysis:module-gates}"
evidence_path="${MASI_ANALYSIS_BUILD_EVIDENCE:-}"
temporary_root="$(mktemp -d /tmp/masi-analysis-build.XXXXXX)"
probe_container=""

cleanup() {
  if [[ -n "${probe_container}" ]] && command -v docker >/dev/null 2>&1; then
    docker rm -f "${probe_container}" >/dev/null 2>&1 || true
  fi
  if [[ "${temporary_root}" == /tmp/masi-analysis-build.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

fail() {
  echo "analysis image build: $*" >&2
  exit 1
}

for command in docker git jq sha256sum tar uv; do
  command -v "${command}" >/dev/null 2>&1 || fail "missing command ${command}"
done

python_builder_image="$(sed -n 's/^ARG PYTHON_BUILDER_IMAGE=//p' "${module_root}/Dockerfile")"
runtime_image="$(sed -n 's/^ARG RUNTIME_IMAGE=//p' "${module_root}/Dockerfile")"
[[ "${python_builder_image}" =~ @sha256:[0-9a-f]{64}$ ]] || fail "invalid Python builder image"
[[ "${runtime_image}" =~ @sha256:[0-9a-f]{64}$ ]] || fail "invalid runtime base image"
python_builder_digest="sha256:${python_builder_image##*@sha256:}"
runtime_base_digest="sha256:${runtime_image##*@sha256:}"
expected_libcrypto_digest="sha256:72db1b3de8b7dfbaba4c056135f408da555f9d5e137c82129478e07e769f8070"
expected_libssl_digest="sha256:9aec161fdbc82d3e4280f5084843118939f1f4acc53c98ec963de03cfe812fad"

source_archive="${temporary_root}/source-tree.tar"
if [[ -n "${MASI_ANALYSIS_WORKING_TREE_STATUS_DIGEST:-}" ]]; then
  working_tree_status_digest="${MASI_ANALYSIS_WORKING_TREE_STATUS_DIGEST}"
  working_tree_dirty="${MASI_ANALYSIS_WORKING_TREE_DIRTY:-true}"
else
  git -C "${repo_root}" status --porcelain=v1 --untracked-files=all >"${temporary_root}/working-tree-status.txt"
  working_tree_status_digest="sha256:$(sha256sum "${temporary_root}/working-tree-status.txt" | awk '{print $1}')"
  working_tree_dirty=false
  [[ ! -s "${temporary_root}/working-tree-status.txt" ]] || working_tree_dirty=true
fi
tar --sort=name --mtime=@1787334400 --owner=0 --group=0 --numeric-owner \
  --exclude='analysis-py/.venv' \
  --exclude='analysis-py/.ruff_cache' \
  --exclude='analysis-py/evidence' \
  --exclude='analysis-py/build' \
  --exclude='analysis-py/dist' \
  --exclude='analysis-py/src/masi_analysis_plugin.egg-info' \
  --exclude='**/node_modules' \
  --exclude='**/__pycache__' \
  --exclude='*.pyc' \
  -cf "${source_archive}" -C "${repo_root}" analysis-py contracts deploy/analysis
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
source_revision="$(git -C "${repo_root}" rev-parse HEAD)"

mkdir -p -- "${temporary_root}/wheelhouse"
(
  cd -- "${module_root}"
  MASI_ANALYSIS_WHEEL_TARGET=host "${script_dir}/prepare-wheelhouse.sh" "${temporary_root}/wheelhouse"
)
wheelhouse_digest="$({
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
label_runtime_base_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-base.digest"}}' "${image_ref}")"
label_libcrypto_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-library.libcrypto.digest"}}' "${image_ref}")"
label_libssl_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-library.libssl.digest"}}' "${image_ref}")"
label_runtime_fallback="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-base.python.fallback"}}' "${image_ref}")"
image_user="$(docker image inspect --format '{{.Config.User}}' "${image_ref}")"
[[ "${label_digest}" == "${source_tree_digest}" ]] || fail "image source-tree label mismatch"
[[ "${label_revision}" == "${source_revision}" ]] || fail "image revision label mismatch"
[[ "${label_python_builder_digest}" == "${python_builder_digest}" ]] || fail "image Python builder label mismatch"
[[ "${label_runtime_base_digest}" == "${runtime_base_digest}" ]] || fail "image runtime base label mismatch"
[[ "${label_libcrypto_digest}" == "${expected_libcrypto_digest}" ]] || fail "image libcrypto label mismatch"
[[ "${label_libssl_digest}" == "${expected_libssl_digest}" ]] || fail "image libssl label mismatch"
[[ "${label_runtime_fallback}" == "false" ]] || fail "runtime base interpreter fallback must be disabled"
[[ "${image_user}" == "65532:65532" ]] || fail "image must use the non-root runtime identity"

mkdir -p -- "${temporary_root}/runtime-libraries"
probe_container="$(docker create "${image_ref}")"
docker cp "${probe_container}:/opt/masi/runtime-libs/libcrypto.so.3" "${temporary_root}/runtime-libraries/libcrypto.so.3"
docker cp "${probe_container}:/opt/masi/runtime-libs/libssl.so.3" "${temporary_root}/runtime-libraries/libssl.so.3"
docker rm "${probe_container}" >/dev/null
probe_container=""
libcrypto_digest="sha256:$(sha256sum "${temporary_root}/runtime-libraries/libcrypto.so.3" | awk '{print $1}')"
libssl_digest="sha256:$(sha256sum "${temporary_root}/runtime-libraries/libssl.so.3" | awk '{print $1}')"
[[ "${libcrypto_digest}" == "${expected_libcrypto_digest}" ]] || fail "runtime libcrypto digest mismatch"
[[ "${libssl_digest}" == "${expected_libssl_digest}" ]] || fail "runtime libssl digest mismatch"

runtime_versions="$(docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 32 --memory 256m --cpus 1 \
  --entrypoint /usr/local/bin/python3.12 "${image_ref}" -c \
  'import json, platform, sqlite3, ssl, zlib; print(json.dumps({"python": platform.python_version(), "openssl": ssl.OPENSSL_VERSION, "sqlite": sqlite3.sqlite_version, "zlib": zlib.ZLIB_RUNTIME_VERSION}, sort_keys=True))')"
jq -e '
  .python == "3.12.13"
  and .openssl == "OpenSSL 3.0.20 7 Apr 2026"
  and .sqlite == "3.53.4"
  and .zlib == "1.3.2"
' <<<"${runtime_versions}" >/dev/null || fail "effective runtime version probe mismatch"

evidence="$(jq -n \
  --arg source_revision "${source_revision}" \
  --arg source_tree_digest "${source_tree_digest}" \
  --arg image_ref "${image_ref}" \
  --arg image_id "${image_id}" \
  --arg image_user "${image_user}" \
  --arg python_builder_image_digest "${python_builder_digest}" \
  --arg runtime_base_image_digest "${runtime_base_digest}" \
  --arg libcrypto_digest "${libcrypto_digest}" \
  --arg libssl_digest "${libssl_digest}" \
  --argjson runtime_versions "${runtime_versions}" \
  --arg wheelhouse_digest "sha256:${wheelhouse_digest}" \
  --arg lock_digest "sha256:$(sha256sum "${module_root}/uv.lock" | awk '{print $1}')" \
  --arg working_tree_status_digest "${working_tree_status_digest}" \
  --argjson working_tree_dirty "${working_tree_dirty}" \
  '{
    schema_version: "analysis-release-build-evidence/v1",
    module_id: "MOD-AGENT-001",
    level: "MODULE",
    applicability: "APPLICABLE",
    result: "PASS",
    qualification: "NOT_QUALIFIED",
    source_revision: $source_revision,
    source_tree_digest: $source_tree_digest,
    working_tree_status_digest: $working_tree_status_digest,
    working_tree_dirty: $working_tree_dirty,
    image_ref: $image_ref,
    image_id: $image_id,
    image_user: $image_user,
    python_builder_image_digest: $python_builder_image_digest,
    runtime_base_image_digest: $runtime_base_image_digest,
    runtime_library_digests: {
      libcrypto: $libcrypto_digest,
      libssl: $libssl_digest
    },
    runtime_versions: $runtime_versions,
    wheelhouse_digest: $wheelhouse_digest,
    lock_digest: $lock_digest,
    offline_image_build: true,
    runtime_download: false
  }')"
if [[ -n "${evidence_path}" ]]; then
  [[ ! -e "${evidence_path}" && ! -L "${evidence_path}" ]] || fail "refusing to overwrite build evidence"
  mkdir -p -- "$(dirname -- "${evidence_path}")"
  printf '%s\n' "${evidence}" >"${evidence_path}"
fi
printf '%s\n' "${evidence}"
