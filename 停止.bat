@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
echo 正在停止 EVE 工业计算器...
set "KILLED="
if exist "logs\server.pid" (
  set /p PID=<"logs\server.pid"
  if defined PID (
    taskkill /f /pid %PID% >nul 2>nul && set "KILLED=1"
    del "logs\server.pid" >nul 2>nul
  )
)
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8420" ^| findstr "LISTENING"') do (
  taskkill /f /pid %%P >nul 2>nul && set "KILLED=1"
)
if defined KILLED (echo 已停止。) else (echo 没有发现正在运行的服务。)
timeout /t 2 >nul
