#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
host_root="$(cd -- "${script_dir}/.." && pwd)"
evidence_dir="${MASI_PLUGIN_HOST_PERFORMANCE_EVIDENCE_DIR:-${host_root}/evidence/performance}"
mkdir -p -- "${evidence_dir}"
output="${evidence_dir}/performance-evidence.json"
[[ ! -e "${output}" && ! -L "${output}" ]] || {
  echo "performance evidence output already exists" >&2
  exit 1
}
MASI_PLUGIN_HOST_PERFORMANCE_OUTPUT="${output}" \
  cargo test --manifest-path "${host_root}/Cargo.toml" --locked --release \
  --test performance bounded_concurrency_batch_and_saturation_matrix \
  -- --ignored --exact --nocapture --test-threads=1
jq -e '.result == "PASS" and .steady_rejected_total == 0 and
  .saturation.classified_rejections > 0 and .saturation.unclassified_failures == 0 and
  .saturation.final_queued == 0 and .saturation.final_in_flight == 0 and
  .wasm_cold_warm.warm_calls == 64 and .maximum_p99_micros <= .absolute_deadline_micros' \
  "${output}" >/dev/null
