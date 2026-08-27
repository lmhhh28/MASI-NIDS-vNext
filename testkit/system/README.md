# System startup and pairwise rehearsals

This directory contains executable integration infrastructure that starts real MASI-NIDS-vNext deployables. It never promotes fixture-assisted or disconnected module evidence into formal pairwise or Full System E2E qualification.

## Go/PostgreSQL/Web P9 rehearsal

```bash
testkit/system/run-web-control-pairwise.sh
```

The runner migrates a fresh PostgreSQL 18 test database, starts the real Control Core and production Web OCI images, then runs Chromium, Firefox, and WebKit as separate processes. Each engine verifies 25 authorized projections, opaque session/CSRF rules, one SPA SSE stream, and a target registration read back from PostgreSQL. It uses plaintext loopback `test-login`, so evidence is always `PASS/NOT_QUALIFIED`, never formal P9.

Port `4189` is intentional. Port `4190` is registered for ManageSieve and is blocked by Firefox/WebKit as an unsafe port.

## Go/PostgreSQL/Analysis P11 rehearsal

```bash
analysis-py/.venv/bin/python testkit/system/run-go-analysis-pairwise.py \
  --run-id <unique-run-id> --evidence <fresh-evidence-path>
```

This runner starts a real Control Core process, PostgreSQL, and the real Analysis process. It submits through the public Go session/CSRF API, accepts `succeeded` or valid `limited` terminal semantics, and requires one PostgreSQL Artifact that is non-executable and not deployment eligible. The internal A2A hop is loopback TLS 1.3 with mutual certificate authentication and exact SAN/server-name checks; external provider/MCP services are deterministic mTLS fixtures, so formal production identity and provider qualification remain `NOT_QUALIFIED`.

## Connected full-stack rehearsal

```bash
python3 testkit/system/run-edge-central-pairwise.py \
  --run-id <unique-run-id> \
  --evidence evidence/system-connected-full/<unique-run-id>/summary.json \
  --real-control --real-p4 --with-web --with-sideplanes
```

This runner keeps one PostgreSQL 18/Control topology while real BMv2, Edge, Central Gateway, pinned Triton/ORT CPU, Plugin Host/Wasm, Analysis, the production Web image, and Chromium/Firefox/WebKit execute their public boundaries. The running `control-core` maintenance dispatcher—not the seed test process—claims the durable statistics run over TLS 1.3 mTLS; Go persists the validated current/history Artifact, and all three browsers read it through the fixed Vue renderer. The same Control process calls the real Analysis process over A2A mTLS, persists a non-executable/non-deployable Artifact, and all three browsers read the task and Artifact.

The isolated one-shot pipeline loader closes before Edge becomes the long-lived P4Runtime owner, and the traffic sender has no P4Runtime credentials. External Analysis provider/MCP processes remain deterministic mTLS fixtures. The runner validates `connected-system-web-rehearsal/v1` plus cross-field identity, timeline, arithmetic, three-browser, cleanup, and secret-path semantics. Current source evidence is [`20260827T023500Z-rehearsal-005`](../../evidence/system-connected-full/20260827T023500Z-rehearsal-005/summary.json): operational rehearsal `PASS`, qualification `NOT_QUALIFIED`.

This is one connected happy-path rehearsal, not formal Full System E2E. It does not replace the twelve clean-environment pairwise runs, ten system waves, required traffic/fault/PITR/performance/soak matrices, protected baseline, hardware/HA qualification, or a real external provider. It must never be promoted beyond its recorded `REHEARSAL` scope.

## Full deployable-set startup rehearsal

```bash
testkit/system/run-full-startup-rehearsal.sh
```

It executes nine real runtime gates in bounded sequence: BMv2/P4, Edge OCI, Gateway plus pinned Triton/ORT, PostgreSQL plus Go plus Web, real Go-to-Analysis A2A, Plugin Host OCI, Host plus Wasm statistics, Analysis OCI, and Offline ML OCI. A component exit code `2` remains `HOLD` when its actual runtime checks passed but qualification-only gates remain.

That startup aggregate remains deliberately `HOLD/NOT_QUALIFIED` even when `operational_startup_result=PASS`, because its own participants were started as disconnected gates and its Go/Analysis identity is an isolated acceptance PKI. The later connected full-stack rehearsal above does not retroactively rewrite this historical evidence or replace the twelve formal pairwise boundaries and ten system waves.

Evidence is written to `evidence/system-startup-rehearsal/<run-id>/` and validated against `contracts/evidence/system-startup-rehearsal/v1/schema.json`.
