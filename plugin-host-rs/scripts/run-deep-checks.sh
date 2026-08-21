#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
host_root="$(cd -- "${script_dir}/.." && pwd)"
evidence_dir="${MASI_PLUGIN_HOST_DEEP_EVIDENCE_DIR:-${host_root}/evidence/deep-checks}"
mkdir -p -- "${evidence_dir}"
for output in audit.log deny.log coverage.json coverage.log valgrind.log deep-checks-evidence.json; do
  [[ ! -e "${evidence_dir}/${output}" && ! -L "${evidence_dir}/${output}" ]] || {
    echo "deep-check evidence output already exists: ${output}" >&2
    exit 1
  }
done

cargo audit --file "${host_root}/Cargo.lock" --no-fetch --deny warnings \
  >"${evidence_dir}/audit.log" 2>&1
cargo deny --manifest-path "${host_root}/Cargo.toml" --offline --locked \
  check advisories licenses sources >"${evidence_dir}/deny.log" 2>&1
cargo llvm-cov --manifest-path "${host_root}/Cargo.toml" \
  --all-features --workspace --json --output-path "${evidence_dir}/coverage.json" \
  -- --test-threads=1 >"${evidence_dir}/coverage.log" 2>&1
coverage_percent="$(jq -er '.data[0].totals.lines.percent' "${evidence_dir}/coverage.json")"
jq -e '.data[0].totals.lines.percent >= 50' "${evidence_dir}/coverage.json" >/dev/null

test_binary="$(
  cargo test --manifest-path "${host_root}/Cargo.toml" --lib --no-run --message-format=json \
    | jq -r 'select(.reason == "compiler-artifact" and .profile.test == true and .target.name == "masi_plugin_host") | .executable' \
    | tail -n 1
)"
[[ -x "${test_binary}" ]] || {
  echo "unable to resolve library test binary for Valgrind" >&2
  exit 1
}
valgrind --quiet --error-exitcode=99 --leak-check=full \
  --show-leak-kinds=definite --errors-for-leak-kinds=definite \
  "${test_binary}" runtime::wasm::tests::executes_component_without_imports \
  --exact --nocapture >"${evidence_dir}/valgrind.log" 2>&1

miri_applicability="NOT_APPLICABLE"
miri_reason="EXACT_STABLE_TOOLCHAIN_HAS_NO_MIRI_COMPONENT"
if rustup component list --installed --toolchain 1.97.1-x86_64-unknown-linux-gnu \
  | grep -q '^miri-'; then
  miri_applicability="APPLICABLE"
  miri_reason="MIRI_COMPONENT_PRESENT"
fi
jq -n \
  --argjson coverage_percent "${coverage_percent}" \
  --arg miri_applicability "${miri_applicability}" \
  --arg miri_reason "${miri_reason}" \
  '{
    schema_version: "plugin-host-deep-checks-evidence/v1",
    module_id: "MOD-PLUGIN-001",
    cargo_audit: "PASS",
    cargo_deny_advisories_licenses_sources: "PASS",
    line_coverage_percent: $coverage_percent,
    line_coverage_minimum_percent: 50,
    valgrind_definite_leaks_and_errors: "PASS",
    sanitizer_profile: "Valgrind 3.18 definite leak/error gate over actual Wasmtime Component invocation",
    miri: {
      applicability: $miri_applicability,
      result: (if $miri_applicability == "APPLICABLE" then "NOT_RUN" else "NOT_RUN" end),
      qualification: "NOT_QUALIFIED",
      stable_reason: $miri_reason
    },
    stable_rust_sanitizer: {
      applicability: "NOT_APPLICABLE",
      result: "NOT_RUN",
      qualification: "NOT_QUALIFIED",
      stable_reason: "RUST_Z_SANITIZER_REQUIRES_NON_PROFILE_NIGHTLY_TOOLCHAIN"
    },
    result: "PASS",
    qualification: "NOT_QUALIFIED",
    reason_code: "RUST_DEEP_CHECKS_PASS"
  }' >"${evidence_dir}/deep-checks-evidence.json"
