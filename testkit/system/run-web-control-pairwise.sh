#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"
if [[ "${MASI_P9_TOTAL_TIMEOUT_GUARD:-0}" != "1" ]]; then
  export MASI_P9_TOTAL_TIMEOUT_GUARD=1
  exec timeout --signal=TERM --kill-after=60s \
    "${MASI_P9_TOTAL_TIMEOUT_SECONDS:-5400}" "${script_dir}/run-web-control-pairwise.sh" "$@"
fi
run_id="${MASI_P9_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
[[ "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ ]] || { echo "invalid P9 run id" >&2; exit 64; }
evidence_root="${MASI_P9_EVIDENCE_DIR:-${repo_root}/evidence/pairwise-rehearsal/p9-web-control/${run_id}}"
run_tag="$(printf '%s' "${run_id,,}" | sed -E 's/[^a-z0-9_.-]+/-/g' | cut -c1-100)"
control_image="masi-nids/control-core:p9-${run_tag}"
web_image="masi-nids/web:p9-${run_tag}"
resource_nonce="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:10])')"
postgres_project="masi-p9-postgres-${resource_nonce}"
network="masi-p9-${resource_nonce}"
control_container="masi-p9-control-${resource_nonce}"
forwarder_container="masi-p9-forwarder-${resource_nonce}"
web_container="masi-p9-web-${resource_nonce}"
export MASI_POSTGRES_PUBLISH="127.0.0.1:"
cleanup_complete=false

cleanup() {
  if [[ "${cleanup_complete}" == true ]]; then return; fi
  if [[ -d "${evidence_root}" ]]; then
    timeout 30s docker logs "${control_container}" >"${evidence_root}/control.log" 2>&1 || true
    timeout 30s docker logs "${forwarder_container}" >"${evidence_root}/control-forwarder.log" 2>&1 || true
    timeout 30s docker logs "${web_container}" >"${evidence_root}/web.log" 2>&1 || true
  fi
  timeout --signal=TERM --kill-after=5s 30s docker rm -f "${web_container}" "${forwarder_container}" "${control_container}" >/dev/null 2>&1 || true
  timeout --signal=TERM --kill-after=5s 30s docker network rm "${network}" >/dev/null 2>&1 || true
  timeout --signal=TERM --kill-after=30s 180s docker compose -p "${postgres_project}" -f "${repo_root}/control-go/testdata/compose.e2e.yaml" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

for command in curl docker go jq node python3 sed timeout; do command -v "${command}" >/dev/null 2>&1 || { echo "missing ${command}" >&2; exit 1; }; done
docker_binary="$(command -v docker)"
docker() {
  timeout --signal=TERM --kill-after=30s \
    "${MASI_P9_DOCKER_COMMAND_TIMEOUT_SECONDS:-600}" "${docker_binary}" "$@"
}
curl_probe=(curl --fail --silent --show-error --connect-timeout 2 --max-time 3)
if [[ -e "${evidence_root}" || -L "${evidence_root}" || "${evidence_root}" == "/" || "${evidence_root}" == "${repo_root}" ]]; then
  echo "unsafe or existing P9 evidence root" >&2
  exit 64
fi
mkdir -p -- "${evidence_root}"
source_identity_path="${evidence_root}/source-identity.json"
python3 "${script_dir}/source_identity.py" "${repo_root}" >"${source_identity_path}.tmp"
mv -T -- "${source_identity_path}.tmp" "${source_identity_path}"
chmod 0444 "${source_identity_path}"

timeout --signal=TERM --kill-after=30s 600s docker compose -p "${postgres_project}" -f "${repo_root}/control-go/testdata/compose.e2e.yaml" up -d --wait
postgres_endpoint="$(timeout 30s docker compose -p "${postgres_project}" -f "${repo_root}/control-go/testdata/compose.e2e.yaml" port postgres 5432)"
postgres_port="${postgres_endpoint##*:}"
[[ "${postgres_port}" =~ ^[0-9]+$ ]] || { echo "invalid dynamic PostgreSQL port" >&2; exit 1; }
postgres_dsn="postgres://masi:masi@127.0.0.1:${postgres_port}/masi_control_test?sslmode=disable"
(
  cd -- "${repo_root}/control-go"
  timeout --signal=TERM --kill-after=30s 600s go run ./cmd/migrate-test \
    --dsn "${postgres_dsn}" \
    --dir ../db/migrations
)

timeout --signal=TERM --kill-after=30s 1800s docker build --pull=false -t "${control_image}" "${repo_root}/control-go"
timeout --signal=TERM --kill-after=30s 1800s docker build --pull=false -f "${repo_root}/web/Dockerfile" -t "${web_image}" "${repo_root}"
control_digest="$(timeout 30s docker image inspect --format '{{.Id}}' "${control_image}")"
web_digest="$(timeout 30s docker image inspect --format '{{.Id}}' "${web_image}")"

start_runtime() {
  local control_endpoint postgres_network session_status web_endpoint
  postgres_network="${postgres_project}_default"
  docker network inspect "${postgres_network}" >/dev/null || return 1
  docker network create "${network}" >/dev/null || return 1
  chmod 0600 "${evidence_root}/web-runtime.conf" "${evidence_root}/control-forwarder.conf" \
    "${evidence_root}/control-config.json" 2>/dev/null || true
  # Nginx must receive the literal runtime variable.
  # shellcheck disable=SC2016
  sed -e '/server_name _;/a\    resolver 127.0.0.11 ipv6=off valid=5s;\n    set $control_upstream http://control-core:18080;' \
    -e 's#proxy_pass http://control-core:8080;#proxy_pass $control_upstream;#g' \
    "${repo_root}/web/nginx.conf" >"${evidence_root}/web-runtime.conf" || return 1
  chmod 0444 "${evidence_root}/web-runtime.conf"
  docker run -d --name "${web_container}" --network "${network}" -p 127.0.0.1::8080 \
    -v "${evidence_root}/web-runtime.conf:/etc/nginx/conf.d/default.conf:ro" \
    --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m --tmpfs /var/cache/nginx:rw,noexec,nosuid,size=16m \
    --tmpfs /var/run:rw,noexec,nosuid,size=1m "${web_image}" >/dev/null || return 1
  web_endpoint="$(docker port "${web_container}" 8080/tcp)"
  web_port="${web_endpoint##*:}"
  [[ "${web_endpoint}" == 127.0.0.1:* && "${web_port}" =~ ^[0-9]+$ ]] || return 1
  for _ in $(seq 1 100); do
    "${curl_probe[@]}" "http://127.0.0.1:${web_port}/healthz" >/dev/null && break
    [[ "$(docker inspect -f '{{.State.Running}}' "${web_container}" 2>/dev/null || true)" == "true" ]] || return 1
    sleep 0.2
  done
  "${curl_probe[@]}" "http://127.0.0.1:${web_port}/healthz" >"${evidence_root}/web-ready.json" || return 1
  jq --arg http_listen "127.0.0.1:8080" --arg grpc_listen "127.0.0.1:8443" \
    --arg public_origin "http://127.0.0.1:${web_port}" \
    --arg dsn "postgres://masi:masi@postgres:5432/masi_control_test?sslmode=disable" \
    '.http_listen=$http_listen | .grpc_listen=$grpc_listen | .public_origin=$public_origin |
     .postgresql_dsn=$dsn | .postgresql_test_dsn=$dsn' \
    "${repo_root}/deploy/system-e2e/control-web-pairwise.json" >"${evidence_root}/control-config.json" || return 1
  chmod 0444 "${evidence_root}/control-config.json"
  sed -e 's/listen 8080;/listen 18080;/' \
    -e 's/listen \[::\]:8080;/listen [::]:18080;/' \
    -e 's#proxy_pass http://127.0.0.1:18080;#proxy_pass http://127.0.0.1:8080;#' \
    "${repo_root}/deploy/system-e2e/control-forwarder.conf" >"${evidence_root}/control-forwarder.conf" || return 1
  chmod 0444 "${evidence_root}/control-forwarder.conf"
  docker run -d --name "${control_container}" --network "${postgres_network}" -p 127.0.0.1::18080 --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    -v "${evidence_root}/control-config.json:/run/masi/config.json:ro" \
    -v "${repo_root}/control-go/testdata/role-mapping-e2e.json:/run/masi/role-mapping-e2e.json:ro" \
    -v "${repo_root}/contracts:/run/masi/contracts:ro" \
    "${control_image}" --config /run/masi/config.json >/dev/null || return 1
  docker network connect --alias control-core "${network}" "${control_container}" || return 1
  docker run -d --name "${forwarder_container}" --network "container:${control_container}" \
    -v "${evidence_root}/control-forwarder.conf:/etc/nginx/conf.d/default.conf:ro" \
    --read-only --tmpfs /tmp:rw,noexec,nosuid,size=8m --tmpfs /var/cache/nginx:rw,noexec,nosuid,size=8m \
    --tmpfs /var/run:rw,noexec,nosuid,size=1m \
    docker.io/nginxinc/nginx-unprivileged:1.29.5-alpine@sha256:42a7d7f2ee23e9f5a1dcdf3647ba5c585bbd18f79e79cd817e70e8cd61c55779 >/dev/null || return 1
  control_endpoint="$(docker port "${control_container}" 18080/tcp)"
  control_port="${control_endpoint##*:}"
  [[ "${control_endpoint}" == 127.0.0.1:* && "${control_port}" =~ ^[0-9]+$ ]] || return 1
  for _ in $(seq 1 100); do
    "${curl_probe[@]}" "http://127.0.0.1:${control_port}/readyz" >/dev/null && break
    [[ "$(docker inspect -f '{{.State.Running}}' "${control_container}" 2>/dev/null || true)" == "true" ]] || return 1
    sleep 0.2
  done
  "${curl_probe[@]}" "http://127.0.0.1:${control_port}/readyz" >"${evidence_root}/control-ready.json" || return 1
  session_status="$(curl --silent --show-error --connect-timeout 2 --max-time 3 --output /dev/null --write-out '%{http_code}' \
    "http://127.0.0.1:${web_port}/api/session")" || return 1
  [[ "${session_status}" == "401" ]] || return 1
}

runtime_started=false
for port_attempt in 1 2 3; do
  if start_runtime; then runtime_started=true; break; fi
  timeout 30s docker logs "${control_container}" >"${evidence_root}/control-attempt-${port_attempt}.log" 2>&1 || true
  timeout 30s docker logs "${forwarder_container}" >"${evidence_root}/forwarder-attempt-${port_attempt}.log" 2>&1 || true
  timeout 30s docker logs "${web_container}" >"${evidence_root}/web-attempt-${port_attempt}.log" 2>&1 || true
  timeout --signal=TERM --kill-after=5s 30s docker rm -f "${web_container}" "${forwarder_container}" "${control_container}" >/dev/null 2>&1 || true
  timeout --signal=TERM --kill-after=5s 30s docker network rm "${network}" >/dev/null 2>&1 || true
done
[[ "${runtime_started}" == true ]] || { echo "P9 runtime exhausted three bounded port attempts" >&2; exit 1; }

for browser in chromium firefox webkit; do
  "${curl_probe[@]}" "http://127.0.0.1:${web_port}/healthz" >/dev/null
  timeout --signal=TERM --kill-after=30s 300s node "${script_dir}/web-control-pairwise.mjs" \
    --browser "${browser}" --web-base-url "http://127.0.0.1:${web_port}" --control-base-url "http://127.0.0.1:${control_port}" \
    --control-image-digest "${control_digest}" --web-image-digest "${web_digest}" \
    --evidence "${evidence_root}/browser-${browser}.json"
done
set +e
python3 "${script_dir}/cleanup-p9.py" \
  --project "${postgres_project}" \
  --compose-file "${repo_root}/control-go/testdata/compose.e2e.yaml" \
  --network "${network}" \
  --container "${control_container}" --container "${forwarder_container}" --container "${web_container}" \
  --evidence-root "${evidence_root}" --output "${evidence_root}/cleanup.json"
cleanup_status=$?
set -e
if [[ "${cleanup_status}" -ne 0 ]]; then
  echo "P9 cleanup left residual resources" >&2
  exit 1
fi
cleanup_complete=true
python3 "${script_dir}/source_identity.py" "${repo_root}" >"${evidence_root}/source-identity-final.json"
cmp --silent "${source_identity_path}" "${evidence_root}/source-identity-final.json" \
  || { echo "P9 source closure changed during execution" >&2; exit 1; }
timeout --signal=TERM --kill-after=10s 60s node "${script_dir}/aggregate-web-control-pairwise.mjs" \
  --inputs "${evidence_root}/browser-chromium.json,${evidence_root}/browser-firefox.json,${evidence_root}/browser-webkit.json" \
  --run-id "${run_id}" --source-identity "${source_identity_path}" \
  --cleanup "${evidence_root}/cleanup.json" \
  --evidence "${evidence_root}/pairwise-rehearsal.json"
python3 "${repo_root}/db/scripts/validate-json.py" \
  --schema "${repo_root}/contracts/evidence/web-control-pairwise-rehearsal/v1/schema.json" \
  --document "${evidence_root}/pairwise-rehearsal.json" >/dev/null
jq . "${evidence_root}/pairwise-rehearsal.json"
