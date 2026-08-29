# MASI-NIDS-vNext

MASI-NIDS-vNext 是一个 greenfield、contract-first 的网络检测与受治理响应系统。唯一规范性需求基线是 [`docs/masi-nids-vnext-system-requirements-2026-08-09.md`](docs/masi-nids-vnext-system-requirements-2026-08-09.md)；本 README 与 [`REQUIREMENTS.md`](REQUIREMENTS.md) 只提供开发、构建和启动入口，不改变需求语义，也不授予部署或生产资格。

## 当前状态

- 仓库不是 production qualified。模块、pairwise、System E2E 与 production 是互不继承的证据范围。
- 当前可执行的是模块门禁、九组件 startup rehearsal、Go/PostgreSQL/Web rehearsal、Go/PostgreSQL/Analysis rehearsal，以及 fixture-assisted connected full-stack rehearsal。
- `deploy/` 目前只有 module、acceptance 和 rehearsal 资产；没有可称为生产全栈部署的 Compose、Helm 或 Kubernetes 清单。
- `legacy-reference/` 保存旧 MASI-NIDS 前端的只读参考快照；它不是第十个模块，也不进入当前 Vue SPA、OCI、CI 或运行依赖图。
- CI 绿色、容器 healthy、URL 可达、本地 rehearsal 或历史 evidence 都不能自动产生正式 PASS。

先阅读：

1. [`REQUIREMENTS.md`](REQUIREMENTS.md)：宿主依赖、精确工具链、初始化、端口、配置、模块命令和启动/停止流程；
2. [`docs/architecture/masi-nids-vnext-overall-architecture-2026-08-11.md`](docs/architecture/masi-nids-vnext-overall-architecture-2026-08-11.md)：运行拓扑与唯一所有权；
3. [`testkit/system/README.md`](testkit/system/README.md)：各 rehearsal 的真实边界和资格限制；
4. 各模块 README：模块专属 build、test、benchmark、OCI 与 formal gate 的唯一可复制命令来源。

## 仓库布局

| 目录 | 职责 |
|---|---|
| `contracts/` | Protobuf、OpenAPI、JSON Schema、WIT、profile 与 golden 的唯一源 |
| `p4/` | P4_16/BMv2 软件数据面 |
| `edge-rs/` | Rust Edge Agent 与每 target actor |
| `infer-cpp/` | C++ Gateway；执行面为 pinned Triton/ORT |
| `control-go/` | Go Control Core 与 API |
| `db/` | PostgreSQL schema、migration job、HA/PITR 门禁 |
| `plugin-host-rs/` | Rust Plugin Runtime Host |
| `analysis-py/` | Python Analysis Plugin |
| `ml-py/` | terminating Offline ML Artifact Pipeline |
| `web/` | Vue 3/Vite SOC SPA |
| `testkit/` | contract golden、fixture、fake、fault 与 rehearsal runner |
| `deploy/` | module/acceptance/rehearsal 部署资产 |
| `legacy-reference/` | 旧实现的完整性可验参考快照；禁止生产 build/runtime 依赖 |

## 旧前端参考快照

同级旧仓库 `../MASI-NIDS/AEE_cuda/nids-frontend` 的当前工作树已完整迁入 [`legacy-reference/masi-nids-frontend/`](legacy-reference/masi-nids-frontend/README.md)，包括 Next.js/React 源码、配置、lockfile、测试与审计脚本；`node_modules`、`.next`、coverage、test output、`.env*` 和其他 ignored/generated 文件均未迁入。快照附带源 revision、dirty status digest 和 236 个文件的 SHA-256 manifest。

该代码依赖旧 Event/Workflow/Review/P4/Admin/MCP API，并包含 Next server BFF、浏览器登录状态和旧授权语义，因此只可用于行为、数据与性能对照。当前 [`web/`](web/README.md) 仍是唯一 vNext Frontend：Vue 3/Vite 静态 SPA，只访问同源 Go `/api`、`/events`、`/oidc`。禁止从参考快照向 `web/` 直接 import、运行旧 Dockerfile/Next server、恢复旧 BFF，或把旧测试结果解释为 vNext PASS。

## 最短开发初始化

参考宿主是 Linux x86_64。正式 Compose runner 固定 Docker Engine `29.5.0`、API `1.54`、Docker Compose `5.1.3`；语言版本和额外系统工具见 [`REQUIREMENTS.md`](REQUIREMENTS.md#工具链与依赖)。

```bash
# 仓库级 Python 工具环境；不要把 PyYAML 额外装进模块 frozen venv
uv python install 3.12.13
uv venv --python 3.12.13 .venv
uv pip install --python .venv/bin/python \
  jsonschema==4.26.0 PyYAML==6.0.3

# Python 模块
(cd analysis-py && uv sync --frozen --python 3.12.13)
(cd ml-py && uv sync --frozen --extra dev --python 3.12.13)

# Web 与 OpenAPI TypeScript client generator
npm --prefix web ci --ignore-scripts --no-audit --no-fund
npm --prefix contracts/openapi/v1/typescript-client ci \
  --ignore-scripts --no-audit --no-fund

# Go 与 Rust dependency closure
(cd control-go && GOTOOLCHAIN=go1.26.6 go mod download)
(cd db && GOTOOLCHAIN=go1.26.6 go mod download)
(cd edge-rs && cargo fetch --locked)
(cd plugin-host-rs && cargo fetch --locked)
```

三浏览器测试还需要：

```bash
(cd web && npx playwright install --with-deps chromium firefox webkit)
```

C++ Gateway 的 gRPC/Protobuf 工具链和 P4 formal source archive 是额外步骤，不属于上述轻量初始化，详见 [`REQUIREMENTS.md`](REQUIREMENTS.md#一次性初始化)。

## 快速验证

GitHub-hosted quick CI 对应的本地快速检查：

```bash
.venv/bin/python scripts/ci/check_repository_hygiene.py --include-untracked
.venv/bin/python -m unittest discover -s scripts/ci -p 'test_*.py'
```

这只是快速静态/契约检查，不启动完整系统，也不是 Module、pairwise 或 System E2E PASS。模块级命令请直接使用对应 README：

上述 `unittest discover` 同时执行 legacy snapshot 的逐文件摘要、生成物/secret 排除、Docker context 和生产依赖零引用回归检查。

- [`p4/README.md`](p4/README.md)
- [`edge-rs/README.md`](edge-rs/README.md)
- [`infer-cpp/README.md`](infer-cpp/README.md)
- [`control-go/README.md`](control-go/README.md)
- [`db/README.md`](db/README.md)
- [`plugin-host-rs/README.md`](plugin-host-rs/README.md)
- [`analysis-py/README.md`](analysis-py/README.md)
- [`ml-py/README.md`](ml-py/README.md)
- [`web/README.md`](web/README.md)

## 推荐启动路径

所有命令均从仓库根目录运行。每次 runner 使用新的 run ID 和不存在的 evidence 路径；不要复用旧目录。

### 1. Go/PostgreSQL/Web 三浏览器 rehearsal

这是验证数据库迁移、真实 Control、production Web OCI 和三浏览器边界的最小自动入口：

```bash
GOTOOLCHAIN=go1.26.6 PATH="$PWD/.venv/bin:$PATH" \
MASI_P9_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-p9-local" \
  testkit/system/run-web-control-pairwise.sh
```

它使用 loopback plaintext、test login 和数据库名包含 `test` 的隔离 PostgreSQL；成功结果是 `PASS/NOT_QUALIFIED`，不是正式 P9 qualification。

### 2. 九组件真实 startup rehearsal

该入口依次启动 BMv2/P4、Edge OCI、Gateway+Triton/ORT CPU、PostgreSQL+Go+Web、Go↔Analysis、Plugin Host、Host↔Wasm、Analysis OCI 和 Offline ML OCI：

```bash
GOTOOLCHAIN=go1.26.6 PATH="$PWD/.venv/bin:$PATH" \
MASI_SYSTEM_STARTUP_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-startup-local" \
  testkit/system/run-full-startup-rehearsal.sh
```

即使九个 runtime gate 都通过，脚本也会有意以状态码 `2` 结束，并在 `evidence/system-startup-rehearsal/<run-id>/summary.json` 记录 `operational_startup_result=PASS`、`result=HOLD`、`qualification=NOT_QUALIFIED`。状态码 `2` 不是 production PASS，也不应被包装为普通成功；以结构化 summary 为准。

### 3. Connected full-stack happy-path rehearsal

```bash
run_id="$(date -u +%Y%m%dT%H%M%SZ)-connected-local"
GOTOOLCHAIN=go1.26.6 PATH="$PWD/.venv/bin:$PATH" \
.venv/bin/python testkit/system/run-edge-central-pairwise.py \
  --run-id "$run_id" \
  --evidence "evidence/system-connected-full/$run_id/summary.json" \
  --real-control --real-p4 --with-web --with-sideplanes
```

该 runner 在同一 PostgreSQL/Control 拓扑中连接真实 BMv2、Edge、Gateway、pinned Triton/ORT CPU、Plugin Host/Wasm、Analysis、production Web 和三浏览器，并执行精确清理。它仍是 fixture-assisted happy path，不替代十二个正式 pairwise、十个 system wave、fault/PITR/performance/soak、真实 provider、硬件或 HA 资格。

### 4. 手工 Web 开发

仅用于 loopback test profile；不要暴露到生产网络。

```bash
# 终端 1：测试数据库、独立 migration、Control
cd control-go
docker compose -f testdata/compose.e2e.yaml up -d --wait
GOTOOLCHAIN=go1.26.6 go run ./cmd/migrate-test \
  --dsn 'postgres://masi:masi@127.0.0.1:55433/masi_control_test?sslmode=disable' \
  --dir ../db/migrations
GOTOOLCHAIN=go1.26.6 go run ./cmd/control-core --config testdata/control-e2e-config.json
```

```bash
# 终端 2：Vite，同源代理到 127.0.0.1:18080
cd web
npm run dev
```

停止两个前台进程后，精确清理测试数据库：

```bash
cd control-go
docker compose -f testdata/compose.e2e.yaml down --volumes --remove-orphans
```

## 生产部署边界

当前仓库没有可以安全照抄执行的生产全栈启动命令。生产至少还要求 `production-ha` deployment tier、PostgreSQL HA/PITR、真实 OIDC/CSRF、全链 mTLS、不可变镜像与模型 repository、Go 的真实 Edge/deployment/plugin/Analysis adapters、同 profile Central Inference 跨故障域 N+1、目标硬件绝对容量，以及十二 pairwise/十 system wave/Full E2E 的 exact evidence。不得把 test Compose、acceptance Compose、模块 OCI smoke 或 rehearsal 改名为生产部署。

## 清理与安全

- migration 永远由独立 job/binary 执行；应用启动不自动迁移。
- 测试数据库名必须包含 `test`；破坏性数据库测试必须显式确认精确目标。
- P4/Mininet/PTF 需要专用测试宿主或隔离 VM/namespace、最小 `NET_ADMIN`/`NET_RAW` 权限和精确 cleanup；不得接触生产接口或 credential。
- secret 只经 secret file、secret manager 或 workload identity 注入，禁止进入仓库、镜像、CLI、日志、前端或 Artifact。
- 不使用宽泛 `docker system prune`、wildcard namespace/qdisc 删除或共享数据库清理。优先让 runner 的 trap 和 cleanup validator 收敛其 exact-owned 资源。

更多依赖、端口、预计时长、退出码与已知缺口见 [`REQUIREMENTS.md`](REQUIREMENTS.md)。
