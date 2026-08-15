# MASI-NIDS vNext 系统需求说明

- 初始日期：2026-08-09
- 最近修订：2026-08-14
- 基线：vNext-requirements-1.19
- 状态：Owner 已确认 v1.0 至 v1.19。首期生产在线推理保持 Central Inference 单架构及启动前显式二选一的 `model-runtime-central-cpu/v1`/`model-runtime-central-cuda/v1`；不得自动改选、运行期互切或按请求 fallback，Edge 不部署本地推理。v1.15 将资格证据拆成互不混装的 `level/applicability/result/qualification`；v1.16 澄清 `operational-single-domain` 不能取得 production qualification，以及独立 Analysis/service/Agent 的业务协议不经 Runtime Host 代理。v1.17 增加受控的 `plugin-statistics/v1` 统计输出能力。v1.18 将所有模块的正式稳定性 soak 统一冻结为 3,600 秒，并明确该 PASS 只关闭对应 soak gate；同时首次需求基线 bootstrap 可使用本地 SSH 签名 tag 与 Cosign 签名 digest manifest，资格上限为 `MODULE`，后续发布仍要求受保护远端。v1.19 将机器派生的 operational Module Complete 与资格聚合正交化：无 open P0、无真实启动/必需测试阻断且完整实现门禁实际通过时可完成；受保护基线、dirty tree、生产绝对门槛或尚未开始的正式 pairwise/system 只限制资格，不得伪装为 operational blocker，也不得被改写成资格 PASS。首期本地/CI 正式 E2E 固定使用 `e2e-runner-compose/v1`，每次正式运行仍须真实启动相关服务并走公开边界
- 文档类型：目标需求基线，不是实现状态、运行授权或发布资格报告
- 适用范围：MASI-NIDS vNext 模块化重写

## 0. 文档地位与变更规则（GOV-001）

本文收敛 2026-08-10 之前关于 MASI-NIDS vNext 的需求讨论，定义产品目标、模块边界、技术栈、数据所有权、部署形态、测试门禁和验收条件。

Owner 已确认本文全部推荐默认值，并追加“所有首期模块必须先完整实现，再开始正式集成测试”以及“实现阶段必须真实启动相关服务并执行 E2E”的全局门禁。模块完成前允许受隔离、无生产副作用并明确标记为 `REHEARSAL/NOT QUALIFIED` 的真实 wire/boundary rehearsal，以尽早验证 TLS、framing、代码生成和取消语义；但 Module black-box E2E 必须真实启动被测模块及所选 runtime，正式 pairwise/full E2E 必须在干净环境真实启动参与链路的服务并走真实公开边界。fake/mock 只能补充尚未接入的外部依赖、确定性 provider fixture 与故障注入，后续实现不得把 fake/mock、rehearsal、readiness、microbenchmark、临时兼容胶水或局部联调成功解释为正式 PASS。

本文与 legacy `../../MASI-NIDS/AEE_cuda/docs/masi-nids-integrated-demo-requirements-2026-07-31.md` 的关系如下：

- 旧文档继续描述现有 Python 整合演示和历史实现目标；
- 本文仅对 vNext 重写生效，并替代旧文档中关于实现语言、Workflow v2、人工审核、应用级 RBAC、自研模型签名发布链和 Agent 内嵌方式的约束；
- vNext 仍继承旧架构中已经证明有价值的正确性不变量，包括 PostgreSQL 事实源、P4 唯一写入者、持久化 intent/outbox、幂等、租约、CAS、代际 fence、真实读回、`unknown` 语义、事务不跨外部等待以及所有资源必须有界；
- 历史 TEL、Phase、full-demo 和 qualification 文档不作为 vNext 运行控制输入；
- 本文不授权启动 BMv2、P4Runtime、Telemetry、Inference、数据库迁移或真实网络处置。

修订记录：

| 基线 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-08-09 | 冻结 greenfield vNext 的模块边界、唯一所有权、durable effect 和先模块完成后正式集成的门禁 |
| v1.1 | 2026-08-10 | 增加 effect 专用提案/授权治理、风险分级、OIDC 角色分离、P4Runtime/滚动升级兼容、旧 Analysis Graph 行为矩阵、治理性能预算以及对应故障与 E2E 门禁；不恢复 legacy Workflow/Review/Auth |
| v1.2 | 2026-08-10 | 将受控通用插件平台纳入首期必需范围；增加 Go Plugin Manager、独立 Runtime Host、OCI service/WASI 0.2/A2A 多运行时、manifest/capability、标准供应链准入、生命周期、隔离、性能、兼容和 E2E 门禁；Analysis Plugin 成为首个官方插件，仍不恢复 legacy Workflow 或开放任意代码市场 |
| v1.3 | 2026-08-10 | 根据 WASI、MCP、A2A、gRPC、P4Runtime、PostgreSQL、OpenAPI/JSON Schema、SLSA、Kubernetes 与 Pact 官方资料复核：区分规范状态和首期资格 profile，定义 restricted MCP、P4/application generation 与 CAS precondition、gRPC/Wasm 中断、撤销新鲜度、UDS/probe、早期 boundary rehearsal 和机器可读资格证据；不扩大插件副作用或正式集成权限 |
| v1.4 | 2026-08-10 | 将 Frontend 改为采用 1Panel 同类成熟技术栈的独立 Vue 3/Vite SOC SPA；冻结 Go 同源认证/API 边界、任务导向交互、服务端状态/SSE、危险操作、视觉与 WCAG 2.2 AA、Web 性能/浏览器 profile，以及“优先复用成熟依赖、第三方应用源码默认仅参考”的许可证与供应链门禁 |
| v1.5 | 2026-08-10 | 增加规则安装、数据面命中、独立结果验证三层证据；冻结 direct-counter 身份/采样/重置语义、规则表现 API/UI、低基数观测、故障/性能门禁；建立全系统成熟组件的“直接复用/条件复用/拒绝”登记，禁止借复用引入第二事实源、第二 P4 writer 或第二 effect queue |
| v1.6 | 2026-08-10 | 将在线检测模型冻结为“稳定 C++ engine + immutable model bundle + versioned feature/label/output contract + Go/PostgreSQL exact binding”；增加多类别/多标签/异常分数适配、side-by-side shadow、batch-boundary activation、current/previous rollback、运行后端资格与模型供应链门禁；同时消除 rule epoch owner、telemetry/direct-counter、模块注册、状态命名和 ADR 覆盖歧义 |
| v1.7 | 2026-08-10 | 根据 ONNX Runtime、Triton、TensorFlow Serving 与 Kubernetes 官方资料重新评估在线切换复杂度：首期改为 C++ 启动时加载 exact bundle、每进程一个 `Ort::Session`、按 `inference_shard` drain/buffer/restart/readback/CAS；删除生产 candidate slot、在线 shadow、进程内 `PrepareModel/ActivateAtBoundary` 和全 cohort 原子 current，保留 per-shard current/previous、代际 fence、显式滚动回滚；进程级 blue-green 与进程内双 Session 只有实测 SLO 触发并提升 profile/基线后才可采用 |
| v1.8 | 2026-08-10 | 再按 ONNX Runtime threading/optimization、Kubernetes Deployment/Pod termination/PDB、Triton startup-only、TensorFlow Serving version/update 与 KServe external-routing 模式复核：保留 v1.7 单 Session 启动绑定；补齐 `model_control_incarnation_id` 防 PITR ABA、Edge 唯一 `shard_routing_epoch`、部署 adapter 动作幂等与终止合同、wire/optimized-artifact exact identity、WAL 内 bounded buffer/gap/resume、滚动容量公式和 restart/quarantine budget；成熟 serving/router 继续只作模式参考，不成为首期依赖或第二 current owner |
| v1.9 | 2026-08-10 | 根据 PTF/P4Testgen、Tcpreplay、Mininet/Linux netem、Linux offload、公开 NIDS 数据集与去标识资料复核：新增 `traffic-replay/v1` fixture/profile，区分包级 oracle、二层字节回放、合成流量和真实会话；冻结方向/改写/时序/速率/扰动/资源/ground-truth/许可证与隐私合同，拆开 sender、DUT ingress、rule hit 和 action outcome 证据；BMv2/Mininet 只资格化软件 target，外部 PCAP 与高性能发生器按条件准入，不进入生产控制面 |
| v1.10 | 2026-08-10 | 根据 P4Runtime Digest、IPFIX flow/observation identity、Linux PACKET_MMAP/AF_XDP/circular buffer、Suricata AF_XDP、ONNX Runtime I/O Binding 与 Triton batching 官方资料复核：冻结 P4 aggregate-first 主源、Digest/PacketIn best-effort supplemental、按需镜像 capture profile、event-time/watermark/final-only window、固定布局 SPSC shared-memory ABI、实际 copy 计量和 telemetry→input→result WAL→PostgreSQL ACK 顺序；首期不引入 DPDK/Triton/Beam/Flink runtime 或第二 telemetry service |
| v1.11 | 2026-08-11 | 根据 BMv2、p4c、P4Runtime、P4Testgen/PTF、p4-constraints 与官方 P4 firewall 教程复核：新增 `p4-stateless-firewall/v1`，明确首期是 BMv2 `simple_switch_grpc`/v1model 的 IPv4 无状态过滤，不是 Linux 主机防火墙；采用 exact response overlay + 双 bank baseline policy + selector 的确定性流水线，冻结策略 IR、priority/overlap/default/fragment/capacity、审批、readback/counter/outcome、故障/性能/前端门禁；IPv6 处置及硬件 P4 另行资格化，不增加第二 controller、writer、queue 或通用工作流 |
| v1.12 | 2026-08-11 | 根据 P4Runtime 1.4.1、OpenConfig gNMI/gNOI、Stratum、ONOS、NetBox、Ansible 与 Nornir 的公开边界复核：新增稳定 target identity/registry/assignment、Edge 每 target actor、冻结 target-set 的 fleet parent + 既有 per-target intent children、静态 canary/wave、partial/reconciling 语义、资源公平和设备/舰队 UI；首期支持 bounded 多 BMv2 target，但不宣称跨 target 原子提交或完整 NMS。P4Runtime 继续是唯一写路径；gNMI 仅条件只读，Stratum 仅可作为 target-side profile，第三方 inventory/automation 只可导入候选或执行离线 provisioning，ONOS/其他 controller 作为生产 writer 明确拒绝 |
| v1.13 | 2026-08-11 | 根据 Triton dynamic batching/model management/secure deployment、ONNX Runtime CUDA/TensorRT/I/O Binding、gRPC performance/retry/deadline、Kubernetes GPU scheduling/topology spread/PDB 与 KServe control-plane 官方资料复核并接受 central-GPU 单架构：首期取消 Edge-local C++/SPSC 推理和任何 CPU/异构 backend 失败回退；以 C++ Gateway + Triton + ORT CUDA + 只读 model-repository snapshot 组成一个中央推理模块，使用有界 batched-unary mTLS gRPC、Triton 唯一延迟批调度、same-generation active-active/N+1；全池不可用时显式 HOLD/gap。模型变更以新 pool generation 启动加载和逐 shard route fence/CAS 切换，Go/PostgreSQL 仍是唯一 current owner。Kubernetes 只作固定容量基础设施 adapter，KServe/Ray Serve/TF Serving/MLflow serving control plane 不进入首期 |
| v1.14 | 2026-08-12 | 根据 Triton `KIND_CPU/KIND_GPU`、无 GPU 构建能力、ONNX Runtime Execution Provider/CPU threading/NUMA 与 OpenVINO CPU 资料复核：保留 Central Inference 唯一执行架构，将 CUDA-only 改为启动前显式二选一的 `model-runtime-central-cpu/v1`/`model-runtime-central-cuda/v1`；硬件 probe 只验证并建议，不能自动选择或故障互切。复杂自动模型容灾、在线 shadow/hot swap 仍不进入首期，只保留 immutable revision、启动自检、显式 exact rollback 和按部署等级资格化的同 Profile 副本。另将“真实启动相关服务并执行 E2E”提升为硬门禁：CPU/CUDA 分别形成真实启动、公开边界、故障与性能证据，fake/mock/rehearsal/microbenchmark 不得替代 Module、pairwise 或 system PASS |
| v1.15 | 2026-08-12 | 复核资格聚合、Central Inference rollout、Triton/ORT 运行语义与 E2E 可复现性：将 evidence 拆为 `level/applicability/result/qualification` 四个正交字段，并按 runtime、availability、deployment tier、拓扑和制品 digest 限定声明范围；明确 single 是单故障域而非单副本、CPU/CUDA 各自资格化、生产绝对性能不可豁免。修正模型切换顺序为 Edge route-withdraw/drain/WAL 后再 PG CAS/commit/resume；冻结 `GetLoadedModel/GetPoolStatus`、Triton NONE 仓库闭包、显式 instance group、CUDA profile 内受控 host-side operator placement及 `e2e-runner-compose/v1`，并要求 traffic scenario 精确选择 backend，不允许自动降级 |
| v1.16 | 2026-08-12 | 不删减首期功能，仅消除两处语义漂移：将容易误导为“可生产但无 HA”的 `production-single-domain` 更名为 `operational-single-domain`，并规定只有 `production-ha` 可取得 `level=PRODUCTION`/production qualification；将插件运行面明确为两条并列路径——Runtime Host 执行 Wasm/显式 Host-managed service，独立 Analysis/service/Agent 由 Manager 管理 binding 但通过自身 A2A/MCP/gRPC 业务协议直连，不把 Host 作为业务代理。System E2E 仍真实启动 Host 与 Analysis，两者均为完整产品必需模块 |
| v1.17 | 2026-08-12 | 根据 OpenTelemetry Metrics、Prometheus、Grafana DataFrame、ECharts dataset/ARIA、Backstage extension、Dapr/go-plugin/OTel Collector、Vega CSP 与 OWASP XSS/CSV 资料复核插件统计展示：不新增 plugin kind，以 `plugin-statistics/v1` 作为 `pure-transform`/`read-only-tool` 的可选输出能力；Go 拥有冻结输入、低频 run、校验、授权和 PostgreSQL 投影，Web 使用固定声明式组件与内部 ECharts dataset 渲染。禁止插件自调度、直写 DB/Prometheus、浏览器直连、UI 代码/路由、任意 ECharts/Vega/HTML；补齐资源、幂等、质量、故障、性能、安全、迁移和真实 E2E 门禁 |
| v1.18 | 2026-08-12 | Owner 将所有模块正式 soak 的资格时长从 24 小时改为精确 3,600 秒：60 秒预热不计入，随后 steady、peak、saturation、recovery-or-activation 四阶段各 900 秒；soak PASS 只关闭对应门禁，不豁免功能、性能、故障、供应链或完整 E2E。首次 bootstrap 允许本地 SSH 签名 tag 与 Cosign 签名 digest manifest，最高 `MODULE`；后续发布仍要求受保护远端。P4 BMv2 WSL2 功能参考档的绝对门槛由 Owner 在资格运行前冻结，不外推原生 Linux、硬件、line-rate 或 production HA |
| v1.19 | 2026-08-14 | Owner 冻结 `DEC-044`：Module Complete 是独立、机器派生的 operational completion，不是资格枚举或 `MODULE PASS` 的别名。完整实现、真实候选 binary/OCI、适用模块测试与 3,600 秒 soak 均已实际通过，且 findings registry 中 open P0 与真实启动/测试 blocker 均为 0 时标记完成；dirty tree、受保护基线、生产绝对门槛及尚未开展的正式 pairwise/system 继续诚实保持 qualification-only `HOLD/NOT_RUN`，但不阻断完成，也不得被提升为相应资格 PASS |

本文使用以下规范用语：

- **必须（SHALL）**：vNext 完成前不可缺少；
- **禁止（SHALL NOT）**：不得通过配置或兼容路径绕过；
- **应当（SHOULD）**：原则上实现，偏离时必须记录理由和证据；
- **可以（MAY）**：允许但不构成首期验收条件；
- **待确认（TBD）**：不影响总体架构，但实现前需要 Owner 选择。

任何强制需求的删除、降级或语义改变必须提升本文基线并记录变更理由。设计文档、代码现状和测试便利不能自行改变本文。

## 1. 已确认的需求摘要（BASE-001）

vNext 必须满足以下总体目标：

1. 基于现有 Edge、Control、State、Analysis、Frontend 的大致分层进行系统性重写，而不是逐文件翻译现有 Python 实现。
2. 使用 C++、Rust、Go、Python 3.12 及更高版本，并让每种语言只承担与其优势一致的职责。
3. 核心在线链路采用“BMv2/P4 → Edge Runtime → Online Inference → Control Backend → PostgreSQL → Frontend”。
4. 真实处置采用独立反向链路“Control durable effect → Edge Agent → P4Runtime → readback → PostgreSQL CAS finalize”。
5. 首期必须交付受控通用插件平台：Go Control 内的统一 Plugin Manager 负责目录、准入、资格、激活和撤销，独立 Plugin Runtime Host/容器负责执行；平台不得进入核心实时链或真实处置闭环。
6. LangGraph、LLM、MCP、A2A 组成平台首个官方 `analysis-agent` 插件，用于辅助分析研判、生成处置建议、报告、通知和其他内容；其产物不可执行。
7. 系统支持单机开发、三机验收和生产多机部署；必须明确同机低延迟边界与跨机协议。
8. PostgreSQL 是唯一核心持久事实源，并按生产级 HA、PITR、监控、迁移和容量要求设计。
9. Redis 不作为首期依赖；未来只能作为可丢失、可重建的非权威加速层。
10. 所有首期模块必须分别完成全部范围内实现，使用实际 binary/OCI 和所选 runtime 真实启动，并通过独立黑盒组件 E2E、故障恢复、性能门槛和 Module DoD；只有全模块完成门禁统一通过后，才允许开始正式相邻模块集成和分波次系统拼装。正式 pairwise/full E2E 必须重新启动真实参与服务并走真实公开边界；此前仅允许 `TEST-GATE-001` 定义的非资格化真实边界 rehearsal，fake/mock 不得替代正式 PASS。
11. 真实 effect 必须经过轻量、确定性、可审计的风险准入；提案和授权决定是不可执行事实，唯一可执行队列仍是 `effect_intents`。
12. vNext 应主动做减法，不迁移无助于核心目标的历史、兼容和治理复杂度。
13. Frontend 必须是独立、可回滚的 Vue 3/Vite SOC SPA；优先复用成熟且通过许可证/供应链准入的基础库，但不得复制第三方产品控制面、业务状态机、品牌资产或授权语义。
14. 每条已下发规则必须能够区分“设备已安装”“数据面已命中”和“预期处置结果已被独立验证”；规则计数器增长不得被展示或审计为处置成功率。
15. 在线检测模型必须可以在不修改Edge/Go业务代码的前提下，以exact bundle/repository snapshot和管理员显式选择的CPU或CUDA runtime profile启动新的Central Inference pool generation，替换为同一已资格化输入输出合同的其他模型，并通过版本化label taxonomy/output adapter支持新增流量类别；这表示“部署前选择、启动新pool并逐shard切换”，不表示runtime热插拔。feature/runtime profile或major改变时必须显式升级合同和generation。
16. 在线模型滚动必须由 Edge 保持每个 shard 的唯一 canonical 输入路由，并以不可复用的 model-control incarnation、route epoch、exact wire/optimization identity、幂等 deployment action、readback/CAS/commit handshake 和有界 WAL 恢复防止 PITR、旧进程或强制终止形成 ABA/双路由；Kubernetes/systemd 状态不得代替这些事实。
17. BMv2/P4 测试流量必须通过版本化 fixture/profile 进入隔离 testkit：PTF/P4Testgen 用于确定性包级 oracle，Tcpreplay 类工具只表示二层字节回放，真实 TCP/应用会话必须由有状态 client/server fixture 产生；requested/sender-accepted/DUT-observed/rule-hit/action-outcome 分开记录，公开攻击数据集必须先通过许可证、隐私、ground-truth 和保留审查。
18. 在线推理的首期生产数据源必须是由 P4 target 形成并可证明 snapshot/epoch 一致性的有界 metadata/window aggregate；P4 Digest/Ack 和 PacketIn 只可作 best-effort 提示、诊断或采样，不能冒充可靠逐包队列或完整覆盖。Rust Edge 统一拥有采集、event-time window、WAL、有界central-gRPC批处理和结果重放；只有 feature contract 或容量证据触发时才增加 mirror PACKET_MMAP/AF_XDP/DPDK profile，且不得新增第二 P4 client、source owner 或 canonical queue。
19. 首期必须交付 `p4-stateless-firewall/v1`：在 BMv2 `simple_switch_grpc`/v1model 中以单包确定性 match/action 实现 IPv4 无状态过滤。短期 incident response 使用高优先级 exact overlay 和 durable TTL；长期 baseline policy 使用版本化策略 revision、双 bank 预装/readback 与单 selector 切换。两者都只通过既有 proposal/decision/`effect_intents`/Edge journal/readback/CAS 链执行；UFW、nftables、iptables、主机 namespace/qdisc 与官方 Bloom-filter 教程均不是生产封禁后端。
20. 首期必须把 P4 target 作为可注册、可分配、可隔离和可审计的受管设备管理，并在冻结绝对 `max_targets_per_edge/max_targets_per_fleet_operation` 后证明 1/2/N 个 BMv2 target。Go/PostgreSQL 是 target registry、assignment 与 fleet operation 的唯一事实源；Edge 为每个 target 建立独立 actor、队列、generation、journal 和 P4Runtime arbitration。一个 fleet change 不跨 target 原子提交，必须保留每 target 结果向量、静态 wave/canary、停止/继续/人工 gate 策略和显式部分失败/未知收敛。
21. 首期生产在线推理只能使用 `inference-central-grpc-batch/v1`：Edge 将已持久化 final feature-window 以有界 batched-unary mTLS gRPC 发送到 Central Inference logical pool；C++ Gateway 做身份、合同、framing、配额和结果适配，Triton 是唯一允许引入等待的动态 batch scheduler。每个 pool generation 必须由 immutable startup envelope 在启动前显式绑定 `model-runtime-central-cpu/v1` 或 `model-runtime-central-cuda/v1`；硬件探测只验证人工选择，profile不匹配必须启动失败。首期没有 Edge-local inference、CPU↔CUDA自动fallback、另一模型fallback、TensorRT自动fallback或逐记录RPC。
22. production pool 的副本数、failure domain、N+1 和容量要求按部署等级及所选 runtime profile 冻结；需要 production HA 的部署必须以同一 exact profile 的等价副本满足故障域与剩余容量门槛，实验/开发单实例不得获得 HA PASS。同一 pool/binding generation 的副本可被重新选择，但请求重试必须沿用 exact request identity/input digest，第一份有效结果成为 canonical candidate，late duplicate 被 fence。全池不可用时 P4 转发及既有规则继续，Edge 仅在已有 WAL 上限内缓冲/背压；超过 age/bytes/records 后产生 exact gap 并使相应检测为 `HOLD/unavailable`，绝不将缺失推理编码为 normal/0或自动启动另一CPU/CUDA profile。
23. 所有资格结论必须使用 `qualification-evidence/v1` 的正交字段：`level=REHEARSAL|MODULE|PAIRWISE|SYSTEM_E2E|PRODUCTION`、`applicability=APPLICABLE|NOT_APPLICABLE`、`result=PASS|FAIL|HOLD|NOT_RUN`、`qualification=QUALIFIED|NOT_QUALIFIED`。`REHEARSAL/NOT QUALIFIED`、`MODULE PASS` 等仅是展示摘要；条件能力不适用必须给出稳定理由，适用项的 `FAIL/HOLD/NOT_RUN` 均阻断对应聚合 PASS。
24. 首期本地/CI 的正式 Module、pairwise 与 BMv2 system E2E 使用 `e2e-runner-compose/v1` 编排发布候选服务；Compose health 只作为依赖就绪条件，不是业务 PASS。每个场景必须显式选择一个 traffic mode、runner/backend、fixture digest、拓扑、方向和重写配置；禁止“best available”、自动探测后切换工具或失败后静默 fallback。生产/多主机部署可使用其已冻结 deployment adapter，但不得把另一 runner 的证据冒充当前 profile。
25. 通用插件平台必须支持版本化 `plugin-statistics/v1` 输出能力：Go 从 canonical facts/projections 冻结有界输入、拥有统计 run 调度/幂等/校验/授权/PostgreSQL current/history，插件只返回不可执行 typed Artifact；Web 只用内置声明式组件显示。该能力不新增 plugin kind、部署模块、浏览器 UI plugin、独立消息代理、插件自有 scheduler/queue、第二数据库 writer、第二 effect queue 或实时热路径依赖；唯一 durable run ledger 与有界 admission/dispatch 仍由 Go Control 拥有，插件关闭时核心统计和非插件页面保持完整。

## 2. 产品目标与非目标

### 2.1 产品目标（CORE-001）

MASI-NIDS vNext 是一套模块化、可替换、可分机部署、性能优先且故障可恢复的网络入侵检测与处置系统。

系统必须能够：

- 从 P4/BMv2 或兼容 P4 target 获取有界遥测；
- 构建连续、代际一致的目标窗口；
- 对窗口进行批量在线推理；
- 持久化事件、incident、运行状态和 effect 状态；
- 使用确定性策略生成或拒绝处置候选；
- 向人类审批者展示完整、当前、可验证的设备变更上下文，并按风险等级实施最小权限和职责分离；
- 通过唯一 P4 writer 可靠执行、读回并收敛处置结果；
- 在不阻塞处置链的前提下，以有界 direct-counter/eligible-counter 和独立 packet oracle 分层展示规则安装、命中与结果证据；
- 通过前端展示真实状态、历史趋势、处置结果和 Agent 分析产物；
- 通过受控、可版本化、可禁用和可替换的插件扩展分析、只读工具和纯计算能力；
- 允许已准入插件对 Go 冻结的有界事实投影执行统计计算，并通过 Go-owned 投影和固定 Web renderer 展示，不向浏览器注入代码；
- 通过官方 Analysis Plugin 完成证据约束的分析、建议和内容生成；
- 在进程崩溃、主机断网、重复投递、结果未知和代际变化时保持可解释、可恢复且不盲写。

### 2.2 产品主链（CORE-002）

```text
网络包
→ P4/BMv2 有界 metadata aggregate（默认）或已资格化 mirror capture（按需）
→ Rust Edge source adapter / event-time window / WAL / bounded batch
→ Central Online Inference（batched-unary mTLS gRPC → C++ Gateway → Triton dynamic batching → startup-selected ONNX Runtime CPU/CUDA）
→ Rust Edge result WAL / bounded gRPC batch
→ Go Control Backend / PostgreSQL durable ACK
→ TypeScript Frontend
```

### 2.3 反向处置链（CORE-003）

```text
Event / Incident
→ deterministic eligibility / risk evaluation
→ 已资格化 automatic policy 或经认证的 authorization decision
→ durable effect_intent
→ Rust Edge Agent
→ P4Runtime write
→ operation journal / P4 readback
→ PostgreSQL CAS finalize
→ applied / failed / unknown
→ applied 时由 Go 创建 canonical rule observation epoch；Edge 按该 identity 异步采样，Go 生成 counter rollup / optional outcome evidence
```

### 2.4 Agent 旁路（CORE-004）

```text
Event / Incident / Evidence references
→ Python Analysis Plugin
→ LangGraph
→ LLM + read-only MCP + optional A2A peers
→ AnalysisArtifact / Recommendation / Report
→ Go Backend / Frontend / A2A client
```

Agent 旁路失败、关闭或超时不得影响产品主链和反向处置链。

### 2.5 插件扩展平面（CORE-PLUGIN-001）

```text
受控 OCI/Wasm artifact + strict manifest
→ Go Plugin Manager admission / qualification / activation
→ 独立 Plugin Runtime Host 或独立 service/Agent container
→ bounded typed input
→ plugin execution
→ schema-validated candidate / Artifact / read-only result
→ Go-owned projection / API
→ built-in Frontend renderer
```

插件扩展平面是非权威旁路。Plugin Manager、Runtime Host、任意插件和插件输出均不得成为 Event、Incident、Effect、Runtime、身份、授权、P4 状态或其他核心事实的替代来源；平台不可用时核心检测、持久化、处置和非插件页面必须继续。

统计类扩展遵守同一边界：`plugin-statistics/v1` 是现有 kind 的可选输出能力，不是第四种 kind。Go 只从自身 canonical facts/projections 生成冻结输入并拥有 schedule/run/current/history；插件不得扫描数据库、自我调度或直接写 Prometheus/Frontend。核心 Rule Effectiveness、Event/Incident、模型、target/fleet 和系统健康统计不经插件才能成立。

### 2.6 在线模型控制链（CORE-MODEL-001）

```text
Offline ML immutable model bundle + qualification evidence
→ Go Model Manager register / qualify / durable rollout request
→ pre-stage exact model-repository snapshot + immutable pool startup envelope
→ start a new Central Inference pool generation with an explicit CPU or CUDA runtime profile; Triton startup-loads one exact binding and Gateway performs hardware preflight/warmup/readback
→ qualify selected-profile resources and, when the deployment claims HA, min-ready replicas/failure-domain/N+1 and rolling capacity outside database transactions
→ Edge advances shard routing epoch, withdraws the old logical-pool route and drains admitted work
→ PostgreSQL per-shard CAS under exact model-control incarnation / retain previous
→ Edge verifies the committed logical-pool binding and resumes; old generation drains within bounded rollback grace
```

模型控制链是低频确定性控制面，不进入 packet/telemetry hot path。Go/PostgreSQL 是 model revision、qualification、rollout operation、pool generation、per-shard current/previous binding、model-control incarnation、rollout 状态和审计事实的唯一 owner；Triton 固定 `model-control-mode=none`，每个实例启动时只从 immutable envelope/read-only repository closure 加载一个 exact binding及其声明依赖，显式固定instance group，不接受额外repository成员或生产 model-control mutation。Edge 是每个 shard 唯一 canonical input router，route identity 绑定 logical pool/binding generation而不是单个 replica；same-generation replica 选择不提升 route epoch，切换 pool/model/contract/backend generation 必须提升。Gateway/deployment/Kubernetes 只执行幂等启动、停止、健康与资源动作，不拥有 route/current。任何数据库事务或连接均不得跨 artifact fetch、实例启停、模型加载/优化/预热、readback 或 readiness 等待。滚动期间不同 shard 可以分别运行各自已提交的旧/新 current，但 group projection 必须为 `rolling_mixed`，结果不得跨 binding generation 拼接；跨 shard 的 policy/effect 默认 `HOLD`，除非版本化兼容 policy 明确允许两个 exact revision。首期禁止 Triton runtime model load/unload、candidate/online-shadow、动态 repository polling和 `latest` 路由。

### 2.7 首期非目标（SCOPE-001）

vNext 首期不要求：

- 兼容 Event v2、旧 `/events`、旧 anomaly score 或 legacy alert 事实；
- 迁移 legacy workflow runner、通用 Workflow v2 DAG、ReviewPacket 或通用人工审核状态机；首期只实现 `FUNC-GOV-001` 定义的 effect 专用治理事实；
- 自建用户目录、密码、refresh session、旧 `admin/analyze` RBAC 或任意业务审批引擎；effect 角色从受信 OIDC 身份与版本化映射获得；
- 自研 scenario/model/release/pipeline 四域签名、独立 release signer 或 signed release report；
- 在核心热路径中运行 LangGraph、LLM、MCP 或 A2A；
- 让 Redis、LangGraph checkpoint、MCP Task、A2A Task 或浏览器状态成为核心事实源；
- 建立第二 P4 writer、第二 effect queue 或旧新系统双写；
- 把首期扩展成通用 NMS、自动拓扑发现、路由协议控制器、端口/VLAN/QoS/设备 OS/证书生命周期平台，或承诺跨交换机 P4Runtime 原子事务；gNMI Set/gNOI 等设备 mutation 需要新需求基线和独立 owner/治理链；
- 建立公共插件市场、允许未审核上传、把任意插件代码动态加载进 Go/Rust/C++ 核心进程，或进行公网 Agent 自动发现；受控私有 catalog、独立运行时加载和显式激活属于首期必需范围；
- 把在线检测模型实现为通用插件 kind，或通过模型包加载 native shared library、Python/Wasm hook、未资格化 ONNX custom op/TensorRT plugin；模型是受 model contract/profile 管理的数据制品，不是可执行插件；
- 启用绕过 durable intent、fence、readback 或 CAS 的 break-glass 写入路径；
- 将 full-demo、Mininet、历史 Phase/TEL harness 作为生产运行组件；
- 为“使用多种语言”而在同一职责上维护两套长期在线实现。
- 让 NetBox/CMDB、Ansible/Nornir、Stratum/ONOS/厂商 controller 或 P4Runtime Shell 成为 canonical target registry、生产 scheduler、第二 P4 writer、effect queue 或 authorization source；这些方案只能按 `ARCH-TARGET-FLEET-001`/ADR-0015 的明确边界条件采用。
- 在 Edge 节点运行本地 C++/CPU 推理、让 Central Inference 在故障时自动切换CPU/CUDA、另一runtime/backend/model，或让TensorRT/LibTorch/旧模型成为隐式fallback；这些都不是首期容错能力。管理员在启动前显式选择已资格化central CPU或CUDA profile不属于fallback。
- 让 Triton/KServe/Ray Serve/TensorFlow Serving/MLflow、Kubernetes Service/Deployment 或 GPU scheduler 成为 model current、per-shard canonical route、Event ACK、授权或 effect 事实源；Triton 只在 `MOD-INF-001` 内承担受控 batching/execution。

## 3. 总体架构原则

### 3.1 模块化定义（ARCH-001）

“完全模块化解耦”在本文中表示：

- 模块只依赖版本化契约，不依赖其他模块内部代码；
- 每个持久事实、队列和设备写入都有唯一所有者；
- 模块可以被独立构建、启动、测试、替换和回滚；
- 模块故障不会无界传播到其他故障域；
- 跨模块输入、输出、错误、超时、幂等和兼容语义均有明确合同；
- 模块不得通过共享语言对象、隐式全局状态或未声明数据库表完成协作；
- 高度模块化不等于每个 package 都成为网络微服务。

### 3.2 在线进程最小化（ARCH-002）

首期生产拓扑应尽量收敛为以下在线组件：

1. P4/BMv2 或硬件交换机；
2. Rust Edge Agent；
3. Central Inference Module（C++ MASI Gateway + pinned Triton + startup-selected ONNX Runtime CPU/CUDA +对应计算资源）；
4. Go Control Core；
5. PostgreSQL/PgBouncer；
6. Rust Plugin Runtime Host；
7. Python Analysis Plugin；
8. TypeScript Web UI；
9. 可观测性、受控 OCI registry/离线 bundle 与可选对象存储基础设施。

Go Control Core 内部必须按端口/适配器方式拆分 Event、Incident、Policy、Effect Governance、Effect Execution、Model Manager、Plugin Manager、API、MCP、Operations 等模块，但首期应作为模块化单体部署，以保留本地事务和降低网络开销。Plugin Runtime Host 和各插件必须保持进程外故障域。只有经过容量或故障隔离证据证明后，才可以把其他内部模块拆成独立服务。

### 3.3 唯一所有权（ARCH-003）

- Rust Edge Agent 是 P4Runtime 的唯一长期 writer 和 StreamChannel owner；
- Go Control Core 是核心 PostgreSQL schema 的唯一业务写入者；
- Go Model Manager 是 model revision、qualification、rollout operation、per-shard binding、startup/readback observation、rollback 和 audit 控制事实的唯一写入者；
- Go Plugin Manager 是 plugin catalog、qualification、activation、binding 和 revocation 控制事实的唯一写入者；
- Rust Plugin Runtime Host 只执行已准入插件，不写核心 PostgreSQL、不访问 P4、不拥有业务状态；
- Python Analysis Plugin 只写自身 plugin schema；
- Central Inference 的 Gateway/Triton/backend 不连接 PostgreSQL、不调用 P4、不拥有 model current、route、Event 或业务状态；
- Frontend 不直连 PostgreSQL、P4、Rust、Central Inference/Triton 或 LLM provider；
- Analysis Plugin 不持有 P4 secret，不写核心 Event、Incident、Effect、Runtime 事实；
- Go Control Core 唯一创建核心 effect proposal、authorization decision 和 intent；只有 `effect_intents` 可以被 dispatcher claim，proposal/decision 永远不可直接执行。

### 3.4 通用插件架构（ARCH-PLUGIN-001）

- “通用”表示统一 manifest、catalog、capability、准入、生命周期、版本、资源、安全、观测和资格门禁，不表示所有插件共享同一业务协议或能够 Hook 任意核心函数；
- 控制面由 Go Plugin Manager 承担；Wasm 和显式绑定为 Host-managed 的 service plugin 由独立 Rust Plugin Runtime Host 执行或代理；独立 service/Agent 插件（包括官方 Analysis Plugin）保持独立 OCI 容器，由 Manager 控制准入、资格、active binding、generation 和撤销，但业务流量通过该 kind 的 A2A/MCP/gRPC adapter 直达，不绕经 Host；
- 平台首期采用封闭、版本化的 plugin kind 和 runtime profile；新增 kind、扩大副作用或接入核心热路径必须提升需求基线；
- 插件只能通过声明并获准的 capability 使用宿主 API；不得导入其他模块内部源码、共享可写 volume、持有 ambient credential 或自行发现高权限 endpoint；
- Runtime Host/插件故障只影响对应插件能力，不得改变核心事实、推进 effect、阻断 P4 readback 或导致无界重试；
- 平台必须保持 `registered/verified/staged/shadow/active/draining/disabled/revoked/failed` 等显式状态，不得以进程存在、端口可连或 tag 指向代替资格与激活事实。
- 在线 model/scaler/feature/label/output-adapter revision 与 Central Inference pool generation 不属于 plugin revision，Model Manager 不复用 Plugin Manager 的 kind、capability 或 active binding；两者只能共享供应链、evidence 和通用 CAS 库，不能共享业务状态机或表。
- `plugin-statistics/v1` 是 kind 输出 capability：Go 拥有冻结输入、低频 run、幂等、结果校验、PostgreSQL 投影和 Web API；插件只计算并返回有界 Artifact。它不新增 kind、部署模块、effect queue、数据库 writer、Prometheus fact source 或浏览器代码扩展点。

### 3.5 确定性核心（ARCH-004）

检测事实、事件身份、风险分类、授权准入、策略准入、P4 entry 构造、容量限制、TTL、generation fence、幂等和 effect 状态转换必须由确定性代码完成。LLM 只能解释、补充假设和生成不可执行建议。

### 3.6 有界原则（ARCH-005）

所有队列、WAL、spool、batch、响应、附件、分页、连接池、并发、重试、超时、日志、retention、未决 proposal 和清理范围必须有显式上限。达到上限时必须背压、降级、拒绝或在副作用前置 `HOLD/stale`；只有已尝试外部副作用而结果不明时才置 `unknown`，不得无界增长。

P4→Edge→Inference→Control→PostgreSQL、Manager→Host→Wasm/Host-managed service，以及 Manager→independent service/Agent binding + direct kind protocol 的每个边界必须在版本化 deployment/performance profile 中声明 capacity、high/critical watermark、queue age、ACK/credit 或等价反馈、超限错误和恢复水位。背压必须反向传播；critical 时依次停止低优先级 capture/shadow/Analysis、拒绝新的非权威插件任务，再将无法保持连续性的核心输入置 `HOLD/stale`。禁止静默丢弃遥测、把缺失补零或用健康状态掩盖拥塞。

### 3.7 成熟组件优先与所有权保留（ARCH-REUSE-001）

- 项目应优先复用边界清晰、维护活跃、可锁定版本且能离线资格化的成熟协议实现、代码生成器、测试框架和运维组件，不重复实现通用连接池、HA 编排、备份恢复、遥测采集、告警路由、契约 lint/breaking check、SBOM、漏洞扫描和制品签名；
- 复用登记必须把候选分为 `ADOPT`、`CONDITIONAL`、`REJECT`，并记录用途、exact version/digest、许可证/NOTICE、SBOM/provenance、运行权限、数据所有权、故障语义、资源上限、兼容矩阵、升级/回滚和退出路径；每项采用事实还必须以稳定 `source_id` 绑定标题、访问日期、可复核归档快照及 digest，上游动态网页、“成熟/稳定/latest”或一个 URL 不能替代项目资格；
- 成熟组件只能实现通用机制，不能取得 MASI-NIDS 的核心事实、业务状态机或设备副作用所有权。PostgreSQL、Go Control、Rust Edge、effect intent/journal/CAS、P4 generation/readback 和插件资格边界保持唯一；
- 采用 Prometheus/Alertmanager/Grafana/OpenTelemetry、PgBouncer/pgBackRest/Patroni、Buf/protoc、PTF/P4Testgen、Syft/Trivy/Cosign 等候选时，必须按 `DEC-025` 和 ADR-0009 的边界部署；其数据库、告警、dashboard、测试配置或签名记录都不是核心业务事实；
- 禁止因“避免造轮子”引入 Kafka/Redis/NATS/Temporal/Argo 等第二核心队列/工作流事实源、第二生产 P4 controller、Grafana 写操作、第三方应用控制面或通用授权引擎来旁路现有所有权；只有容量、故障隔离或业务缺口证据成立并提升需求基线后才可重新评估；
- 直接依赖、vendored/forked source、生成器、CLI、容器镜像、漏洞数据库、dashboard JSON、test fixture 和部署 chart 都属于供应链资产，必须可重现、可审计、可撤销；工具不可用时不得改变核心正确性或使旧结果自动获得新资格。

### 3.8 在线检测模型模块化（ARCH-MODEL-001）

- 模型替换分为三层：稳定的 central inference execution profile、不可变 model bundle/repository snapshot、由 `contracts/model/v1` 映射到稳定 `InferenceResult` 的 feature/label/output contract。Gateway、Triton、ONNX Runtime及CPU/CUDA profile、模型/optimized artifact 与合同 identity/digest 必须分别记录，禁止只保存一个可变 `model_id` 或 tag；
- 同一 feature-contract major、runtime profile 和 canonical output contract 下的模型算法、权重、阈值或类别集合可以作为新 immutable revision，在部署前完成离线 replay/qualification 后，通过启动新的 exact pool generation、预热/readback并按 shard切换 logical-pool binding；替换不得要求修改 Rust Edge 或 Go Event 业务代码，也不得把 model bytes 编译进 Gateway binary；
- 新模型需要新特征、改变 tensor layout/单位/缺失语义、引入新 runtime/backend/custom operator 或改变 output contract major 时，不是 drop-in replacement，必须新增 profile、old/new producer-consumer matrix 和滚动部署；
- label taxonomy 使用永不复用的稳定 label ID、版本和 digest，明确 single-label、multi-label、anomaly-score/open-set、unknown/OOD/abstain、threshold/calibration 和增删/合并/拆分映射；新增 label 默认只能形成检测事实，不能自动继承旧 policy/effect eligibility；
- output adapter 是受测的确定性代码/config，把模型原始 tensor 映射为 canonical scores/decisions/quality；adapter identity/digest 与模型一起资格化。任何模型输出、类别或置信度均不能直接创建 Proposal/Decision/Intent 或 P4 effect；
- `inference_shard` 是稳定的在线输入所有权/路由分片；Edge 是每个 shard 唯一 canonical input router，任何时刻只绑定一个 exact `(model_control_incarnation_id, shard_routing_epoch, logical_pool_id, pool_generation, binding_generation)`。同一 generation 的每次执行另记录 `worker_runtime_id/attempt_id`，但 replica 不是 canonical route owner。每个 `(inference_scope, inference_shard)` 至多一个 PostgreSQL finalized current generation，并保留一个仍合格的 exact previous；滚动中的旧/新 shard 各自绑定其 per-shard current，禁止跨 generation/window/rollup 混合；
- 首期每个 Triton instance 只启动加载一个 exact binding，固定 `model-control-mode=none`、strict readiness、禁用 auto-complete，并使用只读、闭包化的 repository/backend 目录。因为 NONE 模式会在启动时加载 repository 中的全部模型，snapshot 必须只包含 exact binding 及 manifest 明确声明的依赖；出现额外模型/version/backend/config 即拒绝启动。`config.pbtxt` 或等价配置必须显式固定 `instance_group` 的 kind/count/device，不能接受 Triton 即使禁用 auto-complete 仍可能补出的默认 instance group。生产禁止 runtime load/unload、repository polling、candidate/online shadow 和同一 endpoint 的多版本自动选择；新 revision 使用隔离的新 pool generation，候选比较在 Offline ML frozen replay 或隔离 rehearsal 中完成；
- Central Inference pool 的同 generation 副本必须使用相同 exact runtime profile；需要 HA 的部署可使用 active-active 和 N+1，但不允许 execution-plane fallback。单 replica 失败时 Edge 对 logical pool 使用原 request identity/input digest 有界重试；第一份通过 contract/fence/digest 的结果成为 canonical candidate，late duplicate 被丢弃或仅审计。全部 replica 不可用、过载或 deadline耗尽时对应 shard 不提交新 canonical result，Edge 只在既有 WAL/checkpoint 域内缓冲/背压，并在超限时形成 durable gap range/HOLD 与 resume watermark；不得建立第二 pending queue，也不得回退到 Edge-local、另一CPU/CUDA profile、另一backend/model、开发默认模型、`latest` 或随机旧文件。

### 3.9 在线遥测与推理热路径（ARCH-TELEMETRY-001）

- 首期生产默认采用 `telemetry-p4-window/v1`：P4 数据面只维护有界 per-target/per-window counter/register/metadata aggregate，Edge 通过已资格化的 bank flip、freeze/barrier、epoch 或 sequence-before/after 机制取得可证明一致的快照；不能证明 snapshot 的窗口为 `partial/not_measurable`，不得按完整输入推理；
- P4Runtime Digest 只用于 window-ready hint、稀疏异常提示或 bounded sampled metadata。DigestListAck 仅表示 Edge 已把对应消息写入自身 telemetry WAL并允许 target清理缓存；P4Runtime 1.4.1明确该机制不是可靠传输，target可在server/channel/client过载时丢消息，因此Ack、`max_timeout_ns=0`或`max_list_size=1`均不能证明逐包覆盖。PacketIn/clone同样只允许有明确sample/truncate/queue/drop上限的诊断或evidence；
- feature contract 明确需要 packet header/flow reconstruction，或目标无法提供合格 P4 aggregate时，可以在专用mirror/TAP上启用`telemetry-mirror-packet-mmap/v1`；只有该profile无法满足已冻结容量且NIC/driver证据成立时，才启用`telemetry-mirror-af-xdp/v1`，并强制exact XDP_DRV/XDP_ZEROCOPY或单独资格化copy profile，禁止静默fallback。DPDK只保留为未来hardware line-rate条件profile；
- capture backend是Rust Edge内部adapter，不新增长期在线模块。任何backend都必须输出统一`contracts/telemetry/v1` source/window/quality合同；Edge保持唯一P4Runtime client、source/window owner和每shard canonical inference router，Central Inference不得读取P4/NIC，Go不得接收raw packet；
- 默认热路径只传 feature tensor 与必要 identity，不传 raw packet/payload。Edge↔Central Inference 首期只使用 `inference-central-grpc-batch/v1`：预分配 buffer 的异步 bounded batched-unary Protobuf/gRPC over mTLS，复用 channel/connection并由服务发现选择同 generation replica；禁止逐 record RPC、一个跨全池的长寿命双向 stream 和未声明 transport fallback。Edge transport 只可无额外等待地合并已经 final/admitted 的记录；需要等待的 dynamic batching 只由 Triton 按冻结的 max queue delay 执行；
- event time、source/export/ingest/processing time必须分开；窗口使用`[start,end)`、per-source/shard watermark和有界allowed lateness。只有final且quality valid的窗口产生canonical inference；final后迟到数据只形成`late_after_final`/gap evidence，不重开Event，不补零；
- 热路径的完整耐久顺序为 telemetry WAL→final window/inference-input WAL→central result→inference-result WAL→Edge到Go有界批次→PostgreSQL canonical Event durable→Go ACK→Edge checkpoint/compaction。gRPC success、Gateway/Triton success或同 generation replica retry 不是 source/Event ACK；
- wire、source profile、window/feature contract分别版本化；Protobuf payload 和 tensor bytes 必须有固定 dtype/shape/order、长度/digest与 checked bounds。wire、单位、window、flow-direction、quality或feature major变化时使用新 pool/profile并在drain后单路切换，禁止旧新 pool 长期双写 canonical window/Event。

### 3.10 BMv2 无状态防火墙平面（ARCH-FW-001）

- 首期执行目标固定为经 `p4-stateless-firewall/v1` 资格化的 BMv2 `simple_switch_grpc`/v1model P4 pipeline；这是交换机数据面能力，不是 Linux 主机 UFW/nftables/iptables，也不允许借 Mininet namespace、netem/qdisc 或宿主机 ACL 代替 P4 action；
- 数据面按 `incident response overlay → baseline policy → forwarding/default` 的确定顺序执行。response overlay 只承载现有 R1/R2 有 TTL 临时处置；baseline policy 承载长期版本化无状态 allow/drop 策略。两个逻辑层不是两个控制面，均由 Go/PostgreSQL 持有事实并只通过唯一 `effect_intents`、Rust Edge journal/readback 和 PostgreSQL CAS 收敛；
- baseline policy 首期使用双 bank：Edge 只向 inactive bank 写入一个完整 revision，逐 entity exact readback 后以单一 selector entry 切换 active bank；旧 bank 在有界 rollback grace 内保留，随后沿同一 durable operation 清理。selector、bank、policy generation、P4Info/profile/canonical plan digest 分开携带，禁止把批量写 ACK 或 selector 成功冒充完整 policy 已资格化；
- P4 热路径只执行有界 parser、exact/ternary lookup、permit/drop 和 direct/eligible counter，不调用 Go、数据库、插件、LLM 或逐包控制面。policy 编译、overlap/shadow/capacity 分析、审批和观测均位于低频控制面；
- 首期强制 IPv4 无状态语义；IPv6 可以继续被 telemetry/model 检测，但 IPv6 真实处置在独立 P4 parser/table/profile 和 packet oracle 完成前必须返回 `unsupported/HOLD`。首期不实现 conntrack、TCP handshake state、NAT、L7、动态 Bloom-filter allowlist、任意用户 P4 程序或运行期 shared-object module；
- BMv2 只形成 exact software-target 功能、故障和性能证据；硬件 P4、ASIC line-rate、目标专用 atomicity/counter/TCAM 资源必须使用新 target profile 独立资格化。

### 3.11 受控多 Target 与设备管理边界（ARCH-TARGET-FLEET-001）

- Go Control 的模块化单体内置 `Target Registry` 与 `Fleet Coordinator`；PostgreSQL 保存 canonical target identity、期望/观测 profile、assignment、lifecycle、scope、capability observation 和 fleet operation。它们不是新的在线模块、第二设备控制器或通用 NMS；
- `target_id` 是系统生成、稳定且永不复用的业务身份；P4Runtime `device_id`、endpoint、hostname、序列号和机框/端口信息是可变化属性，均不能替代 `target_id`。每次 assignment 使用单调 `target_assignment_generation`，P4 session/pipeline 继续使用独立 application generation、election、role、P4Info/pipeline digest；字段不得折叠；
- Rust Edge 在一个进程内由 `TargetSupervisor` 管理多个有界 `TargetActor`。每个 actor 独占一个 target 的 StreamChannel/mastership、P4 I/O、journal、telemetry/source、rule observation 和 per-target 调度队列；target 故障、慢响应或 backpressure 不得无界阻塞其他 actor。任何 `(target_id, device_id, role, application_generation)` 仍最多一个 active writer；
- fleet operation 是 Go/PostgreSQL 中不可 claim 的聚合事实，只冻结 target-set snapshot/digest、ordered waves、logical change/authorization digest、failure policy、deadline 和 per-target child vector。每个实际 target mutation 仍是既有 `effect_intent`，按 `(fleet_operation_id,target_id,effect_digest)` 幂等、独立 claim/journal/readback/CAS；禁止创建 `fleet_jobs` 或第二 dispatcher；
- P4Runtime batch atomicity 只在一个 target 的能力范围内解释。跨 target 不存在项目承诺的原子 commit：每 target current/failed/unknown 单独成立；父级仅投影 `planned|running|partial|reconciling|applied|failed|aborted` 并始终保存 exact child vector。任何 child `unknown` 时父级为 `reconciling`，不能显示全局成功；
- rollout 首期只支持冻结的静态 target cohort：显式 canary wave 后按 ordered waves 继续，failure policy 封闭为 `fail_fast|continue_isolated|manual_gate`；target membership 在 operation 内不可动态变化。这里的 canary 是设备批次，不是加权流量或跨 target 事务；rollback 必须创建新的 fleet parent 与 per-target child intents，并以各 target exact previous 为输入；
- P4Runtime 继续承担已资格化 P4 pipeline 的表项读写与仲裁；规范未覆盖的端口、机框、设备发现和完整运维不能由项目猜测。`target-gnmi-readonly/v1` 可在真实设备/Stratum profile 触发后条件启用，仅允许 allowlist 的 `Capabilities/Get/Subscribe` 与 mTLS、路径/model/version、bytes/rate/deadline/freshness/gap 上限；首期禁止 gNMI `Set`。gNOI 和设备 OS/证书/reboot 等 mutation 不在本基线；
- Stratum 只可作为经资格化的 target-side P4Runtime/gNMI 实现，不取得 MASI controller ownership；ONOS/厂商 controller 作为生产规则 writer/scheduler 被拒绝。NetBox/CMDB 只可经显式 review 导入 inventory candidate；Ansible/Nornir 只可用于离线 provisioning、测试拓扑或只读核验，均不得持有生产 effect/P4 writer credential或回写 canonical current。

## 4. 模块与技术栈需求

### 4.1 P4/Switch 模块（MOD-SW-001）

技术栈：P4_16、BMv2；后续可适配硬件 P4 target。

职责：

- 数据包转发；
- 有界 counters/registers/digest；
- 通过目标资格 profile 提供 per-target/per-window aggregate、bank/epoch/snapshot 或等价一致性原语；不能提供时明确暴露 capability 缺失，不由 Edge 猜测原子快照；
- 为需观测的 effect table/action 提供已资格化的 per-entry direct counter，并在可计算比例的 profile 中提供同 table/stage eligible counter；
- 受控的镜像或 bounded capture；
- 执行由唯一 Edge Agent 写入的 P4 table entry；
- 暴露可读回的真实设备状态。

禁止：

- 感知模型、LLM、Event、Incident、PostgreSQL 或前端；
- 由 Control、Plugin 或 Frontend 直接写入；
- 存储核心业务状态。

### 4.1a BMv2 无状态防火墙数据面（MOD-SW-FW-001）

首期 mandatory target profile 为 `bmv2-simple-switch-grpc-v1model/firewall-v1`；具体 BMv2/p4c/container digest、P4 source/P4Info/device-config digest、启动参数、端口图、table/action/counter ID 和 target capability 由 `contracts/profiles/v1` 固定，本文不以 `latest` 或工具默认值代替。

数据面必须至少包含：

- `response_overlay`：优先执行的 exact IPv4 五元组临时规则，动作只允许 profile 声明的 `drop` 或 `permit-and-continue`，每条规则有 direct counter；真实 response rule 必须有 Go-owned expiry，P4 idle-timeout notification 只能作 best-effort hint；
- `baseline_policy`：以 `policy_bank`、ingress port（可选）、IPv4 source/destination prefix、protocol、L4 source/destination exact-or-wildcard port 和 fragment class 为 canonical key 的有界策略表；profile 额外资格化 port range 时才能接受 range，禁止无上限展开；动作首期只有 `permit-and-continue` 与 `drop`；
- `policy_selector`：单 entry 将 packet 绑定到 active baseline bank。inactive bank 全量安装/readback 完成前不得切换；切换失败、结果不明或 readback mismatch 必须保持旧 bank current 或进入 `unknown/reconciling`，不得同时声称两个 bank active；
- 每个可执行 response/baseline action 的 per-entry direct counter，以及同 stage 可资格化的 eligible counter；permit 只表示继续既有 forwarding pipeline，不拥有 L2/L3 rewrite，drop 必须形成独立 packet oracle 可验证的无预期 egress；
- parser/metadata 明确表示 IPv4、TCP/UDP/ICMP、`unfragmented|first_fragment|non_initial_fragment`、L4 port presence 和 parser error。使用 L4 port 的规则不能匹配 port unavailable 的非首片；malformed/truncated/parser-error 按 profile 固定动作并首期默认 fail closed drop；非 IPv4 packet 不得误匹配 IPv4 规则，其处置能力明确为不在首期 firewall coverage。

规则 precedence 必须完全确定：response overlay 先于 baseline；baseline 中更高显式 priority 优先；任何可能匹配同一 packet、priority 相同而 action/参数不同的物理 entry 在编译期拒绝。default action 必须由 exact policy revision 显式选择 `permit-and-continue|drop` 并进入 proposal digest；改变 default、selector、P4Info、pipeline 或无 TTL 永久响应规则属于 R3，不能走 incident effect API。

容量分别计算 logical rules、展开后的 active physical entries、inactive bank entries、response entries、direct-counter resources 和 selector；`4,096 observable rules` 不自动等于任一 P4 table 的可写容量。任一维度超限必须在写前返回稳定 `capacity_exceeded` 且零 P4 mutation。

### 4.2 Rust Edge Agent（MOD-EDGE-001）

技术栈：Rust stable、Tokio、Tonic/Prost 或等价受支持实现；本地文件/WAL 使用明确的 fsync 和原子替换语义。

职责：

- 唯一生产 P4Runtime session、mastership、读取和写入；
- 由进程内 `TargetSupervisor` 管理有界 target actor 集合；每个 `TargetActor` 只拥有一个 registered target 的 endpoint/role/election/application generation、StreamChannel、journal、telemetry source、rule-observation 与调度队列，并验证 Go 签发的 `target_assignment_generation`。新增 target 不新增第二 Edge 服务类型，也不允许 actor 共享可写 journal/queue；
- P4 counter/digest 读取与严格解码；
- 作为唯一 telemetry source owner 实现 `telemetry-p4-window/v1`，并按部署选择可选 digest/PacketIn sample 或 mirror capture adapter；验证 target/source profile、observation point、sequence/epoch/snapshot/coverage/drop，不把 Digest ACK 或少量样本当完整覆盖；
- 规则 direct-counter 的低优先级 bounded sweep、canonical identity、sequence、reset/generation fence 与 RuleObservationBatch；
- 依据 `p4-stateless-firewall/v1` 和当前 P4Info 将 Go 的 normalized firewall policy/effect 编译为 canonical P4 entity plan；执行 overlap/priority/field-presence/physical-capacity preflight，返回绑定 logical revision 与 concrete plan 的 digest，但不把 preflight 当 reservation 或授权；
- 沿唯一 effect operation 对 response overlay 进行 install/delete/expiry reconcile，并对 baseline inactive bank 执行 bounded batch write、逐 entity journal/readback、selector cutover、rollback grace 与旧 bank cleanup；
- telemetry 窗口聚合、sequence、generation/session/cookie 管理；
- 分离 event/export/ingest/processing time，执行 `[start,end)`、watermark、allowed-lateness、final-only inference、IPv4/IPv6/fragment/flow-direction 和 quality/gap 语义；
- segmented binary WAL、checkpoint、backpressure 和 crash recovery；
- 通过`inference-central-grpc-batch/v1`向logical Central Inference pool发送有界批量数据；预分配并复用buffer，执行request/input digest、deadline、per-source quota、bounded in-flight、channel reuse、backpressure与同identity重试；
- 作为每个 `inference_shard` 唯一 canonical input router，执行 route withdraw/drain、`shard_routing_epoch` fence、WAL-backed bounded pending、gap range/resume watermark 和 committed-binding handshake；deployment/service discovery只能在 exact logical pool generation 内选择 replica，不能并行制造第二 canonical route；
- 与 Go Control 维持有界、可重连的 gRPC stream；
- 向 Go 上报每 target 的实际 actor/runtime epoch、arbitration、pipeline/P4Info/capability、queue/freshness 和 read-only reconcile observation；不得把 target process 存在、TCP 可连或 gNMI 健康值当作可写 readiness；
- 在 Go/PostgreSQL canonical ACK 前保存 inference result WAL并沿原 identity 重放；Gateway/Triton/gRPC 成功不得提前推进 source cursor 或清理可恢复结果；
- 执行 effect operation，保存本地 operation journal 并提供真实 readback；
- 在断网期间进行有界本地缓存，不猜测 Control 侧提交结果。

禁止：

- 直接连接核心 PostgreSQL；
- 运行 LLM、LangGraph 或业务策略；
- 根据模型分数自行生成任意 P4 规则；
- 在未知 operation 结果时盲目重写。

### 4.3 Central Online Inference（MOD-INF-001）

技术栈：C++20 或 C++23、CMake、digest-pinned NVIDIA Triton Inference Server、ONNX Runtime；首期提供两个互斥的启动profile：`model-runtime-central-cpu/v1`固定CPU EP、CPU feature/thread/NUMA/RAM，`model-runtime-central-cuda/v1`固定CUDA EP、CUDA/cuDNN/GPU/VRAM/driver。每个pool generation只能绑定一个profile。TensorRT只能作为未来另一个显式、离线资格化的pool/backend generation，不能按运行时可用性自动替换ORT CPU/CUDA；LibTorch不属于首期backend。

职责：

- C++ MASI Gateway 是唯一对 Edge 暴露的 mTLS endpoint，接收版本化、固定 tensor 语义的有界批量推理输入；Triton HTTP/model-control/metrics 端口不得暴露到 Edge、用户或插件网络；
- Gateway 严格验证 framing/body/record/tensor count、dtype/shape/order/length、deadline、source/window/model/logical-pool/route identity、request/input digest和全部合同；执行 source/shard quota、bounded admission和结果 schema adapter，但不持有 durable queue、不重新构造窗口、不引入第二个 delay-based batch scheduler；
- Triton dynamic batcher 是唯一可按 `preferred_batch_size/max_queue_delay` 等待合批的组件；instance group、concurrency、priority/timeout 和 queue policy 必须由 benchmark 固定，不能仅因增加 instance count就声称提速；
- startup-selected ONNX Runtime CPU或CUDA backend执行scaler/feature adapter、模型推理、threshold/OOD或等价确定性计算并返回批量`InferenceResult`；CPU profile固定thread/affinity/NUMA/arena，CUDA profile固定device/stream/显存及资格化时已声明、冻结、读回和测量的host-side operator placement。ORT provider partition变化、运行期出现新的CPU节点接管或unsupported operator必须fail closed，不能被描述为切换CPU profile或合法fallback。使用预分配input/output，I/O Binding/device tensor/pinned memory只有exact CUDA profile实测后才可宣称减少host/device copy；
- 每个 Triton instance 只在启动时读取受控 immutable pool envelope 和只读、digest-pinned、闭包化 model repository snapshot；固定 `model-control-mode=none`、strict readiness、禁用 auto-complete，验证 manifest、repository成员/依赖闭包、ONNX metadata、feature/label/output adapter、wire、runtime/backend、shape/dtype/opset、optimization artifact、显式instance group和资源 profile；
- 完成 repository verification、backend/Session create、warmup、Gateway↔Triton probe 和 exact identity readback 后才通过 startup/readiness。只读 `GetLoadedModel/GetPoolStatus` 返回 model-control incarnation、logical pool/pool generation、worker runtime identity、operation、binding generation、optimization identity和全部 digest；`loaded/ready/min-ready` 不等于 PostgreSQL current；
- 仅处理与实例启动 binding、Edge route epoch和Go已提交 per-shard current generation一致的batch；旧、未提交、未知或跨 incarnation/shard/route/pool/binding generation 的结果被 fence，不提供生产 candidate/shadow 比较通道；
- 对错误 shape、dtype、NaN、Inf、未知版本和超限 batch 明确拒绝；
- 支持独立离线 replay 和 benchmark。

禁止：

- 连接 PostgreSQL；
- 直接读取 P4/BMv2 或 capture NIC；
- 直接打开 capture NIC、AF_PACKET/AF_XDP/DPDK、读取 raw packet或自行重建第二flow/window；
- 执行业务策略或 P4 effect；
- 使用 Python 对象作为跨模块 ABI；
- 把模型置信度冒充业务处置授权。
- 按 tag/文件名/mtime 自行选择模型，运行期联网下载模型，原地覆盖 active bytes，或加载 model bundle 携带的未资格化 native/Python/Wasm/custom-op 代码；
- 自行写入 model catalog/binding、把进程内 current pointer 当 canonical fact，或让旧 generation late result 冒充当前结果。
- 在生产实例接受 load/unload/swap/repository polling mutation，按 mutable model version自动选择、创建 candidate/shadow，或在同一 logical endpoint混用不兼容 execution provider/offline-optimized artifact；新 revision/backend必须使用新 pool generation并通过完整资格门禁。
- 在所选ORT CPU/CUDA profile不可用、过载或失败时自动切换另一profile、TensorRT、LibTorch、另一模型或Edge-local inference；CPU/CUDA选择只能来自启动前人工确认的immutable envelope，任何profile变化都必须创建新pool generation并完整资格化，不能是故障fallback。

### 4.4 Go Control Core（MOD-CTRL-001）

技术栈：受支持的 Go stable、标准 `net/http` 或轻量路由、gRPC-Go、`pgx`、`sqlc` 或等价静态 SQL 工具。

内部模块：

- Event Ingest；
- Incident/rollup；
- deterministic policy；
- Effect Proposal/Authorization；
- Effect Intent/Outbox；
- Edge RPC client 与 reconcile；
- Runtime/operations；
- Target Registry/Fleet Coordinator：稳定 target identity、registration/verification/lifecycle、Edge assignment generation、capability observation、scope、冻结 target-set/wave、per-target child intent 与 fleet aggregate projection；
- Model Manager：model catalog、qualification、model-control incarnation、rollout/recovery operation、per-shard desired/current/previous binding、routing/deployment/startup/readback/commit observation、rollback 与 mixed-rollout 状态投影；
- Rule Observation：epoch/current/status/rollup、公式/coverage/quality 与只读诊断候选；
- Plugin Manager：catalog、admission、qualification、activation、binding、revocation 与状态投影；
- Plugin Statistics：冻结输入、on-demand/periodic schedule、run/idempotency、Artifact 校验、current/history 投影与授权 API；
- REST/OpenAPI API；
- read-only Agent MCP endpoint；
- Analysis Plugin A2A client；
- Frontend aggregation。

职责：

- 验证并批量持久化 Edge inference event；
- 验证每个 inference result 的 inference shard、model revision/binding generation、feature/label/output-adapter/profile digest；拒绝 late、未提交、跨 shard、未知或不兼容结果进入 canonical Event；
- 维护 Event、Incident、cursor、runtime 和 effect 事实；
- 维护 immutable/stable target registry、assignment/lifecycle、expected profile 与 observed capability；对第三方 inventory 只接受有 provenance/digest 的 candidate，经差异预览和 scoped Admin 确认后才写 canonical fact；
- 维护 immutable firewall policy revision、qualification/compile result、desired/current/previous binding、activation operation 和 rule-to-physical-entry 映射；policy/binding 事实仍位于核心 schema，不由 Edge/P4/插件持有；
- 创建不可变 effect proposal 和 append-only authorization decision；
- 由已资格化确定性策略或有效授权决定，在重新验证当前事实后创建唯一 effect intent；
- 对 typed fleet operation 冻结 target set 与 ordered waves，在同一短事务中按 exact authorization 创建幂等的 per-target `effect_intent` children；父级 aggregate 绝不可被 dispatcher claim，父级只从 child durable facts 投影且不隐藏 partial/unknown；
- 以短事务 claim intent，事务外调用 Edge，随后 CAS finalize；
- timeout-after-effect 时标记 `unknown` 并通过 operation ID/readback 收敛；
- 在 exact effect readback 后建立 rule observation epoch，验证 Edge cumulative sample 并持久化 current/status/rollup；
- 为 response rule 与 baseline policy activation 生成 normalized logical diff、风险、审批上下文和唯一 durable intent；baseline revision 只有在 inactive-bank readback、selector readback 与 CAS 完成后才能成为 current；
- 暴露前端 API、只读 MCP 工具和插件状态；
- 持久化插件平台控制事实并向 Runtime Host/独立插件签发有界、版本化的 activation/binding；
- 从 Go-owned canonical facts/projections 生成有界 `StatisticsInputBundleV1`，在事务外调用已准入统计 producer，校验 `PluginStatisticsArtifactV1` 并只由 Go 写入统计投影；
- 持久化模型平台控制事实；使用短事务创建rollout、new pool generation和per-shard desired generation，在事务外按幂等deployment action完成repository pre-stage、新Gateway/Triton replica启动、warmup/readback及所选availability profile的capacity qualification（HA另含failure-domain/min-ready/N+1），再由Edge逐shard route withdraw/drain，以短事务按model-control incarnation/expected generation CAS finalize current/previous；随后向Edge发送exact logical-pool committed-binding handshake。CAS后handshake失败保持新current+`resume_pending/unavailable`并沿原identity reconcile，不暗中回退；
- 插件禁用或故障时返回明确 unavailable，而不影响核心 API。

禁止：

- 直接持有 P4Runtime writer；
- 在数据库事务中等待 Edge、LLM、MCP、A2A 或其他 HTTP/gRPC；
- 通过 Redis 或进程内队列替代 durable effect intent；
- 把 proposal/decision 表实现成第二 effect queue，或让审批 API 直接等待 Edge/P4；
- 接受 Agent 或通用插件输出直接成为 P4 entry；
- 在 Go 进程中动态加载或执行第三方插件代码。
- 在数据库事务或持有连接期间拉取模型、编译/加载runtime、停止/启动pool/worker、等待Gateway/Triton readiness/readback，或让systemd/Kubernetes/MLflow/KServe/Triton repository/OCI tag成为canonical current binding。

### 4.4a Target/Fleet 跨模块职责（MOD-TARGET-FLEET-001）

本需求不新增可部署模块，而是在既有 Edge、Control、State、Web 与 P4 qualification target 中分配明确职责：

- Go `Target Registry`：canonical target identity、lifecycle、desired profile、external candidate review、scope 与 Edge assignment；
- Go `Fleet Coordinator`：冻结 target set/waves、创建 parent + per-target child intents、gate 与 aggregate projection；不执行P4、不成为第二队列；
- Rust `TargetSupervisor/TargetActor`：按 assignment 在一个Edge进程内隔离每target P4Runtime session、journal/source/observation/queue、公平与故障；
- PostgreSQL `target_fleet`：唯一持久事实、unique constraint/CAS、control incarnation、parent-child和audit；
- Web `Managed Targets/Fleet Operations`：只消费Go投影，按target向量展示，不直连设备；
- P4/BMv2：仍只执行自身table/action/counter；无target registry、fleet transaction或跨设备协调逻辑。

上述职责只能通过`contracts/target/v1`、`contracts/fleet-operation/v1`及既有effect/P4契约协作。任何未来独立inventory service、device orchestrator或fleet scheduler都属于新增部署模块/所有权，必须提升基线并重新评估连接、状态、故障、权限和Module Gate。

### 4.5 PostgreSQL State Store（MOD-DB-001）

技术栈：PostgreSQL 18 的受支持补丁版或经兼容验证的更高版本；生产使用 PgBouncer，并在条件允许时优先采用托管 HA。无法采用托管服务时，自建方案必须交付 Patroni 或等价 failover、PITR 和恢复演练。

职责：

- 保存核心 durable facts；
- 保存 target registry、assignment generation、lifecycle/capability observation、fleet operation 与 per-target child mapping；
- 提供事务、唯一约束、幂等、CAS、分区和 retention；
- 保存 rule observation epoch、current projection、bounded rollup、status/reset event 和 outcome evidence 引用；
- 保存 immutable firewall policy revision/rule、compile/overlap/capacity evidence、desired/current/previous binding、bank/selector/activation operation、logical-to-physical mapping 和审计引用；实际设备状态仍由 Edge readback 观察，数据库记录不能自证安装；
- 保存 immutable model revision/qualification、rollout operation、per-shard desired/current/previous binding、startup/readback observation 和审计引用；
- 支持备份、WAL archive、PITR、standby/failover 和审计；
- 为核心与 plugin 提供隔离角色/schema。

PostgreSQL 不保存逐包原始数据，不代替 Edge 本地 WAL，也不把所有 telemetry sample 逐条写入核心事实表。

生产资格前必须在 `reliability-environment/v1` 中冻结并由 Owner 接受数据规模、拓扑、故障域、同步/异步复制、WAL archive 周期以及数值 RPO、自动 failover RTO 和隔离 PITR restore RTO；未冻结或未实测时只能为 `HOLD/NOT RUN`。restore 验证必须重算 schema/migration checksum，并验证 Event/Incident、Proposal→Decision/Policy→Intent→Attempt/readback→rule observation epoch/rollup、model revision→qualification→rollout operation→per-shard current/previous binding、plugin revision→qualification→binding/revocation，以及 statistic schedule→run→artifact→current/history 引用链。

### 4.6 Python Analysis Plugin（MOD-AGENT-001）

技术栈：CPython 3.12 及更高版本；生产镜像必须固定到当时仍受安全支持的 minor/patch，并由 CI 覆盖 3.12 兼容基线与目标生产版本；LangGraph、Pydantic v2、MCP SDK、A2A 1.0 SDK/协议实现、httpx/等价受限 HTTP client。

职责：

- 接收手工、订阅或 A2A 委派的分析任务；
- 读取冻结的 Event/Incident/Evidence/Runtime/P4 引用；
- 使用固定 LangGraph 和有界 LLM/MCP/A2A 调用完成证据约束分析；
- 生成不可执行的 AnalysisArtifact、Recommendation、报告、通知稿、工单草稿、调查清单和复盘内容；
- 提供 A2A Agent Card、Task/Message/Artifact 接口和只读 trace；
- 保存自身 Task、Run、Artifact、Trace 和可选 subscription cursor。

禁止：

- 直接写核心 schema；
- 调用 P4 write、effect、approve、deploy、rollback 或 capture mutation 工具；
- 调用核心 effect proposal/authorization mutation；人类可以在新 proposal 中显式引用 Artifact，但插件不能代人发起或授权；
- 将 LangGraph checkpoint 或 A2A Task 当作核心业务状态；
- 把外部 A2A Agent 结果当成可信网络事实；
- 使核心检测、持久化或处置依赖插件可用性。

### 4.7 Frontend（MOD-WEB-001）

技术栈：TypeScript、Vue 3 Composition API、Vite、Vue Router、Pinia、Element Plus、Apache ECharts；服务端状态使用通过 `web-spa/v1` 资格化的 Vue Query 类库，长列表按需使用资格化的虚拟化库。样式使用项目自有 design tokens、受控 Tailwind utilities 与 scoped Sass；生产不得依赖 runtime CDN、远程字体或远程 JavaScript。

Frontend 是独立静态 SOC SPA，不承担 BFF、OIDC token exchange、业务授权、canonical projection 或设备副作用。生产由同一 HTTPS origin 提供 SPA、`/api` 与 `/events`；Go Control/受信网关拥有 OIDC callback、受限会话、CSRF 和所有业务授权。内部控制台没有 SEO/SSR 需求，不引入 Next.js server runtime。

职责：

- 展示 Event、Incident、Runtime、Effect Proposal/Decision/Execution 和 Analysis Artifact；
- 在 Operations & Audit 下展示 Managed Targets 与 Fleet Operations：target identity/alias、desired/observed profile、Edge assignment/actor/mastership、P4Info/application generation、freshness/health、每 wave 与每 target child 结果；混合状态不得压成一个绿色结果；
- 展示规则 installation、dataplane match、eligible/no-traffic、reset/gap 与 independent outcome 分层的 Rule Effectiveness 工作台；
- 在固定 `Plugins → <plugin> → Statistics` 路由展示经 Go 校验的插件统计 current/history；只用内置 renderer，不加载插件提供的组件、route、HTML、ECharts/Vega option 或脚本；
- 在 Effects & Governance 下分开提供 `Response Rules` 与 `Firewall Policies`：前者供 Analyst 提出有 TTL 临时处置、Operator 按 R1/R2 授权；后者供 scoped Platform Admin 创建 immutable baseline revision、查看 normalized diff/overlap/capacity/default/bank plan，由不同稳定身份的 Operator 完成 R3 policy activation 授权；
- 为具有明确 plugin scope 的 Platform Admin 展示插件 catalog、准入/资格证据、capability/resource profile、active generation、运行状态以及 exact-binding activation/rollback/revoke 操作；
- 在Operations/Models中分开展示desired/selected/observed CPU或CUDA runtime profile、hardware mismatch、model bundle/repository、feature/label/output-adapter、image/config digest、qualification、`availability-single|ha`、logical pool/current-new-previous generation、replica/capacity及所选availability profile适用的failure-domain/N+1、CPU core/thread/NUMA/RAM或GPU/driver/CUDA/cuDNN/VRAM、per-shard desired/current/previous、probe/start/load/warmup/readback、route drain/CAS、CPU/CUDA独立E2E evidence、group`rolling_mixed`和exact rollback；不得显示/提交`auto/best available`、mutable tag、runtime load/unload/fallback，也不得把Pod/Triton Ready显示为current或把一种profile的PASS展示为另一种；
- 依据 `WEB-UX-001` 重构任务导向的信息架构、导航和运维交互；legacy/第三方控制台只作为行为参考，不构成页面兼容或源码依赖；
- 只通过 Go Control API 获取核心数据；
- Analysis 页面通过 Go 获取 A2A Task/Artifact 投影；
- mutation timeout 后查询原 operation，不自动创建新 operation；
- 正确表达 missing、stale、unknown、failed 和 generation change；
- 审批界面必须展示 exact logical P4 diff、作用域、TTL、回滚、证据新鲜度、generation/P4Info、容量和未知项，不得把 Agent 文本或置信分数包装成执行授权；
- 交付 `WEB-UX-001` 至 `WEB-SUPPLY-001` 及 `WEB-RULE-001` 的交互、状态、安全、性能、可访问性和依赖准入门禁。

禁止：

- 直连 PostgreSQL、P4、Edge、Inference、Plugin Host、LLM/MCP/A2A provider，或在浏览器持有这些身份；
- 把 Pinia、浏览器缓存、URL、localStorage/IndexedDB 或 Service Worker 当作核心事实、授权结果或离线执行队列；
- 加载任意 UI JavaScript plugin、远程模块、第三方控制面页面或 iframe；
- 复制 1Panel/sub2api 的业务 API、账户/权限模型、部署动作、页面源码、品牌、图标、文案或主题来替代 MASI-NIDS 契约与设计。

### 4.8 离线 ML 工具链（MOD-ML-001）

技术栈：Python 3.12 及更高版本；生产/训练环境固定受支持 minor/patch；PyTorch/NumPy/scikit-learn 或项目实际需要的工具。

职责：

- 训练、评估、回放、模型导出和 golden vector 生成；
- 导出Triton/ONNX Runtime CPU与CUDA profile均可按各自资格合同加载的稳定ONNX模型与repository snapshot输入；
- 生成简单、版本化 artifact manifest 和 SHA-256；
- 生成不可变 model bundle：模型/可选 scaler、feature schema reference、label taxonomy、output-adapter config、threshold/calibration/OOD policy、resource profile、runtime/backend profile 和全部 digest；
- 不要求自研四域签名或 signed release report；
- 为每个候选模型生成机器可读 qualification manifest，绑定 train/validation/test 数据 digest、特征/标签合同、seed、工具链、训练 config、模型/scaler digest、数值容差、离线 replay quality threshold、OOD/NaN/Inf、性能/资源结果和 rollback compatibility；
- quality threshold 必须由 model profile 给出具体数值并经 Owner 接受，不能用“优于旧模型”或单一 accuracy 代替 precision/recall/FPR/FNR 与目标分布证据；未通过或数据/工具链无法重建时为 `HOLD/NOT QUALIFIED`。
- 只产出candidate/qualification/offline-comparison evidence，不写PostgreSQL model binding、不控制Central Inference生产pool rollout、不把训练registry alias/tag当生产identity；ONNX metadata可以冗余自描述，但必须与外部manifest/contract一致且不能替代digest、provenance或资格。

### 4.9 Plugin Runtime Host（MOD-PLUGIN-001）

技术栈：Rust stable、Tokio、Tonic/Prost、固定且受支持的 Wasmtime 版本、首期资格 profile WASI 0.2（Preview 2）Component Model/WIT；OCI service 运行由 systemd/Podman/Kubernetes 等部署层负责，Host 不实现容器编排器。

职责：

- 接收 Go Plugin Manager 已验证、带 generation/epoch 和 digest 的 activation/binding；
- 加载并预编译已准入的 Wasm component，或连接同机受控 OCI service plugin；
- 执行 plugin handshake、contract/capability 校验、batch/task admission、deadline、取消、资源预算、熔断、drain 和结果 fencing；
- 仅暴露按 plugin kind 定义的最小 Host API，并对输入输出执行 framing、大小、schema、identity 和 digest 校验；
- 暴露 startup/readiness/liveness、低基数 metrics、受控日志和只读运行状态；
- 在插件崩溃、OOM、超时、越权、版本漂移或撤销时隔离对应 generation，并返回稳定 `unavailable/timeout/resource_exhausted/revoked`。

禁止：

- 连接核心 PostgreSQL、读取 Analysis Plugin 私有 schema、持有 P4/Edge/数据库/OIDC 高权限 secret；
- 创建 Event/Incident/Proposal/Decision/Intent 或把插件任务变成第二 effect queue；
- 把未签名、未验证、未知 kind/major、tag-only、`latest`、已撤销或 capability 超出 policy 的制品加载为 active；
- 让插件访问未声明网络、文件、设备、进程、时钟、随机源、secret 或宿主内存；
- 在 Rust Edge、Central Inference Gateway/Triton/backend 或 Go Control 进程内加载插件动态库、Python package 或 Wasm component。

### 4.10 首期模块注册表（MOD-REGISTRY-001）

以下九项是 `TEST-GATE-001` 中“所有首期模块”的完整集合；Target Registry/Fleet Coordinator 是 Go Control 的内部模块，TargetSupervisor/TargetActor 是 Rust Edge 的内部模块，不构成第十个在线服务。`contracts/`、`testkit/`、`deploy/` 和 `docs/` 是跨模块交付资产，不是额外可部署模块，但缺失自身门禁时仍会阻止相关模块完成：

| 模块 | 需求 ID | 目标目录 | 资格主体 |
|---|---|---|---|
| Switch | `MOD-SW-001` | `p4/` | P4 program/P4Info + BMv2/target profile |
| Edge | `MOD-EDGE-001` | `edge-rs/` | Rust Edge Agent |
| Inference | `MOD-INF-001` | `infer-cpp/` | 一个Central Inference资格主体：C++ MASI Gateway+pinned Triton+startup-selected ORT CPU或CUDA+read-only repository/compute profile |
| Control | `MOD-CTRL-001` | `control-go/` | Go Control modular monolith，含 Model/Plugin Manager |
| State | `MOD-DB-001` | `db/` | PostgreSQL schema/migration/restore qualification target |
| Plugin Host | `MOD-PLUGIN-001` | `plugin-host-rs/` | Rust Plugin Runtime Host |
| Analysis | `MOD-AGENT-001` | `analysis-py/` | Python Analysis Plugin |
| Web | `MOD-WEB-001` | `web/` | Vue 3/Vite SOC SPA |
| Offline ML | `MOD-ML-001` | `ml-py/` | Offline ML Artifact Pipeline qualification target |

不得通过遗漏目录、把 Offline ML/数据库称为“只是工具”，或把 Model Manager/Plugin Manager/Target Registry/Fleet Coordinator 拆成未登记的第二控制服务来改变该集合。以后增加可部署模块必须提升需求基线并更新本表、集成顺序、连接预算、故障域和 Module DoD。

## 5. 契约与通信协议

### 5.1 契约唯一源（CONTRACT-001）

仓库必须建立独立 `contracts/`，至少包含：

```text
contracts/
├── telemetry/v1
├── inference/v1
├── model/v1
├── event/v1
├── effect/v1
├── p4/table-entry-canonical/v1
├── p4/firewall-policy/v1
├── p4/rule-observation/v1
├── target/v1
├── fleet-operation/v1
├── testkit/traffic-replay/v1
├── db/effect-cas/v1
├── analysis/v1
├── plugin/v1
├── plugin/manifest/v1
├── plugin/wit/v1
├── plugin/statistics/v1
├── evidence/v1
├── web/v1
├── supply-chain/v1
├── profiles/v1
└── common/v1
```

Rust、C++、Go、Python 和 TypeScript 类型必须由同一 Protobuf/JSON Schema/OpenAPI/WIT 源生成或通过可验证适配器映射。模块不得复制并手工漂移同名契约。

`contracts/telemetry/v1`至少包含source profile/observation、flow/window identity、event-time/watermark、sampling/coverage、quality/gap和feature-input adapter；`contracts/inference/v1`至少包含central batched-gRPC input/result、tensor descriptor、request/attempt/logical-pool identity、quota/retry/dedupe/fence/error、result→Event mapping和跨语言golden；`contracts/model/v1`至少包含bundle/repository manifest、feature/label/output adapter、qualification、pool generation、per-shard binding、rollout、pool startup envelope和worker/pool observation；`contracts/p4/firewall-policy/v1`至少包含normalized policy、default/priority/overlap、response TTL、compile plan、bank/selector/capacity和readback；`contracts/target/v1`至少包含stable target、profile/credential reference、lifecycle、assignment和observation；`contracts/fleet-operation/v1`至少包含冻结target-set、waves/failure policy、child intent mapping、aggregate/vector和rollback；`contracts/testkit/traffic-replay/v1`至少包含fixture、topology/direction/rewrite、replay/session、impairment、ground truth、packet oracle和result；`contracts/plugin/statistics/v1`至少包含冻结输入、run identity/status、指标/series/table/quality/provenance/truncation、声明式显示 union、资源与错误；`contracts/evidence/v1`至少包含公共envelope、正交的level/applicability/result/qualification、claim scope、fault/performance/DB restore/model qualification+pool rollout/offline comparison；`contracts/web/v1`至少包含dashboard snapshot、实时event和状态语义；`contracts/supply-chain/v1`至少包含全系统inventory/adoption与稳定source_id→归档快照/digest登记；`contracts/profiles/v1`登记ADR-0005/0012/0013/0014/0015/0017/0018的现行wire/runtime/test/qualification/deployment/E2E-runner profile；ADR-0016仅保留v1.13历史身份。目录尚未形成实际schema与golden前只能为`result=HOLD, qualification=NOT_QUALIFIED`，不得创建空文件占位。

### 5.2 公共 envelope（CONTRACT-002）

跨模块消息必须按适用范围包含：

- `schema_version`；
- `message_id` / `operation_id` / `task_id`；
- `source_id` / `target_id`；
- generation/session/cookie；
- monotonic sequence；
- data timestamp 与 produced timestamp；
- idempotency key；
- payload length；
- content digest；
- producer version/config ID；
- policy/governance profile version、actor reference 和 authorization reference（适用时）；
- status/error code；
- `status_namespace` 或由消息类型唯一确定的领域状态；qualification、effect、model rollout/per-shard observation、plugin lifecycle、rule observation 和 UI transport 状态必须使用不同 enum/type，禁止以一个通用字符串 `status` 混装 `HOLD/stale/unknown/failed/unavailable`；
- trace/correlation ID。

### 5.3 版本规则（CONTRACT-003）

- 未知 major version 必须拒绝；
- minor version 兼容必须通过契约测试证明，不能自动假设；
- 发送方不得在未协商时静默降级；
- 持久化事实必须记录实际 contract version；
- 废弃版本必须有明确停止写入、停止读取和数据保留日期；
- effect proposal/decision/intent 的 minor 兼容必须覆盖旧写新读、新写旧读和持久化旧事实读取；未通过矩阵的组合必须拒绝 readiness 或 mutation。

### 5.4 协议选择（CONTRACT-004）

| 边界 | 首选协议 |
|---|---|
| P4 target ↔ Rust Edge | P4Runtime gRPC、本地 UDS/barrier |
| P4 target ↔ Rust Edge device-management adapter | 条件 `target-gnmi-readonly/v1`：OpenConfig gNMI over mTLS，仅 allowlist `Capabilities/Get/Subscribe`；首期禁止 `Set`，不得影响 P4 writer/effect readiness |
| Rust Edge ↔ Central Inference Gateway | `inference-central-grpc-batch/v1`：异步 bounded batched-unary Protobuf/gRPC + mTLS、channel/connection reuse、logical-pool generation routing；不使用逐记录 RPC、全池单一长寿命 bidi stream或本地推理 fallback |
| C++ Gateway ↔ Triton | 隔离推理网络上的 pinned Triton gRPC inference API；Triton model-control/HTTP 不对外暴露。共享内存扩展只有 copy benchmark 触发并单独资格化时采用，不是首期默认 |
| Go Model Manager ↔ Central Inference control | 低频 Protobuf/gRPC + mTLS：pool startup/readback/min-ready/qualification/route-commit；不得与 hot-data admission 共享无界队列或让 Gateway/Triton写 current |
| Rust Edge ↔ Go Control | Protobuf/gRPC streaming + mTLS |
| Go Control ↔ PostgreSQL | PostgreSQL wire、pgx、COPY Binary |
| Frontend ↔ Go Control | 同源 HTTPS REST/OpenAPI + `/events` SSE；Go 持有 session/CSRF/授权，SPA 无 token/BFF |
| Go Plugin Manager ↔ Plugin Runtime Host | Protobuf/gRPC + mTLS；同机可以使用受保护 UDS |
| Runtime Host ↔ Host-managed OCI service plugin | Protobuf/gRPC；同机 UDS 优先，跨机必须 mTLS；独立 service/Agent 不使用此代理边界 |
| Runtime Host ↔ Wasm component | 首期资格 profile `wasm-component/v1`：WASI 0.2（Preview 2）Component Model + 版本化 WIT world |
| Analysis Plugin ↔ Go tools | `masi-mcp-readonly/v1`：MCP 2025-11-25 Streamable HTTP restricted profile，只读、mTLS；不经过 Runtime Host |
| Go/外部 Agent ↔ Analysis Plugin | A2A 1.0 HTTP+JSON binding over HTTPS；首期不启用 streaming/push，不经过 Runtime Host |
| 模型/配置 manifest | 严格 JSON + schema + SHA-256 |

热路径禁止逐记录 JSON、逐记录 HTTP 和逐记录数据库事务。

### 5.5 P4Runtime 与 P4Info 兼容（CONTRACT-P4-001）

- Edge 必须在契约和部署清单中固定已验证的 P4Runtime major/minor、P4Info digest、device config digest 和 target capability；不得把“gRPC 可连接”解释为兼容；
- 首期资格 profile 固定 P4Runtime 1.4.1；每次建连或 pipeline/generation 变化后，Edge 必须通过 `GetForwardingPipelineConfig` 取得 P4Info/cookie 或目标支持的等价身份，计算并比对 canonical P4Info、device-config 和 pipeline-config digest；资格 profile 不按 `latest` 漂移（依据：[P4Runtime 1.4.1](https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html)）；
- 每次写入必须绑定正确的 `device_id`、controller role、`election_id`、应用级 switch generation、pipeline/P4Info digest 和 target capability；只有收到成功 `MasterArbitrationUpdate` 的当前 primary/master 可以写；
- P4Runtime server 对同一 `(device_id, role)` 记忆已收到的最高 `election_id`；只有仍存活且使用该最高值的 controller 才是 primary，较低值不能夺回 mastership，server 完整重启才会丢失该协议内记忆。项目因此由 Edge 控制面持久、单调分配 election ID，并用 assignment 的 allocation floor/range 防旧 actor 越界；floor/range、target assignment generation 和 application generation 都是 MASI 的额外 fence，不是 P4Runtime 原生字段。server full restart、角色变化、pipeline identity 变化或主备接管必须创建新的应用 generation，并通过 Go↔Edge 契约、journal 和 readback 显式携带；
- Edge 升级、重连和主备切换必须保持同一 `(device_id, role)` 下最多一个 primary，不得产生双 writer 窗口；失去 arbitration、无法证明 election 单调性或 pipeline identity 时立即停止 claim/write 并置 `HOLD`；
- Control 只发送逻辑 effect；Rust 根据当前已验证 P4Info 构造具体 entry，并在 write 后读取目标 entry/table state；
- logical effect 必须声明所需 `WriteRequest.atomicity` profile；目标不支持所需原子性时在写前拒绝。批量部分成功必须逐项 journal/readback 并保持同一 operation 为 `unknown/reconciling`，不得将部分成功汇总为 `applied`；
- readback 比较使用契约定义的 TableEntry canonical form，明确默认值、字段 presence、bitstring、priority、match key、action parameter 和 TTL；目标端持续变化的 counter/meter 当前值必须排除在表项逻辑相等性 digest 之外，并经独立 Rule Observation 契约采集。只比较 protobuf bytes 或依赖返回顺序均不合格；
- P4Runtime/P4Info/target capability 未知、不匹配或漂移时必须拒绝写入并置 `HOLD`；不得静默降级到旧 entry layout；
- Digest/PacketIn 属于同一主 StreamChannel 的best-effort supplemental telemetry，不是第二连接或可靠queue。`DigestListAck`只在Edge已将对应消息durable到telemetry WAL后发送，但P4Runtime规范明确Ack不提供可靠传输，target可在server/channel/client过载时丢digest，且未确认DigestList没有规范级在途上限；因此digest配置、dedupe/cache/drop/gap必须进入source profile，Ack、`max_timeout_ns=0`或`max_list_size=1`不得被解释为完整逐包覆盖。PacketIn/clone同样必须有sample、truncate、message/queue/rate和drop上限，不能作为默认全量检测feed；
- 兼容测试必须覆盖支持版本、未知 major、已知/未知 minor、role/election mismatch、full restart、P4Info/pipeline/cookie drift、不同 atomicity、部分成功、滚动升级和 canonical readback 差异。

### 5.5a BMv2 无状态防火墙契约（CONTRACT-P4-FW-001）

`contracts/p4/firewall-policy/v1` 与 `p4-stateless-firewall/v1` 必须共同固定：

- target class `software-bmv2`、`simple_switch_grpc`、v1model、P4_16/p4c/BMv2/P4Runtime exact source/artifact/image digest、P4 source/P4Info/device config、启动参数、port map、compiler flags 和 capability/atomicity/readback profile；BMv2 官方定位为开发/测试参考 software switch，任何结果不得自动提升为 hardware/production line-rate 资格；
- policy revision/binding identity、scope/target set、explicit default action、rule ID/revision、priority、normalized IPv4 prefix/protocol/port/fragment/ingress match、action/parameter、enabled/expiry、actor/reason、schema/profile/generation 和 canonical digest；未知 field/match/action/major 一律拒绝；
- `response_overlay`、`baseline_policy`、`policy_selector` 的固定 table/action/direct-counter/eligible-counter ID 与执行顺序。字段 presence、prefix/mask、wildcard、port unavailable、priority 和 default entry 必须有 canonical encoding；同义输入产生相同 bytes/digest，不同语义不得碰撞；
- logical rule 到 concrete P4 entities 的 deterministic compiled plan，包含 plan digest、每 rule expansion、active/inactive bank、selector expected/current、per-table physical units、counter units、总容量与 stable rejection reason。任意无界 range/set 展开、同 priority 冲突 overlap、unsupported IPv6/L4/fragment 或超限必须在 P4 write 前 fail closed；
- baseline activation identity 与阶段：`prepared → inactive_writing → inactive_verified → selector_switching → selector_verified → current_committed → previous_retained → cleanup`；这些是一个 effect operation 的明确阶段，不是第二 queue。只有 exact inactive entries 与 selector readback、Go CAS 都满足时 revision 才是 current；
- response expiry 由 Go/PostgreSQL durable time fact、Edge reconcile 和 exact delete/readback 保证；P4Runtime idle-timeout notification 允许丢失，只能作为提示。baseline rules 没有 incident TTL，但 policy activation 属 R3 独立 change API 并继续产生唯一 `effect_intent`；
- direct counter/eligible counter、observation epoch 与 packet oracle 映射；counter hit 不能替代 install readback 或 outcome，bank cutover/reset/gap/no eligible traffic 不能跨 epoch 合并；
- Go/Rust/P4/testkit/TypeScript 对 normalized policy、canonical compiled plan、priority/overlap、capacity、entry bytes/digest、状态/reason code 的 golden vectors。P4Runtime server 可规范化或重排返回，因此 logical equality 不得依赖 protobuf byte equality或响应顺序。

Linux UFW/nftables/iptables、Mininet namespace ACL、`tc netem`/qdisc、P4Runtime Shell 和官方教程的 Bloom-filter stateful firewall 均不实现本契约：前四者不是生产 P4 数据面 writer/path；Bloom filter 存在 collision false positive，不能成为 exact allow/drop 策略。`p4-constraints` 可以作为 CI/编译与测试期 defense-in-depth，但其 CLI 不进入生产授权或执行链，项目 contract/Go/Edge 校验仍是权威。

### 5.5b 受管 Target 契约（CONTRACT-TARGET-001）

`contracts/target/v1` 必须把设备事实、P4Runtime 会话事实和瞬时健康分开，至少固定：

- identity：系统生成、永不复用的 `target_id`；可修改 display name/alias、site/zone/tenant/scope 与 immutable creation provenance。P4Runtime `device_id`、endpoint、hostname、serial/chassis/port 信息均是属性或外部引用，不是 canonical identity；同一 active `(p4_endpoint_ref,device_id,role)` 不得绑定两个 target；
- desired configuration：target class/vendor/model、P4Runtime profile、expected P4 program/P4Info/pipeline/capability digest、允许的 Edge placement、resource profile、credential reference 和可选 read-only gNMI profile/path set；secret、raw credential、mutable tag 和任意 CLI 不进入 contract；
- lifecycle：`registered|verified|active|draining|disabled|quarantined|retired`，状态转换、actor/scope、reason、revision 与审计引用明确；retired identity 不得重新分配。qualification `HOLD`、effect `unknown` 和瞬时 `reachable/unreachable/stale/drift/not_observed` 使用不同 namespace，不能混为生命周期；
- fence：不可复用的 `target_control_incarnation_id`、单调 `target_registry_revision`、per-target `target_assignment_generation`、Edge instance/TargetActor runtime epoch，以及独立的 P4Runtime `device_id/role/election_id/application generation/pipeline digest`。无损 PostgreSQL failover保留 control incarnation；PITR/restore/clone/rewind 在重新开放 assignment/effect claim 前轮换新 incarnation并逐 target read-only 重验；
- assignment：一个 target 同时至多一个 active Edge assignment；assignment 必须绑定 Edge workload identity、placement/failure domain、generation、不可复用 lease identity、Edge 本地 monotonic expiry、desired profile、accepted capability digest 与 durable P4 election allocation floor/range。Edge 在每次 claim/write 前验证 assignment 尚 current 且 lease 未过期；revoke/expiry 后旧 actor 只能 read-only reconcile，不能再生成可写 election 或发送 Write。新 assignment 的 election floor 必须严格大于所有旧 assignment 可能合法发出的 election ID，旧 generation/actor epoch 的 observation、claim、preflight、readback 和 late result不得推进 current；
- observation：Edge 上报 arbitration、P4Info/pipeline/capability、actor/target runtime epoch、queue/backlog、last successful read、freshness、drift 和稳定 reason；TCP/gRPC 连接、process healthy、gNMI 可读或 CMDB 条目存在均不能单独证明 P4 mutation readiness；
- external candidate：NetBox/CMDB/文件/API 等输入只以 `candidate_source/revision/digest/retrieved_at` 进入有界 staging；Go 计算 create/update/disable diff，scoped Platform Admin 显式确认 exact digest 后才写 canonical registry。同 key+同 digest 幂等，同 key+不同 digest 冲突；外部删除/漂移不自动 retire target。

### 5.5c Fleet Effect 聚合契约（CONTRACT-FLEET-EFFECT-001）

`contracts/fleet-operation/v1` 只编排现有 target-scoped effect，不增加新的执行队列：

- parent identity 至少绑定 `target_control_incarnation_id/fleet_operation_id/kind/proposal/decision/authorization/effect-or-policy digest/target-set snapshot+digest/ordered waves/failure policy/max parallel/deadline/idempotency key/trace ID`；target set 和 wave membership 一经授权不可动态改变，membership/drift 只能阻止或产生新 operation；
- approve 的同一短 PostgreSQL 事务必须 append exact Decision、创建不可 claim 的 parent fact，并为冻结集合创建唯一 per-target `effect_intent`。每个 child 使用 `(fleet_operation_id,target_id,effect_digest)` 幂等，并绑定 target assignment/application generation、P4Info/profile、wave gate 和 expected current；dispatcher 仍只扫描/claim `effect_intents`，future-wave intent 在 durable gate 开启前不可 claim；
- 每个 child 独立执行 `claim/fence → Edge target actor journal/P4 write/readback → PostgreSQL CAS finalize`。P4Runtime `WriteRequest.atomicity` 只能描述一个 target 的 Write；不同 target 没有分布式事务、two-phase commit 或“全部同时生效”承诺；
- parent 是由 child vector 确定性重建的投影：`planned` 表示尚无 child 尝试外部副作用，`running` 表示当前 wave 正在执行且尚无混合结果，`partial` 表示已出现 applied/failed/blocked/待执行混合但无未知副作用，`reconciling` 表示至少一个 child 为 `unknown/reconciling`，`applied` 仅在全部 required child applied，`failed` 表示不再继续且至少一个 required child 未 applied，`aborted` 只允许在所有 child 均未 `write_started` 且已阻止 claim 时成立；父级不得覆盖 child 状态或用一个百分比掩盖不确定性；
- failure policy 封闭为 `fail_fast|continue_isolated|manual_gate`。首期冻结 target set 中每个 child 都是 required；不得在执行期改成 optional。下一 wave gate 的 CAS predicate 必须同时满足：当前 wave 的完整 required child vector 已到 profile 明确允许的 known terminal set、没有 `unknown/reconciling`、parent/wave deadline 未过期、原 scope/authorization/target-set digest 未漂移、每个下一 wave child 的 assignment/application generation/P4Info/capacity/freshness 仍满足。`fail_fast` 只有当前 wave 全部 applied 才可自动继续，任一 known non-applied 使未开始 child `blocked`；`continue_isolated` 可在没有 unknown 且原授权明确允许时跳过已知失败 target 继续其他 wave，但最终 parent 不能成为 applied；`manual_gate` 即使全为 known 也不自动开启，继续/停止 Decision 必须绑定 exact completed-vector digest。deadline 到期只把未尝试 child置 `blocked`，不能把已尝试结果改成 failed；
- static canary 是 ordered waves 的第一个小型 target cohort，不进行 weighted traffic split。rollback 必须创建新的 parent/Decision（或在原授权仍有效且 profile允许的短 grace内引用它）和新的 per-target intents，逐 target选择 exact previous；某 target无兼容 previous时保持其已确认 current/HOLD，不得用其他 target状态猜测；
- 每个 API/UI/evidence 必须返回 ordered target vector、per-wave progress、每 target current/desired/application generation/assignment/intent/attempt/readback/result/reason/freshness，并区分 `not_started|blocked|failed|unknown|reconciling|applied`；未尝试不得编码为 effect `unknown`，父级 `applied` 不得仅由计数、健康或大多数成功推定。

### 5.6 规则观测契约（CONTRACT-RULE-001）

`contracts/p4/rule-observation/v1` 必须定义规则身份、累积计数样本、窗口增量、观测质量和可选结果证明，且与可执行 effect 状态分开。至少满足：

- 稳定规则身份绑定 `effect_intent/operation/entity`、logical rule digest、canonical `TableEntry` digest、device/role、application generation、pipeline/P4Info/capability digest、table/direct-counter ID、match/priority/action digest、install/readback time、TTL/expiry 和 observation profile；rule revision、generation 或 pipeline 变化必须开启新的 observation epoch，禁止跨 epoch 拼接；
- 安装证据只表示 Write 后 exact canonical readback 成功；命中证据只表示在目标 P4 profile 中，绑定该 entry 且被对应 action 执行的 direct counter 在有效窗口内产生正增量。P4Runtime Read 应优先显式请求 exact `DirectCounterEntry`；若通过 `TableEntry` 读取，则请求中必须设置 `counter_data` presence，否则响应不含该计数。counter 不存在、action 未执行 counter 或 target 未资格化时为 `not_measurable`，不得补零；
- 每个 cumulative sample 至少携带 packet/byte count、counter width/mode（wrap 或 saturate）、sample sequence、read start/end、target data time 能力、baseline/current digest、reset epoch、gap/duplicate/order、quality status 和 trace ID。P4Runtime 分批、重排或重复返回必须按 canonical entity identity 去重排序，不能把 RPC 返回顺序当 identity 或全局快照；
- 规则 direct counter 默认只读，不由观测轮询清零；delta 仅在同一 identity/generation/reset epoch 的两个有效累计样本间计算。回退、wrap、saturation、reset、重启、样本间隙、过期或时钟异常必须显式标为 `reset_or_wrap/ambiguous/stale/invalid`，不能产生负值、巨量 delta 或虚假 0%；
- `first_observed_hit_window` / `last_observed_hit_window` 只能给出两次采样之间的观测区间；除非 target 的已资格化能力提供可验证时间戳，不得把轮询时刻显示为精确首次/最后命中时刻。idle-timeout 的 best-effort `time_since_last_hit` 与 counter delta 是不同证据，不能互换；
- 结果证明必须引用独立 packet oracle 或 action-specific telemetry 的身份、窗口、预期/实际结果和 digest；没有独立 oracle 时 `outcome_status=not_measurable`。counter 增长不能证明包已按预期 drop/forward/mirror，也不能证明攻击、Incident 或业务影响已消失；
- unknown observation version、identity drift、oversize、重复冲突或质量不足必须拒绝合并并显示 `unavailable/stale/invalid/not_measurable`；`unknown` 仍只用于已尝试外部副作用但结果未确认的 effect，不得复用为规则统计缺失。

### 5.7 Effect 治理契约（CONTRACT-EFFECT-001）

`effect/v1` 至少定义 `EffectProposal`、`AuthorizationDecision`、`EffectIntent`、`EffectResult` 和稳定错误码。其公共不变量如下：

- Proposal 是不可变、不可执行的完整候选，至少绑定 proposal/revision ID、canonical digest、creator、logical target 或冻结 target-set/wave digest、switch/generation/session vector、exact logical diff、evidence/P4Info/schema/target-state digest、risk class、policy/governance profile、TTL、proposal expiry、rollback/precondition 和 trace ID；
- Decision 是 append-only 事实，至少绑定 proposal ID/digest、`approve/reject`、actor 的稳定 `(iss, sub)` 引用、有效 scope、role-mapping version/digest、认证上下文、reason code、受限说明、decision time 和 authorization expiry；
- Intent 必须且只能绑定一种授权来源：产生它的有效 Decision，或允许自动处置的已资格化 policy/profile version；普通 operation 每 target 一个 intent，fleet Decision 可以在同一短事务中产生 bounded、冻结且逐 target 唯一的 child intent vector，但 parent/target mapping 永远不可 claim；
- canonical digest 的字段顺序、编码、大小写、IP/prefix、时间和空值规则必须由契约固定，并有跨 Go/TypeScript golden vector；
- 修改 target、entry、TTL、evidence、generation、P4Info、policy/profile 或任何授权相关字段必须创建新 proposal/revision 和新 digest；旧 decision 不得沿用；
- 同一 idempotency key + 同一 digest 返回同一 canonical fact；同一 key + 不同 digest 返回冲突；
- Proposal/Decision 不是 Edge RPC、dispatcher job 或 P4 queue；只有已持久化并通过当前事实重验的 Intent 可以被 claim。
- fleet child idempotency、wave gate 与 parent aggregate 遵守 `CONTRACT-FLEET-EFFECT-001`；同一 child identity+digest 幂等，不同 digest 冲突。任何 target-set/wave/application generation/P4Info 漂移都不得复用旧 Decision 追加未授权 child。

### 5.8 Agent 协议兼容（CONTRACT-AGENT-001）

- A2A 固定协议版本 `1.0`（规范 patch `1.0.x` 不参与线上协商）和 HTTP+JSON binding；Agent Card 只能声明已测试的 interface/binding/version；
- 每个 A2A 请求必须显式选择 `1.0`；MASI-NIDS client 固定发送 `A2A-Version: 1.0`，server 也可以接受规范允许的同值 query parameter。header/query 同时存在但不一致时拒绝；缺失/空版本按规范识别为旧 0.3 后返回 `VersionNotSupportedError`，禁止自动回退、双解析 0.x payload 或按 `latest` 漂移（依据：[A2A 1.0 versioning](https://a2a-protocol.org/latest/specification/#36-versioning)）；
- MCP 使用私有、版本化的 `masi-mcp-readonly/v1` restricted profile：initialization 必须协商 `2025-11-25`，后续每个 Streamable HTTP 请求必须携带 `MCP-Protocol-Version: 2025-11-25`，或由同一经认证 session 中已持久的协商结果唯一识别版本；非法、不支持或既无 header 又无法由 session 唯一识别时返回 HTTP 400，禁止隐式执行 `2025-03-26` payload 或 legacy HTTP+SSE endpoint；
- 上述缺 header 时 fail-closed 是 MASI-NIDS 私有 endpoint 的有意收紧，不宣称是通用向后兼容 MCP server。官方 MCP 对无法识别版本的缺 header 请求建议假定 `2025-03-26`，因此任何面向通用 MCP client 的 endpoint 必须另建 profile 和兼容矩阵，不得复用本 restricted endpoint（依据：[MCP 2025-11-25 transport/version header](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)）；
- MCP client 对 POST 响应必须支持 `application/json` 与 `text/event-stream`；首期 Go read-only MCP server 可以只返回 bounded JSON，并可以禁用独立 GET/SSE notification，但不得谎报 capability；
- `masi-mcp-readonly/v1` 只允许 manifest 与 binding 双重 allowlist 中的 tools/resources；禁止 prompts、sampling、elicitation、server-to-client mutation、任意 notification 和未声明 URI。每个 binding 必须固定 tool/resource、scope、calls/rounds、request/result bytes、total deadline、认证 identity、egress 和审计字段；其中 Analysis graph 保持最多 6 次工具调用、2 轮 MCP 和 30 秒 graph hard deadline，其他 binding 不得继承无界或隐式默认值；
- MCP Streamable HTTP server 必须验证存在的 `Origin`、执行 mTLS/应用认证和 body/framing limit；本地监听不得默认暴露到 `0.0.0.0`；
- A2A Task/Message/Artifact 与 MCP request/result 必须携带自身协议版本、schema version、稳定 identity、大小、deadline 和 digest；协议 task/session 不是核心事实或 effect identity；
- SDK 版本必须锁定并记录制品 digest；升级 SDK、规范 patch 或 binding 均须重跑 wire golden、unknown-version、错误映射、oversize、断连/恢复和 old/new compatibility matrix。

### 5.9 插件 Manifest 与 Kind 契约（CONTRACT-PLUGIN-001）

`plugin/manifest/v1` 必须使用严格 schema，并至少绑定：

- 稳定且不得复用的 `plugin_id`、封闭 `kind`、`manifest_version`、`plugin_version` 和发布者；
- `runtime_profile`、OCI manifest/index digest 或 Wasm component digest、entrypoint 与支持平台；
- `host_api_version`、kind input/output contract versions、config schema ID/version/digest；
- capability、skill、MCP tool/resource、A2A peer、网络出口、文件 preopen、secret reference 的声明；
- CPU、memory、linear memory、table/instance、PID、FD、ephemeral storage、batch、in-flight bytes、队列、并发、deadline、retry 和输出上限；
- SBOM、provenance、签名/验证 bundle 引用、owner/support level、兼容矩阵、migration/rollback metadata；
- startup/readiness/liveness、drain、failure/fallback 和 observability contract。

Manifest 声明只是权限申请，实际授予权限必须是 manifest、部署 policy 和 runtime capability 三者交集。未知 manifest/kind/host API major、未验证 minor、未知字段、缺失 digest、配置漂移、预算越界或 capability 扩张必须拒绝 admission/readiness，禁止按默认 kind、旧版本或宽权限运行。

### 5.10 插件 Runtime 与生命周期契约（CONTRACT-PLUGIN-002）

- `plugin/v1` 必须定义 `ValidateManifest`、`ValidateConfig`、`Handshake`、`Prepare`、`Execute/ExecuteBatch`、`Health`、`Drain`、`Disable` 和稳定错误码；kind 可以裁剪方法，但不得谎报 capability；
- service plugin 使用 Protobuf/gRPC，所有 RPC 必须引用机器可读 `grpc-service/v1` method profile，固定 deadline、请求/响应大小、in-flight、retryable status、最大 attempts、指数退避和 retry throttling；总 deadline 包含排队、连接与退避。默认禁止 hedging；非幂等、健康检查和 effect/设备副作用 RPC 必须禁用应用层及 channel transparent retry，模糊结果只能按原 operation reconcile。即使没有显式 retry policy，gRPC 仍可能透明重试，因此只写“无 retry policy”不构成禁用（依据：[gRPC retry](https://grpc.io/docs/guides/retry/) 与 [deadline](https://grpc.io/docs/guides/deadlines/)）；
- `wasm-component/v1` 首期资格 profile 固定 WASI 0.2（Preview 2）与 `masi:plugin-transform@1.0.0` 的 `pure-transform` WIT world；首期只允许同步、确定性、纯计算调用，宿主不得链接未声明的 filesystem/network/clock/random/process capability。WASI 0.3 已稳定，但在单独的工具链、Canonical ABI、性能和 old/new compatibility matrix 通过前必须拒绝 admission，不得因 runtime 支持而自动升级（依据：[WASI releases](https://wasi.dev/releases) 与 [WASI roadmap](https://wasi.dev/roadmap)）；
- Wasm CPU 预算以 fuel 为确定性主限制，并以 epoch/wall-clock total deadline 作为宿主外层保险；fuel exhaustion、epoch interruption、deadline 和 host cancellation 必须映射为不同稳定错误。不得引入不能被 deadline/cancellation 终止的 blocking host import（依据：[Wasmtime interruption](https://docs.wasmtime.dev/examples-interrupting-wasm.html)）；
- A2A Agent plugin 继续遵守 `CONTRACT-AGENT-001`，MCP tool plugin 继续遵守 MCP 版本和工具合同；MCP/A2A task 不替代 plugin activation、核心事务或 effect identity；
- activation 必须绑定 `plugin_id + kind + scope + artifact digest + config digest + contract set + generation/epoch`；旧 generation 的 late result 必须被 fence；
- version compatibility 必须分别覆盖 manifest、host API、kind contract、config schema、WIT world 和持久化 plugin fact；仅镜像能启动或握手成功不构成兼容。

### 5.10a 插件统计结果与声明式展示契约（CONTRACT-PLUGIN-STAT-001）

`contracts/plugin/statistics/v1` 是插件统计计算、Go 投影和 Web 展示的唯一跨语言源。统计是现有 kind 的可选输出 capability，不是新 plugin kind；未声明该 capability 的插件不能返回或注册统计定义。

- `PluginStatisticsDefinitionV1` 是随 immutable plugin revision/capability 签入 manifest 并进入 qualification 的统计项描述符，至少绑定稳定且不得在同 revision 内复用的 `definition_id/revision/digest`、纯文本名称/说明、producer kind、host-owned input projection/field set 与 data class、允许的 scope/window/trigger、output/display profile、freshness、请求的 deadline/resource 和兼容范围；`read-only-tool` 还可引用 manifest/binding 中已批准的 external-source capability ID，但不能内嵌 endpoint/URL/credential。definition 只能引用项目 registry 中的 typed projection/field 或 capability ID；禁止携带 SQL、PromQL、JSONPath、MCP prompt、任意表达式/代码、URL 或自行发现数据源。manifest 请求值仍受 binding policy 和系统 profile 收紧，不能自行授予 schedule、网络、数据或展示权限；
- `StatisticsInputBundleV1` 至少包含 schema/run/request identity、exact `plugin_id/revision/config digest/binding generation`、definition ID/revision/digest、scope 与 data-class reference、半开时间窗 `[start,end)`、`as_of`、source fact/profile/generation/epoch/sequence/coverage/quality references、input digest、budget/deadline 和 trace ID。输入只能由 Go 从 definition 指向且当前授权的 canonical facts/projections 冻结；插件不得接收核心数据库连接、可变对象、raw packet、secret 或未授权全量 Event；
- `PluginStatisticsArtifactV1` 至少包含 artifact/result identity、exact producer identity、definition ID/revision/digest、scope/window、input/result digest、produced/observed/valid/expires time、独立 run status 与 Artifact quality、metrics/series/tables/display hints、provenance、limitations、coverage、truncation reason/count 和 trace reference；插件声明的 ACL、freshness 或“verified”文本不覆盖 Go 校验；
- `read-only-tool` 的 external read 只能使用 definition 引用且 binding 已批准的 capability。Artifact provenance 必须额外给出 capability ID、canonical request digest、external observed time、response content digest 与可用的 ETag/version、partial/timeout 状态；不得回传 endpoint、credential 或无界 raw response。同一 run 重试得到不同 external input/response digest 时必须稳定冲突，不能选择“较新”结果覆盖 current；需要新数据时创建新 run identity；
- run status 封闭为 `queued|running|succeeded|failed|cancelled|expired|fenced`；Artifact quality 封闭为 `valid|partial|gap|stale|no_data|not_measurable|invalid`；point quality 封闭为 `valid|missing|gap|reset|late|invalid`。missing/no data/gap/reset/not measurable 不得补 0，不能借用 effect `unknown`；
- metric kind 封闭为 `gauge|sum|histogram`；只有 `sum` 可以声明 monotonic，temporality 封闭为 `delta|cumulative`。每条 series identity 绑定 metric ID、canonical dimensions、unit、temporality、producer generation 和适用 reset epoch；不同 generation/reset/window 不得盲目聚合。数值只允许有限值或显式 null+quality，NaN/Inf、重复时间点不同值、未声明乱序、unknown unit/major 稳定拒绝；
- table column type 封闭为 `string|int|number|bool|timestamp`；未知 object、HTML、URL、脚本、可执行内容和任意深层结构拒绝。dimensions 必须排序、规范化并受基数/长度上限约束；
- display kind 封闭为 `metric-card|status|timeseries|bar|heatmap|table|text|evidence-list`。hint 只能引用 Artifact 内已声明字段 ID，使用 host-defined semantic token、有限排序/映射和纯文本标题；插件不得提交 Vue/React component、route/nav、template、HTML/SVG/CSS/class、JavaScript、iframe/URL/MIME、完整 `EChartsOption`、formatter/callback/`renderItem`/event/custom series、Vega/Vega-Lite spec/表达式或远程数据源；
- Web 内置 registry 将合法 display kind 映射到项目自有 Vue 组件，并在内部生成 ECharts `dataset/encode`。未知 kind/major/field reference 必须拒绝或显示受控 `unsupported`，不得使用 raw JSON/HTML fallback。每张图必须显示单位、时间窗、新鲜度、quality/coverage/truncation 并有等价表格/摘要；
- 统计 run 幂等键至少绑定 `plugin_id/plugin_revision/config_digest/binding_generation/definition_id/definition_revision/definition_digest/scope_digest/input_digest/window/trigger-or-schedule_revision`。同键同 result digest 返回原结果，同键不同 digest 冲突；old-definition/old-generation late result 只追加审计，不覆盖 current；
- 首期 profile 固定每 plugin revision 的 statistics definitions ≤32、definition descriptor block ≤128 KiB、每 definition input field refs ≤64、external-source capability refs ≤1、definition 全部人类文本 ≤64 KiB；input ≤2 MiB、Artifact ≤1 MiB、Artifact metric definitions ≤32、series ≤64、总 numeric points ≤10,000、单 view ≤2,000、每个 histogram point 的 bucket ≤64 且 bucket count 计入 numeric points；tables ≤8、每表 columns ≤32、全部 tables 总 rows ≤2,000、display hints ≤16、evidence refs ≤200、JSON nesting depth ≤8、任一 definition/Artifact text/string scalar ≤4 KiB、Artifact 全部文本 UTF-8 bytes 合计 ≤64 KiB、每 series dimension keys ≤8、key/value 分别 ≤64/128 UTF-8 bytes。每 binding in-flight ≤2、待运行 queue ≤32、单 run total deadline ≤10 秒、API 单页 ≤200、run/artifact 默认保留 30 天；任何放宽必须新 profile 和完整资格证据；
- 插件统计不得自动导出为 Prometheus business series。Prometheus 只允许低基数 run/queue/latency/result/quality 聚合，plugin/result/artifact/rule/IP/五元组/任意 dimension value 不得成为 label。JSON 为首选导出；CSV 由 Go 有界生成并防公式注入。

### 5.11 契约与运行 Profile Registry（CONTRACT-PROFILE-001）

- `contracts/profiles/v1` 必须是所有 wire/runtime profile 的唯一机器可读 registry；至少记录 profile ID/version、上游规范版本、schema/WIT identity、生成器与验证器版本、source/generated digest、method/resource limits、兼容矩阵和停止支持日期；
- 首期 REST source profile 固定 OpenAPI 3.1.2，JSON Schema dialect 固定 2020-12；MCP schema 在未声明 `$schema` 时也按 2020-12 验证。OpenAPI 3.2 或其他 JSON Schema dialect 只有在 Go/TypeScript/Python/Rust 生成、校验和 old/new golden 全部通过后才能新增 profile，不能把 `latest` 当版本（依据：[OpenAPI published versions](https://spec.openapis.org/oas/) 与 [JSON Schema specification](https://json-schema.org/specification)）；
- Protobuf/gRPC、OpenAPI、JSON Schema 和 WIT 分别拥有自己的 source；跨格式只通过有测试的显式 adapter 映射，不生成循环 source，也不把 MCP/A2A payload、WIT world 或 OpenAPI Schema 合并为万能插件 ABI；
- WIT package ID、world、imports/exports、Canonical ABI/toolchain、Wasmtime release channel 与 compilation-cache key 必须在 profile 中完整绑定。Wasmtime 普通 major 生命周期短，生产必须选择受支持的固定 release/LTS policy 并通过升级矩阵，不得长期写死未维护 major（依据：[Wasmtime release process](https://docs.wasmtime.dev/stability-release.html)）；
- 未知 profile major、profile digest 漂移、生成器/验证器不在资格集合、source 与 generated digest 不一致时必须拒绝 build/admission/readiness；仅 schema lint 成功不构成跨语言兼容。
- 首期必须登记 `qualification-evidence/v1`。wire 证据必须分列：`level=REHEARSAL|MODULE|PAIRWISE|SYSTEM_E2E|PRODUCTION`、`applicability=APPLICABLE|NOT_APPLICABLE`、`result=PASS|FAIL|HOLD|NOT_RUN`、`qualification=QUALIFIED|NOT_QUALIFIED`；另绑定 runtime/availability/deployment-tier/topology/release/environment/profile/artifact digest 的 claim scope。`NOT_APPLICABLE` 不能塞入 result，`HOLD/NOT_RUN` 不能塞入 level，展示层可以推导 `REHEARSAL/NOT QUALIFIED` 或 `MODULE PASS`，但不得持久化成第五种混合状态。
- 首期必须登记 `deployment-tier/v1`，封闭值为 `development|acceptance|operational-single-domain|production-ha`。tier 与 qualification level 正交：前三项都可以形成 Module/Pairwise/System E2E 证据，但不得产生 `level=PRODUCTION` 或 production-qualified claim；`operational-single-domain` 明确表示完整、可运维但只接受一个故障域的部署，不宣称 HA。只有 `production-ha` 有资格进入 production 门禁，并且必须同时选择并通过 `availability-ha/v1`、生产绝对性能/容量、PostgreSQL HA/PITR 和其余生产门禁；选择该 tier 本身不自动产生资格。任何 production claim 必须精确绑定 `(runtime_profile, availability_profile, deployment_tier, topology, release/artifact digests)`。
- 首期本地/CI 正式 E2E runner 固定为 `e2e-runner-compose/v1`，绑定 exact Docker Compose/Engine/API、runner image、project/network/volume命名、health dependency、startup/total deadline、资源预算、日志/evidence/清理和失败语义。Compose 的 `running`/`service_healthy` 只表示依赖就绪；业务 PASS 仍由场景 oracle 决定。Playwright 只作为真实浏览器 driver，不拥有多服务生命周期。多主机/生产 runner 必须由对应 deployment profile 精确指定并独立资格化，不得运行时自动换 runner或继承本地证据。
- 首期必须登记 `web-spa/v1`、`web-browser/v1` 和 `web-performance/v1`，固定 Vue/Vite/TypeScript 及关键依赖版本、OpenAPI client 生成器、受支持浏览器引擎、build target/polyfill policy、bundle/请求/DOM/chart/cache/SSE 预算、目标设备与网络、Core Web Vitals 门槛和证据 digest；Vite 默认 browser target 或浏览器“当前版本”不得成为隐式生产合同。
- 首期必须登记 `plugin-statistics/v1` 与 `plugin-statistics-display/v1`，固定 `CONTRACT-PLUGIN-STAT-001` 的 definition/input/artifact schema 与 canonicalization、host-owned projection/field 与 approved external-source capability registry、producer kind capability、run/quality/metric/temporality/unit/dimension/table/display union、definitions/输入输出/points/rows/text/queue/concurrency/deadline/retention 上限、Go/Host/plugin/Web compatibility 和 golden digest；未知 projection/field/external-source capability/display/metric/profile major 或 SQL/PromQL/JSONPath/表达式/完整 option/code payload 必须拒绝，不能因某个数据库、ECharts/Vega 或浏览器能解析而自动兼容。
- 首期必须登记 `p4-rule-observation/v1`，固定 target/P4Info/direct-counter 能力、计数宽度与 wrap/saturate/reset 语义、entry/eligible-traffic 观测点、轮询/batch/deadline/response/retention 上限、状态枚举、公式和 packet-oracle profile；未登记能力只能显示 `not_measurable`，不得用通用“命中率”兜底。
- 首期必须登记 `p4-stateless-firewall/v1`，固定 `simple_switch_grpc`/v1model/p4c/BMv2 artifact、P4 source/P4Info/device config、response/baseline/selector table/action/counter、IPv4/match/fragment/default/priority/overlap、logical-to-physical expansion、active/inactive capacity、bank cutover/rollback、direct-counter/oracle 和 stable reason code；IPv6、port range、其他 target 或硬件能力未登记时只能 `unsupported/HOLD`，不能静默改写或退化到主机防火墙。
- 首期必须登记 `p4-target-fleet/v1`，固定 `target_id/device_id/role/endpoint` 映射、target-control incarnation、registry/assignment/application generation、Edge actor/queue/resource、公平调度、最大 target/operation/wave/parallel child、BMv2 topology/port map、静态 canary/failure policy、parent/child 状态与绝对性能/恢复门槛。首期至少覆盖 1/2/N 个 exact BMv2 target；N 与所有资源数值未按 `DEC-001` 冻结前只能 `HOLD/NOT RUN`，不得从单 target benchmark 外推。
- `target-gnmi-readonly/v1` 是条件 profile，不是首期多 BMv2 target PASS 的依赖；触发后必须锁定 OpenConfig gNMI proto/spec/model/path、target implementation、TLS identity、`Capabilities/Get/Subscribe` allowlist、encoding、request/response/update bytes、subscription/rate/queue/deadline/freshness/gap 与 schema digest，并在身份/路径/版本/资源漂移时 fail closed。首期 production identity/API/部署策略必须拒绝 `Set`；gNOI 和其他 device mutation 需要新 profile、ADR 与需求基线。
- 首期必须登记 `p4-traffic-replay/v1`，固定 fixture schema、runner/tool exact digest、P4/BMv2/Mininet/kernel/iproute2/offload/MTU/namespace/cgroup profile、允许的 input link type、direction/interface map、rewrite、timing/rate/loop/impairment、packet/byte/duration/resource 上限、ground-truth 与 packet-oracle 语义；软件 target 与硬件 target 使用不同 profile/evidence，未登记 backend、mode 或 target class 必须拒绝，不能用工具默认值形成隐式测试合同。
- 首期必须登记 `telemetry-p4-window/v1` 与 `inference-central-grpc-batch/v1`。前者固定target/P4 program/P4Info、counter/register/digest IDs和bit width、observation domain/point、target/flow/window上限、bank/epoch/snapshot/clear、Read batch/deadline、source sequence/runtime epoch、event-time/watermark/lateness、sampling/coverage/quality/drop与feature adapter；后者固定Protobuf/gRPC/TLS版本、message/record/tensor identity、dtype/shape/order/bytes、request/input/result digest、logical pool/pool generation/worker attempt、batch records/bytes、Edge coalescing规则、Triton dynamic-batch delay、deadline/in-flight、quota、retry/dedupe/fence、channel reuse、load-balancing/service-discovery、backpressure与old/new matrix。任一资源数值或绝对性能门槛未冻结时对应模块保持`HOLD/NOT RUN`；
- supplemental `telemetry-p4-digest-sample/v1`/`telemetry-p4-packetin-sample/v1`只可声明best-effort hint/sample，不得声明reliable/full coverage。`telemetry-mirror-packet-mmap/v1`、`telemetry-mirror-af-xdp/v1`、`telemetry-mirror-dpdk/v1`和Gateway↔Triton shared-memory extension均为条件profile：只有feature/target/capacity/copy证据触发后才成为该部署门禁。AF_XDP必须固定NIC/driver/kernel/XDP program、XDP_DRV或XDP_SKB、zero-copy或copy、fallback policy、RSS queue/CPU/NUMA、UMEM/ring/need-wakeup/multi-buffer；生产不得让默认自动fallback改变已资格性能语义；
- 首期必须登记 `model-runtime-central-cpu/v1`、`model-runtime-central-cuda/v1`、`availability-single/v1`、`availability-ha/v1` 与 `model-rollout-pool-generation/v1`。两个runtime profile共享Gateway/Triton/ONNX、opset、operator/custom-op policy、input/output、wire digest、Triton model-control `none`/strict readiness/auto-complete disabled、闭包化repository snapshot、显式dynamic batch/instance group/queue、deadline和numeric合同；CPU profile另固定CPU EP、architecture/feature、thread/affinity/NUMA/arena/RAM，CUDA profile另固定CUDA EP、CUDA/cuDNN/GPU/driver/VRAM、I/O Binding/device/copy/stream。ORT 可按 provider 优先级把不受 CUDA EP 支持的节点放到 CPU；项目只允许在 CUDA profile 中使用 qualification 时已声明、冻结并测量的 host-side operator placement，运行期新出现的 provider partition、unsupported operator 或 device drift 必须失败关闭，不能冒充 CPU profile fallback。管理员在startup envelope中显式二选一，preflight必须读回actual provider、partition与hardware；缺失或不符即startup fail closed，不能自动改选。availability profile与runtime profile正交：`availability-single/v1`固定恰好一个failure domain，但允许在该域内按容量部署1..N个同profile副本；它必须固定`required_replicas/min_ready_replicas/max_unavailable`与中断语义，域内多副本只提供容量/有限重试而不构成failure-domain HA。只有`availability-ha/v1`才固定至少两个failure domain、min-ready/static N+1及故障后剩余容量。rollout profile固定model-control incarnation、logical pool/pool generation、per-shard route owner/epoch、startup envelope、WAL-backed drain/buffer/gap/resume、deployment action、所选availability profile要求的资源与门槛、readback、CAS/commit handshake、mixed rollout、rollback和故障语义。TensorRT只有作为独立显式资格化的新generation才可条件采用；LibTorch与其他未登记backend没有首期profile，任何backend/profile均不得按运行时可用性fallback。
- `model-rollout-pool-generation/v1` 是首期唯一生产切换 profile：同一 Triton instance只启动加载一个 exact binding，且无运行期 load/unload/repository polling/candidate/online shadow；新 revision/backend通过隔离的新 pool generation预热并按 shard切换。Edge仍是 shard唯一 canonical router，Kubernetes Service/KServe/serving router不得成为第二 route owner。旧 generation只在有界drain/rollback grace内保留，不允许长期双写 canonical result。
- 每个可用于模型 PASS 的 `performance-environment/v1`、所选`model-runtime-central-cpu/v1|model-runtime-central-cuda/v1`、`availability-single/v1|availability-ha/v1`、`deployment-tier/v1`与`model-rollout-pool-generation/v1`组合必须冻结exact CPU/feature/thread/NUMA/RAM，或GPU/driver/CUDA/cuDNN/VRAM，连同network/cgroup、模型/optimized artifact/合同digest、batch/workload，以及该部署声明的最低steady/peak/rolling qualified capacity、min-ready/headroom/max-unavailable、最大p99、CPU/RSS/VRAM、cold/warm/cache-hit/cache-miss各startup stage、dynamic batch queue、Edge↔Gateway RTT/bytes/retry、Gateway↔Triton与适用的host↔device copy、per-shard drain/unavailable/buffer/gap/replay、readback/CAS/commit/resume、termination、restart/quarantine、全组mixed-rollout和rollback deadline、soak门槛；选择`availability-ha/v1`时还必须冻结failure domains/static N+1及最大故障单元丢失后的剩余容量，选择`availability-single/v1`时必须明确中断语义且不得记录HA PASS。所有容量计算同时计入active、warming、draining和replay generation/实例/资源。CPU与CUDA各自产生独立、profile-scoped证据，不能互相继承；某一profile PASS足以资格化只声明该profile的精确部署，但产品若声明“CPU与CUDA均受支持”，两套要求场景都必须PASS。任一适用字段仍为TBD、只写“按环境”或缺少原始结果时，`PERF-INF-001`只能为`result=HOLD|NOT_RUN, qualification=NOT_QUALIFIED`。

### 5.12 在线模型包与绑定契约（CONTRACT-MODEL-001）

`contracts/model/v1` 是在线检测模型可替换性的唯一语义源，至少固定：

- immutable identity：`model_id/revision/bundle_digest/manifest_digest/artifact_uri/size`，以及 model/scaler/config/feature-schema/label-taxonomy/output-adapter/inference-wire/runtime/resource/qualification digest；若使用 preoptimized artifact，还必须保存 raw-model、optimized-model、optimization-tool/options、ORT/EP/device/CPU-feature digest。tag、alias、路径、mtime 和 display name 只能用于发现，解析后不得持久化为 binding identity；
- feature schema：字段稳定 ID/顺序、dtype/shape、单位/尺度、窗口、缺失/default、normalization/scaler、NaN/Inf/OOD 和 producer compatibility；同 major 内只允许经 old/new matrix 证明的兼容扩展；
- label taxonomy：稳定且永不复用的 label ID、父子/别名/弃用映射、single-label/multi-label/anomaly-score/open-set mode、unknown/OOD/abstain、score domain、threshold/calibration 和 class order。增加 label 只有在旧 reader 能保留 unknown label 且不触发旧 policy 时才可判为兼容 minor；重用 ID、改变含义/score domain 或把 single-label 改为 multi-label 必须升 major；
- output adapter：稳定ID/version/digest，把raw tensor按明确class order/axis/top-k/threshold/calibration映射为canonical`Prediction[]/decision/quality`；adapter必须是Gateway/backend内已资格化的确定性实现或受限数据配置，不能由bundle注入可执行代码；
- `InferenceResult`：input/window identity、`model_control_incarnation_id`、`inference_shard`、`shard_routing_epoch`、logical pool/pool generation、worker runtime/attempt ID、model/bundle revision、binding generation、feature/label/adapter/wire/runtime/profile digest、scores/predictions、abstain/OOD/quality、input/output digest、started/completed time和trace ID。late、未提交、未知或跨incarnation/shard/route/pool/binding generation结果可区分且不能写canonical Event；
- binding identity：`model_control_incarnation_id + scope + inference_shard + model revision + all contract/config/profile digests + binding_generation`。incarnation 使用不可复用的强随机 identity；无损 PostgreSQL failover 保留它，PITR/restore/clone/rewind 必须在重新开放 writer 前轮换。同一 shard 至多一个 PostgreSQL finalized current，并保留一个 exact previous；current/previous 是 PostgreSQL exact pointer，不使用 registry mutable alias，也不表示模型常驻两个 Session。rollout group/cohort 是 operation scope，不是第三个 binding slot；
- rollout operation identity：至少固定 `schema_version/model_control_incarnation_id/operation_id/kind(rollout|rollback|recover_current)/idempotency_key/request_digest/requester_identity_ref/scope/rollout_group/ordered_shard_set_digest/expected_binding_vector_digest/expected_routing_vector_digest/desired_revision/all_contract_profile_digests/proposed_generation_vector_digest/proposed_routing_epoch_vector_digest/issued_at/expires_at/trace_id`，自动发布还必须携带 exact `policy_digest`；人类 requester 使用稳定 `(iss,sub)` 引用而非 email/display name。`recover_current` 只能重建 exact current，不能改变 revision。相同 operation/key+digest 幂等，相同 operation/key+不同 digest 稳定冲突；
- pool startup envelope：对每个pool generation绑定`schema_version/model_control_incarnation_id/operation_id/kind/logical_pool_id/pool_generation/availability_profile_id/deployment_tier/model revision/inference_wire_profile_digest/runtime_profile_id/optimization_mode/raw-or-optimized-artifact/repository_snapshot identity+closure digest/all Gateway-Triton-ORT-EP-contract-runtime-resource-profile digests/explicit instance_group+operator-partition digest/CPU-feature-thread-NUMA-RAM或CUDA-driver-cuDNN-GPU-VRAM digests/proposed_binding_generation/issued_at/expires_at/trace_id`；所有profile必须绑定`required_replicas/min_ready_replicas/max_unavailable`，`availability-single/v1`另明确恰好一个failure domain、域内replica count与中断语义，`availability-ha/v1`另绑定`failure_domain_set/max_failure_unit/static_N_plus_1/headroom/remaining_capacity`。`runtime_profile_id`、`availability_profile_id`与`deployment_tier`必须由管理员显式选择，probe只验证并记录selected/observed profile、provider partition与hardware，不能自动改写。每个replica另有不可复用worker identity。envelope自身有digest并以受控普通文件或认证部署接口交付；Gateway/Triton在load前完整验证，旧incarnation/pool/current、过期/不完整/digest、repository额外成员、implicit instance、provider partition、profile或硬件不符必须拒绝startup/readiness；
- loaded-model observation：`GetLoadedModel/GetPoolStatus` 返回 startup-envelope/repository digest、model-control incarnation、logical pool/pool generation、worker runtime identity/failure domain、actual bundle/contract/wire/runtime/profile/optimization artifact digest、proposed binding generation、verification/load/backend-Session/warmup/Gateway-probe 分阶段 result/time 和 observed time。它是 per-replica/pool readback evidence，不是 current；同一 observation identity+same payload digest 幂等，same identity+different digest 冲突；
- deployment action/termination：每个 pre-stage/start/query/quarantine/drain/stop 动作使用 `(operation_id,logical_pool_id,pool_generation,worker_runtime_id,action_seq,action_kind)` + request digest 作为幂等身份；同 key/digest 返回同一 observed workload/worker identity，同 key/不同 digest 冲突。adapter 必须报告 workload UID、selected/observed runtime profile、CPU/NUMA或GPU/failure-domain资源和Gateway/Triton runtime identity，不得以Pod名/`Ready`代替readback。旧generation stop前必须完成相关shard route-withdraw/drain/commit与in-flight截止；异常强制终止记录incomplete-drain/gap；
- committed-binding handshake：Go 在 CAS 后向 Edge 发送 versioned、幂等的 `(model_control_incarnation_id,operation_id,scope,shard,shard_routing_epoch,logical_pool_id,pool_generation,expected/proposed/current binding generation,startup-envelope/pool-observation/binding digest,deadline)`；Edge 只对 exact match ACK，并返回 runtime epoch、pending/gap/resume watermark。CAS 后 handshake 未确认时 canonical current 不回退，但 availability 为 `resume_pending/unavailable`，只沿原 handshake reconcile；
- control methods/错误：Go 边界至少包含 `RegisterEvidence/RequestRollout/GetRollout/AbortBeforeRouteSwitch/RequestRollback`，Central Inference 控制边界只包含只读 `GetLoadedModel/GetPoolStatus`/health 或等价方法；固定幂等、deadline、资源、cancellation、readback 和 `invalid_manifest|incompatible_contract|unqualified|resource_exhausted|startup_failed|readback_mismatch|cas_conflict|buffer_overflow|pool_unavailable|fenced` 等稳定错误。operation state machine 必须机器可读地限定合法前置状态、per-shard CAS 条件、外部动作、terminal result 和 retry/reconcile，非法跳转稳定拒绝；
- golden：`contracts/model/v1/golden/` 必须保存 manifest/identity/taxonomy/adapter/result/error 的机器可读 input、expected bytes/digest/status；合同冻结 canonicalization version、字段/unknown-field 规则、class order、浮点 absolute/relative/ULP tolerance 以及 NaN/Inf/signed-zero 行为，各语言 runner 只能消费同一组 vector；
- unknown major、unverified minor、digest/metadata/schema/shape/dtype/opset/backend/device/resource drift、qualification/revocation 失效或 custom executable content 必须在 Session 创建/readiness 前拒绝；ONNX `metadata_props` 仅作冗余自描述，必须与外部 manifest 一致，不能覆盖 contract 或资格事实。

生产 model rollout 必须绑定 exact qualification evidence、scope/shard、current/previous、actor/policy、operation、pool startup envelope、repository snapshot closure 和 generation。模型 revision/绑定不属于通用插件 kind；MLflow alias、Triton repository/version、KServe route、Kubernetes Service/Deployment/Pod/Ready 状态均不能成为 canonical model binding。Triton repository只作为已解析、只读、digest-pinned 的执行输入；NONE 模式加载范围必须等于 manifest closure，不能因仓库中碰巧存在额外模型而扩大。

### 5.13 成熟组件与供应链登记契约（CONTRACT-SUPPLY-001）

- `contracts/supply-chain/v1` 是全系统成熟组件采用状态和第三方资产清单的机器可读 source；每项必须记录 `component_id/category/ADOPT|CONDITIONAL|REJECT`、用途/非用途、稳定`source_id`、upstream title/URL/access date/archive snapshot+digest、exact version/revision/digest、license/SPDX/NOTICE、package/image/file/provenance/SBOM、运行身份/capability/data access、owner/support/EOL、故障/降级、兼容/升级/回滚/退出、qualification evidence 和 decision/waiver reference；动态网页、branch 或 `latest` 只能作为导航，不能单独支撑资格结论；
- `ADOPT` 只表示在声明边界和 exact profile 下首选，不表示已实现或 production qualified；`CONDITIONAL` 必须列出可机器判断的触发条件与禁止条件；`REJECT` 必须记录被拒用途和理由，防止以别名、间接依赖、sidecar 或 dashboard 重新引入；
- package lock、OCI digest、CLI/generator image、漏洞数据库 snapshot、dashboard/config/chart、generated output、vendored/forked source、字体/图标/fixture 和传递依赖必须能够映射到 inventory；源码复用还必须逐文件绑定 upstream path/digest、修改、copyright、source/NOTICE obligation、测试与退出；
- build/admission/release 必须拒绝未登记 direct dependency/image/tool、source/output digest 漂移、许可证/NOTICE 缺失、已撤销/过期 artifact、未批准 capability/data access 和 production runtime 网络下载；离线漏洞数据库过期应使安全资格 `HOLD`，但不得改变已持久核心事实或自动回滚设备；
- module inventory 可以从语言 lockfile、OCI SBOM 和 source manifest 生成，但必须汇聚为同一全系统 inventory 并保留 module ownership；不得以一份手写表替代真实 lock/transitive/image/file 扫描，也不得让 SBOM、签名或 vulnerability scan 单独证明功能、安全或许可证兼容。

### 5.14 P4 流量 Fixture 与回放契约（CONTRACT-TRAFFIC-001）

`contracts/testkit/traffic-replay/v1` 是 BMv2/P4 功能、规则结果和检测链重放输入的唯一机器可读合同。每次运行必须引用 immutable manifest 与 digest，禁止把 shell command、文件名约定或测试脚本默认值当作合同。manifest 至少固定：

- identity/provenance：`fixture_id/revision/class`，其中 class 封闭为 `generated-packet|synthetic-flow|curated-pcap|live-session`；source URI/reference、取得日期、SHA-256、byte size、producer/tool/version、citation、license/use/redistribution 条件、payload/sensitive-data 状态、malware/exploit-content 声明、sanitization 与 retention；外部来源许可或隐私事实不明确时只能本地隔离评估并 `HOLD`，不得提交、缓存到共享制品或随产品分发；
- capture inventory：PCAP/PCAPNG format、DLT/link type、timestamp resolution、snaplen、每包 `caplen/origlen` 一致性、packet/wire-byte/captured-byte/flow count、首末时间和 truncation/malformed 状态；存在不受 profile 支持的 DLT、截断或解析歧义时 fail closed，不得静默补齐缺失 payload；
- ground truth：label taxonomy/version、产生者与方法、粒度（packet/flow/window/session）、时间/五元组映射、coverage、unknown/background/ambiguous/conflict 语义和 expected detection/outcome；CSV 时间标签或数据集说明不能在未经 canonical join/golden 验证时自动成为 packet truth；
- topology/direction：`target_class=bmv2-software|qualified-hardware`、BMv2/P4 program/P4Info、namespace/host/switch/link、ingress/expected egress、client/server side、interface/port map、VLAN/MTU、original address/port 与 rewrite plan；双向 PCAP 必须使用显式、可复核的 direction classification/cache，不得按包顺序、源端口猜方向；
- transformation：原始 artifact digest、`tcpprep`/等价方向 cache digest、MAC/IP/port/VLAN/TTL/sequence/checksum/length/MTU rewrite 的 canonical config、seed、输出 artifact digest和逐类修改计数。原始 fixture 不可原地覆盖；未声明、失败或超出范围的 rewrite 必须拒绝；
- execution mode：`recorded-timing|multiplier|fixed-pps|fixed-mbps|topspeed` 五选一，并显式记录唯一 `traffic_mode`、`runner_profile_id`、`runner_backend_id`、requested value、loop count、最大 duration/packets/wire bytes、preload、warm-up、repeat、seed 和 deadline；不同 speed mode 不得叠加。`topspeed` 只表示 sender 尽力发送，不表示目标容量；runner 不得按本机可用工具自动改用另一 backend，也不得在失败后静默从 live-session/PTF/Tcpreplay/其他发生器互换；
- impairment/environment：可选 netem/qdisc 的 delay/jitter/loss/duplicate/reorder/corrupt/rate/slot/queue limit/seed，应用方向与 exact `tc` state/readback；kernel、iproute2、Mininet/BMv2、veth/NIC、driver、TSO/GSO/GRO/checksum offload、MTU、CPU/NUMA/frequency/cgroup 与 runner image digest。TCP 性能扰动必须按 profile 明确施加位置，不能把共享宿主调度和内核 timer 误差隐藏为网络行为（依据：[Linux netem](https://man7.org/linux/man-pages/man8/tc-netem.8.html)、[segmentation offloads](https://docs.kernel.org/networking/segmentation-offloads.html)）；
- oracle/result：每个受控 case 的 ingress packet/mask、expected egress port/packet/mask/count/tolerance 或 expected no-egress/drop/mirror，预期 eligible/direct-counter delta 和 target capability；结果至少分开 `sender_attempted/sender_accepted`、test ingress observed、DUT ingress/egress/drop、rule counter、independent action outcome 和 detection result，任一层不得由另一层推定。

Tcpreplay 类工具默认只发送已捕获的二层 frame bytes，不建立 socket、ARP、路由、TCP handshake 或应用状态；其“发送成功”只表示 frame 被交给发送接口（依据：[Tcpreplay replay model](https://tcpreplay.appneta.com/concepts/replay-model/)）。需要 handshake、重传、拥塞控制、应用请求/响应或目标真实应答的场景必须选择 `live-session`，使用受控 client/server 或经单独资格化的 stateful generator，并保存两端 transcript/counter；不得把 PCAP 字节回放命名为“真实 TCP 攻击会话”。

traffic manifest 只能表达测试输入和预期结果，不得携带任意 argv、shell、动态 Python、P4Runtime Write、生产 target/credential 或 effect mutation。所有控制面配置仍经 test fixture/Edge 唯一 writer 边界完成；PTF/P4Testgen/Tcpreplay/Mininet/流量发生器均不得取得生产 P4 ownership。

每个 scenario manifest 必须一次性冻结 `traffic_mode + fixture_digest + runner_profile_id + runner_backend_id + target/topology + direction + rewrite_digest + impairment_digest`，并以声明顺序执行 prepare→preflight/readback→send/session→observe/oracle→cleanup。精确 runner/backend 缺失或不兼容时记录 `result=NOT_RUN|HOLD`，禁止通过“自动选择可用方案”获得同一 scenario PASS；切换 backend 必须创建新的 scenario/evidence identity。

### 5.15 在线遥测源、流与窗口契约（CONTRACT-TELEMETRY-001）

`contracts/telemetry/v1` 是所有 Edge capture backend 到统一 feature/window 语义的唯一源，至少固定：

- source identity：`telemetry_source_id`、target/device/role、observation domain/point、source profile digest、P4 program/P4Info/pipeline/application generation、`source_runtime_epoch`、shard和endpoint/capture identity；source sequence只在同一runtime epoch内单调，restart/reconnect/reset必须新开epoch，不能靠数字重新从0开始伪装连续；
- source profiles：首期默认`telemetry-p4-window/v1`；digest/PacketIn只作best-effort supplemental sample/hint；PACKET_MMAP/AF_XDP/DPDK只按`ARCH-TELEMETRY-001`条件启用。每个profile固定输入字段、capability、触发/读/清语义、最大target/flow/window/packet/bytes/rate/queue/batch/deadline、drop/backpressure、sampling/coverage和不支持时的错误，禁止隐式backend fallback；
- observation/window identity：source epoch/sequence range、target、inference shard、model/feature generation、`window_id`、半开`[window_start,window_end)`、input/content digest；不同source/runtime/application/model generation、observation point或window profile不得拼接；
- 时间：packet/event time、source export time、Edge ingest time、window finalized time、produced time和用于queue/deadline的同runtime monotonic time分开。每source/shard固定watermark算法、max out-of-order、idle timeout和allowed lateness；只有`final`且quality valid的窗口进入canonical inference，final后数据为`late_after_final`且不得改写旧Event；
- flow key：明确`unidirectional|canonical_bidirectional`、observation point、IPv4/IPv6、protocol、稳定endpoint排序、port、VLAN/tunnel/fragment（适用时）与generation。无L4 header的fragment必须表达`port_unavailable`和fragment identity，不能使用0/垃圾值冒充普通五元组；
- source snapshot：P4 register/counter aggregate必须选择target-qualified bank flip、freeze/barrier/snapshot或sequence-before/after验证；多entity Read顺序不是全局快照。无法在deadline内证明一致时窗口为`partial/not_measurable`，不得将不一致值推理为完整feature；
- sampling/coverage：algorithm、rate、seed/hash selector、eligible population、observed sample、加权公式/误差和coverage。sampling只可供声明兼容的feature/model profile使用，不得把sample count当全量packet count；
- quality namespace：`valid|partial|gap|stale|not_covered|not_measurable|invalid`加稳定reason bitset；保存expected/observed sequence、gap range、source/capture/kernel/NIC/P4/server/client drop、snapshot consistency、parse/truncate/checksum/fragment、watermark/late/idle/reconnect。该namespace不得复用effect `unknown`，缺测不得补0；
- raw packet policy：默认只输出header-derived metadata和定长数值feature。payload/raw packet只有专用capture/evidence profile可以短期保留受控reference，不能进入推理ring、InferenceResult、PostgreSQL热表、日志、metrics、trace或Frontend。

### 5.16 中央推理传输与结果确认契约（CONTRACT-INFERENCE-001）

`contracts/inference/v1` 必须同时定义业务语义和跨主机 wire，不允许 Rust/Gateway/backend 各自复制或猜测同名结构。至少固定：

- `inference-central-grpc-batch/v1`：固定 Protobuf/gRPC/TLS profile、request/result service/method、max message/record/tensor count与bytes、compression policy、deadline/cancellation、channel/connection reuse、keepalive、resolver/load-balancer、per-source/shard quota、bounded in-flight和retry budget；未知major、oversize或TLS identity不符在完整解析/分配前拒绝；
- request identity：model-control incarnation、inference shard、route epoch、logical pool/pool generation、binding generation、source/window identity与sequence、request/attempt ID、feature/label/adapter/wire/runtime profile digest、record count、enqueue/deadline time、trace reference和input digest。retry必须沿用request ID/input digest；same ID+same digest幂等，same ID+different digest冲突；
- tensor descriptor：稳定 tensor ID/name、dtype、rank/shape、payload offset/byte length/alignment。offset+length使用checked arithmetic且完全落在message payload；shape/bytes、overlap、overflow、unknown major或digest drift在分配GPU buffer/运行模型前拒绝；
- batching：Edge只无等待合并已经final且立即可发送的记录，并在max records/max bytes/最早deadline任一条件触发后提交；Triton是唯一delay-based dynamic scheduler，固定max queue delay/preferred batch/instance group/queue policy。Edge/Gateway不得再增加隐式timer或无界priority queue；
- buffer/runtime：Edge/Gateway/backend预分配并复用input/output buffer。Gateway↔Triton、host↔device的actual copy count/bytes分别测量；只有已资格化Triton shared-memory、I/O Binding/device tensor/stream/lifetime profile才可宣称相应copy优化，不能把Triton/ORT kernel时间当端到端或把网络/shared memory统称zero-copy；
- availability/compatibility：同generation replica失败只允许沿原identity/input digest在logical pool内有界重试，不改变model/runtime profile/backend/contract或route epoch；全池不可用时返回稳定`pool_unavailable/deadline_exceeded`并由Edge缓冲/HOLD/gap，禁止Edge-local或CPU↔CUDA/其他backend/model自动fallback。wire/feature/output/runtime profile/backend major变化使用新pool generation，Edge先withdraw/drain并提升route epoch后单路切换；
- `InferenceInputBatch`/`InferenceResultBatch`：input/result identity、input/output digest、record-level status、scores/predictions/decision/OOD/abstain/quality、stable error和每record→Event mapping。result→Event idempotency key必须绑定source/window、model-control incarnation、shard/route/binding generation、feature/label/adapter profile与input digest；same key+same payload digest幂等，same key+different digest冲突；
- durable confirmation：telemetry/source WAL durable→final window/inference-input WAL durable→central result验证/fence/dedupe→inference-result WAL durable→Edge到Go bounded batch→Go/PostgreSQL canonical Event durable→canonical ACK→Edge checkpoint/compaction。gRPC send/receive、Gateway/Triton/ORT success或replica retry均不能单独推进source cursor、删除未确认result或产生第二Event。

## 6. 功能需求

### 6.1 遥测与窗口（FUNC-TEL-001）

- Edge 必须从真实、已资格化的 telemetry source 产生同 observation point、target、source epoch、model binding、feature profile和generation的连续窗口；首期默认source为`telemetry-p4-window/v1`，不是逐包Digest或PacketIn；
- P4 aggregate必须在读取前证明bank/epoch/snapshot一致；Digest/PacketIn只形成有coverage/drop标记的hint/sample，不独立完成窗口。mirror capture backend只有profile被条件触发时启用，且必须输出同一telemetry contract；
- source/flow/window必须满足`CONTRACT-TELEMETRY-001`的event/export/ingest/finalized time、`[start,end)`、watermark、allowed lateness、IPv4/IPv6/fragment、direction、sampling/coverage和quality语义；只有final+valid窗口进入canonical inference；
- 缺测不能补成 0，不同 generation 不能拼接；
- telemetry source counter/register read、WAL durable、telemetry window/source checkpoint advance、checkpoint durable 的顺序必须可验证；本条不适用于 `CONTRACT-RULE-001` 的规则 direct counter，后者由 observation 只读且禁止周期清零；
- WAL record 必须带 magic、version、sequence、length、CRC 或等价完整性字段；
- partial tail、重复 batch、乱序、回退 sequence 和磁盘满必须有确定语义；
- final后迟到数据、source idle/reconnect、snapshot不一致、capture/P4/server/client drop必须形成明确`late_after_final|partial|gap|not_covered|not_measurable`及reason，不回写旧Event或默认分类；
- backpressure必须从Gateway/Triton/selected CPU或GPU compute/result/Go queue反向传播到Edge window/capture调度；达到上限时降采样只能使用已声明source profile，否则显式gap/HOLD，不得通过静默丢弃、fallback或补零伪装健康。

### 6.2 在线推理（FUNC-INF-001）

- 推理必须支持批量输入；
- 首期hot data path必须遵守`CONTRACT-INFERENCE-001`：每shard经`inference-central-grpc-batch/v1`向exact logical Central Inference pool generation发送有界batched-unary Protobuf/gRPC；pool在启动前显式绑定CPU或CUDA profile，禁止逐记录JSON/HTTP/gRPC、全池单一长寿命bidi stream和任何本地/运行时profile fallback；
- Edge只无等待合并已final记录，并在max records、max bytes或最早deadline触发时提交；Gateway对framing/tensor/identity/digest/fence做完整校验，Triton按冻结的dynamic-batch queue delay调度，并在deadline/cancellation后阻止late result成为canonical；
- 相同模型、输入和配置产生契约等价的确定性输出；
- 模型、scaler、feature contract、class map、pool startup envelope、repository snapshot和runtime/offline-optimization compatibility必须在每个worker启动时验证schema/hash/shape/dtype；
- 不再要求自研发布者身份、key registry、release signer 或签名报告；
- 输入损坏、不兼容或超限必须拒绝，不能降级为任意默认分类；
- 推理结果必须携带 source/window identity、inference shard、route/process epoch、exact model/binding generation、contract/profile digest、input/output digest、scores/decision、OOD/abstain/quality、record status 和 node/runtime identity；
- Central Inference只计算和返回结果，不确认Event durable。Edge必须先对same-generation replica result做identity/digest/fence/dedupe并写入result WAL，再向Go有界批量发送；只有Go/PostgreSQL返回同一canonical result/Event ACK后才能推进相应result/source checkpoint。

### 6.2a 在线模型替换与类别扩展（FUNC-INF-MODEL-001）

- 同一 `contracts/inference/v1` 与 `contracts/model/v1` 兼容 major 下，替换模型算法、权重、阈值、scaler 或扩展 label taxonomy 不得要求修改 Rust Edge/Go Event 核心代码；所有差异通过 model bundle、feature/label contract 和 output adapter 表达；
- Go Model Manager 只接受 Offline ML/CI 已生成且 evidence 完整的 immutable revision；日常 exact-binding rollout/rollback 可由一个具有 model/scope 权限的 Platform Admin 或 Owner 批准的版本化自动发布 policy 发起，不新增 legacy model/release 多域签名或通用 maker-checker 工作流。quality threshold、label/policy 语义或自动发布 policy 的放宽仍需提升相应 Owner baseline/profile；
- rollout 顺序固定为：durable operation + ordered shard/expected binding+routing vector → 事务外 pre-stage exact read-only model-repository snapshot/pool envelope → deployment adapter启动新 pool generation的Gateway+Triton replicas → Triton startup-load、warmup、per-replica exact readback并证明所选availability profile的min-ready/rolling capacity（HA另含failure-domain/N+1）→ 对一个 shard由Edge提升route epoch、撤销旧logical-pool route、停止新窗口并排空admitted work、在既有WAL硬上限内缓冲新完整窗口 → PostgreSQL短事务按model-control incarnation/expected generation CAS finalize current/previous → Edge验证committed-binding handshake并恢复到新logical pool generation → 依次处理下一 shard → 旧 pool generation有界drain/rollback grace后停止；任何外部等待期间数据库 active transaction/held connection 必须为零；
- 滚动期间每个已 finalize shard 的新 revision 与尚未滚动 shard 的旧 revision都是各自 canonical current；group projection 为 `rolling_mixed`，不得把部署进度包装成全局单一 current。canonical result、window、cursor 和 rollup 不得跨 model generation 拼接；Offline ML/rehearsal 可以在同一 frozen input 上显式比较两个 exact revision，但必须保持非权威资格证据。跨 shard聚合可以并列展示，但不能省略 revision/generation。依赖跨 shard可比性的 policy/effect 在 mixed期间默认 `HOLD`，除非 exact compatibility policy明确列出两个 revision；
- 新 pool startup/readback/capacity 或单 shard route/CAS 失败时不得继续后续 shard；失败 shard 保持最后一次已确认 current，但 availability 为 unavailable/HOLD，或由新的 rollback operation选择 exact previous pool generation；已经 finalize 的 shard不自动盲回滚。操作者必须看到 mixed/failed shard 集合并显式选择继续、修复或滚动回滚；
- 首期生产不运行 candidate shadow 或 weighted traffic canary。候选质量/numeric/taxonomy/decision/resource 比较在 Offline ML frozen replay 或隔离、无生产副作用的 rehearsal 完成，其证据不能写 canonical Event/effect。新旧 pool generation在资格化 rollout/drain grace内短暂并存只用于精确路由切换/回滚，不得同时消费同一 shard canonical input；
- 新 label 默认只可形成带 exact taxonomy/model identity 的检测事实。任何 deterministic auto-effect policy 必须显式绑定允许的 model/feature/label/adapter/profile digest 与具体 label ID；未知/新增 label、OOD/abstain 或低质量结果不得继承旧 label 的 effect eligibility；
- feature schema/runtime/output contract major 改变时，必须先部署能够读取 current/previous wire/schema 的 Edge/Gateway/Go image，执行 producer-consumer/golden/replay/rollback 矩阵，再切 binding；“dual reader”只表示代码/image在滚动期间兼容两版schema，不授权同一endpoint自动选择多版本模型。wire不兼容时使用新logical pool/profile，并由Edge在drain后按新route epoch单路切换，禁止旧新pool同时写同一canonical Event；无法兼容时以新inference scope/epoch切换。

### 6.3 Event 与 Incident（FUNC-EVENT-001）

- Go 必须按稳定 idempotency key 接收有界 event batch；
- duplicate 必须返回同一 canonical identity，hash 冲突必须拒绝；
- Event 表必须按时间分区并支持 target、decision、incident 和 cursor seek；
- Incident/rollup 是可重建投影，不得反向改写 Event fact；
- Event 不得在无 FlowEvidence 时声称已知攻击源五元组；
- Event ingest 不得等待 LLM 或 Agent Plugin。
- Go必须按`CONTRACT-INFERENCE-001`验证result→Event idempotency key、payload digest、source/window/model/route/binding/quality和final状态；same key+same digest返回原canonical identity，same key+different digest拒绝。只有PostgreSQL事务提交后返回canonical ACK，gRPC接收或内存投影不构成ACK。

### 6.4 Evidence 与 Capture（FUNC-EVIDENCE-001）

- 系统必须交付 bounded flow evidence/capture 能力，并与自动 effect 分开验收；
- capture 必须有 target、duration、sample count、bytes、TTL 和并发上限；
- Agent Plugin 只能建议收集证据，不能直接启动 capture；
- 证据必须绑定 switch generation/session、capture window、content hash 和实际观测五元组；
- generation 改变或 evidence 过期后只能用于解释，不能用于新 effect。

### 6.5 Policy 与 Effect（FUNC-EFFECT-001）

- vNext 不复刻 legacy ReviewPacket、Workflow DAG 或通用 approve/edit/reject 状态机；只实现 effect 专用 proposal/decision 事实；
- effect 只能由已资格化、Owner 启用的版本化 deterministic policy，或满足 `FUNC-GOV-001` 的有效 authorization decision 发起；
- approval/command 本身不得调用 Edge；创建 intent 前必须重新校验当前 evidence、P4 schema/P4Info、generation、capacity、TTL、target state、governance profile 和授权有效期；
- Agent recommendation 不能直接转换为设备命令；核心必须重新构造并验证 entry；
- durable effect intent 必须先于 RPC 持久化；
- claim 和 finalize 必须由 CAS/fence 保护；
- 每个 `(switch, generation)` 只能有一个有效 P4 writer；
- `unknown` 必须通过 operation journal 与真实 P4 readback 收敛，禁止盲目重写；
- TTL rollback、显式 rollback 和 bounded capture mutation 使用同一 intent/outbox/readback 路径，不建立第二队列；
- `p4-stateless-firewall/v1` 的 response overlay 与 baseline policy activation 同样复用本条唯一 proposal/decision/intent/outbox/readback/CAS 链；baseline policy revision/change API 可以有独立业务页面和 typed proposal kind，但不得建立第二执行队列、dispatcher 或 P4 credential；
- multi-target/fleet change 只允许按 `CONTRACT-FLEET-EFFECT-001` 将一个 exact Decision 展开为 bounded per-target child intents；parent、wave、target mapping 和外部 inventory 均不可被 claim。一个 target child 的结果不影响其他 target 已确认的 current，也不能把多数成功投影成全局 applied；
- Proposal/Decision 达到 stale、expired、rejected、superseded 或 `HOLD` 时禁止创建 intent；Intent 预条件失效时禁止 Edge RPC。

首期哪些 policy 可以自动产生 effect、哪些只产生 proposal，按照已确认的 `DEC-004` 执行。

### 6.6 Effect 专用治理（FUNC-GOV-001）

首期采用上游 OIDC 身份和核心内的细粒度 effect scope，不建立自研账户系统或通用 RBAC/Workflow 引擎：

| 角色 | 可以 | 禁止 |
|---|---|---|
| Analyst | 读取授权范围内的 Event/Incident/Evidence/Runtime；创建不可变 proposal；通过新 revision 补充引用和理由 | approve、自批、创建 intent、调用 Edge/P4、修改已提交 proposal |
| Operator | 在授权 target/risk/effect scope 内 approve/reject；发起或授权 R0/R1；查看执行/readback | 绕过重验、授权范围外 effect、以 admin 身份替代 effect scope |
| Platform Admin | 管理 OIDC role mapping、policy/governance profile、P4Info/config、target candidate/identity/lifecycle/assignment、插件 trust policy/catalog/qualification/activation/revocation 与部署；创建 immutable baseline firewall policy revision 并提交 exact compiled diff | 默认不具备日常 effect 或 firewall activation approve/execute 权限；不得自动接管外部 inventory 中的设备、修改历史 decision、绕过插件资格门禁或以 tag 代替 exact digest |
| Auditor | 只读 proposal、decision、intent、attempt、readback 与审计导出 | 任何 mutation |

同一人可以从 IdP 获得多个角色，但每次请求只按明确 scope 判断；Platform Admin 权限不能隐式蕴含 Operator 权限。首期风险分级为：

| 风险 | 确定性判定 | 授权规则 |
|---|---|---|
| R0 观察 | read/readback 或不改变转发语义的 bounded capture，范围与资源上限已固定 | 已资格化 policy 可以自动；否则 Operator |
| R1 低风险可逆 | exact five-tuple、单 target/单 application generation、单 entry、非受保护目标、可读回、apply TTL 不超过 300 秒 | Owner 启用的已资格化 policy 可以自动；否则一个有 scope 的 Operator |
| R2 提升风险 | wildcard/subnet、多个 entry，或在 `p4-target-fleet/v1` 上限内对冻结、同 profile target set 进行 TTL≤1800 秒的临时 response；受保护目标、接近容量阈值、例外策略，或 TTL 大于 300 秒且不超过 1800 秒 | 必须先有 proposal，再由不同于 proposer 的有 scope Operator approve；maker-checker 不得合一。multi-target 只允许 per-target child intent 和静态 wave，不改变 fleet membership、assignment、pipeline 或 baseline current |
| R3 结构/持久变更 | 无 TTL、TTL 超过 1800 秒、P4Info/pipeline/device config、P4 default/control table、baseline firewall revision/default/selector 或持久/fleet-wide baseline 变更 | NIDS incident effect API 必须拒绝。首期只允许 `FUNC-FW-001`/`FUNC-TARGET-FLEET-001` 定义的单 target 或冻结 target-set baseline activation 通过独立 typed change API：scoped Platform Admin 作 maker、不同稳定身份且具 scope/step-up 的 Operator 作 checker；P4Info/pipeline/device config、target registry/assignment/fleet membership 变更仍不在 effect API 范围 |

纯只读查询/readback 不创建 Proposal、Decision 或 Intent；R0 的治理与 durable 路径只适用于会改变设备状态的 bounded capture 等操作。

风险判定任一输入缺失、过期、无法验证或落在多个等级时，必须取更高风险；无法确定时为 `HOLD`。首期不启用 break-glass。未来若新增，必须提升需求基线，且仍不得绕过 intent/fence/journal/readback/CAS。

R0/R1 的“Operator 发起”也必须先形成 canonical Proposal 和绑定其 exact digest 的 Decision；R1 允许同一 Operator 作为 proposer/approver，R2 明确禁止。系统不存在未持久化、无摘要的直接 operator command。

人类授权必须依据可复核事实，而不是仅依据模型分数、Agent 建议或自然语言摘要。审批视图和 Decision 必须覆盖：

- observed facts、inferred assumptions、unknown/missing facts 分区；
- exact logical P4 diff、target、generation/session、P4Info/schema/target-state digest；
- evidence identity/hash、capture window、freshness 和与目标五元组的绑定；
- risk class 及其确定性 reason code、policy/governance profile version；
- entry capacity 影响、冲突、作用范围、apply TTL、proposal/authorization expiry；
- firewall proposal 适用时的 policy revision/default action、normalized rule diff、overlap/shadow/conflict、logical→physical expansion、active/inactive bank、selector、双 bank 总容量与 expected rollback plan；
- rollback/expiry 行为、预期 readback 和 `unknown` 恢复方式；
- proposer、approver、角色映射版本和完整 proposal digest。

Proposal 本身最长有效 24 小时；R1/R2 authorization 最长有效 15 分钟，且 intent execution deadline 不得晚于 authorization expiry。更短上限可以由版本化 profile 收紧。任何关键事实变化都必须使 proposal/decision 为 `stale`，不得“刷新后继续用旧审批”。

外部 P4/Edge preflight 必须在数据库事务外完成，且有固定 total/per-target deadline、request/response schema、稳定错误码和被签入审计的 `precondition_token`；fleet preflight 返回冻结 target set 的有界 token vector 与整体 digest，不能用多数/平均结果隐藏缺失 target。该 token 不是 PostgreSQL MVCC snapshot，必须至少绑定 proposal/evidence/target assignment/application generation/P4Info/pipeline/target-state/policy/capacity version 与 digest。Go 随后在一个短 PostgreSQL 事务中重新读取这些强类型版本列，以唯一约束和 CAS 验证 token 仍对应当前事实，并写入唯一 terminal decision；单 target 只有全部匹配时才创建或关联唯一 intent，fleet 则按 `CONTRACT-FLEET-EFFECT-001` 原子创建不可 claim parent 与 bounded per-target child intent vector。并发 approve/reject 只能有一个 terminal decision 成功；失败、过期、serialization failure 或 stale 均不得产生 Edge RPC。Read Committed 每条语句可能看到不同快照，若选择 Repeatable Read/Serializable 则必须处理并有界重试 serialization failure；具体 isolation、锁顺序和 CAS SQL shape 必须由 `contracts/db/effect-cas/v1` 与数据库 golden 固定（依据：[PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html)）。

治理状态与执行状态必须分离：

- 治理：`pending / approved / rejected / expired / stale / superseded`；
- 执行：`pending / blocked / claimed / write_started / applied / failed / unknown / reconciling`；`blocked` 表示副作用前前置条件/波次被确定拒绝且零 Edge RPC，不得与已尝试但结果不明的 `unknown` 混用。

`unknown` 只描述外部副作用结果，不能用于 proposal；proposal 不确定性使用 `HOLD/stale`。Agent Artifact 只能作为带 hash 的补充引用，不能充当 proposer、approver、policy 或 authorization。

### 6.7 API 与用户界面（FUNC-API-001）

- API 必须以 OpenAPI 为源生成 TypeScript client，并为 Go/Rust/Python 提供需要的 generated types；
- dashboard snapshot 与 `/events` SSE envelope 必须来自 `contracts/web/v1`；浏览器不能把多个 endpoint、自定义 event 或缓存值拼成第二套 canonical projection；
- mutation 必须支持 idempotency key 和原 operation 查询；
- 错误 envelope、request ID、分页、cursor、大小、超时必须统一；
- Proposal create、approve、reject、supersede 和 operation query 必须是独立、明确的 API；不存在原地 edit，任何修改创建新 revision/digest；
- Firewall Policy API 必须分开提供 revision validate/compile、diff/overlap/capacity、submit activation proposal、approve/reject 和原 activation operation query；API 只接受 normalized policy contract，不接受 raw P4Runtime entity、P4 source、UFW/nftables/iptables 命令或任意 shell；
- Managed Target API 必须分开提供 candidate import/diff、register、verify/activate/assign/drain/disable/quarantine/retire、detail/observation 和 operation query；Fleet API 必须返回冻结 target set、wave、failure policy、parent status 与完整 child vector。API 不接受任意 SSH/CLI/gNMI Set/raw P4Runtime 请求，也不提供“全部强制成功”或跨 target transaction 参数；
- API 必须返回 canonical proposal/decision/intent identity、当前状态、stale/hold reason 和关联 readback，不能只返回布尔 `approved=true`；
- Frontend 不拼装服务端事实，不从多个不一致 endpoint 猜状态；
- 首页应展示实时流量、Event/Incident、Edge/Inference/Control 状态和 effect 进度；
- 审批页必须在确认动作之前展示 `FUNC-GOV-001` 的完整上下文；危险操作不得只靠颜色、默认勾选或模糊“确认”文案；
- 具有明确 plugin scope 的 Platform Admin 可以查看 catalog、manifest/digest、publisher、SBOM/provenance、qualification、capability/resource profile、active generation、drain/revoke 和运行状态；生产激活/回滚/撤销必须绑定 exact revision/digest/scope，不能用“最新版本”或通用确认代替；
- Plugin Statistics API 必须分开提供 definition/list/detail/history、on-demand run request、schedule list/create-revise-disable 与原 run query；Go 返回 exact plugin/revision/generation、definition ID/revision/digest、scope/window、input/result digest、quality/coverage/truncation 和 projection version。on-demand 需要当前 actor 同时具备源数据 read 与 `plugin.statistics.run` scope；schedule mutation 需要 scoped Platform Admin、相关 data-class scope、idempotency key、immutable revision/digest 和 audit，不能原地 edit。每次 scheduled run 仍重验当前 binding/revocation、schedule policy 与 data scope。浏览器不得直连插件、下载无界原始事实或自行计算 canonical result；
- mutation timeout 后必须查询同一 proposal/operation；UI 不得自动重提、重批或新建 effect；
- Agent Recommendation 卡片必须显式标记“不可执行”，不得提供把原始 Agent payload 直接转换为 proposal/intent 的快捷路径；
- Analysis Plugin 禁用时，Analysis 区域显示 unavailable 或隐藏，其他页面和 API 保持可用；
- UI 状态不能只依赖颜色，必须满足键盘、移动端和 reduced-motion 要求。

### 6.8 Frontend 信息架构与交互（WEB-UX-001）

- 一级导航固定围绕用户任务组织为：Overview、Detection（Event/Incident）、Evidence、Effects & Governance、Analysis、Plugins、Operations & Audit；是否可见由 Go 返回的 capability/scope 投影决定，隐藏导航不能代替服务端授权；
- 全局 shell 必须持续显示当前 scope/target、时区、时间范围、刷新/暂停、数据新鲜度、登录 actor/当前角色、全局 `HOLD/degraded` 状态和不可变 build/profile identity；切换 scope、actor 或 session 必须先清除旧 server-state cache；
- Overview 使用 Go 提供的有界聚合 snapshot，而不是浏览器并发拼装大量 endpoint；每张 KPI/图表显示定义、单位、时间窗、最后更新时间、数据来源和 stale/partial 状态，并可下钻到保留同一过滤条件的明细；
- 筛选、排序、游标、选中对象、tab 和时间范围必须可编码为可复制 URL；列表使用服务端稳定 cursor，不使用无界 offset 深翻页；刷新、返回和共享链接不得丢失上下文；
- 列表采用“可扫描表格 + 可深链详情”的 master/detail 交互：桌面可使用抽屉快速研判，但每个 Event、Incident、Proposal、Operation、Artifact 和 Plugin Revision 必须存在可直接访问的完整详情路由；
- 每个页面和组件必须定义 `initial/loading/empty/partial/stale/HOLD/unauthorized/unavailable/error/retrying/ready` 状态，禁止用空白、无限 spinner、旧值无标记或 toast 代替持久错误；
- 搜索、时间范围、分页、导出和刷新必须显式有界；大导出由服务端异步 operation 生成，不能在浏览器拉取无界事实后拼装；
- 采用 1Panel/sub2api 可验证的侧栏、状态卡、趋势图、密度切换和明细抽屉等成熟交互模式，但必须以 MASI-NIDS 任务、契约、状态语义和自有视觉系统重新实现，不复制第三方页面表达。

### 6.9 Frontend 服务端状态与实时更新（WEB-STATE-001）

- OpenAPI generated client 是 API 调用唯一入口；不得在页面内手写漂移的 URL、DTO、错误解释或状态合并逻辑；generator/source/output digest 必须进入 `web-spa/v1`；
- server state 使用资格化 query/cache library 管理；Pinia 只保存导航、主题、密度、非敏感草稿等本地 UI state，不保存 canonical Event/Decision/Intent/Operation/Plugin binding 或授权判定；
- query key 必须包含 API/profile major、scope/target、筛选、cursor、适用的 generation/revision；登录 actor、scope、role mapping、API major 或 generation 变化时必须取消旧请求并清空受影响缓存；
- 插件统计 query key 还必须包含 plugin/revision/binding generation、definition ID/revision/digest、projection version 与 window；SSE 只发送有界 invalidation/identity，完整 Artifact 必须经原授权 API refetch。definition/input projection/binding/revoke/data-class/scope 变化立即清理相关 cache，不能把旧统计标为 fresh；
- canonical server state 默认不持久化到 localStorage、IndexedDB、Cache API 或 Service Worker。首期禁止离线 mutation、后台同步和 PWA API cache；localStorage 只允许主题、密度等明确 allowlist 的非敏感偏好；
- mutation 默认不做 optimistic success。每次 mutation 绑定 idempotency key 与原 operation identity；timeout、5xx、断连或页面恢复后只查询原 operation，禁止 UI/cache library 自动创建第二 proposal、Decision、Intent、activation 或 rollback；
- 实时更新通过一个有界、可恢复的 `/events` SSE multiplex stream 传递 invalidation/小型投影，而不是传输无界 Event 全量。event 必须携带 cursor、sequence、generation、type、produced/data time、payload length/digest；重复去重、间隙/乱序/generation 变化必须触发有界 snapshot refetch；
- `web-spa/v1` 初始 SSE 上限为单 event 64 KiB、每 tab 一个连接、客户端累计待处理 1 MiB 或 1,000 event 先到为止、15 秒 heartbeat、带 jitter 的 1–30 秒退避；连续 10 次连接失败后进入明确 degraded 状态并改用 15 秒 polling，用户可手工重试；
- tab 不可见、网络离线或页面卸载时暂停非必要 polling/request；恢复时先验证 session/profile/generation 和 cursor，再显示 current，不得把旧缓存瞬间标为 fresh。

### 6.10 Frontend 治理与危险操作（WEB-GOV-001）

- Analyst 创建 proposal 时只能从 Go 返回的当前 canonical target/evidence/P4 schema 选择并构造 exact logical diff；Analysis Artifact 只能作为显式 hash 引用，UI 不得把 Agent 原始 payload、自然语言或 score 一键转换成可提交 proposal；
- Operator 的 approve/reject 页面必须在动作区同屏展示 `FUNC-GOV-001` 全部事实、unknown/missing/stale 分区、proposal digest、risk reason、scope、TTL/expiry、rollback、capacity 与预期 readback；任一强制事实缺失或变化时禁用 approve 并显示稳定 reason code；
- R2 approve 在服务端确认 step-up 与 maker-checker 前不可用。所有 approve/reject 必须要求选择受控 reason code；高风险动作按钮与导航/关闭分离，不预选 approve，不使用通用“确定”，不提供批量 approve、键盘单键确认或以 toast 作为唯一结果；
- 确认动作必须明确陈述“当前仅持久化 Decision/Intent，P4 结果稍后经 readback 收敛”；完成后跳转/固定展示 Proposal → Decision → Intent → Attempt → readback 时间线，并保留 `unknown/reconciling`；
- proposal stale/expired/superseded 后只能只读或创建新 revision；刷新数据不得让旧 Decision 重新可用。mutation timeout 只进入原 operation 页面；
- Platform Admin 的 plugin qualification/activate/rollback/revoke 界面必须展示 exact revision/artifact/config/profile/scope/generation、publisher/SBOM/provenance/revocation 与 readiness 证据；禁止 `latest`、tag-only、未展示 diff 的通用确认或把 Platform Admin 隐式当 Operator；
- Platform Admin 的 firewall policy workspace 只能创建/验证 immutable revision；提交后不能自批。Operator activation 页面必须展示 default、priority/overlap、IPv4/unsupported coverage、logical/physical count、active/inactive bank、selector、P4Info/generation、current/previous diff、rollback grace 与 packet-oracle evidence；缺一禁用 approve，页面必须明示这不是 Linux 主机防火墙；
- route guard、禁用按钮、二次弹窗和前端 capability 仅用于防误操作；所有业务授权、stale/HOLD、idempotency 和 CAS 仍由 Go Control 强制执行并返回 canonical result。

### 6.11 Frontend 视觉系统与数据可视化（WEB-VIS-001）

- 建立项目自有三层 design tokens（primitive → semantic → component），覆盖 light/dark、颜色、字号、间距、密度、圆角、阴影、层级、图表和 motion；Element Plus/Tailwind/Sass 只能消费 token，不得各自形成平行主题真相；
- 视觉语言采用克制、高密度、可扫描的运维控制台风格；品牌、logo、插图、文案、图标选择和页面构图必须由 MASI-NIDS 独立产生，不得仿制 1Panel、sub2api、Grafana 或其他产品的可识别表达；
- `healthy/success`、`warning/stale`、`danger/failed`、`unknown/HOLD`、`disabled/unavailable` 使用稳定 semantic token，并同时提供文本、图标/形状和可访问名称；`unknown` 不得与 `failed` 共用同一表达；
- 使用表格呈现精确事实，使用 line/area/bar/heatmap 等图表表达趋势、组成与分布；禁止为了装饰使用 3D、无刻度 gauge、过量 pie、无单位数字或会掩盖不确定性的动画；
- 每个图表必须有标题、单位、时区/时间窗、legend、采样/聚合说明、新鲜度、empty/partial/stale 状态、数据点上限和等价文本/表格入口；颜色不能成为唯一系列或异常区分手段；
- 插件统计图表也必须经过内置 renderer 并显示 producer revision/generation、quality/coverage/truncation；插件 display hint 只选择封闭的宿主组件和字段映射，不能覆盖 design token、ARIA、ECharts option、formatter、交互 action 或错误语义；
- 桌面支持 compact/comfortable density；窄屏只保证只读研判和状态查看，R2 审批、plugin activation 等危险流程在不满足完整上下文展示宽度时必须阻止并引导到受支持视口；
- 非必要 motion 使用 150–250 ms token 且不驱动关键状态；`prefers-reduced-motion` 下禁用非必要过渡、自动滚动、闪烁和图表动画。

### 6.12 Frontend 可访问性（WEB-A11Y-001）

- 所有完整页面、响应式变体和关键流程必须达到 [WCAG 2.2](https://www.w3.org/TR/WCAG22/) Level AA；不能以只测组件、首页或自动扫描替代完整页面验收；
- 导航、表格、筛选、抽屉、dialog、tabs、toast/status、图表下钻和所有 mutation 必须支持键盘、可见 focus、逻辑焦点顺序、关闭/返回和错误恢复；dialog 打开/关闭时必须正确捕获并恢复焦点；
- 使用原生语义或受验证的 Element Plus 语义；名称、说明、错误、required/disabled/busy/current/live region 关系必须可被辅助技术识别，禁止用可点击 `div`、仅 tooltip 或 placeholder 代替 label；
- 文本缩放、200% zoom、窄屏 reflow、对比度、非颜色状态和 reduced motion 必须测试；触屏/移动 profile 的交互命中区至少 44×44 CSS px，桌面 compact 模式仍须保持键盘与 focus 可用；
- ECharts 必须显式导入/启用 ARIA 能力；复杂图表提供人工编写的摘要和等价表格/下载数据入口，不能只依赖自动生成描述；
- CI 执行自动 accessibility scan、语义/键盘 component tests 与 Playwright 流程；Module Complete 前还必须完成屏幕阅读器、全键盘、zoom/reflow 和高风险流程的人工复核并保存证据。

### 6.13 Frontend 性能与浏览器兼容（WEB-PERF-001）

- `web-performance/v1` 必须冻结目标 SOC workstation、CPU/RAM、viewport、浏览器引擎、LAN/受控弱网、API fixture、数据规模、cold/warm cache、build/config/contract digest 和统计方法；开发服务器或主观“流畅”不构成证据；
- 生产 build 必须使用 tree shaking、route-level dynamic import、按需 Element Plus/ECharts 模块和依赖去重；Analysis、Plugins、重图表和导出不得进入首屏同步 chunk；禁止运行期 CDN polyfill；
- 初始 production budget：应用 shell 的同步入口 JavaScript gzip 合计不超过 250 KiB，首屏 CSS gzip 不超过 80 KiB，任一路由首次所需静态 JS+CSS gzip 合计不超过 600 KiB；bundle analyzer 与 route manifest 必须在 CI 逐次比较，超限为 `HOLD`，只能通过版本化 profile 和证据调整；
- 通用列表默认 50、单页最多 200；超过 200 个可见 row/node 的专用视图必须虚拟化。单图默认最多 2,000 个可交互数据点、单页最多 10,000 点，超过时由 Go 聚合/降采样，不能把无界原始序列传给浏览器；
- 插件统计必须同时满足 `CONTRACT-PLUGIN-STAT-001` 的 Artifact/series/points/rows/display-hint 上限；renderer 在目标浏览器分别测 metric-card/table/timeseries/bar/heatmap 最小、典型、最大 payload，不能以插件端预聚合为由跳过 heap/DOM/long-task/soak 门禁；
- 每 tab 同时 HTTP request 最多 8、SSE 最多 1；请求、cache entry/bytes、chart instance、observer/timer 和 DOM node 必须有 profile 上限并在 route unmount/session switch 时释放；soak 不得持续增长；
- 在目标 profile 的真实 production build 上，Core Web Vitals p75（desktop 与受支持窄屏分别统计）必须达到 LCP ≤2.5 s、INP ≤200 ms、CLS ≤0.1；同时记录 route navigation、API wait、render、heap、long task 和 error/retry；
- 浏览器支持由 `web-browser/v1` 精确固定并至少覆盖项目资格化的 Chromium、Firefox、WebKit engine；Vite major 默认 target 变化、polyfill 变化或依赖升级必须重跑 compatibility、visual、a11y、performance 和 E2E，不得静默扩大/缩小支持范围。

### 6.14 Frontend 交付与认证安全（WEB-SEC-001）

- 生产只暴露一个受信 HTTPS origin；SPA assets、`/api`、`/events` 和 OIDC callback 通过同源路由提供。Go/受信认证组件完成 authorization code exchange、会话、scope projection、CSRF 和业务授权；SPA 不读取或持久化 ID/access/refresh token；
- 会话 cookie 必须 Secure、HttpOnly、SameSite 且有受控 path/domain/expiry；所有 mutation 使用服务端签发并绑定 session/origin 的 CSRF 防护，验证 `Origin`/`Sec-Fetch-Site` 等适用信号；登出、actor/scope 变化必须清除 server cache 和非敏感草稿（依据：[OWASP CSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)）；
- `/events` 只能使用同源 session 认证，不得在 URL/query、Last-Event-ID 或 payload 放 credential；Go 必须验证 Origin/scope、在 session expiry/revocation 时关闭 stream，并对响应使用 no-store、nosniff 和受控 framing；
- production CSP 默认 `default-src 'self'` 并按最小需要收紧 script/style/connect/img/font/frame/object/base/form；禁止 `unsafe-eval`、远程 script/module/font、任意 iframe、JSONP 和运行期插件注入。任何 unavoidable `unsafe-inline` 必须使用 nonce/hash、记录例外并验证不扩大 mutation 权限（依据：[MDN CSP](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CSP)）；
- 所有插件统计字符串按不可信纯文本处理，只允许 Vue interpolation/等价安全 sink，禁止 `v-html`、动态 component name、任意 URI、style/template 和表达式求值；CSP 只是附加防线，不能替代 schema/输出编码。CSV 导出必须在服务端防止公式注入，不能把以 `= + - @`、tab、CR/LF 等危险前缀/分隔内容原样交给电子表格；
- 构建输出使用 content-hashed assets；HTML/app config 不缓存或短缓存并验证 build/profile digest，hash asset 可以 immutable 长缓存。滚动部署必须保留上一受支持 asset set，避免旧 HTML/新 chunk 交叉 404，并能按 exact artifact digest 回滚；
- 首期不注册 Service Worker，不提供 offline shell 对 canonical facts 的陈旧展示；若未来启用，只能缓存不可变静态 asset，必须提升需求基线并验证 session/logout/update/rollback；
- 前端 route visibility、字段隐藏、disabled state 和 client-side validation 都不能成为安全边界；Go 对每个 read/mutation 重新执行 scope、freshness、digest、risk、CAS 和审计校验。

### 6.15 Frontend 成熟依赖与源码复用（WEB-SUPPLY-001）

- 默认“复用成熟依赖，独立实现业务界面”：Vue、Vite、Vue Router、Pinia、Element Plus、Apache ECharts、VueUse、TanStack Vue Query/Virtual、Vitest、Vue Test Utils、Playwright 和资格化 OpenAPI client generator 是首选候选；只有 exact version/digest、许可证、NOTICE、SBOM、漏洞/维护状态、bundle/兼容/性能证据进入 `web-spa/v1` 后才可使用；
- 复用分三级：① package manager 的独立上游依赖；②经批准 vendored/forked source；③仅作交互/行为参考的应用源码。不得把“开源”“GitHub 可见”“仅复制一个组件”“改名/翻译/换色”当作许可证或独立实现依据；
- 每个 vendored/forked 文件必须登记 upstream repository、exact commit/tag、原始 path、license/SPDX、copyright、source/output digest、修改清单、测试、更新/漏洞响应与退出策略，并保留适用 LICENSE/NOTICE/source offer；CI 必须检测未登记 vendoring、许可证漂移和 lockfile/SBOM 差异；
- Web 模块启动时必须建立机器可读 `third-party-inventory/v1` 与人类可读 `THIRD_PARTY_NOTICES`，覆盖 direct/transitive/generated/vendored/assets/fonts/icons/themes/fixtures；两者必须由 lockfile/source manifest 生成或交叉校验，不得靠发布前手工回忆补录；
- 1Panel 应用源码按其 [GPL-3.0 license](https://github.com/1Panel-dev/1Panel/blob/dev-v2/LICENSE) 默认归入“参考级”，不得复制其 Vue SFC、业务 composable/store、CSS/theme、SVG/icon、文案、页面组合或品牌资产；sub2api 应用源码按其 [LGPL-3.0 license](https://github.com/Wei-Shaw/sub2api/blob/main/LICENSE) 归入“条件级”，复制/修改/打包其业务组件前必须有 Owner 与许可证合规审查、可满足的分发/源码义务和独立追踪记录；
- 当前仓库没有项目级 LICENSE/NOTICE，因此在 Owner 明确项目分发许可证和合规流程前，任何 GPL/LGPL 应用源码导入一律 `HOLD`。这不阻止通过包管理器使用经审核的 MIT/Apache-2.0/BSD 等独立库，但仍必须满足 attribution/NOTICE/传递依赖和组织 policy；
- 第三方应用只可借鉴不受版权保护的思想、信息架构和交互模式，并以 MASI-NIDS 契约、自有 token/文案/布局重新实现；不得依赖其后端 API、数据库 schema、账号/权限、部署动作、商标或运行时；
- OpenAPI generated client、图标、字体、ECharts theme/example、测试 fixture 和 copied snippet 同样属于供应链资产，必须逐项核对其实际许可证；generated output 是否可用以所选 generator/template 的 exact license 与 header 为准；
- production build 不从公网下载 npm module、脚本、字体、主题或图标；依赖使用 lockfile、受控 registry/cache、checksum、SBOM、漏洞 policy 和可复现 build。许可证通过不等于安全/功能资格，漏洞扫描通过也不等于许可证兼容。

### 6.15a 插件统计计算与前端投影（PLUGIN-STAT-001 / WEB-PLUGIN-STAT-001）

- `pure-transform` 是统计计算的默认 producer：只消费冻结 `StatisticsInputBundleV1` 并返回确定性、可哈希 Artifact。`read-only-tool` 只有 manifest/binding 明确声明只读 external source、data class、provenance 和网络 capability 时才可产生统计；`analysis-agent` 可以引用已校验 Artifact 作叙述，但其自然语言/AnalysisArtifact 不成为 canonical statistics；
- Plugin Manager 在 qualification/activation 时校验并登记 active revision 内的 `PluginStatisticsDefinitionV1`；definition list 来自 immutable manifest/capability，而不是插件运行时注册、数据库扫描或浏览器发现。definition revision/digest、input projection/data class、external-source capability 与 binding generation 任一变化都必须创建新 schedule/run identity，旧结果不得冒充 current；
- Go 拥有 on-demand/periodic schedule、run identity、admission、input freeze、dispatch、cancel/deadline、result validation、current/history 和 retention；`plugin_statistic_runs` 是唯一 durable run ledger，由 Go Control 内的有界 statistics dispatcher 按 CAS/fence 推进，不引入独立 broker/service。插件不得通过 DB polling、内部 timer、MCP/A2A loop 或自身持久 queue 自我调度；statistics run 不是 `effect_intent`，不能被 effect dispatcher claim，也不能创建 Proposal/Decision/Intent；
- Go 在短事务中持久化 run request/current precondition，事务外经 Host-managed 或对应 direct typed adapter 执行插件，再以 exact binding generation/input/result digest/expected current CAS 写投影。任何外部等待期间 PostgreSQL active transaction/held connection 必须为零；
- 默认页面固定为 `Plugins → <plugin> → Statistics`，包含 definition list、current cards/charts/table、quality/freshness/producer、时间窗与历史 detail；宿主可以按 Go 返回的 scope 提供固定 `Run now` 和 schedule list/create-revise-disable 控件，按钮、字段、确认、限流和 operation/run query 全由项目拥有，插件不能声明 action。可以从 Incident/Analysis/Plugin detail 通过 Go 生成的内部 deep link 引用 Artifact，但插件不得增加一级导航、route、按钮、action 或页面脚本；
- Web 只消费 OpenAPI generated client；renderer 按 `CONTRACT-PLUGIN-STAT-001` 的固定 union 生成内置组件和 ECharts `dataset/encode`，不执行插件表达式、完整 option、HTML、SVG、CSS、URL、Vega/Vega-Lite 或 JavaScript。未知/超限内容显示受控 unsupported/error，不降级为 raw payload；
- Go 对每次 read/export 依据 source facts 的数据分类、当前 actor/scope、plugin binding/revocation 与 Artifact identity 重新授权；插件 manifest/display hint 不能定义 ACL。JSON 导出优先，CSV 由服务端有界生成并记录 export audit；
- plugin/Host/Manager unavailable、disabled、revoked 或 current 超过 freshness 时，对应结果明确 `unavailable|stale|revoked` 并保留只读 provenance；核心 Rule Effectiveness、Overview 内置指标、Event/Incident、effect/P4 和其他页面继续，不能以旧 Artifact 无标记填充；
- 首期不允许插件统计自动写回核心 dashboard KPI、rule status、model qualification、Incident risk 或 effect eligibility。未来若允许特定 placement/业务消费，必须新增 host-owned 映射合同、授权与资格矩阵；不能仅靠 display hint 或 config 开启。

### 6.16 规则安装、命中与结果验证（FUNC-RULE-001）

- 首期规则表现对外必须采用三层证据，不提供一个混合“生效率”：① `installation`：effect operation 已 `applied` 且当前 generation/pipeline 的 exact canonical entry readback 匹配；② `dataplane_match`：该 entry 的已资格化 direct counter 在有效窗口内出现 packet/byte 正增量；③ `action_outcome`：受控 probe/traffic fixture packet oracle 或独立 action-specific telemetry 证明预期 drop/forward/mirror 结果；底层 `effect execution` 仍是第四个正交维度并沿既有 intent/journal/readback/CAS 状态机保存，不计入对外三层，也不被三层覆盖；
- P4 program 必须为需要观测的 effect table/action 显式绑定 per-entry direct counter，并在资格测试中证明 counter 的执行位置、packet/byte 定义、width、wrap/saturate/reset 和 action coverage；共享 counter、action 未调用 counter、仅有 table idle timeout 或仅有全表计数时，不能声称 per-rule match；
- Rust Edge 作为唯一 P4Runtime session/StreamChannel owner 负责所有 rule readback/counter Read。Go、Frontend、插件、Grafana 和测试工具不得在生产连接上成为第二 P4 reader/writer；Edge 对规则计数读取、去重、序列、generation/reset fence 和 bounded batch 负责，但不判定业务有效性；
- Go 在 effect exact readback 后创建 canonical observation identity，并接收 Edge 的有界累积样本/窗口增量，写入核心规则观测事实与投影。Control 只依据 `CONTRACT-RULE-001` 计算指标，不能从日志、Prometheus 或浏览器缓存反推 canonical rule state；
- 四个维度必须分开存储/展示：`installation_status=confirmed|mismatch|stale|expired|superseded`、`observation_quality=valid|no_eligible_traffic|stale|invalid|not_measurable`、`match_status=hit_observed|no_hit_observed|not_observed`、`outcome_status=verified|verification_failed|stale|not_measurable`。任一维度不能覆盖 effect 的 `pending/applied/failed/unknown/reconciling`；
- `no_hit_observed` 只表示在已声明 lookback、有效计数和存在 eligible traffic 的窗口内增量为 0；`no_eligible_traffic`、观测 gap、reset/wrap 不明、rule TTL 已过或 denominator 缺失必须分开。零命中不是规则无效、冗余或可删除的充分条件；
- 若 P4 profile 提供同一 table/stage/generation/window 的 eligible-ingress packet/byte counter，才可以计算 `packet_match_ratio = rule_delta_packets / eligible_delta_packets` 和 byte 等价指标；denominator 为 0 时返回 `not_observed`，不返回 0%。若没有同点 denominator，只展示命中增量与 `packet_rate/byte_rate`；
- `same_table_hit_share = rule_delta_packets / sum(comparable_rule_delta_packets)` 仅用于同 target/table/generation/window 的相对分布；`observable_rule_utilization = hit_observed_active_rules / validly_observed_active_rules` 仅描述规则集利用情况。两者名称、coverage 和 numerator/denominator 必须完整展示，禁止改名为攻击拦截率或处置成功率；
- no-hit/dead-rule、shadow/redundancy、overlap/priority 冲突和 expiry 分析只能产生只读诊断或新的 Analyst proposal；不得因 counter、Grafana alert、插件结论或定时任务自动 disable/delete/modify 规则。任何真实变更继续走唯一治理/effect/readback/CAS 链。

### 6.16a BMv2 无状态防火墙功能（FUNC-FW-001）

首期必须完成以下端到端行为：

1. **策略创建与编译**：scoped Platform Admin 通过严格表单/JSON Schema 创建 immutable baseline policy revision；Go 规范化业务字段，Edge 在只读 preflight 中按 current P4Info/profile 生成 canonical entity plan、overlap/conflict/capacity result 和 digest。compile result 不授权也不预留设备资源；任何输入或 target fact 漂移必须重编译；
2. **长期策略激活**：Platform Admin 提交 typed R3 activation proposal，不同 actor 的 scoped Operator 在 phishing-resistant step-up 后对 exact revision/plan/current diff 授权；单 target 时 Go 短事务创建唯一 `effect_intent`，fleet 时按 6.16b 创建不可执行 parent 与逐 target intents。每个 target 的 Edge actor 依次清理/准备 inactive bank、bounded write、逐 entity readback，确认完整后切 selector 并 exact readback；Go 分别 CAS per-target current/previous，旧 bank 在 profile grace 内可显式 rollback，随后沿原 operation 清理；
3. **临时响应规则**：Analyst 可以从 current Incident/Evidence 形成 response overlay proposal；R1/R2 沿 `FUNC-GOV-001` 授权，Go 创建有 TTL 的 intent，Edge 安装/readback，并由 durable expiry intent 删除/readback。LLM/Analysis Artifact 只可作为引用，不提供一键转换或自动授权；
4. **确定数据面语义**：response exact match 优先；miss 后只查询 selector 指定的 baseline bank；permit 继续 forwarding，drop 无预期 egress。baseline priority 更高者优先，冲突 same-priority overlap 写前拒绝；所有 rule miss 执行 revision 明确的 default action；
5. **首期 coverage**：Ethernet/IPv4，TCP/UDP/ICMP，IPv4 source/destination prefix、protocol、ingress port（profile 启用时）、L4 exact-or-wildcard port 和 fragment class。port range、IPv6、VLAN/tunnel、NAT、rate limit、stateful conntrack/L7 只有新 contract/profile/基线后才能加入；IPv6 detection 不得被 UI 表述为 IPv6 blocking；
6. **可验证表现**：每条 active response/baseline rule 绑定 exact install readback、active bank/selector、direct counter 和独立 PTF outcome。inactive/previous bank counter 不进入 current rate；bank cutover新开 observation epoch。counter hit、selector readback或攻击告警减少任一项都不能单独证明 policy/action 成功；
7. **无旁路**：Go、Web、插件、P4Runtime Shell、UFW/nftables/iptables、Mininet host 命令和 traffic runner 均无 production writer credential。任何 host firewall rule 存在都会使 BMv2 oracle environment `HOLD`，防止宿主提前 drop 制造假 PASS。

baseline rollback 是对 exact previous revision 的新授权 operation；只有原 activation authorization 尚有效且 profile 明确允许的短 grace 内，才能按同一 proposal/decision事实执行 scoped rollback，否则重新走 R3 maker-checker。任何 selector/partial write 结果不明都沿原 operation `unknown/reconciling`，禁止重提新 activation 掩盖。

### 6.16b 多 Target 注册、分配与 Fleet 执行（FUNC-TARGET-FLEET-001）

1. **注册与身份**：scoped Platform Admin 可以手工创建或导入 inventory candidate；Go 必须规范化、检测 duplicate endpoint/device identity、展示 exact create/update/no-op/conflict diff，并生成永不复用的 `target_id`。外部 source 变化、删除或名称改变不自动修改 canonical target；
2. **验证与激活**：target 从 `registered` 进入 `verified/active` 前，Go 在事务外让指定 Edge 使用只读/deny-write mode 验证 TLS/identity、P4Runtime arbitration capability、device_id/role、pipeline/P4Info/profile/port map/capacity，随后在短事务中以 Admin step-up、exact observation digest 和 assignment generation CAS 激活。注册或激活本身不安装/删除规则；无法验证保持 `HOLD/disabled`；
3. **每 Target Actor**：Edge 对每个 active assignment 创建一个 actor；actor 必须先证明 current assignment、target-control incarnation、application generation、arbitration 和 pipeline identity才允许 source/effect。draining先停止新 claim，等待有界 journal/reconcile，再释放 mastership；旧 actor late result被fence；
4. **临时多目标处置**：R2 proposal 必须列出冻结 target ID 集合、每 target evidence/generation/P4Info/capacity/diff、共同 TTL 和 exact target-set digest；由不同于 proposer 的 scoped Operator checker 授权后，才按 bounded wave生成 per-target child intents。任何目标缺证据或不兼容不得从 UI 隐藏；policy决定整单 `HOLD`、排除后重建新 proposal，或按明确的 `continue_isolated` scope执行，旧授权不能自动扩展；
5. **长期 Fleet Baseline**：scoped Platform Admin 可对同一兼容 firewall profile 创建 R3 fleet activation proposal，按 target 编译 exact plan/current→desired/rollback vector；由不同稳定身份的 scoped Operator 完成 phishing-resistant step-up，并授权完整 target set和ordered waves。每 target仍独立执行双 bank/selector/readback/CAS，先通过静态 canary wave，再按`fail_fast|continue_isolated|manual_gate`推进；不存在全局 selector或跨设备原子 current；
6. **状态与恢复**：Go 返回 parent + child vector；任一 target `unknown/reconciling` 时 parent必须 `reconciling`，已 applied target继续保持真实current，未开始target不可因多数成功自动claim。停止后对未开始 child写`blocked`且零Edge RPC；修复、继续、重试、rollback均沿原或新的明确operation/idempotency执行；
7. **资源公平**：Edge/Go 必须同时实施 global、per-edge、per-target、per-wave concurrency/bytes/QPS/deadline上限。调度优先级固定为mastership/effect journal+readback > telemetry source continuity > rule observation > read-only device telemetry；一个慢、离线或大规则集target不能耗尽其他target的P4 RPC、WAL、CPU、FD或Go/DB连接；
8. **首期范围**：必须在 exact BMv2 test topology证明 1/2/N target；设备 CRUD、assignment、状态、P4 pipeline/profile、fleet policy和operation审计可像普通网络设备运维一样查看，但不提供端口/VLAN/QoS/routing/OS upgrade/certificate/reboot/拓扑自动发现等完整 NMS 能力。条件 gNMI只读状态不得改变effect或P4 current。

### 6.17 规则表现工作台（WEB-RULE-001）

- Effects & Governance 下增加 `Rule Effectiveness` 工作台及每条 Effect/Operation 的 `Rule Observation` 页签；只消费 Go 的 bounded snapshot/list/detail/history API，并将 target、table、generation、rule revision、time range、lookback、quality 和 outcome filter 编入 URL；
- 页首按当前 scope 分别展示：预期 active rule、exact readback confirmed、hit observed、no-hit-with-eligible-traffic、no-eligible-traffic、stale/invalid/not-measurable 和 independently outcome-verified 的数量；只有分母完整时才显示 installation confirmation ratio、observable rule utilization 或 packet match ratio，卡片必须显示公式、分子/分母、coverage、窗口和新鲜度；
- 主表至少展示 rule/effect identity、target/table、match/priority/action 摘要、installation/match/outcome 三层状态、packet/byte cumulative 与 window delta、pps/bps、同表 hit share、eligible denominator/ratio（可测时）、首次/最后 observed-hit window、样本质量、reset epoch、TTL/expiry、generation/P4Info 和下钻；列表默认 50、最多 200，服务端 cursor/sort/filter，禁止浏览器全量排序；
- 视觉采用：Top-N 命中/低命中规则的水平条形图、packet/byte rate 的时间序列、installation/match/quality/outcome 的 state timeline，以及精确等价表格。Top-N 默认 20 且可暂停；长尾、null/gap、reset、窗口边界和采样精度必须可见，禁止用饼图或单个 gauge 把多层状态合成“有效/无效”；
- `last observed hit` 必须显示为 `(previous_sample_end, current_sample_end]` 区间或明确的 target timestamp precision，不能伪装为精确包到达时间。hover、legend、颜色、图标和文字必须区分 `no hit`、`no traffic`、`stale`、`invalid`、`not measurable` 和 `verification failed`，并提供键盘/屏幕阅读器可用的摘要与表格；
- no-hit/dead-rule 视图必须同时显示 lookback、eligible traffic、规则年龄、TTL、overlap/priority 诊断和 observation coverage，并使用“待复核候选”措辞；不得提供批量 disable/delete/approve，也不得从图表、Grafana link 或 Agent Artifact 直接创建/执行 mutation；
- Grafana 可以复用为低基数运维聚合和只读 deep link，production 不 provision Grafana data-source write、panel action/API mutation 或嵌入式高权限凭据。按 rule ID 的授权明细、审计和历史始终由 Go API/本 SPA 提供。

### 6.17a 防火墙策略与响应交互（WEB-FW-001）

- `Effects & Governance` 下必须提供 `Response Rules`、`Firewall Policies`、`Approvals`、`Operations` 和 `Rule Effectiveness` 的清晰导航；页面在标题与帮助中固定显示“BMv2/P4 数据面”，不使用“系统防火墙”暗示 Linux 主机；
- policy list 显示 target/scope、desired/current/previous revision、default action、active bank/selector、logical/physical/counter capacity、IPv4 coverage、compile/qualification、last activation/readback 和 `HOLD/stale/unknown`；revision detail 显示规则表、normalized diff、priority/overlap/shadow、每规则 expansion/cost、unsupported field 与 PTF outcome coverage；
- rule editor 只暴露 contract 允许的 typed fields/actions；高级 JSON 仍经过同一 schema/normalization，禁止 raw TableEntry、P4 code、CLI/shell。任何 rule/default/priority 改动创建新 revision；UI 不提供原地编辑 current、拖拽后自动生效、批量 approve 或浏览器端 capacity 结论；
- approval workspace 按角色隔离：Analyst 处理临时 response proposal；Platform Admin 准备 baseline revision；Operator 对 exact digest 授权；Auditor 只读。一个 actor 同时具有多个角色也不能在同一 R3 operation 充当 maker/checker；
- operation timeline 必须展示 `prepare inactive → write/readback entries → switch/readback selector → PostgreSQL current CAS → previous retained/cleanup`，以及每阶段 entity counts、deadline、错误和 rollback。selector 已切但 CAS/readback 未确认时显示 `unknown/reconciling`，不得显示“策略已启用”；
- Rule Effectiveness 对 response、active baseline、inactive/previous bank 使用明确 scope/filter；inactive/previous 只显示配置/历史，不混入 active 命中率。IPv6、non-IP、port-unavailable、no-traffic、host-filter interference 和未资格化 action 分别显示，不补零。

### 6.17b Managed Targets 与 Fleet Operations 交互（WEB-TARGET-FLEET-001）

- `Operations & Audit` 下增加 `Managed Targets` 与 `Fleet Operations`；全局 target selector 支持 `all/target group/single target`，但 mutation 必须在确认页重新展示冻结 exact target set，列表筛选或 URL 不构成授权 scope；
- target inventory 以表格展示 stable target ID/alias、site/scope/class、P4Runtime endpoint reference/device_id/role、desired/observed profile、Edge assignment/actor epoch、mastership、application generation/P4Info、lifecycle、freshness、drift/HOLD 和 last readback；endpoint/credential 仅显示受限引用，不显示 secret。详情页分 `Identity & Assignment / P4 & Capabilities / Telemetry & Rules / Operations / Audit`；
- register/import 使用 candidate→diff→validate→Admin step-up→canonical result 的向导；duplicate identity、endpoint/profile/P4Info drift、外部 source deletion和unsupported capability必须逐项显示。页面不得提供 raw CLI、SSH console、P4Runtime entity editor、gNMI Set或“发现后自动接管”；
- fleet operation 建立页必须显示 target count/set digest、兼容性分组、每 target preflight状态、policy/effect diff、ordered static canary/waves、parallel limit、failure policy、deadline和rollback availability；任一 target detail可下钻。R3仍由Admin maker与不同Operator checker，R2仍遵守Analyst/Operator边界；
- 执行页用 target×stage 状态矩阵和ordered wave timeline展示 `not_started/blocked/claimed/write_started/applied/failed/unknown/reconciling`，同时显示parent投影与明确公式。不得用“87%成功”、单一绿色badge或隐藏失败行代替向量；`reconciling`、mixed current和not-started必须非颜色区分且可访问；
- `fail_fast`/停止只阻止尚未开始 child，确认文案必须说明已尝试target不会被撤销；继续下一wave、manual gate和rollback只查询/操作原canonical identity，timeout不自动重复。Fleet rollback页面逐target显示 exact previous/unsupported，并创建新 operation；
- 大量target使用服务端cursor/聚合和虚拟化；exact authorization vector由Go生成canonical digest并支持分页验证，浏览器不得下载全库后计算target set、状态或成功率。SSE gap/generation/assignment变化先标stale并refetch，不能把旧矩阵显示为current。

### 6.18 BMv2/P4 流量生成与回放（FUNC-TRAFFIC-001）

- 首期 testkit 必须提供四种明确分离的测试入口：① PTF/P4Testgen `generated-packet` 用于最小、确定性 table/action/path oracle；② 受控 host/client 命令或程序产生 `synthetic-flow`，覆盖 ICMP、UDP 与真实 socket TCP 的正常/攻击形态；③ Tcpreplay 类 `curated-pcap` 二层字节回放，用于检测链和历史包形状复现；④ `live-session` client/server，用于必须验证 handshake、应用状态、重传或响应的场景。一个 case 可以组合多个阶段，但每个结果必须标明实际 mode，不能互相冒充；
- `generated-packet` 与受控小型 synthetic fixture 是 PR/模块测试的默认输入；大型公开语料只在许可、隐私、完整性和 ground-truth 门禁通过的受控环境运行。[CIC-IDS2017](https://www.unb.ca/cic/datasets/ids-2017.html)、[UNSW-NB15](https://research.unsw.edu.au/projects/unsw-nb15-dataset)、[CTU-13](https://www.stratosphereips.org/datasets-overview) 等名称只是候选来源，不因公开下载或论文引用自动成为项目可再分发 fixture；
- 检测链 replay 与规则 action-outcome qualification 必须是两个独立 scenario：前者验证 packet/flow→telemetry→inference→Event 的可观测行为与标签映射；后者在 exact P4 config/rule 下验证 ingress、expected egress/drop/mirror、eligible/direct counter。检测到攻击不能代替某条 P4 action 正确，action oracle 通过也不能代替模型检测质量；
- Mininet/BMv2 拓扑至少支持 attacker、victim/server、normal client、observation sink 与 BMv2 switch 的显式端口/namespace 映射；双向回放必须将 client/server 两侧分别注入对应 ingress，单接口混合发送只有 fixture 明确声明并证明适用时允许。测试结束必须读取并保存实际 topology、interface、qdisc、offload、route/ARP 和 BMv2/P4Info identity；
- 正常、matching attack、non-matching attack 和 mixed background 必须使用同一 label/fixture contract，且至少覆盖 zero traffic、eligible non-match、matching hit、counter-hit-but-wrong-output 四个反例。攻击类别、地址和 payload 不得硬编码到 runner 业务逻辑；fixture taxonomy/profile 决定预期；
- runner 必须在开始前验证 manifest/digest/limits/topology，原子准备 rewrite artifact 与 direction cache，再配置隔离拓扑/P4 test state、发包、收集独立结果并精确清理。任一阶段失败保留结构化 partial result；禁止把返回码 0、sender accepted 或预期包数当作 DUT/outcome PASS；
- test traffic 不得路由到生产、管理、公网或宿主非 allowlist interface。测试模式不创建生产 Event/Incident/Proposal/Decision/Intent，不触发自动 effect；若 full E2E 需要真实 Go/PostgreSQL 事实，必须使用专用 test tenant/identity/database/target，并按 `TEST-GATE-001` 记录四维资格字段与 exact claim scope。
- `p4-stateless-firewall/v1` 必须提供独立 `stateless-firewall` scenario family，至少覆盖 response hit/miss、baseline permit/drop/default、priority overlap、fragment/port-unavailable、bank preload/cutover/rollback、direct/eligible counter 和 wrong-output 反例；PTF 以 ingress/expected egress/no-egress 判定 action，Mininet/Linux namespace 仅承载拓扑，宿主 UFW/nftables/iptables 必须 disabled/readback 并进入 environment digest，不能成为被测对象或提前过滤流量。

## 7. 通用插件平台与 Analysis Plugin 需求

### 7.1 平台目标与边界（PLUGIN-PLAT-001）

首期必须交付一个受控、私有、可扩展的通用插件平台。平台必须统一处理注册、准入、资格、能力、版本、配置、激活、撤销、观测和回滚，但不得提供任意核心 Hook、公共 marketplace、未经审批的自助上传或进程内动态代码加载。

平台必须满足：

- Go Plugin Manager 是 catalog 与生命周期控制面的唯一所有者，PostgreSQL 保存其 canonical control facts；
- Rust Plugin Runtime Host 是 Wasm 和显式 Host-managed service plugin 的执行隔离层，不成为核心事实源；Host-managed service 可以按 exact deployment profile 使用同机受控 UDS 或跨机 mTLS gRPC，但不能因此取得独立 Agent 的业务代理职责；
- 独立 Agent/service plugin 保持独立进程/容器，使用自身 kind 协议，不与核心共享地址空间或可写 volume；Manager 仍控制其 exact active binding，Go/peer 的业务调用经相应 adapter 直达插件，不经过 Runtime Host；
- 平台关闭、失联或全部插件禁用时，核心检测、Event/Incident、确定性 policy/effect、P4 readback 和非插件页面继续；
- 平台不存在“超级插件”权限；每个插件仅获得单个 kind 和显式 scope/capability 的最小权限。

### 7.2 首期 Plugin Kind 与 Runtime Profile（PLUGIN-PLAT-002）

首期 kind 是封闭枚举：

| Kind | Runtime profile | 允许产物 | 禁止 |
|---|---|---|---|
| `analysis-agent` | 独立 OCI service，A2A 1.0；内部可用 LangGraph/LLM，工具使用只读 MCP | `AnalysisArtifact`、Recommendation、内容、调查清单；可引用已校验统计 Artifact | 核心 mutation、proposal/decision/intent、P4、未经批准工具、把自然语言当 canonical statistics |
| `read-only-tool` | 独立 OCI service，`masi-mcp-readonly/v1` 或经资格化的 `grpc-service/v1` adapter | 有界、带 schema/evidence identity 的只读结果；获准时可返回 `plugin-statistics/v1` | mutation tool、任意 DB/网络、Agent 间协作、插件自调度 |
| `pure-transform` | `wasm-component/v1`：Wasmtime、首期资格 profile WASI 0.2、`masi:plugin-transform@1.0.0` WIT | 确定性、可哈希的有界转换结果；默认统计 producer | ambient I/O、网络、文件、时钟、随机、secret、核心状态、自建 queue |

首期不提供外部副作用 connector、authorizer、policy writer、database writer、effect executor、P4 writer 或任意 UI JavaScript plugin kind。新增 kind 必须提升需求基线，定义独立 contract、side-effect/幂等/恢复语义和完整资格门禁；不能仅修改 manifest 枚举或 prompt 开启。

### 7.3 Capability 与数据边界（PLUGIN-PLAT-003）

- capability 采用默认拒绝、细粒度动词和明确 target/tenant/project/data-class scope；`admin`、`all`、通配 secret 和未解析 URL 不得作为首期 capability；
- 插件只能接收冻结、版本化、带长度和 digest 的数据；不能获得数据库连接、可变业务对象或其他模块内部类型；
- Host API 只能暴露 kind 所需的最小操作；插件不得通过回调、MCP、A2A、文件或网络绕过 manifest policy；
- 除第一方 Analysis Plugin 的隔离 schema 外，首期插件不获得 PostgreSQL 凭据；需要持久化的通用结果必须经公开 Go API 校验并写入 Go 自有投影，或由明确 owner 的独立插件服务保存非核心私有状态；
- 插件输出始终是不可信 candidate/result/Artifact，不能直接成为 Event、authorization、policy eligibility、effect intent、P4 entry 或 runtime truth；
- 插件不得创建 effect proposal、decision 或 intent。人类可以在新 proposal 中引用插件产物的稳定 ID/hash，但 Go 必须从 canonical facts 重新构造并验证候选。
- 统计输入只能是 Go 按当前 read scope/data class 冻结的 `StatisticsInputBundleV1`；统计 output 必须经 Go 验证 exact binding/input/result digest、quality、resource limits 和 display union 后才可进入 Go-owned projection。插件不能定义 ACL、freshness current、前端 route/action，也不能把结果直接写入核心 dashboard、rule/model/Incident/effect 字段；

### 7.4 准入、资格与供应链（PLUGIN-PLAT-004）

插件进入 `staged` 前必须完成 manifest/schema、artifact digest、发布者信任、SBOM/provenance、contract、capability、资源和 isolation profile 静态校验。进入 `active` 前还必须完成对应 kind 的 golden、black-box、fault、security、compatibility 和 performance qualification。

- 生产只接受 digest-pinned OCI artifact/image 或 digest-pinned Wasm component；tag 仅能用于解析，不能成为持久身份或执行依据；
- 生产使用版本化 trust policy 验证 Cosign/Sigstore bundle 或等价组织 PKI，必须绑定允许的 issuer、publisher identity、artifact digest 和 provenance subject；禁止只验证“存在某个签名”；
- trust 验证必须支持保存 bundle 和信任根后离线重验，运行正确性不得依赖每次启动访问公共透明日志；
- 生产 activation、rollback 和 revoke 必须由具有精确 plugin/scope 权限的 Platform Admin 发起或由 Owner 明确批准的版本化发布 policy 自动执行，并绑定 immutable revision、qualification evidence digest、trust-policy digest 和目标 scope；publisher 签名只证明制品来源，不等同于激活授权；
- 首期只读/纯计算 kind 的日常 exact-binding activation/rollback/revoke 不建立第二套 maker-checker、通用 Workflow 或 legacy 多域签名审批链；自动资格检查通过后只需要一次可审计的精确动作。未来引入副作用 kind 时必须提升基线并单独设计职责分离，不能继承当前简化授权；
- trust root、允许的 issuer/publisher/repository/builder、revocation override/freshness grace 和自动发布 policy 属于高杠杆 policy expectations；其变更必须经受保护 Git 变更或等价 append-only `PolicyChange`，由两个不同稳定 `(iss, sub)` 的 Platform Admin/Owner 使用最近 5 分钟内 phishing-resistant step-up 复核，并绑定旧新 policy digest。常规 exact-binding activation 不继承该双人要求，紧急 revoke 也不得被第二人等待阻断。生产 unsigned 或 trust bypass 仍被禁止，不能借双人 policy change 开启，除非提升需求基线（依据：[SLSA v1.2 verifying artifacts](https://slsa.dev/spec/v1.2/verifying-artifacts)）；
- 开发 profile 可以显式允许未签名本地 fixture，但必须标记 `NOT QUALIFIED`，不能生成生产 activation 或被生产配置静默继承；
- 任一关键材料缺失、过期、不匹配、未知或已撤销时为 `HOLD/unavailable`，不得回退到 tag、旧 runtime、无 sandbox 或宽权限配置。

### 7.5 生命周期、升级与回滚（PLUGIN-PLAT-005）

- `plugin_revision` 不可变；manifest、artifact、config、contract 或 capability 任一变化都创建新 revision/digest；
- 生命周期至少包含 `registered → verified → staged → shadow → active → draining → disabled/revoked/failed`，每次转换必须带 actor/service identity、原因、旧新 digest、generation/epoch 和审计记录；
- `shadow` 只消费复制的有界输入并产生隔离比较结果，不能写 canonical business fact、调用外部 mutation 或形成第二 effect path；
- statistics schedule/run 由 Go 拥有且与 plugin lifecycle 分离；插件 activation 不自动创建无界周期任务，disable/revoke 立即停止新 run。old-generation/current Artifact 只读标记 stale/revoked，不能因 rollback 无校验地恢复为 fresh；
- 同一 `(plugin_id, kind, scope)` 同时至多一个 active generation；切换 active pointer 必须原子，旧实例进入 draining 后拒绝新任务，in-flight 工作受 deadline 限制；
- 升级采用 side-by-side + shadow + explicit activation，不允许原地替换动态库、Python package、Wasm bytes、config 或 contract；
- rollback 只能指向仍通过当前 host/kind/config/持久化事实兼容矩阵且未撤销的 revision；不兼容时保持插件 `HOLD/unavailable`，不能删除新事实、恢复宽权限或重放旧输出；
- revoke 必须在可达控制面内 60 秒内传播至全部受影响的 Manager dispatch adapter、Runtime Host 和插件实例，并阻止新 admission/activation/restart/rollback/任务；Host-managed binding 至少每 30 秒由 Host reconcile，独立 service/Agent 的新任务由 Manager/typed adapter 在直连前检查同一 canonical binding/revocation。生产撤销/trust cache 超过 5 分钟未成功刷新时对应 binding 必须停止新任务并进入 `HOLD`；运行实例按风险策略 bounded drain，无法安全 drain 时在 60 秒或该任务更短 deadline 内终止，历史 Artifact/审计只读保留。

### 7.6 官方插件与平台完整性（PLUGIN-PLAT-006）

- 首个必交产品插件是 `masi.analysis.langgraph`，kind 为 `analysis-agent`，实现 `AGENT-001` 至 `AGENT-008` 和 `AGENT-COMPAT-001`；
- 平台必须提供由公开契约生成的 SDK/adapter、manifest schema、Fake Manager、Fake Host、Fake capability provider 和 conformance suite；它们不得复制插件业务实现；
- SDK/conformance suite 必须包含 deterministic statistics fixture，覆盖 input/artifact/display golden、资源上限、old-generation fence、无 DB/P4/effect 权限和 Web 固定 renderer；fixture 证明合同，不构成第二个生产业务插件；
- 首期必须用真实 `masi.analysis.langgraph` 证明 A2A Agent profile，并用无业务权限的 conformance fixture 分别证明 `service-grpc/v1` 与 `wasm-component/v1` 的 handshake、版本、预算、隔离和 lifecycle；fixture 通过不等于存在第二个生产业务插件；
- 增加 Analysis skill 可以继续复用插件内部受测组件，但不得绕过平台 revision/capability/config qualification；
- 平台“可扩展”只有在未知插件拒绝、兼容插件可 side-by-side 安装、不同 runtime profile 通过同一控制面、禁用/撤销不影响核心等黑盒证据完成后才能声明，不能仅凭 interface 或 manifest 文件存在。

### 7.7 Analysis 插件形态（AGENT-001）

Analysis Plugin 必须以 `masi.analysis.langgraph`、`analysis-agent` kind 注册到通用插件平台，经同一 manifest、qualification、activation、generation fence、drain/revoke 和审计控制面后，作为独立进程/容器和协议插件运行；不得作为任意 Python package 动态加载进 Go/Rust 核心进程，也不得因是第一方插件绕过平台门禁。

插件必须有：

- `plugin_id`、版本、镜像 digest、config ID；
- 能力/skill 声明；
- 所需 MCP 工具 allowlist；
- A2A endpoint/Agent Card；
- 输入输出 contract version；
- CPU、内存、并发、deadline、token/cost 和响应大小预算；
- readiness、禁用和升级回滚机制。

### 7.8 A2A 与 MCP 职责（AGENT-002）

- MCP 只用于 Agent 调用工具和读取资源；
- A2A 只用于独立 Agent 的能力发现、Task、Message、Artifact 和多轮协作；
- LangGraph 只用于插件内部编排；
- A2A 不用于实现 LangGraph 子节点或替代 MCP tool call；
- 首期使用 A2A 1.0 HTTP+JSON binding，并按 `CONTRACT-AGENT-001` 显式发送/校验版本，不自动兼容旧 0.x；
- 首期使用直接配置或受保护 Agent Card，不实现公网动态 discovery；
- A2A credential 通过 mTLS/OAuth/service credential 等带外方式传递，禁止写入 Agent Card、Message 或 Artifact；
- 首期不启用 push notification 或 streaming，采用 request/task polling；outbound peer 使用静态 allowlist，按照已确认的 `DEC-007` 执行。
- MCP 固定 2025-11-25 Streamable HTTP；Plugin client 必须支持 bounded JSON/SSE POST 响应，Go server 首期使用 bounded JSON response，不启用 server-initiated notification。

### 7.9 Agent Skills（AGENT-003）

首期必须固定交付以下 skill：

1. `analyze_nids_incident`；
2. `compare_event_windows`；
3. `draft_mitigation_advice`；
4. `generate_incident_content`。

新增 skill 必须声明输入、输出、工具权限、资源预算、失败语义和 Artifact schema；不得仅通过 prompt 增加隐式能力。

### 7.10 分析输入（AGENT-004）

分析任务必须引用冻结的 Event/Incident bundle，而不是把数据库连接或可变对象传入 LangGraph。输入至少包含：

- task/run ID；
- target/tenant/project scope；
- Event/Incident IDs 与 content hashes；
- evidence refs；
- runtime/P4 generation refs；
- input digest；
- plugin/provider/prompt/tool-policy versions；
- deadline；
- locale/content request。

### 7.11 分析输出（AGENT-005）

`AnalysisArtifactV1` 至少包含：

- task/run/plugin identity；
- input digest 和引用事实；
- observed claims；
- inferred claims；
- uncertainties、limitations 和 missing evidence；
- recommendations、risk、preconditions、expiry 和 evidence refs；
- generated content；
- model/provider metadata；
- tool trajectory digest；
- trace/artifact hash；
- `deployment_eligible=false`。

Artifact 必须 append-only。大附件只能以受限 URI/hash/size/media type 引用。

### 7.12 预算与降级（AGENT-006）

首期沿用以下硬上限基线，并可在不放宽安全边界的前提下通过版本化配置收紧：

- 最多 2 次 LLM 调用；
- 最多 2 个 MCP rounds；
- 最多 6 次 MCP tool calls；
- tool parallelism 默认 3、硬上限 4；
- 单 tool timeout 默认 2 秒、硬上限 5 秒；
- 单 tool response 默认 32 KiB、硬上限 64 KiB；全部 tool response 默认 128 KiB、硬上限 256 KiB；
- 单次 LLM timeout 硬上限 6 秒；8 秒 result budget 后不得再发新外部调用，应返回有界、不可执行的 `analysis_outcome=limited|insufficient_evidence` Artifact；
- graph hard deadline 30 秒；
- Artifact 默认最大 64 KiB，不含外部附件；
- 每个 run 最多 2 个 outbound A2A delegation、深度 1、每个 task 最多 3 次 poll、总接收内容 128 KiB，且受同一 graph deadline 约束；
- 必须检测 A2A 委派环路。

LLM/provider/MCP/A2A 不可用、超时、非法输出、预算耗尽或证据不足时，插件必须在 Analysis 自身 namespace 产生 `analysis_outcome=limited|insufficient_evidence|failed`，不能冒充完整 AI 成功。该降级只能减少或停止非执行性 Artifact 内容，不得改变 runtime/model、Event/effect、P4、授权、工具 allowlist 或重试预算；本文不把它称为 inference fallback。

### 7.13 Agent 安全（AGENT-007）

- 所有 Event、Evidence、MCP tool result、A2A Message/Artifact 均视为不可信数据；
- prompt 必须区分系统策略、用户目标和不可信证据；
- 不可信文本不得修改 graph topology、预算、工具 allowlist、身份或安全策略；
- grounding validator 必须验证每个事实引用的 evidence ID/digest 确实来自冻结 bundle 或本次受信工具结果，并把“有证据的事实”“模型推断”“未知/证据不足”分栏；没有稳定 evidence reference 的陈述不能伪装为已证实事实；
- A2A endpoint、Agent Card 和文件 URI 必须受 allowlist、DNS/IP 和 SSRF 防护约束；
- mTLS 必须验证 CA、SAN、hostname 和用途；
- HTTP body 必须在完整反序列化前执行大小限制；
- trace/log 禁止原始 prompt、完整 provider 输出、token、Authorization、cookie、private key、未脱敏异常和 raw packet；
- 插件不得获得 P4 token、Edge volume、Docker socket 或核心 DB 写凭据。

### 7.14 Analysis 插件关闭等价性（AGENT-008）

插件未部署、禁用、崩溃或网络不可达时：

- Edge telemetry/inference 必须继续；
- Event/Incident 持久化必须继续；
- deterministic policy/effect 必须按自身配置继续或安全暂停；
- P4 状态查询必须继续；
- 非 Analysis 前端页面必须继续；
- 核心数据库事实不得因插件状态改变；
- 插件端点应返回稳定 unavailable 状态，不触发隐式重试风暴。

### 7.15 旧实验 Analysis Graph 行为兼容（AGENT-COMPAT-001）

vNext 不迁移旧外层 Workflow v2、revision/node-attempt 数据库状态机或 checkpoint，但必须建立“legacy observable capability → vNext skill/node/contract/test”矩阵，逐项覆盖旧 `masi-event-analysis-v1` 的有价值行为：

- 冻结 Incident/Event/Evidence bundle、identity/hash 与 quality 验证；
- rate/impact、quality/provenance、runtime/P4 read-only、incident/replay 四类确定性上下文，可并行但合并结果必须确定；
- 一次 hypothesis/gap planning、按证据需要执行的 bounded read-only MCP round、可选第二次 synthesis；
- 最多 2 次 LLM、2 个 MCP rounds、6 次 tool calls，tool trajectory 可审计且 mutation tool 为零；
- evidence join、确定性 grounding validator、low-quality/provider/tool failure 的 deterministic `limited|insufficient_evidence|failed` report 和最终 report guard；
- graph/skill version、topology 或等价执行计划 digest、input/output hash、trace identity 和 `deployment_eligible=false`。

兼容指外部可观察的输入约束、分析语义、预算、失败模式和 Artifact 不变量，不要求复刻旧节点名、内部拓扑、Workflow identity、数据库表或 Python 源码。四个 `AGENT-003` skill 可以复用同一受测子图/节点组件，但每个 skill 的能力映射必须明确。

在该矩阵、跨版本 golden/limited-outcome tests 和差异清单完成前，项目只能报告“目标能力已定义”或部分兼容，禁止声称已支持旧 MASI-NIDS LangGraph 工作流的全部功能。Owner 接受的差异必须引用本需求 ID，并证明不扩大工具权限或进入真实处置闭环。

## 8. PostgreSQL 与数据需求

### 8.1 vNext 精简 schema（DB-001）

目标 schema 至少包含以下逻辑域，最终表名可在设计阶段细化：

```text
target_fleet
├── target_control_incarnations
├── managed_targets
├── target_assignments
├── target_capability_observations
├── fleet_operations
├── fleet_operation_targets
└── target_audit_events

events
├── event_sources
├── source_cursors
├── events
└── incidents / rollups

effects
├── effect_proposals
├── effect_decisions
├── effect_intents
├── effect_attempts
└── effect_events

rule_observation
├── rule_observation_epochs
├── rule_observation_rollups
└── rule_observation_events

operations
├── runtime_nodes
├── policy_versions
├── governance_profile_versions
├── role_mapping_versions
└── schema_migrations

model_platform
├── model_control_incarnations
├── model_revisions
├── model_qualifications
├── model_rollout_operations
├── model_bindings
├── model_shard_observations
└── model_audit_events

plugin_platform
├── plugin_revisions
├── plugin_qualifications
├── plugin_activations
├── plugin_runtime_bindings
├── plugin_revocations
└── plugin_audit_events

plugin_statistics
├── plugin_statistic_schedules
├── plugin_statistic_runs
├── plugin_statistic_artifacts
└── plugin_statistic_current

analysis_plugin
├── analysis_tasks
├── analysis_runs
├── analysis_artifacts
├── analysis_trace_events
└── analysis_subscriptions/cursors
```

vNext 不继承全部 52 个历史 migration，不迁移 users/auth_sessions、legacy human review、旧 Workflow DAG、Event v2、legacy demo projection、旧设备 inventory/runtime 表、旧模型签名 registry、mutable model alias 或历史 qualification 控制表。首期 `effect_proposals/effect_decisions` 是新的 effect 专用事实，不是旧 Review schema 的兼容层；`target_fleet` 与 `model_platform` 都是 clean-start exact-binding/control facts，不兼容旧 target alias、controller session、签名或发布状态。需要保留的历史数据通过独立导出/归档方案处理。

### 8.2 数据类型和写入（DB-002）

- 时间统一使用 `TIMESTAMPTZ` 和 UTC 语义；
- 布尔使用 `BOOLEAN`，身份使用明确 UUID/typed key；
- 热查询字段使用强类型列；
- JSONB 只保存低频扩展内容，不代替可索引热字段；
- Event 使用时间分区；
- 时间范围查询优先 BRIN，seek/identity 查询使用必要的复合 B-tree；
- 禁止没有查询证据的无界 GIN/表达式索引；
- Event ingest 使用 bounded batch + `COPY Binary`、staging/merge 或经 benchmark 证明的等价方案；
- 原始 packet、模型、大报告和大附件不直接放热表；
- Proposal 的 target、risk、generation、expiry、digest、creator、current projection 等热查询字段必须使用强类型列；evidence/Artifact 只保存有界引用，不把完整报告复制进治理热表。

### 8.3 写入所有权（DB-003）

- 每张表必须记录唯一 owner role、允许写入者、retention 和删除条件；
- Go core role 不得直接写 plugin schema；若需要投影，只能通过插件公开 API/契约取得结果并写入 Go 自有 core projection；
- Go Plugin Manager 只写 Go 所有的 `plugin_platform` 控制事实，不得写 Analysis Plugin 私有 Task/Run/Trace；
- Go Plugin Statistics 模块只写 Go 所有的 `plugin_statistics` run/artifact/current/history；Runtime Host、插件与 Web 均无该 schema 凭据，插件返回值只能经公开 typed boundary 由 Go 校验后写入；
- Go Model Manager只写Go所有的`model_platform`控制事实；Gateway/Triton/backend、Edge、Offline ML、外部registry/serving/runtime和deploy tool均不得获得该schema数据库凭据；
- Go Target Registry/Fleet Coordinator 只写 Go 所有的 `target_fleet` 控制事实；Edge、P4 target、NetBox/CMDB、Ansible/Nornir、Stratum/ONOS/厂商 controller 和 deploy tool 均不得获得核心数据库凭据或回写 current；
- Plugin role 不能读取或写入 core schema，核心事实读取通过 MCP/API；
- Frontend、Central Inference、Rust Edge 和 Rust Plugin Runtime Host 不获得数据库凭据；
- 数据库权限必须强制执行，不只靠代码约定。

### 8.4 Migration（DB-004）

- migration 必须由独立 job/binary 执行，不能由每个应用实例启动时自动抢跑；
- 每个 migration 必须带版本、checksum、source revision、applied_at 和状态；
- 已应用 migration 不得修改；
- 生产 migration 使用 expand/contract；
- 建索引、表重写、锁等待和执行时间必须有预算；
- 每次 release 必须提供空库安装、从上一受支持版本升级、重复执行、失败恢复和回滚/forward-fix 演练；
- 禁止隐式双写、长期兼容触发器和无界线上回填。

### 8.5 HA、备份与恢复（DB-005）

生产部署必须实现：

- PostgreSQL primary/standby 或托管 HA；
- WAL archive 和 PITR；
- 加密备份与保留策略；
- 定期隔离 restore drill；
- 明确且实测的 RPO/RTO；
- failover 后 application_name、连接池、TLS 和 idempotency 恢复；
- replication lag、archive failure、backup age 和 restore test 告警。
- 无损HA failover必须保持`model_control_incarnation_id`；PITR/restore/clone/rewind必须在Go writer、model rollout和Edge canonical ingest开放前durable新incarnation，旧pool envelope/worker readback/handshake全部失效，再以new recovery operation/new pool+per-shard binding generation重验exact current；不得因复用数字generation接受旧worker结果。
- 无损 HA failover 同样保持 `target_control_incarnation_id`；PITR/restore/clone/rewind 必须在 target assignment、fleet coordinator、effect claim 和 Edge write readiness 开放前轮换从未使用的新 incarnation，废止旧 assignment/wave/preflight/claim，并逐 target 通过 current Edge/P4 arbitration、pipeline/P4Info、selector/entry/journal readback 重验；数据库恢复出的 registry/current 不自行授权设备写入。

三机实验环境可以缩减 HA 组件，但不能据此宣称生产 HA 已完成。

### 8.6 连接池（DB-006）

- 按全部实例和角色计算总连接预算；
- PgBouncer transaction pooling 只服务无 session 状态流量；
- migration、LISTEN、必要 session lock 和管理连接必须直连；
- 每个 pool 设置 max size、acquire/idle/lifetime timeout、statement/transaction/lock timeout；
- 管理和故障诊断保留连接；
- 禁止通过无限增大 pool 掩盖慢事务；
- 禁止跨 Edge/LLM/MCP/A2A/HTTP 等待持有数据库连接。

### 8.7 Retention（DB-007）

- Event、Incident projection、Effect、Runtime、target registry/assignment/capability/fleet operation/audit、model revision/qualification/rollout/binding/startup-readback/audit、Plugin Task/Trace/Artifact、plugin qualification/activation/revocation/audit、plugin statistic run/artifact/current/history 必须分别定义 TTL/保留规则；
- Event 分区应提前创建，旧分区优先 detach/drop；
- default partition 只能作短期兜底，必须告警和搬迁；
- retention 受 legal/diagnostic hold 或等价 pin 保护；
- purge 必须按本次任务拥有的精确范围执行，禁止 wildcard/TRUNCATE 核心事实；
- `unknown`、effect audit 和恢复所需 identity 不得为“归零”而删除。

### 8.8 Effect 治理存储（DB-GOV-001）

- `effect_proposals` 必须不可变；修改以新 revision/new digest 表示，supersede 关系显式记录；
- `effect_decisions` 必须 append-only；同一 proposal digest 最多存在一个有效 terminal decision，并由唯一约束、事务和 CAS 共同保证；
- current governance status 可以是可重建投影，但不能覆盖或删除 proposal/decision 审计事实；
- `effect_intents` 必须引用有效 Decision 或已资格化自动 policy/profile；dispatcher 查询不得扫描或 claim proposal/decision；
- maker-checker、scope、authorization expiry 和 proposal digest 必须在创建 intent 的同一短事务中重验；事务期间禁止 OIDC、Edge、P4、HTTP、MCP、A2A 或 LLM 等待；
- role mapping、governance profile 和 policy 更新必须产生新版本/digest；历史 Decision 保留当时版本，更新不得追溯授权旧 proposal；
- proposal payload 上限 32 KiB、Decision 自由文本上限 2 KiB；大 Evidence/Artifact 只保存 ID/hash/size/type 引用；
- 未决 proposal 按 `(target, generation, status, expires_at, proposal_id)` 提供 seek/cursor 查询；不得以无界 offset 或无证据 GIN 支撑审批页；
- normal retention 不得删除 unresolved `unknown` 所需的 Decision/Intent/operation identity，也不得删除仍在审计保留期内的授权链。

### 8.9 插件平台控制事实（DB-PLUGIN-001）

- `plugin_revisions` 必须不可变，唯一身份至少包含 `plugin_id/revision/artifact digest/manifest digest/config digest/contract set`；
- qualification、activation、drain、disable 和 revoke 记录必须 append-only；current active binding 可以是可重建投影，但不能覆盖历史资格或审计事实；
- 同一 `(plugin_id, kind, scope)` 的 active generation 必须由唯一约束、事务和 CAS 保证至多一个；旧 generation result 不能覆盖新 binding；
- `plugin_platform` 只保存 catalog/lifecycle/security 控制事实和有界结果引用，不保存插件任意 payload、大附件、完整 prompt、provider 输出或第三方数据库副本；
- catalog/activation 表不得被插件 runner 扫描或 claim 为工作队列，不得包含能够派生 P4 effect 的可执行 payload；
- Analysis Task/Run/Artifact/Trace 继续由隔离 `analysis_plugin` schema 和角色拥有；其他首期插件默认无数据库凭据；
- plugin schema migration 必须独立 version/checksum、expand/contract、空库/升级/失败恢复可测，不得与核心 migration 共享应用启动抢跑；
- unresolved activation drift、revocation、qualification evidence 和安全审计在保留期内不得删除；PITR/restore 后必须重验 active binding 与已部署 artifact digest，不能仅凭恢复前内存状态继续调度。

### 8.9a 插件统计运行与投影事实（DB-PLUGIN-STAT-001）

- `plugin_statistic_schedules` 保存 Go-owned on-demand/periodic trigger policy、scope/data-class、exact definition ID/revision/digest、binding generation、input profile、interval/window、deadline、enabled/revision/digest 和 actor/policy audit；create/revise/disable 只追加 immutable revision，不原地改写历史。definition body 继续来自 `plugin_platform` 的 immutable manifest revision，不在本表复制成可漂移配置。它不是插件自有 timer、effect queue 或数据库扫描器。首期不得使用亚分钟高频 schedule；exact 最小 interval 由 profile 固定；
- `plugin_statistic_runs` append-only 保存幂等键、trigger/schedule revision、exact plugin binding、definition ID/revision/digest、scope/window/as-of/input digest、状态/attempt/deadline/error、started/completed time 和 trace；它是唯一 durable run ledger，只有 Go scheduler/请求处理器可创建，且只有 Go Control 内有界 statistics dispatcher 可按 CAS/fence 推进 attempt；effect dispatcher、Host 和插件不得 claim/renew；
- `plugin_statistic_artifacts` 保存经 schema/resource/security 校验的有界 `PluginStatisticsArtifactV1` 与 definition identity、result digest、quality/coverage/truncation/provenance；默认 30 天 retention。任意大附件只保存授权 reference，禁止 raw packet、secret、完整数据库副本、HTML/脚本或无界第三方 payload；
- `plugin_statistic_current` 是按 `(plugin_id, definition_id, definition_revision, definition_digest, scope_digest, binding_generation, window/profile)` 的可重建 current pointer，使用 version/expected-input CAS。只有同 definition/binding generation、未撤销、未过 freshness/expiry 且 validation succeeded 的 Artifact 可以成为 current；old-definition/old-generation late result 只追加审计；
- 同一 run idempotency key+same input/result digest 幂等返回原 run/artifact；same key 不同 input 或 result digest 稳定冲突并记录 invalid event。run status 与 Artifact quality 分列，不能把 `no_data/gap/stale/not_measurable` 塞入 succeeded/failed；
- Go 是唯一 writer；插件、Host、Analysis schema、Prometheus/Grafana/OTel 和 Web cache 不是统计事实源。统计 Artifact 不能更新核心 Event/Incident/rule/model/effect/qualification 表或触发 P4 mutation；
- migration/restore matrix 覆盖 empty/current/上一受支持 schema、重复/中断/checksum drift、run/current unique/CAS、old/new Go/Web reader、retention/legal hold、无损 failover、PITR/restore 后 binding/revocation/current 重验。恢复出的 fresh pointer不能自证插件仍 active；重验前标记 stale 且不自动重新执行 run。

### 8.10 在线模型控制事实（DB-MODEL-001）

- `model_control_incarnations` append-only 保存不可复用 identity、创建原因/时间、restore/timeline/backup manifest 引用和 supersedes；`model_revisions` 不可变，唯一身份至少包含 model/revision、bundle/manifest/raw-or-optimized artifact/scaler/config、feature-schema、label-taxonomy、output-adapter、inference-wire、optimization、runtime/resource/profile 和 qualification digest；同 identity 不同 digest 必须拒绝；
- qualification、rollout request、pool-generation startup/worker observation、per-shard route-withdraw/drain/readback/CAS/resume、old-pool drain、abort/rollback和audit必须append-only；per-shard desired/current/previous与group rollout是可重建projection，不能覆盖历史revision/evidence/operation；
- 同一`(model_control_incarnation_id,inference_scope,inference_shard)`最多一个finalized current，由unique constraint、短事务和CAS保证。只有`GetPoolStatus`证明exact pool envelope、logical pool/pool generation、availability profile、required worker/min-ready/capacity及HA适用的failure-domain/N+1、operation、expected current、proposed binding、wire/optimization全部匹配，且Edge已对该shard提交同route epoch的`route_withdrawn/drained`观察后，Go才能逐shardfinalize并原子保留previous；worker/pool`loaded/ready`本身不改变current；
- rollout operation冻结ordered shard set、expected binding/routing vector、logical pool generation和incarnation；`model_pool_observations`以`(incarnation,logical_pool_id,pool_generation,worker_runtime_id)`唯一标识worker startup/readback/failure-domain，`model_shard_observations`以`(incarnation,scope,shard,route_epoch,proposed_binding_generation,logical_pool_id,pool_generation)`唯一标识withdraw/drain/CAS/commit/resume。旧incarnation/pool/worker/route、集合外shard、旧envelope/readback或expected-current漂移被fence；
- 同一shard的`rollout|rollback|recover_current`只能一个durable operation/lease推进；自动恢复只可恢复exact current pool generation，不自行改变revision/backend或生成隐式rollback。首期shard切换默认串行。operation状态至少为`requested|staging|pool_starting|pool_qualified|rolling|draining_previous|completed|rollback_requested|pool_starting_or_reuse|rolling_back|recovery_requested|pool_recovering|failed|aborted`；per-shard observation表达`pending|route_withdrawn|draining|drained|current|resume_pending|resumed|unavailable|quarantined|rollback_ready`；terminal不可重开且只有未route withdraw可aborted；
- 部分 shard 已 finalize、后续 shard 失败时，不伪造全组回滚或单一 current；operation 以 exact updated/pending/failed vector 终止为 `failed`，group projection 为 `rollout_failed_mixed`。显式 rollback 创建引用各 shard exact current/previous 的新 durable operation，并在每个 shard 外部 restart/CAS 前重新读取 qualification、revocation、artifact availability 与 compatibility digest；
- canonical Event/Inference fact 保存实际 shard、model/binding generation、feature/label/adapter/runtime/profile digest。跨 model generation 的窗口、cursor 或 rollup 不得混合；离线/rehearsal comparison evidence 不写核心 model binding/Event/effect queue；
- Go是核心model facts唯一写入者；生产Gateway/Triton/backend/Edge/plugin/Offline ML不得取得核心数据库凭据。deny-role fixture证明非Go身份无法写Event/effect/model binding；
- 无损PostgreSQL failover保持model-control incarnation并从per-shard binding/pool readback/Edge route handshake重验；PITR/restore先轮换incarnation，再以new operation/new pool+binding generation逐shardreadback/CAS/commit。旧envelope/action/worker readback/Edge handshake和同数字generation全部fenced。model rollout不得在数据库事务中拉取/加载artifact、启停pool或等待Central RPC/readiness。

### 8.11 Redis 边界（DB-REDIS-001）

首期不得把 Redis 加入核心依赖图。未来只有在实测证明需要时，才可以用于：

- 多 Go 实例的 SSE/WebSocket fanout；
- 跨实例短 TTL rate limit；
- 可丢失、可从 PostgreSQL 重建的只读 dashboard cache。

Redis 禁止保存：

- Event/Incident 核心事实；
- source cursor；
- effect intent/outbox；
- P4 operation/result；
- idempotency canonical result；
- runtime truth；
- plugin revision/qualification/active binding/revocation；
- model revision/qualification/rollout operation、per-shard desired/current/previous binding 或 startup/readback observation；
- Agent Task/Artifact 唯一副本；
- 分布式锁或任何决定 `applied/unknown/HOLD` 的状态。

Redis 全部丢失时，系统正确性必须不受影响。

### 8.12 规则观测事实与保留（DB-RULE-001）

- 核心 schema 增加精简的 `rule_observation_epochs`、`rule_observation_rollups` 和 `rule_observation_events` 逻辑域：epoch 保存 immutable rule/counter/generation/profile identity，rollup 保存有界窗口增量与质量，event append-only 保存 install/readback mismatch、reset/wrap/saturation、gap、stale、expiry/supersede 和 outcome evidence 引用；Go Control 是唯一业务写入者；
- 当前规则表现是可重建 projection，不得覆盖 Effect/Intent/Attempt/readback 或历史 observation epoch；Prometheus、Grafana、Edge 内存、日志和 SSE 都不是规则事实源。outcome 仅保存 `Evidence`/packet-oracle 的 bounded ID/digest/window/result 引用，不复制 PCAP/raw packet；
- 热查询字段至少包括 target、table、generation、rule/effect/operation identity、status/quality、window start/end、packet/byte delta、eligible denominator、reset epoch、last observed-hit window、TTL/expiry 和 profile digest；不得把这些字段只放 JSONB，也不得为 arbitrary match/payload 建无界 GIN；
- latest projection 使用 `(target, generation, table, canonical_entry_digest)` 唯一键和 version/CAS；rollup 按时间分区，以稳定 cursor 查询。duplicate same identity+digest 幂等吸收；同 sequence/identity 不同 digest、跨 generation 合并和负 delta 必须拒绝并追加 invalid event；
- 初始 retention 与粒度按 `PERF-RULE-001`：latest/identity 与对应 effect 审计同寿命，5 分钟 per-rule rollup 保留 7 天，1 小时 rollup 保留 90 天，status/reset/outcome event 按 effect audit 保留；legal/diagnostic hold 可 pin。生产容量证据不足时 profile 可以收紧保留，放宽必须经过分区、索引、WAL、PITR/restore 和查询 benchmark；
- rollup/retention job 只能从 PostgreSQL canonical rows 幂等生成，使用有界 partition/batch/timeout；不得在事务中读取 P4，也不得删除 unresolved effect `unknown`、当前 epoch identity 或仍被审计引用的 outcome evidence。

### 8.13 无状态防火墙控制事实（DB-FW-001）

- 核心 schema 必须以精简强类型逻辑域保存 `firewall_policy_revisions`、normalized rules/default、compile/overlap/capacity result、target bindings、activation operations/stages、bank/selector observations、logical-to-physical entry mapping 和审计引用；Go 是唯一业务写入者，Edge/P4/Web/插件无数据库凭据；
- revision 与 compile plan immutable；任何规则、顺序、priority、default、scope、profile、P4Info 或 capacity input 改变都创建新 revision/plan digest。current/previous/desired 是可重建 projection，不能覆盖历史 revision、Decision、Intent、journal/readback 或 outcome evidence；
- baseline activation 复用 `effect_proposals/effect_decisions/effect_intents`，通过 `proposal_kind`/typed payload 与稳定 contract 区分，不建立 `firewall_jobs`、第二 outbox 或可 claim policy table。dispatcher 仍只能 claim `effect_intents`；
- 同一 `(target, application_generation, firewall_profile)` 至多一个 current policy binding和一个 active bank，由唯一约束、version predicate 与 CAS 保证。只有 Edge 返回 exact inactive-entry set、selector readback、operation/claim generation/P4Info/plan digest 全部匹配后才能 finalize current；late/old bank result 只追加审计，不覆盖 current；
- PITR/restore/clone/rewind 或 P4 restart 后必须从 Edge current pipeline/selector/entry readback 重建可信 observation，提升 application generation 并重新验证 current binding；数据库恢复出的 selector/bank 字段不能自行授权写入或声称设备 current；
- policy revision、activation audit、logical-to-physical mapping 与对应 effect/rule evidence 同寿命；previous bank payload只在 rollback/audit需要的有界窗口保留，cleanup 不删除仍被 unresolved `unknown/reconciling`、legal hold 或 outcome evidence 引用的事实。
- firewall schema migration 必须使用 immutable version/checksum，并在隔离 test database 覆盖 empty→current、上一受支持 schema→current、重复执行、partial/interrupted、checksum drift、expand/contract rollback、并发 activation 被拒绝/排空、无损 failover 与 PITR/restore。每个 case 固定 old/new fixture 和预期约束/current-previous/operation/audit 引用；migration/restore 不调用 P4、不翻 selector，也不能把恢复出的 binding 直接标为 device current。

### 8.14 Target Registry 与 Fleet Operation 事实（DB-TARGET-FLEET-001）

- `target_control_incarnations` append-only 保存不可复用 identity、创建原因/时间、restore/timeline/backup manifest 与 supersedes；`managed_targets` 保存 stable non-reused `target_id`、desired identity/profile/scope/lifecycle/revision/provenance。active `(p4_endpoint_ref,device_id,role)`、target assignment 和 retired identity必须由 partial unique constraint/等价强约束保证无冲突；
- `target_assignments` append-only 保存 target→Edge、assignment generation、actor runtime epoch、desired/accepted profile/capability、不可复用 lease identity、authenticated grant digest/最大TTL/撤销事实、durable election allocation floor/range、freshness、drain/revoke和审计；current assignment是可重建projection，只有一个active。Edge本地monotonic lease deadline及其observation只能通过Go API进入，不能直接写表、续租或把heartbeat当current；
- `target_capability_observations` append-only、大小和保留有界，保存 arbitration、pipeline/P4Info/capability、readiness/freshness/drift与短evidence reference；高频原始gNMI update、P4 counter或日志不逐条进入此表。current observation使用version/CAS，旧incarnation/assignment/actor epoch结果只审计；
- `fleet_operations` 保存不可claim的parent；`fleet_operation_targets`保存冻结ordered target/wave、child `effect_intent_id`、expected target/application generation、P4Info/current/previous、result/reason/readback reference。dispatcher唯一claim source仍是`effect_intents`；数据库权限/查询测试必须证明parent/target rows没有lease/claim语义；
- approve事务必须原子写Decision、parent和bounded child intents；future wave child使用durable gate predicate不可提前claim。same `(fleet_operation_id,target_id,effect_digest)`+same request digest幂等，不同digest冲突。parent状态只由同一incarnation、完整target vector确定性重建；不得删除失败/unknown child或以聚合数覆盖真实结果；
- target/fleet热查询使用强类型列与stable cursor；identity、scope、site、lifecycle、assignment、profile、freshness、parent/child status、wave、deadline和reason不得仅放JSONB。大capability/diagnostic只保存有界reference，不为任意外部inventory字段建立无界GIN；
- normal retention不得删除active/retired identity防重用证据、current assignment、unresolved fleet child、effect unknown reconcile或审计引用。PITR后先轮换target-control incarnation，再逐targetread-only reconcile；未完成的old-incarnation parent/child保持fenced/auditable，不自动恢复claim；
- migration matrix必须覆盖empty/current/previous schema、duplicate target/device endpoint、assignment race、parent-child原子性、wave gate、partial/unknown vector、无损failover、PITR/clone/rewind incarnation轮换和old/new Go/Edge/Web reader；migration/restore不得连接P4、启动Edgeactor、打开wave或把恢复数据标为device current。

## 9. 性能需求

### 9.1 性能目标原则（PERF-001）

“极致性能”必须通过固定环境和可复核 benchmark 定义，不能用语言选择或一次 p95 结果代替证据。

每个可用于门禁的 benchmark 必须引用机器可读 `performance-environment/v1` digest。该 profile 至少固定 CPU 型号/微码/频率策略/核与 NUMA、RAM、存储与 fsync、NIC、P4 target、内核、容器/cgroup、编译器/生成器/runtime、模块镜像/config/model/contract digest，以及 packet/Event/window/batch/target/concurrency 的 steady、peak、saturation 和 soak workload。没有 profile、原始结果或统计方法时只能为 `NOT RUN`；环境不匹配时只能比较趋势，不能判定 PASS。

热路径必须追求：

- 无逐记录 JSON；
- 无逐记录 HTTP；
- 无逐记录 PostgreSQL transaction；
- 批量读取、批量推理和批量写入；
- 预分配 buffer 和受控内存复用；
- 有界批量 binary RPC、连接复用和预分配 buffer；
- group write/group fsync；
- 最少数据复制；
- 无 LLM/MCP/A2A；
- 无数据库或网络等待期间的锁持有。

### 9.2 必须采集的指标（PERF-002）

每个 module benchmark 至少报告：

- throughput；
- p50/p95/p99/max latency；
- error/drop/retry rate；
- CPU 与 CPU/record；
- RSS/heap/allocation；
- bytes copied/record；
- WAL bytes/record 和 fsync latency；
- queue depth/backpressure duration；
- file descriptor/thread/goroutine 数；
- PostgreSQL connection、pool wait、transaction、lock 和 WAL；
- 测试环境、CPU、内存、存储、内核、编译器、依赖、模型和配置 hash。
- 每段 queue capacity/high/critical watermark、queue age、throttle/reject/drop reason，以及上游实际收到的 backpressure/credit 状态；
- 重复次数、warm-up、测量窗口、置信区间或误差范围、异常值规则和 `performance-environment/v1` digest。
- Frontend production bundle/route chunk、Core Web Vitals、route navigation、API wait/render、long task、heap/DOM/chart/query-cache、HTTP/SSE 并发和 browser/profile digest。
- 规则观测的 P4 Read RPC/QPS/bytes、entities/batch、full-sweep/freshness、queue/gap/reset/invalid coverage、per-rule rollup rows/WAL/index、Rule API/UI latency/points 和关闭基线差异。
- 插件统计的 schedule/run/admission/queue/execute/validate/DB-project/API/render 分段时延、input/artifact/series/points/rows/text bytes、quality/truncation、SQL rows/WAL/index、Web heap/DOM/long-task，以及统计插件关闭基线差异；
- 测试流量发生/回放的 input packet/byte/duration、captured/original length 与截断数、requested/sender/test-ingress/DUT-observed PPS/bps、scheduler drift/lateness、send/drop/error/retry、direction/rewrite/cache/offload/qdisc、CPU/RSS/I/O，以及 exact fixture/tool/config/output digest；无法观测的层必须标为 `not_measurable`，不得用相邻层估算冒充。
- 在线模型的 repository/artifact verification、backend/Session-create、warmup、Gateway↔Triton probe、startup-to-min-ready分段，Triton dynamic batch/instance group/queue、Edge↔Gateway RTT/bytes/retry、Gateway↔Triton和host↔device actual copy、per-shard route-withdraw/drain/unavailable/buffer/gap/replay、deployment action/termination、pool/availability profile及HA适用的failure-domain/N+1、readback/CAS/commit/resume/restart/quarantine/rollback、group mixed duration、numeric/label/adapter/wire/optimization reject、model incarnation/route/pool/binding generation fence，以及滚动相对steady基线的throughput/p99/CPU/RSS/VRAM/network差异。

### 9.3 初始相对门槛（PERF-003）

在相同硬件、负载、数据和功能合同下，候选实现应至少满足：

- 目标热路径吞吐提升或 CPU/record 降低至少 10%；
- p95/p99 不得回退超过 5%；
- RSS 不得增加超过 10%；
- 业务 identity、hash、decision 和最终 P4 readback 契约等价。

若模块重写主要目标是安全、隔离或可维护性而未达到性能提升，必须单独说明收益和是否接受。最终绝对 SLO 按照已确认的 `DEC-001`，先建立现有系统基线，再结合目标硬件与负载冻结。上述相对门槛只能证明相对回归/收益，不能单独形成模型容量、滚动可用性或 production PASS。

### 9.3a 在线模型资源与切换预算（PERF-INF-001）

本节公式的量纲和测量边界固定如下，profile 不得自行重解释：`λ_s,peak` 是 shard `s` 在冻结 workload/window 口径下的峰值入站 records/s，`λ_assigned,peak` 是本次将路由到新 generation 的 shard 峰值 records/s 之和，`λ_peak` 是整个 logical pool 的已准入峰值 records/s；对应 byte rate 使用同名 `*_bytes` 字段。`C_remaining`、`C_new`、`C_s` 分别是最大故障单元丢失后剩余 pool、新 generation 和 shard 分配的实测可持续 records/s，必须在 exact model/runtime/batch/network/竞争负载下同时满足冻结 p99/error SLO，不能由core数、GPU kernel或单请求benchmark推算。`H` 是无量纲headroom且`H >= 1`。`T_unavailable,s` 从Edge开始撤销shard canonical route起，到收到exact committed-binding handshake ACK并恢复该route为止；`T_replay,s` 从恢复route起，到该shard backlog回到profile冻结的稳态watermark为止。`B_s,records/B_s,bytes` 是同一现有WAL域内可用于该shard的硬容量，不含另一个隐式队列。

- 每个`model-runtime-central-cpu/v1|model-runtime-central-cuda/v1`必须为每种model/backend固定repository/artifact最大bytes、optimization artifact/tool/options/ORT-EP与inference wire；CPU profile另固定architecture/feature/thread/affinity/NUMA/arena/RAM，CUDA profile另固定CUDA/cuDNN/driver/GPU-architecture/VRAM/pinned/device/stream。两者都固定verification/load/backend-Session/warmup/Gateway-probe分阶段deadline、Triton dynamic batch/instance group/queue、Gateway/Edge in-flight、input/output bytes、network、cache和replica上限；禁止接受默认漂移、从模型shape推导无界allocation或在artifact/backend/hardware不兼容时静默改选profile；
- 用于release gate的model/runtime/rollout/environment组合还必须写入最低steady/peak/rolling throughput、最大端到端active p99、cold/warm startup-to-min-ready、drain、per-shard unavailable、buffer age/depth/overflow、readback/CAS/resume、全组mixed-rollout、rollback deadline与soak时长绝对值；声明HA时还必须写入N+1、single-replica/failure-domain loss。CPU与CUDA分别冻结，目标硬件/网络/模型/workload digest不匹配时不得判PASS。`DEC-001`未完成只允许趋势/rehearsal，不允许以相对5%门槛代替绝对容量资格；
- `availability-single/v1`表示恰好一个failure domain，可按容量在该域内部署1..N个同一exact profile副本；它必须固定`required_replicas/min_ready_replicas/max_unavailable`和中断语义，但域内副本不能抵御整域丢失，因此不得声称HA。`availability-ha/v1`用于production HA，至少跨两个独立failure domain，且失去profile定义的一个最大故障单元后仍满足`C_remaining >= H × λ_peak`。`C_remaining`必须由相同模型、runtime profile、batch/latency SLO、网络和竞争负载实测，不能把CPU core或GPU kernel throughput简单相加推定。HA profile采用static N+1，不启用scale-to-zero或依赖自动扩容及时救场；
- rollout同时计入current active、new warming、old draining和Edge replay：切换任一shard前，必须证明新generation的`C_new >= H × λ_assigned,peak`，remaining current generation仍承载未切shard，CPU/RAM或GPU/VRAM及network均不超硬上限；每个shard同时满足`B_s,records >= λ_s,peak × T_unavailable,s`、`B_s,bytes >= λ_s,peak_bytes × T_unavailable,s`、`C_s > λ_s,peak`，并实测backlog在`T_replay,s`门槛内回到稳态watermark。公式左右必须使用相同时间单位并记录测量误差/峰值窗口。若资源不足以同时保持两generation资格容量，rollout必须HOLD，不得通过未声明CPU/CUDA或旧模型fallback完成；
- Edge 为每个 restarting shard 在既有 WAL/checkpoint 域使用独立有界 pending sequence，profile 固定最大 window/bytes/age、high/critical watermark、overflow/drop/gap range、resume watermark 和恢复顺序。buffer age 在同 runtime epoch 使用 monotonic `delivery_time-window_completed_time`；跨 Edge epoch/时钟无法证明时 fail closed 为 stale/HOLD。超限必须 backpressure/显式 gap/HOLD，不得无界等待、跨 generation 拼接或把缺测补零；
- pool/replica restart/recovery profile必须冻结max attempts、指数退避、总deadline、circuit/quarantine和解除权限；adapter不能在Go已quarantine后无限重启。预算耗尽只隔离该replica/pool并告警，不改变current revision；当remaining容量不足时availability为HOLD/unavailable并触发Edge背压/gap，不切换backend；
- benchmark对CPU/CUDA分别覆盖cold/warm startup、steady active、最小/典型/最大dynamic batch与queue、repository/cache hit/miss、网络抖动、全池unavailable、drain/readback/CAS/resume、partial rollout、rolling rollback、CPU/RAM或GPU/VRAM/network saturation和3,600 秒 soak；HA profile另覆盖single replica/failure-domain loss。报告throughput/latency、CPU/RSS、适用的VRAM/GPU utilization、allocation/copy、queue/buffer、retry/dedupe、load/cache、numeric error和fence/drop reason；
- 首期默认每次只切换1个inference shard；只有current/new pool容量、Edge buffer和故障域证据通过的新profile才提高并发。rolling时核心p99/RSS/VRAM/network、可用容量和buffer age必须满足冻结门槛；性能不达标只能停止后续shard并`HOLD/failed`，不能跳过qualification/readback/CAS/fence或启用fallback；
- Triton dynamic batching和instance group必须通过Model Analyzer或等价可重现benchmark搜索，但最终参数进入项目profile而非运行时自动漂移。增加instance count若只增加延迟/显存不得采用；Kubernetes GPU extended resource、node label/topology spread/PDB只实现placement/自愿中断约束，不证明N+1、rollout或应用可用性。

### 9.4 用户可感知目标（PERF-004）

以下作为初始目标，必须在目标硬件确认后冻结为 release gate：

- 首页/近期状态读 API p95 目标不高于 100 ms；
- 24 小时趋势 API p95 目标不高于 250 ms；
- Agent 分析 p95 在 8 秒内完成或给出明确降级；
- Agent graph hard deadline 30 秒；
- Event 从 Control 接收成功到可查询应在一次前端刷新周期内可见；
- Edge→Central Inference→Control的端到端吞吐和p99目标按CPU/CUDA profile分别依照`DEC-001`基线流程冻结。
- Frontend 必须满足 `WEB-PERF-001` 的 production bundle 与 Core Web Vitals 门槛；API p95 与浏览器渲染/交互耗时分别度量，禁止用快 API 掩盖慢渲染，或用缓存旧数据伪造快加载。

### 9.5 Effect 治理性能与资源预算（PERF-GOV-001）

- Proposal/Decision 是低频控制面，不得进入 telemetry、inference、Event ingest 或 P4 write/readback 热路径；
- create/approve/reject 请求不得同步调用 LLM、MCP、A2A、Edge 或 P4；成功响应以 PostgreSQL 中的 canonical fact/intent durable 为边界，设备结果通过 operation 查询或 SSE 收敛；
- 在 `DEC-001` 的初始目标环境，proposal/decision mutation p95 目标不高于 100 ms、p99 不高于 250 ms，PostgreSQL pool acquire p95 目标不高于 10 ms；最终随硬件基线冻结为 release gate；
- 治理列表默认 50 条、单页最多 200 条，必须使用稳定 cursor；禁止无界导出、offset 深翻页和逐行 N+1 查询；
- 每个 `(target, generation)` 最多同时存在 128 个 unresolved proposal；部署还必须设置全局 unresolved/backlog 硬上限。达到上限时拒绝新 proposal 并告警，不得驱逐已批准、执行中或审计保留期内的事实；
- 必须以并发 2/8/32/128 测量同 proposal 冲突与不同 proposal 吞吐，报告 lock wait、serialization retry、deadlock、connection wait、WAL 和 idempotency conflict；
- 在最大治理并发与最大 Event/effect 负载叠加时，核心检测/处置 p99 相对无治理负载不得回退超过 5%，进程 RSS 不得增加超过 10%，且不得突破连接预算；
- 审批页所需数据应由有界聚合 API 一次或少量固定查询返回；任何缓存只能是 PostgreSQL 可重建的只读投影，缓存 miss 不得改变授权结果。

### 9.6 插件平台性能与资源预算（PERF-PLUGIN-001）

- 首期不得在 packet、telemetry record、InferenceResult、Event ingest、effect claim/P4 write/readback 的同步路径调用插件；插件只消费已形成的 bounded batch/window/task 或 shadow copy；
- service plugin 每个方法必须固定 max request/response bytes、batch records、in-flight bytes、队列、并发、queue/execution/total deadline、retryable status 和最大尝试；达到上限时只能显式 backpressure、reject 或返回 kind contract 声明的 typed error/受限结果，不得静默改用另一插件、runtime 或未声明路径；
- Wasm component 必须预验证并可缓存预编译制品；每次调用固定 linear memory、table/instance/stack、fuel 主预算、epoch/wall total deadline 和输出上限；cold compile/instantiate 与 warm execute 必须分开测量；
- Plugin Manager/Host 不得为每条 Event 建立独立 HTTP/JSON/RPC、进程或 Wasm instance；相同 revision 的安全实例池与只读编译缓存必须有界复用；
- 必须以并发 1/2/4/8/32、最小/典型/最大 batch、queue saturation、插件 crash/restart 和 Host restart 分别 benchmark `service-grpc/v1`、`wasm-component/v1` 与 `a2a-agent/v1`；
- 至少报告 admission/queue/execute/end-to-end p50/p95/p99/max、throughput、reject/timeout/retry/circuit-open、CPU、RSS、allocation/copy bytes、RPC bytes、Wasm fuel、instance count、FD/thread/task 和 restart count；
- 最大插件负载或插件故障风暴叠加核心峰值时，核心检测/处置 p99 相对插件全部禁用基线不得回退超过 5%，核心进程 RSS 不得增加超过 10%，且不能突破连接/FD/线程预算；
- 插件性能不达标只能禁用或保持 `HOLD`，不能通过放宽输入、队列、超时、网络或 effect 边界取得表面吞吐。

### 9.6a 插件统计性能与资源预算（PERF-PLUGIN-STAT-001）

- 统计计算是低频旁路，不得进入 packet、telemetry/window、InferenceResult、Event ingest、effect/P4、rule observation sweep 或核心 API 的同步依赖；只消费 Go 已形成的有界投影/冻结 bundle。禁止把全量 raw Event/packet 推给插件后再依赖插件分页或过滤；
- 初始 absolute profile 采用 `CONTRACT-PLUGIN-STAT-001` 的每 plugin revision 32 definitions/128 KiB definition block/每 definition 64 field refs/1 external-source capability ref、2 MiB input、1 MiB Artifact、每 Artifact 32 metrics、64 series、10,000 points、每 histogram point 64 buckets、2,000 rows、200 evidence refs、64 KiB total text、4 KiB scalar string、depth 8、in-flight 2、queue 32、deadline 10 秒；每个 schedule 的最小 interval、每 scope on-demand rate 和全局并发必须在 `performance-environment/v1` 冻结。未冻结或只依赖 worker 数量/平均时延时为 `HOLD/NOT RUN`；
- benchmark 至少覆盖 pure-transform 与获准 read-only-tool 的 empty/min/typical/max bundle、1/8/32 definitions、1/64 series、zero/no-data/gap/reset、max points/rows/text、queue saturation、timeout/cancel/crash/revoke、DB/API/SSE 慢和 3,600 秒 soak；分别报告 schedule wait、freeze/query、dispatch、Host/RPC、execute、schema/security validate、transaction/CAS、API、browser render；
- statistics list/current API 在目标 profile p95 ≤100 ms，24 小时/30 天 bounded history p95 ≤250 ms；Web 单 view 继续满足 `WEB-PERF-001` 的 INP/heap/DOM/points 门槛。达不到时只能由 Go 聚合、分页、降采样或收紧 profile，不得把无界序列交给浏览器、增加第二 cache fact source 或执行插件自带代码；
- 最大统计插件负载、crash/restart/revoke storm 叠加核心峰值时，核心检测/处置及原生 Rule Effectiveness p99 相对统计插件全部禁用基线不得回退超过 5%，核心进程 RSS 不得增加超过 10%，且不突破 PostgreSQL connection/WAL、Host queue、FD/thread/task、HTTP/SSE 和 Web heap budget；
- 统计性能不达标只使相应 binding/definition `HOLD/unavailable`；不得降低核心数据质量、延长 effect deadline、跳过 result validation、开放高基数 Prometheus series 或将旧 Artifact 伪装为 fresh。

### 9.7 规则观测性能与资源预算（PERF-RULE-001）

- 规则观测是低优先级异步控制面，不进入 packet、telemetry window、inference、Event ingest、effect claim/write 或 exact effect readback 的同步路径；effect readback、mastership 和 telemetry source 连续性始终高于统计轮询；
- `p4-rule-observation/v1` 初始 profile：每 target 最多 4,096 个 active observable entry；每个 P4Runtime Read 请求最多 256 个 exact direct-counter entity、响应最多 1 MiB、deadline 2 秒、每 target 同时最多 1 个 rule-observation Read；调度器每 15 秒最多执行 4 个 batch，并在 60 秒内完成一次全量 sweep。不得重叠未完成 sweep；超时/积压时降速并标记 gap/stale，不扩大并发或阻塞 effect；
- 单 entry freshness 超过 75 秒为 stale；恢复后必须从新 cumulative baseline 开启有效窗口，不能把断档前后直接相减。目标能力、规则容量或 P4Runtime 延迟不能满足上述 profile 时，该 observation 显示 `not_measurable`，对应 target profile 的资格状态为 `HOLD`；只能以新的 target profile 和 benchmark 调整，不得采样一部分却显示 100% coverage；
- Edge→Go observation batch 最多 256 条、1 MiB；per target bounded queue 最多 8 batch。队列满时不得阻塞 P4 写/readback，也不得静默丢弃：记录 exact sequence gap、丢弃低优先级未持久 sample 并使受影响窗口 invalid/stale；恢复后的第一条只建立 baseline；
- Go 只维护 current projection、append-only status event 和 5 分钟 rollup；列表默认 50/最多 200，Top-N 默认/最多 20，单图/单页点数继续受 `WEB-PERF-001` 约束。current/list API p95 目标 ≤100 ms，24 小时/7 天趋势 p95 目标 ≤250 ms；不得用逐规则 P4/Prometheus 查询、N+1 SQL 或浏览器全量计算达成页面；
- benchmark 至少覆盖 0/128/1,024/4,096 rules、1/最大 target、零命中/热点/均匀/重置/过期、正常/2 秒 timeout、队列 saturation 和 3,600 秒 soak；报告 P4 Read RPC/QPS/bytes/error、full-sweep/freshness、Edge CPU/RSS/allocation/queue、Go ingest/SQL rows/WAL/index、API latency、Frontend render 和 gap/stale coverage；
- 4,096-rule 最大观测负载叠加核心峰值时，相对规则观测关闭基线，核心检测和 effect write/readback p99 均不得回退超过 5%，Edge/Go RSS 各不得增加超过 10%，不得突破 P4 RPC、PostgreSQL connection/WAL、SSE/HTTP、FD/thread/task 预算；未通过只能降低 profile 容量/频率或暂时 `not_measurable`，不能牺牲唯一 writer、readback、generation fence 或数据库耐久性。

### 9.7a BMv2 无状态防火墙性能与容量（PERF-P4-FW-001）

- firewall packet path 不进行 P4Runtime、digest、PacketIn、host firewall、Go/RPC/数据库或插件调用；每包固定为 bounded parser + response exact lookup + selector/baseline lookup + direct/eligible counter + forwarding/drop。任何新增 table/action/parser branch 必须用 forwarding-only 与 firewall-disabled 等价 P4 profile作关闭基线；
- benchmark 矩阵至少覆盖 0/128/1,024/4,096 active logical rules、response-only/baseline-only/mixed、exact/wildcard-prefix、permit/drop/default、uniform/hotspot/miss、fragment/malformed、direct/eligible counter on，以及 baseline active+inactive 双 bank physical capacity。报告 logical/physical/counter units、compile/prepare/write/readback/selector/rollback/cleanup duration；
- 数据面分别记录 sender attempted/accepted、test ingress、BMv2 ingress/egress/drop、achieved pps/bps、p50/p95/p99/max latency、packet loss、BMv2/runner CPU/RSS/core/cgroup/queue、每 stage/table hit 与 counter。requested PPS、host capture 或 counter hit不能代替 DUT observation；
- 控制面分别记录 policy rules/bytes、compile/preflight latency、P4 Write/Read RPC/entity/bytes/error、journal/fsync、unknown reconcile、Edge/Go CPU/RSS/queue、PostgreSQL rows/WAL/index/API/UI。inactive preload 不得阻塞 mastership、incident response write/readback、telemetry snapshot 或 rule observation超过其 deadline；
- `4,096 active observable` 与双 bank `2×physical` 是待资格化的首期最大矩阵，不是 BMv2 或硬件承诺。每个 target profile必须冻结最大 active response/baseline logical rules、每 bank/total physical entries、counter/selector resources和绝对 achieved pps/p99/activation deadline；这些绝对门槛未按`DEC-001`冻结或未实测时只能`HOLD/NOT RUN`，不得用相对回归或 legacy 1,024-entry 表推定 PASS；
- 24 小时 steady/peak/saturation/activation-loop soak 必须证明无 entry/counter leak、bank混淆、selector drift、RSS/FD/queue无界增长或持续性能衰退。BMv2证据只对 exact software environment有效；hardware profile重新资格化 TCAM/SRAM/priority、atomicity、counter、pipeline latency 与 line-rate。

### 9.7b 多 Target/Fleet 性能与公平预算（PERF-TARGET-FLEET-001）

- `p4-target-fleet/v1` 必须冻结 `max_targets_per_edge/max_targets_per_control/max_targets_per_fleet_operation/max_targets_per_wave/max_parallel_children`，以及每 target/global P4 RPC、StreamChannel message、FD/task/thread、journal/WAL/disk、queue、memory、Go/DB connection/row/WAL、API page/SSE/UI matrix 的绝对上限；任一值或目标硬件未冻结时只能 `HOLD/NOT RUN`；
- benchmark 至少覆盖 0/1/2/N targets、每 target 0/128/1,024/4,096 rules、同 profile/混合可拒绝 profile、全部健康/一个 slow/unreachable/restarting/drift、并发 telemetry/effect/readback/observation、single large fleet activation、多个互不相交 operation 与 3,600 秒 soak；单 target结果不得线性外推N target；
- Edge 分段报告 supervisor/actor startup、arbitration、P4 snapshot/effect/readback/observation latency，per-target/global queue age、scheduler wait、CPU/RSS/allocation、FD/task、journal/fsync、reconnect/backoff和fairness。一个target timeout/满队列时，其他健康target的source/effect p99相对同规模无故障基线回退不得超过已冻结阈值，且不能越过其deadline；默认阈值未冻结时不得用通用5%推定PASS；
- Fleet control面报告 target-set canonicalization/preflight、Decision+parent+child transaction、wave gate、claim/finalize、parent projection、API/UI render 的 p50/p95/p99/max、rows/WAL/locks/connections/bytes；必须证明事务和payload在最大 target set下仍有界，且任何 P4/gNMI等待期间数据库active transaction/held connection为零；
- priority/fairness 必须证明 effect journal/readback与mastership不会被full-sweep/gNMI subscription/fleet compile压制；read-only gNMI queue满只丢/合并低优先级观测并标gap/stale，不阻塞P4Runtime。提高 target/wave 并发只能创建新profile并重跑资源、故障和恢复矩阵；
- UI/API列表默认50、最多200 target；大fleet矩阵只返回有界summary+cursor child pages，SSE只发送invalidation/小型聚合。浏览器不得因N target建立N个polling timer/SSE/P4连接；Web heap/DOM/chart预算继续遵守`WEB-PERF-001`。

### 9.8 流量回放性能与资源预算（PERF-TRAFFIC-001）

- 每次 replay/traffic run 必须分别报告 requested rate、sender attempted/accepted packets 与 wire bytes、sender error/drop、test ingress observed、DUT ingress/egress/drop、duration、实际平均/窗口 pps/bps、inter-packet-gap/burst 分布和 packet loss；requested `--pps/--mbps/topspeed`、进程退出 0 或 sender accepted 均不能作为 BMv2/target 实际吞吐；
- `recorded-timing`、`multiplier`、`fixed-pps`、`fixed-mbps` 与 `topspeed` 分开 benchmark；报告 warm-up、preload/disk/backend、loop、capture timestamp resolution、timer/scheduler error 和 achieved/requested deviation。只有冻结误差容限与观测点的 rate profile 可以 PASS；未达 requested rate 必须显示 generator-limited、host-limited、link/qdisc-limited 或 DUT-limited，无法归因时为 `HOLD`；
- runner/Mininet/BMv2/流量发生器分别记录 CPU/core/NUMA、RSS、allocation、FD/thread/process、cgroup throttle/OOM、veth/NIC/qdisc queue/drop/overlimit、softnet/drop、BMv2 CPU/queue 与 capture loss；测试 artifact、preload memory、PCAP rewrite/temp 和日志必须有 byte/file/inode/retention 硬上限；
- initial software profile 必须冻结最大 input 4 GiB、最多 10,000,000 packets、单 run 15 分钟、loop 最多 100、并发 run 1、preload 最大 1 GiB；任一更小的 CI profile可以收紧。放宽只能创建新 `p4-traffic-replay/v1` revision并证明磁盘/RSS/CPU/超时/清理边界，不能由命令行绕过；
- 功能/结果 oracle 使用低到中速、误差可控的 deterministic profile；性能 ceiling 使用独立 profile和重复测量，不能让发包压垮 capture/oracle 后仍声称功能 PASS。BMv2/Mininet 共享单机 CPU/内核，结果只资格化 exact software environment，不外推硬件 line rate、真实 NIC 或生产容量（依据：[Mininet overview/limitations](https://mininet.org/overview/)）；
- 真实硬件 throughput/latency/flow-scale 只有在独立 hardware target、NIC/driver/offload/CPU isolation、发生器端口校准、双端计数和 owner 冻结绝对门槛后才可 PASS。若软件发生器无法驱动目标，可按 ADR-0012 条件资格化 TRex/等价 stateful/stateless generator；它不进入首期 BMv2 路径，也不取得 P4 控制权。

### 9.9 在线遥测与中央推理热路径预算（PERF-TEL-INF-001）

- 每个`telemetry-p4-window/v1`+`inference-central-grpc-batch/v1`+runtime profile+model+target+environment组合必须冻结packet/target/window/flow/feature规模、P4 Read实体/批/bytes/deadline/QPS、window duration/lateness、Edge network-batch records/bytes/in-flight、Triton dynamic-batch records/queue delay/instance group、per-source quota、retry、Gateway/Triton/runtime queue、Edge→Go batch/queue/WAL/fsync和PostgreSQL ingest硬上限；任一数值仍为TBD或依赖默认值时为`HOLD/NOT RUN`；
- benchmark必须从target observation point到PostgreSQL canonical Event端到端测量，并分段报告P4 snapshot/read、Edge decode/window/finalize/input WAL、network batch/serialize/mTLS/RTT、Gateway admission、Triton dynamic queue/selected ORT CPU或CUDA backend、适用的host-device copy/result、result WAL、Edge→Go、Go validate/DB commit/ACK的throughput与p50/p95/p99/max；只测ORT`Run()`、Triton server latency或gRPC microbenchmark不能判热路径PASS；
- 每层报告records/packets/windows/bytes、CPU/core/NUMA、RSS以及适用的pinned/device/VRAM/GPU utilization、allocation、syscall/context-switch、actual copy count/bytes、network retransmit、queue depth/age/watermark、backpressure/retry/dedupe/gap/drop/late/invalid和fsync。CUDA I/O Binding另报告host↔device copy、stream/sync与execution-provider时间；CPU profile另报告thread/affinity/NUMA/arena；未测量不得宣称zero-copy或高性能；
- source覆盖必须分层报告P4 generated/read/snapshot、digest generated/sent/received/acked/drop、PacketIn、mirror NIC/kernel/ring/user、window admitted/final、inference admitted/completed、result/Event durable。相邻层数量不能互相替代，采样流量还必须报告eligible population、rate与coverage；
- 首期默认只资格化一个`telemetry-p4-window/v1`主源和一个`inference-central-grpc-batch/v1`主传输；每个pool generation只绑定一个显式CPU或CUDA执行profile。PACKET_MMAP只有feature/target条件触发；AF_XDP只有PACKET_MMAP在同硬件/同功能合同下无法达到冻结容量时触发；DPDK只有AF_XDP仍无法满足hardware line-rate时触发。Edge-local inference、CPU↔CUDA/TensorRT自动fallback与未触发capture候选不得为了“备用”常驻；
- AF_XDP profile必须分别测XDP_DRV/XDP_SKB、zero-copy/copy、RSS queue/core、NUMA-local/remote、UMEM/ring/batch/need-wakeup、multi-buffer、busy-poll和fallback拒绝；只允许profile声明的exact模式获得PASS。生产要求zero-copy时bind失败必须readiness失败，不能自动copy后仍沿用原性能资格；
- saturated/peak/full-pool-unavailable/3,600 秒 soak必须分别证明CPU/CUDA source→window/input WAL→Edge gRPC admission→Gateway/Triton/selected runtime→result WAL→Go/DB的backpressure闭环；HA profile另覆盖single-replica/failure-domain loss。任一queue满只允许按合同拒绝、背压或形成exact gap/HOLD，不能OOM、切换runtime/backend/model、阻塞P4 effect readback、丢失已durable result或无界增加延迟；
- 与telemetry/capture关闭基线相比，开启默认source的绝对目标及对effect write/readback、rule observation、Go ingest的最大p99/CPU/RSS回退必须由`DEC-001`冻结。相对`PERF-003`只能发现回归，不能代替目标packet/window/Event率、coverage和端到端p99门槛。

## 10. 可靠性与失败语义

### 10.1 Durable 顺序（REL-001）

以下顺序必须通过故障注入验证：

```text
P4 telemetry source counter/register read
→ telemetry WAL durable
→ advance/clear only the qualified telemetry window/source
→ checkpoint durable
```

该顺序只描述 `FUNC-TEL-001` 的 telemetry source 生命周期；`CONTRACT-RULE-001` 的 per-entry direct counter 始终按累计值只读，rule observation 不清零 P4 counter。

```text
qualified source observation
→ telemetry/source WAL durable
→ source clear/advance（仅已资格化source）+ checkpoint durable
→ final valid window + inference input WAL durable
→ batch inference
→ result identity/fence/digest validated
→ inference result WAL durable
→ bounded Edge→Go result batch
→ PostgreSQL canonical Event durable
→ Go canonical ACK
→ Edge result/source checkpoint advance + later WAL compaction
```

DigestListAck、Gateway/Triton/ORT success、gRPC send/receive success和Go内存投影都不是上述canonical ACK，不能单独推进source cursor。DigestListAck仅在相应digest消息已进入telemetry WAL后允许发送，且仍不证明P4数据面覆盖。

```text
proposal durable
→ bounded preflight outside database transaction
→ short transaction: digest/current-fact/scope/CAS validation
→ decision durable
→ approved 时 intent durable；其他状态无 intent
```

```text
effect intent durable
→ claim/fence
→ transaction-free Edge RPC
→ operation journal/P4 write/readback
→ PostgreSQL CAS finalize
```

### 10.2 断网与宕机（REL-002）

- Analysis Plugin 宕机：核心检测、事件、effect 和非分析页面继续；
- Plugin Manager 宕机：所有插件的新任务、activation 和 binding 变更稳定 unavailable，已持久核心事实和真实处置链继续；不得用旧内存 binding 启动新 generation。Runtime Host 单独宕机只使 Wasm/Host-managed service plugin unavailable；Manager 健康且 exact binding 有效时，独立 Analysis/service/Agent 的直连业务边界不依赖 Host，但仍不得绕过 Manager 的准入、generation、revoke 和 deadline；
- Edge↔Control 断网：Edge 继续 bounded telemetry/inference 与本地 WAL；达到上限后背压/gap；新 effect 不盲写；恢复后沿原 identity 重放；
- Control 宕机：Edge 不生成业务 effect，已有 P4 rule 继续；
- PostgreSQL 不可用：Go 拒绝或暂停新的 durable mutation，不能改用 Redis/内存推进；
- 单 Central Inference replica 崩溃/失联：service discovery停止新选择；Edge只在同一logical pool/pool+binding generation内沿原request identity/input digest有界重试到其他已ready replica，第一份有效结果成为candidate，late duplicate被fence；不改变route epoch/current/model/backend；
- logical pool剩余容量不足、全部replica不可用、startup/readback/route handshake漂移或rollout未确认：停止对应shard新canonical result/ACK，在Edge既有WAL域保留bounded pending input并反向背压；达到records/bytes/age上限后形成exact gap和`HOLD/unavailable`。Go只按exact current和同一model-control incarnation有界恢复同generation pool；若不能恢复，由显式rollback operation选择exact previous pool generation。不得使用Edge-local、CPU/TensorRT/LibTorch、tag/latest、随机cache、旧incarnation或未资格模型自动恢复；
- Rust Edge 崩溃：Control 将相关 runtime/effect 置 stale/unknown；重启后依据 WAL/journal/readback 恢复；
- P4 write 后响应丢失：保持 `unknown`，按 operation ID 和真实设备状态 reconcile；
- generation/mastership 改变：旧证据、旧 claim、旧 result 不能覆盖新 generation；
- OIDC/role mapping 服务不可用或认证上下文无法验证：仅已建立且仍可本地验证、未过期、具备 read scope 的会话可以继续只读；新的 proposal/decision/effect mutation 必须拒绝或 `HOLD`；
- Frontend SSE 中断、cursor gap、sequence 乱序、generation/build/profile 变化：先标记 degraded/stale，按原 cursor 或有界 snapshot 恢复；禁止把缓存旧状态标为 current，其他 REST 读和核心链继续；
- PostgreSQL failover：不得用进程内 proposal/approval 状态继续推进；连接恢复后从数据库 canonical facts 和原 idempotency identity 重建；
- governance profile/role mapping/P4Info 更新：必须带显式 compatibility/cutoff/revocation 语义；绑定版本已撤销、不兼容或超过 cutoff 的 proposal/authorization/intent 必须 stale，兼容且未过期者仍按原 digest 和执行 deadline 重验，历史 Decision 不被覆盖。

### 10.3 重试与幂等（REL-003）

- retry 必须沿用原 identity、input digest 和 idempotency key；
- 相同 key+相同 hash 返回同一结果；
- 相同 key+不同 hash 必须冲突拒绝；
- late result 必须被 fence；
- 网络 5xx/timeout 不得触发前端或插件自动创建第二 operation；
- mutation retry 必须有最大次数、退避和总 deadline；
- 对 outcome unknown 的设备副作用禁止自动 blind retry；
- 每个 gRPC client 必须加载并暴露实际生效的 `grpc-service/v1` method config digest；未显式配置的 method 默认不重试、不 hedging。仅声明“调用幂等”不自动授权 retry；资格矩阵必须验证 transparent retry 是否已按 method profile 禁用或可由原 identity 安全吸收；
- server 收到 cancellation/deadline 后必须停止派生工作、释放 semaphore/连接/instance，并保证 late result 不能提交 canonical fact；忽略 cancellation 视为资源泄漏和故障门禁失败。

### 10.3a 在线模型生命周期与恢复（REL-INF-001）

- model revision不可变；register/qualify/rollout/recover operation、pool startup envelope、binding/route/pool generation、deployment action、readback/handshake全部绑定exact digest与model-control incarnation。operation envelope遵守`CONTRACT-MODEL-001`；rollout主路径只能`requested→staging→pool_starting→pool_qualified→rolling→draining_previous→completed`，rollback只能`rollback_requested→staging→pool_starting_or_reuse→rolling_back→completed`，current recovery只能`recovery_requested→pool_recovering→qualified→completed`。任一确定失败进入terminal`failed`并保存exact replica/shard vector；只有尚未开始route withdraw的request可`aborted`；
- artifact/repository pre-stage和新pool ready不改变current。新pool generation必须先完成所有required replica startup/readback、所选availability profile的min-ready/rolling capacity资格；HA profile另须满足failure-domain/N+1。每个shard随后由Edge推进route epoch、撤销旧logical-pool route、停止新window并排空admitted work，再CAS/commit到新pool。Edge只在既有WAL上限内缓冲后续完整window。replica crash/restart只从exact pool envelope+read-only snapshot恢复同generation，并在被选择前完成readiness/readback；文件名、Triton repository scan、Pod/Deployment Ready不能恢复资格；
- 每个batch/window只能携带一个model-control incarnation、shard/route epoch、logical pool/pool generation与current binding generation。pool readback与PostgreSQL current不一致、新pool ready但CAS尚未提交、或CAS已提交但Edge commit handshake未ACK时，Edge不向该新pool发送canonical batch、Go不接受其result；old/unknown incarnation/route/pool/generation late result被fence；
- deployment action/start/readback/drain/stop/commit response loss沿原operation、action sequence、pool-envelope digest、pool/worker runtime identity与route epoch查询实际replica和`GetPoolStatus`，不能盲目创建第二generation。无法证明pool/route identity时该shard保持unavailable/HOLD；不得仅凭Pod名、Service endpoint、Triton ready或健康检查推进CAS。adapter hook至少一次投递由动作幂等吸收；
- 新 revision startup/readback/CAS 失败时停止后续 shard；每个 shard 的 canonical current 保持最后一次成功 CAS（CAS 前失败通常仍为旧 revision，CAS 后恢复失败则为新 revision），availability 单独标记。未滚动 shard 保持旧 current，已 finalize shard 保持其 exact new current，group 标为 `rollout_failed_mixed`。自动化不得盲目回切；显式 rollback operation 才能逐 shard 指向 exact previous，并在每步重验 qualification/revocation/artifact/reader/runtime compatibility；
- previous是exact rollback target，可在冻结rollback grace内以隔离old pool generation保持warm，超过grace后允许只保留repository/qualification reference并按deadline重建。previous不可用时保持当前已确认revision或将对应shard置`HOLD/unavailable`，不得加载开发默认模型、修改taxonomy/threshold绕过或退到任意更旧revision；
- CAS 已把 shard current 提交为新 generation但 route/commit/resume失败时，不撤销已提交事实：availability 进入 `resume_pending/unavailable`，沿原 handshake与buffer watermark有界重试；预算耗尽后 quarantine/HOLD。只有新的 durable rollback operation可以改变 current，不能由 deployment adapter或健康检查隐式回切；
- 无损PostgreSQL failover保留model-control incarnation；PITR/restore/clone/rewind先轮换incarnation并禁用ingest/mutation，再以new operation/new pool/binding generation重验每shard。PostgreSQL/Central Inference/Edge/Go failover、并发rollout/rollback/recovery和stale envelope必须通过unique constraint、single-operation lease、per-shard CAS、ordered shard set、incarnation/pool/worker/route epoch、generation fence、readback与commit handshake收敛。任一外部模型拉取、加载、实例控制、RPC或readiness等待期间PostgreSQL active transaction/held connection为零。

### 10.3b Central Inference池可用性（REL-INF-POOL-001）

- same-generation active-active只提供同一exact CPU或CUDA profile的执行副本冗余，不提供模型、runtime profile、backend、contract或位置回退；logical pool的ready replica集合由exact pool generation、selected=observed runtime、worker readback、failure domain和freshness决定，负载均衡器健康值本身不建立资格。`availability-single/v1`可按其1..N域内副本和required/min-ready门槛证明精确scope内的容量或单replica连续性，但绝不产生failure-domain HA声明；只有`availability-ha/v1`才应用跨域N+1/failure-domain门槛；
- Edge拥有应用层retry/dedupe：仅在请求尚未超过total deadline、input WAL仍current、错误属于profile明确的pre-execution/unavailable集合时重试；max attempts、per-attempt timeout、backoff和retry budget固定。gRPC transparent retry/hedging默认禁用，除非profile证明与相同request identity兼容；
- Gateway/Triton已开始执行但响应丢失时允许重复计算，不允许重复Event；Edge接受第一份通过exact route/binding/input/output digest的结果，其余attempt为late duplicate。same request ID+different input digest、同attempt冲突输出或worker generation漂移必须HOLD/告警；
- 全池不可用不是effect `unknown`：因为尚无设备副作用，它属于inference availability。P4 forwarding、既有规则和Edge P4职责继续；检测窗口在WAL上限内pending，超限形成durable gap，恢复后从resume watermark重放仍未过期且合同current的输入；过期/跨generation输入不得补推成当前Event；
- Kubernetes PDB只覆盖部分自愿中断，Service/endpoint选择只实现same-generation replica发现；两者不能代替应用readback、route fence或全池故障测试，也不能在HA profile中代替N+1/failure-domain门禁。

### 10.4 治理并发与恢复（REL-GOV-001）

- Proposal/Decision 写入必须使用 monotonic revision、unique constraint 和 CAS；两个并发 terminal decision 只能一个成功，失败方读取 canonical result；
- approve 事务必须原子写入 Decision 与其单 target 唯一 Intent，或 fleet 的不可 claim parent + bounded per-target unique Intents；reject 必须原子写入 terminal Decision；stale/blocked 必须以 CAS 写入可重建状态和 append-only audit event，三者均不创建可 claim Intent；禁止出现“审计显示批准但无可追踪 intent/child vector”的提交窗口；
- Go 在 intent durable 前崩溃不得产生 Edge RPC；intent durable 后崩溃按原 operation/idempotency 恢复，不重新征求或伪造授权；
- authorization expiry、proposal expiry、generation/P4Info/evidence/capacity/target-state drift 必须在 claim 前再次检查；失效时 CAS 到稳定 blocked/stale 结果且不调用 Edge；
- Decision 已 durable 但客户端超时时，客户端必须查询原 proposal/operation；不得自动新建 proposal、再次 approve 或生成第二 intent；
- `unknown` 不得因 TTL 或授权过期变成自动 retry；只能沿原 operation journal/readback 进入 `reconciling` 并收敛；
- rollback/expiry 删除失败必须成为同一 effect 链上的 `failed/unknown` 并告警，不能靠第二清理队列掩盖；
- retention、备份恢复和 PITR 必须保持 Proposal → Decision/Policy → Intent → Attempt/Event → readback 的引用链可验证。

### 10.5 插件生命周期与故障恢复（REL-PLUGIN-001）

- admission/qualification/activation 采用 immutable revision、unique constraint、monotonic generation 和 CAS；并发 activate/rollback/revoke 只能产生一个 canonical active binding；
- Plugin Manager 在短 PostgreSQL 事务内切换 binding，事务期间不得拉取 OCI、验证远程 trust service、启动容器、编译 Wasm 或等待 plugin RPC；这些预检在事务外完成并以 digest/snapshot 重验；
- 新 generation 必须先 ready，再原子激活；旧 generation 随后 draining。任一时刻新任务只路由到一个 active generation，shadow 输出和 late result 不得进入 canonical projection；
- Host/plugin crash、OOM、hang、协议错误、资源耗尽或连续失败必须进入有界指数退避和 circuit-open/quarantine；禁止 restart storm、无限 health polling 或核心请求同步等待恢复；
- 只有幂等、无外部副作用且保持同一 task/input/plugin/config digest 的插件调用可以重试；首期 plugin kind 不提供外部 mutation，因此不得把 timeout 结果解释为可自动重放的业务副作用；
- Manager 不可用时全部插件新任务返回稳定 `unavailable`；Host 不可用时仅 Wasm/Host-managed service 路径返回稳定 `unavailable/partial/failed`，独立 service/Agent 仍按 Manager-controlled direct adapter 的真实状态工作。恢复后各路径都依据 PostgreSQL binding、deployment artifact digest 和原 generation 重建，不能依据进程内 cache 猜测 active；
- trust network 短暂不可用不得破坏本地 bundle/撤销 cache 新鲜度仍在 5 分钟上限内的实例；新的 install/activate 必须能够离线完成完整验证，否则 `HOLD`；超过新鲜度上限后禁止新任务，不能以“最后一次曾验证成功”无限延长；
- revoke、capability/policy drift、artifact/config mismatch 必须阻止新任务。revoke 在可达控制面内传播最长 60 秒，Host reconcile 周期最长 30 秒；无法安全 drain 时在 60 秒或更短 task deadline 内终止对应 generation，但不得删除历史结果或影响核心 effect 的恢复；
- runtime profile 默认使用 1 秒起、上限 60 秒的指数退避；同一 generation 在 10 分钟内最多自动重启 5 次，超限后至少 quarantine 15 分钟且必须由 canonical policy/人工动作解除，不能由 liveness probe 无限重置计数。

### 10.5a 插件统计运行与投影恢复（REL-PLUGIN-STAT-001）

- schedule/run/artifact/current 分离；Go crash 在 run durable 前不得 dispatch，run durable 后只沿原 idempotency/input/plugin/config digest 查询或重试。timeout/5xx/SSE gap/页面刷新不得创建第二 run identity；
- 只有无外部副作用、输入和 exact binding 仍 current、同一 run/input digest 且在 total deadline/max attempts 内的调用可以重试；read-only-tool 已对外发起请求但响应未知时仍只重算派生结果，不得据此执行 mutation或改变外部状态；
- result 在 Go 校验/CAS 前不成为 current。schema/identity/digest/quality/resource/display 失败、old generation、revoked binding 或 expected-current conflict 只产生 failed/fenced/invalid audit，不覆盖 last known current；
- plugin/Host/Manager 不可用、disabled/revoked、Artifact 过期或 source facts generation/data-class 漂移时，current projection立即显示 `stale|unavailable|revoked`；历史仍只读且携带原 producer/provenance。核心/原生统计继续，不能无标记使用旧值；
- PostgreSQL failover 后从 schedule/run/artifact/current 重建；PITR/restore 后先重验 plugin active binding/revocation、source scope/profile 与 Artifact digest，再开放新 run。恢复出的 queued/running 不得直接 claim，必须由 Go 创建新的 attempt identity或确定 terminal；
- cancellation/deadline 必须传播到 Host/plugin并释放 semaphore、DB connection、RPC和renderer request；late result由 generation/run fence 拒绝。queue/retention cleanup按 exact owned run/partition执行，禁止 wildcard 删除历史或 legal hold。

### 10.6 规则观测恢复与失真防护（REL-RULE-001）

- rule observation 读取失败、排队溢出、PostgreSQL/API/Grafana 不可用只使对应观测维度 `stale/invalid/not_measurable`；不得改变已安装规则、effect execution 状态、authorization 或自动触发补写/删除；
- Edge/P4 full restart、application generation、pipeline/P4Info、counter capability、entry digest 或 rule revision 变化时，Edge 必须上报 epoch-invalidating condition 并只建立新的本地 cumulative-counter sample baseline；Go 关闭旧 canonical observation epoch，并在 exact identity/readback 条件满足后创建新 epoch。旧 epoch late sample 只可审计，不能覆盖 current 或与新样本计算 delta；
- cumulative count 下降只有在 target profile 的 counter width/mode、前值接近上界、最大可能速率和同一 epoch 全部能证明单次 wrap 时才能按 wrap 计算；否则标记 `reset_or_wrap/ambiguous` 并从当前值重建 baseline。达到 saturation 上界后不得继续推算命中；
- duplicate sample 按 identity+sequence+digest 幂等去重；same identity/sequence different digest、乱序、跨窗口重叠、缺口和 read response 中重复 entity 必须记录稳定错误并使受影响 rollup invalid。P4Runtime response 分片/顺序不得用于时间或 identity 推断；
- rate 的 elapsed time 使用同一 Edge monotonic sample interval 或 target profile 的可验证 data timestamp；主机 wall-clock 回拨、NTP step、跨 Edge 接管或时间精度不足时只保留 cumulative/窗口边界，不计算 pps/bps；
- wildcard、LPM、ternary、priority、default entry 和多 table pipeline 的命中只代表该 table 实际选择/执行的 entry；不得把一条规则的命中外推为唯一攻击归因，也不得跨 table 累加为全局 ingress 比率。overlap/shadow 诊断必须引用 P4Info 和独立静态/packet test 证据；
- TTL expiry、rollback、manual supersede 和 readback mismatch 必须及时停止 active observation 并保留 final sample/event；target 仍存在但数据库认为 expired，或数据库 active 但 target readback 缺失时必须告警并进入原 effect reconcile，不得用 counter presence 把 drift 标成正常。

### 10.6a BMv2 无状态防火墙激活与恢复（REL-P4-FW-001）

- baseline policy 激活必须作为一个 durable effect operation 执行固定阶段：锁定 exact revision/target/P4Info/application generation/active-inactive bank/selector precondition → 编译并预检完整 plan → 写 inactive bank → exact entry/count/digest readback → 原子更新单条 policy selector → exact selector readback → PostgreSQL CAS current/previous → 在有界 rollback grace 后清理旧 bank。上述阶段不得形成第二队列或第二 writer；
- inactive bank 任一写入、读取、容量或摘要校验失败时，active selector 必须保持原值，失败 bank 只按原 operation 精确清理或覆盖；禁止把 partial bank 设为 active。selector Write 响应丢失、Edge/Go 在 selector 前后崩溃或 PostgreSQL CAS 失败时，不得盲目重写：必须沿原 operation journal 读取 selector 和两个 bank 的 exact state 后收敛；
- selector 已切换但 PostgreSQL current 未确认时，状态必须为 `unknown/reconciling`，暂停该 target 的后续 baseline activation、rollback 和与 active bank 冲突的 response overlay mutation；只有 readback 能唯一证明 old 或 new revision 后，才允许按原 precondition token CAS finalize。无法唯一证明时维持 `HOLD` 并保留已验证的数据面状态，不得猜测；
- concurrent activation、rollback、TTL expiry 和 retry 必须由 target-scoped claim、operation generation、expected selector revision 和 plan digest fence；旧 operation、旧 application generation、旧 P4Info、旧 bank epoch 或 different-digest retry 必须稳定拒绝。rollback 是创建新 operation 激活 exact previous revision，不是把历史 Decision/Intent 标回成功，也不是从任意 bank 猜一版规则；
- response overlay 的 expiry 必须来自 PostgreSQL durable deadline，并由原 effect reconcile 驱动 exact delete/readback/CAS；不得依赖 BMv2 idle-timeout notification、进程内 timer 或 counter inactivity 作为唯一删除依据。重启后 target 中存在而数据库无 active fact、数据库 active 而 target 缺失、或 selector/default action 不符时均进入 drift/HOLD，不自动放行或重装；
- P4Runtime session/mastership、BMv2、Edge 或 Go 重启后，Edge 必须先验证 pipeline/P4Info、application generation、selector、active bank revision 和 response overlay journal，再恢复写 readiness。证据缺失或不一致时只允许 bounded read/reconcile；宿主 UFW/nftables/iptables 不得成为 fallback、补偿动作或恢复依据。

### 10.6b 多 Target Assignment 与 Fleet 恢复（REL-TARGET-FLEET-001）

- 一个 target actor断连、崩溃、超时、队列/磁盘满、pipeline drift或失去mastership，只使该target assignment/readiness与相关child `stale/HOLD/unknown`；其他actor继续各自有界工作。Supervisor不得全局重启、串行等待无上限或让一个target的reconnect storm耗尽FD/task/P4 RPC；
- planned assignment handoff固定为：Go冻结新claim并 durable revoke/drain旧 assignment lease → old actor停止接收新work、收敛/导出journal+readback摘要并释放/失去mastership → old actor确认 revoke 或其 lease 到期 → Go CAS新assignment generation并分配严格更高、旧 assignment 无权使用的 election floor/range → new actor建立独立StreamChannel并取得primary → read-only验证pipeline/P4Info/selector/entries与未决operation → Go确认readiness后才开放claim。任一步响应丢失沿原assignment operation查询，不能并行猜测；
- old Edge不可达时允许emergency reassignment，但 Go 必须先 revoke 旧 assignment、为新 generation durable 分配严格更高的 election floor/range；新 actor 再通过 P4Runtime arbitration取得可证明更高primary、提升application generation，并从Go/PostgreSQL intent/attempt与target exact readback逐项reconcile。旧 actor 即使分区恢复或保留本地journal，也因 lease/revoke、assignment generation 与受限 election range只能read-only，且 target arbitration拒绝其旧 primary；测试必须覆盖旧/新 actor并发重连、旧 actor尝试分配/发送更高 election/Write 与 late result。旧journal不可访问意味着不能blind retry；无法确定已有副作用时保持child `unknown/reconciling`，不因新actorhealthy而改为failed/applied；
- fleet wave只在当前wave全部required child达到允许的terminal/known状态且policy条件满足时开启下一wave；任何unknown使parent`reconciling`并关闭后续gate。fail-fast只把未开始child CAS为`blocked`，不回滚已applied/unknown；manual-gate decision必须绑定completed-vector digest，stale vector不执行；
- target membership/assignment/application generation/P4Info在operation中变化时，对应未claim child在副作用前`blocked/stale`，已write_started child沿原identity收敛。不得动态把replacement target塞入原target set，也不得把新target继承旧target的Decision、intent、counter或previous policy；
- PostgreSQL无损failover从canonicalparent/child/gate重建；PITR/restore/clone/rewind先轮换target-control incarnation、禁止fleet/claim，逐targetread-onlyreconcile并明确处置old-incarnation未决child后才开放。恢复出的百分比、wave或assignment不能自证current；
- rollback按target创建新child并保留混合结果；一部分target回滚成功不把全fleet显示为rollback complete。目标无previous、previous不兼容、target离线或readback不明确时保持该target真实current/HOLD，并在parent vector中显示。

### 10.7 流量回放故障与清理语义（REL-TRAFFIC-001）

- fixture 不存在、digest/size/format/DLT/timestamp/snaplen/count 不符、PCAP truncated/corrupt、ground-truth join 冲突、direction cache/rewrite digest 不符或 unsupported mode 时必须在配置网络和发包前失败；不得以跳过坏包、猜测方向、自动换 DLT 或忽略标签冲突继续 PASS；
- topology/interface/namespace/P4Info/port map 漂移、MTU/checksum/offload 不符、qdisc apply/readback 失败、非预期 route/ARP/egress 或测试 target 不是 manifest 中 exact identity 时 fail closed；已发包时结果标为 partial/invalid 并保留发生范围，不能重命名为 zero-hit；
- sender crash/hang、deadline、link down、queue/capture overflow、BMv2 restart、receiver/oracle loss、磁盘/inode/RSS/FD/PID/cgroup 耗尽、用户取消和宿主重启必须停止新发送，终止子进程组，并在总清理 deadline 内只删除本 run exact-owned qdisc/namespace/veth/temp artifact；禁止 wildcard cleanup、遗留无限 loop 或影响其他 run/宿主接口；
- retry 只能沿用同一 run/fixture/config/environment digest，并创建新的 attempt identity；已有部分发送或无法证明 target/counter baseline 时不得把 retry 与原 attempt 聚合。每次 attempt 单独保存 start/end packet index、sender/DUT/capture count 与 counter baseline；
- netem random impairment 必须固定 seed并保存实际 qdisc state/statistics；相同 seed 只保证同 profile 下可复核输入，不保证不同 kernel/iproute2/调度环境逐包完全相同。timer compression、TSQ/offload 或宿主争用超出 profile 容限时为 environment-invalid/HOLD，不得归因于 P4；
- 每个 fault case 必须证明生产 NIC/route/credential/P4 target/数据库未被访问，测试控制面没有成为第二 writer，清理后无 namespace/veth/qdisc/process/temp/file-descriptor 泄漏；无法证明清理完成时 runner host 进入 quarantine，不继续下一 case。

### 10.8 在线遥测与推理热路径恢复（REL-TEL-INF-001）

- P4 source每次bank flip/freeze/read/clear/advance都绑定source runtime epoch、application generation、pipeline/P4Info和window identity。Edge只有在snapshot已验证且telemetry WAL durable后才能clear/advance；read后fsync前崩溃允许重读/去重，clear后checkpoint前崩溃必须由target epoch/readback恢复，不能把新bank误作旧窗口或补零；
- target不支持原子snapshot时，profile必须使用可验证sequence-before/after+bounded reread；deadline内不能一致则窗口`partial/not_measurable`并停止canonical inference。多register/counter entity的RPC顺序、返回顺序或一次成功status不能冒充同一时刻快照；
- Digest duplicate/list cache/Ack丢失、server/client重启或过载drop只影响supplemental coverage；Edge按exact list/item identity幂等，Ack只在本地durable后发送。PacketIn/digest queue达到上限优先丢弃supplemental sample并记录gap，不得阻塞mastership/effect/readback或把丢失隐藏为full coverage；
- source restart/reconnect、Edge restart、capture backend重启、NIC queue重配、P4 pipeline/generation、bank/reset或clock epoch变化必须开启新的source runtime epoch和window baseline；旧epoch late observation只可审计，不能完成新window；
- gRPC request在发送前已经进入input WAL；连接中断、partial frame、deadline/cancellation、Gateway/Triton/worker crash、resolver endpoint替换或same-generation response duplicate均不推进source/Event ACK。Edge只沿原request identity/input digest和冻结retry budget重试，不能信任Gateway/Triton queue或gRPC success作为耐久副本；
- Central Inference hang/deadline/invalid/conflicting result时Edge不推进source/Event ACK；已durable input只在同model-control incarnation、logical pool/pool+binding generation和route epoch内有界重试/缓冲。若generation/route已变化则旧input/result被fence并形成exact gap/HOLD，禁止跨generation重新组window、切换backend或补推为当前Event；
- Go/Control/网络不可用时Edge先保存result WAL并有界重放；result queue/WAL达到high/critical watermark必须反向停止新window admission或按source profile降采样，不能覆盖已durable未ACK result。PostgreSQL commit后ACK丢失时same identity+same digest返回原canonical result，different digest冲突；
- final-window之后到达record不触发retraction、第二inference或Event改写；保存`late_after_final`计数/有界evidence。watermark无法在跨runtime/时钟漂移后证明时对应source为stale/HOLD并建立新baseline；
- PACKET_MMAP/AF_XDP capture失败、ring/UMEM耗尽、kernel/NIC drop、interface/link/driver/XDP mode变化必须进入分层drop/coverage。性能profile要求的XDP_DRV/zero-copy不可用时readiness失败；禁止静默回退到XDP_SKB/copy或切另一backend后仍使用原epoch/profile资格。

## 11. 安全需求

### 11.1 服务身份与网络（SEC-001）

- 跨主机 gRPC、HTTPS、MCP、A2A 必须使用真实 TLS；
- 服务间使用独立 mTLS identity；
- PostgreSQL 使用 `sslmode=verify-full` 或等价完整验证；
- 必须验证 CA、SAN、hostname、用途和有效期；
- 禁止明文 fallback、`sslmode=prefer` 或只检查“存在证书”；
- 证书轮换必须支持有界重叠、连接重建、失败回滚和审计。
- UDS 只能位于受控 runtime directory 的绝对路径；每个 binding/generation 目录由 socket producer UID 拥有、peer 使用专用 GID，默认 directory mode `0750`、socket mode `0660`。启动/连接必须拒绝 symlink、抽象 namespace、非 socket、非预期 owner/group、共享可写 volume 和路径替换，并使用 `SO_PEERCRED` 或等价机制校验实际 UID/GID/PID/workload identity；仅依赖路径或文件 mode 不构成完整身份验证；
- UDS endpoint 必须绑定 deployment/binding/generation，采用原子创建与精确清理；旧 generation socket、inode 或 peer credential 不得被新 binding 复用。UDS 同样执行 framing、message size、deadline、backpressure 和 audit，不能因位于本机绕过应用层授权。

### 11.2 人类身份与 Effect 授权（SEC-002）

vNext 不复刻 legacy 用户/会话/通用审核系统。按照 `DEC-005`，首期使用受信上游 OIDC 身份，将 Analyst、Operator、Platform Admin、Auditor 映射为版本化、target/risk/effect/model/plugin 受限的 scope；授权判定由 Go Control 完成，反向代理只提供已验证身份上下文，不能替代业务授权。

- actor 的稳定身份键必须基于精确 `(iss, sub)`，不能使用可能变化或冲突的 email/display name；
- Go/受信认证组件必须验证 issuer、签名/JWKS、audience、expiry、not-before/issued-at，以及多 audience 时适用的 authorized party；无法验证时 fail closed；
- role/scope mapping 必须版本化、有 digest、最小权限、默认拒绝并记录到每个 Decision；IdP group 名不能在业务代码中散落硬编码；
- R2 approve 必须要求 IdP 提供可验证、最近 5 分钟内完成的 phishing-resistant MFA/step-up 认证上下文（例如 WebAuthn/FIDO2）；认证上下文缺失、过旧或不满足 profile 时为 `HOLD`；
- R2 proposer 与 approver 的稳定 actor identity 必须不同；Platform Admin、服务账号、Agent Plugin 和 Frontend BFF 身份不能代替人类 approver；
- 所有人类 R0/R1/R2 authorization 必须绑定完整 proposal digest、scope、risk、target 和 expiry；“有 Operator 角色”不能批准任意设备或任意 effect；
- 浏览器只能持有安全、HttpOnly、SameSite 的受限会话 cookie，并实施 CSRF、replay 和 session fixation 防护；OIDC authorization code/token exchange 由 Go/受信认证组件完成，SPA 不读取或持久化 ID/access/refresh token，且不能持有 P4、Edge、数据库或 Agent provider secret；
- proposal/decision/role/policy mutation 必须记录 actor/service identity reference、scope、role-mapping digest、认证上下文等级、idempotency key、request/trace ID、reason code 和 canonical result；
- 审计 UI 与日志必须脱敏；原始 ID token/access token、cookie 和敏感 OIDC claims 不得持久化到 proposal、日志或前端 bundle；
- role mapping/profile 变更和撤销必须可审计，并只影响重新验证后的未来授权；不得覆盖历史 Decision。

### 11.3 Secret（SEC-003）

- secret 通过 secret file、secret manager 或受控 workload identity 注入；
- 禁止写入镜像、仓库、前端 bundle、`VITE_*`/其他 public build-time config、命令行、日志或 Artifact；
- DB、P4、Event、MCP、A2A、LLM provider identity 必须分离；
- 插件不得获得核心或 Edge 高权限 secret；
- secret 轮换、撤销、过期和连接重建必须有测试。

### 11.4 Artifact 完整性（SEC-004）

vNext 删除自研多域签名发布链，但仍必须：

- 使用严格 manifest schema；
- 验证 model/scaler/config/P4Info/device config 的 SHA-256；
- 验证 shape/dtype/contract/runtime compatibility；
- 使用只读路径、普通文件/no-symlink/size 检查；
- 生产 OCI image 使用不可变 digest；
- 不因缺少 legacy 自研四域签名报告阻止兼容且完整的核心 runtime 启动；通用插件仍必须满足 `SEC-PLUGIN-002` 的标准供应链准入；
- 不把 SHA-256 声称为恶意发布者身份认证。
- 生产 model bundle 必须使用 digest-pinned OCI artifact 或受控离线 bundle，记录 producer/build identity、SBOM/provenance、qualification evidence 和 offline verification metadata；这些标准证据不恢复 legacy model/release signer、key registry 或 signed report，也不代替模型质量门禁；
- model bundle 只能携带合同允许的数据文件。ONNX custom op library、TensorRT plugin、LibTorch extension、native shared library、Python/Wasm hook 或其他可执行 pre/post-processing 必须作为 inference image/runtime component 独立登记、签名、sandbox 和资格化，禁止由 bundle 动态注入；
- model manifest 必须以 file-role allowlist 列出每个 entry 的 normalized relative path、type、size 和 digest，并固定文件数、单文件/总压缩/解压 bytes 上限；解析/解包在写入前拒绝绝对路径、`..`、重复/大小写冲突路径、symlink/hardlink/device/FIFO/socket、unknown role、executable mode/native-library magic、额外 archive member 与未资格 custom operator，防止 bundle 成为路径穿越或代码加载器；
- 模型路径/cache 必须 read-only、普通文件、no-symlink、精确 owner/mode/size/digest，prepare 使用同文件系统临时隔离目录并在完整验证后原子发布；production runtime 不联网解析 mutable registry tag/alias 或下载依赖。

### 11.5 输入与日志（SEC-005）

- 所有外部输入在解析前实施 framing/body size limit；
- schema 必须拒绝未知字段或按明确兼容策略处理；
- 文件 URI、A2A endpoint 和 webhook 防 SSRF/path traversal/symlink；
- 日志使用结构化字段和稳定 message code；
- 禁止 secrets、raw packet、完整 prompt、完整模型输出和敏感 header；
- 清理按精确 ownership 执行，不允许 wildcard 或破坏审计事实。

### 11.6 插件 Capability 与运行隔离（SEC-PLUGIN-001）

- 插件 identity 必须绑定 workload identity、`plugin_id/revision/artifact digest/config digest/generation`，不能只使用进程 UID、容器名、tag、display name 或 Agent Card 名称；
- capability 默认拒绝，运行时实际能力只能是 signed/deployed policy 与 manifest 声明的交集；插件、prompt、Artifact、A2A peer 或 MCP result 均不能自行增权；
- OCI plugin 必须非 root、read-only rootfs、禁止 privilege escalation/privileged、drop all capabilities 后按需最小增加、使用 seccomp/AppArmor/SELinux 等适用隔离，并禁止 hostNetwork/hostPID/hostIPC/hostPath、Docker socket、设备和共享可写 volume；
- 每个插件必须有独立 cgroup/等价 CPU、memory、PID、FD、I/O、ephemeral storage、inode 和进程数硬上限；Wasm `ResourceLimiter` 不能替代进程级资源限制；
- Wasm Host 只链接 kind WIT 所需 imports；`pure-transform` 不得获得 WASI filesystem、socket、environment、clock、random 或 process capability；
- 网络默认 deny；获准 endpoint 必须同时受协议、DNS/IP/CIDR/port、TLS identity、redirect 和 SSRF policy 约束，禁止解析后漂移到 loopback、link-local、metadata、管理网或 P4 control endpoint；
- secret 仅以短期、插件专属 reference/workload identity 注入，禁止进入 manifest 明文、env dump、argv、日志、Artifact、Wasm linear memory snapshot 或其他插件；轮换/撤销必须重建对应实例；
- 越权尝试、sandbox 约束无法施加、identity/config/capability drift 或 isolation profile 未验证时必须拒绝 readiness/执行并审计，不能降级为宿主普通进程。

### 11.6a 插件统计与声明式显示安全（SEC-PLUGIN-STAT-001）

- Go 依据输入源事实的数据分类、当前 `(iss,sub)` scope、plugin binding/capability 和 exact definition revision/digest 重新授权 freeze/read/export；插件 manifest、返回字段、display hint、Artifact provenance或签名均不能定义/扩大 ACL；
- on-demand run 必须同时验证源数据 read 与 `plugin.statistics.run` scope；schedule create/revise/disable 必须验证 scoped Platform Admin、相关 data-class scope、CSRF/step-up policy（若 profile 要求）、exact definition/binding、idempotency 和审计。schedule 不是永久授权：每次执行都重验当前 binding/revocation、policy revision、data scope 与 external-source capability；任一漂移即 fenced/HOLD，不运行插件；
- 插件统计输入不得包含 raw packet/payload、secret、credential、完整 prompt/provider output、数据库 DSN、内部对象地址、任意 file/URL 或超出当前 scope 的事实。所有字符串、名称、单位、dimension 与 table cell 都是不可信输入；
- Go 在 admission/分配/完整解析前执行 definition/projection/field/external-source capability allowlist、framing/body/depth/count/bytes limit、strict schema、canonical dimension排序/长度/基数、finite-number、timestamp/window、duplicate/out-of-order、digest/generation和display reference验证；definition 内 endpoint/credential、SQL/PromQL/JSONPath/MCP prompt/URL/表达式、runtime definition registration、unknown major/kind/unit/field、NaN/Inf、整数溢出、压缩炸弹或超限 fail closed；
- Web 只用内置 Vue component、安全文本 sink与内部ECharts `dataset/encode`；生产构建和负向测试拒绝 `v-html`、dynamic remote component、HTML/SVG/CSS/template、script/module、iframe/URL、formatter/callback/`renderItem`/event、Vega expression和任意 option。CSP保留`unsafe-eval`禁用，且不能作为唯一XSS防线；
- CSV/电子表格导出由 Go 进行字段长度、引号、分隔符、CR/LF 与 formula-injection 防护；原始 JSON 导出同样受授权、分页、bytes、审计和 Content-Type/nosniff。插件不能提供文件名、MIME、下载 URL 或公式；
- Plugin/Host 无核心 DB/P4/effect/Frontend secret；网络默认 deny。统计 result不能创建 Proposal/Decision/Intent、更新 rule/model/Incident/qualification、注册 route、触发前端 action或变成 Prometheus high-cardinality series；必须以凭据缺失、API拒绝、CSP/build policy和数据库 deny-role共同证明。

### 11.7 插件供应链与撤销（SEC-PLUGIN-002）

- 生产 admission 必须验证完整 OCI manifest/index 与 layer/component 的 media type、platform、size 和 digest；只允许 `sha256` 内容地址，tag/`latest` 不得持久化为 active identity；
- 必须使用 Cosign/Sigstore bundle 或等价组织 PKI 验证发布者；policy 必须固定允许的 OIDC issuer + certificate identity/repository/workflow 或公钥/KMS identity，并验证 signature subject 与实际 artifact digest 精确相同；
- 每个生产插件必须附 SPDX/CycloneDX 或等价 SBOM，以及与 artifact digest 绑定的 in-toto/SLSA provenance；deployment policy 必须校验允许的 source revision、builder identity 和 build type，不能只检查文件存在；
- 签名、provenance 和 SBOM 是准入证据，不代表代码无漏洞或行为安全；仍必须通过 capability、sandbox、黑盒、漏洞 policy 和运行时资格门禁；
- Go 必须维护按 `(plugin_id, artifact digest, publisher identity, verification bundle digest)` 定位的版本化撤销事实；撤销检查覆盖 install、activate、restart、rollback 和周期性 reconcile；
- Host reconcile 周期最长 30 秒，可达控制面下撤销传播 SLO 为 60 秒；生产撤销/trust state 的 `verified_at` 超过 5 分钟即 stale，对应 binding 停止新任务并 `HOLD`。任何放宽这三个上限的 profile 变更均属于 `PLUGIN-PLAT-004` 定义的高杠杆双人 policy change；
- verification bundle、信任根、policy 与检查器版本必须可离线重验并记录审计，避免把公共 registry、OIDC、Fulcio/Rekor 或外部网络变成每次启动的同步正确性依赖；
- 开发环境的 unsigned fixture 必须使用独立 profile/namespace，UI、日志和证据明确 `NOT QUALIFIED`，不得通过 config fallback、复制数据库或镜像 promotion 进入生产；
- 本节不恢复 legacy scenario/model/release/pipeline 四域签名、独立 release signer、key registry 或 signed release report；禁止自研第二套签名格式和审批链。

### 11.8 全系统成熟组件供应链（SEC-SUPPLY-001）

- 所有 production package、image、binary、generator、scanner、漏洞数据库、dashboard/config/chart 和 vendored/generated source 必须符合 `CONTRACT-SUPPLY-001`；未知来源、mutable tag/range、digest/lock/SBOM/NOTICE 漂移、EOL/撤销或未批准 capability/data access 必须拒绝 build/admission/release；
- CI/build 使用受控 registry/cache 和 digest-pinned tool image/binary；production runtime 不执行 curl-to-shell、package install、plugin auto-discovery、remote template/chart/font/script 下载或在线更新 scanner database。离线 bundle/database 必须带 digest、生成时间、有效期和更新/回滚流程；
- Syft/等价 SBOM generator、Trivy/选定 scanner 和 Cosign/组织 PKI 使用相互独立的最小身份；scanner 不获得 signing key，build job 不获得 production deploy/P4/DB identity，dashboard/observability job 不获得核心 write；
- scanner database 超过 profile freshness、上游 advisory/source 不可验证或扫描器失败时，新 artifact 资格为 `HOLD`；不得把未扫描 artifact 自动部署，也不得因新 CVE 直接停止/删除 P4 rule、改写 PostgreSQL 或回滚生产，响应必须走独立 incident/change/revoke 流程；
- source vendoring/fork 必须在导入前完成许可证/NOTICE/source obligation、恶意代码/secret、测试和维护审查；删除 upstream header、只改名/格式/翻译或从生成物反推源码不能规避登记；
- 组件 upgrade/rollback 必须同时验证配置 schema、持久化数据、wire/current-previous、权限、性能与 failure fallback；无法回滚的 schema/data change 必须走 expand/contract。成熟组件的 CVE/许可证/停更应有 owner、SLA 和替换演练，但不能由其自身控制面获得 MASI 核心 mutation 权限。

### 11.9 测试流量、PCAP 与发生器隔离（SEC-TRAFFIC-001）

- 所有 traffic fixture 按不可信二进制输入处理：解析前校验普通文件、受控绝对根目录、no-symlink/no-hardlink、owner/mode、大小/文件数、magic/format/digest；解析和 rewrite 在无生产 secret、read-only input、独立 writable temp、CPU/RSS/PID/FD/disk/inode/deadline 限制的 runner/容器中执行，禁止 archive traversal、device/FIFO/socket、任意 plugin/script 或文件名驱动执行；
- runner 需要 root/CAP_NET_ADMIN/CAP_NET_RAW 时只能在专用测试宿主或隔离 VM/namespace 使用，采用最小 capability、固定 binary/argv schema 和 egress deny；manifest 不能提供 raw command、environment expansion、host path、interface glob、容器 socket、SSH/云凭据或生产 P4Runtime identity。UI/API 不提供上传后立即执行任意 PCAP 的生产功能；
- 拓扑 allowlist 只能列入本次运行创建或预先登记的专用测试 namespace、veth/TAP 与 test target；始终拒绝宿主管理/生产/公网 interface、非测试 route、DNS、metadata endpoint 和非测试 P4 target。测试 namespace 默认无外网，外部数据集获取在独立 ingestion job 完成，运行阶段只读消费已验证离线 artifact；
- PCAP/PCAPNG 可能包含完整 payload、IP/MAC、凭据、个人数据或恶意 exploit。每个 corpus 在取得、处理、共享和删除前必须有 owner、合法用途、license/redistribution、数据分类、去标识风险评估、最小访问、加密、审计、retention/deletion 和 incident handling；仅替换 IP 地址不自动视为匿名或安全（依据：[NIST SP 800-188](https://csrc.nist.gov/pubs/sp/800/188/final)、[RFC 6235](https://datatracker.ietf.org/doc/rfc6235/)）。raw packet/payload 不进入日志、Prometheus、Frontend、普通 CI artifact 或长期 evidence；evidence 只保存受控 reference/digest和必要的脱敏摘要；
- 项目自有 synthetic fixture 应最小化 payload，并使用保留测试地址/域名与无真实 credential；真实漏洞利用、malware C2 或可对外造成影响的 payload 只有在单独批准的 cyber-range profile 中允许，默认断网且禁止向第三方目标发送；
- Tcpreplay GPLv3、PTF packet backend、Mininet、TRex/其他 generator 及其传递依赖必须进入 `CONTRACT-SUPPLY-001`（Tcpreplay 上游许可声明见其[官方文档页脚](https://tcpreplay.appneta.com/concepts/replay-model/)）。在项目许可证/NOTICE/分发义务未审查前，GPL 工具只可作为外部、隔离、条件资格化的 test binary/image，禁止复制/vendoring其源码或链接进生产制品；许可证合规不代替 sandbox、功能或性能资格。

### 11.10 在线采集与中央推理隔离（SEC-TEL-INF-001）

- 默认`telemetry-p4-window/v1`不得导出payload；P4 digest/PacketIn只允许manifest/profile列出的字段、bit width、sample/truncate和bytes/rate。未知digest ID、metadata field、oversize、malformed bitstring或unqualified observation point在写入window/WAL前拒绝；
- mirror capture只能绑定部署清单中的专用TAP/mirror interface与ifindex/MAC/driver/netns。始终拒绝management/production control/public interface、`any`/glob、动态name-only匹配和hostNetwork旁路；AF_XDP需要BPF/XDP或raw capability时由专用Edge identity最小授予，Go/Central Inference/plugin/Web不得获得；
- AF_XDP/DPDK/XDP program、libbpf/libxdp/driver/firmware/PMD/hugepage配置属于`CONTRACT-SUPPLY-001`与runtime profile；加载/替换XDP program不得取得P4 writer、effect或通用host mutation权限。采集接口不可同时作为管理/secret/registry/数据库网络路径；
- Edge↔Gateway跨主机只允许mTLS，完整验证CA/SAN/workload identity、logical pool/pool generation、method allowlist、max frame/body、deadline和revocation；禁止明文fallback、public endpoint、redirect/proxy、浏览器/插件直连、permissive DNS或跨generation Service selector。证书/身份错误必须在发送feature前fail closed；
- Gateway和Triton使用独立最小UID、read-only rootfs/repository/backend目录和隔离网络；Triton HTTP/model-control endpoint、repository poll/update、auto-complete、未登记backend/custom library和任意model-control RPC在production配置/网络/负向测试中拒绝。模型目录动态更新可能执行任意代码，因此production只允许startup-bound digest snapshot；NONE模式snapshot必须是exact binding依赖闭包，拒绝额外model/version/config/backend，并显式固定instance group与ORT provider partition；
- Rust/Gateway/backend在使用任何payload offset/length/shape前执行checked arithmetic、message bounds、alignment、overlap、record/tensor count和digest验证；禁止raw pointer、file path、FD number、argv、environment、function pointer、native object/vtable或可执行内容进入wire/model bundle；
- 网络/inference buffer只保存有界in-flight feature/result且不得被core dump、日志、trace、普通diagnostic bundle或插件volume收集；进程dump默认关闭或进入受控敏感证据流程。Gateway无durable spool，Triton无业务queue持久化，Edge WAL仍是唯一可恢复输入/结果所有者；
- source/flow IP、五元组、tensor、input/output digest和window identity不得作为Prometheus label。日志只保留低基数reason与受限短reference；raw packet/payload、完整feature tensor和model output不得记录。

### 11.11 BMv2 无状态防火墙安全边界（SEC-P4-FW-001）

- 人类、Frontend、Plugin、Analysis、LLM/MCP/A2A 和第三方工具只能提交版本化 normalized firewall policy/effect contract；所有生产 API 必须拒绝 raw `TableEntry`、P4 source、P4Info/device config、shell/CLI、任意 action ID/bytes 和绕过 Go eligibility/risk 的命令。只有 Rust Edge 将已授权 normalized plan 编译为当前 profile 的 P4Runtime entity；
- response overlay 继续遵守 `SEC-002` 的 R0/R1/R2 scope、expiry 和 maker-checker；baseline policy revision 由 scoped Platform Admin 创建，R3 activation 必须由不同稳定人类身份的 scoped Operator 通过 phishing-resistant step-up 授权。两者均绑定 exact policy/compiled-plan/target/P4Info/default-action digest；Platform Admin 不因管理 policy 而获得 Operator 权限；
- Web、Go以外的插件/运行时、Central Inference、测试runner和观测组件不得持有production P4Runtime write identity；P4Runtime endpoint必须限制到Edge workload identity和必要只读诊断identity。测试identity、P4Runtime Shell、PTF/P4Testgen、p4-constraints CLI与Linux host firewall均不得进入production mutation路径；
- P4 program、BMv2 JSON、P4Info、firewall profile 和 compiled plan 均属于不可变、digest-pinned artifact/config；生产 BMv2 禁止运行时加载未登记 `.so`/extern module，禁止由 policy bundle 动态注入 P4/原生/Wasm/Python 可执行内容；
- parser 对 malformed、unsupported EtherType、IPv4 options、fragment class、协议/端口不可用和未知 metadata 的动作必须由 profile 明确并经 packet golden 证明；不得由控制面猜测未解析字段、把非首片端口当作 0，或因规则缺失隐式改用 permissive development default；
- policy/rule/entry/operation digest、IP/prefix、五元组、actor 和 raw packet 不进入 Prometheus label；结构化日志只保留短 reference、bounded reason 和脱敏摘要。导出/审计内容遵守 scope、分页、retention 和敏感字段遮蔽；
- 资格测试宿主必须读取并记录 UFW/nftables/iptables/eBPF/XDP/namespace/qdisc 状态，确保没有非被测 host filter 造成假 drop PASS；若宿主控制不可证明，action outcome 为 environment-invalid/HOLD。生产主机自身加固若采用 Linux firewall，必须由独立运维 profile 管理，不能被展示为 BMv2 policy/readback/outcome。

### 11.12 多 Target 与设备管理安全边界（SEC-TARGET-FLEET-001）

- target create/import/read/activate/assign/drain/disable/quarantine/retire 与 fleet effect分别使用最小OIDC scope；注册候选、标签编辑和真实effect授权不可因同一actor拥有多个role而合并。target activate/endpoint/device_id/role/credential-reference/assignment/profile变更要求scoped Platform Admin最近5分钟phishing-resistant step-up、exact diff/digest和append-onlyaudit，但不授予Operator或P4 write；
- P4Runtime/gNMI endpoint必须来自受控registry并验证scheme/host/IP/port/SAN/CA/workload identity，解析后拒绝loopback/link-local/metadata/public/unapproved-management range与DNS rebinding；redirect/proxy/HTTP fallback禁止。credential只保存secret reference，每target/role/协议分离、最小权限、可轮换撤销；
- production Edge是唯一持有target P4Runtime read/write identity的组件。条件gNMI使用不同只读identity与method/path allowlist；部署、server policy与负向测试三层拒绝`Set`以及未经声明的Subscribe/Get path。gNMI失效、慢或被攻陷不得改变P4readiness/current、触发effect或获得P4credential；
- TargetActor之间不得共享可写journal目录、unscoped request queue或credential handle；所有RPC/日志/audit携带stable target短reference、assignment/application generation和actor epoch。跨target confused-deputy、wrong endpoint/device_id、old assignment、same operation different target与replayed preflight必须在写前拒绝；
- NetBox/CMDB/Ansible/Nornir/API/file inventory按不可信外部输入处理：framing/size/schema/provenance/digest/cursor/timeout有界，不接受secret、raw command/template、SSH key、P4 entity或自动active标志。external source compromise只能产生candidate/HOLD，不能修改canonical registry、scope、assignment、Decision或intent；
- ONOS、Stratum controller、P4Runtime Shell daemon、厂商controller和gNOI/gNMI Set identity在production依赖/镜像/网络/credential policy中显式拒绝；Stratum仅可作为target-side server profile。发现第二primary/writer credential、未知controller连接或target endpoint被共享控制时，相关target立即停止claim/write并告警，不自动争抢或清表；
- target/fleet/operation/device endpoint、serial、IP、actor、digest不得作为无界Prometheus label或普通日志全文；UI/API/导出按scope、字段遮蔽、cursor和审计控制。设备管理页面不得嵌入SSH/Web console或第三方高权限iframe。

## 12. 分机部署与运维需求

### 12.1 最小三机部署（DEP-001）

```text
Edge
├── P4/BMv2
└── Rust Edge Agent

Control/State
├── Go Control Core
├── PgBouncer
└── PostgreSQL（实验可单实例，生产必须 HA）

Central Inference + Analysis/SOC（仅三机实验共置）
├── C++ MASI Gateway + Triton + startup-selected ONNX Runtime CPU或CUDA
├── Rust Plugin Runtime Host
├── Python Analysis Plugin
└── TypeScript/Vue 3/Vite Frontend
```

三机拓扑只用于功能、故障演练和性能基线：第三机同时承载推理与SOC是显式实验共置，可运行CPU或CUDA profile，但只能获得对应功能/性能证据，不能获得`availability-ha/v1` PASS。声明production HA时必须把Central Inference与SOC分离，并以同一exact runtime profile跨至少两个failure domain部署。

### 12.2 生产扩展部署（DEP-002）

生产可以扩展为：

- 多个彼此独立的 Edge 节点；
- 独立Central Inference pool：按exact pool generation部署C++ Gateway+Triton+显式ORT CPU或CUDA replicas；`availability-single/v1`明确接受中断且不声称HA，`availability-ha/v1`至少跨两个failure domain并具static N+1；无Edge-local或CPU↔CUDA自动fallback；
- 2 个或以上无状态 Go Control 副本；
- PostgreSQL 托管 HA，或经资格化的自建 Patroni/等价 HA + pgBackRest/等价 backup/PITR；自建时不得并存第二 PostgreSQL manager；
- 独立 Plugin Runtime Host 池，按 trust domain/tenant/资源 profile 隔离；
- 独立官方 Analysis Plugin worker 池；
- 独立 Web/SOC；
- 最小 OpenTelemetry Collector distribution、Prometheus、Alertmanager、只读 Grafana 和条件采用的 log/trace backend；这些组件使用独立最小身份且不是核心事实/授权/mutation 控制面；
- 受控 OCI registry 或可离线验证的 OCI artifact bundle；
- 可选对象存储。

### 12.3 同机约束（DEP-003）

以下边界必须位于同一 Edge 主机：

- 首期 BMv2 software profile 的 P4/BMv2 本地控制接口与 Rust Agent；一个 Edge 主机可以按 `p4-target-fleet/v1` 运行/连接多个隔离、端口唯一、资源有界的 BMv2 target actor。未来真实硬件可经专用管理网连接 Edge，但必须使用独立 hardware deployment/profile 和 mTLS，不把“远程可连”当兼容；
- Rust WAL/checkpoint/operation journal；
- target-specific barrier。

Rust Edge↔Central Inference明确是跨主机mTLS gRPC边界；不得用NFS、SMB、共享可写volume或overlay filesystem搬运in-flight feature/result或伪装WAL/route语义。Gateway↔Triton必须同一inference failure-domain单元并位于隔离低延迟网络；是否同Pod/同主机由exact deployment profile冻结。

### 12.4 OCI 与宿主机（DEP-004）

- 每个模块交付独立 OCI image 和 immutable digest；
- 记录 source commit、toolchain、dependency lock、config ID 和 SBOM；
- image/tool/dashboard/config/漏洞数据库必须登记于 `contracts/supply-chain/v1` 并满足 `SEC-SUPPLY-001`；production runtime 不从公网自更新/下载依赖；
- runtime image 非必要不包含编译器、测试数据和私钥；
- 使用非 root、read-only rootfs、cap drop、seccomp/AppArmor 等适用基线；
- P4/BMv2 特权只留在明确 Edge 角色；
- 每个容器声明 CPU、memory、PID、FD、tmpfs、WAL/spool/log disk 和并发预算；
- OOM、disk/inode full、restart loop 和证书过期必须有 fail-closed 行为；
- Edge更适合systemd/Podman/受控OCI，Control/SOC与Central Inference可以部署到Kubernetes；CPU profile使用固定CPU/NUMA/RAM资源与隔离，CUDA profile通过vendor device plugin/extended resource和已批准node labels调度；topology spread/PDB只作placement/自愿中断约束。部署平台不是核心协议或model current的一部分，首期禁止scale-to-zero和依赖autoscaler恢复SLO。

### 12.5 配置（DEP-005）

- 每个模块使用严格、版本化 config schema；
- 减少大量散落环境变量，普通配置优先文件/明确 CLI，secret 使用引用；
- deployment 必须记录 canonical config ID；
- 多机 placement、endpoint、证书、contract version，以及 model bundle/binding generation/feature/label/adapter/runtime profile digest 漂移必须阻止对应 readiness；仅比较 model ID 不足以证明一致；
- 多个 Go 副本必须使用同一 active policy、governance profile、role-mapping、plugin trust-policy 和 active-binding generation/digest；漂移时相应 mutation/activation readiness 必须失败，不能由负载均衡随机产生不同授权或插件绑定结果；
- profile/mapping 激活必须是版本化、原子、可回滚的控制面变更；回滚不得使已 stale/expired 的旧 authorization 重新有效；
- 配置变更必须区分可热加载和必须重启项；
- 配置失败不得回退到开发默认 secret/DSN。

### 12.5a Central Inference在线模型部署与回滚（DEP-INF-001）

- model bundle/repository snapshot由CI/deployment job从digest-pinned OCI/离线bundle预取到每个Central Inference节点的只读、no-symlink、大小受限cache；Gateway/Triton不持有registry push/admin credential，production推理不依赖公网/registry可用性；
- deployment manifest必须绑定model-control incarnation、rollout group、ordered shard set、expected binding/routing vector、logical pool/pool generation、availability profile、required replica/failure-domain/min-ready、每个worker/workload identity、管理员selected runtime profile、Gateway/Triton/ORT/EP、CPU/feature/thread/NUMA/RAM或CUDA/driver/GPU/VRAM、desired/current/previous exact bundle/repository、wire/optimization artifact与全部contract/profile digest、resource/termination/restart/retry profile、operation/action sequence和pool-envelope digest；mutable tag/alias只在受控发布阶段解析一次；
- 首期只支持`model-rollout-pool-generation/v1`：每个Triton instance从immutable envelope/read-only snapshot启动加载一个exact binding，`model-control-mode=none`、strict readiness、auto-complete disabled。模型/backend变化创建新pool generation，不要求重启Rust Edge/Go；Kubernetes/systemd只执行受控start/drain/stop，不拥有rollout/current；
- rollout顺序固定为pre-stage → start new generation → hardware preflight selected=observed → per-worker warmup/readback → pool selected-profile resource/rolling-capacity qualification（HA profile另验min-ready/failure-domain/N+1）→ 对一个shard Edge route withdraw/new epoch + drain + WAL buffer → PostgreSQL per-shard CAS → committed logical-pool handshake/resume → repeat → old generation drain/rollback grace/stop。Edge是唯一canonical router；Service/KServe/adapter只能在exact same generation和runtime profile选replica，不能跨generation/profile分流。old/new shard可共存且group显示`rolling_mixed`；Pod/Triton Ready不能推进current；
- 同feature/runtime major的模型只需更换repository snapshot/pool envelope并创建新generation。feature schema、wire、output contract、backend/custom operator major改变时，先部署能读取old/new schema的兼容Edge/Gateway/Go image，再逐shard切binding，最后移除旧reader；wire不兼容使用不同logical pool/profile并由Edge在drain后单路切换，不能原地覆盖repository或双写canonical Event；
- rollback创建新的ordered per-shard operation，只选择exact previous binding/pool generation；在rollback grace内可以复用仍ready且exact qualified的old pool，超过grace则从immutable snapshot重建。回滚不重放过期旧输入、不改写历史分类、不使旧policy自动恢复；previous不兼容/不可重建时保持current/HOLD，而非降级到任意旧模型；
- startup验证envelope/repository/artifact/manifest/contract/wire/optimization/runtime/resource，并执行硬件preflight；readiness证明selected runtime profile与actual EP/hardware一致、单worker已load/warmup且readback可用，pool qualification还需capacity/freshness，HA profile另需required replica/failure domain/N+1。它们都不证明PostgreSQL current；只有CAS后的Go/Edge handshake允许canonical traffic。probe只报告或拒绝，不触发profile自动选择、model-control、回滚、数据库或设备mutation；
- 正常stop旧generation必须在所有绑定shard完成route-withdraw/drain/commit或rollback grace截止后发生。Kubernetes`PreStop`/SIGTERM只作幂等兜底；强制终止/identity不明记录incomplete drain和可能gap。PDB只约束部分Eviction类自愿中断，不能替代应用rollout/N+1；
- 离线重建门禁覆盖pool/worker/failure-domain×current/previous/desired snapshot×envelope valid/expired/wrong-current×cache complete/partial/missing/wrong-digest×registry reachable/unreachable×verification bundle valid/corrupt；任一identity/artifact无法验证时replica/pool不ready并保持`HOLD`，不得从mutable repository/tag或其他node猜测current；
- production HA Kubernetes profile采用固定副本与static N+1，single profile同样不启用scale-to-zero；HPA/自定义autoscaling/KServe/Ray Serve若未来引入，必须新profile/ADR证明冷启动、容量、route/current所有权和退出路径。无论调度器如何，Edge-local与CPU↔CUDA/异构backend自动fallback仍被禁止。

### 12.6 对象存储（DEP-006）

首期不引入对象存储，模型保存在 OCI 制品或受控本地制品库。后续只有附件规模证明需要时才能启用 S3-compatible storage；届时只允许保存模型、不可变报告附件和归档大对象，PostgreSQL 保存 canonical identity/hash/URI/size，Edge WAL 和核心 effect 事实不得迁移到对象存储。

### 12.7 插件部署与 Placement（DEP-PLUGIN-001）

- 单机/三机首期将 Plugin Runtime Host 放在 Analysis/SOC 故障域；它不得与 Rust Edge 共用 P4 credential、WAL、journal、network namespace 或可写 volume；
- 生产可以按租户、trust level 或 runtime profile 扩展 Host 池，但一个 binding 必须固定 placement、host identity、artifact/config digest 和 generation；调度漂移必须阻止 readiness；
- OCI service plugin 由 systemd/Podman/Kubernetes 等受控部署层启动，Plugin Manager/Host 不得取得 Docker socket 或自行实现通用容器编排；Host-managed service 同机优先 UDS、跨机只允许 exact-profile mTLS gRPC；独立 service/Agent 通过 Manager-controlled typed adapter 直连，不为经过 Host 而增加代理跳数；
- 同机 UDS 必须遵守 `SEC-001` 的路径、owner/mode、no-symlink、peer credential、generation 和 framing 合同；每个 binding 使用独立 runtime directory/socket，禁止多个互不信任插件共享可写 socket directory；
- Wasm component 由 Runtime Host 从只读、no-symlink、大小受限、digest 校验的 artifact cache 加载；编译缓存必须绑定 Wasmtime/compiler/target/config digest，不能跨不兼容版本复用；
- 安装、qualification、activation、drain、rollback、revoke 和 cache purge 必须使用精确 plugin/revision/digest target；禁止 wildcard 删除共享插件目录或在运行中覆盖 artifact bytes；
- Runtime Host 和每个 service plugin 必须有独立 startup/readiness/liveness、资源、日志和 restart budget；初始 probe profile 为 startup 总预算不超过 120 秒、readiness 每 5 秒且连续 3 次失败停止新任务、liveness 每 10 秒且连续 3 次失败才触发有界重启、单次 probe timeout 不超过 2 秒。10 分钟内最多自动重启 5 次，超限 quarantine 至少 15 分钟；profile 可以按已资格化 runtime 收紧，放宽必须记录性能/故障证据和新 profile digest；
- production deployment 必须能在 registry/公网离线时使用已保存 verification bundle 重建已批准 revision；任何 digest/policy/revocation 无法验证的 revision 保持 `HOLD`。

统计 capability 不增加新部署模块或常驻 scheduler：Go Control 内部的 Plugin Statistics 模块拥有 schedule/run/projection，实际执行沿插件原有 Host-managed 或 direct typed placement。Web 不下载插件 asset，不挂载 plugin volume，不新增跨域 origin。扩展 Go/Host/plugin replica 时仍由 PostgreSQL exact run/binding generation 和有界 admission 防重复；Kubernetes CronJob、插件内部 cron、数据库轮询和外部 workflow engine不得成为第二 scheduler/current owner。

### 12.8 Frontend 部署与回滚（DEP-WEB-001）

- Frontend 交付为独立、不可变、content-hashed static artifact/OCI image；Web image 不包含 Node.js SSR/BFF runtime、数据库 client、P4/Edge credential 或生产 source map。受控 source map 必须与 release digest 分开存放并访问受限；
- 生产入口通过同一 HTTPS origin 路由 SPA、`/api`、`/events` 与 OIDC callback；静态服务层不能解释业务身份、scope 或 canonical state，Go 仍是业务授权与 API owner；
- `index.html`/runtime config 使用 no-store 或短缓存并带 build/profile identity；hash asset 使用 `immutable` 长缓存。发布必须原子切换 manifest，并在 rollout/rollback 窗口保留当前与上一受支持 asset set；
- Frontend 可以独立滚动和回滚，但每个版本必须通过当前/上一 Go API、session cookie、CSRF、SSE cursor/event 和持久 deep-link compatibility matrix；不兼容时只读/HOLD，不得通过 CORS、禁用 CSRF 或旧新双 API 写入恢复；
- 生产不得依赖公共 CDN、GitHub、npm registry、远程字体/图标或第三方 Web 运行时；镜像、lockfile、SBOM、LICENSE/NOTICE、OpenAPI generated digest 和 design-token digest 必须可离线重建；
- SPA readiness 只验证精确 asset/config/profile 可提供且与 Go API major 兼容；不得触发登录、mutation 或下游健康检查。回滚按 exact Web artifact digest，不能回滚 canonical server facts。

### 12.9 在线采集与中央推理热路径部署（DEP-TEL-INF-001）

- 每个Edge部署必须显式选择一个canonical telemetry source profile和一个inference hotpath profile；首期默认`telemetry-p4-window/v1`+`inference-central-grpc-batch/v1`。未配置不得自动探测“最佳backend”；同一`(telemetry_source_id,inference_shard,source_runtime_epoch)`不得同时运行两个canonical capture backend或两个logical pool generation；
- P4 aggregate部署清单固定device/role/election、P4 program/P4Info/pipeline digest、observation domain/point、counter/register/digest ID、bank/epoch/snapshot/clear方法、target/flow/window上限和Read调度。BMv2 snapshot/barrier行为与真实hardware profile分别资格化，软件PASS不自动选择硬件实现；
- PACKET_MMAP/AF_XDP只部署在专用mirror/TAP接口；记录netns、ifindex/name/MAC、driver/firmware、RSS queue、CPU/IRQ affinity、NUMA、MTU/offload、capture snaplen、ring/UMEM、sampling和interface ownership。Suricata官方经验指出AF_XDP选定接口不能同时用于普通网络，部署必须提供独立管理路径；
- `telemetry-mirror-af-xdp/v1`必须在startup探测并readback XDP_DRV/XDP_SKB、zero-copy/copy、XSK queue、multi-buffer和NIC capability；实际模式与profile不符时readiness失败。copy fallback只能使用独立profile/digest重启，不允许原地降级；
- Edge↔Gateway使用受控inference网络、mTLS和exact logical-pool service identity；per-Edge/per-shard channel、connection、in-flight、message、bandwidth、DNS/endpoint refresh、keepalive/deadline/retry固定。一个全局长寿命bidi stream不得作为全池负载均衡路径；服务发现只能返回same generation已资格replica；
- Edge/Gateway/Triton的CPU/NUMA/network placement进入共同deployment/performance profile；CPU runtime另固定ORT thread/affinity/arena/RAM，CUDA runtime另固定thread/stream、GPU/MIG（如资格化）、pinned/device memory。Gateway/Triton internal queue不能通过抢占P4 mastership/effect/readback或其他tenant无限资源取得吞吐；
- rollout或wire切换顺序固定为新pool ready/readback/qualified→Edge停止该shard新window admission→drain old in-flight并确认input/result WAL与Go ACK watermark→提升route epoch→CAS/commit到新logical pool→恢复单路→旧pool drain。失败时保持HOLD/unavailable或显式rollback exact previous，禁止双写；
- DPDK和Gateway↔Triton shared-memory extension是条件优化，不是fallback；Central Inference是首期必选，CPU或CUDA由startup envelope显式二选一。Edge-local inference、CPU↔CUDA/TensorRT/LibTorch自动fallback和跨generation/profile service routing无部署profile。

### 12.10 BMv2 无状态防火墙部署（DEP-P4-FW-001）

- 首期唯一软件实现与资格 target 为 digest-pinned `simple_switch_grpc` + v1model 软件 profile；P4 program、BMv2 JSON、P4Info、port map、table capacity、priority/default-action、counter width/mode、selector/bank layout 与 BMv2/p4c exact revision 必须进入同一 deployment/profile digest。BMv2 只用于功能、恢复和软件容量资格，不得声称硬件 line-rate、生产级转发能力或真实 ASIC 兼容；
- Edge 与 BMv2 使用受控本地或专用管理网络，只有 Edge 长期持有 production P4Runtime StreamChannel/mastership/read-write identity；Go、Frontend、Plugin Host、Analysis、testkit、Grafana 和 host firewall 无该凭据。Edge startup/readiness 必须读回 pipeline/P4Info、selector、active revision、capacity budget 和 journal fence；漂移时 mutation readiness 失败；
- 初始 profile 必须预留并分别限制 response overlay、两个 baseline bank、selector、direct/eligible counter 和 telemetry aggregate 资源；总容量、每类配额、最坏规则展开倍数和控制面 read/write budget 不得靠运行时自动借用或超配。达到上限时在创建 intent/外部写之前稳定 `HOLD/capacity_exceeded`；
- baseline activation 和 rollback 使用 `REL-P4-FW-001` 双 bank 流程；旧 bank 只在 current/previous CAS 和 rollback grace 完成后按 exact revision 清理。P4 pipeline/P4Info 更新属于独立 R3 change-management/deployment 操作，必须先停止 effect claim/write、排空 journal、切 pipeline、重建 baseline/overlay 与 observation epoch；不得伪装成普通 firewall policy activation；
- Linux UFW/nftables/iptables 只能按独立主机加固职责保护 Edge/Control/SOC 服务端口，不能作为 BMv2 dataplane fallback、策略编译目标、规则事实源或 outcome oracle。部署必须明确 packet path，避免同一测试/生产流量先被 host filter、bridge、namespace 或 eBPF/XDP 意外截断；
- 硬件 target、PSA/TNA、IPv6 enforcement、stateful connection tracking、NAT、rate limiting/meter、VLAN/tunnel-aware policy 或其他 action 只有建立新 `p4-stateless-firewall` target profile、契约/ADR、容量/原子性/计数语义和真实设备 evidence 后才能启用；不得把 BMv2 v1model plan 直接下发到不同架构。

### 12.11 多 Target/Fleet 部署（DEP-TARGET-FLEET-001）

- 三机首期必须在Edge节点部署至少2个隔离BMv2 target用于功能/故障证据，并按`DEC-001`冻结的N-target profile完成最大矩阵；每个target固定进程/容器/endpoint/device_id/role/P4 program/P4Info/JSON/port map/CPU-memory-cgroup/WAL-journal目录和test dataplane接口，不共享device_id、端口、可写目录或未分区资源；
- production按site/failure domain将target显式assignment到Edge pool；一个target同时只有一个active assignment，一个Edge只接收profile允许数量。Go不自动发现/抢占设备；新增、迁移、drain和retire使用typed operation与audit。load balancer/service discovery不得把P4Runtime/gNMI endpoint随机路由到不同设备；
- P4 management网络与dataplane、mirror、数据库、OCI和用户网络分离；ACL只允许assigned Edge到exact P4Runtime endpoint，条件gNMI只读identity单独放行。Control/Web/Plugin/observability/traffic runner无production device credential；
- Edge supervisor/actor使用独立startup/readiness/liveness：supervisor healthy不表示全部target ready；一个actor不ready只从该target mutation/source scope摘除。deployment不得因单target失败重启全部actors；restart/backoff/quarantine与per-target/global资源预算固定；
- fleet rollout的target-set、wave、parallel/failure policy来自Go durable operation，systemd/Kubernetes/Ansible/Nornir只执行进程/离线provisioning动作，不决定下一wave、current或rollback。部署平台标签/NetBox group不能在operation开始后动态扩展target set；
- `target-gnmi-readonly/v1`只有exact target/Stratum implementation和OpenConfig model/path矩阵通过后部署；首期BMv2可以完全不启用gNMI。gNOI、gNMI Set、端口/VLAN/QoS/routing、设备OS/certificate/reboot和自动拓扑发现不存在fallback；业务需要时先提升基线；
- Stratum target-side profile、NetBox candidate import与Ansible/Nornir offline adapter都是可移除条件组件；完全移除后canonical registry、P4Runtime effect和检测主链仍完整。ONOS/厂商controller不进入production deployment graph。

## 13. 可观测性需求

### 13.1 Metrics（OBS-001）

所有模块必须暴露低基数 Prometheus metrics，至少覆盖：

- input/output throughput；
- p50/p95/p99 latency；
- queue/WAL/spool/backlog；
- retries/timeouts/unknown；
- P4 mastership/generation/readback；
- inference batch/decision/error；
- model repository/artifact verification/load/backend-Session/warmup/Gateway-probe/startup-to-min-ready、availability profile、required/ready replica及HA适用的failure-domain/N+1、dynamic batch/instance group、selected CPU或GPU资源与network、per-shard route-withdraw/drain/unavailable/buffer/gap/replay、deployment action/termination、readback/CAS/commit/resume/restart/quarantine/rollback、group mixed duration、incarnation/route/pool/binding-generation drift、retry/duplicate/fenced late result与numeric/label/adapter/wire/optimization rejection；
- PostgreSQL pool/transaction/lock/WAL/replication；
- effect proposal backlog/age、decision latency/result、stale/expired、authorization failure、idempotency conflict 和 maker-checker violation；
- plugin admission/qualification/activation、queue/in-flight、execute latency、reject/timeout/resource exhaustion、circuit/quarantine、generation drift、revoke 和 sandbox denial；
- plugin statistics schedule/run/queue/execute/validate/project/API/render、quality/truncation/stale/revoke、oversize/cardinality reject；只使用低基数 kind/result/quality/reason/bucket，不使用 plugin/result/artifact/dimension value identity；
- Agent LLM/tool/A2A call、budget、`analysis_outcome=limited|insufficient_evidence|failed` 与 grounding reject；
- Frontend build/API/profile mismatch、asset/chunk load failure、route-group navigation/Core Web Vitals、JS error、SSE gap/reconnect/polling-recovery、HTTP saturation 和 stale duration；
- CPU/RSS/FD/thread/goroutine；
- certificate expiry 和 config drift。

event/proposal/decision/operation/workflow/task ID、actor、IP、prompt hash、完整 URL/query/object ID 等高基数字段不得作为 metric label。治理 metric 只允许 bounded risk class、status、effect kind 和 reason code 等枚举 label；Frontend 只允许 bounded route group/browser engine/build/profile/status，不记录用户、deep-link 或筛选值。每个 metric 最多 10 个 label；枚举 label 默认最多 32 个允许值，target/instance 等部署维度也必须受 inventory profile 硬上限约束。每个 scrape endpoint 默认 active series 上限 10,000，核心热路径服务上限 5,000；超过时必须通过预声明 overflow series 和告警有界收敛，不能让 exporter OOM。任何放宽必须在 deployment profile 中给出 TSDB/内存证据；高基数 identity 只能进入受限 trace/log 字段（依据：[OpenTelemetry metrics](https://opentelemetry.io/docs/concepts/signals/metrics/)）。

### 13.2 Trace 与日志（OBS-002）

- 使用 OpenTelemetry 或等价标准传播 trace/correlation ID；
- 日志必须结构化、可轮转、受容量和保留限制；
- Analysis trace append-only、分页、脱敏且与具体 run/attempt 绑定；
- Proposal → Decision/Policy → Intent → Attempt/readback 必须通过 trace/correlation ID 和持久 identity 可追踪；结构化日志不得成为唯一审计事实；
- trace 不保存 chain-of-thought，只保存节点、工具、耗时、状态、受控 hash 和 redacted summary；
- readiness、liveness、startup probe 必须语义分离；
- readiness 失败只阻止流量或置 HOLD，不自动执行 mutation；startup 成功前不得运行 readiness/liveness，liveness 只判断进程自身是否还能取得进展，不能把下游依赖不可用当作进程死亡。该语义与 [Kubernetes probe contract](https://kubernetes.io/docs/concepts/workloads/pods/probes/) 保持一致，即使部署在 systemd/Podman 也不得改变；
- error/unknown/mutation trace 必须 100% 保留在受控容量内；正常流量采样率由 deployment profile 固定为 1%–10%。采样、exporter queue、batch、retry 和丢弃必须有上限及 drop reason，telemetry exporter 失败不得阻塞核心热路径或改变业务状态。

### 13.3 告警（OBS-003）

至少为以下情况建立告警：

- WAL/spool/backlog 达阈值；
- Event/Effect/Analysis projection lag；
- unresolved unknown/outbox age；
- unresolved proposal backlog/age、authorization stale/expired spike、重复决策冲突和 role/profile drift；
- P4 generation/mastership mismatch；
- PostgreSQL replication/archive/backup/pool/lock；
- disk/inode/memory/FD；
- certificate expiry/mTLS failure；
- config/model/contract drift；
- model desired/loaded/current mismatch、同shard双current、selected/observed runtime或pool generation/worker readback drift、HA profile的min-ready/N+1/failure-domain不足、single-replica/full-pool unavailable、unqualified/revoked bundle、startup/readback/CAS timeout、rollout_failed_mixed、buffer overflow、rollback unavailable与taxonomy/adapter drift；
- plugin artifact/manifest/config/capability/active-generation drift、revoked digest 仍运行、双 active、sandbox/cgroup enforcement failure；
- Plugin Manager/Host/service/Wasm 各层 timeout、typed error、quarantine、restart，以及 Analysis 的受限 outcome/error rate；
- plugin statistics run stuck/queue saturation、validation conflict、current stale/revoked、projection lag、oversize/cardinality/XSS-display payload reject 与 browser render budget regression；告警只通知/置 HOLD，不自动重跑无界任务或修改核心事实；
- Frontend asset/chunk 404、build/API/profile mismatch、SSE gap/polling-recovery 持续、JS error spike、session/CSRF failure 和 Core Web Vitals/bundle budget regression；
- restart loop 和 readiness 持续失败。

### 13.3a 在线模型可观测性（OBS-INF-001）

- PostgreSQL/Go 保存 per-model revision、per-shard binding 与 rollout operation 的 canonical fact；Prometheus 只暴露低基数 model family/runtime profile/rollout state/reason 聚合，禁止 model/bundle/input/output/label/flow identity、artifact URI 或 digest 作为 label；
- 每个 `InferenceResult` 与 Event 可通过受限字段追溯 exact shard、model/binding/pool generation、logical pool、worker runtime/attempt、feature/label/adapter/runtime/profile digest；结构化日志只记录短 hash/reference 和稳定错误，不记录完整输入、原始 tensor、模型权重或敏感流量；
- Model Operations UI分开显示rollout desired revision、model-control incarnation短引用、ordered shard set、logical pool/current-new-previous generation、管理员selected runtime、hardware preflight与observed EP/resource、availability profile、required/ready replica、capacity及HA适用的failure-domain/N+1、每shard current/previous、route-withdraw/draining/pool-starting/ready/readback/CAS/resume-pending/resumed/quarantined、envelope/action/gap reference、qualification和group`rolling_mixed|rollout_failed_mixed`；Pod/Triton Ready不得显示为current，不提供auto-select、candidate shadow、runtime load/unload或fallback控件；
- 告警/自动化只能通知或暂停尚未开始的后续 shard；不得让 Prometheus/Grafana/Alertmanager/MLflow/KServe/Triton/systemd/Kubernetes 自动改变 PostgreSQL binding、policy、Event 或 P4 effect。任何 rollback 继续使用 Go exact operation/per-shard CAS/readback；
- 离线/rehearsal comparison 必须绑定 frozen input、current/desired revision、feature/label/adapter/profile 和有效 coverage，区分 numeric、taxonomy、decision、quality、latency 和 error difference，并显示`level/applicability/result/qualification`与claim scope；样本不足、gap、不同 contract 或 rehearsal 不得显示为“可直接生产切换”；
- `model-runtime-central-cpu/v1|model-runtime-central-cuda/v1`/`model-rollout-pool-generation/v1`必须固定desired/loaded/current generation age、selected/observed profile、hardware preflight、startup分阶段、availability profile及其required/ready replicas与适用的failure-domain/N+1、Edge↔Gateway RTT/retry、Gateway/Triton/runtime dynamic queue、readback/commit freshness、route-withdraw/drain/unavailable/buffer、restart/quarantine、per-shard/group rollout、fenced-late/duplicate、current availability，以及active/warming/draining/replay的CPU/RSS或VRAM/GPU、network/queue单位、bucket、采样、缺失语义和告警阈值；missing/unknown/stale不得编码为0或正常绿色。exact model/shard/worker/digest只进入受限API/log/trace reference，不进入Prometheus label。

### 13.4 规则观测与成熟可观测性组件（OBS-RULE-001）

- per-rule identity、累计值、rollup、首次/最后 observed-hit window 和 outcome evidence 只进入 PostgreSQL/Go 授权 API；rule/effect/operation/entry digest、match key、IP/prefix、actor 和五元组禁止作为 Prometheus label。规则数量增长不能线性制造 Prometheus series；
- Prometheus 只暴露低基数聚合：sweep/read RPC count/duration/bytes/error、entities sampled、queue/gap/stale/reset/invalid/not-measurable count、active/readback-confirmed/hit-observed/no-hit-with-traffic/no-eligible-traffic/outcome-verified rule count，以及 full-sweep/freshness age；label 仅使用受 profile 硬限的 target group、table profile、status/quality/reason，不使用任意 rule ID；
- OpenTelemetry SDK/Collector 可以复用来接收、批处理、脱敏和导出 metrics/log/trace；Collector queue/drop/retry 有界且失败不阻塞 Edge/Go/P4、不中断 PostgreSQL rule facts。Collector component stability/version/digest 必须逐项资格化，不能因通用 Collector 可扩展而加载任意 production component；
- Grafana 可以复用 state timeline、time series、Top-N 聚合和到 MASI SPA 的只读 data link；datasource 使用最小只读账号/聚合 view，禁止 Grafana Actions、POST/PUT/DELETE data link、直连 P4/Edge、核心 schema 写权限或将 dashboard annotation 当审计事实；
- Alertmanager 只对 observation sweep stale、gap/reset spike、readback drift、capacity/saturation 和 exporter failure 做分组、去重、路由、抑制和静默；silence/ack/notification 不是 Incident、Effect、规则有效性或授权事实，不得触发设备 mutation；
- dashboard/alert 必须分别显示观测 coverage、数据新鲜度、阈值/窗口和 `no eligible traffic`；缺数据、NaN、gap 或 backend unavailable 不得变成绿色 0。规则统计告警只能形成运维诊断/工单引用，不能自动 disable/delete rule。

### 13.4a BMv2 无状态防火墙可观测性（OBS-P4-FW-001）

- PostgreSQL/Go 保存 firewall policy revision、normalized/compiled plan digest、active/previous bank、selector generation、response overlay、activation operation、exact readback 和 observation/outcome 的 canonical fact；BMv2 CLI、Edge memory、日志、Prometheus 或 Linux firewall state 均不是事实源；
- Frontend/API 必须提供 activation state timeline，至少区分 `planned/preflight/inactive-writing/inactive-verified/selector-switching/selector-verified/committing/applied/unknown/reconciling/rollback/failed/HOLD`，并显示当前/上一 revision、default action、target/P4Info/application generation、容量、过期和短 digest reference；不得把 Write ACK、process healthy 或 bank populated 显示成 policy active；
- Prometheus 只暴露低基数聚合：active target/policy count、overlay count、bank utilization bucket、compile/preflight reject、write/readback/selector/CAS latency/result、reconcile/drift/expiry backlog 和 rule evidence state count；policy/rule/entry/operation identity、digest、match、IP/prefix、五元组和 actor 禁止作为 label；
- 告警至少覆盖 selector/数据库 current 不一致、active bank partial/readback drift、default-action drift、capacity approaching/exceeded、overlay expiry backlog、reconcile deadline、mastership/P4Info/generation drift 和 host-filter contamination；告警只进入运维诊断/HOLD，不自动 flip selector、delete/restore rule 或创建 Decision/Intent；
- rule installation/match/outcome 继续遵守 `OBS-RULE-001`，并分别显示 baseline 与 response overlay 来源；same packet 命中多个 stage 时不得重复宣称攻击阻断，默认 action、permit-and-continue 和后续 forwarding result 必须由独立 packet path/oracle 解释。

### 13.4b 多 Target/Fleet 可观测性（OBS-TARGET-FLEET-001）

- PostgreSQL/Go API是target identity/lifecycle/assignment、fleet parent/child/wave/current与audit唯一事实源；Edge memory、P4Runtime/gNMI连接、NetBox、Ansible inventory、Prometheus/Grafana或Kubernetes状态不能替代；
- Managed Targets UI显示per-target desired/observed profile、actor/mastership、application generation/P4Info、source/effect/readback freshness、queue/resource和drift；Fleet Operations显示ordered wave与完整child vector。parent `applied`只由全部required child applied得出，unknown/reconciling/blocked/not-started/mixed不可编码为0或绿色；
- Prometheus只暴露低基数聚合：registered/active/draining/quarantined target count、actor/arbitration/readiness/drift/reconnect、P4 RPC/queue/fairness、fleet parent/child/wave status count和gNMI read-only queue/gap。`target_id/device_id/endpoint/serial/operation/digest/IP`禁止作为label；site/target-group/profile/status/reason必须受registry硬上限；
- 告警至少覆盖duplicate identity/assignment、同target多个primary/writer迹象、actor/registry generation drift、pipeline/P4Info/capability drift、target unreachable、journal/reconcile age、fleet wave stuck/partial/unknown、fairness/starvation、gNMI Set attempt/read-only freshness和外部inventory drift；告警只通知/置HOLD，不自动reassign、开wave、写P4或修改registry；
- target/fleet详情通过Go稳定cursor和受限trace短reference关联Proposal→Decision→parent→child Intent→Attempt→Edge actor/journal→P4 readback→rule observation；日志/trace不保存credential、完整endpoint、raw gNMI payload、P4 entity bytes或高基数target集合。

### 13.5 流量回放可观测性（OBS-TRAFFIC-001）

- traffic runner 的结构化结果必须绑定 run/attempt、fixture/transformation/profile/environment/P4Info digest，并分栏保存 requested、sender、test ingress、DUT、counter、outcome oracle 与 detection observation；不得只保留命令 stdout、单个 `packets sent` 数字或截图；
- metrics 只暴露低基数 run mode/target class/result/reason 聚合、active run、actual/requested rate bucket、sender/DUT/capture drop、queue/resource/cleanup/quarantine；fixture/run/PCAP/地址/五元组/digest 不进入 Prometheus label；
- 测试报告必须显示 mode、target class、software-only 限制、方向/rewrite/timing/impairment/offload profile、ground-truth coverage 和各证据层状态。`sender_accepted`、`counter_hit`、`detection_observed` 与 `outcome_verified` 使用不同字段和文案；缺少观测点显示 `not_measured`，不显示 0 或绿色成功；
- replay 运行环境、capture/oracle 或 cleanup 异常必须告警并阻止同宿主后续资格运行；告警只管理 test runner，不触发 production Incident/effect/P4 mutation。

### 13.6 在线遥测与推理热路径可观测性（OBS-TEL-INF-001）

- telemetry source必须分层暴露低基数metric：P4 snapshot/read/clear/epoch result与duration，digest generated/received/acked/drop/gap，PacketIn/mirror NIC/kernel/ring/user drop，source sequence/gap/reconnect，window open/final/late/partial/quality，sampling coverage bucket；一个`received`或`acked`总数不能显示为完整采集率；
- hotpath必须暴露Edge network-batch/in-flight/high-critical watermark/oldest age、serialize/mTLS/RTT/retry/dedupe/fence/error、input/result WAL bytes/age/fsync/replay、Gateway admission/quota、Triton dynamic queue/batch/backend、selected CPU core/thread/NUMA/RAM或GPU utilization/VRAM、Edge→Go batch/retry、DB ACK age，以及反向backpressure持续时间与reason；
- copy/allocation观测必须区分Rust decode→feature、Protobuf serialize/network、Gateway input/adaptation、Gateway↔Triton、host↔device、output/result与Go gRPC serialization；actual copy bytes无法由runtime直接提供时，资格benchmark使用instrumented build/eBPF/perf或等价受控方法并在production只保留低开销聚合，不得默认为0；
- source/window/result exact identity、IP/五元组、model/input/output/content digest只进入授权API或受限trace短reference，不进入metric label。允许label仅为受限source profile、target group、stage、quality/status/reason、backend/mode和bucket；series上限继续遵守`OBS-001`；
- dashboard必须同时显示source coverage、snapshot consistency、watermark/finality、capture/drop/gap、Edge WAL/gRPC、Gateway/Triton/selected CPU或GPU资源/ACK backlog、pool availability及HA适用的N+1和end-to-end packet/window→Event latency；missing/NaN/not-covered/not-measurable不得编码为0或绿色healthy。AF_XDP页面显示actual XDP/bind/copy mode；
- 告警至少覆盖snapshot不一致、source epoch频繁变化、digest/PacketIn/capture drop、window late/gap/partial、WAL/gRPC/Gateway/Triton/selected compute/Go ACK high watermark、replica/full-pool unavailable、HA profile的failure-domain loss、deadline/invalid/fenced/duplicate spike、copy/profile drift、fallback尝试和end-to-end p99/coverage未达门槛。告警只触发运维诊断/HOLD，不自动切backend/model、改变sampling、重启P4 pipeline或创建effect。

## 14. 模块化开发与测试需求

### 14.1 目录结构（TEST-001）

目标仓库结构：

```text
vnext/
├── contracts/
├── p4/
├── edge-rs/
├── infer-cpp/
├── control-go/
├── plugin-host-rs/
├── analysis-py/
├── ml-py/
├── web/
├── db/migrations/
├── testkit/
└── deploy/
```

`testkit/` 只能提供共享契约、golden、fixtures、fake server 和故障注入，不得包含被测模块的业务实现。

### 14.2 语言级验证（TEST-002）

- Rust：format、clippy、unit/property、sanitizer/Miri（适用时）、benchmark；
- C++：format/lint、GoogleTest 或等价框架、ASan/UBSan/TSan、Google Benchmark 或等价；
- Go：format、vet/staticcheck、unit、race、benchmark、真实 PostgreSQL integration；
- Python：3.12+ type/lint、`unittest`、contract/golden/fault；当前仓库继续遵守不用 pytest 的约定；
- TypeScript/Vue：strict typecheck、Vue lint、unit/component、accessibility、production build/bundle budget、Playwright E2E；
- Contracts：schema lint、breaking-change check、跨语言 golden bytes。

### 14.3 Module Black-box E2E（TEST-003）

每个可部署模块必须使用实际binary/OCI及其真实runtime以公开边界独立启动和运行测试；被测模块自身不得由fake/mock替代。PostgreSQL migration/restore与Offline ML Artifact Pipeline作为非在线但独立的qualification target接受同等级证据门禁。模块黑盒测试可以用契约一致的fake模拟尚未接入邻居，但该证据只证明被测模块边界，不能直接成为对应pairwise或system E2E PASS。测试不得导入内部实现或通过stdout作为主要结果：

| 模块 | 黑盒输入 | 黑盒输出 | 必验内容 |
|---|---|---|---|
| P4/BMv2 | `traffic-replay/v1` generated/synthetic/PCAP/live-session fixture、P4 config、normalized firewall plan | ingress/egress packet oracle、window aggregate/counters/registers/digest/table/selector/bank state、结构化 replay result | direction/rewrite/timing/impairment、真实包、baseline 双 bank/selector、response overlay、priority/default/fragment、per-entry direct/eligible counter、target/window bank/epoch/snapshot/clear、digest best-effort/drop、action/result oracle、sender≠DUT、资源/清理、规则、软件 target 容量 |
| Rust Edge | 多 Fake/real P4Runtime target + qualified mirror source fixture + Fake Central Gateway/Go | per-target actor/session/journal/source、input/result WAL、final TelemetryWindow、InferenceBatch/Result、EffectResult、RuleObservationBatch、canonical ACK | target assignment/actor isolation/arbitration、1/2/N公平调度、source profile/snapshot、Digest/PacketIn sample、event-time/watermark/late/quality、IPv4/IPv6/fragment/flow direction、central gRPC batch/retry/dedupe/fence/backpressure、three-stage WAL/replay/crash recovery、counter reset/generation、single owner/writer |
| Central Inference | golden gRPC batch + valid/invalid pool envelope/repository + Fake Edge/Go + real Gateway + pinned Triton/selected ORT CPU或CUDA profile | InferenceResult、Gateway admission、Triton batch/backend result、startup/readiness、hardware preflight、`GetLoadedModel/GetPoolStatus` readback | mTLS/framing/size/tensor/digest、人工profile选择与selected=observed、CPU thread/NUMA/RAM或CUDA/driver/GPU/VRAM、batch/deadline/quota/retry conflict、dynamic batch/instance group、numeric/copy/I/O Binding、feature/label/adapter/wire、exact repository/startup binding、loaded≠current、pool/worker/route/generation fence、no automatic fallback、性能、sanitizer；fake Gateway或只测ORT/Triton不得PASS |
| Go Control | 多 Fake Edge/target + Inference/deployment adapter/Plugin Host + real PostgreSQL + Fake OIDC context | TargetRegistry/FleetOperation/Event canonical ACK/Incident/Proposal/Decision/Effect/FirewallPolicy/RuleObservation/ModelControl/PluginControl/PluginStatistics/API | target candidate/identity/assignment/control-incarnation、parent+child/wave/partial、result→Event identity/digest/idempotency、commit-before-ACK/replay、risk/scope、maker-checker、baseline revision/R3 activation/default action/current-previous CAS、response overlay expiry、per-shard incarnation/routing/action/readback/CAS/commit、stale/HOLD、unknown/reconcile、规则三层证据/公式/retention、model qualification/pool-generation rollout/recovery/rollback、plugin activation/revoke、statistics freeze/run/idempotency/validate/current-history/auth、migration |
| PostgreSQL State/Migration | migration set、旧/新 schema fixture、backup/WAL | schema、恢复点、canonical 引用链 | checksum、expand/contract、锁/statement timeout、target/fleet parent-child/assignment、plugin statistics run/artifact/current unique/CAS/retention约束、failover、PITR/clone/rewind target/model-control incarnation轮换、隔离 restore、RPO/RTO、连接预算 |
| Rust Plugin Host | signed/invalid manifest、Wasm/service/statistics conformance fixtures、Fake Manager | admission/lifecycle/typed result/metrics | digest/identity、kind/version、statistics input/artifact/display contract、capability、budget、fence、sandbox、drain/revoke、zero core access |
| Python Analysis Plugin | frozen bundle + Fake LLM/MCP/A2A | AnalysisArtifact | legacy capability matrix、grounding、budget、typed `analysis_outcome`、zero core mutation |
| Frontend | OpenAPI mock/real Go + Fake OIDC/session/SSE；真实production Web artifact与浏览器 | page/user behavior + accessibility/performance evidence | generated client、URL/deep-link/cursor、all UI states、Managed Targets identity/assignment/drift、Fleet target×stage/wave/partial矩阵、baseline policy diff/default/coverage/shadow/conflict/dual-bank activation、response overlay expiry、exact authorization context、规则安装/命中/结果分层与无流量/失真、Model Pool desired/selected/observed CPU/CUDA、single/HA、资源、exact generation/replica、HA适用的N+1、逐shard rollout/readback/CAS/full-pool gap/no-fallback与独立E2E evidence、stale/unknown、original-operation query、plugin qualification/exact-binding admin、plugin statistics fixed renderer/current-history/quality/truncation/XSS-CSP-CSV injection负例、analysis display/Agent non-executable、session/cache isolation、SSE gap/polling-recovery、bundle/browser/a11y；dev server或静态截图不得PASS |
| Offline ML Artifact Pipeline | frozen train/validation/test digest、seed、toolchain、model profile | digest-pinned model bundle + qualification manifest | reproducibility、feature schema/label taxonomy/output adapter、跨语言 numeric golden、NaN/Inf/OOD、离线 replay quality、性能/资源、current/previous reader 与 rollback compatibility |

### 14.3a 在线模型独立门禁（TEST-INF-001）

Online Model能力必须在Offline ML、Central Inference、Go、PostgreSQL、Edge和Web各自边界形成证据；至少证明：

1. `contracts/model/v1`的bundle/repository manifest、feature schema、label taxonomy、output adapter、pool generation、per-shard binding/rollout/envelope/worker+pool observation schema lint/breaking/golden通过；Python/C++/Rust/Go/TypeScript对identity、class order、score、threshold、unknown/OOD/abstain和错误一致；
2. single-label、multi-label、anomaly-score/open-set fixture，以及 label 增加、弃用、重命名、合并/拆分、ID 重用负例和 unknown-reader matrix 通过；新增/未知 label 永不继承旧 effect eligibility；
3. 同一 input+bundle+config 在允许 numeric tolerance 内确定；覆盖 NaN/Inf/OOD、错误 shape/dtype/opset、unknown major、digest/metadata/feature/class-order/adapter/runtime/device mismatch、超限 artifact/batch/resource 和未资格 custom op；
4. 每个 Triton instance只从exact pool envelope/read-only repository snapshot启动加载一个binding；`model-control-mode=none`、strict readiness、auto-complete disabled、Gateway↔Triton probe和`GetLoadedModel/GetPoolStatus`全部exact，runtime load/unload/repository poll/candidate/shadow与mutable version选择被负向测试拒绝；
5. durable rollout operation、new pool generation startup、所选availability profile的required replica/min-ready（HA另含failure-domain/N+1）qualification、ordered shard drain/bounded buffer、logical-route switch、loaded readback、CAS current/previous和resume全链通过；并发rollout/rollback、旧envelope、timeout/response loss、Central/Go/Edge/PostgreSQL crash/failover、late result均保持每shard唯一current；
6. current→new→previous rolling rollback 只选择 exact qualified compatible revision；partial artifact、cache/path/symlink、tag/alias drift、previous revoked/unavailable、feature/runtime major mismatch 均 fail closed，历史 Event 不改写、不跨 generation 拼接；
7. 每个声明支持的CPU/CUDA runtime profile分别以真实Gateway+Triton+ORT启动，`PERF-INF-001`的cold/warm startup、dynamic batch/instance group、steady/peak/rolling capacity、single-replica/full-pool outage、network、drain/buffer、readback/CAS/resume/rollback/saturation/soak通过；选择HA时另验N+1与failure-domain loss，选择single时明确不声称HA；pre-stage/optimization/实例控制不阻塞Edge P4职责，资源超限停止后续shard；两种profile证据不可互相继承；
8. current/previous Edge/Gateway/backend/Go reader、central gRPC wire、持久化旧Event与Web projection compatibility matrix通过；不兼容feature/output/backend切换按`DEP-INF-001`新pool generation执行；
9. Model Operations UI显示exact bundle/qualification、desired/selected/observed CPU或CUDA、hardware mismatch、single/HA availability、资源、CPU/CUDA独立E2E evidence、desired rollout、ordered shard set、per-shard current/previous、drain/start/ready/readback/CAS/resume、mixed/failed状态与error，禁止`auto`、mutable tag、deployment-ready-as-current、隐藏mixed rollout和无readback成功；
10. `model-runtime-central-cpu/v1`与`model-runtime-central-cuda/v1`均为首期可选但互斥的startup-bound profile；管理员选择、hardware preflight、actual EP/resource readback和startup fail-closed通过，CPU↔CUDA自动选择/切换及所有backend fallback负例通过。TensorRT只按ADR-0009作为未来显式资格化的新generation条件运行，LibTorch与其他未登记backend稳定拒绝；外部registry/KServe/Ray/TF Serving/MLflow control plane断开或删除后，当前qualified pool能离线重建且canonical binding不变。
11. `contracts/model/v1/golden/` 中每个 vector 固定 canonicalization/profile version、input bytes、expected bytes/digest/status/error、class order、absolute/relative/ULP tolerance 与 NaN/Inf/signed-zero 规则；Python/C++/Rust/Go/TypeScript runner 命令和 result digest 写入同一 evidence envelope，禁止各语言复制后独立维护 expected data；
12. `fault-evidence/v1`为operation创建、pre-stage、新pool start、worker load/warmup/readback、所选availability profile的min-ready（HA另含N+1/failure-domain）qualification、per-shard drain/buffer/CAS/resume、old-pool stop和rollback各注入点固定initial binding/pool/worker vector、预期durable facts、允许restart/CAS次数、canonical result数和reconcile deadline；每个case证明每shard零或一次逻辑切换且无双current；
13. 多节点测试至少覆盖两个 shard 依次滚动、一个 node 替换/重启、旧 node late readback、partial rollout、ordered-set drift、buffer overflow、rollout_failed_mixed 和 rolling rollback；每个 shard 只在自身 exact readback/CAS 后恢复，old/new shard 可共存但不得跨 generation 拼接或让 mixed-sensitive policy/effect继续；
14. 生产no-DB-credential棋盘与PostgreSQL deny-role fixture必须证明Gateway/Triton/backend/Edge/plugin/Offline ML/deployment adapter无法写canonical Event/effect/model binding；taxonomy golden证明未显式绑定label默认拒绝。Offline/rehearsal comparison的四维资格字段、claim scope和digest阻止其导入生产current/effect evidence；
15. 首期new/old pool generation短时并存必须证明隔离、所选profile的双generation CPU/RAM或GPU/VRAM及network容量、logical-route切换、readback/fence和crash matrix；同一endpoint动态多版本、runtime load/unload、CPU/CUDA混合generation与weighted traffic split必须稳定拒绝，不能用未声明路径获得PASS。
16. raw-online 与 preoptimized-offline 两种 optimization profile 分别覆盖 exact artifact/tool/options/ORT/EP/device/CPU-feature、cache hit/miss、无 silent fallback、分阶段 startup timing、thread/arena/NUMA/spinning 和资源峰值；wire profile 漂移在 Session 创建前拒绝；
17. Edge唯一路由、route epoch、WAL pending/gap/resume、same-generation replica retry/dedupe、deployment action同key同digest幂等/异digest冲突、graceful/forced termination、CAS后handshake丢失、restart/quarantine均通过；任何路径不产生双route、第二pending queue、fallback或deployment-ready-as-current；
18. 无损failover保持model-control incarnation；PITR/restore/clone/rewind先轮换incarnation并阻断writer/ingest，旧pool envelope/readback/action/handshake与同数字generation均被fence，再以newoperation/newpool/binding generation逐shard重验；rolling capacity同时计入active/warming/draining/replay并达到冻结绝对门槛。
19. 声明production HA的CPU或CUDA same-generation pool必须跨profile冻结的failure domain并通过replica选择、单replica crash、整failure-domain loss、stale endpoint、connection reset、duplicate/conflicting result和全池outage；`availability-single/v1`的实验/开发或单域部署无论域内是1个还是多个replica，都必须明确显示无failure-domain HA资格。全池outage只有bounded WAL/backpressure/HOLD/gap，P4继续且无Edge-local、另一CPU/CUDA profile或其他backend/model inference。

### 14.4 Frontend 独立门禁（TEST-WEB-001）

Frontend Module Complete 必须同时证明：

1. `web-spa/v1`、`web-browser/v1`、`web-performance/v1`、OpenAPI source/generated digest、design tokens、状态语义和所有资源上限已冻结；未知 API/profile major、生成器漂移和 browser target 漂移稳定拒绝；
2. unit/component tests 覆盖 query key、cache/session 清理、format/canonical adapter、所有 UI 状态、键盘/focus、dialog/drawer、error recovery 和 reduced motion；异步断言使用 framework/web-first wait，不使用固定 sleep；
3. Playwright在资格化Chromium/Firefox/WebKit engine上覆盖登录/session expiry、Overview下钻、Event→Incident→Evidence、Analyst proposal、Operator approve/reject/stale/step-up、原operation恢复、P4 readback、Rule Effectiveness的hit/no-hit/no-traffic/reset/stale/not-measurable/outcome层次、Model Pool desired/selected/observed CPU/CUDA与mismatch、single/HA、rollout/readback/E2E evidence、`auto/fallback`负例、Analysis unavailable、plugin exact-binding/revoke、Plugin Statistics current/history/quality/coverage/truncation/unsupported/revoke和Auditor只读；
4. Fake/real Go 返回 duplicate、乱序、cursor gap、generation/profile change、partial、oversize、429/5xx/timeout/断网时，SSE/polling/cache 收敛且不创建第二 mutation、不越界重试、不显示未证实 current；
5. WCAG 2.2 AA 自动扫描、键盘/焦点/zoom/reflow/屏幕阅读器人工复核和 ECharts 文本/表格替代通过；危险动作不只靠颜色、默认值、单键或通用确认；
6. production build 的 bundle/route/CWV/heap/DOM/chart/request/cache/SSE benchmark 与 soak 通过 `WEB-PERF-001`；route 反复进入/退出、scope/session 切换后没有持续 heap/listener/timer/chart/query 增长；
7. CSP、同源 session/CSRF、logout/cache purge、无 token/browser secret、无 remote runtime、无 Service Worker、恶意 deep-link/XSS/oversize 与 route-visibility-not-authorization 通过安全测试；插件统计另证明 HTML/SVG/Vue/ECharts/Vega/URL/expression payload均未执行，CSV formula injection被安全处理；
8. dependency lock、SBOM、LICENSE/NOTICE、provenance、vulnerability/license policy 与 `WEB-SUPPLY-001` 复用登记通过；1Panel/sub2api 应用源码或品牌资产未被未授权复制；
9. Web OCI/static artifact以production模式真实启动、按exact digest回滚，并通过当前/上一Go API/session/CSRF/SSE/deep-link compatibility matrix；Module证据可使用Go contract fake但Web自身不得fake，正式Go↔Web和system E2E按`TEST-REAL-E2E-001`启动真实服务；所有证据仍遵守`TEST-GATE-001`的四维资格字段、claim scope与聚合规则。

### 14.5 通用插件平台门禁（TEST-PLUGIN-001）

Plugin Platform Module Complete 必须同时证明：

1. `plugin/manifest/v1`、Host API、kind contract、WIT world、config schema、错误、状态和 resource profile 已冻结并通过 breaking/golden 检查；
2. Fake Manager、Fake Host、Fake capability provider、`service-grpc/v1` 与 `wasm-component/v1` conformance fixture 只使用公开契约，不复制 Manager/Host 内部实现；
3. manifest 缺失/未知字段、unknown major/kind、unverified minor、digest/size/platform/config mismatch、错误 publisher/provenance、revoked artifact 和 capability 扩张均稳定拒绝；
4. `registered→verified→staged→shadow→active→draining→disabled/revoked`、并发 activate/rollback/revoke、old-generation late result、Host/Manager restart 和 PostgreSQL failover 均收敛到唯一 canonical binding；
5. OCI/Wasm sandbox、network/filesystem/secret default deny、CPU/memory/PID/FD/disk/fuel/deadline、oversize、crash/OOM/hang/restart storm 和 circuit/quarantine 故障门禁通过；
6. shadow/插件输出/Analysis Artifact 均不能写核心 schema、创建 Proposal/Decision/Intent、调用 Edge/P4 或成为第二 effect queue；必须用数据库权限、credential absence、协议拒绝和审计共同证明，不只检查返回 payload；
7. old/new Manager↔Host↔Wasm/Host-managed service、Manager/typed adapter↔独立 service/Agent、manifest/kind/config/WIT 与已持久化 plugin fact 兼容矩阵通过；测试证明官方 Analysis 的 A2A/MCP 业务流量不经过 Host，rollback 只恢复仍合格且未撤销的 revision；
8. `PERF-PLUGIN-001` benchmark/soak 通过，并证明全部插件禁用、平台 crash 或故障风暴不突破核心 p99/RSS/连接预算；
9. production profile 的 OCI digest、publisher identity、SBOM/provenance、offline verification bundle 和 revocation 测试通过；unsigned fixture 始终 `NOT QUALIFIED`；
10. 真实 `masi.analysis.langgraph` 与 conformance fixtures 分别证明 Agent、service gRPC 和 Wasm runtime profile，但只有官方 Analysis Plugin 被视为首期生产业务插件。

### 14.5a 插件统计与声明式 Web 投影门禁（TEST-PLUGIN-STAT-001）

Plugin Statistics 能力必须在 Contracts、Go、PostgreSQL、Plugin Host/对应 direct adapter、conformance plugin 与 Web 各自公开边界同时证明：

1. `plugin-statistics/v1`、`plugin-statistics-display/v1`、OpenAPI 与 persisted projection schema lint/breaking 通过；Go/Rust/Python/TypeScript/Wasm 共用同一 definition/input/artifact/display/status/quality/canonical digest golden，不复制 expected data；
2. `pure-transform` deterministic statistics fixture 通过 Host/Wasm，`read-only-tool` fixture 通过 exact typed adapter；`analysis-agent`只能引用已校验 Artifact。immutable `PluginStatisticsDefinitionV1` 的 projection/field/data-class/scope/window/trigger/resource 与 approved external-source capability 正例通过，runtime registration、unknown projection/field/capability、definition 内 endpoint/credential、SQL/PromQL/JSONPath/MCP prompt/URL/表达式稳定拒绝；新增 capability 不改变三个封闭 kind，不增加生产业务插件或 UI plugin；
3. gauge/sum/histogram、delta/cumulative、monotonic、unit/dimension、半开时间窗、reset/gap/no-data/missing/stale/not-measurable、finite number、duplicate/out-of-order 和 canonical sorting 正反例一致；`read-only-tool` 还验证 external capability/request/observed/response digest/ETag provenance 与 same-run changed-response conflict；missing/NaN/Inf 不补零或进入图表；
4. 每 plugin revision 32 definitions/128 KiB definition block/每 definition 64 field refs/1 external-source capability ref、2 MiB input、1 MiB Artifact、每 Artifact 32 metrics、64 series、10,000 points、每 histogram point 64 buckets、8 tables/32 columns/2,000 rows、16 hints、200 evidence refs、64 KiB total text/4 KiB scalar、depth 8、dimension、in-flight 2、queue 32、deadline 10 秒等全部上限在 admission/分配/执行/持久化/render 前生效；超限有稳定错误并无核心资源泄漏；
5. same run key+same input/result digest 幂等，same key不同digest冲突；old generation、revoked binding、source/profile/data-class drift、timeout/cancel/late result、Go/Host/plugin/PostgreSQL crash/failover和PITR后均不覆盖current或产生第二run/queue；
6. 数据库 deny-role、credential/network/API 负例证明插件/Host/Web不能写 `plugin_statistics` 或核心 schema、扫描catalog为queue、创建Proposal/Decision/Intent、调用P4、更新rule/model/Incident/qualification或自动导出高基数Prometheus series；
7. Go read/export/on-demand/schedule mutation 与每次 scheduled execution 重新执行 actor或policy/scope/data-class/definition/binding/revocation/external-capability 授权；缺 `plugin.statistics.run`、非 scoped Platform Admin、same idempotency key不同schedule digest、跨scope cache、stale SSE、Artifact伪造ACL、unauthorized deep link、oversize export 和 CSV formula injection稳定拒绝或安全编码并有审计；
8. Web只用固定route、内置component与内部ECharts dataset；HTML/SVG/CSS/Vue/React/JavaScript/iframe/URL、完整ECharts option/formatter/renderItem/event/custom series、Vega/Vega-Lite spec/expression和unknown display kind/field均不执行，不存在raw HTML/JSON fallback；
9. metric-card/status/timeseries/bar/heatmap/table/text/evidence-list 在empty/partial/stale/revoked/min/typical/max数据上满足WCAG 2.2 AA、键盘/屏幕阅读器、非颜色语义、单位/时间窗/freshness/coverage/truncation和等价表格，并通过production browser/heap/DOM/long-task/soak预算；
10. `PERF-PLUGIN-STAT-001` 的schedule/freeze/dispatch/execute/validate/DB/API/render与核心隔离矩阵通过；全部统计插件disabled/unavailable后，核心检测、effect/P4、原生Rule Effectiveness、Overview内置统计和非插件页面行为等价；
11. 正式System E2E真实启动deterministic conformance statistics plugin、所需Host/direct adapter、Go、PostgreSQL、production Web和Playwright，运行`freeze→execute→validate→project→API/SSE→render→revoke/stale`；fake只能用于模块邻居，不能替代正式参与服务。未运行、缺浏览器/DB/runtime或仅静态截图时为`HOLD/NOT RUN`。

### 14.6 全模块完成门禁（TEST-GATE-001）

正式跨模块集成测试开始前，`MOD-REGISTRY-001` 明确登记的九个首期模块必须同时达到 Module Complete 状态。任何一个模块未完成时，全局门禁状态均为 `HOLD`，不得通过遗漏 P4/Offline ML/PostgreSQL 资格主体、先接真实上下游或边联调边补齐实现来绕过。

资格证据与聚合必须使用四个正交字段，不再把阶段和结果拼成一个自由字符串：

- `level` 只允许 `REHEARSAL|MODULE|PAIRWISE|SYSTEM_E2E|PRODUCTION`，表示证据在哪个门禁产生；
- `applicability` 只允许 `APPLICABLE|NOT_APPLICABLE`。条件能力未触发时使用 `NOT_APPLICABLE` 并给出稳定 reason/profile/requirement；它不是 PASS，也不能用于必需能力；
- `result` 只允许 `PASS|FAIL|HOLD|NOT_RUN`。`HOLD` 表示前置/profile/环境/证据不完整或不兼容，`NOT_RUN` 表示没有执行；
- `qualification` 只允许 `QUALIFIED|NOT_QUALIFIED`，并绑定精确 claim scope。`REHEARSAL` 永远为 `NOT_QUALIFIED`；适用项只有 result PASS 且同 scope 的全部前置有效时才可 `QUALIFIED`。

`REHEARSAL/NOT QUALIFIED`、`MODULE PASS`、`SYSTEM E2E PASS` 等是由上述字段生成的人类显示摘要，不是 wire enum。本文历史短语 `HOLD/NOT RUN` 表示 result 的两个可能值而不是一个复合状态。资格 aggregate 只排除有稳定理由的 `NOT_APPLICABLE` 项；其他 required applicable 项的 `FAIL/HOLD/NOT_RUN` 均阻断对应 scope 的资格 PASS。CPU、CUDA、single-domain、HA 和不同 topology 的结果按精确 scope 独立聚合，不得继承。`DEC-044` 的 operational Module Complete 单独按下述规则派生，不改变任何原始资格字段。

性能例外不改变原始性能 result，也不把未达到的门槛写成 PASS。例外必须由 Owner 签署并绑定 requirement、精确 scope、风险、补救、owner、expiry 和 `max_qualification_level`；默认最高只能到 `MODULE`。只有不影响契约、正确性、安全、恢复及目标集成功能时，Owner 才可显式将非生产例外上限设为 `PAIRWISE` 或 `SYSTEM_E2E`。任何绝对 production 性能/容量门槛、HA门槛或已经失败的安全/正确性/恢复门禁均不得被豁免为 `PRODUCTION`；过期、scope漂移或补救未跟踪会立即使聚合回到 `NOT_QUALIFIED`。

Module Complete 必须表示：

1. 本文分配给该模块的首期功能、错误语义、资源边界、安全约束、可观测性和运维接口已经完整实现；
2. 不存在代替模块自身职责的 stub、TODO、placeholder、硬编码成功结果或临时兼容分支；
3. 允许使用fake/mock模拟尚未接入的外部模块，但fake/mock必须遵守正式契约且不能替代被测模块内部逻辑；这种Module证据不能被改名为对应pairwise/system E2E PASS；
4. 被测可部署模块使用发布候选binary/OCI和实际runtime真实启动；语言级验证、契约/golden、独立黑盒E2E、故障恢复、性能、镜像启动和文档全部通过Module DoD；仅有进程存在、readiness、mock调用或microbenchmark不满足本条；
5. 每个模块形成独立、可复核的完成证据包，统一门禁报告列出版本、镜像 digest、配置、四维资格字段、claim scope、测试结果和未决例外；
6. append-only findings registry 中 open `P0` 必须为 0，真实 binary/OCI startup 与全部适用的模块公开边界、故障、安全、性能和 3,600 秒 soak 测试不得存在未执行、失败、证据缺失或其他实际 blocker；完成状态必须由公开 evidence schema 和语义 validator 重新派生，禁止手工改写；
7. 受保护发布基线、dirty tree、生产绝对性能/容量/HA 门槛或依开发顺序尚未开展的正式 pairwise/system 所产生的 qualification-only `HOLD/NOT_RUN` 不阻断 operational completion，但原始 result/`NOT_QUALIFIED` 必须保留，也不得借完成状态宣称 `MODULE PASS`、pairwise、system 或 production qualification。实际模块测试的 `FAIL/HOLD/NOT_RUN`、open P0、真实启动失败或缺失证据仍一律阻断完成。

模块实现阶段可以提前完成契约设计、generated client、golden vectors、fake server 和 test harness，也可以执行受控的真实 boundary rehearsal：使用真实生成 client/server、TLS/mTLS、UDS、framing、容器和真实 PostgreSQL test 实例验证 wire compatibility，但必须同时满足：

- 状态和证据显式标记 `REHEARSAL/NOT QUALIFIED`，不能复制为 Module、pairwise、system 或 production PASS；
- 使用隔离 test identity、名称明确包含 `test` 的数据库、fake P4 target 或无设备副作用的只读接口；禁止真实生产 target、真实 effect/P4 mutation、生产 credential 和生产数据；
- rehearsal 可以发现并修复契约/transport 缺陷，但不能以 adapter 胶水、跳过门禁或借用上下游内部实现补齐本模块职责；
- rehearsal 证据记录参与 revision/digest、环境、限制和结果；正式 integration 必须在全模块 Module Complete 后从干净环境重新执行。

该分层采用持续 contract testing 的成熟实践，但不把 example-based contract test 当成完整 schema 或系统 E2E 的替代（参考：[Pact contract testing](https://docs.pact.io/)）。

集成中发现模块缺陷时，该模块必须退出 Module Complete 状态；修复后重新通过自身完整门禁，才能恢复相关集成测试。

### 14.6a 真实服务启动与正式 E2E（TEST-REAL-E2E-001）

- 实现阶段每个可部署模块都必须提供可复制的真实启动命令、固定制品/profile/config digest、startup/readiness/liveness结果和有界关闭/清理命令；启动成功只证明进程可运行，不等于功能或E2E PASS；
- 首期本地/CI正式Module、pairwise和BMv2 System E2E由`e2e-runner-compose/v1`编排，必须固定Compose/Engine/API和runner image digest、隔离project/network/volume名称、显式healthcheck/`service_healthy`依赖、有界startup/total deadline、资源预算、证据采集与精确清理；不得自动换Podman/Kubernetes/systemd runner。Compose health只用于继续场景，不是业务PASS；
- Module black-box E2E必须真实启动被测模块。正式pairwise必须在干净环境真实启动边界两侧的发布候选服务；System E2E必须真实启动BMv2/P4Runtime、Rust Edge、所选Central Inference CPU或CUDA stack、Go Control、真实PostgreSQL、Plugin Host、deterministic statistics conformance plugin、官方Analysis Plugin和Web，并通过公开网络/API/数据库/浏览器边界运行；Host、统计fixture与Analysis作为并列必需资格主体分别验收，Analysis A2A/MCP业务流量不经过Host；任何内部MASI服务不得用fake/mock替代；
- CPU与CUDA profile必须分别执行`probe→explicit selection→startup→load/warmup→readback→numeric/golden→fault→performance→shutdown`，保存selected/observed profile、硬件指纹、命令、日志摘要和artifact/image/profile digest；没有对应硬件时该profile只能`result=HOLD|NOT_RUN, qualification=NOT_QUALIFIED`，不能用另一profile的结果替代。只声明CPU的精确部署可按CPU scope通过；产品或release若声明同时支持CPU与CUDA，则两套required matrix都必须通过；
- 外部LLM/provider可使用资格化确定性provider fixture完成可重复的Analysis业务断言，但Analysis Plugin、MCP/A2A服务边界必须真实启动；启用生产provider时另做真实TLS/认证/限额/超时/脱敏smoke并标明provider资格。fixture不得被描述为真实provider PASS，也不得进入核心检测/effect依赖；
- 每次正式运行必须保存服务清单及健康转换、拓扑、端口/证书身份、数据库migration、traffic/model fixture、开始结束时间、原始结构化结果、故障注入点、清理结果和evidence digest；任一相关服务未启动、边界被绕过、证据缺失或复用rehearsal/mock/microbenchmark时，结果必须为`HOLD/NOT RUN`而非PASS。

### 14.7 Pairwise Integration（TEST-004）

只有`TEST-GATE-001`全局通过后，才允许开始以下正式、可记PASS的集成，并必须依次覆盖；此前相同边界即使做过rehearsal也必须按`TEST-REAL-E2E-001`在干净环境真实启动双方发布候选服务重跑，禁止fake任一侧：

1. P4/BMv2 ↔ Rust Edge（含 exact `p4-traffic-replay/v1` fixture、`p4-stateless-firewall/v1` baseline 双 bank/selector 与 response overlay、priority/default/fragment/capacity、`telemetry-p4-window/v1` bank/epoch/snapshot/read-clear、Digest/PacketIn best-effort sample/drop、event-time source identity、ingress/egress packet oracle、direct-counter/eligible-counter Read、reset/generation，以及 sender/DUT evidence separation）；
2. Rust Edge ↔ C++ MASI Gateway（含`inference-central-grpc-batch/v1` mTLS/framing/message/tensor、input/result WAL、network batch/deadline/quota/retry/dedupe/copy/backpressure、exact wire/profile、logical pool/pool+binding generation、route epoch、late-result fence、WAL pending/gap/resume）；
3. C++ Gateway ↔ pinned Triton/selected ORT CPU或CUDA profile（分别真实启动，含explicit selection、hardware preflight、startup-bound闭包化只读repository、显式dynamic batching/instance group/queue、numeric、CPU thread/NUMA或CUDA copy/I/O Binding与已声明host-side operator placement、worker identity、deadline/cancellation、no model-control/no automatic fallback）；
4. Go Model Manager ↔ deployment adapter/Central Inference rollout（幂等action、pool envelope、所选availability/deployment tier的required/min-ready/capacity及HA适用的failure-domain/N+1、`GetLoadedModel/GetPoolStatus`、per-shard route withdraw/drain/WAL buffer→CAS→commit/resume、restart/quarantine与rolling rollback）；
5. Rust Edge ↔ Go Control（含 InferenceResult→Event idempotency、PostgreSQL commit-before-canonical-ACK、result WAL replay/same-key-different-digest、source/result cursor，RuleObservationBatch 的 duplicate/gap/epoch/quality，以及 model binding commit/resume 的 response-loss/retry/fence）；
6. Go Control ↔ PostgreSQL（含 firewall policy revision/current-previous binding/default/authorization/activation operation/overlay expiry、model_platform incarnation/revision/qualification/rollout/routing/action/per-shard binding/startup-readback/commit/audit、PITR 后 incarnation 轮换、plugin_platform catalog/lifecycle/qualification/activation/binding/revocation、plugin_statistics schedule/run/artifact/current/history，以及 rule epoch/current/status/rollup/retention）；
7. Go Plugin Manager/Statistics ↔ Rust Plugin Runtime Host（含 exact binding、frozen input、run identity、typed Artifact、cancel/deadline/fence）；
8. Runtime Host ↔ `service-grpc/v1`/`wasm-component/v1` conformance plugin（含 deterministic statistics capability fixture）；
9. Go Control ↔ Frontend（proposal/decision/effect/model-operation/rule-observation/plugin/statistics projection、fixed renderer、SSE invalidation 与 timeout query）；
10. Python Analysis Plugin ↔ read-only MCP；
11. Go/peer Agent ↔ Python Analysis Plugin A2A；
12. Go effect dispatcher ↔ Rust Edge ↔ P4 readback（分别覆盖 response overlay 与 baseline inactive-bank write→verify→selector flip→verify→PG CAS，以及 response-loss/crash reconcile）；随后由 Go 对 `applied` exact entry 创建 canonical observation epoch，Edge 只按该 identity 执行异步 rule counter sweep/outcome evidence。

上述第 1、5、6、9、12 项必须同时覆盖 `target/v1`/`fleet-operation/v1`：Go 注册并分配 1/2/N target，Edge 每 target 独立 arbitration/journal，Decision 原子产生 parent+child intents，静态 wave 逐 target 执行，PostgreSQL 和 Web 保留 partial/unknown vector；不得另起 fleet pairwise queue 或跳过原 effect 边界。

每一对至少测试成功、timeout、duplicate、乱序、未知版本、generation mismatch、oversize、backpressure、disconnect/reconnect 和错误映射。

### 14.8 分波次拼装（TEST-005）

系统按以下顺序集成：

1. **数据面**：BMv2 P4 aggregate/snapshot → Rust source WAL/window；
2. **检测面**：BMv2 → Rust final window/input WAL → batched-unary mTLS gRPC → C++ Gateway → Triton dynamic batch/explicit ORT CPU或CUDA result → Rust result WAL；
3. **模型控制**：qualified bundle/repository → Go rollout operation/incarnation → idempotent new pool generation startup → selected availability profile的replica/readback/capacity qualification（HA另含failure-domain/N+1）→ Edge per-shard route-withdraw/drain/WAL buffer → PG CAS current/previous → Edge logical-pool commit/resume → old generation drain；
4. **事实链**：Rust result WAL → Go → PostgreSQL canonical Event → ACK/cursor；
5. **可视化**：PostgreSQL → Go → Frontend；
6. **Target/Fleet 控制**：Go target registry/assignment → Edge per-target actor/arbitration/read-only reconcile → Frontend Managed Targets；
7. **反向处置**：Go intent → Rust → P4；临时 response overlay 逐 entry readback，baseline policy 按 inactive bank 验证后切 selector，再由 PG CAS finalize；fleet parent 只按静态 wave 放行同一队列中的 per-target intents；
8. **规则表现**：applied exact readback → Go epoch → Rust counter sweep/packet oracle → Go/PG rollup → Frontend Rule Effectiveness；
9. **插件扩展平面**：signed manifest → Manager admission/qualification/active binding → `{Host → Wasm/Host-managed service | direct typed adapter → independent service/Agent}` → typed result；统计 capability 另走 `Go frozen bundle → run → Artifact validation → PG projection → fixed Web renderer`，不新增 kind、部署模块、独立消息代理、插件自有 queue 或第二 effect queue；唯一 durable run ledger 与有界 dispatch 留在 Go Control；
10. **Agent 旁路**：Incident → official Analysis Plugin A2A Task → LangGraph/LLM/MCP → Artifact → Frontend。

禁止在前一波契约、恢复和性能门禁未通过时接入下一波真实 mutation。

### 14.9 系统 Full E2E（TEST-006）

`TEST-006` 是场景套件聚合器，不是一条可覆盖子结果的“大测试”。每个 `fixture × runtime profile × availability profile × deployment tier × topology × fault scenario` 必须产生独立 evidence identity 和四维资格字段；只聚合当前 release scope 明确列为 required 且 `APPLICABLE` 的场景。System E2E aggregate 只有在所有 required applicable 场景均为 `result=PASS, qualification=QUALIFIED`，且不存在过期 waiver/profile/digest 时才可 PASS；任一 `FAIL/HOLD/NOT_RUN` 阻断聚合。`NOT_APPLICABLE` 必须有稳定理由且不能用于跳过release已声明支持的CPU/CUDA、HA、target或流量模式。

检测链流量重放验收：

```text
immutable traffic fixture manifest + original/transformed artifact
→ direction/topology/timing/rate/impairment validation
→ BMv2
→ P4 aggregate bank/epoch snapshot（default）或 exact qualified mirror source
→ Rust telemetry source WAL + event-time final window + input WAL
→ `inference-central-grpc-batch/v1` → C++ Gateway → Triton dynamic batching / startup-selected ORT CPU或CUDA
→ Rust result WAL + bounded Go batch
→ Go validation → PostgreSQL canonical Event durable → ACK
→ API
→ Frontend
```

检测链必须分别使用：①确定性generated/synthetic正常与攻击fixture；②至少一个许可证/隐私/ground-truth/transformation已通过的curated PCAP slice；③至少一个真实socket TCP client/server fixture。每次运行按`TEST-REAL-E2E-001`真实启动全链，并保存requested/sender/test-ingress/DUT/P4 source/window/Edge input-WAL/gRPC/Gateway/Triton+actual EP/result-WAL/Event-ACK/detection各层结果；sender成功、counter增长、Digest ACK或Central Inference success均不能单独使检测链PASS。至少注入一次Go ACK丢失证明零双Event、一次same-generation replica response loss证明原identity dedupe、一次full-pool outage证明bounded HOLD/gap且无自动profile fallback，以及一次source gap证明不补零分类。CPU和CUDA均被声明支持时，必须各运行一套全链E2E，证据不得复制。

模型替换链验收：

```text
qualified immutable model bundle
→ Go durable rollout operation + model-control incarnation + ordered shard/routing vectors
→ pre-stage exact raw/optimized artifact + read-only repository snapshot + pool envelope
→ idempotent deployment action starts a new Gateway/Triton pool generation
→ all required replicas load/warm/read back; qualify selected availability profile and rolling capacity（HA另含failure-domain/N+1）
→ Edge advances route epoch, withdraws old logical-pool route and drains one shard
→ Edge WAL-buffers later complete windows with gap/resume watermark
→ PostgreSQL per-shard CAS current/previous pool binding
→ Go/Edge exact committed-binding handshake; resume and repeat next shard
→ old pool generation drains within rollback grace and stops
→ Edge/Go/Event/Web carry exact incarnation + shard/route + binding generation
```

至少覆盖：①同feature/output contract的不同算法/权重只改exact bundle/repository/pool envelope、Edge/Go代码不变；②新增label旧reader保留/展示但不自动effect；③single-label、multi-label、anomaly/OOD/abstain；④离线qualification、pool startup、required replica/selected-profile resource/rolling capacity不通过时停止后续rollout，HA profile另覆盖failure-domain/N+1不足；⑤deployment action/readback/CAS/commit response loss沿原operation reconcile；⑥exact previous pool rolling rollback；⑦feature/runtime major改变时拒绝直接替换，image兼容old/new schema且Edge按独立logical pool单路切换；⑧runtime load/unload/repository poll、online shadow、weighted split、第二router和Pod/Triton Ready冒充current均被拒绝；⑨partial rollout明示mixed，无跨incarnation/route/pool/binding generation漂移；⑩HA profile的单replica与failure-domain loss由same-generation N+1承接，single profile中断时明确HOLD/gap且不得声称HA；全池outage形成bounded WAL/HOLD/gap且无fallback；⑪PITR轮换incarnation并逐shard重绑；⑫active/warming/draining/replay合计满足冻结CPU/RAM或GPU/VRAM及网络容量。

处置链验收：

```text
Event/Evidence
→ deterministic eligibility/risk classification
→ qualified policy 或 Proposal + AuthorizationDecision
→ effect intent
→ Rust P4 write
→ BMv2 table readback
→ PostgreSQL CAS applied/failed/unknown
→ Frontend
```

处置链至少包含四个独立场景：

1. R0/R1 已资格化 policy 自动产生 intent，并完整执行 journal/readback/CAS；
2. Analyst proposal → 不同 Operator approve → R2 intent → Rust/P4/readback，且 UI 显示完整授权链；
3. 缺 evidence、generation/P4Info/capacity drift、过期、self-approval、普通 incident R3，或未通过 `FUNC-FW-001` typed maker-checker change API 的 baseline R3 被 `HOLD/reject`，并证明零 Edge RPC；
4. Agent Recommendation/Artifact 可以被显示和人工引用，但 Plugin、Artifact payload 或 Frontend 快捷操作均不能直接创建 Decision/Intent。

规则表现链验收：

```text
effect exact readback applied
→ Go 创建并持久化 canonical rule/counter observation epoch
→ Edge 接收该 identity 并执行 bounded sweep
→ `traffic-replay/v1` PTF/generated fixture 产生受控 eligible + matching/non-matching traffic
→ bounded P4Runtime direct-counter Read
→ Go/PostgreSQL current + rollup + quality
→ Rule Effectiveness API/SSE
→ Frontend 三层状态、趋势、Top-N、state timeline 与等价表格
```

必须分别覆盖：① exact readback 但无 eligible traffic；② 有 eligible traffic 但无 rule hit；③ sender accepted 但 DUT ingress 未观察到；④ counter hit 但独立 packet oracle 失败；⑤ install + hit + outcome 全部验证；⑥ reset/gap/generation/TTL 导致 observation stale/invalid 而不改变原 effect；⑦ no-hit 诊断不能直接触发任何 P4 mutation。规则 outcome 的确定性 PASS 默认使用最小 PTF/generated fixture；curated PCAP 用于补充检测/行为覆盖，不能以复杂语料替代精确 packet oracle。

BMv2 无状态防火墙策略链验收：

```text
immutable normalized baseline revision + explicit default action
→ Go validation/diff/conflict-shadow-capacity preflight
→ different Admin maker / Operator checker R3 authorization
→ durable effect intent
→ Edge compile exact target plan
→ write + readback inactive bank
→ selector flip + exact readback
→ PostgreSQL CAS current/previous
→ generated/PTF traffic verifies permit/drop and direct counters
→ Frontend activation timeline + layered effectiveness
```

至少覆盖：①空策略、最小策略与最大 4,096-rule initial profile；②prefix/protocol/ingress-port/L4 exact-or-wildcard/fragment class、priority、explicit default、permit-and-continue/drop；③same-priority overlap conflict、shadow、unsupported IPv6/range/stateful/NAT/rate-limit 和容量展开在零 P4 RPC 前拒绝；④inactive bank partial write/readback mismatch 不改变 active selector；⑤selector response loss、Edge/Go/DB crash 和 CAS conflict 沿原 operation 收敛且不双 flip；⑥exact previous rollback、overlay 与 baseline precedence、durable TTL expiry；⑦UFW/nftables/iptables/eBPF 不在 packet path 中制造假 PASS；⑧BMv2 evidence 始终标为 software target，不外推真实硬件。

多 Target/Fleet 链验收：

```text
registered target set + exact assignments/profile observations
→ Admin creates exact temporary/baseline fleet proposal
→ Operator authorizes frozen target-set/wave digest
→ one non-claimable parent + bounded per-target effect intents
→ Edge TargetActors execute independently through P4Runtime
→ per-target journal/readback/PostgreSQL CAS
→ parent derives exact child vector
→ Frontend target×stage matrix / static wave timeline
```

至少覆盖：①手工与 external candidate 注册、duplicate endpoint/device 拒绝、activate/assign/drain/retire；②1/2/N BMv2 target 各自 StreamChannel/application generation/P4Info/port map；③静态 canary→后续 wave，三种 failure policy；④一个 target slow/unreachable/restart/P4Info drift 不阻塞健康 target 且后续 gate 符合 policy；⑤child applied/failed/blocked/unknown/reconciling 的 parent 投影；⑥assignment handoff/old actor late result/更高 election 与 read-only reconcile；⑦部分 fleet rollback 只选择各 target exact previous；⑧PITR 轮换 target-control incarnation；⑨gNMI Set、ONOS/第二 writer、NetBox/Ansible 自动 current 全部被拒绝；⑩绝对 N-target 性能/资源门槛未冻结时保持 HOLD。

Agent 链验收：

```text
Incident bundle
→ official Python Analysis Plugin
→ real qualified MCP/A2A service boundaries + deterministic provider fixture or qualified real LLM
→ AnalysisArtifact
→ Frontend
```

Agent 链本身必须真实启动官方 Analysis Plugin 及资格化 MCP/A2A 服务边界，业务 Task/Message/Artifact 通过 direct typed adapter 交换，不得绕经 Plugin Host。完整 System E2E 仍须同时真实启动 Plugin Host，并在独立插件平台场景验证 service/Wasm；这表示两个必需模块同时在场，不表示 Host 是 Analysis 代理。Agent 场景分别覆盖证据充分、需要 bounded MCP、新证据不足、低质量、provider/tool timeout、grounding reject，以及对应的 `limited|insufficient_evidence|failed` outcome，并对 `AGENT-COMPAT-001` 矩阵生成可复核差异报告。真实 LLM provider 不作为确定性 CI gate；release E2E 可使用资格化 deterministic provider fixture，真实 provider 另作 TLS/auth/quota/timeout/redaction smoke 与 quality qualification，两类证据不得混写。

插件平台链验收：

```text
digest-pinned OCI/Wasm artifact + manifest + verification bundle
→ Go Manager verify/qualify
→ explicit active generation
├─ Host-managed binding → Rust Host staged/shadow → bounded service/Wasm execution
└─ independent binding → direct typed A2A/MCP/gRPC adapter → bounded service/Agent execution
→ schema/digest/fence validation
→ disable/revoke/rollback
```

必须分别覆盖有效 Host-managed service/Wasm、独立 service/Agent、未知 kind/major、错误 digest/publisher、超权限、资源耗尽、并发 activation、old-generation result、Host crash、revocation 和不兼容 rollback。全程证明零核心 DB/P4/effect mutation；Analysis Plugin 必须通过同一 catalog/qualification/activation 控制面后才能参与 Agent E2E，但其业务 A2A/MCP 流量不得因这一控制面关系被实现为 Host 代理。

插件统计展示链验收：

```text
Go authorized canonical projection + exact source generation
→ frozen StatisticsInputBundleV1
→ durable non-effect run + deterministic conformance statistics plugin
→ PluginStatisticsArtifactV1
→ Go schema/digest/scope/generation/resource/display validation
→ PostgreSQL current/history CAS
→ OpenAPI + SSE invalidation
→ production Vue fixed renderer / ECharts dataset / equivalent table
```

必须分别覆盖 valid、partial/gap/no-data/reset/stale/not-measurable、old generation、revoked binding、same-key conflict、timeout/cancel/crash、max points/rows/text、DB/SSE/browser故障和全部UI代码注入负例；统计插件关闭后核心原生统计与非插件页面保持等价。正式场景真实启动conformance statistics plugin、适用Host/direct adapter、Go、PostgreSQL和Web；只用mock payload或截图不能PASS。

### 14.10 Fault Injection（TEST-007）

必须覆盖：

- WAL write/fsync/checkpoint 各阶段崩溃；
- P4 aggregate bank flip/freeze/snapshot/read/clear前后崩溃、sequence-before/after不一致、counter/register reset/wrap、Digest duplicate/Ack丢失/server-client drop、PacketIn burst/oversize、source epoch/reconnect/watermark/late-after-final，以及PACKET_MMAP/AF_XDP ring/UMEM/kernel/NIC drop和XDP mode/fallback漂移；
- gRPC partial/oversize/malformed frame、错误tensor offset/length/shape/dtype、mTLS/SAN失败、resolver stale/wrong-generation endpoint、connection reset、deadline/cancellation、request ID同digest重试/异digest冲突、same-generation duplicate/conflicting output、in-flight/Gateway/Triton queue saturation、input/result WAL与Go/PostgreSQL ACK各阶段response loss；
- Gateway/Triton/selected ORT CPU或CUDA单replica crash/hang/OOM/invalid output、HA profile的failure-domain loss、全池unavailable、网络分区/抖动/带宽耗尽，以及验证无Edge-local、CPU↔CUDA/TensorRT/LibTorch/旧模型自动fallback；
- model bundle/manifest/metadata/feature/label/adapter/wire/opset/backend/device/resource/optimization artifact mismatch、partial repository/symlink/custom executable content、pool envelope过期/旧incarnation/current/错误pool generation、Triton model-control/poll/auto-complete尝试、verification/load/backend-Session/warmup/Gateway-probe/readback timeout、availability profile的min-ready/capacity失败及HA profile的N+1/failure-domain失败、route-withdraw/drain/buffer overflow/gap、deployment action冲突/重复、PreStop失败/SIGKILL、start/readback/commit response loss、per-shard CAS race、同shard双route/current、old late result、restart storm/quarantine、PITR ABA、rollout_failed_mixed、previous revoked/unavailable与rollback failure；
- Rust Edge restart、P4 session/mastership reset；
- Go claim 后崩溃、RPC 后 finalize 前崩溃；
- P4 write accepted 但响应丢失；
- Edge↔Control、Plugin↔Control、Frontend↔Control 断网；
- PostgreSQL restart/failover/pool exhaustion/deadlock/timeout；
- disk full/inode full/WAL corruption/partial tail；
- duplicate idempotency、late result、generation drift；
- 并发 approve/reject/self-approval、proposal/authorization expiry、role/profile/P4Info/evidence/capacity 在审批前后漂移；
- Decision/Intent 原子事务前后崩溃、PostgreSQL failover 后恢复原 canonical proposal/operation、OIDC/JWKS 不可用或认证上下文过期；
- proposal/backlog/payload/page 上限、冲突风暴和确保 blocked/HOLD 路径零 Edge RPC；
- LLM/MCP/A2A timeout、malformed、oversize、prompt injection；
- Plugin Manager/Host crash、双 active race、drain 超时、old-generation late result、artifact/config/capability drift；
- OCI manifest/layer/Wasm digest 或 size mismatch、错误 publisher/provenance/SBOM、revoked artifact、离线 bundle 不完整；
- service/Wasm CPU/memory/PID/FD/disk/fuel/deadline 耗尽、sandbox/egress/preopen/secret 越权和 restart storm；
- Plugin Platform disabled、Manager/Host unhealthy、单插件 disabled/quarantined/unavailable；
- plugin statistics schedule/run durable前后崩溃、same-key same/different digest、old binding generation/late result、input source/data-class drift、timeout/cancel、queue/points/rows/text/cardinality/depth超限、NaN/Inf/duplicate/out-of-order/reset/gap、Artifact validate/CAS/DB failover/PITR、SSE gap、renderer unmount/heap，以及HTML/SVG/Vue/ECharts/Vega/URL/CSV formula injection；
- rule direct counter 未绑定/action 未执行、Read 未包含 exact `DirectCounterEntry` 或 `TableEntry.counter_data` presence、零 eligible traffic、counter reset/wrap/saturation、重复/乱序/分片/gap、generation/pipeline/revision drift、TTL expiry/supersede、overlap/priority/default entry、观测队列/数据库/Prometheus/Grafana 不可用；
- firewall plan compile/expansion/capacity/overlap conflict、inactive bank 各写入与 readback 阶段、selector write 前/后与响应丢失、selector 已切换但 PostgreSQL CAS 失败、active/previous bank drift、overlay expiry/delete response loss、old operation/generation/P4Info/bank epoch late result、rollback grace 清理与 host-filter contamination；
- target candidate/duplicate identity、activate/assignment CAS、old/new actor overlap、arbitration/election reseed、per-target queue/disk/FD exhaustion、one-target reconnect storm、pipeline/capability drift、fleet parent+child transaction、wave gate 前后崩溃、partial/unknown vector、fail-fast/manual-gate、assignment handoff、target-control incarnation/PITR ABA、gNMI slow/oversize/gap/Set attempt、external inventory drift 与 second-controller connection；
- traffic fixture 缺失/digest drift、malformed/truncated PCAP、unsupported DLT/timestamp/snaplen、ground-truth conflict、方向 cache/rewrite/checksum/MTU/offload/port-map错误、qdisc apply/readback失败、sender accepted 但 ingress/capture 丢包、runner/BMv2/receiver crash、link down、deadline/取消、loop/queue/disk/RSS/PID/FD耗尽、cleanup失败与非测试 interface/route/target 访问尝试；
- 证书过期/SAN 错误/config drift。

每个场景必须定义故障前状态、注入点、预期中间状态、恢复步骤、最终不变量和证据文件，并输出符合 `fault-evidence/v1` JSON Schema 的 append-only envelope。必填字段至少包括 `scenario_id`、`requirement_ids`、`run_id`、起止时间、module、image/config/contract/schema/environment profile digest、随机 seed、fault injection、pre/intermediate/post state、recovery steps、invariants、observations、独立的`level/applicability/result/qualification`、claim scope、artifact refs、log/trace hash 和 tool versions。摘要不能覆盖原始结果；schema/version/digest 不匹配时证据无效。

### 14.11 Benchmark 与 Soak（TEST-008）

- benchmark 固定 warm-up、测量窗口、并发、数据规模、模型/config hash 和 `performance-environment/v1` digest；
- 至少重复多次，报告分位数、吞吐、错误、CPU、RSS、连接和队列；
- 原始结构化结果与摘要不可覆盖并带自哈希；
- soak 必须检查连接/FD/线程/RSS/queue/WAL 是否持续增长；
- governance benchmark 必须覆盖 `PERF-GOV-001` 的并发矩阵、热点 proposal 冲突、不同 target 吞吐、过期清理、索引/WAL 增长及其对 Event/effect 热路径的隔离；
- plugin benchmark 必须覆盖 `PERF-PLUGIN-001` 的 runtime profile、batch/concurrency、queue saturation、cold/warm Wasm、crash/restart/circuit、全部插件禁用基线以及对核心热路径的隔离；
- plugin statistics benchmark 必须覆盖 `PERF-PLUGIN-STAT-001` 的 schedule/freeze/dispatch/execute/validate/project/API/render、min/typical/max metrics-series-points-tables、quality/reset/gap、queue/deadline、revoke/crash、DB/SSE慢、browser heap/DOM/long-task/soak与全部统计插件禁用基线；
- Frontend benchmark 必须使用 production build，覆盖 `WEB-PERF-001` 的 bundle/route/CWV/heap/DOM/chart/request/cache/SSE 预算、cold/warm、资格化浏览器、scope/session 切换和长时间 dashboard/list/detail 导航；
- rule observation benchmark 必须覆盖 `PERF-RULE-001` 的 rule/target 矩阵、full sweep/freshness、P4 Read/queue/gap、5 分钟 rollup/retention、Top-N/list/trend、Frontend state timeline 和叠加核心峰值隔离；
- firewall benchmark 必须覆盖 `PERF-P4-FW-001` 的 0/128/1,024/4,096 normalized rule、最坏合法展开、cold/warm compile、inactive-bank write/readback、selector flip/readback、overlay churn/expiry、rollback/reconcile、packet throughput/latency/drop correctness、control-plane contention 和 3,600 秒 soak；
- target/fleet benchmark 必须覆盖 `PERF-TARGET-FLEET-001` 的 0/1/2/N target、每 target 规则/telemetry/effect 叠加、一个 slow/unreachable target、公平/优先调度、最大 parent/child/wave transaction、partial/reconcile/rollback、API/UI 矩阵和 3,600 秒 soak；单 target 性能结果不得复制为 fleet PASS；
- traffic replay benchmark 必须覆盖 `PERF-TRAFFIC-001` 的 recorded/multiplier/fixed-pps/fixed-mbps/topspeed、small/typical/max bounded fixture、preload on/off、offload profile、netem delay/loss/reorder、sender-vs-DUT-vs-capture count、resource saturation、cleanup 和 software-target-only 声明；
- online model benchmark必须分别覆盖`PERF-INF-001`的Gateway/Triton/ORT CPU与CUDA profile、dynamic batch/instance group、cold/warm/cache-hit/cache-miss startup-to-min-ready、steady/peak/rolling capacity、full-pool outage、network、route-withdraw/drain/buffer/gap/replay、active/warming/draining资源峰值、start/termination/readback/CAS/commit/resume/restart/quarantine/rollback、partial/mixed rollout、CPU/RAM或GPU/VRAM saturation、backend/device/optimization matrix和3,600 秒 soak；HA profile另覆盖N+1、single replica/failure-domain loss；
- online telemetry/hotpath benchmark必须分别覆盖`PERF-TEL-INF-001`的P4 aggregate snapshot、Digest/PacketIn supplemental loss、event-time window、Edge gRPC batch/RTT/retry、Gateway/Triton/selected runtime queue、actual copy、input/result WAL、Edge→Go/DB ACK端到端分段、peak/saturation/outage/3,600 秒 soak；条件capture profile分别覆盖PACKET_MMAP和已触发AF_XDP/DPDK，不能用gRPC/Triton/ORT microbenchmark替代packet/window→Event资格；
- destructive test 只能使用名称含 `test` 且显式确认的数据库；
- test run 使用唯一前缀并精确清理；
- 禁止 benchmark、retention 和其他测试同时操作同一 DB。

### 14.12 CI 分层（TEST-009）

- PR：lint、format、static、unit、contract、module black-box、manifest/WIT schema、Frontend production bundle/a11y/license/lockfile 与供应链 policy 静态检查；可以运行明确标记的无副作用 boundary rehearsal，但不得产出 integration PASS；
- 受控 integration 环境：Module Complete 前只运行 `TEST-GATE-001` 允许的 `REHEARSAL/NOT QUALIFIED`；全模块完成门禁通过后才运行可记 PASS 的 pairwise tests；真实 PostgreSQL test 实例可以在 Go/DB 模块黑盒及 rehearsal 中提前使用，不等于正式 pairwise integration；
- nightly：仅在相应集成波次放行后运行多模块 E2E、fault matrix、benchmark；
- release candidate：真实三机/多机 full E2E、P4、HA/restore、soak；
- 缺失环境、skip、报告版本不匹配、fixture 污染或证据缺失必须标为 `HOLD/NOT RUN`，不能标为 PASS；
- CI 上传 JUnit/结构化 JSON、环境指纹、脱敏日志和 artifact hash。
- fault、benchmark、traffic fixture/replay、telemetry-source/window/hotpath、DB restore 和 ML/model lifecycle qualification 必须分别上传 `fault-evidence/v1`、`performance-environment/v1`/原始结果、traffic replay 结构化结果/fixture/output digest、source/window/ABI/ACK evidence、restore manifest、model qualification/rollout/startup-readback/offline-comparison manifest；CI summary 只能引用这些不可覆盖证据。

### 14.13 模块 Definition of Done（TEST-010）

模块只有同时满足以下条件才算完整实现，并有资格进入全模块完成门禁：

1. 该模块全部首期需求已经实现，没有未处置的必需功能、TODO、placeholder 或临时成功路径；
2. 契约/golden 通过；
3. 黑盒功能 E2E 通过；
4. 故障注入和恢复通过；
5. 性能门槛通过；若使用性能例外，只能按 `TEST-GATE-001` 保留原始非PASS结果、显示waived requirement并限制最高资格级别，绝对production性能/容量/HA门槛不可豁免；
6. 资源、队列、输入输出全部有界；
7. 未知版本和异常输入 fail closed；
8. OCI image 可独立启动并通过 startup/readiness/liveness；
9. 未导入其他模块源代码；
10. 未越权访问其他模块数据或 secret；
11. 生成可复核测试报告和镜像 digest；
12. 对可执行插件/Host，manifest、publisher identity、SBOM/provenance、isolation profile、qualification 和 revocation 证据完整；
13. 需求 ID 已映射到测试、命令、环境、证据和负责人；
14. 所有 fault/benchmark/traffic fixture/replay/telemetry source/window/hotpath/ACK/restore/model qualification/rollout/startup-readback/offline-comparison 证据符合相应版本化 schema，引用不可变 profile/artifact/config/output digest，且 rehearsal 结果未被误标为 PASS；
15. 对P4/Edge/Central Inference/Go，`CONTRACT-TELEMETRY-001`/`CONTRACT-INFERENCE-001`的source coverage、event-time/finality、central gRPC wire、same-generation retry/dedupe、input/result WAL、commit-before-ACK、no-fallback和绝对性能门槛已按自身公开边界通过；条件mirror/backend优化未触发时使用`applicability=NOT_APPLICABLE`并给出机器可读理由，不能用fallback冒充兼容。
16. 对 P4/Edge/Go/Web/DB，`CONTRACT-P4-FW-001` 的 normalized policy、双 bank/selector、response overlay、default/priority/fragment、R3 activation、readback/reconcile、rule effectiveness 与绝对软件 target 性能门槛已按自身公开边界通过；未被release声明且确实未触发的硬件、IPv6 或 stateful profile使用`applicability=NOT_APPLICABLE`，已经声明但缺失/未资格化的能力必须是`applicability=APPLICABLE,result=HOLD|NOT_RUN`，不能用主机防火墙或 BMv2 PASS 冒充。
17. 对 P4/Edge/Go/PostgreSQL/Web，`CONTRACT-TARGET-001`/`CONTRACT-FLEET-EFFECT-001` 的 stable target、assignment/actor fence、1/2/N target isolation、公平调度、parent/child/wave/partial/reconcile、PITR incarnation 与绝对 fleet 容量门槛已按自身公开边界通过；条件 gNMI、Stratum/NetBox/Ansible/Nornir 未触发时有机器可读 NOT_APPLICABLE，不能以单 target demo 或第三方产品存在冒充。
18. 对 Contracts/Go/PostgreSQL/Plugin Host或direct adapter/conformance plugin/Web，`CONTRACT-PLUGIN-STAT-001` 的 immutable definition/host projection allowlist、冻结输入、run幂等、Artifact quality/metric/table/display、资源/security/current-history、fixed renderer和disabled equivalence已按 `TEST-PLUGIN-STAT-001` 通过；仅有schema、插件输出JSON或静态图表不得冒充完整能力。

### 14.14 规则表现独立门禁（TEST-RULE-001）

Rule Observation 在 P4、Edge、Go、DB 与 Web 各自 Module Complete 中必须分别通过公开边界门禁；下列工具只属于 testkit/qualification，不能进入生产 P4 ownership：

1. `p4c`/P4Info lint 与 P4Testgen 生成 input/control-plane/expected-output tests；PTF 按 `CONTRACT-TRAFFIC-001` 向 BMv2/资格 target 注入真实包并验证 ingress/egress/drop、direct counter 和 eligible counter。P4Runtime Shell 只允许隔离 rehearsal/故障诊断，使用 test identity 或 production read-only role，禁止成为 daemon、writer、scheduler 或部署依赖；
2. golden 覆盖 `CONTRACT-RULE-001` canonical identity、累计→delta、pps/bps、packet match ratio、same-table hit share、observable utilization、denominator zero、coverage、first/last observed-hit window 和所有状态/错误；Go/TypeScript/Rust 对同一 vector 必须一致；
3. install/readback 测试证明 Write ACK 不能代替 exact entry readback；未发包保持 `no_eligible_traffic/not_observed`，发送不匹配包产生 eligible traffic + no hit，发送匹配包只增加目标 entry，且 wildcard/LPM/ternary/priority/default/多 table 不被错误归因；
4. action outcome 使用独立 packet oracle 分别验证 drop 无 egress、forward 正确 egress/改写和 mirror 的受控副本；移除 oracle 时只能为 `not_measurable`。仅 counter 增长而实际输出错误的 fixture 必须使 outcome verification failed，不能显示处置成功；
5. counter 32/64-bit（按 target profile）、wrap、saturate、显式/隐式 reset、target/Edge restart、generation/pipeline/P4Info/rule revision、duplicate/order/split/gap/timeout/oversize 和 clock step 均不产生负 delta、跨代拼接、精确伪时间或错误 0%；
6. TTL expiry、rollback、supersede、readback drift、数据库 failover/PITR/retention、late sample 和 concurrent projection update 保持唯一 epoch/current projection，并可沿原 effect operation reconcile；
7. no-hit/dead-rule/overlap 诊断只生成只读候选；测试从 Prometheus alert、Alertmanager、Grafana data link/action、Analysis Artifact 和 Frontend chart 尝试 mutation 时均被架构/凭据/API 拒绝，P4Runtime writer 始终只有 Edge；
8. `PERF-RULE-001` 的 0/128/1,024/4,096-rule、最大 target、热点/均匀/reset storm、队列 saturation、3,600 秒 soak 和核心峰值叠加通过；任何缩小容量/频率只在新 profile 下重跑，不能把未覆盖 rule 隐藏为成功；
9. Rule Effectiveness 的 URL/filter/cursor、三层状态、公式/分母/coverage、state timeline/Top-N/table、gap/reset/no-traffic/not-measurable、键盘/ARIA/等价表格、权限与导出在 `TEST-WEB-001` 浏览器矩阵通过；
10. 所有 evidence 绑定 P4 program/P4Info/target/profile、`traffic-replay/v1` fixture/transformation/run、rule/effect/generation、counter mode、environment、source/image/config/contract digest；在真实 target、硬件 byte-count 语义和规模未资格化前只能报告 `HOLD/NOT RUN`，不得用 BMv2 PASS 声称硬件生产资格。

### 14.14a BMv2 无状态防火墙独立门禁（TEST-P4-FW-001）

P4、Edge、Go、PostgreSQL 与 Web 的 Module Complete 必须分别证明：

1. `contracts/p4/firewall-policy/v1`、`p4-stateless-firewall/v1` profile、P4Info/normalized IR/compiled plan/operation/API schema lint、breaking/golden 通过；Rust/Go/TypeScript/test oracle 对 canonicalization、priority、default、fragment、unsupported、digest 和错误一致；
2. exact `simple_switch_grpc`/v1model/p4c/P4 program/P4Info profile 下，response overlay 与两个 baseline bank/selector/直接计数资源隔离且容量有界；0/128/1,024/4,096 rule 与最坏合法展开通过，超限在外部写前拒绝；
3. generated/PTF packet matrix 覆盖 matching/non-matching、prefix/protocol/ingress/L4 wildcard、TCP/UDP/ICMP、首片/非首片、priority、same-priority conflict、shadow、permit-and-continue、drop、explicit default 与后续 forwarding；p4c/P4Testgen 和 packet oracle 同时检查 table action、direct/eligible counter 与 egress/drop；
4. baseline activation 严格执行 inactive-bank complete write/readback → selector flip/readback → PostgreSQL CAS，任何 partial bank、wrong digest/count/action/priority/default、response loss 或 crash 均不产生两个 current、未知 active 或盲重试；same-key/same-digest 重放返回 canonical result，different digest 冲突；
5. concurrent activate/rollback、old generation/P4Info/bank epoch、selector CAS race、Edge/Go/PostgreSQL/BMv2 restart、mastership loss、pipeline drift 和 PITR 后 fence 通过 `fault-evidence/v1`；rollback 只激活 exact previous revision并保留历史事实；
6. response overlay 与 baseline precedence、独立 quota、R0/R1/R2 proposal/decision/intent、durable TTL expiry/delete/readback、restart reconciliation 和 observation epoch 通过；overlay 不能修改 baseline selector，baseline activation 不能删除未列入 plan 的 overlay；
7. Admin maker/Operator checker、R3 typed change API、step-up/exact diff/default/scope/expiry、self-approval/stale/capacity/P4Info drift 和零 Edge RPC reject path 通过；raw P4Runtime/P4 source/shell、Plugin/Artifact/LLM/Frontend bypass 与第二 writer 被协议、身份和部署负例拒绝；
8. Frontend 显示 draft/validated/authorized/activating/current/previous/unknown/reconciling/failed/HOLD、compiled impact、shadow/conflict、capacity、default、exact approval与三层规则表现；浏览器刷新、SSE gap、timeout query 和重复点击不创建第二 operation；
9. test namespace 的 UFW/nftables/iptables/eBPF/XDP/bridge/qdisc/route 状态有机器可读 pre/post evidence，非被测过滤路径关闭或隔离；故意打开 host drop 的负例必须产生 environment-invalid/HOLD，不能获得 P4 outcome PASS；
10. `PERF-P4-FW-001` 的 compile/expand/write/readback/flip/rollback、steady packet p50/p95/p99、throughput/drop correctness、overlay churn、observation sweep、control-plane contention、资源饱和与 3,600 秒 soak 达到冻结绝对门槛；BMv2 结果只授予 exact software profile，硬件/IPv6/stateful 等未资格能力稳定拒绝。
11. firewall schema/migration matrix 覆盖 empty/上一受支持版本到 current、重复/中断/checksum drift、expand/contract rollback、并发 activation、failover/PITR/restore；迁移后引用链、唯一约束和 old/new reader-writer 矩阵一致，且 migration/restore 对 P4 零 mutation、恢复 binding 仍须 Edge readback 后才能成为 current。

### 14.14b 多 Target/Fleet 独立门禁（TEST-TARGET-FLEET-001）

P4、Edge、Go、PostgreSQL 与 Web 的 Module Complete 必须分别证明：

1. `contracts/target/v1`、`contracts/fleet-operation/v1` 与 `p4-target-fleet/v1` schema/lint/breaking/golden 通过；Go/Rust/TypeScript/testkit 对 target identity、incarnation/assignment/application generation、wave/child/parent状态、canonical digest和错误一致；
2. stable target ID永不复用；duplicate endpoint/device_id/role、alias collision、retired reuse、same key different digest、unknown major/profile、oversize inventory和external provenance drift在canonical mutation前拒绝；NetBox/CMDB candidate不能自动active；
3. 1/2/N个exact BMv2 target分别拥有唯一device_id/endpoint/port map/P4Info、Edge actor/StreamChannel/election/application generation/journal/queue；同target双assignment/双primary、wrong target/device/actor epoch、过期/撤销lease、耗尽/越界election range和old assignment result都在P4 Write前被fence；
4. register→verified→active→draining/disabled/quarantined/retired、Admin step-up、assignment handoff、old Edge失联或仅与Control分区但仍可达target、late lease/control response、higher-election/range reseed、read-only pipeline/selector/entry reconcile与restart通过；target lifecycle、qualification、availability和effect状态不混装；
5. exact Decision、non-claimable parent和bounded per-target child intents在一个短事务中原子形成；dispatcher只能claim `effect_intents`，future-wave gate不可提前claim，parent/target table无lease/worker语义；数据库事务期间外部P4/gNMI等待为零；
6. static canary/ordered waves与`fail_fast|continue_isolated|manual_gate`均通过；target set/ordered membership中途漂移时不扩权，manual gate绑定completed-vector digest，fail-fast只block未开始child；
7. applied/failed/blocked/not-started/unknown/reconciling组合产生唯一parent projection；任一unknown保持reconciling，不存在多数成功/百分比推定applied。per-target current/readback保持真实，partial rollback创建新parent/child并逐target选择exact previous；
8. 一个target slow/hang/restart/unreachable/P4Info drift/queue-disk-FD exhaustion时，其他target的mastership/effect/readback/source继续满足公平与绝对deadline；reconnect storm、full sweep和条件gNMI不能饿死effect；
9. PostgreSQL failover、PITR/restore/clone/rewind通过target-control incarnation轮换、old assignment/preflight/claim fence和逐targetread-only reconcile；恢复出的registry/wave/current不能自行打开writer或下一wave；
10. Managed Targets/Fleet Operations页面通过URL/cursor、target×stage矩阵、wave timeline、all states、step-up/maker-checker、original-operation query、SSE gap/cache purge、WCAG和大fleet性能；页面无raw CLI/gNMI Set/automatic takeover、无单一绿色聚合；
11. `target-gnmi-readonly/v1`未触发时有NOT_APPLICABLE证据；触发时exact gNMI spec/proto/model/path/target profile、mTLS、Capabilities/Get/Subscribe、rate/queue/gap/freshness和Set拒绝通过。Stratum只作target-side；ONOS/厂商controller/P4Runtime Shell daemon无writer credential；
12. `PERF-TARGET-FLEET-001`的0/1/2/N、最大rule/target/operation/wave、故障公平、DB/API/UI和3,600 秒 soak达到冻结绝对门槛；N或门槛未冻结时为`HOLD/NOT RUN`，不以单target、BMv2 demo或第三方平台成熟度替代。

### 14.15 流量生成与回放独立门禁（TEST-TRAFFIC-001）

P4/testkit Module Complete 和涉及流量的 system E2E 必须同时证明：

1. `contracts/testkit/traffic-replay/v1` 与 `p4-traffic-replay/v1` 的 schema/lint/breaking/golden 通过；unknown class/mode/DLT/target/profile、字段缺失、oversize、digest drift 和 unsupported transformation 在发包前稳定拒绝；
2. PTF/P4Testgen 作为 `ADOPT for test` 的 packet harness/oracle 以 exact p4c/P4Info/tool/backend digest运行；Tcpreplay/tcprewrite/tcpprep 作为 GPLv3 `CONDITIONAL test-only` 只在许可证/NOTICE/image审查后启用；Mininet/netem 作为软件 topology/impairment test dependency 固定 exact kernel/iproute2/profile。缺任一可选工具时对应 mode 为 `HOLD/NOT RUN`，不得用自制 shell fallback 伪装同一资格；
3. generated-packet、synthetic-flow、curated-pcap L2 replay、live-session 四类 fixture 各有正/负 case；L2 replay 证明不建立连接状态，真实 TCP case证明 handshake/request/response来自有状态 endpoint。工具退出成功不能改变 mode 语义；
4. 单向与双向 fixture 的 ingress/egress/client/server map、tcpprep/等价 direction cache、MAC/IP/port/VLAN/checksum/length/MTU rewrite和 original/transformed digest通过 golden；错误方向、旧 cache、原地覆盖、unexpected packet mutation均失败；
5. recorded timing、multiplier、fixed pps、fixed Mbps、topspeed分别运行并记录 achieved/error；loop/packet/byte/duration/preload/backend上限生效，要求同时选择多个 speed mode、unbounded loop或超限时拒绝；
6. netem delay/jitter/loss/duplicate/reorder/corrupt/rate/seed 和 qdisc readback通过；TSO/GSO/GRO/checksum offload on/off、MTU、kernel timer/TSQ影响被 profile显式区分。相同 fixture在不等价 environment下只能比较趋势；
7. oracle 矩阵至少包含 sender error、sender accepted但 test ingress/DUT未见、DUT ingress但rule未hit、rule hit但错误egress/drop/mirror、完整成功；每层使用独立 observation并对 packet mask/count tolerance做明确 golden，任何缺失层不得自动提升；
8. 至少一个最小项目自有 synthetic corpus 与一个外部 curated PCAP slice 通过 provenance、SHA-256、license/use/redistribution、citation、payload/privacy、ground-truth join/coverage、retention/deletion 门禁；CIC-IDS2017/UNSW-NB15/CTU-13 只按实际通过项使用，未得到再分发权的原始数据不进入仓库、通用 CI image 或发布制品；
9. 专用 test namespace/VM、interface/route/P4 target allowlist、无生产 credential/data、默认 egress deny、root/capability最小化和 test database/tenant隔离通过负向测试；manifest/raw PCAP不能注入 shell、argv、path、host interface或P4Runtime mutation；
10. malformed/corrupt/truncated PCAP、runner/sender/receiver/BMv2 crash/hang、capture/queue/disk/memory/PID/FD/inode exhaustion、cancel/deadline与宿主重启故障后，在冻结 deadline内停止发送并精确清理本 run namespace/veth/qdisc/process/temp；cleanup不能证明时宿主quarantine且本次FAIL/HOLD；
11. BMv2/Mininet结果仅可获得 exact software target 的功能、恢复和软件容量资格；硬件 profile必须在真实 target/NIC、校准发生器、双端计数、counter/byte/action语义和绝对容量门槛上重跑。TRex/等价高性能发生器只有硬件 profile证明普通 runner不足时才条件引入，不进入生产或成为第二 P4 writer；
12. 所有结果写入公共 evidence envelope，引用 fixture/transformation/tool/environment/P4Info/image digest与原始结构化 sender/DUT/counter/oracle artifact；检测 replay、rule outcome、software performance和hardware qualification使用不同 scenario/evidence ID，禁止复制、重命名或聚合成一个 PASS。

### 14.16 成熟组件复用门禁（TEST-REUSE-001）

每个 `ADOPT`/已触发的 `CONDITIONAL` 组件在相应模块 Module Complete 前必须证明：

1. `CONTRACT-SUPPLY-001` registry、全系统/module inventory、lockfile、OCI/image/file SBOM、LICENSE/NOTICE、provenance、exact digest 与运行配置一致；未登记 direct/transitive/generated/vendored/runtime asset 或 source/output drift 稳定拒绝；
2. Buf/protoc/OpenAPI generator 等生成/lint 工具使用 digest-pinned image/binary与固定配置；schema lint、breaking change、source/generated golden 和 current/previous reader matrix通过，工具升级不会静默改 wire、nullable/presence、错误或 pagination；
3. PgBouncer transaction pooling 下 short transaction 通过，而 migration、LISTEN/session state 明确走 direct/合格 pool；自建 profile 的 pgBackRest/等价 backup、WAL/PITR/隔离 restore 与 Patroni/等价 failover 使用真实拓扑验证，存在配置或进程不算 RPO/RTO PASS；
4. OpenTelemetry Collector、Prometheus、Alertmanager、Grafana 全部断开、queue 满、backend 慢、重启和配置漂移时，核心链不阻塞、业务事实不丢失/改写、规则 identity 不产生高基数 series、silence/annotation/action 不产生 Incident/Effect/P4 mutation；
5. PTF/P4Testgen/Tcpreplay/Mininet/netem/TRex 只能按 ADR-0012 的隔离 test profile 使用；只有 P4Runtime Shell 另可使用明确的 production read-only diagnosis profile。CI/部署检查证明没有第二 P4 writer、常驻 shell/traffic daemon、未经 Edge 的 production Read/Write、生产网发包或工具 credential 泄漏；GPL/其他许可证、NOTICE、image/source obligations 与条件采用状态均可追溯；
6. Syft 生成 SPDX/CycloneDX 或资格化格式 SBOM，Trivy/选定 scanner 使用锁定且有 freshness 的 database snapshot，Cosign/组织 PKI 验证 exact OCI digest/publisher/provenance/offline bundle；扫描/签名通过不跳过功能、许可证、sandbox、兼容或 revoke 门禁；
7. 每个组件执行 capability/credential/network/filesystem/database 权限负向测试、资源/queue/retry/timeout/retention/health 上限、upgrade/rollback/current-previous matrix 和完全移除/替换演练；组件故障只能按 registry 的 fallback 退化；
8. 架构/依赖图 policy 拒绝被列为 `REJECT` 用途的 Kafka/Redis/NATS/Temporal/Argo 核心事实/effect queue、第二 P4 controller、Grafana mutation、第三方控制面和未批准通用授权状态机；仅换包名、经 sidecar/插件/Helm 间接引入同样失败；
9. managed service、Kubernetes/Helm、Keycloak/其他 IdP、Loki/Tempo、service mesh 等条件组件只有触发条件、责任边界、数据驻留、HA/退出和成本证据满足后才进入具体 deployment profile；未选择不影响首期核心功能；
10. 所有结果遵守 ADR-0006 的四维资格字段、exact claim scope与聚合规则；缺少上游版本、许可证、offline artifact/database、真实 HA/restore 环境或 raw evidence 时为 `HOLD/NOT RUN`，不能以“行业成熟”“官方镜像”或 demo 成功标为 production qualified。
11. P4Runtime/OpenConfig gNMI/Stratum/NetBox/Ansible/Nornir/ONOS按ADR-0015的用途矩阵验证：P4Runtime是唯一生产P4协议；gNMI只读为条件profile；Stratum仅target-side；NetBox只给candidate；Ansible/Nornir只离线/test；ONOS/其他controller writer被依赖、credential和网络负例拒绝。关闭所有条件组件后target registry、fleet effect与单writer正确性不变。

### 14.17 在线遥测与推理热路径独立门禁（TEST-TEL-INF-001）

P4、Edge、Central Inference、Go与适用部署profile在Module Complete前必须分别证明：

1. `contracts/telemetry/v1`与`contracts/inference/v1`的schema/lint/breaking/golden通过；Rust/C++ Gateway/backend/Go/Python test oracle对source/window/time/quality、Protobuf/framing/tensor dtype/shape/order/offset/bytes、request/attempt/pool identity、result→Event digest/error一致；
2. P4/BMv2使用`telemetry-p4-window/v1`产生有界aggregate，并对bank flip/freeze/barrier/sequence snapshot、read/clear/advance、counter/register width/reset/wrap、target/flow/window上限形成packet-input→expected aggregate golden；普通多entity Read与返回顺序不能获得atomic snapshot PASS；
3. DigestList配置、duplicate过滤、list/Ack、cache timeout、server/client restart、slow consumer/drop和mastership change通过；测试明确证明Ack只发生在Edge telemetry WAL durable后，但Ack/`max_timeout_ns=0`/`max_list_size=1`都不提升为reliable/full coverage。PacketIn/clone的sample/truncate/rate/queue/oversize/drop同样有界；
4. source identity覆盖observation domain/point、P4/application/source runtime epoch和sequence；IPv4/IPv6、单向/双向flow、VLAN/tunnel、首片/非首片fragment、端口不可用、event/export/ingest/finalize time均有正反golden，禁止跨source/generation/window拼接；
5. `[start,end)`、watermark、max out-of-order、allowed lateness、idle/reconnect、open→final、late-after-final、zero traffic、gap/partial/not-covered/not-measurable通过；只有final+valid窗口进入Central Inference，late或缺测不重开/改写Event、不补零/默认normal；
6. sampling profile固定algorithm/rate/seed/hash、eligible population、coverage和可选加权公式；不支持sampling的模型稳定拒绝sampled window，sample count不冒充full packet count；
7. `inference-central-grpc-batch/v1`通过真实跨主机Rust↔Gateway：mTLS identity/rotation、Protobuf framing、max message/tensor、channel reuse、resolver/endpoint refresh、bounded in-flight、deadline/cancellation、backpressure、same-generation retry/dedupe与old pool endpoint fence；ASan/UBSan/TSan及适用sanitizer无越界、race或use-after-free；
8. malformed version/frame/offset/alignment、integer overflow、overlap、record/tensor count、dtype/shape/bytes、deadline、profile/model/logical-pool/route/generation与unknown field/major均在大分配/运行模型前拒绝；wire无法注入pointer/path/FD/argv/executable content；
9. batching覆盖empty/1/typical/max records/bytes、Edge无等待coalescing、Triton max queue delay/preferred batch/instance group、earliest deadline、Gateway/Triton/selected runtime queue full、worker slow/hang/crash、cancellation/late result与backpressure；所有queue/in-flight/WAL/response上限生效且不阻塞P4 effect/readback；
10. CPU/CUDA分别真实启动并证明selected=observed；Edge→Gateway、Gateway→Triton和适用的host↔device分别报告actual copy。Triton shared memory/I/O Binding/device tensor/lifetime/stream/sync只有exact CUDA profile通过才可声称copy优化；CPU profile另验thread/affinity/NUMA/arena。preallocation与numeric golden同时通过，不以Triton/ORT执行时间替代端到端；
11. telemetry/source WAL→final window/input WAL→central result/dedupe→result WAL→Edge→Go→PostgreSQL Event commit→canonical ACK→checkpoint/compaction每个边界都注入crash/timeout/duplicate/response loss；证明零永久丢失、零双Event，same identity+same digest幂等，same identity+different digest冲突；
12. PACKET_MMAP条件profile覆盖TPACKET_V3 block/ring/fanout、flow affinity、snaplen/truncation/checksum/offload、kernel/user drop、interface drift与资源上限；AF_XDP触发时另覆盖XDP_DRV/XDP_SKB、zero-copy/copy、RSS queue/core/NUMA、UMEM/ring/need-wakeup/multi-buffer和fallback拒绝。未触发条件profile不要求实现，但必须由registry证明不影响当前feature/target/SLO；
13. `PERF-TEL-INF-001`对CPU/CUDA分别在0/typical/max target/flow/window/batch、steady/peak/saturation/full-pool outage、source/drop/gap、network/Go/DB slowdown和3,600 秒 soak下通过；HA profile另验N+1、single replica/failure-domain loss。报告observation point→Event ACK端到端p99、throughput、CPU/RSS及适用的VRAM/GPU、network、allocation/copy、queue/WAL/coverage，绝对门槛未冻结时只能`HOLD/NOT RUN`；
14. security/ownership负例证明只有Edge拥有P4/capture/source/window/canonical router，Gateway只验证/适配，Triton/ORT只batch/execute，Go只收result且commit后ACK；无raw payload进入inference/DB/log/metric/Frontend，无第二P4/source/router/queue，无Edge-local或CPU↔CUDA/异构backend静默fallback；
15. BMv2、software mirror、Central Inference CPU/CUDA与hardware/NIC profile证据严格分开；BMv2/gRPC/Triton/ORT microbenchmark或Digest收包成功不能被重命名为hardware coverage、line-rate或production-qualified end-to-end inference。

## 15. 迁移与切换需求

### 15.1 vNext 独立实现（MIG-001）

vNext 应在独立目录、schema 和部署 profile 中开发。不得先在现有主路径大规模删除功能，再等待新系统补齐。

### 15.2 Shadow（MIG-002）

- 新系统可以在旧/新系统迁移或隔离 `REHEARSAL/NOT QUALIFIED` 环境读取复制的输入或镜像流量进行 shadow inference；本节不授权 `model-rollout-pool-generation/v1` 的生产 pool 创建 candidate/online-shadow/weighted split或同一shard双route；
- shadow 只能比较 Event/decision/artifact，不得形成第二条真实 P4 effect；
- 旧新结果必须通过稳定 identity/golden mapping 比较；
- shadow 差异必须分类为 contract、numeric、timing、quality 或 implementation defect。

### 15.2a 在线模型演进与切换（MIG-INF-001）

- model catalog/binding 使用 clean-start `model_platform`，不导入 legacy model signer/key/release/qualification 状态、mutable alias 或旧数据库 active pointer；旧模型只通过可重建 artifact、frozen replay 和 explicit qualification 形成新 revision；
- 首个模型按contracts/profile→Offline ML bundle/qualification→Go initial rollout/pool envelope→Central pool generation startup、所选availability profile的replica/readback/capacity资格（HA另含failure-domain/N+1）→per-shard CAS explicit current顺序上线，禁止先在production config写文件名/tag/repository version再补manifest/evidence；
- compatible revision先使用frozen replay/隔离rehearsal完成差异分类，再启动新pool generation并按shard drain/route/CAS；旧per-shard current成为exact previous，但历史Event保留实际model/shard/pool identity，不重新推理或覆盖分类；
- feature/label/output/runtime major演进采用expand/contract：先部署可解析old+new schema/wire的Edge/Gateway/Go reader，资格化新producer/model/backend，再按shard切换logical pool/binding，支持窗口后停止旧读取。兼容两版reader不等于同一endpoint动态选模型；wire不兼容时使用独立版本化pool/profile，由Edge在route withdraw/drain后单路切换。禁止长期双写canonical Event或用默认label吞掉未知类别；
- rollback只通过新的rolling pool-binding operation回到仍qualified、未撤销且reader/runtime compatible的exact previous；无法回滚时继续已确认current或停止对应shard，不能恢复legacy发布链、Edge-local/动态plugin model或无证据模型；
- Triton是首期Central Inference内部执行组件，但其repository/version/ready状态不迁移或同步为PostgreSQL canonical binding；MLflow/KServe/Ray Serve/TensorFlow Serving等外部系统只能在条件profile中导出不可变evidence/artifact，不拥有alias/stage/route/current。
- `model_platform` migration 必须具有 immutable version/checksum，并在隔离测试库覆盖 empty→current、上一受支持 schema→current、重复执行、partial/interrupted migration、checksum drift、并发 rollout 被 migration 拒绝/排空、expand/contract rollback、无损 failover、PITR/restore/clone/rewind 与 operation replay；每个 case 固定 old/new fixture digest、预期表/约束/只读字段、lock/statement deadline 和 evidence result。restore orchestration必须在开放writer前轮换从未使用的`model_control_incarnation_id`，旧 envelope/readback/action/handshake失效；再以new operation/new generation恢复并readback exact pool，逐 shard执行route withdraw/drain/CAS/commit，未完成前不得恢复 canonical ingest；
- 模型事实兼容矩阵至少覆盖当前与上一受支持release的producer/reader、central gRPC wire、Gateway/Triton、ORT CPU+CPU feature/thread/NUMA/RAM或ORT CUDA+CUDA/cuDNN/driver/GPU/VRAM、optimization artifact、persisted Event/InferenceResult、taxonomy/class order与Web projection；矩阵机器可读列出`read|write|rollout|rollback|reject`、support cutoff和证据digest。CPU与CUDA证据独立；更早版本最多受限只读或稳定拒绝；unknown field/label不得映射到既有label或旧effect policy。

### 15.2b 在线遥测源与中央推理 Wire 演进（MIG-TEL-INF-001）

- vNext不迁移legacy Python P4Runtime controller、逐包JSONL/fsync、insecure channel、旧21列feature或三分类class map；只将可重建P4/flow/window输入、source durability顺序与frozen behavior fixture转成新的telemetry/inference golden；
- source profile、window/flow/quality contract、feature schema和central inference wire分别演进。兼容minor必须通过old/new producer-consumer与persisted WAL/Event reader matrix；改变field order/width/unit/window/direction/watermark/quality、Protobuf/tensor语义或retry identity必须升major，禁止在同logical endpoint猜版本；
- wire/source backend切换必须先部署能读取new contract的Edge/Gateway/backend/Go image与独立logical pool/profile，随后对一个shard停止新window、drain old in-flight、确认input/result WAL和Go ACK watermark、提升source/route epoch、CAS/commit新profile并建立baseline，最后在支持窗口后移除旧reader/pool；同一shard不得长期双写window/result/Event；
- 从P4 aggregate切换到PACKET_MMAP/AF_XDP或反向切换不是透明fallback：必须使用新source profile/runtime epoch，证明observation point、flow/window、sampling/coverage和feature等价或记录accepted difference。不能等价时使用新feature/model binding/scope，不得把两源数据拼接为连续window；
- rollback只回到当前reader仍支持、资源/安全/性能仍合格的exact previous source/wire/pool profile，并同样执行withdraw/drain/new epoch。previous不兼容时保持shardHOLD/unavailable；不得恢复逐包Digest可靠队列、逐记录JSON/HTTP、Edge-local inference或未资格capture路径。

### 15.2c BMv2 无状态防火墙迁移与演进（MIG-P4-FW-001）

- vNext 不迁移 legacy Python P4Runtime controller、runtime state、数据库 ACL 表、临时 blocklist、主机 UFW/nftables/iptables 规则或旧 writer 身份。旧仓库只提供 clean-room 行为 fixture：例如 exact IPv4 五元组 allow/drop、优先级、默认动作和 packet outcome；legacy `1,024` exact-entry/global-drop-counter 实现不能被标为 vNext `4,096` normalized-rule/per-entry observation 资格；
- 初次上线顺序必须为 firewall contracts/profile/P4Info freeze → P4/Edge/Go/DB/Web 各自 Module Complete → deny-write/read-only boundary rehearsal → clean target 装载 exact pipeline → 创建 explicit baseline revision/default → Admin maker + Operator checker R3 activation → readback/CAS → response overlay 资格 → 流量/故障/性能证据。不得先复制旧表项或打开双 writer 再补事实；
- 旧系统与 vNext shadow 期间只能比较 normalized policy、compiled entries 和 packet outcome；旧 writer 必须对 vNext test target 无凭据，vNext 必须对旧 production target 无凭据。shadow suggestion/Artifact 不产生真实 rule，比较结果明确 software target、capacity 和 unsupported differences；
- firewall contract/profile minor 演进必须通过 current/previous P4/Edge/Go/Web reader-writer 与 persisted revision/operation matrix；改变 match/action/default/priority/fragment/bank/selector/counter 语义必须升 major。先部署能读取 old/new 的控制面，再在独立 pipeline cutover 中重建 exact policy；禁止长期双写两个 table layout；
- cutover 和 rollback 始终遵守单一 P4 writer。pipeline/P4Info 变化前停止新 intent、排空/冻结 journal、记录 active baseline/overlay exact revision，装载新 pipeline 后重新编译、readback 和创建新 observation epoch；任一步不确定则 target `HOLD/read-only`，不能用 Linux firewall 临时顶替后继续声称 P4 protection。

### 15.2d 单 Target 到受控 Fleet 的演进（MIG-TARGET-FLEET-001）

- vNext clean-start创建`target_fleet`，不导入legacy controller process/session/election/runtime、设备别名表、Mininet临时拓扑、ONOS/NetBox current、Ansible inventory或厂商controller state。旧信息只能作为有provenance/digest的candidate，经duplicate/profile/identity验证和Admin确认生成新的stable target；
- 首个上线target也必须走`target/v1`并由Edge TargetActor管理，禁止先保留“无target_id默认switch”再为第二台设备补多租户分支。单target API/表/metric/配置必须使用与N-target相同的target-scoped contract、queue和fence；
- 扩展顺序为target/fleet contract+schema → Go Registry/Fleet与Edge Supervisor各自Module Complete → 1-target行为等价 → 2-target隔离/故障 → N-target绝对容量 → Frontend矩阵 → 正式pairwise/system E2E。早期多BMv2 wire测试仍为`REHEARSAL/NOT QUALIFIED`；
- 从一个Edge迁移target到另一个Edge时使用`REL-TARGET-FLEET-001` assignment handoff，不复制旧journal/election文件后并行启动。旧writer先drain/失去mastership，新writer以新assignment/application generation和可证明更高election read-only reconcile后再开放；无法证明时target HOLD；
- target/fleet contract minor演进覆盖current/previous Go/Edge/Web producer-reader和persisted registry/parent/child事实；改变identity、assignment/election、wave/failure、parent aggregation或gNMI mutation边界必须升major/需求基线。禁止旧新registry双写、动态target set或用兼容adapter引入第二queue；
- gNMI只从无到`target-gnmi-readonly/v1`条件启用；启用/关闭不改变target identity、assignment、P4 current或effect。未来gNMI Set/gNOI/完整NMS不是minor演进，必须新owner、治理、故障、迁移、回滚和基线；
- migration matrix覆盖empty/上一受支持schema、duplicate identity、assignment/parent-child约束、partial/interrupted、old/newreader、failover/PITR/clone/rewind与target-control incarnation。migration/restore阶段无P4/gNMI副作用，不自动接管external inventory中的设备。

### 15.3 数据迁移（MIG-003）

- 首期默认 clean-start vNext schema；
- 不自动导入 legacy Workflow、Review、Auth、Event v2 或签名 registry；
- legacy review/approval、旧 operator identity 和旧 policy eligibility 不得转换为 vNext Decision 或 Intent；历史只读展示必须明确标记 non-authoritative；
- 若需要保留历史 Event/Incident，必须通过单独、可验证的离线 import/export 项目完成；
- 历史数据不得通过运行时双写或兼容 trigger 导入；
- 按照已确认的 `DEC-006`，旧 Event/Incident 默认不导入 vNext，只归档；确有后续产品需求时再建立独立离线 import 项目。

### 15.4 Cutover（MIG-004）

- cutover 前必须通过三机 full E2E、故障矩阵和性能资格；
- 切换时只能有一个 P4Runtime writer；
- 切换必须冻结新 effect、排空/确认旧 outbox、验证 generation/mastership、切换 writer、执行 readback；
- 回滚只能回到已验证版本和明确 state boundary，不能同时恢复两条 writer/effect path；
- 旧系统在观察期只读保留，随后归档。

### 15.5 滚动升级与事实兼容（MIG-005）

- PostgreSQL schema 使用 expand/contract；先部署可兼容读取的 schema/client，再启用新写入，最后在支持窗口结束后停止旧字段读取；禁止长期双写两套 proposal/decision/effect 事实；
- 每个 release 必须测试上一受支持 Go/Frontend/Edge/Plugin Host 与当前版本的 old/new client-service 组合，以及当前代码读取上一版本持久化 Proposal/Decision/Intent 和 plugin revision/qualification/active binding；Frontend 矩阵还必须覆盖 session cookie、CSRF、SSE event/cursor、deep link、static manifest 与上一 asset set 回滚；
- 新 governance profile、role mapping、P4Info 或 canonicalization version 激活时必须定义旧 proposal 的 cutoff；不能完整重验的旧 proposal 只读保留并标为 `stale`，不得继续 authorize；
- 未知 major 或未验证 minor 必须拒绝 mutation；只读兼容不得被解释为写入兼容；
- Edge/P4Runtime 滚动升级必须先停止旧实例 claim/write、确认 journal 与 mastership，再让新实例取得更高 election/mastership 并 readback；任何时刻不得存在双 writer；
- rollback 只能回到仍能读取当前 durable facts、且通过兼容矩阵的版本；否则进入 R0/read-only + effect `HOLD`，不得通过丢弃新字段恢复写入。

### 15.6 插件平台迁移与切换（MIG-PLUGIN-001）

- plugin catalog、qualification、activation、revocation 和 audit schema 采用 clean-start；不得导入 legacy Workflow/Task/Review/checkpoint、旧 plugin registry 或历史签名控制表；
- `masi.analysis.langgraph` 必须作为新的 immutable plugin revision 注册和资格化；旧 Analysis Graph 只提供脱敏 golden/行为矩阵，不转换为 active binding 或运行时 checkpoint；
- 上线顺序为 schema/contract expand → Manager/Host deny-all → conformance qualification → official Analysis staged → shadow → explicit active；不得先允许未资格插件运行再补 manifest/权限/证据；
- plugin revision 升级采用 side-by-side，不长期双写 task/artifact 或同一 canonical projection；切换前必须记录 input/output 差异、性能、错误和 accepted difference；
- rollback 仅切换 active pointer，不覆盖 artifact、config、manifest、历史 result 或 audit；旧 revision 已撤销或不兼容时保持 plugin unavailable；
- 平台切换、回滚或灾难恢复不得改变 P4 mastership、effect intent、authorization 或核心 Event identity；插件 shadow 与旧系统 shadow 都不能产生真实 effect。
- `plugin_statistics` 使用 clean-start schema，不导入 legacy dashboard、Prometheus/Grafana panel、Analysis checkpoint或任意插件私有统计表。上线顺序为statistics contract/profile→Go/DB/Host/Web各自Module gate→deterministic conformance plugin→正式E2E；不得先让插件直写DB/前端再补投影；
- statistics schema/display minor演进必须覆盖current/previous Go/plugin/Web reader与persisted Artifact；改变metric/quality/temporality/display执行能力或放宽代码/URL边界必须升major/需求基线。回滚Web或插件时历史Artifact保持只读、带exact producer与stale/revoked标记，不恢复旧UI代码或无校验current；
- migration matrix覆盖schedule/run/artifact/current/history的empty/上一受支持schema、重复/中断/checksum drift、old/new reader、unique/CAS、retention/legal hold、failover/PITR与binding/revocation重验；migration/restore不执行插件、不开schedule、不创建effect，恢复出的current在重验前为stale。

## 16. 验收条件（ACCEPT-001）

vNext 完成时必须证明：

1. 所有首期模块先分别完整实现并独立通过契约、黑盒 E2E、故障恢复、性能和 Module DoD；在第一个正式 pairwise integration 运行前，全模块完成门禁已有可复核 PASS 证据；
2. 通过`traffic-replay/v1`验证的generated/synthetic/curated PCAP fixture按`TEST-REAL-E2E-001`真实经过BMv2、Rust telemetry、启动前所选Central Inference CPU或CUDA profile、Go ingest和PostgreSQL，并在Frontend显示；需要TCP/应用状态的场景另以live-session fixture通过；
3. 不同 generation 的数据不被拼接，缺测不补零，duplicate 不产生第二事实；
4. Proposal/Decision 保持不可执行且不会成为第二队列；只有有效自动 policy 或绑定 exact digest 的有效授权，经当前事实重验后才能产生唯一 intent；
5. R2 self-approval、普通 incident R3、未通过 `FUNC-FW-001` typed maker-checker change API 的 baseline R3、过期、stale、缺证据和 scope 不足均 fail closed，测试证明这些路径零 Edge RPC；
6. P4 effect 只由 Rust Edge Agent 执行，Go durable intent、operation journal、真实 readback 和 PG CAS 最终一致；
7. timeout-after-effect、崩溃、重启和断网不产生盲写或重复 P4 effect；
8. Analysis Plugin 通过 A2A/MCP/LangGraph/LLM 生成 grounded、不可执行 Artifact，并逐项通过 `AGENT-COMPAT-001` capability matrix；未完成前不声称完整兼容；
9. Plugin Platform、Runtime Host 或任一插件 disabled/unavailable 时核心功能和非插件页面保持不变；
10. Analysis/其他插件、Frontend、Central Inference、Rust Edge和Rust Plugin Runtime Host均不能越权写核心PostgreSQL；
11. PostgreSQL HA/PITR/restore drill、连接预算、分区、retention 和监控通过；
12. 三机部署和至少一种生产扩展拓扑通过网络分区与恢复测试；
13. Redis 不存在时系统功能完整；若未来启用，完全清空 Redis 后核心正确性不变；
14. 旧 Event v2、legacy Review/Auth/Workflow、自研签名发布链和 legacy P4 writer 不参与 vNext profile；
15. 所有测试、benchmark、镜像、配置、role/profile mapping 和数据库 schema 都有可追溯版本/hash；
16. 文档与证据明确分开`level/applicability/result/qualification`，并使 rehearsal、E2E结果和production资格都只能由精确claim scope推导；
17. 受控通用插件平台完成 Go Manager、独立 Rust Host、strict manifest、capability、qualification、activation、drain、revoke、rollback 和审计，并证明它不是公共市场或进程内任意代码 loader；
18. `service-grpc/v1`、`wasm-component/v1` 与 `a2a-agent/v1` 均通过公开契约和 conformance/真实官方插件门禁，未知 kind/major、错误 digest/publisher 和超权限稳定拒绝；
19. `masi.analysis.langgraph` 作为首个官方插件通过平台准入后，经 Manager-controlled direct A2A/MCP 边界生成 grounded、不可执行 Artifact；它不经 Runtime Host 代理，平台或该插件 disabled/unavailable 时核心功能保持不变；
20. 生产插件全部使用 digest-pinned artifact、受信 publisher policy、SBOM/provenance、离线可重验 bundle、sandbox/resource profile 和撤销事实；开发 unsigned fixture 永不获得生产资格；
21. Plugin Manager、Runtime Host、任何插件及其输出均不能写核心 schema、创建 Proposal/Decision/Intent、调用 Edge/P4、成为第二 effect queue 或进入核心热路径；
22. P4Runtime 1.4.1 profile、application generation、election、pipeline digest、atomicity 和 canonical readback 合同通过真实 target/fake fault matrix，外部 preflight 与 PostgreSQL CAS 使用同一版本化 precondition token 且无事务跨外部等待；
23. `contracts/profiles/v1`、`fault-evidence/v1`、`performance-environment/v1`、traffic replay 结构化结果、DB restore manifest 和 model qualification manifest 均有 schema、digest、生成/验证工具和 evidence；
24. MCP endpoint 明确为 `masi-mcp-readonly/v1` restricted profile，WASI 0.2 明确为首期 qualification profile 而非最新稳定版本；未知或未资格化 profile fail closed；
25. 所有早期真实boundary结果保持`REHEARSAL/NOT QUALIFIED`；正式pairwise/full E2E在全模块Module Complete后从干净环境真实启动参与链路的发布候选服务、走真实公开边界重跑并形成独立证据，fake/mock或旧rehearsal结果不得替代。
26. Frontend 以独立 Vue 3/Vite SPA 交付，生产同源 `/api`/`/events`/OIDC，Go 保持会话、CSRF、授权和 canonical projection owner；浏览器无 token/secret、无 BFF、无核心直连和无离线 mutation；
27. Overview、Detection、Evidence、Effects、Analysis、Plugins、Operations/Audit 的 URL/deep-link/cursor/all-state 交互通过；Analyst/Operator/Platform Admin/Auditor 的危险流程符合 exact context、maker-checker、step-up、原 operation 和不可执行 Agent 边界；
28. `web-spa/v1`、`web-browser/v1`、`web-performance/v1` 的 generated client、SSE、bundle、Core Web Vitals、浏览器、WCAG 2.2 AA、soak 和上一 Go/Web compatibility/rollback 证据通过；
29. Frontend dependency lock、SBOM、LICENSE/NOTICE、来源/修改登记与离线构建通过；成熟基础库按 exact version 复用，1Panel/sub2api 的应用源码、品牌资产和控制面未被未经批准复制；
30. Web/Frontend 关闭、回滚、SSE 故障、Analysis/Plugin 不可用或浏览器缓存清空时不改变 PostgreSQL 核心事实、P4 effect、安全授权或非相关页面可用性。
31. 每条 active effect rule 可追溯到 exact operation/readback、observation epoch 和 target/P4Info/generation；安装确认、数据面命中和独立结果验证三层状态分开，zero traffic/reset/gap/not-measurable 不被显示为 0% 或失败；
32. direct-counter/eligible-counter、Edge bounded sweep、Go/PostgreSQL rollup、Rule Effectiveness UI 和 `TEST-RULE-001` 的 PTF/packet-oracle/fault/performance/browser 门禁通过；任何 counter 增长均未被声称为攻击阻断或处置成功的充分证据；
33. per-rule identity 未进入 Prometheus labels，Grafana/Alertmanager/OTel 不成为规则事实或 mutation 路径；no-hit/dead-rule 只产生只读复核候选，所有真实改变继续经唯一 proposal/decision/intent/Edge/readback/CAS；
34. ADR-0009 的成熟组件登记对每项给出 `ADOPT/CONDITIONAL/REJECT`、exact artifact/license/SBOM/profile/故障与退出证据；组件缺失或失败不产生第二事实源、第二 P4 writer、第二 effect queue、隐式授权或未审计生产依赖。
35. `MOD-REGISTRY-001` 的九个首期模块均有独立 Module Complete 证据；P4、PostgreSQL 和 Offline ML qualification target 未因“不是常驻服务”而遗漏。
36. `contracts/model/v1`、`model-runtime-central-cpu/v1`、`model-runtime-central-cuda/v1`、`availability-single/v1|availability-ha/v1`、`model-rollout-pool-generation/v1`、model qualification/pool-startup/readback evidence、ADR-0017及ADR-0010/0011保留语义已落地；ADR-0016只保留v1.13历史。同合同模型无需修改Edge/Go业务代码，通过exact repository/profile/pool binding、per-shard logical-route切换和exact previous rollback完成替换。
37. single-label、multi-label、anomaly-score/open-set 与新增类别通过稳定 label taxonomy/output adapter 表达；未知/新增 label、OOD/abstain 和非生产 comparison 不自动获得 Event policy/effect eligibility，不直接产生 Proposal/Decision/Intent/P4 mutation。
38. Go/PostgreSQL是model revision/qualification/rollout/pool+shard binding唯一事实owner；每个Triton instance只startup-load一个exact binding，每个shard的pool/worker readback与PostgreSQL current一致。replica/restart/readback/CAS response loss、partial mixed rollout、old worker late result、并发rollout/rollback和崩溃不产生双current、跨generation window或历史Event改写。
39. Triton+ONNX Runtime为首期ADOPT执行栈，`model-runtime-central-cpu/v1`与`model-runtime-central-cuda/v1`是并列、互斥的startup-bound选择；管理员选择、hardware preflight和actual EP readback一致，任一不符即fail closed且不自动改选。TensorRT仅可作为显式资格化的新generation条件采用，LibTorch首期拒绝；KServe/Ray Serve/TF Serving/MLflow serving control plane按ADR-0009拒绝。关闭公网/registry和全部外部serving control plane后，当前pool仍能从离线exact snapshot重建且canonical binding不变；关闭Triton不属于可降级运行，而使inference明确unavailable。
40. 每项 model contract/fault/performance/migration/compatibility证据同时保存原始结果、执行命令、时间、environment/profile/source/artifact/image/schema digest、`level/applicability/result/qualification`、claim scope、requirement mapping 和 reviewer/Owner reference；只有 schema、工具或文档存在而未实际执行时必须为 `HOLD/NOT RUN`。
41. 首期生产没有runtime model load/unload/repository polling、candidate/online shadow、weighted split或同endpoint多版本路径；new/old pool只在受控rollout/rollback grace内短时并存且不双写同一shard。startup-to-min-ready、Edge buffer、rolling capacity、mixed-state policy HOLD和rollback达到冻结门槛；选择`availability-ha/v1`时另需failure-domain/N+1门槛，`availability-single/v1`必须明确不声称HA。
42. 在线模型每个shard只有Edge一个canonical route owner；model-control incarnation、route epoch、logical pool/pool+binding generation、runtime profile、worker attempt、wire/optimization、deployment action、termination、WAL gap/resume、same-generation retry/dedupe、readback/CAS/commit和restart/quarantine均可故障注入。PITR不接受旧worker；rolling PASS分别满足所选CPU/RAM或GPU/VRAM及网络绝对门槛且不以Service/PDB/Pod/Triton Ready代替。
43. `CONTRACT-TRAFFIC-001`/`TEST-TRAFFIC-001` 通过 generated-packet、synthetic-flow、curated-pcap 和 live-session 矩阵；方向、改写、时序/速率、netem/offload、ground-truth、资源/故障/清理与隔离均有 exact profile/golden/evidence，runner 无法访问生产网络、credential、P4 target 或 effect path。
44. replay 报告分别保存 requested、sender accepted、test/DUT ingress、counter、action outcome 与 detection result，发包成功、counter hit 或标签均未被单独声称为处置/检测成功；BMv2/Mininet PASS只覆盖 exact software target，公开 corpus 与 Tcpreplay/TRex 等工具的许可证、隐私、NOTICE、分发和条件采用门禁通过后才使用，硬件容量保持独立资格。
45. `CONTRACT-TELEMETRY-001`/`TEST-TEL-INF-001`证明首期P4 aggregate主源的bank/epoch/snapshot/read-clear与source identity完整；Digest/PacketIn只保留best-effort hint/sample语义，Ack或收到样本从未被声称为逐包可靠/完整覆盖。缺测、drop、snapshot不一致、watermark迟到、sampling不足分别为partial/gap/not-covered/not-measurable，不补零或默认normal。
46. `CONTRACT-INFERENCE-001`的central batched-unary gRPC、mTLS/framing/tensor bounds、logical-pool/attempt identity、same-generation retry/dedupe/fence、Triton dynamic batch、deadline/backpressure和actual copy通过Rust/C++/backend跨语言golden、sanitizer、fault与绝对性能门槛；gRPC、Triton/ORT kernel或BMv2 microbenchmark均未被单独当作端到端PASS。
47. telemetry WAL→final window/input WAL→central result/dedupe→result WAL→Go/PostgreSQL canonical Event→ACK链在崩溃/超时/重放后无永久丢失、无双Event；Gateway/Triton/ORT success、gRPC send和Digest ACK均不提前推进source cursor。Edge仍是唯一P4/capture/source/window/router owner，Central/Go/插件不取得raw packet或第二queue。
48. PACKET_MMAP、AF_XDP、DPDK、Triton shared memory与GPU I/O Binding只在registry条件被真实feature/target/copy/capacity证据触发后资格化；Edge-local inference、CPU↔CUDA/TensorRT/LibTorch/旧模型自动fallback没有首期条件路径。显式central CPU profile是合法启动选择而非fallback；任何优化/backend不能静默替代当前profile或双写canonical window/Event。
49. `CONTRACT-P4-FW-001`、ADR-0014 与 `p4-stateless-firewall/v1` 将首期防火墙固定为 BMv2 `simple_switch_grpc`/v1model 的 IPv4 无状态软件 profile；response overlay、baseline 双 bank/selector、priority/default/fragment、permit-and-continue/drop、容量和 unsupported 语义均有 digest-pinned contract/profile/golden。
50. baseline policy revision 由 scoped Platform Admin 创建、不同 scoped Operator 以 phishing-resistant step-up 对 exact diff/default/target/P4Info/compiled-plan digest 做 R3 授权；response overlay 继续使用 R0/R1/R2 处置链。两者只产生现有、逐 target 的 `effect_intent`；fleet parent 不可执行，Rust Edge 始终是唯一 P4 writer，Web/插件/LLM/测试工具不能绕过。
51. baseline activation 在任意 partial write、readback mismatch、selector response loss、Edge/Go/PostgreSQL/BMv2 crash、CAS conflict、old generation/P4Info/bank epoch 和 rollback 下仍只有一个可证明 current；unknown 只沿原 operation readback/reconcile，绝不盲写。response overlay 的 TTL/删除为 durable fact，不依赖内存 timer 或 idle notification。
52. `TEST-P4-FW-001` 通过 0/128/1,024/4,096 normalized rules、最坏合法展开、generated/PTF packet/action/direct-counter oracle、fault/performance/3,600 秒 soak、Frontend timeline 与规则表现门禁；绝对性能门槛未冻结或未达时为 `HOLD/NOT RUN`，不能只报告相对 overhead。
53. UFW/nftables/iptables 不作为 BMv2 dataplane backend、fallback、策略事实源或 outcome oracle；test pre/post evidence 证明没有 host/eBPF/bridge/qdisc 过滤制造假 PASS。Linux 主机加固如存在，使用独立运维 profile 和所有权，不进入 MASI P4 policy/readback/effectiveness。
54. official P4 tutorial Bloom-filter firewall 不作为精确阻断实现；p4c/P4Tools/PTF/P4Testgen 可作为固定版本测试链，p4-constraints 仅在资格化后作为 defense-in-depth preflight/CI，任何工具不得成为第二 writer、运行时事实源或生产正确性依赖。
55. IPv6 detection/telemetry 可以继续存在，但首期 IPv6 firewall effect、stateful tracking、NAT、rate limit/meter、VLAN/tunnel-aware policy 和硬件 target 均稳定返回 unsupported/HOLD；只有新契约/profile/ADR、真实 target 容量/原子性/counter/outcome evidence 后才能启用。
56. `CONTRACT-TARGET-001`/`DB-TARGET-FLEET-001`建立stable、永不复用的target identity、desired/observed profile、lifecycle、assignment、不可复用lease、受限election range和target-control incarnation；duplicate endpoint/device、external inventory drift、PITR ABA、过期lease、越界election与old assignment结果不能创建双current或自动接管设备。
57. Rust Edge在同一模块内以TargetSupervisor/每target TargetActor管理1/2/N个exact BMv2 target；每个actor独占StreamChannel/mastership/application generation/journal/queue，单target断连、慢、重启或资源耗尽不会无界阻塞其他target，且全系统仍只有Edge这一类P4 writer。
58. `CONTRACT-FLEET-EFFECT-001`证明fleet parent不可claim，exact Decision在短事务中创建bounded per-target child intents，dispatcher仍只claim`effect_intents`；每target分别journal/readback/CAS，不存在第二queue、跨target两阶段提交或“多数成功=applied”。
59. static canary/ordered waves、`fail_fast|continue_isolated|manual_gate`、target-set drift、blocked/not-started/failed/unknown/reconciling、partial rollback与assignment handoff通过故障E2E；任一unknown关闭后续gate并使parent reconciling，未尝试target不编码为unknown。
60. Frontend Managed Targets/Fleet Operations通过identity/assignment/P4Info/freshness详情、target×stage矩阵、wave timeline、exact maker-checker/step-up、cursor/SSE/原operation/a11y/performance门禁；页面不提供raw P4/gNMI Set/SSH console或单一绿色fleet状态。
61. P4Runtime 1.4.1仍是生产P4 table读写与arbitration协议；条件`target-gnmi-readonly/v1`只允许exact OpenConfig Capabilities/Get/Subscribe且Set被身份/API/部署负例拒绝。gNOI、完整NMS、端口/VLAN/QoS/routing/OS/reboot/证书mutation不被暗中实现或宣称支持。
62. Stratum只有target-side exact profile可条件采用；NetBox/CMDB只产生经人工diff确认的candidate；Ansible/Nornir只作离线provisioning/test/read-only核验；ONOS/厂商controller/P4Runtime Shell daemon不持有production writer/scheduler credential。关闭全部条件组件后target/fleet/effect正确性保持完整。
63. `TEST-TARGET-FLEET-001`/`PERF-TARGET-FLEET-001`的0/1/2/N target、最大rule/operation/wave、故障公平、DB/API/UI与3,600 秒 soak达到`DEC-001`冻结的绝对门槛；N或任一门槛未冻结/未实测时为`HOLD/NOT RUN`，单target/BMv2/第三方产品demo不得外推PASS。
64. 声明production HA的Central Inference pool必须使用同一exact CPU或CUDA profile跨profile冻结的failure domain并具static N+1；same-generation单replica/failure-domain故障保持冻结SLO，全池故障只形成bounded WAL/backpressure/HOLD/gap且P4职责继续。任何`availability-single/v1`单域部署和三机共置都明确不获得production HA PASS，即使域内配置了多个replica。
65. Triton固定startup-only`model-control-mode=none`、strict readiness、auto-complete disabled、只读digest repository/backend目录和非公开model-control/HTTP；Gateway是唯一Edge mTLS入口且无durable queue/第二batch timer。CPU/CUDA由startup envelope显式选择，probe不自动改选；KServe/Ray/TF Serving/MLflow不拥有route/current。
66. `TEST-REAL-E2E-001`通过：实现阶段真实启动每个被测模块；正式pairwise和system E2E在干净环境启动相关发布候选服务，至少覆盖真实BMv2/P4Runtime、Edge、所选Central Inference runtime、Go、PostgreSQL、Plugin Host、Analysis Plugin和Web公开边界。CPU/CUDA分别形成独立证据；fake/mock、readiness、microbenchmark与rehearsal均未被当成正式PASS。
67. `qualification-evidence/v1` 已在 contracts、CI、API 与 Web 统一实现正交的 `level/applicability/result/qualification` 和精确 claim scope；不存在把 `NOT_APPLICABLE` 当PASS、把`HOLD/NOT_RUN`当level、把rehearsal改名、让 `operational-single-domain` 产生 `level=PRODUCTION`，或把CPU/CUDA、single/HA、不同topology证据互相继承的路径。
68. `availability-single/v1` 已证明“一个故障域、1..N副本、明确required/min-ready/max-unavailable/中断语义且无HA声明”；`availability-ha/v1` 独立证明跨域static N+1与剩余容量。PERF-INF公式的records/s、bytes/s、headroom、unavailable/replay/buffer边界均按exact profile实测，任何绝对production门槛未PASS时没有waiver可提升为production qualified。
69. `e2e-runner-compose/v1` 已冻结并通过clean-start/health-dependency/deadline/resource/evidence/cleanup故障矩阵；traffic scenario精确绑定唯一mode/backend/fixture/topology/direction/rewrite，runner缺失或失败时为`HOLD/NOT_RUN`而非自动换工具。Compose health、Playwright URL可达和服务Ready均未被当作业务PASS。
70. Triton NONE模式的repository snapshot closure、额外模型/version拒绝、显式instance group和`GetLoadedModel/GetPoolStatus`已通过正负测试；CUDA profile的ORT provider partition/host-side operator placement已冻结、读回并测量，运行期新CPU接管或provider漂移无法冒充合法fallback。
71. `plugin-statistics/v1`作为现有`pure-transform|read-only-tool`的可选输出能力落地且没有新增kind、部署模块、浏览器UI plugin、独立消息代理、插件自有scheduler/queue、第二effect queue或数据库writer；Go唯一拥有freeze/run/idempotency/validation/current-history/auth以及唯一durable run ledger/有界dispatch，插件只能返回不可执行有界Artifact。
72. `PluginStatisticsArtifactV1`的gauge/sum/histogram、delta/cumulative、quality/reset/gap/no-data/missing、dimensions/tables/display union、资源和canonical digest通过Go/Rust/Python/TypeScript/Wasm同一golden；NaN/Inf、old generation、same-key conflict、oversize/cardinality和未知major/kind稳定拒绝，缺数据从未补0。
73. Plugin Statistics页面只经同源Go OpenAPI/SSE使用fixed Vue renderer和内部ECharts dataset；不存在插件route/nav/component/HTML/SVG/CSS/JavaScript/URL、任意ECharts/Vega option/表达式或浏览器直连。XSS/CSP/CSV injection、WCAG、browser性能/soak和scope/cache隔离门禁通过。
74. 真实E2E启动deterministic statistics conformance plugin、适用Host/direct adapter、Go、PostgreSQL和production Web，完成freeze→execute→validate→project→render→revoke/stale；全部统计插件disabled/unavailable时核心检测/effect/P4、原生Rule Effectiveness/Overview统计和非插件页面等价，最大负载不突破核心p99/RSS/连接预算。

## 17. Owner 已确认的架构决策

Owner于2026-08-09确认v1.0默认值，于2026-08-10确认v1.1至v1.10，于2026-08-11确认v1.11 BMv2/P4无状态防火墙、v1.12受控多target/fleet和v1.13 Central GPU方向，并于2026-08-12确认v1.14至v1.17的运行、资格和插件统计边界，以及v1.18全模块3,600秒soak与首次本地签名bootstrap例外。以下决定现已成为v1.18内容基线，不再是开放项：

| ID | 决策事项 | 已确认值 |
|---|---|---|
| `DEC-001` | 目标硬件、包速率、target 数、窗口率、Event 率和端到端 p99 SLO | 先建立现有系统基线，再按目标硬件冻结绝对门槛 |
| `DEC-002` | 在线模型执行 runtime | 首期固定Triton+ONNX Runtime，启动前由管理员显式选择`model-runtime-central-cpu/v1`或`model-runtime-central-cuda/v1`；probe只验证selected=observed，缺失/不符即fail closed。TensorRT只可作为显式、独立资格化的新pool/backend generation条件采用，LibTorch首期拒绝；所有backend/profile均不得自动fallback |
| `DEC-003` | PostgreSQL 托管或自建 | 有条件优先托管 HA；自建必须交付 Patroni/等价 failover、PITR 和演练 |
| `DEC-004` | 首期哪些策略允许自动产生真实 effect | R0 和 Owner 显式启用、已资格化的 R1 固定策略可以自动；R2 必须 proposal + maker-checker；普通 incident effect API 拒绝 R3，例外只有 `DEC-031`/`DEC-032` 的 typed 单 target/fleet baseline-policy activation change API |
| `DEC-005` | 人类身份与职责分离 | 使用上游 OIDC + 版本化 scope mapping；区分 Analyst、Operator、Platform Admin、Auditor，不自建账户/refresh session/通用 RBAC Workflow，Platform Admin 不隐含 Operator |
| `DEC-006` | 旧 Event/Incident 数据是否导入 vNext | 不导入，只归档；确有产品需求再做独立离线 import |
| `DEC-007` | A2A 首期是否启用 streaming/push/outbound peer delegation | 使用非 streaming request/task polling；不启用 push；outbound peer 使用静态 allowlist |
| `DEC-008` | 首期是否引入 S3/MinIO | 不引入；模型放 OCI/受控本地制品，附件规模证明需要后再加 |
| `DEC-009` | Frontend 长期保留 Next.js BFF 还是采用独立 SPA | 采用 TypeScript/Vue 3/Vite 独立 SOC SPA；生产同源交付，OIDC callback/session/CSRF、业务授权、聚合投影和 SSE 由 Go/受信网关承担；不保留 Next.js BFF/SSR runtime |
| `DEC-010` | bounded capture 是否属于首期必交能力 | 保留为首期必交能力，但与自动 effect 分开验收 |
| `DEC-011` | 人工授权与设备下发如何衔接 | Proposal/Decision 与 fleet parent 仅为不可执行事实；Go 重验后只创建单 target 唯一或 bounded 逐 target `effect_intent`，Rust 仍是唯一 P4 writer；首期不启用 break-glass |
| `DEC-012` | 治理资源与时效初始上限 | proposal 32 KiB、说明 2 KiB、单 target/generation 未决 128、分页 50/200、proposal 24 小时、authorization 15 分钟；只能通过版本化 profile 收紧，放宽须提升基线 |
| `DEC-013` | 旧 LangGraph 如何兼容 | 保留冻结输入、确定性上下文、bounded LLM/MCP、evidence grounding、`limited|insufficient_evidence|failed`非执行性降级、trace 与不可执行 Artifact 的可观察行为；不迁移外层 Workflow v2、checkpoint、旧 identity/schema 或节点源码，也不把Analysis降级称为inference fallback |
| `DEC-014` | A2A/MCP 首期 wire profile | A2A 1.0 HTTP+JSON、每请求显式版本、polling/no push/no streaming；MCP 使用私有 `masi-mcp-readonly/v1` restricted 2025-11-25 Streamable HTTP profile，支持 JSON/SSE POST、Go 首期返回 bounded JSON；缺 header 只有能从同一认证 session 唯一识别协商版本时才接受，不提供通用 2025-03-26 fallback |
| `DEC-015` | 是否将通用插件平台纳入首期 | 是；采用受控私有 catalog、Go Plugin Manager 和独立 Rust Plugin Runtime Host，禁止公共 marketplace、未经审核上传、进程内动态代码加载和公网自动发现 |
| `DEC-016` | 首期 plugin kind 与 runtime | 封闭支持 `analysis-agent`、`read-only-tool`、`pure-transform`；分别使用 A2A/受限 MCP、OCI service `grpc-service/v1` 和 `wasm-component/v1`；WASI 0.2 是首期资格 profile，WASI 0.3 虽已稳定但须新矩阵；新增 kind 或副作用必须提升基线 |
| `DEC-017` | 插件供应链与发布授权 | 使用 OCI digest + 标准 Cosign/Sigstore 或组织 PKI + SBOM/provenance + offline bundle + revocation；日常 exact-binding 仍为单一 scoped Admin，但 trust/publisher/自动发布等高杠杆 policy 变更必须双人保护；不恢复 legacy 自研四域签名、独立 signer/key registry 或 signed release report |
| `DEC-018` | 首期契约/运行 profile | OpenAPI 3.1.2、JSON Schema 2020-12、P4Runtime 1.4.1、A2A 1.0、MCP 2025-11-25 restricted、WASI 0.2 Preview 2；统一登记于 `contracts/profiles/v1`，任何新规范版本先通过生成/运行/old-new 矩阵 |
| `DEC-019` | Module Complete 前真实边界测试 | 允许无生产副作用、隔离且标记 `REHEARSAL/NOT QUALIFIED` 的真实 wire/boundary 测试；正式 pairwise PASS 仍只在全模块 Module Complete 后重跑 |
| `DEC-020` | 插件撤销与健康初始 profile | Host reconcile ≤30 秒、可达控制面撤销传播 ≤60 秒、撤销/trust cache 新鲜度 ≤5 分钟；startup ≤120 秒、readiness 5 秒×3、liveness 10 秒×3、10 分钟最多重启 5 次后 quarantine ≥15 分钟；放宽须版本化 profile 与证据 |
| `DEC-021` | 资格证据公共 envelope | fault、benchmark、DB restore 和模型资格分别使用版本化机器可读 schema/profile 与不可变 digest；无原始证据、环境 profile 或把 rehearsal 当 PASS 均为 `HOLD` |
| `DEC-022` | 是否直接复用 1Panel/sub2api 等成熟应用源码 | 优先复用经资格化的成熟独立基础库，应用源码默认只作交互参考；1Panel GPL 源码默认禁止复制，sub2api LGPL 源码条件准入；任何 vendoring/fork 必须经 Owner/许可证审查、逐文件来源/修改登记、NOTICE/SBOM/测试与退出策略 |
| `DEC-023` | Frontend 交互、视觉、性能与兼容如何冻结 | 使用任务导向信息架构、项目自有 design tokens、WCAG 2.2 AA、generated client、server-state/SSE 和危险操作专门流程；bundle/CWV/browser/resource 上限固定在 `web-spa/v1`/`web-browser/v1`/`web-performance/v1` 并作为 Module 门禁 |
| `DEC-024` | 下发规则的“生效性”如何定义和展示 | 拒绝单一生效率；分为 exact readback 安装、direct-counter 数据面命中、独立 packet/action oracle 结果验证三层。只在同点同窗 denominator 可用时显示 match ratio；counter hit 不等于处置成功，zero traffic/no hit/stale/reset/not-measurable 分开 |
| `DEC-025` | 全系统如何复用成熟解决方案 | 通用机制按 ADR-0009 登记为 `ADOPT/CONDITIONAL/REJECT` 并优先复用；核心 NIDS 事实、effect/fence/journal/CAS、P4 sole-writer 和业务 UI 保持自有。明确拒绝第二队列/工作流事实源、第二 P4 controller、Grafana mutation 和第三方应用控制面 |
| `DEC-026` | 在线检测模型如何模块化、扩类与替换 | 保留immutable model bundle+versioned feature/label/output adapter+Go/PostgreSQL exact binding；执行层由现行 Central Inference CPU/CUDA startup-bound profile承接。模型不是插件，新类别/置信度不自动获得effect eligibility，major feature/runtime变化走new pool generation兼容矩阵 |
| `DEC-027` | 首期模型何时加载、如何切换 | v1.7的Edge-local单进程startup-bound原则被`DEC-033`在placement/transport上替代；保留“无runtime load/unload、exact startup binding、per-shard current/previous、drain/fence/CAS、mixed显式和exact rollback”语义，并改由Triton pool generation实现 |
| `DEC-028` | 成熟编排下如何防止模型滚动的兼容、恢复和容量漏洞 | 保留Edge每shard唯一canonical route、incarnation/route epoch、WAL gap/resume、readback/CAS/commit、restart/quarantine和绝对容量门禁；v1.13把endpoint identity改为logical pool/pool generation+worker attempt。Kubernetes仍只作adapter，Service/Pod/PDB/Triton Ready均非current或资格事实 |
| `DEC-029` | BMv2/P4 如何生成攻击流量、回放 PCAP 并形成可信 PASS | 采用 PTF+P4Testgen 的确定性 packet oracle、Mininet/netem 的隔离软件拓扑/扰动；Tcpreplay/tcprewrite/tcpprep 仅作经许可证审查的条件二层回放，真实 TCP/应用会话使用有状态 endpoint；四类 fixture统一进入 `traffic-replay/v1`，但 sender/DUT/counter/outcome/detection 证据分层。BMv2只资格化软件 target；TRex/等价高性能发生器仅在硬件 profile触发后条件采用，所有工具 test-only且不能成为生产服务、第二 P4 writer或 effect path |
| `DEC-030` | 在线推理如何实时取得数据，同时控制性能、稳定性和工程复杂度 | 保留P4 aggregate-first、Edge event-time/final window与input/result WAL→PostgreSQL ACK顺序；v1.10的本机SPSC/不引入Triton结论由`DEC-033`替代。首期改用central batched-unary gRPC，仍不引入Beam/Flink、全量P4Runtime包流、第二采集服务或第二queue |
| `DEC-031` | BMv2/P4 通用防火墙策略如何实现、审批和复用成熟方案 | 首期为 `simple_switch_grpc`/v1model IPv4 无状态软件 profile：临时 response overlay + 长期 baseline 双 bank/selector；Admin maker 创建 immutable revision，不同 Operator checker step-up 授权 R3 activation，仍复用逐 target effect intent/Edge writer/readback/CAS。UFW/nftables/iptables 只可独立保护 Linux 主机，不作为 BMv2 后端/fallback；官方 Bloom-filter tutorial 不用于精确阻断，p4c/P4Tools/PTF/P4Testgen 复用作编译/测试，p4-constraints 仅条件作为 preflight defense-in-depth |
| `DEC-032` | P4交换机是否作为受管网络设备、如何支持多交换机且避免第二控制面 | 首期在九模块内实现Go Target Registry/Fleet Coordinator + Rust Edge每target actor，支持bounded多BMv2 target、stable identity/assignment lease/election fence、静态canary/wave与per-target child intents；不承诺跨target原子事务或完整NMS。P4Runtime继续唯一写路径；gNMI仅条件只读，Stratum仅target-side，NetBox仅candidate，Ansible/Nornir仅离线/test，ONOS/厂商controller writer拒绝；绝对N-target门槛未冻结/未测时HOLD |
| `DEC-033` | 首期在线推理部署形态与故障退化 | 只实现Central Inference生产架构，Edge无本地inference。Central Inference作为一个模块，由C++ Gateway、digest-pinned Triton、startup-selected ORT CPU或CUDA、只读repository snapshot和对应资源组成；Edge经`inference-central-grpc-batch/v1`发送final feature batch。CPU/CUDA是启动前显式profile而非fallback；全池不可用时仅bounded WAL/backpressure/HOLD/gap，P4职责继续，禁止任何模型/backend/location自动fallback |
| `DEC-034` | Central Inference如何获得性能、HA且不引入第二控制面 | Triton是唯一delay-based dynamic batch/execution scheduler，固定startup-only`model-control-mode=none`；Go/PostgreSQL唯一持有revision/pool/per-shard current，Edge唯一持有canonical route。HA按部署等级声明；声明production HA的same-generation pool必须以同一exact CPU或CUDA profile跨failure domain并具static N+1，允许原identity/input digest有界retry/dedupe。模型或runtime profile切换启动新pool generation并逐sharddrain/fence/CAS。Kubernetes只作基础设施placement，KServe/Ray/TF Serving/MLflow serving control plane拒绝 |
| `DEC-035` | 实现阶段是否必须真实启动服务并执行E2E | 是。Module E2E真实启动被测模块和所选runtime；正式pairwise/full E2E在干净环境真实启动相关MASI服务、BMv2/P4Runtime和PostgreSQL并走公开边界。CPU/CUDA分别出证据；fake/mock只作模块邻居、确定性provider fixture或故障注入，不能替代正式链路、真实内部服务或PASS |
| `DEC-036` | 资格状态、适用性、部署范围与例外如何表达和聚合 | 使用`qualification-evidence/v1`正交的`level/applicability/result/qualification`与精确claim scope；展示摘要不成为wire状态。CPU/CUDA、single/HA、deployment tier、topology和digest分别聚合。`availability-single/v1`是一个failure domain且可有1..N域内副本，不是single replica。性能waiver保留原始非PASS并限制最高level，绝不能把未达到的绝对production性能/容量/HA门槛提升为production qualified |
| `DEC-037` | 首期正式E2E如何可重现编排、流量runner如何选择 | 本地/CI固定`e2e-runner-compose/v1`，Compose health只作依赖就绪，Playwright只作浏览器driver；每个traffic scenario精确绑定一个mode/backend/fixture/topology/direction/rewrite，缺失时HOLD/NOT_RUN，禁止best-available、自动换runner/backend或以URL/Ready代替业务PASS。生产/多主机runner由精确deployment profile独立资格化 |
| `DEC-038` | Triton/ORT启动闭包与模型切换顺序如何消除隐式漂移 | NONE repository snapshot只包含exact binding及声明依赖，显式固定instance_group；CUDA profile内只允许已声明、读回和测量的host-side operator placement。只读控制方法统一为`GetLoadedModel/GetPoolStatus`。逐shard切换必须先Edge route-withdraw/drain/WAL，再PG CAS、exact commit handshake/resume，不能把loaded/Ready或先CAS后撤路由当安全切换 |
| `DEC-039` | 单故障域完整部署与 production qualification 如何避免名称和资格冲突 | `deployment-tier/v1` 使用 `operational-single-domain` 表示完整可运维、接受一个故障域的非生产资格部署；它最多形成 `SYSTEM_E2E` 级证据。只有 `production-ha` 可进入 `level=PRODUCTION` 门禁，并且必须通过 `availability-ha/v1`、生产绝对性能/容量、PostgreSQL HA/PITR 和全部适用生产门禁；tier 名称本身不自动授予资格 |
| `DEC-040` | Runtime Host 与独立 Analysis/service/Agent 的控制和业务路径如何区分 | Go Plugin Manager 统一拥有准入、资格、binding、generation 和撤销；Runtime Host 执行 Wasm 与显式 Host-managed service。独立 Analysis/service/Agent 使用自身 A2A/MCP/gRPC typed adapter 直连，不经 Host 代理；System E2E 仍真实启动并分别验证 Host 与 Analysis，二者都是完整产品必需模块 |
| `DEC-041` | 后续统计插件如何计算并在前端展示且不开放浏览器代码执行 | 不新增plugin kind；增加`plugin-statistics/v1`作为`pure-transform`默认、`read-only-tool`条件支持的输出能力。每个统计项以immutable `PluginStatisticsDefinitionV1`随revision资格化，只引用host-owned projection/field ID或`read-only-tool`已批准external-source capability ID，不接受runtime registration、endpoint/credential、SQL/PromQL/JSONPath/表达式。Go唯一拥有冻结输入、schedule/run/idempotency、Artifact校验、PostgreSQL current/history、read/export授权；Web只用固定route、内置声明式renderer与内部ECharts dataset。拒绝插件自调度、直写DB/Prometheus、浏览器直连、UI代码/路由、HTML/URL及任意ECharts/Vega配置；核心统计和实时/effect链不依赖插件 |
| `DEC-042` | 全模块正式 soak 的统一时长和完成语义 | 精确运行3,600秒，60秒预热不计入；steady、peak、saturation、recovery-or-activation各900秒。正式PASS要求monotonic qualified elapsed不少于3,600秒、阶段完整、无未分类gap/异常且cleanup成功；该PASS只关闭soak gate，不豁免其他资格门禁。短时运行只能是`REHEARSAL/NOT_QUALIFIED` |
| `DEC-043` | 首次可审计基线在尚无受保护远端时如何bootstrap | v1.18首次bootstrap可使用独立Ed25519 SSH签名commit/tag与Cosign签名canonical digest manifest，签名私钥不得入库，公开key、fingerprint、撤销清单和验证证据必须入库；该例外最高只授予`MODULE`，后续发布仍要求受保护远端，不能把本地tag永久解释为production发布保护 |
| `DEC-044` | Module Complete 与资格 HOLD/NOT_RUN 如何避免互相污染 | Module Complete 是独立、机器派生的 operational completion：完整实现与适用模块门禁实际通过、真实候选 binary/OCI 启动通过、open P0=0 且真实启动/测试 blocker=0 时为 `COMPLETE`。dirty tree、受保护基线、生产绝对门槛及按顺序尚未执行的正式 pairwise/system 只限制资格，不阻断完成；原四维证据保持原值，完成不得重命名为 `MODULE PASS`、pairwise/system PASS 或 production qualified。实际测试未运行/失败、证据缺失、真实启动失败或 open P0 仍阻断完成；findings 与证据 digest 必须公开、append-only 并可重派生 |

实现团队不得自行把上述决策扩大为新核心依赖、第二状态机或第二 effect path。任何改变必须提升需求基线并记录理由、影响和迁移方案。

## 18. 术语防漂移

| 术语 | 本文含义 |
|---|---|
| 模块化 | 依赖稳定契约、所有权唯一、可独立测试替换；不等于无限微服务 |
| Edge Runtime | Rust 实现的 P4/telemetry/WAL/effect 边缘运行时，不包含业务策略或 LLM |
| Online Inference | Central Inference模块内的C++ Gateway校验/适配与Triton/启动前显式选择的ORT CPU或CUDA批量模型计算，不包含Event持久化、model current或P4 effect |
| Model Bundle | 不可变、digest-pinned 的模型数据制品，包含模型/scaler/config 及 feature/label/adapter/profile 引用；不包含可执行插件或 mutable alias |
| Model Revision | 一个 exact model bundle 及其 qualification identity；不是 plugin revision，也不能由 tag/path 原地覆盖 |
| Model Binding | Go/PostgreSQL持有的`(scope,inference_shard)`→exact model revision/logical pool/pool+binding current/previous generation控制事实；worker/pool loaded readback是CAS前证据而非current，deployment status不是binding |
| Model Control Incarnation | PostgreSQL 中不可复用的 model-control 恢复 fence；无损 failover 保留，PITR/restore/clone/rewind 在开放 writer/ingest 前轮换。它与 binding generation共同阻止恢复出相同数字generation时的ABA |
| Shard Routing Epoch | Edge对一个inference shard的单调/不可混淆路由fence，绑定唯一logical pool/pool generation与binding generation；同generation worker选择不改变它，只有CAS后的exact commit handshake可恢复canonical route |
| Model Optimization Mode | `session_online_optimization`或`preoptimized_offline`二选一的资格profile；offline制品绑定生成工具、ORT/EP/options/device/CPU feature，缺失/不兼容时不得静默fallback |
| Feature Schema | 在线输入字段顺序、dtype/shape、单位、窗口、缺失/缩放和 producer compatibility 合同 |
| Label Taxonomy | 稳定 label ID、版本、类别模式、unknown/OOD/abstain、score/threshold/calibration 与演进映射；显示名变化不重用 ID |
| Output Adapter | 将 raw model tensor 确定性映射为 canonical prediction/decision/quality 的受测 engine/config；不能注入任意代码 |
| Model Revision Candidate | Offline ML/CI中尚未成为生产desired/current的immutable revision；通过frozen replay/qualification，不对应生产Triton version route或online shadow |
| Model Rollout | Go管理的低频pool-generation+逐shard路由操作；`desired`是目标revision，`loaded`是worker/pool startup readback，`current`是per-shard PostgreSQL CAS，group可为`rolling_mixed`；四者不能共用模糊`status` |
| Startup-bound Model | exact bundle/repository snapshot在Triton instance启动时由immutable pool envelope选择、验证和加载；模型不编译进Gateway binary，production不支持runtime load/unload/polling |
| Central Inference Module | 一个资格主体，物理由无状态C++ MASI Gateway、pinned Triton、启动前显式选择的ORT CPU或CUDA backend、只读model repository snapshot和对应计算资源组成；不是第二model-control plane |
| Logical Inference Pool | Edge canonical route绑定的稳定服务身份及exact pool generation；同generation可包含多个等价worker，Service/Pod/Triton Ready本身不建立该绑定 |
| Worker Runtime ID | 一个Central Inference replica/进程的不可复用执行身份，记录attempt、readback和故障；不是per-shard route或model current事实 |
| Inference Fallback | 因故障自动改变执行位置、runtime profile、backend或model。首期全部禁止；管理员在启动前显式选择central CPU/CUDA、same-generation等价replica retry、显式durable rollout/rollback到exact generation均不是fallback |
| Control Core | Go 模块化单体，拥有核心 PostgreSQL 业务写入和 durable effect orchestration |
| Plugin Platform | 统一 manifest/catalog/capability/qualification/lifecycle 的受控扩展平面；不等于公共市场、任意 Hook 或统一业务协议 |
| Plugin Manager | Go Control 内部控制面，拥有 plugin revision/qualification/activation/binding/revocation 控制事实，不执行第三方代码 |
| Plugin Runtime Host | 独立 Rust 进程，执行已准入 Wasm/显式 Host-managed service plugin 并强制 capability、资源、deadline、fence 和 sandbox；不拥有核心事实，也不代理独立 service/Agent 的业务流量 |
| Plugin Kind | 封闭、版本化的能力类别；每个 kind 有独立输入输出、权限、副作用和资格合同 |
| Plugin Statistics Capability | `plugin-statistics/v1`：现有kind可选的有界统计输出合同，不是新kind。Go拥有输入、run、校验、投影和授权；插件不拥有数据库、调度、前端代码或核心统计 |
| Plugin Statistics Definition | `PluginStatisticsDefinitionV1`：随 immutable plugin revision 签入 manifest 的统计项描述符，绑定 exact identity/digest、host-owned input projection/field IDs，并可为 `read-only-tool` 引用已批准 external-source capability ID；不是 endpoint/credential、SQL/PromQL/表达式、运行时注册或数据访问授权 |
| Statistics Input Bundle | Go按当前scope/data-class从canonical facts/projections冻结的有界、带window/generation/quality/digest输入；不是数据库连接、raw Event dump或插件自取任务 |
| Plugin Statistics Artifact | 经版本化schema表达metric/series/table/quality/provenance/display hint的不可执行派生结果；只有Go校验/CAS后才可成为plugin statistics current，不能修改核心事实 |
| Declarative Plugin Display | 插件只选择宿主封闭显示kind并引用字段；Vue组件、design token、ECharts dataset/option、ARIA、route、授权和action均由宿主拥有，不允许插件HTML/JS/Vega/ECharts option |
| Analysis Plugin | 平台首个官方 Python `analysis-agent` 插件，使用 LangGraph/LLM/MCP/A2A，产物不可执行 |
| MCP | Agent 到工具/资源的只读协议，不用于 Agent 间协作 |
| A2A | 独立 Agent 间的发现、Task、Message 和 Artifact 协议，不替代 MCP 或核心状态机 |
| Effect | 对 P4 的 capture/apply/rollback 等持久化副作用操作 |
| Effect Proposal | 不可变、带 canonical digest 的候选变更事实；不是审批状态机节点、队列或设备命令 |
| Authorization Decision | 对一个 exact proposal digest 的 append-only approve/reject 事实；仍不可直接执行 |
| Analyst | 研判和提出候选的人类角色，无 approve、intent 或 P4 权限 |
| Operator | 仅在明确 target/risk/effect scope 内授权处置的人类角色 |
| Platform Admin | 管理身份映射、policy/profile、P4Info/config、model/plugin trust/catalog/qualification，以及模型 exact-binding rollout/rollback与插件 activation/rollback/revocation 的角色；默认没有日常 effect 授权权，也不能绕过模型/插件自动资格门禁 |
| P4 sole writer | Rust Edge Agent 是唯一长期持有 P4Runtime 写权限的进程 |
| Managed Target | 由Go/PostgreSQL registry持有stable、永不复用`target_id`的受管P4设备；endpoint、P4Runtime `device_id`、hostname/serial是属性，不是identity。首期可像普通设备查看/分配/隔离/审计，但不是完整NMS |
| Target Control Incarnation | target registry/assignment/fleet控制时间线的不可复用恢复fence；无损failover保留，PITR/restore/clone/rewind在开放assignment/effect前轮换，与P4 application generation、election和model-control incarnation不同 |
| Target Assignment Generation | Go为一个target→Edge active assignment维护的单调fence；旧assignment/actor结果不能推进current。它不替代P4Runtime election或application generation |
| TargetActor | Rust Edge进程内只负责一个assigned target的StreamChannel/mastership、P4 I/O、journal、telemetry、rule observation与有界队列的actor；不是独立微服务或第二controller |
| Fleet Operation | 对冻结target set、ordered waves和同一logical change的不可claim聚合事实；实际副作用仍是一组现有per-target `effect_intent`，没有跨target原子commit |
| Static Target Canary | fleet operation的第一个显式小型设备wave；只验证该cohort的per-target结果后决定是否继续，不做weighted traffic split，也不动态添加target |
| Fleet Partial / Reconciling | `partial`表示child已出现applied/failed/blocked/待执行混合且无未知副作用；任一child为effect unknown/reconciling时parent必须`reconciling`。两者都不能显示为全局成功 |
| Read-only gNMI Profile | 条件`target-gnmi-readonly/v1`，只允许已资格化OpenConfig model/path上的Capabilities/Get/Subscribe；不允许Set，不拥有P4 current/effect/readiness事实 |
| Effect Unknown | 已尝试外部设备副作用但真实结果未确认，只能沿原 operation readback/reconcile；不是失败，不得盲目重试，也不用于模型、插件或规则缺数据 |
| Qualification Level | `REHEARSAL|MODULE|PAIRWISE|SYSTEM_E2E|PRODUCTION`，只描述证据门禁层次，不包含PASS/HOLD等结果 |
| Evidence Applicability | `APPLICABLE|NOT_APPLICABLE`；后者只用于未触发的条件能力并必须有稳定理由，不是PASS或跳过必需能力的手段 |
| Evidence Result | `PASS|FAIL|HOLD|NOT_RUN`；`HOLD`表示前置/profile/环境/证据不完整或不兼容，`NOT_RUN`表示没有执行 |
| Qualification | `QUALIFIED|NOT_QUALIFIED`，是对精确claim scope的资格结论；不能从较低level、另一runtime/availability/topology或展示文本推断 |
| Qualification Claim Scope | runtime、availability、deployment tier、topology、release/environment/profile/artifact digest 的不可变组合；所有PASS声明只在该范围有效 |
| Deployment Tier | `development|acceptance|operational-single-domain|production-ha`；与 qualification level 正交，前三项不得产生 `level=PRODUCTION`/production-qualified claim；只有最后一项可在全部生产门禁通过后取得 production qualification，且必须绑定 `availability-ha/v1` |
| Availability Single | `availability-single/v1`：恰好一个failure domain，可有1..N个域内同profile副本；不等于single replica，也不提供failure-domain HA |
| Stale | 某个先前有效的业务前置事实、binding、generation 或 observation 已过期/漂移；属于各业务 namespace，不与 qualification `HOLD` 共用 enum |
| Unavailable / Failed | `unavailable` 表示当前能力不可提供，`failed` 表示某个领域操作/生命周期已确定失败；必须由消息类型/`status_namespace` 区分，不能与 `HOLD/stale/unknown` 混装 |
| 去签名 | 删除 legacy 自研四域发布签名/审批链；不免除可执行插件使用标准 OCI publisher identity、SBOM/provenance 和准入验证 |
| 生产级 PostgreSQL | 包含 HA/PITR/恢复演练、迁移、连接预算、监控、分区、retention 和 TLS，不只是使用 PostgreSQL 镜像 |
| Module E2E | 使用实际binary/OCI与runtime真实启动被测模块并通过公开边界运行的黑盒组件验收；外部邻居可为contract fake，但不等于对应pairwise或System E2E |
| System E2E | 在干净环境真实启动相关MASI服务并通过跨模块、跨主机、真实PostgreSQL/P4公开边界执行的完整检测、处置和Agent旁路验收；fake/mock、readiness、microbenchmark或rehearsal不得替代 |
| Boundary Rehearsal | Module Complete 前用于发现 wire/TLS/UDS/framing/codegen 问题的隔离真实边界演练；字段为`level=REHEARSAL, qualification=NOT_QUALIFIED`，展示摘要可为`REHEARSAL/NOT QUALIFIED`，不是 pairwise PASS |
| E2E Runner | 负责可重复启动、等待、观测、停止和清理测试服务的测试基础设施；首期本地/CI为`e2e-runner-compose/v1`，不拥有业务current、状态机或PASS判定 |
| Qualification Profile | 将上游规范版本、实现/生成工具、能力、资源、兼容矩阵和证据 digest 绑定成可执行门禁的项目 profile；不等于上游“最新稳定版本” |
| Restricted MCP Profile | 私有 `masi-mcp-readonly/v1` endpoint；只支持受 allowlist 约束的只读 tools/resources，并有意不提供通用旧版缺 header fallback |
| SOC SPA | 独立 Vue 3/Vite 静态控制台；只消费 Go 的 canonical API/projection，不承担 BFF、授权、核心状态或副作用 |
| Server State | 来自 Go API/SSE 的 canonical/投影数据；query cache 只是有界、可清除的加速副本，Pinia/localStorage 不是事实源 |
| Clean-room UI Reimplementation | 只借鉴成熟产品的思想和任务模式，依据 MASI-NIDS 契约以自有代码、token、文案、布局和资产实现，不复制其受保护表达或业务控制面 |
| Vendored Source | 被复制进仓库或构建上下文的第三方源文件；必须绑定 exact upstream revision、许可证、修改、NOTICE、测试、更新与退出记录 |
| Rule Installation Evidence | effect 已 applied 且当前 generation/pipeline 的 exact canonical P4 entry readback 匹配；Write ACK、数据库 intent 或 counter 存在均不能单独替代 |
| Rule Dataplane Match | 已资格化且绑定该 entry/action 的 direct counter 在同一有效 epoch/window 内产生正 packet/byte delta；不等于 alert、攻击归因或 action outcome |
| Rule Outcome Evidence | 独立 packet oracle 或 action-specific telemetry 对预期 drop/forward/mirror 的验证；无 oracle 时为 `not_measurable`，不能由 counter 推定 |
| No Hit Observed | 在存在 eligible traffic、计数有效且窗口完整时 rule delta 为 0；与 no traffic、gap、reset、stale、not measurable 不同，也不自动表示 dead/ineffective |
| Match Ratio | 同 target/table/stage/generation/window 的 rule packet delta 除以 eligible-ingress packet delta；分母不可用或为 0 时不计算，不是处置成功率 |
| Rule Observation / Rule Effectiveness / 规则表现 | 分别是后端事实/协议名、英文产品能力名和中文展示名；三者映射同一分层能力，不是三套状态或数据源 |
| Stateless Firewall Policy | 在当前 target profile 中可确定编译为有界 match/action entry 的版本化无状态策略；不包含连接跟踪、L7、NAT、主机防火墙或任意 P4 命令 |
| Response Overlay | 由 Incident/effect 链产生的高优先级、exact IPv4 五元组临时 permit/drop entry，具有 durable TTL、独立 quota 与逐 entry readback；不修改 baseline selector |
| Baseline Policy Revision | scoped Platform Admin 创建的 immutable 长期策略、显式 default action 和 canonical digest；只有不同 Operator 对 exact revision 做 R3 change authorization 后才能形成 intent |
| Firewall Policy Bank | P4 中保存完整 baseline compiled plan 的两个隔离 bank 之一；inactive bank 完整写入并 readback 后，才能由单条 selector 切为 active；bank 自身存在不等于 current |
| Policy Selector | 每个 target/profile 唯一选择 active baseline bank/revision 的小型 P4 entry；其 exact readback 与 PostgreSQL CAS 共同建立 current，Write ACK 不能替代 |
| Permit-and-continue | 防火墙 stage 允许 packet 继续进入后续 forwarding pipeline 的动作；它不负责选择 egress、重写 header，也不单独证明 packet 最终转发成功 |
| Mature Component Reuse | 复用已资格化通用机制而不转让 MASI-NIDS 事实、状态机或副作用所有权；每项必须登记采用级别、供应链、边界、故障和退出策略 |
| Traffic Fixture | `contracts/testkit/traffic-replay/v1` 描述的 immutable 测试输入、来源/标签、拓扑/方向、改写、时序/速率/扰动、资源和 oracle；不是任意 shell plan、生产 capture 或业务事实 |
| L2 Byte Replay | 将 capture 中保存的 frame bytes 按声明时序交给接口；不建立 ARP/路由/socket/TCP/application 状态，sender accepted 不证明 DUT 收到或处理 |
| Live Session Traffic | 由受控 client/server 或已资格化 stateful generator 通过真实协议栈产生并记录双方状态的流量；用于 handshake、重传、拥塞控制和应用响应语义 |
| Traffic Evidence Layers | requested/sender attempted-or-accepted/test ingress/DUT ingress-egress/rule counter/action outcome/detection result 的独立观测；任一层不能自动推定下一层 |
| Software Target Qualification | 只对 exact BMv2/Mininet/kernel/CPU/profile 有效的功能、故障或性能结论；不得外推真实 NIC、硬件 ASIC、line rate 或生产容量 |
| Telemetry Source Profile | 对一个具体采集来源冻结observation point、字段/capability、snapshot/sequence、event-time、sampling/coverage、资源/drop/backpressure和质量的机器可读合同；backend名称本身不代表完整性或性能 |
| P4 Aggregate Window | P4数据面在有界target/window结构中维护的metadata/counter/register聚合，并由bank/epoch/freeze/barrier或验证sequence形成可证明快照；不是多个普通Read结果的随意拼接 |
| Best-effort Digest Sample | P4Runtime Digest携带的有界hint/sample；Ack只允许server清理cache，不构成可靠传输、逐包覆盖、window完成或Event durable确认 |
| Final Telemetry Window | `[start,end)`窗口超过已资格化watermark+allowed lateness且quality valid后形成的不可重开canonical inference输入；迟到/缺测形成evidence/gap，不回写旧Event |
| Inference Hotpath Wire | `contracts/inference/v1`定义的batched-unary Protobuf/gRPC、mTLS、tensor布局、request/attempt/pool identity、quota/retry/dedupe、batch/deadline/fence合同；不是逐记录RPC或共享native struct |
| Canonical Event ACK | Go在PostgreSQL中以exact result identity/digest提交或确认原canonical Event后返回的ACK；Gateway/Triton/ORT success、gRPC send/receive和内存投影均不能替代 |

## 19. 参考基线

- `../AGENTS.md`
- `adr/0001-effect-governance-and-p4-dispatch.md`
- `adr/0002-analysis-plugin-legacy-behavior-compatibility.md`
- `adr/0003-controlled-general-plugin-platform.md`
- `adr/0004-p4runtime-fencing-preflight-and-cas.md`
- `adr/0005-contract-runtime-and-protocol-profiles.md`
- `adr/0006-qualification-levels-and-evidence.md`
- `adr/0007-vue-soc-console-and-source-reuse.md`
- `adr/0008-rule-effectiveness-observation.md`
- `adr/0009-mature-component-reuse-boundaries.md`
- `adr/0010-online-model-lifecycle-and-rollout.md`
- `adr/0011-startup-bound-model-selection-and-rolling-restart.md`
- `adr/0012-bmv2-p4-traffic-generation-and-replay.md`
- `adr/0013-online-telemetry-and-inference-hot-path.md`
- `adr/0014-bmv2-stateless-firewall-policy-and-activation.md`
- `adr/0015-multi-target-p4-fleet-and-device-management-boundary.md`
- `adr/0016-central-gpu-inference-pool-and-routing-boundary.md`
- `adr/0017-central-inference-runtime-selection-and-real-e2e.md`
- `adr/0018-plugin-statistics-and-declarative-web-projection.md`
- `architecture/masi-nids-vnext-overall-architecture-2026-08-11.md`
- `research/mature-solutions-review-sources-2026-08-10.md`
- `research/frontend-ops-console-and-source-reuse-assessment-2026-08-10.md`
- `research/rule-effectiveness-and-system-reuse-assessment-2026-08-10.md`
- `research/online-model-modularity-assessment-2026-08-10.md`
- `research/multi-target-p4-fleet-management-assessment-2026-08-11.md`
- `research/central-gpu-inference-architecture-assessment-2026-08-11.md`
- `research/central-inference-cpu-cuda-and-real-e2e-assessment-2026-08-12.md`
- `research/plugin-statistics-and-declarative-web-assessment-2026-08-12.md`
- `../../MASI-NIDS/AEE_cuda/docs/masi-nids-integrated-demo-requirements-2026-07-31.md`
- `../../MASI-NIDS/AEE_cuda/docs/masi-nids-integrated-demo-architecture-design-2026-07-29.md`
- `../../MASI-NIDS/AEE_cuda/docs/runtime-wiring-v3.md`
- `../../MASI-NIDS/AEE_cuda/docs/masi-nids-v3-implementation-verification-2026-07-18.md`
- A2A Protocol 1.0.0：<https://a2a-protocol.org/v1.0.0/specification/>
- MCP 2025-11-25 Streamable HTTP：<https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- HashiCorp go-plugin（成熟本地进程 RPC 模式参考，不作为跨机协议）：<https://github.com/hashicorp/go-plugin/blob/main/README.md>
- Dapr Pluggable Components（独立 gRPC/UDS 组件模式参考）：<https://docs.dapr.io/developing-applications/develop-components/pluggable-components/pluggable-components-overview/>
- OpenTelemetry Collector Builder（组件 manifest、构建期 registry 与严格版本参考）：<https://github.com/open-telemetry/opentelemetry-collector/blob/main/cmd/builder/README.md>
- Wasmtime release policy：<https://docs.wasmtime.dev/stability-release.html>；WASI releases/roadmap：<https://wasi.dev/releases>、<https://wasi.dev/roadmap>；WIT：<https://component-model.bytecodealliance.org/design/wit.html>
- gRPC performance/deadline/retry：<https://grpc.io/docs/guides/performance/>、<https://grpc.io/docs/guides/deadlines/>、<https://grpc.io/docs/guides/retry/>
- OCI Image/Distribution 1.1 artifact/referrers：<https://opencontainers.org/posts/blog/2024-03-13-image-and-distribution-1-1/>
- Sigstore/Cosign verification：<https://docs.sigstore.dev/cosign/verifying/verify/>
- PostgreSQL 版本支持策略：<https://www.postgresql.org/support/versioning/>
- PostgreSQL transaction isolation：<https://www.postgresql.org/docs/current/transaction-iso.html>
- P4Runtime Specification 1.4.1：<https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html>
- OpenConfig gNMI Specification（Capabilities/Get/Set/Subscribe；项目只条件资格化read-only子集）：<https://openconfig.net/docs/gnmi/gnmi-specification/>；gNOI services（首期不启用mutation）：<https://github.com/openconfig/gnoi>
- Stratum（target-side P4Runtime/gNMI实现候选）：<https://github.com/stratum/stratum>；ONOS（完整分布式controller，拒绝作为MASI生产writer）：<https://github.com/opennetworkinglab/onos>
- NetBox（intended-state/DCIM/IPAM候选，只导入candidate）：<https://netbox.readthedocs.io/en/stable/introduction/>、<https://netbox.readthedocs.io/en/stable/integrations/rest-api/>；Ansible Network/Nornir（仅离线provisioning/test/read-only模式）：<https://docs.ansible.com/projects/ansible/latest/network/getting_started/>、<https://github.com/nornir-automation/nornir>
- BMv2 README（reference P4 software switch，面向开发/测试/调试而非生产级硬件）：<https://github.com/p4lang/behavioral-model#readme>；p4c BMv2 backend：<https://p4lang.github.io/p4c/behavioral_model_backend.html>
- P4 tutorials firewall（Bloom filter 碰撞限制，只作教学/行为参考）：<https://github.com/p4lang/tutorials/tree/master/exercises/firewall#readme>；p4-constraints（library 可作 defense-in-depth，CLI 仅测试/实验）：<https://github.com/p4lang/p4-constraints#readme>
- P4Tools/P4Testgen：<https://p4lang.github.io/p4c/p4tools.html>
- IPFIX flow/observation identity：<https://www.rfc-editor.org/rfc/rfc7011.html>、<https://www.rfc-editor.org/rfc/rfc7012.html>、<https://www.rfc-editor.org/rfc/rfc5103.html>
- Linux Packet MMAP/AF_XDP/circular buffers：<https://docs.kernel.org/networking/packet_mmap.html>、<https://docs.kernel.org/networking/af_xdp.html>、<https://docs.kernel.org/core-api/circular-buffers.html>；Suricata AF_XDP：<https://docs.suricata.io/en/latest/capture-hardware/af-xdp.html>
- P4 Portable Switch Architecture 1.2：<https://p4lang.github.io/p4-spec/docs/PSA.pdf>
- Suricata rule profiling：<https://docs.suricata.io/en/latest/performance/rule-profiling.html>；nftables counters：<https://wiki.nftables.org/wiki-nftables/index.php/Counters>
- PTF：<https://github.com/p4lang/ptf>；P4Testgen：<https://github.com/p4lang/p4c/blob/main/backends/p4tools/modules/testgen/README.md>；P4Runtime Shell：<https://github.com/p4lang/p4runtime-shell>
- Tcpreplay replay/timing/tcprewrite：<https://tcpreplay.appneta.com/concepts/replay-model/>、<https://tcpreplay.appneta.com/concepts/timing-and-speed/>、<https://tcpreplay.appneta.com/reference/man/tcprewrite/>
- Mininet overview/license：<https://mininet.org/overview/>、<https://github.com/mininet/mininet/blob/master/LICENSE>；Linux netem/offload：<https://man7.org/linux/man-pages/man8/tc-netem.8.html>、<https://docs.kernel.org/networking/segmentation-offloads.html>
- TRex stateful/stateless traffic generator（硬件 profile 条件候选）：<https://trex-tgn.cisco.com/>、<https://trex-tgn.cisco.com/trex/doc/trex_stateless.pdf>
- NIDS corpus 与数据治理：CIC-IDS2017 <https://www.unb.ca/cic/datasets/ids-2017.html>；UNSW-NB15 <https://research.unsw.edu.au/projects/unsw-nb15-dataset>；CTU datasets <https://www.stratosphereips.org/datasets-overview>；NIST SP 800-188 <https://csrc.nist.gov/pubs/sp/800/188/final>；RFC 6235 <https://datatracker.ietf.org/doc/rfc6235/>
- OpenAPI published versions：<https://spec.openapis.org/oas/>；JSON Schema 2020-12：<https://json-schema.org/specification>
- SLSA v1.2 artifact verification：<https://slsa.dev/spec/v1.2/verifying-artifacts>
- Kubernetes Deployment/Pod lifecycle/hooks/disruptions/probes/GPU/topology：<https://kubernetes.io/docs/concepts/workloads/controllers/deployment/>、<https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/>、<https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/>、<https://kubernetes.io/docs/concepts/workloads/pods/disruptions/>、<https://kubernetes.io/docs/concepts/workloads/pods/probes/>、<https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/>、<https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/>
- Pact contract testing：<https://docs.pact.io/>
- OpenID Connect Core 1.0：<https://openid.net/specs/openid-connect-core-1_0.html>
- NIST SP 800-53 Rev. 5.1，AC-5/AC-6：<https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final>
- Web Authentication Level 3：<https://www.w3.org/TR/webauthn-3/>
- 1Panel frontend/package/license：<https://github.com/1Panel-dev/1Panel/blob/dev-v2/frontend/package.json>、<https://github.com/1Panel-dev/1Panel/blob/dev-v2/LICENSE>
- sub2api frontend/package/license：<https://github.com/Wei-Shaw/sub2api/blob/main/frontend/package.json>、<https://github.com/Wei-Shaw/sub2api/blob/main/LICENSE>
- Vue performance：<https://vuejs.org/guide/best-practices/performance>；Vite build/browser target：<https://vite.dev/guide/build.html>
- WCAG 2.2：<https://www.w3.org/TR/WCAG22/>；Core Web Vitals：<https://web.dev/articles/defining-core-web-vitals-thresholds>；ECharts ARIA：<https://echarts.apache.org/handbook/en/best-practices/aria/>
- Prometheus instrumentation/cardinality：<https://prometheus.io/docs/practices/instrumentation/>；Alertmanager：<https://prometheus.io/docs/alerting/latest/alertmanager/>；Grafana state timeline/data links：<https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/state-timeline/>、<https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/configure-data-links/>；OpenTelemetry Collector：<https://opentelemetry.io/docs/collector/>
- 插件统计与声明式展示语义：OpenTelemetry Metrics Data Model <https://opentelemetry.io/docs/specs/otel/metrics/data-model/>；Prometheus naming/instrumentation <https://prometheus.io/docs/practices/naming/>、<https://prometheus.io/docs/practices/instrumentation/>；Grafana DataFrame/time series <https://grafana.com/developers/dataplane/dataframes>、<https://grafana.com/developers/dataplane/timeseries/>；ECharts dataset/ARIA <https://echarts.apache.org/handbook/en/concepts/dataset/>、<https://echarts.apache.org/handbook/en/best-practices/aria/>
- 插件扩展/安全模式：Backstage extension blueprints <https://backstage.io/docs/frontend-system/architecture/extension-blueprints/>；Dapr pluggable components <https://docs.dapr.io/developing-applications/develop-components/pluggable-components/pluggable-components-overview/>；OpenTelemetry Collector components <https://opentelemetry.io/docs/collector/components/>；Vega interpreter/CSP <https://vega.github.io/vega/usage/interpreter/>；OWASP XSS/CSV Injection <https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html>、<https://owasp.org/www-community/attacks/CSV_Injection>
- PgBouncer feature matrix：<https://www.pgbouncer.org/features.html>；pgBackRest：<https://pgbackrest.org/user-guide.html>；Patroni：<https://patroni.readthedocs.io/en/latest/>
- Buf lint/breaking：<https://buf.build/docs/lint/>、<https://buf.build/docs/breaking/>；Syft：<https://oss.anchore.com/docs/guides/sbom/>；Trivy：<https://trivy.dev/docs/latest/guide/>；Cosign：<https://docs.sigstore.dev/cosign/signing/signing_with_containers/>
- OWASP CSRF/HTML5 storage：<https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html>、<https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html>；MDN CSP：<https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CSP>
- ONNX IR/metadata：<https://onnx.ai/onnx/repo-docs/IR.html>；ONNX Runtime ModelMetadata：<https://onnxruntime.ai/docs/api/c/struct_ort_1_1_model_metadata.html>
- ONNX Runtime C/C++ Session、threading 与 graph optimization：<https://onnxruntime.ai/docs/get-started/with-c.html>、<https://onnxruntime.ai/docs/api/c/struct_ort_1_1_session.html>、<https://onnxruntime.ai/docs/performance/tune-performance/threading.html>、<https://onnxruntime.ai/docs/performance/model-optimizations/graph-optimizations.html>
- ONNX Runtime I/O Binding/device tensor/memory：<https://onnxruntime.ai/docs/performance/tune-performance/iobinding.html>、<https://onnxruntime.ai/docs/performance/device-tensor.html>、<https://onnxruntime.ai/docs/performance/tune-performance/memory.html>
- Triton optimization/dynamic batching与shared-memory：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/optimization.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/batcher.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/protocol/extension_shared_memory.html>；Beam/Flink window/watermark只作窗口语义参考：<https://beam.apache.org/documentation/programming-guide/#windowing>、<https://nightlies.apache.org/flink/flink-docs-release-2.3/docs/dev/datastream/event-time/generating_watermarks/>
- Triton model management与secure deployment：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/deploy.html>；TensorFlow Serving architecture/config（拒绝首期控制面，只作比较）：<https://www.tensorflow.org/tfx/serving/architecture>、<https://www.tensorflow.org/tfx/serving/serving_config>
- MLflow model signatures/registry workflow：<https://mlflow.org/docs/latest/ml/model/signatures/>、<https://mlflow.org/docs/latest/ml/model-registry/workflow>；KServe control plane（拒绝首期第二控制面）：<https://kserve.github.io/website/docs/concepts/architecture/control-plane>；NVIDIA Triton model repository/version：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_repository.html>
- ONNX Runtime CUDA/TensorRT execution provider兼容：<https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html>、<https://onnxruntime.ai/docs/execution-providers/TensorRT-ExecutionProvider.html>
- ONNX Runtime execution providers 与 CPU threading/NUMA：<https://onnxruntime.ai/docs/execution-providers/>、<https://onnxruntime.ai/docs/performance/tune-performance/threading.html>
- Triton CPU/GPU instance group 与无 GPU 构建：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_configuration.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/build.html>
- OpenVINO CPU plugin（未来条件候选，不进入首期默认 runtime）：<https://docs.openvino.ai/nightly/openvino-workflow/running-inference/inference-devices-and-modes/cpu-device.html>
- Docker Compose服务启动顺序/healthcheck与Playwright webServer（仅作真实E2E runner/browser driver模式，不以health或URL可达代替业务PASS）：<https://docs.docker.com/compose/how-tos/startup-order/>、<https://playwright.dev/docs/test-webserver>
- legacy 行为证据（仅作不变量参考，不是源码依赖）：`../../MASI-NIDS/AEE_cuda/docs/p4-durable-outbox-design.md`、`../../MASI-NIDS/AEE_cuda/nids_backend/p4/outbox.py`、`../../MASI-NIDS/AEE_cuda/nids_p4_agent/journal.py`
- legacy Analysis Graph 行为证据（仅作兼容测试参考）：`../../MASI-NIDS/AEE_cuda/nids_backend/agents/analysis_graph/topology.py`、`../../MASI-NIDS/AEE_cuda/nids_backend/agents/analysis_graph/runner.py`、`../../MASI-NIDS/AEE_cuda/nids_backend/agents/analysis_graph/artifact.py`、`../../MASI-NIDS/AEE_cuda/nids_backend/tests/test_agent_analysis_graph.py`

本文 v1.17 内容已由 Owner 确认。后续架构设计、ADR、模块合同、数据库设计和测试矩阵必须逐项引用稳定需求 ID，不得以代码存在、单元测试通过、本地 rehearsal、fake/mock、readiness 或 microbenchmark 代替需求验收；首个受保护提交/tag/digest 形成前，仍不得把内容确认表述为可审计发布基线。
