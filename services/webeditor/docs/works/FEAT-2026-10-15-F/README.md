# FEAT-2026-10-15-F —— 样例新增动画步：用 `/updateItem` 把 `shoulder_pan_joint` 以 1 rad/s 转满一圈

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-15 |
| 落地 | 本卡提交 |
| 依赖 | M3-1,M5-*,M7-* |
| 决议号 | Q33、Q75、Q85（决议原文见 `docs/works/历史档案/` 中归档的需求规格 §13） |

## 原因与做了什么（PLAN_STATE 备注）

FEAT-2026-10-15f 新卡（第 ⑬ 张）：调用方样例新增动画步 —— 用 `/updateItem` 逐拍把 `shoulder_pan_joint` 以 1 rad/s 转满一圈（Q85）。用户原话：「继续拓展这个演示脚本，在演示脚本中让 UR5eArm 的 shoulder_pan_joint 以 1rad/s 的速度转一圈，你可以通过/updateItem 来实现这个功能」。巡回 14 → 15 步（新第 11 步），新增 `SPIN_*` 三常量与 `pick_object/driver_of/object_for_joint/box_shift/sweep_joint/spin_shoulder_pan`；分工：第 11 步是调用方喂值（每拍 bump 版本 + 一条 `scene.applied`），第 12 步 `/updateTraj` 是交给场景播放（不 bump、不广播），两条路由互为对照。时基取墙钟（`speed * (monotonic() - started)`，睡眠按截止时间 `max(0, frames*interval - elapsed)`）：一次 relay 往返 100~250 ms ⇒ 同一圈写 28~66 次（九次真机的区间，不写死进断言），而编排脚本独立读 `/getWorld` 得 +0.7/2.2/3.7/5.2 s = 0.601/1.923/3.443/5.083 rad ⇒ 0.88~1.09 rad/s，速度守住；撞 409 就停拍继续巡回。限位读场景（`drivers.ts:clampToLimit` 用 `joint_limits`，scene 6 的 ±6.28319 恰好容得下 2π，余量 5e-6 rad 是资产给的），容不下就 `RuntimeError` 停下，绝不把"转一圈"静默降级成"摆一下"。写入体现读现改 + 单键合并（Q75：一圈结束字典里仍恰一条）。几何取证：半圈/整圈/写回 home 三发 `/get2DAABB`，末轮 `

## 任务卡原文

### FEAT-2026-10-15f —— 样例新增动画步：用 `/updateItem` 把 `shoulder_pan_joint` 以 1 rad/s 转满一圈（新卡；依赖 FEAT-2026-10-15e、Q75、Q33、Q85）

- **起因（用户原话）**：「继续拓展这个演示脚本，在演示脚本中让 UR5eArm 的 shoulder_pan_joint 以 1rad/s 的速度转一圈，你可以通过 /updateItem 来实现这个功能」。顺序按既定口径：文档（**Q85** + §5.9 指针行 + README）→ 实现 → 测试 → 本卡 → 本地提交 → 停服务。
- **交付物**：`examples/gateway_client_demo.py` 从 14 步变 **15 步**（第 11 步是动画），新增常量 `SPIN_JOINT` / `SPIN_SPEED_RAD_PER_S = 1.0` / `SPIN_TICK_HZ = 10.0` 与六个函数 `pick_object` / `driver_of` / `object_for_joint` / `box_shift` / `sweep_joint` / `spin_shoulder_pan`；`tests/test_caller_demo.py` **20 → 29 例**（新增 `JointSweepTests`）；规格新增 **Q85** 一行、§5.9 的样例指针改写；README 的 "Caller routes (§5.9)" 段补一句"动画从调用方侧长什么样"。**前端源码与 gateway 零改动** —— 本轮所有结论都落在既有 §5.4/§5.7/§5.11 契约之内，样例仍是纯读者。
- **两条动关节的路由互为对照**（Q85 的主体）：第 11 步是"调用方自己按拍子喂值"（每拍一次整对象 `/updateItem`、每拍 bump 版本、每拍一条 `scene.applied`），第 12 步是"把一段烘焙好的动作交给场景"（一次请求、不改世界、不广播、进播放态）。样例因此把**同一台机械臂**用两种方式各动了一次，读者能直接比较代价。
- **时基取墙钟**：`sweep_joint` 每拍的角度是 `start + direction * min(travel, speed * (monotonic() - started))`，睡眠取 `max(0, frames * interval - elapsed)`（**截止时间**而非固定间隔）。理由与实测：一次 relay 往返在这台机器上花 100~250 ms，按拍计数会让"1 rad/s"随延迟漂移；按墙钟则掉拍只影响顺度。**同一圈写了 28~66 次**（九次真机巡回的区间），而编排脚本独立读 `/getWorld` 测得的角度在 +0.7/2.2/3.7/5.2 s 分别是 0.601 / 1.923 / 3.443 / 5.083 rad ⇒ 相邻段 **0.88 / 1.01 / 1.09 rad/s**，速度确实守住了。撞 409 `VERSION_CONFLICT` 时**停拍并继续巡回**，不跟并发写者抢版本。
- **限位读场景、不读 URDF**：`drivers.ts:clampToLimit` 用的是场景里的 `joint_limits`（scene 6 给 `[-6.28319, 6.28319]`，一圈 6.283185 恰好放得下，余量 5e-6 rad 是资产给的）。样例先 `/getWorld` 核一遍"从起点起容得下 2π"，**容不下直接 `RuntimeError` 停下**，绝不静默把"转一圈"降级成"摆一下"。
- **写入体一律现读现改 + 单键合并**：扫描前从 `/getWorld` 取回场上那份机械臂（不是内存里的嵌入副本），每拍只往 `now_joint_positions_rad` 合并 `shoulder_pan_joint` **一个键**，一圈结束字典里仍恰一条 —— 这就是 **Q75** 逐关节合并的形状，`test_object_for_joint_commands_one_joint_and_keeps_the_rest` 钉住"另一个关节保留、嵌入样例不被就地改、四个组件原样带回"。
- **"转过了"用几何取证**：半圈、整圈、以及"写回 home 再测一次"三个时刻各读一次 `/get2DAABB` 的 `aabb_world`。最后一轮真机：`|full - home again| = 0.0 mm`（转满一圈回到同一朝向，这是这步的硬判据）、`|half - full| = 0.173 m`（半圈确实不同）。**扫描前那一发只展示、不参与判据**（见**坑 50**），home 那一发放到扫描之后测，两端才都是就绪态。
- **起点取声明的 home，不取场上 `now`**（见**坑 51**）：`home_angle` 读 `initial_joint_positions_rad`，限位检查也按它算；扫完补**一次** `/updateItem` 把关节写回 home（这一发是测量不是动画）。这是本轮第二次真机才暴露的缺陷：第一次跑完 `now = 2π`，第二次就必须在 ±6.28319 里塞下 4π 而直接报错，一个该幂等的演示只能跑一遍。
- **巡回输出改为行缓冲**：`main()` 里加 `sys.stdout.reconfigure(line_buffering=True)`。此前编排脚本（仓库外 `scene5probe\run_demo.cjs`）解析 stdout 来掐截图时机，而 Python 的 stdout 在管道里是**块缓冲**，"step 11/15"要等进程退出才到达，四张截图全部落在巡回结束之后（见**坑 52**）。
- **本轮修掉的三处真机缺陷**：① `box_shift` 最初按 `[[min],[max]]` 写，真实形状是 `{"min":[x,y,z],"max":[x,y,z]}` ⇒ `zip` 迭代到键名字符串，第 11 步 `TypeError` 退出码 1（已用 `test_box_shift_reads_the_shape_get2daabb_actually_reports` 钉死 §5.11 的形状）；② 上面那条"第二次跑不起来"；③ 上面那条"扫描前的框当判据"，中途真为此写过 `settled_box` 轮询、被自己的真机数据推翻后**删掉**（连同两条单测），把结论留在坑 50。
- **验证**（一次性栈：gateway 8099 + OLS 8000 + vite 5188 + 一个 playwright tab）：**真机巡回九次、退出码全 0、页面零报错**，最后一轮 `now = {"shoulder_pan_joint": 6.283185307179586}`、`scene_version=72`、广播逐条带 `source_browser_id`；`/updateTraj` 那步仍如实打印"版本没动、无广播"。回归链：`python -X utf8 -m unittest discover -s tests` **122 → 131 例 OK**、`audit_parity.py` **52 PASS / gaps []**、`tsc --noEmit` 0 错、vitest **327 例 / 14 文件**全绿（前端零改动 ⇒ 无新增单测）、`npm run build` ✓ 8.49 s（`index-BInq6S3P.js` **861.06 kB** / `index-DlU2n5kl.css` 20.57 kB，chunk>650 kB warn 为既有）。
- **明确不做**：不给动画加 `--spin-hz`/`--speed` 之类的第二组旋钮（常量足够，且样例的价值在"照抄就能跑"）；不为了截图去改样例或 gateway；不把真机拍数写进任何断言（它是区间不是常数，写死必假）；不实现浏览器侧的插值播放 —— 那属于 `/updateTraj` 的语义，两条路由各管一段。
- **一轮收尾**：停掉本轮起的 gateway 8099 + OLS 8000 + vite 5188（`TaskStop` 只杀外壳，孤儿 node 按 `netstat` 的 PID `Stop-Process`），`netstat` 确认 8000/8099/5188 无监听；只本地提交、**不 push**。
