# ADR-0014：BMv2 无状态防火墙策略、审批与双 Bank 激活

- 状态：Accepted
- 日期：2026-08-11
- 决策者：Owner
- 需求基线：`vNext-requirements-1.18`
- 关联需求：`ARCH-FW-001`、`ARCH-003`、`ARCH-004`、`ARCH-REUSE-001`、`ARCH-TARGET-FLEET-001`、`MOD-SW-FW-001`、`MOD-TARGET-FLEET-001`、`CONTRACT-P4-001`、`CONTRACT-P4-FW-001`、`CONTRACT-RULE-001`、`CONTRACT-TRAFFIC-001`、`CONTRACT-TARGET-001`、`CONTRACT-FLEET-EFFECT-001`、`FUNC-EFFECT-001`、`FUNC-GOV-001`、`FUNC-FW-001`、`FUNC-TARGET-FLEET-001`、`WEB-FW-001`、`WEB-TARGET-FLEET-001`、`DB-FW-001`、`DB-TARGET-FLEET-001`、`PERF-P4-FW-001`、`PERF-TARGET-FLEET-001`、`REL-P4-FW-001`、`REL-TARGET-FLEET-001`、`SEC-P4-FW-001`、`SEC-TARGET-FLEET-001`、`DEP-P4-FW-001`、`OBS-P4-FW-001`、`TEST-P4-FW-001`、`TEST-TARGET-FLEET-001`、`MIG-P4-FW-001`、`ACCEPT-001`、`DEC-031`、`DEC-032`

## 背景

MASI-NIDS-vNext 的首期目标是 BMv2 P4 交换机，而不是 Linux 真实主机。UFW、nftables 和 iptables 管理的是 Linux 内核网络栈；它们可以保护承载 Edge/Control/SOC 的宿主机，但不能代表 BMv2 数据面已经安装、命中或执行了 P4 规则。若把二者混在一起，测试中很容易出现“包其实被宿主机丢掉，却被报告为 P4 防火墙生效”的假结论。nftables 的官方 counter 文档也体现其规则与 Linux packet-processing rule 绑定；本项目只借鉴该语义，不把它当 P4 target。[nftables counters](https://wiki.nftables.org/wiki-nftables/index.php/Counters)

BMv2 官方把自身定位为 P4 软件交换机参考实现，适合开发、测试和调试，并明确不以生产级性能为目标。因此首期只能对 exact `simple_switch_grpc`/v1model 软件环境授予功能、恢复和软件容量资格，不能把结果外推为 ASIC line-rate 或不同 P4 架构兼容。[BMv2 README](https://github.com/p4lang/behavioral-model#readme)

项目还同时存在两类不同变更：Incident 驱动、短时、精确的封禁/放行，以及管理员维护、长期、可能覆盖大量地址段的基线策略。若把两者塞进一个表、一个模糊“规则列表”或一套临时 TTL 语义，会造成容量互相挤占、审批边界不清和整批策略更新时的半配置状态。

## 决策

### 1. 首期目标和明确不做的能力

首期冻结 `p4-stateless-firewall/v1`：

- target：digest-pinned BMv2 `simple_switch_grpc`、v1model、p4c、P4 program、BMv2 JSON、P4Info 和 port map；
- address family：IPv4 enforcement；IPv6 仍可被 telemetry/detection 观察，但 IPv6 effect 返回 `unsupported/HOLD`；
- state：纯无状态；不做 connection tracking、TCP state machine、L7、NAT、动态 routing ownership 或 host firewall 管理；
- action：`permit-and-continue` 与 `drop`。permit 只让 packet 继续进入后续 forwarding pipeline，不选择 egress，也不改写 header；
- rule：IPv4 source/destination prefix、IP protocol、可选 ingress port、L4 source/destination exact-or-wildcard port 和 fragment class；range、VLAN/tunnel-aware、meter/rate limit 只有新 profile 资格化后才增加；
- capacity：初始最多 4,096 normalized rules，但 compiled entry、每 bank、overlay、counter 和总资源分别有硬上限；normalized rule 展开超过资源时在写前拒绝。

这些边界不会阻止未来迁移真实 P4 硬件；它们要求未来 target 用独立 profile 证明 match/action、原子性、counter、容量和 packet outcome，而不是假设 v1model 可直接移植。

### 2. 数据面分成两个策略层

```text
parser / normalized metadata
            │
            ▼
response_overlay（高优先级、exact 五元组、有 TTL）
      ├─ matched permit/drop → 记录 direct counter
      └─ no match
            │
            ▼
policy_selector（选择 baseline bank 0 或 1）
            │
            ▼
baseline_bank_0 或 baseline_bank_1
      ├─ matched permit/drop → 记录 direct counter
      └─ default permit-and-continue/drop
            │
            ▼
existing forwarding / telemetry pipeline
```

`response_overlay` 服务 Incident 处置：高优先级、exact IPv4 五元组、独立 quota、durable TTL，逐 entry 安装/删除/readback。它不修改 baseline selector。

`baseline_policy` 服务长期通用防火墙：每个 immutable revision 是完整策略与显式 default action，编译后只写 inactive bank。每台 target 都有自己的一对 bank、current/previous binding 与唯一 `policy_selector` entry；不同 target 之间不共享 selector 或原子性。

这种布局不是两条副作用链。两个层次都由同一 `effect_intents` 队列、同一 Rust Edge P4Runtime session、同一 journal/readback/CAS 完成；“overlay/baseline”只是 logical effect kind 和 P4 资源隔离。

### 3. 规则语义和编译约束

Go 只接收 `contracts/p4/firewall-policy/v1` 的 normalized business fields。Rust Edge 使用当前 P4Info/profile 将其确定性编译为 concrete entities，并返回 canonical plan digest、entry count、资源消耗、冲突/遮蔽诊断和 unsupported reason。

- 高显式 priority 优先；response overlay 整体 precedence 高于 baseline；
- 可能命中同一 packet、priority 相同但 action/parameter 不同的 entry 必须拒绝；可证明被更高规则完全覆盖的 rule 作为 shadow warning 显示，但不会被静默删除；
- default action 是 revision 的必填字段，进入 canonical digest 和 R3 授权；禁止依赖 P4 源码中的开发默认值；
- L4 port rule 只能匹配 unfragmented/initial fragment。non-initial fragment 没有可用端口，不得把未解析端口伪造为 0；带端口条件和 non-initial fragment 条件的组合在编译前拒绝；
- prefix/protocol/port wildcard 的物理展开必须可预测且有 profile 上限；range 不得通过任意枚举偷偷实现；
- malformed、unsupported EtherType/IPv4 option/fragment 和 parser error 的处理由 target profile 与 packet golden 固定，不允许运行时猜测；
- API、UI、Plugin 和 Analysis 不得提交 raw `TableEntry`、P4 code、action ID/bytes、CLI 或 shell。Edge 编译器是唯一 normalized policy → 当前 P4 entity 的 adapter。

可选 p4-constraints 只能在 exact revision/profile 资格化后作为 CI/preflight 的第二层检查。其结果不能授权、预留容量、下发设备或替代项目 canonical compiler/readback；其官方 README 也将 CLI 定位为测试/实验用途，而 library 仅适合作为 defense-in-depth。[p4-constraints](https://github.com/p4lang/p4-constraints#readme)

### 4. 人工角色和审批依据

临时 response overlay 沿既有 Incident 治理：

1. Analyst 根据 Event、模型结果、packet evidence、资产上下文和当前规则状态提出 proposal；
2. Go 确定性重算 eligibility、risk、scope、TTL、capacity、generation/P4Info 与 exact diff；
3. R1 可由已资格化固定 policy 自动授权；R2 由 Analyst/有权 proposer 提案，再由不同于 proposer 的 scoped Operator checker 授权；
4. 只有有效 Decision 经 claim 前重验后才产生唯一 intent。

长期 baseline policy 使用独立 typed R3 change API：

1. scoped Platform Admin 创建 immutable revision，填写业务理由、冻结的单 target 或 fleet target set、规则/default 与 rollout/rollback window；
2. Go/Edge 为每台 target 生成 normalized diff、compiled plan、conflict/shadow、capacity、assignment/P4Info/generation 和 current→desired 影响；
3. 不同稳定身份的 scoped Operator 使用 phishing-resistant step-up，对 exact revision、default、target-set/wave digest、每 target plan digest、expiry 和 reason 授权；
4. Go 在短事务内 append Decision；单 target 创建唯一 effect intent，fleet 原子创建不可执行 parent 与有界逐 target intents；
5. claim 前任何 policy、scope、capacity、P4Info、generation 或 actor context 漂移都使授权 stale，零 Edge Write。

审批凭的是可复核证据和确定性差异，不是模型置信度、LLM 建议、Analyst 个人判断或“管理员权限很大”。Platform Admin 负责制作和维护策略，Operator 负责独立检查改变是否可以影响真实流量；一个人即使同时拥有两个角色，也不能在同一 R3 operation 兼任 maker/checker。typed fleet baseline activation 是多个既有 target 上的同类 policy effect，受本 ADR 与 ADR-0015 约束；P4Info/pipeline/device config、target lifecycle/assignment 和任意设备命令仍不得包装成 firewall activation，必须走独立 typed target/deployment 变更流程。

### 5. 双 Bank 激活状态机

baseline activation 是现有 effect operation 的显式阶段，不是新增 workflow engine：

```text
prepared
→ inactive_writing
→ inactive_verified
→ selector_switching
→ selector_verified
→ current_committed
→ previous_retained
→ cleanup
```

固定顺序如下：

1. Go 持久化 intent，绑定 exact target、current revision、desired revision、P4Info/application generation、active/inactive bank、selector precondition、compiled plan digest 和 deadline；
2. Edge journal `write_started`，清理/准备 exact inactive bank，按 bounded batch 写完整 plan；
3. Edge 读取 inactive bank，逐 entry 检查 count、match、priority、action、default/counter binding 和 plan digest；
4. 只有完整一致时，Edge 更新单条 selector；target profile 必须证明 packet 不会观察到由两个 bank 拼成的 hybrid policy；
5. Edge exact readback selector 和 active bank identity；
6. Go 以原 operation/precondition 做 PostgreSQL CAS，把 desired 设为 current、旧 current 设为 previous；
7. old bank 在有界 rollback grace 中保留，之后只按 exact previous revision 清理。

P4Runtime 的 Write atomicity、错误和 readback 语义按 ADR-0004 资格化；一次 RPC 成功或 selector Write ACK 都不等于 current。[P4Runtime 1.4.1](https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html)

Fleet activation 不把上述状态机扩大成跨设备事务。Go 按授权时冻结的静态 canary/waves 开启 child gate，每个 target 独立执行完整 inactive-write/readback/selector/readback/CAS。parent 只从 child vector 投影 `planned|running|partial|reconciling|applied|failed|aborted`；任何 child `unknown/reconciling` 都使 parent `reconciling`。`fail_fast` 只阻止未开始 child，不能撤销已写 target；rollback 必须创建新的 fleet parent，并逐 target 激活各自 exact previous。

### 6. 故障、重试和回滚

- inactive bank partial write/readback mismatch：保持旧 selector；沿原 operation 精确清理或覆盖失败 bank；
- selector 响应丢失：不得重发猜测。读取 selector 和两个 bank，证明旧或新状态后收敛；
- selector 已切但 PostgreSQL CAS 未完成：状态为 `unknown/reconciling`，冻结同 target baseline mutation；readback 唯一证明后沿原 CAS finalize；
- Edge/Go/BMv2/PostgreSQL crash、mastership/P4Info/generation 漂移：旧 claim/plan/bank epoch 被 fence，重启先 read-only reconcile；
- rollback：创建新 operation 激活 exact previous revision；短 grace 内可复用仍有效且 profile 允许的原授权，超时后重新走 R3 maker-checker；
- response overlay expiry：PostgreSQL deadline 驱动 durable delete intent、Edge delete/readback/CAS；idle-timeout notification 或本地 timer 只能是提示；
- 无法证明 active selector/bank、数据库与 target 一致：保持已观测的数据面状态并 `HOLD/read-only`，不自动放行、不盲目重装，也不用 host firewall 补偿。

### 7. 规则表现和前端交互

安装、命中、结果继续使用 ADR-0008 的三层语义：

- installation：active selector/bank 或 overlay exact entry readback；
- dataplane match：同 epoch 的 per-entry direct counter；
- outcome：独立 PTF/packet oracle，生产无独立证据时为 `not_measurable`。

Web 增加两个任务区，而不是一个万能“封禁”按钮：

- `Response Rules`：Incident 上下文、Analyst proposal、TTL/risk、Operator 审批、安装/命中/结果；
- `Firewall Policies`：draft revision、current/desired/previous、normalized diff、default、conflict/shadow、compiled entry/capacity、Admin maker/Operator checker、双 bank operation timeline 和 rollback。

UI 必须明确显示 `draft/validated/authorized/activating/current/previous/unknown/reconciling/failed/HOLD`。selector 已切但 CAS 未确认、Write ACK、BMv2 process healthy、counter 为零或 Grafana 无告警均不得显示为“策略已生效”。

### 8. 性能、稳定性与兼容性

高性能来自数据面只执行预编译、有界的 table match/action/direct-counter；LLM、插件、数据库、策略编译、审批和批量 readback 全在包路径之外。双 bank 把大量控制面写入放在 inactive 资源，在线切换只改变一个 selector；这降低半配置风险，但不把 BMv2 软件性能伪装为硬件性能。

稳定性来自 immutable revision、完整 inactive readback、单 selector publication、journal/fence/CAS、previous rollback 和独立 overlay quota。复杂度集中在低频控制面，不引入第二 controller、第二 queue 或第三方 workflow。

兼容性来自 normalized IR 与 target compiler 分离。未来硬件或另一 P4 architecture 实现自己的 compiler/profile，并复用 Go policy/governance/API；它必须以 old/new contract matrix、真实 target packet/counter/capacity/atomicity evidence 获得资格。首期不会为了“通用”预先实现未证实 target 的最低公分母。

绝对门槛必须在 `performance-environment/v1` 与 `p4-target-fleet/v1` 冻结后测试每 target 0/128/1,024/4,096 normalized rules、最坏合法展开、compile、inactive write/readback、selector flip/readback、rollback/reconcile、overlay churn/expiry、packet p50/p95/p99、throughput、CPU/RSS，以及 0/1/2/N target、最大 wave/parallel child、一个慢/断连 target、公平隔离和 3,600 秒 soak。门槛或 N 未冻结时只能 `HOLD/NOT RUN`。

### 9. 成熟方案复用矩阵

| 方案 | 结论 | 复用内容 | 明确边界 |
|---|---|---|---|
| BMv2 `simple_switch_grpc` + p4c backend | `ADOPT for software target` | P4_16/v1model 软件执行、P4Info/JSON、P4Runtime 测试环境 | 非生产级硬件、非 line-rate；exact revision/profile only |
| p4c/P4Tools/P4Testgen + PTF | `ADOPT for build/test` | 编译、P4Testgen 符号路径/输入输出测试生成、具体 target packet oracle | test-only，不是生产 controller/writer |
| p4-constraints | `CONDITIONAL defense-in-depth` | exact profile 下补充 entity constraint validation | 不授权、不下发、不替代 compiler/readback；CLI 不进生产 |
| P4 tutorials firewall | `REJECT as exact enforcement` | 教学与测试思路 | Bloom filter 存在碰撞，官方说明 unwanted flow 可能通过，不能满足 exact policy |
| UFW/nftables/iptables | `REJECT as BMv2 backend` | 只可借鉴 rule/counter/运维语义，或独立保护 Linux host | 不控制 BMv2，不是 fallback/fact/oracle，不进入 MASI effect path |
| P4Runtime Shell/ONOS/其他 controller | `REJECT as production writer` | Shell 仅隔离测试或受控 read-only 诊断 | 不常驻、不调度、不持 writer credential、不形成第二控制面 |
| gNMI/Stratum/NetBox/Ansible/Nornir | `CONDITIONAL per ADR-0015` | 只读设备状态、target-side实现、candidate inventory或离线/test自动化 | 不拥有 firewall current、wave gate、effect queue或生产 P4 write；gNMI `Set`首期拒绝 |

官方 tutorial 明确说明 Bloom filter 的碰撞可能让不需要的 flow 通过，因此不能用于要求 exact installation/readback 的阻断策略。[P4 tutorials firewall](https://github.com/p4lang/tutorials/tree/master/exercises/firewall#readme) P4Tools/P4Testgen 适合生成供具体 target 执行的 P4 路径测试，最终仍由 exact target 上的 PTF/packet oracle 验证。[P4Tools](https://p4lang.github.io/p4c/p4tools.html)

## 取舍

收益：

- 临时处置和长期策略边界清楚、容量隔离；
- 整批策略不会以 partial bank 暴露给数据面；
- 审批者看到 exact diff/default/影响，且职责分离可审计；
- packet 热路径没有 LLM/DB/plugin/RPC，性能开销可预测；
- normalized policy 可复用，target-specific 编译/证据不被错误外推；
- 复用成熟 P4 编译/测试工具而不引入第二 controller。

代价：

- 两个完整 baseline bank 消耗约双份 table/counter 资源；
- Edge 需要 deterministic compiler、bank journal 和 selector reconcile；
- Go/DB/Web 增加 policy revision、typed R3 change 和 operation timeline；
- 首期明确不做 IPv6/stateful/NAT/rate limiting，会返回更多 `unsupported/HOLD`；
- BMv2 软件容量与硬件生产资格必须维护不同证据。

## 被拒绝的方案

1. 用 UFW/nftables/iptables 直接实现 P4 防火墙：执行对象和 packet path 不同，会形成第二事实/副作用路径。
2. 在一个 active table 原地批量改长期规则：失败或重启会暴露半新半旧策略，rollback 难以证明。
3. 为 baseline 新建第二审批系统或 workflow：重复 proposal/decision/intent，增加冲突事实。
4. 让 Frontend/Plugin/LLM 生成 raw P4Runtime command：绕过 typed contract、scope、capacity、compiler 和唯一 writer。
5. 使用 Bloom-filter tutorial 作为精确 deny：碰撞带来 false positive/false allow 语义，无法逐 rule exact readback。
6. 依赖 P4Runtime atomic multi-entry Write 而不做 bank：target capability/错误模型不同，且不能解决版本化 current/rollback；selector publication 更小、更容易验证。
7. 首期实现 connection tracking/stateful firewall：需要双向状态、超时、SYN/fragment/retransmission、资源耗尽和硬件语义矩阵，显著扩大工程与资格面。
8. 把 pipeline/P4Info 更新包装成普通 policy activation：它会改变 parser/table/action/counter identity，必须独立 cutover 和重建 observation epoch。

## 迁移与回滚

greenfield 不导入 legacy ACL runtime/schema/writer、host firewall rule 或旧 approval。legacy exact IPv4 五元组行为只转换为 frozen fixture/golden，并在新 P4 program 上重新验证；legacy 1,024 exact entries/global drop counter 不能获得 vNext 4,096 normalized-rule/per-entry evidence。

上线先冻结 contract/profile/P4Info 和 generated/PTF golden，再分别完成 P4、Edge、Go/DB、Web Module Complete；随后以 deny-write/read-only rehearsal 验证连接和 readback，最后依次在 clean target 完成 1-target、2-target 隔离和 N-target 绝对容量后激活 explicit baseline。旧/新 writer 永不同时拥有同一 target。

应用 rollback 只激活 exact previous policy revision；pipeline rollback 按 ADR-0004 先停止 write、切 generation/P4Info、重编译/readback policy 并重建 observation epoch。无法证明兼容时保持 target `HOLD/read-only`。

## 验证

- schema/canonicalization、priority/default/fragment、unsupported 和 cross-language golden；
- 0/128/1,024/4,096 normalized rule、最坏合法展开、overlay/bank/counter quota；
- generated/PTF matching/non-matching/fragment/overlap/permit/drop/default/forwarding outcome；
- incomplete bank、wrong readback、selector response loss、selector-after-write crash、CAS conflict、restart/mastership/P4Info/generation drift；
- concurrent activate/rollback、old operation/bank epoch、exact previous、overlay TTL/delete/readback；
- fleet parent/Decision/children 原子形成、静态 waves/gate/failure policy、partial/unknown/reconcile、一个 target 失败不污染其他 target、逐 target previous rollback；
- Admin maker/Operator checker、step-up、self-approval/stale/scope/capacity zero-write negative；
- raw TableEntry/P4 code/shell、Plugin/LLM/UI bypass、second writer credential negative；
- host UFW/nftables/iptables/eBPF contamination negative；
- installation/match/outcome 分层、no traffic/reset/gap/not measurable 与 Frontend timeline；
- compile/write/readback/flip/reconcile packet performance、control-plane contention、resource saturation 和 3,600 秒 soak；
- BMv2 software-only 与未来硬件 profile 证据不可互相提升。

当前仓库尚处初始化阶段，上述实现和资格证据不存在；因此 ADR 已接受不代表模块或系统 PASS，状态保持 `HOLD/NOT RUN`。

## 参考

- BMv2 README：<https://github.com/p4lang/behavioral-model#readme>
- p4c BMv2 backend：<https://p4lang.github.io/p4c/behavioral_model_backend.html>
- P4Runtime Specification 1.4.1：<https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html>
- P4 tutorials firewall：<https://github.com/p4lang/tutorials/tree/master/exercises/firewall#readme>
- p4-constraints：<https://github.com/p4lang/p4-constraints#readme>
- P4Tools/P4Testgen：<https://p4lang.github.io/p4c/p4tools.html>
- P4Runtime fence/readback：`0004-p4runtime-fencing-preflight-and-cas.md`
- 规则表现：`0008-rule-effectiveness-observation.md`
- 成熟组件复用：`0009-mature-component-reuse-boundaries.md`
- 多 target/fleet 与设备管理边界：`0015-multi-target-p4-fleet-and-device-management-boundary.md`
- 流量生成与回放：`0012-bmv2-p4-traffic-generation-and-replay.md`
- 需求基线：`../masi-nids-vnext-system-requirements-2026-08-09.md`
