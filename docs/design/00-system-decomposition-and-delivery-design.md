# MASI-NIDS-vNext 系统拆分与交付设计

- 文档状态：`DRAFT`
- 日期：2026-08-14
- 需求基线：`vNext-requirements-1.19`
- 核心需求：`ARCH-001`、`ARCH-002`、`ARCH-003`、`MOD-REGISTRY-001`、`TEST-003`、`TEST-GATE-001`、`TEST-REAL-E2E-001`、`TEST-004`、`TEST-005`、`TEST-006`、`TEST-010`、`ACCEPT-001`、`DEC-044`
- 主要 ADR：ADR-0001、ADR-0003、ADR-0004、ADR-0005、ADR-0006、ADR-0009、ADR-0013、ADR-0014、ADR-0015、ADR-0017、ADR-0018

## 1. 设计结论

系统固定拆为九个资格模块：P4/Switch、Rust Edge、Central Inference、Go Control、PostgreSQL、Plugin Runtime Host、Python Analysis、Web、Offline ML。这个数量来自 `MOD-REGISTRY-001`，不是为了语言或目录整齐而人为切分。

每个模块先独立实现完整功能并取得 Module Complete 证据；九个模块同时完成前，正式跨模块集成状态一律为 `HOLD`。契约、generated client、golden、fake 和无副作用 boundary rehearsal 可以提前协作，但 rehearsal 不能改名为 Module、pairwise 或 system PASS。

Target/Fleet、Model Manager、Plugin Manager、Plugin Statistics、rule observation、firewall policy 等是九个模块内的明确子域，不新增长期在线服务。CPU 与 CUDA 是 Central Inference 的两个互斥启动 profile，不是两个模块。Contracts、Testkit、Deploy、Docs/Evidence 是共享交付资产，不是模块或事实源。

## 2. 拆分原则

模块边界同时满足以下条件：

1. **唯一所有权**：每类持久事实、设备 session、canonical route、durable queue 或可执行副作用只有一个 owner。
2. **公开契约**：跨模块只使用 `contracts/` 生成或验证的公开协议，不导入邻居内部源码，不复制 DTO 后各自演化。
3. **独立资格**：模块可以独立构建、真实启动、从公开输入得到公开输出，并在邻居 fake 下证明自身全部逻辑。
4. **故障隔离**：一个模块失败时按合同进入 `unavailable|HOLD|gap|stale|unknown` 等所属 namespace，不通过隐藏 fallback 改变语义。
5. **资源有界**：队列、WAL、并发、重试、连接、分页、消息、Artifact、日志和 retention 都由 profile 限定。
6. **低耦合集成**：替换模块实现或 current/previous 制品时，只要公开合同和资格范围不变，邻居业务代码不需要同步重写。
7. **不微服务化内部包**：Go Control 和 Rust Edge 都是模块化单体；内部 package/actor/dispatcher 不因职责清晰而变成网络服务。

## 3. 九个资格模块

| 模块 | 目录 | 独立运行/资格单元 | 唯一职责 | 主要公开边界 |
|---|---|---|---|---|
| P4/Switch | `p4/` | P4 program、P4Info、BMv2 JSON 与 exact target profile；真实启动 `simple_switch_grpc` | 数据面转发、IPv4 无状态 firewall、聚合遥测、rule counter | P4Runtime、dataplane ports、test-only packet oracle |
| Rust Edge Agent | `edge-rs/` | 一个 Rust OCI/binary；进程内管理 1..N `TargetActor` | 唯一 P4 session/writer、source/window/WAL、inference route、effect journal/readback | P4Runtime client、Edge control/result gRPC、Central inference gRPC |
| Central Inference | `infer-cpp/` | 一个资格主体：C++ Gateway + pinned Triton + selected ORT CPU 或 CUDA + exact repository | 有界 admission、唯一 delayed batching、模型计算、结果适配与 loaded readback | Edge-facing mTLS gRPC、内部隔离 Triton API、只读 status/readback |
| Go Control Core | `control-go/` | 一个 Go 模块化单体 OCI/binary | 核心业务事实唯一 writer、治理、target/fleet、model/plugin/statistics 管理、API | gRPC、同源 `/api`、`/events`、OIDC/session、PostgreSQL |
| PostgreSQL State | `db/` | PostgreSQL 18 schema、migration job、HA/PITR/restore 资格主体 | 核心持久事实、事务、CAS、幂等、分区和 retention | PostgreSQL wire、migration/backup/restore artifacts |
| Plugin Runtime Host | `plugin-host-rs/` | 独立 Rust OCI/binary + pinned Wasmtime/WASI profile | 实例化已准入 Wasm，受控连接已部署的显式 Host-managed service，强制能力与资源 | Manager/Statistics gRPC、WIT、Host-managed service boundary |
| Python Analysis Plugin | `analysis-py/` | 独立 Python 3.12+ OCI/process | LangGraph/LLM/MCP/A2A 有界证据分析，生成不可执行 Artifact | A2A、restricted MCP client、provider adapter、自有 schema |
| Web SOC SPA | `web/` | production Vue 3/Vite 静态 artifact/OCI + 资格化浏览器 | SOC 展示、输入、可访问交互；不拥有事实或授权 | 同源 HTTPS `/api`、`/events`、OIDC callback |
| Offline ML Pipeline | `ml-py/` | 可重复执行的 Python artifact pipeline 资格主体 | 训练、评估、导出 immutable model bundle 与证据 | dataset/model contracts、bundle/manifest/golden output |

P4、PostgreSQL 与 Offline ML 虽不是普通长期业务服务，仍必须分别形成与可部署模块同等级的独立资格证据，不得遗漏。

## 4. 支撑资产而非新模块

| 资产 | 目录 | 作用 | 禁止取得的所有权 |
|---|---|---|---|
| Contracts/Profile | `contracts/` | 跨语言 schema、WIT、OpenAPI、Protobuf、P4 profile、golden 的唯一源 | 运行时 current、业务事实、调度、授权 |
| Testkit | `testkit/` | traffic fixtures、fake、packet oracle、fault injection、evidence validator | 生产发包、第二 P4 writer、被测模块业务实现、PASS 判定捷径 |
| Deployment | `deploy/` | Compose/三机/生产 profile、migration job、证书和资源编排资产 | model current、target assignment、业务 readiness、自动 fallback |
| Docs/Evidence | `docs/` 与 evidence store | 设计、操作边界、需求追踪和不可变证据引用 | 用文字替代实际执行、覆盖原始结果、修改需求强制语义 |

详细边界见 [`01-supporting-assets-design.md`](01-supporting-assets-design.md)。

## 5. 运行时总拓扑

```text
Web SOC SPA ── same-origin HTTPS /api + /events ──► Go Control Core
  ├── PostgreSQL wire（bidirectional）──────────── PostgreSQL 18 / canonical facts
  ├── control/result/effect gRPC（bidirectional）─ Rust Edge Agent / actors + WAL
  │     ├── only production P4Runtime read/write ─ P4 targets / 1..N BMv2
  │     └── batched-unary mTLS ──────────────────► Central Inference / Gateway→Triton→ORT
  └── plugin control/statistics（bidirectional）── Plugin Host / Wasm + host-managed service

Go Control/qualified peer ◄──── direct typed A2A ────► Python Analysis
Go allowlist read-only MCP endpoint ◄──────────────── Python Analysis MCP client

Offline ML ── immutable bundle/repository/golden ──► Go qualification + Central startup
```

图中只有 Edge 与 P4 target 之间存在生产 P4Runtime 边；Go 不建立生产 P4Runtime 直连。Go 到 P4 的逻辑控制必须经过 durable intent → Edge RPC → P4 journal/readback → PostgreSQL CAS。

Analysis 的业务 A2A/MCP 路径直接通过 typed adapter 连接 Go/peer 和 Go 提供的 allowlist MCP endpoint，不经 Runtime Host。Host 与 Analysis 仍是两个都要真实启动和独立验收的模块。

## 6. 事实、状态与执行所有权

| 事实或执行能力 | 唯一 owner | 其他模块允许做什么 |
|---|---|---|
| 核心业务写入、Event、Incident、Proposal、Decision、Intent | Go Control + PostgreSQL transaction | Edge/插件/Web 只能通过公开合同提交输入或读取投影 |
| P4Runtime session、mastership、write、readback、counter sweep | 对应 Edge `TargetActor` | Go 创建 intent/epoch；P4 执行；Web 展示；testkit 仅隔离测试 |
| target registry、assignment、fleet parent/child/wave | Go Control | Edge 消费租约并报告 actor/readback；Web 展示 |
| model revision、pool generation、per-shard current/previous | Go Model Manager + PostgreSQL | Central 只读 startup envelope/readback；Edge 路由；ML 产 bundle |
| canonical inference route 和 input/result/source WAL | Edge | Go 提交 binding/ACK；Central 计算但不推进 cursor |
| plugin catalog、qualification、binding、revocation | Go Plugin Manager + PostgreSQL | Host/Analysis 执行 exact binding；Web 展示 |
| statistics schedule/run/input freeze/current/history | Go Plugin Statistics + PostgreSQL | Host/direct plugin 计算 Artifact；Web 使用固定 renderer |
| immutable model artifact 与 qualification input | Offline ML Pipeline | Go 登记资格；Central 只读加载；不得由 runtime 原地改写 |
| browser session/CSRF/业务授权 | Go/受信网关 | Web 只携带同源 cookie/CSRF，不保存 token 或自行授权 |

`effect_proposals`、`effect_decisions`、fleet parent、plugin statistic run、deployment action、Triton queue 均不得成为第二 `effect_intents` 队列。只有 per-target `effect_intent` 可被 effect dispatcher claim。

## 7. 模块依赖关系

模块实现允许并行，但依赖必须通过已冻结契约解耦：

```text
contracts / profiles / cross-language golden
├── P4/Switch
├── Rust Edge
├── Central Inference
├── Go Control ─── PostgreSQL
├── Plugin Runtime Host
├── Python Analysis
├── Web SOC SPA
└── Offline ML ── model bundle ──► Central Inference / Go Model Manager

testkit + deploy + evidence tooling
└── each module black-box ──► all-module gate ──► pairwise ──► system waves ──► full E2E
```

这里的箭头不授权“先把真实上下游接起来再补模块”。实现阶段的模块只依赖契约、generated client 和合格 fake；真实 pairwise 必须等待九模块同时 Module Complete。

## 8. 模块独立完成模型

每个模块都要形成同样的交付闭环：

```text
frozen public contract/profile/golden
→ complete module implementation
→ language and contract verification
→ real binary/OCI + actual runtime startup
→ public-boundary black-box E2E
→ fault/recovery/security/performance/compatibility evidence
→ module documentation and reproducible commands
→ Module Complete aggregate
```

Module Complete 的含义是该模块被分配的首期功能全部实现；邻居 fake 只能代替未接入模块，不能代替被测模块内部职责。仅有 schema、stub、健康端点、microbenchmark、静态页面或 rehearsal 都不满足完成条件。

模块状态至少记录：module/release/profile/artifact digest、required requirement set、证据 ID、`level/applicability/result/qualification`、claim scope、waiver、append-only findings 和未决项。`DEC-044` 的 operational Module Complete 由完整实现、真实候选 binary/OCI、适用模块门禁的实际执行结果、open P0=0 与真实启动/测试 blocker=0 共同重派生；实际测试 `FAIL|HOLD|NOT_RUN`、缺失证据或启动失败都阻断完成。受保护基线、dirty tree、生产绝对门槛或尚未开展的正式 pairwise/system 只形成 qualification-only HOLD，不阻断 operational completion，原资格 result/`NOT_QUALIFIED` 不得改写。条件能力只有稳定、机器可读的 `NOT_APPLICABLE` 才能排除。

## 9. 正式集成门禁

正式集成按以下阶段推进：

1. **Gate 0：合同冻结**。冻结公开协议、profile、golden、错误、资源上限和模块黑盒接口。
2. **Gate 1：九模块独立完成**。模块可以并行开发，但每个都必须独立真实启动并完整通过自身门禁。
3. **Gate 2：全模块完成**。九个 Module Complete aggregate 同时有效；任一模块退出完成状态，全局回到 `HOLD`。
4. **Gate 3：十二个 pairwise**。在干净环境按需求指定边界依次真实启动双方，链外只使用确定性 fixture。
5. **Gate 4：十个系统波次**。从数据面、检测面逐步拼到插件扩展与 Agent 旁路；前一波的契约、恢复和性能未通过时不引入下一波真实 mutation。
6. **Gate 5：Full E2E 与目标部署资格**。按 runtime、availability、deployment tier、topology 和 fault scenario 分别出证据；不同 scope 不继承。

集成中发现模块缺陷时，缺陷模块立即退出 Module Complete；修复必须先重跑该模块完整门禁，再重跑受影响 pairwise、系统波次和 Full E2E。不得只在集成环境加 adapter、feature flag 或数据修补绕过模块缺陷。

## 10. 性能、稳定性与兼容性总策略

### 10.1 性能

- packet 热路径只包含已资格化的 P4 parser/match/action/counter；数据库、插件、LLM、策略编译和审批均在热路径外。
- P4 使用有界聚合而非默认逐包上送；Edge 批量 Read、预分配、有界异步 I/O、无等待 coalescing；Triton 是唯一延迟型 batch scheduler。
- Go 使用批量 ingest、短 PostgreSQL transaction、索引化强类型热字段和 server-side cursor；Web 消费有界投影。
- 所有门槛以 exact environment/profile 的绝对值验收。相对回退、单点 microbenchmark 或 BMv2 软件结果不能外推生产硬件。

### 10.2 稳定性

- 核心写入以 PostgreSQL 为事实源；外部副作用遵循 durable intent、claim/fence、无事务 RPC、readback、CAS finalize。
- Edge 对 source/input/result 使用同一所有权域内的有界 WAL；只有 PostgreSQL Event commit 后的 canonical ACK 才推进 cursor。
- 多 target 每个 actor 独立 session/journal/queue；多 inference replica 只在同 generation 内等价重算；故障不通过换模型、换 backend 或 Edge-local 推理掩盖。
- 不确定的外部副作用使用 `unknown/reconciling`；尚未尝试但前置缺失使用 `HOLD/stale`，两个语义不混用。

### 10.3 兼容性

- 所有边界按 major/minor 和 exact profile 管理；unknown major 拒绝，minor 只有 old/new 契约测试通过才接受。
- PostgreSQL 采用 expand/contract；current/previous binary、schema、API、P4Info、model、plugin 与 Web asset 都要有兼容矩阵。
- 模型、插件、P4 policy 和部署制品只按 immutable digest 绑定，不解析 `latest`、tag、mtime 或外部 alias 为 current。
- CPU/CUDA、single/HA、BMv2/硬件、development/production 的证据完全分开。

## 11. 何时允许新增模块

只有同时满足下列条件，才能通过新需求基线和 ADR 考虑第十个模块：

- 现有 owner 无法在不破坏故障隔离或资源上限的情况下承担职责；
- 新职责具有独立生命周期、独立扩缩容和独立故障域；
- 不会复制核心事实、route、writer、scheduler 或 durable queue；
- 公开合同、迁移、兼容、性能、故障、部署和退出方案已完整定义；
- Owner 明确批准模块注册表变化。

仅因为代码量大、使用另一种语言、想引入成熟平台或希望独立扩容，不足以新增模块。

## 12. 当前状态

截至2026-08-21，P4/Switch、Rust Edge、Central Inference、Go Control、PostgreSQL State、Plugin Runtime Host六个独立模块已形成各自实现与证据；每个operational completion都只由本模块latest module-gate evidence独立判定。P4/Switch已`PASS/QUALIFIED`，其余五个只达到operational Module Complete并保持各自`HOLD/NOT_QUALIFIED`；Python Analysis、Web、Offline ML仍未开始实现。任何单模块完成都不授予其他模块或系统aggregate资格；九个模块各自完成后，才依[`../integration/pairwise-and-system-integration-design.md`](../integration/pairwise-and-system-integration-design.md)进入正式集成。
