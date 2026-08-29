# Legacy implementation references

本目录只保存旧 MASI-NIDS 实现的不可执行参考快照，用于 clean-room 行为、数据、交互和性能对照。它不属于 `MOD-REGISTRY-001` 的九个模块，不是 vendored runtime dependency，也不参与 vNext build、OCI context、CI、deployment、Module Gate、pairwise 或 System E2E。

## 强制边界

- 当前生产候选前端只有 [`../web/`](../web/README.md) 的 Vue 3/Vite 静态 SPA。
- 禁止从参考源码向 Go/Rust/C++/Python/P4/Web runtime直接 import、link、copy artifact 或共享 writable volume。
- 禁止运行参考目录中的 Dockerfile、Next server/BFF、migration、真实 mutation或任何旧后端连接，除非未来需求基线和隔离 test profile明确授权。
- 旧 Workflow、Review、Auth、Event v2、P4 controller、MCP/Admin mutation 和 BFF 路径不属于 vNext 兼容层。
- 参考 snapshot 的测试、build 或截图不能成为 vNext Module、pairwise、System E2E 或 production evidence。
- 源项目没有许可证声明时，只能记录来源与权利状态；入库不自动授予下游使用、修改或再分发许可。

## 快照

| 快照 | 用途 | 状态 |
|---|---|---|
| [`masi-nids-frontend/`](masi-nids-frontend/README.md) | 旧 Next.js/React SOC 前端及测试的完整工作树参考 | `REFERENCE_ONLY / NO_BUILD / NO_RUNTIME` |

任何后续刷新必须创建新的 snapshot identity、source revision/status digest 和 per-file manifest；不得无记录覆盖旧快照或把差异直接合入 `web/`。
