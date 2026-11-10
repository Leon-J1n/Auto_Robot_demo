@echo off
rem Apply OLS git hooks to this clone. Run once after cloning: hooks\install_hooks.bat
setlocal
cd /d "%~dp0.."
git config core.hooksPath hooks
if errorlevel 1 (
    echo [ols-hooks] failed to set core.hooksPath
    exit /b 1
)
echo [ols-hooks] installed: core.hooksPath=hooks (post-commit version refresh)
endlocal