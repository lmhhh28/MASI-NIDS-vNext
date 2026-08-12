# 规则有效性展示与全系统成熟方案复用独立评估

- 初始评估日期：2026-08-10；2026-08-11 按 BMv2 无状态防火墙决策复核
- 评估范围：MASI-NIDS vNext 规则安装/命中/结果观测、前端表达、P4/Edge/Go/PostgreSQL 数据链及全系统成熟组件复用
- 需求基线：原始规则表现评估形成于 v1.6，BMv2 firewall 复核形成于 v1.11；当前基线 `vNext-requirements-1.17`
- 性质：来源驱动的独立工程评估，不替代需求基线或 ADR
- 实现状态：当前仓库仍处初始化/文档阶段；本文不构成 `PASS`、E2E 或生产资格证据

## 结论先行

1. 应当增加规则表现工作台，但不应展示一个含糊“生效率”。正确模型是“安装确认—数据面命中—结果验证”三层，外加原 effect execution 状态。
2. P4 per-entry direct counter 是首期在线命中证据的最佳基础：数据面原生、可批量读取、无需逐包上送；它只证明 entry/action 的计数路径执行，不能证明 drop/forward 成功或攻击结束。
3. 百分比只有在同 target/table/stage/generation/window 存在 eligible-ingress denominator 时才成立。否则应展示 packet/byte delta 与 pps/bps，不应虚构 0% 或“无效”。
4. per-rule identity/history 应保存在 PostgreSQL，经 Go 授权 API 展示；Prometheus 只保存低基数聚合，Grafana/Alertmanager 只做只读运维呈现和通知。
5. mature reuse 的正确策略不是 fork 大型产品，而是复用标准/工具/primitive：Buf/protoc、PTF/P4Testgen、PgBouncer/pgBackRest/Patroni、OTel/Prometheus/Alertmanager/Grafana、Syft/Trivy/Cosign 和现有前端基础库。
6. 必须拒绝会复制核心所有权的“成熟方案”：第二 P4 controller、第二 effect/workflow queue、Grafana auto-remediation、第三方应用控制面和通用授权状态库。
7. 对 BMv2 防火墙，成熟方案应复用 p4c/P4Tools/PTF/P4Testgen 和 BMv2 软件 target；不能把 UFW/nftables/iptables 当 P4 后端，也不能采用存在 Bloom collision 的教学 firewall 作为 exact enforcement。长期 baseline 用双 bank/selector，临时 response 用独立 overlay，规则表现只统计 current active source。

## 1. “规则生效”实际包含哪些问题

| 人类问题 | 可验证事实 | 不足时的正确显示 |
|---|---|---|
| 规则下发了吗？ | intent/attempt + 当前 generation/pipeline exact canonical readback | `mismatch/stale/expired`，不是命中 0 |
| 规则被包选中过吗？ | 绑定 entry/action 的 direct counter 正 delta | `no hit/no traffic/not measurable/invalid` 分开 |
| 命中了多少？ | packet/byte cumulative、window delta、pps/bps | gap/reset 时不计算窗口值 |
| 占可匹配流量多少？ | 同 table/stage 的 eligible counter | 无 denominator 时不显示百分比 |
| action 真执行正确了吗？ | 独立 packet oracle/action telemetry | `not_measurable`，不能由 hit 推定 |
| 风险已降低了吗？ | 需要更长时间的 Incident/业务/反事实分析 | 只能作为另一个分析结论，不能称规则事实 |

独立判断：把最后两项混进 hit rate 是最危险的设计错误。规则可能命中但 action 参数错误；也可能从未命中，只因为窗口内没有 eligible traffic；还可能成功阻断单个探测包，却不能证明真实攻击已经终止。

## 2. 官方 P4 语义核验

### 2.1 Direct counter 读取

[P4Runtime 1.4.1 §9.1.7](https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html) 的关键事实：

- direct counter 可以通过 `DirectCounterEntry` 读取，也可随 `TableEntry` 读取；只需要计数时前者通常更高效；
- client 可以请求 exact `DirectCounterEntry`；若改用 `TableEntry` 路径，则只有请求中设置 `counter_data` presence，响应才包含目标计数；
- INSERT 未设置 counter 初始值时为 0，MODIFY 未设置时不改变 counter；但项目恢复/兼容仍应实际 Read 建 baseline，而不是让数据库假设 target 当前为 0；
- 一个 table 绑定 direct resource 不代表所有 action 都执行它；对不执行 resource 的 action 写 direct-resource config 会报错；
- idle timeout 的“hit”是 entry 被 packet lookup 选中，notification/时间为 best effort，与精确 packet/byte delta 不是同一证据；
- Read 可以返回多条/分片结果，client 必须按 entity identity 处理，不能把返回顺序当全局 snapshot。

[PSA 1.2](https://p4lang.github.io/p4-spec/docs/PSA.pdf) 将 direct counter 更新绑定到当前 matching entry 的 counter 执行；target 可以对溢出采用 wrap 或 saturate。项目初期是 BMv2/v1model，不能把 PSA 文字直接当目标资格，但它说明了必须在具体 architecture/target profile 中冻结行为。

### 2.2 安装、命中、结果的证据强度

```text
Write ACK
  < exact TableEntry readback
  < direct-counter positive delta
  < independent packet/action outcome oracle
  < longitudinal Incident/business effectiveness analysis
```

箭头表示回答的问题更接近“结果”，不是说后一证据能替代前一证据。比如 packet oracle 验证某个受控包被 drop，不自动证明 production rule 当前仍存在；因此 UI 保留多个维度而不折叠成一个状态。

### 2.3 Instrumentation 设计

- effect table 的每个可执行 action 明确调用 per-entry direct counter；资格测试覆盖每个 action，防止“部分 action 永远不计数”。
- eligible counter 放在该 table 的同一观测点之前/周边，并冻结哪些 packet 属于 eligible；跨 table/stage 的总 ingress 不能当通用 denominator。
- packet 与 byte 同时记录；byte 的 L2/L3/CRC/metadata 口径以 target profile 为准。
- rule counter 不周期 reset。使用累积 Read + observation epoch 计算 delta；pipeline/restart/revision/reset 建新 baseline。
- P4 logical entry equality 排除 target-owned current counter；否则每次命中都会让 readback digest“漂移”。

## 3. 可展示指标的精确定义

### 3.1 在线指标

| 指标 | 精确定义 | 推荐 UI 名称 |
|---|---|---|
| cumulative packets/bytes | 当前 epoch target 累积读数 | 累积命中包/字节 |
| window delta | 两个连续有效 cumulative sample 差值 | 窗口命中包/字节 |
| packet/byte rate | delta / valid elapsed seconds | 命中速率 pps/bps |
| packet match ratio | rule packet delta / same-point eligible packet delta | 匹配占比 |
| same-table hit share | rule delta / comparable rules delta sum | 同表命中份额 |
| observable rule utilization | hit rules / validly observed active rules | 可观测规则利用率 |
| installation confirmation ratio | exact readback / expected active | 安装确认率 |
| outcome verification coverage | 有独立 oracle 的 rule/operation 数量与范围 | 结果验证覆盖，不称成功率 |

公式必须和 numerator、denominator、coverage、window、generation、quality 一起返回。只返回 `42%` 无法复核，也无法区分 scope/filter 变化。

### 3.2 不应计算的情况

- eligible denominator 不存在或为 0；
- previous/current sample 不同 generation、pipeline、rule revision 或 reset epoch；
- counter 回退但无法证明 wrap；
- saturation 后 target 无法提供额外计数；
- sweep gap、timeout、duplicate conflict、clock step 或 window overlap；
- rule 已 expiry/rollback/supersede；
- wildcard/priority/default/multi-table 数据被混在一个 denominator；
- sampled counter 与 full counter 未携带 estimator、sample rate 和 error bound。

这些情况返回状态和原因，不返回 0%。

### 3.3 首次/最后命中的时间精度

轮询发现 counter 从 N 变成 N+k，只能证明命中发生在 `(previous_read_end, current_read_end]`。因此使用 `first_observed_hit_window` 和 `last_observed_hit_window`。除非 target profile 提供经过验证的 timestamp，不应把当前轮询时间写成精确 `last_hit_at`。

## 4. 成熟 NIDS/防火墙的可借鉴模式

| 来源 | 官方事实 | 借鉴 | 不照搬 |
|---|---|---|---|
| [Suricata rule profiling](https://docs.suricata.io/en/latest/performance/rule-profiling.html) | 分开 Checks、Matches 和计算成本；match 可能因 suppression/thresholding 不产生 alert | 匹配与上层结果分层；性能成本另列 | Suricata CPU ticks 不是 P4 hit share |
| [nftables counters](https://wiki.nftables.org/wiki-nftables/index.php/Counters) | per-rule packet/byte，自 reset 起累计；counter 位置影响语义 | 显式 per-entry counter、reset epoch、packet+byte | 不把共享/named counter 当 per-rule；不在观测轮询中 reset |
| [Cisco Policy Analyzer/Optimizer](https://secure.cisco.com/secure-firewall/v7.6/docs/policy-analyzer-and-optimizer) | 提供 top-hit、dead/no-hit、shadowed/redundant/expired/overlap 分析及时间过滤 | Top-N、no-hit lookback、遮蔽/冗余候选 | 不让诊断自动 disable/delete；MASI 仍走 proposal/effect 链 |

独立判断：成熟产品的最大价值在“多维诊断 + lookback + 候选清理”，不是一个万能分数。MASI-NIDS 应先实现可靠计数和状态质量，再做 overlap/dead-rule 静态分析；否则 UI 会精确地展示不可靠数据。

## 5. 前端交互与视觉建议

### 5.1 页面结构

```text
Rule Effectiveness
├─ scope/time/generation/lookback/quality filters
├─ count cards
│  ├─ expected active / readback confirmed
│  ├─ hit observed / no-hit-with-traffic
│  ├─ no eligible traffic
│  ├─ stale / invalid / not measurable
│  └─ independently outcome-verified
├─ Top-N horizontal bars + rate time series
├─ installation/match/quality/outcome state timeline
└─ cursor table → rule/effect/operation/evidence detail
```

### 5.2 推荐图表

- Top-N：水平条形图，默认 20，显示完整单位和窗口；适合比较长 rule label。
- 趋势：line chart 显示 pps/bps，gap 不连线，reset 用注记/断点；允许暂停 live update。
- 状态：state timeline 展示 confirmed/hit/no traffic/stale/invalid/outcome 变化。[Grafana state timeline](https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/state-timeline/) 的模式适合状态随时间变化，但 MASI SPA 应用自有 token/ARIA/表格实现。
- 精确事实：服务端 cursor table；所有图有等价表格/下载，颜色不是唯一编码。

不推荐单一 gauge、3D/pie、把 stale 当 0 的连续线、自动轮播和只给颜色不给状态文本。

### 5.3 操作边界

no-hit/dead-rule 页面只显示“待复核候选”，必须同时展示 eligible traffic、lookback、rule age、TTL、priority/overlap、coverage 和观察质量。没有批量 approve/disable/delete；要改变规则，必须进入新的 proposal builder 并重新获取当前 canonical facts。

## 6. 数据、性能与恢复设计

### 6.1 数据放置

| 数据 | 位置 | 原因 |
|---|---|---|
| per-rule identity/latest/rollup/status/outcome refs | PostgreSQL | 授权、历史、重建和审计 |
| current cumulative sample/queue | Edge bounded memory/queue | 靠近唯一 P4 session；短期传输状态 |
| low-card sweep/status counts | Prometheus | 运维告警/趋势 |
| trace/log | OTel/日志 backend | 调试；不是事实 |
| dashboard | MASI SPA；Grafana 只读聚合 | 业务授权与 per-rule detail 留在 Go |

[Prometheus instrumentation guidance](https://prometheus.io/docs/practices/instrumentation/) 指出每个 labelset 都带来 RAM/CPU/disk/network 成本，并建议对可能超过 100 cardinality 的维度移到其他分析系统。因此 rule ID/match/IP/effect ID 不能做 Prometheus label。

### 6.2 初始资源 profile

- 每 target 最多 4,096 active observable entries；
- 256 entries/Read、响应 1 MiB、deadline 2 秒、每 target 1 in-flight；
- 每 15 秒最多 4 batch，60 秒内 full sweep，75 秒后 stale；
- Edge→Go batch 256/1 MiB，per-target queue 8 batch；
- current projection + status events；5-minute rollup 7 天、hourly 90 天；
- API list 50/200、Top-N 20、图表点数沿用 Web profile。

这是首期可测试值，不是所有硬件的永恒常数。放宽需要 P4 QPS/CPU、Edge/Go RSS、PostgreSQL rows/WAL/index/PITR、API/UI 和 core p99 证据。

### 6.3 失败语义

- 观测组件失败不删除/补写规则，不改变 effect `applied/unknown`；只使 observation stale/invalid。
- reset/wrap 无法区分时建新 baseline；不猜 delta。
- queue 满记录 sequence gap；恢复第一样本只建 baseline。
- generation/pipeline/revision 改变关闭旧 epoch；late sample 只审计。
- database/Grafana/Prometheus 失败不让 Edge 阻塞 effect readback。

## 7. 全系统成熟组件复用评估

### 7.1 直接复用（按 exact profile 资格化）

| 领域 | 成熟候选 | 复用价值 | 必须保留的项目逻辑 |
|---|---|---|---|
| Contract | [Buf lint/breaking](https://buf.build/docs/lint/)、[breaking](https://buf.build/docs/breaking/)、protoc | 不自研 schema lint/break detector/codegen runner | source ownership、compat policy、golden/profile |
| P4 test | [PTF](https://github.com/p4lang/ptf)、[P4Testgen](https://github.com/p4lang/p4c/blob/main/backends/p4tools/modules/testgen/README.md) | packet injection/oracle、symbolic test generation | target profile、effect identity、production Edge ownership |
| DB pool | [PgBouncer](https://www.pgbouncer.org/features.html) | 成熟连接复用 | session feature 路由、transaction/CAS schema |
| Telemetry | [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/) | vendor-neutral receive/process/export、batch/retry/filter | 最小 component profile、资源/脱敏、业务事实 |
| Metrics/alert/UI | Prometheus、[Alertmanager](https://prometheus.io/docs/alerting/latest/alertmanager/)、Grafana | 采集、查询、通知路由、运维图表 | 低基数、无 mutation、Go/PostgreSQL canonical state |
| Supply chain | [Syft](https://oss.anchore.com/docs/guides/sbom/)、[Trivy](https://trivy.dev/docs/latest/guide/)、[Cosign](https://docs.sigstore.dev/cosign/signing/signing_with_containers/) | SBOM、已知风险扫描、digest/publisher verification | qualification、license、revoke、offline policy |
| Frontend | ADR-0007 的 Vue/Router/query/UI/chart/test libraries | 不自研通用 primitive | P4/effect/authorization UX、tokens、a11y |

### 7.2 条件复用

- 自建 PostgreSQL：[Patroni](https://patroni.readthedocs.io/en/latest/) 或等价 HA + [pgBackRest](https://pgbackrest.org/user-guide.html) 或等价 backup/PITR；托管 PG 则使用供应商可验证等价能力。两条 profile 不同时在线管理同一 cluster。
- 身份：优先组织现有 OIDC；没有时再评估 Keycloak/等价。IdP 只认证，Go 继续拥有业务 scope/maker-checker。
- Loki/Tempo、Kubernetes/Helm、service mesh、object storage、Redis read cache：只有容量/组织平台/合规证据触发；不成为首期强依赖或事实源。
- P4Runtime Shell：只做隔离测试/只读诊断。其上游定位是 interactive shell 且仍 work in progress，不适合成为 production daemon。

### 7.3 明确拒绝的用途

- Kafka/NATS/Redis Streams 作为 core/effect queue；
- Temporal/Argo/BPM 作为 effect 或 legacy Workflow 状态机；
- 另一个 SDN/P4 controller、P4Runtime Shell daemon 作为 production writer；
- Grafana Action/webhook/Alertmanager silence 作为 rule mutation/authorization；
- OPA/Casbin 在首期替代 Go risk/scope/maker-checker；
- fork 1Panel/sub2api/Grafana 业务控制面；
- 公网 marketplace/runtime auto-install。
- UFW/nftables/iptables 作为 BMv2 backend/fallback/fact/outcome oracle；
- P4 tutorial Bloom-filter firewall 作为 exact deny/allow；
- p4-constraints CLI、P4Runtime Shell 或其他 controller 作为 production writer。

这些拒绝不是否定组件成熟度，而是其被提议用途会违反 MASI-NIDS 已冻结的唯一所有权。

### 7.4 BMv2 无状态防火墙的复用结论

- `ADOPT`：BMv2 `simple_switch_grpc`/v1model 作为 exact software target；p4c 负责编译，P4Testgen 负责符号路径/输入输出测试生成，具体 target 由 PTF/packet oracle 验证。BMv2 官方明确面向开发/测试而非生产级性能，因此这些 evidence 不提升硬件资格。[BMv2 README](https://github.com/p4lang/behavioral-model#readme)
- `CONDITIONAL`：p4-constraints 仅作 exact profile 的 defense-in-depth CI/preflight；上游把 CLI 定位为测试/实验，不能授权或替代 Edge canonical compiler/readback。[p4-constraints](https://github.com/p4lang/p4-constraints#readme)
- `REJECT`：官方 tutorial Bloom filter 存在 collision，可能让 unwanted flow 通过，不能满足 exact policy/readback。[P4 tutorials firewall](https://github.com/p4lang/tutorials/tree/master/exercises/firewall#readme)
- `REJECT as P4 backend`：UFW/nftables/iptables 操作 Linux host packet path；只可独立做宿主加固或借鉴 counter 语义。firewall 测试必须记录 host filter/eBPF/bridge/qdisc pre/post state，避免假 drop PASS。

规则表现必须绑定 `response_overlay` 或 selector 指向的 current baseline bank。inactive/previous bank 的 counter 不进入 current rollup；bank flip 关闭旧 observation epoch，selector/bank readback与 PostgreSQL CAS 后才建立新 baseline。完整策略/审批/恢复见 ADR-0014。

## 8. 复用不是“复制源码”的判断框架

按长期成本排序：

1. 使用官方协议和 package/OCI 的公开边界；
2. 编写很薄且有 contract test 的 adapter；
3. 只有必要时 vendoring permissive-licensed 小模块，并逐文件登记；
4. 对应用产品只 clean-room 参考 IA/交互/运维思想；
5. 最后才自研没有成熟边界的项目特有逻辑。

项目特有逻辑包括 effect canonicalization、risk/maker-checker、generation/fence、operation journal/readback/CAS、rule evidence quality 和安全关键前端流程。这些不是应当“复用掉”的通用轮子。

## 9. 独立风险复核

| 风险 | 典型误判 | 控制 |
|---|---|---|
| 分母错误 | rule hit / 全网流量 | 只接受同 table/stage eligible denominator |
| 无流量误判 | zero hit = dead | `no_eligible_traffic` 独立状态 |
| counter reset | reset 后巨量/负 delta | epoch、width/mode、ambiguous 时新 baseline |
| 轮询时间伪精确 | sample time = last hit | 保存 observed-hit interval |
| action 因果误判 | hit = drop success | 独立 packet oracle；无证据 not measurable |
| P4 负载 | 每规则逐 RPC | batch、单 in-flight、full sweep/freshness cap |
| DB/TSDB 爆炸 | rule ID metric label | per-rule PG，Prom 只聚合 |
| 自动清理事故 | dead-rule alert 删除规则 | 只读候选，真实变更走治理/effect |
| 成熟组件夺权 | Grafana/Temporal/第二 controller | registry 明确 REJECT 用途、负向架构测试 |
| supply-chain 漂移 | latest image/在线 DB | exact digest、offline bundle/database、inventory/golden |

## 10. 建议实施顺序

1. 同时冻结 `p4-stateless-firewall/v1` 与 `p4-rule-observation/v1`：normalized/compiled policy、response/bank/selector/default/fragment、counter/eligible P4Info、canonical identity、状态、公式、golden 和 PTF/packet oracle。
2. P4 模块独立证明每个 action 的 counter 与 outcome；Edge 实现 bounded cumulative sampler/fence；两者各自先过 Module E2E。
3. Go/DB 实现 epoch/latest/event/rollup 和 bounded API；Web 实现所有状态、公式/coverage、Top-N/timeline/table/a11y。
4. 分别完成 0/128/1,024/4,096-rule fault/performance/soak；没有真实 target 证据时保持 `HOLD/NOT RUN`。
5. 全模块 Module Complete 后，按既定波次做 P4↔Edge、Edge↔Go、Go↔DB、Go↔Web 正式 pairwise，再做规则表现 full E2E。
6. 同时建立 `contracts/supply-chain/v1`，逐项引入 ADR-0009 的 `ADOPT` 组件；不为了填目录或表面成熟提前部署条件组件。

## 11. 文档落点

- 强制需求：`../masi-nids-vnext-system-requirements-2026-08-09.md`
- 规则观测决策：`../adr/0008-rule-effectiveness-observation.md`
- 成熟组件边界：`../adr/0009-mature-component-reuse-boundaries.md`
- P4 fence/readback：`../adr/0004-p4runtime-fencing-preflight-and-cas.md`
- Vue/源码复用：`../adr/0007-vue-soc-console-and-source-reuse.md`
- 来源总登记：`mature-solutions-review-sources-2026-08-10.md`
- BMv2 无状态防火墙决策：`../adr/0014-bmv2-stateless-firewall-policy-and-activation.md`
