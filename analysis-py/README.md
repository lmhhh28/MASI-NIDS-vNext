# MASI-NIDS-vNext Analysis Plugin

`analysis-py/` is the seventh independent module (`MOD-AGENT-001`) and the official
`masi.analysis.langgraph` / `analysis-agent` plugin. It runs as an independent
CPython 3.12 service. Go or an approved peer sends bounded A2A 1.0 tasks directly;
the plugin uses the restricted MCP 2025-11-25 client, a fixed LangGraph, an optional
bounded A2A peer call, and a structured provider adapter. Business traffic never
passes through the Rust Plugin Runtime Host.

The module owns only its private bounded Task/Artifact/Trace store. It has no core
PostgreSQL, P4Runtime, Edge, effect, approval, deployment or Docker authority.
Every Artifact is grounded, `non_executable=true`, and
`deployment_eligible=false`. Model result facts, qualified explanation facts and
LLM interpretation remain separate layers.

## Frozen runtime

- CPython `3.12.13`;
- LangGraph `1.1.10`;
- MCP Python SDK `2.0.0` with a stricter project-owned transport policy;
- A2A HTTP+JSON `1.0`, polling only;
- offline CPython builder
  `python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2`;
- final Wolfi runtime
  `cgr.dev/chainguard/python@sha256:d812438658b47b73cb4c089f4cca09bca1ba50f6cd1843133864ee074d9ec49b`;
- exact OpenSSL `3.0.20` `libcrypto`/`libssl` files from the builder, with
  build-time digest and effective-version probes; the final runtime supplies
  SQLite `3.53.4` and zlib `1.3.2`;
- exact transitive dependency closure in `uv.lock`;
- runtime image contains only the Analysis config/binding and plugin manifest
  schemas needed at startup.

## Development and language gates

Use the repository-qualified Python 3.12 interpreter; do not use the host Python
3.10 and do not add pytest.

```bash
cd analysis-py
uv sync --frozen
ruff format --check src tests scripts
ruff check src tests scripts
pyright --project pyproject.toml
.venv/bin/python -m unittest discover -s tests -t . -v
.venv/bin/python scripts/validate-contracts.py
```

Regenerate the shared Go/Python wire vectors only after an intentional contract
change, then review every diff and update the compatibility matrix catalog digest:

```bash
cd analysis-py
.venv/bin/python scripts/generate-contract-goldens.py
.venv/bin/python scripts/validate-compatibility.py \
  --repo .. \
  --legacy-root /home/lmhhh/MASI-NIDS \
  --require-legacy-snapshot
```

## Release process and public-boundary tests

The release builder constructs the project wheel twice, requires byte-identical
digests, installs the locked host wheel closure into a fresh Python 3.12 virtual
environment, and records the actual entrypoint digest.

```bash
cd analysis-py
release_root="$(mktemp -d /tmp/masi-analysis-release.XXXXXX)"
./scripts/build-release-runtime.sh \
  "${release_root}/runtime" \
  "${release_root}/release-evidence.json"
"${release_root}/runtime/venv/bin/python" scripts/run-blackbox.py \
  --binary "${release_root}/runtime/venv/bin/masi-analysis"
"${release_root}/runtime/venv/bin/python" scripts/run-performance.py \
  --binary "${release_root}/runtime/venv/bin/masi-analysis"
```

The black box starts the actual release process plus real TLS 1.3/mTLS A2A, MCP,
provider and peer HTTP boundaries. The neighboring provider/MCP/peer processes are
deterministic contract fixtures; they do not qualify a real external LLM provider.

A short soak is only a rehearsal:

```bash
"${release_root}/runtime/venv/bin/python" scripts/run-soak.py \
  --binary "${release_root}/runtime/venv/bin/masi-analysis" \
  --warmup-seconds 2 \
  --phase-seconds 3
```

The formal gate always runs an uncounted 60-second warmup followed by four
continuous 900-second phases and validates at least 3,600,000 monotonic
milliseconds. It cannot be shortened while still producing formal evidence.

## OCI and supply chain

The image build downloads the exact glibc wheel closure before the build, then
installs it with Docker build networking disabled. It verifies the two selected
OpenSSL files by digest and executes a network-none Python/OpenSSL/SQLite/zlib ABI
probe before publishing evidence. The local tag is only a Module candidate;
production deployment assets require `repository@sha256`.

```bash
cd analysis-py
MASI_ANALYSIS_IMAGE_REF=masi-analysis:module-gates ./scripts/build-image.sh
.venv/bin/python scripts/run-oci-smoke.py \
  --image masi-analysis:module-gates \
  --allow-local-candidate
```

The supply gate needs the pinned Syft, Trivy and Cosign images, a current offline
Trivy database under `out/supply-chain/trivy-cache`, the controlled signing key
under `.masi-secrets/cosign`, and the public key under `contracts/trust/v1`:

```bash
cd analysis-py
MASI_ANALYSIS_IMAGE_REF=masi-analysis:module-gates \
  ./scripts/run-supply-chain.sh
```

It generates source, builder and final-image SPDX SBOMs; scans the final runtime and
the selected builder library provenance offline; performs misconfiguration and
secret scans; signs the exact release manifest; verifies it offline; and proves
tampered and wrong-publisher negatives fail closed.

For production compose rendering, set both parts of an immutable reference:

```bash
export MASI_ANALYSIS_IMAGE_REPOSITORY=registry.example/masi-analysis
export MASI_ANALYSIS_IMAGE_DIGEST=sha256:REPLACE_WITH_64_LOWER_HEX
export MASI_ANALYSIS_CONFIG_DIR=/absolute/read-only/config
export MASI_ANALYSIS_SECRET_DIR=/absolute/read-only/secrets
export MASI_ANALYSIS_NETWORK=precreated-restricted-analysis-network
docker compose -f deploy/analysis/compose.acceptance.yaml config
```

## Operational Module Complete gate

The only completion authority is:

```bash
cd analysis-py
MASI_ANALYSIS_RUN_ID=analysis-formal-YYYYMMDD-NNN \
  ./scripts/run-module-gates.sh
```

It creates a new immutable `evidence/module-gates/runs/<run-id>/` directory with a
log and `edge-command-execution/v1` sidecar for every required command, structured
release/black-box/compatibility/performance/OCI/deployment/supply/soak evidence,
exact requirement traceability, tamper negatives and a machine-derived summary.
An existing run ID is never overwritten.

Operational `overall_module_complete=true` is distinct from qualification. A dirty
tree, local candidate image, deterministic provider fixture, single failure domain,
and not-yet-started pairwise/system work remain explicit
`qualification=NOT_QUALIFIED` / `HOLD` and cannot be renamed to Module, pairwise,
system or production PASS.
