from types import SimpleNamespace

import numpy as np

from agent_service.libraries.ikfast.scene_target import solver_target
from agent_service.skills.platform_runtime.transforms import rpy_to_matrix


def test_world_tcp_target_is_converted_through_rotated_base_and_tool():
    rb = rpy_to_matrix(.2, -.3, .7)
    rw = rpy_to_matrix(-.5, .4, .1)
    offset_r = rpy_to_matrix(.1, .2, -.3)
    base = {"position": np.array([.3, -.2, .5]), "rotation_matrix": rb}
    wrist = {"position": np.array([.6, .1, 1.]), "rotation_matrix": rw}
    offset_p = np.array([.02, .01, .157])
    tcp = {"position": wrist["position"] + rw @ offset_p, "rotation_matrix": rw @ offset_r}
    runtime = SimpleNamespace(link_pose=lambda name: base if name == "base_link" else wrist,
                              get_end_effector_pose=lambda: tcp)
    target_p = np.array([.601709, .06735, .5289625])
    target_r = rpy_to_matrix(np.pi, -np.pi / 2, 0.)
    p, r = solver_target(runtime, target_p, target_r)
    np.testing.assert_allclose(base["position"] + rb @ p + rb @ r @ offset_p, target_p)
    np.testing.assert_allclose(rb @ r @ offset_r, target_r, atol=1e-12)
