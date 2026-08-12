# 插件统计结果与声明式前端展示成熟方案评估

- 日期：2026-08-12
- 对应需求基线：`vNext-requirements-1.17`
- 性质：独立技术评估；不替代需求基线或 ADR
- 状态：文档级结论已确认；实现、真实 E2E、性能与生产资格均为 `HOLD/NOT RUN`

## 1. 问题与结论

后续插件需要能够读取经授权、冻结且有界的系统事实，计算统计结果，并把结果安全地显示在现有 Vue SOC 前端。独立评估结论是：统计结果由现有 `pure-transform`（默认）和具备明确外部只读能力的 `read-only-tool` 复用一个版本化输出合同产生；`analysis-agent` 只能引用已经校验的统计 Artifact。系统不新增“统计插件”kind，也不开放任意前端插件代码。

推荐链路为：

```text
active binding + qualified PluginStatisticsDefinitionV1
→ authorize host-owned input projection/fields + conditional external-source capability
→ Go canonical facts/projections
→ Go freezes StatisticsInputBundleV1
→ bounded non-effect plugin run
→ PluginStatisticsArtifactV1
→ Go schema/identity/digest/scope/generation/quality validation
→ Go-owned PostgreSQL projection
→ OpenAPI + SSE invalidation
→ built-in Vue renderer + ECharts dataset
```

插件只计算数据和给出受限展示提示；Go 决定调度、授权、校验、持久化和 API 投影；前端只使用内置组件渲染。用户需要主动刷新或周期执行时，由宿主提供固定 `Run now` 和 schedule list/create-revise-disable 交互，插件不声明按钮、表单、定时表达式或 mutation。核心 Rule Effectiveness、Event/Incident、模型和系统健康统计仍由 Go/PostgreSQL 原生提供，插件不可替换这些事实。

## 2. 成熟方案中可直接采用的语义

### 2.1 OpenTelemetry Metrics 数据模型

[OpenTelemetry Metrics Data Model](https://opentelemetry.io/docs/specs/otel/metrics/data-model/) 明确区分 metric stream、Gauge、Sum、Histogram、temporality、monotonicity、start/end time、reset 和无记录值，并要求同一 stream 避免多写者歧义。这些概念适合作为 `plugin-statistics/v1` 的指标语义来源：

- 指标类型封闭为 `gauge|sum|histogram`；
- `sum` 才可声明 monotonic，temporality 明确为 `delta|cumulative`；
- 点必须携带时间窗和质量，missing/gap/reset 不能补 0；
- 同一 series identity 在一个 generation 内只有一个有效 producer；
- 跨窗口、generation、reset epoch 的聚合必须由合同明确允许。

项目不直接把 OTel wire 或 Collector 作为插件业务 Artifact；它只提供成熟的数据语义。业务统计仍进入 Go/PostgreSQL 的受权投影。

### 2.2 Prometheus 命名与基数经验

[Prometheus metric naming](https://prometheus.io/docs/practices/naming/) 和 [instrumentation guidance](https://prometheus.io/docs/practices/instrumentation/) 适合约束名称、单位、可聚合性和维度基数，但 Prometheus 不适合作为插件统计事实库：高基数 identity 会消耗内存、CPU、磁盘和网络，且其 scrape/retention 语义不能承担业务授权、审计或 current projection。

因此插件统计可以借鉴稳定名称和基础单位，但不得自动导出为 Prometheus series。Prometheus 只接收低基数的运行次数、时延、失败、质量和队列聚合；plugin/result/rule/IP/五元组/artifact identity 不得成为 label。

### 2.3 Grafana DataFrame 与 ECharts dataset

[Grafana DataFrame](https://grafana.com/developers/dataplane/dataframes) 使用 frame/field/value/meta 组织查询结果，[Grafana time series contract](https://grafana.com/developers/dataplane/timeseries/) 则强调时间字段、数值字段、null 和排序。这适合作为 Go API 到前端内部 projection 的参考，但它是面向 UI 的传输/转换结构，不应成为插件持久化 Artifact 的权威格式。

[Apache ECharts dataset](https://echarts.apache.org/handbook/en/concepts/dataset/) 证明“数据集与系列映射分离”可以复用同一数据并减少图表配置重复；[ECharts ARIA](https://echarts.apache.org/handbook/en/best-practices/aria/) 提供可访问性基础。因此前端可以把已校验的统计 Artifact 转成内部 `dataset/encode`，但插件不得提交 `EChartsOption`、formatter、callback、`renderItem`、event handler 或 custom series。

### 2.4 Backstage 的宿主定义扩展点

[Backstage extension blueprints](https://backstage.io/docs/frontend-system/architecture/extension-blueprints/) 展示了由宿主定义扩展种类、配置和 attachment point 的成熟模式。MASI-NIDS 采用其“宿主定义、类型封闭、明确挂载点”的思想，但不采用其 React element/code 扩展方式：本系统的插件不能注入 Vue/React 组件、route、导航或 JavaScript。

### 2.5 进程外插件生命周期

[Dapr pluggable components](https://docs.dapr.io/developing-applications/develop-components/pluggable-components/pluggable-components-overview/) 和 [HashiCorp go-plugin](https://github.com/hashicorp/go-plugin) 支持进程外、类型化 RPC、握手和生命周期管理的模式；[OpenTelemetry Collector components](https://opentelemetry.io/docs/collector/components/) 也使用封闭组件类别和 Start/Shutdown 生命周期。

项目只采用这些边界模式，不引入 Dapr/Collector 第二控制面，不采用 socket 自动发现作为准入，不把 go-plugin 当跨机零信任协议，也不允许插件自建 scheduler、queue 或数据库扫描器。Go Plugin Manager 仍是唯一 catalog/binding owner。

## 3. 明确拒绝的方案

### 3.1 任意 Vega/Vega-Lite 或表达式执行

[Vega expression interpreter](https://vega.github.io/vega/usage/interpreter/) 说明默认表达式运行使用代码生成，受严格 CSP 限制；解释器模式则有额外性能代价。允许第三方提交任意 Vega/Vega-Lite spec 还会把表达式、URL、transform 和渲染能力扩大为新的资格与安全面。因此首期拒绝任意 Vega/Vega-Lite spec、表达式语言和远程数据源。

### 3.2 任意 ECharts/Vue 配置或 HTML

插件不得提交 HTML、SVG、CSS、template、Vue component、JavaScript、URL、iframe、MIME handler 或完整图表 option。所有文本视为不可信纯文本，通过 Vue 插值或等价安全 sink 展示；不得使用 `v-html`。这与 [OWASP XSS Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html) 的上下文编码和安全 sink 原则一致；CSP 是附加防线，不能替代输出约束。

### 3.3 浏览器直连插件和插件自定义路由

浏览器直连插件会复制认证、授权、缓存、错误和兼容语义，也会让插件可用性影响 Web shell。首期只提供宿主固定路由 `Plugins → <plugin> → Statistics` 和受限详情页，不允许插件声明一级导航、route、action 或 mutation。这里禁止的是插件提供 action；宿主仍可通过 generated Go client 提供固定 `Run now` 与 schedule 管理，但必须服务端重新授权、绑定 exact definition/binding、使用幂等键并查询原 run。

## 4. 推荐合同

`PluginStatisticsDefinitionV1` 应随 immutable plugin revision/capability 签入 manifest，声明稳定 definition identity/digest、producer kind、host-owned input projection/field IDs、data class、允许的 scope/window/trigger、output/display profile、freshness 和请求的资源预算；`read-only-tool` 可额外引用 manifest/binding 已批准的 external-source capability ID，但不内嵌 endpoint/URL/credential。它采用 Backstage 式“宿主定义扩展点”思想，只引用项目 registry 中的 typed ID；不接受 SQL、PromQL、JSONPath、MCP prompt、表达式或运行时注册。Go 只授予 manifest、binding policy 与系统 profile 的交集。

`StatisticsInputBundleV1` 至少包含 run/request identity、exact plugin binding/revision/config/generation、definition ID/revision/digest、scope、半开时间窗、`as_of`、source facts/profile/generation/epoch/sequence/coverage/quality references、input digest、预算和 deadline。

`PluginStatisticsArtifactV1` 至少包含：

- exact producer identity、definition ID/revision/digest、scope/window、input/result digest；
- produced/observed/valid/expires time；
- 独立的 run status 与 artifact quality；
- 有界 metrics/series/tables/display hints；
- provenance、limitations、coverage、truncation 和计数；
- 只引用稳定字段 ID，不包含表达式、代码或任意 URL。

若 producer 是 `read-only-tool`，provenance 还必须包含已批准 external-source capability ID、canonical request digest、external observed time、response content digest 与可用的 ETag/version、partial/timeout 状态；不回传 endpoint、credential 或无界 raw response。同一 run 重试获得不同 external digest 时应稳定冲突并保留旧 current，而不是任选一次结果。

run status 封闭为 `queued|running|succeeded|failed|cancelled|expired|fenced`；artifact quality 封闭为 `valid|partial|gap|stale|no_data|not_measurable|invalid`。每个点另区分 `valid|missing|gap|reset|late|invalid`，并拒绝 NaN/Inf、重复冲突和未声明的乱序。

首期展示 kind 封闭为 `metric-card|status|timeseries|bar|heatmap|table|text|evidence-list`。显示提示只能引用已声明字段、单位、排序和有限视觉语义；最终组件、design token、ECharts option、ARIA 摘要和数据降采样都由 Web 自有 renderer 决定。

## 5. 初始资源与运行边界

建议首期 profile 固定：每个 plugin revision 最多 32 个 statistics definition，definition descriptor block 最多 128 KiB，每 definition 最多 64 个 input field ref 与 1 个 external-source capability ref，definition 全部人类文本最多 64 KiB；输入不超过 2 MiB，Artifact 不超过 1 MiB；每个 Artifact 最多 32 个指标定义、64 条 series、总计 10,000 个数值点、单 view 2,000 点，每个 histogram point 最多 64 个 bucket 且计入总点数；最多 8 张表、每表 32 列、总计 2,000 行、200 个 evidence ref；最多 16 个 display hint；JSON 深度不超过 8，单个 definition/Artifact text/string scalar 不超过 4 KiB，Artifact 全部文本不超过 64 KiB；每条 series 最多 8 个 dimension key，key/value 分别不超过 64/128 UTF-8 bytes。每 binding 最大 in-flight 2、待运行 32、单次 total deadline 10 秒；run/artifact 初始保留 30 天。

这些数字是工程复杂度和浏览器预算之间的保守首期值，必须进入 `contracts/profiles/v1` 并通过 benchmark 才能形成资格；不是上游标准承诺。放宽任一限制必须创建新 profile 并重跑 Go/Host/plugin/PostgreSQL/Web 的资源、故障和性能矩阵。

## 6. 安全、导出与可访问性

- Go 根据源事实的数据分类和用户 scope 重新授权；插件不能决定 ACL。
- on-demand 需要源数据 read 与 `plugin.statistics.run`；schedule create/revise/disable 需要 scoped Platform Admin、相关 data-class scope、CSRF/适用 step-up、immutable revision/digest 和审计。schedule 不是永久授权，每次运行都重新验证 binding/revocation/policy/scope/external capability。
- JSON 是首选导出。CSV 导出必须按 [OWASP CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection) 处理公式前缀、分隔符、引号和换行，不能把插件字符串原样交给电子表格。
- 未知 display kind/major/field reference 必须拒绝或显示受控 unsupported，不提供 raw JSON/HTML fallback。
- 每张图必须显示单位、时间窗、新鲜度、coverage/quality/truncation，并提供等价表格或文字摘要；颜色不是唯一编码。
- 插件 disabled/revoked/unavailable 时只使对应统计结果 stale/unavailable，核心页面和内置统计保持可用。

## 7. 工程复杂度评估

这不是“零成本”的功能，但比开放通用 UI 插件显著可控。新增复杂度集中在四个明确位置：两个版本化 schema、Go 的低频 run/projection 模块、PostgreSQL 四张逻辑表、Web 固定 renderer 与测试矩阵。它不增加可部署模块、插件 kind、P4 writer、effect queue、浏览器 runtime 或实时热路径依赖。

因此独立评估为：首期值得实现，且应作为通用插件平台的正式输出能力；但必须坚持“插件给数据，宿主给界面”，否则供应链、CSP、XSS、兼容、性能和前端回滚成本会快速失控。

## 8. 落点

- 强制需求：`../masi-nids-vnext-system-requirements-2026-08-09.md`
- 决策：`../adr/0018-plugin-statistics-and-declarative-web-projection.md`
- 通用插件边界：`../adr/0003-controlled-general-plugin-platform.md`
- 契约 profile：`../adr/0005-contract-runtime-and-protocol-profiles.md`
- 资格证据：`../adr/0006-qualification-levels-and-evidence.md`
- Web：`../adr/0007-vue-soc-console-and-source-reuse.md`
- 核心规则统计边界：`../adr/0008-rule-effectiveness-observation.md`
