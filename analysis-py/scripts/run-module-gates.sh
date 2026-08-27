#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
module_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${module_root}/.." && pwd)"
evidence_root="${module_root}/evidence/module-gates"
run_id="${MASI_ANALYSIS_RUN_ID:-analysis-formal-$(date -u +%Y%m%dT%H%M%SZ)-${$}}"
run_dir="${evidence_root}/runs/${run_id}"
temporary_root="$(mktemp -d /tmp/masi-analysis-gates.XXXXXX)"
command_schema="${repo_root}/contracts/evidence/command/v1/schema.json"
python_runtime="${module_root}/.venv/bin/python"
image_ref="masi-analysis:module-gates"

cleanup() {
  if [[ "${temporary_root}" == /tmp/masi-analysis-gates.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

fail() {
  echo "analysis module gates: $*" >&2
  exit 1
}

[[ "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$ ]] || fail "invalid run id"
[[ ! -e "${run_dir}" && ! -L "${run_dir}" ]] || fail "run id already exists"
[[ -x "${python_runtime}" ]] || fail "Analysis development runtime is absent; run uv sync --frozen"
for command in docker git go jq pyright ruff sha256sum tar uv; do
  command -v "${command}" >/dev/null 2>&1 || fail "missing command ${command}"
done
cd -- "${module_root}"

git -C "${repo_root}" status --porcelain=v1 --untracked-files=all >"${temporary_root}/working-tree-status.txt"
working_tree_status_digest="sha256:$(sha256sum "${temporary_root}/working-tree-status.txt" | awk '{print $1}')"
working_tree_dirty=false
[[ ! -s "${temporary_root}/working-tree-status.txt" ]] || working_tree_dirty=true
tar --sort=name --mtime=@1787334400 --owner=0 --group=0 --numeric-owner \
  --exclude='analysis-py/.venv' --exclude='analysis-py/.ruff_cache' \
  --exclude='analysis-py/evidence' --exclude='analysis-py/build' --exclude='analysis-py/dist' \
  --exclude='analysis-py/src/masi_analysis_plugin.egg-info' --exclude='**/node_modules' \
  --exclude='**/__pycache__' --exclude='*.pyc' \
  -cf "${temporary_root}/source-tree.tar" -C "${repo_root}" analysis-py contracts deploy/analysis
source_tree_digest="sha256:$(sha256sum "${temporary_root}/source-tree.tar" | awk '{print $1}')"
source_revision="$(git -C "${repo_root}" rev-parse HEAD)"

mkdir -p -- "${run_dir}" "${run_dir}/release" "${run_dir}/blackbox" "${run_dir}/compatibility" \
  "${run_dir}/performance" "${run_dir}/image" "${run_dir}/oci" "${run_dir}/deployment" \
  "${run_dir}/supply" "${run_dir}/formal-soak"
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
      working_directory: "analysis-py",
      argv: $argv,
      exit_code: $exit_code,
      result: $result,
      qualification: $qualification,
      stable_reason: $stable_reason,
      source_tree_digest: $source_tree_digest,
      working_tree_status_digest: $working_tree_status_digest,
      log: {path: ($command_id + ".log"), sha256: $log_sha256, bytes: $log_bytes, media_type: "text/plain"}
    }' >"${sidecar}"
  "${python_runtime}" -c 'import json,sys; from jsonschema import Draft202012Validator,FormatChecker; schema=json.load(open(sys.argv[1])); doc=json.load(open(sys.argv[2])); errors=list(Draft202012Validator(schema,format_checker=FormatChecker()).iter_errors(doc)); (_ for _ in ()).throw(RuntimeError("; ".join(e.message for e in errors))) if errors else None' "${command_schema}" "${sidecar}"
  if [[ "${exit_code}" -ne 0 ]]; then
    echo "required command failed: ${command_id}; see ${log}" >&2
    exit "${exit_code}"
  fi
}

run_command contract-schema "${python_runtime}" "${script_dir}/validate-contracts.py"
run_command control-contract-consumer "${script_dir}/validate-control-consumer.sh"
run_command source-sentinels "${python_runtime}" "${script_dir}/check-source-sentinels.py"
run_command findings "${python_runtime}" "${script_dir}/validate-static-evidence.py" --repo "${repo_root}" --scope findings
run_command format ruff format --check src tests scripts
run_command lint ruff check src tests scripts
run_command typecheck pyright --project pyproject.toml
run_command unit-contract "${python_runtime}" -m unittest discover -s tests -t . -v
run_command compatibility "${python_runtime}" "${script_dir}/validate-compatibility.py" \
  --repo "${repo_root}" --legacy-root "${MASI_LEGACY_ROOT:-/home/lmhhh/MASI-NIDS}" \
  --require-legacy-snapshot --evidence "${run_dir}/compatibility/compatibility-evidence.json"
run_command release-build env \
  MASI_ANALYSIS_WORKING_TREE_STATUS_DIGEST="${working_tree_status_digest}" \
  MASI_ANALYSIS_WORKING_TREE_DIRTY="${working_tree_dirty}" \
  "${script_dir}/build-release-runtime.sh" "${temporary_root}/release" "${run_dir}/release/release-runtime-evidence.json"
release_binary="${temporary_root}/release/venv/bin/masi-analysis"
run_command release-blackbox "${temporary_root}/release/venv/bin/python" "${script_dir}/run-blackbox.py" \
  --binary "${release_binary}" --source-tree-digest "${source_tree_digest}" \
  --working-tree-status-digest "${working_tree_status_digest}" --evidence "${run_dir}/blackbox/blackbox-evidence.json"
run_command performance "${temporary_root}/release/venv/bin/python" "${script_dir}/run-performance.py" \
  --binary "${release_binary}" --source-tree-digest "${source_tree_digest}" \
  --working-tree-status-digest "${working_tree_status_digest}" --evidence "${run_dir}/performance/performance-evidence.json"
run_command image-build env \
  MASI_ANALYSIS_IMAGE_REF="${image_ref}" \
  MASI_ANALYSIS_BUILD_EVIDENCE="${run_dir}/image/image-build-evidence.json" \
  MASI_ANALYSIS_WORKING_TREE_STATUS_DIGEST="${working_tree_status_digest}" \
  MASI_ANALYSIS_WORKING_TREE_DIRTY="${working_tree_dirty}" \
  "${script_dir}/build-image.sh"
immutable_image_ref="masi-analysis@$(jq -er .image_id "${run_dir}/image/image-build-evidence.json")"
run_command oci-smoke "${python_runtime}" "${script_dir}/run-oci-smoke.py" \
  --image "${immutable_image_ref}" --evidence "${run_dir}/oci/oci-evidence.json"
run_command deployment-policy "${python_runtime}" "${script_dir}/validate-deployment.py" \
  --repo "${repo_root}" --evidence "${run_dir}/deployment/deployment-evidence.json"
run_command supply-chain env \
  MASI_ANALYSIS_SUPPLY_EVIDENCE_DIR="${run_dir}/supply" \
  MASI_ANALYSIS_IMAGE_REF="${immutable_image_ref}" \
  MASI_ANALYSIS_WORKING_TREE_STATUS_DIGEST="${working_tree_status_digest}" \
  MASI_ANALYSIS_WORKING_TREE_DIRTY="${working_tree_dirty}" \
  "${script_dir}/run-supply-chain.sh"
run_command formal-soak "${temporary_root}/release/venv/bin/python" "${script_dir}/run-soak.py" \
  --binary "${release_binary}" --warmup-seconds 60 --phase-seconds 900 \
  --source-tree-digest "${source_tree_digest}" --evidence "${run_dir}/formal-soak/soak-evidence.json"
run_command traceability "${python_runtime}" "${script_dir}/validate-static-evidence.py" \
  --repo "${repo_root}" --scope traceability

"${python_runtime}" "${script_dir}/build-traceability.py" \
  --repo "${repo_root}" --run-dir "${run_dir}" \
  --manifest "${module_root}/requirements-traceability.json" --output "${run_dir}/traceability.json"
"${python_runtime}" "${script_dir}/build-module-summary.py" \
  --repo "${repo_root}" --run-dir "${run_dir}" --output "${run_dir}/gate-summary.provisional.json"
run_command evidence-integrity "${python_runtime}" "${script_dir}/validate-module-evidence.py" \
  --repo "${repo_root}" --run-dir "${run_dir}" --summary "${run_dir}/gate-summary.provisional.json" \
  --provisional --negative-self-test
"${python_runtime}" "${script_dir}/build-module-summary.py" \
  --repo "${repo_root}" --run-dir "${run_dir}" --output "${run_dir}/gate-summary.json"
"${python_runtime}" "${script_dir}/validate-module-evidence.py" \
  --repo "${repo_root}" --run-dir "${run_dir}" --summary "${run_dir}/gate-summary.json" --negative-self-test

cp -- "${run_dir}/gate-summary.json" "${evidence_root}/.gate-summary-${run_id}.json"
mv -- "${evidence_root}/.gate-summary-${run_id}.json" "${evidence_root}/gate-summary.json"
jq -n --arg run_id "${run_id}" --arg evidence "runs/${run_id}/gate-summary.json" \
  --arg digest "sha256:$(sha256sum "${run_dir}/gate-summary.json" | awk '{print $1}')" \
  '{schema_version: "analysis-plugin-module-latest/v1", run_id: $run_id, evidence: $evidence, digest: $digest, overall_module_complete: true}' \
  >"${evidence_root}/.latest-${run_id}.json"
mv -- "${evidence_root}/.latest-${run_id}.json" "${evidence_root}/latest.json"
chmod -R a-w "${run_dir}"
jq . "${run_dir}/gate-summary.json"
