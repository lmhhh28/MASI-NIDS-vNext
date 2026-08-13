#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
output_dir=${1:-"$repo_root/out/p4-switch"}

install -d -o 65532 -g 65532 -m 0750 "$output_dir"
chown -R 65532:65532 "$output_dir"
output_dir=$(CDPATH= cd -- "$output_dir" && pwd)
docker run --rm \
  --network none \
  --read-only \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:size=64m,mode=1777 \
  --volume "$repo_root/p4/src/masi_switch.p4:/source/masi_switch.p4:ro" \
  --volume "$output_dir:/artifacts" \
  masi-nids/p4-switch-p4c@sha256:8c26666dfa1041b0f9a29b5051c92dbf4ce5df807273f80dd54b3aff5412c926 \
  p4c-bm2-ss \
    --arch v1model \
    --std p4-16 \
    --p4runtime-files /artifacts/masi_switch.p4info.txtpb \
    --p4runtime-format text \
    -o /artifacts/masi_switch.json \
    /source/masi_switch.p4

sha256sum \
  "$output_dir/masi_switch.json" \
  "$output_dir/masi_switch.p4info.txtpb" \
  "$repo_root/p4/src/masi_switch.p4" \
  >"$output_dir/SHA256SUMS"
