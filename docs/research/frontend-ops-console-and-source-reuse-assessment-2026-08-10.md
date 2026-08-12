# MASI-NIDS vNext 前端运维控制台与源码复用独立评估

- 评估日期：2026-08-10
- 性质：成熟方案调研、工程/供应链评估与来源登记；不替代需求基线、ADR 或正式法律意见
- 需求基线：原始评估 `vNext-requirements-1.6`；当前基线 `vNext-requirements-1.17`
- 状态：Frontend 技术栈、clean-room 与供应链决策继续有效；后续 Firewall、Fleet、Rule Effectiveness 和 Central Inference Pool 页面以当前需求与相应 ADR 扩展；仓库尚无 Frontend 实现、lockfile、SBOM、LICENSE/NOTICE 或运行证据，均为 `HOLD/NOT RUN`

## 结论

建议采用“独立 Vue 3/Vite SOC SPA + Go 同源认证/API + 成熟基础库 + MASI-NIDS 业务组件 clean-room 实现”。不建议嵌入、fork 或复制 1Panel/sub2api 的业务前端。

可以复用成熟方案的源代码模块，但优先级应是：发布良好的独立 package/tool > 经审查的 permissive source module/fork > clean-room 借鉴应用交互 > GPL/LGPL 应用源码。真正能避免重复造轮子的模块是 router、UI primitive、server-state query/cache、chart、virtualization、test runner、a11y tooling 和 OpenAPI generator，不是对方的 Dashboard.vue、store、API wrapper、权限菜单或部署动作。

## 成熟产品模式复核

| 方案 | 可借鉴模式 | 不应带入的边界 | 来源 |
|---|---|---|---|
| 1Panel `dev-v2` | Vue 3/Vite/Pinia/Router/Element Plus/ECharts/Tailwind-Sass 技术组合；侧栏、状态卡、资源趋势、全局运维密度 | 主机/容器/应用商店控制面、账号权限、API/store、页面源码、品牌/theme/assets | [frontend package](https://github.com/1Panel-dev/1Panel/blob/dev-v2/frontend/package.json)、[repository](https://github.com/1Panel-dev/1Panel)、[GPL-3.0 license](https://github.com/1Panel-dev/1Panel/blob/dev-v2/LICENSE) |
| sub2api | Vue/Vite/Pinia/Router/Tailwind 的轻量管理台；摘要卡、时序图、列表/详情与 channel monitor 思路 | subscription/channel 业务模型、API、状态、组件源码、文案/主题 | [frontend package](https://github.com/Wei-Shaw/sub2api/blob/main/frontend/package.json)、[repository](https://github.com/Wei-Shaw/sub2api)、[LGPL-3.0 license](https://github.com/Wei-Shaw/sub2api/blob/main/LICENSE) |
| Grafana | 全局时间范围、变量/筛选、dashboard drill-down、data link、时区/单位/数据新鲜度 | 通用 dashboard builder、查询语言、插件市场和把浏览器当事实聚合器 | [Dashboards documentation](https://grafana.com/docs/grafana/latest/dashboards/) |
| Argo CD | desired/live diff、operation timeline、sync/rollback 可观察性；适合作为 exact P4 diff/readback 的交互类比 | Kubernetes 控制器、GitOps state machine 和“Sync”等同 P4 已生效 | [Diffing](https://argo-cd.readthedocs.io/en/stable/user-guide/diffing/)、[Sync options](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/) |
| OpenSearch Alerting/Dashboards | monitor/trigger/alert 分层、状态与调查入口；适合作为 Event/Incident/Proposal 分离的参考 | OpenSearch 数据模型、告警执行器和通用 dashboard runtime | [Alerting documentation](https://docs.opensearch.org/latest/observing-your-data/alerting/index/) |

这些来源支持“模式成熟”，不授权 MASI-NIDS 声称已实现、像素复刻、协议兼容或可以直接复用其控制面。

## 建议前端架构

| 层 | 所有者/候选 | 只负责 | 明确不负责 |
|---|---|---|---|
| Delivery/Auth | Go Control + 受信 gateway | 同源 assets/API/SSE/OIDC callback、HttpOnly session、CSRF、scope | SPA token storage、第二 BFF、前端授权 |
| Contract client | OpenAPI generated client | DTO、HTTP、error/cursor/idempotency adapter | 页面手写 URL/DTO、猜测服务端状态 |
| Server state | 资格化 Vue Query 类库 | query、dedupe、取消、失效、bounded cache | canonical facts、授权、离线 mutation |
| Local UI state | Pinia | theme、density、navigation、非敏感草稿 | Event/Decision/Intent/Operation truth |
| UI primitive | Element Plus + project wrappers | form/table/dialog/drawer/tabs/a11y primitive | proposal/approval/plugin 业务语义 |
| Visualization | ECharts + project chart contract | bounded trend/distribution rendering、ARIA | 无界原始数据、服务端聚合、事实判断 |
| Navigation | Vue Router | deep link、URL filters、route lazy load | 安全授权 |
| Test/quality | Vitest、Vue Test Utils、Playwright、a11y tools | unit/component/browser/a11y/regression | 资格措辞和人工风险判断 |

Go 应提供 Overview aggregate snapshot、稳定 cursor pages、单对象详情、proposal/decision/operation projection 和 multiplex SSE invalidation。浏览器不应为一张首页卡片调用十几个 endpoint 后自行决定系统是否 healthy，也不应把 Pinia/localStorage 当作审计事实。

## 交互设计评估

### 全局 shell

- 左侧任务导航：Overview、Detection、Evidence、Effects & Governance、Analysis、Plugins、Operations & Audit。
- 顶部上下文：scope/target、时间范围/时区、刷新/暂停、最后更新时间、HOLD/degraded、actor/role、build/profile。
- URL 是可复核的阅读状态：filter、cursor、sort、time、tab、selected ID 均可复制；敏感 token、reason 草稿和 secret 不进入 URL。

### Overview

采用一屏三层：运行健康与 freshness；Event/Incident/effect KPI；流量/风险趋势与最近待办。每张卡必须有单位、时间窗、定义、source、freshness 和 drill-down。避免 1Panel 式资源卡被误用为授权证据，也避免 sub2api 式业务指标直接套进 NIDS。

### Detection 与 Evidence

服务端 cursor table 提供稳定筛选和排序；抽屉用于快速研判，完整详情用于深链和审计。observed、inferred、unknown 分区显示；generation、model/evidence digest、capture window、freshness 永远可见。大数据只在服务端聚合/采样。

### Effects & Governance

这是不能复用第三方业务页面的核心。使用独立 workspace：左侧 current facts/evidence，中间 exact logical P4 diff 与风险/容量/TTL/rollback，右侧授权条件、reason 与 action；底部固定 Proposal → Decision → Intent → Attempt → readback timeline。stale/unknown 不是普通 warning，必须改变可执行状态。R2 需要 step-up 与 maker-checker；不提供批量 approve 或“一键应用 Agent 建议”。

### Analysis 与 Plugins

Analysis Artifact 显式标记不可执行，展示引用证据、模型/provider/profile、预算、`analysis_outcome=limited|insufficient_evidence|failed`、trace 摘要和 freshness；可以打开独立 proposal builder，但不能提交 Agent payload，也不能把受限 Analysis outcome 混称为模型/runtime fallback。Plugins 只向有 scope 的 Platform Admin 展示 exact revision/digest/config/capability/resource/qualification/revocation/generation，不使用 `latest`。

### Model Pool Operations

页面把“管理员期望”“进程实际加载”“服务可用性”和“资格证据”分开：显示desired/selected/observed CPU或CUDA profile、exact current/previous generation、model/runtime/image/config digest、single/HA availability、replica/capacity以及HA profile适用的failure-domain/N+1、CPU core/thread/NUMA/RAM或GPU/driver/CUDA/cuDNN/VRAM、startup/load/warmup/readback与CPU/CUDA独立E2E evidence。管理员只能选择已资格化profile创建新generation或exact rollback；不提供`auto/best available`、runtime load/unload或CPU↔CUDA fallback控件。Ready不等于current，CPU PASS不显示成CUDA PASS。

## 成熟依赖与许可证评估

以下是候选，不是自动批准。实际使用必须核对 lockfile 中 exact version 的 LICENSE、NOTICE、嵌入资产和传递依赖。

| 能力 | 首选候选 | 上游许可证/来源 | 初步结论 |
|---|---|---|---|
| Framework | Vue 3 | [MIT](https://github.com/vuejs/core/blob/main/LICENSE) | 可作为直接依赖 |
| Build | Vite | [MIT](https://github.com/vitejs/vite/blob/main/LICENSE) | 可直接依赖；固定 browser target，不继承默认漂移 |
| Router | Vue Router | [MIT](https://github.com/vuejs/router/blob/main/LICENSE) | 可直接依赖，不自研 router |
| Local state | Pinia | [MIT](https://github.com/vuejs/pinia/blob/v3/LICENSE) | 可直接依赖；禁止保存 canonical server facts |
| UI primitives | Element Plus | [MIT](https://github.com/element-plus/element-plus/blob/dev/LICENSE) | 可直接依赖；按需导入并做 a11y/project wrapper |
| Charts | Apache ECharts | [Apache-2.0](https://github.com/apache/echarts/blob/master/LICENSE) | 可直接依赖；检查 NOTICE/嵌入第三方内容、按需导入 |
| Composables | VueUse | [MIT](https://github.com/vueuse/vueuse/blob/main/LICENSE) | 按需依赖，防止重叠 composable 与 bundle 膨胀 |
| Server state | TanStack Query Vue | [MIT](https://github.com/TanStack/query/blob/main/LICENSE)、[Vue overview](https://tanstack.com/query/latest/docs/framework/vue/overview) | 推荐；用 project wrapper 固定 retry/cache/session policy |
| Virtualization | TanStack Virtual | [MIT](https://github.com/TanStack/virtual/blob/main/LICENSE) | 推荐长列表候选；先验证 Element Plus table/keyboard/a11y 组合 |
| Unit/component | Vitest、Vue Test Utils | [Vitest MIT](https://github.com/vitest-dev/vitest/blob/main/LICENSE)、[VTU MIT](https://github.com/vuejs/test-utils/blob/main/LICENSE) | 推荐；仍需 browser/E2E/人工 a11y |
| Browser E2E | Playwright | [Apache-2.0](https://github.com/microsoft/playwright/blob/main/LICENSE)、[webServer](https://playwright.dev/docs/test-webserver) | 推荐；使用production artifact、真实浏览器与web-first assertions，不固定sleep。完整服务由受控runner启动；URL可达/dev server不等于system E2E |
| API generation | OpenAPI Generator `typescript-fetch` / openapi-typescript 候选 | [OpenAPI Generator Apache-2.0](https://github.com/OpenAPITools/openapi-generator/blob/master/LICENSE)、[generator docs](https://openapi-generator.tech/docs/generators/typescript-fetch/)、[openapi-typescript MIT](https://github.com/openapi-ts/openapi-typescript/blob/main/LICENSE) | 先 spike 再二选一；检查 template/generated header/runtime/bundle/错误与 cursor adapter，不在需求里永久硬编码工具 |

Apache-2.0 依赖需要保留适用 LICENSE/NOTICE；MIT 也需要 attribution。ECharts theme/example、图标、字体和 generated template 不能因主项目许可证而自动推定相同许可证。

## 源码模块复用判定

### 可以直接做

- 通过 lockfile 引用上述已资格化 package，不修改 upstream source；
- 使用官方公开 API 和文档编写 project adapter/wrapper；
- 使用生成器从 MASI-NIDS OpenAPI source 产生 client，并保留 generator/template 规定的 header/NOTICE；
- 学习“全局时间范围、drill-down、详情抽屉、desired/actual diff、operation timeline”等抽象模式，再以项目 code/token/copy/layout 实现。

### 可以，但必须先审查

- 修复上游 bug 而 fork/vendoring permissive source；
- 复制一个无法作为 package 消费的独立 utility；
- 使用第三方 ECharts theme、SVG/icon、font、fixture 或 code snippet；
- 从 sub2api LGPL 应用抽取/修改 Vue 组件。必须先证明项目许可证和构建/分发方式能持续满足 LGPL/其他义务；“只复制一个文件”不降低该要求。

每项必须登记 exact repository/commit/path、SPDX/copyright、digest、修改、测试、upstream issue/patch、NOTICE/source obligation、更新责任和移除方案。

### 默认不做

- 复制或派生 1Panel GPL 的 Vue SFC、API/store/composable、CSS/theme、SVG/icon、文案和页面组合；
- 复制 sub2api 业务 Dashboard/ChannelMonitor 后仅改名、换色或改字段；
- 使用 1Panel/sub2api logo、商标、截图或可识别品牌资产；
- 嵌入其页面、后端、数据库 schema、账号/权限、应用商店或部署控制面；
- 为了“插件化”加载任意第三方 UI JavaScript、remote module 或 iframe。

当前仓库没有项目 LICENSE/NOTICE。在 Owner 和正式合规审查选定分发策略前，任何 GPL/LGPL 应用源码导入必须为 `HOLD`。这是一条保守工程门禁，不是对许可证最终法律效果的裁定。

## 准入流程

1. 先写能力缺口：现有 approved dependency 是否已解决；为什么需要新增 package 或 source copy。
2. 冻结 exact upstream release/commit 与下载 digest，审查维护状态、安全公告、许可证、NOTICE、传递依赖、浏览器/Node support 和 bundle cost。
3. package 进入 lockfile/SBOM；vendored source 另建逐文件 manifest、保留原 license/header、记录 patch 和 source offer/NOTICE。
4. 执行 generated/client golden、unit/component、browser/a11y、bundle/performance、CSP/CSRF 和 current/previous compatibility。
5. Owner 接受资格 evidence 后才进入 `web-spa/v1`；升级创建新 profile revision，不用 range/`latest` 静默漂移。
6. CI 阻止未知 license、缺 NOTICE、unregistered vendoring、远程 runtime、lock/SBOM 漂移、已知漏洞超 policy 和 bundle/profile 超限。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| Vue SPA 把原 Next BFF 职责遗漏 | Go module black-box 明确 OIDC/session/CSRF/projection/SSE；Web 不完成前保持 HOLD |
| Element Plus/ECharts 体积增大 | 自动/按需 import、route lazy load、bundle budget 和 analyzer gate |
| Query cache 显示跨 actor/scope 旧数据 | session-aware query key、切换取消/清理、无 canonical persistence、E2E fault matrix |
| 图表不可访问或掩盖 unknown | ECharts ARIA、人工摘要/等价表格、semantic status、WCAG 自动+人工门禁 |
| 复制成熟页面造成许可证/维护债 | 应用 source 默认 reference-only，逐文件 vendoring manifest，license CI 和 near-copy review |
| 依赖供应链扩大 | lock、受控 registry/cache、SBOM/provenance、漏洞 policy、offline reproducible build、精确 profile |
| 静态 asset 滚动 404/旧 UI | HTML no-store/短缓存、hash asset immutable、双 asset set、exact digest rollback |
| mock页面全绿但真实系统起不来 | Module真实启动production Web；正式Go↔Web与system E2E在干净环境启动真实内部服务，保存service inventory/start-stop evidence；fake只用于模块邻居 |

## 尚未完成

- 尚未选择 exact Vue/Vite/Element Plus/ECharts/Query/Virtual/Test/generator 版本；
- 尚未建立 `web-spa/v1`、`web-browser/v1`、`web-performance/v1` schema/profile；
- 尚未形成项目 LICENSE/NOTICE、第三方依赖清单、lockfile、SBOM 或 vendored-source manifest；
- 尚未实现 Go OIDC/session/CSRF/SSE/aggregation 或 Web；
- 尚未执行真实production Web启动、browser、WCAG、bundle、Core Web Vitals、故障、兼容、Go↔Web pairwise或完整system E2E。

因此，本评估只确认目标架构和准入规则，不构成实现、许可证合规、Module PASS 或 production qualification。

## 质量依据

- Vue performance best practices：<https://vuejs.org/guide/best-practices/performance>
- Vite production build/browser compatibility：<https://vite.dev/guide/build.html>
- WCAG 2.2：<https://www.w3.org/TR/WCAG22/>
- Core Web Vitals thresholds：<https://web.dev/articles/defining-core-web-vitals-thresholds>
- ECharts accessibility/ARIA：<https://echarts.apache.org/handbook/en/best-practices/aria/>
- Playwright assertions/auto-retrying assertions：<https://playwright.dev/docs/test-assertions>
- Playwright webServer：<https://playwright.dev/docs/test-webserver>
- Vue Test Utils：<https://test-utils.vuejs.org/>
- OWASP CSRF prevention：<https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html>
- OWASP HTML5 storage guidance：<https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html>
- MDN Content Security Policy：<https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CSP>
