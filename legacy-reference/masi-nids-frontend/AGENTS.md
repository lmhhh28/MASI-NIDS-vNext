# Legacy frontend snapshot instructions

本文件适用于本目录及其 `source/` 子树，并补充仓库根 `AGENTS.md`。

- `source/` 是 `files.sha256` 绑定的 immutable historical snapshot。禁止直接修改、格式化、升级依赖或重新生成 lockfile；需要刷新时创建新的 snapshot identity、manifest 和 provenance。
- 禁止在本目录运行 package manager、Next/Vitest/Playwright、Docker build、dev server、test、audit、codegen 或任何可能执行 dependency lifecycle script 的命令。
- 禁止从 vNext 模块 import/link/copy 本目录源码、类型、BFF、auth store、API client、route、asset或build output。
- 禁止把旧 Workflow/Review/Auth/Event/P4/Admin/MCP/BFF 行为作为 vNext兼容路径、fallback、事实源、writer或资格证据。
- 只允许只读检查：查看文本、执行 `sha256sum --check`、运行仓库级 `scripts/ci/test_legacy_reference_isolation.py`、扫描secret/license/SBOM，或把行为重述为独立vNext需求/测试。
- 源树没有项目许可证声明。未经Owner/法律/NOTICE/SBOM审查，不得把入库解释为下游许可或把源码/依赖放入发布制品。
