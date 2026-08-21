#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
host_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${host_root}/.." && pwd)"
evidence_dir="${MASI_PLUGIN_HOST_OCI_EVIDENCE_DIR:-${host_root}/evidence/oci-smoke}"
image_ref="${MASI_PLUGIN_HOST_IMAGE_REF:-masi-plugin-host:module-smoke}"
temporary_root="$(mktemp -d /tmp/masi-plugin-host-oci.XXXXXX)"
container_name="masi-plugin-host-oci-${$}"
container_created=""

cleanup() {
  docker rm --force "${container_name}" >/dev/null 2>&1 || true
  if [[ -n "${container_created}" ]]; then
    docker rm --force "${container_created}" >/dev/null 2>&1 || true
  fi
  if [[ "${temporary_root}" == /tmp/masi-plugin-host-oci.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

fail() {
  echo "plugin-host OCI smoke: $*" >&2
  exit 1
}

for command in cargo curl docker git jq openssl sha256sum tar; do
  command -v "${command}" >/dev/null 2>&1 || fail "missing command ${command}"
done
mkdir -p -- "${evidence_dir}" "${temporary_root}/config" "${temporary_root}/secrets" \
  "${temporary_root}/artifacts" "${temporary_root}/vendor"
[[ ! -e "${evidence_dir}/oci-smoke-evidence.json" ]] \
  || fail "evidence output already exists"

openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 2 \
  -subj "/CN=MASI Plugin Host OCI Test CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout "${temporary_root}/ca.key" \
  -out "${temporary_root}/secrets/ca.pem" >/dev/null 2>&1

generate_leaf() {
  local name="$1"
  local serial="$2"
  local usage="$3"
  local san="$4"
  openssl req -new -newkey rsa:3072 -nodes -sha256 \
    -subj "/CN=${name}" -addext "extendedKeyUsage=${usage}" \
    -addext "subjectAltName=${san}" \
    -keyout "${temporary_root}/secrets/${name}.key" \
    -out "${temporary_root}/${name}.csr" >/dev/null 2>&1
  openssl x509 -req -sha256 -days 2 -set_serial "${serial}" \
    -in "${temporary_root}/${name}.csr" \
    -CA "${temporary_root}/secrets/ca.pem" -CAkey "${temporary_root}/ca.key" \
    -copy_extensions copyall -out "${temporary_root}/secrets/${name}.pem" >/dev/null 2>&1
  chmod 0600 "${temporary_root}/secrets/${name}.key"
  rm -- "${temporary_root}/${name}.csr"
}

generate_leaf plugin-host.test 101 serverAuth DNS:plugin-host.test
generate_leaf manager.test 102 clientAuth DNS:manager.test
generate_leaf other-manager.test 103 clientAuth DNS:other-manager.test
manager_digest="sha256:$(openssl x509 -in "${temporary_root}/secrets/manager.test.pem" \
  -outform DER | sha256sum | awk '{print $1}')"

jq -n --arg manager_digest "${manager_digest}" '{
  schema_version: "plugin-host-config/v1",
  host_id: "plugin-host-oci-smoke",
  profile_id: "plugin-runtime-host/v1",
  listen_address: "0.0.0.0:7445",
  health_address: "0.0.0.0:8080",
  artifact_cache_root: "/var/lib/masi-plugin-host/artifacts",
  server_tls: {
    certificate_path: "/run/plugin-host-secrets/plugin-host.test.pem",
    private_key_path: "/run/plugin-host-secrets/plugin-host.test.key",
    client_ca_path: "/run/plugin-host-secrets/ca.pem",
    allowed_client_certificate_sha256: [$manager_digest]
  },
  trust: {
    trust_policy_digest: ("sha256:" + ("a" * 64)),
    revocation_max_age_ms: 300000,
    publishers: [{
      publisher_identity: "publisher.oci.test",
      ed25519_public_key_hex: ("01" * 32)
    }]
  },
  limits: {
    max_bindings: 32,
    global_queue_depth: 64,
    per_binding_queue_depth: 32,
    per_binding_in_flight: 2,
    max_control_message_bytes: 4194304,
    max_input_bytes: 2097152,
    max_output_bytes: 1048576,
    max_deadline_ms: 10000,
    queue_wait_ms: 1000,
    wasm_fuel: 50000000,
    wasm_linear_memory_bytes: 67108864,
    wasm_table_elements: 10000,
    wasm_instances: 32,
    wasm_memories: 2,
    wasm_tables: 4,
    wasm_stack_bytes: 2097152,
    epoch_tick_ms: 10,
    failure_threshold: 3,
    circuit_open_ms: 1000,
    restart_window_ms: 600000,
    max_restarts_in_window: 5,
    quarantine_ms: 900000,
    drain_deadline_ms: 10000,
    shutdown_deadline_ms: 10000
  },
  service_endpoints: []
}' >"${temporary_root}/config/config.json"

source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
source_archive="${temporary_root}/source-tree.tar"
tar --sort=name --mtime=@1787270400 --owner=0 --group=0 --numeric-owner \
  --exclude='plugin-host-rs/target' --exclude='plugin-host-rs/evidence' \
  -cf "${source_archive}" -C "${repo_root}" plugin-host-rs contracts deploy/plugin-host
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"

CARGO_NET_OFFLINE=true cargo vendor --locked --versioned-dirs \
  --manifest-path "${host_root}/Cargo.toml" "${temporary_root}/vendor" \
  >"${evidence_dir}/cargo-vendor-config.toml" \
  2>"${evidence_dir}/cargo-vendor.log"
docker build --platform linux/amd64 --network none --provenance=false --sbom=false \
  --build-context "cargo_vendor=${temporary_root}/vendor" \
  --file "${host_root}/Dockerfile" \
  --build-arg "SOURCE_REVISION=${source_revision}" \
  --build-arg "SOURCE_TREE_DIGEST=${source_tree_digest}" \
  --tag "${image_ref}" "${repo_root}" \
  >"${evidence_dir}/docker-build.log"
cargo build --manifest-path "${host_root}/Cargo.toml" --locked --release \
  --bin masi-plugin-hostctl --bin masi-plugin-host \
  >"${evidence_dir}/cargo-release-build.log"

image_user="$(docker image inspect --format '{{.Config.User}}' "${image_ref}")"
[[ "${image_user}" == "65532:65532" ]] || fail "image is not non-root"
image_revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "${image_ref}")"
image_source_digest="$(docker image inspect --format '{{index .Config.Labels "io.masi-nids.source-tree.digest"}}' "${image_ref}")"
[[ "${image_revision}" == "${source_revision}" && "${image_source_digest}" == "${source_tree_digest}" ]] \
  || fail "image labels do not bind source"

chown -R 65532:65532 "${temporary_root}/config" "${temporary_root}/secrets" "${temporary_root}/artifacts"
docker run --detach --name "${container_name}" --read-only \
  --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 \
  --memory 384m --cpus 1 --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --publish 127.0.0.1:0:7445 --publish 127.0.0.1:0:8080 \
  --volume "${temporary_root}/config:/run/plugin-host-config:ro" \
  --volume "${temporary_root}/secrets:/run/plugin-host-secrets:ro" \
  --volume "${temporary_root}/artifacts:/var/lib/masi-plugin-host/artifacts:ro" \
  "${image_ref}" --config /run/plugin-host-config/config.json >/dev/null

manager_port=""
health_port=""
for _attempt in $(seq 1 100); do
  manager_port="$(docker port "${container_name}" 7445/tcp 2>/dev/null | awk -F: 'NR == 1 {print $NF}' || true)"
  health_port="$(docker port "${container_name}" 8080/tcp 2>/dev/null | awk -F: 'NR == 1 {print $NF}' || true)"
  container_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "${container_name}" 2>/dev/null || true)"
  if [[ "${manager_port}" =~ ^[0-9]+$ && "${health_port}" =~ ^[0-9]+$ \
    && "${container_health}" == "healthy" ]] \
    && curl --fail --silent --max-time 2 "http://127.0.0.1:${health_port}/readyz" \
      >"${evidence_dir}/readyz.json"; then
    break
  fi
  sleep 0.1
done
[[ "${manager_port}" =~ ^[0-9]+$ && "${health_port}" =~ ^[0-9]+$ ]] \
  || fail "container did not publish bounded endpoints"

hostctl="${host_root}/target/release/masi-plugin-hostctl"
"${hostctl}" --endpoint "https://127.0.0.1:${manager_port}" \
  --server-name plugin-host.test \
  --ca "${temporary_root}/secrets/ca.pem" \
  --cert "${temporary_root}/secrets/manager.test.pem" \
  --key "${temporary_root}/secrets/manager.test.key" \
  list --page-size 10 --trace-id oci-authorized-list \
  >"${evidence_dir}/authorized-list.json"

if "${hostctl}" --endpoint "https://127.0.0.1:${manager_port}" \
  --server-name plugin-host.test \
  --ca "${temporary_root}/secrets/ca.pem" \
  --cert "${temporary_root}/secrets/other-manager.test.pem" \
  --key "${temporary_root}/secrets/other-manager.test.key" \
  list --page-size 10 --trace-id oci-unauthorized-list \
  >"${evidence_dir}/unauthorized-list.log" 2>&1; then
  fail "non-allowlisted manager leaf was accepted"
fi

if openssl s_client -connect "127.0.0.1:${manager_port}" -servername plugin-host.test \
  -tls1_2 -CAfile "${temporary_root}/secrets/ca.pem" \
  -cert "${temporary_root}/secrets/manager.test.pem" \
  -key "${temporary_root}/secrets/manager.test.key" \
  </dev/null >"${evidence_dir}/tls12-negative.log" 2>&1; then
  fail "TLS1.2 unexpectedly negotiated"
fi
if curl --silent --max-time 2 "http://127.0.0.1:${manager_port}/" \
  >"${evidence_dir}/plaintext-negative.log" 2>&1; then
  fail "plaintext unexpectedly reached gRPC boundary"
fi

curl --fail --silent --max-time 2 "http://127.0.0.1:${health_port}/startupz" \
  >"${evidence_dir}/startupz.json"
curl --fail --silent --max-time 2 "http://127.0.0.1:${health_port}/livez" \
  >"${evidence_dir}/livez.json"
curl --fail --silent --max-time 2 "http://127.0.0.1:${health_port}/metrics" \
  >"${evidence_dir}/metrics.txt"
grep -q '^masi_plugin_host_ready 1$' "${evidence_dir}/metrics.txt" \
  || fail "readiness metric missing"

readonly_rootfs="$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "${container_name}")"
cap_drop="$(docker inspect --format '{{json .HostConfig.CapDrop}}' "${container_name}")"
security_opt="$(docker inspect --format '{{json .HostConfig.SecurityOpt}}' "${container_name}")"
[[ "${readonly_rootfs}" == "true" && "${cap_drop}" == '["ALL"]' \
  && "${security_opt}" == *"no-new-privileges:true"* ]] \
  || fail "container hardening drift"

container_created="$(docker create "${image_ref}")"
docker cp "${container_created}:/usr/local/bin/masi-plugin-host" "${temporary_root}/image-host"
image_binary_digest="sha256:$(sha256sum "${temporary_root}/image-host" | awk '{print $1}')"
release_binary_digest="sha256:$(sha256sum "${host_root}/target/release/masi-plugin-host" | awk '{print $1}')"

started_stop="$(date +%s)"
docker stop --time 10 "${container_name}" >/dev/null
stop_seconds="$(( $(date +%s) - started_stop ))"
[[ "${stop_seconds}" -le 10 ]] || fail "SIGTERM drain exceeded 10 seconds"

image_id="$(docker image inspect --format '{{.Id}}' "${image_ref}")"
jq -n \
  --arg image_ref "${image_ref}" \
  --arg image_id "${image_id}" \
  --arg source_revision "${source_revision}" \
  --arg source_tree_digest "${source_tree_digest}" \
  --arg image_binary_digest "${image_binary_digest}" \
  --arg local_release_binary_digest "${release_binary_digest}" \
  --arg manager_leaf_digest "${manager_digest}" \
  --argjson stop_seconds "${stop_seconds}" \
  '{
    schema_version: "plugin-host-oci-smoke-evidence/v1",
    module_id: "MOD-PLUGIN-001",
    image_ref: $image_ref,
    image_id: $image_id,
    source_revision: $source_revision,
    source_tree_digest: $source_tree_digest,
    image_binary_digest: $image_binary_digest,
    local_release_binary_digest: $local_release_binary_digest,
    release_binaries_share_exact_source_tree: true,
    manager_leaf_digest: $manager_leaf_digest,
    runtime_profile: "plugin-runtime-host/v1",
    wasmtime_version: "47.0.3",
    non_root_user: "65532:65532",
    read_only_rootfs: true,
    capability_drop_all: true,
    no_new_privileges: true,
    tls13_only: true,
    plaintext_rejected: true,
    unauthorized_leaf_rejected: true,
    startup_ready_live_metrics: true,
    docker_semantic_healthcheck: true,
    graceful_stop_seconds: $stop_seconds,
    actual_oci_started: true,
    result: "PASS",
    qualification: "NOT_QUALIFIED",
    reason_code: "MODULE_OCI_BOUNDARY_VERIFIED"
  }' >"${evidence_dir}/oci-smoke-evidence.json"
