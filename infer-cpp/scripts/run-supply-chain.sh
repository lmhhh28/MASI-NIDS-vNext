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

# ---------------------------------------------------------------------------
# Vendored third-party source verification.
# Every vendored file must match the registered upstream digest byte for byte,
# and the Gateway sources must never reference a forbidden Triton RPC.
# ---------------------------------------------------------------------------
vendored_registry="${repo_root}/contracts/supply-chain/v1/central-inference-vendored-sources.json"
[[ -f "${vendored_registry}" ]] || fail "vendored source registry missing"
vendored_checked=0
while IFS=$'\t' read -r vendored_path expected_digest; do
  [[ -n "${vendored_path}" ]] || continue
  abs="${repo_root}/${vendored_path}"
  [[ -f "${abs}" ]] || fail "registered vendored file missing: ${vendored_path}"
  observed="$(sha256sum "${abs}" | awk '{print $1}')"
  if [[ "${observed}" != "${expected_digest}" ]]; then
    fail "vendored file digest drift: ${vendored_path} observed=${observed} registered=${expected_digest}"
  fi
  vendored_checked=$((vendored_checked + 1))
done < <(jq -r '.files[] | [.vendored_path, .sha256] | @tsv' "${vendored_registry}")
[[ "${vendored_checked}" -gt 0 ]] || fail "vendored source registry lists no files"

forbidden_hits="${supply_dir}/forbidden-triton-rpcs.txt"
: >"${forbidden_hits}"
while read -r rpc; do
  [[ -n "${rpc}" ]] || continue
  # Match only call sites in the Gateway's own sources; the vendored protocol
  # definition legitimately declares the full upstream service.
  if grep -rn --include='*.cc' --include='*.h' --include='*.cpp' \
      -e "->${rpc}(" -e "\\.${rpc}(" "${inf_root}/src" >>"${forbidden_hits}" 2>/dev/null; then
    :
  fi
done < <(jq -r '.method_allowlist.forbidden_client_rpcs[]' "${vendored_registry}")
if [[ -s "${forbidden_hits}" ]]; then
  cat -- "${forbidden_hits}" >&2
  fail "Gateway sources call a forbidden Triton RPC (model-control or shared memory)"
fi

# ---------------------------------------------------------------------------
# Registry vs actual toolchain. Wire evidence must be bound to the registered
# gRPC/protobuf version, so a drifted local toolchain fails the gate.
# ---------------------------------------------------------------------------
components_registry="${repo_root}/contracts/supply-chain/v1/central-inference-cpu-components.json"
registered_grpc="$(jq -r '.components[] | select(.name=="gRPC C++") | .version' "${components_registry}" | sed 's/^v//')"
registered_protobuf="$(jq -r '.components[] | select(.name=="Protocol Buffers") | .version' "${components_registry}")"
grpc_prefix="${MASI_INF_GRPC_PREFIX:-/opt/masi-toolchain/grpc-${registered_grpc}}"
observed_grpc=""
observed_protobuf=""
if [[ -f "${grpc_prefix}/lib/pkgconfig/grpc++.pc" ]]; then
  observed_grpc="$(PKG_CONFIG_PATH="${grpc_prefix}/lib/pkgconfig" pkg-config --modversion grpc++ 2>/dev/null || true)"
  observed_protobuf="$(PKG_CONFIG_PATH="${grpc_prefix}/lib/pkgconfig" pkg-config --modversion protobuf 2>/dev/null || true)"
fi
[[ "${observed_grpc}" == "${registered_grpc}" ]] || \
  fail "gRPC version drift: registry=${registered_grpc} observed=${observed_grpc:-<not found at ${grpc_prefix}>}"
[[ "${observed_protobuf}" == "${registered_protobuf}" ]] || \
  fail "protobuf version drift: registry=${registered_protobuf} observed=${observed_protobuf:-<not found>}"

jq -n --arg grpc "${observed_grpc}" --arg protobuf "${observed_protobuf}" \
  --arg prefix "${grpc_prefix}" --argjson vendored_files "${vendored_checked}" '{
    schema_version: "central-inference-toolchain-binding/v1",
    grpc_version: $grpc,
    protobuf_version: $protobuf,
    toolchain_prefix: $prefix,
    vendored_files_verified: $vendored_files,
    forbidden_triton_rpc_references: 0
  }' >"${supply_dir}/toolchain-binding.json"

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


# Reproducibility diagnostics: the two builds must embed the SAME absolute gRPC
# source path, because gRPC bakes __FILE__ into its assertion/log strings. When
# the digests disagree, these observations say which input drifted instead of
# leaving the gate with an unexplained mismatch (ISSUE-INF-001).
registered_grpc_source_dir="${MASI_INF_GRPC_SOURCE_DIR:-/opt/masi-toolchain/grpc-src}"
embedded_path_count_rebuilt=0
embedded_path_count_image=0
foreign_path_samples="[]"
# grep exits non-zero when a pattern is absent, which under `set -o pipefail`
# would abort these pipelines, so the non-match is swallowed inside the group
# and the final jq always emits exactly one JSON document.
if [[ -f "${supply_dir}/masi_inference_gateway.offline-rebuild.bin" ]]; then
  embedded_path_count_rebuilt="$(strings -a \
    "${supply_dir}/masi_inference_gateway.offline-rebuild.bin" \
    | { grep -c -- "${registered_grpc_source_dir}" || true; })"
fi
if [[ -f "${supply_dir}/masi_inference_gateway.image.bin" ]]; then
  embedded_path_count_image="$(strings -a \
    "${supply_dir}/masi_inference_gateway.image.bin" \
    | { grep -c -- "${registered_grpc_source_dir}" || true; })"
  foreign_path_samples="$(strings -a "${supply_dir}/masi_inference_gateway.image.bin" \
    | { grep -oE '(/tmp|/build|/workspace|/root|/home)/[A-Za-z0-9_./-]*(grpc|json)[A-Za-z0-9_./-]*' \
        || true; } \
    | sort -u | head -5 | jq -R . | jq -sc .)"
fi
[[ -n "${embedded_path_count_rebuilt}" ]] || embedded_path_count_rebuilt=0
[[ -n "${embedded_path_count_image}" ]] || embedded_path_count_image=0
[[ -n "${foreign_path_samples}" ]] || foreign_path_samples="[]"
vendored_json_digest="$(jq -r '.files[] | select(.vendored_path | endswith("nlohmann/json.hpp")) | .sha256' \
  "${vendored_registry}" 2>/dev/null || true)"

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
  --argjson binary_digest_match "${binary_digest_match}" \
  --arg registered_grpc_source_dir "${registered_grpc_source_dir}" \
  --argjson embedded_path_count_rebuilt "${embedded_path_count_rebuilt:-0}" \
  --argjson embedded_path_count_image "${embedded_path_count_image:-0}" \
  --argjson foreign_path_samples "${foreign_path_samples}" \
  --arg vendored_json_digest "${vendored_json_digest}" \
  --arg source_date_epoch "${SOURCE_DATE_EPOCH:-1786233600}" '{
    schema_version: "central-inference-offline-rebuild/v1",
    started_at: $started_at,
    finished_at: $finished_at,
    command: "cmake --build build/cpu-release --target masi_inference_gateway",
    source_tree_digest: $source_tree_digest,
    exit_code: $exit_code,
    rebuilt_binary_digest: $rebuilt_binary_digest,
    image_binary_digest: $image_binary_digest,
    binary_digest_match: $binary_digest_match,
    source_date_epoch: $source_date_epoch,
    vendored_json_digest: (if $vendored_json_digest == "" then null
                           else "sha256:" + $vendored_json_digest end),
    embedded_grpc_source_dir: $registered_grpc_source_dir,
    embedded_grpc_source_path_occurrences: {
      offline_rebuild: $embedded_path_count_rebuilt,
      image: $embedded_path_count_image,
      equal: ($embedded_path_count_rebuilt == $embedded_path_count_image)
    },
    foreign_source_path_samples_in_image: $foreign_path_samples
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
  if [[ -z "${image_binary_digest}" ]]; then
    # Missing precondition, not a contradiction: no OCI image is available to
    # compare against. Report HOLD instead of FAIL and never PASS.
    evidence_result="HOLD"
    evidence_qualification="NOT_QUALIFIED"
    qualification_reason="OCI_IMAGE_NOT_BUILT_FOR_BINARY_COMPARISON"
  elif [[ "${offline_rebuild_status}" -ne 0 ]]; then
    evidence_result="FAIL"
    evidence_qualification="NOT_QUALIFIED"
    qualification_reason="OFFLINE_REBUILD_FAILED"
  else
    evidence_result="FAIL"
    evidence_qualification="NOT_QUALIFIED"
    qualification_reason="OFFLINE_REBUILD_BINARY_DIGEST_MISMATCH"
  fi
fi

sbom_generated=false
if [[ -s "${supply_dir}/source.spdx.json" ]]; then
  sbom_generated=true
fi
scan_completed=false
if [[ -s "${supply_dir}/trivy-image.json" ]]; then
  scan_completed=true
fi
toolchain_binding_digest="sha256:$(sha256sum "${supply_dir}/toolchain-binding.json" | awk '{print $1}')"

jq -n --arg run_id "${run_id}" --arg reason "${qualification_reason}" \
  --arg source_revision "${source_revision}" --arg source_tree_digest "${source_tree_digest}" \
  --argjson working_tree_dirty "${working_tree_dirty}" \
  --arg working_tree_status_digest "${working_tree_status_digest}" \
  --arg result "${evidence_result}" --arg qualification "${evidence_qualification}" \
  --argjson offline_rebuild "${binary_digest_match}" \
  --argjson sbom_generated "${sbom_generated}" \
  --argjson scan_completed "${scan_completed}" \
  --arg toolchain_binding_digest "${toolchain_binding_digest}" \
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
    checks:{offline_rebuild:$offline_rebuild,sbom_generated:$sbom_generated,scan:$scan_completed},
    toolchain_binding_digest:$toolchain_binding_digest,
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