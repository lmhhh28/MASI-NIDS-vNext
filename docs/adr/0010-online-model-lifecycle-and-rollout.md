# ADR-0010：在线检测模型模块化、绑定与安全切换

- 状态：Partially Superseded；模型部署与切换机制先由ADR-0011收敛，v1.13 central-GPU历史由ADR-0016保留，现行Central Inference CPU/CUDA启动选择、路由、启动绑定、人工回滚、资格范围和真实服务E2E由ADR-0017/0006、`vNext-requirements-1.17`、`DEC-033`至`DEC-038`定义；在线遥测与推理传输由ADR-0013定义
- 日期：2026-08-10
- 决策者：Owner
- 需求基线：原始决策 `vNext-requirements-1.6`；当前适用边界 `vNext-requirements-1.17`
- `document_status`: `historical_partially_superseded`
- `current_normative_source`: `../masi-nids-vnext-system-requirements-2026-08-09.md@vNext-requirements-1.17`、ADR-0017、ADR-0006
- `implementation_authority`: `current_normative_source_only`
- `qualification_authority`: `current_normative_source_only`
- `retained_invariants`: immutable bundle、版本化feature/label/output合同、Go/PostgreSQL exact binding、供应链与effect安全边界；仅因现行需求再次规定而有效
- 关联需求：`CORE-MODEL-001`、`ARCH-MODEL-001`、`ARCH-TELEMETRY-001`、`MOD-INF-001`、`MOD-CTRL-001`、`MOD-ML-001`、`CONTRACT-MODEL-001`、`CONTRACT-PROFILE-001`、`CONTRACT-TELEMETRY-001`、`CONTRACT-INFERENCE-001`、`FUNC-INF-001`、`FUNC-INF-MODEL-001`、`DB-MODEL-001`、`PERF-INF-001`、`PERF-TEL-INF-001`、`REL-INF-001`、`REL-INF-POOL-001`、`REL-TEL-INF-001`、`DEP-INF-001`、`OBS-INF-001`、`TEST-INF-001`、`TEST-TEL-INF-001`、`MIG-INF-001`、`DEC-002`、`DEC-026`、`DEC-030`、`DEC-033`、`DEC-034`

> **历史文档：不可作为实现或PASS依据。** 本文只保留上列不变量和决策追溯。第4节起的candidate/shadow、进程内`PrepareModel/ActivateAtBoundary`、本机多Session、per-shard本机重启和旧runtime/transport取舍均为历史。现行首期只有一个Central Inference架构：Edge把final input通过有界batched-unary gRPC/mTLS发送到无状态C++ Gateway，由固定Triton和管理员启动前显式选择的ORT CPU或CUDA profile执行；模型与runtime只在pool启动/滚动前绑定，不运行期reload，不提供Edge-local、CPU↔CUDA或异模型自动回退。实现与资格只读[ADR-0017](0017-central-inference-runtime-selection-and-real-e2e.md)、[ADR-0006](0006-qualification-levels-and-evidence.md)、[ADR-0013](0013-online-telemetry-and-inference-hot-path.md)及v1.17需求。

## 背景

vNext 已把 packet/telemetry、C++ inference、Go facts 和 Offline ML 分开，但“模型带 ID 和 hash”不足以证明模型可以安全替换。若没有 feature schema、label taxonomy、output adapter、active binding、切换 readback 和 previous rollback，新增攻击类别可能改变 class order，运行时可能按 mutable tag 加载错误 bytes，旧结果也可能在切换后覆盖新事实。

成熟方案提供了可借鉴的积木：

- [ONNX IR](https://onnx.ai/onnx/repo-docs/IR.html) 定义 `model_version` 与 `metadata_props`；[ONNX Runtime ModelMetadata](https://onnxruntime.ai/docs/api/c/struct_ort_1_1_model_metadata.html) 可以在 C++ 中读取模型 metadata；
- [MLflow Model Signatures](https://mlflow.org/docs/latest/ml/model/signatures/) 展示 dtype/shape 的输入输出合同，[Model Registry workflow](https://mlflow.org/docs/latest/ml/model-registry/workflow) 展示 immutable version、tag 和 alias promotion；
- [Triton model repository](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_repository.html) 使用 model/version 目录与 version policy；[KServe canary rollout](https://kserve.github.io/website/docs/next/model-serving/generative-inference/llmisvc/canary-rollout) 展示 side-by-side、Ready、逐步切流和 rollback。该 KServe 页面特指 `LLMInferenceService`，本文只借鉴 rollout pattern，不据此断言所有 KServe predictor/runtime 具有相同语义或已获项目资格。

这些上游能力不能直接成为MASI-NIDS的生产模型事实源。MLflow alias、Triton repository state或KServe route都是可变外部控制状态。v1.15确立并由现行v1.17继续采用Triton作为central module内部固定执行器，同时使用显式ORT CPU/CUDA启动profile；Gateway、Edge和Go/PostgreSQL仍保留身份、路由与current所有权。下文“本地Rust↔C++”评价只记录原始背景。

## 决策

### 1. 三层可替换边界

在线检测能力拆为三个独立版本层：

1. **Inference engine/runtime**：`infer-cpp/`的稳定C++ Gateway、固定Triton execution server、启动前显式绑定且分别资格化的ONNX Runtime CPU或CUDA profile；其他backend必须新profile；
2. **Model bundle**：不可变、digest-pinned 的模型/scaler/config/threshold/OOD 数据制品；
3. **Semantic contracts**：feature schema、label taxonomy、output adapter 和 canonical `InferenceResult`。

模型 revision 不是 plugin revision。Plugin Manager/Runtime Host 不拥有在线模型 catalog、binding 或执行；model bundle 不允许携带动态 library、Python/Wasm hook、custom native operator 或其他可执行扩展。需要 custom op/TensorRT plugin/LibTorch extension 时，它属于 inference image/runtime profile，必须独立供应链登记和资格化。

同一 feature/output/runtime compatible major 下，模型算法、权重、scaler、threshold 或 label 集合可以作为新 bundle 替换，不修改 Rust Edge/Go Event 业务代码。feature layout/单位/窗口、ADR-0013 定义的 inference wire contract、output semantics 或 backend/custom-op major 改变时，必须滚动升级 producer/reader，不能叫作 drop-in hot swap。

### 2. Immutable Model Bundle

`contracts/model/v1` manifest 至少绑定：

- `model_id`、immutable revision、bundle/manifest/artifact URI/size/digest；
- model/scaler/pre/post config、feature schema、label taxonomy、output adapter、threshold/calibration/OOD policy digest；
- ONNX IR/opset、runtime/backend/execution provider/device/resource profile；
- training/evaluation data、seed、toolchain/config、export、numeric/quality/performance/rollback evidence；
- producer/build identity、SBOM/provenance、offline verification metadata；
- supported inference contract/current-previous reader matrix 和停止支持日期；
- ADR-0006 公共 evidence envelope 的 `level/applicability/result/qualification`、exact claim scope、requirement/test mapping、environment/profile/source/artifact/image digest、原始结果、命令、时间和 reviewer/Owner reference。

ONNX metadata 必须与 manifest 交叉验证 input/output name、dtype、shape、model version 和项目 reverse-DNS keys，但 metadata 是模型生产者可写声明，不能替代 external schema、artifact digest、publisher/provenance、qualification 或 active binding。

生产 binding 只保存 exact resolved digest。tag、MLflow alias/stage、文件路径、mtime、Triton version policy 和 display name只能用于发现，不能在 runtime 解析为 current。

### 3. Feature、Label 与 Output Adapter

feature schema 固定字段 ID/顺序、dtype/shape、单位/尺度、窗口、缺失/default、normalization/scaler、NaN/Inf/OOD 和 producer compatibility。只有 old/new matrix 证明旧 producer/reader 能保持语义时才允许 compatible minor；改变单位、窗口、class axis 或缺失语义必须升 major。

label taxonomy 固定永不复用的 label ID、版本/digest、父子/别名/弃用以及增删/合并/拆分映射，并显式声明：

- `single_label`、`multi_label`、`anomaly_score` 或 `open_set` mode；
- class order/axis、score domain、threshold、calibration、top-k；
- `unknown`、OOD、abstain 和低质量语义；
- display name 与稳定 ID 分离。

output adapter 是 C++ engine 中已资格化的确定性实现或受限数据配置，用 exact adapter ID/version/digest 把 raw tensor 映射为 canonical `Prediction[]/decision/quality`。bundle 不能提供任意 executable adapter。

增加 label 只有在旧 reader 能保留 unknown ID、不会错误映射为已有类别、不会触发旧 policy 时才可作为 minor。重用 label ID、改变已有含义/score domain、single-label↔multi-label 或 anomaly↔classification 转换必须升 major。

新 label、模型或更高 confidence 默认没有自动 effect eligibility。确定性 policy 必须显式绑定允许的 model/feature/label/adapter/profile digest 和 label ID；OOD、abstain、unknown/新增 label、shadow 或低质量结果不能创建 Proposal/Decision/Intent 或 P4 effect。

### 4. 控制事实与所有权（历史 v1.6 切换机制，已由 ADR-0011 替代）

Go Control 内的 Model Manager 是 `model_platform` 唯一业务写入者，持有 immutable revision、qualification、activation operation、desired/current/previous/candidate binding、candidate shadow mode 和 audit。C++、Rust Edge、Offline ML、MLflow/Triton/KServe、OCI registry 和部署平台均不连接该 schema。

C++ 只执行以下低频 model-control 边界：prepare、read prepared state、activate at batch boundary、read active state、abort、rollback prepare。它不自行选择 tag/latest、不把进程 pointer 当 canonical fact，也不直接写 PostgreSQL。

状态必须按 namespace 分开：

- revision：`registered|qualified|rejected|revoked`；
- activation operation：`requested|rollback_requested|preparing|prepared|activating|activation_unconfirmed|active|rolled_back|aborted|failed`，并另带 `kind=activate|rollback`；
- binding slot role：`current|previous|candidate`；candidate run mode：`prepared_only|shadowing`；
- inference result：`canonical|shadow|fenced|invalid`。

这些 enum 不与 effect `unknown`、plugin lifecycle、rule observation quality 或 qualification `HOLD` 共用通用 `status`。`desired` 是 Go operation projection，`prepared` 是 per-node/token state，`observed` 是 C++ readback，`current` 是 PostgreSQL finalized binding，四者也不是同一个 enum。

合法主路径必须由 `contracts/model/v1` 机器编码：activate 为 `requested→preparing→prepared→activating→active`，rollback 是引用 exact current/previous 的新 operation，路径为 `rollback_requested→preparing→prepared→activating→rolled_back`。`activating` 可在响应丢失时进入 `activation_unconfirmed`，但只能经 exact readback 收敛到相应成功状态或 `failed`；`requested|rollback_requested|preparing|prepared` 可转 `aborted`。`active|rolled_back|aborted|failed` 为 terminal，不能重开或原地改 kind/digest。

### 5. 唯一切换顺序（历史 v1.6，禁止用于当前实现/PASS）

```text
qualified immutable revision
→ durable activation operation
→ freeze required-node cohort and node runtime epochs
→ deployment pre-stage exact bundle to read-only local cache
→ transaction-free per-node C++ PrepareModel/load/warmup/validate
→ each prepare token binds schema/version + operation + node/runtime epoch + current + candidate + contracts + runtime/resources + cohort + proposed generation + issued/expiry + trace
→ short PostgreSQL CAS stores desired binding generation
→ transaction-free per-node ActivateAtBoundary(operation, generation)
→ required-node C++ GetActiveModel exact readback
→ PostgreSQL CAS finalizes observed current and previous
```

任何 artifact fetch、load/compile/warmup、C++ RPC 或 readiness 等待期间，PostgreSQL active transaction/held connection 必须为零。prepare 不改变 current；只有 immutable cohort 中全部 required node 的 actual active readback 与 operation/cohort/node-runtime-epoch/generation/digest 一致后，Go 才把 candidate finalize 为 logical current。旧 node、cohort 外 node、过期 token 或 partial readback 不能投票。

切换发生在明确 batch boundary。切换前 admitted batch 保留旧 generation，切换后 batch 使用新 generation；一个 batch/window 不能混合。Edge、C++、Go 和 Event 都携带 model/binding generation，old/shadow late result 被 fence。

activate 响应丢失时，Go 沿原 operation 调用 `GetActiveModel` reconcile，不能盲目重新切换。多节点 partial activation 时 PostgreSQL current 保持旧 binding；已切换 node 暂停 canonical ingest并沿原 operation回切 exact current 或等待全 cohort 收敛。任一 observed state 与 PostgreSQL current 不一致时保留有界 pending input并告警，直到 readback/CAS 收敛，禁止把 partial rollout 宣称 active。

### 6. Shadow、首期无 Weighted Canary（历史 v1.6，生产 shadow 已取消）

candidate 可 side-by-side prepare 并对有界复制输入执行 shadow。shadow 必须：

- `canonical=false/effect_eligible=false`；
- 不推进 source cursor/ACK，不写 canonical Event/Incident，不进入 policy/effect；
- 仅写有界 comparison evidence，并绑定同一 input/window/current/candidate/contract/coverage；幂等键固定为 `(scope,target_group,input/window digest,current generation,candidate generation,comparison profile digest)`，另存 payload digest，same-key/different-payload 稳定冲突；
- 分开报告 numeric、taxonomy、decision、quality、latency 和 error difference；
- 达到核心 high watermark 时首先停止，失败只形成 gap/reason，不阻塞 current。

首期不采用 weighted canary 产生 canonical Event。NIDS 若把一部分窗口只送 candidate，可能降低当前检测覆盖；若双跑又把 candidate 输出纳入业务，会引入 duplicate Event/policy/幂等问题。未来若需要 canary，必须新增 profile，固定 deterministic cohort、双跑/单跑、coverage、identity、policy eligibility、停止阈值和 rollback，并提升需求基线。

### 7. 回滚与故障语义（历史 v1.6 多 slot 机制，已替代）

同一 `(inference_scope, target_group)` 至多一个 observed current、一个 exact previous 和一个 bounded candidate slot；shadowing 是 candidate mode，不增加第四个 slot。previous 只有仍 qualified、未 revoked、artifact 可用且通过当前 feature/label/adapter/runtime/reader matrix时可回滚。

candidate prepare/quality/resource 失败时旧 current 继续。current 故障时创建引用 exact current/previous 的新 rollback operation，并在每个 CAS/activate 前重新读取 qualification、revocation、artifact availability 与 compatibility digests；previous 不可用时停止对应 scope并 `HOLD/unavailable`，禁止加载开发默认模型、随机 cache、mutable alias 或未经证据的更旧 revision。

回滚不重放旧输入、不改写历史 Event、不恢复旧 policy eligibility，也不删除 candidate、qualification、comparison 或 activation audit。PostgreSQL failover/PITR、C++/Edge/Go restart 后必须以 canonical binding + C++ actual readback重验，不能依赖进程内 cache 猜 active。

### 8. 性能与运行后端（历史 v1.6 多 Session 资源模型，已替代）

首期默认 ONNX Runtime C++，原因是它能在现有 C++ 边界内加载 ONNX 并避免新服务跳数。`model-runtime/v1` 固定 ORT/execution provider/device/opset/operator/resource/numeric profile，不允许 runtime 自动切换 backend。

TensorRT 只有目标 GPU/compute capability 明确、plan/plugin 供应链固定、跨 backend golden 和性能收益通过时才建立独立 profile。LibTorch 只有 ONNX Runtime 无法保持已接受模型数值/算子合同且维护/ABI/镜像/资源/退出成本可接受时条件采用。首期不同时常驻多套 backend 作为“自动备用”。

current/previous/candidate 以及 candidate shadowing 的 model bytes、session、arena、CPU/RSS/GPU/VRAM、thread、queue、batch、load/compile/warmup 和 cache 全部受 `PERF-INF-001` 限制。model-control 使用独立低频 queue/thread，不得阻塞 ADR-0013 定义的 Rust↔C++ 有界共享内存热路径。每个 model/runtime/environment gate 必须冻结最低 throughput、最大 p99/RSS/VRAM 与 load/warmup/activate/readback/rollback deadline 的绝对数值和统计方法；目标硬件/模型/profile 未冻结时只能 `HOLD/NOT RUN`，不能只凭相对回退门槛 PASS。

MLflow 可作为条件采用的离线实验/registry/signature evidence，Triton/KServe 只作 version/rollout 模式参考；首期不把它们作为 serving runtime、在线 router、active binding 或正确性依赖。

## 取舍（历史 v1.6；当前权衡见 ADR-0017）

收益：

- 相同合同下可以替换算法、权重或扩展类别，而不改 Edge/Go 核心代码；
- model bytes、输入语义、标签语义、原始输出适配和 runtime 分别可审计；
- shadow、切换、响应丢失和回滚有唯一事实/readback/fence；
- 不把通用插件、registry alias 或外部 serving controller引入性能热路径；
- 新类别不能静默扩大自动处置权限。

代价：

- Go/PostgreSQL 增加专用 model control facts，C++ 增加 side-by-side slot 和低频控制 API；
- feature/label/adapter/backend 的兼容矩阵显著扩大；
- 首期放弃 weighted canary，模型推广粒度比通用 serving 平台保守；
- current/previous/candidate 与 shadowing 的内存预算需要在目标硬件上实测。

## 被拒绝的方案

1. **把模型作为 Plugin Platform kind**：把动态扩展带入 inference 热路径，混淆 plugin capability 与 model semantic qualification。
2. **C++ 启动时读取 `latest`/文件名/mtime**：没有 durable operation、readback、回滚和跨节点一致性。
3. **MLflow alias/Triton repository/KServe route 直接作为 active truth**：产生第二控制面和可变外部事实。
4. **bundle 携带 custom native/Python/Wasm pre/post code**：模型数据成为未审计代码加载器。
5. **模型切换只改 config 并重启**：无法区分 desired/prepared/actual active，也无法可靠处理 response loss/partial rollout。
6. **shadow 输出写 Event 后再加标记**：已经污染 canonical facts、cursor、policy 和幂等空间。
7. **首期 weighted canary**：检测覆盖、双跑 duplicate/policy 和回滚语义尚未有项目 profile。
8. **所有 backend 同时保留以便 fallback**：扩大镜像、ABI、资源、漏洞和数值资格面；fallback 必须是显式 qualified operation。

## 迁移与回滚（历史 v1.6 实施顺序，当前见 ADR-0017）

greenfield 首先冻结 `contracts/model/v1`、`model-runtime/v1`、golden、evidence schema 和 Fake Model Manager/C++ control endpoint；随后资格化 Offline ML bundle、C++ current-only、Go/PostgreSQL binding，再实现 previous/candidate/shadowing/activation/readback/rollback。Module Complete 前真实模型边界只能标记 `REHEARSAL/NOT QUALIFIED`。

旧 MASI 模型不得直接导入 active。只有原始 artifact、feature/label 行为、frozen replay 和工具链能够重建时，才创建新的 vNext revision/qualification；legacy signer/key/release/alias 状态不迁移。

模型控制面出现缺陷时禁止新的 activation/shadow，保持已确认 current 运行。若 current 无法安全运行，则尝试 exact previous；两者均不可用时停止对应检测 scope并显式 `HOLD/unavailable`，P4 已安装规则、effect recovery 和其他模块继续。

## 验证（历史 v1.6 candidate/shadow 项不得作为当前门禁）

除 `TEST-INF-001` 外，至少证明：

- bundle/metadata/feature/label/adapter/runtime/resource/digest 正反例和跨语言 golden；
- canonical golden 的 expected bytes/digest/error、浮点 tolerance/NaN/Inf/signed-zero 与各语言 runner/evidence digest；
- single/multi-label、anomaly/open-set、unknown/OOD/abstain 与 taxonomy evolution；
- same-contract 模型替换无需修改 Edge/Go业务代码，major change 稳定拒绝热替换；
- prepare/warmup/shadow 不影响 current，shadow 零 canonical Event/effect；
- operation state/非法转移、batch-boundary activation、required-node cohort/readback、partial rollout/old-node fence、concurrent activate/rollback、response loss/restart/failover 与 late-result fence；
- exact previous rollback、previous revoked/unavailable、历史 Event 不改写；
- comparison 幂等/冲突/retention 与数据库权限证明 shadow 零 canonical write；
- current/previous/candidate 与 shadowing 最大资源、绝对/相对门槛、hot-ring isolation、saturation 和 soak；
- ONNX Runtime exact profile、TensorRT/LibTorch 条件门禁、MLflow offline-only 以及 Triton/KServe serving/control-plane rejection。

当前仓库仍处于初始化阶段。ADR accepted 只表示需求和边界被 Owner 确认，不表示 contract、Model Manager、C++ control API、bundle、遥测/推理热路径、测试或生产资格已经实现；证据形成前均为 `HOLD/NOT RUN`。

## 参考

- v1.13历史central-GPU pool：`0016-central-gpu-inference-pool-and-routing-boundary.md`
- 当前Central Inference CPU/CUDA启动选择、路由与E2E：`0017-central-inference-runtime-selection-and-real-e2e.md`
- 历史启动绑定/滚动重启决策：`0011-startup-bound-model-selection-and-rolling-restart.md`
- 在线遥测、窗口、热路径 ABI 与 canonical ACK：`0013-online-telemetry-and-inference-hot-path.md`
- 专项调研：`../research/online-model-modularity-assessment-2026-08-10.md`
- 契约/Profile：`0005-contract-runtime-and-protocol-profiles.md`
- 资格证据：`0006-qualification-levels-and-evidence.md`
- 插件边界：`0003-controlled-general-plugin-platform.md`
- 成熟组件采用：`0009-mature-component-reuse-boundaries.md`
- ONNX IR：<https://onnx.ai/onnx/repo-docs/IR.html>
- ONNX Runtime C++ ModelMetadata：<https://onnxruntime.ai/docs/api/c/struct_ort_1_1_model_metadata.html>
- MLflow Model Signatures/Registry：<https://mlflow.org/docs/latest/ml/model/signatures/>、<https://mlflow.org/docs/latest/ml/model-registry/workflow>
- NVIDIA Triton Model Repository：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_repository.html>
- KServe `LLMInferenceService` Canary Rollout（仅模式参考）：<https://kserve.github.io/website/docs/next/model-serving/generative-inference/llmisvc/canary-rollout>
