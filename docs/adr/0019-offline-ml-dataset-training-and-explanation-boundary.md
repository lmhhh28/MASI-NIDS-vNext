# ADR-0019：首期离线训练数据集、模型候选与解释边界

- 状态：Accepted
- 日期：2026-08-22
- 决策者：Owner
- 对应需求基线：`vNext-requirements-1.19`
- 影响需求：`CORE-MODEL-001`、`ARCH-MODEL-001`、`MOD-ML-001`、`CONTRACT-MODEL-001`、`FUNC-INF-MODEL-001`、`CONTRACT-TRAFFIC-001`、`TEST-INF-001`、`TEST-TRAFFIC-001`、`AGENT-001`、`TEST-PLUGIN-001`、`TEST-WEB-001`
- 当前资格：source/recipe 决策已接受；可消费 dataset revision 尚未冻结。官方下载、摄取、训练、bundle与真实运行均为`result=NOT_RUN, qualification=NOT_QUALIFIED`
- 评估依据：[`../research/offline-ml-dataset-and-model-selection-assessment-2026-08-22.md`](../research/offline-ml-dataset-and-model-selection-assessment-2026-08-22.md)

## 背景

公开 NIDS corpus 普遍提供 flow CSV 或 PCAP，但首期在线输入已经固定为 P4 256-cell
aggregate 经 Edge finalization 后的六维 10 秒窗口。直接训练 CICFlowMeter/Zeek/legacy 21 列
模型，会形成训练/在线两套 feature 语义。旧 MASI-NIDS dataset 目录还缺少 vNext 所需的
许可、隐私、来源、capture-family split 和 exact P4-window provenance，不能直接接纳。

同时，项目需要在不返工已完成 Central Inference、Edge、Go、PostgreSQL 和 Plugin Host
公开边界的情况下启动 Offline ML、Analysis 和 Web。模型算法可以作为 compatible immutable
bundle 替换，但本recipe的首期 wire 仍必须是 `[N,6] uint64-le -> [N,2] finite probability`。

## 决策

本文把三个容易混淆的状态分开：

- **source selection frozen**：首期允许哪些来源承担拟合、校准或独立盲测角色已经确定；
- **dataset revision frozen**：只有 exact 官方 artifact、digest、许可/隐私/ground-truth、转换器、split manifest 与样本清单全部形成后才成立；当前尚不成立；
- **production winner selected**：只有三个 mandatory candidate 均实际完成比较、至少一个候选通过全部门槛，并形成 exact bundle 后才成立；当前 `winner=none`。

因此“训练方案与数据集已确定”只表示来源角色、数据产品和训练 recipe 已冻结，不表示数据已经下载、模型已经训练或取得任何资格。

### 1. Canonical 训练样本

首期唯一训练数据产品是 `dataset-p4-window-binary/v1`：每个样本必须是
`edge-feature-p4-window/v1` 的 exact `[1,6] uint64-le` final+valid 10 秒窗口，并绑定 P4
selector、256-cell、bank/epoch/snapshot、source/capture/scenario、ground-truth、split、
license/privacy 和全部 digest。

大型 corpus 可由通过 P4/BMv2 小型逐字段 golden 的离线 reference extractor 生成；不能把
公开 flow CSV 的任意字段重命名、补零或投影成线上特征。selector/window/P4Info/feature major
变化创建新数据 profile。

### 2. 数据集角色

| Source profile | 决策 | 角色 |
|---|---|---|
| `masi-synthetic-p4-window/v1` | `ADOPT` | 项目自有 generated/synthetic/live-session 基础 corpus；四 split 使用不同 scenario/seed/topology/capture family |
| `cic-ddos2019-p4-window/v1` | `ADOPT after ingestion gates` | 官方 PCAP 重提取的主要外部 training/calibration/blind corpus；官方 test partition 保持 sealed blind |
| `cse-cic-ids2018-p4-window-ooc/v1` | `CONDITIONAL` | 从官方来源重新取得、通过许可/隐私/join 后的 cross-corpus blind evaluation；只评价 aggregate-visible family，不训练 |
| `cic-ids2017-p4-window-ooc/v1` | `CONDITIONAL` | 许可批准后的 cross-corpus blind evaluation；只评价 aggregate-visible family，不训练 |
| `unsw-nb15-p4-window-ooc/v1` | `CONDITIONAL` | 学术/商业用途批准和 PCAP join 通过后的外部验证；不训练、不分发 |
| `ton-iot-p4-window-ooc/v1` | `CONDITIONAL` | IoT domain validation；不训练，不直接使用 legacy CSV |
| `ciciot2023-p4-window-ooc/v1` | `CONDITIONAL future IoT profile` | 仅在 IoT target/source profile 被声明、许可与 PCAP join 通过后作独立盲测；不用于当前通用 BMv2 scope |
| legacy `/home/lmhhh/MASI-NIDS/dataset` | `REJECT direct import` | 只作来源定位/行为参考；不得复制 CSV/NPY/scaler/model/class map |

任何 source 的 exact官方下载artifact、digest、用途/再分发、privacy 和 ground truth 未形成时，
该 source 为 `HOLD`。原始 PCAP 不进入 Git 或普通 OCI。

拟合 source 的使用矩阵固定如下；不得在看到指标后修改来源权重或把 blind source 混回训练：

| Source | `train` | `early_stop` | `calibration` | `blind_test` |
|---|---|---|---|---|
| project synthetic | capture-family hash 60% | 15% | 15% | 10% |
| CIC-DDoS2019 official training partition | capture-family hash 70% | 15% | 15% | 禁止 |
| CIC-DDoS2019 official test partition | 禁止 | 禁止 | 禁止 | 100%，sealed |
| 所有 `*-ooc/v1` | 禁止 | 禁止 | 禁止 | 100%，各 source 独立报告 |

hash 算法为 `u64be(SHA-256("masi-split-v1\0" || source_revision || "\0" || capture_family_id)[0:8]) / 2^64`；边界使用左闭右开区间。任一 source×label 在所需 split 缺类、group 数不足或发生 capture-family 交叉时，dataset revision 整体拒绝，不移动单个 window“补齐”比例。

训练权重不通过复制样本或 SMOTE 获得。每个可用 `source_profile × binary_label` 单元总权重相同，单元内每个 `capture_family_id` 总权重相同，family 内 window 等权；最终归一化为权重和等于训练样本数。每个 source/scenario 的未加权结果仍单独报告，不能用训练权重产生的 pooled score 掩盖失败。

### 3. Label 与 split

首期生产 label taxonomy 固定为 binary：`benign=0`、
`supported_aggregate_visible_attack=1`。攻击 family 只作资格切片元数据。DoS/DDoS/flood/scan
等 aggregate-visible 场景属于首期候选；XSS、SQL injection、Heartbleed exploit、ransomware
payload 和内容型 infiltration 为 `not_covered`，不得因 corpus 标签存在而声称支持。

split 固定为 `train|early_stop|calibration|blind_test`，按 source revision、raw capture、
scenario、session、endpoint/topology 和不重叠时间段形成 capture-family group；禁止逐 row/window
随机切分。官方 test partition 只能进入 blind test。IP、port、Flow ID、timestamp、文件名和攻击
时段只用于 join/group，不进入 feature。

拟合只接受 ground-truth coverage 完整的高纯度窗口：benign 为零 attack packet；attack 的
supported attack packet share 至少 90%。mixed/ambiguous/unknown 不拟合但必须在 evaluation
单列，不得静默删除或补零。

ground-truth join 固定为：按原始 packet timestamp 与 canonical direction-aware 5-tuple/protocol 关联官方 flow/event label；窗口采用 `[start,end)`，边界 packet 只属于右侧窗口。只有 `labeled_eligible_packets / eligible_packets = 1.0` 的窗口可拟合；仅有时间段而无法完成 tuple join、标签冲突或重叠攻击均为 `ambiguous|unknown`。attack share 的分母是该窗口全部 eligible packet，不是非零 P4 cell、flow row 或 hash bucket；P4 hash collision 只影响 feature quality，不改变 packet-level label。join 算法、输入清单、冲突计数和 coverage digest 必须进入 dataset manifest。

### 4. 训练候选

首期必须训练和比较三类 candidate，且 production rollout 只选择其中一个 exact bundle：

1. `lr-window-binary/v1`：L2 Logistic Regression 透明基线。`StandardScaler` 使用加权 train
   population mean/variance；零方差维度的 scale 固定为 `1.0`。`lbfgs`、`max_iter=1000`、
   `tol=1e-6`，只搜索 `C∈{0.01,0.1,1,10}`。另运行 `SGDClassifier(loss=log_loss)` 的
   固定顺序 `partial_fit` 作为 out-of-core/增量一致性证据，但不作为 production artifact，
   也不在线更新 current；
2. `xgb-window-binary/v1`：首个 production champion candidate，使用 CPU `hist`、
   `max_bin=256`、`subsample=1`、`colsample_bytree=1`、最多 512 round、32 round early stop；
   bounded grid 只含 `max_depth∈{3,6}`、`eta∈{0.03,0.1}`、
   `min_child_weight∈{1,5}`、`reg_lambda∈{1,10}`。内部 raw margin 经下述 calibration 后，
   导出无 ZipMap/custom op 的 ONNX-ML `[N,2]` probability；
3. `ae-window-benign/v1`：`6-8-3-8-6`、ReLU、benign-only undercomplete autoencoder
   challenger；`StandardScaler` 只拟合 benign train。CPU deterministic mini-batch
   `batch_size=1024`、SmoothL1 `beta=1`、AdamW，最多 200 epoch、patience 20，只搜索
   `lr∈{3e-4,1e-3}` 与 `weight_decay∈{0,1e-4}`。每样本 raw anomaly score 固定为
   `mean_j SmoothL1(s_j,reconstruct(s)_j; beta=1)`；逐特征 residual 保留同一 scaled-log
   空间。独立 calibration split 使用 Platt sigmoid 映射为 `[benign,attack]` probability。
   新增数据只能产生新 immutable revision。

共同 preprocessing 是在 ONNX 图内 `Cast(float32) -> log1p`；LR/AE scaler只从 train
split 拟合并绑定 bundle，XGBoost 不另做 scaler。固定 seeds `17,29,43`，样本顺序固定为
`(source_profile,capture_family_id,window_start,window_id)` 的 bytewise 升序；训练 runtime 必须
启用其 exact deterministic CPU profile。EBM 只作 offline glass-box challenger；
未通过 exact ONNX/Triton/ORT profile前不能上线。首期不采用 ensemble、在线 shadow、
weighted route、失败后换模型或 Edge-local fallback。

### 5. 概率、校准与阈值

三个候选的 bundle public output domain 统一固定为 finite float32 probability，class order 固定
`[benign=0,attack=1]`。LR logit、XGBoost margin 与 AE reconstruction score 都只在独立
`calibration` split 上拟合一个二参数 Platt sigmoid `p_attack=sigmoid(a*r+b)`；对 LR/XGBoost
要求 `a>0`，对 AE 同样要求 anomaly score 单调对应 `a>0`，否则候选拒绝。ONNX 图使用标准
`Mul/Add/Sigmoid/Sub/Concat` 或等价已资格化标准算子生成 `[1-p_attack,p_attack]`，不允许
Python/custom op。校准指南要求 calibrator 与模型拟合数据相互独立，本 split 正是该边界。

阈值只在 calibration split 搜索，并严格复用现有 `masi-window-adapter-v1` 决策规则：

- `output_adapter.threshold = τ_alert`；
- `label_taxonomy.threshold.value = output_adapter.ood_policy.threshold = τ_conf`；
- 候选集合来自 calibration 中实际出现的 finite float32 score 加 `{0.5,1.0}`，并要求
  `0.5 <= τ_conf <= τ_alert <= 1.0`；
- feasible tuple 必须同时满足 benign FPR `<=0.01`、attack recall（abstain计未检出）
  `>=0.90`、non-abstained coverage `>=0.95`、benign abstain rate `<=0.05`；
- 按“最高 recall → 最低 FPR → 最高 coverage → 更低 `τ_alert` → 更低 `τ_conf`”稳定排序
  取唯一 tuple；没有 feasible tuple 时该候选 `REJECTED`，不得读 blind test 回调阈值。

这里的 OOD 是现有 adapter 的“最大 canonical probability 低于 `τ_conf`”低置信代理，不是对
未知攻击或真实分布外样本的因果证明。blind/cross-corpus 另报告 OOD、abstain 与 coverage；
不得把 abstain 从 precision/recall 分母中静默删除。

### 6. 选择与质量门槛

超参只读 train/early-stop，阈值/概率只读 calibration，blind test 只在所有候选和 config
冻结后打开一次。三个 seed 必须分别满足：

- `PR-AUC >= 0.95`；
- `precision >= 0.90`、`recall >= 0.90`、`macro-F1 >= 0.90`；
- benign `FPR <= 0.01`；
- `ECE <= 0.05`、`Brier <= 0.10`；
- non-abstained coverage `>= 0.95`、benign abstain rate `<= 0.05`；
- supported family 至少 100 个独立窗口时 recall `>= 0.80`。

指标实现固定为：profile历史字段`PR-AUC`实际定义为scikit-learn `average_precision_score`的non-interpolated Average Precision（报告同时显示`AP`，不声称梯形插值PR面积）；`FPR=FP/(FP+TN)`；binary Brier为attack probability与`0|1` label的mean squared error；ECE使用15个equal-frequency bin并按bin样本占比加权绝对confidence-accuracy gap。zero denominator、缺类或低于family最小样本数分别记`not_measurable|not_applicable`，不得补零。

各 source/scenario 分开报告，不以 pooled average 掩盖失败。三个 mandatory candidate 都必须
实际完成三 seed 训练、导出和评价；某候选质量未达标是可审计的 `REJECTED` 结果，不允许省略，
但不要求失败候选被伪装为通过。一个候选只有三个 seed 在全部 required blind source 上均通过
才是 eligible；其排序分数为 `robust_AP=min(seed × required blind source 的 AP)`。

若最简单 eligible 候选与最高 `robust_AP` 候选的差不超过 0.01，且二者 worst-case FPR
差不超过 0.002，按复杂度 `LR < XGBoost < AE` 选择更简单者；否则依次按最高
`robust_AP`、最低 worst-case FPR、最低 CPU p99、最低 RSS、复杂度顺序取唯一 winner。
若三个candidate都完成但没有eligible candidate，则`winner=none`、不生成可注册/rollout的
bundle，模型质量证据为`result=FAIL, qualification=NOT_QUALIFIED`；尚未执行则为
`result=NOT_RUN`。禁止选“相对最好但不合格”的模型。

以上只是 acceptance candidate profile；目标生产分布、性能、HA或 protected release evidence
缺失时仍为 `HOLD/NOT_QUALIFIED`。ADR-0019首期 recipe 只声明
`model-runtime-central-cpu/v1`；CUDA 对该 exact bundle 为 `NOT_APPLICABLE`，理由是未声明该
runtime。未来某 bundle 显式声明 CUDA 时必须产生独立 numeric/performance/soak evidence，
不能继承 CPU 结果。

### 7. 可解释证据与 Agent/LLM

Offline ML bundle/qualification package 必须生成只读 explanation evidence：

- Logistic：scaled-log空间的`coefficient_j * s_j`、intercept与逐样本加和检查；另外提供只用于
  人类阅读的原单位敏感性摘要，不把它伪装成线性可加贡献；
- XGBoost：`TreeExplainer(model_output="raw", feature_perturbation="interventional")` 的
  exact TreeSHAP；background 从train按source/class/capture-family确定性抽取至多256个样本，
  local sample从blind每source/scenario最多4096个，全部绑定清单/digest。每个样本验证
  `base_value + sum(phi) = raw_margin` 的冻结numeric tolerance；Platt后的概率不是SHAP加和域；
- Autoencoder：scaled-log空间六个SmoothL1 residual及其mean必须等于raw anomaly score，再由
  独立Platt映射概率；residual只是模型重构偏差，不是攻击字段或因果解释。

所有 explanation 都绑定 feature/scaler/model/background/method/tool/version/sample digest、
coverage、truncation、三seed重复性和限制。global重要性按每feature mean absolute contribution
报告；跨seed global rank Spearman中位数低于0.80时标记`unstable`，不得在UI/Agent中写成稳定
原因，但仍保留原结果。解释只在frozen sample上生成，不接受LLM补齐缺失值。

首期 explanation evidence 不进入 inference result、canonical Event identity 或 effect eligibility，
因此本 ADR 不修改 Central/Edge/Go wire。Analysis Agent 只能通过 Go 授权的
Event/model/result/evidence reference 对这些确定性事实生成不可执行叙述，必须区分模型事实、
LLM inference、unknown 和 limitation；不得重新运行模型、伪造 SHAP、把 attribution 写成因果，
或产生可执行处置。

若未来要求逐 Event 在线 feature contribution，必须另行冻结 explanation contract/profile、
producer owner、bytes/top-k/deadline/status 和 Go/Web projection，并重验所有受影响模块；不能把
该扩展藏入 scores、Analysis 文本或插件自定义图表。

### 8. 对已完成模块的影响

本决策只选择 compatible model/data/training profile，不改变现有 `[N,6] -> [N,2]` wire、
output adapter、Event identity、model binding、route、数据库 schema 或 Plugin Host。因此现有
P4/Switch、Edge、Central Inference、Go、PostgreSQL 和 Plugin Host 的 operational completion
不因本文档本身失效。

Offline ML 实现必须用现有 contract/profile 真实验证 XGBoost/Logistic/AE bundle；若发现必须
改变 wire、operator/custom-op policy、output semantics 或 Go projection，则先形成独立 contract
change，届时受影响模块按 ADR-0006 重新运行门禁，不能用本文预先授权。

## 取舍

收益：训练与在线 feature 完全同义；公开数据、legacy 数据和项目 synthetic 各自可追溯；
tabular champion、透明 baseline 和 anomaly challenger 能公平比较；可解释性辅助研判但不把 LLM
带入热路径；首期不迫使已完成模块返工。

代价：必须实现 PCAP→P4-window reference extractor 和 BMv2 golden；当前六维特征只能支持
aggregate-visible binary scope；CIC/UNSW/TON 的许多攻击类型会诚实地保持 not-covered；模型
质量在实际数据、bundle 和目标环境证据产生前保持 HOLD。

## 被拒绝的方案

1. 直接训练旧 MASI-NIDS 21 列或公开 80 列 flow CSV，再让线上六维输入补零适配。
2. 按 row 随机拆分同一 PCAP/攻击时段，制造数据泄漏后的高 accuracy。
3. 只训练 Autoencoder 并把所有高 reconstruction error 命名为已知攻击。
4. 只看 accuracy 或 pooled score，不限制 FPR、calibration、source/family 和 coverage。
5. 为显示 SHAP 立即扩展实时 wire 或让 LLM/Agent 重跑模型。
6. 运行时 ensemble、自动选择 CPU/CUDA/模型或失败后回退另一 candidate。

## 验证与回滚

Offline ML Module gate 必须真实执行四 split、三 seed、三个 mandatory candidate、数据拒绝、
P4/reference golden、ONNX export、Triton/ORT numeric、explanation artifact、fault/resource/security
和3,600秒soak。mandatory execution缺失、runner/导出/验证失败或无eligible winner都会阻断；
某个已完整执行候选的质量未达标记录为`REJECTED`，不要求把三个候选都改写成PASS。

在实现开始前还必须新增并冻结 `dataset-p4-window-binary/v1` 与
`model-explanation-evidence/v1` 的公开schema/profile/golden。当前仓库只有ADR级语义，尚无这两套
机器可读合同；任何设计文档不得把它们表述为已存在的Gate 0 PASS。

回滚本 ADR 的训练选择只影响尚未上线的 candidate recipe；已经注册的 model revision、证据和
数据 manifest 保持 append-only。生产模型回滚仍只能由 Go 创建指向 exact qualified previous 的
新 rollout operation，不回滚到 legacy model、mutable tag 或未资格化候选。
