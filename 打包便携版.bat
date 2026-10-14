@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

title 构建便携版 - EVE 工业计算器

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem ---- 优先用已存在的内置运行时（重复打包时就不用系统 Python 了）----
set "PY="
if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"

rem ---- 否则找一个系统 Python 3（首次构建需要它来下载运行时）----
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  for %%D in (
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%ProgramFiles%\Python314\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
  ) do (
    if not defined PY if exist %%D set "PY=%%~D"
  )
)

if not defined PY (
  echo.
  echo   [错误] 首次构建便携版需要一个 Python 才能下载官方运行时。
  echo.
  echo   请先安装 Python 3.8+：https://www.python.org/downloads/windows/
  echo   （安装时勾选 "Add python.exe to PATH"）
  echo.
  echo   说明：只有「构建」这一步需要 Python；构建完成后生成的便携版
  echo         自己带运行时，分发给别人时对方无需安装任何东西。
  echo.
  pause
  exit /b 1
)

echo.
echo   正在构建便携版，首次会下载约 11 MB 的官方免安装 Python 运行时...
echo.

%PY% tools\build_portable.py %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo   构建完成。便携包在 build\eve-industry-portable.zip
  echo   解压后双击里面的 start.bat 即可，无需安装 Python。
) else (
  echo   构建以返回码 %RC% 结束，请阅读上面的提示。
)
echo.
pause
endlocal
