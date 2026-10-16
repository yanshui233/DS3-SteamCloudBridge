@echo off
REM ============================================================
REM  SteamCloudBridge - BRIDGE (save mover), interactive wizard
REM
REM  Double-click me. I will ask a few questions, then:
REM    1. read-only diagnosis (nothing is touched)
REM    2. show the plan       (nothing is written)
REM    3. write ONLY after you type YES
REM  Every write is preceded by a backup + a snapshot, so it can
REM  always be rolled back.
REM
REM  TIP: close Steam completely BEFORE running this. Writing while
REM  Steam is running is refused on purpose (it would race the cloud).
REM
REM  You can also pass a command straight through:
REM    RUN-BRIDGE.bat status --appid 374320
REM    RUN-BRIDGE.bat plan   --appid 374320 --source D:\DS30000.sl2
REM    RUN-BRIDGE.bat --help
REM
REM  NOTE: messages are ASCII on purpose - the batch parser and
REM  non-ASCII output do not mix reliably. See README.md for Chinese.
REM ============================================================
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0"

set "EXE=%~dp0SteamCloudBridgeBridge.exe"
set "PYF=%~dp0SteamCloudBridgeBridge.py"
set "RT=%~dp0runtime\python.exe"
set "CMDLINE="
set "RC=0"

set "ARGS=%*"
if not defined ARGS set "ARGS=wizard"

echo.
echo   SteamCloudBridge - bridge (save mover)
echo   --------------------------------------
echo.

REM ---- pick how to run it: bundled exe > bundled runtime > PATH python ----
if exist "%EXE%" set "CMDLINE="%EXE%""
if not defined CMDLINE if exist "%PYF%" call :find_python

if not defined CMDLINE (
  if not exist "%PYF%" (
    echo   [ERROR] SteamCloudBridgeBridge.py is missing.
    echo           Keep this .bat in the same folder as the bridge files.
    goto :fail_pause
  )
  echo   [ERROR] No bundled runtime and no Python 3 found on this computer.
  echo.
  echo   This package normally ships SteamCloudBridgeBridge.exe, which runs
  echo   without installing anything. Please check that the file is still
  echo   in this folder, next to RUN-BRIDGE.bat.
  echo.
  echo   Alternative: install Python 3 from https://www.python.org/downloads/
  echo        IMPORTANT - on the first installer screen tick
  echo        "Add python.exe to PATH" before clicking Install.
  echo        Then run this file again.
  goto :fail_pause
)

echo   Running: %CMDLINE% %ARGS%
echo.
%CMDLINE% %ARGS%
set "RC=%errorlevel%"

echo.
echo   ------------------------------------------------------------
if "%RC%"=="0" (
  echo   [OK] finished, nothing is left half-done.
) else (
  echo   [exit code %RC%] The run did not complete cleanly.
  echo   Exit codes: 2 = blocked or you cancelled  ^(nothing written^)
  echo               3 = Steam is still running     ^(close it and retry^)
  echo               1 = failed, see the messages above
  echo   Nothing is written without your YES, and every write has a
  echo   backup + a snapshot next to it. Chinese details: README.md
)
echo   ------------------------------------------------------------

if exist "%~dp0backup" (
  echo.
  echo   Opening the backup folder ^(that is your undo button^) ...
  start "" "%~dp0backup"
)

echo.
echo   Press any key to close this window.
pause >nul
exit /b %RC%

REM ------------------------------------------------------------------
REM  Subroutine: locate a Python 3 and build the command line.
REM  Note the two nested `if` blocks - `%errorlevel%` inside a ( ) block
REM  is expanded too early, so only `&&` is used there.
REM ------------------------------------------------------------------
:find_python
if exist "%RT%" (
  set "CMDLINE="%RT%" "%PYF%""
  goto :eof
)
where py >nul 2>nul && set "CMDLINE=py -3 "%PYF%""
if not defined CMDLINE ( where python >nul 2>nul && set "CMDLINE=python "%PYF%"" )
if not defined CMDLINE ( where python3 >nul 2>nul && set "CMDLINE=python3 "%PYF%"" )
goto :eof

:fail_pause
echo.
pause
exit /b 2
