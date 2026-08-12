# ADR-0017：Central Inference CPU/CUDA 启动选择与真实服务 E2E

- 状态：Accepted
- 日期：2026-08-12
- 决策者：Owner
- 需求基线：`vNext-requirements-1.17`
- 关联需求：`BASE-001`、`ARCH-003`、`ARCH-005`、`ARCH-MODEL-001`、`MOD-INF-001`、`CONTRACT-INFERENCE-001`、`CONTRACT-MODEL-001`、`CONTRACT-PROFILE-001`、`FUNC-INF-001`、`FUNC-INF-MODEL-001`、`PERF-INF-001`、`REL-INF-001`、`REL-INF-POOL-001`、`SEC-TEL-INF-001`、`DEP-INF-001`、`OBS-INF-001`、`TEST-GATE-001`、`TEST-REAL-E2E-001`、`TEST-INF-001`、`ACCEPT-001`、`DEC-002`、`DEC-026`、`DEC-033`、`DEC-034`、`DEC-035`、`DEC-036`、`DEC-037`、`DEC-038`
- 替代范围：替代 ADR-0016 中“CUDA 是唯一首期 runtime profile”和“所有部署都必须 N+1”的结论，并补齐正式 Module/pairwise/system E2E 的真实服务边界；ADR-0016 的中央架构、启动绑定、不可变模型、WAL/fence/readback/CAS 和禁止运行时自动 fallback 继续有效

## 背景

中央推理的部署环境并不总有 NVIDIA GPU。开发机、演示环境和部分高核心数服务器可能只提供 CPU；若把 CUDA 写死为唯一启动条件，模型和系统链路无法在这些机器上获得真实运行证据。反过来，如果在一个进程内根据错误、负载或设备状态自动切换 CPU/CUDA，又会让同一请求的数值、延迟、容量和故障语义漂移，并扩大验证矩阵。

ONNX Runtime 通过 execution provider 抽象不同计算设备；CPU threading 文档还明确暴露 intra/inter-op、affinity、NUMA 和 spinning 等影响性能的选项。因此“支持 CPU”不是简单删除 CUDA 配置，而是建立独立、可复现的 runtime profile。[ORT execution providers](https://onnxruntime.ai/docs/execution-providers/)、[ORT threading](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)

Triton 的 model instance group 可以选择 CPU 或 GPU instance，且官方提供不包含 GPU 支持的构建方式；这允许项目保持相同 Gateway、Triton 和外部 gRPC 架构，同时交付不同的 CPU/CUDA 制品与配置。[Triton model configuration](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_configuration.html)、[Triton build](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/build.html)

本决策同时明确“真实启动服务并做 E2E”的含义。contract fake 能提早发现边界问题，但不能证明发布候选二进制、runtime、证书、数据库、P4 target 和浏览器在同一链路中确实工作。

## 决策

### 1. 只有一个 Central Inference 架构

首期仍只有一个中央在线推理模块，不恢复 Edge-local inference，也不增加第十个业务模块：

```text
Rust Edge
  → bounded batched-unary gRPC/mTLS
  → C++ MASI Gateway
  → digest-pinned Triton
  → startup-selected ONNX Runtime CPU or CUDA profile
  → canonical result
  → Edge WAL/fence
  → Go/PostgreSQL Event
```

CPU 与 CUDA 是同一模块、同一公开合同下的两个受控启动 profile，不是两个独立控制面。它们复用：

- `contracts/inference/v1` 请求/结果、identity、digest、deadline 和错误合同；
- immutable model bundle、feature schema、label taxonomy、output adapter 与 numeric golden；
- C++ Gateway、Triton startup-only/read-only model repository、安全边界和 Go/Edge rollout 状态机；
- Go/PostgreSQL canonical current/previous、Edge 唯一路由、WAL、重试/去重和 Event 幂等。

### 2. 启动前由管理员显式选择 Profile

首期只登记：

| Profile | 执行栈 | 必须冻结的主要环境 |
|---|---|---|
| `model-runtime-central-cpu/v1` | C++ Gateway + pinned Triton + ONNX Runtime CPU | CPU 架构/feature、微码、核心/线程、NUMA、affinity、intra/inter-op、execution mode、arena、RAM、batch/instance/queue、模型与 numeric/performance 门槛 |
| `model-runtime-central-cuda/v1` | C++ Gateway + pinned Triton + ONNX Runtime CUDA | ORT/CUDA/cuDNN/driver/GPU/compute capability、VRAM、instance/stream、I/O Binding、host/device copy、batch/queue、模型与 numeric/performance 门槛 |

启动顺序固定为：

```text
read explicit desired runtime_profile_id
→ probe actual CPU/GPU/runtime/driver/resources
→ compare desired profile with observed envelope
→ verify image/repository closure/model/config/explicit instance-group digests
→ start Triton in NONE mode and load only the exact binding closure
→ warmup + numeric self-test
→ Gateway GetLoadedModel/GetPoolStatus readback
→ readiness
```

硬件探测器只给出观察结果和建议，不能替管理员选择，也不能改写配置。未选择、选择未知 profile、镜像与 profile 不符、CUDA 被选但 GPU/driver 不合格、CPU feature/NUMA/RAM 不合格、repository出现closure外成员、instance group未显式固定、模型或 readback 不一致时，startup/readiness fail closed；不得偷偷改用另一个 profile。

evidence 必须同时保存 `selected_runtime_profile_id`、`observed_hardware_envelope`、actual ORT provider partition、repository closure/instance-group、image/runtime/config/model digest 与每个 startup stage，不能只记录“GPU available=true”或“服务 Ready”。CUDA EP不支持的节点可按ORT provider顺序落到CPU，但只允许资格化时已声明、冻结并测量的host-side operator placement；运行期新partition/CPU接管是profile drift并fail closed，不是切换到CPU profile或合法fallback。[ORT execution providers](https://onnxruntime.ai/docs/execution-providers/)

### 3. Profile 在 Pool Generation 生命周期内不可变

一个 pool generation 的全部 worker 必须使用同一 exact runtime profile、model/feature/label/output/wire digest 与相容 numeric contract。同 generation 内可选择等价 replica，但不得混合 CPU/CUDA worker。

CPU↔CUDA 切换是显式 rollout：

1. 管理员选择已资格化的目标 profile 和 immutable pool envelope；
2. Go 创建新的 durable rollout operation/pool generation；
3. deployment adapter 启动目标 CPU 或 CUDA 制品；
4. 完成 load、warmup、numeric、readback、容量与故障门禁；
5. Edge 先按 shard 提升route epoch、withdraw旧route、停止新canonical send、drain已admit工作并在既有WAL中有界buffer；只有这些完成后才允许PostgreSQL per-shard CAS，再执行exact committed-binding handshake并resume唯一 canonical route；
6. 旧 generation 在 rollback grace 后停止。

新旧 generation 可为受控 rollout/rollback 暂时共存，但同一 shard 的 canonical input 始终只去一个 generation。禁止请求级、错误级、过载级或 endpoint 级 CPU↔CUDA fallback，也禁止一个 Service selector 同时选择两个 profile。

### 4. 模型正确性、模型回滚与服务可用性分开

“模型已经调试好”可以降低错误模型进入生产的概率，但不能消除进程崩溃、机器断电、驱动故障、内存耗尽、网络分区、制品损坏或错误部署。项目因此保留：

- 上线前 immutable bundle、numeric/quality/security/performance 资格；
- 启动时实际 loaded-model readback 与 warmup；
- current/previous exact generation 和管理员发起的 durable rollback；
- stale/late/wrong-generation result fence。

首期不实现复杂的自动模型容灾：不因推理错误自动加载旧模型，不做 runtime hot swap、online shadow、weighted canary 或跨 backend 自动切换。需要回到 previous 时，由人发起明确、可审计的 rollback operation。

服务可用性另由部署 profile 表达：

| Availability profile | 含义 | 可声称 |
|---|---|---|
| `availability-single/v1` | 恰好一个故障域，可按容量配置1..N个域内同profile副本并固定required/min-ready/max-unavailable；适用于开发、验收或明确接受整域中断的生产部署 | 功能/性能可按精确scope资格化；域内多副本只提供容量/有限重试，不得声称 HA，整域丢失时为 inference `HOLD/gap` |
| `availability-ha/v1` | 同一 exact CPU 或 CUDA profile 跨至少两个故障域，static N+1 qualified capacity | 只有 single-replica/failure-domain loss、rolling capacity、网络与 soak 全部通过后可声称 HA |

HA 使用同 profile 等价副本，不是模型或 backend fallback。三机实验可以采用 single profile；没有第二故障域时如实标记非 HA，而不是为了形式引入第二 execution plane。

### 5. CPU 与 CUDA 分别形成资格证据

两种 profile 必须共用相同语义测试集合，但分别运行并保存证据：

- model/feature/label/output/wire golden 与跨 Python/C++ numeric tolerance；
- single-label、multi-label、anomaly/open-set、OOD/abstain、NaN/Inf；
- startup/load/warmup/readback、wrong hardware/config/model/profile；
- batch、queue、deadline、cancellation、retry/dedupe、saturation、24h soak；
- Gateway/Triton/runtime crash、response loss、full-pool unavailable、WAL/gap/recovery；
- steady/peak/rolling 的 throughput、p50/p95/p99、CPU/RSS/RAM、network/copy；CUDA 另测 GPU/VRAM/stream/host-device copy，CPU 另测 core/thread/NUMA/affinity/arena；
- exact previous rollback 与 CPU↔CUDA 新 generation 显式切换。

CPU PASS 不能继承为 CUDA PASS，CUDA PASS 也不能证明 CPU。执行环境缺少所选硬件、驱动、资源或绝对门槛时，该profile必须报告`result=HOLD|NOT_RUN, qualification=NOT_QUALIFIED`，不得把 skip 记为 PASS。只声明CPU的精确部署可以获得CPU-scope资格；产品/release若声称CPU与CUDA均受支持，则两套required matrix都必须PASS。

### 6. 正式 E2E 必须真实启动相关服务

以下名称是`qualification-evidence/v1`四个字段推导出的展示摘要，不是混合wire enum；每条证据仍分别保存level、applicability、result、qualification和runtime/availability/deployment-tier/topology/digest claim scope。运行边界固定为：

| 等级 | 必须真实启动 | 允许的替身 | 不能声称 |
|---|---|---|---|
| Module black-box E2E | 被测模块的发布候选 binary/OCI、实际 runtime 和公开边界；Central Inference 必须真实启动 Gateway、Triton 与所选 ORT profile | 尚未接入邻居的 contract fake、可重复故障注入 | 对应 pairwise/system 已通过 |
| Pairwise E2E | 边界两侧的真实发布候选服务，在干净环境走真实 TLS/UDS/gRPC/HTTP/DB wire | 只允许链外的确定性 fixture；pair 中任一侧不得 fake | 系统或生产已通过 |
| System E2E | BMv2/P4Runtime、Rust Edge、所选 Central Inference stack、Go Control、真实 PostgreSQL、Plugin Host、官方 Analysis Plugin、Web 浏览器链路 | 外部 LLM 可用资格化 deterministic provider fixture；故障注入工具可作为测试设施 | 另一 runtime profile、真实硬件或生产拓扑已通过 |
| Production qualification | exact release/topology/profile 的全部真实服务、目标硬件、HA/PITR/security/performance/soak | 仅明确的外部 provider/test traffic fixture | 其他 digest/profile/topology 已资格化 |

正式 System E2E 中，任何内部 MASI 服务都不能由 fake/mock 替代。Analysis Plugin、MCP/A2A client/server 和 Plugin Host 必须真实启动；为保证断言可重复，外部 LLM provider 可以由合同一致、受限的 deterministic provider fixture 替代，但真实 provider 仍需单独完成 TLS/auth/quota/timeout/redaction smoke，且二者证据不得混写。

首期本地/CI的Module、pairwise与BMv2 System E2E由`e2e-runner-compose/v1`统一编排，冻结Compose/Engine/API、runner image、隔离网络/volume、显式healthcheck、startup/total deadline、资源、evidence和cleanup；不得按机器环境自动切到Podman/Kubernetes/systemd。Compose `running`/`service_healthy`只允许场景继续，Playwright只作浏览器driver，两者都不建立业务PASS。[Docker Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/)、[Playwright webServer](https://playwright.dev/docs/test-webserver)

readiness、端口可连接、单元/契约测试、microbenchmark、fake Gateway、只运行 ORT/Triton、开发服务器或早期 boundary rehearsal 都不是正式 E2E PASS。每次正式运行至少保存：

- 服务清单、binary/image/config/profile/model/contract digest；
- 拓扑、端口、证书和 workload identity；
- startup→ready→drain→stop 时间线与实际进程/container identity；
- PostgreSQL migration、P4 pipeline/P4Info、traffic/model/provider fixture digest；
- 场景、故障注入、原始结构化结果、日志/trace 引用与清理结果；
- CPU/CUDA selected profile 与 observed hardware envelope；
- evidence ID、`level/applicability/result/qualification`、exact scope 和完整 hash manifest。

缺少任一必需服务、绕过公开边界、复用 rehearsal 状态、使用内部 fake 或缺少原始证据时必须为 `HOLD/NOT RUN`，不能 PASS。

### 7. 实现与部署边界

为控制工程复杂度，首期采用两个显式构建/部署 profile，而不是一个包含所有驱动并在运行时猜测的万能镜像：

- CPU artifact：固定 C++ Gateway、CPU-capable Triton/ORT、无不必要 CUDA 依赖，按目标 CPU/NUMA profile 构建和测试；
- CUDA artifact：固定 C++ Gateway、Triton/ORT CUDA、CUDA/cuDNN/driver compatibility 与 GPU 资源；
- 两者实现同一个配置 schema、health/readback API 和 inference contract；共享代码必须通过同一 golden，而构建制品和 SBOM/digest 独立；
- 两种artifact的repository snapshot都必须是NONE模式exact binding closure，显式固定instance group；CUDA artifact还必须固定并读回ORT provider partition/host-side operator placement；
- deployment adapter 只按管理员已提交的 desired profile 启动对应 artifact，不拥有 profile 选择、current binding 或自动回退；
- Kubernetes 可按 profile 使用 CPU request/limit、GPU extended resource、node selector/topology spread；systemd/Podman 采用等价的资源与 affinity 配置。基础设施 Ready 不能替代 loaded model/readback。

OpenVINO CPU plugin 可作为未来性能候选，但不是首期默认或 fallback；只有在独立 numeric、operator、thread/NUMA、性能、供应链和 old/new matrix 通过后，才能新增显式 runtime profile。[OpenVINO CPU device](https://docs.openvino.ai/nightly/openvino-workflow/running-inference/inference-devices-and-modes/cpu-device.html)

### 8. 前端与运维交互

Model Pool 页面必须显示而不能推断：

- desired/selected/observed runtime profile，CPU/CUDA 及不匹配原因；
- exact current/previous pool generation、model/runtime/image/config digest；
- availability profile、failure domain、`replicas/required_replicas/min_ready_replicas`；只有 HA profile 才展示 N+1 与跨 failure-domain 状态；
- load/warmup/readback/numeric/performance/E2E qualification 状态与证据链接；
- CPU core/thread/NUMA/RAM 或 GPU/driver/CUDA/cuDNN/VRAM 资源；
- queue/batch/latency/throughput、WAL/backpressure/gap 与 rollout timeline。

管理员可以选择一个已经登记的 CPU/CUDA profile并发起“创建新 pool generation”或 exact rollback；UI 必须展示影响范围、预计中断、`level/applicability/result/qualification`、exact scope 和不可变 digest。浏览器不能发送“auto”“best available”或“fallback”选择，也不能直接调用 Triton load/unload、Kubernetes API 或修改 worker current。

## 成熟方案采用矩阵

| 候选 | 结论 | 项目边界 |
|---|---|---|
| ONNX Runtime CPU EP | `ADOPT CPU profile` | exact CPU/thread/NUMA/arena/model numeric/performance；不与 CUDA 自动切换 |
| ONNX Runtime CUDA EP | `ADOPT CUDA profile` | exact ORT/CUDA/cuDNN/driver/GPU/VRAM/I/O Binding；不与 CPU 自动切换 |
| NVIDIA Triton | `ADOPT execution only` | startup-only/read-only、CPU/GPU instance profile、dynamic batching；不拥有 current/route/model-control |
| Triton CPU-only build | `ADOPT when CPU artifact requires` | 独立 pinned build/SBOM；不得运行期下载或自动换镜像 |
| TensorRT | `CONDITIONAL explicit future profile` | 目标 GPU numeric/performance/supply-chain 通过后新增 generation；绝非错误 fallback |
| OpenVINO CPU | `CONDITIONAL future profile` | 只有独立资格证明收益后采用；首期不与 ORT CPU 并存 |
| Kubernetes GPU/CPU scheduling | `CONDITIONAL infrastructure adapter` | 资源与 placement；不拥有 desired/current/profile selection/readback |
| KServe/Ray Serve/MLflow serving | `REJECT first-release control plane` | 会复制 route、autoscale、model lifecycle/current 所有权 |

## 取舍

收益：无 CUDA 机器可以用同一中央架构真实运行；高核心数 CPU 可获得独立优化；GPU 环境仍保留集中 batch 效率；没有运行时自动分支，故障和数值语义可审计；真实服务 E2E 阻止“mock 全绿、实际起不来”。

代价：从一个 runtime profile 变为两个，构建、SBOM、numeric、性能和故障证据接近增加一套；CPU 与 CUDA 不能互相继承容量结论；正式 E2E 需要可重复的多服务环境、证书、数据库和浏览器编排，CI 时间和资源成本上升。

复杂度控制点：不增加业务模块、不恢复 Edge-local inference、不做自动硬件选择、不做 runtime hot swap、不做在线 shadow/weighted split、不同时引入 OpenVINO/TensorRT、不要求 single profile 冒充 HA。

## 被拒绝的方案

1. **CUDA-only**：无 GPU 环境无法完成真实功能/E2E，兼容性不足。
2. **启动时自动选“最好设备”**：相同配置可能在不同机器产生不同 backend，无法复现或审计。
3. **请求失败时 CPU↔CUDA 自动 fallback**：错误与延迟语义改变，可能在过载时放大资源耗尽。
4. **同一 generation 混合 CPU/CUDA worker**：load balancer 会让结果/时延随 replica 漂移。
5. **把模型容灾理解为自动加载旧模型**：错误模型、旧 taxonomy/feature 与 effect eligibility 可能被静默恢复。
6. **为开发环境强制双故障域 GPU**：成本与目标不符；应明确使用 `availability-single/v1` 并不声称 HA。
7. **只用 fake/mock E2E**：不能验证制品启动、runtime/driver、证书、DB/P4 和真实跨进程边界。
8. **服务全 Ready 即 PASS**：健康端点不证明检测、Event、审批、P4 write/readback 或浏览器行为。
9. **NONE仓库中保留多个候选模型**：Triton会在startup加载仓库中的全部模型，扩大资源和选择面；必须使用exact closure。
10. **把CUDA profile中的运行期CPU接管当容灾**：ORT provider partition已改变，数值/容量profile失效；只允许预先声明和资格化的host-side placement。

## 迁移与回滚

1. 将原 `model-runtime-central-gpu/v1` 历史引用替换为 CPU/CUDA 两个现行 profile；历史 evidence 保留原 ID 和 v1.13 level，不重命名。
2. contracts 增加 `runtime_profile_id`、selected/observed envelope、availability profile 与 stable mismatch errors。
3. 构建独立 CPU/CUDA image、SBOM 和 startup self-test；同一模型分别产生 numeric/performance evidence。
4. 先以 fake 邻居完成各模块 black-box；所有模块 Module Complete 后，在干净环境启动真实 pair 和完整系统重跑。
5. CPU↔CUDA 改变通过新 pool generation；回滚只选择 exact qualified previous generation，不修改运行中 worker。

若 v1.17 实现需要回滚代码，系统只能退回仍能读取现行 profile/pool/qualification 持久事实的版本。不能安全读取时保持 inference `HOLD`；不得重新启用 Edge-local inference或运行时自动 fallback。历史 v1.13/v1.14 制品只有 exact digest/profile/evidence 仍受支持且能读取v1.17字段时才可作为显式 previous generation，而不是隐式默认。

## 验证

- contracts/profile：unknown/absent profile、selected/observed mismatch、CPU/CUDA old-new、same-generation mixed-profile negative；
- startup：CPU-only host、qualified CUDA host、CUDA selected without GPU、driver/cuDNN drift、CPU feature/NUMA/RAM不足、model/repository/config/image digest drift、NONE repository额外model/version/config/backend、implicit instance group、ORT provider partition/host-side placement漂移；
- numeric：Python↔ORT CPU↔ORT CUDA class/order/tolerance/OOD/NaN/Inf；超出 accepted tolerance时 profile不得共享同一 compatibility claim；
- performance：两 profile独立 steady/peak/saturation/24h soak，冻结绝对 p99/throughput/resource；只测 `Run()` 或 Triton latency不得替代packet/window→Event；
- availability：single profile中断语义；HA profile同 exact profile跨域N+1、replica/domain loss、rolling capacity；
- rollout：CPU→CUDA、CUDA→CPU、新模型同profile、exact previous rollback、严格`route-withdraw→drain/WAL→CAS→commit handshake→resume`、late result fence；在withdraw前CAS或CAS后未handshake即发送canonical input的负例必须拒绝；
- real E2E：每个Module真实启动；每对服务在干净环境真实启动；system链路真实启动BMv2/P4Runtime、Edge、所选推理stack、Go、PostgreSQL、Host、Analysis和Web；内部fake负例阻断PASS；
- evidence：CPU/CUDA独立 evidence ID，正交level/applicability/result/qualification、deployment/availability claim scope、服务清单/start-stop timeline、profile/hardware、raw artifacts和hash manifest完整；无硬件或无绝对门槛为`HOLD/NOT RUN`。
- runner：`e2e-runner-compose/v1`的exact版本、health/deadline/resource/evidence/cleanup通过；缺runner/backend、healthy但业务失败、Playwright URL可达但链路错误均不能PASS，且不得自动换runner。

当前仓库处于初始化阶段。本 ADR Accepted 只表示需求边界已确认；在上述实现和证据实际形成前，CPU、CUDA、E2E、HA 与 production claim均为`applicability=APPLICABLE, result=NOT_RUN, qualification=NOT_QUALIFIED`（若前置profile尚未冻结则result为`HOLD`）。

## 参考

- ONNX Runtime execution providers：<https://onnxruntime.ai/docs/execution-providers/>
- ONNX Runtime CPU threading/NUMA：<https://onnxruntime.ai/docs/performance/tune-performance/threading.html>
- ONNX Runtime CUDA EP：<https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html>
- ONNX Runtime I/O Binding：<https://onnxruntime.ai/docs/performance/tune-performance/iobinding.html>
- Triton model configuration/instance group：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_configuration.html>
- Triton build customization：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/build.html>
- Triton model management：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html>
- Triton secure deployment：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/deploy.html>
- Triton optimization：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/optimization.html>
- OpenVINO CPU device：<https://docs.openvino.ai/nightly/openvino-workflow/running-inference/inference-devices-and-modes/cpu-device.html>
- gRPC performance/deadline/retry：<https://grpc.io/docs/guides/performance/>、<https://grpc.io/docs/guides/deadlines/>、<https://grpc.io/docs/guides/retry/>
- Kubernetes probes/GPU/topology/disruptions：<https://kubernetes.io/docs/concepts/workloads/pods/probes/>、<https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/>、<https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/>、<https://kubernetes.io/docs/concepts/workloads/pods/disruptions/>
- 唯一需求基线：`../masi-nids-vnext-system-requirements-2026-08-09.md`
- v1.13 历史 Central GPU 决策：`0016-central-gpu-inference-pool-and-routing-boundary.md`
