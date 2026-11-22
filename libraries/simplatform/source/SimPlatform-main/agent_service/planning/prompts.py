from __future__ import annotations

import json


TASK_UNDERSTANDING_PROMPT = """解析机器人任务，只输出规定 JSON。
支持 locate/search/move/grasp/release/pick_and_place/press/open/close。
open/close 的 target 是门或抽屉，reference 是对应把手；不要把 open/close 当成底层控制指令。
必须区分 motion direction 与 entity spatial selector。
motion direction 仅允许 left/right/front/back/up/down；东=right、西=left、南=back、北=front，并写入 raw_direction。
机械臂“向左上方移动”等复合 motion direction 返回 direction_clarification_required。
实体描述中的“左边/左上角/最右边/右下角”不是 motion direction，不得触发 direction_clarification_required，必须写入 scope=selection 的 relations。
二维场景角落使用现有 relation 组合表达：左上角=left+front、右上角=right+front、左下角=left+back、右下角=right+back；这里的上/下是平面前/后，不是 Z 轴 above/below。每条 relation 的 subject 必须是被修饰实体。
一句话可以同时包含实体 selector 和 motion direction，例如“把左边的棒球向右移动”应给棒球 selection relation=left，并给 operation 的 raw_direction=right。
相对定位（如“在机械臂末端左上方找个点”“在盒子右侧找个位置”）使用 locate operation 加现有 selection relations 表达，不填写 raw_direction，也不要输出世界坐标。
不支持的任务返回 unsupported_task。
禁止 explanation、operation_id、XYZ、object_id、模型信息、技能步骤和 task_types。

对常见搬运指令必须返回 accepted，并把动作拆成一个 pick_and_place operation。
例如“把红色方块放进蓝色盒子”应返回：
{"status":"accepted","entities":[{"id":"red_block","name":"红色方块","category":"object","color":"red"},{"id":"blue_box","name":"蓝色盒子","category":"container","color":"blue"}],"operations":[{"type":"pick_and_place","source":"red_block","destination":"blue_box"}],"relations":[],"raw_direction":null,"raw_task":null}
例如“打开柜门”应返回 open operation，其中 target 是 cabinet_door、reference 是 cabinet_handle。
实体 id 使用简短稳定的 snake_case；source/destination/target/reference 必须引用 entities 中的 id。"""


VISION_GROUNDING_PROMPT = """按实体语义返回 RGB 中所有相关候选 bbox。
bbox=[ymin,xmin,ymax,xmax]，整数范围 0..1000；同一 entity 可以有多个候选。
只输出 entity 和 bbox；不要输出 detection id、object_id、世界坐标、动作、置信度或解释。"""


SKILL_PLANNING_PROMPT = """你是机器人高层技能规划器。

根据 operations 中的高层任务目标、task-relevant entities 的 category/affordances/semantic regions，以及 Atomic Skill Catalog 中每个技能的语义、preconditions 和 effects，自主组合 Atomic Skills 完成每个 operation。

规则：
- high-level operation 只是目标，不是 Atomic Skill；不要假设任何预定义的高层任务 recipe；
- 只能使用 Atomic Skill Catalog 中存在的技能，使用前必须满足其 affordance 和 preconditions；
- 必须保持 operation 顺序及其依赖顺序；
- target/reference/source/destination 只能引用当前 operation 的角色；
- 不要重新解释或修改给定 operation type；
- 不要输出 object_id、step_id、depends_on、XYZ、关节角、轨迹、距离或解释；
- 输出必须严格满足 SkillPlan LLM schema，且必须覆盖 operations 中的每一个 operation；
- 顶层始终是一个对象，唯一字段为 operations，其值是 operation 计划数组；每项形如 {"id":"op-...","steps":[{"skill":"...","target":null,"reference":null,"region":null}]}。"""


def prompt_payload(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
