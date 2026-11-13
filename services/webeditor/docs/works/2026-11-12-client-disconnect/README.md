# 2026-11-12 客户端中途断连不算服务端错误（Q163）

## 原因

用户在 OLS 日志里看到两段叠在一起的 traceback（`20:00:40`，`GET /list?type=model`）：

```
ConnectionAbortedError: [WinError 10053] 你的主机中的软件中止了一个已建立的连接。
...
During handling of the above exception, another exception occurred:
...
  File "server.py", line 354, in _dispatch
    self._send_error(500, "internal_error", "unexpected server error")
ConnectionAbortedError: [WinError 10053] ...
```

**病灶是"只挡了 `BrokenPipeError`"**：浏览器刷新页签／切走会取消在途请求，Windows 上这种断连抛的是 `ConnectionAbortedError`（10053）或 `ConnectionResetError`（10054），它们与 `BrokenPipeError` 是**兄弟类**（同继承 `ConnectionError`），不是父子。于是落进宽 `except Exception` ⇒ 打第一段栈 ⇒ 试图往**已经死掉的 socket** 上写一个 500 ⇒ 第二段栈。日志里那个 `500` 是假的，客户端什么都没收到。

**这是既有代码，不是 Q162 引入的**：`git show 26227c9~1:server.py` 里同样是"只挡 `BrokenPipeError` + 宽 `except Exception` 转 500"，Q162 一个字没动这段。

**顺手自查了"是不是 Q162 把 `/list` 拖慢了才更容易撞上断连"**（每行现在都要算版本号）。在真实库上直接计时：`model` 40 行 **34.4 ms**、`robot` 11 行 26.5 ms、`scene` 7 行 3.5 ms。几十毫秒，不足以造成断连 —— stat-only 的设计在这里是有效的。

## 做了什么

### 1. 规格就地改（`docs/Web前端需求规格v3.md`，Q163）

- **§6.11** OLS 错误码那段后面补一条：客户端中途断连不是服务端错误，静默结束、不打 traceback、不得再写 500；真异常照旧。**只收窄"什么算异常"，不改任何响应形状**，故 `OLS接口.md` 的码表不动。
- **新增 §12.46**：四条判据（三种断连一律静默且不得二次写／三个入口都算／真异常仍 500＋traceback／响应形状零变化）。

### 2. OLS（`ObjectLibraryServer`，同级仓库、另一次提交）

- **`_dispatch`**：在宽 `except Exception` **之前**加 `except ConnectionError` 分支（一次覆盖 10053/10054/EPIPE），`ApiError` 分支里的 `except BrokenPipeError` 一并换成 `except ConnectionError`；两处都置 `close_connection = True`，不再复用这条连接。
- **`handle_one_request`**：外面再包一层同样的守卫。**这一层是复现探针逼出来的**：第一版探针在发出请求后立刻 RST，结果服务端死在 `rfile.readline`（10054）——**根本进不了 `_dispatch`**，同样刷一屏栈。`OPTIONS` 预检直写响应，也走这一层。
- **两层都必须存在，不是重复开关**：外层管"派发之前"和"直写路由"；内层管"不得再尝试写 500"——少了内层，宽 `except Exception` 会先往死 socket 上写一次，外层再吞掉也已经晚了（栈已经打了、第二次写已经发生）。
- **`README.md`** 新增「客户端中途断连」一节。**`docs/OLS接口.md` 不动**（用户维护的输入）。

## 不做

- **不改任何响应形状**：状态码、头、体、错误信封一律与 Q163 之前逐字相同。
- **不加日志行**：断连请求保留它原本那条 access log（头已经发出去的就是 `200`），不额外打"client disconnected"——那是第二套噪声。
- **不动前端、gateway、AgentBrain**：本轮前端**零代码改动**，只改规格文本。
- **不改 `OLS接口.md`**：码表没有新增或删除。
- **不给断连加计数/指标/开关**：一个 `except` 就够。
- **不治那份 48 MB 的 `d435i.obj`**：用户已明确交给同事处理。
- **不动 Q162 的缓存口径**：本轮与缓存正交，`/list` 的 34 ms 版本号开销实测不构成问题。

## 验证

### 单测

| 命令 | 结果 |
|---|---|
| OLS `python -X utf8 -u -m unittest discover -s tests -p test_api.py` | **71 例全绿**（改前 62，+9） |
| OLS `-p test_hooks.py` / `-p test_version.py` | 10 例 / 7 例全绿 |

新增两个类：`TestClientHangup`（6 例，派发路径）与 `TestHangupOutsideDispatch`（3 例，派发之前与预检）。夹具是一个 `HangupWriter`：放行前 N 次写、第 N+1 次抛指定异常，于是"写头时被掐"与"写体时被掐"是两个可分别钉住的场景；`stderr` 用 `redirect_stderr` 捕获，断言里面没有 `Traceback`。

### 负控制（逐条改坏一处 → 只跑该文件 → 复核）

7 条全部在**临时目录的副本**上跑（`server.py` + `init_index.py` + `tests/test_api.py` 拷进 temp），**活文件一个字节都没动**，前后 sha256 逐字相同。

| 改坏的那一处 | 变红的用例（恰好这些） |
|---|---|
| 删掉 `_dispatch` 的 `ConnectionError` 分支 | 3 条：写体、写头、发文件 |
| `ApiError` 分支退回 `except BrokenPipeError` | 1 条：`test_hangup_while_answering_an_api_error_is_silent` |
| 断连后不置 `close_connection` | 3 条：写体、写头、发文件 |
| 把宽 `except Exception` 也改成静默吞掉 | 1 条：`test_a_real_failure_still_reports_500` |
| `ConnectionError` 分支挪到宽 `except Exception` **之后** | 3 条：写体、写头、发文件（顺序即语义） |
| `_send_json` 去掉 `ETag` 头 | 2 条：`test_a_hangup_does_not_change_a_successful_response`、`test_json_etag_round_trip` |
| 删掉 `handle_one_request` 外层守卫 | 2 条：读请求行被掐、预检被掐 |

### 真 socket 复现（本轮跑了）

一次性栈：两份 OLS 各起一个进程，**都只读真实 `data`**——`8002` 跑 `git show HEAD:server.py`（修前），`8001` 跑本轮改后的文件。探针用裸 socket 发请求后 **`SO_LINGER=(1,0)` 强制 RST**（这才是浏览器取消请求的样子，`FIN` 不是），两种时机各三次：

- **mid-write**：先 `recv(1)` 拿到响应第一个字节再 RST ⇒ 服务端正在写 48 MB 的 `d435i.obj`；
- **pre-read**：发完立刻 RST ⇒ 服务端还没读请求行。

| | 修前（HEAD） | 修后（Q163） |
|---|---|---|
| traceback 段数 | **12** | **0** |
| 假日志行 `" 500 -` | **3** | **0** |
| stderr 总行数 | 294 | **6**（3 行启动横幅 + 3 条 access log） |

**如实说明**：loopback 上抛的是 **10054**（12 次全是），用户跨机那条是 **10053** —— 两者都是 `ConnectionError` 的子类，同一条守卫覆盖；单测里三种异常各跑一遍 subTest，不依赖某一种码。

## 进度

- [x] 规格 §6.11 就地补 + 新增 §12.46
- [x] OLS：`_dispatch` 守卫 + `handle_one_request` 守卫 + README
- [x] 9 条新用例 + 7 条负控制
- [x] 真 socket 修前/修后 A/B 复现
- [x] 本卡 + INDEX 顶行 + 坑清单 136
- [x] 两仓各一次本地提交（**不 push**）
- [x] 本轮起的 8001 / 8002 已停，端口复核为 free；用户的 8000 / 8099 全程未碰