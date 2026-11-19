from types import SimpleNamespace
import mujoco
import numpy as np

from demo.final_scene import final_scene
from demo.output_layout import summary_view


def test_current_joint_export_preserves_deployment_pose():
    model = mujoco.MjModel.from_xml_string('''<mujoco><worldbody><body name="arm">
    <joint name="j" type="hinge"/><geom type="sphere" size=".1"/>
    </body></worldbody></mujoco>''')
    data = mujoco.MjData(model)
    data.qpos[0] = .7
    obj = {'object_id_in_scene': 2, 'object_name_in_scene': 'arm', 'components': [
        {'component_type': 'Transform', 'properties': {'position': [0, 0, 0]}},
        {'component_type': 'RobotDriver', 'properties': {
            'joint_sequence': ['j'], 'initial_joint_positions_rad': {'j': .1}}}]}
    backend = SimpleNamespace(model=model, data=data, resources=SimpleNamespace(robot_object=obj))
    result = final_scene({'objects': [obj]}, backend, {'objects': {}})
    driver = result['objects'][0]['components'][1]['properties']
    assert driver['initial_joint_positions_rad'] == {'j': .1}
    assert driver['now_joint_positions_rad'] == {'j': .7}
    assert 'now_joint_positions_rad' not in obj['components'][1]['properties']


def test_summary_retains_failure_state_evidence():
    result = summary_view({'success': False, 'execution_started': False,
                           'state_capture_error': 'capture failed', 'steps': []})
    assert result['execution_started'] is False
    assert result['state_capture_error'] == 'capture failed'
    assert 'final_state_file' not in result


def test_summary_records_explicit_collision_representation():
    assert summary_view({'collision_model': 'precise'})['collision_model'] == 'precise'
