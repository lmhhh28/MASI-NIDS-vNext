#!/usr/bin/env bash
# shellcheck disable=SC2016  # nested bash -lc intentionally expands its own $0
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ctrl_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${ctrl_root}/.." && pwd)"
evidence_base_input="${MASI_CONTROL_EVIDENCE_DIR:-${ctrl_root}/evidence/module-gates}"
run_id="${MASI_CONTROL_GATE_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
if [[ ! "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ ]]; then
  echo "invalid module gate run id" >&2
  exit 64
fi
evidence_base_lexical="$(realpath -m -s -- "${evidence_base_input}")"
evidence_base_resolved="$(realpath -m -- "${evidence_base_input}")"
if [[ "${evidence_base_lexical}" != "${evidence_base_resolved}" \
  || "${evidence_base_lexical}" == "/" \
  || "${evidence_base_lexical}" == "${repo_root}" \
  || "${evidence_base_lexical}" == "${ctrl_root}" \
  || -L "${evidence_base_input}" \
  || ( -e "${evidence_base_input}" && ! -d "${evidence_base_input}" ) ]]; then
  echo "unsafe module gate evidence base" >&2
  exit 64
fi
evidence_base="${evidence_base_lexical}"
if [[ -L "${evidence_base}" || -L "${evidence_base}/runs" ]]; then
  echo "module gate evidence base contains a symbolic-link boundary" >&2
  exit 64
fi
mkdir -p -- "${evidence_base}"
if [[ -L "${evidence_base}" \
  || "$(realpath -e -- "${evidence_base}")" != "${evidence_base}" ]]; then
  echo "module gate evidence base contains a symbolic-link boundary" >&2
  exit 64
fi
if [[ ! -e "${evidence_base}/runs" ]]; then
  mkdir -- "${evidence_base}/runs"
fi
if [[ -L "${evidence_base}/runs" || ! -d "${evidence_base}/runs" \
  || "$(realpath -e -- "${evidence_base}/runs")" != "${evidence_base}/runs" ]]; then
  echo "module gate evidence base contains a symbolic-link boundary" >&2
  exit 64
fi
if [[ -L "${evidence_base}/gate-summary.json" || -L "${evidence_base}/latest.json" ]]; then
  echo "module gate publication target is a symbolic link" >&2
  exit 64
fi
evidence_root="${evidence_base}/runs/${run_id}"
blackbox_evidence="${evidence_root}/module-blackbox"
oci_evidence="${evidence_root}/oci-smoke"
formal_soak_evidence="${evidence_root}/formal-soak"
qualification_evidence="${evidence_root}/qualification"

if ! mkdir -- "${evidence_root}"; then
  echo "module gate run id already exists: ${run_id}" >&2
  exit 1
fi
mkdir -- "${blackbox_evidence}" "${oci_evidence}" "${formal_soak_evidence}" "${qualification_evidence}"
for created_directory in "${evidence_root}" "${blackbox_evidence}" "${oci_evidence}" \
  "${formal_soak_evidence}" "${qualification_evidence}"; do
  if [[ -L "${created_directory}" \
    || "$(realpath -e -- "${created_directory}")" != "${created_directory}" ]]; then
    echo "module gate evidence directory escaped its fresh run root" >&2
    exit 64
  fi
done

write_command_sidecar() {
  local log_name="$1"
  local sidecar="${evidence_root}/${log_name}.command.json"
  local temporary="${evidence_root}/.${log_name}.command.$$.json"
  [[ ! -e "${temporary}" && ! -L "${temporary}" ]] || return 1
  jq -n \
    --arg run_id "${run_id}" --arg command_id "${log_name}" \
    --arg started_at "${started_at}" --arg finished_at "${finished_at}" \
    --argjson duration_ms "${duration_ms}" --argjson exit_code "${command_status}" \
    --arg result "${result}" --arg qualification "${qualification}" \
    --arg stable_reason "${stable_reason}" --arg source_tree_digest "${source_tree_digest}" \
    --arg working_tree_status_digest "${working_tree_status_digest}" \
    --arg log_path "${log_name}.log" --arg log_digest "${log_digest}" \
    --argjson log_bytes "${log_bytes}" \
    --args '$ARGS.positional as $argv | {
      schema_version:"edge-command-execution/v1",run_id:$run_id,command_id:$command_id,
      started_at:$started_at,finished_at:$finished_at,duration_ms:$duration_ms,
      working_directory:"control-go",argv:$argv,exit_code:$exit_code,result:$result,
      qualification:$qualification,source_tree_digest:$source_tree_digest,
      working_tree_status_digest:$working_tree_status_digest,
      stable_reason:(if $stable_reason == "" then null else $stable_reason end),
      log:{path:$log_path,sha256:$log_digest,bytes:$log_bytes,media_type:"text/plain"}
    }' -- "${command[@]}" >"${temporary}" || return 1
  python3 "${script_dir}/validate-evidence.py" \
    --schema "${repo_root}/contracts/evidence/command/v1/schema.json" \
    --document "${temporary}" >/dev/null || return 1
  [[ ! -L "${sidecar}" ]] || return 1
  mv -T -- "${temporary}" "${sidecar}" || return 1
}

run_logged() {
  local log_name="$1"
  shift
  local -a command=("$@")
  local started_at finished_at started_ns finished_ns duration_ms command_status filter_status tee_status
  local result qualification stable_reason log_digest log_bytes
  local log_path="${evidence_root}/${log_name}.log"
  local temporary_log="${evidence_root}/.${log_name}.log.$$.tmp"
  local log_publish_status=0
  local restore_errexit=false
  if [[ "$-" == *e* ]]; then
    restore_errexit=true
  fi
  if [[ ! "${log_name}" =~ ^[a-z0-9][a-z0-9-]{1,63}$ \
    || -e "${log_path}" || -L "${log_path}" \
    || -e "${temporary_log}" || -L "${temporary_log}" ]]; then
    return 1
  fi
  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  started_ns="$(date +%s%N)"
  set +e
  timeout --signal=TERM --kill-after=30s \
    "${MASI_CONTROL_COMMAND_TIMEOUT_SECONDS:-7200}s" "${command[@]}" 2>&1 |
    awk -v max_bytes="${MASI_CONTROL_MAX_LOG_BYTES:-16777216}" '
      BEGIN { used=0; truncated=0 }
      {
        bytes=length($0)+1
        if (used+bytes <= max_bytes) { print; used+=bytes }
        else if (!truncated) {
          print "[MASI_LOG_TRUNCATED_AT_BYTE_LIMIT]"
          truncated=1
        }
      }
      { fflush() }
    ' | tee "${temporary_log}"
  local -a pipeline_status=("${PIPESTATUS[@]}")
  command_status="${pipeline_status[0]}"
  filter_status="${pipeline_status[1]}"
  tee_status="${pipeline_status[2]}"
  finished_ns="$(date +%s%N)"
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  duration_ms="$(((finished_ns - started_ns) / 1000000))"
  if [[ "${tee_status}" -ne 0 && "${command_status}" -eq 0 ]]; then
    command_status="${tee_status}"
  fi
  if [[ "${filter_status}" -ne 0 && "${command_status}" -eq 0 ]]; then
    command_status="${filter_status}"
  fi
  if ! mv -T -- "${temporary_log}" "${log_path}"; then
    command_status=1
    log_publish_status=1
    unlink -- "${temporary_log}" 2>/dev/null || true
  fi
  result="FAIL"
  qualification="NOT_QUALIFIED"
  stable_reason="COMMAND_EXITED_FAILURE"
  if [[ "${command_status}" -eq 0 ]]; then
    result="PASS"
    qualification="QUALIFIED"
    stable_reason=""
  elif [[ "${command_status}" -eq 2 ]]; then
    result="HOLD"
    stable_reason="COMMAND_EXITED_HOLD"
  fi
  if [[ "${log_publish_status}" -eq 0 ]]; then
    log_digest="sha256:$(sha256sum "${log_path}" | awk '{print $1}')"
    log_bytes="$(stat -c '%s' "${log_path}")"
  fi
  if [[ "${log_publish_status}" -ne 0 ]]; then
    command_status=1
    result="FAIL"
    qualification="NOT_QUALIFIED"
    stable_reason="COMMAND_EVIDENCE_WRITE_FAILURE"
    unlink -- "${evidence_root}/${log_name}.command.json" 2>/dev/null || true
    unlink -- "${evidence_root}/.${log_name}.command.$$.json" 2>/dev/null || true
  elif ! write_command_sidecar "${log_name}"; then
    command_status=1
    result="FAIL"
    qualification="NOT_QUALIFIED"
    stable_reason="COMMAND_EVIDENCE_WRITE_FAILURE"
    unlink -- "${evidence_root}/${log_name}.command.json" 2>/dev/null || true
    unlink -- "${evidence_root}/.${log_name}.command.$$.json" 2>/dev/null || true
    if ! write_command_sidecar "${log_name}"; then
      command_status=1
    fi
  fi
  if [[ "${restore_errexit}" == true ]]; then
    set -e
  else
    set +e
  fi
  return "${command_status}"
}

last_command_status=0
run_gate() {
  local restore_errexit=false
  if [[ "$-" == *e* ]]; then
    restore_errexit=true
  fi
  set +e
  run_logged "$@"
  last_command_status=$?
  if [[ "${restore_errexit}" == true ]]; then
    set -e
  else
    set +e
  fi
  return 0
}

recorded_result() {
  local log_name="$1"
  local sidecar="${evidence_root}/${log_name}.command.json"
  if [[ ! -f "${sidecar}" ]]; then
    printf 'NOT_RUN\n'
    return 0
  fi
  jq -er '.result' "${sidecar}" 2>/dev/null || printf 'FAIL\n'
}

record_not_run() {
  local log_name="$1"
  local stable_reason="$2"
  shift 2
  local -a command=("$@")
  local timestamp temporary record_status=0
  if [[ ! "${log_name}" =~ ^[a-z0-9][a-z0-9-]{1,63}$ \
    || -e "${evidence_root}/${log_name}.log" \
    || -L "${evidence_root}/${log_name}.log" \
    || -e "${evidence_root}/${log_name}.command.json" \
    || -L "${evidence_root}/${log_name}.command.json" ]]; then
    return 0
  fi
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '%s\n' "${stable_reason}" >"${evidence_root}/${log_name}.log" || record_status=1
  temporary="${evidence_root}/.${log_name}.command.$$.json"
  jq -n \
    --arg run_id "${run_id}" --arg command_id "${log_name}" \
    --arg timestamp "${timestamp}" --arg stable_reason "${stable_reason}" \
    --arg source_tree_digest "${source_tree_digest}" \
    --arg working_tree_status_digest "${working_tree_status_digest}" \
    --arg log_path "${log_name}.log" \
    --arg log_digest "sha256:$(sha256sum "${evidence_root}/${log_name}.log" | awk '{print $1}')" \
    --argjson log_bytes "$(stat -c '%s' "${evidence_root}/${log_name}.log")" \
    --args '$ARGS.positional as $argv | {
      schema_version:"edge-command-execution/v1",run_id:$run_id,command_id:$command_id,
      started_at:$timestamp,finished_at:$timestamp,duration_ms:0,
      working_directory:"control-go",argv:$argv,exit_code:null,result:"NOT_RUN",
      qualification:"NOT_QUALIFIED",source_tree_digest:$source_tree_digest,
      working_tree_status_digest:$working_tree_status_digest,stable_reason:$stable_reason,
      log:{path:$log_path,sha256:$log_digest,bytes:$log_bytes,media_type:"text/plain"}
    }' -- "${command[@]}" >"${temporary}" || record_status=1
  if [[ "${record_status}" -eq 0 ]] && ! python3 "${script_dir}/validate-evidence.py" \
      --schema "${repo_root}/contracts/evidence/command/v1/schema.json" \
      --document "${temporary}" >/dev/null; then
    record_status=1
  fi
  if [[ "${record_status}" -eq 0 ]]; then
    mv -T -- "${temporary}" "${evidence_root}/${log_name}.command.json" || record_status=1
  fi
  if [[ "${record_status}" -ne 0 ]]; then
    unlink -- "${temporary}" 2>/dev/null || true
  fi
  return 0
}

publish_latest() {
  local summary_digest summary_temporary latest_temporary
  if [[ -L "${evidence_base}/gate-summary.json" || -L "${evidence_base}/latest.json" ]]; then
    return 1
  fi
  summary_digest="sha256:$(sha256sum "${evidence_root}/gate-summary.json" | awk '{print $1}')" \
    || return 1
  summary_temporary="$(mktemp "${evidence_base}/.gate-summary.${run_id}.XXXXXX")" \
    || return 1
  if ! cp -- "${evidence_root}/gate-summary.json" "${summary_temporary}" \
    || [[ -L "${evidence_base}/gate-summary.json" ]] \
    || ! mv -T -- "${summary_temporary}" "${evidence_base}/gate-summary.json"; then
    unlink -- "${summary_temporary}" 2>/dev/null || true
    return 1
  fi
  latest_temporary="$(mktemp "${evidence_base}/.latest.${run_id}.XXXXXX")" || return 1
  if ! jq -n --arg run_id "${run_id}" --arg evidence "runs/${run_id}/gate-summary.json" \
    --arg digest "${summary_digest}" \
    '{schema_version:"control-core-module-gate-latest/v1",run_id:$run_id,evidence:$evidence,digest:$digest}' \
    >"${latest_temporary}" \
    || [[ -L "${evidence_base}/latest.json" ]] \
    || ! mv -T -- "${latest_temporary}" "${evidence_base}/latest.json"; then
    unlink -- "${latest_temporary}" 2>/dev/null || true
    return 1
  fi
}

cd -- "${ctrl_root}"
source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
baseline_git_tree="$(git -C "${repo_root}" rev-parse 'HEAD^{tree}')"
working_tree_status="$(git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)"
working_tree_dirty=false
if [[ -n "${working_tree_status}" ]]; then
  working_tree_dirty=true
fi
printf '%s\n' "${working_tree_status}" >"${evidence_root}/working-tree-status.txt"
working_tree_status_digest="sha256:$(sha256sum "${evidence_root}/working-tree-status.txt" | awk '{print $1}')"
source_archive="${evidence_root}/source-tree.tar"
tar --sort=name --mtime=@1786406400 --owner=0 --group=0 --numeric-owner \
  --exclude='control-go/evidence' \
  --exclude='control-go/**/__pycache__' --exclude='contracts/**/__pycache__' \
  -cf "${source_archive}" -C "${repo_root}" control-go contracts db
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
unlink -- "${source_archive}"

control_dsn="${MASI_CONTROL_E2E_DSN:-postgres://masi:masi@127.0.0.1:55433/masi_control_test?sslmode=disable}"
control_config="${MASI_CONTROL_E2E_CONFIG:-${ctrl_root}/testdata/control-e2e-config.json}"
control_binary="${MASI_CONTROL_E2E_BINARY:-/tmp/masi-control-gate}"
blackbox_e2e_endpoint="$(jq -r '.grpc_listen // empty' "${control_config}" 2>/dev/null || true)"
if [[ -z "${blackbox_e2e_endpoint}" ]]; then
  blackbox_e2e_endpoint="127.0.0.1:19090"
fi

# Language-level and contract gates.
run_gate go-version go version
run_gate public-contracts python3 "${script_dir}/validate-public-contracts.py" --repo "${repo_root}"
run_gate format bash -lc 'cd "${0}" && test -z "$(gofmt -l ./cmd ./internal ./tests)"' "${ctrl_root}"
run_gate vet bash -lc 'cd "${0}" && go vet ./...' "${ctrl_root}"
run_gate staticcheck bash -lc 'cd "${0}" && staticcheck ./...' "${ctrl_root}"
run_gate tests bash -lc 'cd "${0}" && go test ./...' "${ctrl_root}"
run_gate race bash -lc 'cd "${0}" && go test -race ./...' "${ctrl_root}"
run_gate coverage bash -lc 'cd "${0}" && go test -coverprofile "${1}/coverage.out" ./cmd/... ./internal/... ./tests/...' "${ctrl_root}" "${evidence_root}"

# Migration integration against the real PostgreSQL test instance. The migrate-test
# runner refuses any database whose name does not contain "test".
run_gate migration bash -lc 'cd "${0}" && go run ./cmd/migrate-test --dsn "${1}" --dir ../db/migrations' "${ctrl_root}" "${control_dsn}"

# Release binary used by the real-process black-box E2E and the formal soak.
run_gate release-build bash -lc 'cd "${0}" && go build -o /tmp/masi-control-gate ./cmd/control-core && echo release-build-ok' "${ctrl_root}"

# Real-process public-boundary black-box E2E: launches the real control-core binary,
# waits on /readyz, calls ControlSink.CommitResults over the public test-profile gRPC
# boundary, verifies committed/idempotent/conflict ACKs, checks PostgreSQL as the
# outcome oracle and confirms bounded graceful shutdown.
MASI_CONTROL_E2E_DSN="${control_dsn}" \
  MASI_CONTROL_E2E_BINARY="${control_binary}" \
  MASI_CONTROL_E2E_CONFIG="${control_config}" \
  MASI_CONTROL_E2E_REQUIRED=1 \
  run_gate blackbox-e2e bash -lc 'cd "${0}" && go test -v ./tests/process_e2e -run TestRealControlProcessCommitResults' "${ctrl_root}"
blackbox_e2e_status="${last_command_status}"
blackbox_e2e_result="PASS"
if [[ "${blackbox_e2e_status}" -ne 0 ]]; then
  blackbox_e2e_result="FAIL"
fi

# Postgres E2E suite: every internal package exercised against the real test DB
# (idempotency, overlay, dispatcher, model revision/rollout, plugin audit,
# pluginstat, fleet projection, target lifecycle/observation). Each test owns its
# own fixture cleanup; all are gated by MASI_CONTROL_E2E_REQUIRED=1.
MASI_CONTROL_E2E_DSN="${control_dsn}" \
  MASI_CONTROL_E2E_CONFIG="${control_config}" \
  MASI_CONTROL_E2E_REQUIRED=1 \
  run_gate postgres-e2e bash -lc 'cd "${0}" && go test -v ./internal/api ./internal/firewall ./internal/governance ./internal/model ./internal/plugin ./internal/pluginstat ./internal/target -run Postgres' "${ctrl_root}"

# OCI smoke: the production image builds and the control-core binary runs inside it.
oci_result="NOT_RUN"
oci_qualification="NOT_QUALIFIED"
if [[ "${MASI_CONTROL_SKIP_OCI:-0}" != "1" ]]; then
  run_gate oci-smoke bash -lc 'cd "${0}" && docker build -t masi-control-core:module-gates . >/dev/null 2>&1 && docker run --rm masi-control-core:module-gates --help >/dev/null 2>&1 && echo oci-ok' "${ctrl_root}"
  oci_status="${last_command_status}"
  oci_result="$(recorded_result oci-smoke)"
  if [[ "${oci_result}" == "PASS" ]]; then
    oci_qualification="QUALIFIED"
  fi
  if [[ "${oci_status}" -ne 0 && "${oci_result}" == "PASS" ]]; then
    oci_result="FAIL"
    oci_qualification="NOT_QUALIFIED"
  fi
else
  record_not_run oci-smoke OCI_GATE_EXPLICITLY_SKIPPED "docker build/run masi-control-core:module-gates"
  oci_result="$(recorded_result oci-smoke)"
  oci_qualification="NOT_QUALIFIED"
fi

# Formal soak (DEC-044). Only runs when MASI_CONTROL_FORMAL_SOAK=1; otherwise it is
# recorded NOT_RUN. The soak harness is a go test whose emitted formal-soak-evidence.json
# .result is authoritative: the test exits 0 when the harness ran, and the validation
# gate is the authority for the recorded result.
formal_soak_result="NOT_RUN"
formal_soak_qualification="NOT_QUALIFIED"
formal_soak_required_ms=3600000
formal_soak_measured_ms=0
formal_soak_level=""
formal_soak_evidence_file="${formal_soak_evidence}/formal-soak-evidence.json"
if [[ "${MASI_CONTROL_FORMAL_SOAK:-0}" == "1" ]]; then
  MASI_CONTROL_E2E_DSN="${control_dsn}" \
    MASI_CONTROL_E2E_BINARY="${control_binary}" \
    MASI_CONTROL_E2E_CONFIG="${control_config}" \
    MASI_CONTROL_E2E_REQUIRED=1 \
    MASI_CONTROL_SOAK_SECONDS="${MASI_CONTROL_SOAK_SECONDS:-3600}" \
    MASI_CONTROL_EVIDENCE_DIR="${formal_soak_evidence}" \
    run_gate formal-soak bash -lc 'cd "${0}" && go test -v -timeout 75m ./tests/soak -run TestFormalSoak' "${ctrl_root}"
  formal_soak_status="${last_command_status}"
  if [[ -f "${formal_soak_evidence_file}" ]] && python3 "${script_dir}/validate-soak-evidence.py" \
      --repo "${repo_root}" --evidence "${formal_soak_evidence_file}" >/dev/null; then
    run_gate formal-soak-validation python3 "${script_dir}/validate-soak-evidence.py" \
      --repo "${repo_root}" --evidence "${formal_soak_evidence_file}"
    formal_soak_result="$(jq -er '.result' "${formal_soak_evidence_file}" 2>/dev/null || printf 'FAIL\n')"
    formal_soak_qualification="$(jq -er '.qualification' "${formal_soak_evidence_file}" 2>/dev/null || printf 'NOT_QUALIFIED\n')"
    formal_soak_measured_ms="$(jq -er '.qualified_elapsed_ms' "${formal_soak_evidence_file}" 2>/dev/null || printf '0\n')"
    formal_soak_level="$(jq -er '.level' "${formal_soak_evidence_file}" 2>/dev/null || printf '')"
  else
    record_not_run formal-soak-validation FORMAL_SOAK_EVIDENCE_INVALID \
      "python3 ${script_dir}/validate-soak-evidence.py --evidence ${formal_soak_evidence_file}"
    formal_soak_result="FAIL"
  fi
  if [[ "${formal_soak_status}" -ne 0 ]]; then
    # The harness itself did not exit cleanly (crash/abort/timeout): that is a hard
    # failure regardless of any evidence file shape.
    formal_soak_result="FAIL"
    formal_soak_qualification="NOT_QUALIFIED"
  fi
  if [[ "${formal_soak_measured_ms}" -lt "${formal_soak_required_ms}" ]]; then
    if [[ "${formal_soak_result}" != "FAIL" ]]; then
      formal_soak_result="HOLD"
    fi
  fi
else
  record_not_run formal-soak FORMAL_SOAK_NOT_REQUESTED "go test ./tests/soak -run TestFormalSoak"
  record_not_run formal-soak-validation FORMAL_SOAK_NOT_REQUESTED "validate-soak-evidence.py"
fi

# Artifact digests.
go_mod_digest="sha256:$(sha256sum "${ctrl_root}/go.mod" | awk '{print $1}')"
edge_contract_digest="sha256:$(sha256sum "${repo_root}/contracts/edge/v1/edge.proto" | awk '{print $1}')"
control_adapter_digest="sha256:$(sha256sum "${repo_root}/contracts/control-adapter/v1/control_adapter.proto" | awk '{print $1}')"
module_findings_schema_digest="sha256:$(sha256sum "${repo_root}/contracts/evidence/module-findings/v1/schema.json" | awk '{print $1}')"
module_findings_registry_digest="sha256:$(sha256sum "${ctrl_root}/module-findings.json" | awk '{print $1}')"
traceability_digest="sha256:$(sha256sum "${ctrl_root}/requirements-traceability.json" | awk '{print $1}')"
requirements_digest="sha256:$(sha256sum "${repo_root}/docs/masi-nids-vnext-system-requirements-2026-08-09.md" | awk '{print $1}')"
qualification_soak_profile_digest="sha256:$(sha256sum "${repo_root}/contracts/profiles/v1/qualification-soak-3600s.json" | awk '{print $1}')"
soak_evidence_schema_digest="sha256:$(sha256sum "${repo_root}/contracts/evidence/soak/v1/schema.json" | awk '{print $1}')"
binary_digest=""
if [[ -f /tmp/masi-control-gate ]]; then
  binary_digest="sha256:$(sha256sum /tmp/masi-control-gate | awk '{print $1}')"
fi
formal_soak_evidence_digest=""
formal_soak_evidence_relative=""
if [[ -f "${formal_soak_evidence_file}" ]]; then
  formal_soak_evidence_digest="sha256:$(sha256sum "${formal_soak_evidence_file}" | awk '{print $1}')"
  formal_soak_evidence_relative="formal-soak/formal-soak-evidence.json"
fi
generated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

absolute_result="HOLD"
absolute_reason="DEC-001 Owner absolute thresholds are not frozen (control-core process thresholds are OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001)"
baseline_result="HOLD"
baseline_reason="protected commit/tag attestation and clean release tree are not present"
if [[ "${working_tree_dirty}" == false ]]; then
  baseline_reason="clean tree observed, but protected commit/tag attestation is not present"
fi

go_result="$(recorded_result go-version)"
public_contracts_result="$(recorded_result public-contracts)"
format_result="$(recorded_result format)"
vet_result="$(recorded_result vet)"
staticcheck_result="$(recorded_result staticcheck)"
tests_result="$(recorded_result tests)"
race_result="$(recorded_result race)"
coverage_result="$(recorded_result coverage)"
migration_result="$(recorded_result migration)"
release_build_result="$(recorded_result release-build)"
postgres_e2e_result="$(recorded_result postgres-e2e)"
formal_soak_execution_result="$(recorded_result formal-soak)"
formal_soak_validation_result="$(recorded_result formal-soak-validation)"

overall_status="HOLD"
for required_result in "${go_result}" "${public_contracts_result}" "${format_result}" \
  "${vet_result}" "${staticcheck_result}" "${tests_result}" "${race_result}" \
  "${coverage_result}" "${migration_result}" "${release_build_result}" \
  "${postgres_e2e_result}" "${oci_result}" "${formal_soak_result}"; do
  if [[ "${required_result}" == "FAIL" ]]; then
    overall_status="FAIL"
  fi
done
if [[ "${blackbox_e2e_result}" == "FAIL" ]]; then
  overall_status="FAIL"
fi

conditional_applicability="$(jq -c '.conditional_applicability' \
  "${ctrl_root}/requirements-traceability.json" 2>/dev/null || printf '[]\n')"
requirement_ids="$(jq -c '[.requirements[].requirement_id]' \
  "${ctrl_root}/requirements-traceability.json" 2>/dev/null || printf '["MOD-CTRL-001"]\n')"

module_complete=true

# DEC-044: the applicable 3600-second module soak must have actually executed with a
# valid, digest-bound evidence file. For control-core the process thresholds are
# owner-unfrozen, so the soak records HOLD/NOT_QUALIFIED rather than PASS; that is the
# module-complete candidate state. A hard failure (or no evidence at all) blocks it.
if [[ "${formal_soak_result}" != "PASS" && "${formal_soak_result}" != "HOLD" ]]; then
  module_complete=false
fi
if [[ -z "${formal_soak_evidence_digest}" ]]; then
  module_complete=false
fi
# The formal soak evidence must be a real MODULE run (not a short rehearsal) whose
# measured qualified window reaches the frozen 3600 seconds.
if [[ "${formal_soak_level}" != "MODULE" ]]; then
  module_complete=false
fi
if [[ "${formal_soak_measured_ms}" -lt "${formal_soak_required_ms}" ]]; then
  module_complete=false
fi
# DEC-044 operational completion additionally requires that the real public-boundary
# E2E actually ran and that no language/contract/migration/OCI gate failed.
if [[ "${blackbox_e2e_result}" != "PASS" ]]; then
  module_complete=false
fi
if [[ "${oci_result}" != "PASS" ]]; then
  module_complete=false
fi
for executed_result in "${go_result}" "${public_contracts_result}" "${format_result}" \
  "${vet_result}" "${staticcheck_result}" "${tests_result}" "${race_result}" \
  "${coverage_result}" "${migration_result}" "${release_build_result}" \
  "${postgres_e2e_result}" "${formal_soak_validation_result}"; do
  if [[ "${executed_result}" != "PASS" ]]; then
    module_complete=false
  fi
done
open_p0_findings="$(jq '[.findings[] | select(.status=="OPEN" and .severity=="P0")] | length' \
  "${ctrl_root}/module-findings.json" 2>/dev/null || printf '1\n')"
open_findings_total="$(jq '[.findings[] | select(.status=="OPEN")] | length' \
  "${ctrl_root}/module-findings.json" 2>/dev/null || printf '1\n')"
if [[ "${open_p0_findings}" != "0" ]]; then
  module_complete=false
fi

gate_summary_temporary="${evidence_root}/.gate-summary.$$.json"
set +e
jq -n \
  --arg run_id "${run_id}" --arg generated_at "${generated_at}" \
  --arg source_revision "${source_revision}" --arg baseline_git_tree "${baseline_git_tree}" \
  --arg source_tree_digest "${source_tree_digest}" \
  --argjson working_tree_dirty "${working_tree_dirty}" \
  --arg working_tree_status_digest "${working_tree_status_digest}" \
  --arg go_mod_digest "${go_mod_digest}" \
  --arg edge_contract_digest "${edge_contract_digest}" \
  --arg control_adapter_digest "${control_adapter_digest}" \
  --arg module_findings_schema_digest "${module_findings_schema_digest}" \
  --arg module_findings_registry_digest "${module_findings_registry_digest}" \
  --arg traceability_digest "${traceability_digest}" --arg requirements_digest "${requirements_digest}" \
  --arg qualification_soak_profile_digest "${qualification_soak_profile_digest}" \
  --arg soak_evidence_schema_digest "${soak_evidence_schema_digest}" \
  --arg binary_digest "${binary_digest}" \
  --arg formal_soak_evidence_digest "${formal_soak_evidence_digest}" \
  --arg formal_soak_evidence_relative "${formal_soak_evidence_relative}" \
  --arg go_result "${go_result}" --arg public_contracts_result "${public_contracts_result}" \
  --arg format_result "${format_result}" --arg vet_result "${vet_result}" \
  --arg staticcheck_result "${staticcheck_result}" --arg tests_result "${tests_result}" \
  --arg race_result "${race_result}" --arg coverage_result "${coverage_result}" \
  --arg migration_result "${migration_result}" --arg release_build_result "${release_build_result}" \
  --arg blackbox_e2e_result "${blackbox_e2e_result}" \
  --arg blackbox_e2e_endpoint "${blackbox_e2e_endpoint}" \
  --arg postgres_e2e_result "${postgres_e2e_result}" \
  --arg formal_soak_execution_result "${formal_soak_execution_result}" \
  --arg formal_soak_validation_result "${formal_soak_validation_result}" \
  --arg oci_result "${oci_result}" --arg oci_qualification "${oci_qualification}" \
  --arg formal_soak_result "${formal_soak_result}" \
  --arg formal_soak_qualification "${formal_soak_qualification}" \
  --arg absolute_result "${absolute_result}" --arg absolute_reason "${absolute_reason}" \
  --arg baseline_result "${baseline_result}" --arg baseline_reason "${baseline_reason}" \
  --arg open_findings_total "${open_findings_total}" --arg open_p0_findings "${open_p0_findings}" \
  --arg overall_status "${overall_status}" \
  --argjson module_complete "${module_complete}" --argjson requirement_ids "${requirement_ids}" \
  --argjson conditional_applicability "${conditional_applicability}" '{
    schema_version: "control-core-module-gate-summary/v1",
    test_id: "TEST-CTRL-MODULE-GATES-001",
    requirement_ids: $requirement_ids,
    module_id: "MOD-CTRL-001",
    run_id: $run_id,
    generated_at: $generated_at,
    level: "MODULE",
    applicability: "APPLICABLE",
    result: $overall_status,
    qualification: "NOT_QUALIFIED",
    source_revision: $source_revision,
    baseline_git_tree: $baseline_git_tree,
    source_tree_digest: $source_tree_digest,
    working_tree_dirty: $working_tree_dirty,
    working_tree_status_digest: $working_tree_status_digest,
    artifact_digests: {
      go_mod: $go_mod_digest,
      edge_contract: $edge_contract_digest,
      control_adapter_contract: $control_adapter_digest,
      module_findings_schema: $module_findings_schema_digest,
      module_findings_registry: $module_findings_registry_digest,
      traceability_manifest: $traceability_digest,
      requirements_baseline: $requirements_digest,
      qualification_soak_profile: $qualification_soak_profile_digest,
      soak_evidence_schema: $soak_evidence_schema_digest,
      release_binary: (if $binary_digest == "" then null else $binary_digest end),
      oci_evidence: null,
      formal_soak_evidence: (if $formal_soak_evidence_digest == "" then null else $formal_soak_evidence_digest end)
    },
    executed_gates: {
      go_version: $go_result,
      public_contract_schema_golden_negative: $public_contracts_result,
      format: $format_result,
      vet: $vet_result,
      staticcheck: $staticcheck_result,
      unit_property_contract_golden: $tests_result,
      race: $race_result,
      coverage: $coverage_result,
      migration_integration: $migration_result,
      release_build: $release_build_result,
      real_process_public_boundary_blackbox: $blackbox_e2e_result,
      postgres_e2e: $postgres_e2e_result,
      oci_startup: {result:$oci_result,qualification:$oci_qualification},
      formal_soak_execution: $formal_soak_execution_result,
      formal_soak_validation: $formal_soak_validation_result
    },
    conditional_applicability: $conditional_applicability,
    qualification_gates: {
      absolute_performance: {result:$absolute_result,qualification:"NOT_QUALIFIED",reason:$absolute_reason},
      formal_soak_3600_seconds: {
        result:$formal_soak_result,qualification:$formal_soak_qualification,
        command:"MASI_CONTROL_FORMAL_SOAK=1 scripts/run-module-gates.sh",
        evidence:(if $formal_soak_evidence_relative == "" then null else $formal_soak_evidence_relative end),
        evidence_digest:(if $formal_soak_evidence_digest == "" then null else $formal_soak_evidence_digest end)
      },
      protected_release_baseline: {result:$baseline_result,qualification:"NOT_QUALIFIED",reason:$baseline_reason},
      formal_pairwise_and_system_integration: {
        result:"NOT_RUN",qualification:"NOT_QUALIFIED",
        reason:"intentionally outside independent module implementation phase"
      }
    },
    requirement_traceability: {
      manifest:"control-go/requirements-traceability.json",digest:$traceability_digest,
      requirement_count:($requirement_ids | length)
    },
    overall_module_complete: $module_complete,
    overall_status: $overall_status,
    blackbox_e2e: {result: $blackbox_e2e_result, endpoint: $blackbox_e2e_endpoint},
    findings: {open_total: ($open_findings_total | tonumber), open_p0: ($open_p0_findings | tonumber)}
  }' >"${gate_summary_temporary}"
gate_summary_status=$?
set -e
if [[ "${gate_summary_status}" -eq 0 ]] && ! python3 "${script_dir}/validate-evidence.py" \
    --schema "${repo_root}/contracts/evidence/v1/control-module-schema.json" \
    --document "${gate_summary_temporary}" --kind module >/dev/null; then
  gate_summary_status=1
fi
if [[ "${gate_summary_status}" -ne 0 ]] \
  || ! mv -T -- "${gate_summary_temporary}" "${evidence_root}/gate-summary.json"; then
  echo "failed to produce the module gate summary" >&2
  exit 1
fi

jq . "${evidence_root}/gate-summary.json" || true
publish_status=0
set +e
publish_latest
publish_status=$?
set -e
if [[ "${overall_status}" == "FAIL" || "${publish_status}" -ne 0 ]]; then
  exit 1
fi
if [[ "${module_complete}" == true ]]; then
  exit 0
fi
exit 2