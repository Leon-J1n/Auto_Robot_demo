"""Synchronize authoritative backend state into planning-side models."""
from contextlib import contextmanager
import numpy as np

class SceneStateSynchronizer:
    """Execution is authoritative after initialization; never teleport it from planning."""
    def __init__(self, blackboard=None):
        from execution.runtime.state import TargetBlackboard
        self.blackboard = blackboard if blackboard is not None else TargetBlackboard()
        self._poses = {}
        self._joint_states = {}
        self.revision = 0
        self.last_changed_objects = ()

    @contextmanager
    def command_state(self, backend, kin, checker):
        """Refresh every command, including failed or newly added skills."""
        state = self.sync(backend, kin, checker)
        try:
            yield state
        finally:
            # Observe the actual endpoint even when execution raises midway.
            # Collision synchronization is deferred to the next planning step.
            self.blackboard.observe(backend.get_state())

    def sync(self, backend, kin, checker):
        state = backend.get_state()
        if self.blackboard is not None:
            self.blackboard.observe(state)
        checker_key = id(checker)
        joints = np.asarray(state['joints'], dtype=float)
        previous_joints = self._joint_states.get(checker_key)
        joints_changed = (previous_joints is None or
                          not np.allclose(previous_joints, joints, atol=1e-9, rtol=0))
        if joints_changed:
            kin.robot.set_current_joints(joints)
            self._joint_states[checker_key] = joints.copy()
        if not np.allclose(state['base_transform'], kin.robot.base_transform(state['joints']), atol=1e-9):
            raise ValueError('Moving robot bases are not supported in this release')
        changed = []
        for name, pose in state['objects'].items():
            key = (checker, name)
            previous = self._poses.get(key)
            # Settled physics bodies retain sub-micrometre solver jitter. Avoid
            # rebuilding native collision transforms for invisible changes;
            # compare to the last transmitted pose, so movement accumulates.
            unchanged = previous is not None and (
                np.allclose(previous['position'], pose['position'], atol=1e-7, rtol=0)
                and abs(float(np.dot(previous['quaternion_xyzw'], pose['quaternion_xyzw']))) >= 1 - 1e-14)
            if not unchanged:
                checker.set_entity_transform(name, **pose)
                self._poses[key] = {
                    'position': list(pose['position']),
                    'quaternion_xyzw': list(pose['quaternion_xyzw']),
                }
                changed.append(name)
        if changed or joints_changed:
            self.revision += 1
        self.last_changed_objects = tuple(changed)
        hook = getattr(checker, 'on_scene_sync', None)
        if callable(hook):
            hook(state, changed_objects=tuple(changed), revision=self.revision)
        return state
