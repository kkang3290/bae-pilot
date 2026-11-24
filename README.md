# BaePilot

**全自动情感 Talk Agent 的本地优先基础框架。**

Less guessing. More understanding. 少一点瞎猜，多一点理解。

本仓库将情感理解、沟通策略、表达、审核和记忆分工，通过 LangGraph 编排；正常情况下向已授权会话自动回复，不需要逐条审批。

> **当前运行边界：Mock 渠道 + Fake 模型可离线执行。个人微信仅预留接口，尚未接入。**
> 离线流程测试不是情感效果评测，也不是微信兼容性验证。不要把真实私人聊天放入演示或测试。

## 快速开始

Python 3.11+，**推荐 Python 3.14**（当前在 macOS arm64 / 3.14.10 验收）。3.15 在本环境缺少部分二进制依赖，其他 Python/平台组合尚未实机验证。初次安装需要网络；依赖安装完成后，默认演示不需要 API Key 或外部服务。

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/baepilot demo
```

锁文件包含开发与可选模型适配测试依赖，便于复现本仓库验收环境；安装 SDK 不会自动启用真实调用。依赖声明与安装策略见 [开发文档](docs/development.md)。只在项目虚拟环境操作，不需要全局安装 uv、Redis 或数据库服务。

`demo` 使用独立临时数据库和合成消息，检查自动回复、复杂路径、事件去重、人工接管和未知投递核对；退出后清理自己创建的临时数据，不覆盖正式数据库。

## 启动本地 API

```bash
export BAEPILOT_API_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/baepilot --config config.example.toml serve
```

默认监听 `127.0.0.1:8765`，只支持单进程。除 `/healthz` 外，管理和数据接口需要 Bearer token。`.env.example` 只是模板，程序不会自动加载 `.env`。

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
- SQLite 事件、会话修订号、持久任务、运行记录、Outbox、带来源的记忆。
- 精确会话白名单、自动回复、暂停、停止、本人接管、自身回声过滤。
- 连续消息合并、过期草稿失效、同会话串行处理、有限后台重试。
- 未知投递阻塞与核对，不把超时当成可直接重发的失败。
- Mock Connector、明确不可用的微信边界、Fake Provider、显式启用的 Anthropic Provider。
- 本地管理 API、CLI、离线演示和行为测试。

## 真实模型是显式选项

默认 `provider=fake`。仅存在 `ANTHROPIC_API_KEY` 不会启用模型调用。配置 `BAEPILOT_PROVIDER=anthropic` 后才会通过官方 SDK 调用；默认模型是 `claude-opus-5-5`，不是 Claude Agent SDK。

启用后会向模型服务发送必要聊天上下文，并产生费用。服务端 refusal fallback 默认启用，可在配置中关闭，可能使用回退模型并产生相应费用。测试不做真实付费调用；不能将 SDK 离线契约测试描述为真实模型已联调。

详见 [配置](docs/configuration.md)。

## 验证

```bash
.venv/bin/python -m pip check
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/baepilot demo
```

## 文档

| 文档 | 内容 |
|---|---|
| [架构](docs/architecture.md) | 分层、状态机、持久化与恢复 |
| [Agent](docs/agents.md) | 角色、输出、路由与记忆 |
| [Connector](docs/connectors.md) | 渠道协议、Mock 与未来微信接入 |
| [配置](docs/configuration.md) | 白名单、身份、预算和秘密 |
| [API](docs/api.md) | 认证、事件、查询和控制示例 |
| [开发](docs/development.md) | 环境、TDD、扩展方式与验收 |
| [运行维护](docs/operations.md) | 接管、未知投递、备份与数据清理 |
| [后续路线](docs/roadmap.md) | 未实现能力与生产化工作 |

## 当前限制

单进程、本地 SQLite、文本一对一、响应式对话；不实现群聊、多媒体、主动发送、跨平台自动合并联系人、真实微信或分布式部署。SQLite 未提供本项目层面的静态加密。外部消息不承诺 exactly-once，发送成功也不等于对方已读。
