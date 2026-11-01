from __future__ import annotations

import ctypes
import os
from pathlib import Path


def _library_path() -> Path:
    configured = os.environ.get("SIM_ALGORITHMS_LIBRARY")
    if configured:
        return Path(configured)
    root = Path(__file__).resolve().parents[2]
    names = ("sim_algorithms_c.dll", "libsim_algorithms_c.so", "libsim_algorithms_c.dylib")
    directories = (
        root / "build" / "algorithm-integration" / "Release",
        root / "build" / "algorithm-integration",
        root / "build" / "windows-full" / "Release",
        root / "build" / "windows-gui" / "Release",
    )
    for directory in directories:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    raise FileNotFoundError("set SIM_ALGORITHMS_LIBRARY to the sim_algorithms_c library")


_LIB = ctypes.CDLL(str(_library_path()))
_LIB.testHelloworld.argtypes = []
_LIB.testHelloworld.restype = ctypes.c_int
_LIB.testPrintInt.argtypes = [ctypes.c_int]
_LIB.testPrintInt.restype = ctypes.c_int
_LIB.testCReg.argtypes = []
_LIB.testCReg.restype = ctypes.c_int
_LIB.sim_algorithms_last_error.argtypes = []
_LIB.sim_algorithms_last_error.restype = ctypes.c_char_p


def _check(status: int) -> None:
    if status != 0:
        value = _LIB.sim_algorithms_last_error()
        raise RuntimeError(value.decode("utf-8") if value else f"algorithm failed: {status}")


def testHelloworld() -> None:
    _check(_LIB.testHelloworld())


def testPrintInt(num: int) -> None:
    _check(_LIB.testPrintInt(num))


def testCReg() -> None:
    _check(_LIB.testCReg())
