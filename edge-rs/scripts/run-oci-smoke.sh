#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"
edge_root="${repo_root}/edge-rs"
evidence_dir="${MASI_EDGE_EVIDENCE_DIR:-${edge_root}/evidence/oci-smoke}"
image_ref="${MASI_EDGE_IMAGE_REF:-masi-edge:module-smoke}"
source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
temporary_root="$(mktemp -d /tmp/masi-edge-oci-smoke.XXXXXX)"
container_name="masi-edge-oci-smoke-${$}"

cleanup() {
  docker rm --force "${container_name}" >/dev/null 2>&1 || true
  if [[ "${temporary_root}" == /tmp/masi-edge-oci-smoke.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

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

mkdir -p -- "${evidence_dir}" "${temporary_root}/secrets" "${temporary_root}/probe" \
  "${temporary_root}/config" "${temporary_root}/data" "${temporary_root}/vendor"
for output in edge-image.tar image-archive-manifest.json image-config.json \
  oci-archive-inspection.json oci-probe.json oci-smoke-evidence.json; do
  [[ ! -e "${evidence_dir}/${output}" && ! -L "${evidence_dir}/${output}" ]] \
    || { echo "OCI evidence output already exists: ${output}" >&2; exit 1; }
done

generate_ca() {
  openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 2 \
    -subj "/CN=MASI Edge module smoke CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "${temporary_root}/ca.key" \
    -out "${temporary_root}/ca.pem" >/dev/null 2>&1
}

generate_leaf() {
  local output_dir="$1"
  local name="$2"
  local serial="$3"
  local usage="$4"
  local san="${5:-}"
  local request=(openssl req -new -newkey rsa:3072 -nodes -sha256
    -subj "/CN=${name}" -keyout "${output_dir}/${name}.key"
    -out "${output_dir}/${name}.csr" -addext "extendedKeyUsage=${usage}")
  if [[ -n "${san}" ]]; then
    request+=(-addext "subjectAltName=${san}")
  fi
  "${request[@]}" >/dev/null 2>&1
  openssl x509 -req -sha256 -days 2 -set_serial "${serial}" \
    -in "${output_dir}/${name}.csr" \
    -CA "${temporary_root}/ca.pem" -CAkey "${temporary_root}/ca.key" \
    -copy_extensions copyall -out "${output_dir}/${name}.pem" >/dev/null 2>&1
  chmod 0600 "${output_dir}/${name}.key"
  rm -- "${output_dir}/${name}.csr"
}

generate_ca
cp -- "${temporary_root}/ca.pem" "${temporary_root}/secrets/ca.pem"
cp -- "${temporary_root}/ca.pem" "${temporary_root}/probe/ca.pem"
generate_leaf "${temporary_root}/secrets" edge-server 101 serverAuth DNS:edge.test
generate_leaf "${temporary_root}/secrets" p4-client 102 clientAuth
generate_leaf "${temporary_root}/secrets" inference-client 103 clientAuth
generate_leaf "${temporary_root}/secrets" control-client 104 clientAuth
generate_leaf "${temporary_root}/probe" edge-probe 105 clientAuth

probe_certificate_digest="sha256:$(openssl x509 \
  -in "${temporary_root}/probe/edge-probe.pem" -outform DER | sha256sum | awk '{print $1}')"

jq -n --arg probe_digest "${probe_certificate_digest}" '{
  schema_version: "edge-config/v1",
  edge_instance_id: "edge-oci-module-smoke",
  listen_address: "0.0.0.0:7444",
  deployment_tier: "module-test",
  data_dir: "/var/lib/masi-edge",
  server_tls: {
    certificate_path: "/run/edge-secrets/edge-server.pem",
    private_key_path: "/run/edge-secrets/edge-server.key",
    client_ca_path: "/run/edge-secrets/ca.pem",
    identity_ref: "edge-server",
    allowed_client_certificate_sha256: [$probe_digest]
  },
  control_sink: {
    endpoint: "https://127.0.0.1:7445",
    tls: {
      ca_path: "/run/edge-secrets/ca.pem",
      certificate_path: "/run/edge-secrets/control-client.pem",
      private_key_path: "/run/edge-secrets/control-client.key",
      server_name: "control.test",
      identity_ref: "control-client"
    }
  },
  client_identities: {
    "p4-client": {
      ca_path: "/run/edge-secrets/ca.pem",
      certificate_path: "/run/edge-secrets/p4-client.pem",
      private_key_path: "/run/edge-secrets/p4-client.key",
      server_name: "p4.test",
      identity_ref: "p4-client"
    },
    "inference-client": {
      ca_path: "/run/edge-secrets/ca.pem",
      certificate_path: "/run/edge-secrets/inference-client.pem",
      private_key_path: "/run/edge-secrets/inference-client.key",
      server_name: "inference.test",
      identity_ref: "inference-client"
    }
  },
  endpoint_policy: {
    allowed_management_cidrs: [],
    module_test_loopback_allowlist: ["127.0.0.1"],
    resolution_deadline_ms: 1000
  },
  limits: {
    max_targets: 1,
    actor_high_queue: 32,
    p4_stream_request_queue: 32,
    p4_stream_response_queue: 256,
    pending_digest_ack_queue: 256,
    p4_updates_per_write: 256,
    p4_entities_per_read: 256,
    p4_read_response_bytes: 1048576,
    p4_read_response_entities: 4096,
    p4_connect_deadline_ms: 2000,
    p4_arbitration_deadline_ms: 2000,
    p4_rpc_deadline_ms: 2000,
    wal_record_bytes: 4194304,
    wal_segment_bytes: 4195000,
    wal_max_age_seconds: 86400,
    source_wal_bytes: 32000000,
    source_wal_records: 20000,
    input_wal_bytes: 32000000,
    input_wal_records: 20000,
    pending_input_identities: 20000,
    result_wal_bytes: 32000000,
    result_wal_records: 20000,
    journal_bytes: 32000000,
    journal_records: 20000,
    route_journal_bytes: 32000000,
    route_journal_records: 4096,
    telemetry_poll_interval_ms: 100,
    window_duration_ms: 100,
    allowed_lateness_ms: 0,
    idle_source_timeout_ms: 1000,
    max_open_windows: 8,
    inference_batch_records: 32,
    inference_message_bytes: 1048576,
    inference_deadline_ms: 1000,
    inference_max_attempts: 3,
    control_batch_records: 32,
    control_message_bytes: 1048576,
    control_deadline_ms: 1000,
    observation_interval_ms: 1000,
    observation_batches: 4,
    baseline_rules: 4096,
    overlay_rules: 1024,
    preflight_validity_ms: 30000,
    preflight_deadline_ms: 5000
  },
  telemetry_source_profile_digest: ("sha256:" + ("a" * 64)),
  feature_profile_digest: ("sha256:" + ("b" * 64)),
  log_filter: "info,masi_edge=debug"
}' >"${temporary_root}/config/edge.json"

config_digest="sha256:$(sha256sum "${temporary_root}/config/edge.json" | awk '{print $1}')"
working_tree_dirty=false
working_tree_status="$(git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)"
if [[ -n "${working_tree_status}" ]]; then
  working_tree_dirty=true
fi
printf '%s\n' "${working_tree_status}" >"${evidence_dir}/working-tree-status.txt"
working_tree_status_digest="sha256:$(sha256sum "${evidence_dir}/working-tree-status.txt" | awk '{print $1}')"
source_archive="${temporary_root}/source-tree.tar"
tar --sort=name --mtime=@1786406400 --owner=0 --group=0 --numeric-owner \
  --exclude='edge-rs/target' --exclude='edge-rs/evidence' \
  --exclude='**/node_modules' --exclude='**/__pycache__' --exclude='*.pyc' \
  -cf "${source_archive}" -C "${repo_root}" edge-rs contracts
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
if [[ -n "${MASI_EDGE_EXPECTED_SOURCE_TREE_DIGEST:-}" \
  && "${source_tree_digest}" != "${MASI_EDGE_EXPECTED_SOURCE_TREE_DIGEST}" ]]; then
  echo "OCI source tree changed after module gate snapshot" >&2
  exit 1
fi

CARGO_NET_OFFLINE=true cargo vendor --locked --versioned-dirs \
  --manifest-path "${edge_root}/Cargo.toml" "${temporary_root}/vendor" \
  >"${evidence_dir}/cargo-vendor-config.toml" \
  2>"${evidence_dir}/cargo-vendor.log"
docker build --platform linux/amd64 --network none --provenance=false --sbom=false \
  --build-context "cargo_vendor=${temporary_root}/vendor" \
  --file "${edge_root}/Dockerfile" \
  --build-arg "SOURCE_REVISION=${source_revision}" \
  --build-arg "SOURCE_TREE_DIGEST=${source_tree_digest}" \
  --tag "${image_ref}" "${repo_root}"
cargo build --manifest-path "${edge_root}/Cargo.toml" --locked --release --bin masi-edge-probe

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

chown -R 65532:65532 \
  "${temporary_root}/secrets" "${temporary_root}/config" "${temporary_root}/data"

docker run --detach --name "${container_name}" --read-only \
  --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 \
  --memory 256m --cpus 1 --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --publish 127.0.0.1:0:7444 \
  --volume "${temporary_root}/config:/run/edge-config:ro" \
  --volume "${temporary_root}/secrets:/run/edge-secrets:ro" \
  --volume "${temporary_root}/data:/var/lib/masi-edge" \
  "${image_ref}" --config /run/edge-config/edge.json >/dev/null

host_port=""
for _port_attempt in $(seq 1 50); do
  host_port="$(docker port "${container_name}" 7444/tcp 2>/dev/null | awk -F: 'NR == 1 {print $NF}' || true)"
  if [[ "${host_port}" =~ ^[0-9]+$ ]]; then
    break
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "${container_name}")" != "true" ]]; then
    break
  fi
  sleep 0.1
done
if [[ ! "${host_port}" =~ ^[0-9]+$ ]]; then
  docker logs "${container_name}" >"${evidence_dir}/edge-oci.log" 2>&1 || true
  if [[ "$(docker inspect --format '{{.State.Running}}' "${container_name}")" == "true" ]]; then
    echo "OCI container did not publish a usable 7444/tcp host port" >&2
  else
    echo "OCI container exited before publishing 7444/tcp; inspect edge-oci.log" >&2
  fi
  exit 1
fi
probe_succeeded=false
for _attempt in $(seq 1 50); do
  if "${edge_root}/target/release/masi-edge-probe" \
    --endpoint "https://127.0.0.1:${host_port}" \
    --server-name edge.test \
    --ca "${temporary_root}/probe/ca.pem" \
    --certificate "${temporary_root}/probe/edge-probe.pem" \
    --private-key "${temporary_root}/probe/edge-probe.key" \
    --expected-config-digest "${config_digest}" \
    --expected-edge-instance-id edge-oci-module-smoke \
    --deadline-ms 1000 >"${temporary_root}/probe.json"; then
    probe_succeeded=true
    break
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "${container_name}")" != "true" ]]; then
    break
  fi
  sleep 0.1
done

docker logs "${container_name}" >"${evidence_dir}/edge-oci.log" 2>&1
cp -- "${temporary_root}/probe.json" "${evidence_dir}/oci-probe.json"
if [[ "${probe_succeeded}" != "true" ]]; then
  exit 1
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
  || "${container_memory_bytes}" != "268435456" \
  || "${container_nano_cpus}" != "1000000000" \
  || "$(jq -r 'has("/tmp")' <<<"${container_tmpfs}")" != "true" ]]; then
  echo "OCI container runtime hardening or resource limits drifted" >&2
  exit 1
fi
image_archive="${evidence_dir}/edge-image.tar"
docker image save --output "${image_archive}" "${image_ref}"
python3 "${script_dir}/inspect-oci-archive.py" \
  --archive "${image_archive}" --image-ref "${image_ref}" \
  --index-output "${evidence_dir}/image-archive-manifest.json" \
  --config-output "${evidence_dir}/image-config.json" \
  --summary-output "${evidence_dir}/oci-archive-inspection.json"
python3 "${script_dir}/test-oci-archive-inspector.py" \
  --repo "${repo_root}" --archive "${image_archive}" --image-ref "${image_ref}"
image_config_digest="$(jq -er '.config_digest' "${evidence_dir}/oci-archive-inspection.json")"
archive_binary_digest="$(jq -er '.binary_digest' "${evidence_dir}/oci-archive-inspection.json")"
archive_manifest_digest="$(jq -er '.manifest_digest' "${evidence_dir}/oci-archive-inspection.json")"
[[ "$(jq -er '.attestation_count' "${evidence_dir}/oci-archive-inspection.json")" == "0" ]] \
  || { echo "OCI archive contains an undeclared embedded attestation" >&2; exit 1; }
image_repo_digests="$(docker image inspect --format '{{json .RepoDigests}}' "${image_ref}")"
image_repository="$(normalized_repository "${image_ref}")"
observed_repo_manifest_digest="$(jq -er --arg prefix "${image_repository}@" \
  '[.[] | select(startswith($prefix))] | if length == 1 then .[0] | split("@")[-1] else error("image has no unique exact-repository RepoDigest") end' \
  <<<"${image_repo_digests}")"
[[ "${observed_repo_manifest_digest}" == "${archive_manifest_digest}" ]] \
  || { echo "OCI archive manifest digest disagrees with exact RepoDigest" >&2; exit 1; }
image_manifest_digest="${archive_manifest_digest}"
docker cp "${container_name}:/usr/local/bin/masi-edge" "${temporary_root}/masi-edge.image.bin"
image_binary_digest="sha256:$(sha256sum "${temporary_root}/masi-edge.image.bin" | awk '{print $1}')"
if [[ "${image_binary_digest}" != "${archive_binary_digest}" ]]; then
  echo "running container binary disagrees with its inspected image archive" >&2
  exit 1
fi
docker stop --time 10 "${container_name}" >/dev/null
container_exit_code="$(docker inspect --format '{{.State.ExitCode}}' "${container_name}")"
if [[ "${container_exit_code}" != "0" ]]; then
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
  --arg image_binary_digest "${image_binary_digest}" \
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
  --argjson tmpfs "${container_tmpfs}" \
  --slurpfile probe "${evidence_dir}/oci-probe.json" \
  --slurpfile archive "${evidence_dir}/oci-archive-inspection.json" '{
    schema_version: "edge-oci-startup-evidence/v1",
    test_id: "TEST-EDGE-OCI-001",
    requirement_ids: ["MOD-EDGE-001", "TEST-003", "TEST-010", "TEST-REAL-E2E-001"],
    level: "MODULE",
    applicability: "APPLICABLE",
    generated_at: $generated_at,
    result: $result,
    qualification: $qualification,
    qualification_reason: $qualification_reason,
    scope: "OCI startup, mTLS public-boundary liveness, and graceful SIGTERM only",
    overall_module_complete: false,
    image_ref: $image_ref,
    image_manifest_digest: $image_manifest_digest,
    image_config_digest: $image_config_digest,
    image_binary_digest: $image_binary_digest,
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
    probe: $probe[0],
    archive: $archive[0],
    checks: {
      image_source_labels: "PASS",
      single_linux_amd64_image_archive: "PASS",
      archive_content_addressing: "PASS",
      non_root_user: "PASS",
      read_only_rootfs: "PASS",
      capability_drop_all: "PASS",
      no_new_privileges: "PASS",
      bounded_pids_memory_cpu_tmpfs: "PASS",
      public_mtls_probe: "PASS",
      graceful_sigterm: "PASS"
    }
  }' >"${evidence_dir}/oci-smoke-evidence.json"

python3 "${script_dir}/validate-edge-evidence.py" \
  --repo "${repo_root}" --evidence "${evidence_dir}/oci-smoke-evidence.json"

if [[ "${evidence_result}" == "HOLD" ]]; then
  exit 2
fi
