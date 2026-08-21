#!/usr/bin/env bash
set -euo pipefail
umask 077

read_secret() {
  local path="$1"
  if [[ ! -f "${path}" || -L "${path}" ]]; then
    echo "invalid secret file: ${path}" >&2
    exit 1
  fi
  local value
  IFS= read -r value <"${path}"
  if [[ -z "${value}" || "${value}" == *$'\n'* ]]; then
    echo "empty or multiline secret: ${path}" >&2
    exit 1
  fi
  printf '%s' "${value}"
}

migration_scram="$(read_secret /run/masi-postgresql/secrets/migration-scram)"
app_scram="$(read_secret /run/masi-postgresql/secrets/app-scram)"
monitoring_scram="$(read_secret /run/masi-postgresql/secrets/monitoring-scram)"
replication_password="$(read_secret /run/masi-postgresql/secrets/replication-password)"

psql --set=ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname postgres \
  --set=migration_scram="${migration_scram}" \
  --set=app_scram="${app_scram}" \
  --set=monitoring_scram="${monitoring_scram}" \
  --set=replication_password="${replication_password}" <<'SQL'
CREATE ROLE masi_migration_login LOGIN NOSUPERUSER CREATEDB CREATEROLE NOINHERIT;
CREATE ROLE masi_app_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
CREATE ROLE masi_replication_login LOGIN REPLICATION NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE masi_control_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE masi_control_readonly NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE masi_control_maintenance NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE masi_analysis_private NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE masi_backup NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE REPLICATION;
CREATE ROLE masi_monitoring NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE masi_monitoring_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
GRANT pg_monitor TO masi_monitoring;
GRANT pg_monitor TO masi_migration_login WITH ADMIN OPTION;
GRANT masi_analysis_private TO masi_migration_login
  WITH ADMIN OPTION, INHERIT TRUE, SET TRUE;
GRANT masi_monitoring TO masi_monitoring_login;
SELECT format('ALTER ROLE masi_migration_login PASSWORD %L', :'migration_scram') \gexec
SELECT format('ALTER ROLE masi_app_login PASSWORD %L', :'app_scram') \gexec
SELECT format('ALTER ROLE masi_monitoring_login PASSWORD %L', :'monitoring_scram') \gexec
SELECT format('ALTER ROLE masi_replication_login PASSWORD %L', :'replication_password') \gexec
ALTER DATABASE masi_state_module_test OWNER TO masi_migration_login;
REVOKE CONNECT ON DATABASE masi_state_module_test FROM PUBLIC;
GRANT CONNECT ON DATABASE masi_state_module_test
  TO masi_migration_login, masi_app_login, masi_monitoring_login;
SQL

unset migration_scram app_scram monitoring_scram replication_password
