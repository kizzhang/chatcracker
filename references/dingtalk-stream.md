# 钉钉 Stream 采集

## 能力边界

钉钉适配器使用钉钉官方 `dingtalk-stream` SDK，只采集明确授权的企业内部机器人实际收到的消息。它适合从启用采集之后逐步沉淀工作证据，但不具备以下能力：

- 回溯机器人启用前的全部历史；
- 读取机器人没有收到的单聊或群聊消息；
- 绕过企业管理员、应用可用范围或群权限；
- 下载聊天附件，或保存 `sessionWebhook`、下载码与原始回调；
- 发送或自动回复钉钉消息。

因此，钉钉消息库的清单始终包含 `complete:false` 和 `coverage.scope: messages_delivered_to_this_bot`。不要把消息库中的沉默解释为没有任务或任务已经完成。

## 前置条件

1. 在钉钉开放平台创建企业内部应用并启用机器人能力；
2. 将机器人的消息接收模式配置为 Stream；
3. 设置应用可用范围，并将机器人加入需要采集的会话；
4. 使用应用的 Client ID 和 Client Secret；
5. 安装项目锁定的官方稳定版 SDK：

```bash
python -m pip install -r requirements-dingtalk.txt
```

不要把 Client Secret 写入仓库、配置示例、日志或命令参数。适配器只从环境变量读取凭据：

```bash
export DINGTALK_CLIENT_ID='<Client ID>'
export DINGTALK_CLIENT_SECRET='<Client Secret>'
```

## 验证凭据和网络

先做本地检查：

```bash
python scripts/dingtalkcracker.py doctor
```

再向钉钉申请一次 Stream 连接票据，验证应用凭据和网络：

```bash
python scripts/dingtalkcracker.py doctor --live
```

成功结果必须包含 `live_connection.success: true`。输出只包含连接主机名，不包含 Client ID、Client Secret 或连接票据。

## 开始采集

选择仅当前用户可读的本地目录：

```bash
python scripts/dingtalkcracker.py collect \
  --vault-dir '<钉钉消息库目录>'
```

第一次运行会创建 `dingtalk.db` 和 `snapshot_manifest.json`；再次运行同一命令会继续追加。按 `Ctrl+C` 安全停止。

采集器对 `msgId` 执行幂等写入：完全相同的重试不会产生重复消息；同一 `msgId` 若出现不同内容则返回存储错误，不会悄悄覆盖证据。

## 端到端验收

1. `doctor --live` 返回成功；
2. 在机器人单聊中发送一条唯一测试短语，或在已加入机器人的群里按钉钉配置要求触发机器人；
3. 采集日志的消息总数增加；
4. 运行完整性检查：

```bash
python scripts/dingtalkcracker.py inspect --vault-dir '<钉钉消息库目录>'
```

5. 搜索唯一测试短语：

```bash
python scripts/dingtalkcracker.py search \
  --vault-dir '<钉钉消息库目录>' \
  --keyword '<唯一测试短语>'
```

只有当 `quick_check` 为 `ok` 且搜索结果含有刚发送消息的 `msgId`、发送者和时间戳时，才可声明真实消息链路已经跑通。

## 离线回放

如果已经在授权范围内保存了 Stream 回调，可将每个完整 `CALLBACK` 帧或其中的 `data` 对象按一行一个 JSON 写成 JSONL，再导入：

```bash
python scripts/dingtalkcracker.py import-jsonl \
  --input '<回调.jsonl>' \
  --vault-dir '<钉钉消息库目录>'
```

导入时不会保留原始 JSONL。规范化后仅保存：会话 ID 与名称、消息 ID、发送者 ID 与名称、消息类型、发送时间、脱敏文本和证据摘要哈希。

## 查询

```bash
python scripts/dingtalkcracker.py sessions \
  --vault-dir '<钉钉消息库目录>' --limit 50

python scripts/dingtalkcracker.py messages \
  --vault-dir '<钉钉消息库目录>' \
  --conversation-id '<会话 ID>' --limit 100

python scripts/dingtalkcracker.py search \
  --vault-dir '<钉钉消息库目录>' \
  --keyword '待办' --limit 100
```

`--since` 为包含边界，`--until` 为不包含边界，均使用 Unix 秒时间戳。

## 隐私与附件

- `dingtalk.db` 和查询输出仍然属于敏感聊天数据，不应提交 GitHub；
- 适配器会脱敏常见令牌，并完全丢弃 `sessionWebhook`、下载码与原始回调；
- 图片、语音、视频和文件只保存 `[图片]`、`[语音]`、`[视频]`、`[文件]` 占位符；
- 占位符只能证明机器人收到过相应类型的消息，不能证明附件内容已经读取。
