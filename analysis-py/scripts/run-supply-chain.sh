#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
module_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${module_root}/.." && pwd)"
evidence_dir="${MASI_ANALYSIS_SUPPLY_EVIDENCE_DIR:-${module_root}/evidence/supply-chain}"
image_ref="${MASI_ANALYSIS_IMAGE_REF:-masi-analysis:module-gates}"
python_runtime="${MASI_ANALYSIS_PYTHON:-${module_root}/.venv/bin/python}"
trivy_cache="${MASI_TRIVY_CACHE:-${repo_root}/out/supply-chain/trivy-cache}"
key_dir="${MASI_COSIGN_KEY_DIR:-${repo_root}/.masi-secrets/cosign}"
temporary_root="$(mktemp -d /tmp/masi-analysis-supply.XXXXXX)"
probe_container=""

cleanup() {
  if [[ -n "${probe_container}" ]] && command -v docker >/dev/null 2>&1; then
    docker rm -f "${probe_container}" >/dev/null 2>&1 || true
  fi
  if [[ "${temporary_root}" == /tmp/masi-analysis-supply.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

fail() {
  echo "analysis supply chain: $*" >&2
  exit 1
}

for command in docker git jq sha256sum tar; do
  command -v "${command}" >/dev/null 2>&1 || fail "missing command ${command}"
done
[[ -x "${python_runtime}" ]] || fail "Analysis Python runtime unavailable: ${python_runtime}"
mkdir -p -- "${evidence_dir}" "${temporary_root}/snapshot" "${temporary_root}/bundle"
[[ ! -e "${evidence_dir}/supply-chain-evidence.json" ]] || fail "supply-chain evidence already exists"
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
runtime_image="$(sed -n 's/^ARG RUNTIME_IMAGE=//p' "${module_root}/Dockerfile")"
[[ "${python_builder_image}" =~ @sha256:[0-9a-f]{64}$ ]] || fail "invalid Python builder image"
[[ "${runtime_image}" =~ @sha256:[0-9a-f]{64}$ ]] || fail "invalid runtime base image"
python_builder_digest="sha256:${python_builder_image##*@sha256:}"
runtime_base_digest="sha256:${runtime_image##*@sha256:}"
expected_libcrypto_digest="sha256:72db1b3de8b7dfbaba4c056135f408da555f9d5e137c82129478e07e769f8070"
expected_libssl_digest="sha256:9aec161fdbc82d3e4280f5084843118939f1f4acc53c98ec963de03cfe812fad"

assert_repo_digest() {
  local reference="$1"
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
for image in "${syft_image}" "${trivy_image}" "${cosign_image}" "${python_builder_image}" "${runtime_image}"; do
  assert_repo_digest "${image}"
done

source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
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
tar -xf "${source_archive}" -C "${temporary_root}/snapshot"
docker image inspect "${image_ref}" >/dev/null 2>&1 || fail "release image is absent: ${image_ref}"
image_id="$(docker image inspect --format '{{.Id}}' "${image_ref}")"
image_source_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.source-tree.digest"}}' "${image_ref}")"
[[ "${image_source_digest}" == "${source_tree_digest}" ]] \
  || fail "image source label does not match current source snapshot"
[[ "$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.python-builder.digest"}}' "${image_ref}")" == "${python_builder_digest}" ]] \
  || fail "image Python builder label mismatch"
[[ "$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-base.digest"}}' "${image_ref}")" == "${runtime_base_digest}" ]] \
  || fail "image runtime base label mismatch"
[[ "$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-base.python.fallback"}}' "${image_ref}")" == "false" ]] \
  || fail "runtime base interpreter fallback must be disabled"

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
docker save --output "${temporary_root}/bundle/analysis-image.tar" "${image_ref}"
docker save --output "${temporary_root}/bundle/python-builder-image.tar" "${python_builder_image}"

tool_run=(docker run --rm --user 0:0 --network none --read-only --cap-drop ALL
  --security-opt no-new-privileges:true --pids-limit 128 --memory 2g --cpus 2
  --tmpfs /tmp:rw,noexec,nosuid,size=1g
  --tmpfs /.cache:rw,noexec,nosuid,size=64m
  --tmpfs /root/.cache:rw,noexec,nosuid,size=512m)

"${tool_run[@]}" --env SYFT_CHECK_FOR_APP_UPDATE=false \
  --volume "${temporary_root}/bundle:/input:ro" --volume "${evidence_dir}:/out" \
  "${syft_image}" scan docker-archive:/input/analysis-image.tar \
  -o spdx-json=/out/image.spdx.json
"${tool_run[@]}" --env SYFT_CHECK_FOR_APP_UPDATE=false \
  --volume "${temporary_root}/snapshot:/source:ro" --volume "${evidence_dir}:/out" \
  "${syft_image}" scan dir:/source -o spdx-json=/out/source.spdx.json
"${tool_run[@]}" --env SYFT_CHECK_FOR_APP_UPDATE=false \
  --volume "${temporary_root}/bundle:/input:ro" --volume "${evidence_dir}:/out" \
  "${syft_image}" scan docker-archive:/input/python-builder-image.tar \
  -o spdx-json=/out/python-builder-source.spdx.json

"${tool_run[@]}" \
  --volume "${trivy_cache}/db:/root/.cache/trivy/db:ro" \
  --volume "${trivy_cache}/java-db:/root/.cache/trivy/java-db:ro" \
  --volume "${temporary_root}/bundle:/input:ro" --volume "${evidence_dir}:/out" \
  "${trivy_image}" image --input /input/analysis-image.tar \
  --skip-db-update --skip-java-db-update --scanners vuln,secret \
  --format json --output /out/trivy-image.json
"${tool_run[@]}" \
  --volume "${trivy_cache}/db:/root/.cache/trivy/db:ro" \
  --volume "${trivy_cache}/java-db:/root/.cache/trivy/java-db:ro" \
  --volume "${temporary_root}/bundle:/input:ro" --volume "${evidence_dir}:/out" \
  "${trivy_image}" image --input /input/python-builder-image.tar \
  --skip-db-update --skip-java-db-update --scanners vuln,secret \
  --format json --output /out/trivy-python-builder-source.json
"${tool_run[@]}" \
  --volume "${trivy_cache}/policy:/root/.cache/trivy/policy:ro" \
  --volume "${temporary_root}/snapshot:/source:ro" --volume "${evidence_dir}:/out" \
  "${trivy_image}" config --skip-check-update --skip-version-check \
  --checks-bundle-repository "${checks_reference}" --format json \
  --output /out/trivy-config.json /source/analysis-py \
  >"${evidence_dir}/trivy-config.log" 2>&1
grep -Fq "Falling back to embedded checks" "${evidence_dir}/trivy-config.log" \
  && fail "Trivy attempted embedded checks fallback"
"${tool_run[@]}" \
  --volume "${temporary_root}/snapshot:/source:ro" --volume "${evidence_dir}:/out" \
  "${trivy_image}" fs --scanners secret --skip-db-update \
  --format json --output /out/trivy-source-secret.json /source

jq -n \
  --arg source_revision "${source_revision}" \
  --arg source_tree_digest "${source_tree_digest}" \
  --arg image_ref "${image_ref}" \
  --arg image_id "${image_id}" \
  --arg python_builder_image_digest "${python_builder_digest}" \
  --arg runtime_base_image_digest "${runtime_base_digest}" \
  --arg libcrypto_digest "${libcrypto_digest}" \
  --arg libssl_digest "${libssl_digest}" \
  --argjson runtime_versions "${runtime_versions}" \
  --arg working_tree_status_digest "${working_tree_status_digest}" \
  --argjson working_tree_dirty "${working_tree_dirty}" \
  --arg image_sbom "sha256:$(sha256sum "${evidence_dir}/image.spdx.json" | awk '{print $1}')" \
  --arg source_sbom "sha256:$(sha256sum "${evidence_dir}/source.spdx.json" | awk '{print $1}')" \
  --arg python_builder_sbom "sha256:$(sha256sum "${evidence_dir}/python-builder-source.spdx.json" | awk '{print $1}')" \
  --arg trivy_image "sha256:$(sha256sum "${evidence_dir}/trivy-image.json" | awk '{print $1}')" \
  --arg trivy_python_builder "sha256:$(sha256sum "${evidence_dir}/trivy-python-builder-source.json" | awk '{print $1}')" \
  --arg trivy_source "sha256:$(sha256sum "${evidence_dir}/trivy-source-secret.json" | awk '{print $1}')" \
  --arg trivy_config "sha256:$(sha256sum "${evidence_dir}/trivy-config.json" | awk '{print $1}')" \
  --arg component_registry_digest "sha256:$(sha256sum "${repo_root}/contracts/supply-chain/v1/analysis-plugin-components.json" | awk '{print $1}')" \
  --arg notices_digest "sha256:$(sha256sum "${repo_root}/contracts/supply-chain/v1/analysis-plugin-third-party-notices.json" | awk '{print $1}')" \
  --arg lock_digest "sha256:$(sha256sum "${module_root}/uv.lock" | awk '{print $1}')" \
  '{
    schema_version: "analysis-release-manifest/v1",
    module_id: "MOD-AGENT-001",
    source_revision: $source_revision,
    source_tree_digest: $source_tree_digest,
    image_ref: $image_ref,
    image_id: $image_id,
    python_builder_image_digest: $python_builder_image_digest,
    runtime_base_image_digest: $runtime_base_image_digest,
    runtime_library_digests: {
      libcrypto: $libcrypto_digest,
      libssl: $libssl_digest
    },
    runtime_versions: $runtime_versions,
    runtime_base_python_fallback: false,
    working_tree_status_digest: $working_tree_status_digest,
    working_tree_dirty: $working_tree_dirty,
    runtime_profile: "analysis-agent-runtime/v1",
    python_version: "3.12.13",
    runtime_download: false,
    lock_digest: $lock_digest,
    component_registry_digest: $component_registry_digest,
    third_party_notices_digest: $notices_digest,
    evidence_digests: {
      "image.spdx.json": $image_sbom,
      "source.spdx.json": $source_sbom,
      "python-builder-source.spdx.json": $python_builder_sbom,
      "trivy-image.json": $trivy_image,
      "trivy-python-builder-source.json": $trivy_python_builder,
      "trivy-source-secret.json": $trivy_source,
      "trivy-config.json": $trivy_config
    }
  }' >"${evidence_dir}/release-manifest.json"

"${tool_run[@]}" --env COSIGN_PASSWORD= \
  --volume "${key_dir}:/keys:ro" --volume "${evidence_dir}:/supply" \
  --volume "${repo_root}/deploy/supply-chain/cosign-offline-signing-config.json:/config/signing-config.json:ro" \
  "${cosign_image}" sign-blob --yes --signing-config /config/signing-config.json \
  --bundle /supply/release-manifest.sigstore.json \
  --key /keys/cosign.key /supply/release-manifest.json \
  >"${evidence_dir}/cosign-sign.log" 2>&1
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
  --argjson wrong "${wrong_rejected}" '{
    positive: $positive,
    tampered_rejected: $tampered,
    wrong_publisher_rejected: $wrong,
    tools_network_none: true
  }' >"${evidence_dir}/signature-checks.json"
rm -- "${evidence_dir}/release-manifest.tampered.json"

"${python_runtime}" "${script_dir}/verify-supply-chain.py" \
  --repo "${repo_root}" --supply-dir "${evidence_dir}" \
  --source-revision "${source_revision}" --source-tree-digest "${source_tree_digest}" \
  --working-tree-status-digest "${working_tree_status_digest}" \
  --image-ref "${image_ref}" --image-id "${image_id}" \
  --output "${evidence_dir}/supply-chain-evidence.json"
