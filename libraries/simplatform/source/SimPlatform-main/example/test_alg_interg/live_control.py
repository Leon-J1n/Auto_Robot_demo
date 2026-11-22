from __future__ import annotations

import argparse

from sim_algorithms import registry, testHelloworld, testPrintInt, testPrintPy
from sim_platform.live import connect


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connect", required=True)
    args = parser.parse_args()
    with connect(args.connect) as simulator:
        simulator.status()
        testHelloworld()
        testPrintInt(42)
        testPrintPy("Live Control SDK")
        registry["testCReg"]()
        registry["testPyReg"]()
        simulator.step(1)


if __name__ == "__main__":
    main()
