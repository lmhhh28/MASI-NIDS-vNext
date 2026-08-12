# P4/Switch 模块详细设计

- 模块 ID：`MOD-SW-001`
- 扩展需求：`MOD-SW-FW-001`
- 目录：`p4/`
- 文档状态：`DRAFT`
- 主要需求：`ARCH-FW-001`、`ARCH-TELEMETRY-001`、`CONTRACT-P4-001`、`CONTRACT-P4-FW-001`、`CONTRACT-RULE-001`、`CONTRACT-TRAFFIC-001`、`CONTRACT-TELEMETRY-001`、`PERF-P4-FW-001`、`TEST-003`、`TEST-P4-FW-001`、`TEST-RULE-001`、`TEST-TRAFFIC-001`、`TEST-TEL-INF-001`
- 主要 ADR：ADR-0004、ADR-0008、ADR-0012、ADR-0013、ADR-0014、ADR-0015

## 1. 模块目标

P4/Switch 模块提供 exact BMv2 `simple_switch_grpc`/v1model 软件数据面：完成基础转发、IPv4 无状态 response overlay 与 baseline 双 bank/selector、per-entry/eligible counter，以及目标资格化的有界遥测 aggregate bank/epoch/snapshot。它必须能以真实 BMv2 进程和编译制品独立启动、注入真实 packet 并形成结构化 packet/counter/state 证据。

模块不承担业务审批、策略 current、target assignment、模型推理、Event 持久化或 fleet 编排。P4Runtime production session 由 Rust Edge 独占；模块独立测试使用 test-only control fixture，不取得生产 writer 资格。

## 2. 交付与资格边界

一个可资格化 P4 artifact set 至少绑定：

- P4_16 source、p4c/toolchain digest；
- BMv2 JSON、P4Info、program/pipeline/application profile digest；
- `simple_switch_grpc` image/binary、v1model、P4Runtime 1.4.1；
- port map、parser behavior、table/action/counter/register IDs 与 widths；
- firewall/telemetry/rule/traffic profile 和 resource manifest；
- generated/PTF/P4Testgen golden、fault/performance environment。

任何一项变化都产生新的 artifact/profile identity。BMv2 PASS 只适用于该软件 target，不外推 ASIC、PSA/TNA、IPv6 effect、stateful/NAT/rate-limit 或 line-rate。

## 3. 数据面组成

```text
ingress parser + normalized metadata
├─ parser/fragment/error classification
├─ response_overlay (exact IPv4 5-tuple, high priority, TTL managed off-switch)
├─ policy_selector
│  └─ baseline_bank_0 | baseline_bank_1
├─ forwarding pipeline
├─ direct rule counters + eligible ingress counters
└─ telemetry aggregate active bank / frozen snapshot bank
```

### 3.1 Parser 与元数据

Parser 固定 Ethernet/IPv4/IPv6 observation、IP protocol、fragment class、L4 availability 和 ingress metadata。首期 firewall 只执行 IPv4；IPv6 可进入 telemetry，但 effect 返回 unsupported。non-initial fragment 不伪造 L4 port，parser error/IPv4 option/unsupported EtherType 的处理由 profile 和 packet golden 固定。

### 3.2 Response Overlay

Overlay 是高优先级、exact IPv4 五元组临时 permit/drop 表，拥有独立 entry/counter quota。TTL 是 Go/PostgreSQL durable fact，P4 只执行 entry；idle timeout/digest 可以提示，不能自行决定 canonical expiry。Overlay 不修改 baseline selector，也不被 baseline bank write 清除。

### 3.3 Baseline 双 Bank

两个 bank 的 schema 和容量完全相同，`policy_selector` 只选择一个 active bank。完整 desired plan 先写 inactive bank并逐 entry readback；只有 plan/count/default/counter binding 都一致时才允许 selector publication。数据面不得观察到两个 bank 拼接的 hybrid policy。

每个 revision 明确包含 default `permit-and-continue|drop`。permit 只继续后续 forwarding，不选择 egress。优先级、overlap/conflict、shadow、fragment 和展开语义由 Edge compiler/profile决定，P4 按 concrete plan 执行。

### 3.4 Rule Observation

需要观测的 overlay 和 active baseline entry 绑定 direct packet/byte counter；同 stage 提供可比较的 eligible ingress counter。counter width、wrap/saturate、initial value、action 是否执行 counter 和 reset 语义进入 P4 profile。inactive/previous bank counter 不进入 current effectiveness。

Counter 只证明 entry/action 的 count path 被执行，不证明最终 drop/forward/mirror outcome。结果必须由独立 packet oracle证明。

### 3.5 Telemetry Aggregate

默认 `telemetry-p4-window/v1` 使用有界 per-target/per-window metadata aggregate。模块必须提供可证明的一种 snapshot 策略，首选 active/frozen 双 bank和 epoch flip；普通多个 P4Runtime Read 的组合不能被当成原子快照。

每个 snapshot 绑定 bank、epoch、sequence、application/P4Info generation、expected/observed entries 和 clear/advance condition。Digest/PacketIn 仅作有界 window-ready hint 或 sample，显式报告 duplicate/drop/coverage；Ack 不表示完整覆盖。

## 4. 公开边界

| 边界 | 调用方 | 语义 |
|---|---|---|
| P4Runtime StreamChannel/arbitration | Rust Edge；隔离测试 fixture | primary、PacketIn/Digest、mastership；production 只允许 Edge |
| P4Runtime Write/Read | Rust Edge；模块测试 fixture | table/selector/counter/register exact entity 和 target error/readback |
| Dataplane ingress/egress | 网络/traffic runner | packet forwarding/drop/mirror 和 aggregate/counter 更新 |
| Compiled artifacts | build/test/deploy | P4Info、BMv2 JSON、profile/resource/golden digest |

Go、Web、插件、Central Inference、Grafana、P4Runtime Shell daemon 不得持有 production writer credential。P4Runtime Shell 只允许隔离 test 或明确 read-only diagnosis profile。

## 5. 状态与不变量

P4 target 保存运行中的 table/register/counter/selector/bank 状态，但它不是业务事实源。PostgreSQL current 只能在 Edge exact readback 后由 Go CAS 建立。

必须持续成立：

- 同一 target/role 只有一个有效 primary writer；
- active baseline 只由 selector 唯一确定；
- incomplete inactive bank 不影响 active policy；
- overlay 与 baseline 资源、precedence、counter identity 分离；
- aggregate snapshot 不跨 epoch/generation 拼接；
- counter 与 logical entry equality 分离；
- Write ACK、process ready、counter hit 都不能替代 exact readback或 outcome。

## 6. 资源与性能设计

首期资格矩阵把4,096 active observable normalized firewall rules与双bank物理占用作为待资格化最大值，不是BMv2或未来硬件已经支持的容量承诺。每个exact target profile仍须分别冻结并实测overlay、每bank、compiled entries、direct counters、eligible counters、aggregate cells/registers、总资源和绝对性能门槛；这些门槛未冻结或未通过时只能`HOLD|NOT_RUN`，不得声称“支持4,096”。超限必须在外部write前被Edge preflight拒绝；P4仍对超限/非法entity返回稳定错误。

packet 热路径只有 parser、table match/action、counter/register 更新，不访问数据库、RPC、LLM 或插件。性能证据覆盖 0/128/1,024/4,096 rules、最坏合法展开、telemetry aggregate 和 rule counter 同时开启、overlay churn、selector flip、control-plane contention 和 24 小时 soak。

## 7. 启动、健康与关闭

- startup：加载 exact BMv2 JSON、P4Info/profile 关联配置、port map，并拒绝 digest/ID/port drift；
- readiness：P4Runtime endpoint、dataplane ports 和 pipeline identity 可由 test/Edge readback，不等于业务 current；
- liveness：进程取得进展且资源未越界；
- shutdown：停止新 test traffic/control、保留结构化 state/evidence 并精确清理本测试实例。

生产 pipeline lifecycle由 target/deployment profile治理，不由 P4 模块内 timer 或脚本自动替换。

## 8. 故障与恢复语义

- partial bank write/readback mismatch：selector保持旧值；
- selector response loss：只读 selector/bank 后收敛，不能猜测重写；
- process/restart/counter reset：开启新 application/source/observation epoch；
- P4Info/pipeline drift：所有旧 concrete plan/epoch 被 fence，target 保持 read-only/HOLD 直至重新资格和 reconcile；
- aggregate snapshot不一致：窗口为 partial/not_measurable，不进入 canonical inference；
- Digest/PacketIn 丢失：只影响 sample/coverage，不回退成逐包可靠路径；
- host UFW/nftables/iptables/eBPF 污染 packet path：测试环境无效，不能把 host drop 归功于 P4。

## 9. 独立实现接口

模块实现应在内部保持 parser/metadata、firewall tables、selector/banks、forwarding、telemetry aggregate 和 observation resources 的清晰边界，但它们共同编译为一个 P4 artifact set，不拆成多个服务或可独立演化的 P4 事实源。

对外只发布版本化 P4Info/profile 和 packet behavior。策略 canonicalization、human authorization、target assignment、fleet wave、WAL 与 Event 均不得复制进 P4 source 或 test fixture。

## 10. 模块黑盒 E2E 验收范围

模块独立验收使用真实 `simple_switch_grpc`、实际编译 artifact 和真实 packet，邻居可用 test-only control fixture。验收必须证明：

- forwarding、overlay、双 bank/selector、priority/default/fragment、permit/drop；
- 0/128/1,024/4,096 rule 与资源/超限；
- direct/eligible counter 与 installation/match/outcome 分层；
- aggregate bank/epoch/snapshot/read-clear、Digest/PacketIn best-effort；
- generated/synthetic/curated/live-session 的适用 packet/DUT evidence；
- crash、response loss、reset/wrap、P4Info drift、host-filter contamination；
- exact software performance、资源、清理和供应链证据。

测试文档只定义上述对象和通过条件；实际命令在模块实现阶段的 `p4/README.md` 和 runner profile 中形成。

## 11. Module Complete 判定

P4 模块只有在 `MOD-SW-001`、`MOD-SW-FW-001` 及适用的 `TEST-P4-FW-001|TEST-RULE-001|TEST-TRAFFIC-001|TEST-TEL-INF-001` 全部形成 `level=MODULE` 的合格证据后才完成。没有真实 BMv2、只有 p4c 编译、只测 TableEntry、只看 counter 或绝对性能门槛未冻结，均保持 `HOLD|NOT_RUN`。
