"""Standalone EAIK service constructed from the common URDF chain loader."""
from __future__ import annotations
from pathlib import Path
from kinematics import KinematicsService, SolverRegistry
from libraries.kinematic_chain.urdf import UrdfRobotModel


def load_urdf(path: str | Path, *, tip_link: str, base_link: str | None = None,
              tcp_transform=None, world_from_base=None, initial_joints=None,
              continuous_limits=None, current_joints_provider=None,
              base_transform_provider=None, validation_backend: str = "auto") -> KinematicsService:
    """Load and validate a selected URDF chain; return the uniform IK service.

    ``validation_backend`` selects the registry backend used for the initial
    chain validation (``auto`` uses the configured fallback chain).  Keeping
    this option at the loader boundary lets callers select the same validation
    policy as the planner without silently dropping it during integration.

    This standalone service defaults to the configured automatic chain. It calculates kinematics only and
    does not assemble a simulator, collision checker or robot motion executor.
    """
    robot = UrdfRobotModel(path, tip_link=tip_link, base_link=base_link,
                          tcp_transform=tcp_transform, world_from_base=world_from_base,
                          initial_joints=initial_joints, continuous_limits=continuous_limits,
                          current_joints_provider=current_joints_provider,
                          base_transform_provider=base_transform_provider)
    # Use the project registry so ``auto`` keeps the configured fallback chain:
    # EAIK first, followed by registered certified/numerical fallbacks.
    registry = SolverRegistry.discover()
    service = KinematicsService(robot, registry)
    # Initialize and verify through the same public API used by callers.
    # Some valid chains (including Piper) have no EAIK decomposition. Verify
    # initialization through the same fallback chain used for task planning.
    if not isinstance(validation_backend, str) or not validation_backend:
        raise ValueError("validation_backend must be a non-empty backend name")
    service.solve_one(robot.forward(robot.current_joints()), backend=validation_backend, timeout=10.)
    return service
