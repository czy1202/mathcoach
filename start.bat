@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python。请先安装 Python 3.10 或更高版本，并勾选 Add Python to PATH。
  pause
  exit /b 1
)

if not exist .venv (
  echo 正在创建虚拟环境...
  python -m venv .venv
)

call .venv\Scripts\activate.bat

echo 正在安装依赖...
pip install -r requirements.txt

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo 已创建 .env。请在记事本中填写 OPENAI_API_KEY，保存并关闭记事本后继续。
  notepad .env
)

echo.
echo 启动 mathcoach：http://127.0.0.1:8000
echo 关闭此窗口即可停止服务。
start "" http://127.0.0.1:8000
python -m web.app
pause
