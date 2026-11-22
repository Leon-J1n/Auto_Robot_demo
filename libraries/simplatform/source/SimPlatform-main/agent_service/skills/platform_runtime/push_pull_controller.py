"""Bounded push/pull controller for the RobotSimPlatform runtime.

Replaces the MuJoCo push/pull controller. What carries over and what does not follows
from one structural difference: the platform's entity model has no scene-object
joints. A MuJoCo scene expresses a cabinet door as a hinge and a drawer as a slide,
and the original controller animated those joints directly.

  * Free-object push works, and is the path this project's scene exercises. A free
    body is just an entity with a transform, which is exactly what the platform has.
    The arm follows the push line by local IK while the object is translated with it,
    and finger-pad contact is verified every frame.

  * Mechanism push/pull -- execute_circular on a hinge, execute_linear on a slide --
    raises PushPullUnsupported. There is no articulated scene object to drive and no
    handle site to grasp, so animating one would mean inventing state the scene does
    not contain. The methods are kept so the skill's strategy dispatch is unchanged
    and the failure names its own cause.

Two checks the MuJoCo version made are also unavailable, because the contact query
reports a colliding flag and a clamped distance but no contact normal, and only pairs
with at least one robot collider:

  * Support contacts are not observed. An object resting on a table is an
    object-vs-object pair, which the query never reports, so "lost its support
    surface mid-push" cannot be detected. `allowed_support_contact_pairs` is 0.
  * Intended pad contacts are identified by which colliders touch, not by comparing
    the physical contact normal against the requested one. Tool-axis alignment is
    still enforced against the request's tolerance, from the end-effector pose.
"""

from __future__ import annotations

import time
from typing import Any, Sequence

import numpy as np

from . import bootstrap  # noqa: F401  (puts the SDK and project root on sys.path)
from .kinematics import IKError, PlatformKinematics
from .transforms import rotation_mapping

from sim_platform import Transform


class PushPullUnsupported(RuntimeError):
    """Raised for a push/pull mode the platform's scene model cannot represent."""


class PlatformPushPullController:
    """Push a free body along a line while the arm maintains tool contact."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.robot = runtime.robot
        self.scene = runtime.scene
        self.kinematics = PlatformKinematics(runtime)

    def get_state(self, object_id: str, *,
                  interaction_mode: str = "mechanism") -> dict[str, Any]:
        entity = self.scene.object_entity(object_id)
        gripper = getattr(self.runtime, "gripper_controller", None)
        gripper_holding = bool(gripper
                              and gripper.get_state(object_id).get("holding"))
        gripper_holding_any = bool(gripper and getattr(gripper, "_holding", False)
                                   and gripper.held_entity is not None)
        if interaction_mode == "free_object":
            ready = entity is not None and not gripper_holding_any
        else:
            # No articulated scene objects exist on this platform.
            ready = False
        return {
            "ready": ready,
            "fault": False,
            "safe": True,
            "interaction_mode": interaction_mode,
            "gripper_holding": gripper_holding,
            "gripper_holding_any": gripper_holding_any,
            "joint_id": None,
            "joint_type": "free" if entity is not None else None,
            "joint_position": None,
        }

    def execute_circular(self, *, object_id: str, **_: Any) -> dict[str, Any]:
        raise PushPullUnsupported(
            f"Circular push/pull needs a hinge joint on {object_id}. The simulation "
            "platform's scene model has no scene-object joints, so there is no "
            "mechanism to drive.")

    def execute_linear(self, *, object_id: str, **_: Any) -> dict[str, Any]:
        raise PushPullUnsupported(
            f"Linear push/pull needs a slide joint on {object_id}. The simulation "
            "platform's scene model has no scene-object joints, so there is no "
            "mechanism to drive.")

    def execute_free_object_push(self, *, object_id: str,
                                 contact_normal: Sequence[float],
                                 direction: Sequence[float], distance: float,
                                 frame: str, maximum_force: float,
                                 contact_threshold: float, speed: float,
                                 maintain_contact: bool, stop_on_contact_loss: bool,
                                 timeout: float, emergency_retract: bool,
                                 normal_alignment_tolerance: float,
                                 avoid_collision: bool) -> dict[str, Any]:
        """Translate a non-grasped free body while preserving an existing pad contact."""
        del frame, maximum_force, contact_threshold, emergency_retract
        entity = self._require_entity(object_id)
        direction = np.asarray(direction, dtype=float)
        direction = direction / np.linalg.norm(direction)
        contact_normal = np.asarray(contact_normal, dtype=float)
        contact_normal = contact_normal / np.linalg.norm(contact_normal)
        if float(np.dot(direction, contact_normal)) > -np.cos(
                np.radians(normal_alignment_tolerance)):
            raise RuntimeError(
                "Free-object push direction is not opposite the outward contact normal.")

        start_pose = self.runtime.get_end_effector_pose()
        start_position = np.asarray(start_pose["position"], dtype=float)
        start_rotation = np.asarray(start_pose["rotation_matrix"], dtype=float)
        maximum_alignment_error = self._normal_alignment_error_deg(
            start_rotation, contact_normal)
        if maximum_alignment_error > normal_alignment_tolerance + 1e-9:
            raise RuntimeError(
                "End-effector tool axis is not collinear with the contact normal: "
                f"{maximum_alignment_error:.6f} deg.")

        allowed_tool_pairs = self._pad_contact_pairs(entity)
        if not allowed_tool_pairs:
            raise RuntimeError("Free-object push requires an existing finger-pad "
                               "contact at the pre-contact pose.")

        duration = float(distance) / max(float(speed), 1e-6)
        if duration > timeout:
            raise TimeoutError("Free-object push exceeds the configured timeout.")
        playback_duration = (duration / self.runtime.playback_speed
                             if self.runtime.realtime else duration)
        frames = max(2, int(np.ceil(playback_duration * self.runtime.playback_fps)) + 1)
        start_object = self._object_transform(entity)
        start_object_position = np.asarray(start_object.translation, dtype=float)
        seed = self.runtime.get_joint_positions()
        baseline_self_pairs = self._current_robot_self_contact_pairs()
        collision_checks = 0
        contact_maintained = True
        started = time.perf_counter()
        if avoid_collision:
            forbidden = self._forbidden_contacts(entity, allowed_tool_pairs,
                                                 baseline_self_pairs)
            collision_checks += 1
            if forbidden:
                raise RuntimeError(
                    f"Free-object push starts in forbidden collision: {forbidden}")

        next_frame = time.perf_counter()
        for travel in np.linspace(0.0, float(distance), frames)[1:]:
            if time.perf_counter() - started > timeout:
                raise TimeoutError("Free-object push execution timed out.")
            previous_arm = seed.copy()
            previous_object = self._object_transform(entity)
            desired_position = start_position + direction * float(travel)
            try:
                solved = self.kinematics.solve(
                    {"position": desired_position,
                     "orientation": rotation_mapping(start_rotation)},
                    seed=seed, method="trac_ik", timeout=min(0.25, timeout))
            except IKError as exc:
                self._restore(entity, previous_object, previous_arm)
                raise RuntimeError(
                    f"Arm cannot follow the free-object push line: {exc}") from exc
            seed = np.asarray(solved["solution"], dtype=float)
            self.runtime.set_joint_positions(seed)
            self._place_object(
                entity, start_object_position + direction * float(travel),
                start_object.rotation_wxyz)

            actual_rotation = self.runtime.get_end_effector_pose()["rotation_matrix"]
            alignment_error = self._normal_alignment_error_deg(actual_rotation,
                                                              contact_normal)
            maximum_alignment_error = max(maximum_alignment_error, alignment_error)
            if alignment_error > normal_alignment_tolerance + 1e-9:
                self._restore(entity, previous_object, previous_arm)
                raise RuntimeError("End-effector/contact-normal alignment was lost: "
                                   f"{alignment_error:.6f} deg.")

            has_contact = bool(self._pad_contact_pairs(entity) & allowed_tool_pairs)
            contact_maintained = contact_maintained and has_contact
            if stop_on_contact_loss and not has_contact:
                self._restore(entity, previous_object, previous_arm)
                raise RuntimeError("Finger-pad contact with the free object was lost.")
            if avoid_collision:
                forbidden = self._forbidden_contacts(entity, allowed_tool_pairs,
                                                     baseline_self_pairs)
                collision_checks += 1
                if forbidden:
                    self._restore(entity, previous_object, previous_arm)
                    raise RuntimeError(
                        f"Forbidden collision during free-object push: {forbidden}")
            if self.runtime.viewer is not None:
                self.runtime.viewer.sync()
            if self.runtime.realtime:
                next_frame += 1.0 / self.runtime.playback_fps
                delay = next_frame - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)

        final = self._object_transform(entity)
        final_position = np.asarray(final.translation, dtype=float)
        actual_distance = float(np.linalg.norm(final_position - start_object_position))
        return {
            "contact_established": True,
            "contact_maintained": bool(contact_maintained or not maintain_contact),
            "target_moved": actual_distance > 1e-5,
            "travel_distance": actual_distance,
            "rotation_angle": 0.0,
            "peak_force": 0.0,
            "object_final_state": {
                "object_id": object_id,
                "position": final_position.tolist(),
                "quaternion_wxyz": list(final.rotation_wxyz),
            },
            "evidence": {
                "simulation_adapter": "free_body_contact_coupled_motion",
                "interaction_mode": "free_object",
                "gripper_holding": False,
                "maximum_normal_alignment_error_deg": maximum_alignment_error,
                "collision_checks": collision_checks,
                "forbidden_collision_count": 0,
                "allowed_tool_contact_pairs": len(allowed_tool_pairs),
                # Object-vs-object pairs are not reported, so support contacts
                # cannot be observed. See the module docstring.
                "allowed_support_contact_pairs": 0,
            },
            "execution_time": time.perf_counter() - started,
        }

    def verification_snapshot(self, object_id: str, *,
                              interaction_mode: str = "mechanism") -> dict[str, Any]:
        if interaction_mode != "free_object":
            raise PushPullUnsupported(
                f"Mechanism push/pull needs a joint on {object_id}, which the "
                "simulation platform's scene model does not provide.")
        entity = self._require_entity(object_id)
        contact = bool(self._pad_contact_pairs(entity))
        transform = self._object_transform(entity)
        return {
            "contact_established": contact,
            "contact_maintained": contact,
            "target_moved": False,
            "travel_distance": 0.0,
            "rotation_angle": 0.0,
            "peak_force": 0.0,
            "object_final_state": {
                "object_id": object_id,
                "position": list(transform.translation),
                "quaternion_wxyz": list(transform.rotation_wxyz),
            },
            "evidence": {"interaction_mode": "free_object", "collision_checks": 0},
            "execution_time": 0.0,
        }

    # ------------------------------------------------------------------ object

    def _require_entity(self, object_id: str) -> int:
        entity = self.scene.object_entity(object_id)
        if entity is None:
            raise RuntimeError(f"Push/Pull target not found in the scene: {object_id}")
        if self.scene.parent_of(entity) is not None:
            # The transform setter writes local, so translating a parented entity by
            # a world delta would be wrong. Pushable scene objects are roots.
            raise RuntimeError(
                f"Push/Pull target {object_id} is parented to another entity; only "
                "free scene objects can be pushed.")
        return entity

    def _object_transform(self, entity: int) -> Transform:
        return self.runtime.world.entity(entity).transform

    def _place_object(self, entity: int, position: Sequence[float],
                      rotation_wxyz: Sequence[float]) -> None:
        """Move a root scene object, then re-sync so contact queries see it there."""
        current = self._object_transform(entity)
        self.runtime.world.entity(entity).transform = Transform(
            tuple(float(value) for value in position),
            tuple(float(value) for value in rotation_wxyz),
            current.scale)
        # Non-robot colliders keep the transform from the last sync, so without this
        # the object would appear to the contact query at its old pose.
        self.runtime.simulator.sync_collision(self.runtime.world)

    def _restore(self, entity: int, transform: Transform,
                 arm_joints: np.ndarray) -> None:
        self._place_object(entity, transform.translation, transform.rotation_wxyz)
        self.runtime.set_joint_positions(arm_joints)

    # ------------------------------------------------------------------ contacts

    def _pad_contact_pairs(self, entity: int) -> set[frozenset[int]]:
        """Colliding pairs between the target and a finger pad."""
        target = set(self.scene.colliders_of(entity))
        if not target:
            return set()
        pairs: set[frozenset[int]] = set()
        for pair in self.robot.configuration_contacts(
                self.runtime.full_joint_positions(), limits="ignore"):
            if not pair.colliding:
                continue
            sides = (pair.robot_collider, pair.other)
            hit = [side for side in sides if side in target]
            if not hit:
                continue
            other = sides[1] if sides[0] == hit[0] else sides[0]
            if self.scene.is_gripper_pad(other):
                pairs.add(frozenset(sides))
        return pairs

    def _forbidden_contacts(self, entity: int,
                            allowed_tool_pairs: set[frozenset[int]],
                            baseline_self_pairs: set[frozenset[int]],
                            ) -> list[dict[str, Any]]:
        """Colliding pairs that are neither the intended tool contact nor pre-existing."""
        forbidden: list[dict[str, Any]] = []
        for pair in self.robot.configuration_contacts(
                self.runtime.full_joint_positions(), limits="ignore"):
            if not pair.colliding:
                continue
            sides = (pair.robot_collider, pair.other)
            key = frozenset(sides)
            if key in allowed_tool_pairs:
                continue
            if self.scene.is_robot(sides[1]) and key in baseline_self_pairs:
                continue
            forbidden.append({"geom1": self.scene.name_of(sides[0]),
                              "geom2": self.scene.name_of(sides[1]),
                              "distance": float(pair.distance)})
        return forbidden

    def _current_robot_self_contact_pairs(self) -> set[frozenset[int]]:
        return {frozenset((pair.robot_collider, pair.other))
                for pair in self.robot.configuration_contacts(
                    self.runtime.full_joint_positions(), limits="ignore")
                if pair.colliding and self.scene.is_robot(pair.other)}

    @staticmethod
    def _normal_alignment_error_deg(rotation: np.ndarray,
                                    normal: np.ndarray) -> float:
        tool_axis = np.asarray(rotation, dtype=float)[:, 2]
        tool_axis = tool_axis / np.linalg.norm(tool_axis)
        normal = np.asarray(normal, dtype=float) / np.linalg.norm(normal)
        return float(np.degrees(np.arccos(
            np.clip(abs(float(np.dot(tool_axis, normal))), -1.0, 1.0))))
