#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 OUTPUT_DIRECTORY" >&2
  exit 2
fi

certificate_dir=$1
mkdir -p "$certificate_dir"
umask 077

openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 2 \
  -subj '/CN=MASI P4 E2E CA' \
  -keyout "$certificate_dir/ca.key" \
  -out "$certificate_dir/ca.crt" >/dev/null 2>&1

openssl req -newkey rsa:3072 -sha256 -nodes \
  -subj '/CN=masi-switch' \
  -addext 'subjectAltName=DNS:masi-switch,DNS:localhost,IP:127.0.0.1' \
  -keyout "$certificate_dir/server.key" \
  -out "$certificate_dir/server.csr" >/dev/null 2>&1
printf '%s\n' \
  'subjectAltName=DNS:masi-switch,DNS:localhost,IP:127.0.0.1' \
  'extendedKeyUsage=serverAuth' \
  >"$certificate_dir/server.ext"
openssl x509 -req -sha256 -days 2 \
  -in "$certificate_dir/server.csr" \
  -CA "$certificate_dir/ca.crt" \
  -CAkey "$certificate_dir/ca.key" \
  -CAcreateserial \
  -extfile "$certificate_dir/server.ext" \
  -out "$certificate_dir/server.crt" >/dev/null 2>&1

openssl req -newkey rsa:3072 -sha256 -nodes \
  -subj '/CN=masi-p4-e2e-controller' \
  -keyout "$certificate_dir/client.key" \
  -out "$certificate_dir/client.csr" >/dev/null 2>&1
printf '%s\n' 'extendedKeyUsage=clientAuth' >"$certificate_dir/client.ext"
openssl x509 -req -sha256 -days 2 \
  -in "$certificate_dir/client.csr" \
  -CA "$certificate_dir/ca.crt" \
  -CAkey "$certificate_dir/ca.key" \
  -CAcreateserial \
  -extfile "$certificate_dir/client.ext" \
  -out "$certificate_dir/client.crt" >/dev/null 2>&1

chmod 0400 "$certificate_dir"/*.key
chmod 0440 "$certificate_dir/server.key" "$certificate_dir/client.key"
chmod 0444 "$certificate_dir"/*.crt
sha256sum "$certificate_dir/ca.crt" "$certificate_dir/server.crt" "$certificate_dir/client.crt" \
  >"$certificate_dir/SHA256SUMS"
chown -R 65532:65532 "$certificate_dir"
chmod 0750 "$certificate_dir"
