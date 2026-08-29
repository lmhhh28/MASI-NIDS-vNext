# MASI-NIDS-vNext GitHub CI/CD 设计

- 文档状态：`DRAFT`
- 日期：2026-08-27
- 当前发布资格：`qualification=NOT_QUALIFIED`
- 关联需求：`TEST-001`、`TEST-002`、`TEST-003`、`TEST-GATE-001`、`TEST-REAL-E2E-001`、`TEST-007`、`TEST-008`、`TEST-009`、`ACCEPT-001`、`DEC-044`
- 关联设计：[`00-system-decomposition-and-delivery-design.md`](00-system-decomposition-and-delivery-design.md)、[`01-supporting-assets-design.md`](01-supporting-assets-design.md)、[`../testing/module-e2e-acceptance-design.md`](../testing/module-e2e-acceptance-design.md)、[`../integration/pairwise-and-system-integration-design.md`](../integration/pairwise-and-system-integration-design.md)

## 1. 结论

GitHub 自动化分为“快速反馈、正式模块门禁、正式集成、发布”四层。层级之间只传递不可变源码、制品 digest 和机器可读 evidence，不把上游 job 成功自动改写成下游资格。

当前仓库只自动启用前两层：

1. `.github/workflows/ci.yml` 在 GitHub-hosted runner 上执行无特权、无 secret 的快速语言和契约检查。
2. `.github/workflows/module-gates.yml` 只在隔离 self-hosted runner 上按周或手动执行九模块真实门禁，并上传该次 run 的不可变 evidence。

正式 pairwise/system workflow 和 GHCR 发布/部署保持 fail-closed。当前源码仍是 dirty-tree 联调后的未保护基线，十二个 pairwise、十个 system waves、目标部署资格与 production HA 未完成；因此任何 CI 绿灯都不能显示为 `SYSTEM_E2E`、`PRODUCTION` 或 production qualified。

## 2. 触发与门禁分层

| 层级 | 触发 | Runner | 必须执行 | 产物与资格语义 |
|---|---|---|---|---|
| PR 快速 CI | `pull_request`、`push main`、手动 | GitHub-hosted Ubuntu | 仓库卫生、合同 validator、format/lint/type/unit、非特权 build | 只证明本次快速检查；不产生 Module Complete 或发布资格 |
| 模块正式门禁 | 每周定时、`workflow_dispatch` | 隔离 self-hosted，按模块标签调度 | 发布候选 binary/OCI、真实公开边界、fault/recovery/security/supply、absolute performance、正式 3,600 秒 soak | 上传 exact run evidence；资格只从 evidence 字段派生 |
| Pairwise/System | 后续受保护手动入口 | clean-start 专用 runner/target/database | 十二个真实 pairwise、十个 system waves、Full E2E；内部 MASI 服务不得 fake | 每个 exact scope 独立 `PAIRWISE|SYSTEM_E2E` evidence；当前不启用 |
| Release/CD | 受保护 tag + Environment 审批 | 隔离 builder/deployer | 重新核对 source/image/config/contract/evidence digest，构建/签名 immutable OCI，按 digest 部署并验证 | 只有全部 required scope `PASS+QUALIFIED` 才发布；当前不启用 |

PR 不运行来自 fork 的 secret、self-hosted privileged runner、P4 target、真实 PostgreSQL restore、Cosign key或生产环境。需要 Docker/`NET_ADMIN`/`NET_RAW`、PITR、固定 CPU/NUMA、浏览器三引擎或长时 soak 的检查进入模块门禁。

## 3. 快速 CI

快速 CI 使用最小 `contents: read` 权限，并把所有第三方/官方 Action 固定到 40 位 commit SHA。任务并行执行且同一 PR 的旧 run 可取消：

- repository hygiene：拒绝 GitHub 100 MiB 附近的大文件、虚拟环境、`node_modules`、构建目录、浏览器输出、私钥/高置信 token，以及未固定 SHA 的 Action；
- P4：运行合同、golden 和纯 Python runner 单测；依赖私有 pinned p4c image 的真实 compile/packet oracle 留给隔离 runner；
- Rust Edge/Plugin Host：`fmt`、Clippy、合同/sentinel 和 unit/property；
- Go Control/PostgreSQL：`gofmt`、`go vet`、unit/contract/evidence tooling；真实 PostgreSQL、PITR、race/coverage/deep gate 由模块门禁运行；
- Analysis/Offline ML：固定 CPython 3.12、uv 0.12.3、lock sync、Ruff、Pyright、`unittest` 和合同校验；不使用 pytest；
- Web：固定 Node/npm lock graph，运行合同、sentinel、lint、typecheck、Vitest、production bundle、安全和 supply 校验；真实 OCI/三浏览器/性能/soak 留给模块门禁；
- Central Inference：运行公共合同与 numeric golden；真实 C++/Triton/ORT、sanitizer、OCI 和 CPU/CUDA 独立门禁留给固定环境。

这些裁剪是快速反馈 scope，不允许被 evidence 聚合为完整模块结果；被裁剪项在正式门禁中仍是 required。

## 4. 正式模块门禁

`module-gates.yml` 使用唯一 run ID `gh-<run_id>-<attempt>-<module>`，`fail-fast=false`，每个模块最多运行 360 分钟。所有 gate 仍调用模块自己的唯一入口，CI 不复制业务测试或重写 summary：

| 模块 | self-hosted 标签 | 正式入口 |
|---|---|---|
| P4/Switch | `masi-isolated, masi-p4` | `MASI_P4_QUALIFICATION_MODE=formal testkit/p4_switch/run-module-e2e.sh` |
| Rust Edge | `masi-isolated, masi-edge` | `MASI_EDGE_FORMAL_SOAK=1 edge-rs/scripts/run-module-gates.sh` |
| Central Inference CPU | `masi-isolated, masi-central-cpu` | `MASI_INF_FORMAL_SOAK=1 infer-cpp/scripts/run-module-gates.sh` |
| Go Control | `masi-isolated, masi-control` | `MASI_CONTROL_FORMAL_SOAK=1 control-go/scripts/run-module-gates.sh` |
| PostgreSQL | `masi-isolated, masi-postgres` | `MASI_DB_FORMAL_SOAK=1 db/scripts/run-module-gates.sh` |
| Plugin Host | `masi-isolated, masi-plugin-host` | `plugin-host-rs/scripts/run-module-gates.sh` |
| Analysis | `masi-isolated, masi-analysis` | `analysis-py/scripts/run-module-gates.sh` |
| Offline ML | `masi-isolated, masi-offline-ml` | `ml-py/scripts/run-module-gates.sh <run-id>` |
| Web | `masi-isolated, masi-web` | `MASI_WEB_FORMAL_SOAK=1 web/scripts/run-module-gates.sh` |

Runner 必须是带 `masi-ephemeral` 标签的一次性实例，并预装、固定模块 README 要求的 Docker、Go、Rust、Python/uv、Node、C++/gRPC/Triton、浏览器、Syft/Trivy/Cosign 等工具。P4 runner 另需隔离 target、root/capability、两个 exact source archive、离线 scan cache 和受控签名材料。Central 性能 runner 必须与 profile 的 CPU/NUMA/RAM 匹配；未来 CUDA 只能使用独立标签和独立 evidence，不能继承 CPU 结果。模块脚本完成 exact cleanup；无论 job 成败，外部 runner controller 随后销毁实例，避免用广域 Docker/process 清理误伤共享资源。

Web 正式门禁还要求 exact candidate 的人工可访问性 evidence：screen reader、全键盘、200% zoom/reflow 与高风险对话框 focus 四项都必须由具名 reviewer 实测并绑定 source/image/artifact digest。`MASI_WEB_MANUAL_A11Y_EVIDENCE` 只能指向 self-hosted runner 上受保护的普通文件；缺失时稳定产生 `NOT_RUN/NOT_QUALIFIED`，不得由 axe 自动结果代替。

每次 run 的完整目录作为 GitHub Artifact 上传；`latest.json` 只作为指向不可变 run 的指针。canonical verifier 必须先按模块公开 schema、run/source identity 和 pointer digest 复核 summary。命令提前失败时只允许上传经 `module-runner-failure/v1` 校验并绑定 command/log digest 的诊断 evidence。只有模块入口成功、canonical summary 存在且绑定同一 run ID、summary 仍能派生 `overall_module_complete=true` 后，job 才能成功。失败、`HOLD`、`NOT_RUN`、缺文件或上传失败均保留原结果，不允许 CI 生成“force pass”。

## 5. 正式集成与 CD 放行

正式集成 workflow 只有在九模块 current-source aggregate 同时有效后才实现并启用。它必须使用 clean project/network/volume/database/target identity，按需求顺序执行十二个 pairwise 与十个 system waves，并为每个 scope 保留独立 evidence。Connected rehearsal runner 可以作为诊断入口，但输出继续是 `REHEARSAL/NOT_QUALIFIED`，不能充当 release check。

Release/CD 的放行条件全部满足前，禁止向可部署 channel 推送或更新环境：

1. tag 指向受保护、已签名且工作树可重现的 commit；
2. 九模块及本次声明所需 pairwise/system evidence 与该 commit 的 source/config/contract/image digest 完全一致；
3. required `APPLICABLE` 项全部为 `PASS+QUALIFIED`，open P0 为 0，无过期 waiver；
4. CPU/CUDA、single/HA、deployment tier 和 topology 分开判定；只有 `production-ha` 可申请 production qualification；
5. 构建输出使用 immutable tag 和 manifest digest，不发布或消费 `latest`；
6. 为源码、builder、runtime 和 image 生成 SPDX/CycloneDX SBOM、离线 vulnerability/config/secret scan、provenance 和签名；
7. 以 `repository@sha256` 更新受控部署清单，经 protected GitHub Environment 审批后部署；migration 仍是独立 one-shot job；
8. 部署后验证 startup/readiness/liveness、业务 readback、回滚与 cleanup；基础设施 healthy 不等于业务 PASS。

当前没有满足以上条件的 release aggregate，所以仓库不配置自动 GHCR push 或自动部署。首次开放时应新增受保护的 `release-candidate` 和 `production` Environment：构建 job 只授予 `packages: write`，keyless provenance/signing job 才授予 `id-token: write`，部署 job 不拥有构建权限。

## 6. GitHub 仓库策略

以下策略是正式 self-hosted gate 或发布前的必需配置；首次 push 后立即配置并核验：

- 私有仓库，默认分支 `main`，仅允许 PR 合并，优先 squash；
- branch protection/ruleset 要求 `CI / repository-hygiene` 及适用快速检查，禁止 force-push 和删除；
- workflow 默认权限为只读，不允许 Actions 创建或批准 PR；
- 只允许 GitHub-owned 且 SHA 固定的 Actions，以及仓库内脚本；
- secret scanning、push protection、Dependabot alerts 在账号能力允许时开启；
- `ci-untrusted`、`module-gates`、`release-candidate`、`production` 四个 Environment 分权；`module-gates` 只允许受保护 `main`，后三者要求人工 reviewer；
- self-hosted runner 不接收 fork PR；每次正式 run 后销毁或清理 workspace、container、network、volume 和临时凭据。

## 7. 运维与故障语义

- PR run 目标反馈时间为 15 分钟级；耗时任务独立并行，旧 PR run 可取消。
- 正式 soak 的 60 秒 warmup 不计入四个连续 900 秒 phase；workflow timeout 不能截短 profile。
- GitHub 服务、runner 或 artifact upload 故障使该项 `NOT_RUN|HOLD`，不得沿用旧 run。
- rerun 使用新的 GitHub run attempt 和新的 evidence run ID，不覆盖历史失败。
- GitHub Artifact 是传输/审阅副本，不自动成为长期审计存储；受保护 release 需要把 exact evidence、SBOM、provenance 和签名 bundle 复制到不可变 retention 后端并记录 digest。
