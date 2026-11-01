"""Sensor abstractions shared by simulation and real hardware."""

from .camera import Camera, CameraFrame, CameraIntrinsics, CameraManager, CameraPose

__all__ = ["Camera", "CameraFrame", "CameraIntrinsics", "CameraManager", "CameraPose"]
