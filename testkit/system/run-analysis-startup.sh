#!/usr/bin/env bash
set -euo pipefail

umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"
run_id="${MASI_ANALYSIS_STARTUP_RUN_ID:?MASI_ANALYSIS_STARTUP_RUN_ID is required}"
evidence_root="${MASI_ANALYSIS_STARTUP_EVIDENCE_DIR:?MASI_ANALYSIS_STARTUP_EVIDENCE_DIR is required}"
[[ "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ ]] || { echo "invalid Analysis startup run id" >&2; exit 64; }
[[ ! -e "${evidence_root}" && ! -L "${evidence_root}" ]] || { echo "Analysis startup evidence root exists" >&2; exit 64; }
mkdir -p -- "${evidence_root}"
image_tag="$(printf '%s' "${run_id,,}" | sed -E 's/[^a-z0-9_.-]+/-/g' | cut -c1-100)"
image_ref="masi-analysis:system-${image_tag}"
MASI_ANALYSIS_IMAGE_REF="${image_ref}" \
MASI_ANALYSIS_BUILD_EVIDENCE="${evidence_root}/image-build.json" \
  "${repo_root}/analysis-py/scripts/build-image.sh"
exec "${repo_root}/analysis-py/.venv/bin/python" \
  "${repo_root}/analysis-py/scripts/run-oci-smoke.py" \
  --image "${image_ref}" --allow-local-candidate --evidence "${evidence_root}/oci.json"
