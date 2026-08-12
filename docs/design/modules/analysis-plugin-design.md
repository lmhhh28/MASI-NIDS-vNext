# Python Analysis Plugin 模块详细设计

- 模块 ID：`MOD-AGENT-001`
- 官方插件 ID：`masi.analysis.langgraph`
- 目录：`analysis-py/`
- 文档状态：`DRAFT`
- 主要需求：`CORE-PLUGIN-001`、`MOD-AGENT-001`、`CONTRACT-AGENT-001`、`AGENT-001`、`AGENT-002`、`AGENT-003`、`AGENT-004`、`AGENT-005`、`AGENT-006`、`AGENT-007`、`AGENT-008`、`AGENT-COMPAT-001`、`TEST-003`、`TEST-PLUGIN-001`、`TEST-006`
- 主要 ADR：ADR-0002、ADR-0003、ADR-0005、ADR-0006；ADR-0018用于统计Artifact引用边界

## 1. 模块目标

Python Analysis Plugin 是平台首个官方 `analysis-agent`。它对Go/peer提供的有界Incident bundle进行证据分析，可使用LangGraph组织内部分析步骤、使用LLM生成受约束推断、通过restricted MCP读取allowlist证据，并输出grounded、不可执行的`AnalysisArtifact`。

Analysis不进入实时检测或P4处置闭环，不拥有Event/Incident/Proposal/Decision/Intent/model/plugin current，也不能调用Edge/P4。它经过Go Plugin Manager统一manifest/qualification/binding/revoke控制，但业务A2A/MCP direct typed流量不经Plugin Runtime Host。

## 2. 运行边界

```text
Go / peer Agent
  └─ A2A task/message (direct typed adapter)
       └─ Python Analysis Plugin
            ├─ bounded input/context builder
            ├─ LangGraph analysis graph
            ├─ LLM provider adapter
            ├─ restricted MCP client
            │    └─ Go allowlist read-only MCP endpoint
            ├─ grounding/quality/redaction validator
            └─ AnalysisArtifact
```

Go/peer通过A2A调用Analysis；Analysis再调用Go提供的`masi-mcp-readonly/v1` endpoint。两条业务路径都不经过Runtime Host。浏览器不直连Analysis、MCP、A2A或LLM provider。

## 3. 输入合同

`IncidentAnalysisBundleV1`（具体名称由contract冻结）至少携带：

- task/incident/evidence identity、schema/profile、scope和trace；
- exact Event/Incident/model/target/rule/generation引用；
- bounded canonical facts、evidence content digest、freshness和quality；
- actor/policy允许的tool/provider capability；
- token/time/tool-call/output/resource budgets；
- redaction/data-class/provenance和expiry。

输入只包含Go授权投影，不提供DB连接、raw secret、P4 credential或可执行prompt/template。Untrusted packet/user/plugin文本与system instruction分离并带来源；超限、过期、未知major或scope不符在graph运行前拒绝。

## 4. LangGraph 内部设计

LangGraph只在本模块内组织可测试的分析状态，不外溢成系统workflow。内部graph可包含：

- 输入/证据验证；
- 确定性上下文构建；
- 是否需要MCP补证的有界决策；
- LLM分析/摘要；
- citation/grounding/quality校验；
- Artifact组装和稳定降级。

Graph state/checkpoint是模块内部短期执行细节，不能迁移legacy Workflow/checkpoint/schema，也不能成为Go业务current。首期不声称逐节点源码兼容；只按`AGENT-COMPAT-001`验证旧系统外部可观察行为。

## 5. LLM Provider Adapter

Provider adapter固定model/provider profile、TLS/auth、request/response schema、token/context/output上限、timeout/retry和redaction。Retry只在同task/input digest、无外部副作用且总budget内进行；provider返回不能修改system policy、tool allowlist或scope。

正式确定性CI/System E2E可使用资格化deterministic provider fixture，但Analysis进程、A2A和MCP必须真实启动。真实provider另做TLS/auth/quota/timeout/redaction smoke与quality资格，两类证据分开。

Provider不可用、超时或低质量时输出明确`limited|insufficient_evidence|failed`或合同等价outcome，不伪造完整结论，不触发fallback到未经资格模型。

## 6. MCP Read-only Tooling

MCP固定私有`masi-mcp-readonly/v1` restricted 2025-11-25 Streamable HTTP profile。Analysis只能调用manifest/binding/input允许的tool/resource ID；Go endpoint按当前task/scope/data-class重新授权，返回bounded JSON或合同数据。

MCP禁止mutation、任意URL/SQL/PromQL、runtime tool registration、credential回传和通用旧版fallback。Tool result带request/content digest、observed time、source/provenance、quality和truncation。Prompt injection或tool返回中的指令只作为不可信数据，不得扩大capability。

## 7. A2A 边界

首期A2A 1.0使用HTTP+JSON、每请求显式版本、非streaming task polling、no push、outbound peer静态allowlist。A2A用于Agent Task/Message/Artifact协作，不替代MCP或Go业务状态机。

Task identity、input digest、binding generation、deadline和artifact digest必须幂等。Same task/same input返回原Artifact；same identity/different input冲突；old/revoked generation late Artifact不可成为current展示。

## 8. AnalysisArtifact

Artifact是不可执行内容，至少包含：

- task/plugin/revision/config/binding identity；
- source fact/evidence引用与digest；
- facts、inferences、unknowns明确分区；
- conclusion/recommendation及confidence/limitations；
- citations/tool/provider provenance；
- quality/outcome、produced/expires time和artifact digest。

Artifact不能包含P4 command、raw TableEntry、effect intent、approval token、browser code、HTML/JS、arbitrary URL或自动action。UI可供人阅读和引用hash，但Analyst仍需从当前Go facts独立创建proposal，Go重算eligibility/risk/diff。

Analysis可以引用已由Go验证的Plugin Statistics Artifact生成叙述，但不能把它升级为核心统计或覆盖rule/model/incident facts。

## 9. 状态与存储

Analysis尽量无状态执行。若合同要求保存task-local audit/checkpoint，使用隔离`analysis_private` schema/role或受控自有存储，只有本模块访问；不得扫描/写core、model、target、plugin或effect schema。

Canonical task/binding/artifact projection仍由Go Plugin Manager/API控制。Local cache有size/age/key scope上限，revoke/session/profile change清除；local state不能作为恢复后自动执行或授权依据。

## 10. 安全、资源与可观测性

- mTLS验证Go/peer/MCP/provider identity，无明文fallback；
- provider/MCP egress使用allowlist、DNS/IP/redirect/proxy防护；
- input、context、tokens、tool calls、steps、recursion、Artifact bytes、concurrency、queue和deadline全部有界；
- secret只由provider/tool adapter持有，不进入prompt、Artifact、log、trace或Web；
- prompt injection、malformed/oversize tool/LLM response、citation spoof和data exfiltration有负例；
- metrics低基数记录task/outcome/latency/tool/provider status，不以Incident/IP/rule作为label；
- Plugin disabled/revoked/unavailable时核心检测/effect/原生页面继续。

## 11. 启动与健康

- startup：验证Python/runtime/image、manifest/revision/config、A2A/MCP/provider profile和Manager binding；
- readiness：A2A公开边界可接收并验证task；provider/MCP不可用可表现为domain degraded，不应隐藏为ready success；
- liveness：event loop/worker取得进展，不因provider暂时故障无限重启；
- drain：停止新task，按budget完成/取消in-flight并返回稳定outcome；
- revoke：拒绝新generation调用，late Artifact fenced，不继续使用缓存tool/provider权限。

## 12. Legacy 行为兼容

`AGENT-COMPAT-001`按冻结输入验证：确定性上下文、有界LLM/MCP、证据grounding、facts/inferences/unknown分离、稳定降级、trace和不可执行Artifact。差异分类为equivalent、accepted difference、defect或not supported。

Legacy Python graph节点源码、Workflow v2、checkpoint、Review/Auth/schema、prompt原文和运行目录不迁移。矩阵未完成前不得声称“支持原LangGraph全部功能”。

## 13. 模块黑盒 E2E 验收范围

必须真实启动Python 3.12+ release OCI/process，以frozen bundle和Fake LLM/MCP/A2A邻居测试公开边界。验收覆盖：

- A2A task/polling/version/identity/idempotency；
- MCP restricted allowlist/read-only/scope/timeout/provenance；
- LangGraph成功、补证、证据不足、低质量、provider/tool故障路径；
- Artifact schema/grounding/citation/不可执行边界；
- prompt injection、oversize、secret/data exfiltration和零core/P4/effect mutation；
- `AGENT-COMPAT-001`完整矩阵、资源、fault、performance和soak；
- Manager binding/revoke/fence，但业务路径不经Host。

Python验证使用type/lint和`unittest`，不引入pytest。

## 14. Module Complete 判定

Analysis只有在官方插件完整功能、A2A/MCP/LLM边界、compatibility matrix、不可执行Artifact、安全/资源/fault/performance和真实OCI E2E全部完成后才可Module Complete。只运行LangGraph样例、只用内部函数测试、只生成一段LLM文本或兼容矩阵缺项均不满足。
