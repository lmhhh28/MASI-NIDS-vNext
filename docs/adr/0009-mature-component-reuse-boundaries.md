# ADR-0009：全系统成熟组件复用边界

- 状态：Accepted
- 日期：2026-08-10
- 决策者：Owner
- 需求基线：`vNext-requirements-1.18`
- 关联需求：`ARCH-001`、`ARCH-003`、`ARCH-REUSE-001`、`ARCH-MODEL-001`、`ARCH-TELEMETRY-001`、`ARCH-TARGET-FLEET-001`、`CONTRACT-001`、`CONTRACT-PROFILE-001`、`CONTRACT-MODEL-001`、`CONTRACT-SUPPLY-001`、`CONTRACT-TRAFFIC-001`、`CONTRACT-TELEMETRY-001`、`CONTRACT-INFERENCE-001`、`CONTRACT-TARGET-001`、`CONTRACT-FLEET-EFFECT-001`、`DB-005`、`DB-006`、`DB-MODEL-001`、`DB-TARGET-FLEET-001`、`PERF-INF-001`、`PERF-TEL-INF-001`、`PERF-TRAFFIC-001`、`PERF-TARGET-FLEET-001`、`REL-INF-POOL-001`、`OBS-001`、`OBS-INF-001`、`OBS-TEL-INF-001`、`OBS-RULE-001`、`OBS-TRAFFIC-001`、`OBS-TARGET-FLEET-001`、`SEC-SUPPLY-001`、`SEC-TRAFFIC-001`、`SEC-TEL-INF-001`、`SEC-TARGET-FLEET-001`、`WEB-SUPPLY-001`、`TEST-002`、`TEST-REAL-E2E-001`、`TEST-INF-001`、`TEST-TEL-INF-001`、`TEST-TRAFFIC-001`、`TEST-REUSE-001`、`TEST-TARGET-FLEET-001`、`DEC-002`、`DEC-003`、`DEC-017`、`DEC-022`、`DEC-025`、`DEC-026`、`DEC-027`、`DEC-028`、`DEC-029`、`DEC-030`、`DEC-032`、`DEC-033`、`DEC-034`、`DEC-035`、`DEC-036`、`DEC-037`、`DEC-038`

## 背景

vNext 是 greenfield 重写，但不应重写数据库连接池、HA 编排、备份恢复、协议代码生成、指标采集、告警路由、图表引擎、P4 测试框架、SBOM、漏洞扫描和制品签名等通用能力。相反，“直接采用成熟平台”也可能带来第二事实源、第二工作流、第二 P4 controller、过宽权限、隐式在线依赖和难以退出的控制面。

本 ADR 回答两个不同问题：哪些通用机制值得复用，以及复用后谁仍拥有 MASI-NIDS 的业务语义与正确性。结论不是“全部自研”或“拼装现成产品”，而是成熟通用机制优先、核心不变量自有且唯一。

## 决策原则

1. 优先采用公开契约/文件/进程边界的组件，而不是复制应用内部源码。
2. `ADOPT`、`CONDITIONAL`、`REJECT` 描述用途，不代表实现或生产资格；exact artifact/profile/evidence 才能获得资格。
3. 组件不得取得 PostgreSQL 核心事实、Go 业务写入、Edge P4 session、effect queue、generation/fence/journal/readback/CAS 或人类 authorization 的所有权。
4. 组件应可被关闭、替换或回滚；失效时按该能力的显式故障语义退化，不能改变canonical fact或设备状态。Central Inference允许启动前显式选择已资格化CPU或CUDA profile，但禁止运行中backend/model/location/profile fallback；全池失效为HOLD/gap。
5. 依赖、CLI、generator、image、漏洞数据库、dashboard/config、vendored source 和 generated output 全部进入 `contracts/supply-chain/v1`；每个采用事实用稳定`source_id`绑定title、access date、exact release/commit、archive snapshot+digest，动态网页URL只作导航。

## 首期采用矩阵

| 能力 | 候选 | 结论 | MASI-NIDS 边界 |
|---|---|---|---|
| Protobuf lint/breaking/codegen | Buf CLI + `protoc`/固定插件 | `ADOPT` | `contracts/` 仍是 source；Buf 只检查/生成，不拥有 schema registry 事实或自动发布 |
| REST client/codegen | 资格化 OpenAPI generator | `ADOPT` 一种 | 用同一 fixture 比较候选后固定；generated output 有 digest/golden，禁止页面手写第二套 DTO |
| P4 编译/模型 | `p4c`、官方 P4Info/P4Runtime proto | `ADOPT` | Edge 仍是唯一 session owner；compiler/proto version 进入 P4 profile |
| 多 target P4 runtime | P4Runtime 1.4.1 | `ADOPT` | 按 target 使用 `(device_id, role, election_id)` arbitration/read/write；Edge `TargetActor` 仍独占 session/journal，协议不提供跨 target 原子性或完整 NMS |
| 设备状态/遥测协议 | OpenConfig gNMI | `CONDITIONAL read-only` | `target-gnmi-readonly/v1` 只允许 exact model/path 的 `Capabilities/Get/Subscribe`、独立只读 identity/mTLS/预算；首期拒绝 `Set` |
| 设备运维 RPC | OpenConfig gNOI | `REJECT first-release mutation` | reboot、OS、证书、诊断等需要新的 owner、治理、回滚与 target 矩阵；不得从读能力推导写授权 |
| target-side switch stack | Stratum | `CONDITIONAL` | 可作为 exact P4Runtime/gNMI target-side 实现；不部署 controller path 替代 Edge，不以单一实现证明跨硬件兼容 |
| 外部 intended inventory | NetBox/组织 CMDB | `CONDITIONAL candidate-only` | 仅导入带 provenance/digest 的 candidate；Go 展示 diff，Admin step-up 后写 canonical registry；外部 current/delete 不自动生效 |
| 网络自动化 | Ansible Network/Nornir | `CONDITIONAL offline/test/read-only` | 只做离线 provisioning、隔离测试或只读核验；不进入 effect/wave gate/current，不持有 production P4 writer credential |
| P4 无状态软件 target | BMv2 `simple_switch_grpc` + v1model | `ADOPT` for exact software profile | 执行 ADR-0014 response overlay/baseline bank/selector；只授予功能、恢复和软件容量资格，不外推硬件/PSA/TNA/line-rate |
| P4 数据面测试 | PTF + P4Testgen | `ADOPT` for test | 只在 testkit/qualification 注入包、生成 oracle；不能进入生产控制面 |
| P4 entity constraint 检查 | p4-constraints library/CLI | `CONDITIONAL` defense-in-depth | exact revision/profile 后用于 CI/preflight 补充；CLI test/experiment only，不授权、下发、拥有容量或替代 Edge compiler/readback |
| 软件 P4 拓扑/链路扰动 | Mininet + BMv2 + Linux netem | `ADOPT` for test | 固定 namespace/veth/qdisc/kernel/offload profile；只资格化 exact software target，不外推硬件容量 |
| PCAP 二层回放与改写 | Tcpreplay/tcprewrite/tcpprep | `CONDITIONAL` test-only | GPLv3/NOTICE/image审查后作为隔离外部工具；不 vendoring/链接生产，发送成功不等于 DUT收到，L2 replay不冒充 live session |
| 硬件高率/有状态流量发生 | TRex 或同矩阵选定的一种等价工具 | `CONDITIONAL` future hardware profile | 只有目标硬件/容量需求与普通 runner不足证据后引入；DPDK/NIC/CPU/双端统计单独资格化，不拥有 P4 control |
| P4 交互诊断 | P4Runtime Shell | `CONDITIONAL` test/read-only | 上游把它定位为交互式 shell 且仍为 work in progress；不得常驻、调度、写生产 target 或成为部署依赖 |
| P4在线遥测主源 | P4 counters/registers + target-qualified bank/epoch/snapshot | `ADOPT` project profile | 使用P4Runtime 1.4.1 official client/proto；Edge仍是唯一P4/source owner。项目自行拥有window/quality/WAL语义，不假设普通Read原子 |
| P4 supplemental telemetry | Digest/PacketIn/clone | `ADOPT` bounded sample only | Digest/Ack best effort、PacketIn可丢；只作hint/sample/evidence并报告coverage/drop，不成为可靠queue或全量包feed |
| 兼容镜像抓包 | Linux PACKET_MMAP/TPACKET_V3 | `CONDITIONAL` | 只有feature/target需要packet header时启用；固定mirror interface/ring/fanout/snaplen/drop/offload，仍由Edge adapter拥有source/window |
| 高率镜像抓包 | Linux AF_XDP/libbpf/libxdp | `CONDITIONAL` after PACKET_MMAP evidence | 强制exact XDP/bind/copy mode、RSS/CPU/NUMA/UMEM/ring；禁止silent fallback；不与默认source同时常驻 |
| 硬件line-rate抓包 | DPDK PMD | `CONDITIONAL` last resort | 只有AF_XDP仍不足且真实hardware SLO明确时引入；专用core/hugepage/NIC/PMD profile，不取得P4/effect所有权 |
| flow/time/window语义 | IPFIX RFC 7011/7012/5103、Beam/Flink | `ADOPT` pattern only | 借鉴observation identity、typed fields、event time/watermark/lateness；不部署collector/stream runtime或第二scheduler |
| Edge↔Central Inference transport | gRPC C++/Rust + Protobuf/TLS | `ADOPT` project profile | `inference-central-grpc-batch/v1`使用bounded batched-unary、channel reuse、deadline和应用幂等；不使用逐记录RPC或全池单一长stream；Edge保留WAL/router |
| Inference buffer/copy优化 | ONNX Runtime I/O Binding/device tensor、Triton shared-memory extension | ORT I/O Binding `ADOPT when proven`；Triton shm `CONDITIONAL` | Edge→Gateway、Gateway→Triton、host↔device分别测actual copy/lifetime/sync；优化API不取得identity/lifecycle，不能称网络端到端zero-copy |
| Inference batching | Triton dynamic batching/instance group | `ADOPT execution` | Triton是首期唯一delay-based batch scheduler；参数由Model Analyzer/等价benchmark冻结。Edge只无等待coalesce，Gateway无第二batch timer |
| 在线模型 CPU runtime | pinned Triton + ONNX Runtime CPU | `ADOPT` explicit CPU profile | 固定Triton/ORT/CPU feature/微码/thread/NUMA/affinity/arena/RAM、ONNX/opset/operator、wire、optimization、dynamic batch/instance/resource；启动前人工选择，不与CUDA自动切换 |
| 在线模型 CUDA runtime | pinned Triton + ONNX Runtime CUDA | `ADOPT` explicit CUDA profile | 固定Triton/ORT/CUDA/cuDNN/driver/GPU/VRAM/stream/I/O Binding、ONNX/opset/operator、wire、optimization、dynamic batch/instance/resource，以及资格化时已声明/读回/测量的host-side operator partition；运行期新CPU接管视为drift，不与CPU自动切换 |
| 在线模型启动/滚动模式 | Triton `NONE` + project pool generation；Kubernetes CPU/GPU/topology/PDB作adapter | `ADOPT` / Kubernetes `CONDITIONAL` | startup-only只读exact repository closure、显式instance group；new pool先load/warm/readback/资格化，再由Edge逐shardroute-withdraw/drain/WAL→PG CAS→commit/resume。single为一个域内1..N副本且不声称HA，HA为同profile跨域N+1；Kubernetes不拥有current/profile选择或证明容量 |
| GPU 优化 backend | TensorRT | `CONDITIONAL explicit generation` | 仅目标GPU、跨backend numeric golden与端到端收益成立时形成独立pool generation；engine/plan/plugin不动态注入，绝无runtime fallback |
| 非 ONNX runtime或自动profile切换 | LibTorch、OpenVINO、CPU↔CUDA fallback | `REJECT first-release fallback` | ORT CPU已是显式首期profile；其他runtime只能未来以新需求/profile资格化，不常驻备用、不因故障自动切换 |
| 训练/实验 registry | MLflow Model Registry/Signatures 或等价 | `CONDITIONAL` offline evidence | 可借鉴/导出 signature、immutable version、tags/alias promotion 证据；alias/stage/REST 不成为生产 active binding、在线依赖或第二数据库控制面 |
| PostgreSQL 连接池 | PgBouncer | `ADOPT` | transaction pool 只服务无 session state 的短事务；migration、LISTEN、session lock 直连/专用 pool |
| 自建 PostgreSQL 备份/PITR | pgBackRest 或证据等价工具 | `CONDITIONAL`（自建必选一种） | backup/WAL/restore 工具，不是事实源；必须隔离 restore 和实测 RPO/RTO |
| 自建 PostgreSQL HA | Patroni 或证据等价方案 | `CONDITIONAL`（自建多机） | Patroni 管理 PostgreSQL/leader 生命周期，不能与另一个 postgres manager 并存；DCS 故障和 fencing 单独资格化 |
| 托管 PostgreSQL | 云/组织托管 HA | `CONDITIONAL` | 只有可验证 PITR/failover/export/退出、TLS/identity、RPO/RTO 和数据驻留满足时替代 Patroni/pgBackRest |
| 人类身份 | 现有企业 OIDC IdP；无现成时评估 Keycloak/等价 | `ADOPT interface` / implementation `CONDITIONAL` | IdP 认证身份；Go 拥有版本化 scope mapping、maker-checker 和业务授权；不自建密码/session 目录 |
| 应用 telemetry | OpenTelemetry SDK + OTLP | `ADOPT` | 标准传播/导出；业务 audit/fact 不进入 trace 真相 |
| telemetry pipeline | 最小 OpenTelemetry Collector distribution | `ADOPT` with exact components | 只接收/批处理/脱敏/导出；component 稳定性逐项冻结，失败不阻塞核心 |
| 指标/告警 | Prometheus + Alertmanager | `ADOPT` | 低基数运维指标和通知路由；silence/ack 不是 Incident/Effect/authorization |
| 运维可视化 | Grafana | `ADOPT` read-only | 聚合 dashboard、state timeline、只读 link；禁用 Action/API mutation、核心 DB write 和 per-rule 高基数事实 |
| 插件统计数据语义 | OpenTelemetry Metrics data model + Grafana data frame pattern | `ADOPT pattern only` | 借鉴 metric/temporality/monotonicity 与 typed frame；合同、run、quality、projection 由 MASI 拥有，不部署第二 metrics store/query control plane |
| 插件统计 Web 映射 | ECharts `dataset`/`encode` | `ADOPT internal renderer` | 仅 Web 内部把 Go-normalized 数据映射到固定组件；插件不得提交 vendor option、formatter、expression 或浏览器代码 |
| 日志/trace backend | Loki/Tempo/组织平台 | `CONDITIONAL` | 有容量/合规/运维证据再选；PostgreSQL audit 与 rule facts 不迁入其中 |
| SBOM | Syft | `ADOPT` | 从 source/image/filesystem 生成锁定格式 inventory；SBOM 不证明漏洞、许可证或功能资格 |
| 漏洞扫描 | Trivy（或经同矩阵选定一种 scanner） | `ADOPT` 一种 | 锁定 scanner 与 database snapshot/freshness；结果进入资格，不自动删除/回滚 runtime |
| OCI 签名/验证 | Cosign/Sigstore 或组织 PKI | `ADOPT` | 绑定 exact digest/publisher/provenance/offline bundle；签名不替代 qualification/activation/revocation |
| 前端通用能力 | Vue/Router/Pinia/Element Plus/ECharts/TanStack/Playwright 等 | `ADOPT` per ADR-0007 | 复用 primitives/tooling；MASI P4/effect/governance 组件 clean-room 实现 |
| 本地/CI 正式 E2E 服务编排 | Docker Compose `e2e-runner-compose/v1`；Playwright 只作浏览器驱动 | `ADOPT` one exact runner profile | Compose 用显式 healthcheck/`service_healthy`、one-shot migration 和有界 teardown 启动真实服务；Playwright `webServer` 不替代系统编排。runner 不拥有业务 readiness、事实、oracle 或 effect |
| 三机/生产单域编排 | systemd/Podman/受控 deployment asset | `ADOPT` profile-specific | 与本地/CI E2E runner 分开资格化；只管理进程/容器，不拥有 readiness、事实或 effect；配置/secret/digest 可审计 |
| Kubernetes/Helm/operator | 组织已有平台时 | `CONDITIONAL` | 需要生产规模/平台证据；不能把 CRD/GitOps 状态当核心事实或提前强制首期依赖 |
| 故障注入/代理 | Toxiproxy、网络 namespace、target-specific fault tool 等候选 | `CONDITIONAL` test-only | 固定版本/seed/注入点；不能进入 production dataplane 或替代 fault evidence schema |

## 明确拒绝的首期用途

| 候选/模式 | 被拒用途 | 原因 |
|---|---|---|
| Kafka、NATS、Redis Streams | Event/effect canonical queue、cursor 或 operation result | 形成第二 durable fact/queue；当前 PostgreSQL outbox/CAS 足够，尚无容量证据 |
| Redis | lock、idempotency truth、P4 result、rule current state | 丢失后将改变正确性，违反 `DB-REDIS-001` |
| Temporal、Argo Workflows、通用 BPM/workflow engine | effect governance/execution 或 Analysis 外层业务状态机 | 复制 proposal/decision/intent 或 legacy Workflow，增加第二可执行状态机 |
| ONOS、Stratum controller path、P4Runtime Shell daemon 或厂商 controller | MASI 规则生产 writer/scheduler | 产生第二 P4 session/write ownership；Edge 的 journal/generation/readback 无法保持唯一。Stratum 仅可按上表作为 target-side stack |
| gNMI `Set`、gNOI、SSH/CLI automation | 首期设备 mutation/control path | 增加与 P4 effect 平行的副作用、凭据、授权与回滚链；只读 profile 不授权写 |
| NetBox/CMDB webhook、Ansible/Nornir inventory | 自动覆盖 canonical target current/assignment 或 wave membership | 外部 intended state 不带 MASI fence/readback/authorization，自动生效会形成第二事实源/调度器 |
| Grafana Actions、webhook auto-remediation | rule disable/delete、effect proposal/decision/intent | dashboard/告警没有业务 scope、freshness、maker-checker 和 CAS 语义 |
| Alertmanager silence/ack | Incident close、rule effective、authorization decision | 通知状态不是业务事实，且可被外部路由规则改变 |
| OPA/Casbin 等通用授权状态库 | 首期替代 Go effect risk/scope/maker-checker | 当前合同小且安全关键；再引入 policy state 会产生双语义。未来只有规则规模/共享需求证据后评估为纯决策 adapter |
| Dapr/full service mesh | 首期核心 runtime/control plane | 当前边界数量有限；引入 sidecar/控制面扩大延迟、证书、升级和故障面。可以借鉴模式，不整体依赖 |
| 1Panel/sub2api/full Grafana frontend fork | MASI 业务控制台 | API、权限、状态、许可证和品牌不匹配；遵循 ADR-0007 的基础库复用/clean-room 实现 |
| 任意“自动发现并安装” marketplace/plugin | production dependency acquisition | 违反私有 catalog、digest、qualification、offline 和 capability 门禁 |
| Backstage runtime extension、Vega spec、ECharts option、HTML/JS UI plugin | 插件统计结果展示 | 扩大浏览器执行/表达式、CSP、供应链和兼容面；只借鉴类型化扩展思想，首期采用封闭 display hints |
| KServe/Ray Serve/MLflow/TF Serving serving/control plane，或Triton model-control API | 替代Go Model Manager、Edge route或PostgreSQL binding | 形成第二model lifecycle/router/autoscaler/alias/current；Triton server仅按上表在MOD-INF-001内部执行，`EXPLICIT/POLL`和公开model-control仍拒绝 |
| Beam、Flink、Kafka、通用IPFIX collector | 首期替代Edge source/window/WAL或新增在线流处理层 | 产生第二queue/watermark/scheduler/runtime与网络跳；只借鉴标准flow/event-time语义，当前容量无需整体平台 |
| 全量P4Runtime Digest/PacketIn/clone | 默认逐包生产检测feed | P4Runtime明确best-effort并允许过载丢弃；控制面stream、payload复制与无在途上限不适合作为完整性链 |
| UFW/nftables/iptables | BMv2 dataplane backend、fallback、policy fact 或 outcome oracle | 管理 Linux 内核 packet path，不控制 BMv2；会制造第二副作用/事实路径和测试假 drop。只可由独立运维 profile 保护宿主服务 |
| P4 tutorials Bloom-filter firewall | exact deny/allow enforcement | 官方说明 Bloom collision 可能让 unwanted flow 通过；不能逐 rule exact readback，故只作教学/测试思路参考 |

`REJECT` 针对上述用途，不禁止组件在独立组织系统中的其他用途。例如 Kafka 可以服务与 MASI 核心无关的数据平台，但不得成为本系统正确性依赖或 effect path。

## 关键组件的具体边界

### 1. 契约工具链

[Buf lint](https://buf.build/docs/lint/) 提供结构/风格规则，[Buf breaking](https://buf.build/docs/breaking/) 可将当前 Protobuf 与历史输入比较并报告破坏 client/server/generated code 的变化。项目采用它们作为门禁，但 schema source、兼容 policy 和停止支持日期仍由 `contracts/`/profile 决定。

generator 以 digest-pinned OCI/二进制运行，source、config、plugin 和 output digest 全部记录。任何 generator 默认变化都创建 profile revision；不接受仅因代码“能编译”就通过 wire compatibility。

### 2. PostgreSQL 运行组件

[PgBouncer feature matrix](https://www.pgbouncer.org/features.html) 明确 transaction pooling 会打破依赖 session 的特性，因此只用于短无状态事务；migration、LISTEN 和必要 session feature 不经过该 pool。

[Patroni](https://patroni.readthedocs.io/en/latest/) 是 PostgreSQL HA template，并会管理 PostgreSQL 本身；自建 profile 中不得同时让 systemd/另一个 operator 争夺同一实例。[pgBackRest](https://pgbackrest.org/user-guide.html) 用于 backup/WAL/archive/restore。两者的存在不构成 HA/PITR PASS；必须注入 primary/DCS/storage/network 故障并隔离恢复。

### 3. 可观测性组件

[OpenTelemetry Collector](https://opentelemetry.io/docs/collector/) 提供 vendor-neutral receive/process/export，但其 component 稳定性并不一致。因此项目构建最小、固定 component distribution，限制 queue、batch、retry、memory 和敏感字段；不用任意 contrib component 动态扩展生产权限。

[Prometheus](https://prometheus.io/docs/practices/instrumentation/) 指出每个 label set 都有 RAM/CPU/disk/network 成本，并建议对可能超过 100 cardinality 的维度考虑替代方案。因此 rule ID 保留在 PostgreSQL，而 Prometheus 只收聚合。Alertmanager 仅做 deduplicate/group/route/silence/inhibit；Grafana state timeline/data link 只读展示。Grafana 支持配置会发 API request 的 Action，production 明确不 provision 此能力。

插件业务统计不进入 Prometheus 动态 series。`PluginStatisticsArtifactV1` 借鉴 [OpenTelemetry Metrics data model](https://opentelemetry.io/docs/specs/otel/metrics/data-model/) 的 gauge/sum/histogram、temporality 和 monotonic 语义，以及 [Grafana data frame](https://grafana.com/developers/dataplane/dataframes) 的 typed field/table 形态；Go 仍拥有输入快照、run、校验和 PostgreSQL 投影。[ECharts dataset](https://echarts.apache.org/handbook/en/concepts/dataset/) 只在内建 Web renderer 中使用。Backstage/Vega 等成熟扩展机制证明“类型化扩展点/受限解释器”可行，但不授权本项目接受任意 UI extension/spec；完整边界见 ADR-0018。

### 4. P4 测试工具

[PTF](https://github.com/p4lang/ptf) 是基于 Python `unittest` 的 dataplane test framework，可向指定端口注入/验证包；[P4Testgen](https://github.com/p4lang/p4c/blob/main/backends/p4tools/modules/testgen/README.md) 用 symbolic execution 自动生成 P4 input/control-plane/expected-output tests并可输出 PTF/P4Runtime 形式。二者显著减少自研 packet harness/oracle，但 generated test 不自动代表真实 target 通过。

P4Runtime Shell 是交互式诊断工具且上游仍称 work in progress。它只进入隔离 test/read-only runbook，不进入生产镜像常驻进程，也不持有 writer credential。

Mininet/BMv2与 Linux netem直接复用为隔离软件拓扑、真实 Linux stack和可重复链路扰动，但其 CPU/带宽受单机限制，kernel timer、TSQ和TSO/GSO/GRO会影响 packet/rate语义；exact environment只形成 software-target evidence。

Tcpreplay suite解决 capture字节发送、方向 cache与离线 header rewrite，不建立 TCP/application状态；由于 GPLv3与当前仓库许可证尚未冻结，先按外部 test binary/image条件采用。真实会话使用受控 client/server。TRex保留为未来 hardware high-rate/stateful profile候选，不与 Tcpreplay同时作为首期 BMv2默认发生器；完整取舍见 ADR-0012。

BMv2/p4c 作为首期软件 target/build tool，复用的是 P4_16/P4Runtime 执行与编译机制，不是现成通用防火墙控制面。ADR-0014 的 normalized policy、双 bank/selector、审批、journal/readback/CAS 和规则表现仍由项目拥有。p4-constraints 只有固定版本和正负 golden 通过后才作为补充 preflight；上游 CLI 的 test/experiment 定位与 library defense-in-depth 边界必须保留。[p4-constraints](https://github.com/p4lang/p4-constraints#readme)

官方 P4 tutorial 的 firewall exercise 使用 Bloom filter，并明确碰撞可能让 unwanted flow 通过；它适合教学路径和测试思路，不适合 MASI 的 exact policy/readback。UFW/nftables/iptables 只可独立保护 Linux 宿主，不参与 BMv2 effect 或 action oracle。[P4 tutorials firewall](https://github.com/p4lang/tutorials/tree/master/exercises/firewall#readme)

### 4.1 多 Target 与设备管理组件

[P4Runtime 1.4.1](https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html)提供 target-local runtime 与 primary arbitration，但端口、设备发现、通用 switch configuration 和跨设备事务不属于它的完整职责；因此项目复用协议，不引入第二 controller，并在 Go/Edge 中分别保留 Target Registry/Fleet Coordinator 与 per-target actor 所有权。

[OpenConfig gNMI](https://openconfig.net/docs/gnmi/gnmi-specification/)的 `Capabilities/Get/Set/Subscribe` 能补充模型化设备状态，但首期只资格化 `Capabilities/Get/Subscribe` 的 exact model/path allowlist，且资源优先级低于 P4 mastership/effect/telemetry/rule observation。`Set` 与 [gNOI](https://github.com/openconfig/gnoi) 的系统/证书/OS/诊断 mutation 都需要新需求基线，不能复用 P4 授权暗中开启。

[Stratum](https://github.com/stratum/stratum)可作为 target-side P4Runtime/gNMI 实现候选；[ONOS](https://github.com/opennetworkinglab/onos)等完整 controller 作为生产 writer 会复制 topology/intent/session 所有权，故拒绝。[NetBox](https://netbox.readthedocs.io/en/stable/introduction/)适合作为 intended inventory candidate，不是 runtime current；[Ansible Network](https://docs.ansible.com/projects/ansible/latest/network/getting_started/)与[Nornir](https://github.com/nornir-automation/nornir)只用于离线、测试或只读自动化。完整边界与故障/性能矩阵见 ADR-0015。

### 5. 软件供应链

[Syft](https://oss.anchore.com/docs/guides/sbom/) 负责从 image/filesystem 生成 SPDX/CycloneDX 等 SBOM；[Trivy](https://trivy.dev/docs/latest/guide/) 作为一个候选扫描 image/filesystem/repository/SBOM 的 vulnerability/misconfiguration/secret/license 范围；项目只选并锁定所需 scanner/DB 能力，避免同时维护多套相同 gate。[Cosign](https://docs.sigstore.dev/cosign/signing/signing_with_containers/) 对容器 digest 进行签名/attestation，并按既有 ADR 验证 publisher policy/offline bundle。

SBOM、scan、signature 是三类证据：inventory、已知风险检测、来源/完整性。任何一个都不能替代另外两个或功能/恢复/性能/许可证资格。

### 6. 在线模型运行、Central Inference 与登记模式

[ONNX IR](https://onnx.ai/onnx/repo-docs/IR.html) 提供 `model_version` 和唯一键的 `metadata_props`；[ONNX Runtime C++ ModelMetadata](https://onnxruntime.ai/docs/api/c/struct_ort_1_1_model_metadata.html) 可以读取这些字段。项目使用它们交叉核对实际加载模型，但 external `contracts/model/v1` manifest/digest/qualification 仍是合同来源；模型 metadata 由生产者控制，不能证明发布者身份、安全或已激活。

[MLflow Model Signatures](https://mlflow.org/docs/latest/ml/model/signatures/) 展示 tensor dtype/shape 输入输出合同，[Model Registry workflow](https://mlflow.org/docs/latest/ml/model-registry/workflow) 展示 immutable version、tag 和可重指 alias。项目采用 signature/version 思路，但生产 binding 永远保存 resolved exact digest；`champion`/stage/alias 只可作为离线候选发现，不在 runtime 解析。

[ONNX Runtime Session API](https://onnxruntime.ai/docs/api/c/struct_ort_1_1_session.html) 显示 C++ Session 从模型文件/内存构造；[execution providers](https://onnxruntime.ai/docs/execution-providers/)提供不同计算设备的统一执行抽象，并按优先级把节点分配给可执行它的provider；[graph optimization](https://onnxruntime.ai/docs/performance/model-optimizations/graph-optimizations.html)说明Session创建时online optimization会增加启动成本，而offline optimized model可降低成本但受execution provider/options/hardware兼容约束；[thread management](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)暴露intra/inter-op、execution mode、spinning、affinity/NUMA与global/per-session pool。项目因此把CPU和CUDA分别纳入exact profile，把optimization mode/artifact与thread/arena固化；CUDA profile中的host-side节点必须预先声明/读回/测量，运行期新CPU接管视为drift。不把optimized artifact当跨设备通用bundle，也不接受默认漂移、自动选择或silent fallback。

[NVIDIA Triton model management](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html)将`NONE`定义为启动时尝试加载repository全部模型并忽略后续变化，另有运行期`EXPLICIT/POLL`。项目采用`NONE`，所以snapshot必须是exact binding依赖闭包；并参考其[secure deployment](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/deploy.html)由MASI profile强制strict readiness、接口隔离、Gateway mTLS和只读目录，而不把这些说成Triton默认保证。模型变更启动隔离new pool generation，先warm/readback/资格化，再由Edge逐shard route-withdraw/drain/WAL、PG CAS、commit/resume；不开放Triton model-control。

Triton的[model configuration](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_configuration.html)支持CPU/GPU instance group，且禁用auto-complete后仍可能补缺失的instance-group默认，因此项目显式固定kind/count/device；官方[build customization](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/build.html)允许构建不含GPU支持的制品，项目据此交付独立digest-pinned CPU/CUDA artifact，而不是运行时猜测环境的万能镜像。Kubernetes[GPU scheduling](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/)、[topology spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)和[PDB/disruptions](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)可复用为CPU/GPU资源与跨域placement；但Ready/PDB/Service/PreStop均不能证明exact binding、selected profile、N+1、drain或current。single profile是一个故障域内1..N副本且不具备HA。Edge唯一route与Go/PostgreSQL readback/CAS继续拥有项目语义；KServe、Ray Serve、TensorFlow Serving和MLflow不作为首期runtime/control plane。

### 7. 在线遥测与中央推理热路径

[P4Runtime 1.4.1](https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html)明确DigestListAck不是可靠传输机制，server可在数据面速率超过自身/channel/client能力时丢digest。项目因此直接复用官方proto/client和target aggregate extern能力，但自行定义bank/epoch/snapshot、source WAL、coverage与window合同；Digest/PacketIn只保留补充用途。

[Linux Packet MMAP](https://docs.kernel.org/networking/packet_mmap.html)提供共享ring、TPACKET_V3 block和fanout，是需要镜像包头时比逐包`recv`更合理的兼容profile。[AF_XDP](https://docs.kernel.org/networking/af_xdp.html)提供per-queue XSK/UMEM/SPSC rings，但默认可能从zero-copy回退到copy；[Suricata AF_XDP guide](https://docs.suricata.io/en/latest/capture-hardware/af-xdp.html)也强调XDP_DRV、queue/thread、NUMA和专用接口。因此两者按证据逐级条件采用，不把AF_XDP/DPDK强制进aggregate-first首期。

[IPFIX](https://www.rfc-editor.org/rfc/rfc7011.html)的Observation Domain/Point、flow key、sequence/time和typed Information Element用于项目contract设计；Beam/Flink的event-time/watermark/lateness只作模式来源。部署它们的collector/runtime会复制Edge source/window/WAL ownership，故首期拒绝整体引入。

Edge↔Gateway采用项目`inference-central-grpc-batch/v1`，复用gRPC/Protobuf/TLS但自行拥有request/pool/attempt、quota/retry/dedupe/WAL/ACK语义。[gRPC performance](https://grpc.io/docs/guides/performance/)建议复用channel，且长stream建立后不能重平衡，因此首期使用batched-unary。[ONNX Runtime I/O Binding](https://onnxruntime.ai/docs/performance/tune-performance/iobinding.html)按exact CUDA profile采用，CPU profile另冻结thread/NUMA/arena；[Triton batching](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/batcher.html)是唯一delay batch scheduler。任何优化都不能替代actual-copy、network、WAL/ACK、真实服务端到端和crash evidence；完整现行取舍见ADR-0017，ADR-0013的本机SPSC部分已被替代。

## 源码复用政策的全系统推广

| 类型 | 默认 | 处理 |
|---|---|---|
| 包管理器/官方二进制/OCI 的独立通用组件 | 优先 | exact version/digest、lock、license、SBOM、profile、black-box/fault/perf |
| permissive-licensed 小型独立源码/fork | 条件 | Owner 审批、逐文件 provenance/修改/NOTICE/test/update/exit |
| 成熟应用的架构/交互/运维思想 | 可参考 | 用 MASI 契约 clean-room 实现并记录设计来源 |
| 应用控制面、业务状态机、品牌/页面、未清晰许可 snippet | 默认拒绝/HOLD | 只有项目许可证、法律/安全/工程审查和持续义务全部可满足后例外 |

复用的优先级是“依赖公开边界 > 薄 adapter > 受控 vendoring > clean-room 参考 > 自研通用轮子”。安全关键 MASI 逻辑不是通用轮子：effect canonicalization、risk/maker-checker、generation/fence、journal/readback/CAS 和 rule evidence semantic 必须由项目拥有。

## 性能、稳定性与兼容性影响

- 性能：连接池、批处理 Collector、成熟 codegen/test tooling 降低自研成本；每个 runtime component 仍需以关闭基线比较 p99/RSS/queue，不以成熟度豁免 benchmark。
- 稳定性：HA/backup/alert routing 使用成熟实现，但故障必须被注入；组件自身 dashboard 或 health 不能代替端到端 restore/readback。
- 兼容性：所有上游版本固定在 profile；current/previous matrix 和 exact digest rollback 优先于 `latest`。managed/self-hosted、systemd/Kubernetes 是 deployment adapter，不改变合同。
- 安全：最小 identity/capability、offline artifact/database、无 production runtime 下载；组件供应链故障使资格 `HOLD`，不自动改变核心事实或设备。

## 取舍

收益：显著减少通用基础设施自研；把维护和协议兼容交给成熟项目；保留清晰替换点；避免通过成熟产品意外复制业务事实和权限。

代价：需要维护 component registry、lock/SBOM/NOTICE、最小发行版、适配器、fault/performance/upgrade matrix；某些组织已有平台仍需做资格化；条件组件会产生多个 deployment profile。

## 被拒绝的方案

1. “能买/能装就全部外包”：核心不变量会分散到多个控制面。
2. “所有东西自己写”：重复实现高风险运维和协议工具，长期维护性差。
3. 只看 GitHub star/下载量/官方镜像：不能证明许可证、当前维护、安全、性能或项目兼容。
4. 只生成 SBOM：不能发现全部漏洞、许可证冲突或运行权限越界。
5. 让成熟组件失败阻断核心：observability、dashboard、scanner 等非核心组件必须可降级。
6. 同时保留两种等价工具“以后再选”：扩大 supply chain、配置和资格矩阵；每种能力首期固定一种 profile。

## 迁移与回滚

greenfield 先建立 `contracts/supply-chain/v1` 和 deny-by-default CI policy，再逐项加入组件。现有文档候选不自动成为依赖；只有目标模块启动时才创建 lock/config/image，并完成 Module 门禁。

回滚按 exact component/config/database/profile digest。任何组件无法安全回滚时关闭对应非核心能力或保持 module `HOLD`；不得恢复第二 writer/queue、丢弃当前 PostgreSQL facts、放宽 TLS/authorization 或从公网拉取旧版本。

## 验证

- registry/inventory/lock/SBOM/license/provenance/source-output 一致性；
- 每个组件完全不可用、慢、满、漂移、升级/回滚/撤销与替换；
- PgBouncer session-feature 负例、Patroni failover、pgBackRest PITR/隔离 restore；
- Collector/Prometheus/Alertmanager/Grafana 失效不阻塞核心、无高基数/无 mutation；
- 统计 conformance plugin 只经 Go frozen input 与 typed Host/direct adapter 运行；Prometheus/OTel/Grafana 不成为其 canonical store，Web 只执行内建 renderer，unknown display/vendor spec/browser code 稳定拒绝；
- PTF/P4Testgen 提升 packet/P4 coverage，Shell 无 production writer；
- P4Runtime/gNMI/Stratum/NetBox/Ansible/Nornir/ONOS 按 ADR-0015 的 target-side、candidate-only、read-only/offline 与 second-writer negative 边界验证；1/2/N target、assignment handoff、slow-target isolation 和 parent/child/wave 不改变唯一 effect queue；
- BMv2/p4c/p4-constraints 按 exact firewall profile 运行，Bloom tutorial 与 UFW/nftables/iptables 不能成为 enforcement backend/fallback/fact/oracle；host-filter contamination 负例必须阻断 PASS；
- Mininet/netem/Tcpreplay/TRex按 `traffic-replay/v1` 的 software/hardware、L2/live-session、许可证和唯一 P4 owner边界运行；sender统计不提升 DUT/outcome资格；
- Syft/Trivy/Cosign 的独立证据与 offline freshness；
- Triton/ORT CPU与CUDA的exact repository/wire/optimization/runtime/hardware/dynamic-batch/startup profile、selected/observed envelope、model-control incarnation、Edge唯一route、pool generation、single/HA availability、deployment action/capacity、TensorRT/OpenVINO显式条件profile、自动fallback rejection、MLflow offline-only和KServe/Ray/TF Serving control-plane rejection均由依赖/部署policy负向验证；
- P4 aggregate/Digest/PacketIn、PACKET_MMAP/AF_XDP/DPDK、IPFIX/Beam/Flink pattern、central gRPC、CPU thread/NUMA、CUDA I/O Binding和Triton batching按ADR-0017的采用层级、actual mode、coverage/drop/network/copy、WAL/ACK、HA profile的跨域same-profile N+1、old-new与hardware matrix验证；关闭条件组件不改变Edge唯一source/window/router或canonical Event；
- 正式Module/pairwise/system E2E检查实际service inventory和公开边界；fake/mock、readiness、microbenchmark、rehearsal或成熟组件自身health不得产生MASI PASS；CPU/CUDA evidence不得互相继承；
- 被拒组件/用途无法经 direct、transitive、sidecar、plugin 或 deploy chart 偷渡；
- 全部结果按 ADR-0006 分开记录 `level/applicability/result/qualification` 与 exact claim scope；`REHEARSAL/NOT QUALIFIED`、`MODULE PASS` 等只可由这些字段推导显示。

## 参考

- 专项调研：`../research/rule-effectiveness-and-system-reuse-assessment-2026-08-10.md`
- 在线模型模块化评估：`../research/online-model-modularity-assessment-2026-08-10.md`
- v1.13 central-GPU 历史评估：`../research/central-gpu-inference-architecture-assessment-2026-08-11.md`
- 当前CPU/CUDA与真实服务E2E评估：`../research/central-inference-cpu-cuda-and-real-e2e-assessment-2026-08-12.md`
- 历史本机模型 rollout 决策：`0011-startup-bound-model-selection-and-rolling-restart.md`
- P4 流量生成与回放决策：`0012-bmv2-p4-traffic-generation-and-replay.md`
- 在线遥测与推理热路径决策：`0013-online-telemetry-and-inference-hot-path.md`
- BMv2 无状态防火墙决策：`0014-bmv2-stateless-firewall-policy-and-activation.md`
- 多 target/fleet 与设备管理决策：`0015-multi-target-p4-fleet-and-device-management-boundary.md`
- v1.13 历史 Central GPU 推理池决策：`0016-central-gpu-inference-pool-and-routing-boundary.md`
- 当前 CPU/CUDA 启动选择与真实服务 E2E：`0017-central-inference-runtime-selection-and-real-e2e.md`
- 插件统计与声明式 Web 投影：`0018-plugin-statistics-and-declarative-web-projection.md`
- 插件统计专项调研：`../research/plugin-statistics-and-declarative-web-assessment-2026-08-12.md`
- 多 target 成熟方案专项调研：`../research/multi-target-p4-fleet-management-assessment-2026-08-11.md`
- Buf lint/breaking：<https://buf.build/docs/lint/>、<https://buf.build/docs/breaking/>
- PgBouncer：<https://www.pgbouncer.org/features.html>
- pgBackRest：<https://pgbackrest.org/user-guide.html>
- Patroni：<https://patroni.readthedocs.io/en/latest/>
- OpenTelemetry Collector：<https://opentelemetry.io/docs/collector/>
- Prometheus/Alertmanager：<https://prometheus.io/docs/practices/instrumentation/>、<https://prometheus.io/docs/alerting/latest/alertmanager/>
- Grafana state timeline/data links：<https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/state-timeline/>、<https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/configure-data-links/>
- OpenTelemetry Metrics data model：<https://opentelemetry.io/docs/specs/otel/metrics/data-model/>
- Grafana data frames：<https://grafana.com/developers/dataplane/dataframes>、<https://grafana.com/developers/dataplane/timeseries/>
- ECharts dataset：<https://echarts.apache.org/handbook/en/concepts/dataset/>
- PTF/P4Testgen/P4Runtime Shell：<https://github.com/p4lang/ptf>、<https://github.com/p4lang/p4c/blob/main/backends/p4tools/modules/testgen/README.md>、<https://github.com/p4lang/p4runtime-shell>
- P4Runtime/gNMI/gNOI/Stratum/ONOS：<https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html>、<https://openconfig.net/docs/gnmi/gnmi-specification/>、<https://github.com/openconfig/gnoi>、<https://github.com/stratum/stratum>、<https://github.com/opennetworkinglab/onos>
- NetBox/Ansible Network/Nornir：<https://netbox.readthedocs.io/en/stable/>、<https://docs.ansible.com/projects/ansible/latest/network/getting_started/>、<https://github.com/nornir-automation/nornir>
- BMv2/p4c backend/p4-constraints/P4 firewall tutorial：<https://github.com/p4lang/behavioral-model#readme>、<https://p4lang.github.io/p4c/behavioral_model_backend.html>、<https://github.com/p4lang/p4-constraints#readme>、<https://github.com/p4lang/tutorials/tree/master/exercises/firewall#readme>
- Syft/Trivy/Cosign：<https://oss.anchore.com/docs/guides/sbom/>、<https://trivy.dev/docs/latest/guide/>、<https://docs.sigstore.dev/cosign/signing/signing_with_containers/>
- ONNX IR/ONNX Runtime metadata：<https://onnx.ai/onnx/repo-docs/IR.html>、<https://onnxruntime.ai/docs/api/c/struct_ort_1_1_model_metadata.html>
- ONNX Runtime Session/execution providers/threading/graph optimization：<https://onnxruntime.ai/docs/api/c/struct_ort_1_1_session.html>、<https://onnxruntime.ai/docs/execution-providers/>、<https://onnxruntime.ai/docs/performance/tune-performance/threading.html>、<https://onnxruntime.ai/docs/performance/model-optimizations/graph-optimizations.html>
- P4Runtime Digest/IPFIX/PACKET_MMAP/AF_XDP/circular buffers：<https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html>、<https://www.rfc-editor.org/rfc/rfc7011.html>、<https://docs.kernel.org/networking/packet_mmap.html>、<https://docs.kernel.org/networking/af_xdp.html>、<https://docs.kernel.org/core-api/circular-buffers.html>
- ONNX Runtime I/O Binding与Triton batcher：<https://onnxruntime.ai/docs/performance/tune-performance/iobinding.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/batcher.html>
- MLflow signatures/registry：<https://mlflow.org/docs/latest/ml/model/signatures/>、<https://mlflow.org/docs/latest/ml/model-registry/workflow>
- Triton model repository/optimization/security：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_repository.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/optimization.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/deploy.html>
- Triton model management/configuration/build、KServe control plane、Kubernetes GPU/topology/disruptions：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_configuration.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/build.html>、<https://kserve.github.io/website/docs/concepts/architecture/control-plane>、<https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/>、<https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/>、<https://kubernetes.io/docs/concepts/workloads/pods/disruptions/>
- gRPC performance/retry/deadline：<https://grpc.io/docs/guides/performance/>、<https://grpc.io/docs/guides/retry/>、<https://grpc.io/docs/guides/deadlines/>
- Docker Compose 启动顺序/healthcheck 与 Playwright `webServer`：<https://docs.docker.com/compose/how-tos/startup-order/>、<https://playwright.dev/docs/test-webserver>
