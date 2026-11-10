# FEAT-2026-10-16-C —— 播放条撤掉 Stop 按钮与连续 RTF 输入

> 历史任务卡，由 `docs/Web前端实施计划.md` 于 2026-11-05 迁入。新任务请按 `docs/works/README.md` 的制度新建文件夹。

## 进度

| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-10-16 |
| 落地 | 本卡提交 |
| 依赖 | M4-4,M6-3,FEAT-2026-10-16-B |
| 决议号 | Q88、Q89（决议原文见 `docs/works/历史档案/` 中归档的需求规格 §13） |

## 原因与做了什么（PLAN_STATE 备注）

FEAT-2026-10-16c 新卡（第 ⑯ 张）：播放条撤掉 Stop 按钮与连续 RTF 输入（Q88）+ 样例里的苹果改挂夹爪夹持中心（Q89）。用户原话：「不再允许用户手动调整 RTF 参数，删除 Stop and leave 按钮，能否把苹果挂载位置改到夹爪末端的中心（即正常抓取时的位置）」；决议在规格 §13.22，就地修订 §7.1/§7.3/§9.3 与 Q42 正文。Q88：删 `data-timeline="stop"` 与 `label.timeline-rtf`/`#timeline-rtf` 及 `.timeline-rtf` 两条 CSS；连带删 `stopPlayback()`（函数体就是 `setTrajectory(null)`，Q76 的写入共存与手动 Load Scene 已覆盖 ⇒ 撤控件不撤能力）与 `syncRateControls()`（只剩一个 `<select>`），`setPlaybackRate` 的非法值分支一并删（五档产不出非法值）但保留"改速率重新锚定时钟"；`playback.play`/`playback.rate` 两键保留，`features.json` 仍 78 键、`literal` 判据不变。Q89：苹果携带段改成 `robotiq_2f85_right_driver` link 局部系 `(0, -0.017507, 0.101727)`（挂点由 `attachTarget → endEffectorNode` = `joint_sequence` 末关节的 child link 决定；link 节点局部系 == URDF 系、OBJ 不做 Z_UP 补偿），并把 `GRIPPER_CLOSED` 0.72 → 0.130556（指垫间距 0⇒85.40 / 0.13⇒74.70 / 0.72⇒8.64 mm vs `apple.obj` 74.66×74.73×71.25 mm，0.72 会让手指长在苹果里）。验证：真机 `bar hosts` 只剩 `play/prev/next/timeline-range/timeline-rate/loop`、倍速仍生效、`pageerror` 空；帧 700 苹果世界盒整体落在夹爪盒内且共用极值 x 面（在指尖端），帧 620/800 均回 home `[380.2,-220,635.6]` mm；三张出图（俯视 30 苹果在桌 → 俯视 700 桌上已无苹果 → 腕部相机苹果顶从指节后沿探出）。回归链全绿：`npm run build` ✓、vitest 329、python 158、`audit_parity.py` 52/52、`cmp types.ts contracts/types.ts` ✓、全仓 `grep timeline-rtf\

## 任务卡原文

### FEAT-2026-10-16c —— 播放条撤掉 Stop 按钮与连续 RTF 输入（Q88）+ 苹果改挂夹持中心（Q89）（新卡；依赖 FEAT-2026-10-16b、M4-4、M6-3）

- **起因（用户原话）**：「不再允许用户手动调整 RTF 参数，删除 Stop and leave 按钮，能否把苹果挂载位置改到夹爪末端的中心（即正常抓取时的位置）」。三件事一次做完：撤控件（前端）、撤按钮（前端）、改挂载点（样例数据）。决议 **Q88/Q89** 记在规格 **§13.22**，并**就地修订** §7.1、§7.3、§9.3 与 **Q42** 的正文（Q42 里"预设下拉与连续输入框是同一个数的两个视图"那半句被收窄，"非法值只报错一次、不改播放状态"的口径保留）。
- **Q88 撤的是什么、留下什么**：`main.ts` 播放条模板删掉 `data-timeline="stop"` 按钮与 `label.timeline-rtf`（连同 `#timeline-rtf` 输入框），`styles.css` 删 `.timeline-rtf` 两条规则（含窄视口那条 `display:none`）。连带删两个函数：`stopPlayback()`（它整个函数体就是 `setTrajectory(null)`，而这条路径已由 **Q76** 的 `applyWriteCoexistence()` 与手动 Load Scene 覆盖，撤控件**不撤能力**）、`syncRateControls()`（只剩一个 `<select>`，没有第二个视图要同步）。`setPlaybackRate` 的"非法值只报错一次"分支一起删 —— 五个档位产不出非法值；**"改速率要重新锚定时钟"保留**（`anchorWallMs`/`anchorSimulationTime` 重写，Q42 的另一半）。`playback.play`/`playback.rate` 两个 feature 键**都留**：键是宿主给的开关面，撤的只是本机控件，`features.json` 仍是 **78 键**、`feature-inventory.csv` 的 `literal` 判据不变。
- **Q89 挂载点怎么来的**：挂点是 `robotiq_2f85_right_driver` link 局部系的 `(0.0, -0.017507, 0.101727)`（单位四元数），来自对真实 URDF 的一次 FK 行走（`scene5probe\grasp_fk.py` + `grasp_gap.py`，按 **Q69** 派生 `<mimic>`、取 `silicone_pad.obj` 贴物面顶点均值作接触面中心、两侧镜像 ⇒ 中点必在对称面上）。**同时把 `GRIPPER_CLOSED` 从 0.72 改成 0.130556**：指垫间距 `0 ⇒ 85.40 mm`、`0.13 ⇒ 74.70 mm`、`0.72 ⇒ 8.64 mm`，而 `apple.obj` 是 `74.66 × 74.73 × 71.25 mm` —— 仍按 0.72 闭合会把整副指垫吞进苹果里，"闭合"必须合到苹果上。0.72 那个值是本卡发现的**上一版真实缺陷**（也是旧脚本把两个 link 原点当接触点的后果，见 **坑 56**）。
- **验证（真机，同一批一次性栈）**：播放条实测 `bar hosts: ["play","prev","next","timeline-range","timeline-rate","loop"]`（stop 与 rtf 已不在 DOM），倍速 `<select>` 仍能改速率，`page errors: []`。帧 700 的挂载判据用"包含 + 极值面"：苹果世界盒 `[-446.8,337.7,664.1]…[-338.6,448.6,749.6]` 整体落在夹爪世界盒 `[-446.8,209.6,631.1]…[-250.6,448.6,782.1]` **之内**且与它共用极值 x 面（＝在指尖那一端，不是挂在基座上）；帧 620 与帧 800 苹果中心同为 `[380.2,-220,635.6]` mm（离桌与回桌都对）。出图三张：俯视 `grasp-30.png`（苹果在桌上、手指张开）→ 俯视 `grasp-700.png`（**桌上那颗红苹果已经不在了**）→ 腕部相机 `grasp-700-wrist.png`（苹果顶从指节后沿探出）。**没有用"两盒中心距离恒定"当刚体判据**（实测 76.4/77.7/72.3 mm 抖动，原因见 **坑 57**）。
- **验证（回归链，全绿）**：`npm run build` 干净、`npm test` **329 例**、`python -X utf8 -m unittest discover -s tests` **158 例**、`python -X utf8 tools/audit_parity.py` **52/52**、`cmp types.ts contracts/types.ts` 一致；`docs/parity-probes.json` 随探针重生成（只有时间戳与 uptime）。全仓 `grep timeline-rtf|stopPlayback|syncRateControls` 只剩文档行，代码与 `tools/browser_smoke.cjs` 零残留（smoke 本来不碰播放条控件）。
- **明确不做**：不改 `types.ts`/`contracts`（撤的是 DOM 控件不是契约字段）；不动 §8 禁令文件与 OLS 资产数据；`.traj` 只换苹果携带段那 7 个浮点与 step 7/9 的夹爪值，**帧数、段表、Attach 帧号（630 挂 / 750 卸）一字不改**。
- **一轮收尾**：一次性栈起的 gateway 8099 与 vite 5188 逐个 `taskkill`（**坑 55**），`netstat` 确认无监听；只本地提交、**不 push**；`examples/gateway_client_demo.py` 里用户自己的 `SPIN_TICK_HZ = 30.0` 改动不进本提交，两份用户自有未跟踪文档同样不进。
