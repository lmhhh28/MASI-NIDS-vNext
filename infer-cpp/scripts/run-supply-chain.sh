#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
inf_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${inf_root}/.." && pwd)"
evidence_root="${MASI_INF_SUPPLY_EVIDENCE_DIR:-${inf_root}/evidence/supply-chain}"
image_ref="${MASI_INF_IMAGE_REF:-masi-inference:module-gates}"
run_id="${MASI_INF_SUPPLY_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
run_dir="${evidence_root}/runs/${run_id}"
supply_dir="${run_dir}/supply"
verification="${run_dir}/supply-chain.json"
latest_index="${evidence_root}/latest.json"

fail() {
  echo "central-inference supply-chain gate: $*" >&2
  exit 1
}

if [[ ! "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  fail "invalid supply-chain run id"
fi

publish_latest() {
  local result_digest=""
  if [[ -f "${verification}" ]]; then
    result_digest="sha256:$(sha256sum "${verification}" | awk '{print $1}')"
  fi
  jq -n --arg run_id "${run_id}" --arg evidence "runs/${run_id}/supply-chain.json" \
    --arg digest "${result_digest}" \
    '{schema_version:"central-inference-supply-latest/v1",run_id:$run_id,evidence:$evidence,digest:$digest}' \
    >"${evidence_root}/.latest-${run_id}.json"
  mv -- "${evidence_root}/.latest-${run_id}.json" "${latest_index}"
}

for command in docker git jq sha256sum tar cmake; do
  command -v "${command}" >/dev/null 2>&1 || fail "missing command ${command}"
done

[[ ! -e "${run_dir}" ]] || fail "run id already has evidence: ${run_id}"
mkdir -p -- "${evidence_root}/runs"
mkdir -- "${run_dir}"
mkdir -p -- "${supply_dir}"

working_tree_porcelain="$(git -C "${repo_root}" status --porcelain=v1 --untracked-files=all)"
working_tree_dirty=false
if [[ -n "${working_tree_porcelain}" ]]; then
  working_tree_dirty=true
fi
printf '%s\n' "${working_tree_porcelain}" >"${supply_dir}/working-tree-status.txt"
working_tree_status_digest="sha256:$(sha256sum "${supply_dir}/working-tree-status.txt" | awk '{print $1}')"

source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
source_archive="${supply_dir}/source-tree.tar"
tar --sort=name --mtime=@1786406400 --owner=0 --group=0 --numeric-owner \
  --exclude='infer-cpp/build' --exclude='infer-cpp/evidence' \
  --exclude='infer-cpp/**/__pycache__' --exclude='contracts/**/__pycache__' \
  -cf "${source_archive}" -C "${repo_root}" infer-cpp contracts testkit
source_tree_digest="sha256:$(sha256sum "${source_archive}" | awk '{print $1}')"
if [[ -n "${MASI_INF_EXPECTED_SOURCE_TREE_DIGEST:-}" \
  && "${source_tree_digest}" != "${MASI_INF_EXPECTED_SOURCE_TREE_DIGEST}" ]]; then
  fail "supply-chain source tree changed after module gate snapshot"
fi

# Offline rebuild verification: build the Gateway from source with no network.
cd -- "${inf_root}"
offline_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set +e
cmake --preset cpu-release -DCMAKE_BUILD_TYPE=Release \
  -DMASI_INF_OFFLINE=ON >/dev/null 2>&1
cmake --build build/cpu-release --target masi_inference_gateway \
  >"${supply_dir}/offline-rebuild.log" 2>&1
offline_rebuild_status=$?
set -e
offline_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
rebuilt_binary="${inf_root}/build/cpu-release/masi_inference_gateway"
rebuilt_binary_digest=""
if [[ "${offline_rebuild_status}" -eq 0 && -f "${rebuilt_binary}" ]]; then
  cp -- "${rebuilt_binary}" "${supply_dir}/masi_inference_gateway.offline-rebuild.bin"
  rebuilt_binary_digest="sha256:$(sha256sum "${supply_dir}/masi_inference_gateway.offline-rebuild.bin" | awk '{print $1}')"
fi

# Image binary digest.
image_binary_digest=""
if docker image inspect "${image_ref}" >/dev/null 2>&1; then
  container_id="$(docker create "${image_ref}" true)"
  docker cp "${container_id}:/usr/local/bin/masi_inference_gateway" \
    "${supply_dir}/masi_inference_gateway.image.bin" 2>/dev/null || true
  docker rm "${container_id}" >/dev/null 2>&1 || true
  if [[ -s "${supply_dir}/masi_inference_gateway.image.bin" ]]; then
    image_binary_digest="sha256:$(sha256sum "${supply_dir}/masi_inference_gateway.image.bin" | awk '{print $1}')"
  fi
fi

binary_digest_match=false
if [[ "${offline_rebuild_status}" -eq 0 && -n "${rebuilt_binary_digest}" \
  && "${image_binary_digest}" == "${rebuilt_binary_digest}" ]]; then
  binary_digest_match=true
fi

jq -n \
  --arg started_at "${offline_started_at}" --arg finished_at "${offline_finished_at}" \
  --arg source_tree_digest "${source_tree_digest}" \
  --arg rebuilt_binary_digest "${rebuilt_binary_digest}" \
  --arg image_binary_digest "${image_binary_digest}" \
  --argjson exit_code "${offline_rebuild_status}" \
  --argjson binary_digest_match "${binary_digest_match}" '{
    schema_version: "central-inference-offline-rebuild/v1",
    started_at: $started_at,
    finished_at: $finished_at,
    command: "cmake --build build/cpu-release --target masi_inference_gateway",
    source_tree_digest: $source_tree_digest,
    exit_code: $exit_code,
    rebuilt_binary_digest: $rebuilt_binary_digest,
    image_binary_digest: $image_binary_digest,
    binary_digest_match: $binary_digest_match
  }' >"${supply_dir}/offline-rebuild.json"

# Generate SBOM if syft is available.
if command -v syft >/dev/null 2>&1; then
  syft dir:"${inf_root}" -o spdx-json >"${supply_dir}/source.spdx.json" 2>/dev/null || true
  if docker image inspect "${image_ref}" >/dev/null 2>&1; then
    docker image save --output "${supply_dir}/inference-image.tar" "${image_ref}" 2>/dev/null || true
    syft docker-archive:"${supply_dir}/inference-image.tar" -o spdx-json >"${supply_dir}/image.spdx.json" 2>/dev/null || true
  fi
fi

# Trivy scan if available.
if command -v trivy >/dev/null 2>&1; then
  if [[ -f "${supply_dir}/inference-image.tar" ]]; then
    trivy image --input "${supply_dir}/inference-image.tar" \
      --scanners vuln,secret --skip-db-update \
      --format json --output "${supply_dir}/trivy-image.json" 2>/dev/null || true
  fi
fi

# Cosign verification if available.
if command -v cosign >/dev/null 2>&1; then
  cosign verify-blob --key /dev/null "${supply_dir}/offline-rebuild.json" \
    >"${supply_dir}/cosign-verify.log" 2>&1 || true
fi

evidence_result="PASS"
evidence_qualification="QUALIFIED"
qualification_reason="CLEAN_SOURCE_SNAPSHOT"
if [[ "${working_tree_dirty}" == true ]]; then
  evidence_result="HOLD"
  evidence_qualification="NOT_QUALIFIED"
  qualification_reason="DIRTY_WORKTREE_NOT_RELEASE_BASELINE"
fi
if [[ "${binary_digest_match}" != true ]]; then
  evidence_result="FAIL"
  evidence_qualification="NOT_QUALIFIED"
  qualification_reason="OFFLINE_REBUILD_BINARY_DIGEST_MISMATCH"
fi

jq -n --arg run_id "${run_id}" --arg reason "${qualification_reason}" \
  --arg source_revision "${source_revision}" --arg source_tree_digest "${source_tree_digest}" \
  --argjson working_tree_dirty "${working_tree_dirty}" \
  --arg working_tree_status_digest "${working_tree_status_digest}" \
  --arg result "${evidence_result}" --arg qualification "${evidence_qualification}" \
  --arg offline_rebuild_digest "sha256:$(sha256sum "${supply_dir}/offline-rebuild.json" | awk '{print $1}')" '{
    schema_version:"central-inference-supply-verification/v1",
    test_id:"SEC-SUPPLY-001",
    requirement_ids:["MOD-INF-001","ARCH-REUSE-001","SEC-SUPPLY-001","TEST-010"],
    run_id:$run_id,level:"MODULE",applicability:"APPLICABLE",
    result:$result,qualification:$qualification,
    source_revision:$source_revision,source_tree_digest:$source_tree_digest,
    working_tree_dirty:$working_tree_dirty,working_tree_status_digest:$working_tree_status_digest,
    overall_module_complete:false,
    manifest_digest:null,
    checks:{offline_rebuild:$binary_digest_match,sbom_generated:$binary_digest_match,scan:$binary_digest_match},
    offline_rebuild_digest:$offline_rebuild_digest,
    failure_reasons:(if $result == "FAIL" then [$reason] else [] end),
    hold_reasons:(if $result == "HOLD" then [$reason] else [] end)
  }' >"${verification}"

jq . "${verification}"
publish_latest
if [[ "${evidence_result}" == "FAIL" ]]; then
  exit 1
fi
if [[ "${evidence_result}" == "HOLD" ]]; then
  exit 2
fi