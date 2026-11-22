# SimPlatform

SimPlatform 的运行库、SDK、原生源码、构建依赖和脚本统一放在本目录。

```text
libraries/simplatform/
├── bin/                       # 当前运行库及依赖 DLL；Linux 构建输出 .so
├── source/SimPlatform-main/   # C++ 原生源码、C API、Python SDK、测试
├── deps/                      # 本机 BqLog 源码、vcpkg 及依赖（Git 忽略）
├── build/windows/             # 新的 Windows 构建目录
├── build/linux/               # 新的 Linux 构建目录
├── build-legacy/              # 迁移前的构建产物，仅保留追溯
├── build.ps1                  # Windows 构建入口
├── build.sh                   # Linux 构建入口
├── NATIVE_BUILD.md            # 历史原生优化记录与构建说明
├── build_manifest.json        # 历史构建清单，哈希不代表当前新构建
└── source_manifest.json       # 导入归档与兼容源码来源
```

原 `simplatform_native` 存放编译工具和依赖，`simplatform` 存放运行组件；现已合并。
旧 CMake 缓存、vcpkg 本机缓存以及历史日志含迁移前绝对路径，不应直接用于新构建。
保留它们只为追溯，重新构建使用 `build/windows` 或 `build/linux`。

## 源码来源

用户提供的 `SimPlatform-main.zip` 补齐了缺失源码及资源；已有 Python SDK 和
`bin/` 运行库未覆盖。归档缺少当前 SDK 使用的原生过滤 C API，因此 `src/`、
`include/`、`tests/` 和 CMake 配置以历史提交 `deb4c4a` 的兼容实现为基础恢复。
随后仅调整 CMake 的 BqLog 默认路径及平台默认值。详细文件清单和归档校验和见
`source_manifest.json`。当前 DLL 未重新编译；源码与运行库的数值一致性仍需重建后验证。

## Windows

```powershell
& libraries/simplatform/build.ps1
```

脚本使用本目录 `deps/vcpkg-master` 和 `deps/BqLog-main`，输出到 `bin/`。
它需要 Visual Studio 2022 C++ 工具链和已安装的 FCL、Assimp、urdfdom 依赖。
重新运行 vcpkg 时，若其本机缓存引用迁移前目录，应重新 bootstrap/configure。

## Linux

先安装 C++20 编译器、CMake >= 3.22，以及 FCL、Assimp、urdfdom 和相关开发依赖。
例如 Ubuntu 的基础依赖通常为：

```bash
sudo apt install build-essential cmake libfcl-dev libassimp-dev liburdfdom-dev
bash libraries/simplatform/build.sh
```

BqLog 默认从 `deps/BqLog-main` 读取，可设置 `BQLOG_ROOT` 指向另一份完整源码。
移动到 Linux 时需要携带 BqLog 源码或单独安装；Git 不会自动包含 `deps/`。
Windows 的 vcpkg 安装产物不能当作 Linux 依赖使用。
生成库为 `bin/libsim_platform_c.so`；可通过 `--library` 显式选择：

```bash
python -B -m demo.run_demo --commands requests/commands.004/commands.004.json \
  --library libraries/simplatform/bin/libsim_platform_c.so
```

Linux 脚本已提供，但尚未在 Linux 环境编译验证。完整 Linux 部署仍需配置 Python
依赖、VLM 凭据和 MuJoCo 渲染环境；本次目录合并不代表完整部署已验证。
