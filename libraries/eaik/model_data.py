"""Compatibility exports for the original EAIK geometry checker."""
from __future__ import annotations
import numpy as np
from libraries.kinematic_chain.model import joint_frame as _joint_frame
from libraries.kinematic_chain.mujoco import extract_chain_parameters as extract_model_data


def parameter_forward(parameters, joints):
    """Independent Rodrigues-chain FK for checking the exported geometry."""
    axes = np.asarray(parameters["H_rows"])
    offsets = np.asarray(parameters["P_rows"])
    joints = np.asarray(joints, dtype=float)
    if joints.shape != (len(axes),):
        raise ValueError("One joint angle per axis is required")
    rotation = np.eye(3)
    position = offsets[0].copy()
    for axis, offset, angle in zip(axes, offsets[1:], joints):
        x, y, z = axis
        skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
        rotation = rotation @ (np.eye(3) + np.sin(angle)*skew + (1-np.cos(angle))*(skew @ skew))
        position += rotation @ offset
    result = np.eye(4)
    result[:3, 3] = position
    result[:3, :3] = rotation @ np.asarray(parameters["R_zero"])
    return result
