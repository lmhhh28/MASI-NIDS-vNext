# MASI-NIDS-vNext 详细设计文档索引

- 文档状态：`DRAFT`
- 当前实现资格：`qualification=NOT_QUALIFIED`
- 日期：2026-08-22
- 唯一需求基线：[`../masi-nids-vnext-system-requirements-2026-08-09.md`](../masi-nids-vnext-system-requirements-2026-08-09.md)
- 总体架构：[`../architecture/masi-nids-vnext-overall-architecture-2026-08-11.md`](../architecture/masi-nids-vnext-overall-architecture-2026-08-11.md)

本目录把已经确认的需求和 ADR 细化为可实现的模块设计。它不改变需求基线，不新增模块、事实源、writer、队列或授权路径。发生冲突时，优先级固定为：Owner 确认的需求基线 → Accepted 且未被替代的 ADR → 总体架构 → 本目录设计。

文档存在只说明设计输入已经形成，不说明代码、镜像、测试或性能门槛已经通过。模块状态只从与当前source/artifact匹配的机器可读evidence重派生；没有实际证据的能力保持`result=HOLD|NOT_RUN`、`qualification=NOT_QUALIFIED`。ADR-0019的ML source/recipe已接受，但exact dataset revision、dataset/explanation contract、训练winner和`ml-py/`证据仍未形成。

## 阅读入口

1. [`00-system-decomposition-and-delivery-design.md`](00-system-decomposition-and-delivery-design.md)：九个模块、所有权、依赖与“先完整实现、后正式集成”的总设计。
2. [`01-supporting-assets-design.md`](01-supporting-assets-design.md)：Contracts、Testkit、Deploy、Docs/Evidence 四类支撑资产，及其不能越过的边界。
3. [`02-github-ci-cd-design.md`](02-github-ci-cd-design.md)：GitHub 上的快速 CI、正式模块门禁、证据保留与 fail-closed CD 发布流程。
4. 模块详细设计：
   - [`modules/p4-switch-design.md`](modules/p4-switch-design.md)
   - [`modules/rust-edge-agent-design.md`](modules/rust-edge-agent-design.md)
   - [`modules/central-inference-design.md`](modules/central-inference-design.md)
   - [`modules/go-control-core-design.md`](modules/go-control-core-design.md)
   - [`modules/postgresql-state-design.md`](modules/postgresql-state-design.md)
   - [`modules/plugin-runtime-host-design.md`](modules/plugin-runtime-host-design.md)
   - [`modules/analysis-plugin-design.md`](modules/analysis-plugin-design.md)
   - [`modules/web-soc-spa-design.md`](modules/web-soc-spa-design.md)
   - [`modules/offline-ml-pipeline-design.md`](modules/offline-ml-pipeline-design.md)
   - Offline ML取舍与数据源：[`../adr/0019-offline-ml-dataset-training-and-explanation-boundary.md`](../adr/0019-offline-ml-dataset-training-and-explanation-boundary.md)
5. [`../testing/module-e2e-acceptance-design.md`](../testing/module-e2e-acceptance-design.md)：模块独立 E2E 的验收对象、场景、不变量、证据与阻断条件。
6. [`../integration/pairwise-and-system-integration-design.md`](../integration/pairwise-and-system-integration-design.md)：全模块门禁之后的十二个 pairwise 边界、十个系统波次和 Full E2E。
7. [`../traceability/design-requirement-matrix.md`](../traceability/design-requirement-matrix.md)：需求 ID、模块、设计、测试与证据的追踪入口。

## 文档约定

- “模块”只指 `MOD-REGISTRY-001` 的九个资格主体；模块内部组件、进程或容器不会因此自动成为新模块。
- “独立完成”表示模块自身功能完整，能以发布候选 binary/OCI 和真实 runtime 启动，并通过公开边界完成黑盒 E2E、故障、性能、安全和兼容门禁；不表示它可以脱离整个产品永久单独运行。
- 模块黑盒测试可以使用契约一致的邻居 fake；正式 pairwise 的两侧和 System E2E 的内部 MASI 服务不得 fake。
- 测试设计只说明要验证什么、必须保持什么不变量、需要什么证据以及何时阻断，不规定内部函数、编码步骤或逐条命令。可复制命令由各模块实现阶段的 README 和受控 runner profile 提供。
- `REHEARSAL`、`MODULE`、`PAIRWISE`、`SYSTEM_E2E`、`PRODUCTION` 是证据 level；`PASS|FAIL|HOLD|NOT_RUN` 是 result。不得把二者拼成新的 wire 状态。
