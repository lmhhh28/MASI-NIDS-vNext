#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "$#" -ne 5 ]]; then
  echo "usage: scan-supply-chain.sh FRESH_OUTPUT_DIRECTORY DB_IMAGE PGBOUNCER_IMAGE POSTGRES_IMAGE RUN_ID" >&2
  exit 64
fi
output="$1"
db_image="$2"
pgbouncer_image="$3"
postgres_image="$4"
run_id="$5"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
db_root="$(cd -- "${script_dir}/.." && pwd)"
repo_root="$(cd -- "${db_root}/.." && pwd)"
cache="${MASI_TRIVY_CACHE:-${repo_root}/out/supply-chain/trivy-cache}"
syft_image="anchore/syft@sha256:5145aaf0384ca6db481c95e093e5c3b31a3b3f3af3dd3aadfb7529df88578019"
trivy_image="aquasec/trivy@sha256:c6e969c5662a546ad5de4a73c2a6b7a7c627f86d916903e175aa623af5b97ada"
if [[ ! "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ ]]; then
  echo "invalid supply-chain run id" >&2
  exit 64
fi

if [[ "${output}" != /* || -e "${output}" || -L "${output}" ]]; then
  echo "supply output must be a fresh absolute directory" >&2
  exit 64
fi
mkdir -m 0700 -- "${output}"
bash "${script_dir}/check-supply-chain.sh"
trivy_observation="$(python3 - "${cache}/db/metadata.json" <<'PY'
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
    raise SystemExit("unsafe Trivy database metadata")
metadata = json.loads(path.read_text(encoding="utf-8"))
timestamp = re.sub(r"(\.\d{6})\d+", r"\1", metadata["UpdatedAt"])
updated = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
observed = datetime.now(timezone.utc)
age = (observed - updated).total_seconds()
if age < 0 or age > 86400:
    raise SystemExit(f"Trivy database age outside 0..86400 seconds: {age}")
print(f"{age:.6f}\t{observed.isoformat().replace('+00:00', 'Z')}")
PY
)"
IFS=$'\t' read -r trivy_database_age_seconds observed_at <<<"${trivy_observation}"
cp -- "${cache}/db/metadata.json" "${output}/trivy-db-metadata.json"

for subject in db pgbouncer postgres; do
  case "${subject}" in
    db) image="${db_image}" ;;
    pgbouncer) image="${pgbouncer_image}" ;;
    postgres) image="${postgres_image}" ;;
  esac
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --env SYFT_CHECK_FOR_APP_UPDATE=false \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=1g,mode=1777 \
    --tmpfs /.cache:rw,noexec,nosuid,nodev,size=64m,mode=0700 \
    --volume /var/run/docker.sock:/var/run/docker.sock:ro \
    --volume "${output}:/out" "${syft_image}" "docker:${image}" \
    -o "cyclonedx-json=/out/${subject}.sbom.cdx.json"
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=1g,mode=1777 \
    --tmpfs /root/.cache/trivy/fanal:rw,noexec,nosuid,nodev,size=512m,mode=0700 \
    --volume /var/run/docker.sock:/var/run/docker.sock:ro \
    --volume "${cache}/db:/root/.cache/trivy/db:ro" \
    --volume "${cache}/java-db:/root/.cache/trivy/java-db:ro" \
    --volume "${output}:/out" "${trivy_image}" image "${image}" \
    --skip-db-update --skip-java-db-update --scanners vuln,secret \
    --format json --output "/out/${subject}.trivy.json"
done

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777 \
  --tmpfs /root/.cache/trivy/fanal:rw,noexec,nosuid,nodev,size=256m,mode=0700 \
  --volume "${repo_root}:/source:ro" --volume "${output}:/out" \
  "${trivy_image}" fs --scanners secret --skip-db-update --format json \
  --skip-dirs /source/.git --skip-dirs /source/.masi-secrets \
  --skip-dirs /source/out --skip-dirs /source/evidence \
  --skip-dirs /source/db/evidence --skip-dirs /source/control-go/evidence \
  --skip-dirs /source/edge-rs/evidence --skip-dirs /source/infer-cpp/evidence \
  --skip-dirs /source/plugin-host-rs/evidence --skip-dirs /source/analysis-py/evidence \
  --skip-dirs /source/ml-py/evidence --skip-dirs /source/web/evidence \
  --skip-dirs /source/p4/evidence --skip-dirs /source/testkit/evidence \
  --skip-dirs /source/contracts/openapi/v1/typescript-client/node_modules \
  --skip-dirs /source/web/node_modules \
  --output /out/source.trivy-secret.json /source

for subject in db pgbouncer postgres; do
  jq -e '.components | length > 0' "${output}/${subject}.sbom.cdx.json" >/dev/null
  jq -e '[.Results[]?.Secrets[]?] | length == 0' "${output}/${subject}.trivy.json" >/dev/null
  jq -e '[.Results[]?.Vulnerabilities[]? |
    select(.Severity=="CRITICAL" or (.Severity=="HIGH" and ((.FixedVersion // "") != "")))] |
    length == 0' "${output}/${subject}.trivy.json" >/dev/null
done
jq -e '[.Results[]?.Secrets[]?] | length == 0' "${output}/source.trivy-secret.json" >/dev/null

critical_vulnerabilities="$(jq -s '[.[] | .Results[]?.Vulnerabilities[]? |
  select(.Severity=="CRITICAL")] | length' "${output}/"{db,pgbouncer,postgres}.trivy.json)"
fixable_high_vulnerabilities="$(jq -s '[.[] | .Results[]?.Vulnerabilities[]? |
  select(.Severity=="HIGH" and ((.FixedVersion // "") != ""))] | length' \
  "${output}/"{db,pgbouncer,postgres}.trivy.json)"
image_secrets="$(jq -s '[.[] | .Results[]?.Secrets[]?] | length' \
  "${output}/"{db,pgbouncer,postgres}.trivy.json)"
source_secrets="$(jq '[.Results[]?.Secrets[]?] | length' "${output}/source.trivy-secret.json")"
db_id="$(docker image inspect --format '{{.Id}}' "${db_image}")"
pgbouncer_id="$(docker image inspect --format '{{.Id}}' "${pgbouncer_image}")"
postgres_id="$(docker image inspect --format '{{.Id}}' "${postgres_image}")"
evidence="${output}/supply-evidence.json"
temporary="${output}/.supply-evidence.$$.json"
trap 'unlink -- "${temporary}" 2>/dev/null || true' EXIT

jq -n \
  --arg run_id "${run_id}" \
  --arg observed_at "${observed_at}" \
  --arg db_image "${db_image}" --arg pgbouncer_image "${pgbouncer_image}" \
  --arg postgres_image "${postgres_image}" \
  --arg db_id "${db_id}" --arg pgbouncer_id "${pgbouncer_id}" --arg postgres_id "${postgres_id}" \
  --arg db_sbom "sha256:$(sha256sum "${output}/db.sbom.cdx.json" | awk '{print $1}')" \
  --arg pgbouncer_sbom "sha256:$(sha256sum "${output}/pgbouncer.sbom.cdx.json" | awk '{print $1}')" \
  --arg db_scan "sha256:$(sha256sum "${output}/db.trivy.json" | awk '{print $1}')" \
  --arg pgbouncer_scan "sha256:$(sha256sum "${output}/pgbouncer.trivy.json" | awk '{print $1}')" \
  --arg postgres_sbom "sha256:$(sha256sum "${output}/postgres.sbom.cdx.json" | awk '{print $1}')" \
  --arg postgres_scan "sha256:$(sha256sum "${output}/postgres.trivy.json" | awk '{print $1}')" \
  --arg source_scan "sha256:$(sha256sum "${output}/source.trivy-secret.json" | awk '{print $1}')" \
  --arg trivy_db_metadata "sha256:$(sha256sum "${output}/trivy-db-metadata.json" | awk '{print $1}')" \
  --argjson critical_vulnerabilities "${critical_vulnerabilities}" \
  --argjson fixable_high_vulnerabilities "${fixable_high_vulnerabilities}" \
  --argjson image_secrets "${image_secrets}" --argjson source_secrets "${source_secrets}" \
  --argjson trivy_database_age_seconds "${trivy_database_age_seconds}" \
  '{schema_version:"postgresql-state-supply-evidence/v1",test_id:"TEST-DB-SUPPLY-001",
    module_id:"MOD-DB-001",run_id:$run_id,result:"PASS",
    qualification:"QUALIFIED",network_during_scan:"none",
    observed_at:$observed_at,
    images:{db:$db_image,pgbouncer:$pgbouncer_image,postgres:$postgres_image},
    image_ids:{db:$db_id,pgbouncer:$pgbouncer_id,postgres:$postgres_id},
    digests:{db_sbom:$db_sbom,pgbouncer_sbom:$pgbouncer_sbom,
      db_scan:$db_scan,pgbouncer_scan:$pgbouncer_scan,postgres_sbom:$postgres_sbom,
      postgres_scan:$postgres_scan,source_scan:$source_scan,
      trivy_db_metadata:$trivy_db_metadata},
    policy:{critical_vulnerabilities:$critical_vulnerabilities,
      fixable_high_vulnerabilities:$fixable_high_vulnerabilities,
      image_secrets:$image_secrets,source_secrets:$source_secrets},
    trivy_database_age_seconds:$trivy_database_age_seconds,
    registry_no_redis_object_store:true}' >"${temporary}"
python3 "${script_dir}/validate-json.py" \
  --schema "${repo_root}/contracts/evidence/postgresql-state-supply/v1/schema.json" \
  --document "${temporary}" >/dev/null
mv -T -- "${temporary}" "${evidence}"
trap - EXIT
