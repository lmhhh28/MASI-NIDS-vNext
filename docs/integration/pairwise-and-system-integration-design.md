# MASI-NIDS-vNext Pairwise 与系统集成测试设计

- 文档状态：`DRAFT`
- 日期：2026-08-22
- 需求基线：`vNext-requirements-1.19`
- 主要需求：`TEST-GATE-001`、`TEST-REAL-E2E-001`、`TEST-004`、`TEST-005`、`TEST-006`、`TEST-007`、`TEST-008`、`ACCEPT-001`
- 前置文档：[`../design/00-system-decomposition-and-delivery-design.md`](../design/00-system-decomposition-and-delivery-design.md)、[`../testing/module-e2e-acceptance-design.md`](../testing/module-e2e-acceptance-design.md)

## 1. 文档定位

本文定义九个模块全部独立完成之后，正式跨模块集成要验证什么。它不是实现教程或逐命令runbook；不规定内部编码步骤、脚本内容或人工点击顺序。正式执行命令、环境物化和清理由各模块README与固定runner profile提供。

<a id="pairwise-gate"></a>

## 2. 不可绕过的入口门禁

第一个可记PASS的正式pairwise开始前，必须同时满足：

- `MOD-REGISTRY-001`的九个模块各自Module Complete；
- 每个aggregate仍在有效期内，artifact/config/profile/digest与待集成release一致；
- required contract/profile/golden和current/previous compatibility已冻结；
- `e2e-runner-compose/v1`或目标部署对应的exact runner已资格化；
- 测试环境为clean start，不复用module/rehearsal数据库、P4状态、WAL、cache、artifact或证据；
- 正式边界两侧都是真实发布候选服务，pair内不得使用fake。

Module Complete前可以做真实TLS/wire rehearsal，但证据必须是`level=REHEARSAL, qualification=NOT_QUALIFIED`。Rehearsal结果不得复制、重命名或提升为pairwise。

## 3. 每个正式边界的共同验收

十二个边界都至少覆盖：

- 成功和明确业务拒绝；
- timeout、cancellation、response loss、disconnect/reconnect；
- duplicate、out-of-order、late、same-key same/different digest；
- unknown major、unverified minor、generation/incarnation/profile mismatch；
- malformed/oversize、quota/queue/backpressure/resource saturation；
- 双方restart及一侧不可用时的最终收敛；
- mTLS/identity/scope/credential和no plaintext fallback；
- trace/identity/digest从请求到结果的关联；
- current/previous compatibility和exact rollback/read-only failure语义。

一个边界PASS只证明该边界和exact scope，不自动证明下一边界、系统波次或production。

## 4. 十二个正式 Pairwise/Boundary 集成

<a id="p1"></a>

### P1. P4/BMv2 ↔ Rust Edge

**真实参与者**：exact BMv2/P4 artifact、Rust Edge、test traffic/oracle设施。

**要验证的内容**：

- per-target StreamChannel/arbitration/P4Info/application generation；
- response overlay、baseline双bank/selector、priority/default/fragment/capacity；
- inactive write/readback、selector switch/readback及response loss；
- P4 aggregate bank/epoch/snapshot/read-clear、Digest/PacketIn sample/drop；
- event-time source identity、direct/eligible counter Read和reset/generation；
- `p4-traffic-replay/v1`的sender/test ingress/DUT/counter/outcome分层；
- 1/2/N target actor隔离、slow target公平性和assignment fence。

**关键不变量**：Edge是唯一P4Runtime writer/read owner；P4 ACK不替代readback；sender/counter不替代packet outcome；一个target故障不串改其他target。

<a id="p2"></a>

### P2. Rust Edge ↔ C++ MASI Gateway

**真实参与者**：Rust Edge、Central的真实C++ Gateway；Gateway后端使用该pair scope明确的真实/资格化执行路径，不能以fake Gateway替代。

**要验证的内容**：

- `inference-central-grpc-batch/v1` mTLS、Protobuf framing、tensor bounds；
- source/window/input/request/attempt/pool/route/binding identity；
- Edge no-delay coalescing、bounded in-flight、deadline/quota/backpressure；
- input/result WAL、same-generation retry/dedupe、conflicting result；
- endpoint refresh、network reset、late-result fence；
- full-pool unavailable时pending/gap/resume和无local/profile fallback。

**关键不变量**：同一shard只有一个canonical generation route；Gateway success不推进source cursor；retry保持原request/input digest。

<a id="p3"></a>

### P3. C++ Gateway ↔ pinned Triton ↔ selected ORT Profile

**真实参与者**：Gateway、pinned Triton、exact repository、所选ORT CPU或CUDA runtime和真实硬件环境。

**要验证的内容**：

- explicit CPU/CUDA selection与selected=observed；
- NONE repository closure、explicit instance group、strict readiness、no auto-complete/model-control；
- Gateway validation/admission到Triton dynamic batch/queue/instance；
- model/feature/label/output/wire numeric golden；
- CPU thread/NUMA/affinity/arena或CUDA provider partition/I/O Binding/copy；
- deadline/cancellation、worker identity、crash/OOM/hang和readback；
- CPU/CUDA各自absolute performance和证据分离。

**关键不变量**：Triton是唯一delayed batcher；loaded不等于current；同generation不混runtime profile；无自动backend fallback。

<a id="p4"></a>

### P4. Go Model Manager ↔ Deployment Adapter/Central Rollout

**真实参与者**：Go Control、真实deployment adapter、Central new/current pool generation、真实PostgreSQL；Edge以本边界所需的真实rollout协作面参与，不可用fake证明最终route。

**要验证的内容**：

- immutable pool envelope与idempotent start/stop action；
- selected availability/deployment tier的required/min-ready/capacity，HA适用的failure-domain/N+1；
- `GetLoadedModel/GetPoolStatus` exact readback；
- ordered per-shard route-withdraw/drain/WAL→PG CAS→commit handshake/resume；
- active/warming/draining/replay容量；
- restart/quarantine、response loss、partial mixed rollout、exact previous rollback；
- PITR model-control incarnation fence。

**关键不变量**：deployment/Ready不拥有current；同shard不得双route；外部等待期间无DB transaction；rollback是新durable operation。

<a id="p5"></a>

### P5. Rust Edge ↔ Go Control

**真实参与者**：Rust Edge、Go Control、真实PostgreSQL。

**要验证的内容**：

- InferenceResult→Event identity/digest/idempotency；
- PostgreSQL commit-before-canonical-ACK和ACK response loss；
- result WAL reconnect/replay、same-key conflict和cursor/compaction；
- RuleObservationBatch duplicate/gap/epoch/reset/quality；
- assignment lease/revoke、effect delivery/result和health/readback；
- model binding commit/resume fence；
- 1/2/N target、slow target、Go/DB outage与bounded backpressure。

**关键不变量**：未commit不ACK；duplicate不产生第二Event；Go不直接P4，Edge不写核心DB；result/observation namespace不混装。

<a id="p6"></a>

### P6. Go Control ↔ PostgreSQL

**真实参与者**：Go Control、exact PostgreSQL/migration/pool/HA profile。

**要验证的内容**：

- core Event/Incident/governance/effect transaction与idempotency；
- firewall revision/binding/operation/overlay expiry；
- target/fleet identity/assignment/parent-child-wave/CAS；
- model incarnation/revision/qualification/pool/shard binding/readback/operation；
- plugin catalog/binding/revocation和statistics schedule/run/artifact/current/history；
- rule epoch/current/rollup/retention；
- migration/current-previous reader、pool/deadlock/failover/PITR/incarnation rotation。

**关键不变量**：Go是唯一业务writer；fleet parent不可claim；statistics run不进入effect queue；restore不自动执行外部动作。

<a id="p7"></a>

### P7. Go Plugin Manager/Statistics ↔ Rust Plugin Host

**真实参与者**：Go Control、PostgreSQL、Rust Host。

**要验证的内容**：

- exact manifest/revision/config/binding generation和capability intersection；
- activation/health/drain/revoke/rollback与old-generation result；
- frozen statistics input、run identity、deadline/cancel和typed Artifact；
- Host crash/restart、Go response loss、queue rebuild和reconcile；
- schema/digest/resource/display validation与current CAS；
- zero core DB/P4/effect credential/mutation。

**关键不变量**：Manager拥有canonical lifecycle/run；Host只执行；Host内存queue不是durable ledger；revoked/late结果不覆盖current。

<a id="p8"></a>

### P8. Runtime Host ↔ Service/Wasm Conformance Plugin

**真实参与者**：Rust Host、真实`grpc-service/v1`fixture、真实`wasm-component/v1`/Wasmtime fixture，含deterministic statistics capability。

**要验证的内容**：

- WIT/gRPC contract、kind/runtime/profile/version；
- filesystem/network/secret/capability sandbox；
- fuel/CPU/memory/PID/FD/disk/output/deadline/queue；
- deterministic transform/statistics Artifact和same-key conflict；
- trap/crash/OOM/hang/drain/revoke/restart storm；
- current/previous Host/runtime compatibility。

**关键不变量**：fixture不能调用core/P4/effect；unknownkind/runtime fail closed；一个plugin故障不影响其他plugin或core。

<a id="p9"></a>

### P9. Go Control ↔ Frontend

**真实参与者**：Go Control、PostgreSQL、production Web artifact、资格化浏览器和同源HTTPS/OIDC边界。

**要验证的内容**：

- OpenAPI generated client、session/CSRF/scope、cursor/SSE；
- Event/Evidence、proposal/decision/effect/original operation；
- firewall/target/fleet/model/rule/plugin/statistics所有投影和状态；
- fixed plugin renderer与AnalysisArtifact不可执行展示；
- duplicate/timeout/5xx/SSE gap/cache purge/rollback；
- a11y/CSP/XSS/CSV/no-token/no-internal-endpoint；
- current/previous Go/Web compatibility和browser performance。

**关键不变量**：浏览器不授权、不直连内部服务、不乐观显示成功；SSE只invalidate；plugin不能注入代码。

<a id="p10"></a>

### P10. Python Analysis Plugin ↔ Read-only MCP

**真实参与者**：official Analysis Plugin、Go提供的restricted MCP endpoint；外部数据/provider可用deterministic fixture。

**要验证的内容**：

- `masi-mcp-readonly/v1` version negotiation、mTLS/session scope；
- tool/resource allowlist、bounded request/response、provenance；
- unknown/mutation/URL/prompt registration/oversize拒绝；
- timeout/partial/malformed/prompt injection和stable degraded outcome；
- binding/revocation/data-class变化后的重新授权。

**关键不变量**：Analysis调用Go MCP，链路不经Host；MCP只读且不能创建proposal/effect；tool data不能扩大instruction/capability。

<a id="p11"></a>

### P11. Go/Peer Agent ↔ Python Analysis Plugin A2A

**真实参与者**：Go或资格化peer、official Analysis Plugin、真实A2A boundary。

**要验证的内容**：

- A2A 1.0 request/task polling、no push/no streaming、peer allowlist；
- task/message/artifact identity、idempotency、deadline和generation；
- sufficient/limited/insufficient/failed outcomes、grounding和Artifact digest；
- timeout/disconnect/restart/revoke/late Artifact；
- `AGENT-COMPAT-001`可观察行为矩阵。

**关键不变量**：A2A不替代MCP或Go workflow；Artifact不可执行；Analysis不写core/P4；业务流量不经Host。

<a id="p12"></a>

### P12. Go Effect Dispatcher ↔ Rust Edge ↔ P4 Readback

**真实参与者**：Go Control、PostgreSQL、Rust Edge、exact BMv2/P4 target和packet/readback oracle。

**要验证的内容**：

- R0/R1/R2 response overlay intent、claim/fence/journal/write/readback/PG CAS；
- typed R3 baseline inactive bank write/readback→selector flip/readback→PG CAS；
- timeout-after-write、response loss、Go/Edge/P4/DB crash和original-operation reconcile；
- assignment/P4Info/generation/capacity/authorization drift零Write；
- applied exact entry后Go创建observation epoch，Edge异步counter sweep；
- fleet parent+child/waves按per-target执行和partial/unknown vector。

**关键不变量**：只有intent可claim，Edge是唯一writer；unknown不盲重试；counter sweep不阻塞effect；fleet无跨target原子或第二queue。

## 5. Pairwise 证据与放行

每个边界产生独立`level=PAIRWISE` evidence，并绑定两侧release/image/config/contract、真实service inventory、environment/topology/profile和raw observations。边界只有required scenarios全部PASS才放行；一个边界PASS不允许省略后续边界。

边界缺陷若归因某模块，该模块退出Module Complete，相关pairwise及所有依赖它的系统波次失效。若归因contract/profile，所有受影响模块重新运行contract与Module gate。

<a id="system-waves"></a>

## 6. 十个系统拼装波次

十个波次是逐步扩大真实系统范围的验收层，不合并成一条“大E2E”。前一波的契约、恢复和绝对性能未通过时，不引入下一波真实mutation。

| 波次 | 真实链路 | 需要建立的系统能力 | 不得掩盖的不变量 |
|---|---|---|---|
| <a id="w1"></a>W1 数据面 | BMv2 aggregate/snapshot → Edge source WAL/window | qualified source、snapshot、final window | Digest/普通Read不冒充coverage；无P4外第二source |
| <a id="w2"></a>W2 检测面 | BMv2 → Edge input WAL → Gateway→Triton→ORT → Edge result WAL | central real-time detection compute | CPU/CUDA显式、single route、无fallback、唯一batcher |
| <a id="w3"></a>W3 模型控制 | frozen ML dataset/recipe evidence→single exact bundle→Go rollout→Central new pool→Edge drain/CAS/resume | exact qualified candidate、current/previous和rolling model replacement | dataset/recipe/winner状态分列；loaded≠current、mixed显式、PITR fence |
| <a id="w4"></a>W4 事实链 | Edge result WAL → Go → PostgreSQL Event → ACK/cursor | exactly-once canonical Event | commit-before-ACK、duplicate/conflict不丢/不双写 |
| <a id="w5"></a>W5 可视化 | PostgreSQL → Go `/api|events` → production Web | canonical SOC projection | 浏览器无事实/授权/internal direct link |
| <a id="w6"></a>W6 Target/Fleet | Go registry/assignment → Edge TargetActors/P4 → Web | 1/2/N managed targets和fleet vector | single writer、parent不可claim、unknown不聚合成功 |
| <a id="w7"></a>W7 反向处置 | Go intent → Edge journal/readback → P4 → PG CAS | response overlay和baseline firewall | maker-checker、zero-write reject、无第二effect path |
| <a id="w8"></a>W8 规则表现 | exact applied→Go epoch→Edge counter/oracle→PG/Web | installation/match/outcome分层 | no traffic/reset/gap不补0，counter不证明outcome |
| <a id="w9"></a>W9 插件扩展 | Manager→Host/service/Wasm或direct service；statistics→Web | controlled plugin lifecycle和派生统计 | 无新kind/module/DB writer/UI code/effect queue |
| <a id="w10"></a>W10 Agent旁路 | Incident→Analysis A2A→LangGraph/LLM/MCP→Artifact→Web | grounded、不可执行分析 | 不进入实时检测/effect，Analysis业务不经Host |

每波同时复核前面已通过的核心不变量和性能预算，避免新增子系统使早期链路p99、资源或故障隔离失效。

<a id="full-system-e2e"></a>

## 7. Full System E2E 场景套件

`TEST-006`是required scenario集合的aggregate，不是一条测试覆盖所有子结论。每个`fixture × runtime × availability × deployment tier × topology × fault`产生独立evidence identity。

### 7.1 检测链

**真实链**：generated/synthetic/curated/live-session traffic → BMv2 → qualified telemetry snapshot → Edge final window/input WAL → Central selected CPU/CUDA → Edge result WAL → Go/PostgreSQL Event/ACK → API/Web。

**Required scenarios**：

- deterministic normal/attack generated和synthetic fixture；
- 一个license/privacy/ground-truth已通过的curated PCAP slice；
- 一个真实socket TCP client/server session；
- Go ACK loss且零双Event；
- same-generation replica response loss且原identity dedupe；
- full-pool outage产生bounded HOLD/gap且无fallback；
- source gap不补零分类；
- release声明CPU/CUDA时两套独立全链。

### 7.2 模型替换链

**Required scenarios**：ADR-0019 canonical dataset与四split/三seed/三mandatory execution证据、`winner=none` fail-closed、同合同新权重、新label/unknown reader、多输出模式、profile/resource资格失败、deployment/readback/CAS/commit response loss、partial mixed、exact previous rollback、major incompatibility拒绝、显式声明后才适用的CPU↔CUDA新generation、single/HA outage、PITR incarnation和active/warming/draining/replay容量。

**不变量**：公开/legacy flow CSV不绕过P4-window extractor，blind test不参与训练/Platt/threshold；同shard始终单路；新label不自动effect；runtime load/poll/shadow/weighted split/Ready-as-current拒绝；历史Event不改写。ADR-0019 exact bundle只声明CPU时，CUDA明确`NOT_APPLICABLE`而不是缺失PASS。

### 7.3 治理与处置链

**Required scenarios**：

- R0/R1 qualified deterministic policy直接生成intent并完成readback/CAS；
- Analyst proposal→不同Operator R2→intent→P4；
- baseline Admin maker→不同Operator R3→dual-bank activation；
- evidence/generation/P4Info/capacity/expiry/self-approval/scope失败且零Edge RPC；
- Agent/Plugin/Web建议可引用但不能直接Decision/Intent。

### 7.4 Firewall 与规则表现链

**Required scenarios**：空/最小/4,096 rule、priority/default/fragment/permit/drop、conflict/shadow/unsupported/over-capacity、partial bank、selector response loss、exact rollback、overlay TTL、host-filter negative；另分别验证exact installation、eligible no-hit、no traffic、counter hit但wrong outcome、完整outcome、reset/gap/generation/TTL和no-hit候选零mutation。

### 7.5 Target/Fleet 链

**Required scenarios**：manual/external candidate registration、duplicate拒绝、1/2/N target、static canary/waves和三种failure policy、slow/unreachable/restart/P4Info drift、parent partial/unknown/reconciling、assignment handoff/higher election、per-target previous rollback、PITR incarnation、gNMI Set/second controller/external auto-current拒绝。

### 7.6 Plugin 平台链

**Required scenarios**：signed Host-managed service/Wasm、独立service/Agent direct adapter、unknown kind/major、wrong publisher/digest、capability/resource超限、concurrent activation、old-generation result、Host crash/revoke/incompatible rollback，且全程零core/P4/effect mutation。

### 7.7 Plugin Statistics 链

**真实链**：Go authorized projection/freeze → durable run → real conformance plugin via Host/direct adapter → Artifact validation → PostgreSQL current/history → OpenAPI/SSE → production Web fixed renderer。

**Required scenarios**：valid/partial/gap/no-data/reset/stale/not-measurable、same-key conflict、old/revoked generation、timeout/cancel/crash、max resource、DB/SSE/browser fault、全部UI injection负例，以及插件全部disabled后core/native statistics页面等价。

### 7.8 Agent 旁路链

**真实链**：Incident bundle + Go-authorized Event/model result/offline explanation references → official Analysis → real A2A/MCP boundaries → deterministic provider fixture或qualified provider → grounded non-executable AnalysisArtifact → Go/Web fixed display。

**Required scenarios**：evidence sufficient、bounded MCP补证、只有model result时明确“结果解读”、有offline explanation时保留method/digest/coverage、missing/stale/truncated/low-quality explanation、伪造SHAP/因果表述拒绝、insufficient/provider/tool timeout/grounding reject、compatibility matrix、binding revoke。System同时真实启动Host并独立验证Host场景，但Analysis业务流量不经Host。

## 8. System Fault 与 Performance 复核

Full E2E在功能场景上叠加cross-module故障：P4/Edge/Central/Go/DB/Host/Analysis/Web crash、network partition、TLS rotation/error、queue/disk/connection exhaustion、PITR、response loss、duplicate/conflict、slow target和full-pool outage。

System performance以observation point/traffic ingress→PostgreSQL Event ACK→Web projection和intent→P4 readback→PG finalize的端到端SLO为主，同时分段记录P4/Edge/network/Gateway/Triton/ORT/Go/DB/Web。Module microbenchmark不能替代system result。

最大firewall、telemetry、rule sweep、N-target、plugin statistics和Web dashboard并发叠加必须满足冻结预算；各自单独PASS不能推定组合PASS。Soak检查connections/FD/threads/RSS/queue/WAL/DB/browser heap是否持续增长。

## 9. 正式服务清单与 Fake 边界

System E2E至少真实启动：

- exact BMv2/P4Runtime；
- Rust Edge；
- selected Central Gateway+Triton+ORT CPU或CUDA；
- Go Control；
- 真实PostgreSQL及migration；
- Plugin Runtime Host；
- deterministic statistics conformance plugin；
- official Python Analysis Plugin；
- production Web和资格化浏览器。

任何内部MASI服务都不得fake。允许的fixture仅包括测试traffic、外部LLM/provider deterministic fixture和fault injection设施；这些fixture不能取得对应真实external provider或production traffic资格。

## 10. 集成 Evidence 与 Aggregate

每次正式运行保存service inventory、process/container identity、topology/network/certificate、migration/P4 pipeline/model/traffic/provider fixture、start/ready/drain/stop timeline、fault、raw result、log/trace refs、resource/performance和cleanup hash。

Pairwise/System aggregate只包含release scope列明的required、`APPLICABLE`场景；全部为`PASS+QUALIFIED`才可通过。任一`FAIL|HOLD|NOT_RUN`、过期waiver、profile/digest drift、缺服务、绕过公开边界、复用rehearsal状态或缺原始evidence都会阻断。

## 11. 缺陷回流规则

- 模块内部缺陷：模块退出Module Complete，重跑其完整Module gate及所有受影响pairwise/waves/full E2E；
- contract/profile缺陷：更新版本/兼容策略，所有producer/consumer重跑contract和相应模块门禁；
- testkit/runner缺陷：受影响evidence失效，修复runner后从clean environment重跑，不能只修改摘要；
- environment/fixture污染：对应scope结果为HOLD/FAIL，精确cleanup/quarantine后新run identity重跑；
- performance回退：保留原始FAIL；只允许需求规定的有限非生产waiver，绝不提升production绝对门槛。

## 12. 当前状态

截至2026-08-27，九个首期模块均已有机器派生的operational-completion历史。Web run `web/evidence/module-gates/runs/20260827T020500Z-formal-002/gate-summary.json`执行了真实production OCI、三引擎27项黑盒、性能矩阵和60秒排除warmup后的3600.079秒soak，结果为`overall_module_complete=true`、`result=HOLD`、`qualification=NOT_QUALIFIED`；Offline ML亦有完整独立模块门禁历史。Go `control-go/evidence/module-gates/runs/20260827T041200Z-formal-003/gate-summary.json`和Analysis `analysis-py/evidence/module-gates/runs/analysis-formal-20260827-011/gate-summary.json`完成了当时source scope的真实OCI/公开边界及3600秒soak。`evidence/system-startup-rehearsal/20260827T062000Z-rehearsal-004/summary.json`实际启动九个runtime gate；P9/P11另分别验证真实Go/PostgreSQL/Web与Go/PostgreSQL/Analysis边界。加固后的connected run `evidence/system-connected-full/20260827T023500Z-rehearsal-005/summary.json`进一步在同一PostgreSQL/Control拓扑中连通真实BMv2→Edge→Gateway→Triton/ORT→Go Event commit/ACK、运行中Control maintenance dispatcher→Plugin Host/Wasm→statistics current/history、Go→Analysis A2A以及production Web；Chromium/Firefox/WebKit均读回同一Event/Incident、统计Artifact和不可执行/不可部署Analysis Artifact，且runner中断清理负例无自有资源残留。该run通过闭合schema与跨字段identity/timeline/count/browser/secret-path validator，但只覆盖64包happy path；pipeline loader/packet sender及external provider/MCP仍是明确fixture，Central子证据因dirty tree保持`HOLD`。本次联调同时修改了Go/Edge/Inference/P4 contract与runner，故较早module summaries只保留为历史，不能覆盖当前dirty source；当前语言/contract回归虽通过，完整module gates尚未重跑。因此connected run只能是`REHEARSAL/PASS/NOT_QUALIFIED`，不得命名为正式Full System E2E；十二个clean-environment pairwise、十个完整system waves、必需fault/traffic/PITR/performance/soak、受保护基线和production HA仍为`HOLD|NOT_RUN`。
