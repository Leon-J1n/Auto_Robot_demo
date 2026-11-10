# 错误清单（静态扫描）

范围：atomic_action、atomic_skill、planning、kinematics、demo、execution、sensors、control，以及 OLS 客户端。未包含资产、第三方 libraries、测试代码及第三方 SDK 内部异常。

错误码包含实际使用和枚举声明；“仅声明”不代表当前演示流程一定会触发。下方同时列出直接 raise 的原始信息，保留动态字段表达式，避免把不同原因合并成笼统报错。

## 错误码

| 错误码 | 扫描结果 | 代表位置 |
| --- | --- | --- |
| `ACTUATOR_TRACKING_FAILED` | 代码中使用 | [control/pull_push_controller.py:443](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:443) |
| `CAMERA_MODEL_UNAVAILABLE` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:16](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:16) |
| `CAMERA_NOT_MOVABLE` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:17](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:17) |
| `CLEARANCE_INSUFFICIENT` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:34](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:34) |
| `COLLISION_DETECTED` | 代码中使用 | [planning/direct_planner.py:48](C:/Users/21474/Desktop/robot-agent/planning/direct_planner.py:48) |
| `COMMAND_EXECUTION_FAILED` | 代码中使用 | [atomic_skill/pull.py:271](C:/Users/21474/Desktop/robot-agent/atomic_skill/pull.py:271) |
| `CONTACT_LOST` | 代码中使用 | [control/pull_push_controller.py:899](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:899) |
| `CONTACT_NOT_ESTABLISHED` | 代码中使用 | [control/pull_push_controller.py:567](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:567) |
| `CONTACT_PATH_COLLISION` | 代码中使用 | [atomic_skill/pick.py:23](C:/Users/21474/Desktop/robot-agent/atomic_skill/pick.py:23) |
| `CONTACT_PATH_PLANNING_FAILED` | 代码中使用 | [atomic_skill/pick.py:24](C:/Users/21474/Desktop/robot-agent/atomic_skill/pick.py:24) |
| `CONTACT_PATH_UNREACHABLE` | 代码中使用 | [atomic_skill/pick.py:22](C:/Users/21474/Desktop/robot-agent/atomic_skill/pick.py:22) |
| `CONTROLLER_NOT_READY` | 枚举声明或显示映射 | [atomic_action/motion/common/utils/error_codes.py:24](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/error_codes.py:24) |
| `DEPTH_UNAVAILABLE` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:25](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:25) |
| `DIRECTION_UNRESOLVED` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:20](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:20) |
| `DOOR_TARGET_NOT_REACHED` | 代码中使用 | [atomic_skill/pull.py:547](C:/Users/21474/Desktop/robot-agent/atomic_skill/pull.py:547) |
| `DRAWER_TARGET_NOT_REACHED` | 枚举声明或显示映射 | [demo/run_demo.py:93](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:93) |
| `DUPLICATE_VIEWPOINT` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:27](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:27) |
| `EXECUTION_FAILED` | 枚举声明或显示映射 | [atomic_action/motion/common/utils/error_codes.py:25](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/error_codes.py:25) |
| `EXECUTION_TIMEOUT` | 枚举声明或显示映射 | [atomic_action/motion/common/utils/error_codes.py:26](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/error_codes.py:26) |
| `FIELD_OF_VIEW_INVALID` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:24](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:24) |
| `FORCE_LIMIT` | 代码中使用 | [control/pull_push_controller.py:879](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:879) |
| `FRAME_TRANSFORM_FAILED` | 枚举声明或显示映射 | [atomic_action/motion/common/utils/error_codes.py:13](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/error_codes.py:13) |
| `GOAL_TOLERANCE_EXCEEDED` | 枚举声明或显示映射 | [atomic_action/motion/common/utils/error_codes.py:27](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/error_codes.py:27) |
| `GRASP_CLOSURE_COLLISION` | 代码中使用 | [demo/interaction/closure_validation.py:49](C:/Users/21474/Desktop/robot-agent/demo/interaction/closure_validation.py:49) |
| `GRASP_CLOSURE_INFEASIBLE` | 代码中使用 | [demo/interaction/object_library.py:193](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:193) |
| `GRASP_FAILED` | 代码中使用 | [execution/convergence.py:33](C:/Users/21474/Desktop/robot-agent/execution/convergence.py:33) |
| `GRASP_NOT_CONFIRMED` | 代码中使用 | [atomic_action/manipulation/grasp/action.py:52](C:/Users/21474/Desktop/robot-agent/atomic_action/manipulation/grasp/action.py:52) |
| `GRASP_OPENING_COLLISION` | 代码中使用 | [demo/interaction/closure_validation.py:49](C:/Users/21474/Desktop/robot-agent/demo/interaction/closure_validation.py:49) |
| `GRASP_POSE_PREDICTION_FAILED` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:30](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:30) |
| `IK_FAILED` | 代码中使用 | [atomic_action/motion/common/strategies/joint_move/joint_move.py:76](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/joint_move/joint_move.py:76) |
| `INSTANCE_AMBIGUOUS` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:16](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:16) |
| `INSUFFICIENT_POINTS` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:26](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:26) |
| `INTERNAL_ERROR` | 枚举声明或显示映射 | [atomic_action/motion/common/utils/error_codes.py:30](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/error_codes.py:30) |
| `INVALID_APPROACH_REQUEST` | 代码中使用 | [atomic_skill/approach.py:157](C:/Users/21474/Desktop/robot-agent/atomic_skill/approach.py:157) |
| `INVALID_ARC_DEFINITION` | 代码中使用 | [atomic_action/motion/common/strategies/circular_move/circular_move.py:50](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/circular_move/circular_move.py:50) |
| `INVALID_OFFSET` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:22](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:22) |
| `INVALID_REQUEST` | 代码中使用 | [atomic_skill/pull.py:481](C:/Users/21474/Desktop/robot-agent/atomic_skill/pull.py:481) |
| `INVALID_RETREAT_REQUEST` | 代码中使用 | [atomic_skill/retreat.py:107](C:/Users/21474/Desktop/robot-agent/atomic_skill/retreat.py:107) |
| `JOINT_LIMIT` | 代码中使用 | [control/pull_push_controller.py:619](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:619) |
| `JOINT_LIMIT_VIOLATION` | 代码中使用 | [atomic_action/motion/common/strategies/joint_move/joint_move.py:91](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/joint_move/joint_move.py:91) |
| `LOCALIZATION_TIMEOUT` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:37](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:37) |
| `LOOK_AT_GENERATION_FAILED` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:23](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:23) |
| `MODEL_NOT_FOUND` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:15](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:15) |
| `NOT_IMPLEMENTED` | 枚举声明或显示映射 | [atomic_action/motion/common/utils/error_codes.py:12](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/error_codes.py:12) |
| `NO_QUALITY_GAIN` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:26](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:26) |
| `NO_VISIBILITY_GAIN` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:25](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:25) |
| `OBJECT_NOT_HELD` | 代码中使用 | [atomic_skill/pull.py:475](C:/Users/21474/Desktop/robot-agent/atomic_skill/pull.py:475) |
| `OBSERVATION_INSUFFICIENT` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:14](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:14) |
| `OCCLUSION_EVIDENCE_INSUFFICIENT` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:19](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:19) |
| `ORIENTATION_ESTIMATION_FAILED` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:28](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:28) |
| `PATH_CONSTRAINT_INFEASIBLE` | 代码中使用 | [planning/collision_free_planner.py:45](C:/Users/21474/Desktop/robot-agent/planning/collision_free_planner.py:45) |
| `PATH_NOT_FOUND` | 代码中使用 | [planning/collision_free_planner.py:187](C:/Users/21474/Desktop/robot-agent/planning/collision_free_planner.py:187) |
| `PLANNING_FAILED` | 代码中使用 | [planning/collision_free_planner.py:67](C:/Users/21474/Desktop/robot-agent/planning/collision_free_planner.py:67) |
| `POSE_ESTIMATION_FAILED` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:29](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:29) |
| `POSE_VALIDATION_FAILED` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:36](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:36) |
| `POSE_VERIFICATION_FAILED` | 代码中使用 | [atomic_action/motion/common/interaction.py:62](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/interaction.py:62) |
| `POUR_EXECUTION_FAILED` | 代码中使用 | [atomic_skill/runner.py:86](C:/Users/21474/Desktop/robot-agent/atomic_skill/runner.py:86) |
| `POUR_MOTION_FAILED` | 代码中使用 | [atomic_skill/pour.py:57](C:/Users/21474/Desktop/robot-agent/atomic_skill/pour.py:57) |
| `POUR_OPENING_NOT_DOWN` | 代码中使用 | [atomic_skill/pour.py:151](C:/Users/21474/Desktop/robot-agent/atomic_skill/pour.py:151) |
| `POUR_OUTLET_DRIFT` | 代码中使用 | [atomic_skill/pour.py:146](C:/Users/21474/Desktop/robot-agent/atomic_skill/pour.py:146) |
| `PREPOINT_COLLISION` | 代码中使用 | [atomic_skill/pick.py:21](C:/Users/21474/Desktop/robot-agent/atomic_skill/pick.py:21) |
| `PREPOINT_UNREACHABLE` | 代码中使用 | [atomic_skill/pick.py:20](C:/Users/21474/Desktop/robot-agent/atomic_skill/pick.py:20) |
| `PROJECTION_FAILED` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:27](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:27) |
| `PUSH_PULL_EXECUTION_ERROR` | 代码中使用 | [control/pull_push_controller.py:20](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:20) |
| `PUSH_PULL_TIMEOUT` | 代码中使用 | [control/pull_push_controller.py:808](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:808) |
| `REFERENCE_INVALID` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:19](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:19) |
| `REGISTRATION_FAILED` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:23](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:23) |
| `ROBOT_JOINT_LIMIT` | 代码中使用 | [control/pull_push_controller.py:778](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:778) |
| `SCENE_CHANGED` | 枚举声明或显示映射 | [atomic_action/motion/common/utils/error_codes.py:28](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/error_codes.py:28) |
| `SEARCH_EXHAUSTED` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:32](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:32) |
| `SEARCH_NOT_REQUIRED` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:13](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:13) |
| `SEARCH_REGION_INVALID` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:20](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:20) |
| `SEARCH_TIMEOUT` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:33](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:33) |
| `SEGMENTATION_FAILED` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:24](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:24) |
| `SEMANTIC_DIRECTION_AMBIGUOUS` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:21](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:21) |
| `SENSOR_DATA_UNAVAILABLE` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:17](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:17) |
| `SINGULARITY_RISK` | 枚举声明或显示映射 | [atomic_action/motion/common/utils/error_codes.py:18](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/error_codes.py:18) |
| `STOPPED_BY_SAFETY` | 枚举声明或显示映射 | [atomic_action/motion/common/utils/error_codes.py:29](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/error_codes.py:29) |
| `TARGET_DESCRIPTION_AMBIGUOUS` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:14](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:14) |
| `TARGET_NOT_FOUND` | 枚举声明或显示映射 | [atomic_action/perception/location/utils/error_codes.py:13](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:13) |
| `TARGET_NOT_REACHED` | 代码中使用 | [control/pull_push_controller.py:458](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:458) |
| `TARGET_POSE_COLLISION` | 代码中使用 | [demo/interaction/object_library.py:174](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:174) |
| `TARGET_POSE_UNREACHABLE` | 代码中使用 | [demo/interaction/object_library.py:173](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:173) |
| `TARGET_PRIOR_INVALID` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:18](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:18) |
| `TARGET_RESOLUTION_FAILED` | 代码中使用 | [atomic_skill/runner.py:183](C:/Users/21474/Desktop/robot-agent/atomic_skill/runner.py:183) |
| `TARGET_UNREACHABLE` | 代码中使用 | [atomic_action/motion/common/strategies/joint_move/joint_move.py:63](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/joint_move/joint_move.py:63) |
| `TRAJECTORY_INVALID` | 代码中使用 | [planning/time_parameterization.py:114](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:114) |
| `TRAVEL_LIMIT` | 代码中使用 | [control/pull_push_controller.py:610](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:610) |
| `UNKNOWN_STRATEGY` | 枚举声明或显示映射 | [atomic_action/motion/common/utils/error_codes.py:11](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/error_codes.py:11) |
| `VIEWPOINT_GENERATION_FAILED` | 枚举声明或显示映射 | [atomic_action/perception/search/utils/error_codes.py:22](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:22) |

| `NO_VALID_APPROACH` | 代码中使用 | [atomic_skill/pick.py:27](C:/Users/21474/Desktop/robot-agent/atomic_skill/pick.py:27) |
| `NO_VALID_TARGET_POSE` | 代码中使用 | [demo/interaction/object_library.py:175](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:175) |
| `NO_VALID_CANDIDATE` | 枚举声明 | [atomic_action/perception/location/utils/error_codes.py:37](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/error_codes.py:37) |
| `NO_VALID_VIEWPOINT` | 枚举声明 | [atomic_action/perception/search/utils/error_codes.py:34](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/error_codes.py:34) |
| `PUT_TARGET_IS_HELD_OBJECT` | 代码中使用 | [atomic_skill/put.py:26](C:/Users/21474/Desktop/robot-agent/atomic_skill/put.py:26) |

## 直接抛出的异常与原始原因

| 源码位置 | 原始错误原因（动态字段保留表达式） |
| --- | --- |
| [atomic_action/manipulation/grasp/action.py:17](C:/Users/21474/Desktop/robot-agent/atomic_action/manipulation/grasp/action.py:17) | A robot runtime is required. |
| [atomic_action/manipulation/release/action.py:11](C:/Users/21474/Desktop/robot-agent/atomic_action/manipulation/release/action.py:11) | A robot runtime is required. |
| [atomic_action/manipulation/rotate_end/action.py:17](C:/Users/21474/Desktop/robot-agent/atomic_action/manipulation/rotate_end/action.py:17) | A robot runtime is required. |
| [atomic_action/manipulation/rotate_end/action.py:20](C:/Users/21474/Desktop/robot-agent/atomic_action/manipulation/rotate_end/action.py:20) | degree must be a finite number. |
| [atomic_action/motion/common/action.py:294](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/action.py:294) | No feasible joint configuration found. |
| [atomic_action/motion/common/action.py:309](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/action.py:309) | MuJoCo controller is not ready. |
| [atomic_action/motion/common/action.py:322](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/action.py:322) | f"Final joint tracking error {result['joint_error']:.6f} exceeds tolerance." |
| [atomic_action/motion/common/action.py:197](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/action.py:197) | Direct path collides and its hard Cartesian constraint cannot be relaxed. |
| [atomic_action/motion/common/interaction.py:21](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/interaction.py:21) | Grasp approach fallback requires a resolved world-frame pose |
| [atomic_action/motion/common/interaction.py:79](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/interaction.py:79) | Pick targets require their service grasp orientation; fixed orientation is supported for move only |
| [atomic_action/motion/common/interaction.py:81](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/interaction.py:81) | Interaction moves cannot ignore target collisions |
| [atomic_action/motion/common/selector.py:94](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/selector.py:94) | f"Cannot select strategy for target type: {target_type}" |
| [atomic_action/motion/common/selector.py:109](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/selector.py:109) | f"Unsupported planning mode: {requested_mode}" |
| [atomic_action/motion/common/selector.py:71](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/selector.py:71) | f"Unsupported path_type: {requested_path}" |
| [atomic_action/motion/common/selector.py:77](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/selector.py:77) | Circular Move requires via-point or center-axis arc geometry. |
| [atomic_action/motion/common/strategies/circular_move/circular_move.py:87](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/circular_move/circular_move.py:87) | f"{field} must contain three finite values." |
| [atomic_action/motion/common/strategies/circular_move/circular_move.py:100](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/circular_move/circular_move.py:100) | Start, via, and target points are collinear. |
| [atomic_action/motion/common/strategies/circular_move/circular_move.py:31](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/circular_move/circular_move.py:31) | Circular Move requires a pose target. |
| [atomic_action/motion/common/strategies/circular_move/circular_move.py:53](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/circular_move/circular_move.py:53) | Circular arc is degenerate. |
| [atomic_action/motion/common/strategies/circular_move/circular_move.py:50](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/circular_move/circular_move.py:50) | arc_center/axis/angle does not reach the target position. |
| [atomic_action/motion/common/strategies/joint_move/joint_move.py:89](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/joint_move/joint_move.py:89) | Target joint state violates UR5e joint limits. |
| [atomic_action/motion/common/strategies/joint_move/joint_move.py:63](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/joint_move/joint_move.py:63) | f"Unknown named robot state: {name}" |
| [atomic_action/motion/common/strategies/joint_move/joint_move.py:76](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/joint_move/joint_move.py:76) | No IK configuration reaches the target. |
| [atomic_action/motion/common/strategies/linear_move/linear_move.py:219](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/linear_move/linear_move.py:219) | Current end-effector orientation must be a finite quaternion. |
| [atomic_action/motion/common/strategies/linear_move/linear_move.py:261](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/linear_move/linear_move.py:261) | Target orientation must define a finite nonzero quaternion. |
| [atomic_action/motion/common/strategies/linear_move/linear_move.py:31](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/linear_move/linear_move.py:31) | Linear Move requires a pose target. |
| [atomic_action/motion/common/strategies/linear_move/linear_move.py:42](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/linear_move/linear_move.py:42) | strategy_params.ik_seed must contain six finite joint values. |
| [atomic_action/motion/common/strategies/linear_move/linear_move.py:119](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/linear_move/linear_move.py:119) | f"No continuous IK branch reaches the target ({len(rejected)} branches rejected)." |
| [atomic_action/motion/common/strategies/linear_move/linear_move.py:181](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/linear_move/linear_move.py:181) | Linear IK exceeds the joint continuity limit. |
| [atomic_action/motion/common/strategies/pose_utils.py:28](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/strategies/pose_utils.py:28) | Current end-effector pose does not contain a quaternion. |
| [atomic_action/motion/common/utils/validation.py:156](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:156) | f"{field} must contain x, y, z." |
| [atomic_action/motion/common/utils/validation.py:20](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:20) | Move request must be a mapping/object. |
| [atomic_action/motion/common/utils/validation.py:25](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:25) | target is required and must be an object. |
| [atomic_action/motion/common/utils/validation.py:29](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:29) | target.type must be pose, joint, or named_state. |
| [atomic_action/motion/common/utils/validation.py:34](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:34) | Pose target requires target.position. |
| [atomic_action/motion/common/utils/validation.py:36](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:36) | Joint target requires target.joint_positions. |
| [atomic_action/motion/common/utils/validation.py:40](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:40) | Named-state target requires target.state_name. |
| [atomic_action/motion/common/utils/validation.py:47](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:47) | motion must be an object. |
| [atomic_action/motion/common/utils/validation.py:56](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:56) | planning must be an object. |
| [atomic_action/motion/common/utils/validation.py:65](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:65) | constraints must be an object. |
| [atomic_action/motion/common/utils/validation.py:82](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:82) | strategy_params must be an object. |
| [atomic_action/motion/common/utils/validation.py:98](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:98) | constraints.avoid_collision must be boolean. |
| [atomic_action/motion/common/utils/validation.py:100](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:100) | constraints.keep_end_effector_orientation must be boolean. |
| [atomic_action/motion/common/utils/validation.py:131](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:131) | f"strategy_params.ik_method must be one of: {', '.join(sorted(allowed_methods))}." |
| [atomic_action/motion/common/utils/validation.py:147](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:147) | f"{field} must contain finite numbers." |
| [atomic_action/motion/common/utils/validation.py:161](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:161) | target.orientation must be an object. |
| [atomic_action/motion/common/utils/validation.py:179](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:179) | f"{key} must be one of: {', '.join(sorted(allowed))}." |
| [atomic_action/motion/common/utils/validation.py:185](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:185) | f"{key} must be an integer." |
| [atomic_action/motion/common/utils/validation.py:188](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:188) | f"{key} must be between {lower} and {upper}." |
| [atomic_action/motion/common/utils/validation.py:94](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:94) | f"planning.{key} must be boolean." |
| [atomic_action/motion/common/utils/validation.py:112](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:112) | target.joint_positions must contain 6 values. |
| [atomic_action/motion/common/utils/validation.py:127](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:127) | Circular motion requires via_point or arc_center/arc_axis/arc_angle. |
| [atomic_action/motion/common/utils/validation.py:139](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:139) | strategy_params.ik_seed must contain 6 values. |
| [atomic_action/motion/common/utils/validation.py:167](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:167) | Quaternion must have non-zero norm. |
| [atomic_action/motion/common/utils/validation.py:172](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:172) | orientation.representation must be quaternion or rpy. |
| [atomic_action/motion/common/utils/validation.py:121](C:/Users/21474/Desktop/robot-agent/atomic_action/motion/common/utils/validation.py:121) | Only base/world pose frames are currently supported. |
| [atomic_action/perception/location/selector.py:90](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/selector.py:90) | Cannot select locate strategy: provide model_id, category, target description, or a relative/approach target definition. |
| [atomic_action/perception/location/selector.py:102](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/selector.py:102) | f"Unsupported locate strategy: {strategy}" |
| [atomic_action/perception/location/selector.py:111](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/selector.py:111) | Known Model Locate requires model_id. |
| [atomic_action/perception/location/selector.py:116](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/selector.py:116) | Known Category Locate requires category. |
| [atomic_action/perception/location/selector.py:123](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/selector.py:123) | Unknown Object Locate requires a target description. |
| [atomic_action/perception/location/selector.py:143](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/selector.py:143) | Approach Point Locate requires a final target pose. |
| [atomic_action/perception/location/selector.py:151](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/selector.py:151) | Relative Point Locate requires reference and offset. |
| [atomic_action/perception/location/selector.py:166](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/selector.py:166) | Grasp Pose Prediction requires target evidence (object/instruction/category/instance_id/model_id) or strategy_params.allow_scene_level=true. |
| [atomic_action/perception/location/selector.py:129](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/selector.py:129) | Grasp Pose Prediction requires target.type=grasp. |
| [atomic_action/perception/location/selector.py:136](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/selector.py:136) | VLM Estimate requires target.type=grasp or place. |
| [atomic_action/perception/location/selector.py:138](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/selector.py:138) | VLM Estimate requires a target description. |
| [atomic_action/perception/location/skill.py:123](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/skill.py:123) | A robot runtime is required for Locate Skill. |
| [atomic_action/perception/location/strategies/approach_point_locate/approach_point_locate.py:30](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/strategies/approach_point_locate/approach_point_locate.py:30) | Implement ApproachPointLocate.locate(). |
| [atomic_action/perception/location/strategies/grasp_pose_prediction/grasp_pose_prediction.py:60](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/strategies/grasp_pose_prediction/grasp_pose_prediction.py:60) | Implement GraspPosePrediction.locate(). |
| [atomic_action/perception/location/strategies/known_category_locate/known_category_locate.py:30](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/strategies/known_category_locate/known_category_locate.py:30) | Implement KnownCategoryLocate.locate(). |
| [atomic_action/perception/location/strategies/known_model_locate/known_model_locate.py:30](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/strategies/known_model_locate/known_model_locate.py:30) | Implement KnownModelLocate.locate(). |
| [atomic_action/perception/location/strategies/relative_point_locate/relative_point_locate.py:30](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/strategies/relative_point_locate/relative_point_locate.py:30) | Implement RelativePointLocate.locate(). |
| [atomic_action/perception/location/strategies/unknown_object_locate/unknown_object_locate.py:31](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/strategies/unknown_object_locate/unknown_object_locate.py:31) | Unknown Object Locate requires a camera runtime. |
| [atomic_action/perception/location/strategies/vlm_estimate/vlm_estimate.py:20](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/strategies/vlm_estimate/vlm_estimate.py:20) | VLM Estimate requires a camera runtime. |
| [atomic_action/perception/location/strategies/vlm_estimate/vlm_estimate.py:24](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/strategies/vlm_estimate/vlm_estimate.py:24) | VLM Estimate supports grasp and place targets only. |
| [atomic_action/perception/location/strategies/vlm_estimate/vlm_estimate.py:28](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/strategies/vlm_estimate/vlm_estimate.py:28) | VLM Estimate requires target.object, target.category or strategy_params.target_description. |
| [atomic_action/perception/location/utils/frame_transform.py:27](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/frame_transform.py:27) | Connect transform_pose() to TF/runtime backend. |
| [atomic_action/perception/location/utils/frame_transform.py:38](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/frame_transform.py:38) | Connect transform_point() to TF/runtime backend. |
| [atomic_action/perception/location/utils/frame_transform.py:49](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/frame_transform.py:49) | Connect transform_vector() to TF/runtime backend. |
| [atomic_action/perception/location/utils/validation.py:23](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/validation.py:23) | Locate request must be a mapping/object. |
| [atomic_action/perception/location/utils/validation.py:28](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/validation.py:28) | target is required and must be an object. |
| [atomic_action/perception/location/utils/validation.py:42](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/validation.py:42) | target.type must be grasp, place, press, insert, extract, approach, relative, or object_pose. |
| [atomic_action/perception/location/utils/validation.py:55](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/validation.py:55) | VLM Estimate currently returns world-frame points only. |
| [atomic_action/perception/location/utils/validation.py:87](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/validation.py:87) | Approach target requires reference.pose or strategy_params.target_pose. |
| [atomic_action/perception/location/utils/validation.py:93](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/validation.py:93) | constraints.confidence_threshold must be between 0 and 1. |
| [atomic_action/perception/location/utils/validation.py:99](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/validation.py:99) | constraints.timeout must be positive. |
| [atomic_action/perception/location/utils/validation.py:75](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/validation.py:75) | Relative target requires reference.type. |
| [atomic_action/perception/location/utils/validation.py:79](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/utils/validation.py:79) | Relative target requires offset.mode semantic or cartesian. |
| [atomic_action/perception/location/validators/pose_validator.py:35](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/location/validators/pose_validator.py:35) | Implement PoseValidator.validate(). |
| [atomic_action/perception/search/selector.py:76](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/selector.py:76) | Cannot select search strategy: provide occlusion evidence, a target prior/direction hint, or a valid search region. |
| [atomic_action/perception/search/selector.py:88](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/selector.py:88) | f"Unsupported search strategy: {strategy}" |
| [atomic_action/perception/search/selector.py:91](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/selector.py:91) | Systematic Scan requires search_region or a runtime viewpoint set. |
| [atomic_action/perception/search/selector.py:96](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/selector.py:96) | Prior-Guided Search requires a position, region, direction, edge, or tracking prior. |
| [atomic_action/perception/search/selector.py:102](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/selector.py:102) | Occlusion-Aware View requires partial visibility or occlusion evidence. |
| [atomic_action/perception/search/selector.py:113](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/selector.py:113) | Occlusion-Aware View requires a coarse target pose, target ray, or target prior pose. |
| [atomic_action/perception/search/selector.py:120](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/selector.py:120) | Local View Refinement requires a currently visible or partially visible target. |
| [atomic_action/perception/search/skill.py:131](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/skill.py:131) | Connect _get_runtime_context() to the robot runtime/context manager. |
| [atomic_action/perception/search/strategies/local_view_refinement/local_view_refinement.py:30](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/strategies/local_view_refinement/local_view_refinement.py:30) | Implement LocalViewRefinement.generate(). |
| [atomic_action/perception/search/strategies/occlusion_aware_view/occlusion_aware_view.py:30](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/strategies/occlusion_aware_view/occlusion_aware_view.py:30) | Implement OcclusionAwareView.generate(). |
| [atomic_action/perception/search/strategies/prior_guided_search/prior_guided_search.py:30](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/strategies/prior_guided_search/prior_guided_search.py:30) | Implement PriorGuidedSearch.generate(). |
| [atomic_action/perception/search/strategies/systematic_scan/systematic_scan.py:30](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/strategies/systematic_scan/systematic_scan.py:30) | Implement SystematicScan.generate(). |
| [atomic_action/perception/search/utils/frame_transform.py:25](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/frame_transform.py:25) | Connect to the project's transform service. |
| [atomic_action/perception/search/utils/frame_transform.py:34](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/frame_transform.py:34) | Implement hand-eye, pan-tilt, or mobile-camera target conversion. |
| [atomic_action/perception/search/utils/history.py:23](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/history.py:23) | Implement SearchHistory.is_duplicate(). |
| [atomic_action/perception/search/utils/history.py:33](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/history.py:33) | Implement SearchHistory.next_state(). |
| [atomic_action/perception/search/utils/validation.py:23](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:23) | Search request must be a mapping/object. |
| [atomic_action/perception/search/utils/validation.py:28](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:28) | target is required and must be an object. |
| [atomic_action/perception/search/utils/validation.py:41](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:41) | target requires a description, identifier, category, model, pose, or region prior. |
| [atomic_action/perception/search/utils/validation.py:47](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:47) | observation is required and must be an object. |
| [atomic_action/perception/search/utils/validation.py:56](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:56) | observation.visibility must be visible, partial, not_visible, or unknown. |
| [atomic_action/perception/search/utils/validation.py:62](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:62) | camera is required and must be an object. |
| [atomic_action/perception/search/utils/validation.py:64](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:64) | camera.id and camera.optical_frame are required. |
| [atomic_action/perception/search/utils/validation.py:74](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:74) | camera.mount_type must identify a supported camera mount. |
| [atomic_action/perception/search/utils/validation.py:78](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:78) | A fixed external camera cannot update viewpoint without another movable observation device. |
| [atomic_action/perception/search/utils/validation.py:116](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:116) | observation.confidence must be between 0 and 1. |
| [atomic_action/perception/search/utils/validation.py:128](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:128) | constraints.min_view_distance cannot exceed max_view_distance. |
| [atomic_action/perception/search/utils/validation.py:133](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:133) | Search history has reached search.max_steps; return SEARCH_EXHAUSTED. |
| [atomic_action/perception/search/utils/validation.py:139](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:139) | constraints.timeout must be positive. |
| [atomic_action/perception/search/utils/validation.py:123](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/utils/validation.py:123) | f"constraints.{key} must be between 0 and 1." |
| [atomic_action/perception/search/validators/viewpoint_validator.py:39](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/search/validators/viewpoint_validator.py:39) | Implement ViewpointValidator.validate(). |
| [atomic_action/perception/vlm_estimate/estimator.py:59](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:59) | point_type must be 'grasp' or 'place'. |
| [atomic_action/perception/vlm_estimate/estimator.py:61](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:61) | A nonempty target description is required. |
| [atomic_action/perception/vlm_estimate/estimator.py:65](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:65) | rgb must be an HxWx3 uint8 RGB image. |
| [atomic_action/perception/vlm_estimate/estimator.py:68](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:68) | depth must match the RGB image height and width. |
| [atomic_action/perception/vlm_estimate/estimator.py:70](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:70) | Camera intrinsics do not match the image dimensions. |
| [atomic_action/perception/vlm_estimate/estimator.py:72](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:72) | Camera intrinsics must be finite with positive focal lengths. |
| [atomic_action/perception/vlm_estimate/estimator.py:76](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:76) | Camera pose must contain a finite 3x3 rotation and 3-D position. |
| [atomic_action/perception/vlm_estimate/estimator.py:107](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:107) | f"No valid metric depth at selected pixel ({u}, {v})." |
| [atomic_action/perception/vlm_estimate/estimator.py:139](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:139) | f"提示词文件必须包含非空的 target、description、respond: {path}" |
| [atomic_action/perception/vlm_estimate/estimator.py:145](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:145) | f"提示词文件的 target_names 必须是名称对照表: {path}" |
| [atomic_action/perception/vlm_estimate/estimator.py:167](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:167) | VLM response must be a JSON object. |
| [atomic_action/perception/vlm_estimate/estimator.py:132](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:132) | f"找不到提示词文件: {path}" |
| [atomic_action/perception/vlm_estimate/estimator.py:134](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:134) | f"提示词文件不是有效 JSON: {path}" |
| [atomic_action/perception/vlm_estimate/estimator.py:165](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:165) | VLM response must start with a JSON object. |
| [atomic_action/perception/vlm_estimate/estimator.py:172](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:172) | f"VLM {key} must be a finite normalized number in [0, 1]." |
| [atomic_action/perception/vlm_estimate/estimator.py:177](C:/Users/21474/Desktop/robot-agent/atomic_action/perception/vlm_estimate/estimator.py:177) | VLM confidence must be in [0, 1]. |
| [atomic_skill/approach.py:113](C:/Users/21474/Desktop/robot-agent/atomic_skill/approach.py:113) | pose_axis must be a finite 3-vector or a signed axis name |
| [atomic_skill/approach.py:116](C:/Users/21474/Desktop/robot-agent/atomic_skill/approach.py:116) | pose_axis must be non-zero |
| [atomic_skill/approach.py:135](C:/Users/21474/Desktop/robot-agent/atomic_skill/approach.py:135) | target_pose orientation must be a finite 4-vector |
| [atomic_skill/approach.py:107](C:/Users/21474/Desktop/robot-agent/atomic_skill/approach.py:107) | pose_axis must be one of +x, -x, +y, -y, +z, -z |
| [atomic_skill/approach.py:168](C:/Users/21474/Desktop/robot-agent/atomic_skill/approach.py:168) | grasp_offset_local must be a finite 3-vector |
| [atomic_skill/approach.py:171](C:/Users/21474/Desktop/robot-agent/atomic_skill/approach.py:171) | grasp_offset_local must be non-zero |
| [atomic_skill/approach.py:129](C:/Users/21474/Desktop/robot-agent/atomic_skill/approach.py:129) | target_pose.rpy must be a finite 3-vector |
| [atomic_skill/pick.py:146](C:/Users/21474/Desktop/robot-agent/atomic_skill/pick.py:146) | skill |
| [atomic_skill/pick.py:79](C:/Users/21474/Desktop/robot-agent/atomic_skill/pick.py:79) | retreat_lift_height must be a finite nonnegative number in metres |
| [atomic_skill/pour.py:69](C:/Users/21474/Desktop/robot-agent/atomic_skill/pour.py:69) | pour requires source and target |
| [atomic_skill/pour.py:71](C:/Users/21474/Desktop/robot-agent/atomic_skill/pour.py:71) | f"pour requires {source!r} to be held; currently held={held!r}" |
| [atomic_skill/pour.py:84](C:/Users/21474/Desktop/robot-agent/atomic_skill/pour.py:84) | opening_axis_object and tilt_axis_object must be non-zero |
| [atomic_skill/pour.py:98](C:/Users/21474/Desktop/robot-agent/atomic_skill/pour.py:98) | invalid pour tilt parameters |
| [atomic_skill/pour.py:116](C:/Users/21474/Desktop/robot-agent/atomic_skill/pour.py:116) | APPROACH cannot place the outlet over the target region |
| [atomic_skill/pour.py:82](C:/Users/21474/Desktop/robot-agent/atomic_skill/pour.py:82) | f"{name} must contain three finite values" |
| [atomic_skill/pull.py:126](C:/Users/21474/Desktop/robot-agent/atomic_skill/pull.py:126) | f'Execution frame collision: {hit.contacts}' |
| [atomic_skill/put.py:11](C:/Users/21474/Desktop/robot-agent/atomic_skill/put.py:11) | release request requires an object target |
| [atomic_skill/put.py:14](C:/Users/21474/Desktop/robot-agent/atomic_skill/put.py:14) | release must be an object |
| [atomic_skill/put.py:17](C:/Users/21474/Desktop/robot-agent/atomic_skill/put.py:17) | release.operation must be release or verify_only |
| [atomic_skill/put.py:36](C:/Users/21474/Desktop/robot-agent/atomic_skill/put.py:36) | destination_name |
| [atomic_skill/put.py:47](C:/Users/21474/Desktop/robot-agent/atomic_skill/put.py:47) | put requires release.operation=release |
| [atomic_skill/retreat.py:39](C:/Users/21474/Desktop/robot-agent/atomic_skill/retreat.py:39) | pose_axis must be a finite 3-vector or a signed axis name |
| [atomic_skill/retreat.py:42](C:/Users/21474/Desktop/robot-agent/atomic_skill/retreat.py:42) | pose_axis must be non-zero |
| [atomic_skill/retreat.py:61](C:/Users/21474/Desktop/robot-agent/atomic_skill/retreat.py:61) | target_pose orientation must be a finite 4-vector |
| [atomic_skill/retreat.py:33](C:/Users/21474/Desktop/robot-agent/atomic_skill/retreat.py:33) | pose_axis must be one of +x, -x, +y, -y, +z, -z |
| [atomic_skill/retreat.py:98](C:/Users/21474/Desktop/robot-agent/atomic_skill/retreat.py:98) | grasp_offset_local must be a finite 3-vector |
| [atomic_skill/retreat.py:101](C:/Users/21474/Desktop/robot-agent/atomic_skill/retreat.py:101) | grasp_offset_local must be non-zero |
| [atomic_skill/retreat.py:55](C:/Users/21474/Desktop/robot-agent/atomic_skill/retreat.py:55) | target_pose.rpy must be a finite 3-vector |
| [atomic_skill/runner.py:94](C:/Users/21474/Desktop/robot-agent/atomic_skill/runner.py:94) | TaskSkillExecutor supports move_to, pick, put, lift_up, put_down, shift_down, grasp, release, approach, retreat, and pour |
| [atomic_skill/runner.py:149](C:/Users/21474/Desktop/robot-agent/atomic_skill/runner.py:149) | No collision-free IK solution exists for the relative move_to target |
| [atomic_skill/runner.py:211](C:/Users/21474/Desktop/robot-agent/atomic_skill/runner.py:211) | f'Execution frame collision: {contacts}' |
| [atomic_skill/runner.py:285](C:/Users/21474/Desktop/robot-agent/atomic_skill/runner.py:285) | f'Executed trajectory collides: {actual_check.contacts}' |
| [atomic_skill/runner.py:309](C:/Users/21474/Desktop/robot-agent/atomic_skill/runner.py:309) | f'{prefix} pose outside tolerance' |
| [atomic_skill/runner.py:339](C:/Users/21474/Desktop/robot-agent/atomic_skill/runner.py:339) | f"Grasp failed: {grasp_result['error']}" |
| [atomic_skill/runner.py:361](C:/Users/21474/Desktop/robot-agent/atomic_skill/runner.py:361) | f"Release failed: {release_result['error']}" |
| [atomic_skill/runner.py:412](C:/Users/21474/Desktop/robot-agent/atomic_skill/runner.py:412) | f"{command['skill_name']} requires an object-library resolver" |
| [planning/collision_free_planner.py:45](C:/Users/21474/Desktop/robot-agent/planning/collision_free_planner.py:45) | A hard Cartesian path cannot be relaxed for collision avoidance. |
| [planning/collision_free_planner.py:54](C:/Users/21474/Desktop/robot-agent/planning/collision_free_planner.py:54) | Start state is in collision. |
| [planning/collision_free_planner.py:60](C:/Users/21474/Desktop/robot-agent/planning/collision_free_planner.py:60) | Goal state is in collision. |
| [planning/collision_free_planner.py:185](C:/Users/21474/Desktop/robot-agent/planning/collision_free_planner.py:185) | No collision-free path exists with the end-effector orientation fixed. |
| [planning/collision_free_planner.py:67](C:/Users/21474/Desktop/robot-agent/planning/collision_free_planner.py:67) | Fixed-orientation collision planning requires a kinematics service. |
| [planning/collision_free_planner.py:80](C:/Users/21474/Desktop/robot-agent/planning/collision_free_planner.py:80) | RRT-Connect could not find a collision-free path within the planning budget. |
| [planning/collision_free_planner.py:109](C:/Users/21474/Desktop/robot-agent/planning/collision_free_planner.py:109) | Final collision-free path failed collision validation. |
| [planning/collision_free_planner.py:201](C:/Users/21474/Desktop/robot-agent/planning/collision_free_planner.py:201) | Fixed-orientation path could not be reconstructed. |
| [planning/collision_free_planner.py:217](C:/Users/21474/Desktop/robot-agent/planning/collision_free_planner.py:217) | Fixed-orientation path failed final collision validation. |
| [planning/direct_planner.py:32](C:/Users/21474/Desktop/robot-agent/planning/direct_planner.py:32) | Path strategy returned an invalid joint path. |
| [planning/direct_planner.py:34](C:/Users/21474/Desktop/robot-agent/planning/direct_planner.py:34) | Path contains non-finite joint values. |
| [planning/direct_planner.py:36](C:/Users/21474/Desktop/robot-agent/planning/direct_planner.py:36) | Path violates joint limits. |
| [planning/direct_planner.py:47](C:/Users/21474/Desktop/robot-agent/planning/direct_planner.py:47) | Goal state is in collision. |
| [planning/direct_planner.py:62](C:/Users/21474/Desktop/robot-agent/planning/direct_planner.py:62) | Direct path collides with the robot or environment. |
| [planning/interaction_planners.py:23](C:/Users/21474/Desktop/robot-agent/planning/interaction_planners.py:23) | search must be an object |
| [planning/interaction_planners.py:28](C:/Users/21474/Desktop/robot-agent/planning/interaction_planners.py:28) | f"{label} search values must be finite" |
| [planning/interaction_planners.py:30](C:/Users/21474/Desktop/robot-agent/planning/interaction_planners.py:30) | f"{label} search requires 0 <= min_distance <= max_distance and step > 0" |
| [planning/motion_limits.py:13](C:/Users/21474/Desktop/robot-agent/planning/motion_limits.py:13) | f'URDF joint {name} has no velocity limit' |
| [planning/motion_limits.py:18](C:/Users/21474/Desktop/robot-agent/planning/motion_limits.py:18) | f'URDF joint {name} requires positive finite velocity/acceleration limits' |
| [planning/time_parameterization.py:12](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:12) | f'{key} must be resolved from the robot URDF before continuous timing' |
| [planning/time_parameterization.py:16](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:16) | f'{key} and {scale_key} must be finite positive joint limits and scale' |
| [planning/time_parameterization.py:48](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:48) | waypoints must be finite NxDOF, N >= 2 |
| [planning/time_parameterization.py:178](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:178) | waypoints must be finite with shape (N, DOF), N >= 2 |
| [planning/time_parameterization.py:185](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:185) | velocity_scale and per-joint max_velocity must be finite and positive |
| [planning/time_parameterization.py:236](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:236) | Ruckig joint limits and scales must be finite and positive |
| [planning/time_parameterization.py:21](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:21) | f'{multiplier_key} must contain one finite positive multiplier per joint' |
| [planning/time_parameterization.py:135](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:135) | Smoothed trajectory exceeds circular path tolerance |
| [planning/time_parameterization.py:160](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:160) | Locally timed references exceed joint limits |
| [planning/time_parameterization.py:200](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:200) | Interpolated trajectory failed collision validation |
| [planning/time_parameterization.py:252](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:252) | Time-parameterized trajectory failed collision validation |
| [planning/time_parameterization.py:87](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:87) | Smoothed trajectory exceeds linear path tolerance |
| [planning/time_parameterization.py:117](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:117) | Continuous trajectory failed collision validation |
| [planning/time_parameterization.py:114](C:/Users/21474/Desktop/robot-agent/planning/time_parameterization.py:114) | Continuous trajectory failed collision validation |
| [kinematics/__init__.py:15](C:/Users/21474/Desktop/robot-agent/kinematics/__init__.py:15) | name |
| [kinematics/contracts.py:59](C:/Users/21474/Desktop/robot-agent/kinematics/contracts.py:59) | Result contains no IK solutions |
| [kinematics/geometry.py:33](C:/Users/21474/Desktop/robot-agent/kinematics/geometry.py:33) | Only world/base poses are supported |
| [kinematics/geometry.py:58](C:/Users/21474/Desktop/robot-agent/kinematics/geometry.py:58) | Pose position must contain 3 finite values |
| [kinematics/geometry.py:62](C:/Users/21474/Desktop/robot-agent/kinematics/geometry.py:62) | Pose rotation must be a proper 3x3 rotation matrix |
| [kinematics/geometry.py:56](C:/Users/21474/Desktop/robot-agent/kinematics/geometry.py:56) | Unsupported orientation representation |
| [kinematics/geometry.py:50](C:/Users/21474/Desktop/robot-agent/kinematics/geometry.py:50) | Quaternion must have finite non-zero norm |
| [kinematics/models/mujoco_model.py:26](C:/Users/21474/Desktop/robot-agent/kinematics/models/mujoco_model.py:26) | Joint vector must contain one finite value per robot joint |
| [kinematics/policies.py:12](C:/Users/21474/Desktop/robot-agent/kinematics/policies.py:12) | Periodic coordinates require finite limits |
| [kinematics/registry.py:49](C:/Users/21474/Desktop/robot-agent/kinematics/registry.py:49) | Solver names/aliases must be unique non-empty strings other than auto |
| [kinematics/registry.py:52](C:/Users/21474/Desktop/robot-agent/kinematics/registry.py:52) | Factory must be module.path:callable |
| [kinematics/registry.py:55](C:/Users/21474/Desktop/robot-agent/kinematics/registry.py:55) | auto_priority must be an integer or null |
| [kinematics/registry.py:57](C:/Users/21474/Desktop/robot-agent/kinematics/registry.py:57) | f"Duplicate solver name or alias: {names}" |
| [kinematics/registry.py:72](C:/Users/21474/Desktop/robot-agent/kinematics/registry.py:72) | IK backend name must be a string |
| [kinematics/registry.py:83](C:/Users/21474/Desktop/robot-agent/kinematics/registry.py:83) | f"{descriptor.name} does not support this robot model" |
| [kinematics/registry.py:76](C:/Users/21474/Desktop/robot-agent/kinematics/registry.py:76) | f"Unknown IK backend {name!r}; registered: {', '.join(self.names)}" |
| [kinematics/registry.py:34](C:/Users/21474/Desktop/robot-agent/kinematics/registry.py:34) | Expected api_version: 1 |
| [kinematics/registry.py:36](C:/Users/21474/Desktop/robot-agent/kinematics/registry.py:36) | aliases must be a list of strings |
| [kinematics/service.py:109](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:109) | "; ".join(errors) or "No automatic IK backends registered" |
| [kinematics/service.py:22](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:22) | Invalid robot joint limits/periods |
| [kinematics/service.py:55](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:55) | Unknown IK selection policy |
| [kinematics/service.py:57](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:57) | IK timeout must be positive and finite |
| [kinematics/service.py:59](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:59) | max_solutions must be a positive integer |
| [kinematics/service.py:61](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:61) | max_joint_step must be positive and finite |
| [kinematics/service.py:63](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:63) | continuous policy requires max_joint_step |
| [kinematics/service.py:66](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:66) | IK seed must contain one finite value per joint |
| [kinematics/service.py:115](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:115) | Invalid backend verification tolerances |
| [kinematics/service.py:75](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:75) | Use either backend or a non-empty backends sequence |
| [kinematics/service.py:94](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:94) | Backend must return IKResult |
| [kinematics/service.py:97](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:97) | "No valid solution" + (f" ({'; '.join(rejected)})" if rejected else "") |
| [kinematics/service.py:123](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:123) | invalid joint vector |
| [kinematics/service.py:126](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:126) | joint limits |
| [kinematics/service.py:128](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:128) | joint continuity limit |
| [kinematics/service.py:134](C:/Users/21474/Desktop/robot-agent/kinematics/service.py:134) | FK target mismatch |
| [demo/interaction/carrying_safety.py:13](C:/Users/21474/Desktop/robot-agent/demo/interaction/carrying_safety.py:13) | grasp_request.strategy_params.safety_distance must be a finite non-negative number in metres |
| [demo/interaction/closure_validation.py:66](C:/Users/21474/Desktop/robot-agent/demo/interaction/closure_validation.py:66) | failure[-1]['error_message'] |
| [demo/interaction/closure_validation.py:104](C:/Users/21474/Desktop/robot-agent/demo/interaction/closure_validation.py:104) | failures[-1]['error_message'] |
| [demo/interaction/object_library.py:23](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:23) | Pose requires finite xyz and unit xyzw quaternion |
| [demo/interaction/object_library.py:30](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:30) | target must be a scene object ID |
| [demo/interaction/object_library.py:33](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:33) | f'Unknown or ambiguous scene target ID: {target_id}' |
| [demo/interaction/object_library.py:40](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:40) | Target requires MetadataRef type=model |
| [demo/interaction/object_library.py:46](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:46) | Invalid/conflicting model ID in MetadataRef |
| [demo/interaction/object_library.py:55](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:55) | RobotModelRef.grasp_frame calibration is required |
| [demo/interaction/object_library.py:81](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:81) | OLS grasp poses are empty or pose_count is inconsistent |
| [demo/interaction/object_library.py:63](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:63) | Grasp frame must descend from tip_link |
| [demo/interaction/object_library.py:66](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:66) | Current grasp frame adapter supports fixed tool joints only |
| [demo/interaction/object_library.py:86](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:86) | f'Invalid OLS grasp pose at index {index}' |
| [demo/interaction/object_library.py:88](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:88) | f'Invalid tolerance axis at index {index}' |
| [demo/interaction/object_library.py:114](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:114) | Target has no measured world pose |
| [demo/interaction/object_library.py:215](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:215) | f'OLS model {model_id} has invalid physical friction metadata' |
| [demo/interaction/object_library.py:218](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:218) | f'OLS model {model_id} has invalid physical friction values' |
| [demo/interaction/object_library.py:136](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:136) | grasp_index is outside the model candidate list |
| [demo/interaction/object_library.py:229](C:/Users/21474/Desktop/robot-agent/demo/interaction/object_library.py:229) | Object MetadataRef path must contain an integer OLS model ID |
| [demo/interaction/requests.py:39](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:39) | Held contact is not a registered manipulation target |
| [demo/interaction/requests.py:19](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:19) | f'Unknown articulated link: {link}' |
| [demo/interaction/requests.py:32](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:32) | A target-free push/pull requires an existing grasp |
| [demo/interaction/requests.py:46](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:46) | Specify either distance or angle_deg for relative motion |
| [demo/interaction/requests.py:48](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:48) | Relative motion cannot include an absolute mechanism goal |
| [demo/interaction/requests.py:51](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:51) | Relative motion frame must be tool or world |
| [demo/interaction/requests.py:70](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:70) | angle_deg must be a finite nonzero increment |
| [demo/interaction/requests.py:81](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:81) | arc_axis must not be zero |
| [demo/interaction/requests.py:95](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:95) | Automatic pull needs one handle bar and its supports on the selected link |
| [demo/interaction/requests.py:104](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:104) | Handle supports do not determine a unique outward normal |
| [demo/interaction/requests.py:110](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:110) | Handle bar axis and outward normal are not perpendicular |
| [demo/interaction/requests.py:124](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:124) | contact_mode must be grasp or contact |
| [demo/interaction/requests.py:138](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:138) | Use a relative distance instead of target_position_m |
| [demo/interaction/requests.py:148](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:148) | Release the held object before using contact push |
| [demo/interaction/requests.py:183](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:183) | Mechanism approach/retreat supports joint or linear move; use push/pull for arcs |
| [demo/interaction/requests.py:249](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:249) | Use a relative distance instead of target_position_m |
| [demo/interaction/requests.py:16](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:16) | f'Mechanism target requires a link when {len(links)} links are declared' |
| [demo/interaction/requests.py:57](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:57) | f'{name} must be a finite xyz vector' |
| [demo/interaction/requests.py:62](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:62) | distance must be positive metres |
| [demo/interaction/requests.py:66](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:66) | direction must not be zero |
| [demo/interaction/requests.py:73](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:73) | Specify arc_center or arc_center_offset, not both |
| [demo/interaction/requests.py:76](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:76) | arc_center must be a finite world xyz point |
| [demo/interaction/requests.py:127](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:127) | Only push supports contact without grasping |
| [demo/interaction/requests.py:129](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:129) | contact mode cannot include grasp configuration |
| [demo/interaction/requests.py:200](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:200) | The robot has no calibrated grasp_center site |
| [demo/interaction/requests.py:206](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:206) | mechanism_pose.frame must be grasp or control |
| [demo/interaction/requests.py:235](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:235) | f'Global {name} must contain one finite positive limit per joint' |
| [demo/interaction/requests.py:254](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:254) | contact_mode must be grasp, or contact for push |
| [demo/interaction/requests.py:256](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:256) | contact mode cannot include grasp configuration |
| [demo/interaction/requests.py:269](C:/Users/21474/Desktop/robot-agent/demo/interaction/requests.py:269) | Relative motion conflicts with the requested trajectory |
| [demo/interaction/visual_targets.py:29](C:/Users/21474/Desktop/robot-agent/demo/interaction/visual_targets.py:29) | f"{command['skill_name']} requires an object target" |
| [demo/interaction/vlm_grasp.py:31](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:31) | Model geometric_properties.AABB must contain six finite bounds |
| [demo/interaction/vlm_grasp.py:34](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:34) | Model AABB bounds must have positive extent |
| [demo/interaction/vlm_grasp.py:82](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:82) | Target is behind the requested camera |
| [demo/interaction/vlm_grasp.py:94](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:94) | Target projects outside the requested camera image |
| [demo/interaction/vlm_grasp.py:107](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:107) | VLM placement point is outside the target model bounds |
| [demo/interaction/vlm_grasp.py:110](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:110) | VLM placement point is too close to the target rim |
| [demo/interaction/vlm_grasp.py:112](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:112) | VLM placement point is on the target wall or rim |
| [demo/interaction/vlm_grasp.py:122](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:122) | VLM grasp depth is outside the target model bounds |
| [demo/interaction/vlm_grasp.py:129](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:129) | VLM grasp depth is on the hidden side; selected depth may belong to the table/background |
| [demo/interaction/vlm_grasp.py:150](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:150) | No visible basket floor depth inside the target model |
| [demo/interaction/vlm_grasp.py:164](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:164) | GripperModel.urdf is required for grasp depth |
| [demo/interaction/vlm_grasp.py:200](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:200) | Finger contact length is shorter than the tip margin |
| [demo/interaction/vlm_grasp.py:203](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:203) | RGB-D surface point is outside the model grasp depth |
| [demo/interaction/vlm_grasp.py:213](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:213) | VLM placement requires a confirmed held object |
| [demo/interaction/vlm_grasp.py:217](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:217) | f'Held object is missing from scene: {held_name}' |
| [demo/interaction/vlm_grasp.py:232](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:232) | Held object bottom is not below the grasp frame |
| [demo/interaction/vlm_grasp.py:170](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:170) | f'Missing {side} finger pad geometry' |
| [demo/interaction/vlm_grasp.py:183](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:183) | f'Missing {side} finger pad contact boxes' |
| [demo/interaction/vlm_grasp.py:361](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:361) | Visual grasp estimate was not recorded |
| [demo/interaction/vlm_grasp.py:180](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:180) | f'Invalid {side} finger pad box' |
| [demo/interaction/vlm_grasp.py:325](C:/Users/21474/Desktop/robot-agent/demo/interaction/vlm_grasp.py:325) | VLM placement estimate must include a pixel |
| [demo/run_demo.py:70](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:70) | type(value).__name__ |
| [demo/run_demo.py:664](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:664) | 0 if result['success'] else 2 if result.get('status') == 'suspend' else 1 |
| [demo/run_demo.py:258](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:258) | commands must be a non-empty array |
| [demo/run_demo.py:289](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:289) | commands must be a non-empty array |
| [demo/run_demo.py:300](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:300) | --collision-probe requires a scene containing probe_block; use the unified_scene_v2 test fixture |
| [demo/run_demo.py:385](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:385) | f'Initial state collides: {start_check.contacts}' |
| [demo/run_demo.py:304](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:304) | command_id is required and must be unique |
| [demo/run_demo.py:308](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:308) | f'This integration runner supports move_to/lift_up/put_down/grasp/release/approach/retreat/pick/put/push/pull/pour: {skill}' |
| [demo/run_demo.py:334](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:334) | f'{skill} requires an interaction object target' |
| [demo/run_demo.py:344](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:344) | Choose an external OLS URL or start the local service, not both |
| [demo/run_demo.py:389](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:389) | Visual estimates require the MuJoCo backend |
| [demo/run_demo.py:405](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:405) | f'Initial assembled robot collides: {start_check.contacts}' |
| [demo/run_demo.py:316](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:316) | pour source and target must be scene object names |
| [demo/run_demo.py:323](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:323) | f'{skill} requires an interaction object target' |
| [demo/run_demo.py:329](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:329) | Mechanisms use move/grasp/push/pull/release, not pick/put retreat sequences |
| [demo/run_demo.py:413](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:413) | --show-collision-geometry requires a MuJoCo viewer backend |
| [demo/run_demo.py:424](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:424) | FCL failed to detect deliberately inserted obstacle |
| [demo/run_demo.py:562](C:/Users/21474/Desktop/robot-agent/demo/run_demo.py:562) | f"Step failed: {row.get('error')}" |
| [demo/scene/__init__.py:39](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:39) | Held contact is not a registered manipulation target |
| [demo/scene/__init__.py:19](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:19) | f'Unknown articulated link: {link}' |
| [demo/scene/__init__.py:32](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:32) | A target-free push/pull requires an existing grasp |
| [demo/scene/__init__.py:46](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:46) | Specify either distance or angle_deg for relative motion |
| [demo/scene/__init__.py:48](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:48) | Relative motion cannot include an absolute mechanism goal |
| [demo/scene/__init__.py:51](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:51) | Relative motion frame must be tool or world |
| [demo/scene/__init__.py:70](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:70) | angle_deg must be a finite nonzero increment |
| [demo/scene/__init__.py:74](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:74) | arc_axis must not be zero |
| [demo/scene/__init__.py:88](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:88) | Automatic pull needs one handle bar and its supports on the selected link |
| [demo/scene/__init__.py:97](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:97) | Handle supports do not determine a unique outward normal |
| [demo/scene/__init__.py:103](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:103) | Handle bar axis and outward normal are not perpendicular |
| [demo/scene/__init__.py:117](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:117) | contact_mode must be grasp or contact |
| [demo/scene/__init__.py:131](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:131) | Use a relative distance instead of target_position_m |
| [demo/scene/__init__.py:141](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:141) | Release the held object before using contact push |
| [demo/scene/__init__.py:176](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:176) | Mechanism approach/retreat supports joint or linear move; use push/pull for arcs |
| [demo/scene/__init__.py:237](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:237) | Use a relative distance instead of target_position_m |
| [demo/scene/__init__.py:16](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:16) | f'Mechanism target requires a link when {len(links)} links are declared' |
| [demo/scene/__init__.py:57](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:57) | f'{name} must be a finite xyz vector' |
| [demo/scene/__init__.py:62](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:62) | distance must be positive metres |
| [demo/scene/__init__.py:66](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:66) | direction must not be zero |
| [demo/scene/__init__.py:120](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:120) | Only push supports contact without grasping |
| [demo/scene/__init__.py:122](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:122) | contact mode cannot include grasp configuration |
| [demo/scene/__init__.py:189](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:189) | The robot has no calibrated grasp_center site |
| [demo/scene/__init__.py:195](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:195) | mechanism_pose.frame must be grasp or control |
| [demo/scene/__init__.py:223](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:223) | f'Global {name} must contain one finite positive limit per joint' |
| [demo/scene/__init__.py:242](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:242) | contact_mode must be grasp, or contact for push |
| [demo/scene/__init__.py:244](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:244) | contact mode cannot include grasp configuration |
| [demo/scene/__init__.py:257](C:/Users/21474/Desktop/robot-agent/demo/scene/__init__.py:257) | Relative motion conflicts with the requested trajectory |
| [demo/scene/assembler.py:116](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:116) | f"Conflicting OLS IDs on {obj.get('object_name_in_scene')}" |
| [demo/scene/assembler.py:126](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:126) | OLS object_name must be a non-empty string |
| [demo/scene/assembler.py:45](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:45) | Transform parent cycle in scene |
| [demo/scene/assembler.py:51](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:51) | f"Invalid Transform for object {ident}" |
| [demo/scene/assembler.py:84](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:84) | f"Transform scale is zero for object {obj['object_id_in_scene']}" |
| [demo/scene/assembler.py:124](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:124) | OLS metadata is missing semantic_properties.object_name |
| [demo/scene/assembler.py:157](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:157) | f"Local OLS asset directory is missing: {directory}" |
| [demo/scene/assembler.py:175](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:175) | f"OLS asset has no executable file: {directory}" |
| [demo/scene/assembler.py:195](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:195) | OLS robot URDF has no valid root link |
| [demo/scene/assembler.py:202](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:202) | f"Robot tip link is absent from OLS URDF: {tip_link}" |
| [demo/scene/assembler.py:331](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:331) | Scene must contain a non-empty objects array |
| [demo/scene/assembler.py:368](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:368) | f"Expected one arm object, found {len(robots)}" |
| [demo/scene/assembler.py:61](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:61) | f"Transform parent {parent} is missing" |
| [demo/scene/assembler.py:77](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:77) | f"Invalid quaternion for object {obj['object_id_in_scene']}" |
| [demo/scene/assembler.py:146](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:146) | f"OLS returned invalid {asset_type} metadata for {object_id}" |
| [demo/scene/assembler.py:375](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:375) | Robot arm must reference an OLS robot asset |
| [demo/scene/assembler.py:165](C:/Users/21474/Desktop/robot-agent/demo/scene/assembler.py:165) | f"Invalid OLS file manifest: {manifest_path}" |
| [demo/task_commands/converter.py:166](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:166) | f"command {index} uses unsupported skill {skill_name!r}" |
| [demo/task_commands/converter.py:556](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:556) | configuration overrides must be objects |
| [demo/task_commands/converter.py:616](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:616) | reference_pose quaternion must not be zero |
| [demo/task_commands/converter.py:67](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:67) | interaction registry must contain an objects mapping |
| [demo/task_commands/converter.py:69](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:69) | interaction registry must contain a scene path |
| [demo/task_commands/converter.py:92](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:92) | command document must contain a commands list |
| [demo/task_commands/converter.py:127](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:127) | f"command {index} must be an object" |
| [demo/task_commands/converter.py:136](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:136) | f"command {index} has no valid skill_name" |
| [demo/task_commands/converter.py:138](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:138) | f"command {index} parameters must be an object" |
| [demo/task_commands/converter.py:178](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:178) | f"{skill_name} distance must be positive and finite" |
| [demo/task_commands/converter.py:180](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:180) | f"{skill_name} requires a current end-effector pose" |
| [demo/task_commands/converter.py:207](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:207) | f"move command {index} has no valid target" |
| [demo/task_commands/converter.py:212](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:212) | pick requires an interaction object target |
| [demo/task_commands/converter.py:245](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:245) | f"move command {index} has unsupported path type {path_type!r}" |
| [demo/task_commands/converter.py:324](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:324) | pick requires a validated interaction pre-point and contact path |
| [demo/task_commands/converter.py:394](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:394) | f"{skill_name} command {index} has no valid target" |
| [demo/task_commands/converter.py:399](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:399) | f"target {target_id!r} does not define action_requests.{skill_name}" |
| [demo/task_commands/converter.py:415](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:415) | f"target {target_id!r} has no spatial configuration" |
| [demo/task_commands/converter.py:431](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:431) | f"target {target_id!r} has an invalid reference_pose" |
| [demo/task_commands/converter.py:442](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:442) | f"target {target_id!r} has no anchor {anchor_key!r}" |
| [demo/task_commands/converter.py:448](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:448) | f"target {target_id!r} has no valid local direction for relation {relation!r}" |
| [demo/task_commands/converter.py:464](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:464) | f"target {target_id!r} has no tool orientation" |
| [demo/task_commands/converter.py:484](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:484) | f"move command {index} targets the end effector before any pose target is known" |
| [demo/task_commands/converter.py:498](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:498) | f"move command {index} has unknown relation {relation!r}" |
| [demo/task_commands/converter.py:509](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:509) | f"move command {index} has invalid distance_m" |
| [demo/task_commands/converter.py:513](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:513) | f"move command {index} relative target has no xyz position" |
| [demo/task_commands/converter.py:521](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:521) | f"command {index} target {target_id!r} is not defined in the interaction registry" |
| [demo/task_commands/converter.py:526](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:526) | f"command {index} target {target_id!r} is ambiguous in the interaction registry" |
| [demo/task_commands/converter.py:98](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:98) | f"command {index} must be an object" |
| [demo/task_commands/converter.py:143](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:143) | put requires a scene object destination in demo.run_demo |
| [demo/task_commands/converter.py:146](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:146) | put requires an interaction object destination |
| [demo/task_commands/converter.py:156](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:156) | f"{skill_name} command {index} has no valid target" |
| [demo/task_commands/converter.py:158](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:158) | f"{skill_name} uses target; target_pose is resolved by the executor" |
| [demo/task_commands/converter.py:176](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:176) | f"{skill_name} requires a numeric distance" |
| [demo/task_commands/converter.py:505](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:505) | f"move command {index} has invalid distance_m" |
| [demo/task_commands/converter.py:218](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:218) | f"named target {target_id!r} does not expose a pose for relative motion" |
| [demo/task_commands/converter.py:375](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:375) | approach collision checker returned an invalid batch result |
| [demo/task_commands/converter.py:230](C:/Users/21474/Desktop/robot-agent/demo/task_commands/converter.py:230) | Object-ID interaction motion does not support relation/distance_m/anchor offsets |
| [demo/tools/benchmark_fcl_native.py:52](C:/Users/21474/Desktop/robot-agent/demo/tools/benchmark_fcl_native.py:52) | f'Native filtering changed collision result at sample {index}: {results}' |
| [execution/assembly.py:28](C:/Users/21474/Desktop/robot-agent/execution/assembly.py:28) | Attachment must connect an existing parent link to the child URDF root |
| [execution/assembly.py:30](C:/Users/21474/Desktop/robot-agent/execution/assembly.py:30) | Attachment link names must be unique |
| [execution/assembly.py:35](C:/Users/21474/Desktop/robot-agent/execution/assembly.py:35) | Attachment requires a finite parent-link pose and unit quaternion |
| [execution/assembly.py:15](C:/Users/21474/Desktop/robot-agent/execution/assembly.py:15) | filename |
| [execution/assembly.py:41](C:/Users/21474/Desktop/robot-agent/execution/assembly.py:41) | 'Duplicate attachment element name: ' + e.get('name') |
| [execution/assembly.py:45](C:/Users/21474/Desktop/robot-agent/execution/assembly.py:45) | Nonzero mimic offsets are unsupported |
| [execution/backends/mujoco/assembly.py:28](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/assembly.py:28) | Attachment must connect an existing parent link to the child URDF root |
| [execution/backends/mujoco/assembly.py:30](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/assembly.py:30) | Attachment link names must be unique |
| [execution/backends/mujoco/assembly.py:35](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/assembly.py:35) | Attachment requires a finite parent-link pose and unit quaternion |
| [execution/backends/mujoco/assembly.py:15](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/assembly.py:15) | filename |
| [execution/backends/mujoco/assembly.py:41](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/assembly.py:41) | 'Duplicate attachment element name: ' + e.get('name') |
| [execution/backends/mujoco/assembly.py:45](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/assembly.py:45) | Nonzero mimic offsets are unsupported |
| [execution/backends/mujoco/backend.py:140](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:140) | MuJoCo plugin expects format=generate_from_scene |
| [execution/backends/mujoco/backend.py:167](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:167) | Floating robot bases are not supported |
| [execution/backends/mujoco/backend.py:298](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:298) | trajectory.fps cannot exceed the physics step frequency |
| [execution/backends/mujoco/backend.py:312](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:312) | f'Missing MuJoCo resource: {name}' |
| [execution/backends/mujoco/backend.py:436](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:436) | Viewer closed during execution |
| [execution/backends/mujoco/backend.py:468](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:468) | Explicit pose edits only support static objects |
| [execution/backends/mujoco/backend.py:480](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:480) | Trajectory requires finite NxDOF joints and nondecreasing timestamps from zero |
| [execution/backends/mujoco/backend.py:485](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:485) | Different joint positions share a trajectory timestamp |
| [execution/backends/mujoco/backend.py:489](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:489) | Trajectory requires a positive duration |
| [execution/backends/mujoco/backend.py:491](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:491) | Execution timeout must be finite and positive |
| [execution/backends/mujoco/backend.py:493](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:493) | Trajectory exceeds joint limits |
| [execution/backends/mujoco/backend.py:495](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:495) | Trajectory start differs from execution state |
| [execution/backends/mujoco/backend.py:172](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:172) | Dynamic environment URDF requires an articulated-object adapter |
| [execution/backends/mujoco/backend.py:183](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:183) | Only static environment URDFs are supported |
| [execution/backends/mujoco/backend.py:201](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:201) | URDF TCP link missing from imported MJCF |
| [execution/backends/mujoco/backend.py:583](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:583) | reason |
| [execution/backends/mujoco/backend.py:209](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:209) | Configured grasp frame link is absent from MJCF |
| [execution/backends/mujoco/backend.py:231](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:231) | f'Non-uniform cylinder scale is unsupported: {name}' |
| [execution/backends/mujoco/backend.py:257](C:/Users/21474/Desktop/robot-agent/execution/backends/mujoco/backend.py:257) | f'Unsupported geometry: {name}' |
| [execution/blackboard.py:93](C:/Users/21474/Desktop/robot-agent/execution/blackboard.py:93) | f'Target has no measured world pose: {name}' |
| [execution/config.py:15](C:/Users/21474/Desktop/robot-agent/execution/config.py:15) | Config requires motion and trajectory objects |
| [execution/config.py:21](C:/Users/21474/Desktop/robot-agent/execution/config.py:21) | f'Invalid {section} config keys; expected {sorted(keys)}' |
| [execution/config.py:24](C:/Users/21474/Desktop/robot-agent/execution/config.py:24) | f'{section}.{key} must be finite numeric data' |
| [execution/config.py:26](C:/Users/21474/Desktop/robot-agent/execution/config.py:26) | f'motion.{key} must be between 0.01 and 1.0' |
| [execution/config.py:28](C:/Users/21474/Desktop/robot-agent/execution/config.py:28) | trajectory.fps must be a positive integer |
| [execution/config.py:30](C:/Users/21474/Desktop/robot-agent/execution/config.py:30) | trajectory.pause_seconds must be nonnegative |
| [execution/convergence.py:10](C:/Users/21474/Desktop/robot-agent/execution/convergence.py:10) | Contact monitor interval must be positive |
| [execution/motion_limits.py:13](C:/Users/21474/Desktop/robot-agent/execution/motion_limits.py:13) | f'URDF joint {name} has no velocity limit' |
| [execution/motion_limits.py:18](C:/Users/21474/Desktop/robot-agent/execution/motion_limits.py:18) | f'URDF joint {name} requires positive finite velocity/acceleration limits' |
| [execution/mujoco.py:140](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:140) | MuJoCo plugin expects format=generate_from_scene |
| [execution/mujoco.py:165](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:165) | Floating robot bases are not supported |
| [execution/mujoco.py:298](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:298) | trajectory.fps cannot exceed the physics step frequency |
| [execution/mujoco.py:312](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:312) | f'Missing MuJoCo resource: {name}' |
| [execution/mujoco.py:448](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:448) | Viewer closed during execution |
| [execution/mujoco.py:480](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:480) | Explicit pose edits only support static objects |
| [execution/mujoco.py:492](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:492) | Trajectory requires finite NxDOF joints and nondecreasing timestamps from zero |
| [execution/mujoco.py:497](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:497) | Different joint positions share a trajectory timestamp |
| [execution/mujoco.py:501](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:501) | Trajectory requires a positive duration |
| [execution/mujoco.py:503](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:503) | Execution timeout must be finite and positive |
| [execution/mujoco.py:505](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:505) | Trajectory exceeds joint limits |
| [execution/mujoco.py:507](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:507) | Trajectory start differs from execution state |
| [execution/mujoco.py:170](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:170) | Dynamic environment URDF requires an articulated-object adapter |
| [execution/mujoco.py:181](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:181) | Only static environment URDFs are supported |
| [execution/mujoco.py:199](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:199) | URDF TCP link missing from imported MJCF |
| [execution/mujoco.py:595](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:595) | reason |
| [execution/mujoco.py:207](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:207) | Configured grasp frame link is absent from MJCF |
| [execution/mujoco.py:253](C:/Users/21474/Desktop/robot-agent/execution/mujoco.py:253) | f'Unsupported geometry: {name}' |
| [execution/registry.py:27](C:/Users/21474/Desktop/robot-agent/execution/registry.py:27) | f'Execution backend already registered: {name}' |
| [execution/registry.py:35](C:/Users/21474/Desktop/robot-agent/execution/registry.py:35) | f'Unknown execution backend: {name}' |
| [execution/resources.py:66](C:/Users/21474/Desktop/robot-agent/execution/resources.py:66) | Runtime configuration scene identity mismatch |
| [execution/resources.py:70](C:/Users/21474/Desktop/robot-agent/execution/resources.py:70) | Unsupported runtime scene settings |
| [execution/resources.py:108](C:/Users/21474/Desktop/robot-agent/execution/resources.py:108) | Transform requires finite position and unit xyzw quaternion |
| [execution/resources.py:111](C:/Users/21474/Desktop/robot-agent/execution/resources.py:111) | Transform requires finite position and unit xyzw quaternion |
| [execution/resources.py:115](C:/Users/21474/Desktop/robot-agent/execution/resources.py:115) | This scene compiler currently requires world-parented objects with unit scale |
| [execution/resources.py:76](C:/Users/21474/Desktop/robot-agent/execution/resources.py:76) | f'Runtime configuration references unknown object: {ident}' |
| [execution/resources.py:92](C:/Users/21474/Desktop/robot-agent/execution/resources.py:92) | Only camera/light objects may be excluded from the control scene |
| [execution/resources.py:138](C:/Users/21474/Desktop/robot-agent/execution/resources.py:138) | f'Missing scene asset: {path}' |
| [execution/resources.py:149](C:/Users/21474/Desktop/robot-agent/execution/resources.py:149) | Task requires schema_version=2.0 and request_id |
| [execution/resources.py:162](C:/Users/21474/Desktop/robot-agent/execution/resources.py:162) | Unknown scene_id or scene_version mismatch |
| [execution/resources.py:166](C:/Users/21474/Desktop/robot-agent/execution/resources.py:166) | Supported scene_schema_version values are 1 and 2 |
| [execution/resources.py:168](C:/Users/21474/Desktop/robot-agent/execution/resources.py:168) | Scene identity disagrees with catalog/task |
| [execution/resources.py:206](C:/Users/21474/Desktop/robot-agent/execution/resources.py:206) | Exactly one robot is supported by this runner |
| [execution/resources.py:209](C:/Users/21474/Desktop/robot-agent/execution/resources.py:209) | Scene RobotModelRef must define urdf |
| [execution/resources.py:213](C:/Users/21474/Desktop/robot-agent/execution/resources.py:213) | f'Missing robot URDF: {urdf}' |
| [execution/resources.py:217](C:/Users/21474/Desktop/robot-agent/execution/resources.py:217) | Duplicate scene object ID/name |
| [execution/resources.py:242](C:/Users/21474/Desktop/robot-agent/execution/resources.py:242) | Scene robot base_link must be the URDF root |
| [execution/resources.py:244](C:/Users/21474/Desktop/robot-agent/execution/resources.py:244) | f'No resources for execution backend {backend}' |
| [execution/resources.py:251](C:/Users/21474/Desktop/robot-agent/execution/resources.py:251) | RobotDriver joint_sequence must match URDF revolute joints |
| [execution/resources.py:254](C:/Users/21474/Desktop/robot-agent/execution/resources.py:254) | Scene RobotDriver must define initial_joint_positions_rad |
| [execution/resources.py:257](C:/Users/21474/Desktop/robot-agent/execution/resources.py:257) | Invalid initial_joints |
| [execution/resources.py:302](C:/Users/21474/Desktop/robot-agent/execution/resources.py:302) | Collision resource mismatch |
| [execution/resources.py:305](C:/Users/21474/Desktop/robot-agent/execution/resources.py:305) | Initial joint state mismatch |
| [execution/resources.py:307](C:/Users/21474/Desktop/robot-agent/execution/resources.py:307) | Joint order mismatch |
| [execution/resources.py:309](C:/Users/21474/Desktop/robot-agent/execution/resources.py:309) | Robot base pose mismatch |
| [execution/resources.py:314](C:/Users/21474/Desktop/robot-agent/execution/resources.py:314) | f'URDF/TCP mismatch: {pos_error}, {rot_error}' |
| [execution/resources.py:376](C:/Users/21474/Desktop/robot-agent/execution/resources.py:376) | Moving robot bases are not supported in this release |
| [execution/resources.py:81](C:/Users/21474/Desktop/robot-agent/execution/resources.py:81) | f'Unsupported or duplicate runtime component: {kind}' |
| [execution/resources.py:231](C:/Users/21474/Desktop/robot-agent/execution/resources.py:231) | Only fixed attachments to the scene robot are supported |
| [execution/resources.py:233](C:/Users/21474/Desktop/robot-agent/execution/resources.py:233) | Attachment Transform.parent disagrees with RobotAttachment |
| [execution/resources.py:235](C:/Users/21474/Desktop/robot-agent/execution/resources.py:235) | Only one gripper attachment is supported |
| [execution/resources.py:267](C:/Users/21474/Desktop/robot-agent/execution/resources.py:267) | Object names require ASCII letters, numbers, underscore or hyphen |
| [execution/resources.py:269](C:/Users/21474/Desktop/robot-agent/execution/resources.py:269) | Duplicate scene object ID/name |
| [execution/resources.py:273](C:/Users/21474/Desktop/robot-agent/execution/resources.py:273) | Articulated end effectors require a separate execution adapter |
| [execution/resources.py:277](C:/Users/21474/Desktop/robot-agent/execution/resources.py:277) | Disabled colliders are not supported by this test pipeline |
| [execution/resources.py:330](C:/Users/21474/Desktop/robot-agent/execution/resources.py:330) | f'Object mismatch: {name}' |
| [execution/resources.py:280](C:/Users/21474/Desktop/robot-agent/execution/resources.py:280) | Specify either primitive geometry or AssetRef, not both |
| [execution/resources.py:283](C:/Users/21474/Desktop/robot-agent/execution/resources.py:283) | Primitive size must contain three positive dimensions |
| [execution/resources.py:285](C:/Users/21474/Desktop/robot-agent/execution/resources.py:285) | Cylinder size is [radius, radius, full_height] |
| [execution/resources.py:287](C:/Users/21474/Desktop/robot-agent/execution/resources.py:287) | Environment requires a box collider or AssetRef |
| [execution/resources.py:291](C:/Users/21474/Desktop/robot-agent/execution/resources.py:291) | asset_path |
| [execution/resources.py:293](C:/Users/21474/Desktop/robot-agent/execution/resources.py:293) | Supported environment assets: URDF, OBJ, STL |
| [execution/resources.py:323](C:/Users/21474/Desktop/robot-agent/execution/resources.py:323) | f'Articulated base pose mismatch: {name}' |
| [execution/resources.py:337](C:/Users/21474/Desktop/robot-agent/execution/resources.py:337) | f'FCL primitive dimensions mismatch: {name}' |
| [execution/resources.py:229](C:/Users/21474/Desktop/robot-agent/execution/resources.py:229) | Gripper MetadataRef path must contain an integer OLS model ID |
| [execution/scene_catalog.py:19](C:/Users/21474/Desktop/robot-agent/execution/scene_catalog.py:19) | f"Scene document must be an object: {path}" |
| [execution/scene_catalog.py:23](C:/Users/21474/Desktop/robot-agent/execution/scene_catalog.py:23) | f"Scene must define scene_id and integer scene_version: {path}" |
| [execution/trajectory_export.py:319](C:/Users/21474/Desktop/robot-agent/execution/trajectory_export.py:319) | _main() |
| [execution/trajectory_export.py:133](C:/Users/21474/Desktop/robot-agent/execution/trajectory_export.py:133) | TRAJ segment_count exceeds the uint16 format limit |
| [execution/trajectory_export.py:157](C:/Users/21474/Desktop/robot-agent/execution/trajectory_export.py:157) | TRAJ export requires exactly one robot object |
| [execution/trajectory_export.py:241](C:/Users/21474/Desktop/robot-agent/execution/trajectory_export.py:241) | file is shorter than the 32-byte TRAJ header |
| [execution/trajectory_export.py:298](C:/Users/21474/Desktop/robot-agent/execution/trajectory_export.py:298) | "TRAJ validation failed: " + "; ".join(errors) |
| [execution/trajectory_export.py:38](C:/Users/21474/Desktop/robot-agent/execution/trajectory_export.py:38) | TRAJ supports at most six driven joints per object |
| [execution/trajectory_export.py:112](C:/Users/21474/Desktop/robot-agent/execution/trajectory_export.py:112) | TRAJ frames require unique nonnegative frame_index values |
| [execution/trajectory_export.py:120](C:/Users/21474/Desktop/robot-agent/execution/trajectory_export.py:120) | f'Command {command!r} has non-contiguous execution frames' |
| [sensors/camera/manager.py:14](C:/Users/21474/Desktop/robot-agent/sensors/camera/manager.py:14) | At least one camera is required. |
| [sensors/camera/manager.py:22](C:/Users/21474/Desktop/robot-agent/sensors/camera/manager.py:22) | f"Unknown camera '{camera_id}'. Available cameras: {available}" |
| [sensors/camera_mujoco/camera.py:42](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/camera.py:42) | Camera width and height must be positive. |
| [sensors/camera_mujoco/camera.py:75](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/camera.py:75) | MuJoCo camera is closed. |
| [sensors/camera_mujoco/camera.py:46](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/camera.py:46) | MujocoCamera requires the 'mujoco' package. |
| [sensors/camera_mujoco/camera.py:54](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/camera.py:54) | f"MuJoCo camera not found: {camera_name}" |
| [sensors/camera_mujoco/camera.py:59](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/camera.py:59) | f"MuJoCo camera id out of range: {self._camera_id}" |
| [sensors/camera_mujoco/scene.py:27](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/scene.py:27) | f"Expected one camera asset manifest in {directory}" |
| [sensors/camera_mujoco/scene.py:31](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/scene.py:31) | f"Camera visual mesh not found: {mesh}" |
| [sensors/camera_mujoco/scene.py:35](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/scene.py:35) | f"Camera visual texture not found: {texture}" |
| [sensors/camera_mujoco/scene.py:52](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/scene.py:52) | f"Multiple scene JSON files for scene {scene_id}; provide scene_json_path." |
| [sensors/camera_mujoco/scene.py:43](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/scene.py:43) | f"Scene JSON not found: {path}" |
| [sensors/camera_mujoco/scene.py:91](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/scene.py:91) | f"Invalid camera quaternion for {camera_name}." |
| [sensors/camera_mujoco/scene.py:85](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/scene.py:85) | f"Unsupported camera parent object: {parent_object}" |
| [sensors/camera_mujoco/scene.py:88](C:/Users/21474/Desktop/robot-agent/sensors/camera_mujoco/scene.py:88) | Wrist camera parent body robotiq_2f85_mount is absent from MJCF. |
| [control/binding_gripper.py:36](C:/Users/21474/Desktop/robot-agent/control/binding_gripper.py:36) | f'Physical grasp requires the articulated gripper backend: missing {name}' |
| [control/binding_gripper.py:95](C:/Users/21474/Desktop/robot-agent/control/binding_gripper.py:95) | Gripper opening timed out |
| [control/binding_gripper.py:104](C:/Users/21474/Desktop/robot-agent/control/binding_gripper.py:104) | Release the held object before acquiring another grasp |
| [control/binding_gripper.py:172](C:/Users/21474/Desktop/robot-agent/control/binding_gripper.py:172) | Physical hold force must be in (0, 40] N |
| [control/continuous_trajectory.py:19](C:/Users/21474/Desktop/robot-agent/control/continuous_trajectory.py:19) | path and fractions must contain at least two matching samples |
| [control/continuous_trajectory.py:21](C:/Users/21474/Desktop/robot-agent/control/continuous_trajectory.py:21) | path and fractions must be finite |
| [control/continuous_trajectory.py:23](C:/Users/21474/Desktop/robot-agent/control/continuous_trajectory.py:23) | fractions must be strictly increasing and end above zero |
| [control/continuous_trajectory.py:66](C:/Users/21474/Desktop/robot-agent/control/continuous_trajectory.py:66) | Local timing requires finite positive joint limits and timestep |
| [control/continuous_trajectory.py:113](C:/Users/21474/Desktop/robot-agent/control/continuous_trajectory.py:113) | Joint velocity limits must be finite and positive |
| [control/continuous_trajectory.py:162](C:/Users/21474/Desktop/robot-agent/control/continuous_trajectory.py:162) | corner_slowdown must be finite and nonnegative |
| [control/continuous_trajectory.py:37](C:/Users/21474/Desktop/robot-agent/control/continuous_trajectory.py:37) | f"Unsupported path interpolator: {interpolator}" |
| [control/continuous_trajectory.py:117](C:/Users/21474/Desktop/robot-agent/control/continuous_trajectory.py:117) | Joint acceleration limits must be finite and positive |
| [control/continuous_trajectory.py:148](C:/Users/21474/Desktop/robot-agent/control/continuous_trajectory.py:148) | Joint jerk limits must be finite and positive |
| [control/continuous_trajectory.py:154](C:/Users/21474/Desktop/robot-agent/control/continuous_trajectory.py:154) | Task speed limit must be finite and positive |
| [control/gripper_controller.py:45](C:/Users/21474/Desktop/robot-agent/control/gripper_controller.py:45) | f"MuJoCo gripper object not found: {name}" |
| [control/gripper_controller.py:130](C:/Users/21474/Desktop/robot-agent/control/gripper_controller.py:130) | Gripper motion timed out. |
| [control/press_controller.py:57](C:/Users/21474/Desktop/robot-agent/control/press_controller.py:57) | Calibrated force feedback is unavailable. |
| [control/press_controller.py:105](C:/Users/21474/Desktop/robot-agent/control/press_controller.py:105) | Press execution timed out. |
| [control/press_controller.py:127](C:/Users/21474/Desktop/robot-agent/control/press_controller.py:127) | Unexpected local collision during press. |
| [control/press_controller.py:130](C:/Users/21474/Desktop/robot-agent/control/press_controller.py:130) | Maximum press force exceeded. |
| [control/press_controller.py:111](C:/Users/21474/Desktop/robot-agent/control/press_controller.py:111) | f"Local press IK failed at travel {travel:.6f} m: {exc}" |
| [control/pull_push_controller.py:44](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:44) | Target must identify one movable body, not the cabinet root. |
| [control/pull_push_controller.py:136](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:136) | f'Forbidden contact: {body_a} ({geom_a}) ↔ {body_b} ({geom_b}); distance={distance:.6g}' |
| [control/pull_push_controller.py:174](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:174) | FCL detected a forbidden robot/environment collision. |
| [control/pull_push_controller.py:222](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:222) | Unsupported end-effector path type. |
| [control/pull_push_controller.py:307](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:307) | Unsupported end-effector path type. |
| [control/pull_push_controller.py:312](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:312) | Robot is not at the prepared interaction configuration. |
| [control/pull_push_controller.py:320](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:320) | Target state changed after the path was prepared. |
| [control/pull_push_controller.py:357](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:357) | Establish measured pad contact before Push/Pull. |
| [control/pull_push_controller.py:359](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:359) | Initial contact exceeds maximum_force. |
| [control/pull_push_controller.py:530](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:530) | Unsupported end-effector path type. |
| [control/pull_push_controller.py:567](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:567) | Establish measured pad contact before Push/Pull. |
| [control/pull_push_controller.py:576](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:576) | Initial contact exceeds maximum_force. |
| [control/pull_push_controller.py:590](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:590) | Tool Z axis is not aligned with contact normal. |
| [control/pull_push_controller.py:609](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:609) | Configured travel limit reached. |
| [control/pull_push_controller.py:634](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:634) | Requested motion pulls away from the surface; non-grasped contact can only push. |
| [control/pull_push_controller.py:799](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:799) | No complete path. |
| [control/pull_push_controller.py:367](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:367) | contact_point is not at the measured pad contact. |
| [control/pull_push_controller.py:443](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:443) | Arm did not reach the planned endpoint. |
| [control/pull_push_controller.py:456](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:456) | Requested forward stroke ended without measured pad contact. |
| [control/pull_push_controller.py:458](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:458) | Actual target motion did not reach the requested endpoint. |
| [control/pull_push_controller.py:548](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:548) | contact_geom must belong to the target link. |
| [control/pull_push_controller.py:555](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:555) | Invalid prepared interaction joint state. |
| [control/pull_push_controller.py:618](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:618) | Mechanism joint limit reached. |
| [control/pull_push_controller.py:629](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:629) | contact_point is not at the measured pad contact. |
| [control/pull_push_controller.py:808](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:808) | Trajectory validation timeout. |
| [control/pull_push_controller.py:917](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:917) | Arm did not reach the planned endpoint. |
| [control/pull_push_controller.py:930](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:930) | Requested forward stroke ended without measured pad contact. |
| [control/pull_push_controller.py:933](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:933) | Actual target motion did not reach the requested endpoint. |
| [control/pull_push_controller.py:408](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:408) | Measured contact force exceeded maximum_force. |
| [control/pull_push_controller.py:441](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:441) | Execution timed out. |
| [control/pull_push_controller.py:692](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:692) | Smoothed path exceeds Cartesian path tolerance. |
| [control/pull_push_controller.py:717](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:717) | Planning timeout. |
| [control/pull_push_controller.py:766](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:766) | No collision-free IK branch at path sample. |
| [control/pull_push_controller.py:769](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:769) | IK returned no finite joint solution |
| [control/pull_push_controller.py:777](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:777) | IK reaches arm joint limits |
| [control/pull_push_controller.py:783](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:783) | No continuous IK continuation |
| [control/pull_push_controller.py:879](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:879) | Measured contact force exceeded maximum_force. |
| [control/pull_push_controller.py:915](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:915) | Execution timed out. |
| [control/pull_push_controller.py:396](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:396) | Physics became unstable. |
| [control/pull_push_controller.py:425](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:425) | Measured target contact lost. |
| [control/pull_push_controller.py:762](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:762) | No continuous IK branch at path sample. |
| [control/pull_push_controller.py:867](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:867) | Physics became unstable. |
| [control/pull_push_controller.py:899](C:/Users/21474/Desktop/robot-agent/control/pull_push_controller.py:899) | Measured target contact lost. |
| [services/object_library.py:18](C:/Users/21474/Desktop/robot-agent/services/object_library.py:18) | Object library URL must be an HTTP(S) base URL |
| [services/object_library.py:20](C:/Users/21474/Desktop/robot-agent/services/object_library.py:20) | Object library uid must be a nonnegative integer |
| [services/object_library.py:26](C:/Users/21474/Desktop/robot-agent/services/object_library.py:26) | f'{asset_type.title()} ID must be a nonnegative integer' |
| [services/object_library.py:39](C:/Users/21474/Desktop/robot-agent/services/object_library.py:39) | f'OLS returned inconsistent {asset_type} identity for {asset_id}' |
| [services/object_library.py:32](C:/Users/21474/Desktop/robot-agent/services/object_library.py:32) | Model metadata exceeds 2 MiB |
| [services/object_library.py:35](C:/Users/21474/Desktop/robot-agent/services/object_library.py:35) | f'OLS {asset_type} {asset_id}: HTTP {exc.code}' |
| [services/object_library.py:37](C:/Users/21474/Desktop/robot-agent/services/object_library.py:37) | f'OLS {asset_type} {asset_id} query failed: {exc}' |
