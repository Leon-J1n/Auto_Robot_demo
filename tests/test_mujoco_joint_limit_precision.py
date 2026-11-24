import xml.etree.ElementTree as ET
from types import SimpleNamespace

import mujoco
import numpy as np
import pytest

from collision.proxy import PlanningProxy
from execution.backends.mujoco.scene_compiler import urdf_mjcf
from planning.common.errors import PlanningError


def test_urdf_limit_precision_survives_export_and_proxy_validation(tmp_path):
    path = tmp_path / 'robot.urdf'
    path.write_text('''<robot name="precision"><link name="base"/><link name="tip">
      <inertial><mass value="1"/><inertia ixx=".01" iyy=".01" izz=".01" ixy="0" ixz="0" iyz="0"/></inertial>
      </link><joint name="joint5" type="revolute"><parent link="base"/><child link="tip"/>
      <axis xyz="0 1 0"/><limit lower="-1.2217304" upper="1.2217304" effort="100" velocity="5"/>
      </joint></robot>''')
    tree = urdf_mjcf(path, tmp_path, 'precision')
    model = mujoco.MjModel.from_xml_string(ET.tostring(tree, encoding='unicode'))
    limits = model.jnt_range.copy()
    np.testing.assert_array_equal(limits, [[-1.2217304, 1.2217304]])
    proxy = SimpleNamespace(checker=SimpleNamespace(backend=SimpleNamespace(
        qpos_indices=[0], joint_limits=limits, joint_names=['joint5'])))
    for value in [-1.2217304, 1.2217304]:
        points = np.array([[value]])
        np.testing.assert_array_equal(PlanningProxy._validate(proxy, points, 1., .003), points)
    with pytest.raises(PlanningError):
        PlanningProxy._validate(proxy, np.array([[1.2217314]]), 1., .003)
