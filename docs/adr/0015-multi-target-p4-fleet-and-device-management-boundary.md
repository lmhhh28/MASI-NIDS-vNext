# ADR-0015：多 P4 Target、Fleet 编排与设备管理边界

- 状态：Accepted
- 日期：2026-08-11
- 决策者：Owner
- 需求基线：`vNext-requirements-1.17`
- 关联需求：`ARCH-003`、`ARCH-REUSE-001`、`ARCH-FW-001`、`ARCH-TARGET-FLEET-001`、`MOD-EDGE-001`、`MOD-CTRL-001`、`MOD-TARGET-FLEET-001`、`CONTRACT-P4-001`、`CONTRACT-TARGET-001`、`CONTRACT-FLEET-EFFECT-001`、`CONTRACT-PROFILE-001`、`FUNC-GOV-001`、`FUNC-FW-001`、`FUNC-TARGET-FLEET-001`、`WEB-TARGET-FLEET-001`、`DB-TARGET-FLEET-001`、`PERF-TARGET-FLEET-001`、`REL-TARGET-FLEET-001`、`SEC-TARGET-FLEET-001`、`DEP-TARGET-FLEET-001`、`OBS-TARGET-FLEET-001`、`TEST-TARGET-FLEET-001`、`MIG-TARGET-FLEET-001`、`DEC-032`

## 背景

此前需求已经在每条 Event、Effect、RuleObservation 和 P4Runtime 会话中携带 `target_id/device_id/generation`，但没有完整定义 target 从哪里注册、如何绑定 Edge、多个 target 如何隔离、批量策略如何表达部分结果、切换 Edge 时如何保持单一 writer，以及管理员能否把设备当作普通网络设备查看和管理。只把一个 `target_id` 填进消息不等于支持多交换机。

P4Runtime 解决的是 P4 数据面的 runtime control。规范用 `(device_id, role, election_id)` 仲裁 primary，并把端口、traffic management、设备发现和 switch configuration 等范围留给其他接口/平台；一次 `WriteRequest` 的 atomicity 也只由该目标设备按自身能力实现，不能外推为多个设备的分布式事务。[P4Runtime 1.4.1](https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html)

OpenConfig gNMI 提供 `Capabilities/Get/Set/Subscribe`，适合配置/状态模型与遥测；gNOI 则提供系统、证书、OS、诊断等运维 RPC。两者能补足设备管理协议，但若首期直接开放 mutation，会新增第二类设备副作用、授权、回滚和资格面。[gNMI specification](https://openconfig.net/docs/gnmi/gnmi-specification/)、[gNOI](https://github.com/openconfig/gnoi)

成熟平台也不能直接等价于本项目需求：Stratum 可作为暴露 P4Runtime/gNMI 的 target-side switch OS/reference stack；ONOS 是完整的分布式 SDN controller；NetBox 是 intended-state DCIM/IPAM，而不是设备 runtime controller；Ansible/Nornir 是自动化框架。把任一平台直接放进 MASI 生产写路径都会与 Edge、PostgreSQL、effect queue 或 authorization 形成双所有权。[Stratum](https://github.com/stratum/stratum)、[ONOS](https://github.com/opennetworkinglab/onos)、[NetBox](https://github.com/netbox-community/netbox)、[Ansible Network](https://docs.ansible.com/projects/ansible/latest/network/getting_started/)、[Nornir](https://github.com/nornir-automation/nornir)

## 决策

### 1. 首期能力是受控 P4 Fleet，不是完整 NMS

首期支持将多个 P4 target 注册、验证、分配、隔离、查看、drain、disable、quarantine、retire，并对冻结 target set 执行临时 response 或长期 baseline policy。前述 lifecycle 操作只改变 Go/PostgreSQL 的 canonical lifecycle/assignment/readiness 与 Edge session 生命周期，不等于修改设备端口、OS、证书或任意配置。管理员能够像管理普通设备资产一样查看身份、位置、期望/观测 profile、Edge assignment、mastership、P4Info、generation、规则/操作和健康新鲜度。

首期不提供端口/VLAN/QoS、路由协议、设备 OS、证书、reboot、自动拓扑发现或任意 CLI/SSH console。它不声称是通用 NMS，也不把 BMv2 的软件能力外推为真实硬件兼容。

### 2. 保持九模块，所有权嵌入现有 Go 与 Edge

不新增第十个在线服务：

- Go Control 内的 `Target Registry` 是 target identity、lifecycle、desired profile、scope、assignment 和 capability observation 的唯一业务写入者；
- Go Control 内的 `Fleet Coordinator` 是冻结 target set、ordered waves、parent/child mapping、gate 和 aggregate projection 的唯一业务写入者；
- PostgreSQL `target_fleet` 是上述事实的唯一持久来源；
- Rust Edge 的 `TargetSupervisor` 管理有界 `TargetActor`；每个 actor 是一个 target 的唯一 P4Runtime client/session owner；
- Web 只消费 Go 的投影；P4 target、Edge、外部 inventory 和部署工具不写核心数据库；
- fleet parent 不可执行，dispatcher 仍只 claim `effect_intents`。

这让“多设备”成为现有模块的明确职责扩展，不形成第二 inventory service、fleet scheduler、P4 controller 或 effect queue。

### 3. Stable Target Identity 与四类 Fence 分离

Go 为每台受管设备生成永不复用的 `target_id`。display name、endpoint、P4Runtime `device_id`、hostname、serial/chassis 和端口都是属性；rename、换地址或换机框不能通过复用 ID 隐藏历史，retired ID 不再分配。同一 active `(p4_endpoint_ref,device_id,role)` 必须有数据库 partial unique constraint；外部 candidate 保存 source/revision/digest/retrieved-at provenance，删除或漂移不得自动 retire、reassign 或覆盖 canonical target。

以下 fence 分别保存，不能压成一个 `generation`：

1. `target_control_incarnation_id`：target/fleet 控制时间线，PITR/clone/rewind 时轮换；
2. `target_assignment_generation`：一个 target 当前分配给哪个 Edge，并绑定不可复用 lease、Edge 本地 monotonic expiry 与 durable P4 election allocation floor/range；
3. Edge/TargetActor runtime epoch：哪个实际进程/actor 返回 observation/result；
4. P4Runtime `device_id/role/election_id` 与项目 application generation、pipeline/P4Info digest：设备会话、primary 和 P4 entity 身份。

无损 PostgreSQL failover保留target-control incarnation；PITR/restore/clone/rewind必须先轮换，再逐target read-only reconcile。P4Runtime election仍由Edge按ADR-0004持久化、验证和在接管时受控reseed；新 assignment 的 election floor 必须严格大于旧 assignment 可能合法发出的值，旧 actor 不得越过其授权 range。Go assignment不伪装成election，连接成功也不等于primary或可写ready。

### 4. Edge 使用每 Target Actor 和分层公平调度

一个 Edge 进程可以管理 profile 上限内的多个 target：

```text
TargetSupervisor
├─ TargetActor A
│  ├─ StreamChannel/mastership/application generation
│  ├─ telemetry/source + effect journal/readback
│  └─ rule observation + bounded queues
├─ TargetActor B
└─ TargetActor N
```

每个 actor 独占 endpoint、session、journal namespace、source/observation identity、queue 和 resource counters。Supervisor 只负责 assignment reconcile、生命周期、全局预算和公平调度；不共享可写 journal，也不把所有 target 串成一个阻塞循环。

调度优先级固定为：mastership与effect journal/readback > telemetry source continuity > rule observation > 条件只读设备遥测。每层同时有global/per-target/per-wave concurrency、bytes、QPS、deadline和queue上限。一个target慢、掉线、满队列或重连风暴不能饿死其他target。

### 5. Assignment Handoff 不复制 Writer

计划迁移顺序为：

```text
freeze new claims
→ durable revoke/drain old assignment lease
→ old actor draining
→ finish/reconcile journal and release/lose mastership
→ old revoke ACK or bounded lease expiry
→ PostgreSQL CAS new assignment generation + higher election floor/range
→ new actor opens StreamChannel and proves primary
→ read-only pipeline/P4Info/selector/entry reconcile
→ open target mutation readiness
```

assignment lease 由 authenticated Go→Edge control response 交付，绑定 control incarnation、target/assignment/Edge workload、election range 与 profile 时钟偏差/最大 TTL；Edge 以本地 monotonic deadline 检查，不在 packet 热路径或每次 P4 RPC 同步查询数据库。每次 claim/write 前，actor 都必须验证本地 assignment identity/generation 尚 current 且 lease 未过期；range 耗尽、clock/lease无法验证、revoke/expiry 后只能 `HOLD/read-only reconcile`，新 range 只能由 Go/PostgreSQL CAS 授予且永不复用。旧 Edge 无法访问时，Go 先 durable revoke旧 assignment并为新 generation分配更高 election range；新 actor仍必须取得可证明的P4Runtime primary、提升application generation，并根据Go/PostgreSQL intent/attempt与真实target readback收敛。旧 actor分区恢复后不能刷新lease、生成越界election或发送Write，target arbitration也拒绝其旧primary。旧journal不可访问时禁止blind retry；无法证明副作用结果则保持原child `unknown/reconciling`。

### 6. Fleet Parent 只聚合，Per-target Intent 才执行

一个 fleet change 由以下事实组成：

```text
immutable proposal + exact target-set/wave digest
→ append-only authorization decision
→ non-claimable fleet parent
→ one existing effect_intent per target
→ per-target claim / Edge actor / journal / P4 readback / PG CAS
→ deterministic parent projection from full child vector
```

Decision、parent 和 bounded child intents 在同一短 PostgreSQL 事务形成。future-wave child已经durable，但gate未开启前不能claim；因此恢复仍由唯一`effect_intents`队列驱动，不扫描parent/target mapping充当第二work queue。

每个 child 使用 `(fleet_operation_id,target_id,effect_digest)` 幂等。P4Runtime atomicity只在单target内解释；项目不实现跨target two-phase commit、distributed lock或“全部同时生效”。parent状态为：

- `planned`：尚未尝试外部副作用；
- `running`：当前wave执行中且尚无混合结果；
- `partial`：已出现applied/failed/blocked/待执行混合，但没有未知副作用；
- `reconciling`：至少一个child为`unknown/reconciling`；
- `applied`：全部required child applied；
- `failed`：不再继续且至少一个required child未applied；
- `aborted`：所有child都未`write_started`并已阻止claim。

UI/API始终返回child vector，不以多数成功、平均值或一个绿色badge替代真实状态。

### 7. 只支持静态设备波次，不做加权流量 Canary

首期 canary 是 ordered waves 的第一个显式小设备集合。target set、membership、顺序、parallel limit、deadline 与 failure policy 在授权时冻结。failure policy只有：

- `fail_fast`：阻止未开始child，不撤销已尝试target；
- `continue_isolated`：失败target隔离，允许满足条件的其他target继续；
- `manual_gate`：人类对exact completed-vector digest决定继续/停止。

首期冻结 target set 的每个 child 都是 required。下一 wave 的 gate 只能以 CAS 在以下条件全部满足时开启：当前 wave 完整 child vector 已到 profile 允许的 known terminal set、没有 `unknown/reconciling`、deadline 未过、scope/authorization/target-set digest 未漂移，并且下一 wave 每个 child 的 assignment/application generation/P4Info/capacity/freshness 仍满足。`fail_fast` 仅在当前 wave 全部 applied 时自动继续；`continue_isolated` 可按原授权跳过已知失败 target 继续，但最终 parent 不能成为 applied；`manual_gate` 永不自动继续，Decision 绑定 exact completed-vector digest。deadline 到期只把未尝试 child 置 `blocked`。target membership或generation漂移不能动态替换target；必须阻止对应child或创建新proposal。rollback是新的fleet parent与per-target intents，逐target指向exact previous；不存在全局瞬时回滚。

### 8. 协议与成熟方案采用矩阵

| 能力/候选 | 结论 | 项目边界 |
|---|---|---|
| P4Runtime 1.4.1 | `ADOPT` | 唯一生产P4 table/pipeline runtime协议；Edge每target独占session、arbitration、journal/readback |
| OpenConfig gNMI | `CONDITIONAL read-only` | `target-gnmi-readonly/v1`只允许exact model/path的`Capabilities/Get/Subscribe`；独立只读identity、mTLS和资源上限；首期拒绝`Set` |
| gNOI | `REJECT first-release mutation` | OS、证书、system/reboot/diagnostic mutation需要新owner、治理、回滚和资格基线 |
| Stratum | `CONDITIONAL target-side` | 可作为exact target-side P4Runtime/gNMI实现；不部署Stratum/其他controller替代Edge，不以BMv2/一种硬件支持证明通用兼容 |
| ONOS/厂商controller | `REJECT production writer` | 完整controller会形成第二拓扑/intent/P4 session/write ownership；只可隔离研究，不进入production依赖图或credential |
| NetBox/外部CMDB | `CONDITIONAL candidate inventory` | 只导入有provenance/digest的candidate，Go展示diff且Admin确认后写canonical registry；外部current/delete不自动生效 |
| Ansible Network/Nornir | `CONDITIONAL offline/test` | 仅离线provisioning、测试拓扑或read-only核验；不得进入实时effect、wave gate、canonical current或持有production P4 writer credential |
| P4Runtime Shell | `CONDITIONAL test/read-only diagnosis` | 不常驻、不调度、不写production target |

NetBox官方把自身定位为desired/intended state的source of truth，并明确不应把未经人工验证的live operational state自动导入；这正适合candidate/import边界，而不是runtime current。[NetBox introduction](https://netbox.readthedocs.io/en/stable/introduction/)

### 9. Frontend 交互

`Operations & Audit` 增加：

- `Managed Targets`：identity/assignment、desired/observed P4 profile、actor/mastership、application generation/P4Info、source/effect/readback freshness、drift和审计；
- `Fleet Operations`：冻结target set、compatibility grouping、static canary/waves、failure policy、target×stage矩阵、per-target current/previous/readback/result与rollback availability。

注册遵循candidate→diff→validate→Admin step-up→canonical result；临时 fleet R2 是 Analyst proposer + 不同 Operator checker，长期 fleet baseline R3 是 Platform Admin maker + 不同 Operator checker。页面不提供raw TableEntry、P4 source、gNMI Set、SSH/CLI或第三方controller嵌入。

### 9.1 Endpoint、Identity 与 Credential 安全

P4Runtime/gNMI endpoint 只能来自 canonical registry。注册验证和每次连接都必须按 exact profile 校验 scheme、解析后的 host/IP/CIDR/port、management-network allowlist、CA/SAN/hostname/workload identity，并拒绝 loopback、link-local、metadata/public/unapproved range、DNS rebinding、redirect、proxy 和明文 fallback。credential 只保存 secret reference，按 target/role/protocol 分离；外部 inventory 不能提供或替换 credential、自动 active/assignment 字段、raw command/template 或 P4 entity。条件 gNMI 使用与 P4Runtime 分离的只读 identity，部署/server/client 三层拒绝 `Set` 与未知 path。

### 10. 性能与资格

`p4-target-fleet/v1`必须冻结`max_targets_per_edge/control/fleet_operation/wave`、parallel child、每target/global P4 RPC、StreamChannel message、FD/task/thread、journal/WAL/disk、queue/memory、DB rows/WAL/connections、API/SSE/UI预算。

必须实测0/1/2/N target、每target最大rule/telemetry/effect/observation叠加、一个slow/unreachable/restarting target、最大parent/child transaction、partial/reconcile/rollback和24小时soak。单targetbenchmark不能线性外推；N或绝对门槛未冻结时为`HOLD/NOT RUN`。

## 取舍

收益：

- 真正补齐多交换机的identity、assignment、session、故障域、编排与UI，而不是只增加`target_id`字段；
- 保持单一P4 writer与单一effect queue，复用现有journal/readback/CAS；
- 一个target故障可隔离，fleet部分结果可解释、可恢复；
- 未来硬件可通过target/profile适配，而无需改写Go业务治理；
- 成熟协议/工具可按公开边界复用，不引入完整controller/NMS的持续复杂度。

代价：

- Go/DB/Web增加target registry与fleet projection，Edge增加actor/supervisor和公平调度；
- assignment handoff、PITR incarnation、parent/child/wave与N-target fault/performance矩阵增加工程和测试量；
- 首期只读gNMI与有限设备生命周期不能满足完整网络运维平台需求；
- 跨target不原子，操作者必须处理partial/mixed和逐targetrollback。

## 被拒绝的方案

1. **每台交换机部署一套独立Control/DB**：切碎全局审计、身份和policy，重复事实与运维成本。
2. **一个全局P4 session/串行队列管理所有target**：单target慢或断线会head-of-line blocking，故障域和资源无法隔离。
3. **新增Fleet Jobs queue**：与`effect_intents`形成第二可执行事实和恢复路径。
4. **把fleet parent直接下发给Edge**：Edge必须隐式拆target、重试和聚合，绕过Go/PG child CAS并扩大副作用所有权。
5. **声称跨target原子更新**：P4Runtime没有跨设备transaction；自建2PC仍无法回滚已经生效的真实数据面状态。
6. **首期采用ONOS/厂商controller作为writer**：形成第二P4 session、intent与拓扑事实源，破坏Edge journal/readback唯一性。
7. **让NetBox/Ansible inventory自动成为current**：外部desired state缺少MASI generation、P4 readback、effect授权和恢复语义。
8. **一次性开放gNMI Set/gNOI**：新增设备副作用种类、凭据和回滚面，工程复杂度远超首期P4 NIDS目标。
9. **把Stratum视为通用控制器或硬件资格**：Stratum的target-side实现价值不授权第二controller，也不证明所有硬件/P4 architecture兼容。

## 迁移

首个target也必须使用新contract/registry/actor，禁止保留无`target_id`的隐式默认switch。上线顺序为contract/schema → Go Registry/Fleet与Edge Supervisor独立Module Complete → 1-target行为等价 → 2-target故障隔离 → N-target绝对性能 → Web → formal pairwise/system E2E。

旧inventory/controller/Mininet临时名称只作为candidate；不导入旧session/election/current。Edge之间的target迁移使用assignment handoff，不能复制旧journal/election目录后并行启动。

## 回滚

代码回滚只能回到仍能读取`target/v1`、`fleet-operation/v1`和现有parent/child facts的版本。否则所有target保持read-only/HOLD，不丢弃registry/audit、不恢复单target隐式配置或第二writer。

功能回滚可关闭fleet新operation并保留单target操作；已有child继续沿原operation收敛。条件gNMI/NetBox/Ansible/Nornir/Stratum adapter可以完全关闭，canonical registry与P4 effect不变。已applied fleet policy只能以新的逐targetrollback operation改变，不能修改parent历史。

## 验证

- contract/golden：target identity、incarnation/assignment/application generation、parent/child/wave/status/digest/error跨Go/Rust/TypeScript一致；
- 1/2/N target：独立StreamChannel、device/role/election、P4Info、journal/source/observation和port map；
- lifecycle/assignment：duplicate、activate/drain/quarantine/retire、planned/emergency handoff、old actor late result、PITR incarnation；
- handoff fence：assignment lease/revoke/expiry、严格更高 election floor/range、旧/新 actor 分区并发重连、旧 actor 越界 election/Write 全部被拒绝；
- fleet：parent+child原子、gate 的 known-terminal/unknown/deadline/scope/freshness/CAS predicate、三种failure policy、partial/unknown/reconcile、target drift、static canary和逐targetrollback；
- isolation/fairness：slow/unreachable/restart/queue/disk/FD/reconnect storm不拖垮健康target或effect优先级；
- security：wrong endpoint/device/actor、active tuple duplicate、external inventory/credential injection、DNS rebinding/redirect/loopback/link-local/metadata/public-range SSRF、TLS CA/SAN/hostname、gNMI Set、ONOS/second writer与scope负例；
- UI：Managed Targets、target×stage matrix、wave timeline、exact approval/original-operation、SSE gap/a11y/large-fleet performance；
- performance：0/1/2/N、最大rules/operation/wave、peak/saturation/24小时soak和绝对门槛；
- 所有结果按ADR-0006区分rehearsal/module/system/production，当前仓库仍为`HOLD/NOT RUN`。

## 参考

- P4Runtime Specification 1.4.1：<https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html>
- OpenConfig gNMI Specification：<https://openconfig.net/docs/gnmi/gnmi-specification/>
- OpenConfig gNOI：<https://github.com/openconfig/gnoi>
- Stratum：<https://github.com/stratum/stratum>
- ONOS：<https://github.com/opennetworkinglab/onos>
- NetBox：<https://github.com/netbox-community/netbox>；REST API：<https://netbox.readthedocs.io/en/stable/integrations/rest-api/>
- Ansible Network：<https://docs.ansible.com/projects/ansible/latest/network/getting_started/>
- Nornir：<https://github.com/nornir-automation/nornir>
- P4Runtime fence/readback：`0004-p4runtime-fencing-preflight-and-cas.md`
- 成熟组件复用：`0009-mature-component-reuse-boundaries.md`
- BMv2 firewall：`0014-bmv2-stateless-firewall-policy-and-activation.md`
- 专项调研：`../research/multi-target-p4-fleet-management-assessment-2026-08-11.md`
- 唯一需求基线：`../masi-nids-vnext-system-requirements-2026-08-09.md`
