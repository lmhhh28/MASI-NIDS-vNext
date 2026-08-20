#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
control_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${control_root}/.." && pwd)"
registry="${repo_root}/contracts/supply-chain/v1/control-core-components.json"
schema="${repo_root}/contracts/supply-chain/v1/schema.json"
package_lock="${repo_root}/contracts/openapi/v1/typescript-client/package-lock.json"

python3 "${script_dir}/validate-evidence.py" --schema "${schema}" --document "${registry}" >/dev/null

while IFS=$'\t' read -r module version; do
  if ! jq -e --arg mod "${module}" --arg version "${version}" \
      '.components[] | select(.name==$mod and .version==$version and .runtime_download==false)' \
      "${registry}" >/dev/null; then
    echo "Go module is absent from exact component registry: ${module}@${version}" >&2
    exit 1
  fi
done < <(cd -- "${control_root}" && go list -deps -json ./cmd/control-core ./cmd/migrate | \
  jq -sr '[.[] | .Module | select(. != null and .Main != true) | {path:.Path,version:.Version}] |
    unique_by(.path)[] | [.path,.version] | @tsv' -r)

if ! jq -e '
  .lockfileVersion == 3 and
  ([.packages | to_entries[] | select(.key != "") |
    select((.value.link // false) == false) |
    select((.value.version // "") == "" or (.value.license // "") == "" or (.value.integrity // "") == "")] | length) == 0
' "${package_lock}" >/dev/null; then
  echo "npm codegen inventory lacks exact version/license/integrity" >&2
  exit 1
fi

grep -Fxq 'FROM golang:1.26.6-alpine@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS build' "${control_root}/Dockerfile"
grep -Fxq 'FROM alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce' "${control_root}/Dockerfile"

echo "control-core-supply-chain-registry-and-locks-ok"
