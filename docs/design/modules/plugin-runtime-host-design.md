# Plugin Runtime Host 模块详细设计

- 模块 ID：`MOD-PLUGIN-001`
- 目录：`plugin-host-rs/`
- 文档状态：`IMPLEMENTED`；operational Module Complete 由 `plugin-host-rs/evidence/module-gates/latest.json` 机器派生，当前 qualification 仍为 `NOT_QUALIFIED`
- 主要需求：`ARCH-PLUGIN-001`、`CONTRACT-PLUGIN-001`、`CONTRACT-PLUGIN-STAT-001`、`PLUGIN-PLAT-001`、`PLUGIN-PLAT-002`、`PLUGIN-PLAT-003`、`PLUGIN-PLAT-004`、`PLUGIN-PLAT-005`、`PLUGIN-PLAT-006`、`PLUGIN-STAT-001`、`PERF-PLUGIN-001`、`PERF-PLUGIN-STAT-001`、`REL-PLUGIN-001`、`SEC-PLUGIN-001`、`TEST-003`、`TEST-PLUGIN-001`、`TEST-PLUGIN-STAT-001`
- 主要 ADR：ADR-0003、ADR-0005、ADR-0006、ADR-0009、ADR-0018

## 1. 模块目标

Plugin Runtime Host 是独立 Rust 隔离执行面。它只实例化已由 Go Plugin Manager 准入并绑定 exact revision/config/generation 的 Wasm component，或受控连接已由资格化 deployment adapter 部署的显式 Host-managed service，并强制 kind contract、capability、resource、deadline、fence、drain、revoke，以及单独授权且带 exact source-generation CAS 的 rollback。

Host不拥有catalog、qualification、activation、statistics run、核心事实或effect；不连接核心PostgreSQL/P4，也不作为独立Analysis/service/Agent的统一业务代理。官方Analysis由Manager控制生命周期，但A2A/MCP业务流量通过direct typed adapter，不经Host。

## 2. 内部构件

```text
Plugin Runtime Host
├─ Manager-facing TLS 1.3 mTLS gRPC Server
│  ├─ apply/get/list/execute/cancel/drain/revoke/rollback
│  └─ exact generation/fence/health observation
├─ Artifact Admission
│  ├─ manifest/schema/kind/profile
│  ├─ digest/publisher/provenance/revocation
│  └─ config/capability intersection
├─ Runtime Registry
│  ├─ wasm-component/v1 (Wasmtime + WASI 0.2)
│  └─ grpc-service/v1 (explicit Host-managed service)
├─ Invocation Scheduler
│  ├─ bounded queues/deadlines/cancellation
│  └─ per-binding/global budgets and circuit/quarantine
├─ Statistics Executor Adapter
│  ├─ exact active definition routing / frozen input
│  └─ bounded Artifact validation/finalization/idempotency
└─ Result Validator / Health / Metrics / Audit Adapter
```

Runtime Registry是构建/配置期封闭集合，不从公网自动发现或下载代码。新增runtime、WASI major或plugin kind必须提升contract/profile和需求基线。

## 3. Manifest 与 Admission

Host只接受Manager提交的exact binding envelope。严格manifest与envelope共同绑定plugin/revision/scope、artifact digest/media type/entrypoint/platform、Host API与input/output contract digest、config schema identity/version/digest、kind/runtime profile、显式capability集合、statistics definition identity/revision/digest、resource profile、verification/publisher、compatibility/rollback、lifecycle/observability、binding generation、issued/expiry和trace；旧的definition ID-only绑定必须拒绝。

Admission重新验证：

- schema/version/kind/runtime/profile；
- OCI/Wasm bytes、size、platform、digest；
- publisher identity、SBOM/provenance/offline verification和revocation freshness；
- config schema identity/version/digest与secret references；
- requested capability与manifest、binding policy、Host profile的交集；
- statistics definition的exact revision/digest集合、兼容与rollback来源；
- resource、network/filesystem/preopen、deadline、retry和output limits。

Unknown major/kind、unverified minor、wrong digest/publisher、capability扩张、revoked artifact、过期generation或超限均在执行前拒绝。Unsigned development fixture永不获得production qualification。

## 4. Runtime Profiles

### 4.1 Wasm Component

首期固定Wasmtime和WASI 0.2 Preview 2的`wasm-component/v1`，只通过版本化`masi:plugin-transform@1.0.0` pure-transform WIT world调用。该world不链接filesystem、network、clock、random、process或CLI capability，不授予preopen、environment或secret；插件只取得经Go/typed caller冻结、由Host校验转交的有界typed input、typed output和调用身份。Fuel/epoch interruption、memory/table/stack、output bytes、deadline和instance count均有上限。

WASI 0.3虽已稳定也不能自动升级；必须新profile和old/new WIT/runtime/security/performance矩阵。

### 4.2 Host-managed Service

`grpc-service/v1`只连接manifest明确声明、已由资格化deployment adapter创建的进程外service endpoint。systemd/Podman/Kubernetes等部署层负责创建、启动、停止和资源隔离；Host不创建process/container、不持有Docker socket，也不实现process/container supervisor或通用scheduler。Host只负责handshake、identity、admission、deadline/cancel、运行观测和drain，业务调用仍使用typed gRPC contract。Service不能继承Host的数据库/P4权限或获得任意argv/env/path。

独立service/Agent不走该runtime；Manager通过deployment/direct adapter控制它们。

## 5. Capability 模型

Capability是封闭、版本化的最小权限，不是任意hook。Host可提供：

- host-owned、已授权的有界input bundle；
- pure transformation input/output；
- 明确只读tool capability；
- statistics definition对应的frozen input和typed Artifact返回；
- 稳定invocation identity、取消和resource-exhaustion状态。

Capability不能提供核心DB连接、P4/Edge client、effect/proposal/decision API、unbounded network、browser code、runtime registration或arbitrary shell。Plugin输出始终不可执行，Manager/Go重新验证schema/digest/scope/generation。

## 6. Invocation 与 Statistics

每次调用绑定invocation/run identity、plugin revision/config/binding generation、input digest、capability set、deadline、resource budget和expected output profile。同key同input/result digest幂等；同key不同digest冲突；old generation late result只审计。

Statistics场景中，Host只把definition ID路由到当前active binding内的exact definition revision/digest；随后校验Go冻结的`StatisticsInputBundleV1`完整身份闭包，包括run/request/plugin/config/binding generation、definition revision/digest、source revision/profile/generation/epoch/sequence、window/coverage/quality、scope/data class、semantic frozen digest和full-bundle digest。校验通过后运行pure-transform conformance/production plugin并返回`PluginStatisticsArtifactV1`。Host不创建schedule/run、不扫描DB、不持久化current、不向Prometheus动态注册业务series，也不解释display为UI代码。

队列仅是内存、有界、从Go durable run派生的执行队列；Host重启后由Go沿原run reconcile，不能成为第二durable ledger。

## 7. 生命周期与撤销

Canonical lifecycle由Manager保存；Host只执行desired binding并报告observed状态。公开lifecycle使用封闭的registered/verified/staged/shadow/active/draining/disabled/revoked/failed/quarantined/stale/unqualified集合；Host内部phase不得覆盖Manager的canonical事实。

Activation先验证exact artifact/capability/resource；Wasm由Host实例化，service则由deployment adapter启动并返回exact endpoint/identity，Host随后连接并完成健康读回。普通activation保持generation单调；延迟到达的旧generation refresh在改变active pointer前被fence。向低generation切换只能使用typed `RollbackBinding`，并同时校验当前`from_generation`、独立authorization digest、fresh exact target envelope、manifest rollback compatibility和此前active后处于draining的target；回滚后更高generation仍可按单调路径roll forward。旧/new generation并存只在受控切换窗口，invocation按binding generation单路分派。Drain/revoke还校验actor/reason、expected envelope digest和authorization digest。Revoke停止新调用、取消in-flight、释放active definition并永久fence该generation；Wasm不再实例化执行，Host向service发送有界typed disable且不会吞掉失败，实际service终止仍由deployment adapter负责，late output被fence。Host每30秒执行trust freshness reconcile，过期或stale binding会被fence并有界drain；restart storm按profile进入quarantine，不无限拉起。

## 8. 安全与资源隔离

- Host与插件没有core DB、P4、Edge、OIDC admin credential；
- artifact、manifest、config、path/URL均按不可信输入，no symlink/path traversal/executable expansion；
- network默认deny，允许目标绑定capability ID和mTLS identity，不接受plugin提供endpoint/credential；
- per-binding/global CPU、memory、PID、FD、disk、fuel、threads/tasks、queue、bytes、deadline、retry和retention有上限；
- crash/OOM/hang只隔离对应binding，核心Go/P4/Inference/Web继续；
- logs/metrics低基数且脱敏，plugin/result/dimension不成为无界label；
- Host API只接受Manager identity，浏览器/LLM/通用plugin不能调用lifecycle管理。

## 9. 启动与健康

- startup：验证Host image/profile/Wasmtime/WASI/WIT、trust bundle、resource controller和Manager identity；默认deny-all；
- readiness：control boundary和至少deny-all runtime可工作，不要求每个plugin健康；
- per-binding readiness：exact artifact/runtime/capability已验证并可接受调用；
- liveness：invocation scheduler和runtime monitor取得进展，不因单plugin失败重启整个Host；
- shutdown：停止新binding/invocation，bounded drain/cancel并回报observed state，不改Manager canonical事实。

## 10. 故障与恢复

- Manager断连：不扩权，不接受新generation；现有binding按lease/revocation freshness profile决定有限继续或drain；
- artifact/config/capability drift：fence对应binding；
- Wasm trap/fuel/memory/OOM：调用失败并计入circuit，不影响其他binding；
- service crash/hang：Host标记对应binding unavailable并回报observed failure；只有deployment adapter可按canonical desired state和有界预算重启，预算耗尽后quarantine；
- timeout/cancel response loss：Manager/Go查询原run/invocation，不创建新identity；
- Host crash：内存queue丢失可由Go durable ledger重建，plugin output不成为current；
- revoke与late result竞争：generation/revocation fence阻止投影；
- supply-chain verification不可用/过期：新activation HOLD，不从公网补依赖。

## 11. 模块黑盒 E2E 验收范围

必须真实启动Rust Host release binary/OCI和pinned Wasmtime；可使用Fake Manager、Fake capability provider以及signed/invalid Wasm/service/statistics fixtures。验收覆盖：

- manifest/digest/publisher/kind/version/config/capability正负例；
- Wasm/WIT和Host-managed service typed invocation；
- statistics frozen input/Artifact/display contract与资源上限；
- activation/generation/drain/revoke/rollback/late result；
- pure-transform的clock/random/process/preopen/network/filesystem/secret拒绝，以及DB/P4/effect权限负例；Host-managed service由deployment adapter创建、Host无process/container/Docker-socket编排权；
- CPU/memory/PID/FD/disk/fuel/deadline、crash/OOM/hang/restart storm；
- Manager/Host restart、queue重建、核心隔离、性能和soak。

Analysis的业务A2A/MCP不经Host应作为明确负向边界验证，但Analysis功能不属于Host模块实现。

## 12. Module Complete 判定

Host只有在两个首期runtime profile、封闭kind/capability、安全隔离、lifecycle/fence、statistics conformance、故障/资源/性能和真实OCI E2E全部完成后才可Module Complete。只运行Wasm hello-world、schema校验、fake Host或未证明零core access均不足。

当前机器派生结论来自 immutable run `plugin-host-formal-20260821-004`，source-tree digest为`sha256:965eae8ee05abed795deb0c6f8b5820e00bc12559fea18260e4776ec533eb868`。19项required gate均为PASS：真实release/OCI启动、两种runtime黑盒、严格manifest/statistics contract、11类真实service故障与独立Wasm fault matrix、59.801%行覆盖率、cargo audit/deny、Valgrind、三轮性能矩阵、真实饱和、SBOM/Trivy/Cosign和60秒warmup加4×900秒正式soak全部完成。soak记录2,368,993次成功调用、2,283,115次有类型饱和拒绝、0次未分类失败、0次trust refresh失败；service crash与restart recovery oracle均为true，368个资源样本内queue/in-flight最终归零且leak阈值通过。`latest.json`绑定的summary digest为`sha256:bef1a250afdd7cceef6f56065f7fdae627f2795f49db970e7e74801a34de8d50`，open finding与open P0均为0，故`overall_module_complete=true`。

该结论是operational completion，不是`MODULE PASS`或production qualification。正式Go Plugin Manager/PostgreSQL↔Host pairwise、扩展strict input bundle的Go producer wire资格、core peak相对退化、protected release baseline、production-ha和九模块global gate仍分别为`HOLD/NOT_RUN`，summary保持`qualification=NOT_QUALIFIED`。审计历史原样保留：`-001`因49.696%覆盖率失败；`-002`虽通过当时runner，但随后独立语义审计发现definition/input fence、真实饱和/故障与manifest/lifecycle证据不足，因此撤回完成结论；修补后`-003`在长跑前因rustdoc broken-link gate失败；修复文档告警后以新identity执行的`-004`才是当前latest。任何失败或撤回run均未被覆盖、改名或冒充PASS。
