# MASI-NIDS vNext 在线检测模型模块化独立评估

- 评估日期：2026-08-10
- 需求基线：原评估 `vNext-requirements-1.10`（模型切换结论形成于 v1.8）；当前基线 `vNext-requirements-1.17`
- 性质：成熟方案调研与架构取舍，不替代需求或 ADR
- 状态：Historical/Partially Superseded；模型模块化、immutable bundle、语义合同与exact binding判断仍有效；本机C++/SPSC/per-shard restart、拒绝Triton serving和CUDA-only结论已由ADR-0016历史及ADR-0017现行决策替代；实现与证据仍为`HOLD/NOT RUN`
- `document_status`: `historical_partially_superseded`
- `current_normative_source`: `../masi-nids-vnext-system-requirements-2026-08-09.md@vNext-requirements-1.17`、ADR-0017、ADR-0006
- `implementation_authority`: `current_normative_source_only`
- `qualification_authority`: `current_normative_source_only`
- `retained_invariants`: 模型数据/合同模块化、immutable bundle与exact binding；仅因现行需求再次规定而有效

> **历史文档：不可作为实现或PASS依据。** 本文记录v1.10以前“每Edge本机单Session”的评估过程，不再定义首期部署、传输或资格。现行首期采用C++ Gateway + pinned Triton + 启动前显式ORT CPU/CUDA profile、batched-unary mTLS gRPC与startup-bound pool generation；HA仅允许同profile跨域副本，不存在Edge-local或CPU↔CUDA/异模型自动fallback。实现与验收只读[ADR-0017](../adr/0017-central-inference-runtime-selection-and-real-e2e.md)、[ADR-0006](../adr/0006-qualification-levels-and-evidence.md)和v1.17需求。

## v1.10 历史结论

检测模型应当支持拆卸替换，但“可替换模型”不等于“模型是通用插件”。推荐边界是：

```text
稳定 C++ inference engine
+ immutable model bundle
+ versioned feature schema / label taxonomy / output adapter
+ Go/PostgreSQL exact model binding
```

在 feature/output/runtime compatible major 不变时，可以替换不同算法、权重、scaler、阈值或扩展类别，Edge/Go 业务代码不变；当模型需要新特征、改变 tensor layout/单位/窗口、引入不同 backend/custom operator 或改变 single/multi-label/anomaly 语义时，必须升级合同和 reader，不能声称是无风险热替换。

首期应在部署前选择 exact bundle，由 C++ 启动时创建单一Session，再由Edge以唯一`shard_routing_epoch`执行route-withdraw/drain/WAL-bounded-buffer/restart/readback/CAS/commit/resume；回滚同样逐shard启动exact previous。PITR/restore通过不可复用model-control incarnation阻止旧进程ABA。生产进程内candidate、online shadow、load/unload和双Session不属于首期；若启动间隙实测违反冻结SLO，先评估进程级blue-green，而不是直接把热插拔复杂度放入C++。

v1.10 没有改变上述模型生命周期结论，而是把模型输入如何实时取得补成独立边界：ADR-0013 规定 P4 aggregate-first、Edge event-time final window、固定布局 SPSC request/result ring、input/result WAL 与 PostgreSQL Event commit 后 canonical ACK。模型 bundle 只能声明其 feature/input/output contract，不能私自选择抓包 backend、修改 hot-path native layout或推进 source cursor。

## 当前文档能力与原缺口

v1.5 的需求/设计已经定义 C++ 独立进程、ONNX Runtime 默认、model/scaler hash、feature/label contract、class map、numeric golden、Offline ML qualification 和 rollback compatibility，说明规划方向具备模块化基础；v1.6 又补齐 exact binding 与多类别语义。它们都不是仓库已有实现、测试 PASS 或运行时资格证据。

原缺口是：没有不可变 model revision 与 canonical binding owner；没有 label taxonomy/class order、single/multi-label、unknown/OOD/abstain；没有 output adapter；没有 exact startup/readback/fence；没有模型级资源 profile、per-shard current/previous 或 rolling restart/rollback fault matrix。因此只能证明“可以加载一个固定模型”，不能证明“可以生产级替换更多类别或其他模型”。

这些缺口现由 `ARCH-MODEL-001`、`CONTRACT-MODEL-001`、`FUNC-INF-MODEL-001`、`DB-MODEL-001`、`PERF-INF-001`、`REL-INF-001`、`DEP-INF-001`、`OBS-INF-001`、`TEST-INF-001`、`MIG-INF-001`、ADR-0010 的模块化边界和 ADR-0011 的启动/滚动机制收敛；与实时输入、ABI、WAL/ACK 的交界由 `ARCH-TELEMETRY-001`、`CONTRACT-TELEMETRY-001`、`CONTRACT-INFERENCE-001` 和 ADR-0013 收敛。

## 官方成熟方案核验

### ONNX 与 ONNX Runtime

[ONNX IR Specification](https://onnx.ai/onnx/repo-docs/IR.html) 的 `ModelProto` 包含 `model_version`、producer/domain、graph 和 `metadata_props`；metadata key 要求唯一。项目可以用 reverse-DNS metadata 记录 feature/label/adapter/profile 引用。

[ONNX Runtime C++ ModelMetadata](https://onnxruntime.ai/docs/api/c/struct_ort_1_1_model_metadata.html) 可以读取 version、producer、graph 和 custom metadata；[`Ort::Session`](https://onnxruntime.ai/docs/api/c/struct_ort_1_1_session.html) 从模型文件或内存构造 Session。C++ 应在 startup、Session 创建前后交叉验证实际模型的 input/output name、dtype、shape、opset 和 metadata。

[ONNX Runtime graph optimization](https://onnxruntime.ai/docs/performance/model-optimizations/graph-optimizations.html)说明online optimization在Session创建时执行并增加启动成本；offline mode可预先保存优化模型，但某些优化依赖execution provider/options/hardware。[Thread management](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)显示intra/inter-op、execution mode、spinning、affinity/NUMA与global/per-session pool都会改变性能与资源。项目应在exact profile中二选一online/offline mode并冻结artifact/tool/ORT/EP/device/CPU feature及thread/arena配置；不兼容时startup失败，不能silent fallback。

独立判断：metadata 是模型作者可写内容，只适合作自描述/交叉检查；它不能替代外部 strict manifest、SHA-256、producer/provenance、qualification 或 Go active binding。

采用结论：ONNX + ONNX Runtime C++ 为首期 `ADOPT` default；固定 exact runtime/execution-provider/device/opset/operator/optimization profile，每进程一个 Session，不按本机可用性自动 fallback。

### MLflow Signatures 与 Model Registry

[MLflow Model Signatures](https://mlflow.org/docs/latest/ml/model/signatures/) 支持 column/tensor inputs/outputs/params，tensor signature 明确 dtype/shape，并可用于 serving input validation。

[MLflow Model Registry workflow](https://mlflow.org/docs/latest/ml/model-registry/workflow) 提供 immutable model version、tags 和可重指 alias，例如 `champion` 可以从一个 version 改到另一个 version。

独立判断：signature、immutable version、qualification tag 和 promotion/rollback 思路值得借鉴；mutable alias 适合操作者工作流，不适合成为 NIDS runtime identity。生产必须在发布阶段把 alias 解析成 exact bundle digest，并由 Go/PostgreSQL 保存 binding。

采用结论：MLflow 为 `CONDITIONAL` offline experiment/registry/evidence；不作为 C++ 在线依赖、Go binding owner、REST serving 或第二生产数据库。

### NVIDIA Triton

[Triton Model Repository](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_repository.html) 使用 `<model-name>/<numeric-version>/` 布局，version policy 决定可服务版本；[Model Management](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html) 区分 `NONE`、`EXPLICIT` 和 `POLL`，其中默认 `NONE` 在启动时加载并忽略运行期 repository变化。

独立判断：version directory、显式 version policy 和 startup-only `NONE` 都是成熟模式；动态 load/unload 只是可选能力，不是生产模型“可替换”的必要条件。引入 Triton会新增 serving process、repository state、network/control API 和第二模型运行控制面，而当前 C++ + shared-memory 热路径没有容量证据需要它。

采用结论：首期拒绝 Triton 作为 serving/control plane，只借鉴不可变版本目录、startup-only、loaded readback 和 backend qualification。未来只有多模型/GPU batching/ensemble 的实测收益超过额外故障面时才重新评估。

### TensorFlow Serving、Kubernetes 与 KServe

[TensorFlow Serving architecture](https://www.tensorflow.org/tfx/serving/architecture) 把模型更新区分为 availability-preserving（先加载新模型，资源更高）和 resource-preserving（先卸载旧模型，可能有间隙）；[serving config](https://www.tensorflow.org/tfx/serving/serving_config) 支持 specific version policy 和可变 version labels。独立判断：这说明“零间隙”和“单份资源”不能同时免费获得。MASI 首期选择 resource-preserving 的单 Session，再用 per-shard rolling 与 Edge bounded buffer限制影响；version label 不成为 runtime identity。

[Kubernetes Deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)提供逐实例替换，但terminating Pod可继续消耗资源，实际峰值可超过`replicas+maxSurge`；[startup/readiness/liveness probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/)区分启动、摘流量与进程失活；[Pod lifecycle/hooks](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)说明grace到期会SIGKILL且hook可能重复；[PDB/disruptions](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)明确workload自身rolling update不受PDB限制。独立判断：这些是可复用的process orchestration，不证明exact model、drain、capacity或current。Edge仍是唯一canonical route owner，Go/PostgreSQL仍是binding owner。

[KServe Canary Rollout](https://kserve.github.io/website/docs/next/model-serving/generative-inference/llmisvc/canary-rollout) 展示 side-by-side revision、Ready 后按 weight 切流、逐步提升、保留旧 revision 和 rollback。该页面针对 `LLMInferenceService`，这里只借鉴 rollout pattern，不据此断言所有 KServe predictor/runtime 具备同一能力或已通过 MASI-NIDS 资格；文档还提示 weight 0 的 dark deployment 可能未预热 backend，后续切流会产生瞬态问题。

独立判断：Ready、side-by-side、逐步验证和 last-good rollback 是未来 blue-green 可借鉴模式；通用 HTTP weighted traffic 的语义不直接适合 NIDS 窗口。只把部分流量送 candidate 会降低当前检测覆盖，双跑后同时写 Event 又会增加 duplicate/policy/幂等复杂度。

采用结论：首期不采用 production online shadow 或 weighted canary；比较在 frozen replay/隔离 rehearsal 完成。未来 blue-green/canary必须固定进程/流量 ownership、双跑/单跑、coverage、identity、policy eligibility、停止阈值和 rollback，并创建新 profile/需求基线。

## 支持更多流量类别的方法

类别扩展不能只修改一个字符串数组。model bundle 必须包含 versioned label taxonomy：

- 稳定、永不复用的 label ID；
- display name、父子/别名/弃用与 merge/split mapping；
- class order/axis 与 score domain；
- single-label、multi-label、anomaly-score/open-set mode；
- threshold/calibration、top-k、unknown/OOD/abstain；
- output adapter version/digest。

如果只增加 label，旧 reader 能保留 unknown ID、不会误映射、不会触发旧 policy，可作为 compatible minor；重用 ID、改变已有类别含义、score domain 或 single↔multi-label 必须升 major。

新类别默认只能产生带 exact model/taxonomy identity 的检测事实。自动处置 policy 必须重新明确绑定 model/feature/label/adapter/profile 与 label ID，并独立资格化；模型扩类不能顺带扩大 P4 下发权限。

## 支持其他模型的方法

按兼容层次处理：

1. 同 feature/output/runtime profile 的不同 ONNX 模型：作为新 immutable revision完成离线资格后，改变 exact startup envelope并滚动重启；
2. 相同输入但不同 raw output：新增受测 output adapter revision，保持 canonical `InferenceResult`；
3. 需要新特征：升级feature/wire contract，先部署能解析old/new schema的reader/producer image，再由Edge按独立endpoint逐shard单路切换model binding；这不表示同一C++进程双Session；
4. TensorRT：独立 GPU/compute-capability/plan/plugin/numeric/resource profile，通过后条件使用；
5. LibTorch：只有 ONNX Runtime 无法保持已接受数值/算子合同才条件评估；
6. ensemble/多模型并行：首期应打包为一个声明清晰、可资格化的 bundle/adapter；若需要独立路由和多个 canonical active detector，必须提升需求基线，避免暗中创建第二结果/策略状态机。

## 高性能、稳定性与兼容性影响

- 高性能：steady state只有一个Session，model rollout与ADR-0013固定的hot ring分离；optimization/thread/arena、cache、startup stages、Edge WAL pending和Session/CPU/GPU/VRAM都有硬上限。固定分片分别证明未重启shard服务能力、重启期间buffer容量和恢复后的backlog replay，并计入warming/terminating峰值；未证明跨shard重路由前不能用总容量掩盖单shard缺口。相对回归门槛不能替代冻结的绝对capacity/p99/buffer门槛。
- 稳定性：startup envelope不改current；Edge先撤route/drain，deployment action幂等；实际loaded必须readback，per-shard CAS后经commit handshake才恢复。response loss沿原operation reconcile，restart有budget/quarantine；PITR先轮换incarnation；只以新rolling operation回滚exact qualified previous。
- 兼容性：feature/label/adapter/wire/runtime/optimization分别版本化；每个Event保留incarnation、shard/route与实际binding generation；旧新window不拼接。major change让image兼容old/new schema，再按不同endpoint逐shard单路切换，不要求同一C++进程双Session。
- 安全：bundle 是数据制品，不是 code loader；custom native op/extension 进入 inference image供应链；production 不解析 mutable tag/alias、不在线下载模型。
- 权限：一个 scoped Platform Admin 或版本化自动发布 policy 可以激活已完全资格化的 exact revision；不新增 legacy model/release 多域签名或通用审批链。放宽质量/语义/policy 仍由 Owner baseline/profile 控制。

## v1.10 历史最终建议

采用ADR-0010保留的专用Model Manager + fixed C++ engine + immutable bundle边界，并以ADR-0011的startup-bound single-session/Edge-single-route per-shard rolling restart作为首期切换机制；实时输入和结果确认遵守ADR-0013。把incarnation、wire/optimization、deployment action、termination、WAL buffer、commit和capacity写进既有模块合同，不新增serving/router/queue。它实现“模型可拆卸替换”，同时删除当前没有必要的进程内slot/shadow状态机。

不建议把模型纳入通用插件平台，也不建议首期直接部署 Triton/KServe/MLflow/TF Serving。它们解决的是通用模型服务/路由/实验管理问题，而本项目首期需要的是本地低延迟、代际一致、可读回、不会扩大 P4 权限的在线检测模型切换。若启动方案在目标 SLO 下被证伪，优先增加进程级 blue-green，而非进程内热插拔。

## 来源可复现性

本页链接记录 2026-08-10 的官方资料点验结果。含 `latest`、`next` 或持续更新页面的 URL 只支持本次设计判断，不构成生产资格证据；实现阶段必须在 `contracts/profiles/v1`/`contracts/supply-chain/v1` 保存访问日期、页面标题、上游 release/commit（可取得时）、内容 snapshot/digest、实际 artifact/runtime digest 与 qualification evidence。无法冻结时保持 `HOLD`。
