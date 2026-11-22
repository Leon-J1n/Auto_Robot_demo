# UR5e IKFast runtime

This package vendors the Windows x64 C ABI backend from
`Auto_Robot_demo/libraries/ikfast`. The native solver uses the LP64 LAPACK
symbols shipped by the SciPy wheel, so the Agent Service Python environment
must install `requirements.txt`.

The service imports the adapter as
`agent_service.libraries.ikfast.ur5e_backend`; it does not depend on the
process working directory or a top-level `libraries` package.
