"""Standalone URDF entry point using the common robot model and service."""
from kinematics import KinematicsService
from libraries.kinematic_chain.urdf import UrdfRobotModel


def load_urdf(path, *, tip_link, base_link=None, **model_options):
    """Create a service and verify SSIK against the selected URDF chain."""
    robot = UrdfRobotModel(path, tip_link=tip_link, base_link=base_link, **model_options)
    service = KinematicsService(robot)
    service.solve_one(robot.forward(robot.current_joints()), backend="ssik", timeout=30)
    return service
