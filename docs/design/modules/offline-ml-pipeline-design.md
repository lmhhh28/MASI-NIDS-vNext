# Offline ML Artifact Pipeline 模块详细设计

- 模块 ID：`MOD-ML-001`
- 目录：`ml-py/`
- 文档状态：`DRAFT`
- 主要需求：`CORE-MODEL-001`、`ARCH-MODEL-001`、`MOD-ML-001`、`CONTRACT-MODEL-001`、`FUNC-INF-MODEL-001`、`PERF-INF-001`、`SEC-SUPPLY-001`、`TEST-003`、`TEST-INF-001`、`TEST-REUSE-001`
- 主要 ADR：ADR-0005、ADR-0006、ADR-0009、ADR-0010保留的模型模块化不变量、ADR-0017、ADR-0019

## 1. 模块目标

Offline ML Pipeline 将受控数据、feature/label合同、ADR-0019冻结的训练recipe和toolchain转换为immutable、digest-pinned、可由Central Inference启动加载的model bundle/repository snapshot候选，并生成质量、数值、可解释、性能、兼容、安全和供应链证据。

它是独立qualification target，不是在线服务。它不连接P4、不写canonical Event/effect/model current、不控制Triton或部署，也不把实验registry alias/stage当生产binding。新模型只有经过Go Model Manager登记/资格化和显式rollout后才可能成为current。

## 2. Pipeline 构件

```text
Immutable source manifests + official fetch/verify metadata
→ validation / license / privacy / ground-truth / capture-family split verification
→ PCAP/fixture to canonical dataset-p4-window-binary/v1
→ P4/BMv2 versus offline reference-extractor golden
→ deterministic LR / XGBoost / benign-only Autoencoder train
→ calibration / threshold / OOD + explanation artifacts
→ evaluation and frozen replay
→ export ONNX + scaler/config/metadata
→ build exact repository closure and optional optimized artifact
→ cross-language numeric/golden generation
→ security/supply-chain/compatibility/performance qualification package
→ immutable model bundle candidate
```

各阶段必须有版本化输入/输出和digest，可从原始manifest重跑；实现可以拆Python packages/commands，但不拆为在线服务或隐藏实验平台依赖。

## 3. 数据与可重现性

每次run固定dataset/source/split digest、license/use/redistribution、privacy/redaction、ground truth、feature extraction、seed、toolchain/image、hardware和config。`train|early_stop|calibration|blind_test`不可在结果产生后静默改变；数据泄漏、duplicate flow/session、time overlap和label join规则必须显式验证。

公开数据集只在具体slice通过license/privacy/ground-truth门禁后使用。原始大型或不允许再分发数据留在受控存储；仓库/OCI只保存允许的最小fixture或fetch/verify metadata。

同输入、seed、toolchain和profile应产生可解释的可重现结果。硬件/库存在允许的非确定性时，manifest声明来源、数值容差和重复统计；不能把未复现结果标成deterministic。

本设计中的“已冻结”分三层：ADR-0019的source selection/recipe已冻结；可消费dataset revision
必须等official artifact、digest、治理证据、转换器和split manifest齐全后才冻结；production
winner必须等真实三候选比较与bundle形成后才冻结。当前后两项均为`NOT_RUN`，不得在UI、README
或evidence中简写成“数据集/模型已就绪”。

### 3.1 首期数据源

首期训练数据产品固定为 `dataset-p4-window-binary/v1`，不是任何公开数据集的现成 flow CSV。每个样本必须由 `edge-feature-p4-window/v1` 的 exact 10 秒 final+valid 窗口产生，特征固定为 `[aggregate_packets,aggregate_bytes,reported_nonzero_cells,max_cell_packets,max_cell_bytes,snapshot_count]`。大型 PCAP 允许使用离线 reference extractor，但它必须先在最小 corpus 上与 exact BMv2/P4 selector、256-cell、bank/epoch/snapshot 逐字段 golden 一致。

数据角色由ADR-0019固定：项目自有 `masi-synthetic-p4-window/v1` 与官方重新摄取的 `cic-ddos2019-p4-window/v1` 是首期拟合来源；CSE-CIC-IDS2018、CIC-IDS2017、UNSW-NB15、TON-IoT及未来IoT profile下的CICIoT2023只在各自许可/隐私/ground-truth门禁通过后作独立cross-corpus验证。旧 `/home/lmhhh/MASI-NIDS/dataset` 为 `REJECT direct import`，不得复制其CSV/NPY/scaler/model/class map或把legacy 21列映射为线上六维特征。

source-role矩阵固定为：synthetic按capture-family hash分配`60/15/15/10`；CIC-DDoS2019
official-training分配`70/15/15/0`；official-test全部sealed blind；所有OOC source仅blind。
训练权重按`source×label`、再按capture family、最后按window三级等权。缺类、group交叉或需要
移动window补比例时dataset revision直接拒绝，不做SMOTE、随机row split或事后重采样。

### 3.2 Split、标签与泄漏

split封闭为`train|early_stop|calibration|blind_test`。按source revision、raw capture digest、scenario、session、endpoint/topology和不重叠时间段形成`capture_family_id`，禁止逐row/window随机切分。官方test partition只能进入blind test；IP、port、Flow ID、timestamp、文件名和攻击时段只用于grouping/ground-truth，不进入feature。

首期production taxonomy是binary：`benign=0`、`supported_aggregate_visible_attack=1`。拟合只接受ground-truth coverage完整的高纯度窗口：benign为零attack packet，attack的supported attack packet share至少90%。mixed/ambiguous/unknown不拟合但必须在evaluation单列。DoS/DDoS/flood/scan是当前feature的候选覆盖；XSS、SQL injection、Heartbleed exploit、ransomware payload和内容型infiltration为`not_covered`，不得从corpus标签推导支持。

join算法必须按packet timestamp和direction-aware canonical 5-tuple/protocol关联官方flow/event，
窗口为`[start,end)`，coverage分母为窗口全部eligible packet。只有coverage=1.0的高纯度窗口可拟合；
时间段命中但tuple不确定、标签重叠/冲突均进入`ambiguous|unknown`。P4 hash collision影响feature
quality但不改变packet label，CICFlowMeter row或非零cell都不能替代packet denominator。

### 3.3 首期训练 Recipe

共同preprocessing在ONNX图内执行`Cast(float32) -> log1p`；LR/Autoencoder scaler只从train拟合，XGBoost不另设scaler。固定seed set为`17,29,43`，输入按`source_profile/capture_family/window_start/window_id`排序，使用exact deterministic CPU训练profile。超参搜索只读train/early-stop，Platt/threshold只读calibration，blind test在所有candidate/config冻结后一次打开。

- `lr-window-binary/v1`：加权population StandardScaler + L2 Logistic Regression；`lbfgs/max_iter=1000/tol=1e-6`，只搜索`C={0.01,0.1,1,10}`。SGD `partial_fit`只作out-of-core/增量训练一致性证据，production artifact仍是immutable full-fit revision；
- `xgb-window-binary/v1`：首个production champion candidate；CPU `hist/max_bin=256`、最多512 round、patience32、subsample与column sample均为1，只搜索ADR-0019固定的16组depth/eta/min-child/lambda；raw margin经Platt校准，导出无ZipMap/custom op的ONNX-ML `[N,2]` probability；
- `ae-window-benign/v1`：`6-8-3-8-6` benign-only undercomplete Autoencoder；scaler只拟合benign train，CPU deterministic、batch1024、SmoothL1 beta=1、AdamW、最多200 epoch/patience20，只搜索4组lr/weight-decay。raw anomaly score是scaled-log空间六维SmoothL1 residual的mean，再由calibration split的Platt映射现有`[benign,attack]` probability。它延续legacy增量自编码器思想，但不复制旧代码/模型且绝不在线更新current；
- EBM只作offline glass-box challenger，未通过exact ONNX/Triton/ORT profile前不得上线。

首期rollout只选择一个exact bundle，不采用ensemble、weighted route、online shadow或失败后换模型。

三个候选的最终ONNX都必须输出finite float32 `[1-p_attack,p_attack]`，score domain为
`probability`。LR logit、XGBoost raw margin、AE residual各自拟合二参数Platt并要求正斜率；
`τ_alert/τ_conf`仅从calibration finite scores按ADR-0019穷举，复用现有
`masi-window-adapter-v1`，满足FPR/recall/coverage/benign-abstain约束。无可行阈值即拒绝候选，
不得读取blind test回调。现有OOD只是maximum-probability低置信代理，不声称检测任意未知攻击。

## 4. Feature、Label 与 Output 合同

### 4.1 Feature Schema

固定field ID/order、dtype/shape、unit/scale、window、missing/default、normalization/scaler、NaN/Inf/OOD、sampling/quality要求和producer compatibility。首期必须保持`[1,6] uint64-le` raw window和同一10秒/P4 selector语义；Offline extractor与Edge online feature adapter使用同一golden，禁止维护“训练版”和“线上版”两套含义。

### 4.2 Label Taxonomy

合同机制支持`single_label|multi_label|anomaly_score|open_set`，稳定label ID永不重用。ADR-0019首期recipe只产出binary single-label `[benign,attack]` probability；Autoencoder的raw reconstruction error在模型内经资格化calibration映射后仍遵守该输出，不把异常分数伪装成攻击家族。Taxonomy固定class order/axis、score domain、threshold/calibration/top-k、unknown/OOD/abstain及增删/合并/拆分映射。

添加更多攻击流量类别通过新taxonomy/model bundle表达；若旧reader能保留unknown且语义兼容可作为minor，否则升major并走reader/rollout矩阵。新label或更高confidence默认没有effect eligibility。

### 4.3 Output Adapter

Pipeline产出受限数据配置和adapter identity，使C++中已资格化的deterministic adapter把raw tensor映射为canonical prediction。Bundle不能携带Python/Wasm/native hook、custom op或任意可执行pre/post代码。

首期三个mandatory candidate都必须输出canonical `[N,2]` finite probability tensor并复用`masi-window-adapter-v1`；`label_taxonomy.threshold.value`和`output_adapter.ood_policy.threshold`均绑定`τ_conf`，`output_adapter.threshold`绑定`τ_alert`。Logit仅是模型graph内校准前中间量，不是公开bundle output。不能为接纳某个训练library而改变scores含义；XGBoost converter的ZipMap、动态map/sequence输出或custom operator必须关闭/拒绝。

## 5. Model Bundle

Bundle至少包含或引用：

- model ID/immutable revision、manifest/artifact size/digest；
- ONNX model、scaler、config、threshold/calibration/OOD数据；
- feature schema、label taxonomy、output adapter digest；
- ONNX IR/opset/operator、runtime/backend/optimization compatibility；
- train/evaluation data/config/seed/toolchain、quality/numeric/performance结果；
- ADR-0019 recipe/profile、capture-family split、candidate selection和explanation evidence digest；
- publisher/provenance、SBOM、license和offline verification；
- current/previous reader、所声明runtime profile和rollback compatibility matrix；未声明CUDA时保留明确`NOT_APPLICABLE`行而不是伪造CUDA PASS；
- qualification evidence IDs和exact claim scope。

ONNX metadata与external manifest交叉验证name/dtype/shape/model version/project keys，但metadata不替代digest、publisher或qualification。

## 6. Repository 与 Optimization Artifact

Pipeline为Triton `NONE`模式生成exact repository closure，只含一个binding及声明依赖，显式`config.pbtxt`/instance group/profile。Repository只读、no symlink/no path traversal，不包含额外version/backend或可执行plugin。

`session_online_optimization`与`preoptimized_offline`是独立profile。Offline optimized artifact绑定raw model、生成工具、ORT/EP/options/device/CPU features；不能跨CPU/CUDA/硬件假设通用，缺失或不兼容不得fallback到另一模式。

## 7. 质量与数值证据

Qualification报告至少分开：

- dataset/split/label coverage和class imbalance；
- source/scenario/capture-family与mixed/ambiguous/not-covered coverage，禁止只报pooled score；
- accuracy/precision/recall/F1/ROC等适用指标及per-class/confusion；
- calibration/threshold、OOD/abstain和unknown；
- Logistic raw-margin contribution/additivity、XGBoost interventional/raw-margin exact TreeSHAP/additivity、Autoencoder scaled-log SmoothL1 residual/mean identity的method/background/sample/version/digest、coverage、truncation、三seed重复性、global-rank稳定性和限制；
- frozen generated/synthetic/curated replay的quality与accepted difference；
- Python reference ↔ C++ ORT CPU的input/output/class order、absolute/relative/ULP tolerance、NaN/Inf/signed-zero；只有exact bundle显式声明CUDA时才加入C++ ORT CUDA列，否则以稳定理由记录`applicability=NOT_APPLICABLE`；
- adversarial/malformed shape/dtype/opset/metadata和custom executable拒绝；
- inference latency/throughput/resource只是模块候选证据，不能替代完整Central或packet→Event性能。

CPU与CUDA数值/性能证据独立。若两者差异超过兼容claim，必须用不同profile/generation表达，不降低容差掩盖。

首期acceptance candidate门槛由ADR-0019固定为三个seed分别满足：blind-test profile `PR-AUC`（定义为non-interpolated AP）`>=0.95`、`precision/recall/macro-F1>=0.90`、benign `FPR<=0.01`、`ECE<=0.05`、`Brier<=0.10`，且有至少100个独立窗口的supported family recall `>=0.80`。这些门槛不外推为目标生产分布、CUDA、HA或production qualification；缺目标证据仍为HOLD。

另要求non-abstained coverage`>=0.95`与benign abstain rate`<=0.05`。三个candidate必须全部实际执行；质量失败的candidate记录`REJECTED`，不是可以省略的步骤。只有三个seed和全部required blind source均通过的candidate才eligible；没有eligible candidate时`winner=none`且不生成可注册bundle。

## 8. 安全与供应链

- input archive/model/ONNX/repository按不可信文件处理，限制file count/size/nesting/decompression ratio/path；
- 拒绝absolute/`..`/symlink/hardlink/device/FIFO/socket、executable bits和未声明native/custom operator；
- build无production DB/P4/Edge credential，默认离线依赖；
- Python/package/compiler/image/dataset/tool均有lock、license/NOTICE、SBOM/provenance和digest；
- secret/personal/raw payload不进入model metadata、log、bundle或普通evidence；
- output先写隔离staging并验证，再发布immutable digest，不原地覆盖revision/tag。

## 9. 与外部实验工具的边界

PyTorch/NumPy/scikit-learn/XGBoost/ONNXMLTools/SHAP或已资格化工具可用于训练与解释；上游支持只是候选依据，actual version/source/license/lock/SBOM必须登记，实际XGBoost converter、calibration graph、operator closure、Triton config和ORT CPU执行必须由本模块真实验证。InterpretML EBM只在offline challenger边界使用。MLflow/等价registry仅可保存/导出offline experiment/signature evidence。Alias/stage、tracking DB、model serving endpoint和registry availability不进入生产runtime依赖或current。

Model Analyzer等工具可以离线搜索Triton batch/instance候选，最终值必须固化到profile并由Central真实性能测试；工具本身不自动发布或修改pool。

## 10. 执行、状态与失败

Pipeline run使用immutable run ID和stage/output digest，支持从已验证stage重算但不把partial output发布为bundle。Same run/input/config产生same canonical artifact或明确nondeterminism conflict；same identity different input拒绝。

任何data/license/privacy/feature/label/numeric/security/performance/compatibility门禁失败都形成rejected candidate和原始evidence，不生成qualified标记。Crash/disk full/OOM/timeout留下可识别partial staging并由exact run cleanup；不能清理时隔离目录/runner。

## 11. 独立真实执行与黑盒 E2E

必须以实际Python 3.12+ release image/toolchain运行完整受控pipeline，输入frozen四split manifest、seed和model profile，输出可由独立validator读取的bundle/qualification manifest。验收覆盖：

- deterministic/repeat run、split/data/ground-truth/license/privacy；
- official fetch/digest、legacy direct-import拒绝、capture-family泄漏与CIC-DDoS2019 official-test sealed blind；
- P4/BMv2与reference extractor的六维逐字段golden、mixed/ambiguous/not-covered coverage；
- 三个seed下Logistic/XGBoost/Autoencoder mandatory execution、candidate eligibility/rejection、fail-closed `winner=none`和最多一个bundle胜出；
- feature/label/output/schema和多类别/unknown/OOD/abstain；
- ONNX export/metadata/repository closure/optimization profile；
- XGBoost ONNX-ML no-ZipMap/no-custom-op、Autoencoder calibrated two-score输出与explanation evidence；
- Python/C++/Rust/Go/TypeScript适用golden和numeric tolerance；首期CPU适用、CUDA稳定`NOT_APPLICABLE`，除非bundle显式声明CUDA；
- malformed/path traversal/symlink/executable/custom op/oversize；
- current/previous reader和CPU/CUDA兼容/rollback matrix；
- crash/OOM/disk/partial output、资源、性能、供应链和cleanup。

邻居Central/Go可以使用contract validator/fake，但Pipeline自身训练、评估、导出、打包和证据逻辑不得fake。小型确定性dataset可用于模块E2E，模型质量production claim仍需目标数据scope证据。

## 12. Module Complete 判定

Offline ML不能因脚本能训练、ONNX能导出或一个模型accuracy达标而完成。完整pipeline、ADR-0019四split/三seed/三mandatory candidate、P4-window golden、immutable bundle、feature/label/output、explanation、numeric/quality/security/supply-chain/compatibility/fault/performance和可重现真实执行证据全部通过后，才可标记Module Complete；这仍不自动激活模型或证明Central/System E2E。

在实现启动前必须先把ADR-0019语义落成`dataset-p4-window-binary/v1`与
`model-explanation-evidence/v1`的公开schema/profile/golden和evidence validator。当前这些文件
尚不存在；本设计不得被引用为contract已冻结或Gate 0已PASS的证据。
