from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

from sim_platform._logging import log


MODULE_PATH = Path(__file__).with_name("python_algorithms.py")
SPEC = importlib.util.spec_from_file_location("sim_algorithms_bridge_functions", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load Python algorithms from {MODULE_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
testPrintPy = MODULE.testPrintPy
testPyReg = MODULE.testPyReg


def main() -> int:
    if len(sys.argv) < 2:
        log(4, "Algorithms.bridge", "missing Python algorithm name")
        return 2
    name = sys.argv[1]
    if name == "testPrintPy" and len(sys.argv) == 3:
        if not sys.argv[2].startswith("b64:"):
            log(4, "Algorithms.bridge", "invalid string payload")
            return 2
        testPrintPy(base64.b64decode(sys.argv[2][4:]).decode("utf-8"))
        return 0
    if name == "testPyReg" and len(sys.argv) == 2:
        testPyReg()
        return 0
    log(4, "Algorithms.bridge", f"unsupported Python algorithm invocation: {name}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
