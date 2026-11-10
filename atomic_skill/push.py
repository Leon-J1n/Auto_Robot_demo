"""Push a held object using the same motion-and-release flow as pull."""
from atomic_skill.pull import PullMotionExecutor


class PushMotionExecutor(PullMotionExecutor):
    operation = 'push'

    def _run(self):
        if self.params.get('contact_mode', 'grasp') != 'grasp':
            return self._fail('INVALID_REQUEST', 'Push requires contact_mode grasp')
        return self._execute_grasped_motion('push')


class CommandPushExecutor(PushMotionExecutor):
    """Check holding, execute a linear/circular action, then release."""
