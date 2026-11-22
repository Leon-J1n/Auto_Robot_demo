"""Double-precision C ABI for the vendored UR5e IKFast solver."""

from __future__ import annotations

import ctypes
from functools import lru_cache
import logging
import os
from pathlib import Path
import sys
from threading import Lock

import numpy as np


LOGGER = logging.getLogger(__name__)
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
        raise RuntimeError(f"IKFast backend missing beside {Path(__file__).name}")
    LOGGER.debug("Loading vendored IKFast native backend: %s", path.name)
    lib = ctypes.CDLL(str(path))

    # The vendored solver uses LP64 LAPACK. NumPy's ILP64 OpenBLAS is not ABI
    # compatible. SciPy wheels expose scipy_* symbols; Conda commonly uses MKL
    # and exposes the standard Fortran symbols from Library/bin/mkl_rt*.dll.
    roots = [Path(sys.prefix) / "Lib/site-packages/scipy.libs",
             Path(sys.base_prefix) / "Lib/site-packages/scipy.libs"]
    try:
        import scipy
        roots.insert(0, Path(scipy.__file__).resolve().parent.parent / "scipy.libs")
    except ImportError:
        pass
    candidates: list[tuple[Path, str]] = []
    for root in roots:
        for candidate in root.glob("*openblas*"):
            if "64_" not in candidate.name and candidate.suffix in {".dll", ".so"}:
                candidates.append((candidate, "scipy_"))
    for prefix in {Path(sys.prefix), Path(sys.base_prefix)}:
        library_bin = prefix / "Library/bin"
        candidates.extend((candidate, "") for candidate in library_bin.glob("mkl_rt*.dll"))
        candidates.extend((candidate, "") for candidate in library_bin.glob("*openblas*.dll"))

    lapack = None
    functions = None
    for candidate, symbol_prefix in candidates:
        try:
            provider = ctypes.CDLL(str(candidate))
            resolved = [getattr(provider, symbol_prefix + function + "_")
                        for function in ("dgetrf", "dgetri", "dgetrs", "dgeev")]
        except (OSError, AttributeError):
            continue
        lapack, functions = provider, resolved
        LOGGER.debug("Using LP64 LAPACK provider: %s", candidate.name)
        break
    if lapack is None or functions is None:
        raise RuntimeError(
            "LP64 LAPACK provider missing; install a SciPy wheel or Conda MKL runtime")
    lib.ur5e_set_lapack.argtypes = [ctypes.c_void_p] * 4
    lib.ur5e_set_lapack.restype = None
    lib.ur5e_set_lapack(*[ctypes.cast(function, ctypes.c_void_p)
                          for function in functions])
    lib._lapack = lapack  # Keep native function pointers alive.
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
    return [list(solution) for solution in
            _solve_cached(tuple(position), tuple(rotation.reshape(-1)))]


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
