"""MuJoCo RobotModel; solver algorithms live in libraries."""
from __future__ import annotations
import mujoco
import numpy as np


class MujocoRobotModel:
    def __init__(self, runtime):
        self.runtime = runtime
        self.model = runtime.model
        self.site_id = runtime.end_effector_site_id
        self.qpos_indices = runtime.qpos_indices
        self.dof_indices = runtime.dof_indices
        self.limits = np.asarray(runtime.joint_limits, dtype=float).copy()
        self.joint_periods = np.array([2*np.pi if self.model.jnt_type[j] == mujoco.mjtJoint.mjJNT_HINGE else 0
                                       for j in runtime.joint_ids])
        self._work_data = mujoco.MjData(self.model)
        self._base_id = int(self.model.body_parentid[self.model.jnt_bodyid[runtime.joint_ids[0]]])

    def current_joints(self):
        return np.asarray(self.runtime.get_joint_positions(), dtype=float).copy()

    def _set_joints(self, joints):
        values = np.asarray(joints, dtype=float)
        if values.shape != (len(self.limits),) or not np.all(np.isfinite(values)):
            raise ValueError("Joint vector must contain one finite value per robot joint")
        self._work_data.qpos[:] = self.runtime.data.qpos
        self._work_data.qpos[self.qpos_indices] = values
        mujoco.mj_forward(self.model, self._work_data)

    def forward(self, joints):
        self._set_joints(joints)
        return {"position": self._work_data.site_xpos[self.site_id].copy(),
                "rotation_matrix": self._work_data.site_xmat[self.site_id].reshape(3, 3).copy()}

    def jacobian(self, joints):
        self._set_joints(joints)
        jac_pos = np.zeros((3, self.model.nv))
        jac_rot = np.zeros((3, self.model.nv))
        mujoco.mj_jacSite(self.model, self._work_data, jac_pos, jac_rot, self.site_id)
        return np.vstack((jac_pos[:, self.dof_indices], jac_rot[:, self.dof_indices]))

    def base_transform(self, joints):
        self._set_joints(joints)
        result = np.eye(4)
        result[:3, 3] = self._work_data.xpos[self._base_id]
        result[:3, :3] = self._work_data.xmat[self._base_id].reshape(3, 3)
        return result
