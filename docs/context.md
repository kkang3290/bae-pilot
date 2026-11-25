# 上下文管理

## 原则与参考

参考陈子杰 / 腾讯云开发者的[《Agent上下文管理，90%的人第一步就做反了》](https://mp.weixin.qq.com/s/OFvkSyjORzjOOEDpqB5Mhw)，以及 [Anthropic：Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)：**先治理输入、移除失效中间结果、折叠可追溯历史，最后才考虑新增模型摘要。** 不应每轮先总结一遍，再把有损总结继续总结。

本项目没有文件/搜索/模型检索工具，不能直接照搬工具结果清理，也不能认定聊天中的“闲聊”没有约束价值。本轮实现的是工程治理部分，**没有新增前台摘要调用**，也没有实现模型按需检索。

## 每轮输入怎么构造

每轮从 PostgreSQL 重新构造独立请求，不依赖不断增长的服务端消息线程或 LangGraph checkpoint：

1. 在短事务中检查 auto、控制开关和 unknown，捕获 revision、本轮 queued 的数量及确认上界。
2. 对 queued 做字符下界预检。必定超限时只返回运行记录/水位和 overflow 标记，不向应用加载正文或完整 ID 列表。
3. 可继续时取候选原文的并集：**最近窗口 + 本轮全部 queued + 最近一条已记录的非 echo outbound**，按 record_id 去重排序；同一次快照读取完整现有记忆。
4. 每次角色调用按职责投影，再按预算选择完整的消息条目，得到实际模型输入和可见来源 ID。
5. 模型输出经结构/来源校验，完成后再次检查 revision，事务性确认精确且完整的 queued 集合，才允许创建 Outbox；发送仍只由 Dispatch 执行。

最近窗口默认是双方合计 40 条**消息**，不是 40 轮。它限制可选历史和后台 Memory 的提取窗口，不限制前台本轮 queued 条数。201 条短消息只要实际能放下就可以一次处理；不能取最后 40 条，却把另外 161 条也当作已读处理。

最近 outbound 是回复锚点，避免“好”“谢谢”等短回复脱离它所回应的内容；即使它在最近窗口之外也取回并保护。对于 bot，只有明确 sent 才创建内部 outbound 事件，未知投递的草稿不能冒充已发消息。若新入站先落库、上一发送回执后落库，这条 outbound 的 record_id 可能大于 MAX(queued)：queued 确认水位不能误当成可见历史上界。

所有查询按完整会话三元组隔离。历史中 ignored_control 等非 echo 消息可能作为背景，但不会被重新列入 queued 确认；恢复 auto 仍等待新入站，不补发旧积压。

## 什么必须保留

以下内容不能为节省预算被截断或删除：

- 本轮全部 queued 消息的完整原文。
- 最近 outbound 锚点（存在时）。
- 配置的表达风格。
- 完整现有记忆快照，保留 kind、content、source_event_ids。
- 当前角色必需的已生成结果。
- 已采用 Understanding 事实引用的原始消息。

**不会按 statement / summary / inference 的类别决定删哪条记忆。** summary 也可能包含用户明确边界；当前快照最多 20 项，本轮完整复用。若这些必要内容本身无法容纳，就明确失败，不以丢掉约束、截断事实或只保留半条当前消息换取自动发送。

唯一可折叠的数据是未被上述规则保护的候选历史消息，按最旧优先整条移除。不做关键词“相关性”猜测，不改写原文，也不额外调用模型给历史打分。

## 角色只看到需要的中间结果

| 调用 | 携带的已生成结果 |
|---|---|
| Understanding | 无旧 Talk 或旧 Review |
| Strategy | Understanding |
| 首次 Talk（包括升级后的首次深路径 Talk） | Understanding、Strategy，若已生成 |
| 重写 Talk | Understanding、Strategy、上版 Talk、Review 反馈 |
| Review | Understanding、Strategy、最新 Talk；不带自己的上次 Review |

完整图状态仍保留用于路由与最终校验。短路径升级的旧草稿不会发出，也不会污染 Understanding、Strategy 或新草稿生成。重写仍最多一次；调用预算、trace、GraphResult 与管理 API 合同不变。

## 字符预算与折叠

`max_context_chars` 继续使用兼容测量：

```python
len(json.dumps(context, ensure_ascii=False))
```

中文、emoji 按 Python 字符计算；引号、换行、反斜杠等 JSON 转义的额外长度也计入。共享测量函数同时用于打包和 `complete_role()` 最终硬检查。

默认规则：

| 阈值 | 默认字符数 | 行为 |
|---|---:|---|
| 软触发：硬限的 90% | 28,800 | 投影后完整输入不超过此值时不折叠历史 |
| 通常目标：硬限的 80% | 25,600 | 触发后批量移除最旧可选历史，尽力达到 |
| 硬限：100% | 32,000 | 全部字段加入后仍必须满足 |

角色投影始终执行，与折叠阈值无关。先测必要骨架：它若已超过 80% 但未超硬限，本轮只要求满足硬限，不为了不可达的 80% 把可选历史删光；超过硬限则终止。没有新增比例配置或在线 token-count 请求。

每条消息的序列化贡献只测一次，选择保留集合时只调整贡献量和小型覆盖说明，最后再完整序列化检查。不每删一条就重算全部正文。各角色始终从本轮原始候选重建，不从上一份已折叠请求继续裁剪，因此后续事实引用仍可恢复到原候选中的原文。

### queued 预检为什么安全

预检在数据库内聚合数量及 `char_length(payload->>'text')`，只扫描 `record_id > last_processed` 的 queued 尾部，利用现有 `(scope, record_id)` 索引。下界由以下部分组成：

- 所有实际正文的字符总量。
- 每条五字段 JSON 对象的最小包络，由同一个字段白名单与序列化器计算。
- 数组括号和默认 `, ` 分隔符。

真实正 BIGINT ID 和枚举字符串不会比最小模板更短，正文转义只会增加 JSON 长度，因此该下界不会高于实际消息数组大小。**下界超硬限可以拒绝；下界未超不能证明完整请求能放下。** 风格、记忆、角色结果、覆盖说明和实际转义仍由后续完整检查兜住。

### 字符不是 token

本限制只覆盖序列化的角色 context，不包括独立 system prompt、输出 JSON Schema、API 包装和输出 token。`model_max_tokens` 是输出上限，不是输入窗口。不能按字符/4 推算并宣称一定满足真实模型窗口；真正的 tokenizer/token-count 和完整请求预算尚未实现。

## 覆盖说明与证据

每份前台请求有固定形状的 `coverage`：选择规则、候选数、保留数、折叠数及至多一组折叠 ID 的 min/max。覆盖说明自身计入硬预算，不放无限增长的逐条清单。

- 统计只针对已读取的本轮候选，不伪造窗口外全部历史的省略总数。
- 数据库 record_id 是全局序列，不用两个 ID 的差计算同会话消息条数。
- min/max 是折叠 ID 的包络，不表示该连续区间内每条消息都被省略；事实保护可能让区间内某些原文仍被保留。
- 标记不是事实、不是新摘要，也不是可调用的原文读取工具。

Understanding 的 facts.source_event_ids 必须属于**该次实际输入 messages** 的 record_id，不是裁剪前的候选集合。记忆所携带但本次没展示原文的 ID 不能绕过此校验。已接受事实的原文在后续 Strategy、Talk、Review 请求中持续保护，独立审核不能只得到摘要却被要求对照不存在的原文。

原始事件始终留在 PostgreSQL，应用/管理者可按完整会话身份和来源 ID 找回；模型本轮没有取回工具。**原文可找回不等于模型视图无损。** 窗口之外或被折叠、又没有提炼进记忆的信息仍可能遗漏，包括未被保存的历史约束；提示词要求对关键依据缺失先澄清或接管，不把“未展示”当作“不存在”。

## 超限、重试与记忆

仅明确的 `AppError(code="context_too_large")` 被视为不能原样重试的确定性失败：

- 前台：记录 failed，不创建 Outbox、不确认当前 queued、不推进 last_processed。仅当 revision 仍匹配、会话仍 auto 时立即 handoff；接管会把待处理事件置为 ignored_control，而非 handled。旧 revision 的失败不能接管新消息。
- 后台 Memory：终止该任务，保留旧快照和 memory_version，不影响 sent、不接管前台。
- 其他临时模型/网络故障：维持原有限指数退避，不增加新的摘要/缩窗重试循环。

接收 HTTP 202 只代表事件入库，后台超限不把先前响应改成 413；查会话和 runs 的错误码。对不完整快照，finish_run 在事务中核对 queued 集合、可见原文、会话/水位及实际 UPDATE/RETURNING 结果，不匹配则整体回滚，不能确认一部分再发送。

后台 Memory 仍只在明确成功发送后正常触发，读取截至 source_version 的同会话最近窗口、去 echo、输入不含旧记忆，再整批替换快照。不为了本轮优化缩短该窗口，也不把它改为前台压缩器。近期窗口滚动仍可能遗忘，现有记忆不是增量长期记忆。详见 [数据库与记忆](database.md)。

## 统计与验证边界

`context_prepared` INFO 日志的附加字段记录 role、original_chars、final_chars、retained_messages、folded_messages、retained_memories；original_chars 是**角色投影后、历史折叠前**的 context，已包含覆盖说明。`context_overflow` WARNING 只记录大小/计数，绝不复制正文、风格或密钥。字段由日志 handler 提取，不新增 GraphResult/API 字段。

验证使用合成消息、RecordingProvider、真实 PostgreSQL、实际 LangGraph 与 Mock 回执，包括窗口外 queued、精确确认、预检、回复锚点、升级/重写、可见来源及超限终结。字符缩减只证明本地序列化输入更小，不证明真实 token/费用下降、Prompt Cache 命中或情感质量提高。

后续可独立评估精确 token 预算、真正的模型检索工具、证据保真的摘要兜底、长期记忆增量更新和质量评测；本轮未实现，也不会隐式启用外部调用。
