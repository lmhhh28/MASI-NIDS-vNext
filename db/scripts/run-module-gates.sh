#!/usr/bin/env bash
# shellcheck disable=SC2016  # nested bash/jq programs expand in their own process
set -euo pipefail
umask 077

formal_mode=0
if [[ "${MASI_DB_FORMAL_SOAK:-0}" == "1" ]]; then
  formal_mode=1
elif [[ "${MASI_DB_REHEARSAL:-0}" != "1" ]]; then
  echo "set MASI_DB_FORMAL_SOAK=1 for Module Complete or MASI_DB_REHEARSAL=1 for explicitly non-qualified preflight" >&2
  exit 64
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
db_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${db_root}/.." && pwd)"
deploy_root="${repo_root}/deploy/postgresql-state"
manifest="${db_root}/requirements-traceability.json"
evidence_base_input="${MASI_DB_EVIDENCE_DIR:-${db_root}/evidence/module-gates}"
run_id="${MASI_DB_GATE_RUN_ID:-db-formal-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
if [[ ! "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ ]]; then
  echo "invalid gate run id" >&2
  exit 64
fi
evidence_base="$(realpath -m -s -- "${evidence_base_input}")"
if [[ "${evidence_base}" == "/" || "${evidence_base}" == "${repo_root}" \
  || "${evidence_base}" == "${db_root}" || -L "${evidence_base_input}" \
  || "$(realpath -m -- "${evidence_base_input}")" != "${evidence_base}" ]]; then
  echo "unsafe evidence base" >&2
  exit 64
fi
mkdir -p -- "${evidence_base}/runs"
[[ ! -L "${evidence_base}" && ! -L "${evidence_base}/runs" ]]
run_dir="${evidence_base}/runs/${run_id}"
mkdir -- "${run_dir}"
mkdir -- "${run_dir}/capacity" "${run_dir}/formal-soak" "${run_dir}/oci" \
  "${run_dir}/recovery"

runtime_root="$(mktemp -d "/tmp/masi-db-module.${run_id//:/_}.XXXXXXXX")"
chmod 0700 -- "${runtime_root}"
toolchain_root="$(mktemp -d "/tmp/masi-db-go.${run_id//:/_}.XXXXXXXX")"
chmod 0700 -- "${toolchain_root}"
project="masidb$$_${RANDOM}"
project="${project,,}"
db_image="masi-dbctl:${project}"
pgbouncer_image="masi-pgbouncer:${project}"
postgres_image="masi-postgres:${project}"
compose=(docker compose --project-name "${project}" --file "${deploy_root}/compose.module-gates.yaml")
export MASI_DB_RUNTIME_ROOT="${runtime_root}"
export MASI_DB_PGBOUNCER_IMAGE="${pgbouncer_image}"
export MASI_DB_POSTGRES_IMAGE="${postgres_image}"
export MASI_GO_TOOLCHAIN_CACHE="${repo_root}/out/toolchains/go1.26.6.linux-amd64.tar.gz"
cleanup_required=true

cleanup() {
  local status=$?
  if [[ "${cleanup_required}" == true ]]; then
    "${compose[@]}" --profile standby --profile restore --profile fault down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
  case "${runtime_root}" in
    /tmp/masi-db-module.*)
      if [[ -d "${runtime_root}" && ! -L "${runtime_root}" ]]; then
        chmod -R u+w -- "${runtime_root}" 2>/dev/null || true
        find "${runtime_root}" -depth -mindepth 1 -delete 2>/dev/null || true
        rmdir -- "${runtime_root}" 2>/dev/null || true
      fi
      ;;
  esac
  case "${toolchain_root}" in
    /tmp/masi-db-go.*)
      if [[ -d "${toolchain_root}" && ! -L "${toolchain_root}" ]]; then
        chmod -R u+w -- "${toolchain_root}" 2>/dev/null || true
        find "${toolchain_root}" -depth -mindepth 1 -delete 2>/dev/null || true
        rmdir -- "${toolchain_root}" 2>/dev/null || true
      fi
      ;;
  esac
  exit "${status}"
}
trap cleanup EXIT INT TERM

for command in curl docker git go gofmt govulncheck jq openssl python3 sha256sum shellcheck staticcheck tar timeout; do
  command -v "${command}" >/dev/null 2>&1 || { echo "missing command: ${command}" >&2; exit 1; }
done

source_revision="$(git -C "${repo_root}" rev-parse HEAD)"
source_tree_digest() {
  {
    git -C "${repo_root}" rev-parse HEAD
    git -C "${repo_root}" diff --binary HEAD --
    while IFS= read -r -d '' path; do
      printf '%s\0' "${path}"
      sha256sum "${repo_root}/${path}"
    done < <(git -C "${repo_root}" ls-files --others --exclude-standard -z | sort -z)
  } | sha256sum | awk '{print "sha256:"$1}'
}
working_status_digest() {
  git -C "${repo_root}" status --porcelain=v1 -z | sha256sum | awk '{print "sha256:"$1}'
}
source_digest_start="$(source_tree_digest)"
status_digest_start="$(working_status_digest)"
working_tree_dirty=false
if [[ -n "$(git -C "${repo_root}" status --porcelain=v1)" ]]; then working_tree_dirty=true; fi

write_sidecar() {
  local id="$1" started_at="$2" finished_at="$3" duration_ms="$4" status="$5" log="$6"
  shift 6
  local result qualification stable_reason log_digest log_bytes sidecar temporary
  result=FAIL; qualification=NOT_QUALIFIED; stable_reason=COMMAND_EXITED_FAILURE
  if [[ "${status}" -eq 0 ]]; then result=PASS; qualification=QUALIFIED; stable_reason=""; fi
  log_digest="sha256:$(sha256sum "${log}" | awk '{print $1}')"
  log_bytes="$(stat -c '%s' "${log}")"
  sidecar="${run_dir}/${id}.command.json"
  temporary="${run_dir}/.${id}.command.$$.json"
  jq -n --arg run_id "${run_id}" --arg command_id "${id}" \
    --arg started_at "${started_at}" --arg finished_at "${finished_at}" \
    --argjson duration_ms "${duration_ms}" --argjson exit_code "${status}" \
    --arg result "${result}" --arg qualification "${qualification}" \
    --arg stable_reason "${stable_reason}" --arg source_tree_digest "${source_digest_start}" \
    --arg working_tree_status_digest "${status_digest_start}" \
    --arg log_path "${id}.log" --arg log_digest "${log_digest}" \
    --argjson log_bytes "${log_bytes}" --args '$ARGS.positional as $argv | {
      schema_version:"edge-command-execution/v1",run_id:$run_id,command_id:$command_id,
      started_at:$started_at,finished_at:$finished_at,duration_ms:$duration_ms,
      working_directory:"db",argv:$argv,exit_code:$exit_code,result:$result,
      qualification:$qualification,source_tree_digest:$source_tree_digest,
      working_tree_status_digest:$working_tree_status_digest,
      stable_reason:(if $stable_reason=="" then null else $stable_reason end),
      log:{path:$log_path,sha256:$log_digest,bytes:$log_bytes,media_type:"text/plain"}
    }' -- "$@" >"${temporary}"
  python3 "${script_dir}/validate-json.py" \
    --schema "${repo_root}/contracts/evidence/command/v1/schema.json" \
    --document "${temporary}" >/dev/null
  mv -T -- "${temporary}" "${sidecar}"
}

run_command() {
  local id="$1"; shift
  local -a command=("$@")
  local started_at finished_at started_ns finished_ns status pipeline_status log temporary
  log="${run_dir}/${id}.log"; temporary="${run_dir}/.${id}.log.$$.tmp"
  [[ "${id}" =~ ^[a-z][a-z0-9-]{0,63}$ && ! -e "${log}" && ! -L "${log}" ]]
  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; started_ns="$(date +%s%N)"
  set +e
  timeout --signal=TERM --kill-after=30s "${MASI_DB_COMMAND_TIMEOUT_SECONDS:-7500}s" \
    "${command[@]}" 2>&1 | tee "${temporary}"
  pipeline_status=("${PIPESTATUS[@]}")
  status="${pipeline_status[0]}"
  if [[ "${pipeline_status[1]}" -ne 0 && "${status}" -eq 0 ]]; then status="${pipeline_status[1]}"; fi
  set -e
  finished_ns="$(date +%s%N)"; finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mv -T -- "${temporary}" "${log}"
  write_sidecar "${id}" "${started_at}" "${finished_at}" "$(((finished_ns-started_ns)/1000000))" \
    "${status}" "${log}" "${command[@]}"
  return "${status}"
}

run_json() {
  local id="$1" output="$2"; shift 2
  local -a command=("$@")
  local started_at finished_at started_ns finished_ns status log temporary_log temporary_output
  log="${run_dir}/${id}.log"; temporary_log="${run_dir}/.${id}.log.$$.tmp"
  temporary_output="${output}.$$.tmp"
  [[ "${output}" == /* && ! -e "${output}" && ! -L "${output}" && ! -e "${log}" ]]
  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; started_ns="$(date +%s%N)"
  set +e
  timeout --signal=TERM --kill-after=30s "${MASI_DB_COMMAND_TIMEOUT_SECONDS:-7500}s" \
    "${command[@]}" >"${temporary_output}" 2>"${temporary_log}"
  status=$?
  set -e
  finished_ns="$(date +%s%N)"; finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [[ -s "${temporary_log}" ]]; then awk '{print}' "${temporary_log}"; fi
  mv -T -- "${temporary_log}" "${log}"
  if [[ "${status}" -eq 0 ]]; then
    jq empty "${temporary_output}"
    mv -T -- "${temporary_output}" "${output}"
  else
    unlink -- "${temporary_output}" 2>/dev/null || true
  fi
  write_sidecar "${id}" "${started_at}" "${finished_at}" "$(((finished_ns-started_ns)/1000000))" \
    "${status}" "${log}" "${command[@]}"
  return "${status}"
}

run_command go-toolchain bash "${deploy_root}/scripts/prepare-go-toolchain.sh" "${toolchain_root}"
export GOROOT="${toolchain_root}"
export PATH="${toolchain_root}/bin:${PATH}"
run_command go-version go version
run_command format bash -lc 'cd "$1" && test -z "$(gofmt -l ./cmd ./internal ./tests)"' -- "${db_root}"
run_command vet bash -lc 'cd "$1" && go vet ./...' -- "${db_root}"
run_command staticcheck bash -lc 'cd "$1" && staticcheck ./...' -- "${db_root}"
run_command vulnerability-scan bash -lc 'cd "$1" && govulncheck ./...' -- "${db_root}"
run_command unit bash -lc 'cd "$1" && go test -count=1 ./...' -- "${db_root}"
run_command race bash -lc 'cd "$1" && go test -race -count=1 ./...' -- "${db_root}"
run_command coverage bash -lc 'cd "$1" && go test -covermode=atomic -coverprofile="$2" ./...' \
  -- "${db_root}" "${run_dir}/coverage.out"
run_command benchmark bash -lc 'cd "$1" && go test -run "^$" -bench BenchmarkLoadChain -benchmem ./internal/migrate' \
  -- "${db_root}"
run_command python-tools python3 "${db_root}/scripts/test_evidence_tools.py"
run_command python-lint ruff check "${db_root}/scripts"
run_command shell-lint shellcheck "${db_root}/scripts/"*.sh "${deploy_root}/postgres/init/001-create-login-roles.sh"
run_command findings python3 "${script_dir}/validate-json.py" \
  --schema "${repo_root}/contracts/evidence/module-findings/v1/schema.json" \
  --document "${db_root}/module-findings.json"
run_command traceability-manifest python3 "${script_dir}/validate-traceability-manifest.py" \
  --repo "${repo_root}" --manifest "${manifest}"
run_command source-sentinels python3 "${script_dir}/check-source-sentinels.py" --repo "${repo_root}"
run_command runtime-prepare bash "${deploy_root}/scripts/prepare-runtime.sh" "${runtime_root}"
mkdir -m 0700 -- "${runtime_root}/bin"
run_command release-build bash -lc 'cd "$1" && CGO_ENABLED=0 go build -trimpath -o "$2" ./cmd/masi-dbctl && CGO_ENABLED=0 go build -trimpath -o "$3" ./cmd/masi-dbload' \
  -- "${db_root}" "${runtime_root}/bin/masi-dbctl" "${runtime_root}/bin/masi-dbload"
run_command image-build-db docker build --pull=false --tag "${db_image}" --file "${db_root}/Dockerfile" "${repo_root}"
run_command image-build-pgbouncer docker build --pull=false --tag "${pgbouncer_image}" \
  --file "${deploy_root}/pgbouncer/Dockerfile" "${deploy_root}/pgbouncer"
run_command image-build-postgres docker build --pull=false --tag "${postgres_image}" \
  --file "${deploy_root}/postgres/Dockerfile" "${deploy_root}/postgres"
run_json migration-chain "${run_dir}/migration-chain.json" docker run --rm --network none \
  --read-only --cap-drop ALL --security-opt no-new-privileges "${db_image}" chain
run_command runtime-start "${compose[@]}" up --detach --wait postgres

dbctl_container=(docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  --network "${project}_state" \
  --mount "type=bind,source=${runtime_root}/secrets/migration-dsn-container,target=/run/masi-postgresql/dsn,readonly" \
  --mount "type=bind,source=${runtime_root}/tls/dbctl,target=/run/masi-postgresql/tls,readonly" \
  "${db_image}")
run_json fresh-migration "${run_dir}/fresh-migration.json" "${dbctl_container[@]}" migrate \
  --dsn-file /run/masi-postgresql/dsn --confirm-database masi_state_module_test \
  --require-test-database --require-tls --source-revision "${source_revision}"
run_json repeat-migration "${run_dir}/repeat-migration.json" "${dbctl_container[@]}" migrate \
  --dsn-file /run/masi-postgresql/dsn --confirm-database masi_state_module_test \
  --require-test-database --require-tls --source-revision "${source_revision}"
run_json catalog-readback "${run_dir}/catalog-readback.json" "${dbctl_container[@]}" inspect \
  --dsn-file /run/masi-postgresql/dsn --confirm-database masi_state_module_test --require-tls
run_command blackbox env MASI_DB_BLACKBOX_REQUIRED=1 MASI_DB_TEST_REQUIRE_TLS=1 \
  MASI_DB_TEST_ADMIN_DSN_FILE="${runtime_root}/secrets/migration-admin-dsn-host" \
  go -C "${db_root}" test -count=1 -v ./tests/blackbox

run_command disk-fault-start "${compose[@]}" --profile fault up --detach --wait postgres-disk-fault
run_json disk-fault-migration "${run_dir}/disk-fault-migration.json" \
  "${runtime_root}/bin/masi-dbctl" migrate \
  --dsn-file "${runtime_root}/secrets/disk-fault-migration-dsn-host" \
  --directory "${db_root}/migrations" \
  --confirm-database masi_state_module_test --require-test-database --require-tls \
  --source-revision "${source_revision}"
disk_fault_container="${project}-postgres-disk-fault-1"
run_command disk-exhaustion bash -c 'docker exec --interactive --env PGPASSFILE=/run/masi-postgresql/secrets/bootstrap-passfile "$1" psql --host /var/run/postgresql --username masi_bootstrap --dbname masi_state_module_test --set ON_ERROR_STOP=1 <"$2"' \
  -- "${disk_fault_container}" "${deploy_root}/postgres/probe-disk-exhaustion.sql"
run_command disk-fault-stop "${compose[@]}" --profile fault stop postgres-disk-fault

postgres_container="${project}-postgres-1"
run_command recovery-seed bash -c 'docker exec --interactive --env PGPASSFILE=/run/masi-postgresql/secrets/bootstrap-passfile "$1" psql --host /var/run/postgresql --username masi_bootstrap --dbname masi_state_module_test --set ON_ERROR_STOP=1 <"$2"' \
  -- "${postgres_container}" "${deploy_root}/postgres/seed-recovery-fixtures.sql"
run_command physical-backup bash "${script_dir}/run-compose-oneshot.sh" "${project}" \
  "${deploy_root}/compose.module-gates.yaml" restore pitr-basebackup
backup_manifest_file="${run_dir}/recovery/backup-manifest.sha256"
run_command backup-manifest bash -c 'docker run --rm --user 70:70 --network none --read-only --cap-drop ALL --security-opt no-new-privileges --entrypoint sha256sum --mount "type=volume,source=${1}_pitr-basebackup,target=/base,readonly" "$2" /base/18/docker/backup_manifest >"$3"' \
  -- "${project}" "${postgres_image}" "${backup_manifest_file}"
backup_manifest_digest="sha256:$(awk '{print $1}' "${backup_manifest_file}")"
run_command pitr-target bash -c 'docker exec --interactive --env PGPASSFILE=/run/masi-postgresql/secrets/bootstrap-passfile "$1" psql --host /var/run/postgresql --username masi_bootstrap --dbname masi_state_module_test --set ON_ERROR_STOP=1 <"$2"' \
  -- "${postgres_container}" "${deploy_root}/postgres/create-pitr-target.sql"
run_command archive-check bash -c 'for attempt in {1..30}; do value="$(docker exec --env PGPASSFILE=/run/masi-postgresql/secrets/bootstrap-passfile "$1" psql --host /var/run/postgresql --username masi_bootstrap --dbname masi_state_module_test --tuples-only --no-align --command "$2")"; printf "attempt=%s archive=%s\n" "$attempt" "$value"; if test "$value" = "0|true"; then exit 0; fi; sleep 1; done; exit 1' \
  -- "${postgres_container}" "SELECT failed_count||'|'||(last_archived_time>clock_timestamp()-interval '60 seconds') FROM pg_stat_archiver"
archive_failures=0
pitr_started_ns="$(date +%s%N)"
run_command pitr-restore-start "${compose[@]}" --profile restore up --detach --wait restore
run_json pitr-before "${run_dir}/recovery/pitr-before.json" "${runtime_root}/bin/masi-dbctl" inspect-recovery \
  --dsn-file "${runtime_root}/secrets/restore-migration-dsn-host" \
  --confirm-database masi_state_module_test --require-tls \
  --included-marker module-pitr-keep --excluded-marker module-pitr-drift
run_json pitr-rotation "${run_dir}/recovery/pitr-rotation.json" "${runtime_root}/bin/masi-dbctl" rotate-incarnations \
  --dsn-file "${runtime_root}/secrets/restore-migration-dsn-host" \
  --confirm-database masi_state_module_test --require-tls \
  --new-target-incarnation "target-after-${run_id}" --new-model-incarnation "model-after-${run_id}" \
  --source pitr --backup-manifest-digest "${backup_manifest_digest}" \
  --schema-chain-digest "$(jq -r .chain_digest "${run_dir}/fresh-migration.json")" \
  --source-timeline 1 --restored-timeline "$(jq -r .timeline_id "${run_dir}/recovery/pitr-before.json")" \
  --recovery-id "recovery-${run_id}" --actor-ref module-gate --trace-id "pitr-${run_id}"
run_json pitr-after "${run_dir}/recovery/pitr-after.json" "${runtime_root}/bin/masi-dbctl" inspect-recovery \
  --dsn-file "${runtime_root}/secrets/restore-migration-dsn-host" \
  --confirm-database masi_state_module_test --require-tls \
  --included-marker module-pitr-keep --excluded-marker module-pitr-drift \
  --expected-recovery-id "recovery-${run_id}"
pitr_finished_ns="$(date +%s%N)"
pitr_rto_ms="$(((pitr_finished_ns-pitr_started_ns)/1000000))"
run_command pitr jq -e --arg target "target-after-${run_id}" --arg model "model-after-${run_id}" '
  .included_marker_present and .excluded_marker_absent and
  (.target_writer_enabled|not) and (.model_writer_enabled|not) and
  .recovery_event_present and .recovery_event_isolated and (.recovery_event_writer_opened|not) and
  .effect_intent_count==0 and .plugin_statistic_run_count==0 and
  .target_control_incarnation==$target and .model_control_incarnation==$model
' "${run_dir}/recovery/pitr-after.json"
run_command pitr-restore-stop "${compose[@]}" --profile restore stop restore

run_command role-seal-apply bash -c 'docker exec --interactive --env PGPASSFILE=/run/masi-postgresql/secrets/bootstrap-passfile "$1" psql --host /var/run/postgresql --username masi_bootstrap --dbname masi_state_module_test --set ON_ERROR_STOP=1 <"$2"' \
  -- "${postgres_container}" "${deploy_root}/postgres/seal-runtime-roles.sql"
run_json runtime-roles "${run_dir}/runtime-roles.json" "${runtime_root}/bin/masi-dbctl" inspect-roles \
  --dsn-file "${runtime_root}/secrets/migration-dsn-host" --confirm-database masi_state_module_test --require-tls
run_command role-seal jq -e '.app_control_member and (.app_backup_member|not) and .monitoring_member and (.migration_private_member|not) and (.migration_monitor_member|not) and (.public_connect|not) and (.public_temporary|not)' \
  "${run_dir}/runtime-roles.json"
run_command pool-start "${compose[@]}" up --detach --wait pgbouncer

pool_dbctl=(docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  --network "${project}_state" \
  --mount "type=bind,source=${runtime_root}/tls/dbctl,target=/run/masi-postgresql/tls,readonly")
run_json pool-readback "${run_dir}/pool-readback.json" "${pool_dbctl[@]}" \
  --mount "type=bind,source=${runtime_root}/secrets/pgbouncer-admin-dsn-container,target=/run/masi-postgresql/dsn,readonly" \
  "${db_image}" inspect-pool --dsn-file /run/masi-postgresql/dsn --require-tls
run_json application-boundary "${run_dir}/application-boundary.json" "${pool_dbctl[@]}" \
  --mount "type=bind,source=${runtime_root}/secrets/app-pgbouncer-dsn-container,target=/run/masi-postgresql/dsn,readonly" \
  "${db_image}" inspect-application --dsn-file /run/masi-postgresql/dsn \
  --confirm-database masi_state_module_test --require-tls
run_command connection-exhaustion bash "${script_dir}/run-connection-exhaustion.sh" \
  "${runtime_root}" "${project}" "${postgres_image}"
run_json capacity-raw "${run_dir}/capacity/capacity-raw.json" "${pool_dbctl[@]}" \
  --entrypoint /usr/local/bin/masi-dbload \
  --mount "type=bind,source=${runtime_root}/secrets/app-pgbouncer-dsn-container,target=/run/masi-postgresql/dsn,readonly" \
  "${db_image}" --dsn-file /run/masi-postgresql/dsn --confirm-database masi_state_module_test \
  --mode capacity --require-tls
run_command capacity bash -c 'python3 "$1" --raw "$2" --run-id "$3" --output "$4" && python3 "$5" --schema "$6" --document "$4"' \
  -- "${script_dir}/build-capacity-evidence.py" "${run_dir}/capacity/capacity-raw.json" "${run_id}" \
  "${run_dir}/capacity/capacity-evidence.json" "${script_dir}/validate-json.py" \
  "${repo_root}/contracts/evidence/postgresql-state-capacity/v1/schema.json"

run_command oci-smoke bash "${script_dir}/run-oci-smoke.sh" "${db_image}" "${pgbouncer_image}" "${postgres_image}" \
  "${run_dir}/oci/oci-evidence.json" "${run_id}"
run_command supply-chain bash "${script_dir}/scan-supply-chain.sh" "${run_dir}/supply" \
  "${db_image}" "${pgbouncer_image}" "${postgres_image}" "${run_id}"

run_command formal-soak-run bash "${script_dir}/run-soak.sh" "${runtime_root}" "${project}" \
  "${db_image}" "${run_dir}/formal-soak/raw-workload.json" \
  "${run_dir}/formal-soak/resource-samples.json" "${run_dir}/formal-soak/progress.log" "${formal_mode}"
if [[ "${formal_mode}" -eq 1 ]]; then
  run_command formal-soak bash -c 'python3 "$1" --raw "$2" --resources "$3" --profile "$4" --run-id "$5" --output "$6" && python3 "$7" --schema "$8" --document "$6"' \
    -- "${script_dir}/build-soak-evidence.py" "${run_dir}/formal-soak/raw-workload.json" \
    "${run_dir}/formal-soak/resource-samples.json" \
    "${repo_root}/contracts/profiles/v1/qualification-soak-3600s.json" "${run_id}" \
    "${run_dir}/formal-soak/formal-soak-evidence.json" "${script_dir}/validate-json.py" \
    "${repo_root}/contracts/evidence/soak/v1/schema.json"
else
  run_command formal-soak jq -e '.result=="PASS" and (.soak.formal|not) and .soak.errors==0' \
    "${run_dir}/formal-soak/raw-workload.json"
fi

run_command standby-start "${compose[@]}" --profile standby up --detach --wait standby
run_json replica-before "${run_dir}/recovery/replica-before.json" "${runtime_root}/bin/masi-dbctl" inspect-replica \
  --dsn-file "${runtime_root}/secrets/standby-migration-dsn-host" \
  --confirm-database masi_state_module_test --required-marker module-failover-marker \
  --require-tls --expect-recovery=true
standby_container="${project}-standby-1"
run_command replication-streaming bash -c 'for attempt in {1..30}; do value="$(docker exec --env PGPASSFILE=/run/masi-postgresql/secrets/monitoring-passfile "$1" psql --host /var/run/postgresql --username masi_monitoring_login --dbname masi_state_module_test --tuples-only --no-align --command "$2")"; printf "attempt=%s streaming_replicas=%s\n" "$attempt" "$value"; if test "$value" = 1; then exit 0; fi; sleep 1; done; exit 1' \
  -- "${postgres_container}" "SELECT count(*) FROM pg_stat_replication WHERE state='streaming'"
run_command primary-crash docker kill --signal KILL "${postgres_container}"
promotion_started_ns="$(date +%s%N)"
run_command standby-promote docker exec --user 70:70 "${standby_container}" pg_ctl \
  --pgdata /var/lib/postgresql/18/docker promote --wait --timeout 30
run_json replica-after "${run_dir}/recovery/replica-after.json" "${runtime_root}/bin/masi-dbctl" inspect-replica \
  --dsn-file "${runtime_root}/secrets/standby-migration-dsn-host" \
  --confirm-database masi_state_module_test --required-marker module-failover-marker \
  --require-tls --expect-recovery=false
promotion_finished_ns="$(date +%s%N)"
promotion_rto_ms="$(((promotion_finished_ns-promotion_started_ns)/1000000))"
run_command streaming-oracle jq -e --slurpfile before "${run_dir}/recovery/replica-before.json" '
  (.in_recovery|not) and .marker_present and .schema_version=="21" and
  (.target_writer|not) and (.model_writer|not) and .timeline_id>$before[0].timeline_id
' "${run_dir}/recovery/replica-after.json"
run_command streaming-failover bash -c 'python3 "$1" --run-id "$2" --backup-manifest-digest "$3" --archive-failures "$4" --pitr-before "$5" --rotation "$6" --pitr-after "$7" --pitr-rto-ms "$8" --replica-before "$9" --replica-after "${10}" --promotion-rto-ms "${11}" --output "${12}" && python3 "${13}" --schema "${14}" --document "${12}"' \
  -- "${script_dir}/build-recovery-evidence.py" "${run_id}" "${backup_manifest_digest}" \
  "${archive_failures}" "${run_dir}/recovery/pitr-before.json" \
  "${run_dir}/recovery/pitr-rotation.json" "${run_dir}/recovery/pitr-after.json" \
  "${pitr_rto_ms}" "${run_dir}/recovery/replica-before.json" \
  "${run_dir}/recovery/replica-after.json" "${promotion_rto_ms}" \
  "${run_dir}/recovery/recovery-evidence.json" "${script_dir}/validate-json.py" \
  "${repo_root}/contracts/evidence/postgresql-state-recovery/v1/schema.json"

run_command cleanup-runtime "${compose[@]}" --profile standby --profile restore --profile fault down --volumes --remove-orphans
cleanup_required=false
run_command cleanup-verify bash -c 'test -z "$(docker ps -aq --filter label=com.docker.compose.project="$1")" && test -z "$(docker volume ls -q --filter label=com.docker.compose.project="$1")"' \
  -- "${project}"
source_digest_end="$(source_tree_digest)"
status_digest_end="$(working_status_digest)"
run_command source-integrity test "${source_digest_end}" = "${source_digest_start}"
run_command status-integrity test "${status_digest_end}" = "${status_digest_start}"

if [[ "${formal_mode}" -ne 1 ]]; then
  echo "PostgreSQL State rehearsal completed; no Module Complete summary was emitted: ${run_dir}"
  exit 0
fi

run_command traceability-build python3 "${script_dir}/build-traceability.py" \
  --manifest "${manifest}" --run-directory "${run_dir}" --run-id "${run_id}" \
  --output "${run_dir}/traceability.json"
run_command traceability-validate python3 "${script_dir}/validate-json.py" \
  --schema "${repo_root}/contracts/evidence/traceability/v1/schema.json" \
  --document "${run_dir}/traceability.json"

db_image_id="$(docker image inspect --format '{{.Id}}' "${db_image}")"
pgbouncer_image_id="$(docker image inspect --format '{{.Id}}' "${pgbouncer_image}")"
postgres_image_id="$(docker image inspect --format '{{.Id}}' "${postgres_image}")"
python3 "${script_dir}/build-module-summary.py" --run-id "${run_id}" \
  --run-directory "${run_dir}" --manifest "${manifest}" \
  --source-revision "${source_revision}" --source-tree-digest "${source_digest_start}" \
  --working-tree-status-digest "${status_digest_start}" --working-tree-dirty "${working_tree_dirty}" \
  --db-image-id "${db_image_id}" --pgbouncer-image-id "${pgbouncer_image_id}" \
  --postgres-image-id "${postgres_image_id}" \
  --output "${run_dir}/gate-summary.json"
python3 "${script_dir}/validate-json.py" \
  --schema "${repo_root}/contracts/evidence/v1/postgresql-state-module-schema.json" \
  --document "${run_dir}/gate-summary.json" --kind module

cp -- "${run_dir}/gate-summary.json" "${evidence_base}/.gate-summary.${run_id}.json"
mv -T -- "${evidence_base}/.gate-summary.${run_id}.json" "${evidence_base}/gate-summary.json"
jq -n --arg run_id "${run_id}" --arg evidence "runs/${run_id}/gate-summary.json" \
  --arg digest "sha256:$(sha256sum "${run_dir}/gate-summary.json" | awk '{print $1}')" \
  '{schema_version:"postgresql-state-module-latest/v1",run_id:$run_id,evidence:$evidence,digest:$digest}' \
  >"${evidence_base}/.latest.${run_id}.json"
mv -T -- "${evidence_base}/.latest.${run_id}.json" "${evidence_base}/latest.json"

echo "PostgreSQL State operational Module Complete evidence: ${run_dir}/gate-summary.json"
