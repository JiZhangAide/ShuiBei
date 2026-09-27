# ShuiBei / 水杯记账

水杯记账是墨清体系中的轻量记账机器人。本仓库提供**公开审计源码（source-available audit source）**，目标是让业务核心可读、可审计，同时不分发生产凭据、内部路径、私有服务接线和可直接复刻线上服务的部署材料。

当前快照按 2026-09-27 的生产代码整理，覆盖账本、客户、应收、SQLite、消息保护、会员/访问控制、Telegram 客户端、Mini App initData 校验，以及反诈/FakeBot 的字段语义。

反诈、FakeBot、Mini App 启动和运行授权均通过 `audit_source/moqing_gateway.py` 进入私有墨清运行层。公开仓不保存真实生产域名/路由、服务器目录、密钥文件位置、上游 session 或部署拓扑。

完整快照默认 fail closed。公开源码无法物理阻止第三方修改本地检查，所以商业保护的真实边界是私有后端能力、生产凭据、品牌/许可和未公开运行环境，而不是混淆代码。

不公开：生产配置与凭据、服务器路径/内网信息/真实 Developer API 路由、启动部署脚本、私有 transport、支付接线、运营后台、生产数据库/日志/运行态文件，以及 Mini App 生产发布路由。

优先审计：`audit_source/app.py`、`ledger.py`、`db.py`、`customers.py`、`receivables.py`、`message_protection.py`、`access_control.py`、`moqing_gateway.py`、`runtime_guard.py`。

本仓库不是 OSI 定义的开源发行版，也不授予第三方商业运营权。见 `AUDIT-LICENSE.md`。
