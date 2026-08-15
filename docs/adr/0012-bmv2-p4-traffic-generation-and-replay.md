# ADR-0012：BMv2/P4 流量生成、PCAP 回放与结果 Oracle

- 状态：Accepted
- 日期：2026-08-10；最近复核：2026-08-11
- 决策者：Owner
- 需求基线：`vNext-requirements-1.18`（原始决策形成于 v1.9；v1.12 只补充多 target 隔离与证据身份，不扩大生产发包权限）
- 关联需求：`ARCH-003`、`ARCH-REUSE-001`、`ARCH-TELEMETRY-001`、`ARCH-TARGET-FLEET-001`、`CONTRACT-P4-001`、`CONTRACT-RULE-001`、`CONTRACT-PROFILE-001`、`CONTRACT-SUPPLY-001`、`CONTRACT-TRAFFIC-001`、`CONTRACT-TELEMETRY-001`、`CONTRACT-INFERENCE-001`、`CONTRACT-TARGET-001`、`CONTRACT-FLEET-EFFECT-001`、`FUNC-TEL-001`、`FUNC-RULE-001`、`FUNC-TRAFFIC-001`、`FUNC-TARGET-FLEET-001`、`PERF-001`、`PERF-RULE-001`、`PERF-TRAFFIC-001`、`PERF-TEL-INF-001`、`PERF-TARGET-FLEET-001`、`REL-RULE-001`、`REL-TRAFFIC-001`、`REL-TEL-INF-001`、`REL-TARGET-FLEET-001`、`SEC-TRAFFIC-001`、`SEC-TARGET-FLEET-001`、`OBS-TRAFFIC-001`、`TEST-003`、`TEST-006`、`TEST-007`、`TEST-008`、`TEST-RULE-001`、`TEST-TRAFFIC-001`、`TEST-TEL-INF-001`、`TEST-TARGET-FLEET-001`、`TEST-REUSE-001`、`ACCEPT-001`、`DEC-024`、`DEC-025`、`DEC-029`、`DEC-030`、`DEC-032`、`DEC-037`

## 背景

legacy MASI-NIDS 可以在 Mininet 中用 `iperf3`、ping、`hping3` 和 Python UDP 产生合成流量，也能把 CSV 转成一组重新生成包的命令；它没有通用 PCAP replay、双向方向 cache、packet outcome oracle 或可复核的流量 profile。vNext 需求此前提到 PCAP、PTF 和 P4Testgen，但没有固定“回放的是什么、从哪一侧进入、速率是否真正达到、发包成功是否等于目标收到、标签如何映射、结果如何判定”。

这些缺口会产生四种假结论：把二层 frame 回放称为真实 TCP 会话；把 requested PPS 称为 BMv2 吞吐；把 direct counter 增长称为 drop/forward 成功；把公开下载的数据集默认视为可分发、无敏感数据且标签精确。

成熟工具各自解决不同问题。[PTF](https://github.com/p4lang/ptf) 提供基于 Python `unittest` 的 packet injection/verification，[P4Testgen](https://github.com/p4lang/p4c/blob/main/backends/p4tools/modules/testgen/README.md) 生成 input/control-plane/expected-output test；[Tcpreplay 的 replay model](https://tcpreplay.appneta.com/concepts/replay-model/) 明确它发送 capture 中的 frame bytes而不建立网络连接，且成功统计只表示已交给发送接口；[Mininet](https://mininet.org/overview/) 使用 namespace/veth 与真实 Linux 网络栈，但仍受单一宿主 CPU/带宽限制；[netem](https://man7.org/linux/man-pages/man8/tc-netem.8.html) 提供 delay/loss/duplicate/reorder/corrupt/rate 等扰动，同时受内核 timer、TSQ 等限制。

## 决策

### 1. 统一合同，不统一语义

建立 `contracts/testkit/traffic-replay/v1` 与 `p4-traffic-replay/v1`。统一的是 fixture identity、provenance、topology/direction、transformation、timing/rate、impairment、resource、ground truth、oracle 和 evidence envelope；以下四种执行语义继续分开：

| Class | 目的 | 首期实现模式 | 能证明 | 不能证明 |
|---|---|---|---|---|
| `generated-packet` | P4 table/action/path 的最小确定性测试 | PTF + 手写 golden/P4Testgen case | exact ingress→egress/drop/mirror、firewall priority/default/fragment/bank/overlay 与受控 counter | 真实流量分布、模型质量、硬件容量 |
| `synthetic-flow` | 可复现的正常/攻击流量形态 | test namespace 中的 ping/UDP/真实 socket client 等受控程序 | Linux 协议栈产生的声明流量与检测路径 | 未声明的应用真实性、公开语料覆盖 |
| `curated-pcap` | 历史 frame 形状、混合流量和检测链回放 | 经方向/改写后的 L2 replay adapter；Tcpreplay suite 为条件候选 | capture bytes按声明时序/速率进入 test interface | ARP、TCP handshake、重传、真实 server 响应 |
| `live-session` | 必须有连接/应用状态的场景 | 受控 client/server；未来硬件规模可条件采用 stateful generator | 两端真实协议状态和 transcript | 自动代表 PCAP corpus、P4 action outcome或模型质量 |

一个 scenario 可以串联多个 class，但每个attempt只能按manifest选择的实际mode/backend报告。每个scenario必须一次性冻结`traffic_mode + fixture_digest + runner_profile_id + runner_backend_id + topology/target + direction + rewrite_digest + impairment_digest`；缺少或失败时为HOLD/NOT_RUN，禁止best-available、自动探测后换工具或把PTF/L2 replay/live-session互相替代。切换backend必须创建新的scenario/evidence identity。禁止以“攻击模拟”一个名称抹平packet、flow、session和corpus的区别。

### 2. 工具采用矩阵

| 能力 | 候选 | 结论 | 边界 |
|---|---|---|---|
| P4 编译与符号用例 | `p4c` + P4Testgen | `ADOPT for test` | 生成 P4Info、input/control-plane/expected-output；generated case仍须在 exact target执行 |
| packet harness/oracle | PTF | `ADOPT for test` | 负责发包与 exact/Mask verification；packet backend、root权限、版本/许可证单独冻结；不进入生产 target |
| 软件拓扑 | Mininet + BMv2 | `ADOPT for test` | 快速、可复现功能/故障环境；只形成 software-target evidence，不能外推 ASIC/line rate |
| 链路扰动 | Linux `tc netem`/qdisc | `ADOPT for test` | exact kernel/iproute2/qdisc/seed/readback；记录 timer/TSQ 限制和实际统计 |
| 二层 PCAP 回放/改写 | Tcpreplay/tcprewrite/tcpprep | `CONDITIONAL test-only` | 功能匹配，但套件为 GPLv3；在项目 license/NOTICE/image 审查前只作外部隔离 binary，不 vendoring、不链接生产制品 |
| 交互式控制诊断 | P4Runtime Shell | `CONDITIONAL test/read-only` | 不常驻、不调度、不持有 production writer credential |
| 硬件高率/有状态发生 | Cisco TRex 或按同矩阵选定的一种等价工具 | `CONDITIONAL future hardware profile` | 只有普通 runner无法驱动已确认硬件 SLO时触发；DPDK/NIC/CPU隔离、许可证、端口校准、双端统计另行资格化；不进入 BMv2 默认路径 |

[Tcpreplay timing](https://tcpreplay.appneta.com/concepts/timing-and-speed/) 支持 recorded timing、multiplier、fixed PPS/Mbps 与 topspeed，这些是互斥发送策略而不是 target 性能结论；高率精度会受 scheduler/timer/syscall/disk影响。[tcprewrite](https://tcpreplay.appneta.com/reference/man/tcprewrite/) 可改写地址、端口、checksum、MTU等，区分 client/server 的操作依赖 tcpprep cache。原始和改写 artifact、cache、配置与输出 digest因此都进入合同。

TRex 同时提供 stateful/stateless 发生能力并面向高率环境，但其 DPDK、NIC、CPU、配置和运行控制面显著扩大资格面；因此只保留为硬件性能阶段的条件候选，不为 BMv2 功能测试提前引入。[TRex official overview](https://trex-tgn.cisco.com/)

### 3. 执行流水线

```text
immutable fixture + manifest
→ schema/digest/license/privacy/limit validation
→ exact runner/backend/mode preflight（首期本地/CI runner=`e2e-runner-compose/v1`）
→ direction classification + immutable rewrite
→ isolated topology/P4 test state + qdisc/offload readback
→ bounded send/live session
→ independent ingress/egress capture + DUT/counter reads
→ packet/action or detection oracle
→ structured evidence envelope
→ exact-owned cleanup + leak check
```

上述阶段顺序是合同的一部分，不能由脚本按工具可用性重排或省略。Compose health只允许场景继续，runner不拥有P4/current/oracle；任何prepare/preflight/send/observe/cleanup失败都按同一attempt保留证据，不静默换backend重跑成PASS。

PR 与 P4 module gate 以小型 generated/synthetic fixture为默认；curated corpus在受控环境运行。P4 standalone qualification可以用 test-only control fixture配置 BMv2；Edge pairwise/system E2E中，所有 P4Runtime配置、Read与Write仍只由 Edge按正式合同完成，traffic runner只发/收数据包。

多 target 场景中，每个 attempt 必须绑定 stable `target_id`、test endpoint/port map、assignment/application generation、P4Info/profile 与独立 sender/DUT/counter/oracle identity；不同 target 的端口、namespace、counter baseline、capture 和结果不得合并。Fleet parent 不可被 runner 执行或配置设备；正式 fleet E2E 仍由 Go 创建逐 target child intents、各 TargetActor 写入/readback，runner 只向明确 allowlist 的对应 dataplane ingress 发包并逐 target 判定 outcome。

当 scenario 覆盖在线检测链时，`detection oracle` 不是读取一个模型返回值就结束，而是按 ADR-0013 观察 `test/DUT ingress → qualified telemetry source → final event-time window → inference input/result identity → PostgreSQL canonical Event ACK`。traffic runner 不读取生产 P4Runtime stream、不写 Edge ring/WAL，也不直接向 C++/Go 注入绕过 source contract 的“成功”数据；仅在模块独立测试中使用同合同 fake boundary，并将结果标为该模块证据或 `REHEARSAL/NOT QUALIFIED`。

### 4. 证据梯子

每次运行分别记录：

1. requested schedule/rate；
2. sender attempted/accepted/error；
3. test ingress capture observed；
4. DUT ingress/egress/drop observation；
5. exact rule/eligible counter；
6. independent packet/action outcome；
7. telemetry/inference/detection result。

任何一级都不能自动推出下一级。规则生效对外仍是 installation—match—outcome 三层，底层 effect execution独立；traffic evidence只为这些维度提供测试输入/观察，不创建第五套 rule状态机。

第 7 级还必须分开保存 telemetry source profile/generation/coverage、window finality/quality、input/result/Event identity 与 canonical ACK；Digest/PacketIn 收到样本、C++ `Run()` 成功或 ring slot 释放都不能单独证明检测事实已持久化。

### 5. 时序、速率与软件 target 边界

recorded timing、multiplier、fixed PPS、fixed Mbps、topspeed分别执行；requested与achieved分开。功能 oracle使用误差可控的低/中速 profile，capacity test使用独立 profile，以免 capture/oracle先丢包却误判 P4。

Mininet使用真实 Linux栈和 namespace/veth，但不提供完整 VM隔离，且不能超过单一宿主 CPU/带宽。BMv2本身也是软件 switch。因此该组合可证明合同、功能、恢复和 exact environment的软件容量，不能证明硬件 line rate。[Mininet limitations](https://mininet.org/overview/#limitations)

1/2/N target 性能与故障场景必须固定每 target 的进程/container、CPU/memory/cgroup、management/dataplane endpoint、device_id、port map 和 runner queue，并分别报告 achieved rate/drop/outcome；一个 BMv2 或 runner queue 饱和不得被其他 target 的成功掩盖。共享宿主结果只资格化该 exact software topology，不能把总发送率平均后外推单 target 或硬件容量。

netem config保存 delay/jitter/loss/duplicate/reorder/corrupt/rate/slot/limit/seed 与实际 qdisc统计。内核文档显示 TSO/GSO/GRO会改变 packet segmentation/aggregation，因此 MTU与 offload状态必须冻结并作为 environment identity；TCP realism还要按 netem文档明确 impairment方向与 TSQ影响。[Linux segmentation offloads](https://docs.kernel.org/networking/segmentation-offloads.html)

### 6. Corpus、标签与数据治理

公开 NIDS 数据集不自动进入仓库或发布制品：

- [CIC-IDS2017](https://www.unb.ca/cic/datasets/ids-2017.html) 提供带完整 payload 的 PCAP与按时间/五元组标注的 flow CSV，适合作为候选，但项目仍须固定实际使用条目的许可/引用/再分发条件和 packet-to-label join；
- [UNSW-NB15](https://research.unsw.edu.au/projects/unsw-nb15-dataset) 包含约100 GB PCAP与九类攻击，官方页面明确 academic free、commercial use需与作者协商，不能默认打包进产品或通用 CI；
- [CTU datasets](https://www.stratosphereips.org/datasets-overview) 可作为有明确场景/许可元数据的候选，但每个 scenario仍独立登记完整/截断、normal/background/malware和ground truth；
- [NIST SP 800-188](https://csrc.nist.gov/pubs/sp/800/188/final) 强调去标识既是技术也是治理过程；[RFC 6235](https://datatracker.ietf.org/doc/rfc6235/)说明 IP flow anonymization需要明确定义字段与风险。仅改写 IP/MAC 不构成自动匿名、授权或无敏感 payload。

首期至少维护一个项目自有、最小、无真实 credential 的 synthetic corpus，以及一个通过所有门禁的外部 PCAP slice。原始大型 corpus保留在独立受控存储；仓库只保存允许分发的最小 fixture或 fetch/verify metadata，不保存来源许可不清的 payload。

### 7. 安全与运行边界

runner只在专用 test host/VM/namespace运行，默认无外网和生产 route/credential。root/CAP_NET_ADMIN/CAP_NET_RAW只授予固定 runner；manifest为严格数据，不接受 shell、任意 argv、environment expansion、host path/interface glob、动态 Python或 P4Runtime mutation。每个 firewall action scenario 在发包前后记录 UFW/nftables/iptables/eBPF/XDP/bridge/qdisc/route state；无法证明 host path 未提前丢包时结果为 environment-invalid/HOLD。

每个 run具有 packet/byte/duration/loop/CPU/RSS/PID/FD/disk/inode上限、总 deadline、独立 temp与 ownership。失败只清理该 run exact namespace/veth/qdisc/process/artifact；不能确认清理完成时 quarantine宿主。PCAP视为不可信且可能含个人数据、credential或 exploit payload，raw payload不进入普通日志、metrics、Frontend或长期 evidence。

## 取舍

收益：复用成熟 packet/replay/topology工具；检测 replay、P4 action oracle和性能测试的结论可解释；同一 manifest可在软件与未来硬件 target重跑；许可、隐私和生产网络风险在执行前 fail closed。

代价：需要维护 fixture/profile schema、rewrite/direction golden、独立 capture/oracle、受控 root runner与 corpus registry；四种 mode不能由一个简单脚本统一；BMv2结果会更诚实地保持 software-only，硬件资格需要另建环境。

## 被拒绝的方案

1. 只用 `tcpreplay pcap` 返回码判 PASS：只证明 sender接口接受，不证明 DUT或结果。
2. 将 PCAP replay称为真实 TCP攻击会话：没有 handshake/协议栈状态。
3. 只用 P4 counter判 drop/forward：counter执行不等于正确 egress/outcome。
4. 用一个大型攻击 PCAP替代 PTF最小 oracle：标签、方向、overlap和丢包会掩盖具体 action错误。
5. 用 BMv2 requested PPS声明生产容量：软件 switch与generator共享 CPU/内核。
6. 首期同时引入 Tcpreplay、TRex、MoonGen/pktgen等多套等价发生器：扩大许可证、NIC、SDK和证据矩阵；首期每种能力固定一种 profile。
7. 把 traffic runner做成生产上传/发包服务：扩大攻击面并可能形成绕过治理的副作用入口。
8. 公开可下载即复制进仓库/镜像：忽略 license、再分发、payload和隐私义务。

## 迁移与回滚

greenfield先实现 schema/golden和最小 PTF/synthetic fixtures，再实现隔离 runner、Mininet/BMv2和netem，随后按许可证审查结果接入一个 L2 replay adapter，最后接 curated corpus。TRex/等价工具不属于首期 BMv2依赖。

legacy CSV→命令 replay plan可用于提取可观察场景，但不能直接迁移为合同或执行输入；应转写为 strict fixture，并以 packet/counter/oracle重新验证。legacy PCAP/CSV/脚本不成为 vNext runtime依赖。

回滚可以禁用 curated-pcap、netem或条件 generator profile，保留 generated/synthetic P4功能门禁；已生成 evidence只读保留并标明 tool/profile。不得用未登记 shell fallback保持表面覆盖，也不得因 test tooling不可用放宽 production P4唯一 writer或规则结果语义。

## 验证

- schema、digest、DLT、direction/rewrite、timing/rate、loop/resource和unknown mode负例；exact runner/backend缺失、失败、自动切换与same-scenario换backend负例；
- generated/synthetic/curated/live-session四类正负用例及真实 TCP与 L2 replay差异；
- sender accepted但 DUT未见、counter hit但 outcome错误、detection与action结果不一致的反例；
- P4 aggregate snapshot一致/不一致、Digest/PacketIn supplemental sample丢失、window迟到/不final、C++成功但Go/PG未ACK，以及同一traffic attempt到canonical Event的identity关联；
- response overlay 与 baseline active bank 的 matching/non-matching、priority/default/fragment/permit/drop、selector切换前后和 host-filter contamination 反例；
- netem/offload/MTU/kernel profile、timer/TSQ影响与 seed/readback；
- malformed/truncated corpus、ground-truth conflict、license/privacy/retention拒绝；
- crash/hang/timeout/resource exhaustion/cleanup leak与 production route/credential/P4访问拒绝；
- 1/2/N target 的 identity/namespace/port/counter/oracle隔离、wrong-target injection拒绝、一个target慢/重启/丢包时其他target结果不被串改，以及fleet parent不可被runner当作执行队列；
- BMv2 software-only evidence与硬件 target evidence不能互相提升；
- Tcpreplay/Mininet/PTF/TRex等组件 exact version/license/SBOM/NOTICE/upgrade/rollback/exit登记。

## 参考

- PTF：<https://github.com/p4lang/ptf>
- P4Testgen：<https://github.com/p4lang/p4c/blob/main/backends/p4tools/modules/testgen/README.md>
- P4Runtime 1.4.1：<https://p4.org/wp-content/uploads/sites/53/2024/10/P4Runtime-Spec-v1.4.1.html>
- Tcpreplay replay model/timing/tcprewrite：<https://tcpreplay.appneta.com/concepts/replay-model/>、<https://tcpreplay.appneta.com/concepts/timing-and-speed/>、<https://tcpreplay.appneta.com/reference/man/tcprewrite/>
- Mininet overview/license：<https://mininet.org/overview/>、<https://github.com/mininet/mininet/blob/master/LICENSE>
- Linux netem/segmentation offloads：<https://man7.org/linux/man-pages/man8/tc-netem.8.html>、<https://docs.kernel.org/networking/segmentation-offloads.html>
- Cisco TRex：<https://trex-tgn.cisco.com/>
- Docker Compose 启动顺序与 healthcheck：<https://docs.docker.com/compose/how-tos/startup-order/>
- CIC-IDS2017：<https://www.unb.ca/cic/datasets/ids-2017.html>
- UNSW-NB15：<https://research.unsw.edu.au/projects/unsw-nb15-dataset>
- CTU datasets：<https://www.stratosphereips.org/datasets-overview>
- NIST SP 800-188：<https://csrc.nist.gov/pubs/sp/800/188/final>
- RFC 6235：<https://datatracker.ietf.org/doc/rfc6235/>
- 成熟方案来源登记：`../research/mature-solutions-review-sources-2026-08-10.md`
- 规则表现决策：`0008-rule-effectiveness-observation.md`
- 组件复用边界：`0009-mature-component-reuse-boundaries.md`
- 在线遥测、窗口、推理热路径与 canonical ACK：`0013-online-telemetry-and-inference-hot-path.md`
- BMv2 无状态防火墙与双 bank 激活：`0014-bmv2-stateless-firewall-policy-and-activation.md`
- 多 target/fleet 与设备管理边界：`0015-multi-target-p4-fleet-and-device-management-boundary.md`
