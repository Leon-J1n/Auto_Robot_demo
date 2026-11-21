"""Solver-independent URDF service entry point for execution and planning."""
from kinematics import KinematicsService
from .urdf import UrdfRobotModel


def load_urdf(path, *, tip_link, base_link=None, **model_options):
    robot = UrdfRobotModel(path, tip_link=tip_link, base_link=base_link, **model_options)
    service = KinematicsService(robot)
    service.automatic_backends()
    return service
