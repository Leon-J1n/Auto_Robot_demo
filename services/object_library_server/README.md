# OLS - ObjectLibraryServer

基于 Python 标准库的对象库 HTTP 服务器，为客户端提供**场景 / 模型 / 机械臂**三类资产数据的查询、下载、上传与软删除。接口规格见 `docs/OLS接口.md`。

- 零第三方依赖，仅需 Python 3.8+
- SQLite（`data/{scene,model,robot}_index.db`）作为资产索引
- 多用户隔离（uid）、软删除、路径安全校验

## 目录结构

```
ObjectLibraryServer/
├── README.md            # 本文档
├── config.json          # 服务配置
├── server.py            # 主程序（纯标准库）
├── init_index.py        # 一次性迁移脚本：legacy *_index.json -> *_index.db
├── launch_server.bat    # Windows 启动脚本
├── pack.py              # 打包脚本：python pack.py <代码版本> [资源版本] -> dist/<版本>/
├── VERSION.txt          # 代码版本（AppVersion x.y.z[-附加] + built at 构建时间）
├── tests/               # 端到端测试（47 个用例）+ 版本规则单元测试（7 个）
├── docs/OLS接口.md      # 接口规格文档
└── data/                # 资产数据（服务只读，写接口产物也落在此处）
    ├── VERSION.txt      # 资源版本（ResVersion x.y.z[-附加] + built at 构建时间）
    ├── scene_index.db / model_index.db / robot_index.db
    ├── scenes/<id>/  models/<id>/  robots/<id>/
    └── *_index.json   # 旧版 JSON 索引（迁移来源，保留存档）
```

## 快速开始

### 1. 初始化索引（首次运行）

如果 `data/*_index.db` 尚不存在，先从遗留 JSON 索引生成：

```bash
python init_index.py --data-dir ./data
```

输出示例：`scene: wrote 5 records`、`model: wrote 29 records`、`robot: wrote 14 records`。
所有导入记录的 `upload_user_id=0`（系统资产）、`is_deleted=0`。脚本可重复执行（每次重建 .db）。

### 2. 配置

`config.json`（字段均可省略，缺省值见 `server.py` 的 `DEFAULT_CONFIG`）：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `host` | `127.0.0.1` | 监听地址；局域网访问用 `0.0.0.0` |
| `port` | `8000` | 监听端口 |
| `data_dir` | `./data` | 数据目录 |
| `allowed_extensions` | `.obj .mtl .png .jpg .jpeg .urdf .stl .dae .gltf .glb .bin` | `/file` 下载扩展名白名单（大小写不敏感） |
| `downloadable_types` | `["model", "robot"]` | 允许通过 `/file`、`/fileByName` 下载的类型 |
| `cors_allow_origin` | `*` | CORS `Access-Control-Allow-Origin`；可设为具体来源如 `http://localhost:3000` 限制跨域访问 |

### 3. 启动

```bash
launch_server.bat                          # 等价于 python server.py
launch_server.bat --port 8080              # 命令行参数优先于 config.json
python server.py --host 0.0.0.0 --port 8000 --data-dir ./data --config ./config.json
```

规则（按规格）：`--config` 指定的文件不存在时回退 `./config.json`（相对当前工作目录）；两者都不存在或 JSON 语法错误则启动失败并输出错误信息。启动成功控制台会先打印代码版本与资源版本（读取根目录 `VERSION.txt` 与 `data/VERSION.txt`），再打印监听地址：

```
[OLS] code version: 1.0.1-dev (built at 2026-10-11T22:51:15)
[OLS] resource version: 1.0.1 (built at 2026-10-11T22:51:15)
[OLS] ObjectLibraryServer listening on http://127.0.0.1:8000 (data_dir=...)
```

版本文件格式两行：`AppVersion|ResVersion <大版本.中版本.小版本[-附加字符串]>` 与 `built at <时间>`。文件缺失时显示 `unknown`，不影响启动。

### 4. 打包发布（可选）

```bash
python pack.py 1.0.2                  # 完整包（含 data/）；未给资源版本时自动把
                                      # data/VERSION.txt 的小版本号 +1（1.0.1 -> 1.0.2）
python pack.py 1.0.2 2.0.0            # 完整包并显式指定资源版本
python pack.py 1.0.2 --no-data        # 精简包：只需要一个版本号，可加 -附加字符串
```

- 版本号规则 `大版本.中版本.小版本[-附加字符串]`（附加可省略，如 `1.0.1-dev`）；非法格式直接报错。
- `--no-data` 时不允许传第二个版本号。
- 产物写入 `dist/<代码版本>/`：发布目录 `ObjectLibraryServer-<代码版本>/` 与 zip
  （含数据时 zip 名为 `...-res-<资源版本>.zip`）。
- 打包会把版本与构建时间（打包时刻）写回**包内**和**源目录**的 `VERSION.txt` /
  `data/VERSION.txt`，因此下次启动打印的就是最新发布版本。`dist/` 已被 git 忽略。

## 接口使用指引

以下示例中 `BASE=http://127.0.0.1:8000`。所有参数经 URL 查询串传递；`/uploadFile` 的文件内容放在 POST 请求体原始字节中。所有接口支持 `uid`（int ≥ 0，缺省 0）；**未提供 uid 即系统用户**。所有响应（含错误与文件下载）均携带 CORS 头（`Access-Control-Allow-Origin` 由 `cors_allow_origin` 配置），浏览器 `OPTIONS` 预检直接支持。

### 用户隔离（uid）语义

- 每个 uid 只能看到：系统上传资产（`upload_user_id=0`）+ 自己上传的资产。
- 他人资产一律返回 404（与不存在一致，不泄露存在性）。
- 软删除的记录对所有 uid 不可见，但 id 永不复用。

### 查询类（GET）

```bash
# 资产列表（可按 category 精确过滤）
curl "$BASE/list?type=model&uid=0&category=模型.抓取物"
# -> {"type":"model","items":[{"id":1,"name":"apple","category":"模型.抓取物"}, ...]}

# 类别列表
curl "$BASE/listCategory?type=scene"
# -> {"type":"scene","category":["场景.开柜","场景.抓取","场景.空白"]}

# 场景：/scene 按 id、/sceneByName 按 name（只返回入口 JSON，不拼接清单）
curl "$BASE/scene?id=1"
curl "$BASE/sceneByName?name=ur5e_table_apple"

# 模型/机械臂：返回 {name}.json 元数据与 {name}_files.json 清单**合并后的单个 JSON**
curl "$BASE/model?id=1"          # /modelByName?name=apple
curl "$BASE/robot?id=1"          # /robotByName?name=ur5e_arm

# 文件下载（type 必须在 downloadable_types 中；扩展名必须在白名单中）
curl -o apple.obj "$BASE/file?type=model&id=1&path=apple.obj"
curl -o arm.urdf  "$BASE/fileByName?type=robot&name=ur5e_arm&path=ur5e_arm.urdf"
# 支持 URDF 常用的 package:// 前缀（自动剥除）
curl "$BASE/file?type=robot&id=1&path=package://ur5e_arm/ur5e_arm.urdf"

# 缩略图（读取清单的 thumbnail 字段；三种类型均支持）
curl -o thumb.png "$BASE/thumbnail?type=model&id=1"
```

`/file`、`/fileByName` 的路径安全规则：禁止 `..`、绝对路径、盘符、空字节、符号链接；最终路径必须落在 `data/<type>s/<id>/` 内；`id`/`name` 必须先在索引中解析为可见记录。

### 写入类（POST）

```bash
# 1) 预注册：分配新 id（uid>0 从 10000 起累加；uid=0 在 10000 以下累加，用尽后回退全表最大 id+1；含已删除记录，id 不复用）
#    建目录并生成初始清单 {name}_files.json
curl -X POST "$BASE/presignUploadID?type=model&name=my_cup&entranceFilePath=my_cup.obj&uid=7&category=模型.抓取物"
# -> {"type":"model","id":30,"name":"my_cup"}
# category 缺省为 "模型.自定义" / "机械臂.自定义" / "场景.自定义"

# 2) 上传文件：content 为请求体原始字节；role 缺省 reference
curl -X POST --data-binary @my_cup.obj  "$BASE/uploadFile?type=model&id=30&path=my_cup.obj&uid=7"                 # 入口文件
curl -X POST --data-binary @my_cup.mtl  "$BASE/uploadFile?type=model&id=30&path=my_cup.mtl&uid=7&role=reference"  # 追加进 reference
curl -X POST --data-binary @my_cup.json "$BASE/uploadFile?type=model&id=30&path=my_cup.json&uid=7&role=metadata"  # 写清单 metadata 字段
curl -X POST --data-binary @thumb.png   "$BASE/uploadFile?type=model&id=30&path=thumbnail.png&uid=7&role=thumbnail"
# path 允许嵌套目录（如 meshes/part.obj）。role=metadata 对 scene 返回 400（场景清单无该字段）；
# 场景清单也没有 reference 字段——场景的非入口 reference 文件只落盘、不登记。

# 3) 软删除：仅置索引 is_deleted=1，磁盘文件保留；uid=0 为管理员可删任意记录
curl -X POST "$BASE/deleteAsset?type=model&id=30&uid=7"
# -> {"deleted": true, "type": "model", "id": 30}
```

`/uploadFile` 校验请求 `uid` 与记录 `upload_user_id` 一致，不一致返回 403。`/deleteAsset`：删除系统资产（对非 0 uid）返回 403；记录不存在/已删除/属他人返回 404。

### 错误码

| 状态码 | error | 场景 |
| --- | --- | --- |
| 400 | `bad_request` | 参数缺失或非法（id/uid/type/role 解析失败、path 为空、场景 role=metadata） |
| 403 | `forbidden` | 路径非法（`..`/绝对/空字节/符号链接）、扩展名不在白名单、类型不可下载、上传/删除 uid 不匹配 |
| 404 | `not_found` | 资源不存在、已删除或对当前 uid 不可见、未知路由 |
| 500 | `internal_error` | 服务器内部错误、索引数据 JSON 损坏 |

错误响应统一为 `{"error": "...", "message": "..."}`；成功 JSON 响应为 `application/json; charset=utf-8`；文件下载按扩展名推断 Content-Type。

## 测试指引

### 自动化端到端测试（推荐先跑这个）

测试会在临时目录构造一套小型资产库（scene/model/robot 各含系统资产），启动真实 HTTP 服务后通过 urllib 逐接口验证，包括 47 个用例：查询与合并返回、id/name 解析、category 过滤、路径安全（`..`、绝对路径、空字节、扩展名白名单、package:// 前缀）、缩略图、uid 隔离（跨用户 404 / 上传 403）、完整上传流程与清单登记、软删除与 id 不复用、管理员删除、id 分区规则（用户 10000 起 / 系统 10000 以下 / 用尽回退）。

```bash
python tests/test_api.py          # 或 python -m unittest tests.test_api -v
# 期望：Ran 47 tests ... OK

python tests/test_version.py      # 版本文件解析 + pack 版本格式/自动 +1 规则（7 个用例）
# 期望：Ran 7 tests ... OK
```

测试完全在临时目录进行，不会触碰 `data/`。

### 真实数据手动冒烟

```bash
python init_index.py                       # 确保 data/*_index.db 已生成
python server.py --port 8000 &             # 或 launch_server.bat
curl "http://127.0.0.1:8000/list?type=model"            # 应返回 29 条系统资产
curl "http://127.0.0.1:8000/modelByName?name=apple"     # 元数据+清单合并
curl -o a.obj "http://127.0.0.1:8000/file?type=model&id=1&path=apple.obj"
curl -o t.png "http://127.0.0.1:8000/thumbnail?type=scene&id=1"
curl "http://127.0.0.1:8000/file?type=model&id=1&path=../docs/OLS%E6%8E%A5%E5%8F%A3.md"   # 期望 403
```

> 写接口冒烟建议先复制数据目录，避免污染真实库：
> `cp -r data /tmp/ols_data && python server.py --port 8081 --data-dir /tmp/ols_data`
> 然后按上文 presignUploadID → uploadFile → deleteAsset 顺序验证，再对照
> `/list?uid=<你的uid>` 可见性、错误码（403/404/400）。

### 常见问题

- **启动报 `config file not found`**：请从含 `config.json` 的目录启动，或用 `--config` 指定路径（规格规定缺省回退 `./config.json`）。
- **`/list` 返回空或 `no such table`**：先运行 `python init_index.py` 生成 .db。
- **中文 category 乱码**：确认终端为 UTF-8；服务响应本身始终是 `charset=utf-8`。
