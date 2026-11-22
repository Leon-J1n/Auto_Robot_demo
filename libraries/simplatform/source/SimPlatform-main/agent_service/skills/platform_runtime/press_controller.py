"""Bounded local Cartesian press controller for the RobotSimPlatform runtime.

Replaces the MuJoCo press controller. The motion is identical: small steps along a
fixed world axis, each solved by local IK from the previous configuration, with a
retract path back to the start pose.

Two capabilities the MuJoCo version had are genuinely absent here, and are reported
rather than faked:

  * Contact force. The platform exposes no contact wrench, and no penetration depth
    to estimate one from -- reported distances are clamped at zero. So `normal_force`
    is always 0.0, `force_feedback_available` is False, and `execute_force` raises
    SENSOR_DATA_UNAVAILABLE instead of pretending. Displacement press is unaffected:
    it gates on geometric contact, which the platform answers exactly. One
    consequence worth knowing is that `contact_threshold` and `maximum_force` cannot
    bite, because the value they compare against never rises above zero.

  * Actuated press targets. Driving a button's own slide joint needs scene-object
    joints, which the platform's entity model does not have. A press therefore acts
    against a rigid target; travel past contact presses into it rather than moving
    it. Nothing silently degrades -- there is simply no movable target to find.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Sequence

import numpy as np

from . import bootstrap  # noqa: F401  (puts the SDK and project root on sys.path)
from .kinematics import IKError, PlatformKinematics
from .transforms import quaternion_mapping, serializable_pose

from skills.manipulation.press.utils.error_codes import ErrorCode, PressExecutionError


class PlatformPressController:
    """Execute short fixed-axis presses without invoking global planning."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.robot = runtime.robot
        self.scene = runtime.scene
        self.kinematics = PlatformKinematics(runtime)
        self.ready = True
        self.force_feedback_available = False
        self.force_sensor_calibrated = False
        self._baseline_self_pairs = self._current_self_contact_pairs()

    def get_state(self, target_object_id: str | None = None) -> dict[str, Any]:
        force, contact, contacts = self._target_contact(target_object_id)
        return {
            "ready": self.ready,
            "enabled": True,
            "fault": False,
            "force_feedback_available": self.force_feedback_available,
            "force_sensor_calibrated": self.force_sensor_calibrated,
            "normal_force": force,
            "contact_detected": contact,
            "contacts": contacts,
        }

    def execute_displacement(self, *, direction: Sequence[float],
                             target_object_id: str | None, press_depth: float | None,
                             travel_distance: float | None, stop_on_contact: bool,
                             post_contact_depth: float, maximum_force: float,
                             maximum_travel: float, contact_threshold: float,
                             speed: float, hold_time: float, timeout: float,
                             retract: bool, retract_speed: float,
                             emergency_retract: bool) -> dict[str, Any]:
        desired_total = (float(travel_distance) if travel_distance is not None
                         else float(maximum_travel))
        desired_post_contact = (float(press_depth) if press_depth is not None
                                else float(post_contact_depth))
        return self._execute_profile(
            direction=np.asarray(direction, dtype=float),
            target_object_id=target_object_id,
            maximum_force=maximum_force, maximum_travel=maximum_travel,
            contact_threshold=contact_threshold, speed=speed, hold_time=hold_time,
            timeout=timeout, retract=retract, retract_speed=retract_speed,
            emergency_retract=emergency_retract, target_force=None,
            force_tolerance=0.0, desired_total=desired_total,
            desired_post_contact=desired_post_contact,
            stop_on_contact=stop_on_contact,
            travel_distance_mode=travel_distance is not None,
        )

    def execute_force(self, *, direction: Sequence[float],
                      target_object_id: str | None, target_force: float,
                      maximum_force: float, maximum_travel: float,
                      contact_threshold: float, force_tolerance: float, speed: float,
                      hold_time: float, timeout: float, retract: bool,
                      retract_speed: float,
                      emergency_retract: bool) -> dict[str, Any]:
        if not self.force_feedback_available or not self.force_sensor_calibrated:
            raise PressExecutionError(
                "Calibrated force feedback is unavailable.",
                error_code=ErrorCode.SENSOR_DATA_UNAVAILABLE,
                failed_stage="preconditions", recoverable=True,
                recommended_action="Use displacement press, which gates on geometric "
                                   "contact, or add a contact-force readout to the "
                                   "simulation platform.",
            )
        return self._execute_profile(
            direction=np.asarray(direction, dtype=float),
            target_object_id=target_object_id,
            maximum_force=maximum_force, maximum_travel=maximum_travel,
            contact_threshold=contact_threshold, speed=speed, hold_time=hold_time,
            timeout=timeout, retract=retract, retract_speed=retract_speed,
            emergency_retract=emergency_retract, target_force=target_force,
            force_tolerance=force_tolerance, desired_total=maximum_travel,
            desired_post_contact=0.0, stop_on_contact=False,
            travel_distance_mode=False,
        )

    def verification_snapshot(self, target_object_id: str | None, *,
                              strategy: str) -> dict[str, Any]:
        force, contact, contacts = self._target_contact(target_object_id)
        pose = serializable_pose(self.runtime.get_end_effector_pose())
        return {
            "contact_detected": contact,
            "target_force_reached": strategy == "force" and contact,
            "target_depth_reached": strategy == "displacement" and contact,
            "peak_force": force, "final_force": force, "travel_distance": 0.0,
            "hold_time": 0.0, "target_actuated": contact, "retracted": True,
            "final_pose": pose, "force_profile": [], "execution_time": 0.0,
            "evidence": {"verification_snapshot": True, "contacts": contacts},
        }

    def _execute_profile(self, **options: Any) -> dict[str, Any]:
        direction = options["direction"] / np.linalg.norm(options["direction"])
        start_q = self.runtime.get_joint_positions()
        start_pose = self.runtime.get_end_effector_pose()
        start_position = np.asarray(start_pose["position"], dtype=float)
        orientation = quaternion_mapping(start_pose["quaternion_wxyz"])
        step = min(0.001, max(float(options["speed"]) * 0.05, 0.0002))
        maximum_travel = float(options["maximum_travel"])
        started = time.perf_counter()
        samples: list[dict[str, Any]] = []
        contact_travel: float | None = None
        peak_force = 0.0
        final_q = start_q.copy()
        reached = False
        failure: PressExecutionError | None = None
        travel = 0.0
        try:
            while travel < maximum_travel - 1e-9:
                if time.perf_counter() - started > float(options["timeout"]):
                    raise PressExecutionError(
                        "Press execution timed out.", error_code=ErrorCode.PRESS_TIMEOUT,
                        failed_stage="execution", recoverable=True,
                        recommended_action="Check the pre-press pose and increase "
                                           "timeout only if safe.")
                travel = min(travel + step, maximum_travel)
                target_position = start_position + direction * travel
                try:
                    solved = self.kinematics.solve(
                        {"position": target_position, "orientation": orientation},
                        seed=final_q, method="trac_ik", timeout=0.25)
                except IKError as exc:
                    raise PressExecutionError(
                        f"Local press IK failed at travel {travel:.6f} m: {exc}",
                        error_code=ErrorCode.PRESS_EXECUTION_ERROR,
                        failed_stage="local_motion", recoverable=True,
                        recommended_action="Move to a better pre-press pose without "
                                           "changing the requested press axis.") from exc
                next_q = np.asarray(solved["solution"], dtype=float)
                self._set_joints(next_q)
                final_q = next_q
                # One query answers both questions, so the pose is only probed once.
                force, contacts, foreign = self._scan(options["target_object_id"])
                # Contact is geometric here. Gating it on contact_threshold, as the
                # MuJoCo controller did, would never fire without a force readout.
                contact = bool(contacts)
                peak_force = max(peak_force, force)
                if contact and contact_travel is None:
                    contact_travel = travel
                if foreign:
                    raise PressExecutionError(
                        "Unexpected local collision during press.",
                        error_code=ErrorCode.LOCAL_COLLISION_DETECTED,
                        failed_stage="execution", recoverable=True,
                        recommended_action="Correct the pre-press pose or local scene; "
                                           "Press Skill will not change the press "
                                           "direction.",
                        details={"contacts": foreign})
                samples.append({"travel": travel, "force": force, "contact": contact,
                                "contacts": contacts})
                if force > float(options["maximum_force"]) + 1e-9:
                    raise PressExecutionError(
                        "Maximum press force exceeded.",
                        error_code=ErrorCode.FORCE_LIMIT_EXCEEDED,
                        failed_stage="execution", recoverable=False,
                        recommended_action="Inspect the target, press direction, and "
                                           "force limit before retrying.",
                        details={"peak_force": force,
                                 "maximum_force": options["maximum_force"]})
                if options["target_force"] is not None:
                    if force >= float(options["target_force"]) - float(
                            options["force_tolerance"]):
                        reached = True
                        break
                elif options["travel_distance_mode"]:
                    if travel >= float(options["desired_total"]) - 1e-9:
                        reached = True
                        break
                elif contact_travel is not None:
                    post_contact = travel - contact_travel
                    if (options["stop_on_contact"]
                            or post_contact >= float(options["desired_post_contact"])
                            - 1e-9):
                        reached = True
                        break
            if float(options["hold_time"]) > 0 and self.runtime.realtime:
                time.sleep(float(options["hold_time"]))
        except PressExecutionError as exc:
            failure = exc
        retracted = False
        if options["retract"] or (failure is not None and options["emergency_retract"]):
            retracted = self._retract(start_q, float(options["retract_speed"]),
                                      started, float(options["timeout"]))
        if failure is not None:
            failure.retracted = retracted
            raise failure
        final_force = samples[-1]["force"] if samples else 0.0
        contact_detected = contact_travel is not None
        return {
            "contact_detected": contact_detected,
            "target_force_reached": bool(options["target_force"] is not None and reached),
            "target_depth_reached": bool(options["target_force"] is None and reached),
            "peak_force": peak_force,
            "final_force": final_force,
            "travel_distance": travel,
            "contact_travel": contact_travel,
            "hold_time": float(options["hold_time"]),
            "target_actuated": reached,
            "retracted": retracted,
            "final_pose": serializable_pose(self.runtime.get_end_effector_pose()),
            "force_profile": samples,
            "execution_time": time.perf_counter() - started,
            "evidence": {
                "sample_count": len(samples),
                "samples": self._decimate(samples, 25),
                "target_object_id": options["target_object_id"],
                "force_feedback_available": self.force_feedback_available,
            },
        }

    def _set_joints(self, joints: np.ndarray) -> None:
        self.runtime.set_joint_positions(joints)
        if self.runtime.viewer is not None:
            self.runtime.viewer.sync()

    def _retract(self, start_q: np.ndarray, speed: float, started: float,
                 timeout: float) -> bool:
        current = self.runtime.get_joint_positions()
        count = max(2, int(np.ceil(np.max(np.abs(current - start_q))
                                   / max(speed * 0.05, 0.01))) + 1)
        for alpha in np.linspace(0.0, 1.0, count)[1:]:
            if time.perf_counter() - started > timeout:
                return False
            self._set_joints(current + alpha * (start_q - current))
        return True

    # ------------------------------------------------------------------ contacts

    def _scan(self, target_object_id: str | None,
              ) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
        """Classify every contact at the live pose into target-side and foreign.

        The platform only reports pairs with at least one robot collider, so the
        robot-involvement filtering the MuJoCo version needed is implicit.
        """
        entity = self.scene.object_entity(target_object_id) if target_object_id else None
        if target_object_id and entity is None:
            return 0.0, [], []
        target = set(self.scene.colliders_of(entity)) if entity is not None else set()
        contacts: list[dict[str, Any]] = []
        foreign: list[dict[str, Any]] = []
        peak = 0.0
        for pair in self.robot.configuration_contacts(
                self.runtime.full_joint_positions(), limits="ignore"):
            sides = (pair.robot_collider, pair.other)
            distance = float(pair.distance)
            if target and (sides[0] in target or sides[1] in target):
                # No contact wrench and no penetration depth exist to derive a force
                # from, so this stays zero. See the module docstring.
                force = 0.0
                peak = max(peak, force)
                if pair.colliding:
                    contacts.append({"geom1": self.scene.name_of(sides[0]),
                                     "geom2": self.scene.name_of(sides[1]),
                                     "distance": distance, "normal_force": force})
                continue
            if not target and pair.colliding:
                contacts.append({"geom1": self.scene.name_of(sides[0]),
                                 "geom2": self.scene.name_of(sides[1]),
                                 "distance": distance, "normal_force": 0.0})
                continue
            if not pair.colliding:
                continue
            if (self.scene.is_robot(sides[1])
                    and frozenset(sides) in self._baseline_self_pairs):
                continue
            foreign.append({"geom1": self.scene.name_of(sides[0]),
                            "geom2": self.scene.name_of(sides[1]),
                            "distance": distance})
        return peak, contacts, foreign

    def _target_contact(self, target_object_id: str | None,
                        ) -> tuple[float, bool, list[dict[str, Any]]]:
        force, contacts, _ = self._scan(target_object_id)
        return force, bool(contacts), contacts

    def _foreign_contacts(self, target_object_id: str | None) -> list[dict[str, Any]]:
        return self._scan(target_object_id)[2]

    def _current_self_contact_pairs(self) -> set[frozenset[int]]:
        """Robot-against-robot pairs already present, which are not press failures."""
        return {frozenset((pair.robot_collider, pair.other))
                for pair in self.robot.configuration_contacts(
                    self.runtime.full_joint_positions(), limits="ignore")
                if pair.colliding and self.scene.is_robot(pair.other)}

    @staticmethod
    def _decimate(samples: list[dict[str, Any]],
                  maximum: int) -> list[dict[str, Any]]:
        if len(samples) <= maximum:
            return samples
        indices = np.unique(np.linspace(0, len(samples) - 1, maximum, dtype=int))
        return [samples[int(index)] for index in indices]
