#!/usr/bin/env bash
# shellcheck disable=SC1007,SC2094  # Empty CDPATH is intentional; SHA256SUMS is excluded from find input.
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
if [ "$#" -ne 9 ]; then
  echo "usage: run-supply-chain.sh EVIDENCE_DIR ARTIFACT_DIR RUN_ID RUNTIME_REF RUNNER_REF BMV2_SOURCE_ARCHIVE PI_SOURCE_ARCHIVE TRIVY_CACHE KEY_DIR" >&2
  exit 64
fi
evidence_dir=$(realpath "$1")
artifact_dir=$(realpath "$2")
run_id=$3
runtime_ref=$4
runner_ref=$5
source_archive=$(realpath "$6")
pi_source_archive=$(realpath "$7")
trivy_cache=$(realpath "$8")
key_dir=$(realpath "$9")
supply_dir="$evidence_dir/supply"
bundle_dir="$repo_root/out/supply-chain/offline-bundles/$run_id"
source_snapshot_dir="$repo_root/out/supply-chain/source-snapshots/$run_id"
mkdir -p "$supply_dir" "$bundle_dir" "$source_snapshot_dir"

readarray -t tools < <(
  python3 - "$repo_root/deploy/supply-chain/tools.lock.json" <<'PY'
import json, sys
lock=json.load(open(sys.argv[1], encoding="utf-8"))
for name in ("syft", "trivy", "cosign"):
    print(lock["tools"][name]["image"])
PY
)
syft_image=${tools[0]}
trivy_image=${tools[1]}
cosign_image=${tools[2]}
cosign_signing_config="$repo_root/deploy/supply-chain/cosign-offline-signing-config.json"
compiler_ref=masi-nids/p4-switch-p4c@sha256:8c26666dfa1041b0f9a29b5051c92dbf4ce5df807273f80dd54b3aff5412c926
runner_deps_ref=masi-nids/p4-switch-runner-deps:local
runner_deps_digest=sha256:54d602c6947b0268a9c93e4e3ab06d9621f0a7b26aa32f6a85932db864c59552

for image in "$runtime_ref" "$runner_ref" "$compiler_ref" "$runner_deps_ref" \
  "$syft_image" "$trivy_image" "$cosign_image"; do
  docker image inspect "$image" >/dev/null
done
test "$(docker image inspect "$runner_deps_ref" --format '{{.Id}}')" = "$runner_deps_digest"
test -f "$trivy_cache/db/trivy.db"
test -f "$trivy_cache/db/metadata.json"
test -f "$trivy_cache/java-db/trivy-java.db"
test -f "$trivy_cache/java-db/metadata.json"
test -f "$key_dir/cosign.key"
test -f "$key_dir/cosign.pub"
test -f "$cosign_signing_config"
cmp -s "$key_dir/cosign.pub" "$repo_root/contracts/trust/v1/cosign.pub"

docker image save --output "$bundle_dir/runtime-image.tar" "$runtime_ref"
docker image save --output "$bundle_dir/runner-image.tar" "$runner_ref"
docker image save --output "$bundle_dir/runner-deps-image.tar" "$runner_deps_ref"
docker image save --output "$bundle_dir/compiler-image.tar" "$compiler_ref"
docker image save --output "$bundle_dir/tool-images.tar" \
  "$syft_image" "$trivy_image" "$cosign_image"
tar -cf "$bundle_dir/trivy-db.tar" -C "$trivy_cache" db java-db
cp -- "$source_archive" "$bundle_dir/source-archive.tar.gz"
cp -- "$repo_root/deploy/p4-switch/bmv2/source.lock.json" "$bundle_dir/source.lock.json"
cp -- "$repo_root/deploy/p4-switch/bmv2/0001-bounded-graceful-shutdown.patch" "$bundle_dir/qualified-source.patch"
cp -- "$pi_source_archive" "$bundle_dir/pi-source-archive.tar.gz"
cp -- "$repo_root/deploy/p4-switch/pi/source.lock.json" "$bundle_dir/pi-source.lock.json"
cp -- "$repo_root/deploy/p4-switch/pi/0001-p4runtime-1.4.1-capabilities.patch" "$bundle_dir/qualified-pi.patch"
cp -- "$repo_root/deploy/p4-switch/bmv2/Dockerfile.runtime" "$bundle_dir/Dockerfile.runtime"
cp -- "$repo_root/deploy/p4-switch/Dockerfile.runner" "$bundle_dir/Dockerfile.runner"
cp -- "$repo_root/deploy/p4-switch/Dockerfile.runner-deps" "$bundle_dir/Dockerfile.runner-deps"
cp -- "$repo_root/deploy/p4-switch/Dockerfile.compiler" "$bundle_dir/Dockerfile.compiler"
cp -- "$repo_root/deploy/supply-chain/tools.lock.json" "$bundle_dir/tools.lock.json"
cp -- "$cosign_signing_config" "$bundle_dir/cosign-offline-signing-config.json"
cp -- "$repo_root/contracts/trust/v1/cosign.pub" "$bundle_dir/cosign.pub"
tar --sort=name --mtime=@1786406400 --owner=0 --group=0 --numeric-owner \
  --exclude=.git --exclude=.cache --exclude=.masi-secrets \
  --exclude=evidence --exclude=out --exclude='**/node_modules' \
  --exclude='**/.venv' --exclude='**/__pycache__' --exclude='*.pyc' \
  --exclude='**/build' --exclude='**/target' --exclude='**/dist' \
  --exclude='**/.ruff_cache' --exclude='**/.mypy_cache' \
  --exclude='**/.pytest_cache' --exclude='**/*.egg-info' \
  --exclude='**/coverage.out' \
  -cf "$bundle_dir/source-tree.tar" -C "$repo_root" .
tar -xf "$bundle_dir/source-tree.tar" -C "$source_snapshot_dir"

docker run --rm --network none \
  -e SYFT_CHECK_FOR_APP_UPDATE=false \
  -v "$bundle_dir:/input:ro" -v "$supply_dir:/out" \
  "$syft_image" scan docker-archive:/input/runtime-image.tar \
  -o spdx-json=/out/runtime.spdx.json
docker run --rm --network none \
  -e SYFT_CHECK_FOR_APP_UPDATE=false \
  -v "$bundle_dir:/input:ro" -v "$supply_dir:/out" \
  "$syft_image" scan docker-archive:/input/runner-image.tar \
  -o spdx-json=/out/runner.spdx.json
docker run --rm --network none \
  -e SYFT_CHECK_FOR_APP_UPDATE=false \
  -v "$bundle_dir:/input:ro" -v "$supply_dir:/out" \
  "$syft_image" scan docker-archive:/input/compiler-image.tar \
  -o spdx-json=/out/compiler.spdx.json
docker run --rm --network none \
  -e SYFT_CHECK_FOR_APP_UPDATE=false \
  -v "$source_snapshot_dir:/source:ro" -v "$supply_dir:/out" \
  "$syft_image" scan dir:/source \
  -o spdx-json=/out/source.spdx.json

for subject in runtime runner compiler; do
  docker run --rm --network none \
    -v "$trivy_cache/db:/root/.cache/trivy/db:ro" \
    -v "$trivy_cache/java-db:/root/.cache/trivy/java-db:ro" \
    -v "$bundle_dir:/input:ro" -v "$supply_dir:/out" \
    "$trivy_image" image --input "/input/${subject}-image.tar" \
    --skip-db-update --skip-java-db-update --scanners vuln,secret \
    --format json --output "/out/trivy-${subject}.json"
done
docker run --rm --network none \
  -v "$source_snapshot_dir:/source:ro" -v "$supply_dir:/out" \
  "$trivy_image" config --skip-check-update --format json \
  --output /out/trivy-config.json /source
docker run --rm --network none \
  -v "$source_snapshot_dir:/source:ro" -v "$supply_dir:/out" \
  "$trivy_image" fs --scanners secret --skip-db-update \
  --format json --output /out/trivy-source-secret.json /source

offline_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
runtime_rebuild_ref="masi-nids/p4-switch-runtime:offline-$run_id"
runner_rebuild_ref="masi-nids/p4-switch-e2e-runner:offline-$run_id"
set +e
MASI_BMV2_NO_CACHE=1 \
  "$repo_root/deploy/p4-switch/bmv2/build-runtime.sh" \
  "$source_archive" "$pi_source_archive" "$runtime_rebuild_ref" \
  >"$supply_dir/offline-runtime-rebuild.log" 2>&1
runtime_rebuild_status=$?
MASI_RUNNER_NO_CACHE=1 \
  "$repo_root/deploy/p4-switch/build-runner.sh" "$runner_rebuild_ref" \
  >"$supply_dir/offline-runner-rebuild.log" 2>&1
runner_rebuild_status=$?
set -e
runtime_expected=$(docker image inspect "$runtime_ref" --format '{{.Id}}')
runner_expected=$(docker image inspect "$runner_ref" --format '{{.Id}}')
compiler_expected=$(docker image inspect "$compiler_ref" --format '{{.Id}}')
runtime_observed=missing
runner_observed=missing
if [ "$runtime_rebuild_status" -eq 0 ]; then
  runtime_observed=$(docker image inspect "$runtime_rebuild_ref" --format '{{.Id}}')
fi
if [ "$runner_rebuild_status" -eq 0 ]; then
  runner_observed=$(docker image inspect "$runner_rebuild_ref" --format '{{.Id}}')
fi

(
  cd "$bundle_dir"
  find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%P\n' \
    | LC_ALL=C sort \
    | xargs -r sha256sum >SHA256SUMS
)
checksums_verified=false
if (cd "$bundle_dir" && sha256sum -c SHA256SUMS >"$supply_dir/offline-bundle-verify.log" 2>&1); then
  checksums_verified=true
fi
set +e
python3 "$repo_root/testkit/p4_switch/scripts/record-offline-rebuild.py" \
  --started-at "$offline_started_at" \
  --runtime-expected "$runtime_expected" --runtime-observed "$runtime_observed" \
  --runner-expected "$runner_expected" --runner-observed "$runner_observed" \
  --runtime-log "$supply_dir/offline-runtime-rebuild.log" \
  --runner-log "$supply_dir/offline-runner-rebuild.log" \
  --bundle-dir "$bundle_dir" --checksums-verified "$checksums_verified" \
  --output "$supply_dir/offline-rebuild.json"
offline_record_status=$?
set -e
if [ ! -s "$supply_dir/offline-rebuild.json" ]; then
  if [ "$offline_record_status" -eq 0 ]; then
    exit 1
  fi
  exit "$offline_record_status"
fi

python3 "$repo_root/testkit/p4_switch/scripts/create-supply-manifest.py" \
  --repo "$repo_root" --artifacts "$artifact_dir" --supply-dir "$supply_dir" \
  --source-archive "$source_archive" --pi-source-archive "$pi_source_archive" \
  --runtime-digest "$runtime_expected" --runner-digest "$runner_expected" \
  --compiler-digest "$compiler_expected" --runner-deps-digest "$runner_deps_digest" \
  --offline-result "$supply_dir/offline-rebuild.json" \
  --trivy-cache "$trivy_cache" --run-id "$run_id" \
  --provenance-output "$supply_dir/provenance.intoto.json" \
  --manifest-output "$supply_dir/release-manifest.json"

docker run --rm --user 0:0 --network none -e COSIGN_PASSWORD= \
  -v "$key_dir:/keys:ro" -v "$supply_dir:/supply" \
  -v "$cosign_signing_config:/config/signing-config.json:ro" \
  "$cosign_image" sign-blob --yes \
  --signing-config /config/signing-config.json \
  --bundle /supply/release-manifest.sigstore.json \
  --key /keys/cosign.key \
  /supply/release-manifest.json \
  >"$supply_dir/cosign-sign.log" 2>&1
chmod 0644 "$supply_dir/release-manifest.sigstore.json"

set +e
python3 "$repo_root/testkit/p4_switch/scripts/verify-supply-chain.py" \
  --repo "$repo_root" --supply-dir "$supply_dir" \
  --manifest "$supply_dir/release-manifest.json" \
  --bundle "$supply_dir/release-manifest.sigstore.json" \
  --offline-result "$supply_dir/offline-rebuild.json" \
  --trivy-cache "$trivy_cache" \
  --runtime-digest "$runtime_expected" --runner-digest "$runner_expected" \
  --compiler-digest "$compiler_expected" --runner-deps-digest "$runner_deps_digest" \
  --output "$evidence_dir/supply-chain.json"
verification_status=$?
set -e

docker image rm "$runtime_rebuild_ref" "$runner_rebuild_ref" >/dev/null 2>&1 || true
exit "$verification_status"
