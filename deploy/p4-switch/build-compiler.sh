#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
image_ref=${1:-${MASI_P4_COMPILER_IMAGE:-masi-nids/p4-switch-p4c:local}}
build_root=$(mktemp -d /tmp/masi-p4c-build.XXXXXX)
image_archive="$build_root/compiler-image.tar"
cleanup() {
  rm -rf -- "$build_root"
}
trap cleanup EXIT

docker buildx build \
  --progress=plain \
  --output "type=docker,dest=$image_archive,rewrite-timestamp=true" \
  --pull=false \
  --network none \
  --platform linux/amd64 \
  --provenance=false \
  --sbom=false \
  --build-arg SOURCE_DATE_EPOCH=1786406400 \
  --file "$repo_root/deploy/p4-switch/Dockerfile.compiler" \
  --tag "$image_ref" \
  "$repo_root"
docker image load --input "$image_archive" >/dev/null
docker image inspect "$image_ref" \
  --format 'compiler_image_id={{.Id}} repo_digests={{json .RepoDigests}}'
