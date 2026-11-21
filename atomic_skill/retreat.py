"""Task-level retreat skill with the same pose-search contract as approach."""
from contextlib import ExitStack, contextmanager

import numpy as np
from scipy.spatial.transform import Rotation

from atomic_action.motion.common.interaction import planning_failed_without_motion
from planning import candidate_pose, run_candidate_search, search_distances
from atomic_skill.pose_precheck import ordered_distances


class RetreatSkill:
    MIN_DISTANCE = 0.01
    MAX_DISTANCE = 0.10
    STEP = 0.005
    DEFAULT_AXIS = "-z"
    DEFAULT_LIFT_DISTANCE = 0.01
    Z_PARALLEL_ANGLE_DEG = 15.0

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

    @contextmanager
    def _initial_clearance(self):
        """Permit existing contact to separate, without ignoring new obstacles."""
        e = self.executor
        checker = getattr(e, 'checker', None)
        capture = getattr(checker, 'capture_lift_clearance', None)
        clear = getattr(checker, 'clear_lift_clearance', None)
        with ExitStack() as cleanup:
            if callable(capture) and callable(clear):
                cleanup.callback(clear)
                margin = float(e.resources.scene.get('move_defaults', {})
                               .get('constraints', {}).get('safety_distance', .003))
                capture(margin)
            yield

    def _lift(self, position, rotation, distance):
        """Apply the former lift_up collision policy only during the lift."""
        e = self.executor
        checker = getattr(e, 'checker', None)
        with ExitStack() as cleanup:
            scope = getattr(checker, 'lift_payload_collision_scope', None)
            if callable(scope):
                cleanup.enter_context(scope())
            capture = getattr(checker, 'capture_lift_clearance', None)
            clear = getattr(checker, 'clear_lift_clearance', None)
            if callable(capture) and callable(clear):
                cleanup.callback(clear)
                margin = float(e.resources.scene.get('move_defaults', {})
                               .get('constraints', {}).get('safety_distance', .003))
                capture(margin)
            lifted = e._move(
                'lift_up', position=position + np.array([0., 0., distance]),
                rotation=rotation, linear=True,
                override={
                    'motion': {'path_type': 'linear', 'path_constraint': 'hard'},
                    'planning': {'mode': 'direct', 'allow_replan': False},
                    'constraints': {'avoid_collision': True,
                                    'keep_end_effector_orientation': True},
                })
        if lifted and hasattr(checker, 'support_geoms'):
            checker.support_geoms.clear()
        return lifted

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
            lift_distance = float(params.get('lift_distance', self.DEFAULT_LIFT_DISTANCE))
            if not np.isfinite(lift_distance) or lift_distance < 0:
                raise ValueError('lift_distance must be a finite nonnegative number in metres')
            reference_rotation = np.asarray(params.get('reference_rotation_world', rotation), dtype=float)
            if reference_rotation.shape != (3, 3) or not np.all(np.isfinite(reference_rotation)):
                raise ValueError('reference_rotation_world must be a finite 3x3 matrix')
            offset = params.get("grasp_offset_local")
            if 'pose_axis' in params or 'direction' in params:
                local_axis = self._axis_vector(params.get('pose_axis', params.get('direction')))
            elif offset is not None:
                offset = np.asarray(offset, dtype=float)
                if offset.shape != (3,) or not np.all(np.isfinite(offset)):
                    raise ValueError("grasp_offset_local must be a finite 3-vector")
                norm = float(np.linalg.norm(offset))
                if norm <= 1e-9:
                    raise ValueError("grasp_offset_local must be non-zero")
                local_axis = offset / norm
            else:
                local_axis = self._axis_vector(params.get("direction"))
            # candidate_pose subtracts the approach axis. The actual withdrawal
            # direction is its negative, expressed in the original interaction frame.
            axis = reference_rotation @ local_axis
            axis_norm = float(np.linalg.norm(axis))
            if axis_norm <= 1e-9:
                raise ValueError('retreat direction must be non-zero')
            near_z = abs(float(axis[2])) / axis_norm >= np.cos(
                np.deg2rad(self.Z_PARALLEL_ANGLE_DEG)) - 1e-12
        except (TypeError, ValueError) as exc:
            return self._fail("INVALID_RETREAT_REQUEST", str(exc))

        if lift_distance > 0 and not near_z:
            if not self._lift(position, rotation, lift_distance):
                return False
            # Use the measured lift endpoint, including tracking error, as the
            # origin for every retreat candidate; never repeat lift on retry.
            try:
                measured = e.backend.get_end_effector_pose()
                position = np.asarray(measured['position'], dtype=float)
                rotation = np.asarray(measured['rotation_matrix'], dtype=float)
                if (position.shape != (3,) or not np.all(np.isfinite(position))
                        or rotation.shape != (3, 3) or not np.all(np.isfinite(rotation))):
                    raise ValueError('invalid measured end-effector pose after lift')
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                return self._fail('INVALID_RETREAT_REQUEST', str(exc))

        def transform_for(distance):
            return self.candidate_pose(
                np.block([[rotation, position[:, None]],
                          [np.zeros((1, 3)), np.ones((1, 1))]]), axis, distance
            )

        def attempt(distance):
            candidate_transform = transform_for(distance)
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
                e.successful_distance_m = float(distance)
                return True
            failed = e.phases[-1]
            if not planning_failed_without_motion(failed, e.backend, before):
                return False
            return None
        # An upward retreat is already the lift. Keep the initial contact
        # bounds for this motion even though no separate lift_up was requested.
        # Downward withdrawals must retain the ordinary collision thresholds.
        with ExitStack() as cleanup:
            if near_z and axis[2] < 0:
                cleanup.enter_context(self._initial_clearance())
            success = run_candidate_search(
                ordered_distances(e,
                    search_distances(params, minimum=minimum, maximum=maximum, step=step, label='retreat'),
                    transform_for, preferred=params.get('_preferred_distance_m')),
                attempt)
        if success and near_z and axis[2] < 0 and hasattr(getattr(e, 'checker', None), 'support_geoms'):
            e.checker.support_geoms.clear()
        return success


