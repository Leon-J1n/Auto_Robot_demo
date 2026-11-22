"""Collision checking isolated from the live simulation state.

Replaces the MuJoCo-backed checker. The platform answers a configuration query on a
clone of the world, so nothing here perturbs the running scene, and the same
rest-pose baseline subtraction keeps a robot's permanently-touching parts from
reading as collisions.

One honest difference from MuJoCo: the platform clamps reported distances at zero
rather than returning penetration depth, so `minimum_distance` bottoms out at 0.0
instead of going negative. Depth is not needed to decide whether a configuration is
usable -- the query flags each pair as colliding or not -- but a caller wanting to
rank two colliding configurations by how badly they overlap cannot do so.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import logging
from typing import Any, Sequence

import numpy as np


@dataclass(frozen=True)
class CollisionResult:
    in_collision: bool
    minimum_distance: float
    contacts: tuple[dict[str, object], ...] = ()


class PlatformCollisionChecker:
    """Check contacts involving the robot while ignoring environment-only contacts."""

    def __init__(self, runtime: Any, *, cache_limit: int = 20000) -> None:
        self.runtime = runtime
        self.robot = runtime.robot
        self.scene = runtime.scene
        self.cache_limit = max(int(cache_limit), 0)
        # Planning repeatedly probes shared segment endpoints (the direct planner,
        # retimer and fallback planner all validate the same samples).  Keep verdicts
        # for the lifetime of one planning pass; MoveSkill clears this cache before
        # each new request so moving scene objects never make a stale result visible.
        self._cache: dict[tuple[Any, ...], CollisionResult] = {}
        self._cache_enabled = False
        self._planning_joints = None
        self.query_count = 0
        self.cache_hits = 0
        # The query only ever reports pairs with at least one robot-side collider, so
        # the environment-only filtering the MuJoCo version needed is implicit here.
        self._baselines: dict[float, frozenset[frozenset[int]]] = {}
        self._initial_environment_distances: dict[float, dict[frozenset[int], float]] = {}
        self._start_joints = np.asarray(self.runtime.full_joint_positions(), dtype=float)
        self.baseline_allowed_pairs = self._baseline(0.0)

    # ------------------------------------------------------------------ baseline

    def _baseline(self, threshold: float) -> frozenset[frozenset[int]]:
        """Pairs already reported at the current pose, cached per threshold.

        A clearance query reports more pairs than a contact query, so the set that
        must be forgiven depends on the threshold it was gathered at.
        """
        key = round(max(threshold, 0.0), 6)
        cached = self._baselines.get(key)
        if cached is not None:
            return cached
        contacts = self.robot.configuration_contacts(
            self.runtime.full_joint_positions(), distance_threshold=key, limits="ignore")
        # Only permanent robot self contacts are a baseline exemption. A nearby
        # box must never become invisible for the entire motion.
        pairs = frozenset(frozenset((pair.robot_collider, pair.other))
                          for pair in contacts if self.scene.is_robot(pair.robot_collider)
                          and self.scene.is_robot(pair.other))
        self._initial_environment_distances[key] = {
            frozenset((pair.robot_collider, pair.other)): float(pair.distance)
            for pair in contacts if not self.scene.is_robot(pair.other) and not pair.colliding}
        self._baselines[key] = pairs
        return pairs

    def refresh_baseline(self) -> None:
        """Re-measure the baseline, after the scene's composition changed."""
        self._baselines.clear()
        self._initial_environment_distances.clear()
        self._start_joints = np.asarray(self.runtime.full_joint_positions(), dtype=float)
        self._cache.clear()
        self.baseline_allowed_pairs = self._baseline(0.0)

    # ------------------------------------------------------------------ checking

    def check(self, joints: Sequence[float], *, safety_distance: float = 0.0,
              allowed_geom_ids: set[int] | None = None) -> CollisionResult:
        if allowed_geom_ids is None:
            allowed_geom_ids = getattr(self, "allowed_target_geom_ids", None)
        values = np.asarray(joints, dtype=float)
        if values.shape != (6,):
            raise ValueError("Collision check requires a 6-value UR5e joint vector.")
        threshold = max(float(safety_distance), 0.0)
        key = self._cache_key(values, threshold, allowed_geom_ids)
        cached = self._cache.get(key)
        if self._cache_enabled and cached is not None:
            self.cache_hits += 1
            return cached
        baseline = self._baseline(threshold)
        held = self._held_colliders()
        contacts = []
        minimum = float("inf")
        self.query_count += 1
        for pair in self.robot.configuration_contacts(
                (self.runtime.expand_joints(values, base=self._planning_joints)
                 if self._planning_joints is not None else self.runtime.expand_joints(values)),
                distance_threshold=threshold, limits="ignore"):
            first, second = pair.robot_collider, pair.other
            if allowed_geom_ids and ((first in allowed_geom_ids and self.scene.is_gripper(second))
                                     or (second in allowed_geom_ids and self.scene.is_gripper(first))):
                continue
            if self._is_held_object_pad_contact(first, second, held):
                continue
            if frozenset((first, second)) in baseline:
                continue
            distance = float(pair.distance)
            minimum = min(minimum, distance)
            # Distances are clamped at zero, so a merely-near pair can also read 0.0.
            # The colliding flag is what distinguishes them; the distance gate only
            # applies when a caller asked for real clearance.
            required = threshold
            initial = self._initial_environment_distances.get(round(threshold, 6), {}).get(
                frozenset((first, second)))
            if held and initial is not None and initial < threshold:
                # Permit retreat from an initially tight grasp, but require the
                # gap to grow and reach full clearance within 0.2 rad of motion.
                start = self._planning_joints if self._planning_joints is not None else self._start_joints
                current = self.runtime.expand_joints(values, base=start) if self._planning_joints is not None else self.runtime.expand_joints(values)
                progress = min(float(np.max(np.abs(np.asarray(current) - start))) / 0.2, 1.0)
                required = initial + (threshold - initial) * progress
            if not (pair.colliding or (required > 0.0 and distance < required - 1e-9)):
                continue
            contacts.append({
                "geom1": self.scene.name_of(first),
                "geom2": self.scene.name_of(second),
                "distance": distance,
                "self_collision": self._is_robot_side(second),
            })
        result = CollisionResult(bool(contacts), minimum, tuple(contacts))
        if self._cache_enabled and len(self._cache) < self.cache_limit:
            self._cache[key] = result
        return result

    def check_path(self, waypoints: Sequence[Sequence[float]], *,
                   resolution: float = 0.05, safety_distance: float = 0.0,
                   allowed_geom_ids: set[int] | None = None) -> CollisionResult:
        if allowed_geom_ids is None:
            allowed_geom_ids = getattr(self, "allowed_target_geom_ids", None)
        points = np.asarray(waypoints, dtype=float)
        all_contacts = []
        minimum = float("inf")
        for start, goal in zip(points[:-1], points[1:]):
            count = max(2, int(np.ceil(np.max(np.abs(goal - start)) / resolution)) + 1)
            for alpha in np.linspace(0.0, 1.0, count):
                result = self.check(start + alpha * (goal - start),
                                    safety_distance=safety_distance,
                                    allowed_geom_ids=allowed_geom_ids)
                minimum = min(minimum, result.minimum_distance)
                if result.in_collision:
                    all_contacts.extend(result.contacts)
                    return CollisionResult(True, minimum, tuple(all_contacts))
        return CollisionResult(False, minimum, ())

    def clear_cache(self) -> None:
        """Discard configuration verdicts after the live scene has changed."""
        self._cache.clear()

    def end_planning(self) -> None:
        """Stop reusing planning state before execution mutates the live scene."""
        self._cache_enabled = False
        self._planning_joints = None
        self.clear_cache()

    @contextmanager
    def planning_scope(self):
        """Reuse exact queries only while a Move planning pass owns the scene."""
        self.clear_cache()
        self._planning_joints = self.runtime.full_joint_positions()
        self._cache_enabled = True
        queries, hits = self.query_count, self.cache_hits
        try:
            yield
        finally:
            self.end_planning()
            logging.getLogger(__name__).debug(
                "Move collision queries=%d reused=%d",
                self.query_count - queries, self.cache_hits - hits)

    def _cache_key(self, joints: np.ndarray, threshold: float,
                   allowed_geom_ids: set[int] | None) -> tuple[Any, ...]:
        allowed = () if not allowed_geom_ids else tuple(sorted(allowed_geom_ids))
        held = tuple(sorted(self._held_colliders()))
        # Exact keys avoid silently relaxing clearance or rounding across a contact
        # boundary. Shared endpoints and repeated path checks are still identical.
        return (tuple(joints), threshold, allowed, held)

    # ------------------------------------------------------------------ helpers

    def _held_colliders(self) -> set[int]:
        """Colliders of the object clamped between the pads.

        A clamp holds by touching, so those contacts are the intended state of the
        gripper rather than a fault, and have to be excused below.
        """
        controller = getattr(self.runtime, "gripper_controller", None)
        return set(getattr(controller, "held_colliders", ()) or ())

    def _is_robot_side(self, entity: int) -> bool:
        """Whether the non-robot side of a pair is in fact part of the robot.

        The platform reports self pairs with the second collider also on the robot.
        A clamped object is not among them: it is never parented to a link and stays
        a scene object owned by physics, so it striking the arm is a collision with
        the environment and is reported as one.
        """
        return self.scene.is_robot(entity)

    def _is_held_object_pad_contact(self, first: int, second: int,
                                    held: set[int]) -> bool:
        """Allow clamped-object contact with its gripper, but not the arm or scene."""
        if not held or (first not in held and second not in held):
            return False
        other = second if first in held else first
        return self.scene.is_gripper(other)
