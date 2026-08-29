#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"
run_id="${MASI_ML_STARTUP_RUN_ID:?MASI_ML_STARTUP_RUN_ID is required}"
evidence_root="${MASI_ML_STARTUP_EVIDENCE_DIR:?MASI_ML_STARTUP_EVIDENCE_DIR is required}"
[[ "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ ]] || { echo "invalid Offline ML startup run id" >&2; exit 64; }
[[ ! -e "${evidence_root}" && ! -L "${evidence_root}" ]] || { echo "Offline ML startup evidence root exists" >&2; exit 64; }
mkdir -p -- "${evidence_root}"
temporary_root="$(mktemp -d /tmp/masi-ml-system-startup.XXXXXX)"
cleanup() {
  if [[ "${temporary_root}" == /tmp/masi-ml-system-startup.* ]]; then
    rm -rf -- "${temporary_root}"
  fi
}
trap cleanup EXIT

release_root="${temporary_root}/release"
candidate_root="${temporary_root}/candidate"
"${repo_root}/ml-py/scripts/build-release-runtime.sh" \
  "${release_root}" "${evidence_root}/release.json"
"${repo_root}/ml-py/.venv/bin/python" \
  "${repo_root}/ml-py/scripts/run-release-blackbox.py" \
  --repo "${repo_root}" --entrypoint "${release_root}/venv/bin/masi-offline-ml" \
  --output "${candidate_root}" --evidence "${evidence_root}/blackbox.json"
image_tag="$(printf '%s' "${run_id,,}" | sed -E 's/[^a-z0-9_.-]+/-/g' | cut -c1-100)"
image_ref="masi-offline-ml:system-${image_tag}"
MASI_ML_IMAGE_REF="${image_ref}" MASI_ML_BUILD_EVIDENCE="${evidence_root}/image-build.json" \
  "${repo_root}/ml-py/scripts/build-image.sh"
"${repo_root}/ml-py/.venv/bin/python" "${repo_root}/ml-py/scripts/run-oci-smoke.py" \
  --image "${image_ref}" --expected-blackbox "${evidence_root}/blackbox.json" \
  --evidence "${evidence_root}/oci.json"
