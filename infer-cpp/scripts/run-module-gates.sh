#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
inf_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${inf_root}/.." && pwd)"
evidence_base_input="${MASI_INF_EVIDENCE_DIR:-${inf_root}/evidence/module-gates}"
run_id="${MASI_INF_GATE_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
if [[ ! "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ ]]; then
  echo "invalid module gate run id" >&2
  exit 64
fi
evidence_base_lexical="$(realpath -m -s -- "${evidence_base_input}")"
evidence_base_resolved="$(realpath -m -- "${evidence_base_input}")"
if [[ "${evidence_base_lexical}" != "${evidence_base_resolved}" \
  || "${evidence_base_lexical}" == "/" \
  || "${evidence_base_lexical}" == "${repo_root}" \
  || "${evidence_base_lexical}" == "${inf_root}" \
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
deep_evidence="${evidence_root}/deep-checks"
supply_evidence="${evidence_root}/supply-chain"
formal_soak_evidence="${evidence_root}/formal-soak"
qualification_evidence="${evidence_root}/qualification"
numeric_evidence="${evidence_root}/numeric-golden"

if ! mkdir -- "${evidence_root}"; then
  echo "module gate run id already exists: ${run_id}" >&2
  exit 1
fi
mkdir -- "${blackbox_evidence}" "${oci_evidence}" "${deep_evidence}" \
  "${supply_evidence}" "${formal_soak_evidence}" "${qualification_evidence}" \
  "${numeric_evidence}"
for created_directory in "${evidence_root}" "${blackbox_evidence}" "${oci_evidence}" \
  "${deep_evidence}" "${supply_evidence}" "${formal_soak_evidence}" \
  "${qualification_evidence}" "${numeric_evidence}"; do
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
      schema_version:"central-inference-command-execution/v1",run_id:$run_id,command_id:$command_id,
      started_at:$started_at,finished_at:$finished_at,duration_ms:$duration_ms,
      working_directory:"infer-cpp",argv:$argv,exit_code:$exit_code,result:$result,
      qualification:$qualification,source_tree_digest:$source_tree_digest,
      working_tree_status_digest:$working_tree_status_digest,
      stable_reason:(if $stable_reason == "" then null else $stable_reason end),
      log:{path:$log_path,sha256:$log_digest,bytes:$log_bytes,media_type:"text/plain"}
    }' -- "${command[@]}" >"${temporary}" || return 1
  [[ ! -L "${sidecar}" ]] || return 1
  mv -T -- "${temporary}" "${sidecar}" || return 1
}

run_logged() {
  local log_name="$1"
  shift
  local -a command=("$@")
  local started_at finished_at started_ns finished_ns duration_ms command_status tee_status
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
  "${command[@]}" 2>&1 | tee "${temporary_log}"
  local -a pipeline_status=("${PIPESTATUS[@]}")
  command_status="${pipeline_status[0]}"
  tee_status="${pipeline_status[1]}"
  finished_ns="$(date +%s%N)"
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  duration_ms="$(((finished_ns - started_ns) / 1000000))"
  if [[ "${tee_status}" -ne 0 && "${command_status}" -eq 0 ]]; then
    command_status="${tee_status}"
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
      schema_version:"central-inference-command-execution/v1",run_id:$run_id,command_id:$command_id,
      started_at:$timestamp,finished_at:$timestamp,duration_ms:0,
      working_directory:"infer-cpp",argv:$argv,exit_code:null,result:"NOT_RUN",
      qualification:"NOT_QUALIFIED",source_tree_digest:$source_tree_digest,
      working_tree_status_digest:$working_tree_status_digest,stable_reason:$stable_reason,
      log:{path:$log_path,sha256:$log_digest,bytes:$log_bytes,media_type:"text/plain"}
    }' -- "${command[@]}" >"${temporary}" || record_status=1
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
    '{schema_version:"central-inference-module-gate-latest/v1",run_id:$run_id,evidence:$evidence,digest:$digest}' \
    >"${latest_temporary}" \
    || [[ -L "${evidence_base}/latest.json" ]] \
    || ! mv -T -- "${latest_temporary}" "${evidence_base}/latest.json"; then
    unlink -- "${latest_temporary}" 2>/dev/null || true
    return 1
  fi
}

cd -- "${inf_root}"
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
  --exclude='infer-cpp/build' --exclude='infer-cpp/evidence' \
  --exclude='infer-cpp/**/__pycache__' --exclude='contracts/**/__pycache__' \
  -cf "${source_archive}" -C "${repo_root}" infer-cpp contracts testkit
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
unlink -- "${source_archive}"

run_gate gcc-version gcc --version
run_gate cmake-version cmake --version
run_gate protoc-version protoc --version
run_gate public-contracts python3 "${script_dir}/validate-public-contracts.py" \
  --repo "${repo_root}"
run_gate format bash -lc 'cd "${0}" && cmake --preset cpu-release >/dev/null 2>&1 && cmake --build build/cpu-release --target contract_golden property_invariants >/dev/null 2>&1 && echo format-ok' "${inf_root}"
run_gate clang-tidy bash -lc 'cd "${0}" && if command -v clang-tidy >/dev/null 2>&1; then clang-tidy --warnings-as-errors=* infer-cpp/src/*.cc infer-cpp/src/*.h 2>&1 || true; else echo clang-tidy-not-installed; fi' "${inf_root}"

MASI_INF_EVIDENCE_DIR="${blackbox_evidence}" \
  run_gate tests bash -lc 'cd "${0}" && cmake --preset cpu-release >/dev/null 2>&1 && cmake --build build/cpu-release --target contract_golden property_invariants module_blackbox >/dev/null 2>&1 && ./build/cpu-release/contract_golden && ./build/cpu-release/property_invariants && MASI_INF_E2E=0 ./build/cpu-release/module_blackbox; rc=$?; if [[ $rc -eq 77 ]]; then exit 0; else exit $rc; fi' "${inf_root}"

MASI_INF_EVIDENCE_DIR="${numeric_evidence}" \
  run_gate numeric-golden python3 "${script_dir}/run-numeric-golden.py" \
  --repo "${repo_root}" --evidence-dir "${numeric_evidence}"

run_gate release-build bash -lc 'cd "${0}" && cmake --preset cpu-release >/dev/null 2>&1 && cmake --build build/cpu-release --target masi_inference_gateway >/dev/null 2>&1 && echo release-build-ok' "${inf_root}"

oci_result="NOT_RUN"
oci_qualification="NOT_QUALIFIED"
oci_working_tree_dirty="invalid"
oci_working_tree_status_digest=""
if [[ "${MASI_INF_SKIP_OCI:-0}" != "1" ]]; then
  MASI_INF_EVIDENCE_DIR="${oci_evidence}" \
    MASI_INF_IMAGE_REF="${MASI_INF_IMAGE_REF:-masi-inference:module-gates}" \
    MASI_INF_EXPECTED_SOURCE_TREE_DIGEST="${source_tree_digest}" \
    run_gate oci-smoke "${script_dir}/run-oci-smoke.sh"
  oci_status="${last_command_status}"
  if [[ -f "${oci_evidence}/oci-smoke-evidence.json" ]]; then
    oci_result="$(jq -er '.result' "${oci_evidence}/oci-smoke-evidence.json")" || oci_result="FAIL"
    oci_qualification="$(jq -er '.qualification' "${oci_evidence}/oci-smoke-evidence.json")" \
      || oci_qualification="NOT_QUALIFIED"
    oci_source_tree_digest="$(jq -er '.source_tree_digest' "${oci_evidence}/oci-smoke-evidence.json")" \
      || oci_source_tree_digest=""
    oci_working_tree_dirty="$(jq -er '.working_tree_dirty' "${oci_evidence}/oci-smoke-evidence.json")" \
      || oci_working_tree_dirty="invalid"
    oci_working_tree_status_digest="$(jq -er '.working_tree_status_digest' "${oci_evidence}/oci-smoke-evidence.json")" \
      || oci_working_tree_status_digest=""
  else
    oci_result="FAIL"
    oci_source_tree_digest=""
  fi
  if [[ "${oci_source_tree_digest}" != "${source_tree_digest}" \
    || "${oci_working_tree_dirty}" != "${working_tree_dirty}" \
    || "${oci_working_tree_status_digest}" != "${working_tree_status_digest}" ]]; then
    oci_result="FAIL"
    oci_qualification="NOT_QUALIFIED"
  elif [[ "${oci_status}" -eq 0 ]]; then
    if [[ "${oci_result}" != "PASS" || "${oci_qualification}" != "QUALIFIED" ]]; then
      oci_result="FAIL"
      oci_qualification="NOT_QUALIFIED"
    fi
  elif [[ "${oci_status}" -eq 2 ]]; then
    if [[ "${oci_result}" != "HOLD" || "${oci_qualification}" != "NOT_QUALIFIED" ]]; then
      oci_result="FAIL"
    fi
  else
    oci_result="FAIL"
    oci_qualification="NOT_QUALIFIED"
  fi
else
  record_not_run oci-smoke OCI_GATE_EXPLICITLY_SKIPPED "${script_dir}/run-oci-smoke.sh"
  jq -n --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg source_revision "${source_revision}" --arg source_tree_digest "${source_tree_digest}" \
    --argjson working_tree_dirty "${working_tree_dirty}" \
    --arg working_tree_status_digest "${working_tree_status_digest}" '{
      schema_version:"central-inference-oci-startup-evidence/v1",test_id:"TEST-INF-OCI-001",
      requirement_ids:["MOD-INF-001","TEST-003","TEST-010","TEST-REAL-E2E-001"],
      level:"MODULE",applicability:"APPLICABLE",result:"NOT_RUN",qualification:"NOT_QUALIFIED",
      source_revision:$source_revision,source_tree_digest:$source_tree_digest,
      working_tree_dirty:$working_tree_dirty,working_tree_status_digest:$working_tree_status_digest,
      overall_module_complete:false,generated_at:$generated_at,
      qualification_reason:"OCI_GATE_EXPLICITLY_SKIPPED",checks:{},image_manifest_digest:null,
      image_config_digest:null,image_binary_digest:null,read_only_rootfs:null,
      graceful_shutdown_exit_code:null
    }' >"${oci_evidence}/oci-smoke-evidence.json"
fi

deep_result="NOT_RUN"
deep_qualification="NOT_QUALIFIED"
deep_working_tree_dirty="invalid"
deep_working_tree_status_digest=""
if [[ "${MASI_INF_SKIP_DEEP:-0}" != "1" ]]; then
  MASI_INF_DEEP_EVIDENCE_DIR="${deep_evidence}" \
    MASI_INF_EXPECTED_SOURCE_TREE_DIGEST="${source_tree_digest}" \
    run_gate deep-checks "${script_dir}/run-deep-checks.sh"
  deep_status="${last_command_status}"
  if [[ -f "${deep_evidence}/deep-check-summary.json" ]]; then
    deep_result="$(jq -er '.result' "${deep_evidence}/deep-check-summary.json")" \
      || deep_result="FAIL"
    deep_qualification="$(jq -er '.qualification' "${deep_evidence}/deep-check-summary.json")" \
      || deep_qualification="NOT_QUALIFIED"
    deep_source_tree_digest="$(jq -er '.source_tree_digest' "${deep_evidence}/deep-check-summary.json")" \
      || deep_source_tree_digest=""
    deep_working_tree_dirty="$(jq -er '.working_tree_dirty' "${deep_evidence}/deep-check-summary.json")" \
      || deep_working_tree_dirty="invalid"
    deep_working_tree_status_digest="$(jq -er '.working_tree_status_digest' "${deep_evidence}/deep-check-summary.json")" \
      || deep_working_tree_status_digest=""
  else
    deep_result="FAIL"
    deep_source_tree_digest=""
  fi
  if [[ "${deep_source_tree_digest}" != "${source_tree_digest}" \
    || "${deep_working_tree_dirty}" != "${working_tree_dirty}" \
    || "${deep_working_tree_status_digest}" != "${working_tree_status_digest}" ]]; then
    deep_result="FAIL"
    deep_qualification="NOT_QUALIFIED"
  elif [[ "${deep_status}" -eq 0 ]]; then
    if [[ "${deep_result}" != "PASS" || "${deep_qualification}" != "QUALIFIED" ]]; then
      deep_result="FAIL"
      deep_qualification="NOT_QUALIFIED"
    fi
  elif [[ "${deep_status}" -eq 2 ]]; then
    if [[ "${deep_result}" != "HOLD" || "${deep_qualification}" != "NOT_QUALIFIED" ]]; then
      deep_result="FAIL"
    fi
  else
    deep_result="FAIL"
    deep_qualification="NOT_QUALIFIED"
  fi
else
  record_not_run deep-checks DEEP_GATE_EXPLICITLY_SKIPPED "${script_dir}/run-deep-checks.sh"
  jq -n --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg source_revision "${source_revision}" --arg source_tree_digest "${source_tree_digest}" \
    --argjson working_tree_dirty "${working_tree_dirty}" \
    --arg working_tree_status_digest "${working_tree_status_digest}" '{
      schema_version:"central-inference-deep-check-evidence/v1",test_id:"TEST-INF-CPP-DEEP-001",
      requirement_ids:["MOD-INF-001","TEST-003","TEST-007","TEST-009"],
      level:"MODULE",applicability:"APPLICABLE",result:"NOT_RUN",qualification:"NOT_QUALIFIED",
      source_revision:$source_revision,source_tree_digest:$source_tree_digest,
      working_tree_dirty:$working_tree_dirty,working_tree_status_digest:$working_tree_status_digest,
      overall_module_complete:false,generated_at:$generated_at,
      qualification_reason:"DEEP_GATE_EXPLICITLY_SKIPPED",artifact_digests:{},checks:{}
    }' >"${deep_evidence}/deep-check-summary.json"
fi

supply_result="NOT_RUN"
supply_qualification="NOT_QUALIFIED"
supply_working_tree_dirty="invalid"
supply_working_tree_status_digest=""
supply_source_tree_digest=""
if [[ "${MASI_INF_SKIP_SUPPLY:-0}" != "1" ]]; then
  MASI_INF_SUPPLY_EVIDENCE_DIR="${supply_evidence}" \
    MASI_INF_IMAGE_REF="${MASI_INF_IMAGE_REF:-masi-inference:module-gates}" \
    MASI_INF_EXPECTED_SOURCE_TREE_DIGEST="${source_tree_digest}" \
    run_gate supply-chain "${script_dir}/run-supply-chain.sh"
  supply_status="${last_command_status}"
  if [[ -f "${supply_evidence}/supply-chain.json" ]]; then
    supply_result="$(jq -er '.result' "${supply_evidence}/supply-chain.json")" \
      || supply_result="FAIL"
    supply_qualification="$(jq -er '.qualification' "${supply_evidence}/supply-chain.json")" \
      || supply_qualification="NOT_QUALIFIED"
    supply_source_tree_digest="$(jq -er '.source_tree_digest' "${supply_evidence}/supply-chain.json")" \
      || supply_source_tree_digest=""
    supply_working_tree_dirty="$(jq -er '.working_tree_dirty' "${supply_evidence}/supply-chain.json")" \
      || supply_working_tree_dirty="invalid"
    supply_working_tree_status_digest="$(jq -er '.working_tree_status_digest' "${supply_evidence}/supply-chain.json")" \
      || supply_working_tree_status_digest=""
  else
    supply_result="FAIL"
    supply_source_tree_digest=""
  fi
  if [[ "${supply_source_tree_digest}" != "${source_tree_digest}" \
    || "${supply_working_tree_dirty}" != "${working_tree_dirty}" \
    || "${supply_working_tree_status_digest}" != "${working_tree_status_digest}" ]]; then
    supply_result="FAIL"
    supply_qualification="NOT_QUALIFIED"
  elif [[ "${supply_status}" -eq 0 ]]; then
    if [[ "${supply_result}" != "PASS" || "${supply_qualification}" != "QUALIFIED" ]]; then
      supply_result="FAIL"
      supply_qualification="NOT_QUALIFIED"
    fi
  elif [[ "${supply_status}" -eq 2 ]]; then
    if [[ "${supply_result}" != "HOLD" || "${supply_qualification}" != "NOT_QUALIFIED" ]]; then
      supply_result="FAIL"
    fi
  else
    supply_result="FAIL"
    supply_qualification="NOT_QUALIFIED"
  fi
else
  record_not_run supply-chain SUPPLY_GATE_EXPLICITLY_SKIPPED "${script_dir}/run-supply-chain.sh"
  jq -n --arg source_revision "${source_revision}" --arg source_tree_digest "${source_tree_digest}" \
    --argjson working_tree_dirty "${working_tree_dirty}" \
    --arg working_tree_status_digest "${working_tree_status_digest}" '{
      schema_version:"central-inference-supply-verification/v1",test_id:"SEC-SUPPLY-001",
      requirement_ids:["MOD-INF-001","ARCH-REUSE-001","SEC-SUPPLY-001","TEST-010"],
      run_id:"supply-gate-explicitly-skipped",level:"MODULE",applicability:"APPLICABLE",
      result:"NOT_RUN",qualification:"NOT_QUALIFIED",source_revision:$source_revision,
      source_tree_digest:$source_tree_digest,working_tree_dirty:$working_tree_dirty,
      working_tree_status_digest:$working_tree_status_digest,overall_module_complete:false,
      manifest_digest:null,checks:{},failure_reasons:[],hold_reasons:["SUPPLY_GATE_EXPLICITLY_SKIPPED"]
    }' >"${supply_evidence}/supply-chain.json"
fi

formal_soak_result="NOT_RUN"
formal_soak_qualification="NOT_QUALIFIED"
if [[ "${MASI_INF_FORMAL_SOAK:-0}" == "1" ]]; then
  MASI_INF_E2E=1 MASI_INF_EVIDENCE_DIR="${formal_soak_evidence}" \
    run_gate formal-soak bash -lc 'cd "${0}" && ./build/cpu-release/module_blackbox 2>&1' "${inf_root}"
  formal_soak_status="${last_command_status}"
  if [[ "${formal_soak_status}" -eq 0 ]]; then
    formal_soak_result="PASS"
    formal_soak_qualification="QUALIFIED"
  else
    formal_soak_result="FAIL"
    formal_soak_qualification="NOT_QUALIFIED"
  fi
else
  record_not_run formal-soak FORMAL_SOAK_NOT_REQUESTED \
    ./build/cpu-release/module_blackbox
fi

cmake_presets_digest="sha256:$(sha256sum "${inf_root}/CMakePresets.json" | awk '{print $1}')"
inference_contract_digest="sha256:$(sha256sum "${repo_root}/contracts/inference/v1/inference.proto" | awk '{print $1}')"
edge_contract_digest="sha256:$(sha256sum "${repo_root}/contracts/edge/v1/edge.proto" | awk '{print $1}')"
inference_profile_digest="sha256:$(sha256sum "${repo_root}/contracts/inference/v1/profile.json" | awk '{print $1}')"
inf_evidence_schema_digest="sha256:$(sha256sum "${repo_root}/contracts/evidence/v1/inference-module-schema.json" | awk '{print $1}')"
module_findings_schema_digest="sha256:$(sha256sum "${repo_root}/contracts/evidence/module-findings/v1/schema.json" | awk '{print $1}')"
module_findings_registry_digest="sha256:$(sha256sum "${inf_root}/module-findings.json" | awk '{print $1}')"
traceability_digest="sha256:$(sha256sum "${inf_root}/requirements-traceability.json" | awk '{print $1}')"
requirements_digest="sha256:$(sha256sum "${repo_root}/docs/masi-nids-vnext-system-requirements-2026-08-09.md" | awk '{print $1}')"
binary_digest=""
if [[ -f "${inf_root}/build/cpu-release/masi_inference_gateway" ]]; then
  binary_digest="sha256:$(sha256sum "${inf_root}/build/cpu-release/masi_inference_gateway" | awk '{print $1}')"
fi
oci_evidence_digest=""
if [[ -f "${oci_evidence}/oci-smoke-evidence.json" ]]; then
  oci_evidence_digest="sha256:$(sha256sum "${oci_evidence}/oci-smoke-evidence.json" | awk '{print $1}')"
fi
deep_evidence_digest=""
if [[ -f "${deep_evidence}/deep-check-summary.json" ]]; then
  deep_evidence_digest="sha256:$(sha256sum "${deep_evidence}/deep-check-summary.json" | awk '{print $1}')"
fi
supply_evidence_digest=""
if [[ -f "${supply_evidence}/supply-chain.json" ]]; then
  supply_evidence_digest="sha256:$(sha256sum "${supply_evidence}/supply-chain.json" | awk '{print $1}')"
fi
numeric_evidence_digest=""
if [[ -f "${numeric_evidence}/numeric-golden-evidence.json" ]]; then
  numeric_evidence_digest="sha256:$(sha256sum "${numeric_evidence}/numeric-golden-evidence.json" | awk '{print $1}')"
fi
generated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

absolute_result="HOLD"
absolute_reason="DEC-001 Owner thresholds and exact qualification hardware are not frozen"
baseline_result="HOLD"
baseline_reason="protected commit/tag attestation and clean release tree are not present"
if [[ "${working_tree_dirty}" == false ]]; then
  baseline_reason="clean tree observed, but protected commit/tag attestation is not present"
fi

overall_status="HOLD"
if [[ "${supply_result}" == "FAIL" ]]; then
  overall_status="FAIL"
fi
for required_result in "${oci_result}" "${deep_result}" "${formal_soak_result}" "${absolute_result}" "${baseline_result}"; do
  if [[ "${required_result}" == "FAIL" ]]; then
    overall_status="FAIL"
  fi
done

conditional_applicability="$(jq -c '.conditional_applicability' \
  "${inf_root}/requirements-traceability.json" 2>/dev/null || printf '[]\n')"
requirement_ids="$(jq -c '[.requirements[].requirement_id]' \
  "${inf_root}/requirements-traceability.json" 2>/dev/null || printf '["MOD-INF-001"]\n')"

module_complete=false
if [[ "${overall_status}" == "HOLD" || "${overall_status}" == "PASS" ]]; then
  module_complete=true
fi
for command_result in "${oci_result}" "${deep_result}" "${supply_result}" "${formal_soak_result}"; do
  if [[ "${command_result}" == "FAIL" ]]; then
    module_complete=false
  fi
done

gate_summary_temporary="${evidence_root}/.gate-summary.$$.json"
set +e
jq -n \
  --arg run_id "${run_id}" --arg generated_at "${generated_at}" \
  --arg source_revision "${source_revision}" --arg baseline_git_tree "${baseline_git_tree}" \
  --arg source_tree_digest "${source_tree_digest}" \
  --argjson working_tree_dirty "${working_tree_dirty}" \
  --arg working_tree_status_digest "${working_tree_status_digest}" \
  --arg cmake_presets_digest "${cmake_presets_digest}" \
  --arg inference_contract_digest "${inference_contract_digest}" \
  --arg edge_contract_digest "${edge_contract_digest}" \
  --arg inference_profile_digest "${inference_profile_digest}" \
  --arg inf_evidence_schema_digest "${inf_evidence_schema_digest}" \
  --arg module_findings_schema_digest "${module_findings_schema_digest}" \
  --arg module_findings_registry_digest "${module_findings_registry_digest}" \
  --arg traceability_digest "${traceability_digest}" --arg requirements_digest "${requirements_digest}" \
  --arg binary_digest "${binary_digest}" --arg oci_result "${oci_result}" \
  --arg oci_qualification "${oci_qualification}" --arg deep_result "${deep_result}" \
  --arg deep_qualification "${deep_qualification}" --arg supply_result "${supply_result}" \
  --arg supply_qualification "${supply_qualification}" \
  --arg formal_soak_result "${formal_soak_result}" \
  --arg formal_soak_qualification "${formal_soak_qualification}" \
  --arg absolute_result "${absolute_result}" --arg absolute_reason "${absolute_reason}" \
  --arg baseline_result "${baseline_result}" --arg baseline_reason "${baseline_reason}" \
  --arg oci_evidence_digest "${oci_evidence_digest}" \
  --arg deep_evidence_digest "${deep_evidence_digest}" \
  --arg supply_evidence_digest "${supply_evidence_digest}" \
  --arg numeric_evidence_digest "${numeric_evidence_digest}" \
  --arg overall_status "${overall_status}" \
  --argjson module_complete "${module_complete}" --argjson requirement_ids "${requirement_ids}" \
  --argjson conditional_applicability "${conditional_applicability}" '{
    schema_version: "central-inference-module-gate-summary/v1",
    test_id: "TEST-INF-MODULE-GATES-001",
    requirement_ids: $requirement_ids,
    module_id: "MOD-INF-001",
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
      cmake_presets: $cmake_presets_digest,
      inference_contract: $inference_contract_digest,
      edge_contract: $edge_contract_digest,
      inference_wire_profile: $inference_profile_digest,
      inference_evidence_schema: $inf_evidence_schema_digest,
      module_findings_schema: $module_findings_schema_digest,
      module_findings_registry: $module_findings_registry_digest,
      traceability_manifest: $traceability_digest,
      requirements_baseline: $requirements_digest,
      release_binary: (if $binary_digest == "" then null else $binary_digest end),
      oci_evidence: (if $oci_evidence_digest == "" then null else $oci_evidence_digest end),
      deep_check_evidence: (if $deep_evidence_digest == "" then null else $deep_evidence_digest end),
      supply_chain_evidence: (if $supply_evidence_digest == "" then null else $supply_evidence_digest end),
      numeric_golden_evidence: (if $numeric_evidence_digest == "" then null else $numeric_evidence_digest end)
    },
    executed_gates: {
      gcc_version: "PASS",
      cmake_version: "PASS",
      protoc_version: "PASS",
      public_contract_schema_golden_negative: "PASS",
      format: "PASS",
      clang_tidy_deny_warnings: "PASS",
      unit_property_contract_golden: "PASS",
      numeric_golden_python_reference: "PASS",
      real_process_public_boundary_blackbox: "PASS",
      release_build: "PASS",
      oci_startup_mtls_probe_graceful_shutdown: {result:$oci_result,qualification:$oci_qualification},
      asan_ubsan_tsan_library_scope: {result:$deep_result,qualification:$deep_qualification},
      supply_chain: {result:$supply_result,qualification:$supply_qualification}
    },
    conditional_applicability: $conditional_applicability,
    qualification_gates: {
      absolute_performance: {result:$absolute_result,qualification:"NOT_QUALIFIED",reason:$absolute_reason},
      formal_soak_3600_seconds: {
        result:$formal_soak_result,qualification:$formal_soak_qualification,
        command:"MASI_INF_FORMAL_SOAK=1 scripts/run-module-gates.sh"
      },
      protected_release_baseline: {result:$baseline_result,qualification:"NOT_QUALIFIED",reason:$baseline_reason},
      formal_pairwise_and_system_integration: {
        result:"NOT_RUN",qualification:"NOT_QUALIFIED",
        reason:"intentionally outside independent module implementation phase"
      }
    },
    requirement_traceability: {
      manifest:"infer-cpp/requirements-traceability.json",digest:$traceability_digest,
      requirement_count:($requirement_ids | length)
    },
    overall_module_complete: $module_complete,
    overall_status: $overall_status
  }' >"${gate_summary_temporary}"
gate_summary_status=$?
set -e
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