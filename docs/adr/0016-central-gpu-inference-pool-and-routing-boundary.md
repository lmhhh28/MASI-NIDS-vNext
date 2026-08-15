# ADR-0016：Central GPU 推理池、路由与无 Fallback 边界（v1.13 历史决策）

- 状态：Partially Superseded by ADR-0017
- 日期：2026-08-11
- 决策者：Owner
- 需求基线：`vNext-requirements-1.13`（历史决策；现行基线见ADR-0017）
- `document_status`: `historical_partially_superseded`
- `current_normative_source`: `../masi-nids-vnext-system-requirements-2026-08-09.md@vNext-requirements-1.18`、ADR-0017、ADR-0006
- `implementation_authority`: `current_normative_source_only`
- `qualification_authority`: `current_normative_source_only`
- `retained_invariants`: 中央架构、Edge无本地推理、startup binding、唯一route、WAL/fence/readback/CAS与禁止自动fallback；仅因现行需求再次规定而有效
- 关联需求：`BASE-001`、`CORE-002`、`CORE-MODEL-001`、`ARCH-003`、`ARCH-005`、`ARCH-MODEL-001`、`ARCH-TELEMETRY-001`、`MOD-EDGE-001`、`MOD-INF-001`、`MOD-CTRL-001`、`CONTRACT-INFERENCE-001`、`CONTRACT-MODEL-001`、`CONTRACT-PROFILE-001`、`FUNC-INF-001`、`FUNC-INF-MODEL-001`、`DB-MODEL-001`、`PERF-INF-001`、`PERF-TEL-INF-001`、`REL-INF-001`、`REL-INF-POOL-001`、`SEC-TEL-INF-001`、`DEP-INF-001`、`DEP-TEL-INF-001`、`OBS-INF-001`、`OBS-TEL-INF-001`、`TEST-INF-001`、`TEST-TEL-INF-001`、`MIG-INF-001`、`MIG-TEL-INF-001`、`DEC-002`、`DEC-026`、`DEC-027`、`DEC-028`、`DEC-030`、`DEC-033`、`DEC-034`
- 部分替代：ADR-0010、ADR-0011、ADR-0013 中的 Edge-local C++ placement、本机 SPSC transport、逐 shard 重启本地 C++ 与“不采用 Triton runtime”结论；其 immutable bundle、startup-bound load、Edge 唯一路由、WAL、fence、readback/CAS、无事务跨外部等待和无在线 shadow 等语义继续有效
- 后续决策：ADR-0017/0006 与 `vNext-requirements-1.18` 替代本 ADR 的“CUDA 是唯一首期 runtime profile”“所有部署均强制 N+1”、资格聚合和正式 E2E 证据范围；本 ADR 所列保留不变量仅通过现行来源继续有效

> **历史文档：不可作为实现或PASS依据。** 本文完整保留 v1.13 的决策上下文供追溯。现行实现不得从“拒绝 CPU fallback”推导出“拒绝显式 CPU 启动 profile”；只能依据ADR-0017/0006与v1.17需求实现和验收。

## 背景

前序方案把 C++ 推理进程放在每个 Edge 节点，并通过本机 SPSC shared memory 获取 feature batch。它可以减少一次网络传输，但会把模型 runtime、CPU/GPU 资源、驱动兼容、升级、容量和故障恢复复制到每个 Edge。随着受管 P4 target 增多，该方案会形成以下问题：

- GPU 资源被按 Edge 分散，空闲容量不能统一合批或共享；
- 模型升级需要逐 Edge/逐 shard 重启本地进程，部署矩阵和回滚面随 Edge 数量增长；
- 一个 Edge 的设备控制职责与推理 runtime/driver 故障域耦合；
- 若再增加 CPU、本地旧模型或其他 backend 作为 fallback，会引入无法证明等价的多执行平面，导致检测语义随故障变化；
- 本机 SPSC 能优化进程间 copy，却不能解决 GPU batch 利用率、跨节点容量池化和统一模型资格。

本项目明确假设 central inference 部署在内网，网络延迟、带宽与可靠性能够按 profile 冻结，并使用高性能 GPU。该条件下，集中式 GPU pool 可以通过跨 Edge 合并请求提高 GPU 利用率，并把 runtime/driver/model repository 资格集中到一个模块；代价是网络成为检测面的显式依赖，必须通过 N+1、WAL、背压和可审计 gap 管理，而不能用未经资格的本地 fallback 掩盖。

NVIDIA Triton 提供 dynamic batching、并发 model instance 与多种 backend。官方优化指南指出 dynamic batching通常是最重要的性能优化，但增加 instance count不一定提高吞吐，某些模型只会增加延迟，因此参数必须按目标模型/硬件实测，而不能采用通用默认值。[Triton optimization](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/optimization.html)

Triton支持`NONE|EXPLICIT|POLL` model-control mode；`NONE`在启动时加载模型，运行期忽略 repository变化。其安全部署指南建议生产禁用不需要的接口和auto-complete、使用strict readiness/TLS，并指出动态更新model repository可能导致任意代码执行。这与项目的immutable startup-bound模型边界一致。[Triton model management](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html)、[secure deployment](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/deploy.html)

gRPC官方建议复用channel/stub、配置deadline，并指出长寿命stream一旦建立便不能重新负载均衡；retry又可能包含透明重试。因此本项目选择有界batched-unary RPC和显式应用幂等，而不是一个跨全池的永久双向stream。[gRPC performance](https://grpc.io/docs/guides/performance/)、[retry](https://grpc.io/docs/guides/retry/)、[deadline](https://grpc.io/docs/guides/deadlines/)

## 决策

### 1. 首期只存在一种生产推理架构

首期 production online inference 只采用 central-GPU。Edge 节点不部署本地 C++/CPU inference，不内置备用模型，也不在 central pool 失败时切换 TensorRT、LibTorch、CPU 或其他位置。

允许的恢复动作只有：

1. 在同一 exact logical pool/pool generation/binding generation 内选择另一个等价 replica，并沿用原 request identity/input digest 有界重试；
2. 恢复同一 exact current pool generation；
3. 通过新的 durable rollback operation 切换到仍合格的 exact previous pool generation。

前两项不改变模型/合同/backend，属于同执行平面的冗余；第三项是可审计的控制面变更。任何因错误自动改变模型、backend或位置的行为均是被禁止的 inference fallback。

### 2. 保持九模块，Central Inference 是一个资格主体

不增加第十个业务模块。`MOD-INF-001` 物理上由以下组件组成：

```text
Central GPU Inference Module
├─ C++ MASI Gateway
│  ├─ Edge mTLS identity / framing / contract validation
│  ├─ per-source/shard quota and bounded admission
│  ├─ result adaptation / identity / digest
│  └─ no durable queue, no model current, no delay-based batch timer
├─ digest-pinned NVIDIA Triton
│  ├─ startup-only model load
│  ├─ sole delay-based dynamic batch scheduler
│  └─ bounded instance group / backend execution
├─ ONNX Runtime CUDA backend
├─ read-only digest-pinned model repository snapshot
└─ qualified GPU/driver/CUDA/cuDNN resources
```

目录仍为`infer-cpp/`，用于C++ Gateway、Triton adapter/config与该整体的构建/资格资产；Triton/ORT镜像和依赖进入统一供应链inventory。模块内部可以是多个容器/进程，但共同接受一个`MOD-INF-001`黑盒、故障、性能和OCI部署门禁。

职责唯一性保持：

- Go/PostgreSQL：model revision、qualification、logical pool/pool generation、per-shard desired/current/previous、rollout和audit；
- Edge：telemetry/window/input+result WAL、每shard canonical route、retry/dedupe/fence、backpressure/gap；
- Gateway：边界校验、准入和结果适配；
- Triton：动态合批与执行调度；
- ORT CUDA：模型计算；
- Kubernetes/systemd/deployment adapter：启动、停止、placement和资源，不拥有current或route。

### 3. 数据路径固定为 batched-unary mTLS gRPC

首期 profile 为`inference-central-grpc-batch/v1`：

```text
P4 aggregate
→ Edge final feature window
→ input WAL durable
→ bounded batched-unary gRPC/mTLS
→ C++ Gateway validation/admission
→ Triton dynamic batching
→ ORT CUDA execution
→ Gateway result envelope
→ Edge fence/dedupe + result WAL
→ Go/PostgreSQL canonical Event ACK
```

Edge使用异步client、预分配buffer、channel/connection reuse和有界in-flight。每个RPC携带model-control incarnation、shard/route epoch、logical pool/pool+binding generation、source/window、request/attempt、合同digest、deadline和input digest。

不采用一个跨全池的长寿命bidi stream，因为它会把一个连接固定到一个backend，增加负载重平衡、故障迁移、stream级head-of-line blocking和恢复状态。若未来有证据证明stream明显优于batched unary，必须创建新wire profile并重跑old/new、load-balancing、failure与performance矩阵。

### 4. 只有 Triton 可以等待合批

Edge可以把已经final且同时可立即发送的记录无等待合并到network batch；达到max records、max bytes或最早deadline即发。Gateway不设置额外batch timer，也不持久化队列。需要等待以提高GPU利用率的dynamic batching只由Triton执行，并固定：

- preferred/max batch；
- max queue delay；
- max queue/in-flight；
- instance group；
- queue/reject/timeout policy；
- model/shape兼容策略。

这样避免Edge、Gateway和Triton三层各自等待导致尾延迟不可解释。Triton Model Analyzer或等价工具可以用于离线搜索参数，但最终值必须写入项目profile和evidence，不能在生产自动漂移。[Triton Model Analyzer](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/model_analyzer/docs/README.html)

### 5. Triton 使用 startup-only、只读、安全 profile

生产固定：

- `model-control-mode=none`；
- strict readiness；
- disable auto-complete；
- read-only、no-symlink、digest-pinned repository/backend目录；
- 禁止repository polling、runtime load/unload和mutable version auto-select；
- Triton HTTP/model-control API不暴露给Edge、Go、Web、Plugin或用户网络；
- Gateway是唯一Edge mTLS入口；Gateway↔Triton仅在隔离推理网络可达；
- 未登记custom backend、custom op、Python/native library和可执行model bundle内容全部拒绝。

新模型或backend必须创建隔离的新pool generation，不能修改运行中的repository。ORT CUDA是首期default；TensorRT只有在同一numeric合同、目标GPU和端到端benchmark证明收益后，才能成为显式新generation，不是ORT失败时的fallback。ORT官方兼容矩阵用于锁定CUDA/cuDNN，TensorRT EP还需锁定TensorRT版本与cache/profile。[ORT CUDA EP](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)、[ORT TensorRT EP](https://onnxruntime.ai/docs/execution-providers/TensorRT-ExecutionProvider.html)

### 6. Logical Pool 与 Worker Identity 分离

Edge canonical route绑定：

```text
(model_control_incarnation_id,
 inference_shard,
 shard_routing_epoch,
 logical_pool_id,
 pool_generation,
 binding_generation)
```

每次实际执行另记录`worker_runtime_id`与`attempt_id`。同generation replica选择或重试不提升`shard_routing_epoch`；切换pool/model/feature/output/backend generation必须提升route epoch并走drain/CAS/commit。

Service discovery或load balancer只能返回当前exact pool generation中已经通过worker readback且仍fresh的replica。Service、endpoint、Pod或Triton Ready都不是canonical route/current。禁止一个selector同时匹配old/new generation。

### 7. At-least-once 计算、Exactly-once Canonical Event

网络故障可能使同一输入被多个replica计算。项目不为GPU计算建立分布式exactly-once事务，而是：

- Edge在发送前durable input WAL；
- retry沿用相同request ID与input digest，并创建attempt ID；
- same request ID + same input digest幂等；same ID + different digest冲突；
- Edge接受第一份通过route/binding/input/output合同与digest的结果；
- late duplicate只丢弃/审计，conflicting output进入HOLD/告警；
- result先进入Edge result WAL；
- Go/PostgreSQL以result→Event idempotency key提交或返回原canonical Event；
- 只有canonical ACK后Edge推进checkpoint。

gRPC transparent retry和hedging默认关闭；只有profile证明其行为仍由相同identity安全吸收时才可启用。应用层retry固定attempt、per-attempt timeout、total deadline、backoff和retry budget。

### 8. HA 是 same-generation N+1，不是 fallback

production pool至少跨两个failure domain，并保持static N+1。资格必须证明在丢失profile定义的一个最大故障单元后：

```text
C_remaining >= lambda_peak × headroom
```

`C_remaining`必须在相同模型、batch、网络、竞争负载和p99 SLO下实测，不能相加单GPU kernel throughput推定。首期固定容量，不使用scale-to-zero，也不依赖autoscaler在故障发生后及时补容量。

Kubernetes可以条件复用：GPU device plugin/extended resources、node labels、topology spread、Deployment/Service和PDB。GPU extended resource帮助调度，topology spread帮助跨域放置；PDB只覆盖部分自愿中断，均不能证明应用N+1或current。[Kubernetes GPU scheduling](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/)、[topology spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)、[disruptions/PDB](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)

三机实验环境允许把Central Inference与Analysis/SOC共置在第三机，但该拓扑只有一个GPU failure domain，明确不能获得production HA PASS。

### 9. 全池不可用时 fail explicit

如果logical pool全部replica不可用、容量不足或deadline耗尽：

- P4 forwarding、已安装规则、Edge P4Runtime mastership/effect readback继续；
- Edge在现有input WAL的records/bytes/age上限内pending并反向backpressure；
- 达到上限后形成exact sequence gap和resume watermark；
- 相应检测availability为`HOLD/unavailable/gap`，不是effect `unknown`；
- 不生成normal/0分、不使用上一结果、不跨generation补推；
- 恢复后只重放仍未过期、合同/current一致的input；
- 由Go显式恢复exact current或创建rollback operation。

这会降低推理不可用时的检测可用性，但保持语义稳定、可审计，也避免错误的“正常”结论进入自动策略。

### 10. 模型切换使用新 Pool Generation

首期`model-rollout-pool-generation/v1`：

```text
durable rollout operation
→ pre-stage immutable repository snapshot and pool envelope
→ start new Gateway/Triton replicas
→ load/warmup/worker readback
→ qualify min-ready/failure-domain/N+1/rolling capacity
→ for each shard:
   Edge withdraw old route + drain + WAL buffer
   → PostgreSQL CAS current/previous to new logical pool generation
   → exact committed-binding handshake + resume
→ drain old pool within rollback grace
→ stop old generation
```

新旧generation可以在rollout/rollback grace内物理并存，但同一shard任何时刻只向一个generation发送canonical input。旧generation不是fallback；rollback必须是新durable operation并选择exact previous。

rollout容量同时计算current active、new warming、old draining和Edge replay。资源不足以同时满足冻结p99/throughput/VRAM/network门槛时，rollout保持HOLD，不能靠CPU或未资格backend完成。

## 成熟方案采用矩阵

| 候选 | 结论 | 项目边界 |
|---|---|---|
| NVIDIA Triton | `ADOPT execution only` | startup-only model load、dynamic batching、instance/backend执行；不拥有model current、route、Event或model-control API |
| ONNX Runtime CUDA EP | `ADOPT first default backend` | exact ORT/CUDA/cuDNN/driver/GPU profile与numeric/performance golden |
| TensorRT EP | `CONDITIONAL explicit generation` | 只有端到端收益、numeric合同、engine/cache兼容和回滚矩阵通过；无runtime fallback |
| Triton Model Analyzer | `ADOPT qualification tool` | 离线搜索batch/instance参数；结果必须固化，工具不进入生产控制面 |
| Kubernetes GPU scheduling/topology/PDB | `CONDITIONAL infrastructure adapter` | 固定副本、static N+1、placement；不拥有current/route，不启用scale-to-zero |
| KServe | `REJECT first-release serving control plane` | 其control/serving runtime、autoscaling和traffic management会扩大资格面并可能成为第二route/lifecycle owner |
| Ray Serve / TensorFlow Serving | `REJECT first-release runtime` | 与Triton重复batch/replica/model lifecycle，增加第二执行/更新语义 |
| MLflow Model Registry/Serving | `CONDITIONAL offline evidence only` / `REJECT serving current` | 可导出artifact/evidence；alias/stage/endpoint不成为PostgreSQL binding |
| Edge-local CPU/C++ inference | `REJECT fallback` | 增加第二execution plane、模型/driver/性能矩阵并使故障改变检测语义 |

KServe官方架构包含控制面、数据面、Storage Initializer、queue-proxy和autoscaling等职责；这些能力有价值，但首期引入会与项目已有Go Model Manager、Edge route和固定N+1重叠。[KServe control plane](https://kserve.github.io/website/docs/concepts/architecture/control-plane)

## 取舍

收益：

- GPU统一池化，dynamic batching可以跨Edge提升利用率；
- Edge不承担CUDA/driver/model runtime，P4控制故障域更小；
- 模型、runtime、GPU和性能资格集中，升级矩阵不再乘以Edge数量；
- active-active same-generation副本提供清晰的N+1，而不改变模型语义；
- Triton/ORT复用成熟执行机制，项目保留自己的事实、路由、WAL和审计；
- 无fallback使测试矩阵和故障语义可控，避免静默精度/类别/延迟漂移。

代价：

- 内网和central pool成为在线检测依赖；网络RTT/带宽必须进入SLO；
- Gateway增加一跳和一次潜在序列化/copy，必须通过batch、channel reuse和profile测量；
- production至少两个failure domain、static N+1，rollout还需new/old generation容量，GPU成本高于单实例；
- Edge需要实现跨主机admission、retry/dedupe、endpoint refresh与全池gap语义；
- Triton、CUDA、driver、GPU与Kubernetes adapter增加供应链和兼容矩阵；
- 三机实验不能代表production HA。

## 被拒绝的方案

1. **保留Edge-local推理作灾备**：形成第二模型执行平面和双倍资格矩阵；故障时结果语义、性能和类别可能变化。
2. **CPU自动fallback**：在过载时进一步消耗Edge CPU并放大延迟，且无法保证与GPU的deadline和numeric合同等价。
3. **TensorRT失败回ORT或ORT失败回TensorRT**：backend切换会改变engine/cache/operator/numeric/resource行为，必须是显式generation而不是异常分支。
4. **一个全局长寿命bidi stream**：连接固定backend，难以跨replica重平衡，stream故障和HOL blocking扩大影响。
5. **Gateway与Edge各自设置batch timer**：与Triton dynamic batching叠加，队列等待不可解释并放大p99。
6. **Triton EXPLICIT/POLL runtime model control**：增加在线mutation、repository竞态和可执行内容风险，与Go/PostgreSQL current冲突。
7. **直接让Edge调用Triton public API**：无法集中执行MASI contract、quota、identity、stable error和结果adapter，也会暴露过宽Triton surface。
8. **首期采用KServe/Ray Serve/TF Serving作为完整平台**：增加第二模型生命周期、router/autoscaler/traffic事实和大量资格面。
9. **只部署一个中央GPU节点**：适合实验，不满足稳定性；单机故障即全检测中断。
10. **把全池不可用结果编码为normal或沿用last value**：制造错误安全结论并污染Event/policy/effect。

## 迁移

1. 将`contracts/inference/v1`从本机SPSC改为`inference-central-grpc-batch/v1`，冻结request/result/pool/attempt identity、error、limits和golden；
2. 将`contracts/model/v1`加入logical pool/pool generation/worker observation和pool startup envelope；
3. Edge先实现Fake Gateway黑盒、input/result WAL、retry/dedupe、backpressure/gap和logical-route fence；
4. Central Inference独立实现Gateway+pinned Triton/ORT CUDA并通过Fake Edge/Go、numeric、fault、performance和security门禁；
5. Go/DB实现pool generation、worker/pool observation、per-shard CAS和rollout状态；
6. 删除/拒绝Edge-local inference配置、镜像、endpoint和fallback代码路径；
7. 先完成无副作用mTLS/wire rehearsal，再按全模块门禁进行正式pairwise；
8. 三机完成实验E2E；production另以两个failure domain、N+1和目标网络/GPU完成资格。

旧SPSC golden可保留为历史fixture证据，但不得进入v1.13 production profile或被误报为兼容fallback。

## 回滚

代码回滚只能回到仍能读取v1.13 model/pool持久事实和central inference wire的版本。不能读取新事实的旧Edge-local版本不得恢复production writer/ingest；系统保持P4职责可用、inference HOLD，不通过重新启用本地模型绕过。

模型回滚使用新的Go durable operation，选择仍qualified的exact previous pool generation。若old pool仍在rollback grace且readback/freshness/capacity全部有效，可以复用；否则从immutable snapshot重建。不存在“连接失败即回上一个模型”的隐式分支。

基础设施adapter可从Kubernetes换为systemd/Podman/其他受控OCI调度，只要`model-rollout-pool-generation/v1`、failure-domain/N+1、worker identity、mTLS和readback不变。移除KServe/Ray/MLflow等外部候选不影响canonical current。

## 验证

- contract/golden：Rust/C++/Go/Python对request/result、tensor、pool/attempt、digest/error一致；
- security：mTLS/SAN、oversize-before-allocation、wrong generation、Triton API isolation、repository no-symlink/read-only、custom executable拒绝；
- numeric：ORT CUDA exact profile、NaN/Inf/OOD、class order、tolerance；
- batching：Edge无等待coalescing、Tritondynamic queue/instance group、deadline和backpressure；
- HA：至少两个failure domain、single replica loss、failure-domain loss、stale endpoint、connection reset与N+1 capacity；
- outage：full-pool unavailable、Edge WAL records/bytes/age、backpressure、exact gap/resume、P4继续、无fallback；
- idempotency：same request/same digest多attempt只产生一个Event；different digest/conflicting output拒绝；
- rollout：new pool start/readback/capacity、逐sharddrain/CAS/commit、mixed、old drain、exact rollback；
- recovery：Gateway/Triton/GPU/Edge/Go/DB crash、response loss、PITR incarnation和old worker late result；
- performance：target observation→Event ACK端到端，分段网络/Gateway/Triton/ORT/GPU/copy/WAL/DB；steady/peak/N+1/rolling/saturation/3,600 秒 soak；
- 所有绝对GPU、网络、batch、queue、WAL、p99、failure-domain和rollback门槛未冻结前保持`HOLD/NOT RUN`。

## 参考

- Triton optimization：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/optimization.html>
- Triton dynamic batcher：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/batcher.html>
- Triton model management：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html>
- Triton secure deployment：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/deploy.html>
- Triton model repository：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_repository.html>
- ONNX Runtime CUDA EP：<https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html>
- ONNX Runtime TensorRT EP：<https://onnxruntime.ai/docs/execution-providers/TensorRT-ExecutionProvider.html>
- ONNX Runtime I/O Binding：<https://onnxruntime.ai/docs/performance/tune-performance/iobinding.html>
- gRPC performance/retry/deadline：<https://grpc.io/docs/guides/performance/>、<https://grpc.io/docs/guides/retry/>、<https://grpc.io/docs/guides/deadlines/>
- Kubernetes GPU/topology/disruptions：<https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/>、<https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/>、<https://kubernetes.io/docs/concepts/workloads/pods/disruptions/>
- KServe control plane：<https://kserve.github.io/website/docs/concepts/architecture/control-plane>
- 唯一需求基线：`../masi-nids-vnext-system-requirements-2026-08-09.md`
- 现行 CPU/CUDA 启动选择与真实服务 E2E：`0017-central-inference-runtime-selection-and-real-e2e.md`
