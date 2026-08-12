# ADR-0013：在线遥测采集、事件时间窗口与中央推理热路径

- 状态：Accepted
- 日期：2026-08-10
- 决策者：Owner
- 需求基线：`vNext-requirements-1.17`（原始热路径决策形成于 v1.10；CPU/CUDA启动选择、资格范围与真实E2E由ADR-0017/0006补充）
- 关联需求：`CORE-001`、`CORE-002`、`ARCH-003`、`ARCH-005`、`ARCH-TELEMETRY-001`、`ARCH-TARGET-FLEET-001`、`MOD-SW-001`、`MOD-EDGE-001`、`MOD-INF-001`、`MOD-TARGET-FLEET-001`、`CONTRACT-P4-001`、`CONTRACT-PROFILE-001`、`CONTRACT-MODEL-001`、`CONTRACT-TELEMETRY-001`、`CONTRACT-INFERENCE-001`、`CONTRACT-TARGET-001`、`FUNC-TEL-001`、`FUNC-INF-001`、`FUNC-TARGET-FLEET-001`、`PERF-TEL-INF-001`、`PERF-TARGET-FLEET-001`、`REL-INF-POOL-001`、`REL-TEL-INF-001`、`REL-TARGET-FLEET-001`、`SEC-TEL-INF-001`、`DEP-TEL-INF-001`、`OBS-TEL-INF-001`、`TEST-TEL-INF-001`、`TEST-REAL-E2E-001`、`TEST-TARGET-FLEET-001`、`DEC-001`、`DEC-002`、`DEC-018`、`DEC-026`、`DEC-027`、`DEC-028`、`DEC-030`、`DEC-032`、`DEC-033`、`DEC-034`、`DEC-035`

## 背景

“在线推理如何取得实时包数据”包含四个不同问题：交换机暴露什么、Edge 如何确认覆盖与窗口、Edge 与 Central Inference pool 如何有界传输、结果何时才可推进上游 cursor。若只写成“P4 digest → RPC → 模型”，实现会隐含以下错误假设：

- P4Runtime Digest ACK 是可靠传输确认；
- 多个 register/counter Read 天然形成原子快照；
- 抓到一些包等于完整覆盖；
- 每条记录单独 RPC、一个全局长流或多层延迟 batching 可以自然获得高性能；
- Gateway/Triton 返回成功即可丢弃 Edge 输入；
- 处理时间可以代替 packet/event time。

P4Runtime 1.4.1 明确指出 DigestList/DigestListAck 机制不是可靠传输；当数据面生成速度超过 server、channel 或 client 能力时，digest 可以被丢弃，规范也不限制未确认 DigestList 的在途数量。`max_timeout_ns=0` 或 `max_list_size=1` 仍只是 best effort。因此 digest 适合作为有界通知或采样，不适合作为“每包必达”的生产事实链。[P4Runtime 1.4.1 Digest](https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html)

成熟实现还提供了可借鉴但无需整体引入的机制：

- IPFIX 把 Observation Point/Domain、flow key、export time、sequence 与模板分开，适合作为项目 flow/window identity 与覆盖语义的来源；项目不因此部署通用 IPFIX collector。[RFC 7011](https://www.rfc-editor.org/rfc/rfc7011.html)、[RFC 7012](https://www.rfc-editor.org/rfc/rfc7012.html)、[RFC 5103](https://www.rfc-editor.org/rfc/rfc5103.html)
- Linux `PACKET_MMAP` 使用可配置的 mmap 环形缓冲区减少 syscall/复制，并提供 TPACKET_V3 block、fanout 与丢包状态；适合作为兼容镜像采集 profile。[Linux Packet MMAP](https://docs.kernel.org/networking/packet_mmap.html)
- AF_XDP 使用每队列 socket、UMEM 与 SPSC rings；zero-copy、copy、`need_wakeup` 和 multi-buffer 都必须显式探测。默认 bind 可以从 zero-copy 回退到 copy，因此生产性能 profile 必须强制模式或 fail closed。[Linux AF_XDP](https://docs.kernel.org/networking/af_xdp.html)
- Suricata 的 AF_XDP 运维经验显示 XDP_DRV、zero-copy、RSS queue/thread 与 NUMA placement 都是部署合同的一部分，且被采集接口不能同时当普通管理接口使用。[Suricata AF_XDP](https://docs.suricata.io/en/latest/capture-hardware/af-xdp.html)
- ONNX Runtime I/O Binding 可以绑定外部分配或设备侧 input/output，避免把设备拷贝隐藏在 `Run()` 内；收益依赖 execution provider 和内存位置，不能对 CPU profile口头宣称“零拷贝”。[ORT I/O Binding](https://onnxruntime.ai/docs/performance/tune-performance/iobinding.html)、[ORT device tensors](https://onnxruntime.ai/docs/performance/device-tensor.html)
- Triton 的 dynamic batching 和 instance group 提供“最大 batch + 最大排队时延 + 有界实例并行”的成熟执行机制，并可按model configuration选择CPU或GPU instance；项目采用固定版本 Triton 作为中央 pool 内部执行器，但拒绝其 runtime model-control 成为第二模型控制面。[Triton optimization](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/optimization.html)、[model configuration](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_configuration.html)、[model management](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html)
- gRPC 官方建议复用 channel/stub、设置 deadline，并提醒长 stream 可能因并发流上限产生排队；项目因此使用按 pool/binding generation 复用连接的 batched-unary RPC，不建立全 fleet 单一长流。[gRPC performance](https://grpc.io/docs/guides/performance/)、[deadlines](https://grpc.io/docs/guides/deadlines/)

legacy MASI-NIDS 中曾有逐包 digest 实验，但生产化 v3 已收敛为有界 target/window/counter 输入、批量推理、WAL 与 checkpoint。vNext 只保留其外部行为和正确性不变量，不迁移 Python controller、JSONL/fsync-per-packet、无 TLS gRPC、旧 21 列 feature 或三类标签合同。

## 决策

### 1. 首期主路径

首期生产默认链固定为：

```text
packet
→ P4 pipeline updates bounded per-target/per-window metadata aggregates
→ qualified snapshot/epoch boundary
→ Rust Edge P4Runtime Read + strict decode
→ telemetry source WAL durable
→ event-time window finalization + feature adapter
→ inference-input WAL durable
→ bounded batched-unary gRPC/mTLS to stateless C++ Gateway
→ pinned Triton dynamic batching + startup-selected ONNX Runtime CPU or CUDA inference
→ Gateway canonical result adaptation
→ Edge validates/fences and durably records result
→ bounded Edge→Go gRPC batch
→ Go/PostgreSQL canonical Event durable
→ canonical ACK advances Edge result/source cursor
```

它是 **metadata/aggregate-first**，不是 raw-packet-first。默认模型只接收 feature contract 声明的定长数值 tensor；原始 payload 不进入 P4Runtime、Edge↔Gateway inference request、InferenceResult、PostgreSQL、日志、metrics 或 Frontend。

采集 backend 属于 Rust Edge 内部适配器，不新增第十个长期在线模块。多 target 时，每个 `TargetActor` 独占自己的 P4 session、source epoch/WAL/queue 与 observation identity，`TargetSupervisor` 只做 assignment、全局预算和公平调度。无论选用 P4 聚合、镜像 PACKET_MMAP 或 AF_XDP，Edge 仍是唯一 source/window owner 和唯一 P4Runtime client；C++ 不读取 NIC/P4，Go 不接收原始 packet。一个慢/断连 target 不得阻塞其他 target，也不得让低优先级 gNMI/rule sweep 饿死 mastership/effect readback 与 telemetry continuity。

### 2. Telemetry Source Profile 采用矩阵

| Profile | 结论 | 用途 | 关键边界 |
|---|---|---|---|
| `telemetry-p4-window/v1` | `ADOPT`，首期必选默认 | P4 counters/registers/metadata 形成目标窗口特征 | 必须有 target-qualified snapshot/epoch/freeze-or-flip 语义；不能把多个普通 Read 假定为原子快照 |
| `telemetry-p4-digest-sample/v1` | `ADOPT` supplemental | window-ready hint、稀疏异常提示、bounded sampled metadata | Digest/Ack best effort；必须报告生成/接收/去重/丢弃/coverage，不能推进完整性 cursor或代替主窗口 |
| `telemetry-p4-packetin-sample/v1` | `CONDITIONAL` | bounded header sample、诊断或 evidence | 只允许 sampling/truncation/queue 上限明确的小流量；不得成为全量检测 feed |
| `telemetry-mirror-packet-mmap/v1` | `CONDITIONAL` compatibility | 模型确需 packet header/flow reconstruction，或目标不具备合格 P4 聚合能力 | 使用专用 mirror/TAP；TPACKET_V3/fanout/ring/snaplen/drop/offload/flow-affinity 全部冻结 |
| `telemetry-mirror-af-xdp/v1` | `CONDITIONAL` high-rate | PACKET_MMAP 无法满足已冻结容量且目标 NIC/driver 支持 | 生产强制 exact XDP_DRV/XDP_ZEROCOPY 或明确的 copy profile；禁止静默 fallback；每 queue 单 owner、RSS/CPU/NUMA/UMEM/ring 资格化 |
| `telemetry-mirror-dpdk/v1` | `CONDITIONAL` future hardware | AF_XDP 仍无法满足真实硬件 line-rate/隔离 SLO | 只有容量证据触发；独立 PMD/hugepage/NIC/NUMA/CPU profile；不得与默认 backend 同时常驻 |
| 全量 PacketIn/digest、逐包 JSON/HTTP、通用流处理平台 | `REJECT` 首期 | 生产主检测热路径 | 易丢、复制/序列化/控制面开销或引入第二 runtime/control plane |

`ADOPT`/`CONDITIONAL` 是需求采用决策，不表示代码已实现或生产资格已取得。首期 Module Complete 必须至少实现并资格化 `telemetry-p4-window/v1`；镜像 backend 只有对应 feature contract、target profile 或容量证据触发时才成为该部署的必需门禁。

### 3. P4 aggregate snapshot 与 digest 语义

`telemetry-p4-window/v1` 必须选择 target 可证明的一种快照策略：

1. 数据面 epoch + 双 bank：P4 在窗口边界翻转 active bank，Edge 只读取冻结 bank；
2. target-supported freeze/barrier/snapshot primitive；
3. target-specific sequence-before/after 验证，且重读能在 deadline 内证明一致；
4. 若以上均不可证明，则该窗口为 `partial/not_measurable`，不得推理为 canonical complete window。

每个 target profile 固定 counter/register width、wrap/saturate、reset/clear、bank 数、target index 上限、P4Info IDs/bit widths、读 batch/deadline、窗口边界和 clear/advance 前置条件。application generation、pipeline/P4Info、source runtime epoch 或 bank epoch 变化必须开启新 source epoch，不能跨 epoch 拼接。

DigestList 只携带 schema 固定的 bounded metadata/hint。Edge 按 `(device, role, generation, pipeline, digest_id, list_id, canonical item digest)` 去重；只有对应 hint/metadata 已写入 telemetry WAL 后才发送 DigestListAck。该 ACK 只允许 target 清理 digest cache，不证明数据面覆盖、可靠传输或 canonical window 已完成。digest gap、server/client drop、cache reset 和 mastership change 都进入 quality/coverage。

### 4. Flow identity、event time 与窗口

`contracts/telemetry/v1` 借鉴 IPFIX 的 Observation Domain/Point，但使用项目自有固定 schema。source identity 至少包含：

```text
telemetry_source_id
+ target/device/role
+ observation_domain_id / observation_point_id
+ source_profile_digest
+ P4 program/P4Info/pipeline/application generation
+ source_runtime_epoch
+ shard_id
```

每个 record/window 分开保存 `event_time`、`source_export_time`、`edge_ingest_time`、`window_finalized_time` 和 `produced_time`；不得用一个 `timestamp` 混装。固定窗口采用半开区间 `[window_start, window_end)`。source/shard sequence 只在同一 runtime epoch 内单调；reconnect/reset 新开 epoch，不能用数字回到 0 伪装连续。

首期使用简单、有界的 event-time 语义，不引入 Beam/Flink runtime：

- 每 source/shard 维护 watermark；watermark 算法、最大乱序、idle timeout 与允许 lateness 进入 profile；
- watermark 之前仍可能变化的窗口为 `open`，到达 `window_end + allowed_lateness` 后才成为 `final`；
- 只有 `final + quality=valid` 的完整窗口进入 canonical inference；首期不对 provisional window 产生可撤销 Event；
- final 之后到达的数据记录为 `late_after_final` evidence/gap，不回写已产生 Event，不重开窗口；
- source idle、disconnect、sequence gap、capture drop、snapshot inconsistency、sampling coverage不足分别表达，不能把它们编码为全零 feature。

flow key 必须声明方向语义（unidirectional 或 canonical bidirectional）、observation point、IPv4/IPv6、protocol、端点、VLAN/tunnel（适用时）与 generation。非首片 IPv4/IPv6 fragment 无 L4 port 时必须使用显式 `port_unavailable/fragment_identity`，不得读取垃圾值或合并到 port 0。

### 5. Quality 与 sampling

telemetry quality 使用独立 namespace：`valid|partial|gap|stale|not_covered|not_measurable|invalid`，并带稳定 reason bitset；不得复用 effect `unknown`。每个窗口至少保存：

- source expected/observed sequence range 与 gap range；
- source/capture/kernel/NIC/P4/server/client 各层 drop；
- snapshot/epoch consistency；
- sampling algorithm、rate、seed/hash selection、eligible population、observed sample 和 coverage；
- parse/truncation/checksum/fragment/unsupported-field 计数；
- watermark、late record、idle/reconnect；
- quality decision 与 input digest。

采样窗口只能用于明确接受 sampling semantics 的模型/profile。不得把 sample count 直接当全量 packet count；加权估计必须由 feature contract 明确公式、误差/置信范围和不适用条件。

### 6. Edge↔Central Gateway transport 与唯一延迟 batcher

首期唯一生产 transport 是 `inference-central-grpc-batch/v1`：每个 Edge 按 logical pool、binding generation 和 endpoint 复用 HTTP/2 gRPC/mTLS channel，以异步、固定上限的 batched-unary RPC 发送 final input。禁止逐记录 RPC、逐记录 JSON、无限 in-flight、全 fleet 单一长 bidi stream，以及把 retry library 当 durable queue。

`contracts/inference/v1` 的 Protobuf source 必须固定：

- schema major/minor、request identity、payload digest、source/window/input identity、trace reference；
- model-control incarnation、inference shard、canonical route epoch、pool/binding generation、model/feature/label/adapter/runtime/profile digest；
- batch identity、record count/bytes、tensor name/ID、dtype、rank/shape、deadline 与 priority class；
- response worker runtime/attempt identity、actual model/runtime/backend/profile digest、started/completed time、canonical prediction/status/error；
- 最大 message/batch/record/tensor/metadata 大小以及 unknown major、duplicate、same-key/different-digest 的稳定错误语义。

Edge 只允许把已经 final 的相邻输入做 **no-delay coalescing**：有可发送记录就立即形成不超过 `max_batch_records|max_batch_bytes` 的 RPC，不另设等待定时器。唯一允许为了凑 batch 主动等待的 scheduler 是 Triton dynamic batcher；其 `max_queue_delay_microseconds`、preferred/max batch、queue policy、instance group、NUMA 和并发全部由启动前显式选择的 `model-runtime-central-cpu/v1` 或 `model-runtime-central-cuda/v1` 冻结。Gateway 只做身份、framing、quota、deadline、binding/fence 校验和结果适配，不做第二 delayed batch、durable queue、运行时profile/模型选择或业务状态。

Triton 启动时从只读、digest-pinned且闭包化的repository snapshot加载exact binding及其声明依赖，使用`model-control-mode=none`、strict readiness、禁用auto-complete，并显式固定instance group；额外model/version/config/backend或implicit instance拒绝。管理员在pool generation启动前显式选择ORT CPU或ORT CUDA profile，硬件probe只验证selected/observed envelope；任何mismatch均fail closed，不能自动换profile。CPU需测thread/affinity/NUMA/arena/RAM，CUDA需冻结并读回ORT provider partition，测量已声明host-side operator placement、I/O Binding/device tensor/pinned memory/stream和host↔device copy；运行期新CPU接管是profile drift，不是fallback。二者都不能笼统宣称“zero-copy”。CPU↔CUDA或TensorRT只能作为另一个exact qualified pool generation显式启用，不能在请求内或故障时自动fallback。

同一 exact pool/binding generation 内，Edge 可在健康 replica 间选择或失败转移；route 不绑定某台 worker，response 必须回报实际 worker runtime/attempt。只有 pool/model/contract/backend generation 变化才提升 canonical route epoch。同 request identity+input digest 的 bounded retry 是 at-least-once computation；Edge result WAL 与 Go/PostgreSQL idempotency 仍保证 exactly-once canonical Event。所有 replica 不可用时保留有界 input WAL/backpressure，超限形成 exact gap/HOLD，不得本地推理、补零或改投其他 generation。

### 7. Durable ACK 与 result→Event 映射

唯一推进顺序为：

```text
qualified source observation
→ telemetry/source WAL durable
→ final valid window + inference-input WAL durable
→ central Gateway request admitted
→ result returned and Edge validates all fences/digests
→ inference-result WAL durable
→ Edge sends bounded result batch to Go
→ Go validates and durably commits canonical Event/idempotency result in PostgreSQL
→ Go returns canonical ACK/cursor
→ Edge advances result/source checkpoint and later compacts WAL
```

RPC completion只表示本次计算调用完成，不是 source/Event ACK。Gateway/Triton 成功不允许 Edge 丢弃尚未被 Go/PostgreSQL确认的 canonical result。Edge→Go reconnect/replay沿原 result identity与payload digest；same identity+same digest幂等，same identity+different digest冲突。

`InferenceResult→Event` idempotency key 固定包含 source/window identity、model-control incarnation、inference shard、route epoch、binding generation、feature/label/adapter profile与input digest；具体 canonical tuple由`contracts/inference/v1`唯一编码。一个窗口可以产生零或多个 canonical prediction，但同一result identity不得产生第二Event；abstain/OOD/low-quality按合同产生明确结果或不产生Event，不能落到默认“normal”。

### 8. 性能、稳定性与兼容性

- 性能：P4 aggregate-first避免全包上送；Edge批量Read/预分配窗口/no-delay coalescing、复用gRPC channel、Triton唯一动态batching/instance并行、所选ORT profile和Edge→Go batch减少控制面往返、计算资源空转与数据库事务。CPU与CUDA分别测量端到端packet/window→Event p99、throughput、network bytes、CPU/RSS/RAM、batch填充率；CPU另测thread/NUMA/affinity/arena，CUDA另测VRAM/GPU利用率/stream/host-device copy，并在0/1/2/N target、最大firewall/observation叠加及一个slow target下验证公平预算。
- 稳定性：所有环节有固定容量、水位、sequence/gap、WAL、deadline与circuit；采集/窗口/network/Gateway/Triton/所选compute/Go任一处拥塞都反向背压或显式产生gap/HOLD。`availability-single/v1`表示恰好一个failure domain，可有1..N域内同profile副本但不声称HA；`availability-ha/v1`要求同exact CPU或CUDA profile跨至少两个故障域并满足N+1。同generation等价replica只允许沿原identity/input digest有界重试；只有HA profile可将跨故障域持续服务计入HA声明，pool-wide failure不改变profile/模型语义。
- 兼容性：source profile、window/feature contract与inference wire分别版本化。兼容minor由跨语言golden证明；layout/单位/window/flow-direction/quality或backend numeric语义改变升major并建立新pool generation。Gateway隔离Edge协议与Triton内部API，禁止浏览器或Go业务直接依赖Triton wire。
- 工程复杂度：首期只实现一个主采集profile、一个central gRPC transport、一个Gateway和两个启动时互斥的Triton/ORT CPU/CUDA runtime profile；拒绝自动设备选择、Edge-local fallback、多serving control plane与多层batcher。新增成本集中在两套独立构建/数值/性能证据，不增加业务模块或运行时切换状态机。
- 防火墙共存：ADR-0014 的 overlay/baseline/selector/direct-counter 与 telemetry aggregate 使用静态分区容量和控制面优先级；packet 热路径只增加已资格化 match/action，不引入 policy compiler/DB/plugin/LLM。benchmark 必须同时开启最大 firewall profile、aggregate snapshot 和 rule sweep，不能各自通过后假设叠加仍满足绝对 SLO。

## 被拒绝的方案

1. **逐包 P4 digest 作为可靠主源**：规范明确 best effort，重复过滤与无在途上限也不适合作为完整性链。
2. **全量 PacketIn/clone 到 controller**：控制面 stream 与payload复制不能承担默认数据面速率；仅保留有界采样/evidence。
3. **默认部署 AF_XDP/DPDK**：对现有 aggregate feature模型没有必要，且增加NIC独占、XDP program、queue/NUMA/hugepage和特权矩阵。
4. **只支持 libpcap/逐包 recv**：兼容性好但无法作为高性能生产门槛；PACKET_MMAP是最低镜像profile。
5. **Edge/Gateway 逐记录 JSON、HTTP 或 gRPC**：序列化、allocation与往返放大，违反批量热路径合同。
6. **Kafka/Flink/Beam 或另一个 serving router/control plane**：会增加第二 durable queue、window scheduler、模型控制面或路由事实；Triton仅作为pool内部固定执行器采用。
7. **Edge-local推理或CPU↔CUDA/其他模型自动fallback**：扩大供应链、数值与故障矩阵，并会让同一canonical route产生不同语义；Central CPU是启动前显式profile，不是fallback；pool不可用必须HOLD/gap。
8. **Gateway/Triton 返回即推进source cursor**：Go/PostgreSQL尚未durable时会在Edge/Control故障中永久丢Event。
9. **late data回写旧Event**：引入retraction/versioned Event和policy/effect撤销语义；首期选择final-only inference与显式late gap。

## 迁移与回滚

greenfield 实施顺序：

1. 冻结`contracts/telemetry/v1`、`contracts/inference/v1`、source/window/quality/wire golden和profile registry；
2. 用P4/BMv2 fake/real target分别实现并资格化`telemetry-p4-window/v1`，证明snapshot与durable clear/advance顺序；
3. 实现Edge窗口与WAL，再以`inference-central-grpc-batch/v1` fake Gateway完成Edge模块隔离黑盒；Central Inference分别真实启动Gateway+pinned Triton+ORT CPU/CUDA完成自身numeric/fault/performance门禁；fake证据不得升级为pairwise/system PASS；
4. 实现result WAL、Edge→Go replay和PostgreSQL canonical ACK；
5. 各模块Module Complete后按`TEST-004`与`TEST-REAL-E2E-001`在干净环境真实启动双方进行正式pairwise，再启动完整内部服务进行system E2E；
6. 只有feature/target/capacity证据触发时才增加PACKET_MMAP、AF_XDP或DPDK profile。

同一compatible contract内回滚到previous source/pool generation必须创建新的durable rollback operation，验证exact previous，提升route generation并完成pool readback/CAS；禁止两个generation同时产生同一canonical result。若previous不能读取当前持久事实或model binding，保持shard `HOLD/unavailable`，不得补零、启用本地fallback或恢复逐包JSON路径。

## 验证

至少覆盖：

- P4 bank flip/freeze/sequence snapshot、read/clear各阶段crash、counter/register reset/wrap、pipeline/mastership/generation变化；
- Digest duplicate/list cache/Ack丢失/server drop/client slow、PacketIn oversize/burst/drop，并证明二者不冒充coverage；
- packet/window source identity、IPv4/IPv6、双向flow、fragment、VLAN/tunnel、event/ingest/export/processing time；
- watermark、允许lateness、idle/reconnect、late-after-final、gap/partial/not-covered与禁止补零；
- PACKET_MMAP ring/fanout/drop/truncation/offload，以及条件AF_XDP copy/zero-copy、XDP_SKB/XDP_DRV、queue/UMEM/NUMA/fallback拒绝；
- Protobuf/gRPC canonical bytes、unknown major/minor、message/record/tensor上限、deadline/cancellation、channel reuse、connection churn、partial response、duplicate与same-key/different-digest；
- batch size/bytes/queue delay/deadline、empty/partial/max batch、queue saturation/backpressure、公平性和24小时soak；
- Triton dynamic batch/queue/instance group、唯一delayed batcher；ORT CPU的thread/NUMA/affinity/arena与ORT CUDA的GPU I/O Binding/actual copy bytes/stream分别通过numeric/performance golden，证据不继承；
- telemetry WAL→input WAL→result WAL→Go/PG durable ACK每个注入点的crash/timeout/replay，证明无丢失、无双Event、same-key/different-digest冲突；
- BMv2只获得software-target资格；真实hardware/NIC/AF_XDP/DPDK分别形成profile/evidence；
- 默认payload-free、无Gateway/Triton P4/NIC/数据库访问、无第二P4 client/effect queue/model control、无日志/metric/raw packet泄漏。
- 最大 firewall overlay/baseline/selector/counter 负载下，telemetry snapshot、mastership/effect readback优先级、packet/window→Event p99与资源仍达门槛；host firewall不参与packet path或性能结论。
- 1/2/N target 的 actor/source/WAL/queue/fence独立；一个target reconnect storm、P4Info drift、queue/FD/disk耗尽或条件gNMI过载不拖垮健康target，且N与绝对资源门槛未冻结时保持`HOLD/NOT RUN`。
- Module/Pairwise/System E2E分别验证实际service inventory：正式链必须真实启动相应Gateway/Triton/ORT、Edge、Go、PostgreSQL、P4和其他内部服务；fake/mock、readiness、microbenchmark或rehearsal不能PASS。

当前仓库仍处于初始化阶段。ADR Accepted只表示需求边界已确认，不表示P4 program、Edge source adapter/WAL/window、central gRPC contract、C++ Gateway/Triton pool、Go ingest或任何性能门槛已实现；证据形成前均为`HOLD/NOT RUN`。

## 参考

- P4Runtime 1.4.1：<https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html>
- IPFIX：<https://www.rfc-editor.org/rfc/rfc7011.html>、<https://www.rfc-editor.org/rfc/rfc7012.html>、<https://www.rfc-editor.org/rfc/rfc5103.html>
- Linux Packet MMAP/AF_XDP：<https://docs.kernel.org/networking/packet_mmap.html>、<https://docs.kernel.org/networking/af_xdp.html>
- Suricata AF_XDP：<https://docs.suricata.io/en/latest/capture-hardware/af-xdp.html>
- ONNX Runtime I/O Binding/device tensor/memory/threading：<https://onnxruntime.ai/docs/performance/tune-performance/iobinding.html>、<https://onnxruntime.ai/docs/performance/device-tensor.html>、<https://onnxruntime.ai/docs/performance/tune-performance/memory.html>、<https://onnxruntime.ai/docs/performance/tune-performance/threading.html>
- Triton optimization/dynamic batching/model management：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/optimization.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html>
- gRPC performance/retry/deadline：<https://grpc.io/docs/guides/performance/>、<https://grpc.io/docs/guides/retry/>、<https://grpc.io/docs/guides/deadlines/>
- Apache Beam/Flink event-time/window/watermark（模式参考）：<https://beam.apache.org/documentation/programming-guide/#windowing>、<https://nightlies.apache.org/flink/flink-docs-release-2.3/docs/dev/datastream/event-time/generating_watermarks/>
- 多 target/fleet 与设备管理边界：`0015-multi-target-p4-fleet-and-device-management-boundary.md`
- 契约/Profile：`0005-contract-runtime-and-protocol-profiles.md`
- 资格证据：`0006-qualification-levels-and-evidence.md`
- 成熟组件采用：`0009-mature-component-reuse-boundaries.md`
- 模型模块化/启动绑定/central pool：`0010-online-model-lifecycle-and-rollout.md`、`0011-startup-bound-model-selection-and-rolling-restart.md`、`0016-central-gpu-inference-pool-and-routing-boundary.md`（v1.13历史）、`0017-central-inference-runtime-selection-and-real-e2e.md`（现行）
- 成熟方案来源登记：`../research/mature-solutions-review-sources-2026-08-10.md`
- BMv2 无状态防火墙：`0014-bmv2-stateless-firewall-policy-and-activation.md`
