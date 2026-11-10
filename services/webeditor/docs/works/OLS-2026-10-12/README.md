# OLS-2026-10-12 —— OLS-2026-10-12

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-12 |
| 落地 | 本卡提交 |
| 依赖 | M6-2,M7-3,M7-4 |

## 原因与做了什么（PLAN_STATE 备注）

维护卡（非 §2 任务卡）：上游从 stub 换成真实 OLS（`F:\ObjectLibraryServer`）＋ `docs/场景文件描述.md` 2026-10-12 修订落地。决议记在规格 §13.19（Q66/Q67），并关闭 §13.2 O7/O8；§6.1/§6.2/§6.11 三处正文、§12.7 验收、本文 §1.4 与 §5 坑 8 同步改写。代码四处：① `library.ts:resolveMetadataTarget(path, type)` —— `MetadataRef.properties.type` 优先决定 id 空间，缺失才退回 `path` 形式（裸数字为 model、`robot?id=` 为 robot），非法 `type` 只告警并丢弃该字段（§3.4 不中断）；② `world.ts` 把 `RobotAttachment` 收进 `KNOWN_COMPONENT_TYPES` 并新增 `applyAttachmentPose()` —— `transform.position/quaternion_xyzw` 覆盖物体 `Transform`（`scale`/`parent` 保留），`parent_object_id` 非法或与 `Transform.parent` 不一致、或缺 transform 时三种情况一律回退普通 `Transform` 并各报一条；不改成"挂到 link 节点"的理由写在函数注释里（父级已是机械臂物体，改挂会把 FK 算两次）；③ `drivers.ts:applyInitialJointPositions()` 新导出 + `renderer.ts:buildRobot()` 调用，`initial_joint_positions_rad` 在 URDF 树建好时逐关节落地（未知名报一次、非有限值跳过、与播放共用 `joint_limits` 限幅），并进 `populate` 的签名第 6 位，改停放位姿会重建；`assets.ts:objectForItem` 面板写入时一并落 `type`；④ `renderer.ts:overrideAssetColor` 支持 `#RRGGBBAA`（后两位进 `opacity`，小于 1 才置 `transparent`），告警文案改为 `is not a #RRGGBB or #RRGGBBAA colour`。路由对账：前端在册七条 GET 逐条命中 `server.py:ROUTES_GET`；类别列表路由按 O7 定稿为 `/listCategory`（`library.ts`、`main.ts`、`assets.ts` 与注释一致）。真实 OLS 用 SQLite（`data/<type>_index.db`）而非 `*_index.json`，`/model` 与 `/robot` 已把 `<name>.json` 与 `<name>_files.json` 合并返回，`thumbnail` 相对资产目录 —— 这三条各改规格 §6.11/§6.2 与 O8 一处，前端代码无需改动（它从不拼服务端路径）。stub 退役：`git rm tests/fixtures/ols_stub.py tests/test_ols_stub.py`；`tools/serve_verification.py` 改为按 `--library-dir`／`$OLS_DIR`／同级 `../ObjectLibraryServer` 定位 OLS，端口空闲才 `python server.py` 子进程起、`finally` 只 terminate 自己起的，被占用则复用（绝不 kill 别人进程），gateway 临时根与 `X-Standalone-Verification` 守卫不变；`start-dev.ps1` 同形（新增 `-LibraryDir`，复用→拉起→否则 `Write-Warning` 指路）；`README.md` 的 Start/Models/Verification 三段、`tests/fixtures/README.md` 与 `tools/browser_smoke.cjs` 头部随之改写。测试：`world.test.ts` 新增四组共 10 例，其中一组直接读 `docs/ur5e_table_empty.json`（五个 `metadataRef` 的 `{path,type}` 原样入内存、夹爪 driver 字段、`toJSON()` 往返）；`library.test.ts` +3（type 对裸数字、type 压过已替换的 routed URL、非法 type）、`drivers.test.ts` +4（落地/限幅/未知名/非有限值）、`renderer.test.ts` +3（`#RRGGBBAA` 透明度、按 type 选空间的请求序列 `robot:3 model:29 model:7`、停放位姿真进关节）。验证：vitest 234 → 254 例 / 12 文件全绿、`npx tsc -p editor/tsconfig.json --noEmit` 0 错、`python -X utf8 -m unittest discover -s tests` 109 → 102 例 OK（减掉的正是退役 stub 的 7 例）、`cmp types.ts contracts/types.ts` 无输出、`node --check tools/browser_smoke.cjs` 与 `py_compile`/PowerShell 解析全过。三点遗留（不在本卡偷偷修）：① `tools/browser_smoke.cjs` 的 §12.1 请求集合与 §12.4/§12.6 的厘米级数字是按 stub 生成库（model 0/2、robot 0、`obj_cube(0.08)`）量的，真实库没有 model 0 且尺寸不同 ⇒ 首跑必须对着实际安装的库重标定，脚本头部已写明"未重标定前红了不代表前端有 bug"；② §6.9/Q5 规定的 `library_via_proxy` 转发（`/api/library`）在 `local_gateway.py` 里从未实现 —— 这一条已由 Q68 以退役代理形式结案：配置、契约与 `library.ts` 都不再保留该键，OLS 的 CORS 头因此成为硬前置；③ `docs/ur5e_table_empty.json` 里夹爪的 `joint_sequence`（`robotiq2f85_base`、`left_finger`、`right_finger`）在真实 `robotiq_2f85.urdf` 中一个都不存在，命中的是"每个未知名报一次并跳过"分支 —— 属上游文件与资产库的命名分歧，改代码只会把矛盾藏起来。本地 gitignored 的 `tests/fixtures/ols_data/` 是 stub 时代残留，不入库也不强行删（可逆优先）

## 任务卡原文

（本卡在原 `Web前端实施计划.md` §4 无独立卡体，决策与验收记录散见于当时的规格决议；原文已整体归档至 `docs/works/历史档案/`。）
