# Go Control Core (`MOD-CTRL-001`)

Go Control is the sole business writer for the core PostgreSQL facts and exposes the same-origin HTTP/SSE API plus the Edge-to-Control gRPC sink. It does not run migrations at application startup and never waits for Edge, P4, OIDC, MCP, A2A, or deployment calls while holding a database transaction.

The module gate (`scripts/run-module-gates.sh`) produces the Module-Complete candidate evidence: the real PostgreSQL public-boundary E2E, the eight internal-package postgres E2E suites, the OCI smoke and the DEC-044 3600-second formal soak all execute against the real `control-core` binary, and the gate summary records `overall_module_complete=true` with `result=HOLD` / `qualification=NOT_QUALIFIED`. The HOLD is deliberate: control-core process thresholds are `OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001`, so qualification stays `NOT_QUALIFIED` until the Owner freezes the absolute thresholds. Formal pairwise/system integration, PostgreSQL HA/PITR and production mTLS/OIDC qualification remain outside this independent-module phase (`TEST-GO-CTRL-001`, `TEST-TEL-INF-001`, `TEST-P4-FW-001`, `TEST-TARGET-FLEET-001`, `TEST-PLUGIN-STAT-001`).

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

## 可复制门禁

```sh
cd control-go
scripts/run-module-gates.sh
```

脚本退出码以 operational completion 为准：全部完成条件通过（含 formal soak）时返回 `0`，即使资格 summary 因 DEC-001 绝对门槛未冻结仍为 `HOLD/NOT_QUALIFIED`；无硬失败但真实启动/必需测试未运行或 formal soak 未请求时返回 `2`；测试或证据绑定硬失败返回 `1`；非法 run-id 或证据基目录返回 `64`。`MASI_CONTROL_SKIP_OCI=1` 只用于显式诊断，runner 会生成结构化 `NOT_RUN` command/evidence，而不是伪造 PASS。

### 正式 soak

```sh
cd control-go
# 正式窗口（负载约 61 分钟；完整门禁另含构建/扫描/OCI/E2E）
MASI_CONTROL_FORMAL_SOAK=1 MASI_CONTROL_SOAK_SECONDS=3600 scripts/run-module-gates.sh
```

control-core 的进程资源门槛为 `OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001`：完整 3600 秒 formal soak 执行后仍只能 `MODULE/HOLD/NOT_QUALIFIED`，直到 Owner 冻结绝对门槛才可能 `PASS/QUALIFIED`。`MASI_CONTROL_SOAK_SECONDS<3600` 只产出 `REHEARSAL/HOLD/NOT_QUALIFIED`，不构成 Module Complete。证据写入 `control-go/evidence/module-gates/runs/<run-id>/` 并发布 `gate-summary.json` + `latest.json`。

### 环境变量

- `MASI_CONTROL_GATE_RUN_ID` — 覆盖 run id（须匹配 `^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$`）。
- `MASI_CONTROL_EVIDENCE_DIR` — 证据输出根目录（默认 `control-go/evidence/module-gates`）。
- `MASI_CONTROL_FORMAL_SOAK=1` — 启用 formal soak 子门禁。
- `MASI_CONTROL_SOAK_SECONDS` — soak 窗口秒数（默认 3600；小于 3600 为 rehearsal）。
- `MASI_CONTROL_SKIP_OCI=1` — 跳过 OCI 子门禁（结构化 `NOT_RUN`，不伪造 PASS）。
- `MASI_CONTROL_E2E_DSN` / `MASI_CONTROL_E2E_CONFIG` / `MASI_CONTROL_E2E_BINARY` — 真实 PostgreSQL DSN、配置路径与 `control-core` binary。
- `MASI_CONTROL_COMMAND_TIMEOUT_SECONDS` / `MASI_CONTROL_MAX_LOG_BYTES` — 单命令超时（默认 7200s）与单日志字节上限（默认 16MiB）。

## Runtime profiles

- `test` + `test-fake`: loopback plaintext only, and the PostgreSQL database name must contain `test`.
- `production` + `production-mtls`: HTTPS, gRPC TLS 1.3 with required client certificates, OIDC, and a digest-pinned role/scope mapping are mandatory. This build refuses production startup until real outbound Edge/deployment mTLS adapters are configured; test fakes cannot silently enter production.

## OCI

```sh
docker build -t masi-control-core:dev .
docker run --rm masi-control-core:dev --help
```

Production startup additionally requires a config and read-only certificate/secret mounts. OCI startup/readiness/liveness evidence must record the exact image digest and selected deployment/availability profile.
