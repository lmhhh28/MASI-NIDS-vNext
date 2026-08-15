# PostgreSQL State 模块详细设计

- 模块 ID：`MOD-DB-001`
- 目录：`db/`、`db/migrations/`
- 文档状态：`DRAFT`
- 主要需求：`DB-001`、`DB-002`、`DB-003`、`DB-004`、`DB-005`、`DB-006`、`DB-007`、`DB-GOV-001`、`DB-PLUGIN-001`、`DB-PLUGIN-STAT-001`、`DB-MODEL-001`、`DB-REDIS-001`、`DB-RULE-001`、`DB-FW-001`、`DB-TARGET-FLEET-001`、`REL-001`、`REL-002`、`REL-003`、`MIG-001`、`MIG-003`、`MIG-005`、`MIG-INF-001`、`MIG-TEL-INF-001`、`MIG-P4-FW-001`、`MIG-TARGET-FLEET-001`、`MIG-PLUGIN-001`、`TEST-003`、`TEST-007`、`TEST-008`、`TEST-010`
- 主要 ADR：ADR-0001、ADR-0003、ADR-0006、ADR-0008、ADR-0009、ADR-0014、ADR-0015、ADR-0017、ADR-0018

## 1. 模块目标

PostgreSQL 18 是核心持久事实的唯一来源。State模块交付精简vNext schema、不可修改的migration链、连接/事务约束、分区/retention、角色隔离、HA/PITR/restore和兼容证据。

它不执行业务决策、设备操作、插件任务或模型部署；数据库过程、trigger、backup/HA工具不能成为第二dispatcher。Go Control是核心schema唯一业务writer，其他模块只能使用明确隔离的最小role或完全无DB凭据。

## 2. 逻辑 Schema 域

| 逻辑域 | 主要事实 | Writer |
|---|---|---|
| `core` | Event/Incident/Evidence、idempotency、audit | Go Control |
| `effect_governance` | proposal、decision、intent、claim/attempt/result | Go Control |
| `firewall` | policy revision、overlay、binding、activation、readback ref | Go Control |
| `rule_observation` | epoch、sample current、status、5m/1h rollup | Go Control |
| `target_fleet` | stable target、lifecycle、assignment、parent/child/wave | Go Control |
| `model_platform` | revision、qualification、incarnation、pool/shard binding、operation/readback | Go Control |
| `plugin_platform` | manifest revision、qualification、binding、revocation | Go Control |
| `plugin_statistics` | schedule、run、artifact、current/history | Go Control |
| `analysis_private` | Analysis自身最小状态/审计（若启用） | 隔离Analysis role，仅本schema |

实际schema命名在contract/migration设计时冻结；逻辑域不能被实现为共享无类型JSON表。热查询identity、status、generation、time、scope使用强类型列，JSONB只保存非热、受schema验证的扩展内容。

## 3. 核心约束设计

### 3.1 Identity 与不可变事实

- stable IDs永不复用，业务identity与display name/endpoint/tag分离；
- immutable revision/decision/audit采用append-only row和canonical digest；
- same idempotency key + same payload digest返回原记录，different digest唯一约束冲突；
- current/previous通过foreign key指向immutable revision，不复制可漂移body；
- generation/incarnation/epoch分别建模，禁止用一个通用generation列混装。

### 3.2 CAS 与 Lease

Effect claim、assignment、fleet gate、model binding、plugin binding、statistics run/current和rule projection使用expected identity/generation/digest条件更新。Lease包含owner/runtime identity、issued/expiry/fence；超时后恢复仍需CAS，不以进程存活或时钟猜测所有权。

Fleet parent不可有claim字段；只有per-target `effect_intents`具dispatcher lease。`plugin_statistic_runs`是独立non-effect ledger，不得被effect dispatcher扫描。

### 3.3 引用链

数据库必须能从：

- Event→Evidence→Proposal→Decision→Intent→Attempt→readback/result；
- target→assignment→actor/application/P4Info observation→fleet child；
- firewall revision→compiled/readback reference→current/previous→rule epoch；
- model revision→qualification→pool generation→per-shard binding→worker readback；
- plugin revision→qualification→binding→statistics definition/run/artifact/current；

追踪到exact digest、actor、scope和time。删除/retention不能留下指向不存在事实的current或审计断链。

## 4. Event、Observation 与分区

Event按event time/ingest policy分区，分区键和唯一幂等约束需共同支持canonical Event replay。Rule sample/rollup和plugin statistics history按独立retention/profile分区；不能跨generation/reset epoch聚合。

Per-rule高基数事实保留在PostgreSQL，不导出为Prometheus label。Current/list/trend查询必须有覆盖索引、server cursor和bounded window，不依赖N+1查询或全表JSON scan。

Retention job只删除profile允许且不受legal hold/引用保护的历史；current、operation、audit和artifact引用先按合同处理。Retention不是业务scheduler，不能创建effect/plugin run。

## 5. Migration 设计

Migration由独立job/binary执行，应用实例不自动争抢。规则如下：

- 已应用migration不可修改，文件有版本、checksum和source digest；
- 生产演进采用expand/contract，先兼容reader/schema，再新write，最后停止旧读取；
- migration定义lock/statement/total deadline和失败语义；
- partial/interrupted/retry/checksum drift可检测，不把未知状态当成功；
- migration期间不调用P4、Edge、Central、Host、插件或外部HTTP；
- destructive test数据库名称必须包含`test`且精确确认。

每个逻辑域维护empty→current、上一受支持版本→current、current/previous reader-writer和rollback/cutoff矩阵。无法让旧binary安全读取current facts时系统进入read-only/HOLD，不丢字段恢复写入。

## 6. 角色与权限

至少分离：

- migration owner：DDL，仅one-shot job；
- Go application writer：所需DML，不能修改migration/history；
- Go read/query或API pool role（如实施）：最小视图/函数权限；
- Analysis private role：仅自有schema，不可访问core/model/plugin/target/effect；
- backup/replication/monitoring roles：按用途最小权限；
- deny-role fixtures：Edge、Central、Host、Web、Offline ML、deployment adapter、通用插件无核心DB credential。

PgBouncer transaction pooling只服务无session state的短transaction；migration、LISTEN和需要session语义的连接使用direct/专用pool。连接数按role/module/profile预算，pool exhaustion有界退化。

## 7. HA、Backup、PITR 与 Incarnation

可选托管HA或自建Patroni/等价方案；自建backup/WAL/PITR采用pgBackRest或资格等价工具。组件存在不构成PASS，必须实际验证failover、WAL archive、backup校验、隔离restore、RPO/RTO、DCS/storage/network故障和退出。

无损failover保留target/model control incarnation。PITR/restore/clone/rewind在开放Go writer、assignment、rollout和canonical ingest前：

- 隔离恢复实例和网络/credential；
- 轮换从未使用的新target-control和model-control incarnation；
- 使旧assignment/envelope/action/readback/handshake及同数字generation失效；
- 由Go创建新recovery operation，Edge/Central逐target/shard readback并CAS；
- statistics current在binding/source重验前stale，不自动补跑schedule。

Restore工具不能自行连接P4、打开writer或把backup中的current当作外部设备现状。

## 8. 可观测性与资源

数据库暴露低基数连接、transaction、lock、WAL、checkpoint、replication lag、partition/retention、query class和storage指标；业务audit仍在表中，trace/metrics不是事实源。

需要冻结：connections/pool、transaction/statement/lock timeout、row/body/page上限、partition数量/大小、WAL/backup/archive、disk/inode、autovacuum、retention batch和restore资源。慢查询、锁竞争、deadlock、pool/full disk不得无界拖住Go核心或改变P4事实。

## 9. 独立资格边界

PostgreSQL State是非普通常驻应用但独立qualification target。它以exact PostgreSQL image/config、migration job、schema fixture、backup/WAL和HA/restore topology真实启动/执行；Go业务逻辑可以由contract-level DB fixture/queries驱动，但数据库constraint、migration、failover/restore不能fake。

黑盒观察通过schema catalog、SQL/public migration boundary、backup/restore artifact和canonical引用链进行，不导入Go repository内部实现作为oracle。

## 10. 模块黑盒 E2E 验收范围

必须覆盖：

- empty/previous→current migration、重复/中断/checksum drift、expand/contract；
- Event/effect/firewall/rule/target/fleet/model/plugin/statistics identity、FK、unique、CAS与retention；
- concurrent claim/approve/rollout/gate/current冲突；
- partition、cursor/index、connection/pool/lock/deadlock/timeout/disk压力；
- failover、backup、WAL archive、PITR/clone/rewind、incarnation轮换与隔离restore；
- role/credential deny棋盘、TLS、审计和无外部mutation；
- RPO/RTO、迁移/查询/retention/restore绝对性能及3,600 秒 soak。

## 11. Module Complete 判定

Schema文件存在、migration能跑一次、单机PostgreSQL ready或Go integration通过都不足。State模块必须完成全部适用migration/constraint/role/HA-PITR/restore/fault/performance/compatibility证据，提供可复制migration/backup/restore验证命令，并证明恢复不会自动打开P4/model/plugin副作用，才能取得Module Complete。只有`production-ha`且全部HA门禁通过才可申请production qualification。
