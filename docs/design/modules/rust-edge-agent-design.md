# Rust Edge Agent 模块详细设计

- 模块 ID：`MOD-EDGE-001`
- 目录：`edge-rs/`
- 文档状态：`DRAFT`
- 主要需求：`ARCH-003`、`ARCH-004`、`ARCH-TELEMETRY-001`、`ARCH-TARGET-FLEET-001`、`CONTRACT-P4-001`、`CONTRACT-P4-FW-001`、`CONTRACT-RULE-001`、`CONTRACT-TARGET-001`、`CONTRACT-TELEMETRY-001`、`CONTRACT-INFERENCE-001`、`FUNC-EFFECT-001`、`FUNC-TEL-001`、`REL-TARGET-FLEET-001`、`TEST-003`、`TEST-RULE-001`、`TEST-P4-FW-001`、`TEST-TARGET-FLEET-001`、`TEST-TEL-INF-001`
- 主要 ADR：ADR-0004、ADR-0008、ADR-0013、ADR-0014、ADR-0015、ADR-0017

## 1. 模块目标

Rust Edge 是 P4 target 与中央控制/推理之间的唯一边缘运行时。它在一个进程内管理 1..N 个 target，拥有每个 target 的 P4Runtime session/mastership、telemetry source/window、source/input/result WAL、canonical inference route、effect journal/readback 和 rule counter sweep。

Edge 不拥有业务授权、Event/Incident、target registry、model/plugin current 或核心 PostgreSQL 写入；不运行本地模型，不代理插件/Agent，不成为第二数据库队列。它只依据 Go 提交的 fenced assignment、binding、intent 和 observation identity执行。

## 2. 进程内构件

```text
Edge Process
├─ Control Plane Client
│  ├─ assignment/lease reconcile
│  ├─ effect claim delivery/result
│  └─ Event/rule/health batches + canonical ACK
├─ TargetSupervisor
│  ├─ global budgets/fair scheduler
│  └─ TargetActor[target_id] × N
│     ├─ P4Runtime session/mastership/application generation
│     ├─ target-scoped effect journal/readback
│     ├─ telemetry capture adapter/source epoch
│     ├─ event-time window + source/input/result WAL
│     ├─ central inference client/route/retry/fence
│     └─ rule observation sampler
├─ Normalized Firewall Compiler
├─ WAL/Checkpoint/Compaction Service
└─ Health/metrics/trace/admin read-only surface
```

这些构件是同一模块内部 package/task/actor，不是独立服务。TargetActor 不共享可写 journal、source epoch 或 queue；Supervisor 只协调 assignment 和全局预算。

## 3. TargetSupervisor 与 TargetActor

### 3.1 Assignment

Supervisor 消费 Go 的 exact assignment，绑定 `target_control_incarnation_id`、`target_assignment_generation`、Edge workload identity、不可复用 lease、monotonic expiry 和 P4 election floor/range。lease/revoke/expiry/range exhaustion 后旧 actor 只能 read-only reconcile；新 assignment 必须使用严格更高 election floor。

每个 P4 claim/write 前 actor 本地验证 assignment current、lease有效、election在range内、application generation/P4Info与计划一致。Edge 不为每个 packet/P4 RPC同步查数据库，但不能用本地 cache 延长 lease。

### 3.2 公平调度

每个 actor 拥有独立连接、queue、journal/source namespace 和资源计数。全局优先级固定为：

1. mastership 与 effect journal/readback；
2. telemetry source continuity；
3. rule observation；
4. 条件只读 gNMI。

global/per-target/per-wave 的并发、bytes、QPS、deadline 和 queue 都有上限。一个 target 的重连风暴、慢 Read、磁盘/FD耗尽不得阻塞健康 target。

## 4. P4 会话与 Effect Executor

Edge 是唯一 production P4Runtime client/writer。Effect 执行固定为：

```text
fenced effect intent
→ local precondition/lease/P4Info validation
→ journal write_started durable
→ bounded P4 write
→ exact P4 readback/reconcile
→ structured EffectResult to Go
→ PostgreSQL CAS result observed
→ journal checkpoint/retention
```

网络/P4 等待期间 Go 不持有数据库 transaction；Edge 本地 journal 也不宣称 canonical applied。response loss 后沿原 operation readback，不盲写新请求。

### 4.1 Firewall Compiler

Edge 将 normalized business policy确定性编译为当前 P4Info concrete plan，输出 canonical plan digest、entry/resource count、unsupported/conflict/shadow diagnostics。它是唯一 normalized policy→P4 entity adapter。

Baseline activation 执行 inactive bank prepare/write/readback、selector switch/readback；response overlay逐 entry执行并尊重独立 quota。Edge 不重新判断 human authorization 或创建 intent。

## 5. Telemetry Source 与窗口

### 5.1 默认源

首期默认 adapter 是 `telemetry-p4-window/v1`：actor读取冻结 aggregate bank/snapshot，严格校验 epoch/sequence/P4Info/generation，并在 clear/advance 前持久化 source WAL。Digest/PacketIn 只作补充 hint/sample，记录 loss/coverage；Digest Ack 只在对应 metadata 已写 source WAL 后发送。

条件 mirror adapter仍位于 Edge 模块内：PACKET_MMAP 是首个兼容 profile，AF_XDP/DPDK 只有明确触发和资格证据后增加。一个 deployment 不按运行故障自动切 source backend，也不同时常驻两个 canonical source。

### 5.2 Event-time Window

Window engine维护 source/shard watermark、最大乱序、idle timeout、allowed lateness 和半开区间 `[start,end)`。只有 `final + quality=valid` 的完整窗口进入 inference；gap/drop/snapshot inconsistency/late-after-final 不补零、不重开旧 Event。

Feature adapter按照 exact feature schema产生定长 tensor，绑定 source/window/input digest、model/pool/route/profile identity。raw payload默认不进入 inference/DB/log/metrics/Web。

## 6. 三阶段 WAL 与 Canonical ACK

Edge 在同一所有权域内维护：

1. telemetry/source WAL；
2. final inference-input WAL；
3. validated inference-result WAL。

顺序不变量：source durable → final/input durable → Central result → result durable → Go/PostgreSQL Event commit → canonical ACK → checkpoint/compaction。Central RPC成功、Triton成功、gRPC send、内存 queue释放或 Digest Ack 都不能提前推进 cursor。

WAL 记录有 records/bytes/age/disk/segment/checkpoint 上限。容量不足时背压并形成 exact gap/HOLD，不无限增长；partial tail/corruption只隔离可证明范围，不能跳过未 ACK canonical identity。

## 7. Central Inference Client 与路由

Edge 使用 `inference-central-grpc-batch/v1` 异步 batched-unary mTLS client：

- 每 logical pool/binding generation复用 channel；
- 只对已 final 且立即可发送记录做 no-delay coalescing；
- 固定 message/batch/in-flight/deadline/retry budget；
- route绑定 model-control incarnation、shard routing epoch、logical pool/pool+binding generation；
- 同 generation 等价 replica可沿原 request identity/input digest重算；
- 第一份完整合法结果写 result WAL，late duplicate审计，冲突结果进入HOLD。

同一 shard任何时刻只有一个 canonical generation route。CPU/CUDA/model/pool切换必须走 Go durable rollout：route-withdraw → drain → WAL buffer → PG CAS → committed-binding handshake → resume。全池失败时不启动 Edge-local或另一 profile 推理。

## 8. Rule Observation

Go 在 effect exact readback后创建 canonical observation identity/epoch；Edge只按该 identity执行低优先级、有界 direct-counter sweep。每个 sample携带 target、rule/entity、generation/epoch/sequence、cumulative value、reset/wrap/quality和window。

Edge不创建 rule current、delta公式或业务百分比，也不主动清零 counter。P4 response的分片、乱序、重复按 canonical entity处理；queue满时显式记录 sequence gap，不能阻塞 effect或telemetry。

## 9. 公开边界

| 边界 | 方向 | 用途 |
|---|---|---|
| P4Runtime 1.4.1 | Edge ↔ target | arbitration、Read/Write、Digest/PacketIn、counter/register/table |
| Edge Control/Effect gRPC | Go ↔ Edge | assignment、intent、binding commit、effect result、health/readback |
| Result/Observation batch gRPC | Edge → Go | canonical inference result、rule observation、cursor/ACK |
| Central inference gRPC | Edge ↔ Gateway | final feature batch、result、retry/fence |
| optional gNMI read-only | Edge ↔ target | 资格化 Capabilities/Get/Subscribe allowlist；无 Set |

所有跨主机边界完整验证 mTLS identity、profile/version、size、deadline 和 trace。Edge 没有核心 PostgreSQL credential，也不接受 Web/插件直接调用 P4 primitive。

## 10. 启动与健康

- startup：验证 config/profile/certificate/local storage/WAL layout 和 Edge workload identity；不因单 target不可达阻止进程提供其他 target管理能力；
- process readiness：控制边界可服务、WAL可用、global resource manager就绪；
- per-target readiness：assignment有效、primary已证明、P4Info/application generation/read-only reconcile完成；与 process readiness分开；
- inference route readiness：exact binding/endpoint/readback/commit已确认；不等于 target readiness；
- liveness：runtime scheduler/WAL/health loop取得进展，不因外部模块暂时不可用制造重启风暴；
- shutdown：停止新 claim/source admission，按预算drain/flush/checkpoint，不能越过 lease或伪造成功。

## 11. 故障恢复

- P4 write response loss：沿原 operation exact readback；
- mastership/assignment loss：停止 write，保留 journal，read-only reconcile；
- Go ACK loss：重放同 result identity/digest，不能生成第二 Event；
- Central replica失败：同 generation bounded retry；全池失败为 WAL/backpressure/HOLD/gap；
- process crash/WAL tail：按 durable checkpoint重放，已 ACK与未 ACK分离；
- model/route late result：generation/route/digest fence；
- target/source reset：新 application/source/observation epoch，不跨代拼接；
- disk/FD/queue exhaustion：按优先级保护mastership/effect/telemetry，隔离目标并显式降级；
- PITR：只接受新 target/model control incarnation和新 assignment/binding generation。

## 12. 安全与资源

- target endpoint只能来自 canonical registry，拒绝 loopback/link-local/metadata/public/unapproved ranges、DNS rebinding、redirect和明文；
- P4/gNMI credential按target/role/protocol分离，gNMI使用只读identity；
- policy/input不能携带 raw P4 entity、path、pointer、FD、argv或可执行内容；
- per-target/global queue、task、connection、FD、memory、WAL/disk、RPC和retry均有上限；
- metrics只使用低基数target/status聚合；rule ID不成为Prometheus label；
- Central/Go/Host/Analysis不可取得P4 credential，Edge不可取得核心DB写入凭据。

## 13. 模块黑盒 E2E 验收范围

真实 Edge binary/OCI 与实际 Rust runtime必须启动。可以使用多 Fake/real P4Runtime target、qualified mirror fixture、Fake Central Gateway和Fake Go，但 Edge 自身 actor、compiler、window、WAL、route、journal、sampler不得 fake。验收覆盖：

- 1/2/N target assignment、arbitration、handoff、隔离与公平；
- firewall compile/preflight/bank/selector/overlay/effect readback；
- P4 aggregate、Digest/PacketIn、event-time/finality/quality/fragment；
- source/input/result WAL、Central batch/retry/dedupe/backpressure和Go ACK；
- rule counter sample/reset/generation/gap；
- crash、断网、response loss、磁盘/queue/FD耗尽和3,600 秒 soak；
- no second writer、no local inference、no DB write、资源和性能门槛。

## 14. Module Complete 判定

Edge 被分配的全部首期功能、错误、资源、安全、恢复和可观测性必须完整实现；真实 binary/OCI public-boundary E2E、Rust语言门禁、契约/golden、fault/security/resource/performance、3,600 秒 soak 和README命令必须实际执行且 operational checks 通过。`DEC-044` 要求完成状态从公开 evidence、命令 sidecar、digest 与 append-only `edge-rs/module-findings.json` 重派生：open P0=0、真实启动/必需测试 blocker=0 才能为 `COMPLETE`，篡改 summary 或缺失证据失败关闭。只用单target、memory mock、fake Edge、无真实 WAL crash、无Central network边界、实际性能/soak测试未运行或启动受阻时不得完成。dirty tree、受保护基线、生产绝对门槛及尚未执行的正式 pairwise/system 只保持 qualification-only HOLD，不阻断 operational completion，也不得被完成状态改写为资格 PASS。
