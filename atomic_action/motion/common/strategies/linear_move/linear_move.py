"""Cartesian linear path strategy with sequential numerical IK."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from kinematics import IKError

from ..pose_utils import effective_orientation
from planning.errors import PlanningError


class LinearMove:
    """Translate a normalized Move request into a Cartesian line specification."""

    strategy_id = "linear_move"

    def __init__(self, config: Mapping[str, Any] | None = None, *, kinematics: Any = None, collision_checker: Any = None) -> None:
        self.config = config or {}
        self.kinematics = kinematics
        self.collision_checker = collision_checker

    def build_path_request(
        self,
        request: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        target = request["target"]
        if target["type"] != "pose":
            raise ValueError("Linear Move requires a pose target.")
        start_q = np.asarray(context["current_joint_state"], dtype=float)
        start_position = np.asarray(context["current_pose"]["position"], dtype=float)
        goal_position = np.array([target["position"][key] for key in ("x", "y", "z")], dtype=float)
        orientation, hold_orientation = effective_orientation(request, context)
        step = float(request["strategy_params"].get("cartesian_step", 0.02))
        count = max(2, int(np.ceil(np.linalg.norm(goal_position - start_position) / step)) + 1)
        params = request["strategy_params"]
        seed_value = params.get("ik_seed")
        seed = np.asarray(start_q if seed_value is None else seed_value, dtype=float)
        if seed.shape != (6,) or not np.all(np.isfinite(seed)):
            raise ValueError("strategy_params.ik_seed must contain six finite joint values.")
        method = params.get("ik_method", "auto")
        alphas = np.linspace(0.0, 1.0, count)[1:]
        waypoint_orientations = _waypoint_orientations(
            orientation,
            np.asarray(context["current_pose"]["quaternion_wxyz"], dtype=float),
            alphas,
            hold_orientation,
        )
        poses = [
            {
                "position": start_position + alpha * (goal_position - start_position),
                "orientation": waypoint_orientation,
            }
            for alpha, waypoint_orientation in zip(alphas, waypoint_orientations)
        ]

        exhaustive = params.get("linear_search_mode", "fast") == "exhaustive"
        # Caches live for one path build only: target, scene and constraints
        # cannot change between branches. Use exact joint values, not modulo
        # or rounded keys, to retain distinct windings and continuity behavior.
        cache = {"ik": {}, "collision": {}, "ik_hits": 0, "collision_hits": 0}
        candidates, colliding_candidates, rejected = [], [], []
        evaluated_count = 0

        def evaluate(branch_seed):
            nonlocal evaluated_count
            index = evaluated_count
            evaluated_count += 1
            _, candidate, rejection, colliding = self._evaluate_branch(
                index, branch_seed, poses, start_q, request, method, cache=cache,
            )
            if rejection is not None:
                rejected.append(rejection)
            elif colliding:
                colliding_candidates.append(candidate)
                rejected.append({"branch": index, "reason": "collision", "contacts": list(colliding)})
            elif candidate is not None:
                candidates.append(candidate)

        branch_seeds = []
        if seed_value is not None or not exhaustive:
            branch_seeds.append(seed)
            evaluate(seed)
        # A collision is also a failed fast path. Search other branches before
        # allowing the existing soft-path collision-free fallback.
        if seed_value is None and (exhaustive or not candidates):
            try:
                physical_branches = self.kinematics.solve_all(
                    poses[-1], seed=start_q, backend=method,
                    timeout=min(float(request["planning"]["planning_time"]), 3.0), max_solutions=8,
                ).solutions
            except IKError:
                # A failed multi-start search can still try the measured seed below.
                physical_branches = []
            alternatives = []
            for physical_branch in physical_branches:
                alternatives.extend(self.kinematics.equivalent_configurations(physical_branch, start_q, max_variants=8))
            from planning.arm_posture import configuration_cost
            alternatives.sort(key=lambda q: configuration_cost(self.kinematics, q, start_q, norm=2))
            alternatives = alternatives[:32]
            if not alternatives and not branch_seeds:
                alternatives = [seed]
            for alternative in alternatives:
                if any(np.array_equal(alternative, known) for known in branch_seeds):
                    continue
                branch_seeds.append(alternative)
                evaluate(alternative)
                if candidates and not exhaustive:
                    break

        if not candidates:
            if colliding_candidates:
                # A soft Cartesian request may legally relax its path.  Pass
                # the best reachable goal branch to DirectPlanner so its
                # collision result can trigger the configured RRT fallback.
                _, points, ik_diagnostics, selected_branch = min(colliding_candidates, key=lambda item: item[0])
            else:
                raise PlanningError(
                    f"No continuous IK branch reaches the target ({len(rejected)} branches rejected).",
                    code="PATH_CONSTRAINT_INFEASIBLE", recoverable=True,
                    details={
                        "candidate_count": len(branch_seeds),
                        "rejected_count": len(rejected),
                        "reason_counts": {
                            reason: sum(1 for item in rejected if str(item.get("reason", "")).split(":", 1)[0] == reason)
                            for reason in {str(item.get("reason", "")).split(":", 1)[0] for item in rejected}
                        },
                        "samples": rejected[:3],
                    },
                )
        else:
            _, points, ik_diagnostics, selected_branch = min(candidates, key=lambda item: item[0])
        return {
            "path_type": "linear", "joint_waypoints": points, "start": points[0], "goal": points[-1],
            "orientation_constraint": {"enabled": hold_orientation, "orientation": orientation},
            "diagnostics": {"ik": ik_diagnostics, "ik_branch_selection": {
                "candidate_count": len(branch_seeds), "rejected": rejected, "selected_branch": selected_branch,
                "evaluated_count": evaluated_count, "explicit_seed": seed_value is not None,
                "exhaustive_search": exhaustive and seed_value is None,
                "search_mode": "exhaustive" if exhaustive else "fast",
                "ik_cache_hits": cache["ik_hits"], "collision_cache_hits": cache["collision_hits"],
            }},
        }

    def _evaluate_branch(self, branch_index, branch_seed, poses, start_q, request, method, *, cache=None):
        """Evaluate one IK branch using thread-local MuJoCo workspaces."""
        kinematics, collision_checker = self.kinematics, self.collision_checker
        joints = [start_q]
        current_seed = np.asarray(branch_seed, dtype=float)
        ik_diagnostics = []
        first_collision = None
        max_step = float(request["strategy_params"].get("max_joint_step", 0.4))
        if cache is None:
            cache = {"ik": {}, "collision": {}, "ik_hits": 0, "collision_hits": 0}

        def solve(pose_index, pose, seed, continuity=None):
            key = (pose_index, np.asarray(seed, dtype=float).tobytes(), continuity)
            if key in cache["ik"]:
                cache["ik_hits"] += 1
                return cache["ik"][key]
            kwargs = {"max_joint_step": continuity} if continuity is not None else {}
            result = kinematics.solve_one(
                pose, seed=seed, backend=method,
                timeout=min(float(request["planning"]["planning_time"]), 1.0), **kwargs,
            )
            cache["ik"][key] = result
            return result

        try:
            for pose_index, pose in enumerate(poses):
                result = solve(pose_index, pose, current_seed)
                next_q = np.asarray(result.solution, dtype=float)
                previous_q = joints[-1]
                if np.max(np.abs(next_q - previous_q)) > max_step:
                    # A branch hint must never override continuity from the
                    # measured start or the preceding accepted waypoint.
                    result = solve(pose_index, pose, previous_q, max_step)
                    next_q = np.asarray(result.solution, dtype=float)
                if np.max(np.abs(next_q - previous_q)) > max_step:
                    raise PlanningError("Linear IK exceeds the joint continuity limit.", code="PATH_CONSTRAINT_INFEASIBLE")
                joints.append(next_q)
                current_seed = next_q
                ik_diagnostics.append(result.metadata())
                if first_collision is None and request["constraints"]["avoid_collision"] and collision_checker is not None:
                    key = (previous_q.tobytes(), next_q.tobytes())
                    if key in cache["collision"]:
                        cache["collision_hits"] += 1
                        collision = cache["collision"][key]
                    else:
                        collision = collision_checker.check_path(
                            [previous_q, next_q], resolution=0.04,
                            safety_distance=float(request["constraints"].get("safety_distance", 0.0)),
                        )
                        cache["collision"][key] = collision
                    if collision.in_collision:
                        first_collision = collision
        except Exception as exc:
            return branch_index, None, {"branch": branch_index, "reason": f"ik_failed: {exc}"}, None
        if len(joints) != len(poses) + 1:
            return branch_index, None, {"branch": branch_index, "reason": "ik_failed: incomplete path"}, None
        points = np.asarray(joints)
        deltas = np.diff(points, axis=0)
        score = float(np.sum(np.linalg.norm(deltas, axis=1)) + 2.0 * np.max(np.abs(deltas)))
        from planning.arm_posture import preference_for
        posture = preference_for(kinematics)
        if posture is not None:
            score += posture.path_penalty(points)
        candidate = (score, points, ik_diagnostics, branch_index)
        return branch_index, candidate, None, first_collision.contacts if first_collision is not None else None

def _waypoint_orientations(
    target: Mapping[str, Any] | None,
    start_wxyz: np.ndarray,
    alphas: np.ndarray,
    hold: bool,
) -> list[Mapping[str, float] | None]:
    """Interpolate unconstrained orientation instead of applying it abruptly."""
    if target is None or hold:
        return [target] * len(alphas)
    start = np.asarray(start_wxyz, dtype=float)
    if start.shape != (4,) or not np.all(np.isfinite(start)) or np.linalg.norm(start) < 1e-12:
        raise ValueError("Current end-effector orientation must be a finite quaternion.")
    start /= np.linalg.norm(start)
    goal = _orientation_to_wxyz(target)
    if float(start @ goal) < 0.0:
        goal = -goal
    dot = float(np.clip(start @ goal, -1.0, 1.0))
    angle = float(np.arccos(dot))
    result = []
    for alpha in alphas:
        if angle < 1e-8:
            quaternion = (1.0 - alpha) * start + alpha * goal
        else:
            quaternion = (
                np.sin((1.0 - alpha) * angle) / np.sin(angle) * start
                + np.sin(alpha * angle) / np.sin(angle) * goal
            )
        quaternion /= np.linalg.norm(quaternion)
        result.append({
            "representation": "quaternion",
            "x": float(quaternion[1]),
            "y": float(quaternion[2]),
            "z": float(quaternion[3]),
            "w": float(quaternion[0]),
        })
    return result


def _orientation_to_wxyz(orientation: Mapping[str, Any]) -> np.ndarray:
    if orientation.get("representation", "quaternion") == "rpy":
        roll, pitch, yaw = np.radians([orientation[key] for key in ("roll", "pitch", "yaw")]) / 2.0
        cr, sr = np.cos(roll), np.sin(roll)
        cp, sp = np.cos(pitch), np.sin(pitch)
        cy, sy = np.cos(yaw), np.sin(yaw)
        quaternion = np.array([
            cr * cp * cy + sr * sp * sy,
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
        ])
    else:
        quaternion = np.array([orientation[key] for key in ("w", "x", "y", "z")], dtype=float)
    if not np.all(np.isfinite(quaternion)) or np.linalg.norm(quaternion) < 1e-12:
        raise ValueError("Target orientation must define a finite nonzero quaternion.")
    return quaternion / np.linalg.norm(quaternion)

