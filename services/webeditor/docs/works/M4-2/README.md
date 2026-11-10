# M4-2 —— `POST /updateTraj` 与二进制分帧

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | M2-1,M4-1 |

## 原因与做了什么（PLAN_STATE 备注）

打通 §5.3 的两端。gateway 侧：`_bytes_body()` 与 JSON 侧的 `_json_body()`（Q26）对称，顺序固定为 `content_length` 声明 → 413、`Content-Type` 必须恰为 `application/octet-stream` → 400 `INVALID_PARAM`、实际读到的字节数 → 413、空 body → 400，不解析内容（TRAJ 语义一律由浏览器的解码器产生，见 Q36）；`update_traj()` 注册在 `/removeObject` 之后、通配静态兜底之前，走 `dispatch("updateTraj", {}, binary=body)` 而不是 `dispatch_with_source()`，把浏览器回报的 §5.12 载荷原样 `web.json_response`。`dispatch()` 的二进制发送侧（`payload.byte_length` + `expects_binary` + 紧随 `send_bytes`）M2-1 已就位，本卡未改。浏览器侧：`connection.binaryType="arraybuffer"`，`onmessage` 按 `typeof data === "string"` 分流到 `handleGatewayFrame()`/`acceptBinaryPayload()`；§5.3 步骤 3 的"第一个 binary 帧配对最近的 `expects_binary` 请求"用单槽 `pendingBinary = {connection, request}` 实现，`answerBrowserRequest()` 见到 `expects_binary === true` 只登记不应答（应答权在 `acceptBinaryPayload` 里），无登记时到达的 binary 帧 `console.warn` 丢弃，`onclose` 清槽防跨连接串帧；成功路径与文本路径共用 `reply()`，`isWriteAction()` 为假所以 `updateTraj` 不 `syncScene`、不发 `state_sync`（Q33）。长度校验放浏览器（Q34）：只有它同时看见声明值与实到字节，`declared !== buffer.byteLength` → `WorldError("GATEWAY_FRAMING_ERROR", …, 502)`，gateway 的 §5.13 表把它转成 502 且回填 `request_id`；gateway 若自比等于拿自己写的数校验自己。§5.12 响应由 `applyTrajRequest(world, request, buffer)` 装配：五个数字取头部声明值（`duration_seconds = totalFrames / fps`），`ignored_objects` 用 `object_id` 对当前世界求差（§8.2，逐条 `{object_id, reason:"OBJECT_NOT_IN_SCENE"}`）、`warnings` 逐条 `ATTACH_PARENT_MISSING@frame=<f>`（§8.6，只报 `parentId !== null` 且父不在场景的），两套数字故意不同源（Q35）；解码产物存入新增的 `state.traj`，播放循环归 M4-4。测试：新建 `tests/test_traj_api.py` 13 例（分帧：text 帧 `payload == {"byte_length": 12088}` + `expects_binary` + 紧随的 binary 帧字节相等、不发第二个 binary 帧；三浏览器扇出同一 `request_id` 且先应答者胜出、迟到帧只 warn；无 `scene.applied` 广播且 `scene_version` 不动、`?expected_version=3` 被忽略；`GATEWAY_FRAMING_ERROR` → 502 带 `request_id`、`TRAJ_TRUNCATED` → 400 保留浏览器码；静默浏览器两次 504 且 `request_id` 不同；无浏览器 503；四条入口门槛；`create_app()` 真路由不回 SPA 壳）；`scene_requests.test.ts` 新增 `applyTrajRequest` 6 例（读真实夹具断言 §5.12 全形状 + `segments` 长 9、不碰世界 version/objectCount、部分 id 缺失、与场景零交集时三条 ignored + 两条 warning、长度不符与缺 `byte_length`、截断 buffer），并把 `updateTraj` 从"本 build 无 handler"的 `INTERNAL_ERROR` 用例里移出（该用例只留 `captureScene`/`get2DAABB`）。验证：`python -X utf8 -m unittest tests.test_traj_api` 13 例 OK、五模块合计 70 例 OK、`npx tsc -p editor/tsconfig.json --noEmit` 0 错、`npx vitest run --config editor/vitest.config.ts` 147 例 / 8 文件、`npm run build` 通过（827 KB）。真机冒烟（OLS stub 8000 + gateway 8080 + vite 5173 + 页面，用完即关、临时文件已删）：`curl -H 'Content-Type: application/json' --data-binary @docs/scene_example.json /updateWorld` → `{"ok":true,"scene_id":1,"scene_version":2,"object_count":5}`，`/getWorld` 回读 id 集合 `[0,1,2,3,5]`；`curl -H 'Content-Type: application/octet-stream' --data-binary @tests/fixtures/robot_grasp.traj /updateTraj` → `{"ok":true,"fps":60,"total_frames":720,"duration_seconds":12,"object_count":3,"attach_count":2,"ignored_objects":[],"warnings":[]}`（数字全部来自浏览器解码，证明二进制链路真的走通）；`/getWorld` 的 `scene_version` 仍为 2（轨迹不是场景变更）、gateway 日志 0 条 WARNING、控制台只有两条来自下面负例的 warn；`head -c 300` 截断文件 → 400 `{"error":{"code":"TRAJ_TRUNCATED","message":"trajectory ends before segment 0 of object 1 (1960 bytes from 328)","request_id":"…"}}`；同一文件换 `Content-Type: application/json` → 400 `INVALID_PARAM` 且无 `request_id`（证明校验先于下发）。三个踩过的坑（已写进测试）：坑 A `max_payload_bytes` 若按 §11.1 的反向直觉设成 4 KB 之类小于夹具的值，`/updateTraj` 会就地 413 而测试还在 `socket.receive()` 上等 `browser.request`，表现为整模块挂死（首轮就是这样）——现按 `MAX_BODY = 16384` 统一，截断阈值由常量派生；坑 B aiohttp 客户端对裸 `bytes` 自动补 `Content-Type: application/octet-stream`，"缺 Content-Type"的负例必须 `skip_auto_headers=["Content-Type"]` 才测得到；坑 C `PendingRequests` 没有 `__len__`，证明"超时归还槽位"改用 `PendingRequests(1)`（第二个请求若拿到 429 就是泄漏）而非窥探私有字段，与 `test_ws_protocol.py:86` 同法；另把 `next_request`/`next_binary` 统一包 `asyncio.wait_for(..., 5)`，任何分帧回归以后都是 5 秒失败而不是无限挂起

## 任务卡原文

#### M4-2 `POST /updateTraj` 与二进制分帧
- **规格**：§5.3、§5.12、§11.1
- **涉及**：`local_gateway.py`（新 handler + `WSMsgType.BINARY` 发送）、`main.ts:connectWebSocket`（`socket.binaryType="arraybuffer"` + binary 配对）
- **改动要点**：text 帧带 `byte_length`/`expects_binary`，紧随**一个** binary 帧；长度不符 → `502 GATEWAY_FRAMING_ERROR`；`Content-Type` 必须 `application/octet-stream`；响应体按 §5.12（`fps/total_frames/duration_seconds/object_count/attach_count/ignored_objects/warnings`）。
- **DoD**：`curl --data-binary @tests/fixtures/robot_grasp.traj localhost:8080/updateTraj` 返回 §5.12 JSON；截断文件返回 `TRAJ_TRUNCATED`。
- **验证**：`python -X utf8 -m unittest tests.test_traj_api -v`（新建）。
- **已完成（2026-10-09）**：`dispatch(binary=…)` 的发送侧在 M2-1 就写好了，本卡补的是**路由 + 浏览器侧的收侧**。三点与卡片原写不同，已进 §13.7：**Q33** `updateTraj` 不广播 `scene.applied`、不接受 `expected_version`（轨迹不改世界）；**Q34** §5.3 的长度校验落在浏览器（gateway 是声明值的写入方，无从自检），配对用单槽 `pendingBinary`；**Q36** `_bytes_body()` 的四条门槛全部先于 `dispatch()`。**2026-10-19 本卡的线格式被 Q99 就地改判**：`Content-Type` 改 `multipart/form-data`、每条轨迹一个 `trajs` part、可选 `?count=`；§5.12 响应换成 `trajs` 数组（平铺 8 字段消失）；binary **仍是一帧**（gateway 拼接、浏览器按 `byte_lengths` 切片）。现行口径看 §5.3/§5.12 与 §13.28，实现看 FEAT-2026-10-19 卡。状态与长注见 §9 M4-2 行。
