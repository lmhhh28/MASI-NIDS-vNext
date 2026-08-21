#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
host_root="$(cd -- "${script_dir}/.." && pwd)"
evidence_dir="${MASI_PLUGIN_HOST_SOAK_EVIDENCE_DIR:-${host_root}/evidence/soak-current}"
mkdir -p -- "${evidence_dir}"
output="${evidence_dir}/soak-evidence.json"
[[ ! -e "${output}" && ! -L "${output}" ]] || {
  echo "soak evidence output already exists" >&2
  exit 1
}
cargo build --manifest-path "${host_root}/Cargo.toml" --locked --release \
  --bin masi-plugin-host --bin masi-plugin-service-fixture
MASI_PLUGIN_HOST_BINARY="${host_root}/target/release/masi-plugin-host" \
MASI_PLUGIN_SERVICE_FIXTURE_BINARY="${host_root}/target/release/masi-plugin-service-fixture" \
MASI_PLUGIN_HOST_SOAK_OUTPUT="${output}" \
  cargo test --manifest-path "${host_root}/Cargo.toml" --locked --release \
  --test soak formal_four_stage_soak -- --ignored --exact --nocapture --test-threads=1
jq -e '
  .formal_soak_executed == true and
  .warmup_seconds == 60 and
  .formal_seconds == 3600 and
  (.stages | length) == 5 and
  .unclassified_failures == 0 and
  .revocation_refresh_failures == 0 and
  .stages[3].name == "saturation" and
  .stages[3].classified_rejections > 0 and
  .stages[4].service_crash_classified == true and
  .stages[4].service_restart_recovered == true and
  .resource_growth.leak_thresholds_passed == true and
  .resource_growth.queued_final == 0 and
  .resource_growth.in_flight_final == 0 and
  .candidate_processes_remained_live == true and
  .cleanup_succeeded == true and
  .result == "PASS"
' "${output}" >/dev/null
