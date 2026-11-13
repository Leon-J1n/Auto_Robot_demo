# Standalone Web Frontend（WebEditor）

独立的 Web 前端 + 本地 gateway：**浏览器是场景的唯一权威**，`local_gateway.py` 只是一条通道——把调用方请求经 `/events` WebSocket 转发给浏览器，并缓存浏览器最近上报的世界。本仓库不依赖 `sim_platform`、原生库或任何 Agent 服务即可运行（Agent 面板可选对接上游 Agent 服务，不接也能用，见下文"联调栈"）。

技术栈：前端 TypeScript + Three.js + Vite（`editor/`），gateway Python + aiohttp（单文件 `local_gateway.py`）。

## 环境配置

需要 Python 3 与 Node.js（含 npm）：

```bash
python -m pip install -r requirements.txt   # gateway 依赖（aiohttp 等）
corepack pnpm install --frozen-lockfile     # 前端依赖
```

资产库服务 OLS（ObjectLibraryServer）是**另一个 checkout**，放在本机下随便哪个目录，按其自身文档启动；端口要与 `config/constant_config.json` 里的 `library_server_url` 一致。浏览器直连 OLS 取资产，gateway 不代理。

## 启动项目

一条脚本（gateway 8099 + Vite 5173，可选带起 OLS 8000）：

```powershell
./start-dev.ps1
./start-dev.ps1 -GatewayPort 8100 -LibraryPort 8010 -FrontendPort 5188
```

或两个终端手动起：

```bash
python local_gateway.py --port 8099          # 读 config/constant_config.json
WEB_GATEWAY_URL=http://127.0.0.1:8099 npm run dev
```

浏览器打开 <http://127.0.0.1:5173>。注意：

- `editor/vite.config.ts` 的 proxy 必须显式列出所有根路径调用方接口（§5.9 的 8 条）；**新加根路径接口时要同步补这一行**，否则 dev 下拿到的是 SPA 的 HTML 壳而不是 JSON（详见 `docs/works/坑清单.md` 坑 3）。
- 运行时配置全在 `config/constant_config.json`（绑定地址、`library_server_url`、载荷上限、超时、相机预览上限、阴影默认值、视口背景色等），gateway 经 `GET /api/runtimeConfig` 原样发给浏览器，另附 `features.json` 的功能键。
- gateway 默认监听 `0.0.0.0` 且无鉴权；仅应在可信局域网中使用，并通过主机防火墙限制可访问设备。
- 场景内容只从两个入口来：页面 **加载场景 / 加载轨迹** 按钮（文件在浏览器本地读取，不上传），或下面的调用方接口。

### 联调栈（可选，Agent 任务链路）

Agent 面板走任务制（规格 §5.15）：浏览器把 `{command, scene}` 交给 gateway，gateway 转发给 `agent_service_url` 指向的上游 Agent 服务并轮询到终态。完整四服务栈（OLS :8000、QwenMock :8080、gateway :8099、AgentBrain :8500）的启动命令与端口分配见 `docs/各服务启动命令.md` 与 `docs/服务占用端口清单.md`；通信协议定稿见 `docs/gateway与Agent新通信流程.md`（brain-web/2）。不接上游时 Agent 面板回 `501 AGENT_UNAVAILABLE`，其余接口不受影响。

### 本地语音输入（可选，streaming-speech）

`streaming-speech` 是独立项目，默认监听 `127.0.0.1:8600`，需要单独启动；`start-dev.ps1`、`launch_gateway.bat` 和 `tools/restart_stack.ps1` 不会启动它。在语音项目目录运行 `./start.ps1` 或 `./start.bat`；首次安装、模型下载和 GPU 依赖按该项目 README 操作。语音服务及模型不包含在 WebEditor 发布包内。

Gateway 从 `config/constant_config.json` 读取已有配置 `"speech_service_url": "http://127.0.0.1:8600"`。这是 **Gateway 访问语音服务的地址**；服务部署到别的主机或端口时修改该值并重启 Gateway，同时按需修改语音项目 config.json 的监听地址与端口。浏览器始终经同源 WebSocket `/speech` → Gateway → 语音服务 `/stream`，无需直连 8600。

服务使用 Zipformer 流式预览、Paraformer 句末复核和 Silero VAD 静音过滤；发送按钮与回车等待最终识别结果后发送。支持 CPU，匹配的 NVIDIA 环境可使用 CUDA，无需 PyTorch。健康检查为语音服务 `GET /health`，返回 provider 和 two_pass。词表由语音项目 `vocabulary.json` 维护，修改后重启语音服务，字段说明见该项目 `VOCABULARY.md`。跨电脑访问麦克风需要 HTTPS 或浏览器认可的可信来源。语音服务未启动时语音输入会报错，普通文字输入仍可使用。

## 调用方接口（§5.9 速览）

8 条根路径接口（无 `/api` 前缀，snake_case 参数，JSON 错误体 `{"error":{code,message,request_id}}`）。**它们需要有一个打开的浏览器页签**，否则一律 `503 NO_BROWSER_CONNECTED`（仅 `/getWorld` 回落到缓存并带 `X-World-Source: cache`）：

```bash
curl -i http://127.0.0.1:8099/getWorld
curl -H 'Content-Type: application/json' --data-binary @tests/fixtures/scene_example.json http://127.0.0.1:8099/updateWorld
curl -X POST -H 'Content-Type: application/json' -d '{...}' 'http://127.0.0.1:8099/updateItem?id=6'
curl -X POST 'http://127.0.0.1:8099/removeObject?id=6'
curl -X POST -F 'trajs=@tests/fixtures/robot_grasp.traj' http://127.0.0.1:8099/updateTraj
curl 'http://127.0.0.1:8099/captureScene?id=4&type=rgbd'
curl 'http://127.0.0.1:8099/get2DAABB?id=6&camera_id=4'
```

（另有 `/addObject`。请求体与响应体的完整字段、错误码表、版本守卫 `?expected_version=N`、`/updateTraj` 的 multipart 多条约定：见规格 §5。）

可直接运行的调用方样例：`python examples/gateway_client_demo.py --gateway http://127.0.0.1:8099`（纯标准库，逐条演示上述接口与错误族）。样例轨迹生成器在 `examples/make_scene3_traj.py`、`make_scene3_variants.py`。`scene_digest` 只在 `digest.py` 一处计算（RFC 8785 规范化 + SHA-256）。

## 验证

```bash
python -X utf8 -m unittest discover -s tests   # gateway / 通道 / 调用方接口 / 错误映射
npm test                                       # vitest（editor/src）
npm run build                                  # tsc + vite build
node tools/browser_smoke.cjs                   # 端到端冒烟（需要下面的一次性验证栈）
```

真浏览器验证用一次性环境（不碰真实数据）：`python tools/serve_verification.py` 起一次性 gateway :8098 + OLS :8000，再以 `WEB_GATEWAY_URL=http://127.0.0.1:8098`、端口 5188 起 Vite，然后跑 smoke（`PLAYWRIGHT_MODULE`/`PLAYWRIGHT_BROWSERS_PATH` 指向本机 playwright）。跑 smoke 前确认 8098 上没有别的 viewer 连接，否则 `/getWorld` 会被空世界抢答（坑清单 坑 32）。

对账工具：`python tools/audit_parity.py`（探测调用方面，不虚构浏览器应答）、`python tools/parity_inventory.py`（重生成 `docs/api-inventory.json` 与 `docs/feature-inventory.csv`），有缺口即非零退出，报告落 `docs/parity-probes.json`。

## 发布打包

```bash
python pack.py 1.0.0-rel            # 先 build 再打包
python pack.py 1.0.0-rel --no-build # 复用已有 dist/
```

产物在 `distribute/<version>/WebEditor-<version>/`（含同名 zip），是**可运行包**：目标机只需 Python + aiohttp，gateway 自己服务 `dist/` 前端产物，`pip install -r requirements.txt && python local_gateway.py --port 8099`（或双击 `launch_gateway.bat`）后开 <http://127.0.0.1:8099>。OLS 不打包，`library_server_url` 指向运行中的实例即可。同版本重复打包会替换旧包，但旧包正被 gateway 服务时（Windows 目录占用）会停下并提示两条出路。

## 如何合作（任务记录制度）

**任务进度不再写进任何单一长文档。** 2026-11-05 起：

- 每次任务在 `docs/works/` 下**新建一个文件夹**（`YYYY-MM-DD-主题/`），里面的 `README.md` 必须写清三节：**原因**（为什么做）、**做了什么**（口径与落点）、**进度**（状态 / 完成日期 / commit / 验证）。
- 收尾时在 `docs/works/INDEX.md` 顶部加一行索引；踩到新坑记进 `docs/works/坑清单.md`。
- 修订功能口径时**就地改** `docs/Web前端需求规格v3.md` 对应小节（保留 Q 决议号），不要把规范文字写进任务文件夹。
- 新会话冷启动顺序、禁令、模板全文：见 `docs/works/README.md`。
- 历史任务卡（M0-*、FEAT-*、FIX-*，共 84 张，全部完成）已按每卡一个文件夹迁入 `docs/works/`；旧长文档原文冻结在 `docs/works/历史档案/`。

## 文档地图

| 文档 | 用途 |
|---|---|
| `docs/Web前端需求规格v3.md` | **现行口径唯一权威**：架构、场景格式引用、协议（§5）、UI（§7）、配置（§9）、验收（§12） |
| `docs/场景文件描述.md` | 世界文件（场景 JSON）逐组件格式说明，配 `docs/ur5e_table_empty.json` 真实样例 |
| `docs/gateway与Agent新通信流程.md` | Gateway ↔ Agent（Brain）通信定稿（brain-web/2） |
| `docs/gateway修改点.md` | brain-web/2 我方施工清单的现状摘要 |
| `docs/OLS接口.md`、`docs/轨迹文件布局描述.md` | 上游输入件（用户维护，勿改）：OLS 资产服务接口、TRAJ v2 二进制布局 |
| `docs/联调总结-1025-调用流程.md`、`docs/联调总结-1025-边界条件.md` | 四服务联调的现行流程与边界条件总结 |
| `docs/各服务启动命令.md`、`docs/服务占用端口清单.md` | 联调栈启动命令与端口分配 |
| `docs/api-inventory.json`、`docs/feature-inventory.csv`、`docs/parity-probes.json` | 工具生成的接口 / 功能键 / 对账清单 |
| `docs/works/` | 任务记录（制度见其 README；索引见 INDEX.md；历史原文在其 历史档案/） |
| `docs/Web前端最新要求.md` | 用户原始需求记录（仅意图溯源；与规格冲突以规格为准） |
