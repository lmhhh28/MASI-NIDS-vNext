#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
db_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${db_root}/.." && pwd)"
registry="${repo_root}/contracts/supply-chain/v1/postgresql-state-components.json"
schema="${repo_root}/contracts/supply-chain/v1/schema.json"
source_lock="${repo_root}/deploy/postgresql-state/pgbouncer/source.lock.json"
postgres_lock="${repo_root}/deploy/postgresql-state/postgres/source.lock.json"
go_lock="${repo_root}/deploy/postgresql-state/toolchain/go-linux-amd64.lock.json"

python3 "${script_dir}/validate-json.py" --schema "${schema}" --document "${registry}" >/dev/null

while IFS=$'\t' read -r module version; do
  if ! jq -e --arg mod "${module}" --arg version "${version}" \
      '.components[] | select(.name==$mod and .version==$version and .runtime_download==false)' \
      "${registry}" >/dev/null; then
    echo "Go module absent from exact PostgreSQL State component registry: ${module}@${version}" >&2
    exit 1
  fi
done < <(cd -- "${db_root}" && GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
  go list -deps -json ./cmd/masi-dbctl ./cmd/masi-dbload | \
  jq -sr '[.[] | .Module | select(. != null and .Main != true) | {path:.Path,version:.Version}] |
    unique_by(.path)[] | [.path,.version] | @tsv' -r)

forbidden_modules="$(cd -- "${db_root}" && GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off \
  go list -m -json all | jq -sr '
  [.[] | .Path | select(
    test("(^|/)(redis|go-redis|redigo)(/|$)") or
    test("^github\\.com/minio/") or
    test("^github\\.com/aws/aws-sdk-go") or
    test("^cloud\\.google\\.com/go/storage$") or
    test("^github\\.com/Azure/azure-sdk-for-go($|/)")
  )] | .[]' -r)"
if [[ -n "${forbidden_modules}" ]]; then
  echo "forbidden Redis/object-storage dependency in first-release State closure: ${forbidden_modules}" >&2
  exit 1
fi
if grep -Eiq '^[[:space:]]{2}(redis|minio|object-storage|s3):[[:space:]]*$' \
    "${repo_root}/deploy/postgresql-state/compose.module-gates.yaml"; then
  echo "forbidden Redis/object-storage service in PostgreSQL State module topology" >&2
  exit 1
fi

jq -e '
  .version == "1.25.2" and
  .archive_sha256 == "sha256:924ad35113fd0a71c8e2dbe85b5d03445532e2b7b37a9f8a48983beea238b332" and
  .runtime_download == false
' "${source_lock}" >/dev/null
jq -e --slurpfile lock "${source_lock}" '
  .components[] | select(.name=="PgBouncer") |
  .version==$lock[0].version and .archive_sha256==$lock[0].archive_sha256 and
  .runtime_download==false
' "${registry}" >/dev/null

grep -Fqx 'FROM golang:1.26.6-alpine@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS build' "${db_root}/Dockerfile"
grep -Fqx 'FROM alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce' "${db_root}/Dockerfile"
grep -Fqx 'FROM alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce AS build' "${repo_root}/deploy/postgresql-state/pgbouncer/Dockerfile"
grep -Fqx 'FROM alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce' "${repo_root}/deploy/postgresql-state/pgbouncer/Dockerfile"
jq -e '.postgresql.version=="18.6" and .postgresql.base_image=="postgres@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2" and .gosu.version=="1.19" and .gosu.toolchain=="go1.26.6" and .gosu.archive_sha256=="sha256:cd9719b775dbfedae53923c9b0dc792b66d42c51e0b36652ed6f747fbadc0164" and .runtime_download==false' "${postgres_lock}" >/dev/null
jq -e --slurpfile lock "${postgres_lock}" '.components[] | select(.name=="tianon/gosu") | .version==$lock[0].gosu.version and .archive_sha256==$lock[0].gosu.archive_sha256 and .runtime_download==false' "${registry}" >/dev/null
jq -e '.version=="1.26.6" and .platform=="linux-amd64" and .archive_sha256=="sha256:708effb774be8237570d0add163225abbdfaf4fca28b2611df167beba4feef89" and .size_bytes==66890545 and .runtime_download==false' "${go_lock}" >/dev/null
jq -e --slurpfile lock "${go_lock}" '.components[] | select(.name=="Go toolchain and standard library") | .version==$lock[0].version and .archive_sha256==$lock[0].archive_sha256 and .runtime_download==false' "${registry}" >/dev/null
grep -Fqx 'FROM postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2' "${repo_root}/deploy/postgresql-state/postgres/Dockerfile"

echo "postgresql-state-supply-chain-registry-locks-and-no-redis-object-store-ok"
