#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/../.." && pwd)"
if [[ "${MASI_RUNTIME_SMOKE_WRAPPED:-0}" != "1" ]]; then
  exec python3 "${repo_root}/scripts/ci/run_bounded_runtime_smoke.py" \
    --repo "${repo_root}" --module db \
    --timeout-seconds "${MASI_DB_OCI_TOTAL_TIMEOUT_SECONDS:-1200}" -- \
    "${script_dir}/run-oci-smoke.sh" "$@"
fi
if [[ "$#" -ne 5 ]]; then
  echo "usage: run-oci-smoke.sh DB_IMAGE PGBOUNCER_IMAGE POSTGRES_IMAGE FRESH_OUTPUT_JSON RUN_ID" >&2
  exit 64
fi
db_image="$1"
pgbouncer_image="$2"
postgres_image="$3"
output="$4"
run_id="$5"
if [[ ! "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ ]]; then
  echo "invalid OCI run id" >&2
  exit 64
fi
output_parent="$(dirname -- "${output}")"
if [[ "${output}" != /* || -e "${output}" || -L "${output}" \
  || ! -d "${output_parent}" || -L "${output_parent}" \
  || "$(realpath -e -- "${output_parent}")" != "$(realpath -m -s -- "${output_parent}")" ]]; then
  echo "OCI output must be a fresh absolute file" >&2
  exit 64
fi
command -v timeout >/dev/null 2>&1 || { echo "missing timeout" >&2; exit 69; }
temporary="$(mktemp "${output}.XXXXXX")"
trap 'unlink -- "${temporary}" 2>/dev/null || true' EXIT
db_user="$(timeout 30s docker image inspect --format '{{.Config.User}}' "${db_image}")"
pool_user="$(timeout 30s docker image inspect --format '{{.Config.User}}' "${pgbouncer_image}")"
postgres_user="$(timeout 30s docker image inspect --format '{{.Config.User}}' "${postgres_image}")"
[[ "${db_user}" == "70:70" ]]
[[ "${pool_user}" == "10003:10003" ]]
[[ "${postgres_user}" == "70:70" ]]

chain="$(timeout --signal=TERM --kill-after=10s 60s docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 64 --memory 256m \
  "${db_image}" chain)"
jq -e '.migration_count==31 and .chain_digest=="sha256:e6b697e7dba9a0e080b32c5f67af9250b4973587727b2d536a2675ea67278b48"' \
  <<<"${chain}" >/dev/null
db_help="$(timeout --signal=TERM --kill-after=10s 60s docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 64 --memory 256m \
  "${db_image}" help)"
grep -Fq 'inspect-recovery' <<<"${db_help}"
grep -Fq 'inspect-replica' <<<"${db_help}"
pool_version="$(timeout --signal=TERM --kill-after=10s 60s docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=4m \
  "${pgbouncer_image}" --version 2>&1)"
grep -Fq 'PgBouncer 1.25.2' <<<"${pool_version}"
postgres_version="$(timeout --signal=TERM --kill-after=10s 60s docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --entrypoint postgres "${postgres_image}" --version)"
gosu_version="$(timeout --signal=TERM --kill-after=10s 60s docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --entrypoint gosu "${postgres_image}" --version)"
grep -Fq 'PostgreSQL) 18.6' <<<"${postgres_version}"
grep -Fq '1.19 (go1.26.6' <<<"${gosu_version}"

db_id="$(timeout 30s docker image inspect --format '{{.Id}}' "${db_image}")"
pool_id="$(timeout 30s docker image inspect --format '{{.Id}}' "${pgbouncer_image}")"
postgres_id="$(timeout 30s docker image inspect --format '{{.Id}}' "${postgres_image}")"
jq -n --arg run_id "${run_id}" --arg db_image "${db_image}" --arg pgbouncer_image "${pgbouncer_image}" \
  --arg postgres_image "${postgres_image}" --arg db_id "${db_id}" \
  --arg pgbouncer_id "${pool_id}" --arg postgres_id "${postgres_id}" \
  --arg db_user "${db_user}" --arg pgbouncer_user "${pool_user}" \
  --arg postgres_user "${postgres_user}" \
  --arg chain_digest "$(jq -r .chain_digest <<<"${chain}")" \
  --arg pgbouncer_version "${pool_version}" \
  --arg postgres_version "${postgres_version}" --arg gosu_version "${gosu_version}" \
  --argjson migration_count "$(jq -r .migration_count <<<"${chain}")" \
  '{schema_version:"postgresql-state-oci-smoke/v1",test_id:"TEST-DB-OCI-001",
    module_id:"MOD-DB-001",run_id:$run_id,result:"PASS",qualification:"QUALIFIED",
    images:{db:$db_image,pgbouncer:$pgbouncer_image,postgres:$postgres_image},
    image_ids:{db:$db_id,pgbouncer:$pgbouncer_id,postgres:$postgres_id},
    users:{db:$db_user,pgbouncer:$pgbouncer_user,postgres:$postgres_user},
    read_only_rootfs:true,capabilities_dropped:true,
    migration_count:$migration_count,migration_chain_digest:$chain_digest,
    pgbouncer_version:$pgbouncer_version,postgres_version:$postgres_version,
    gosu_version:$gosu_version}' >"${temporary}"
python3 "${script_dir}/validate-json.py" \
  --schema "${repo_root}/contracts/evidence/postgresql-state-oci/v1/schema.json" \
  --document "${temporary}" >/dev/null
mv -T -- "${temporary}" "${output}"
trap - EXIT
