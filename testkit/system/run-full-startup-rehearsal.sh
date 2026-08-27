#!/usr/bin/env bash
# shellcheck disable=SC2016 # nested bash expands its positional runtime path
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"
run_id="${MASI_SYSTEM_STARTUP_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
evidence_root="${MASI_SYSTEM_STARTUP_EVIDENCE_DIR:-${repo_root}/evidence/system-startup-rehearsal/${run_id}}"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [[ ! "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ || -e "${evidence_root}" || -L "${evidence_root}" ]]; then
  echo "invalid run id or existing system startup evidence root" >&2
  exit 64
fi
mkdir -p -- "${evidence_root}/components" "${evidence_root}/logs"
source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
component_failures=0

run_component() {
  local component_id="$1"
  local neighbor_mode="$2"
  local boundary_scope="$3"
  shift 3
  local log_path="${evidence_root}/logs/${component_id}.log"
  local started_ns finished_ns duration_ms status result
  started_ns="$(date +%s%N)"
  set +e
  timeout --signal=TERM --kill-after=60s "${MASI_SYSTEM_COMPONENT_TIMEOUT_SECONDS:-10800}s" "$@" >"${log_path}" 2>&1
  status=$?
  set -e
  finished_ns="$(date +%s%N)"
  duration_ms="$(((finished_ns - started_ns) / 1000000))"
  result="PASS"
  if [[ "${status}" -eq 2 ]]; then
    result="HOLD"
  elif [[ "${status}" -ne 0 ]]; then
    result="FAIL"
    component_failures=$((component_failures + 1))
  fi
  jq -n --arg component_id "${component_id}" --arg neighbor_mode "${neighbor_mode}" \
    --arg boundary_scope "${boundary_scope}" --arg result "${result}" \
    --arg log_digest "sha256:$(sha256sum "${log_path}" | awk '{print $1}')" \
    --argjson duration_ms "${duration_ms}" --argjson exit_code "${status}" \
    '{component_id:$component_id,actual_runtime:true,boundary_scope:$boundary_scope,
      neighbor_mode:$neighbor_mode,duration_ms:$duration_ms,exit_code:$exit_code,
      result:$result,log_digest:$log_digest}' >"${evidence_root}/components/${component_id}.json"
}

run_component p4-switch isolated "Real BMv2/P4Runtime, PTF packet/counter/action oracle and Mininet rehearsal" \
  env MASI_P4_RUN_ID="${run_id}-p4" MASI_P4_QUALIFICATION_MODE=rehearsal \
  bash -c 'umask 0022; exec "$1"' _ "${repo_root}/testkit/p4_switch/run-module-e2e.sh"

run_component edge isolated "Real hardened Edge OCI and authenticated public status boundary; no assignment injected" \
  env MASI_EDGE_EVIDENCE_DIR="${evidence_root}/edge" MASI_EDGE_IMAGE_REF="masi-edge:system-${run_id}" \
  "${repo_root}/edge-rs/scripts/run-oci-smoke.sh"

run_component central-inference real "Real hardened Gateway OCI, digest-pinned mTLS Triton/ORT CPU and GetBinding" \
  env MASI_INF_EVIDENCE_DIR="${evidence_root}/central-inference" \
  MASI_INF_IMAGE_REF="masi-inference:system-${run_id}" MASI_INF_BUILDER_IMAGE_REF="masi-inference-builder:system-${run_id}" \
  "${repo_root}/infer-cpp/scripts/run-oci-smoke.sh"

run_component control-postgresql-web real "Real PostgreSQL 18, migrated Go Control OCI, production Web OCI and three-browser P9" \
  env MASI_P9_RUN_ID="${run_id}-p9" MASI_P9_EVIDENCE_DIR="${evidence_root}/p9" \
  "${repo_root}/testkit/system/run-web-control-pairwise.sh"

mkdir -p -- "${evidence_root}/go-analysis"
run_component go-analysis-pairwise external-fixture "Real Control Core and PostgreSQL drive real Analysis A2A over TLS1.3 mTLS; external provider/MCP fixtures only" \
  "${repo_root}/analysis-py/.venv/bin/python" "${repo_root}/testkit/system/run-go-analysis-pairwise.py" \
  --run-id "${run_id}-p11" --evidence "${evidence_root}/go-analysis/summary.json"

run_component plugin-host external-fixture "Real hardened Plugin Host OCI, mTLS manager allowlist and health boundary" \
  env MASI_PLUGIN_HOST_OCI_EVIDENCE_DIR="${evidence_root}/plugin-host" \
  MASI_PLUGIN_HOST_IMAGE_REF="masi-plugin-host:system-${run_id}" \
  "${repo_root}/plugin-host-rs/scripts/run-oci-smoke.sh"

run_component plugin-statistics-wasm external-fixture "Real Plugin Host process executes qualified bounded Wasm statistics fixture" \
  bash -lc 'cd "$1" && cargo build --locked --release --bins >/dev/null && MASI_PLUGIN_HOST_BINARY="$1/target/release/masi-plugin-host" MASI_PLUGIN_SERVICE_FIXTURE_BINARY="$1/target/release/masi-plugin-service-fixture" cargo test --locked --release --test module_blackbox -- --nocapture --test-threads=1' \
  _ "${repo_root}/plugin-host-rs"

mkdir -p -- "${evidence_root}/analysis"
run_component analysis-agent external-fixture "Real hardened Analysis OCI completes A2A task using deterministic external provider/MCP fixtures" \
  "${repo_root}/analysis-py/.venv/bin/python" "${repo_root}/analysis-py/scripts/run-oci-smoke.py" \
  --image masi-analysis:module-gates --allow-local-candidate --evidence "${evidence_root}/analysis/oci.json"

mkdir -p -- "${evidence_root}/offline-ml"
run_component offline-ml isolated "Real hardened terminating Offline ML OCI reproduces and verifies immutable CPU repository" \
  "${repo_root}/ml-py/.venv/bin/python" "${repo_root}/ml-py/scripts/run-oci-smoke.py" \
  --image masi-offline-ml:offline-ml-formal-20260826-001 \
  --expected-blackbox "${repo_root}/ml-py/evidence/module-gates/runs/offline-ml-formal-20260826-001/blackbox.json" \
  --evidence "${evidence_root}/offline-ml/oci.json"

finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
components="$(jq -s 'sort_by(.component_id)' "${evidence_root}"/components/*.json)"
operational_startup_result="PASS"
top_result="HOLD"
if [[ "${component_failures}" -ne 0 ]]; then operational_startup_result="FAIL"; top_result="FAIL"; fi
jq -n --arg run_id "${run_id}" --arg started_at "${started_at}" --arg finished_at "${finished_at}" \
  --arg source_revision "${source_revision}" --arg operational_startup_result "${operational_startup_result}" \
  --arg result "${top_result}" --argjson components "${components}" '{
    schema_version:"system-startup-rehearsal/v1",run_id:$run_id,started_at:$started_at,finished_at:$finished_at,
    source_revision:$source_revision,profile:"full-deployable-startup-rehearsal/v1",components:$components,
    real_connected_boundaries:["P4_BMV2_PACKET_ORACLE","GATEWAY_TRITON_ORT_CPU","GO_POSTGRESQL","GO_WEB_THREE_BROWSER","GO_ANALYSIS_A2A_MTLS","ANALYSIS_A2A_EXTERNAL_FIXTURE_MTLS","PLUGIN_HOST_WASM_EXECUTION"],
    remaining_unintegrated_boundaries:[
      "Edge, BMv2, Central and Go were started but not wired into one real mTLS detection graph",
      "Go Plugin Manager did not drive the real Host statistics executor",
      "Twelve formal pairwise boundaries and ten system waves remain separately NOT_RUN"
    ],
    operational_startup_result:$operational_startup_result,level:"REHEARSAL",applicability:"APPLICABLE",
    result:$result,qualification:"NOT_QUALIFIED",
    qualification_scope:"REAL_DEPLOYABLE_STARTUP_AND_PUBLIC_BOUNDARY_REHEARSAL; NOT_ONE_CONNECTED_SYSTEM; NO_PAIRWISE_OR_SYSTEM_PASS",
    full_system_e2e:false
  }' >"${evidence_root}/summary.json"

"${repo_root}/analysis-py/.venv/bin/python" - "${repo_root}/contracts/evidence/system-startup-rehearsal/v1/schema.json" "${evidence_root}/summary.json" <<'PY'
import json, sys
from jsonschema import Draft202012Validator, FormatChecker
schema=json.load(open(sys.argv[1], encoding="utf-8"))
document=json.load(open(sys.argv[2], encoding="utf-8"))
errors=sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document), key=lambda e:list(e.path))
if errors:
    raise SystemExit("\n".join(f"{list(e.path)}: {e.message}" for e in errors))
PY
jq . "${evidence_root}/summary.json"
if [[ "${component_failures}" -ne 0 ]]; then exit 1; fi
exit 2
