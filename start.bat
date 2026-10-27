@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

"%PY%" -c "import fastapi, uvicorn, mijiaAPI, qrcode" >nul 2>&1
if errorlevel 1 (
  echo [错误] 依赖未安装，请先执行：
  echo     %PY% -m pip install -r requirements.txt
  echo 国内网络建议加源： -i https://mirrors.aliyun.com/pypi/simple/
  pause
  exit /b 1
)

echo 启动 MiHome Web 测试环境...
echo 关闭本窗口即停止服务。
echo.
"%PY%" run_local.py
pause
