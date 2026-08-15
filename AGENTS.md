# AGENTS.md

本文件适用于整个 MASI-NIDS-vNext 仓库，是日常开发、评审和自动化代理必须遵守的稳定工程规则。

## 需求与项目定位

- 唯一需求内容基线是 `docs/masi-nids-vnext-system-requirements-2026-08-09.md`。设计、实现、测试和验收必须引用其中稳定的需求 ID；只有受保护提交/tag 与文档 digest 均形成后，才可称为可审计发布基线。
- 本项目是独立的 greenfield vNext 重写，不是对旧 MASI-NIDS Python 项目的逐文件翻译。
- 旧仓库只能作为行为、数据和性能基线参考；禁止直接依赖旧仓库源码、运行目录、数据库 schema 或历史控制文档。
- 需求基线的强制语义只能由 Owner 明确修改。代码现状、测试便利或临时联调不能改变需求。
- 当前仓库处于初始化阶段；在形成可复核证据前，任何模块或系统状态都不得标记为 `PASS` 或 production qualified。

## 目标目录

```text
contracts/       跨语言契约的唯一源
p4/              P4_16 程序、P4Info 与 target profile
edge-rs/         Rust Edge Agent
infer-cpp/       C++ Central Inference Gateway 与 CPU/CUDA Runtime 适配
control-go/      Go Control Core
plugin-host-rs/  Rust Plugin Runtime Host
analysis-py/     Python Analysis Plugin
ml-py/           Offline ML Artifact Pipeline
web/             TypeScript/Vue 3/Vite Frontend
db/migrations/   PostgreSQL schema 与 migration
testkit/         golden、traffic fixtures/replay、fake server 与故障注入
deploy/          单机、三机（远程中央推理）与生产部署资产
docs/            需求、架构、ADR 和验收文档
```

不得为了填充目录而添加占位实现；目录在对应模块正式启动时创建。

## 不可破坏的架构约束

- PostgreSQL 是核心持久事实的唯一来源。
- Rust Edge Agent 是唯一长期 P4Runtime session/StreamChannel owner，也是唯一生产 P4Runtime read/write client；PTF/P4Testgen/Tcpreplay/Mininet/netem/流量发生器只允许 ADR-0012 定义的隔离 test，P4Runtime Shell 另可用于明确的 production read-only 诊断 profile；它们均不得成为第二 writer/scheduler、生产发包服务或 effect path。
- 多交换机能力必须遵守 ADR-0015：Go Control 内的 Target Registry/Fleet Coordinator 拥有 stable target/assignment/fleet parent-child 控制事实，Rust Edge 在一个进程内以每 target 独立 TargetActor 持有 StreamChannel/mastership/journal/source/queue。assignment 必须有不可复用 lease/monotonic expiry 与有界 election range；revoke/expiry 后旧 actor 只能 read-only，新 assignment 使用严格更高 election floor。fleet parent 不可 claim，实际副作用仍是现有 per-target `effect_intent`；不存在跨 target 原子提交、第二 fleet queue 或第十个在线模块。
- P4Runtime 仍是生产 P4 table 读写协议；条件 `target-gnmi-readonly/v1` 只能使用 exact OpenConfig `Capabilities/Get/Subscribe` allowlist，首期禁止 `Set`/gNOI mutation。Stratum 仅可作为 target-side profile，NetBox/CMDB 仅产生待审 candidate，Ansible/Nornir 仅离线 provisioning/test/read-only；ONOS、厂商 controller、P4Runtime Shell daemon 不得取得 production writer/scheduler 所有权。
- 首期防火墙是 ADR-0014 定义的 BMv2 `simple_switch_grpc`/v1model IPv4 无状态软件 profile：高优先级、有 TTL 的 response overlay 与长期 baseline 双 bank/selector 共用唯一 effect queue/Edge writer/readback/CAS；baseline revision 由 scoped Platform Admin 创建、不同 Operator step-up 授权 typed R3 activation。UFW/nftables/iptables 不是 P4 backend、fallback、事实源或 outcome oracle；IPv6 effect、stateful/NAT/rate-limit/硬件 target 未有新 profile 与证据前必须 `HOLD/unsupported`。
- 首期默认在线遥测源是 `telemetry-p4-window/v1` 的 P4 有界聚合与目标资格化 bank/epoch/snapshot；P4Runtime Digest/PacketIn 只可作为明确标注丢失语义的补充 sample/hint，不能作为完整逐包流。模型确需报头/流重组且 P4 特征不足时，才允许 Edge 内条件启用受控 mirror capture；它不得形成第二 deployable collector、第二 canonical source 或 raw-payload 默认路径。
- Edge 同时拥有 capture adapter、event-time final window、source/input/result WAL、回压与 canonical cursor；在线推理首期唯一生产路径是版本化 `inference-central-grpc-batch/v1`，由 Edge 通过复用连接的异步、有界、mTLS batched-unary gRPC 请求 Central Inference pool。Edge 不运行本地 C++/CPU inference，`inference-shm-spsc/v1` 与 `inference-uds-batch/v1` 不得作为 production fallback。禁止逐记录 JSON/HTTP、无界队列，以及用 Gateway/Triton/runtime 成功、RPC 返回或 Digest Ack 替代 PostgreSQL Event commit 后的 canonical ACK。
- Go Control Core 是核心 PostgreSQL schema 的唯一业务写入者。
- Go Control 内的 Model Manager 是 model revision、qualification、model-control incarnation、central pool generation、rollout/recovery operation、per-shard desired/current/previous binding、routing/deployment/startup/readback/commit observation 和 rollback 控制事实的唯一写入者；central Inference replica 只按 immutable startup envelope 启动并读回一个 exact model/runtime binding，不连接 PostgreSQL。
- 受控通用插件平台是首期必需模块：Go Plugin Manager 是 catalog、qualification、activation、binding 和 revocation 控制事实的唯一写入者；独立 Rust Plugin Runtime Host 只执行已准入的 Wasm/显式 Host-managed service plugin。独立 service/Agent（包括官方 Analysis）仍受 Manager 控制，但业务 A2A/MCP/gRPC 通过 typed adapter 直连，不经 Host 代理。
- 插件统计是 `plugin-statistics/v1` 输出 capability，不是新 kind 或新模块：每个统计项以 immutable `PluginStatisticsDefinitionV1` 随 plugin revision 资格化，只能引用 host-owned input projection/field registry；`read-only-tool` 可额外引用 manifest/binding 已批准的 external-source capability ID，但 definition 禁止 endpoint/credential、运行时注册、SQL/PromQL/JSONPath/MCP prompt/URL/表达式。Go 唯一拥有 canonical input freeze、schedule/run/idempotency、Artifact validation、`plugin_statistics` PostgreSQL current/history 与 read/export authorization；`plugin_statistic_runs` 是唯一 durable run ledger，只有 Go Control 内从其有界派生的 statistics dispatcher 可按 CAS/fence 推进，不增加独立 broker/scheduler service 或第二 durable queue。on-demand 必须同时验证 source-read 与 `plugin.statistics.run`；schedule create/revise/disable 采用 immutable append-only revision，并验证 scoped Platform Admin、data-class、CSRF/适用 step-up、幂等与审计，每次执行重新验证 binding/revocation/policy/scope/external capability。`pure-transform` 是默认 producer，`read-only-tool` 仅按明确只读 capability 使用。插件不得自调度、扫描/写数据库、直写 Prometheus、更新核心统计或向浏览器注入 route/component/action/HTML/JavaScript/ECharts/Vega 配置；Web 只使用内置声明式 renderer 和平台固定 `Run now`/schedule 控件。统计插件不可用时核心原生统计与非插件页面必须继续。
- Central Inference 模块由无状态 C++ MASI Gateway、固定资格 profile 的 Triton server、启动前显式选择的 ONNX Runtime CPU 或 CUDA backend，以及 digest-pinned model repository snapshot 组成一个资格主体；它不连接 PostgreSQL、不访问 P4、不拥有业务状态或 durable queue。Gateway 只做合同/身份/配额/结果适配，不实现第二个延迟型 batch scheduler；Triton 只拥有 replica 内 batching/execution，不拥有 model current、canonical route、Event 或 effect。
- 在线检测模型采用“Central Inference 唯一执行面 + immutable model bundle + versioned feature/label/output/wire/runtime contract + Go-owned per-shard exact binding”。首期固定 Triton `model-control-mode=none`、只读 repository、严格 readiness，并由 immutable startup envelope 显式二选一绑定 `model-runtime-central-cpu/v1` 或 `model-runtime-central-cuda/v1`；生产禁止 Edge-local、旧模型或未声明 backend 的故障回退，也禁止 CPU/CUDA 按探测或请求失败自动互切、KServe/Ray Serve/负载均衡器成为第二 model control/router。硬件探测只验证并报告人工选择，profile/resource/readback 不符必须 startup/readiness fail closed。相同 pool/binding generation 的 active-active replica 可以在 bounded retry budget 内重算同一 request identity；跨 generation/model/runtime profile 必须提升 route epoch并走新 rollout。Edge 仍是每个 shard 唯一 canonical input router，deployment/Kubernetes status、PDB、Triton repository 或 hook 不是 route/current 事实。
- 模型 revision 不是 plugin revision，模型包不得包含未资格化 custom native operator、Python/Wasm hook 或其他可执行扩展。生产 candidate/online shadow/运行期 repository POLL/load-unload 禁止；feature/runtime major 变化必须走滚动兼容门禁。全部 central replica 不可用时仅允许 Edge 既有 WAL 域内有界等待、背压、`HOLD/unavailable` 与精确 `gap`，不得启动本地推理；rollback 只能是指向已资格化 exact previous 的新 durable operation。
- 无损 PostgreSQL failover 保留 `model_control_incarnation_id`；PITR/restore/clone/rewind 必须在开放 writer/ingest 前轮换从未使用的新 incarnation，并以新 operation/new per-shard generation 重验 exact current。旧 envelope/action/readback/handshake 和同数字 generation 必须被 fence。
- Python Analysis Plugin 是平台首个官方 `analysis-agent` 插件，必须经过同一 manifest、qualification、activation、fence 和 revoke 门禁；它是独立进程/容器，通过 direct A2A/MCP typed boundary 工作而不经 Runtime Host，只写自身 plugin schema，不得写核心事实或执行设备副作用。
- Frontend 是独立 Vue 3/Vite SOC SPA，只通过同源 Go Control `/api` 与 `/events` 访问系统，不直连 PostgreSQL、P4、Rust、Central Inference/Triton、Plugin Host、LLM/MCP/A2A provider；Go/受信网关拥有 OIDC callback/session/CSRF 和业务授权，浏览器不承担 BFF 或持有 token/secret。
- LangGraph、LLM 只存在于 Analysis Plugin；MCP、A2A 只用于已声明的插件/Agent 协议边界。四者均不得进入实时检测或真实处置闭环，其产物必须不可执行。
- 新模型、新标签或更高置信度不得自动获得 effect eligibility；只有绑定 exact model/feature/label/output-adapter digest 的确定性 policy 通过独立资格后，才可参与既有 effect 治理链。
- “通用插件”只表示统一 manifest/catalog/capability/lifecycle/test，不表示任意 Hook 或统一业务协议。首期 kind 封闭为 `analysis-agent`、`read-only-tool`、`pure-transform`；新增 kind 或副作用必须提升需求基线。
- 禁止把第三方代码动态加载进 Go/Rust/C++ 核心进程。Service/Agent 插件必须进程外运行，Wasm 只能在独立 Host 中按版本化 WIT 和最小 capability 运行；Host 不是独立 Agent/service 的统一业务代理。Plugin Manager、Host 和插件均不得成为第二 effect queue/P4 writer 或核心热路径依赖。
- 旧 LangGraph 只做外部可观察行为兼容；禁止迁移 legacy Workflow/checkpoint/schema 或节点源码，在 `AGENT-COMPAT-001` 矩阵与黑盒证据完成前不得声称完整兼容。
- Effect Proposal/Authorization Decision 是 Go/PostgreSQL 中不可执行的治理事实；只有 `effect_intents` 可被 claim，禁止把 proposal/decision 变成第二 effect queue。
- 规则“生效性”必须分层：effect execution、exact entry installation readback、per-entry direct-counter dataplane match、独立 packet/action outcome evidence 分开。counter 增长不证明 action 成功或攻击终止；no hit、no eligible traffic、stale/reset/gap/not measurable 不得混为 0%/失败。
- 测试流量必须按 `CONTRACT-TRAFFIC-001` 分为 generated packet、synthetic flow、curated PCAP L2 replay 和 live session；requested/sender/test-ingress/DUT/counter/outcome/detection 证据分开。BMv2/Mininet PASS 只覆盖 exact software target，公开 corpus 未通过许可证/隐私/ground-truth 门禁不得进入仓库或制品。
- Analyst 只能研判和提出 proposal；Operator 只能在明确 scope/risk 内授权；Platform Admin 管理 target lifecycle/assignment 与已资格化 model/plugin exact binding，并可创建 immutable baseline firewall revision，但不隐含 Operator 权限。临时 R2 是 Analyst/Operator maker-checker，baseline R3 是 Platform Admin/不同 Operator maker-checker。
- Redis 不是首期依赖。未来若引入，只能承载可丢失、可从 PostgreSQL 重建的加速数据。
- 真实处置必须遵守 `durable effect intent -> claim/fence -> 无事务 Edge RPC -> P4 journal/readback -> PostgreSQL CAS finalize`。
- 不得在数据库事务或持有连接期间等待 P4、LLM、MCP、A2A、HTTP 或 gRPC 外部结果。
- 副作用前缺失、过期、损坏、不兼容或未验证的数据必须 fail closed 为 `HOLD/stale`；只有已尝试外部副作用但结果未确认时才使用 `unknown`。
- 队列、WAL、spool、batch、响应、分页、连接、并发、重试、超时、日志和 retention 必须有显式上限。
- 禁止第二 P4 writer、第二 effect queue、旧新双写、隐式状态机和跨模块共享可写 volume。
- 通用机制优先复用经 `ARCH-REUSE-001`/ADR-0009 资格化的成熟组件，但组件不得取得核心事实、effect、authorization、P4 或 rule observation 所有权。每项必须登记 `ADOPT/CONDITIONAL/REJECT`、exact digest、license/SBOM、权限、故障、兼容、回滚和退出；禁止用成熟度为第二控制面开例外。

## 技术职责

| 模块 | 技术栈 | 核心职责 |
|---|---|---|
| Switch | P4_16、BMv2 | 转发、IPv4 无状态 response overlay 与 baseline 双 bank/selector、目标资格化有界聚合/bank/epoch/snapshot、补充 sample hint、执行并读回 P4 状态、规则 direct/eligible counter |
| Edge | Rust stable、Tokio、Tonic/Prost | TargetSupervisor + 每 target 独立 actor；唯一 P4 会话与 canonical source/router；firewall normalized-plan 编译/preflight/双 bank 激活/reconcile、capture adapter、event-time final window、source/input/result WAL、central-gRPC client、bounded admission/retry/backpressure、effect journal/readback、bounded rule observation sweep |
| Inference | C++20/23 Gateway、固定 Triton、ONNX Runtime CPU/CUDA | Central Inference 同代 replica；mTLS batched request admission、合同/identity/result fence、Triton 动态 batching、启动前显式 CPU/CUDA profile、exact loaded/runtime/hardware readback、数值与性能资格；不抓包、不组流、不推进 canonical cursor |
| Control | Go stable、gRPC-Go、pgx、sqlc | Target Registry/Fleet Coordinator、Event/Incident、策略、firewall revision/binding/typed activation、effect proposal/authorization、durable effect、rule observation/rollup、Model Manager、Plugin Manager、Plugin Statistics freeze/run/validation/projection、API |
| State | PostgreSQL 18、PgBouncer | 事务、幂等、CAS、target/assignment/fleet parent-child、firewall/model/rule observation、plugin statistics current/history、分区、retention、HA/PITR |
| Plugin Host | Rust stable、Tokio、Tonic/Prost、固定受支持 Wasmtime、首期资格 profile WASI 0.2 | 插件隔离执行、capability/resource/deadline/fence、service/Wasm lifecycle；WASI 0.3 已稳定但未通过项目矩阵前不得自动升级 |
| Analysis | Python 3.12+、LangGraph、MCP、A2A | 首个官方插件；有界的证据分析、建议和内容 Artifact |
| Offline ML | Python 3.12+、PyTorch/NumPy/scikit-learn 或已资格化工具 | 训练、评估、导出 immutable model bundle、golden 与 qualification evidence |
| Web | TypeScript、Vue 3、Vite、Vue Router、Pinia、Element Plus、ECharts | 独立 SOC SPA；Managed Targets/Fleet Operations、真实状态、历史、response/baseline policy diff/审批/激活、规则安装/命中/结果、插件运维、固定声明式统计 renderer 与 Analysis Artifact 展示 |

一种职责只能有一个长期在线实现。不得为了“使用多语言”重复实现同一所有权。

## 契约规则

- `contracts/` 是 Protobuf、OpenAPI、JSON Schema 和 WIT 的唯一源；model bundle、feature schema、label taxonomy、output adapter 与 binding 同样必须在公开 contract/profile 中定义；跨语言类型必须生成或通过有测试的显式适配器映射。
- `contracts/profiles/v1` 必须固定 OpenAPI/JSON Schema/P4Runtime/P4 target fleet/条件只读 gNMI/P4 stateless firewall/P4 rule observation/P4 traffic replay/telemetry source+window/central inference gRPC+A2A/MCP/WASI/WIT/gRPC/plugin statistics+display/Web SPA/browser/model runtime+rollout/deployment tier/availability/E2E runner/qualification evidence/performance/supply tooling 的项目资格 profile、工具链和 digest；model/inference profile还必须分别固定 `model-runtime-central-cpu/v1` 与 `model-runtime-central-cuda/v1` 的 wire、Triton/ORT/EP、CPU feature/thread/NUMA/RAM 或 CUDA/cuDNN/driver/GPU/VRAM、batch/queue/copy、incarnation/pool/route/worker/commit/retry和绝对容量；上游`latest`、浏览器current或“stable”不自动获得项目资格。
- 资格证据必须把 `level`、`applicability`、`result` 与 `qualification` 分列；`REHEARSAL/NOT QUALIFIED` 只能是这些字段推导出的展示摘要，不得作为混合 wire 状态。所有 PASS 声明必须绑定精确 runtime、availability、deployment tier、拓扑和制品 digest。
- `contracts/target/v1` 固定 stable target/lifecycle/assignment/capability/fence，`contracts/fleet-operation/v1` 固定 target-set/wave/per-target child/parent projection；`contracts/p4/firewall-policy/v1` 固定 normalized policy/revision/default/priority/fragment、response overlay、compiled plan、bank/selector/operation/readback/unsupported；`contracts/p4/rule-observation/v1` 固定 rule/counter/generation/epoch/sample/quality/formula/outcome；`contracts/telemetry/v1` 固定 source/observation point/flow identity、event-time window/watermark/lateness、generation/epoch/sequence、quality/gap/sampling；`contracts/inference/v1` 固定 batched gRPC request/result、pool/binding/worker identity、deadline/retry/digest/conflict、model/result fence 与 canonical ACK identity；`contracts/testkit/traffic-replay/v1` 固定 fixture/source/ground-truth、topology/direction/rewrite、timing/rate/impairment/resource/oracle/evidence；`contracts/supply-chain/v1` 固定全系统 component adoption registry、third-party inventory 和源码来源/修改登记。
- `contracts/plugin/statistics/v1` 固定 immutable definition、host-owned projection/field 与 approved external-source capability allowlist、Go-frozen input、run/idempotency、metric/series/table/status/quality/provenance/truncation、封闭 display union 和所有 definitions/bytes/points/rows/cardinality/deadline 上限；Go/Rust/Python/TypeScript/Wasm 必须共用 golden，definition endpoint/credential、运行时 registration、SQL/PromQL/JSONPath/表达式、未知 projection/field/capability/display/metric/profile major、NaN/Inf、代码/HTML/URL/任意 ECharts/Vega payload 必须拒绝。
- 模块只能依赖版本化公开契约，不得导入其他模块内部源码或复制同名结构后独立演化。
- 未知 major、plugin kind 或 runtime profile 必须拒绝；minor 兼容必须由契约测试证明。
- 跨模块消息必须按适用范围携带 schema version、稳定 identity、generation/session、sequence、时间戳、长度、digest、状态和 trace ID。
- 热路径禁止逐记录 JSON、逐记录 HTTP 和逐记录数据库事务。
- `testkit/` 可以提供契约一致的 fake/mock，但不得包含或替代被测模块内部业务实现。

## 开发顺序与全局门禁

1. 先冻结契约、golden vector、错误语义、资源上限和模块黑盒测试接口。
2. 各模块独立完整实现，并分别通过语言级验证、契约测试、黑盒 E2E、故障恢复、性能、镜像和文档门禁。
3. 只有所有首期模块同时达到 Module Complete，才允许开始正式 pairwise integration；任一模块未完成时全局状态为 `HOLD`。
4. 正式 pairwise 按十二个边界依次进行：（1）P4/BMv2↔Rust Edge（含1/2/N target actor、firewall双bank/selector/response overlay、P4 aggregate snapshot、supplemental sample loss、`p4-traffic-replay/v1` packet oracle、rule counter与sender/DUT分层）；（2）Rust Edge↔C++ Gateway（mTLS/framing、bounded batch/admission、same-generation等价副本有界重试、model identity/result fence、无本地fallback）；（3）C++ Gateway↔pinned Triton/selected ORT CPU或CUDA；（4）Go Model Manager↔deployment adapter/Central Inference rollout（pool generation、readback与逐shard rollout）；（5）Rust Edge↔Go Control（Event/result/observation batch与commit ACK）；（6）Go Control↔PostgreSQL（core/model/plugin/target/fleet/firewall/rule facts与migration）；（7）Go Plugin Manager/Plugin Statistics↔Rust Plugin Host；（8）Host↔Host-managed service/Wasm statistics conformance plugin；（9）Go↔Frontend（Managed Targets/Fleet Operations/Firewall Policies/Model Operations/Inference Pool/Rule Effectiveness/Plugin Statistics）；（10）Analysis Plugin↔MCP；（11）Go/peer↔Analysis Plugin A2A；（12）Go effect dispatcher↔Rust Edge↔P4 per-target readback/outcome oracle。
5. 系统按数据面、检测面、模型控制、事实链、可视化、Target/Fleet、反向处置/防火墙、规则生效性、插件扩展平面、Agent 旁路共十个波次拼装；不得省略或合并会掩盖独立所有权与验收边界的波次。
6. 集成发现模块缺陷时，该模块立即退出 Module Complete；完整重跑自身门禁后才能恢复集成。

契约设计、generated client、fake server 和 test harness 可以提前协作。还可以在隔离 test identity/database/fake target 上进行无生产副作用的真实 wire/boundary rehearsal，以提前验证 TLS、UDS、framing、codegen、deadline 和 cancellation；所有这类结果必须标记 `REHEARSAL/NOT QUALIFIED`，不得转用为 Module、pairwise、system 或 production PASS。实现阶段必须真实启动每个被测可部署模块及所选 CPU/CUDA runtime；正式 pairwise 与 system E2E 必须在干净环境启动参与链路的真实服务并通过真实公开网络、P4/BMv2 和 PostgreSQL 边界运行。fake/mock 只能补充未接入依赖的模块测试、故障注入和确定性 provider fixture，不能替代正式链路中的相关服务或获得正式 PASS。

## Module Complete / Definition of Done

模块只有同时满足以下条件才可标记完成：

- 首期需求全部实现，无必需功能 TODO、placeholder、硬编码成功或临时兼容路径；
- format、lint、static analysis、unit/property、contract/golden 全部通过；
- 被测模块使用其实际 OCI/binary 与所选 runtime profile 真实启动，并通过公开边界运行的独立黑盒 E2E；仅有 fake/mock、readiness、microbenchmark 或 rehearsal 不算通过；
- crash、timeout、duplicate、乱序、断网、超限、磁盘/连接耗尽等故障恢复通过；
- 性能基线和资源预算通过；Owner 例外不得记作性能 PASS，必须记录精确范围、风险、期限、补救与最高允许资格级别，且绝不允许把未达到的绝对生产性能门槛豁免为 production qualified；
- 对 P4/Edge/Go/Web，规则 installation/match/outcome 分层、reset/gap/generation/no-traffic 语义及最大 4,096-rule 初始 profile 已按自身边界通过；
- 对 P4/Edge/Go/PostgreSQL/Web，normalized firewall policy、response overlay、baseline 双 bank/selector、R3 maker-checker、readback/reconcile/rollback、host-filter-negative evidence 与 0/128/1,024/4,096-rule 绝对软件性能门槛已按 `TEST-P4-FW-001` 通过；
- 对 P4/Edge/Go/PostgreSQL/Web，stable target/assignment/TargetActor、fleet parent+per-target intent、static waves/partial/reconcile、PITR target-control incarnation、0/1/2/N故障隔离/公平与绝对容量已按`TEST-TARGET-FLEET-001`通过；条件gNMI/Stratum/NetBox/Ansible/Nornir未触发时必须以独立 `applicability=NOT_APPLICABLE` 和稳定理由记录；
- 对 P4/testkit，generated/synthetic/curated/live-session fixture、方向/rewrite/timing/netem/offload、sender/DUT/counter/oracle、资源/故障/清理、软件/硬件 target 和 corpus license/privacy/ground-truth 已按 `TEST-TRAFFIC-001` 通过；
- 对 P4/Edge/Inference/Go，telemetry source profile、aggregate snapshot/sample loss、flow identity、event-time finalization、late/gap/reset、central mTLS gRPC contract、Triton dynamic batch/queue 上限、same-generation等价副本有界重试/no-fallback、source/input/result WAL、result fence、PostgreSQL Event commit ACK、cursor/compaction 与 mirror capture 条件矩阵已按 `TEST-TEL-INF-001` 通过；
- 对 Offline ML/Inference/Go/Edge/Web，model bundle、feature/label/output/wire、CPU/CUDA runtime contract、人工选择+硬件preflight、central startup exact binding、model-control incarnation、pool/binding generation、Edge per-shard唯一route、current/previous、WAL buffer/gap、deployment action/termination、readback/CAS/commit/resume、所选availability profile的required/min-ready/restart/quarantine（HA另含failure-domain/N+1）、mixed rollout、rollback、late-result fence、无Edge-local/自动profile fallback和多类别语义已按自身边界通过；CPU与CUDA证据不得互相继承；
- 对 Contracts/Go/PostgreSQL/Plugin Host或direct typed adapter/conformance plugin/Web，插件统计的 immutable definition/host projection allowlist、冻结输入、run幂等、Artifact status/quality/metric/table/display、资源与安全边界、current/history、固定renderer、禁用等价性和真实服务E2E已按 `TEST-PLUGIN-STAT-001` 通过；仅schema、fixture JSON或静态图表不算通过；
- 输入、输出和内部资源有界，异常和未知版本 fail closed；
- OCI image 可独立启动并通过 startup/readiness/liveness；
- 未越权访问其他模块代码、数据、设备或 secret；
- 需求 ID 已映射到测试、命令、环境、证据、制品 digest 和负责人。

`DEC-044` 将“首期实现是否完成”与“精确 scope 是否取得资格”分开：Module Complete 是机器派生的 operational completion，不是第五种资格状态。只有公开契约/错误语义/资源上限已冻结、必需实现无 TODO/placeholder/stub/隐藏 fallback、真实候选 binary 与 OCI 已启动、全部适用的模块公开边界/故障恢复/安全/性能/soak 测试已实际执行且没有测试或启动阻断、append-only findings registry 中 open P0 为 0 时，才可为 `COMPLETE`。实际测试 `FAIL/HOLD/NOT_RUN`、缺失证据、真实启动失败或 open P0 均阻断完成；仅由受保护基线、dirty tree、生产绝对门槛、未来 pairwise/system 尚未取得而产生的 qualification-only `HOLD/NOT_RUN` 不阻断 operational completion，但原 `result/qualification` 必须保持不变且不得宣称相应 `MODULE PASS`、pairwise、system 或 production qualification。条件能力只有独立标记为 `applicability=NOT_APPLICABLE` 且给出稳定理由才可排除。CPU profile 的 PASS 不得替代 CUDA profile，单故障域 profile 的 PASS 不得宣称 HA。

## 语言级最低验证

- Rust（Edge 与 Plugin Host 分别）：`fmt`、`clippy`、unit/property、适用的 sanitizer/Miri、benchmark。
- C++：format/lint、unit、ASan/UBSan/TSan、benchmark、golden numeric tests。
- P4/testkit：p4c/P4Info lint、P4Testgen、PTF packet/counter/action oracle、`p4-stateless-firewall/v1` compile/bank/selector/default/fragment/capacity golden、`traffic-replay/v1` schema/golden、Mininet/netem/BMv2 fault与目标 profile benchmark；条件 p4-constraints preflight 与 L2 replay/live-session profile按资格/许可证/隔离门禁执行；BMv2 PASS 不等于硬件资格。
- Go：format、vet/staticcheck、unit、race、benchmark、真实 PostgreSQL integration。
- Python：支持 Python 3.12+，执行 type/lint、`unittest`、contract/golden/fault；禁止引入 pytest。
- TypeScript/Vue：strict typecheck、Vue lint、unit/component、accessibility、production bundle/performance budget、Playwright E2E。
- Contracts：schema/WIT lint、breaking-change check、跨语言 golden bytes、telemetry/window/central-inference gRPC mapping+numeric golden、plugin statistics input/artifact/display golden 和 plugin conformance。

每个模块必须在自己的 README 中给出可复制的构建、测试和 benchmark 命令。缺少环境、跳过、版本不匹配或证据缺失必须把 `result` 报告为 `HOLD` 或 `NOT_RUN`，不能报告为 PASS。

## PostgreSQL 规则

- 使用精简的 vNext schema，不复制旧系统全部 migration、Workflow、Review、Auth、Event v2 或签名 registry；需求基线定义的 `effect_proposals/effect_decisions` 是新的专用事实，不是 legacy Review 兼容层。
- migration 由独立 job/binary 执行，不允许所有应用实例启动时自动抢跑。
- 已应用 migration 不得修改；生产演进采用 expand/contract。
- 测试数据库名称必须明确包含 `test`；破坏性测试必须显式确认精确目标。
- Event 使用时间分区；热查询字段使用强类型列，JSONB 不代替可索引热字段。
- Plugin Manager 只写 Go 所有的 `plugin_platform` 控制事实；Runtime Host 和通用插件不得获得数据库凭据，Analysis Plugin 仅使用隔离的自有 schema/role。
- Plugin Statistics definition body 只存在于 Go-owned `plugin_platform` immutable manifest revision；`plugin_statistics` schedules/runs/artifacts/current 只引用 exact definition identity/digest并只由 Go 写。Host、插件、Web、Prometheus/Grafana/OTel 均不是该投影 writer/fact source。statistics run 不得成为 effect dispatcher claim source、插件自有队列或独立 broker。
- Model Manager 只写 Go 所有的 `model_platform` 控制事实；Central Gateway、Triton、Edge、Offline ML 工具和外部 model registry/serving 工具不得获得核心数据库凭据或成为 active binding 事实源。
- 无损failover保留model-control incarnation；PITR/restore/clone/rewind必须在开放writer/ingest前轮换新incarnation并逐shard以新generation重验，旧model envelope/action/readback/handshake不能因恢复出同数字generation重新有效。
- per-rule latest/rollup/status/outcome reference 只由 Go 写入 PostgreSQL；Prometheus/Grafana/Edge memory/log 不是规则事实源。5-minute/1-hour rollup 按需求 profile 分区/retention，不能跨 generation/reset epoch 拼接。
- firewall policy revision、current/previous binding、selector/bank、activation operation、response overlay/expiry 与 exact readback reference 只由 Go 写入 PostgreSQL；BMv2 CLI、Edge memory、host firewall、Prometheus/Grafana 不是 canonical fact。
- target registry、assignment generation、target-control incarnation、capability observation、fleet parent/child/wave/current projection只由Go写入PostgreSQL；Edge heartbeat、P4/gNMI连接、NetBox/CMDB/Ansible inventory、Kubernetes或controller状态不是canonical fact。fleet parent不可claim，dispatcher仍只claim`effect_intents`。
- PgBouncer transaction pooling 不服务 migration、LISTEN 或需要 session 状态的连接。
- 只有 `deployment-tier=production-ha` 才能取得 `level=PRODUCTION`/production qualification，并且必须包含 HA/failover、WAL archive、PITR、隔离恢复演练、连接预算和监控证据；`operational-single-domain` 可以完整运行并取得至多 `SYSTEM_E2E` 资格，但不得以名称、域内多副本或 waiver 冒充生产资格。

## 安全与可观测性

- 跨主机 gRPC、HTTPS、MCP、A2A 和 PostgreSQL 必须完整验证 TLS/mTLS 身份，不允许明文 fallback。
- Central Inference 对外只暴露受限 mTLS Gateway inference/readback 边界；Triton HTTP、model-control、repository、backend directory 与内部 gRPC 不得暴露给 Edge、Frontend、插件或公网。首期 Triton 使用 `model-control-mode=none`、禁用 auto-complete、只读且闭包化的 repository/后台目录和严格 readiness；repository snapshot 只能包含 exact binding 及其声明依赖，配置必须显式固定 `instance_group` kind/count/device。启动 envelope 显式选择 CPU 或 CUDA profile；CUDA profile 内仅允许资格化时已声明、冻结并测量的 host-side operator placement，不得把运行期 CPU 接管当作切换 CPU profile。实际硬件/EP/资源不匹配时失败关闭，不得生成另一种 instance或切换其他模型/backend。
- 人类 effect mutation 必须使用受信 OIDC 的稳定 `(iss, sub)`、版本化 scope mapping 和可审计的 exact proposal digest；R2 与 baseline firewall typed R3 activation 需要职责分离与可验证 step-up 认证。
- target activate/endpoint/device_id/role/credential-reference/profile/Edge assignment变更必须使用scoped Platform Admin最近5分钟step-up、exact diff/digest和append-only audit；它不授予Operator/P4 write。fleet R2/R3 effect继续遵守既有maker-checker，并绑定冻结target-set/wave digest。
- Frontend 生产必须同源、严格 CSP、content-hashed asset、HttpOnly/SameSite session、CSRF 和无 token persistence；首期禁止 Service Worker/API offline cache、远程 runtime/CDN、任意 UI JavaScript plugin 和把 route visibility 当授权。
- 插件统计的所有文本/字段按不可信输入处理；Frontend 禁止 `v-html`、动态远程 component、插件提供的 route/action/form、任意 URL/HTML/SVG/CSS/JavaScript、完整 ECharts option 和 Vega 表达式。平台固定的 `Run now`/schedule 控件只能调用同源 Go API，不能由 route visibility 代替 source-read/run/Admin/data-class/CSRF/step-up 服务端授权。CSV 导出必须防 formula injection；Go 按源数据分类与当前 scope 重新授权。
- secret 只能通过 secret file、secret manager 或 workload identity 注入；禁止进入仓库、镜像、CLI、日志、前端 bundle 或 Artifact。
- 生产插件必须使用 digest-pinned OCI/Wasm artifact、受信 Cosign/Sigstore 或组织 PKI publisher policy、SBOM/provenance、可离线重验 bundle 和撤销事实；不得恢复 legacy 自研多域签名发布链。
- 插件日常 exact-binding activation/rollback/revoke 可由一个 scoped Platform Admin 执行；trust root、publisher/builder allowlist、revocation grace 和自动发布 policy 的改变必须由两个不同稳定人类身份保护。Host reconcile、撤销传播和 cache 新鲜度遵守需求基线的显式上限。
- 同机 UDS 仍必须验证受控绝对路径、owner/mode、no-symlink、peer credential、binding/generation、framing 和 deadline；不得把“本机”当作身份或权限证明。
- 外部输入必须在完整解析前实施 framing/body size limit，并防止 SSRF、路径穿越和 symlink 攻击。
- PCAP/PCAPNG 与 traffic manifest 视为不可信且可能含完整 payload、credential、个人数据或 exploit；只能在专用 test host/namespace按严格文件/schema/size/digest、无任意 shell/argv、生产 route/credential deny、资源/retention/精确清理 profile处理，raw packet不得进入普通日志、metrics、Frontend或发布制品。
- 生产在线遥测默认只携带资格化 metadata/aggregate；条件 mirror capture 必须在完整解析前限制 frame/snaplen、队列/内存/保留期并证明 payload 不进入 WAL、日志、metrics、trace、Event 或 Frontend。AF_XDP 的 bind 成功、copy-mode fallback 或 PACKET_MMAP 可运行均不是性能资格证明。
- 日志、metrics 和 trace 必须结构化、有界、低基数并脱敏；不得记录 chain-of-thought、完整 prompt、原始 provider 输出、raw packet 或 credential。
- rule/effect/operation/entry digest、match/IP/五元组不得作为 Prometheus label；Grafana/Alertmanager/OTel 只做低基数只读运维观测，production 禁止 Grafana Action/API mutation、告警 auto-remediation 或把 silence/annotation 当业务事实。
- readiness、liveness 和 startup 语义必须分离；健康检查不得触发 mutation。

## 仓库工作规则

- 修改前先读取需求基线和目标模块的公开契约；只读取任务需要的文件。
- 保留用户和其他任务的既有修改，不得使用 `git reset --hard`、破坏性 checkout 或宽泛删除。
- 使用锁文件和不可变 OCI digest；升级依赖必须附兼容性、安全和性能验证。
- Frontend 优先通过包管理器复用经 `WEB-SUPPLY-001` 资格化的成熟独立基础库，不重复实现 router、query cache、UI primitive、chart、virtualization、test runner 或 OpenAPI client generator；不得据此复制第三方应用控制面或业务组件。
- 全系统优先复用 ADR-0009 的成熟通用机制（Buf/protoc、p4c/P4Tools/PTF/P4Testgen、Mininet/netem、条件 p4-constraints/Tcpreplay、P4Runtime、条件只读OpenConfig gNMI、P4 aggregate/IPFIX event-time语义模式、gRPC deadline/retry、Triton dynamic batching/strict startup、ONNX Runtime CPU/CUDA与I/O Binding、Kubernetes GPU device plugin/Deployment仅作条件基础设施adapter、PgBouncer/自建时 pgBackRest/Patroni、OTel/Prometheus/Alertmanager/Grafana、Syft/Trivy/Cosign 等），但每种能力首期固定一种 profile；KServe/Ray Serve/TF Serving/MLflow不得成为第二 serving router、model current或autoscaling控制事实，Triton也不得取得这些所有权。官方 Bloom-filter firewall 只作教学参考，UFW/nftables/iptables 不得成为 BMv2 backend/fallback。Stratum仅target-side条件profile，NetBox仅candidate inventory，Ansible/Nornir仅离线/test，ONOS/厂商controller拒绝作为writer。PACKET_MMAP只作条件镜像兼容路径，AF_XDP只在冻结硬件/queue/NUMA/XDP_DRV/zero-copy或明确copy profile并证明必要收益后采用，DPDK只留给未来硬件线速profile；不得因此引入第二 collector/queue/workflow/P4 controller/授权或 dashboard mutation。TRex/等价高率发生器仅由hardware profile触发，不与BMv2默认工具并存。
- 首期本地/CI 正式 E2E 使用冻结的 `e2e-runner-compose/v1`；场景 manifest 必须精确指定 traffic mode、fixture digest、runner/backend profile、拓扑、方向和重写参数，禁止“自动选择可用后端”或失败后静默换 runner。缺少精确前置条件必须如实记录 `result=HOLD` 或 `result=NOT_RUN`。
- vendored/forked 第三方源码必须逐文件登记 upstream exact revision/path、license/SPDX、copyright、digest、修改、NOTICE/source obligation、测试、更新与退出策略。当前仓库未声明项目 LICENSE/NOTICE，未经 Owner 和许可证合规审查不得导入 GPL/LGPL 应用源码；1Panel/sub2api 默认只作交互参考。
- 每个被采用的外部事实或组件必须在 `contracts/supply-chain/v1` 使用稳定 `source_id` 关联可复核的标题、版本/commit、访问日期、归档快照及 digest；动态网页链接只能用于调研导航，不能单独支撑资格结论。
- Web Module Complete 前必须从 lockfile/source manifest 生成或交叉校验机器可读 `third-party-inventory/v1`、`THIRD_PARTY_NOTICES` 和 SBOM；direct/transitive/generated/vendored/assets/fonts/icons/themes/fixtures 都在范围内，生产不得运行期从公网取依赖或资产。
- 代码变化与 runtime qualification 证据分开提交和存放；本地 rehearsal 不得声称生产资格。
- 新增架构决策写入 `docs/adr/`，并引用受影响的需求 ID、取舍、迁移和回滚方案。
- 提交前至少运行目标模块完整门禁、契约检查和 `git diff --check`，并如实报告未运行项。
- 不提交 secret、构建目录、缓存、测试数据库、临时日志或未脱敏运行证据。
