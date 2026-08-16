#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"
inf_root="${repo_root}/infer-cpp"
evidence_dir="${MASI_INF_EVIDENCE_DIR:-${inf_root}/evidence/oci-smoke}"
image_ref="${MASI_INF_IMAGE_REF:-masi-inference:module-smoke}"
source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
temporary_root="$(mktemp -d /tmp/masi-inf-oci-smoke.XXXXXX)"
container_name="masi-inf-oci-smoke-${$}"

cleanup() {
  docker rm --force "${container_name}" >/dev/null 2>&1 || true
  if [[ "${temporary_root}" == /tmp/masi-inf-oci-smoke.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

mkdir -p -- "${evidence_dir}" "${temporary_root}/secrets" "${temporary_root}/probe" \
  "${temporary_root}/config" "${temporary_root}/data" "${temporary_root}/modelrepo"
for output in inference-image.tar image-archive-manifest.json image-config.json \
  oci-probe.json oci-smoke-evidence.json; do
  [[ ! -e "${evidence_dir}/${output}" && ! -L "${evidence_dir}/${output}" ]] \
    || { echo "OCI evidence output already exists: ${output}" >&2; exit 1; }
done

generate_ca() {
  openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 2 \
    -subj "/CN=MASI Inference module smoke CA" \
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
generate_leaf "${temporary_root}/secrets" inference-server 301 serverAuth DNS:inference.test
generate_leaf "${temporary_root}/secrets" edge-client 302 clientAuth
generate_leaf "${temporary_root}/probe" inference-probe 303 clientAuth

probe_certificate_digest="sha256:$(openssl x509 \
  -in "${temporary_root}/probe/inference-probe.pem" -outform DER | sha256sum | awk '{print $1}')"

# Build a minimal model repository closure.
model_bytes="fake-oci-smoke-model"
printf '%s' "${model_bytes}" >"${temporary_root}/modelrepo/model.onnx"
model_digest="sha256:$(printf '%s' "${model_bytes}" | sha256sum | awk '{print $1}')"
closure_input="${model_digest}"$'\n'
closure_digest="sha256:$(printf '%s' "${closure_input}" | sha256sum | awk '{print $1}')"
jq -n --arg identity "repo-oci-smoke-0001" --arg closure_digest "${closure_digest}" \
  --arg model_digest "${model_digest}" '{
    identity:$identity, closure_digest:$closure_digest,
    members:[{rel_path:"model.onnx", member_digest:$model_digest, role:"model"}]
  }' >"${temporary_root}/modelrepo/closure-manifest.json"
chmod -R a-w "${temporary_root}/modelrepo"

# Build a minimal startup envelope.
jq -n --arg model_digest "${model_digest}" --arg closure_digest "${closure_digest}" '{
  schema_version:"inference-startup-envelope/v1",
  model_control_incarnation_id:"incarnation-oci-smoke-0001",
  operation_id:"op-oci-smoke-0001",
  kind:"binding",
  logical_pool_id:"pool-oci-smoke-0001",
  pool_generation:1,
  availability_profile_id:"availability-single/v1",
  deployment_tier:"acceptance",
  model_revision_digest:"sha256:0000000000000000000000000000000000000000000000000000000000000000",
  inference_wire_profile_digest:"sha256:0000000000000000000000000000000000000000000000000000000000000000",
  runtime_profile_id:"model-runtime-central-cpu/v1",
  repository_snapshot:{identity:"repo-oci-smoke-0001", closure_digest:$closure_digest},
  instance_group:{kind:"KIND_CPU", count:1, operator_partition_digest:"sha256:0000000000000000000000000000000000000000000000000000000000000000"},
  proposed_binding_generation:1,
  issued_at_unix_ms:0,
  expires_at_unix_ms:9999999999999,
  trace_id:"trace-oci-smoke-0001"
}' >"${temporary_root}/config/envelope.json"

jq -n \
  --arg envelope_path "/run/inference-config/envelope.json" \
  --arg repo_path "/run/inference-modelrepo" \
  --arg ca_path "/run/inference-secrets/ca.pem" \
  --arg cert_path "/run/inference-secrets/inference-server.pem" \
  --arg key_path "/run/inference-secrets/inference-server.key" \
  --arg probe_digest "${probe_certificate_digest}" '{
    schema_version:"inference-config/v1",
    startup_envelope_path:$envelope_path,
    model_repository_path:$repo_path,
    triton_endpoint:"127.0.0.1:8001",
    gateway_listen:"0.0.0.0:7443",
    tls_ca_path:$ca_path,
    tls_cert_path:$cert_path,
    tls_key_path:$key_path,
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
  --exclude='infer-cpp/**/__pycache__' --exclude='contracts/**/__pycache__' \
  -cf "${source_archive}" -C "${repo_root}" infer-cpp contracts testkit
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
if [[ -n "${MASI_INF_EXPECTED_SOURCE_TREE_DIGEST:-}" \
  && "${source_tree_digest}" != "${MASI_INF_EXPECTED_SOURCE_TREE_DIGEST}" ]]; then
  echo "OCI source tree changed after module gate snapshot" >&2
  exit 1
fi

# Build the Docker image.
docker build --platform linux/amd64 --network none --provenance=false --sbom=false \
  --file "${inf_root}/Dockerfile" \
  --build-arg "SOURCE_REVISION=${source_revision}" \
  --build-arg "SOURCE_TREE_DIGEST=${source_tree_digest}" \
  --tag "${image_ref}" "${repo_root}"

# Build the probe binary.
cmake --preset cpu-release -DCMAKE_BUILD_TYPE=Release >/dev/null 2>&1 || true
cmake --build build/cpu-release --target masi_inference_probe >/dev/null 2>&1 || true

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
  "${temporary_root}/secrets" "${temporary_root}/config" "${temporary_root}/data" "${temporary_root}/modelrepo"

docker run --detach --name "${container_name}" --read-only \
  --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 \
  --memory 512m --cpus 1 --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --publish 127.0.0.1:0:7443 \
  --volume "${temporary_root}/config:/run/inference-config:ro" \
  --volume "${temporary_root}/secrets:/run/inference-secrets:ro" \
  --volume "${temporary_root}/modelrepo:/run/inference-modelrepo:ro" \
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
  sleep 0.1
done
if [[ ! "${host_port}" =~ ^[0-9]+$ ]]; then
  docker logs "${container_name}" >"${evidence_dir}/inference-oci.log" 2>&1 || true
  echo "OCI container did not publish a usable 7443/tcp host port" >&2
  exit 1
fi

probe_succeeded=false
probe_bin="${inf_root}/build/cpu-release/masi_inference_probe"
if [[ ! -x "${probe_bin}" ]]; then
  probe_bin="${inf_root}/build/cpu-debug/masi_inference_probe"
fi
for _attempt in $(seq 1 50); do
  if [[ -x "${probe_bin}" ]] && "${probe_bin}" \
    --endpoint "127.0.0.1:${host_port}" \
    --tls-ca "${temporary_root}/probe/ca.pem" \
    --tls-cert "${temporary_root}/probe/inference-probe.pem" \
    --tls-key "${temporary_root}/probe/inference-probe.key" \
    --logical-pool-id "pool-oci-smoke-0001" \
    --pool-generation 1 \
    --binding-generation 1 \
    --model-control-incarnation-id "incarnation-oci-smoke-0001" \
    --deadline-ms 1000 >"${temporary_root}/probe.json" 2>&1; then
    probe_succeeded=true
    break
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "${container_name}")" != "true" ]]; then
    break
  fi
  sleep 0.1
done

docker logs "${container_name}" >"${evidence_dir}/inference-oci.log" 2>&1
cp -- "${temporary_root}/probe.json" "${evidence_dir}/oci-probe.json" 2>/dev/null || true
if [[ "${probe_succeeded}" != "true" ]]; then
  echo "OCI mTLS probe failed" >&2
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
  || "${container_memory_bytes}" != "536870912" \
  || "${container_nano_cpus}" != "1000000000" \
  || "$(jq -r 'has("/tmp")' <<<"${container_tmpfs}")" != "true" ]]; then
  echo "OCI container runtime hardening or resource limits drifted" >&2
  exit 1
fi

image_archive="${evidence_dir}/inference-image.tar"
docker image save --output "${image_archive}" "${image_ref}"
image_manifest_digest="sha256:$(sha256sum "${image_archive}" | awk '{print $1}')"

docker stop --time 10 "${container_name}" >/dev/null
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
    scope: "OCI startup, mTLS public-boundary liveness, and graceful SIGTERM only",
    overall_module_complete: false,
    image_ref: $image_ref,
    image_manifest_digest: $image_manifest_digest,
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
      public_mtls_probe: "PASS",
      graceful_sigterm: "PASS"
    }
  }' >"${evidence_dir}/oci-smoke-evidence.json"

if [[ "${evidence_result}" == "HOLD" ]]; then
  exit 2
fi