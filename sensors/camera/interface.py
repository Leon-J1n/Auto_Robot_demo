"""Backend-independent camera contract."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .frame import CameraFrame


class Camera(ABC):
    """A camera capable of returning its latest complete observation."""

    @property
    @abstractmethod
    def camera_id(self) -> str:
        """Stable logical camera identifier."""

    @abstractmethod
    def get_current_frame(self) -> CameraFrame:
        """Capture and return a synchronized RGB-D frame."""

    def close(self) -> None:
        """Release backend resources, if any."""

