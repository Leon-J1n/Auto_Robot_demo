from __future__ import annotations

from sim_platform._logging import log


def testPrintPy(value: str) -> None:
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    log(2, "Algorithms.testPrintPy", value)


def testPyReg() -> None:
    log(2, "Algorithms.testPyReg", "PyREG")
