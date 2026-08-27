# Web SOC SPA 模块详细设计

- 模块 ID：`MOD-WEB-001`
- 目录：`web/`
- 文档状态：`DRAFT`
- 主要需求：`FUNC-API-001`、`WEB-UX-001`、`WEB-STATE-001`、`WEB-GOV-001`、`WEB-VIS-001`、`WEB-A11Y-001`、`WEB-PERF-001`、`WEB-SEC-001`、`WEB-SUPPLY-001`、`WEB-RULE-001`、`WEB-FW-001`、`WEB-TARGET-FLEET-001`、`WEB-PLUGIN-STAT-001`、`TEST-WEB-001`、`TEST-REAL-E2E-001`
- 主要 ADR：ADR-0007、ADR-0008、ADR-0014、ADR-0015、ADR-0017、ADR-0018、ADR-0019

## 1. 模块目标

Web是独立TypeScript/Vue 3/Vite SOC SPA，以任务导向方式展示Go的canonical facts/projections并采集用户输入。它只通过同源`/api`和`/events`工作，不直连PostgreSQL、P4、Edge、Central/Triton、Plugin Host、Analysis、LLM/MCP/A2A provider。

Web不承担BFF、OIDC code exchange、业务授权、事实、队列或设备副作用。Go/受信网关拥有HttpOnly session、CSRF、scope和mutation；浏览器不保存token/secret，不乐观宣称危险操作成功。

## 2. 技术与交付边界

- Vue 3 Composition API、TypeScript strict、Vite、Vue Router、Pinia；
- Element Plus基础primitive、Apache ECharts图表、资格化Vue Query类库管理server state、资格化virtualization library；
- production static artifact/OCI，无Next.js、SSR、Node BFF、micro-frontend、runtime module federation或Service Worker；
- content-hashed asset immutable，HTML/config短缓存或no-store，保留current/previous asset set；
- exact dependency lock、browser target、generator、bundle/profile/SBOM/NOTICE和artifact digest。

借鉴1Panel/sub2api的技术组合与运维交互思想，但业务页面、状态、权限、tokens、文案和资产clean-room实现；不复制GPL/LGPL应用源码或品牌资产。

## 3. 信息架构

一级任务固定为：

- Overview；
- Detection；
- Evidence；
- Effects & Governance：Response Rules、Firewall Policies、Rule Effectiveness；
- Analysis；
- Plugins：catalog/binding/qualification、Plugin Statistics；
- Operations & Audit：Managed Targets、Fleet Operations、Model Pool Operations、audit。

列表使用server cursor，filter/sort/time/tab/selected object进入URL；详情可deep-link。Drawer用于快速研判，核心对象仍有完整页面。Overview只消费有界聚合snapshot，不把全量Event/rule/target交给浏览器计算。

## 4. Frontend 分层

```text
App shell / Router
├─ generated OpenAPI client + SSE adapter
├─ session/scope boundary
├─ server-state query/cache layer
├─ Pinia local UI state only
├─ domain adapters (typed canonical projections)
├─ task pages and safety-critical workflows
├─ built-in charts/statistics renderer
└─ design tokens / a11y / telemetry
```

OpenAPI generated client是唯一HTTP client。Domain adapter只把server DTO映射为view model，不重新计算授权、current、risk或rule denominator。

Pinia只保存theme/density/navigation和非敏感草稿；canonical facts、authorization、operation和server result不持久化到Pinia/localStorage/IndexedDB。Query key绑定actor/scope/target/filter/cursor/generation/revision；上下文变化先取消请求并清cache。

## 5. Server State 与 SSE

- Mutation不乐观标记成功；返回operation identity后查询同一operation；timeout/5xx/重复点击不创建第二mutation；
- 每tab最多一个multiplex SSE，单event最多64 KiB，事件只传bounded invalidation/小identity；客户端累计待处理达到1 MiB或1,000 events任一上限时停止消费并触发受控snapshot refetch；
- duplicate/out-of-order/cursor gap/generation change触发对应snapshot refetch；
- SSE使用15秒heartbeat和带jitter的1–30秒重连退避；连续10次失败后退化为15秒polling并显示degraded/stale，同时保留人工重试；
- 旧cache不能无标记显示为current；所有页面覆盖loading、empty、partial、stale、HOLD、unauthorized、unavailable、error、retrying、ready。

## 6. 处置与审批交互

### 6.1 Response Rules

Analyst从当前Event/Evidence构造proposal，页面显示facts/inferences/unknown、model/target/generation、exact diff、risk、scope、TTL、capacity、evidence freshness和Artifact hash引用。R2 approval workspace要求不同Operator、step-up、exact digest；stale或缺失事实禁用approve。

提交后时间线明确Proposal→Decision→Intent→Attempt→P4 readback→PG finalize；审批成功不显示为P4已生效。

### 6.2 Firewall Policies

Platform Admin创建immutable baseline revision；页面显示normalized diff、explicit default、conflict/shadow、compiled entries/capacity、target/P4Info/current/desired/previous。不同Operator对exact R3 activation step-up。

Timeline分开inactive write/readback、selector switch/readback、PG CAS、previous grace/cleanup。浏览器不生成raw P4 entity、不把Write ACK或BMv2 healthy显示为current。

## 7. Managed Targets 与 Fleet

Managed Targets显示stable identity、endpoint/device属性、lifecycle、desired/observed profile、assignment/actor/mastership、application generation/P4Info、source/effect/readback freshness、drift和audit。

注册使用candidate→server diff→validate→Admin step-up→canonical result。页面拒绝automatic takeover、raw P4、gNMI Set、SSH/CLI和第三方controller嵌入。

Fleet Operations显示冻结target set、ordered waves/failure policy、target×stage child vector、partial/reconciling/current/previous/readback和逐targetrollback。URL filter或“全选”不是authorization scope；任一child unknown都不能被多数成功/绿色badge隐藏。

## 8. Rule Effectiveness

页面分别展示effect execution、installation、dataplane match和action outcome，对外可简化为安装—命中—结果。视图包括metric cards、Top-N horizontal bar、pps/bps trend、state timeline和等价table。

每个视图显示formula、numerator/denominator、coverage、target/table/generation/window、freshness、gap/reset/no traffic/not measurable。禁止单一“生效率”gauge、null补零、counter hit推断drop成功或轮询时间伪装exact last-hit。

## 9. Model Pool Operations

页面显示：

- desired/selected/observed CPU或CUDA profile与mismatch；
- exact model/repository/runtime/image/config/pool current/previous digest；
- availability single/HA、replica/min-ready/capacity，HA适用的failure-domain/N+1；
- CPU core/thread/NUMA/RAM或GPU/driver/CUDA/cuDNN/VRAM；
- per-shard current/previous route、withdraw/drain/WAL/start/warmup/readback/CAS/commit/resume；
- rolling_mixed/failed/full-pool unavailable/gap、rollback与独立E2E证据。
- ADR-0019 exact source-selection与dataset-revision状态、split/seed/candidate/quality evidence摘要；必须分别显示`recipe_frozen`、`dataset_revision_frozen`和`winner_selected`，不能把设计接受显示成模型已训练。可用时再显示由Go投影的offline global explanation artifact、method/model/feature/scaler/background/sample digest、coverage/stability/truncation和limitations。

管理员只可选择已资格化profile并创建新pool generation或exact rollback。UI不提供`auto|best available|fallback`、runtime load/unload、weighted split、Edge-local或deployment-ready-as-current。

## 10. Plugin 与 Analysis 展示

Plugin Admin绑定exact revision/artifact/config/profile/scope/generation，显示qualification/activation/drain/revoke/rollback；不激活`latest`。

Plugin Statistics固定route，消费Go校验并规范化字段映射后的封闭display kind：`metric-card|status|timeseries|bar|heatmap|table|text|evidence-list`。内建registry生成自有Vue组件/ECharts dataset；unknown major/kind/field reference显示受控unsupported并拒绝raw JSON/HTML fallback。插件不能注入route/nav/component/HTML/SVG/CSS/JS/URL/EChartsOption/Vega/expression/action。

页面显示plugin/revision/binding、definition identity、scope/window/input/artifact digest、quality/freshness/coverage/truncation和run state。`Run now`仅在Go返回source-read与`plugin.statistics.run`均有效时启用；schedule控件只把Go返回的coarse capability用于展示，每次create/revise/disable仍携带exact definition/binding、CSRF、适用step-up和幂等身份，由Go重新验证scoped Admin与data-class，不能把客户端缓存权限当授权。控件均由平台固定，插件不能声明按钮、表单或cron表达式。

Analysis页面只展示escaped纯文本/结构化facts/inferences/unknown/citations/limitations/outcome和Artifact digest。模型区域固定分成“模型结果事实”“离线解释证据”“Agent辅助解读”：没有model-native explanation时必须显示“模型结果解读”，不得伪装成SHAP/攻击原因；有解释时显示方法及其适用字段、exact model/feature/scaler/background/sample digest、quality/coverage/stability/truncation、贡献所在空间（raw margin或scaled-log residual）和非因果提示。`background`对TreeSHAP、`scaler`对LR/AE才是必填；其他方法显示`不适用`理由，不显示空白绿色状态。Recommendation不可点击直接执行；人工必须进入独立proposal流程。

Web不根据scores、feature或模型权重自行计算canonical attribution，不让Analysis/plugin提供ECharts option。结构化解释若未来进入Go API，只能由内建horizontal contribution bar、状态/限制和等价table渲染；LR显示raw-margin contribution，XGBoost显示raw-margin TreeSHAP，AE显示scaled-log residual，均禁止使用“导致攻击”的因果文案。当前ADR-0019只要求offline qualification/Artifact引用，不扩展现有实时InferenceResult。

`model-explanation-evidence/v1`及其Go OpenAPI projection尚未创建，因此当前这部分是Web实现前的必需合同，不是已可消费API。合同冻结前只能显示已有evidence reference/digest与`explanation unavailable`，不得由浏览器解析任意ML JSON作为fallback。

## 11. Design System 与可访问性

使用primitive→semantic→component三层tokens统一Element Plus、utility/Sass和ECharts，提供自有light/dark/density、icons、copy和status semantics。

完整页面达到WCAG 2.2 AA：键盘/focus、screen reader、zoom/reflow、reduced motion、非颜色状态、dialog/drawer、chart ARIA与等价table。危险操作使用专用workflow和明确文案，不使用默认approve、单键或模糊确认。

所有plugin/Analysis字符串视为不可信纯文本，禁止`v-html`。CSP默认self-only、无`unsafe-eval`、remote runtime或任意iframe；CSV由Go安全生成并防公式注入。

浏览器只使用同源受限cookie session，不读取token或cookie内容。Mutation携带Go签发的CSRF材料；服务端负责session/origin、`Origin`/`Sec-Fetch-Site`、replay和session-fixation校验。logout、session expiry/revocation以及actor/scope切换必须取消请求、关闭旧SSE并清除server-state cache和非敏感草稿。

## 12. 性能与资源

`web-performance/v1`冻结bundle、route、CWV、heap、DOM、request/cache/SSE/chart/table points和concurrency。初始要求包括shell同步JS 250 KiB gzip、首屏CSS 80 KiB、单route首次JS+CSS 600 KiB、每tab并发HTTP request≤8、SSE≤1，以及目标环境p75 LCP≤2.5s、INP≤200ms、CLS≤0.1；cache entry/bytes、chart instance、observer/timer和DOM node的绝对上限也必须在Module gate前冻结并验证释放。profile schema/digest与真实证据尚未形成前，这些项保持`HOLD|NOT_RUN`，不能据初始数字宣称资格。

Route lazy-load、tree shaking、server cursor、virtualization和bounded ECharts dataset用于大target/rule/fleet/statistics页面。长期导航、scope/session切换、SSE reconnect和chart mount/unmount不得持续增长heap/listener/timer/query。

## 13. 启动与健康

- production artifact由exact静态server/OCI启动，不使用Vite dev server；
- startup验证asset manifest/config/API compatibility和CSP；
- readiness只表示static asset可提供，不表示Go/P4业务成功；
- 前端显示后端domain availability而不伪造本地fallback；
- current/previous Web可独立滚动/回滚，必须兼容Go API/session/CSRF/SSE/deep link和static manifest。

## 14. 模块黑盒 E2E 验收范围

必须真实启动production Web artifact与资格化浏览器；可以使用OpenAPI mock/real Go、Fake OIDC/session/SSE，但Web自身不得由静态截图、组件stub或dev server替代。验收覆盖：

- generated client、session/scope/cache、URL/deep-link/cursor、全部UI状态；
- Event/Evidence/Response/R2/R3、original operation和readback；
- Managed Targets/Fleet matrix、Firewall、Rule Effectiveness、Model Pool；
- Plugin lifecycle/statistics固定renderer、Analysis不可执行Artifact；
- Model recipe/data provenance与“结果事实/离线解释/Agent解读”分层，缺失解释、低coverage/truncation、非因果文案和等价table；
- SSE gap/duplicate/乱序/polling、timeout/5xx/429和零重复mutation；
- cookie/session-fixation/replay、Origin/Fetch Metadata、CSP/CSRF/XSS/CSV/deep-link/token/secret负例；
- WCAG、三浏览器、bundle/CWV/heap/DOM/soak和current/previous rollback。

## 15. Module Complete 判定

所有首期页面、状态、危险流程、a11y/security/performance和production-browser E2E必须完整；只交付dashboard壳、静态图表、mock截图或部分角色流程不能完成。真实Go↔Web属于全模块门禁后的pairwise，不可用模块fake证据冒充。
