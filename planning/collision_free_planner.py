"""Dependency-free bidirectional RRT-Connect planner."""

from __future__ import annotations

import time
from contextlib import nullcontext
from typing import Any, Mapping

import numpy as np

from .errors import PlanningError
from .trajopt_optimizer import TrajOptOptimizer


class CollisionFreePlanner:
    """Search for a safe alternative trajectory while preserving legal constraints."""

    planner_id = "collision_free"

    def __init__(self, config: Mapping[str, Any] | None = None, *, collision_checker: Any = None, joint_limits: Any = None, kinematics: Any = None) -> None:
        self.config = config or {}
        self.collision_checker = collision_checker
        self.joint_limits = np.asarray(joint_limits, dtype=float)
        self.kinematics = kinematics
        collision_free_config = self.config.get("planning", {}).get("collision_free", {})
        self.rrt_collision_resolution = max(1e-4, float(collision_free_config.get("rrt_collision_resolution", 0.02)))
        trajopt_config = dict(collision_free_config.get("trajopt", {}))
        trajopt_config["enabled"] = (
            bool(collision_free_config.get("optimize_path", True))
            and bool(trajopt_config.get("enabled", True))
        )
        self.trajopt = TrajOptOptimizer(
            collision_checker=self.collision_checker,
            joint_limits=self.joint_limits,
            config=trajopt_config,
        )

    def plan(
        self,
        path_request: Mapping[str, Any],
        move_request: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Any:
        if move_request["motion"]["path_constraint"] == "hard" and path_request["path_type"] != "joint":
            raise PlanningError("A hard Cartesian path cannot be relaxed for collision avoidance.", code="PATH_CONSTRAINT_INFEASIBLE", recoverable=True)
        started = time.perf_counter()
        deadline = started + float(move_request["planning"]["planning_time"])
        attempts = int(move_request["planning"]["max_attempts"])
        start = np.asarray(path_request["start"], dtype=float)
        goal = np.asarray(path_request["goal"], dtype=float)
        safety_distance = float(move_request["constraints"].get("safety_distance", 0.0))
        start_collision = self.collision_checker.check(start, safety_distance=safety_distance)
        if start_collision.in_collision:
            raise PlanningError(
                "Start state is in collision.", code="COLLISION_DETECTED", recoverable=True,
                details={"location": "start", "contacts": list(start_collision.contacts)},
            )
        goal_collision = self.collision_checker.check(goal, safety_distance=safety_distance)
        if goal_collision.in_collision:
            raise PlanningError(
                "Goal state is in collision.", code="COLLISION_DETECTED", recoverable=True,
                details={"location": "goal", "contacts": list(goal_collision.contacts)},
            )
        orientation_constraint = path_request.get("orientation_constraint", {})
        if orientation_constraint.get("enabled"):
            if self.kinematics is None:
                raise PlanningError("Fixed-orientation collision planning requires a kinematics service.", code="PLANNING_FAILED")
            return self._plan_fixed_orientation(
                start, goal, move_request, context, orientation_constraint["orientation"], deadline, attempts, safety_distance, started,
            )
        cache_scope = getattr(self.collision_checker, 'planning_cache', nullcontext)
        with cache_scope():
            rng = np.random.default_rng(17)
            path = None
            for _ in range(attempts):
                path = self._rrt_connect(start, goal, deadline, rng, safety_distance)
                if path is not None:
                    break
            if path is None:
                raise PlanningError(
                    "RRT-Connect could not find a collision-free path within the planning budget.",
                    code="PATH_NOT_FOUND", recoverable=True,
                    details={"planner": "rrt_connect", "attempts": attempts, "planning_time": float(move_request["planning"]["planning_time"])},
                )
            shortcut_diagnostics: dict[str, Any] = {}
            path = self._shortcut(path, rng, safety_distance, iterations=120, diagnostics=shortcut_diagnostics)
            trajopt_seed = _densify(np.asarray(path), 0.04)
            optimized_path, trajopt_diagnostics = self.trajopt.optimize(
                trajopt_seed,
                safety_distance=safety_distance,
            )
        waypoints = _densify(optimized_path, 0.04)
        allowed_geom_ids = getattr(self.collision_checker, "allowed_target_geom_ids", None)
        final_check = self.collision_checker.check_path(
            waypoints,
            resolution=0.01,
            safety_distance=safety_distance,
            allowed_geom_ids=allowed_geom_ids,
        )
        if final_check.in_collision:
            # A discretization or scene-state edge case must never discard a
            # feasible RRT solution merely because optimization was attempted.
            fallback_waypoints = _densify(np.asarray(path), 0.04)
            fallback_check = self.collision_checker.check_path(
                fallback_waypoints, resolution=0.01,
                safety_distance=safety_distance, allowed_geom_ids=allowed_geom_ids,
            )
            if fallback_check.in_collision:
                raise PlanningError(
                    "Final collision-free path failed collision validation.",
                    code="TRAJECTORY_INVALID", recoverable=True,
                    details={"stage": "final_collision_check", "contacts": list(fallback_check.contacts)},
                )
            waypoints = fallback_waypoints
            trajopt_diagnostics.update(applied=False, fallback_to_rrt=True, reason="optimized_path_final_check_failed")
        trajectory = {
            "planner": self.planner_id,
            "path_type": "joint",
            "original_path_type": path_request["path_type"],
            "waypoints": waypoints.tolist(),
            "planning_time": time.perf_counter() - started,
            "duration": float(np.sum(np.max(np.abs(np.diff(waypoints, axis=0)), axis=1)) / max(float(move_request["constraints"]["velocity_scale"]), 0.01)),
            "diagnostics": {
                "algorithm": "rrt_connect+trajopt",
                "path_relaxed": path_request["path_type"] != "joint",
                "shortcut": shortcut_diagnostics,
                "collision_state_cache": getattr(self.collision_checker, "last_planning_cache_stats", None),
                "trajopt": trajopt_diagnostics,
            },
        }
        return trajectory

    def _plan_fixed_orientation(
        self,
        start_q: np.ndarray,
        goal_q: np.ndarray,
        move_request: Mapping[str, Any],
        context: Mapping[str, Any],
        orientation: Mapping[str, Any],
        deadline: float,
        attempts: int,
        safety_distance: float,
        started: float,
    ) -> dict[str, Any]:
        """Plan a translational detour while keeping one Cartesian orientation.

        The tree lives in end-effector position space.  Every edge is converted
        to a dense, continuous IK chain before being accepted, so unlike the
        joint-space RRT fallback it cannot introduce an unconstrained wrist turn.
        """
        start_position = np.asarray(context["current_pose"]["position"], dtype=float)
        goal_position = np.array([move_request["target"]["position"][key] for key in ("x", "y", "z")], dtype=float)
        cartesian_step = float(move_request["strategy_params"].get("cartesian_step", 0.02))
        rng = np.random.default_rng(17)
        nodes: list[tuple[np.ndarray, np.ndarray, int]] = [(start_position, start_q, -1)]
        goal_index: int | None = None
        # A bounded workspace region centred on the requested move keeps the
        # sampler useful without assuming a robot-specific global workspace.
        margin = max(0.20, min(0.50, np.linalg.norm(goal_position - start_position) + 0.10))
        lower = np.minimum(start_position, goal_position) - margin
        upper = np.maximum(start_position, goal_position) + margin
        for _ in range(max(1, attempts) * 900):
            if time.perf_counter() >= deadline:
                break
            sample = goal_position if rng.random() < 0.20 else rng.uniform(lower, upper)
            parent_index = int(np.argmin([np.linalg.norm(node[0] - sample) for node in nodes]))
            parent_position, parent_q, _ = nodes[parent_index]
            delta = sample - parent_position
            length = float(np.linalg.norm(delta))
            if length < 1e-9:
                continue
            candidate = sample if length <= 0.08 else parent_position + delta / length * 0.08
            edge = self._fixed_orientation_edge(parent_position, parent_q, candidate, orientation, cartesian_step, safety_distance, move_request)
            if edge is None:
                continue
            nodes.append((candidate, edge[-1], parent_index))
            candidate_index = len(nodes) - 1
            if np.linalg.norm(candidate - goal_position) <= 0.08:
                final_edge = self._fixed_orientation_edge(candidate, edge[-1], goal_position, orientation, cartesian_step, safety_distance, move_request)
                if final_edge is not None:
                    nodes.append((goal_position, final_edge[-1], candidate_index))
                    goal_index = len(nodes) - 1
                    break
        if goal_index is None:
            raise PlanningError(
                "No collision-free path exists with the end-effector orientation fixed.",
                code="PATH_NOT_FOUND", recoverable=True,
                details={"planner": "cartesian_rrt_fixed_orientation", "attempts": attempts, "planning_time": float(move_request["planning"]["planning_time"])},
            )
        chain: list[tuple[np.ndarray, np.ndarray]] = []
        index = goal_index
        while index >= 0:
            position, joints, parent = nodes[index]
            chain.append((position, joints))
            index = parent
        chain.reverse()
        waypoints = [chain[0][1]]
        for (position_a, joints_a), (position_b, _) in zip(chain[:-1], chain[1:]):
            edge = self._fixed_orientation_edge(position_a, waypoints[-1], position_b, orientation, cartesian_step, safety_distance, move_request)
            if edge is None:  # Defensive: a previously validated edge became invalid.
                raise PlanningError(
                    "Fixed-orientation path could not be reconstructed.",
                    code="TRAJECTORY_INVALID", recoverable=True,
                    details={"stage": "path_reconstruction"},
                )
            waypoints.extend(edge[1:])
        output = np.asarray(waypoints)
        original = output.copy()
        output, shortcut_diagnostics = self._shortcut_fixed_orientation(
            output, orientation, cartesian_step, safety_distance, move_request, rng,
        )
        final_check = self.collision_checker.check_path(output, resolution=0.01, safety_distance=safety_distance)
        if final_check.in_collision:
            output = original
            shortcut_diagnostics.update(applied=False, fallback_to_original=True)
            if self.collision_checker.check_path(output, resolution=0.01, safety_distance=safety_distance).in_collision:
                raise PlanningError("Fixed-orientation path failed final collision validation.", code="TRAJECTORY_INVALID", recoverable=True)
        output, smoothing_diagnostics = self._smooth_fixed_orientation(
            output, orientation, safety_distance, move_request,
        )
        trajectory = {
            "planner": self.planner_id,
            "path_type": "cartesian_fixed_orientation",
            "original_path_type": "joint",
            "waypoints": output.tolist(),
            "planning_time": time.perf_counter() - started,
            "duration": float(np.sum(np.max(np.abs(np.diff(output, axis=0)), axis=1)) / max(float(move_request["constraints"]["velocity_scale"]), 0.01)),
            "diagnostics": {"algorithm": "cartesian_rrt_fixed_orientation", "orientation_fixed": True,
                            "shortcut": shortcut_diagnostics, "smoothing": smoothing_diagnostics},
        }
        return trajectory

    def _smooth_fixed_orientation(self, waypoints, orientation, safety_distance, request):
        """Smooth Cartesian positions while preserving the validated IK branch."""
        config = self.config.get("planning", {}).get("collision_free", {})
        seed = np.asarray(waypoints, dtype=float).copy()
        enabled = bool(config.get("optimize_path", True)) and bool(config.get("smooth_result", True))
        diagnostics = {"enabled": enabled, "applied": False, "accepted_updates": 0,
                       "fallback_to_simplified": False}
        if not enabled or len(seed) < 3:
            return seed, diagnostics
        points = seed.copy()
        positions = np.asarray([self.kinematics.forward(q)["position"] for q in points])
        cost = lambda p: float(np.sum(np.diff(p, n=2, axis=0) ** 2))
        length = lambda p: float(np.sum(np.linalg.norm(np.diff(p, axis=0), axis=1)))
        travel = lambda q: float(np.sum(np.abs(np.diff(q, axis=0))))
        initial_cost = current_cost = cost(positions)
        initial_length = current_length = length(positions)
        initial_travel = current_travel = travel(points)
        deadline = time.perf_counter() + max(0.01, float(config.get("fixed_orientation_smoothing_time", 0.5)))
        radius = max(1e-5, float(config.get("fixed_orientation_smoothing_step", 0.005)))
        limit = float(request["strategy_params"].get("max_joint_step", 0.4))
        for iteration in range(8):
            accepted = 0
            indices = range(1, len(points) - 1)
            if iteration % 2:
                indices = reversed(range(1, len(points) - 1))
            for index in indices:
                if time.perf_counter() >= deadline:
                    diagnostics["stopped_by_time_limit"] = True
                    break
                delta = 0.5 * (positions[index - 1] + positions[index + 1]) - positions[index]
                norm = float(np.linalg.norm(delta))
                if norm < 1e-7:
                    continue
                delta *= min(1.0, radius / norm)
                for scale in (1.0, 0.5, 0.25):
                    if time.perf_counter() >= deadline:
                        break
                    target = positions[index] + scale * delta
                    try:
                        solved = self.kinematics.solve_one(
                            {"position": target, "orientation": orientation}, seed=points[index],
                            backend=request["strategy_params"].get("ik_method", "auto"),
                            timeout=min(0.1, max(0.001, deadline - time.perf_counter())),
                            max_joint_step=limit,
                        )
                    except Exception:
                        continue
                    q = np.asarray(solved.solution, dtype=float)
                    if np.any(q < self.joint_limits[:, 0]) or np.any(q > self.joint_limits[:, 1]):
                        continue
                    local = np.vstack((points[index - 1], q, points[index + 1]))
                    if np.max(np.abs(np.diff(local, axis=0))) > limit:
                        continue
                    candidate_positions = positions.copy()
                    candidate_positions[index] = self.kinematics.forward(q)["position"]
                    candidate_cost = cost(candidate_positions)
                    candidate_length = length(candidate_positions)
                    candidate_points = points.copy()
                    candidate_points[index] = q
                    candidate_travel = travel(candidate_points)
                    if (candidate_cost >= current_cost - 1e-12
                            or candidate_length > current_length + 1e-8
                            or candidate_travel > current_travel + 1e-8):
                        continue
                    if self.collision_checker.check_path(local, resolution=0.01, safety_distance=safety_distance).in_collision:
                        continue
                    points, positions = candidate_points, candidate_positions
                    current_cost, current_length, current_travel = candidate_cost, candidate_length, candidate_travel
                    diagnostics["accepted_updates"] += 1
                    accepted += 1
                    break
            if not accepted or time.perf_counter() >= deadline:
                break
        final_collision = self.collision_checker.check_path(points, resolution=0.01, safety_distance=safety_distance)
        if final_collision.in_collision:
            diagnostics.update(fallback_to_simplified=True, reason="final_collision_check_failed")
            return seed, diagnostics
        diagnostics.update(
            applied=diagnostics["accepted_updates"] > 0,
            initial_smoothness_cost=initial_cost, final_smoothness_cost=current_cost,
            initial_cartesian_length_m=initial_length, final_cartesian_length_m=current_length,
            initial_joint_travel_rad=initial_travel, final_joint_travel_rad=current_travel,
        )
        return points, diagnostics

    def _shortcut_fixed_orientation(self, waypoints, orientation, step, safety_distance, request, rng):
        """Replace detours only with collision-free, continuous fixed-pose IK."""
        config = self.config.get("planning", {}).get("collision_free", {})
        points = np.asarray(waypoints, dtype=float).copy()
        enabled = bool(config.get("optimize_path", True))
        diagnostics = {"enabled": enabled, "applied": False, "accepted_shortcuts": 0,
                       "attempts": 0, "fallback_to_original": False}
        if not enabled or len(points) < 3:
            return points, diagnostics
        positions = np.asarray([self.kinematics.forward(q)["position"] for q in points])
        initial_length = float(np.sum(np.linalg.norm(np.diff(positions, axis=0), axis=1)))
        deadline = time.perf_counter() + max(0.01, float(config.get("fixed_orientation_shortcut_time", 0.5)))
        for iteration in range(120):
            if time.perf_counter() >= deadline or len(points) < 3:
                break
            first, second = (0, len(points) - 1) if iteration == 0 else sorted(rng.choice(len(points), size=2, replace=False).tolist())
            if second - first < 2:
                continue
            old_positions = positions[first:second + 1]
            old_length = float(np.sum(np.linalg.norm(np.diff(old_positions, axis=0), axis=1)))
            if np.linalg.norm(positions[second] - positions[first]) >= old_length - 1e-8:
                continue
            diagnostics["attempts"] += 1
            edge = self._fixed_orientation_edge(
                positions[first], points[first], positions[second], orientation,
                step, safety_distance, request, deadline=deadline,
            )
            if edge is None or not np.allclose(edge[-1], points[second], atol=1e-5, rtol=0):
                continue
            edge = np.asarray(edge)
            # Preserve the selected goal branch and the untouched suffix exactly.
            edge[-1] = points[second]
            old_travel = float(np.sum(np.abs(np.diff(points[first:second + 1], axis=0))))
            if float(np.sum(np.abs(np.diff(edge, axis=0)))) > old_travel + 1e-8:
                continue
            if self.collision_checker.check_path(edge, resolution=0.01, safety_distance=safety_distance).in_collision:
                continue
            edge_positions = np.asarray([self.kinematics.forward(q)["position"] for q in edge])
            points = np.vstack((points[:first], edge, points[second + 1:]))
            positions = np.vstack((positions[:first], edge_positions, positions[second + 1:]))
            diagnostics["accepted_shortcuts"] += 1
        diagnostics.update(
            applied=diagnostics["accepted_shortcuts"] > 0,
            initial_cartesian_length_m=initial_length,
            final_cartesian_length_m=float(np.sum(np.linalg.norm(np.diff(positions, axis=0), axis=1))),
        )
        return points, diagnostics

    def _fixed_orientation_edge(
        self,
        start_position: np.ndarray,
        seed: np.ndarray,
        goal_position: np.ndarray,
        orientation: Mapping[str, Any],
        step: float,
        safety_distance: float,
        move_request: Mapping[str, Any],
        *,
        deadline: float | None = None,
    ) -> list[np.ndarray] | None:
        count = max(1, int(np.ceil(np.linalg.norm(goal_position - start_position) / step)))
        result = [np.asarray(seed, dtype=float)]
        current = result[0]
        for alpha in np.linspace(0.0, 1.0, count + 1)[1:]:
            if deadline is not None and time.perf_counter() >= deadline:
                return None
            point = start_position + alpha * (goal_position - start_position)
            try:
                solved = self.kinematics.solve_one(
                    {"position": point, "orientation": orientation}, seed=current,
                    backend=move_request["strategy_params"].get("ik_method", "auto"),
                    timeout=min(0.25, max(0.001, deadline - time.perf_counter())) if deadline is not None else 0.25,
                    max_joint_step=float(move_request["strategy_params"].get("max_joint_step", 0.4)),
                )
            except Exception:
                return None
            candidate = np.asarray(solved.solution, dtype=float)
            if np.max(np.abs(candidate - current)) > float(move_request["strategy_params"].get("max_joint_step", 0.4)):
                return None
            if self.collision_checker.check_path([current, candidate], resolution=self.rrt_collision_resolution, safety_distance=safety_distance).in_collision:
                return None
            current = candidate
            result.append(current)
        return result

    def _rrt_connect(self, start: np.ndarray, goal: np.ndarray, deadline: float, rng: np.random.Generator, safety_distance: float) -> list[np.ndarray] | None:
        tree_a = _Tree(start)
        tree_b = _Tree(goal)
        swapped = False
        while time.perf_counter() < deadline:
            sample = goal if rng.random() < 0.15 else rng.uniform(self.joint_limits[:, 0], self.joint_limits[:, 1])
            new_index = self._extend_once(tree_a, sample, safety_distance)
            if new_index is not None:
                target = tree_a.nodes[new_index]
                connected_index = self._connect(tree_b, target, safety_distance)
                if connected_index is not None and np.linalg.norm(tree_b.nodes[connected_index] - target) < 1e-6:
                    path_a = tree_a.path(new_index)
                    path_b = tree_b.path(connected_index)
                    return path_a + list(reversed(path_b[:-1])) if not swapped else path_b + list(reversed(path_a[:-1]))
            tree_a, tree_b = tree_b, tree_a
            swapped = not swapped
        return None

    def _extend_once(self, tree: "_Tree", target: np.ndarray, safety_distance: float) -> int | None:
        distances = [np.linalg.norm(node - target) for node in tree.nodes]
        parent = int(np.argmin(distances))
        source = tree.nodes[parent]
        delta = target - source
        distance = float(np.linalg.norm(delta))
        if distance < 1e-9:
            return parent
        candidate = target.copy() if distance <= 0.16 else source + delta / distance * 0.16
        if self.collision_checker.check_path([source, candidate], resolution=self.rrt_collision_resolution, safety_distance=safety_distance).in_collision:
            return None
        return tree.add(candidate, parent)

    def _connect(self, tree: "_Tree", target: np.ndarray, safety_distance: float) -> int | None:
        last = None
        for _ in range(80):
            last = self._extend_once(tree, target, safety_distance)
            if last is None:
                return None
            if np.linalg.norm(tree.nodes[last] - target) < 1e-6:
                return last
        return None

    def _shortcut(self, path: list[np.ndarray], rng: np.random.Generator, safety_distance: float, *, iterations: int,
                  diagnostics: dict[str, Any] | None = None) -> list[np.ndarray]:
        """Simplify within a soft budget, reusing checks only during this call.

        The scene and checking policy must stay fixed during synchronous planning.
        Cache keys retain direction and exact endpoints; nothing is reused across
        calls, execution, or the independent final path validation. An in-flight
        collision check finishes even if it crosses the time budget.
        """
        config = self.config.get("planning", {}).get("collision_free", {})
        budget = max(0.0, float(config.get("shortcut_time", 0.5)))
        started = time.perf_counter()
        deadline = started + budget
        stats = diagnostics if diagnostics is not None else {}
        stats.update(time_budget_s=budget, checks=0, cache_hits=0, accepted_shortcuts=0,
                     stopped_by_time_limit=False)
        cache: dict[tuple[bytes, bytes], bool] = {}
        result = list(path)
        # Deterministic rope-style pass: try long-range connections first,
        # which is substantially more effective on RRT-Connect paths than
        # spending the whole budget on random adjacent choices.
        for gap in range(len(result) - 1, 1, -1):
            if time.perf_counter() >= deadline or len(result) < 3:
                stats["stopped_by_time_limit"] = time.perf_counter() >= deadline
                break
            changed = True
            while changed and len(result) >= 3:
                changed = False
                for first in range(0, len(result) - gap):
                    second = first + gap
                    if time.perf_counter() >= deadline:
                        stats["stopped_by_time_limit"] = True
                        break
                    key = (np.asarray(result[first], dtype=np.float64).tobytes(),
                           np.asarray(result[second], dtype=np.float64).tobytes())
                    if key not in cache:
                        cache[key] = self.collision_checker.check_path(
                            [result[first], result[second]], resolution=self.rrt_collision_resolution,
                            safety_distance=safety_distance).in_collision
                        stats["checks"] += 1
                    else:
                        stats["cache_hits"] += 1
                    if not cache[key]:
                        result = result[:first + 1] + result[second:]
                        stats["accepted_shortcuts"] += 1
                        changed = True
                        break
                if stats["stopped_by_time_limit"]:
                    break
        # Partial-shortcut pass: sample interior points so a shortcut can
        # remove a bend even when neither endpoint is an original RRT vertex.
        for _ in range(iterations):
            if len(result) < 3:
                break
            if time.perf_counter() >= deadline:
                stats["stopped_by_time_limit"] = True
                break
            first, second = sorted(rng.choice(len(result), size=2, replace=False).tolist())
            if second - first < 2:
                continue
            key = (np.asarray(result[first], dtype=np.float64).tobytes(),
                   np.asarray(result[second], dtype=np.float64).tobytes())
            if key in cache:
                in_collision = cache[key]
                stats["cache_hits"] += 1
            else:
                in_collision = self.collision_checker.check_path(
                    [result[first], result[second]], resolution=self.rrt_collision_resolution,
                    safety_distance=safety_distance,
                ).in_collision
                cache[key] = in_collision
                stats["checks"] += 1
            if not in_collision:
                result = result[:first + 1] + result[second:]
                stats["accepted_shortcuts"] += 1
        stats["elapsed_s"] = time.perf_counter() - started
        return result


class _Tree:
    def __init__(self, root: np.ndarray) -> None:
        self.nodes = [root.copy()]
        self.parents = [-1]

    def add(self, node: np.ndarray, parent: int) -> int:
        self.nodes.append(node.copy())
        self.parents.append(parent)
        return len(self.nodes) - 1

    def path(self, index: int) -> list[np.ndarray]:
        result = []
        while index >= 0:
            result.append(self.nodes[index])
            index = self.parents[index]
        return list(reversed(result))


def _densify(points: np.ndarray, max_step: float) -> np.ndarray:
    output = [points[0]]
    for start, goal in zip(points[:-1], points[1:]):
        count = max(1, int(np.ceil(np.max(np.abs(goal - start)) / max_step)))
        output.extend(start + alpha * (goal - start) for alpha in np.linspace(0.0, 1.0, count + 1)[1:])
    return np.asarray(output)

