# ADR-0005：契约、Runtime 与 Agent 协议 Profile

- 状态：Accepted
- 日期：2026-08-10
- 决策者：Owner
- 需求基线：`vNext-requirements-1.17`
- 关联需求：`CONTRACT-001` 至 `CONTRACT-004`、`CONTRACT-P4-FW-001`、`CONTRACT-RULE-001`、`CONTRACT-TARGET-001`、`CONTRACT-FLEET-EFFECT-001`、`CONTRACT-AGENT-001`、`CONTRACT-PLUGIN-001`、`CONTRACT-PLUGIN-002`、`CONTRACT-PROFILE-001`、`CONTRACT-MODEL-001`、`CONTRACT-SUPPLY-001`、`CONTRACT-TRAFFIC-001`、`CONTRACT-TELEMETRY-001`、`CONTRACT-INFERENCE-001`、`PLUGIN-PLAT-002`、`FUNC-FW-001`、`FUNC-INF-MODEL-001`、`FUNC-TRAFFIC-001`、`FUNC-TARGET-FLEET-001`、`PERF-P4-FW-001`、`PERF-INF-001`、`PERF-TEL-INF-001`、`PERF-PLUGIN-001`、`PERF-RULE-001`、`PERF-TRAFFIC-001`、`PERF-TARGET-FLEET-001`、`WEB-FW-001`、`WEB-STATE-001`、`WEB-PERF-001`、`WEB-SUPPLY-001`、`WEB-TARGET-FLEET-001`、`REL-003`、`REL-P4-FW-001`、`REL-INF-001`、`REL-INF-POOL-001`、`REL-TEL-INF-001`、`REL-TARGET-FLEET-001`、`SEC-PLUGIN-001`、`SEC-TARGET-FLEET-001`、`TEST-002`、`TEST-REAL-E2E-001`、`TEST-P4-FW-001`、`TEST-INF-001`、`TEST-TEL-INF-001`、`TEST-PLUGIN-001`、`TEST-WEB-001`、`TEST-RULE-001`、`TEST-TRAFFIC-001`、`TEST-REUSE-001`、`TEST-TARGET-FLEET-001`、`DEC-027`、`DEC-028`、`DEC-029`、`DEC-030`、`DEC-031`、`DEC-032`、`DEC-033`、`DEC-034`、`DEC-035`

## 背景

“上游规范稳定”“SDK 可以连接”“schema lint 成功”和“MASI-NIDS 已验证兼容”是四个不同结论。WASI 0.3 已稳定，但首期纯转换并不需要异步能力；MCP 提供通用向后兼容建议，但 MASI-NIDS 私有工具 endpoint 有意不接受旧 payload；gRPC 即使没有显式 retry policy 也可能透明重试。若这些差异只写在实现配置中，滚动升级和故障恢复将没有统一可审计依据。

## 决策

### 1. Profile Registry

建立 `contracts/profiles/v1`，作为项目资格 profile 的唯一机器可读 registry。每个 profile 至少包含：

- 稳定 profile ID/major/minor；
- 上游规范及精确版本；
- source schema/package/world 与 digest；
- generator、validator、SDK/runtime release policy 与 artifact digest；
- capability、method、import/export、错误、大小、并发、deadline/retry；
- old/new compatibility matrix、稳定external `source_id`与归档snapshot digest、停止支持日期和 qualification evidence digest。

规范更新只创建新 profile；不得原地改变已激活 major 的语义。未知 major、未验证 minor、profile/source/generated digest 漂移都 fail closed。

### 2. 首期 Contract Profile

| Profile | 首期值 | 使用边界 |
|---|---|---|
| `openapi-rest/v1` | OpenAPI 3.1.2 | Go Control ↔ Web/外部 REST client |
| `json-schema/v1` | JSON Schema 2020-12 | manifest、config、Artifact、evidence envelope；MCP 未显式 `$schema` 时同样使用 |
| `grpc-service/v1` | Protobuf + gRPC method config | Edge↔Control、Manager↔Host、Host↔Host-managed service plugin；独立 service/Agent 使用其 direct typed adapter |
| `p4runtime/v1` | P4Runtime 1.4.1 | Edge↔P4 target；详细 fence 见 ADR-0004 |
| `p4-target-fleet/v1` | stable target identity + assignment/actor/application fences + bounded 1/2/N target resources | Go/PG Target Registry/Fleet Coordinator↔Edge TargetSupervisor/Actor↔Web；详见 ADR-0015 |
| `target-gnmi-readonly/v1` | 条件 OpenConfig gNMI exact proto/model/path；仅 `Capabilities/Get/Subscribe` | 真实设备/Stratum 的低优先级只读状态；独立 mTLS identity，首期拒绝 `Set`，未触发时 `NOT_APPLICABLE` |
| `p4-stateless-firewall/v1` | BMv2 `simple_switch_grpc`/v1model IPv4 normalized policy + response overlay + baseline dual-bank/selector | Go/Edge/P4/Web；default/priority/fragment/capacity/operation/readback/unsupported 见 ADR-0014 |
| `p4-rule-observation/v1` | exact direct counter + eligible counter + packet-oracle profile | Edge↔P4 rule observation、Go rollup 与 Web 公式；详细语义见 ADR-0008 |
| `p4-traffic-replay/v1` | generated/synthetic/curated/live-session fixture + exact topology/direction/rewrite/timing/impairment/oracle | 隔离 P4/testkit 流量输入与结构化结果；详细语义见 ADR-0012 |
| `telemetry-p4-window/v1` | P4 aggregate + target-qualified bank/epoch/snapshot + event-time window/quality | 首期P4→Edge canonical source；详细语义见ADR-0013 |
| `telemetry-p4-digest-sample/v1` / `telemetry-p4-packetin-sample/v1` | best-effort bounded hint/sample | 只能补充coverage/evidence，不是reliable feed或window完成确认 |
| `telemetry-mirror-packet-mmap/v1` / `telemetry-mirror-af-xdp/v1` / `telemetry-mirror-dpdk/v1` | 条件mirror capture profiles | 按feature/target/capacity逐级触发；不同时常驻，不取得第二source owner |
| `inference-central-grpc-batch/v1` | async bounded batched-unary Protobuf/gRPC + mTLS + logical pool/attempt identity | 首期Edge↔C++ Gateway hot data；channel reuse、no-delay coalescing、deadline/retry/fence/WAL/ACK见ADR-0013/0017 |
| `model-runtime-central-cpu/v1` | C++ Gateway + pinned Triton + ORT CPU + exact CPU/thread/NUMA/RAM/network/batch/numeric profile | Offline ML bundle/repository snapshot↔Central Inference；管理员启动前显式选择，startup-only，见ADR-0017 |
| `model-runtime-central-cuda/v1` | C++ Gateway + pinned Triton + ORT CUDA + exact CUDA/cuDNN/driver/GPU/VRAM/network/batch/numeric profile | Offline ML bundle/repository snapshot↔Central Inference；管理员启动前显式选择，startup-only，见ADR-0017 |
| `availability-single/v1` / `availability-ha/v1` | 恰好一个故障域、允许域内1..N同profile副本且明确非HA / 同 exact runtime profile 跨至少两个故障域且static N+1 | 与CPU/CUDA正交；两者均固定required/min-ready/max-unavailable；不得把域内多副本、异profile备用或基础设施Ready解释为HA |
| `model-rollout-pool-generation/v1` | incarnation + exact runtime/availability pool envelope + Edge single-route per-shard generation rollout | Go/Edge/deployment adapter/Gateway/Triton/PostgreSQL；route/drain/WAL/action/readback/CAS/commit/recovery/rollback见ADR-0017；v1.13历史见ADR-0016 |
| `qualification-evidence/v1` | 正交`level/applicability/result/qualification` + exact claim scope | Module/pairwise/System/Production evidence与CI/API/Web聚合；详见ADR-0006 |
| `deployment-tier/v1` | `development|acceptance|operational-single-domain|production-ha` | 与qualification level正交；前三项最高为`SYSTEM_E2E`，只有最后一项可进入`PRODUCTION`且必须绑定并通过`availability-ha/v1`及其余生产门禁 |
| `e2e-runner-compose/v1` | exact Docker Compose/Engine/API + isolated project/network/volume + health/deadline/resource/evidence/cleanup | 首期本地/CI Module、pairwise与BMv2 System E2E；health不是业务PASS，生产/多主机runner另按deployment profile资格化 |
| `a2a-agent/v1` | A2A 1.0 HTTP+JSON | Go/peer↔Analysis Agent |
| `masi-mcp-readonly/v1` | MCP 2025-11-25 Streamable HTTP restricted | Analysis↔Go/read-only tool |
| `wasm-component/v1` | WASI 0.2 Preview 2 + Component Model | Host↔pure-transform |
| `plugin-statistics/v1` | immutable `PluginStatisticsDefinitionV1` + `StatisticsInputBundleV1` → `PluginStatisticsArtifactV1` JSON Schema + stable run/quality/metric/table semantics | Go↔Host/direct typed plugin adapter↔Go projection；统计是 output capability，不是新 kind，详见 ADR-0018 |
| `plugin-statistics-display/v1` | 封闭 `metric-card|status|timeseries|bar|heatmap|table|text|evidence-list` hints | Go↔Web 内建 renderer；只传数据/语义，不传 ECharts/Vega/HTML/JS |
| `web-spa/v1` | Vue 3/Vite/TypeScript + qualified dependency/generator lock | Web build、generated client、state/SSE/resource policy |
| `web-browser/v1` | 精确浏览器 engine/build target/polyfill matrix | Web compatibility、E2E、visual/a11y |
| `web-performance/v1` | 目标设备/网络/workload + bundle/CWV/resource budget | Web production qualification |
| `supply-tooling/v1` | Buf/protoc、SBOM/scanner/Cosign、dashboard/config 与 vulnerability DB 的 exact tool/data digest | 全系统 build/admission/release；采用边界见 ADR-0009 |

OpenAPI 当前已有 3.2.0，JSON Schema 当前发布版为 2020-12；项目选择 3.1.2 是首期生成器兼容 profile，而不是声称它是上游最新版。[OpenAPI published versions](https://spec.openapis.org/oas/)、[JSON Schema specification](https://json-schema.org/specification)

### 3. Schema 所有权与生成链

- Protobuf、OpenAPI、JSON Schema 和 WIT 分别是对应边界的 source of truth；不得从生成物反向生成另一个 source，形成循环所有权。
- OpenAPI 只定义 HTTP API；Protobuf 只定义内部 binary/gRPC RPC；JSON Schema 定义 JSON document；WIT 只定义 Wasm component imports/exports。
- 跨格式共享概念必须通过显式 adapter 和双向 golden 测试，不复制同名结构独立演化。
- generated artifact 必须记录 source digest、generator image/version、参数和输出 digest。手改 generated 文件或 generator 漂移必须由 CI 拒绝。
- presence/null、unknown fields、数字范围、时间/IP canonicalization、错误 envelope 和 pagination 由各 profile 固定，不能依赖语言默认值。
- Web OpenAPI client 由 `openapi-rest/v1` source 生成并绑定 `web-spa/v1`；generator/template 只是工具，不取得 API source 所有权。输出、runtime adapter、error/cursor/SSE 映射必须有 golden/contract tests，页面不得手写第二套 DTO 或 URL。
- `web-spa/v1` 同时绑定 package manager、lockfile、package/OCI digest、license/NOTICE/SBOM、design-token digest 和 build flags；依赖升级创建新的 profile revision，并触发 browser/bundle/a11y/performance/old-new matrix。Vite 默认 browser target、package range 和 remote CDN 均不得充当隐式 profile。
- `p4-rule-observation/v1` 同时绑定 P4 program/P4Info/direct-counter/eligible-counter/packet-oracle、target counter width/mode/byte semantics、sweep/batch/deadline/freshness 和 formula/status golden；counter capability 变化创建新 application generation/observation epoch，不能原地解释旧 sample。
- `p4-stateless-firewall/v1` 同时绑定 normalized policy canonicalization、target/P4Info/compiler、response/bank/selector table/action/counter identity、priority/default/fragment、normalized→compiled capacity、operation/readback/reconcile 和 Admin-maker/Operator-checker API。target architecture、match/action/default 或 bank/selector 语义改变必须新 major/profile，不能由 raw P4 entity 兼容绕过。
- `p4-target-fleet/v1` 同时绑定 stable/never-reused `target_id`、endpoint/P4 `device_id`映射、target-control incarnation、assignment generation、actor epoch、P4 election/application generation/P4Info、lifecycle/capability、parent/child/wave/gate/status/digest，以及 per-target/global connection/RPC/FD/task/journal/queue/DB/API/UI 上限。跨 target 原子性不是该 profile capability。
- `target-gnmi-readonly/v1` 同时绑定 gNMI source proto/spec、OpenConfig model/revision/path allowlist、target implementation、encoding/subscription mode、mTLS identity、framing/bytes/rate/queue/deadline/freshness/gap 与 P4 workload 优先级；`Set`、unknown path/model、write credential 和 silent fallback 必须拒绝。
- `p4-traffic-replay/v1` 绑定 fixture/provenance/ground-truth、PCAP format/DLT/timestamp/caplen、topology/direction/port、immutable rewrite、唯一traffic mode与runner/backend、netem/qdisc/offload/MTU/kernel、resource limits、sender/DUT/counter/outcome/detection evidence与 software/hardware target class。每个scenario精确绑定`mode+fixture+runner/backend+topology+direction+rewrite`；未知/缺失/失败时HOLD或NOT_RUN，禁止best-available、自动切换或工具默认漂移；二层 replay不得解释为 live session。
- `telemetry-p4-window/v1`绑定target/P4Info counter/register/digest identity、observation domain/point、bank/epoch/snapshot/clear、source runtime epoch/sequence、event/export/ingest/finalized time、`[start,end)`、watermark/lateness、flow direction/fragment、sampling/coverage、quality/drop、feature adapter和资源上限。Digest/PacketIn profile必须明确best-effort；mirror profile必须冻结interface/NIC/driver/kernel/offload/RSS/NUMA/ring/UMEM和actual mode。backend fallback或source语义改变创建新profile/epoch；
- `inference-central-grpc-batch/v1`绑定Protobuf canonical source、mTLS identity、logical pool/binding generation、shard/route/source/window/request/input digest、worker attempt/result digest、tensor dtype/shape/order/bytes、batch records/bytes、Edge no-delay coalescing、channel/resolver reuse、message/in-flight/quota、deadline/cancellation/retry/dedupe/fence、stable error、input/result WAL和PostgreSQL canonical ACK。禁止逐record RPC、全fleet单一长stream、无限in-flight、未声明transport fallback和same-generation异digest；wire/feature/output major变化通过新pool generation drain后单路切换。
- `model-runtime-central-cpu/v1` 与 `model-runtime-central-cuda/v1` 共享 Gateway/Triton/ONNX、ONNX IR/opset/operator/custom-op policy、feature/label/output-adapter/wire、bundle/metadata/闭包化repository snapshot、numeric tolerance、raw/optimized artifact/tool/options、Triton `model-control-mode=none`/strict readiness/auto-complete disabled、显式dynamic batch/instance-group/queue、network和只读`GetLoadedModel/GetPoolStatus`。NONE snapshot只能包含exact binding及声明依赖，额外model/version/config/backend拒绝；禁用auto-complete后仍必须显式固定instance group。CPU profile另绑定CPU feature/微码/core/thread/affinity/NUMA/execution mode/arena/RAM；CUDA profile另绑定ORT CUDA/CUDA/cuDNN/driver/GPU/VRAM/stream/I/O Binding/host-device copy及资格化时已声明、读回和测量的host-side operator partition。二者分别保存selected/observed envelope和证据；运行期新provider partition/CPU接管必须失败关闭，不能静默选择或互相fallback。
- `availability-single/v1` 明确恰好一个故障域，可按容量配置1..N个域内同profile副本，并固定required/min-ready/max-unavailable与中断语义；域内多副本不得声称HA。`availability-ha/v1` 绑定同一exact runtime profile、至少两个故障域、required/min-ready/max-unavailable/static N+1、single-replica/domain-loss与rolling capacity证据。availability profile不允许用CPU作CUDA灾备或反之。
- `model-rollout-pool-generation/v1` 绑定不可复用model-control incarnation、immutable pool startup envelope、selected runtime/availability profile、logical pool/worker/failure-domain/compute identity、expected/proposed pool+binding generation、Edge route-withdraw/WAL bounded buffer/gap/resume、deployment action/termination/restart budget、startup/warmup/readback、per-shard CAS、committed-binding handshake、mixed rollout、drain/recovery和rolling rollback。CPU↔CUDA必须创建新generation并重新资格化；首期显式拒绝production load/unload/repository poll、candidate/online shadow、weighted split、第二route owner、Edge-local inference和任何runtime自动fallback。
- `supply-tooling/v1` 与 `contracts/supply-chain/v1` 分工：前者固定执行工具、配置、漏洞数据库和结果 schema；后者保存 component/inventory/采用决策。scanner 或 generator 不能通过输出反向取得 source ownership。
- `qualification-evidence/v1`把level、applicability、result、qualification与claim scope分开；`NOT_APPLICABLE`不是result，`HOLD/NOT_RUN`不是level，展示摘要不能回写wire。`deployment-tier/v1`与qualification level正交；`operational-single-domain`不得生成`level=PRODUCTION`，CPU/CUDA、single/HA、topology/digest分别聚合。
- `e2e-runner-compose/v1`只拥有测试服务生命周期和证据采集，不拥有业务current或oracle。Compose running/healthy与Playwright URL可达不能提升qualification；场景失败不得自动换Podman/Kubernetes/systemd runner。
- `plugin-statistics/v1` 固定 immutable definition identity/digest、host-owned input projection/field 与 approved external-source capability allowlist、input snapshot/source revision、scope/window、plugin/revision/config/binding generation、run key、metric `gauge|sum|histogram`、temporality/monotonicity、有限数值、dimension/series/table/point quality、artifact digest、分页/retention 与 stable error；definition endpoint/credential、运行时 registration、SQL/PromQL/JSONPath/MCP prompt/URL/表达式、未知 major/projection/field/capability/metric/display kind、同 key 异 digest、NaN/Inf、越界维度或旧 definition/generation 结果必须拒绝。`plugin-statistics-display/v1` 只允许封闭 display hint 和 server-normalized dataset/encode 映射，不包含 URL、MIME、HTML/SVG/CSS/JS、formatter、event、expression、custom series 或任意 vendor option。
- `contracts/supply-chain/v1`中的每个采用事实还必须用稳定`source_id`绑定title、access date、exact release/commit、archive snapshot+digest；动态网页或branch URL只能用于导航。

### 4. Restricted MCP

`masi-mcp-readonly/v1` 是私有安全 profile，不是通用兼容 MCP server：

- initialization 协商 `2025-11-25`；后续请求发送 `MCP-Protocol-Version: 2025-11-25`，或由同一认证 session 中保存的协商结果唯一识别；
- invalid/unsupported，或缺 header 且无法由 session 识别时返回 HTTP 400；不解析 `2025-03-26` payload，也不暴露 legacy HTTP+SSE endpoint；
- 只启用 binding/manifest 双 allowlist 中的 tools/resources；禁用 prompts、sampling、elicitation、任意 server mutation、未声明 notification 和任意 URI；
- Origin、mTLS/应用身份、DNS/IP/CIDR/port/TLS、redirect/SSRF、framing/body/output、round/call/total deadline 和审计全部有界；
- MCP client 支持 POST 的 JSON 与 SSE response；server 可以只返回 bounded JSON，但不得宣告未实现 capability。

官方 MCP 2025-11-25 规定客户端发送版本头；服务端在无法以其他方式识别版本且缺头时建议假定 `2025-03-26`。项目选择私有 endpoint fail closed 是有记录的收紧，因此文档和 Agent Card 不得声称通用 backwards-compatible MCP server。[MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

MCP schema 未声明 `$schema` 时默认 JSON Schema 2020-12，项目 server/client 必须支持并验证该 dialect。[MCP basic/schema rules](https://modelcontextprotocol.io/specification/2025-11-25/basic)

### 5. A2A

`a2a-agent/v1` 固定 A2A `1.0` Major.Minor；patch 不参与线上协商。MASI-NIDS client 每次请求携带 `A2A-Version: 1.0`；server 也接受官方允许的同值 query parameter，header/query 冲突时拒绝。缺失/空版本按上游规范识别为旧 0.3 后返回 `VersionNotSupportedError`，不双解析 0.3/1.0 payload。首期 HTTP+JSON、polling、no push、no streaming、静态 outbound peer allowlist。[A2A 1.0 specification](https://a2a-protocol.org/latest/specification)

### 6. gRPC Deadline、Retry 与 Cancellation

每个 method 在 `grpc-service/v1` 中固定：queue、connect、execute、total deadline；request/response/in-flight；idempotency；retryable status；max attempts；initial/max backoff；throttling；wait-for-ready；hedging；cancellation 和稳定错误映射。

- total deadline 包含排队、连接、执行和退避，客户端必须显式设置并传播剩余 deadline；server 必须停止派生工作和释放资源。
- 默认不配置 retry policy、channel retries disabled、effective attempts 为 1，hedging disabled；只有无外部副作用、使用同一 identity/input/config digest 且 profile 明确列出的 read/validation/execute method 可以重试。
- 获准重试的方法首期最多 2 attempts，只允许 `UNAVAILABLE`，退避 100 ms 起、1 s 上限并启用 throttling；不得重试 `FAILED_PRECONDITION`、`ABORTED`、validation/auth/resource error。
- central inference execute只有在请求已进入Edge input WAL、复用同一request identity与input digest、仍处同一pool/binding generation且剩余deadline足够时才可按`inference-central-grpc-batch/v1`重试；它允许at-least-once computation，不允许第二canonical Event。full-pool unavailable由Edge bounded WAL/HOLD/gap处理，不改变backend/model。
- health、activation mutation、effect/Edge/P4 RPC 禁用应用层和 channel transparent retry；模糊设备结果由原 operation journal/readback reconcile。
- 每个 client 暴露实际 method-config digest、attempt、transparent-retry 和 deadline/cancellation metrics。

gRPC 默认不设置 deadline；即使没有显式 retry policy，也可能发生透明重试。因此“未配置 retry”不能当作 mutation 安全保证。[gRPC deadlines](https://grpc.io/docs/guides/deadlines/)、[gRPC retry](https://grpc.io/docs/guides/retry/)

### 7. Wasm/WASI/WIT

`wasm-component/v1` 固定：

- 首期 qualification profile：WASI 0.2（Preview 2）Component Model；
- WIT package `masi:plugin-transform@1.0.0`，world `pure-transform`；
- 同步、确定性、纯计算 import/export；无 filesystem/network/clock/random/process/environment；
- fuel 为确定性 CPU 主预算，epoch/wall total deadline 为宿主外层保险；错误区分 fuel exhaustion、epoch interruption、deadline、cancellation；
- linear memory、table、instance、stack、input/output、compiled cache、concurrency 和 pool 全部有界；
- cache key 绑定 component、WIT、Wasmtime/compiler、target、CPU feature、config digest。

WASI 0.3 已于 2026-06-11 发布并标为 Stable，增加原生 async；0.2 仍为 Stable。项目保留 0.2 是资格和确定性选择，不是版本事实判断。0.3 必须创建新 profile，验证 Canonical ABI、async/cancellation、toolchain、cache、golden、性能和 rollback 后才能激活。[WASI roadmap](https://wasi.dev/roadmap)、[WASI releases](https://wasi.dev/releases)

Wasmtime 普通 major 发布快、支持周期短，生产 profile 必须选择仍受支持的固定 release/LTS，并把 runtime digest 写入 qualification；ADR 不永久硬编码一个会快速失去支持的 major。[Wasmtime release process](https://docs.wasmtime.dev/stability-release.html)

### 8. Local UDS Profile

同机 gRPC/IPC 使用每 binding/generation 独立绝对 UDS path；目录由 socket producer UID 拥有、peer 使用专用 GID，默认 directory mode `0750`、socket mode `0660`。创建/连接拒绝 symlink、抽象 namespace、非 socket、owner/group 不符和共享可写目录；以 `SO_PEERCRED` 或等价机制校验 UID/GID/PID/workload identity。UDS 继续执行 framing、size、deadline、backpressure、authorization 和 audit；本机路径不等于可信身份。

### 9. Target/Fleet 与条件 gNMI Profile

P4Runtime、target registry 与 gNMI 各有不同所有权：P4Runtime 负责单 target 的 P4 runtime/arbitration；Go/PG 保存 canonical target/fleet facts；条件 gNMI 只补充设备只读状态。gNMI current、NetBox inventory 或 endpoint 探测都不能自动改变 assignment、mutation readiness、P4 current 或 fleet membership。未来 gNMI `Set`/gNOI/完整 NMS 必须新增副作用 owner、治理、journal/readback/rollback 和 qualification profile，不能作为 v1 minor 扩展。

## 取舍

收益：版本和运行行为可机器验证；上游更新不会静默改变生产语义；MCP 安全收紧与通用规范的差异透明；gRPC/Wasm 的超时和重试可故障注入。

代价：需要维护 profile registry、生成器镜像和 old/new matrix；新上游版本不能立即使用；restricted MCP endpoint 不能直接服务只支持旧 fallback 的通用 client。

## 被拒绝的方案

1. 全部使用 `latest`：不可复现且无法滚动兼容。
2. 将 MCP/A2A/gRPC/WIT 合成一个插件 ABI：协议职责和资源模型不同。
3. 仅靠 SDK 默认重试/超时：默认行为不足以保护副作用和资源。
4. 因 WASI 0.3 稳定立即升级：没有项目工具链/性能/rollback 证据。
5. 为兼容旧 MCP 在同一 endpoint 双解析：扩大攻击和测试矩阵。
6. 用一个 `generation` 合并 target-control/assignment/actor/P4 application fence，或用 fleet parent 代替逐 target intent：会隐藏 ABA、部分结果和第二调度路径。
7. 因 gNMI/Stratum/NetBox/Ansible/Nornir“成熟”而开放写能力或接受上游 `latest` model/path/inventory：没有 exact profile 与项目授权。
8. 把开放式 ECharts/Vega/Grafana dashboard JSON 当插件输出 ABI：会引入浏览器执行/表达式面、版本耦合与不可控查询；v1 只接受封闭的统计数据和展示 hints。

## 迁移与回滚

greenfield 首先建立 profile registry、schema lint、generator lock 和 golden；模块只在引用合格 profile digest 后实现。升级新增 profile 并 side-by-side qualification，不原地覆盖 source/generated/runtime。回滚只能选仍受支持且能读取当前持久事实的 profile；否则相应接口 `HOLD/unavailable`。

## 验证

- source/generated digest、generator drift、unknown major/minor/dialect；
- MCP header/session、旧 payload、Origin/SSRF、method/capability allowlist；
- A2A 1.0/0.3/unknown version 与 patch 处理；
- gRPC transparent retry、deadline propagation、server cancellation、late result；
- WASI 0.2 imports、fuel/epoch、cache mismatch、WASI 0.3 rejection；
- UDS owner/mode/symlink/peer/generation/path replacement；
- Web source/generated client drift、lockfile/license/SBOM、browser target/polyfill、bundle/resource/CWV、SSE cursor/generation 与 current/previous Go/Web compatibility；
- rule observation counter/action/target profile、formula/status、sample/epoch 和 P4/Go/Rust/TypeScript golden；
- stateless firewall normalized/compiled plan、priority/default/fragment/unsupported、bank/selector/response operation 与 P4/Go/Rust/TypeScript/test-oracle golden；
- target/fleet identity/fence/lifecycle/parent-child-wave/status/error 与 Go/Rust/TypeScript/testkit golden；1/2/N、assignment handoff、partial/reconcile 和资源上限；
- 条件 gNMI exact proto/model/path/encoding/subscription/mTLS/rate/gap/freshness 与 `Set`/unknown path/credential negative；未启用时有 `NOT_APPLICABLE` evidence；
- traffic fixture/direction/rewrite/timing/impairment/oracle profile、sender-vs-DUT证据、generated/PCAP/live-session模式和 software-vs-hardware target matrix；
- telemetry source/snapshot/digest-sample/mirror profile、event-time/watermark/window/coverage/quality，以及central gRPC framing/tensor/batch/deadline/retry/copy/result→Event ACK的Rust/C++/Go跨语言golden与software/CPU/CUDA/hardware矩阵；
- model bundle/feature/label/output-adapter/wire/runtime/optimization profile、single/multi-label/OOD/abstain、Python/C++/Rust/Go/TypeScript golden、真实Gateway/Triton/selected ORT startup、selected/observed profile、dynamic batch/instance、model-control incarnation、pool/worker identity、single/HA availability、per-shard current/previous与Edge唯一route、WAL/action/readback/CAS/commit/resume/restart/quarantine/rollback和CPU/CUDA old-new matrix；两profile证据不能继承，fake/mock/readiness/microbenchmark不能替代正式E2E；
- qualification字段与claim-scope跨语言golden；single profile一个域内1..N副本不生成HA claim，CPU/CUDA、tier/topology/digest aggregate不互相继承；
- Compose runner exact版本、health/deadline/resource/evidence/cleanup和runner/backend缺失负例；不得自动切换runner，healthy/URL可达不得形成业务PASS；
- statistics definition/input/artifact/display/run-key/quality/status/resource profile 使用同一 golden，覆盖 Go/Rust/Python/TypeScript/Wasm；unknown projection/field/capability/display/metric、SQL/PromQL/JSONPath/表达式、NaN/Inf、oversize/high-cardinality、CSV formula、same-key-different-digest 和 late definition/generation 稳定拒绝；
- Triton NONE repository closure、额外成员拒绝、显式instance group、CUDA host-side provider partition读回/漂移拒绝；
- supply registry/lock/SBOM/NOTICE/provenance、tool/vulnerability-database freshness、source/generated digest 与 rejected component policy；
- 每个 profile 的 old/new client/service/runtime 和持久化事实矩阵。

## 参考

- OpenAPI：<https://spec.openapis.org/oas/>
- JSON Schema：<https://json-schema.org/specification>
- MCP 2025-11-25：<https://modelcontextprotocol.io/specification/2025-11-25/basic>
- A2A 1.0：<https://a2a-protocol.org/latest/specification>
- gRPC deadlines/retry：<https://grpc.io/docs/guides/deadlines/>、<https://grpc.io/docs/guides/retry/>
- WASI releases/roadmap：<https://wasi.dev/releases>、<https://wasi.dev/roadmap>
- Wasmtime release policy/interruption：<https://docs.wasmtime.dev/stability-release.html>、<https://docs.wasmtime.dev/examples-interrupting-wasm.html>
- WIT：<https://component-model.bytecodealliance.org/design/wit.html>
- Vue performance：<https://vuejs.org/guide/best-practices/performance>
- Vite production build：<https://vite.dev/guide/build.html>
- 成熟方案来源登记：`../research/mature-solutions-review-sources-2026-08-10.md`
- Frontend 评估：`../research/frontend-ops-console-and-source-reuse-assessment-2026-08-10.md`
- 规则观测与全系统复用评估：`../research/rule-effectiveness-and-system-reuse-assessment-2026-08-10.md`
- 在线模型模块化评估：`../research/online-model-modularity-assessment-2026-08-10.md`
- v1.13 central-GPU 历史评估：`../research/central-gpu-inference-architecture-assessment-2026-08-11.md`
- 当前CPU/CUDA与真实服务E2E评估：`../research/central-inference-cpu-cuda-and-real-e2e-assessment-2026-08-12.md`
- BMv2 无状态防火墙：`0014-bmv2-stateless-firewall-policy-and-activation.md`
- 多 target/fleet 与设备管理：`0015-multi-target-p4-fleet-and-device-management-boundary.md`
- OpenConfig gNMI：<https://openconfig.net/docs/gnmi/gnmi-specification/>
- Docker Compose startup order/health：<https://docs.docker.com/compose/how-tos/startup-order/>；Playwright webServer：<https://playwright.dev/docs/test-webserver>
- ONNX IR/metadata：<https://onnx.ai/onnx/repo-docs/IR.html>、<https://onnxruntime.ai/docs/api/c/struct_ort_1_1_model_metadata.html>
- ONNX Runtime Session/threading/optimization：<https://onnxruntime.ai/docs/api/c/struct_ort_1_1_session.html>、<https://onnxruntime.ai/docs/performance/tune-performance/threading.html>、<https://onnxruntime.ai/docs/performance/model-optimizations/graph-optimizations.html>
- ONNX Runtime execution provider placement：<https://onnxruntime.ai/docs/execution-providers/>
- Triton model management/configuration：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_configuration.html>
- v1.13 历史模型 pool/rollout 决策：`0016-central-gpu-inference-pool-and-routing-boundary.md`
- 当前 CPU/CUDA 启动选择与真实服务 E2E：`0017-central-inference-runtime-selection-and-real-e2e.md`
- 历史本机 startup/rollout 决策：`0011-startup-bound-model-selection-and-rolling-restart.md`
- P4 流量生成与回放决策：`0012-bmv2-p4-traffic-generation-and-replay.md`
- 在线遥测与推理热路径决策：`0013-online-telemetry-and-inference-hot-path.md`
- 插件统计与声明式 Web 投影：`0018-plugin-statistics-and-declarative-web-projection.md`
- 插件统计专项调研：`../research/plugin-statistics-and-declarative-web-assessment-2026-08-12.md`
- OpenTelemetry Metrics data model：<https://opentelemetry.io/docs/specs/otel/metrics/data-model/>
- Grafana data frames/time series：<https://grafana.com/developers/dataplane/dataframes>、<https://grafana.com/developers/dataplane/timeseries/>
