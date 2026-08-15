# ADR-0008：规则安装、命中与处置结果的分层观测

- 状态：Accepted
- 日期：2026-08-10
- 决策者：Owner
- 需求基线：`vNext-requirements-1.18`（原始规则观测决策形成于 v1.9）
- 关联需求：`ARCH-003`、`ARCH-TARGET-FLEET-001`、`CONTRACT-P4-001`、`CONTRACT-RULE-001`、`CONTRACT-TRAFFIC-001`、`CONTRACT-TARGET-001`、`CONTRACT-FLEET-EFFECT-001`、`FUNC-EFFECT-001`、`FUNC-RULE-001`、`FUNC-TRAFFIC-001`、`FUNC-TARGET-FLEET-001`、`WEB-RULE-001`、`WEB-TARGET-FLEET-001`、`DB-RULE-001`、`PERF-RULE-001`、`PERF-TRAFFIC-001`、`PERF-TARGET-FLEET-001`、`REL-RULE-001`、`OBS-RULE-001`、`TEST-RULE-001`、`TEST-TRAFFIC-001`、`TEST-TARGET-FLEET-001`、`DEC-024`、`DEC-029`、`DEC-032`

## 背景

用户需要看到下发规则是否真正生效。这里至少存在三种不同问题：规则是否已经安装到当前设备、是否被真实数据包选择、被选择后是否产生了预期 drop/forward/mirror 结果。把三者合成一个“规则生效率”会制造错误安全感。

[P4Runtime 1.4.1](https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html) 允许通过 exact `DirectCounterEntry`，或在请求中设置 `counter_data` presence 的 `TableEntry` Read 读取 direct counter；后一条路径未设置该字段时响应不会返回计数。规范也指出，并非每个绑定 action 都必然执行 direct resource。[PSA 1.2](https://p4lang.github.io/p4-spec/docs/PSA.pdf) 进一步把 direct counter 更新与对应 entry/action 的 `count()` 执行绑定，并允许 target 对溢出采用 wrap 或 saturate。因此，计数器必须先由具体 P4 program/target profile 资格化。

成熟 IDS/防火墙也区分不同含义。[Suricata rule profiling](https://docs.suricata.io/en/latest/performance/rule-profiling.html) 将被检查次数与匹配次数分开，并明确匹配不保证产生 alert；[nftables counter](https://wiki.nftables.org/wiki-nftables/index.php/Counters) 记录自 reset 后的 packet/byte，且 counter 在 rule 中的位置会改变计数语义。这些模式支持“分层证据”，不支持从 hit 直接推导业务成功。

## 决策

### 1. 四个独立维度

每条规则使用四个正交维度，不建立第二 effect 状态机：

| 维度 | 回答的问题 | 可信证据 | 不能证明 |
|---|---|---|---|
| Effect execution | 是否尝试并收敛设备副作用 | intent、claim、journal、P4 readback、PG CAS | 当前规则仍存在、近期被命中 |
| Installation | 当前 target/generation/pipeline 是否存在 exact entry | canonical `TableEntry` readback | 有流量命中、action 正确 |
| Dataplane match | 有效窗口内该 entry/action 是否执行 counter | 同 epoch direct-counter 正 delta | alert、攻击归因、drop/forward 成功 |
| Action outcome | 预期数据面结果是否发生 | 独立 PTF/packet oracle 或已资格化 action telemetry | 攻击已终止、业务风险已消失 |

对外可以按三层简写为“安装—命中—结果”，但底层 effect execution 继续使用既有状态。任何层缺证据都显式显示，不用下一层或上一层补齐。

### 2. P4 观测点与唯一会话所有权

- 需要观测的 effect table 使用 per-entry direct counter；每个可下发 action 必须在 P4 program 中明确是否执行该 counter。P4Info、counter ID/type/width、packet/byte 定义、wrap/saturate/reset、default entry 和 target implementation 进入 `p4-rule-observation/v1`。ADR-0014 的 response overlay 与 active baseline bank 分别携带来源/bank/selector identity；inactive bank counter 不得混入 active effectiveness。
- 若需要百分比，P4 program 还需在同一 table/stage 定义 eligible-ingress counter。没有同点 denominator 时只展示 packets/bytes 与 pps/bps。
- Rust Edge 是唯一生产 P4Runtime StreamChannel owner，也是唯一生产 counter/readback client；多 target 时由对应 `TargetActor` 独占各自 session/queue/source identity。Go、Web、插件、Prometheus、Grafana、PTF 和 P4Runtime Shell 不直接连接生产 target。
- exact effect readback 与统计 counter 分开 canonicalize；target-owned counter 值不进入 entry logical equality digest，防止正常计数变化制造 readback mismatch。
- direct counter 由 observation 只读，不周期清零。INSERT 的初始计数和恢复 baseline 仍以 target profile/实际 Read 为准，不从数据库假设。

### 3. Observation epoch 与数据路径

```text
Go effect operation + exact readback
→ Go 创建并持久化 immutable rule observation identity/epoch
→ Edge bounded direct-counter sweep
→ cumulative sample + sequence/generation/reset metadata
→ Go validation/delta/quality
→ PostgreSQL latest + status event + 5-minute rollup
→ bounded API/SSE
→ Rule Effectiveness UI
```

epoch 由 Go 在 effect exact readback 收敛后创建并持久化；Edge 只消费该 canonical identity 执行采样，不在本地创建第二 epoch 事实。epoch 至少绑定 effect/operation/entity、canonical rule digest、stable target/control incarnation/assignment/actor、device/role、application generation、pipeline/P4Info/capability、table/counter、match/priority/action、TTL 和 profile。任一 identity 变化由 Go 开启新 epoch；late sample 不跨 epoch 合并。

P4Runtime Read 可能分片、重排或返回重复 entity。Edge 和 Go 以 canonical entity identity、sequence 和 digest 去重/排序，绝不以 response order 当样本顺序或把一次 batch 当全设备原子快照。

### 4. 指标定义

| 名称 | 公式/语义 | 必要条件 |
|---|---|---|
| `packet_rate` | `rule_delta_packets / valid_elapsed_seconds` | 同 epoch、连续有效样本、可靠 elapsed time |
| `byte_rate` | `rule_delta_bytes / valid_elapsed_seconds` | 同上且 target byte 语义已资格化 |
| `packet_match_ratio` | `rule_delta_packets / eligible_delta_packets` | 同 target/table/stage/generation/window；denominator > 0 |
| `same_table_hit_share` | `rule_delta_packets / Σ comparable_rule_delta_packets` | 同一明确 rule set；显示 coverage，不能跨 table |
| `observable_rule_utilization` | `hit_observed active rules / validly_observed active rules` | 分母只含当前、可测且窗口完整规则 |
| `installation_confirmation_ratio` | `exact-readback active rules / expected active rules` | 同一 snapshot/version vector，显示 mismatch/stale |

`packet_match_ratio` 是“被该 table entry 选中的 eligible packet 占比”，不是攻击拦截率。`same_table_hit_share` 是同表相对热度，`observable_rule_utilization` 是规则集利用情况，也不是质量分。

denominator 为 0 时返回 `not_observed`；缺失、gap、reset、跨 generation 或 coverage 不完整时不计算。UI/API 共同返回 formula ID、numerator、denominator、window、coverage、precision、quality 和 profile digest，不能只返回格式化百分比。

### 5. Reset、时间与不确定性

- cumulative 下降默认是 `reset_or_wrap/ambiguous`。只有 counter mode/width、前值、最大速率和窗口共同证明单次 wrap 时才计算 wrap delta；saturation 后不能继续推算。
- Edge/P4 restart、pipeline/generation/revision 变化建立新 baseline。断档后的第一条 sample 只恢复 baseline，不生成跨 gap delta。
- 轮询只能证明 hit 发生在相邻 Read 的区间内。因此保存 `first_observed_hit_window` / `last_observed_hit_window`，不伪造精确命中时间。
- P4Runtime idle timeout 的 `time_since_last_hit` 是 best-effort、target-specific 证据，不替代 direct counter；只有进入资格 profile 后才可作为补充。
- `no_hit_observed` 只在 eligible traffic > 0、样本有效、lookback 完整时成立。无流量是 `no_eligible_traffic`，未资格化是 `not_measurable`，故障是 `stale/invalid`。

### 6. 结果验证

PTF/packet oracle 是首期 action outcome 的确定性资格方法：按 `traffic-replay/v1` 对受控 generated packet验证 drop 无预期 egress、forward 的 egress/改写正确、mirror 产生受控副本。P4Testgen 可以生成 input/control-plane/expected-output 用例并提高路径覆盖，但 generated tests 仍需在目标 P4 profile 上执行。curated PCAP用于补充检测/真实包形状覆盖，不能替代最小 oracle；sender accepted、DUT ingress、counter与outcome必须独立观测。

生产实时流量若没有独立 oracle，只展示 `outcome_status=not_measurable`。不使用 action 内的同一 counter 自证 action 成功，也不以 Incident 数量下降、没有新 alert 或模型分数变化自动建立因果关系。

### 7. 展示与自动化边界

Web 提供数量卡、Top-N 水平条形图、rate 时间序列、状态 timeline 和等价表格。所有视图显示 target/table/generation、窗口、formula、coverage、新鲜度、reset/gap 和 outcome 证据。跨 target fleet 视图只聚合可比较、同 profile 的明确集合并保留 per-target drill-down；不得用平均命中率或多数 target 成功覆盖 child/quality 差异。颜色不是唯一状态表达。

Rule Effectiveness 是 Go/PostgreSQL 原生 canonical 投影，启停任何插件都不得改变其结果、公式或可用性。统计插件可以接收 Go 冻结的只读 rule-observation snapshot，产生明确标注为 derived/non-authoritative 的跨 scope 对比或报告；其 Artifact 必须引用 source revision、formula/profile、target/generation/window/quality，不能覆盖 per-rule current、补造 denominator、把缺失补零、声称 action outcome，或把结果写回规则事实。前端通过 ADR-0018 的内建声明式 renderer 展示这些派生视图，不执行插件代码。

no-hit/dead-rule、shadow/redundant/overlap 分析只生成“待复核候选”。任何 disable/delete/modify 都必须创建新的 canonical proposal，并继续执行风险分类、授权、intent、Edge write/readback 和 CAS。Grafana、Alertmanager、插件、Agent 和浏览器不能拥有自动处置 hook。

### 8. 性能与可观测性

初始 profile 每 target 最多 4,096 rules、每 Read 256 entities/1 MiB/2 秒、单 target 一条 in-flight Read、15 秒最多 4 batch、60 秒 full sweep、75 秒 stale。Edge 使用 global/per-target 低优先级有界公平调度，P4 effect readback/mastership/telemetry 优先；必须在 0/1/2/N target 和一个慢/断连 target 下实测，N 与全局 RPC/queue/FD/memory 上限未冻结时为 `HOLD/NOT RUN`。

Edge→Go `RuleObservationBatch` 最多 256 条、1 MiB，per-target queue 最多 8 batch。队列满时不得阻塞 P4 write/readback 或静默丢弃；必须记录 exact sequence gap，丢弃低优先级未持久 sample，使受影响窗口 `invalid/stale`，并让恢复后的第一条 cumulative sample 只建立 baseline。

Go current/list API 默认 50、最多 200，Top-N 默认且最多 20；current/list p95 目标 ≤100 ms，24 小时/7 天 trend p95 目标 ≤250 ms。实现不得逐规则查询 P4/Prometheus、产生 N+1 SQL 或把全量 raw series 交给浏览器计算；页面点数继续受 `WEB-PERF-001` 限制。统计插件也只能消费同一有界 Go snapshot，不得另行扫描 P4、PostgreSQL 或 Prometheus；它的队列、存储和查询预算与 core rule observation 分离。

per-rule 数据进入 PostgreSQL，不进入 Prometheus label。Prometheus 只收低基数 sweep、queue、quality/status 聚合；Grafana 只读呈现运维聚合；Alertmanager 只负责通知路由；OpenTelemetry Collector 只做有界 telemetry pipeline。任一组件失败不改变 rule/effect 事实。

## 独立评估

这套设计比“规则命中率 = hit / total”更复杂，但复杂度来自问题本身：系统若没有 eligible denominator，就没有可解释百分比；若没有独立 packet oracle，就没有处置结果证明。把缺失信息显示出来，才能让 Analyst/Operator 判断规则未命中究竟是无流量、规则过窄、优先级遮蔽、采集失真还是设备 drift。

direct counter 是首期最合适的在线证据：它位于真实数据面、读取可批量、无需把每包送入控制面。它的边界也必须保留：只证明 entry/action 的计数路径执行。未来可以增加受控 sampled packet proof 或厂商 action telemetry，但只能作为新 outcome profile，不能改变本 ADR 的分层语义。

## 取舍

收益：规则表现可解释；不会把无流量或 reset 误判为失败；兼顾 P4 热路径和控制面成本；能用 PTF/P4Testgen/packet oracle 形成可复核证据；no-hit 优化不会旁路治理。

代价：P4 program 需要额外 counter/eligible instrumentation；Edge/Go/DB/Web 增加 observation 合同与容量；硬件 target 的 counter 语义需分别资格化；多数生产规则的 outcome 可能诚实地显示 `not_measurable`。

## 被拒绝的方案

1. 单一“规则生效率” gauge：混合安装、流量、命中与结果，分母不清。
2. `hit / all ingress` 对所有规则通用：不同 table/stage/overlap 不可比较，也可能没有同点 denominator。
3. 只用 P4 Write ACK：不能证明 current entry/readback，更不能证明 hit。
4. 只用 idle-timeout/last-hit：best-effort 且没有 packet/byte delta。
5. 每条规则作为 Prometheus label：cardinality 随规则增长，破坏 TSDB 预算。
6. 轮询时清零 counter：与诊断/恢复竞争，reset 边界难以证明；采用累积值+epoch delta。
7. zero-hit 自动删除/禁用：无流量、遮蔽或观测故障都可能产生 zero，且会建立第二 mutation path。
8. 让 Grafana/P4Runtime Shell 直连生产 target：产生第二控制入口并绕过 Go/Edge 的身份、审计和 fence。

## 迁移与回滚

greenfield 先冻结 P4Info/counter/profile、`traffic-replay/v1` 和 golden，再在 BMv2/PTF 中资格化，随后分别实现 Edge sampler、Go/DB projection 和 Web。对 baseline policy，observation epoch 只在 selector/bank exact readback与PG current CAS后建立；bank cutover关闭旧epoch并为新bank建立baseline。Module Complete 前真实 target 只可做只读 `REHEARSAL/NOT QUALIFIED`。

回滚可以关闭 rule observation scheduler/UI，已安装规则和 effect 链继续；历史 observation 只读保留。不得通过回滚 P4 program 让当前 effect entry 失去可读回能力；pipeline 回滚必须走 ADR-0004 的 generation/P4Info/cutover 门禁。

关闭、撤销或回滚统计插件只移除/冻结其派生 `plugin_statistics` current，不删除 canonical rule observations，也不改变 Rule Effectiveness 页面；历史 Artifact 按 retention 只读保留并显示 stale/revoked generation。

## 验证

- exact readback、matching/non-matching/zero traffic、counter action coverage；
- eligible denominator、overlap/priority/default/multi-table；
- wrap/saturate/reset、restart/generation/pipeline/revision、duplicate/split/order/gap；
- counter hit + wrong packet outcome 的反例；
- TTL/rollback/supersede/readback drift 和 late sample；
- 4,096-rule sweep、queue saturation、3,600 秒 soak 与核心 p99/RSS 隔离；
- per-rule Prometheus cardinality rejection、Grafana/Alertmanager/Agent mutation rejection；
- browser formula/coverage/state/a11y/表格替代与 no-hit 候选无 mutation。
- generated/synthetic/PCAP/live-session fixture按 ADR-0012分型；sender accepted但 DUT未见、counter hit但错误outcome、软件 target不提升硬件资格的反例。
- response overlay/baseline active bank/selector source分开，inactive/previous bank hit不进入current rollup；双bank切换、default与host-filter contamination按ADR-0014验证。
- 1/2/N target 的独立 actor/epoch/queue/fence、公平 full sweep、assignment/actor late sample 与 fleet partial/rollback；一个 target gap/reset/stale 不污染其他 target 或被聚合隐藏。
- 统计插件 disabled/revoked/timeout/oversize/late-generation 时 Rule Effectiveness API/UI 与公式字节级不变；derived view 保留 source/profile/generation/window/quality，不能覆盖 canonical current 或产生 mutation。

## 参考

- 多 target/fleet 与设备管理边界：`0015-multi-target-p4-fleet-and-device-management-boundary.md`

- P4Runtime 1.4.1：<https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html>
- PSA 1.2：<https://p4lang.github.io/p4-spec/docs/PSA.pdf>
- Suricata rule profiling：<https://docs.suricata.io/en/latest/performance/rule-profiling.html>
- nftables counters：<https://wiki.nftables.org/wiki-nftables/index.php/Counters>
- PTF：<https://github.com/p4lang/ptf>
- P4Testgen：<https://github.com/p4lang/p4c/blob/main/backends/p4tools/modules/testgen/README.md>
- Prometheus instrumentation：<https://prometheus.io/docs/practices/instrumentation/>
- Grafana state timeline：<https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/state-timeline/>
- 专项调研：`../research/rule-effectiveness-and-system-reuse-assessment-2026-08-10.md`
- P4 流量生成与回放：`0012-bmv2-p4-traffic-generation-and-replay.md`
- BMv2 无状态防火墙：`0014-bmv2-stateless-firewall-policy-and-activation.md`
- 插件统计与声明式 Web 投影：`0018-plugin-statistics-and-declarative-web-projection.md`
- 插件统计专项调研：`../research/plugin-statistics-and-declarative-web-assessment-2026-08-12.md`
