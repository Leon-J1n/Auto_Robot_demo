# DOC-2026-10-13 —— DOC-2026-10-13

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-13 |
| 落地 | 本卡提交 |
| 依赖 | MIMIC-2026-10-13 |

## 原因与做了什么（PLAN_STATE 备注）

维护卡（非 §2 任务卡）：把三份文档对齐到 2026-10-13 的真实数据与已实现行为，不动代码。规格 §2/§3：组件表补 `RobotAttachment` 行与 `MetadataRef.type`、`#RRGGBBAA`、`Light` 的 `-Z` 朝向口径、`RobotDriver` 的 `initial_joint_positions_rad`/`available_connect_joints`、Camera 缺省值；§2.4 改为“model/robot/scene 三套互不相通编号”、§2.8 删掉已不存在的 `rotationWxyz` 说法、§3.2 名称可缺省（回退 `object_<id>`）、§3.4 补重复组件取首个（O5）并纠正“未知字段忽略”与 §3.8 保留承诺的矛盾、§3.5 说明三种 id 形态都合法、§3.8 写明 `rawComponents` 与类型化视图都存整份 `properties`、§3.9 拆成四条路径讲清 `scene_version`（`/updateWorld` 是 `max(当前+1, 文件+1)`，手动 Load Scene 不自增）。 样例引用：§2.6/§2.8/§3.8/§6.1/Q1/Q3/Q10.4/11.3 里指向已删除的 `docs/scene_example.json` 的取值改为 `docs/ur5e_table_empty.json`，`$LIBRARY_SERVER/0` 换成真实 id 29 并注明实装库 model 从 1 起；Q22/Q24 补“已被 Q67 取代”。§8.4/O1 补 `<mimic>` 之后的可行性口径，O9 关闭（`MetadataRef.type` + `$LIBRARY_SERVER/robot?id=` 都已是实现事实并被 `docs/场景文件描述.md` §4.2 收编）。 §12.1 改为按实际取证写：真机 `browser_smoke.cjs` 的 §12.1 幕走手动 Load Scene 读 `docs/ur5e_table_empty.json`，`/updateWorld` 的字节级往返与拒绝路径由 `world.test.ts`/`scene_requests.test.ts` 用 `tests/fixtures/scene_example.json` 钉住。 `docs/场景文件描述.md`（用户维护的格式说明，只改与已实现行为/真实样本互相矛盾的行）：§2 的 id 说明改为“三套互不相通编号”，§4.5 补 `Spot` 与 Directional/Spot 沿局部 -Z 发光（完整枚举仍是 O4），§4.7 物体 3 改成真实关节名并解释 `robotiq_2f85.urdf` 只有左侧 4 条 `<mimic>`、右侧四连杆不会折叠，§4.8 `child_link` 改 `robotiq_2f85_mount` 并记下样本 `Transform` 已只剩 `scale`/`parent`，§6 事实段补 `MetadataRef.type` 的分布，第 5 行注明数值按 `087dc0e` 版样本核对。 验证：`npx tsc -p editor/tsconfig.json --noEmit` 0 错、`npx vitest run --config editor/vitest.config.ts` 260 全绿、`python -X utf8 -m unittest discover -s tests` 102 例 OK、`python -X utf8 tools/audit_parity.py` 52 PASS、`python -X utf8 tools/parity_inventory.py` 退出码 0、`cmp types.ts contracts/types.ts` 无输出、`npm run build` 通过，两份中文文档的 GFM 表格列数逐表核对无漂移

## 任务卡原文

（本卡在原 `Web前端实施计划.md` §4 无独立卡体，决策与验收记录散见于当时的规格决议；原文已整体归档至 `docs/works/历史档案/`。）
