# MASI-NIDS vNext Offline ML Artifact Pipeline

`MOD-ML-001` is the bounded Python 3.12 qualification target that turns an
immutable P4-window dataset into audited LR, XGBoost, and autoencoder
candidates, evaluates seeds `17/29/43`, emits non-causal explanation evidence,
selects exactly one winner, and publishes a read-only digest-pinned ONNX/Triton
repository atomically.

This module never connects to PostgreSQL or P4Runtime, does not own model
current/rollout, and cannot create Event, Proposal, Decision, Intent, or effect
facts. Its exact first bundle declares only `model-runtime-central-cpu/v1`;
CUDA is `NOT_APPLICABLE` for that bundle. The deterministic synthetic module
corpus can close operational Module Complete, but it does not replace official
corpus/legal/privacy/production-quality evidence, so qualification remains
`NOT_QUALIFIED`.

## Frozen toolchain

- CPython `3.12.13` (`>=3.12,<3.13`)
- `uv 0.12.3` with exact [uv.lock](./uv.lock)
- NumPy 2.2.6, scikit-learn 1.7.2, XGBoost CPU 3.3.0
- ONNX 1.21.0, ONNX Runtime CPU 1.19.0, ONNXMLTools 1.16.0,
  skl2onnx 1.20.0, SHAP 0.52.0
- no pytest; all Python tests use `unittest`

Bootstrap and verify the developer environment:

```bash
cd ml-py
uv sync --frozen --extra dev
.venv/bin/ruff format --check src tests scripts
.venv/bin/ruff check src tests scripts
.venv/bin/pyright
.venv/bin/python -m unittest discover -s tests -v
```

## Public contracts and real pipeline

```bash
cd ml-py
.venv/bin/python scripts/validate-contracts.py --repo ..
.venv/bin/python scripts/validate-static-contracts.py \
  --repo .. --output /tmp/masi-ml-static.json
.venv/bin/masi-offline-ml run --repo .. --output /tmp/masi-ml-candidate
.venv/bin/masi-offline-ml verify --repo .. --output /tmp/masi-ml-candidate
```

The output path must not exist. The pipeline publishes by a same-filesystem
atomic rename only after all nine candidates, explanations, contract bindings,
repository closure, ONNX CPU numeric checks, and archive checks pass. Existing
output is never overwritten. SIGTERM removes exact-owned staging; after
SIGKILL, the next run reclaims only a dead process staging tree carrying the
same exact target ownership marker.

## Release and OCI

```bash
cd ml-py
scripts/build-release-runtime.sh \
  /tmp/masi-ml-release /tmp/masi-ml-release-evidence.json

.venv/bin/python scripts/run-release-blackbox.py \
  --repo .. --entrypoint /tmp/masi-ml-release/venv/bin/masi-offline-ml \
  --output /tmp/masi-ml-release-candidate \
  --evidence /tmp/masi-ml-release-blackbox.json

MASI_ML_IMAGE_REF=masi-offline-ml:module-gates \
MASI_ML_BUILD_EVIDENCE=/tmp/masi-ml-image-evidence.json \
  scripts/build-image.sh

.venv/bin/python scripts/run-oci-smoke.py \
  --image masi-offline-ml:module-gates \
  --expected-blackbox /tmp/masi-ml-release-blackbox.json \
  --evidence /tmp/masi-ml-oci-evidence.json
```

Image construction is `--network=none` from a complete binary wheelhouse. A
digest-pinned CPython 3.12.13 image is an offline build stage only; the final
runtime is digest-pinned distroless Debian 12 plus exact-digest Wolfi zlib and
libffi files from a separately pinned source image. No source-image interpreter
or package database is copied, and the absolute entrypoint uses the copied
`/usr/local` CPython 3.12.13 with no interpreter fallback. The OCI smoke also
requires byte-identical model, repository-closure, and archive identities from
the independent release blackbox.
The runtime is UID/GID `65532:65532`; the formal smoke uses network none, a read-only
root filesystem, drop-all capabilities, no-new-privileges, two CPUs, 2 GiB RAM,
128 PIDs, and bounded tmpfs. This is a terminating offline job, so HTTP
startup/readiness/liveness endpoints are explicitly not applicable; the
equivalent operational boundary is process startup, full job exit `0`, atomic
artifact publication, and independent read-only verification.

The production-style Compose policy requires immutable inputs:

```bash
export MASI_OFFLINE_ML_IMAGE='registry.example/masi-offline-ml@sha256:<64-hex>'
export MASI_OFFLINE_ML_OUTPUT=/absolute/empty/output-parent
docker compose -f ../deploy/offline-ml/compose.acceptance.yaml config
```

## Candidate consumers, faults, and performance

After building the C++ validator and a release candidate output:

```bash
cd ../infer-cpp
cmake --preset cpu-release
cmake --build build/cpu-release --target masi_repository_validator
cd ../ml-py

.venv/bin/python scripts/run-central-consumer.py \
  --validator ../infer-cpp/build/cpu-release/masi_repository_validator \
  --pipeline-output /tmp/masi-ml-candidate \
  --evidence /tmp/masi-ml-central-consumer.json

.venv/bin/python scripts/run-triton-smoke.py \
  --pipeline-output /tmp/masi-ml-candidate \
  --evidence /tmp/masi-ml-triton.json

.venv/bin/python scripts/run-faults.py \
  --repo .. --entrypoint /tmp/masi-ml-release/venv/bin/masi-offline-ml \
  --evidence /tmp/masi-ml-fault.json

.venv/bin/python scripts/run-performance.py \
  --repo .. --entrypoint /tmp/masi-ml-release/venv/bin/masi-offline-ml \
  --evidence /tmp/masi-ml-performance.json
```

The Triton smoke uses the digest-pinned 25.06 image, `model-control-mode=none`,
disabled auto-complete, strict readiness, a read-only repository, and real
UINT64-to-FP32 inference. The C++ consumer calls the same closure and bundle
validator compiled into the Gateway and includes wrong-identity/digest negative
fences.

## Supply chain and soak

The supply gate needs the repository's offline Trivy DB/policy cache and local
Cosign bootstrap key material. All Syft/Trivy/Cosign containers run with network
none.

```bash
cd ml-py
MASI_ML_IMAGE_REF=masi-offline-ml:module-gates \
MASI_ML_SUPPLY_EVIDENCE_DIR=/tmp/masi-ml-supply \
  scripts/run-supply-chain.sh

# Short workflow validation only; never a formal PASS.
.venv/bin/python scripts/run-soak.py \
  --repo .. --entrypoint /tmp/masi-ml-release/venv/bin/masi-offline-ml \
  --evidence /tmp/masi-ml-soak-rehearsal.json --mode rehearsal

# Formal: 60-second uncounted warmup, then four exact 900-second phases.
.venv/bin/python scripts/run-soak.py \
  --repo .. --entrypoint /tmp/masi-ml-release/venv/bin/masi-offline-ml \
  --evidence /tmp/masi-ml-soak-formal.json --mode formal
```

The one-command formal gate is:

```bash
cd ml-py
scripts/run-module-gates.sh offline-ml-formal-YYYYMMDD-NNN
```

It refuses to label a shortened soak as formal and derives
`overall_module_complete` from schema-validated command/evidence digests,
current source/status identity, real release/OCI/Triton/C++ consumers, zero open
P0 findings, and all applicable operational gates. Protected baseline,
official-corpus production quality, pairwise/system integration, and production
HA remain qualification-only `HOLD` and are never renamed to Module PASS.
