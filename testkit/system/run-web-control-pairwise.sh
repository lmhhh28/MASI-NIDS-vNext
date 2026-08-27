#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"
run_id="${MASI_P9_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
evidence_root="${MASI_P9_EVIDENCE_DIR:-${repo_root}/evidence/pairwise-rehearsal/p9-web-control/${run_id}}"
control_image="masi-nids/control-core:p9-${run_id}"
web_image="masi-nids/web:p9-${run_id}"
postgres_project="masi-p9-postgres-${$}"
network="masi-p9-${$}"
control_container="masi-p9-control-${$}"
forwarder_container="masi-p9-forwarder-${$}"
web_container="masi-p9-web-${$}"

cleanup() {
  if [[ -d "${evidence_root}" ]]; then
    docker logs "${control_container}" >"${evidence_root}/control.log" 2>&1 || true
    docker logs "${forwarder_container}" >"${evidence_root}/control-forwarder.log" 2>&1 || true
    docker logs "${web_container}" >"${evidence_root}/web.log" 2>&1 || true
  fi
  docker rm -f "${web_container}" "${forwarder_container}" "${control_container}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
  docker compose -p "${postgres_project}" -f "${repo_root}/control-go/testdata/compose.e2e.yaml" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

for command in curl docker go jq node; do command -v "${command}" >/dev/null 2>&1 || { echo "missing ${command}" >&2; exit 1; }; done
if [[ -e "${evidence_root}" || -L "${evidence_root}" || "${evidence_root}" == "/" || "${evidence_root}" == "${repo_root}" ]]; then
  echo "unsafe or existing P9 evidence root" >&2
  exit 64
fi
mkdir -p -- "${evidence_root}"

docker compose -p "${postgres_project}" -f "${repo_root}/control-go/testdata/compose.e2e.yaml" up -d --wait
(
  cd -- "${repo_root}/control-go"
  go run ./cmd/migrate-test \
    --dsn 'postgres://masi:masi@127.0.0.1:55433/masi_control_test?sslmode=disable' \
    --dir ../db/migrations
)

docker build --pull=false -t "${control_image}" "${repo_root}/control-go"
docker build --pull=false -f "${repo_root}/web/Dockerfile" -t "${web_image}" "${repo_root}"
control_digest="$(docker image inspect --format '{{.Id}}' "${control_image}")"
web_digest="$(docker image inspect --format '{{.Id}}' "${web_image}")"

docker run -d --name "${control_container}" --network host --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  -v "${repo_root}/deploy/system-e2e/control-web-pairwise.json:/run/masi/config.json:ro" \
  -v "${repo_root}/control-go/testdata/role-mapping-e2e.json:/run/masi/role-mapping-e2e.json:ro" \
  -v "${repo_root}/contracts:/run/masi/contracts:ro" \
  "${control_image}" --config /run/masi/config.json >/dev/null
for _ in $(seq 1 60); do
  if curl --fail --silent http://127.0.0.1:18080/readyz >/dev/null; then break; fi
  sleep 1
done
curl --fail --silent http://127.0.0.1:18080/readyz >"${evidence_root}/control-ready.json"

docker network create "${network}" >/dev/null
docker run -d --name "${forwarder_container}" --network host \
  -v "${repo_root}/deploy/system-e2e/control-forwarder.conf:/etc/nginx/conf.d/default.conf:ro" \
  docker.io/nginxinc/nginx-unprivileged:1.29.5-alpine@sha256:42a7d7f2ee23e9f5a1dcdf3647ba5c585bbd18f79e79cd817e70e8cd61c55779 >/dev/null
docker run -d --name "${web_container}" --network "${network}" -p 127.0.0.1:4189:8080 \
  --add-host control-core:host-gateway \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m --tmpfs /var/cache/nginx:rw,noexec,nosuid,size=16m \
  --tmpfs /var/run:rw,noexec,nosuid,size=1m "${web_image}" >/dev/null
for _ in $(seq 1 60); do
  if curl --fail --silent http://127.0.0.1:4189/healthz >/dev/null; then break; fi
  sleep 1
done
curl --fail --silent http://127.0.0.1:4189/healthz >"${evidence_root}/web-ready.json"

for browser in chromium firefox webkit; do
  curl --fail --silent http://127.0.0.1:4189/healthz >/dev/null
  node "${script_dir}/web-control-pairwise.mjs" \
    --browser "${browser}" --web-base-url http://127.0.0.1:4189 --control-base-url http://127.0.0.1:18080 \
    --control-image-digest "${control_digest}" --web-image-digest "${web_digest}" \
    --evidence "${evidence_root}/browser-${browser}.json"
done
node "${script_dir}/aggregate-web-control-pairwise.mjs" \
  --inputs "${evidence_root}/browser-chromium.json,${evidence_root}/browser-firefox.json,${evidence_root}/browser-webkit.json" \
  --evidence "${evidence_root}/pairwise-rehearsal.json"
jq . "${evidence_root}/pairwise-rehearsal.json"
