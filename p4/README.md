# MASI-NIDS vNext P4 Switch

This module is the BMv2 `simple_switch_grpc`/v1model software target for
`p4-stateless-firewall/v1`. It implements the P4-only ownership defined by
`ADR-0012`, `ADR-0013`, and `ADR-0014`; it does not contain an Edge Agent,
policy authorization, TTL scheduler, durable queue, or canonical database.

## Qualification status

The operational status is derived only from the newest immutable
`evidence/p4-switch/<run-id>/qualification-evidence.json` produced by the complete
formal command below. A `PASS / QUALIFIED` record with no remaining holds is
Module evidence only for its exact claim scope when it also records
`overall_module_complete=true`, `findings.open_p0=0`, and every `completion`
predicate as true. If the current P4 source, contracts, deployment
configuration, runtime, runner, findings registry, or compiled artifacts no
longer match the recorded digests, the claim is invalid until the complete gate
is rerun. Prose in this README never overrides that machine-readable result.

The P4Runtime 1.4.1 candidate runtime is
`sha256:24669f08df3583b0d170226f6969f1090235604ef6d5bb5e2b2bb06062d9a571`
and runner
`sha256:f0b02a81a6fc7e13b3d10695311663eb54aaf25e4487920e7de93b3b211d42dc`.
It contains PI commit `577b502da91b7d17e4be5821d49d481dc2e1bb7a`
with the P4Runtime `v1.4.1` proto submodule and the project-qualified exact
Capabilities patch. The historical formal run
[`20260813T060546Z-runner-version-formal-006`](../evidence/p4-switch/20260813T060546Z-runner-version-formal-006/qualification-evidence.json)
remains immutable historical evidence for its older runtime and is not evidence
for this candidate.

The runner's public execution-boundary readback and SPDX both match Alpine
Linux 3.24.1, Tcpreplay 4.5.2/4.5.2-r1, and iproute2 7.0.0/7.0.0-r0 exactly.
The closure is recorded in
[`ISSUE-P4-SW-001`](../docs/issues/ISSUE-P4-SW-001-module-complete-holds.md).
This is not production, hardware, IPv6 effect, stateful, NAT, rate-limit, or
gNMI qualification. Formal pairwise and system integration remain globally
gated until all other required initial modules reach Module Complete.

## Data-plane behavior

- exact IPv4 five-tuple response overlay, with `drop` and
  `permit-and-continue` actions;
- two 4,096-entry baseline banks and a one-entry selector;
- explicit per-bank default action;
- per-rule direct packet/byte counters and separate eligible counters;
- two banks of 256 bounded aggregate telemetry cells, bank epoch, and a
  monotonic sequence used for frozen-bank snapshot verification;
- best-effort Digest and PacketIn hints every 1,024 packets;
- exact-MAC L2 forwarding used only by the isolated packet oracle.

IPv4 options, checksum errors, parser errors, and truncated headers fail
closed. IPv6, stateful inspection, NAT, rate limiting, and hardware targets are
outside this software profile.

## Reproducible commands

Compile with the digest-pinned, network-isolated p4c container:

```sh
./p4/scripts/compile.sh
```

Run schema/golden/unit checks:

```sh
python3 -m unittest discover -s testkit/p4_switch/tests -p 'test_*.py' -v
```

Run the pinned, isolated, mTLS-enabled BMv2 module E2E as root. It includes a
real two-host Mininet topology, PTF packet oracles, crash recovery, security
checks, and the 0/128/1,024/4,096 rule matrix:

```sh
sudo ./testkit/p4_switch/run-module-e2e.sh
```

To reproduce the formal exact-image flow, first provide both recursive source
archives named and hashed by `deploy/p4-switch/bmv2/source.lock.json` and
`deploy/p4-switch/pi/source.lock.json`, then run:

```sh
sudo env \
  MASI_P4_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-module-formal" \
  MASI_P4_QUALIFICATION_MODE=formal \
  MASI_BMV2_SOURCE_ARCHIVE=/tmp/behavioral-model-693e69e634fedf007964a069fdd69acc441a5f80.tar.gz \
  MASI_PI_SOURCE_ARCHIVE=/tmp/PI-577b502da91b7d17e4be5821d49d481dc2e1bb7a-p4runtime-v1.4.1.tar.gz \
  MASI_TRIVY_CACHE="$PWD/out/supply-chain/trivy-cache" \
  MASI_COSIGN_KEY_DIR="$PWD/.masi-secrets/cosign" \
  MASI_P4_RUNTIME_IMAGE=masi-nids/p4-switch-runtime@sha256:24669f08df3583b0d170226f6969f1090235604ef6d5bb5e2b2bb06062d9a571 \
  MASI_P4_RUNNER_IMAGE=masi-nids/p4-switch-e2e-runner@sha256:f0b02a81a6fc7e13b3d10695311663eb54aaf25e4487920e7de93b3b211d42dc \
  ./testkit/p4_switch/run-module-e2e.sh
```

The runner writes machine-readable evidence under `evidence/p4-switch/`.
Container health is only a startup prerequisite; only the PTF result and the
validated evidence document determine the run result.
Exit status `1` means a test or evidence failure, `2` means the executed tests
completed but at least one required gate remains `HOLD/NOT_RUN`, and `0` is
reserved for a fully qualified result.

## Benchmark and qualification semantics

The E2E records compile, bounded-write, complete readback, selector-flip, and
packet-oracle latencies for every required rule count. It additionally checks
priority/conflict/shadow behavior, partial inactive-bank readback, selector
response loss and read-only reconciliation, P4Info/pipeline drift, direct
counter wrap/reset, frozen aggregate read/clear, best-effort hint loss,
host-filter contamination, link/netem recovery, and SIGKILL replay.

The Owner-frozen software reference profile defines absolute latency,
throughput, resource, and 3,600-second soak thresholds. The qualification runner
uses the project-patched, digest-pinned BMv2 runtime and requires ten clean
SIGINT plus ten clean SIGTERM lifecycle cycles, each followed by an mTLS
P4Runtime pipeline/readback and packet oracle. A short run is always recorded as
`REHEARSAL/NOT_QUALIFIED`; only the formal command may produce Module evidence.
The authoritative run passed benchmark, soak, lifecycle, and signed supply-chain
phases in one exact scope; later changes invalidate that claim until the complete
gate is rerun.

The tracked remediation and closure criteria are recorded in
[`ISSUE-P4-SW-001`](../docs/issues/ISSUE-P4-SW-001-module-complete-holds.md).
