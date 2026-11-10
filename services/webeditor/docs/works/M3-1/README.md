# M3-1 —— `updateWorld` / `updateItem` / `addObject` / `removeObject`

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | M1-*,M2-* |

## 原因与做了什么（PLAN_STATE 备注）

gateway 新增 4 个 handler 并在 `create_app` 里紧随 `/getWorld` 注册（在通配 static 之前，坑 2）：`update_world/update_item/add_object/remove_object` 全部只做"参数校验 + §11.1 尺寸校验 + `dispatch()`"，不解析任何场景语义。校验前置（决议 Q26）：`_uint_query()` 用 `raw.isascii() and raw.isdigit()`（单用 `isdigit()` 会放过 `"²"` 这类非 ASCII 数字而 `int()` 随后抛错）并卡 `0xFFFFFFFF` 上界，`_bool_query()` 认 `1/true/yes/on`/`0/false/no/off`，`_json_body()` 先按 `content_length` 挡 413 `PAYLOAD_TOO_LARGE` 再要求 `Content-Type` 恰为 `application/json`（§11.2）、body 必须是对象；`updateItem` 的 query `id` 与 `body.object_id_in_scene` 不一致就地 `400 BODY_ID_MISMATCH`。校验失败绝不下发浏览器（不消耗 §5.7 挂起槽位），`tests/test_caller_apis.py` 用 `assert_no_frame()` 做实质断言而不是只看状态码。写入后的广播（决议 Q25）：`dispatch()` 拆出 `dispatch_with_source() -> WriteOutcome{payload,request_id,browser_id}`，`_apply_browser_response()` 结算成功时把应答者记入 `self._responders`，`_write()` 据此广播 `scene.applied{scene_version,action,source_request_id,source_browser_id}` 并同步 `self.scene_version`；契约 `SceneApplied` 增可选 `source_browser_id: string\

## 任务卡原文

#### M3-1 `updateWorld` / `updateItem` / `addObject` / `removeObject`
- **规格**：§4.2、§5.9、§5.14、§5.5、§5.7、§11.2
- **涉及**：`local_gateway.py`（新增 4 个 handler + 注册、`dispatch_with_source`）、新建 `editor/src/scene_requests.ts`、`main.ts`（WS `onmessage` 分派 + `adoptWorld`）、`world.ts` 的 mutators、`contracts/types.ts`+根 `types.ts`（`SceneApplied.source_browser_id`）、`editor/vite.config.ts`（dev 代理清单）
- **改动要点**：
  - gateway：4 个 handler 全部只做 **参数校验 + 尺寸校验(≤100 MB) + `dispatch()`**，绝不解析场景语义；错误码严格按 §5.13。
  - 前端：`browser.request` 分支按 action 分派到 `applyWorld` / `updateItem` / `addObject` / `removeObject`，执行后回 `browser.response`，并广播/接收 `scene.applied`，按 `source_request_id` 去重（§5.4）。**落地时拆成独立模块 `editor/src/scene_requests.ts`（`applySceneRequest()` + `isWriteAction()`），不是卡片原写的 `applyWorldRequest()`**，理由与细节见规格 §13.5 **Q27**。
  - `updateItem` = **components 全量替换**（§4.2）；`addObject` id 省略时按 §5.5 水位分配；`removeObject` 默认级联 + `detach_children` 变体（§5.14；级联收集可参考 `local_gateway.py:170-175` 被删逻辑的等价 TS 实现）。
  - `expected_version` 不匹配 → `409 VERSION_CONFLICT`，不应用。
  - 多端同步（§5.4）：广播带 `source_browser_id`，其他浏览器**定向回源** `GET /getWorld?browser_id=<它>` 采纳权威世界，**Q25**；参数校验一律先于下发，**Q26**。
- **DoD**：`curl` 四条命令（见 §6 速查）在浏览器打开时全部返回 `{"ok":true,...}` 且画面同步；无浏览器时 `503`。
- **验证**：`bash`/`curl` 序列（§6），以及 `tests/test_caller_apis.py`(新建 20 例)、`editor/src/scene_requests.test.ts`(新建 17 例)。
- **已完成（2026-10-09）**：状态与长注见 §9 M3-1 行；dev 环境需在 `editor/vite.config.ts` 的 `callerRoutes` 补路由（坑 3、**Q28**）。
