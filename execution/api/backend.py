"""Backend contract and lazy backend factory."""

from typing import Callable, Protocol


class ExecutionBackend(Protocol):
    """Runtime surface consumed by motion actions."""

    def load(self, resources, output_dir, **kwargs): ...
    def get_state(self) -> dict: ...
    @property
    def joint_limits(self): ...
    def get_joint_positions(self): ...
    def get_end_effector_pose(self) -> dict: ...
    def get_context(self, request=None) -> dict: ...
    def set_frame_callback(self, callback: Callable[[dict], None] | None): ...
    def execute_trajectory(self, trajectory, *, timeout=30.0) -> dict: ...
    def open_viewer(self): ...
    def set_status(self, status: str): ...
    def close(self): ...


class ExecutionBackendRegistry:
    """Create a named runtime backend without importing simulator SDKs early."""

    def __init__(self):
        self._factories: dict[str, Callable] = {}

    def register(self, name, factory):
        if name in self._factories:
            raise ValueError(f"Execution backend already registered: {name}")
        self._factories[name] = factory

    def create(self, name):
        if name == "mujoco" and name not in self._factories:
            from ..backends.mujoco.backend import MuJoCoExecutionBackend

            return MuJoCoExecutionBackend()
        if name not in self._factories:
            raise ValueError(f"Unknown execution backend: {name}")
        return self._factories[name]()
