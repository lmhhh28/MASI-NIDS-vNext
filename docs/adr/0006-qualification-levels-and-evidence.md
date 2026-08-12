# ADR-0006：资格等级、早期 Boundary Rehearsal 与机器可读证据

- 状态：Accepted
- 日期：2026-08-10
- 决策者：Owner
- 需求基线：`vNext-requirements-1.17`
- 关联需求：`ARCH-TARGET-FLEET-001`、`CONTRACT-TARGET-001`、`CONTRACT-FLEET-EFFECT-001`、`CONTRACT-PROFILE-001`、`MOD-DB-001`、`MOD-INF-001`、`MOD-ML-001`、`MOD-TARGET-FLEET-001`、`DB-TARGET-FLEET-001`、`PERF-001`、`PERF-002`、`PERF-INF-001`、`PERF-TEL-INF-001`、`PERF-RULE-001`、`PERF-TRAFFIC-001`、`PERF-TARGET-FLEET-001`、`REL-INF-POOL-001`、`REL-TARGET-FLEET-001`、`SEC-TARGET-FLEET-001`、`DEP-TARGET-FLEET-001`、`OBS-TARGET-FLEET-001`、`WEB-PERF-001`、`WEB-A11Y-001`、`WEB-SUPPLY-001`、`WEB-TARGET-FLEET-001`、`TEST-002` 至 `TEST-010`、`TEST-GATE-001`、`TEST-REAL-E2E-001`、`TEST-INF-001`、`TEST-TEL-INF-001`、`TEST-PLUGIN-001`、`TEST-WEB-001`、`TEST-RULE-001`、`TEST-TRAFFIC-001`、`TEST-REUSE-001`、`TEST-TARGET-FLEET-001`、`ACCEPT-001`、`DEC-001`、`DEC-019`、`DEC-020`、`DEC-021`、`DEC-023`、`DEC-024`、`DEC-025`、`DEC-026`、`DEC-027`、`DEC-028`、`DEC-029`、`DEC-030`、`DEC-032`、`DEC-033`、`DEC-034`、`DEC-035`、`DEC-036`、`DEC-037`、`DEC-038`

## 背景

全模块 Module Complete 后才允许正式 pairwise integration，可以阻止“边联调边补实现”被误报为完成；但若连真实 TLS、UDS、framing、生成 client/server 和容器边界都禁止提前接触，协议错误会过晚暴露。成熟的 contract testing 可在持续集成中提前验证具体交互，但它既不是完整 schema conformance，也不是系统 E2E。[Pact contract testing](https://docs.pact.io/)

因此需要把“允许尽早发现问题”和“不得提前声称集成通过”分别编码，而不是使用含糊的“联调”。

## 决策

### 1. 正交资格字段与声明范围

`qualification-evidence/v1` 不再使用把阶段、结果和资格拼在一起的自由字符串。每条 evidence 必须独立携带：

| 字段 | 封闭值 | 含义 |
|---|---|---|
| `level` | `REHEARSAL|MODULE|PAIRWISE|SYSTEM_E2E|PRODUCTION` | 证据在哪个门禁产生；不表达成功或失败 |
| `applicability` | `APPLICABLE|NOT_APPLICABLE` | requirement/profile 对该精确 scope 是否适用；后者必须有稳定理由 |
| `result` | `PASS|FAIL|HOLD|NOT_RUN` | 实际执行结果；`HOLD`是前置/profile/环境/证据阻断，`NOT_RUN`是未执行 |
| `qualification` | `QUALIFIED|NOT_QUALIFIED` | 该精确 claim scope 是否取得相应资格 |

另有不可缺少的 `claim_scope`，至少绑定 runtime profile、availability profile、deployment tier、topology、release/image/artifact/config/contract/environment digest。展示层可以把字段推导为 `REHEARSAL/NOT QUALIFIED`、`MODULE PASS`、`SYSTEM E2E PASS` 或 `PRODUCTION QUALIFIED`，但这些不是第五套 wire 状态；历史短语 `HOLD/NOT RUN` 只是 result 的两个可能值。

聚合规则固定为：

- `level=REHEARSAL` 永远是 `qualification=NOT_QUALIFIED`，即使 result 为 PASS；
- `applicability=NOT_APPLICABLE` 不参与 aggregate，但必须给出稳定 requirement/profile/reason，且不能用于跳过 release 已声明的必需能力；
- required applicable 项只有 `result=PASS` 且同 scope 的前置证据有效时才可 `qualification=QUALIFIED`；其他 result 阻断聚合，受控性能 waiver 仅按第 6.1 节处理；
- Module、pairwise、System E2E 和 production 分别聚合，低 level 证据不能升级、复制或重命名；System E2E 每个 fixture×runtime×availability×tier×topology×fault scenario 是独立 evidence，套件只有全部 required applicable 场景通过才 PASS；
- CPU、CUDA、single-domain、HA、不同 topology 和制品 digest 各自形成 scope。CPU PASS 可资格化只声明 CPU 的部署，但不能替代 CUDA；单故障域 PASS 不能宣称 HA。产品若声明同时支持 CPU/CUDA，则两套 required matrix 都必须通过。
- `deployment-tier/v1` 封闭为 `development|acceptance|operational-single-domain|production-ha`。前三项最高只能聚合到 `SYSTEM_E2E`；只有 `production-ha` 可以出现 `level=PRODUCTION`，且仍须通过 `availability-ha/v1`、绝对生产性能/容量、PostgreSQL HA/PITR 与全部适用生产门禁。

任何资格都必须绑定精确 digest；“上次通过”“本地可用”、人工截图、另一profile的结果或健康端点不能代替。

### 2. Module Complete 前允许的 Boundary Rehearsal

允许使用真实生成 client/server、TLS/mTLS、UDS、framing、容器、SDK 和名称含 `test` 的真实 PostgreSQL 实例验证：

- handshake、version、schema、错误映射、oversize；
- deadline/cancellation/retry、disconnect/reconnect；
- workload identity、certificate、UDS peer credential；
- generated code 和语言 runtime interoperability；
- migration/transaction 行为及 test-only restore。
- `traffic-replay/v1` 的 generated/synthetic/PCAP/live-session wire rehearsal、方向/rewrite/timing/netem 与 packet oracle，但仅限 fake/BMv2/明确 test target。
- `telemetry-p4-window/v1`的bank/epoch/snapshot/read-clear、Digest/PacketIn best-effort drop，和`inference-central-grpc-batch/v1`真实Rust↔C++ Gateway mTLS/framing/tensor/batch/deadline/retry/fence/ACK rehearsal；Gateway↔pinned Triton/selected ORT CPU或CUDA可在隔离test profile演练，但不能转为另一runtime、hardware coverage、HA、system或production PASS。
- `p4-stateless-firewall/v1` 的 normalized compile、inactive-bank write/readback、selector flip/reconcile、response overlay、PTF packet oracle 与 host-filter-negative rehearsal；仅限 BMv2/test identity，仍不能转为 production effect、硬件或 system PASS。
- `target/v1`、`fleet-operation/v1` 与 `p4-target-fleet/v1` 的 1/2/N BMv2 actor/session、registration/assignment handoff、parent/child/wave/gate/partial/reconcile rehearsal；仅限 fake/BMv2/test identity，parent 仍不可 claim，结果不能转为硬件或 production fleet PASS。
- 条件 `target-gnmi-readonly/v1` 的 exact `Capabilities/Get/Subscribe`、mTLS、path/model、rate/gap/freshness rehearsal；必须用无写 credential 并证明 `Set` 拒绝，未触发 profile 时记录 `NOT_APPLICABLE`。
- Vue production build 与 generated OpenAPI client 对真实 Go test endpoint 的同源 cookie/CSRF、SSE cursor/gap、CSP、browser target 和 asset cache/rollback 行为。

必须同时满足：

- 隔离 test tenant/identity/namespace/database；
- P4 使用 fake/BMv2 test target，或只读接口；禁止生产 target 和真实生产 effect；
- 不使用生产 credential/data，不绕过 manifest/capability/sandbox；
- 不以 adapter、stub 或上下游内部实现代替被测模块自身职责；
- evidence 明确`level=REHEARSAL, qualification=NOT_QUALIFIED`（展示可为`REHEARSAL/NOT QUALIFIED`），记录限制并在正式门禁后从干净环境重跑。

rehearsal 发现模块缺陷时正常修复并重跑模块门禁；它不会使模块提前获得或失去一个尚不存在的 Module PASS。

### 3. 正式集成门禁

只有所有首期模块同时达到 Module Complete，才能产生新的 `PAIRWISE PASS`。正式运行必须使用完整构建制品、受支持 profile、独立 evidence ID 和干净状态；早期 rehearsal 的数据库、socket、证书、日志和结果不得复用为 PASS。

正式集成发现模块内部缺陷时，该模块立即退出 Module Complete；修复并完整重跑自身门禁后才恢复对应 pair。契约/fixture 缺陷同样生成新的 contract/profile digest，所有受影响证据失效。

### 3.1 真实服务启动与 E2E 硬门禁

实现阶段必须真实启动被测服务，不以“代码可编译”或“mock请求成功”代替运行：

- Module black-box E2E真实启动被测模块的发布候选binary/OCI及实际runtime，并只通过公开边界观察。尚未接入的邻居可以是contract fake，但被测模块自身及其内部runtime不能被替代；
- 正式PAIRWISE E2E在干净环境真实启动边界两侧的发布候选服务，任一侧不得fake；
- 正式SYSTEM E2E真实启动BMv2/P4Runtime、Rust Edge、所选Central Inference CPU或CUDA stack、Go Control、真实PostgreSQL、Rust Plugin Host、deterministic statistics conformance plugin、官方Python Analysis Plugin和Vue Web，并通过真实TLS/UDS/gRPC/HTTP/数据库/浏览器边界运行；Host、统计 fixture 与Analysis是并列的必需资格主体，不代表Analysis业务流量经过Host；
- 外部LLM可使用资格化deterministic provider fixture保证断言可重复，但Analysis Plugin、MCP/A2A边界及Host必须真实启动；Go/peer↔Analysis和Analysis↔MCP走direct typed boundary，Host另行验证Wasm/Host-managed service；真实provider另做TLS/auth/quota/timeout/redaction smoke；
- CPU与CUDA分别执行probe→显式选择→startup→load/warmup→readback→numeric/golden→fault→performance→shutdown并形成独立evidence；一方PASS不能继承给另一方，无环境时该profile为`result=HOLD|NOT_RUN, qualification=NOT_QUALIFIED`。只声明CPU的scope不因CUDA环境缺失而失败，但不得产生CUDA或“双profile支持”声明。

readiness、端口连通、fake/mock、microbenchmark、仅运行ORT/Triton、开发服务器和boundary rehearsal均不能替代上述PASS。任一必需内部服务未启动、公开边界被绕过、使用旧rehearsal状态或证据不完整时，资格系统必须拒绝记录PASS。

### 3.2 首期 E2E Runner

本地/CI 的正式 Module、pairwise 和 BMv2 System E2E 固定使用 `e2e-runner-compose/v1`，而不是在 Docker Compose、Podman Compose、Kubernetes 和 systemd 之间自动选择。profile 必须固定 Compose/Engine/API 与 runner image digest、隔离 project/network/volume 名称、显式 healthcheck/`service_healthy` 依赖、startup/total deadline、CPU/RAM/disk/port预算、证据抓取、退出码和精确清理。Docker Compose 默认只保证依赖启动顺序，只有显式 healthcheck 才能等待就绪；即使 `service_healthy` 也只允许继续测试，不能建立业务 PASS。[Docker Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/)

Playwright 只作为真实浏览器 driver；其 `webServer`/URL等待不拥有多服务生命周期，也不证明核心链路。生产或多主机部署可以由其 exact deployment profile 选择 Kubernetes/systemd/Podman adapter并独立资格化，但一个场景不得失败后静默切换 runner，另一 runner 的 evidence 不能继承。[Playwright webServer](https://playwright.dev/docs/test-webserver)

### 4. 公共 Evidence Envelope

所有 evidence 使用 append-only、自哈希的公共 envelope，至少包含：

- `schema_version`、`evidence_id`、独立的`level`、`applicability`、`result`、`qualification`；
- `claim_scope`：runtime/availability/deployment-tier/topology/release/environment/profile/artifact digest；
- requirement/test/scenario ID；
- source commit、image/artifact/config/contract/profile/schema/model digest；
- environment profile、runner/toolchain/version；
- run identity、起止时间、seed、actor/service identity；
- command/entrypoint、输入/fixture digest；
- 实际服务/进程/container清单、startup/ready/drain/stop timeline、拓扑/端口/证书identity，以及每个fake/fixture的声明和适用边界；
- 对Central Inference记录selected runtime profile、observed hardware/provider partition、availability profile、deployment tier和CPU/CUDA独立run identity；`availability-single/v1`另记录唯一failure domain及域内1..N replica/required/min-ready/max-unavailable，`availability-ha/v1`另记录跨域N+1；
- 对插件统计记录 plugin/revision/config/binding generation、definition ID/revision/digest、host-owned input projection/field profile与适用的approved external-source capability、scope/input/window/trigger-or-schedule revision、on-demand/schedule actor与policy revision、authorization decision、idempotency/run key、input/artifact/display profile digest、artifact digest、run/quality status、实际 definitions/points/series/table rows/bytes/deadline 与 Go projection identity；
- raw artifact refs 与 hash、redacted log/trace refs；
- expected/observed invariants、exception/waiver reference；
- producer、签名/attestation 或 CI workload identity。

允许的 wire result 只有 `PASS|FAIL|HOLD|NOT_RUN`，其中 `NOT_RUN` 的人类显示为 `NOT RUN`；`REHEARSAL` 是 level，不是把未通过改名为成功，`NOT_APPLICABLE` 是 applicability 而不是 result。缺 schema、digest、原始结果、环境或 requirement mapping 时为 `result=HOLD, qualification=NOT_QUALIFIED`。

### 5. Fault Evidence

`fault-evidence/v1` 扩展公共 envelope，必填 fault injection、pre/intermediate/post state、recovery steps、deadline、invariants 和 observation timeline。每个故障场景使用稳定 `scenario_id` 和随机 seed；故障注入工具、时钟和 target 也记录版本。摘要不能覆盖原始 state/trace；同一 run 的 artifacts 通过 hash manifest 关联。

### 6. Performance Environment 与结果

`performance-environment/v1` 固定：

- CPU/微码/频率/核隔离/NUMA、RAM；
- storage/fsync、NIC、kernel、container/cgroup、runtime；
- P4 target、PostgreSQL topology/settings、网络延迟/带宽；
- packet/Event/window/batch/target/plugin concurrency；统计另记录 schedule/run queue、per-binding in-flight、input/artifact bytes、metric/series/point/table/row/display-hint 数量和 retention；
- target count、per-Edge/Control/operation/wave target 上限、parallel child、assignment/actor、每 target/global P4/gNMI RPC、StreamChannel、FD/task/thread、journal/WAL/disk/queue/memory、Go/DB rows/WAL/connections 与 API/SSE/UI matrix；
- telemetry source profile、observation domain/point、P4 aggregate bank/epoch/snapshot/read-clear、Digest/PacketIn generated/received/acked/drop、mirror backend/interface/NIC/driver/XDP/bind/copy mode、RSS/CPU/NUMA/ring/UMEM、source sequence/epoch、event-time/watermark/lateness/window/flow/coverage/quality；
- inference central gRPC endpoint/channel/resolver/mTLS、batch/message/in-flight/queue/deadline/retry、logical pool/worker attempt、Gateway/Triton dynamic queue/instance、selected CPU或CUDA runtime、CPU/core/thread/NUMA/RAM或GPU/VRAM/driver、actual network/Gateway-Triton/host-device copy bytes、input/result WAL、Edge→Go/PostgreSQL canonical ACK和packet/window→Event端到端分段；
- active rule/target/table、P4 counter Read batch/QPS/response、full-sweep/freshness、eligible/matching traffic、reset/wrap/saturation/gap、5-minute/hourly rollup 和 rule UI points；
- firewall normalized/compiled rule count、expansion/capacity、overlay/active-inactive bank/selector、compile/write/readback/flip/CAS/reconcile/rollback stage、default/fragment、packet p50/p95/p99/throughput/drop correctness、host filter/eBPF/bridge/qdisc state；
- traffic fixture/class/size/packet/wire-byte、direction/rewrite/timing/rate/loop、netem/qdisc/offload/MTU、requested/sender/test-ingress/DUT/capture count、oracle、runner/BMv2 CPU/RSS/queue/drop 与 software/hardware target class；
- Web target workstation、viewport、browser engine/build target、LAN/受控弱网、cold/warm cache、API fixture/rows/chart points、bundle/route manifest、HTTP/SSE concurrency；
- source/image/config/model/contract digest；
- model bundle/repository closure/feature-schema/label-taxonomy/output-adapter/inference-wire/Gateway/Triton/selected ORT CPU或CUDA/optimization-artifact/pool+binding-generation/model-control-incarnation digest；CPU profile另记录CPU feature/微码/core/thread/NUMA/affinity/arena/RAM，CUDA另记录CUDA/cuDNN/driver/GPU/VRAM/stream/I/O Binding及已声明host-side operator partition；并记录deployment tier、single/HA availability、replica/required/min-ready/max-unavailable/capacity，其中single固定恰好一个failure domain且允许域内1..N副本，HA profile另记录failure-domain/N+1；同时记录per-shard current/previous/route epoch、startup stages、dynamic batch/显式instance、drain/WAL/action/termination/readback/CAS/commit/resume/restart/quarantine/rollback workload；
- warm-up、measurement、repeat、statistics/outlier；
- steady/peak/saturation/soak workload。

benchmark result 必须引用 environment digest 和原始结构化 samples。相对比较只有在 profile 等价时成立。绝对 SLO、RPO/RTO 未由 Owner 在目标 profile 中冻结或未实测时为 `HOLD/NOT RUN`，不得用实验机数字冒充生产资格。

### 6.1 性能 Waiver 的最高资格边界

waiver 不是 PASS。原 performance evidence 保持实际 `result` 和 `qualification=NOT_QUALIFIED`；aggregate 只在 waiver 明确列出的 requirement/claim scope/expiry/风险/补救/owner 范围内越过该单项，并必须在摘要列出 `waived_requirements`。默认 `max_qualification_level=MODULE`；若性能偏差不影响契约、正确性、安全、恢复和目标集成功能，Owner 可以显式允许到 `PAIRWISE` 或 `SYSTEM_E2E`，但不能泛化到另一环境/profile。绝对 production throughput/p99/capacity、HA remaining-capacity、RPO/RTO、安全和正确性门槛都不能被 waiver 提升为 `PRODUCTION QUALIFIED`。waiver 过期、scope/digest漂移或补救失联时，aggregate 自动回到 `NOT_QUALIFIED`，不得修改历史原始证据。

Frontend evidence 还必须分离 API wait、browser render/interaction 与 cache 命中，记录 production asset gzip/brotli bytes、Core Web Vitals p75、route navigation、long task、heap/DOM/chart/query-cache、request/SSE、a11y/browser matrix 和 bundle analyzer artifact。开发服务器、单次 Lighthouse 分数、截图或缓存旧数据不能成为资格证据。

插件统计 Frontend evidence 还必须记录内建 renderer/display-kind、server-normalized dataset/encode、分页/虚拟化、等价表格、quality/freshness/source identity、points/rows/DOM/heap/render p95，并证明 Artifact 中的 HTML/SVG/CSS/JS/URL/MIME/vendor option/formatter/expression/event/custom series/CSV formula payload 不能进入可执行浏览器或不安全导出路径。

规则观测 evidence 还必须绑定 P4 program/P4Info/target/counter profile、effect/operation/canonical entry、application generation/observation epoch、PCAP/PTF/P4Testgen input-output oracle、cumulative/raw delta/quality、eligible denominator、formula/coverage 和 independent outcome result。BMv2/PTF 结果只资格化对应 software target；缺真实硬件 byte/count/reset/scale 证据时硬件 profile 保持 `HOLD/NOT RUN`。

firewall evidence 还必须绑定 normalized policy/default、compiled plan、target/P4Info/compiler、active/inactive bank、selector pre/post、response overlay/TTL、Admin-maker/Operator-checker Decision、journal/readback/CAS timeline、capacity 与 host-filter pre/post state。Write ACK、bank populated、selector response、counter hit或宿主机 drop 均不能单独提升为 active/current/outcome；BMv2结果只资格化 exact software profile。

流量回放 evidence 还必须绑定 `traffic-replay/v1` fixture/transformation、source/license/privacy/ground-truth、runner/tool/backend、topology/direction/qdisc/offload/environment和 raw sender/test-ingress/DUT/counter/oracle result。requested rate、sender accepted、counter hit、detection observed与outcome verified不能合并；检测 replay、规则 outcome、软件性能和硬件资格使用不同 scenario/evidence ID。

在线遥测/推理 evidence 还必须绑定source/profile/P4Info/observation point、source runtime epoch/sequence、snapshot/coverage/quality/window、central gRPC request/batch/pool/worker attempt、model/route/binding、input/result/Event identity和每层durability/ACK。Digest ACK、PacketIn/capture收到、gRPC、Gateway、Triton/所选ORT runtime和PostgreSQL commit分别记录，只有最后一个exact canonical ACK可以推进source cursor。BMv2、PACKET_MMAP、AF_XDP、DPDK、Central Inference CPU和CUDA分别使用不同environment/profile；gRPC/Triton/ORT microbenchmark不能提升为hardware或packet/window→Event端到端资格。

Target/fleet evidence 还必须绑定 stable target set、control incarnation、assignment/actor/application generation、device/role/election/P4Info、每 target journal/readback/CAS、parent/child/wave/gate/failure policy、完整 child vector、资源与 fairness。单 target success、多数 child success、外部 inventory current、gNMI connected 或 ONOS/Stratum/Ansible/Nornir task success 都不能提升为 fleet applied；任何已尝试但无法确认的 child 使 parent `reconciling`。

### 7. PostgreSQL Qualification Target

PostgreSQL State/Migration 作为独立 qualification target，必须证明：

- migration checksum、独立 job、锁/statement timeout、expand/contract；
- real PostgreSQL integration、pool/connection/transaction budget；
- HA/failover、WAL archive、PITR 和隔离 restore；
- Owner 冻结的数值 RPO、自动 failover RTO、目标数据规模 restore RTO；
- restore 后 schema checksum、Event/Incident、effect治理/执行、target/fleet parent-child、plugin binding/revocation 与 plugin statistics schedule/run/artifact/current 引用链；对PITR/restore/clone/rewind，还必须证明在开放writer/ingest前分别轮换从未使用的model-control与target-control incarnation，旧模型envelope/readback/handshake及旧target assignment/actor/child result被fence并逐shard/逐target重新绑定与只读reconcile；无损failover则保持原incarnation；
- destructive test 只作用于名称含 `test` 且精确确认的数据库。

### 8. Offline ML Qualification Target

Offline ML Artifact Pipeline 不是在线服务，但必须有独立 qualification manifest：

- train/validation/test dataset 与 preprocessing digest；
- feature schema、label taxonomy、output adapter、class order/mode/unknown/OOD/abstain、seed、toolchain/config；
- model bundle/manifest/scaler/export/runtime profile digest、ONNX metadata 对照和跨 Python/C++ numeric tolerance；
- model profile 中具体 precision/recall/FPR/FNR、OOD/NaN/Inf threshold；
- offline replay、性能/资源、rollback/previous-reader compatibility；
- data/license/privacy/retention 与 accepted difference。

任一阈值缺失、数据无法重建、label/output 语义不明确、numeric golden 或目标分布不通过时为`result=HOLD|FAIL, qualification=NOT_QUALIFIED`；签名、SBOM、provenance 或 SHA-256 不代替模型质量。

### 8.1 Online Model Lifecycle Qualification

离线候选通过不等于在线可滚动。每个所选 `model-runtime-central-cpu/v1|model-runtime-central-cuda/v1` + `availability-single/v1|availability-ha/v1` + `deployment-tier/v1` + `model-rollout-pool-generation/v1` + target scope 还必须形成独立 lifecycle evidence：

- exact revision、qualification、Gateway/Triton/selected runtime/backend/observed hardware、feature/label/adapter/wire、repository/optimization artifact、resource与current/previous reader compatibility；
- pool startup envelope的incarnation/pool/route/worker identity、repository closure/显式instance-group/provider-partition，以及HA profile适用的failure-domain identity；repository verification、backend/Session-create、warmup/numeric/Gateway probe/readiness、`GetLoadedModel/GetPoolStatus`、所选availability profile的required/min-ready/max-unavailable（single为一个域内1..N副本，HA另含跨域N+1）、Edge route-withdraw/drain/WAL、PostgreSQL per-shard CAS、Go/Edge committed-binding handshake与resume timeline；
- steady/peak/rolling的CPU/RSS/RAM/network/dynamic-batch/instance/queue/p99，CUDA profile另含VRAM/GPU/stream/copy，CPU profile另含thread/NUMA/affinity/arena；以及per-shard route-withdraw/drain/unavailable/WAL buffer age/depth/overflow/gap/replay、active/warming/draining/terminating generation资源；
- concurrent rollout/rollback/recovery、deployment action conflict/duplicate、graceful/forced termination、start/readback/CAS/commit/stop response loss、restart budget/quarantine、Gateway/Triton/selected compute/Go/Edge/PostgreSQL crash/restart/failover/PITR、single profile中断，以及HA profile的single-replica/failure-domain/full-pool loss、partial mixed rollout、late incarnation/route/generation/attempt fence和exact previous rolling rollback；
- 负向证明Edge-local、CPU↔CUDA/TensorRT/LibTorch/旧模型自动fallback、Triton runtime load/poll、第二delayed batcher、第二route/current与scale-to-zero均未形成隐式路径；
- Offline ML/rehearsal comparison 的 input coverage、numeric/taxonomy/decision/quality/latency/error difference、`level/applicability/result/qualification`、exact scope 与不可导入生产 current/effect 的负向证据；
- single-label、multi-label、anomaly/open-set、unknown/OOD/abstain 及新增 label 的 old/new reader/policy matrix。

外部 registry alias、KServe/Triton route、Gateway process pointer、systemd/Kubernetes deployment status 或一次健康检查均不能替代 Go/PostgreSQL per-shard binding 与 exact pool readback。任一 identity/digest/profile/evidence 不完整时 rollout 为 `HOLD`，不是以 fallback model继续。

### 9. Probe 与 Restart Evidence

startup/readiness/liveness 必须分别故障注入。startup 成功前不执行 readiness/liveness；readiness 失败只停止新任务/摘流量，liveness连续失败才允许由canonical budget约束的重启。正常终止还必须证明route withdraw/drain先于stop，hook至少一次投递幂等，grace超时/SIGKILL产生gap/HOLD；PDB不能替代workload rollout门禁。该语义与Kubernetes官方[probe](https://kubernetes.io/docs/concepts/workloads/pods/probes/)、[Pod termination](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)和[disruption](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)模型一致，并适用于systemd/Podman等部署适配。

初始 plugin profile：startup ≤120 秒；readiness 5 秒周期、2 秒 timeout、3 次失败；liveness 10 秒周期、2 秒 timeout、3 次失败；10 分钟最多自动重启 5 次，随后 quarantine ≥15 分钟。放宽必须产生新 profile 和 cold-start/fault/restart-storm 证据。

## 取舍

收益：保留严格正式门禁，同时尽早暴露真实协议问题；阶段、适用性、结果、资格与声明范围不再混淆；CPU/CUDA和single/HA不会误继承；fault/performance/DB/ML 都能被机器验证和追溯。

代价：CI 需要更多 evidence schema、隔离环境和存储；首期要固定Compose runner并维护clean-start/cleanup；正式集成仍需重跑；初始接入成本高于只保存日志摘要。

## 被拒绝的方案

1. 完全禁止早期真实边界：反馈过晚。
2. 把 rehearsal 算作正式 integration：绕过全模块门禁。
3. 只保留 JUnit/截图摘要：无法验证环境、原始数据和 digest。
4. 把 Pact/contract test 当完整 E2E：只能覆盖声明的交互样例。
5. 为离线 DB/ML 目标免除 Module 级证据：会把恢复与模型质量留到系统末期。
6. 把fake/mock、readiness、端口连通或microbenchmark称为E2E：没有真实启动链和公开边界证据。
7. 让CPU/CUDA共用一个PASS：硬件、runtime、numeric、资源和故障矩阵不同，无法继承。
8. 把`NOT_APPLICABLE`、`HOLD`、`REHEARSAL`和`PASS`塞进一个status enum：无法可靠聚合或判断究竟是未执行、条件不适用还是失败。
9. 在Compose/Podman/Kubernetes/systemd之间自动选择“可用runner”：同一个scenario会因机器环境改变含义，无法重现。
10. 用性能waiver把绝对production门槛标为PASS：掩盖真实容量风险并污染后续scope。

## 迁移与回滚

现阶段无既有 PASS 证据需要迁移。新 CI 首先建立 schema validator 和 evidence storage，再允许上传 rehearsal/module evidence。schema 升级采用新 major/minor 和兼容 reader；不得重写历史 evidence。证据系统不可用时测试可以执行，但资格状态保持 `HOLD`。

## 验证

- rehearsal 环境无法访问生产 target/credential/data；
- evidence level 不能被 CI/API/UI 提升或改名；
- `level/applicability/result/qualification`跨Go/Rust/C++/Python/TypeScript使用同一golden；`NOT_APPLICABLE`不能进入result，HOLD/NOT_RUN不能进入level，展示摘要不能回写wire；
- 缺 profile/digest/raw artifact 的结果稳定 `HOLD`；
- 正式 pairwise 在全模块 gate 前无法调度或记 PASS；
- Module、pairwise和system调度器分别验证真实service inventory；缺进程/container、runtime、公开边界、start/stop timeline或使用内部fake时无法记PASS；
- fault envelope、performance samples、DB restore、ML manifest 与 model pool rollout/startup-readback/offline-comparison evidence 均通过 schema/golden；
- probe/restart/quarantine、deployment action/termination、model incarnation/pool/route/commit状态机、CPU/CUDA显式选择与mismatch、single/HA availability、所选profile的绝对rolling capacity，以及HA profile适用的failure-domain/N+1通过真实启动、故障注入与benchmark。
- Web rehearsal 不能被改名为 browser/E2E/performance PASS；production build、资格化 browser、a11y 人工复核、dependency/license 和 current/previous Go/Web rollback evidence 缺一均为 `HOLD`。
- statistics conformance plugin 必须以真实 Host/direct typed boundary 运行，并贯通 immutable definition qualification→Go frozen input→durable run→Artifact validation→PostgreSQL projection→OpenAPI/SSE→production Web/Playwright；同一场景还要验证 on-demand 与 append-only schedule 的授权、幂等、每次执行重新 fence、宿主固定控件和插件 action 负例。仅 schema test、fixture JSON、组件 story 或直接把插件输出交给浏览器不能形成 `TEST-PLUGIN-STAT-001` PASS。
- PTF/P4Testgen/BMv2 rehearsal 不能被改名为硬件 rule-effectiveness/outcome PASS；counter hit 没有独立 oracle 时不得把 evidence level 或业务结论提升为 action success。
- inactive bank 完整、selector Write ACK、UFW/nftables drop 或 typed R3 approval 不能被改名为 firewall current/packet outcome；必须有 selector/bank readback、PostgreSQL CAS、独立 oracle 和 host-filter-negative evidence。
- Tcpreplay sender success、Mininet/BMv2 requested rate、公开 corpus label或TRex发生器统计不能被改名为 DUT ingress、检测正确、action outcome或硬件生产容量 PASS；缺许可证/隐私/ground-truth/双端 observation时保持 `HOLD/NOT RUN`。
- DigestListAck、收到PacketIn/镜像包、多个P4 Read成功、gRPC吞吐、Triton server latency或ONNX Runtime `Run()`延迟不能被改名为完整source coverage、原子window、packet→Event端到端或production PASS；缺snapshot、drop、watermark、WAL/ACK、actual-copy、绝对门槛，或HA profile缺N+1时保持`HOLD/NOT RUN`。
- 成熟组件的官方镜像、签名、SBOM、scanner PASS 或默认 dashboard 不能被改名为 MASI module/production qualification；仍须绑定 registry、权限、故障、性能、兼容、回滚与退出 evidence。
- 单 target demo、P4Runtime/gNMI connected、Stratum/ONOS/NetBox/Ansible/Nornir 可用或 fleet 多数成功不能被改名为 multi-target/fleet PASS；必须有 1/2/N、assignment/election、isolation/fairness、parent-child/wave、partial/reconcile/rollback、PITR incarnation 和绝对性能证据。
- `availability-single/v1`的1..N域内副本不产生HA claim；CPU/ CUDA与single/HA/tier/topology aggregate互不继承；产品双runtime声明必须两套matrix通过。
- `e2e-runner-compose/v1`验证exact版本、health依赖、deadline、资源、evidence和clean cleanup；故意移除runner/backend时scenario为HOLD/NOT_RUN，不能自动切换；Playwright URL可达/Compose healthy不能提升业务qualification。
- 性能waiver保留原result并受scope/expiry/max-level约束；过期或漂移使aggregate失效，任何production绝对门槛缺失/失败时无法记录PRODUCTION QUALIFIED。

## 参考

- Pact contract testing：<https://docs.pact.io/>
- Kubernetes probes：<https://kubernetes.io/docs/concepts/workloads/pods/probes/>
- Docker Compose startup order/health：<https://docs.docker.com/compose/how-tos/startup-order/>
- Playwright webServer/real browser driver：<https://playwright.dev/docs/test-webserver>、<https://playwright.dev/docs/test-assertions>
- PostgreSQL continuous archiving/PITR：<https://www.postgresql.org/docs/current/continuous-archiving.html>
- Core Web Vitals thresholds：<https://web.dev/articles/defining-core-web-vitals-thresholds>
- WCAG 2.2：<https://www.w3.org/TR/WCAG22/>
- 成熟方案来源登记：`../research/mature-solutions-review-sources-2026-08-10.md`
- 规则观测与全系统复用评估：`../research/rule-effectiveness-and-system-reuse-assessment-2026-08-10.md`
- 在线模型模块化评估：`../research/online-model-modularity-assessment-2026-08-10.md`
- v1.13 central-GPU 历史评估：`../research/central-gpu-inference-architecture-assessment-2026-08-11.md`
- v1.13 历史模型 pool/rollout 决策：`0016-central-gpu-inference-pool-and-routing-boundary.md`
- 当前CPU/CUDA启动选择与真实服务E2E：`0017-central-inference-runtime-selection-and-real-e2e.md`
- CPU/CUDA与真实E2E独立评估：`../research/central-inference-cpu-cuda-and-real-e2e-assessment-2026-08-12.md`
- 历史本机 startup/rollout 决策：`0011-startup-bound-model-selection-and-rolling-restart.md`
- P4 流量生成与回放决策：`0012-bmv2-p4-traffic-generation-and-replay.md`
- 在线遥测与推理热路径决策：`0013-online-telemetry-and-inference-hot-path.md`
- BMv2 无状态防火墙决策：`0014-bmv2-stateless-firewall-policy-and-activation.md`
- 多 target/fleet 与设备管理决策：`0015-multi-target-p4-fleet-and-device-management-boundary.md`
- 插件统计与声明式 Web 投影：`0018-plugin-statistics-and-declarative-web-projection.md`
- 插件统计专项调研：`../research/plugin-statistics-and-declarative-web-assessment-2026-08-12.md`
