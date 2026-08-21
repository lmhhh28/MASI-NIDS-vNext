#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 3 ]]; then
  echo "usage: run-connection-exhaustion.sh RUNTIME_ROOT COMPOSE_PROJECT POSTGRES_IMAGE" >&2
  exit 64
fi

runtime_root="$1"
project="$2"
postgres_image="$3"
if [[ "${runtime_root}" != /* || ! -d "${runtime_root}" || -L "${runtime_root}" ]]; then
  echo "invalid runtime root" >&2
  exit 64
fi
for input in \
  "${runtime_root}/secrets/app-pgbouncer-passfile" \
  "${runtime_root}/tls/dbctl/ca.crt" \
  "${runtime_root}/tls/dbctl/app.crt" \
  "${runtime_root}/tls/dbctl/app.key"; do
  if [[ ! -f "${input}" || -L "${input}" ]]; then
    echo "invalid connection-exhaustion input: ${input}" >&2
    exit 64
  fi
done

docker run --rm --user 70:70 --network "${project}_state" \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --cpus 1 --memory 512m --pids-limit 64 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777 \
  --entrypoint /bin/sh \
  --mount "type=bind,source=${runtime_root}/secrets/app-pgbouncer-passfile,target=/run/masi-postgresql/passfile,readonly" \
  --mount "type=bind,source=${runtime_root}/tls/dbctl,target=/run/masi-postgresql/tls,readonly" \
  --env PGHOST=pgbouncer --env PGPORT=6432 \
  --env PGDATABASE=masi_state_module_test --env PGUSER=masi_app_login \
  --env PGPASSFILE=/run/masi-postgresql/passfile --env PGSSLMODE=verify-full \
  --env PGSSLROOTCERT=/run/masi-postgresql/tls/ca.crt \
  --env PGSSLCERT=/run/masi-postgresql/tls/app.crt \
  --env PGSSLKEY=/run/masi-postgresql/tls/app.key \
  --env PGCONNECT_TIMEOUT=5 --env PGAPPNAME=masi-db-connection-exhaustion \
  --env PSQLRC=/dev/null \
  "${postgres_image}" -eu -c '
    pids=""
    cleanup() {
      for pid in ${pids}; do kill -TERM "${pid}" 2>/dev/null || true; done
      for pid in ${pids}; do wait "${pid}" 2>/dev/null || true; done
    }
    trap cleanup EXIT INT TERM

    index=1
    while test "${index}" -le 28; do
      psql --no-psqlrc --tuples-only --no-align --command "SELECT pg_sleep(20)" \
        >"/tmp/holder-${index}.out" 2>"/tmp/holder-${index}.err" &
      pids="${pids} $!"
      index=$((index + 1))
    done
    sleep 4
    live=0
    for pid in ${pids}; do
      if kill -0 "${pid}" 2>/dev/null; then live=$((live + 1)); fi
    done
    if test "${live}" -ne 28; then
      echo "not all 28 pool holders remained active: ${live}" >&2
      exit 1
    fi

    started="$(date +%s)"
    set +e
    psql --no-psqlrc --tuples-only --no-align --command "SELECT 1" \
      >/tmp/exhausted.out 2>/tmp/exhausted.err
    status=$?
    set -e
    elapsed=$(($(date +%s) - started))
    if test "${status}" -eq 0; then
      echo "query unexpectedly bypassed bounded PgBouncer exhaustion" >&2
      exit 1
    fi
    if ! grep -Fq "query_wait_timeout" /tmp/exhausted.err; then
      sed -n "1,20p" /tmp/exhausted.err >&2
      echo "expected query_wait_timeout was not observed" >&2
      exit 1
    fi
    if test "${elapsed}" -lt 8 || test "${elapsed}" -gt 16; then
      echo "query_wait_timeout elapsed outside 8..16 seconds: ${elapsed}" >&2
      exit 1
    fi
    printf "connection_exhaustion_bounded holders=28 elapsed_seconds=%s\n" "${elapsed}"

    cleanup
    pids=""
    trap - EXIT INT TERM
    attempt=1
    while test "${attempt}" -le 10; do
      if test "$(psql --no-psqlrc --tuples-only --no-align --command "SELECT 1" 2>/dev/null)" = 1; then
        printf "connection_exhaustion_recovered attempt=%s\n" "${attempt}"
        exit 0
      fi
      sleep 1
      attempt=$((attempt + 1))
    done
    echo "PgBouncer application boundary did not recover" >&2
    exit 1
  '
