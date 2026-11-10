# FEAT-2026-10-16-F —— 发布打包：`pack.py` 产出可运行包 + gateway 改为服务 `dist/` 产物

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-16 |
| 落地 | 本卡提交 |
| 依赖 | M2-1,M7-3,FEAT-2026-10-16-E |
| 决议号 | Q94、Q95（决议原文见 `docs/works/历史档案/` 中归档的需求规格 §13） |

## 原因与做了什么（PLAN_STATE 备注）

FEAT-2026-10-16f 新卡（第 ⑲ 张）：发布打包 —— `pack.py` 产出可运行包（Q94）+ gateway 改为服务 `dist/` 产物（Q95）。用户原话：「添加打包脚本，我会从命令行参数提供一个版本号，格式为 大版本.中版本.小版本，可能有附加的字符串如-rel…打包后的内容放在distribute/版本号中，同时有一个zip压缩包。你可以参考F:\ObjectLibraryServer\pack.py」；形态经确认取可运行包。决议记规格 §13.25，新增 §11.6/§11.7/§12.11。因果链：打包前没有任何进程在服务 `dist/`，而 `static()` 无条件回 `editor/index.html`（vite 开发入口，`/src/main.ts` 会被同一条 catch-all 再兜成 HTML ⇒ 白屏），即 8080 的页面从来不是可用发布路径 —— 可运行包必须先做 Q95。Q95 三条：真实文件优先 / SPA 兜底；`(dist/route).resolve()` 必须 `is_relative_to(dist)`（仓库根真有 `config/constant_config.json`、`features.json`，`/..%2F…` 一律兜底）；缺 `dist/index.html` ⇒ 404 + 点名 `npm run build`，不回退开发入口。`_version_note()` 把包内 `VERSION.txt` 首行打进启动日志，源码树打印 `unversioned checkout`；`VERSION.txt` 只写进包、不 stamp 源码树（与 OLS 版差异：无资源版本线、不做 `bump_patch`）。Q94：`RELEASE_FILES` 5 + `RELEASE_DIRS` 4（`dist`/`config`/`docs`/`examples`），`IGNORE` 剔 `__pycache__`/`*.pyc`/`.DS_Store`；zip 用 `ZipInfo` 统一 `date_time`、`compresslevel=1`、`external_attr=0o644<<16`，包内根即发布目录名；`--no-build` 复用 `dist/`；`--dist-dir` 相对路径按仓库根解析；新增 `launch_gateway.bat`（ASCII 注释，cmd 在 GBK 代码页下中文 rem 会坏）。自查出的第二个真问题：真机验证时包内 gateway（8097）没停就又打同版本号 ⇒ `rmtree` 把 `WebEditor-1.0.0-rel/` 删空才在目录锁上抛 `PermissionError [WinError 32]`；改成 `stage.rename(".old")` 再删（改名原子 ⇒ 锁住即整步失败、原样保留）并把 `require_sources()` 提到删旧包之前（坑 61）。测试：`tests/test_gateway_routes.py` 夹具改 `stage_dist()` 摆 `dist/index.html`+hash 产物、并在 `ROOT` 被换走前用真实 ROOT 建 gateway，4 条覆盖兜底/真实产物/越界/无构建 404；新建 `tests/test_pack.py` 10 例（版本号接受与拒绝、清单、空 `dist/`、`--dist-dir`、zip 布局、`clear_previous` 正常与锁住）；`tools/audit_parity.py` 摆 `dist/index.html`、`tools/serve_verification.py` 改为整份复制真实 `dist/`。验证：`python pack.py 1.0.0-rel` ⇒ 27 文件 / zip 638.6 KB；解包后 `python local_gateway.py --port 8097` 全程无 Node，`/`→SPA、`/assets/*.js

## 任务卡原文

### FEAT-2026-10-16f —— 发布打包：`pack.py` 产出可运行包 + gateway 改为服务 `dist/` 产物（Q94/Q95）（新卡；依赖 FEAT-2026-10-16e、M2-1、M7-3）

- **起因（用户原话）**：「添加打包脚本，我会从命令行参数提供一个版本号，格式为 大版本.中版本.小版本，可能有附加的字符串如-rel。版本号形似1.0.0-rel。打包后的内容放在distribute/版本号中，同时有一个zip压缩包。你可以参考F:\ObjectLibraryServer\pack.py」。形态用 `AskUserQuestion` 确认为**可运行包**（不是源码包）。决议 **Q94/Q95** 记在规格 **§13.25**，新增 **§11.6 发布包**、**§11.7 gateway 服务产物**、**§12.11 验收**。
- **本轮最关键的一条因果**：打包之前**没有任何进程在服务 `dist/`**，而 `static()` 无条件返回 `ROOT/editor/index.html` —— 那是 vite 的开发入口（`<script type="module" src="/src/main.ts">`），经 gateway 返回时 `/src/main.ts` 又被同一条 catch-all 兜成一份 HTML，浏览器白屏。也就是说 **8080 端口的页面从来就不是可用的发布路径**。可运行包成立的前提是让 gateway 自己服务产物，这就是 Q95。
- **`local_gateway.py:static()` 三条口径**：① 真实文件优先（`dist/assets/index-<hash>.js`、`.css` 原样返回，Content-Type 由 `FileResponse` 猜），其余一律回 `dist/index.html`；② **不得越界** —— `(dist / route).resolve()` 必须 `is_relative_to(dist)`，`/..%2Fconfig%2Fconstant_config.json` 落到 SPA 兜底（这条不是假想敌：仓库根就有 `config/constant_config.json` 和 `features.json`）；③ 没有 `dist/index.html` 返回 **404** 且响应体点名 `npm run build`，**不回退** `editor/index.html`。注册顺序不变，仍在 §5.9 全部路由之后（**坑 2**）。
- **启动即自报身份**：`_version_note()` 读包内 `VERSION.txt` 首行打进日志（`WebEditor gateway (AppVersion 1.0.0-rel) on http://…`），源码树里没有该文件则打印 `unversioned checkout`。`pack.py` **只把 `VERSION.txt` 写进包里，不 stamp 源码树**（与 OLS 那版的差异：本项目代码即产物，没有第二条资源版本线，不做 `bump_patch`，每次打包都改源码树只会留一处无意义 diff）。
- **`pack.py`（仓库根，新增）**：`python pack.py 1.0.0-rel` → 默认 `npm run build` → `require_sources()` 清单校验 → 定位 `distribute/<版本号>/WebEditor-<版本号>/` → `clear_previous()` → 拷清单（`RELEASE_FILES` 5 个：`local_gateway.py`/`features.json`/`requirements.txt`/`README.md`/`launch_gateway.bat`；`RELEASE_DIRS` 4 个：`dist`/`config`/`docs`/`examples`；`IGNORE` 剔 `__pycache__`、`*.pyc`、`.DS_Store`）→ `VERSION.txt` → `make_zip()`（`ZipInfo` 统一 `date_time`、`ZIP_DEFLATED compresslevel=1`、`external_attr=0o644<<16`，zip 内根就是那一层目录）。`--no-build` 复用现有 `dist/`；`--dist-dir` 相对路径按**仓库根**解析。目标机只需 Python + aiohttp，OLS/Agent 是外部依赖，包内不含。
- **`launch_gateway.bat`（新增）**：`cd /d "%~dp0"` + `python local_gateway.py %*` + `pause`，注释全 ASCII（cmd 在 GBK 代码页下会把中文 rem 显示成乱码，且部分版本直接报错）。
- **自查出来的第二个真问题（已修，见坑 61）**：真机验证时从包内起 gateway（8097）后忘了停，又跑一次同版本号打包 —— `rmtree` 把 `WebEditor-1.0.0-rel/` **删空**，最后在那把目录锁上抛 `PermissionError [WinError 32]`，正在被服务的发布包面目全非。改成 `stage.rename(stage+".old")` 再删（改名原子，锁着就整步失败、原样保留），并把 `require_sources()` 提到删旧包之前；实测锁现场得到的是 `[pack] cannot take … out of the way …; stop the gateway running from it, or pack a different version`，旧包 10 项俱在。
- **测试（python 158 → 169 例，全绿）**：`tests/test_gateway_routes.py` 的夹具从"临时根摆 `editor/index.html`"改成 `stage_dist()` 摆 `dist/index.html` + `dist/assets/index-abc.js`，并**在 `local_gateway.ROOT` 被换掉之前**先用真实 ROOT 建好 gateway（`/api/runtimeConfig` 读的是 import 期算出的 `DEFAULT_CONFIG_PATH`）；新增/改写 4 条：兜底回 SPA、真实产物按自身 Content-Type 返回、`..%2F` 越界不得读出 `config/constant_config.json`、没有构建时 404 且点名 `npm run build`。新建 `tests/test_pack.py` **10 例**：版本号接受/拒绝（含路径非法字符，这一层跑在形状校验之前）、清单与 `VERSION.txt`、空 `dist/` 拦下、`--dist-dir` 按仓库根解析、zip 全量包在发布目录名下且时间戳统一、`clear_previous()` 的正常与"锁住"两条。`tools/audit_parity.py` 与 `tools/serve_verification.py` 的临时根一起改成摆/复制真实 `dist/`（后者现在把构建产物整份复制进临时根，8099 的 `/` 就是发布包会看到的那一页）。
- **验证（真机，解包后的包直接跑）**：`python pack.py 1.0.0-rel` → 27 个文件、zip 638.6 KB；`cd distribute/1.0.0-rel/WebEditor-1.0.0-rel && python local_gateway.py --port 8097`（**全程无 Node**，OLS 8000 在跑）。curl 面：`/` 200 `text/html`、`/assets/index-C__q8kh4.js` 200 `text/javascript` 859 397 B、`/assets/index-CZqqwigv.css` 200 `text/css`、`/no/such/route` 兜底、`/..%2Fconfig%2Fconstant_config.json` 兜底（读不出配置）、`/api/health` 与 `/api/runtimeConfig` JSON。Chromium 面（`scene5probe\pack96\bundle-1440.png`）：`script` 只有那一条 hash 产物、`hasCanvas:true`、状态栏 `Connected · #0 · v0 · 0 objects`（**WS `/events` 同源直连**），打开资产面板得 `LIBRARY 55`、一级 `预设场景7/预制模型34/机械臂14/自定义7`、场景子类 7 行 —— 与 vite 开发页一致。日志首行 `INFO … WebEditor gateway (AppVersion 1.0.0-rel) on http://127.0.0.1:8097`。
- **验证（回归链，全绿）**：`npm run build` 干净（既有 chunk>650 kB warn 不变）、`npm test` **330 例**、`python -X utf8 -m unittest discover -s tests` **169 例**、`tools/parity_inventory.py` 79 键 / 21 本地路由 / 0 死键、`tools/audit_parity.py` **52/52**、一次性栈（gateway 8099 + vite 5188 + OLS 8000）`node tools/browser_smoke.cjs` **11/11**。
- **明确不做**：不做 `bump_patch`/自动版本号、不做资源版本线；不引 Node 进目标机、不做 pyinstaller 之类冻结；不把 `distribute/` 纳入版本控制（`.gitignore` 已加，与 `dist/` 同理）；不改 §5.9 任何接口（打包是产物与进程的事，parity 判据不增不减）；§8 禁令文件一字不改。
- **一轮收尾**：本轮起的 gateway 8097（包内）、8099、vite 5188、OLS 8000 逐个停并 `netstat` 确认；只本地提交、**不 push**；`examples/gateway_client_demo.py` 的用户自有改动与三份用户自有未跟踪文档（`docs/gateway与Agent服务通信流程.md`、`docs/服务占用端口清单.md`、`docs/轨迹文件布局v3(草案).md`）不进本提交 —— 但它们**会**被 `pack.py` 拷进包（发布包按目录整体收 `docs/`，这是本卡有意留下的取舍，见确认项）。
