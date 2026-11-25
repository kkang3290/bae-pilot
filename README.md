# BaePilot

**全自动情感 Talk Agent 的本地优先基础框架。**

Less guessing. More understanding. 少一点瞎猜，多一点理解。

本仓库将情感理解、沟通策略、表达、审核和记忆分工，通过 LangGraph 编排；正常情况下向已授权会话自动回复，不需要逐条审批。

> **当前运行边界：Mock 渠道 + Fake 模型 + PostgreSQL。个人微信仅预留接口，尚未接入。**
> 合成流程测试不是情感效果评测，也不是微信兼容性验证。不要把真实私人聊天放入演示或测试。

## 快速开始

Python 3.11+，**推荐 Python 3.14**（当前在 macOS arm64 / 3.14.10 验收），需要可连接的 **PostgreSQL 16**。3.15 在本环境缺少部分二进制依赖，其他 Python/平台和 PostgreSQL 大版本组合尚未实机验证。初次安装需要网络；默认 Fake/Mock 不需要模型 API Key，不访问真实模型或消息平台，但运行时必须连接 PostgreSQL。

先安装 **uv 0.13.9**（见[官方安装指南](https://docs.astral.sh/uv/getting-started/installation/)，项目固定工具版本）。`.python-version` 选择已验收的 Python 3.14.10；uv 创建并管理项目 `.venv`，缺少对应 Python 时可下载项目所需解释器，不替换系统 Python。

```bash
uv sync --locked --all-extras
```

有 Docker Compose 时，可启动**仅供合成开发/测试**的数据库：

```bash
export BAEPILOT_POSTGRES_PASSWORD="$(uv run --locked python -c 'import secrets; print(secrets.token_hex(24))')"
# 5432 已被占用时，先 export BAEPILOT_POSTGRES_PORT=55432
export BAEPILOT_POSTGRES_PORT="${BAEPILOT_POSTGRES_PORT:-5432}"
docker compose up -d --wait postgres
export BAEPILOT_DATABASE_URL="postgresql://baepilot:${BAEPILOT_POSTGRES_PASSWORD}@127.0.0.1:${BAEPILOT_POSTGRES_PORT}/baepilot"
uv run --locked baepilot demo
```

也可以不使用 Docker，直接设置指向现有专用开发数据库的 `BAEPILOT_DATABASE_URL`。数据库与角色需预先存在，并允许在该开发库内创建 schema。Compose 只绑定 loopback，使用持久卷；正常停止用 `docker compose stop postgres`。不要将示例的开发数据库超级用户用于生产，也不要随意删除卷；已有卷的密码不会随环境变量重新生成而自动改变。

`pyproject.toml` 声明依赖，`uv.lock` 是唯一依赖锁文件；开发工具位于 `dev` dependency group，Anthropic SDK 保持可选。上述 `--all-extras` 安装完整验收环境，但不会启用真实模型。日常使用 `uv add` / `uv remove` 修改依赖、`uv run --locked` 运行命令，不手改锁文件或混用其他安装器。依赖与构建策略见 [开发文档](docs/development.md)。不需要 Redis。

`demo` 使用新建的临时 PostgreSQL schema 和合成消息，检查自动回复、复杂路径、事件去重、人工接管和未知投递核对；退出前核对所有权标记并清理自己的 schema，不读写正式业务 schema。它只读取数据库 URL，不继承外部模型配置。数据库不可用时明确失败，不使用替代存储。

## 启动本地 API

保持上述数据库运行，并在当前终端保留数据库 URL：

```bash
export BAEPILOT_API_TOKEN="$(uv run --locked python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run --locked baepilot --config config.example.toml init-db
uv run --locked baepilot --config config.example.toml serve
```

默认监听 `127.0.0.1:8765`，只支持单进程、单 Uvicorn worker。默认业务 schema 为 `baepilot`，可用 `BAEPILOT_DATABASE_SCHEMA` 覆盖。除 `/healthz` 外，管理和数据接口需要 Bearer token。`.env.example` 只是模板，应用不会自动加载 `.env`；Docker Compose 自身有独立的 `.env` 加载规则。

另一个终端在设置相同 token 后提交消息：

```bash
curl -sS -X POST http://127.0.0.1:8765/api/v1/mock/events \
  -H "Authorization: Bearer $BAEPILOT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @examples/message.json

curl -sS \
  -H "Authorization: Bearer $BAEPILOT_API_TOKEN" \
  http://127.0.0.1:8765/api/v1/channels/mock/accounts/demo-account/conversations/demo-chat/outbox
```

接收返回 `202` 只表示已入库；沿返回的 `Location` 查询处理状态。连续消息默认等待约 0.5 秒静默窗口再处理，最多合并约 2 秒。重复提交相同事件不会再次回复；需要新测试消息时更换 `event_id`。

## 框架包含什么

- 编译执行的 LangGraph：普通短路径、复杂专家路径、短路径升级、一次审核重写上限。
- 五种逻辑角色：Understanding、Strategy、Talk、Review、异步 Memory。
- PostgreSQL 事件、会话修订号、持久任务、运行记录、Outbox、带来源的记忆、审计和 Mock 回执。
- 原生 JSONB、BIGINT identity/来源数组、TIMESTAMPTZ、短事务和会话级实例锁。
- 精确会话白名单、自动回复、暂停、停止、本人接管、自身回声过滤。
- 连续消息合并、过期草稿失效、同会话串行处理、有限后台重试。
- 未知投递阻塞与核对，不把超时当成可直接重发的失败。
- Mock Connector、明确不可用的微信边界、Fake Provider、显式启用的 Anthropic Provider。
- 本地管理 API、CLI、合成演示和真实 PostgreSQL 行为测试。

记忆目前是“最近消息提炼的结构化快照”，不是知识图谱或检索式长期记忆。LangGraph 的执行图不等于图数据库。选型、更新语义和后端迁移边界见 [数据库与记忆](docs/database.md)。

## 真实模型是显式选项

默认 `provider=fake`。仅存在 `ANTHROPIC_API_KEY` 不会启用模型调用。配置 `BAEPILOT_PROVIDER=anthropic` 后才会通过官方 SDK 调用；默认模型是 `claude-opus-5-5`，不是 Claude Agent SDK。

启用后会向模型服务发送必要聊天上下文，并产生费用。服务端 refusal fallback 默认启用，可在配置中关闭，可能使用回退模型并产生相应费用。测试不做真实付费调用；不能将 SDK 离线契约测试描述为真实模型已联调。

详见 [配置](docs/configuration.md)。

## 验证

只将测试 URL 指向专用合成开发/测试库，不要使用生产库。每个数据库测试创建并清理独立 schema；未设置测试 URL 时相关测试明确失败，不静默跳过或使用替代存储。

```bash
export BAEPILOT_TEST_DATABASE_URL="$BAEPILOT_DATABASE_URL"
uv lock --check
uv pip check
uv run --locked --all-extras pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv audit --locked
uv run --locked baepilot demo
```

## 文档

| 文档 | 内容 |
|---|---|
| [架构](docs/architecture.md) | 分层、状态机、持久化与恢复 |
| [数据库与记忆](docs/database.md) | PostgreSQL、事务/锁、图数据库区别、记忆限制 |
| [Agent](docs/agents.md) | 角色、输出、路由与记忆 |
| [Connector](docs/connectors.md) | 渠道协议、Mock 与未来微信接入 |
| [配置](docs/configuration.md) | 白名单、身份、预算和秘密 |
| [API](docs/api.md) | 认证、事件、查询和控制示例 |
| [开发](docs/development.md) | 环境、TDD、扩展方式与验收 |
| [运行维护](docs/operations.md) | 接管、未知投递、备份与数据清理 |
| [后续路线](docs/roadmap.md) | 未实现能力与生产化工作 |

## 当前限制

单进程、PostgreSQL、文本一对一、响应式对话；不实现群聊、多媒体、主动发送、跨平台自动合并联系人、真实微信或分布式部署。PostgreSQL 不等于已启用静态加密；远程数据库需另行配置 TLS 和最小权限。外部消息不承诺 exactly-once，发送成功也不等于对方已读。
