> **这份文件是插件仓库与中继仓库共享的线协议契约。**
> 两边的副本必须保持一致：中继仓库里有一条跨语言测试逐字比对 op 白名单
> （`tests/test_protocol.py::test_python_and_plugin_op_allowlists_agree`），其余部分靠人同步。
> 改动协议的正确顺序：改这里 → 同步另一份 → 两边同时升 `PROTOCOL_VERSION`。
> 中继以 `backend/` 单独部署时，那条跨语言测试会自动 skip（那里没有插件那一半）。

# DSH Remote Bridge — 协议规范 v1

本文档是 **唯一契约**。桌面插件（`plugin/`）与 FastAPI 中继（`backend/`）都必须严格按此实现，
两端的常量分别位于：

- `plugin/src/protocol.js` → `PROTOCOL_VERSION`
- `backend/app/protocol.py` → `PROTOCOL_VERSION`

任何一方改动线协议都必须同时提升版本号并更新本文档。

---

## 1. 拓扑

```
Flutter App ──HTTPS/WSS──▶ FastAPI 中继 ◀──出站 WSS── dsh-remote-bridge (桌面插件)
                              │                              │
                          SQLite(桌面/手机/审计)      typertGateway / session.event
                                                              │
                                                        DSH Host
```

- **桌面永远不接受入站连接。** 插件是 WSS 的*主动拨号方*（dialer），因此家用 NAT 后无需端口映射。
- 中继**不解析、不落盘**任何会话内容；它只做鉴权、请求/响应关联与事件扇出。
- 一个中继实例 = 一条 uvicorn worker（请求关联表在内存中）。横向扩展需要 Redis，v1 不支持。

术语：
- **desktop（桌面）**：一台运行 DSH 的机器，由连接器令牌标识 → `desktops` 表。
- **phone（手机）**：一个配对过的移动客户端 → `devices` 表。两者是不同的东西。

---

## 2. 插件 ↔ 中继：帧协议

单一 WebSocket 连接，承载 UTF-8 JSON **文本帧**。所有帧都是对象，含 `t`（type）字段。
未知的额外字段会被双方忽略（向前兼容）。

### 2.1 接入

```
wss://<host>/api/v1/attach?token=<connector-token>
```

令牌通过**查询参数**传递，因为 Node 的 WHATWG `WebSocket` 不支持自定义请求头。
服务端同时接受 `Authorization: Bearer <token>`。服务端必须在日志中对该查询参数脱敏。

握手顺序：

1. 插件连接。服务端 **accept 后校验** 连接器令牌；失败 → 以 `4401` 关闭
   （而不是 HTTP 403，这样插件能区分「令牌无效」与「网络故障」并停止重连）。
2. 插件发送 `hello`。
3. 服务端校验 `v` 与 `deviceId`。成功 → 回 `welcome`；失败 → 回 `bye` 并以 `4400` 关闭。
4. 进入稳态：双向 `req`/`res`、`sub`→`evt`、`ping`/`pong`。
5. 服务端每 `DSH_RELAY_HEARTBEAT_SECONDS`（默认 30）发一次 `ping`；若
   `2.5 × 间隔` 内没有收到插件任何帧，服务端以 `bye` + `1001` 关闭该链路。

### 2.2 闭码约定

| 闭码 | 含义 | 插件行为 |
|---|---|---|
| `4001` | 同一 `deviceId` 被新连接接管 | **停止重连**（可用 `reconnectOnReplaced` 覆盖） |
| `4400` | 协议版本不符或 `deviceId` 为空 | 停止重连，打印原因 |
| `4401` | 连接器令牌无效/被撤销 | 停止重连，提示重新签发令牌 |
| 其它 | 网络类断开 | 指数退避重连（1s 起，×2，上限 30s，±20% 抖动） |

### 2.3 插件 → 中继

| `t` | 载荷 | 说明 |
|---|---|---|
| `hello` | `v, deviceId, deviceName, platform, harness{version,cwd?}, capabilities[]` | 每连接一次。**`harness.version` 是插件自己的版本**，不是 harness 的版本——打包后的 app 版本在 `app.asar` 里，插件看不到。字段名是历史原因，两边含义一致即可 |
| `res` | `id, ok:true, value` 或 `id, ok:false, error{code,message}` | 应答单次 `req` |
| `stream` | `id, phase:"open"\|"chunk"\|"end"\|"error", value?, error?` | 应答流式 `req` |
| `evt` | `topic, payload` | 主动推送（见 §2.5） |
| `ping` | `id` | 插件侧保活；中继必须以 `pong` 回应 |
| `pong` | `id` | 应答中继的 `ping` |

`capabilities` 取值：`"ops"`、`"events"`、`"approvals"`。中继据此决定是否向该设备开放相应能力
（缺失 `events` 时订阅返回 `501`）。

> **`ping`/`pong` 是双向的。** 两侧都需要独立探活：中继用它判断插件是否沉默，插件用它判断
> socket 是否半开（TCP 半开时 `send` 不会立刻报错）。任一方收到 `ping` 都必须用相同 `id` 回
> `pong`。
>
> 这里踩过一次坑：早期版本只把 `ping` 放在中继→插件的方向上，于是插件每 30 秒的心跳被当成
> 非法帧，中继回 `bye` 并断链——表现为**每 31 秒稳定重连一次**，而所有本地测试都跑不满 30 秒
> 所以全部漏过。回归用例见 `tests/test_e2e.py::test_plugin_heartbeat_ping_is_answered_and_keeps_the_link`。

### 2.4 中继 → 插件

| `t` | 载荷 | 说明 |
|---|---|---|
| `welcome` | `v, deviceId, serverTime` | 握手成功 |
| `bye` | `code, message` | 握手失败或心跳超时，随后关闭 |
| `req` | `id, op, args, deadlineMs?` | 单次调用；流式调用不带 `deadlineMs` |
| `cancel` | `id` | 取消进行中的 `req`（超时或客户端断开时发出） |
| `sub` | `id, topics[], args?` | 开始推送某组 topic；`id` 形如 `<phoneId>:<subId>` |
| `unsub` | `id` | 停止推送该订阅 |
| `approval` | `askId, decision, answers?` | 手机对某次审批的决策 |
| `ping` | `id` | 中继侧保活；插件必须以 `pong` 回应 |

**中继从不向插件发送 `res` 或 `stream`。**

### 2.4.1 未知帧与损坏帧的区别

中继按 `t` 分两步解析入站帧，两种失败的处理方式**不同**：

| 情况 | 错误码 | 中继行为 |
|---|---|---|
| `t` 是中继不认识的类型 | `unknown_frame` | **记警告并忽略**，链路保持（向前兼容：新插件可以安全地对旧中继说新话） |
| 不是 JSON、不是对象、`t` 不是字符串、字段类型不对 | `bad_args` | 回 `bye` 并**关闭链路**（这是协议违规） |

把「不认识的帧」也当成致命错误是危险的：任何一次版本错配都会让整条隧道反复断开，
而现象只是「设备时断时续」，很难定位。

### 2.5 事件 topic

| topic | `payload` | 触发源 |
|---|---|---|
| `session.event` | `{sessionId, seq, type, time, data, surfaceOp?, sourceEventSeqs?}` | `session/event` |
| `session.status` | `{sessionId, status}` | `agent/status` |
| `session.activity` | `{kind:"added"\|"removed"\|"activity", sessionId, ...}` | `api-session/*` |
| `approval.ask` | `{askId, sessionId, toolName, callId?, reason?, displayReason?, deadline}` | `approval/request` |
| `question.ask` | `{askId, sessionId, questions[], wait?, deadline}` | `user-questions/request` |
| `approval.settled` | `{askId, by}` | 本地结算 |
| `device.status` | `{deviceId, connected}` | 链路状态变化（中继生成） |

`by` 取值：`phone` | `timeout` | `cancelled` | `link_lost` | `desktop`。
只有**真正发给过手机**的 ask 才会产生 `approval.settled`。

`session.event` 的 `seq` 单调递增，客户端**必须**按 `seq` 去重与排序。
`session/event` 的 `data` 是 DSH 原始事件数据，中继不做任何解释。

`device.status` 是**设备级广播**，不属于任何订阅，因此其 `evt` 帧的 `subId` 为 `null`。

### 2.6 op 表（v1）

全部映射到 DSH 的 `typertGateway`，除 `harness.info` 由插件本地实现。

| op | Remote | `args` | 流式 |
|---|---|---|---|
| `harness.info` | —（本地） | `{}` | 否 |
| `session.list` | `session.list` | `{cursor?}` | 否 |
| `session.page` | `session.page` | `{address, throughSeq, beforeSeq?, maxMessages?, turnWindow?}` | 否 |
| `session.follow` | `session.follow` | `{address, assistantStream?:true, ...}` | **是** |
| `session.prompt` | `session.prompt` | `{requestId, sessionId, mode, content[], clientTimeZone?}` | 否 |
| `session.cancel` | `session.cancel` | `{sessionId}` | 否 |
| `session.create` | `session.create` | `{workspaceId?, cwd?, agentPreset?}` | 否 |
| `session.selectModel` | `session.selectModel` | `{sessionId, provider, model, reasoningEffort?}` | 否 |
| `userQuestions.answer` | `userQuestions.answer` | `{agentId, callId, answer:{answers:[{id, selected[], custom?}]}}` | 否 |
| `fileUploads.upload` | `fileUploads.upload` | `{agentId, request:{data, name?}}` | 否 |
| `model.catalog` | `session.modelCatalog` | `{}` | 否 |

**模型**：切换模型是**独立调用**，`session.prompt` 没有 model 参数。当前选择由
`session.list` 的 `projections.values.modelSelection` 给出（`next` 为下一回合将使用的，
`lastUsed` 为兜底），所以显示当前模型不需要额外请求。`model.catalog` 返回
`{default, groups:[{id, name, models:[{id, name}]}], routableProviders, failures}`，
其中 `groups[].id` 就是 `selectModel` 要的 `provider`。
`reasoningEffort` 可选：省略时由 Harness 为所选模型挑默认值，避免传入该模型不支持的档位。

**提问作答**：`userQuestions.answer` 的三个 wire 参数名取自权威描述符（第三个叫 `answer`
而不是 `request`，最后一个参数的类型是 `AskUserQuestionAnswer`）。注意它**只对「已 continued」
的提问**有效：阻塞中的提问由 answerer waterfall 作答，走 `approval` 帧那条路。

**附件**：`session.prompt` 的 `content[]` 支持三种块——

| 块 | 形状 | 用途 |
|---|---|---|
| 文本 | `{type:"text", text}` | 普通消息 |
| 图片（内联） | `{type:"image", mediaType:"image/png\|jpeg\|webp\|gif", data:<base64>, name?}` | 图片**不需要上传**，直接随 prompt 送 |
| 文件（凭据） | `{type:"file", receiptId}` | 任意文件：先 `fileUploads.upload` 换 `receiptId`，再随 prompt 引用 |

`fileUploads.upload` 的 `request.data` 是**原始字节的 base64**（DSH 客户端自己也是
`bytesToBase64(data)`），返回 `{receiptId, file:{attachmentId, name, bytes}}`；`receiptId`
由 Session Controller 在 prompt 受理时消费，**一次性**。
`session.attachment`（`{sessionId, attachmentId}`）只是**回读**附件，发消息用不到，所以不在白名单里。

> 体积：中继的 `Content-Length` 上限默认 4 MiB（`DSH_RELAY_MAX_REQUEST_BYTES`），
> 而 base64 会放大 4/3。所以经这条路能送的文件约 **≤ 3 MiB**；图片应在手机端压缩后再送
> （长边约 2000 px 的 JPEG 通常远低于这个数）。要送更大的文件，得给附件单开一条分块上传路由。

**两侧都强制白名单**：未列出的 op 一律以 `error.code = "op_not_supported"` 拒绝，
不得转发任意 namespace/method。中继拒绝时返回 HTTP `501`。

### 2.7 错误码

| code | 含义 | 中继 HTTP 映射 |
|---|---|---|
| `op_not_supported` | op 不在白名单 | 501 |
| `bad_args` | 参数不满足契约 | 400 |
| `remote_error` | Remote 抛出业务错误（`message` 前缀为原始码） | 400 |
| `timeout` | 超过 `deadlineMs` | 504 |
| `cancelled` | 被 `cancel` 取消 | 409 |
| `link_lost` | 链路断开，请求未完成 | 503 |
| `device_offline` | 设备未连接 | 503 |
| `device_busy` | 该设备在途请求数达到上限 | 429 |
| `session_not_allowed` | 会话不在 `allowedSessions` 白名单内 | 403 |
| `gateway_internal` | 插件内部异常 / 网关不可用 | 500 |

---

## 3. App ↔ 中继：HTTP / WS API

除 `/`、`/healthz`、`/docs` 与配对接口外，全部需要 `Authorization: Bearer <device-token>`。
错误统一为 `{"ok": false, "error": {"code", "message"}}`。

### 3.1 配对

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| `POST` | `/api/v1/auth/pair/start` | `{deviceName}` | `{ok, value:{code, expiresAt, ttlSeconds}}` |
| `POST` | `/api/v1/auth/pair/claim` | `{code}` | `{ok, value:{deviceToken, deviceId, deviceName}}` |

配对码为 6 位数字，TTL 默认 300 秒。**必须**由管理员在服务器上用 CLI 批准后 `claim` 才会成功：

```bash
python -m app.cli pair-approve 123456 --name "我的 Pixel"
```

未批准时 `claim` 返回 `400 pair_not_ready`。配对码一次性。`pair/start` 与 `pair/claim`
都按来源 IP 限速（默认 20 次/小时）。

### 3.2 桌面与调用

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/v1/devices` | 列出所有**曾经连过的桌面**：`{id, name, online, platform, harnessVersion, capabilities[], lastSeen, pendingRequests}` |
| `GET` | `/api/v1/devices/{id}/sessions` | `session.list` 的便捷封装 |
| `POST` | `/api/v1/devices/{id}/op` | `{op, args}` → `{ok:true, value}` / `{ok:false, error}` |
| `POST` | `/api/v1/devices/{id}/stream` | `{op, args}` → **SSE** 流（见 §3.3） |

`{id}` 是**桌面的 `deviceId`**，不是手机设备 id。从未连过的 id 返回 `404 unknown_device`；
连过但当前离线返回 `503 device_offline`。`/op` 与 `/stream` 按手机设备限速（默认 240 次/分钟）。

### 3.3 SSE 流

`POST /api/v1/devices/{id}/stream`，`Content-Type: text/event-stream`：

```
event: open
data: {"op":"session.follow","streamId":"s-1a2b3c"}

event: chunk
data: {"type":"snapshot","cursor":0,...}

event: end
data: {"dropped":0}
```

失败时以 `event: error` 结束：`data: {"code":"timeout","message":"..."}`。
流在打开后才返回响应，因此「设备离线」「op 不允许」会以普通 HTTP 错误返回，而不是 200 + error 事件。
客户端断开时中继会向插件发送 `cancel`。

### 3.4 事件流

```
WS /api/v1/events?token=<device-token>
```

客户端发送：

```json
{"t":"sub","id":"s1","deviceId":"dev-home","topics":["session.event"],"args":{"sessionIds":["..."]}}
{"t":"unsub","id":"s1"}
{"t":"ping","id":"p1"}
```

服务端推送：

```json
{"t":"ready","phoneId":"phone-1a2b3c4d5e","protocol":1}
{"t":"ack","id":"s1","ok":true}
{"t":"error","id":"s1","error":{"code":"device_offline","message":"..."}}
{"t":"evt","subId":"s1","deviceId":"dev-home","topic":"session.event","payload":{...}}
{"t":"evt","subId":null,"deviceId":"dev-home","topic":"device.status","payload":{"connected":true}}
{"t":"pong","id":"p1"}
```

订阅登记在**服务端**；断线重连后客户端必须重新发送 `sub`。
每个手机订阅对应一条独立的下发插件订阅，`id` 为 `<phoneId>:<subId>`（避免不同手机用同名
`subId` 时在插件侧冲突）。同一设备的事件会扇出给每个匹配的订阅。
中继只在一台手机真正订阅时才向插件下发 `sub`；无订阅时不产生任何上行流量。

> **客户端必须容忍随时到达的无关帧。** 中继会在任意时刻广播设备级事件
> （`device.status`，其 `subId` 为 `null`），例如某台桌面刚刚接入时。
> 客户端**不能**假设「发出 `sub` 后收到的第一帧就是该订阅的 `ack`」，
> 而应按 `t` 与 `id` 分发帧。`scripts/verify_remote.py` 里的 `recv_until` 就是这种写法。

### 3.5 审批

| 方法 | 路径 | 请求 |
|---|---|---|
| `POST` | `/api/v1/approvals/{askId}` | `{decision:"approved"\|"denied"\|"cancelled", answers?}` |

`answers` 用于 `question.ask`，形状为 `[{id, selected[], custom?}]`（对应
`AskUserQuestionAnswerItem`）。未知、已过期、已使用或其设备离线的 `askId` → `404 unknown_ask`。

决策映射（插件侧）：

| 手机决策 | `approval/request` 返回 | `user-questions/request` 返回 |
|---|---|---|
| `approved` | `allowed-once` | `{answers}` |
| `denied` | `rejected` | `{answers}` |
| `cancelled` | `cancelled` | 委托 `next()` |
| 超时 / 链路断开 / 未订阅 | 委托 `next()` | 委托 `next()` |

**插件在超时或任何异常路径上一律委托 `next()`，回落到桌面原有应答器。**
v1 不提供任何形式的自动批准。

### 3.6 健康检查

`GET /healthz`（无需鉴权）→

```json
{"status":"ok","version":"0.1.0","protocol":1,"uptimeSeconds":12,
 "devices":{"total":1,"online":1},"phones":1,"pendingAsks":0,"phoneConnections":0}
```

`GET /` 返回服务描述；`/docs` 是 FastAPI 自动生成的 OpenAPI 文档。

---

## 4. 版本与兼容

- `PROTOCOL_VERSION = 1`。
- 中继拒绝 `hello.v != 1` 的连接（`bye` + `4400`）。
- 新增**可选**字段不升版本；新增必填字段、改变语义或移除字段必须升版本。
- `capabilities` 允许中继向旧插件优雅降级：缺失的能力对应接口返回 `501 op_not_supported`。
