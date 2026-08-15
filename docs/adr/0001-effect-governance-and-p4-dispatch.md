# ADR-0001：Effect 专用治理与 P4 下发边界

- 状态：Accepted
- 日期：2026-08-10
- 决策者：Owner
- 需求基线：`vNext-requirements-1.18`（原始决策形成于 v1.6）
- 关联需求：`ARCH-003`、`ARCH-004`、`ARCH-FW-001`、`ARCH-PLUGIN-001`、`ARCH-TARGET-FLEET-001`、`CONTRACT-P4-001`、`CONTRACT-P4-FW-001`、`CONTRACT-EFFECT-001`、`CONTRACT-TARGET-001`、`CONTRACT-FLEET-EFFECT-001`、`CONTRACT-PROFILE-001`、`FUNC-EFFECT-001`、`FUNC-GOV-001`、`FUNC-FW-001`、`FUNC-TARGET-FLEET-001`、`PLUGIN-PLAT-003`、`PLUGIN-PLAT-005`、`DB-GOV-001`、`DB-TARGET-FLEET-001`、`PERF-GOV-001`、`REL-GOV-001`、`REL-P4-FW-001`、`REL-TARGET-FLEET-001`、`SEC-002`、`SEC-P4-FW-001`、`SEC-PLUGIN-001`、`SEC-TARGET-FLEET-001`、`MIG-005`

## 背景

vNext 需要同时满足两类约束：

1. 真实 P4 effect 必须可审计、可撤销、可恢复，不能仅凭模型分数、Agent 文本或一个无上下文的“同意”按钮执行；
2. 不能把 legacy Workflow v2、ReviewPacket、账户/session 和通用审批状态机搬入 greenfield 核心，也不能增加第二 effect queue 或第二 P4 writer。

旧系统的 durable outbox、写前 journal、真实 readback、generation fence 和审批摘要绑定证明了关键正确性不变量；旧通用 Workflow/Review/Auth 的状态与授权复杂度不构成 vNext 依赖。尤其是“数据库事务内发出 P4 RPC，再在提交前崩溃”的窗口会产生数据库没有 intent、设备已有规则的 ghost effect，因此任何人类审批方案都不能改变既定 durable effect 顺序。

## 决策

### 1. 只增加 Effect 专用治理事实

核心新增：

- `effect_proposals`：不可变候选；
- `effect_decisions`：append-only 授权事实；
- 既有 `effect_intents/effect_attempts/effect_events`：唯一可执行和恢复链。

Proposal/Decision 与 fleet parent 都不是工作队列，dispatcher 只能 claim `effect_intents`。不引入通用 DAG、任意审批节点、旧 ReviewPacket 兼容层、fleet jobs queue 或跨业务审批引擎。

### 2. 身份、职责与审批依据

- Analyst 读取事实并创建 proposal，不能 approve 或 effect；
- Operator 仅在 target/risk/effect scope 内 approve/reject；
- Platform Admin 管理 OIDC mapping、policy/profile、P4Info/config 和插件 trust/catalog/activation，并可创建 immutable baseline firewall revision，但该角色不隐含 Operator；
- Auditor 只读。

R2 proposer 与 approver 必须为不同的稳定 `(iss, sub)` actor。R2 approver 还必须提供 IdP 可验证、最近 5 分钟内完成且满足项目 profile 的 phishing-resistant MFA/step-up 认证上下文（例如 WebAuthn/FIDO2）；缺失、过期、认证类型不满足或无法绑定当前 actor/session 时为 `HOLD`。Decision 必须绑定 exact proposal digest、角色映射版本、认证上下文、reason code 和 expiry。审批依据是当前 Evidence、generation/session、P4Info/schema、exact logical diff、capacity、TTL、rollback 与 unknown 恢复语义；模型 confidence 和 Analysis Artifact 只能作为补充引用。

### 3. 风险等级

| 风险 | 示例 | 首期规则 |
|---|---|---|
| R0 | readback、不改变转发的 bounded capture | 已资格化 policy 自动或 Operator |
| R1 | exact five-tuple、单 target/entry、TTL ≤ 300 秒、可逆 | Owner 启用的已资格化 policy 自动，否则单 Operator |
| R2 | wildcard、冻结且有界的多 target 临时 response、受保护目标/容量风险，或 300 < TTL ≤ 1800 秒 | Analyst/有权 proposer 创建 proposal，再由不同于 proposer 的 scoped Operator checker 授权；多 target 仍拆为逐 target intent |
| R3 | 无 TTL、TTL > 1800 秒、default/control、单 target 或 fleet baseline policy 变更 | 普通 incident effect API 拒绝；ADR-0014/0015 typed baseline activation 由 Admin maker + 不同 Operator checker step-up 后创建不可执行 parent 与逐 target intents。P4Info/pipeline/device lifecycle/assignment 继续进入独立 typed target 变更管理 |

纯 read/readback 不产生治理事实或 Intent；R0 durable 路径只用于 bounded capture 等设备 mutation。R0/R1 的人类命令也必须持久化 canonical Proposal + exact-digest Decision，只有 R1 允许 proposer/approver 为同一 Operator。

缺失、过期、不兼容或无法唯一分类时提升风险；仍不确定时 `HOLD`。首期不提供 break-glass。

### 4. 从批准到 P4 的唯一顺序

```text
immutable proposal
→ transaction-free bounded preflight
→ short PostgreSQL transaction:
   exact digest + current fact + scope + maker-checker + CAS
   append decision + create unique durable intent
   （fleet 时同时创建 non-claimable parent + bounded per-target child intents）
→ claim/fence
→ transaction-free Go→Rust RPC
→ Rust write_started journal/fsync
→ P4Runtime primary write
→ exact readback
→ PostgreSQL CAS finalize
```

任何 approval、UI、Plugin Manager、Plugin Runtime Host 或插件都不能直连 Edge/P4。外部 preflight 是只读、无 mutation 的版本向量读取：不 claim Intent、不取得/预留 writer 权限、不写/删/清 P4 entity/counter，也不持有数据库连接；它产生绑定 proposal/evidence/application generation/P4Info/pipeline/target-state/policy/capacity version 的 `precondition_token`，但不是 PostgreSQL MVCC snapshot、设备 reservation 或授权。单 target 的 Decision 与唯一 approved Intent 原子提交；fleet 的 exact target-set/wave Decision、不可执行 parent 与有界逐 target child intents 在同一短事务提交，future-wave child 只由 gate 控制可 claim 性。claim 前再次验证 expiry、assignment/generation、P4Info、evidence、capacity 和 target state。失效则稳定 blocked/stale 且零 mutation Edge RPC。隔离级别、锁顺序、serialization retry 和 CAS SQL shape 由 ADR-0004 与 `contracts/db/effect-cas/v1` 固定。

### 5. P4Runtime 所有权

Rust Edge Agent 继续作为唯一长期 P4Runtime StreamChannel owner/writer；多 target 时由同一架构中的每 target `TargetActor` 独占各自 session、journal namespace 和故障域。首期资格 profile 固定 P4Runtime 1.4.1。每次写入绑定 `target_id/target_assignment_generation/device_id/role/election_id/application generation/pipeline/P4Info digest`，只有收到成功 arbitration 的当前 primary 可以写；升级、迁移和重连不得产生同一 target 双 primary writer 窗口。Edge 使用 `GetForwardingPipelineConfig` 或目标支持的等价接口验证 pipeline identity，Control 发送逻辑 effect，Rust 按 ADR-0004/0015 构造、journal、写入并以 canonical TableEntry 读回。application generation 是项目 fence，不伪装成 P4Runtime 原生字段；P4Runtime atomicity 只在单 target 内解释。

### 6. 性能和资源

治理属于低频控制面，不进入 telemetry/inference/Event ingest/P4 execution 热路径。同步 proposal/decision API 不等待 OIDC 远程回调、Edge、P4、LLM、MCP 或 A2A；使用一个短数据库事务和 cursor 查询。baseline firewall 的编译/preflight 同样位于事务外，双 bank/selector 是原 effect operation 的阶段，不建立第二 dispatcher/outbox。

初始上限为 proposal 32 KiB、说明 2 KiB、单 target/generation 128 个未决 proposal、分页 50/200、proposal 24 小时、authorization 15 分钟。fleet 还必须冻结每 operation target 数、wave 数/大小、parallel child 和 parent+child transaction bytes/rows。并发 2/8/32/128 的冲突与吞吐及 0/1/2/N target 必须 benchmark；叠加治理负载时核心 p99 回退不得超过 5%，RSS 增幅不得超过 10%。

### 7. 兼容与演进

- Contract 使用明确 major/minor 和 canonical digest golden vectors；
- schema 采用 expand/contract，不长期双写新旧事实；
- role mapping、governance profile、policy 和 P4Info 都版本化并带 digest、compatibility/cutoff/revocation 语义；
- 旧 proposal 不能完整重验时只读保留为 stale；
- legacy Review/Auth/Workflow/approval 不导入；
- 滚动升级必须测试 old/new client-service 与持久化旧事实读取。

## 取舍

收益：

- 在不恢复通用工作流的前提下提供最小权限、职责分离和完整授权证据；
- 保持单一 effect queue、单一 P4 writer 和既有崩溃恢复不变量；
- 治理查询与写入可有界 benchmark，不污染实时检测链；
- 提案、授权与设备结果可以独立表达，避免把 `approved` 误当 `applied`。

代价：

- Go、PostgreSQL、OpenAPI 和 Frontend 增加 proposal/decision 契约、表、页面和测试矩阵；
- R2 增加人类操作时延，OIDC/step-up 不可用时真实处置会 fail closed；
- 版本化 mapping/profile 与滚动升级需要额外运维纪律和审计保留。

## 被拒绝的方案

1. **沿用单一 Operator 直接下发所有规则**：缺少职责分离和 exact proposal 绑定，权限面过大。
2. **迁移 legacy Workflow/Review/Auth**：引入通用状态机、历史 schema 和兼容负担，违反 greenfield 与精简核心目标。
3. **让 Analysis Plugin、通用插件或 Plugin Runtime Host 自动创建或批准 effect**：扩大非确定性、供应链和 tool 攻击面，违反插件旁路不可执行约束。
4. **为审批结果建立第二 dispatcher/outbox**：形成第二执行真相和恢复链。
5. **在审批事务中等待 Edge/P4 或直接写设备**：扩大锁/连接占用，并重新引入 ghost effect 崩溃窗口。
6. **Platform Admin 自动拥有 Operator 权限**：违反最小权限和职责分离。

## 迁移

vNext 以 clean-start schema 创建 proposal/decision 表及版本化 profile/mapping。没有 legacy 数据迁移或运行时双写。上线顺序为 schema expand → 只读 API → proposal/decision 写入但 effect deny-all → 完成契约/故障/性能门禁 → 启用 R0 → 单独资格化 R1 → 启用 R2 maker-checker → 最后按 ADR-0014/0015 单独资格化 typed baseline R3 activation 与 fleet waves。普通 incident R3 与 P4Info/pipeline/device lifecycle/assignment 仍不通过 incident API。

## 回滚

治理版本出现缺陷时，立即切换到版本化 deny profile：暂停 R1/R2 新 intent，保留 R0 read/readback 和核心检测；不得回退为无审批直写。已有 Proposal/Decision/Intent 保持只读和可恢复，未决设备结果继续沿原 journal/readback 收敛。应用只能回滚到通过当前 schema/contract 兼容矩阵的版本；否则保持 effect `HOLD`。

## 验证

必须用黑盒和 fault injection 证明：

- auto R0/R1、单/多 target R2 maker-checker、typed 单/fleet baseline R3 Admin-maker/Operator-checker + 最近 5 分钟 phishing-resistant step-up、普通 R3/HOLD 和任意插件不可执行 E2E；
- self-approval、scope mismatch、step-up 缺失/过旧/错误认证类型/错误 actor、过期、并发 approve/reject、P4Info/generation/evidence/capacity drift 均零 mutation Edge RPC；preflight 本身由只读调用/credential/P4 audit 证明零 claim/write/clear/reservation；
- Decision/Intent 原子性、崩溃恢复、PostgreSQL failover、timeout query、unknown/readback；
- P4 mastership/role/election 与滚动升级无双 writer；
- fleet parent 不可 claim、parent/Decision/child intents 原子形成、future-wave gate、每 target readback/CAS、partial/unknown/reconcile 与逐 target rollback；
- 治理性能预算、连接预算、索引/WAL 增长和热路径隔离。
- 禁用、撤销或攻陷 Plugin Manager/Runtime Host/Analysis Plugin 均不能创建 Proposal/Decision/Intent、取得 P4 credential 或形成第二 effect queue。

## 参考

- 通用插件平台边界：`0003-controlled-general-plugin-platform.md`
- P4Runtime fencing/preflight/CAS 细化：`0004-p4runtime-fencing-preflight-and-cas.md`
- BMv2 baseline/response 防火墙治理与激活：`0014-bmv2-stateless-firewall-policy-and-activation.md`
- 多 target/fleet 所有权与编排：`0015-multi-target-p4-fleet-and-device-management-boundary.md`
- P4Runtime Specification 1.4.1：<https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html>
- PostgreSQL transaction isolation：<https://www.postgresql.org/docs/current/transaction-iso.html>
- OpenID Connect Core 1.0：<https://openid.net/specs/openid-connect-core-1_0.html>
- NIST SP 800-53 Rev. 5.1，AC-5/AC-6：<https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final>
- Web Authentication Level 3：<https://www.w3.org/TR/webauthn-3/>
- legacy durable outbox 证据：`../../../MASI-NIDS/AEE_cuda/docs/p4-durable-outbox-design.md`
