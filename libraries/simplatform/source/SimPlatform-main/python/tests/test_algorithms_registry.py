from __future__ import annotations

import sim_algorithms
from sim_algorithms import registry


def test_algorithm_registry_contains_all_integration_functions() -> None:
    assert set(registry) == {
        "testHelloworld", "testPrintInt", "testPrintPy", "testCReg", "testPyReg",
    }
    assert registry["testHelloworld"] is sim_algorithms.testHelloworld
    assert registry["testPrintPy"] is sim_algorithms.testPrintPy


def test_python_algorithms(capsys) -> None:
    sim_algorithms.testPrintPy("python value")
    sim_algorithms.testPrintPy("")
    registry["testPyReg"]()
    assert capsys.readouterr().out == ""
