# MASI-NIDS-vNext SOC Web

`web/` is the independent Vue 3/Vite SOC SPA (`MOD-WEB-001`). It serves one immutable static artifact and talks only to same-origin Go Control routes under `/api`, `/events`, and `/oidc`. It contains no Node production runtime, browser token store, service worker, plugin-supplied UI, or alternate business API client.

## Toolchain

- Node.js `22.22.3`, npm `10.9.8`
- Vue `3.5.41`, TypeScript `5.9.3`, Vite `8.2.2`
- Playwright `1.62.1` with the exact Chromium, Firefox, and WebKit revisions in `contracts/profiles/v1/web-browser.json`
- Docker/BuildKit for the digest-pinned non-root NGINX OCI

Install the exact lockfile graph:

```bash
cd web
npm ci --ignore-scripts --no-audit --no-fund
```

## Build and language gates

```bash
npm run verify:contracts
npm run verify:sentinels
npm run lint
npm run typecheck
npm run test
npm run build
npm run verify:bundle
npm run verify:security
npm run sbom
npm run verify:supply
npm audit --omit=dev --audit-level=high
```

`npm run test` runs Vitest unit, component, runtime-contract, hostile-payload, and axe checks. The production build has content-hashed route chunks and no source maps. Bundle limits and browser performance limits come from `web-performance/v1`.

Automated axe coverage does not replace the required human accessibility gate. For the exact candidate source/image pair, a reviewer must exercise screen-reader navigation, full keyboard operation, 200% zoom/reflow, and high-risk dialog focus. The input must validate against `contracts/evidence/web-manual-accessibility/v1/schema.json`, identify the reviewer, assistive technology and platform, record all four flows as `PASS`, and bind four or more digest-addressed review artifacts.

Validate and normalize that review without running the full module gate:

```bash
node scripts/validate-manual-accessibility.mjs \
  --input /absolute/protected/manual-accessibility.json \
  --output /tmp/manual-accessibility.validated.json \
  --source-tree-digest sha256:<exact-source-digest> \
  --image-digest sha256:<exact-image-digest>
```

Do not fabricate this input. If it is absent, the validator emits `NOT_RUN/NOT_QUALIFIED` and the Web module remains operationally incomplete.

## Browser and OCI black box

The OCI smoke builds the real production artifact, starts it with a read-only root filesystem as UID `101:101`, starts a bounded contract fake only for its Control neighbor, and runs all 27 flows in the three pinned browser engines:

```bash
scripts/run-oci-smoke.sh
```

That command proves the Web module boundary only. Its fake neighbor is explicitly `NOT_QUALIFIED` for formal Go↔Web pairwise or system E2E.

For local development against the real test-profile Go process on `127.0.0.1:18080`:

```bash
npm run dev
```

The Vite proxy remains same-origin from the browser's perspective. Do not expose this plaintext test profile as production.

## Performance and soak

With a production Web OCI running at `http://127.0.0.1:4180`:

```bash
node scripts/run-performance.mjs --base-url http://127.0.0.1:4180
node scripts/run-soak.mjs --base-url http://127.0.0.1:4180 --warmup-seconds 60 --phase-seconds 900
```

The formal soak excludes a 60-second warmup, then measures four 900-second phases (steady, peak, saturation, recovery). Shorter durations are marked `REHEARSAL_SHORT_DURATION` and cannot satisfy Module Complete.

## Reproducible module gate

The stable full gate takes a little over one hour because it genuinely executes the formal soak:

```bash
MASI_WEB_FORMAL_SOAK=1 \
MASI_WEB_MANUAL_A11Y_EVIDENCE=/absolute/protected/manual-accessibility.json \
scripts/run-module-gates.sh
```

Evidence is published under `web/evidence/module-gates/runs/<run-id>/`; `gate-summary.json` and `latest.json` are immutable-run pointers. The manual review must bind the reproducibly rebuilt image and source digests exactly. DEC-044 operational completion remains separate from protected-baseline, formal pairwise, system, and production qualification.

## Architecture and evidence

- Design: `docs/design/modules/web-soc-spa-design.md`
- Requirements baseline: `docs/masi-nids-vnext-system-requirements-2026-08-09.md`
- Traceability: `web/requirements-traceability.json`
- Findings: `web/module-findings.json`
- Public contracts: `contracts/web/v1`, `contracts/openapi/v1`, and the generated TypeScript client under `contracts/generated/typescript/control-api`
