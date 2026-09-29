# MoQing Public API Contract

开源版使用用户自己的 `SHUIBEI_DEVELOPER_API_KEY` 调用公开 Developer API。

请求头：

```text
Authorization: Bearer <SHUIBEI_DEVELOPER_API_KEY>
```

当前水杯开源版直接使用：

- `GET /api/v1/fanzha/records?query=...`：成功响应包含 `count` 与 `records`；不可用时客户端降级为 unknown/unavailable。
- `GET /api/v1/fakebot/check?username=...`：客户端消费 `official/suspect/match/similarity/distance/score`；不可用时 fail closed。

Base URL：`https://api.jizhang.org`。

`runtime.entitlement` 与 `miniapp.launch` 仍属于私有运行能力，不通过上述 Developer API Key 暴露生产内部路由。
