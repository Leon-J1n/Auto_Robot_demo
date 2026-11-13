# 2026-11-12 PhysicalProperties 组件：文档同步、原样读、摆放/替换写入（Q165）

## 原因

用户口径：「OLS 中新增了一个场景组件 PhysicalProperties，把相关文档也同步到前端的相关文档中。此外，前端读取场景文件时，只把数据原样存进内存以备后续使用。此外，前端放置物体或切换机械臂、夹爪、相机时，需要给新物体加上此组件。此任务应当为 Q165。」

OLS 侧已落地两份文档（核对的是真实文件，不是散文摘要）：

- `ObjectLibraryServer/docs/资产说明md/场景文件描述.md` §4.10：组件示例、五字段表（`is_static`／`mass_kg`／`friction.format`／`friction.values`／`restitution`）、**继承与覆盖**段、**接入状态**段。
- `ObjectLibraryServer/docs/资产说明md/仿真模型JSON属性说明.md` §1.3：资产 `physical_properties` 的六字段（多一个 `scale`），并写明「场景 `PhysicalProperties` 组件可按实例覆盖 `is_static`、`mass_kg`、`friction`、`restitution`……`scale` 仍表示资产允许缩放范围，实例缩放使用 `Transform.scale`」。

两份合起来确定组件的**全部**可能属性就是 `is_static`／`mass_kg`／`friction{format,values}`／`restitution`，**不含 `scale`**。再逐物体核对 OLS 自己的场景文件（`data/scenes/2`）：每个模型/机器人实例的 `PhysicalProperties.properties` 恰等于该资产 `physical_properties` **逐字删去 `scale`**——这就是前端写入要照抄的口径。

三条子要求各自的落点：

1. **文档同步**：把 §4.10 搬进前端 `docs/场景文件描述.md`，并在需求规格组件表与 §4.6.1 立判据。
2. **读取原样存**：§3.4/§3.8 的未知组件保留路径**已经**满足——`world.ts:normalizeObject` 把整份 `components` 原样挂上 `rawComponents`（不裁剪、不换算），`toJSON()` 逐字节回吐。`PhysicalProperties` 不在 `KNOWN_COMPONENT_TYPES`（九类）里，正好走这条「告警但保留」的路。OLS §4.10 接入状态原文也是这么要求的：「尚未接入此组件的解析器按未知组件规则保留原始数据」。⇒ **读取端零代码改动**，只补一条钉住它的用例。
3. **摆放/替换写入**：`placement.ts` 的建造器与替换补丁此前不写这枚组件，需要补。

## 做了什么

1. **规格就地改** `docs/Web前端需求规格v3.md`：
   - §3 组件表 `LocalAxes` 行后新增 `PhysicalProperties` 行（五字段、只存只写不消费、读取走 §3.4/§3.8、写入=资产逐字删 `scale` 合并覆盖、开放联合无需改 types）。
   - §4.6.1「一切写都是 ② 类」后新增 **Q165** 条：摆放/替换一律带组件、逐字删 `scale`、合并保留作者键、资产无声明也写 `{}`、不按角色猜 `is_static`、读取端零改动、`remountGrippers` 不碰。
   - §12 验收新增 **12.48**（六条：原样读、摆放即带、替换刷新且保留作者键、不按角色猜、负控制、范围）。
2. **场景文档同步** `docs/场景文件描述.md`：插入 §4.10（示例 + 五字段表 + 继承与覆盖逐字搬自 OLS），**接入状态改写为前端 Q165 口径**（只存只写不消费 + 读取/写入两条 + 落点）；§3 那条「第十类才走这条忽略路径」就地坐实为 `PhysicalProperties`，§4 表下注补一句它同样不在样本里、走保留路径。**不搬** OLS 那份里残缺的 `| LocalAxes |` 表行与多余空行。
3. **代码** `editor/src/placement.ts`：新增纯函数 `physicalProperties(asset, node?)` = `{...实例已有同名组件 properties, ...structuredClone(资产 physical_properties) 删 scale}`；四个建造器（`armComponents`/`gripperComponents`/`propComponents`/`pedestalComponents`）各追加一项；`placeAsset` 相机替换、`placeArms`/`placeGrippers`/`placePedestal` 的替换补丁各带上它（替换传 `node` 以合并保留作者键）。`world.ts`、`types.ts`/`contracts/types.ts` **一字未动**。
4. **测试**：
   - `world.test.ts` +1：`PhysicalProperties`（含嵌套 `friction` + 作者自定义键 `curator`）原样入 `rawComponents` 且 `toJSON()` 回吐逐字相同。
   - `placement.test.ts` +7（新 describe「PhysicalProperties on placed and swapped objects」）：道具逐字删 `scale`、资产无声明写 `{}`、臂/底座各按自己资产、换臂刷新且保留作者键、场景文件没带时替换补上、换爪+换两台相机、换相机不动 `Camera`/`Transform`；另把 5 处既有 `componentTypes(...)` 断言追加 `PhysicalProperties`，9 枚夹具补 `physical_properties`（逐字抄 OLS）。
5. 本卡、`docs/works/INDEX.md` 顶行、`docs/works/坑清单.md` 第 **138** 条。

## 不做

- **不把 `PhysicalProperties` 加进 `KNOWN_COMPONENT_TYPES`**：本轮要的是「原样存备后续使用」，不是建类型化视图/消费它。保留路径已满足，加进去反而偏离 OLS 接入状态口径。
- **不消费**这枚组件：不做物理模拟、不改渲染、不据 `is_static` 固定任何物体。
- **不按角色猜 `is_static`、不补默认值**：一律取自资产；缺省继承由消费端按 OLS 约定处理。
- **不动 `remountGrippers`**：同一资产重挂，物理属性不变。
- **不删 `pedestalComponents`**：它虽被导出却无人调用（`placePedestal` 内联补丁），本轮为 coherence 同步更新了它，删除属于另一轮清理。
- **不动 `types.ts`/`contracts/types.ts`**：`ComponentType = KnownComponentType | (string & {})` 是开放联合，`PhysicalProperties` 作为字符串字面量直接合法。
- **不修一次性网关 `/getWorld` 的 503/504**：坑 131 同族的基础设施读超时，Q165 不碰任何 `/getWorld` 路径。
- **不搬** OLS 文档里残缺的 `| LocalAxes |` 表行与 cosmetic 空行。

## 验证

| 命令 | 结果 |
|---|---|
| `npx tsc -p editor/tsconfig.json --noEmit` | 0 错 |
| `npx vitest run --config editor/vitest.config.ts` | **760 / 24 文件全绿**（`world.test.ts` 67，+1；`placement.test.ts` 78，+7） |
| `npx vite build --config editor/vite.config.ts` | JS **909.37 → 909.75 kB**（+0.38，gzip 251.28 → 251.39，+0.11）；CSS 21.99 kB 不变 |

**负控制两条**（各自只删一行，跑 `placement.test.ts`，恢复后 `md5sum -c` 校验和一致）：

1. 删 `propComponents` 里的 `physicalProperties(asset),` ⇒ **恰 3 红**，全是道具路：`clicking a 模型 … adds it resting on the top`（组件表少一项）、`switching the 夹爪和相机 … stands a camera on the table when the scene has none`（相机-as-道具组件表少一项）、`PhysicalProperties … stands a prop with the asset's own numbers`（`expected {} to deeply equal {is_static:true,…}`）。臂/爪/底座/替换用例一律绿（各用自己的建造器）。
2. 删 `delete copy.scale;` ⇒ **恰 3 红**，全是「无 scale」断言：道具逐字、换臂刷新、换爪/换相机（`scale` 漏进 `properties`）。其余绿。

**真浏览器**（一次性栈：gateway 8098 + 自启 OLS 8096 + `dist/`，跑完已停，两端口确认 free）：

- **读取原样**：载入预设场景 2（苹果夹取，9 物体），`/getWorld`（带 `X-WebEditor-Browser-ID`）回读——7 个模型/机器人实例（id 1–7）各带 `PhysicalProperties`，两盏灯（id 0/8）没有；工作台 id1 的 `properties` 与资产 29 `physical_properties` 删 `scale` **逐字相同**（`is_static:true, mass_kg:22, friction{…}, restitution:0.05`）。
- **换臂 → robot5（UR3）**：toast「已把机械臂换成「UR3机械臂」」；臂 id2 `properties` == 资产 5 删 `scale`（`mass_kg:18.4`）✓；工作台 id1 仍是自己的 22/static（没被误换）✓。
- **换爪 → robot10（2F-140）**：toast「已把夹爪换成「2F-140夹爪」」；爪 id3 == 资产 10 删 `scale`（`mass_kg:0.3638485410356241, restitution:0.08`）✓。
- **换相机 → robot11（D455）**：toast「已把 2 个相机换成「D455相机」」；两台 id4/id5 `ppMatch=true`，且 **`cameraUnchanged=true`、`transformUnchanged=true`、`nameUnchanged=true`**——Q165 写组件的同时，Q161「换相机只动引用、视点内参一字不动」的口径未被破坏 ✓。
- **摆放 → model1（苹果）**：toast「已把「苹果」摆到桌面上」；新物体 id9 `properties` == 资产 1 删 `scale`（`mass_kg:0.068`）✓。

console/network 噪声按坑 131 纪律**按 URL 排除**：`/events` 的 `Invalid frame header`、一次性网关首帧 `/getWorld` 的 503，以及重载几何时 `/getWorld` 的 504 读超时（同族，Q165 不碰该路径；探针用重试读拿到正确数据）。其余无 error。截图与探针存 `var/`，不提交。

**接口面**：纯浏览器写入改动，不新增路由、不改请求形状、不碰 gateway/OLS/AgentBrain ⇒ §5.9 路由清单与 `audit_parity.py` 探针不增不减；`types.ts`/`contracts/types.ts` 未动；不新增常量键或功能键。

## 进度

已完成；一个本地提交。