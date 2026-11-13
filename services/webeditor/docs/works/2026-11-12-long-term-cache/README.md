# 2026-11-12 长期缓存：刷新页签／重启浏览器不再重传已下载的字节（Q162）

## 原因

用户口径：「为前端页面添加长期缓存，单纯的刷新页签或重启浏览器不再传输已经下载或的内容。我希望能通过这种方式加快第二次及以后加载模型的速度。此外，OLS的元数据也加入到缓存内容。你可能需要给OLS添加相关接口以便于浏览器和OLS服务比对某个资产是否有更改。」

选型时用户明确选了最重的一档并加了一条硬约束：「新增版本接口+全量immutable  注意此变更只影响OLS和前端的加载，不要影响到AgentBrain，brain启动前会先自己构建一次几何缓存」。

**病灶是 OLS 一个缓存头都不发**：`git show HEAD:server.py` 里 `Cache-Control`／`ETag` 出现 **0** 次。没有 `Cache-Control` 的响应在 Chrome 里只走启发式 freshness，且普通刷新（F5）会对每一条重新协商；没有 `ETag` 就连协商都退化成整份重下。于是每次刷新页签都把整份世界重新拉一遍 —— 实测 `docs/ur5e_table_empty.json` 这一份是 **73 个文件 / 93,329,912 字节**（其中 `robot 3 / d435i.obj` 一份就 48.27 MB）。

会话内那层内存缓存（`LibraryClient` 的三张 Map、renderer 的 `<kind>:<id>` 三张缓存，Q53/Q54）只在**同一页面生命**里有效，刷新即清空，所以它救不了这个场景。

## 做了什么

### 1. 规格就地改（`docs/Web前端需求规格v3.md`，Q162）

- **§6.7 整节重写**：从"会话内一层"改成"**会话内内存 + 跨刷新／跨重启的 HTTP 长期缓存**两层"，写死版本号算法、两条出口、`v` 决定缓存强度、三条 oracle 路由恒 `no-cache`、所有 GET 带 `ETag`、前端落点、fail-open、对 AgentBrain 无影响、以及实测到的 Chrome 单条上限。
- **§6.2 / §6.3**：两条元数据读与 `/file` 都带 `v=<资产版本号>`；取不到版本就退回 `no-cache`+ETag。
- **§6.9**：澄清 `If-None-Match` 是**浏览器网络层自己补的**协商头、不是脚本写进 `fetch` 的 header，预检发生在补它之前 ⇒ 不违反"只用简单 GET"。**顺带更正一句旧措辞**：真实 OLS **已有** `do_OPTIONS`（`server.py`，204 + `Access-Control-Max-Age: 86400`），旧文里"OLS 无 OPTIONS 处理"是错的；`Access-Control-Allow-Headers` 仍只列 `Content-Type`，本轮两处都不动。
- **§6.11**：`/list` 条目追加 `version`；新增 `/assetVersion` 条目；路由计数 **七条 → 八条**，并写明新路由在 **OLS 那一侧**、不在 §5.9 的 gateway 清单里 ⇒ `editor/vite.config.ts` 代理表与 `tools/audit_parity.py` 探针**不增不减**（坑 3 只管 gateway 根路径）。
- **§4.6 缩略图**：`<img src>` 也带 `v`，版本号由 `client.thumbnailUrl` 从表里同步取，`main.ts:assetRow` **一字不改**。
- **新增 §12.45**：七条验收判据（条件请求、`v` 的两支、版本号算法与一致性、前端 URL 形状、fail-open、AgentBrain 无影响、真机取证）。

### 2. OLS（`ObjectLibraryServer`，同级仓库、另一次提交）

- `server.py:asset_version(data_dir, typ, row)`：`rglob` 资产目录下全部文件，取 `(相对路径, 字节数, mtime_ns)` 排序，连同索引行的 `name`/`category` 一起 SHA-256，取前 16 位十六进制。**只 stat、不读文件内容**（出厂库 417 MB）。**不写进任何磁盘索引**。
- `_send_json` / `_send_file` 都带 `ETag`（JSON 按响应体哈希，文件按 `(size, mtime_ns)`）与 `Cache-Control`；`_etag_matches` 命中即 `_send_not_modified` ⇒ **304、空体、仍带 CORS 头、不带 `Content-Length`**。文件那一路在 304 时**连读盘都省掉**。
- `_dispatch` 每次请求重算 `self.cache_version`（**放在未知路由的早退之前** —— `BaseHTTPRequestHandler` 的实例在 keep-alive 上跨请求复用，晚设会让上一条请求的 `v` 漏给下一条）；`ORACLE_ROUTES = ("/list", "/listCategory", "/assetVersion")` 恒不吃 `v`。
- `api_assetVersion` 新 handler + `ROUTES_GET` 新条目；`api_list` 每个 item 追加 `version`。
- `README.md` 新增「浏览器长期缓存（版本号 + ETag）」一节与 `/assetVersion` 示例。**`docs/OLS接口.md` 不动**（用户维护的输入）。

### 3. 前端（`editor/src/library.ts`，唯一改动的生产文件）

- `LibraryItem` 追加可选 `version`；`LibraryClient` 多一张 `versions: Map<"<kind>:<id>", string>`。
- `versionQuery(kind, id)` **同步**拼 `&v=`；`fileUrl()`（仅 OLS 分支）与 `thumbnailUrl()` 用它 —— 两个调用点 `ModelFiles.resolve` 与 `<img src>` 都是同步的，版本必须先就位。
- `ensureVersion(kind, id)`：表里没有才问一次 `/assetVersion`；`model()`/`robot()`/`scene()` 先 await 它再带 `v` 读文档。
- `list()` 用 items 的 `version` **播种**该表（于是面板那 60 条详情与缩略图一个版本请求都不发），并把 `version` 透传进 `LibraryItem`。
- `invalidateMetadata()` **同时清版本表**。
- **fail-open**：路由缺失／404／500／没有 `version` 键／非字符串 ⇒ 表里不写 ⇒ URL 与 Q162 之前逐字相同。COS 分支不拼 `v`。

## 不做

- **不改 `main.ts`**：缩略图版本号走客户端那张表，`assetRow` 的签名与 DOM 一字不动。
- **不动 renderer 的 `models`/`robots`/`templates` 三张缓存**：同一页面生命内换资产仍要重进页面（与 Q162 之前一致）。
- **不做 Service Worker / Cache Storage / IndexedDB**：浏览器自带的 HTTP 缓存已经够，自建持久缓存是第二套开关。
- **不加 `Last-Modified`**：`ETag` 一套就够，第二套机制覆盖同一层。
- **不加常量键／功能键／`RuntimeConfig` 字段**：`max-age` 是 OLS 侧的模块常量，浏览器无从配置。
- **不动 `Access-Control-Allow-Headers`、不动 `do_OPTIONS`**：`If-None-Match` 不触发预检，无需放开。
- **不动 `types.ts` / `contracts/types.ts`**：`LibraryItem` 住在 `library.ts`，本轮没有协议字段变化（两份副本仍逐字节相同）。
- **不动 vite 代理表与 parity 探针**：新路由在 OLS 侧，浏览器直连。
- **不校验 `v` 的内容**：校验就要为每次文件请求走一遍目录统计；`v` 只是缓存键，写错的键此后永远不会再被问到。
- **不治那份 48 MB 的相机网格**：减面／换格式是资产侧的事，不在本轮。
- **不动 AgentBrain 的任何文件**。

## 验证

### 静态与单测

| 命令 | 结果 |
|---|---|
| `npx tsc -p editor/tsconfig.json --noEmit` | 0 错 |
| `npx vitest run --config editor/vitest.config.ts` | **24 文件 / 741 例全绿**（改前 729，+12） |
| `npx vite build --config editor/vite.config.ts` | JS **907.77 → 908.54 kB**（+0.77 kB，gzip 250.75 → 250.95 kB，+0.20 kB）；CSS 21.99 kB 不变 |
| OLS `python -X utf8 -u -m unittest discover -s tests -p test_api.py` | **62 例全绿**（改前 50，+12） |
| OLS `-p test_hooks.py` / `-p test_version.py` | 10 例 / 7 例全绿 |

改到的既有用例各一条，都是**契约变了**而不是回归：OLS `test_list_model`（`/list` 条目多了 `version`）、前端 `loads a robot from its own route…`（多一次 `/assetVersion` 探问）。

### 负控制（逐条改坏一行 → 只跑该文件 → 还原 → 校验和复核）

OLS 那 9 条在**临时目录的副本**上跑（`server.py` + `init_index.py` + `tests/test_api.py` 拷进 temp），**活文件一个字节都没动**；前端那 7 条原地改、`finally` 还原，`sha256` 复核一致。

| 改坏的那一行 | 变红的用例（恰好这些） |
|---|---|
| OLS `_etag_matches` 恒 False | 3 条：JSON 往返、文件/缩略图往返、304 保留缓存强度 |
| OLS oracle 路由也吃 `v` | 1 条：`test_oracle_routes_never_go_immutable` |
| OLS `v` 不再变 immutable | 2 条：`test_v_decides_cache_strength`、304 那条 |
| OLS 版本号不算 mtime | 1 条：`test_version_tracks_files_and_index_row` |
| OLS 版本号改读文件内容 | 1 条：`test_version_is_stat_only` |
| OLS 版本号不算索引行 | 1 条：`test_version_tracks_files_and_index_row` |
| OLS `/list` 不给 `version` | 2 条：`test_assetVersion_agrees_with_list`、`test_list_model` |
| OLS 摘掉 `/assetVersion` 路由 | 4 条 |
| OLS 改成校验 `v` | 1 条：`test_v_is_a_cache_key_not_a_credential` |
| 前端 `versionQuery` 恒空 | 5 条 |
| 前端 `list()` 不播种 | 1 条 |
| 前端 `invalidateMetadata` 不清版本表 | 1 条 |
| 前端 `ensureVersion` 不发请求 | 5 条（含既有 robot 那条） |
| 前端 COS 分支也拼 `v` | 1 条 |
| 前端版本号不校验类型 | 2 条（no version key / non-string） |
| 前端 `scene()` 跳过取版本 | 1 条 |

### 真浏览器（本轮跑了）

临时栈：OLS 在 **8001**（`--config` 指向真实 `data`，只读）、`tools/serve_verification.py --port 8098 --library-url http://127.0.0.1:8001` 服务 `dist/`；playwright 用**同一个 user-data-dir** 启两次**完整浏览器进程**（第一次前把 profile 删干净），中间 `context.close()`。用户自己的 8000/8080/8099/8500 全程没起、没碰。

**A. 去掉那台 48 MB 相机的世界**（`docs/ur5e_table_empty.json` 的临时副本，6 个物体）：

| | 冷启动 | 重启后 |
|---|---|---|
| `/file` 请求 | 70 | 70（**全部 fromDiskCache**） |
| `/file` 传输字节 | 45,053,784 | **0** |
| OLS 服务端 `/file` 命中 | 70 | **0** |
| 元数据文档（41 model + 12 robot + 7 scene） | 全部下载 | **全部 fromDiskCache，0 字节** |
| 真正打到服务端的请求 | 140 | **10**（4 `assetVersion` + 3 `list` + 3 `listCategory`，共 2,930 字节，多数是 304） |
| 加载耗时 | 41.95 s | **16.95 s**（−60%，2.47×） |

**B. 完整世界**（8 个物体，含两台相机）：冷 73 文件 / 93,329,912 字节 / 53.67 s；重启后 **72/73 零请求**、只重传 **48,274,465 字节**、37.68 s。

**唯一那份没入缓存的是 `robot 3 / d435i.obj`（48,274,097 字节）**。同一次运行里最大的入缓存条目是 7,313,211 字节的 `tabletop.png`；把 `--disk-cache-size` 提到 1 GB 重跑，结果一字不变。这是 **Chrome 磁盘缓存对单个响应的容量上限**（约总容量 1/8），不是本轮口径的漏洞 —— 服务端与前端都越不过它，要治只能把那份网格本身变小。已写进 §6.7 与坑 135。

渲染正确性：两次启动都截了图，臂／夹爪／桌子／支架形状与位置一致，重启那次控制台**零错误**（冷启动那条 503 是没起语音服务，与本轮无关）。

### 取证脚本的一个坑（写进坑 135）

第一版 harness 在 `goto(waitUntil:"load")` 之后**立刻** `setInputFiles`，冷启动能过、**缓存热了以后反而不过** —— JS/CSS 从磁盘缓存秒回，`load` 事件比 `start()` 里那串 async 更早，文件选择落在监听器接上之前，于是"世界加载了、几何一个字节没取"。改成先等 `#status-primary` 出现 `Connected` 再喂文件就稳定了。**这类 harness 的等待条件不能只看 `load`**。

## 进度

- [x] 规格 §6.7 / §6.2 / §6.3 / §6.9 / §6.11 / §4.6 就地改 + 新增 §12.45
- [x] OLS：版本号、`/assetVersion`、`ETag`/304、`v` → immutable、README
- [x] OLS 12 条新用例 + 9 条负控制
- [x] 前端：版本表、同步拼 `v`、`list` 播种、`invalidateMetadata` 清表、fail-open
- [x] 前端 12 条新用例 + 7 条负控制
- [x] 真浏览器两次完整启动取证（A/B 两份世界）
- [x] 本卡 + INDEX 顶行 + 坑清单 135
- [x] 两仓各一次本地提交（**不 push**）
- [x] 本轮起的 8001 / 8098 已停，端口复核为 free

**待用户确认**：那份 48 MB 的 `d435i.obj` 要不要单独开一轮减面（它现在是"第二次加载"里唯一还要重传的东西，占完整世界剩余耗时的绝大部分）。