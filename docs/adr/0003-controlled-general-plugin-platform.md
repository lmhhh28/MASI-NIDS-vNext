# ADR-0003：受控通用插件平台与多运行时边界

- 状态：Accepted
- 日期：2026-08-10
- 决策者：Owner
- 需求基线：原始决策 `vNext-requirements-1.6`；当前适用基线 `vNext-requirements-1.17`
- 关联需求：`CORE-PLUGIN-001`、`ARCH-003`、`ARCH-PLUGIN-001`、`MOD-CTRL-001`、`MOD-PLUGIN-001`、`CONTRACT-PLUGIN-001`、`CONTRACT-PLUGIN-002`、`PLUGIN-PLAT-001` 至 `PLUGIN-PLAT-006`、`DB-PLUGIN-001`、`PERF-PLUGIN-001`、`REL-PLUGIN-001`、`SEC-PLUGIN-001`、`SEC-PLUGIN-002`、`DEP-PLUGIN-001`、`TEST-PLUGIN-001`、`MIG-PLUGIN-001`

## 背景

MASI-NIDS vNext 需要保留旧 Analysis Graph 的可观察能力，也需要让后续只读工具和纯计算扩展不必修改、重编译或重启核心模块。仅把 LangGraph、LLM、MCP 和 A2A 写成一个固定 Python 插件，能够完成当前 Analysis 功能，但不能形成可验证的通用扩展能力；反过来，开放任意 Hook、进程内动态库或公共市场，会直接破坏单一事实源、唯一 P4 writer、单一 effect queue、确定性热路径和最小权限边界。

本次独立评估于 2026-08-10 点验官方资料，并采用“提取已验证模式，不整体照搬”的原则：

- HashiCorp `go-plugin` 证明了子进程隔离、握手和 RPC 插件的可行性，但其安全模型明确偏向本机受控进程，不适合作为跨主机零信任协议或完整 catalog/资格平台；
- Dapr Pluggable Components 证明了独立进程通过 gRPC/UDS 提供组件的模式，但引入完整 Dapr runtime 会扩大首期控制面、运维面和故障面；
- OpenTelemetry Collector Builder 的 manifest、明确组件版本和可复现构建值得采用，但其构建期静态注册不能单独满足运行期 side-by-side、shadow、激活、撤销和回滚；
- Wasmtime Component Model、WASI 和 WIT 适合能力默认拒绝的纯转换插件。WASI 0.3 已于 2026-06-11 稳定发布并由 Wasmtime 43+ 支持，但“上游稳定”不等于“项目已资格化”；首期有意固定 WASI 0.2（Preview 2）同步纯计算 profile，以降低工具链和 Canonical ABI 变化面，不强行用 Wasm 承载 Python Agent、异步网络服务或全部业务协议（依据：[WASI roadmap](https://wasi.dev/roadmap) 与 [releases](https://wasi.dev/releases)）；
- MCP 是 Agent 到工具/资源的协议，A2A 是独立 Agent 间 Task/Message/Artifact 协议；二者均不是通用插件生命周期、主机 ABI、资格或激活协议；
- OCI digest/referrer、Sigstore/Cosign、SBOM 和 provenance 已提供成熟供应链积木，没有理由恢复 legacy 自研多域签名和发布审批链。

因此，“通用”必须被限定为统一的控制、资格和运维模型，而不是任意代码获得任意扩展点。

## 决策

### 1. 平台定义

首期交付受控、私有、类型化的通用插件平台。平台统一：

- immutable manifest/revision 和 artifact identity；
- catalog、publisher/trust、qualification、capability 和 resource profile；
- staged/shadow/active/draining/disabled/revoked 生命周期；
- generation fence、观测、审计、升级、回滚和兼容门禁；
- Fake Manager/Host、SDK/adapter 和 conformance suite。

平台不统一所有业务 wire protocol，不提供任意核心函数 Hook、进程内第三方代码加载、公共 marketplace、未经审核上传、公网自动发现或通用副作用 connector。

### 2. 一个控制面，多种类型化运行面

```text
                         PostgreSQL plugin_platform facts
                                      ↑
                                      │ short transaction/CAS
                                      │
Platform Admin / approved release-policy API → Go Plugin Manager
                                      │ immutable binding/generation
                    ┌─────────────────┴──────────────────┐
                    │                                    │
          Rust Plugin Runtime Host            independent OCI Agent/service
             ├─ Wasm Component                     ├─ A2A analysis-agent
             │  qualified WASI 0.2 + WIT           └─ restricted MCP/read-only gRPC tool
             └─ Host-managed service gRPC/UDS/mTLS
```

- Go Plugin Manager 位于 Control 模块化单体内部，是 catalog、qualification、activation、binding 和 revocation 控制事实的唯一所有者；它不执行第三方代码。
- Rust Plugin Runtime Host 是独立故障域，执行 Wasm component，并对显式 Host-managed service plugin 强制 identity、deadline、资源和 generation；同机优先 UDS，跨机只有 exact profile 的 mTLS gRPC。它没有核心数据库或 P4 凭据。
- Kubernetes、systemd 或 Podman 等部署层负责创建 OCI service 容器。Manager/Host 不持有 Docker socket，也不自行实现通用调度器。
- 独立 Agent/service 保持独立容器和自己的业务协议。Manager 控制其准入、资格、binding、generation 和撤销；Go/peer 经对应 A2A/MCP/gRPC typed adapter 直连，业务流量不绕经 Host。官方 Python Analysis Plugin 属于这条独立运行路径；Host 与 Analysis 在 System E2E 中仍分别真实启动和资格化。

### 3. 首期封闭 Kind

| Kind | 运行面 | 输出 | 核心限制 |
|---|---|---|---|
| `analysis-agent` | 独立 OCI、A2A 1.0；内部可用 LangGraph/LLM，只读 MCP | `AnalysisArtifact`、Recommendation、内容 | 输出不可执行，不得 proposal/approve/effect/P4 |
| `read-only-tool` | 独立 OCI、MCP 2025-11-25 或受测 gRPC adapter | 有界且带 schema/evidence identity 的只读结果 | 无 mutation、无 ambient DB/network/secret |
| `pure-transform` | `wasm-component/v1`：固定受支持 Wasmtime、首期资格 profile WASI 0.2、`masi:plugin-transform@1.0.0` WIT | 确定性、有界、可哈希转换结果 | 无 filesystem/network/clock/random/process capability；WASI 0.3 未经矩阵不得自动准入 |

`service-grpc/v1`、`wasm-component/v1` 和 `a2a-agent/v1` 是不同 runtime/contract profile，不是可互换的万能 ABI。新增 kind、外部副作用、authorizer、policy writer、database writer、effect executor、P4 writer 或 UI JavaScript plugin 必须提升需求基线并另行证明幂等、安全与恢复语义。

统计计算不是第四种 kind，而是现有 kind 可声明的版本化输出 capability `plugin-statistics/v1`。每个统计项先以 immutable `PluginStatisticsDefinitionV1` 随 plugin revision 签入 manifest，且只能引用 host-owned input projection/field registry；`read-only-tool` 可额外引用 manifest/binding 已批准的 external-source capability ID，但 definition 禁止 endpoint/credential、运行时注册、SQL/PromQL/JSONPath/MCP prompt/URL/表达式。确定性统计默认由 `pure-transform` 产生；需要访问已批准外部只读来源时才使用 `read-only-tool`；`analysis-agent` 可以引用已经由 Go 校验并持久化的统计 Artifact，但叙事性 `AnalysisArtifact` 不得冒充 canonical 统计。插件若要把结果显示在 Web，只能返回 `PluginStatisticsArtifactV1` 与封闭的 `plugin-statistics-display/v1` hints，不能携带浏览器代码或任意图表 DSL。详细边界见 ADR-0018。

### 4. 所有权和不可执行边界

- PostgreSQL 仍是核心持久事实唯一来源；Plugin Manager 只写 Go 所有的 `plugin_platform` 控制事实。
- Runtime Host 和通用插件不获得数据库凭据；官方 Analysis Plugin 仅写自己的隔离 schema，并通过 Go API/MCP 读取核心事实。
- 插件输出一律视为不可信 candidate/result/Artifact，不能成为 Event、authorization、policy eligibility、effect intent、P4 entry 或 runtime truth。
- Go 冻结有界 `StatisticsInputBundleV1`，拥有统计 schedule/run、幂等键、结果校验和 PostgreSQL `plugin_statistics` 投影；`plugin_statistic_runs` 是唯一 durable run ledger，Go 内存 dispatch queue 只能从中有界派生并可重建。Runtime Host/typed adapter 只执行 exact binding，插件不得轮询核心数据库、Prometheus 或 P4 自行调度，也不得直写数据库、推送浏览器或建立插件自有/第二 durable 任务队列。
- Plugin Manager、Runtime Host、插件和 Artifact 均不能创建 Proposal/Decision/Intent、调用 Edge/P4 或成为 dispatcher 可 claim 的队列。
- capability 默认拒绝，并绑定 kind、动词、scope、数据等级、endpoint、预算和 generation；禁止 `admin`/`all`、ambient credential、共享可写 volume 和未解析外部 URL。
- 平台、Host 或任一插件不可用时，检测、Event/Incident、确定性 policy/effect、P4 readback 和非插件页面继续。
- Central Inference的model/scaler/feature-schema/label-taxonomy/output-adapter/runtime/pool revision不是plugin revision，也不是首期plugin kind。模型生命周期由Go Model Manager、`model_platform`、ADR-0010保留的模块化语义和ADR-0017管理；Plugin Manager/Host不加载、激活、路由或回滚在线模型。CPU/CUDA启动profile也不属于插件。两套控制面可以复用OCI/SBOM/provenance/evidence/CAS工具，但不得共享catalog table、active pointer、generation或comparison output。

### 5. 准入、激活和发布授权

生产准入按以下顺序执行：

```text
digest-pinned artifact
→ manifest/schema/platform/size validation
→ publisher + artifact digest verification
→ SBOM/provenance/trust/revocation policy
→ contract/capability/resource/isolation qualification
→ staged
→ shadow
→ exact-binding activation
```

- 制品身份只使用 OCI/Wasm digest；tag 只能解析，不能持久化为 active identity。
- 使用 Cosign/Sigstore bundle 或组织 PKI 验证允许的 issuer/publisher 和实际 artifact digest，同时校验 SBOM、provenance 和撤销事实；保存信任根、bundle、policy 与验证器版本，以便离线重验。
- 签名说明“谁发布了哪些 bytes”，不说明功能正确、安全或已获准运行。qualification 和 activation 是独立事实。
- 首期 kind 均无核心副作用，因此日常 exact-binding activation/rollback/revoke 不建立第二套 maker-checker 或通用审批 Workflow。自动资格门禁全部通过后，由具有精确 plugin/scope 权限的 Platform Admin 执行一次绑定 exact revision、evidence digest、trust-policy digest、scope 和 generation 的显式激活；Owner 批准的版本化自动发布 policy 可以等价执行。
- trust root、issuer/publisher/repository/builder allowlist、revocation grace 和自动发布 policy 属于高杠杆 expectations；其变更必须通过受保护 Git/append-only PolicyChange，由两个不同稳定人类身份复核并绑定旧新 digest。该保护不恢复 legacy 多域签名链，也不延迟单人紧急 revoke（依据：[SLSA v1.2 verification guidance](https://slsa.dev/spec/v1.2/verifying-artifacts)）。
- 开发环境可以使用独立 profile 的 unsigned fixture，但必须持续标记 `NOT QUALIFIED`，不能产生生产 activation。

### 6. 生命周期、升级和回滚

- revision 不可变；artifact、manifest、config、contract 或 capability 任一改变都创建新 revision/digest。
- 生命周期为 `registered → verified → staged → shadow → active → draining → disabled/revoked/failed`；转换 append-only 审计。
- 同一 `(plugin_id, kind, scope)` 至多一个 active generation，active pointer 由 PostgreSQL 唯一约束、短事务和 CAS 原子切换。
- shadow 只读取复制的有界输入，输出进入隔离比较结果；不得写 canonical fact 或触发外部 mutation。
- 升级 side-by-side 运行。旧 generation 在 draining 后拒绝新任务，late result 被 fence；禁止原地覆盖 image、Wasm bytes、Python package、config 或 contract。
- 回滚只能选择仍通过当前 trust、host、kind、config、WIT 和持久化事实兼容矩阵且未撤销的 revision；否则保持 `HOLD/unavailable`。
- revoke 阻止 install/activate/restart/rollback 和新任务；Host-managed binding 由 Host 最长每 30 秒 reconcile，独立 Agent/service 则由 Manager-controlled typed adapter 在每次直连派发前检查同一 canonical binding/revocation；可达控制面传播最长 60 秒，撤销/trust cache 超过 5 分钟未刷新时 binding 停止新任务并 `HOLD`。无法安全 drain 时在 60 秒或更短 task deadline 内终止；历史 Artifact、qualification 和审计只读保留。
- 统计 run identity 至少绑定 plugin/revision/config、binding generation、definition/scope、input/window 与 trigger/schedule revision。同 key、同 digest 只幂等返回；同 key、异 digest 为冲突；旧 generation 的晚到结果只保留审计，不更新 `plugin_statistic_current`。统计任务只使用 Go Control 内从 durable run ledger 派生的有界非 effect dispatch queue，不能占用 effect dispatcher，也不形成第二 durable queue。on-demand 同时验证源数据 read 与 `plugin.statistics.run`；append-only schedule mutation 验证 scoped Admin/data-class/CSRF/适用 step-up 门禁。每次 scheduled run 重新验证 binding/revocation/policy/scope。

### 7. 性能和稳定性

- 插件不得进入 packet、telemetry、inference、Event ingest 或 effect execution 同步链；核心调用插件必须是旁路、有界并可取消。
- Service RPC 必须引用 `grpc-service/v1` method profile，设置 total deadline、请求/响应大小、并发、queue、retryable code、attempt/backoff/throttling。只有明确幂等、复用同一 identity/digest 的调用可以有界重试；默认不 hedging，非幂等/健康/effect RPC 禁用应用层和 channel transparent retry（依据：[gRPC retry](https://grpc.io/docs/guides/retry/) 与 [deadline](https://grpc.io/docs/guides/deadlines/)）。
- Wasm 必须限制 memory、table、instance、fuel 主预算、epoch/wall deadline、输出和编译缓存；纯转换不得预开网络、目录、时钟或随机能力。fuel/epoch/deadline/cancellation 使用稳定不同错误，禁止不可取消 blocking import（依据：[Wasmtime interruption](https://docs.wasmtime.dev/examples-interrupting-wasm.html)）。
- 每个插件/Host 都有 CPU、RSS、PID、FD、disk、并发、restart、circuit/quarantine 上限；健康检查不触发 mutation。
- 同机 UDS 使用每 binding/generation 独立绝对路径，验证 owner/mode、no-symlink、`SO_PEERCRED`/workload identity、framing 和 deadline；startup/readiness/liveness 使用独立语义与有界 restart budget，具体初始值由 `DEC-020` 固定。
- 必须基于“平台完全禁用”的核心基线对 1/2/4/8/32 并发、cold/warm、饱和、crash/restart 和 soak 测量；插件平台负载造成的核心 p99 回退不得超过 5%，核心 RSS 增幅不得超过 10%。
- Manager/Host/插件故障不得占满 Go/PostgreSQL/Edge 连接池，不得产生重试风暴；单插件故障只熔断或隔离该 binding。
- `plugin-statistics/v1` 初始限制为每 plugin revision 32 definitions/128 KiB definition block/每 definition 64 field refs/1 external-source capability ref、input 2 MiB、artifact 1 MiB、每 Artifact 32 metrics、64 series、总计 10,000 points、单 view 2,000 points、每 histogram point 64 buckets、总计 2,000 table rows、200 evidence refs、64 KiB total text/4 KiB scalar、depth 8、per-binding in-flight 2、queue 32、deadline 10 秒、history retention 30 天；精确值由 profile 固定。最大统计负载仍须满足相对于全插件关闭基线的核心 p99 回退不超过 5%、核心 RSS 增幅不超过 10%。

### 8. 兼容性

版本兼容分别判定，不以“进程可启动”代替：

1. `plugin/manifest/v1`；
2. Manager↔Host API，以及 Manager-controlled typed adapter↔独立 Agent/service 的直接协议边界；
3. plugin kind input/output contract；
4. runtime profile；
5. config schema；
6. WIT world；
7. 可选 output capability、`plugin-statistics/v1` 与 `plugin-statistics-display/v1`；
8. 已持久化 plugin revision/qualification/binding/run/artifact/current facts。

未知 major/kind/profile 必须拒绝；minor 只有经 old/new contract 和 golden 测试证明后才兼容。滚动升级必须覆盖上一受支持 Manager、Host、插件和持久化事实的组合，且 late result 始终按 generation fence 拒绝。

官方 `masi.analysis.langgraph` 必须作为真实 `analysis-agent` 通过同一 Manager 控制面，而不是绕过 catalog/qualification/binding 的特殊部署；“同一平台”不表示其 A2A/MCP 业务流量经过 Host。它另外按 ADR-0002 保留旧 Analysis Graph 的外部行为兼容；平台兼容不等于 Analysis 行为兼容，二者必须分别验收。

## 取舍

收益：

- 后续增加只读工具或纯转换时，不需要修改核心所有权和处置链；
- 统一准入、版本、撤销和可观测性，避免每个插件自建一套 loader/registry；
- Wasm 与进程隔离按工作负载匹配，兼顾启动成本、语言自由和故障边界；
- 官方 Analysis Plugin 为平台提供真实业务验证，conformance fixture 仍保持无业务权限；
- 不在热路径加入动态扩展点，核心性能和恢复语义可以独立资格化。

代价：

- 新增 Go Manager、Rust Host、contracts、数据库控制事实、供应链和多版本测试矩阵；
- 同时维护 A2A、MCP、gRPC 和 WIT，不存在单一协议带来的表面简化；
- side-by-side、shadow、离线验证和撤销需要额外制品存储、运维和审计；
- WASI 0.2/0.3 与各语言 Component Model 工具链成熟度、Canonical ABI 和异步模型不同；首期必须限制在已资格化的 0.2 纯转换 profile，并为后续 0.3 或 service profile 准备显式兼容路径。

## 被拒绝的方案

1. **只有固定 Analysis Plugin，不建设平台**：可以完成当前 Agent 功能，但目录、资格、激活、撤销和后续扩展会继续分叉。
2. **在 Go/Rust/C++ 进程内加载共享库、Python package 或任意 Hook**：崩溃、ABI、供应链、资源和权限故障会污染核心故障域。
3. **把 MCP 或 A2A 当作万能插件总线**：两者解决的业务语义不同，也不提供宿主资源、生命周期、Wasm ABI 或供应链资格。
4. **所有插件都强制 Wasm**：会给 Python/LLM/异步网络 Agent 带来不必要适配，并受当前 WASI 生态限制。
5. **直接采用完整 Dapr/Kubernetes Operator 作为核心平台**：首期能力超出需要，引入第二控制面和更大运维面；只采用其进程外 gRPC/UDS 模式。
6. **使用 HashiCorp go-plugin 作为跨机协议**：其本机子进程模型有参考价值，但不替代 mTLS 网络协议、catalog、资格和撤销。
7. **仅使用构建期静态 registry**：可复现但不能满足运行期 shadow、原子激活、drain、撤销和 side-by-side。
8. **恢复 legacy 多域签名、独立 signer/key registry 和 signed release report**：与 OCI/Sigstore 标准证据重复，并形成多余发布授权链。
9. **允许 authorizer/effect/P4/database writer 插件**：会创建第二事实源、第二队列或第二 writer，首期明确禁止。
10. **把在线检测模型包装为 `pure-transform`/service plugin**：会把动态插件生命周期带入 inference 热路径，并混淆 model qualification/binding 与 plugin capability；模型按 ADR-0010 的固定 C++ engine + data bundle 边界替换。
11. **为统计结果新增任意 dashboard/UI 插件或让插件返回 ECharts/Vega/HTML**：会把不可信执行/表达式面带入浏览器并使显示合同不可控；采用 Go 校验后的封闭数据与展示 hints。
12. **让统计插件直连 PostgreSQL/Prometheus/P4 或自建 scheduler**：会产生第二事实读取口径、第二队列和不可审计负载；所有输入由 Go 冻结并显式派发。

## 迁移

平台按 clean-start 实现，不导入 legacy Workflow/Task/Review/checkpoint、旧 plugin registry 或历史签名控制表：

1. 冻结 manifest、Host API、kind contract、`contracts/profiles/v1`、WIT、错误和资源 profile；
2. 创建 `plugin_platform` schema、Fake Manager/Host/capability provider 和 conformance fixtures；
3. 独立完成 Go Manager 与 Rust Host 的黑盒、故障、安全、兼容和性能门禁；
4. 以 deny-all 启动平台，资格化无业务权限的 service/Wasm fixtures；
5. 以无业务副作用的 deterministic statistics conformance plugin 冻结 input/artifact/display golden，并证明 Go-owned run/projection 与内建 Web renderer；
6. 将 `masi.analysis.langgraph` 注册为 immutable revision，完成 legacy capability matrix、shadow 和显式激活；
7. Module Complete 前可以在隔离 test identity/target 上做明确标记 `REHEARSAL/NOT QUALIFIED` 的真实 wire/boundary rehearsal；全模块达到 Module Complete 后，必须从干净环境按要求顺序重跑 Manager↔Host、Host↔fixture、statistics↔Go/PostgreSQL/Web、Analysis↔MCP/A2A 和 Frontend 正式集成。

## 回滚

平台异常时先禁止新的 activation 和任务，保持核心检测、事实链、effect 和 P4 readback 运行；对单插件执行 bounded drain/disable/quarantine。若 Manager/Host 版本可回滚，只能回到通过当前 schema、manifest、Host API、kind/WIT 和持久化事实兼容矩阵的版本。

插件回滚通过 CAS 切换到仍合格、未撤销的旧 active revision，不覆盖制品和历史事实。不存在兼容 revision 时保持对应插件 `HOLD/unavailable`，不得动态加载旧代码、放宽 capability、恢复 legacy Workflow 或绕过供应链验证。

## 验证

除 `TEST-PLUGIN-001` 全部门禁外，验收至少提供以下可复核证据：

- unknown kind/major/profile、malformed/oversize manifest、digest/publisher/provenance mismatch、revoked artifact 和 capability 扩张稳定拒绝；
- Manager/Host restart、PostgreSQL failover、并发 activate/revoke/rollback 和 old-generation late result 收敛到唯一 binding；
- service/Wasm 的 CPU、memory、PID、FD、disk、fuel、deadline、sandbox、egress、preopen、secret、crash/OOM/hang/restart-storm 故障门禁；
- production offline bundle 重验、trust-policy rotation 和 revocation；unsigned fixture 始终无法激活到生产；
- 真实 `masi.analysis.langgraph` 和无业务权限的 service/Wasm fixture 分别证明三种 runtime profile；
- 数据库权限、credential absence、协议拒绝和审计共同证明零核心 DB/P4/effect mutation；
- deterministic statistics conformance plugin 证明 definition/input/run/artifact/display 跨 Go/Rust/Python/TypeScript/Wasm golden、host projection/field allowlist、幂等/late-definition/late-generation fence、oversize/cardinality/NaN/Inf/unknown-display/SQL-expression rejection，以及 Web 不执行插件代码；
- 全插件关闭基线、并发/饱和/soak 和核心 p99/RSS/连接隔离预算。

当前仓库仍处于初始化阶段。ADR accepted 只表示方案已冻结，不表示 Manager、Host、官方插件或系统已实现、通过 E2E 或具备生产资格；在形成上述证据前状态为 `HOLD/NOT RUN`。

## 参考

- HashiCorp go-plugin：<https://github.com/hashicorp/go-plugin/blob/main/README.md>
- Dapr Pluggable Components：<https://docs.dapr.io/developing-applications/develop-components/pluggable-components/pluggable-components-overview/>
- OpenTelemetry Collector Builder：<https://github.com/open-telemetry/opentelemetry-collector/blob/main/cmd/builder/README.md>
- Wasmtime release policy：<https://docs.wasmtime.dev/stability-release.html>
- WASI releases/roadmap：<https://wasi.dev/releases>、<https://wasi.dev/roadmap>
- WebAssembly Interface Types：<https://component-model.bytecodealliance.org/design/wit.html>
- gRPC deadlines：<https://grpc.io/docs/guides/deadlines/>
- gRPC retry：<https://grpc.io/docs/guides/retry/>
- OCI Image/Distribution 1.1 artifact/referrers：<https://opencontainers.org/posts/blog/2024-03-13-image-and-distribution-1-1/>
- Sigstore/Cosign verification：<https://docs.sigstore.dev/cosign/verifying/verify/>
- A2A 1.0.0：<https://a2a-protocol.org/v1.0.0/specification/>
- MCP 2025-11-25 Streamable HTTP：<https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- SLSA v1.2 verifying artifacts：<https://slsa.dev/spec/v1.2/verifying-artifacts>
- 契约与 runtime profile：`0005-contract-runtime-and-protocol-profiles.md`
- 资格等级与 rehearsal：`0006-qualification-levels-and-evidence.md`
- 在线模型生命周期：`0010-online-model-lifecycle-and-rollout.md`
- 插件统计与声明式 Web 投影：`0018-plugin-statistics-and-declarative-web-projection.md`
- 插件统计专项调研：`../research/plugin-statistics-and-declarative-web-assessment-2026-08-12.md`
- OpenTelemetry Metrics data model：<https://opentelemetry.io/docs/specs/otel/metrics/data-model/>
- Grafana data frames：<https://grafana.com/developers/dataplane/dataframes>
