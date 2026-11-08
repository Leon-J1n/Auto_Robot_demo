"""Task-level approach skill with a minimal public request."""
from copy import deepcopy

import numpy as np
from scipy.spatial.transform import Rotation

from atomic_action.motion.common.interaction import planning_failed_without_motion
from atomic_action.motion.common.interaction import pose_request
from demo.task_commands.converter import _deep_merge
from planning import candidate_distances, candidate_pose, run_candidate_search, search_distances


class PoseSearchExecutor:
    """Minimal execution context shared by standalone approach/retreat calls."""

    def __init__(self, *, resources, backend, synchronizer, kinematics, checker, execute_motion):
        self.resources, self.backend = resources, backend
        self.sync, self.kinematics, self.checker = synchronizer, kinematics, checker
        self.execute_motion = execute_motion
        self.params = {}
        self.phases = []
        self.phase = 'pose_search'

    def _fail(self, code, message, **details):
        result = {'success': False, 'status': 'error',
                  'error': {'error_code': code, 'failed_stage': self.phase,
                            'error_message': message, 'details': details}}
        self.phases.append({'phase': self.phase, **result})
        return False

    def _move(self, name, position=None, rotation=None, *, spec=None, linear=False,
              contact=False, override=None, **_kwargs):
        self.phase = name
        if spec is not None:
            pose = spec.get('mechanism_pose', spec)
            position = np.asarray(pose['position'], dtype=float)
            rotation = self._pose_rotation(pose)
        position = np.asarray(position, dtype=float)
        rotation = np.asarray(rotation, dtype=float)
        transform = np.eye(4)
        transform[:3, :3], transform[:3, 3] = rotation, position
        request = _deep_merge(self.resources.scene.get('move_defaults', {}), override or {})
        request['target'] = pose_request(transform)
        request.setdefault('motion', {}).update(
            path_type='linear' if linear else self.params.get('planning_method', 'joint'),
            path_constraint='hard' if linear else 'soft',
        )
        if linear:
            request.setdefault('planning', {}).update(mode='direct', allow_replan=False)
        self.sync.sync(self.backend, self.kinematics, self.checker)
        result = self.execute_motion(request)
        self.phases.append({'phase': name, 'request': request, **result})
        self.sync.sync(self.backend, self.kinematics, self.checker)
        return bool(result.get('success'))

    @staticmethod
    def _pose_rotation(pose):
        if 'rotation_matrix' in pose:
            return np.asarray(pose['rotation_matrix'], dtype=float)
        if 'quaternion_xyzw' in pose:
            return Rotation.from_quat(np.asarray(pose['quaternion_xyzw'], dtype=float)).as_matrix()
        orientation = pose.get('orientation')
        if isinstance(orientation, dict):
            return Rotation.from_quat([orientation[key] for key in 'xyzw']).as_matrix()
        if 'rpy' in pose:
            return Rotation.from_euler('xyz', pose['rpy']).as_matrix()
        return np.eye(3)

    def run(self, skill, params):
        self.params = deepcopy(params)
        self.phases = []
        self.phase = skill
        from atomic_skill.approach import ApproachSkill
        from atomic_skill.retreat import RetreatSkill
        operation = ApproachSkill(self) if skill == 'approach' else RetreatSkill(self)
        success = operation.execute(params=self.params)
        failed = next((item for item in reversed(self.phases) if not item.get('success')), None)
        return {'success': bool(success), 'status': 'success' if success else 'error',
                'error': None if success else (failed or {}).get('error'),
                'substeps': self.phases}


class ApproachSkill:
    MIN_DISTANCE = 0.03
    MAX_DISTANCE = 0.10
    STEP = 0.005
    DEFAULT_AXIS = "+z"

    @staticmethod
    def candidate_pose(transform, axis_world, distance):
        """Generate the pre-contact pose used by approach and pick."""
        return candidate_pose(transform, axis_world, distance)

    def __init__(self, executor):
        self.executor = executor

    @staticmethod
    def _axis_vector(axis):
        if axis is None:
            axis = ApproachSkill.DEFAULT_AXIS
        if isinstance(axis, str):
            value = axis.strip().lower()
            aliases = {"x": "+x", "y": "+y", "z": "+z"}
            value = aliases.get(value, value)
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
        distances = search_distances(params, minimum=ApproachSkill.MIN_DISTANCE,
                                     maximum=ApproachSkill.MAX_DISTANCE,
                                     step=ApproachSkill.STEP, label='approach')
        return float(distances[-1]), float(distances[0]), float(distances[0] - distances[1]) if len(distances) > 1 else ApproachSkill.STEP

    def _fail(self, code, message):
        fail = getattr(self.executor, "_fail", None)
        return fail(code, message) if callable(fail) else False

    def execute(self, operation=None, *, params=None):
        e = self.executor
        params = e.params if params is None else params
        target_pose = params.get("target_pose")
        if not isinstance(target_pose, dict):
            return self._fail("INVALID_APPROACH_REQUEST", "target_pose is required")
        pose = deepcopy(target_pose.get("mechanism_pose", target_pose))
        if "position" not in pose:
            return self._fail("INVALID_APPROACH_REQUEST", "target_pose.position is required")
        position = np.asarray(pose["position"], dtype=float)
        if position.shape != (3,) or not np.all(np.isfinite(position)):
            return self._fail("INVALID_APPROACH_REQUEST", "target_pose.position must be a finite 3-vector")
        try:
            minimum, maximum, step = self._search_parameters(params)
            rotation = self._pose_rotation(pose)
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
                local_axis = self._axis_vector(params.get("pose_axis"))
            axis = rotation @ local_axis
        except (TypeError, ValueError) as exc:
            return self._fail("INVALID_APPROACH_REQUEST", str(exc))

        operation = operation or getattr(e, "operation", None) or "approach"
        def attempt(distance):
            # Derive a complete pre-grasp transform from the final grasp pose.
            # The rotation is part of the candidate: IK and collision planning
            # must validate the gripper orientation together with its position.
            candidate_transform = self.candidate_pose(
                np.block([[rotation, position[:, None]],
                          [np.zeros((1, 3)), np.ones((1, 1))]]), axis, distance
            )
            candidate_pose = deepcopy(pose)
            candidate_pose["position"] = candidate_transform[:3, 3].tolist()
            candidate_pose["rotation_matrix"] = candidate_transform[:3, :3].tolist()
            before = e.backend.physics_steps
            if e._move(
                    f"{operation}_approach_{int(round(float(distance) * 1000))}mm",
                    spec={"mechanism_pose": candidate_pose}, contact=False,
                    override={"motion": {"path_type": "joint", "path_constraint": "soft"},
                              "planning": {"mode": "collision_free", "allow_replan": True}}):
                return True
            failed = e.phases[-1]
            if not planning_failed_without_motion(failed, e.backend, before):
                return False
            return None
        return run_candidate_search(
            search_distances(params, minimum=minimum, maximum=maximum, step=step, label='approach'),
            attempt)




