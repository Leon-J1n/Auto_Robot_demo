"""Registry for cameras exposed by a robot runtime."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from .frame import CameraFrame
from .interface import Camera


class CameraManager(Mapping[str, Camera]):
    def __init__(self, cameras: Mapping[str, Camera]) -> None:
        if not cameras:
            raise ValueError("At least one camera is required.")
        self._cameras = dict(cameras)

    def __getitem__(self, camera_id: str) -> Camera:
        try:
            return self._cameras[camera_id]
        except KeyError as exc:
            available = ", ".join(sorted(self._cameras))
            raise KeyError(
                f"Unknown camera '{camera_id}'. Available cameras: {available}"
            ) from exc

    def __iter__(self) -> Iterator[str]:
        return iter(self._cameras)

    def __len__(self) -> int:
        return len(self._cameras)

    def get_current_frame(self, camera_id: str) -> CameraFrame:
        return self[camera_id].get_current_frame()

    def close(self) -> None:
        for camera in self._cameras.values():
            camera.close()
