# ADR-0004：P4Runtime 会话 Fence、外部 Preflight 与 PostgreSQL CAS

- 状态：Accepted
- 日期：2026-08-10
- 决策者：Owner
- 需求基线：`vNext-requirements-1.17`（原始决策形成于 v1.6）
- 关联需求：`ARCH-003`、`ARCH-004`、`ARCH-FW-001`、`ARCH-TARGET-FLEET-001`、`CONTRACT-P4-001`、`CONTRACT-P4-FW-001`、`CONTRACT-RULE-001`、`CONTRACT-EFFECT-001`、`CONTRACT-TARGET-001`、`CONTRACT-FLEET-EFFECT-001`、`CONTRACT-PROFILE-001`、`FUNC-EFFECT-001`、`FUNC-GOV-001`、`FUNC-FW-001`、`FUNC-RULE-001`、`FUNC-TARGET-FLEET-001`、`DB-GOV-001`、`DB-TARGET-FLEET-001`、`REL-001`、`REL-GOV-001`、`REL-P4-FW-001`、`REL-RULE-001`、`REL-TARGET-FLEET-001`、`MIG-004`、`MIG-005`、`TEST-007`、`TEST-P4-FW-001`、`TEST-RULE-001`、`TEST-TARGET-FLEET-001`

## 背景

P4Runtime 原生 primary 仲裁使用 `(device_id, role, election_id)`，但 MASI-NIDS 还需要表达 target identity/assignment、Edge actor 重启、pipeline 变化、会话重建和证据失效。项目中的 target-control incarnation、assignment generation、actor epoch 与 `application generation` 是跨 Go、Rust、journal、evidence 和 PostgreSQL 的不同应用级 fence，都不是 P4Runtime 字段。

与此同时，人类授权前的 Edge/P4 preflight 必须位于 PostgreSQL 事务外。外部设备观察不可能与数据库共享一个 MVCC snapshot；若实现把“同一 snapshot”理解为跨外部调用持有事务或连接，会扩大锁窗口并重新引入 ghost effect 风险。

## 决策

### 1. 固定 P4Runtime 资格 Profile

首期 `p4runtime/v1` profile 固定 P4Runtime 1.4.1。profile 必须记录 protobuf/P4Info source digest、target implementation/version、支持的 role、atomicity、readback、idle-timeout 和 pipeline RPC capability；`p4-target-fleet/v1` 另固定 per-Edge/Control/operation/wave 设备上限与资源预算。上游更新或“gRPC 可连接”不自动构成兼容。

P4Runtime 规范规定，server 对同一 `(device_id, role)` 记忆已收到的最高 `election_id`；只有仍存活且使用该最高值的controller才是primary，较低值不能夺回mastership，server完整重启才会重置这份协议内记忆。角色/election分配策略位于协议服务端之外，端口、设备发现和通用switch configuration也不属于其完整管理范围。因此 Edge 必须按target持久、单调分配并验证election生命周期，而不能把连接成功当作mastership；Go Target Registry负责endpoint/assignment等业务事实，但不伪装成协议能力。[P4Runtime 1.4.1](https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html)

### 2. Election、Session 与 Application Generation

- 每个 stable `target_id` 由当前 `target_assignment_generation` 指定一个 Edge `TargetActor`；每个 actor 独占 endpoint、StreamChannel、journal namespace 与 actor epoch，并持有不可复用 assignment lease identity/本地 monotonic expiry。每次 claim/write 前都验证 assignment current 且 lease 未过期；revoke/expiry 后只允许 read-only reconcile，旧 assignment/actor 的结果不能覆盖 current。
- 每个 `(target_id, device_id, role)` 的 `election_id` 由唯一 Edge writer 使用 durable 状态单调分配；assignment 同时授予有界 election range，新 assignment 的 durable floor 必须严格大于旧 assignment 可能合法发出的最大值。floor/range、assignment generation与application generation都是MASI应用fence，不是P4Runtime字段。actor 不得生成越界 election；发送 Write 前必须收到匹配 tuple 且 status 成功的 `MasterArbitrationUpdate`。
- 丢失本地 election 状态、收到更高 election、arbitration 失败或无法证明单调性时，Edge 停止 claim/write。它只能通过新 StreamChannel 返回的 `MasterArbitrationUpdate` 观察服务端 arbitration、执行人工/受控 reseed 并持久化新值后恢复，不能猜测或回退数值；不得假设存在额外的 election 查询 RPC。
- application generation 在 target full restart/session reset、pipeline identity 变化、writer cutover 或重新取得 mastership 时创建新值，并在允许采集/写入前 durable。单纯 TCP reconnect 不自动复用旧 generation；只有 target session、pipeline 和 arbitration 全部证明连续时才可继续。
- target-control incarnation、assignment generation、actor epoch、application generation、session/cookie、election 和 pipeline digest 分别携带，禁止把它们折叠成一个字段或把 election ID 当 generation。
- 旧 generation 的 evidence、claim、precondition token、write result 和 late readback 只能用于审计/收敛旧 operation，不能推进新 generation 的事实。

### 3. Pipeline 身份

Edge 只有在成功取得 primary 后，才在首次 mutation-readiness、target reconnect、pipeline 通知或写入前 freshness 到期时调用 `GetForwardingPipelineConfig`，优先取得 P4Info 与 cookie，并按 target profile 验证 device config。非 primary 不得把该 RPC 的 `PERMISSION_DENIED` 降级为可写 readiness。canonical pipeline identity 至少包括：

- stable `target_id`、assignment generation/actor epoch、P4Runtime `device_id`、target implementation/version；
- P4Runtime profile；
- P4Info canonical digest；
- pipeline cookie；
- device config digest（目标能够安全返回时）；
- target capability/atomicity/readback profile digest。

目标不能返回全部 device config 时，不得伪造 digest；profile 必须明确可验证字段和不足时的 `HOLD` 条件。pipeline/P4Info 变化总是使旧 proposal、authorization 和未 claim intent 变为 stale。

### 4. Write、Journal 与 Canonical Readback

- Go 只发送 logical effect 和所需 atomicity；Rust 依据当前 P4Info 构造 P4 entity。ADR-0014 baseline policy 的 inactive-bank plan、selector precondition 和 response overlay 也属于 logical effect 的版本化 target adapter，不允许 Go/Frontend 发送 raw entity。
- Rust 在 Write 前持久化 `write_started` journal，绑定 operation、logical effect digest、generation、election、pipeline identity、具体 canonical entities 和 atomicity。
- 多 entity effect 必须声明 `CONTINUE_ON_ERROR`、`ROLLBACK_ON_ERROR` 或 `DATAPLANE_ATOMIC` 的合格 profile。目标不支持需求 atomicity 时写前拒绝。
- 上述 atomicity 只约束一个 target 的一个 `WriteRequest`；fleet operation 必须拆成每 target child intent，禁止将其解释为跨设备原子提交、全局锁或 2PC。
- 部分成功、timeout 或响应丢失一律按 entity journal/readback 进入 `unknown/reconciling`；不得因部分响应为 OK 把 operation 汇总为 `applied`。
- readback 使用 `contracts/p4/table-entry-canonical/v1`，明确 field presence/default、bitstring、match key、priority、action/parameter、TTL、target-owned counter/meter 字段和 entity 顺序。protobuf byte equality 只能作为传输 golden，不能代替 logical equality。变化中的 target-owned counter 值不进入 entry logical equality digest；它们由独立 `contracts/p4/rule-observation/v1` 绑定同一 canonical entry identity，防止正常命中被误判为配置 drift。

### 4.1 安装 Readback 与规则命中 Read 分离

- `applied` 只由 effect operation 的 exact entry readback/CAS 收敛；统计 counter 的有无或增长不能替代安装 readback。
- rule observation 只在 exact readback 后由 Go 创建并持久化 canonical epoch，绑定 operation、generation、pipeline/P4Info、canonical entry 和 direct-counter capability；Edge 只按该 identity 采样。pipeline/generation/revision 变化由 Edge 上报 epoch-invalidating condition并建立新的本地 cumulative-counter sample baseline；Go 关闭旧 canonical epoch，并在 exact identity/readback 满足后创建新 epoch。
- P4Runtime rule counter Read 应优先包含 exact `DirectCounterEntry`；若使用 `TableEntry` 路径则必须设置 `counter_data` presence。两种路径都使用 bounded exact entities、canonical identity 和 sequence；response 分片、重排或重复不影响 identity。统计 Read 不取得 write/mastership 所有权，也不创建第二 operation journal。
- 观测 scheduler 的 timeout/gap/reset 只使规则统计 stale/invalid，不把已收敛 effect 改成 `unknown`；只有 effect write/readback 本身结果不明确时使用 `unknown/reconciling`。

### 5. 外部 Preflight Token

审批请求可以在短事务前调用只读 Edge preflight。单 target 返回一个 token；fleet 返回冻结 target set 对应的有界 token vector 与整体 digest，任一 target 超限、缺失、重复或 stale 都不能被平均/多数结果掩盖。该 method 必须使用无写权限或由服务端 method-level authorization 强制只读的 identity，且不得 claim Intent、取得 writer reservation、发送 P4 Write、修改/删除 entity、清零 counter 或改变 scheduler/checkpoint；健康/readiness/preflight 都不能成为 mutation。调用必须有不超过 10 秒的 total deadline，并有 per-target 并发/bytes/QPS 上限；返回的 `precondition_token` 最长有效 30 秒，并至少绑定：

- proposal/revision/canonical digest；
- evidence identity/version/digest/freshness；
- target/control incarnation/assignment/actor、device/role/election/application generation/session；
- P4Info/pipeline/target capability digest；
- target-state digest 与 readback time；
- firewall compiled-plan digest、active/inactive bank、selector/current revision 与各 quota/capacity（适用时）；
- capacity version/available units；
- policy/governance/role-mapping version/digest；
- observed/issued/expires time、response schema version 和 trace ID。

token 通过 mTLS authenticated response 和 canonical digest 防替换；它是可重验的版本向量，不是授权、锁、设备 reservation 或 PostgreSQL snapshot。测试必须以 P4/Edge method audit、writer credential absence/authorization 和 before/after target state 共同证明 preflight 零 mutation，不能只检查返回 payload。

### 6. PostgreSQL CAS

Go 使用短 `READ COMMITTED` 事务，禁止在事务或持有连接期间调用 Edge/P4/OIDC/MCP/A2A/HTTP/gRPC：

1. 按稳定顺序锁定/读取 proposal 与必要强类型当前版本列；
2. 验证 token 未过期，且其中所有版本/digest 与当前数据库事实一致；
3. 通过唯一约束确保一个 proposal/revision 只有一个 terminal Decision；
4. 使用带 version/digest predicate 的 CAS 写 append-only Decision；
5. 单 target approve 时在同一事务创建唯一 Intent；fleet approve 时原子创建 Decision、non-claimable parent 和有界逐 target Intent，并冻结 waves/gates；reject/stale/blocked 不创建可 claim Intent；
6. commit 后才允许 dispatcher claim。

CAS 零行、唯一冲突或事实漂移返回 canonical conflict/stale；客户端读取既有结果。数据库错误只允许在原 request deadline 内对整个短事务最多重试 3 次，且每次重新读取事实；precondition token 过期时必须重新执行事务外 preflight。PostgreSQL Read Committed 每条语句可能看到不同已提交状态，因此正确性来自显式版本 predicate、锁顺序和唯一约束，不来自模糊的“同一 snapshot”。[PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html)

### 7. Claim 前重验与 Finalize

dispatcher 仍只 claim `effect_intents`。claim 前再次验证 authorization expiry、target control/assignment/actor、application generation、pipeline/P4Info、evidence、capacity、target-state、fleet wave gate 和 governance cutoff。失效时 CAS 到稳定 blocked/stale，零 Edge RPC；fleet parent 从完整 child vector 派生，不是第二 scheduler。

Edge RPC 不在数据库事务内。Rust journal/write/readback 后，Go 仅以 `(operation_id, claim_generation, attempt, expected_status/version)` CAS finalize。旧 claim 或旧 application generation 的 late result 不能覆盖新事实。

## 取舍

收益：

- 明确区分 P4Runtime mastership 与项目 generation；
- 以版本向量解决外部观察与数据库事实的 TOCTOU，而不跨外部调用持锁；
- pipeline drift、部分成功和响应丢失均能沿同一 operation 收敛；
- readback 与 CAS 可以生成跨语言 golden 和 fault evidence。

代价：

- Edge 需要 durable election/generation/pipeline journal；
- 合同增加 precondition token、canonical P4 entity 和 CAS schema；
- target capability 不足时会更频繁 `HOLD`，但不会静默降级。

## 被拒绝的方案

1. 在审批事务中等待 Edge/P4：持锁时间不可控并可能产生 ghost effect。
2. 把 preflight token 当 reservation：设备状态仍可能变化，必须在 claim 前重验。
3. 仅使用 election ID 作为 generation：无法表达 pipeline/session/证据生命周期。
4. Write 成功响应代替 readback：不能处理响应丢失、部分成功或目标规范化差异。
5. 盲目 retry unknown Write：可能产生重复或覆盖真实设备状态。

## 迁移与回滚

greenfield schema 直接创建 version vector、token、journal 和 CAS 字段，不导入 legacy election/generation。首个 target 也使用 stable identity/assignment/actor 合同。启用真实 write 前先以 read-only profile 验证 arbitration、pipeline identity 和 canonical readback，再资格化单 entity R0/R1、单 target 多 entity/R2，最后资格化 2/N-target parent/child/wave 与 assignment handoff。

回滚只能到仍能读取当前 token/journal/intent/readback facts 且通过 P4 profile 矩阵的版本。否则保持 Edge read-only、effect `HOLD`，不得丢弃新字段或恢复第二 writer。

## 验证

- arbitration success/failure、更高 election、状态丢失、full restart、pipeline/cookie drift；
- 1/2/N target 的独立 actor/session/journal、assignment handoff、旧 actor late result、慢/断连 target 公平隔离；
- 不同 atomicity、部分成功、timeout/response loss、canonical readback 差异；
- preflight 后 evidence/capacity/generation/policy 任一漂移时零 Edge RPC；
- 并发 approve/reject、CAS 冲突、数据库 failover/serialization、token expiry；
- intent durable 前后崩溃、旧 claim/旧 generation late result、unknown reconcile；
- planned/emergency assignment handoff、lease revoke/expiry、严格更高 election range、旧/新 actor 分区并发重连及旧 actor 越界 election/Write 拒绝；
- exact install readback 与 counter observation 分层；counter 变化不改变 canonical entry equality，old epoch/reset/gap/duplicate response 不覆盖 current rule observation；
- baseline inactive-bank partial/write/readback、selector response loss、selector 已切但 PG CAS 未完成和 exact previous rollback 均沿原 operation 收敛；host firewall 或第二 controller 不参与 preflight/write/readback；
- fleet Decision/parent/children 原子形成、future-wave gate、逐 target atomicity/partial/unknown/reconcile/rollback，且 parent 永不可 claim；
- 所有外部等待期间 PostgreSQL active transaction/held connection 为零。

## 参考

- P4Runtime Specification 1.4.1：<https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html>
- PostgreSQL 18 Transaction Isolation：<https://www.postgresql.org/docs/current/transaction-iso.html>
- Effect 治理总决策：`0001-effect-governance-and-p4-dispatch.md`
- BMv2 防火墙双 bank/selector：`0014-bmv2-stateless-firewall-policy-and-activation.md`
- 多 target/fleet 与设备管理边界：`0015-multi-target-p4-fleet-and-device-management-boundary.md`
- 成熟方案来源登记：`../research/mature-solutions-review-sources-2026-08-10.md`
