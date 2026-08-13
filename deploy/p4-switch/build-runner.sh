#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
deps_ref=${MASI_P4_RUNNER_DEPS_IMAGE:-masi-nids/p4-switch-runner-deps:local}
deps_digest=sha256:54d602c6947b0268a9c93e4e3ab06d9621f0a7b26aa32f6a85932db864c59552
image_ref=${1:-${MASI_P4_RUNNER_IMAGE:-masi-nids/p4-switch-e2e-runner:local}}
observed_deps=$(docker image inspect "$deps_ref" --format '{{.Id}}')
if [ "$observed_deps" != "$deps_digest" ]; then
  echo "runner dependency image mismatch: expected $deps_digest, got $observed_deps" >&2
  exit 65
fi

build_root=$(mktemp -d /tmp/masi-runner-build.XXXXXX)
image_archive="$build_root/runner-image.tar"
cleanup() {
  rm -rf -- "$build_root"
}
trap cleanup EXIT

build_arguments=(
  --progress=plain
  --output "type=docker,dest=$image_archive,rewrite-timestamp=true"
  --pull=false
  --network none
  --platform linux/amd64
  --provenance=false
  --sbom=false
  --build-arg "RUNNER_DEPS_IMAGE=$deps_ref"
  --build-arg SOURCE_DATE_EPOCH=1786406400
  --file "$repo_root/deploy/p4-switch/Dockerfile.runner"
  --tag "$image_ref"
)
if [ "${MASI_RUNNER_NO_CACHE:-0}" = 1 ]; then
  build_arguments+=(--no-cache)
fi
if [ -n "${MASI_RUNNER_BUILD_METADATA:-}" ]; then
  mkdir -p "$(dirname -- "$MASI_RUNNER_BUILD_METADATA")"
  build_arguments+=(--metadata-file "$MASI_RUNNER_BUILD_METADATA")
fi

docker buildx build "${build_arguments[@]}" "$repo_root"
docker image load --input "$image_archive" >/dev/null
docker image inspect "$image_ref" \
  --format 'runner_image_id={{.Id}} repo_digests={{json .RepoDigests}}'
