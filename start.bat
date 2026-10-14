@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title EVE 工业计算器 - 欧服

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONNOUSERSITE=1"

rem ============================================================
rem  1) 优先使用随包附带的内置运行时（便携版，无需安装 Python）
rem ============================================================
set "PY="
set "PY_KIND="
if exist "%~dp0runtime\python.exe" (
  set "PY=%~dp0runtime\python.exe"
  set "PY_KIND=内置运行时（免安装）"
)

rem ============================================================
rem  2) 没有内置运行时，才去找系统安装的 Python 3
rem ============================================================
if not defined PY (
  where python >nul 2>nul && set "PY=python" && set "PY_KIND=系统 Python"
)
if not defined PY (
  where py >nul 2>nul && set "PY=py -3" && set "PY_KIND=系统 Python (py 启动器)"
)
if not defined PY (
  for %%D in (
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%ProgramFiles%\Python314\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "C:\Python314\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
  ) do (
    if not defined PY if exist %%D set "PY=%%~D" & set "PY_KIND=系统 Python"
  )
)

if not defined PY (
  echo.
  echo   [错误] 没有找到可用的 Python。
  echo.
  echo   这个版本的程序需要 Python 3.8 以上。有两种解决办法：
  echo.
  echo     A. 用「便携版」—— 双击 打包便携版.bat，它会自动下载一个免安装的
  echo        Python 运行时放进 runtime\ 目录，之后再启动就不需要装 Python 了。
  echo     B. 自己安装 Python：https://www.python.org/downloads/windows/
  echo        安装时务必勾选 "Add python.exe to PATH"，然后重新双击本文件。
  echo.
  pause
  exit /b 1
)

rem ---- 检查版本 ----
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>nul
if errorlevel 1 (
  echo   [错误] Python 版本过低，需要 3.8 或更高。
  %PY% -V
  pause
  exit /b 1
)

echo.
echo   正在启动 EVE 工业计算器...
echo   使用：%PY_KIND%
echo   浏览器会自动打开；关闭本窗口或按 Ctrl+C 即可停止。
echo.

%PY% -m app.main %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo   程序退出，返回码 %RC%
  pause
)
endlocal
