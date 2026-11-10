# M4-1 —— TRAJ v2 解码器

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-09 |
| 落地 | 本卡提交 |
| 依赖 | M0-3 |

## 原因与做了什么（PLAN_STATE 备注）

新建 `editor/src/traj.ts`：`decodeTraj(buffer: ArrayBuffer): Traj`。`Reader` 类把所有读取集中在解码期且全部 `littleEndian=true`（§7.1），`require(offset,length,what)` 是唯一的越界闸门（坑：`ArrayBuffer.slice` 出来的副本才可比对，Node `Buffer` 共享池化 buffer，夹具读入必须显式复制到 detached `ArrayBuffer`）；magic 数值比对 `0x5452414A`（§7.2：落盘字节是 `"JART"`，测试同时断言前四字节 ASCII 是 JART 以钉死这个陷阱）；`version!==2`→`TRAJ_UNSUPPORTED_VERSION`、`pose_dim!==7`→`TRAJ_POSE_DIM_UNSUPPORTED`（逐物体判，报错带 object id）；Data 起点 = `attach_offset + 16*attach_count`（头部不存，只能派生，Q32 归一化）；段长 = `frame_count*pose_dim*4`，位姿用 `new Float32Array(buffer, dataOffset, count)` 零拷贝窗口（§8.8，测试断言 `poses.buffer === 传入的 buffer`；顺带把 `data_offset % 4 !== 0` 挡在 `Float32Array` 抛错之前）。错误码划分见 Q30：字节层面（区域越界、未对齐）一律 `TRAJ_TRUNCATED`，语义层面（`fps=0`、`index_offset<32`、`attach_offset<index_offset`、索引区末端越过 `attach_offset`、`start_frame+frame_count>total_frames`）用通用 `INVALID_PARAM`，不新增 §5.13 之外的码（未知码在 gateway 会掉到 502）。`0xFFFFFFFF` 父级 → `parentId:null`（§2.5，与 `world.ts:SCENE_ROOT_SENTINEL` 同语义，解码器不引 world.ts 以免模块耦合）；`attaches` 解码期按帧升序排（§8.6 重放依赖它，不信任写入端）；`steps[]` 是 `step_count` 的闭区间聚合视图（同 step 的空隙帧归入该 step，遇不同 step 断新 run，同一 step 号可出现两次，Q31），命名一律 camelCase 而 §5.12 的 wire 形状仍 snake_case，由 M4-2 显式映射。§8.2 的 `ignored_objects` 与 §8.3 的机器人判定有意不进解码器（都要当前世界，属 M4-2/M4-3），所以 `decodeTraj` 只有 buffer 一个参数。测试：新建 `editor/src/traj.test.ts` 19 例（DoD 六条：`fps=60/total_frames=720/object_count=3/attach_count=2`、机械臂九段的 `startFrame/frameCount/dataOffset/step` 全表、苹果与盒子 `segments:[]`、attach 帧 340/530 且 parent 分别 1/3、末段末帧与首段首帧逐分量等于默认位姿（循环无跳变）、`duration_seconds=12`、`byteLength=12088`、`dataStart=328`；负例全部靠 `patch()` 改夹具单个字段生成真实畸形文件：magic+1、version=3、pose_dim=6、截到 31/300/12080 字节、`data_offset=99999`、`data_offset=329` 未对齐、四个头字段自相矛盾、`total_frames=100` 造成帧越界、attach 帧乱序仍排好；`stepsFrom` 另用合成物体测排序/断 run/合并三种情形）。验证：`npx tsc -p editor/tsconfig.json --noEmit` 0 错（`noUncheckedIndexedAccess` 打开，测试里的下标读取需 `need()` 守卫）、`npx vitest run --config editor/vitest.config.ts editor/src/traj.test.ts` 19 例绿、`npm test` 141 例 / 8 文件、`npm run build` 通过。本卡是纯模块，无 gateway/UI 改动，故不做真机冒烟（`/updateTraj` 与二进制分帧在 M4-2）。float32 陷阱：`0.35` 落盘是 `0.34999999…`，断言按 `toFixed(4)` 比而不是 `toEqual` 原值

## 任务卡原文

#### M4-1 TRAJ v2 解码器
- **规格**：§8.1、§2.5
- **涉及**：新建 `editor/src/traj.ts`；夹具 `tests/fixtures/robot_grasp.traj`
- **改动要点**：`decodeTraj(buf: ArrayBuffer): Traj`；`new DataView` 全部 `littleEndian=true`；magic **数值比对 `0x5452414A`**；`version!==2` → `TRAJ_UNSUPPORTED_VERSION`；`pose_dim!==7` → `TRAJ_POSE_DIM_UNSUPPORTED`；越界 → `TRAJ_TRUNCATED`；Data 起点 = `attach_offset + 16*attach_count`；段长 = `frame_count*pose_dim*4`；`Float32Array` 零拷贝窗口（§8.8）；输出 `objects[](含 defaultPose、segments)`、`attaches[]`、`steps[]`。
- **DoD**：`traj.test.ts` 用 fixture 断言：`fps=60/total_frames=720/object_count=3/attach_count=2`、机械臂 9 段首段 `start_frame=120/frame_count=70/data_offset=328/step=1`、苹果与盒子 `segment_count=0`、attach 帧为 340/530、末段末帧值等于默认位姿。
- **验证**：`npx vitest run --config editor/vitest.config.ts editor/src/traj.test.ts`。
- **已完成（2026-10-09）**：命名落地为 camelCase（`totalFrames/defaultPose/startFrame/dataOffset`），§5.12 的 wire 形状仍 snake_case，由 M4-2 显式映射（**Q31**）；错误码划分见 **Q30**，解码期归一化与"为什么 `decodeTraj` 只有 buffer 一个参数"见 **Q32**。状态与长注见 §9 M4-1 行。
