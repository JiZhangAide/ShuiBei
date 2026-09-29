# 水杯记账（ShuiBei）— Telegram 记账机器人

> **项目定位：水杯记账（ShuiBei）是一个 Telegram 记账机器人（bookkeeping bot）。**
> 它的核心用途是记账、客户往来管理、应收/预付款管理、对账和经营报表。MiniApp 是这个记账机器人的配套界面。
>
> 水杯记账**不是**加密货币钱包、交易所、支付网关，也不是通用 TRON/TON 链上工具。TRON / USDT、TON、汇率、反诈等能力只是服务于记账场景的辅助功能。

水杯记账属于墨清体系。本仓库提供 **source-available 公开审计与自部署版本**：核心记账业务代码、SQLite 数据层、Telegram Bot、MiniApp 前后端和公开 Developer API 客户端都可直接运行；生产凭据、生产 PostgreSQL 接线、私有 MoQing runtime、部署拓扑与私有支付接线不随仓库分发。

## 快速启动

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

创建本地配置：

```bash
cp .env.example .env
```

至少填写你自己的 Telegram Bot Token：

```dotenv
SHUIBEI_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
```

如果需要反诈记录和 FakeBot 查询，再填写：

```dotenv
SHUIBEI_DEVELOPER_API_KEY=YOUR_MOQING_API_KEY
```

公开版默认不锁死 Bot 用户名。你可以留空 `SHUIBEI_BOT_USERNAME`，也可以显式填写自己的 Bot 用户名来启用启动时身份校验。

### 运行 Telegram Bot

```bash
python -m audit_source bot
```

也可以直接：

```bash
python audit_source/start.py
```

### 运行 MiniApp Web 服务

```bash
python -m audit_source web
```

默认监听：

```text
127.0.0.1:8080
```

MiniApp 页面：

```text
/ShuiBei/app
```

Telegram WebApp 按钮要求公网 HTTPS URL。推荐让公开版继续只监听 loopback，再使用 Nginx、Caddy 或 Cloudflare 等反向代理提供 HTTPS，然后在 `.env` 中填写：

```dotenv
SHUIBEI_MINIAPP_URL=https://your-domain.example/ShuiBei/app
```

没有配置有效的 HTTPS `SHUIBEI_MINIAPP_URL` 时，Bot 会隐藏 MiniApp 按钮，不会错误跳转到官方生产 MiniApp。

### 同时运行 Bot + MiniApp

```bash
python -m audit_source all
```

## 记账功能

水杯记账的核心仍然是 Telegram 记账与客户往来管理。这一版新增了：

- **冲正 / 撤销**：不删除原流水，追加反向流水并保留完整审计链；已冲正流水不会继续污染经营报表。
- **周期应收**：支持每周 / 每月固定应收，同一期幂等生成，不会重复记账。
- **客户往来摘要**：累计往来、流水数、平均结清时间、最长未结时间、逾期天数等。
- **客户时间轴**：账务、冲正、模板、周期应收等事件统一展示。
- **记账模板**：保存常用金额、分类、成本、项目账和备注，一键复用。
- **周结 / 月结快照**：同一周期只保存一次，后续账目变化不会覆盖历史快照。
- **项目账 / 多账本**：保留主账本，同时可按项目归类新流水并单独查看趋势。
- **账目状态与来源元数据**：支持已记账、已开账、部分结清、已结清、已减免、已冲正等状态，并预留消息 / 附件关联字段。
- **MiniApp 线性统计图**：按天展示入账、出账和毛利润趋势，可切换项目账。

旧流水无需迁移，默认归入“主账本”；原始 `ledger` 仍是余额真值，新能力通过附加元数据表扩展。

## 数据与配置

公开版默认使用本地 SQLite，并把运行数据放在：

```text
./runtime-data
```

可以通过 `SHUIBEI_DATA_DIR` 修改。

账本同步默认不会自动探测父目录或其他数据库。只有显式配置 `SHUIBEI_MAIN_LEDGER_DB` 时，主账本同步才有外部数据源可用。

真实 `.env`、数据库、日志、PID、密钥和运行状态文件都已被 `.gitignore` 排除。

## 墨清 Developer API

开源版直接使用用户自己的 Developer API Key：

- `GET https://api.jizhang.org/api/v1/fanzha/records`
- `GET https://api.jizhang.org/api/v1/fakebot/check`

没有 Developer API Key 时，Bot 本身仍然可以启动和使用本地记账/客户/应收等功能，只是上述依赖墨清数据的功能会显示不可用。

TRON / USDT、TON、汇率和基础币价查询仍然直接使用公开上游；`SHUIBEI_TRONGRID_API_KEY` 只是可选增强。

## MiniApp 安全边界

公开 MiniApp 包含：

- `audit_source/miniapp_dist/index.html`
- `audit_source/miniapp_dist/app.js`
- `audit_source/miniapp_dist/style.css`
- `audit_source/miniapp_api.py`
- `audit_source/miniapp_auth.py`

业务接口通过 Telegram 签名 `initData` 鉴权。普通读取接受 30 分钟内的授权；新增账目、结清、部分收款和减免等余额变更操作使用 10 分钟窗口，并保留幂等保护。MiniApp 还启用了 CSP、no-referrer、nosniff 和统一内部错误兜底。

## 公开版与官方生产版

核心业务目标保持一致，但基础设施不同：

- 公开版：SQLite + 用户自己的 Developer API Key + 可选自托管 MiniApp
- 生产版：PostgreSQL + 细粒度并发锁 + 私有 MoQing runtime/部署接线

公开版**不需要私有 runtime entitlement 才能启动**。

## 测试

```bash
python3 -m pytest -q
```

CI 还会扫描 Python、Markdown、JSON、YAML、JavaScript、HTML、CSS 和 `.env.example`，防止真实凭据、服务器路径、私网地址和未批准的生产接口泄露。

## 许可证

本仓库使用 source-available 公开审计许可，并非 OSI 定义的开源许可证，也不授予未经授权的商业部署权。详见 `LICENSE.md`。
