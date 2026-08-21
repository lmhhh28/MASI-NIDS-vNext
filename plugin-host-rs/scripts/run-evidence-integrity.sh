#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 3 ]]; then
  echo "usage: run-evidence-integrity.sh REPO RUN_DIR SUMMARY" >&2
  exit 64
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 "${script_dir}/test-evidence-tools.py"
python3 "${script_dir}/validate-module-evidence.py" \
  --repo "$1" --run-dir "$2" --summary "$3" --negative-self-test
