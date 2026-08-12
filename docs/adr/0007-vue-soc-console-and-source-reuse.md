# ADR-0007：Vue SOC 控制台、交互边界与第三方源码复用

- 状态：Accepted
- 日期：2026-08-10
- 决策者：Owner
- 需求基线：`vNext-requirements-1.17`（原始前端决策形成于 v1.8）
- 关联需求：`MOD-WEB-001`、`MOD-TARGET-FLEET-001`、`FUNC-API-001`、`FUNC-RULE-001`、`FUNC-INF-MODEL-001`、`FUNC-TARGET-FLEET-001`、`WEB-UX-001`、`WEB-STATE-001`、`WEB-GOV-001`、`WEB-VIS-001`、`WEB-A11Y-001`、`WEB-PERF-001`、`WEB-SEC-001`、`WEB-SUPPLY-001`、`WEB-RULE-001`、`WEB-TARGET-FLEET-001`、`OBS-INF-001`、`OBS-RULE-001`、`OBS-TARGET-FLEET-001`、`REL-INF-POOL-001`、`DEP-WEB-001`、`TEST-INF-001`、`TEST-REAL-E2E-001`、`TEST-WEB-001`、`TEST-RULE-001`、`TEST-TARGET-FLEET-001`、`SEC-002`、`SEC-TARGET-FLEET-001`、`DEC-009`、`DEC-022`、`DEC-023`、`DEC-024`、`DEC-027`、`DEC-032`、`DEC-033`、`DEC-034`、`DEC-035`

## 背景

v1.3 曾暂定 React/Next.js 并保留 BFF。进一步复核后，MASI-NIDS 是受控内部 SOC 控制台，没有 SEO/SSR 需求；若同时保留 Next BFF 与 Go Control，会产生第二会话/聚合层、两套错误/缓存语义和额外滚动兼容面。Owner 要求采用 1Panel 当前前端同类技术栈，并参考 1Panel、sub2api 等成熟运维仪表盘。

[1Panel `dev-v2` frontend package](https://github.com/1Panel-dev/1Panel/blob/dev-v2/frontend/package.json) 展示了 Vue 3、TypeScript、Vite、Pinia、Vue Router、Element Plus、ECharts、Tailwind/Sass 等组合；[sub2api frontend package](https://github.com/Wei-Shaw/sub2api/blob/main/frontend/package.json) 也采用 Vue/Vite/Pinia/Router/Tailwind 与图表库。这证明技术组合和常见交互模式成熟，但不证明它们的业务页面、权限模型或源码适合直接合并。

同时，1Panel 应用采用 [GPL-3.0](https://github.com/1Panel-dev/1Panel/blob/dev-v2/LICENSE)，sub2api 采用 [LGPL-3.0](https://github.com/Wei-Shaw/sub2api/blob/main/LICENSE)。当前 vNext 仓库尚无项目级 LICENSE/NOTICE。直接复制 Vue SFC、store、CSS、SVG 或业务 composable 会引入衍生作品、分发和源码提供义务的不确定性。以下是工程与供应链决策，不替代组织的正式法律意见。

## 决策

### 1. 独立 Vue SPA 与唯一后端边界

- Web 使用 TypeScript、Vue 3 Composition API、Vite、Vue Router、Pinia、Element Plus 和 Apache ECharts；VueUse 仅作为经资格化、按需导入的通用 composable 候选，不能拥有 server/business state。具体版本、build target、按需模块与依赖 digest 固定在 `web-spa/v1`。
- Web 是独立静态 artifact/OCI image，不包含 Next.js、Node SSR 或 Web BFF。生产通过一个 HTTPS origin 提供 assets、`/api`、`/events` 和 OIDC callback。
- Go/受信认证组件拥有 OIDC code exchange、HttpOnly session、CSRF、scope、业务授权、canonical projection 和 SSE；SPA 只负责展示与输入，不读取 token，不直连任何核心/插件/设备服务。
- 不引入前端微服务、micro-frontend、runtime module federation、iframe 控制面或 UI plugin kind。插件只能通过 Go 投影展示，不能注入浏览器代码。

### 2. 任务导向的信息架构

一级任务固定为 Overview、Detection、Evidence、Effects & Governance、Analysis、Plugins、Operations & Audit；Effects & Governance 下分开提供 Incident 驱动的 `Response Rules`、长期 `Firewall Policies` 与只读 `Rule Effectiveness`，并在 Effect/Operation 详情中提供 Rule Observation 页签；Plugins 下提供 catalog/binding/qualification 运维和只读 `Plugin Statistics`；Operations & Audit 下提供 `Managed Targets`、`Fleet Operations` 与 `Model Pool Operations`。Managed Targets 显示 stable identity、desired/observed profile、Edge assignment/actor/mastership、P4Info/application generation、freshness/drift；Fleet Operations 显示冻结 target set、静态 waves、target×stage child vector、partial/reconciling 与逐 target rollback。Model Pool Operations 分开显示 qualification、desired/selected/observed CPU或CUDA profile、exact model/runtime/image/config/repository digest、logical pool current/new/previous generation、single/HA availability、replica/min-ready/capacity以及HA profile适用的failure-domain/N+1、CPU core/thread/NUMA/RAM或GPU/driver/CUDA/cuDNN/VRAM、ordered shard set、per-shard current/previous route、new-generation startup/warmup/readback、drain/CAS/resume、`rolling_mixed|rollout_failed_mixed`、rolling rollback与CPU/CUDA独立E2E evidence。借鉴成熟控制台的侧栏、全局范围/时间、状态卡、趋势图、可筛选表格、详情抽屉和 drill-down，但所有页面依据 MASI-NIDS API 与状态语义重新设计。

Overview 只消费有界聚合 snapshot；列表采用服务端 cursor；过滤、排序、时间、tab 与选中对象进入 URL；抽屉用于快速研判，每个核心对象仍有可深链完整页面。所有页面显式处理 loading、empty、partial、stale、HOLD、unauthorized、unavailable、error、retrying 和 ready，禁止把旧缓存无标记地显示为 current。

### 3. Server state、Pinia 与 SSE 分工

- OpenAPI generated client 是唯一 HTTP client。generator/template、source/output digest 和适配器测试进入 profile。
- 资格化 Vue Query 类库管理 server state；Pinia 只保存主题、密度、导航和非敏感草稿。canonical facts、授权结果和 operation 不持久化到 Pinia/localStorage/IndexedDB/Service Worker。
- 每个 cache key 绑定 scope、target、filter/cursor 和适用 generation/revision；actor、scope、profile、generation 变化先取消请求并清 cache。
- mutation 不乐观提交成功，不自动重建 operation。timeout/5xx 只查询同一 idempotency/operation identity。
- 每个 tab 最多一个 multiplex SSE，事件只承载有界 invalidation/小投影。cursor gap、乱序、generation 变化触发 snapshot refetch；连续失败退化为有界 polling，并在 UI 明确标识 degraded/stale。

### 4. 危险操作是独立产品流程

常规 UI primitive 可以复用，安全关键业务组件必须由项目独立实现并黑盒验收：

- Analyst proposal builder 只从当前 Go canonical facts 构造 exact diff；Agent Artifact 只能作为 hash 引用。
- Operator approval workspace 同屏显示事实/推断/未知、exact diff、risk、scope、digest、evidence freshness、generation/P4Info、capacity、TTL/expiry、rollback 和 readback。stale/缺失事实禁用 approve。
- R2 必须服务端确认 step-up 与 maker-checker。无批量 approve、默认 approve、单键确认或模糊“确定”。
- Baseline Firewall workspace 由 Platform Admin 创建 immutable revision，显示 normalized diff、explicit default、overlap/shadow、compiled entry/capacity、target/P4Info 与 current/desired/previous；不同 Operator 才能 step-up 授权 typed R3 activation。timeline 分开显示 inactive write/readback、selector switch/readback、PG CAS、previous grace/cleanup；浏览器不生成 raw P4 entity 或把 Write ACK显示为 current。
- Target registration/assignment workspace 只接受 candidate→server diff→validate→Platform Admin recent step-up→canonical result；endpoint、P4 `device_id`、profile、scope、Edge assignment 的 exact diff 与 duplicate/SSRF/identity 风险必须同屏，页面不提供自动 takeover、SSH/CLI、raw P4 或 gNMI `Set`。
- Fleet approval workspace 在确认页重新显示冻结的 exact target set/wave/failure policy、每 target plan/P4Info/generation/capacity 与 maker-checker/step-up；URL、当前过滤器、“全选”或 group membership 不是授权 scope。parent 只作聚合，任何 child unknown 均显示 `reconciling`，禁止多数成功或一个绿色 badge 掩盖 mixed state。
- 提交后展示 Proposal → Decision → Intent → Attempt → readback 时间线，并保留 unknown/reconciling；UI 明示审批不是 P4 已生效。
- Plugin Admin 同样绑定 exact revision/artifact/config/profile/scope/generation，不提供 `latest` 激活。
- Model pool rollout/rollback绑定exact model bundle/repository、feature/label/output-adapter/wire、管理员显式选择的`model-runtime-central-cpu/v1`或`model-runtime-central-cuda/v1`、Gateway/Triton/ORT/observed hardware/optimization/resource/qualification、single/HA availability、model-control incarnation、logical pool/pool+binding generation、replica/capacity以及HA profile适用的failure-domain/N+1、scope/shard set、per-shard route/current/previous和operation；展示probe/stage/start/warmup/readback、profile mismatch、route-withdraw/drain/CAS/resume-pending/gap/quarantine/old-generation stop和E2E evidence链接。Gateway/Triton/Pod `ready`不显示为current，mixed rollout不隐藏为成功，tag/alias不进入提交payload。首期UI不提供`auto/best available`、Triton runtime load/unload/poll、candidate shadow、weighted split、第二router、Edge-local或CPU↔CUDA/异模型自动fallback控件；新增label明示默认无自动effect eligibility。

### 5. 自有视觉、可访问性与性能

- 使用 primitive → semantic → component 三层 token，同一 token 驱动 Element Plus、Tailwind utilities、Sass 和 ECharts；建立自有品牌、图标选择、文案、布局与 light/dark/密度，不制作 1Panel/sub2api 换色版。
- 精确事实用表格，趋势/分布用适合的 line/bar/area/heatmap；每图有单位、时区/时间窗、新鲜度、采样、上限、状态和等价表格。unknown/HOLD 与 failed 必须可区分。
- 完整页面达到 [WCAG 2.2 Level AA](https://www.w3.org/TR/WCAG22/)；ECharts ARIA 需要显式启用并为复杂图表提供人工摘要/表格。自动扫描与键盘、屏幕阅读器、zoom/reflow 人工复核同时作为门禁。
- 遵循 [Vue performance guidance](https://vuejs.org/guide/best-practices/performance) 做 production build 测量、tree shaking、route lazy load 和大列表 virtualization；浏览器 target 不继承 [Vite](https://vite.dev/guide/build.html) 随 major 变化的默认值。
- `web-performance/v1` 初始限制应用 shell 同步入口 JS 250 KiB gzip、首屏 CSS 80 KiB gzip、单路由首次静态 JS+CSS 600 KiB gzip，以及列表/图表/request/SSE 的显式上限。在目标 profile 上以 p75 验证 LCP ≤2.5 s、INP ≤200 ms、CLS ≤0.1；阈值依据 [Core Web Vitals](https://web.dev/articles/defining-core-web-vitals-thresholds)。
- Managed Targets 与 Fleet Operations 必须使用服务端 cursor、虚拟化与有界 target×stage matrix；`max_targets_per_control/fleet_operation/wave`、API page/SSE invalidation/chart/table points、DOM/heap 与交互 p95 在 `p4-target-fleet/v1`/`web-performance/v1` 同时冻结，N 未冻结时不宣称大规模 fleet PASS。

规则表现必须遵守 ADR-0008 的分层语义：安装确认、数据面命中、action outcome 与 effect execution 分开；Top-N 水平条形、pps/bps 时间序列、状态 timeline 和 cursor table 都显示 formula、numerator/denominator、coverage、generation/window、新鲜度、gap/reset/no traffic/not measurable。禁止单一“生效率” gauge、把 null 补零或把轮询时间伪装为精确 last hit。

`Plugin Statistics` 使用项目内建 renderer 消费 Go 校验后的 `plugin-statistics-display/v1`，仅支持 `metric-card|status|timeseries|bar|heatmap|table|text|evidence-list`。definition list 只来自 active immutable manifest 中已资格化的 `PluginStatisticsDefinitionV1`，Web 不接受运行时注册。页面显示插件/revision/config/binding generation、definition ID/revision/digest、scope、input/window、artifact digest、quality/freshness/coverage 和 run 状态；query key 同样绑定 definition identity，历史使用服务端 cursor，SSE 只做 invalidation。宿主可按 Go 返回的 scope 提供固定 `Run now` 与 schedule list/create-revise-disable 控件；字段、确认、幂等、限流、原 run query 和审计由项目拥有，插件不得声明 action、表单或定时表达式。Go 将 Artifact 规范化为 ECharts `dataset`/`encode` 所需的纯数据，Web 不接收插件生成的 `EChartsOption`、Vega spec、HTML/SVG/CSS/JS、URL/MIME、formatter、`renderItem`、event、expression 或 custom series。未知 display kind 必须拒绝，不提供 raw/HTML fallback。

统计 Artifact 的缺失、gap、reset、late、stale、no_data、not_measurable 与 invalid 必须保留，不能补成 0。表格列类型与分页由合同封闭；JSON 是首选导出，CSV 由服务端实施公式注入防护。插件页面不可见或统计插件停用时，核心 Overview、Rule Effectiveness、Event/Incident 与处置页面仍完整可用。

### 6. 源码复用采用四级判定

| 复用类型 | 默认结论 | 必须满足 |
|---|---|---|
| 独立成熟 package/runtime tool | 可准入，优先于自研 | exact version/digest、lockfile、license/NOTICE、SBOM、漏洞/维护、bundle/兼容/性能和离线构建 |
| permissive-licensed 独立源码模块或必要 fork | 条件准入 | Owner 审批；逐文件 upstream commit/path/license/digest/修改/测试/更新/退出登记；保留 NOTICE/attribution |
| 1Panel/sub2api 等应用的交互思想、IA、行为 | 可 clean-room 参考 | 只复用思想；以项目契约、自有代码/token/文案/布局/资产重新实现并留设计来源记录 |
| GPL/LGPL 应用页面、业务 store/API、CSS/theme、SVG/品牌资产 | 默认禁止或 HOLD | 项目许可证已决定；完成正式合规审查；能持续满足源码/修改/链接/分发等义务；纳入 SBOM/NOTICE/测试/更新；否则不得导入 |

首选成熟能力边界如下：

- Vue Router 负责路由，Pinia 负责本地 UI state，资格化 Vue Query 负责 server state，TanStack Virtual/同类库负责虚拟化，VueUse 只复用按需、无业务所有权的 browser/composable primitive；不自研第二套 router/query cache/virtual list，也不让 composable cache 变成事实源。
- Element Plus 负责经过 a11y 修正的基础表单、dialog、table primitive；安全关键 proposal/approval/plugin binding 是项目组件，不复制第三方业务 SFC。
- ECharts 负责图表 engine；主题、数据语义、ARIA 描述和聚合由项目拥有。example/theme/icon/font 仍逐项核对许可证。
- ECharts `dataset`/`encode` 只作为 Web 内部映射；插件只提交项目 schema。Backstage 的 blueprint/extension 机制只借鉴“类型化扩展点”思想，不引入其 runtime UI extension；Vega interpreter 可满足更严 CSP 的事实不构成开放任意 spec 的理由，首期明确拒绝该额外表达式/兼容面。
- Grafana 可以复用为低基数运维 dashboard/state timeline 和只读 deep link，但不嵌入高权限 session、不 provision Action/API mutation、不承载 per-rule canonical detail；Rule Effectiveness 的授权详情和历史仍由 Go/SPA 提供。
- NetBox/CMDB 可作为 candidate inventory 来源，但不嵌入其 UI、权限或 current；ONOS/Stratum controller、Ansible/Nornir console 也不进入 SPA。所有 candidate diff、assignment、fleet gate 与 per-target result 由 Go 投影和项目自有安全关键组件呈现。
- Vitest、Vue Test Utils、Playwright 负责测试分层；OpenAPI Generator、openapi-typescript 或其他候选必须先以同一 API fixture 比较生成正确性、运行时代码、许可证、bundle、错误/cursor/SSE 适配和升级稳定性，再固定一种 profile。

### 7. 发布、兼容与回滚

- HTML/config 短缓存或 no-store，content-hashed asset immutable；发布原子切 manifest，并保留当前和上一 asset set。
- Web 与 Go 独立滚动，但必须测试 current/previous API、session cookie、CSRF、SSE cursor/event、deep link 和 static manifest。回滚只切 exact Web artifact digest，不改变 server facts。
- CSP 默认 self-only 且禁止 remote runtime、`unsafe-eval`、任意 iframe/module；首期不注册 Service Worker。
- greenfield 不运行 Next 与 Vue 双前端，也不长期兼容两套 BFF/API。若 Vue 方案在 Module 门禁前被证伪，必须以新 ADR/需求基线重新决策，不能临时把第二控制面带入生产。

## 独立评估

可以借鉴成熟解决方案的源码模块，但“模块”应优先理解为独立、通用、版本化的上游 library/tool，而不是从成熟应用中剪出页面。对本项目最有价值的复用是 router、UI primitive、query cache、chart、virtualization、test runner 和 code generator；这些边界稳定，社区维护和测试收益明显。

直接复制 1Panel/sub2api 业务前端的净收益较低：它会把对方的 API、菜单、权限、状态、样式耦合和许可证义务一起带入，而 MASI-NIDS 的 exact P4 diff、stale/HOLD、maker-checker、operation readback 和 plugin qualification 又必须重写。工程上更快且长期成本更低的路径，是复用基础库并 clean-room 实现薄的 MASI-NIDS 业务组件。

因此，本 ADR 不采用“全部自己写”，也不采用“fork 成熟产品再删减”；采用“成熟基础能力直接依赖 + 少量经审查源码 vendoring + 应用模式 clean-room 重实现”。

## 取舍

收益：删除 Node BFF 和重复认证/缓存层；与 Owner 指定技术栈一致；基础设施复用率高；危险操作保持项目可验证语义；许可证与来源在引入前处理；Web 可独立部署回滚。

代价：需要 Go 补齐同源 session/CSRF/SSE/聚合边界；需要建立 design system 和业务组件；Vue/Go compatibility、browser/a11y/bundle/license 增加独立门禁；不能通过复制成熟页面快速得到表面完成度。

## 被拒绝的方案

1. 保留 Next.js BFF：内部 SPA 没有 SSR 收益，却产生第二 session/aggregation runtime。
2. fork/嵌入 1Panel：引入 GPL、控制面/API/权限耦合和大面积无关功能。
3. 直接复制 sub2api 页面：LGPL 不等于无条件 permissive；业务语义和 MASI-NIDS 不一致。
4. 全部 UI primitive 自研：重复 router/query/cache/chart/a11y/test 基础设施，稳定性与维护成本更差。
5. runtime UI plugin/micro-frontend：扩大浏览器供应链、CSP、授权和兼容面，并违反首期封闭 plugin kind。
6. 只做视觉换肤：无法证明 clean-room，也无法满足 exact governance 与状态语义。

## 实施顺序

1. 冻结 `web-spa/v1`、`web-browser/v1`、`web-performance/v1`、OpenAPI source、dashboard/SSE contract、design tokens、license policy 和测试接口。
2. 建立Vue shell、generated client、query/session/cache boundary与OpenAPI/OIDC/SSE contract fake；以production bundle、资格化真实浏览器和公开HTTP边界真实启动Web module black-box。Go可在该等级由fake模拟，但Web自身不得由静态截图、组件stub或dev server替代。
3. 以 Overview → Managed Targets → Event/Incident/Evidence → Response Proposal/Approval/readback → Firewall Policy revision/R3 activation → Fleet Operations → Rule Effectiveness → Model Pool Operations → Analysis → Plugin Statistics → Plugins/Operations 的顺序完成纵向切片；每片先覆盖所有状态、a11y 与资源上限。
4. Go 在自身模块内完成同源 OIDC/session/CSRF、projection 和 SSE，并分别达到 Module Complete；此前真实 wire 只算 rehearsal。
5. Web通过`TEST-WEB-001`后，等待全模块gate，再在干净环境真实启动Go与Web做正式pairwise；系统E2E真实启动完整内部服务并由Playwright/资格化浏览器走同源`/api`与`/events`，不得复用mock/rehearsal状态。

## 验证

- generated client drift、unknown API/profile、error/cursor/SSE adapter；
- actor/scope/session/generation 切换 cache 隔离，SSE gap/乱序/断网/polling 恢复；
- Analyst/Operator/Admin/Auditor 权限与 proposal/approval/plugin exact-binding 全流程；
- model qualification/desired rollout、CPU/CUDA desired/selected/observed与mismatch、single/HA availability、logical pool current/new/previous、replica/capacity以及HA profile适用的failure-domain/N+1、per-shard current/previous route、stage/start/warmup/readback/drain/CAS/resume、mixed/failed/full-pool-unavailable/gap、exact rolling rollback、CPU/CUDA独立E2E evidence、timeout后原operation、unknown label/OOD和离线comparison不可执行；Pod/Triton Ready、mutable tag、runtime load/unload、`auto`与fallback控件负例通过；
- timeout 后原 operation、unknown/readback、stale/expiry/step-up/maker-checker 与零重复 mutation；
- Rule Effectiveness 的 install/hit/no-hit/no-traffic/reset/gap/stale/not-measurable/outcome，公式/coverage/Top-N/timeline/table 和 no-hit 候选零 mutation；
- Plugin Statistics 的 8 种封闭 renderer、run/quality/freshness/source identity、empty/partial/gap/stale/no-data/not-measurable/invalid、分页/虚拟化/ARIA/等价表格；固定 `Run now`/schedule 控件分别验证 source-read + `plugin.statistics.run`、scoped Admin/data-class/CSRF/step-up/idempotency、append-only revision、每次执行重新授权与原 run query；unknown kind、oversize/cardinality、HTML/SVG/JS/URL/vendor option/formatter/expression/插件 action/CSV formula 负例通过，浏览器 network trace 中无 plugin endpoint；
- Firewall Policies 的 draft/validated/authorized/activating/current/previous/unknown/reconciling/failed/HOLD、diff/default/shadow/capacity、Admin-maker/Operator-checker、双 bank timeline、timeout 原 operation 与 host-filter evidence；
- Managed Targets 的 candidate/diff/duplicate/assignment/actor/mastership/P4Info/freshness/drift/quarantine/retire 与 Admin step-up；Fleet Operations 的 exact target set、waves/gates/failure policy、target×stage vector、partial/reconciling/rollback、timeout 原 operation及大 fleet 虚拟化；
- CSP/CSRF/XSS/deep-link/asset cache/logout/no token/no Service Worker；
- browser、visual、WCAG、keyboard/screen-reader/zoom/reduced-motion；
- production bundle、CWV、heap/DOM/chart/query/SSE soak 和 current/previous rollback；
- lockfile、SBOM、LICENSE/NOTICE、vendored-source manifest、1Panel/sub2api near-copy/brand asset 检查。

## 参考

- Frontend 成熟方案与源码复用评估：`../research/frontend-ops-console-and-source-reuse-assessment-2026-08-10.md`
- 规则观测与全系统复用评估：`../research/rule-effectiveness-and-system-reuse-assessment-2026-08-10.md`
- 1Panel frontend/package/license：<https://github.com/1Panel-dev/1Panel/blob/dev-v2/frontend/package.json>、<https://github.com/1Panel-dev/1Panel/blob/dev-v2/LICENSE>
- sub2api frontend/package/license：<https://github.com/Wei-Shaw/sub2api/blob/main/frontend/package.json>、<https://github.com/Wei-Shaw/sub2api/blob/main/LICENSE>
- Vue performance：<https://vuejs.org/guide/best-practices/performance>
- Vite production build：<https://vite.dev/guide/build.html>
- WCAG 2.2：<https://www.w3.org/TR/WCAG22/>
- Core Web Vitals thresholds：<https://web.dev/articles/defining-core-web-vitals-thresholds>
- ECharts ARIA：<https://echarts.apache.org/handbook/en/best-practices/aria/>
- ECharts dataset：<https://echarts.apache.org/handbook/en/concepts/dataset/>
- Backstage extension blueprints：<https://backstage.io/docs/frontend-system/architecture/extension-blueprints/>
- Vega CSP interpreter：<https://vega.github.io/vega/usage/interpreter/>
- OWASP XSS/CSV Injection：<https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html>、<https://owasp.org/www-community/attacks/CSV_Injection>
- 插件统计与声明式 Web 投影：`0018-plugin-statistics-and-declarative-web-projection.md`
- 插件统计专项调研：`../research/plugin-statistics-and-declarative-web-assessment-2026-08-12.md`
- OWASP CSRF/HTML5 storage：<https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html>、<https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html>
- MDN Content Security Policy：<https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CSP>
- v1.13历史Central GPU pool决策：`0016-central-gpu-inference-pool-and-routing-boundary.md`
- 当前CPU/CUDA启动选择与真实服务E2E：`0017-central-inference-runtime-selection-and-real-e2e.md`
- 历史本机 startup/rollout 决策：`0011-startup-bound-model-selection-and-rolling-restart.md`
- BMv2 无状态防火墙决策：`0014-bmv2-stateless-firewall-policy-and-activation.md`
- 多 target/fleet 与设备管理决策：`0015-multi-target-p4-fleet-and-device-management-boundary.md`
