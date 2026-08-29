# MASI-NIDS-vNext 开发与启动要求

- 文档用途：从干净 Linux 主机开发、构建、测试和启动当前仓库的依赖与步骤清单
- 更新日期：2026-08-30
- 需求锚点：`BASE-001`、`DB-004`、`DEP-001` 至 `DEP-005`、`TEST-002`、`TEST-GATE-001`、`TEST-REAL-E2E-001`
- 规范性基线：[`docs/masi-nids-vnext-system-requirements-2026-08-09.md`](docs/masi-nids-vnext-system-requirements-2026-08-09.md)

本文不是第二份产品需求基线。它只把当前 source、lockfile、profile、Dockerfile、CI、module README 和 runner 中已经存在的开发/启动要求汇总为操作入口；冲突时始终以规范性需求基线、accepted ADR、公开 contract/profile 和模块实现为准。本文存在不表示命令已经在任意主机执行 PASS。

## 支持的执行范围

| 目的 | 入口 | 实际启动范围 | 证据上限 |
|---|---|---|---|
| 快速静态检查 | `.github/workflows/ci.yml`、根 README 的快速命令 | 不启动真实 runtime | 不产生 runtime qualification evidence |
| 单模块开发 | 各模块 README 的分项命令 | 目标模块；邻居可为 contract fake | 由实际 evidence 决定 |
| 单模块完整门禁 | 各模块 `scripts/run-module-gates.*` | 真实 binary/OCI 与实际 runtime | Module scope；不外推 pairwise/system |
| Go/PostgreSQL/Web | `testkit/system/run-web-control-pairwise.sh` | 真实 PG、Control、Web、三浏览器 | loopback test rehearsal |
| Go/PostgreSQL/Analysis | `testkit/system/run-go-analysis-pairwise.py` | 真实 PG、Control、Analysis；provider/MCP fixture | mTLS boundary rehearsal |
| 九组件启动 | `testkit/system/run-full-startup-rehearsal.sh` | 九个真实 runtime gate，部分彼此隔离 | `HOLD/NOT_QUALIFIED` |
| Connected full stack | `testkit/system/run-edge-central-pairwise.py` | 一套真实 happy-path 拓扑，外部 provider/MCP 为 fixture | `REHEARSAL/NOT_QUALIFIED` |
| 正式 pairwise/System E2E | 需求基线定义的十二边界、十波次 | 干净环境、参与侧全部真实启动 | 当前没有统一可执行生产入口 |
| Production | `production-ha` exact deployment profile | HA/PITR、真实身份、全链 mTLS、硬件与绝对容量 | 当前仓库未提供完整部署资产 |

`operational_startup_result=PASS`、Module Complete、`result=PASS` 与 `qualification=QUALIFIED` 是不同概念。任何结论必须保留 evidence 中独立的 `level`、`applicability`、`result`、`qualification` 和 exact claim scope。

## 工具链与依赖

### 宿主与容器基线

默认开发宿主必须是 Linux x86_64，并具备：

- Git、Bash、GNU coreutils（含 `timeout`、`sha256sum`、`setsid`）、`find`、`sed`、`grep`、`tar`、`realpath`；
- `curl`、`jq`、OpenSSL、CA certificates；
- Docker daemon、BuildKit/buildx 和 Compose plugin；当前 `e2e-runner-compose/v1` 固定 Docker Engine `29.5.0`、API `1.54`、Compose `5.1.3`；
- 能拉取或从受控离线 cache 提供所有 digest-pinned OCI image；production runtime 禁止联网下载依赖；
- 充足磁盘、内存、CPU、PID/FD 和 inode。P4 formal supply-chain profile单独要求至少 `32 GiB` 可用空间；
- 正确时间、可创建临时目录、可绑定 loopback 端口，以及对 Docker daemon 的授权访问。

GitHub quick CI 的宿主参考是 Ubuntu `24.04`。Inference builder 自身固定 Ubuntu `22.04`/GCC `11.4.0`/CMake `3.22.1`；P4 runner 在 pinned Alpine `3.24.1` 容器内运行。不要把任意发行版默认包版本解释为资格 profile。

### 精确工具链矩阵

| 范围 | 精确版本/约束 | 权威来源 |
|---|---|---|
| Docker E2E runner | Engine `29.5.0`、API `1.54`、Compose `5.1.3` | `contracts/profiles/v1/e2e-runner-compose.json` |
| Python modules | CPython `3.12.13`，项目约束 `>=3.12,<3.13` | `analysis-py/pyproject.toml`、`ml-py/pyproject.toml`、runtime profiles |
| Python tooling | `uv 0.12.3`、Ruff `0.15.17`、Pyright `1.1.409` | CI 与 module README |
| Python tests | 只允许 `unittest`；禁止引入 pytest | `TEST-002`、两个 Python README |
| Rust | stable `1.97.1`；Edge/Host 分别由 `rust-toolchain.toml` 固定 | `edge-rs/`、`plugin-host-rs/` |
| Edge deep checks | nightly commit `c98d0cb27cc63afdd62602a52eb4feb8a1c682dd` | `edge-rs/scripts/run-deep-checks.sh` |
| Go | Control/DB formal 使用 `1.26.6` | `control-go/go.mod`、DB toolchain lock/README |
| Node/npm | Node `22.22.3`、npm `10.9.8` | `web/package.json` |
| Browser driver | Playwright `1.62.1`；pinned Chromium/Firefox/WebKit revisions | `contracts/profiles/v1/web-browser.json` |
| C++ | C++20、CMake `>=3.22`；registered GCC `11.4.0` | `infer-cpp/CMakeLists.txt`、Dockerfile |
| Gateway gRPC | gRPC `1.82.1` commit `acccf84c0df20487d64101f528e5d426541ca4e5` | inference profile/build script |
| Gateway Protobuf CMake package | `35.0.0` | `infer-cpp/CMakeLists.txt` |
| Gateway host profile `protoc` | `3.12.4` | `contracts/profiles/v1/central-inference-cpu.json` |
| Triton/ORT CPU | Triton `2.59.0` image tag `25.06-py3` by digest；ORT `1.19.0` | `central-inference-cpu/v1` |
| Wasm | Wasmtime `47.0.3`、WASI 0.2、`wasm-tools 1.256.0` | Plugin Host profile/CI |
| PostgreSQL | PostgreSQL `18.6`、PgBouncer `1.25.2` | DB/deploy Dockerfiles |
| P4Runtime | `1.4.1` | P4 profiles/source locks |
| P4 runner | Python `3.13.15`、PTF `0.12.0`、Scapy `2.6.1`、gRPC `1.80.0` | `e2e-runner-compose/v1` |
| P4 network tools | iproute2 `7.0.0-r0`、Tcpreplay `4.5.2-r1`、Mininet `2.3.0-1ubuntu1` | P4 runner/Mininet profiles |

注意：Central profile 当前记录 `protoc_version=3.12.4`，而 CMake 对其私有 gRPC prefix 强制 Protobuf package `35.0.0`。两者不可被文档擅自合并或改写；host build 以 `scripts/build-grpc-toolchain.sh` 和 CMake 的 fail-closed 检查为准，但该 profile 表述差异在形成新资格结论前必须由契约/供应链维护者解决。

### 系统包与专用工具

普通开发至少需要以下类别。发行版安装命令只是获取手段，是否合格仍由上表的版本检查决定。

- C/C++：`gcc`、`g++`、`make`、CMake、`pkg-config`、OpenSSL headers、zlib headers、`clang-format`、`clang-tidy`；
- Go gate：`gofmt`、`staticcheck`、`govulncheck`；
- shell/DB gate：`shellcheck`；
- Plugin Host deep gate：`cargo-audit`、`cargo-deny`、`cargo-llvm-cov`、Valgrind；
- Web browser gate：Playwright 的 Chromium、Firefox、WebKit 及其 Linux libraries；
- P4/traffic gate：root/sudo、Linux namespace/veth、Mininet、iproute2/`tc netem`、Tcpreplay、tcpdump、`NET_ADMIN`、`NET_RAW`；
- supply-chain gate：仓库锁定的 Syft、Trivy、Cosign image，fresh offline Trivy DB/policy cache、Cosign key material和公开 trust root；
- P4 formal build：BMv2 与 PI 的 exact source archive，文件名、size 与 SHA-256 必须匹配两个 `source.lock.json`。

P4、Mininet、PTF、Tcpreplay 和 netem 仅可用于隔离测试，不能成为生产 writer、scheduler、发包服务或 effect path。

### 语言依赖的唯一来源

不要手工维护一份重复的全量第三方包表。direct/transitive closure 分别以以下文件为准：

| 模块 | direct manifest | 完整锁定闭包 |
|---|---|---|
| Edge | `edge-rs/Cargo.toml` | `edge-rs/Cargo.lock` |
| Plugin Host | `plugin-host-rs/Cargo.toml` | `plugin-host-rs/Cargo.lock` |
| Control | `control-go/go.mod` | `control-go/go.sum` |
| DB tools | `db/go.mod` | `db/go.sum` |
| Analysis | `analysis-py/pyproject.toml` | `analysis-py/uv.lock` |
| Offline ML | `ml-py/pyproject.toml` | `ml-py/uv.lock` |
| Web | `web/package.json` | `web/package-lock.json` |
| OpenAPI TS generator | `contracts/openapi/v1/typescript-client/package.json` | 同目录 `package-lock.json` |
| C++ Gateway | `infer-cpp/CMakeLists.txt`、vendored manifest | supply-chain registry 与 gRPC build script |
| P4 test Python | `testkit/p4_switch/requirements-e2e.txt` | exact pinned requirements + runner image digest |

关键 direct dependency 包括：Control 的 chi/pgx/gRPC/Protobuf；Edge 的 Tokio/Tonic/Prost；Plugin Host 的 Tokio/Tonic/Rustls/Wasmtime；Analysis 的 aiohttp/httpx/jsonschema/LangGraph/MCP/Pydantic；Offline ML 的 NumPy/ONNX/ORT/scikit-learn/SHAP/XGBoost CPU；Web 的 Vue/Router/Pinia/Element Plus/ECharts/TanStack Query/Virtual。具体 patch 版本必须从 manifest/lockfile读取，不从本文复制后单独演进。

## 一次性初始化

### 1. 验证宿主

```bash
uname -m
git --version
docker version
docker compose version
docker buildx version
python3 --version
uv --version
go version
rustc --version
cargo --version
node --version
npm --version
cmake --version
gcc --version
protoc --version
jq --version
openssl version
```

任何 formal gate 的版本不符都应记录为 `HOLD`/`NOT_RUN`，不能静默使用“最接近”的版本。

### 2. Python

```bash
uv python install 3.12.13
uv venv --python 3.12.13 .venv
uv pip install --python .venv/bin/python \
  jsonschema==4.26.0 PyYAML==6.0.3
(cd analysis-py && uv sync --frozen --python 3.12.13)
(cd ml-py && uv sync --frozen --extra dev --python 3.12.13)
.venv/bin/python --version
analysis-py/.venv/bin/python --version
ml-py/.venv/bin/python --version
```

根 `.venv` 专供仓库级 CI/contract/runner 脚本；共享 validator 使用 JSON Schema Draft 2020-12，当前 CI 固定 `jsonschema==4.26.0`，Control OpenAPI validator另需 `PyYAML==6.0.3`。Analysis 与 Offline ML 继续使用各自 frozen `.venv`，不要把根工具依赖额外安装进模块环境。

### 3. Node 与浏览器

```bash
npm --prefix web ci --ignore-scripts --no-audit --no-fund
npm --prefix contracts/openapi/v1/typescript-client ci \
  --ignore-scripts --no-audit --no-fund
(cd web && npx playwright install --with-deps chromium firefox webkit)
```

生成/核验 OpenAPI client：

```bash
(cd control-go && scripts/check-typescript-client.sh)
```

该脚本还需要 `jq`、GNU `tar` 和 `sha256sum`，并拒绝 generated tree digest 漂移。

### 4. Rust

```bash
rustup toolchain install 1.97.1 --profile minimal \
  --component rustfmt,clippy,rust-src
(cd edge-rs && cargo fetch --locked)
(cd plugin-host-rs && cargo fetch --locked)
cargo +1.97.1 install wasm-tools --version 1.256.0 --locked
```

Plugin Host 还需要 `rust-src`；进入其目录时 `rust-toolchain.toml` 会补齐。Edge deep checks 的 pinned nightly、Miri 与 sanitizer 由其脚本管理，不应替代 stable build。

### 5. Go

```bash
(cd control-go && GOTOOLCHAIN=go1.26.6 go mod download)
(cd db && GOTOOLCHAIN=go1.26.6 go mod download)
GOTOOLCHAIN=go1.26.6 go version
```

正式 DB gate 可以从 `deploy/postgresql-state/toolchain/go-linux-amd64.lock.json` 准备 exact toolchain；不要以较低 patch 的宿主 Go 生成正式证据。

### 6. C++ Gateway

先安装 C/C++ system packages，再构建一次 exact gRPC prefix：

```bash
cd infer-cpp
sudo scripts/build-grpc-toolchain.sh
export MASI_INF_GRPC_PREFIX=/opt/masi-toolchain/grpc-1.82.1
cmake --preset cpu-release
cmake --build build/cpu-release --target masi_inference_gateway
```

脚本需要 `git cmake jq make`，并有意拒绝覆盖已存在的 source/prefix。不要用系统 gRPC、系统 nlohmann/json 或另一 Protobuf package 替代。

### 7. P4 formal inputs

普通 P4 compile 会使用 pinned compiler image：

```bash
./p4/scripts/compile.sh
```

正式 source-rebuild 另外需要：

- `behavioral-model-693e69e634fedf007964a069fdd69acc441a5f80.tar.gz`；
- `PI-577b502da91b7d17e4be5821d49d481dc2e1bb7a-p4runtime-v1.4.1.tar.gz`；
- `out/supply-chain/trivy-cache`；
- `.masi-secrets/cosign`；
- 与 profile 完全相等的 runtime/runner image digest。

缺少这些材料时只能运行 rehearsal，不能伪造 formal PASS。

## 配置、证书与秘密

### 通用规则

- 模块配置使用严格 JSON/schema；未知字段、未知 major、digest/profile drift 必须 fail closed。
- Edge 与 Inference config 最大 `1 MiB`，必须是普通、非 symlink 文件；TLS 私钥不可授予 group/other 权限。
- production secret 只允许 secret file、secret manager 或 workload identity；不得进入 Git、镜像、CLI、环境转储、日志、前端 bundle 或 Artifact。
- 跨主机 gRPC/HTTPS/MCP/A2A 与 PostgreSQL 必须完整 TLS/mTLS 验证 CA、SAN、hostname、用途和有效期；无明文 fallback。
- Analysis/Plugin Host 的 acceptance Compose 要求绝对 config/secret 目录；Analysis 还要求预创建的 restricted external network。
- Offline ML 是无网络 terminating job，只需不可变 image 与空的绝对 output parent。

### PostgreSQL module runtime

DB module gate 的短期 PKI、SCRAM secret 与 DSN 使用 fresh、空、绝对目录生成：

```bash
runtime_root="$(mktemp -d /tmp/masi-db-runtime.XXXXXX)"
deploy/postgresql-state/scripts/prepare-runtime.sh "$runtime_root"
```

生成目录包含敏感信息，默认 `0600`；使用完只按 exact-owned 路径清理。production secret 与长期 PKI 不能复用该两日测试 CA。

### 配置模板现状

当前仓库没有统一 `.env.example`、根级 PKI 生成器或一套可长期运行的全模块 production config。Edge、Inference、Plugin Host 和 Analysis 的 OCI smoke 会按测试身份动态生成严格配置；手工长期启动必须自行提供与公开 schema/profile一致的配置、证书、artifact cache 和 endpoint，不能回退到 test config。

## 端口清单

| 范围 | 默认/容器端口 | 说明 |
|---|---|---|
| Control test | HTTP `18080`、gRPC `19090` | loopback test profile |
| Control test PostgreSQL | host `55433` | plaintext、数据库 `masi_control_test`，仅测试 |
| Edge | gRPC `7444` | 必须按 config/mTLS 使用 |
| Central Gateway | gRPC `7443` | Edge 唯一可见 inference endpoint |
| Triton internal | HTTP `8000`、gRPC `8001`、metrics `8002` | production 不得暴露给 Edge/Web/插件/公网 |
| Plugin Host | manager gRPC `7445`、health `8080` | acceptance host bind 默认 loopback |
| Analysis | A2A `7446` | acceptance host bind 默认 loopback |
| Web OCI | `8080` | `/healthz`；TLS由受信 ingress/gateway 提供 |
| BMv2 P4Runtime | container `9559` | connected rehearsal动态分配 host loopback端口 |
| DB module primary/PgBouncer | `55442`/`56442` | TLS module gate |
| DB standby/restore/fault | `55443`/`55444`/`55445` | 仅对应 gate profile |

Runner 会尽量分配动态 loopback 端口。不要依赖固定 host 端口发现生产服务，也不要把 Triton internal API 当公共入口。

## 九模块开发、构建与运行

以下是最小入口；完整命令、环境变量、benchmark 和 evidence 语义以链接的模块 README 为准。

| 模块 | 开发/构建 | 真实运行或门禁 | 主要外部前置 |
|---|---|---|---|
| P4/Switch | `./p4/scripts/compile.sh` | `sudo ./testkit/p4_switch/run-module-e2e.sh` | Docker、root、Mininet/netns、pinned images；formal另需source archives |
| Edge | `cd edge-rs && cargo build --locked --release --bin masi-edge` | `target/release/masi-edge --config /abs/edge.json`；`scripts/run-module-gates.sh` | strict config、mTLS、writable target-scoped `data_dir`、P4/Central/Go endpoints |
| Inference | `cd infer-cpp && cmake --preset cpu-release && cmake --build build/cpu-release` | 先启动 exact Triton repository，再运行 Gateway config；`scripts/run-module-gates.sh` | exact gRPC prefix、Triton digest、model closure、mTLS |
| Control | `cd control-go && go build ./cmd/control-core` | `go run ./cmd/control-core --config <file>`；`scripts/run-module-gates.sh` | 已迁移 PostgreSQL；production另需OIDC和所有真实mTLS adapter |
| PostgreSQL State | `cd db && go build ./cmd/masi-dbctl ./cmd/masi-dbload` | `MASI_DB_FORMAL_SOAK=1 scripts/run-module-gates.sh` | Docker、TLS runtime、独立 migration job、PITR/standby资源 |
| Plugin Host | `cd plugin-host-rs && cargo build --locked --release --bins` | `target/release/masi-plugin-host --config /abs/plugin-host.json` | Manager mTLS、artifact cache、可选per-generation UDS service |
| Analysis | `cd analysis-py && uv sync --frozen` | `.venv/bin/masi-analysis --config /abs/config.json`；module gate | exact binding、mTLS A2A/MCP/provider、private writable store |
| Offline ML | `cd ml-py && uv sync --frozen --extra dev` | `.venv/bin/masi-offline-ml run --repo .. --output <new-path>`，随后 `verify` | output 必须不存在；CPU-only、无网络/DB/P4 |
| Web | `cd web && npm run build` | `npm run dev` 或 production NGINX OCI；module gate | same-origin Go `/api`、`/events`、`/oidc`；三浏览器 |

Contracts 没有独立的全仓统一 gate。当前验证由 Edge、Inference、Control、Plugin Host、Analysis、Offline ML、Web 和 P4 consumer 分别执行；OpenAPI TypeScript client 是明确的统一生成链。不要声称“运行一个 contracts 命令已完成全部 breaking/golden 跨语言门禁”。

## 启动步骤

### A. 手工 loopback Web 开发

1. 完成 Python/Node/Go bootstrap。
2. 启动 `control-go/testdata/compose.e2e.yaml` 的 PostgreSQL。
3. 运行 `cmd/migrate-test`；它会拒绝数据库名不含 `test` 的目标。
4. 运行 `control-core --config testdata/control-e2e-config.json`。
5. 在另一终端运行 `web/npm run dev`。
6. 停止前台进程，再对同一 compose file执行 `down --volumes --remove-orphans`。

根 README 给出完整命令。该 profile 明文且带测试凭据，只允许 loopback 开发。

### B. 自动 Go/PostgreSQL/Web rehearsal

```bash
GOTOOLCHAIN=go1.26.6 PATH="$PWD/.venv/bin:$PATH" \
MASI_P9_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-p9-local" \
  testkit/system/run-web-control-pairwise.sh
```

依赖：`curl docker go jq node python3 sed timeout`、Web lock graph及三浏览器。Runner 自行迁移、build、分配端口、验证、采证并清理。

### C. Go/PostgreSQL/Analysis rehearsal

```bash
run_id="$(date -u +%Y%m%dT%H%M%SZ)-p11-local"
GOTOOLCHAIN=go1.26.6 PATH="$PWD/.venv/bin:$PATH" \
analysis-py/.venv/bin/python testkit/system/run-go-analysis-pairwise.py \
  --run-id "$run_id" \
  --evidence "evidence/pairwise-rehearsal/p11-go-analysis/$run_id/summary.json"
```

Analysis 本体、A2A mTLS 与 Go/PostgreSQL 都是真实进程；外部 provider/MCP 是 deterministic fixture，所以不是 provider 或正式 P11 资格。

### D. 九组件 startup rehearsal

前置：根工具 `.venv` 与两个模块 `.venv`、全部语言工具链、Docker exact profile、Playwright browsers、P4 root/network能力、Triton image、足够磁盘与较长执行窗口。

```bash
GOTOOLCHAIN=go1.26.6 PATH="$PWD/.venv/bin:$PATH" \
MASI_SYSTEM_STARTUP_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-startup-local" \
  testkit/system/run-full-startup-rehearsal.sh
```

顺序固定为：

1. BMv2/P4Runtime/PTF/Mininet；
2. Edge OCI；
3. Gateway + pinned Triton/ORT CPU；
4. PostgreSQL + migration + Control + Web + 三浏览器；
5. Go↔Analysis A2A；
6. Plugin Host OCI；
7. Host↔Wasm statistics；
8. Analysis OCI；
9. Offline ML OCI。

每个 component 默认总 timeout `10800s`。全组件 operational startup 通过时顶层仍为 `HOLD/NOT_QUALIFIED`，脚本有意返回 `2`。检查 summary 与 cleanup，不要只看退出码或 stdout。

### E. Connected full-stack rehearsal

```bash
run_id="$(date -u +%Y%m%dT%H%M%SZ)-connected-local"
GOTOOLCHAIN=go1.26.6 PATH="$PWD/.venv/bin:$PATH" \
.venv/bin/python testkit/system/run-edge-central-pairwise.py \
  --run-id "$run_id" \
  --evidence "evidence/system-connected-full/$run_id/summary.json" \
  --real-control --real-p4 --with-web --with-sideplanes
```

Runner 自行创建测试 PKI、数据库、P4 compose、binary/image、动态端口和 cleanup evidence。不要并行修改它检索/构建的源码，也不要复用 evidence 路径。可用 `--keep-alive-seconds <bounded-seconds>` 在清理前短暂观察，但它仍不是长期部署器。

### F. 概念上的 production 启动顺序

当前没有实现统一 production runner；以下只是需求约束，不是可直接执行命令：

1. 解析并验证 immutable release/image/model/config/profile digest、SBOM/provenance、PKI 与 secret reference；
2. 启动 production PostgreSQL HA/PgBouncer，执行独立 migration job，完成 schema/readback；
3. 启动 P4 targets；分配给唯一 Edge actor，先只读验证 arbitration、P4Info、pipeline/selector/current；
4. 启动 exact Triton repository/runtime，再启动 Gateway并完成 hardware/provider/warmup/readback；
5. 启动 Go Control，验证 OIDC、TLS、role mapping、DB schema和真实 outbound adapters；
6. 启动 Plugin Host、Host-managed service/Wasm、独立 Analysis Agent，并验证 exact binding/revocation；
7. 仅在 PostgreSQL current、Edge route handshake和全部 readiness fence满足后开放 canonical traffic；
8. 最后通过同源 HTTPS ingress发布 Web；Triton、P4Runtime、数据库和内部插件端口不向浏览器/公网暴露。

在 production Compose/Helm/Kubernetes assets、真实身份、HA、绝对容量和正式 E2E evidence 完成前，不得据此手工拼装并宣称生产可用。

## 停止、清理与恢复

- 优先让 runner 自身捕获 `SIGINT`/`SIGTERM` 并执行 exact cleanup；等待 summary/cleanup evidence写完。
- 手工 Compose 只对明确的 `-p <project>` 和 `-f <file>` 执行 `down --volumes --remove-orphans`。
- 不执行 `docker system prune`、wildcard volume/network/container删除、全局 qdisc/netns 清理或对共享数据库的 `TRUNCATE`。
- P4 runner cleanup 必须验证 standalone container、compose network/volume、Mininet host process、`masi-s1/masi-s2` 与 qdisc 全部消失；失败则隔离该 runner host。
- Offline ML output path必须原本不存在；失败只清理 exact-owned staging，不覆盖已有 artifact。
- PostgreSQL migration不能通过回滚应用进程或手工改已应用 SQL“撤销”；使用 expand/contract与明确 forward-fix/restore流程。

## 正式门禁的额外输入与时长

- 每个模块 formal soak：`60s` warmup（不计）+ `4 × 900s`，实际至少约 61 分钟；完整门禁还包括 build、OCI、fault、scan、PITR/浏览器等。
- Web formal gate还需要与 exact source/image digest绑定的人工 accessibility evidence。
- Supply-chain gates需要 fresh offline Trivy DB/policy、pinned Syft/Trivy/Cosign、签名 key/trust root和离线重建素材。
- 正式 module-gates workflow只在受保护 `main`、clean checkout和专用 self-hosted runner labels上执行；普通 GitHub CI不等价。
- CUDA profile当前未被 `infer-cpp` 首期实现资格化；CPU evidence不能继承到CUDA。
- `availability-single/v1` 只表示一个故障域，即使域内多个replica也不等于HA；production要求 `production-ha` + `availability-ha/v1`。

## 已知缺口与启动阻断

1. **没有生产全栈部署入口**：无 production Compose、Helm、Kustomize 或 Kubernetes manifest；现有 deployment assets 只覆盖 module/acceptance/rehearsal。
2. **没有统一 config/secret 模板**：无根 `.env.example`、完整PKI生成器或跨模块 production config bundle。
3. **Contracts 无单一统一 gate**：validators分散在consumer模块；Buf lint/breaking的完整统一入口尚未实现。
4. **Inference Protobuf 表述差异**：profile 的 `protoc 3.12.4` 与 CMake package `35.0.0` 需要在新资格结论前收敛。
5. **手工长期服务配置不完整**：Edge/Inference/Plugin Host/Analysis module smoke动态生成测试配置；仓库没有可直接复制的生产配置。
6. **历史 evidence只属于原 source digest**：当前文件、contract、migration、runner或工作树变化后必须重跑对应门禁。

## 提交前验证

文档或启动入口变化至少执行：

```bash
git diff --check
.venv/bin/python scripts/ci/check_repository_hygiene.py --include-untracked
.venv/bin/python -m unittest discover -s scripts/ci -p 'test_*.py'
```

再按受影响模块 README 执行 contract/language gate；如果没有运行真实 runtime、浏览器、P4、PostgreSQL、fault、performance或soak，必须在提交/评审中明确写 `NOT_RUN`，不能以文档审计代替执行证据。
