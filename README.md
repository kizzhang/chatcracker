<p align="center">
  <img src="assets/icon-b-clean-wecom-256.png" alt="WeComCracker logo" width="220">
</p>

# WeComCracker：企微工作回溯

WeComCracker 是一个给 Codex 使用的工作聊天回溯 Skill。它可以从当前 Windows 用户自己的企业微信本地聊天，或当前用户通过钉钉 OAuth 明确授权的消息历史中，重建有证据支持的工作时间线、任务清单、决定、交付物、阻塞关系和未闭环事项，并生成可审阅的项目管理交接预览。

企业微信本地读取器只使用 Python 标准库与 Windows CNG，不依赖第三方 Python 包；当企业微信 API 或 CLI 无法提供足够长的历史记录时，可读取本机 wxSQLite3 数据库快照。钉钉历史采集器通过 DWS CLI 使用当前用户 OAuth 登录，可在 Windows、macOS 和 Linux 上运行；官方 Stream SDK 仍可作为机器人实时采集的可选路径。

## 只读取自己的授权数据

企业微信路径只面向当前 Windows 用户自己的企业微信账号；钉钉路径只面向当前用户亲自完成 OAuth 授权后可读取的消息，或用户明确授权的企业内部机器人。请勿用于读取其他人的账号、远程机器，绕过系统或企业权限，转储进程内存，或发送聊天消息。

本版本只生成供用户审阅的工作摘要和 `not_synced` 项目交接预览，不会直接向 Vikunja、Microsoft Project 或其他项目系统写入任务。

## 企业微信：30 秒上手

推荐环境：64 位 Windows、已登录并保持运行的企业微信、Python，以及 Codex Desktop。

直接安装到个人 Codex Skills 目录：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.codex\skills" | Out-Null

git clone https://github.com/kizzhang/wecomcracker.git `
  "$env:USERPROFILE\.codex\skills\wecomcracker"
```

然后在 Codex 中使用：

```text
使用 $wecomcracker 回顾我最近一周的企业微信记录，梳理任务、承诺、截止时间、阻塞和未闭环事项；只生成带证据的审阅预览，不写入外部项目系统。
```

## 企业微信命令入口

先检查环境并发现当前账号的数据目录：

```powershell
python scripts/wecomcracker.py doctor
python scripts/wecomcracker.py discover
```

创建经过校验的明文快照：

```powershell
python scripts/wecomcracker.py decrypt `
  --db-dir '<账号目录>\Data' `
  --out-dir '<全新的明文输出目录>' `
  --timeout 120 `
  --verbose
```

查询会话和消息：

```powershell
python scripts/wecomcracker.py sessions `
  --decrypted-dir '<快照目录>' --keyword 'AI' --limit 50

python scripts/wecomcracker.py messages `
  --decrypted-dir '<快照目录>' --conversation-id '<会话 ID>' --limit 100

python scripts/wecomcracker.py search `
  --decrypted-dir '<快照目录>' --keyword '待办' --limit 100
```

## 钉钉：用 DWS CLI 登录并回溯消息

先安装可用的 DWS CLI，然后由当前用户完成钉钉 OAuth 授权。认证令牌由 DWS 自己管理，不会写入本仓库的消息库：

```bash
dws auth login --recommend --format json
python scripts/dingtalkcracker.py dws-doctor
```

按明确的 UTC+08:00 时间窗口回溯消息：

```bash
python scripts/dingtalkcracker.py collect-dws \
  --start '2026-08-01 00:00:00' \
  --end '2026-08-08 00:00:00' \
  --vault-dir '<钉钉消息库目录>'
```

每页最多读取 30 条并按 `nextCursor` 翻页；只有服务端返回 `hasMore:false` 时，清单中的 `coverage.window_complete` 才为 `true`。同一时间窗口可以安全重放，消息按 `openMessageId` 幂等写入。

验收消息库：

```bash
python scripts/dingtalkcracker.py inspect \
  --vault-dir '<钉钉消息库目录>'

python scripts/dingtalkcracker.py search \
  --vault-dir '<钉钉消息库目录>' \
  --keyword '<已知消息中的唯一短语>'
```

`dws-doctor`、SQLite `quick_check`、`window_complete:true` 和已知消息搜索全部成功，才表示这个时间窗口的真实链路跑通。完整登录、采集与隐私边界见 [钉钉 DWS CLI 采集说明](references/dingtalk-dws.md)。

## 钉钉：可选的机器人 Stream 实时采集

需要从启用后持续监听机器人收到的新消息时，安装锁定的官方 Stream SDK：

```bash
python -m pip install -r requirements-dingtalk.txt
export DINGTALK_CLIENT_ID='<Client ID>'
export DINGTALK_CLIENT_SECRET='<Client Secret>'

python scripts/dingtalkcracker.py doctor --live
python scripts/dingtalkcracker.py collect \
  --vault-dir '<钉钉消息库目录>'
```

Stream 消息库与 DWS 历史消息库必须使用不同目录，避免混淆来源和覆盖范围。完整配置、离线 JSONL 回放和安全边界见 [钉钉 Stream 采集说明](references/dingtalk-stream.md)。

## 能梳理什么

- 明确任务、负责人、期限和验收证据；
- 隐含跟进、等待状态和未闭环事项；
- 决定、审批、变更指令和已被替代的工作；
- 已承诺或已交付的产物；
- `blocks`、`precedes`、`parent_of`、`parallel_with` 等任务关系；
- 关键链、硬阻塞、可并行分支和需要确认的依赖；
- 适用于后续 Vikunja 或 Microsoft Project 连接器的标准化交接预览。

每个结论都应保留会话、发送者、时间戳和消息 ID 等证据。不要根据沉默推断任务已经完成，也不要把聊天中出现的文件名当作已经读取了附件内容。

## 会输出什么

- 经过 `PRAGMA quick_check` 校验的明文 SQLite 快照；
- 经过 `PRAGMA quick_check` 校验、可幂等追加的钉钉 SQLite 消息库；
- `snapshot_manifest.json` 数据来源、覆盖范围与 WAL 状态清单；
- 工作时间线、任务清单、决定和交付物摘要；
- 带置信度与证据的任务关系图；
- 所有事项保持 `not_synced` 的项目管理交接数据。

为兼容已经生成的快照，清单中的格式标识暂时保留为 `wecom-chat-vault-snapshot-v1`。

钉钉消息库使用 `dingtalk-chat-vault-v1`，并始终标记 `complete:false`。DWS 路径使用 `coverage.scope: current_user_authorized_message_history`，Stream 路径使用 `coverage.scope: messages_delivered_to_this_bot`；两者都不得表述成账号全部历史。

## Skill 入口

- 主工作流：[SKILL.md](SKILL.md)
- 安全与限制：[references/safety-and-limitations.md](references/safety-and-limitations.md)
- 钉钉 DWS CLI 采集：[references/dingtalk-dws.md](references/dingtalk-dws.md)
- 钉钉 Stream 采集：[references/dingtalk-stream.md](references/dingtalk-stream.md)
- 数据模型：[references/data-model.md](references/data-model.md)
- 任务关系：[references/task-relationships.md](references/task-relationships.md)
- 项目交接：[references/project-handoff.md](references/project-handoff.md)
- 本地读取脚本：[scripts/wecomcracker.py](scripts/wecomcracker.py)
- 钉钉采集脚本：[scripts/dingtalkcracker.py](scripts/dingtalkcracker.py)
- 零依赖自测：[scripts/self_test.py](scripts/self_test.py)
- 钉钉自测：[scripts/dingtalk_self_test.py](scripts/dingtalk_self_test.py)

## 安全与限制

- 恢复出的数据库密钥只保留在进程内存中，绝不打印或写入磁盘。
- 加密源数据库只读；明文快照必须写入单独的新目录。
- 企业微信快照查询使用 SQLite `mode=ro&immutable=1` 和 `PRAGMA query_only=ON`；钉钉消息库查询使用 `mode=ro` 和 `PRAGMA query_only=ON`，允许采集器继续追加。
- 默认发现非空源 WAL 时安全中止；只有用户明确接受不完整快照时才使用 `--base-only`。
- 明文快照和提取出的聊天文本都属于敏感数据，不应提交到 GitHub。
- 附件内容不会因为聊天中解析出文件名而自动被读取。
- DWS OAuth 令牌由 DWS 自己管理；适配器不会将令牌、OAuth 登录响应中的当前组织/用户元数据、分页游标或原始 API 响应写入消息库。作为消息证据的会话 ID 和发送者 ID 仍会保存。
- Stream Client Secret 只从环境变量读取；不会写入消息库或输出。
- 钉钉采集器丢弃 `sessionWebhook`、下载码与原始回调，附件只保留类型占位符。
- DWS 只覆盖明确时间窗口内由钉钉返回的授权消息；Stream 只覆盖机器人实际收到的消息。不得根据任一路径的缺失消息推断工作状态。
