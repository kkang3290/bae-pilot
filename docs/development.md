# 开发与验证

## 环境与安装

项目声明 Python ≥3.11，当前实际验收环境为 **macOS arm64 + Python 3.14.10 + PostgreSQL 16 + uv 0.13.9**。`.python-version` 固定本地开发解释器为 3.14.10，不收窄包元数据中的兼容范围；尚未对所有声明支持版本和平台运行测试。3.15 在本环境缺少 zstandard 所需二进制 wheel，不通过隐式源码编译绕过，也不替换系统 Python。

先安装 uv 0.13.9，项目通过 `tool.uv.required-version` 拒绝其他版本。安装方式见 [uv 官方指南](https://docs.astral.sh/uv/getting-started/installation/)。如果不希望全局安装，可使用独立工具环境引导一次（这里的 pip 仅用于安装 uv，不管理项目依赖）：

```bash
python3 -m venv .bootstrap-venv
.bootstrap-venv/bin/python -m pip install --only-binary=:all: "uv==0.13.9"
export PATH="$PWD/.bootstrap-venv/bin:$PATH"
uv --version
```

然后在仓库根目录执行：

```bash
uv sync --locked --all-extras
# 先按 README 启动 PostgreSQL，并设置 BAEPILOT_DATABASE_URL
uv run --locked baepilot demo
```

uv 自动创建/同步 `.venv`，无需激活或手工 editable 安装；缺少对应解释器时可下载 uv 管理的 Python，不修改系统 Python。已有环境可继续同步，无需删除重建。首次解析/下载需要网络；演示只访问 PostgreSQL，不访问真实模型/平台。Windows 使用相同的 `uv sync` / `uv run` 入口，该平台尚未实机验收。

### 依赖策略

- `pyproject.toml` 声明运行依赖、可选 `anthropic` extra 和 `dev` dependency group。uv 默认安装 dev；完整测试还需 `--all-extras`，安装 SDK 不会启用真实调用。
- `uv.lock` 是唯一依赖锁文件，记录依赖图、精确版本、平台条件、来源和分发哈希；与 `pyproject.toml` 一起提交，不维护第二份安装锁文件。项目本身使用相对 editable 来源，不写入本机绝对路径。
- `uv sync --locked` 检查锁文件是否与声明一致，过期时失败；不要用 `--frozen` 掩盖声明与锁的不一致。`uv run --locked` 同样拒绝隐式改锁。`uv sync` 默认移除环境中未声明的包，不用其他安装器向 `.venv` 混装依赖。
- `tool.uv.no-build=true` 禁止第三方依赖的隐式源码构建；支持的预构建 wheel 不可用时失败。uv 仍允许已审阅的第一方项目/editable 构建，也可能复用此前源码构建的缓存 wheel，因此这不是任意现有缓存的来源证明。复验来源时使用全新的缓存和虚拟环境。
- 本项目保留 setuptools 构建后端，使用隔离构建。`tool.uv.build-constraint-dependencies` 将其固定为已验收的 84.2.1 并列出分发哈希；`require-build-hashes=true` 拒绝未提供哈希的构建依赖。setuptools 不再作为运行或开发环境依赖。构建哈希功能目前是 uv 的 preview，因此显式开启该功能并固定 uv 版本。
- `uv sync --locked --no-dev` 只安装基础运行环境；真实模型适配再加 `--extra anthropic`。运行时也应使用相同选择，例如 `uv run --locked --no-dev --extra anthropic baepilot --config config.example.toml serve`。不带 extra 的 Fake/Mock 不依赖可选模型 SDK；不带 dev 的环境不能运行测试和 Ruff。

日常依赖操作（占位符需替换为真实包名；不要原样复制）：

```bash
uv add <package>                 # 运行依赖
uv add --dev <package>           # 开发工具
uv add --optional anthropic <package>
uv remove <package>
uv remove --dev <package>
uv lock                         # 修改声明后更新锁文件，尽量保留既有版本
uv lock --upgrade-package <package>  # 显式升级某个依赖
uv sync --locked --all-extras
```

每次审阅新增包、版本、来源、平台分支和构建约束，运行完整回归。升级构建后端时同步核对并更新版本与哈希，不关闭构建哈希校验来绕过失败。uv 工具升级也需审阅后修改 `required-version` 并重新验收。

`uv lock --check` 检查声明与锁的一致性，`uv pip check` 只检查已安装依赖兼容性，二者都不是漏洞审计。`uv audit --locked` 使用 uv 原生审计命令，查询外部漏洞服务并发送包名/版本，不发送聊天或密钥；当前 uv 将此命令标为实验性。审计只覆盖服务已知通告，不能证明不存在未知漏洞或供应链问题；构建后端也需单独审阅。发布前复跑审计，按运行/构建/测试路径分析可达性与修复版本，记录延期理由和复查日期，不强制跨版本修复，也不将本次基础验证描述为生产安全认证。

## 开发循环

遵循 TDD：写行为测试 → 运行并看到预期失败 → 最小实现 → 完整回归 → 重构。不要只检查某个角色被调用，而忽略真实 PostgreSQL、Outbox 和回执；不要在测试中复制生产算法计算预期值。

```bash
uv sync --locked --all-extras
# 仅限专用合成开发/测试库；不要指向生产库
export BAEPILOT_TEST_DATABASE_URL="$BAEPILOT_DATABASE_URL"
uv lock --check
uv pip check
uv run --locked --all-extras pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv audit --locked
# 先按 README 启动 PostgreSQL，并设置 BAEPILOT_DATABASE_URL
uv run --locked baepilot demo
# 验证源码包和 wheel；输出到已忽略的 dist/
uv build
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
