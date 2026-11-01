"""UR5e IKFast plugin, including generated-model/tool-frame calibration."""
from __future__ import annotations
import numpy as np
from kinematics.contracts import IKError, IKResult
from kinematics.geometry import transform
from . import ur5e_backend


class IKFastBackend:
    def __init__(self, robot):
        self.robot = robot
        self._tip_offset = None

    def supports(self, robot):
        return len(robot.limits) == 6 and np.allclose(robot.joint_periods, 2*np.pi)

    def _initialize_frames(self):
        zeros = np.zeros(6)
        pose = self.robot.forward(zeros)
        base = self.robot.base_transform(zeros)
        offset = np.linalg.inv(transform(*ur5e_backend.forward(zeros))) @ np.linalg.inv(base) @ transform(pose["position"], pose["rotation_matrix"])
        for q in ([.2, -.7, 1, -.4, .5, -.2], [-.5, -1, 1.2, -2, -1, .7]):
            pose = self.robot.forward(q)
            predicted = self.robot.base_transform(q) @ transform(*ur5e_backend.forward(q)) @ offset
            if not np.allclose(predicted, transform(pose["position"], pose["rotation_matrix"]), atol=1e-7, rtol=0):
                raise IKError("Compiled IKFast model does not match the scene robot/tool chain")
        self._tip_offset = offset

    def solve(self, request):
        if self._tip_offset is None:
            self._initialize_frames()
        if request.remaining_time <= 0:
            raise IKError("IKFast initialization exhausted the IK budget")
        target = np.linalg.inv(self.robot.base_transform(request.seed)) @ transform(request.position, request.rotation) @ np.linalg.inv(self._tip_offset)
        return IKResult(solutions=[np.asarray(q, dtype=float) for q in ur5e_backend.solve(target[:3, 3], target[:3, :3])],
                        backend="native_ur5e_ikfast", position_tolerance=1e-6,
                        orientation_tolerance=2e-6)


def create_backend(robot):
    return IKFastBackend(robot)
