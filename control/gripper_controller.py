"""MuJoCo adapter for the Robotiq 2F-85 gripper."""

from __future__ import annotations

import time
from typing import Any

import mujoco
import numpy as np


class MujocoGripperController:
    """Control and inspect the scene's Robotiq open/close actuator."""

    actuator_name = "robotiq_2f85_fingers_actuator"
    driver_joint_names = (
        "robotiq_2f85_left_driver_joint",
        "robotiq_2f85_right_driver_joint",
    )
    maximum_width = 0.085
    maximum_force = 40.0

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.model = runtime.model
        self.data = runtime.data
        self.actuator_id = self._id(mujoco.mjtObj.mjOBJ_ACTUATOR, self.actuator_name)
        self.driver_joint_ids = np.asarray(
            [self._id(mujoco.mjtObj.mjOBJ_JOINT, name) for name in self.driver_joint_names],
            dtype=int,
        )
        self.driver_qpos = self.model.jnt_qposadr[self.driver_joint_ids].astype(int)
        self.pad_body_ids = {
            self._id(mujoco.mjtObj.mjOBJ_BODY, f"robotiq_2f85_{side}_pad")
            for side in ("left", "right")
        }
        self._holding = False
        self._commanded_force = 0.0
        self._held_body_id: int | None = None
        self._hold_arm_pose()

    def _id(self, object_type: mujoco.mjtObj, name: str) -> int:
        object_id = mujoco.mj_name2id(self.model, object_type, name)
        if object_id < 0:
            raise ValueError(f"MuJoCo gripper object not found: {name}")
        return object_id

    def open(self, width: float, *, speed: float = 0.5, timeout: float = 5.0) -> dict[str, Any]:
        self._holding = False
        self._commanded_force = 0.0
        self._held_body_id = None
        return self._move(width, speed=speed, timeout=timeout, stop_on_contact=False)

    def close(
        self,
        width: float,
        *,
        speed: float = 0.5,
        force: float = 20.0,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        self._commanded_force = float(force)
        result = self._move(width, speed=speed, timeout=timeout, stop_on_contact=True)
        contacts = self._bilateral_contacts()
        self._held_body_id = min(contacts, default=None)
        self._holding = self._held_body_id is not None
        result["force"] = self._commanded_force if result["contact_detected"] else 0.0
        return result

    def hold(self, force: float) -> None:
        self._holding = True
        self._commanded_force = float(force)

    def release(self, *, width: float | None = None, timeout: float = 5.0) -> dict[str, Any]:
        return self.open(self.maximum_width if width is None else width, timeout=timeout)

    def get_state(self) -> dict[str, Any]:
        contacts = self._bilateral_contacts()
        held = self._held_body_id is not None and self._held_body_id in contacts
        return {
            "known": True,
            "initialized": True,
            "width": self.width,
            "force": self._commanded_force if held else 0.0,
            "contact_detected": held,
            "object_present": contact or held,
            "holding": self._holding and held,
            "slip_detected": False,
        }

    @property
    def width(self) -> float:
        angle = float(np.mean(self.data.qpos[self.driver_qpos]))
        return float(np.clip(self.maximum_width * (1.0 - angle / 0.8), 0.0, self.maximum_width))

    def _move(
        self,
        target_width: float,
        *,
        speed: float,
        timeout: float,
        stop_on_contact: bool,
    ) -> dict[str, Any]:
        target_width = float(np.clip(target_width, 0.0, self.maximum_width))
        final_ctrl = 255.0 * (1.0 - target_width / self.maximum_width)
        start_ctrl = float(self.data.ctrl[self.actuator_id])
        locked_arm_qpos = self.data.qpos[self.runtime.qpos_indices].copy()
        duration = max(abs(final_ctrl - start_ctrl) / 255.0 / max(float(speed), 0.05), 0.05)
        start = time.perf_counter()
        contact = False
        while True:
            elapsed = time.perf_counter() - start
            alpha = min(elapsed / duration, 1.0)
            self.data.ctrl[self.actuator_id] = start_ctrl + (final_ctrl - start_ctrl) * alpha
            self._hold_arm_pose()
            for _ in range(5):
                mujoco.mj_step(self.model, self.data)
                self.data.qpos[self.runtime.qpos_indices] = locked_arm_qpos
                self.data.qvel[self.runtime.dof_indices] = 0.0
                mujoco.mj_forward(self.model, self.data)
            contacts = self._bilateral_contacts()
            contact = bool(contacts)
            if self.runtime.viewer is not None:
                self.runtime.viewer.sync()
            if stop_on_contact and contact:
                break
            if alpha >= 1.0:
                break
            if elapsed >= timeout:
                raise TimeoutError("Gripper motion timed out.")
            if self.runtime.realtime:
                time.sleep(self.model.opt.timestep * 5)
        return {
            "target_width": target_width,
            "final_width": self.width,
            "contact_detected": contact,
            "object_present": contact,
        }

    def _hold_arm_pose(self) -> None:
        actuator_ids = getattr(self.runtime, "actuator_ids", None)
        qpos_indices = getattr(self.runtime, "qpos_indices", None)
        if actuator_ids is not None and qpos_indices is not None:
            self.data.ctrl[actuator_ids] = self.data.qpos[qpos_indices]


    def _bilateral_contacts(self) -> set[int]:
        sides: dict[int, set[int]] = {}
        for index in range(self.data.ncon):
            contact = self.data.contact[index]
            body1 = int(self.model.geom_bodyid[contact.geom1])
            body2 = int(self.model.geom_bodyid[contact.geom2])
            if body1 in self.pad_body_ids:
                sides.setdefault(body2, set()).add(body1)
            if body2 in self.pad_body_ids:
                sides.setdefault(body1, set()).add(body2)
        return {body for body, pads in sides.items()
                if pads == self.pad_body_ids and self.model.body_jntnum[body] > 0}
