# Central Inference CPU/CUDA 与真实服务 E2E 独立评估

- 日期：2026-08-12
- 对应需求基线：`vNext-requirements-1.17`
- 性质：成熟方案调研与独立工程判断；规范性结论见ADR-0017和稳定需求ID
- 当前状态：需求已确认；实现、CPU/CUDA测试、完整E2E与生产资格均为`result=NOT_RUN, qualification=NOT_QUALIFIED`（前置profile未冻结的项为`result=HOLD`）

## 1. 独立结论

建议确认当前方向：

1. 保持Central Inference单一架构，不恢复Edge-local inference。
2. 首期支持两个启动时互斥的profile：ORT CPU和ORT CUDA。
3. 管理员在启动前显式选择；硬件自检只校验并给建议，不自动选择或fallback。
4. 不实现“模型出错就自动换旧模型/另一backend”的复杂容灾；保留上线前资格、启动readback、人工exact rollback；只有选择HA profile时才以跨故障域同profile static N+1提供推理HA。
5. 实现阶段必须真实启动相关服务。模块隔离可以使用邻居fake，但正式pairwise和system E2E必须启动真实内部服务并走公开边界。
6. 资格证据必须分开level、applicability、result、qualification与claim scope；single表示一个failure domain而非single replica，CPU/CUDA、single/HA不能互相继承。
7. 首期本地/CI正式E2E固定`e2e-runner-compose/v1`，流量场景精确选择runner/backend，不做“best available”自动fallback。

这是性能、兼容性和工程复杂度之间更均衡的方案。CPU兼容解决无CUDA机器的可运行性，CUDA保留高吞吐能力；启动绑定避免运行时分支；真实服务E2E避免“合同测试全绿但制品、驱动、证书或服务拓扑起不来”。

## 2. 成熟方案证据

### 2.1 ONNX Runtime适合做统一模型合同下的设备适配层

[ONNX Runtime execution providers](https://onnxruntime.ai/docs/execution-providers/)把不同设备执行放在统一API后，并按注册优先级把节点放到能够执行它们的provider；CUDA后注册CPU时，不受CUDA支持的节点可能在CPU执行。[CPU threading](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)暴露intra/inter-op、execution mode、affinity、NUMA和spinning；[CUDA EP](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)要求明确CUDA/cuDNN兼容并提供GPU相关options。

独立判断：可以共享model/wire/Gateway代码，但CPU/CUDA绝不能共用一个笼统性能profile。CPU的thread/NUMA/arena与CUDA的driver/VRAM/stream/copy决定不同故障和容量边界，必须分别冻结、测试、出证据；CUDA profile中合法的host-side operator placement必须预先声明、读回和测量，运行期新CPU接管是profile drift而不是容灾。

### 2.2 Triton可以保持一个服务架构、两个构建/配置profile

[Triton model configuration](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_configuration.html)支持CPU/GPU model instance，且禁用auto-complete后仍可能为缺失的instance group补默认值；[build customization](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/build.html)支持按需要构建后端与GPU能力；[model management](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html)的`NONE`模式在启动时尝试加载repository中的全部模型并忽略运行期变化。

独立判断：复用Triton的执行、动态合批和instance scheduling，项目自己保留model current、pool generation、route、WAL与rollback。NONE repository snapshot必须只包含exact binding及声明依赖，显式固定instance_group kind/count/device。CPU镜像不必携带不需要的CUDA栈；CUDA镜像固定完整兼容矩阵。两个制品实现同一外部合同，但SBOM、digest和资格独立。

### 2.3 健康检查只解决启动编排，不等于E2E

[Docker Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/)明确：普通启动顺序只保证容器running；要等待dependency readiness需显式healthcheck与`service_healthy`。[Kubernetes probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/)也把startup、readiness和liveness分成不同目的；readiness只决定是否接收流量，liveness决定何时重启。

独立判断：这些机制适合确保测试在服务可接收流量后开始，却不能证明Event已入库、P4表项已readback、规则命中、浏览器状态正确或故障恢复成功。因此健康检查是E2E runner的前置条件，不是断言或PASS。

### 2.4 Playwright适合驱动真实Web边界，但不应拥有整套服务编排

[Playwright webServer](https://playwright.dev/docs/test-webserver)可以启动一个或多个server并等待URL，但官方示例也把它定位在测试前启动本地server。MASI完整E2E还包含BMv2、Edge、Triton、PostgreSQL、Plugin Host等异构组件。

独立判断：Playwright负责真实浏览器行为、可访问性和同源API/SSE断言；完整本地/CI服务生命周期固定由`e2e-runner-compose/v1`管理。生产/多主机可由精确deployment profile选择其他adapter，但不能自动切换。避免让前端测试配置成为系统部署事实源。

## 3. “模型容灾”应拆成三件事

| 问题 | 首期需要 | 不建议首期做 |
|---|---|---|
| 错误模型能否上线 | immutable bundle、离线质量/numeric/security资格、startup loaded-model readback | 自动认为签名或hash等于质量正确 |
| 正在运行的模型是否可恢复 | current/previous exact generation、人工durable rollback、旧结果fence | 错误触发自动加载旧模型、runtime hot swap |
| 推理服务节点是否可用 | single profile固定一个failure domain、允许域内1..N同profile副本并明确整域中断语义；需要HA时同profile跨域N+1 | 把域内多副本称为HA、CPU作为CUDA故障备用、异backend混合worker |

如果Owner能保证模型经过充分调试，自动模型fallback的收益更小，而状态机、误回滚和语义漂移风险仍存在。保留人工exact rollback已经覆盖“发现上线错误后安全退回”的必要能力。节点/网络故障由同profile replica解决；没有HA预算时诚实使用single profile。

## 4. CPU兼容的推荐实现

### 4.1 配置与启动

启动配置必须包含明确`runtime_profile_id`，只接受registry中的CPU或CUDA profile。runner先执行硬件preflight，将实际CPU feature/NUMA/RAM或GPU/driver/CUDA/cuDNN/VRAM写入observed envelope，再与desired profile比较。

```text
explicit desired profile
→ hardware/runtime probe
→ exact compatibility validation
→ image/exact repository closure/model/config/instance-group verification
→ Triton NONE startup-only exact binding load
→ warmup + numeric self-test
→ Gateway GetLoadedModel/GetPoolStatus readback
→ readiness
```

probe可以给人“本机检测到GPU，建议选择CUDA”的提示，但不能自行改写desired profile。这样同一配置在不同机器上不会悄悄跑出不同backend。

### 4.2 构建与依赖

- CPU artifact：C++ Gateway + pinned CPU-capable Triton/ORT，固定CPU target/thread/NUMA配置，避免不必要CUDA依赖。
- CUDA artifact：C++ Gateway + pinned Triton/ORT CUDA，固定CUDA/cuDNN/driver/GPU兼容矩阵。
- 两种artifact都使用exact repository closure与显式instance group；CUDA另冻结ORT provider partition/host-side operator placement。
- 共享Protobuf、Gateway adapter、model manifest和golden；分开image digest、SBOM、runtime config和性能证据。
- OpenVINO只作为未来CPU性能候选。其[CPU device](https://docs.openvino.ai/nightly/openvino-workflow/running-inference/inference-devices-and-modes/cpu-device.html)提供额外性能调优能力，但首期同时维护ORT CPU和OpenVINO会无必要扩大矩阵。

### 4.3 性能预期

不能预先声称CPU一定足够或CUDA一定更快。最终比较必须使用同一model、batch、network、traffic/window workload和packet/window→Event端到端SLO：

- CPU关注cores、thread pools、NUMA、RAM bandwidth、batch填充与尾延迟；
- CUDA关注GPU/VRAM、host-device copy、stream、batch填充与尾延迟；
- 两者都测Gateway/Triton排队、network、Edge WAL和PostgreSQL ACK；
- microbenchmark只用于定位瓶颈，不能替代端到端容量资格。

## 5. 真实服务E2E的推荐分层

### 5.1 Module black-box

真实启动被测release-candidate binary/OCI及其runtime，只走公开边界。允许用contract fake模拟未集成邻居。例如：

- Edge真实运行，P4和Gateway可用test fixture；
- Central Inference真实运行Gateway+Triton+所选ORT，Edge/Go可用fixture；
- Web使用production bundle和真实浏览器，Go可用OpenAPI fake。

它只证明模块自身，不证明pairwise。

### 5.2 Pairwise

所有模块都Module Complete后，在干净环境启动边界两侧真实制品。例如Edge↔Central Inference必须是真实Edge、Gateway、Triton和selected ORT；Go↔PostgreSQL必须是真实Go、migration和PostgreSQL。pair内任一侧fake都只能算rehearsal。

### 5.3 System

最小正式链真实启动：

```text
BMv2/P4Runtime
→ Rust Edge
→ selected Central Inference CPU or CUDA
→ Go Control + real PostgreSQL
→ Vue Web + real browser

Go Plugin Manager
→ Rust Plugin Host
→ Python Analysis Plugin
→ real MCP/A2A boundaries
```

外部LLM允许deterministic provider fixture以保证测试稳定，但内部Analysis/Host/MCP/A2A服务必须真实；真实provider另做受控smoke。系统场景至少覆盖检测、Event、人工proposal/authorization、effect intent、Edge P4 write/readback、规则counter/outcome、前端展示，以及推理/网络/DB/P4故障恢复。

### 5.4 Runner与证据

首期本地/CI正式Module、pairwise与BMv2 System E2E固定`e2e-runner-compose/v1`，精确锁定Docker Compose/Engine/API与runner image；不在Compose-like/Podman/systemd/Kubernetes之间自动探测。生产/多主机环境可由其deployment profile选择一个adapter并独立资格化。runner的职责只有：

- 从exact image/config/secret-file digest创建隔离网络和volume；
- 运行独立migration job，再按dependency/health等待startup；
- 执行fixture、故障和浏览器driver；
- 收集service inventory、start/ready/drain/stop timeline、structured results和hash manifest；
- 无论PASS/FAIL都清理隔离资源，并报告残留。

runner不得成为第二deployment/current/effect/P4 owner。生产制品禁止运行时从公网拉依赖；测试使用隔离identity、名称含`test`的数据库和BMv2 target。

每个traffic scenario还必须精确绑定一个traffic mode、fixture digest、runner/backend、topology、direction和rewrite。精确backend缺失或失败时为HOLD/NOT_RUN；换工具意味着新scenario/evidence，不能通过“best available”静默获得原scenario PASS。

## 6. 工程复杂度评估

以下是设计阶段估算，不是已测项目数据：

| 变化 | 初始复杂度 | 长期影响 |
|---|---|---|
| CUDA-only改为显式CPU/CUDA双profile | 中等增加 | 增加一套build/SBOM/numeric/performance/fault矩阵，但共享合同/Gateway/rollout，远低于Edge-local+central双架构 |
| 不做自动模型fallback | 明显降低 | 删除运行时选择、跨backendretry、隐式旧模型恢复和混合worker状态机 |
| single/HA profile分离 | 小到中等增加 | 避免开发环境被迫模拟HA，也防止单故障域内多副本冒充HA |
| 资格四字段与claim scope | 小到中等增加 | 增加schema/聚合实现，但消除HOLD、N/A、阶段、CPU/CUDA与single/HA误继承 |
| 固定Compose E2E runner | 小幅增加 | 需要锁定版本和清理故障矩阵，但减少多runner组合与环境漂移 |
| Module真实启动 | 中等增加 | 早期暴露缺库、配置、证书、runtime和shutdown问题，降低后期集成返工 |
| Pairwise/System真实E2E | 中到较高增加 | 需要环境编排、证据、故障和清理；但属于系统可交付的必要成本 |

相对整个项目，CPU profile主要增加Inference与部署/测试矩阵，不改变P4、Go事实、插件或前端的业务所有权。真实E2E主要增加testkit/deploy/CI工作，不增加生产热路径。运行时性能不会因“文档要求E2E”下降；CPU/CUDA双profile也不会同时常驻，因此正常请求没有动态分派开销。

## 7. 最终采用矩阵

| 能力 | 决定 |
|---|---|
| Central Inference共同Gateway/gRPC合同 | `ADOPT` |
| ORT CPU显式启动profile | `ADOPT` |
| ORT CUDA显式启动profile | `ADOPT` |
| 启动硬件probe | `ADOPT validate/recommend only` |
| CPU↔CUDA自动选择/fallback | `REJECT` |
| 人工exact previous rollback | `ADOPT` |
| 同profile跨域N+1 | `ADOPT when availability-ha selected` |
| OpenVINO/TensorRT | `CONDITIONAL future explicit profile` |
| fake-only pairwise/system E2E | `REJECT` |
| `e2e-runner-compose/v1`本地/CI编排 | `ADOPT exact first-release profile` |
| Podman/systemd/Kubernetes测试编排 | `CONDITIONAL exact deployment profile only; no auto switch` |
| Playwright真实浏览器驱动 | `ADOPT` |

## 8. 验收建议

在实现开始前冻结：

- CPU/CUDA profile schema和stable error；
- selected/observed startup envelope、repository closure、explicit instance group与ORT provider partition；
- 两制品构建、SBOM、runtime/driver矩阵；
- single一个failure domain内1..N副本/HA跨域语义、deployment tier与required/min-ready/max-unavailable；
- Module/pairwise/system service inventory；
- `qualification-evidence/v1`四字段与claim scope、`e2e-runner-compose/v1` exact版本/资源/清理、traffic runner精确选择、E2E scenario、fault injection点、原始evidence schema与绝对性能门槛。

在这些内容尚未实现并实际运行前，只能称为“需求与架构已确认”，不能称CPU可用、CUDA可用、E2E通过或生产合格。

## 参考

- ONNX Runtime execution providers：<https://onnxruntime.ai/docs/execution-providers/>
- ONNX Runtime threading/NUMA：<https://onnxruntime.ai/docs/performance/tune-performance/threading.html>
- ONNX Runtime CUDA EP：<https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html>
- Triton model configuration/build/model management：<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_configuration.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/customization_guide/build.html>、<https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/model_management.html>
- Docker Compose startup order：<https://docs.docker.com/compose/how-tos/startup-order/>
- Kubernetes startup/readiness/liveness probes：<https://kubernetes.io/docs/concepts/workloads/pods/probes/>
- Playwright webServer：<https://playwright.dev/docs/test-webserver>
- OpenVINO CPU device：<https://docs.openvino.ai/nightly/openvino-workflow/running-inference/inference-devices-and-modes/cpu-device.html>
- 现行决策：`../adr/0017-central-inference-runtime-selection-and-real-e2e.md`
- 唯一需求基线：`../masi-nids-vnext-system-requirements-2026-08-09.md`
