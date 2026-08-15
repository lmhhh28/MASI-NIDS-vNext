#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
edge_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${edge_root}/.." && pwd)"
evidence_dir="${MASI_EDGE_DEEP_EVIDENCE_DIR:-${edge_root}/evidence/deep-checks}"
nightly_toolchain="${MASI_EDGE_NIGHTLY_TOOLCHAIN:-nightly}"
expected_commit="${MASI_EDGE_NIGHTLY_COMMIT:-c98d0cb27cc63afdd62602a52eb4feb8a1c682dd}"
target_triple="x86_64-unknown-linux-gnu"
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
  --exclude='edge-rs/target' --exclude='edge-rs/evidence' \
  --exclude='edge-rs/**/__pycache__' --exclude='contracts/**/__pycache__' \
  -cf "${source_archive}" -C "${repo_root}" edge-rs contracts
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
if [[ -n "${MASI_EDGE_EXPECTED_SOURCE_TREE_DIGEST:-}" \
  && "${source_tree_digest}" != "${MASI_EDGE_EXPECTED_SOURCE_TREE_DIGEST}" ]]; then
  echo "deep-check source tree changed after module gate snapshot" >&2
  exit 1
fi
source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
cargo_lock_digest="sha256:$(sha256sum "${edge_root}/Cargo.lock" | awk '{print $1}')"
edge_contract_digest="sha256:$(sha256sum "${repo_root}/contracts/edge/v1/edge.proto" | awk '{print $1}')"
profile_digest="sha256:$(sha256sum "${repo_root}/contracts/profiles/v1/rust-edge-agent.json" | awk '{print $1}')"

cd -- "${edge_root}"
rustc "+${nightly_toolchain}" -Vv | tee "${evidence_dir}/nightly-rustc.log"
actual_commit="$(rustc "+${nightly_toolchain}" -Vv | awk '/^commit-hash:/ {print $2}')"
if [[ "${actual_commit}" != "${expected_commit}" ]]; then
  echo "nightly toolchain commit does not match the frozen deep-check commit" >&2
  exit 1
fi
cargo "+${nightly_toolchain}" miri --version | tee "${evidence_dir}/miri-version.log"

MIRIFLAGS="-Zmiri-disable-isolation" \
  cargo "+${nightly_toolchain}" miri test --lib --locked \
  2>&1 | tee "${evidence_dir}/miri.log"

CARGO_TARGET_DIR="${edge_root}/target/asan" \
  RUSTFLAGS="-Zsanitizer=address" \
  RUSTDOCFLAGS="-Zsanitizer=address" \
  cargo "+${nightly_toolchain}" test -Zbuild-std \
    --target "${target_triple}" --lib --locked \
  2>&1 | tee "${evidence_dir}/asan.log"

CARGO_TARGET_DIR="${edge_root}/target/tsan" \
  RUSTFLAGS="-Zsanitizer=thread" \
  RUSTDOCFLAGS="-Zsanitizer=thread" \
  cargo "+${nightly_toolchain}" test -Zbuild-std \
    --target "${target_triple}" --lib --locked \
  2>&1 | tee "${evidence_dir}/tsan.log"

generated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
miri_log_digest="sha256:$(sha256sum "${evidence_dir}/miri.log" | awk '{print $1}')"
asan_log_digest="sha256:$(sha256sum "${evidence_dir}/asan.log" | awk '{print $1}')"
tsan_log_digest="sha256:$(sha256sum "${evidence_dir}/tsan.log" | awk '{print $1}')"
evidence_result="PASS"
evidence_qualification="QUALIFIED"
qualification_reason="CLEAN_SOURCE_SNAPSHOT"
if [[ "${working_tree_dirty}" == true ]]; then
  evidence_result="HOLD"
  evidence_qualification="NOT_QUALIFIED"
  qualification_reason="DIRTY_WORKTREE_NOT_RELEASE_BASELINE"
fi
jq -n \
  --arg generated_at "${generated_at}" \
  --arg nightly_commit "${actual_commit}" \
  --arg target_triple "${target_triple}" \
  --arg source_revision "${source_revision}" \
  --arg source_tree_digest "${source_tree_digest}" \
  --arg working_tree_status_digest "${working_tree_status_digest}" \
  --arg cargo_lock_digest "${cargo_lock_digest}" \
  --arg edge_contract_digest "${edge_contract_digest}" \
  --arg profile_digest "${profile_digest}" \
  --arg miri_log_digest "${miri_log_digest}" \
  --arg asan_log_digest "${asan_log_digest}" \
  --arg tsan_log_digest "${tsan_log_digest}" \
  --arg result "${evidence_result}" \
  --arg qualification "${evidence_qualification}" \
  --arg qualification_reason "${qualification_reason}" \
  --argjson working_tree_dirty "${working_tree_dirty}" '{
    schema_version: "edge-deep-check-evidence/v1",
    test_id: "TEST-EDGE-RUST-DEEP-001",
    requirement_ids: ["MOD-EDGE-001", "TEST-003", "TEST-007", "TEST-009"],
    generated_at: $generated_at,
    level: "MODULE",
    applicability: "APPLICABLE",
    result: $result,
    qualification: $qualification,
    qualification_reason: $qualification_reason,
    scope: "Rust library unit state-space under Miri, AddressSanitizer, and ThreadSanitizer",
    nightly_commit: $nightly_commit,
    target_triple: $target_triple,
    source_revision: $source_revision,
    source_tree_digest: $source_tree_digest,
    working_tree_dirty: $working_tree_dirty,
    working_tree_status_digest: $working_tree_status_digest,
    artifact_digests: {
      cargo_lock: $cargo_lock_digest,
      edge_contract: $edge_contract_digest,
      qualification_profile: $profile_digest,
      miri_log: $miri_log_digest,
      address_sanitizer_log: $asan_log_digest,
      thread_sanitizer_log: $tsan_log_digest
    },
    checks: {
      miri_disable_isolation: "PASS",
      address_sanitizer: "PASS",
      thread_sanitizer: "PASS"
    },
    public_boundary_blackbox_coverage: "recorded separately by run-module-gates.sh",
    overall_module_complete: false
  }' >"${evidence_dir}/deep-check-summary.json"

jq . "${evidence_dir}/deep-check-summary.json"
python3 "${script_dir}/validate-edge-evidence.py" \
  --repo "${repo_root}" --evidence "${evidence_dir}/deep-check-summary.json"
if [[ "${evidence_result}" == "HOLD" ]]; then
  exit 2
fi
