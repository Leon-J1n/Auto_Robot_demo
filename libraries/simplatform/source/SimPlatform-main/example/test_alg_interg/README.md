# Algorithm integration test

Configure and build a minimal tree:

```powershell
cmake -S . -B build/algorithm-integration -DSIM_ENABLE_IFPS_CPU_PLUGIN=OFF
cmake --build build/algorithm-integration --config Release `
  --target test_algorithm_integration_c sim_c_api
```

Run the C example:

```powershell
# MinGW / Ninja single-configuration build
build\algorithm-integration\example\test_alg_interg\test_algorithm_integration_c.exe

# Visual Studio multi-configuration build
build\algorithm-integration\example\test_alg_interg\Release\test_algorithm_integration_c.exe
```

Run the Python SDK example:

```powershell
$env:PYTHONPATH = "$PWD\python"
$env:SIM_ALGORITHMS_LIBRARY = "$PWD\build\algorithm-integration\sim_algorithms_c.dll"
$env:SIM_PLATFORM_LIBRARY = "$PWD\build\algorithm-integration\sim_platform_c.dll"
python example\test_alg_interg\python_sdk.py
```

For Visual Studio builds, insert `Release\` before each DLL filename.

Launch a Live host, then use its reported PID:

```powershell
simulator-python launch --kind headless --name algorithm-test --detach
python example\test_alg_interg\live_control.py --connect pid:12345
```

The Live client uses the same `PYTHONPATH` and `SIM_ALGORITHMS_LIBRARY` values as
the Python SDK example. The Live host must use a full platform build that includes
the selected collision backend, for example `build\windows-full\Release\sim_platform_c.dll`.

Expected output from each example contains `Hello World!`, `42`, its Python string,
`CREG`, and `PyREG`.

The feature is controlled by `SIM_BUILD_ALGORITHMS`; the examples additionally use
`SIM_BUILD_ALGORITHM_INTEGRATION_EXAMPLES`. Both options default to `ON`.
