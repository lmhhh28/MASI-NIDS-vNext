# Central Inference Gateway (`MOD-INF-001`)

`masi_inference_gateway` 是 MASI-NIDS-vNext 的独立 C++ Central Inference Gateway。它是一个无状态 mTLS gRPC 服务，只暴露 `GetBinding` 和 `Infer` 两个公开边界。Gateway 不连接 PostgreSQL、不访问 P4、不拥有业务状态或 durable queue。Triton 是唯一的延迟型 batch scheduler；Gateway 只做合同/身份/配额/结果适配。

按 Owner 冻结的 `DEC-044`，当前实现的 operational 状态为 **Module Complete**；它与资格等级正交。总体资格仍为 `HOLD/NOT_QUALIFIED`：`DEC-001` 尚未冻结生产绝对硬件和延迟阈值，受保护发布基线未形成，正式 pairwise/system 也依开发顺序未运行。这些资格限制不改写实际启动/测试结果，也不构成 operational blocker。

## 所有权边界

- Gateway 是无状态的；它不持有 durable queue、不写数据库、不推进 canonical cursor。
- 在线推理唯一生产路径是 `inference-central-grpc-batch/v1` mTLS batched-unary gRPC。Edge 是每个 shard 的唯一 canonical input router。
- 启动时通过 immutable startup envelope 显式选择 `model-runtime-central-cpu/v1` 或 `model-runtime-central-cuda/v1`。首期只有 CPU profile 资格化。
- Triton 固定 `model-control-mode=none`、只读 repository、严格 readiness。repository snapshot 只包含 exact binding 及其声明依赖。
- 29 维 result fence：`request_id`、`input_id`、`event_idempotency_key`、`input_digest`、`model_control_incarnation_id`、`operation_id`、`scope`、`shard_id`、`route_epoch`、`logical_pool_id`、`pool_generation`、`binding_generation`、`startup_envelope_digest`、`pool_observation_digest`、`binding_digest`、`model_revision_digest`、`model_bundle_digest`、`feature_contract_digest`、`label_contract_digest`、`output_adapter_digest`、`wire_profile_digest`、`runtime_profile_digest`、`optimization_profile_digest`、`worker_id`、`worker_digest`、`worker_attempt_id`、`source_input_result_WAL_sequence`、`source_window_identity`、`trace_id`。未知/旧/跨 generation → `RESULT_IDENTITY_MISMATCH`。
- 禁止 fallback 矩阵：`edge_local_inference=false`、`old_model=false`、`automatic_cpu_cuda_switch=false`、`uds_or_shared_memory=false`、`json_or_http=false`。
- 同一 pool/binding generation 的 active-active replica 可在有界 retry budget 内重算同一 request identity；跨 generation/model/runtime profile 必须提升 route epoch 并走新 rollout。
- 无损 PostgreSQL failover 保留 `model_control_incarnation_id`；PITR/restore/clone/rewind 必须在开放 writer/ingest 前轮换从未使用的新 incarnation。
- 所有外部 gRPC 强制 mTLS、精确 DNS SAN、peer verification；无 plaintext fallback。
- 未知 major、未知 dtype/shape、NaN/Inf、oversize batch 均在 admission 阶段 fail closed，不进入模型执行。
- 配置未知字段被拒绝；runtime profile 非 `model-runtime-central-cpu/v1` 或 `model-runtime-central-cuda/v1` 被拒绝。
- repository closure 必须是 digest-pinned、read-only、无 symlink 的精确闭包；extra model/version/config/backend 被拒绝。

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
- `../contracts/golden/inference/` (10 golden vectors)
- `../contracts/golden/evidence/central-inference-*.json`
- `module-findings.json`

`tests/contract_golden.cc` 验证 `InferenceRecord`、`InferenceInputBatch`、`InferenceResultRecord` 的精确 protobuf bytes 和 SHA-256 稳定性，并核对 29 维 result fence dimensions、fallback 矩阵（全 false）和 10 个 golden vectors 的 schema/category。`tests/property_invariants.cc` 验证 20 个 error code 稳定字符串、SHA-256 格式与已知向量、constant-time digest 比较、tensor layout 检查（oversize/misaligned/overflow/unknown dtype）、NaN/Inf 拒绝、config 未知字段拒绝和 repository closure extra-member 拒绝。`scripts/validate-public-contracts.py` 另外使用 Draft 2020-12 schema 验证所有 inference golden/evidence 结构和负例（unknown major、plaintext、fallback、path traversal、false-PASS）。

## 构建与运行

```bash
cd infer-cpp
cmake --preset cpu-release
cmake --build build/cpu-release --target masi_inference_gateway
./build/cpu-release/masi_inference_gateway /absolute/path/to/gateway.json
```

配置必须是小于等于 1 MiB 的普通非 symlink JSON，schema 为 `inference-config/v1`，未知字段被拒绝。TLS PEM 也必须是普通非 symlink 文件，私钥不得授予 group/other 权限。`startup_envelope_path` 指向 immutable 启动信封，`model_repository_path` 指向 digest-pinned read-only 闭包。进程收到 SIGTERM/SIGINT 后停止接收、drain 并以 0 退出。

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

`tests/module_blackbox.cc` 在 `MASI_INF_E2E=1` 时启动真实 `masi_inference_gateway` 子进程，生成 mTLS 证书，创建 fake Edge gRPC client，调用 `GetBinding` 和 `Infer`，并测试 oversize/unknown-major/plaintext/wrong-identity 拒绝和 SIGTERM exit 0。未设置 `MASI_INF_E2E=1` 时返回 77（skip），以便 CI 在没有 Triton/ORT 完整栈时仍能运行 contract/property 测试。

```bash
MASI_INF_E2E=1 ./build/cpu-release/module_blackbox
```

### 深度 C++ 检查

ASan/UBSan 和 TSan 在 library scope 运行：

```bash
cd infer-cpp
scripts/run-deep-checks.sh
```

其 PASS 不外推到黑盒或整体模块。黑盒测试始终启动 CMake 提供的真实 `masi_inference_gateway` 子进程，并通过环回隔离网络上的 mTLS fake Edge 访问公开边界。这是独立 MODULE 测试，不是正式 pairwise/system integration。

### 正式 soak

```bash
cd infer-cpp
MASI_INF_FORMAL_SOAK=1 scripts/run-module-gates.sh
```

缺少完整执行时，短时测试只写 `REHEARSAL/HOLD/NOT_QUALIFIED`；即使完整执行，在 DEC-001 绝对阈值未冻结时仍只能 `MODULE/HOLD/NOT_QUALIFIED`。

## OCI

Dockerfile 的 builder/runtime 均按 manifest digest 固定；构建要求真实 source revision。通常直接使用 `run-oci-smoke.sh`。最终镜像只有 `masi_inference_gateway` binary，默认 `65532:65532`，无 baked-in config/secret。`run-oci-smoke.sh` 以 read-only rootfs、`cap_drop=ALL`、`no-new-privileges`、pids/memory/CPU/tmpfs 限额启动真实镜像，随后用独立 client leaf 执行 mTLS probe 并验证 SIGTERM exit 0。

## 证据与资格状态

`scripts/run-module-gates.sh` 在 `evidence/module-gates/runs/<run-id>/` 生成命令日志及对应的 `central-inference-command-execution/v1` sidecar、每个 black-box JSON、OCI/deep/supply evidence、`gate-summary.json`，并原子更新只含路径和 digest 的 `latest.json`；历史 run 不覆盖。`requirements-traceability.json` 将全部 20 个适用需求 ID 绑定到精确 run-relative evidence、test symbol、scenario、producer command 和资格上限。

以下项目在外部决策或环境满足前必须保持原状态：

- `DEC-001` 绝对性能与延迟资格：`HOLD/NOT_QUALIFIED`。
- CUDA profile 未资格化：gate summary 从追踪 manifest 生成 `applicability=NOT_APPLICABLE`、`result=NOT_RUN` 和稳定理由 `CPU_PROFILE_FIRST_RELEASE_ONLY`。
- HA profile 未触发：`SINGLE_FAILURE_DOMAIN_HOST`。
- 正式 Central↔Edge pairwise 及 system E2E：本模块阶段不执行、不宣称 PASS。
- `overall_module_complete` 只反映 `DEC-044` operational completion；全仓模块同时完成、受保护基线/tag/digest 与正式硬件/availability/deployment-tier evidence 形成前，总体资格仍为 `HOLD/NOT_QUALIFIED`，正式 pairwise/system 仍为 `NOT_RUN`。