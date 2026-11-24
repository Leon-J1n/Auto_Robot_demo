"""Contact surfaces must survive visual-derived gripper collision generation."""
from types import SimpleNamespace

import mujoco
import numpy as np
import pytest

from execution.backends.mujoco.gripper_controller import BindingGripperController


def controller(*, contacts=True, mesh=False):
    asset = '<asset><mesh name="pad_mesh" vertex="-.01 -.004 -.01 .01 -.004 -.01 -.01 .004 -.01 .01 .004 -.01 -.01 -.004 .01 .01 -.004 .01 -.01 .004 .01 .01 .004 .01"/></asset>' if mesh else ''
    shape = 'type="mesh" mesh="pad_mesh"' if mesh else 'type="box" size=".01 .004 .01"'
    pads = ''
    for side, y in [('left', -.05), ('right', .05)]:
        active = f'<geom {shape}/>' if contacts else ''
        pads += f'<body name="robotiq_2f85_{side}_pad" pos="0 {y} 0">{active}<geom {shape} contype="0" conaffinity="0"/><body name="robotiq_2f85_{side}_silicone_pad"/></body>'
    xml = f'<mujoco>{asset}<worldbody><body name="robotiq_2f85_base" euler="23 17 41"><joint name="robotiq_2f85_right_driver_joint"/><geom type="sphere" size=".001" mass="1"/>{pads}</body></worldbody><actuator><position name="grasp_servo" joint="robotiq_2f85_right_driver_joint"/></actuator></mujoco>'
    model = mujoco.MjModel.from_xml_string(xml)
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    return BindingGripperController(SimpleNamespace(model=model, data=data))


@pytest.mark.parametrize('mesh', [False, True])
def test_width_uses_facing_surfaces_in_rotated_frame(mesh):
    c = controller(mesh=mesh)
    assert c.width == pytest.approx(.092, abs=1e-7)
    assert all(len(geoms) == 1 for geoms in c.pad_geoms.values())
    assert np.isfinite(c.width)


def test_missing_contact_geometry_fails_with_actionable_error():
    with pytest.raises(ValueError, match='left pad has no active collision geometry'):
        controller(contacts=False)


def test_gripper_adapter_keeps_generated_contact_meshes(tmp_path):
    import xml.etree.ElementTree as ET
    from execution.backends.mujoco.gripper import add_gripper
    source = tmp_path / 'tool.urdf'
    source.write_text('<robot name="tool"/>')
    root = ET.fromstring('<mujoco><worldbody/><actuator/></mujoco>')
    for side in ('left', 'right'):
        for suffix in ('pad', 'silicone_pad'):
            body = ET.SubElement(root.find('worldbody'), 'body', name=f'robotiq_2f85_{side}_{suffix}')
            ET.SubElement(body, 'geom', type='mesh', mesh='visual', group='1', contype='0', conaffinity='0')
            ET.SubElement(body, 'geom', type='mesh', mesh='convex_part')
    resources = SimpleNamespace(robot_object={'components': [
        {'component_type': 'GripperModel', 'properties': {'urdf': str(source)}}]}, asset=lambda p: p)
    add_gripper(root, resources)
    excluded = {frozenset((e.get('body1'), e.get('body2')))
                for e in root.findall('./contact/exclude')}
    assert frozenset(('robotiq_2f85_left_spring_link',
                      'robotiq_2f85_right_spring_link')) in excluded
    assert all('pad' not in name for pair in excluded for name in pair)
    for body in root.findall('./worldbody/body'):
        visual, collision = body.findall('geom')
        assert visual.get('contype') == visual.get('conaffinity') == '0'
        assert collision.get('contype', '1') == collision.get('conaffinity', '1') == '1'
        assert collision.get('condim') == '4'
