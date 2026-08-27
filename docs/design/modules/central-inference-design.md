# Central Inference 模块详细设计

- 模块 ID：`MOD-INF-001`
- 目录：`infer-cpp/`
- 文档状态：`DRAFT`
- 主要需求：`ARCH-005`、`ARCH-MODEL-001`、`CONTRACT-INFERENCE-001`、`CONTRACT-MODEL-001`、`FUNC-INF-001`、`FUNC-INF-MODEL-001`、`PERF-INF-001`、`PERF-TEL-INF-001`、`REL-INF-001`、`REL-INF-POOL-001`、`SEC-TEL-INF-001`、`DEP-INF-001`、`TEST-003`、`TEST-INF-001`、`TEST-TEL-INF-001`、`TEST-REAL-E2E-001`
- 主要 ADR：ADR-0005、ADR-0006、ADR-0009、ADR-0013、ADR-0017、ADR-0019；ADR-0016 仅作被部分替代的历史背景

## 1. 模块目标

Central Inference 是首期唯一在线模型执行面。它接收 Edge 的 final feature batch，验证身份/合同/配额和 generation，经 pinned Triton 的唯一延迟型 dynamic batcher 调用启动前显式选择的 ONNX Runtime CPU 或 CUDA profile，再返回带 exact identity 的 canonical inference result。

该模块不抓包、不组流、不访问 P4、不连接核心 PostgreSQL、不持有 model current、canonical route、Event、effect 或 durable queue。所有 replica 均为同一 exact pool generation 内的无状态执行 worker；Go/PostgreSQL拥有 binding，Edge拥有 route/WAL/cursor。

## 2. 一个模块、多个内部进程

```text
Central Inference qualification unit
├─ C++ MASI Gateway
│  ├─ mTLS workload identity
│  ├─ wire/tensor/digest/deadline validation
│  ├─ per-Edge/shard quota + bounded admission
│  ├─ pool/worker/generation fence
│  └─ deterministic result adapter/readback API
├─ pinned Triton server
│  ├─ model-control-mode=none
│  ├─ strict readiness, auto-complete disabled
│  ├─ sole delay-based dynamic batch scheduler
│  └─ explicit instance_group and bounded queue
├─ startup-selected ONNX Runtime
│  ├─ model-runtime-central-cpu/v1
│  └─ model-runtime-central-cuda/v1
├─ read-only exact model repository closure
└─ selected compute/resource profile
```

Gateway 与 Triton可以是独立进程/容器，但共同构成一个 `MOD-INF-001` 资格主体。只启动 Gateway、只测 ORT、只测 Triton或以 fake Gateway代替真实 Gateway都不能获得模块 PASS。

## 3. Artifact 与 Startup Envelope

### 3.1 Immutable Pool Envelope

Go Model Manager 产生并审计 exact desired pool envelope，部署 adapter 只物化它。Envelope 至少绑定：

- schema/profile、model-control incarnation、operation、logical pool/pool generation；
- model revision、bundle/repository/manifest/model/scaler/config digest；
- feature/label/output adapter/inference wire/runtime profile digest；
- Gateway/Triton/ORT image与config、optimization mode、instance group；
- availability/deployment tier、replica/worker identity和资源；
- issued/expiry、expected previous/current、trace和envelope digest。

Envelope 与 repository 只通过受控普通文件或认证部署接口交付，必须 absolute path、no symlink、owner/mode/size/digest正确。Tag、alias、目录最新项、mtime、外部 registry stage 和 Pod annotation 不参与选择。

### 3.2 Repository Closure

Triton `NONE` 模式启动时尝试加载 repository 内容，因此 snapshot 只允许包含 exact binding 及其声明依赖。额外 model/version/config/backend、隐式 instance group、custom native/Python/Wasm executable或未知 operator在启动前拒绝。

Repository/backend目录只读且不对外公开；production禁止 runtime load/unload、POLL/EXPLICIT model control、auto-complete和运行期下载。

## 4. CPU/CUDA 启动 Profile

CPU 与 CUDA 共用公开 inference contract、Gateway、Triton startup模式、model bundle和Go/Edge rollout语义，但使用独立 artifact、SBOM、硬件指纹、numeric和performance证据。

| Profile | 关键冻结内容 | 失败语义 |
|---|---|---|
| `model-runtime-central-cpu/v1` | CPU arch/features/微码、cores/threads、NUMA/affinity、intra/inter-op、execution mode、arena/RAM、batch/instance/queue | selected/observed或资源不符则startup fail closed |
| `model-runtime-central-cuda/v1` | ORT/CUDA/cuDNN/driver/GPU/compute capability、VRAM、instance/stream、I/O Binding、host-device copy、provider partition | GPU/driver/partition/resource不符则startup fail closed |

管理员在pool generation创建前显式选择 profile；probe只产生 observed envelope并验证，不自动选“最好设备”。同一 pool generation不得混合 CPU/CUDA worker。CUDA profile仅允许资格时已声明、读回和测量的host-side operator placement；运行期新增CPU接管是drift，不是合法 fallback。

CPU↔CUDA切换、新模型或backend变化都通过新pool generation与逐shard rollout完成。请求失败、过载或worker不可达时禁止自动换profile/backend/model/location。

## 5. Gateway 数据面

### 5.1 Admission Pipeline

Gateway 对每个 batched-unary request依次完成：

- mTLS peer/workload/scope验证；
- schema/profile major/minor、size/count/deadline/quota检查；
- request/input/source/window/model/pool/route/binding identity与digest检查；
- tensor name/ID/dtype/rank/shape/offset/length/alignment和整数溢出检查；
- bounded buffer admission；
- Triton call和actual worker/attempt observation；
- output shape/numeric/status检查与deterministic output adapter；
- response digest、timing和worker identity生成。

拒绝必须发生在大分配或模型执行前。Wire 不能携带path、pointer、FD、argv或可执行内容。

### 5.2 Batching

Edge只做无等待coalescing；Gateway不设第二batch timer、不持久化queue。唯一允许主动等待以凑batch的是Triton dynamic batcher，其preferred/max batch、max queue delay、queue policy、instance group、concurrency和shape policy由runtime profile固定。

Gateway、Triton和ORT的queue/in-flight/message/result/timeout都有硬上限。过载返回稳定admission/timeout错误，Edge决定有界retry/backpressure；Gateway不把内存queue伪装成durable工作队列。

### 5.3 Idempotency

计算语义允许at-least-once：同 request identity + input digest可由同 generation等价replica重复计算；每次执行使用新的attempt/worker runtime ID。Gateway不得自行决定哪一结果成为Event。Edge接受第一份完全匹配的结果并持久化，Go/PostgreSQL实现canonical Event幂等。

同identity不同input digest、同generation冲突output或wrong route/binding必须稳定冲突/HOLD；不能“最后返回者覆盖”。

### 5.4 ADR-0019 模型候选兼容边界

Central不解析训练数据集、超参、SHAP或模型业务名称；这些由Offline ML qualification和bundle digest绑定。ADR-0019首期Logistic、XGBoost和Autoencoder candidate都必须保持现有`[N,6] uint64-le -> [N,2] probability`公开合同；logit/margin/residual只允许作为ONNX图内Platt校准前的中间量。Gateway只执行同一shape/numeric/class-order/threshold/fence校验，不在运行时拟合校准器或阈值。

`xgb-window-binary/v1`只有在无ZipMap/custom op的ONNX-ML graph、标准算子Platt校准、Triton config、ORT operator closure与actual output tensor通过真实CPU profile测试后才能进入repository。ADR-0019首期三个recipe均只声明CPU；该exact bundle的CUDA适用性必须记录为`NOT_APPLICABLE`，不能用CPU operator placement冒充CUDA evidence。未来bundle显式声明CUDA时才增加独立CUDA门禁。Autoencoder必须在模型graph/bundle中完成scaled-log residual mean、Platt映射和两类概率输出，Gateway不实现第二套anomaly adapter。

ADR-0019的coefficient/TreeSHAP/reconstruction-residual是offline只读qualification evidence，不进入本模块实时result、response digest或readiness。未来逐Event explanation必须先有新contract/profile并重新验收，不能塞入`scores`、日志、metadata或任意额外Triton output绕过公开wire。

## 6. 只读状态边界

模块公开受限的 `GetLoadedModel`/`GetPoolStatus` 等价方法，返回：

- envelope/pool/model/runtime/profile/config/repository digest；
- selected与observed hardware/provider/instance group；
- worker/process runtime identity；
- verification/load/optimization/warmup/numeric probe阶段状态和时间；
- bounded capacity/queue/readiness observation。

`loaded|ready` 只表示worker可按该exact binding计算，不等于 per-shard `current`。Go只有在读回、资格、Edge withdraw/drain和PostgreSQL CAS后才能建立 current；Edge在commit handshake后恢复route。

## 7. 网络与安全边界

- Edge-facing入口只开放受限mTLS gRPC；Triton HTTP/model-control/repository/backend API不暴露给Edge、Go、Web、插件或公网；
- Gateway↔Triton仅在隔离推理网络/namespace可达，并固定server identity和message limits；
- 模块没有P4、核心DB、OIDC、plugin/effect credential；
- raw payload、secret、model path和unbounded tensor不进入日志/metrics；
- metrics只暴露低基数admission、queue、batch、latency、worker/profile/status；
- artifact/image/repository/config均需digest、publisher、SBOM/provenance和离线验证。

## 8. 启动与健康

启动不变式为：读取 explicit profile → probe observed environment → 验证 selected=observed → 验证 image/repository/config closure → Triton NONE load → warmup/numeric self-test → Gateway readback → readiness。

- startup失败：不加载default/previous/另一profile，不启动partial服务；
- readiness：Gateway可接收、Triton exact model ready、readback一致；不代表Go current；
- liveness：进程/执行线程取得进展，不依赖Go/DB短暂可达；
- drain：停止新admission，按有界deadline处理已admit工作并暴露未完成attempt；
- shutdown：停止Gateway/Triton并保留结构化startup/runtime evidence，不改Go binding。

## 9. Pool、Replica 与 Availability

同generation replica必须使用相同 model/feature/label/output/wire/runtime profile和numeric claim。service discovery/load balancer只在Go/部署投影允许的exact pool generation内返回fresh worker；Service/Pod/Ready不是route事实。

- `availability-single/v1`：一个failure domain，可有1..N域内同profile副本；可证明容量/域内retry，不声称HA；
- `availability-ha/v1`：同exact profile跨至少两个failure domain并具static N+1；必须实测replica/domain loss后的剩余容量和p99。

全池不可用时模块返回明确unavailable；Edge保持有界WAL/backpressure/gap且P4职责继续。Central不触发模型回滚；回滚是Go创建的新durable operation。

## 10. 性能设计

性能来自复用channel、batched-unary、bounded admission、Triton唯一dynamic batching、明确instance group和针对CPU/CUDA的执行优化，不来自多层隐藏queue。

端到端证据至少分段测量Edge network、Gateway validation/copy、Triton queue/batch、ORT execution、host/device copy和response；同时记录throughput、p50/p95/p99、error、queue、CPU/RSS/RAM，CUDA另记GPU/VRAM/stream/copy，CPU另记thread/NUMA/affinity/arena。

只测ORT `Run()`、Triton model latency或Gateway microbenchmark不能替代packet/window→Event SLO。绝对门槛、目标模型、硬件和profile未冻结时保持HOLD。

## 11. 故障与恢复

- malformed/oversize/wrong generation：admission前拒绝；
- Gateway/Triton/ORT crash/hang/OOM：worker unavailable，重启只恢复exact current envelope；
- response loss：Edge沿原identity有界retry，Gateway不创建durable状态；
- wrong/late result：Edge fence，Central提供worker/attempt evidence；
- repository/model/config drift：readiness fail closed/quarantine；
- CPU/CUDA resource drift：profile失效，不自动改选；
- restart storm：遵守deployment retry/circuit/quarantine profile；
- PITR old envelope或同数字generation：model-control incarnation fence；
- full-pool outage：不返回normal/last value，不触发Edge-local inference。

## 12. 模块黑盒 E2E 验收范围

必须真实启动发布候选Gateway、pinned Triton和所选ORT profile，使用golden gRPC batch、valid/invalid pool envelope/repository及Fake Edge/Go。验收覆盖：

- mTLS/framing/tensor/size/digest/quota/deadline/cancellation；
- explicit CPU或CUDA选择、selected=observed、wrong hardware/config负例；
- exact repository/NONE/instance group/load/warmup/readback；
- numeric/class order/OOD/NaN/Inf/output adapter；
- ADR-0019 candidate的现有wire兼容性：Logistic/XGBoost/Autoencoder `[N,2]`、XGBoost CPU ONNX-ML no-ZipMap/no-custom-op、离线解释零实时输出；
- dynamic batching、queue saturation、retry conflict、copy和资源；
- crash/hang/OOM/response loss/full-pool unavailable/no fallback；
- cold/warm/steady/peak/rolling/soak绝对性能；
- sanitizer、供应链、network/DB/P4 credential负例。

CPU与CUDA分别形成独立证据；只声明一种profile的release可只资格化该scope，但不得声称另一种已支持。

## 13. Module Complete 判定

Central Inference 只有在真实完整stack、所声明runtime/availability/deployment scope、全部功能/fault/security/numeric/performance/compatibility证据完成后才可标记Module Complete。fake Gateway、只测Triton/ORT、服务Ready、无absolute capacity或借另一profile证据均不满足。
