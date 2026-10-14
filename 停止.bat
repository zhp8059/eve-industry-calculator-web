@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo 正在停止 EVE 工业计算器...

set "PID="
set "PORT="
if exist "logs\server.pid" set /p PID=<"logs\server.pid"
if exist "logs\server.port" set /p PORT=<"logs\server.port"

set "KILLED="

rem ---- 1) 按 pid 杀，并**校验**（原来只看 taskkill 返回码，失败也当成功）----
if defined PID (
  taskkill /f /pid %PID% >nul 2>nul
  timeout /t 1 >nul
  tasklist /fi "PID eq %PID%" 2>nul | findstr /c:"%PID%" >nul
  if errorlevel 1 set "KILLED=1"
)

rem ---- 2) 按**实际端口**找监听进程（不再写死 8420）----
if not defined KILLED if defined PORT (
  for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
    taskkill /f /pid %%P >nul 2>nul && set "KILLED=1"
  )
)

rem ---- 3) 兜底：默认端口 8420 ----
if not defined KILLED (
  for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8420 " ^| findstr LISTENING') do (
    taskkill /f /pid %%P >nul 2>nul && set "KILLED=1"
  )
)

if exist "logs\server.pid" del "logs\server.pid" >nul 2>nul
if exist "logs\server.port" del "logs\server.port" >nul 2>nul

if defined KILLED (
  echo 已停止。
) else (
  echo 没有找到正在运行的服务^(可能已经关闭^)。
)
timeout /t 2 >nul
