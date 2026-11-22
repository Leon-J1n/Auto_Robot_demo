"""Cartesian linear path strategy with sequential numerical IK."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ..pose_utils import effective_orientation
from ...planners.errors import PlanningError


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
        """The smoothest Cartesian path to the target."""
        return self.build_path_requests(request, context, max_candidates=1)[0]

    def build_path_requests(
        self,
        request: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        max_candidates: int = 8,
    ) -> list[Mapping[str, Any]]:
        """Every distinct Cartesian path that reaches the target, smoothest first.

        One straight line in space admits many joint-space paths: the arm can reach a pose
        elbow-up or elbow-down, and several wrist configurations put the hand in the same
        place. The enumeration below already builds and validates a complete path for each
        such branch -- up to 32 of them -- and then keeps only the cheapest. A caller that
        means to offer a choice wants the rest, so this returns them, deduplicated because
        distinct branches often converge on the same motion.

        `build_path_request` is this with `max_candidates=1`, so the planning pipeline's
        behaviour is unchanged: it still plans against the single smoothest path.
        """
        if max_candidates < 1:
            raise ValueError("max_candidates must be at least one.")
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
        safety_distance = float(request["constraints"].get("safety_distance", 0.0))
        checking = bool(request["constraints"]["avoid_collision"]) and self.collision_checker is not None
        alphas = np.linspace(0.0, 1.0, count)[1:]
        waypoint_orientations = _waypoint_orientations(
            orientation,
            np.asarray(context["current_pose"].get("quaternion_wxyz"), dtype=float),
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

        # An explicit seed remains an authoritative branch hint.  Otherwise,
        # enumerate target-pose branches and validate each complete Cartesian
        # path before selecting the smoothest collision-free one.
        if seed_value is not None:
            branch_seeds = [seed]
        else:
            physical_branches = self.kinematics.solve_candidates(
                poses[-1], seed=start_q, method=method,
                timeout=min(float(request["planning"]["planning_time"]), 3.0), max_solutions=8,
            )
            branch_seeds = []
            for physical_branch in physical_branches:
                branch_seeds.extend(self.kinematics.equivalent_configurations(physical_branch, start_q, max_variants=8))
            branch_seeds.sort(key=lambda q: float(np.linalg.norm(q - start_q)))
            branch_seeds = branch_seeds[:32]
            if not branch_seeds:
                branch_seeds = [seed]
            elif checking and physical_branches:
                # If no posture that reaches the target is itself clear, then no path
                # ending there can be clear either, whatever route it takes. Saying so now
                # costs one collision query per posture instead of a full Cartesian IK
                # chain per branch -- the difference between answering in a moment and
                # spending twenty seconds to reach the same conclusion. Only the physical
                # postures are tested: the equivalents added above differ from them by
                # whole turns of single joints, which is the same arm in the same place.
                blocked = [self.collision_checker.check(branch, safety_distance=safety_distance)
                           for branch in physical_branches]
                if all(result.in_collision for result in blocked):
                    contacts = [contact for result in blocked for contact in result.contacts]
                    raise PlanningError(
                        "Every arm posture that reaches the target is in collision.",
                        code="COLLISION_DETECTED", recoverable=True,
                        details={"postures_tested": len(blocked), "contacts": contacts[:6]},
                    )
                # Some postures are clear and some are not. A branch whose own goal posture
                # collides cannot produce a clear path either, so it is dropped before its
                # Cartesian chain is built rather than after: each such chain costs about
                # forty IK solves and forty collision queries, and near an obstacle a query
                # runs several times slower than in open space.
                #
                # The surviving seeds are rebuilt from the clear postures rather than
                # filtered out of the existing list, because an equivalent differs from its
                # parent posture by whole turns of single joints -- so regenerating gives
                # exactly the equivalents of the clear postures, with no matching to do.
                clear = [branch for branch, result in zip(physical_branches, blocked)
                         if not result.in_collision]
                if clear:
                    surviving = []
                    for posture in clear:
                        surviving.extend(self.kinematics.equivalent_configurations(
                            posture, start_q, max_variants=8))
                    surviving.sort(key=lambda q: float(np.linalg.norm(q - start_q)))
                    branch_seeds = surviving[:32]

        candidates = []
        colliding_candidates = []
        rejected = []
        for branch_index, branch_seed in enumerate(branch_seeds):
            joints = [start_q]
            current_seed = np.asarray(branch_seed, dtype=float)
            ik_diagnostics = []
            first_collision = None
            try:
                for pose in poses:
                    result = self.kinematics.solve(
                        pose, seed=current_seed, method=method,
                        timeout=min(float(request["planning"]["planning_time"]), 1.0),
                    )
                    next_q = np.asarray(result["solution"], dtype=float)
                    previous_q = joints[-1]
                    joints.append(next_q)
                    current_seed = next_q
                    ik_diagnostics.append({key: value for key, value in result.items() if key not in {"solution", "solutions"}})
                    if first_collision is None and checking:
                        collision = self.collision_checker.check_path(
                            [previous_q, next_q], resolution=0.04,
                            safety_distance=safety_distance,
                        )
                        if collision.in_collision:
                            first_collision = collision
                            # The rest of this branch is IK for a path already known to
                            # collide. It was being solved to the end so the diagnostics
                            # would carry a full waypoint list, which costs a solve per
                            # remaining pose -- around 40 of them per branch, across up to
                            # 32 branches, to describe a path that cannot be offered.
                            # Stopping here is what keeps a blocked Cartesian target
                            # answering in under a second instead of eighteen.
                            break
            except Exception as exc:
                rejected.append({"branch": branch_index, "reason": f"ik_failed: {exc}"})
                continue
            if first_collision is None and len(joints) != len(poses) + 1:
                # An incomplete chain with no recorded collision means IK quietly stopped
                # short, which is not a usable path. A chain cut short *by* a collision is
                # expected -- the break above does that deliberately -- and is still worth
                # keeping as the colliding candidate that seeds the RRT fallback.
                continue
            points = np.asarray(joints)
            deltas = np.diff(points, axis=0)
            score = float(np.sum(np.linalg.norm(deltas, axis=1)) + 2.0 * np.max(np.abs(deltas)))
            # A branch cut short by a collision stops mid-path, so its last waypoint is not
            # the target. The branch seed is, being the IK solution for the final pose that
            # this branch was built from -- RRT reroutes to that, since handing it a
            # waypoint the arm merely passed through would send it to the wrong place.
            complete = len(joints) == len(poses) + 1
            candidate = (score, points, ik_diagnostics, branch_index,
                         points[-1] if complete else np.asarray(branch_seed, dtype=float))
            if first_collision is None:
                candidates.append(candidate)
                if len(candidates) >= max_candidates:
                    # Enough clear paths, so the remaining branches go unbuilt. The seeds
                    # were sorted by joint travel from the start, and travel is most of what
                    # the score below measures, so the branches skipped here are the ones
                    # that would have scored worst. This is the smoothest path among those
                    # examined rather than provably the smoothest of all 32 -- a trade the
                    # caller can undo by asking for more candidates, and one worth making at
                    # roughly a third of a second per branch built.
                    break
            else:
                rejected.append({"branch": branch_index, "reason": "collision", "contacts": list(first_collision.contacts)})
                colliding_candidates.append(candidate)

        # Clear paths first, then the colliding ones. A colliding branch is still a usable
        # candidate -- a soft Cartesian request may legally relax its path, and the
        # DirectPlanner's collision result is what triggers the configured RRT fallback --
        # it just costs more than a branch that was clear to begin with.
        ranked = [(entry, True) for entry in sorted(candidates, key=lambda item: item[0])]
        ranked += [(entry, False)
                   for entry in sorted(colliding_candidates, key=lambda item: item[0])]
        if not ranked:
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
        results: list[Mapping[str, Any]] = []
        seen: set[bytes] = set()
        for (_, points, ik_diagnostics, selected_branch, goal), collision_free in ranked:
            # Branches often converge on one motion: an equivalent configuration that
            # differs by a full wrist turn traces the same line through space. Offering
            # that twice would spend a slot and a colour on a duplicate.
            fingerprint = np.round(points, 6).tobytes()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            results.append({
                "path_type": "linear", "joint_waypoints": points, "start": points[0],
                "goal": goal,
                "orientation_constraint": {"enabled": hold_orientation, "orientation": orientation},
                "diagnostics": {"ik": ik_diagnostics, "ik_branch_selection": {
                    "candidate_count": len(branch_seeds), "rejected": rejected, "selected_branch": selected_branch,
                    "explicit_seed": seed_value is not None, "collision_free": collision_free,
                }},
            })
            if len(results) >= max_candidates:
                break
        return results


def _waypoint_orientations(
    target: Mapping[str, Any] | None,
    start_wxyz: np.ndarray,
    alphas: np.ndarray,
    hold: bool,
) -> list[Mapping[str, float] | None]:
    """Interpolate unconstrained orientation instead of applying it abruptly."""
    if target is None or hold:
        return [target] * len(alphas)
    if start_wxyz.shape != (4,) or not np.all(np.isfinite(start_wxyz)) or np.linalg.norm(start_wxyz) < 1e-12:
        raise ValueError("Current end-effector orientation must be a finite quaternion.")
    start = start_wxyz / np.linalg.norm(start_wxyz)
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
            "x": float(quaternion[1]), "y": float(quaternion[2]),
            "z": float(quaternion[3]), "w": float(quaternion[0]),
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
