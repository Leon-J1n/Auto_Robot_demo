"""Task-level retreat skill with the same pose-search contract as approach."""
from copy import deepcopy

import numpy as np
from scipy.spatial.transform import Rotation

from atomic_action.motion.common.interaction import planning_failed_without_motion
from planning import candidate_pose, run_candidate_search, search_distances


class RetreatSkill:
    MIN_DISTANCE = 0.03
    MAX_DISTANCE = 0.10
    STEP = 0.005
    DEFAULT_AXIS = "-z"

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
        if release_first and not e._release():
            return False
        try:
            measured = e.backend.get_end_effector_pose()
            position = np.asarray(measured["position"], dtype=float)
            rotation = np.asarray(measured["rotation_matrix"], dtype=float)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            return self._fail("INVALID_RETREAT_REQUEST", f"current end-effector pose unavailable: {exc}")
        if position.shape != (3,) or not np.all(np.isfinite(position)):
            return self._fail("INVALID_RETREAT_REQUEST", "current end-effector position must be a finite 3-vector")
        if rotation.shape != (3, 3) or not np.all(np.isfinite(rotation)):
            return self._fail("INVALID_RETREAT_REQUEST", "current end-effector rotation must be a finite 3x3 matrix")
        try:
            minimum, maximum, step = self._search_parameters(params)
            offset = params.get("grasp_offset_local")
            if offset is not None:
                offset = np.asarray(offset, dtype=float)
                if offset.shape != (3,) or not np.all(np.isfinite(offset)):
                    raise ValueError("grasp_offset_local must be a finite 3-vector")
                norm = float(np.linalg.norm(offset))
                if norm <= 1e-9:
                    raise ValueError("grasp_offset_local must be non-zero")
                local_axis = offset / norm
            else:
                local_axis = self._axis_vector(params.get("direction"))
            axis = rotation @ local_axis
        except (TypeError, ValueError) as exc:
            return self._fail("INVALID_RETREAT_REQUEST", str(exc))

        def attempt(distance):
            candidate_transform = self.candidate_pose(
                np.block([[rotation, position[:, None]],
                          [np.zeros((1, 3)), np.ones((1, 1))]]), axis, distance
            )
            before = e.backend.physics_steps
            moved = e._move(
                f"retreat_{int(round(float(distance) * 1000))}mm",
                position=candidate_transform[:3, 3], rotation=rotation,
                linear=True, contact=True,
                override={
                    "motion": {"path_type": "linear", "path_constraint": "hard"},
                    "planning": {"mode": "direct", "allow_replan": False},
                    "constraints": {
                        "safety_distance": 0.003,
                        "keep_end_effector_orientation": True,
                    },
                    "strategy_params": {"max_joint_step": 0.8},
                })
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


