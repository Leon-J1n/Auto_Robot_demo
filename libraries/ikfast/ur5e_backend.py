"""Double-precision C ABI for the vendored UR5e IKFast solver."""

from __future__ import annotations

import ctypes
from functools import lru_cache
from pathlib import Path
import os
import sys
from threading import Lock

import numpy as np


_load_lock = Lock()


def _library():
    # lru_cache alone permits simultaneous first calls to execute twice.
    with _load_lock:
        return _load_library()


@lru_cache(maxsize=1)
def _load_library():
    name = "ur5e_ikfast.dll" if os.name == "nt" else "libur5e_ikfast.so"
    path = Path(__file__).with_name(name)
    if not path.is_file():
        raise RuntimeError("IKFast backend missing; run python -m libraries.ikfast.build")
    lib = ctypes.CDLL(str(path))
    # The vendored solver uses LP64 LAPACK. NumPy's ILP64 OpenBLAS is NOT
    # ABI-compatible. Use SciPy's LP64 wheel, including a Conda base install.
    roots = [Path(sys.prefix)/"Lib/site-packages/scipy.libs",
             Path(sys.base_prefix)/"Lib/site-packages/scipy.libs"]
    try:
        import scipy
        roots.insert(0, Path(scipy.__file__).resolve().parent.parent/"scipy.libs")
    except ImportError:
        pass
    lapack = None
    for root in roots:
        for candidate in root.glob("*openblas*"):
            if "64_" not in candidate.name and candidate.suffix in {".dll", ".so"}:
                lapack = ctypes.CDLL(str(candidate))
                break
        if lapack is not None:
            break
    if lapack is None:
        raise RuntimeError("LP64 LAPACK missing; install scipy in this Python environment")
    functions = [getattr(lapack, "scipy_"+name+"_") for name in ("dgetrf", "dgetri", "dgetrs", "dgeev")]
    lib.ur5e_set_lapack.argtypes = [ctypes.c_void_p]*4
    lib.ur5e_set_lapack.restype = None
    lib.ur5e_set_lapack(*[ctypes.cast(f, ctypes.c_void_p) for f in functions])
    lib._lapack = lapack  # Keep dependency loaded for the native function pointers.
    pointer = np.ctypeslib.ndpointer(dtype=np.float64, flags="C_CONTIGUOUS")
    lib.ur5e_inverse.argtypes = [pointer, pointer, pointer, ctypes.c_int]
    lib.ur5e_inverse.restype = ctypes.c_int
    lib.ur5e_forward.argtypes = [pointer, pointer, pointer]
    lib.ur5e_forward.restype = None
    return lib


def solve(position, rotation):
    position = np.ascontiguousarray(position, dtype=np.float64).reshape(3)
    rotation = np.ascontiguousarray(rotation, dtype=np.float64).reshape(3, 3)
    if not np.all(np.isfinite(position)) or not np.all(np.isfinite(rotation)):
        raise ValueError("IKFast target must be finite")
    # A Cartesian branch search asks for the same pose with many seeds. The
    # analytic roots do not depend on the seed or scene collision state.
    return [list(solution) for solution in _solve_cached(tuple(position), tuple(rotation.reshape(-1)))]


@lru_cache(maxsize=2048)
def _solve_cached(position_key, rotation_key):
    position = np.asarray(position_key, dtype=np.float64)
    rotation = np.asarray(rotation_key, dtype=np.float64).reshape(3, 3)
    result = np.empty((16, 6), dtype=np.float64)
    count = _library().ur5e_inverse(position, rotation, result, len(result))
    if count < 0:
        raise RuntimeError(f"IKFast native error: {count}")
    return tuple(tuple(row) for row in result[:count])


def forward(joints):
    joints = np.ascontiguousarray(joints, dtype=np.float64).reshape(6)
    position, rotation = np.empty(3), np.empty((3, 3))
    _library().ur5e_forward(joints, position, rotation)
    return position, rotation
