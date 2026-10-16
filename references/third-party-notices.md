# 实现参考与许可

`scripts/wechat_backend.py` 中的 WCDB `Config.Cipher` 配置定位、掩码常量和 SQLCipher 4 页布局依据并改编自 fanyuantaier 的 wechatauto-replica 数据库模块：

https://github.com/fanyuantaier/wechatauto-replica/blob/main/wechatauto/db.py

上游以 Apache License 2.0 发布，许可全文保留在 [wechatauto-replica-LICENSE.txt](wechatauto-replica-LICENSE.txt)。本项目的改动包括复用 Windows CNG、禁止密钥缓存、限制当前会话、加入扫描时限、稳定捕获与 WAL-index 提交边界校验，以及整合只读查询入口。

WAL 格式和校验和规则参照 SQLite 原始文档：

https://www.sqlite.org/fileformat2.html#walformat

SQLCipher 4 加密设计参照 Zetetic 文档：

https://www.zetetic.net/sqlcipher/design/
