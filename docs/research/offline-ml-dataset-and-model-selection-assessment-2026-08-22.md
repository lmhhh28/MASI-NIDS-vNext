# MASI-NIDS vNext 离线训练数据集与模型方案评估

- 评估日期：2026-08-22
- 性质：成熟方案、公开数据集与 legacy 数据资产的独立评估；不替代需求基线或 ADR
- 当前需求基线：`vNext-requirements-1.19`
- 结论落点：ADR-0019
- 当前资格：source selection与训练recipe已冻结；exact dataset revision、训练与真实bundle均为`result=NOT_RUN, qualification=NOT_QUALIFIED`

## 1. 问题与硬约束

首期模型不能按公开数据集现成 CSV 的字段任意选特征。在线生产输入已由
`edge-feature-p4-window/v1` 固定为一个 10 秒 final window 的六个 `uint64-le`
字段：`aggregate_packets`、`aggregate_bytes`、`reported_nonzero_cells`、
`max_cell_packets`、`max_cell_bytes`、`snapshot_count`。P4 每个 bank 只有 256 个
CRC32 hash cell，碰撞属于公开质量语义，不是精确 flow map。

因此，训练数据的唯一合法样本形状必须是通过同一 P4 selector、bank/window 和
feature golden 得到的 `[1,6]` canonical window。CICFlowMeter、Zeek、Argus 或 legacy
21/38/80 列 flow CSV 可以帮助定位 ground truth，却不能直接成为 vNext 在线模型输入。

当前 `InferenceResult` 固定输出 class-ordered float scores、label、decision、OOD、
abstain 和 quality。首期训练方案必须保持 `[N,6] -> [N,2]` 的二分类输出，才能在不修改
已经完成的 Central Inference、Edge、Go 和 PostgreSQL 模块公开边界的情况下交付新
immutable bundle。

## 2. Legacy 数据资产审计

旧仓库 `/home/lmhhh/MASI-NIDS/dataset` 当前有30个CSV、约736万行：CICIDS-2018
`server1/server2.csv`约361万行、UNSW-NB15四个无header CSV约254万行、TON-IoT 22个
CSV约120万行。目录内没有逐artifact license/privacy/ground-truth sidecar，也没有PCAP。
旧 importer实际只消费TON-IoT并投影为另一套21列flow feature；旧Mininet/AE训练再消费
`features.npy/labels.npy`和3/12类映射，并非vNext P4六维窗口。

旧TON-IoT处理run虽有source attestation、清洗与采样记录，但其用途限定为旧competition demo，
所依赖trust key在2026-08-18到期且状态为staged；它不能自动升级为vNext dataset revision。
旧CICIDS-2018/UNSW-NB15副本还没有可核验的官方artifact digest或许可链。上述计数仅是本地
资产盘点，不是接纳或资格证据。

结论固定为：

- `REJECT direct import`：不得复制旧 CSV/NPY、旧 scaler、旧 class map 或旧 model 到
  `ml-py/`、仓库、OCI 或 production bundle；
- 旧目录只能作为人工定位原始数据集名称和 legacy 行为对照；
- 需要使用同名公开数据时，必须从官方入口重新摄取，保存取得时间、原始 digest、
  license/use/redistribution 判定和 canonical ground-truth join；
- legacy 21 列与 vNext 6 维窗口不得通过补零、重命名或隐式 adapter 混用。

## 3. 公开数据集比较

| 数据集 | 官方事实 | 许可/用途边界 | 与六维 P4 窗口的适配 | 首期角色 |
|---|---|---|---|---|
| [CIC-DDoS2019](https://www.unb.ca/cic/datasets/ddos-2019.html) | 按天提供 PCAP、日志和 CICFlowMeter-V3 flow CSV；训练/测试日含多个 DDoS 家族，PortScan 只在测试日出现 | 官方明确允许任意形式再分发、再发布和镜像，但必须引用数据集及论文；payload/privacy 仍需项目门禁 | PCAP 可经 exact selector/window 重提取；攻击以体量、集中度、fan-out 为主，和当前聚合特征最匹配 | 主要外部 train/calibration/blind corpus |
| [CSE-CIC-IDS2018](https://www.unb.ca/cic/datasets/ids-2018.html) | 50台攻击机、420台组织机器、30台服务器，提供PCAP/日志/80项flow feature与七类攻击 | 官方允许再分发/再发布/镜像，但要求引用并链接官方AWS页面；仍需payload/privacy审查 | 仅DoS/DDoS/Brute-force等aggregate-visible slice适配，广泛L7标签不适配当前六维输入 | 官方重摄取后的cross-corpus blind evaluation；不训练 |
| [CIC-IDS2017](https://www.unb.ca/cic/datasets/ids-2017.html) | 五天 PCAP + 80 余项 flow feature；周一只有 benign，后续含 Brute Force、DoS/DDoS、Web、Infiltration、Botnet 等 | 官方称研究者公开可用并要求引用，但没有给出足以自动授权商业再分发的标准许可文本 | 仅 DoS/DDoS/PortScan 等可观察 slice 适配；XSS/SQLi/Heartbleed/Infiltration 的攻击语义不能由六维无 payload 聚合证明 | 许可通过后的 cross-corpus blind evaluation；不进入首期训练 |
| [UNSW-NB15](https://research.unsw.edu.au/projects/unsw-nb15-dataset) | 约 100 GB PCAP、49 features、9 类攻击；官方 train/test 为 175,341/82,332 records | 学术研究永久免费，商业用途须与作者协商 | 必须从 PCAP 重提取并重新做 window ground-truth；官方 row split 不能替代 vNext capture-family split | `CONDITIONAL` 外部验证；不训练、不随产品分发 |
| [TON_IoT](https://research.unsw.edu.au/projects/toniot-datasets) | 含 IoT/IIoT telemetry、Windows/Linux audit、network PCAP/Zeek/CSV、ground-truth timestamp 和 train/test 样本 | 学术研究永久免费，商业用途须询问作者 | 是多模态 IoT corpus，不能把 legacy CSV 字段直接映射到 P4 六维窗口 | `CONDITIONAL` IoT domain validation；不训练 |
| [CICIoT2023](https://www.unb.ca/cic/datasets/iotdataset-2023.html) | 105台真实IoT设备、33种攻击/7类，提供PCAP、CSV、notebook与feature extractor | 官方页面未给出足以直接支撑项目再分发/商业使用的明确license段，需单独核准 | 设备/协议分布对IoT profile有价值，但不能代表当前通用BMv2/enterprise scope | 未来IoT profile的条件blind evaluation；当前不训练 |

数据集公开可下载不等于：允许进入产品、允许提交 raw payload、允许用 flow row 当 packet
truth，或能够外推生产网络。所有 external corpus 指标必须按 source/scenario 单独报告，不与
项目 synthetic 指标池化成一个好看的总分。

首期选择CIC-DDoS2019而不是更宽泛的CSE-CIC-IDS2018作为拟合主集，是因为当前六维特征只
能观察packet/byte volume、非零bucket、最大bucket与snapshot数量；DDoS/flood与这一观察面
最匹配，而且官方给出PCAP、分日训练/测试和明确再分发条款。CSE-CIC-IDS2018覆盖更广，但
大量Web/Heartbleed/infiltration语义无法从六维无payload窗口证明，放入训练会制造虚假coverage。
该选择不表示CIC-DDoS2019代表生产流量；项目synthetic与未来目标网络数据仍需独立门禁。

## 4. Canonical 数据产品

训练消费的数据产品命名为 `dataset-p4-window-binary/v1`，每行至少包含：

- canonical `[1,6] uint64-le` feature bytes、feature digest 和 window identity；
- source/corpus/revision/PCAP/fixture/rewrite/selector/profile digest；
- target/topology/observation point、10 秒半开窗口、bank/epoch/snapshot quality；
- ground-truth source、packet/flow-to-window join、coverage 和 supported attack family；
- stable `capture_family_id`、`scenario_id`、`session_id` 和 split；
- binary label `benign|attack`，另存非生产的 attack-family metadata；
- `valid|mixed|ambiguous|unknown|not_covered` 标签质量；
- license/use/redistribution/privacy/redaction/retention 判定。

大型 PCAP 不进入 Git 或普通 OCI。受控 ingestion job 下载到隔离存储，先校验原始 digest，
再通过两条独立路径产生相同 golden：

1. exact BMv2/P4 replay 后读取 frozen bank；
2. 离线 reference extractor 复刻同一 CRC32 selector、256-cell、bank 和 10 秒 window。

只有最小 golden slice 要求两条路径逐字段一致；大型训练可使用已通过该 golden 的离线
extractor。任何 selector、window、P4Info 或 feature digest 变化都创建新数据 profile，不能
沿用旧数据。

## 5. Split 与泄漏防护

禁止逐 row/window 随机切分。分组身份至少为：

```text
source revision + raw capture digest + scenario + session
+ endpoint pair/topology + non-overlapping time segment
```

固定四个互斥 split：

1. `train`：拟合 scaler/model；
2. `early_stop`：超参选择与 early stopping；
3. `calibration`：概率校准、alert/OOD/abstain threshold；
4. `blind_test`：选择结束前不可读取结果，不参与任何训练或阈值调整。

CIC-DDoS2019 官方 training/test 边界优先于项目 hash split：官方 test 部分全部进入
`blind_test`；官方 training 部分再按 capture family 的稳定 hash 分配到前三个 split。
项目 synthetic corpus使用不同 scenario、seed、topology 和 capture identity分割。IP、port、
timestamp、Flow ID、文件名和攻击时间段只用于 grouping/ground-truth，不进入模型特征。

具体比例为：synthetic `60/15/15/10`，CIC-DDoS2019 official-training `70/15/15/0`，
official-test `0/0/0/100`。分配值使用ADR-0019固定的SHA-256 capture-family hash；任一必需
source×label缺类或family跨split时拒绝整个dataset revision，不移动单window补比例。

拟合 split 只使用 ground-truth coverage 完整的高纯度窗口：benign 窗口必须无攻击 packet；
attack 窗口中 supported attack packet share 必须至少 90%。其余 mixed/ambiguous 窗口不参与
拟合，但必须在 blind/operational evaluation 中单列 coverage 和结果，不能静默删除。

标签join使用packet timestamp与direction-aware canonical 5-tuple/protocol关联官方flow/event；
窗口固定`[start,end)`。只有eligible packet标签coverage为100%的窗口可拟合。时间段命中但tuple
无法确认、重叠攻击或冲突标签为`ambiguous|unknown`。attack share以窗口eligible packet为
分母，不以CICFlowMeter row或P4 hash cell为分母。

## 6. 模型候选比较

| 候选 | 优点 | 风险/边界 | 决策 |
|---|---|---|---|
| Logistic Regression | 小、稳定、raw-margin contribution可精确加和、易转ONNX | 表达能力有限 | mandatory transparent baseline |
| XGBoost | [KDD论文](https://doi.org/10.1145/2939672.2939785)验证的成熟tabular boosted-tree；可输出margin并用TreeSHAP解释 | ONNX-ML、calibration graph、output tensor与CPU operator必须真实验证；不自动取得CUDA资格 | first production champion candidate |
| Dense Autoencoder | benign-only anomaly detection，逐特征reconstruction residual可解释；保留legacy增量AE的有价值思路 | threshold/污染/缩放敏感；异常不等于已知攻击；在线自更新会破坏immutable identity | mandatory anomaly challenger，offline-only training |
| EBM | glass-box GAM，原生 global/local term contribution | 未核实项目所需的标准 ONNX 导出与 Triton/ORT profile | offline-only challenger，`CONDITIONAL` online |
| 更深 MLP/Transformer | 可拟合复杂非线性 | 六维输入下收益未证，解释/资源/漂移资格面更大 | 首期不采用，除非上述候选全部未达门槛且形成新评估 |

[ONNX Runtime 官方文档](https://onnxruntime.ai/docs/tutorials/traditional-ml.html)说明其支持
ONNX-ML；[ONNXMLTools官方项目](https://github.com/onnx/onnxmltools)提供XGBoost等模型的
ONNX转换入口，但这两项都不能替代exact graph的项目数值门禁。
[SHAP TreeExplainer文档](https://shap.readthedocs.io/en/stable/generated/shap.TreeExplainer.html)
说明TreeSHAP对树ensemble提供快速exact attribution，并要求明确feature dependence/background
假设；因此项目固定interventional background和raw-margin additivity，不把SHAP称为因果解释。
[InterpretML EBM 文档](https://interpret.ml/docs/ebm.html)说明 EBM 是可输出逐 term
contribution 的可解释加性模型；这不等于其已通过项目 ONNX runtime 资格。

[Kitsune/KitNET](https://arxiv.org/abs/1802.09089)证明ensemble autoencoder可用于在线无监督
NIDS，但它依赖自身packet/channel feature extraction与在线学习语义。MASI vNext只借鉴
“benign reconstruction anomaly”模式：训练在Offline ML中进行，每次新增数据形成新immutable
revision；Central/Edge不得`partial_fit`或自更新current。

## 7. 推荐训练与选择策略

首期任务固定为 `benign=0, supported aggregate-visible attack=1`。XSS、SQL injection、
Heartbleed exploit、ransomware payload 和内容型 infiltration 在当前 feature profile 下为
`not_covered`，不得因公开 corpus 有该标签就宣称检测支持。

共同 preprocessing 为 raw `uint64` 在 ONNX 图内 `Cast(float32) -> log1p`；线性模型和
autoencoder 的中心/尺度只从 `train` 拟合并进入 bundle。训练权重按
`source×label → capture_family → window`三级等权归一化，禁止 DDoS 大文件或重复相邻窗口
支配目标函数，也禁止复制样本或用SMOTE跨group合成样本。

- `lr-window-binary/v1`：加权StandardScaler + L2 Logistic Regression，固定`lbfgs`、
  `max_iter=1000`、`tol=1e-6`，只搜索`C={0.01,0.1,1,10}`；同时提供SGD `partial_fit`
  结果作为大数据增量训练一致性对照，但 production
  artifact 仍是一个 immutable full-fit revision；
- `xgb-window-binary/v1`：deterministic CPU `hist`、`max_bin=256`、最多512 rounds、patience32；
  grid只含`depth={3,6}`、`eta={0.03,0.1}`、`min_child_weight={1,5}`、
  `lambda={1,10}`，subsample/column sample均为1；raw margin经Platt校准，导出ONNX-ML时
  关闭ZipMap，输出canonical `[N,2]` probability，不允许custom op；
- `ae-window-benign/v1`：`6 -> 8 -> 3 -> 8 -> 6` undercomplete autoencoder，仅用 benign
  train window拟合scaler和模型，CPU deterministic、batch1024、SmoothL1 beta=1、AdamW、
  最多200 epoch/patience20，搜索`lr={3e-4,1e-3}`、`weight_decay={0,1e-4}`；raw score为
  六维scaled-log residual的mean，在独立calibration split用Platt sigmoid映射为
  `[benign,attack]` probability。mini-batch/追加数据
  只是离线增量训练方式，每次训练产生新 immutable revision，绝不在线更新 current model。

固定 seed set 为 `17,29,43`。超参搜索只读 `train/early_stop`；校准只读 calibration；
blind test 在所有候选和阈值冻结后一次打开。首期上线只选择一个 exact bundle，不做 ensemble、
weighted route、在线 shadow 或失败后模型 fallback。

三个候选统一在calibration split把raw score拟合为`p_attack=sigmoid(a*r+b)`并要求`a>0`；
ONNX最终输出`[1-p,p]`，score domain只允许`probability`。`τ_alert/τ_conf`按ADR-0019的
finite-score穷举与稳定tie-break选择，满足FPR、recall、non-abstained coverage和benign abstain
上限；`τ_conf`同时绑定现有adapter的label threshold与max-probability OOD threshold。它只是
低置信代理，不证明未知攻击。阈值、Platt参数和score domain都进入bundle digest。

## 8. 初始 acceptance quality profile

以下是 acceptance/Module 候选的初始绝对门槛，不外推为 production target-distribution SLO：

- blind-test `PR-AUC >= 0.95`、`precision >= 0.90`、`recall >= 0.90`、`macro-F1 >= 0.90`；
- benign `FPR <= 0.01`；alert threshold 在 calibration split 上选择满足该 FPR 上限的最高 recall；
- `ECE <= 0.05`、`Brier score <= 0.10`；
- non-abstained coverage `>=0.95`、benign abstain rate `<=0.05`；
- supported family 独立窗口数至少 100 时，per-family recall `>= 0.80`；
- 三个 seed 分别通过，不用平均值掩盖失败；
- synthetic 与每个 external corpus 分开报告；mixed/ambiguous/not-covered 报 coverage，不补零；
- Logistic、XGBoost、Autoencoder均生成完整结果；候选必须三个seed和全部required blind source
  分别通过才eligible，排序使用worst-seed/worst-source `robust_AP`。若最简单eligible候选与最佳
  候选的`robust_AP`差不超过0.01且worst FPR差不超过0.002，选择更简单者，否则按
  robust AP、FPR、CPU p99、RSS和复杂度取唯一winner。没有eligible候选时`winner=none`，
  不发布“相对最好”的bundle。

指标算法与ADR-0019一致：历史profile字段`PR-AUC`实际使用`average_precision_score`并同时显示为`AP`，不声称梯形插值PR面积；FPR为`FP/(FP+TN)`；binary Brier使用attack probability；ECE使用15个equal-frequency bin。zero denominator、blind split缺类或family样本不足必须输出`not_measurable|not_applicable`和稳定原因，不得当作0或PASS。

目标生产流量、绝对 p99/throughput、长期漂移和 hardware/HA 未形成前，资格仍保持
`HOLD/NOT_QUALIFIED`。

## 9. 可解释性与 Agent 边界

Offline ML 必须生成版本化、只读 explanation evidence：

- Logistic：标准化空间和原始单位下的 coefficient/contribution；
- XGBoost：`model_output=raw`、`feature_perturbation=interventional`，从train按group稳定抽取
  至多256个background，blind每source/scenario至多4096个local sample；验证raw margin
  additivity，Platt概率不冒充SHAP加和域；
- Autoencoder：scaled-log空间逐特征SmoothL1 residual，其mean必须等于raw anomaly score，
  再经Platt映射概率；
- 三类解释都报告coverage/truncation、三seed重复性与global rank稳定性；Spearman中位数
  `<0.80`时标记`unstable`并限制UI/Agent措辞。

这些是模型资格和人工研判证据，不进入实时检测/effect，也不修改首期 inference wire。
Analysis Agent 只能消费 Go 已授权的 Event/model/result/evidence reference，区分“模型原始事实”
和“LLM 推断”，用自然语言解释限制；它不能重新运行模型、计算或伪造 SHAP，也不能把
association 写成攻击因果。若未来要求逐 Event 在线 contribution，必须先新增版本化 contract、
资源上限和 producer/consumer 资格，再重验受影响模块。

## 10. 仍然阻断资格的事项

- 官方下载 artifact、exact digest、citation/license decision 和 privacy review 尚未形成；
- `dataset-p4-window-binary/v1` ingestion/extractor/golden 尚未实现；
- `dataset-p4-window-binary/v1`与`model-explanation-evidence/v1`机器可读schema/profile/golden尚未创建；
- XGBoost ONNX-ML `[N,2]`、Triton config 与 ORT 1.19 CPU operator closure 尚未实测；
- Autoencoder calibration、跨 seed、cross-corpus 与 not-covered coverage 尚未实测；
- `ml-py/` release image、bundle、README、fault/performance/soak evidence 尚不存在；
- 首期recipe只声明CPU；CUDA为未声明条件能力而非已有支持，未来声明时必须独立资格化；
- acceptance 阈值不替代目标生产分布、single/HA 和 production performance 资格。
