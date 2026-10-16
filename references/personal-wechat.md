# 个人微信 4.x 本地读取

使用统一入口 `scripts/wecomcracker.py`，在子命令后加 `--app wechat`。现有企微命令和兼容入口不变。本实现已在 Windows 微信 4.1.15.51 验证；不同版本可能改变内存配置布局，无法校验时应停止，不注入客户端或修改权限。

## 环境与发现

需要 64 位 Windows、正常权限的 Python 3.14，以及本用户登录中的 `Weixin.exe`。压缩正文使用标准库 `compression.zstd`；不安装第三方 Python 依赖。

```powershell
python scripts/wecomcracker.py doctor --app wechat
python scripts/wecomcracker.py discover --app wechat
python scripts/wecomcracker.py discover --app wechat --data-root 'E:\WeChat'
```

自动发现读取当前用户的配置和注册表，也支持传入保存根目录、账号目录或 `db_storage`。多个账号时先明确用户要读取的账号。数据库密钥仅从当前 Windows 会话中可读的 `Weixin.exe` 恢复，并须通过所选目录各库的首个页 HMAC 校验；不保存密钥或内存内容。

## 创建快照

```powershell
python scripts/wecomcracker.py decrypt --app wechat `
  --db-dir '<账号目录>\db_storage' `
  --out-dir '<源目录以外的全新目录>' --timeout 120
```

脚本拒绝现存输出路径、与源相交的路径和经过重解析点的输出路径。先验证所有待读取库的密钥，随后逐库复制稳定的加密数据库、WAL 与 WAL-index 头，再按 SQLCipher 4 的 4096 字节页进行 AES-256-CBC 解密与 HMAC-SHA512 认证。仅跳过无进程内密钥、查询不使用的保留库 `migrate/unspportmsg.db`，在清单的 `skipped_databases` 中记录路径与原因；其他数据库仍须全部验证。

WAL 检查头、代次、连续校验和及提交边界；存在稳定 WAL-index 时，先将其 `aSalt` 与 WAL 头的 salt 逐字节核对，再以其 `mxFrame` 为已提交边界。代次不一致时拒绝快照，避免把旧代次帧数当作当前提交边界；清单的 `wal_index` 同时保留帧数和代次标识。只应用已提交帧，处理数据库截断，拒绝丢帧或损坏的当前代次。此模式不接受 `--base-only`。

成功要求 `success:true`、`complete:true`、`failed:0`，每库 `quick_check:ok`。结果在 `db/`，清单在 `snapshot_manifest.json`。报告输出路径、`complete` 和 `wal_processed`。

所有数据库先写入输出路径旁的唯一临时目录；全部校验通过后，才通过目录重命名发布最终快照。失败或键盘中断会删除临时目录，使原输出路径可用于重试。若系统拒绝清理，错误会明确列出残留临时目录，不能将其视为成功快照。

捕获期间 WAL/SHM 被微信删除或替换时，会将该次捕获视为不稳定并重试，最多尝试 12 次；权限错误等其他读取错误仍会中止。

微信的三个 FTS 库依赖 `MMFtsTokenizer`。标准 SQLite 无法验证其分词器语义时，脚本校验所有物理表和 schema，在 `quick_check_scope` 明确标注范围；聊天正文保留在普通消息库中。逐库稳定捕获不保证整个账号各库属于同一个原子事务；清单记录各库时间，`cross_database_atomic:false`。

## 查询

```powershell
python scripts/wecomcracker.py inspect --app wechat --decrypted-dir '<快照>'
python scripts/wecomcracker.py sessions --app wechat --decrypted-dir '<快照>' --keyword '项目'
python scripts/wecomcracker.py messages --app wechat --decrypted-dir '<快照>' --conversation-id '<username 或会话哈希>' --limit 100
python scripts/wecomcracker.py search --app wechat --decrypted-dir '<快照>' --keyword '待办' --limit 100
```

`messages`、`search` 支持包含边界的 Unix 秒 `--since`、`--until`；`search` 也可加 `--conversation-id`。先用 `sessions` 将显示名称解析为准确会话 ID。查询合并普通聊天和公众号各分片，按时间从新到旧返回，同秒消息按 `sort_seq` 从大到小排序；不需要额外导出索引。

查询仅接受成功的快照，使用不可变只读 SQLite，并拒绝非空明文 WAL。输出含 `complete`、`wal_processed`、消息来源库和表、`local_id`、`server_id`、发送者、UTC 时间、正文与解析状态。文件传输密钥和类似令牌在文本输出中遮盖。`scanned_unresolved_messages` 表示扫描中未解出的正文，不能把空解析当成不存在消息。

`inspect` 会重新执行不可变只读完整性检查，并将每个数据库的当前 SHA-256 与快照清单核对；损坏、修改、缺少记录哈希或存在非空明文 WAL 时拒绝报告成功。`quick_check_scope` 来自本次实际检查；微信专用 FTS 库仍明确标注物理表检查范围。若压缩备用正文未能解析，消息保留 `compress_content:..._unresolved` 状态，并计入未解析消息数量，即使未恢复出文本。

## 数据模型与整理

- `contact/contact.db` 的 `contact` 提供备注和昵称；其中含群和公众号，不等于好友总数。
- `session/session.db` 的 `SessionTable` 提供会话身份。
- `message/message_*.db` 与 `biz_message_*.db` 的 `Msg_<MD5(username)>` 保存消息。
- 发送者编号必须关联**消息所在分片**的 `Name2Id.rowid`，不可套用其他库的编号。
- `message_content`、`compress_content` 可能为 UTF-8 或 Zstandard；复合 `local_type` 的低 32 位表示基础类型。
- 收藏、朋友圈和媒体数据库随快照保留；消息查询命令不自动转写语音、识别图片或读取附件。

做工作生活回顾时，先明确日期和时区，优先检查本人参与的对话与点名事项。群公告、推送和转发并不自动构成本人的承诺。核对后续回复后再区分完成、取消、替代和未确认事项，证据保留会话、时间和消息来源 ID。不要把沉默判定为完成，也不要把聊天中的提议写成已经达成的结果。
