# 配置

## 加载规则

入口为 `Settings` 和 `load_settings()`，示例见根目录 `config.example.toml`。优先级从高到低：

1. 支持的环境覆盖项。
2. CLI `--config PATH` 指定的 TOML；未指定时读取 `BAEPILOT_CONFIG` 指向的文件。
3. 内置默认值。

没有指定文件时不会自动发现当前目录的配置。所有相对路径相对于**启动时工作目录**，不是 TOML 所在目录。程序不自动加载 `.env`；不要直接 `source .env.example`，它只是说明模板。未知配置字段、重复账号、重复会话，以及引用未注册账号的规则均拒绝启动。

`baepilot demo` 是例外：只读取 `BAEPILOT_DATABASE_URL`（未设置时使用内置本地 URL），不读取 TOML、业务 schema 或模型环境选择；固定使用新建临时 PostgreSQL schema、Fake、Mock。

## 环境变量与秘密

| 环境变量 | 用途 |
|---|---|
| `BAEPILOT_CONFIG` | TOML 文件路径，CLI `--config` 优先 |
| `BAEPILOT_DATABASE_URL` | PostgreSQL URL，必须包含数据库名；凭据按秘密处理 |
| `BAEPILOT_DATABASE_SCHEMA` | 业务 schema，默认 baepilot |
| `BAEPILOT_TEST_DATABASE_URL` | pytest 专用合成数据库 URL，测试无默认回退 |
| `BAEPILOT_PROVIDER` | `fake` 或 `anthropic` |
| `BAEPILOT_API_TOKEN` | 本地 HTTP 管理 token；启动 HTTP 必须设置 |
| `ANTHROPIC_API_KEY` | 仅显式启用 Anthropic 时由官方 SDK 解析 |

管理 token 至少 16 个字符，拒绝模板中的占位值，建议用 `secrets.token_urlsafe(32)`。不要把真实 token 写进 TOML、提交到 Git、写进聊天或命令示例。HTTP 客户端和服务需使用相同 token。管理 token 拥有整个实例的读取及控制权限，**不是多用户权限系统**。

发现 API Key 不会隐式启用真实模型。`init-db` 和 `status` 直接连接 PostgreSQL 仓储，不创建模型客户端。

## 账号与精确会话白名单

```toml
[[accounts]]
channel = "mock"
account_id = "demo-account"
self_id = "self"

[[conversations]]
channel = "mock"
account_id = "demo-account"
conversation_id = "demo-chat"
mode = "auto"
style = "自然、简短、认真倾听；不虚构经历或承诺。"
```

- 账号标识与平台上的本人发送者标识 `self_id` 不一定相同。
- 会话身份由三元组 `(channel, account_id, conversation_id)` 精确匹配，不支持通配符。
- 已注册账号中的白名单外会话可以记录入站事件，但没有自动发送权限。
- `mode` 是会话**首次入库**时的初始模式。后续模式保存在数据库，重启不会被 TOML 重置；用管理 API 修改。
- 从配置移除规则会撤销下一次生成/发送认领权限，但无法撤回已经进入渠道调用的消息。改配置需要重启。
- 风格只是模型输入，不能扩大权限、选择收件人、越过程序限制。
- 标识符只允许 `A-Z a-z 0-9 _ . : @ -`，长度 1–128；接入真实平台时应建立稳定的标识映射，而非直接用任意显示名称。

### 模式

| 模式 | 行为 |
|---|---|
| `auto` | 白名单、全局/账号开关、投递状态均允许时自动回复 |
| `paused` | 暂停自动处理；保留消息 |
| `handoff` | 本人接管，或程序因不支持/审核失败转交本人 |
| `stopped` | 显式停止，需显式恢复 |

全局及账号控制只支持 `auto / paused / stopped`。所有控制层必须为 `auto` 才允许发送；会话改为 `auto` 不会绕过全局暂停。控制变化会作废待发草稿，恢复后**不追发积压消息**，等待新的入站事件。

## 运行限制

| 字段 | 默认 | 含义 |
|---|---:|---|
| `database_url` | `postgresql://localhost:5432/baepilot` | 唯一数据库，推荐通过环境提供凭据 |
| `database_schema` | `baepilot` | 专用 schema；小写字母/数字/下划线，不允许 public、information_schema、pg_* |
| `debounce_s` | 0.5 | 连续消息静默合并时间，0–30 秒 |
| `max_wait_s` | 2 | 最大合并时间，必须 ≥ 静默窗口，最多 60 秒 |
| `min_send_interval_s` | 1 | 本会话成功发送后的最小发送间隔，最多 3600 秒 |
| `reply_ttl_s` | 60 | 生成后回复有效期；认领前过期则取消 |
| `max_parallel` | 4 | 前台任务并发，1–32；另有最多一个后台记忆槽 |
| `max_pending_jobs` | 1000 | pending/running 持久任务容量，1–10000 |
| `max_job_attempts` | 3 | 可重试任务的有限尝试，1–5；context_too_large 不重试，SDK 网络重试另计 |
| `max_reply_chars` | 1200 | 回复字符上限，不能高于统一发送契约的 1200 |
| `context_message_limit` | 40 | 前台可选历史的最近窗口/后台 Memory 提取窗口，1–200；前台另加全部 queued 及最近 outbound |
| `max_context_chars` | 32000 | 序列化角色上下文的硬字符预算，1000–200000；不是 token 或完整 API 请求预算 |

前台请求含覆盖说明，在 90% 硬预算以内不折叠；超过才整条折叠最旧的可选历史，通常以 80% 为目标。全部 queued、最近 outbound、完整记忆、风格及必要角色结果/事实原文始终保护。必要骨架已超过 80% 时只要求满足硬限，不为不可达目标删光可选历史；必要输入超过硬限则 `context_too_large`，该任务只尝试一次，仍属当前 revision 的前台运行立即转 handoff。Memory 保留原提取窗口，超限终止任务而不清空旧快照、不接管前台。阈值是固定比例，不增加配置项，详见 [上下文管理](context.md)。

`context_message_limit` 不是本轮待处理消息数上限，更不是无限长历史；没有检索式长期记忆。字符测量使用 `len(json.dumps(context, ensure_ascii=False))`，包含折叠标记，不包含系统提示词、输出 Schema、完整 API 包装或输出 token；不要按字符/4 宣称满足模型窗口。事件文本上限 4000 字符，HTTP 请求体总上限 65536 字节。API 每实例 120 次/60 秒的简易尝试计数包括错误 token，`/healthz` 不计；并不是抗公网攻击方案。

## 模型

| 字段 | 默认 | 含义 |
|---|---|---|
| `provider` | `fake` | 必须显式选择真实服务 |
| `model` | `claude-opus-5-5` | 真实模型 ID，不影响 Fake |
| `model_timeout_s` | 30 | 单角色等待上限；首版渠道发送/查询也复用该超时 |
| `model_max_tokens` | 2000 | 每次模型请求输出 token 上限 |
| `model_max_calls` | 8 | 单轮前台图的调用预算，1–12；记忆另有单次调用 |
| `refusal_fallback` | `true` | 启用 SDK 服务端 refusal fallback；可关闭 |

真实适配使用官方 `anthropic` Python SDK、流式最终消息、adaptive thinking、medium effort 和 JSON Schema 输出。拒绝、截断或结构不合法不能当作有效回复。错误不会自动改用 Fake。

默认 fallback 请求带 `server-side-fallback-2026-07-01` beta 和 `fallbacks="default"`；可能由服务端回退至其他模型并按服务规则计费。可用性、模型权限和费用应在正式接入时核实。本次只做 SDK 离线传输契约测试，未调用付费模型。

启用真实模型前，应明确授权哪些聊天可被第三方处理。发送的是最近消息正文、方向、来源、类型、证据用本地记录编号、风格和相关记忆；账号/联系人标识、API token、内部 run_id 不作为模型上下文。正文自身仍可能含个人信息，最小化字段不等于匿名化。

程序显式关闭图执行的 LangSmith tracing，不提供自动外部遥测。不要自行打开 SDK 调试日志记录聊天。
