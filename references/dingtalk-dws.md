# 钉钉 DWS CLI 采集

## 能力边界

此路径通过本机 `dws` 命令行工具和当前用户亲自完成的钉钉 OAuth 授权，读取钉钉在明确时间窗口内返回的消息历史。适配器只调用只读命令 `dws auth status` 与 `dws chat message list-all`，不会发送、回复、撤回或修改消息。

它不能：

- 读取当前 OAuth 用户无权访问的会话；
- 绕过企业管理员、组织、数据授权或产品权益限制；
- 证明钉钉返回了账号自注册以来的全部历史；
- 将未返回的消息解释为任务不存在或已经完成；
- 保存 OAuth 令牌、OAuth 登录响应中的当前组织/用户元数据、分页游标或原始 API 响应；
- 下载或解析附件内容。

因此，消息库始终保留 `complete:false`。`coverage.window_complete:true` 只表示指定时间窗口已经翻页到服务端的 `hasMore:false`，不表示账号全部历史完整。

## 登录

先确认本机已有可用的 DWS CLI：

```bash
dws version --format json
```

由当前用户完成 OAuth 授权：

```bash
dws auth login --recommend --format json
```

登录流程不支持账号密码、手机号验证码或 AppSecret 代登录。授权完成后检查适配器看到的登录态：

```bash
python scripts/dingtalkcracker.py dws-doctor
```

成功结果必须同时包含：

- `success:true`；
- `auth.authenticated:true`；
- `auth.token_valid:true`。

多组织账号可在上述命令和采集命令中添加 `--profile '<profile 名或 corpId>'`。解析会话和采集时必须使用同一 profile。

## 回溯消息

时间按 UTC+08:00 解释，起止格式固定为 `YYYY-MM-DD HH:MM:SS`：

```bash
python scripts/dingtalkcracker.py collect-dws \
  --start '2026-08-01 00:00:00' \
  --end '2026-08-08 00:00:00' \
  --vault-dir '<钉钉消息库目录>'
```

采集器固定调用：

```text
dws chat message list-all --start ... --end ... --limit ... --cursor ... --format json
```

安全与分页约束：

- `--page-size` 默认为 30，最大也是 30；
- 首页游标为 `0`，后续只使用上一页返回的 `nextCursor`；
- 游标重复时立即停止，避免无限循环；
- `--max-pages` 默认 100；达到页数上限且仍有下一页时，结果成功落库但 `window_complete:false`；
- 原始 JSON 只存在于采集进程内存，不写入临时文件或消息库；
- 同一 `openMessageId` 在一次响应中出现多个展示版本时，选择时间更新、规范化内容更完整的版本；
- 重放相同窗口不会重复写入完全相同的消息；同一消息 ID 若在已落库后变成冲突内容则安全失败，不会静默覆盖证据。

## 消息库与清单

第一次运行创建：

- `dingtalk.db`：规范化 SQLite 消息库；
- `snapshot_manifest.json`：来源、覆盖范围和最近采集窗口。

DWS 消息库的关键清单字段：

```json
{
  "complete": false,
  "source": {
    "system": "dingtalk",
    "mode": "dws_user_oauth",
    "command": "dws chat message list-all"
  },
  "coverage": {
    "scope": "current_user_authorized_message_history",
    "history_backfill": true,
    "window_complete": true
  },
  "oauth_tokens_persisted": false,
  "raw_api_responses_persisted": false
}
```

不要让 DWS 和 Stream 共用同一个消息库目录。适配器会校验 `source.mode` 并拒绝混用。

## 验收

```bash
python scripts/dingtalkcracker.py inspect \
  --vault-dir '<钉钉消息库目录>'

python scripts/dingtalkcracker.py search \
  --vault-dir '<钉钉消息库目录>' \
  --keyword '<该窗口内已知消息的唯一短语>'
```

只有以下条件同时满足，才可声明指定窗口跑通：

1. `dws-doctor` 成功；
2. `collect-dws` 返回 `success:true`；
3. `window_complete:true`，或明确说明因页数上限而不完整；
4. `inspect` 的 `quick_check` 为 `ok`；
5. `search` 能返回已知消息的消息 ID、发送者和时间；
6. 重放相同窗口不会增加相同消息。

## 隐私与附件

- `dingtalk.db` 和查询输出仍是敏感聊天数据，不应提交 GitHub；
- 会话 ID、消息 ID 和发送者 ID 是证据字段，会保存在消息库中；
- 适配器脱敏常见令牌，但这不能替代选择安全的私有存储目录；
- DWS 返回的图片/文件描述只作为正文占位或 `[附件]` 保存；
- 附件占位只能证明消息中存在附件，不能证明附件内容已经读取。
