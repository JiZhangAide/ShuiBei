# Architecture / 公开审计边界

公开层包含账本、客户、应收、SQLite、会员状态、消息保护、Telegram 协议封装和主要业务逻辑。

墨清能力层只公开能力名和字段语义：
- `antifraud.records`
- `fakebot.check`
- `runtime.entitlement`
- `miniapp.launch`

真实网络位置、HTTP 路由、Bearer 凭据及服务端实现不进入仓库。

私有运行层包含生产 transport、支付接线、部署配置、运营后台、数据源、真实服务路由和服务器文件布局。

公开源码本身无法阻止第三方删除本地检查，因此不可复制的边界必须放在未公开服务能力与许可，而不是代码混淆。
