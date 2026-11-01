"""NumPy FK/geometric Jacobian for a serial revolute chain in H/P form."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import numpy as np


def rigid_transform(value, name):
    result = np.eye(4) if value is None else np.asarray(value, dtype=float).copy()
    if (result.shape != (4, 4) or not np.all(np.isfinite(result))
            or not np.allclose(result[3], [0, 0, 0, 1], atol=1e-9, rtol=0)
            or not np.allclose(result[:3, :3].T @ result[:3, :3], np.eye(3), atol=1e-9, rtol=0)
            or not np.isclose(np.linalg.det(result[:3, :3]), 1, atol=1e-9)):
        raise ValueError(f"{name} must be a finite rigid 4x4 transform")
    return result


def axis_rotation(axis, angle):
    x, y, z = axis
    skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return np.eye(3) + np.sin(angle)*skew + (1-np.cos(angle))*(skew @ skew)


def joint_frame(axis, anchor):
    reference = np.eye(3)[np.argmin(np.abs(axis))]
    x = np.cross(reference, axis)
    x /= np.linalg.norm(x)
    result = np.eye(4)
    result[:3, :3] = np.column_stack((x, np.cross(axis, x), axis))
    result[:3, 3] = anchor
    return result


class ChainRobotModel:
    """Rigid serial revolute geometry, separate from runtime state and IK.

    H/P rows describe q=0 axes/offsets in one base frame. Providers only read
    current state/base pose; FK/Jacobian never invoke a simulator or modify it.
    """
    def __init__(self, parameters, *, current_joints_provider=None,
                 base_transform_provider=None, initial_joints=None,
                 world_from_base=None):
        spec = deepcopy(parameters)
        if spec.get("schema_version", 1) != 1:
            raise ValueError("Unsupported chain parameter schema_version")
        if spec.get("units", {"length": "m", "angle": "rad"}) != {"length": "m", "angle": "rad"}:
            raise ValueError("Chain parameters require metre/radian units")
        if spec.get("coordinate_frame", "robot_base_at_q_zero") != "robot_base_at_q_zero":
            raise ValueError("Chain axes and offsets must be expressed in the base frame at q=0")
        self._axes = np.asarray(spec["H_rows"], dtype=float).copy()
        if (self._axes.ndim != 2 or self._axes.shape[1] != 3 or not len(self._axes)
                or not np.all(np.isfinite(self._axes))
                or not np.allclose(np.linalg.norm(self._axes, axis=1), 1, atol=1e-9, rtol=0)):
            raise ValueError("H_rows must contain one unit axis per revolute joint")
        self._axes /= np.linalg.norm(self._axes, axis=1)[:, None]
        count = len(self._axes)
        self._offsets = np.asarray(spec["P_rows"], dtype=float).copy()
        if self._offsets.shape != (count+1, 3) or not np.all(np.isfinite(self._offsets)):
            raise ValueError("P_rows must contain N+1 finite three-dimensional offsets")
        zero = np.eye(4)
        rotation = np.asarray(spec["R_zero"], dtype=float)
        if rotation.shape != (3, 3):
            raise ValueError("R_zero must be a 3x3 rotation")
        zero[:3, :3] = rotation
        self._tip_rotation = rigid_transform(zero, "R_zero")[:3, :3]
        self.joint_names = tuple(spec["joint_names"])
        if (len(self.joint_names) != count or len(set(self.joint_names)) != count
                or any(not isinstance(name, str) or not name for name in self.joint_names)):
            raise ValueError("joint_names must contain N unique names")
        self.limits = np.asarray(spec["joint_limits"], dtype=float).copy()
        if (self.limits.shape != (count, 2) or not np.all(np.isfinite(self.limits))
                or np.any(self.limits[:, 0] > self.limits[:, 1])):
            raise ValueError("joint_limits must contain N finite ordered bounds")
        self.joint_periods = np.asarray(spec.get("joint_periods", [2*np.pi]*count), dtype=float).copy()
        if (self.joint_periods.shape != (count,) or not np.all(np.isfinite(self.joint_periods))
                or not np.allclose(self.joint_periods, 2*np.pi, atol=1e-9, rtol=0)):
            raise ValueError("ChainRobotModel currently supports revolute coordinates with period 2*pi")
        self._world_from_base = rigid_transform(
            spec.get("world_from_base_zero") if world_from_base is None else world_from_base, "world_from_base")
        self._current_provider = current_joints_provider
        self._base_provider = base_transform_provider
        self._current = np.clip(np.zeros(count), self.limits[:, 0], self.limits[:, 1])
        if initial_joints is not None:
            self.set_current_joints(initial_joints)
        anchors = np.cumsum(self._offsets[:-1], axis=0)
        zero[:3, 3] = np.sum(self._offsets, axis=0)
        frames = np.stack([*(joint_frame(h, p) for h, p in zip(self._axes, anchors)), zero])
        if "joint_transforms_zero" in spec:
            provided = np.asarray(spec["joint_transforms_zero"], dtype=float)
            if provided.shape != frames.shape:
                raise ValueError("joint_transforms_zero must contain N+1 rigid 4x4 matrices")
            for index, matrix in enumerate(provided):
                rigid_transform(matrix, "joint_transforms_zero")
                if index < count:
                    matches = np.allclose(matrix[:3, 2], self._axes[index], atol=1e-9, rtol=0) and np.allclose(matrix[:3, 3], anchors[index], atol=1e-9, rtol=0)
                else:
                    matches = np.allclose(matrix, zero, atol=1e-9, rtol=0)
                if not matches:
                    raise ValueError("Zero joint transforms disagree with H/P/TCP geometry")
        spec.update(H_rows=self._axes.tolist(), P_rows=self._offsets.tolist(),
                    R_zero=self._tip_rotation.tolist(), joint_periods=self.joint_periods.tolist(),
                    joint_transforms_zero=frames.tolist(), world_from_base_zero=self._world_from_base.tolist())
        self._parameters = spec
        self._cached_chain = None

    @classmethod
    def from_json(cls, path, **kwargs):
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return cls(value.get("parameters", value), **kwargs)

    @property
    def chain_parameters(self):
        return deepcopy(self._parameters)

    @property
    def eaik_parameters(self):
        """Compatibility for the original EAIK JSON export entry."""
        return self.chain_parameters

    def _joints(self, joints):
        q = np.asarray(joints, dtype=float)
        if q.shape != (len(self.joint_names),) or not np.all(np.isfinite(q)):
            raise ValueError("One finite angle per selected joint is required, in joint_names order")
        return q

    def current_joints(self):
        value = self._current if self._current_provider is None else self._current_provider()
        return self._joints(value).copy()

    def set_current_joints(self, joints):
        if self._current_provider is not None:
            raise ValueError("Current joints are supplied by the external state provider")
        q = self._joints(joints)
        if np.any(q < self.limits[:, 0]) or np.any(q > self.limits[:, 1]):
            raise ValueError("Current joint state is outside the configured joint limits")
        self._current = q.copy()

    def base_transform(self, joints):
        q = self._joints(joints)
        if self._base_provider is None:
            return self._world_from_base.copy()
        return rigid_transform(self._base_provider(q.copy()), "base_transform_provider result")

    def _chain_pose(self, joints):
        q = self._joints(joints)
        if self._cached_chain is not None and np.array_equal(q, self._cached_chain[0]):
            return self._cached_chain[1]
        rotation = np.eye(3)
        position = self._offsets[0].copy()
        axes, anchors = [], []
        for axis, offset, angle in zip(self._axes, self._offsets[1:], q):
            axes.append(rotation @ axis)
            anchors.append(position.copy())
            rotation = rotation @ axis_rotation(axis, angle)
            position += rotation @ offset
        tip = np.eye(4)
        tip[:3, :3] = rotation @ self._tip_rotation
        tip[:3, 3] = position
        result = (tip, np.asarray(axes), np.asarray(anchors))
        for value in result:
            value.setflags(write=False)
        self._cached_chain = (q.copy(), result)
        return result

    def forward(self, joints):
        tip = self.base_transform(joints) @ self._chain_pose(joints)[0]
        return {"position": tip[:3, 3].copy(), "rotation_matrix": tip[:3, :3].copy()}

    def jacobian(self, joints):
        tip, axes, anchors = self._chain_pose(joints)
        rotation = self.base_transform(joints)[:3, :3]
        linear = np.cross(axes, tip[:3, 3]-anchors) @ rotation.T
        angular = axes @ rotation.T
        return np.vstack((linear.T, angular.T))
