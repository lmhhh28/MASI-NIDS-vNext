# Go Control Core (`MOD-CTRL-001`)

Go Control is the sole business writer for the core PostgreSQL facts and exposes the same-origin HTTP/SSE API plus the Edge-to-Control gRPC sink. It does not run migrations at application startup and never waits for Edge, P4, OIDC, MCP, A2A, or deployment calls while holding a database transaction.

The current implementation is a Module-Complete candidate, not a `MODULE PASS` or production-qualified release. The local process test below is an explicit `test/test-fake` rehearsal. Full fault, performance, soak, OCI, mTLS/OIDC, PostgreSQL HA/PITR, pairwise, and system evidence is still required by `TEST-GO-CTRL-001`, `TEST-TEL-INF-001`, `TEST-P4-FW-001`, `TEST-TARGET-FLEET-001`, and `TEST-PLUGIN-STAT-001`.

## Language-level checks

From `control-go/`:

```sh
gofmt -w ./cmd ./internal ./tests
go vet ./...
staticcheck ./...
go test ./...
go test -race ./...
go test -coverprofile=coverage.out ./...
go test ./internal/ruleobs -run TestGoldenRuleFormula
go test ./internal/pluginstat -run TestGoldenDefinitionFieldsMatchContract
```

## Real-process PostgreSQL rehearsal

The test runner refuses a database whose name does not contain `test`.

```sh
cd control-go
docker compose -f testdata/compose.e2e.yaml up -d --wait
go run ./cmd/migrate-test \
  --dsn 'postgres://masi:masi@127.0.0.1:55433/masi_control_test?sslmode=disable' \
  --dir ../db/migrations
go build -o /tmp/masi-control-e2e ./cmd/control-core
MASI_CONTROL_E2E_DSN='postgres://masi:masi@127.0.0.1:55433/masi_control_test?sslmode=disable' \
MASI_CONTROL_E2E_BINARY=/tmp/masi-control-e2e \
MASI_CONTROL_E2E_CONFIG="$PWD/testdata/control-e2e-config.json" \
MASI_CONTROL_E2E_REQUIRED=1 \
go test -v ./tests/process_e2e -run TestRealControlProcessCommitResults
MASI_CONTROL_E2E_DSN='postgres://masi:masi@127.0.0.1:55433/masi_control_test?sslmode=disable' \
MASI_CONTROL_E2E_CONFIG="$PWD/testdata/control-e2e-config.json" \
MASI_CONTROL_E2E_REQUIRED=1 \
go test -v ./internal/plugin -run TestPluginAuditLifecyclePostgres
docker compose -f testdata/compose.e2e.yaml down -v
```

The process E2E launches the actual `control-core` binary, waits on `/readyz`, calls `ControlSink.CommitResults` over the public test-profile gRPC boundary, verifies committed/idempotent/conflict ACKs, checks PostgreSQL as the outcome oracle, and confirms bounded graceful shutdown. The plugin PostgreSQL E2E verifies atomic structured audit records, `active → drain → revoked`, refusal to reuse a pre-revocation qualification, and reactivation only after a new qualification.

## Runtime profiles

- `test` + `test-fake`: loopback plaintext only, and the PostgreSQL database name must contain `test`.
- `production` + `production-mtls`: HTTPS, gRPC TLS 1.3 with required client certificates, OIDC, and a digest-pinned role/scope mapping are mandatory. This build refuses production startup until real outbound Edge/deployment mTLS adapters are configured; test fakes cannot silently enter production.

## OCI

```sh
docker build -t masi-control-core:dev .
docker run --rm masi-control-core:dev --help
```

Production startup additionally requires a config and read-only certificate/secret mounts. OCI startup/readiness/liveness evidence must record the exact image digest and selected deployment/availability profile.
