# ADR-0018：插件统计结果与声明式 Web 投影

- 状态：Accepted
- 日期：2026-08-12
- 对应需求基线：`vNext-requirements-1.17`
- 影响需求：`ARCH-PLUGIN-001`、`CONTRACT-PLUGIN-STAT-001`、`PLUGIN-STAT-001`、`DB-PLUGIN-STAT-001`、`WEB-PLUGIN-STAT-001`、`PERF-PLUGIN-STAT-001`、`REL-PLUGIN-STAT-001`、`SEC-PLUGIN-STAT-001`、`TEST-PLUGIN-STAT-001`
- 当前资格：设计已接受；实现、真实服务 E2E、性能与生产资格为 `HOLD/NOT RUN`

## 背景

通用插件平台需要支持后续统计类能力：插件基于已授权系统事实计算命中分布、趋势、质量或其他派生结果，并在现有 Vue SOC 中显示。若直接开放 Vue/JavaScript、任意 HTML、ECharts/Vega option 或插件自定义 route，插件就会把代码执行、认证、CSP、浏览器兼容、前端回滚和供应链面一起带入浏览器；若让插件直接写数据库或 Prometheus，则会形成第二事实源、无界高基数或隐式任务队列。

成熟方案提供了可以组合但必须收紧的模式：OpenTelemetry Metrics 的时间、temporality、reset/missing 语义；Grafana DataFrame 与 ECharts `dataset` 的数据/视图分离；Backstage 的宿主定义 extension kind；Dapr/go-plugin/OTel Collector 的进程外 typed lifecycle。它们都不能直接转让 MASI-NIDS 的事实、调度、授权或 UI 代码所有权。

## 决策

### 1. 统计是输出能力，不是新 Plugin Kind

首期 kind 仍封闭为 `analysis-agent`、`read-only-tool`、`pure-transform`，不增加第四种 `statistics` kind。新增版本化 `plugin-statistics/v1` 能力/输出合同：

- `pure-transform` 是默认统计 producer，对 Go 冻结的输入执行确定性计算；
- `read-only-tool` 可在 manifest 与 binding 明确允许的只读外部数据源上产生统计结果，并完整记录 provenance；
- `analysis-agent` 可以引用已校验统计结果并生成叙述，但其 `AnalysisArtifact` 不能成为 canonical 核心统计或绕过统计合同。

核心 Rule Effectiveness、Event/Incident、模型、target/fleet 与系统健康统计继续由 Go/PostgreSQL 原生实现。插件统计是派生、非权威、可禁用的投影，不能替代或改写核心事实。

### 2. 唯一数据与调度链

```text
active binding + qualified PluginStatisticsDefinitionV1
→ authorize host-owned input projection/fields + conditional external-source capability
→ Go canonical facts/projections
→ StatisticsInputBundleV1
→ durable non-effect statistics run
→ Host-managed execution or direct typed plugin adapter
→ PluginStatisticsArtifactV1
→ Go validation
→ Go-owned PostgreSQL plugin_statistics projection
→ OpenAPI list/detail/history + SSE invalidation
→ built-in Vue renderer
```

Go 是 schedule、on-demand request、run identity、authorization、input freeze、validation、projection 与 retention 的唯一 owner。`plugin_statistic_runs` 是唯一 durable run ledger，由 Go Control 内的有界 statistics dispatcher 按 CAS/fence 推进；内存 dispatch queue 只能由该 ledger 派生且可重建，不增加独立 broker、scheduler service、插件自有 queue 或第二 durable queue。插件不能扫描 `plugin_platform`/核心数据库、不能自我调度，也不能把 run 变成 effect intent。统计 run 是独立的低频、无副作用计算状态；它不可被 effect dispatcher claim，外部等待不得发生在数据库事务或持有连接期间。

### 3. Definition、输入、输出和幂等身份

`PluginStatisticsDefinitionV1` 随 immutable plugin revision/capability 签入 manifest 并参加 qualification，绑定稳定 `definition_id/revision/digest`、纯文本名称/说明、producer kind、host-owned input projection/field IDs、data class、允许的 scope/window/trigger、output/display profile、freshness、请求的 deadline/resource 与兼容范围。`read-only-tool` 可以额外引用 manifest/binding 已批准的 external-source capability ID，但 definition 不得包含 endpoint/URL/credential。它不得包含 SQL、PromQL、JSONPath、MCP prompt、任意表达式/代码或运行时数据源发现。Go 只授予 manifest、binding policy 与系统 profile 的交集；插件不能运行时注册 definition。

`StatisticsInputBundleV1` 必须携带 schema、run/request identity、exact plugin revision/config/binding generation、definition ID/revision/digest、scope、半开时间窗 `[start,end)`、`as_of`、source fact/profile/generation/epoch/sequence/coverage/quality references、input digest、预算与 deadline。

`PluginStatisticsArtifactV1` 必须携带 schema、artifact/result identity、exact producer identity、definition ID/revision/digest、scope/window、input/result digest、produced/observed/valid/expires time、独立 run status 与 artifact quality、metrics/series/tables/display hints、provenance、limitations、coverage、truncation 和计数。

`read-only-tool` 的 external read 只可使用 definition 引用且 binding 已批准的 capability；Artifact provenance 还要记录 capability ID、canonical request digest、external observed time、response content digest 与可用的 ETag/version、partial/timeout 状态，不回传 endpoint、credential 或无界 raw response。同一 run 重试若得到不同 external input/response digest，必须冲突并保持旧 current，而不是选择“较新”响应。

幂等键至少绑定：

```text
plugin_id + revision + config_digest + binding_generation
+ definition_id + definition_revision + definition_digest
+ scope_digest + input_digest
+ window_start + window_end + trigger/schedule_revision
```

同键同 result digest 幂等返回原结果；同键不同 digest 稳定冲突。旧 definition 或 binding generation 的 late result 只可审计，不能覆盖 current projection。

### 4. 统计语义

指标类型封闭为 `gauge|sum|histogram`；只有 `sum` 可声明 monotonic，temporality 封闭为 `delta|cumulative`。series identity 由 metric ID、规范化有界 dimensions、unit、temporality 和 producer generation 组成；不得跨 generation/reset epoch/window 盲目拼接。

run status 为 `queued|running|succeeded|failed|cancelled|expired|fenced`；Artifact quality 为 `valid|partial|gap|stale|no_data|not_measurable|invalid`；point quality 为 `valid|missing|gap|reset|late|invalid`。missing、no data、gap、reset 和 not measurable 均不能编码成 0。NaN/Inf、重复时间点冲突、dimension 未规范化、未知 unit/major 或超限必须拒绝。

表格列类型封闭为 `string|int|number|bool|timestamp`，不接受任意 object、HTML、URL 或可执行值。

### 5. 固定声明式显示合同

首期 display kind 封闭为：

```text
metric-card | status | timeseries | bar | heatmap | table | text | evidence-list
```

display hint 只能引用 Artifact 中已声明的稳定字段 ID，并声明受限标题、单位、排序、series 映射和 host semantic token。插件不得提供：

- Vue/React component、route、导航、template、HTML、SVG、CSS、class 或 JavaScript；
- `EChartsOption`、formatter/callback、`renderItem`、custom series、event handler；
- Vega/Vega-Lite spec、表达式或远程数据源；
- iframe、任意 URL、MIME handler、下载脚本、action 或 mutation。

Web 内置 registry 将 display kind 映射到自有 Vue 组件，并在内部生成 ECharts `dataset/encode`。未知 kind/major/field reference 必须拒绝或显示受控 unsupported，禁止 raw JSON/HTML fallback。首期只使用固定路由 `Plugins → <plugin> → Statistics` 和详情 deep link，不允许插件注入一级导航或页面。

宿主可以按 Go 返回的权限和 definition 状态提供固定的 `Run now`、schedule list/create-revise-disable 控件；表单字段、确认、限流、幂等、operation/run query 和审计均由项目代码拥有。插件不得在 manifest、Artifact 或 display hint 中声明按钮、action、mutation、定时表达式或自定义表单。关闭或撤销 binding 必须停止新 run，不能靠隐藏按钮代替服务端授权与 fence。

### 6. PostgreSQL 与 API

Go-owned `plugin_statistics` 逻辑域至少包含：

- `plugin_statistic_schedules`；
- `plugin_statistic_runs`；
- `plugin_statistic_artifacts`；
- `plugin_statistic_current`。

这些表不属于插件私有 schema，也不是 `plugin_platform` catalog/activation 表的 payload queue。只有 Go 写入。definition body 属于 `plugin_platform` 中 immutable manifest revision，schedule/run/current 只保存 exact definition identity/digest，不能复制成可漂移的运行配置。schedule create/revise/disable 采用 append-only immutable revision；不得原地改写历史、把 schedule 当永久授权或在恢复数据库时自动执行。Artifact body 受严格上限；大对象未来若需要，仍只保存受限 URI/hash/size/type reference。

API 只通过 OpenAPI generated client 提供授权后的 definition/list/detail/history、on-demand run request、schedule list/create-revise-disable 与原 run query。definition list 来自 active immutable manifest revision，不接受插件运行时注册。on-demand 必须同时具备源数据 read 与 `plugin.statistics.run`；schedule mutation 必须具备 scoped Platform Admin、相关 data-class scope，并携带 CSRF/适用的 step-up、idempotency key、exact definition/binding 和 immutable schedule digest。每次 scheduled execution 都重新验证 binding/revocation、policy revision、data scope 与适用的 external-source capability，任何漂移均 fenced/HOLD。SSE 只发送 invalidation 和小型 identity，不传完整 Artifact。query key 至少包含 API/profile major、scope、plugin/revision/generation、definition ID/revision/digest、projection version 和 window。Go 按源事实的数据分类和当前人类 scope 重新授权；插件不能定义 ACL。

### 7. 初始资源 Profile

首期 `plugin-statistics/v1` 固定：

- 每 plugin revision 的 statistics definitions ≤32、definition descriptor block ≤128 KiB、每 definition input field refs ≤64、external-source capability refs ≤1、definition 全部人类文本 ≤64 KiB；input ≤2 MiB，Artifact ≤1 MiB；
- 每 Artifact 的 metric definitions ≤32，series ≤64，总数值点 ≤10,000，单 view ≤2,000；每个 histogram point 的 buckets ≤64，bucket count 计入总数值点；
- tables ≤8，每表 columns ≤32，全部 tables 总 rows ≤2,000，API 单页 ≤200；evidence refs ≤200；
- display hints ≤16；JSON nesting depth ≤8；任一 definition/Artifact text/string scalar ≤4 KiB，Artifact 全部文本合计 ≤64 KiB；每 series dimension keys ≤8，key/value 分别 ≤64/128 UTF-8 bytes；
- 每 binding in-flight ≤2、待运行 queue ≤32、单 run total deadline ≤10 秒；
- run/artifact 默认保留 30 天；current pointer 可在 source/binding 失效后保留为明确 stale/revoked 的只读历史，不能继续显示 fresh。

放宽必须创建新 profile 并重跑 contract、DB、fault、security、Web 和性能矩阵；不得由插件 manifest 单方面提高。

### 8. 安全、导出与可观测性

所有字符串均按不可信纯文本处理；Web 使用 Vue interpolation/安全 sink，禁止 `v-html`。CSP 继续禁止 `unsafe-eval` 和远程 runtime。JSON 为首选导出；CSV 必须防公式注入，并由 Go 服务端有界生成。

插件业务统计不得自动转成 Prometheus series。Prometheus 只暴露低基数 run/queue/latency/result/quality 聚合；plugin/result/artifact/rule/IP/五元组/dimension value 均不得作为 label。Grafana/OTel 只做低基数运维观测，不成为统计事实源或显示 mutation。

### 9. 可靠性与核心隔离

插件、Host 或 Manager 不可用时，对应统计 run 返回稳定 unavailable/failed，last current 明确 stale；核心检测、Event/Incident、effect/P4、原生 Rule Effectiveness、其他插件与非插件 Web 页面继续。只有保持完全相同 run identity/input/plugin/config digest、无外部副作用且在总 deadline/retry budget 内的调用可以有界重试；timeout 后查询原 run，不创建第二 run identity。

最大统计插件负载、crash/restart/revoke、数据库和 SSE 故障必须证明核心 p99 相对统计插件关闭基线回退不超过 5%，核心进程 RSS 不增加超过 10%，且不突破连接、FD、线程、queue 和 Web budget。

## 取舍

该方案增加两个契约、Go 低频 run/projection 模块、四张逻辑表和 Web 固定 renderer，但不增加部署模块、plugin kind、浏览器代码加载器、P4 writer、effect queue 或实时热路径依赖。它牺牲了“插件想画什么就画什么”的自由，换取安全、兼容、可访问、性能和独立回滚都能被完整资格化。

## 被拒绝的方案

1. 新增 `statistics` plugin kind：统计是输出合同，不是新的权限/副作用类别；新增 kind 会复制 lifecycle 与资格面。
2. 插件直接写 PostgreSQL/Prometheus：形成第二事实源、隐式队列和高基数风险。
3. 浏览器直连插件：复制认证、授权、缓存、兼容和故障语义。
4. 插件提供 Vue/JavaScript/iframe：破坏 CSP、供应链和 Web 独立回滚。
5. 任意 ECharts/Vega/Vega-Lite spec：扩大表达式、URL、transform、XSS 和浏览器性能资格面。
6. 把 Grafana dashboard 当插件 UI：Grafana 仍只作低基数只读运维观测，不能承接细粒度业务授权或核心事实。

## 迁移与回滚

初次实现按 contract/profile → Go run/projection + DB migration → conformance statistics plugin → Web renderer → pairwise/system E2E 顺序进行。`plugin_statistics` 使用 expand/contract；current projection 可重建，Artifact/history append-only 保留。关闭该 capability、撤销插件或回滚 Web 时，核心表和核心页面不变；旧 Artifact 只读并标记 producer generation/revocation/freshness，不能被新 binding 重用。

## 验证

至少覆盖：

- Go/Rust/Python/TypeScript/Wasm 对 definition/input/artifact/display 的同一 golden；
- Gauge/Sum/Histogram、delta/cumulative、reset/gap/no-data/missing/NaN/Inf、duplicate/out-of-order，以及 read-only external request/response digest/ETag provenance 与 changed-response conflict；
- exact idempotency、old generation late result、timeout/cancel/crash/revoke、Manager/Host/DB/SSE 故障；
- oversize、深度/基数/definitions/points/rows/text 上限、unknown projection/field/external-source capability/major/kind/unit，以及 definition 内 endpoint/credential、SQL/PromQL/JSONPath/MCP prompt/URL/表达式负例；
- HTML/SVG/Vue/ECharts/Vega/URL/XSS/CSV formula injection 负例；
- on-demand 的 source-read + `plugin.statistics.run`、schedule create/revise/disable 的 scoped Admin/data-class/CSRF/step-up/idempotency/audit，以及每次 scheduled run 的 binding/revocation/policy/scope/external-capability 重新授权；
- scope/data-class/revocation、无核心 DB/P4/effect credential 和无插件自有/第二 durable queue；
- WCAG/键盘/屏幕阅读器/等价表格、production browser/render/heap/soak；
- 真实 conformance statistics plugin → Go → PostgreSQL → OpenAPI/SSE → production Web → Playwright System E2E；
- 全部统计插件 disabled/unavailable 与核心功能/原生统计等价性。

任何代码、schema 或文档存在而未执行上述证据时保持 `HOLD/NOT RUN`。

## 参考

- [OpenTelemetry Metrics Data Model](https://opentelemetry.io/docs/specs/otel/metrics/data-model/)
- [Prometheus metric naming](https://prometheus.io/docs/practices/naming/)
- [Prometheus instrumentation](https://prometheus.io/docs/practices/instrumentation/)
- [Grafana DataFrame](https://grafana.com/developers/dataplane/dataframes)
- [Grafana time series contract](https://grafana.com/developers/dataplane/timeseries/)
- [Apache ECharts dataset](https://echarts.apache.org/handbook/en/concepts/dataset/)
- [Apache ECharts ARIA](https://echarts.apache.org/handbook/en/best-practices/aria/)
- [Backstage extension blueprints](https://backstage.io/docs/frontend-system/architecture/extension-blueprints/)
- [Dapr pluggable components](https://docs.dapr.io/developing-applications/develop-components/pluggable-components/pluggable-components-overview/)
- [HashiCorp go-plugin](https://github.com/hashicorp/go-plugin)
- [OpenTelemetry Collector components](https://opentelemetry.io/docs/collector/components/)
- [Vega expression interpreter](https://vega.github.io/vega/usage/interpreter/)
- [OWASP XSS Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html)
- [OWASP CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection)
