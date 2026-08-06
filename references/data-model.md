# 数据模型与证据规则

## 企业微信核心数据库

| 数据库 | 表 | 用途 |
|---|---|---|
| `session.db` | `conversation_table` | 会话 ID、名称、备注、最后一条消息的元数据、置顶状态 |
| `message.db` | `message_table` | 消息 ID、序列号、会话 ID、发送者 ID、内容类型、Unix 发送时间、二进制内容 |
| `user.db` | `user_table` | 发送者 ID、显示名称、英文名 |

## 关联键

- 使用 `message_table.conversation_id` 关联 `conversation_table.id`。
- 使用 `message_table.sender_id` 关联 `user_table.id`。
- 将 `message_id` 视为证据标识符，将 `sequence` 视为会话内的排序字段。

## 钉钉规范化消息库

钉钉适配器将机器人实际收到的消息写入单个 `dingtalk.db`，不保存原始回调：

| 表 | 用途 |
|---|---|
| `conversation_table` | 会话 ID、名称、会话类型、最后一条已采集消息的时间和 ID |
| `message_table` | 本地序号、钉钉 `msgId`、会话、发送者、类型、Unix 发送时间、脱敏文本和证据摘要哈希 |
| `user_table` | 发送者 ID 与机器人回调中提供的显示名称 |

- 使用 `message_table.conversation_id` 关联 `conversation_table.id`；
- 使用 `message_table.sender_id` 关联 `user_table.id`；
- 将钉钉 `msgId` 视为证据标识符，并以 `dingtalk:<conversation_id>:<msgId>` 构造跨平台 `local_id`；
- `sessionWebhook`、下载码、Client Secret 和原始回调不属于证据模型，不得持久化；
- 图片、文件、音视频占位符只证明消息类型，不能证明附件内容已经读取；
- 钉钉清单的 `complete:false` 表示覆盖范围仅限机器人实际收到的消息，不表示采集器写入失败。

## 时间处理

- `send_time` 和 `last_message_time` 是 Unix 秒数。
- 脚本输出包含 UTC ISO 时间戳。
- 在确定面向用户的截止时间前，先将时间转换为用户的工作时区。
- 不要仅凭消息时间推断截止时间。

## 任务提取

将每个候选事项归入以下类别之一：

1. **明确任务**：带有指定负责人或负责团队，并包含明确或可由上下文确定的截止时间的指令或承诺。
2. **待跟进事项**：隐含需要行动，但缺少负责人、验收标准或日期。
3. **已完成/已取代事项**：后续证据明确确认已经交付、验收、取消或被其他事项替代。
4. **附件待查事项**：任务含义取决于尚未打开查看的附件内容。

对每项报告的任务，保留会话名称、发送者、时间戳和消息 ID。仅引用说明任务所需的最少文本，并隐去令牌或无关的个人数据。
