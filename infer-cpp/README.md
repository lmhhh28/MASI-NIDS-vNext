# Central Inference Gateway (`MOD-INF-001`)

`masi_inference_gateway` 是 MASI-NIDS-vNext 的独立 C++ Central Inference Gateway。它是一个无状态 mTLS gRPC 服务，只暴露 `GetBinding` 和 `Infer` 两个公开边界。Gateway 不连接 PostgreSQL、不访问 P4、不拥有业务状态或 durable queue。**Gateway 自身没有任何执行引擎**：唯一执行面与唯一延迟型 batch scheduler 都是固定资格 profile 的 Triton；Gateway 只做合同/身份/配额/结果适配。

## 当前状态与完成判定

按 `DEC-044`，operational Module Complete 由 `scripts/run-module-gates.sh` 机器派生；唯一权威字段是本次 exact-source run 的 `gate-summary.json#overall_module_complete`。代码审计登记的28条finding均已CLOSED、open P0/P1/P2为0。完整门禁必须实际运行release binary、真实Triton/Gateway mTLS边界、真实OCI、ASan/UBSan/TSan、网络隔离供应链重建和3,600秒合格soak；任一证据缺失或schema不符都会阻断完成。本次connected integration修改了wire/profile、Gateway smoke/export与fixture closure，故已有summary只保留历史，current dirty source须重跑完整module gate。

`overall_module_complete=true` 不等于 production qualified。dirty tree、`DEC-001` 绝对生产阈值、受保护提交/tag 与未来 pairwise/system 可以继续保持 `HOLD/NOT_QUALIFIED` 或 `NOT_RUN`，但不得改变已执行门禁的原始结果。

## 所有权边界

- Gateway 是无状态的；它不持有 durable queue、不写数据库、不推进 canonical cursor。
- **Gateway 不链接 ONNX Runtime、不持有任何进程内推理会话**。CPU execution provider 只运行在固定 Triton 的 `onnxruntime` backend 内。Triton 不可用即 startup 失败（`return 1`），运行期无本地回退。
- 一个通过 admission 的批就是**一次** Triton `ModelInfer`（shape `[N,6]`），响应 `[N,2]` 按行切分回每条记录。Triton 的 dynamic batcher 是唯一允许为凑批而等待的组件。
- 在线推理唯一生产路径是 `inference-central-grpc-batch/v1` mTLS batched-unary gRPC。Edge 是每个 shard 的唯一 canonical input router。
- 启动时通过 immutable startup envelope 显式选择 `model-runtime-central-cpu/v1` 或 `model-runtime-central-cuda/v1`。当前构建只资格化 CPU profile，选择 CUDA 会以 `RUNTIME_PROFILE_UNSUPPORTED` fail closed。
- Triton 固定 `model-control-mode=none`、只读 repository、严格 readiness。startup 对 **live ModelConfig ↔ pin 住的 `config.pbtxt` ↔ 冻结 profile** 做三方比对（`max_batch_size`/`dynamic_batching`/`preferred_batch_size`/queue delay/queue size/显式 `instance_group`/张量名与 dtype/dims），并校验 `ModelReady` 与 server 版本。
- output adapter 是按 `adapter_id` 选中的**已资格化确定性实现**，其参数（`score_domain`、`class_order`、`axis`、`top_k`、alert/abstain 阈值和 OOD policy）全部来自 digest-pinned closure 的 `role=bundle-manifest` 成员，并与 envelope 声明的 feature/label/adapter digest 交叉校验；不一致即 fail closed。r3 固定 `max-probability-below-threshold=0.55`，命中即 `out_of_distribution=true` 且强制 abstain。规则与 canonical `float32-le` 分数编码冻结在 `contracts/inference/v1/profile.json#output_adapter_binding`。
- 每条记录的 `input_digest` 在 admission 阶段被重算校验，不是透传值。
- readback 由**对 Triton 的真实观测**构造（ServerMetadata/ModelMetadata/ModelConfig 的规范化投影 + 已验证的 closure/bundle digest），并填齐 `pool_observation_digest` 与 `binding_digest`；它不是 envelope 的回声。`GetBinding` 对应证据 schema 的 `get_loaded_model`/`get_pool_status` 字段（保留 wire 方法名，映射在此显式记录）。
- 29 维 result fence：`request_id`、`input_id`、`event_idempotency_key`、`input_digest`、`model_control_incarnation_id`、`operation_id`、`scope`、`shard_id`、`route_epoch`、`logical_pool_id`、`pool_generation`、`binding_generation`、`startup_envelope_digest`、`pool_observation_digest`、`binding_digest`、`model_revision_digest`、`model_bundle_digest`、`feature_contract_digest`、`label_contract_digest`、`output_adapter_digest`、`wire_profile_digest`、`runtime_profile_digest`、`optimization_profile_digest`、`worker_id`、`worker_digest`、`worker_attempt_id`、`source_input_result_WAL_sequence`、`source_window_identity`、`trace_id`。未知/旧/跨 generation → `RESULT_IDENTITY_MISMATCH`。
- 禁止 fallback 矩阵：`edge_local_inference=false`、`old_model=false`、`automatic_cpu_cuda_switch=false`、`uds_or_shared_memory=false`、`json_or_http=false`、`gateway_local_inference=false`。
- 同一 pool/binding generation 内，同 `request_id` + 同 batch input digest 会被**重算**并给出新的 `worker_attempt_id`（at-least-once 计算语义）；同 `request_id` + 异 input digest 返回 `RESULT_DIGEST_CONFLICT`。
- 有界资源：每个 route target 至多 1 个在途批，进程级 `max_in_flight`，超限返回 `RESOURCE_EXHAUSTED` 并携带稳定原因；请求 deadline 与取消通过 `ClientContext::FromServerContext` 传播给 Triton。
- 所有外部 gRPC 强制 mTLS 且验证 **CA 链 + 精确 SAN allowlist**；无 SAN 或非白名单 SAN 返回 `UNAUTHENTICATED`。Gateway↔Triton 通道要么配置 `triton_tls_*`（四项齐全），要么 endpoint 必须是 loopback，否则 connect 阶段即拒绝。
- 未知 major、未知 dtype/shape、NaN/Inf、oversize batch、缺失/错误 `input_digest` 均在 admission 阶段 fail closed，不进入模型执行。
- 配置未知字段被拒绝；`client_san_allowlist` 必填非空。
- repository closure 必须是 digest-pinned、read-only、无 symlink 的精确闭包；digest 前像绑定 `role` 与 `rel_path`，extra model/version/config/backend 被拒绝。

公开契约唯一来源是：

- `../contracts/edge/v1/edge.proto`
- `../contracts/inference/v1/inference.proto`
- `../contracts/inference/v1/profile.json`
- `../contracts/evidence/v1/inference-module-schema.json`
- `../contracts/evidence/central-inference-blackbox/v1/schema.json`
- `../contracts/evidence/central-inference-numeric/v1/schema.json`
- `../contracts/evidence/central-inference-readback/v1/schema.json`
- `../contracts/evidence/central-inference-startup/v1/schema.json`
- `../contracts/evidence/central-inference-supply/v1/schema.json`
- `../contracts/evidence/command/v1/schema.json`
- `../contracts/evidence/module-findings/v1/schema.json`
- `../contracts/evidence/soak/v1/schema.json`
- `../contracts/evidence/traceability/v1/schema.json`
- `../contracts/golden/inference/` (11 golden vectors, digest-pinned in `catalog.json`)
- `../contracts/golden/evidence/central-inference-*.json`
- `module-findings.json`

`tests/contract_golden.cc` 钉住 `InferenceRecord`/`InferenceInputBatch` 的精确 protobuf digest（不再只检查前缀），核对 29 维 result fence dimensions、fallback 矩阵（全 false）与 11 个 golden vectors 的 schema/category/digest，并交叉校验 golden 钉住的 canonical scores/decision/quality/output_digest 等于适配器实现的输出。`tests/property_invariants.cc` 验证 error code 稳定字符串、SHA-256 格式、constant-time digest 比较、tensor layout（oversize/misaligned/overflow/unknown dtype）、NaN/Inf 拒绝、config 未知字段与缺失 `client_san_allowlist` 拒绝、closure extra-member 拒绝、closure digest 绑定 role/path（相同字节换路径必须改变 digest）、bundle-manifest 自校验与参数篡改拒绝、适配器语义（logit/probability 域、baseline 标签、alert/abstain 阈值可判别）以及 1/2/32/256/257/0 记录批的逐记录 admission 与 `input_digest` 校验。`scripts/validate-public-contracts.py` 另外使用 Draft 2020-12 schema 验证所有 inference golden/evidence 结构和负例（unknown major、plaintext、fallback、path traversal、false-PASS）。

## 构建与运行

构建必须使用供应链登记的 gRPC/protobuf。CMake 找不到该版本或版本不符会直接 `FATAL_ERROR`，不会静默回落到系统 gRPC：

```bash
# 一次性：按 registry pin 构建 gRPC v1.82.1（commit acccf84c…）。
# 该脚本与 Dockerfile builder 阶段的 flags 完全一致，且已存在 prefix 时拒绝覆盖。
sudo scripts/build-grpc-toolchain.sh
export MASI_INF_GRPC_PREFIX=/opt/masi-toolchain/grpc-1.82.1

cd infer-cpp
cmake --preset cpu-release
cmake --build build/cpu-release --target masi_inference_gateway
./build/cpu-release/masi_inference_gateway /absolute/path/to/gateway.json
```

配置必须是小于等于 1 MiB 的普通非 symlink JSON，schema 为 `inference-config/v1`，未知字段被拒绝，`client_san_allowlist` 必填非空。TLS PEM 也必须是普通非 symlink 文件，私钥不得授予 group/other 权限。`startup_envelope_path` 指向 immutable 启动信封（必须声明 feature/label/output_adapter/runtime/optimization digest 与 `triton_server_version`），`model_repository_path` 指向 digest-pinned read-only 闭包。进程收到 SIGTERM/SIGINT 后停止接收、按配置 `drain_ms` 等待在途请求，依次销毁 server/health/service/Triton channel 并以 0 退出；成功路径完成这些同步边界后使用 `_Exit(0)`，避开 pinned gRPC/OpenSSL 已复现的进程级全局析构竞态，失败路径仍返回非零。

## 字节级可复现性契约

供应链门禁要求宿主机离线重建的 Gateway 二进制与 OCI 镜像内的二进制**逐字节相同**
（`binary_digest_match=true`）。以下三项是承载该等式的输入，任何一项只改一侧都会立刻破坏它：

| 输入 | 取值 | 为什么会进入二进制 |
|---|---|---|
| gRPC 源码目录 | `/opt/masi-toolchain/grpc-src` | gRPC 把 `__FILE__` 编进 488 处断言/日志字符串 |
| gRPC 安装 prefix | `/opt/masi-toolchain/grpc-1.82.1` | prefix 出现在 15 处编译产物字符串中 |
| nlohmann/json | vendored `third_party/nlohmann/json.hpp` 3.11.3 | 3.11 引入 inline ABI 命名空间，3.10.5 与 3.11.3 生成不同代码 |

nlohmann/json **不从发行版包安装**：ubuntu:22.04 是 3.10.5，宿主机上曾是手工安装、无任何 dpkg
出处的 3.11.3，二者编译结果不同。现在唯一来源是仓库内 vendored 单头文件，digest
`sha256:9bea4c80…03ea6`，已登记在 `contracts/supply-chain/v1/central-inference-vendored-sources.json`；
CMake 在 configure 期比对 digest，`src/envelope.h` 再用 `static_assert` 锁定
`NLOHMANN_JSON_VERSION_*`，确保系统头无法顶替。构建期还断言 builder 的 g++/cmake 版本与登记值一致。

供应链 runner 从固定 source tar 解包新快照，在 builder 容器内以 `--network none` 重建并与 OCI binary 逐字节比较；随后使用 digest-pinned Syft/Trivy/Cosign 离线生成双 SPDX、漏洞/secret/config 扫描、签名、篡改/错误发布者负例、provenance 与 SHA256SUMS exact closure。任一工具或工件缺失均不能 PASS。

## 可复制门禁

完整模块门禁入口会实际执行 public contract/golden 负例、格式/clang-tidy、unit/property/contract golden、numeric golden、release build、OCI smoke、ASan/UBSan/TSan 和供应链门禁：

```bash
cd infer-cpp
scripts/run-module-gates.sh
```

脚本退出码以 operational completion 为准：全部完成条件通过时返回 `0`，即使资格 summary 因 DEC-001、dirty tree 或受保护基线仍为 `HOLD/NOT_QUALIFIED`；无硬失败但真实启动/必需测试未运行或受阻时返回 `2`；测试、证据绑定或供应链硬失败返回 `1`。`MASI_INF_SKIP_OCI=1`、`MASI_INF_SKIP_DEEP=1` 或 `MASI_INF_SKIP_SUPPLY=1` 只用于显式诊断，runner 会生成结构化 `NOT_RUN` command/evidence，而不是伪造 PASS。

等价的分项命令：

```bash
cmake --preset cpu-release
cmake --build build/cpu-release --target contract_golden property_invariants module_blackbox
./build/cpu-release/contract_golden
./build/cpu-release/property_invariants
MASI_INF_E2E=0 ./build/cpu-release/module_blackbox  # skip real-process E2E (exit 77)
python3 scripts/validate-public-contracts.py --repo ..
python3 scripts/run-numeric-golden.py --repo .. --evidence-dir evidence/numeric-golden
cmake --build build/cpu-release --target masi_inference_gateway
MASI_INF_EVIDENCE_DIR="$PWD/evidence/oci-smoke" scripts/run-oci-smoke.sh
scripts/run-deep-checks.sh
scripts/run-supply-chain.sh
```

### 真实进程黑盒 E2E

`tests/module_blackbox.cc` 在 `MASI_INF_E2E=1` 时启动真实 `masi_inference_gateway` 子进程，生成 mTLS 材料，用 contract-consistent Edge client 走公开边界。它**硬要求**一个真实 Triton 服务 digest-pinned 的 r3 闭包；缺失时输出结构化 `HOLD/NOT_RUN` 并以 77 跳过，绝不记 PASS。

```bash
# 1) 起一个服务 pin 住闭包的 Triton（端口任选）
docker run -d --name masi-triton-r3 \
  -p 127.0.0.1:8010:8000 -p 127.0.0.1:8011:8001 -p 127.0.0.1:8012:8002 \
  -v "$PWD/../testkit/fixtures/repositories/masi-ids-window-v1-r3:/models:ro" \
  nvcr.io/nvidia/tritonserver@sha256:75bcfa5b0043898ece3e603c17a5bbbb1c9bddc390563db24312ef59d83735e5 \
  tritonserver --model-repository=/models --model-control-mode=none \
  --disable-auto-complete-config --strict-readiness=true

# 2) 跑黑盒 E2E
MASI_INF_E2E=1 MASI_INF_TRITON_ENDPOINT=127.0.0.1:8011 \
  ./build/cpu-release/module_blackbox
```

覆盖矩阵（全部为公开边界断言）：`GetBinding` readback、单记录 golden 的 canonical scores/decision/output_digest、2/32/256 记录批（每批必须是 **1 次 Triton execution + N 次 inference**，用 `ModelStatistics` 增量判别，因此进程内执行面无法伪装成"走了 Triton"）、257 记录与空批拒绝、未知 major 拒绝、错误 `input_digest` 在执行前拒绝且 Triton 计数不动、重放返回完整结果且 `worker_attempt_id` 变化、同 `request_id` 异输入 `RESULT_DIGEST_CONFLICT`、跨代与 adapter digest 漂移 fenced、过期 deadline 不进入 Triton、plaintext 拒绝、非白名单 SAN 与无 SAN 客户端 `UNAUTHENTICATED`、并发同 route 的 in-flight 上限、SIGTERM exit 0。

故障注入（同一次运行内，后端仍是真实 Triton，只在环回代理上操纵传输层）：后端连接被切断时必须 fail closed（`pool_unavailable`、无结果记录、Triton 执行计数不变、Gateway 进程存活）；后端恢复后同一批必须重新成功且 canonical `output_digest` 完全一致；后端停滞超过 Gateway 自身 `request_deadline_ms` 时由 **Gateway 的上限**（而非调用方 deadline）触发 `deadline_exceeded` 并释放 in-flight 配额；`SIGKILL` 后用同一配置重启，必须复现完全相同的 `binding_digest` 与 `output_digest`。

运行结束写出 `central-inference-module-e2e-evidence/v1`（`MASI_INF_EVIDENCE_DIR`），其中每个布尔位只在对应断言真正通过时被置位。

若 `testkit/fixtures/repositories/masi-ids-window-v1-r3` 需要重建：

```bash
python3 ../testkit/fixtures/models/scripts/generate_fixture.py --revision r3
python3 ../testkit/fixtures/models/scripts/build_repository.py --revision r3 \
  --out ../testkit/fixtures/repositories/masi-ids-window-v1-r3
```

### 深度 C++ 检查

ASan/UBSan 和 TSan 在 library scope 运行：

```bash
cd infer-cpp
scripts/run-deep-checks.sh
```

其 PASS 不外推到黑盒或整体模块。黑盒测试始终启动 CMake 提供的真实 `masi_inference_gateway` 子进程，并通过环回隔离网络上的 mTLS fake Edge 访问公开边界。这是独立 MODULE 测试，不是正式 pairwise/system integration。

### 正式 soak

冻结的 `qualification-soak/v1` profile 固定 60s warmup、4 × 900s 阶段、10s 采样、100 batch requests/s 单调定速与 1/32/128/256 批量。`MASI_INF_SOAK_SECONDS=3600` 只表示合格窗口，warmup 另计，因此真实负载约 61 分钟。驱动通过真实 mTLS 边界持续加压，从 `/proc` 采样 Gateway 自身的 RSS/FD/线程/CPU，并以 Triton 的单调 inference/execution/success counters 侦测 provider reset、把成功记录数绑定到真实执行计数；每一条返回记录都对照 alert/OOD golden digest。PASS 还要求 validator 独立复算四阶段速率与 10s 采样节拍、所有样本可测、零 gap/oracle/error/OOM/restart/resource violation 与清理完整：

```bash
cd infer-cpp
# 正式窗口（负载约 61 分钟；完整门禁另含构建/扫描）
MASI_INF_FORMAL_SOAK=1 MASI_INF_SOAK_SECONDS=3600 scripts/run-module-gates.sh

# 仅验证机制的短跑（只能产出 REHEARSAL）
MASI_INF_E2E=1 MASI_INF_TRITON_ENDPOINT=127.0.0.1:8011 MASI_INF_SOAK_SECONDS=100 \
  MASI_INF_EVIDENCE_DIR=/tmp/soak-ev ./build/cpu-release/module_blackbox
```

窗口不足时驱动只写 `formal-soak-rehearsal.json`（`level=REHEARSAL`、`HOLD`、`NOT_QUALIFIED`），不会写正式 `formal-soak-evidence.json`；门禁要求 `qualified_elapsed_ms >= 3600000` 才可 PASS，否则记 HOLD。即使跑满窗口，DEC-001 绝对阈值未冻结时资格仍受 `absolute_performance=HOLD` 限制。

## OCI

Dockerfile 的 builder/runtime 均按 manifest digest 固定；构建要求真实 source revision。通常直接使用 `run-oci-smoke.sh`。最终镜像只有 `masi_inference_gateway` binary，默认 `65532:65532`，无 baked-in config/secret。`run-oci-smoke.sh` 以 read-only rootfs、`cap_drop=ALL`、`no-new-privileges`、pids/memory/CPU/tmpfs 限额启动真实镜像，随后用独立 client leaf 执行 mTLS probe 并验证 SIGTERM exit 0。

## 证据与资格状态

`scripts/run-module-gates.sh` 在 `evidence/module-gates/runs/<run-id>/` 生成命令日志及对应的 `edge-command-execution/v1` 通用 sidecar、每个 black-box JSON、OCI/deep/supply/soak evidence、`gate-summary.json`，并原子更新只含路径和 digest 的 `latest.json`；历史 run 不覆盖。所有 sidecar、嵌套 latest、供应链、soak 与最终 summary 都在发布前严格校验。`requirements-traceability.json` 将全部 20 个适用需求 ID 绑定到精确 run-relative evidence、test symbol、scenario、producer command 和资格上限。

以下项目在外部决策或环境满足前必须保持原状态：

- `DEC-001` 绝对性能与延迟资格：`HOLD/NOT_QUALIFIED`。
- CUDA profile 未资格化：gate summary 从追踪 manifest 生成 `applicability=NOT_APPLICABLE`、`result=NOT_RUN` 和稳定理由 `CPU_PROFILE_FIRST_RELEASE_ONLY`。
- HA profile 未触发：`SINGLE_FAILURE_DOMAIN_HOST`。
- 正式 Central↔Edge pairwise 及 system E2E：本模块阶段不执行、不宣称 PASS。
- `overall_module_complete` 只反映 `DEC-044` operational completion；全仓模块同时完成、受保护基线/tag/digest 与正式硬件/availability/deployment-tier evidence 形成前，总体资格仍为 `HOLD/NOT_QUALIFIED`，正式 pairwise/system 仍为 `NOT_RUN`。
