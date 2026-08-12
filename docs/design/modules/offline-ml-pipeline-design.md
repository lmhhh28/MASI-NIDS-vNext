# Offline ML Artifact Pipeline 模块详细设计

- 模块 ID：`MOD-ML-001`
- 目录：`ml-py/`
- 文档状态：`DRAFT`
- 主要需求：`CORE-MODEL-001`、`ARCH-MODEL-001`、`MOD-ML-001`、`CONTRACT-MODEL-001`、`FUNC-INF-MODEL-001`、`PERF-INF-001`、`SEC-SUPPLY-001`、`TEST-003`、`TEST-INF-001`、`TEST-REUSE-001`
- 主要 ADR：ADR-0005、ADR-0006、ADR-0009、ADR-0010保留的模型模块化不变量、ADR-0017

## 1. 模块目标

Offline ML Pipeline 将受控数据、feature/label合同、训练配置和toolchain转换为immutable、digest-pinned、可由Central Inference启动加载的model bundle/repository snapshot候选，并生成质量、数值、性能、兼容、安全和供应链证据。

它是独立qualification target，不是在线服务。它不连接P4、不写canonical Event/effect/model current、不控制Triton或部署，也不把实验registry alias/stage当生产binding。新模型只有经过Go Model Manager登记/资格化和显式rollout后才可能成为current。

## 2. Pipeline 构件

```text
Immutable data manifests
→ validation / license / privacy / split verification
→ feature extraction compatible with contracts/telemetry + feature schema
→ deterministic train / calibration / threshold / OOD artifacts
→ evaluation and frozen replay
→ export ONNX + scaler/config/metadata
→ build exact repository closure and optional optimized artifact
→ cross-language numeric/golden generation
→ security/supply-chain/compatibility/performance qualification package
→ immutable model bundle candidate
```

各阶段必须有版本化输入/输出和digest，可从原始manifest重跑；实现可以拆Python packages/commands，但不拆为在线服务或隐藏实验平台依赖。

## 3. 数据与可重现性

每次run固定dataset/source/split digest、license/use/redistribution、privacy/redaction、ground truth、feature extraction、seed、toolchain/image、hardware和config。Train/validation/test split不可在结果产生后静默改变；数据泄漏、duplicate flow/session、time overlap和label join规则必须显式验证。

公开数据集只在具体slice通过license/privacy/ground-truth门禁后使用。原始大型或不允许再分发数据留在受控存储；仓库/OCI只保存允许的最小fixture或fetch/verify metadata。

同输入、seed、toolchain和profile应产生可解释的可重现结果。硬件/库存在允许的非确定性时，manifest声明来源、数值容差和重复统计；不能把未复现结果标成deterministic。

## 4. Feature、Label 与 Output 合同

### 4.1 Feature Schema

固定field ID/order、dtype/shape、unit/scale、window、missing/default、normalization/scaler、NaN/Inf/OOD、sampling/quality要求和producer compatibility。Offline extractor与Edge online feature adapter使用同一golden，禁止维护“训练版”和“线上版”两套含义。

### 4.2 Label Taxonomy

支持`single_label|multi_label|anomaly_score|open_set`，稳定label ID永不重用。Taxonomy固定class order/axis、score domain、threshold/calibration/top-k、unknown/OOD/abstain及增删/合并/拆分映射。

添加更多攻击流量类别通过新taxonomy/model bundle表达；若旧reader能保留unknown且语义兼容可作为minor，否则升major并走reader/rollout矩阵。新label或更高confidence默认没有effect eligibility。

### 4.3 Output Adapter

Pipeline产出受限数据配置和adapter identity，使C++中已资格化的deterministic adapter把raw tensor映射为canonical prediction。Bundle不能携带Python/Wasm/native hook、custom op或任意可执行pre/post代码。

## 5. Model Bundle

Bundle至少包含或引用：

- model ID/immutable revision、manifest/artifact size/digest；
- ONNX model、scaler、config、threshold/calibration/OOD数据；
- feature schema、label taxonomy、output adapter digest；
- ONNX IR/opset/operator、runtime/backend/optimization compatibility；
- train/evaluation data/config/seed/toolchain、quality/numeric/performance结果；
- publisher/provenance、SBOM、license和offline verification；
- current/previous reader、CPU/CUDA和rollback compatibility matrix；
- qualification evidence IDs和exact claim scope。

ONNX metadata与external manifest交叉验证name/dtype/shape/model version/project keys，但metadata不替代digest、publisher或qualification。

## 6. Repository 与 Optimization Artifact

Pipeline为Triton `NONE`模式生成exact repository closure，只含一个binding及声明依赖，显式`config.pbtxt`/instance group/profile。Repository只读、no symlink/no path traversal，不包含额外version/backend或可执行plugin。

`session_online_optimization`与`preoptimized_offline`是独立profile。Offline optimized artifact绑定raw model、生成工具、ORT/EP/options/device/CPU features；不能跨CPU/CUDA/硬件假设通用，缺失或不兼容不得fallback到另一模式。

## 7. 质量与数值证据

Qualification报告至少分开：

- dataset/split/label coverage和class imbalance；
- accuracy/precision/recall/F1/ROC等适用指标及per-class/confusion；
- calibration/threshold、OOD/abstain和unknown；
- frozen generated/synthetic/curated replay的quality与accepted difference；
- Python reference ↔ C++ ORT CPU ↔ C++ ORT CUDA的input/output/class order、absolute/relative/ULP tolerance、NaN/Inf/signed-zero；
- adversarial/malformed shape/dtype/opset/metadata和custom executable拒绝；
- inference latency/throughput/resource只是模块候选证据，不能替代完整Central或packet→Event性能。

CPU与CUDA数值/性能证据独立。若两者差异超过兼容claim，必须用不同profile/generation表达，不降低容差掩盖。

## 8. 安全与供应链

- input archive/model/ONNX/repository按不可信文件处理，限制file count/size/nesting/decompression ratio/path；
- 拒绝absolute/`..`/symlink/hardlink/device/FIFO/socket、executable bits和未声明native/custom operator；
- build无production DB/P4/Edge credential，默认离线依赖；
- Python/package/compiler/image/dataset/tool均有lock、license/NOTICE、SBOM/provenance和digest；
- secret/personal/raw payload不进入model metadata、log、bundle或普通evidence；
- output先写隔离staging并验证，再发布immutable digest，不原地覆盖revision/tag。

## 9. 与外部实验工具的边界

PyTorch/NumPy/scikit-learn或已资格化工具可用于训练；MLflow/等价registry仅可保存/导出offline experiment/signature evidence。Alias/stage、tracking DB、model serving endpoint和registry availability不进入生产runtime依赖或current。

Model Analyzer等工具可以离线搜索Triton batch/instance候选，最终值必须固化到profile并由Central真实性能测试；工具本身不自动发布或修改pool。

## 10. 执行、状态与失败

Pipeline run使用immutable run ID和stage/output digest，支持从已验证stage重算但不把partial output发布为bundle。Same run/input/config产生same canonical artifact或明确nondeterminism conflict；same identity different input拒绝。

任何data/license/privacy/feature/label/numeric/security/performance/compatibility门禁失败都形成rejected candidate和原始evidence，不生成qualified标记。Crash/disk full/OOM/timeout留下可识别partial staging并由exact run cleanup；不能清理时隔离目录/runner。

## 11. 独立真实执行与黑盒 E2E

必须以实际Python 3.12+ release image/toolchain运行完整受控pipeline，输入frozen train/validation/test manifest、seed和model profile，输出可由独立validator读取的bundle/qualification manifest。验收覆盖：

- deterministic/repeat run、split/data/ground-truth/license/privacy；
- feature/label/output/schema和多类别/unknown/OOD/abstain；
- ONNX export/metadata/repository closure/optimization profile；
- Python/C++/Rust/Go/TypeScript适用golden和numeric tolerance；
- malformed/path traversal/symlink/executable/custom op/oversize；
- current/previous reader和CPU/CUDA兼容/rollback matrix；
- crash/OOM/disk/partial output、资源、性能、供应链和cleanup。

邻居Central/Go可以使用contract validator/fake，但Pipeline自身训练、评估、导出、打包和证据逻辑不得fake。小型确定性dataset可用于模块E2E，模型质量production claim仍需目标数据scope证据。

## 12. Module Complete 判定

Offline ML不能因脚本能训练、ONNX能导出或一个模型accuracy达标而完成。完整pipeline、immutable bundle、feature/label/output、numeric/quality/security/supply-chain/compatibility/fault/performance和可重现真实执行证据全部通过后，才可标记Module Complete；这仍不自动激活模型或证明Central/System E2E。
