#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
host_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${host_root}/.." && pwd)"
evidence_root="${host_root}/evidence/module-gates"
run_id="${MASI_PLUGIN_HOST_RUN_ID:-plugin-host-formal-$(date -u +%Y%m%dT%H%M%SZ)-${$}}"
run_dir="${evidence_root}/runs/${run_id}"
temporary_root="$(mktemp -d /tmp/masi-plugin-host-gates.XXXXXX)"
command_schema="${repo_root}/contracts/evidence/command/v1/schema.json"

cleanup() {
  if [[ "${temporary_root}" == /tmp/masi-plugin-host-gates.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

fail() {
  echo "plugin-host module gates: $*" >&2
  exit 1
}

[[ "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$ ]] || fail "invalid run id"
[[ ! -e "${run_dir}" && ! -L "${run_dir}" ]] || fail "run id already exists"
for command in cargo docker git go jq python3 sha256sum tar; do
  command -v "${command}" >/dev/null 2>&1 || fail "missing command ${command}"
done

git -C "${repo_root}" status --porcelain=v1 --untracked-files=all \
  >"${temporary_root}/working-tree-status.txt"
tar --sort=name --mtime=@1787270400 --owner=0 --group=0 --numeric-owner \
  --exclude='plugin-host-rs/target' --exclude='plugin-host-rs/evidence' \
  --exclude='**/node_modules' --exclude='**/__pycache__' --exclude='*.pyc' \
  -cf "${temporary_root}/source-tree.tar" -C "${repo_root}" plugin-host-rs contracts deploy/plugin-host
source_tree_digest="sha256:$(sha256sum "${temporary_root}/source-tree.tar" | awk '{print $1}')"
working_tree_status_digest="sha256:$(sha256sum "${temporary_root}/working-tree-status.txt" | awk '{print $1}')"
mkdir -p -- "${run_dir}" "${run_dir}/deep" "${run_dir}/fault" "${run_dir}/performance" \
  "${run_dir}/oci" "${run_dir}/supply" "${run_dir}/formal-soak"
cp -- "${temporary_root}/working-tree-status.txt" "${run_dir}/working-tree-status.txt"

run_command() {
  local command_id="$1"
  shift
  local log="${run_dir}/${command_id}.log"
  local sidecar="${run_dir}/${command_id}.command.json"
  local started_at finished_at started_ms finished_ms duration_ms exit_code result qualification reason
  local argv_json
  argv_json="$(printf '%s\n' "$@" | jq -R . | jq -s .)"
  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  started_ms="$(date +%s%3N)"
  set +e
  "$@" >"${log}" 2>&1
  exit_code=$?
  set -e
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  finished_ms="$(date +%s%3N)"
  duration_ms="$((finished_ms - started_ms))"
  if [[ "${exit_code}" -eq 0 ]]; then
    result="PASS"
    qualification="QUALIFIED"
    reason="null"
  elif [[ "${exit_code}" -eq 2 ]]; then
    result="HOLD"
    qualification="NOT_QUALIFIED"
    reason='"COMMAND_EXITED_HOLD"'
  else
    result="FAIL"
    qualification="NOT_QUALIFIED"
    reason='"COMMAND_EXITED_FAILURE"'
  fi
  jq -n \
    --arg run_id "${run_id}" --arg command_id "${command_id}" \
    --arg started_at "${started_at}" --arg finished_at "${finished_at}" \
    --argjson duration_ms "${duration_ms}" --argjson argv "${argv_json}" \
    --argjson exit_code "${exit_code}" --arg result "${result}" \
    --arg qualification "${qualification}" --argjson stable_reason "${reason}" \
    --arg source_tree_digest "${source_tree_digest}" \
    --arg working_tree_status_digest "${working_tree_status_digest}" \
    --arg log_sha256 "sha256:$(sha256sum "${log}" | awk '{print $1}')" \
    --argjson log_bytes "$(stat -c %s "${log}")" \
    '{
      schema_version: "edge-command-execution/v1",
      run_id: $run_id,
      command_id: $command_id,
      started_at: $started_at,
      finished_at: $finished_at,
      duration_ms: $duration_ms,
      working_directory: "plugin-host-rs",
      argv: $argv,
      exit_code: $exit_code,
      result: $result,
      qualification: $qualification,
      stable_reason: $stable_reason,
      source_tree_digest: $source_tree_digest,
      working_tree_status_digest: $working_tree_status_digest,
      log: {
        path: ($command_id + ".log"),
        sha256: $log_sha256,
        bytes: $log_bytes,
        media_type: "text/plain"
      }
    }' >"${sidecar}"
  python3 "${script_dir}/validate-json.py" --schema "${command_schema}" --document "${sidecar}"
  if [[ "${exit_code}" -ne 0 ]]; then
    echo "required command failed: ${command_id}; see ${log}" >&2
    python3 "${repo_root}/scripts/ci/write_module_failure.py" \
      --repo "${repo_root}" --module plugin-host --run-dir "${run_dir}" \
      --command-sidecar "${command_id}.command.json" \
      --output "${run_dir}/gate-failure.json" \
      || fail "could not publish structured module failure evidence"
    exit "${exit_code}"
  fi
}

run_command contract-schema python3 "${script_dir}/validate-contracts.py"
run_command control-contract-consumer python3 \
  "${repo_root}/control-go/scripts/validate-public-contracts.py" --repo "${repo_root}"
run_command control-statistics-adapter go -C "${repo_root}/control-go" test \
  ./internal/pluginstat ./internal/grpc
run_command source-sentinels python3 "${script_dir}/check-source-sentinels.py"
run_command findings python3 "${script_dir}/validate-static-evidence.py" \
  --repo "${repo_root}" --scope findings
run_command format cargo fmt --manifest-path "${host_root}/Cargo.toml" --all -- --check
run_command clippy cargo clippy --manifest-path "${host_root}/Cargo.toml" \
  --all-targets --all-features -- -D warnings
run_command unit-property-contract cargo test --manifest-path "${host_root}/Cargo.toml" \
  --locked --all-targets --all-features -- --test-threads=1
run_command rustdoc env RUSTDOCFLAGS=-Dwarnings cargo doc \
  --manifest-path "${host_root}/Cargo.toml" --locked --all-features --no-deps
run_command release-build cargo build --manifest-path "${host_root}/Cargo.toml" \
  --locked --release --bins
run_command release-blackbox env \
  MASI_PLUGIN_HOST_BINARY="${host_root}/target/release/masi-plugin-host" \
  MASI_PLUGIN_SERVICE_FIXTURE_BINARY="${host_root}/target/release/masi-plugin-service-fixture" \
  MASI_PLUGIN_HOST_FAULT_OUTPUT="${run_dir}/fault/fault-evidence.json" \
  cargo test --manifest-path "${host_root}/Cargo.toml" --locked --release \
  --test module_blackbox -- --nocapture --test-threads=1
run_command wasm-fault-matrix cargo test --manifest-path "${host_root}/Cargo.toml" \
  --locked --release --lib runtime::wasm::tests -- --nocapture --test-threads=1
run_command deep-checks env MASI_PLUGIN_HOST_DEEP_EVIDENCE_DIR="${run_dir}/deep" \
  "${script_dir}/run-deep-checks.sh"
run_command performance env MASI_PLUGIN_HOST_PERFORMANCE_EVIDENCE_DIR="${run_dir}/performance" \
  "${script_dir}/run-performance.sh"
run_command oci-smoke env MASI_PLUGIN_HOST_OCI_EVIDENCE_DIR="${run_dir}/oci" \
  MASI_PLUGIN_HOST_IMAGE_REF=masi-plugin-host:module-gates \
  "${script_dir}/run-oci-smoke.sh"
run_command supply-chain env MASI_PLUGIN_HOST_SUPPLY_EVIDENCE_DIR="${run_dir}/supply" \
  MASI_PLUGIN_HOST_IMAGE_REF=masi-plugin-host:module-gates \
  "${script_dir}/run-supply-chain.sh"
run_command formal-soak env MASI_PLUGIN_HOST_SOAK_EVIDENCE_DIR="${run_dir}/formal-soak" \
  "${script_dir}/run-soak.sh"
run_command traceability python3 "${script_dir}/validate-static-evidence.py" \
  --repo "${repo_root}" --scope traceability

python3 "${script_dir}/build-traceability.py" \
  --repo "${repo_root}" --run-dir "${run_dir}" \
  --manifest "${host_root}/requirements-traceability.json" \
  --output "${run_dir}/traceability.json"
python3 "${script_dir}/build-module-summary.py" \
  --repo "${repo_root}" --run-dir "${run_dir}" \
  --output "${run_dir}/gate-summary.provisional.json"
run_command evidence-integrity "${script_dir}/run-evidence-integrity.sh" \
  "${repo_root}" "${run_dir}" "${run_dir}/gate-summary.provisional.json"
python3 "${script_dir}/build-module-summary.py" \
  --repo "${repo_root}" --run-dir "${run_dir}" \
  --output "${run_dir}/gate-summary.json"
python3 "${script_dir}/validate-module-evidence.py" \
  --repo "${repo_root}" --run-dir "${run_dir}" \
  --summary "${run_dir}/gate-summary.json" --negative-self-test

cp -- "${run_dir}/gate-summary.json" "${evidence_root}/.gate-summary-${run_id}.json"
mv -- "${evidence_root}/.gate-summary-${run_id}.json" "${evidence_root}/gate-summary.json"
jq -n --arg run_id "${run_id}" \
  --arg evidence "runs/${run_id}/gate-summary.json" \
  --arg digest "sha256:$(sha256sum "${run_dir}/gate-summary.json" | awk '{print $1}')" \
  --argjson overall_module_complete "$(jq '.overall_module_complete' "${run_dir}/gate-summary.json")" \
  '{
    schema_version: "plugin-runtime-host-module-latest/v1",
    run_id: $run_id,
    evidence: $evidence,
    digest: $digest,
    overall_module_complete: $overall_module_complete
  }' >"${evidence_root}/.latest-${run_id}.json"
mv -- "${evidence_root}/.latest-${run_id}.json" "${evidence_root}/latest.json"
chmod -R a-w "${run_dir}"
jq . "${run_dir}/gate-summary.json"
