# PostgreSQL State（`MOD-DB-001`）

本目录是 MASI-NIDS-vNext 的独立 PostgreSQL State 模块。PostgreSQL 是核心持久事实的唯一来源；Go Control 是核心 schema 的唯一业务写入者。migration 只由独立 `masi-dbctl` job 执行，应用实例不会在启动时抢跑 migration。Rust Edge、Central Inference、Plugin Host、通用插件和 Web 均不得获得核心数据库凭据；Analysis Plugin 只能使用隔离的自有 schema/role。

## 当前状态与完成判定

按 `DEC-044`，operational Module Complete 只由 `scripts/run-module-gates.sh` 的同一次 formal run 机器派生。[`evidence/module-gates/latest.json`](evidence/module-gates/latest.json) 只是“最近发布的 immutable 历史 run”指针；只有其 source revision/tree/status digest 经根 verifier 与当前源码完全匹配时，所指 `gate-summary.json#overall_module_complete` 才对当前源码权威。仓库当前跟踪的 v21/29-file 指针明确是历史证据，不能替代当前 v22/31-file 门禁；历史 rehearsal、单项 PASS、readiness、mock 或手工摘要同样不能替代它。

`overall_module_complete=true` 与资格状态正交。本模块的 acceptance scope 固定为 `operational-single-domain/v1`：一个物理主机上的 PostgreSQL 18.6 primary、异步物理 standby、隔离 PITR restore 与 PgBouncer transaction pool。完整 operational 门禁可以通过，而 `result=HOLD`、`qualification=NOT_QUALIFIED` 继续保留，原因是 `DEC-001` 绝对生产性能/RPO/RTO 尚未由 Owner 冻结、受保护 release baseline 尚未形成，并且单故障域手工 promotion 不是 `production-ha` 自动故障转移。正式 pairwise/system 也尚未开始，不得从本模块结论外推。

## 所有权与数据边界

- `db/migrations/` 是精简 vNext schema 的 append-only migration chain；已应用 migration 不得修改，生产演进使用 expand/contract。
- `masi-dbctl` 负责 migration、catalog/role/pool/application/recovery/replica readback，以及 PITR 后 target/model incarnation 的原子轮换。它只从有界、非 symlink secret file 读取 DSN。
- `masi-dbload` 只用于隔离模块门禁，覆盖 exact-schema 的 0/128/1,024/4,096-rule capacity matrix 与四阶段有界 soak；它拒绝数据库名、TLS 或模式不符。
- 核心事实的关系、generation/incarnation fence、CAS、分区、retention 和 legal hold 由数据库约束/函数强制；恢复不会自动打开 target/model writer，也不会生成 effect intent 或 plugin statistic run。
- PgBouncer 只使用 transaction pooling；migration、复制、备份、LISTEN 和 session-state workload 不经过它。
- 跨容器连接强制 TLS 1.3、双向证书校验和 SCRAM。运行期登录角色按最小权限封存；bootstrap 被设为 `NOLOGIN`，migration 账号不保留 `CREATEROLE`、`CREATEDB`、`pg_monitor` 或 Analysis schema 权限。
- PostgreSQL、PgBouncer、Go toolchain 与 hardened `gosu` 均由 exact digest/source lock 固定；三个候选 OCI 都必须通过 SBOM、漏洞、secret 与 source-registry closure 门禁。

## 可复制的语言级检查

模块工具使用 Go 1.26.6。完整门禁会从 [`deploy/postgresql-state/toolchain/go-linux-amd64.lock.json`](../deploy/postgresql-state/toolchain/go-linux-amd64.lock.json) 校验并准备 exact toolchain。已安装该版本时，可从仓库根目录执行：

```sh
cd db
test -z "$(gofmt -l ./cmd ./internal ./tests)"
go vet ./...
staticcheck ./...
govulncheck ./...
go test -count=1 ./...
go test -race -count=1 ./...
go test -covermode=atomic -coverprofile=coverage.out ./...
go test -run '^$' -bench BenchmarkLoadChain -benchmem ./internal/migrate
python3 scripts/test_evidence_tools.py
ruff check scripts
shellcheck scripts/*.sh ../deploy/postgresql-state/postgres/init/001-create-login-roles.sh
```

真实 PostgreSQL 黑盒测试要求一个数据库名明确包含 `test` 的 PostgreSQL 18 TLS 实例，并通过普通文件提供管理 DSN：

```sh
cd db
MASI_DB_BLACKBOX_REQUIRED=1 \
MASI_DB_TEST_REQUIRE_TLS=1 \
MASI_DB_TEST_ADMIN_DSN_FILE=/run/secrets/postgresql-test-admin-dsn \
go test -count=1 -v ./tests/blackbox
```

## 完整模块门禁

正式门禁会自行生成短期 CA/leaf/secret，构建 fresh run-specific binary 与三个 OCI，真实启动 PostgreSQL 18.6、PgBouncer 1.25.2、physical standby 和隔离 PITR restore，并依次验证：

- fresh/repeat/previous-to-current migration、checksum drift 与原子失败重试；
- exact FK/CAS、角色封存、TLS/plaintext 拒绝、分区、retention、statement/lock timeout 与真实 deadlock recovery；
- 256 MiB fault-only PGDATA 的 `SQLSTATE 53100` disk-full containment/readback，以及 28 个长事务占满 PgBouncer 后 `query_wait_timeout` 的有界拒绝与恢复；
- `pg_basebackup` + `pg_verifybackup`、连续 WAL archive、named restore point、PITR include/exclude、恢复事件与 incarnation fence；
- primary 强杀、physical streaming readback、standby promotion、timeline 前进和 writer 保持关闭；
- 0/128/1,024/4,096 rule facts、PgBouncer transaction pooling、四阶段 60 秒 warmup + 3,600 秒 formal soak、CPU/RSS/FD/thread/OOM/restart 采样；
- non-root/read-only OCI、SBOM、reachable vulnerability、secret 与 supply-chain closure；
- source/status digest 不漂移、环境与 volume 完整清理、64 个精确需求 ID 的 evidence/command/artifact digest 追踪、无 live implementation sentinel 和 open P0=0。

```sh
cd db
MASI_DB_FORMAL_SOAK=1 \
MASI_DB_GATE_RUN_ID="db-formal-$(date -u +%Y%m%dT%H%M%SZ)" \
scripts/run-module-gates.sh
```

正式 workload 约 61 分钟，完整门禁还包含构建、扫描、PITR 与 failover。成功时脚本返回 `0` 并原子发布 `gate-summary.json` 与 `latest.json`；测试、证据、启动、恢复或供应链失败返回非零；非法 run ID/调用模式返回 `64`。资格 summary 保持 `HOLD/NOT_QUALIFIED` 不会把已执行的 operational PASS 改写成 qualification PASS。

仅验证编排机制的短跑必须显式标记 rehearsal：

```sh
cd db
MASI_DB_REHEARSAL=1 \
MASI_DB_GATE_RUN_ID="db-rehearsal-$(date -u +%Y%m%dT%H%M%SZ)" \
scripts/run-module-gates.sh
```

rehearsal 使用 4 × 5 秒 workload，只能输出 `REHEARSAL/NOT_QUALIFIED` 过程证据，不生成 Module Complete summary。

## 证据与资格范围

每个 run 写入 `evidence/module-gates/runs/<run-id>/`，命令 sidecar 绑定 exact argv、exit code、时间、source-tree/status digest 与日志 digest。最终 summary 会独立重验这些 sidecar，并绑定当前源码计算出的 31-file migration chain、0029 hardening、0030 DEFAULT-partition drain 与 0031 zero-digest rejection、需求基线、traceability/findings、database/role/pool/application readback、capacity/soak/recovery 原始制品、三个 OCI 的 exact image ID，以及三份非空 SBOM、四份零阻断 finding scan、Trivy database freshness 和全部供应链原始制品 digest。OCI 与供应链 evidence 分别由 `postgresql-state-oci/v1`、`postgresql-state-supply/v1` 公共 schema 约束。仓库内旧 formal evidence 只绑定其记录的历史 source/29-file chain，不能继承到当前 v22/31-file 源码。

以下范围保持独立，不由本模块 acceptance evidence 继承：

- `production-ha/v1`、automatic failover、跨故障域或跨地域 WAL archive：`NOT_APPLICABLE/NOT_RUN` 于当前 single-domain scope；
- Owner 未冻结的绝对 latency/throughput/RPO/RTO：`HOLD/NOT_QUALIFIED`；
- 正式 Go↔PostgreSQL pairwise、系统波次、production deployment：尚未执行，不宣称 PASS；
- dirty-tree formal run 只能绑定实际被测源码，不能替代受保护 commit/tag 与签名 provenance。
