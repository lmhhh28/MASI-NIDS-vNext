#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
module_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${module_root}/.." && pwd)"

"${module_root}/.venv/bin/python" "${repo_root}/control-go/scripts/validate-public-contracts.py" --repo "${repo_root}"
go -C "${repo_root}/control-go" test ./internal/a2a
