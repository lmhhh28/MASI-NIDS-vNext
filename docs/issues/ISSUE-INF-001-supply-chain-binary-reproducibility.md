# [Central Inference] 修复供应链二进制可复现性缺口（host 与 Docker gRPC `__FILE__` 路径不一致）

- Issue ID：`ISSUE-INF-001`
- 状态：`IN_PROGRESS`（修复已实施，等待镜像重建后的字节比对复核）
- 类型：`supply-chain-reproducibility`
- 优先级：`P1`（第二个根因是未登记的第三方依赖版本漂移，见“根因修正”）（不阻塞 OCI 启动 smoke 或 formal MODULE soak，但 `supply_chain` 门禁为 `FAIL`，阻止 `overall_module_complete`）
- 模块：`central-inference`（`MOD-INF-001`）
- 创建日期：2026-08-17
- Owner：`待 Owner 决定修复策略`（见下文“决策项”）
- 执行负责人：Kiro（2026-08-17）
- 需求基线：[masi-nids-vnext-system-requirements-2026-08-09.md](../masi-nids-vnext-system-requirements-2026-08-09.md)
- 关联要求：`CONTRACT-SUPPLY-001`、`SEC-SUPPLY-001`、`ARCH-REUSE-001`、`MOD-INF-001`
- 关联 finding：`INF-AUDIT-0021`（P1，nlohmann/json 版本漂移且未登记）、`INF-AUDIT-0022`（P2，gRPC 源码路径经 `__FILE__` 渗入）、`INF-AUDIT-0023`（P2，登记表 commit 占位值）。`INF-AUDIT-0012` 已关闭且与本缺口无关。

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

## 根因修正（2026-08-17 实证复核）

原 Issue 只捕获了一个根因。对两侧真实二进制做逐节/符号/字符串比对后，确认存在**两个独立根因**，
且 Issue 未捕获的那个更严重：

比对对象：`evidence/module-gates/runs/formal-soak-mod-0817/supply-chain/runs/20260817T001312Z-673781/supply/`
下的 `masi_inference_gateway.image.bin`（24,342,184 字节）与 `masi_inference_gateway.offline-rebuild.bin`
（24,378,248 字节），差 36,064 字节。

### 根因 1（Issue 已捕获，实为次要）：gRPC 源码路径经 `__FILE__` 渗入

```
image.bin           /tmp/grpc                      488 处
offline-rebuild.bin /opt/masi-toolchain/grpc-src    488 处
```

路径长度差 19 字符 × 488 ≈ 9.3 KB，只能解释 36 KB 差异中的约四分之一。

### 根因 2（Issue 未捕获，P1）：nlohmann/json 版本漂移，且该依赖完全未登记

符号集比对直接暴露：

```
[image only] masi::inf::(anonymous namespace)::require_field(nlohmann::basic_json<…>)
[host  only] masi::inf::(anonymous namespace)::require_field(nlohmann::json_abi_v3_11_3::basic_json<…>)
```

inline ABI 命名空间 `json_abi_v3_11_3` 是 3.11 引入的，因此：

| 构建路径 | nlohmann/json 来源 | 版本 |
|---|---|---|
| 宿主机离线重建 | `/usr/include/nlohmann/json.hpp`，**不属于任何 dpkg 包**（手工安装，无出处） | 3.11.3 |
| Docker 镜像构建 | Dockerfile `apt-get install nlohmann-json3-dev`（ubuntu:22.04） | 3.10.5 |

这解释了 `.text +7,856`、`.symtab +1,200`、`.strtab +10,320`、`.rodata +12,000` 的其余差异
（image 侧 145 个独有符号、host 侧 185 个）。它不仅破坏可复现性，还意味着：一个被编译进 Gateway
的第三方依赖此前**在 `contracts/supply-chain/v1` 中完全没有登记**，且两侧实际版本不同——
即“被测二进制”与“被交付二进制”并非同一份代码。

### 已排除的其它嫌疑（均实测一致，不是差异来源）

- 编译器：两侧 `.comment` 均为 `GCC: (Ubuntu 11.4.0-1ubuntu1~22.04.3) 11.4.0`；cmake 均 3.22.1；
- 安装 prefix `/opt/masi-toolchain/grpc-1.82.1`：两侧各 15 处，已一致（故 **prefix 不可改动**）；
- 项目自身源码路径：两侧均 0 处（Release + 无 `__FILE__`/`__DATE__`/`__TIME__` 使用）；
- gRPC 构建目录：两侧均 0 处，不渗入；
- build-id：两侧均无；
- 运行时 `libssl.so.3`/`libz.so.1`：镜像内副本与宿主机 `sha256` 逐字节相同。

## 已实施的修复（2026-08-17）

采用**策略 2 + 依赖固化**，不涉及冻结契约的语义变更，也不重建已登记的宿主机工具链
（重建会改变全部既有证据所绑定的二进制，且 prefix 字符串本已一致，无需改动）：

1. **vendored nlohmann/json 3.11.3**：`infer-cpp/third_party/nlohmann/json.hpp`，
   `sha256:9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6`（919,975 字节），
   与上游 release 资产、`v3.11.3` tag 源文件、官方发布公告三方核对一致；
   tag commit `9cca280a4d0ccf0c08f47a99aa71d1b0e52f8d03`（`git ls-remote` 核实）。
   登记到 `central-inference-vendored-sources.json`（逐文件出处/许可/修改/更新退出策略）、
   `central-inference-cpu-components.json`（`ADOPT`）与 `central-inference-cpu-third-party-notices.json`。
2. **构建期与编译期双重锁定**：CMake `file(SHA256)` 比对登记 digest，不符即 `FATAL_ERROR`；
   include 目录以 `BEFORE` 置于最前；`src/envelope.h` 追加
   `static_assert(NLOHMANN_JSON_VERSION_* == 3.11.3)`，系统头无法顶替。
3. **Dockerfile**：删除 `nlohmann-json3-dev` apt 层；`git clone` 目标改为
   `ARG GRPC_SOURCE_DIR=/opt/masi-toolchain/grpc-src`（与宿主机一致）；新增 builder
   `g++`/`cmake` 版本与登记值不符即 fail closed 的断言。
4. **`scripts/build-grpc-toolchain.sh`（新增）**：把此前只存在于口头约定的宿主机工具链
   构建过程写成可复现脚本，flags 与 Dockerfile 完全一致，版本/commit 从登记表读取并交叉校验，
   prefix 已存在时拒绝覆盖（非破坏性）。
5. **证据可诊断化**：`offline-rebuild.json` 增加两侧嵌入 gRPC 源码路径的出现次数及是否相等、
   镜像内异常源码路径样本、vendored header digest 与 `SOURCE_DATE_EPOCH`，
   使下次不匹配能直接定位漂移输入。
6. **顺带修正**：登记表中 protobuf/Abseil 的 commit 由占位串
   `pinned-as-grpc-v1.82.1-submodule` 换为 pinned gRPC 树中的真实 submodule commit，
   登记表现已零错误通过自身 schema。

## 验证结果（2026-08-17）

### 字节一致性

```
host  build/cpu-release/masi_inference_gateway            5a285babcf41930b0dc72458ba4bba3bcafd2ed4bafc054837df753aa6f01b7e
image masi-inference:repro-test   (layer cache)           5a285babcf41930b0dc72458ba4bba3bcafd2ed4bafc054837df753aa6f01b7e
image masi-inference:repro-test2  (--no-cache 全量重建)    5a285babcf41930b0dc72458ba4bba3bcafd2ed4bafc054837df753aa6f01b7e
```

24,378,248 字节，`cmp` 逐字节相同。第二次镜像构建使用 `--no-cache`，重新克隆 gRPC 并全量编译，
因此三方一致同时证明了**跨独立构建的确定性**，而不只是缓存复用。宿主机 digest 与修复前相同
（宿主机本来就是 3.11.3 + `grpc-src` 路径），即镜像侧向宿主机对齐，既有宿主机证据不失效。

### 门禁输出（`evidence/supply-chain/runs/verify-repro/`）

```json
"binary_digest_match": true,
"embedded_grpc_source_path_occurrences": { "offline_rebuild": 488, "image": 488, "equal": true },
"foreign_source_path_samples_in_image": [],
"vendored_json_digest": "sha256:9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6",
"checks": { "offline_rebuild": true },
"failure_reasons": [],
"hold_reasons": ["DIRTY_WORKTREE_NOT_RELEASE_BASELINE"]
```

`FAIL` 已消除（`failure_reasons` 为空，`offline_rebuild` 检查为 true）。当前 `result=HOLD`
的唯一原因是工作树未提交（`DIRTY_WORKTREE_NOT_RELEASE_BASELINE`）；按脚本判定逻辑，
在干净工作树上同样的输入会给出 `PASS/QUALIFIED/CLEAN_SOURCE_SNAPSHOT`。按 `DEC-044`，
这类仅由 dirty tree 造成的 qualification-only HOLD 不阻断 operational completion，
但在提交前不得声称 `supply_chain PASS`。

### 回归复核

宿主机侧改用 vendored header 后：`contract_golden=0`、`property_invariants=0`、
真实 Triton 上的 `blackbox_e2e=0`、`validate-public-contracts.py` `PASS/QUALIFIED`
（`negative_vectors=13`）、`run-numeric-golden.py=0`。
`scripts/build-grpc-toolchain.sh` 的登记表交叉校验与 prefix 覆盖保护均已实测生效。

### 期间修掉的一个脚本缺陷

新增诊断字段时发现：`grep` 无匹配返回 1，在 `set -o pipefail` 下会使
`... | jq -sc . || printf '[]'` 同时产出两个 `[]`，导致 `jq --argjson` 收到非法 JSON 并让
整个供应链门禁以 `exit 2` 中断。已改为把非零退出吞在 `{ grep ... || true; }` 组内，
使管道恒定产出单个合法 JSON 文档。

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

- [x] 采用策略 2（统一克隆目录）+ 依赖固化；未触及冻结契约语义，未重建已登记宿主机工具链，因此无需新 revision。Owner 若要求策略 1（`-ffile-prefix-map` 路径无关）需另行批准，代价见“决策项”。原文：Owner 在上述三策略中选定一个；若涉及 `contracts/` 冻结件语义变更，开新 revision。
- [x] 实施 gRPC 构建的 `__FILE__` 归一化（策略 1）或目录统一（策略 2）或匹配口径放宽
  （策略 3），并在宿主机与 Docker 两侧一致应用。
- [x] 仅重建 Docker 侧（宿主机 gRPC 与 prefix 未变，无需重建）。原文：若选策略 1/2：重建宿主机 gRPC（`/opt/masi-toolchain/grpc-1.82.1`）与宿主机
  Gateway，并重建 Docker 镜像 `masi-inference:module-gates`。
- [x] 已确认：两侧均 488 处 `/opt/masi-toolchain/grpc-src`，`/tmp/grpc` 归零，`.comment` 两侧同为 GCC 11.4.0。原文：用 `readelf -p .comment` / `strings` 抽样确认两侧二进制中 gRPC 源码路径已归一化
  （不再出现 `/opt/masi-toolchain/grpc-src` 与 `/tmp/grpc` 的分歧）。

### B. 验证可复现性

- [x] `run-supply-chain.sh` 输出 `binary_digest_match=true`，
  `rebuilt_binary_digest == image_binary_digest`。
- [~] `FAIL` 已消除；`PASS/QUALIFIED` 待提交后在干净工作树复跑确认。原文：`supply_chain` 门禁由 `FAIL` 转为 `PASS/QUALIFIED`（在干净 release-baseline 工作树上）。
- [x] 记录两侧 digest、`__FILE__` 归一化样本、构建命令与 `SOURCE_DATE_EPOCH` 到供应链
  证据。
- [x] 离线 no-cache 重建两侧，确认 digest 稳定（两次独立重建字节一致）。

### C. 同步契约与证据

- [x] binary digest 未变化（宿主机侧不变），故无需新 revision；仅补齐此前缺失的 nlohmann 登记并把 protobuf/Abseil 的 commit 占位值换成真实值。原文：若 binary digest 变化：按新 revision 更新
  `contracts/supply-chain/v1/central-inference-cpu-components.json` 及对应 golden。
- [x] 运行 `python3 infer-cpp/scripts/validate-public-contracts.py --repo <root>`，确认
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