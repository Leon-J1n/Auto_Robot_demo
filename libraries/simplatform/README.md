# SimPlatform 运行时组件

此目录统一保存项目当前使用的 SimPlatform FCL 组件。

```text
libraries/simplatform/
├── bin/
│   ├── sim_platform_c.dll
│   └── 依赖 DLL
└── source/
    └── SimPlatform-main/
        └── python/
            └── sim_platform/
```

`bin/` 中的 `sim_platform_c.dll` 是编译后的原生库，内部提供 FCL 碰撞和距离计算。`source/SimPlatform-main/python/` 是 Python SDK，`collision/simplatform_fcl.py` 通过它调用原生库。

运行 demo 时传入：

```powershell
--library libraries\simplatform\bin\sim_platform_c.dll
```

构建脚本仍位于 `libraries/simplatform_native/build.ps1`。它使用 `libraries/simplatform/source/SimPlatform-main` 作为源码，并把构建结果输出到本目录的 `bin/`。
