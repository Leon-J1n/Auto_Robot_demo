"""URDF loader for the common revolute ChainRobotModel; no IK dependency."""
from __future__ import annotations
from pathlib import Path
from typing import Mapping, Sequence
import numpy as np
from .model import ChainRobotModel, rigid_transform as _rigid_transform, joint_frame as _joint_frame


class UrdfRobotModel(ChainRobotModel):
    """Selected base-to-tip chain; current joint state is supplied by the caller.

    FK/Jacobian use URDF origins/axes and return world-frame quantities.
    Continuous joints require explicit finite representation windows because the
    existing service enumerates limit-valid periodic joint representations.
    """

    def __init__(self, path: str | Path, *, tip_link: str, base_link: str | None = None,
                 tcp_transform=None, world_from_base=None, initial_joints=None,
                 continuous_limits: Mapping[str, Sequence[float]] | None = None,
                 current_joints_provider=None, base_transform_provider=None):
        from urchin import URDF

        self.source_path = Path(path).expanduser().resolve()
        if not self.source_path.is_file():
            raise FileNotFoundError(f"URDF file not found: {self.source_path}")
        # Kinematics needs no visual/collision meshes or ROS package resolution.
        self._urdf = URDF.load(str(self.source_path), lazy_load_meshes=True)
        self.base_link = self._urdf.base_link.name if base_link is None else base_link
        self.tip_link = tip_link
        for name in (self.base_link, self.tip_link):
            if name not in self._urdf.link_map:
                raise ValueError(f"Unknown URDF link {name!r}; available: {', '.join(self._urdf.link_map)}")
        parents = {joint.child: joint for joint in self._urdf.joints}
        chain = []
        child = self.tip_link
        while child != self.base_link:
            if child not in parents:
                raise ValueError(f"tip_link {self.tip_link!r} is not a descendant of base_link {self.base_link!r}")
            joint = parents[child]
            chain.append(joint)
            child = joint.parent
        self._chain = list(reversed(chain))
        self._active = []
        for joint in self._chain:
            _rigid_transform(joint.origin, f"origin of joint {joint.name!r}")
            if joint.joint_type == "fixed":
                continue
            if joint.joint_type not in {"revolute", "continuous"}:
                raise ValueError(f"Chain joint {joint.name!r} has unsupported type {joint.joint_type!r}; select a revolute chain")
            if joint.mimic is not None:
                raise ValueError(f"Mimic joint {joint.name!r} is inside the selected chain; coupled joints are not supported")
            self._active.append(joint)
        if not self._active:
            raise ValueError("Selected base-to-tip chain contains no revolute joints")
        self.joint_names = tuple(j.name for j in self._active)
        overrides = dict(continuous_limits or {})
        allowed = {j.name for j in self._active if j.joint_type == "continuous"}
        if set(overrides) - allowed:
            raise ValueError("continuous_limits keys must name continuous joints in the selected chain")
        limits = []
        for joint in self._active:
            if joint.joint_type == "continuous":
                if joint.name not in overrides:
                    raise ValueError(f"Continuous joint {joint.name!r} needs a finite continuous_limits angle window, e.g. (-2*np.pi, 2*np.pi)")
                limit = np.asarray(overrides[joint.name], dtype=float)
            else:
                if joint.limit is None or joint.limit.lower is None or joint.limit.upper is None:
                    raise ValueError(f"Revolute joint {joint.name!r} needs lower/upper limits")
                limit = np.asarray([joint.limit.lower, joint.limit.upper], dtype=float)
            if limit.shape != (2,) or not np.all(np.isfinite(limit)) or limit[0] > limit[1]:
                raise ValueError(f"Invalid finite joint limits for {joint.name!r}")
            limits.append(limit)
        self.limits = np.asarray(limits)
        self.joint_periods = np.full(len(limits), 2*np.pi)
        self._tcp = _rigid_transform(tcp_transform, "tcp_transform (tip_from_tcp)")
        self._world_from_base = _rigid_transform(world_from_base, "world_from_base")
        tip = np.eye(4)
        axes, anchors = [], []
        for joint in self._chain:
            tip = tip @ joint.origin
            if joint.joint_type != "fixed":
                axis = np.asarray(joint.axis, dtype=float)
                if not np.all(np.isfinite(axis)) or np.linalg.norm(axis) < 1e-12:
                    raise ValueError(f"Invalid joint axis for {joint.name!r}")
                axes.append(tip[:3, :3] @ (axis/np.linalg.norm(axis)))
                anchors.append(tip[:3, 3].copy())
        tip = tip @ self._tcp
        axes, anchors = np.asarray(axes), np.asarray(anchors)
        offsets = np.vstack((anchors[0], np.diff(anchors, axis=0), tip[:3, 3]-anchors[-1]))
        parameters = {
            "schema_version": 1, "source_urdf": str(self.source_path),
            "units": {"length": "m", "angle": "rad"},
            "coordinate_frame": "robot_base_at_q_zero",
            "base_link": self.base_link, "tip_link": self.tip_link,
            "joint_names": list(self.joint_names), "joint_limits": self.limits.tolist(),
            "H_rows": axes.tolist(), "P_rows": offsets.tolist(),
            "R_zero": tip[:3, :3].tolist(),
            "joint_transforms_zero": np.stack([*(_joint_frame(h, p) for h, p in zip(axes, anchors)), tip]).tolist(),
            "tip_from_tcp": self._tcp.tolist(),
            "world_from_base_zero": self._world_from_base.tolist(),
        }
        super().__init__(parameters, initial_joints=initial_joints,
                         current_joints_provider=current_joints_provider,
                         base_transform_provider=base_transform_provider)


