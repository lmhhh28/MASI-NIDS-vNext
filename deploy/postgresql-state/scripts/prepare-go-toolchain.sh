#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "$#" -ne 1 || "$1" != /* || -L "$1" || ! -d "$1" \
  || -n "$(find "$1" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "usage: prepare-go-toolchain.sh ABSOLUTE_FRESH_EMPTY_DIRECTORY" >&2
  exit 64
fi
destination="$(realpath -e -- "$1")"
archive="${destination}/go1.26.6.linux-amd64.tar.gz"
cache="${MASI_GO_TOOLCHAIN_CACHE:-}"
if [[ -n "${cache}" && "${cache}" != /* ]]; then
  echo "toolchain cache path must be absolute" >&2
  exit 64
fi
if [[ -n "${cache}" && -f "${cache}" && ! -L "${cache}" ]]; then
  cp -- "${cache}" "${archive}"
else
  curl --fail --location --proto '=https' --tlsv1.2 \
    --output "${archive}" https://go.dev/dl/go1.26.6.linux-amd64.tar.gz
fi
echo '708effb774be8237570d0add163225abbdfaf4fca28b2611df167beba4feef89  '"${archive}" | sha256sum -c -
if [[ -n "${cache}" && ! -e "${cache}" && ! -L "${cache}" ]]; then
  mkdir -p -- "$(dirname -- "${cache}")"
  install -m 0600 -- "${archive}" "${cache}.$$.tmp"
  mv -T -- "${cache}.$$.tmp" "${cache}"
fi
tar --extract --gzip --file "${archive}" --strip-components=1 --directory "${destination}"
unlink -- "${archive}"
"${destination}/bin/go" version | grep -F 'go version go1.26.6 linux/amd64'
