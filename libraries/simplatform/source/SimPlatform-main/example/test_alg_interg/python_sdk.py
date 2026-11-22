from __future__ import annotations

from sim_algorithms import registry, testHelloworld, testPrintInt, testPrintPy
from sim_platform import api_version


def main() -> None:
    assert api_version() > 0
    testHelloworld()
    testPrintInt(42)
    testPrintPy("Python SDK")
    registry["testCReg"]()
    registry["testPyReg"]()


if __name__ == "__main__":
    main()
