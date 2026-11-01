"""Task-level retreat skill with the same pose-search contract as approach."""
from copy import deepcopy

import numpy as np
from scipy.spatial.transform import Rotation

from demo.task_skills.move import planning_failed_without_motion
from planning import candidate_pose, run_candidate_search, search_distances


class RetreatSkill:
    MIN_DISTANCE = 0.03
    MAX_DISTANCE = 0.10
    STEP = 0.005
    DEFAULT_AXIS = "+z"

    @staticmethod
    def candidate_pose(transform, axis_world, distance):
        """Generate the reverse-derived pose used by retreat and pick."""
        return candidate_pose(transform, axis_world, distance)

    def __init__(self, executor):
        self.executor = executor

    @staticmethod
    def _axis_vector(axis):
        if axis is None:
            axis = RetreatSkill.DEFAULT_AXIS
        if isinstance(axis, str):
            value = axis.strip().lower()
            value = {"x": "+x", "y": "+y", "z": "+z"}.get(value, value)
            if value not in {"+x", "-x", "+y", "-y", "+z", "-z"}:
                raise ValueError("pose_axis must be one of +x, -x, +y, -y, +z, -z")
            vector = np.zeros(3, dtype=float)
            vector["xyz".index(value[1])] = 1.0 if value[0] == "+" else -1.0
            return vector
        vector = np.asarray(axis, dtype=float)
        if vector.shape != (3,) or not np.all(np.isfinite(vector)):
            raise ValueError("pose_axis must be a finite 3-vector or a signed axis name")
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-9:
            raise ValueError("pose_axis must be non-zero")
        return vector / norm

    @staticmethod
    def _pose_rotation(pose):
        if "quaternion_xyzw" in pose:
            quaternion = pose["quaternion_xyzw"]
        elif "orientation" in pose:
            orientation = pose["orientation"]
            quaternion = [orientation[key] for key in "xyzw"] if isinstance(orientation, dict) else orientation
        elif "rpy" in pose:
            rpy = np.asarray(pose["rpy"], dtype=float)
            if rpy.shape != (3,) or not np.all(np.isfinite(rpy)):
                raise ValueError("target_pose.rpy must be a finite 3-vector")
            return Rotation.from_euler("xyz", rpy).as_matrix()
        else:
            return np.eye(3)
        quaternion = np.asarray(quaternion, dtype=float)
        if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
            raise ValueError("target_pose orientation must be a finite 4-vector")
        return Rotation.from_quat(quaternion).as_matrix()

    @staticmethod
    def _search_parameters(params):
        distances = search_distances(params, minimum=RetreatSkill.MIN_DISTANCE,
                                     maximum=RetreatSkill.MAX_DISTANCE,
                                     step=RetreatSkill.STEP, label='retreat')
        return float(distances[-1]), float(distances[0]), float(distances[0] - distances[1]) if len(distances) > 1 else RetreatSkill.STEP

    def _fail(self, code, message):
        fail = getattr(self.executor, "_fail", None)
        return fail(code, message) if callable(fail) else False

    def execute(self, params=None, *, release_first=False):
        e = self.executor
        params = e.params if params is None else params
        if not isinstance(params, dict):
            return self._fail("INVALID_RETREAT_REQUEST", "retreat parameters must be an object")
        target_pose = params.get("target_pose")
        if not isinstance(target_pose, dict):
            return self._fail("INVALID_RETREAT_REQUEST", "target_pose is required")
        pose = deepcopy(target_pose.get("mechanism_pose", target_pose))
        if "position" not in pose:
            return self._fail("INVALID_RETREAT_REQUEST", "target_pose.position is required")
        position = np.asarray(pose["position"], dtype=float)
        if position.shape != (3,) or not np.all(np.isfinite(position)):
            return self._fail("INVALID_RETREAT_REQUEST", "target_pose.position must be a finite 3-vector")
        try:
            minimum, maximum, step = self._search_parameters(params)
            axis = self._pose_rotation(pose) @ self._axis_vector(params.get("pose_axis"))
        except (TypeError, ValueError) as exc:
            return self._fail("INVALID_RETREAT_REQUEST", str(exc))

        if release_first and not e._release():
            return False
        world_pose = pose.get("frame") == "world" or target_pose.get("frame") == "world"
        rotation = self._pose_rotation(pose)
        def attempt(distance):
            candidate_pose = deepcopy(pose)
            # Retreat reverses the execution direction of approach: the
            # interaction pose is the start, and the reverse-derived pose is
            # the endpoint.
            candidate_pose["position"] = self.candidate_pose(
                np.block([[rotation, position[:, None]],
                          [np.zeros((1, 3)), np.ones((1, 1))]]), axis, distance
            )[:3, 3].tolist()
            before = e.backend.physics_steps
            if world_pose:
                moved = e._move(
                    f"retreat_{int(round(float(distance) * 1000))}mm",
                    position=np.asarray(candidate_pose["position"], dtype=float),
                    rotation=rotation, linear=True, contact=True,
                    override={"constraints": {"safety_distance": 0.003}})
            else:
                candidate = {
                    "mechanism_pose": candidate_pose,
                    "request": {"constraints": {"safety_distance": 0.003}},
                }
                moved = e._move(
                    f"retreat_{int(round(float(distance) * 1000))}mm",
                    spec=candidate, contact=True)
            if moved:
                clearance = getattr(e, "_retreat_clearance_met", None)
                if not callable(clearance) or clearance():
                    return True
                return None
            failed = e.phases[-1]
            if not planning_failed_without_motion(failed, e.backend, before):
                return False
            return None
        return run_candidate_search(
            search_distances(params, minimum=minimum, maximum=maximum, step=step, label='retreat'),
            attempt)
