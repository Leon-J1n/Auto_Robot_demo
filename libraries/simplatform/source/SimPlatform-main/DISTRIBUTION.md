# WebEditor 发布包

使用项目根目录的 `Distribute.bat VERSION` 生成 `distribute\VERSION`。发布包包含：

- WebEditor 的生产构建 `web\dist`，由 Python Gateway 直接托管；
- Agent Service、Gateway 和 Python SDK（不包含 `example`、`examples`、测试目录）；
- GUI/full Release 的 EXE、DLL、Qt/物理后端运行库和插件；
- `Start-WebEditor.ps1`、`Start-LAN-WebEditor.ps1`、`Stop-WebEditor.ps1`；
- README 和交接文档。

目标机器需要安装 Python 3.10+ 以及可用的 `python` PATH 命令。首次部署后，在发布包根目录执行一键部署脚本：

```bat
.\Deploy-WebEditor.bat
```

脚本会安装 `pyproject.toml` 中的 `[web]` 依赖并启动 Agent、Gateway 和 WebEditor。

默认地址为 `http://127.0.0.1:5173`，Gateway 为 `http://127.0.0.1:8080`，Agent 为 `127.0.0.1:8765`。局域网部署使用：

```powershell
powershell -ExecutionPolicy Bypass -File .\Start-LAN-WebEditor.ps1
```

停止服务：

```powershell
powershell -ExecutionPolicy Bypass -File .\Stop-WebEditor.ps1
```

发布包不包含 Python、Node.js 或 GPU 驱动本身。
