"""Dependency-free persistence for RGB-D camera frames."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

from .frame import CameraFrame


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _write_png(path: Path, image: np.ndarray, *, bit_depth: int, color_type: int) -> None:
    """Write an 8-bit RGB or 16-bit grayscale PNG without Pillow/OpenCV."""
    image = np.ascontiguousarray(image)
    height, width = image.shape[:2]
    channels = 3 if color_type == 2 else 1
    row_bytes = width * channels * (bit_depth // 8)
    flat = image.reshape(-1)
    raw = b"".join(
        b"\x00"
        + flat[
            row * row_bytes // image.itemsize : (row + 1) * row_bytes // image.itemsize
        ].tobytes()
        for row in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(payload)


def save_camera_frame(
    frame: CameraFrame,
    output_dir: str | Path,
    *,
    prefix: str | None = None,
) -> dict[str, Path]:
    """Save RGB, viewable depth, and exact metric depth for one frame.

    The depth PNG stores millimetres as unsigned 16-bit values. The NPY file
    stores the original float32 depth in metres without quantization.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    stem = prefix or f"{frame.camera_id}_{frame.sequence:06d}"
    rgb = np.asarray(frame.rgb, dtype=np.uint8)
    depth_mm = np.clip(
        np.nan_to_num(frame.depth, nan=0.0, posinf=65535.0, neginf=0.0) * 1000.0,
        0.0,
        65535.0,
    ).astype(">u2")
    rgb_path = output / f"{stem}_rgb.png"
    depth_path = output / f"{stem}_depth.png"
    depth_npy_path = output / f"{stem}_depth_m.npy"
    _write_png(rgb_path, rgb, bit_depth=8, color_type=2)
    _write_png(depth_path, depth_mm, bit_depth=16, color_type=0)
    np.save(depth_npy_path, np.asarray(frame.depth, dtype=np.float32))
    return {"rgb": rgb_path, "depth": depth_path, "depth_m": depth_npy_path}
