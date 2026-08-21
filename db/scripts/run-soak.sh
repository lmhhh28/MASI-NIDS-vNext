#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "$#" -ne 7 ]]; then
  echo "usage: run-soak.sh RUNTIME_ROOT COMPOSE_PROJECT IMAGE RAW_JSON RESOURCE_JSON PROGRESS_LOG FORMAL_0_OR_1" >&2
  exit 64
fi
runtime_root="$1"
project="$2"
image="$3"
raw="$4"
resources="$5"
progress="$6"
formal="$7"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

for output in "${raw}" "${resources}" "${progress}"; do
  if [[ "${output}" != /* || -e "${output}" || -L "${output}" ]]; then
    echo "soak output must be a fresh absolute path: ${output}" >&2
    exit 64
  fi
done
if [[ "${formal}" == "1" ]]; then
  duration=3665
  interval=10
  warmup=60
  phase_duration=900
  load_args=(--mode soak --formal --sample-every 10s)
elif [[ "${formal}" == "0" ]]; then
  duration=25
  interval=1
  warmup=0
  phase_duration=5
  load_args=(--mode soak --phase-duration 5s --warmup 0s --sample-every 1s)
else
  echo "formal flag must be 0 or 1" >&2
  exit 64
fi

postgres_container="${project}-postgres-1"
pgbouncer_container="${project}-pgbouncer-1"
sampler_pid=""
cleanup_sampler() {
  if [[ -n "${sampler_pid}" ]] && kill -0 "${sampler_pid}" 2>/dev/null; then
    kill -TERM "${sampler_pid}" 2>/dev/null || true
    wait "${sampler_pid}" 2>/dev/null || true
  fi
}
trap cleanup_sampler EXIT

python3 "${script_dir}/sample-resources.py" \
  --output "${resources}" --duration-seconds "${duration}" \
  --interval-seconds "${interval}" --warmup-seconds "${warmup}" \
  --phase-duration-seconds "${phase_duration}" \
  --container "${postgres_container}" --container "${pgbouncer_container}" &
sampler_pid=$!

set +e
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  --cpus 2 --memory 512m --pids-limit 128 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777 \
  --network "${project}_state" \
  --entrypoint /usr/local/bin/masi-dbload \
  --mount "type=bind,source=${runtime_root}/secrets/app-pgbouncer-dsn-container,target=/run/masi-postgresql/dsn,readonly" \
  --mount "type=bind,source=${runtime_root}/tls/dbctl,target=/run/masi-postgresql/tls,readonly" \
  "${image}" --dsn-file /run/masi-postgresql/dsn \
  --confirm-database masi_state_module_test --require-tls \
  "${load_args[@]}" >"${raw}" 2> >(tee "${progress}" >&2)
load_status=$?
set -e
if [[ "${load_status}" -ne 0 ]]; then
  cleanup_sampler
  exit "${load_status}"
fi
wait "${sampler_pid}"
sampler_pid=""
jq -e '.result=="PASS" and .soak.errors==0' "${raw}" >/dev/null
jq -e '.failures==[] and (.samples|length)>0' "${resources}" >/dev/null
