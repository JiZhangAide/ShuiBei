# MoQing Public API Contract

开源版使用用户自己的 `SHUIBEI_DEVELOPER_API_KEY` 调用公开 Developer API。

请求头：

```text
Authorization: Bearer <SHUIBEI_DEVELOPER_API_KEY>
```

当前水杯开源版直接使用：

- `GET /api/v1/fanzha/records?query=...`：成功响应包含 `count` 与 `records`；不可用时客户端降级为 unknown/unavailable。
- `GET /api/v1/fakebot/check?username=...`：客户端消费 `official/suspect/match/similarity/distance/score`；不可用时 fail closed。
- `GET /api/v1/telegram/profile/history?target=...`：用于 owner 已建立的 Telegram Business 客户公开资料历史。客户端读取 `history_total` 与 `history`，最多展示最近 6 条；最新一条直接展示，其余用 Telegram expandable blockquote 折叠。当前端点要求 Pro Developer API 权限，50 Credits / 次；不可用时仅资料历史降级，不影响本地记账。

Base URL：`https://api.jizhang.org`。

`runtime.entitlement` 与 `miniapp.launch` 仍属于私有运行能力，不通过上述 Developer API Key 暴露生产内部路由。
