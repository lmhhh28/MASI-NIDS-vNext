# MASI Plugin Runtime Host

`plugin-host-rs` is the sixth independent vNext module (`MOD-PLUGIN-001`). It is a deny-by-default execution isolation layer for exact Go Plugin Manager bindings; it is not a catalog owner, database writer, scheduler, container orchestrator, Analysis Agent proxy, effect path or P4 client.

The authoritative operational state is machine-derived from `evidence/module-gates/latest.json` and the referenced immutable `gate-summary.json`. A summary may set `overall_module_complete=true` only after every required command, the real release/OCI black box, both runtime profiles, supply-chain checks, bounded performance, the exact 60-second warmup plus 3,600-second formal soak, evidence integrity and open-P0-zero checks pass. The summary deliberately remains `qualification=NOT_QUALIFIED`: protected release baseline, formal Go↔Host pairwise, official Analysis, multi-module core-load isolation, nine-module global completion and production HA are separate gates.

## Ownership and runtime closure

- Go Plugin Manager remains the only writer of catalog, qualification, canonical lifecycle, binding and revocation facts.
- The Host observes immutable `plugin_id/revision/artifact/config/contract/capability/generation/epoch` envelopes and reports runtime observations; its memory queue and caches are not durable facts. The strict manifest also binds artifact media/entrypoint/platform, Host/config/kind contracts, ambient-capability declarations, full resource requests, compatibility/rollback, lifecycle and observability profiles.
- `wasm-component/v1` is fixed to Wasmtime `47.0.3`, WASI 0.2 Component Model and `masi:plugin-transform@1.0.0`. The qualified world has exactly one `transform` export and zero imports.
- `grpc-service/v1` uses an allowlisted absolute per-generation UDS, directory mode `0750`, socket mode `0660`, no symbolic-link component, exact UID/GID, `SO_PEERCRED`, exact workload identity and typed challenge handshake. The Host never starts the service process or receives arbitrary command/environment input.
- `analysis-agent` business A2A/MCP traffic is explicitly direct and never routed through this Host.
- Statistics execution uses the existing Go adapter, an exact active definition revision+digest mapping, schema-complete Go-frozen input with independently recomputed semantic/full-bundle digests, pure-transform capability, bounded candidate validation and Host-supplied final Artifact identity/provenance. It cannot create schedules, current projections, database facts, browser code or Prometheus business series.
- Ordinary binding activation is generation-monotonic. A delayed refresh for an older generation is fenced; moving backward is possible only through the typed `RollbackBinding` request with an exact active `from_generation` CAS fence, a separately authorized digest, manifest compatibility and a still-observed draining target. Drain/revoke also bind actor, reason, authorization and expected-envelope CAS. A later higher generation can be activated normally as a monotonic roll-forward.

## Security and resource profile

The Manager boundary is TLS 1.3-only mTLS with a client CA plus exact leaf DER SHA-256 allowlist. Plaintext, TLS 1.2 and a CA-valid but non-allowlisted leaf are rejected. Connections, HTTP/2 streams, messages, queues, in-flight calls, deadlines, Wasm fuel/memory/table/instance/stack and output are bounded. Invocation admission rechecks envelope expiry and revocation freshness; a fixed 30-second reconciler fences stale active/shadow/staged observations even without a new call.

The first-release maximums are 32 observed bindings, 64 global queued/running calls, 32 queued and 2 running calls per binding, 4 MiB control messages, 2 MiB input, 1 MiB output and 10 seconds total deadline. Fuel exhaustion, epoch interruption, deadline and caller cancellation have different stable error codes. Repeated failures use 1–60 second exponential backoff, a circuit, at most five restart events per ten minutes and at least fifteen minutes of quarantine.

The production image contains only `masi-plugin-host`, runs as `65532:65532`, has a semantic in-container health probe, and supports read-only root filesystem, all Linux capabilities dropped and `no-new-privileges`. Configuration, TLS material and component cache are external read-only mounts.

## Reproducible commands

Run from `plugin-host-rs/`:

```bash
cargo fmt --all -- --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --locked --all-targets --all-features -- --test-threads=1
RUSTDOCFLAGS=-Dwarnings cargo doc --locked --all-features --no-deps
python3 scripts/validate-contracts.py
python3 scripts/check-source-sentinels.py
```

The release-candidate public-boundary black box is:

```bash
cargo build --locked --release --bins
MASI_PLUGIN_HOST_BINARY="$PWD/target/release/masi-plugin-host" \
MASI_PLUGIN_SERVICE_FIXTURE_BINARY="$PWD/target/release/masi-plugin-service-fixture" \
cargo test --locked --release --test module_blackbox -- --nocapture --test-threads=1
```

Independent expensive gates are:

```bash
scripts/run-deep-checks.sh
scripts/run-performance.sh
scripts/run-oci-smoke.sh
scripts/run-supply-chain.sh
scripts/run-soak.sh
```

`run-soak.sh` always executes the formal profile: 60 seconds warmup followed by steady, peak, saturation and recovery/activation stages of 900 seconds each. The test-only `MASI_PLUGIN_HOST_SOAK_QUICK=1` route writes `NOT_RUN/NOT_QUALIFIED` rehearsal evidence and is never accepted by the module summary.

The single completion authority is:

```bash
MASI_PLUGIN_HOST_RUN_ID=plugin-host-formal-YYYYMMDD-NNN \
scripts/run-module-gates.sh
```

It creates a new, non-reusable `evidence/module-gates/runs/<run-id>/`, writes a bounded log and schema-validated command sidecar for every command, validates all referenced digests, runs tamper negatives, publishes `latest.json` atomically and makes the run read-only. A failed or interrupted run is retained as partial evidence and cannot be renamed into a passing run.

## Process operation

Start the Host with one strict external configuration:

```bash
target/release/masi-plugin-host --config /absolute/path/plugin-host.json
```

The configured health listener exposes `/startupz`, `/readyz`, `/livez` and `/metrics`. The same binary provides the OCI semantic probe:

```bash
target/release/masi-plugin-host \
  --config /absolute/path/plugin-host.json \
  --probe ready
```

`SIGTERM` stops new admission, boundedly drains/cancels exact bindings and exits without changing canonical Manager facts. A Host crash loses only its bounded memory observation/queue; the Manager must replay the original durable binding/run identities. The black box proves the restarted Host begins deny-all and accepts work only after exact binding reconstruction.

## Evidence and qualification limits

The module evidence binds the source-tree/status digest, release binaries, OCI image/binary identity, Wasmtime/WIT/service profile, SPDX SBOMs, Trivy database/checks and results, Cosign positive/tamper/wrong-publisher results, coverage/Valgrind, a structured service/Wasm fault matrix, repeated min/typical/max performance trials, real over-capacity queue saturation, separate Wasm compile/cold-instantiate/warm execution, queue/in-flight/connection resource trends, formal soak, findings and requirement traceability. Conditional profiles are represented independently as `applicability=NOT_APPLICABLE, result=NOT_RUN`; they are never converted into PASS.

Operational Module Complete does not claim Plugin Platform pairwise/system/production qualification. In particular, Go/PostgreSQL canonical lifecycle and statistics persistence, official Analysis A2A/MCP, cross-host service mTLS, read-only external-source tools, core p99/RSS relative degradation, production HA and the protected release baseline remain in their own later scopes.
