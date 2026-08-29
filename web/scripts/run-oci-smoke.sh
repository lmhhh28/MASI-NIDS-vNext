#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
module_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${module_root}/.." && pwd)"
if [[ "${MASI_RUNTIME_SMOKE_WRAPPED:-0}" != "1" ]]; then
  exec python3 "${repo_root}/scripts/ci/run_bounded_runtime_smoke.py" \
    --repo "${repo_root}" --module web \
    --timeout-seconds "${MASI_WEB_OCI_TOTAL_TIMEOUT_SECONDS:-7200}" -- \
    "${script_dir}/run-oci-smoke.sh" "$@"
fi
image_ref="${MASI_WEB_IMAGE_REF:-masi-nids/web:module-gates}"
evidence_path="${MASI_WEB_OCI_EVIDENCE:-}"
browser_evidence_path="${MASI_WEB_BROWSER_EVIDENCE:-}"
performance_evidence_path="${MASI_WEB_PERFORMANCE_EVIDENCE:-}"
soak_evidence_path="${MASI_WEB_SOAK_EVIDENCE:-}"
suffix="${$}"
network="masi-web-smoke-${suffix}"
fake="masi-web-fake-${suffix}"
web="masi-web-oci-${suffix}"
port="${MASI_WEB_OCI_PORT:-}"
temporary_root="$(mktemp -d /tmp/masi-web-oci.XXXXXX)"

cleanup() {
  if [[ "${web}" == masi-web-oci-* ]]; then timeout --signal=TERM --kill-after=5s 30s docker rm -f "${web}" >/dev/null 2>&1 || true; fi
  if [[ "${fake}" == masi-web-fake-* ]]; then timeout --signal=TERM --kill-after=5s 30s docker rm -f "${fake}" >/dev/null 2>&1 || true; fi
  if [[ "${network}" == masi-web-smoke-* ]]; then timeout --signal=TERM --kill-after=5s 30s docker network rm "${network}" >/dev/null 2>&1 || true; fi
  if [[ "${temporary_root}" == /tmp/masi-web-oci.* ]]; then rm -rf -- "${temporary_root}"; fi
}
trap cleanup EXIT

for command in curl docker git jq node npx sha256sum tar timeout; do command -v "${command}" >/dev/null 2>&1 || { echo "missing ${command}" >&2; exit 1; }; done
docker_binary="$(command -v docker)"
docker() {
  timeout --signal=TERM --kill-after=30s \
    "${MASI_WEB_DOCKER_COMMAND_TIMEOUT_SECONDS:-600}" "${docker_binary}" "$@"
}

tar --sort=name --mtime=@1787760000 --owner=0 --group=0 --numeric-owner \
  --exclude='web/node_modules' --exclude='web/dist' --exclude='web/output' \
  --exclude='web/evidence' --exclude='web/.playwright-cli' --exclude='web/sbom.cdx.json' \
  -cf "${temporary_root}/source.tar" -C "${repo_root}" web contracts/generated/typescript/control-api \
  contracts/web/v1 contracts/profiles/v1/web-spa.json contracts/profiles/v1/web-browser.json \
  contracts/profiles/v1/web-performance.json contracts/evidence/web-performance/v1 contracts/evidence/web-soak/v1 \
  contracts/evidence/web-manual-accessibility/v1 contracts/evidence/web-module/v1 \
  contracts/supply-chain/v1/web-components.json
source_tree_digest="sha256:$(sha256sum "${temporary_root}/source.tar" | awk '{print $1}')"
source_revision="$(git -C "${repo_root}" rev-parse HEAD)"

timeout --signal=TERM --kill-after=60s 1800s docker build --pull=false -f "${module_root}/Dockerfile" -t "${image_ref}" \
  --build-arg "SOURCE_REVISION=${source_revision}" --build-arg "SOURCE_TREE_DIGEST=${source_tree_digest}" "${repo_root}"
image_digest="$(timeout 30s docker image inspect --format '{{.Id}}' "${image_ref}")"
timeout --signal=TERM --kill-after=5s 30s docker network create "${network}" >/dev/null
timeout --signal=TERM --kill-after=10s 60s docker run -d --name "${fake}" --network "${network}" --network-alias control-core \
  -v "${module_root}/test-server:/app:ro" \
  docker.io/library/node:22.22.3-alpine3.23@sha256:968df39aedcea65eeb078fb336ed7191baf48f972b4479711397108be0966920 \
  node /app/server.mjs /tmp 8080 0.0.0.0 >/dev/null
publish="127.0.0.1::8080"
if [[ -n "${port}" ]]; then
  [[ "${port}" =~ ^[0-9]{1,5}$ && "${port}" -ge 1024 && "${port}" -le 65535 ]] \
    || { echo "invalid MASI_WEB_OCI_PORT" >&2; exit 64; }
  publish="127.0.0.1:${port}:8080"
fi
timeout --signal=TERM --kill-after=10s 60s docker run -d --name "${web}" --network "${network}" -p "${publish}" \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m --tmpfs /var/cache/nginx:rw,noexec,nosuid,size=16m \
  --tmpfs /var/run:rw,noexec,nosuid,size=1m "${image_ref}" >/dev/null
if [[ -z "${port}" ]]; then
  published="$(timeout 30s docker port "${web}" 8080/tcp)"
  [[ "${published}" =~ ^127\.0\.0\.1:([0-9]{1,5})$ ]] \
    || { echo "Docker did not allocate a loopback Web port" >&2; exit 1; }
  port="${BASH_REMATCH[1]}"
fi

for _ in $(seq 1 30); do
  if curl --fail --silent --show-error --connect-timeout 2 --max-time 3 "http://127.0.0.1:${port}/healthz" >"${temporary_root}/health.json"; then break; fi
  sleep 1
done
jq -e '.state=="ready" and .module=="MOD-WEB-001"' "${temporary_root}/health.json" >/dev/null
curl --fail --silent --show-error --connect-timeout 2 --max-time 5 --dump-header "${temporary_root}/headers.txt" "http://127.0.0.1:${port}/operations/models" -o "${temporary_root}/deep-link.html"
grep -q 'Content-Security-Policy:' "${temporary_root}/headers.txt"
grep -q 'Cache-Control: no-store' "${temporary_root}/headers.txt"
grep -q '<div id="app"></div>' "${temporary_root}/deep-link.html"
curl --fail --silent --show-error --connect-timeout 2 --max-time 5 "http://127.0.0.1:${port}/api/session" -o "${temporary_root}/session.json"
jq -e '.schema_version=="masi-web-projection/v1" and (.csrf_token|length)>=32' "${temporary_root}/session.json" >/dev/null
runtime_user="$(timeout 30s docker inspect --format '{{.Config.User}}' "${web}")"
[[ "${runtime_user}" == "101:101" ]]
if timeout 30s docker exec "${web}" sh -c 'command -v node' >/dev/null 2>&1; then echo "Node runtime present in production image" >&2; exit 1; fi
if timeout 30s docker exec "${web}" sh -c 'find /usr/share/nginx/html -type f -name "*.map" -print -quit' | grep -q .; then echo "source map present" >&2; exit 1; fi
health_status="starting"
for _ in $(seq 1 30); do
  health_status="$(timeout 30s docker inspect --format '{{.State.Health.Status}}' "${web}")"
  [[ "${health_status}" == "healthy" ]] && break
  sleep 1
done
[[ "${health_status}" == "healthy" ]]

browser_tests=0
browser_failures=0
if [[ "${MASI_WEB_OCI_SKIP_BROWSER:-0}" != "1" ]]; then
  (
    cd -- "${module_root}"
    WEB_E2E_BASE_URL="http://127.0.0.1:${port}" timeout --signal=TERM --kill-after=30s 1800s npx playwright test
  )
  browser_tests="$(jq '.stats.expected' "${module_root}/output/playwright/results.json")"
  browser_failures="$(jq '(.stats.unexpected // 0) + (.stats.flaky // 0)' "${module_root}/output/playwright/results.json")"
  [[ "${browser_tests}" -ge 18 && "${browser_failures}" -eq 0 ]]
  if [[ -n "${browser_evidence_path}" ]]; then
    [[ ! -e "${browser_evidence_path}" && ! -L "${browser_evidence_path}" ]] || { echo "browser evidence path exists" >&2; exit 1; }
    cp -- "${module_root}/output/playwright/results.json" "${browser_evidence_path}"
  fi
fi

if [[ -n "${performance_evidence_path}" ]]; then
  [[ ! -e "${performance_evidence_path}" && ! -L "${performance_evidence_path}" ]] || { echo "performance evidence path exists" >&2; exit 1; }
  timeout --signal=TERM --kill-after=30s 1800s node "${module_root}/scripts/run-performance.mjs" \
    --base-url "http://127.0.0.1:${port}" --evidence "${performance_evidence_path}" \
    --source-tree-digest "${source_tree_digest}" --image-digest "${image_digest}"
fi

if [[ -n "${soak_evidence_path}" ]]; then
  [[ ! -e "${soak_evidence_path}" && ! -L "${soak_evidence_path}" ]] || { echo "soak evidence path exists" >&2; exit 1; }
  timeout --signal=TERM --kill-after=30s 7200s node "${module_root}/scripts/run-soak.mjs" \
    --base-url "http://127.0.0.1:${port}" --evidence "${soak_evidence_path}" \
    --source-tree-digest "${source_tree_digest}" \
    --image-digest "${image_digest}" \
    --warmup-seconds "${MASI_WEB_SOAK_WARMUP_SECONDS:-60}" \
    --phase-seconds "${MASI_WEB_SOAK_PHASE_SECONDS:-900}"
fi

performance_result="NOT_RUN"
soak_result="NOT_RUN"
if [[ -n "${performance_evidence_path}" ]]; then performance_result="$(jq -r '.result' "${performance_evidence_path}")"; fi
if [[ -n "${soak_evidence_path}" ]]; then soak_result="$(jq -r '.result' "${soak_evidence_path}")"; fi

evidence="$(jq -n --arg image_digest "${image_digest}" --arg source_tree_digest "${source_tree_digest}" \
  --arg source_revision "${source_revision}" --arg user "${runtime_user}" \
  --argjson browser_tests "${browser_tests}" --argjson browser_failures "${browser_failures}" \
  --arg performance_result "${performance_result}" --arg soak_result "${soak_result}" \
  '{schema_version:"web-oci-evidence/v1",image_digest:$image_digest,source_tree_digest:$source_tree_digest,
    source_revision:$source_revision,user:$user,read_only_rootfs:true,node_runtime:false,source_maps:false,
    health:"ready",deep_link:true,same_origin_proxy:true,security_headers:true,result:"PASS",
    browser_tests:$browser_tests,browser_failures:$browser_failures,
    performance_result:$performance_result,soak_result:$soak_result,
    qualification:"NOT_QUALIFIED",qualification_scope:"REAL_WEB_OCI_WITH_CONTRACT_FAKE_NEIGHBOR"}')"
if [[ -n "${evidence_path}" ]]; then
  [[ ! -e "${evidence_path}" ]] || { echo "evidence path exists" >&2; exit 1; }
  printf '%s\n' "${evidence}" >"${evidence_path}"
fi
printf '%s\n' "${evidence}"
