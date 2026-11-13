# 超大几何走 CacheStorage（Q164）

- **决议号 / 落点**：Q164，规格 §6.7（就地更正 Q162 的两句话）、§12.45 ⑦（补一句指向）、新增 §12.47。
- **接口面**：无。不新增路由、常量键、功能键；`types.ts` / `contracts/types.ts` 一字未动；OLS、gateway、AgentBrain 全不动。
- **改动文件**：`editor/src/library.ts`、`editor/src/library.test.ts`、`docs/Web前端需求规格v3.md`、本卡、`docs/works/INDEX.md`、`docs/works/坑清单.md`。

## 原因

Q162 把刷新／重启后的重传压到了零，**只剩一份例外**：`robot 3 / d435i.obj`，48,274,097 字节。Chrome 的磁盘缓存不收超过其容量约 1/8 的单个响应，`--disk-cache-size=1GB` 也不改变它（坑 135 ①）。Q162 的结论写的是"服务端与前端都越不过这条上限"。

用户问：能不能抬阈值，或者把大文件拆成 `cache.1 / cache.2` 存？

- **抬阈值不行**：那是浏览器启动参数，前端无权要求用户这么开 Chrome；而且实测它根本不动这条边界。
- **拆分是死路**：Chrome 现在默认的 Simple Cache 后端**不支持稀疏条目**，`Range` 拿回来的 **206 响应根本不落盘**。要落盘就得是各自独立 URL 的 200（`/filePart?...&part=n`），OLS 要加路由、前端要加拼接，6 个请求换 1 个。
- **第三条路**：`CacheStorage` 是**显式存储**，没有单条上限，配额是 origin 级的。OLS 一行都不用改。

先做了一次最小可行性验证（真实那份 48 MB obj，静态服务 + 计数，两次独立浏览器进程共用同一 user-data-dir）：

| 轮次 | 结果 | 服务端该文件传输 |
|---|---|---|
| cold | fetch 855.5 ms + `caches.put` **214.1 ms**，落盘 48,274,688 B，quota 10.74 GB | 1 次 / 48,274,097 B |
| warm（新进程、同 profile） | `match` 96.8 ms + 读 59.9 ms = **156.7 ms**，SHA-256 逐字相同 | **0 次 / 0 B** |
| 负控制（先 `caches.delete` 再普通 fetch） | 200 全量、**2446.2 ms** | 2 次 / 48,274,097 B |

负控制那一行是关键：删掉 CacheStorage 后 HTTP 磁盘缓存**接不住**它，所以 warm 的零请求确实来自 CacheStorage。用户看过这组数字后要求"在前端添加 CacheStorage"。

## 做了什么

`editor/src/library.ts`：

1. **`CACHE_STORAGE_MIN_BYTES = 8 MiB`**：只有**来自网络**且 ≥ 这个字节数的响应才写进 CacheStorage。小的仍由 Q162 的 `v` + `immutable` 覆盖，两层不重叠。
2. **`ByteStore` 接口 + `cacheStorageBytes()`**：把用到的那一小片 CacheStorage surface 收成结构类型（`match`/`put`/`keys`/`delete`），真实 `caches` 结构上可赋值，测试注入假实现**不需要任何 `as`**。`caches` 不存在（非 secure context）⇒ 返回 `undefined`，整层退出。
3. **键就是带 `v` 的那条 URL**；写入时按"去掉 `v` 的 URL"比对，删掉**同一资产其它版本**的旧条目，别的资产一条不动。
4. **没有 `v` 就不写**（老 OLS、`is_on_cos`）：无版本的键无从失效。
5. **字节层按 URL 去重**：`fileText` 与 `fileBytes` 共用 `binaryCache`，文本只是字节的 UTF-8 解码视图。同一个 OBJ 本来被 `placement.ts`（量包围盒／预处理）和 `bundle` 各读一次，不合并就会取两遍、也可能写两遍。原来的 `read()` 辅助函数随之删除。
6. **三处 fail-open**：无 `caches`、`get` 抛错（当作未命中）、`put` 抛错（字节照常返回）。
7. **`invalidateMetadata()` 不清 CacheStorage**：它清的是版本表；资产没变则 `v` 不变、那份字节仍然有效。

规格：§6.7 里"**服务端与前端都无法越过这条上限**"改成"**HTTP 缓存这条路**越不过"，并新增 Q164 那一整段；"不做 Service Worker / **Cache Storage** / IndexedDB"里的 Cache Storage 摘掉（历史留在本卡）；§12.45 ⑦ 的例外补一句指向 §12.47；新增 §12.47 ①–⑧。

## 不做

- 不动 OLS、gateway、AgentBrain、`contracts/types.ts`。
- 不加 `constant_config.json` 键或 `features.json` 键：8 MiB 是**浏览器实现边界推出来的门槛**，不是给用户调的旋钮，也不是可远程开关的 UI 能力。
- 不做 Service Worker、不做 IndexedDB、不做通用离线层；CacheStorage 只装超阈值的文件字节。
- 不做 `Range`/分片（206 不落盘，见上）。
- 不申请 `navigator.storage.persist()`：默认 best-effort 已够，淘汰了就走 fail-open。
- **不动那份 48 MB 网格本身**（减面／换格式）——用户明确交给同事。省掉的是传输，**解析那 1.8–2.3 s 一分没少**。
- 不动 renderer 的 `models`/`robots`/`templates` 三张缓存（与 Q162 一致）。

## 验证

| 命令 | 结果 |
|---|---|
| `npx tsc -p editor/tsconfig.json --noEmit` | 0 error |
| `npx vitest run --config editor/vitest.config.ts editor/src/library.test.ts` | **52 → 63**，全绿，452 ms |
| `npx vitest run --config editor/vitest.config.ts` | **24 文件 / 741 → 752**，全绿 |
| `npx vite build --config editor/vite.config.ts` | JS **908.54 → 909.37 kB**（+0.83 kB，gzip 250.95 → 251.28 kB，+0.33 kB）；CSS 21.99 kB 不变 |

### 负控制（9 条，全部在临时改动上做，逐条只红预期用例）

| 改的那一行 | 恰好红 |
|---|---|
| 去掉 `if (stored) return stored` | 3 条（命中零请求、跨会话读回、invalidate 后仍命中） |
| 去掉字节数门槛（小的也写） | 1 条（差一字节不写） |
| 门槛 `<` 改 `<=`（边界差一） | 3 条（门槛那条、只读一次、只留最新） |
| 去掉"无 `v` 不写" | 1 条 |
| 关掉剪旧分支 | 1 条（只留最新版本） |
| `fileText` 自己发请求、不走字节层 | 1 条（只读一次） |
| `storeGet` 不吞异常 | 1 条（读抛错当未命中） |
| `storePut` 不吞异常 | 1 条（写失败仍返回字节） |
| `cacheStorageBytes` 不再对缺失 storage 返回 undefined | 1 条（无 caches 时整层退出） |

九条全部 `matched=True, exact=True`；`library.ts` 还原后 sha256 与改动前逐字相同（`dae55ceff84fe900b566a81dbecca42747ca94fd970119fe008d755230da0a0d`）。

### 真浏览器（本轮跑了）

临时栈：OLS 在 **8001**（`--data-dir` 指向真实 `data`）、`tools/serve_verification.py --port 8098 --library-url http://127.0.0.1:8001` 服务 `dist/`；playwright 用**同一个 user-data-dir** 启三次**完整浏览器进程**（第一次前把 profile 删干净）。世界是 `docs/ur5e_table_empty.json` 原件（8 个物体、含两台 robot 3 相机），只读不改。用户自己的 8000/8080/8099/8500 全程没起、没碰。

| | 冷启动 | 重启后 | 负控制（先删存储条目再重启） |
|---|---|---|---|
| `/file` 网络事件 | 73 | 72（**全部 fromDiskCache**） | 73（72 fromDiskCache + 1 真下载） |
| `/file` 传输字节 | 93,329,912 | **0** | 48,274,465 |
| `d435i.obj` 请求 | 1 | **0**（连网络事件都没有） | **1** |
| OLS 服务端 `/file` 命中 | 73 | **0** | 1 |
| 全部请求传输字节 | 94,438,997 | **17,821** | 48,292,287 |
| CacheStorage 条目 | 1 条 / 48,274,097 B | 1 条 / 48,274,097 B | 删掉 1 条，加载后重新写回 1 条 |
| 加载耗时（网络静默判据） | 32.76 s | **18.56 s**（−43%） | 16.42 s |

服务端日志合计 `/file` 74 次、其中 `d435i.obj` **2 次**（冷启动 + 负控制），重启那次一次都没有 —— 与浏览器侧读数互相印证。

**耗时口径要说清**：这三个数是"最后一个网络事件之后静默 6 s"的时刻，**不含之后的解析尾巴**，所以负控制那 16.42 s 比 warm 的 18.56 s 还短并不矛盾（它更早停止发请求）。可比的是同一判据下的冷/热：32.76 → 18.56 s。Q162 同判据下重启是 37.68 s（那时这份 obj 还要重传）。

## 进度

- [x] 可行性验证（含负控制）并取得用户确认
- [x] 规格就地更正 §6.7 两句 + §12.45 ⑦ 指向 + 新增 §12.47
- [x] `library.ts` 实现（门槛、版本键、剪旧、字节层去重、三处 fail-open）
- [x] 单测 52 → 63，全套 741 → 752
- [x] 9 条负控制，逐条 exact，校验和还原
- [x] 真浏览器三次进程取证 + 服务端命中数互证
- [x] 任务卡 + INDEX 顶行 + 坑清单 137
- [x] 删一次性脚本（可行性探针、harness、负控制脚本），证据 JSON/PNG/日志留在 gitignored `var/`
- [x] 本轮起的 8001 / 8098 已停，端口复核为 free
- [x] 一次本地提交（**不 push**）