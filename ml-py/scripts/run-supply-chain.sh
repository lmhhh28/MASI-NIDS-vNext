#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
module_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${module_root}/.." && pwd)"
evidence_dir=${MASI_ML_SUPPLY_EVIDENCE_DIR:-${module_root}/evidence/supply-chain}
image_ref=${MASI_ML_IMAGE_REF:-masi-offline-ml:module-gates}
python_runtime=${MASI_ML_PYTHON:-${module_root}/.venv/bin/python}
trivy_cache=${MASI_TRIVY_CACHE:-${repo_root}/out/supply-chain/trivy-cache}
key_dir=${MASI_COSIGN_KEY_DIR:-${repo_root}/.masi-secrets/cosign}
temporary_root="$(mktemp -d /tmp/masi-offline-ml-supply.XXXXXX)"
probe_container=""

cleanup() {
  if [[ -n "${probe_container}" ]] && command -v docker >/dev/null 2>&1; then
    docker rm -f "${probe_container}" >/dev/null 2>&1 || true
  fi
  if [[ "${temporary_root}" == /tmp/masi-offline-ml-supply.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

fail() {
  echo "offline ml supply chain: $*" >&2
  exit 1
}

for command in docker git jq sha256sum tar; do
  command -v "${command}" >/dev/null 2>&1 || fail "missing command ${command}"
done
[[ -x "${python_runtime}" ]] || fail "Python runtime unavailable"
[[ ! -e "${evidence_dir}/supply-chain-evidence.json" ]] || fail "supply evidence already exists"
mkdir -p "${evidence_dir}" "${temporary_root}/snapshot" "${temporary_root}/bundle"
for required in \
  "${trivy_cache}/db/trivy.db" \
  "${trivy_cache}/db/metadata.json" \
  "${trivy_cache}/java-db/trivy-java.db" \
  "${trivy_cache}/java-db/metadata.json" \
  "${trivy_cache}/policy/metadata.json" \
  "${key_dir}/cosign.key" \
  "${repo_root}/contracts/trust/v1/cosign.pub"; do
  [[ -f "${required}" && ! -L "${required}" ]] || fail "required offline input missing: ${required}"
done

tools_lock="${repo_root}/deploy/supply-chain/tools.lock.json"
syft_image="$(jq -er '.tools.syft.image' "${tools_lock}")"
trivy_image="$(jq -er '.tools.trivy.image' "${tools_lock}")"
cosign_image="$(jq -er '.tools.cosign.image' "${tools_lock}")"
checks_reference="$(jq -er '.trivy_checks_bundle.reference' "${tools_lock}")"
python_builder_image="$(sed -n 's/^ARG PYTHON_BUILDER_IMAGE=//p' "${module_root}/Dockerfile")"
runtime_libraries_image="$(sed -n 's/^ARG RUNTIME_LIBRARIES_IMAGE=//p' "${module_root}/Dockerfile")"
runtime_image="$(sed -n 's/^ARG RUNTIME_IMAGE=//p' "${module_root}/Dockerfile")"
python_builder_digest="sha256:${python_builder_image##*@sha256:}"
runtime_libraries_digest="sha256:${runtime_libraries_image##*@sha256:}"
runtime_base_digest="sha256:${runtime_image##*@sha256:}"
expected_zlib_digest="sha256:cd0d4cbe528e1d83875605c2aa10f0a9ea2d34a055d24db96642e6f78ad7206d"
expected_libffi_digest="sha256:baae077ca48493f45210ce490bb281403b1d68362adef95495f5e0bc28e70eaa"

assert_repo_digest() {
  local reference=$1
  local repository="${reference%@sha256:*}"
  local digest="sha256:${reference##*@sha256:}"
  case "${repository}" in
    docker.io/library/*) repository="${repository#docker.io/library/}" ;;
    docker.io/*) repository="${repository#docker.io/}" ;;
  esac
  docker image inspect --format '{{json .RepoDigests}}' "${reference}" \
    | jq -e --arg expected "${repository}@${digest}" 'index($expected) != null' >/dev/null \
    || fail "tool/base RepoDigest mismatch: ${reference}"
}
for image in "${syft_image}" "${trivy_image}" "${cosign_image}" "${python_builder_image}" "${runtime_libraries_image}" "${runtime_image}"; do
  assert_repo_digest "${image}"
done

source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
source_tree_digest="sha256:$(${python_runtime} "${script_dir}/source-tree-digest.py" --repo "${repo_root}")"
if [[ -n "${MASI_ML_WORKING_TREE_STATUS_DIGEST:-}" ]]; then
  working_tree_status_digest="${MASI_ML_WORKING_TREE_STATUS_DIGEST}"
  working_tree_dirty="${MASI_ML_WORKING_TREE_DIRTY:-true}"
else
  git -C "${repo_root}" status --porcelain=v1 --untracked-files=all >"${temporary_root}/working-tree-status.txt"
  working_tree_status_digest="sha256:$(sha256sum "${temporary_root}/working-tree-status.txt" | awk '{print $1}')"
  working_tree_dirty=false
  [[ ! -s "${temporary_root}/working-tree-status.txt" ]] || working_tree_dirty=true
fi

source_archive="${temporary_root}/source-tree.tar"
tar --sort=name --mtime=@1787334400 --owner=0 --group=0 --numeric-owner \
  --exclude='ml-py/.venv' --exclude='ml-py/.ruff_cache' --exclude='ml-py/evidence' \
  --exclude='ml-py/build' --exclude='ml-py/dist' --exclude='ml-py/src/masi_offline_ml.egg-info' \
  --exclude='**/__pycache__' --exclude='*.pyc' \
  -cf "${source_archive}" -C "${repo_root}" \
  ml-py contracts/dataset contracts/model-explanation contracts/profiles/v1 \
  contracts/inference/v1 contracts/golden/telemetry contracts/supply-chain/v1 contracts/evidence \
  infer-cpp/src/repository_validator.cpp infer-cpp/CMakeLists.txt deploy/supply-chain deploy/offline-ml \
  docs/masi-nids-vnext-system-requirements-2026-08-09.md \
  docs/adr/0019-offline-ml-dataset-training-and-explanation-boundary.md \
  docs/design/modules/offline-ml-pipeline-design.md
tar -xf "${source_archive}" -C "${temporary_root}/snapshot"

docker image inspect "${image_ref}" >/dev/null 2>&1 || fail "release image absent: ${image_ref}"
image_id="$(docker image inspect --format '{{.Id}}' "${image_ref}")"
image_source_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.source-tree.digest"}}' "${image_ref}")"
[[ "${image_source_digest}" == "${source_tree_digest}" ]] || fail "image source label mismatch"
[[ "$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-libraries.image.digest"}}' "${image_ref}")" == "${runtime_libraries_digest}" ]] || fail "runtime libraries image label mismatch"
[[ "$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-base.digest"}}' "${image_ref}")" == "${runtime_base_digest}" ]] || fail "runtime base image label mismatch"
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
[[ "${zlib_digest}" == "${expected_zlib_digest}" ]] || fail "runtime zlib digest mismatch"
[[ "sha256:$(sha256sum "${temporary_root}/runtime-libraries/libz.so.1.3.2" | awk '{print $1}')" == "${expected_zlib_digest}" ]] || fail "versioned zlib digest mismatch"
[[ "${libffi_digest}" == "${expected_libffi_digest}" ]] || fail "runtime libffi digest mismatch"
[[ "sha256:$(sha256sum "${temporary_root}/runtime-libraries/libffi.so.8.5.0" | awk '{print $1}')" == "${expected_libffi_digest}" ]] || fail "versioned libffi digest mismatch"
docker save --output "${temporary_root}/bundle/offline-ml-image.tar" "${image_ref}"
docker save --output "${temporary_root}/bundle/runtime-libraries-image.tar" "${runtime_libraries_image}"

tool_run=(docker run --rm --user 0:0 --network none --read-only --cap-drop ALL
  --security-opt no-new-privileges:true --pids-limit 128 --memory 2g --cpus 2
  --tmpfs /tmp:rw,noexec,nosuid,size=1g
  --tmpfs /.cache:rw,noexec,nosuid,size=64m
  --tmpfs /root/.cache:rw,noexec,nosuid,size=512m)

"${tool_run[@]}" --env SYFT_CHECK_FOR_APP_UPDATE=false \
  --volume "${temporary_root}/bundle:/input:ro" --volume "${evidence_dir}:/out" \
  "${syft_image}" scan docker-archive:/input/offline-ml-image.tar -o spdx-json=/out/image.spdx.json
"${tool_run[@]}" --env SYFT_CHECK_FOR_APP_UPDATE=false \
  --volume "${temporary_root}/snapshot:/source:ro" --volume "${evidence_dir}:/out" \
  "${syft_image}" scan dir:/source -o spdx-json=/out/source.spdx.json
"${tool_run[@]}" --env SYFT_CHECK_FOR_APP_UPDATE=false \
  --volume "${temporary_root}/bundle:/input:ro" --volume "${evidence_dir}:/out" \
  "${syft_image}" scan docker-archive:/input/runtime-libraries-image.tar \
  -o spdx-json=/out/runtime-libraries-source.spdx.json

"${tool_run[@]}" \
  --volume "${trivy_cache}/db:/root/.cache/trivy/db:ro" \
  --volume "${trivy_cache}/java-db:/root/.cache/trivy/java-db:ro" \
  --volume "${temporary_root}/bundle:/input:ro" --volume "${evidence_dir}:/out" \
  "${trivy_image}" image --input /input/offline-ml-image.tar \
  --skip-db-update --skip-java-db-update --scanners vuln,secret \
  --format json --output /out/trivy-image.json
"${tool_run[@]}" \
  --volume "${trivy_cache}/db:/root/.cache/trivy/db:ro" \
  --volume "${trivy_cache}/java-db:/root/.cache/trivy/java-db:ro" \
  --volume "${temporary_root}/bundle:/input:ro" --volume "${evidence_dir}:/out" \
  "${trivy_image}" image --input /input/runtime-libraries-image.tar \
  --skip-db-update --skip-java-db-update --scanners vuln,secret \
  --format json --output /out/trivy-runtime-libraries-source.json
"${tool_run[@]}" \
  --volume "${trivy_cache}/policy:/root/.cache/trivy/policy:ro" \
  --volume "${temporary_root}/snapshot:/source:ro" --volume "${evidence_dir}:/out" \
  "${trivy_image}" config --skip-check-update --skip-version-check \
  --checks-bundle-repository "${checks_reference}" --format json \
  --output /out/trivy-config.json /source/ml-py \
  >"${evidence_dir}/trivy-config.log" 2>&1
grep -Fq "Falling back to embedded checks" "${evidence_dir}/trivy-config.log" \
  && fail "Trivy attempted embedded checks fallback"
"${tool_run[@]}" \
  --volume "${temporary_root}/snapshot:/source:ro" --volume "${evidence_dir}:/out" \
  "${trivy_image}" fs --scanners secret --skip-db-update \
  --format json --output /out/trivy-source-secret.json /source

SOURCE_REVISION="${source_revision}" SOURCE_TREE_DIGEST="${source_tree_digest}" \
IMAGE_REF="${image_ref}" IMAGE_ID="${image_id}" STATUS_DIGEST="${working_tree_status_digest}" \
WORKING_TREE_DIRTY="${working_tree_dirty}" EVIDENCE_DIR="${evidence_dir}" REPO_ROOT="${repo_root}" \
PYTHON_BUILDER_DIGEST="${python_builder_digest}" RUNTIME_LIBRARIES_DIGEST="${runtime_libraries_digest}" \
RUNTIME_BASE_DIGEST="${runtime_base_digest}" ZLIB_DIGEST="${zlib_digest}" LIBFFI_DIGEST="${libffi_digest}" \
"${python_runtime}" - <<'PY'
import hashlib
import json
import os
from pathlib import Path

root = Path(os.environ["REPO_ROOT"])
evidence = Path(os.environ["EVIDENCE_DIR"])
def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
document = {
    "schema_version": "offline-ml-release-manifest/v1",
    "module_id": "MOD-ML-001",
    "source_revision": os.environ["SOURCE_REVISION"],
    "source_tree_digest": os.environ["SOURCE_TREE_DIGEST"],
    "image_ref": os.environ["IMAGE_REF"],
    "image_id": os.environ["IMAGE_ID"],
    "working_tree_status_digest": os.environ["STATUS_DIGEST"],
    "working_tree_dirty": os.environ["WORKING_TREE_DIRTY"] == "true",
    "runtime_profile": "offline-ml-runtime/v1",
    "python_version": "3.12.13",
    "python_builder_image_digest": os.environ["PYTHON_BUILDER_DIGEST"],
    "runtime_libraries_image_digest": os.environ["RUNTIME_LIBRARIES_DIGEST"],
    "runtime_base_image_digest": os.environ["RUNTIME_BASE_DIGEST"],
    "runtime_library_digests": {
        "zlib": os.environ["ZLIB_DIGEST"],
        "libffi": os.environ["LIBFFI_DIGEST"],
    },
    "runtime_base_python_fallback": False,
    "runtime_download": False,
    "lock_digest": digest(root / "ml-py/uv.lock"),
    "component_registry_digest": digest(root / "contracts/supply-chain/v1/offline-ml-components.json"),
    "third_party_notices_digest": digest(root / "contracts/supply-chain/v1/offline-ml-third-party-notices.json"),
    "evidence_digests": {
        name: digest(evidence / name)
        for name in (
            "image.spdx.json", "source.spdx.json", "runtime-libraries-source.spdx.json",
            "trivy-image.json", "trivy-runtime-libraries-source.json",
            "trivy-source-secret.json", "trivy-config.json"
        )
    },
}
(evidence / "release-manifest.json").write_text(
    json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8"
)
PY

"${tool_run[@]}" --env COSIGN_PASSWORD= \
  --volume "${key_dir}:/keys:ro" --volume "${evidence_dir}:/supply" \
  --volume "${repo_root}/deploy/supply-chain/cosign-offline-signing-config.json:/config/signing-config.json:ro" \
  "${cosign_image}" sign-blob --yes --signing-config /config/signing-config.json \
  --bundle /supply/release-manifest.sigstore.json --key /keys/cosign.key \
  /supply/release-manifest.json >"${evidence_dir}/cosign-sign.log" 2>&1
chmod 0644 "${evidence_dir}/release-manifest.sigstore.json"

positive=false
if "${tool_run[@]}" \
  --volume "${repo_root}/contracts/trust/v1:/trust:ro" --volume "${evidence_dir}:/supply:ro" \
  "${cosign_image}" verify-blob --private-infrastructure --key /trust/cosign.pub \
  --bundle /supply/release-manifest.sigstore.json /supply/release-manifest.json \
  >"${evidence_dir}/cosign-verify.log" 2>&1; then
  positive=true
fi
[[ "${positive}" == true ]] || fail "Cosign positive verification failed"
jq '.image_id = "sha256:tampered"' "${evidence_dir}/release-manifest.json" \
  >"${evidence_dir}/release-manifest.tampered.json"
tampered_rejected=false
if ! "${tool_run[@]}" \
  --volume "${repo_root}/contracts/trust/v1:/trust:ro" --volume "${evidence_dir}:/supply:ro" \
  "${cosign_image}" verify-blob --private-infrastructure --key /trust/cosign.pub \
  --bundle /supply/release-manifest.sigstore.json /supply/release-manifest.tampered.json \
  >"${evidence_dir}/cosign-tamper-negative.log" 2>&1; then
  tampered_rejected=true
fi
wrong_rejected=false
if ! "${tool_run[@]}" \
  --volume "${repo_root}/testkit/fixtures/supply-chain:/wrong:ro" --volume "${evidence_dir}:/supply:ro" \
  "${cosign_image}" verify-blob --private-infrastructure --key /wrong/wrong-publisher-cosign.pub \
  --bundle /supply/release-manifest.sigstore.json /supply/release-manifest.json \
  >"${evidence_dir}/cosign-wrong-publisher-negative.log" 2>&1; then
  wrong_rejected=true
fi
jq -n --argjson positive "${positive}" --argjson tampered "${tampered_rejected}" \
  --argjson wrong "${wrong_rejected}" \
  '{positive:$positive,tampered_rejected:$tampered,wrong_publisher_rejected:$wrong,tools_network_none:true}' \
  >"${evidence_dir}/signature-checks.json"
rm "${evidence_dir}/release-manifest.tampered.json"

"${python_runtime}" "${script_dir}/verify-supply-chain.py" \
  --repo "${repo_root}" --supply-dir "${evidence_dir}" \
  --source-revision "${source_revision}" --source-tree-digest "${source_tree_digest}" \
  --working-tree-status-digest "${working_tree_status_digest}" \
  --image-ref "${image_ref}" --image-id "${image_id}" \
  --output "${evidence_dir}/supply-chain-evidence.json"
