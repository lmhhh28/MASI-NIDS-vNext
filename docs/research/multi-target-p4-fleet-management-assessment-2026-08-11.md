# MASI-NIDS vNext 多 P4 Target 与设备管理成熟方案评估

- 日期：2026-08-11
- 性质：独立技术评估与来源登记，不替代需求基线或 ADR
- 对应需求基线：`vNext-requirements-1.17`
- 结论状态：设计已收敛；实现、1/2/N target E2E、性能和生产资格均为 `HOLD/NOT RUN`

## 1. 独立结论

当前项目应该支持多交换机，但不应该在首期建设一个完整网络管理系统，也不应该直接引入 ONOS、厂商 controller 或第二套自动化平台接管设备。

最合适的边界是：

```text
Go/PostgreSQL
  ├─ canonical Target Registry
  ├─ Edge Assignment
  └─ Fleet parent / waves / per-target effect intents
                 │
                 ▼
Rust Edge TargetSupervisor
  ├─ TargetActor A → P4Runtime → Switch A
  ├─ TargetActor B → P4Runtime → Switch B
  └─ TargetActor N → P4Runtime → Switch N
```

这套方案把设备当作可注册、可分配、可隔离、可审计的正常网络资产管理；同时只实现NIDS真正需要的P4 pipeline、遥测、规则、readback和处置，不承担端口/VLAN/QoS/routing/OS等通用NMS职责。

Fleet change必须拆成per-target真实操作。父操作只表达目标集合、静态wave和总览，不能声称跨交换机原子执行。这样增加的是低频控制面复杂度，不改变packet热路径。

## 2. 上游协议事实

### P4Runtime

P4Runtime 1.4.1为P4设备提供pipeline与entity读写、StreamChannel和controller arbitration。primary按`(device_id, role, election_id)`确定；设备/endpoint发现、端口和traffic management不由协议整体解决。`WriteRequest.atomicity`是目标设备的能力，不是多个设备的分布式事务。[P4Runtime 1.4.1](https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html)

对项目的含义：

- 可以直接复用P4Runtime，不应自造P4写协议；
- 必须自行拥有target registry、endpoint/profile和assignment；
- 每target分别arbitration、journal和readback；
- 不得把一组RPC包装成“fleet atomic”。

### OpenConfig gNMI

gNMI定义`Capabilities/Get/Set/Subscribe`和基于schema path的配置/状态交换，适合设备能力、状态快照与流式遥测。[gNMI specification](https://openconfig.net/docs/gnmi/gnmi-specification/)

对项目的含义：

- 它可以补充P4Runtime不覆盖的设备信息；
- 上游页面/current revision不是项目自动兼容合同，实际proto/model/path/encoding/target实现必须锁进profile；
- 首期只需要`Capabilities/Get/Subscribe`的allowlist只读子集；
- `Set`会新增设备副作用、权限、rollback和事实链，不应与首期P4 effect混在一起。

### gNOI

gNOI仓库包含System、OS、Certificate、Diagnostics等运维服务。[OpenConfig gNOI](https://github.com/openconfig/gnoi)

对项目的含义：它不是“顺手打开”的只读补充，而是设备重启、升级、证书等新的高风险mutation面。首期拒绝；未来需要独立需求、角色、intent/journal/readback/rollback和真实设备故障矩阵。

## 3. 成熟方案逐项评估

| 方案 | 成熟能力 | 独立判断 | MASI-NIDS采用方式 |
|---|---|---|---|
| P4Runtime 1.4.1 | P4 pipeline/entity、StreamChannel、arbitration | 正好覆盖生产P4控制边界 | `ADOPT`，Edge唯一client/writer |
| OpenConfig gNMI | schema化配置/状态与subscription | 有价值，但设备/model兼容面大 | `CONDITIONAL read-only`，首期禁Set |
| OpenConfig gNOI | OS/System/Certificate/Diagnostics RPC | 首期收益小于安全/恢复成本 | `REJECT first-release mutation` |
| Stratum | target-side HAL/switch OS，暴露P4Runtime/gNMI等northbound | 可降低特定target适配成本，但不是小型SDK，也不证明所有硬件兼容 | `CONDITIONAL target-side` |
| ONOS | 分布式SDN controller、全局状态、southbound控制 | 能力成熟但与Go/Edge所有权直接重叠 | `REJECT production writer/controller` |
| NetBox | DCIM/IPAM与intended-state source of truth、REST API | 很适合作为组织资产候选源，不适合作为P4 runtime current | `CONDITIONAL candidate import` |
| Ansible Network | 多厂商网络自动化与模块化任务执行 | 适合安装/离线provisioning；实时effect会绕过journal/CAS | `CONDITIONAL offline/test` |
| Nornir | Python inventory/task runner框架 | 适合研究、测试和受控批处理；引入生产会新增scheduler/runtime | `CONDITIONAL offline/test` |
| P4Runtime Shell | 交互诊断 | 上游仍定位为work in progress，常驻风险高 | test/read-only diagnosis only |

来源：[Stratum](https://github.com/stratum/stratum)、[ONOS](https://github.com/opennetworkinglab/onos)、[NetBox introduction](https://netbox.readthedocs.io/en/stable/introduction/)、[NetBox REST API](https://netbox.readthedocs.io/en/stable/integrations/rest-api/)、[Ansible Network](https://docs.ansible.com/projects/ansible/latest/network/getting_started/)、[Nornir](https://github.com/nornir-automation/nornir)、[P4Runtime Shell](https://github.com/p4lang/p4runtime-shell)。

### 为什么不直接用 ONOS

ONOS解决的正是分布式controller、拓扑、intent和southbound控制问题。若把它放在MASI与P4之间，会出现：

- ONOS与Edge都可能持有P4Runtime session/mastership；
- ONOS intent/state与PostgreSQL effect intent/current重复；
- MASI journal/readback/CAS无法证明ONOS内部重试或批处理；
- 故障、升级、权限和性能资格面显著扩大。

因此拒绝的是“作为MASI生产writer/controller”，不是否认ONOS成熟度。它可以用于隔离研究或比较，不进入生产依赖图。

### Stratum为什么可以是target-side候选

Stratum的价值在设备侧统一接口/HAL，并可暴露P4Runtime/gNMI。若某个BMv2/硬件profile以Stratum作为target server，MASI Edge仍直接拥有唯一P4Runtime session和effect语义，因此不会天然形成第二controller。

但每个exact Stratum release、target implementation、P4 architecture、P4Info、gNMI model/path、counter/atomicity/readback仍须资格化。存在Stratum支持项不等于硬件production PASS。

### NetBox为什么只做candidate

NetBox适合保存组织网络资产的intended state并通过REST API集成。项目可以定期或手工拉取有provenance/digest的候选，展示create/update/conflict/no-op diff，再由scoped Platform Admin确认。

NetBox的删除、标签、状态或同步失败不应自动retire/disable/reassign MASI target；否则外部系统会成为canonical事实和高权限mutation源。

### Ansible/Nornir为什么不进入实时链

两者可以复用来安装软件、生成实验拓扑、准备配置、执行只读检查或离线批量任务。但若用于生产P4 rule rollout，就会产生第二scheduler、重试语义和凭据路径，并绕过`effect_intent → Edge journal → readback → CAS`。

正确边界是：部署前/测试期辅助，或以无production writer credential的只读方式核验；runtime fleet gate和P4 effect仍由Go/Edge负责。

## 4. 推荐的核心数据模型

### Target Registry

最低必要字段：

- stable `target_id`与可变alias；
- site/zone/scope/tenant/target class/vendor/model；
- P4Runtime endpoint reference、`device_id`、role；
- expected P4Runtime/P4Info/pipeline/capability/profile digests；
- credential reference，而非credential本身；
- lifecycle：registered/verified/active/draining/disabled/quarantined/retired；
- target-control incarnation、registry revision、assignment generation、不可复用 assignment lease/monotonic expiry 与 durable election floor/range；
- assigned Edge、actor runtime epoch、last observation/freshness/drift；
- external candidate provenance/digest和audit。

`target_id`不可复用；endpoint/device_id不是身份。active endpoint/device/role组合必须唯一。

### Edge Actor

每target actor拥有：

- StreamChannel、role/election/mastership；
- application generation、pipeline/P4Info identity；
- effect journal/readback/reconcile；
- telemetry source与rule observation；
- 独立queue/resource/reconnect budget。

actor 的 lease/revoke 与 P4Runtime election 是两层 fence：旧 assignment lease 过期后只能只读，新 assignment 使用严格高于旧授权 range 的 election floor取得 primary；这样分区恢复的旧 actor 既不能合法生成更高 election，也不能继续 Write。

Supervisor拥有global资源、公平和assignment reconcile，不拥有第二业务事实。

### Fleet Operation

Parent保存：

- exact proposal/authorization/logical effect digest；
- target-set snapshot/digest；
- ordered static waves与failure policy；
- max parallel/deadline；
- child intent vector和aggregate projection。

每target child仍是`effect_intent`。Parent不能claim，也不能直接发给Edge执行。

## 5. 人工操作与审批

### 设备注册/分配

1. Platform Admin手工输入或选择external candidate；
2. Go展示identity/profile/endpoint/assignment exact diff并检测冲突；
3. Edge以read-only/deny-write mode验证TLS、device/role、P4Runtime能力、P4Info、port map和容量；
4. Platform Admin完成recent phishing-resistant step-up，提交exact observation/diff digest；
5. Go短事务CAS registry/assignment；
6. Edge取得绑定 current assignment lease/有界 election range 的 primary并read-only reconcile后，才开放该target的mutation readiness；range耗尽、lease过期或无法验证时 fail closed。

设备注册不会自动安装规则，Platform Admin也不会因此获得Operator权限。

### 临时多设备响应

Analyst只能提交冻结target set/evidence/TTL/diff proposal。Go按每target重算generation/P4Info/capacity/risk；R2由不同于proposer的scoped Operator批准。授权后产生per-target intents，按静态wave执行。

### 长期Fleet Baseline

Platform Admin制作immutable baseline revision和target set；Go/Edge按target生成current→desired plan/rollback vector；不同稳定身份的scoped Operator完成phishing-resistant step-up并批准exact target-set/wave digest。每target仍独立双bank/readback/CAS。

审批依据是事实向量，不是“这组设备大部分健康”、模型分数或LLM建议。

## 6. 性能影响

### Packet热路径

基本不变。每个packet仍只在对应P4 target执行预编译table/action/counter；target registry、fleet parent、wave和gNMI不进入packet path。

### Edge控制面

开销随target数增加：StreamChannel、actor task、FD、journal、telemetry/readback scheduler与内存。Actor-per-target比“每target一套完整服务”更轻，但不能只凭Tokio轻量就声称规模能力；必须实测0/1/2/N、一个慢target和最大规则/观测叠加。

公平调度是性能正确性要求：慢target不能head-of-line block健康target，gNMI/rule sweep不能饿死effect readback。

### Go/PostgreSQL/Web

Fleet authorization会增加target vector canonicalization、parent+child rows、wave projection和UI矩阵。影响位于低频控制面，可通过bounded target set、短事务、cursor、批量SQL、server aggregation和虚拟化控制。

不应该为此引入Kafka/Redis/Temporal；当前PostgreSQL intent/CAS足够，除非未来有真实容量证据。

## 7. 工程复杂度

总体为“中等偏高控制面增量、低数据面增量”：

| 范围 | 增量 | 主要原因 |
|---|---|---|
| P4 dataplane | 低 | 每target运行相同exact profile，packet pipeline不增加fleet逻辑 |
| Rust Edge | 中高 | supervisor/actor、assignment handoff、per-target journal/queue、公平和N-target故障 |
| Go/DB | 中高 | stable registry、incarnation、parent+child原子、wave gate、partial projection和migration |
| Frontend | 中 | inventory/detail、target×stage矩阵、wave/partial/rollback与大列表性能 |
| 安全/运维 | 中高 | endpoint/credential/scope、second-writer负例、PITR/reassignment与条件协议 |
| optional gNMI | 中 | model/path/target兼容、subscription/backpressure、Set拒绝；因此条件采用 |
| 完整NMS/gNOI mutation | 很高 | 新副作用、设备类型、权限、回滚和硬件矩阵；首期拒绝 |

相比“每target一套Control/DB”或“直接上ONOS再适配MASI”，推荐方案的新增代码更多集中在项目必需的不变量，长期运维组件和事实源更少。

## 8. 稳定性设计

- per-target actor隔离故障与backpressure；
- assignment lease/revoke、actor、application generation、有界 election range、P4Info分别fence；新 assignment election floor严格高于旧range；
- parent不可claim，child沿现有journal/readback/CAS恢复；
- unknown关闭后续wave，不用多数成功掩盖；
- fail-fast只阻止未开始目标；已尝试目标逐一收敛；
- PITR先轮换target-control incarnation，再逐targetread-only reconcile；
- external inventory/gNMI/observability失效不改变canonical current；
- 所有queue、target count、wave、RPC、FD、WAL、DB与UI资源有上限。

## 9. 兼容性设计

- business `target_id`与P4Runtime `device_id`分离，允许设备地址/实现变化；
- target class/profile固定P4 architecture、P4Info、atomicity、counter/readback；BMv2 evidence不提升硬件；
- `target/v1`与`fleet-operation/v1`做old/new Go/Edge/Web/persisted fact矩阵；
- gNMI spec/proto/model/path单独profile，不用上游latest；
- Stratum/厂商设备是target adapter差异，不改变Go policy/effect合同；
- external inventory adapter可完全移除；canonical registry保持完整。

## 10. 建议实施顺序

1. 冻结`target/v1`、`fleet-operation/v1`、状态/error/golden和`p4-target-fleet/v1`资源字段；
2. 把当前“默认单switch”也改成registry+一个TargetActor，先证明1-target行为等价；
3. 实现2-target BMv2 identity/session/journal/telemetry/effect隔离与故障；
4. 实现N-target公平、绝对容量和24小时soak；
5. 实现Go parent+child/waves和PostgreSQL migration/fault；
6. 实现Managed Targets/Fleet Operations交互；
7. 全模块Module Complete后重跑formal pairwise/system E2E；
8. 只有真实硬件需要且P4Runtime不足时，再条件资格化gNMI只读/Stratum；
9. 端口/VLAN/QoS/gNOI/NMS需求若真实出现，另立基线，不在本实现预埋万能hook。

## 11. 不能声称的内容

- 文档存在不表示系统已实现多交换机；
- 两个BMv2能连通不表示N-target性能或故障隔离PASS；
- P4Runtime batch成功不表示跨设备原子；
- target healthy/gNMI readable不表示P4 rule current；
- majority applied不表示fleet applied；
- NetBox/Ansible/ONOS/Stratum成熟不授权其成为MASI事实源或writer；
- Stratum/BMv2 PASS不表示任意硬件兼容；
- gNMI支持Set不表示项目已准入设备mutation；
- single-target benchmark不能线性外推N target；
- 绝对target/资源/SLO未冻结时只能`HOLD/NOT RUN`。

## 12. 需求映射

本评估只提供设计依据；规范语义仍以需求基线和 ADR-0015 为准。

| 需求/决策 | 本文落点 |
|---|---|
| `ARCH-TARGET-FLEET-001` | §1、§4、§8 |
| `MOD-TARGET-FLEET-001` | §1、§4 |
| `CONTRACT-TARGET-001` | §4 Target Registry/Edge Actor、§9 |
| `CONTRACT-FLEET-EFFECT-001` | §4 Fleet Operation、§8 |
| `FUNC-TARGET-FLEET-001` | §5、§10 |
| `WEB-TARGET-FLEET-001` | §5、§6、§10 |
| `DB-TARGET-FLEET-001` | §4、§8 |
| `PERF-TARGET-FLEET-001` | §6、§7、§10 |
| `REL-TARGET-FLEET-001` | §8、§10 |
| `SEC-TARGET-FLEET-001` | §3、§5、§8 |
| `DEP-TARGET-FLEET-001` | §3、§10 |
| `OBS-TARGET-FLEET-001` | §4、§8 |
| `TEST-TARGET-FLEET-001` | §6、§8、§10、§11 |
| `MIG-TARGET-FLEET-001` | §9、§10 |
| `DEC-032` | §1、§3、§10、§11 |

## 13. 相关文档

- 决策：`../adr/0015-multi-target-p4-fleet-and-device-management-boundary.md`
- 成熟组件总边界：`../adr/0009-mature-component-reuse-boundaries.md`
- P4Runtime fence：`../adr/0004-p4runtime-fencing-preflight-and-cas.md`
- BMv2 firewall：`../adr/0014-bmv2-stateless-firewall-policy-and-activation.md`
- 唯一需求基线：`../masi-nids-vnext-system-requirements-2026-08-09.md`
