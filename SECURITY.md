# Security Policy

欢迎审计金额/余额一致性、SQLite 事务、消息保护、访问控制、Telegram 请求、Mini App initData 校验以及墨清运行时边界。

公开 Issue / PR 不应包含真实凭据、聊天记录、数据库样本、服务器目录、内网地址、主机名或有效 session。

公开仓硬规则：缺少墨清私有 adapter 时 fail closed；不硬编码生产凭据、真实 Developer API 路由或服务器路径；核心逻辑保持可读；运行态文件不入库；反诈/FakeBot 服务不可用时不得把 unknown 伪装成 safe。
