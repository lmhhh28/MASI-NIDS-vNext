#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
lock_file="$repo_root/deploy/p4-switch/bmv2/source.lock.json"
patch_file="$repo_root/deploy/p4-switch/bmv2/0001-bounded-graceful-shutdown.patch"
pi_lock_file="$repo_root/deploy/p4-switch/pi/source.lock.json"
pi_patch_file="$repo_root/deploy/p4-switch/pi/0001-p4runtime-1.4.1-capabilities.patch"
dockerfile="$repo_root/deploy/p4-switch/bmv2/Dockerfile.runtime"
source_archive=${1:-${MASI_BMV2_SOURCE_ARCHIVE:-}}
pi_source_archive=${2:-${MASI_PI_SOURCE_ARCHIVE:-}}
image_ref=${3:-${MASI_P4_RUNTIME_IMAGE:-masi-nids/p4-switch-runtime:local}}
metadata_file=${MASI_BMV2_BUILD_METADATA:-}

if [ -z "$source_archive" ] || [ -z "$pi_source_archive" ]; then
  echo "usage: build-runtime.sh BMV2_SOURCE_ARCHIVE PI_SOURCE_ARCHIVE [IMAGE_REF]" >&2
  exit 64
fi
if [ ! -f "$source_archive" ]; then
  echo "BMv2 source archive does not exist: $source_archive" >&2
  exit 66
fi
if [ ! -f "$pi_source_archive" ]; then
  echo "PI recursive source archive does not exist: $pi_source_archive" >&2
  exit 66
fi

readarray -t lock_values < <(
  python3 - "$lock_file" <<'PY'
import json
import sys

lock = json.load(open(sys.argv[1], encoding="utf-8"))
print(lock["source_commit"])
print(lock["source_archive"]["filename"])
print(lock["source_archive"]["sha256"])
print(lock["source_archive"]["size_bytes"])
print(lock["patch"]["sha256"])
print(lock["source_date_epoch"])
PY
)
source_commit=${lock_values[0]}
expected_name=${lock_values[1]}
expected_archive_sha=${lock_values[2]}
expected_archive_size=${lock_values[3]}
expected_patch_sha=${lock_values[4]}
source_date_epoch=${lock_values[5]}

readarray -t pi_lock_values < <(
  python3 - "$pi_lock_file" <<'PY'
import json
import sys

lock = json.load(open(sys.argv[1], encoding="utf-8"))
print(lock["source_commit"])
print(lock["source_archive"]["filename"])
print(lock["source_archive"]["sha256"])
print(lock["source_archive"]["size_bytes"])
print(lock["source_archive"]["top_level_directory"])
print(lock["patch"]["sha256"])
print(lock["source_date_epoch"])
PY
)
pi_source_commit=${pi_lock_values[0]}
pi_expected_name=${pi_lock_values[1]}
pi_expected_archive_sha=${pi_lock_values[2]}
pi_expected_archive_size=${pi_lock_values[3]}
pi_top_level=${pi_lock_values[4]}
pi_expected_patch_sha=${pi_lock_values[5]}
pi_source_date_epoch=${pi_lock_values[6]}
if [ "$pi_source_date_epoch" != "$source_date_epoch" ]; then
  echo "BMv2 and PI source locks use different SOURCE_DATE_EPOCH values" >&2
  exit 65
fi

observed_name=$(basename -- "$source_archive")
observed_archive_sha=$(sha256sum "$source_archive" | cut -d' ' -f1)
observed_archive_size=$(stat -c '%s' "$source_archive")
observed_patch_sha=$(sha256sum "$patch_file" | cut -d' ' -f1)
pi_observed_name=$(basename -- "$pi_source_archive")
pi_observed_archive_sha=$(sha256sum "$pi_source_archive" | cut -d' ' -f1)
pi_observed_archive_size=$(stat -c '%s' "$pi_source_archive")
pi_observed_patch_sha=$(sha256sum "$pi_patch_file" | cut -d' ' -f1)
if [ "$observed_name" != "$expected_name" ] \
  || [ "$observed_archive_sha" != "$expected_archive_sha" ] \
  || [ "$observed_archive_size" != "$expected_archive_size" ] \
  || [ "$observed_patch_sha" != "$expected_patch_sha" ]; then
  echo "source archive or patch does not match source.lock.json" >&2
  exit 65
fi
if [ "$pi_observed_name" != "$pi_expected_name" ] \
  || [ "$pi_observed_archive_sha" != "$pi_expected_archive_sha" ] \
  || [ "$pi_observed_archive_size" != "$pi_expected_archive_size" ] \
  || [ "$pi_observed_patch_sha" != "$pi_expected_patch_sha" ]; then
  echo "PI source archive or patch does not match its source.lock.json" >&2
  exit 65
fi

build_root=$(mktemp -d /tmp/masi-bmv2-build.XXXXXX)
image_archive="$build_root/runtime-image.tar"
cleanup() {
  rm -rf -- "$build_root"
}
trap cleanup EXIT

tar -xzf "$source_archive" -C "$build_root"
source_dir="$build_root/behavioral-model-$source_commit"
if [ ! -d "$source_dir" ]; then
  echo "source archive has an unexpected top-level directory" >&2
  exit 65
fi
patch --batch --forward --fuzz=0 -d "$source_dir" -p1 <"$patch_file"
mkdir -p "$build_root/pi"
tar -xzf "$pi_source_archive" -C "$build_root/pi"
pi_source_dir="$build_root/pi/$pi_top_level"
if [ ! -d "$pi_source_dir" ] || [ ! -x "$pi_source_dir/configure" ]; then
  echo "PI source archive has an unexpected layout or lacks generated configure" >&2
  exit 65
fi
patch --batch --forward --fuzz=0 -d "$pi_source_dir" -p1 <"$pi_patch_file"
if ! grep -Fq 'p4runtime_api_version[] = "1.4.1"' \
  "$pi_source_dir/proto/server/pi_server.cpp"; then
  echo "PI patch did not freeze the Capabilities version to 1.4.1" >&2
  exit 65
fi
mv "$pi_source_dir" "$source_dir/.masi-pi-source"

build_arguments=(
  --progress=plain
  --output "type=docker,dest=$image_archive,rewrite-timestamp=true"
  --pull=false
  --network none
  --platform linux/amd64
  --provenance=false
  --sbom=false
  --build-arg "SOURCE_DATE_EPOCH=$source_date_epoch"
  --build-arg "PI_SOURCE_COMMIT=$pi_source_commit"
  --file "$dockerfile"
  --tag "$image_ref"
)
if [ -n "$metadata_file" ]; then
  mkdir -p "$(dirname -- "$metadata_file")"
  build_arguments+=(--metadata-file "$metadata_file")
fi
if [ "${MASI_BMV2_NO_CACHE:-0}" = 1 ]; then
  build_arguments+=(--no-cache)
fi

docker buildx build \
  "${build_arguments[@]}" \
  "$source_dir"
docker image load --input "$image_archive" >/dev/null

docker image inspect "$image_ref" \
  --format 'runtime_image_id={{.Id}} repo_digests={{json .RepoDigests}}'
