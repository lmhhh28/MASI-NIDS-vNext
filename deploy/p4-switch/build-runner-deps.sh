#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
image_ref=${1:-${MASI_P4_RUNNER_DEPS_IMAGE:-masi-nids/p4-switch-runner-deps:local}}
build_root=$(mktemp -d /tmp/masi-runner-deps-build.XXXXXX)
image_archive="$build_root/runner-deps-image.tar"
cleanup() {
  rm -rf -- "$build_root"
}
trap cleanup EXIT

build_arguments=(
  --progress=plain
  --output "type=docker,dest=$image_archive,rewrite-timestamp=true"
  --pull=false
  --platform linux/amd64
  --provenance=false
  --sbom=false
  --build-arg SOURCE_DATE_EPOCH=1786406400
  --file "$repo_root/deploy/p4-switch/Dockerfile.runner-deps"
  --tag "$image_ref"
)
if [ "${MASI_RUNNER_DEPS_NO_CACHE:-0}" = 1 ]; then
  build_arguments+=(--no-cache)
fi

docker buildx build "${build_arguments[@]}" "$repo_root"
docker image load --input "$image_archive" >/dev/null
docker image inspect "$image_ref" \
  --format 'runner_deps_image_id={{.Id}} repo_digests={{json .RepoDigests}}'
