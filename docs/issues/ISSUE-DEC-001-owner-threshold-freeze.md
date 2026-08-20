# [DEC-001] Owner 绝对门槛冻结流程

- Issue ID：`ISSUE-DEC-001`
- 状态：`OPEN`（qualification-blocker，待 Owner 冻结）
- 类型：`qualification-blocker`
- 优先级：`P0 before qualification PASS`
- 模块：`p4-switch`（已冻结/QUALIFIED）、`rust-edge`、`central-inference`、`go-control`
- 关联决策：`DEC-001`（系统需求 §Owner-confirmed decisions）
- 关联需求：`PERF-001`、`PERF-002`、`PERF-P4-FW-001`、`PERF-TEL-INF-001`、`PERF-INF-001`、`TEST-GATE-001`、`TEST-003`、`ADR-0006`
- 创建日期：2026-08-20

## 1. 背景

`DEC-001` 定义 Owner 必须冻结的六类**绝对**门槛：目标硬件、包速率、target 数、窗口率、Event 率和端到端 p99 SLO。冻结流程（系统需求 `:1729`）：**先建立现有系统基线，再按目标硬件冻结绝对门槛**。

当前状态（2026-08-20）：

| 模块 | operational Module Complete | qualification | DEC-001 |
|---|---|---|---|
| P4/Switch | ✅ | ✅ PASS/QUALIFIED | **OWNER_FROZEN**（模板，见 `p4-bmv2-functional-reference.json`） |
| Rust Edge | ✅ | HOLD/NOT_QUALIFIED | 未冻结 |
| Central Inference | ✅ | HOLD/NOT_QUALIFIED | 未冻结 |
| Go Control | ✅ | HOLD/NOT_QUALIFIED | 未冻结（此前无性能环境 profile） |

按 `DEC-044`，绝对门槛未冻结只产生 qualification-only `HOLD/NOT_QUALIFIED`，**不阻断** operational Module Complete，但**阻断** qualification PASS。`protected_release_baseline` 是独立资格 gate（需受保护 commit/tag attestation），DEC-001 冻结不替代它。

## 2. 本 Issue 的范围

为 Edge / Central Inference / Go Control 三模块准备 DEC-001 冻结机制并记录冻结流程，使 Owner 提供 WSL2/目标硬件 SLO 数值后可一次性冻结并重跑 formal soak 翻 `PASS/QUALIFIED`。**本 Issue 不替 Owner 选择门槛数值**（见 §4 禁止项）。

已完成（2026-08-20）：
- 新增 `contracts/profiles/v1/performance-environment-control-core.json` —— Go Control 此前无性能环境 profile，门槛是 `control-go/scripts/validate-soak-evidence.py:28-29` 的硬编码常量。新 profile 含 WSL2 host 实测环境 + `process_thresholds`（TBD）+ `observed_baseline`（从 `ctrl-formal-3600-003` formal soak 提取的观测值：max_rss=38,408,192 B、max_fd=16、max_thread=14、max_queue=37）。
- Edge / Infer 的 profile 已有 `TBD-DEC-001` 占位。

## 3. 冻结编辑面（Owner 提供数值后改动）

| 层 | 文件:行 | 当前未冻结值 | 冻结目标 |
|---|---|---|---|
| Edge profile | `contracts/profiles/v1/rust-edge-agent.json:43,222,223` | `OWNER_NOT_FROZEN_DEC_001` / `HOLD_OWNER_NOT_FROZEN_DEC_001` / `OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001` | `OWNER_FROZEN` + 冻结 N、pps、窗口/Event 率、p99、RSS/FD/thread |
| Infer env | `contracts/profiles/v1/performance-environment-central-cpu.json:29,33-39,56,63` | `TBD-DEC-001` / `TBD-DEC-001-not-frozen` / `DEC-001-NOT-FROZEN-OBSERVED-ONLY` / `NOT_FROZEN` | 具体数值 + `OWNER_FROZEN` |
| Control env | `contracts/profiles/v1/performance-environment-control-core.json` `process_thresholds.*` | `TBD-DEC-001-not-frozen` | Owner 冻结数值 |
| Control validator | `control-go/scripts/validate-soak-evidence.py:28-29` | `CONTROL_THRESHOLD_STATUS = "OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001"` 常量 | 改为从 profile 读取；冻结时映射为 `FROZEN` |
| Control evidence builder | `control-go/tests/soak/soak_test.go:30,444,451-454,520,529` | `thresholdStatus` 常量 + `rss/fd/thread=nil` | 冻结状态 + 观测 maxima 作为 limit |
| Soak schema | `contracts/evidence/soak/v1/schema.json:372,453,495` | `process_threshold_status` const `OBSERVED_ONLY_OWNER_NOT_FROZEN_DEC_001` | const `FROZEN` |
| Soak schema 资源字段 | `schema.json:369-371,450-452,492-494` | `rss_bytes/fd_count/thread_count` `{"type":"null"}` | 正整数范围 |
| Validators | `edge-rs/scripts/validate-soak-evidence.py:561`；`control-go/scripts/validate-soak-evidence.py:241,379` | `threshold_status != "FROZEN"` → HOLD | `== "FROZEN"` → 接受 `PASS/QUALIFIED` |

P4 路径（`p4-stateless-firewall-bmv2.json:162` + `p4-bmv2-functional-reference.json`）是该模式的已冻结参考实现。

## 4. 冻结步骤

1. **Owner** 在受保护基线中针对 WSL2/目标硬件冻结每模块的绝对门槛（profile 数值）。门槛必须在资格运行**前**冻结。
2. 填入 §3 表中的 profile 数值；将 schema `process_threshold_status` const 改 `FROZEN`，资源字段由 null 改正整数范围；validator/evidence builder 相应改。
3. 重跑每模块 formal soak（Edge/Control/Infer 各 `MASI_*_FORMAL_SOAK=1 scripts/run-module-gates.sh`，≥3600s）。
4. validator 的 `threshold_status == "FROZEN"` 分支接受 `result=PASS, qualification=QUALIFIED`；证据 `qualified_elapsed_ms≥3600000`、零 counter、无中断。
5. 更新本 Issue 状态、证据 digest 与可复制命令；更新各 README/traceability 的 `当前证据` 列。

## 5. 明确禁止的冻结方式

- 不得按已观察的 soak 结果事后选择一个刚好能过的阈值（反推）——`observed_baseline` 仅是测量，不是 SLO。
- 不得从单 target / BMv2 / 第三方产品 demo 外推 PASS（系统需求 `:838`、`:3090`）。
- 不得以相对 5% 回归门槛代替绝对容量资格（系统需求 `:1736`）。
- 不得用 waiver 把未冻结的绝对生产门槛提升为 PRODUCTION QUALIFIED（ADR-0006 `:150`、`:154`）。
- Owner waiver 如适用必须遵守 `TEST-GATE-001`，原结果保持原值，不得改写为 PASS。

## 6. 关闭条件

- [ ] Edge / Infer / Control 三模块的 profile 绝对门槛由 Owner 在受保护基线中冻结（`OWNER_FROZEN` + 具体数值）。
- [ ] soak schema `process_threshold_status` const 改 `FROZEN`、资源字段改正整数范围。
- [ ] 三模块在新 exact scope 下完整重跑 formal soak，证据为 `result=PASS, qualification=QUALIFIED`。
- [ ] validator `threshold_status=="FROZEN"` 分支接受，聚合门禁 `remaining_holds=[]`（`absolute_performance` 与 `formal_soak_3600_seconds` 转为 PASS/QUALIFIED）。
- [ ] `protected_release_baseline` 仍为独立 gate：受保护 commit/tag attestation 未形成前，整体 qualification 仍可保持 HOLD，但不得改写已执行门禁的原始结果。
- [ ] profile digest 漂移使旧证据失效（CONTRACT-PROFILE-001）；文档记录该预期。

## 7. 参考

- [P4 Switch 冻结 Issue](ISSUE-P4-SW-001-module-complete-holds.md)（已冻结参考）
- [资格等级与证据 ADR](../adr/0006-qualification-levels-and-evidence.md)
- [系统需求基线](../masi-nids-vnext-system-requirements-2026-08-09.md) §DEC-001、`:1729`、`:1736`、`:838`、`:3090`
- 已冻结模板：`contracts/profiles/v1/p4-bmv2-functional-reference.json`、`contracts/profiles/v1/performance-environment-p4-bmv2-wsl2.json`
- 新增 Control profile：`contracts/profiles/v1/performance-environment-control-core.json`