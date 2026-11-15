"""Offline planning replay; saved finger width approximates the original finger state."""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))
os.environ['SIM_PLATFORM_LIBRARY'] = str(ROOT / 'libraries/simplatform/bin/sim_platform_c.dll')
import numpy as np
from execution import SceneResourceCoordinator, ExecutionBackendRegistry, SceneStateSynchronizer
from execution.resources import component, pose_matrix
from libraries.eaik import load_urdf

output = ROOT / 'requests/commands.003/output'
report = json.loads((output / 'report.json').read_text(encoding='utf-8'))
task = json.loads((output.parent / 'commands.003.json').read_text(encoding='utf-8'))
catalog = ROOT / 'assets/catalog.json'
with patch('execution.tools.catalog_migration.register_scene', return_value=json.loads(catalog.read_text(encoding='utf-8'))):
    resources = SceneResourceCoordinator(catalog).resolve(task, 'mujoco')
ref = component(resources.robot_object, 'RobotModelRef')
kin = load_urdf(resources.robot_urdf, base_link=ref['base_link'], tip_link=ref['tip_link'],
               tcp_transform=ref.get('tcp_transform'), world_from_base=pose_matrix(resources.robot_object),
               initial_joints=resources.initial_joints)
backend = ExecutionBackendRegistry().create('mujoco')
from demo.collision.grasp_collision import GraspCollisionChecker
from demo.interaction.table_clearance import set_static_gripper_width
from atomic_action.motion.common.action import _MoveExecutor
from atomic_action.motion.common.utils.validation import validate_move_request
checker = GraspCollisionChecker(scene_file=resources.scene_file, scene_data=resources.scene,
    robot_urdf=resources.robot_urdf, initial_joints=resources.initial_joints, asset_root=resources.asset_root)
try:
    backend.load(resources, output / 'approach_budget_probe')
    checker.bind_backend(backend)
    backend.gripper_controller = backend.create_gripper_controller()
    import mujoco
    state = report['final_state']
    backend.data.qpos[backend.qpos_indices] = state['joints']
    backend.data.ctrl[backend.actuator_ids] = state['joints']
    for name, pose in state['objects'].items():
        body = backend.object_ids[name]
        if backend.model.body_jntnum[body] and int(backend.model.jnt_type[backend.model.body_jntadr[body]]) == int(mujoco.mjtJoint.mjJNT_FREE):
            address = backend.model.jnt_qposadr[backend.model.body_jntadr[body]]
            backend.data.qpos[address:address + 3] = pose['position']
            backend.data.qpos[address + 3:address + 7] = np.asarray(pose['quaternion_xyzw'])[[3, 0, 1, 2]]
    set_static_gripper_width(backend.model, backend.data, state['gripper']['width'])
    mujoco.mj_kinematics(backend.model, backend.data)
    controller = backend.gripper_controller
    controller.held_name = state['gripper']['object_id']
    controller.confirmed = controller._holding = True
    controller.relative_pose = controller._relative(backend.object_ids[controller.held_name])
    SceneStateSynchronizer().sync(backend, kin, checker)
    executor = _MoveExecutor(robot_runtime=backend, kinematics=kin, collision_checker=checker)
    request = validate_move_request(report['steps'][-1]['substeps'][0]['request'])
    context = executor._get_runtime_context(request)
    selection = executor.selector.select(request, context)
    path = executor.strategies[selection['path_strategy']].build_path_request(request, context)
    print('ik_candidates', len(path.get('candidate_goals', [])), flush=True)
    started = time.perf_counter()
    try:
        trajectory = executor._plan_with_fallback(request, context, selection, path)
        result = {'success': True, 'elapsed_s': time.perf_counter() - started,
                  'selection': selection, 'diagnostics': trajectory.get('diagnostics'),
                  'waypoint_count': len(trajectory['waypoints'])}
    except Exception as exc:
        result = {'success': False, 'elapsed_s': time.perf_counter() - started,
                  'error': str(exc), 'details': getattr(exc, 'details', None)}
    result.update(state_basis='saved arm/object poses; finger positions reconstructed from saved width',
                  physics_steps=backend.physics_steps)
    (output / 'approach_budget_replan.json').write_text(json.dumps(result, ensure_ascii=False, indent=2,
        default=lambda x: x.tolist() if isinstance(x, np.ndarray) else str(x)), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ('details', 'diagnostics')}, ensure_ascii=True), flush=True)
    print('search_summary', (result.get('diagnostics') or {}).get('configuration_selection') or
          {k: v for k, v in (result.get('details') or {}).items() if k != 'rejected'}, flush=True)
finally:
    backend.close()
    checker.close()

