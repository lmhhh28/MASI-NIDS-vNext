#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -ne 2 ]]; then
  echo "usage: $0 MODULE UNIQUE_RUN_ID" >&2
  exit 64
fi

module="$1"
run_id="$2"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"

if [[ ! "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ ]]; then
  echo "invalid module-gate run id" >&2
  exit 64
fi

cd -- "${repo_root}"
case "${module}" in
  p4)
    if [[ "${EUID}" -ne 0 ]]; then
      command -v sudo >/dev/null 2>&1 || {
        echo "P4 formal gate requires an isolated root-capable runner" >&2
        exit 69
      }
      exec sudo --preserve-env=MASI_BMV2_SOURCE_ARCHIVE,MASI_PI_SOURCE_ARCHIVE,MASI_TRIVY_CACHE,MASI_COSIGN_KEY_DIR,MASI_P4_RUNTIME_IMAGE,MASI_P4_RUNNER_IMAGE \
        env MASI_P4_RUN_ID="${run_id}" MASI_P4_QUALIFICATION_MODE=formal \
        "${repo_root}/testkit/p4_switch/run-module-e2e.sh"
    fi
    MASI_P4_RUN_ID="${run_id}" MASI_P4_QUALIFICATION_MODE=formal \
      "${repo_root}/testkit/p4_switch/run-module-e2e.sh"
    ;;
  edge)
    cd -- "${repo_root}/edge-rs"
    MASI_EDGE_GATE_RUN_ID="${run_id}" MASI_EDGE_FORMAL_SOAK=1 \
      ./scripts/run-module-gates.sh
    ;;
  inference)
    cd -- "${repo_root}/infer-cpp"
    MASI_INF_GATE_RUN_ID="${run_id}" MASI_INF_FORMAL_SOAK=1 \
      ./scripts/run-module-gates.sh
    ;;
  control)
    cd -- "${repo_root}/control-go"
    MASI_CONTROL_GATE_RUN_ID="${run_id}" MASI_CONTROL_FORMAL_SOAK=1 \
      ./scripts/run-module-gates.sh
    ;;
  db)
    cd -- "${repo_root}/db"
    MASI_DB_GATE_RUN_ID="${run_id}" MASI_DB_FORMAL_SOAK=1 \
      ./scripts/run-module-gates.sh
    ;;
  plugin-host)
    cd -- "${repo_root}/plugin-host-rs"
    MASI_PLUGIN_HOST_RUN_ID="${run_id}" ./scripts/run-module-gates.sh
    ;;
  analysis)
    cd -- "${repo_root}/analysis-py"
    uv sync --frozen
    MASI_ANALYSIS_RUN_ID="${run_id}" ./scripts/run-module-gates.sh
    ;;
  offline-ml)
    cd -- "${repo_root}/ml-py"
    uv sync --frozen --extra dev
    ./scripts/run-module-gates.sh "${run_id}"
    ;;
  web)
    cd -- "${repo_root}/web"
    MASI_WEB_GATE_RUN_ID="${run_id}" MASI_WEB_FORMAL_SOAK=1 \
      ./scripts/run-module-gates.sh
    ;;
  *)
    echo "unknown module: ${module}" >&2
    exit 64
    ;;
esac
