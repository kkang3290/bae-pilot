# 开发与验证

## 环境与安装

项目声明 Python ≥3.11，当前实际验收环境为 **macOS arm64 + Python 3.14.10 + PostgreSQL 16**。推荐 3.14；尚未对所有声明支持版本和平台运行测试。3.15 在本环境缺少 zstandard 所需二进制 wheel，因此没有改成从来源不明脚本编译，也没有替换系统 Python。

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
# 先按 README 启动 PostgreSQL，并设置 BAEPILOT_DATABASE_URL
.venv/bin/baepilot demo
```

已有项目环境时无需重建。首次安装依赖需要网络；演示只访问 PostgreSQL，不访问真实模型/平台，但不再是零网络运行。Windows 使用 `.venv\Scripts\python` 等对应入口；该平台分支未实机验收。

### 依赖策略

- `pyproject.toml` 声明范围和可选依赖，是源配置。
- `requirements.lock` 是唯一安装锁文件，含 dev、可选 Anthropic SDK 和本项目 editable 构建用 setuptools，精确版本和分发哈希；不含本机源码绝对路径。
- 第三方依赖只安装 wheel，`--only-binary=:all:` 拒绝隐式源码构建。哈希检查适用于重新安装，不会重新验证已经满足的包；怀疑环境污染时创建新虚拟环境复验。
- 本项目使用已锁定 setuptools 在本地执行 editable 构建，`--no-build-isolation --no-deps` 避免额外隐式解析。不要将此命令用于未经审阅的第三方源码。
- 生成器为 uv 0.13.9，升级锁文件需显式审查新增包、版本、来源和测试结果。命令示例（使用你在隔离工具环境安装的同版本 uv）：

```bash
uv pip compile pyproject.toml --all-extras --python .venv/bin/python \
  --universal --generate-hashes --only-binary=:all: --output-file requirements.lock
```

`pip check` 只验证依赖一致性，**不是已知漏洞审计或供应链安全证明**。pip 没有内置漏洞审计子命令；发布前还需引入经审阅的漏洞审计工具及 CI 策略，记录不可修复项的可达性与复查时间。本次没有声称完成生产发布安全审计。

## 开发循环

遵循 TDD：写行为测试 → 运行并看到预期失败 → 最小实现 → 完整回归 → 重构。不要只检查某个角色被调用，而忽略真实 PostgreSQL、Outbox 和回执；不要在测试中复制生产算法计算预期值。

```bash
# 仅限专用合成开发/测试库；不要指向生产库
export BAEPILOT_TEST_DATABASE_URL="$BAEPILOT_DATABASE_URL"
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pip check
# 先按 README 启动 PostgreSQL，并设置 BAEPILOT_DATABASE_URL
.venv/bin/baepilot demo
```

当前 LangSmith 间接依赖在 Python 3.14 导入时会发出 `asyncio.iscoroutinefunction` 的 DeprecationWarning（指向未来 3.16），不是本项目测试失败。暂未修改第三方包或全局屏蔽警告；升级依赖后复查。pytest 禁用 LangSmith 自动插件，图运行也显式禁用 tracing。

## 模块导航

```text
src/bae_pilot/
  cli.py, demo.py             # 本地入口、独立临时演示
  app.py, config.py           # API 边界、显式配置
  domain.py                  # 不含框架副作用的统一契约
  agents/                    # 角色提示词、结构化输出、实际 StateGraph
  models/                    # Fake 与可选官方 SDK 适配
  connectors/                # 渠道协议、Mock、未实现微信
  services/                  # 入站、调度、唯一发送服务、异步记忆
  storage/                   # SQL schema、短事务仓储
```

当前没有 Alembic 和版本升级迁移链。PostgreSQL schema_version=1；首次空 schema 原子初始化，未知版本或无版本信息的非空 schema 拒绝打开。修改布局时需设计升级迁移和兼容性，不要只改版本号绕过检查。

## 测试层次

- 领域/配置：输入约束、完整会话键和授权规则。
- 运行时集成：实际编译图、独立 PostgreSQL 测试 schema、真实持久队列和 Mock 回执。
- 可靠性：去重、旧结果失效、回声、人工接管、unknown、崩溃恢复、发送间隔、记忆证据和独立调度。
- API：实际 ASGI 应用、鉴权、错误脱敏、流式请求体上限、分页及控制冲突。
- Provider：使用**官方 SDK** 和 HTTP mock transport 返回完整合成 SSE；验证实际请求参数、拒绝、截断与解析，不调用真实模型。
- CLI：演示不继承真实 Provider 环境；临时 schema 所有权校验与清理、数据库初始化、启动边界和配置错误不回显 token/连接密码。

测试时只在平台或模型外部边界替换依赖；数据库始终为真实 PostgreSQL，不做 SQL mock、不使用替代存储。数据库测试显式要求 BAEPILOT_TEST_DATABASE_URL；未设置时明确失败，纯领域/Provider 测试不需要连接。每个数据库测试创建独立 baepilot_test_<uuid> schema，结束时核对标记并只清理自身 schema；测试 URL 必须指向可创建 schema 的专用合成开发/测试库。合成事件工厂和普通 Runtime 生命周期放在 `tests/helpers.py` 复用，重启/崩溃和受控并发测试保留自己的清理顺序。使用可控时钟及 asyncio.Event 构造竞态，不靠长时间 sleep。`Runtime.drain()` 只排空**当前到期**任务；未来到期任务需推进测试时钟，不是自动等待所有延迟任务。

## 扩展角色或模型

增加角色时先定义 Pydantic 契约和成功/失败测试，再添加独立提示词、图节点与有限边界；不能让模型直接发送或改变授权。新增 Provider 实现 `complete(role, context, schema)` 和 `close()`，保留本地校验和错误分类；未显式选用时不得创建外部客户端。切换 Provider 不修改会话身份、Outbox 和权限逻辑。

目前 Fake 只用于流程示例。换成真实模型之前，另做匿名合成对话质量评估，测事实归属、承诺、表达、接管和成本，不把单元测试数当作情感质量指标。

新增渠道见 [Connector 文档](connectors.md)，微信实机实践留待后续。调试数据必须使用合成内容，不能为了测试方便导入私人聊天。
