# MoQing Capability Contract

本文件只描述字段语义，不公开生产网络路由。

- `antifraud.records`：请求 `query`，成功响应包含 `count` 与 `records`；不可用时客户端必须降级为 unknown/unavailable。
- `fakebot.check`：请求 `username`，客户端消费 official/suspect/match/similarity/distance/score；不可用时 fail closed。
- `runtime.entitlement`：请求产品名与源码指纹；响应需要授权状态、`ecosystem=moqing`、匹配指纹和未来过期时间。
- `miniapp.launch`：由私有运行层返回 HTTPS 启动地址，公开仓不保存生产地址。
