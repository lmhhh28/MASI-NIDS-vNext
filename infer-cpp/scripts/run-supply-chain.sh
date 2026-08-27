#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
inf_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${inf_root}/.." && pwd)"
evidence_root="${MASI_INF_SUPPLY_EVIDENCE_DIR:-${inf_root}/evidence/supply-chain}"
image_ref="${MASI_INF_IMAGE_REF:-masi-inference:module-gates}"
builder_ref="${MASI_INF_BUILDER_IMAGE_REF:-masi-inference-builder:module-gates}"
trivy_cache="${MASI_TRIVY_CACHE:-${repo_root}/out/supply-chain/trivy-cache}"
key_dir="${MASI_COSIGN_KEY_DIR:-${repo_root}/.masi-secrets/cosign}"
run_id="${MASI_INF_SUPPLY_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
run_dir="${evidence_root}/runs/${run_id}"
supply_dir="${run_dir}/supply"
bundle_dir="${repo_root}/out/supply-chain/offline-bundles/central-inference-cpu-${run_id}"
snapshot_dir="${repo_root}/out/supply-chain/source-snapshots/central-inference-cpu-${run_id}"
rebuild_dir="${repo_root}/out/supply-chain/rebuilds/central-inference-cpu-${run_id}"
verification="${run_dir}/supply-chain.json"
manifest="${supply_dir}/release-manifest.json"
signature_bundle="${supply_dir}/release-manifest.sigstore.json"
latest_index="${evidence_root}/latest.json"
container_id=""

fail() {
  echo "central-inference supply-chain gate: $*" >&2
  exit 1
}

cleanup() {
  if [[ -n "${container_id}" ]]; then
    docker rm --force "${container_id}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ ! "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  fail "invalid supply-chain run id"
fi

publish_latest() {
  local result_digest temporary
  result_digest="sha256:$(sha256sum "${verification}" | awk '{print $1}')"
  temporary="${evidence_root}/.latest-${run_id}.json"
  jq -n --arg run_id "${run_id}" --arg evidence "runs/${run_id}/supply-chain.json" \
    --arg digest "${result_digest}" \
    '{schema_version:"central-inference-supply-latest/v1",run_id:$run_id,evidence:$evidence,digest:$digest}' \
    >"${temporary}"
  jq -e 'type == "object" and keys == ["digest","evidence","run_id","schema_version"]' \
    "${temporary}" >/dev/null
  mv -T -- "${temporary}" "${latest_index}"
}

for command in cmake docker git grep jq pkg-config python3 sha256sum strings tar; do
  command -v "${command}" >/dev/null 2>&1 || fail "missing command ${command}"
done
[[ ! -e "${run_dir}" && ! -e "${bundle_dir}" && ! -e "${snapshot_dir}" \
  && ! -e "${rebuild_dir}" ]] || fail "run id already exists"
mkdir -p -- "${evidence_root}/runs"
mkdir -- "${run_dir}" "${bundle_dir}" "${snapshot_dir}" "${rebuild_dir}"
mkdir -- "${supply_dir}" "${rebuild_dir}/build"

for required in \
  "${trivy_cache}/db/trivy.db" \
  "${trivy_cache}/db/metadata.json" \
  "${trivy_cache}/java-db/trivy-java.db" \
  "${trivy_cache}/java-db/metadata.json" \
  "${trivy_cache}/policy/metadata.json" \
  "${key_dir}/cosign.key" \
  "${key_dir}/cosign.pub" \
  "${repo_root}/deploy/supply-chain/cosign-offline-signing-config.json"; do
  [[ -f "${required}" && ! -L "${required}" ]] || fail "required offline input absent: ${required}"
done
[[ -d "${trivy_cache}/policy/content" && ! -L "${trivy_cache}/policy/content" ]] \
  || fail "offline Trivy checks cache absent"
cmp -s "${key_dir}/cosign.pub" "${repo_root}/contracts/trust/v1/cosign.pub" \
  || fail "signing public key does not match the trust contract"

python3 "${script_dir}/test-supply-input-safety.py" >"${supply_dir}/input-safety.json"

working_tree_status="$(git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)"
working_tree_dirty=false
if [[ -n "${working_tree_status}" ]]; then
  working_tree_dirty=true
fi
printf '%s\n' "${working_tree_status}" >"${supply_dir}/working-tree-status.txt"
working_tree_status_digest="sha256:$(sha256sum "${supply_dir}/working-tree-status.txt" | awk '{print $1}')"
source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
source_archive="${bundle_dir}/source-tree.tar"
tar --sort=name --mtime=@1786406400 --owner=0 --group=0 --numeric-owner \
  --exclude='infer-cpp/build' --exclude='infer-cpp/evidence' \
  --exclude='**/node_modules' --exclude='**/__pycache__' --exclude='*.pyc' \
  -cf "${source_archive}" -C "${repo_root}" infer-cpp contracts testkit
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
if [[ -n "${MASI_INF_EXPECTED_SOURCE_TREE_DIGEST:-}" \
  && "${source_tree_digest}" != "${MASI_INF_EXPECTED_SOURCE_TREE_DIGEST}" ]]; then
  fail "source tree changed after module gate snapshot"
fi
tar -xf "${source_archive}" -C "${snapshot_dir}"
mkdir -- "${snapshot_dir}/infer-cpp/build"

components="${repo_root}/contracts/supply-chain/v1/central-inference-cpu-components.json"
vendored="${repo_root}/contracts/supply-chain/v1/central-inference-vendored-sources.json"
registered_grpc="$(jq -er '.components[] | select(.name=="gRPC C++") | .version' "${components}" | sed 's/^v//')"
registered_protobuf="$(jq -er '.components[] | select(.name=="Protocol Buffers") | .version' "${components}")"
grpc_prefix="${MASI_INF_GRPC_PREFIX:-/opt/masi-toolchain/grpc-${registered_grpc}}"
observed_grpc="$(PKG_CONFIG_PATH="${grpc_prefix}/lib/pkgconfig" pkg-config --modversion grpc++)"
observed_protobuf="$(PKG_CONFIG_PATH="${grpc_prefix}/lib/pkgconfig" pkg-config --modversion protobuf)"
[[ "${observed_grpc}" == "${registered_grpc}" && "${observed_protobuf}" == "${registered_protobuf}" ]] \
  || fail "registered gRPC/Protobuf toolchain drift"

vendored_checked=0
while IFS=$'\t' read -r relative expected; do
  [[ -f "${repo_root}/${relative}" ]] || fail "vendored source missing: ${relative}"
  [[ "$(sha256sum "${repo_root}/${relative}" | awk '{print $1}')" == "${expected}" ]] \
    || fail "vendored source digest drift: ${relative}"
  vendored_checked=$((vendored_checked + 1))
done < <(jq -r '.files[] | [.vendored_path,.sha256] | @tsv' "${vendored}")
[[ "${vendored_checked}" -gt 0 ]] || fail "vendored registry is empty"
forbidden="${supply_dir}/forbidden-triton-rpcs.txt"
: >"${forbidden}"
while read -r rpc; do
  grep -rn --include='*.cc' --include='*.h' --include='*.cpp' \
    -e "->${rpc}(" -e "\\.${rpc}(" "${inf_root}/src" >>"${forbidden}" 2>/dev/null || true
done < <(jq -r '.method_allowlist.forbidden_client_rpcs[]' "${vendored}")
[[ ! -s "${forbidden}" ]] || fail "Gateway calls a forbidden Triton RPC"
jq -n --arg grpc "${observed_grpc}" --arg protobuf "${observed_protobuf}" \
  --arg prefix "${grpc_prefix}" --argjson vendored "${vendored_checked}" \
  '{schema_version:"central-inference-toolchain-binding/v1",grpc_version:$grpc,
    protobuf_version:$protobuf,toolchain_prefix:$prefix,vendored_files_verified:$vendored,
    forbidden_triton_rpc_references:0}' >"${supply_dir}/toolchain-binding.json"

tools_lock="${repo_root}/deploy/supply-chain/tools.lock.json"
syft_image="$(jq -er '.tools.syft.image' "${tools_lock}")"
trivy_image="$(jq -er '.tools.trivy.image' "${tools_lock}")"
cosign_image="$(jq -er '.tools.cosign.image' "${tools_lock}")"
checks_reference="$(jq -er '.trivy_checks_bundle.reference' "${tools_lock}")"
for required_image in "${image_ref}" "${builder_ref}" "${syft_image}" "${trivy_image}" "${cosign_image}"; do
  docker image inspect "${required_image}" >/dev/null 2>&1 \
    || fail "required local image absent: ${required_image}"
done
for tool_image in "${syft_image}" "${trivy_image}" "${cosign_image}"; do
  docker image inspect --format '{{json .RepoDigests}}' "${tool_image}" \
    | jq -e --arg expected "${tool_image}" 'index($expected) != null' >/dev/null \
    || fail "tool image manifest digest drift: ${tool_image}"
done

image_revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "${image_ref}")"
image_source_tree="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.source-tree.digest"}}' "${image_ref}")"
[[ "${image_revision}" == "${source_revision}" && "${image_source_tree}" == "${source_tree_digest}" ]] \
  || fail "release image labels are stale"
docker image inspect "${image_ref}" >"${supply_dir}/image-config.json"
image_config_digest="$(docker image inspect --format '{{.Id}}' "${image_ref}")"
image_repo_digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "${image_ref}")"
image_manifest_digest="${image_repo_digest##*@}"
[[ "${image_repo_digest}" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] \
  || fail "release image has no immutable RepoDigest"

container_id="$(docker create "${image_ref}" true)"
docker cp "${container_id}:/usr/local/bin/masi_inference_gateway" \
  "${supply_dir}/masi_inference_gateway.image.bin"
docker rm "${container_id}" >/dev/null
container_id=""
[[ -s "${supply_dir}/masi_inference_gateway.image.bin" ]] || fail "image binary is empty"
image_binary_digest="sha256:$(sha256sum "${supply_dir}/masi_inference_gateway.image.bin" | awk '{print $1}')"

offline_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set +e
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 512 --memory 4g --cpus 4 \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m --tmpfs /root:rw,noexec,nosuid,size=32m \
  --volume "${snapshot_dir}:/workspace:ro" \
  --volume "${rebuild_dir}/build:/workspace/infer-cpp/build" \
  --workdir /workspace/infer-cpp --entrypoint /bin/bash "${builder_ref}" -lc \
  'cmake --preset cpu-release -DMASI_INF_OFFLINE=ON >/tmp/configure.log 2>&1 &&
   cmake --build build/cpu-release --target masi_inference_gateway -- -j4' \
  >"${supply_dir}/offline-rebuild.log" 2>&1
offline_status=$?
set -e
offline_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
rebuilt_binary="${rebuild_dir}/build/cpu-release/masi_inference_gateway"
[[ "${offline_status}" -eq 0 && -s "${rebuilt_binary}" ]] || fail "network-none clean snapshot rebuild failed"
cp -- "${rebuilt_binary}" "${supply_dir}/masi_inference_gateway.offline-rebuild.bin"
rebuilt_binary_digest="sha256:$(sha256sum "${supply_dir}/masi_inference_gateway.offline-rebuild.bin" | awk '{print $1}')"
binary_digest_match=false
if [[ "${image_binary_digest}" == "${rebuilt_binary_digest}" ]]; then
  binary_digest_match=true
fi
jq -n --arg started_at "${offline_started_at}" --arg finished_at "${offline_finished_at}" \
  --arg source_tree_digest "${source_tree_digest}" --arg builder_image "${builder_ref}" \
  --arg image_binary_digest "${image_binary_digest}" \
  --arg rebuilt_binary_digest "${rebuilt_binary_digest}" \
  --argjson exit_code "${offline_status}" --argjson digest_match "${binary_digest_match}" '{
    schema_version:"central-inference-offline-rebuild/v1",started_at:$started_at,
    finished_at:$finished_at,network:"none",clean_snapshot:true,
    command:"cmake --build build/cpu-release --target masi_inference_gateway -- -j4",
    source_tree_digest:$source_tree_digest,builder_image:$builder_image,exit_code:$exit_code,
    image_binary_digest:$image_binary_digest,rebuilt_binary_digest:$rebuilt_binary_digest,
    binary_digest_match:$digest_match}' >"${supply_dir}/offline-rebuild.json"
[[ "${binary_digest_match}" == true ]] || fail "offline rebuild binary digest mismatch"

docker image save --output "${bundle_dir}/inference-image.tar" "${image_ref}"
archive_config_path="$(tar -xOf "${bundle_dir}/inference-image.tar" manifest.json | jq -er '.[0].Config')"
archive_config_hex="$(basename -- "${archive_config_path}" .json)"
[[ "${archive_config_hex}" =~ ^[0-9a-f]{64}$ ]] || fail "image archive config path is invalid"
[[ "$(tar -xOf "${bundle_dir}/inference-image.tar" "${archive_config_path}" | sha256sum | awk '{print $1}')" \
  == "${archive_config_hex}" ]] || fail "image archive config digest mismatch"
image_config_digest="sha256:${archive_config_hex}"
docker image save --output "${bundle_dir}/builder-image.tar" "${builder_ref}"
docker image save --output "${bundle_dir}/tool-images.tar" \
  "${syft_image}" "${trivy_image}" "${cosign_image}"
tar -cf "${bundle_dir}/trivy-db.tar" -C "${trivy_cache}" db java-db policy
cp -- "${tools_lock}" "${bundle_dir}/tools.lock.json"
cp -- "${repo_root}/deploy/supply-chain/trivy-policy.json" "${bundle_dir}/trivy-policy.json"
cp -- "${repo_root}/deploy/supply-chain/cosign-offline-signing-config.json" \
  "${bundle_dir}/cosign-offline-signing-config.json"
cp -- "${repo_root}/contracts/trust/v1/cosign.pub" "${bundle_dir}/cosign.pub"

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /.cache:rw,noexec,nosuid,size=64m \
  --env SYFT_CHECK_FOR_APP_UPDATE=false \
  --volume "${bundle_dir}:/input:ro" --volume "${supply_dir}:/out" \
  "${syft_image}" scan docker-archive:/input/inference-image.tar -o spdx-json=/out/image.spdx.json
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /.cache:rw,noexec,nosuid,size=64m \
  --env SYFT_CHECK_FOR_APP_UPDATE=false \
  --volume "${snapshot_dir}:/source:ro" --volume "${supply_dir}:/out" \
  "${syft_image}" scan dir:/source -o spdx-json=/out/source.spdx.json

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /root/.cache/trivy/fanal:rw,noexec,nosuid,size=256m \
  --volume "${trivy_cache}/db:/root/.cache/trivy/db:ro" \
  --volume "${trivy_cache}/java-db:/root/.cache/trivy/java-db:ro" \
  --volume "${bundle_dir}:/input:ro" --volume "${supply_dir}:/out" \
  "${trivy_image}" image --input /input/inference-image.tar \
  --skip-db-update --skip-java-db-update --scanners vuln,secret \
  --format json --output /out/trivy-image.json
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /root/.cache/trivy/fanal:rw,noexec,nosuid,size=256m \
  --volume "${trivy_cache}/policy:/root/.cache/trivy/policy:ro" \
  --volume "${snapshot_dir}:/source:ro" --volume "${supply_dir}:/out" \
  "${trivy_image}" config --skip-check-update --skip-version-check \
  --checks-bundle-repository "${checks_reference}" --format json \
  --output /out/trivy-config.json /source >"${supply_dir}/trivy-config.log" 2>&1
grep -Fq "Falling back to embedded checks" "${supply_dir}/trivy-config.log" \
  && fail "Trivy config scan fell back to embedded checks"
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /root/.cache/trivy/fanal:rw,noexec,nosuid,size=256m \
  --volume "${snapshot_dir}:/source:ro" --volume "${supply_dir}:/out" \
  "${trivy_image}" fs --scanners secret --skip-db-update \
  --format json --output /out/trivy-source-secret.json /source

(
  cd -- "${bundle_dir}"
  find . -type f ! -path './SHA256SUMS' -printf '%P\0' \
    | LC_ALL=C sort -z | xargs -0 sha256sum >"${supply_dir}/SHA256SUMS.tmp"
  mv -- "${supply_dir}/SHA256SUMS.tmp" SHA256SUMS
  sha256sum -c SHA256SUMS >"${supply_dir}/offline-bundle-verify.log"
)

builder_base="$(awk -F= '/^ARG BUILDER_IMAGE=/{print $2; exit}' "${inf_root}/Dockerfile")"
runtime_base="$(awk -F= '/^ARG RUNTIME_IMAGE=/{print $2; exit}' "${inf_root}/Dockerfile")"
python3 "${script_dir}/build-supply-manifest.py" \
  --repo "${repo_root}" --supply-dir "${supply_dir}" --bundle-dir "${bundle_dir}" \
  --run-id "${run_id}" --source-revision "${source_revision}" \
  --source-tree-digest "${source_tree_digest}" --working-tree-dirty "${working_tree_dirty}" \
  --working-tree-status-digest "${working_tree_status_digest}" \
  --image-ref "${image_repo_digest}" --image-manifest-digest "${image_manifest_digest}" \
  --image-config-digest "${image_config_digest}" --builder-image "${builder_base}" \
  --runtime-image "${runtime_base}" --trivy-cache "${trivy_cache}" \
  --offline-rebuild "${supply_dir}/offline-rebuild.json" \
  --provenance-output "${supply_dir}/provenance.intoto.json" \
  --manifest-output "${manifest}"

docker run --rm --user 0:0 --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 64 --memory 256m --cpus 1 \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m --env COSIGN_PASSWORD= \
  --volume "${key_dir}:/keys:ro" --volume "${supply_dir}:/supply" \
  --volume "${repo_root}/deploy/supply-chain/cosign-offline-signing-config.json:/config/signing-config.json:ro" \
  "${cosign_image}" sign-blob --yes --signing-config /config/signing-config.json \
  --bundle /supply/release-manifest.sigstore.json --key /keys/cosign.key \
  /supply/release-manifest.json >"${supply_dir}/cosign-sign.log" 2>&1
chmod 0644 "${signature_bundle}"

set +e
python3 "${script_dir}/verify-supply-chain.py" \
  --repo "${repo_root}" --supply-dir "${supply_dir}" --bundle-dir "${bundle_dir}" \
  --manifest "${manifest}" --signature-bundle "${signature_bundle}" \
  --trivy-cache "${trivy_cache}" --output "${verification}"
verification_status=$?
set -e
python3 "${script_dir}/validate-evidence.py" \
  --schema "${repo_root}/contracts/evidence/central-inference-supply/v1/schema.json" \
  --document "${verification}" >/dev/null
jq . "${verification}"
publish_latest
exit "${verification_status}"
