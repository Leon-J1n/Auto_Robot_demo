"""Standalone EAIK service constructed from the common URDF chain loader."""
from __future__ import annotations
from pathlib import Path
from kinematics import KinematicsService, SolverRegistry
from libraries.kinematic_chain.urdf import UrdfRobotModel


def load_urdf(path: str | Path, *, tip_link: str, base_link: str | None = None,
              tcp_transform=None, world_from_base=None, initial_joints=None,
              continuous_limits=None, current_joints_provider=None,
              base_transform_provider=None) -> KinematicsService:
    """Load and validate a selected URDF chain; return the uniform IK service.

    This standalone service defaults to EAIK. It calculates kinematics only and
    does not assemble a simulator, collision checker or robot motion executor.
    """
    robot = UrdfRobotModel(path, tip_link=tip_link, base_link=base_link,
                          tcp_transform=tcp_transform, world_from_base=world_from_base,
                          initial_joints=initial_joints, continuous_limits=continuous_limits,
                          current_joints_provider=current_joints_provider,
                          base_transform_provider=base_transform_provider)
    # Use the project registry so ``auto`` keeps the configured fallback chain:
    # EAIK first, then the independent Jacobian solver.
    registry = SolverRegistry.discover()
    service = KinematicsService(robot, registry)
    # Initialize and verify through the same public API used by callers.
    service.solve_one(robot.forward(robot.current_joints()), backend="eaik")
    return service
