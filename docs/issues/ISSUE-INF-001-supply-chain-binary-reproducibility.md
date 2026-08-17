# [Central Inference] 修复供应链二进制可复现性缺口（host 与 Docker gRPC `__FILE__` 路径不一致）

- Issue ID：`ISSUE-INF-001`
- 状态：`OPEN`
- 类型：`supply-chain-reproducibility`
- 优先级：`P2`（不阻塞 OCI 启动 smoke 或 formal MODULE soak，但 `supply_chain` 门禁为 `FAIL`，阻止 `overall_module_complete`）
- 模块：`central-inference`（`MOD-INF-001`）
- 创建日期：2026-08-17
- Owner：`待 Owner 决定修复策略`（见下文“决策项”）
- 执行负责人：`待指定 agent`
- 需求基线：[masi-nids-vnext-system-requirements-2026-08-09.md](../masi-nids-vnext-system-requirements-2026-08-09.md)
- 关联要求：`CONTRACT-SUPPLY-001`、`SEC-SUPPLY-001`、`ARCH-REUSE-001`、`MOD-INF-001`
- 关联 finding：本缺口目前**未**登记为 `INF-AUDIT-XXXX` finding；`INF-AUDIT-0012` 已关闭且与本缺口无关。

## 问题陈述

`infer-cpp/scripts/run-supply-chain.sh` 的 `supply_chain` 门禁为 `FAIL`，原因是
Gateway 二进制在“宿主机离线重建”与“OCI 镜像内构建”两条路径下**字节不一致**
（`binary_digest_match=false`）。根因不是源码或依赖版本漂移，而是 gRPC 源码在两条
构建路径下的**克隆目录不同**，导致编译进二进制的 `__FILE__` 字符串（断言、日志、
`assert` 消息中的源文件路径）不同：

| 构建路径 | gRPC 源码目录 | 安装 prefix | 二进制中的 `__FILE__` 路径 |
|---|---|---|---|
| 宿主机离线重建 | `/opt/masi-toolchain/grpc-src` | `/opt/masi-toolchain/grpc-1.82.1` | `/opt/masi-toolchain/grpc-src/...` |
| Docker 镜像构建 | `/tmp/grpc`（Dockerfile `git clone`） | `/opt/masi-toolchain/grpc-1.82.1` | `/tmp/grpc/...` |

两条路径都使用同一个 pinned commit
`acccf84c0df20487d64101f528e5d426541ca4e5`（gRPC v1.82.1）、同一
`MASI_INF_GRPC_PREFIX=/opt/masi-toolchain/grpc-1.82.1`、同一编译器/标志
（`-DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_STANDARD=20`，`SOURCE_DATE_EPOCH` 已固定）。
唯一变量是 gRPC 源码的构建目录路径，它通过 `__FILE__` 宏渗入二进制，破坏字节级可复现性。

门禁逻辑（`run-supply-chain.sh`）：

1. 用宿主机 toolchain 离线重建 Gateway，得
   `rebuilt_binary_digest = sha256(masi_inference_gateway.offline-rebuild.bin)`；
2. 从已构建的 OCI 镜像提取 Gateway，得
   `image_binary_digest = sha256(masi_inference_gateway.image.bin)`；
3. `binary_digest_match = (rebuilt_binary_digest == image_binary_digest)`。
   当前为 `false` → `supply_chain` 结果 `FAIL`。

## 当前证据（截至 2026-08-17）

- 复现命令：
  `cd infer-cpp && MASI_INF_SKIP_OCI=1 scripts/run-module-gates.sh`
  （或直接 `scripts/run-supply-chain.sh`）
- 最近一次完整 gate run（`run_id=oci-close-0012-0817` 与
  `run_id=formal-soak-mod-0817`）中 `supply_chain` 均为
  `{result: FAIL, qualification: NOT_QUALIFIED}`，`supply_chain_evidence: null`。
- 宿主机 gRPC 路径：`/opt/masi-toolchain/grpc-src`（源码）、
  `/opt/masi-toolchain/grpc-1.82.1`（安装 prefix）。
- Docker gRPC 路径：`/tmp/grpc`（Dockerfile 第 37 行 `git clone ... /tmp/grpc`）。
- 供应链组件登记：
  [contracts/supply-chain/v1/central-inference-cpu-components.json](../../contracts/supply-chain/v1/central-inference-cpu-components.json)
  （gRPC C++ `ADOPT` v1.82.1，commit 已固定）。
- 可复现性缺口记忆：
  `host gRPC built at /opt/masi-toolchain/grpc-src vs Docker /tmp/grpc → 不同 __FILE__ 路径 → binary_digest_match=false → supply-chain gate FAIL`。

## 决策项（需 Owner 批准）

任何修复策略都可能改变 Gateway 二进制 digest，进而可能改变供应链契约中固定的
binary digest 或离线重建基线。按项目规则，`contracts/` 冻结件的语义变更须走**新
revision** 并由 Owner 批准，不得原地语义修改。请在以下策略中选择：

1. **`-ffile-prefix-map` 双向映射（推荐）**：在宿主机与 Docker 两条 gRPC 构建中都加
   `-ffile-prefix-map=<各自的克隆目录>=/masi-src/grpc`（以及
   `-ffile-prefix-map=<各自构建目录>=/masi-build/grpc`），把 `__FILE__` 归一化到同一
   虚拟路径。需重建宿主机 gRPC 与 Gateway，并重建 Docker 镜像。最小侵入、不改变克隆
   目录约定。
2. **统一克隆目录**：将 Dockerfile 的 `git clone` 目标从 `/tmp/grpc` 改为
   `/opt/masi-toolchain/grpc-src`，与宿主机一致。需确认该路径在 builder 镜像内可写
   且不与既有层冲突；仍可能需要对构建目录做 prefix-map。
3. **只比较可复现子集 / 放宽匹配口径**：不追求字节级一致，改为对去路径后的符号/段
   做等价比较。**需 Owner 明确批准放宽**，并修改 supply-chain verifier 的匹配语义
   与对应 schema/contract revision。

> 默认推荐策略 1。无论选哪种，若最终 binary digest 变化，须按新 revision 同步
> `contracts/supply-chain/v1/central-inference-cpu-components.json` 及对应的 golden /
> 供应链证据，并重跑完整 `run-module-gates.sh`。

## 工作项

### A. 选定并实施修复策略

- [ ] Owner 在上述三策略中选定一个；若涉及 `contracts/` 冻结件语义变更，开新 revision。
- [ ] 实施 gRPC 构建的 `__FILE__` 归一化（策略 1）或目录统一（策略 2）或匹配口径放宽
  （策略 3），并在宿主机与 Docker 两侧一致应用。
- [ ] 若选策略 1/2：重建宿主机 gRPC（`/opt/masi-toolchain/grpc-1.82.1`）与宿主机
  Gateway，并重建 Docker 镜像 `masi-inference:module-gates`。
- [ ] 用 `readelf -p .comment` / `strings` 抽样确认两侧二进制中 gRPC 源码路径已归一化
  （不再出现 `/opt/masi-toolchain/grpc-src` 与 `/tmp/grpc` 的分歧）。

### B. 验证可复现性

- [ ] `run-supply-chain.sh` 输出 `binary_digest_match=true`，
  `rebuilt_binary_digest == image_binary_digest`。
- [ ] `supply_chain` 门禁由 `FAIL` 转为 `PASS/QUALIFIED`（在干净 release-baseline 工作树上）。
- [ ] 记录两侧 digest、`__FILE__` 归一化样本、构建命令与 `SOURCE_DATE_EPOCH` 到供应链
  证据。
- [ ] 离线 no-cache 重建两侧，确认 digest 稳定（两次独立重建字节一致）。

### C. 同步契约与证据

- [ ] 若 binary digest 变化：按新 revision 更新
  `contracts/supply-chain/v1/central-inference-cpu-components.json` 及对应 golden。
- [ ] 运行 `python3 infer-cpp/scripts/validate-public-contracts.py --repo <root>`，确认
  `result=PASS, qualification=QUALIFIED` 且 source == golden。
- [ ] 完整重跑 `scripts/run-module-gates.sh`（含 `MASI_INF_FORMAL_SOAK=1
  MASI_INF_SOAK_SECONDS=3670`），确认 `supply_chain` 与 `formal_soak_3600_seconds` 均
  `PASS/QUALIFIED`，并在干净工作树上 `overall_module_complete=true`（当其余条件也满足时）。

## 关闭条件

- [ ] 选定修复策略并由 Owner 批准（若涉及契约变更）；
- [ ] `binary_digest_match=true` 在两次独立离线重建中稳定成立；
- [ ] `supply_chain` 门禁为 `PASS/QUALIFIED`；
- [ ] 涉及的 `contracts/` 冻结件以新 revision 同步，`validate-public-contracts.py` PASS；
- [ ] 完整 `run-module-gates.sh` 重跑，`supply_chain` 非 `FAIL|HOLD|NOT_RUN`；
- [ ] 更新本 Issue 状态、证据 digest 与可复制命令。

## 明确禁止的关闭方式

- 不得通过跳过、mock 或改写 `binary_digest_match` 使门禁“通过”；
- 不得用 `MASI_INF_SKIP_SUPPLY=1` 跳过本门禁后宣称供应链合格；
- 不得在两侧路径仍不一致时，仅以“功能等价”替代字节级可复现（策略 3 除外，且须 Owner
  明确批准并改契约 revision）；
- 不得静默修改 `contracts/` 冻结件 digest 以匹配新二进制而不开新 revision；
- 不得把本缺口的修复扩大为对 OCI 启动 smoke、formal soak 或公开边界 blackbox 的语义
  放宽。

## 参考

- 供应链组件登记：[contracts/supply-chain/v1/central-inference-cpu-components.json](../../contracts/supply-chain/v1/central-inference-cpu-components.json)
- 供应链门禁脚本：[infer-cpp/scripts/run-supply-chain.sh](../../infer-cpp/scripts/run-supply-chain.sh)
- Dockerfile（Docker 侧 gRPC 克隆路径）：[infer-cpp/Dockerfile](../../infer-cpp/Dockerfile)
- 宿主机 gRPC prefix：`/opt/masi-toolchain/grpc-1.82.1`，源码 `/opt/masi-toolchain/grpc-src`
- 关联 OCI 启动 smoke 修复（已完成）：[inf-oci-smoke-startup-fixes 记忆](../../infer-cpp/Dockerfile)
- 资格等级与证据 ADR：[../adr/0006-qualification-levels-and-evidence.md](../adr/0006-qualification-levels-and-evidence.md)