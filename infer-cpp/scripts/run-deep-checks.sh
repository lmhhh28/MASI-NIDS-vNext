#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
inf_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${inf_root}/.." && pwd)"
evidence_dir="${MASI_INF_DEEP_EVIDENCE_DIR:-${inf_root}/evidence/deep-checks}"
mkdir -p -- "${evidence_dir}"

source_archive="$(mktemp "${evidence_dir}/source-tree.XXXXXX.tar")"
cleanup() {
  if [[ -f "${source_archive}" ]]; then
    unlink -- "${source_archive}"
  fi
}
trap cleanup EXIT

working_tree_status="$(git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)"
working_tree_dirty=false
if [[ -n "${working_tree_status}" ]]; then
  working_tree_dirty=true
fi
printf '%s\n' "${working_tree_status}" >"${evidence_dir}/working-tree-status.txt"
working_tree_status_digest="sha256:$(sha256sum "${evidence_dir}/working-tree-status.txt" | awk '{print $1}')"
tar --sort=name --mtime=@1786406400 --owner=0 --group=0 --numeric-owner \
  --exclude='infer-cpp/build' --exclude='infer-cpp/evidence' \
  --exclude='**/node_modules' --exclude='**/__pycache__' --exclude='*.pyc' \
  -cf "${source_archive}" -C "${repo_root}" infer-cpp contracts testkit
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
if [[ -n "${MASI_INF_EXPECTED_SOURCE_TREE_DIGEST:-}" \
  && "${source_tree_digest}" != "${MASI_INF_EXPECTED_SOURCE_TREE_DIGEST}" ]]; then
  echo "deep-check source tree changed after module gate snapshot" >&2
  exit 1
fi
source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
cmake_presets_digest="sha256:$(sha256sum "${inf_root}/CMakePresets.json" | awk '{print $1}')"
inference_contract_digest="sha256:$(sha256sum "${repo_root}/contracts/inference/v1/inference.proto" | awk '{print $1}')"
profile_digest="sha256:$(sha256sum "${repo_root}/contracts/inference/v1/profile.json" | awk '{print $1}')"

cd -- "${inf_root}"

# AddressSanitizer + UBSan on the library scope.
cmake --preset cpu-asan >/dev/null 2>&1
MASI_INF_BUILD_TESTS=ON cmake --build build/cpu-asan \
  --target masi_inf_gateway_lib contract_golden property_invariants \
  2>&1 | tee "${evidence_dir}/asan-build.log"
MASI_INF_E2E=0 ./build/cpu-asan/contract_golden 2>&1 | tee "${evidence_dir}/asan-contract.log"
MASI_INF_E2E=0 ./build/cpu-asan/property_invariants 2>&1 | tee "${evidence_dir}/asan-property.log"

# ThreadSanitizer on the library scope. On kernels with 32-bit mmap randomness
# TSan aborts with "unexpected memory mapping", so the sanitizer runs with ASLR
# disabled for the child process only (no host sysctl change). If it still
# cannot start, that is a missing-environment HOLD, never a PASS and never a
# code FAIL.
cmake --preset cpu-tsan >/dev/null 2>&1
MASI_INF_BUILD_TESTS=ON cmake --build build/cpu-tsan \
  --target masi_inf_gateway_lib contract_golden property_invariants \
  2>&1 | tee "${evidence_dir}/tsan-build.log"

tsan_runner=()
if command -v setarch >/dev/null 2>&1; then
  tsan_runner=(setarch "$(uname -m)" -R)
fi
tsan_status=0
set +e
MASI_INF_E2E=0 "${tsan_runner[@]}" ./build/cpu-tsan/contract_golden \
  2>&1 | tee "${evidence_dir}/tsan-contract.log"
tsan_status=$(( tsan_status | PIPESTATUS[0] ))
MASI_INF_E2E=0 "${tsan_runner[@]}" ./build/cpu-tsan/property_invariants \
  2>&1 | tee "${evidence_dir}/tsan-property.log"
tsan_status=$(( tsan_status | PIPESTATUS[0] ))
set -e
tsan_environment_blocked=false
if [[ "${tsan_status}" -ne 0 ]] && \
   grep -q "unexpected memory mapping" "${evidence_dir}/tsan-contract.log" \
     "${evidence_dir}/tsan-property.log" 2>/dev/null; then
  tsan_environment_blocked=true
  echo "ThreadSanitizer could not start: kernel mmap randomness incompatible" \
    >>"${evidence_dir}/tsan-property.log"
elif [[ "${tsan_status}" -ne 0 ]]; then
  echo "ThreadSanitizer reported failures (see logs)" >&2
  exit 1
fi

generated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
asan_log_digest="sha256:$(sha256sum "${evidence_dir}/asan-property.log" | awk '{print $1}')"
tsan_log_digest="sha256:$(sha256sum "${evidence_dir}/tsan-property.log" | awk '{print $1}')"
evidence_result="PASS"
evidence_qualification="QUALIFIED"
qualification_reason="CLEAN_SOURCE_SNAPSHOT"
if [[ "${tsan_environment_blocked}" == true ]]; then
  evidence_result="HOLD"
  evidence_qualification="NOT_QUALIFIED"
  qualification_reason="TSAN_KERNEL_MMAP_RANDOMNESS_INCOMPATIBLE"
fi
if [[ "${working_tree_dirty}" == true && "${tsan_environment_blocked}" == false ]]; then
  evidence_result="HOLD"
  evidence_qualification="NOT_QUALIFIED"
  qualification_reason="DIRTY_WORKTREE_NOT_RELEASE_BASELINE"
fi

jq -n \
  --arg generated_at "${generated_at}" \
  --arg source_revision "${source_revision}" \
  --arg source_tree_digest "${source_tree_digest}" \
  --arg working_tree_status_digest "${working_tree_status_digest}" \
  --arg cmake_presets_digest "${cmake_presets_digest}" \
  --arg inference_contract_digest "${inference_contract_digest}" \
  --arg profile_digest "${profile_digest}" \
  --arg asan_log_digest "${asan_log_digest}" \
  --arg tsan_log_digest "${tsan_log_digest}" \
  --arg result "${evidence_result}" \
  --arg qualification "${evidence_qualification}" \
  --arg qualification_reason "${qualification_reason}" \
  --argjson working_tree_dirty "${working_tree_dirty}" '{
    schema_version: "central-inference-deep-check-evidence/v1",
    test_id: "TEST-INF-CPP-DEEP-001",
    requirement_ids: ["MOD-INF-001", "TEST-003", "TEST-007", "TEST-009"],
    generated_at: $generated_at,
    level: "MODULE",
    applicability: "APPLICABLE",
    result: $result,
    qualification: $qualification,
    qualification_reason: $qualification_reason,
    scope: "C++ library scope under AddressSanitizer+UBSan and ThreadSanitizer",
    source_revision: $source_revision,
    source_tree_digest: $source_tree_digest,
    working_tree_dirty: $working_tree_dirty,
    working_tree_status_digest: $working_tree_status_digest,
    artifact_digests: {
      cmake_presets: $cmake_presets_digest,
      inference_contract: $inference_contract_digest,
      inference_wire_profile: $profile_digest,
      address_sanitizer_log: $asan_log_digest,
      thread_sanitizer_log: $tsan_log_digest
    },
    checks: {
      address_sanitizer: "PASS",
      undefined_behavior_sanitizer: "PASS",
      thread_sanitizer: (if $qualification_reason == "TSAN_KERNEL_MMAP_RANDOMNESS_INCOMPATIBLE"
                         then "HOLD" else "PASS" end)
    },
    public_boundary_blackbox_coverage: "recorded separately by run-module-gates.sh",
    overall_module_complete: false
  }' >"${evidence_dir}/deep-check-summary.json"

jq . "${evidence_dir}/deep-check-summary.json"
if [[ "${evidence_result}" == "HOLD" ]]; then
  exit 2
fi
