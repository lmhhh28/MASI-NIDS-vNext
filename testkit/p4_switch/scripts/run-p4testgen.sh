#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 EVIDENCE_DIRECTORY" >&2
  exit 2
fi

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
evidence_dir=$1
generated_dir="$evidence_dir/p4testgen-generated"
install -d -o 65532 -g 65532 -m 0750 "$generated_dir"
chown -R 65532:65532 "$generated_dir"

docker run --rm \
  --network none \
  --read-only \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:size=64m,mode=1777 \
  --volume "$repo_root:/workspace:ro" \
  --volume "$generated_dir:/generated" \
  masi-nids/p4-switch-p4c@sha256:8c26666dfa1041b0f9a29b5051c92dbf4ce5df807273f80dd54b3aff5412c926 \
  p4testgen \
    --target bmv2 \
    --arch v1model \
    --std p4-16 \
    --test-backend PTF \
    --max-tests 8 \
    --seed 1 \
    --out-dir /generated \
    /workspace/p4/src/masi_switch.p4 \
  >"$evidence_dir/p4testgen.log" 2>&1

python3 "$repo_root/testkit/p4_switch/scripts/record-p4testgen.py" \
  "$generated_dir/masi_switch.py" \
  "$evidence_dir/p4testgen.log" \
  "$evidence_dir/p4testgen.json"
