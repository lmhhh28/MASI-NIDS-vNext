# MASI-NIDS vNext Central-GPU 在线推理架构独立评估（v1.13历史）

- 评估日期：2026-08-11
- 需求基线：`vNext-requirements-1.13`（历史评估；现行基线为v1.17）
- 性质：成熟方案调研与独立取舍；不替代需求或 ADR
- 状态：Partially Superseded；ADR-0016保留中央架构与禁止运行时fallback，CPU/CUDA启动前显式选择及真实服务E2E改由ADR-0017定义
- `document_status`: `historical_partially_superseded`
- `current_normative_source`: `../masi-nids-vnext-system-requirements-2026-08-09.md@vNext-requirements-1.17`、ADR-0017、ADR-0006
- `implementation_authority`: `current_normative_source_only`
- `qualification_authority`: `current_normative_source_only`
- `retained_invariants`: Central Inference集中架构与禁止运行时自动fallback；仅因现行需求再次规定而有效

> **历史文档：不可作为实现或PASS依据。** 本文保留确认central-GPU方向时的评估依据，不再定义唯一runtime、availability或资格。现行方案允许同一中央架构启动前显式选择ORT CPU或CUDA；CPU不是GPU故障fallback。实现与验收只读[ADR-0017](../adr/0017-central-inference-runtime-selection-and-real-e2e.md)、[ADR-0006](../adr/0006-qualification-levels-and-evidence.md)及v1.17需求。
- 资格状态：文档级决策已形成；实现、目标环境 benchmark、故障注入与生产证据均为 `HOLD/NOT RUN`

## 独立结论

在用户已确认“内网低延迟、集中高性能 GPU、统一资源管理”的前提下，central-GPU 比每个 Edge 部署本机 C++/CPU 推理更适合本项目。它能把昂贵 GPU、batch、模型制品和 runtime 资格集中管理，并允许多个 Edge 的小批量请求在中央形成更饱满的 GPU batch。

但“中央化”不能简化成一个无状态 HTTP 服务，也不能依赖失败时偷偷改用本地 CPU。推荐边界是：

```text
P4 aggregate → Edge final window/input WAL
→ batched-unary gRPC/mTLS
→ stateless C++ MASI Gateway
→ pinned Triton dynamic batching
→ ONNX Runtime CUDA/GPU
→ Edge result WAL → Go/PostgreSQL canonical ACK
```

Go/PostgreSQL 持有 model revision、current/previous pool generation、rollout/CAS 和审计；Edge 持有 canonical input route、WAL、retry/fence；Gateway 只持有短暂请求；Triton 只调度和执行。这样采用成熟 GPU serving 组件，但不把 model current、业务路由、Event ACK 或 P4 权力交给它。

## 为什么不保留“失败回退”

Edge-local C++、CPU、TensorRT 或旧模型自动 fallback 看似提高可用性，实际会让同一个 canonical request 在不同数值 backend、模型、特征适配器和资源配置上运行。项目必须同时资格化每条回退路径的数值一致性、延迟、供应链、资源、切换 fence、恢复和审计，且容易把推理不可用伪装成正常检测。

首期更稳定的语义是：

- 同一 exact pool/binding generation 内，可在 active-active replica 间选择和故障转移；
- 同 request identity + input digest 可在固定 budget 内重复计算；
- 所有 replica 不可用时，Edge 使用已有 bounded input WAL 和 backpressure；超过 bytes/records/age 上限形成 exact `gap/HOLD/unavailable`；
- P4 forwarding、已安装规则、独立 effect recovery 继续，不把推理故障扩大成交换机故障；
- 恢复或回滚必须回到 exact qualified generation，而不是“找一个能跑的 backend”。

这不是要求系统永不失败，而是要求失败可见、可审计且不改变检测语义。

## 成熟方案核验

### Triton Inference Server

[Triton optimization guide](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/optimization.html) 说明 dynamic batching、并发 model instance 和 Model Analyzer 可用于寻找吞吐/延迟配置。项目采用这些执行机制，但最终 `max_batch_size`、queue delay、instance group 和 concurrency 必须冻结进 profile，不能在生产自动漂移。

[Triton model management](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html) 区分 `NONE`、`EXPLICIT`、`POLL`；`NONE` 在启动时加载模型并忽略运行期 repository 变化。项目采用 `NONE`，新模型通过隔离的新 pool generation 启动、预热、readback 和切路由，不开放生产 runtime load/unload 或 repository polling。

[Triton secure deployment](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/deploy.html) 明确默认部署需要额外安全加固。项目因此不把 Triton 直接暴露给 Edge、用户或插件；C++ Gateway 是外部 mTLS、身份、framing、配额和错误语义边界，Triton 端口仅在隔离 inference 网络可见。

独立判断：`ADOPT` Triton 作为模块内部执行器和唯一 delayed batcher；`REJECT` Triton repository state、model-control API、ready state或自带路由成为 canonical current。

### ONNX Runtime CUDA 与 TensorRT

[ORT CUDA Execution Provider](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html) 允许 ONNX graph 在 NVIDIA CUDA 上执行；[I/O Binding](https://onnxruntime.ai/docs/performance/tune-performance/iobinding.html) 可减少把 device copy 隐藏在 `Run()` 内，但需要调用方正确管理 memory location 和生命周期。

[ORT TensorRT Execution Provider](https://onnxruntime.ai/docs/execution-providers/TensorRT-ExecutionProvider.html) 同时存在 CUDA fallback、engine/timing cache、shape/profile 和 TensorRT/CUDA 兼容约束。这里的“fallback”是上游执行能力，不是项目授权。

独立判断：首期 `ADOPT` ORT CUDA exact profile；TensorRT 只在独立 numeric/performance/supply-chain matrix 通过后作为新 pool generation `CONDITIONAL` 使用，禁止请求内自动切换。每个 profile都报告实际 host↔device 和 Gateway↔Triton copy bytes，不能统一称为 zero-copy。

### gRPC

[gRPC performance guide](https://grpc.io/docs/guides/performance/) 建议复用 channel/stub，并指出长 stream 可能受到每连接并发 stream 上限和排队影响；[deadline guide](https://grpc.io/docs/guides/deadlines/) 说明默认没有 deadline；[retry guide](https://grpc.io/docs/guides/retry/) 区分透明重试和显式策略。

独立判断：采用异步 batched-unary、channel reuse、每方法 deadline/cancellation 和固定 retry budget；拒绝逐记录 RPC、无限 in-flight、全 fleet 单一 bidi stream，以及把 gRPC retry 当 durable queue。Edge 的 input/result WAL 才是可恢复所有者。

### Kubernetes 与 KServe

[Kubernetes GPU scheduling](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/) 使用 device plugin 和 extended resource 调度 GPU；[topology spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/) 可分散 workload；[PDB](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/) 只限制部分自愿中断，不能证明应用 N+1 或 rollout 正确。

[KServe control-plane architecture](https://kserve.github.io/website/docs/concepts/architecture/control-plane) 提供面向 Kubernetes 的模型服务控制面。它解决的范围与 MASI 自有 Go Model Manager、Edge canonical route 和 exact CAS 重叠。

独立判断：Kubernetes `CONDITIONAL` 为基础设施 adapter，固定 replicas、failure-domain spread、PDB 和 GPU placement，但这些对象不拥有 current/route。首期不用 scale-to-zero 或 HPA 证明容量。KServe、Ray Serve、TensorFlow Serving 和 MLflow serving control plane `REJECT`，避免第二模型/路由控制面。

## 最终逻辑与物理架构

逻辑上仍只有一个 `MOD-INF-001` qualification target；物理上分为：

1. **C++ MASI Gateway**：外部 mTLS endpoint、Protobuf contract、identity/digest/fence、quota/deadline、Triton adapter、canonical result/error；无 DB、P4、durable queue或模型选择。
2. **Pinned Triton**：唯一 delayed batch scheduler、instance scheduling、backend execution；启动时加载 exact snapshot，无生产 model-control mutation。
3. **ORT CUDA backend**：首期固定数值 backend；其 ORT/CUDA/cuDNN/driver/GPU/opset/operator组合全部进入 exact profile。
4. **Read-only repository snapshot**：由 CI/deployment job 从 digest-pinned制品预取和验证；不在请求路径访问公网或 mutable registry。
5. **GPU pool**：生产至少两个 failure domain、静态 N+1 qualified capacity；三机实验可以把一组 GPU replica 与 Analysis/SOC 共机，但不能据此声称生产 HA。

route 绑定 logical pool + binding generation，不绑定某台 worker。同 generation worker 改变不提升 canonical route epoch；model、contract、backend 或 pool generation 改变才提升。response 必须记录实际 worker runtime/attempt，便于故障和数值追踪。

## Batching 只有一处主动等待

两层都“等一等凑 batch”会把尾延迟叠加且难以归因。因此：

- Edge 只无等待地合并已经 final、当前就绪的连续记录，遇到 max records、max bytes 或最早 deadline 立即提交；
- Gateway 不设置第二 batching timer；
- Triton 是唯一允许按 `max_queue_delay` 等待的组件；
- batch size、fill ratio、queue time、GPU execution、end-to-end p99 必须同时观测；只提高 GPU throughput 而违反 packet/window→Event p99 的配置不能采用。

## 对性能、稳定性和兼容性的影响

| 维度 | 主要收益 | 新代价 | 必须冻结的门禁 |
|---|---|---|---|
| 性能 | 跨 Edge 汇聚批量、共享高性能 GPU、集中 instance tuning | 增加内网序列化、mTLS、RTT 与 host/device copy | network RTT/bytes、batch fill/queue、GPU throughput/utilization、端到端 p99、actual copy |
| 稳定性 | runtime/profile 集中，same-generation active-active，减少每 Edge backend 漂移 | 中央 pool 成为共享故障域 | 至少两故障域、静态 N+1、单 replica/域/全池故障、bounded WAL/gap、24h soak |
| 兼容性 | Gateway 隔离 Edge contract 与 Triton API；模型/bundle/runtime分层 | Gateway/Triton/ORT/CUDA/driver/GPU矩阵更明确也更严格 | exact digest、cross-language golden、numeric tolerance、current/previous reader matrix |
| 工程复杂度 | 删除本机 SPSC ABI、每 Edge runtime安装和多 fallback路径 | 新增 Gateway↔Triton、GPU调度、pool rollout/HA与网络资格 | 一个 transport、一个默认 backend、一个 rollout profile、无第二控制面/队列/batcher |

与旧“每 Edge 本机单 Session”相比，首期建设复杂度是**中等增加**：新增 GPU pool、Gateway/Triton内部边界、网络和 HA 测试；同时删除共享内存 ABI、每 Edge 模型进程生命周期和所有 fallback 分支。随着 Edge/target 数增加，集中 runtime、GPU 和模型资格的长期运维复杂度通常更低。这个判断是架构推论，不是性能事实；只有目标环境 benchmark 能确认净收益。

## 关键风险和控制

- **网络抖动/分区**：请求发送前进入 input WAL；固定 deadline/retry/circuit/in-flight；全池失联产生 HOLD/gap，不伪造 normal。
- **共享 pool noisy neighbor**：按 Edge/tenant/model设置固定 quota与公平 admission；production不接受无界 priority或临时超卖。
- **GPU OOM/fragmentation**：冻结 model bytes、batch、instance、workspace、pinned/device memory和VRAM headroom；OOM replica隔离，不自动换backend。
- **双 generation 混写**：Edge 唯一 route、per-shard withdraw/drain、binding generation/fence、Go CAS和late-result拒绝。
- **Triton攻击面**：隔离网络、只读 rootfs/repository/backend、最小 UID、无公开 HTTP/model-control、禁 auto-complete/未登记 backend。
- **中央故障影响扩大**：生产跨故障域 N+1；三机实验明确不等于 HA；P4 forwarding和effect链与inference availability解耦。

## 采用矩阵

| 组件/能力 | 结论 | 边界 |
|---|---|---|
| C++ MASI Gateway | `BUILD` | 项目身份、合同、fence、quota与稳定result/error边界 |
| Triton dynamic batching/instance group | `ADOPT` | 模块内部固定执行器；唯一 delayed batcher |
| Triton model-control/repository polling | `REJECT` | `model-control-mode=none`，新generation启动替换 |
| ONNX Runtime CUDA | `ADOPT` | 首期 exact default backend |
| TensorRT | `CONDITIONAL` | 独立generation、numeric/performance/supply资格；无自动fallback |
| gRPC batched-unary/mTLS | `ADOPT` | channel reuse、bounded in-flight、deadline/cancellation |
| Triton shared-memory extension | `CONDITIONAL` | 只有copy benchmark触发且同故障域资格化时采用 |
| Kubernetes GPU placement/spread/PDB | `CONDITIONAL` | infrastructure adapter；不证明current、N+1或rollout |
| KServe/Ray/TF Serving/MLflow serving | `REJECT first release` | 不建立第二model/route control plane |
| Edge-local/CPU/异模型 fallback | `REJECT` | unavailable必须HOLD/gap；回滚只到exact previous |

## 验证本结论所需证据

在冻结硬件/网络/模型/workload 数值前，不得声称 central-GPU 已比本机方案更快。至少需要：

- 1/2/N Edge、1/typical/max batch、steady/peak/saturation 的 packet/window→Event throughput与p50/p95/p99/max；
- Edge serialize/mTLS/RTT、Gateway admission、Triton queue/batch/backend、host-device copy、result WAL和DB ACK分段；
- 单worker、单GPU节点、单failure domain、全pool、network partition、deadline/response loss、GPU OOM/hang故障；
- same-identity retry/duplicate、same-key different-digest、late old generation、worker identity冲突；
- current→new generation、mixed rollout、response loss、PITR incarnation、exact previous rollback；
- CPU/RSS/VRAM/network/WAL、N+1 headroom、restart/quarantine和24小时soak；
- 负向证明没有 Edge-local/CPU/TensorRT/旧模型自动fallback，没有Triton runtime load/poll，没有第二route/current/Event ACK。

## 结论落点

- 强制需求：`../masi-nids-vnext-system-requirements-2026-08-09.md`
- v1.13 历史架构决策：`../adr/0016-central-gpu-inference-pool-and-routing-boundary.md`
- 现行 CPU/CUDA 与真实服务 E2E 决策：`../adr/0017-central-inference-runtime-selection-and-real-e2e.md`
- 在线遥测与传输：`../adr/0013-online-telemetry-and-inference-hot-path.md`
- 保留的模型模块化语义：`../adr/0010-online-model-lifecycle-and-rollout.md`
- 被部分替代的本机启动方案：`../adr/0011-startup-bound-model-selection-and-rolling-restart.md`
- 成熟组件总登记：`mature-solutions-review-sources-2026-08-10.md`
