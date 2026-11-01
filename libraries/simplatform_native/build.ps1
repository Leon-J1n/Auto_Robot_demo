param([int]$Jobs = 4)
$ErrorActionPreference = 'Stop'
$nativeRoot = $PSScriptRoot
$projectRoot = (Resolve-Path "$nativeRoot/../..").Path
$sourceRoot = "$projectRoot/libraries/simplatform/source/SimPlatform-main"
$vcpkgRoot = "$nativeRoot/deps/vcpkg-master"
$bqRoot = "$nativeRoot/deps/BqLog-main"
if (!(Test-Path "$vcpkgRoot/installed/x64-windows/share/fcl")) { throw 'Build dependencies are missing; see README.md' }
& cmake -S $sourceRoot -B "$nativeRoot/build" -G 'Visual Studio 17 2022' -A x64 `
    "-DCMAKE_TOOLCHAIN_FILE=$vcpkgRoot/scripts/buildsystems/vcpkg.cmake" `
    -DVCPKG_MANIFEST_MODE=OFF "-DBQLOG_ROOT=$bqRoot" `
    -DSIM_ENABLE_FCL=ON -DSIM_ENABLE_ASSIMP=ON -DSIM_ENABLE_URDFDOM=ON `
    -DSIM_ENABLE_MUJOCO=OFF -DSIM_ENABLE_IFPS_CPU_PLUGIN=OFF `
    -DSIM_BUILD_ALGORITHMS=OFF -DSIM_BUILD_ALGORITHM_INTEGRATION_EXAMPLES=OFF `
    -DSIM_BUILD_CLI=OFF -DSIM_BUILD_TESTS=ON
if ($LASTEXITCODE) { throw 'CMake configuration failed' }
& cmake --build "$nativeRoot/build" --config Release --parallel $Jobs
if ($LASTEXITCODE) { throw 'Native build failed' }
New-Item -ItemType Directory -Force "$projectRoot/libraries/simplatform/bin" | Out-Null
Copy-Item -LiteralPath "$nativeRoot/build/Release/sim_platform_c.dll" -Destination "$projectRoot/libraries/simplatform/bin"
Get-ChildItem -LiteralPath "$vcpkgRoot/installed/x64-windows/bin" -Filter '*.dll' |
    Copy-Item -Destination "$projectRoot/libraries/simplatform/bin"
Write-Output "DLL: $projectRoot/libraries/simplatform/bin/sim_platform_c.dll"
