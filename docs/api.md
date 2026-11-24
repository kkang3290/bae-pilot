# 本地管理 API v1

默认 `http://127.0.0.1:8765`。CLI 拒绝公网监听地址，仅允许 `127.0.0.1` / `::1`，单 Uvicorn worker。HTTP 明文仅用于本机；不要直接暴露公网或以反向代理绕过边界。

## 认证与响应

除 `GET /healthz` 外，数据和控制接口需要：

```text
Authorization: Bearer <BAEPILOT_API_TOKEN>
```

成功通常为 `{"data": ...}`；健康检查和 OpenAPI schema 为原生结构。错误示例：

```json
{"error":{"code":"revision_conflict","message":"revision_conflict","request_id":"..."}}
```

响应带 `X-Request-ID`，默认不缓存。校验错误只返回字段位置与错误类型，不回显输入值。每实例每 60 秒最多 120 次 API 认证尝试，超过返回 `429` 和 `Retry-After: 60`。请求体超过 65536 字节返回 `413`，按实际流式字节数而不是只信 Content-Length。

| 状态码 | 常见情况 |
|---|---|
| 200 | 查询/控制成功，或完全重复的事件 |
| 202 | 新事件持久化成功；不代表已经发送 |
| 401 / 403 | 管理 token 不正确 / 未注册账号或未授权会话操作 |
| 404 | 事件、会话、投递不存在，或路由不存在 |
| 409 | 同事件 ID 不同内容、控制 revision 冲突、数据库被其他实例占用 |
| 413 / 422 | 请求过大 / 输入契约不合法、Mock 冒充其他渠道 |
| 429 | 本机 API 简易限流 |
| 501 | 微信未实现，或渠道不支持投递查询 |
| 503 | 持久任务队列已满等暂时不可用情况 |

OpenAPI：`GET /api/v1/openapi.json`，同样需要 token。没有公开 Swagger UI。

## 资源列表

下面用 `C` 代表：

```text
/api/v1/channels/{channel}/accounts/{account_id}/conversations/{conversation_id}
```

| 方法 | 路径 | 返回/用途 |
|---|---|---|
| GET | `/healthz` | 进程健康与 Provider 名称；不表示渠道实机连通 |
| GET | `/api/v1/connectors` | 能力与 available/reason |
| GET | `/api/v1/status` | 会话、控制、任务与投递状态计数 |
| POST | `/api/v1/mock/events` | 提交 Mock 绑定事件 |
| GET | `/api/v1/events/{record_id}` | 已入库事件及其处理状态 |
| GET | `C` | 会话 mode/revision、在途和未知投递状态 |
| PATCH | `C` | 修改会话模式，必须提供 expected_revision |
| GET | `C/messages` | 最近消息，游标向过去翻页 |
| GET | `C/outbox` | 生成后的投递状态，游标向过去翻页 |
| GET | `C/memories` | 当前记忆及 kind/证据/版本 |
| GET | `C/runs` | 首版仅返回最新运行或 null，不是全部历史列表 |
| PATCH | `/api/v1/runtime-controls` | 全局或账号控制 |
| POST | `/api/v1/outbox/{send_id}/reconciliations` | 核对 unknown；不重新发送 |

分页参数 `limit=1..100`（默认 50）、`before`（正整数游标）。每页内部按记录顺序正序，首次取最新页。响应 `meta.has_more` 与 `meta.next_cursor`；把 next_cursor 作为下一次 before，直到 has_more=false。消息使用 record_id，Outbox 使用 cursor，不能互换。

## 提交与查询事件

```bash
BASE=http://127.0.0.1:8765
C="$BASE/api/v1/channels/mock/accounts/demo-account/conversations/demo-chat"

curl -sS -i "$BASE/api/v1/mock/events" \
  -H "Authorization: Bearer $BAEPILOT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @examples/message.json
```

首次返回 `202`，带 `Location: /api/v1/events/<record_id>`：

```json
{"data":{"record_id":1,"status":"queued","duplicate":false}}
```

使用 Location 查询入站状态；使用 Outbox 确认是否真正得到发送回执。

```bash
curl -sS -H "Authorization: Bearer $BAEPILOT_API_TOKEN" "$C/outbox"
curl -sS -H "Authorization: Bearer $BAEPILOT_API_TOKEN" "$C/runs"
```

相同身份四元组 `(channel, account_id, conversation_id, event_id)` 的完全重复输入返回 `200` 与 duplicate=true；同键不同业务载荷返回 `409 event_id_conflict`。不要改文本后复用 event_id。可选 occurred_at 若提供，也属于去重载荷的一部分。

事件状态包括 queued、handled、echo、ignored_mode、ignored_control、handoff、unsupported。未排队不一定是故障，例如 paused 时正常记录但不回复。运行失败详情和投递结果要分别查看，不以事件入库代替成功发送。

## 暂停、接管和恢复

先 GET 会话，从响应取最新 revision，例如 2：

```bash
curl -sS -H "Authorization: Bearer $BAEPILOT_API_TOKEN" "$C"
curl -sS -X PATCH "$C" \
  -H "Authorization: Bearer $BAEPILOT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"paused","expected_revision":2}'
```

恢复时重新 GET，使用新 revision，mode 改为 auto。本人接管可设 handoff，明确关闭可设 stopped。不能用旧 revision 强行覆盖新消息造成的状态变化。只有配置白名单内会话能恢复 auto。

控制接口的 `in_flight` 表示已经认领、可能正在发送的数量；暂停取消尚未认领的待发，无法保证撤回在途调用。恢复不会追发暂停期间旧消息，提交**新 event_id** 才启动新一轮。

## 全局和账号开关

先 GET `/api/v1/status` 获取 controls。全局 scope 为 `*`，首次 revision 为 0：

```bash
curl -sS -X PATCH "$BASE/api/v1/runtime-controls" \
  -H "Authorization: Bearer $BAEPILOT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"stopped","expected_revision":0}'
```

账号级在请求中同时提供 channel 和 account_id：

```json
{"mode":"paused","expected_revision":0,"channel":"mock","account_id":"demo-account"}
```

首次尚无账号控制行时按 revision=0；之后用 status 中该行的最新 revision。不能只提供账号身份的一半。账号开关不影响其他账号；全局开关影响全部。

## 未知投递核对

```bash
curl -sS -X POST "$BASE/api/v1/outbox/<send_id>/reconciliations" \
  -H "Authorization: Bearer $BAEPILOT_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{}'
```

空对象表示查询渠道。Mock 有回执时得到 sent；没有回执时仍是 unknown，不擅自断言失败。

管理者只有在外部证据明确时才可以提交 `{"confirmed_status":"sent"}` 或 `{"confirmed_status":"failed"}`。这会影响记忆及未来对话，不是“点一下重试”。本接口**从不重新发送**；未能核对的 unknown 继续阻塞本会话自动发送。对非 unknown 的记录返回当前记录。
