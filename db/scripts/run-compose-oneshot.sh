#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 4 ]]; then
  echo "usage: run-compose-oneshot.sh PROJECT COMPOSE_FILE PROFILE SERVICE" >&2
  exit 64
fi
project="$1"
compose_file="$2"
profile="$3"
service="$4"
container="${project}-${service}-1"
docker compose --project-name "${project}" --file "${compose_file}" \
  --profile "${profile}" up --detach "${service}"
status="$(docker wait "${container}")"
docker logs "${container}" 2>&1
if [[ "${status}" -ne 0 ]]; then
  echo "one-shot ${service} exited ${status}" >&2
  exit "${status}"
fi
