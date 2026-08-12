# MASI-NIDS-vNext 设计—需求—测试追踪矩阵

- 文档状态：`DRAFT`
- 日期：2026-08-12
- 唯一需求基线：[`../masi-nids-vnext-system-requirements-2026-08-09.md`](../masi-nids-vnext-system-requirements-2026-08-09.md)

## 1. 用途

本矩阵为实现、评审和证据聚合提供入口：稳定需求ID必须能追踪到责任模块、详细设计、Module验收、正式边界与System场景。它不是需求副本；需求正文变化时必须由Owner先更新基线，再同步本矩阵。

“应产证据”列描述未来必须产生的证据类别，不表示当前已存在。当前所有行均为`result=HOLD|NOT_RUN`、`qualification=NOT_QUALIFIED`，不存在可复用的执行证据。

### 1.1 追踪粒度与执行回填

本设计阶段的表格按资格模块或跨模块能力聚合，提供稳定导航入口。进入实现时，每个模块README与evidence manifest必须把聚合行展开为“一条稳定需求ID一行”，至少包含：`requirement_id`、稳定责任角色/身份、design anchor、module scenario ID、适用的pairwise/wave、runner/profile和实际command digest、environment/topology digest、artifact/image/config/contract digest、immutable run/evidence ID以及四个资格字段。测试设计不在这里规定逐步命令；命令与环境是在实现后作为实际执行证据回填。

未执行的行只能写`evidence=NOT_GENERATED`，不得预填未来链接、拿rehearsal/fake结果占位或用截图/摘要代替原始证据。

## 2. 九模块追踪

| 模块 | 责任 Owner 角色 | 核心需求 ID | 详细设计 | Module 验收入口 | 正式边界/系统波次 | 应产证据 | 当前证据 |
|---|---|---|---|---|---|---|---|
| P4/Switch | P4/Switch module owner | `MOD-SW-001`、`MOD-SW-FW-001`、`CONTRACT-P4-001`、`CONTRACT-P4-FW-001`、`CONTRACT-RULE-001`、`CONTRACT-TELEMETRY-001` | [`p4-switch-design.md`](../design/modules/p4-switch-design.md) | [`TEST-003`/P4专项](../testing/module-e2e-acceptance-design.md#module-p4) | [P1](../integration/pairwise-and-system-integration-design.md#p1)、[P12](../integration/pairwise-and-system-integration-design.md#p12)；[W1/W2/W6/W7/W8](../integration/pairwise-and-system-integration-design.md#system-waves) | compiled artifact/P4Info、packet oracle、bank/snapshot/counter、fault/performance、software target claim | `NOT_GENERATED` |
| Rust Edge | Rust Edge module owner | `MOD-EDGE-001`、`ARCH-004`、`ARCH-TELEMETRY-001`、`ARCH-TARGET-FLEET-001`、`FUNC-EFFECT-001`、`FUNC-TEL-001` | [`rust-edge-agent-design.md`](../design/modules/rust-edge-agent-design.md) | [`TEST-003`/Edge专项](../testing/module-e2e-acceptance-design.md#module-edge) | [P1](../integration/pairwise-and-system-integration-design.md#p1)、[P2](../integration/pairwise-and-system-integration-design.md#p2)、[P4](../integration/pairwise-and-system-integration-design.md#p4)、[P5](../integration/pairwise-and-system-integration-design.md#p5)、[P12](../integration/pairwise-and-system-integration-design.md#p12)；[W1–W8](../integration/pairwise-and-system-integration-design.md#system-waves) | actor/session/journal、three-stage WAL、route/fence、effect/readback、fault/performance | `NOT_GENERATED` |
| Central Inference | Central Inference module owner | `MOD-INF-001`、`ARCH-005`、`ARCH-MODEL-001`、`CONTRACT-INFERENCE-001`、`FUNC-INF-001`、`FUNC-INF-MODEL-001` | [`central-inference-design.md`](../design/modules/central-inference-design.md) | [`TEST-INF-001`/Central专项](../testing/module-e2e-acceptance-design.md#module-central) | [P2](../integration/pairwise-and-system-integration-design.md#p2)、[P3](../integration/pairwise-and-system-integration-design.md#p3)、[P4](../integration/pairwise-and-system-integration-design.md#p4)；[W2/W3](../integration/pairwise-and-system-integration-design.md#system-waves) | real Gateway/Triton/ORT startup、numeric、selected/observed、batch/copy、fault/performance | `NOT_GENERATED` |
| Go Control | Go Control module owner | `MOD-CTRL-001`、`FUNC-API-001`、`FUNC-GOV-001`、`FUNC-EFFECT-001`、`FUNC-FW-001`、`FUNC-RULE-001`、`FUNC-INF-MODEL-001`、`FUNC-TARGET-FLEET-001`、`PLUGIN-STAT-001` | [`go-control-core-design.md`](../design/modules/go-control-core-design.md) | [`TEST-003`/Go专项](../testing/module-e2e-acceptance-design.md#module-go) | [P4–P7](../integration/pairwise-and-system-integration-design.md#p4)、[P9–P12](../integration/pairwise-and-system-integration-design.md#p9)；[W3–W10](../integration/pairwise-and-system-integration-design.md#system-waves) | transaction/idempotency/CAS、authorization、API/SSE、manager/dispatcher、fault/performance | `NOT_GENERATED` |
| PostgreSQL | PostgreSQL module owner | `MOD-DB-001`、`DB-005`、`DB-006`、`DB-MODEL-001`、`DB-RULE-001`、`DB-FW-001`、`DB-TARGET-FLEET-001`、`DB-PLUGIN-STAT-001` | [`postgresql-state-design.md`](../design/modules/postgresql-state-design.md) | [`TEST-003`/DB专项](../testing/module-e2e-acceptance-design.md#module-postgresql) | [P4–P7、P9、P12依赖](../integration/pairwise-and-system-integration-design.md#p4)；[W3–W10](../integration/pairwise-and-system-integration-design.md#system-waves) | migration/constraint/role、HA/PITR/restore/incarnation、RPO/RTO、query/retention/performance | `NOT_GENERATED` |
| Plugin Host | Plugin Runtime Host module owner | `MOD-PLUGIN-001`、`ARCH-PLUGIN-001`、`CONTRACT-PLUGIN-STAT-001`、`PLUGIN-STAT-001` | [`plugin-runtime-host-design.md`](../design/modules/plugin-runtime-host-design.md) | [`TEST-PLUGIN-001`/Host专项](../testing/module-e2e-acceptance-design.md#module-plugin-host) | [P7](../integration/pairwise-and-system-integration-design.md#p7)、[P8](../integration/pairwise-and-system-integration-design.md#p8)；[W9](../integration/pairwise-and-system-integration-design.md#w9)；[Full E2E](../integration/pairwise-and-system-integration-design.md#full-system-e2e) | manifest/runtime/sandbox、WIT/gRPC、generation/revoke、statistics conformance、fault/performance | `NOT_GENERATED` |
| Python Analysis | Python Analysis module owner | `MOD-AGENT-001`、`CONTRACT-AGENT-001`、`AGENT-001`、`AGENT-002`、`AGENT-003`、`AGENT-004`、`AGENT-005`、`AGENT-006`、`AGENT-007`、`AGENT-008`、`AGENT-COMPAT-001` | [`analysis-plugin-design.md`](../design/modules/analysis-plugin-design.md) | [`TEST-003`/Analysis专项](../testing/module-e2e-acceptance-design.md#module-analysis) | [P10](../integration/pairwise-and-system-integration-design.md#p10)、[P11](../integration/pairwise-and-system-integration-design.md#p11)；[W10](../integration/pairwise-and-system-integration-design.md#w10) | real A2A/MCP、LangGraph/provider、grounded Artifact、compat matrix、security/fault | `NOT_GENERATED` |
| Web | Web module owner | `MOD-WEB-001`、`WEB-UX-001`、`WEB-STATE-001`、`WEB-GOV-001`、`WEB-A11Y-001`、`WEB-PERF-001`、`WEB-SEC-001`、`WEB-RULE-001`、`WEB-TARGET-FLEET-001`、`WEB-PLUGIN-STAT-001` | [`web-soc-spa-design.md`](../design/modules/web-soc-spa-design.md) | [`TEST-WEB-001`/Web专项](../testing/module-e2e-acceptance-design.md#module-web) | [P9](../integration/pairwise-and-system-integration-design.md#p9)；[W5–W10](../integration/pairwise-and-system-integration-design.md#system-waves) | production artifact/browser、user flows、a11y/security、bundle/CWV/heap/soak、compat rollback | `NOT_GENERATED` |
| Offline ML | Offline ML module owner | `MOD-ML-001`、`CORE-MODEL-001`、`CONTRACT-MODEL-001`、`FUNC-INF-MODEL-001` | [`offline-ml-pipeline-design.md`](../design/modules/offline-ml-pipeline-design.md) | [`TEST-INF-001`/ML专项](../testing/module-e2e-acceptance-design.md#module-offline-ml) | [W3](../integration/pairwise-and-system-integration-design.md#w3)；[Full model replacement chain](../integration/pairwise-and-system-integration-design.md#full-system-e2e) | reproducible run、bundle/repository、feature/label/adapter/numeric、quality/security/supply-chain | `NOT_GENERATED` |

## 3. Cross-module 能力追踪

| 能力 | 需求/决策 ID | 唯一 Owner 与参与模块 | 设计入口 | 正式验收入口 | 关键不变量 |
|---|---|---|---|---|---|
| 模块拆分与门禁 | `ARCH-001`、`ARCH-002`、`MOD-REGISTRY-001`、`TEST-GATE-001`、`TEST-010`、`ACCEPT-001` | 九模块各自owner；全局gate只聚合证据 | [`system decomposition`](../design/00-system-decomposition-and-delivery-design.md) | [`Module Complete`](../testing/module-e2e-acceptance-design.md#module-complete)、[`pairwise gate`](../integration/pairwise-and-system-integration-design.md#pairwise-gate) | 九模块先完整；rehearsal/fake不升级；缺一模块全局HOLD |
| Contract/Profile | `CONTRACT-001`、`CONTRACT-PROFILE-001`、`CONTRACT-SUPPLY-001` | `contracts/`唯一source；各模块consumer | [`supporting assets`](../design/01-supporting-assets-design.md) | `TEST-002`、`TEST-REUSE-001` | unknown major拒绝；同一golden；exact工具/输出digest |
| 实时检测与ACK | `ARCH-TELEMETRY-001`、`CONTRACT-TELEMETRY-001`、`CONTRACT-INFERENCE-001`、`FUNC-TEL-001`、`FUNC-INF-001` | P4 aggregate；Edge source/window/WAL/route；Central compute；Go/PG Event | P4/Edge/Central/Go/DB设计 | P1/P2/P3/P5；W1/W2/W4；`TEST-TEL-INF-001` | final+valid；commit-before-ACK；无逐包Digest可靠假设；无自动fallback |
| 模型模块化与替换 | `CORE-MODEL-001`、`ARCH-MODEL-001`、`FUNC-INF-MODEL-001`、`DB-MODEL-001`、`MIG-INF-001` | ML产bundle；Go/PG binding；Central loaded；Edge route；Web展示 | ML/Central/Go/Edge/Web/DB设计 | P3/P4/P5/P6/P9；W3；Full model chain；`TEST-INF-001` | bundle不可执行；loaded≠current；per-shard单路；CPU/CUDA证据独立；新label无effect权 |
| Effect治理 | `FUNC-GOV-001`、`FUNC-EFFECT-001`、`SEC-002` | Go/PG proposal/decision/intent；Edge/P4 effect | Go/Edge/P4/DB/Web设计 | P5/P6/P9/P12；W7；Full governance chain | proposal/decision不可执行；只有intent可claim；maker-checker；reject零Edge RPC |
| BMv2 firewall | `ARCH-FW-001`、`CONTRACT-P4-FW-001`、`FUNC-FW-001`、`DB-FW-001`、`MIG-P4-FW-001` | Go policy/current；Edge compile/journal；P4 execute；DB fact；Web UI | P4/Edge/Go/DB/Web设计 | P1/P6/P9/P12；W7；`TEST-P4-FW-001` | overlay/bank隔离；inactive完整后selector；readback+CAS；host firewall不冒充 |
| Rule effectiveness | `CONTRACT-RULE-001`、`FUNC-RULE-001`、`DB-RULE-001`、`OBS-RULE-001` | Go epoch/formula/current；Edge sweep；P4 counters；Web view | P4/Edge/Go/DB/Web设计 | P1/P5/P6/P9/P12；W8；`TEST-RULE-001` | installation/match/outcome分层；no traffic/reset/gap不补零；counter不证明outcome |
| Traffic replay | `CONTRACT-TRAFFIC-001`、`FUNC-TRAFFIC-001`、`SEC-TRAFFIC-001` | testkit/runner；P4/Edge/System被测 | Supporting assets/P4设计 | P1；Full detection/firewall/rule；`TEST-TRAFFIC-001` | 四类mode分开；sender≠DUT≠counter≠outcome≠detection；software-only claim |
| Target/Fleet | `ARCH-TARGET-FLEET-001`、`MOD-TARGET-FLEET-001`、`CONTRACT-TARGET-001`、`CONTRACT-FLEET-EFFECT-001`、`FUNC-TARGET-FLEET-001`、`DB-TARGET-FLEET-001` | Go Registry/Fleet；DB facts；Edge actors；P4 targets；Web | Edge/Go/DB/P4/Web设计 | P1/P5/P6/P9/P12；W6/W7；`TEST-TARGET-FLEET-001` | stable ID/lease/election fence；parent不可claim；per-target result；无跨target原子 |
| Plugin platform | `ARCH-PLUGIN-001`、`MOD-PLUGIN-001`、`TEST-PLUGIN-001`、`MIG-PLUGIN-001` | Go Manager；Host runtime；direct Analysis/service；DB facts；Web | Host/Analysis/Go/DB/Web设计 | P7/P8/P10/P11；W9/W10 | Manager控制≠Host代理；三kind封闭；无core/P4/effect权；业务代码不入core进程 |
| Plugin Statistics | `CONTRACT-PLUGIN-STAT-001`、`PLUGIN-STAT-001`、`DB-PLUGIN-STAT-001`、`WEB-PLUGIN-STAT-001`、`TEST-PLUGIN-STAT-001` | Go freeze/run/validate/current；Host/directplugin execute；DB store；Web fixed render | Go/Host/DB/Web设计 | P7/P8/P9；W9；Full statistics chain | 不新增kind/module/queue/writer；无UI代码；missing不补0；disabled不影响core |
| Analysis Agent | `CONTRACT-AGENT-001`、`AGENT-001`、`AGENT-002`、`AGENT-003`、`AGENT-004`、`AGENT-005`、`AGENT-006`、`AGENT-007`、`AGENT-008`、`AGENT-COMPAT-001` | Manager control；Analysis execute；Go/peer A2A；Go MCP；Web display | Analysis/Go/Web设计 | P10/P11；W10；Full Agent chain | direct A2A/MCP不经Host；Artifact不可执行；不进入实时/effect；兼容按行为矩阵 |
| Frontend与身份 | `FUNC-API-001`、`WEB-STATE-001`、`WEB-GOV-001`、`WEB-SEC-001`、`SEC-002` | Go session/auth/projection；Web display/input | Go/Web设计 | P9；W5–W10；`TEST-WEB-001` | same-origin；browser无token/BFF/direct core；server authorization；SSE只invalidate |
| PostgreSQL恢复 | `DB-005`、`DB-006`、`MIG-005`、`TEST-007` | DB HA/PITR；Go恢复；Edge/Central readback | DB/Go/Edge/Central设计 | P4/P5/P6；W3/W4/W6/W7；Full fault | PITR轮换incarnation；restore不打开writer；旧generation/fence失效 |
| 真实服务E2E | `TEST-REAL-E2E-001`、`TEST-004`、`TEST-005`、`TEST-006` | 九模块与runner | [`integration design`](../integration/pairwise-and-system-integration-design.md) | [12 pairwise](../integration/pairwise-and-system-integration-design.md#p1)、[10 waves](../integration/pairwise-and-system-integration-design.md#system-waves)、[Full E2E](../integration/pairwise-and-system-integration-design.md#full-system-e2e) | 内部服务全真实；clean environment；ready/mock/microbenchmark不算PASS |

## 4. 模块详细设计的共同 Requirement Set

每个模块除了上表业务ID，还必须映射并满足以下通用需求：

- `ARCH-003`：所有权、热路径和副作用唯一性；
- `CONTRACT-001`、`CONTRACT-PROFILE-001`：公开contract/profile和跨语言golden；
- `TEST-002`、`TEST-003`、`TEST-007`、`TEST-008`、`TEST-009`、`TEST-010`：语言、黑盒、故障、性能、CI/evidence分层和DoD；
- `TEST-REAL-E2E-001`：实际制品/runtime启动与真实边界；
- `TEST-REUSE-001`、`SEC-SUPPLY-001`：第三方采用、license/SBOM/provenance/退出；
- 需求基线中按该模块和claim scope明确列出的性能、可靠性、安全、部署、可观测性与迁移稳定需求 ID。

模块README和evidence manifest必须列出精确展开后的ID，不能只写通配符或“见需求”。本矩阵的聚合行不替代实现阶段的逐条映射。

## 5. 变更控制

- 新需求或强制语义：只由Owner修改需求基线并分配稳定ID；
- 新架构取舍：更新/新增ADR，再更新相关详细设计；
- Contract/profile变化：标明受影响producer/consumer、golden、Module和pairwise；
- 模块边界变化：必须更新`MOD-REGISTRY-001`，不能只增加目录/进程；
- 测试缺口：补充验收映射，但不得用测试便利改变需求；
- 证据失败：保留原始结果并使对应aggregate失效，不修改设计文字宣称PASS。

## 6. 当前状态

本矩阵已建立设计、验收和集成的稳定导航入口，但代码、contract、runner和evidence尚未形成。各Owner稳定身份也必须在模块实施启动前登记；表中的Owner角色不能替代实际负责人。所有未来证据链接必须在真实执行后以immutable ID/digest补充；当前不得将矩阵完整视为需求、模块或系统PASS。
