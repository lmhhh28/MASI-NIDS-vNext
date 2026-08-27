#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"
generator_root="${repo_root}/contracts/openapi/v1/typescript-client"
generated_root="${repo_root}/contracts/generated/typescript/control-api"

tree_digest() {
  printf 'sha256:%s\n' "$(tar --sort=name --mtime=@1786406400 --owner=0 --group=0 --numeric-owner \
    -cf - -C "${generated_root}" . | sha256sum | awk '{print $1}')"
}

if [[ ! -d "${generated_root}" || ! -f "${generator_root}/package-lock.json" \
  || "$(jq -r '.packages[""].devDependencies["@hey-api/openapi-ts"]' "${generator_root}/package-lock.json")" != "0.99.0" \
  || "$(jq -r '.packages[""].devDependencies.typescript' "${generator_root}/package-lock.json")" != "5.9.3" ]]; then
  echo "TypeScript client generator/output or exact dependency pins are missing" >&2
  exit 1
fi

before="$(tree_digest)"
npm --prefix "${generator_root}" ci --ignore-scripts --no-audit --no-fund
npm --prefix "${generator_root}" run generate
chmod -R u=rwX,go=rX "${generated_root}"
npm --prefix "${generator_root}" run typecheck
after="$(tree_digest)"
if [[ "${before}" != "${after}" ]]; then
  echo "generated TypeScript client is stale: before=${before} after=${after}" >&2
  exit 1
fi
if ! grep -q 'export const getDashboard' "${generated_root}/sdk.gen.ts" \
  || ! grep -q 'export const listEvents' "${generated_root}/sdk.gen.ts" \
  || ! grep -q 'export const registerBoundedCapture' "${generated_root}/sdk.gen.ts" \
  || ! grep -q 'export const submitAnalysisTask' "${generated_root}/sdk.gen.ts"; then
  echo "generated TypeScript client is missing required public operations" >&2
  exit 1
fi
echo "typescript-client-fresh ${after}"
