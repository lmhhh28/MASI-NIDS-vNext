# [P4 Switch] 清除 Module Complete 的四项资格 HOLD

- Issue ID：`ISSUE-P4-SW-001`
- 状态：`CLOSED / MODULE COMPLETE`
- 类型：`qualification-blocker`
- 优先级：`P0 before Module Complete`
- 模块：`p4-switch`
- 创建日期：2026-08-12
- Owner：`Owner authorized 2026-08-12`，绝对性能门槛与 3,600 秒完成规则已冻结
- 执行负责人：`Codex`
- 原关闭日期：2026-08-13
- 重新打开日期：2026-08-13
- 二次关闭日期：2026-08-13
- 需求基线：[masi-nids-vnext-system-requirements-2026-08-09.md](../masi-nids-vnext-system-requirements-2026-08-09.md)
- 关联要求：`PERF-001`、`PERF-002`、`PERF-P4-FW-001`、`TEST-P4-FW-001`、`TEST-GATE-001`、`TEST-003`、`ARCH-REUSE-001`、`CONTRACT-SUPPLY-001`、`MOD-SW-001`

## 二次关闭结论

完整 formal run `20260813T060546Z-runner-version-formal-006` 已在修正后的 exact
runtime/runner/profile/fixture/supply-chain scope 内以退出码 0 完成。聚合证据为
`level=MODULE, applicability=APPLICABLE, result=PASS, qualification=QUALIFIED`，
32 个 required+applicable 项全部 `PASS/QUALIFIED`，`remaining_holds=[]`；另两个
`NOT_RUN` 均为有稳定理由的 `applicability=NOT_APPLICABLE` 条件能力。

runner 公开执行边界实际读回与 profile 完全一致：Alpine Linux 3.24.1、x86_64、
Tcpreplay 4.5.2/4.5.2-r1、iproute2 7.0.0/7.0.0-r0；runner SPDX 的两个包版本和
`distro=alpine-3.24.1` qualifier 也精确一致。离线 no-cache 重建、签名正反例、
revocation、provenance、扫描策略和 173 项 evidence checksum 全部通过。

P4 Switch 因此在 `p4-stateless-firewall/v1`、BMv2
`simple_switch_grpc`/v1model、`deployment_tier=acceptance` 范围内恢复
**Module Complete**。这不扩大为 production、硬件、IPv6 effect、stateful、NAT、
rate-limit 或 gNMI 资格；正式 pairwise/system 仍须等待其他全部适用首期模块达到
Module Complete。

## 重新打开原因：runner 版本证据漂移（已修复）

正式 run `20260813T012707Z-module-formal-005` 使用的 runner 实际为 Alpine Linux
3.24.1、Tcpreplay 4.5.2（Alpine 包 4.5.2-r1）和 iproute2 7.0.0（Alpine 包
7.0.0-r0），但当时的 profile、traffic schema、四类 fixture 和供应链组件登记仍写着
Tcpreplay 4.5.1 与 iproute2 6.15.0-1。功能、流量重放、故障和性能结果仍是有价值的
历史证据，但“profile = fixture = SBOM = 实际运行容器”的 exact binding 不成立，不能
继续支撑可审计、可复现的 Module Complete 声明。

修正后的 runner digest 为
`sha256:f0b02a81a6fc7e13b3d10695311663eb54aaf25e4487920e7de93b3b211d42dc`。
在该 digest 下新增的 runtime readback 和 runner SBOM exact-version 门禁、更新后的契约与
fixture 均须进入同一轮完整 formal Module gate。新 run 产生
`result=PASS, qualification=QUALIFIED, remaining_holds=[]` 前，本 Issue 保持 HOLD；旧 run
不改写、不删除，也不转用于正式 pairwise/system PASS。独立 Rust Edge 模块开发不受此
HOLD 阻塞。

重新关闭条件：

- [x] profile、traffic schema、四类 fixture 和组件登记统一为 Alpine Linux 3.24.1、
  Tcpreplay 4.5.2/4.5.2-r1、iproute2 7.0.0/7.0.0-r0；
- [x] runner 启动后通过公开的容器执行边界读回 OS、架构、工具版本和包版本，任一漂移
  fail closed；
- [x] 供应链 verifier 对 runner SPDX 中的 Tcpreplay/iproute2 包版本和 Alpine distro
  qualifier 做 exact match；
- [x] 重建并固定新的 runner OCI digest，四类 fixture 绑定该 digest 并重算 canonical
  manifest digest；
- [x] 在新 exact scope 下完整重跑 formal P4 Module gate；
- [x] 新总证据 required+applicable 全部 `PASS/QUALIFIED`、无 `FAIL|HOLD|NOT_RUN`，并更新
  README、Issue、证据 digest 与可复制命令。

## 历史关闭结论（已被上述版本漂移失效）

最终正式 run `20260813T012707Z-module-formal-005` 已在同一 exact software claim
scope 下通过全部适用 Module 门禁，聚合结果为
`level=MODULE, applicability=APPLICABLE, result=PASS, qualification=QUALIFIED`，
且 `remaining_holds=[]`。P4 Switch 因此在
`p4-stateless-firewall/v1`、BMv2 `simple_switch_grpc`/v1model、
`deployment_tier=acceptance` 范围内达到 **Module Complete**。

这不是 production、硬件、IPv6 effect、stateful、NAT、rate-limit 或 gNMI 资格；
全局正式 pairwise 仍须等待其他全部首期模块同时达到 Module Complete。

## 原问题

本 Issue 创建时，P4 Switch 的真实 BMv2 功能、故障、安全、Mininet/PTF packet oracle 和
0/128/1,024/4,096-rule 测量已经执行且没有 `FAIL`，但以下四个适用门禁仍为
`HOLD|NOT_RUN`：

1. Owner 尚未冻结 BMv2 绝对延迟、吞吐、激活期限和资源门槛；
2. `PERF-P4-FW-001` 要求的 3,600 秒 soak 尚未执行；
3. 签名 provenance、依赖闭包 SBOM、revocation 和离线重建证据未完成；
4. 固定 BMv2 `simple_switch_grpc` 收到 SIGINT 后未在 10 秒 grace period 内退出，Docker 最终以 SIGKILL 终止，退出码为 137。

这些 HOLD 已由下述最终证据全部清除；旧 run 仍按原结果保留，不被最终 PASS 改写。

## 当前证据

- 验收 run ID：`20260813T060546Z-runner-version-formal-006`
- 总证据：
  [qualification-evidence.json](../../evidence/p4-switch/20260813T060546Z-runner-version-formal-006/qualification-evidence.json)
- 总证据 SHA-256：
  `sha256:dad9b719269cdc581fb0bc3bc5e6bd6f4d1114bd9e6ad2ab1b79ef2fc68d70f5`。
- 完整性清单：
  [SHA256SUMS](../../evidence/p4-switch/20260813T060546Z-runner-version-formal-006/SHA256SUMS)
- 清单验证：173 个条目全部通过 `sha256sum -c SHA256SUMS`。
- 汇总结果：32 个 required+applicable 项全部 `PASS/QUALIFIED`、`HOLD=0`、
  `FAIL=0`；另有 2 个 `NOT_RUN` 是 BMv2-only scope 下具有稳定理由的
  `applicability=NOT_APPLICABLE` 条件能力。
- exact runtime：
  `sha256:8b8655c2fb7bc5563706ee853fb668ba70457633cda7d93633d022b30b7ead42`。
- exact runner：
  `sha256:f0b02a81a6fc7e13b3d10695311663eb54aaf25e4487920e7de93b3b211d42dc`。
- runner environment readback：
  [runner-environment.json](../../evidence/p4-switch/20260813T060546Z-runner-version-formal-006/runner-environment.json)。
- qualified source patch：
  `sha256:0f0665b4db472680e17c5f2d9f5eddfa1e78201f4097f7cf5dd614a690b2597a`。
- 生命周期证据：
  [lifecycle.json](../../evidence/p4-switch/20260813T060546Z-runner-version-formal-006/lifecycle.json)
- 3,600 秒 soak：
  [soak.json](../../evidence/p4-switch/20260813T060546Z-runner-version-formal-006/soak.json)
  与
  [soak-evidence.json](../../evidence/p4-switch/20260813T060546Z-runner-version-formal-006/soak-evidence.json)。
- 供应链资格：
  [supply-chain.json](../../evidence/p4-switch/20260813T060546Z-runner-version-formal-006/supply-chain.json)
  与
  [verification.json](../../evidence/p4-switch/20260813T060546Z-runner-version-formal-006/supply/verification.json)。

最终 Owner-frozen 五轮绝对性能中位数如下，均为 `PASS/QUALIFIED`：

| 规则数 | 写入 ms | 完整读回 ms | selector flip ms | packet oracle pps |
|---:|---:|---:|---:|---:|
| 0 | 0.003 | 0.648 | 1.258 | 1,000.000 |
| 128 | 14.254 | 16.025 | 2.082 | 1,000.000 |
| 1,024 | 112.816 | 133.750 | 2.443 | 1,000.000 |
| 4,096 | 452.144 | 594.064 | 2.388 | 1,000.000 |

完整的重复测量、延迟分位数、CPU/RSS/cgroup/FD/thread/queue、OOM/restart 和 profile
digest 在 `performance.json` 与原始 workload/resource evidence 中；门槛在本次正式运行前
已经冻结，未按观察结果倒推。

## 工作项

### A. 冻结绝对性能门槛

- [x] Owner 在受保护变更中冻结 exact BMv2 software claim scope 的
  `performance-environment/v1`：CPU/微码/频率/核与 NUMA、RAM、kernel、Docker/cgroup、
  BMv2/p4c/runner image digest、P4 artifact/profile/contract digest、拓扑和 workload。
- [x] 冻结每种 0/128/1,024/4,096 规则 workload 的最低 achieved pps/bps、最大 packet
  p50/p95/p99/max、最大 compile/preflight/write/readback/selector/rollback/cleanup duration、
  error/drop 上限和 CPU/RSS/FD/thread/queue/cgroup 预算。
- [x] 明确 response-only、baseline-only、mixed、permit/drop/default、uniform/hotspot/miss、
  fragment/malformed、counter/telemetry on 和 control-plane contention 的 workload 参数。
- [x] 把门槛、统计方法、warm-up、重复次数、测量窗口、误差/置信区间、异常值规则和
  profile digest 写入机器可读 contract/profile，并映射到 `PERF-P4-FW-001`。
- [x] 门槛必须在最终资格运行前冻结；禁止根据本 Issue 中已经观察到的结果事后选择
  一个刚好可通过的阈值。

### B. 执行 3,600 秒 soak

- [x] 使用 A 中冻结的 exact 环境、artifact 和 workload，执行连续 3,600 秒
  steady、peak、saturation 与 activation-loop 阶段。
- [x] activation loop 覆盖 0/128/1,024/4,096 规则、inactive bank 完整写入/读回、selector
  flip/readback、overlay churn/expiry、rollback/reconcile 和 direct/eligible counter。
- [x] 分层记录 requested、sender accepted、test ingress、DUT ingress/egress/drop、规则
  counter 和独立 packet outcome；任一层不能推定下一层。
- [x] 持续记录 entry/counter 数、active/inactive bank、selector、application/reset epoch、
  RSS、CPU、FD、thread、queue、cgroup throttle/OOM、错误/丢包和性能时间序列。
- [x] 证明无 entry/counter 泄漏、bank 混淆、selector drift、RSS/FD/queue 无界增长或持续
  性能衰退；任何采集空洞必须标记 `gap|not_measurable`，不得补零。
- [x] 保存原始结构化结果、日志、环境/profile/artifact digest、开始/结束时间、清理读回和
  SHA-256 清单。

### C. 完成供应链资格证据

- [x] 选择并固定 Syft 或等价 SBOM generator、Trivy 或选定 scanner、Cosign/Sigstore 或
  组织 PKI 的 exact version/digest、许可证、最小权限和离线资产。
- [x] 为 P4 source、BMv2 JSON、P4Info、device config、p4c image、BMv2 image、runner image、
  OS packages、Python direct/transitive dependencies、shell/deploy/config 和 generated files
  生成同一 inventory 下的 SPDX/CycloneDX 或资格化等价格式 SBOM。
- [x] 补齐 LICENSE/NOTICE、source/output digest、direct/transitive/generated/vendored/file/image
  closure，并使 inventory 与实际启动容器和证据中的 digest 一致。
- [x] 生成与 exact artifact digest 绑定的 in-toto/SLSA 或等价 provenance，记录 source
  revision、builder identity、build type、toolchain、参数和输出。
- [x] 对 exact OCI digest/publisher/provenance 执行签名与正向验证；错误 publisher、错误
  subject/digest、过期或撤销 trust material 必须稳定拒绝。
- [x] 生成离线 verification bundle，在断开 registry/公网的环境中验证并重建已批准 revision，
  对 source/output/SBOM/provenance/NOTICE 漂移 fail closed 为 `HOLD`。
- [x] 保存扫描数据库 digest/freshness、命令、原始输出、策略、例外和证据 SHA-256；签名或
  扫描成功不得替代功能、安全、许可证或性能门禁。

### D. 修复有界优雅关闭

- [x] 在隔离环境中稳定复现 SIGINT、10,000 ms grace period、exit 137，并保存进程树、PID 1、
  实际信号接收和退出时间线。
- [x] 确认问题属于固定 BMv2 runtime、PI/gRPC shutdown、信号处理还是启动封装；不得通过
  readiness、runner 状态或日志静默来推定已 drain。
- [x] 使用新 digest-pinned BMv2 image、可审计的上游修复或最小 target-side patch 实现有界
  shutdown；封装不得吞掉信号、启动第二控制面或后台遗留进程。
- [x] 在不触发 Docker forced kill 的情况下于 10,000 ms 内完成停止，保存退出码、stop duration、
  容器/进程/接口 absence readback 和结构化 evidence。
- [x] 连续重复启动→mTLS P4Runtime arbitration/pipeline readback→packet oracle→停止，证明无
  端口、namespace、veth、FD 或进程泄漏。
- [x] 如果 BMv2 image、编译产物、P4Info/profile 或启动配置 digest 发生变化，旧功能、故障、
  安全、Mininet/PTF 和性能证据全部失效，必须在新 exact scope 下完整重跑。

## 实际执行顺序

1. Owner 先冻结 A 的 absolute performance、资源和 3,600 秒完成规则。
2. 完成 D 的最小 runtime patch、生命周期回归及 C 的 fail-closed 供应链 harness。
3. 以新 exact runtime 重新生成 SBOM/provenance/signature/offline rebuild closure。
4. 在最终 runtime/artifact/profile 下执行 B 的四阶段 3,600 秒 soak。
5. 完整重跑 module E2E 并聚合；旧 digest 和失败 attempt 均未复用或改写。

## 关闭条件

本 Issue 只有同时满足以下条件才能关闭：

- [x] A 的绝对性能门槛已由 Owner 在受保护基线/profile 中冻结，最终 0/128/1,024/4,096
  benchmark 全部达到门槛；
- [x] B 的 3,600 秒 steady/peak/saturation/activation-loop soak 为 `PASS`；
- [x] C 的 SBOM/provenance/signature/revocation/offline verification/rebuild 全部为 `PASS`；
- [x] D 的 lifecycle test 不再发生 forced kill/exit 137，shutdown 在 10 秒内为 `PASS`；
- [x] 使用最终 exact runtime、artifact、contract、environment 和 topology 重跑真实 BMv2
  module E2E，所有 required+applicable 项均非 `FAIL|HOLD|NOT_RUN`；
- [x] 最终 `qualification-evidence.json` 为
  `level=MODULE, applicability=APPLICABLE, result=PASS, qualification=QUALIFIED`，聚合命令
  返回 0，证据 SHA-256 清单全部通过；
- [x] P4 README、需求映射、可复制命令、环境、制品 digest 和负责人引用最终证据；
- [x] 在上述条件全部满足前，不写入“Module Complete”，不开始正式 pairwise 集成。

## 明确禁止的关闭方式

- 不得把 mock、readiness、容器 Running、P4Runtime RPC 成功或 counter 增长当作业务 PASS；
- 不得延长 grace period、忽略 exit 137 或把 forced kill 改名为 graceful shutdown；
- 不得使用 mutable tag、未闭包 SBOM、在线临时下载或仅“存在签名”作为供应链 PASS；
- 不得以相对性能、单次均值、requested PPS 或本轮观察值替代 Owner 冻结的绝对门槛；
- Owner waiver 如适用必须遵守 `TEST-GATE-001`，原性能结果仍保持原值，不得改写为 PASS；
- 本 Issue 不扩大到 Edge 实现、正式 pairwise、硬件、IPv6 effect、stateful、NAT、rate-limit
  或 gNMI 资格。

## 参考

- [P4 Switch 设计](../design/modules/p4-switch-design.md)
- [模块 E2E 验收设计](../testing/module-e2e-acceptance-design.md)
- [资格等级与证据 ADR](../adr/0006-qualification-levels-and-evidence.md)
- [成熟组件复用边界 ADR](../adr/0009-mature-component-reuse-boundaries.md)
- [P4/BMv2 流量与回放 ADR](../adr/0012-bmv2-p4-traffic-generation-and-replay.md)
- [P4 Switch README](../../p4/README.md)
