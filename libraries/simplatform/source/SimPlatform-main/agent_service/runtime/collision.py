"""Collision checking isolated from the live simulation state.

The platform answers a configuration query on a private copy of the world, so nothing
here perturbs the running scene, and the rest-pose baseline subtraction keeps a
robot's permanently-touching parts from reading as collisions. That subtraction is not
optional: the Robotiq gripper alone reports around nineteen self-pairs at every finger
angle, so a raw contact count says nothing about whether a configuration is usable.

Two honest limitations, both inherited from the platform rather than introduced here:

The platform clamps reported distances at zero rather than returning penetration
depth, so `minimum_distance` bottoms out at 0.0 instead of going negative. Depth is
not needed to decide whether a configuration is usable -- the query flags each pair as
colliding or not -- but a caller wanting to rank two colliding configurations by how
badly they overlap cannot do so.

`configuration_contacts` takes one configuration per call, and over Live Python each
call is a TCP round trip, so a path check costs one round trip per interpolated
sample. The batch alternative, `configurations_collide`, cannot be used as a
pre-screen: it reports raw collision, which the gripper's baseline self-pairs make
true at every configuration. What is done instead is to never ask the same question
twice -- results are cached per configuration, which matters because RRT shortcutting
re-tests shared segment endpoints constantly -- and to stop at the first colliding
sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

# Configurations are cached at 1e-6 rad, far finer than any planner's resolution and
# far coarser than the float noise that would make every lookup a miss.
_CACHE_DECIMALS = 6


@dataclass(frozen=True)
class CollisionResult:
    in_collision: bool
    minimum_distance: float
    contacts: tuple[dict[str, object], ...] = ()


class LiveCollisionChecker:
    """Check contacts involving the robot while ignoring environment-only contacts."""

    def __init__(self, runtime: Any, *, cache_limit: int = 20000) -> None:
        self.runtime = runtime
        self.robot = runtime.robot
        self.scene = runtime.scene
        self.cache_limit = int(cache_limit)
        # The query only ever reports pairs with at least one robot-side collider, so
        # environment-only filtering is implicit here.
        self._baselines: dict[float, frozenset[frozenset[int]]] = {}
        self._cache: dict[tuple[Any, ...], CollisionResult] = {}
        self.query_count = 0
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
        pairs = frozenset(
            frozenset((pair["robot_collider"], pair["other"]))
            for pair in self._contacts(self.runtime.full_joint_positions(), key))
        self._baselines[key] = pairs
        return pairs

    def refresh_baseline(self) -> None:
        """Re-measure the baseline, after the scene's composition changed."""
        self._baselines.clear()
        self._cache.clear()
        self.baseline_allowed_pairs = self._baseline(0.0)

    def _contacts(self, positions: Sequence[float],
                  threshold: float) -> list[dict[str, Any]]:
        """One contact query, normalised to dicts whichever surface answered it."""
        self.query_count += 1
        pairs = self.robot.configuration_contacts(
            [float(value) for value in positions],
            distance_threshold=float(threshold), limits="ignore")
        return [_pair(pair) for pair in pairs]

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
        if cached is not None:
            return cached
        baseline = self._baseline(threshold)
        held = self._held_colliders()
        contacts = []
        minimum = float("inf")
        for pair in self._contacts(self.runtime.expand_joints(values), threshold):
            first, second = pair["robot_collider"], pair["other"]
            if self._is_manipulation_contact(first, second, allowed_geom_ids):
                continue
            if self._is_held_object_pad_contact(first, second, held):
                continue
            if frozenset((first, second)) in baseline:
                continue
            distance = float(pair["distance"])
            minimum = min(minimum, distance)
            # Distances are clamped at zero, so a merely-near pair can also read 0.0.
            # The colliding flag is what distinguishes them; the distance gate only
            # applies when a caller asked for real clearance.
            if not (pair["colliding"] or (threshold > 0.0 and distance <= threshold)):
                continue
            contacts.append({
                "geom1": self.scene.name_of(first),
                "geom2": self.scene.name_of(second),
                "distance": distance,
                "self_collision": self._is_robot_side(second),
            })
        result = CollisionResult(bool(contacts), minimum, tuple(contacts))
        if len(self._cache) < self.cache_limit:
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
        """Forget cached verdicts, after anything in the scene moved."""
        self._cache.clear()

    # ------------------------------------------------------------------ helpers

    def _cache_key(self, joints: np.ndarray, threshold: float,
                   allowed_geom_ids: Iterable[int] | None) -> tuple[Any, ...]:
        allowed = () if not allowed_geom_ids else tuple(sorted(allowed_geom_ids))
        held = tuple(sorted(self._held_colliders()))
        return (tuple(np.round(joints, _CACHE_DECIMALS)), round(threshold, 6),
                allowed, held)

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

    def _is_manipulation_contact(self, first: int, second: int,
                                 allowed: set[int] | None) -> bool:
        """Whether a pair is the intended contact of a manipulation, not a fault.

        Manipulating something means touching it: the pads close around a grasp
        target, and the hand goes inside a container to place an object in it. Those
        contacts are the point of the motion, so a caller naming the object being
        manipulated excuses them.

        What is *not* excused is the rest of the arm hitting the same object. A
        container is the case that makes the distinction matter -- exempting a tray's
        whole subtree so the hand can reach inside would otherwise also let the
        forearm sweep through a tray wall on the way, which is a real collision that
        happens to involve an exempted entity. Only gripper-side colliders get the
        pass; every other link still has to respect the object's geometry.
        """
        if not allowed:
            return False
        if first in allowed and second in allowed:
            return True
        if first not in allowed and second not in allowed:
            return False
        other = second if first in allowed else first
        # The platform reports every pair with at least one robot collider, so `other`
        # is robot-side unless the exempted object is touching another scene object.
        return self.scene.is_gripper(other) or not self.scene.is_robot(other)


def _pair(value: Any) -> dict[str, Any]:
    """One contact pair as a dict, from either the Live mapping or a ContactPair."""
    if isinstance(value, Mapping):
        return {"robot_collider": int(value["robot_collider"]),
                "other": int(value["other"]),
                "distance": float(value["distance"]),
                "colliding": bool(value["colliding"])}
    return {"robot_collider": int(value.robot_collider), "other": int(value.other),
            "distance": float(value.distance), "colliding": bool(value.colliding)}
