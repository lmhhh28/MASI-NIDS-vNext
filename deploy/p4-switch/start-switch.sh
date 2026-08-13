#!/usr/bin/env bash
set -euo pipefail

deadline=$((SECONDS + 30))
while [ ! -e /sys/class/net/masi-s1 ] \
  || [ ! -e /sys/class/net/masi-s2 ] \
  || [ "$(cat /sys/class/net/masi-s1/operstate 2>/dev/null || true)" != up ] \
  || [ "$(cat /sys/class/net/masi-s2/operstate 2>/dev/null || true)" != up ]; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "timed out waiting for isolated test interfaces" >&2
    exit 1
  fi
  sleep 0.05
done

exec simple_switch_grpc \
  --no-p4 \
  --device-id 1 \
  --max-port-count 512 \
  --log-console \
  --log-level "${MASI_P4_LOG_LEVEL:-warn}" \
  -i 1@masi-s1 \
  -i 2@masi-s2 \
  -- \
  --grpc-server-addr 0.0.0.0:9559 \
  --grpc-server-ssl \
  --grpc-server-cacert /certs/ca.crt \
  --grpc-server-cert /certs/server.crt \
  --grpc-server-key /certs/server.key \
  --grpc-server-with-client-auth \
  --cpu-port 510
