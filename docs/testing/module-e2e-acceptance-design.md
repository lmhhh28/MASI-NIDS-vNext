# MASI-NIDS-vNext 模块独立 E2E 验收设计

- 文档状态：`DRAFT`
- 日期：2026-08-22
- 需求基线：`vNext-requirements-1.19`
- 主要需求：`TEST-002`、`TEST-003`、`TEST-INF-001`、`TEST-WEB-001`、`TEST-PLUGIN-001`、`TEST-PLUGIN-STAT-001`、`TEST-GATE-001`、`TEST-REAL-E2E-001`、`TEST-007`、`TEST-008`、`TEST-009`、`TEST-010`、`TEST-RULE-001`、`TEST-P4-FW-001`、`TEST-TARGET-FLEET-001`、`TEST-TRAFFIC-001`、`TEST-REUSE-001`、`TEST-TEL-INF-001`
- 证据语义：ADR-0006

## 1. 文档定位

本文是验收设计，不是实现教程、测试脚本或运行手册。它只规定：

- 哪个资格主体必须真实启动或真实执行；
- 需要验证的能力、场景和故障；
- 必须保持的不变量与可观察oracle；
- 必须保存的证据；
- 哪些结果阻断Module Complete。

本文不规定内部类/函数、代码组织、具体命令、逐步操作或测试框架实现。模块实现形成后，其README提供可复制命令；`e2e-runner-compose/v1`负责可重复编排，但runner health和退出码不决定业务PASS。

## 2. 资格字段与判定

每条测试证据分别保存：

| 字段 | 合法值 | 含义 |
|---|---|---|
| `level` | `REHEARSAL|MODULE|PAIRWISE|SYSTEM_E2E|PRODUCTION` | 证据产生的门禁层次 |
| `applicability` | `APPLICABLE|NOT_APPLICABLE` | 条件能力是否适用于exact claim scope |
| `result` | `PASS|FAIL|HOLD|NOT_RUN` | 实际执行结果 |
| `qualification` | `QUALIFIED|NOT_QUALIFIED` | exact claim scope能否获得资格 |

Claim scope至少绑定release、module、runtime、availability、deployment tier、topology、environment/profile、artifact/image/config/contract digest。CPU/CUDA、single/HA、BMv2/硬件和不同topology不得继承结果。

Required且`APPLICABLE`的任一项为`FAIL|HOLD|NOT_RUN`，或缺少原始证据，都会阻断对应 exact scope 的资格 aggregate。`NOT_APPLICABLE`只用于未触发的条件能力，必须有稳定机器可读理由；不得用于release已声明支持的能力。Operational Module Complete 另按 `DEC-044` 派生：资格专属的 protected-baseline/dirty-tree/production-threshold/future-integration HOLD 不改变实际模块测试结论，也不充当 operational blocker。

## 3. 模块黑盒 E2E 的共同约束

每个模块的Module E2E必须满足：

1. 被测资格主体使用发布候选binary/OCI、实际runtime和production-equivalent配置真实启动或执行。
2. 输入输出只通过公开contract、network/database/file artifact boundary；不导入内部实现，不以私有函数或stdout为主要oracle。
3. 邻居可以是contract-consistent fake，但被测模块内部职责、runtime和核心依赖不得fake。
4. 场景覆盖min/typical/max、success、invalid、timeout、duplicate、out-of-order、oversize、resource saturation和recovery。
5. Startup/readiness/liveness/drain/shutdown都被观察；ready只表示可继续测试，不等于业务PASS。
6. Fault、performance、security、compatibility、supply-chain和cleanup evidence与功能证据同scope关联。
7. 测试环境、identity、network、database和target与production隔离；破坏性数据库名称明确包含`test`。
8. 任何fallback、自动换runner/backend/profile、跳过或证据缺失都明确报告，不用另一路径补成PASS。

## 4. 共同证据包

每个模块至少保存：

- module/release/artifact/image/config/runtime/profile/contract digest；
- 实际process/container/service inventory及identity；
- topology、port、certificate/workload identity和resource limits；
- startup→ready→drain→stop时间线；
- input fixture/golden/fault/environment digest；
- raw structured result、oracle observation、error/status和timing；
- CPU/RSS/FD/thread/connection/queue/disk/WAL及适用GPU/VRAM/network；
- fault pre/intermediate/post state与最终不变量；
- logs/traces/artifacts引用及hash、cleanup结果；
- requirement/test mapping和四个资格字段。

截图、dashboard、人工说明或JUnit摘要只能作为辅助引用，不能替代结构化原始结果。

<a id="module-p4"></a>

## 5. P4/Switch 模块验收

### 5.1 资格主体

真实BMv2 `simple_switch_grpc`、实际P4 program/BMv2 JSON/P4Info、exact v1model/p4c/port/target profile。模块测试允许test-only P4Runtime control fixture；production writer资格不由该fixture获得。

### 5.2 必验能力与场景

- parser/forwarding、IPv4/IPv6 observation、fragment/L4 availability和error语义；
- response overlay、两个baseline bank、selector、explicit default、priority/conflict/shadow、permit-and-continue/drop；
- empty/128/1,024/4,096 normalized rule和最坏合法展开/资源拒绝；
- direct rule counter、eligible counter、inactive/active bank identity、wrap/saturate/reset；
- telemetry aggregate bank/epoch/snapshot/read-clear和Digest/PacketIn best-effort sample/drop；
- generated packet的exact ingress/egress/drop/mirror oracle；适用的synthetic/PCAP/live-session边界；
- partial bank、selector response loss、restart/P4Info drift、host-filter contamination；
- packet/control-plane性能、资源饱和和soak。

### 5.3 不变量与Oracle

- incomplete inactive bank不能改变active packet behavior；
- selector唯一决定active baseline，overlay precedence不变；
- Write ACK不等于installation，counter hit不等于outcome；
- aggregate普通多Read不冒充atomic snapshot，Digest Ack不冒充coverage；
- host UFW/nftables/iptables/eBPF不能制造P4 PASS；
- 结果只资格化exact BMv2软件环境。

### 5.4 阻断条件

没有真实packet、没有独立outcome oracle、只有p4c编译/CLI、缺4,096-rule门槛、无法证明host path或将BMv2外推硬件，均阻断完成。

<a id="module-edge"></a>

## 6. Rust Edge 模块验收

### 6.1 资格主体

真实Rust release binary/OCI和实际filesystem/WAL/runtime。邻居可为多Fake/real P4Runtime target、qualified mirror fixture、Fake Central Gateway与Fake Go。

### 6.2 必验能力与场景

- 1/2/N target actor、assignment lease/election range、arbitration、handoff、old actor fence；
- per-target journal/source/queue隔离和global fairness/priority；
- firewall normalized compile/preflight、overlay/bank/selector effect journal/readback；
- P4 aggregate/Digest/PacketIn、event-time/watermark/finality/late/quality、IPv4/IPv6/fragment/flow direction；
- source/input/result三阶段WAL、checkpoint/compaction、crash/partial tail/replay；
- central batched-unary mTLS、no-delay coalescing、route generation、retry/dedupe/backpressure/full-pool gap；
- Edge→Go result/observation batch、commit ACK loss、same-key conflict；
- rule sweep/reset/generation/gap和effect优先级；
- disk/FD/queue/connection/reconnect storm、performance和soak。

### 6.3 不变量与Oracle

- 每target只有对应actor持有production P4 session/write；
- revoke/expiry后零P4 Write，新assignment使用更高election floor；
- final+valid窗口才发送inference，缺测不补零；
- Go/PostgreSQL canonical ACK前不回收canonical WAL/cursor；
- 同shard只有一个exact pool generation route；
- 无Edge-local inference、第二source、第二queue或core DB write；
- slow target不突破健康target deadline与effect优先级。

### 6.4 阻断条件

仅单target、memory-only queue、无真实WAL crash、无network gRPC、未证明lease/election fence或full-pool no-fallback，均阻断完成。

<a id="module-central"></a>

## 7. Central Inference 模块验收

### 7.1 资格主体

真实C++ Gateway、pinned Triton、所选ORT CPU或CUDA、exact read-only repository和selected compute profile共同启动。Fake Edge/Go可作为邻居；Gateway/Triton/runtime中任一不得fake。

### 7.2 必验能力与场景

- mTLS/framing/message/tensor/offset/shape/dtype/digest、quota/deadline/cancellation；
- explicit profile selection、selected=observed、CPU/CUDA硬件/config/resource正负例；
- NONE repository exact closure、extra model/version/backend拒绝、explicit instance group、load/warmup/readback；
- numeric/class order/threshold/OOD/abstain/NaN/Inf和Python reference golden；
- Gateway admission、Triton dynamic batch/queue/instance group、CPU thread/NUMA或CUDA provider/copy/I/O Binding；
- duplicate/retry/conflicting input/output、wrong/late generation；
- Gateway/Triton/ORT crash/hang/OOM、full-pool outage、restart/quarantine；
- cold/warm/steady/peak/rolling/saturation/3,600 秒 soak。

### 7.3 不变量与Oracle

- loaded/Ready不等于model current；
- Gateway无durable queue/第二batch timer，Triton是唯一delayed batcher；
- 同generation不混CPU/CUDA，错误/过载不触发profile/backend/model fallback；
- repository无runtime mutation和可执行扩展；
- 模块没有P4/core DB/effect credential；
- CPU/CUDA证据分别产生，不互相继承。

### 7.4 阻断条件

Fake Gateway、只测ORT/Triton、只有Ready、没有actual hardware envelope、没有numeric/absolute performance、或自动profile fallback路径存在，均阻断完成。

<a id="module-go"></a>

## 8. Go Control 模块验收

### 8.1 资格主体

真实Go release binary/OCI和真实PostgreSQL test实例。邻居可为Fake Edge/target、Central/deployment adapter、Plugin Host、Analysis adapter和OIDC context。

### 8.2 必验能力与场景

- InferenceResult→Event identity/digest/idempotency、commit-before-ACK、replay/conflict；
- Event/Incident/Evidence和deterministic eligibility/risk；
- R0/R1自动policy、R2maker-checker、baseline typed R3、self-approval/stale/expiry/scope/capacity零Edge RPC；
- effect intent claim/fence/external timeout/readback/finalize/reconcile；
- firewall revision/default/current-previous/overlay expiry/dual-bank timeline；
- target candidate/identity/lifecycle/assignment/incarnation和fleet parent/child/waves/failure policies/partial；
- rule epoch/sample/delta/formula/quality/current/rollup/retention；
- model revision/qualification/pool/shard binding/rollout/readback/CAS/recovery/rollback/PITR；
- plugin catalog/qualification/binding/revoke和statistics definition/freeze/run/idempotency/Artifact/current/history/auth；
- OIDC/session/CSRF/scope、OpenAPI/cursor/SSE、timeout/original operation；
- DB failover/pool/deadlock/resource/performance/soak。

### 8.3 不变量与Oracle

- Go是核心schema唯一业务writer，external wait期间无active transaction/held connection；
- Proposal/Decision/fleet parent不可claim，只有effect_intent可执行；
- statistics run不进入effect dispatcher，插件不写projection；
- loaded/deployment ready不成为model current；
- unknown副作用只沿原operation reconcile，不盲写；
- all API mutation以server authorization为准，route可见性不授权。

### 8.4 阻断条件

使用内存DB、硬编码OIDC/approve、外部RPC持有transaction、stub manager/dispatcher、第二queue或未覆盖真实PostgreSQL migration/failover，均阻断完成。

<a id="module-postgresql"></a>

## 9. PostgreSQL State 模块验收

### 9.1 资格主体

真实PostgreSQL 18 image/config、migration job、schema fixtures、backup/WAL和适用HA/restore topology。Schema/constraint/restore不得fake。

### 9.2 必验能力与场景

- empty/previous→current、重复/中断/checksum drift、expand/contract和reader-writer matrix；
- core/effect/firewall/rule/target/fleet/model/plugin/statistics的FK/unique/CAS/append-only/retention；
- Event partition、cursor/index、5m/1h rollup和高基数数据边界；
- role/credential deny棋盘、TLS、PgBouncer session-feature负例；
- connection/pool/lock/deadlock/timeout/disk/inode/WAL资源；
- failover、backup/archive、PITR/restore/clone/rewind、incarnation rotation、RPO/RTO；
- restore期间零P4/plugin/model action和schedule不自动执行。

### 9.3 不变量与Oracle

- Go以外无核心业务DML writer；
- migration/trigger/backup工具不执行外部副作用；
- fleet parent无claim，statistics ledger与effect queue分离；
- PITR后旧assignment/model envelope/fence失效；
- current引用immutable exact revision，history/audit不被原地改写。

### 9.4 阻断条件

只测fresh schema、只有单机ready、无previous migration/restore、未轮换incarnation或未验证deny roles，均阻断完成。Production claim另要求production-ha全部证据。

<a id="module-plugin-host"></a>

## 10. Plugin Runtime Host 模块验收

### 10.1 资格主体

真实Rust Host OCI/binary、pinned Wasmtime/WASI 0.2和实际resource sandbox。可使用Fake Manager/capability provider及signed/invalid Wasm/service/statistics fixtures。

### 10.2 必验能力与场景

- strict manifest的artifact/Host API/input-output/config/lifecycle/compatibility/statistics definition exact identity，以及kind/version/digest/publisher/provenance/capability；
- `wasm-component/v1` WIT和`grpc-service/v1` Host-managed service；
- staged/shadow/active、普通activation generation单调、显式授权rollback、drain/revoke/late result；
- statistics exact definition revision/digest routing、完整frozen input identity/digest闭包、typed Artifact/display contract；
- filesystem/network/secret/DB/P4/effect deny；
- fuel/CPU/memory/PID/FD/disk/deadline/output/queue上限；
- trap/crash/OOM/hang/cancel、disable/drain失败与超时、trust freshness reconcile、restart storm/circuit/quarantine、Manager/Host restart；
- 全部插件disabled和故障风暴下核心隔离/performance/soak。

### 10.3 不变量与Oracle

- Manager拥有canonical lifecycle，Host只执行observed binding；
- 内存queue可重建但不成为durable ledger；
- Host不代理Analysis direct A2A/MCP业务；
- 插件零核心DB/P4/effect mutation；
- unknown kind/runtime/WASI major和capability扩张fail closed。

### 10.4 阻断条件

Wasm hello-world、无sandbox负例、只校验manifest、Fake Host、或未证明Host crash不影响core，均阻断完成。

### 10.5 当前 operational 证据

截至2026-08-21，`plugin-host-rs/evidence/module-gates/latest.json`指向immutable run `plugin-host-formal-20260821-004`，summary digest为`sha256:bef1a250afdd7cceef6f56065f7fdae627f2795f49db970e7e74801a34de8d50`，source-tree digest为`sha256:965eae8ee05abed795deb0c6f8b5820e00bc12559fea18260e4776ec533eb868`。19项required gate、真实release/OCI、两种runtime黑盒、严格contract/golden、service与Wasm故障矩阵、59.801%覆盖率、性能、深度与供应链检查均PASS；正式soak完成60秒warmup和4×900秒阶段，记录2,368,993次成功调用、2,283,115次有类型饱和拒绝、0未分类失败、0 trust refresh失败，真实service crash/restart oracle均为true，368个资源样本内queue/in-flight最终归零并成功清理。open finding/open P0均为0，机器派生`overall_module_complete=true`。

该结论只表示operational Module Complete。summary仍为`qualification=NOT_QUALIFIED`；Go Plugin Manager/PostgreSQL正式pairwise、扩展strict input bundle的Go producer wire资格、core peak相对退化、protected release baseline、production-ha与九模块global gate仍保持`HOLD/NOT_RUN`。`-001`覆盖率失败、`-002`语义审计后撤回、`-003` rustdoc gate失败的证据均原样保留；只有修复后从零执行的`-004`是当前latest。

<a id="module-analysis"></a>

## 11. Python Analysis 模块验收

### 11.1 资格主体

真实Python 3.12+ release OCI/process和官方`masi.analysis.langgraph`实现。邻居可为Fake LLM/MCP/A2A，但Analysis、LangGraph、A2A/MCP client逻辑不得fake。

### 11.2 必验能力与场景

- A2A version/task/polling/no-push/no-streaming/identity/idempotency；
- restricted MCP allowlist/read-only/scope/provenance/timeout和mutation拒绝；
- evidence sufficient、bounded tool补证、insufficient evidence、low quality、provider/tool timeout/malformed；
- Go授权的model result事实、可选offline explanation引用、只有结果时的非XAI解读、缺失/过期/低coverage/truncated explanation和伪造attribution拒绝；
- facts/inferences/unknown/citations/limitations/outcome和Artifact digest；
- prompt injection/oversize/secret leakage/data exfiltration；
- Manager binding/revoke/fence和direct path不经Host；
- `AGENT-COMPAT-001`全矩阵、resource/fault/performance/soak。

### 11.3 不变量与Oracle

- Artifact不可执行且不能创建Proposal/Decision/Intent/P4 action；
- Analysis只写隔离自有schema或无状态，不写核心DB；
- LLM/MCP/A2A不进入实时检测/effect；
- model result/explanation facts与LLM inference分层；Analysis不重跑模型、不生成SHAP/残差、不把attribution写成因果；
- Provider fixture证据不冒充真实provider qualification；
- 未完成compatibility matrix不声称完整兼容。

### 11.4 阻断条件

只测graph节点、只生成文本、未真实启动A2A/MCP边界、缺grounding/安全负例或使用pytest违反项目约束，均阻断完成。

### 11.5 当前 operational 证据

截至2026-08-22，`analysis-py/evidence/module-gates/latest.json`指向immutable run `analysis-formal-20260822-004`，summary digest为`sha256:fe07406b4e0ab2d8a57bc4ad8245ef0035b3fcfc3e996d09f1ec2c07a583049e`，source-tree digest为`sha256:9194f01dd5f3e6c040b0a10e4a015d9476eb2f47d3ecc69b45182f2aa76a8d0d`。19项required gate全部PASS，真实release process与immutable image `sha256:7d699ab2825f4c29a2d7f2cde72fa0cb384cbdee5415860f7e9b047810bec9ce`实际启动；A2A/MCP/provider边界、compatibility、fault、performance、OCI最小权限、供应链、55条需求追踪与negative evidence-integrity self-test均通过，open finding/open P0为0。

正式soak完成60秒warmup和steady/peak/saturation/recovery各900秒，记录10,252个completed task、6,040个typed rejection、0 failed、0 HTTP error、0 unclassified error；计划内crash/restart、未完成任务fence、queue/in-flight最终归零、RSS/FD增长和cleanup均通过。summary机器派生`overall_module_complete=true`，但保持`qualification=NOT_QUALIFIED`：deterministic provider fixture不替代真实外部provider资格，P10/P11、Web展示、protected baseline、production-ha和九模块global gate仍为`HOLD/NOT_RUN`。失败的`-001/-002/-003`证据原样保留，只有从零重跑成功的`-004`是当前latest。

<a id="module-web"></a>

## 12. Web 模块验收

### 12.1 资格主体

真实production Vue/Vite artifact/OCI和资格化Chromium/Firefox/WebKit。可使用OpenAPI mock/real Go和Fake OIDC/session/SSE；Web不得用dev server、静态截图或component stub替代。

### 12.2 必验能力与场景

- generated client、URL/deep-link/cursor、scope/session/query cache/SSE；
- loading/empty/partial/stale/HOLD/unauthorized/unavailable/error/retrying/ready；
- Event/Evidence、Response proposal/R2、Firewall R3/dual-bank、original operation；
- Rule Effectiveness三层、formula/coverage/no traffic/reset/gap/not measurable；
- Managed Targets和Fleet target×stage/wave/partial/reconciling；
- Model Pool desired/selected/observed CPU/CUDA、single/HA、route/readback/CAS/gap/no fallback；
- ADR-0019 training recipe/data provenance/quality摘要及“模型结果事实/离线解释证据/Agent辅助解读”分层、non-causal copy与等价table；
- Plugin lifecycle、statistics八种fixed renderer/current/history/quality/truncation、AnalysisArtifact；
- duplicate/out-of-order/SSE gap/429/5xx/timeout/offline polling和零重复mutation；
- WCAG 2.2 AA、keyboard/screen reader/zoom/reflow/chart equivalent table；
- CSP/CSRF/XSS/CSV/deep-link/no token/no Service Worker；
- bundle/CWV/heap/DOM/chart/request/SSE/soak和current/previous rollback。

### 12.3 不变量与Oracle

- 浏览器只调用同源Go，不直连内部服务或DB；
- Web不计算授权/current/risk，不乐观显示dangerous success；
- 插件不能注入UI code/route/ECharts/Vega/URL/action；
- unknown/HOLD/stale/failed语义和mixed child向量不被绿色聚合隐藏；
- 清空浏览器cache/回滚Web不改变server事实。

### 12.4 阻断条件

只做视觉截图、只在dev server/单浏览器、缺危险流程/a11y/security/performance、或网络trace出现内部plugin/P4/Triton endpoint，均阻断完成。

<a id="module-offline-ml"></a>

## 13. Offline ML 模块验收

### 13.1 资格主体

真实Python release image/toolchain完整执行frozen train/validation/test profile，生成bundle、repository和qualification artifacts。Central/Go可由contract validator/fake模拟，但train/evaluate/export/package逻辑不得fake。

### 13.2 必验能力与场景

- dataset/split/seed/toolchain/digest/license/privacy/ground truth；
- source-selection/dataset-revision/winner-selection状态分列；`dataset-p4-window-binary/v1` official fetch、legacy direct-import拒绝、CIC-DDoS2019 official-test sealed blind、CSE-CIC-IDS2018/CIC-IDS2017/UNSW/TON/CICIoT条件OOC、capture-family四split与mixed/ambiguous/not-covered coverage；
- synthetic `60/15/15/10`、CIC training `70/15/15/0`、official-test sealed的确定性group hash；packet timestamp+directional tuple join、半开窗口、100%拟合coverage、attack-share denominator和source×label/family等权；
- reproducibility、feature extraction与online golden一致；
- exact BMv2/P4与offline reference extractor的10秒六维逐字段golden；
- seeds `17,29,43`下Logistic/XGBoost/benign-only Autoencoder三个mandatory execution与exact bounded grid、Platt/`τ_alert|τ_conf`、absolute quality/calibration/abstain门槛、candidate reject、fail-closed `winner=none`、bounded winner selection和最多一个bundle；
- single/multi-label、anomaly/open-set、taxonomy evolution、unknown/OOD/abstain；
- ONNX export/metadata、scaler/config/output adapter和repository exact closure；
- XGBoost CPU ONNX-ML no-ZipMap/no-custom-op、三个候选标准算子calibrated `[N,2]` probability、Logistic raw-margin contribution/TreeSHAP raw-margin additivity/AE scaled-log residual-mean offline explanation evidence；
- raw/optimized profile、CPU numeric/tolerance和reader/rollback matrix；CUDA只有bundle显式声明时适用，否则以稳定理由`NOT_APPLICABLE`；
- malformed archive/path traversal/symlink/executable/custom op/oversize；
- crash/OOM/disk/partial staging/cleanup、resource/performance/supply-chain。

### 13.3 不变量与Oracle

- Bundle immutable，模型不是plugin，不含可执行hook；
- experiment alias/stage不成为production current；
- Offline evidence不激活模型、不写core DB/P4；
- feature/label/output语义只有一份contract/golden；
- public/legacy flow CSV不能绕过canonical P4-window extractor；blind test不能反向影响训练/Platt/threshold；explanation不进入实时result/effect；
- 新label不自动获得effect eligibility。

### 13.4 阻断条件

脚本能训练、accuracy单项达标、ONNX能加载、任一mandatory candidate/seed/split未运行、blind泄漏/回调阈值、只报pooled score、没有eligible winner、没有可重现run/解释/兼容/安全证据，或依赖mutable external registry，均阻断完成。已完整执行但质量未达门槛的候选必须保留`REJECTED`原始结果；不得省略，也不要求把三候选都伪装为通过。

## 14. Cross-cutting Fault Acceptance

每个适用模块的fault evidence必须定义pre-state、fault point、预期intermediate state、recovery observation、final invariants和deadline。共同故障类包括：

- crash/hang/OOM、timeout/cancellation/response loss；
- duplicate/out-of-order/late/conflict/generation drift；
- network partition/TLS/SAN/credential/profile drift；
- queue/connection/FD/thread/memory/disk/inode/WAL exhaustion；
- restart storm/quarantine、partial migration/restore；
- dependency unavailable和current/previous rollback。

测试设计只要求这些故障及最终不变量可被证明，不规定用哪段代码或逐步操作注入。故障工具、seed和注入点必须版本化。

## 15. Performance 与 Soak Acceptance

每个适用模块冻结absolute capacity/SLO、environment profile、warm-up、measurement window、concurrency、data/model/config digest和repeat count。报告至少包含throughput、p50/p95/p99、error、CPU/RSS、queue/connection/FD/disk及适用GPU/VRAM/network/copy。

Performance必须覆盖模块的min/typical/max、steady/peak/saturation/recovery和3,600 秒 soak；soak检查资源是否持续增长。只有relative overhead、microbenchmark、缩小数据规模或未冻结绝对门槛，结果为HOLD而不是PASS。Production绝对性能/容量/HA门槛不可通过waiver提升为production qualified。

性能waiver是聚合规则的唯一受限例外，不改变原始性能`result`或`qualification`。有效waiver必须由Owner签署并绑定requirement、exact scope、risk、remediation、owner、expiry与`max_qualification_level`；默认最多允许Module aggregate继续。只有不影响contract、correctness、security、recovery及目标集成功能时，Owner才能明确放宽至非生产`PAIRWISE|SYSTEM_E2E`。Production绝对性能/容量/HA、安全、正确性或恢复门禁不可豁免；过期、scope漂移或补救未跟踪会立即使aggregate失效。Evidence摘要必须显式列出`waived`，不得把原结果重写为PASS。

## 16. Security 与 Supply-chain Acceptance

每个模块验证：

- 最小identity/credential/network/filesystem/database权限；
- unknown version、malformed/oversize、path/symlink/SSRF/injection和secret redaction；
- exact lock/image/source/config/license/NOTICE/SBOM/provenance/signature/revocation；
- offline rebuild/verification、upgrade/current-previous/rollback和complete removal；
- `REJECT`组件或用途不能经transitive dependency、sidecar、plugin或deploy asset进入。

Scanner、SBOM或签名任一项通过不替代功能、安全、许可证和资格。

<a id="module-complete"></a>

## 17. Module Complete 聚合

Operational module aggregate 只有在以下全部成立时才能标记完成：

- 本模块全部首期需求无TODO/placeholder/stub/临时成功路径；
- language/static/unit/property/contract/golden通过；
- 发布候选及实际runtime真实启动，公开边界黑盒E2E通过；
- fault/recovery/security/compatibility/supply-chain、适用 performance 与精确 3,600 秒 soak 的 operational checks 均已实际执行并通过；
- resource/input/output/queue均有界，unknown version fail closed；
- OCI/startup/readiness/liveness/drain/shutdown和README可复制命令存在；
- requirement→test→environment→evidence→artifact digest→owner映射完整；
- rehearsal/fake/microbenchmark未被误标为正式PASS。
- append-only findings registry 的 open P0 为0，真实启动/必需测试 blocker为0，并可从原始 evidence/digest 重新派生相同结论。

受保护发布基线、dirty tree、production absolute threshold 或尚未开始的正式 pairwise/system 造成的 qualification-only `HOLD|NOT_RUN` 保持原值但不阻断 operational completion；它们仍阻断对应资格声明。任何实际模块测试未运行/失败、证据缺失、真实启动失败或 open P0 都阻断完成。`COMPLETE` 不能改名为 `MODULE PASS`、pairwise/system PASS 或 production qualified。

九个模块的Module Complete都有效后，才允许进入正式pairwise。集成发现模块缺陷时，对应Module Complete立即失效并完整重验，而不是只修集成脚本。

## 18. 当前状态

截至2026-08-27，九个首期模块均有机器派生的operational-completion历史；Web `20260827T020500Z-formal-002`和Offline ML summary补齐原先两个空缺，Go `20260827T041200Z-formal-003`和Analysis `analysis-formal-20260827-011`完成各自当时source scope的正式门禁。startup rehearsal `20260827T062000Z-rehearsal-004`实际启动九个runtime gate；connected rehearsal `20260827T023500Z-rehearsal-005`又在同一PG/Control拓扑运行真实检测、Host/Wasm statistics、Analysis和production Web三浏览器并验证清理。后者仍只覆盖64包happy path且保留明确fixtures/dirty-tree Central HOLD，不能替代正式pairwise/system。本次联调修改的Go/Edge/Inference/P4 source须按第17节退出current Module Complete并重跑完整module gate；语言级回归和rehearsal不能代替。正式pairwise/system仍为`HOLD/NOT_RUN`；本文本身不授予任何资格PASS。
