# PostgreSQL 存储与记忆选型

## 唯一数据库

BaePilot 仅使用 PostgreSQL，驱动为 Psycopg 3 的异步连接；数据库不可用时明确失败，不使用内存替代或模拟降级。当前实际验证 PostgreSQL 16，其余大版本尚未实机验收。

同一个数据库的专用 schema 保存：

| 表 | 职责 |
|---|---|
| `conversations`、`controls` | 完整会话身份、模式、修订号、记忆版本和全局/账号控制 |
| `events` | 去重后的消息与已确认发送事件 |
| `jobs` | process / memory 持久任务、到期时间、重试和状态 |
| `runs` | 图执行结果及显式递增的 `run_seq` |
| `outbox` | 发送意图、明确的 bigint `cursor`、投递状态和时间 |
| `memories` | 当前结构化记忆快照、来源 ID、源版本 |
| `audit_log`、`mock_receipts` | 审计与独立 Mock 收件回执 |
| `schema_version` | 存储布局版本，当前为 1 |

消息 payload、角色结果、任务 payload 和审计 details 使用 `JSONB`；来源 ID 为 `BIGINT[]`；数据库时间使用 `TIMESTAMPTZ`。仓储在边界转换到现有应用/API 的 Unix 秒数，因此限流、TTL 和可控测试时钟保持原有语义。Identity 序号允许因回滚出现空洞，不应把它当连续计数。

## 为什么不是图数据库？

这是三个不同的问题，不应混为一谈：

| 概念 | 解决的问题 | 当前实现 |
|---|---|---|
| LangGraph 执行图 | 先调用哪个角色、是否重写或接管 | 已实现，不要求图数据库 |
| 知识图谱/图数据库 | 谁与谁有什么关系、某事件涉及哪些实体、多跳关联 | 未实现；不是使用 Agent 记忆的必需条件 |
| 向量检索 | 找到与当前话题语义相关的旧消息或记忆 | 未实现；可后续考虑 pgvector 等方式 |

关系库按表、键、约束和 JOIN 组织数据，擅长事务性状态修改；图数据库按节点、边和属性组织数据，擅长可变深度路径与多跳关系查询。图数据库也可提供事务，但“消息入库 + 任务排队”“运行结果 + Outbox”“确认成功 + 记忆任务”这些现有边界用关系模型可以直接表达。

例如“对方说不喜欢被催促”现在是一条有来源的 statement；知识图谱则可能建模为“人物—表达偏好—不被催促”，并保留时间与证据。后者需要新增实体识别、关系提取、冲突和失效规则，不是换个数据库就能自动得到。

PostgreSQL 也能用实体表、关系表和递归查询表达图。只有实际出现大量复杂、多跳的图查询需求，才需要评估独立图数据库；不必为了“记忆”这个名字引入第二套基础设施。

## 记忆算法没有因换库而升级

仍然是：明确发送成功后创建 memory 任务 → 按源版本读取同会话最近消息（默认 40 条，去 echo）→ Memory 角色提取最多 20 项 statement / summary / inference → 校验窗口证据和数据库归属 → 版本检查后整批替换当前记忆。

Memory 输入不含旧记忆，因此不是长期事实增量合并。早期信息会随窗口滚动从记忆快照中消失，即使原始消息还在 events。来源有效也不证明模型解释一定正确。当前 Fake 只验证流程。长期事实合并、纠正、遗忘和按话题检索需要单独设计与评估。

## 事务与实例约束

每个 Store 使用一条专用异步连接；短事务由 `asyncio.Lock` 串行化，事务内不等待模型或 Connector。任务认领采用带 status 条件的 UPDATE ... RETURNING，唯一/部分唯一索引约束事件去重和同会话 pending 任务。

全局/账号控制按选中的会话 scope 数组批量推进修订号和取消待处理内容，不逐会话往返数据库。记忆证据用参数化数组 `ANY` 查询；通过来源与版本校验后，在同一事务中删除旧快照并用 Psycopg `executemany` 写入新快照。空记忆列表仍会清空快照并推进版本，旧版本不能恢复已清空的内容。

连接为每个 `(database, schema)` 持有 session advisory lock。第二个实例会得到 `database_in_use`，关闭/断开连接后 PostgreSQL 自动释放锁。连接丢失后不会自动重连并继续发送，需排障并重启；发送中遗留状态仍按 unknown 恢复。已经进入外部调用的发送无法保证撤回。

**仍只支持一个应用进程、一个 Uvicorn worker。** 换库不代表已经完成分布式调度。不要使用 PgBouncer transaction pooling：它不保持本项目依赖的 session advisory lock 和 search_path 会话状态；使用直连或保持会话的连接方式。未来多实例需要事务级协调、任务租约、配置一致性与共享限流，不能只删除实例锁。

## 配置与初始化

- `BAEPILOT_DATABASE_URL`：含明确数据库名的 `postgresql://...` 或 `postgres://...` URL；在配置对象中作为 SecretStr 隐藏。密码特殊字符需要 URL 编码。
- `BAEPILOT_DATABASE_SCHEMA`：默认 `baepilot`；限定小写安全标识，拒绝 public、information_schema、pg_*。只访问该专用 schema。
- `init-db` 在已存在的数据库内创建 schema 和表；**不会创建数据库或角色**。空 schema 首次初始化；不兼容版本或没有版本信息的非空 schema 拒绝打开，不覆盖既有表。
- URL 可通过 TOML 的 `database_url` 设置，但真实密码应放环境变量。不要在日志、issue 或聊天中粘贴连接串。

本地合成开发启动见 README 的 Docker Compose 示例。Compose 只绑定 loopback，数据库卷持久化；修改环境密码不会自动修改已有卷内的角色密码。正常用 `docker compose stop postgres` 停止，不随意删除 volume。

生产环境不要使用 Compose 示例的数据库超级用户：预建专用数据库/schema，初始化后使用只具备所需表、序列和 schema 权限的运行角色；schema 升级使用单独授权。远程数据库应配置 TLS（如 `sslmode=verify-full` 及 CA）、网络限制和凭据轮换。PostgreSQL 本身不等于已经启用静态加密或完成隐私合规。

## 测试和演示隔离

测试必须显式设置 `BAEPILOT_TEST_DATABASE_URL`，不回退连接正式库，也不因缺数据库而静默跳过。每个数据库测试创建独立的 `baepilot_test_<uuid>` schema，清理前读取所有权标记；只清理自身创建的 schema。重启测试重开同一测试 schema 验证持久状态。纯领域和 Provider 测试不依赖数据库连接。

`demo` 只读取 `BAEPILOT_DATABASE_URL`（未设置则使用内置本地 URL），不加载 TOML 或模型环境选择。每次新建 `baepilot_demo_<uuid>` schema 并标记所有权，固定 Fake/Mock；正常或异常退出后关闭 Runtime，再核对标记和 owner 后清理自己创建的 schema，不使用 `BAEPILOT_DATABASE_SCHEMA` 指向的业务 schema。硬终止可能留下临时 schema，需要管理员核对后清理。

两者均需要数据库 CREATE schema 权限，**只应指向专用合成开发/测试数据库**。虽然不访问真实模型和消息平台，但会连接 PostgreSQL，因此不再声称是完全零网络的离线演示。
