#!/usr/bin/env bash
# OCI startup smoke for the Central Inference Gateway (MOD-INF-001).
#
# Builds the real distroless OCI image, then starts it for real against a REAL
# Triton sidecar (the digest-pinned r3 fixture server) on an isolated Docker
# network. The Gateway connects to Triton over mutual TLS (non-loopback endpoint
# => triton_tls_* is required); plaintext/loopback shortcuts and --network host
# are deliberately not used. A real, digest-pinned startup envelope is generated
# from the r3 closure manifests so the Gateway's startup gate (live ModelConfig
# vs pinned config.pbtxt vs frozen profile, exact triton_server_version, wire
# profile digest, envelope_digest self-check) actually passes. An independent
# client leaf then exercises the public mTLS boundary (GetBinding) and the
# container is verified to exit 0 on SIGTERM. No fake model repo, no zero
# digests, no hardcoded Triton port.
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"
inf_root="${repo_root}/infer-cpp"
if [[ "${MASI_RUNTIME_SMOKE_WRAPPED:-0}" != "1" ]]; then
  exec python3 "${repo_root}/scripts/ci/run_bounded_runtime_smoke.py" \
    --repo "${repo_root}" --module inference \
    --timeout-seconds "${MASI_INF_OCI_TOTAL_TIMEOUT_SECONDS:-14400}" -- \
    "${script_dir}/run-oci-smoke.sh" "$@"
fi
evidence_dir="${MASI_INF_EVIDENCE_DIR:-${inf_root}/evidence/oci-smoke}"
image_ref="${MASI_INF_IMAGE_REF:-masi-inference:module-smoke}"
builder_ref="${MASI_INF_BUILDER_IMAGE_REF:-masi-inference-builder:module-gates}"
runtime_export_dir="${MASI_INF_EXPORT_RUNTIME_DIR:-}"
hold_seconds="${MASI_INF_HOLD_SECONDS:-0}"
source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
temporary_root="$(mktemp -d /tmp/masi-inf-oci-smoke.XXXXXX)"
container_name="masi-inf-oci-smoke-${$}"
triton_container="masi-inf-oci-smoke-triton-${$}"
smoke_network="masi-inf-oci-smoke-net-${$}"

if [[ ! "${hold_seconds}" =~ ^[0-9]+$ ]] || (( hold_seconds > 28800 )); then
  echo "MASI_INF_HOLD_SECONDS must be an integer in 0..28800" >&2
  exit 64
fi
if (( hold_seconds > 0 )); then
  if [[ -z "${runtime_export_dir}" || "${runtime_export_dir}" != /* \
    || ! -d "${runtime_export_dir}" || -L "${runtime_export_dir}" \
    || -n "$(find "${runtime_export_dir}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "hold mode requires an absolute, empty, non-symlink MASI_INF_EXPORT_RUNTIME_DIR" >&2
    exit 64
  fi
  chmod 0700 "${runtime_export_dir}"
elif [[ -n "${runtime_export_dir}" ]]; then
  echo "MASI_INF_EXPORT_RUNTIME_DIR requires MASI_INF_HOLD_SECONDS>0" >&2
  exit 64
fi

# Pinned Triton sidecar (contracts/profiles/v1/central-inference-cpu.json#triton).
# Derive both the immutable image reference and the expected live metadata
# version from the same public profile so a stale duplicated literal cannot
# silently qualify a different server build.
central_cpu_profile="${repo_root}/contracts/profiles/v1/central-inference-cpu.json"
triton_image="$(jq -er '.triton.image' "${central_cpu_profile}")"
triton_image_digest="$(jq -er '.triton.image_digest' "${central_cpu_profile}")"
expected_triton_version="$(jq -er '.triton.triton_version' "${central_cpu_profile}")"
[[ "${triton_image}" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ \
  && "${triton_image##*@}" == "${triton_image_digest}" ]] \
  || { echo "central CPU profile has an invalid Triton image binding" >&2; exit 1; }
fixture_repo="${repo_root}/testkit/fixtures/repositories/masi-ids-window-v1-r3"
proto_dir="${inf_root}/proto/vendor/triton"

cleanup() {
  timeout --signal=TERM --kill-after=5s 30s docker rm --force "${container_name}" >/dev/null 2>&1 || true
  timeout --signal=TERM --kill-after=5s 30s docker rm --force "${triton_container}" >/dev/null 2>&1 || true
  timeout --signal=TERM --kill-after=5s 30s docker network rm "${smoke_network}" >/dev/null 2>&1 || true
  if [[ "${temporary_root}" == /tmp/masi-inf-oci-smoke.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT
command -v timeout >/dev/null 2>&1 || { echo "missing timeout" >&2; exit 69; }
docker_binary="$(command -v docker)"
[[ -n "${docker_binary}" ]] || { echo "missing docker" >&2; exit 69; }
docker() {
  timeout --signal=TERM --kill-after=30s \
    "${MASI_INF_DOCKER_COMMAND_TIMEOUT_SECONDS:-1800}" "${docker_binary}" "$@"
}

mkdir -p -- "${evidence_dir}" "${temporary_root}/secrets" "${temporary_root}/probe" \
  "${temporary_root}/config" "${temporary_root}/data" "${temporary_root}/triton-ca" \
  "${temporary_root}/triton-server" "${temporary_root}/triton-client"
for output in inference-image.tar image-archive-manifest.json image-config.json \
  oci-probe.json oci-smoke-evidence.json; do
  [[ ! -e "${evidence_dir}/${output}" && ! -L "${evidence_dir}/${output}" ]] \
    || { echo "OCI evidence output already exists: ${output}" >&2; exit 1; }
done

# The r3 fixture must be a digest-pinned, read-only, symlink-free closure. The
# Gateway's repository-closure validator checks mode bits (not mount ro-ness),
# so fail fast here with a clear message instead of a deep startup failure.
if ! [[ -d "${fixture_repo}" ]]; then
  echo "r3 fixture repository missing: ${fixture_repo}" >&2
  exit 1
fi
if [[ -n "$(find "${fixture_repo}" \( -perm /u+w -o -perm /g+w -o -perm /o+w \) -print -quit 2>/dev/null)" ]]; then
  echo "r3 fixture has writable path(s); expected a read-only digest-pinned closure" >&2
  exit 1
fi
if [[ -n "$(find "${fixture_repo}" -type l -print -quit 2>/dev/null)" ]]; then
  echo "r3 fixture contains symlinks; repository closure forbids symlinks" >&2
  exit 1
fi

generate_ca() {
  local out_dir="$1"
  local cn="$2"
  openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 2 \
    -subj "/CN=${cn}" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "${out_dir}/ca.key" \
    -out "${out_dir}/ca.pem" >/dev/null 2>&1
}

generate_leaf() {
  local output_dir="$1" name="$2" serial="$3" usage="$4" san="${5:-}"
  local ca_cert="$6" ca_key="$7"
  local request=(openssl req -new -newkey rsa:3072 -nodes -sha256
    -subj "/CN=${name}" -keyout "${output_dir}/${name}.key"
    -out "${output_dir}/${name}.csr" -addext "extendedKeyUsage=${usage}")
  if [[ -n "${san}" ]]; then
    request+=(-addext "subjectAltName=${san}")
  fi
  "${request[@]}" >/dev/null 2>&1
  openssl x509 -req -sha256 -days 2 -set_serial "${serial}" \
    -in "${output_dir}/${name}.csr" \
    -CA "${ca_cert}" -CAkey "${ca_key}" \
    -copy_extensions copyall -out "${output_dir}/${name}.pem" >/dev/null 2>&1
  chmod 0600 "${output_dir}/${name}.key"
  rm -- "${output_dir}/${name}.csr"
}

# Gateway-facing PKI: CA + server leaf (SAN covers the probe's 127.0.0.1 target,
# since the probe performs no SNI override) + probe client leaf (SAN must be in
# the deployment client_san_allowlist).
generate_ca "${temporary_root}" "MASI Inference module smoke CA"
cp -- "${temporary_root}/ca.pem" "${temporary_root}/secrets/ca.pem"
cp -- "${temporary_root}/ca.pem" "${temporary_root}/probe/ca.pem"
generate_leaf "${temporary_root}/secrets" inference-server 301 serverAuth \
  "DNS:inference.test,IP:127.0.0.1" "${temporary_root}/ca.pem" "${temporary_root}/ca.key"
generate_leaf "${temporary_root}/probe" inference-probe 303 clientAuth \
  "DNS:masi-edge.test" "${temporary_root}/ca.pem" "${temporary_root}/ca.key"

probe_certificate_digest="sha256:$(openssl x509 \
  -in "${temporary_root}/probe/inference-probe.pem" -outform DER | sha256sum | awk '{print $1}')"

# Triton-facing PKI: separate CA + Triton server leaf (SAN DNS:triton for the
# Gateway's SetSslTargetNameOverride("triton") + IP:127.0.0.1 for the host
# grpcurl version probe) + Gateway client leaf (presented to Triton under
# mutual TLS). The Gateway mounts ca + client cert/key; Triton mounts ca +
# server cert/key.
generate_ca "${temporary_root}/triton-ca" "MASI Inference Triton smoke CA"
cp -- "${temporary_root}/triton-ca/ca.pem" "${temporary_root}/probe/triton-ca.pem"
generate_leaf "${temporary_root}/triton-server" triton-server 401 serverAuth \
  "DNS:triton,IP:127.0.0.1" "${temporary_root}/triton-ca/ca.pem" "${temporary_root}/triton-ca/ca.key"
generate_leaf "${temporary_root}/triton-client" triton-client 402 clientAuth \
  "DNS:masi-gateway.test" "${temporary_root}/triton-ca/ca.pem" "${temporary_root}/triton-ca/ca.key"
cp -- "${temporary_root}/triton-ca/ca.pem" "${temporary_root}/triton-server/ca.pem"
cp -- "${temporary_root}/triton-ca/ca.pem" "${temporary_root}/triton-client/ca.pem"

# Read the real r3 closure digests straight from the committed manifests. The
# Gateway's repository-closure validator recomputes member hashes and the
# sorted-line closure digest and compares to closure-manifest.json, so these
# values must be the manifest's own (never recomputed in bash).
closure_manifest="${fixture_repo}/closure-manifest.json"
bundle_manifest="${fixture_repo}/bundle-manifest.json"
repo_identity="$(jq -er '.identity' "${closure_manifest}")"
closure_digest="$(jq -er '.closure_digest' "${closure_manifest}")"
model_revision_digest="$(jq -er '.model_revision_digest' "${bundle_manifest}")"
feature_digest="$(jq -er '.contract_digests.feature_contract_digest' "${bundle_manifest}")"
label_digest="$(jq -er '.contract_digests.label_contract_digest' "${bundle_manifest}")"
adapter_digest="$(jq -er '.contract_digests.output_adapter_digest' "${bundle_manifest}")"
runtime_profile_digest="$(jq -er '.binding_identity.runtime_profile_digest' "${bundle_manifest}")"
optimization_profile_digest="$(jq -er '.binding_identity.optimization_profile_digest' "${bundle_manifest}")"

# The one build-time-pinned digest: raw-byte SHA-256 of the frozen wire profile.
# CMake injects this as MASI_INF_WIRE_PROFILE_DIGEST (CMakeLists.txt); the
# binary rejects any envelope declaring a different value. Compute it from the
# same file so the smoke tracks the profile the binary was built against.
wire_profile_digest="sha256:$(sha256sum "${repo_root}/contracts/inference/v1/profile.json" | awk '{print $1}')"
empty_operator_partition_digest="sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

working_tree_dirty=false
working_tree_status="$(git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)"
if [[ -n "${working_tree_status}" ]]; then
  working_tree_dirty=true
fi
printf '%s\n' "${working_tree_status}" >"${evidence_dir}/working-tree-status.txt"
working_tree_status_digest="sha256:$(sha256sum "${evidence_dir}/working-tree-status.txt" | awk '{print $1}')"
source_archive="${temporary_root}/source-tree.tar"
tar --sort=name --mtime=@1786406400 --owner=0 --group=0 --numeric-owner \
  --exclude='infer-cpp/build' --exclude='infer-cpp/evidence' \
  --exclude='**/node_modules' --exclude='**/__pycache__' --exclude='*.pyc' \
  -cf "${source_archive}" -C "${repo_root}" infer-cpp contracts testkit
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
if [[ -n "${MASI_INF_EXPECTED_SOURCE_TREE_DIGEST:-}" \
  && "${source_tree_digest}" != "${MASI_INF_EXPECTED_SOURCE_TREE_DIGEST}" ]]; then
  echo "OCI source tree changed after module gate snapshot" >&2
  exit 1
fi

# Build the Docker image. The builder fetches the registered gRPC from the
# pinned commit (verified against GRPC_COMMIT inside the RUN) and so needs
# build-time network access; this is distinct from the runtime prohibition on
# --network host. The image has no baked-in config/secret.
timeout --signal=TERM --kill-after=60s 1800s docker build --platform linux/amd64 --provenance=false --sbom=false --target builder \
  --file "${inf_root}/Dockerfile" \
  --build-arg "SOURCE_REVISION=${source_revision}" \
  --build-arg "SOURCE_TREE_DIGEST=${source_tree_digest}" \
  --tag "${builder_ref}" "${repo_root}"
timeout --signal=TERM --kill-after=60s 1800s docker build --platform linux/amd64 --provenance=false --sbom=false \
  --file "${inf_root}/Dockerfile" \
  --build-arg "SOURCE_REVISION=${source_revision}" \
  --build-arg "SOURCE_TREE_DIGEST=${source_tree_digest}" \
  --tag "${image_ref}" "${repo_root}"

# Build the probe from this source snapshot. A stale probe cannot qualify a new
# image, so configure/build failures are fatal and the resulting digest is
# recorded below.
cd -- "${inf_root}"
timeout --signal=TERM --kill-after=30s 600s cmake --preset cpu-release -DCMAKE_BUILD_TYPE=Release >/dev/null
timeout --signal=TERM --kill-after=30s 600s cmake --build build/cpu-release --target masi_inference_probe >/dev/null
probe_bin="${inf_root}/build/cpu-release/masi_inference_probe"
[[ -x "${probe_bin}" ]] || { echo "current-source OCI probe was not built" >&2; exit 1; }
probe_binary_digest="sha256:$(sha256sum "${probe_bin}" | awk '{print $1}')"

image_user="$(docker image inspect --format '{{.Config.User}}' "${image_ref}")"
if [[ "${image_user}" != "65532:65532" ]]; then
  echo "OCI image user is not 65532:65532" >&2
  exit 1
fi
image_revision_label="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "${image_ref}")"
image_source_tree_label="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.source-tree.digest"}}' "${image_ref}")"
if [[ "${image_revision_label}" != "${source_revision}" \
  || "${image_source_tree_label}" != "${source_tree_digest}" ]]; then
  echo "OCI image source labels do not bind the current source snapshot" >&2
  exit 1
fi

# Record the image config and the binary digest embedded in the image. The
# supply-chain gate independently extracts the same binary and compares it to an
# offline rebuild; recording it here makes the smoke evidence self-describing.
docker image inspect "${image_ref}" >"${evidence_dir}/image-config.json"
image_repo_digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "${image_ref}")"
image_manifest_digest="${image_repo_digest##*@}"
[[ "${image_manifest_digest}" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || { echo "OCI image has no immutable manifest RepoDigest" >&2; exit 1; }
extract_container="$(timeout --signal=TERM --kill-after=5s 30s docker create "${image_ref}" true)"
timeout --signal=TERM --kill-after=5s 60s docker cp "${extract_container}:/usr/local/bin/masi_inference_gateway" \
  "${temporary_root}/gateway-binary"
timeout --signal=TERM --kill-after=5s 30s docker rm --force "${extract_container}" >/dev/null
if [[ ! -s "${temporary_root}/gateway-binary" ]]; then
  echo "could not extract masi_inference_gateway from image" >&2
  exit 1
fi
gateway_binary_digest="sha256:$(sha256sum "${temporary_root}/gateway-binary" | awk '{print $1}')"

# Start the real Triton sidecar on an isolated network. The Gateway will reach
# it by service name (triton:8001); the host reaches the published port for the
# version probe only. The --network-alias triton makes "triton" resolvable to
# this container on smoke_network (Docker DNS resolves by container name/alias;
# the container's own name is a run-specific id, so the alias is required). The
# alias "triton" also matches the triton-server cert SAN DNS:triton that the
# Gateway verifies via its SetSslTargetNameOverride("triton"). Triton runs with
# gRPC mutual TLS: the server presents triton-server.pem and verifies client
# certs against the same CA.
timeout --signal=TERM --kill-after=5s 30s docker network create "${smoke_network}" >/dev/null
timeout --signal=TERM --kill-after=10s 60s docker run --detach --name "${triton_container}" --network "${smoke_network}" \
  --network-alias triton \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --pids-limit 512 --memory 4g --cpus 4 --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --publish 127.0.0.1:0:8001 \
  --volume "${fixture_repo}:/models:ro" \
  --volume "${temporary_root}/triton-server:/tls:ro" \
  "${triton_image}" \
  tritonserver --model-repository=/models --model-control-mode=none \
    --disable-auto-complete-config --strict-readiness=true \
    --grpc-use-ssl-mutual=true \
    --grpc-server-cert=/tls/triton-server.pem \
    --grpc-server-key=/tls/triton-server.key \
    --grpc-root-cert=/tls/ca.pem \
    --grpc-port=8001 --http-port=8000 --metrics-port=8002 \
    --allow-grpc=true --allow-http=false --allow-metrics=false \
  >/dev/null

triton_host_port=""
for _port_attempt in $(seq 1 50); do
  triton_host_port="$(docker port "${triton_container}" 8001/tcp 2>/dev/null | awk -F: 'NR == 1 {print $NF}' || true)"
  if [[ "${triton_host_port}" =~ ^[0-9]+$ ]]; then
    break
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "${triton_container}")" != "true" ]]; then
    break
  fi
  sleep 0.2
done
if [[ ! "${triton_host_port}" =~ ^[0-9]+$ ]]; then
  docker logs "${triton_container}" >"${evidence_dir}/triton-oci.log" 2>&1 || true
  echo "Triton sidecar did not publish a usable 8001/tcp host port" >&2
  exit 1
fi

# grpcurl invocation against the mutual-TLS Triton endpoint. The triton-server
# leaf carries IP:127.0.0.1 so the host probe (no SNI override) verifies.
grpcurl_args=(-cacert "${temporary_root}/triton-client/ca.pem"
  -cert "${temporary_root}/triton-client/triton-client.pem"
  -key "${temporary_root}/triton-client/triton-client.key"
  -import-path "${proto_dir}"
  -proto grpc_service.proto -proto model_config.proto)

triton_version=""
triton_ready=false
for _attempt in $(seq 1 90); do
  if [[ "$(docker inspect --format '{{.State.Running}}' "${triton_container}")" != "true" ]]; then
    break
  fi
  server_meta="$(timeout --signal=TERM --kill-after=5s 10s grpcurl "${grpcurl_args[@]}" -d '{}' \
    "127.0.0.1:${triton_host_port}" inference.GRPCInferenceService/ServerMetadata 2>/dev/null || true)"
  if [[ -n "${server_meta}" ]]; then
    triton_version="$(printf '%s' "${server_meta}" | jq -r '.version // empty' 2>/dev/null || true)"
    model_ready="$(timeout --signal=TERM --kill-after=5s 10s grpcurl "${grpcurl_args[@]}" \
      -d '{"name":"masi-ids-window-v1","version":"1"}' \
      "127.0.0.1:${triton_host_port}" inference.GRPCInferenceService/ModelReady 2>/dev/null || true)"
    if [[ "${triton_version}" != "" ]] \
      && [[ "$(printf '%s' "${model_ready}" | jq -r '.ready // false' 2>/dev/null || echo false)" == "true" ]]; then
      triton_ready=true
      break
    fi
  fi
  sleep 1
done

if [[ "${triton_ready}" != "true" ]]; then
  # Fallback version source if grpcurl is unavailable on a runner: the pinned
  # image prints "Triton Server Version <x.y.z>" early in its startup log.
  if [[ -z "${triton_version}" ]]; then
    triton_version="$(docker logs "${triton_container}" 2>&1 \
      | grep -oE 'Triton Server Version [0-9]+\.[0-9]+\.[0-9]+' | head -1 \
      | awk '{print $4}' || true)"
  fi
fi
if [[ -z "${triton_version}" ]]; then
  docker logs "${triton_container}" >"${evidence_dir}/triton-oci.log" 2>&1 || true
  echo "could not determine live Triton server version" >&2
  exit 1
fi
if [[ "${triton_ready}" != "true" ]]; then
  docker logs "${triton_container}" >"${evidence_dir}/triton-oci.log" >&2 || true
  echo "Triton sidecar or model never became ready" >&2
  exit 1
fi
if [[ "${triton_version}" != "${expected_triton_version}" ]]; then
  docker logs "${triton_container}" >"${evidence_dir}/triton-oci.log" 2>&1 || true
  echo "live Triton version mismatch: expected ${expected_triton_version}, observed ${triton_version}" >&2
  exit 1
fi

# Build the real startup envelope. The envelope_digest preimage must be
# byte-identical to compute_envelope_body_digest (envelope.cc:31-61): the 26
# fields below (everything except envelope_digest) each followed by '\n', in
# this exact order, numerics as bare decimal. parse_startup_envelope recomputes
# it and rejects on mismatch. The preimage is piped straight into sha256sum so
# the trailing '\n' after the last field is hashed (command substitution would
# strip it and break the digest).
envelope_digest="sha256:$(printf '%s\n' \
  "inference-startup-envelope/v1" \
  "incarnation-oci-smoke-0001" \
  "op-oci-smoke-0001" \
  "binding" \
  "pool-oci-smoke-0001" \
  "1" \
  "availability-single/v1" \
  "acceptance" \
  "${model_revision_digest}" \
  "${feature_digest}" \
  "${label_digest}" \
  "${adapter_digest}" \
  "${wire_profile_digest}" \
  "model-runtime-central-cpu/v1" \
  "${runtime_profile_digest}" \
  "${optimization_profile_digest}" \
  "${triton_version}" \
  "${repo_identity}" \
  "${closure_digest}" \
  "KIND_CPU" \
  "1" \
  "${empty_operator_partition_digest}" \
  "1" \
  "0" \
  "9999999999999" \
  "trace-oci-smoke-0001" | sha256sum | awk '{print $1}')"

jq -n \
  --arg model_revision_digest "${model_revision_digest}" \
  --arg feature_digest "${feature_digest}" \
  --arg label_digest "${label_digest}" \
  --arg adapter_digest "${adapter_digest}" \
  --arg wire_profile_digest "${wire_profile_digest}" \
  --arg runtime_profile_digest "${runtime_profile_digest}" \
  --arg optimization_profile_digest "${optimization_profile_digest}" \
  --arg triton_server_version "${triton_version}" \
  --arg repo_identity "${repo_identity}" \
  --arg closure_digest "${closure_digest}" \
  --arg operator_partition_digest "${empty_operator_partition_digest}" \
  --arg envelope_digest "${envelope_digest}" '{
    schema_version:"inference-startup-envelope/v1",
    model_control_incarnation_id:"incarnation-oci-smoke-0001",
    operation_id:"op-oci-smoke-0001",
    kind:"binding",
    logical_pool_id:"pool-oci-smoke-0001",
    pool_generation:1,
    availability_profile_id:"availability-single/v1",
    deployment_tier:"acceptance",
    model_revision_digest:$model_revision_digest,
    feature_contract_digest:$feature_digest,
    label_contract_digest:$label_digest,
    output_adapter_digest:$adapter_digest,
    inference_wire_profile_digest:$wire_profile_digest,
    runtime_profile_id:"model-runtime-central-cpu/v1",
    runtime_profile_digest:$runtime_profile_digest,
    optimization_profile_digest:$optimization_profile_digest,
    triton_server_version:$triton_server_version,
    repository_snapshot:{identity:$repo_identity, closure_digest:$closure_digest},
    instance_group:{kind:"KIND_CPU", count:1, operator_partition_digest:$operator_partition_digest},
    proposed_binding_generation:1,
    issued_at_unix_ms:0,
    expires_at_unix_ms:9999999999999,
    trace_id:"trace-oci-smoke-0001",
    envelope_digest:$envelope_digest
  }' >"${temporary_root}/config/envelope.json"

# Gateway config: non-loopback Triton endpoint => triton_tls_* (all four) is
# required and present. client_san_allowlist is mandatory and non-empty.
jq -n \
  --arg envelope_path "/run/inference-config/envelope.json" \
  --arg repo_path "/run/inference-modelrepo" \
  --arg ca_path "/run/inference-secrets/ca.pem" \
  --arg cert_path "/run/inference-secrets/inference-server.pem" \
  --arg key_path "/run/inference-secrets/inference-server.key" \
  --arg triton_ca_path "/run/inference-triton-tls/ca.pem" \
  --arg triton_cert_path "/run/inference-triton-tls/triton-client.pem" \
  --arg triton_key_path "/run/inference-triton-tls/triton-client.key" '{
    startup_envelope_path:$envelope_path,
    model_repository_path:$repo_path,
    triton_endpoint:"triton:8001",
    triton_tls_ca_path:$triton_ca_path,
    triton_tls_cert_path:$triton_cert_path,
    triton_tls_key_path:$triton_key_path,
    triton_tls_server_name:"triton",
    gateway_listen:"0.0.0.0:7443",
    tls_ca_path:$ca_path,
    tls_cert_path:$cert_path,
    tls_key_path:$key_path,
    client_san_allowlist:["masi-edge.test"],
    max_records_per_batch:256,
    max_request_bytes:4194304,
    max_response_bytes:4194304,
    max_in_flight:64,
    request_deadline_ms:2000,
    runtime_profile:"model-runtime-central-cpu/v1",
    availability_profile:"availability-single/v1",
    drain_ms:500
  }' >"${temporary_root}/config/gateway.json"
config_digest="sha256:$(sha256sum "${temporary_root}/config/gateway.json" | awk '{print $1}')"

chown -R 65532:65532 \
  "${temporary_root}/secrets" "${temporary_root}/config" "${temporary_root}/data" \
  "${temporary_root}/triton-server" "${temporary_root}/triton-client"

timeout --signal=TERM --kill-after=10s 60s docker run --detach --name "${container_name}" --network "${smoke_network}" --read-only \
  --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 \
  --memory 512m --cpus 1 --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --publish 127.0.0.1:0:7443 \
  --volume "${temporary_root}/config:/run/inference-config:ro" \
  --volume "${temporary_root}/secrets:/run/inference-secrets:ro" \
  --volume "${temporary_root}/triton-client:/run/inference-triton-tls:ro" \
  --volume "${fixture_repo}:/run/inference-modelrepo:ro" \
  --volume "${temporary_root}/data:/var/lib/masi-inference" \
  "${image_ref}" /run/inference-config/gateway.json >/dev/null

host_port=""
for _port_attempt in $(seq 1 50); do
  host_port="$(docker port "${container_name}" 7443/tcp 2>/dev/null | awk -F: 'NR == 1 {print $NF}' || true)"
  if [[ "${host_port}" =~ ^[0-9]+$ ]]; then
    break
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "${container_name}")" != "true" ]]; then
    break
  fi
  sleep 0.2
done
if [[ ! "${host_port}" =~ ^[0-9]+$ ]]; then
  docker logs "${container_name}" >"${evidence_dir}/inference-oci.log" 2>&1 || true
  docker inspect "${container_name}" >"${evidence_dir}/inference-oci-inspect.json" 2>&1 || true
  echo "OCI container did not publish a usable 7443/tcp host port" >&2
  exit 1
fi

probe_succeeded=false
for _attempt in $(seq 1 80); do
  if [[ -x "${probe_bin}" ]] && "${probe_bin}" \
    --endpoint="127.0.0.1:${host_port}" \
    --tls-ca="${temporary_root}/probe/ca.pem" \
    --tls-cert="${temporary_root}/probe/inference-probe.pem" \
    --tls-key="${temporary_root}/probe/inference-probe.key" \
    --logical-pool-id="pool-oci-smoke-0001" \
    --pool-generation=1 \
    --binding-generation=1 \
    --model-control-incarnation-id="incarnation-oci-smoke-0001" \
    --expected-startup-envelope-digest="${envelope_digest}" >"${temporary_root}/probe.json" 2>&1; then
    probe_succeeded=true
    break
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "${container_name}")" != "true" ]]; then
    break
  fi
  sleep 0.2
done

docker logs "${container_name}" >"${evidence_dir}/inference-oci.log" 2>&1
cp -- "${temporary_root}/probe.json" "${evidence_dir}/oci-probe.json"
if [[ "${probe_succeeded}" != "true" ]]; then
  echo "OCI mTLS probe failed" >&2
  exit 1
fi

if (( hold_seconds > 0 )); then
  mkdir -p -- "${runtime_export_dir}/gateway-tls" "${runtime_export_dir}/triton-tls" \
    "${runtime_export_dir}/config"
  cp -- "${temporary_root}/probe/ca.pem" "${runtime_export_dir}/gateway-tls/ca.pem"
  cp -- "${temporary_root}/probe/inference-probe.pem" \
    "${runtime_export_dir}/gateway-tls/edge-client.pem"
  cp -- "${temporary_root}/probe/inference-probe.key" \
    "${runtime_export_dir}/gateway-tls/edge-client.key"
  cp -- "${temporary_root}/triton-client/ca.pem" "${runtime_export_dir}/triton-tls/ca.pem"
  cp -- "${temporary_root}/triton-client/triton-client.pem" \
    "${runtime_export_dir}/triton-tls/client.pem"
  cp -- "${temporary_root}/triton-client/triton-client.key" \
    "${runtime_export_dir}/triton-tls/client.key"
  cp -- "${temporary_root}/config/envelope.json" "${runtime_export_dir}/config/envelope.json"
  cp -- "${temporary_root}/config/gateway.json" "${runtime_export_dir}/config/gateway.json"
  cp -- "${temporary_root}/probe.json" "${runtime_export_dir}/binding-readback.json"
  chmod 0600 "${runtime_export_dir}/gateway-tls/edge-client.key" \
    "${runtime_export_dir}/triton-tls/client.key"
  jq -e '.schema_version == "inference-committed-binding/v1"' \
    "${temporary_root}/probe.json" >/dev/null
  readback="$(jq -c . "${temporary_root}/probe.json")"
  runtime_manifest_tmp="${runtime_export_dir}/.runtime.json.${$}"
  jq -n \
    --arg gateway_endpoint "https://127.0.0.1:${host_port}" \
    --arg triton_host_endpoint "127.0.0.1:${triton_host_port}" \
    --arg gateway_container "${container_name}" \
    --arg triton_container "${triton_container}" \
    --arg network "${smoke_network}" \
    --arg image_ref "${image_ref}" \
    --arg image_manifest_digest "${image_manifest_digest}" \
    --arg config_digest "${config_digest}" \
    --arg startup_envelope_digest "${envelope_digest}" \
    --arg gateway_ca_path "${runtime_export_dir}/gateway-tls/ca.pem" \
    --arg gateway_client_cert_path "${runtime_export_dir}/gateway-tls/edge-client.pem" \
    --arg gateway_client_key_path "${runtime_export_dir}/gateway-tls/edge-client.key" \
    --arg triton_ca_path "${runtime_export_dir}/triton-tls/ca.pem" \
    --arg triton_client_cert_path "${runtime_export_dir}/triton-tls/client.pem" \
    --arg triton_client_key_path "${runtime_export_dir}/triton-tls/client.key" \
    --arg envelope_path "${runtime_export_dir}/config/envelope.json" \
    --arg gateway_config_path "${runtime_export_dir}/config/gateway.json" \
    --arg binding_readback_path "${runtime_export_dir}/binding-readback.json" \
    --arg consumer_done_path "${runtime_export_dir}/consumer.done" \
    --argjson readback "${readback}" '{
      schema_version:"central-inference-oci-runtime-export/v1",state:"READY",
      gateway_endpoint:$gateway_endpoint,triton_host_endpoint:$triton_host_endpoint,
      triton_internal_endpoint:"triton:8001",gateway_container:$gateway_container,
      triton_container:$triton_container,network:$network,image_ref:$image_ref,
      image_manifest_digest:$image_manifest_digest,config_digest:$config_digest,
      startup_envelope_digest:$startup_envelope_digest,
      tls:{server_name:"inference.test",client_san:"masi-edge.test",ca_path:$gateway_ca_path,
        client_cert_path:$gateway_client_cert_path,client_key_path:$gateway_client_key_path},
      triton_tls:{server_name:"triton",ca_path:$triton_ca_path,
        client_cert_path:$triton_client_cert_path,client_key_path:$triton_client_key_path},
      artifacts:{envelope_path:$envelope_path,gateway_config_path:$gateway_config_path,
        binding_readback_path:$binding_readback_path},consumer_done_path:$consumer_done_path,
      binding_readback:$readback
    }' >"${runtime_manifest_tmp}"
  mv -- "${runtime_manifest_tmp}" "${runtime_export_dir}/runtime.json"
  for _hold_elapsed in $(seq 1 "${hold_seconds}"); do
    if [[ -f "${runtime_export_dir}/consumer.done" ]]; then
      break
    fi
    if [[ "$(docker inspect --format '{{.State.Running}}' "${container_name}")" != "true" \
      || "$(docker inspect --format '{{.State.Running}}' "${triton_container}")" != "true" ]]; then
      echo "exported Central runtime exited during bounded hold" >&2
      exit 1
    fi
    sleep 1
  done
fi

container_read_only="$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "${container_name}")"
container_cap_drop="$(docker inspect --format '{{json .HostConfig.CapDrop}}' "${container_name}")"
container_security="$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "${container_name}")"
container_pids_limit="$(docker inspect --format '{{.HostConfig.PidsLimit}}' "${container_name}")"
container_memory_bytes="$(docker inspect --format '{{.HostConfig.Memory}}' "${container_name}")"
container_nano_cpus="$(docker inspect --format '{{.HostConfig.NanoCpus}}' "${container_name}")"
container_tmpfs="$(docker inspect --format '{{json .HostConfig.Tmpfs}}' "${container_name}")"
if [[ "${container_read_only}" != "true" \
  || "$(jq -r 'index("ALL") != null' <<<"${container_cap_drop}")" != "true" \
  || "$(jq -r 'index("no-new-privileges:true") != null' <<<"${container_security}")" != "true" \
  || "${container_pids_limit}" != "128" \
  || "${container_memory_bytes}" != "536870912" \
  || "${container_nano_cpus}" != "1000000000" \
  || "$(jq -r 'has("/tmp")' <<<"${container_tmpfs}")" != "true" ]]; then
  echo "OCI container runtime hardening or resource limits drifted" >&2
  exit 1
fi

image_archive="${evidence_dir}/inference-image.tar"
timeout --signal=TERM --kill-after=30s 600s docker image save --output "${image_archive}" "${image_ref}"
image_archive_digest="sha256:$(sha256sum "${image_archive}" | awk '{print $1}')"
archive_config_path="$(tar -xOf "${image_archive}" manifest.json | jq -er '.[0].Config')"
archive_config_hex="$(basename -- "${archive_config_path}" .json)"
[[ "${archive_config_hex}" =~ ^[0-9a-f]{64}$ ]] \
  || { echo "OCI archive config path is not digest-addressed" >&2; exit 1; }
observed_config_hex="$(tar -xOf "${image_archive}" "${archive_config_path}" | sha256sum | awk '{print $1}')"
[[ "${observed_config_hex}" == "${archive_config_hex}" ]] \
  || { echo "OCI archive config bytes do not match their digest path" >&2; exit 1; }
image_config_digest="sha256:${archive_config_hex}"
image_archive_bytes="$(stat -c %s "${image_archive}")"
jq -n --arg archive "inference-image.tar" --arg digest "${image_archive_digest}" \
  --argjson bytes "${image_archive_bytes}" \
  '{archive:$archive, digest:$digest, bytes:$bytes}' \
  >"${evidence_dir}/image-archive-manifest.json"

timeout --signal=TERM --kill-after=10s 30s docker stop --time 10 "${container_name}" >/dev/null
container_exit_code="$(docker inspect --format '{{.State.ExitCode}}' "${container_name}")"
if [[ "${container_exit_code}" != "0" ]]; then
  echo "OCI container did not exit 0 on SIGTERM (got ${container_exit_code})" >&2
  exit 1
fi

evidence_result="PASS"
evidence_qualification="QUALIFIED"
qualification_reason="CLEAN_SOURCE_SNAPSHOT"
if [[ "${working_tree_dirty}" == true ]]; then
  evidence_result="HOLD"
  evidence_qualification="NOT_QUALIFIED"
  qualification_reason="DIRTY_WORKTREE_NOT_RELEASE_BASELINE"
fi
generated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

jq -n \
  --arg image_ref "${image_ref}" \
  --arg image_manifest_digest "${image_manifest_digest}" \
  --arg image_config_digest "${image_config_digest}" \
  --arg gateway_binary_digest "${gateway_binary_digest}" \
  --arg probe_binary_digest "${probe_binary_digest}" \
  --arg image_archive_digest "${image_archive_digest}" \
  --arg triton_image "${triton_image}" \
  --arg triton_server_version "${triton_version}" \
  --arg probe_certificate_digest "${probe_certificate_digest}" \
  --arg image_user "${image_user}" \
  --arg source_revision "${source_revision}" \
  --arg source_tree_digest "${source_tree_digest}" \
  --arg working_tree_status_digest "${working_tree_status_digest}" \
  --arg config_digest "${config_digest}" \
  --arg result "${evidence_result}" \
  --arg qualification "${evidence_qualification}" \
  --arg qualification_reason "${qualification_reason}" \
  --arg generated_at "${generated_at}" \
  --argjson working_tree_dirty "${working_tree_dirty}" \
  --argjson read_only "${container_read_only}" \
  --argjson cap_drop "${container_cap_drop}" \
  --argjson security_opt "${container_security}" \
  --argjson exit_code "${container_exit_code}" \
  --argjson pids_limit "${container_pids_limit}" \
  --argjson memory_bytes "${container_memory_bytes}" \
  --argjson nano_cpus "${container_nano_cpus}" \
  --argjson tmpfs "${container_tmpfs}" '{
    schema_version: "central-inference-oci-startup-evidence/v1",
    test_id: "TEST-INF-OCI-001",
    requirement_ids: ["MOD-INF-001", "TEST-003", "TEST-010", "TEST-REAL-E2E-001"],
    level: "MODULE",
    applicability: "APPLICABLE",
    generated_at: $generated_at,
    result: $result,
    qualification: $qualification,
    qualification_reason: $qualification_reason,
    scope: "Real OCI image startup against a real mTLS Triton sidecar on an isolated network, public-boundary mTLS liveness, and graceful SIGTERM",
    overall_module_complete: false,
    image_ref: $image_ref,
    image_manifest_digest: $image_manifest_digest,
    image_config_digest: $image_config_digest,
    gateway_binary_digest: $gateway_binary_digest,
    probe_binary_digest: $probe_binary_digest,
    image_archive_digest: $image_archive_digest,
    triton_image: $triton_image,
    triton_server_version: $triton_server_version,
    probe_certificate_digest: $probe_certificate_digest,
    image_user: $image_user,
    source_revision: $source_revision,
    source_tree_digest: $source_tree_digest,
    working_tree_dirty: $working_tree_dirty,
    working_tree_status_digest: $working_tree_status_digest,
    config_digest: $config_digest,
    read_only_rootfs: $read_only,
    cap_drop: $cap_drop,
    security_options: $security_opt,
    pids_limit: $pids_limit,
    memory_bytes: $memory_bytes,
    nano_cpus: $nano_cpus,
    tmpfs: $tmpfs,
    graceful_shutdown_exit_code: $exit_code,
    checks: {
      image_source_labels: "PASS",
      non_root_user: "PASS",
      read_only_rootfs: "PASS",
      capability_drop_all: "PASS",
      no_new_privileges: "PASS",
      bounded_pids_memory_cpu_tmpfs: "PASS",
      real_triton_sidecar_mtls: "PASS",
      real_triton_startup: "PASS",
      public_mtls_probe: "PASS",
      graceful_sigterm: "PASS"
    }
  }' >"${evidence_dir}/oci-smoke-evidence.json"

python3 "${script_dir}/validate-evidence.py" \
  --schema "${repo_root}/contracts/evidence/central-inference-oci/v1/schema.json" \
  --document "${evidence_dir}/oci-smoke-evidence.json" >/dev/null

if [[ "${evidence_result}" == "HOLD" ]]; then
  exit 2
fi
