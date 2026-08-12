# Go Control Core 模块详细设计

- 模块 ID：`MOD-CTRL-001`
- 目录：`control-go/`
- 文档状态：`DRAFT`
- 主要需求：`ARCH-002`、`ARCH-004`、`MOD-TARGET-FLEET-001`、`FUNC-API-001`、`FUNC-GOV-001`、`FUNC-EFFECT-001`、`FUNC-FW-001`、`FUNC-RULE-001`、`FUNC-INF-MODEL-001`、`FUNC-TARGET-FLEET-001`、`PLUGIN-STAT-001`、`DB-GOV-001`、`DB-PLUGIN-001`、`DB-PLUGIN-STAT-001`、`DB-MODEL-001`、`DB-RULE-001`、`DB-FW-001`、`DB-TARGET-FLEET-001`、`SEC-002`、`TEST-003`、`TEST-GATE-001`
- 主要 ADR：ADR-0001、ADR-0003、ADR-0004、ADR-0006、ADR-0007、ADR-0008、ADR-0014、ADR-0015、ADR-0017、ADR-0018

## 1. 模块目标

Go Control Core 是系统业务控制面和核心 PostgreSQL schema 的唯一业务写入者。它接收 Edge 的检测结果和设备观测，维护 Event/Incident、治理、effect、firewall、target/fleet、model、plugin、statistics、rule observation等事实，并通过同源API/SSE向Web提供授权投影。

Go采用模块化单体：内部子域具有清晰package、repository、service和dispatcher边界，但共同运行在一个发布候选binary/OCI中，共用一致的身份、事务、审计和资源治理。不得把内部manager/dispatcher拆成第二事实服务、workflow engine或消息队列。

## 2. 进程内子域

```text
Go Control Core
├─ Identity & Session (OIDC callback, session, CSRF, scope mapping)
├─ API & Projection (OpenAPI, SSE invalidation, cursor)
├─ Event / Incident / Evidence
├─ Governance (eligibility, risk, proposal, decision)
├─ Effect Orchestrator + bounded dispatcher
├─ Firewall Policy Manager
├─ Target Registry + Fleet Coordinator
├─ Rule Observation Projector
├─ Model Manager + deployment adapter coordinator
├─ Plugin Manager
├─ Plugin Statistics + bounded dispatcher
├─ Audit / Retention / Reconcile
└─ PostgreSQL repositories / outbox claim adapters
```

子域只能通过本模块内部显式接口和同一 canonical domain types协作；跨模块types从`contracts/`生成。Effect dispatcher只claim `effect_intents`，Statistics dispatcher只从`plugin_statistic_runs`有界派生，两者不共享任务语义。

## 3. 数据库与事务规则

- Go是core、target_fleet、model_platform、plugin_platform、plugin_statistics、firewall/rule projection的唯一业务writer；
- transaction保持短小，只做读取前置、append事实、CAS、claim/finalize和outbox状态；
- 不在transaction或持有连接期间等待P4、Edge、Central、deployment、Plugin Host、Analysis、LLM/MCP/A2A或HTTP；
- external call前生成不可变request/precondition token和durable operation；返回后重新开启短transaction并按exact token CAS；
- same identity+same digest幂等返回原事实，same identity+different digest冲突；
- PostgreSQL trigger不执行设备、网络、插件或模型动作。

## 4. Event Ingest 与 Canonical ACK

Edge按有界batch提交InferenceResult。Go验证source/window/input/result、model-control incarnation、shard/route/pool/binding generation、feature/label/adapter/profile和payload digest后，在同一短transaction中创建或查回canonical Event与幂等记录。

只有transaction durable commit后才返回canonical ACK/cursor。Duplicate replay返回原Event；same key different digest稳定冲突；wrong/late generation不写canonical Event。Abstain/OOD/low-quality按contract明确投影，不默认为normal。

Event partition、Incident聚合和policy evaluation使用强类型热字段；插件/Agent Artifact只能作为引用/evidence，不直接写Event或effect。

## 5. Governance 与 Effect

### 5.1 事实模型

- Effect Proposal：不可执行、immutable、canonical digest；
- Authorization Decision：对exact proposal digest的append-only approve/reject事实；
- Effect Intent：唯一可claim的设备副作用事实；
- Effect Attempt/Result：claim/fence、Edge call、readback与finalize记录。

纯只读R0不创建Proposal、Decision或Intent。对会改变设备状态的R0/R1，只有Owner启用且已资格化的确定性policy可作为授权来源直接产生intent；人类Operator发起的R0/R1必须先持久化canonical Proposal和绑定exact digest的Decision，R1才允许同一Operator兼任proposer/approver。R2必须先有proposal，由与proposer具有不同稳定`(iss,sub)`的scoped Operator在最近5分钟内完成可验证的phishing-resistant step-up后审批；普通Incident API拒绝R3。Baseline firewall使用typed R3：Platform Admin maker创建immutable revision，不同稳定身份的scoped Operator checker在phishing-resistant step-up后对exact diff/default/target-set/wave/plan digest授权。

Claim前重新验证actor、scope、risk、evidence freshness、target assignment、generation/P4Info、capacity、TTL、policy/authorization expiry和digest。缺失/漂移在零Edge RPC前HOLD/reject。

### 5.2 Dispatcher

Dispatcher从PostgreSQL `effect_intents`以lease/CAS/fence有界claim，transaction提交后调用Edge，再以原operation/precondition finalize。Timeout后查询原operation/readback，不创建第二intent或盲重试。Fleet parent永不可claim；future-wave child仍是同一表中的intent，gate未开前不可claim。

## 6. Firewall Policy Manager

Manager拥有normalized baseline revision、default、current/previous binding、activation operation、response overlay/expiry和readback reference。它不生成raw P4 entity；调用Edge compiler/preflight取得per-target compiled plan与capacity evidence。

Baseline激活事实跟踪inactive write/readback、selector switch/readback、PG current CAS、previous grace/cleanup。Selector已切但CAS未知时状态为reconciling，冻结同target baseline mutation并沿原operationreadback；rollback是激活exact previous的新operation。

Response overlay沿普通R0/R1/R2治理和durable TTL delete intent，不依赖Go内存timer或P4 idle notification作为事实。

## 7. Target Registry 与 Fleet Coordinator

Registry创建永不复用的stable `target_id`，维护lifecycle、desired profile、endpoint/credential reference、scope、assignment和capability observation。External NetBox/CMDB仅导入带provenance的candidate；Admin确认diff后才写canonical事实。

Assignment绑定target-control incarnation、generation、Edge workload、不可复用lease和election floor/range。Handoff先durable revoke/drain旧assignment，再CAS新generation和严格更高range；Go不伪装成P4 election或primary。

Fleet Coordinator冻结target set、ordered waves、parallel limit和failure policy。Decision、non-claimable parent和bounded per-target intents在一个短transaction中形成。Parent只从完整child vector投影planned/running/partial/reconciling/applied/failed/aborted；任一unknown保持reconciling。不存在跨target原子提交或多数成功=applied。

## 8. Rule Observation

Effect exact readback applied后，Go创建immutable rule observation identity/epoch并下发Edge采样。Go验证sample identity/sequence/generation/reset/quality，计算同epoch cumulative delta、rate/ratio/coverage，写latest/status/5-minute/1-hour rollup。

安装、命中、outcome和effect execution分开；denominator不可用不计算百分比，zero traffic/no hit/reset/gap/stale/not measurable不补零。Per-rule事实只在PostgreSQL，不作为Prometheus label。No-hit/shadow分析只产生只读候选，不能自动mutation。

## 9. Model Manager

Model Manager是model revision/qualification、model-control incarnation、logical pool/pool generation、per-shard desired/current/previous、rollout/recovery/rollback operation和worker/pool observation的唯一writer。

Rollout固定为：durable operation → exact pool envelope/deployment action → real Central startup/warmup/readback/capacity → Edge per-shard route-withdraw/drain/WAL → short PG CAS current/previous → committed-binding handshake/resume → old generation drain。所有外部等待均在transaction外。

CPU/CUDA profile由管理员显式选择，probe只验证。Loaded/Ready/deployment status不等于current；same-generation replica不改变route；CPU↔CUDA/model/backend变化使用新generation。Rollback是选择仍qualified exact previous的新operation，不自动因inference错误切旧模型。

PITR/restore/clone/rewind在开放writer/ingest前轮换从未使用的model-control incarnation，并以新operation/generation逐shard重验。

## 10. Plugin Manager 与 Plugin Statistics

### 10.1 Plugin Manager

Manager拥有catalog、manifest revision、verification/qualification、activation/binding generation、drain/revoke/rollback和audit。它不执行第三方代码；Host实例化Wasm或受控连接已部署的Host-managed service，独立Analysis/service/Agent通过direct typed adapter运行。

Kind封闭为`analysis-agent|read-only-tool|pure-transform`。Unknown kind/major、错误digest/publisher、capability扩张、revoked artifact和unqualified runtime拒绝。插件永远不能写core schema、创建Decision/Intent或调用Edge/P4。

### 10.2 Plugin Statistics

Statistics是既有kind的output capability，不是模块或kind。Go拥有：

- immutable definition与host projection/field allowlist验证；
- on-demand同时验证source-read与`plugin.statistics.run`，schedule mutation验证scoped Platform Admin、data-class、CSRF/适用step-up、append-only schedule revision与幂等；
- canonical input freeze、run/idempotency/fence和唯一durable ledger；
- Host/direct adapter调用、Artifact schema/resource/display验证；
- PostgreSQL current/history CAS、read/export授权和SSE invalidation。

插件不得自调度、扫描DB、直写Prometheus、提交UI代码/route/action或成为effect source。每次scheduled run重新验证binding/revocation/policy revision/scope/data-class和适用external-source capability；任一漂移均在调用插件前fence/HOLD。

## 11. API、OIDC 与 Web Projection

生产同源提供static asset gateway或受信代理后的`/api`、`/events`和OIDC callback。Go/受信认证组件拥有code exchange、session、scope mapping和业务授权；会话cookie固定为Secure、HttpOnly、SameSite及受控path/domain/expiry，并实施replay与session-fixation防护。Mutation的CSRF token绑定session/origin并验证适用的`Origin`/`Sec-Fetch-Site`信号；logout、session revocation或actor/scope变化关闭SSE并使旧session/cache不可继续使用。浏览器不持token/secret。

OpenAPI generated contract是唯一Web HTTP接口。List使用server cursor和bounded page；mutation使用idempotency key和operation identity，不返回模糊即时成功。SSE只发送有界invalidation/小投影，单event不超过64 KiB，按15秒heartbeat维持；cursor gap/generation change要求snapshot refetch。Go按session/origin/scope授权stream，不在URL、cursor或payload携带credential。

Go为Web生成canonical/authorized projection，明确current/desired/observed、stale/HOLD/unknown/reconciling等namespace。路由可见性从不替代服务端授权。

## 12. 启动与健康

- startup：验证config/profile/cert、DB schema compatibility、required contracts和identity；应用实例不自动执行migration；
- readiness：可访问所需DB、repositories和公开API，且schema/profile兼容；外部P4/Central/Host/IdP局部故障通过domain status反映，不都转换为process not-ready；
- liveness：scheduler/HTTP/gRPC/DB pools取得进展，不因外部模块短暂故障重启风暴；
- drain：停止新mutation/claim，等待有界in-flight，释放leases，不在关闭时盲目重复外部动作；
- shutdown：审计未完成operation，保留durable facts供新实例reconcile。

## 13. 安全与资源

- OIDC稳定身份使用`(iss,sub)`与版本化scope mapping；maker/checker比较稳定身份；
- dangerous mutation要求CSRF、exact digest/scope/freshness；R2必须验证最近5分钟内的phishing-resistant step-up和不同稳定maker/checker，R3 checker也必须使用phishing-resistant step-up；缺失、过期或类型不符均HOLD；
- API/body/page/backlog/queue/connection/transaction/retry/timeout/log/retention全部有profile上限；
- endpoint、URL、artifact、external capability采用allowlist和SSRF/path/symlink防护；
- 各外部模块使用最小mTLS identity，Go不向Web/插件暴露P4/Central/DB secret；
- audit是append-only业务记录，trace/Prometheus/Grafana不是事实源或授权输入。

## 14. 模块黑盒 E2E 验收范围

必须真实启动Go release binary/OCI和真实PostgreSQL test实例；可以使用Fake Edge/target、Inference/deployment adapter、Plugin Host和OIDC context。验收覆盖：

- result→Event commit-before-ACK、duplicate/conflict/replay；
- 只读R0无治理事实、自动policy直达intent、人类R0/R1 Proposal→Decision、R2/R3 phishing-resistant maker-checker、stale/zero Edge RPC；
- effect claim/timeout/reconcile/finalize；
- firewall revision/overlay/dual-bank operation/current-previous；
- target identity/assignment/incarnation、fleet parent/child/wave/partial；
- rule epoch/formula/quality/rollup/retention；
- model qualification/pool rollout/readback/CAS/recovery/rollback；
- plugin lifecycle/revoke和statistics freeze/run/validate/current/history/auth；
- OIDC/session/CSRF/API/SSE/cursor、fault、DB failover、资源和性能。

测试只通过公开Go API/gRPC和数据库结果观察，不导入内部package或以日志作为主要oracle。

## 15. Module Complete 判定

Go的全部子域必须在一个release中完整实现；不能以“后续补齐某manager/页面”宣称模块完成。真实PostgreSQL、public-boundary E2E、Go format/vet/staticcheck/race、migration integration、fault/security/performance/compatibility和README命令都必须通过。任何stub dispatcher、硬编码授权、外部等待持有transaction、第二queue或缺失绝对门槛都会阻断Module Complete。
