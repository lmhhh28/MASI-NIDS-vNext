# Rust Edge Agent (`MOD-EDGE-001`)

`masi-edge` 是 MASI-NIDS-vNext 的独立 Rust Edge Agent。每个已分配 target 由一个进程内 `TargetActor` 管理；该 actor 是该 target 唯一长期 P4Runtime `StreamChannel` owner、唯一生产 P4Runtime read/write client，以及 telemetry source、event-time window、source/input/result WAL、central inference route 和 effect journal/readback 的唯一 Edge owner。

按 Owner 冻结的 `DEC-044`，当前实现的 operational 状态为 **Module Complete**；它与资格等级正交。总体资格仍为 `HOLD/NOT_QUALIFIED`：`DEC-001` 尚未冻结生产绝对硬件、N-target、流量/窗口/Event 速率和延迟阈值，受保护发布基线未形成，正式 pairwise/system 也依开发顺序未运行。这些资格限制不改写实际启动/测试结果，也不构成 operational blocker。完成结论必须由同一完整 module-gate run 的真实 binary/OCI、公开边界、故障、安全、性能和 `DEC-042` 60 秒 warmup + 3,600 秒正式 soak 证据，加上 open P0=0 的 findings registry 共同重派生；当前机器事实以 `evidence/module-gates/latest.json` 指向且通过公开 validator 的不可覆盖 run 为准。

## 所有权边界

- `TargetSupervisor` 校验 assignment ledger、不可复用 lease/generation/election range，并创建最多 32 个隔离 `TargetActor`。旧 lease/revoke 后 actor 只读；新 assignment 必须使用严格更高 election floor，handoff 收敛到一个 active StreamChannel。
- `P4Session` 只允许 `Capabilities`、`StreamChannel`、pipeline `VERIFY`、`Read` 和 `Write(CONTINUE_ON_ERROR)`；拒绝 pipeline mutation、未知 target capability version、P4Info/device-config/cookie drift、非资格 atomicity 和第二 writer。
- telemetry 使用 P4 bounded aggregate dual bank/epoch/snapshot。freeze、selector flip、exact readback、clear、finalize 均落 source WAL；Digest/PacketIn 只是显式丢失语义的 supplemental hint，不是完整逐包流。Digest 必须先 fsync durable record 才 ACK，PacketIn 上限 2,048 bytes 且没有虚构 ACK。
- event-time window 只把 final、valid、identity-compatible record 送入 central inference。late/gap/reset/stale/partial 不补零、不重开已 final window。
- production inference 只有 `inference-central-grpc-batch/v1` mTLS batched-unary 路径。每个 shard 使用 Go-owned exact incarnation/operation/scope/pool/binding/route epoch 与 startup/pool-observation/binding/profile digest；CAS 后的 `inference-committed-binding/v1` 完整 tuple 先落 CanonicalActive WAL 再恢复发送，重复 tuple 幂等且不重复追加，变化 tuple 被 fence。同一 generation 的有界重试可切换到 binding readback 冻结的最多 64 个等价 worker 之一；result 逐记录复核完整 route/WAL/profile fence、eligible worker pair、attempt identity 和执行时序，跨 generation、未读回 worker 或 digest drift 会 fail closed 为 HOLD。同代最多三次、同 request/batch digest 重算，且所有 attempt/backoff 共用单一 monotonic deadline；只有 `UNAVAILABLE`、`RESOURCE_EXHAUSTED`、`ABORTED` 可重试。没有 Edge-local、旧模型、CPU/CUDA 自动切换或 UDS/SHM fallback。
- PostgreSQL Event commit ACK 是 source/input/result canonical cursor 前进的唯一依据。Edge 无数据库凭据，Go Control 是核心事实 writer。
- effect 流程固定为 preflight/compiled digest → durable journal → P4 write → exact readback/reconcile → result → Go canonical ACK/checkpoint。response loss 后先 exact readback，不能盲目重写。rule installation、counter match 和 packet/action outcome 保持分层，Edge 不把 counter 增长解释为处置成功。
- 所有外部 gRPC 强制 mTLS、精确 DNS SAN、独立 credential reference 和 endpoint allowlist；动态 P4/inference credential registry 与静态专属 Control sink identity 相互隔离，所有 private-key path 不可复用；无 plaintext、redirect、proxy 或 metadata endpoint fallback。
- 五类 WAL 使用相同的 36-byte checksummed header 和 durable Unix-ms admission time。最老未 checkpoint record 超过 86,400 秒，或重启时无法证明时钟单调性，target 进入 sticky `HOLD`、关闭 P4 session，且不删除、不覆盖、不推进行为边界；只能停 actor 后走显式人工离线恢复。

公开契约唯一来源是：

- `../contracts/edge/v1/edge.proto`
- `../contracts/inference/v1/inference.proto`
- `../contracts/inference/v1/profile.json`
- `../contracts/edge/v1/error-semantics.json`
- `../contracts/target/v1/schema.json`
- `../contracts/p4runtime/v1/p4runtime.proto`
- `../contracts/profiles/v1/p4runtime-edge-compatibility.json`
- `../contracts/profiles/v1/rust-edge-agent.json`
- `../contracts/profiles/v1/qualification-soak-3600s.json`
- `../contracts/evidence/command/v1/schema.json`
- `../contracts/evidence/edge-blackbox/v1/schema.json`
- `../contracts/evidence/v1/edge-module-schema.json`
- `../contracts/evidence/module-findings/v1/schema.json`
- `../contracts/evidence/soak/v1/schema.json`
- `../contracts/evidence/traceability/v1/schema.json`
- `../contracts/golden/edge/`
- `../contracts/golden/evidence/edge-traceability-v1.json`
- `../contracts/golden/evidence/edge-command-execution-v1.json`
- `../contracts/golden/evidence/edge-blackbox-v1.json`
- `../contracts/golden/evidence/edge-module-v1.json`
- `../contracts/golden/evidence/edge-oci-startup-v1.json`
- `../contracts/golden/evidence/edge-oci-not-run-v1.json`
- `../contracts/golden/evidence/edge-deep-check-v1.json`
- `../contracts/golden/evidence/edge-deep-not-run-v1.json`
- `../contracts/golden/evidence/edge-supply-verification-v1.json`
- `../contracts/golden/evidence/edge-supply-failure-v1.json`
- `../contracts/golden/evidence/edge-supply-not-run-v1.json`
- `../contracts/golden/evidence/module-findings-v1.json`
- `module-findings.json`

`tests/contract_golden.rs` 对 assignment、effect、telemetry、完整 inference route/input/result/ACK fence、committed-binding route WAL、source WAL、P4Runtime Digest/ACK/PacketIn 的精确 protobuf bytes 和 SHA-256 执行检查，并验证 target JSON↔protobuf 显式适配与生产代码发出的稳定 reason code。`scripts/validate-public-contracts.py` 另外使用 Draft 2020-12 schema/format checker 验证 target、telemetry、firewall、rule-observation、command-execution、blackbox、module-summary、findings、OCI、deep-check、supply 的 PASS/HOLD/FAIL/NOT_RUN 结构、traceability golden 及 65 个 unknown-major/unknown-field/plaintext/fallback/path-traversal/false-PASS/缺失制品、完成篡改与 gate/profile 漂移负例，并核验 inference profile 的有界 admission/retry、完整 handshake 字段集与禁止 fallback 矩阵。

## 构建与运行

稳定工具链固定为 Rust 1.97.1：

```bash
cd edge-rs
cargo build --locked --release --bin masi-edge
target/release/masi-edge --config /absolute/path/to/edge.json
```

配置必须是小于等于 1 MiB 的普通非 symlink JSON，schema 为 `edge-config/v1`，未知字段被拒绝。TLS PEM 也必须是普通非 symlink 文件，私钥不得授予 group/other 权限。`data_dir` 是唯一需要持久可写的 target-scoped 根目录；配置与证书应只读挂载。所有 queue、RPC deadline、message、WAL、window、target 和 rule 上限均必须显式配置，并受 `RuntimeLimits::validate` 与 `rust-edge-agent/v1` profile 双重约束。

进程收到 SIGTERM/SIGINT 后停止接收 assignment、drain actor、关闭唯一 session 并以 0 退出。`masi-edge-probe` 使用真实 mTLS `GetStatus`；它校验 status schema、process state、edge instance/config digest 和可选 target，不能用 TCP-only probe 代替。

## 可复制门禁

完整模块门禁入口会实际执行 public contract/golden 负例、需求追踪校验、格式、Clippy、unit/property、真实 binary 公共 mTLS 黑盒、Linux ENOSPC/FD exhaustion、release 性能 rehearsal、release build、OCI smoke、Miri/ASan/TSan 和供应链门禁：

```bash
cd edge-rs
scripts/run-module-gates.sh
```

脚本退出码以 operational completion 为准：全部完成条件通过时返回 `0`，即使资格 summary 因 DEC-001、dirty tree 或受保护基线仍为 `HOLD/NOT_QUALIFIED`；无硬失败但真实启动/必需测试未运行或受阻时返回 `2`；测试、证据绑定或供应链硬失败返回 `1`。`MASI_EDGE_SKIP_OCI=1`、`MASI_EDGE_SKIP_DEEP=1` 或 `MASI_EDGE_SKIP_SUPPLY=1` 只用于显式诊断，runner 会生成结构化 `NOT_RUN` command/evidence，而不是伪造 PASS，且必然派生为 `INCOMPLETE`。

等价的分项命令：

```bash
cargo fmt --all -- --check
cargo clippy --all-targets --locked -- -D warnings
python3 scripts/validate-public-contracts.py --repo ..
python3 scripts/validate-traceability.py --repo .. --manifest requirements-traceability.json
python3 scripts/test-soak-validator.py --repo ..
python3 scripts/test-module-gate-runner.py --repo ..
MASI_EDGE_EVIDENCE_DIR="$PWD/evidence/module-blackbox" cargo test --all-targets --locked
MASI_EDGE_RUN_OS_FAULTS=1 MASI_EDGE_EVIDENCE_DIR="$PWD/evidence/module-blackbox" \
  cargo test --locked --test module_blackbox os_ -- --ignored
cargo build --locked --release --bins
MASI_EDGE_EVIDENCE_DIR="$PWD/evidence/performance-rehearsal" \
  cargo test --release --locked --test module_blackbox \
  bounded_rule_activation_matrix_rehearsal -- --exact
MASI_EDGE_EVIDENCE_DIR="$PWD/evidence/oci-smoke" scripts/run-oci-smoke.sh
scripts/run-deep-checks.sh
scripts/run-supply-chain.sh
```

深度 Rust 检查使用冻结 commit `c98d0cb27cc63afdd62602a52eb4feb8a1c682dd` 的 nightly 作为补充工具，稳定编译工具链仍是 1.97.1：

```bash
cd edge-rs
scripts/run-deep-checks.sh
```

深度脚本只对 Rust library 执行 Miri（允许隔离外的真实临时 filesystem）、ASan 和 TSan，其 PASS 不外推到黑盒或整体模块。黑盒测试始终启动 Cargo 提供的真实 `masi-edge` 子进程，并通过环回隔离网络上的独立 deterministic mTLS fake P4Runtime/Central/Go 邻居访问公开边界；fake 不替代 Edge actor、compiler、window、WAL、route 或 journal。这是独立 MODULE 测试，不是正式 pairwise/system integration。

黑盒覆盖包括：2-target pipeline 和单一 session；P4 disconnect、pipeline mismatch 的 target 隔离/重连与 StreamChannel 清理；lease expiry 和高 election handoff；telemetry dual-bank/window 与 source→input→result→canonical ACK；Digest/PacketIn durable-before-ACK 及 pending-ACK recovery 上限；committed-binding digest/deadline/重复/篡改/崩溃恢复与 WAL watermark；central full-pool bounded retry/no fallback、精确 retry code 与跨 attempt/backoff 全局 deadline；late/drifted result fence；Go ACK loss；P4 write response loss、exact readback 和 crash recovery；完整 WAL checksum corruption、跨 segment 时钟回退、future record、24h retention sticky HOLD；WAL/identity set 满时不覆盖未 ACK 记录；rule installation/direct counter/eligible counter 分层；0/128/1,024/4,096 rule rehearsal；0/1/2/32 target implementation bound；mTLS/plaintext/identity reuse/metadata endpoint 负例；真实 tmpfs ENOSPC、RLIMIT_NOFILE；四阶段短时 soak rehearsal。

正式 soak 只在有连续 3,660 秒预算的专用环境中作为完整 module gate 运行：

```bash
cd edge-rs
MASI_EDGE_FORMAL_SOAK=1 scripts/run-module-gates.sh
```

正式模式严格消费 `qualification-soak-3600s/v1`：60 秒 warmup，依次执行 900 秒 steady、peak、saturation、recovery-or-activation，并以 10 秒间隔采集真实 Edge 进程 CPU/RSS/FD/thread/queue、source/input/result WAL、canonical commits 与 P4 stream 计数。运行通过公开 `RenewTarget` 边界续租并记录次数；claim scope 固定本次 runtime queue/WAL 上限并逐样本执行，RSS/FD/thread 在 DEC-001 未冻结时明确为 observational、不得冒充绝对阈值。证据同时校验 claim digest、wall/monotonic clocks、每阶段起止窗口、样本覆盖、零 gap/error/resource violation、P4 reconnect 和 cleanup。缺少完整执行时，短时测试只写 `REHEARSAL/HOLD/NOT_QUALIFIED`；即使完整执行，在 DEC-001 绝对阈值未冻结时仍只能 `MODULE/HOLD/NOT_QUALIFIED`。

## OCI

Dockerfile 的 builder/runtime 均按 manifest digest 固定；构建要求离线 vendor named context 和真实 source revision。通常直接使用 `run-oci-smoke.sh`；等价构建形状如下：

```bash
vendor_dir="$(mktemp -d)"
CARGO_NET_OFFLINE=true cargo vendor --locked --versioned-dirs \
  --manifest-path edge-rs/Cargo.toml "$vendor_dir"
docker build --platform linux/amd64 --network none --provenance=false --sbom=false \
  --build-context "cargo_vendor=$vendor_dir" \
  --file edge-rs/Dockerfile \
  --build-arg "SOURCE_REVISION=$(git rev-parse HEAD)" \
  --build-arg "SOURCE_TREE_DIGEST=<deterministic-edge-contract-source-tree-sha256>" \
  --tag masi-edge:module-smoke .
```

最终镜像只有 `masi-edge` binary，默认 `65532:65532`，无 baked-in config/secret。`run-oci-smoke.sh` 以 read-only rootfs、`cap_drop=ALL`、`no-new-privileges`、pids/memory/CPU/tmpfs 限额启动真实镜像，仅给 `/var/lib/masi-edge` 可写 volume；随后用不同 client leaf 执行 mTLS `GetStatus` 并验证 SIGTERM exit 0。`inspect-oci-archive.py` 对单一 `linux/amd64` archive、exact tag、content-addressed config/layers、非 root user/entrypoint、成员类型/路径/重复项及展开资源上限做独立 fail-closed 检查，并将 index/config/inspection 证据嵌入 OCI parent 供 traceability 逐摘要复核。

`run-supply-chain.sh` 为每次 run 创建不可覆盖目录，保存离线 source/vendor/base/tool/image bundle，使用无网络 `cargo --offline --locked` 重建并要求 binary digest 相等，生成 source/image SPDX，使用冻结 Trivy DB 与 `tools.lock.json` 固定 OCI digest/content digest/file count 的 checks bundle 执行 vulnerability/secret/config 扫描，并对 release manifest 做 Cosign 正向、篡改和错误 publisher 验证。config scan 必须从只读离线 policy cache 加载；下载、缺失 cache 或 Trivy embedded-check fallback 都会 fail closed。公开 `rust-edge-agent-release/v1` schema/golden 冻结 8 MiB manifest、64 MiB JSON/checks、32,768 文件和 8 GiB bundle、65,536 tar member/8 GiB 展开等上限；实际 bundle 文件集合、SHA256SUMS 与 manifest 必须形成完全相等的闭包，symlink/special/traversal/duplicate 条目均拒绝。`test-supply-input-safety.py` 还会以独立负例验证 checks/inventory symlink、路径逃逸/非规范/超长以及 tar traversal/symlink/duplicate/special-file 均 fail closed。OCI manifest、archive index/config、完整 archive digest、checks bundle、SBOM、scan、Cargo.lock registry closure 与 exact RepoDigest 分别绑定。dirty tree 只能得到 `HOLD/NOT_QUALIFIED`；本地 Owner bootstrap key 的限定 PASS 也不替代受保护发布签名体系。

## 证据与资格状态

`scripts/run-module-gates.sh` 在 `evidence/module-gates/runs/<run-id>/` 生成命令日志及对应的 `edge-command-execution/v1` sidecar、每个 black-box JSON、OCI/deep/supply evidence、`traceability-summary.json` 和 `gate-summary.json`，并原子更新只含路径和 digest 的 `latest.json`；历史 run 不覆盖。sidecar 将 exact argv/exit code/时间/source-tree 与日志 bytes+SHA-256 绑定。`requirements-traceability.json` 将全部 26 个适用需求 ID 绑定到精确 run-relative evidence、test symbol、scenario、producer command 和资格上限，并以 catalog 冻结每份 JSON 的 schema/test ID/需求集合；Rust target 只接受 digest 已绑定日志中的精确 `test <qualified-name> ... ok` 记录，脚本 target 保留其真实 PASS/HOLD/NOT_RUN。完成 validator 会独立重算 12 个 operational criteria、15 个直接 evidence digest、findings registry digest 与 P0/blocker 数，拒绝手工改写 `completion` 或 `overall_module_complete`。聚合器还会拒绝失败 sidecar/JSON、未知 schema、模糊或伪造测试文本、digest/catalog 漂移、缺失、路径穿越和无关 test ID。证据绑定 source revision/tree/dirty-state digest、需求基线、Cargo.lock、contract/profile、trace manifest 与 release binary；dirty tree evidence 只能说明实际被测工作区，不能替代受保护提交/tag 的审计发布基线。

以下项目在外部决策或环境满足前必须保持原状态：

- `DEC-001` 绝对性能与 N-target 资格：`HOLD/NOT_QUALIFIED`。
- 任一具体 run 未执行完整 3,600 秒 measurement 时，`DEC-042` soak 为 `NOT_RUN/NOT_QUALIFIED`；完整执行且正确但 DEC-001 仍未冻结时为 `MODULE/HOLD/NOT_QUALIFIED`。
- conditional gNMI/mirror profiles 未触发：gate summary 从追踪 manifest 生成四条独立 `applicability=NOT_APPLICABLE`、`result=NOT_RUN` 和稳定理由，不隐式启用。
- 正式 P4↔Edge、Edge↔Central、Edge↔Go/effect pairwise 及 system E2E：本模块阶段不执行、不宣称 PASS。
- `overall_module_complete` 只反映 `DEC-044` operational completion；当 12 项完成条件、15 个 evidence digest、open P0=0 和真实启动/测试 blocker=0 可重派生时为 `true`。全仓模块同时完成、受保护基线/tag/digest 与正式硬件/availability/deployment-tier evidence 形成前，总体资格仍为 `HOLD/NOT_QUALIFIED`，正式 pairwise/system 仍为 `NOT_RUN`。
