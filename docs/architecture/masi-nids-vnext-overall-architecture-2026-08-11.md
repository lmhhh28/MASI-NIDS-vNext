# MASI-NIDS vNext 整体与分部架构说明

- 日期：2026-08-12
- 对应需求基线：`vNext-requirements-1.18`
- 性质：非规范性架构说明；与需求冲突时，以 `../masi-nids-vnext-system-requirements-2026-08-09.md` 的稳定需求 ID 为准
- 当前状态：架构已确认。截至 2026-08-21，契约/profile/golden 已冻结（Gate 0）；P4/Switch 已 `PASS/QUALIFIED`，Rust Edge / Central Inference / Go Control / PostgreSQL State / Plugin Runtime Host 已 operational Module Complete（qualification 仍 `HOLD/NOT_QUALIFIED`；PostgreSQL只覆盖single-domain/manual-promotion scope，Plugin Host只覆盖single-domain Host且尚未开展Go/DB pairwise）；Python Analysis、Web、Offline ML尚未开始实现；九模块global gate、正式pairwise/system与生产资格为`HOLD/NOT RUN`
- 关键 ADR：ADR-0001、0003、0004、0005、0006、0007、0008、0009、0012、0013、0014、0015、0017、0018（ADR-0010/0011只保留模型模块化与启动绑定的历史来源，ADR-0016保留v1.13 Central GPU历史）
- 详细设计入口：[`../design/README.md`](../design/README.md)；模块验收：[`../testing/module-e2e-acceptance-design.md`](../testing/module-e2e-acceptance-design.md)；正式集成：[`../integration/pairwise-and-system-integration-design.md`](../integration/pairwise-and-system-integration-design.md)

## 1. 一句话结论

最终架构是一套“多台P4数据面快速处理，Rust Edge按target actor独占设备会话与实时数据，Central Inference由管理员在启动前显式选择CPU或CUDA profile并统一完成批量推理，Go + PostgreSQL统一保存target/fleet/model事实和治理，独立插件平面做非实时扩展，Vue SOC控制台只通过Go操作”的分层系统。

它不是把所有能力塞进一个大服务，也不是把每个小功能拆成微服务。拆分边界按“谁拥有状态、谁执行副作用、谁在实时热路径”确定；一个职责只有一个长期 owner。

## 2. 整体架构

```text
Vue 3 SOC SPA（审阅、审批、运维、规则表现）
└─ same-origin /api + /events
   └─ Go Control Core
      ├─ short transaction/CAS → PostgreSQL canonical facts
      ├─ bounded control gRPC → Rust Edge Agent（per-target actors）
      │                          ├─ P4Runtime → P4 target A/B/.../N
      │                          └─ batched gRPC/mTLS → Central Inference
      │                                                   └─ C++ Gateway → Triton → ORT CPU or CUDA
      └─ Plugin Manager admission/qualification/active binding
         ├─ Host-managed binding → Rust Plugin Runtime Host
         │                         └─ Wasm / Host-managed service plugin
         ├─ independent binding + direct typed A2A/MCP/gRPC adapter
                                   └─ Python Analysis Plugin
                                      └─ LangGraph/LLM/read-only MCP/A2A → Artifact only
         └─ optional statistics capability
            → Go frozen input → bounded plugin run → validated Artifact
            → PostgreSQL plugin_statistics → fixed Vue renderer

 Offline ML → immutable model bundle → Go pool rollout → read-only repository snapshot
 Testkit → isolated PTF/P4Testgen/replay/fault/performance evidence only
```

对应约束：`ARCH-001`、`ARCH-003`、`ARCH-004`、`ARCH-PLUGIN-001`、`ARCH-MODEL-001`、`ARCH-TELEMETRY-001`、`ARCH-FW-001`、`ARCH-TARGET-FLEET-001`。

## 3. 为什么这样拆

| 平面 | 负责什么 | 不负责什么 | 主要模块 |
|---|---|---|---|
| 数据面 | 对每个包做有界 match/action、聚合和 counter | LLM、数据库、审批、复杂状态机 | P4/BMv2 |
| 边缘实时面 | 每 target P4 session/actor、窗口、WAL、批处理、回压、设备 journal/readback | 业务授权、核心数据库写入、LLM、跨 target 原子承诺 | Rust Edge |
| 推理计算面 | 固定合同的中央批量模型计算；启动时互斥选择CPU或CUDA资源 | 抓包、P4、数据库、模型current决策、durable queue、运行时自动profile切换 | C++ Gateway + pinned Triton/ORT CPU或CUDA pool |
| 控制/治理面 | Event/Incident、target registry、fleet waves、策略、审批、effect、模型/插件控制、API | 直接持有 P4 session、运行第三方代码 | Go Control |
| 状态面 | target/fleet 与其他 canonical facts、事务、幂等、CAS、历史和恢复 | P4 写入、业务逻辑旁路 | PostgreSQL |
| 扩展面 | 已准入插件隔离执行、非实时分析 | 实时检测、核心 effect、第二队列 | Plugin Host + Wasm/Host-managed service；独立 service/Agent |
| 交互面 | 研判、审批、状态展示和运维入口 | token/secret、BFF、设备直连、浏览器插件 | Vue SPA |
| 资格面 | contract/golden/fault/replay/performance evidence | 生产 writer 或在线发包服务 | testkit/CI |

这套拆分把最危险的两个权力分别锁死：Go/PostgreSQL 是业务事实唯一写入链，Rust Edge 是 P4 唯一写入链。插件、LLM、Web、Grafana 和测试工具都不能取得这两种权力。

## 4. 主要运行链路

### 4.1 实时检测链

```text
packet
→ P4 bounded aggregate bank/epoch（主采集源）
→ Edge batch Read + event-time final window + telemetry/input WAL
→ Edge no-delay coalescing + bounded batched-unary gRPC/mTLS
→ stateless C++ Gateway identity/framing/quota/fence
→ pinned Triton dynamic batching + startup-selected ORT CPU or CUDA inference
→ Edge result WAL
→ bounded Edge→Go batch
→ PostgreSQL canonical Event commit
→ canonical ACK 后 Edge 才推进 cursor/compact
→ Go API/SSE → Web
```

性能关键点：P4 aggregate-first，避免把所有packet送控制面；Edge复用gRPC channel并批量发送，Triton是唯一允许主动等待的dynamic batcher，所选CPU或CUDA profile用资格化instance group并行；Edge→Go批量写，PostgreSQL commit后才ACK。Digest/PacketIn只是可丢失的补充hint/sample，不是主队列。Gateway和retry也不是第二durable queue。详见`ARCH-TELEMETRY-001`、`CONTRACT-TELEMETRY-001`、`CONTRACT-INFERENCE-001`、ADR-0013/0017。

### 4.2 临时攻击处置链

```text
Event/Incident/Evidence
→ deterministic eligibility/risk
├─ qualified automation：已资格化的 R0 policy，或 Owner 显式启用且已资格化的 R1 policy
│  └─ PostgreSQL unique effect_intent
└─ human authorization（仅适用于未走上述自动策略的请求）
   → canonical proposal
   → scoped Operator decision（R0/R1 允许有 scope 的 Operator 按规则同时作为 proposer/approver；R2 checker 必须不同于 proposer，并完成 step-up）
   → PostgreSQL unique effect_intent
→ Edge claim/fence + write_started journal
→ response_overlay P4 Write
→ exact entry readback
→ PostgreSQL CAS applied/failed/unknown
→ observation epoch + direct-counter sweep
→ Web installation/match/outcome
→ durable TTL expiry intent → delete/readback/CAS
```

LLM/Analysis 可以给建议，但不能生成 Decision、Intent 或 P4 Write。审批完成只表示“允许执行”，不表示“设备已经生效”。详见 `FUNC-EFFECT-001`、`FUNC-GOV-001`、ADR-0001/0004/0014。

### 4.3 长期通用防火墙策略链

```text
Platform Admin creates immutable baseline revision + explicit default
→ Go validates normalized policy
→ Edge read-only compiles exact target plan
→ diff / conflict / shadow / expansion / capacity
→ different Operator step-up approves exact R3 change
→ unique effect_intent
→ write complete inactive bank
→ exact inactive-bank readback
→ flip one selector entry
→ exact selector/active-bank readback
→ PostgreSQL current/previous CAS
→ retain previous for bounded rollback grace
→ exact cleanup
```

该链使用与临时处置相同的 proposal/decision/intent/Edge/journal/readback/CAS，不增加第二审批系统或第二队列。双 bank 的目的不是炫技，而是避免一次更新上千条长期规则时出现“半新半旧”。详见 `FUNC-FW-001`、`REL-P4-FW-001`、ADR-0014。

### 4.4 规则表现链

```text
effect/readback applied
→ Go creates canonical observation epoch
→ Edge bounded direct-counter sweep
→ PostgreSQL latest + 5-minute/hourly rollup
→ Web Rule Effectiveness

installation  ≠  dataplane match  ≠  packet/action outcome
```

canonical epoch 和 PostgreSQL 规则事实只由 Go 创建/写入；Edge 只按该 identity 采样，不在本地建立第二 epoch。一个 counter 增长只能说明 entry/action 的计数路径执行过，不能独自证明包被正确 drop、攻击停止或业务风险下降。无 eligible traffic、no hit、reset、gap、stale 和 not measurable 必须分开。详见 `FUNC-RULE-001`、ADR-0008。

### 4.5 模型替换链

```text
Offline ML qualified immutable bundle
→ Go durable pool rollout operation + proposed generation
→ stage read-only digest-pinned exact repository closure and explicit CPU or CUDA runtime image
→ probe selected/observed hardware; mismatch fails closed
→ start/warm/qualify Gateway/Triton replicas without canonical traffic
→ exact GetLoadedModel/GetPoolStatus/readiness/numeric/performance readback（HA profile另验证跨域N+1）
→ Edge advances route epoch, withdraws old route, drains admitted work and WAL-buffers new final windows
→ PostgreSQL per-shard CAS current/previous pool binding generation
→ Go/Edge exact generation commit handshake; Edge resumes the unique logical-pool route
→ drain old generation; late old result fenced
```

模型是可拆卸的数据/合同模块，不是通用插件，也不编译进C++ Gateway binary。首期在pool generation启动前选择并验证模型与`model-runtime-central-cpu/v1`或`model-runtime-central-cuda/v1`；硬件probe只验证管理员选择，不自动改选。Triton使用`model-control-mode=none`、只读exact repository closure和显式instance group；CUDA profile只允许预先声明、读回并测量的host-side operator placement。模型或CPU↔CUDA切换都通过新generation启动/readback、Edge route-withdraw/drain/WAL、PostgreSQL CAS、exact commit/resume和旧generation drain完成，不做进程内热插拔或runtime repository reload。同一exact generation的同profile健康replica可由服务发现选择并按合同做有界重试，但不能切换到Edge-local、另一compute profile、其他backend或其他模型；pool全不可用时Edge只做有界WAL/backpressure，随后`gap/HOLD`。详见`ARCH-MODEL-001`、ADR-0010/0011/0017。

### 4.6 插件与 Agent 分析链

```text
digest-pinned plugin artifact + manifest + publisher/SBOM/provenance
→ Go Plugin Manager verify/qualify/activate exact binding
├─ Host-managed binding
│  → Rust Plugin Host applies capability/resource/deadline/fence
│  → bounded Wasm/service execution → typed result
└─ independent analysis-agent binding
   → Go/peer direct typed A2A task → official Python Analysis Plugin
   → bounded LangGraph + LLM + read-only MCP + A2A polling
   → grounded, non-executable AnalysisArtifact
→ Go projection → Web
```

通用插件平台首期支持封闭 kind：`analysis-agent`、`read-only-tool`、`pure-transform`。Manager 是统一控制面，但不是业务代理：Wasm 和显式 Host-managed service 走 Host；独立 service/Agent（包括官方 Analysis）通过自身 typed adapter 直连。Service/Agent 都在进程外，Wasm 只在独立 Host；第三方代码不加载进 Go/Rust Edge/C++ 核心进程。LangGraph/LLM 只在官方 Analysis Plugin，MCP/A2A 只在插件/Agent 边界，全部位于实时检测和真实处置闭环之外。System E2E 仍真实启动 Host 与 Analysis 并分别验证，只是不让 Analysis 流量多绕一跳。详见 `ARCH-PLUGIN-001`、ADR-0002/0003。

### 4.6a 插件统计与前端展示链

```text
active binding + qualified PluginStatisticsDefinitionV1
→ authorize host-owned input projection/fields + conditional external-source capability
→ Go canonical facts/projections
→ authorized bounded StatisticsInputBundleV1
→ durable non-effect run
→ pure-transform（默认）/ read-only-tool（条件）
→ PluginStatisticsArtifactV1
→ Go validates schema + identity + digest + scope + generation + quality + limits
→ PostgreSQL plugin_statistics current/history
→ OpenAPI + SSE invalidation
→ built-in Vue renderer + internal ECharts dataset
```

统计是现有 kind 的输出 capability，不是第四种 kind。每个统计项先以 immutable `PluginStatisticsDefinitionV1` 随 plugin revision 资格化，只能引用 Go 的 host-owned input projection/field registry，或为 `read-only-tool` 引用 manifest/binding 已批准的 external-source capability ID；不能携带 endpoint/credential、SQL/PromQL/JSONPath/MCP prompt/URL/表达式或运行时注册。Go 拥有 schedule/run/idempotency、输入冻结、授权、结果校验和投影；唯一 durable run ledger 是 PostgreSQL `plugin_statistic_runs`，内部 dispatch queue 只能有界派生并可重建。插件不能自调度、扫描数据库、直写 Prometheus 或向浏览器提供代码。Web 固定使用 `metric-card|status|timeseries|bar|heatmap|table|text|evidence-list` 内置组件，并可按 Go 权限提供宿主固定 `Run now`/schedule list/create-revise-disable；不执行插件 action、HTML/SVG/CSS/JavaScript、route、URL、完整 ECharts option 或 Vega 表达式。on-demand 同时验证 source-read 与 `plugin.statistics.run`；schedule mutation 验证 scoped Admin/data-class/CSRF/step-up/idempotency；每次 scheduled run 重新 fence。核心 Rule Effectiveness、Overview 内置指标和真实处置不依赖该链。详见 `CONTRACT-PLUGIN-STAT-001`、ADR-0018。

### 4.7 多 Target 与 Fleet 处置链

```text
Admin registers/validates target candidate
→ Go Target Registry commits stable target_id + desired profile + Edge assignment
→ assigned Edge TargetActor proves assignment lease/election range, endpoint/device/mastership/P4Info/read-only state
→ target becomes mutation-ready

immutable proposal + frozen target-set/wave digest
→ exact Decision
→ one non-claimable fleet parent + one durable effect_intent per target
→ static device canary/waves open bounded child gates
→ each TargetActor journals, writes, reads back and finalizes its own child
→ Go derives parent state from the complete per-target vector
```

Fleet parent 只是聚合事实，实际可 claim 的仍只有现有 `effect_intents`；P4Runtime 原子性只在单台 target 内成立，不承诺跨设备同时生效、2PC 或全局瞬时回滚。一个 target 掉线时，其 actor 与队列被隔离，其他 target 可按冻结的 `fail_fast|continue_isolated|manual_gate` 策略继续或停止。详见 `ARCH-TARGET-FLEET-001`、ADR-0015。

## 5. 各部分详细架构

### 5.1 P4/BMv2 数据面

职责：

- 正常 forwarding；
- IPv4 无状态 firewall：高优先级 response overlay、baseline bank 0/1、policy selector；
- per-entry direct counter 和 eligible counter；
- 有界 telemetry aggregate bank/epoch/snapshot；
- 可选、有界 Digest/PacketIn sample。

首期 firewall match 为 IPv4 src/dst prefix、protocol、可选 ingress port、L4 exact/wildcard ports、fragment class；action 为 `permit-and-continue|drop`。permit 不拥有后续 forwarding。default action 必填。IPv6 effect、stateful、NAT、rate limit、VLAN/tunnel-aware 和硬件 target 未资格化前拒绝。

P4 热路径只有预编译的 parser/table/action/counter，不访问数据库、不跑模型、不运行插件。BMv2 是软件 reference target，适合开发/测试而非生产级硬件性能；软件结果不能外推 ASIC。[BMv2 README](https://github.com/p4lang/behavioral-model#readme)

### 5.2 Rust Edge Agent

内部可以是模块化单体，至少包含：

- `p4_session`：StreamChannel、election、mastership、pipeline/P4Info/application generation；
- `target_supervisor`：assignment reconcile、actor lifecycle、global/per-target 预算与分层公平调度；
- `target_actor`：每 target 独立 assignment lease/election range、endpoint/session、journal namespace、source/observation identity、queue 与故障域；
- `firewall_compiler`：normalized policy → exact target entities、冲突/展开/容量；
- `effect_executor`：claim fence、journal、write/readback/reconcile、bank selector；
- `telemetry_source`：aggregate snapshot、补充 sample、source epoch；
- `window_engine`：flow identity、event time、watermark、finality、quality；
- `inference_transport`：logical pool route、channel reuse、no-delay coalescing、bounded batched-unary/deadline/retry、result fence；
- `durability`：source/input/result WAL、checkpoint、bounded replay；
- `rule_observer`：bounded direct-counter sweep，不拥有 canonical epoch；
- `control_client`：有界 gRPC batch、canonical ACK、backpressure。

这些模块共享一个 Edge 进程，是因为每个 target 的 P4 session、source cursor、journal 和实时调度必须保持单一所有权；`TargetSupervisor` 可以有界管理多个 actor，但绝不共享可写 journal 或用一个串行循环阻塞全部 target。不为了语言或“微服务化”再拆一个常驻 collector/controller。

### 5.3 Central Online Inference

它仍是一个C++ Inference qualification target，不新增业务事实owner，但物理上由无状态C++ MASI Gateway、固定受支持版本的Triton Server、启动前显式选择的ORT CPU或ORT CUDA backend、只读exact model repository closure和对应compute worker pool组成。CPU/CUDA是两个独立资格profile，不是两个业务模块。

C++ Gateway 内部建议分为：

- mTLS identity、request framing/size/quota/deadline；
- binding generation、contract、digest、duplicate 和 fence validator；
- canonical feature/input adapter 与 Triton client；
- output adapter、worker/attempt identity 与稳定错误映射；
- startup hardware preflight、selected/observed profile/provider partition、repository closure/explicit instance group、readiness/liveness/`GetLoadedModel/GetPoolStatus` readback。

Triton独占delayed dynamic batching和instance scheduling；Gateway不做第二delayed batch或durable queue。NONE模式repository只包含exact binding及声明依赖，显式固定instance group。管理员在启动前选择已资格化ORT CPU或ORT CUDA；selected/observed不一致则startup fail closed。CUDA profile中只允许已资格化的host-side operator partition，运行期新CPU接管属于drift。CPU↔CUDA和未来TensorRT都只能创建新的exact qualified generation，不允许请求内、错误时或运行中的backend fallback。Gateway/Triton不抓包、不组流、不访问P4/PostgreSQL、不决定current model、不创建Event，也不直接暴露给浏览器或Go业务。`availability-single/v1`表示恰好一个failure domain，可有1..N域内同profile副本但不具备HA；`availability-ha/v1`必须让同一exact runtime profile跨至少两个故障域并保留N+1资格化容量。

### 5.4 Go Control Core

Go 采用模块化单体，避免把每个业务模块变成独立网络服务：

- `auth/session`：OIDC callback、HttpOnly session、CSRF、scope mapping、step-up context；
- `event/incident`：InferenceResult 校验、Event 幂等、Incident/evidence；
- `governance`：proposal、risk、preflight token、decision、maker-checker；
- `effect`：intent/outbox、claim/finalize、unknown reconcile；
- `target_registry`：稳定 target identity、lifecycle、desired/observed profile、capability 与 Edge assignment；
- `fleet_coordinator`：冻结 target set、静态 canary/waves、parent/child mapping、gate 与聚合投影；
- `firewall`：baseline revision/default/diff、current/previous、R3 activation、overlay projection；
- `rule_observation`：epoch、delta/quality、latest/rollup/status；
- `model_manager`：revision/qualification/desired/current/previous、pool generation、replica qualification、rollout/drain/rollback；
- `plugin_manager`：catalog/qualification/activation/binding/revoke；
- `plugin_statistics`：definition/schedule/run、授权输入冻结、Artifact校验、current/history、retention；
- `api/projection`：OpenAPI、cursor、SSE invalidation、Web 聚合；
- `audit/operations`：append-only timeline、timeout original-operation query。

Go 是核心 PostgreSQL schema 唯一业务写入者，但不在数据库事务中等待 P4、LLM、MCP、A2A、HTTP 或 gRPC。外部调用前后用 durable fact、version vector 和 CAS 连接。

### 5.5 PostgreSQL

PostgreSQL 是 canonical facts 的唯一来源，逻辑上至少分为：

- Event/Incident/evidence；
- governance：effect proposals/decisions；
- effect intents/attempts/events；
- target registry/assignment/capability observations 与 fleet parent/target/wave facts；
- firewall policy revisions/bindings/activation operations/overlay expiry；
- rule observation epochs/latest/status/rollups/outcome refs；
- model platform；
- plugin platform；
- plugin statistics schedules/runs/artifacts/current；
- audit/projection cursors。

只有 Go 写核心事实；Analysis Plugin 只用隔离自有 schema/role，Host/其他插件/C++ Gateway/Triton/Edge/Web 无核心 DB 凭据。migration 由独立 job 执行，生产使用 HA、WAL archive、PITR、隔离恢复演练和连接预算；Redis 不是首期依赖。

### 5.6 Plugin Runtime Host

Host 只执行 Manager 已准入且明确标记为 Host-managed 的 exact binding：

- 验证 plugin/revision/artifact/config/capability/generation；
- 对 Host-managed service plugin 使用受控 UDS/mTLS gRPC；
- 对 Wasm component 使用固定 Wasmtime/WASI 0.2/WIT profile；
- 强制 CPU/memory/PID/FD/disk/fuel/deadline/network/filesystem/secret 最小能力；
- lifecycle：staged/shadow/active/draining/disabled/revoked、bounded restart/quarantine；
- 验证 typed result 和 old-generation fence。

它不拥有 catalog current、不写核心 DB、不创建 effect、不访问 P4，也不实现容器编排器或充当独立 Analysis/service/Agent 的业务代理。

### 5.7 Python Analysis Plugin

官方首个 `analysis-agent` 插件，作为独立 OCI service 由 Plugin Manager 管理 exact binding。Go/外部 peer 通过 A2A typed adapter 直接调用 Analysis Plugin；Analysis Plugin 作为 MCP client，通过 `masi-mcp-readonly/v1` 调用 Go 暴露的 allowlist 只读 tools/resources。A2A 与 MCP 两条路径都不经过 Runtime Host。插件内部可使用 LangGraph 编排 evidence extraction、bounded tool calls、LLM analysis、evidence grounding和不可执行Artifact生成。A2A使用固定1.0 HTTP+JSON polling/no-push/no-streaming profile。provider/tool/证据不足时只返回Analysis namespace中的`limited|insufficient_evidence|failed` Artifact，不改变runtime/model/Event/effect/P4，也不称为inference fallback。

兼容 legacy LangGraph 的是外部可观察行为矩阵，不迁移旧 Workflow/checkpoint/schema/节点源码。输出只包含事实引用、推断、未知、建议、受控 trace summary；不包含可执行 P4 command 或 chain-of-thought。

### 5.8 Offline ML

Offline ML 负责训练/评估/导出 immutable model bundle、feature schema、label taxonomy、output adapter、numeric golden 和 qualification evidence。它不部署模型、不决定 current、不写生产 Event/Incident/effect。

不同算法、权重或更多类别只要维持合同，可以替换 bundle 而无需修改 Edge/Go 主业务代码。新增 label 默认没有 effect eligibility；feature/runtime major 改变走 expand/contract 与新旧 pool generation 兼容矩阵。

### 5.9 Vue SOC Frontend

任务信息架构：

```text
Overview
Detection
Evidence
Effects & Governance
├─ Response Rules
├─ Firewall Policies
└─ Rule Effectiveness
Analysis
Plugins
└─ <Plugin> / Statistics（固定宿主页面）
Operations & Audit
├─ Managed Targets / Fleet Operations
└─ Model Pool Operations / Effect Operations / Health
```

角色交互：

- Analyst：研判 Event/Incident、建立临时 response proposal；
- Operator：检查 evidence、exact diff、risk/scope/TTL/capacity/P4Info/rollback，批准或拒绝；
- Platform Admin：创建 baseline policy revision，管理 target lifecycle/assignment 与 model/plugin exact binding；不自动拥有 Operator 或 P4 effect 权限；
- Auditor：只读查看完整链路和证据。

Web 采用 1Panel 同类 Vue 3/Vite/TypeScript/Pinia/Router/Element Plus/ECharts 技术组合，clean-room 借鉴成熟仪表盘任务模式，不复制 1Panel/sub2api 应用控制面、业务组件或品牌资产。Router/query cache/UI primitive/chart/test/codegen 优先复用资格化基础库；安全关键 proposal/approval/bank activation/rule evidence 组件自有实现。

插件 Statistics 页面只消费 Go OpenAPI/SSE 投影。插件可以提供数据与受限 display hint，但 Vue 组件、design token、ECharts `dataset/encode`、ARIA、route、授权、导出和 action 始终由宿主拥有；固定 `Run now`/schedule 控件只提交 exact definition/binding/idempotency 并查询原 run，插件不能定义按钮、表单或定时表达式。unknown/oversize/unsafe payload 只显示受控错误，不降级渲染 raw HTML/JSON。

Model Pool页面显示desired/selected/observed CPU或CUDA profile、exact current/previous generation、model/runtime/image/config digest、single/HA availability、replica/故障域/N+1容量、CPU core/thread/NUMA/RAM或GPU/driver/CUDA/cuDNN/VRAM、batch/queue/延迟、rollout/readback/CAS/drain和unavailable gap。管理员只能选择已登记且已资格化的profile来创建新generation或发起exact rollback；不能在浏览器中选择`auto/latest`、调用Triton load/unload、开启fallback或把某台worker临时标为current。

### 5.10 Testkit、部署与可观测性

Testkit统一contract golden、fake server、PTF/P4Testgen、generated/synthetic/curated/live-session fixture、packet oracle、fault injection和evidence envelope。它只在隔离test target工作，不成为生产发包服务或第二P4 writer。fake server只服务模块隔离或rehearsal；正式pairwise/system E2E不得用它替代任何参与链路的内部MASI服务。

可观测性复用 OpenTelemetry、Prometheus、Alertmanager 和只读 Grafana，但 per-rule/policy/operation identity 留在 PostgreSQL/Go API，禁止高基数 label、Grafana mutation 和告警自动处置。

## 6. 三机部署

```text
Edge node（可按 profile 横向增加）
├─ BMv2/P4 target A/B/.../N，或经资格化的远端 target
└─ Rust Edge Agent（TargetSupervisor + per-target actors）

Control/State node
├─ Go Control Core
├─ PgBouncer
└─ PostgreSQL（实验单实例；生产 HA/PITR）

Central Inference + Analysis/SOC node（仅三机实验profile允许合并）
├─ C++ MASI Gateway
├─ pinned Triton + explicit ORT CPU or CUDA resources
├─ Rust Plugin Runtime Host
├─ Python Analysis Plugin
└─ Vue static SPA
```

三机只是最小实验布局；使用`availability-single/v1`时明确不具备推理HA。生产若选择`availability-ha/v1`，必须把Central Inference与Analysis/SOC分开，并让同一exact CPU或CUDA profile至少部署在两个独立故障域、满足N+1 qualified capacity；不能用CPU和CUDA互相充当灾备。系统可以横向增加Edge、无状态Go/Gateway/Triton replica、Plugin Host/Analysis worker和只读observability backend。每个target在任一assignment generation只能由一个Edge actor持有P4 writer；迁移必须drain/fence/reconcile，不能增加第二P4 writer、第二effect queue、第二model router或第二核心数据库事实源。

## 7. 高性能、稳定性和兼容性如何同时实现

### 高性能

1. packet 热路径只在 P4 做预编译、有界 match/action/aggregate/counter。
2. P4 aggregate-first，避免全包进入 P4Runtime/用户态；只在模型确需包头时条件启用 mirror capture。
3. Edge复用mTLS gRPC channel、异步batched-unary和no-delay coalescing；Triton作为唯一delayed batcher，用dynamic batch + instance group提升所选CPU或CUDA资源利用率，避免逐记录RPC和多层排队。
4. Edge→Go/DB 批处理，索引/分区/cursor 查询；不逐 Event/规则建立额外 HTTP 或 DB transaction。
5. Gateway 无状态且只做协议适配；插件/LLM/审批/策略编译/全量 readback 都在实时热路径之外。
6. baseline 双 bank 在 inactive 资源批量准备，在线 publication 只改一个 selector；rule observation 低优先级 bounded sweep。
7. 所有性能结论必须使用冻结的绝对 SLO 与 `performance-environment/v1`，以同量纲records/s、bytes/s、p99、headroom、unavailable/replay时间和WAL容量实测，并测试 firewall + telemetry + inference + observation 叠加负载；core数、GPU kernel或单请求benchmark不能推算production capacity。
8. 多设备按 per-target actor 隔离队列、journal 与连接，以 global/per-target/per-wave 配额和分层公平调度避免慢设备造成 head-of-line blocking；必须实测 0/1/2/N，不能从单设备线性外推。
9. 插件统计只读 Go 已聚合的有界投影并在低频队列执行；Artifact/series/points/rows与浏览器渲染均有硬上限，绝不扫描无界 raw Event 或进入核心热路径。

### 稳定性

1. 所有 owner 唯一：Edge/P4、Go/core DB、Model Manager、Plugin Manager 各自只有一个事实链。
2. WAL/journal/commit-before-ACK 防止崩溃丢失和 ghost effect。
3. generation/incarnation/route/bank/selector/claim fence 拒绝旧结果和 ABA。
4. response loss 不盲重试；readback + same-operation reconcile。
5. 双bank防止半策略；model pool current/previous generation CAS提供exact人工rollback，同generation同profile replica failure不改变模型语义。
6. queue/WAL/batch/retry/deadline/resource 全部有上限；过载背压或显式 gap/HOLD。
7. PostgreSQL HA/PITR、应用 expand/contract、startup/readiness/liveness 分离、故障注入和 3,600 秒 soak。
8. 缺失或不兼容在副作用前 fail closed；只有外部副作用已尝试但结果未知才用 `unknown`。
9. target assignment 使用不可复用 lease/monotonic expiry 与有界 election range；新 assignment 的 election floor 严格高于旧 range，旧 actor revoke/expiry 后只能只读。assignment、actor epoch、P4 election/application generation 与 pipeline/P4Info 是独立 fence；fleet parent 始终从完整 child vector 投影，任何 child `unknown` 都使 parent `reconciling`。
10. Central Inference全部不可用时，Edge只使用既有有界input WAL、backpressure和exact gap/HOLD；禁止正常值填充、Edge-local或CPU↔CUDA/异模型自动fallback。P4 forwarding、已安装规则和独立effect recovery不受推理不可用强行回滚。
11. 统计插件、Host或Manager故障只使对应统计stale/unavailable；Go保留有界历史并拒绝old-generation late result，核心原生统计、检测、处置和非插件页面继续。

### 兼容性

1. `contracts/` 是跨语言唯一源，OpenAPI/Protobuf/JSON Schema/WIT/profile 全部版本化和 digest-pinned。
2. normalized firewall policy 与 target compiler 分离，未来硬件复用业务层但独立资格化 P4 adapter。
3. Gateway/Triton/runtime与bundle/feature/label/output adapter分离；模型与CPU/CUDA runtime在新pool generation启动前显式绑定，wire major、backend、hardware和numeric profile分别通过old/new matrix兼容。
4. plugin manifest/kind/runtime 分离；新增 kind/副作用必须升级基线，不能靠万能 Hook。
5. Web generated client 和 current/previous Go/Web matrix；未知 major 拒绝写入。
6. legacy 只做 clean-room observable behavior/golden，不依赖旧源码/schema/runtime。
7. stable `target_id` 与 endpoint/device ID/hostname/serial 分离；不同 target/P4 architecture 通过版本化 capability/profile/adapter 资格化。
8. gNMI 首期仅在 exact profile 下提供只读 `Capabilities/Get/Subscribe`；`Set`、gNOI 与完整 NMS 能力不得作为兼容 fallback 偷渡。
9. `plugin-statistics/v1` 将metric/quality/table/display和资源版本化；宿主固定renderer拒绝unknown major/显示kind，因此插件与Web可以独立滚动，不需要加载第三方前端代码。

### 资格声明不混写

所有证据按`qualification-evidence/v1`分开保存`level/applicability/result/qualification`，并绑定runtime、availability、deployment tier、topology和制品digest。`REHEARSAL/NOT QUALIFIED`、`MODULE PASS`只是推导展示；CPU PASS不替代CUDA，single-domain的1..N域内副本不产生HA声明。`operational-single-domain` 可以完成整套 System E2E，但不能产生 `level=PRODUCTION`；只有 `production-ha` 在全部生产门禁通过后才可能 production qualified。性能waiver保留原始非PASS，只能到明确的最高非生产level；绝对production容量/HA门槛不能被豁免。

首期本地/CI正式E2E由`e2e-runner-compose/v1`启动、等待、采证和清理；Compose healthy与Playwright URL可达只说明可以继续测试。流量场景精确绑定一个mode/backend/fixture/topology/direction/rewrite，缺失时HOLD/NOT_RUN，不自动换runner或工具。

## 8. 成熟方案复用边界

| 复用 | 结论 |
|---|---|
| p4c、BMv2、PTF、P4Testgen/P4Tools | 复用编译、软件 target 和测试能力；项目自有 policy/governance/journal/readback |
| P4Runtime 1.4.1 | 复用每 target runtime/arbitration/read/write；Edge actor 仍独占 session，协议不提供跨 target 原子性或完整设备管理 |
| OpenConfig gNMI/gNOI | gNMI 仅条件只读；gNOI/`Set` 首期拒绝，避免形成第二类设备副作用与授权链 |
| Stratum | 条件作为 exact target-side P4Runtime/gNMI 实现；不作为第二 controller，也不自动授予硬件兼容性 |
| NetBox/外部 CMDB | 只作 candidate inventory；Go 展示 diff、Admin 确认后才写 canonical registry，外部 current/delete 不自动生效 |
| Ansible Network/Nornir | 仅离线 provision、测试或只读核验；不进入实时 effect/wave/current，不持有生产 P4 writer credential |
| ONOS/厂商 controller | 拒绝作为生产 writer/scheduler；否则形成第二拓扑/intent/P4 session 所有权 |
| p4-constraints | 条件 CI/preflight defense-in-depth；不下发、不授权、不进生产 CLI |
| UFW/nftables/iptables | 只可独立保护 Linux host或借鉴语义；拒绝作为 BMv2 backend/fallback/fact/oracle |
| P4 tutorial Bloom firewall | 只作教学参考；碰撞语义不满足 exact enforcement |
| gRPC | 复用 HTTP/2、mTLS、deadline/retry primitive；项目自有 batch identity、WAL、fence、quota 和 logical pool route |
| Triton Inference Server | `ADOPT` 为中央 pool 内固定执行器/dynamic batcher；`model-control-mode=none`，不拥有 model current、route、rollout或外部API |
| ONNX Runtime CPU/CUDA | 两个启动时互斥的显式Triton backend profile；项目自有model contract/binding/rollout，CPU↔CUDA或TensorRT只允许显式资格化新generation，禁止自动fallback |
| KServe/Ray Serve/TensorFlow Serving/MLflow serving | 只参考模式或离线证据，拒绝成为第二 serving/model/route 控制面；Kubernetes只作基础设施adapter |
| PgBouncer/Patroni/pgBackRest | 复用连接池/自建 HA/PITR；PostgreSQL facts和项目恢复门禁自有 |
| OTel/Prometheus/Alertmanager/Grafana | 复用 telemetry/告警/只读 dashboard；不拥有业务事实或 mutation |
| OTel Metrics/Grafana DataFrame/ECharts dataset/Backstage extension pattern | 复用统计语义、数据/视图分离和宿主定义扩展点；Go/PG仍拥有投影，Web只运行固定renderer，拒绝插件UI代码、任意Vega/ECharts option和高基数Prometheus事实 |
| Vue ecosystem | 复用 router/query/UI/chart/test/codegen primitive；业务控制面 clean-room 实现 |
| Cosign/Syft/Trivy | 复用签名验证/SBOM/扫描；不替代功能、许可证或资格证据 |

BMv2的官方定位、P4 tutorial的Bloom collision、p4-constraints的library/CLI边界和P4Tools用途分别见[BMv2 README](https://github.com/p4lang/behavioral-model#readme)、[P4 firewall exercise](https://github.com/p4lang/tutorials/tree/master/exercises/firewall#readme)、[p4-constraints README](https://github.com/p4lang/p4-constraints#readme)和[P4Tools](https://p4lang.github.io/p4c/p4tools.html)。多设备边界参考[P4Runtime 1.4.1](https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html)、[OpenConfig gNMI](https://openconfig.net/docs/gnmi/gnmi-specification/)、[Stratum](https://github.com/stratum/stratum)、[ONOS](https://github.com/opennetworkinglab/onos)、[NetBox](https://netbox.readthedocs.io/en/stable/introduction/)与ADR-0015。中央推理采用边界参考[Triton optimization](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/optimization.html)、[model configuration](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_configuration.html)、[model management](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html)、[secure deployment](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/deploy.html)、[gRPC performance](https://grpc.io/docs/guides/performance/)、[ORT execution providers](https://onnxruntime.ai/docs/execution-providers/)和[ORT CPU threading](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)，具体取舍见ADR-0017；ADR-0016只保留v1.13历史。

## 9. 实现和集成顺序

1. 冻结 contracts/profile/golden/error/resource/test boundary；首批包括 target/fleet、telemetry、central inference wire/pool、effect、firewall、rule observation、model、plugin/statistics、Web。
2. 九个qualification target分别完整实现并通过Module DoD：P4、Edge、C++、Go、PostgreSQL、Plugin Host、Analysis、Web、Offline ML。每个可部署模块的black-box E2E必须真实启动自身发布候选binary/OCI和实际runtime；Central Inference分别真实启动CPU/CUDA profile形成独立证据。
3. Module Complete前可以做隔离wire rehearsal，但只能标`REHEARSAL/NOT QUALIFIED`；模块隔离可使用邻居contract fake，但被测模块不能fake。
4. 所有模块同时Module Complete后，才按`TEST-004`依次做十二个正式pairwise：P4/BMv2↔Edge、Edge↔Gateway、Gateway↔pinned Triton/selected ORT、Go Model Manager↔deployment adapter/Central rollout、Edge↔Go、Go↔PostgreSQL、Go Plugin Manager/Statistics↔Host、Host↔Host-managed service/Wasm statistics conformance plugin、Go↔Web、Analysis↔MCP、Go/peer↔Analysis A2A、Go effect dispatcher↔Edge↔P4；每个边界参与侧都在干净环境真实启动，禁止fake任一侧。Analysis 的两个 pairwise 不经过 Host；System E2E 则同时启动并分别验证 Host、statistics conformance plugin 与 Analysis。target/fleet parent-child、wave和partial vector在适用边界内验证，不另造pairwise queue。
5. 再按数据面、检测面、模型控制、事实链、可视化、Target/Fleet、反向处置/防火墙、规则生效性、插件扩展平面（含统计投影）、Agent旁路共十个波次做system E2E。统计场景真实启动deterministic conformance statistics plugin、适用Host/direct adapter、Go、PostgreSQL和Web；正式完整链真实启动BMv2/P4Runtime、Edge、所选Central Inference stack、Go、真实PostgreSQL、Plugin Host、Analysis Plugin与Web，内部服务不得fake。
6. fault、traffic replay、绝对 benchmark、3,600 秒 soak、三机/HA/PITR、安全/供应链/兼容矩阵完成后，指定 exact digest/profile 才可能获得 production qualification。

所以，需求并不是“先把所有代码拼起来，再补E2E”。正确顺序是先冻结边界，各模块真实启动自身并做到可测、可恢复、性能达标；全部完成后再在干净环境真实启动pair和整套系统正式集成。早期fake/rehearsal用于发现wire问题，但永远不能改名为正式E2E PASS。

## 10. 当前仍需后续冻结的值

- 目标实验/生产 CPU、包率、target 数、flow/window/Event rate 与端到端 p99；
- `max_targets_per_edge/control/fleet_operation/wave`、parallel child、每 target/global P4 RPC/StreamChannel/FD/task/journal/queue/DB/UI 绝对预算；
- BMv2 firewall normalized→compiled 最坏展开、bank/overlay/counter 精确配额及 selector visibility 测量；
- firewall + telemetry + inference + observation 叠加绝对容量；
- PostgreSQL 目标规模、RPO/RTO/restore RTO；
- Central Inference CPU与CUDA各自的目标硬件、core/thread/NUMA/RAM或GPU/driver/VRAM、batch/queue/concurrency、network、startup/rolling/unavailable/buffer、single/HA与N+1资源门槛；
- Web 目标工作站/网络/browser 的实际 bundle/CWV/heap/soak结果。
- plugin statistics schedule最小间隔、on-demand rate/global concurrency，以及初始32 definitions/128 KiB definition block/2 MiB input/1 MiB Artifact/64 series/10,000 points/2,000 rows profile在目标Go/Host/DB/Web环境的绝对容量与render门槛。

这些值未冻结或未实际执行前，对应证据为`result=HOLD|NOT_RUN, qualification=NOT_QUALIFIED`；文档存在、工具成熟或本地 demo 成功都不能替代证据。
