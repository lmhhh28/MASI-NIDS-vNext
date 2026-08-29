#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
guard_repo_root="$(cd -- "${script_dir}/../.." && pwd)"
if [[ "${MASI_RUNTIME_SMOKE_WRAPPED:-0}" != "1" ]]; then
  exec python3 "${guard_repo_root}/scripts/ci/run_bounded_runtime_smoke.py" \
    --repo "${guard_repo_root}" --module control \
    --timeout-seconds "${MASI_CONTROL_OCI_TOTAL_TIMEOUT_SECONDS:-3600}" -- \
    "${script_dir}/run-oci-smoke.sh" "$@"
fi
if [[ "$#" -ne 6 ]]; then
  echo "usage: run-oci-smoke.sh CONTROL_ROOT REPO_ROOT SOURCE_CONFIG EVIDENCE_DIR RUNTIME_CONFIG IMAGE_TAG" >&2
  exit 64
fi

control_root="$(realpath -e -- "$1")"
repo_root="$(realpath -e -- "$2")"
source_config="$(realpath -e -- "$3")"
evidence_input="$4"
evidence_dir="$(realpath -m -s -- "${evidence_input}")"
runtime_config_input="$5"
runtime_config="$(realpath -m -s -- "${runtime_config_input}")"
image_tag="$6"

if [[ ! -d "${control_root}" || ! -d "${repo_root}/contracts" || ! -d "${evidence_dir}" \
  || -L "${evidence_input}" || "$(realpath -e -- "${evidence_input}")" != "${evidence_dir}" \
  || "$(realpath -m -- "${runtime_config_input}")" != "${runtime_config}" \
  || ! -d "$(dirname -- "${runtime_config}")" || -L "$(dirname -- "${runtime_config}")" \
  || ! "${image_tag}" =~ ^[a-z0-9][a-z0-9._:/-]{1,127}$ \
  || -e "${runtime_config}" || -L "${runtime_config}" ]]; then
  echo "unsafe OCI smoke input" >&2
  exit 64
fi
command -v timeout >/dev/null 2>&1 || { echo "missing timeout" >&2; exit 69; }

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
started_ns="$(date +%s%N)"
image_id_file="${evidence_dir}/image-id.txt"
timeout --signal=TERM --kill-after=60s 1800s docker build --iidfile "${image_id_file}" -t "${image_tag}" "${control_root}"
image_id="$(tr -d '\r\n' <"${image_id_file}")"
if [[ ! "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "OCI builder did not return an immutable image id" >&2
  exit 1
fi

container_name_base="masi-control-gate-$(printf '%s' "${image_tag}" | sha256sum | cut -c1-12)"
container_name=""
container_id=""
cleanup_container() {
  if [[ -n "${container_id}" ]]; then
    timeout --signal=TERM --kill-after=5s 30s docker stop -t 3 "${container_id}" >/dev/null 2>&1 || true
    timeout --signal=TERM --kill-after=5s 30s docker rm -f "${container_id}" >/dev/null 2>&1 || true
  fi
}
trap cleanup_container EXIT

allocate_loopback_port() {
  python3 - <<'PY'
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
}

startup_log=""
for attempt in 1 2 3; do
  http_port="$(allocate_loopback_port)"
  grpc_port="$(allocate_loopback_port)"
  while [[ "${grpc_port}" == "${http_port}" ]]; do grpc_port="$(allocate_loopback_port)"; done
  runtime_temporary="${runtime_config}.${attempt}.tmp"
  jq --arg http "127.0.0.1:${http_port}" --arg grpc "127.0.0.1:${grpc_port}" '
    .http_listen=$http |
    .grpc_listen=$grpc |
    .public_origin=("http://" + $http) |
    .role_mapping_path="/runtime/role-mapping-e2e.json" |
    .contract_root="/contracts"
  ' "${source_config}" >"${runtime_temporary}"
  chmod 0644 -- "${runtime_temporary}"
  mv -T -- "${runtime_temporary}" "${runtime_config}"
  container_name="${container_name_base}-${attempt}"
  container_id="$(timeout --signal=TERM --kill-after=10s 60s docker run -d --name "${container_name}" --network host --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
    -v "${runtime_config}:/runtime/control.json:ro" \
    -v "${control_root}/testdata/role-mapping-e2e.json:/runtime/role-mapping-e2e.json:ro" \
    -v "${repo_root}/contracts:/contracts:ro" \
    "${image_tag}" --config /runtime/control.json)"
  sleep 0.5
  if [[ "$(timeout 30s docker inspect -f '{{.State.Running}}' "${container_id}" 2>/dev/null || true)" == "true" ]]; then
    break
  fi
  startup_log="$(timeout 30s docker logs "${container_id}" 2>&1 || true)"
  timeout --signal=TERM --kill-after=5s 30s docker rm -f "${container_id}" >/dev/null 2>&1 || true
  container_id=""
done
[[ -n "${container_id}" ]] || { echo "OCI startup exhausted loopback port retries: ${startup_log}" >&2; exit 1; }

ready=false
live=false
for _ in $(seq 1 100); do
  if MASI_CONTROL_OCI_HTTP_PORT="${http_port}" python3 - 2>/dev/null <<'PY'
import os
import urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:{os.environ['MASI_CONTROL_OCI_HTTP_PORT']}/readyz", timeout=0.5) as response:
    raise SystemExit(0 if response.status == 200 else 1)
PY
  then
    ready=true
    break
  fi
  if [[ "$(docker inspect -f '{{.State.Running}}' "${container_id}" 2>/dev/null || true)" != "true" ]]; then
    docker logs "${container_id}" >&2 || true
    exit 1
  fi
  sleep 0.1
done
if [[ "${ready}" != true ]]; then
  docker logs "${container_id}" >&2 || true
  exit 1
fi
if MASI_CONTROL_OCI_HTTP_PORT="${http_port}" python3 - 2>/dev/null <<'PY'
import os
import urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:{os.environ['MASI_CONTROL_OCI_HTTP_PORT']}/livez", timeout=1) as response:
    raise SystemExit(0 if response.status == 200 else 1)
PY
then
  live=true
fi
if [[ "${live}" != true ]]; then
  exit 1
fi

docker logs "${container_id}" >"${evidence_dir}/container.log" 2>&1 || true
timeout --signal=TERM --kill-after=5s 30s docker stop -t 10 "${container_id}" >/dev/null
exit_code="$(timeout 30s docker inspect -f '{{.State.ExitCode}}' "${container_id}")"
if [[ "${exit_code}" != "0" ]]; then
  docker logs "${container_id}" >&2 || true
  exit 1
fi
timeout --signal=TERM --kill-after=5s 30s docker rm "${container_id}" >/dev/null
container_id=""

finished_ns="$(date +%s%N)"
finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
runtime_config_digest="sha256:$(sha256sum "${runtime_config}" | awk '{print $1}')"
log_digest="sha256:$(sha256sum "${evidence_dir}/container.log" | awk '{print $1}')"
jq -n \
  --arg started_at "${started_at}" --arg finished_at "${finished_at}" \
  --arg image_id "${image_id}" --arg runtime_config_digest "${runtime_config_digest}" \
  --arg log_digest "${log_digest}" --arg http_port "${http_port}" \
  --argjson duration_ms "$(((finished_ns-started_ns)/1000000))" \
  '{schema_version:"control-core-oci-smoke/v1",level:"MODULE",applicability:"APPLICABLE",
    result:"PASS",qualification:"QUALIFIED",deployment_tier:"test-single-process",
    runtime_profile:"test",image_id:$image_id,runtime_config_digest:$runtime_config_digest,
    started_at:$started_at,finished_at:$finished_at,duration_ms:$duration_ms,
    startup:true,readiness:true,liveness:true,graceful_shutdown:true,
    endpoints:{readyz:("http://127.0.0.1:"+$http_port+"/readyz"),livez:("http://127.0.0.1:"+$http_port+"/livez")},
    container_log:{path:"container.log",digest:$log_digest}}' \
  >"${evidence_dir}/oci-smoke-evidence.json"

echo "oci-startup-readiness-liveness-ok image=${image_id}"
