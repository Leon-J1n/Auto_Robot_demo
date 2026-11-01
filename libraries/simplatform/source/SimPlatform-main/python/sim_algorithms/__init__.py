from ._native import testCReg, testHelloworld, testPrintInt
from .python_algorithms import testPrintPy, testPyReg
from .registry import call, get, names, register, registry


for _name, _function in (
    ("testHelloworld", testHelloworld),
    ("testPrintInt", testPrintInt),
    ("testPrintPy", testPrintPy),
    ("testCReg", testCReg),
    ("testPyReg", testPyReg),
):
    register(_name, _function)

del _name, _function

__all__ = [
    "call", "get", "names", "register", "registry",
    "testHelloworld", "testPrintInt", "testPrintPy", "testCReg", "testPyReg",
]
