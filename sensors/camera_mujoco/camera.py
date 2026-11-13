"""Capture synchronized RGB and metric-depth frames from a MuJoCo scene.

The camera is intentionally pull-based: it never advances the simulation.  A
caller can therefore construct one once and call ``get_current_frame()`` at
any point after changing ``MjData`` (and after ``mj_forward`` when state was
edited directly).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..camera import Camera, CameraFrame, CameraIntrinsics, CameraPose


class MujocoCamera(Camera):
    """MuJoCo renderer-backed RGB-D camera.

    Args:
        model: Loaded ``mujoco.MjModel`` containing the camera.
        data: The matching ``mujoco.MjData`` instance. It is read at capture
            time, so simulation changes are reflected in the next frame.
        camera_name: Name from the MJCF ``<camera name=...>`` element. The
            integer camera id may also be supplied.
        width, height: Output image dimensions in pixels.
        camera_id: Optional stable identifier returned in ``CameraFrame``.
    """

    def __init__(
        self,
        model: Any,
        data: Any,
        camera_name: str | int,
        *,
        width: int = 640,
        height: int = 480,
        camera_id: str | None = None,
        renderer: Any = None,
    ) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("Camera width and height must be positive.")
        try:
            import mujoco
        except ImportError as exc:  # pragma: no cover - depends on optional backend
            raise ImportError("MujocoCamera requires the 'mujoco' package.") from exc

        self.model = model
        self.data = data
        self._mujoco = mujoco
        if isinstance(camera_name, str):
            self._camera_id = int(mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, camera_name))
            if self._camera_id < 0:
                raise ValueError(f"MuJoCo camera not found: {camera_name}")
            self._camera_name = camera_name
        else:
            self._camera_id = int(camera_name)
            if not 0 <= self._camera_id < int(model.ncam):
                raise ValueError(f"MuJoCo camera id out of range: {self._camera_id}")
            self._camera_name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_CAMERA, self._camera_id) or str(camera_name)
        self._camera_identifier = camera_id or self._camera_name
        self.width = int(width)
        self.height = int(height)
        self._owns_renderer = renderer is None
        self._renderer = renderer or mujoco.Renderer(model, height=self.height, width=self.width)
        self._sequence = 0
        self._closed = False

    @property
    def camera_id(self) -> str:
        return self._camera_identifier

    def get_current_frame(self) -> CameraFrame:
        """Render the current simulation state without stepping it."""
        if self._closed:
            raise RuntimeError("MuJoCo camera is closed.")
        # update_scene reads the current data state and does not mutate time.
        self._renderer.update_scene(self.data, camera=self._camera_name)
        rgb = np.asarray(self._renderer.render(), dtype=np.uint8).copy()
        self._renderer.enable_depth_rendering()
        try:
            self._renderer.update_scene(self.data, camera=self._camera_name)
            # MuJoCo Renderer already converts the depth buffer to meters.
            depth = np.asarray(self._renderer.render(), dtype=np.float32).copy()
        finally:
            self._renderer.disable_depth_rendering()
        far = float(self.model.vis.map.zfar * self.model.stat.extent)
        depth[depth >= far * 0.999] = np.nan
        intrinsics = self._intrinsics()
        pose = CameraPose(
            position=np.asarray(self.data.cam_xpos[self._camera_id], dtype=float).copy(),
            rotation_matrix=np.asarray(self.data.cam_xmat[self._camera_id], dtype=float).reshape(3, 3).copy(),
            quaternion_wxyz=self._camera_quaternion().copy(),
        )
        frame = CameraFrame(
            camera_id=self.camera_id,
            timestamp=float(self.data.time),
            sequence=self._sequence,
            rgb=rgb,
            depth=depth,
            intrinsics=intrinsics,
            camera_pose=pose,
        )
        self._sequence += 1
        return frame

    def _intrinsics(self) -> CameraIntrinsics:
        fovy = float(self.model.cam_fovy[self._camera_id])
        fy = (self.height / 2.0) / np.tan(np.deg2rad(fovy) / 2.0)
        return CameraIntrinsics(self.width, self.height, float(fy), float(fy), (self.width - 1) / 2.0, (self.height - 1) / 2.0)

    def _camera_quaternion(self) -> np.ndarray:
        quat = np.empty(4, dtype=float)
        self._mujoco.mju_mat2Quat(quat, np.asarray(self.data.cam_xmat[self._camera_id]).reshape(-1))
        return quat

    def close(self) -> None:
        if not self._closed:
            if self._owns_renderer:
                self._renderer.close()
            self._closed = True
