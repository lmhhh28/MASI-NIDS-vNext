# ADR-0011：启动时精确模型绑定与按分片滚动重启

- 状态：Partially Superseded；本机per-shard C++部署、路由与滚动重启机制先由ADR-0016的central-GPU pool替代，现行CPU/CUDA启动profile、资格范围与真实服务E2E由ADR-0017/0006定义
- 日期：2026-08-10
- 决策者：Owner
- 需求基线：原始决策形成于 v1.8，最后适用本机方案为 `vNext-requirements-1.10`；当前边界 `vNext-requirements-1.18`
- `document_status`: `historical_partially_superseded`
- `current_normative_source`: `../masi-nids-vnext-system-requirements-2026-08-09.md@vNext-requirements-1.18`、ADR-0017、ADR-0006
- `implementation_authority`: `current_normative_source_only`
- `qualification_authority`: `current_normative_source_only`
- `retained_invariants`: startup-bound exact binding、无runtime load/unload、Edge唯一route、readback/CAS/fence、显式rollback与WAL gap；仅因现行需求再次规定而有效
- 关联需求：`CORE-MODEL-001`、`ARCH-MODEL-001`、`ARCH-TELEMETRY-001`、`MOD-INF-001`、`MOD-CTRL-001`、`CONTRACT-PROFILE-001`、`CONTRACT-MODEL-001`、`CONTRACT-TELEMETRY-001`、`CONTRACT-INFERENCE-001`、`FUNC-INF-MODEL-001`、`DB-MODEL-001`、`PERF-INF-001`、`PERF-TEL-INF-001`、`REL-INF-001`、`REL-INF-POOL-001`、`REL-TEL-INF-001`、`DEP-INF-001`、`OBS-INF-001`、`TEST-INF-001`、`TEST-TEL-INF-001`、`MIG-INF-001`、`ACCEPT-001`、`DEC-001`、`DEC-002`、`DEC-026`、`DEC-027`、`DEC-028`、`DEC-030`、`DEC-033`、`DEC-034`
- 替代范围：部分替代 ADR-0010 的进程内 candidate、online shadow、`PrepareModel/ActivateAtBoundary`、双/多 Session 与全 required-node 原子 current；ADR-0010 的三层模块化、immutable bundle、语义合同、Go/PostgreSQL 所有权、exact digest、资格、安全和 effect 边界继续有效
- v1.8 修订：不改变单 Session/startup-bound 主决策；补充 model-control incarnation、Edge 唯一路由、wire/optimization exact identity、部署动作/终止幂等、WAL gap/resume、commit handshake、滚动容量与 restart/quarantine 门禁
- v1.10 配套：不改变启动前选模和滚动重启；实时遥测 source profile、event-time final window、Rust↔C++ 固定布局 SPSC ABI、result WAL 与 PostgreSQL Event commit 后 canonical ACK 统一由 ADR-0013/`DEC-030` 定义

> **历史文档：不可作为实现或PASS依据。** 本文只保留上列不变量。以下把C++放在每个Edge、使用本机SPSC/UDS、逐shard停启本机进程、单本机Session与blue-green顺序的具体实现均为v1.10历史。现行实现与资格只读[ADR-0017](0017-central-inference-runtime-selection-and-real-e2e.md)、[ADR-0006](0006-qualification-levels-and-evidence.md)及v1.17需求；模型与ORT CPU或CUDA profile在pool generation启动前固定，同generation仅同profile等价replica，HA由availability profile决定，不存在Edge-local、CPU↔CUDA或异模型自动回退。

## 背景

ADR-0010 首先解决了“模型能不能安全拆卸替换”：稳定 C++ engine、不可变 model bundle、版本化 feature/label/output contract 和 Go/PostgreSQL exact binding 缺一不可。它同时把首期切换设计成进程内 side-by-side prepare、online shadow 和 batch-boundary activation。

进一步评估后，首期不需要为“可替换”支付“运行期热插拔”的全部成本。双 Session 会扩大 RSS/VRAM、allocator/execution-provider、warmup、并发状态、故障注入和跨节点一致性矩阵；online shadow 还需要隔离 comparison queue、幂等和资源抢占。用户接受在部署前选定模型并通过滚动重启切换，因此应把默认机制收敛到更小、可证明的边界。

官方资料支持把“启动加载”视为成熟而非退化的运行模式：

- ONNX Runtime C++ 的 [`Ort::Session`](https://onnxruntime.ai/docs/api/c/struct_ort_1_1_session.html) 从模型文件或内存构造推理会话；它提供推理 runtime，不提供 MASI-NIDS 所需的 catalog、durable rollout 或 canonical binding 控制面。
- ONNX Runtime [graph optimization](https://onnxruntime.ai/docs/performance/model-optimizations/graph-optimizations.html) 区分 Session 创建时的 online optimization 与预先保存的 offline optimized model；后者可降低启动开销，但必须绑定兼容的 execution provider、options 与硬件，不能把一个 optimized artifact 当作通用模型。
- ONNX Runtime [thread management](https://onnxruntime.ai/docs/performance/tune-performance/threading.html) 暴露 intra/inter-op、execution mode、spinning、affinity/NUMA 和 per-session/global pool；这些默认值会影响延迟、CPU与线程数，必须进入 exact runtime profile而不是随机器漂移。
- Triton [model management](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html) 把 `NONE` 定义为启动时加载并忽略 repository 变化的默认模式，同时另设 `EXPLICIT`/`POLL` 运行期管理模式。这证明“startup-only”是明确的成熟 profile；本项目只借鉴模式，不引入 Triton serving/control plane。
- TensorFlow Serving [architecture](https://www.tensorflow.org/tfx/serving/architecture) 明确区分 availability-preserving（先加载新模型再卸载旧模型，消耗更多资源）和 resource-preserving（先卸载旧模型再加载新模型，可能产生服务间隙）更新；这正是进程/模型并存与资源复杂度之间的真实取舍。
- Kubernetes [Deployment rolling update](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/) 与 [probe](https://kubernetes.io/docs/concepts/workloads/pods/probes/) 可编排逐实例替换和摘流量，但 Deployment/Pod/Ready 只描述进程与路由状态，不能证明 exact model bytes 已成为 PostgreSQL canonical current。
- Kubernetes 官方还明确：terminating Pod 会继续消耗资源并可能使实际资源超过 `replicas + maxSurge`；Pod grace period从 `PreStop` 前开始，到期会强制终止；PDB不限制Deployment自身rolling update。故 rollout capacity、drain/termination和动作幂等必须由项目合同证明，不能由PDB或`Ready`推断。[Deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)、[Pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)、[lifecycle hooks](https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/)、[disruptions/PDB](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)

## 决策

### 1. 模型仍可拆卸，但在启动前选择

在线检测继续使用：

```text
稳定 C++ inference binary/image
+ immutable digest-pinned model bundle
+ versioned feature / label / output contracts
+ Go/PostgreSQL per-shard exact binding
```

model bundle 在 Offline ML/CI 中训练、导出、扫描、资格化并可预优化；它不是编译进 C++ binary 的常量。部署一个兼容模型只改变 exact bundle、startup envelope 和 rollout operation，不修改 Edge/Go 业务代码，也不重新编译 C++。需要新的 custom op/backend/ABI 时才改变 inference image/runtime profile并执行 old/new reader matrix；其中在线输入/结果 ABI 的唯一规范见 ADR-0013，模型 bundle 不得私自扩展其 native layout。

首期每个 C++ 进程只创建一个 `Ort::Session` 或一个等价已资格化 backend session。进程运行期间不提供模型 load/unload/swap mutation，不存在 production candidate slot、online shadow slot或第二 Session。

`model-runtime/v1` 必须精确选择 `session_online_optimization` 或 `preoptimized_offline`。online模式绑定raw model与全部Session options并测量优化阶段；offline模式同时绑定raw/optimized model、生成工具、ORT、EP/options、device/CPU feature，并在加载时关闭不应重复执行的优化。缺失或不兼容时startup失败，禁止在两种模式间静默fallback。intra/inter-op thread、execution mode、affinity/NUMA、spinning、global/per-session pool、arena/memory-pattern也必须冻结和benchmark。

### 2. Immutable Startup Envelope

Go Model Manager 为每个待滚动 shard 创建受审计的 exact startup binding；部署 adapter 将其物化为 immutable startup envelope。至少包含：

- schema/profile、`model_control_incarnation_id`、operation kind/ID、request 与 envelope digest；
- scope、`inference_shard`、`shard_routing_epoch`、endpoint identity、node identity、node/process runtime/deployment epoch；
- expected current generation、proposed generation；
- model revision、bundle/manifest/model/scaler/config digest；
- feature schema、label taxonomy、output adapter、`inference_wire_profile_digest`、runtime/resource/qualification digest；
- C++ image/backend/execution provider/device、optimization mode、raw/optimized artifact与生成options/tool digest；
- issued/expiry、trace ID。

envelope 通过认证部署接口或受控普通文件交付；文件必须使用绝对路径、普通文件、no-symlink、精确 owner/mode/size/digest。tag、alias、目录最新项、mtime、Pod annotation 或环境自动探测不能参与选择。

C++ 在创建 Session 前完整验证 envelope、bundle、metadata、shape/dtype/opset/operator、wire/resource和optimization identity/compatibility。失败则startup/readiness失败，不加载默认模型、任意previous或另一optimization mode。

### 3. C++ 只有只读运行状态边界

C++ 暴露 startup/readiness/liveness 和只读 `GetLoadedModel`（或契约等价方法）：

- startup：验证并创建唯一 Session、执行有界 warmup；
- readiness：Session 可服务且 `GetLoadedModel` 能返回 exact identity；
- liveness：进程自身仍能取得进展；
- `GetLoadedModel`：返回 envelope、model-control incarnation、operation、shard/route/node/process runtime epoch、endpoint、实际 bundle/contract/wire/runtime/optimization/profile digest、proposed generation，以及verification/unpack/optimization/Session-create/warmup分阶段结果与时间。

`loaded/ready` 不等于 `current`。只有 Go 在 PostgreSQL 中完成该 shard 的 CAS 后，Edge 才恢复 canonical traffic。C++ 不连接 PostgreSQL，也不拥有 desired/current/previous。

### 4. 按 `inference_shard` 滚动

`inference_shard` 是稳定的在线输入所有权/路由分片；首期通常对应一个 Edge 节点上的 C++ inference 实例。Edge 是该 shard 唯一 canonical input router，任何时刻只向一个 exact `(model_control_incarnation_id, shard_routing_epoch, endpoint, process_runtime_epoch, binding_generation)` 投递。Kubernetes Service、KServe/serving router或deployment adapter不能成为第二 route owner。canonical binding 是 `(model_control_incarnation_id, inference_scope, inference_shard)` 级事实，不建立一个需要全节点同时翻转的虚构全局 pointer。

单 shard 顺序固定为：

```text
durable rollout operation + expected binding vector
→ pre-stage exact bundle and startup envelope
→ Edge advances routing epoch and withdraws the canonical shard route
→ Edge stops admitting new windows for the shard
→ drain already-admitted complete windows
→ Edge WAL-backed bounded-buffers later complete windows
→ deployment adapter idempotently stops/starts C++
→ one Session load + warmup + readiness
→ GetLoadedModel exact readback
→ PostgreSQL short transaction CAS current/previous under exact incarnation
→ Go/Edge idempotent committed-binding handshake
→ resume shard; continue with next shard
```

外部 artifact、进程控制、readiness 或 RPC 等待期间不能持有 PostgreSQL transaction/connection。每个部署动作使用 `(operation_id, shard, node_runtime_epoch, action_seq, action_kind)+request_digest` 幂等；同key异digest冲突。首期同一rollout group默认一次重启一个shard；提高并发必须由capacity/resource profile资格化。

上述顺序只定义模型 route/binding 的变更，不另造数据确认链。未滚动与恢复后的正常数据始终遵守 ADR-0013：source WAL/final-window input WAL → C++ result → result WAL → Go/PostgreSQL Event commit → canonical ACK → Edge cursor/compaction；C++ 成功、ring slot 释放、RPC send 或 deployment readiness 都不得替代该 ACK。

### 5. Mixed Rollout 是显式正常状态

滚动期间，已 finalize shard 的新 revision 与尚未处理 shard 的旧 revision分别是各自 canonical current。group projection 为 `rolling_mixed`，不是“全局 current 已切换”或“partial node 都 non-canonical”。

所有 input/window/result/Event 携带 shard、telemetry source/generation、window identity、model/binding generation、feature/label/output 与 hot-path profile digest。禁止跨 generation 拼接 window、cursor 或 rollup；跨 shard聚合必须保留 revision/generation分组。依赖所有 shard 语义一致的 deterministic policy/effect 在 mixed 期间默认 `HOLD`，只有版本化 compatibility policy 明确允许两个 exact revision时例外。新增 label仍不自动获得 effect eligibility。

### 6. 失败、停止和回滚

operation 状态为：

- rollout：`requested → staging → rolling → completed`；
- rollback：`rollback_requested → staging → rolling_back → completed`；
- exact-current recovery：`recovery_requested → staging → recovering → completed`；
- terminal：`completed|failed|aborted`。

只有未开始任何 shard route-withdraw/drain 的 request 可以 abort。per-shard observation 另用 `pending|route_withdrawn|draining|drained|stopped|starting|ready|readback_verified|current|resume_pending|resumed|unavailable|quarantined|rollback_ready`，不能与 operation、qualification、effect `unknown` 共用 enum。同一shard的rollout/rollback/recovery只允许一个durable operation/lease推进；自动recovery只可重建exact current，不能改变revision或暗中rollback。

某 shard startup/readback/CAS 失败时停止后续 rollout：

- 未处理 shard 继续其旧 current；
- 已 finalize shard 保持 exact 新 current；
- 失败 shard 的 canonical current 仍是最后一次成功 CAS 的 exact binding（CAS 前失败通常仍为旧 revision，CAS 后恢复失败则为新 revision），availability 另标 `unavailable/HOLD`，直到运行实例 readback 与该 current重新一致；
- operation 保存 exact updated/pending/failed vector，并以 `failed` 终止；group 显示 `rollout_failed_mixed`。

系统不执行盲目全组自动回滚。回滚是新的 durable operation，按每 shard exact current/previous 重新验证 qualification、revocation、artifact 与 reader/runtime compatibility 后滚动重启。previous 只需保存在 exact artifact cache/事实中，不常驻第二 Session。

CAS已提交新current但Edge commit/resume失败时，不撤销事实：availability进入`resume_pending/unavailable`，沿原handshake与watermark有界reconcile；预算耗尽后quarantine/HOLD。只有新的durable rollback operation可以改变current。

### 7. 部署、终止与恢复 Fence

deployment adapter只执行action，不拥有binding、route或operation state。它必须返回workload UID/instance identity与C++ process runtime epoch；PID、Pod名、systemd active、Service endpoint或Ready均不足。正常stop必须已有Edge route-withdraw+drain ACK；Kubernetes `PreStop`/SIGTERM只作幂等兜底，不能作为唯一drain机制。hook重复/失败、grace超时/SIGKILL或instance identity不明必须形成incomplete-drain、exact sequence gap和HOLD。

PostgreSQL无损failover保留`model_control_incarnation_id`。PITR/restore/clone/rewind在开放Go writer、rollout与Edge canonical ingest前必须生成从未使用的新incarnation；旧envelope/action/readback/handshake与同数字generation全部fenced。随后以new recovery operation/new generation逐shard启动、readback、CAS和commit；恢复平台不能直接把旧进程重新标为current。

restart/recovery profile必须冻结max attempts、backoff、total deadline、circuit/quarantine和解除权限；adapter必须能执行Go的同一budget，不能让liveness或Deployment controller在quarantine后无限重启。

### 8. Edge Buffer 与可用性语义

单节点 startup 方案必然存在有限检测间隙。Edge只在 ADR-0013 定义的同一 source/input/result WAL 与 checkpoint 所有权内为 restarting shard 保存有界 pending sequence，记录 window sequence/digest/completed time、binding/route epoch、durable gap range、canonical Event ACK 与 resume watermark，不建立第二 queue。profile必须冻结最大window/bytes/age、watermark、backpressure、overflow/drop/gap reason和恢复顺序。buffer age在同runtime epoch用monotonic `delivery-window_completed`；跨runtime/时钟无法证明时fail closed为stale/HOLD。达到上限时显式gap/HOLD，不无界增长、不补零、不跨generation重组；只有匹配 exact input/result/Event identity 的 PostgreSQL commit ACK 才能推进 durable cursor 或回收相应 WAL。

多 shard部署通过保留未滚动 shard容量降低整体影响，但不能假称单 shard“零中断”。readiness 只在模型加载和 warmup 后恢复；liveness 不因模型 registry、Go或数据库暂时不可达而重启风暴。

### 9. 后续零间隙能力的升级顺序

只有 `model-rollout-startup/v1` 在冻结目标硬件/模型/workload 下违反绝对 startup gap、buffer age/overflow、rolling capacity 或端到端 SLO，才允许扩大方案：

1. 先优化 artifact cache、offline graph optimization、warmup、shard 大小和滚动参数；
2. 再评估进程级 `model-rollout-blue-green/v1`，由两个隔离进程承担 old/new，证明双份 CPU/RSS/VRAM、路由切换、identity/fence、故障和回滚；
3. 最后才评估进程内双 Session/热切换；必须证明它相对 blue-green 的净收益，并为 allocator、execution provider、control RPC、并发故障和资源峰值建立新 profile与需求基线。

上游 runtime 能动态加载不构成项目授权。

## 性能、稳定性与兼容性影响

- 性能：steady state 只有一个 Session，最小化常驻 RSS/VRAM 与控制分支；代价转移到 rollout 的 startup-to-ready 和 Edge buffer，必须测量而不能口头假定。
- 稳定性：模型只在 startup 边界改变；普通崩溃恢复沿同一exact current/new process epoch，PITR用新incarnation消除ABA；没有运行期slot swap、shadow queue或双Session allocator竞态。
- 兼容性：model bytes 与 binary/image 分离；同合同模型只改 bundle/envelope，wire与optimization identity显式；major feature/output/backend变化仍走可解析双版本schema的reader image expand/contract，而不是同进程双Session。
- 可运维性：systemd/Podman/Kubernetes 可以复用成熟 restart/rolling/probe 机制，但不取得 model facts或 CAS所有权。
- 可用性：单 shard存在显式有界间隙；多 shard可滚动。若门槛不满足，以测量证据触发 blue-green，而不是预先把复杂度放进所有部署。
- 工程复杂度：新增字段和故障测试留在既有Go Model Manager、Edge WAL/router、C++ startup和deployment adapter内；不新增模型server、router、queue或数据库。绝对capacity按分片分别证明active service、startup buffer与恢复后的backlog replay，并同时计入warming、terminating资源；固定分片未证明可重路由前不能用group总容量掩盖单shard缺口。

## 被拒绝的方案

1. **首期进程内 candidate/online shadow/双 Session**：资源和故障矩阵超出当前必要范围。
2. **运行期按 tag/alias/repository 自动 reload**：不可复现并形成第二 active-model 控制面。
3. **把模型编译进 C++ binary**：破坏 bundle 独立资格和不改代码替换模型的目标。
4. **把全部 node 当作一个必须原子翻转的 current**：会让滚动升级产生长时间停顿或把已安全切换的 shard 误标为无效。
5. **把 Deployment/Pod Ready 当 current**：process routing state 不能证明 exact bundle、contract 或 PostgreSQL CAS。
6. **partial failure 自动盲回滚全部 shard**：会引入第二轮未确认副作用；使用新 operation 和 exact per-shard readback。
7. **首期直接引入 Triton/TensorFlow Serving/KServe**：它们的成熟模式值得借鉴，但会增加 serving/router/repository/control plane；当前没有容量证据需要转让本地热路径。
8. **依靠Pod Ready、PDB或PreStop证明安全滚动**：它们分别只表达局部健康/部分Eviction约束/终止hook，不能证明exact binding、drain、容量、CAS或resume。

## 迁移与回滚

v1.8 是greenfield模型切换决策，v1.10 又补齐与 ADR-0013 热路径合同的交界；当前仍无已部署runtime state需要在线迁移。实现顺序改为：

1. 冻结 `contracts/model/v1`、`model-runtime/v1`、`model-rollout-startup/v1`、golden、model-control incarnation、routing/action/handshake、startup envelope 和 fake deployment/C++ readback；
2. 资格化 Offline ML immutable bundle、offline replay和 optional offline optimized artifact；
3. 实现 C++ single-session startup/readback，再实现 Edge per-shard drain/buffer，再实现 Go/PostgreSQL rollout/CAS；
4. 实现 Web mixed rollout和 rolling rollback；
5. 各模块分别 Module Complete 后，才进行正式 pairwise/system integration。

ADR-0010 的旧 candidate/shadow/control method schema不得作为兼容层保留；仓库尚无已发布契约，因此直接以v1.8 clean-start。若未来从startup profile升级blue-green/热切换，采用新profile/ADR和expand/contract reader matrix，不原地改变v1 semantics。

## 验证

至少覆盖：

- startup envelope canonicalization、expiry、wrong shard/node/runtime epoch/current generation、digest/path/symlink/size/custom executable content；
- Session 数量硬上限为 1，production load/unload/candidate/shadow/双 Session API 和配置稳定拒绝；
- cold/warm/cache-hit/cache-miss、online/offline exact optimization、ORT thread/arena/NUMA、verification/unpack/optimization/Session-create/load/warmup/readiness/`GetLoadedModel`；
- Edge唯一route、route epoch、drain、已admitted window、source/input/result WAL bounded pending、backpressure、overflow/gap、PostgreSQL Event commit 后 canonical ACK、resume watermark 与 ordering；
- deployment action幂等/冲突、hook重复、graceful/SIGKILL termination、stop/start/readback/CAS/commit每个前后注入crash/timeout/response loss；
- 两个以上 shard 的 old/new mixed、late result fence、跨 generation merge拒绝、mixed-sensitive policy/effect HOLD；
- partial failure/`rollout_failed_mixed`、explicit rolling rollback、previous revoked/missing/incompatible；
- 无损PostgreSQL failover保持incarnation，PITR/restore/clone/rewind轮换incarnation；C++/Edge/Go restart和旧node/readback/同数字generation不能制造同shard双route/current；
- steady/peak/rolling qualified capacity、p99、CPU/RSS/VRAM、startup stages、active/warming/terminating/replay资源、per-shard unavailable、buffer age/depth/overflow/gap、restart/quarantine、group mixed duration和3,600 秒 soak的绝对门槛；
- systemd/Podman/Kubernetes全部作为 adapter时，deployment state均不能绕过 Go readback/CAS。

当前仓库仍处于初始化阶段。本文只记录已被部分替代的v1.10决策来源，不构成当前实现或资格声明；现行ADR-0017、contract、Central Inference、Edge routing/WAL、Go rollout/recovery、Web、真实服务E2E或生产资格在证据形成前均为`HOLD/NOT RUN`。

## 参考

- v1.13历史central-GPU pool：`0016-central-gpu-inference-pool-and-routing-boundary.md`
- 当前Central Inference CPU/CUDA启动绑定、路由、回滚与真实E2E：`0017-central-inference-runtime-selection-and-real-e2e.md`
- 被部分替代的模型模块化 ADR：`0010-online-model-lifecycle-and-rollout.md`
- 在线遥测、窗口、热路径 ABI 与 canonical ACK：`0013-online-telemetry-and-inference-hot-path.md`
- 在线模型专项评估：`../research/online-model-modularity-assessment-2026-08-10.md`
- ONNX Runtime C++ Session：<https://onnxruntime.ai/docs/api/c/struct_ort_1_1_session.html>
- ONNX Runtime C API/thread management：<https://onnxruntime.ai/docs/get-started/with-c.html>、<https://onnxruntime.ai/docs/performance/tune-performance/threading.html>
- ONNX Runtime graph optimization：<https://onnxruntime.ai/docs/performance/model-optimizations/graph-optimizations.html>
- NVIDIA Triton model management：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html>
- TensorFlow Serving architecture/config：<https://www.tensorflow.org/tfx/serving/architecture>、<https://www.tensorflow.org/tfx/serving/serving_config>
- Kubernetes Deployment/Pod lifecycle/hooks/disruptions/probes：<https://kubernetes.io/docs/concepts/workloads/controllers/deployment/>、<https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/>、<https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/>、<https://kubernetes.io/docs/concepts/workloads/pods/disruptions/>、<https://kubernetes.io/docs/concepts/workloads/pods/probes/>
