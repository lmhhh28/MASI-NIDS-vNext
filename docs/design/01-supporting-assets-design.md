# MASI-NIDS-vNext 支撑资产设计

- 文档状态：`DRAFT`
- 日期：2026-08-12
- 关联需求：`CONTRACT-001`、`CONTRACT-PROFILE-001`、`CONTRACT-SUPPLY-001`、`TEST-001`、`TEST-002`、`TEST-003`、`TEST-007`、`TEST-008`、`TEST-009`、`TEST-REAL-E2E-001`、`TEST-REUSE-001`
- 关联 ADR：ADR-0005、ADR-0006、ADR-0009、ADR-0012、ADR-0017

## 1. 定位

`contracts/`、`testkit/`、`deploy/` 和 `docs/` 是九个模块共同依赖的交付资产。它们必须像代码一样版本化、测试和固定 digest，但不拥有在线业务状态，不运行第二控制面，也不增加模块注册表。

支撑资产可以先于业务模块建立，用于解除团队并行开发的耦合；它们不能包含“临时实现一份业务逻辑供大家共用”的捷径。凡是会决定 Event、授权、effect、model current、plugin current、P4 write 或统计 current 的逻辑，都必须回到唯一业务 owner。

## 2. Contracts 与 Profiles

### 2.1 目录职责

`contracts/` 是以下内容的唯一源：

- Protobuf/gRPC、OpenAPI 3.1.2、JSON Schema 2020-12、WIT；
- P4Info、normalized firewall/rule observation/telemetry/target/fleet profile；
- model bundle、feature schema、label taxonomy、output adapter、inference wire；
- plugin manifest/kind/statistics/display 和 A2A/MCP restricted profile；
- qualification、fault、performance、traffic replay、restore、supply-chain evidence schema；
- 跨 Go/Rust/C++/Python/TypeScript/Wasm 的 canonical bytes、digest、数值与错误 golden。

每个 contract domain 必须有 owner、version policy、canonicalization、资源上限、错误语义、兼容矩阵和停止支持条件。生成代码是派生物，source/config/generator/output digest 必须可追踪；任何模块不得手写第二套同名 DTO 作为长期兼容层。

### 2.2 Profile Registry

`contracts/profiles/v1` 固定上游规范和项目使用子集，包括但不限于：

- P4Runtime 1.4.1、BMv2/v1model、P4 firewall/telemetry/rule/target-fleet；
- `inference-central-grpc-batch/v1`、ORT CPU/CUDA、availability、deployment tier；
- A2A 1.0、`masi-mcp-readonly/v1`、WASI 0.2/WIT；
- Web SPA/browser/performance、PostgreSQL、E2E runner、traffic backend；
- qualification evidence、fault、performance、supply-chain tooling。

上游的 `latest`、浏览器 current、OCI tag 或“stable”名称都不能自动进入 profile。每个升级都作为明确 profile revision，通过 old/current reader-writer、golden、故障和回滚矩阵。

### 2.3 Golden 管理

Golden 保存一次，所有语言 runner 读取同一份 expected bytes/data；不得由各模块复制后修改。Golden 至少覆盖：

- canonical identity/digest、field presence、ordering、unknown version/error；
- telemetry window/time/quality、inference tensor/result、model taxonomy；
- firewall normalized plan、target/fleet、rule observation；
- plugin manifest/WIT/statistics/display；
- NaN/Inf/signed-zero、absolute/relative/ULP tolerance。

Golden 通过只证明合同一致，不证明模块功能、性能或系统 E2E。

## 3. Testkit

### 3.1 允许提供的能力

`testkit/` 可以提供：

- contract fake server/client、deterministic provider fixture；
- generated/synthetic/curated/live-session traffic fixture 与严格 manifest；
- PTF/P4Testgen packet/action oracle、Mininet/BMv2/netem adapter；
- fault injection adapter、seed、注入点与 structured evidence collector；
- model/telemetry/plugin/statistics/security 正负 fixture；
- schema/golden/evidence validator 和 exact cleanup ownership。

### 3.2 禁止边界

Testkit 不得：

- 复制被测模块的策略、状态机、编译器、窗口、授权或投影实现；
- 成为生产 traffic upload/send 服务或持有 production target credential；
- 在正式 pairwise/system 中替代边界内真实 MASI 服务；
- 用 stdout、进程退出码、健康端点、sender accepted 或 counter hit 单独判业务 PASS；
- 自动选择“可用的另一个”runner/backend/profile；
- 修改生产数据库、生产 P4 target 或 effect path。

每个 fake 都必须只实现公开合同，并显式声明覆盖/不覆盖的行为。用于模块黑盒的 fake 证据只能取得 `level=MODULE` 的被测模块 claim；用于提前连线的真实 boundary 只能是 `level=REHEARSAL`。

### 3.3 Traffic Evidence

traffic fixture 必须分开 `generated-packet|synthetic-flow|curated-pcap|live-session`，并分别记录 requested、sender、test ingress、DUT ingress/egress、counter、action outcome 和 detection。任何一级不自动推出下一级；BMv2/Mininet 证据只适用于 exact software target。

## 4. Deployment Assets

### 4.1 Profile 分层

`deploy/` 设计为三个互不冒充的 profile 集合：

1. **本地/CI E2E**：固定 `e2e-runner-compose/v1`，使用隔离 network/volume/project、显式 health dependency、有界 deadline/resource/evidence/cleanup。
2. **三机/operational-single-domain**：验证远程中央推理、TLS、真实流量和完整系统；最多取得 `SYSTEM_E2E`，不声称 production HA。
3. **production-ha**：跨故障域 inference N+1、PostgreSQL HA/PITR、生产网络/安全/容量和 soak；只有该 tier 可申请 `level=PRODUCTION`。

每个 profile 固定 image/config/secret reference/certificate/topology/resource/runtime/availability digest。Compose、systemd/Podman 或 Kubernetes 只负责物化已提交 desired state，不拥有 target assignment、model current、业务 readiness 或 fallback 决策。

### 4.2 启动与迁移

- migration 是独立 one-shot job/binary，不由每个应用副本启动时竞争执行；
- Central Inference 的 CPU/CUDA artifact 分开构建和启动，部署 adapter 不做自动硬件选择；
- Triton repository 是只读 exact closure，不从公网或 mutable registry 在运行期补内容；
- 各模块 startup/readiness/liveness 分开，基础设施 healthy 只允许测试继续；
- stop/drain/cleanup 有边界和 exact identity，不使用广域进程清理或共享可写 volume。

## 5. Docs 与 Evidence

### 5.1 文档层次

- 需求基线：定义必须实现和验收的内容；只有 Owner 可修改强制语义。
- ADR：记录已做出的关键取舍、被拒方案与迁移边界。
- 总体/详细设计：说明模块和内部构件如何满足需求，不提升资格。
- 模块 README：实现形成后提供真实构建、启动、测试、benchmark 和停止命令。
- Runbook：只面向部署/运维操作，不替代测试设计或需求。
- Evidence：保存实际执行事实和不可变引用，不由文档人工宣称 PASS。

### 5.2 Evidence 包

每个证据包至少包含：

- evidence/run ID、requirements、test/profile/schema version；
- `level`、`applicability`、`result`、`qualification` 和 exact claim scope；
- source/image/binary/config/contract/model/P4Info/environment digest；
- 服务清单、身份、拓扑、开始结束时间；
- 输入 fixture、fault、raw structured result、日志/trace/artifact hash；
- cleanup、reviewer/Owner reference、waiver 和 expiry。

摘要只能引用原始 append-only evidence；不得覆盖失败结果或把另一个 runtime/topology 的证据复制过来。当前文档没有生成任何资格 evidence。

## 6. 支撑资产自身门禁

支撑资产至少要证明：

- schema lint、breaking、canonical golden 和 generated drift 可检测；
- fake 与公开合同一致，且没有导入模块内部源码；
- traffic/fault/evidence runner 的资源、权限、路径、网络和 cleanup 有界；
- runner/backend 缺失时稳定 `HOLD|NOT_RUN`，不自动换实现；
- dependency/tool/image 有 exact version、license/NOTICE、SBOM、provenance、离线可重建和退出策略；
- 被拒的第二 writer、第二 queue、runtime download、Grafana mutation、第三方控制面不能经 test/deploy asset 间接进入产品。

这些门禁由相关模块和 `TEST-REUSE-001` 共同消费；支撑资产不单独产生第十个 Module Complete。
