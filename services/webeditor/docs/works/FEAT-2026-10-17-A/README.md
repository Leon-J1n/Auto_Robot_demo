# FEAT-2026-10-17-A —— 段末幽灵：用 20% 不透明度预告「这一段结束时机械臂会在哪」

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-17 |
| 落地 | 本卡提交 |
| 依赖 | M4-1,M4-3,M4-4,FEAT-2026-10-16-D |
| 决议号 | Q96（决议原文见 `docs/works/历史档案/` 中归档的需求规格 §13） |

## 原因与做了什么（PLAN_STATE 备注）

FEAT-2026-10-17 新卡（第 ⑳ 张）：段末幽灵 —— 以 20% 不透明度预告当前段末帧的机械臂姿态（Q96）。规格新增 §7.3 七条口径 + §12.12 验收 + 决议 §13.26 Q96；`features.json` 79 → 80 键（`playback.ghostPose`）。「段」取 `stepsFrom` 并出的 3 个 `TrajStep`（120–339 / 340–529 / 530–689，即播放条上那三根刻度），不是 9 条段表条目，推翻只需换 `stepAtFrame()` 的输入。落点：`traj.ts:stepAtFrame`（与 `poseAtFrame` 同构地按 `start_frame` 升序走）、`drivers.ts:applyFrameJointPositions`（从 `driveRobot` 提出，真机与幽灵共用一份限位/联动，只有 `warn` 两份）、`renderer.ts:buildGhost/syncGhost/clearGhost/createGhost/ghostAsset` + `isEditorOnly()` 单条身份判据（不进 `nodes`/`robotTrees` ⇒ 层级树、Details、`/getWorld`、拾取、`worldAabb()` 全看不见它）、`main.ts:ghostTarget/updateGhostPose`（`showFrame()` 每帧重算，`setTrajectory()` 建与拆）。自查修掉一个真 bug：`robotDriver()` 返回 `undefined`，`!== null` 判空失效会把苹果当臂画（坑 20 同族）。新增 `坑 62`（世界与 clip 的 id 空间对不上时 Attach 重挂载能闭出父链环 ⇒ 页面栈溢出；`ghostPose:false` 下同样复现，故与 Q96 无关；运行时 Attach 没有环检测，本轮有意不补）。验证：`npm run build` 干净、`npx vitest run` 330 → 343 例（`traj` +5 / `drivers` +2 / `renderer` +6）、python 171 例（零改动）、`parity_inventory.py` 80 键 / 0 死键、`audit_parity.py` 52/52、`browser_smoke.cjs` 11 → 12/12（同一帧开关两侧 `/captureScene` rgb+depth 逐字节相同、`#viewport` 不同、`/getWorld` id 不变）。真机（一次性栈 8099 + 5188 + OLS 8000，取证在仓库外 `scene5probe\`）：0 帧两侧 `md5` 相同（首段前无幽灵），200/400/700 两侧均不同；推近后差值包围盒在同一段内恒定（130 与 200 帧同为 `(453,430,516,524)`＝钉在 339；345 与 400 帧同为 `(432,436,508,524)`＝钉在 529）。

## 任务卡原文

### FEAT-2026-10-17 —— 段末幽灵：用 20% 不透明度预告「这一段结束时机械臂会在哪」（Q96）（新卡；依赖 M4-1、M4-3、M4-4、FEAT-2026-10-16d）

- **起因（用户原话）**：「添加一个新功能：在每段轨迹中，以20%不透明度显示机械臂在这段轨迹中末尾的位置」。范围用 `AskUserQuestion` 确认为**只画当前段那一台**：「只显示当前段的末帧（1 个幽灵）」—— 不是三段末帧各摆一台。决议 **Q96** 记在规格 **§13.26**，正文新增 **§7.3** 的「段末幽灵位姿」七条口径与 **§12.12** 验收；`features.json` 由 79 键改为 **80 键**（新增 `playback.ghostPose`，与 `playback.stepMarkers`/`playback.attachMarkers` 同族：都是播放条上某一类可见物的开关），`docs/feature-inventory.csv` 与 `docs/parity-probes.json` 随之重生成。
- **「段」取的是刻度那条里的段，不是段表条目**：`docs/robot_grasp.traj` 里机械臂有 **9 条段表条目**，`traj.ts:stepsFrom` 把同号且相连的条目并成 **3 个 `TrajStep`**（120–339 / 340–529 / 530–689），播放条上那三根刻度就是它们 —— 幽灵与刻度共用同一个对象，用户眼里的「段」因此必然是画出来的那三格。**推翻路径只有一处**：若改判为「按段表条目」，只换 `stepAtFrame()` 的输入（把 `TrajObject.segments` 直接当步），`renderer`/`drivers`/`main` 三层一字不动。段间空隙归它所在的那一段（§7.6），首段之前（0–119）无段可预告 ⇒ 不画，末段之后（690–719）仍指最后一段（与 §8.5「保持最后一次采样」同源）。
- **为什么要第二台 URDF 实例**：另一条路是「把真机摆到那一帧、截图、再摆回来」，那要么污染渲染状态、要么每帧多一次全图渲染。幽灵是**同一份几何的第二个 `Object3D`**，`renderer.ts:cloneAsset` 连材质一起克隆，所以改透明度**碰不到**真机的材质（这条由单测钉住，因为 `urdf.ts:buildVisual` 本来就给每个 box/sphere/cylinder visual 新建材质，测试因此不需要任何网格文件）。代价是多一份节点，收益是它跟着相机、跟着光照、跟着真机的基座走，不需要额外同步。
- **它必须「不存在」**：幽灵**不进** `nodes`、**不进** `robotTrees`，于是层级树、Details、`/getWorld`、拾取射线、`worldAabb()` 天然看不见它；编辑层排除只在 `isEditorOnly()`（`renderer.ts:1032`）加**一条身份判据** `object === this.ghost?.root`，`/captureScene` 的 `hideOverlays` 因此自动覆盖它 —— 不写第二条判据，两条会漂移。`depthWrite=false` 不是可选项：半透明面若还写深度，会把自己挡住画成斑驳的块。
- **驱动只有一份实现**：`drivers.ts` 把 `TrajDriver.driveRobot` 的主体（`joint_sequence` 顺序、`joint_limits` 裁剪、`<mimic>` 联动）提成导出的 `applyFrameJointPositions(objectId, driver, pose, handles, warn)`，真机仍经 `TrajDriver` 调它、幽灵直接调它 —— **幽灵预告的必然是真机到得了的姿态**，限位与联动不存在两套实现。两份的唯一差别是 `warn`：幽灵传空实现，同一条越界提示已由真机报过一次，不重复刷屏。基座每帧由 `syncGhost()` 从真机节点重拷 `position/quaternion/scale`，所以 `/updateItem` 挪基座、Attach 换挂载点之后幽灵不会留在原地；`buildGhost()` 用 `ghostRequest` 令牌作废在飞的构建，换臂/清轨迹时晚到的那份自己 dispose。
- **自查出来的一个真 bug（已修）**：`ghostTarget()` 最初写的是 `world.robotDriver(id) !== null`，而 `World.robotDriver()`（`world.ts:419`）返回 `RobotDriverProps | undefined` —— 这条判据对**任何**有段的物体都成立，于是 clip 里第一个带段的物体（苹果）会被当成机械臂画幽灵。**症状是「画出一台不存在的手臂」或干脆不画，且不报错**（**坑 20** 同族：同一处判据被不同消费者读出不同结论）。
- **测试（vitest 330 → 343 例，全绿）**：`traj.test.ts` +5（段内 / 空隙 / 首段前 / 末段后 / 出厂夹具那三步的形状）；`drivers.test.ts` +2（`applyFrameJointPositions` 的裁剪与「warn 每条只报一次」、经 `TrajDriver` 与直接调的结果逐关节一致）；`renderer.test.ts` +6（20% 材质与真机材质互不污染且 `nodes`/`robotTrees` 不增、幽灵跟随基座但只转自己的关节、`worldAabb()` 不受它影响、并发与重复 `buildGhost` 只展开一次、世界换掉后节点与 `userData.ghost` 全清、非 URDF 资产只报一次「没有幽灵可画」）。
- **验证（真机，一次性栈 gateway 8099 + vite 5188 + OLS 8000，探针与截图在仓库外 `scene5probe\`）**：`ghost_evidence.cjs` 在**同一帧**开/关两侧各截一张 `#viewport` —— `md5(ghost-0.png) == md5(ghost-off-0.png)`（首段之前确实没有幽灵），200/400/700 三帧两侧均不同，两侧 `#panel-count` 恒为 6、层级树恒 6 行。`ghost_closeup.cjs` 滚轮推近后再拍一轮，差值包围盒落在臂上且**在同一段内恒定**：130 与 200 帧都是 `(453,430,516,524)`（幽灵钉在 339 帧）、345 与 400 帧都是 `(432,436,508,524)`（钉在 529 帧）—— 这是「当前段末帧」最直接的像素级证据；`side-130.png`/`side-345.png` 左右并排能看见那台半透明臂伸向不同的姿态。
- **§12.12 的现场配对写错过一次，已就地改口**：那条验收原先写「载入 `docs/scene_example.json` + `docs/robot_grasp.traj`」，而 ① `docs/scene_example.json` 已随 2026-10-12 的《场景文件描述》修订从 `docs/` 删除（`git log --diff-filter=D -- docs/scene_example.json` 可见），② 即便它还在，**Q62** 早就判过这两份文件的 id 空间「数字相同、语义不同」。幽灵的验收现场只能是**与 clip 同 id 空间**的那个世界，即 §12.4 与 smoke 用的 `clipWorld()`。规格正文已按此改写，并把「浏览器侧断言」从虚写的三条（在场/透明/拾取）改成实际落地的那三条：同一帧开关两侧 `/captureScene` 的 rgb+depth **逐字节相同**、`#viewport` 截图**不同**、`/getWorld` 的 id 列表不变。
- **顺带撞出来的既有缺口（记为坑 62，本轮不修）**：用 Load Scene 装 `docs/ur5e_table_empty.json`、再 Load Traj 装 `robot_grasp.traj`，页面抛 `RangeError: Maximum call stack size exceeded` —— 该 clip 的 Attach 是「child 2 → parent 1 / 3」，而那份世界里的 2 是机械臂、3 是挂在它 `wrist_3_link` 下的夹爪，重挂载正好闭合出环。以 `playback.ghostPose:false` 复现，确认与 Q96 无关（**取证前先怀疑自己的现场**，这一条当时把幽灵功能白查了一轮）。
- **验证（回归链，全绿）**：`npm run build` 干净（既有 chunk>650 kB warn 不变）、`npx vitest run` **343 例 / 14 文件**、`python -X utf8 -m unittest discover -s tests` **171 例**（python 侧本轮零改动）、`tools/parity_inventory.py` **80 键** / 21 本地路由 / 8 浏览器动作 / `features_unreferenced` 空、`tools/audit_parity.py` **52/52**、`node tools/browser_smoke.cjs` **12/12**（新增那条报 `{"frame":200,"sensorIdentical":true,"viewportDiffers":true,"objects":[0,1,2,3,4,6]}`）。
- **明确不做**：不给多臂 clip 画多台幽灵（§7.3 第 7 条，一台）；不给幽灵加描边/配色/重合淡入淡出（20% 本身就是区分）；不让苹果、盒子与 Attach 事件作用于幽灵；不在运行时给 Attach 重挂载补环检测（那是 §5.3 的拒绝语义，属接口变更，见坑 62 与确认项）；不改 §5.9 任何接口（parity 判据不增不减，`/getWorld` 查不到幽灵正是这条判据）；§8 禁令文件一字不改。
- **一轮收尾**：本轮起的一次性栈（gateway 8099、vite 5188、它拉起的 OLS 8000）逐个 `taskkill` 并 `netstat` 确认（**坑 55**）；用户自己的 5173/8080 一律不停，但提醒：`features.json` 只在启动时读一次（**坑 22**），要让 8080 那个 gateway 认得 `playback.ghostPose` 得自行重启；只本地提交、**不 push**；`examples/gateway_client_demo.py` 里用户自己的 `SPIN_TICK_HZ = 30.0` 改动与三份用户自有未跟踪文档不进本提交。
