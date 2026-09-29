# Architecture / 公开审计边界

公开层包含账本、客户、应收、SQLite、会员状态、消息保护、Telegram 协议封装和主要业务逻辑。

## 公开 Developer API

开源版可直接通过用户自己的 `SHUIBEI_DEVELOPER_API_KEY` 调用墨清公开接口：

- `GET https://api.jizhang.org/api/v1/fanzha/records`
- `GET https://api.jizhang.org/api/v1/fakebot/check`

这些是公开产品接口，不属于内部 service 路由。

## 私有运行层

以下能力仍保持私有接线：

- `runtime.entitlement`
- `miniapp.launch`
- 生产支付接线
- 运营后台与生产 transport
- 生产数据源、服务器文件布局和内部服务路由

生产版本使用 PostgreSQL 与私有运行层；公开版默认使用本地 SQLite 和公开 Developer API。

公开源码本身无法阻止第三方删除本地检查，因此不可复制的边界必须放在未公开服务能力与许可，而不是代码混淆。
