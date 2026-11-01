"""One-time scene geometry extraction and runtime state binding."""
from __future__ import annotations

import numpy as np
from .model import ChainRobotModel, joint_frame


def extract_chain_parameters(runtime):
    import mujoco

    model = runtime.model
    joints = np.asarray(runtime.joint_ids, dtype=int)
    if not len(joints) or np.any(model.jnt_type[joints] != mujoco.mjtJoint.mjJNT_HINGE):
        raise ValueError("Chain preparation requires a non-empty revolute chain")
    base_id = int(model.body_parentid[model.jnt_bodyid[joints[0]]])
    body = int(model.site_bodyid[runtime.end_effector_site_id])
    chain = []
    while body != base_id:
        if body == 0:
            raise ValueError("The TCP does not descend from the selected robot base")
        chain[:0] = list(range(int(model.body_jntadr[body]),
                              int(model.body_jntadr[body] + model.body_jntnum[body])))
        body = int(model.body_parentid[body])
    if chain != joints.tolist():
        raise ValueError("The base-to-TCP chain contains missing, extra or reordered joints")

    data = mujoco.MjData(model)
    data.qpos[:] = runtime.data.qpos
    data.qpos[runtime.qpos_indices] = 0
    mujoco.mj_forward(model, data)
    base_rotation = data.xmat[base_id].reshape(3, 3)
    base_position = data.xpos[base_id]
    # Each row is expressed in the same base frame at q=0 (not a moving frame).
    axes = data.xaxis[joints] @ base_rotation
    anchors = (data.xanchor[joints] - base_position) @ base_rotation
    axes /= np.linalg.norm(axes, axis=1)[:, None]
    tip = np.eye(4)
    tip[:3, :3] = base_rotation.T @ data.site_xmat[runtime.end_effector_site_id].reshape(3, 3)
    tip[:3, 3] = base_rotation.T @ (data.site_xpos[runtime.end_effector_site_id] - base_position)
    offsets = np.vstack((anchors[0], np.diff(anchors, axis=0), tip[:3, 3] - anchors[-1]))
    frames = np.stack([*(joint_frame(h, p) for h, p in zip(axes, anchors)), tip])
    base = np.eye(4)
    base[:3, :3], base[:3, 3] = base_rotation, base_position

    def clean(array):
        array = np.asarray(array).copy()
        array[np.abs(array) < 1e-14] = 0
        return array.tolist()

    return {
        "schema_version": 1,
        "source_scene": str(runtime.scene_path),
        "units": {"length": "m", "angle": "rad"},
        "coordinate_frame": "robot_base_at_q_zero",
        "joint_names": list(runtime.joint_names),
        "joint_limits": clean(runtime.joint_limits),
        "base_body": mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, base_id),
        "tcp_site": runtime.end_effector_site,
        "H_rows": clean(axes),
        "P_rows": clean(offsets),
        "R_zero": clean(tip[:3, :3]),
        "joint_transforms_zero": clean(frames),
        "world_from_base_zero": clean(base),
    }


def create_scene_kinematics(runtime, registry=None):
    """Extract rigid geometry once; then bind NumPy calculations to live state."""
    from kinematics import KinematicsService

    parameters = extract_chain_parameters(runtime)
    base_id = int(runtime.model.body_parentid[runtime.model.jnt_bodyid[runtime.joint_ids[0]]])

    def live_base_transform(joints):
        # Base pose is independent of the selected arm angles. Scene updates
        # maintain these state arrays; this callback does not run mj_forward.
        result = np.eye(4)
        result[:3, :3] = runtime.data.xmat[base_id].reshape(3, 3)
        result[:3, 3] = runtime.data.xpos[base_id]
        return result

    robot = ChainRobotModel(parameters, current_joints_provider=runtime.get_joint_positions,
                            base_transform_provider=live_base_transform)
    return KinematicsService(robot, registry)


