<p align="center">
  <img src="assets/icon-chatcracker-256.png" alt="ChatCracker logo" width="180">
</p>

# ChatCracker

**从聊天记录中，找回上下文、承诺与下一步。**

ChatCracker 是一个以本地聊天记录为基础的个人记忆与回顾工具，通过 Codex Skill 使用。它帮助你回顾工作与生活，找出做过的决定、答应过的事、尚未闭环的跟进，并让每个结论都能追溯到原始对话。

目前支持 Windows 上的**个人微信 4.x 和企业微信**。项目的方向是用统一的读取、查询与证据结构逐步接入更多聊天来源；其他平台尚未实现。

> GitHub 仓库已更名为 `chatcracker`。Skill 标识、安装目录与命令入口仍沿用 `wecomcracker`，可继续使用 `$wecomcracker` 和下方命令。

## 可以用它做什么

- **回顾工作与生活**：按时间、人物和主题整理近期对话，找回事情的来龙去脉。
- **找出待跟进事项**：梳理任务、承诺、截止时间、阻塞和等待回复的事情。
- **找回决策依据**：定位谁提出了什么、何时确认、后来是否发生变化。
- **准备项目交接**：生成带消息证据的任务与关系预览，供你审阅。

例如，你可以直接告诉 Codex：

```text
使用 $wecomcracker 整理我最近两个月的聊天记录，给我工作与生活全景，重点列出待跟进事项，并附上聊天证据。
```

## 当前支持

| 来源 | 读取方式 | 环境与范围 |
| --- | --- | --- |
| 个人微信 | `--app wechat` | 个人微信 4.x，64 位 Windows，Python 3.14 |
| 企业微信 | `--app wecom`，也可省略该选项 | 64 位 Windows，已登录的企业微信，Python |

只读取当前用户自己的本地记录，使用 Python 标准库与 Windows CNG，不安装第三方 Python 依赖。个人微信使用 SQLCipher 4，企微使用 wxSQLite3；两者共用 `scripts/wecomcracker.py` 入口。

## 安装

将项目安装到个人 Codex Skills 目录：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.codex\skills" | Out-Null

git clone https://github.com/kizzhang/chatcracker.git `
  "$env:USERPROFILE\.codex\skills\wecomcracker"
```

以下命令在项目目录下运行。创建快照时，保持对应客户端已登录并运行。

## 个人微信快速开始

需要 64 位 Windows、Python 3.14 和已登录的 `Weixin.exe`。只使用 Python 标准库和 Windows CNG，压缩消息由标准库 Zstandard 解码。

```powershell
python scripts/wecomcracker.py doctor --app wechat
python scripts/wecomcracker.py discover --app wechat
python scripts/wecomcracker.py decrypt --app wechat --db-dir '<账号目录>\db_storage' --out-dir '<全新快照目录>'
python scripts/wecomcracker.py sessions --app wechat --decrypted-dir '<快照目录>' --keyword '项目'
python scripts/wecomcracker.py search --app wechat --decrypted-dir '<快照目录>' --keyword '待办' --limit 100
```

也可以直接告诉 Codex：`使用 $wecomcracker 整理我最近两个月的个人微信，列出带聊天证据的待跟进事项。`

支持自定义数据根目录、逐库密钥验证、已提交 WAL 合并、只读查询和跨分片发送者关联。已在 Windows 微信 4.1.15.51 验证；当前不支持个人微信 3.x。完整说明见 [个人微信读取](references/personal-wechat.md)。

## 只读取自己的本地数据

ChatCracker 只面向当前 Windows 用户自己的微信或企微账号，以及用户明确授权查看的本地文件。请勿用于读取其他人的账号、远程机器，绕过 Windows 权限，转储进程内存，或发送消息。数据库密钥不输出、不缓存、不上传；明文快照只写到明确指定的独立目录。

本版本只生成供用户审阅的工作摘要和 `not_synced` 项目交接预览，不会直接向 Vikunja、Microsoft Project 或其他项目系统写入任务。

## 企业微信快速开始

推荐环境：64 位 Windows、已登录并保持运行的企业微信、Python，以及 Codex Desktop。

在 Codex 中使用：

```text
使用 $wecomcracker 回顾我最近一周的企业微信记录，梳理任务、承诺、截止时间、阻塞和未闭环事项；只生成带证据的审阅预览，不写入外部项目系统。
```

## 命令入口

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
- `snapshot_manifest.json` 完整性与 WAL 状态清单；
- 工作时间线、任务清单、决定和交付物摘要；
- 带置信度与证据的任务关系图；
- 所有事项保持 `not_synced` 的项目管理交接数据。

快照清单按来源使用独立格式标识：个人微信为 `wechat-local-snapshot-v1`，企微为 `wecom-chat-vault-snapshot-v1`。

## Skill 入口

- 主工作流：[SKILL.md](SKILL.md)
- 安全与限制：[references/safety-and-limitations.md](references/safety-and-limitations.md)
- 数据模型：[references/data-model.md](references/data-model.md)
- 任务关系：[references/task-relationships.md](references/task-relationships.md)
- 项目交接：[references/project-handoff.md](references/project-handoff.md)
- 本地读取脚本：[scripts/wecomcracker.py](scripts/wecomcracker.py)
- 零依赖自测：[scripts/self_test.py](scripts/self_test.py)

## 安全与限制

- 恢复出的数据库密钥只保留在进程内存中，绝不打印或写入磁盘。
- 加密源数据库只读；明文快照必须写入单独的新目录。
- 查询使用 SQLite `mode=ro&immutable=1` 和 `PRAGMA query_only=ON`。
- 个人微信会校验并合并已提交 WAL；企微默认发现非空源 WAL 时中止，只有用户明确接受不完整快照时才使用 `--base-only`。
- 明文快照和提取出的聊天文本都属于敏感数据，不应提交到 GitHub。
- 附件内容不会因为聊天中解析出文件名而自动被读取。
