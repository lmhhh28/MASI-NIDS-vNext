#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
host_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${host_root}/.." && pwd)"
evidence_dir="${MASI_PLUGIN_HOST_SUPPLY_EVIDENCE_DIR:-${host_root}/evidence/supply-chain}"
image_ref="${MASI_PLUGIN_HOST_IMAGE_REF:-masi-plugin-host:module-gates}"
trivy_cache="${MASI_TRIVY_CACHE:-${repo_root}/out/supply-chain/trivy-cache}"
key_dir="${MASI_COSIGN_KEY_DIR:-${repo_root}/.masi-secrets/cosign}"
temporary_root="$(mktemp -d /tmp/masi-plugin-host-supply.XXXXXX)"

cleanup() {
  if [[ "${temporary_root}" == /tmp/masi-plugin-host-supply.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

fail() {
  echo "plugin-host supply chain: $*" >&2
  exit 1
}

for command in docker git jq python3 sha256sum tar; do
  command -v "${command}" >/dev/null 2>&1 || fail "missing command ${command}"
done
mkdir -p -- "${evidence_dir}" "${temporary_root}/snapshot" "${temporary_root}/bundle"
[[ ! -e "${evidence_dir}/supply-chain-evidence.json" ]] \
  || fail "supply-chain evidence already exists"
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
for image in "${syft_image}" "${trivy_image}" "${cosign_image}" \
  "docker.io/library/rust@sha256:408fe88047cef61a2087653b0c5255fa51c0f2d6d94ddedd7a2562a9b91a46f6" \
  "gcr.io/distroless/cc-debian12@sha256:adcd20c7b4c988b73cbfbddb26d2eee574571e6d7c9ffea29b3821e0690efb77"; do
  assert_repo_digest "${image}"
done

source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
source_archive="${temporary_root}/source.tar"
tar --sort=name --mtime=@1787270400 --owner=0 --group=0 --numeric-owner \
  --exclude='plugin-host-rs/target' --exclude='plugin-host-rs/evidence' \
  -cf "${source_archive}" -C "${repo_root}" plugin-host-rs contracts deploy/plugin-host
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
tar -xf "${source_archive}" -C "${temporary_root}/snapshot"
docker image inspect "${image_ref}" >/dev/null 2>&1 || fail "release image is absent: ${image_ref}"
image_id="$(docker image inspect --format '{{.Id}}' "${image_ref}")"
image_source_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.source-tree.digest"}}' "${image_ref}")"
[[ "${image_source_digest}" == "${source_tree_digest}" ]] \
  || fail "image source label does not match current source snapshot"
docker save --output "${temporary_root}/bundle/plugin-host-image.tar" "${image_ref}"

tool_run=(docker run --rm --user 0:0 --network none --read-only --cap-drop ALL
  --security-opt no-new-privileges:true --pids-limit 128 --memory 2g --cpus 2
  --tmpfs /tmp:rw,noexec,nosuid,size=256m
  --tmpfs /.cache:rw,noexec,nosuid,size=64m
  --tmpfs /root/.cache:rw,noexec,nosuid,size=512m)

"${tool_run[@]}" --env SYFT_CHECK_FOR_APP_UPDATE=false \
  --volume "${temporary_root}/bundle:/input:ro" --volume "${evidence_dir}:/out" \
  "${syft_image}" scan docker-archive:/input/plugin-host-image.tar \
  -o spdx-json=/out/image.spdx.json
"${tool_run[@]}" --env SYFT_CHECK_FOR_APP_UPDATE=false \
  --volume "${temporary_root}/snapshot:/source:ro" --volume "${evidence_dir}:/out" \
  "${syft_image}" scan dir:/source -o spdx-json=/out/source.spdx.json

"${tool_run[@]}" \
  --volume "${trivy_cache}/db:/root/.cache/trivy/db:ro" \
  --volume "${trivy_cache}/java-db:/root/.cache/trivy/java-db:ro" \
  --volume "${temporary_root}/bundle:/input:ro" --volume "${evidence_dir}:/out" \
  "${trivy_image}" image --input /input/plugin-host-image.tar \
  --skip-db-update --skip-java-db-update --scanners vuln,secret \
  --format json --output /out/trivy-image.json
"${tool_run[@]}" \
  --volume "${trivy_cache}/policy:/root/.cache/trivy/policy:ro" \
  --volume "${temporary_root}/snapshot:/source:ro" --volume "${evidence_dir}:/out" \
  "${trivy_image}" config --skip-check-update --skip-version-check \
  --checks-bundle-repository "${checks_reference}" --format json \
  --output /out/trivy-config.json /source/plugin-host-rs \
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
  --arg image_sbom "sha256:$(sha256sum "${evidence_dir}/image.spdx.json" | awk '{print $1}')" \
  --arg source_sbom "sha256:$(sha256sum "${evidence_dir}/source.spdx.json" | awk '{print $1}')" \
  --arg trivy_image "sha256:$(sha256sum "${evidence_dir}/trivy-image.json" | awk '{print $1}')" \
  --arg trivy_source "sha256:$(sha256sum "${evidence_dir}/trivy-source-secret.json" | awk '{print $1}')" \
  --arg trivy_config "sha256:$(sha256sum "${evidence_dir}/trivy-config.json" | awk '{print $1}')" \
  --arg component_registry_digest "sha256:$(sha256sum "${repo_root}/contracts/supply-chain/v1/plugin-runtime-host-components.json" | awk '{print $1}')" \
  --arg notices_digest "sha256:$(sha256sum "${repo_root}/contracts/supply-chain/v1/plugin-runtime-host-third-party-notices.json" | awk '{print $1}')" \
  '{
    schema_version: "plugin-host-release-manifest/v1",
    module_id: "MOD-PLUGIN-001",
    source_revision: $source_revision,
    source_tree_digest: $source_tree_digest,
    image_ref: $image_ref,
    image_id: $image_id,
    runtime_profile: "plugin-runtime-host/v1",
    wasmtime_version: "47.0.3",
    runtime_download: false,
    component_registry_digest: $component_registry_digest,
    third_party_notices_digest: $notices_digest,
    evidence_digests: {
      "image.spdx.json": $image_sbom,
      "source.spdx.json": $source_sbom,
      "trivy-image.json": $trivy_image,
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

python3 "${script_dir}/verify-supply-chain.py" \
  --repo "${repo_root}" --supply-dir "${evidence_dir}" \
  --source-revision "${source_revision}" --source-tree-digest "${source_tree_digest}" \
  --image-ref "${image_ref}" --image-id "${image_id}" \
  --output "${evidence_dir}/supply-chain-evidence.json"
