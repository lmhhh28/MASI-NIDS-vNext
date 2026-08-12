# ADR-0002：Analysis Plugin 保留行为兼容，不迁移旧 Workflow

- 状态：Accepted
- 日期：2026-08-10
- 决策者：Owner
- 需求基线：原始决策 `vNext-requirements-1.6`；当前适用基线 `vNext-requirements-1.17`
- 关联需求：`ARCH-001`、`ARCH-003`、`ARCH-PLUGIN-001`、`CONTRACT-AGENT-001`、`CONTRACT-PLUGIN-001`、`MOD-AGENT-001`、`PLUGIN-PLAT-001` 至 `PLUGIN-PLAT-006`、`AGENT-001` 至 `AGENT-008`、`AGENT-COMPAT-001`、`TEST-003`、`TEST-PLUGIN-001`、`TEST-006`、`MIG-003`、`MIG-PLUGIN-001`

## 背景

旧实验项目包含两个不同概念：

1. 通用 Workflow v2 外层运行时，包括 workflow/revision/node-attempt、审核和数据库状态；
2. 固定 `masi-event-analysis-v1` 分析图，包括冻结 bundle、确定性上下文、bounded LLM/MCP、grounding、显式受限结论、trace 和不可执行 Artifact。

vNext 是 greenfield 重写，需求已排除第一类。若把两类一起删除，会丢失已经验证的分析安全与降级行为；若逐节点复制，又会把旧 identity、schema、checkpoint 和耦合带入插件。因此需要定义“能力兼容”而不是“实现兼容”。

## 决策

Analysis Plugin 是通用插件平台首个官方产品插件，以不可变 `masi.analysis.langgraph` revision 和 `analysis-agent` kind 注册，并通过统一 manifest、qualification、activation、generation fence、drain/revoke 和审计控制面。它继续作为独立 Python 进程/容器，通过 A2A 接收任务，通过只读 MCP 取证，并在内部使用固定、版本化 LangGraph；第一方身份不允许绕过平台门禁。

平台只统一插件控制面，不接管 Analysis 的业务图、Task、Artifact 或私有 schema，也不把 A2A/MCP 变成所有 plugin kind 的统一 ABI。Analysis Plugin 必须逐项保留以下外部可观察能力：

- 冻结且可哈希的 Incident/Event/Evidence 输入；
- identity、quality、provenance、runtime/P4 read-only 和 incident/replay 的确定性检查；
- hypothesis/gap planning；
- 按证据缺口启用的 bounded MCP 与可选第二次 LLM synthesis；
- 最多 2 次 LLM、2 轮 MCP、6 次工具调用和 30 秒 hard deadline；
- deterministic evidence join、grounding validator、`analysis_outcome=limited|insufficient_evidence|failed` 的受限报告与 report guard；这些 outcome 只描述 Analysis Artifact 的证据充分度或执行状态，不选择模型/runtime、不生成可执行 effect，也不改变 Event/P4/current binding；
- append-only trace、执行计划/topology digest、Artifact hash 和 `deployment_eligible=false`；
- LLM/MCP/A2A/Plugin 不可用时核心系统保持等价。

首期 wire profile 固定为 A2A 1.0 HTTP+JSON 和私有 `masi-mcp-readonly/v1`（MCP 2025-11-25 Streamable HTTP restricted profile）；每次请求显式携带已协商版本，或由同一认证 session 唯一识别，不执行旧版 payload。A2A 使用 polling/no push/no streaming；MCP Plugin client 支持 JSON/SSE POST response，Go server 首期返回 bounded JSON，并只暴露 binding allowlist 内的只读 tools/resources。缺少 MCP version header 且 session 无法唯一识别版本时返回 HTTP 400，是项目对私有 endpoint 的有意收紧；该 endpoint 不宣称通用 MCP 向后兼容。官方缺 header fallback 与项目取舍记录在 ADR-0005。

不保留：

- legacy Workflow/revision/node-attempt identity；
- legacy Review/Auth、数据库表、checkpoint、outer runner 和前端 Workflow 页面合同；
- 节点源码、节点名称或完全相同的内部边顺序；
- mutation MCP tool、P4/effect/approve/deploy/rollback 权限。

`AGENT-003` 的四个 skill 可以组合共享的受测节点，但每个旧能力必须在 capability matrix 中映射到明确的新 contract、skill/node 和 test。无法映射的差异需要 Owner 决定，不能用“新架构”默认为已兼容。

## 取舍

收益：

- 保留旧实验图中最有价值的 grounding、预算、显式受限结论和不可执行边界；
- Analysis Plugin 可以独立演进，不依赖旧 Workflow 数据和 Go 核心内部状态；
- 可用 golden/fake tests 判断兼容，而不是比较 Python 源码或节点名称；
- LangGraph/MCP/A2A 仍完全位于旁路，不影响热路径性能和真实 effect 稳定性。

代价：

- 需要维护 capability matrix、旧/新 golden bundle 和差异报告；
- 内部拓扑改变时仍需证明外部语义与预算不变量；
- “全部功能支持”必须等矩阵完成后才能声明，初始化阶段只能是 `HOLD/NOT RUN`。

## 被拒绝的方案

1. **逐文件移植旧 graph/workflow**：继承旧 schema、identity 和耦合，不符合 greenfield。
2. **只实现四个 prompt，不做兼容矩阵**：无法证明 grounding、预算、受限结论和 trace 行为仍在。
3. **让 A2A 代替 LangGraph 节点或让 MCP 承载 Agent 间协作**：混淆协议职责并扩大故障面。
4. **Agent 直接生成 proposal/intent**：把非确定性输出接入真实处置，违反不可执行边界。
5. **第一方 Analysis 绕过通用平台直接部署**：会产生第二套 registry、资格、激活和回滚语义，无法证明平台对真实业务插件有效。

## 迁移

不迁移旧 Workflow/Task/Review/checkpoint 数据。测试资产只提取脱敏、冻结的 legacy bundle/golden expected invariant，经新 contract 转换器显式映射。转换器属于 testkit/离线迁移工具，不进入生产热路径，也不能写核心事实。

实施顺序：

1. 冻结 capability matrix、`analysis/v1`、`plugin/manifest/v1` 和 `analysis-agent` kind contract；
2. 建立 fake Manager/Host、fake LLM/MCP/A2A 和 deterministic golden/limited-outcome vectors；可以在隔离 test identity 下做真实 TLS/framing/SDK boundary rehearsal，但始终使用 `level=REHEARSAL, qualification=NOT_QUALIFIED`，展示可标记 `REHEARSAL/NOT QUALIFIED`；
3. 独立实现四个 skill 与共享受测节点；
4. 将 immutable revision 通过通用平台的 admission、qualification、shadow 和 explicit activation 门禁；
5. 通过 Analysis Plugin black-box、fault、budget 和兼容差异门禁；
6. 全模块完成后从干净环境重跑并正式集成 Go/Manager/A2A、MCP 与 Frontend；早期 rehearsal 证据不能转为 integration PASS。

## 回滚

Analysis revision 失败时，由 Plugin Manager 对 exact active binding 执行 disable/revoke，或原子回滚到仍通过当前 trust/contract/config/持久化事实兼容矩阵的旧 revision；Go 返回稳定 unavailable，核心链保持运行。旧插件 Artifact 只读保留；不得为回滚恢复 legacy Workflow 服务、覆盖 artifact bytes 或把 Agent 嵌回 Go/Rust。没有合格 revision 时 Analysis 保持 `HOLD/NOT RUN`。

## 验证

- capability matrix 的每一行映射需求 ID、输入、预期 Artifact/`analysis_outcome`、测试和证据 hash；
- 分别测试证据充分、需要工具、新证据不足、low quality、provider/tool timeout、malformed/oversize、grounding reject；
- 证明 tool calls 不超过 6、rounds 不超过 2、LLM 不超过 2、Artifact 不超过 64 KiB；
- 证明所有 mutation tool 均拒绝、`deployment_eligible` 永远为 false；
- 证明官方插件无法绕过 manifest、qualification、active generation、capability 和 revoke；
- Manager/Host/Analysis disabled、crash 或 network partition 不改变 Event、effect、P4 和非 Analysis 页面。

## 参考

- 通用插件平台边界：`0003-controlled-general-plugin-platform.md`
- 契约、restricted MCP 与 runtime profile：`0005-contract-runtime-and-protocol-profiles.md`
- 资格等级与 rehearsal：`0006-qualification-levels-and-evidence.md`
- A2A 1.0.0：<https://a2a-protocol.org/v1.0.0/specification/>
- MCP 2025-11-25 Streamable HTTP：<https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- legacy topology：`../../../MASI-NIDS/AEE_cuda/nids_backend/agents/analysis_graph/topology.py`
- legacy runner：`../../../MASI-NIDS/AEE_cuda/nids_backend/agents/analysis_graph/runner.py`
- legacy Artifact：`../../../MASI-NIDS/AEE_cuda/nids_backend/agents/analysis_graph/artifact.py`
- legacy tests：`../../../MASI-NIDS/AEE_cuda/nids_backend/tests/test_agent_analysis_graph.py`
