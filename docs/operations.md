# 本地运行与维护

## 启动与停止

```bash
export BAEPILOT_API_TOKEN="<你自行生成的高熵 token>"
.venv/bin/baepilot --config config.example.toml init-db
.venv/bin/baepilot --config config.example.toml serve
```

不要真的使用上面的说明性占位字符串。初始化命令不需要管理 token 或真实模型凭据；只有 HTTP 服务要求 token。端口默认 8765，可通过 `serve --port 8766` 更改，host 只能是 loopback。使用 Ctrl-C 正常退出；本项目不安装系统服务。

**只允许一个实例和一个 Uvicorn worker。** 同数据库第二实例会报 `database_in_use`，`status` 离线命令也需要获得独占实例锁。服务运行时用 HTTP `/api/v1/status`，不要同时开 CLI 仓储。不要通过删除锁文件解决冲突：锁跟随打开的文件句柄，删文件反而可能破坏互斥。

## 停止开关与人工接管

- 停止全部自动回复：使用全局 runtime-controls 设 stopped。
- 只暂停某账号：提供 channel、account_id 的账号级控制。
- 单会话临时暂停/本人接管：修改会话 mode=paused/handoff。
- 识别到本人手动发言自动进入 handoff；不支持的群聊/非文本事件也转交本人。

所有修改都带最新 expected_revision；409 时重新读取状态再决定，不循环强行覆盖。控制生效后不再认领新的发送，pending 草稿被取消；已经进入外部网络的调用无法保证撤回，响应的 in_flight 用于提示。在途结果仍必须被记录。

恢复全部相关控制层为 auto；单会话还必须在配置白名单中。恢复**不会补发旧积压**。等待新的入站消息，不要复用旧 event_id 期待它重新执行。

## 看哪里判断成功

1. `POST /mock/events` 返回 202：只是持久化接收。
2. 事件 status=handled：生成阶段处理完，不是发送回执。
3. `C/runs`：查看最新角色 trace、调用次数和错误代码。
4. `C/outbox`：查看 pending/sending/sent/failed/unknown 等状态。
5. Mock 下 sent 来自独立模拟收件回执，不是模型自己声称成功。

`GET /healthz` 只说明应用进程可响应。微信 Connector 的 available=false 是正常未实现状态，不是已在后台尝试接入。

## unknown 的处理

发送超时、异常，或进程在 sending 状态退出，无法判断外部是否已经接收，保守记为 unknown。本会话后续自动发送被阻塞，**不要直接重发**。

先 POST `/api/v1/outbox/{send_id}/reconciliations`，body 为 `{}`，请求 Connector 查询。Mock 只能确认已有回执；缺少回执并不能证明未投递，继续 unknown。实际渠道不支持查询时需要人工在渠道中核实。

仅有明确证据时可由管理者提交 confirmed_status=sent 或 failed。核对写入审计，成功才创建已发送消息并安排记忆；不重新发同一 send_id。核对之后也不补发未知期间消息，等待新消息。无法核实时保持 unknown，必要时保持 handoff。

外部渠道没有天然 exactly-once 保证，sent 也只表示渠道接收，不代表对方已读。

## 任务与恢复

事件、任务、运行和 Outbox 均在 SQLite 中。重启时：

- pending 工作保留；安全的 running 生成/记忆任务重新排队，已有更新任务时以更新任务为准。
- running 运行记录标 interrupted，允许重新生成，但不是 LangGraph 节点级 checkpoint 恢复。
- sending 发送标 unknown，绝不自动当成 failed 重发。
- 草稿过期或 revision 不一致时不能发送。

模型错误采用有限指数退避；达到 max_job_attempts 后，仍属于当前 revision 的前台失败转 handoff。新消息到来可能创建新任务，这不是全会话累计费用硬上限。后台记忆失败不撤销成功发送、不接管前台。进程退出期间如遇硬终止，下次启动按持久状态恢复。

## 数据、隐私与备份

数据库保存聊天正文、模型输出、模拟回执和带证据记忆，均可能是敏感内容。默认目录权限尽量为 0700、数据库和锁文件为 0600；已有文件权限、宿主磁盘和备份安全仍由运行者负责。SQLite **没有本项目层面的静态加密**。

当前面向合成数据开发，没有自动保留期、逐会话删除/导出、审计脱敏导出或备份轮换功能。**不要在完善授权、保留/删除及备份策略前接入真实私人聊天。** 不是生产隐私合规方案。

备份建议：先全局停止、查看在途与 unknown、正常停止服务；确认进程完全退出后，使用 SQLite backup API 或受控复制整组数据库文件。WAL 模式下不要在运行时只复制 `.sqlite` 忽略 `-wal`。备份可能包含全部聊天，应限制权限、加密、设定过期和销毁策略。

清理开发数据时先停止实例并备份；核对配置解析出的**实际数据库绝对路径**和自己的所有权，再手动处理该文件及其 `-wal`、`-shm` 和闲置锁文件。本项目不提供通配符删除或自动清理任意配置路径的命令。删除数据库是不可逆的历史丢失；不要把恢复模式与删库重置混为一谈。

`demo` 只清理它自己创建的临时目录，不读取或清理配置中的正式数据库。

## 日志与常见问题

默认不打印正文、完整模型输入或 token；日志主要为任务/请求标识和错误代码。CLI 关闭 HTTP access log，避免 URL 中的会话标识进入常规访问日志。数据库仍保留完整业务数据，不等于数据未持久化。

| 现象 | 检查 |
|---|---|
| api_token_required | 设置高熵 BAEPILOT_API_TOKEN，拒绝示例占位值 |
| 401 / 429 | token 是否一致；是否超过单实例尝试上限 |
| 409 revision_conflict | 重新 GET 会话或 controls 后再操作 |
| queue_full | 排查慢任务/模型，等待或暂停输入；不要盲增上限 |
| 没有自动回复 | 精确白名单、所有 mode、unknown、静默窗口、未来发送间隔 |
| pending 长时间不发 | 是否到 not_before、是否已过 TTL、Worker 是否运行 |
| model_* / invalid_model_output | 看运行错误代码；不能用 Fake 成功掩盖真实服务失败 |
| connector_not_implemented | 微信边界未实现；本次不实践接入 |
| database_in_use | 使用已运行实例的 API 或先正常停止原实例 |

日志中的异常类名只用于定位；不要为了排障把真实正文、密钥或整个数据库发到外部 issue。
