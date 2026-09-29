# Architecture / 公开审计边界

**水杯记账（ShuiBei）是一个 Telegram 记账机器人。** 核心领域是账本、客户往来、应收/预付款、对账和经营报表；MiniApp 是记账机器人的配套界面。链上查询、汇率、反诈和 FakeBot 属于辅助能力，不改变项目的“记账机器人”定位。

公开层包含账本、客户、应收、SQLite、会员状态、消息保护、Telegram 协议封装、MiniApp 和主要业务逻辑。

## 公开 Developer API

开源版可直接通过用户自己的 `SHUIBEI_DEVELOPER_API_KEY` 调用墨清公开接口：

- `GET https://api.jizhang.org/api/v1/fanzha/records`
- `GET https://api.jizhang.org/api/v1/fakebot/check`

这些是公开产品接口，不属于内部 service 路由。

## 公开版与生产版基础设施

公开版默认使用本地 SQLite、用户自己的 Developer API Key 和可选自托管 MiniApp。官方生产版本使用 PostgreSQL、细粒度并发锁以及私有部署/服务接线。

生产凭据、生产支付接线、运营后台、生产数据源、服务器文件布局和内部服务路由不进入公开仓库。

公开源码本身无法阻止第三方删除本地检查，因此商业与生产边界必须放在未公开服务能力、凭据和许可，而不是代码混淆。
