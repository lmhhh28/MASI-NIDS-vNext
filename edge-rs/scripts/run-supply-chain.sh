#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
edge_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${edge_root}/.." && pwd)"
evidence_root="${MASI_EDGE_SUPPLY_EVIDENCE_DIR:-${edge_root}/evidence/supply-chain}"
image_ref="${MASI_EDGE_IMAGE_REF:-masi-edge:module-gates}"
trivy_cache="${MASI_TRIVY_CACHE:-${repo_root}/out/supply-chain/trivy-cache}"
key_dir="${MASI_COSIGN_KEY_DIR:-${repo_root}/.masi-secrets/cosign}"
run_id="${MASI_EDGE_SUPPLY_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
run_dir="${evidence_root}/runs/${run_id}"
supply_dir="${run_dir}/supply"
bundle_dir="${repo_root}/out/supply-chain/offline-bundles/rust-edge-agent-${run_id}"
snapshot_dir="${repo_root}/out/supply-chain/source-snapshots/rust-edge-agent-${run_id}"
rebuild_dir="${repo_root}/out/supply-chain/rebuilds/rust-edge-agent-${run_id}"
source_archive="${bundle_dir}/source-tree.tar"
image_archive="${bundle_dir}/edge-image.tar"
offline_result="${supply_dir}/offline-rebuild.json"
manifest="${supply_dir}/release-manifest.json"
signature_bundle="${supply_dir}/release-manifest.sigstore.json"
verification="${run_dir}/supply-chain.json"
latest_index="${evidence_root}/latest.json"
container_id=""

cleanup() {
  if [[ -n "${container_id}" ]]; then
    docker rm --force "${container_id}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

fail() {
  echo "rust-edge-agent supply-chain gate: $*" >&2
  exit 1
}

if [[ ! "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  fail "invalid supply-chain run id"
fi

publish_latest() {
  local result_digest=""
  if [[ -f "${verification}" ]]; then
    result_digest="sha256:$(sha256sum "${verification}" | awk '{print $1}')"
  fi
  jq -n --arg run_id "${run_id}" --arg evidence "runs/${run_id}/supply-chain.json" \
    --arg digest "${result_digest}" \
    '{schema_version:"rust-edge-agent-supply-latest/v1",run_id:$run_id,evidence:$evidence,digest:$digest}' \
    >"${evidence_root}/.latest-${run_id}.json"
  mv -- "${evidence_root}/.latest-${run_id}.json" "${latest_index}"
}

assert_pinned_repo_digest() {
  local image="$1"
  local repository="${image%@sha256:*}"
  local digest="sha256:${image##*@sha256:}"
  local normalized_repository="${repository}"
  case "${normalized_repository}" in
    docker.io/library/*) normalized_repository="${normalized_repository#docker.io/library/}" ;;
    docker.io/*) normalized_repository="${normalized_repository#docker.io/}" ;;
  esac
  local expected="${normalized_repository}@${digest}"
  local repo_digests
  repo_digests="$(docker image inspect --format '{{json .RepoDigests}}' "${image}")"
  jq -e --arg expected "${expected}" 'index($expected) != null' <<<"${repo_digests}" >/dev/null \
    || fail "image RepoDigest mismatch for ${image}; observed ${repo_digests}"
}

normalized_repository() {
  local repository="${1%@*}"
  local tail="${repository##*/}"
  if [[ "${tail}" == *:* ]]; then
    repository="${repository%:*}"
  fi
  case "${repository}" in
    docker.io/library/*) repository="${repository#docker.io/library/}" ;;
    docker.io/*) repository="${repository#docker.io/}" ;;
  esac
  printf '%s\n' "${repository}"
}

assert_no_symbolic_path_components() {
  python3 - "$@" <<'PY'
import os
import sys
from pathlib import Path

for value in sys.argv[1:]:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise SystemExit(f"supply-chain path must be absolute: {value!r}")
    if len(os.fsencode(candidate)) > 4096:
        raise SystemExit(f"supply-chain path exceeds its byte ceiling: {value!r}")
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise SystemExit(f"supply-chain path contains a symbolic component: {current}")
PY
}

for command in cargo docker git grep jq python3 sha256sum tar; do
  command -v "${command}" >/dev/null 2>&1 || fail "missing command ${command}"
done

python3 "${script_dir}/test-supply-input-safety.py"
assert_no_symbolic_path_components \
  "${repo_root}" "${evidence_root}" "${run_dir}" "${supply_dir}" \
  "${bundle_dir}" "${snapshot_dir}" "${rebuild_dir}" "${trivy_cache}" "${key_dir}"

for path in \
  "${trivy_cache}/db/trivy.db" \
  "${trivy_cache}/db/metadata.json" \
  "${trivy_cache}/java-db/trivy-java.db" \
  "${trivy_cache}/java-db/metadata.json" \
  "${trivy_cache}/policy/metadata.json" \
  "${key_dir}/cosign.key" \
  "${key_dir}/cosign.pub" \
  "${repo_root}/deploy/supply-chain/cosign-offline-signing-config.json"; do
  [[ -f "${path}" ]] || fail "required offline input is absent: ${path}"
done
[[ -d "${trivy_cache}/policy/content" ]] \
  || fail "required offline Trivy checks content is absent"
cmp -s "${key_dir}/cosign.pub" "${repo_root}/contracts/trust/v1/cosign.pub" \
  || fail "signing public key does not match the trust contract"

working_tree_porcelain="$(git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)"
working_tree_dirty=false
if [[ -n "${working_tree_porcelain}" ]]; then
  working_tree_dirty=true
fi

[[ ! -e "${run_dir}" ]] || fail "run id already has evidence: ${run_id}"
[[ ! -e "${bundle_dir}" ]] || fail "run id already has an offline bundle: ${run_id}"
[[ ! -e "${snapshot_dir}" ]] || fail "run id already has a source snapshot: ${run_id}"
[[ ! -e "${rebuild_dir}" ]] || fail "run id already has a rebuild directory: ${run_id}"
mkdir -p -- "${evidence_root}/runs"
mkdir -- "${run_dir}"
mkdir -p -- "${supply_dir}" "${bundle_dir}" \
  "${snapshot_dir}" "${rebuild_dir}" "${rebuild_dir}/target"
assert_no_symbolic_path_components \
  "${evidence_root}" "${run_dir}" "${supply_dir}" "${bundle_dir}" \
  "${snapshot_dir}" "${rebuild_dir}" "${rebuild_dir}/target"
printf '%s\n' "${working_tree_porcelain}" >"${supply_dir}/working-tree-status.txt"
working_tree_status_digest="sha256:$(sha256sum "${supply_dir}/working-tree-status.txt" | awk '{print $1}')"

readarray -t tool_images < <(
  python3 - "${repo_root}/deploy/supply-chain/tools.lock.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as source:
    lock = json.load(source)
for name in ("syft", "trivy", "cosign"):
    print(lock["tools"][name]["image"])
PY
)
syft_image="${tool_images[0]}"
trivy_image="${tool_images[1]}"
cosign_image="${tool_images[2]}"
trivy_checks_reference="$(jq -er '.trivy_checks_bundle.reference' \
  "${repo_root}/deploy/supply-chain/tools.lock.json")"
trivy_checks_digest="${trivy_checks_reference##*@}"
[[ "${trivy_checks_reference}" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ \
  && "$(jq -er '.Digest' "${trivy_cache}/policy/metadata.json")" == "${trivy_checks_digest}" ]] \
  || fail "offline Trivy checks metadata does not match the digest-pinned bundle"
builder_image="$(awk -F= '/^ARG RUST_IMAGE=/{print $2; exit}' "${edge_root}/Dockerfile")"
runtime_image="$(awk -F= '/^ARG RUNTIME_IMAGE=/{print $2; exit}' "${edge_root}/Dockerfile")"

for image in "${image_ref}" "${builder_image}" "${runtime_image}" \
  "${syft_image}" "${trivy_image}" "${cosign_image}"; do
  docker image inspect "${image}" >/dev/null 2>&1 \
    || fail "required digest-pinned image is not local: ${image}"
done
for image in "${builder_image}" "${runtime_image}" "${syft_image}" "${trivy_image}" "${cosign_image}"; do
  assert_pinned_repo_digest "${image}"
done

source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
source_revision_label="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "${image_ref}")"
source_tree_digest_label="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.source-tree.digest"}}' "${image_ref}")"
builder_digest_label="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.rust-builder.digest"}}' "${image_ref}")"
runtime_digest_label="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.runtime-base.digest"}}' "${image_ref}")"
module_label="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.module"}}' "${image_ref}")"
[[ "${source_revision_label}" == "${source_revision}" ]] || fail "image source revision label is stale"
[[ "${builder_digest_label}" == "sha256:${builder_image##*@sha256:}" ]] || fail "image builder digest label is invalid"
[[ "${runtime_digest_label}" == "sha256:${runtime_image##*@sha256:}" ]] || fail "image runtime digest label is invalid"
[[ "${module_label}" == "MOD-EDGE-001" ]] || fail "image module label is invalid"

tar --sort=name --mtime=@1786406400 --owner=0 --group=0 --numeric-owner \
  --exclude='edge-rs/target' --exclude='edge-rs/evidence' \
  --exclude='edge-rs/**/__pycache__' --exclude='contracts/**/__pycache__' \
  -cf "${source_archive}" -C "${repo_root}" edge-rs contracts
tar -xf "${source_archive}" -C "${snapshot_dir}"
mkdir -p -- "${snapshot_dir}/edge-rs/target"
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
[[ "${source_tree_digest_label}" == "${source_tree_digest}" ]] \
  || fail "image source tree digest label is stale"
if [[ -n "${MASI_EDGE_EXPECTED_SOURCE_TREE_DIGEST:-}" \
  && "${source_tree_digest}" != "${MASI_EDGE_EXPECTED_SOURCE_TREE_DIGEST}" ]]; then
  fail "supply-chain source tree changed after module gate snapshot"
fi

CARGO_NET_OFFLINE=true cargo vendor --locked --versioned-dirs \
  --manifest-path "${edge_root}/Cargo.toml" "${bundle_dir}/vendor" \
  >"${supply_dir}/cargo-vendor-config.toml" \
  2>"${supply_dir}/cargo-vendor.log"
tar --sort=name --mtime=@1786406400 --owner=0 --group=0 --numeric-owner \
  -cf "${bundle_dir}/cargo-vendor.tar" -C "${bundle_dir}" vendor

docker image save --output "${image_archive}" "${image_ref}"
docker image save --output "${bundle_dir}/base-images.tar" "${builder_image}" "${runtime_image}"
docker image save --output "${bundle_dir}/tool-images.tar" \
  "${syft_image}" "${trivy_image}" "${cosign_image}"
tar -cf "${bundle_dir}/trivy-db.tar" -C "${trivy_cache}" db java-db policy
cp -- "${repo_root}/deploy/supply-chain/tools.lock.json" "${bundle_dir}/tools.lock.json"
cp -- "${repo_root}/deploy/supply-chain/trivy-policy.json" "${bundle_dir}/trivy-policy.json"
cp -- "${repo_root}/deploy/supply-chain/cosign-offline-signing-config.json" "${bundle_dir}/cosign-offline-signing-config.json"
cp -- "${repo_root}/contracts/trust/v1/cosign.pub" "${bundle_dir}/cosign.pub"
python3 "${script_dir}/inspect-oci-archive.py" \
  --archive "${image_archive}" --image-ref "${image_ref}" \
  --index-output "${supply_dir}/image-archive-manifest.json" \
  --config-output "${supply_dir}/image-archive-config.json" \
  --summary-output "${supply_dir}/oci-archive-inspection.json"
[[ "$(jq -er '.attestation_count' "${supply_dir}/oci-archive-inspection.json")" == "0" ]] \
  || fail "release OCI archive contains an undeclared embedded attestation"
docker image inspect "${image_ref}" >"${supply_dir}/image-config.json"

container_id="$(docker create "${image_ref}")"
docker cp "${container_id}:/usr/local/bin/masi-edge" "${supply_dir}/masi-edge.image.bin"
docker rm "${container_id}" >/dev/null
container_id=""
[[ -s "${supply_dir}/masi-edge.image.bin" ]] || fail "release image binary is absent or empty"

offline_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set +e
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 512 \
  --memory 4g --cpus 4 --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --tmpfs /cargo-home:rw,noexec,nosuid,size=64m \
  --volume "${snapshot_dir}:/workspace:ro" \
  --volume "${bundle_dir}/vendor:/vendor:ro" \
  --volume "${rebuild_dir}:/out" \
  --workdir /workspace/edge-rs \
  --env CARGO_HOME=/cargo-home \
  --env CARGO_NET_OFFLINE=true \
  --env RUSTUP_TOOLCHAIN=1.97.1-x86_64-unknown-linux-gnu \
  --volume "${rebuild_dir}/target:/workspace/edge-rs/target" \
  "${builder_image}" cargo build --offline --locked --release --bin masi-edge \
  --config 'source.crates-io.replace-with="vendored-sources"' \
  --config 'source.vendored-sources.directory="/vendor"' \
  >"${supply_dir}/offline-rebuild.log" 2>&1
offline_rebuild_status=$?
set -e
offline_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
rebuilt_binary="${rebuild_dir}/target/release/masi-edge"
rebuilt_binary_digest=""
if [[ "${offline_rebuild_status}" -eq 0 && -s "${rebuilt_binary}" ]]; then
  cp -- "${rebuilt_binary}" "${supply_dir}/masi-edge.offline-rebuild.bin"
  rebuilt_binary_digest="sha256:$(sha256sum "${supply_dir}/masi-edge.offline-rebuild.bin" | awk '{print $1}')"
fi
image_binary_digest="sha256:$(sha256sum "${supply_dir}/masi-edge.image.bin" | awk '{print $1}')"
binary_digest_match=false
if [[ "${offline_rebuild_status}" -eq 0 && "${image_binary_digest}" == "${rebuilt_binary_digest}" ]]; then
  binary_digest_match=true
fi
jq -n \
  --arg started_at "${offline_started_at}" --arg finished_at "${offline_finished_at}" \
  --arg source_tree_digest "${source_tree_digest}" \
  --arg builder_image "${builder_image}" \
  --arg image_binary_digest "${image_binary_digest}" \
  --arg rebuilt_binary_digest "${rebuilt_binary_digest}" \
  --argjson exit_code "${offline_rebuild_status}" \
  --argjson binary_digest_match "${binary_digest_match}" '{
    schema_version: "rust-edge-agent-offline-rebuild/v1",
    started_at: $started_at,
    finished_at: $finished_at,
    network: "none",
    cargo_locked_offline: true,
    command: "cargo build --offline --locked --release --bin masi-edge",
    source_tree_digest: $source_tree_digest,
    builder_image: $builder_image,
    exit_code: $exit_code,
    image_binary_digest: $image_binary_digest,
    rebuilt_binary_digest: $rebuilt_binary_digest,
    binary_digest_match: $binary_digest_match
  }' >"${offline_result}"

if [[ "${offline_rebuild_status}" -ne 0 || ! -s "${supply_dir}/masi-edge.offline-rebuild.bin" || "${binary_digest_match}" != true ]]; then
  failure_reason="OFFLINE_REBUILD_FAILED"
  if [[ "${offline_rebuild_status}" -eq 0 && -s "${supply_dir}/masi-edge.offline-rebuild.bin" ]]; then
    failure_reason="OFFLINE_REBUILD_BINARY_DIGEST_MISMATCH"
  fi
  jq -n --arg run_id "${run_id}" --arg reason "${failure_reason}" \
    --arg source_revision "${source_revision}" --arg source_tree_digest "${source_tree_digest}" \
    --argjson working_tree_dirty "${working_tree_dirty}" \
    --arg working_tree_status_digest "${working_tree_status_digest}" \
    --arg offline_rebuild_digest "sha256:$(sha256sum "${offline_result}" | awk '{print $1}')" '{
      schema_version:"rust-edge-agent-supply-verification/v1",
      test_id:"SEC-SUPPLY-001",requirement_ids:["MOD-EDGE-001","ARCH-REUSE-001","SEC-SUPPLY-001","TEST-010"],
      run_id:$run_id,level:"MODULE",applicability:"APPLICABLE",result:"FAIL",qualification:"NOT_QUALIFIED",
      source_revision:$source_revision,source_tree_digest:$source_tree_digest,
      working_tree_dirty:$working_tree_dirty,working_tree_status_digest:$working_tree_status_digest,
      manifest_digest:null,checks:{offline_rebuild:false},offline_rebuild_digest:$offline_rebuild_digest,
      failure_reasons:[$reason],hold_reasons:[],overall_module_complete:false
    }' >"${verification}"
  python3 "${script_dir}/validate-edge-evidence.py" \
    --repo "${repo_root}" --evidence "${verification}"
  publish_latest
  jq . "${verification}"
  exit 1
fi

docker run --rm --network none \
  --env SYFT_CHECK_FOR_APP_UPDATE=false \
  --volume "${bundle_dir}:/input:ro" --volume "${supply_dir}:/out" \
  "${syft_image}" scan docker-archive:/input/edge-image.tar \
  -o spdx-json=/out/image.spdx.json
docker run --rm --network none \
  --env SYFT_CHECK_FOR_APP_UPDATE=false \
  --volume "${snapshot_dir}:/source:ro" --volume "${supply_dir}:/out" \
  "${syft_image}" scan dir:/source -o spdx-json=/out/source.spdx.json

docker run --rm --network none \
  --volume "${trivy_cache}/db:/root/.cache/trivy/db:ro" \
  --volume "${trivy_cache}/java-db:/root/.cache/trivy/java-db:ro" \
  --volume "${bundle_dir}:/input:ro" --volume "${supply_dir}:/out" \
  "${trivy_image}" image --input /input/edge-image.tar \
  --skip-db-update --skip-java-db-update --scanners vuln,secret \
  --format json --output /out/trivy-image.json
docker run --rm --network none \
  --volume "${trivy_cache}/policy:/root/.cache/trivy/policy:ro" \
  --volume "${snapshot_dir}:/source:ro" --volume "${supply_dir}:/out" \
  "${trivy_image}" config --skip-check-update --skip-version-check \
  --checks-bundle-repository "${trivy_checks_reference}" --format json \
  --skip-files /source/edge-rs/Dockerfile.dockerignore \
  --output /out/trivy-config.json /source \
  >"${supply_dir}/trivy-config.log" 2>&1
if grep -Fq "Falling back to embedded checks" "${supply_dir}/trivy-config.log"; then
  fail "Trivy config scan attempted an embedded-check fallback"
fi
docker run --rm --network none \
  --volume "${snapshot_dir}:/source:ro" --volume "${supply_dir}:/out" \
  "${trivy_image}" fs --scanners secret --skip-db-update \
  --format json --output /out/trivy-source-secret.json /source

(
  cd "${bundle_dir}"
  checksum_temp="${supply_dir}/bundle-SHA256SUMS-${run_id}.tmp"
  find . -type f ! -path './SHA256SUMS' -printf '%P\0' \
    | LC_ALL=C sort -z | xargs -0 sha256sum >"${checksum_temp}"
  mv -- "${checksum_temp}" SHA256SUMS
  sha256sum -c SHA256SUMS >"${supply_dir}/offline-bundle-verify.log"
)

image_archive_index_digest="$(jq -er '.index_digest' "${supply_dir}/oci-archive-inspection.json")"
image_config_digest="$(jq -er '.config_digest' "${supply_dir}/oci-archive-inspection.json")"
archive_manifest_digest="$(jq -er '.manifest_digest' "${supply_dir}/oci-archive-inspection.json")"
release_repo_digests="$(docker image inspect --format '{{json .RepoDigests}}' "${image_ref}")"
release_repository="$(normalized_repository "${image_ref}")"
observed_repo_manifest_digest="$(jq -er --arg prefix "${release_repository}@" \
  '[.[] | select(startswith($prefix))] | if length == 1 then .[0] | split("@")[-1] else error("release image has no unique exact-repository RepoDigest") end' \
  <<<"${release_repo_digests}")"
[[ "${observed_repo_manifest_digest}" == "${archive_manifest_digest}" ]] \
  || fail "release OCI archive manifest disagrees with exact RepoDigest"
image_manifest_digest="${archive_manifest_digest}"
python3 "${script_dir}/create-supply-manifest.py" \
  --repo "${repo_root}" --supply-dir "${supply_dir}" --bundle-dir "${bundle_dir}" \
  --source-revision "${source_revision}" --source-tree-digest "${source_tree_digest}" \
  --image-manifest-digest "${image_manifest_digest}" \
  --image-archive-index-digest "${image_archive_index_digest}" \
  --image-config-digest "${image_config_digest}" --image-ref "${image_ref}" \
  --builder-image "${builder_image}" --runtime-image "${runtime_image}" \
  --working-tree-dirty "${working_tree_dirty}" \
  --working-tree-status-digest "${working_tree_status_digest}" \
  --run-id "${run_id}" \
  --offline-rebuild "${offline_result}" --trivy-cache "${trivy_cache}" \
  --provenance-output "${supply_dir}/provenance.intoto.json" \
  --manifest-output "${manifest}"

docker run --rm --user 0:0 --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 64 --memory 256m --cpus 1 \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m --env COSIGN_PASSWORD= \
  --volume "${key_dir}:/keys:ro" --volume "${supply_dir}:/supply" \
  --volume "${repo_root}/deploy/supply-chain/cosign-offline-signing-config.json:/config/signing-config.json:ro" \
  "${cosign_image}" sign-blob --yes \
  --signing-config /config/signing-config.json \
  --bundle /supply/release-manifest.sigstore.json \
  --key /keys/cosign.key /supply/release-manifest.json \
  >"${supply_dir}/cosign-sign.log" 2>&1
chmod 0644 "${signature_bundle}"

set +e
python3 "${script_dir}/verify-supply-chain.py" \
  --repo "${repo_root}" --supply-dir "${supply_dir}" --bundle-dir "${bundle_dir}" \
  --manifest "${manifest}" --bundle "${signature_bundle}" \
  --trivy-cache "${trivy_cache}" --output "${verification}"
verification_status=$?
set -e
if ! python3 "${script_dir}/validate-edge-evidence.py" \
  --repo "${repo_root}" --evidence "${verification}"; then
  verification_status=1
fi
jq . "${verification}"
publish_latest
exit "${verification_status}"
