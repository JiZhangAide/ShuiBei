# ShuiBei / 水杯记账

水杯记账是墨清体系中的轻量记账机器人。本仓库提供**公开审计源码（source-available audit source）**，目标是让业务核心可读、可审计，同时不分发生产凭据、服务器路径、私有服务接线和生产数据。

当前公开快照覆盖账本、客户、应收、SQLite、本地消息保护、会员/访问控制、Telegram 客户端、Mini App initData 校验，以及反诈/FakeBot 的客户端调用逻辑。

## 开源版的 Developer API

开源版直接使用墨清公开 Developer API，不需要生产私有 gateway 来读取反诈/FakeBot 数据：

- `GET https://api.jizhang.org/api/v1/fanzha/records`
- `GET https://api.jizhang.org/api/v1/fakebot/check`

这些接口使用你自己的 Developer API Key。生产版本仍使用独立的私有运行时接线；本次 `.env` 改动**仅用于开源版**。

TRON / USDT、TON、汇率与基础币价查询仍可直接使用公开上游（TronGrid、TON API、CoinGecko/汇率服务），无需为了这些普通公开数据额外消耗墨清 API Credits。

## .env 配置

仓库只提交 `.env.example`，真实 `.env` 永远不应进入 Git。

```bash
cp .env.example .env
```

至少按你的用途填写：

```dotenv
SHUIBEI_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
SHUIBEI_DEVELOPER_API_KEY=YOUR_MOQING_API_KEY
```

可选：

```dotenv
SHUIBEI_TRONGRID_API_KEY=
SHUIBEI_ADMIN_IDS=
SHUIBEI_DATA_DIR=./runtime-data
SHUIBEI_BOT_USERNAME=@ShuiBei_bot
```

开源版会在导入配置时自动读取仓库根目录 `.env`；真实环境变量优先于 `.env`。

## 公开与私有边界

公开仓允许看到公开 Developer API 的正式域名和上述公开路由；仍然不公开生产内部 service 路由、生产凭据、服务器目录、私网信息、支付接线、运营后台、生产数据库/日志/运行态文件或私有 transport。

Mini App 生产发布地址与运行授权仍属于私有运行层。公开源码本身无法物理阻止第三方删除本地检查，所以商业保护的真实边界是私有服务能力、生产凭据、品牌/许可和未公开运行环境，而不是代码混淆。

## 本地测试

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest -q
```

## 许可证

本仓库不是 OSI 定义的开源发行版，也不授予第三方商业运营权。详见 `LICENSE.md`。
