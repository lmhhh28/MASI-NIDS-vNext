#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
module_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${module_root}/.." && pwd)"
evidence_base="$(realpath -m -- "${MASI_WEB_EVIDENCE_DIR:-${module_root}/evidence/module-gates}")"
run_id="${MASI_WEB_GATE_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"

if [[ "${MASI_WEB_FORMAL_SOAK:-0}" != "1" ]]; then
  echo "set MASI_WEB_FORMAL_SOAK=1: Module Complete requires the 3600-second soak" >&2
  exit 64
fi
if [[ ! "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ ]]; then
  echo "invalid Web gate run id" >&2
  exit 64
fi
if [[ "${evidence_base}" == "/" || "${evidence_base}" == "${repo_root}" || "${evidence_base}" == "${module_root}" || -L "${evidence_base}" ]]; then
  echo "unsafe Web evidence directory" >&2
  exit 64
fi
mkdir -p -- "${evidence_base}/runs"
run_root="${evidence_base}/runs/${run_id}"
if ! mkdir -- "${run_root}"; then
  echo "Web gate run already exists: ${run_id}" >&2
  exit 1
fi
mkdir -- "${run_root}/commands" "${run_root}/logs"

calculate_source_tree_digest() {
  tar --sort=name --mtime=@1787760000 --owner=0 --group=0 --numeric-owner \
    --exclude='web/node_modules' --exclude='web/dist' --exclude='web/output' \
    --exclude='web/evidence' --exclude='web/.playwright-cli' --exclude='web/sbom.cdx.json' \
    -cf - -C "${repo_root}" web contracts/generated/typescript/control-api \
    contracts/web/v1 contracts/profiles/v1/web-spa.json contracts/profiles/v1/web-browser.json \
    contracts/profiles/v1/web-performance.json contracts/supply-chain/v1/web-components.json | sha256sum | awk '{print "sha256:" $1}'
}

source_tree_digest="$(calculate_source_tree_digest)"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
working_tree_status="$(git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)"
working_tree_dirty=false
if [[ -n "${working_tree_status}" ]]; then working_tree_dirty=true; fi
printf '%s\n' "${working_tree_status}" >"${run_root}/working-tree-status.txt"
working_tree_status_digest="sha256:$(sha256sum "${run_root}/working-tree-status.txt" | awk '{print $1}')"

run_logged() {
  local command_id="$1"
  shift
  local log_path="${run_root}/logs/${command_id}.log"
  local command_path="${run_root}/commands/${command_id}.json"
  local command_started command_finished start_ns finish_ns duration_ms status result qualification
  local -a command=("$@")
  command_started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  start_ns="$(date +%s%N)"
  set +e
  timeout --signal=TERM --kill-after=30s "${MASI_WEB_COMMAND_TIMEOUT_SECONDS:-7200}s" "${command[@]}" >"${log_path}" 2>&1
  status=$?
  set -e
  finish_ns="$(date +%s%N)"
  command_finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  duration_ms="$(((finish_ns - start_ns) / 1000000))"
  result="PASS"
  qualification="QUALIFIED"
  if [[ "${status}" -ne 0 ]]; then result="FAIL"; qualification="NOT_QUALIFIED"; fi
  jq -n --arg command_id "${command_id}" --arg started_at "${command_started}" --arg finished_at "${command_finished}" \
    --argjson duration_ms "${duration_ms}" --argjson exit_code "${status}" --arg result "${result}" \
    --arg qualification "${qualification}" --arg log_digest "sha256:$(sha256sum "${log_path}" | awk '{print $1}')" \
    --args '$ARGS.positional as $argv | {command_id:$command_id,started_at:$started_at,finished_at:$finished_at,
      duration_ms:$duration_ms,exit_code:$exit_code,result:$result,qualification:$qualification,
      log_digest:$log_digest,argv:$argv}' -- "${command[@]}" >"${command_path}"
  if [[ "${status}" -ne 0 ]]; then
    tail -n 120 "${log_path}" >&2
    exit "${status}"
  fi
}

cd -- "${module_root}"
run_logged node-version node --version
run_logged dependency-lock npm ci --ignore-scripts --no-audit --no-fund
run_logged contracts node scripts/validate-contracts.mjs
run_logged generated-client bash "${repo_root}/control-go/scripts/check-typescript-client.sh"
run_logged source-sentinels node scripts/check-source-sentinels.mjs
run_logged lint npm run lint
run_logged typecheck npm run typecheck
run_logged unit-component-a11y npm run test
run_logged production-build npm run build
run_logged bundle node scripts/check-bundle.mjs
run_logged security node scripts/check-security.mjs
run_logged sbom npm run sbom
run_logged supply-chain node scripts/validate-supply-chain.mjs
run_logged vulnerability-audit npm audit --omit=dev --audit-level=high
run_logged findings jq -e '[.findings[] | select(.status=="OPEN" and .severity=="P0")] | length == 0' module-findings.json
run_logged traceability jq -e '.module_id=="MOD-WEB-001" and (.requirements|length)>=20 and (.execution_bindings|length)>=10' requirements-traceability.json

MASI_WEB_OCI_EVIDENCE="${run_root}/oci.json" \
MASI_WEB_BROWSER_EVIDENCE="${run_root}/browser.json" \
MASI_WEB_PERFORMANCE_EVIDENCE="${run_root}/performance.json" \
MASI_WEB_SOAK_EVIDENCE="${run_root}/soak.json" \
MASI_WEB_SOAK_WARMUP_SECONDS=60 MASI_WEB_SOAK_PHASE_SECONDS=900 \
MASI_WEB_OCI_PORT="${MASI_WEB_OCI_PORT:-4181}" \
  run_logged real-oci-browser-performance-soak "${script_dir}/run-oci-smoke.sh"

source_tree_digest_end="$(calculate_source_tree_digest)"
run_logged source-integrity bash -c '[[ "$1" == "$2" ]]' _ "${source_tree_digest}" "${source_tree_digest_end}"

node "${script_dir}/build-module-summary.mjs" \
  --run-root "${run_root}" --run-id "${run_id}" --started-at "${started_at}" \
  --source-tree-digest "${source_tree_digest}" --working-tree-dirty "${working_tree_dirty}" \
  --working-tree-status-digest "${working_tree_status_digest}"

summary_digest="sha256:$(sha256sum "${run_root}/gate-summary.json" | awk '{print $1}')"
temporary_summary="$(mktemp "${evidence_base}/.gate-summary.${run_id}.XXXXXX")"
cp -- "${run_root}/gate-summary.json" "${temporary_summary}"
mv -T -- "${temporary_summary}" "${evidence_base}/gate-summary.json"
temporary_latest="$(mktemp "${evidence_base}/.latest.${run_id}.XXXXXX")"
jq -n --arg run_id "${run_id}" --arg evidence "runs/${run_id}/gate-summary.json" --arg digest "${summary_digest}" \
  '{schema_version:"web-spa-module-gate-latest/v1",run_id:$run_id,evidence:$evidence,digest:$digest}' >"${temporary_latest}"
mv -T -- "${temporary_latest}" "${evidence_base}/latest.json"
jq . "${run_root}/gate-summary.json"
