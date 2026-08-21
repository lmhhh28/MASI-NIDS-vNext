#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "$#" -ne 1 ]]; then
  echo "usage: prepare-runtime.sh ABSOLUTE_EMPTY_RUNTIME_DIRECTORY" >&2
  exit 64
fi

runtime_root="$1"
case "${runtime_root}" in
  /*) ;;
  *) echo "runtime directory must be absolute" >&2; exit 64 ;;
esac
if [[ "${runtime_root}" == "/" || -L "${runtime_root}" ]]; then
  echo "unsafe runtime directory" >&2
  exit 64
fi
if [[ -e "${runtime_root}" ]]; then
  if [[ ! -d "${runtime_root}" || -n "$(find "${runtime_root}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "runtime directory must be a fresh empty directory" >&2
    exit 64
  fi
else
  mkdir -m 0700 -- "${runtime_root}"
fi
runtime_root="$(realpath -e -- "${runtime_root}")"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mkdir -m 0755 -- "${runtime_root}/secrets" "${runtime_root}/tls"
mkdir -m 0755 -- "${runtime_root}/tls/ca" "${runtime_root}/tls/postgres" \
  "${runtime_root}/tls/pgbouncer" "${runtime_root}/tls/dbctl" \
  "${runtime_root}/tls/standby" "${runtime_root}/tls/restore" \
  "${runtime_root}/tls/host"

for name in bootstrap migration app replication monitoring; do
  openssl rand -hex 32 >"${runtime_root}/secrets/${name}-password"
  chmod 0600 -- "${runtime_root}/secrets/${name}-password"
done

openssl req -x509 -new -nodes -newkey rsa:3072 -sha256 -days 2 \
  -subj '/CN=MASI-NIDS PostgreSQL State module CA' \
  -keyout "${runtime_root}/tls/ca/ca.key" \
  -out "${runtime_root}/tls/ca/ca.crt" >/dev/null 2>&1
chmod 0600 -- "${runtime_root}/tls/ca/ca.key"
chmod 0444 -- "${runtime_root}/tls/ca/ca.crt"

issue_certificate() {
  local output_dir="$1"
  local basename="$2"
  local common_name="$3"
  local usage="$4"
  local subject_alt_name="$5"
  local request="${runtime_root}/tls/ca/${basename}.csr"
  local extensions="${runtime_root}/tls/ca/${basename}.ext"
  openssl req -new -nodes -newkey rsa:3072 -sha256 \
    -subj "/CN=${common_name}" \
    -keyout "${output_dir}/${basename}.key" \
    -out "${request}" >/dev/null 2>&1
  {
    printf 'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n'
    printf 'extendedKeyUsage=%s\n' "${usage}"
    if [[ -n "${subject_alt_name}" ]]; then
      printf 'subjectAltName=%s\n' "${subject_alt_name}"
    fi
  } >"${extensions}"
  openssl x509 -req -sha256 -days 2 \
    -in "${request}" \
    -CA "${runtime_root}/tls/ca/ca.crt" \
    -CAkey "${runtime_root}/tls/ca/ca.key" \
    -CAcreateserial \
    -extfile "${extensions}" \
    -out "${output_dir}/${basename}.crt" >/dev/null 2>&1
  chmod 0600 -- "${output_dir}/${basename}.key"
  chmod 0444 -- "${output_dir}/${basename}.crt"
}

issue_certificate "${runtime_root}/tls/postgres" server postgres serverAuth \
  'DNS:postgres,DNS:localhost,IP:127.0.0.1,IP:::1'
issue_certificate "${runtime_root}/tls/pgbouncer" pgbouncer pgbouncer serverAuth \
  'DNS:pgbouncer,DNS:localhost,IP:127.0.0.1,IP:::1'
issue_certificate "${runtime_root}/tls/pgbouncer" client masi_app_login clientAuth ''
issue_certificate "${runtime_root}/tls/dbctl" client masi_migration_login clientAuth ''
issue_certificate "${runtime_root}/tls/dbctl" app masi_app_login clientAuth ''
issue_certificate "${runtime_root}/tls/standby" server standby serverAuth \
  'DNS:standby,DNS:localhost,IP:127.0.0.1,IP:::1'
issue_certificate "${runtime_root}/tls/standby" replication masi_replication_login clientAuth ''
issue_certificate "${runtime_root}/tls/restore" server restore serverAuth \
  'DNS:restore,DNS:localhost,IP:127.0.0.1,IP:::1'
issue_certificate "${runtime_root}/tls/host" migration masi_migration_login clientAuth ''
issue_certificate "${runtime_root}/tls/host" app masi_app_login clientAuth ''

for directory in postgres pgbouncer dbctl standby restore host; do
  cp -- "${runtime_root}/tls/ca/ca.crt" "${runtime_root}/tls/${directory}/ca.crt"
  chmod 0444 -- "${runtime_root}/tls/${directory}/ca.crt"
done

bootstrap_password="$(<"${runtime_root}/secrets/bootstrap-password")"
replication_password="$(<"${runtime_root}/secrets/replication-password")"
monitoring_password="$(<"${runtime_root}/secrets/monitoring-password")"
app_password="$(<"${runtime_root}/secrets/app-password")"
printf '*:5432:*:masi_bootstrap:%s\n' "${bootstrap_password}" \
  >"${runtime_root}/secrets/bootstrap-passfile"
printf '*:5432:*:masi_monitoring_login:%s\n' "${monitoring_password}" \
  >"${runtime_root}/secrets/monitoring-passfile"
printf 'postgres:5432:*:masi_replication_login:%s\n' "${replication_password}" \
  >"${runtime_root}/secrets/replication-passfile"
printf 'pgbouncer:6432:*:masi_app_login:%s\n' "${app_password}" \
  >"${runtime_root}/secrets/app-pgbouncer-passfile"
chmod 0600 -- "${runtime_root}/secrets/bootstrap-passfile" \
  "${runtime_root}/secrets/monitoring-passfile" \
  "${runtime_root}/secrets/replication-passfile" \
  "${runtime_root}/secrets/app-pgbouncer-passfile"

python3 "${script_dir}/generate-scram-auth.py" \
  --secret-directory "${runtime_root}/secrets" \
  --verifier-directory "${runtime_root}/secrets" \
  --output "${runtime_root}/secrets/pgbouncer-users.txt"

migration_password="$(<"${runtime_root}/secrets/migration-password")"
host_tls="${runtime_root}/tls/host"
printf 'postgres://masi_migration_login:%s@postgres:5432/masi_state_module_test?sslmode=verify-full&sslrootcert=/run/masi-postgresql/tls/ca.crt&sslcert=/run/masi-postgresql/tls/client.crt&sslkey=/run/masi-postgresql/tls/client.key\n' \
  "${migration_password}" >"${runtime_root}/secrets/migration-dsn-container"
printf 'postgres://masi_migration_login:%s@127.0.0.1:55442/postgres?sslmode=verify-full&sslrootcert=%s/ca.crt&sslcert=%s/migration.crt&sslkey=%s/migration.key\n' \
  "${migration_password}" "${host_tls}" "${host_tls}" "${host_tls}" \
  >"${runtime_root}/secrets/migration-admin-dsn-host"
printf 'postgres://masi_migration_login:%s@127.0.0.1:55442/masi_state_module_test?sslmode=verify-full&sslrootcert=%s/ca.crt&sslcert=%s/migration.crt&sslkey=%s/migration.key\n' \
  "${migration_password}" "${host_tls}" "${host_tls}" "${host_tls}" \
  >"${runtime_root}/secrets/migration-dsn-host"
printf 'postgres://masi_migration_login:%s@127.0.0.1:55443/masi_state_module_test?sslmode=verify-full&sslrootcert=%s/ca.crt&sslcert=%s/migration.crt&sslkey=%s/migration.key\n' \
  "${migration_password}" "${host_tls}" "${host_tls}" "${host_tls}" \
  >"${runtime_root}/secrets/standby-migration-dsn-host"
printf 'postgres://masi_migration_login:%s@127.0.0.1:55444/masi_state_module_test?sslmode=verify-full&sslrootcert=%s/ca.crt&sslcert=%s/migration.crt&sslkey=%s/migration.key\n' \
  "${migration_password}" "${host_tls}" "${host_tls}" "${host_tls}" \
  >"${runtime_root}/secrets/restore-migration-dsn-host"
printf 'postgres://masi_migration_login:%s@127.0.0.1:55445/masi_state_module_test?sslmode=verify-full&sslrootcert=%s/ca.crt&sslcert=%s/migration.crt&sslkey=%s/migration.key\n' \
  "${migration_password}" "${host_tls}" "${host_tls}" "${host_tls}" \
  >"${runtime_root}/secrets/disk-fault-migration-dsn-host"
printf 'postgres://masi_app_login:%s@127.0.0.1:56442/masi_state_module_test?sslmode=verify-full&sslrootcert=%s/ca.crt&sslcert=%s/app.crt&sslkey=%s/app.key\n' \
  "${app_password}" "${host_tls}" "${host_tls}" "${host_tls}" \
  >"${runtime_root}/secrets/app-pgbouncer-dsn-host"
printf 'postgres://masi_migration_login:%s@pgbouncer:6432/pgbouncer?sslmode=verify-full&sslrootcert=/run/masi-postgresql/tls/ca.crt&sslcert=/run/masi-postgresql/tls/client.crt&sslkey=/run/masi-postgresql/tls/client.key\n' \
  "${migration_password}" >"${runtime_root}/secrets/pgbouncer-admin-dsn-container"
printf 'postgres://masi_app_login:%s@pgbouncer:6432/masi_state_module_test?sslmode=verify-full&sslrootcert=/run/masi-postgresql/tls/ca.crt&sslcert=/run/masi-postgresql/tls/app.crt&sslkey=/run/masi-postgresql/tls/app.key\n' \
  "${app_password}" >"${runtime_root}/secrets/app-pgbouncer-dsn-container"
printf 'postgres://masi_migration_login:%s@127.0.0.1:56442/pgbouncer?sslmode=verify-full&sslrootcert=%s/ca.crt&sslcert=%s/migration.crt&sslkey=%s/migration.key\n' \
  "${migration_password}" "${host_tls}" "${host_tls}" "${host_tls}" \
  >"${runtime_root}/secrets/pgbouncer-admin-dsn-host"
chmod 0600 -- "${runtime_root}/secrets/"*-dsn-*

# Runtime images intentionally use fixed non-root UIDs. Only the exact files
# each service needs are made readable by that service.
chown 70:70 -- "${runtime_root}/secrets/"{bootstrap,migration,app,replication,monitoring}-password \
  "${runtime_root}/secrets/"{migration,app,monitoring}-scram \
  "${runtime_root}/secrets/bootstrap-passfile" \
  "${runtime_root}/secrets/monitoring-passfile" \
  "${runtime_root}/secrets/replication-passfile" \
  "${runtime_root}/secrets/app-pgbouncer-passfile" \
  "${runtime_root}/secrets/migration-dsn-container" \
  "${runtime_root}/secrets/pgbouncer-admin-dsn-container" \
  "${runtime_root}/secrets/app-pgbouncer-dsn-container"
chown -R 70:70 -- "${runtime_root}/tls/postgres" "${runtime_root}/tls/dbctl" \
  "${runtime_root}/tls/standby" "${runtime_root}/tls/restore"
chown 10003:10003 -- "${runtime_root}/secrets/pgbouncer-users.txt"
chown -R 10003:10003 -- "${runtime_root}/tls/pgbouncer"

unset bootstrap_password migration_password app_password replication_password monitoring_password
printf '%s\n' "${runtime_root}"
