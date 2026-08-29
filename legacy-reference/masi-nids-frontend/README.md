# MASI-NIDS legacy frontend snapshot

> `REFERENCE_ONLY / NO_BUILD / NO_RUNTIME / NOT_QUALIFIED`

本目录保存旧 `MASI-NIDS/AEE_cuda/nids-frontend` 当前工作树的完整、可校验参考副本。它不是 vNext Frontend 的替代实现或 fallback；vNext 唯一前端仍是仓库根 [`../../web/`](../../web/README.md)。

## Snapshot identity

- 源仓库：本地 `MASI-NIDS`（未配置 Git remote）
- 源路径：`AEE_cuda/nids-frontend`
- 源 branch：`feature/bounded-runtime-20260726`
- 源 revision：`800944a41fdc6af9a2ea8547db6f958322b3b851`
- snapshot 类型：dirty working tree；包含 tracked 文件的当前内容和未忽略的 untracked 文件
- 捕获时间：`2026-08-29T17:43:24Z`
- 文件选择：`git ls-files --cached --others --exclude-standard -- AEE_cuda/nids-frontend`
- 文件数：`236`
- 总字节数：`1,905,214`
- manifest：[`files.sha256`](files.sha256)
- manifest SHA-256：`e65aa4cbfbe0c6a67d47eef7afd8d3058e1dea7e2162252e0680e628c71e4d67`
- source status SHA-256：`4bb158614b7ebbc41b790d5aba7cb099a1dab236989a57a8dea5d30ad3b31bdb`

`source status SHA-256` 是源仓库对该子树执行 `git status --porcelain=v1 -z` 后的原始字节摘要。完整机器可读信息见 [`SNAPSHOT.json`](SNAPSHOT.json)。

验证副本完整性：

```bash
cd legacy-reference/masi-nids-frontend/source
sha256sum --check ../files.sha256
```

## 包含与排除

`source/` 包含源码、Next/TypeScript/Tailwind配置、Dockerfile、`package.json`、`package-lock.json`、Vitest/Playwright测试和审计脚本，包括源工作树中未跟踪但未忽略的 `src/test/api-response.ts`。

未复制 `node_modules/`、`.next/`、coverage、build、dist、out、test-results、`*.tsbuildinfo`、`.env*`、旧仓库其他服务、outputs/evidence 或 Git 元数据。旧仓库根 `.env` 明确不在选择范围内。导入前的受限 secret-pattern scan 只命中测试用 `fake-sensitive-marker`，未把它解释为真实凭据；该扫描不能替代未来正式供应链审查。

## 为什么不能直接运行或合并到 `web/`

该快照使用 Node `24.18.x`、npm `11.16.x`、Next.js `16.3.0`、React `19.2.4`、Zustand、React Query、Tailwind/shadcn 与 Next server BFF。它直接依赖旧 `/events`、Workflow、Review、P4、Admin、MCP、Event-v2/v3 compatibility和用户名/密码 session语义，并默认把 BFF 指向旧 backend。

vNext `web/` 则是 Node `22.22.3` 构建的 Vue 3/Vite 静态 SPA，无 Node production runtime或BFF，只消费生成的 Go OpenAPI client与同源 `/api`、`/events`、`/oidc`。因此：

- 不得从 `source/` 向 `web/` 直接 import React/Next代码、DTO、auth store、API client、route或UI asset；
- 不得把旧 Dockerfile、package-lock、Node server、BFF proxy、用户名/密码 login或localStorage mutation带入vNext；
- 不得恢复legacy Workflow/Review/Auth/Event-v2/P4 writer控制面；
- 可保留的页面意图必须按vNext contracts和任务模型clean-room重写，并重新执行Web/Go contract、安全、可访问性、浏览器与性能门禁。

## License and supply-chain status

源仓库及前端子树在捕获时没有 `LICENSE`、`COPYING` 或 `NOTICE` 文件。该快照由本仓库用户明确要求迁入，但没有因此形成项目级或下游许可证授予。参考源码和其 npm dependency closure均未被 vNext supply-chain registry采用；在 Owner/法律/NOTICE/SBOM审查完成前，它保持 `REFERENCE_ONLY`，不能进入发布制品或生产运行时。

`legacy-reference` 已由根 `.dockerignore` 排除。即使根目录作为 Docker build context，该快照也不会发送给 builder。

## Preserved security and build risks

完整快照有意保留原 Dockerfile、package scripts 与endpoint defaults，因此源码目录技术上仍可被人手直接执行；“参考”边界依靠仓库policy、嵌套`AGENTS.md`、CI isolation test和根build排除共同约束，而不是修改历史源码使其无法解析。

- 旧 Dockerfile执行裸`npm ci`。lockfile标记`esbuild@0.28.1`、`msw@2.15.0`、`unrs-resolver@1.11.1`及可选`fsevents`含install script；`package.json#allowScripts`没有被vNext供应链门禁资格化。
- Docker/Next配置默认使用`http://nids-backend:8090`、`http://127.0.0.1:8090`和`http://127.0.0.1:8088`。它们是legacy本地默认值，不满足vNext production TLS身份边界。
- 旧UI允许提交可编辑source endpoint，并只在UI设置`loopback_only`默认值；后端是否强制SSRF/loopback约束不在本次前端快照审计范围，前端字段不能作为安全边界。
- 测试中的`fake-access-*`、`fake-sensitive-marker`和密码字段是fixture；当前扫描未发现真实credential，但正式重用前仍须重新执行secret、SBOM、许可证、install-script和endpoint审查。

任何人或自动化代理均不得在该目录运行`npm ci/install`、`npm run dev/build/start/test`或`docker build`。完整约束见同目录[`AGENTS.md`](AGENTS.md)。
