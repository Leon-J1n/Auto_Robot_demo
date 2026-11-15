"""Small orchestration boundary for executing an already planned trajectory."""


class TrajectoryExecutor:
    """Delegate trajectory execution to the selected backend.

    Planning and collision validation happen before this object is called. It
    deliberately does not inspect or alter the trajectory for collision safety.
    """

    def __init__(self, backend):
        self.backend = backend

    def execute(self, trajectory, *, timeout=30.0):
        return self.backend.execute_trajectory(trajectory, timeout=timeout)
