用python制作一个HTTP服务器，项目名为ObjectLibraryServer。该服务用于提供场景、模型和机械臂数据。

在接下来的描述中，对于模型来说，id均指代object_id，name均指代object_name。对于场景来说，id均指代scene_id，name均指代scene_name。对于机械臂来说，id与name与其元数据中的object_id/object_name含义一致。数据目录结构保证所有存在于对应索引（`{type}_index.db`）中的内容均为有效内容。除写接口（/presignUploadID、/uploadFile、/deleteAsset）外，data目录中的数据对服务只读；上传产生的新数据也存放在data/下对应类型的目录中。

用户隔离（uid）规则，适用于所有接口：

1. 所有接口均支持参数 `uid`（int，>=0），用于标识当前操作用户。未提供或为空时按 `uid=0`（系统用户）处理。
2. 用户 uid 的可见范围仅限：系统上传的资产（`upload_user_id = 0`）与其自己上传的资产（`upload_user_id = uid`）。uid=0 时只能访问系统上传的资产。
3. 所有查询接口（/list、/listCategory、/scene、/sceneByName、/model、/modelByName、/robot、/robotByName、/file、/fileByName、/thumbnail）只在可见范围内解析和返回数据。若 id 或 name 在索引中存在但对当前 uid 不可见，返回 404（与不存在一致，避免泄露他人资产的存在性）。
4. /presignUploadID 新建的记录 `upload_user_id = uid`；/uploadFile 校验目标记录的 `upload_user_id` 与请求 uid 一致，不一致返回 403。
5. 所有查询接口只返回 `is_deleted = false` 的记录；/deleteAsset 通过置 `is_deleted = true` 实现软删除（详见接口表）。
6. `no_display = true` 仅从 `/list` 结果中排除该记录；按 id、按 name、文件与缩略图接口仍按上述 uid 与 `is_deleted` 规则访问。`/listCategory` 不受此标记影响。

具有以下接口：

| 类型 | 接口             | 参数                       | 说明                                                         |
| ---- | ---------------- | -------------------------- | ------------------------------------------------------------ |
| GET  | /list            | type,uid,category          | type只能为scene或model或robot，列出当前uid可见、未删除且 `no_display=false` 的资产；`category` 为可选精确匹配过滤参数。`no_display` 不出现在响应条目中。返回 JSON类似：`{"type":"model","items":[{"id":1,"name":"apple","category":"模型.抓取物"}]}` |
| GET  | /listCategory    | type,uid                   | 列出当前指定type下、当前uid可见资产的所有可能类别（不按 `no_display` 筛选），返回JSON类似：{"type":"model", "category":["模型.抓取物", "模型.容器"]} |
| GET  | /scene           | id,uid                     | 获取指定id的场景数据。根据id从scene\_index.db中找到场景名，返回 `data/scenes/id/{scene_name}.json` 的 JSON 内容。 |
| GET  | /sceneByName     | name,uid                   | 获取指定name的场景数据，根据name从scene\_index.db中找到场景id，然后返回 `data/scenes/id/{scene_name}.json` 的 JSON 内容。`name` 必须存在于对应的索引中，不能直接拼接用户输入。若不存在（或对该uid不可见），返回 404。如果有多个，返回第一个。 |
| GET  | /model           | id,uid                     | 获取指定id的模型数据，返回 `data/models/id/name.json` 的 JSON 内容+data/models/id/{$name}_files.json的内容，将两组json拼接成一个返回。 |
| GET  | /modelByName     | name,uid                   | 获取指定name的模型数据，返回 `data/models/id/name.json` 的 JSON 内容+data/models/id/{$name}_files.json的内容，将两组json拼接成一个返回。`name` 必须存在于对应的索引中，不能直接拼接用户输入。若不存在（或对该uid不可见），返回 404。如果有多个，返回第一个。 |
| GET  | /robot           | id,uid                     | 获取指定id的机械臂数据，根据id从robot\_index.db中找到机械臂名，然后返回 `data/robots/id/name.json` 的 JSON 内容+data/robots/id/{$name}_files.json的内容，将两组json拼接成一个返回。 |
| GET  | /robotByName     | name,uid                   | 获取指定name的机械臂数据，返回 `data/robots/id/name.json` 的 JSON 内容+data/robots/id/{$name}_files.json的内容，将两组json拼接成一个返回。`name` 必须存在于对应索引中，不能直接拼接用户输入。若不存在（或对该uid不可见），返回 404。如果有多个，返回第一个。 |
| GET  | /file            | type,id,path,uid           | type必须为model或robot（可通过config调整），id必须是当前uid可见的有效id，`path` 是相对于模型或机械臂目录的任意相对路径，但扩展名必须在 `allowed_extensions` 中，通过对应类型的索引文件查询名字，且最终路径必须位于 `data/<type>s/<id>/` 内。本接口用于获取指定路径的文件（通常用于客户端下载模型），根据id从{type}\_index.db中找到模型名或机械臂名字，返回./data/{type}s/id/path 的文件，由于数据中当is_on_cos=true时，path总是一个相对路径，因此可以放心拼接。通过config来指定哪些类型可以通过该接口下载。 |
| GET  | /fileByName      | type,name,path,uid         | type必须为model或robot（可通过config调整），name必须是当前uid可见的有效名字，`path` 是相对于模型或机械臂目录的任意相对路径，但扩展名必须在 `allowed_extensions` 中，且最终路径必须位于 `data/<type>s/<id>/` 内。本接口用于获取指定路径的文件（通常用于客户端下载模型），返回./data/{type}s/id/path 的文件，由于数据中当is_on_cos=true时，path总是一个相对路径，因此可以放心拼接。通过config来指定哪些类型可以通过该接口下载。如果有多个id，返回第一个。 |
| GET  | /thumbnail       | type,id,uid                | 获取指定类型指定id的缩略图，type=scene\|model\|robot，thumbnail路径存在于对应物体的文件清单（`{name}_files.json`）的 `thumbnail` 字段中（三种类型均有文件清单）。 |
| POST | /deleteAsset     | type,id,uid                | type为scene\|model\|robot，软删除指定资产：仅将该type索引中对应记录的 `is_deleted` 置为 true，磁盘上的数据目录和文件保留不动。删除后，所有查询接口不再返回该资产（含 /list、/listCategory、/scene、/model、/robot、/file、/fileByName、/thumbnail 及按 name 的解析），但已分配的 id 不会被复用（新上传仍按第7条分区规则取号，含已删除记录）。仅允许删除 `upload_user_id = uid` 的资产（即用户只能删除自己上传的）：目标记录存在但对当前 uid 可见范围内属于系统上传或他人上传时返回 403；记录不存在、已删除或对当前 uid 不可见时返回 404。uid=0 视为系统管理员，可删除任意记录。成功返回 JSON：{"deleted": true, "type": "model", "id": 30}。 |
| POST | /presignUploadID | type,name,entranceFilePath,uid,category | type为scene\|model\|robot，当用户准备开始上传文件之前，先获取一个预注册的ID用于后续上传。name是新资产的名字，entranceFilePath表示该资产的入口文件路径，将会填入{$name}_files.json中的path字段。category为新资产的类别字符串（如"模型.抓取物"、"场景.抓取"），未提供时默认为"{类型前缀}.自定义"，类型前缀按type取 模型/机械臂/场景（即默认"模型.自定义"、"机械臂.自定义"、"场景.自定义"）。服务端在对应类型的索引中新建记录：id按第7条分区规则分配（uid>0 时从 10000 起累加；uid=0 时在 10000 以下累加，用尽后回退全表最大+1；均在该type全部记录上取值，避免与任何人已占用的id冲突），upload_user_id=uid，is_deleted=false，no_display=false，并在 `data/<type>s/<id>/` 下建立目录和一份仅含 `is_on_cos:false` 与 `path:entranceFilePath` 的{$name}_files.json（metadata/thumbnail/reference字段由后续 /uploadFile 的role参数逐步补全）。返回 JSON：{"type":"model","id":30,"name":"apple"}。注意：在当前版本,is_on_cos字段总为false. |
| POST | /uploadFile      | type,id,path,content,uid,role | type为scene\|model\|robot，id为从/presignUploadID获取的预签名ID，path是一个相对路径，表示上传后此文件应当位于目录中的哪里，content是文件内容（type/id/path/uid/role 通过查询字符串传递，content 为 POST 请求体的原始字节，不做 encoding）。服务端校验该记录的upload_user_id与请求uid一致（不一致返回403）。`role` 为可选参数，取值 metadata\|thumbnail\|reference，缺省为 reference：role=reference 且 path 非 entranceFilePath 时，路径填入清单的 reference 数组；role=metadata 时写入清单的 metadata 字段（仅model/robot，场景清单无此字段，返回400）；role=thumbnail 时写入清单的 thumbnail 字段。文件一律写入 `data/<type>s/<id>/<path>`（路径安全规则同 /file）。 |

建议的项目结构如下：

<$ProjectRoot> # 项目根目录

-README.MD 

-config.json # 服务配置文件

-server.py # 主要代码文件

-launch\_server.bat # 启动服务，可以通过命令行指定端口等参数，此参数优先级高于配置文件

-data/ # 这里存放数据

--scene\_index.db # 场景列表和索引

--model\_index.db # 模型列表和索引

--robot\_index.db # 机械臂列表和索引

--scenes/ # 场景数据

---场景id/ # 场景0

----scene_name.json # 场景入口文件（以场景名命名）

----scene_name_files.json # 场景文件清单

----scene_name.png # 场景预览图

----scene_name_thumbnail.png # 场景缩略图

---场景id/  # 场景1

----...

--models/ # 模型数据

---模型id/ 

----model0.obj # 有可能是个obj模型

----model0.mtl # obj模型引用了mtl

----model0.json # 模型元数据

----model0_files.json # 模型文件清单

----model0_thumbnail.png # 模型缩略图

---模型id/

----example.urdf # 有可能是个urdf

----example.json # 模型元数据

----example_files.json # 模型文件清单

----example_thumbnail.png

----example_meshes/ # urdf有可能继续往下引用其他的obj，这些obj可能又组成新的目录结构

-----xxx.obj

--robots/ # 机械臂数据，目录结构与models相同

---robot id/

----robot.urdf

----robot.json

----robot_files.json

----robot_thumbnail.png

...

模型/机械臂元数据结构类似（两种类型共用同一结构，机械臂的grasp\__properties通常为空）：

```json
{
  "semantic_properties": {
    "object_name": "apple",
    "object_id": 1,
    "deformable": false,
    "scalable": false,
    "interactable": true,
    "tag": "red"
  },
  "geometric_properties": {
    "AABB": [
      0.074655,
      0.074732,
      0.071247
    ]
  },
  "physical_properties": {
    "mass_kg": 0.068,
    "friction": {
      "format": "[sliding, torsional, rolling]",
      "values": [
        0.8,
        0.005,
        0.0005
      ]
    },
    "restitution": 0.05,
    "scale": [
      1.0,
      1.0
    ]
  },
  "grasp_properties": {
    "pose_count": 3,
    "poses": [
      [
        0.0,
        0.0,
        0.0,
        3.1415926,
        0.0,
        0.0,
        0,
        1,
        0,
        1.047198
      ]
    ]
  }
}

```

其中 `grasp_properties.poses` 每个元素格式为 `[x, y, z, rx, ry, rz, tolerance_axis_x, tolerance_axis_y, tolerance_axis_z, tolerance_radius]`：前六项为抓取位姿，第7\~9项为容差轴单位向量，第10项为容差半径（弧度）。

模型/机械臂文件清单类似：

```json
{
  "is_on_cos": false,
  "metadata": "apple.json",
  "path": "apple.obj",
  "thumbnail": "apple_thumbnail.png",
  "reference": [
    "apple.png",
    "apple.mtl"
  ]
}
```

场景文件清单结构更简单（场景没有metadata，且入口文件不会出现多处引用，因此无 `metadata` 和 `reference` 字段）：

```json
{
  "is_on_cos": false,
  "path": "ur5e_table_empty.json",
  "thumbnail": "ur5e_table_empty_thumbnail.png"
}
```



{$type}_index.db数据库结构设计如下：

| 名字           | 类型        | 说明                                            |
| -------------- | ----------- | ----------------------------------------------- |
| id             | int         | 主键，数字ID，绝不重复                          |
| name           | varchar(64) | 名字，带索引，可重复，绝不为null，需要支持UTF-8 |
| upload_user_id | int         | 上传用户id，如果是系统上传，默认为0，绝不为null |
| category       | varchar(32) | 类别，需要支持UTF-8，绝不为null                 |
| is_deleted     | bool        | 是否被删除，默认是false                         |
| no_display     | bool        | 是否从 `/list` 隐藏，默认是false；不影响按 id 访问 |

category使用分层字符串，按类型分别以`模型.`、`机械臂.`、`场景.`为前缀，例如"模型.抓取物"、"机械臂.夹爪"、"场景.抓取"、"场景.开柜"、"场景.空白"。

旧 JSON 索引中也保留 `no_display`：`scenes/scene_index.json` 的每个场景条目使用布尔字段 `"no_display": false`；`models/model_index.json` 与 `robots/robot_index.json` 使用顶层 `"no_display": {"1": false, ...}`，以字符串 id 为键。`init_index.py` 从这些字段导入数据库，缺省为 `false`。已有 SQLite 索引在服务器启动时自动添加该列，保留原数据。`init_index.py` 会重建数据库，因此不能用它迁移已有上传记录。设置为 `true` 的资产仍能按 id 访问；若需持久保留隐藏设置，在数据库与旧 JSON 索引中同步修改对应值。



额外说明：

1.`/file` 是高风险接口。

- 禁止 `../`
- 禁止绝对路径
- 禁止空字节
- 不允许符号链接
- 如果 `path` 以 `package://<package_name>/` 开头，则去掉 `package://<package_name>/` 前缀，替换为 `data/<type>s/<id>/`（如 type=model 时为 `data/models/<id>/`），再拼接剩余路径。
- 路径必须规范化后仍位于 `data/<type>s/<id>/` 内
- `id` 必须来自当前uid可见的索引记录，不能直接拼接用户输入
- 允许模型目录下任意相对路径，但扩展名必须在 config 白名单中

2.错误码定义
  `400`：参数缺失或非法（包括 id/name/uid 解析失败） `404`：资源不存在、已删除或对当前uid不可见 `403`：路径非法，或上传/删除时uid与资产的upload_user_id不匹配，或类型不允许下载 `500`：服务器内部错误

`path` 扩展名不在 `allowed_extensions` 中时，返回 403。
`path` 为空或缺失时，返回 400。
文件不存在时，返回 404。

错误响应统一为 JSON：
   {"error": "not_found", "message": "..."}
   成功 JSON 响应 Content-Type 为 application/json; charset=utf-8。
   文件下载根据扩展名推断 Content-Type。

3.技术栈

使用python的http标准库实现

4.服务器config结构示意

```json
{
  "host": "127.0.0.1",
  "port": 8000,
  "data_dir": "./data",
  "allowed_extensions": [".obj", ".mtl", ".png", ".jpg", ".jpeg", ".urdf", ".stl", ".dae", ".gltf", ".glb", ".bin"],
  "downloadable_types": ["model", "robot"]
}
```

- `allowed_extensions` 是扩展名白名单，大小写不敏感。
- `downloadable_types` 指定哪些类型可以通过 /file、/fileByName 下载。

5.启动参数

```
python server.py --host 0.0.0.0 --port 8000 --data-dir ./data --config ./config.json
   默认 host=127.0.0.1，port=8000，data-dir=./data，config=./config.json。
   命令行参数优先级高于 config.json。如果 --config 指定的文件不存在，则尝试使用./config.json，如果依然失败，则启动失败并输出错误信息；如果文件存在但 JSON 格式错误，则启动失败并输出错误信息。
```

6.id 与 uid 类型约定：

1. metadata 中的 object_id / scene_id 为 int。
2. /list 返回的 items[].id 为 int。
3. 索引统一使用SQLite数据库，id列、upload_user_id列均为INTEGER，直接以int查询（旧JSON索引中by_id以str(id)为键的约定随索引重设计一并废弃）。
4. URL 查询参数 id 和 uid 是字符串，服务端收到后必须解析为 int，解析失败返回 400。
5. 不允许负数id，不允许负数uid；uid缺省为0。
6. 其他内部逻辑和 JSON 响应中，id 统一为 int。
7. 新上传资产的id分配（分区规则，`10000` 及以上为普通用户保留区；取值均在全部记录上进行，含已删除记录，id 绝不复用）：

   1. uid>0（非系统用户）：id 从 10000 开始逐渐累加。取该type索引中 `id >= 10000` 记录的最大 id+1；若不存在此类记录，则为 10000。
   2. uid=0（系统用户）：取该type索引中 `id < 10000` 记录的最大 id+1（若无记录则为 1）。若结果 >= 10000（即 10000 以下区段已用尽），则回退为该type全部记录的最大 id+1。
